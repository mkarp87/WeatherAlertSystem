from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
import os

import yaml


class ConfigError(ValueError):
    pass


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [value]


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "yes", "true", "on"}
    return bool(value)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_present(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value is not None:
            return value
    return default


def _overlay(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in extra.items():
        if value is not None and key not in result:
            result[key] = value
    return result


@dataclass(frozen=True)
class ManagedFileConfig:
    path: str
    content: str
    mode: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], prefix: str) -> "ManagedFileConfig":
        path = str(raw.get("path") or "").strip()
        if not path:
            raise ConfigError(f"{prefix}.path is required")
        content = raw.get("content")
        if content is None:
            raise ConfigError(f"{prefix}.content is required")
        mode = None if raw.get("mode") is None else str(raw.get("mode"))
        return cls(path=path, content=str(content), mode=mode)


@dataclass(frozen=True)
class ManagedProcessConfig:
    name: str
    command: str
    enabled: bool = True
    cwd: str | None = None
    shell: bool = False

    @classmethod
    def from_raw(cls, raw: Any, prefix: str, index: int) -> "ManagedProcessConfig":
        if isinstance(raw, str):
            return cls(name=f"process-{index + 1}", command=raw)
        if not isinstance(raw, dict):
            raise ConfigError(f"{prefix}[{index}] must be a string or mapping")
        name = str(raw.get("name") or f"process-{index + 1}").strip()
        command = str(raw.get("command") or "").strip()
        if not command:
            raise ConfigError(f"{prefix}[{index}].command is required")
        return cls(
            name=name,
            command=command,
            enabled=_bool(raw.get("enabled", True)),
            cwd=None if raw.get("cwd") is None else str(raw.get("cwd")),
            shell=_bool(raw.get("shell", False)),
        )


@dataclass(frozen=True)
class AnalogBridgeUSRPConfig:
    address: str
    tx_port: int
    local_rx_port: int | None
    register: bool
    sample_rate: int
    frame_ms: int
    slot: int
    color_code: int
    subscriber_id: int
    repeater_id: int
    callsign: str
    pre_tx_commands: tuple[str, ...]


@dataclass(frozen=True)
class GroupBridgeConfig:
    usrp: AnalogBridgeUSRPConfig
    files: tuple[ManagedFileConfig, ...] = field(default_factory=tuple)
    processes: tuple[ManagedProcessConfig, ...] = field(default_factory=tuple)
    variables: dict[str, str] = field(default_factory=dict)
    startup_delay_seconds: float = 0.5


@dataclass(frozen=True)
class GroupConfig:
    name: str
    county_codes: tuple[str, ...]
    talkgroup: int
    enabled: bool = True
    include_events: tuple[str, ...] = field(default_factory=tuple)
    exclude_events: tuple[str, ...] = field(default_factory=tuple)
    dmr_id: int | None = None
    source_id: int | None = None
    repeater_id: int | None = None
    mmdvm_id: int | None = None
    openbridge_src_id: int | None = None  # deprecated: managed_openbridge uses global openbridge.network_id
    bridge: GroupBridgeConfig | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], default_usrp: AnalogBridgeUSRPConfig | None = None) -> "GroupConfig":
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ConfigError("Each group needs a non-empty name")
        codes = tuple(str(x).strip().upper() for x in _as_list(raw.get("county_codes")) if str(x).strip())
        if not codes:
            raise ConfigError(f"Group {name} needs at least one county code")
        tg = _int(raw.get("talkgroup"), f"groups.{name}.talkgroup")
        if tg <= 0 or tg > 16_777_215:
            raise ConfigError(f"Group {name} talkgroup must fit in a 24-bit DMR ID")

        def optional_id(key: str, max_value: int, label: str) -> int | None:
            if raw.get(key) is None:
                return None
            value = _int(raw.get(key), f"groups.{name}.{key}")
            if value <= 0 or value > max_value:
                raise ConfigError(f"Group {name} {label} must be 1 through {max_value}")
            return value

        dmr_id = optional_id("dmr_id", 0xFFFFFF, "dmr_id")
        source_id = optional_id("source_id", 0xFFFFFF, "source_id")
        repeater_id = optional_id("repeater_id", 0xFFFFFFFF, "repeater_id")
        mmdvm_id = optional_id("mmdvm_id", 0xFFFFFFFF, "mmdvm_id")
        openbridge_src_id = optional_id("openbridge_src_id", 0xFFFFFFFF, "openbridge_src_id")

        bridge_raw = raw.get("bridge")
        bridge = None
        if bridge_raw is not None:
            if not isinstance(bridge_raw, dict):
                raise ConfigError(f"groups.{name}.bridge must be a mapping")
            bridge = _parse_group_bridge(bridge_raw, default_usrp, f"groups.{name}.bridge")

        return cls(
            name=name,
            county_codes=codes,
            talkgroup=tg,
            enabled=_bool(raw.get("enabled", True)),
            include_events=tuple(str(x).strip() for x in _as_list(raw.get("include_events")) if str(x).strip()),
            exclude_events=tuple(str(x).strip() for x in _as_list(raw.get("exclude_events")) if str(x).strip()),
            dmr_id=dmr_id,
            source_id=source_id,
            repeater_id=repeater_id,
            mmdvm_id=mmdvm_id,
            openbridge_src_id=openbridge_src_id,
            bridge=bridge,
        )


