"""Housemate links: /h/<token> (the House page), /h/<token>/feed.xml, /audio/<date>.mp3 and /cover.png, plus the
housemate writes (notes, to-dos, ticks and voice memos).

These paths may be reached through Tailscale Funnel, so each is keyed by an unguessable per-housemate token and
answers with the same plain 404 whenever the token is unknown or revoked. The page shows House content only: nothing
from the personal brief, and no link back into the app.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import shutil
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool

from . import house, house_audio, house_facts, house_feed, podcast
from .config import TZ

PKG = Path(__file__).resolve().parent
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EVENT_DAYS = 14
TODOS_SHOWN = 50
FORM_MAX = 16 * 1024
RECEIVE_TIMEOUT_S = 120   # read at call time so tests can change them
MAX_RECEIVING = 3
log = logging.getLogger(__name__)
Id = Annotated[int, PathParam(ge=1, le=2**63 - 1)]

RECORD_SCRIPT = """
(function () {
  var root = document.getElementById("recorder");
  if (!root || !window.MediaRecorder || !navigator.mediaDevices) { return; }
  root.hidden = false;
  var button = document.getElementById("rec-button");
  var status = document.getElementById("rec-status");
  var maxSeconds = Number(root.dataset.max);
  var action = root.dataset.action;
  var recorder = null, chunks = [], ticker = null, stopper = null, startedAt = 0;
  function label() {
    status.textContent = "Recording… " + Math.floor((Date.now() - startedAt) / 1000) + " s (stops at " + maxSeconds + " s)";
  }
  function send(blob) {
    var form = new FormData();
    form.append("audio", blob, "memo");
    status.textContent = "Sending…";
    fetch(action, { method: "POST", body: form }).then(function (response) {
      if (response.ok) { window.location.href = response.url; return; }
      status.textContent = response.headers.get("X-Memo-Error") || "Couldn't send that recording.";
      button.disabled = false;
    }).catch(function () {
      status.textContent = "Couldn't send that recording.";
      button.disabled = false;
    });
  }
  button.addEventListener("click", function () {
    if (recorder && recorder.state === "recording") { recorder.stop(); return; }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = function (event) { if (event.data && event.data.size) { chunks.push(event.data); } };
      recorder.onstop = function () {
        clearInterval(ticker);
        clearTimeout(stopper);
        stream.getTracks().forEach(function (track) { track.stop(); });
        button.textContent = "Record a voice note";
        button.disabled = true;
        send(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
      };
      startedAt = Date.now();
      recorder.start();
      button.textContent = "Stop and send";
      label();
      ticker = setInterval(label, 500);
      stopper = setTimeout(function () { recorder.stop(); }, Math.max(1, maxSeconds - 1) * 1000);
    }).catch(function () {
      status.textContent = "Couldn't use the microphone. You can choose a file below instead.";
    });
  });
})();
"""
SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(RECORD_SCRIPT.encode()).digest()).decode()
COMMON_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
HOUSE_HEADERS = {
    **COMMON_HEADERS,
    "Permissions-Policy": "microphone=(self)",
    "Content-Security-Policy": (
        f"default-src 'self'; script-src '{SCRIPT_HASH}'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
        "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}
AUDIO_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "X-Content-Type-Options": "nosniff"}
NOT_FOUND_HTML = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                  '<meta name="viewport" content="width=device-width, initial-scale=1">'
                  "<title>Not available</title></head><body><p>This link isn't available.</p></body></html>")
OK_MESSAGES = {
    "note": "Note added.", "todo": "To-do added.", "claimed": "It's yours.", "done": "Marked done.",
    "memo": "Thanks! Your voice note is waiting for the owner to approve it.",
}


class FormTooLarge(Exception):
    pass


async def small_form(request: Request) -> dict[str, str]:
    """The fields of a small urlencoded form, never reading more than FORM_MAX bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and (not (declared.isascii() and declared.isdigit()) or int(declared) > FORM_MAX):
        raise FormTooLarge()
    if not request.headers.get("content-type", "").lower().startswith("application/x-www-form-urlencoded"):
        return {}
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > FORM_MAX:
            raise FormTooLarge()
    try:
        parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
    except (UnicodeDecodeError, ValueError):
        return {}
    return {key: values[0] for key, values in parsed.items()}


