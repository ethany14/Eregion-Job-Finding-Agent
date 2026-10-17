"""Memory stage 2: stable keys, normalised values and validity dates.

Deterministic and conservative: it maps spelling variants and known aliases to one concept and
one value form so the same preference said twice (or in Chinese and English) is recognised as
the same record. It never decides what a user meant; the extractor (model) proposes the concept,
and anything it cannot normalise is kept as stated.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

# Variants a model or an older writer may produce for the same concept.
KEY_ALIASES = {
    "job.remote_preference": "job.location_mode",
    "job.work_mode": "job.location_mode",
    "job.workplace_type": "job.location_mode",
    "job.work_arrangement": "job.location_mode",
    "resume.summary.length": "resume.summary.max_sentences",
    "resume.summary.sentence_limit": "resume.summary.max_sentences",
    "cover_letter.style": "cover_letter.tone",
}

# Per-concept value aliases (English and Chinese forms of the same option).
VALUE_ALIASES = {
    "job.location_mode": {
        "remote": "remote", "fully remote": "remote", "remote only": "remote", "远程": "remote", "全远程": "remote",
        "hybrid": "hybrid", "混合": "hybrid", "混合办公": "hybrid",
        "onsite": "onsite", "on-site": "onsite", "on site": "onsite", "in office": "onsite",
        "现场": "onsite", "坐班": "onsite", "到岗": "onsite",
    },
}

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_ALLOWED = re.compile(r"[^a-z0-9._:-]+")


# Canonical concepts already used by governed writers (proposals, materials, applications).
KNOWN_KEYS = frozenset({*KEY_ALIASES.values(), "resume.summary.max_sentences", "writing.style",
                        "cover_letter.tone", "avoid_unsupported_metrics", "job.relocation"})


def _skeleton(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


_BY_SKELETON = {_skeleton(k): k for k in KNOWN_KEYS} | {_skeleton(a): c for a, c in KEY_ALIASES.items()}


def normalize_key(raw: str) -> str:
    """Known concepts match regardless of separators ("Job Location Mode", "job_location_mode",
    "job.work_mode" -> "job.location_mode"); unknown concepts keep a cleaned stable form."""
    key = str(raw or "").strip().casefold()
    known = _BY_SKELETON.get(_skeleton(key))
    if known:
        return known
    key = re.sub(r"[\s\-/]+", "_", key)
    key = _ALLOWED.sub(".", key)
    key = re.sub(r"\.{2,}", ".", key).strip(".")
    return KEY_ALIASES.get(key, key)[:160]


def normalize_value(key: str, value):
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [normalize_value(key, v) for v in value]
    text = re.sub(r"\s+", " ", str(value)).strip()
    lowered = text.casefold()
    aliases = VALUE_ALIASES.get(key)
    if aliases and lowered in aliases:
        return aliases[lowered]
    if lowered in _NUMBER_WORDS:
        return _NUMBER_WORDS[lowered]
    if re.fullmatch(r"-?\d+", lowered):
        return int(lowered)
    return text


def same_value(key: str, left, right) -> bool:
    return normalize_value(key, left) == normalize_value(key, right)


def parse_validity_date(value) -> datetime | None:
    """An explicit ISO date/datetime only. Relative expressions ("next month") must already have
    been resolved by the extractor against the message date; anything else is unknown (None)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = date.fromisoformat(text)
            return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
