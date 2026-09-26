import json

from morning_brief import db
from tests.helpers import seed_episode

NOW = "2026-09-25T13:00:00+00:00"


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == []


def test_run_lifecycle(conn):
    rid = db.start_run(conn, date="2026-09-25", trigger="manual", model="claude-sonnet-5", started_at=NOW)
    assert db.latest_run_for(conn, "2026-09-25")["status"] == "running"
    db.set_stage(conn, rid, "writing")
    db.set_feed_errors(conn, rid, ["BBC: timeout"])
    db.add_usage(conn, rid, 100, 20)
    db.finish_run(conn, rid, "failed", NOW, error="boom")
    row = db.latest_run_for(conn, "2026-09-25")
    assert (row["status"], row["stage"], row["error"], row["input_tokens"], row["output_tokens"]) == (
        "failed", "writing", "boom", 100, 20)
    assert json.loads(row["feed_errors"]) == ["BBC: timeout"]
    assert db.latest_run(conn)["id"] == rid


def test_failed_run_count(conn):
    rid = db.start_run(conn, date="2026-09-25", trigger="schedule", model="m", started_at=NOW)
    assert db.failed_run_count(conn, "2026-09-25") == 0
    db.finish_run(conn, rid, "failed", NOW, error="boom")
    assert db.failed_run_count(conn, "2026-09-25") == 1
    rid2 = db.start_run(conn, date="2026-09-25", trigger="schedule", model="m", started_at=NOW)
    db.finish_run(conn, rid2, "succeeded", NOW)
    assert db.failed_run_count(conn, "2026-09-25") == 1
    assert db.failed_run_count(conn, "2026-09-26") == 0


def test_fail_interrupted_runs(conn):
    db.start_run(conn, date="2026-09-25", trigger="schedule", model="m", started_at=NOW)
    assert db.fail_interrupted_runs(conn, NOW) == 1
    row = db.latest_run_for(conn, "2026-09-25")
    assert (row["status"], row["error"]) == ("failed", "interrupted")


def test_publish_upserts_and_replaces_sources(conn, settings):
    seed_episode(conn, settings, "2026-09-25", words=500, now="2026-09-25T13:05:00+00:00")
    db.publish_episode(
        conn, date="2026-09-25", cutoff_at="2026-09-25T14:00:00+00:00", script_json="{}", word_count=600,
        duration_s=250.0, audio_bytes=10, now="2026-09-25T14:05:00+00:00",
        sources=[{"item_id": "b2", "source": "MPR News", "title": "T", "url": "https://x/b2",
                  "published_at": NOW}],
    )
    ep = db.get_episode(conn, "2026-09-25")
    assert (ep["word_count"], ep["created_at"], ep["updated_at"]) == (
        600, "2026-09-25T13:05:00+00:00", "2026-09-25T14:05:00+00:00")
    assert [r["item_id"] for r in db.episode_sources(conn, "2026-09-25")] == ["b2"]


def test_previous_episode_excludes_same_day(conn, settings):
    seed_episode(conn, settings, "2026-09-24")
    seed_episode(conn, settings, "2026-09-25")
    assert db.previous_episode(conn, before_date="2026-09-25")["date"] == "2026-09-24"
    assert db.previous_episode(conn, before_date="2026-09-24") is None


def test_list_episodes_newest_first(conn, settings):
    for d in ("2026-09-23", "2026-09-25", "2026-09-24"):
        seed_episode(conn, settings, d)
    assert [r["date"] for r in db.list_episodes(conn)] == ["2026-09-25", "2026-09-24", "2026-09-23"]
    assert db.episode_dates_before(conn, "2026-09-25") == ["2026-09-23", "2026-09-24"]


def test_delete_episode_row_cascades_sources(conn, settings):
    seed_episode(conn, settings, "2026-09-25")
    assert db.delete_episode_row(conn, "2026-09-25") is True
    assert db.get_episode(conn, "2026-09-25") is None
    assert db.episode_sources(conn, "2026-09-25") == []
    assert db.delete_episode_row(conn, "2026-09-25") is False


def test_usage_by_model_and_run_pruning(conn):
    old = db.start_run(conn, date="2026-07-01", trigger="schedule", model="claude-sonnet-5", started_at=NOW)
    db.add_usage(conn, old, 5, 5)
    db.finish_run(conn, old, "succeeded", NOW)
    new = db.start_run(conn, date="2026-09-25", trigger="schedule", model="claude-sonnet-5", started_at=NOW)
    db.add_usage(conn, new, 1_000_000, 100_000)
    rows = db.usage_by_model(conn, since_date="2026-09-01")
    assert [(r["model"], r["input_tokens"], r["output_tokens"]) for r in rows] == [
        ("claude-sonnet-5", 1_000_000, 100_000)]
    assert db.delete_runs_before(conn, "2026-08-01") == 1


def test_app_state_round_trip(conn):
    assert db.get_state(conn, "worker_last_seen") is None
    db.set_state(conn, "worker_last_seen", "a")
    db.set_state(conn, "worker_last_seen", "b")
    assert db.get_state(conn, "worker_last_seen") == "b"


def test_topics_have_long_notified_column(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(topics)")}
    assert "long_notified" in cols