def make_router(*, settings, conn, templates, clock, limits) -> APIRouter:
    router = APIRouter()

    def not_found() -> HTMLResponse:
        return HTMLResponse(NOT_FOUND_HTML, status_code=404, headers=HOUSE_HEADERS)

    def who(token: str):
        """The active housemate for this link, or None (also None when the House brief is off)."""
        return house.member_for_token(conn, token) if settings.house_brief else None

    def base_url() -> str:
        return settings.share_base_url or settings.public_base_url

    def view(member, token: str, error: str | None = None, ok: str | None = None) -> dict:
        today = clock().astimezone(TZ).date()
        names = house.member_names(conn)
        events = []
        for e in house.list_events(conn, today):
            if (e["day"] - today).days > EVENT_DAYS:
                continue
            events.append({"title": e["title"], "when": f"{e['day']:%A, %B} {e['day'].day}",
                           "host": names.get(e["host_id"]) if e["host_id"] else None, "prep": e["prep"]})
        chores = []
        for chore in house.list_chores(conn):
            day = house_facts.occurrence_in_window(chore, today)
            if day is None or house.chore_is_done(conn, chore["id"], day):
                continue
            who_id = house_facts.assignee(chore, day)
            offset = (day - today).days
            chores.append({"id": chore["id"], "name": chore["name"], "who": names.get(who_id) if who_id else None,
                           "mine": who_id == member["id"],
                           "when": "tomorrow" if offset == 1 else "today" if offset == 0 else "overdue"})
        todos = [{"id": t["id"], "text": t["text"], "mine": t["claimed_by"] == member["id"],
                  "who": names.get(t["claimed_by"]) if t["claimed_by"] else None,
                  "due": f"{date.fromisoformat(t['due']):%b} {date.fromisoformat(t['due']).day}" if t["due"] else None}
                 for t in house.list_todos(conn)[:TODOS_SHOWN]]
        episodes = house.list_episodes(conn)
        return {"name": member["name"], "token": token, "error": error, "ok": OK_MESSAGES.get(ok or ""),
                "events": events, "chores": chores, "todos": todos,
                "latest": episodes[0]["date"] if episodes else None,
                "feed_url": f"{base_url()}/h/{token}/feed.xml",
                "max_memo_s": settings.house_memo_max_s, "max_upload_mb": settings.house_upload_max_mb,
                "uploads": shutil.which("ffmpeg") is not None}

    def page(member, token: str, request: Request, *, error: str | None = None, ok: str | None = None,
             status: int = 200, extra_headers: dict | None = None):
        return templates.TemplateResponse(
            request, "house_page.html",
            {"v": view(member, token, error, ok), "record_script": Markup(RECORD_SCRIPT)},
            status_code=status, headers={**HOUSE_HEADERS, **(extra_headers or {})})

    router.who, router.page, router.not_found = who, page, not_found

    @router.api_route("/h/{token}", methods=["GET", "HEAD"], response_class=HTMLResponse)
    def house_page(token: str, request: Request, ok: str | None = None):
        member = who(token)
        if member is None:
            return not_found()
        return page(member, token, request, ok=ok)

    @router.api_route("/h/{token}/feed.xml", methods=["GET", "HEAD"])
    def feed(token: str):
        if who(token) is None:
            return not_found()
        base = base_url()
        xml = podcast.build_house_feed(
            house_feed.episodes(conn), base, lambda ep: f"{base}/h/{token}/audio/{ep['date']}.mp3",
            f"/h/{token}/cover.png")
        return Response(xml, media_type="application/rss+xml", headers=COMMON_HEADERS)

    @router.api_route("/h/{token}/audio/{day}.mp3", methods=["GET", "HEAD"])
    def audio(token: str, day: str):
        if who(token) is None or not DATE_RE.match(day):
            return not_found()
        try:
            path = house.episode_path(settings.house_dir, date.fromisoformat(day))
        except ValueError:
            return not_found()
        if not path.is_file():
            return not_found()
        return FileResponse(path, media_type="audio/mpeg", headers=AUDIO_HEADERS)

    @router.api_route("/h/{token}/cover.png", methods=["GET", "HEAD"])
    def cover(token: str):
        if who(token) is None:
            return not_found()
        return FileResponse(PKG / "static" / "house-cover.png", media_type="image/png", headers=AUDIO_HEADERS)

    def redirect(token: str, ok: str):
        return RedirectResponse(f"/h/{token}?ok={ok}", status_code=303, headers=HOUSE_HEADERS)

    def throttled(member, token: str, request: Request):
        return page(member, token, request, error="Slow down a little: try again in a minute.", status=429)

    async def write(token: str, request: Request, action, ok: str):
        """Look up the link, apply the write limit, read a small form, run `action(member, form)` and redirect."""
        member = who(token)
        if member is None:
            return not_found()
        if not limits.allow_write(token, clock().timestamp()):
            return throttled(member, token, request)
        try:
            form = await small_form(request)
        except FormTooLarge:
            return page(member, token, request, error="That was too much to send at once.", status=413)
        try:
            action(member, form)
        except house.HouseError as exc:
            return page(member, token, request, error=str(exc), status=400)
        return redirect(token, ok)

    def due_date(form: dict) -> date | None:
        raw = form.get("due", "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise house.HouseError("pick a real due date") from None

    @router.post("/h/{token}/notes")
    async def add_note(token: str, request: Request):
        return await write(token, request, lambda m, f: house.add_note(conn, f.get("text"), m["id"], clock()), "note")

    @router.post("/h/{token}/todos")
    async def add_todo(token: str, request: Request):
        return await write(token, request,
                           lambda m, f: house.add_todo(conn, f.get("text"), m["id"], due_date(f), clock()), "todo")

    @router.post("/h/{token}/todos/{todo_id}/claim")
    async def claim_todo(token: str, todo_id: Id, request: Request):
        return await write(token, request, lambda m, f: house.claim_todo(conn, todo_id, m["id"]), "claimed")

    @router.post("/h/{token}/todos/{todo_id}/done")
    async def finish_todo(token: str, todo_id: Id, request: Request):
        return await write(token, request, lambda m, f: house.finish_todo(conn, todo_id, m["id"], clock()), "done")

    @router.post("/h/{token}/prep/{prep_id}/done")
    async def tick_prep(token: str, prep_id: Id, request: Request):
        return await write(token, request, lambda m, f: house.tick_prep(conn, prep_id, m["id"], clock()), "done")

    @router.post("/h/{token}/chores/{chore_id}/done")
    async def finish_chore(token: str, chore_id: Id, request: Request):
        def action(member, form):
            today = clock().astimezone(TZ).date()
            for chore in house.list_chores(conn):
                if chore["id"] == chore_id:
                    day = house_facts.occurrence_in_window(chore, today)   # never taken from the client
                    if day is not None:
                        house.mark_chore_done(conn, chore_id, day, member["id"], clock())

        return await write(token, request, action, "done")

    def memo_failure(member, token: str, request: Request, message: str, status: int):
        return page(member, token, request, error=message, status=status,
                    extra_headers={"X-Memo-Error": message})

    receiving = 0   # uploads being read right now (one event loop, so a plain counter is safe)

    @router.post("/h/{token}/memos")
    async def add_memo(token: str, request: Request):
        nonlocal receiving
        member = who(token)
        if member is None:
            return not_found()
        if receiving >= MAX_RECEIVING:
            return memo_failure(member, token, request, "busy right now, try again in a minute", 429)
        if not limits.allow_memo(token, clock().timestamp()):
            return memo_failure(member, token, request, "That's a lot of voice notes: try again later.", 429)
        if house.pending_memo_count(conn, member["id"]) >= settings.house_pending_memos:
            return memo_failure(member, token, request,
                                f"you already have {settings.house_pending_memos} voice notes waiting for approval", 429)
        path = None
        try:
            receiving += 1
            try:
                async with asyncio.timeout(RECEIVE_TIMEOUT_S):
                    path = await house_audio.receive(request, settings.house_upload_max_mb * 1024 * 1024,
                                                     settings.house_dir / "incoming")
            except TimeoutError:
                return memo_failure(member, token, request, "that upload took too long", 408)
            finally:
                receiving -= 1
            memo = await run_in_threadpool(house_audio.prepare, path, settings.house_memo_max_s)
            memo_id = house.add_memo(conn, member["id"], memo.duration_s, clock(), settings.house_pending_memos)
            try:
                house_audio.save_wav(house.memo_path(settings.house_dir, memo_id), memo.samples)
            except BaseException:
                house.delete_memo(conn, memo_id)
                wav = house.memo_path(settings.house_dir, memo_id)
                wav.with_name(wav.name + ".tmp").unlink(missing_ok=True)
                raise
        except house_audio.AudioError as exc:
            return memo_failure(member, token, request, exc.message, exc.status)
        except house.HouseError as exc:
            return memo_failure(member, token, request, str(exc), 429)
        except Exception:
            log.exception("voice memo failed")
            return memo_failure(member, token, request, "Couldn't save that recording. Try again later.", 500)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        return redirect(token, "memo")

    return router


def add_catch_alls(router: APIRouter) -> None:
    """Every other path under /h/ gets the same plain 404. Call after all the real /h/ routes are registered."""

    @router.api_route("/h/{rest:path}", methods=["GET", "HEAD"])
    def catch_all_get(rest: str):
        return router.not_found()

    @router.post("/h/{rest:path}")
    def catch_all_post(rest: str):
        return router.not_found()
