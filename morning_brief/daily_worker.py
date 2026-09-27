"""Daily brief written by the Mac worker (DAILY_BRIEF=worker).

The server gathers candidate stories from the RSS feeds into a daily job, hands them to the worker on claim,
validates the script it submits, then speaks and publishes it. At READY_BY a missing brief sends one push.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date as date_cls, datetime, timedelta
from typing import Callable

import httpx

from . import articles, db, feeds, pipeline, retention, speech, window, writer
from .config import TZ, Settings
from .deepdives import _has_control_chars
from .models import SEGMENTS

log = logging.getLogger(__name__)
MODEL = "mac-worker"
CLAIM_TTL = timedelta(minutes=15)
TARGET_WORDS = 550
SUMMARY_WORDS = 60
LOCK_WAIT_S, LOCK_RETRY_S = 30 * 60, 60
CLAIM_KEYS = ("id", "segment", "source", "published", "text", "title", "summary", "url")
ERROR_MAX = 500
HEADLINE_MAX = 200
MAX_PER_SEGMENT = 15
_lock = threading.RLock()  # job transitions on the shared connection
_gather_lock = threading.Lock()  # one gather at a time; independent of the Kokoro run_lock


def _local_date(now: datetime) -> str:
    return now.astimezone(TZ).date().isoformat()


def _kind(item) -> str:
    if len(item.body.split()) >= articles.MIN_FULL_WORDS:
        return "feed"
    return "page" if item.fetch_pages else "summary"


def get_job(conn, episode_date: str):
    return conn.execute("SELECT * FROM daily_jobs WHERE date = ?", (episode_date,)).fetchone()


def _in_progress_message(status: str) -> str:
    return ("today's brief is already being written" if status == "claimed"
            else "today's brief is already being spoken")


def _cap_per_segment(items: list) -> list:
    """The first MAX_PER_SEGMENT items of each segment, in order (select_window sorts newest first)."""
    kept, counts = [], {}
    for it in items:
        counts[it.segment] = counts.get(it.segment, 0) + 1
        if counts[it.segment] <= MAX_PER_SEGMENT:
            kept.append(it)
    return kept


def gather(settings: Settings, conn, http, now: datetime, trigger: str) -> str:
    """Fetch feeds and store today's candidates as a 'waiting' job. Returns the date; raises on failure."""
    episode_date = _local_date(now)
    existing = get_job(conn, episode_date)
    if existing is not None:
        if existing["status"] in ("claimed", "speaking"):
            raise pipeline.PipelineError(_in_progress_message(existing["status"]))
        if existing["status"] == "done" and trigger != "manual":
            raise pipeline.PipelineError("today's brief is already published")
    run_id = db.start_run(conn, date=episode_date, trigger=trigger, model=MODEL, started_at=now.isoformat())
    try:
        db.set_stage(conn, run_id, "fetching")
        items, errors = feeds.fetch_all(feeds.load_sources(settings.feeds_path), http)
        db.set_feed_errors(conn, run_id, errors)
        previous = db.previous_episode(conn, before_date=episode_date)
        previous_cutoff = datetime.fromisoformat(previous["cutoff_at"]) if previous else None
        chosen = window.select_window(items, window.window_start(previous_cutoff, now))
        if not chosen:
            if not items and errors:
                raise pipeline.PipelineError("every feed failed")
            raise pipeline.PipelineError("no new items since the last episode")
        chosen = _cap_per_segment(chosen)
        candidates = [{"id": it.id, "segment": it.segment, "source": it.source, "title": it.title,
                       "summary": " ".join(it.summary.split()[:SUMMARY_WORDS]), "url": it.url,
                       "published": writer._local(it.published_at), "published_at": it.published_at.isoformat(),
                       "text": _kind(it)} for it in chosen]
        bodies = {it.id: it.body for it in chosen if _kind(it) == "feed"}
        headlines = [s["headline"] for s in json.loads(previous["script_json"])["segments"]] if previous else []
        with _lock:
            old = get_job(conn, episode_date)
            if old is not None and old["status"] in ("claimed", "speaking"):
                raise pipeline.PipelineError(_in_progress_message(old["status"]))
            if old is not None:
                run = conn.execute("SELECT status FROM runs WHERE id = ?", (old["run_id"],)).fetchone()
                if run is not None and run["status"] == "running":
                    db.finish_run(conn, old["run_id"], "failed", now.isoformat(), error="replaced by a new gather")
            conn.execute(
                "INSERT OR REPLACE INTO daily_jobs (date, run_id, status, candidates_json, bodies_json, "
                "previous_json, script_json, cutoff_at, claimed_at, error, created_at, updated_at) "
                "VALUES (?, ?, 'waiting', ?, ?, ?, NULL, ?, NULL, NULL, ?, ?)",
                (episode_date, run_id, json.dumps(candidates), json.dumps(bodies), json.dumps(headlines),
                 now.isoformat(), now.isoformat(), now.isoformat()))
            db.set_stage(conn, run_id, "waiting")
        return episode_date
    except Exception as exc:
        db.finish_run(conn, run_id, "failed", now.isoformat(), error=f"{type(exc).__name__}: {exc}"[:ERROR_MAX])
        raise


