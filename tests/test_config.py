from pathlib import Path

import pytest

from skyalert_bridge.config import ConfigError, load_config


def test_example_config_loads():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    assert len(cfg.groups) == 3
    assert cfg.groups[0].county_codes == ("ARC125",)
    assert cfg.groups[2].county_codes == ("ARC200", "ARC201")
    assert cfg.groups[2].talkgroup == 201


def test_simplified_config_shape_loads(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
openbridge:
  target_ip: 10.255.0.254
  target_port: 54097
  passphrase: EMERGENCY
  network_id: 31000182
station:
  callsign: NC4ES
  source_id: 1234567
  repeater_id: 31000182
internal_ports:
  start: 45000
  step: 10
nws:
  included_events: []
  excluded_events: [Test Message]
groups:
  - name: ARC125
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.output.mode == "managed_openbridge"
    assert cfg.nws.exclude_events == ("Test Message",)
    assert cfg.output.direct_openbridge.target_ip == "10.255.0.254"
    assert cfg.groups[0].bridge.usrp.tx_port == 45001
    assert cfg.groups[0].bridge.variables["hbp_master_port"] == "45006"
    assert cfg.groups[0].bridge.usrp.callsign == "NC4ES"


def test_managed_openbridge_auto_offsets_group_identities(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
openbridge:
  target_ip: 10.255.0.254
  target_port: 54097
  passphrase: EMERGENCY
  network_id: 31000182
station:
  callsign: NC4ES
  source_id: 310001
  repeater_id: 31000182
internal_ports:
  start: 43000
  step: 10
groups:
  - name: Pitt County
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
  - name: ARC119
    enabled: true
    county_codes: [ARC119]
    talkgroup: 28514
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert [g.bridge.usrp.tx_port for g in cfg.groups] == [43001, 43011]
    assert [g.bridge.usrp.subscriber_id for g in cfg.groups] == [310001, 310002]
    assert [g.bridge.usrp.repeater_id for g in cfg.groups] == [31000182, 31000183]
    assert [g.bridge.variables["mmdvm_id"] for g in cfg.groups] == ["31000182", "31000183"]
    assert all("openbridge_network_id" not in g.bridge.variables for g in cfg.groups)


def test_group_identity_overrides(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
openbridge:
  target_ip: 10.255.0.254
  target_port: 54097
  passphrase: EMERGENCY
  network_id: 31000182
station:
  callsign: NC4ES
  source_id: 310001
  repeater_id: 31000182
internal_ports:
  start: 43000
  step: 10
groups:
  - name: Pitt County
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
    dmr_id: 310101
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    group = cfg.groups[0]
    assert group.dmr_id == 310101
    assert group.bridge.usrp.subscriber_id == 310101
    assert group.bridge.usrp.repeater_id == 31000182
    assert group.bridge.variables["mmdvm_id"] == "31000182"
    assert "openbridge_network_id" not in group.bridge.variables


def test_tail_message_config_loads(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
openbridge:
  target_ip: 10.255.0.254
  passphrase: EMERGENCY
  network_id: 31000182
station:
  callsign: NC4ES
  source_id: 310001
  repeater_id: 31000182
tail_message:
  enabled: true
  text: This is NC4ES weather alert.
groups:
  - name: ARC125
    county_codes: [ARC125]
    talkgroup: 28515
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.announcements.tail_message_enabled is True
    assert cfg.announcements.tail_message == "This is NC4ES weather alert."


def test_alert_repeat_defaults_disabled(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
openbridge:
  target_ip: 10.255.0.254
  passphrase: EMERGENCY
  network_id: 31000182
station:
  callsign: NC4ES
  source_id: 310001
groups:
  - name: Greenville
    county_codes: [NCC147]
    talkgroup: 28515
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.alert_repeat.enabled is False
    assert cfg.alert_repeat.unchanged_policy == "ignore"
    assert cfg.alert_repeat.repeat_after_minutes == 0


def test_managed_openbridge_rejects_matching_source_and_repeater_ids(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
openbridge:
  target_ip: 10.255.0.254
  target_port: 54097
  passphrase: EMERGENCY
  network_id: 31000182
station:
  callsign: NC4ES
  source_id: 310001
  repeater_id: 310001
groups:
  - name: BadIdentity
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Analog_Bridge requires them to be different"):
        load_config(cfg_path)
