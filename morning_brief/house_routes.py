"""The owner's House card and the owner's House feed (tailnet only; the login and feed token protect them)."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path as PathParam, Request, Response
from fastapi.responses import FileResponse

from . import db, house, house_facts, house_feed, house_script, podcast
from .config import TZ

PKG = Path(__file__).resolve().parent
Id = Annotated[int, PathParam(ge=1, le=2**63 - 1)]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _int(raw, what: str) -> int:
    try:
        return int(str(raw or "").strip())
    except ValueError:
        raise house.HouseError(f"pick a {what}") from None


def _date(raw, what: str, *, required: bool = True) -> date | None:
    raw = str(raw or "").strip()
    if not raw:
        if required:
            raise house.HouseError(f"pick a {what}")
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise house.HouseError(f"pick a {what}") from None


def make_router(*, settings, conn, templates, clock, check_hx_request, check_token, start_house) -> APIRouter:
    router = APIRouter()

    def on() -> None:
        if not settings.house_brief:
            raise HTTPException(404)

    def short(day: date) -> str:
        return f"{day:%b} {day.day}"

    def view(error: str | None = None, notice: str | None = None) -> dict:
        if not settings.house_brief:
            return {"enabled": False}
        now = clock()
        today = now.astimezone(TZ).date()
        names = house.member_names(conn)
        chores = []
        for chore in house.list_chores(conn):
            nxt = house_facts.next_occurrence(chore, today)
            who = house_facts.assignee(chore, nxt)
            open_day = house_facts.occurrence_in_window(chore, today)
            if open_day is not None and house.chore_is_done(conn, chore["id"], open_day):
                open_day = None
            offset = (open_day - today).days if open_day else 0
            chores.append({"id": chore["id"], "name": chore["name"], "weekday": WEEKDAYS[chore["weekday"]],
                           "next": f"{nxt:%a} {short(nxt)}", "who": names.get(who) if who else None,
                           "rotation": [names.get(i, "?") for i in chore["rotation"]],
                           "open_day": open_day.isoformat() if open_day else None,
                           "open_label": "tomorrow" if offset == 1 else "today" if offset == 0 else "overdue"})
        events = [{"id": e["id"], "title": e["title"], "when": f"{e['day']:%a} {short(e['day'])}",
                   "host": names.get(e["host_id"]) if e["host_id"] else None, "prep": e["prep"]}
                  for e in house.list_events(conn, today)]
        upkeep = []
        for item in house.list_upkeep(conn):
            due = house_facts.upkeep_due_on(item, today)
            days = (due - today).days
            upkeep.append({"id": item["id"], "label": item["label"], "due": short(due),
                           "state": "overdue" if days < 0 else "due today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"})
        todos = [{"id": t["id"], "text": t["text"], "who": names.get(t["claimed_by"]) if t["claimed_by"] else None,
                  "due": short(date.fromisoformat(t["due"])) if t["due"] else None} for t in house.list_todos(conn)]
        notes = [{"id": n["id"], "who": names.get(n["member_id"]) if n["member_id"] else None, "text": n["text"]}
                 for n in house.pending_notes(conn)]
        has_today = house.get_episode(conn, today) is not None
        next_day = house_facts.next_day(now, settings.house_at, has_today)
        script = house_script.compose(house_facts.gather(conn, next_day), next_day)
        preview = [f"{s['heading']}: {s['text']}" if s["kind"] == "text" else f"{s['heading']} (recording)"
                   for s in script["sections"]]
        episodes = house.list_episodes(conn)
        last = {"date": episodes[0]["date"], "minutes": round(episodes[0]["duration_s"] / 60, 1)} if episodes else None
        problem = db.get_state(conn, "house_last_error") or ""
        base = settings.share_base_url
        members = []
        for m in house.list_members(conn):
            link = f"{base}/h/{m['token']}" if base else None
            members.append({"id": m["id"], "name": m["name"], "link": link,
                            "feed": f"{link}/feed.xml" if link else None})
        pending = [{"id": m["id"], "who": names.get(m["member_id"], "a housemate"),
                    "seconds": round(m["duration_s"])} for m in house.list_pending_memos(conn)]
        return {"enabled": settings.house_brief, "error": error, "notice": notice,
                "members": members, "pending_memos": pending,
                "approved_waiting": house.approved_waiting_count(conn), "sharing": bool(base),
                "chores": chores, "events": events, "upkeep": upkeep, "todos": todos, "notes": notes,
                "preview": preview, "preview_day": f"{next_day:%A} {short(next_day)}", "last": last,
                "last_error": problem.split("|", 1)[1] if "|" in problem else "",
                "feed_url": f"{settings.public_base_url}/feed/house/{settings.feed_token}.xml",
                "today": today.isoformat(), "weekdays": list(enumerate(WEEKDAYS))}

    def render(request: Request, error: str | None = None, notice: str | None = None):
        return templates.TemplateResponse(request, "partials/house.html", {"hs": view(error, notice)})

    def respond(request: Request, action):
        try:
            action()
        except house.HouseError as exc:
            return render(request, error=str(exc))
        return render(request)

    def guard(request: Request) -> None:
        check_hx_request(request)
        on()

    # --- housemates ---
    @router.post("/house/members")
    async def add_member(request: Request):
        guard(request)
        form = await request.form()
        return respond(request, lambda: house.add_member(conn, form.get("name"), clock()))

    @router.delete("/house/members/{member_id}")
    def revoke_member(member_id: Id, request: Request):
        guard(request)
        house.revoke_member(conn, member_id, clock())
        return render(request)

    # --- voice notes waiting ---
    @router.post("/house/memos/{memo_id}/approve")
    def approve_memo(memo_id: Id, request: Request):
        guard(request)
        house.approve_memo(conn, memo_id, clock())
        return render(request)

    @router.post("/house/memos/{memo_id}/reject")
    def reject_memo(memo_id: Id, request: Request):
        guard(request)
        house.reject_memo(conn, memo_id, settings.house_dir)
        return render(request)

    @router.get("/house/memos/{memo_id}/audio")
    def memo_audio(memo_id: Id):
        on()
        memo = house.get_memo(conn, memo_id)
        path = house.memo_path(settings.house_dir, memo_id)
        if memo is None or memo["status"] not in ("pending", "approved") or memo["played_on"] or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # --- chores ---
    @router.post("/house/chores")
    async def add_chore(request: Request):
        guard(request)
        form = await request.form()

        def act():
            rotation = [_int(v, "housemate") for v in form.getlist("rotation")]
            house.add_chore(conn, form.get("name"), _int(form.get("weekday"), "weekday"),
                            _date(form.get("start"), "start date"), rotation, clock())

        return respond(request, act)

    @router.delete("/house/chores/{chore_id}")
    def delete_chore(chore_id: Id, request: Request):
        guard(request)
        house.delete_chore(conn, chore_id)
        return render(request)

    @router.post("/house/chores/{chore_id}/done")
    def finish_chore(chore_id: Id, request: Request):
        guard(request)
        today = clock().astimezone(TZ).date()
        for chore in house.list_chores(conn):
            if chore["id"] == chore_id:
                day = house_facts.occurrence_in_window(chore, today)  # never taken from the client
                if day is not None:
                    house.mark_chore_done(conn, chore_id, day, None, clock())
        return render(request)

    # --- hosting ---
    @router.post("/house/events")
    async def add_event(request: Request):
        guard(request)
        form = await request.form()

        def act():
            host = form.get("host")
            house.add_event(conn, form.get("title"), _date(form.get("day"), "date"),
                            _int(host, "host") if host else None, str(form.get("prep") or "").splitlines(),
                            clock(), clock().astimezone(TZ).date())

        return respond(request, act)

    @router.post("/house/prep/{prep_id}/tick")
    def tick_prep(prep_id: Id, request: Request):
        guard(request)
        house.tick_prep(conn, prep_id, None, clock())
        return render(request)

    @router.delete("/house/events/{event_id}")
    def delete_event(event_id: Id, request: Request):
        guard(request)
        house.delete_event(conn, event_id)
        return render(request)

    # --- upkeep ---
    @router.post("/house/upkeep")
    async def add_upkeep(request: Request):
        guard(request)
        form = await request.form()

        def act():
            kind = str(form.get("kind") or "")
            extra = {"last_done": _date(form.get("last_done"), "last done date", required=False)}
            if kind == "every":
                extra["every_days"] = _int(form.get("every_days"), "number of days") if form.get("every_days") else None
            elif kind == "yearly":
                extra["month"] = _int(form.get("month"), "month") if form.get("month") else None
                extra["day"] = _int(form.get("day"), "day") if form.get("day") else None
            now = clock()
            house.add_upkeep(conn, form.get("label"), kind, now, now.astimezone(TZ).date(), **extra)

        return respond(request, act)

    @router.post("/house/upkeep/{upkeep_id}/done")
    def upkeep_done(upkeep_id: Id, request: Request):
        guard(request)
        house.mark_upkeep_done(conn, upkeep_id, clock().astimezone(TZ).date())
        return render(request)

    @router.delete("/house/upkeep/{upkeep_id}")
    def delete_upkeep(upkeep_id: Id, request: Request):
        guard(request)
        house.delete_upkeep(conn, upkeep_id)
        return render(request)

    # --- to-dos ---
    @router.post("/house/todos")
    async def add_todo(request: Request):
        guard(request)
        form = await request.form()
        return respond(request, lambda: house.add_todo(conn, form.get("text"), None,
                                                       _date(form.get("due"), "due date", required=False), clock()))

    @router.post("/house/todos/{todo_id}/claim")
    async def claim_todo(todo_id: Id, request: Request):
        guard(request)
        form = await request.form()
        member = form.get("member")
        return respond(request, lambda: house.claim_todo(conn, todo_id, _int(member, "housemate") if member else None))

    @router.post("/house/todos/{todo_id}/done")
    def finish_todo(todo_id: Id, request: Request):
        guard(request)
        house.finish_todo(conn, todo_id, None, clock())
        return render(request)

    @router.delete("/house/todos/{todo_id}")
    def delete_todo(todo_id: Id, request: Request):
        guard(request)
        house.delete_todo(conn, todo_id)
        return render(request)

    # --- notes, generate ---
    @router.delete("/house/notes/{note_id}")
    def delete_note(note_id: Id, request: Request):
        guard(request)
        house.delete_note(conn, note_id)
        return render(request)

    @router.post("/house/generate")
    def generate(request: Request):
        guard(request)
        start_house()
        return render(request, notice="Making the episode. It takes a few minutes; reload to see it.")

    # --- the owner's feed (tailnet, through the existing serve) ---
    @router.get("/feed/house/{token}.xml")
    def feed(token: str):
        on()
        check_token(token)
        episodes = house_feed.episodes(conn)
        base = settings.public_base_url
        xml = podcast.build_house_feed(
            episodes, base, lambda ep: f"{base}/audio/{settings.feed_token}/house/{ep['date']}.mp3",
            "/feed/house-cover.png")
        return Response(xml, media_type="application/rss+xml")

    @router.get("/audio/{token}/house/{day}.mp3")
    def audio(token: str, day: str):
        on()
        check_token(token)
        if not DATE_RE.match(day):
            raise HTTPException(404)
        try:
            path = house.episode_path(settings.house_dir, date.fromisoformat(day))
        except ValueError:
            raise HTTPException(404) from None
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/mpeg")

    @router.get("/feed/house-cover.png")
    def cover():
        on()
        return FileResponse(PKG / "static" / "house-cover.png", media_type="image/png")

    router.view = view
    return router