def _http() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": feeds.USER_AGENT}, timeout=20, follow_redirects=True)


def gather_run(settings: Settings, trigger: str, *, http_factory: Callable | None = None,
               now: Callable[[], datetime] = pipeline.utc_now) -> str | None:
    """gather() under the gather lock with its own connection. Never raises; returns the date or None."""
    if not _gather_lock.acquire(blocking=False):
        log.info("skipped daily gather (%s): a gather is already running", trigger)
        return None
    try:
        http = (http_factory or _http)()
        try:
            conn = db.connect(settings.db_path)
            try:
                return gather(settings, conn, http, now(), trigger)
            finally:
                conn.close()
        finally:
            http.close()
    except pipeline.PipelineError as exc:
        log.info("daily gather (%s) stopped: %s", trigger, exc)
    except Exception:
        log.exception("daily gather (%s) failed", trigger)
    finally:
        _gather_lock.release()
    return None


def claim(conn, now: datetime, location: str) -> dict | None:
    """Hand today's job to the worker if it is waiting, or claimed more than CLAIM_TTL ago."""
    episode_date = _local_date(now)
    with _lock:
        job = get_job(conn, episode_date)
        if job is None:
            return None
        stale = (job["status"] == "claimed" and job["claimed_at"] is not None
                 and datetime.fromisoformat(job["claimed_at"]) < now - CLAIM_TTL)
        if job["status"] != "waiting" and not stale:
            return None
        cur = conn.execute(
            "UPDATE daily_jobs SET status = 'claimed', claimed_at = ?, updated_at = ? "
            "WHERE date = ? AND status IN ('waiting', 'claimed')",
            (now.isoformat(), now.isoformat(), episode_date))
        if cur.rowcount == 0:
            return None
        db.set_stage(conn, job["run_id"], "writing")
    local = now.astimezone(TZ)
    return {
        "date": episode_date,
        "today": f"{local:%A, %B} {local.day}, {local.year}",
        "location": location,
        "now": writer._local(now),
        "previous_headlines": json.loads(job["previous_json"]),
        "target_words": TARGET_WORDS,
        "candidates": [{k: c[k] for k in CLAIM_KEYS} for c in json.loads(job["candidates_json"])],
    }


def item_text(conn, episode_date: str, item: str) -> str | None:
    job = get_job(conn, episode_date)
    if job is None or job["status"] not in ("waiting", "claimed"):
        return None
    return json.loads(job["bodies_json"]).get(item)


def known_ids(job) -> set[str]:
    return {c["id"] for c in json.loads(job["candidates_json"])}


