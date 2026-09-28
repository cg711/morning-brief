"""Deep-dive routes: the Mac worker's API, the Deep Dives feed/audio, and (Task 6) the UI section."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from . import db, deepdives, podcast, shares
from .config import TZ

log = logging.getLogger(__name__)

PKG = Path(__file__).resolve().parent
DEEP_COVER = PKG / "static" / "deep-dives-cover.png"


def make_router(*, settings, conn, templates, clock, start_render, check_token, check_hx_request) -> APIRouter:
    router = APIRouter()

    def check_worker(request: Request) -> None:
        if not settings.worker_token:
            raise HTTPException(503, "worker API disabled: WORKER_TOKEN is not set")
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {settings.worker_token}".encode()):
            raise HTTPException(401)
        try:
            db.set_state(conn, "worker_last_seen", clock().isoformat())
        except Exception as exc:
            log.warning("could not stamp worker_last_seen: %s", type(exc).__name__)

    @router.post("/api/deep-dives/claim")
    def claim(request: Request):
        check_worker(request)
        row = deepdives.claim(conn, clock())
        if row is None:
            return Response(status_code=204)
        return {"id": row["id"], "topic": row["topic"], "notes": row["notes"], "url": row["source_url"],
                "fact_check": bool(row["fact_check"]), "two_hosts": bool(row["two_hosts"])}

    @router.post("/api/deep-dives/{topic_id}/script", status_code=202)
    async def submit_script(topic_id: int, request: Request):
        check_worker(request)
        try:
            script = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"problems": ["the body must be valid JSON"]}, status_code=422)
        topic = deepdives.get_topic(conn, topic_id)
        if topic is None:
            raise HTTPException(404)
        problems = deepdives.validate_script(script, two_hosts=bool(topic["two_hosts"]),
                                             fact_check=bool(topic["fact_check"]))
        if problems:
            return JSONResponse({"problems": problems}, status_code=422)
        if not deepdives.accept_script(conn, topic_id, script, clock()):
            raise HTTPException(404)
        start_render(topic_id)
        return {"status": "speaking"}

    @router.post("/api/deep-dives/{topic_id}/fail", status_code=204)
    async def worker_fail(topic_id: int, request: Request):
        check_worker(request)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        reason = str(body.get("reason", "")).strip() if isinstance(body, dict) else ""
        if not deepdives.fail(conn, topic_id, reason or "the worker gave no reason", clock(),
                              from_status="researching"):
            raise HTTPException(404)
        return Response(status_code=204)

    @router.get("/feed/deep-dives-cover.png")
    def deep_cover():
        return FileResponse(DEEP_COVER, media_type="image/png")

    @router.get("/feed/deep-dives/{token}.xml")
    def deep_feed(token: str):
        check_token(token)
        episodes = []
        for row in deepdives.list_topics(conn):
            if row["status"] not in ("ready", "heard") or not row["published_at"]:
                continue
            sources = [dict(s) for s in deepdives.sources_for(conn, row["id"])]
            episodes.append({
                "id": row["id"], "title": row["episode_title"], "published_at": row["published_at"],
                "updated_at": row["episode_updated_at"], "audio_bytes": row["audio_bytes"],
                "duration_s": row["duration_s"],
                "description": podcast.describe_deep_dive(json.loads(row["script_json"]), sources),
            })
        episodes.sort(key=lambda e: e["published_at"], reverse=True)
        xml = podcast.build_deep_dive_feed(episodes, settings.public_base_url, settings.feed_token)
        return Response(xml, media_type="application/rss+xml")

    @router.get("/audio/{token}/deep-dives/{topic_id}.mp3")
    def deep_audio(token: str, topic_id: int):
        check_token(token)
        path = deepdives.audio_path(settings.deep_dives_dir, topic_id)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/mpeg")

    def _duration(seconds) -> str:
        total = round(seconds or 0)
        return f"{total // 60}:{total % 60:02d}"

    def _clock_label(seconds: float) -> str:
        total = int(seconds)
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

    def _minutes_since(iso: str) -> int:
        return max(0, int((clock() - datetime.fromisoformat(iso)).total_seconds() // 60))

    def _local_date(iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%b %-d")

    def _link_host(url: str | None) -> str | None:
        if not url:
            return None
        return (urlsplit(url).hostname or "").removeprefix("www.") or None

    def _topic_meta(row) -> dict:
        return {"source_url": row["source_url"], "link_host": _link_host(row["source_url"]),
                "fact_check": bool(row["fact_check"]), "two_hosts": bool(row["two_hosts"])}

    def _fact_check_summary(script: dict) -> dict | None:
        summary = script.get("fact_check")
        keys = ("claims_checked", "corrected", "removed")
        if isinstance(summary, dict) and all(
                isinstance(summary.get(k), int) and not isinstance(summary.get(k), bool) for k in keys):
            return {k: summary[k] for k in keys}
        return None

    def episode(row, followed: frozenset = frozenset()) -> dict:
        script = json.loads(row["script_json"])
        sources = {s["source_id"]: dict(s) for s in deepdives.sources_for(conn, row["id"])}
        version = int(datetime.fromisoformat(row["episode_updated_at"]).timestamp())
        marks = json.loads(row["chapters_json"]) if row["chapters_json"] else None
        if marks is not None and len(marks) != len(script["sections"]) + 2:
            marks = None  # not the layout we wrote; don't guess
        starts = [marks[1 + n][1] for n in range(len(script["sections"]))] if marks else None
        return {
            "id": row["id"], "title": row["episode_title"], "topic": row["topic"],
            "duration": _duration(row["duration_s"]),
            "audio_url": f"/audio/{settings.feed_token}/deep-dives/{row['id']}.mp3?v={version}",
            "intro": script["intro"], "outro": script["outro"],
            "sections": [{"heading": s["heading"],
                          "text": s.get("text"),
                          "lines": [{"speaker": "Host" if line["speaker"] == "host" else "Co-host",
                                     "text": line["text"]} for line in s["lines"]] if "lines" in s else None,
                          "sources": [sources[i] for i in s["source_ids"] if i in sources],
                          "start": starts[n] if starts else None,
                          "start_label": _clock_label(starts[n]) if starts else None,
                          "index": n, "followed": n in followed}
                         for n, s in enumerate(script["sections"])],
            "published_at": row["published_at"], "heard_at": row["heard_at"],
            "heard_date": _local_date(row["heard_at"]) if row["heard_at"] else None,
            "fact_check": _fact_check_summary(script) if row["fact_check"] else None,
        }

    def _sig(rows, long_ids=frozenset()) -> str:
        pairs = sorted((row["id"], row["status"]) for row in rows)
        return hashlib.sha1(str((pairs, sorted(long_ids))).encode()).hexdigest()[:12]

    def worker_view() -> dict | None:
        if not settings.worker_token:
            return None
        return deepdives.worker_status(db.get_state(conn, "worker_last_seen"), clock())

    def section_view(error: str | None = None) -> dict:
        rows = deepdives.list_topics(conn)
        live = shares.live_tokens(conn)
        long_ids = {r["id"] for r in deepdives.long_running(conn, clock())}
        followed: dict[int, set[int]] = {}
        for row in rows:
            if row["parent_topic_id"] is not None:
                followed.setdefault(row["parent_topic_id"], set()).add(row["parent_section"])
        groups = {"ready": [], "in_progress": [], "queued": [], "failed": [], "heard": []}
        for row in rows:
            status = row["status"]
            if status in ("ready", "heard"):
                ep = episode(row, frozenset(followed.get(row["id"], ())))
                token = live.get(row["id"])
                ep["shared"] = token is not None
                ep["share_url"] = f"{settings.share_base_url}/s/{token}" if token and settings.share_base_url else None
                groups[status].append(ep)
            elif status in ("researching", "speaking"):
                groups["in_progress"].append({"id": row["id"], "topic": row["topic"], "status": status,
                                              "minutes": _minutes_since(row["updated_at"]),
                                              "long": row["id"] in long_ids,
                                              "parent_title": row["parent_title"], **_topic_meta(row)})
            else:
                groups[status].append({"id": row["id"], "topic": row["topic"], "notes": row["notes"],
                                       "error": row["error"], "parent_title": row["parent_title"],
                                       **_topic_meta(row)})
        groups["ready"].sort(key=lambda e: e["published_at"], reverse=True)
        groups["heard"].sort(key=lambda e: e["heard_at"], reverse=True)
        suggestions = [dict(s) for s in deepdives.list_suggestions(conn)]
        return {**groups, "polling": bool(groups["in_progress"]), "error": error,
                "empty": not any(groups.values()) and not suggestions,
                "sig": _sig(rows, long_ids), "worker": worker_view(),
                "suggestions": suggestions,
                "defaults": deepdives.get_defaults(conn),
                "sharing": bool(settings.share_base_url)}

    def render_section(request: Request, error: str | None = None, view: dict | None = None):
        return templates.TemplateResponse(request, "partials/deep_dives.html", {"dd": view or section_view(error)})

    @router.get("/partials/deep-dives")
    def section_partial(request: Request, sig: str | None = None):
        view = section_view()
        if sig is not None and sig == view["sig"]:
            return Response(status_code=204)
        return render_section(request, view=view)

    @router.get("/partials/worker-status")
    def worker_status_partial(request: Request):
        ws = worker_view()
        if ws is None:
            return Response(status_code=200)
        return templates.TemplateResponse(request, "partials/worker_status.html", {"ws": ws})

    @router.post("/deep-dives")
    async def add(request: Request):
        check_hx_request(request)
        form = await request.form()
        try:
            deepdives.add_topic(conn, str(form.get("topic", "")), str(form.get("notes", "")), clock(),
                                url=str(form.get("url", "")), fact_check=form.get("fact_check") == "1",
                                two_hosts=form.get("two_hosts") == "1")
        except deepdives.TopicError as exc:
            return render_section(request, str(exc))
        return render_section(request)

    @router.post("/deep-dives/defaults")
    async def set_defaults(request: Request):
        check_hx_request(request)
        form = await request.form()
        try:
            deepdives.set_default(conn, str(form.get("key", "")), form.get("value") == "1")
        except ValueError:
            raise HTTPException(400)
        return render_section(request)

    @router.post("/deep-dives/{topic_id}/top")
    def to_top(topic_id: int, request: Request):
        check_hx_request(request)
        deepdives.move_to_top(conn, topic_id, clock())
        return render_section(request)

    @router.post("/deep-dives/{topic_id}/heard")
    def heard(topic_id: int, request: Request):
        check_hx_request(request)
        deepdives.mark_heard(conn, topic_id, clock())
        return render_section(request)

    @router.post("/deep-dives/{topic_id}/retry")
    def retry(topic_id: int, request: Request):
        check_hx_request(request)
        if deepdives.retry(conn, topic_id, clock()) == "speaking":
            start_render(topic_id)
        return render_section(request)

    @router.post("/deep-dives/{topic_id}/deeper/{section_index}")
    def deeper(topic_id: int, section_index: int, request: Request):
        check_hx_request(request)
        deepdives.add_follow_up(conn, topic_id, section_index, clock())
        return render_section(request)

    @router.post("/suggestions/{suggestion_id}/add")
    def add_suggestion(suggestion_id: int, request: Request):
        check_hx_request(request)
        deepdives.add_suggestion(conn, suggestion_id, clock())
        return render_section(request)

    @router.post("/suggestions/{suggestion_id}/dismiss")
    def dismiss_suggestion(suggestion_id: int, request: Request):
        check_hx_request(request)
        deepdives.dismiss_suggestion(conn, suggestion_id)
        return render_section(request)

    @router.post("/deep-dives/{topic_id}/share")
    def share(topic_id: int, request: Request):
        check_hx_request(request)
        if not settings.share_base_url or shares.create_or_get(conn, topic_id, clock()) is None:
            raise HTTPException(404)
        return render_section(request)

    @router.post("/deep-dives/{topic_id}/unshare")
    def unshare(topic_id: int, request: Request):
        check_hx_request(request)
        shares.revoke(conn, topic_id, clock())
        return render_section(request)

    @router.delete("/deep-dives/{topic_id}")
    def delete(topic_id: int, request: Request):
        check_hx_request(request)
        deepdives.delete_topic(conn, settings.deep_dives_dir, topic_id)
        return render_section(request)

    router.section_view = section_view
    router.check_worker = check_worker
    router.episode_view = episode

    return router
