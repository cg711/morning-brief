"""Upcoming events for the daily brief ("Around town").

The worker reads the pages listed in events.yaml and returns picks as data; the server checks them and writes every
spoken word itself. This module holds the sources, the claim block, the checks, the sentences and the record of what
has already been mentioned.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from .deepdives import _has_control_chars

log = logging.getLogger(__name__)
SEGMENT = "events"
HEADLINE = "Around town"
WINDOW_DAYS = 7
MENTION_LOOKBACK_DAYS = 14
KEEP_DAYS = 30


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str


def load_sources(path) -> list[Source]:
    """The usable pages in the yaml file, numbered e1, e2, … in file order. Entries that are not a mapping with a
    non-empty name and an https:// url are skipped (and not counted). Raises on a wholly bad file."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    sources: list[Source] = []
    for entry in data["sources"] or []:
        if not isinstance(entry, dict):
            continue
        name, url = entry.get("name"), entry.get("url")
        if not (isinstance(name, str) and isinstance(url, str)):
            continue
        name, url = name.strip(), url.strip()
        if name and url.startswith("https://"):
            sources.append(Source(f"e{len(sources) + 1}", name, url))
    return sources


def title_key(title: str) -> str:
    """A title with case, punctuation, spacing and width variants folded away, for spotting repeats."""
    text = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(re.sub(r"[^\w\s]", "", text).split())


def mentioned(conn, episode_date: date) -> list[dict]:
    """Events not yet past that an earlier day's episode in the last two weeks has already talked about (not the
    same day's: a regenerated brief may pick the same events again)."""
    rows = conn.execute(
        "SELECT title, date FROM event_mentions WHERE date >= ? AND mentioned_on >= ? AND mentioned_on < ? "
        "ORDER BY date, title_key",
        (episode_date.isoformat(), (episode_date - timedelta(days=MENTION_LOOKBACK_DAYS)).isoformat(),
         episode_date.isoformat()))
    return [{"title": r["title"], "date": r["date"]} for r in rows]


def claim_block(settings, conn, episode_date: date) -> dict | None:
    """What the worker is told about events today, or None (feature off, no sources, or any error: the brief
    must go ahead without events rather than fail)."""
    if not settings.events:
        return None
    try:
        sources = load_sources(settings.events_path)
        if not sources:
            return None
        return {"window": {"from": episode_date.isoformat(),
                           "to": (episode_date + timedelta(days=WINDOW_DAYS)).isoformat()},
                "max": settings.events_max,
                "sources": [{"id": s.id, "name": s.name, "url": s.url} for s in sources],
                "mentioned": mentioned(conn, episode_date)}
    except Exception:
        log.exception("events: could not build the events block; the brief goes ahead without it")
        return None


def record(conn, items: list[dict], episode_date: date) -> None:
    """Remember that an episode spoke these events (call only after the episode is published)."""
    for item in items:
        conn.execute(
            "INSERT INTO event_mentions (title_key, title, date, mentioned_on) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(title_key, date) DO UPDATE SET title = excluded.title, mentioned_on = excluded.mentioned_on",
            (title_key(item["title"]), item["title"], item["date"], episode_date.isoformat()))


def prune(conn, today: date) -> int:
    """Forget events that happened more than KEEP_DAYS ago."""
    return conn.execute("DELETE FROM event_mentions WHERE date < ?",
                        ((today - timedelta(days=KEEP_DAYS)).isoformat(),)).rowcount


TITLE_MAX, VENUE_MAX = 120, 80
KEYS = {"title", "date", "time", "venue", "source_id"}
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def _parse_date(value) -> date | None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _text_problems(value, label: str, limit: int, where: str) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{where} needs a non-empty '{label}'"]
    if _has_control_chars(value):
        return [f"{where} '{label}' contains control characters"]
    if len(" ".join(value.split())) > limit:
        return [f"{where} '{label}' must be at most {limit} characters"]
    return []