def validate_submission(script, known: set[str]) -> list[str]:
    """Shape checks for a worker-written daily script, then the same rules as the API writer."""
    if not isinstance(script, dict):
        return ["the body must be a JSON object"]
    problems = []
    for key in ("intro", "outro"):
        value = script.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"'{key}' must be a non-empty string")
        elif _has_control_chars(value):
            problems.append(f"'{key}' contains control characters")
    segments = script.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= writer.MAX_STORIES:
        problems.append(f"'segments' must be a list of 1 to {writer.MAX_STORIES} stories")
        return problems
    for n, seg in enumerate(segments, start=1):
        if not isinstance(seg, dict):
            problems.append(f"segment {n} must be an object")
            continue
        if seg.get("segment") not in SEGMENTS:
            problems.append(f"segment {n} 'segment' must be one of {', '.join(SEGMENTS)}")
        for key in ("headline", "text"):
            value = seg.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"segment {n} needs a non-empty '{key}'")
            elif _has_control_chars(value):
                problems.append(f"segment {n} {key} contains control characters")
            elif key == "headline" and len(value) > HEADLINE_MAX:
                problems.append(f"segment {n} headline must be at most {HEADLINE_MAX} characters")
        ids = seg.get("item_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            problems.append(f"segment {n} 'item_ids' must be a list of candidate ids")
    if problems:
        return problems
    return writer.validate_script(script, known)


def _clean_script(script: dict) -> dict:
    """Only the fields the app itself wrote and reads back — never the worker's raw dict verbatim."""
    return {
        "intro": script.get("intro"),
        "outro": script.get("outro"),
        "segments": [{"segment": seg.get("segment"), "headline": seg.get("headline"),
                      "text": seg.get("text"), "item_ids": seg.get("item_ids")}
                     for seg in script.get("segments", [])],
    }


def accept(conn, episode_date: str, script: dict, now: datetime) -> bool:
    """claimed → speaking, storing the script. False if the job isn't claimed (any more)."""
    with _lock:
        cur = conn.execute(
            "UPDATE daily_jobs SET status = 'speaking', script_json = ?, updated_at = ? "
            "WHERE date = ? AND status = 'claimed'",
            (json.dumps(_clean_script(script)), now.isoformat(), episode_date))
        if cur.rowcount == 0:
            return False
        db.set_stage(conn, get_job(conn, episode_date)["run_id"], "speaking")
        return True


def fail_job(conn, episode_date: str, reason: str, now: datetime, *, from_status=("claimed",)) -> bool:
    with _lock:
        job = get_job(conn, episode_date)
        if job is None or job["status"] not in from_status:
            return False
        cur = conn.execute(
            "UPDATE daily_jobs SET status = 'failed', error = ?, updated_at = ? WHERE date = ? AND status = ?",
            (reason[:ERROR_MAX], now.isoformat(), episode_date, job["status"]))
        if cur.rowcount == 0:
            return False
        db.finish_run(conn, job["run_id"], "failed", now.isoformat(), error=reason[:ERROR_MAX])
        return True


def _publish_locked(settings: Settings, conn, episode_date: str, synthesize: Callable, now: Callable) -> str:
    job = get_job(conn, episode_date)
    if job is None or job["status"] != "speaking":
        return "gone"
    script = json.loads(job["script_json"])
    cited = {i for seg in script["segments"] for i in seg["item_ids"]}
    sources = [{"item_id": c["id"], "source": c["source"], "title": c["title"], "url": c["url"],
                "published_at": c["published_at"]} for c in json.loads(job["candidates_json"]) if c["id"] in cited]
    pipeline.speak_and_store(settings, conn, job["run_id"], episode_date, job["cutoff_at"], script, sources,
                             synthesize, now)
    try:
        with _lock:
            conn.execute("UPDATE daily_jobs SET status = 'done', updated_at = ? WHERE date = ? AND status = 'speaking'",
                         (now().isoformat(), episode_date))
        db.finish_run(conn, job["run_id"], "succeeded", now().isoformat())
        retention.prune(conn, settings.audio_dir, date_cls.fromisoformat(episode_date))
    except Exception:
        log.exception("daily %s: post-publish bookkeeping failed", episode_date)
    return "ready"


def speak_and_publish(settings: Settings, episode_date: str, *, synthesize: Callable | None = None,
                      now: Callable[[], datetime] = pipeline.utc_now, lock_wait_s: float = LOCK_WAIT_S,
                      lock_retry_s: float = LOCK_RETRY_S, sleep: Callable[[float], None] = time.sleep) -> str:
    """Speak and publish an accepted daily script. Returns 'ready', 'failed' or 'gone'."""
    synthesize = synthesize or (speech.fake_synthesize if settings.fake_speech else speech.synthesize)
    conn = None
    try:
        conn = db.connect(settings.db_path)
        waited = 0.0
        while True:
            try:
                with pipeline.run_lock(settings):
                    return _publish_locked(settings, conn, episode_date, synthesize, now)
            except pipeline.RunInProgress:
                if waited >= lock_wait_s:
                    fail_job(conn, episode_date, f"speech lock busy for {int(lock_wait_s // 60)} minutes; "
                             "press Regenerate", now(), from_status=("speaking",))
                    return "failed"
                sleep(lock_retry_s)
                waited += lock_retry_s
    except Exception as exc:
        log.exception("daily %s: speech or publish failed", episode_date)
        if conn is not None:
            try:
                fail_job(conn, episode_date, f"{type(exc).__name__}: {exc}", now(), from_status=("speaking",))
            except Exception:
                log.exception("daily %s: could not mark the job failed", episode_date)
        return "failed"
    finally:
        if conn is not None:
            conn.close()


def start_publish(settings: Settings, episode_date: str, **kwargs) -> threading.Thread:
    thread = threading.Thread(target=speak_and_publish, args=(settings, episode_date), kwargs=kwargs,
                              name=f"daily-{episode_date}", daemon=True)
    thread.start()
    return thread


def check_missed(settings: Settings, conn, now: datetime, notify) -> bool:
    """At READY_BY: if today has no episode and nothing is on its way, mark it missed and push once."""
    episode_date = _local_date(now)
    if db.get_episode(conn, episode_date) is not None or db.get_state(conn, "daily_missed_date") == episode_date:
        return False
    with _lock:
        job = get_job(conn, episode_date)
        if job is not None and job["status"] in ("done", "speaking", "missed"):
            return False
        reason = job["error"] if job is not None and job["status"] == "failed" else None
        if job is None:
            run = db.latest_run_for(conn, episode_date)
            if run is not None and run["status"] == "failed" and run["error"] not in (None, "interrupted"):
                reason = run["error"]
        else:
            conn.execute(
                "UPDATE daily_jobs SET status = 'missed', updated_at = ? "
                "WHERE date = ? AND status NOT IN ('done', 'speaking', 'missed')",
                (now.isoformat(), episode_date))
            run = conn.execute("SELECT status FROM runs WHERE id = ?", (job["run_id"],)).fetchone()
            if run is not None and run["status"] == "running":
                db.finish_run(conn, job["run_id"], "failed", now.isoformat(), error="missed")
        db.set_state(conn, "daily_missed_date", episode_date)
    if job is not None:
        message = f"Couldn't make it: {reason}" if reason else "The Mac worker didn't run."
    else:
        message = f"Couldn't gather stories: {reason}" if reason else "No stories were gathered."
    try:
        notify(settings, "No brief today", message, ["warning"])
    except Exception:
        log.exception("missed-brief push failed")
    return True


def fail_interrupted(conn, now: datetime) -> int:
    """A restart killed speech for these jobs; fail them (the run too) so the page offers Regenerate.

    Waiting or claimed jobs from earlier days were never written; fail them as missed so their runs finish."""
    count = 0
    for row in conn.execute("SELECT date FROM daily_jobs WHERE status = 'speaking'").fetchall():
        count += fail_job(conn, row["date"], "interrupted during speech; press Regenerate", now,
                          from_status=("speaking",))
    for row in conn.execute("SELECT date FROM daily_jobs WHERE status IN ('waiting', 'claimed') AND date < ?",
                            (_local_date(now),)).fetchall():
        count += fail_job(conn, row["date"], "missed", now, from_status=("waiting", "claimed"))
    return count
