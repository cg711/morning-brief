"""The "Notes & countdowns" card: notes for a coming brief and countdowns for Your morning."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request

from . import notes
from .config import TZ


def make_router(*, settings, conn, templates, clock, check_hx_request) -> APIRouter:
    router = APIRouter()

    def _check_on() -> None:
        if not (settings.worker_mode and settings.personal_segment):
            raise HTTPException(404)

    def view(error: str | None = None) -> dict:
        now = clock()
        today = now.astimezone(TZ).date()
        next_brief = notes.next_brief_date(now, settings.run_at)
        pending = [{"id": r["id"], "text": r["text"], "url": r["url"], "host": notes.host(r["url"]),
                    "when": notes.day_name(max(date.fromisoformat(r["for_date"]), next_brief), today)}
                   for r in notes.pending_notes(conn)]
        countdowns = []
        for r in notes.list_countdowns(conn, today):
            day = date.fromisoformat(r["date"])
            days = (day - today).days
            countdowns.append({"id": r["id"], "label": r["label"], "date": f"{day:%b} {day.day}",
                               "in": "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"})
        return {"enabled": settings.worker_mode and settings.personal_segment, "notes": pending,
                "countdowns": countdowns, "next_brief": next_brief.isoformat(), "today": today.isoformat(),
                "error": error}

    def render(request: Request, error: str | None = None):
        return templates.TemplateResponse(request, "partials/notes.html", {"nc": view(error)})

    def parse_day(raw, default: date | None) -> date:
        raw = str(raw or "").strip()
        if not raw:
            if default is None:
                raise notes.NoteError("pick a date")
            return default
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise notes.NoteError("pick a date") from None

    @router.post("/notes")
    async def add_note(request: Request):
        check_hx_request(request)
        _check_on()
        form = await request.form()
        now = clock()
        try:
            day = parse_day(form.get("date"), notes.next_brief_date(now, settings.run_at))
            notes.add_note(conn, str(form.get("text", "")), str(form.get("url", "")), day, now)
        except notes.NoteError as exc:
            return render(request, str(exc))
        return render(request)

    @router.delete("/notes/{note_id}")
    def delete_note(note_id: int, request: Request):
        check_hx_request(request)
        notes.delete_note(conn, note_id)
        return render(request)

    @router.post("/countdowns")
    async def add_countdown(request: Request):
        check_hx_request(request)
        _check_on()
        form = await request.form()
        try:
            day = parse_day(form.get("date"), None)
            notes.add_countdown(conn, str(form.get("label", "")), day, clock())
        except notes.NoteError as exc:
            return render(request, str(exc))
        return render(request)

    @router.delete("/countdowns/{countdown_id}")
    def delete_countdown(countdown_id: int, request: Request):
        check_hx_request(request)
        notes.delete_countdown(conn, countdown_id)
        return render(request)

    router.view = view
    return router
