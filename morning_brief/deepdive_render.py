"""Speak an accepted deep-dive script with Kokoro and publish it. Runs on a background thread."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable

from . import db, deepdives, id3, music as music_mod, notify as notify_mod, pipeline, speech
from .config import Settings

log = logging.getLogger(__name__)
LOCK_WAIT_S = 30 * 60
LOCK_RETRY_S = 60


def render(settings: Settings, topic_id: int, *, synthesize: Callable | None = None,
           now: Callable[[], datetime] = pipeline.utc_now, lock_wait_s: float = LOCK_WAIT_S,
           lock_retry_s: float = LOCK_RETRY_S, sleep: Callable[[float], None] = time.sleep,
           notify: Callable | None = None) -> str:
    """Speak and publish one topic. Returns 'ready', 'failed', or 'gone' (deleted / not speaking)."""
    synthesize = synthesize or (speech.fake_synthesize if settings.fake_speech else speech.synthesize)
    notify = notify or notify_mod.send
    conn = None
    try:
        conn = db.connect(settings.db_path)
        waited = 0.0
        while True:
            try:
                with pipeline.run_lock(settings):
                    return _render_locked(settings, conn, topic_id, synthesize, now, notify)
            except pipeline.RunInProgress:
                if waited >= lock_wait_s:
                    if deepdives.get_topic(conn, topic_id) is not None:
                        deepdives.fail(conn, topic_id,
                                       f"speech lock busy for {int(lock_wait_s // 60)} minutes; press Retry", now())
                    return "failed"
                sleep(lock_retry_s)
                waited += lock_retry_s
    except Exception as exc:
        # Anything unexpected here (including db.connect or lock setup failing) must never leave the
        # topic silently stuck in 'speaking' without at least a log line.
        log.exception("deep dive %s: render crashed", topic_id)
        if conn is not None:
            try:
                if deepdives.get_topic(conn, topic_id) is not None:
                    deepdives.fail(conn, topic_id, f"{type(exc).__name__}: {exc}", now())
            except Exception:
                log.exception("deep dive %s: could not mark topic failed after crash", topic_id)
        return "failed"
    finally:
        if conn is not None:
            conn.close()


def _render_locked(settings: Settings, conn, topic_id: int, synthesize: Callable, now: Callable, notify: Callable) -> str:
    row = deepdives.get_topic(conn, topic_id)
    if row is None or row["status"] != "speaking" or not row["script_json"]:
        return "gone"
    script = json.loads(row["script_json"])
    if not deepdives.begin_render(conn, topic_id, now()):
        return "gone"
    final = deepdives.audio_path(settings.deep_dives_dir, topic_id)
    try:
        audio = synthesize(deepdives.passages(script), settings.voice, settings.models_dir,
                           music_mod.stings(settings, speech.SAMPLE_RATE))
        duration = audio.duration
        mp3, marks = id3.try_tag(audio.mp3, title=script["title"],
                                 chapters=lambda: deepdives.chapters(script, audio.starts), duration=duration)
        settings.deep_dives_dir.mkdir(parents=True, exist_ok=True)
        partial = final.with_name(final.name + ".tmp")
        partial.write_bytes(mp3)
        os.replace(partial, final)
        if deepdives.get_topic(conn, topic_id) is None:  # deleted while speaking
            final.unlink(missing_ok=True)
            return "gone"
        deepdives.publish(conn, topic_id, title=script["title"], word_count=deepdives.script_word_count(script),
                          duration_s=duration, audio_bytes=len(mp3), now=now(), chapters=marks)
    except Exception as exc:
        log.exception("deep dive %s: speech failed", topic_id)
        if deepdives.get_topic(conn, topic_id) is None:  # deleted mid-render; don't leave an orphan file
            final.unlink(missing_ok=True)
        else:
            deepdives.fail(conn, topic_id, f"{type(exc).__name__}: {exc}", now())
        return "failed"
    log.info("deep dive %s published: %d words, %.0f s of audio", topic_id,
             deepdives.script_word_count(script), duration)
    try:
        notify(settings, "Deep dive ready", f"{script['title']} ({round(duration / 60)} min)", ["headphones"])
    except Exception:
        log.exception("deep dive %s: ready push failed", topic_id)
    return "ready"


def start(settings: Settings, topic_id: int, **kwargs) -> threading.Thread:
    thread = threading.Thread(target=render, args=(settings, topic_id), kwargs=kwargs,
                              name=f"deep-dive-{topic_id}", daemon=True)
    thread.start()
    return thread


def resume(settings: Settings, conn, *, notify: Callable | None = None) -> list[int]:
    """A restart left these topics in 'speaking'. render_attempts >= 1 means a previous render was already
    under way when the process died (likely what killed it) — fail those instead of looping forever.
    render_attempts == 0 means the script was accepted but speech never started; render those, one after
    another on a single thread. Returns only the ids that will be (re-)rendered."""
    to_render = []
    for topic_id in deepdives.speaking_ids(conn):
        row = deepdives.get_topic(conn, topic_id)
        if row is None:
            continue
        if row["render_attempts"] >= 1:
            deepdives.fail(conn, topic_id, "interrupted during speech; press Retry", pipeline.utc_now())
        else:
            to_render.append(topic_id)
    if to_render:
        def target():
            for topic_id in to_render:
                render(settings, topic_id, notify=notify)

        threading.Thread(target=target, name="deep-dive-resume", daemon=True).start()
        log.info("resuming speech for deep dives %s", to_render)
    return to_render
