"""The house's data: housemates, chores, to-dos, hosting, upkeep, notes and episodes.

All input validation lives here. Housemate text is data: it is checked for length and control characters, stored
as written, and escaped wherever it is shown.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .config import TZ

NAME_MAX, TEXT_MAX, TODO_MAX, TITLE_MAX, PREP_MAX, LABEL_MAX = 30, 300, 200, 80, 100, 60
PREP_ITEMS_MAX, EVERY_DAYS_MAX = 20, 3650
DATE_SPAN_DAYS = 3660  # about ten years either side of today
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_lock = threading.RLock()


class HouseError(ValueError):
    """Invalid house input from the UI."""


@contextmanager
def _transaction(conn):
    """BEGIN/COMMIT around a block, ROLLBACK if it raises. The connection is autocommit; hold _lock around this."""
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:  # no transaction is active; don't mask the original error
            pass
        raise


def _sane_date(day: date, today: date) -> None:
    """Refuse a date so far from today that date arithmetic downstream could overflow."""
    if abs((day - today).days) > DATE_SPAN_DAYS:
        raise HouseError("pick a date within ten years of today")


def _clean(value, what: str, limit: int) -> str:
    raw = str(value or "")
    if _CONTROL_RE.search(raw):
        raise HouseError(f"the {what} contains control characters")
    text = " ".join(raw.split())
    if not text:
        raise HouseError(f"write something for the {what}")
    if len(text) > limit:
        raise HouseError(f"the {what} can be at most {limit} characters")
    return text


# --- housemates ---------------------------------------------------------------------------------------------

def add_member(conn, name, now: datetime) -> int:
    name = _clean(name, "name", NAME_MAX)
    with _lock:
        taken = conn.execute("SELECT 1 FROM house_members WHERE lower(name) = lower(?) AND revoked_at IS NULL",
                             (name,)).fetchone()
        if taken:
            raise HouseError(f"there's already a {name} in the house")
        return conn.execute("INSERT INTO house_members (name, token, created_at) VALUES (?, ?, ?)",
                            (name, secrets.token_urlsafe(16), now.isoformat())).lastrowid


def list_members(conn) -> list:
    return conn.execute("SELECT * FROM house_members WHERE revoked_at IS NULL ORDER BY id").fetchall()


def member_names(conn) -> dict[int, str]:
    """Every housemate ever added, revoked ones included, so old notes and to-dos still show who wrote them."""
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM house_members")}


def revoke_member(conn, member_id: int, now: datetime) -> bool:
    with _lock, _transaction(conn):
        revoked = conn.execute("UPDATE house_members SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                               (now.isoformat(), member_id)).rowcount
        if not revoked:
            return False  # nothing was written, so the empty transaction commits harmlessly
        for row in conn.execute("SELECT id, rotation_json FROM house_chores").fetchall():
            rotation = json.loads(row["rotation_json"])
            if member_id in rotation:
                conn.execute("UPDATE house_chores SET rotation_json = ? WHERE id = ?",
                             (json.dumps([i for i in rotation if i != member_id]), row["id"]))
        return True


# --- chores -------------------------------------------------------------------------------------------------

def add_chore(conn, name, weekday, start_date: date, rotation: list[int], now: datetime) -> int:
    name = _clean(name, "chore name", LABEL_MAX)
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
        raise HouseError("pick a weekday")
    _sane_date(start_date, now.astimezone(TZ).date())
    rotation = list(dict.fromkeys(rotation))
    with _lock:
        active = {m["id"] for m in list_members(conn)}
        if any(i not in active for i in rotation):
            raise HouseError("pick housemates from the list")
        return conn.execute(
            "INSERT INTO house_chores (name, weekday, start_date, rotation_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, weekday, start_date.isoformat(), json.dumps(rotation), now.isoformat())).lastrowid


def list_chores(conn) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "weekday": r["weekday"],
             "start_date": date.fromisoformat(r["start_date"]), "rotation": json.loads(r["rotation_json"])}
            for r in conn.execute("SELECT * FROM house_chores ORDER BY id")]


def delete_chore(conn, chore_id: int) -> bool:
    with _lock:
        return conn.execute("DELETE FROM house_chores WHERE id = ?", (chore_id,)).rowcount > 0


def mark_chore_done(conn, chore_id: int, day: date, done_by: int | None, now: datetime) -> bool:
    with _lock:
        if not conn.execute("SELECT 1 FROM house_chores WHERE id = ?", (chore_id,)).fetchone():
            return False
        return conn.execute("INSERT OR IGNORE INTO house_chore_done (chore_id, day, done_by, done_at) "
                            "VALUES (?, ?, ?, ?)", (chore_id, day.isoformat(), done_by, now.isoformat())).rowcount > 0


def chore_is_done(conn, chore_id: int, day: date) -> bool:
    return conn.execute("SELECT 1 FROM house_chore_done WHERE chore_id = ? AND day = ?",
                        (chore_id, day.isoformat())).fetchone() is not None


# --- to-dos -------------------------------------------------------------------------------------------------

def add_todo(conn, text, added_by: int | None, due: date | None, now: datetime) -> int:
    text = _clean(text, "to-do", TODO_MAX)
    if due is not None:
        _sane_date(due, now.astimezone(TZ).date())
    with _lock:
        return conn.execute("INSERT INTO house_todos (text, added_by, due, created_at) VALUES (?, ?, ?, ?)",
                            (text, added_by, due.isoformat() if due else None, now.isoformat())).lastrowid


def list_todos(conn) -> list:
    return conn.execute("SELECT * FROM house_todos WHERE done_at IS NULL ORDER BY due IS NULL, due, id").fetchall()


def claim_todo(conn, todo_id: int, member_id: int | None) -> bool:
    with _lock:
        if member_id is not None and member_id not in {m["id"] for m in list_members(conn)}:
            raise HouseError("pick a housemate from the list")
        return conn.execute("UPDATE house_todos SET claimed_by = ? WHERE id = ? AND done_at IS NULL",
                            (member_id, todo_id)).rowcount > 0


def finish_todo(conn, todo_id: int, done_by: int | None, now: datetime) -> bool:
    with _lock:
        return conn.execute("UPDATE house_todos SET done_at = ?, done_by = ? WHERE id = ? AND done_at IS NULL",
                            (now.isoformat(), done_by, todo_id)).rowcount > 0


def delete_todo(conn, todo_id: int) -> bool:
    with _lock:
        return conn.execute("DELETE FROM house_todos WHERE id = ?", (todo_id,)).rowcount > 0


# --- notes --------------------------------------------------------------------------------------------------

def add_note(conn, text, member_id: int | None, now: datetime) -> int:
    text = _clean(text, "note", TEXT_MAX)
    with _lock:
        return conn.execute("INSERT INTO house_notes (member_id, text, created_at) VALUES (?, ?, ?)",
                            (member_id, text, now.isoformat())).lastrowid


def pending_notes(conn) -> list:
    return conn.execute("SELECT * FROM house_notes WHERE announced_on IS NULL ORDER BY id").fetchall()


def delete_note(conn, note_id: int) -> bool:
    with _lock:
        return conn.execute("DELETE FROM house_notes WHERE id = ? AND announced_on IS NULL", (note_id,)).rowcount > 0


# --- hosting ------------------------------------------------------------------------------------------------

def add_event(conn, title, day: date, host_id: int | None, prep: list[str], now: datetime, today: date) -> int:
    title = _clean(title, "title", TITLE_MAX)
    if day < today:
        raise HouseError("pick today or a later day")
    _sane_date(day, today)
    items = [_clean(p, "prep item", PREP_MAX) for p in prep if str(p or "").strip()]
    if len(items) > PREP_ITEMS_MAX:
        raise HouseError(f"at most {PREP_ITEMS_MAX} prep items")
    with _lock:
        if host_id is not None and host_id not in {m["id"] for m in list_members(conn)}:
            raise HouseError("pick a host from the list")
        with _transaction(conn):
            event_id = conn.execute("INSERT INTO house_events (title, day, host_id, created_at) VALUES (?, ?, ?, ?)",
                                    (title, day.isoformat(), host_id, now.isoformat())).lastrowid
            for position, text in enumerate(items):
                conn.execute("INSERT INTO house_prep (event_id, text, position) VALUES (?, ?, ?)",
                             (event_id, text, position))
        return event_id


def list_events(conn, today: date) -> list[dict]:
    events = []
    for r in conn.execute("SELECT * FROM house_events WHERE day >= ? ORDER BY day, id", (today.isoformat(),)):
        prep = conn.execute("SELECT id, text, done_at FROM house_prep WHERE event_id = ? ORDER BY position, id",
                            (r["id"],)).fetchall()
        events.append({"id": r["id"], "title": r["title"], "day": date.fromisoformat(r["day"]),
                       "host_id": r["host_id"],
                       "prep": [{"id": p["id"], "text": p["text"], "done": p["done_at"] is not None} for p in prep]})
    return events


def tick_prep(conn, prep_id: int, done_by: int | None, now: datetime) -> bool:
    with _lock:
        return conn.execute("UPDATE house_prep SET done_at = ?, done_by = ? WHERE id = ? AND done_at IS NULL",
                            (now.isoformat(), done_by, prep_id)).rowcount > 0


def delete_event(conn, event_id: int) -> bool:
    with _lock:
        return conn.execute("DELETE FROM house_events WHERE id = ?", (event_id,)).rowcount > 0


# --- upkeep -------------------------------------------------------------------------------------------------

def add_upkeep(conn, label, kind, now: datetime, today: date, *, every_days=None, month=None, day=None,
               last_done: date | None = None) -> int:
    label = _clean(label, "label", LABEL_MAX)
    if kind == "every":
        if isinstance(every_days, bool) or not isinstance(every_days, int) or not 1 <= every_days <= EVERY_DAYS_MAX:
            raise HouseError(f"every how many days? (1 to {EVERY_DAYS_MAX})")
        month = day = None
    elif kind == "yearly":
        try:
            date(2023, month, day)  # 2023 is not a leap year, so Feb 29 is refused
        except (TypeError, ValueError):
            raise HouseError("pick a month and day") from None
        every_days = None
    else:
        raise HouseError("pick how often")
    if last_done is not None and last_done > today:
        raise HouseError("last done can't be in the future")
    if last_done is not None:
        _sane_date(last_done, today)
    with _lock:
        return conn.execute(
            "INSERT INTO house_upkeep (label, kind, every_days, month, day, last_done, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (label, kind, every_days, month, day, last_done.isoformat() if last_done else None,
             now.isoformat())).lastrowid


def list_upkeep(conn) -> list[dict]:
    return [{"id": r["id"], "label": r["label"], "kind": r["kind"], "every_days": r["every_days"],
             "month": r["month"], "day": r["day"],
             "last_done": date.fromisoformat(r["last_done"]) if r["last_done"] else None,
             "created": datetime.fromisoformat(r["created_at"]).astimezone(TZ).date()}
            for r in conn.execute("SELECT * FROM house_upkeep ORDER BY id")]


def mark_upkeep_done(conn, upkeep_id: int, today: date) -> bool:
    with _lock:
        return conn.execute("UPDATE house_upkeep SET last_done = ? WHERE id = ?",
                            (today.isoformat(), upkeep_id)).rowcount > 0


def delete_upkeep(conn, upkeep_id: int) -> bool:
    with _lock:
        return conn.execute("DELETE FROM house_upkeep WHERE id = ?", (upkeep_id,)).rowcount > 0


# --- episodes, bookkeeping, retention -----------------------------------------------------------------------

KEEP_EPISODES = 14


def episode_path(house_dir: Path, day: date) -> Path:
    return house_dir / f"{day.isoformat()}.mp3"


def memo_path(house_dir: Path, memo_id: int) -> Path:
    return house_dir / "memos" / f"{memo_id}.wav"


def save_episode(conn, day: date, script: dict, *, duration_s: float, audio_bytes: int, word_count: int,
                 chapters, now: datetime) -> None:
    with _lock:
        conn.execute(
            "INSERT INTO house_episodes (date, script_json, duration_s, audio_bytes, word_count, chapters_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET script_json = excluded.script_json, duration_s = excluded.duration_s, "
            "audio_bytes = excluded.audio_bytes, word_count = excluded.word_count, "
            "chapters_json = excluded.chapters_json, updated_at = excluded.updated_at",
            (day.isoformat(), json.dumps(script), duration_s, audio_bytes, word_count,
             json.dumps(chapters) if chapters else None, now.isoformat(), now.isoformat()))


def get_episode(conn, day: date):
    return conn.execute("SELECT * FROM house_episodes WHERE date = ?", (day.isoformat(),)).fetchone()


def list_episodes(conn) -> list:
    return conn.execute("SELECT * FROM house_episodes ORDER BY date DESC").fetchall()


def _in_clause(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


def mark_announced(conn, facts: dict, today: date) -> None:
    """Record that an episode read these items. Only items not yet announced or played are changed."""
    iso = today.isoformat()
    with _lock, _transaction(conn):
        for table, key in (("house_notes", "notes"), ("house_todos", "todos")):
            ids = [item["id"] for item in facts[key]]
            if ids:
                conn.execute(f"UPDATE {table} SET announced_on = ? WHERE announced_on IS NULL "
                             f"AND id IN ({_in_clause(ids)})", (iso, *ids))
        memo_ids = [item["id"] for item in facts["memos"]]
        if memo_ids:
            conn.execute(f"UPDATE house_memos SET status = 'played', played_on = ? WHERE status = 'approved' "
                         f"AND id IN ({_in_clause(memo_ids)})", (iso, *memo_ids))


def reject_memo(conn, memo_id: int) -> bool:
    with _lock:
        return conn.execute("UPDATE house_memos SET status = 'rejected' WHERE id = ? "
                            "AND status IN ('pending', 'approved')", (memo_id,)).rowcount > 0


def prune(conn, house_dir: Path, today: date, keep: int = KEEP_EPISODES) -> dict:
    """Keep the newest episodes; delete the WAVs of memos played before today and of rejected memos.

    Rows are deleted in one transaction first and files only after it commits, so a failure never leaves a row
    whose MP3 is already gone.
    """
    with _lock:
        with _transaction(conn):
            old = [r["date"] for r in conn.execute(
                "SELECT date FROM house_episodes ORDER BY date DESC LIMIT -1 OFFSET ?", (keep,))]
            for day in old:
                conn.execute("DELETE FROM house_episodes WHERE date = ?", (day,))
        stale = [r["id"] for r in conn.execute(
            "SELECT id FROM house_memos WHERE (status = 'played' AND played_on < ?) OR status = 'rejected'",
            (today.isoformat(),))]
    for day in old:
        episode_path(house_dir, date.fromisoformat(day)).unlink(missing_ok=True)
    memo_files = 0
    for memo_id in stale:
        path = memo_path(house_dir, memo_id)
        if path.exists():
            path.unlink(missing_ok=True)
            memo_files += 1
    return {"episodes": len(old), "memo_files": memo_files}
