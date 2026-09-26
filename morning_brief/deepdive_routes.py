"""Deep-dive routes: the Mac worker's API, the Deep Dives feed/audio, and (Task 6) the UI section."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from . import deepdives, podcast
from .config import TZ

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

    @router.post("/api/deep-dives/claim")
    def claim(request: Request):
        check_worker(request)
        row = deepdives.claim(conn, clock())
        if row is None:
            return Response(status_code=204)
        return {"id": row["id"], "topic": row["topic"], "notes": row["notes"]}

    @router.post("/api/deep-dives/{topic_id}/script", status_code=202)
    async def submit_script(topic_id: int, request: Request):
        check_worker(request)
        try:
            script = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"problems": ["the body must be valid JSON"]}, status_code=422)
        problems = deepdives.validate_script(script)
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

    def _minutes_since(iso: str) -> int:
        return max(0, int((clock() - datetime.fromisoformat(iso)).total_seconds() // 60))

    def _local_date(iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%b %-d")

    def episode(row) -> dict:
        script = json.loads(row["script_json"])
        sources = {s["source_id"]: dict(s) for s in deepdives.sources_for(conn, row["id"])}
        version = int(datetime.fromisoformat(row["episode_updated_at"]).timestamp())
        return {
            "id": row["id"], "title": row["episode_title"], "topic": row["topic"],
            "duration": _duration(row["duration_s"]),
            "audio_url": f"/audio/{settings.feed_token}/deep-dives/{row['id']}.mp3?v={version}",
            "intro": script["intro"], "outro": script["outro"],
            "sections": [{"heading": s["heading"], "text": s["text"],
                          "sources": [sources[i] for i in s["source_ids"] if i in sources]}
                         for s in script["sections"]],
            "published_at": row["published_at"], "heard_at": row["heard_at"],
            "heard_date": _local_date(row["heard_at"]) if row["heard_at"] else None,
        }

    def _sig(rows) -> str:
        pairs = sorted((row["id"], row["status"]) for row in rows)
        return hashlib.sha1(str(pairs).encode()).hexdigest()[:12]

    def section_view(error: str | None = None) -> dict:
        rows = deepdives.list_topics(conn)
        groups = {"ready": [], "in_progress": [], "queued": [], "failed": [], "heard": []}
        for row in rows:
            status = row["status"]
            if status in ("ready", "heard"):
                groups[status].append(episode(row))
            elif status in ("researching", "speaking"):
                groups["in_progress"].append({"id": row["id"], "topic": row["topic"], "status": status,
                                              "minutes": _minutes_since(row["updated_at"])})
            else:
                groups[status].append({"id": row["id"], "topic": row["topic"], "notes": row["notes"],
                                       "error": row["error"]})
        groups["ready"].sort(key=lambda e: e["published_at"], reverse=True)
        groups["heard"].sort(key=lambda e: e["heard_at"], reverse=True)
        return {**groups, "polling": bool(groups["in_progress"]), "error": error,
                "empty": not any(groups.values()), "sig": _sig(rows)}

    def render_section(request: Request, error: str | None = None, view: dict | None = None):
        return templates.TemplateResponse(request, "partials/deep_dives.html", {"dd": view or section_view(error)})

    @router.get("/partials/deep-dives")
    def section_partial(request: Request, sig: str | None = None):
        view = section_view()
        if sig is not None and sig == view["sig"]:
            return Response(status_code=204)
        return render_section(request, view=view)

    @router.post("/deep-dives")
    async def add(request: Request):
        check_hx_request(request)
        form = await request.form()
        try:
            deepdives.add_topic(conn, str(form.get("topic", "")), str(form.get("notes", "")), clock())
        except deepdives.TopicError as exc:
            return render_section(request, str(exc))
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

    @router.delete("/deep-dives/{topic_id}")
    def delete(topic_id: int, request: Request):
        check_hx_request(request)
        deepdives.delete_topic(conn, settings.deep_dives_dir, topic_id)
        return render_section(request)

    router.section_view = section_view

    return router