def validate(items, block: dict | None) -> list[str]:
    """Problems with the worker's `events` list (empty when it is fine). Never raises, whatever `items` holds."""
    if items is None or items == []:
        return []
    if block is None:
        return ["there is no events block today; remove 'events'"]
    if not isinstance(items, list):
        return ["'events' must be a list"]
    if len(items) > block["max"]:
        return [f"'events' can have at most {block['max']} items"]
    start = date.fromisoformat(block["window"]["from"])
    end = date.fromisoformat(block["window"]["to"])
    source_ids = {s["id"] for s in block["sources"]}
    already = {(title_key(m["title"]), m["date"]) for m in block["mentioned"]}
    seen: set[tuple[str, str]] = set()
    problems: list[str] = []
    for n, item in enumerate(items, start=1):
        where = f"event {n}"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        if set(item) - KEYS:
            problems.append(f"{where} has unknown fields {sorted(set(item) - KEYS)}")
        title_problems = _text_problems(item.get("title"), "title", TITLE_MAX, where)
        if not title_problems and not title_key(item["title"]):
            title_problems = [f"{where} 'title' needs letters or numbers"]
        problems += title_problems
        if item.get("venue") is not None:
            venue_problems = _text_problems(item["venue"], "venue", VENUE_MAX, where)
            if not venue_problems and not title_key(item["venue"]):
                venue_problems = [f"{where} 'venue' needs letters or numbers"]
            problems += venue_problems
        day = _parse_date(item.get("date"))
        if day is None:
            problems.append(f"{where} 'date' must be a date like 2026-10-17")
        elif not start <= day <= end:
            problems.append(f"{where} date {day} is outside {start} to {end}")
        if item.get("time") is not None and not (isinstance(item["time"], str) and _TIME_RE.fullmatch(item["time"])):
            problems.append(f"{where} 'time' must be 24-hour HH:MM like 19:30")
        if not (isinstance(item.get("source_id"), str) and item["source_id"] in source_ids):
            problems.append(f"{where} 'source_id' must be one of {sorted(source_ids)}")
        if not title_problems and day is not None:
            key = (title_key(item["title"]), day.isoformat())
            if key in seen:
                problems.append(f"{where} appears twice")
            seen.add(key)
            if key in already and (day - start).days > 1:
                problems.append(f"{where} was already mentioned in an earlier brief; pick something else")
    return problems


def clean(items: list[dict]) -> list[dict]:
    """Validated picks reduced to the known keys, with whitespace collapsed, in the order they will be spoken."""
    out = []
    for item in items:
        venue = " ".join(item["venue"].split()) if item.get("venue") else None
        out.append({"title": " ".join(item["title"].split()), "date": item["date"], "time": item.get("time"),
                    "venue": venue, "source_id": item["source_id"]})
    return sorted(out, key=lambda i: (i["date"], i["time"] is None, i["time"] or ""))


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def _num(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")


def time_words(value: str) -> str:
    """'19:30' → 'seven thirty in the evening'. Midnight and noon are named."""
    hour, minute = int(value[:2]), int(value[3:])
    if (hour, minute) == (0, 0):
        return "midnight"
    if (hour, minute) == (12, 0):
        return "noon"
    words = _num(hour % 12 or 12)
    if minute:
        words += (" oh " if minute < 10 else " ") + _num(minute)
    part = ("in the morning" if 5 <= hour < 12 else "in the afternoon" if 12 <= hour < 17
            else "in the evening" if 17 <= hour < 21 else "at night")
    return f"{words} {part}"


def _trim(text: str) -> str:
    return text.strip().rstrip(" .!?,;:")


def _join(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def _sentence(item: dict, episode_date: date) -> str:
    day = date.fromisoformat(item["date"])
    offset = (day - episode_date).days
    when = ("Today" if offset == 0 else "Tomorrow" if offset == 1
            else f"Next {day:%A}" if offset >= 7 else f"{day:%A}")
    if item["time"]:
        when += f" at {time_words(item['time'])}"
    text = f"{when}, {_trim(item['title'])}"
    if item["venue"]:
        text += f", at {_trim(item['venue'])}"
    return text + "."


def compose(items: list[dict], block: dict, episode_date: date) -> str:
    """The spoken events segment: a fixed frame around the validated fields, nothing else."""
    names = {s["id"]: s["name"] for s in block["sources"]}
    used = list(dict.fromkeys(names[i["source_id"]] for i in items))
    parts = ["Around town this week.", *(_sentence(i, episode_date) for i in items),
             f"Listings from {_join(used)}."]
    return " ".join(parts)


def segment(items: list[dict], block: dict, episode_date: date) -> dict:
    return {"segment": SEGMENT, "headline": HEADLINE, "text": compose(items, block, episode_date), "item_ids": []}
