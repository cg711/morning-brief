import logging
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from morning_brief import db, notify
from morning_brief.app import create_app
from tests.helpers import NOW, seed_episode

TOKEN = "t" * 40


@pytest.fixture
def started():
    return []


@pytest.fixture
def client(settings, started):
    def start_run(trigger):
        started.append(trigger)
        return True

    app = create_app(settings, clock=lambda: NOW, start_run=start_run, start_scheduler=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def app_conn(client):
    return client.app.state.conn


def test_waiting_state(client):
    html = client.get("/").text
    assert "Generate now" in html and "Generates at 8:00 AM" in html
    assert f"https://brief.test/feed/{TOKEN}.xml" in html
    assert "No earlier episodes yet." in html


def test_offline_banner_shown_when_claude_offline(settings):
    app = create_app(replace(settings, claude_offline=True), clock=lambda: NOW,
                     start_run=lambda t: True, start_scheduler=False)
    with TestClient(app) as c:
        html = c.get("/").text
    assert 'class="banner"' in html
    assert "Offline test mode — episodes are written without Claude (CLAUDE_OFFLINE=1)." in html


def test_offline_banner_absent_when_claude_offline_false(client):
    html = client.get("/").text
    assert "Offline test mode" not in html


def test_ready_state_and_past_episodes(client, app_conn, settings):
    seed_episode(app_conn, settings, "2026-09-25")
    seed_episode(app_conn, settings, "2026-09-24")
    html = client.get("/").text
    assert f'src="/audio/{TOKEN}/2026-09-25.mp3?v=' in html
    assert "3:20 · 1 stories" in html and "Regenerate" in html
    assert 'id="ep-2026-09-24"' in html and "Thursday, September 24" in html
    assert "NPR: Big news" in html


def test_generating_state_polls(client, app_conn):
    rid = db.start_run(app_conn, date="2026-09-25", trigger="schedule", model="m", started_at=NOW.isoformat())
    db.set_stage(app_conn, rid, "writing")
    html = client.get("/partials/today").text
    assert 'hx-trigger="every 3s"' in html and "Writing…" in html and "0 min" in html


def test_failed_state_shows_error_and_feed_errors(client, app_conn):
    rid = db.start_run(app_conn, date="2026-09-25", trigger="schedule", model="m", started_at=NOW.isoformat())
    db.set_feed_errors(app_conn, rid, ["BBC News: timeout"])
    db.finish_run(app_conn, rid, "failed", NOW.isoformat(), error="WriterError: nope")
    html = client.get("/partials/today").text
    assert "Generation failed: WriterError: nope" in html and "BBC News: timeout" in html and "Retry" in html


HX = {"HX-Request": "true"}


def test_generate_starts_run_and_polls(client, started):
    html = client.post("/generate", headers=HX).text
    assert started == ["manual"]
    assert 'hx-trigger="every 3s"' in html


def test_delete_past_episode(client, app_conn, settings):
    seed_episode(app_conn, settings, "2026-09-24")
    r = client.delete("/episodes/2026-09-24", headers=HX)
    assert r.status_code == 200 and r.text == ""
    assert db.get_episode(app_conn, "2026-09-24") is None
    assert not (settings.audio_dir / "2026-09-24.mp3").exists()


def test_delete_today_returns_waiting_card(client, app_conn, settings):
    seed_episode(app_conn, settings, "2026-09-25")
    html = client.delete("/episodes/2026-09-25", headers=HX).text
    assert 'id="today"' in html and "Generate now" in html


def test_delete_rejects_bad_date(client):
    assert client.delete("/episodes/../etc", headers=HX).status_code == 404
    assert client.delete("/episodes/2026-9-1", headers=HX).status_code == 404


def test_cross_site_guard_requires_hx_request_header(client):
    assert client.post("/generate").status_code == 403
    assert client.delete("/episodes/2026-09-24").status_code == 403


def test_feed_requires_token(client, app_conn, settings):
    seed_episode(app_conn, settings, "2026-09-25")
    assert client.get("/feed/wrong.xml").status_code == 404
    r = client.get(f"/feed/{TOKEN}.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml")
    assert f"https://brief.test/audio/{TOKEN}/2026-09-25.mp3?v=" in r.text


def test_audio_supports_range_requests(client, app_conn, settings):
    seed_episode(app_conn, settings, "2026-09-25")
    r = client.get(f"/audio/{TOKEN}/2026-09-25.mp3", headers={"Range": "bytes=0-1"})
    assert r.status_code == 206 and r.content == b"\xff\xf3"
    assert r.headers["content-range"] == "bytes 0-1/4"
    assert client.get(f"/audio/wrong/2026-09-25.mp3").status_code == 404
    assert client.get(f"/audio/{TOKEN}/2026-09-20.mp3").status_code == 404


def test_cover_served(client):
    r = client.get("/feed/cover.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"


def test_funnel_requests_do_not_reach_the_ui_or_feeds(client):
    funnel = {"Tailscale-Funnel-Request": "?1"}
    assert client.get("/", headers=funnel).status_code == 404
    assert client.post("/generate", headers=funnel).status_code == 404
    assert client.get(f"/feed/{TOKEN}.xml", headers=funnel).status_code == 404


def test_month_cost_in_footer(client, app_conn):
    rid = db.start_run(app_conn, date="2026-09-20", trigger="schedule", model="claude-sonnet-5",
                       started_at=NOW.isoformat())
    db.add_usage(app_conn, rid, 1_000_000, 100_000)
    db.finish_run(app_conn, rid, "succeeded", NOW.isoformat())
    assert "API spend this month: $3.00" in client.get("/").text


def test_create_app_quiets_phonemizer_warnings(settings):
    logger = logging.getLogger("phonemizer")
    saved_level = logger.level
    try:
        create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
        assert logger.level == logging.ERROR
    finally:
        logger.setLevel(saved_level)


def test_create_app_configures_morning_brief_logging(settings):
    # Save the logger state to restore later
    logger = logging.getLogger("morning_brief")
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    saved_propagate = logger.propagate

    try:
        # Clear any existing handlers before test
        logger.handlers.clear()

        # Create app once
        create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)

        # Create app again to verify idempotency
        create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)

        # Verify the logger is configured at INFO level
        assert logger.level == logging.INFO

        # Verify it has exactly one handler (idempotent)
        assert len(logger.handlers) == 1

        # Verify child logger (like morning_brief.pipeline) inherits INFO level
        child_logger = logging.getLogger("morning_brief.pipeline")
        assert child_logger.getEffectiveLevel() == logging.INFO
    finally:
        # Restore logger state for other tests
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        logger.level = saved_level
        logger.propagate = saved_propagate


