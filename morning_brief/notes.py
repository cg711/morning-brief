"""Notes for a coming daily brief ("Your notes") and countdowns mentioned in "Your morning"."""
from __future__ import annotations

import re
import threading
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from . import deepdives
from .config import TZ

NOTE_TEXT_MAX, LABEL_MAX, NOTES_PER_BRIEF = 500, 60, 8
MILESTONES = frozenset({100, 60, 30, 21, 14})
DAILY_FROM = 7
KEEP_DELIVERED_DAYS = 30
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_lock = threading.RLock()


class NoteError(ValueError):
    """Invalid note or countdown input."""


def _today(now: datetime) -> date:
    return now.astimezone(TZ).date()


def next_brief_date(now: datetime, run_at: time) -> date:
    """The first brief whose facts haven't been gathered yet: today before run_at, otherwise tomorrow."""
    local = now.astimezone(TZ)
    return local.date() if local.time() < run_at else local.date() + timedelta(days=1)


def day_name(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if today < d <= today + timedelta(days=6):
        return f"{d:%A}"
    return f"{d:%b} {d.day}"


def host(url: str | None) -> str | None:
    if not url:
        return None
    return (urlsplit(url).hostname or "").removeprefix("www.") or None


def _collapse(text) -> str:
    return " ".join(str(text or "").split())


def add_note(conn, text: str, url: str | None, for_date: date, now: datetime) -> int:
    text = _collapse(text)
    url = (url or "").strip() or None
    if not text and not url:
        raise NoteError("write a note or share a link")
    if len(text) > NOTE_TEXT_MAX:
        raise NoteError(f"a note can be at most {NOTE_TEXT_MAX} characters")
    if _CONTROL_RE.search(text):
        raise NoteError("the note contains control characters")
    if url:
        try:
            url = deepdives._clean_link(url)
        except deepdives.TopicError as exc:
            raise NoteError(str(exc)) from None
    if for_date < _today(now):
        raise NoteError("pick today or a later day")
    with _lock:
        return conn.execute("INSERT INTO notes (text, url, for_date, created_at) VALUES (?, ?, ?, ?)",
                            (text, url, for_date.isoformat(), now.isoformat())).lastrowid


def pending_notes(conn) -> list:
    return conn.execute("SELECT * FROM notes WHERE delivered_on IS NULL ORDER BY for_date, id").fetchall()


def delete_note(conn, note_id: int) -> bool:
    with _lock:
        return conn.execute("DELETE FROM notes WHERE id = ? AND delivered_on IS NULL", (note_id,)).rowcount > 0


def due_notes(conn, today: date) -> list[dict]:
    """Undelivered notes, plus notes already delivered for today's episode (a manual re-run keeps them)."""
    rows = conn.execute(
        "SELECT id, text, url FROM notes WHERE (delivered_on IS NULL OR delivered_on = ?) AND for_date <= ? "
        "ORDER BY for_date, id LIMIT ?", (today.isoformat(), today.isoformat(), NOTES_PER_BRIEF)).fetchall()
    return [{"id": r["id"], "text": r["text"], "url": r["url"]} for r in rows]


def mark_delivered(conn, ids: list[int], episode_date: str) -> int:
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with _lock:
        return conn.execute(f"UPDATE notes SET delivered_on = ? WHERE delivered_on IS NULL AND id IN ({marks})",
                            (episode_date, *ids)).rowcount


def add_countdown(conn, label: str, day: date, now: datetime) -> int:
    label = _collapse(label)
    if not label:
        raise NoteError("give the countdown a label")
    if len(label) > LABEL_MAX:
        raise NoteError(f"a countdown label can be at most {LABEL_MAX} characters")
    if _CONTROL_RE.search(label):
        raise NoteError("the label contains control characters")
    if day < _today(now):
        raise NoteError("pick today or a later day")
    with _lock:
        return conn.execute("INSERT INTO countdowns (label, date, created_at) VALUES (?, ?, ?)",
                            (label, day.isoformat(), now.isoformat())).lastrowid


def list_countdowns(conn, today: date) -> list:
    return conn.execute("SELECT * FROM countdowns WHERE date >= ? ORDER BY date, id", (today.isoformat(),)).fetchall()


def delete_countdown(conn, countdown_id: int) -> bool:
    with _lock:
        return conn.execute("DELETE FROM countdowns WHERE id = ?", (countdown_id,)).rowcount > 0


def due_countdowns(conn, today: date) -> list[dict]:
    """Countdowns to mention today: at the milestone days, then every day for the last week."""
    due = []
    for row in list_countdowns(conn, today):
        days = (date.fromisoformat(row["date"]) - today).days
        if days in MILESTONES or 0 <= days <= DAILY_FROM:
            due.append({"label": row["label"], "days": days})
    return due


def housekeeping(conn, today: date) -> dict:
    """Drop countdowns whose day has passed and notes delivered more than KEEP_DELIVERED_DAYS ago."""
    cutoff = (today - timedelta(days=KEEP_DELIVERED_DAYS)).isoformat()
    with _lock:
        countdowns = conn.execute("DELETE FROM countdowns WHERE date < ?", (today.isoformat(),)).rowcount
        old = conn.execute("DELETE FROM notes WHERE delivered_on IS NOT NULL AND delivered_on < ?",
                           (cutoff,)).rowcount
    return {"countdowns": countdowns, "notes": old}