@dataclass(frozen=True)
class AppConfig:
    poll_interval_seconds: int
    state_file: Path
    audio_dir: Path
    control_dir: Path
    log_level: str
    user_agent: str
    dry_run: bool


@dataclass(frozen=True)
class NWSConfig:
    api_base: str
    request_timeout_seconds: int
    max_alerts_per_group: int
    include_events: tuple[str, ...]
    exclude_events: tuple[str, ...]
    severity_rank: dict[str, int]


@dataclass(frozen=True)
class AlertRepeatConfig:
    enabled: bool
    repeat_after_minutes: int
    unchanged_policy: str


@dataclass(frozen=True)
class AnnouncementConfig:
    say_new_alerts: bool
    say_changed_alerts: bool
    say_all_clear: bool
    include_description: bool
    include_instruction: bool
    max_text_chars: int
    intro: str
    all_clear_text: str
    tail_message_enabled: bool
    tail_message: str
    text_cleanup_enabled: bool


@dataclass(frozen=True)
class DirectOpenBridgeConfig:
    local_ip: str
    local_port: int
    target_ip: str
    target_port: int
    passphrase: str
    network_id: int
    source_id: int
    peer_id: int
    slot: int
    color_code: int
    frame_interval_ms: int
    silence_ambe72_hex: str


@dataclass(frozen=True)
class ManagedOpenBridgeHelperPathsConfig:
    analog_bridge: str
    mmdvm_bridge: str
    md380_emu: str
    md380_emu_args: str
    md380_emu_wrapper: str
    md380_emu_workdir: str


@dataclass(frozen=True)
class ManagedOpenBridgePortAllocatorConfig:
    usrp_rx_base: int
    usrp_tx_base: int
    ambe_rx_base: int
    ambe_tx_base: int
    mmdvm_local_base: int
    hbp_master_base: int
    md380emu_base: int
    step: int
    md380emu_step: int
    scan_limit: int


@dataclass(frozen=True)
class ManagedOpenBridgeConfig:
    enabled: bool
    start_helpers: bool
    cleanup_stale_helpers: bool
    hbp_bind_ip: str
    hbp_password: str
    startup_delay_seconds: float
    enforce_group_talkgroup: bool
    helper_paths: ManagedOpenBridgeHelperPathsConfig
    port_allocator: ManagedOpenBridgePortAllocatorConfig


@dataclass(frozen=True)
class OutputConfig:
    mode: str
    concurrent_streams: int
    direct_openbridge: DirectOpenBridgeConfig
    analog_bridge_usrp: AnalogBridgeUSRPConfig
    managed_openbridge: ManagedOpenBridgeConfig


@dataclass(frozen=True)
class EncoderConfig:
    backend: str
    command: str | None
    output_format: str
    frame_size: int
    timeout_seconds: int
    file: str | None = None


@dataclass(frozen=True)
class TTSConfig:
    backend: str
    espeak_command: str
    voice: str
    speed_wpm: int
    amplitude: int
    command: str | None
    voice_rss_api_key: str | None
    voice_rss_voice: str
    voice_rss_language: str
    voice_rss_speed: int
    voice_rss_max_words: int | None
    voice_rss_codec: str
    voice_rss_format: str


@dataclass(frozen=True)
class AudioConfig:
    target_sample_rate: int
    target_channels: int
    target_sample_width_bytes: int
    converter: str
    normalize: bool


@dataclass(frozen=True)
class AudioSchedulerConfig:
    mode: str
    max_concurrent_groups: int
    same_group_policy: str


@dataclass(frozen=True)
class Config:
    path: Path
    app: AppConfig
    nws: NWSConfig
    announcements: AnnouncementConfig
    output: OutputConfig
    encoder: EncoderConfig
    tts: TTSConfig
    audio: AudioConfig
    audio_scheduler: AudioSchedulerConfig
    alert_repeat: AlertRepeatConfig
    groups: tuple[GroupConfig, ...]


