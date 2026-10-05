"""Worker API for the daily brief (DAILY_BRIEF=worker). Same bearer token as the deep-dive worker."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from . import daily_worker


def make_router(*, settings, conn, clock, check_worker, start_publish) -> APIRouter:
    router = APIRouter()

    @router.post("/api/daily/claim")
    def claim(request: Request):
        check_worker(request)
        job = daily_worker.claim(conn, clock(), settings.listener_location)
        if job is None:
            return Response(status_code=204)
        # indented so the file has short lines the worker's Read tool can page through
        return Response(json.dumps(job, indent=1), media_type="application/json")

    @router.get("/api/daily/{episode_date}/items/{item}")
    def item(episode_date: str, item: str, request: Request):
        check_worker(request)
        text = daily_worker.item_text(conn, episode_date, item, clock())
        if text is None:
            raise HTTPException(404)
        return PlainTextResponse(text)

    @router.post("/api/daily/{episode_date}/script", status_code=202)
    async def submit(episode_date: str, request: Request):
        check_worker(request)
        job = daily_worker.get_job(conn, episode_date)
        if not daily_worker.can_submit(job, clock()):
            raise HTTPException(404)
        try:
            script = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"problems": ["the body must be valid JSON"]}, status_code=422)
        facts = json.loads(job["personal_json"]) if job["personal_json"] else None
        problems = daily_worker.validate_submission(script, daily_worker.known_ids(job),
                                                    personal_allowed=bool(facts and set(facts) - {"notes"}),
                                                    notes_allowed=bool(facts and facts.get("notes")))
        if problems:
            return JSONResponse({"problems": problems}, status_code=422)
        if not daily_worker.accept(conn, episode_date, script, clock()):
            raise HTTPException(404)
        start_publish(episode_date)
        return {"status": "speaking"}

    @router.post("/api/daily/{episode_date}/fail", status_code=204)
    async def fail(episode_date: str, request: Request):
        check_worker(request)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        reason = str(body.get("reason", "")).strip() if isinstance(body, dict) else ""
        if not daily_worker.fail_job(conn, episode_date, reason or "the worker gave no reason", clock()):
            raise HTTPException(404)
        return Response(status_code=204)

    return router
