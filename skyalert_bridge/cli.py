from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

from . import __version__
from .app import SkyAlertBridgeApp
from .announcer import apply_tail_message
from .audio import ensure_pcm_wav
from .config import ConfigError, load_config
from .control import write_audio_request
from .encoder import EncoderError
from .helpers import resolve_helper_path
from .logutil import configure_logging
from .managed_openbridge import HBPForwarder, ManagedOpenBridgeSupervisor, dmrd_for_test
from .openbridge import OpenBridgeEndpoint, OpenBridgeSender
from .process_manager import ProcessSupervisor
from .state import StateStore
from .tts import TTS

logger = logging.getLogger(__name__)


def _load(path: str):
    cfg = load_config(path)
    configure_logging(cfg.app.log_level)
    return cfg


def _mode(cfg) -> str:
    return "dry_run" if cfg.app.dry_run else cfg.output.mode.lower()


def _command_exists(command: str) -> bool:
    if not command:
        return False
    first = command.strip().split()[0]
    if "/" in first:
        return Path(first).exists()
    return shutil.which(first) is not None


def _helper_status(kind: str, configured: str) -> tuple[str, bool]:
    resolved = resolve_helper_path(kind, configured)
    return (resolved or configured, resolved is not None)


def _warnings_for_config(cfg) -> list[str]:
    warnings: list[str] = []
    mode = _mode(cfg)
    if mode == "managed_dvswitch":
        for group in cfg.groups:
            if not group.enabled or group.bridge is None:
                continue
            if not group.bridge.files:
                warnings.append(f"{group.name}: managed_dvswitch has no bridge.files; no INI/rules files will be rendered")
            if not group.bridge.processes:
                warnings.append(f"{group.name}: managed_dvswitch has no bridge.processes; no HBLink/MMDVM/Analog_Bridge/md380-emu processes will start")
            for proc in group.bridge.processes:
                if "hblink.py" in proc.command and "bridge.py" not in proc.command:
                    warnings.append(f"{group.name}: process {proc.name!r} runs hblink.py; use bridge.py with a rules.py file to forward MASTER traffic to OPENBRIDGE")
    if mode == "managed_openbridge":
        mob = cfg.output.managed_openbridge
        ob = cfg.output.direct_openbridge
        if ob.network_id <= 0:
            warnings.append("managed_openbridge requires openbridge.network_id")
        if ob.target_ip in {"127.0.0.1", "CHANGE_ME_OPENBRIDGE_HOST", "unused-in-managed-mode"}:
            warnings.append("openbridge.target_ip still looks like a placeholder")
        if ob.passphrase in {"password", "CHANGE_ME_SHARED_SECRET", "unused", "CHANGE_ME_OPENBRIDGE_SECRET"}:
            warnings.append("openbridge.passphrase still looks like a placeholder")
        if not mob.start_helpers:
            warnings.append("managed_openbridge.start_helpers is false; app will not start Analog_Bridge/MMDVM_Bridge/md380-emu")
        if mob.start_helpers:
            for label, kind, command in (
                ("Analog_Bridge", "analog_bridge", mob.helper_paths.analog_bridge),
                ("MMDVM_Bridge", "mmdvm_bridge", mob.helper_paths.mmdvm_bridge),
                ("md380-emu", "md380_emu", mob.helper_paths.md380_emu),
            ):
                resolved, ok = _helper_status(kind, command)
                if not ok:
                    warnings.append(f"managed_openbridge helper for {label} was not found; install DVSwitch helpers or set helpers.{kind}")
        for group in cfg.groups:
            if not group.enabled:
                continue
            if group.bridge is None:
                warnings.append(f"{group.name}: managed_openbridge could not build bridge settings")
                continue
            if group.bridge.usrp.subscriber_id <= 0:
                warnings.append(f"{group.name}: station.source_id is 0; set a real DMR source ID")
            if group.bridge.usrp.repeater_id <= 0:
                warnings.append(f"{group.name}: station.repeater_id is 0; set a real peer/repeater ID")
            if group.bridge.usrp.subscriber_id == group.bridge.usrp.repeater_id:
                warnings.append(f"{group.name}: subscriber/source ID and repeater ID are both {group.bridge.usrp.subscriber_id}; Analog_Bridge requires different values")
            if group.bridge.usrp.callsign.upper() == "N0CALL":
                warnings.append(f"{group.name}: station.callsign is still N0CALL")
            if group.bridge.usrp.slot != 1:
                warnings.append(f"{group.name}: USRP/MMDVM slot is {group.bridge.usrp.slot}; most OpenBridge targets expect slot 1")
    if mode == "direct_openbridge" and cfg.encoder.command:
        first = cfg.encoder.command.strip().split()[0]
        if first.endswith(".py") and not first.startswith(("python", "python3", sys.executable)):
            path = Path(first)
            if path.exists() and not path.stat().st_mode & 0o111:
                warnings.append("direct_openbridge encoder command points to a non-executable Python script; prefix it with python3")
    return warnings