def _path_from(base: Path, value: str | os.PathLike[str]) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def _parse_analog_bridge_usrp(raw: dict[str, Any], prefix: str, defaults: AnalogBridgeUSRPConfig | None = None) -> AnalogBridgeUSRPConfig:
    def pick(name: str, default: Any) -> Any:
        if name in raw:
            return raw[name]
        if defaults is not None:
            return getattr(defaults, name)
        return default

    local_rx = pick("local_rx_port", 32001)
    slot = _int(pick("slot", 2), f"{prefix}.slot")
    color_code = _int(pick("color_code", 1), f"{prefix}.color_code")
    subscriber_id = _int(pick("subscriber_id", 0), f"{prefix}.subscriber_id")
    repeater_id = _int(pick("repeater_id", 0), f"{prefix}.repeater_id")
    if slot not in {1, 2}:
        raise ConfigError(f"{prefix}.slot must be 1 or 2")
    if not 0 <= color_code <= 15:
        raise ConfigError(f"{prefix}.color_code must be 0 through 15")
    if not 0 <= subscriber_id <= 0xFFFFFF:
        raise ConfigError(f"{prefix}.subscriber_id must fit in a 24-bit DMR ID, max 16777215")
    if not 0 <= repeater_id <= 0xFFFFFFFF:
        raise ConfigError(f"{prefix}.repeater_id must fit in a 32-bit peer ID")
    return AnalogBridgeUSRPConfig(
        address=str(pick("address", "127.0.0.1")),
        tx_port=_int(pick("tx_port", 34001), f"{prefix}.tx_port"),
        local_rx_port=None if local_rx is None else _int(local_rx, f"{prefix}.local_rx_port"),
        register=_bool(pick("register", True)),
        sample_rate=_int(pick("sample_rate", 8000), f"{prefix}.sample_rate"),
        frame_ms=_int(pick("frame_ms", 20), f"{prefix}.frame_ms"),
        slot=slot,
        color_code=color_code,
        subscriber_id=subscriber_id,
        repeater_id=repeater_id,
        callsign=str(pick("callsign", "N0CALL")),
        pre_tx_commands=tuple(str(x) for x in _as_list(pick("pre_tx_commands", []))),
    )


def _parse_group_bridge(raw: dict[str, Any], default_usrp: AnalogBridgeUSRPConfig | None, prefix: str) -> GroupBridgeConfig:
    usrp_raw = raw.get("usrp") or raw.get("analog_bridge_usrp") or {}
    if not isinstance(usrp_raw, dict):
        raise ConfigError(f"{prefix}.usrp must be a mapping")
    usrp = _parse_analog_bridge_usrp(usrp_raw, f"{prefix}.usrp", default_usrp)

    files_raw = raw.get("files") or []
    if not isinstance(files_raw, list):
        raise ConfigError(f"{prefix}.files must be a list")
    files = tuple(ManagedFileConfig.from_dict(item or {}, f"{prefix}.files[{index}]") for index, item in enumerate(files_raw))

    processes_raw = raw.get("processes") or []
    if not isinstance(processes_raw, list):
        raise ConfigError(f"{prefix}.processes must be a list")
    processes = tuple(ManagedProcessConfig.from_raw(item, f"{prefix}.processes", index) for index, item in enumerate(processes_raw))

    variables_raw = raw.get("variables") or {}
    if not isinstance(variables_raw, dict):
        raise ConfigError(f"{prefix}.variables must be a mapping")
    variables = {str(k): str(v) for k, v in variables_raw.items()}

    return GroupBridgeConfig(
        usrp=usrp,
        files=files,
        processes=processes,
        variables=variables,
        startup_delay_seconds=_float(raw.get("startup_delay_seconds", 0.5), f"{prefix}.startup_delay_seconds"),
    )


