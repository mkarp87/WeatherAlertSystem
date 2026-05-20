from __future__ import annotations

import re

from .config import AnnouncementConfig, GroupConfig
from .models import AlertChangeSet, WeatherAlert

_WHITESPACE = re.compile(r"\s+")
_DOT_DOT_DOT = re.compile(r"\.\s*\.\s*\.\s*")

# SkyWarnPlus/SkyDescribe-style replacements used to make NWS alert text sound
# better over TTS. Keep these conservative: only patterns that are commonly
# spoken poorly by VoiceRSS/espeak or commonly appear in NWS text.
_SPEECH_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), replacement)
    for pattern, replacement in (
        (r"\bmph\b", "miles per hour"),
        (r"\bknots\b", "nautical miles per hour"),
        (r"\bNm\b", "nautical miles"),
        (r"\bnm\b", "nautical miles"),
        (r"\bft\.\b", "feet"),
        (r"\bin\.\b", "inches"),
        (r"\bm\b", "meter"),
        (r"\bkm\b", "kilometer"),
        (r"\bmi\b", "mile"),
        (r"\b%\b", "percent"),
        (r"\bN\b", "north"),
        (r"\bS\b", "south"),
        (r"\bE\b", "east"),
        (r"\bW\b", "west"),
        (r"\bNE\b", "northeast"),
        (r"\bNW\b", "northwest"),
        (r"\bSE\b", "southeast"),
        (r"\bSW\b", "southwest"),
        (r"\bF\b", "Fahrenheit"),
        (r"\bC\b", "Celsius"),
        (r"\bUV\b", "ultraviolet"),
        (r"\bgusts up to\b", "gusts of up to"),
        (r"\bhrs\b", "hours"),
        (r"\bhr\b", "hour"),
        (r"\bmin\b", "minute"),
        (r"\bsec\b", "second"),
        (r"\bsq\b", "square"),
        (r"\bw/\b", "with"),
        (r"\bc/o\b", "care of"),
        (r"\bblw\b", "below"),
        (r"\babv\b", "above"),
        (r"\bavg\b", "average"),
        (r"\bfr\b", "from"),
        (r"\btill\b", "until"),
        (r"\bb/w\b", "between"),
        (r"\bbtwn\b", "between"),
        (r"\bN/A\b", "not available"),
        (r"\b&\b", "and"),
        (r"\b\+\b", "plus"),
        (r"\be\.g\.\b", "for example"),
        (r"\bi\.e\.\b", "that is"),
        (r"\best\.\b", "estimated"),
        (r"\bEDT\b", "eastern daylight time"),
        (r"\bEST\b", "eastern standard time"),
        (r"\bCST\b", "central standard time"),
        (r"\bCDT\b", "central daylight time"),
        (r"\bMST\b", "mountain standard time"),
        (r"\bMDT\b", "mountain daylight time"),
        (r"\bPST\b", "pacific standard time"),
        (r"\bPDT\b", "pacific daylight time"),
        (r"\bAKST\b", "Alaska standard time"),
        (r"\bAKDT\b", "Alaska daylight time"),
        (r"\bHST\b", "Hawaii standard time"),
        (r"\bHDT\b", "Hawaii daylight time"),
    )
)


def normalize_for_speech(text: str, cfg: AnnouncementConfig | None = None) -> str:
    """Clean NWS alert text before TTS.

    This mirrors the useful SkyWarnPlus/SkyDescribe behavior: remove NWS bullet
    asterisks, collapse whitespace, expand common weather abbreviations, smooth
    ellipses, and make compact time strings easier for TTS to pronounce.
    """
    text = (text or "").replace("\n", " ")
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""
    if cfg is not None and not cfg.text_cleanup_enabled:
        return text

    text = text.replace("*", "")
    text = _DOT_DOT_DOT.sub(" ", text)
    # Split compact number+lowercase-unit groups, e.g. 60mph -> 60 mph.
    # Do not split station/callsign-like tokens such as NC4ES.
    text = re.sub(r"(\d)(?=[a-z])", r"\1 ", text)
    for pattern, replacement in _SPEECH_REPLACEMENTS:
        text = pattern.sub(replacement, text)

    # 630 PM -> 6:30 PM, 1125AM -> 11:25AM.
    text = re.sub(r"\b(\d{1,2})(\d{2}\s*[AP]M)\b", r"\1:\2", text)
    text = _DOT_DOT_DOT.sub(" ", text)
    text = re.sub(r"\.\s*", ". ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def _sentence(text: str, cfg: AnnouncementConfig | None = None) -> str:
    text = normalize_for_speech(text, cfg)
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text


def _trim(text: str, max_chars: int) -> str:
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" .,;") + "."


def apply_tail_message(text: str, cfg: AnnouncementConfig) -> str:
    text = normalize_for_speech(text, cfg)
    tail = _sentence(cfg.tail_message, cfg) if cfg.tail_message_enabled else ""
    if not tail:
        return text
    if not text:
        return tail
    return f"{text} {tail}"


def _alert_phrase(alert: WeatherAlert, cfg: AnnouncementConfig) -> str:
    parts = [
        _sentence(alert.event, cfg),
        _sentence(alert.headline, cfg),
    ]
    if alert.area_desc:
        parts.append(_sentence(f"Affected area: {alert.area_desc}", cfg))
    end_time = alert.spoken_end_time()
    if end_time:
        parts.append(_sentence(end_time, cfg))
    if cfg.include_description and alert.description:
        parts.append(_sentence(alert.description, cfg))
    if cfg.include_instruction and alert.instruction:
        parts.append(_sentence(alert.instruction, cfg))
    return " ".join(p for p in parts if p)


def build_announcement(group: GroupConfig, changes: AlertChangeSet, cfg: AnnouncementConfig) -> str | None:
    phrases: list[str] = []

    if changes.new and cfg.say_new_alerts:
        phrases.append(_sentence(f"{cfg.intro} for {group.name}", cfg))
        for alert in changes.new:
            phrases.append(_alert_phrase(alert, cfg))

    if changes.changed and cfg.say_changed_alerts:
        if not phrases:
            phrases.append(_sentence(f"{cfg.intro} update for {group.name}", cfg))
        else:
            phrases.append(_sentence("Updated alert information", cfg))
        for alert in changes.changed:
            phrases.append(_alert_phrase(alert, cfg))

    if changes.cleared_ids and cfg.say_all_clear and not changes.current:
        phrases.append(_sentence(cfg.all_clear_text.format(group=group.name, talkgroup=group.talkgroup), cfg))

    text = " ".join(p for p in phrases if p).strip()
    if not text:
        return None
    return _trim(normalize_for_speech(text, cfg), cfg.max_text_chars)
