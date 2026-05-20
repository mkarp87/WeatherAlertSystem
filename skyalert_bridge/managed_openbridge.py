from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import hmac
import logging
from pathlib import Path
import random
import shlex
import socket
import subprocess
import threading
import time
import zlib
from typing import Any, BinaryIO

from .config import Config, GroupBridgeConfig, GroupConfig
from .helpers import require_helper_path
from .openbridge import OpenBridgeEndpoint, OpenBridgeSender, int_to_3

logger = logging.getLogger(__name__)

DMRD = b"DMRD"
RPTL = b"RPTL"
RPTACK = b"RPTACK"
RPTK = b"RPTK"
RPTC = b"RPTC"
RPTCL = b"RPTCL"
RPTP = b"RPTP"
RPTPING = b"RPTPING"
RPTO = b"RPTO"
MSTNAK = b"MSTNAK"
MSTPONG = b"MSTPONG"


ANALOG_BRIDGE_TEMPLATE = """[GENERAL]
logLevel = 2
exportMetadata = true
decoderFallBack = true
useEmulator = true
emulatorAddress = 127.0.0.1:{md380emu_port}

[AMBE_AUDIO]
address = 127.0.0.1
txPort = {ab_ambe_tx_port}
rxPort = {ab_ambe_rx_port}
ambeMode = DMR
minTxTimeMS = 2500
gatewayDmrId = {subscriber_id}
repeaterID = {repeater_id}
txTg = {talkgroup}
txTs = {slot}
colorCode = {color_code}

[USRP]
address = {usrp_address}
txPort = {usrp_local_rx_port}
rxPort = {usrp_tx_port}
usrpAudio = AUDIO_UNITY
usrpGain = 1.10
tlvAudio = AUDIO_UNITY
tlvGain = 0.35
"""


DVSWITCH_TEMPLATE = """[DMR]
address = 127.0.0.1
txPort = {ab_ambe_rx_port}
rxPort = {ab_ambe_tx_port}
slot = {slot}
exportTG = {talkgroup}
hangTimerInFrames = 0
"""


MMDVM_BRIDGE_TEMPLATE = """[General]
Callsign={callsign}
Id={mmdvm_id}
Timeout=180
Duplex=0

[Info]
RXFrequency=000000000
TXFrequency=000000000
Power=1
Latitude=0.0000
Longitude=0.0000
Height=0
Location=Weather Alert System
Description=Weather Alert System {group}
URL=https://groups.io/g/DVSwitch

[Log]
DisplayLevel=1
FileLevel=2
FilePath={bridge_dir}
FileRoot=MMDVM_Bridge

[Modem]
Port=/dev/null
RSSIMappingFile=/dev/null
Trace=0
Debug=0

[DMR]
Enable=1
ColorCode={color_code}
EmbeddedLCOnly=1
DumpTAData=0

[DMR Network]
Enable=1
Address={hbp_bind_ip}
Port={hbp_master_port}
Jitter=360
Local={mmdvm_local_port}
Password={hbp_password}
Slot1=1
Slot2=1
Debug=0

[D-Star]
Enable=0
[System Fusion]
Enable=0
[P25]
Enable=0
[NXDN]
Enable=0
"""


@dataclass
class RunningProcess:
    group: str
    name: str
    process: subprocess.Popen[Any]
    log_path: Path


@dataclass(frozen=True)
class RenderedBridgeFiles:
    analog_bridge_ini: Path
    dvswitch_ini: Path
    mmdvm_bridge_ini: Path

    def as_list(self) -> list[Path]:
        return [self.analog_bridge_ini, self.dvswitch_ini, self.mmdvm_bridge_ini]


