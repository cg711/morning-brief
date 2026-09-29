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
    assert set(jobs) == {"daily", "retry", "prune", "long_running"}
    assert "hour='8', minute='0'" in jobs["daily"]
    assert "hour='8', minute='30'" in jobs["retry"]
    assert "hour='3', minute='0'" in jobs["prune"]
    assert "interval[0:05:00]" in jobs["long_running"]


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


def test_daily_off_registers_only_housekeeping_jobs(conn, settings):
    sched = scheduler.build(replace(settings, daily_brief=False), conn, lambda: NOW, lambda t: True)
    assert {j.id for j in sched.get_jobs()} == {"prune", "long_running"}


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


def test_long_running_job_pushes_once_per_stage(conn, settings):
    from morning_brief import deepdives
    from tests.helpers import deep_dive_script
    calls = []
    record = lambda s, title, message, tags: calls.append((title, message, tags))
    t = deepdives.add_topic(conn, "The Fed", "", NOW - timedelta(minutes=75))
    deepdives.claim(conn, NOW - timedelta(minutes=75))
    quick = deepdives.add_topic(conn, "Quick", "", NOW)
    assert scheduler.long_running_job(conn, settings, lambda: NOW, record) == 1
    assert calls == [("Deep dive running long", "The Fed: researching for 75 min", ["hourglass"])]
    assert scheduler.long_running_job(conn, settings, lambda: NOW + timedelta(minutes=5), record) == 0
    deepdives.accept_script(conn, t, deep_dive_script(), NOW)
    assert scheduler.long_running_job(conn, settings, lambda: NOW + timedelta(minutes=41), record) == 1
    assert calls[-1][1] == "The Fed: speaking for 41 min"
    assert quick  # queued topics never push


def test_long_running_job_sends_nothing_for_a_topic_that_finished_before_the_tick(conn, settings):
    from morning_brief import deepdives
    from tests.helpers import deep_dive_script
    calls = []
    record = lambda s, title, message, tags: calls.append((title, message, tags))
    # long past the research limit, but it failed (or was accepted and published) before this tick ran
    a = deepdives.add_topic(conn, "A", "", NOW - timedelta(minutes=90))
    deepdives.claim(conn, NOW - timedelta(minutes=90))
    deepdives.fail(conn, a, "gave up", NOW, from_status="researching")
    b = deepdives.add_topic(conn, "B", "", NOW - timedelta(minutes=90))
    deepdives.claim(conn, NOW - timedelta(minutes=90))
    deepdives.accept_script(conn, b, deep_dive_script(), NOW)
    deepdives.publish(conn, b, title="B", word_count=2400, duration_s=1.0, audio_bytes=1, now=NOW)
    assert scheduler.long_running_job(conn, settings, lambda: NOW, record) == 0
    assert calls == []


def test_long_running_job_survives_push_errors(conn, settings):
    from morning_brief import deepdives
    deepdives.add_topic(conn, "A", "", NOW - timedelta(hours=2))
    deepdives.claim(conn, NOW - timedelta(hours=2))

    def exploding(*args):
        raise RuntimeError("down")

    assert scheduler.long_running_job(conn, settings, lambda: NOW, exploding) == 0


def test_worker_mode_jobs(conn, settings):
    s = replace(settings, daily_mode="worker")
    sched = scheduler.build(s, conn, lambda: NOW, lambda t: True)
    jobs = {j.id: str(j.trigger) for j in sched.get_jobs()}
    assert set(jobs) == {"daily", "ready_by", "prune", "long_running"}
    assert "hour='8', minute='0'" in jobs["daily"] and "hour='8', minute='30'" in jobs["ready_by"]


def test_worker_mode_catch_up_window(conn, settings):
    from morning_brief import daily_worker
    s = replace(settings, daily_mode="worker")
    calls = []
    start = lambda t: calls.append(t) or True
    assert scheduler.catch_up(s, conn, lambda: NOW - timedelta(minutes=1), start) is False  # 7:59, before RUN_AT
    assert scheduler.catch_up(s, conn, lambda: NOW + timedelta(minutes=31), start) is False  # 8:31, after READY_BY
    assert scheduler.catch_up(s, conn, lambda: NOW + timedelta(minutes=10), start) is True and calls == ["catchup"]
    from tests.test_daily_worker import gathered
    gathered(conn, s)
    assert scheduler.catch_up(s, conn, lambda: NOW + timedelta(minutes=10), start) is False  # job exists


def test_start_background_run_in_worker_mode_gathers(settings, monkeypatch):
    from morning_brief import daily_worker
    import threading
    done = threading.Event()
    seen = []
    monkeypatch.setattr(daily_worker, "gather_run", lambda s, trigger: seen.append(trigger) or done.set())
    assert scheduler.start_background_run(replace(settings, daily_mode="worker"), "manual") is True
    assert done.wait(2) and seen == ["manual"]


def test_start_background_run_in_worker_mode_gathers_while_a_render_holds_the_run_lock(settings, monkeypatch):
    from morning_brief import daily_worker, pipeline
    import threading
    done = threading.Event()
    monkeypatch.setattr(pipeline, "is_running", lambda: True)
    monkeypatch.setattr(daily_worker, "gather_run", lambda s, trigger: done.set())
    assert scheduler.start_background_run(replace(settings, daily_mode="worker"), "schedule") is True
    assert done.wait(2)


def test_prune_job_runs_notes_housekeeping(conn, settings):
    from datetime import date, timedelta

    from morning_brief import notes

    conn.execute("INSERT INTO countdowns (label, date, created_at) VALUES ('Gone', ?, ?)",
                 ((date(2026, 9, 25) - timedelta(days=1)).isoformat(), NOW.isoformat()))
    scheduler.prune_job(conn, settings, lambda: NOW)
    assert conn.execute("SELECT COUNT(*) FROM countdowns").fetchone()[0] == 0
