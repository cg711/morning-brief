import json
import threading
from datetime import timedelta

import httpx
import pytest

from morning_brief import daily_worker, db, pipeline, speech
from morning_brief.feeds import item_id
from tests.helpers import NOW, seed_episode

STORM, COUNCIL = "https://wire.test/storm", "https://local.test/council"
FULL = "<content:encoded><![CDATA[" + " ".join(["council"] * 200) + "]]></content:encoded>"


def rss(*items):
    body = "".join(f"<item><title>{t}</title><link>{link}</link><pubDate>{pub}</pubDate>"
                   f"<description>{t} summary.</description>{extra}</item>" for t, link, pub, extra in items)
    return ('<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            f"<channel><title>x</title>{body}</channel></rss>")


FEEDS = {"https://wire.test/rss": rss(("Storm hits coast", STORM, "Fri, 25 Sep 2026 12:00:00 +0000", "")),
         "https://local.test/rss": rss(("Council passes budget", COUNCIL, "Fri, 25 Sep 2026 12:45:00 +0000", FULL))}


def http():
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=FEEDS[str(r.url)]) if str(r.url) in FEEDS else httpx.Response(500)))


def gathered(conn, settings, now=NOW):
    return daily_worker.gather(settings, conn, http(), now, "schedule")


def script_for(ids, words=500):
    per = (words - 5) // len(ids)
    segs = [{"segment": "headlines", "headline": f"Story {n}", "text": " ".join(["word"] * per), "item_ids": [i]}
            for n, i in enumerate(ids)]
    segs[0]["text"] += " word" * ((words - 5) - per * len(ids))
    return {"intro": "Good morning.", "segments": segs, "outro": "See you tomorrow."}


def test_gather_stores_waiting_job(conn, settings):
    date = gathered(conn, settings)
    job = daily_worker.get_job(conn, date)
    assert date == "2026-09-25" and job["status"] == "waiting"
    cands = {c["url"]: c for c in json.loads(job["candidates_json"])}
    assert cands[COUNCIL]["text"] == "feed" and cands[STORM]["text"] == "summary"
    assert set(json.loads(job["bodies_json"])) == {item_id(COUNCIL)}
    run = db.latest_run(conn)
    assert (run["status"], run["stage"], run["model"]) == ("running", "waiting", "mac-worker")
    assert json.loads(run["feed_errors"])[0].startswith("Broken Feed:")


def test_gather_with_nothing_new_fails_run_and_stores_no_job(conn, settings):
    # previous cutoff 3 h after NOW: the window (cutoff - 2 h overlap) starts after every fixture item
    seed_episode(conn, settings, "2026-09-24", cutoff_at=(NOW + timedelta(hours=3)).isoformat())
    with pytest.raises(pipeline.PipelineError):
        gathered(conn, settings)
    assert daily_worker.get_job(conn, "2026-09-25") is None
    assert db.latest_run(conn)["status"] == "failed"


def test_regather_replaces_job_and_fails_old_run(conn, settings):
    gathered(conn, settings)
    first = db.latest_run(conn)["id"]
    gathered(conn, settings, NOW + timedelta(minutes=5))
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (first,)).fetchone()["status"] == "failed"
    assert daily_worker.get_job(conn, "2026-09-25")["run_id"] != first


def test_claim_lifecycle(conn, settings):
    assert daily_worker.claim(conn, NOW, "Minneapolis") is None
    gathered(conn, settings)
    got = daily_worker.claim(conn, NOW, "Minneapolis")
    assert got["date"] == "2026-09-25" and got["today"] == "Friday, September 25, 2026"
    assert got["location"] == "Minneapolis" and got["target_words"] == 550 and got["previous_headlines"] == []
    assert set(got["candidates"][0]) == set(daily_worker.CLAIM_KEYS)
    assert db.latest_run(conn)["stage"] == "writing"
    assert daily_worker.claim(conn, NOW + timedelta(minutes=14), "Minneapolis") is None
    assert daily_worker.claim(conn, NOW + timedelta(minutes=16), "Minneapolis") is not None


