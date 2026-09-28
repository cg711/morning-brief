"""Deep-dive topic queue: states, the 3-ready rule, claims and housekeeping.

Lifecycle: queued → researching → speaking → ready → heard (→ deleted), with failed as a side branch.
Validation of submitted scripts lives at the bottom of this module (Task 2).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import db

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
URL_MAX = 2000
SUGGESTIONS_MAX, SUGGESTION_REASON_MAX = 3, 300
DEFAULT_KEYS = ("fact_check", "two_hosts")
_WHITESPACE_RE = re.compile(r"\s")

log = logging.getLogger(__name__)

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


def _clean_link(url: str | None) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    if (len(url) > URL_MAX or not url.lower().startswith(("http://", "https://")) or not host
            or _WHITESPACE_RE.search(url) or _has_control_chars(url)):
        raise TopicError("the link must be a full http(s) URL")
    return url


def _link_display(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    return host + parts.path.rstrip("/")


def add_topic(conn, topic: str, notes: str, now: datetime, *, url: str | None = None,
              fact_check: bool = False, two_hosts: bool = False) -> int:
    topic, notes = topic.strip(), notes.strip()
    url = _clean_link(url)
    if not topic and url:
        topic = f"From link: {_link_display(url)}"[:TOPIC_MAX]
    if not topic:
        raise TopicError("enter a topic or a link")
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
            "INSERT INTO topics (topic, notes, position, status, created_at, updated_at, source_url, fact_check, "
            "two_hosts) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
            (topic, notes, position, _iso(now), _iso(now), url, int(fact_check), int(two_hosts)),
        )
        return cur.lastrowid


def get_defaults(conn) -> dict[str, bool]:
    """The starting state of the per-topic checkboxes (also used for follow-ups and suggestions)."""
    return {key: db.get_state(conn, f"default_{key}") == "1" for key in DEFAULT_KEYS}


def set_default(conn, key: str, value: bool) -> None:
    if key not in DEFAULT_KEYS:
        raise ValueError(f"unknown default {key!r}")
    with _write_lock:
        db.set_state(conn, f"default_{key}", "1" if value else "0")


FOLLOW_UP_TITLE_MAX, FOLLOW_UP_HEADING_MAX = 120, 150


def follow_up_notes(title: str, heading: str, text: str) -> str:
    """Worker notes for a Go deeper topic, capped at NOTES_MAX with the excerpt cut at a word boundary."""
    head = (f'Follow-up to "{title[:FOLLOW_UP_TITLE_MAX]}": go deeper on "{heading[:FOLLOW_UP_HEADING_MAX]}". '
            "The earlier episode already covered: ")
    tail_plain = ". Skip that overview and go further."
    tail_punct = " Skip that overview and go further."
    excerpt = text.strip().rstrip(" .")
    room = NOTES_MAX - len(head) - max(len(tail_plain), len(tail_punct))
    if len(excerpt) > room:
        cut = excerpt[:room - 1]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        excerpt = cut.rstrip(" ,;:.") + "…"
    tail = tail_punct if excerpt.endswith(("…", "?", "!")) else tail_plain
    return head + excerpt + tail


def add_follow_up(conn, parent_id: int, section_index: int, now: datetime) -> int | None:
    """Queue a deeper look at one section of a ready/heard episode, at the top of the queue.
    None if the parent or section is invalid, or that section already has a follow-up."""
    with _write_lock:
        parent = conn.execute(
            "SELECT t.status, t.script_json, d.title FROM topics t JOIN deep_dives d ON d.topic_id = t.id "
            "WHERE t.id = ?", (parent_id,)
        ).fetchone()
        if parent is None or parent["status"] not in ("ready", "heard"):
            return None
        sections = json.loads(parent["script_json"])["sections"]
        if not 0 <= section_index < len(sections):
            return None
        if conn.execute("SELECT 1 FROM topics WHERE parent_topic_id = ? AND parent_section = ?",
                        (parent_id, section_index)).fetchone():
            return None
        section = sections[section_index]
        top = conn.execute("SELECT COALESCE(MIN(position), 1) - 1 AS p FROM topics").fetchone()["p"]
        flags = get_defaults(conn)
        cur = conn.execute(
            "INSERT INTO topics (topic, notes, position, status, created_at, updated_at, parent_topic_id, "
            "parent_section, fact_check, two_hosts) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (section["heading"][:TOPIC_MAX], follow_up_notes(parent["title"], section["heading"], section_text(section)),
             top, _iso(now), _iso(now), parent_id, section_index, int(flags["fact_check"]),
             int(flags["two_hosts"])),
        )
        return cur.lastrowid


def get_topic(conn, topic_id: int):
    return conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()


def clean_suggestions(raw) -> list[dict]:
    """The worker's suggested next topics, cleaned. Invalid entries are dropped (never fail a script)."""
    if not isinstance(raw, list):
        return []
    kept, dropped = [], 0
    for item in raw:
        topic = item.get("topic") if isinstance(item, dict) else None
        reason = item.get("reason", "") if isinstance(item, dict) else ""
        if (not isinstance(topic, str) or not topic.strip() or len(topic.strip()) > TOPIC_MAX
                or _has_control_chars(topic) or not isinstance(reason, str) or _has_control_chars(reason)):
            dropped += 1
            continue
        if len(kept) < SUGGESTIONS_MAX:
            kept.append({"topic": topic.strip(), "reason": reason.strip()[:SUGGESTION_REASON_MAX]})
    if dropped:
        log.info("dropped %d invalid suggestion(s)", dropped)
    return kept