def _udp_listener_lines(ports: set[int]) -> list[str]:
    if not ports or shutil.which("ss") is None:
        return []
    try:
        result = subprocess.run(["ss", "-lunp"], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
    except Exception:
        return []
    lines: list[str] = []
    for line in result.stdout.splitlines():
        if any(f":{port} " in line or f":{port}\t" in line for port in ports):
            lines.append(line)
    return lines


def _configured_ports(cfg) -> set[int]:
    ports: set[int] = set()
    for group in cfg.groups:
        if not group.enabled:
            continue
        bridge = group.bridge
        usrp = bridge.usrp if bridge else cfg.output.analog_bridge_usrp
        ports.add(usrp.tx_port)
        if usrp.local_rx_port is not None:
            ports.add(usrp.local_rx_port)
        if bridge:
            for key in ("ab_ambe_tx_port", "ab_ambe_rx_port", "mmdvm_local_port", "hbp_master_port", "openbridge_local_port", "md380emu_port"):
                value = bridge.variables.get(key)
                if value and str(value).isdigit():
                    ports.add(int(value))
    return ports


def cmd_check_config(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    mode = _mode(cfg)
    print(f"OK: {cfg.path}")
    print(f"Groups: {len(cfg.groups)}")
    for group in cfg.groups:
        enabled = "enabled" if group.enabled else "disabled"
        bridge = group.bridge
        if bridge is None:
            bridge_text = "default USRP endpoint"
        else:
            process_count = sum(1 for item in bridge.processes if item.enabled)
            file_count = len(bridge.files)
            if mode == "managed_openbridge":
                bridge_text = "auto-managed local bridge"
            else:
                bridge_text = f"USRP {bridge.usrp.address}:{bridge.usrp.tx_port}, managed files={file_count}, managed processes={process_count}"
        print(f"  - {group.name}: TG {group.talkgroup}, zones {','.join(group.county_codes)} ({enabled}); {bridge_text}")
    print(f"Output mode: {'dry_run' if cfg.app.dry_run else cfg.output.mode}")
    print(
        f"Audio scheduler: {cfg.audio_scheduler.mode}, "
        f"max_concurrent_groups={cfg.audio_scheduler.max_concurrent_groups}, "
        f"same_group_policy={cfg.audio_scheduler.same_group_policy}"
    )
    print(
        f"Alert repeat: {'enabled' if cfg.alert_repeat.enabled else 'disabled'}, "
        f"unchanged_policy={cfg.alert_repeat.unchanged_policy}, "
        f"repeat_after_minutes={cfg.alert_repeat.repeat_after_minutes}"
    )
    if mode == "managed_openbridge":
        ob = cfg.output.direct_openbridge
        print(f"OpenBridge target: {ob.target_ip}:{ob.target_port} network_id={ob.network_id} local={ob.local_ip}:{ob.local_port}")
    warnings = _warnings_for_config(cfg)
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    mode = _mode(cfg)
    print(f"Config: {cfg.path}")
    print(f"Output mode: {mode}")
    print(
        f"Audio scheduler: {cfg.audio_scheduler.mode}, "
        f"max_concurrent_groups={cfg.audio_scheduler.max_concurrent_groups}, "
        f"same_group_policy={cfg.audio_scheduler.same_group_policy}"
    )
    print(
        f"Alert repeat: {'enabled' if cfg.alert_repeat.enabled else 'disabled'}, "
        f"unchanged_policy={cfg.alert_repeat.unchanged_policy}, "
        f"repeat_after_minutes={cfg.alert_repeat.repeat_after_minutes}"
    )
    warnings = _warnings_for_config(cfg)
    if warnings:
        print("\nLikely problems:")
        for warning in warnings:
            print(f"  - {warning}")
    else:
        print("\nNo static config problems detected.")

    if mode == "managed_openbridge":
        mob = cfg.output.managed_openbridge
        print("\nManaged helper paths:")
        for label, kind, command in (
            ("Analog_Bridge", "analog_bridge", mob.helper_paths.analog_bridge),
            ("MMDVM_Bridge", "mmdvm_bridge", mob.helper_paths.mmdvm_bridge),
            ("md380-emu", "md380_emu", mob.helper_paths.md380_emu),
        ):
            resolved, ok = _helper_status(kind, command)
            status = "found" if ok else "missing"
            display = resolved if ok else command
            print(f"  {label}: {display} ({status})")
        emu_path, emu_ok = _helper_status("md380_emu", mob.helper_paths.md380_emu)
        if emu_ok:
            configured_workdir = mob.helper_paths.md380_emu_workdir.strip()
            if not configured_workdir or configured_workdir.lower() == "auto":
                emu_workdir = str(Path(emu_path).resolve().parent)
            else:
                emu_workdir = configured_workdir
            configured_wrapper = mob.helper_paths.md380_emu_wrapper.strip()
            wrapper_display = "none"
            if configured_wrapper.lower() not in ("", "none", "false", "off"):
                if configured_wrapper.lower() == "auto":
                    local_wrapper = Path(emu_workdir) / "qemu-arm-static"
                    wrapper_display = str(local_wrapper) if local_wrapper.exists() else (resolve_helper_path("md380_emu_wrapper", "auto") or "auto-not-found")
                else:
                    wrapper_display = resolve_helper_path("md380_emu_wrapper", configured_wrapper) or configured_wrapper
            print(f"  md380-emu workdir: {emu_workdir}")
            print(f"  md380-emu wrapper: {wrapper_display}")
        ob = cfg.output.direct_openbridge
        print("\nOpenBridge outbound target:")
        print(f"  {ob.target_ip}:{ob.target_port} via local {ob.local_ip}:{ob.local_port}, network_id={ob.network_id}")

    ports = _configured_ports(cfg)
    if ports:
        print("\nConfigured local UDP ports:")
        print("  " + ", ".join(str(p) for p in sorted(ports)))
        listener_lines = _udp_listener_lines(ports)
        if listener_lines:
            print("\nCurrent matching UDP listeners from ss -lunp:")
            for line in listener_lines:
                print("  " + line)
        else:
            print("\nNo matching UDP listeners found via ss -lunp, or ss is unavailable.")
            if mode == "analog_bridge_usrp":
                print("  In analog_bridge_usrp mode, Analog_Bridge must already be running and listening on the group USRP rxPort.")
            if mode == "managed_dvswitch":
                print("  In managed_dvswitch mode, run test-audio with --keep-bridges-seconds and check state/bridges/<group>/logs/.")
            if mode == "managed_openbridge":
                print("  In managed_openbridge mode, embedded HBP listeners exist only while run/once/test-audio is active.")
                print("  Use test-audio --transmit --keep-bridges-seconds 20, then check state/bridges/<group>/logs/.")
    return 0


def cmd_render_managed(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    mode = _mode(cfg)
    if mode == "managed_openbridge":
        supervisor = ManagedOpenBridgeSupervisor(cfg)
        try:
            written = supervisor.render_files()
        finally:
            supervisor.stop(timeout=0.1)
    else:
        supervisor = ProcessSupervisor(cfg)
        written = supervisor.render_files()
    if not written:
        print("No managed bridge files rendered.")
        return 1
    for path in written:
        print(path)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    app = SkyAlertBridgeApp(cfg)
    try:
        return app.run_once()
    finally:
        app.close()


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    app = SkyAlertBridgeApp(cfg)
    try:
        app.run_forever()
    finally:
        app.close()
    return 0


def cmd_show_state(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    state = StateStore.load(cfg.app.state_file)
    print(json.dumps(state.data, indent=2, sort_keys=True))
    return 0


def cmd_test_audio(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    group = next((g for g in cfg.groups if g.name == args.group), None)
    if group is None:
        raise SystemExit(f"Unknown group: {args.group}")
    if args.queue:
        if not args.transmit:
            raise SystemExit("--queue is only meaningful with --transmit")
        path = write_audio_request(cfg.app.control_dir, group=group.name, text=args.text)
        print(f"Queued test audio request for the running service: {path}")
        print("The weather-alert-system service will synthesize and transmit it using the already-running helper chain.")
        return 0
    text = apply_tail_message(args.text, cfg.announcements)
    tts = TTS(cfg.tts, cfg.app.audio_dir)
    wav = tts.synthesize(text, basename=f"test-{group.name}")
    wav = ensure_pcm_wav(wav, cfg.audio)
    print(str(wav))
    if args.transmit:
        app = SkyAlertBridgeApp(cfg)
        try:
            app.transmitter.transmit(group, Path(wav), args.text)
            if args.keep_bridges_seconds > 0:
                logger.info("Keeping managed bridge children up for %.1f seconds", args.keep_bridges_seconds)
                time.sleep(args.keep_bridges_seconds)
        finally:
            app.close()
    return 0


def cmd_queue_audio(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    group = next((g for g in cfg.groups if g.name == args.group), None)
    if group is None:
        raise SystemExit(f"Unknown group: {args.group}")
    path = write_audio_request(cfg.app.control_dir, group=group.name, text=args.text)
    print(f"Queued audio request for the running service: {path}")
    return 0


def _recv_udp(sock: socket.socket, timeout: float = 2.0) -> bytes:
    sock.settimeout(timeout)
    data, _addr = sock.recvfrom(2048)
    return data


def _exercise_hbp_to_openbridge(group, forwarder: HBPForwarder, packet_count: int) -> None:
    if group.bridge is None:
        raise RuntimeError(f"Group {group.name} has no managed bridge settings")
    usrp = group.bridge.usrp
    peer_id_int = usrp.repeater_id or 1
    rf_src = usrp.subscriber_id or 1
    peer_id = peer_id_int.to_bytes(4, "big")
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2.0)
    target = ("127.0.0.1", forwarder.actual_port)
    try:
        client.sendto(b"RPTL" + peer_id, target)
        ack = _recv_udp(client)
        if not ack.startswith(b"RPTACK") or len(ack) < 10:
            raise RuntimeError(f"{group.name}: unexpected HBP login ACK: {ack!r}")
        salt = ack[6:10]
        digest = bytes.fromhex(hashlib.sha256(salt + b"skyalert-self-test").hexdigest())
        client.sendto(b"RPTK" + peer_id + digest, target)
        ack = _recv_udp(client)
        if ack != b"RPTACK" + peer_id:
            raise RuntimeError(f"{group.name}: HBP auth failed: {ack!r}")
        client.sendto(b"RPTC" + peer_id + b"0" * 300, target)
        ack = _recv_udp(client)
        if ack != b"RPTACK" + peer_id:
            raise RuntimeError(f"{group.name}: HBP config ACK failed: {ack!r}")
        client.sendto(b"RPTPING" + peer_id, target)
        pong = _recv_udp(client)
        if pong != b"MSTPONG" + peer_id:
            raise RuntimeError(f"{group.name}: HBP ping/PONG failed: {pong!r}")
        client.sendto(b"RPTP" + peer_id, target)
        pong = _recv_udp(client)
        if pong != b"MSTPONG" + peer_id:
            raise RuntimeError(f"{group.name}: HBP short ping/PONG failed: {pong!r}")
        stream_id = (0x1000 + (group.talkgroup & 0x0FFF)) & 0xFFFFFFFF
        for seq in range(1, packet_count + 1):
            frame = dmrd_for_test(peer_id_int, rf_src, group.talkgroup, seq=seq, slot=usrp.slot, stream_id=stream_id)
            client.sendto(frame, target)
    finally:
        client.close()


def _validate_openbridge_packet(packet: bytes, *, passphrase: str, network_id: int, talkgroup: int) -> None:
    if len(packet) != 73:
        raise RuntimeError(f"OpenBridge packet should be 73 bytes, got {len(packet)}")
    data = packet[:53]
    digest = packet[53:]
    if not data.startswith(b"DMRD"):
        raise RuntimeError(f"OpenBridge payload is not DMRD: {data[:4]!r}")
    got_network = int.from_bytes(data[11:15], "big")
    if got_network != network_id:
        raise RuntimeError(f"OpenBridge network ID mismatch: got {got_network}, expected {network_id}")
    got_tg = int.from_bytes(data[8:11], "big")
    if got_tg != talkgroup:
        raise RuntimeError(f"OpenBridge TG mismatch: got {got_tg}, expected {talkgroup}")
    expected = hmac.new(passphrase.encode("utf-8"), data, hashlib.sha1).digest()
    if not hmac.compare_digest(digest, expected):
        raise RuntimeError("OpenBridge HMAC-SHA1 validation failed")


def cmd_self_test_openbridge(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    enabled = [group for group in cfg.groups if group.enabled]
    if args.group:
        enabled = [group for group in enabled if group.name == args.group]
    if not enabled:
        raise SystemExit("No enabled groups matched the self-test request")

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(3.0)
    target_port = int(listener.getsockname()[1])
    ob_cfg = cfg.output.direct_openbridge
    endpoint = OpenBridgeEndpoint(
        local_ip="127.0.0.1",
        local_port=0,
        target_ip="127.0.0.1",
        target_port=target_port,
        passphrase=ob_cfg.passphrase,
        network_id=ob_cfg.network_id,
    )
    sender = OpenBridgeSender(endpoint)
    forwarders: list[HBPForwarder] = []
    try:
        for group in enabled:
            forwarder = HBPForwarder(
                group=group,
                bind_ip="127.0.0.1",
                port=0,
                hbp_password="skyalert-self-test",
                openbridge=sender,
                enforce_group_talkgroup=True,
            )
            forwarder.start()
            forwarders.append(forwarder)

        for group, forwarder in zip(enabled, forwarders):
            _exercise_hbp_to_openbridge(group, forwarder, args.frames)

        expected_count = len(enabled) * args.frames
        received: list[bytes] = []
        deadline = time.monotonic() + 3.0
        while len(received) < expected_count and time.monotonic() < deadline:
            listener.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                received.append(_recv_udp(listener, timeout=max(0.1, deadline - time.monotonic())))
            except socket.timeout:
                break

        if len(received) != expected_count:
            raise RuntimeError(f"Expected {expected_count} outbound OpenBridge packets, received {len(received)}")

        index = 0
        for group in enabled:
            for _ in range(args.frames):
                _validate_openbridge_packet(
                    received[index],
                    passphrase=ob_cfg.passphrase,
                    network_id=ob_cfg.network_id,
                    talkgroup=group.talkgroup,
                )
                index += 1

        print("OpenBridge egress self-test passed")
        print(f"  local UDP listener received {len(received)} OpenBridge packet(s)")
        print(f"  validated HMAC-SHA1, network_id={ob_cfg.network_id}, and talkgroup routing")
        for group in enabled:
            print(f"  - {group.name}: TG {group.talkgroup}, frames={args.frames}")
        return 0
    finally:
        for forwarder in forwarders:
            forwarder.stop()
        sender.close()
        listener.close()

def cmd_version(args: argparse.Namespace) -> int:
    print(f"weather-alert-system {__version__}")
    print(f"module path: {Path(__file__).resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weather-alert-system")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("version", help="Print installed Weather Alert System version and module path")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("check-config", help="Validate config and print a summary")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("doctor", help="Print local troubleshooting information")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("render-managed", help="Render managed bridge files without starting child processes")
    p.set_defaults(func=cmd_render_managed)

    p = sub.add_parser("once", help="Poll once and announce any changes")
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("run", help="Run continuously")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("show-state", help="Print current state file")
    p.set_defaults(func=cmd_show_state)

    p = sub.add_parser("test-audio", help="Generate and optionally transmit a test announcement")
    p.add_argument("--group", required=True, help="Configured group name")
    p.add_argument("--text", default="Skywarn test announcement.", help="Text to synthesize")
    p.add_argument("--transmit", action="store_true", help="Transmit after generating audio")
    p.add_argument("--queue", action="store_true", help="Queue the test for the already-running service instead of starting another helper chain")
    p.add_argument("--keep-bridges-seconds", type=float, default=0.0, help="After transmit, keep managed bridge child processes running briefly for debugging")
    p.set_defaults(func=cmd_test_audio)

    p = sub.add_parser("queue-audio", help="Queue text for the running service to synthesize and transmit without restarting helpers")
    p.add_argument("--group", required=True, help="Configured group name")
    p.add_argument("--text", required=True, help="Text to synthesize and transmit")
    p.set_defaults(func=cmd_queue_audio)

    p = sub.add_parser("self-test-openbridge", help="Loopback-test embedded HBP to outbound OpenBridge packet egress")
    p.add_argument("--group", help="Optional enabled group name to test; defaults to all enabled groups")
    p.add_argument("--frames", type=int, default=3, help="Synthetic DMRD voice frames to forward per group")
    p.set_defaults(func=cmd_self_test_openbridge)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except EncoderError as exc:
        print(f"Encoder error: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"Runtime error: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
