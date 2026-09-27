import fcntl
import json
import os
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest
from mutagen.id3 import ID3

from morning_brief import db, pipeline, speech
from morning_brief.feeds import item_id
from tests.helpers import NOW, FakeClaude, claude_reply, seed_episode

STORM = "https://wire.test/storm"
COUNCIL = "https://local.test/council"
OLD = "https://wire.test/old"


def rss(*items):
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><pubDate>{pub}</pubDate>"
        f"<description>{t} summary.</description>{extra}</item>"
        for t, link, pub, extra in items
    )
    return f'<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>x</title>{body}</channel></rss>'


FULL = "<content:encoded><![CDATA[" + " ".join(["council"] * 200) + "]]></content:encoded>"
FEEDS = {
    "https://wire.test/rss": rss(("Storm hits coast", STORM, "Fri, 25 Sep 2026 12:00:00 +0000", ""),
                                 ("Old news", OLD, "Mon, 21 Sep 2026 12:00:00 +0000", "")),
    "https://local.test/rss": rss(("Council passes budget", COUNCIL, "Fri, 25 Sep 2026 12:45:00 +0000", FULL)),
}


def handler(request):
    url = str(request.url)
    if url in FEEDS:
        return httpx.Response(200, text=FEEDS[url])
    return httpx.Response(500)


def select_reply(ids):
    return claude_reply({"stories": [
        {"story_id": f"s{n}", "segment": "headlines", "item_ids": [i], "reason": "r"} for n, i in enumerate(ids)
    ]})


def script_reply(ids, words=500):
    per = (words - 5) // len(ids)
    segs = [{"segment": "headlines", "headline": f"Story {n}", "text": " ".join(["word"] * per), "item_ids": [i]}
            for n, i in enumerate(ids)]
    segs[0]["text"] += " word" * ((words - 5) - per * len(ids))
    return claude_reply({"intro": "Good morning.", "segments": segs, "outro": "See you tomorrow."})


def deps_for(settings, claude, http_handler=handler):
    return pipeline.Deps(settings=settings, claude=claude,
                         http=httpx.Client(transport=httpx.MockTransport(http_handler)),
                         synthesize=speech.fake_synthesize, now=lambda: NOW)


def run_row(conn, run_id):
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def test_happy_path_publishes_episode(conn, settings):
    ids = [item_id(STORM), item_id(COUNCIL)]
    claude = FakeClaude([select_reply(ids), script_reply(ids)])
    run_id = pipeline.run_episode(deps_for(settings, claude), trigger="manual")

    run = run_row(conn, run_id)
    assert run["status"] == "succeeded" and run["stage"] == "publishing"
    assert json.loads(run["feed_errors"])[0].startswith("Broken Feed:")
    assert (run["input_tokens"], run["output_tokens"]) == (2000, 400)

    ep = db.get_episode(conn, "2026-09-25")
    assert ep["cutoff_at"] == NOW.isoformat() and ep["word_count"] == 500
    assert (settings.audio_dir / "2026-09-25.mp3").stat().st_size == ep["audio_bytes"]
    assert {r["url"] for r in db.episode_sources(conn, "2026-09-25")} == {STORM, COUNCIL}

    select_prompt = claude.calls[0]["messages"][0]["content"]
    assert "Storm hits coast" in select_prompt and "Old news" not in select_prompt
    write_prompt = claude.calls[1]["messages"][0]["content"]
    assert "(full text)" in write_prompt and "(summary-only)" in write_prompt

    tags = ID3(str(settings.audio_dir / "2026-09-25.mp3"))
    titles = [c.sub_frames["TIT2"].text[0] for c in sorted(tags.getall("CHAP"), key=lambda c: c.start_time)]
    assert titles == ["Introduction", "Story 0", "Story 1", "Wrap-up"]


def test_window_starts_at_previous_episode_cutoff(conn, settings):
    seed_episode(conn, settings, "2026-09-24", cutoff_at="2026-09-25T12:30:00+00:00")
    ids = [item_id(COUNCIL)]
    claude = FakeClaude([select_reply(ids), script_reply(ids)])
    pipeline.run_episode(deps_for(settings, claude), trigger="manual")
    prompt = claude.calls[0]["messages"][0]["content"]
    assert "Council passes budget" in prompt and "Storm hits coast" not in prompt
    assert "- Big news" in prompt