def _store_suggestions(conn, topic_id: int, suggestions: list[dict], now: datetime) -> None:
    """Insert new suggestions, skipping any that match (case-insensitively) a topic or a live suggestion."""
    taken = {r[0].strip().casefold() for r in conn.execute(
        "SELECT topic FROM topics UNION ALL SELECT topic FROM suggestions WHERE status IN ('new', 'added')")}
    for s in suggestions:
        key = s["topic"].casefold()
        if key in taken:
            continue
        taken.add(key)
        conn.execute(
            "INSERT INTO suggestions (topic, reason, from_topic_id, status, created_at) VALUES (?, ?, ?, 'new', ?)",
            (s["topic"], s["reason"], topic_id, _iso(now)))


def list_suggestions(conn, limit: int = 6) -> list:
    return conn.execute(
        "SELECT s.id, s.topic, s.reason, d.title AS from_title FROM suggestions s "
        "LEFT JOIN deep_dives d ON d.topic_id = s.from_topic_id "
        "WHERE s.status = 'new' ORDER BY s.id DESC LIMIT ?", (limit,)
    ).fetchall()


def add_suggestion(conn, suggestion_id: int, now: datetime) -> int | None:
    """Queue a suggested topic at the end of the queue with the page defaults. None if it isn't 'new'."""
    with _write_lock:
        row = conn.execute("SELECT topic, reason FROM suggestions WHERE id = ? AND status = 'new'",
                           (suggestion_id,)).fetchone()
        if row is None:
            return None
        topic_id = add_topic(conn, row["topic"], row["reason"], now, **get_defaults(conn))
        conn.execute("UPDATE suggestions SET status = 'added' WHERE id = ?", (suggestion_id,))
        return topic_id


def dismiss_suggestion(conn, suggestion_id: int) -> bool:
    with _write_lock:
        return conn.execute("UPDATE suggestions SET status = 'dismissed' WHERE id = ? AND status = 'new'",
                            (suggestion_id,)).rowcount > 0