def test_create_app_exposes_notifier(settings):
    default = create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
    assert default.state.notifier is notify.send
    custom = lambda *a: None
    app = create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False, notifier=custom)
    assert app.state.notifier is custom


def worker_client(settings, clock):
    app = create_app(replace(settings, daily_mode="worker"), clock=clock, start_run=lambda t: True,
                     start_scheduler=False)
    return TestClient(app)


def test_worker_mode_waiting_and_stage_labels(settings):
    from datetime import timedelta
    with worker_client(settings, lambda: NOW - timedelta(minutes=30)) as c:
        assert "Gathers at 8:00 AM; ready by 8:30 AM." in c.get("/").text
        conn = c.app.state.conn
        rid = db.start_run(conn, date="2026-09-25", trigger="schedule", model="mac-worker",
                           started_at=(NOW - timedelta(minutes=40)).isoformat())
        db.set_stage(conn, rid, "waiting")
        html = c.get("/partials/today").text
        assert "Waiting for your Mac" in html and "Run now" in html and "10 min" in html
        assert "caution" not in html


def test_worker_mode_amber_when_waiting_past_ready_by(settings):
    from datetime import timedelta
    with worker_client(settings, lambda: NOW + timedelta(minutes=35)) as c:
        conn = c.app.state.conn
        rid = db.start_run(conn, date="2026-09-25", trigger="schedule", model="mac-worker",
                           started_at=NOW.isoformat())
        db.set_stage(conn, rid, "waiting")
        assert 'class="caution"' in c.get("/partials/today").text