def test_gather_keeps_at_most_fifteen_candidates_per_segment(conn, settings):
    many = rss(*[(f"Wire story {n}", f"https://wire.test/s{n}", f"Fri, 25 Sep 2026 12:{n:02d}:00 +0000", "")
                 for n in range(40)])
    feeds = {**FEEDS, "https://wire.test/rss": many}
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=feeds[str(r.url)]) if str(r.url) in feeds else httpx.Response(500)))
    date = daily_worker.gather(settings, conn, client, NOW, "schedule")
    cands = json.loads(daily_worker.get_job(conn, date)["candidates_json"])
    headlines = [c["title"] for c in cands if c["segment"] == "headlines"]
    assert headlines == [f"Wire story {n}" for n in range(39, 24, -1)]
    assert [c["url"] for c in cands if c["segment"] == "local"] == [COUNCIL]


def test_item_text(conn, settings):
    date = gathered(conn, settings)
    assert daily_worker.item_text(conn, date, item_id(COUNCIL)).startswith("council council")
    assert daily_worker.item_text(conn, date, item_id(STORM)) is None
    assert daily_worker.item_text(conn, "2026-09-24", item_id(COUNCIL)) is None


def test_validate_submission():
    known = {"a", "b"}
    assert daily_worker.validate_submission(script_for(["a", "b"]), known) == []
    assert daily_worker.validate_submission([], known) == ["the body must be a JSON object"]
    bad = script_for(["a"])
    bad["segments"][0]["segment"] = "sports"
    bad["intro"] = "Hi\x07"
    problems = daily_worker.validate_submission(bad, known)
    assert any("'segment' must be one of" in p for p in problems) and any("control characters" in p for p in problems)
    assert any("unknown item ids" in p for p in daily_worker.validate_submission(script_for(["zzz"]), known))
    assert any("too short" in p for p in daily_worker.validate_submission(script_for(["a"], 100), known))
    assert any("1 to 8" in p for p in daily_worker.validate_submission({**script_for(["a"]), "segments": []}, known))
    long_headline = script_for(["a"])
    long_headline["segments"][0]["headline"] = "x" * 201
    assert any("headline must be at most 200 characters" in p
              for p in daily_worker.validate_submission(long_headline, known))


def test_regather_over_claimed_job_raises_and_creates_no_run(conn, settings):
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    run_count = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    with pytest.raises(pipeline.PipelineError, match="already being written"):
        gathered(conn, settings)
    assert daily_worker.get_job(conn, date)["status"] == "claimed"
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == run_count


def test_regather_over_speaking_job_raises_and_creates_no_run(conn, settings):
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    daily_worker.accept(conn, date, script_for([item_id(COUNCIL)]), NOW)
    run_count = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    with pytest.raises(pipeline.PipelineError, match="already being spoken"):
        gathered(conn, settings)
    assert daily_worker.get_job(conn, date)["status"] == "speaking"
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == run_count


def test_regather_over_done_job_scheduled_raises_but_manual_replaces(conn, settings):
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    daily_worker.accept(conn, date, script_for([item_id(COUNCIL)]), NOW)
    daily_worker.speak_and_publish(settings, date, synthesize=speech.fake_synthesize, now=lambda: NOW)
    run_count = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    with pytest.raises(pipeline.PipelineError, match="already published"):
        gathered(conn, settings)
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == run_count
    manual_date = daily_worker.gather(settings, conn, http(), NOW, "manual")
    assert manual_date == date
    assert daily_worker.get_job(conn, date)["status"] == "waiting"


def test_gather_failure_after_fetch_fails_the_run(conn, settings, monkeypatch):
    def boom(dt):
        raise RuntimeError("boom")

    monkeypatch.setattr(daily_worker.writer, "_local", boom)
    with pytest.raises(RuntimeError):
        gathered(conn, settings)
    assert db.latest_run(conn)["status"] == "failed"


