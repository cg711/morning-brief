from datetime import datetime, time, timedelta

from fastapi.testclient import TestClient

from morning_brief import db, scheduler
from morning_brief.app import create_app
from morning_brief.config import TZ
from tests.helpers import NOW, seed_episode


def local(h, m):
    return datetime(2026, 9, 25, h, m, tzinfo=TZ)


def test_should_catch_up():
    assert scheduler.should_catch_up(local(9, 0), time(8, 0), False, False) is True
    assert scheduler.should_catch_up(local(7, 59), time(8, 0), False, False) is False
    assert scheduler.should_catch_up(local(9, 0), time(8, 0), True, False) is False
    assert scheduler.should_catch_up(local(9, 0), time(8, 0), False, True) is False


def test_retry_if_missing(conn, settings):
    calls = []
    start = lambda trigger: calls.append(trigger) or True
    assert scheduler.retry_if_missing(conn, lambda: NOW, start) is True
    seed_episode(conn, settings, "2026-09-25")
    assert scheduler.retry_if_missing(conn, lambda: NOW, start) is False
    assert calls == ["retry"]


def test_build_registers_jobs(conn, settings):
    sched = scheduler.build(settings, conn, lambda: NOW, lambda t: True)
    jobs = {j.id: str(j.trigger) for j in sched.get_jobs()}
    assert set(jobs) == {"daily", "retry", "prune"}
    assert "hour='8', minute='0'" in jobs["daily"]
    assert "hour='8', minute='30'" in jobs["retry"]
    assert "hour='3', minute='0'" in jobs["prune"]


def test_catch_up_skipped_after_interrupted_run(conn, settings):
    rid = db.start_run(conn, date="2026-09-25", trigger="schedule", model="m", started_at=NOW.isoformat())
    db.finish_run(conn, rid, "failed", NOW.isoformat(), error="interrupted")
    calls = []
    start = lambda trigger: calls.append(trigger) or True
    result = scheduler.catch_up(settings, conn, lambda: NOW + timedelta(hours=1), start)
    assert result is False
    assert calls == []


def test_catch_up_skipped_after_two_failed_runs_today(conn, settings):
    for _ in range(2):
        rid = db.start_run(conn, date="2026-09-25", trigger="schedule", model="m", started_at=NOW.isoformat())
        db.finish_run(conn, rid, "failed", NOW.isoformat(), error="boom")
    calls = []
    start = lambda trigger: calls.append(trigger) or True
    result = scheduler.catch_up(settings, conn, lambda: NOW + timedelta(hours=1), start)
    assert result is False
    assert calls == []


def test_catch_up_runs_after_one_non_interrupted_failure(conn, settings):
    rid = db.start_run(conn, date="2026-09-25", trigger="schedule", model="m", started_at=NOW.isoformat())
    db.finish_run(conn, rid, "failed", NOW.isoformat(), error="boom")
    calls = []
    start = lambda trigger: calls.append(trigger) or True
    result = scheduler.catch_up(settings, conn, lambda: NOW + timedelta(hours=1), start)
    assert result is True
    assert calls == ["catchup"]


def test_startup_catches_up_when_past_run_time(settings):
    calls = []
    app = create_app(settings, clock=lambda: NOW + timedelta(hours=1),
                     start_run=lambda t: calls.append(t) or True, start_scheduler=True)
    with TestClient(app):
        pass
    assert calls == ["catchup"]


def test_startup_skips_catch_up_when_episode_exists(settings):
    calls = []
    app = create_app(settings, clock=lambda: NOW + timedelta(hours=1),
                     start_run=lambda t: calls.append(t) or True, start_scheduler=True)
    seed_episode(app.state.conn, settings, "2026-09-25")
    with TestClient(app):
        pass
    assert calls == []


from dataclasses import replace


def test_daily_off_registers_only_prune(conn, settings):
    sched = scheduler.build(replace(settings, daily_brief=False), conn, lambda: NOW, lambda t: True)
    assert {j.id for j in sched.get_jobs()} == {"prune"}


def test_daily_off_skips_catch_up(conn, settings):
    calls = []
    off = replace(settings, daily_brief=False)
    assert scheduler.catch_up(off, conn, lambda: NOW + timedelta(hours=1), lambda t: calls.append(t) or True) is False
    assert calls == []


def test_prune_job_runs_deep_dive_housekeeping(conn, settings):
    from morning_brief import deepdives
    from tests.helpers import deep_dive_script
    a = deepdives.add_topic(conn, "A", "", NOW - timedelta(days=9))
    deepdives.claim(conn, NOW - timedelta(days=9))
    deepdives.accept_script(conn, a, deep_dive_script(), NOW - timedelta(days=9))
    deepdives.publish(conn, a, title="T", word_count=2400, duration_s=1.0, audio_bytes=1, now=NOW - timedelta(days=8))
    scheduler.prune_job(conn, settings, lambda: NOW)
    assert deepdives.get_topic(conn, a)["status"] == "heard"