def _parse_managed_openbridge(raw: dict[str, Any]) -> ManagedOpenBridgeConfig:
    helpers_raw = raw.get("helper_paths") or {}
    if not isinstance(helpers_raw, dict):
        raise ConfigError("output.managed_openbridge.helper_paths must be a mapping")
    ports_raw = raw.get("port_allocator") or {}
    if not isinstance(ports_raw, dict):
        raise ConfigError("output.managed_openbridge.port_allocator must be a mapping")

    helpers = ManagedOpenBridgeHelperPathsConfig(
        analog_bridge=str(helpers_raw.get("analog_bridge", "auto")),
        mmdvm_bridge=str(helpers_raw.get("mmdvm_bridge", "auto")),
        md380_emu=str(helpers_raw.get("md380_emu", "auto")),
        md380_emu_args=str(helpers_raw.get("md380_emu_args", "-S {md380emu_port}")),
        md380_emu_wrapper=str(helpers_raw.get("md380_emu_wrapper", "auto")),
        md380_emu_workdir=str(helpers_raw.get("md380_emu_workdir", "auto")),
    )
    ports = ManagedOpenBridgePortAllocatorConfig(
        usrp_rx_base=_int(ports_raw.get("usrp_rx_base", 43001), "output.managed_openbridge.port_allocator.usrp_rx_base"),
        usrp_tx_base=_int(ports_raw.get("usrp_tx_base", 43002), "output.managed_openbridge.port_allocator.usrp_tx_base"),
        ambe_rx_base=_int(ports_raw.get("ambe_rx_base", 43003), "output.managed_openbridge.port_allocator.ambe_rx_base"),
        ambe_tx_base=_int(ports_raw.get("ambe_tx_base", 43004), "output.managed_openbridge.port_allocator.ambe_tx_base"),
        mmdvm_local_base=_int(ports_raw.get("mmdvm_local_base", 43005), "output.managed_openbridge.port_allocator.mmdvm_local_base"),
        hbp_master_base=_int(ports_raw.get("hbp_master_base", 43006), "output.managed_openbridge.port_allocator.hbp_master_base"),
        md380emu_base=_int(ports_raw.get("md380emu_base", 43000), "output.managed_openbridge.port_allocator.md380emu_base"),
        step=_int(ports_raw.get("step", 10), "output.managed_openbridge.port_allocator.step"),
        md380emu_step=_int(ports_raw.get("md380emu_step", 1), "output.managed_openbridge.port_allocator.md380emu_step"),
        scan_limit=max(1, _int(ports_raw.get("scan_limit", 200), "output.managed_openbridge.port_allocator.scan_limit")),
    )
    return ManagedOpenBridgeConfig(
        enabled=_bool(raw.get("enabled", True)),
        start_helpers=_bool(raw.get("start_helpers", True)),
        cleanup_stale_helpers=_bool(raw.get("cleanup_stale_helpers", True)),
        hbp_bind_ip=str(raw.get("hbp_bind_ip", "127.0.0.1")),
        hbp_password=str(raw.get("hbp_password", "skyalert")),
        startup_delay_seconds=_float(raw.get("startup_delay_seconds", 5.0), "output.managed_openbridge.startup_delay_seconds"),
        enforce_group_talkgroup=_bool(raw.get("enforce_group_talkgroup", True)),
        helper_paths=helpers,
        port_allocator=ports,
    )


def _offset_24bit(value: int, offset: int) -> int:
    if value <= 0 or offset <= 0:
        return value
    candidate = value + offset
    if candidate > 0xFFFFFF:
        raise ConfigError("station.source_id plus group index exceeds the 24-bit DMR ID limit")
    return candidate


def _offset_32bit(value: int, offset: int) -> int:
    if value <= 0 or offset <= 0:
        return value
    candidate = value + offset
    if candidate > 0xFFFFFFFF:
        raise ConfigError("station.repeater_id plus group index exceeds the 32-bit peer ID limit")
    return candidate


def _managed_bridge_for_group(
    group: GroupConfig,
    index: int,
    default_usrp: AnalogBridgeUSRPConfig,
    managed: ManagedOpenBridgeConfig,
) -> GroupConfig:
    ports = managed.port_allocator
    offset = index * ports.step
    emu_offset = index * ports.md380emu_step

    # In simplified managed_openbridge mode, each group has separate local DMR
    # subscriber and repeater identities because Analog_Bridge exits if
    # gatewayDmrId/subscriber_id and repeaterID are equal. The optional
    # per-group dmr_id is therefore the subscriber/source ID only. The
    # repeaterID and MMDVM_Bridge [General] Id default to station.repeater_id
    # plus group index, and the outbound OpenBridge SRC_ID remains the global
    # openbridge.network_id.
    auto_source_id = _offset_24bit(default_usrp.subscriber_id, index)
    auto_repeater_id = _offset_32bit(default_usrp.repeater_id, index)
    group_dmr_id = group.dmr_id if group.dmr_id is not None else auto_source_id
    group_source_id = group.source_id if group.source_id is not None else group_dmr_id
    group_repeater_id = group.repeater_id if group.repeater_id is not None else auto_repeater_id
    group_mmdvm_id = group.mmdvm_id if group.mmdvm_id is not None else group_repeater_id
    if group_source_id == group_repeater_id:
        raise ConfigError(
            f"Group {group.name} has the same subscriber/source ID and repeater ID ({group_source_id}); "
            "Analog_Bridge requires them to be different. Set station.repeater_id or groups[].repeater_id to a different value."
        )

    if group.bridge is None:
        usrp = replace(
            default_usrp,
            tx_port=ports.usrp_rx_base + offset,
            local_rx_port=ports.usrp_tx_base + offset,
            subscriber_id=group_source_id,
            repeater_id=group_repeater_id,
        )
        files: tuple[ManagedFileConfig, ...] = ()
        processes: tuple[ManagedProcessConfig, ...] = ()
        variables: dict[str, str] = {}
        startup_delay = managed.startup_delay_seconds
    else:
        usrp = group.bridge.usrp
        if group.dmr_id is not None or group.source_id is not None or group.repeater_id is not None:
            effective_source = group.source_id if group.source_id is not None else (group.dmr_id if group.dmr_id is not None else usrp.subscriber_id)
            effective_repeater = group.repeater_id if group.repeater_id is not None else usrp.repeater_id
            if effective_source == effective_repeater:
                raise ConfigError(
                    f"Group {group.name} has the same subscriber/source ID and repeater ID ({effective_source}); "
                    "Analog_Bridge requires them to be different. Set groups[].repeater_id to a different value."
                )
            usrp = replace(
                usrp,
                subscriber_id=effective_source,
                repeater_id=effective_repeater,
            )
        group_source_id = usrp.subscriber_id
        group_repeater_id = usrp.repeater_id
        if group.mmdvm_id is None:
            group_mmdvm_id = group_repeater_id
        files = group.bridge.files
        processes = group.bridge.processes
        variables = dict(group.bridge.variables)
        startup_delay = group.bridge.startup_delay_seconds

    defaults = {
        "md380emu_port": str(ports.md380emu_base + emu_offset),
        "ab_ambe_rx_port": str(ports.ambe_rx_base + offset),
        "ab_ambe_tx_port": str(ports.ambe_tx_base + offset),
        "mmdvm_local_port": str(ports.mmdvm_local_base + offset),
        "hbp_master_port": str(ports.hbp_master_base + offset),
        "hbp_password": managed.hbp_password,
        # MMDVM_Bridge [General] Id follows the local repeater/peer identity,
        # not the subscriber/source ID. Keep it explicit so rendered configs
        # show that Analog_Bridge's subscriber and repeater IDs differ.
        "mmdvm_id": str(group_mmdvm_id),
    }
    defaults.update(variables)
    bridge = GroupBridgeConfig(
        usrp=usrp,
        files=files,
        processes=processes,
        variables=defaults,
        startup_delay_seconds=startup_delay,
    )
    return replace(group, bridge=bridge)


