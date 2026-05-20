from __future__ import annotations

import audioop
import shutil
import subprocess
import wave
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from .config import AudioConfig


class AudioError(RuntimeError):
    pass


def wav_is_compatible(path: Path, cfg: AudioConfig) -> bool:
    try:
        with wave.open(str(path), "rb") as wav:
            return (
                wav.getframerate() == cfg.target_sample_rate
                and wav.getnchannels() == cfg.target_channels
                and wav.getsampwidth() == cfg.target_sample_width_bytes
                and wav.getcomptype() == "NONE"
            )
    except wave.Error:
        return False


def ensure_pcm_wav(path: Path, cfg: AudioConfig) -> Path:
    path = Path(path)
    if wav_is_compatible(path, cfg):
        return path
    converter = cfg.converter.lower()
    if converter in {"auto", "ffmpeg"} and shutil.which("ffmpeg"):
        return _convert_ffmpeg(path, cfg)
    if converter in {"auto", "sox"} and shutil.which("sox"):
        return _convert_sox(path, cfg)
    if converter == "none":
        raise AudioError(f"{path} is not {cfg.target_sample_rate} Hz mono PCM16 WAV")
    raise AudioError("No WAV converter found. Install ffmpeg or sox, or configure tts to emit 8 kHz mono PCM16 WAV.")


def _converted_path(path: Path) -> Path:
    return path.with_name(path.stem + ".8kmono.wav")


def _convert_ffmpeg(path: Path, cfg: AudioConfig) -> Path:
    out = _converted_path(path)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-ar", str(cfg.target_sample_rate),
        "-ac", str(cfg.target_channels),
        "-sample_fmt", "s16",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    return out


def _convert_sox(path: Path, cfg: AudioConfig) -> Path:
    out = _converted_path(path)
    cmd = [
        "sox", str(path), "-r", str(cfg.target_sample_rate), "-c", str(cfg.target_channels), "-b", "16", str(out)
    ]
    subprocess.run(cmd, check=True)
    return out


def read_pcm_chunks(path: Path, samples_per_chunk: int = 160) -> Iterable[bytes]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise AudioError("read_pcm_chunks expects mono PCM16 WAV")
        while True:
            data = wav.readframes(samples_per_chunk)
            if not data:
                break
            expected = samples_per_chunk * 2
            if len(data) < expected:
                data += b"\x00" * (expected - len(data))
            yield data


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate()
        return frames / float(rate) if rate else 0.0


def make_silence_wav(path: Path, duration_seconds: float, sample_rate: int = 8000) -> Path:
    frames = int(duration_seconds * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)
    return path


def normalize_pcm16_wav(path: Path, target_peak: int = 22000) -> Path:
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        data = wav.readframes(wav.getnframes())
    if params.sampwidth != 2:
        return path
    peak = audioop.max(data, 2)
    if peak <= 0:
        return path
    factor = min(4.0, target_peak / peak)
    normalized = audioop.mul(data, 2, factor)
    out = path.with_name(path.stem + ".norm.wav")
    with wave.open(str(out), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(normalized)
    return out