def test_long_running_stage_is_amber(client, app_conn):
    from datetime import timedelta
    rid = db.start_run(app_conn, date="2026-09-25", trigger="schedule", model="m",
                       started_at=(NOW - timedelta(minutes=31)).isoformat())
    db.set_stage(app_conn, rid, "speaking")
    html = client.get("/partials/today").text
    assert "Speaking…" in html and 'class="caution"' in html and "31 min" in html


def test_player_stays_during_regenerate(client, app_conn, settings):
    seed_episode(app_conn, settings, "2026-09-25")
    rid = db.start_run(app_conn, date="2026-09-25", trigger="manual", model="m", started_at=NOW.isoformat())
    db.set_stage(app_conn, rid, "fetching")
    html = client.get("/partials/today").text
    assert "Gathering stories…" in html and f'src="/audio/{TOKEN}/2026-09-25.mp3?v=' in html
    version = int(datetime.fromisoformat(db.get_episode(app_conn, "2026-09-25")["updated_at"]).timestamp())
    assert f'id="player-today-{version}" hx-preserve' in html
    assert "Regenerate" not in html and "Transcript" in html


def test_missed_state(settings):
    from morning_brief import daily_worker
    from tests.test_daily_worker import gathered
    with worker_client(settings, lambda: NOW) as c:
        conn = c.app.state.conn
        gathered(conn, c.app.state.settings)
        daily_worker.check_missed(c.app.state.settings, conn, NOW, lambda *a: None)
        html = c.get("/partials/today").text
        assert "No brief today: the Mac worker didn&#39;t run" in html and "Generate now" in html


def test_worker_mode_amber_measures_time_in_the_writing_stage(settings):
    from datetime import timedelta
    from morning_brief import daily_worker
    from tests.test_daily_worker import gathered
    with worker_client(settings, lambda: NOW) as c:
        conn = c.app.state.conn
        gathered(conn, c.app.state.settings, NOW - timedelta(minutes=60))
        daily_worker.claim(conn, NOW - timedelta(minutes=5), "Minneapolis")
        html = c.get("/partials/today").text
        assert "Your Mac is writing" in html and "60 min" in html and "caution" not in html


def test_worker_mode_card_follows_the_live_job_run(settings):
    from morning_brief import daily_worker
    from tests.test_daily_worker import gathered
    with worker_client(settings, lambda: NOW) as c:
        conn = c.app.state.conn
        gathered(conn, c.app.state.settings)
        rid = db.start_run(conn, date="2026-09-25", trigger="manual", model=daily_worker.MODEL,
                           started_at=NOW.isoformat())
        db.finish_run(conn, rid, "failed", NOW.isoformat(), error="today's brief is already being written")
        html = c.get("/partials/today").text
        assert "Waiting for your Mac" in html and "Generation failed" not in html


def test_worker_mode_missed_without_a_job(settings):
    from morning_brief import daily_worker
    with worker_client(settings, lambda: NOW) as c:
        conn = c.app.state.conn
        daily_worker.check_missed(c.app.state.settings, conn, NOW, lambda *a: None)
        html = c.get("/partials/today").text
        assert "No brief today: no stories were gathered" in html and "Generate now" in html


def test_worker_mode_polls_slowly_while_waiting_or_writing(settings):
    from datetime import timedelta
    with worker_client(settings, lambda: NOW) as c:
        conn = c.app.state.conn
        rid = db.start_run(conn, date="2026-09-25", trigger="schedule", model="mac-worker",
                           started_at=(NOW - timedelta(minutes=1)).isoformat())
        db.set_stage(conn, rid, "waiting")
        html = c.get("/partials/today").text
        assert 'hx-trigger="every 20s"' in html and "every 3s" not in html
        db.set_stage(conn, rid, "speaking")
        html = c.get("/partials/today").text
        assert 'hx-trigger="every 3s"' in html and "every 20s" not in html
