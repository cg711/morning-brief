"""POST /api/inbox: the phone's share-sheet Shortcut queues a deep dive (link or topic) with a Bearer token."""
from __future__ import annotations

import json
import secrets
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import deepdives, inbox, notes
from .config import TZ

ACTIVE_FOR_DUPLICATES = ("queued",) + deepdives.ACTIVE
BAD_BODY = 'Send JSON with an "input" string.'


def _reply(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"message": message, **extra}, status_code=status)


def make_router(*, settings, conn, clock) -> APIRouter:
    router = APIRouter()

    def _save_note(body: dict, typed: str) -> JSONResponse:
        if not (settings.worker_mode and settings.personal_segment):
            return _reply(409, "Notes are off: they need DAILY_BRIEF=worker and PERSONAL_SEGMENT=1.")
        raw = body.get("date", "")
        if not isinstance(raw, str):
            return _reply(422, "date must be YYYY-MM-DD.")
        now = clock()
        next_brief = notes.next_brief_date(now, settings.run_at)
        try:
            for_date = date.fromisoformat(raw.strip()) if raw.strip() else next_brief
        except ValueError:
            return _reply(422, "date must be YYYY-MM-DD.")
        text, url = inbox.parse_note(body["input"])
        text = " — ".join(part for part in (text, " ".join(typed.split())) if part)
        if len(text) > notes.NOTE_TEXT_MAX:
            cut = text[:notes.NOTE_TEXT_MAX - 1]
            if " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            text = cut.rstrip() + "…"
        try:
            note_id = notes.add_note(conn, text, url, for_date, now)
        except notes.NoteError as exc:
            return _reply(422, str(exc))
        when = notes.day_name(max(for_date, next_brief), now.astimezone(TZ).date())
        what = text if len(text) <= 80 else text[:79].rstrip() + "…"
        return _reply(201, f"Saved for {when}'s brief: {what or notes.host(url)}", id=note_id, kind="note")

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
        if position not in ("top", "end", "brief"):
            return _reply(422, 'position must be "top", "end" or "brief".')
        note = body.get("note", "").strip()
        # This duplicates add_topic's own NOTES_MAX check, on purpose, to give the phone a friendlier message.
        if len(note) > deepdives.NOTES_MAX:
            return _reply(422, f"The note is too long ({deepdives.NOTES_MAX} characters at most).")
        if position == "brief":
            return _save_note(body, note)

        topic, url = inbox.parse_input(body["input"])
        if not topic and not url:
            return _reply(422, "Nothing to add: share a link or some text.")

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
