from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

from . import articles, db, feeds, id3, music, podcast, retention, speech, window, writer
from .config import TZ, Settings

log = logging.getLogger(__name__)
_lock = threading.Lock()


class RunInProgress(Exception):
    pass


class PipelineError(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Deps:
    settings: Settings
    claude: object
    http: httpx.Client
    synthesize: Callable = speech.synthesize
    now: Callable[[], datetime] = utc_now


def default_deps(settings: Settings) -> Deps:
    if settings.claude_offline:
        from .offline import OfflineClaude
        claude = OfflineClaude()
    else:
        import anthropic
        claude = anthropic.Anthropic(max_retries=3)

    return Deps(
        settings=settings,
        claude=claude,
        http=httpx.Client(headers={"User-Agent": feeds.USER_AGENT}, timeout=20, follow_redirects=True),
        synthesize=speech.fake_synthesize if settings.fake_speech else speech.synthesize,
    )


def is_running() -> bool:
    return _lock.locked()


@contextmanager
def run_lock(settings: Settings):
    """Hold the in-process thread lock together with a cross-process flock on data_dir/run.lock.

    The thread lock alone doesn't stop a second OS process (e.g. `docker compose exec ... python -m
    morning_brief.run`) from starting a concurrent run and loading a second Kokoro instance.
    """
    if not _lock.acquire(blocking=False):
        raise RunInProgress()
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(settings.data_dir / "run.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RunInProgress() from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    finally:
        _lock.release()


def run_episode(deps: Deps, trigger: str) -> int:
    """Generate and publish today's episode. Returns the run id; failures are recorded on the run."""
    with run_lock(deps.settings):
        return _run(deps, trigger)


def _run(deps: Deps, trigger: str) -> int:
    settings = deps.settings
    conn = db.connect(settings.db_path)
    try:
        started = deps.now()
        episode_date = started.astimezone(TZ).date()
        run_id = db.start_run(conn, date=episode_date.isoformat(), trigger=trigger, model=settings.model,
                              started_at=started.isoformat())
        # Persist token usage after each Claude call (not only at run end) so a killed process
        # (Kokoro/espeak segfault, OOM) still leaves the spend on the run row.
        usage = writer.Usage(on_add=lambda i, o: db.add_usage(conn, run_id, i, o))
        try:
            _generate(deps, conn, run_id, episode_date.isoformat(), started, usage)
        except Exception as exc:
            log.exception("run %s failed", run_id)
            db.finish_run(conn, run_id, "failed", deps.now().isoformat(), error=f"{type(exc).__name__}: {exc}"[:500])
        else:
            db.finish_run(conn, run_id, "succeeded", deps.now().isoformat())
            retention.prune(conn, settings.audio_dir, episode_date)
        return run_id
    finally:
        conn.close()


def _generate(deps: Deps, conn, run_id: int, episode_date: str, started: datetime, usage: writer.Usage) -> None:
    settings = deps.settings

    db.set_stage(conn, run_id, "fetching")
    items, errors = feeds.fetch_all(feeds.load_sources(settings.feeds_path), deps.http)
    db.set_feed_errors(conn, run_id, errors)
    previous = db.previous_episode(conn, before_date=episode_date)
    previous_cutoff = datetime.fromisoformat(previous["cutoff_at"]) if previous else None
    candidates = window.select_window(items, window.window_start(previous_cutoff, started))
    if not candidates:
        raise PipelineError("no new items since the last episode")

    db.set_stage(conn, run_id, "selecting")
    previous_headlines = [s["headline"] for s in json.loads(previous["script_json"])["segments"]] if previous else []
    picks = writer.select_stories(deps.claude, settings.model, candidates, previous_headlines, started, usage,
                                  location=settings.listener_location)

    db.set_stage(conn, run_id, "reading")
    by_id = {item.id: item for item in candidates}
    stories = articles.build_stories(picks, by_id, deps.http, articles.RobotsCache(deps.http))
    for s in stories:
        log.info("story %s [%s] %s, %d words from %s", s.story_id, s.segment,
                 "full" if s.full else "summary-only", len(s.text.split()),
                 ", ".join(it.source for it in s.items))

    db.set_stage(conn, run_id, "writing")
    script = writer.write_script(deps.claude, settings.model, stories, started, usage)

    db.set_stage(conn, run_id, "speaking")
    audio = deps.synthesize(speech.script_passages(script), settings.voice, settings.models_dir,
                            music.stings(settings, speech.SAMPLE_RATE))
    duration = audio.duration
    mp3, _ = id3.try_tag(audio.mp3, title=podcast.episode_title(episode_date),
                         chapters=lambda: speech.script_chapters(script, audio.starts), duration=duration)

    db.set_stage(conn, run_id, "publishing")
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    final = settings.audio_dir / f"{episode_date}.mp3"
    partial = settings.audio_dir / f"{episode_date}.mp3.tmp"
    backup = final.with_name(final.name + ".bak")
    partial.write_bytes(mp3)
    if final.exists():
        backup.unlink(missing_ok=True)
        os.link(final, backup)
    os.replace(partial, final)
    cited = {i for seg in script["segments"] for i in seg["item_ids"]}
    try:
        db.publish_episode(
            conn, date=episode_date, cutoff_at=started.isoformat(), script_json=json.dumps(script),
            word_count=writer.script_word_count(script), duration_s=duration, audio_bytes=len(mp3),
            now=deps.now().isoformat(),
            sources=[{"item_id": it.id, "source": it.source, "title": it.title, "url": it.url,
                      "published_at": it.published_at.isoformat()} for it in candidates if it.id in cited],
        )
    except Exception:
        if backup.exists():
            os.replace(backup, final)
        else:
            final.unlink(missing_ok=True)
        raise
    else:
        backup.unlink(missing_ok=True)
