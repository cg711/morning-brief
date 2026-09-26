"""Deep-dive topic queue: states, the 3-ready rule, claims and housekeeping.

Lifecycle: queued → researching → speaking → ready → heard (→ deleted), with failed as a side branch.
Validation of submitted scripts lives at the bottom of this module (Task 2).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

MAX_ACTIVE = 3
ACTIVE = ("researching", "speaking", "ready")
CLAIM_TTL = timedelta(hours=3)
AUTO_HEARD_AFTER = timedelta(days=7)
KEEP_HEARD_FOR = timedelta(days=30)
STRAY_TMP_AGE = timedelta(days=1)
WORKER_STALE = timedelta(hours=2)
RESEARCH_LONG = timedelta(minutes=60)   # measured from claimed_at; normal is about 5 min
SPEAKING_LONG = timedelta(minutes=40)   # measured from updated_at (script accepted / speech started); normal ~11 min
TOPIC_MAX, NOTES_MAX, REASON_MAX = 200, 500, 500

# The app shares one SQLite connection across request threads; serialize multi-statement writes.
_write_lock = threading.RLock()


class TopicError(ValueError):
    """Invalid topic input from the UI."""


# C0 controls (excluding \t \n \r) and DEL, plus lone surrogates: both break RSS/HTML downstream.
_CONTROL_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")


def _has_control_chars(value) -> bool:
    return isinstance(value, str) and bool(_CONTROL_CHARS_RE.search(value))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def audio_path(directory: Path, topic_id: int) -> Path:
    return directory / f"{topic_id}.mp3"


def add_topic(conn, topic: str, notes: str, now: datetime) -> int:
    topic, notes = topic.strip(), notes.strip()
    if not topic:
        raise TopicError("topic is required")
    if len(topic) > TOPIC_MAX:
        raise TopicError(f"topic must be at most {TOPIC_MAX} characters")
    if len(notes) > NOTES_MAX:
        raise TopicError(f"notes must be at most {NOTES_MAX} characters")
    if _has_control_chars(topic):
        raise TopicError("topic contains control characters")
    if _has_control_chars(notes):
        raise TopicError("notes contains control characters")
    with _write_lock:
        position = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM topics").fetchone()["p"]
        cur = conn.execute(
            "INSERT INTO topics (topic, notes, position, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (topic, notes, position, _iso(now), _iso(now)),
        )
        return cur.lastrowid


def get_topic(conn, topic_id: int):
    return conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()


def list_topics(conn) -> list:
    return conn.execute(
        """SELECT t.*, d.title AS episode_title, d.word_count, d.duration_s, d.audio_bytes,
                  d.published_at, d.heard_at, d.updated_at AS episode_updated_at
           FROM topics t LEFT JOIN deep_dives d ON d.topic_id = t.id
           ORDER BY t.position, t.created_at"""
    ).fetchall()


def move_to_top(conn, topic_id: int, now: datetime) -> bool:
    with _write_lock:
        top = conn.execute("SELECT COALESCE(MIN(position), 1) - 1 AS p FROM topics").fetchone()["p"]
        cur = conn.execute(
            "UPDATE topics SET position = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
            (top, _iso(now), topic_id),
        )
        return cur.rowcount > 0


def delete_topic(conn, directory: Path, topic_id: int) -> bool:
    with _write_lock:
        existed = conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,)).rowcount > 0
    audio_path(directory, topic_id).unlink(missing_ok=True)
    return existed


def active_count(conn) -> int:
    marks = ",".join("?" * len(ACTIVE))
    return conn.execute(f"SELECT COUNT(*) AS n FROM topics WHERE status IN ({marks})", ACTIVE).fetchone()["n"]


def release_expired_claims(conn, now: datetime) -> int:
    with _write_lock:
        return conn.execute(
            "UPDATE topics SET status = 'queued', claimed_at = NULL, updated_at = ? "
            "WHERE status = 'researching' AND claimed_at < ?",
            (_iso(now), _iso(now - CLAIM_TTL)),
        ).rowcount


def claim(conn, now: datetime):
    """Hand the top queued topic to the worker if fewer than MAX_ACTIVE are researching/speaking/ready."""
    with _write_lock:
        release_expired_claims(conn, now)
        if active_count(conn) >= MAX_ACTIVE:
            return None
        row = conn.execute(
            "SELECT id FROM topics WHERE status = 'queued' ORDER BY position, created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE topics SET status = 'researching', claimed_at = ?, error = NULL, long_notified = NULL, "
            "updated_at = ? WHERE id = ?",
            (_iso(now), _iso(now), row["id"]),
        )
        return get_topic(conn, row["id"])


def accept_script(conn, topic_id: int, script: dict, now: datetime) -> bool:
    """Move a researching topic to speaking and store its sources. False (no change) if it's not researching
    any more — deleted, or its claim expired — checked under the same transaction as the update."""
    with _write_lock:
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                "UPDATE topics SET status = 'speaking', script_json = ?, error = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'researching'",
                (json.dumps(script), _iso(now), topic_id),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return False
            conn.execute("DELETE FROM deep_dive_sources WHERE topic_id = ?", (topic_id,))
            conn.executemany(
                "INSERT INTO deep_dive_sources (topic_id, source_id, title, publisher, url) VALUES (?, ?, ?, ?, ?)",
                [(topic_id, s["id"], s["title"], s.get("publisher") or "", s["url"]) for s in script["sources"]],
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise


def fail(conn, topic_id: int, reason: str, now: datetime, *, from_status: str | None = None) -> bool:
    """Mark a topic failed. With from_status, only a topic currently in that status is changed;
    returns whether a row actually changed."""
    with _write_lock:
        sql = "UPDATE topics SET status = 'failed', error = ?, claimed_at = NULL, updated_at = ? WHERE id = ?"
        params = [reason[:REASON_MAX], _iso(now), topic_id]
        if from_status is not None:
            sql += " AND status = ?"
            params.append(from_status)
        return conn.execute(sql, params).rowcount > 0


def retry(conn, topic_id: int, now: datetime) -> str | None:
    """failed → speaking (a stored script is re-spoken) or queued (research again). Returns the new status."""
    with _write_lock:
        row = get_topic(conn, topic_id)
        if row is None or row["status"] != "failed":
            return None
        status = "speaking" if row["script_json"] else "queued"
        conn.execute(
            "UPDATE topics SET status = ?, error = NULL, render_attempts = 0, long_notified = NULL, "
            "updated_at = ? WHERE id = ?",
            (status, _iso(now), topic_id))
        return status


def begin_render(conn, topic_id: int, now: datetime) -> bool:
    """Record that a render attempt is starting for a speaking topic. Returns False if it's not speaking
    (deleted, or moved on) any more."""
    with _write_lock:
        cur = conn.execute(
            "UPDATE topics SET render_attempts = render_attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = 'speaking'",
            (_iso(now), topic_id),
        )
        return cur.rowcount > 0


def publish(conn, topic_id: int, *, title: str, word_count: int, duration_s: float, audio_bytes: int,
            now: datetime) -> None:
    with _write_lock:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO deep_dives "
                "(topic_id, title, word_count, duration_s, audio_bytes, published_at, heard_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (topic_id, title, word_count, duration_s, audio_bytes, _iso(now), _iso(now)),
            )
            conn.execute("UPDATE topics SET status = 'ready', updated_at = ? WHERE id = ?", (_iso(now), topic_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_heard(conn, topic_id: int, now: datetime) -> bool:
    with _write_lock:
        row = get_topic(conn, topic_id)
        if row is None or row["status"] != "ready":
            return False
        conn.execute("UPDATE deep_dives SET heard_at = ? WHERE topic_id = ?", (_iso(now), topic_id))
        conn.execute("UPDATE topics SET status = 'heard', updated_at = ? WHERE id = ?", (_iso(now), topic_id))
        return True


def speaking_ids(conn) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM topics WHERE status = 'speaking' ORDER BY updated_at")]


def sources_for(conn, topic_id: int) -> list:
    return conn.execute(
        "SELECT * FROM deep_dive_sources WHERE topic_id = ? ORDER BY rowid", (topic_id,)
    ).fetchall()


def worker_status(last_seen: str | None, now: datetime) -> dict:
    """The 'Mac last checked in …' line. Stale (amber) once the hourly worker has been silent over 2 hours."""
    if last_seen is None:
        return {"text": "Mac worker hasn't checked in yet", "stale": True}
    age = now - datetime.fromisoformat(last_seen)
    minutes = max(0, int(age.total_seconds() // 60))
    if minutes < 1:
        ago = "just now"
    elif minutes < 90:
        ago = f"{minutes} min ago"
    elif minutes < 48 * 60:
        ago = f"{int(minutes / 60 + 0.5)} h ago"
    else:
        ago = f"{minutes // (24 * 60)} days ago"
    return {"text": f"Mac last checked in {ago}", "stale": age > WORKER_STALE}


def stage_minutes(row, now: datetime) -> int:
    started = row["claimed_at"] if row["status"] == "researching" else row["updated_at"]
    return max(0, int((now - datetime.fromisoformat(started)).total_seconds() // 60))


def long_running(conn, now: datetime) -> list:
    """Topics researching or speaking well past their normal time. Feeds the UI label and the push job."""
    return conn.execute(
        "SELECT * FROM topics WHERE (status = 'researching' AND claimed_at < ?) "
        "OR (status = 'speaking' AND updated_at < ?) ORDER BY id",
        (_iso(now - RESEARCH_LONG), _iso(now - SPEAKING_LONG)),
    ).fetchall()


def mark_long_notified(conn, topic_id: int, status: str) -> bool:
    """Record that a 'running long' push went out for this stage. False if already recorded, or the topic
    isn't in that status any more."""
    with _write_lock:
        return conn.execute(
            "UPDATE topics SET long_notified = ? WHERE id = ? AND status = ? AND long_notified IS NOT ?",
            (status, topic_id, status, status),
        ).rowcount > 0


