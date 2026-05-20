from pathlib import Path
import json
import threading
import time

from skyalert_bridge.app import SkyAlertBridgeApp
from skyalert_bridge.config import load_config


class SlowCountingTransmitter:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.groups = []
        self.lock = threading.Lock()

    def transmit(self, group, wav_path: Path, text: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.groups.append(group.name)
        try:
            time.sleep(0.1)
        finally:
            with self.lock:
                self.active -= 1

    def close(self) -> None:
        pass


def write_request(control_dir: Path, name: str, group: str) -> None:
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / f"{name}.json").write_text(json.dumps({"group": group, "text": f"test for {group}"}), encoding="utf-8")


def test_queue_scheduler_runs_different_groups_in_parallel_and_keeps_same_group_serial(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
app:
  dry_run: false
  state_file: ./state/state.json
  audio_dir: ./state/audio
  control_dir: ./state/control
output:
  mode: analog_bridge_usrp
audio_scheduler:
  mode: parallel_by_group
  max_concurrent_groups: 2
groups:
  - name: A
    enabled: true
    county_codes: [NCC001]
    talkgroup: 101
  - name: B
    enabled: true
    county_codes: [NCC003]
    talkgroup: 102
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    app = SkyAlertBridgeApp(cfg)
    fake = SlowCountingTransmitter()
    app.transmitter = fake
    app._synthesize_text = lambda group, text, basename=None: tmp_path / f"{group.name}.wav"
    control_dir = cfg.app.control_dir
    write_request(control_dir, "001-A", "A")
    write_request(control_dir, "002-B", "B")
    write_request(control_dir, "003-A", "A")

    failures = app.process_queued_requests()

    assert failures == 0
    assert fake.max_active == 2
    assert sorted(fake.groups) == ["A", "B"]
    assert (control_dir / "003-A.json").exists()
    assert not (control_dir / "001-A.running").exists()
    assert not (control_dir / "002-B.running").exists()
