from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator

from .config import EncoderConfig


class EncoderError(RuntimeError):
    pass


def parse_hex_frames(text: str, frame_size: int) -> Iterator[bytes]:
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip().replace(" ", "")
        if not line:
            continue
        if len(line) % 2:
            raise EncoderError(f"Odd number of hex characters in encoder output line: {raw_line!r}")
        data = bytes.fromhex(line)
        if len(data) % frame_size:
            raise EncoderError(f"Hex line length {len(data)} is not a multiple of frame size {frame_size}")
        for index in range(0, len(data), frame_size):
            yield data[index : index + frame_size]


def chunk_raw(data: bytes, frame_size: int) -> Iterator[bytes]:
    if len(data) % frame_size:
        raise EncoderError(f"Raw encoder output length {len(data)} is not a multiple of frame size {frame_size}")
    for index in range(0, len(data), frame_size):
        yield data[index : index + frame_size]


class AMBE72Encoder:
    def encode(self, wav_path: Path) -> Iterable[bytes]:
        raise NotImplementedError


class ExternalAMBE72Encoder(AMBE72Encoder):
    def __init__(self, cfg: EncoderConfig):
        self.cfg = cfg
        if not cfg.command:
            raise EncoderError("encoder.command is required for external_ambe72")

    def _argv(self, wav_path: Path) -> tuple[list[str], str]:
        replacements = {"wav": str(wav_path)}
        cmd_text = self.cfg.command.format(**replacements)
        argv = shlex.split(cmd_text)
        if not argv:
            raise EncoderError("encoder.command expanded to an empty command")

        # Zip/scp/package installs sometimes strip executable bits from helper scripts.
        # If the first argv entry is a local Python file and is not executable, run it
        # through the current interpreter instead of relying on mode bits/shebang.
        first = Path(argv[0])
        if first.suffix == ".py" and first.exists() and not os.access(first, os.X_OK):
            argv = [sys.executable, *argv]
        return argv, cmd_text

    def encode(self, wav_path: Path) -> Iterable[bytes]:
        argv, cmd_text = self._argv(wav_path)
        try:
            result = subprocess.run(
                argv,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.cfg.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise EncoderError(f"Encoder command not found: {cmd_text}") from exc
        except PermissionError as exc:
            raise EncoderError(
                "Encoder command permission denied: "
                f"{cmd_text}. If this is a Python script, prefix the command with python3 "
                "or run chmod +x on the script."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise EncoderError(f"Encoder timed out after {self.cfg.timeout_seconds}s") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            raise EncoderError(f"Encoder command failed: {stderr}") from exc

        output_format = self.cfg.output_format.lower()
        if output_format == "hex_lines":
            return list(parse_hex_frames(result.stdout.decode("utf-8", errors="replace"), self.cfg.frame_size))
        if output_format == "raw":
            return list(chunk_raw(result.stdout, self.cfg.frame_size))
        raise EncoderError(f"Unsupported encoder.output_format: {self.cfg.output_format}")


class FileAMBE72Encoder(AMBE72Encoder):
    def __init__(self, cfg: EncoderConfig):
        self.cfg = cfg
        if not cfg.file:
            raise EncoderError("encoder.file is required for file_ambe72")

    def encode(self, wav_path: Path) -> Iterable[bytes]:  # noqa: ARG002 - file backend ignores source WAV
        path = Path(self.cfg.file)
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return list(chunk_raw(data, self.cfg.frame_size))
        if all(c in "0123456789abcdefABCDEF\r\n \t#" for c in text):
            return list(parse_hex_frames(text, self.cfg.frame_size))
        return list(chunk_raw(data, self.cfg.frame_size))


class StubAMBE72Encoder(AMBE72Encoder):
    """Generates silence-like frames for pipeline tests only.

    This does not create useful speech. It exists so OpenBridge packet timing and
    HMAC can be tested without a vocoder.
    """

    def __init__(self, frame_hex: str, count: int = 50):
        self.frame = bytes.fromhex(frame_hex)
        if len(self.frame) != 9:
            raise EncoderError("stub AMBE72 frame must be 9 bytes")
        self.count = count

    def encode(self, wav_path: Path) -> Iterable[bytes]:  # noqa: ARG002
        return [self.frame for _ in range(self.count)]


def build_encoder(cfg: EncoderConfig, silence_hex: str = "000000000000000000") -> AMBE72Encoder:
    backend = cfg.backend.lower()
    if backend == "external_ambe72":
        return ExternalAMBE72Encoder(cfg)
    if backend == "file_ambe72":
        return FileAMBE72Encoder(cfg)
    if backend == "stub_ambe72":
        return StubAMBE72Encoder(silence_hex)
    raise EncoderError(f"Unsupported encoder backend: {cfg.backend}")
