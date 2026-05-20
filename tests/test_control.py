import json

from skyalert_bridge.control import write_audio_request


def test_write_audio_request_creates_atomic_json(tmp_path):
    path = write_audio_request(tmp_path, group="Pitt County", text="Skywarn test announcement.")
    assert path.exists()
    assert path.suffix == ".json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["group"] == "Pitt County"
    assert data["text"] == "Skywarn test announcement."
    assert data["kind"] == "test_audio"
