from __future__ import annotations

import json
from pathlib import Path
import re
import time
import uuid
from typing import Any

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(value: str) -> str:
    text = _SAFE_RE.sub("_", value.strip())
    return text.strip("._-") or "request"


def write_audio_request(control_dir: Path, *, group: str, text: str, kind: str = "test_audio") -> Path:
    control_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{safe_name(group)}-{uuid.uuid4().hex}.json"
    final = control_dir / name
    tmp = control_dir / f".{name}.tmp"
    payload: dict[str, Any] = {
        "kind": kind,
        "group": group,
        "text": text,
        "created_unix": time.time(),
    }
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(final)
    return final
