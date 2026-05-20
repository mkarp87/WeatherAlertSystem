from pathlib import Path

from skyalert_bridge.config import load_config
from skyalert_bridge.managed_openbridge import ManagedOpenBridgeSupervisor
from skyalert_bridge.process_manager import ProcessSupervisor


def test_example_config_has_group_specific_usrp_ports_when_managed_openbridge_enabled(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg_path = tmp_path / "config.yaml"
    text = source.read_text(encoding="utf-8").replace("mode: dry_run", "mode: managed_openbridge", 1)
    cfg_path.write_text(text, encoding="utf-8")
    cfg = load_config(cfg_path)
    ports = [group.bridge.usrp.tx_port for group in cfg.groups]
    assert ports == [43001, 43011, 43021]
    assert cfg.groups[0].bridge.variables["md380emu_port"] == "43000"
    assert cfg.groups[1].bridge.variables["hbp_master_port"] == "43016"


def test_managed_example_loads_templates():
    cfg = load_config(Path(__file__).resolve().parents[1] / "examples" / "managed_dvswitch.yaml")
    assert cfg.output.mode == "managed_dvswitch"
    assert len(cfg.groups[0].bridge.files) == 5
    assert len(cfg.groups[0].bridge.processes) == 4
    assert any(item.path == "{rules_py}" for item in cfg.groups[0].bridge.files)
    assert "bridge.py" in cfg.groups[0].bridge.processes[1].command
    assert cfg.groups[2].bridge.variables["openbridge_network_id"] == "3129201"


def test_managed_openbridge_example_renders_default_files(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples" / "managed_openbridge.yaml"
    cfg_path = tmp_path / "config.yaml"
    text = source.read_text(encoding="utf-8")
    text = text.replace("state_file: ./state/skyalert_state.json", "state_file: ./state/skyalert_state.json")
    text = text.replace('local_ip: "0.0.0.0"', 'local_ip: "127.0.0.1"')
    text = text.replace("local_port: 54097", "local_port: 0", 1)
    cfg_path.write_text(text, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.output.mode == "managed_openbridge"
    supervisor = ManagedOpenBridgeSupervisor(cfg)
    try:
        written = supervisor.render_files()
    finally:
        supervisor.stop(timeout=0.1)
    assert len(written) == 3
    assert (tmp_path / "state" / "bridges" / "ARC125" / "Analog_Bridge.ini").exists()
    mmdvm_ini = (tmp_path / "state" / "bridges" / "ARC125" / "MMDVM_Bridge.ini").read_text(encoding="utf-8")
    assert "Port=43006" in mmdvm_ini
    assert "Id=31000182" in mmdvm_ini


def test_process_supervisor_renders_group_file(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
app:
  dry_run: true
  state_file: ./state/state.json
  audio_dir: ./state/audio
nws: {}
announcements: {}
output:
  mode: managed_dvswitch
  analog_bridge_usrp:
    address: 127.0.0.1
    tx_port: 34001
    local_rx_port: 32001
    subscriber_id: 1234567
    repeater_id: 123456789
    callsign: N0CALL
  direct_openbridge: {}
encoder: {}
tts: {}
audio: {}
groups:
  - name: ARC125
    county_codes: [ARC125]
    talkgroup: 125
    bridge:
      variables:
        md380emu_port: 2470
      files:
        - path: ./rendered/{group}.ini
          content: |
            TG={talkgroup}
            EMU={md380emu_port}
            PORT={usrp_tx_port}
      processes: []
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    supervisor = ProcessSupervisor(cfg)
    supervisor.start()
    try:
        rendered = tmp_path / "rendered" / "ARC125.ini"
        assert rendered.read_text(encoding="utf-8") == "TG=125\nEMU=2470\nPORT=34001\n"
    finally:
        supervisor.stop()


def test_managed_openbridge_skips_busy_internal_port_block(tmp_path):
    import socket

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
app:
  log_level: WARNING
  state_file: ./state/state.json
  audio_dir: ./state/audio
openbridge:
  target_ip: 127.0.0.1
  target_port: 9
  passphrase: loopback-secret
  network_id: 31000182
  local_ip: 127.0.0.1
  local_port: 0
station:
  callsign: NC4ES
  source_id: 1234567
  repeater_id: 31000182
  slot: 1
  color_code: 1
internal_ports:
  start: 43000
  end: 43050
  step: 10
groups:
  - name: Pitt
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
""",
        encoding="utf-8",
    )
    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    busy.bind(("127.0.0.1", 43004))
    try:
        cfg = load_config(cfg_path)
        supervisor = ManagedOpenBridgeSupervisor(cfg)
        try:
            supervisor.render_files()
            group = supervisor.cfg.groups[0]
            assert group.bridge.variables["md380emu_port"] == "43010"
            assert group.bridge.variables["ab_ambe_tx_port"] == "43014"
            assert group.bridge.variables["hbp_master_port"] == "43016"
            assert group.bridge.usrp.tx_port == 43011
            rendered = tmp_path / "state" / "bridges" / "Pitt" / "DVSwitch.ini"
            assert "rxPort = 43014" in rendered.read_text(encoding="utf-8")
        finally:
            supervisor.stop(timeout=0.1)
    finally:
        busy.close()


def test_managed_openbridge_uses_local_qemu_wrapper_for_md380(tmp_path):
    md_dir = tmp_path / "md380-emu"
    md_dir.mkdir()
    emu = md_dir / "md380-emu"
    qemu = md_dir / "qemu-arm-static"
    emu.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    qemu.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    emu.chmod(0o755)
    qemu.chmod(0o755)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""
app:
  log_level: WARNING
  state_file: ./state/state.json
  audio_dir: ./state/audio
openbridge:
  target_ip: 127.0.0.1
  target_port: 9
  passphrase: loopback-secret
  network_id: 31000182
  local_ip: 127.0.0.1
  local_port: 0
station:
  callsign: NC4ES
  source_id: 1234567
  repeater_id: 31000182
  slot: 1
  color_code: 1
helpers:
  analog_bridge: /bin/true
  mmdvm_bridge: /bin/true
  md380_emu: {emu}
  md380_emu_wrapper: auto
  md380_emu_workdir: auto
internal_ports:
  start: 43000
  end: 43050
  step: 10
groups:
  - name: Pitt
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    supervisor = ManagedOpenBridgeSupervisor(cfg)
    command, cwd = supervisor._md380_emu_command({"md380emu_port": "43100"})
    assert command == [str(qemu), str(emu), "-S", "43100"]
    assert cwd == str(md_dir.resolve())


def test_managed_openbridge_cleanup_stale_helpers_kills_process_with_state_bridge_path(tmp_path):
    import subprocess
    import sys
    import time

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
app:
  log_level: WARNING
  state_file: ./state/state.json
  audio_dir: ./state/audio
openbridge:
  target_ip: 127.0.0.1
  target_port: 9
  passphrase: loopback-secret
  network_id: 31000182
  local_ip: 127.0.0.1
  local_port: 0
station:
  callsign: NC4ES
  source_id: 1234567
  repeater_id: 31000182
  slot: 1
  color_code: 1
internal_ports:
  start: 43000
  end: 43050
  step: 10
groups:
  - name: Pitt
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    stale_arg = str(tmp_path / "state" / "bridges" / "Pitt" / "MMDVM_Bridge.ini")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", stale_arg])
    try:
        time.sleep(0.2)
        supervisor = ManagedOpenBridgeSupervisor(cfg)
        assert supervisor.cleanup_stale_helpers() >= 1
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_systemd_unit_kills_control_group():
    service = (Path(__file__).resolve().parents[1] / "systemd" / "weather-alert-system.service").read_text(encoding="utf-8")
    assert "KillMode=control-group" in service
    assert "TimeoutStopSec=20" in service
