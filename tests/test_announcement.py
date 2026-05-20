from skyalert_bridge.announcer import apply_tail_message, build_announcement, normalize_for_speech
from skyalert_bridge.config import AnnouncementConfig, GroupConfig
from skyalert_bridge.models import AlertChangeSet, WeatherAlert


def _ann_cfg():
    return AnnouncementConfig(
        say_new_alerts=True,
        say_changed_alerts=True,
        say_all_clear=True,
        include_description=False,
        include_instruction=True,
        max_text_chars=500,
        intro="Skywarn alert",
        all_clear_text="All clear for {group}.",
        tail_message_enabled=False,
        tail_message="",
        text_cleanup_enabled=True,
    )


def test_announcement_contains_group_talkgroup_alert():
    group = GroupConfig(name="ARC125", county_codes=("ARC125",), talkgroup=125)
    alert = WeatherAlert(id="a1", event="Tornado Warning", headline="Tornado Warning issued", instruction="Take shelter now")
    changes = AlertChangeSet(group.name, (alert,), (alert,), (), ())
    text = build_announcement(group, changes, _ann_cfg())
    assert "ARC125" in text
    assert "Tornado Warning" in text
    assert "Take shelter now" in text


def test_tail_message_appends_after_text():
    cfg = _ann_cfg()
    cfg = cfg.__class__(
        say_new_alerts=cfg.say_new_alerts,
        say_changed_alerts=cfg.say_changed_alerts,
        say_all_clear=cfg.say_all_clear,
        include_description=cfg.include_description,
        include_instruction=cfg.include_instruction,
        max_text_chars=cfg.max_text_chars,
        intro=cfg.intro,
        all_clear_text=cfg.all_clear_text,
        tail_message_enabled=True,
        tail_message="This is NC4ES weather alert",
        text_cleanup_enabled=True,
    )
    assert apply_tail_message("Main message.", cfg).endswith("This is NC4ES weather alert.")


def test_skywarnplus_style_cleanup_removes_asterisks_and_expands_abbreviations():
    cfg = _ann_cfg()
    text = normalize_for_speech("* At 630 PM EDT, winds 60 mph... move N.", cfg)
    assert "*" not in text
    assert "6:30 PM" in text
    assert "eastern daylight time" in text
    assert "miles per hour" in text
    assert "north" in text


def test_announcement_cleanup_applies_to_description():
    group = GroupConfig(name="Greenville", county_codes=("NCC147",), talkgroup=28515)
    alert = WeatherAlert(
        id="a2",
        event="Severe Thunderstorm Warning",
        headline="Warning issued",
        description="* At 745 PM EDT, wind gusts up to 60 mph...",
        instruction="Move inside.",
    )
    cfg = _ann_cfg().__class__(
        say_new_alerts=True,
        say_changed_alerts=True,
        say_all_clear=True,
        include_description=True,
        include_instruction=True,
        max_text_chars=500,
        intro="Skywarn alert",
        all_clear_text="All clear for {group}.",
        tail_message_enabled=False,
        tail_message="",
        text_cleanup_enabled=True,
    )
    text = build_announcement(group, AlertChangeSet(group.name, (alert,), (alert,), (), ()), cfg)
    assert text is not None
    assert "*" not in text
    assert "7:45 PM eastern daylight time" in text
    assert "miles per hour" in text
