import json
import wave
from datetime import date, datetime, timedelta, timezone

import pytest

from morning_brief import db, house, house_render, pipeline, speech
from tests.helpers import NOW  # 2026-09-25 08:00 Central, a Friday

TODAY = date(2026, 9, 25)


class Pushes:
    def __init__(self):
        self.calls = []

    def __call__(self, settings, title, message, tags):
        self.calls.append((title, message))


def write_wav(path, seconds=1.0, rate=24000, width=2, channels=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x10\x10" * int(rate * seconds) * channels)


def run(settings, **kw):
    pushes = kw.setdefault("notify", Pushes())
    kw.setdefault("synthesize", speech.fake_synthesize)
    kw.setdefault("now", lambda: NOW)
    kw.setdefault("sleep", lambda s: None)
    return house_render.generate(settings, **kw), pushes


def episode_row(conn, day=TODAY):
    return house.get_episode(conn, day)


def test_load_clip(tmp_path):
    write_wav(tmp_path / "a.wav", seconds=0.5)
    clip = house_render.load_clip(tmp_path / "a.wav")
    assert clip.samples.dtype.name == "float32" and len(clip.samples) == 12000
    assert 0 < clip.samples[0] < 1
    write_wav(tmp_path / "stereo.wav", channels=2)
    write_wav(tmp_path / "slow.wav", rate=16000)
    (tmp_path / "empty.wav").write_bytes(b"")
    (tmp_path / "junk.wav").write_bytes(b"not a wav at all")
    for name in ("stereo.wav", "slow.wav", "empty.wav", "junk.wav", "missing.wav"):
        with pytest.raises(Exception):
            house_render.load_clip(tmp_path / name)


def test_an_empty_day_makes_no_episode(conn, settings):
    result, pushes = run(settings)
    assert result == "nothing" and episode_row(conn) is None and pushes.calls == []
    assert not (settings.house_dir / "2026-09-25.mp3").exists()


def test_a_day_with_content_is_spoken_stored_and_marked(conn, settings):
    sam = house.add_member(conn, "Sam", NOW)
    house.add_chore(conn, "trash night", 4, date(2026, 9, 21), [sam], NOW)
    note = house.add_note(conn, "We're out of paper towels", sam, NOW)
    todo = house.add_todo(conn, "Buy bulbs", None, None, NOW)
    result, pushes = run(settings)
    assert result == "ready" and pushes.calls == []
    mp3 = settings.house_dir / "2026-09-25.mp3"
    row = episode_row(conn)
    assert mp3.stat().st_size == row["audio_bytes"] > 0 and not mp3.with_name(mp3.name + ".tmp").exists()
    script = json.loads(row["script_json"])
    assert script["title"] == "House brief: Friday, September 25"
    assert [s["heading"] for s in script["sections"]] == ["Today", "To-dos", "Notes"]
    assert [c[0] for c in json.loads(row["chapters_json"])] == ["Introduction", "Today", "To-dos", "Notes", "Wrap-up"]
    assert conn.execute("SELECT announced_on FROM house_notes WHERE id = ?", (note,)).fetchone()[0] == "2026-09-25"
    assert conn.execute("SELECT announced_on FROM house_todos WHERE id = ?", (todo,)).fetchone()[0] == "2026-09-25"
    assert db.get_state(conn, house_render.ERROR_KEY) == ""


def test_an_approved_memo_is_spliced_in_and_marked_played(conn, settings):
    sam = house.add_member(conn, "Sam", NOW)
    memo = conn.execute("INSERT INTO house_memos (member_id, duration_s, status, created_at) "
                        "VALUES (?, 2.0, 'approved', 'x')", (sam,)).lastrowid
    write_wav(house.memo_path(settings.house_dir, memo), seconds=2.0)
    result, _ = run(settings)
    assert result == "ready"
    row = episode_row(conn)
    marks = json.loads(row["chapters_json"])
    assert [m[0] for m in marks] == ["Introduction", "Voice note from Sam", "Wrap-up"]
    assert row["duration_s"] > 2.0  # the clip is in there
    assert tuple(conn.execute("SELECT status, played_on FROM house_memos").fetchone()) == ("played", "2026-09-25")
    assert house.memo_path(settings.house_dir, memo).exists()  # kept until the next prune, for a regenerate


def test_a_missing_or_corrupt_memo_is_skipped_and_the_rest_still_publishes(conn, settings):
    sam = house.add_member(conn, "Sam", NOW)
    house.add_note(conn, "Still here", sam, NOW)
    bad = conn.execute("INSERT INTO house_memos (member_id, duration_s, status, created_at) "
                       "VALUES (?, 2.0, 'approved', 'x')", (sam,)).lastrowid
    missing = conn.execute("INSERT INTO house_memos (member_id, duration_s, status, created_at) "
                           "VALUES (?, 2.0, 'approved', 'x')", (sam,)).lastrowid
    house.memo_path(settings.house_dir, bad).parent.mkdir(parents=True)
    house.memo_path(settings.house_dir, bad).write_bytes(b"garbage")
    result, pushes = run(settings)
    assert result == "ready" and pushes.calls == []
    statuses = dict(conn.execute("SELECT id, status FROM house_memos").fetchall())
    assert statuses == {bad: "rejected", missing: "rejected"}
    assert not house.memo_path(settings.house_dir, bad).exists()      # the unusable file goes at once
    headings = [s["heading"] for s in json.loads(episode_row(conn)["script_json"])["sections"]]
    assert headings == ["Notes"]


def test_only_bad_memos_means_nothing_to_say(conn, settings):
    sam = house.add_member(conn, "Sam", NOW)
    conn.execute("INSERT INTO house_memos (member_id, duration_s, status, created_at) VALUES (?, 2.0, 'approved', 'x')",
                 (sam,))
    result, _ = run(settings)
    assert result == "nothing" and episode_row(conn) is None


def test_a_failed_run_marks_nothing_and_tells_the_owner(conn, settings):
    note = house.add_note(conn, "Still here", None, NOW)

    def broken(*args):
        raise RuntimeError("espeak exploded")

    result, pushes = run(settings, synthesize=broken)
    assert result == "failed" and episode_row(conn) is None
    assert pushes.calls == [("House brief failed", "RuntimeError: espeak exploded")]
    assert conn.execute("SELECT announced_on FROM house_notes WHERE id = ?", (note,)).fetchone()[0] is None
    assert db.get_state(conn, house_render.ERROR_KEY) == "2026-09-25|RuntimeError: espeak exploded"
    assert house_render.failed_day(conn) == TODAY


def test_retry_runs_only_after_a_failure_and_clears_it(conn, settings):
    house.add_note(conn, "Still here", None, NOW)
    quiet = house_render.retry(settings, synthesize=speech.fake_synthesize, now=lambda: NOW, notify=Pushes())
    assert quiet == "nothing" and episode_row(conn) is None  # nothing had failed, so no retry
    run(settings, synthesize=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert house_render.retry(settings, synthesize=speech.fake_synthesize, now=lambda: NOW,
                              notify=Pushes()) == "ready"
    assert episode_row(conn) is not None and house_render.failed_day(conn) is None
    assert house_render.retry(settings, synthesize=speech.fake_synthesize, now=lambda: NOW,
                              notify=Pushes()) == "nothing"


def test_a_busy_speech_lock_gives_up_and_tells_the_owner(conn, settings):
    house.add_note(conn, "Still here", None, NOW)
    with pipeline.run_lock(settings):
        result, pushes = run(settings, lock_wait_s=0)
    assert result == "busy" and episode_row(conn) is None
    assert pushes.calls == [("House brief failed", "speech was busy")]


def test_a_scheduled_run_does_not_replace_todays_episode(conn, settings):
    house.add_note(conn, "Still here", None, NOW)
    assert run(settings)[0] == "ready"
    first = episode_row(conn)["updated_at"]
    result, _ = run(settings, now=lambda: NOW + timedelta(hours=1))
    assert result == "nothing" and episode_row(conn)["updated_at"] == first


def test_regenerating_the_same_day_keeps_its_items(conn, settings):
    sam = house.add_member(conn, "Sam", NOW)
    house.add_note(conn, "Still here", sam, NOW)
    memo = conn.execute("INSERT INTO house_memos (member_id, duration_s, status, created_at) "
                        "VALUES (?, 1.0, 'approved', 'x')", (sam,)).lastrowid
    write_wav(house.memo_path(settings.house_dir, memo), seconds=1.0)
    assert run(settings)[0] == "ready"
    later = NOW + timedelta(hours=1)
    result, _ = run(settings, manual=True, now=lambda: later)
    assert result == "ready"
    row = episode_row(conn)
    assert row["updated_at"] == later.isoformat() and row["created_at"] == NOW.isoformat()
    headings = [s["heading"] for s in json.loads(row["script_json"])["sections"]]
    assert headings == ["Notes", "Voice note from Sam"]
    assert conn.execute("SELECT announced_on FROM house_notes").fetchone()[0] == "2026-09-25"  # not re-dated
    # the nightly prune on the same day must not take the memo audio away from another regenerate
    house.prune(conn, settings.house_dir, TODAY)
    assert house.memo_path(settings.house_dir, memo).exists()
    house.prune(conn, settings.house_dir, TODAY + timedelta(days=1))
    assert not house.memo_path(settings.house_dir, memo).exists()


def test_the_episode_is_dated_by_the_local_day_not_the_utc_day(conn, settings):
    house.add_note(conn, "Late note", None, NOW)
    late = datetime(2026, 9, 26, 4, 30, tzinfo=timezone.utc)  # 23:30 on Sep 25 in Chicago
    result, _ = run(settings, now=lambda: late)
    assert result == "ready" and episode_row(conn, date(2026, 9, 25)) is not None
    assert episode_row(conn, date(2026, 9, 26)) is None
    assert (settings.house_dir / "2026-09-25.mp3").exists()


def test_start_runs_a_manual_generate_in_a_thread(conn, settings):
    house.add_note(conn, "Still here", None, NOW)
    thread = house_render.start(settings, synthesize=speech.fake_synthesize, now=lambda: NOW, notify=Pushes())
    thread.join(timeout=60)
    assert not thread.is_alive() and episode_row(conn) is not None


def test_retry_after_midnight_rebuilds_the_day_that_failed(conn, settings):
    house.add_note(conn, "Late note", None, NOW)
    late = datetime(2026, 9, 26, 4, 50, tzinfo=timezone.utc)  # 23:50 on Sep 25 in Chicago
    run(settings, now=lambda: late, synthesize=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert db.get_state(conn, house_render.ERROR_KEY).startswith("2026-09-25|")
    after_midnight = datetime(2026, 9, 26, 5, 15, tzinfo=timezone.utc)  # 00:15 on Sep 26 in Chicago
    result = house_render.retry(settings, synthesize=speech.fake_synthesize, now=lambda: after_midnight,
                                notify=Pushes())
    assert result == "ready"
    assert episode_row(conn, date(2026, 9, 25)) is not None and episode_row(conn, date(2026, 9, 26)) is None
    assert (settings.house_dir / "2026-09-25.mp3").exists()
    assert db.get_state(conn, house_render.ERROR_KEY) == ""


@pytest.mark.parametrize("value", ["", "garbage", "|x", "2026-13-45|x"])
def test_retry_ignores_an_empty_or_malformed_error(conn, settings, value):
    house.add_note(conn, "Still here", None, NOW)
    db.set_state(conn, house_render.ERROR_KEY, value)
    assert house_render.retry(settings, synthesize=speech.fake_synthesize, now=lambda: NOW,
                              notify=Pushes()) == "nothing"
    assert episode_row(conn) is None


def test_a_failing_state_write_still_tells_the_owner(settings, monkeypatch):
    pushes = Pushes()

    def boom(*args):
        raise RuntimeError("db is down")

    monkeypatch.setattr(db, "set_state", boom)
    monkeypatch.setattr(db, "connect", boom)
    result = house_render.generate(settings, now=lambda: NOW, notify=pushes, sleep=lambda s: None)
    assert result == "failed" and pushes.calls == [("House brief failed", "RuntimeError: db is down")]


def test_two_quick_starts_make_only_one_thread(conn, settings):
    import threading
    house.add_note(conn, "Still here", None, NOW)
    release, calls = threading.Event(), []

    def slow(*args, **kwargs):
        calls.append(1)
        assert release.wait(timeout=30)
        return speech.fake_synthesize(*args, **kwargs)

    try:
        first = house_render.start(settings, synthesize=slow, now=lambda: NOW, notify=Pushes())
        second = house_render.start(settings, synthesize=slow, now=lambda: NOW, notify=Pushes())
        assert second is first
    finally:
        release.set()
    first.join(timeout=60)
    assert not first.is_alive() and calls == [1]
    third = house_render.start(settings, synthesize=speech.fake_synthesize, now=lambda: NOW, notify=Pushes())
    third.join(timeout=60)
    assert third is not first


def test_a_later_quiet_day_ends_an_old_failures_retry(conn, settings):
    house.add_note(conn, "Still here", None, NOW)
    run(settings, synthesize=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))  # Sep 25 fails
    assert house_render.failed_day(conn) == TODAY
    house.delete_note(conn, house.pending_notes(conn)[0]["id"])  # nothing left to say on Sep 26
    next_day = NOW + timedelta(days=1)
    result, _ = run(settings, now=lambda: next_day)
    assert result == "nothing" and db.get_state(conn, house_render.ERROR_KEY) == ""
    assert house_render.retry(settings, synthesize=speech.fake_synthesize, now=lambda: next_day,
                              notify=Pushes()) == "nothing"
    assert episode_row(conn, TODAY) is None and episode_row(conn, date(2026, 9, 26)) is None