def list_topics(conn) -> list:
    return conn.execute(
        """SELECT t.*, d.title AS episode_title, d.word_count, d.duration_s, d.audio_bytes,
                  d.published_at, d.heard_at, d.updated_at AS episode_updated_at, d.chapters_json,
                  p.title AS parent_title
           FROM topics t LEFT JOIN deep_dives d ON d.topic_id = t.id
                         LEFT JOIN deep_dives p ON p.topic_id = t.parent_topic_id
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
        conn.execute("DELETE FROM shares WHERE topic_id = ?", (topic_id,))
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
            _store_suggestions(conn, topic_id, clean_suggestions(script.get("suggestions")), now)
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
            now: datetime, chapters: list[tuple[str, float]] | None = None) -> None:
    chapters_json = json.dumps(chapters) if chapters else None
    with _write_lock:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO deep_dives "
                "(topic_id, title, word_count, duration_s, audio_bytes, published_at, heard_at, updated_at, "
                "chapters_json) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (topic_id, title, word_count, duration_s, audio_bytes, _iso(now), _iso(now), chapters_json),
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
    """Auto-heard after 7 days, delete heard after 30 (unless shared), release stale claims, remove stray temp files."""
    auto_heard = 0
    for row in conn.execute(
        "SELECT t.id FROM topics t JOIN deep_dives d ON d.topic_id = t.id "
        "WHERE t.status = 'ready' AND d.published_at < ?", (_iso(now - AUTO_HEARD_AFTER),)
    ).fetchall():
        auto_heard += mark_heard(conn, row["id"], now)
    old = [r["id"] for r in conn.execute(
        "SELECT t.id FROM topics t JOIN deep_dives d ON d.topic_id = t.id "
        "WHERE t.status = 'heard' AND d.heard_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM shares s WHERE s.topic_id = t.id AND s.revoked_at IS NULL)",
        (_iso(now - KEEP_HEARD_FOR),)
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

SPEAKERS = ("host", "cohost")
MAX_LINES = 80
FACT_CHECK_KEYS = ("claims_checked", "corrected", "removed")


def section_text(section: dict) -> str:
    """The spoken words of a section, whichever format (text, or two-host lines) it uses."""
    lines = section.get("lines")
    if isinstance(lines, list):
        return " ".join(line["text"] for line in lines if isinstance(line, dict) and isinstance(line.get("text"), str))
    text = section.get("text")
    return text if isinstance(text, str) else ""


def script_word_count(script: dict) -> int:
    sections = script.get("sections")
    sections = sections if isinstance(sections, list) else []
    texts = [script.get("intro"), *(section_text(s) for s in sections if isinstance(s, dict)),
             script.get("outro")]
    return sum(len(t.split()) for t in texts if isinstance(t, str))


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _line_problems(n: int, lines, seen: set) -> list[str]:
    if not isinstance(lines, list) or not lines:
        return [f"section {n} 'lines' must be a non-empty list"]
    problems = []
    if len(lines) > MAX_LINES:
        problems.append(f"section {n} has {len(lines)} lines; at most {MAX_LINES}")
    for m, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            problems.append(f"section {n} line {m} must be an object")
            continue
        if line.get("speaker") not in SPEAKERS:
            problems.append(f"section {n} line {m} speaker must be 'host' or 'cohost'")
        else:
            seen.add(line["speaker"])
        text = line.get("text")
        if not _nonempty(text):
            problems.append(f"section {n} line {m} needs non-empty 'text'")
        elif _has_control_chars(text):
            problems.append(f"section {n} line {m} text contains control characters")
    return problems


def _fact_check_problems(summary) -> list[str]:
    if not isinstance(summary, dict):
        return ["'fact_check' must be an object with claims_checked, corrected and removed "
                 "(is the worker prompt up to date?)"]
    values = [summary.get(k) for k in FACT_CHECK_KEYS]
    if any(not isinstance(v, int) or isinstance(v, bool) for v in values):
        return ["'fact_check' needs whole-number claims_checked, corrected and removed"]
    checked, corrected, removed = values
    if checked < 1 or corrected < 0 or removed < 0 or corrected + removed > checked:
        return ["'fact_check' numbers are inconsistent (claims_checked >= 1, corrected + removed <= claims_checked)"]
    return []


def validate_script(script, *, two_hosts: bool = False, fact_check: bool = False) -> list[str]:
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
    speakers_seen: set = set()
    for n, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            problems.append(f"section {n} must be an object")
            continue
        heading = section.get("heading")
        if not _nonempty(heading):
            problems.append(f"section {n} needs a non-empty 'heading'")
        elif _has_control_chars(heading):
            problems.append(f"section {n} heading contains control characters")
        if two_hosts:
            if "text" in section or "lines" not in section:
                problems.append(f"section {n} must use 'lines' (this topic has two hosts), not 'text' "
                                 "(is the worker prompt up to date?)")
            else:
                problems += _line_problems(n, section["lines"], speakers_seen)
        elif "lines" in section:
            problems.append(f"section {n} uses 'lines' but this topic has one host; use 'text'")
        else:
            text = section.get("text")
            if not _nonempty(text):
                problems.append(f"section {n} needs a non-empty 'text'")
            elif _has_control_chars(text):
                problems.append(f"section {n} text contains control characters")
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
    if two_hosts and sections and speakers_seen and speakers_seen != set(SPEAKERS):
        problems.append("a two-host script needs both 'host' and 'cohost' lines")
    if fact_check:
        problems += _fact_check_problems(script.get("fact_check"))
    return problems


def passages(script: dict, *, host_voice: str | None = None, cohost_voice: str | None = None) -> list:
    """What to speak, in order: title line, intro, one passage per section, outro. A two-host section is a
    list of (voice, text) lines: the host reads the heading, then the dialogue."""
    voices = {"host": host_voice, "cohost": cohost_voice}
    out: list = [f"Morning Brief deep dive: {script['title']}.", script["intro"]]
    for s in script["sections"]:
        if "lines" in s:
            out.append([(host_voice, f"{s['heading']}.")] + [(voices[l["speaker"]], l["text"]) for l in s["lines"]])
        else:
            out.append(f"{s['heading']}. {s['text']}")
    out.append(script["outro"])
    return out


def chapters(script: dict, starts: list[float]) -> list[tuple[str, float]]:
    """Chapter marks for passages() output: title line, intro, one per section, outro."""
    marks = [("Introduction", 0.0)]
    marks += [(s["heading"], starts[2 + n]) for n, s in enumerate(script["sections"])]
    marks.append(("Wrap-up", starts[-1]))
    return marks
