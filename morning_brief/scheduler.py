from __future__ import annotations

import logging
import threading
from datetime import datetime, time

from apscheduler.schedulers.background import BackgroundScheduler

from . import daily_worker, db, deepdives, notes, notify as notify_mod, pipeline, retention
from .config import Settings, TZ

log = logging.getLogger(__name__)


def start_background_run(settings: Settings, trigger: str, deps_factory=None) -> bool:
    """Start a pipeline run on a thread. Returns False if one is already running."""
    if settings.worker_mode:  # the gather doesn't use Kokoro, so a render holding run_lock doesn't block it
        threading.Thread(target=daily_worker.gather_run, args=(settings, trigger),
                         name=f"gather-{trigger}", daemon=True).start()
        return True
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
    if settings.worker_mode:
        now_local = clock().astimezone(TZ)
        date = now_local.date().isoformat()
        if not settings.run_at <= now_local.time() < settings.ready_by:
            return False
        if db.get_episode(conn, date) or daily_worker.get_job(conn, date) or pipeline.is_running():
            return False
        return start_run("catchup")
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
    log.info("notes housekeeping: %s", notes.housekeeping(conn, now.astimezone(TZ).date()))


def long_running_job(conn, settings: Settings, clock, notify) -> int:
    """Push once per stage for each topic running well past its normal time. Returns pushes sent."""
    now = clock()
    sent = 0
    for row in deepdives.long_running(conn, now):
        if not deepdives.mark_long_notified(conn, row["id"], row["status"]):
            continue
        try:
            notify(settings, "Deep dive running long",
                   f"{row['topic']}: {row['status']} for {deepdives.stage_minutes(row, now)} min", ["hourglass"])
            sent += 1
        except Exception:
            log.exception("deep dive %s: running-long push failed", row["id"])
    return sent


def ready_by_job(settings: Settings, conn, clock, notify) -> bool:
    return daily_worker.check_missed(settings, conn, clock(), notify)


def build(settings: Settings, conn, clock, start_run, notifier=None) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=TZ)
    common = {"misfire_grace_time": 3600, "coalesce": True, "replace_existing": True}
    if settings.worker_mode:
        sched.add_job(start_run, "cron", args=["schedule"], id="daily",
                      hour=settings.run_at.hour, minute=settings.run_at.minute, **common)
        sched.add_job(ready_by_job, "cron", args=[settings, conn, clock, notifier or notify_mod.send], id="ready_by",
                      hour=settings.ready_by.hour, minute=settings.ready_by.minute, **common)
    elif settings.daily_brief:
        sched.add_job(start_run, "cron", args=["schedule"], id="daily",
                      hour=settings.run_at.hour, minute=settings.run_at.minute, **common)
        sched.add_job(retry_if_missing, "cron", args=[conn, clock, start_run], id="retry",
                      hour=settings.retry_at.hour, minute=settings.retry_at.minute, **common)
    sched.add_job(prune_job, "cron", args=[conn, settings, clock], id="prune", hour=3, minute=0, **common)
    sched.add_job(long_running_job, "interval", args=[conn, settings, clock, notifier or notify_mod.send],
                  id="long_running", minutes=5, **common)
    return sched
