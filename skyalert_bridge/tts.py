from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
import requests

from .config import TTSConfig


class TTSError(RuntimeError):
    pass


class TTS:
    def __init__(self, cfg: TTSConfig, audio_dir: Path):
        self.cfg = cfg
        self.audio_dir = audio_dir
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str, basename: str | None = None) -> Path:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        safe_base = basename or f"announcement-{digest}"
        safe_base = "".join(c if c.isalnum() or c in "-_." else "_" for c in safe_base)
        out = self.audio_dir / f"{safe_base}-{digest}.wav"
        if out.exists() and out.stat().st_size > 44:
            return out
        backend = self.cfg.backend.lower()
        if backend == "espeak":
            return self._synthesize_espeak(text, out)
        if backend == "command":
            return self._synthesize_command(text, out)
        if backend == "voice_rss":
            return self._synthesize_voice_rss(text, out)
        raise TTSError(f"Unsupported TTS backend: {self.cfg.backend}")

    def _synthesize_espeak(self, text: str, out: Path) -> Path:
        cmd = [
            self.cfg.espeak_command,
            "-w", str(out),
            "-v", self.cfg.voice,
            "-s", str(self.cfg.speed_wpm),
            "-a", str(self.cfg.amplitude),
            text,
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            raise TTSError(f"TTS command not found: {self.cfg.espeak_command}. Install espeak-ng or set tts.backend=command.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            raise TTSError(f"espeak failed: {stderr}") from exc
        return out

    def _synthesize_command(self, text: str, out: Path) -> Path:
        if not self.cfg.command:
            raise TTSError("tts.backend=command requires tts.command")
        with NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(text)
            text_file = Path(handle.name)
        replacements = {
            "text_file": str(text_file),
            "wav": str(out),
            "voice": self.cfg.voice,
            "speed_wpm": str(self.cfg.speed_wpm),
        }
        cmd_text = self.cfg.command.format(**replacements)
        try:
            subprocess.run(shlex.split(cmd_text), check=True)
        finally:
            try:
                text_file.unlink()
            except OSError:
                pass
        if not out.exists():
            raise TTSError(f"TTS command did not create {out}")
        return out

    def _synthesize_voice_rss(self, text: str, out: Path) -> Path:
        if not self.cfg.voice_rss_api_key:
            raise TTSError("tts.voice_rss.api_key or SkyDescribe.APIKey is required for voice_rss backend")
        text = self._limit_words(text, self.cfg.voice_rss_max_words)
        params = {
            "key": self.cfg.voice_rss_api_key,
            "hl": self.cfg.voice_rss_language,
            "f": self.cfg.voice_rss_format,
            "c": self.cfg.voice_rss_codec,
            "r": str(self.cfg.voice_rss_speed),
            "v": self.cfg.voice_rss_voice,
            "src": text,
        }
        # POST avoids oversized URLs when full NWS alert text is spoken.
        response = requests.post("https://api.voicerss.org/", data=params, timeout=45)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if b"ERROR" in response.content[:20].upper() or "text" in content_type.lower():
            raise TTSError(response.text[:300])
        out.write_bytes(response.content)
        return out

    @staticmethod
    def _limit_words(text: str, max_words: int | None) -> str:
        if max_words is None or max_words <= 0:
            return text
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words])
