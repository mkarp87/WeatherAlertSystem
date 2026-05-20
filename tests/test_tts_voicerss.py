from pathlib import Path

from skyalert_bridge.config import load_config, TTSConfig
from skyalert_bridge.tts import TTS


def test_skydescribe_config_maps_to_voice_rss(tmp_path):
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
SkyDescribe:
  APIKey: test-key
  Language: en-us
  Speed: 0
  Voice: John
  MaxWords: 300
groups:
  - name: Pitt County
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.tts.backend == "voice_rss"
    assert cfg.tts.voice_rss_api_key == "test-key"
    assert cfg.tts.voice_rss_language == "en-us"
    assert cfg.tts.voice_rss_voice == "John"
    assert cfg.tts.voice_rss_speed == 0
    assert cfg.tts.voice_rss_max_words == 300


def test_voice_rss_posts_limited_words(monkeypatch, tmp_path):
    calls = []

    class Response:
        headers = {"Content-Type": "audio/wav"}
        content = b"RIFF" + (b"\x00" * 80)
        text = ""

        def raise_for_status(self):
            return None

    def fake_post(url, data, timeout):
        calls.append((url, data, timeout))
        return Response()

    monkeypatch.setattr("skyalert_bridge.tts.requests.post", fake_post)
    cfg = TTSConfig(
        backend="voice_rss",
        espeak_command="espeak-ng",
        voice="en-us",
        speed_wpm=145,
        amplitude=120,
        command=None,
        voice_rss_api_key="secret",
        voice_rss_voice="John",
        voice_rss_language="en-us",
        voice_rss_speed=0,
        voice_rss_max_words=3,
        voice_rss_codec="WAV",
        voice_rss_format="8khz_16bit_mono",
    )
    out = TTS(cfg, tmp_path).synthesize("one two three four five", basename="test")
    assert out.exists()
    assert calls
    assert calls[0][1]["src"] == "one two three"
    assert calls[0][1]["r"] == "0"
    assert calls[0][1]["c"] == "WAV"
    assert calls[0][1]["f"] == "8khz_16bit_mono"
