"""Share links: at most one live public link per ready/heard deep dive, revocable, never expiring."""
from __future__ import annotations

import secrets
from datetime import datetime

from . import deepdives

SHAREABLE = ("ready", "heard")
TOKEN_MAX = 64


def live_token(conn, topic_id: int) -> str | None:
    row = conn.execute("SELECT token FROM shares WHERE topic_id = ? AND revoked_at IS NULL",
                       (topic_id,)).fetchone()
    return row["token"] if row else None


def live_tokens(conn) -> dict[int, str]:
    rows = conn.execute("SELECT topic_id, token FROM shares WHERE revoked_at IS NULL").fetchall()
    return {r["topic_id"]: r["token"] for r in rows}


def create_or_get(conn, topic_id: int, now: datetime) -> str | None:
    """The topic's live token, creating one if needed. None if the topic isn't ready or heard."""
    # deepdives' lock also serializes delete_topic, so a share can't be inserted for a topic mid-delete.
    with deepdives._write_lock:
        token = live_token(conn, topic_id)
        if token is not None:
            return token
        topic = conn.execute("SELECT status FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if topic is None or topic["status"] not in SHAREABLE:
            return None
        token = secrets.token_urlsafe(16)
        conn.execute("INSERT INTO shares (topic_id, token, created_at) VALUES (?, ?, ?)",
                     (topic_id, token, now.isoformat()))
        return token


def revoke(conn, topic_id: int, now: datetime) -> bool:
    with deepdives._write_lock:
        return conn.execute("UPDATE shares SET revoked_at = ? WHERE topic_id = ? AND revoked_at IS NULL",
                            (now.isoformat(), topic_id)).rowcount > 0


def resolve(conn, token: str):
    """The shared topic's row (list_topics columns) if the link is live and the episode is ready or heard."""
    if not token or len(token) > TOKEN_MAX:
        return None
    return conn.execute(
        """SELECT t.*, d.title AS episode_title, d.word_count, d.duration_s, d.audio_bytes,
                  d.published_at, d.heard_at, d.updated_at AS episode_updated_at, d.chapters_json,
                  NULL AS parent_title
           FROM shares s JOIN topics t ON t.id = s.topic_id JOIN deep_dives d ON d.topic_id = t.id
           WHERE s.token = ? AND s.revoked_at IS NULL AND t.status IN ('ready', 'heard')""",
        (token,),
    ).fetchone()
