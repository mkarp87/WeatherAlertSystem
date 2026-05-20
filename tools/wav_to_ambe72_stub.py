#!/usr/bin/env python3
"""Pipeline-test AMBE72 stub.

This script does not encode speech. It emits a repeated 9-byte frame for the
estimated duration of the input WAV so direct_openbridge timing/HMAC can be
exercised without a real vocoder.
"""
from __future__ import annotations

import argparse
import math
import sys
import wave
from pathlib import Path


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        return wav.getnframes() / float(rate) if rate else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input WAV path")
    parser.add_argument("--format", choices=("hex", "raw"), default="hex")
    parser.add_argument("--frame-hex", default="000000000000000000", help="9-byte frame encoded as hex")
    args = parser.parse_args()

    frame = bytes.fromhex(args.frame_hex)
    if len(frame) != 9:
        print("--frame-hex must decode to exactly 9 bytes", file=sys.stderr)
        return 2

    count = max(1, math.ceil(wav_duration(Path(args.input)) / 0.020))
    if args.format == "hex":
        for _ in range(count):
            print(frame.hex())
    else:
        stdout = getattr(sys.stdout, "buffer", None)
        if stdout is None:
            raise RuntimeError("binary stdout unavailable")
        for _ in range(count):
            stdout.write(frame)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
