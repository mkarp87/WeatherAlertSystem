from skyalert_bridge.models import WeatherAlert
from skyalert_bridge.state import StateStore


def test_unchanged_alert_does_not_repeat(tmp_path):
    store = StateStore.load(tmp_path / "state.json")
    alert = WeatherAlert(id="alert-1", event="Severe Thunderstorm Warning", headline="Warning issued", description="same")

    first = store.changes_for_group("Greenville", [alert])
    assert first.new == (alert,)
    assert first.has_work

    second = store.changes_for_group("Greenville", [alert])
    assert not second.has_work
    assert second.new == ()
    assert second.changed == ()
    assert second.cleared_ids == ()


def test_changed_alert_announces_once(tmp_path):
    store = StateStore.load(tmp_path / "state.json")
    original = WeatherAlert(id="alert-1", event="Severe Thunderstorm Warning", headline="Warning issued", description="original")
    changed = WeatherAlert(id="alert-1", event="Severe Thunderstorm Warning", headline="Warning issued", description="updated")

    assert store.changes_for_group("Greenville", [original]).new == (original,)
    second = store.changes_for_group("Greenville", [changed])
    assert second.changed == (changed,)
    assert second.has_work
    third = store.changes_for_group("Greenville", [changed])
    assert not third.has_work


def test_alert_sent_time_change_alone_does_not_repeat(tmp_path):
    from datetime import datetime, timezone
    store = StateStore.load(tmp_path / "state.json")
    a1 = WeatherAlert(id="alert-1", event="Flood Advisory", headline="Advisory", description="same", sent=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc))
    a2 = WeatherAlert(id="alert-1", event="Flood Advisory", headline="Advisory", description="same", sent=datetime(2026, 5, 20, 12, 1, tzinfo=timezone.utc))

    assert store.changes_for_group("Farmville", [a1]).has_work
    assert not store.changes_for_group("Farmville", [a2]).has_work