def test_failure_records_error_and_keeps_live_episode(conn, settings):
    seed_episode(conn, settings, "2026-09-25", audio=b"old-audio")
    claude = FakeClaude([claude_reply({"stories": []}, stop_reason="refusal", input_tokens=7, output_tokens=1)])
    run_id = pipeline.run_episode(deps_for(settings, claude), trigger="manual")
    run = run_row(conn, run_id)
    assert run["status"] == "failed" and "WriterError" in run["error"] and run["input_tokens"] == 7
    assert (settings.audio_dir / "2026-09-25.mp3").read_bytes() == b"old-audio"


def test_usage_persisted_after_select_call_before_write_runs(conn, settings):
    """A process kill between the select and write calls must not lose the select call's spend."""
    ids = [item_id(STORM), item_id(COUNCIL)]
    seen = {}

    class RecordingClaude(FakeClaude):
        def create(self, **kwargs):
            if self.calls:  # a call already happened: this is the write call, probe the run row first
                run = db.latest_run(conn)
                seen["tokens_before_write"] = (run["input_tokens"], run["output_tokens"])
            return super().create(**kwargs)

    claude = RecordingClaude([
        select_reply(ids),
        claude_reply({"stories": []}, stop_reason="refusal", input_tokens=5, output_tokens=2),
    ])
    run_id = pipeline.run_episode(deps_for(settings, claude), trigger="manual")
    assert seen["tokens_before_write"] == (1000, 200)

    run = run_row(conn, run_id)
    assert run["status"] == "failed"
    # select's 1000/200 tokens plus write's 5/2 tokens, select tokens recorded exactly once (not doubled)
    assert (run["input_tokens"], run["output_tokens"]) == (1005, 202)


def test_no_new_items_fails(conn, settings):
    empty = lambda request: httpx.Response(200, text=rss())
    run_id = pipeline.run_episode(deps_for(settings, FakeClaude([]), empty), trigger="manual")
    assert "no new items" in run_row(conn, run_id)["error"]


def test_lock_prevents_concurrent_runs(settings):
    assert pipeline._lock.acquire(blocking=False)
    try:
        assert pipeline.is_running()
        with pytest.raises(pipeline.RunInProgress):
            pipeline.run_episode(deps_for(settings, FakeClaude([])), trigger="manual")
    finally:
        pipeline._lock.release()


def test_cross_process_lock_prevents_concurrent_runs(conn, settings):
    lock_path = settings.data_dir / "run.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(pipeline.RunInProgress):
            pipeline.run_episode(deps_for(settings, FakeClaude([])), trigger="manual")
        assert db.latest_run(conn) is None
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_success_prunes_old_episodes(conn, settings):
    seed_episode(conn, settings, "2026-09-10")
    ids = [item_id(STORM)]
    pipeline.run_episode(deps_for(settings, FakeClaude([select_reply(ids), script_reply(ids)])), trigger="manual")
    assert db.get_episode(conn, "2026-09-10") is None


def test_publish_db_failure_leaves_old_episode_untouched(conn, settings, monkeypatch):
    seed_episode(conn, settings, "2026-09-25", audio=b"old-audio")

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pipeline.db, "publish_episode", boom)

    ids = [item_id(STORM), item_id(COUNCIL)]
    claude = FakeClaude([select_reply(ids), script_reply(ids)])
    run_id = pipeline.run_episode(deps_for(settings, claude), trigger="manual")

    run = run_row(conn, run_id)
    assert run["status"] == "failed" and "OperationalError" in run["error"]
    assert (settings.audio_dir / "2026-09-25.mp3").read_bytes() == b"old-audio"
    assert db.get_episode(conn, "2026-09-25")["word_count"] == 500
    assert not (settings.audio_dir / "2026-09-25.mp3.bak").exists()
    assert not (settings.audio_dir / "2026-09-25.mp3.tmp").exists()


def test_publish_db_failure_with_no_previous_episode_leaves_no_file(conn, settings, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pipeline.db, "publish_episode", boom)

    ids = [item_id(STORM), item_id(COUNCIL)]
    claude = FakeClaude([select_reply(ids), script_reply(ids)])
    run_id = pipeline.run_episode(deps_for(settings, claude), trigger="manual")

    run = run_row(conn, run_id)
    assert run["status"] == "failed" and "OperationalError" in run["error"]
    assert not (settings.audio_dir / "2026-09-25.mp3").exists()
    assert not (settings.audio_dir / "2026-09-25.mp3.bak").exists()
    assert not (settings.audio_dir / "2026-09-25.mp3.tmp").exists()
