from __future__ import annotations

import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, deepdive_render, deepdive_routes, notify, pipeline, podcast, retention, scheduler
from .config import PRICES, TZ, Settings

PKG = Path(__file__).resolve().parent
COVER = PKG / "static" / "cover.png"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FUNNEL_HEADER = "tailscale-funnel-request"
PUBLIC_PREFIXES = ("/feed/", "/audio/")
templates = Jinja2Templates(directory=PKG / "templates")


def _configure_logging() -> None:
    """Send morning_brief.* INFO logs to stderr (uvicorn configures only its own loggers)."""
    logger = logging.getLogger("morning_brief")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # phonemizer logs a harmless "words count mismatch" WARNING for nearly every passage.
    logging.getLogger("phonemizer").setLevel(logging.ERROR)


def _duration(seconds: float) -> str:
    total = round(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%b %-d, %-I:%M %p")


def create_app(settings: Settings | None = None, *, clock=None, start_run=None, start_render=None,
                start_scheduler=True, notifier=None) -> FastAPI:
    _configure_logging()
    settings = settings or Settings.from_env()
    clock = clock or (lambda: datetime.now(timezone.utc))
    notifier = notifier or notify.send
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    db.fail_interrupted_runs(conn, clock().isoformat())
    start_run = start_run or (lambda trigger: scheduler.start_background_run(settings, trigger))
    start_render = start_render or (lambda topic_id: deepdive_render.start(settings, topic_id, notify=notifier))
    settings.deep_dives_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = None
        if start_scheduler:
            sched = scheduler.build(settings, conn, clock, start_run, notifier)
            sched.start()
            scheduler.catch_up(settings, conn, clock, start_run)
            deepdive_render.resume(settings, conn, notify=notifier)
        yield
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(title="Morning Brief", lifespan=lifespan)
    app.state.conn, app.state.settings, app.state.clock, app.state.start_run = conn, settings, clock, start_run
    app.state.start_render = start_render
    app.state.notifier = notifier
    app.mount("/static", StaticFiles(directory=PKG / "static"), name="static")

    @app.middleware("http")
    async def funnel_guard(request: Request, call_next):
        # Requests arriving through Tailscale Funnel may only reach the feed and audio.
        if FUNNEL_HEADER in request.headers and not request.url.path.startswith(PUBLIC_PREFIXES):
            return Response(status_code=404)
        return await call_next(request)

    def today_str() -> str:
        return clock().astimezone(TZ).date().isoformat()

    def check_token(token: str) -> None:
        if not secrets.compare_digest(token.encode(), settings.feed_token.encode()):
            raise HTTPException(404)

    def check_date(value: str) -> None:
        if not DATE_RE.match(value):
            raise HTTPException(404)

    def check_hx_request(request: Request) -> None:
        # htmx always sends this header; a cross-site form or fetch can't set it without a CORS
        # preflight, so its absence means the request did not come from our own page.
        if "hx-request" not in request.headers:
            raise HTTPException(403)

    dd_router = deepdive_routes.make_router(
        settings=settings, conn=conn, templates=templates, clock=clock, start_render=start_render,
        check_token=check_token, check_hx_request=check_hx_request,
    )
    app.include_router(dd_router)

    def episode_view(row) -> dict:
        script = json.loads(row["script_json"])
        sources = {r["item_id"]: dict(r) for r in db.episode_sources(conn, row["date"])}
        version = int(datetime.fromisoformat(row["updated_at"]).timestamp())
        return {
            "date": row["date"],
            "title": podcast.episode_title(row["date"]),
            "duration": _duration(row["duration_s"]),
            "audio_url": f"/audio/{settings.feed_token}/{row['date']}.mp3?v={version}",
            "intro": script["intro"],
            "outro": script["outro"],
            "segments": [
                {"headline": s["headline"], "text": s["text"],
                 "sources": [sources[i] for i in s["item_ids"] if i in sources]}
                for s in script["segments"]
            ],
        }

    def today_view(force_generating: bool = False) -> dict:
        if not settings.daily_brief:
            return {"state": "off", "title": podcast.episode_title(today_str())}
        date = today_str()
        episode = db.get_episode(conn, date)
        run = db.latest_run_for(conn, date)
        view = {"title": podcast.episode_title(date), "run_at": settings.run_at.strftime("%-I:%M %p"),
                "episode": None, "error": None, "stage": "starting", "feed_errors": []}
        if force_generating or (run and run["status"] == "running"):
            view["state"] = "generating"
            if run and run["status"] == "running":
                view["stage"] = run["stage"]
        elif episode:
            view["state"] = "ready"
            view["episode"] = episode_view(episode)
            if run and run["status"] == "failed" and run["started_at"] > episode["updated_at"]:
                view["error"] = run["error"]
        elif run and run["status"] == "failed":
            view["state"] = "failed"
            view["error"] = run["error"]
            view["feed_errors"] = json.loads(run["feed_errors"])
        else:
            view["state"] = "waiting"
        return view

    def month_cost() -> float:
        first = clock().astimezone(TZ).date().replace(day=1).isoformat()
        total = 0.0
        for row in db.usage_by_model(conn, since_date=first):
            price_in, price_out = PRICES.get(row["model"], (0.0, 0.0))
            total += row["input_tokens"] / 1e6 * price_in + row["output_tokens"] / 1e6 * price_out
        return total

    def render_today(request: Request, force_generating: bool = False) -> HTMLResponse:
        return templates.TemplateResponse(request, "partials/today.html",
                                          {"today": today_view(force_generating)})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        today = today_str()
        last = db.latest_run(conn)
        return templates.TemplateResponse(request, "index.html", {
            "today": today_view(),
            "past": [episode_view(r) for r in db.list_episodes(conn) if r["date"] < today],
            "feed_url": f"{settings.public_base_url}/feed/{settings.feed_token}.xml",
            "deep_feed_url": f"{settings.public_base_url}/feed/deep-dives/{settings.feed_token}.xml",
            "dd": dd_router.section_view(),
            "month_cost": month_cost(),
            "last_run": f"{last['status']} {_local(last['started_at'])}" if last else None,
            "claude_offline": settings.claude_offline,
        })

    @app.get("/partials/today", response_class=HTMLResponse)
    def today_partial(request: Request):
        return render_today(request)

    @app.post("/generate", response_class=HTMLResponse)
    def generate(request: Request):
        check_hx_request(request)
        if not settings.daily_brief:
            raise HTTPException(409, "daily brief is off")
        started = app.state.start_run("manual")
        return render_today(request, force_generating=started or pipeline.is_running())

    @app.delete("/episodes/{date}", response_class=HTMLResponse)
    def delete_episode(request: Request, date: str):
        check_hx_request(request)
        check_date(date)
        retention.delete_episode(conn, settings.audio_dir, date)
        if date == today_str():
            return render_today(request)
        return HTMLResponse("")

    @app.get("/feed/cover.png")
    def cover():
        return FileResponse(COVER, media_type="image/png")

    @app.get("/feed/{token}.xml")
    def feed(token: str):
        check_token(token)
        episodes = []
        for row in db.list_episodes(conn):
            sources = [dict(r) for r in db.episode_sources(conn, row["date"])]
            episodes.append({**dict(row), "description": podcast.describe(json.loads(row["script_json"]), sources)})
        xml = podcast.build_feed(episodes, settings.public_base_url, settings.feed_token)
        return Response(xml, media_type="application/rss+xml")

    @app.get("/audio/{token}/{date}.mp3")
    def audio(token: str, date: str):
        check_token(token)
        check_date(date)
        path = settings.audio_dir / f"{date}.mp3"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/mpeg")

    return app
