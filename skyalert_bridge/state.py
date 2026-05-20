from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .models import AlertChangeSet, WeatherAlert


@dataclass
class StateStore:
    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "StateStore":
        if not path.exists():
            return cls(path=path, data={"groups": {}})
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            data = {"groups": {}}
        data.setdefault("groups", {})
        return cls(path=path, data=data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=str(self.path.parent), delete=False) as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            tmp_name = handle.name
        Path(tmp_name).replace(self.path)

    def changes_for_group(self, group_name: str, current_alerts: list[WeatherAlert]) -> AlertChangeSet:
        groups = self.data.setdefault("groups", {})
        group_state = groups.setdefault(group_name, {})
        previous: dict[str, str] = dict(group_state.get("alerts") or {})
        current = {alert.id: alert.fingerprint() for alert in current_alerts}

        new = [alert for alert in current_alerts if alert.id not in previous]
        changed = [alert for alert in current_alerts if alert.id in previous and previous[alert.id] != current[alert.id]]
        cleared_ids = [alert_id for alert_id in previous if alert_id not in current]

        group_state["alerts"] = current
        return AlertChangeSet(
            group_name=group_name,
            current=tuple(current_alerts),
            new=tuple(new),
            changed=tuple(changed),
            cleared_ids=tuple(cleared_ids),
        )