class HBPForwarder:
    """Small Homebrew/MMDVM master endpoint that forwards DMRD to OpenBridge.

    This is intentionally limited to the pieces MMDVM_Bridge needs for outbound
    audio: repeater login, challenge, config ACK, pings, and DMRD voice/data
    packets. It is not a conference bridge and does not repeat traffic locally.
    """

    def __init__(
        self,
        group: GroupConfig,
        bind_ip: str,
        port: int,
        hbp_password: str,
        openbridge: OpenBridgeSender,
        *,
        enforce_group_talkgroup: bool = True,
    ):
        self.group = group
        self.bind_ip = bind_ip
        self.port = port
        self.hbp_password = hbp_password.encode("utf-8")
        self.openbridge = openbridge
        self.enforce_group_talkgroup = enforce_group_talkgroup
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_ip, port))
        self.sock.settimeout(0.25)
        self.actual_port = int(self.sock.getsockname()[1])
        self._peers: dict[bytes, dict[str, Any]] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self.forwarded_packets = 0
        self.dropped_packets = 0
        self.openbridge_packets = 0
        self.openbridge_bytes = 0
        self._stream_xor = zlib.crc32(group.name.encode("utf-8")) or 1
        # OpenBridge SRC_ID is shared globally for this app instance. Passing
        # None makes OpenBridgeSender use openbridge.network_id from config.
        self.openbridge_network_id: int | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name=f"hbp-{self.group.name}", daemon=True)
        self._thread.start()
        logger.info("Embedded HBP listener for %s on %s:%s", self.group.name, self.bind_ip, self.actual_port)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.sock.close()

    def _loop(self) -> None:
        while self._running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_datagram(data, addr)
            except Exception:
                logger.exception("Embedded HBP handler failed for %s from %s", self.group.name, addr)

    def _send(self, packet: bytes, addr: tuple[str, int], peer_id: bytes | None = None) -> None:
        if packet.startswith(DMRD) and peer_id is not None:
            packet = b"".join([packet[:11], peer_id, packet[15:]])
        self.sock.sendto(packet, addr)

    def _peer_connected(self, peer_id: bytes, addr: tuple[str, int]) -> bool:
        peer = self._peers.get(peer_id)
        return bool(peer and peer.get("state") == "YES" and peer.get("addr") == addr)

    def _handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 4:
            return
        if data.startswith(DMRD):
            self._handle_dmrd(data, addr)
            return
        command = data[:4]
        if command == RPTL:
            self._handle_login(data, addr)
        elif command == RPTK:
            self._handle_key(data, addr)
        elif command == RPTC:
            self._handle_config_or_close(data, addr)
        elif command == RPTP:
            self._handle_ping(data, addr)
        elif command == RPTO:
            peer_id = data[4:8]
            self._send(RPTACK + peer_id, addr)
        else:
            logger.debug("%s ignored HBP command %r from %s", self.group.name, command, addr)

    def _handle_login(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 8:
            return
        peer_id = data[4:8]
        salt = random.getrandbits(32).to_bytes(4, "big")
        self._peers[peer_id] = {"state": "CHALLENGE_SENT", "addr": addr, "salt": salt, "last": time.monotonic()}
        logger.info("%s MMDVM peer login from ID %s at %s:%s", self.group.name, int.from_bytes(peer_id, "big"), addr[0], addr[1])
        self._send(RPTACK + salt, addr)

    def _handle_key(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 40:
            return
        peer_id = data[4:8]
        peer = self._peers.get(peer_id)
        if not peer or peer.get("state") != "CHALLENGE_SENT" or peer.get("addr") != addr:
            self._send(MSTNAK + peer_id, addr)
            return
        sent_hash = data[8:]
        calc_hash = bytes.fromhex(hashlib.sha256(peer["salt"] + self.hbp_password).hexdigest())
        if hmac.compare_digest(sent_hash, calc_hash):
            peer["state"] = "WAITING_CONFIG"
            peer["last"] = time.monotonic()
            self._send(RPTACK + peer_id, addr)
            logger.info("%s MMDVM peer authenticated ID %s", self.group.name, int.from_bytes(peer_id, "big"))
        else:
            self._send(MSTNAK + peer_id, addr)
            self._peers.pop(peer_id, None)
            logger.warning("%s MMDVM peer authentication failed ID %s", self.group.name, int.from_bytes(peer_id, "big"))

    def _handle_config_or_close(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.startswith(RPTCL):
            peer_id = data[5:9]
            self._peers.pop(peer_id, None)
            self._send(MSTNAK + peer_id, addr)
            logger.info("%s MMDVM peer closed ID %s", self.group.name, int.from_bytes(peer_id, "big"))
            return
        if len(data) < 8:
            return
        peer_id = data[4:8]
        peer = self._peers.get(peer_id)
        if not peer or peer.get("addr") != addr:
            self._send(MSTNAK + peer_id, addr)
            return
        peer["state"] = "YES"
        peer["last"] = time.monotonic()
        self._send(RPTACK + peer_id, addr)
        logger.info("%s MMDVM peer config accepted ID %s", self.group.name, int.from_bytes(peer_id, "big"))

    def _handle_ping(self, data: bytes, addr: tuple[str, int]) -> None:
        # HBLink peers normally send RPTPING + 4-byte peer ID (11 bytes).
        # Some MMDVM builds have been seen sending the shortened RPTP + ID
        # form. Accept both, and if a same-address peer is already connected,
        # answer that peer rather than timing out the bridge.
        if data.startswith(RPTPING) and len(data) >= 11:
            peer_id = data[7:11]
        elif len(data) >= 8:
            peer_id = data[4:8]
        else:
            return
        if not self._peer_connected(peer_id, addr):
            for candidate, peer in self._peers.items():
                if peer.get("state") == "YES" and peer.get("addr") == addr:
                    peer_id = candidate
                    break
        if self._peer_connected(peer_id, addr):
            self._peers[peer_id]["last"] = time.monotonic()
            self._send(MSTPONG + peer_id, addr)
            logger.debug("%s answered HBP ping from peer %s", self.group.name, int.from_bytes(peer_id, "big"))
        else:
            self._send(MSTNAK + peer_id, addr)
            logger.warning("%s rejected HBP ping from unregistered peer %s at %s:%s", self.group.name, int.from_bytes(peer_id, "big"), addr[0], addr[1])

    def _group_isolated_dmrd(self, dmrd: bytes) -> bytes:
        """Return a DMRD frame with a group-unique stream ID.

        Multiple enabled groups use separate Analog_Bridge/MMDVM_Bridge paths,
        but some upstream OpenBridge/HBLink stacks also track active calls by
        stream ID and source ID. Rewriting only the 4-byte stream ID keeps the
        AMBE voice payload intact while making simultaneous group calls distinct
        on the shared OpenBridge UDP connection.
        """
        if len(dmrd) != 53:
            return dmrd
        original_stream = int.from_bytes(dmrd[16:20], "big")
        isolated_stream = (original_stream ^ self._stream_xor) & 0xFFFFFFFF
        if isolated_stream == 0:
            isolated_stream = self._stream_xor
        if isolated_stream == original_stream:
            return dmrd
        fixed = bytearray(dmrd)
        fixed[16:20] = isolated_stream.to_bytes(4, "big")
        return bytes(fixed)

    def _handle_dmrd(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 53:
            logger.warning("%s dropped short DMRD frame of %s bytes", self.group.name, len(data))
            self.dropped_packets += 1
            return
        dmrd = data[:53]
        peer_id = dmrd[11:15]
        if not self._peer_connected(peer_id, addr):
            logger.warning("%s dropped DMRD from unregistered peer %s", self.group.name, int.from_bytes(peer_id, "big"))
            self.dropped_packets += 1
            return
        dst_tg = int.from_bytes(dmrd[8:11], "big")
        if self.enforce_group_talkgroup and dst_tg != self.group.talkgroup:
            logger.warning(
                "%s dropped DMRD for TG %s; expected TG %s",
                self.group.name,
                dst_tg,
                self.group.talkgroup,
            )
            self.dropped_packets += 1
            return
        isolated_dmrd = self._group_isolated_dmrd(dmrd)
        with self._send_lock:
            sent_bytes = self.openbridge.send_dmrd(isolated_dmrd, network_id=self.openbridge_network_id)
        self.forwarded_packets += 1
        self.openbridge_packets += 1
        self.openbridge_bytes += sent_bytes
        if self.forwarded_packets == 1:
            sid = int.from_bytes(isolated_dmrd[5:8], "big")
            stream_id = int.from_bytes(isolated_dmrd[16:20], "big")
            logger.info(
                "%s forwarding first DMRD stream packet to OpenBridge TG %s SID %s stream_id %s openbridge_src_id=global",
                self.group.name,
                self.group.talkgroup,
                sid,
                stream_id,
            )


class ManagedOpenBridgeSupervisor:
    """Manage helper bridge processes and embedded HBP-to-OpenBridge forwarding."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base_dir = cfg.path.parent
        self.running: list[RunningProcess] = []
        self.forwarders: list[HBPForwarder] = []
        self.started = False
        self.openbridge: OpenBridgeSender | None = None
        self._ports_assigned = False
        self._monitor_running = False
        self._monitor_thread: threading.Thread | None = None

    def start(self) -> None:
        if self.started:
            return
        self._ensure_openbridge()
        self._ensure_effective_ports()
        self._start_forwarders()
        self.render_files()
        if self.cfg.output.managed_openbridge.start_helpers:
            self._start_helper_processes()
            delay = self.cfg.output.managed_openbridge.startup_delay_seconds
            if delay > 0:
                logger.info("Waiting %.1f seconds for managed bridge helpers", delay)
                time.sleep(delay)
        else:
            logger.info("managed_openbridge.start_helpers is false; embedded HBP listener is running but helper binaries were not started")
        self.started = True
        self._start_process_monitor()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_process_monitor()
        for item in reversed(self.running):
            proc = item.process
            if proc.poll() is not None:
                continue
            logger.info("Stopping managed process %s for group %s", item.name, item.group)
            proc.terminate()
        deadline = time.monotonic() + timeout
        for item in reversed(self.running):
            proc = item.process
            if proc.poll() is not None:
                continue
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.warning("Killing managed process %s for group %s", item.name, item.group)
                proc.kill()
        self.running.clear()
        for forwarder in self.forwarders:
            logger.info(
                "Stopping embedded HBP listener for %s; forwarded=%s dropped=%s",
                forwarder.group.name,
                forwarder.forwarded_packets,
                forwarder.dropped_packets,
            )
            forwarder.stop()
        self.forwarders.clear()
        if self.openbridge is not None:
            self.openbridge.close()
            self.openbridge = None
        self.started = False

    def forwarder_for(self, group_name: str) -> HBPForwarder | None:
        for forwarder in self.forwarders:
            if forwarder.group.name == group_name:
                return forwarder
        return None

    def openbridge_counters(self) -> tuple[int, int]:
        if self.openbridge is None:
            return 0, 0
        return self.openbridge.counters()

    def _start_process_monitor(self) -> None:
        if self._monitor_running:
            return
        self._monitor_running = True
        self._monitor_thread = threading.Thread(target=self._monitor_processes, name="managed-openbridge-monitor", daemon=True)
        self._monitor_thread.start()

    def _stop_process_monitor(self) -> None:
        self._monitor_running = False
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=1.0)
            self._monitor_thread = None

    def _monitor_processes(self) -> None:
        reported: set[tuple[str, str]] = set()
        while self._monitor_running:
            for item in list(self.running):
                code = item.process.poll()
                key = (item.group, item.name)
                if code is not None and key not in reported:
                    reported.add(key)
                    logger.error(
                        "Managed helper %s for group %s exited with code %s. Log tail:\n%s",
                        item.name,
                        item.group,
                        code,
                        self._tail(item.log_path),
                    )
            time.sleep(1.0)

    def bridge_dir_for(self, group: GroupConfig) -> Path:
        return (self.cfg.app.state_file.parent / "bridges" / group.name).resolve()

    def _require_bridge(self, group: GroupConfig) -> GroupBridgeConfig:
        if group.bridge is None:
            raise RuntimeError(f"Group {group.name} does not have managed bridge settings")
        return group.bridge

    def _context(self, group: GroupConfig) -> dict[str, str]:
        bridge = self._require_bridge(group)
        usrp = bridge.usrp
        bridge_dir = self.bridge_dir_for(group)
        mob = self.cfg.output.managed_openbridge
        ob = self.cfg.output.direct_openbridge
        ctx: dict[str, Any] = {
            "config_dir": str(self.base_dir),
            "state_dir": str(self.cfg.app.state_file.parent),
            "audio_dir": str(self.cfg.app.audio_dir),
            "bridge_dir": str(bridge_dir),
            "analog_bridge_ini": str(bridge_dir / "Analog_Bridge.ini"),
            "dvswitch_ini": str(bridge_dir / "DVSwitch.ini"),
            "mmdvm_bridge_ini": str(bridge_dir / "MMDVM_Bridge.ini"),
            "group": group.name,
            "talkgroup": group.talkgroup,
            "county_codes": ",".join(group.county_codes),
            "usrp_address": usrp.address,
            "usrp_tx_port": usrp.tx_port,
            "usrp_local_rx_port": "" if usrp.local_rx_port is None else usrp.local_rx_port,
            "usrp_sample_rate": usrp.sample_rate,
            "usrp_frame_ms": usrp.frame_ms,
            "slot": usrp.slot,
            "color_code": usrp.color_code,
            "subscriber_id": usrp.subscriber_id,
            "repeater_id": usrp.repeater_id,
            "mmdvm_id": bridge.variables.get("mmdvm_id", usrp.repeater_id),
            "callsign": usrp.callsign,
            "hbp_bind_ip": mob.hbp_bind_ip,
            "openbridge_local_ip": ob.local_ip,
            "openbridge_local_port": ob.local_port,
            "openbridge_target_ip": ob.target_ip,
            "openbridge_target_port": ob.target_port,
            "openbridge_network_id": ob.network_id,
            "analog_bridge_path": mob.helper_paths.analog_bridge,
            "mmdvm_bridge_path": mob.helper_paths.mmdvm_bridge,
            "md380_emu_path": mob.helper_paths.md380_emu,
        }
        ctx.update(bridge.variables)
        return {key: str(value) for key, value in ctx.items()}

    def render(self, template: str, group: GroupConfig) -> str:
        return template.format(**self._context(group))

    def rendered_files_for(self, group: GroupConfig) -> RenderedBridgeFiles:
        bridge_dir = self.bridge_dir_for(group)
        return RenderedBridgeFiles(
            analog_bridge_ini=bridge_dir / "Analog_Bridge.ini",
            dvswitch_ini=bridge_dir / "DVSwitch.ini",
            mmdvm_bridge_ini=bridge_dir / "MMDVM_Bridge.ini",
        )

    def render_files(self) -> list[Path]:
        self._ensure_effective_ports()
        written: list[Path] = []
        for group in self.cfg.groups:
            if not group.enabled:
                continue
            files = self.rendered_files_for(group)
            payloads = {
                files.analog_bridge_ini: self.render(ANALOG_BRIDGE_TEMPLATE, group),
                files.dvswitch_ini: self.render(DVSWITCH_TEMPLATE, group),
                files.mmdvm_bridge_ini: self.render(MMDVM_BRIDGE_TEMPLATE, group),
            }
            for path, content in payloads.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                logger.info("Rendered managed_openbridge file for %s: %s", group.name, path)
                written.append(path)
        return written


    def _ensure_effective_ports(self) -> None:
        """Assign per-group internal ports, skipping ports already in use.

        The user-facing config exposes a range instead of per-helper ports. At
        runtime the app picks one free block per enabled group and rewrites the
        generated helper configs to match. This avoids failures from stale
        helper processes or unrelated local services holding a port such as
        43004.
        """
        if self._ports_assigned:
            return
        mob = self.cfg.output.managed_openbridge
        allocator = mob.port_allocator
        used: set[int] = set()
        ob = self.cfg.output.direct_openbridge
        if ob.local_port > 0:
            used.add(ob.local_port)
        new_groups: list[GroupConfig] = []
        slot_index = 0
        for group in self.cfg.groups:
            if not group.enabled or group.bridge is None:
                new_groups.append(group)
                continue
            chosen: dict[str, int] | None = None
            first_candidate = slot_index
            max_slot = first_candidate + allocator.scan_limit
            candidate_slot = first_candidate
            while candidate_slot < max_slot:
                candidate = self._candidate_ports(candidate_slot)
                candidate_ports = set(candidate.values())
                if not candidate_ports.intersection(used) and self._ports_available(candidate_ports, mob.hbp_bind_ip):
                    chosen = candidate
                    break
                candidate_slot += 1
            if chosen is None:
                raise RuntimeError(
                    f"No free internal UDP port block found for group {group.name}. "
                    f"Increase internal_ports.end or move internal_ports.start above busy local services."
                )
            if candidate_slot != first_candidate:
                logger.info(
                    "%s internal port block beginning at %s was busy; using block beginning at %s",
                    group.name,
                    self._candidate_ports(first_candidate)["md380emu_port"],
                    chosen["md380emu_port"],
                )
            used.update(chosen.values())
            preview_group = self._group_with_ports(group, chosen)
            logger.info(
                "%s isolated bridge: dmr_id=%s repeater_id=%s mmdvm_id=%s openbridge_src_id=global md380emu=%s usrp_rx=%s usrp_tx=%s ambe_rx=%s ambe_tx=%s mmdvm_local=%s hbp_master=%s",
                group.name,
                preview_group.bridge.usrp.subscriber_id if preview_group.bridge else "?",
                preview_group.bridge.usrp.repeater_id if preview_group.bridge else "?",
                preview_group.bridge.variables.get("mmdvm_id", str(preview_group.bridge.usrp.repeater_id)) if preview_group.bridge else "?",
                chosen["md380emu_port"],
                chosen["usrp_rx_port"],
                chosen["usrp_tx_port"],
                chosen["ab_ambe_rx_port"],
                chosen["ab_ambe_tx_port"],
                chosen["mmdvm_local_port"],
                chosen["hbp_master_port"],
            )
            new_groups.append(self._group_with_ports(group, chosen))
            slot_index = candidate_slot + 1
        self.cfg = replace(self.cfg, groups=tuple(new_groups))
        self._ports_assigned = True

    def _candidate_ports(self, slot_index: int) -> dict[str, int]:
        allocator = self.cfg.output.managed_openbridge.port_allocator
        return {
            "md380emu_port": allocator.md380emu_base + (slot_index * allocator.md380emu_step),
            "usrp_rx_port": allocator.usrp_rx_base + (slot_index * allocator.step),
            "usrp_tx_port": allocator.usrp_tx_base + (slot_index * allocator.step),
            "ab_ambe_rx_port": allocator.ambe_rx_base + (slot_index * allocator.step),
            "ab_ambe_tx_port": allocator.ambe_tx_base + (slot_index * allocator.step),
            "mmdvm_local_port": allocator.mmdvm_local_base + (slot_index * allocator.step),
            "hbp_master_port": allocator.hbp_master_base + (slot_index * allocator.step),
        }

    def _group_with_ports(self, group: GroupConfig, ports: dict[str, int]) -> GroupConfig:
        if group.bridge is None:
            return group
        variables = dict(group.bridge.variables)
        variables.update(
            {
                "md380emu_port": str(ports["md380emu_port"]),
                "ab_ambe_rx_port": str(ports["ab_ambe_rx_port"]),
                "ab_ambe_tx_port": str(ports["ab_ambe_tx_port"]),
                "mmdvm_local_port": str(ports["mmdvm_local_port"]),
                "hbp_master_port": str(ports["hbp_master_port"]),
            }
        )
        usrp = replace(
            group.bridge.usrp,
            tx_port=ports["usrp_rx_port"],
            local_rx_port=ports["usrp_tx_port"],
        )
        bridge = replace(group.bridge, usrp=usrp, variables=variables)
        return replace(group, bridge=bridge)

    @staticmethod
    def _ports_available(ports: set[int], bind_ip: str) -> bool:
        return all(ManagedOpenBridgeSupervisor._udp_port_available(bind_ip, port) for port in ports)

    @staticmethod
    def _udp_port_available(bind_ip: str, port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((bind_ip, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()


    def _ensure_openbridge(self) -> None:
        if self.openbridge is not None:
            return
        ob = self.cfg.output.direct_openbridge
        endpoint = OpenBridgeEndpoint(
            local_ip=ob.local_ip,
            local_port=ob.local_port,
            target_ip=ob.target_ip,
            target_port=ob.target_port,
            passphrase=ob.passphrase,
            network_id=ob.network_id,
        )
        self.openbridge = OpenBridgeSender(endpoint)

    def _start_forwarders(self) -> None:
        mob = self.cfg.output.managed_openbridge
        for group in self.cfg.groups:
            if not group.enabled:
                continue
            bridge = self._require_bridge(group)
            port_text = bridge.variables.get("hbp_master_port")
            if port_text is None:
                raise RuntimeError(f"Group {group.name} lacks hbp_master_port")
            if self.openbridge is None:
                raise RuntimeError("OpenBridge sender is not initialized")
            forwarder = HBPForwarder(
                group=group,
                bind_ip=mob.hbp_bind_ip,
                port=int(port_text),
                hbp_password=bridge.variables.get("hbp_password", mob.hbp_password),
                openbridge=self.openbridge,
                enforce_group_talkgroup=mob.enforce_group_talkgroup,
            )
            forwarder.start()
            self.forwarders.append(forwarder)

    def _start_helper_processes(self) -> None:
        for group in self.cfg.groups:
            if not group.enabled:
                continue
            self._start_group_helpers(group)

    def _start_group_helpers(self, group: GroupConfig) -> None:
        ctx = self._context(group)
        mob = self.cfg.output.managed_openbridge
        analog_bridge_path = require_helper_path("analog_bridge", mob.helper_paths.analog_bridge)
        mmdvm_bridge_path = require_helper_path("mmdvm_bridge", mob.helper_paths.mmdvm_bridge)
        md380_command, md380_cwd = self._md380_emu_command(ctx)
        specs = [
            ("md380-emu", md380_command, False, md380_cwd),
            (
                "mmdvm_bridge",
                f"DVSWITCH={shlex.quote(ctx['dvswitch_ini'])} {shlex.quote(mmdvm_bridge_path)} {shlex.quote(ctx['mmdvm_bridge_ini'])}",
                True,
                None,
            ),
            ("analog_bridge", [analog_bridge_path, ctx["analog_bridge_ini"]], False, None),
        ]
        for name, command, shell, cwd in specs:
            self._start_process(group, name, command, shell=shell, cwd=cwd)
            time.sleep(0.3)


    def _md380_emu_command(self, ctx: dict[str, str]) -> tuple[list[str], str | None]:
        mob = self.cfg.output.managed_openbridge
        md380_path = require_helper_path("md380_emu", mob.helper_paths.md380_emu)
        args = shlex.split(mob.helper_paths.md380_emu_args.format(**ctx))

        configured_workdir = mob.helper_paths.md380_emu_workdir.strip()
        if not configured_workdir or configured_workdir.lower() == "auto":
            workdir = str(Path(md380_path).resolve().parent) if "/" in md380_path else str(self.base_dir)
        else:
            workdir = configured_workdir

        configured_wrapper = mob.helper_paths.md380_emu_wrapper.strip()
        wrapper: str | None = None
        if configured_wrapper.lower() not in ("", "none", "false", "off"):
            if configured_wrapper.lower() == "auto":
                local_wrapper = Path(workdir) / "qemu-arm-static"
                if local_wrapper.exists() and local_wrapper.is_file():
                    wrapper = str(local_wrapper)
                else:
                    from .helpers import resolve_helper_path

                    wrapper = resolve_helper_path("md380_emu_wrapper", "auto")
            else:
                from .helpers import resolve_helper_path

                wrapper = resolve_helper_path("md380_emu_wrapper", configured_wrapper)
                if wrapper is None:
                    raise RuntimeError(f"Could not find configured md380_emu_wrapper: {configured_wrapper}")

        if wrapper:
            logger.info("Using md380-emu wrapper %s with cwd=%s", wrapper, workdir)
            return [wrapper, md380_path, *args], workdir
        logger.info("Starting md380-emu directly with cwd=%s", workdir)
        return [md380_path, *args], workdir

    def _start_process(self, group: GroupConfig, name: str, command: str | list[str], *, shell: bool, cwd: str | None) -> None:
        log_path = self.bridge_dir_for(group) / "logs" / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        pretty = command if isinstance(command, str) else " ".join(shlex.quote(part) for part in command)
        logger.info("Starting managed_openbridge process %s for %s; log=%s", name, group.name, log_path)
        try:
            proc = self._popen(command, cwd=str(self.base_dir) if cwd is None else cwd, log_path=log_path, shell=shell)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Managed process command not found for {group.name}/{name}: {pretty}") from exc
        time.sleep(0.2)
        if proc.poll() is not None:
            tail = self._tail(log_path)
            raise RuntimeError(
                f"Managed process {group.name}/{name} exited immediately with code {proc.returncode}. "
                f"Command: {pretty}. Log tail:\n{tail}"
            )
        self.running.append(RunningProcess(group=group.name, name=name, process=proc, log_path=log_path))

    @staticmethod
    def _popen(command: str | list[str], *, cwd: str | None, log_path: Path, shell: bool) -> subprocess.Popen[Any]:
        log_fh: BinaryIO = log_path.open("ab")
        try:
            return subprocess.Popen(
                command,
                cwd=cwd,
                shell=shell,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_fh.close()

    @staticmethod
    def _tail(path: Path, max_bytes: int = 4000) -> str:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return "<no log file>"
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace") or "<empty log file>"


def dmrd_for_test(peer_id: int, rf_src: int, dst_tg: int, *, seq: int = 1, slot: int = 1, stream_id: int = 1234) -> bytes:
    bits = 0x80 if slot == 2 else 0x00
    bits |= 0x21
    return b"".join(
        [
            DMRD,
            bytes([seq & 0xFF]),
            int_to_3(rf_src),
            int_to_3(dst_tg),
            peer_id.to_bytes(4, "big"),
            bytes([bits]),
            stream_id.to_bytes(4, "big"),
            b"\x00" * 33,
        ]
    )
