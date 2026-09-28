"""POST /api/inbox: the phone's share-sheet Shortcut queues a deep dive (link or topic) with a Bearer token."""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import deepdives, inbox

ACTIVE_FOR_DUPLICATES = ("queued",) + deepdives.ACTIVE
BAD_BODY = 'Send JSON with an "input" string.'


def _reply(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"message": message, **extra}, status_code=status)


def make_router(*, settings, conn, clock) -> APIRouter:
    router = APIRouter()

    @router.post("/api/inbox")
    async def receive(request: Request):
        if not settings.inbox_token:
            return _reply(503, "The inbox is off: set INBOX_TOKEN.")
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {settings.inbox_token}".encode()):
            return _reply(401, "Wrong token.")
        try:
            body = json.loads(await request.body())
        except (ValueError, RecursionError):
            return _reply(422, BAD_BODY)
        if not isinstance(body, dict) or not isinstance(body.get("input"), str) \
                or not isinstance(body.get("note", ""), str):
            return _reply(422, BAD_BODY)
        position = body.get("position", "end")
        if position not in ("top", "end"):
            return _reply(422, 'position must be "top" or "end".')
        topic, url = inbox.parse_input(body["input"])
        if not topic and not url:
            return _reply(422, "Nothing to add: share a link or some text.")
        note = body.get("note", "").strip()
        # This duplicates add_topic's own NOTES_MAX check, on purpose, to give the phone a friendlier message.
        if len(note) > deepdives.NOTES_MAX:
            return _reply(422, f"The note is too long ({deepdives.NOTES_MAX} characters at most).")

        now = clock()
        with deepdives._write_lock:
            if url:
                marks = ",".join("?" * len(ACTIVE_FOR_DUPLICATES))
                existing = conn.execute(
                    f"SELECT id, topic FROM topics WHERE source_url = ? AND status IN ({marks}) ORDER BY id LIMIT 1",
                    (url, *ACTIVE_FOR_DUPLICATES),
                ).fetchone()
                if existing is not None:
                    return _reply(200, f"Already in your queue: {existing['topic']}", id=existing["id"],
                                  duplicate=True)
            try:
                topic_id = deepdives.add_topic(conn, topic, note, now, url=url, **deepdives.get_defaults(conn))
            except deepdives.TopicError as exc:
                return _reply(422, str(exc))
            stored = deepdives.get_topic(conn, topic_id)["topic"]
            if position == "top":
                deepdives.move_to_top(conn, topic_id, now)
                message = f"Queued at the top: {stored}"
            else:
                queued = [r["id"] for r in conn.execute(
                    "SELECT id FROM topics WHERE status = 'queued' ORDER BY position, created_at").fetchall()]
                n = queued.index(topic_id) + 1 if topic_id in queued else len(queued) + 1
                message = f"Queued, {inbox.ordinal(n)} in line: {stored}"
        return _reply(201, message, id=topic_id, duplicate=False)

    return router