def housekeeping(conn, directory: Path, now: datetime) -> dict:
    """Auto-heard after 7 days, delete heard after 30, release stale claims, remove stray temp files."""
    auto_heard = 0
    for row in conn.execute(
        "SELECT t.id FROM topics t JOIN deep_dives d ON d.topic_id = t.id "
        "WHERE t.status = 'ready' AND d.published_at < ?", (_iso(now - AUTO_HEARD_AFTER),)
    ).fetchall():
        auto_heard += mark_heard(conn, row["id"], now)
    old = [r["id"] for r in conn.execute(
        "SELECT t.id FROM topics t JOIN deep_dives d ON d.topic_id = t.id "
        "WHERE t.status = 'heard' AND d.heard_at < ?", (_iso(now - KEEP_HEARD_FOR),)
    ).fetchall()]
    for topic_id in old:
        delete_topic(conn, directory, topic_id)
    released = release_expired_claims(conn, now)
    stray = 0
    if directory.exists():
        for tmp in directory.glob("*.tmp"):
            if datetime.fromtimestamp(tmp.stat().st_mtime, tz=now.tzinfo) < now - STRAY_TMP_AGE:
                tmp.unlink(missing_ok=True)
                stray += 1
    return {"auto_heard": auto_heard, "deleted": len(old), "released": released, "stray": stray}


