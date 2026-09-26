from datetime import date

from morning_brief import db, retention
from tests.helpers import seed_episode


def test_prune_keeps_today_and_ten_previous_days(conn, settings):
    seed_episode(conn, settings, "2026-09-14")
    seed_episode(conn, settings, "2026-09-15")
    removed = retention.prune(conn, settings.audio_dir, date(2026, 9, 25))
    assert removed == ["2026-09-14"]
    assert db.get_episode(conn, "2026-09-15") is not None
    assert not (settings.audio_dir / "2026-09-14.mp3").exists()
    assert (settings.audio_dir / "2026-09-15.mp3").exists()


def test_prune_removes_runs_after_62_days_and_stray_files(conn, settings):
    db.start_run(conn, date="2026-07-20", trigger="schedule", model="m", started_at="2026-07-20T13:00:00+00:00")
    db.finish_run(conn, 1, "failed", "2026-07-20T13:01:00+00:00", "x")
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    (settings.audio_dir / "2026-09-01.mp3").write_bytes(b"stray")
    retention.prune(conn, settings.audio_dir, date(2026, 9, 25))
    assert db.latest_run(conn) is None
    assert not (settings.audio_dir / "2026-09-01.mp3").exists()


def test_prune_removes_old_stray_tmp_and_bak_files_but_keeps_todays(conn, settings):
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    old_tmp = settings.audio_dir / "2026-09-14.mp3.tmp"
    old_bak = settings.audio_dir / "2026-09-14.mp3.bak"
    today_tmp = settings.audio_dir / "2026-09-25.mp3.tmp"
    today_bak = settings.audio_dir / "2026-09-25.mp3.bak"
    for f in (old_tmp, old_bak, today_tmp, today_bak):
        f.write_bytes(b"x")
    retention.prune(conn, settings.audio_dir, date(2026, 9, 25))
    assert not old_tmp.exists() and not old_bak.exists()
    assert today_tmp.exists() and today_bak.exists()


def test_delete_episode_removes_row_and_file(conn, settings):
    seed_episode(conn, settings, "2026-09-24")
    assert retention.delete_episode(conn, settings.audio_dir, "2026-09-24") is True
    assert db.get_episode(conn, "2026-09-24") is None
    assert not (settings.audio_dir / "2026-09-24.mp3").exists()
    assert retention.delete_episode(conn, settings.audio_dir, "2026-09-24") is False
