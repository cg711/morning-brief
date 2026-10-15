"""Make the House episode: compose from today's facts, speak it, store it, then mark what it read."""
from __future__ import annotations

import logging
import os
import threading
import time
import wave
from datetime import date, datetime
from typing import Callable

import numpy as np

from . import db, house, house_facts, house_script, id3, notify as notify_mod, pipeline, speech
from . import music as music_mod
from .config import TZ, Settings

log = logging.getLogger(__name__)
LOCK_WAIT_S, LOCK_RETRY_S = 30 * 60, 60
ERROR_KEY = "house_last_error"  # "<date>|<message>", or "" when the last attempt worked


def load_clip(path) -> speech.Clip:
    """A stored memo (24 kHz mono 16-bit WAV, made by the audio pipeline) as samples."""
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, speech.SAMPLE_RATE):
            raise ValueError("memo audio must be 24 kHz mono 16-bit")
        frames = w.readframes(w.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if not len(samples):
        raise ValueError("memo audio is empty")
    if not np.isfinite(samples).all():
        raise ValueError("memo audio has non-finite samples")
    return speech.Clip(samples)


def failed_day(conn) -> date | None:
    """The day whose scheduled attempt last failed and still has no episode, else None."""
    try:
        day = date.fromisoformat((db.get_state(conn, ERROR_KEY) or "").partition("|")[0])
    except ValueError:
        return None
    return day if house.get_episode(conn, day) is None else None


def _fail(settings: Settings, conn, day: date | None, message: str, notify: Callable) -> None:
    if conn is not None and day is not None:
        try:
            db.set_state(conn, ERROR_KEY, f"{day.isoformat()}|{message}"[:300])
        except Exception:
            log.exception("could not record the House brief failure")
    try:
        notify(settings, "House brief failed", message[:200], ["warning"])
    except Exception:
        log.exception("could not tell the owner the House brief failed")


def _supersede_failure(conn, result: str) -> None:
    """A later attempt that did not fail (an episode, or honestly nothing to say) ends any earlier failure's retry."""
    if result not in ("ready", "nothing"):
        return
    try:
        if db.get_state(conn, ERROR_KEY):
            db.set_state(conn, ERROR_KEY, "")
    except Exception:
        log.exception("could not clear the House brief failure")


def generate(settings: Settings, *, manual: bool = False, synthesize: Callable | None = None,
             now: Callable[[], datetime] = pipeline.utc_now, notify: Callable = notify_mod.send,
             lock_wait_s: float = LOCK_WAIT_S, lock_retry_s: float = LOCK_RETRY_S,
             sleep: Callable[[float], None] = time.sleep, day: date | None = None) -> str:
    """Returns 'ready', 'nothing' (no content, or a scheduled run when today's episode exists), 'busy' or 'failed'.

    `day` defaults to the local day of `now()`; the retry passes the day that failed (it may be yesterday)."""
    conn = None
    try:
        synthesize = synthesize or (speech.fake_synthesize if settings.fake_speech else speech.synthesize)
        day = day or now().astimezone(TZ).date()
        conn = db.connect(settings.db_path)
        waited = 0.0
        while True:
            try:
                with pipeline.run_lock(settings):
                    result = _generate_locked(settings, conn, day, manual, synthesize, now)
                _supersede_failure(conn, result)
                return result
            except pipeline.RunInProgress:
                if waited >= lock_wait_s:
                    _fail(settings, conn, day, "speech was busy", notify)
                    return "busy"
                sleep(lock_retry_s)
                waited += lock_retry_s
    except Exception as exc:
        log.exception("house brief failed")
        _fail(settings, conn, day, f"{type(exc).__name__}: {exc}", notify)
        return "failed"
    finally:
        if conn is not None:
            conn.close()


def _generate_locked(settings: Settings, conn, day: date, manual: bool, synthesize: Callable,
                     now: Callable[[], datetime]) -> str:
    if not manual and house.get_episode(conn, day) is not None:
        return "nothing"
    facts = house_facts.gather(conn, day)
    clips: dict[int, speech.Clip] = {}
    for memo in list(facts["memos"]):
        try:
            clips[memo["id"]] = load_clip(house.memo_path(settings.house_dir, memo["id"]))
        except Exception as exc:
            log.warning("house memo %s can't be used: %s", memo["id"], type(exc).__name__)
            house.reject_memo(conn, memo["id"], settings.house_dir)
            facts["memos"].remove(memo)
    if house_facts.is_empty(facts):
        return "nothing"
    script = house_script.compose(facts, day)
    passages, firsts = house_script.passages(script, clips)
    audio = synthesize(passages, settings.house_voice_name, settings.models_dir,
                       music_mod.stings(settings, speech.SAMPLE_RATE))
    mp3, marks = id3.try_tag(audio.mp3, title=script["title"],
                             chapters=lambda: house_script.chapters(script, audio.starts, firsts),
                             duration=audio.duration)
    settings.house_dir.mkdir(parents=True, exist_ok=True)
    final = house.episode_path(settings.house_dir, day)
    partial = final.with_name(final.name + ".tmp")
    partial.write_bytes(mp3)
    os.replace(partial, final)
    house.save_episode(conn, day, script, duration_s=audio.duration, audio_bytes=len(mp3),
                       word_count=house_script.word_count(script), chapters=marks, now=now())
    house.mark_announced(conn, facts, day)  # only once the audio and its row are safely stored
    db.set_state(conn, ERROR_KEY, "")
    log.info("house brief %s published: %.0f s of audio", day.isoformat(), audio.duration)
    return "ready"


def retry(settings: Settings, **kwargs) -> str:
    """The second chance: run only for a day whose scheduled attempt failed and has no episode.

    The failed day comes from the recorded error, since after a late-night run the retry fires past midnight."""
    conn = db.connect(settings.db_path)
    try:
        day = failed_day(conn)
    finally:
        conn.close()
    return generate(settings, day=day, **kwargs) if day else "nothing"


_running: threading.Thread | None = None
_start_lock = threading.Lock()


def start(settings: Settings, **kwargs) -> threading.Thread:
    """Generate now (the owner's button), in the background."""
    global _running
    with _start_lock:
        if _running is not None and _running.is_alive():
            return _running
        _running = threading.Thread(target=generate, args=(settings,), kwargs={"manual": True, **kwargs},
                                    name="house-brief", daemon=True)
        _running.start()
        return _running