def _event_list(nws_raw: dict[str, Any], events_raw: dict[str, Any], include: bool) -> tuple[str, ...]:
    keys = ("include_events", "included_events") if include else ("exclude_events", "excluded_events")
    value = None
    for source in (nws_raw, events_raw):
        for key in keys:
            if key in source:
                value = source[key]
                break
        if value is not None:
            break
    return tuple(str(x).strip() for x in _as_list(value) if str(x).strip())


def _identity_value(identity_raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in identity_raw:
            return identity_raw[key]
    return default


def _pick_first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _env_or_value(value: Any, env_name: Any = None) -> str | None:
    if value not in (None, ""):
        return str(value)
    if env_name not in (None, ""):
        return os.environ.get(str(env_name))
    return None


def _simple_internal_ports(raw: dict[str, Any], mob_raw: dict[str, Any]) -> dict[str, Any]:
    """Map a single internal port range to the older detailed allocator shape."""
    range_raw = _mapping(raw.get("internal_ports") or raw.get("internal_port_range"))
    if not range_raw:
        return mob_raw
    start = _int(_first_present(range_raw.get("start"), range_raw.get("base"), default=43000), "internal_ports.start")
    step = _int(range_raw.get("step", 10), "internal_ports.step")
    if step < 7:
        raise ConfigError("internal_ports.step must be at least 7")
    if range_raw.get("end") is not None:
        end = _int(range_raw.get("end"), "internal_ports.end")
        if end < start + 6:
            raise ConfigError("internal_ports.end must allow at least 7 ports after internal_ports.start")
        scan_limit = max(1, ((end - start - 6) // step) + 1)
    else:
        scan_limit = _int(range_raw.get("scan_limit", range_raw.get("blocks", 200)), "internal_ports.scan_limit")
    port_allocator = dict(_mapping(mob_raw.get("port_allocator")))
    derived = {
        "md380emu_base": start + 0,
        "usrp_rx_base": start + 1,
        "usrp_tx_base": start + 2,
        "ambe_rx_base": start + 3,
        "ambe_tx_base": start + 4,
        "mmdvm_local_base": start + 5,
        "hbp_master_base": start + 6,
        "step": step,
        "md380emu_step": step,
        "scan_limit": scan_limit,
    }
    for key, value in derived.items():
        port_allocator.setdefault(key, value)
    mob_raw = dict(mob_raw)
    mob_raw["port_allocator"] = port_allocator
    return mob_raw


def load_config(path: str | os.PathLike[str]) -> Config:
    cfg_path = Path(path).expanduser().resolve()
    base = cfg_path.parent
    with cfg_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Top-level YAML value must be a mapping")

    app_raw = _mapping(raw.get("app"))
    nws_raw = _mapping(raw.get("nws"))
    events_raw = _mapping(raw.get("events"))
    ann_raw = _mapping(raw.get("announcements"))
    out_raw = _mapping(raw.get("output"))
    simple_ob_raw = _mapping(raw.get("openbridge"))
    direct_raw = dict(_mapping(out_raw.get("direct_openbridge")))
    usrp_raw = dict(_mapping(out_raw.get("analog_bridge_usrp")))
    mob_raw = dict(_mapping(out_raw.get("managed_openbridge")))
    helpers_raw = _mapping(raw.get("helpers") or raw.get("helper_paths") or raw.get("binaries"))
    identity_raw = _mapping(raw.get("station") or raw.get("dmr") or raw.get("radio") or raw.get("identity"))
    enc_raw = _mapping(raw.get("encoder"))
    tts_raw = _mapping(raw.get("tts"))
    sky_describe_raw = _mapping(raw.get("SkyDescribe") or raw.get("skydescribe") or raw.get("sky_describe"))
    rss_raw = _mapping(tts_raw.get("voice_rss"))
    audio_raw = _mapping(raw.get("audio"))
    scheduler_raw = _mapping(raw.get("audio_scheduler") or raw.get("scheduler"))
    repeat_raw = _mapping(raw.get("alert_repeat") or raw.get("repeat_policy"))

    # New simplified config shape:
    #   openbridge: {target_ip, target_port, passphrase, network_id}
    #   station: {callsign, source_id, repeater_id, slot, color_code}
    #   helpers: {analog_bridge, mmdvm_bridge, md380_emu}
    #   internal_ports: {start, step}
    # It is translated into the older internal structure here for compatibility.
    if simple_ob_raw:
        aliases = {
            "local_ip": ("local_ip",),
            "local_port": ("local_port",),
            "target_ip": ("target_ip", "remote_ip", "host", "ip"),
            "target_port": ("target_port", "remote_port", "port"),
            "passphrase": ("passphrase", "password", "secret"),
            "network_id": ("network_id", "peer_id"),
            "source_id": ("source_id", "subscriber_id", "dmr_id"),
            "peer_id": ("peer_id", "repeater_id"),
            "slot": ("slot",),
            "color_code": ("color_code", "colorcode"),
        }
        for target_key, source_keys in aliases.items():
            if target_key in direct_raw:
                continue
            for source_key in source_keys:
                if source_key in simple_ob_raw:
                    direct_raw[target_key] = simple_ob_raw[source_key]
                    break

    station_derived_usrp = {
        "subscriber_id": _identity_value(identity_raw, "subscriber_id", "source_id", "dmr_id"),
        "repeater_id": _identity_value(identity_raw, "repeater_id", "peer_id", "network_id", default=direct_raw.get("network_id")),
        "callsign": _identity_value(identity_raw, "callsign"),
        "slot": _identity_value(identity_raw, "slot", default=direct_raw.get("slot", 1)),
        "color_code": _identity_value(identity_raw, "color_code", "colorcode", default=direct_raw.get("color_code", 1)),
    }
    usrp_raw = _overlay(usrp_raw, station_derived_usrp)

    helper_paths = dict(_mapping(mob_raw.get("helper_paths")))
    for key in ("analog_bridge", "mmdvm_bridge", "md380_emu", "md380_emu_args", "md380_emu_wrapper", "md380_emu_workdir"):
        if key in helpers_raw and key not in helper_paths:
            helper_paths[key] = helpers_raw[key]
    if helper_paths:
        mob_raw["helper_paths"] = helper_paths
    mob_raw = _simple_internal_ports(raw, mob_raw)

    app = AppConfig(
        poll_interval_seconds=_int(app_raw.get("poll_interval_seconds", 60), "app.poll_interval_seconds"),
        state_file=_path_from(base, app_raw.get("state_file", "./state/skyalert_state.json")),
        audio_dir=_path_from(base, app_raw.get("audio_dir", "./state/audio")),
        control_dir=_path_from(base, app_raw.get("control_dir", "./state/control")),
        log_level=str(app_raw.get("log_level", "INFO")).upper(),
        user_agent=str(app_raw.get("user_agent", "WeatherAlertSystem/1.0")),
        dry_run=_bool(app_raw.get("dry_run", False)),
    )

    default_severity = {
        "Extreme": 0,
        "Severe": 1,
        "Moderate": 2,
        "Minor": 3,
        "Unknown": 4,
    }
    severity_raw = nws_raw.get("severity_rank") or default_severity
    nws = NWSConfig(
        api_base=str(nws_raw.get("api_base", "https://api.weather.gov")).rstrip("/"),
        request_timeout_seconds=_int(nws_raw.get("request_timeout_seconds", 15), "nws.request_timeout_seconds"),
        max_alerts_per_group=_int(nws_raw.get("max_alerts_per_group", 6), "nws.max_alerts_per_group"),
        include_events=_event_list(nws_raw, events_raw, include=True),
        exclude_events=_event_list(nws_raw, events_raw, include=False),
        severity_rank={str(k): int(v) for k, v in severity_raw.items()},
    )

    tail_raw = raw.get("tail_message")
    if isinstance(tail_raw, str):
        tail_text = tail_raw
        tail_enabled = bool(tail_text.strip())
    else:
        tail_map = _mapping(tail_raw)
        ann_tail = ann_raw.get("tail_message")
        if isinstance(ann_tail, dict):
            tail_map = _overlay(tail_map, ann_tail)
            ann_tail = None
        tail_text = str(_first_present(tail_map.get("text"), tail_map.get("message"), ann_tail, default=""))
        tail_enabled = _bool(_first_present(tail_map.get("enabled"), ann_raw.get("tail_message_enabled"), default=bool(tail_text.strip())))

    announcements = AnnouncementConfig(
        say_new_alerts=_bool(ann_raw.get("say_new_alerts", True)),
        say_changed_alerts=_bool(ann_raw.get("say_changed_alerts", True)),
        say_all_clear=_bool(ann_raw.get("say_all_clear", True)),
        include_description=_bool(ann_raw.get("include_description", True)),
        include_instruction=_bool(ann_raw.get("include_instruction", True)),
        max_text_chars=_int(ann_raw.get("max_text_chars", 4000), "announcements.max_text_chars"),
        intro=str(ann_raw.get("intro", "Skywarn alert")),
        all_clear_text=str(ann_raw.get("all_clear_text", "All clear. No active weather alerts remain for {group}.")),
        tail_message_enabled=tail_enabled,
        tail_message=tail_text,
        text_cleanup_enabled=_bool(ann_raw.get("text_cleanup_enabled", ann_raw.get("clean_text", True))),
    )

    direct = DirectOpenBridgeConfig(
        local_ip=str(direct_raw.get("local_ip", "0.0.0.0")),
        local_port=_int(direct_raw.get("local_port", 0), "openbridge.local_port"),
        target_ip=str(direct_raw.get("target_ip", "CHANGE_ME_OPENBRIDGE_HOST")),
        target_port=_int(direct_raw.get("target_port", 54097), "openbridge.target_port"),
        passphrase=str(direct_raw.get("passphrase", "CHANGE_ME_OPENBRIDGE_SECRET")),
        network_id=_int(direct_raw.get("network_id", 0), "openbridge.network_id"),
        source_id=_int(_first_present(direct_raw.get("source_id"), _identity_value(identity_raw, "source_id", "subscriber_id", "dmr_id"), default=0), "openbridge.source_id"),
        peer_id=_int(_first_present(direct_raw.get("peer_id"), _identity_value(identity_raw, "peer_id", "repeater_id"), direct_raw.get("network_id"), default=0), "openbridge.peer_id"),
        slot=_int(direct_raw.get("slot", _identity_value(identity_raw, "slot", default=1)), "openbridge.slot"),
        color_code=_int(direct_raw.get("color_code", _identity_value(identity_raw, "color_code", "colorcode", default=1)), "openbridge.color_code"),
        frame_interval_ms=_int(direct_raw.get("frame_interval_ms", 60), "openbridge.frame_interval_ms"),
        silence_ambe72_hex=str(direct_raw.get("silence_ambe72_hex", "000000000000000000")),
    )

    usrp = _parse_analog_bridge_usrp(usrp_raw, "station")
    managed_openbridge = _parse_managed_openbridge(mob_raw)

    default_mode = "managed_openbridge" if simple_ob_raw or not out_raw else "dry_run"
    output = OutputConfig(
        mode=str(out_raw.get("mode", default_mode)),
        concurrent_streams=max(1, _int(out_raw.get("concurrent_streams", 3), "output.concurrent_streams")),
        direct_openbridge=direct,
        analog_bridge_usrp=usrp,
        managed_openbridge=managed_openbridge,
    )

    encoder = EncoderConfig(
        backend=str(enc_raw.get("backend", "external_ambe72")),
        command=None if enc_raw.get("command") is None else str(enc_raw.get("command")),
        output_format=str(enc_raw.get("output_format", "hex_lines")),
        frame_size=_int(enc_raw.get("frame_size", 9), "encoder.frame_size"),
        timeout_seconds=_int(enc_raw.get("timeout_seconds", 120), "encoder.timeout_seconds"),
        file=None if enc_raw.get("file") is None else str(enc_raw.get("file")),
    )

    # VoiceRSS/SkyDescribe compatibility. The original SkyWarnPlus-style
    # section can be used directly:
    #   SkyDescribe: {APIKey, Language, Speed, Voice, MaxWords}
    # The newer tts.voice_rss section remains supported as well.
    if sky_describe_raw and "backend" not in tts_raw:
        tts_backend = "voice_rss"
    else:
        tts_backend = str(tts_raw.get("backend", "espeak"))
    voice_rss_key = _env_or_value(
        _pick_first(rss_raw, "api_key", "APIKey", default=_pick_first(sky_describe_raw, "APIKey", "api_key")),
        _pick_first(rss_raw, "api_key_env", "APIKeyEnv", default=_pick_first(sky_describe_raw, "APIKeyEnv", "api_key_env")),
    )
    max_words_value = _pick_first(rss_raw, "max_words", "MaxWords", default=_pick_first(sky_describe_raw, "MaxWords", "max_words"))

    tts = TTSConfig(
        backend=tts_backend,
        espeak_command=str(tts_raw.get("espeak_command", "espeak-ng")),
        voice=str(tts_raw.get("voice", "en-us")),
        speed_wpm=_int(tts_raw.get("speed_wpm", 145), "tts.speed_wpm"),
        amplitude=_int(tts_raw.get("amplitude", 120), "tts.amplitude"),
        command=None if tts_raw.get("command") is None else str(tts_raw.get("command")),
        voice_rss_api_key=voice_rss_key,
        voice_rss_voice=str(_pick_first(rss_raw, "voice", "Voice", default=_pick_first(sky_describe_raw, "Voice", "voice", default="John"))),
        voice_rss_language=str(_pick_first(rss_raw, "language", "Language", default=_pick_first(sky_describe_raw, "Language", "language", default="en-us"))),
        voice_rss_speed=_int(_pick_first(rss_raw, "speed", "Speed", default=_pick_first(sky_describe_raw, "Speed", "speed", default=0)), "tts.voice_rss.speed"),
        voice_rss_max_words=None if max_words_value is None else max(1, _int(max_words_value, "tts.voice_rss.max_words")),
        voice_rss_codec=str(_pick_first(rss_raw, "codec", "Codec", default=_pick_first(sky_describe_raw, "Codec", "codec", default="WAV"))),
        voice_rss_format=str(_pick_first(rss_raw, "format", "Format", default=_pick_first(sky_describe_raw, "Format", "format", default="8khz_16bit_mono"))),
    )

    audio = AudioConfig(
        target_sample_rate=_int(audio_raw.get("target_sample_rate", 8000), "audio.target_sample_rate"),
        target_channels=_int(audio_raw.get("target_channels", 1), "audio.target_channels"),
        target_sample_width_bytes=_int(audio_raw.get("target_sample_width_bytes", 2), "audio.target_sample_width_bytes"),
        converter=str(audio_raw.get("converter", "auto")),
        normalize=_bool(audio_raw.get("normalize", False)),
    )

    scheduler_mode = str(scheduler_raw.get("mode", "parallel_by_group")).strip().lower()
    if scheduler_mode not in {"parallel_by_group", "serial"}:
        raise ConfigError("audio_scheduler.mode must be parallel_by_group or serial")
    same_group_policy = str(scheduler_raw.get("same_group_policy", "queue")).strip().lower()
    if same_group_policy not in {"queue"}:
        raise ConfigError("audio_scheduler.same_group_policy currently supports only queue")
    scheduler_max = scheduler_raw.get("max_concurrent_groups", out_raw.get("concurrent_streams", 3))
    audio_scheduler = AudioSchedulerConfig(
        mode=scheduler_mode,
        max_concurrent_groups=max(1, _int(scheduler_max, "audio_scheduler.max_concurrent_groups")),
        same_group_policy=same_group_policy,
    )


    repeat_policy = str(repeat_raw.get("unchanged_policy", "ignore")).strip().lower()
    if repeat_policy not in {"ignore"}:
        raise ConfigError("alert_repeat.unchanged_policy currently supports only ignore")
    alert_repeat = AlertRepeatConfig(
        enabled=_bool(repeat_raw.get("enabled", False)),
        repeat_after_minutes=max(0, _int(repeat_raw.get("repeat_after_minutes", 0), "alert_repeat.repeat_after_minutes")),
        unchanged_policy=repeat_policy,
    )

    groups_raw = raw.get("groups") or []
    if not isinstance(groups_raw, list):
        raise ConfigError("groups must be a list")
    groups = tuple(GroupConfig.from_dict(item or {}, usrp) for item in groups_raw)
    if not groups:
        raise ConfigError("At least one group is required")

    if output.mode.lower() == "managed_openbridge":
        groups = tuple(_managed_bridge_for_group(group, index, usrp, managed_openbridge) for index, group in enumerate(groups))

    return Config(
        path=cfg_path,
        app=app,
        nws=nws,
        announcements=announcements,
        output=output,
        encoder=encoder,
        tts=tts,
        audio=audio,
        audio_scheduler=audio_scheduler,
        alert_repeat=alert_repeat,
        groups=groups,
    )
