from __future__ import annotations

import logging
from typing import Iterable

import requests

from .config import GroupConfig, NWSConfig
from .models import WeatherAlert

logger = logging.getLogger(__name__)


class NWSClient:
    def __init__(self, cfg: NWSConfig, user_agent: str):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/geo+json, application/json",
        })

    def get_active_alerts_for_zone(self, zone_id: str) -> list[WeatherAlert]:
        url = f"{self.cfg.api_base}/alerts/active/zone/{zone_id}"
        response = self.session.get(url, timeout=self.cfg.request_timeout_seconds)
        response.raise_for_status()
        body = response.json()
        features = body.get("features") or []
        if not isinstance(features, list):
            logger.warning("NWS response for %s did not contain a features list", zone_id)
            return []
        alerts: list[WeatherAlert] = []
        for feature in features:
            try:
                alert = WeatherAlert.from_feature(feature)
            except Exception as exc:  # noqa: BLE001 - preserve polling loop on unexpected upstream shape
                logger.warning("Could not parse NWS alert feature for %s: %s", zone_id, exc)
                continue
            if alert.id and alert.event:
                alerts.append(alert)
        return alerts

    def get_group_alerts(self, group: GroupConfig) -> list[WeatherAlert]:
        by_id: dict[str, WeatherAlert] = {}
        for zone in group.county_codes:
            for alert in self.get_active_alerts_for_zone(zone):
                by_id.setdefault(alert.id, alert)
        return list(by_id.values())


def filter_alerts(
    alerts: Iterable[WeatherAlert],
    global_include: tuple[str, ...],
    global_exclude: tuple[str, ...],
    group: GroupConfig,
) -> list[WeatherAlert]:
    include = set(group.include_events or global_include)
    exclude = set(global_exclude) | set(group.exclude_events)
    result: list[WeatherAlert] = []
    for alert in alerts:
        if include and alert.event not in include:
            continue
        if alert.event in exclude:
            continue
        result.append(alert)
    return result


def sort_alerts(alerts: Iterable[WeatherAlert], severity_rank: dict[str, int]) -> list[WeatherAlert]:
    def _ts(value):
        return value.timestamp() if value is not None else 0.0

    def key(alert: WeatherAlert):
        rank = severity_rank.get(alert.severity, 99)
        end = alert.best_end_time()
        sent = alert.sent
        return (rank, end is None, _ts(end or sent), alert.event, alert.id)

    return sorted(alerts, key=key)