def test_gather_when_every_feed_fails_records_reason(conn, settings):
    def all_500():
        return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    with pytest.raises(pipeline.PipelineError):
        daily_worker.gather(settings, conn, all_500(), NOW, "schedule")
    assert "every feed failed" in db.latest_run(conn)["error"]


def test_accept_strips_unknown_fields(conn, settings):
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    script = script_for([item_id(COUNCIL)])
    script["extra"] = "nope"
    script["segments"][0]["extra"] = "nope"
    assert daily_worker.accept(conn, date, script, NOW) is True
    stored = json.loads(daily_worker.get_job(conn, date)["script_json"])
    assert "extra" not in stored and "extra" not in stored["segments"][0]


def test_accept_speak_and_publish(conn, settings):
    date = gathered(conn, settings)
    assert daily_worker.accept(conn, date, script_for([item_id(COUNCIL)]), NOW) is False  # not claimed
    daily_worker.claim(conn, NOW, "Minneapolis")
    assert daily_worker.accept(conn, date, script_for([item_id(COUNCIL)]), NOW) is True
    status = daily_worker.speak_and_publish(settings, date, synthesize=speech.fake_synthesize, now=lambda: NOW)
    assert status == "ready"
    job = daily_worker.get_job(conn, date)
    ep = db.get_episode(conn, date)
    assert job["status"] == "done" and ep["cutoff_at"] == job["cutoff_at"]
    assert [s["url"] for s in db.episode_sources(conn, date)] == [COUNCIL]
    assert db.latest_run(conn)["status"] == "succeeded"


def test_speak_failure_fails_job_and_run(conn, settings):
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    daily_worker.accept(conn, date, script_for([item_id(COUNCIL)]), NOW)

    def broken(*args):
        raise RuntimeError("espeak exploded")

    assert daily_worker.speak_and_publish(settings, date, synthesize=broken, now=lambda: NOW) == "failed"
    job = daily_worker.get_job(conn, date)
    assert job["status"] == "failed" and "espeak exploded" in job["error"]
    assert db.latest_run(conn)["status"] == "failed"


def test_fail_job_only_from_claimed(conn, settings):
    date = gathered(conn, settings)
    assert daily_worker.fail_job(conn, date, "nope", NOW) is False
    daily_worker.claim(conn, NOW, "Minneapolis")
    assert daily_worker.fail_job(conn, date, "no good stories", NOW) is True
    assert daily_worker.get_job(conn, date)["error"] == "no good stories"
    assert db.latest_run(conn)["error"] == "no good stories"


def recorder():
    calls = []
    return calls, lambda s, title, message, tags: calls.append((title, message))


def test_check_missed_pushes_once(conn, settings):
    calls, notify = recorder()
    gathered(conn, settings)
    assert daily_worker.check_missed(settings, conn, NOW, notify) is True
    assert calls == [("No brief today", "The Mac worker didn't run.")]
    assert daily_worker.get_job(conn, "2026-09-25")["status"] == "missed"
    assert db.latest_run(conn)["error"] == "missed"
    assert daily_worker.check_missed(settings, conn, NOW, notify) is False and len(calls) == 1


def test_check_missed_reports_worker_failure(conn, settings):
    calls, notify = recorder()
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    daily_worker.fail_job(conn, date, "every candidate was paywalled", NOW)
    daily_worker.check_missed(settings, conn, NOW, notify)
    assert calls == [("No brief today", "Couldn't make it: every candidate was paywalled")]
    job = daily_worker.get_job(conn, date)
    assert job["status"] == "missed" and job["error"] == "every candidate was paywalled"


def test_check_missed_with_no_job_reports_failed_run_error(conn, settings):
    calls, notify = recorder()
    date = "2026-09-25"
    run_id = db.start_run(conn, date=date, trigger="schedule", model=daily_worker.MODEL, started_at=NOW.isoformat())
    db.finish_run(conn, run_id, "failed", NOW.isoformat(), error="X")
    assert daily_worker.check_missed(settings, conn, NOW, notify) is True
    assert calls == [("No brief today", "Couldn't gather stories: X")]


