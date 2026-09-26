from __future__ import annotations

import logging
import threading
from datetime import datetime, time

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, deepdives, pipeline, retention
from .config import Settings, TZ

log = logging.getLogger(__name__)


def start_background_run(settings: Settings, trigger: str, deps_factory=None) -> bool:
    """Start a pipeline run on a thread. Returns False if one is already running."""
    if pipeline.is_running():
        return False
    factory = deps_factory or pipeline.default_deps

    def target():
        deps = factory(settings)
        try:
            pipeline.run_episode(deps, trigger)
        except pipeline.RunInProgress:
            log.info("skipped %s run: another run is in progress", trigger)
        except Exception:
            log.exception("%s run crashed", trigger)
        finally:
            deps.http.close()

    threading.Thread(target=target, name=f"run-{trigger}", daemon=True).start()
    return True


def should_catch_up(now_local: datetime, run_at: time, has_episode_today: bool, running: bool) -> bool:
    return not has_episode_today and not running and now_local.time() >= run_at


def catch_up(settings: Settings, conn, clock, start_run) -> bool:
    if not settings.daily_brief:
        return False
    now_local = clock().astimezone(TZ)
    date = now_local.date().isoformat()
    has_episode = db.get_episode(conn, date) is not None
    if not should_catch_up(now_local, settings.run_at, has_episode, pipeline.is_running()):
        return False
    latest = db.latest_run_for(conn, date)
    if latest is not None and latest["status"] == "failed" and latest["error"] == "interrupted":
        log.warning("skipped catch-up run for %s: the latest run today failed with 'interrupted'", date)
        return False
    failed = db.failed_run_count(conn, date)
    if failed >= 2:
        log.warning("skipped catch-up run for %s: %d failed runs today already", date, failed)
        return False
    return start_run("catchup")


def retry_if_missing(conn, clock, start_run) -> bool:
    if db.get_episode(conn, clock().astimezone(TZ).date().isoformat()) is None:
        return start_run("retry")
    return False


def prune_job(conn, settings: Settings, clock) -> None:
    now = clock()
    retention.prune(conn, settings.audio_dir, now.astimezone(TZ).date())
    result = deepdives.housekeeping(conn, settings.deep_dives_dir, now)
    log.info("deep-dive housekeeping: %s", result)


def build(settings: Settings, conn, clock, start_run) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=TZ)
    common = {"misfire_grace_time": 3600, "coalesce": True, "replace_existing": True}
    if settings.daily_brief:
        sched.add_job(start_run, "cron", args=["schedule"], id="daily",
                      hour=settings.run_at.hour, minute=settings.run_at.minute, **common)
        sched.add_job(retry_if_missing, "cron", args=[conn, clock, start_run], id="retry",
                      hour=settings.retry_at.hour, minute=settings.retry_at.minute, **common)
    sched.add_job(prune_job, "cron", args=[conn, settings, clock], id="prune", hour=3, minute=0, **common)
    return sched