# --- script validation (worker submissions) ---------------------------------

MIN_WORDS, MAX_WORDS = 2000, 3000
MIN_SECTIONS, MAX_SECTIONS = 3, 12
TITLE_MAX = 120


def script_word_count(script: dict) -> int:
    sections = script.get("sections")
    sections = sections if isinstance(sections, list) else []
    texts = [script.get("intro"), *(s.get("text") for s in sections if isinstance(s, dict)),
             script.get("outro")]
    return sum(len(t.split()) for t in texts if isinstance(t, str))


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_script(script) -> list[str]:
    if not isinstance(script, dict):
        return ["the body must be a JSON object"]
    problems = []
    for key in ("title", "intro", "outro"):
        value = script.get(key)
        if not _nonempty(value):
            problems.append(f"'{key}' must be a non-empty string")
        elif _has_control_chars(value):
            problems.append(f"'{key}' contains control characters")
    if isinstance(script.get("title"), str) and len(script["title"]) > TITLE_MAX:
        problems.append(f"'title' must be at most {TITLE_MAX} characters")

    sources = script.get("sources")
    if not isinstance(sources, list) or not sources:
        problems.append("'sources' must be a non-empty list")
        sources = []
    ids: list[str] = []
    for n, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            problems.append(f"source {n} must be an object")
            continue
        source_id = source.get("id")
        if not _nonempty(source_id):
            problems.append(f"source {n} needs an 'id'")
        elif source_id in ids:
            problems.append(f"source id '{source_id}' is used more than once")
        else:
            ids.append(source_id)
        if _has_control_chars(source_id):
            problems.append(f"source {n} id contains control characters")
        url = source.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(f"source {n} needs an http(s) 'url'")
        elif _has_control_chars(url):
            problems.append(f"source {n} url contains control characters")
        title = source.get("title")
        if not _nonempty(title):
            problems.append(f"source {n} needs a 'title'")
        elif _has_control_chars(title):
            problems.append(f"source {n} title contains control characters")
        if _has_control_chars(source.get("publisher")):
            problems.append(f"source {n} publisher contains control characters")

    sections = script.get("sections")
    if not isinstance(sections, list):
        problems.append("'sections' must be a list")
        sections = []
    elif not MIN_SECTIONS <= len(sections) <= MAX_SECTIONS:
        problems.append(f"there are {len(sections)} sections; there must be {MIN_SECTIONS} to {MAX_SECTIONS}")
    for n, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            problems.append(f"section {n} must be an object")
            continue
        for key in ("heading", "text"):
            value = section.get(key)
            if not _nonempty(value):
                problems.append(f"section {n} needs a non-empty '{key}'")
            elif _has_control_chars(value):
                problems.append(f"section {n} {key} contains control characters")
        cited = section.get("source_ids")
        if not isinstance(cited, list) or not cited:
            problems.append(f"section {n} must cite at least one source")
        else:
            unknown = [c for c in cited if c not in ids]
            if unknown:
                problems.append(f"section {n} cites unknown source ids {unknown}")

    words = script_word_count(script)
    if not MIN_WORDS <= words <= MAX_WORDS:
        problems.append(f"the script is {words} words; it must be between {MIN_WORDS} and {MAX_WORDS}")
    return problems


def passages(script: dict) -> list[str]:
    return [
        f"Morning Brief deep dive: {script['title']}.",
        script["intro"],
        *(f"{s['heading']}. {s['text']}" for s in script["sections"]),
        script["outro"],
    ]