def test_check_missed_silent_when_published_or_speaking(conn, settings):
    calls, notify = recorder()
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    daily_worker.accept(conn, date, script_for([item_id(COUNCIL)]), NOW)
    assert daily_worker.check_missed(settings, conn, NOW, notify) is False  # speaking
    daily_worker.speak_and_publish(settings, date, synthesize=speech.fake_synthesize, now=lambda: NOW)
    assert daily_worker.check_missed(settings, conn, NOW, notify) is False and calls == []


def test_check_missed_without_any_job(conn, settings):
    calls, notify = recorder()
    assert daily_worker.check_missed(settings, conn, NOW, notify) is True
    assert calls == [("No brief today", "No stories were gathered.")]


def test_fail_interrupted_speaking_jobs(conn, settings):
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    daily_worker.accept(conn, date, script_for([item_id(COUNCIL)]), NOW)
    assert daily_worker.fail_interrupted(conn, NOW) == 1
    job = daily_worker.get_job(conn, date)
    assert job["status"] == "failed" and "interrupted during speech" in job["error"]


def test_fail_interrupted_fails_orphaned_jobs_from_earlier_days(conn, settings):
    old = gathered(conn, settings, NOW - timedelta(days=1))
    old_run = daily_worker.get_job(conn, old)["run_id"]
    today = gathered(conn, settings)
    assert daily_worker.fail_interrupted(conn, NOW) == 1
    job = daily_worker.get_job(conn, old)
    assert (job["status"], job["error"]) == ("failed", "missed")
    run = conn.execute("SELECT status, error FROM runs WHERE id = ?", (old_run,)).fetchone()
    assert (run["status"], run["error"]) == ("failed", "missed")
    assert daily_worker.get_job(conn, today)["status"] == "waiting"


def test_gather_run_never_raises(conn, settings):  # conn: the DB is migrated
    assert daily_worker.gather_run(settings, "manual", http_factory=lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))), now=lambda: NOW) is None


def test_gather_run_logs_refusal_without_traceback(conn, settings, monkeypatch):
    calls = []
    monkeypatch.setattr(daily_worker, "log", type("Log", (), {
        "info": lambda self, *a: calls.append(("info", a[-1])),
        "exception": lambda self, *a: calls.append(("exception", a))})())
    daily_worker.gather_run(settings, "manual", http_factory=lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))), now=lambda: NOW)
    assert len(calls) == 1 and calls[0][0] == "info" and "every feed failed" in str(calls[0][1])


def test_gather_run_succeeds_while_run_lock_held(conn, settings):
    with pipeline.run_lock(settings):
        assert daily_worker.gather_run(settings, "schedule", http_factory=http, now=lambda: NOW) == "2026-09-25"


def test_gather_run_returns_none_while_gather_lock_held(conn, settings):
    assert daily_worker._gather_lock.acquire(blocking=False)
    try:
        assert daily_worker.gather_run(settings, "schedule", http_factory=http, now=lambda: NOW) is None
    finally:
        daily_worker._gather_lock.release()
    assert daily_worker.get_job(conn, "2026-09-25") is None


def test_speak_and_publish_lock_timeout(conn, settings):
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    daily_worker.accept(conn, date, script_for([item_id(COUNCIL)]), NOW)
    naps = []
    with pipeline.run_lock(settings):
        status = daily_worker.speak_and_publish(settings, date, synthesize=speech.fake_synthesize, now=lambda: NOW,
                                                lock_wait_s=2, lock_retry_s=1, sleep=naps.append)
    assert status == "failed"
    assert naps == [1, 1]
    job = daily_worker.get_job(conn, date)
    assert job["status"] == "failed" and "speech lock busy" in job["error"]


from morning_brief import personal as personal_mod


def personal_seg(words=50, **extra):
    return {"segment": "personal", "headline": "Morning", "text": " ".join(["well"] * words), **extra}


