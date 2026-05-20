from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import shlex
import signal
import subprocess
import time
from typing import Any, BinaryIO

from .config import Config, GroupBridgeConfig, GroupConfig, ManagedProcessConfig

logger = logging.getLogger(__name__)


@dataclass
class RunningProcess:
    group: str
    name: str
    process: subprocess.Popen[Any]
    log_path: Path | None = None


class PlaceholderError(RuntimeError):
    pass


class ProcessSupervisor:
    """Render optional per-group bridge files and supervise child bridge processes.

    The Python service remains the single long-running entry point, but bridge
    binaries such as md380-emu, Analog_Bridge, MMDVM_Bridge, or HBLink can still
    run as isolated child processes with per-group ports.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base_dir = cfg.path.parent
        self.running: list[RunningProcess] = []
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        for group in self.cfg.groups:
            if not group.enabled or group.bridge is None:
                continue
            if self.cfg.output.mode.lower() == "managed_dvswitch" and not group.bridge.processes:
                logger.warning("Group %s has no managed bridge processes configured", group.name)
            if self.cfg.output.mode.lower() == "managed_dvswitch" and not group.bridge.files:
                logger.warning("Group %s has no managed bridge files configured", group.name)
            self._write_files(group, group.bridge)
            self._start_processes(group, group.bridge)
            if group.bridge.processes and group.bridge.startup_delay_seconds > 0:
                time.sleep(group.bridge.startup_delay_seconds)
        self.started = True

    def stop(self, timeout: float = 5.0) -> None:
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
        self.started = False

    def bridge_dir_for(self, group: GroupConfig) -> Path:
        return (self.cfg.app.state_file.parent / "bridges" / group.name).resolve()

    def _context(self, group: GroupConfig, bridge: GroupBridgeConfig) -> dict[str, str]:
        usrp = bridge.usrp
        bridge_dir = self.bridge_dir_for(group)
        ctx: dict[str, Any] = {
            "config_dir": str(self.base_dir),
            "state_dir": str(self.cfg.app.state_file.parent),
            "audio_dir": str(self.cfg.app.audio_dir),
            "bridge_dir": str(bridge_dir),
            "analog_bridge_ini": str(bridge_dir / "Analog_Bridge.ini"),
            "dvswitch_ini": str(bridge_dir / "DVSwitch.ini"),
            "mmdvm_bridge_ini": str(bridge_dir / "MMDVM_Bridge.ini"),
            "hblink_cfg": str(bridge_dir / "hblink.cfg"),
            "rules_py": str(bridge_dir / "rules.py"),
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
            "callsign": usrp.callsign,
        }
        ctx.update(bridge.variables)
        return {key: str(value) for key, value in ctx.items()}

    def render(self, template: str, group: GroupConfig, bridge: GroupBridgeConfig) -> str:
        try:
            return template.format(**self._context(group, bridge))
        except KeyError as exc:
            missing = exc.args[0]
            raise PlaceholderError(f"Unknown managed bridge placeholder {{{missing}}} in group {group.name}") from exc

    def _resolve_path(self, text: str, group: GroupConfig, bridge: GroupBridgeConfig) -> Path:
        rendered = self.render(text, group, bridge)
        path = Path(rendered)
        if path.is_absolute():
            return path
        return (self.base_dir / path).resolve()

    def render_files(self) -> list[Path]:
        written: list[Path] = []
        for group in self.cfg.groups:
            if not group.enabled or group.bridge is None:
                continue
            written.extend(self._write_files(group, group.bridge))
        return written

    def _write_files(self, group: GroupConfig, bridge: GroupBridgeConfig) -> list[Path]:
        written: list[Path] = []
        for spec in bridge.files:
            path = self._resolve_path(spec.path, group, bridge)
            content = self.render(spec.content, group, bridge)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if spec.mode:
                path.chmod(int(spec.mode, 8))
            logger.info("Rendered managed bridge file for %s: %s", group.name, path)
            written.append(path)
        return written

    def _start_processes(self, group: GroupConfig, bridge: GroupBridgeConfig) -> None:
        for spec in bridge.processes:
            if not spec.enabled:
                continue
            argv_or_cmd = self.render(spec.command, group, bridge)
            cwd = str(self.base_dir) if spec.cwd is None else str(self._resolve_path(spec.cwd, group, bridge))
            log_path = self.bridge_dir_for(group) / "logs" / f"{spec.name}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Starting managed process %s for %s; log=%s", spec.name, group.name, log_path)
            try:
                proc = self._popen(spec, argv_or_cmd, cwd, log_path)
            except FileNotFoundError as exc:
                raise RuntimeError(f"Managed process command not found for {group.name}/{spec.name}: {argv_or_cmd}") from exc
            time.sleep(0.2)
            if proc.poll() is not None:
                tail = self._tail(log_path)
                raise RuntimeError(
                    f"Managed process {group.name}/{spec.name} exited immediately with code {proc.returncode}. "
                    f"Command: {argv_or_cmd}. Log tail:\n{tail}"
                )
            self.running.append(RunningProcess(group=group.name, name=spec.name, process=proc, log_path=log_path))

    def _popen(self, spec: ManagedProcessConfig, command: str, cwd: str | None, log_path: Path) -> subprocess.Popen[Any]:
        log_fh: BinaryIO = log_path.open("ab")
        try:
            if spec.shell:
                return subprocess.Popen(
                    command,
                    cwd=cwd,
                    shell=True,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            return subprocess.Popen(
                shlex.split(command),
                cwd=cwd,
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


def signal_name_to_number(name: str) -> int:
    normalized = name.upper().removeprefix("SIG")
    return getattr(signal, "SIG" + normalized)
