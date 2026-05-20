from __future__ import annotations

from pathlib import Path
import shutil


HELPER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "analog_bridge": (
        "Analog_Bridge",
        "/opt/Analog_Bridge/Analog_Bridge",
        "/usr/bin/Analog_Bridge",
        "/usr/local/bin/Analog_Bridge",
        "/opt/analog_bridge/Analog_Bridge",
    ),
    "mmdvm_bridge": (
        "MMDVM_Bridge",
        "/opt/MMDVM_Bridge/MMDVM_Bridge",
        "/usr/bin/MMDVM_Bridge",
        "/usr/local/bin/MMDVM_Bridge",
        "/opt/mmdvm_bridge/MMDVM_Bridge",
    ),
    "md380_emu": (
        "md380-emu",
        "/usr/bin/md380-emu",
        "/usr/local/bin/md380-emu",
        "/opt/md380-emu/md380-emu",
        "/opt/Analog_Bridge/md380-emu",
    ),
    "md380_emu_wrapper": (
        "/opt/md380-emu/qemu-arm-static",
        "qemu-arm-static",
        "/usr/bin/qemu-arm-static",
        "/usr/local/bin/qemu-arm-static",
    ),
}


class HelperNotFound(FileNotFoundError):
    pass


def _is_executable_path(value: str) -> bool:
    path = Path(value)
    return path.exists() and path.is_file()


def resolve_helper_path(kind: str, configured: str | None = None) -> str | None:
    """Resolve a DVSwitch helper binary.

    configured may be an explicit path, command name, "auto", or None.
    Returns an absolute path/command string if found, otherwise None.
    """
    value = (configured or "auto").strip()
    if value and value.lower() != "auto":
        if "/" in value:
            return value if _is_executable_path(value) else None
        found = shutil.which(value)
        return found

    for candidate in HELPER_CANDIDATES.get(kind, ()): 
        if "/" in candidate:
            if _is_executable_path(candidate):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def require_helper_path(kind: str, configured: str | None = None) -> str:
    resolved = resolve_helper_path(kind, configured)
    if resolved:
        return resolved
    label = kind.replace("_", "-")
    raise HelperNotFound(f"Could not find {label}. Install the DVSwitch helper package or set helpers.{kind} in config.yaml.")