def test_gather_stores_personal_and_claim_returns_it(conn, settings, monkeypatch):
    facts = {"sleep": {"sleep_score": 67, "hours": 6.1, "readiness": 85, "hrv_balance": 82}}
    monkeypatch.setattr(personal_mod, "gather_personal", lambda s, http, now: facts)
    gathered(conn, settings)
    assert daily_worker.claim(conn, NOW, "Minneapolis")["personal"] == facts


def test_claim_personal_is_null_when_off(conn, settings):
    gathered(conn, settings)
    assert daily_worker.claim(conn, NOW, "Minneapolis")["personal"] is None


def test_validate_personal_segment():
    known = {"a"}
    news = script_for(["a"])
    ok = {**news, "segments": [personal_seg(), *news["segments"]]}
    assert daily_worker.validate_submission(ok, known, personal_allowed=True) == []
    assert daily_worker.validate_submission(news, known, personal_allowed=True) == []  # optional
    assert any("no personal data" in p for p in daily_worker.validate_submission(ok, known))
    late = {**news, "segments": [*news["segments"], personal_seg()]}
    assert any("must be the first segment" in p for p in daily_worker.validate_submission(late, known, personal_allowed=True))
    twice = {**news, "segments": [personal_seg(), personal_seg(), *news["segments"]]}
    assert any("must be the first segment" in p for p in daily_worker.validate_submission(twice, known, personal_allowed=True))
    for bad in (personal_seg(10), personal_seg(120), personal_seg(item_ids=["a"]), {**personal_seg(), "text": "hi\x07 " * 30}):
        script = {**news, "segments": [bad, *news["segments"]]}
        assert any("personal" in p for p in daily_worker.validate_submission(script, known, personal_allowed=True)), bad


def test_personal_words_do_not_count_toward_news_range():
    news = script_for(["a"], words=740)
    with_personal = {**news, "segments": [personal_seg(90), *news["segments"]]}
    assert daily_worker.validate_submission(with_personal, {"a"}, personal_allowed=True) == []


def test_accept_forces_personal_headline(conn, settings, monkeypatch):
    monkeypatch.setattr(personal_mod, "gather_personal", lambda s, http, now: {"tip": {"title": "t", "detail": "d"}})
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    news = script_for([item_id(COUNCIL)])
    assert daily_worker.accept(conn, date, {**news, "segments": [personal_seg(item_ids=None), *news["segments"]]}, NOW)
    stored = json.loads(daily_worker.get_job(conn, date)["script_json"])["segments"][0]
    assert stored == {"segment": "personal", "headline": "Your morning", "text": personal_seg()["text"], "item_ids": []}


def test_gather_survives_personal_failure(conn, settings, monkeypatch):
    def boom(s, http, now):
        raise ValueError("bad data")

    monkeypatch.setattr(personal_mod, "gather_personal", boom)
    date = gathered(conn, settings)
    assert daily_worker.get_job(conn, date)["personal_json"] is None
    assert daily_worker.claim(conn, NOW, "Minneapolis")["personal"] is None


def test_news_problems_are_numbered_as_news_after_a_personal_segment():
    news = script_for(["a"])
    news["segments"][0]["headline"] = ""
    problems = daily_worker.validate_submission({**news, "segments": [personal_seg(), *news["segments"]]}, {"a"},
                                                personal_allowed=True)
    assert problems == ["news segment 1 needs a non-empty 'headline'"]
    assert daily_worker.validate_submission(news, {"a"}, personal_allowed=True) == \
        ["segment 1 needs a non-empty 'headline'"]


def test_previous_headlines_leave_out_your_morning(conn, settings):
    seed_episode(conn, settings, "2026-09-24")
    script = script_for(["a1"])
    script["segments"].insert(0, {"segment": "personal", "headline": "Your morning", "text": "well", "item_ids": []})
    conn.execute("UPDATE episodes SET script_json = ? WHERE date = '2026-09-24'", (json.dumps(script),))
    gathered(conn, settings)
    assert daily_worker.claim(conn, NOW, "Minneapolis")["previous_headlines"] == ["Story 0"]
