import time
from dataclasses import replace

from morning_brief import deepdive_render, deepdives, pipeline, speech
from tests.helpers import NOW, deep_dive_script


def speaking_topic(conn, script=None):
    topic_id = deepdives.add_topic(conn, "The Fed", "", NOW)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, topic_id, script or deep_dive_script(2400), NOW)
    return topic_id


def test_render_publishes(conn, settings):
    topic_id = speaking_topic(conn)
    status = deepdive_render.render(settings, topic_id, synthesize=speech.fake_synthesize, now=lambda: NOW)
    assert status == "ready"
    row = [r for r in deepdives.list_topics(conn) if r["id"] == topic_id][0]
    assert (row["status"], row["episode_title"], row["word_count"]) == ("ready", "How the Fed Began", 2400)
    assert row["render_attempts"] == 1
    path = deepdives.audio_path(settings.deep_dives_dir, topic_id)
    assert path.stat().st_size == row["audio_bytes"] and not list(settings.deep_dives_dir.glob("*.tmp"))


def test_speech_error_fails_topic_and_keeps_script(conn, settings):
    topic_id = speaking_topic(conn)

    def broken(passages, voice, models_dir):
        raise RuntimeError("espeak exploded")

    assert deepdive_render.render(settings, topic_id, synthesize=broken, now=lambda: NOW) == "failed"
    row = deepdives.get_topic(conn, topic_id)
    assert row["status"] == "failed" and "espeak exploded" in row["error"] and row["script_json"]
    assert deepdives.retry(conn, topic_id, NOW) == "speaking"


def test_deleted_topic_is_gone(conn, settings):
    topic_id = speaking_topic(conn)
    deepdives.delete_topic(conn, settings.deep_dives_dir, topic_id)
    assert deepdive_render.render(settings, topic_id, synthesize=speech.fake_synthesize) == "gone"


def test_busy_lock_waits_then_fails(conn, settings):
    topic_id = speaking_topic(conn)
    naps = []
    with pipeline.run_lock(settings):
        status = deepdive_render.render(settings, topic_id, synthesize=speech.fake_synthesize, now=lambda: NOW,
                                        lock_wait_s=3, lock_retry_s=1, sleep=naps.append)
    assert status == "failed" and naps == [1, 1, 1]
    assert "speech lock busy" in deepdives.get_topic(conn, topic_id)["error"]


def test_resume_rerenders_speaking_topics(conn, settings):
    fake = replace(settings, fake_speech=True)
    topic_id = speaking_topic(conn)
    assert deepdive_render.resume(fake, conn) == [topic_id]
    for _ in range(50):
        if deepdives.get_topic(conn, topic_id)["status"] == "ready":
            break
        time.sleep(0.1)
    assert deepdives.get_topic(conn, topic_id)["status"] == "ready"


def test_resume_passes_notify_through_to_render(conn, settings):
    fake = replace(settings, fake_speech=True)
    topic_id = speaking_topic(conn)
    calls = []
    notified = lambda s, title, message, tags: calls.append((title, message, tags))
    assert deepdive_render.resume(fake, conn, notify=notified) == [topic_id]
    for _ in range(50):
        if deepdives.get_topic(conn, topic_id)["status"] == "ready":
            break
        time.sleep(0.1)
    assert deepdives.get_topic(conn, topic_id)["status"] == "ready"
    assert calls and calls[0][0] == "Deep dive ready"


def test_resume_fails_interrupted_topics_and_only_renders_fresh_ones(conn, settings):
    fake = replace(settings, fake_speech=True)
    interrupted = speaking_topic(conn)
    deepdives.begin_render(conn, interrupted, NOW)  # a previous render started (attempts -> 1)
    fresh = speaking_topic(conn, script=deep_dive_script(2400, title="Other"))
    assert deepdive_render.resume(fake, conn) == [fresh]
    row = deepdives.get_topic(conn, interrupted)
    assert row["status"] == "failed"
    assert "interrupted during speech; press Retry" in row["error"]
    for _ in range(50):
        if deepdives.get_topic(conn, fresh)["status"] == "ready":
            break
        time.sleep(0.1)
    assert deepdives.get_topic(conn, fresh)["status"] == "ready"


def test_render_catches_exception_before_lock_and_fails_topic(conn, settings, monkeypatch):
    topic_id = speaking_topic(conn)

    def boom(_settings):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "run_lock", boom)
    status = deepdive_render.render(settings, topic_id, synthesize=speech.fake_synthesize, now=lambda: NOW)
    assert status == "failed"
    row = deepdives.get_topic(conn, topic_id)
    assert row["status"] == "failed" and "disk full" in row["error"]


def test_orphan_mp3_removed_when_topic_deleted_during_render_error(conn, settings, monkeypatch):
    topic_id = speaking_topic(conn)
    final = deepdives.audio_path(settings.deep_dives_dir, topic_id)

    def broken_publish(c, tid, **kwargs):
        c.execute("DELETE FROM topics WHERE id = ?", (tid,))  # simulates a concurrent delete
        raise RuntimeError("db exploded")

    monkeypatch.setattr(deepdives, "publish", broken_publish)
    status = deepdive_render.render(settings, topic_id, synthesize=speech.fake_synthesize, now=lambda: NOW)
    assert status == "failed"
    assert not final.exists()


def recorder():
    calls = []
    return calls, lambda s, title, message, tags: calls.append((title, message, tags))


def test_ready_push_after_publish(conn, settings):
    topic_id = speaking_topic(conn)
    calls, notify = recorder()
    fake = lambda passages, voice, models_dir: (b"\xff\xf3", 17 * 60 + 20.0)
    assert deepdive_render.render(settings, topic_id, synthesize=fake, now=lambda: NOW, notify=notify) == "ready"
    assert calls == [("Deep dive ready", "How the Fed Began (17 min)", ["headphones"])]


def test_no_push_on_failure_or_deletion(conn, settings):
    calls, notify = recorder()
    failing = speaking_topic(conn)

    def broken(passages, voice, models_dir):
        raise RuntimeError("boom")

    assert deepdive_render.render(settings, failing, synthesize=broken, now=lambda: NOW, notify=notify) == "failed"
    gone = speaking_topic(conn)
    deepdives.delete_topic(conn, settings.deep_dives_dir, gone)
    assert deepdive_render.render(settings, gone, synthesize=speech.fake_synthesize, notify=notify) == "gone"
    assert calls == []


def test_push_error_does_not_fail_the_episode(conn, settings):
    topic_id = speaking_topic(conn)

    def exploding(*args):
        raise RuntimeError("ntfy down")

    status = deepdive_render.render(settings, topic_id, synthesize=speech.fake_synthesize, now=lambda: NOW,
                                    notify=exploding)
    assert status == "ready" and deepdives.get_topic(conn, topic_id)["status"] == "ready"
