from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_nws_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class WeatherAlert:
    id: str
    event: str
    headline: str = ""
    description: str = ""
    instruction: str = ""
    area_desc: str = ""
    severity: str = "Unknown"
    urgency: str = "Unknown"
    certainty: str = "Unknown"
    status: str = ""
    message_type: str = ""
    sender_name: str = ""
    effective: datetime | None = None
    onset: datetime | None = None
    expires: datetime | None = None
    ends: datetime | None = None
    sent: datetime | None = None
    affected_zones: tuple[str, ...] = field(default_factory=tuple)
    geocode_same: tuple[str, ...] = field(default_factory=tuple)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_feature(cls, feature: dict[str, Any]) -> "WeatherAlert":
        props = feature.get("properties") or {}
        geocode = props.get("geocode") or {}
        same = tuple(str(x) for x in geocode.get("SAME", []) if x)
        zones = tuple(str(x) for x in props.get("affectedZones", []) if x)
        return cls(
            id=_clean(props.get("id") or feature.get("id")),
            event=_clean(props.get("event")),
            headline=_clean(props.get("headline")),
            description=_clean(props.get("description")),
            instruction=_clean(props.get("instruction")),
            area_desc=_clean(props.get("areaDesc")),
            severity=_clean(props.get("severity")) or "Unknown",
            urgency=_clean(props.get("urgency")) or "Unknown",
            certainty=_clean(props.get("certainty")) or "Unknown",
            status=_clean(props.get("status")),
            message_type=_clean(props.get("messageType")),
            sender_name=_clean(props.get("senderName")),
            effective=parse_nws_time(props.get("effective")),
            onset=parse_nws_time(props.get("onset")),
            expires=parse_nws_time(props.get("expires")),
            ends=parse_nws_time(props.get("ends")),
            sent=parse_nws_time(props.get("sent")),
            affected_zones=zones,
            geocode_same=same,
            raw=feature,
        )

    def fingerprint(self) -> str:
        payload = {
            "event": self.event,
            "headline": self.headline,
            "description": self.description,
            "instruction": self.instruction,
            "area_desc": self.area_desc,
            "severity": self.severity,
            "urgency": self.urgency,
            "certainty": self.certainty,
            "expires": self.expires.isoformat() if self.expires else "",
            "ends": self.ends.isoformat() if self.ends else "",
            "zones": list(self.affected_zones),
            "same": list(self.geocode_same),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def best_end_time(self) -> datetime | None:
        return self.ends or self.expires

    def spoken_end_time(self) -> str:
        end = self.best_end_time()
        if not end:
            return "until further notice"
        # NWS times are converted to UTC here. Local rendering can be added later.
        return "until " + end.strftime("%Y-%m-%d %H:%M UTC")


@dataclass(frozen=True)
class AlertChangeSet:
    group_name: str
    current: tuple[WeatherAlert, ...]
    new: tuple[WeatherAlert, ...]
    changed: tuple[WeatherAlert, ...]
    cleared_ids: tuple[str, ...]

    @property
    def has_work(self) -> bool:
        return bool(self.new or self.changed or self.cleared_ids)
