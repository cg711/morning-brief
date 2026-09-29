from datetime import date, datetime, time, timedelta, timezone

import pytest

from morning_brief import notes
from tests.helpers import NOW  # 2026-09-25 08:00 Central

TODAY = date(2026, 9, 25)


def test_next_brief_date_around_run_at():
    run_at = time(7, 30)
    before = datetime(2026, 9, 25, 12, 29, tzinfo=timezone.utc)  # 07:29 Central
    after = datetime(2026, 9, 25, 12, 30, tzinfo=timezone.utc)   # 07:30 Central
    assert notes.next_brief_date(before, run_at) == TODAY
    assert notes.next_brief_date(after, run_at) == TODAY + timedelta(days=1)


@pytest.mark.parametrize("days,name", [(0, "today"), (1, "tomorrow"), (2, "Sunday"), (6, "Thursday"),
                                       (7, "Oct 2"), (17, "Oct 12")])
def test_day_name(days, name):
    assert notes.day_name(TODAY + timedelta(days=days), TODAY) == name


def test_host():
    assert notes.host("https://www.example.com/a") == "example.com"
    assert notes.host(None) is None


def test_add_note_and_due_notes(conn):
    a = notes.add_note(conn, "  Call   the dentist ", None, TODAY, NOW)
    b = notes.add_note(conn, "", "https://example.com/story", TODAY, NOW)
    notes.add_note(conn, "Later", None, TODAY + timedelta(days=2), NOW)
    assert notes.due_notes(conn, TODAY) == [{"id": a, "text": "Call the dentist", "url": None},
                                            {"id": b, "text": "", "url": "https://example.com/story"}]
    assert len(notes.due_notes(conn, TODAY + timedelta(days=2))) == 3
    assert [r["id"] for r in notes.pending_notes(conn)][:2] == [a, b]


def test_due_notes_capped_oldest_first(conn):
    ids = [notes.add_note(conn, f"note {n}", None, TODAY, NOW) for n in range(10)]
    assert [n["id"] for n in notes.due_notes(conn, TODAY)] == ids[:notes.NOTES_PER_BRIEF]


@pytest.mark.parametrize("text,url,day,match", [
    ("", None, TODAY, "write a note or share a link"),
    ("x" * 501, None, TODAY, "500"),
    ("bad\x07", None, TODAY, "control characters"),
    ("ok", "ftp://example.com", TODAY, "http"),
    ("ok", None, TODAY - timedelta(days=1), "today or a later day"),
])
def test_add_note_rejects(conn, text, url, day, match):
    with pytest.raises(notes.NoteError, match=match):
        notes.add_note(conn, text, url, day, NOW)


def test_mark_delivered_and_delete(conn):
    a = notes.add_note(conn, "one", None, TODAY, NOW)
    b = notes.add_note(conn, "two", None, TODAY, NOW)
    assert notes.mark_delivered(conn, [a], "2026-09-25") == 1
    assert notes.mark_delivered(conn, [a], "2026-09-26") == 0  # already delivered: unchanged
    assert conn.execute("SELECT delivered_on FROM notes WHERE id = ?", (a,)).fetchone()[0] == "2026-09-25"
    assert notes.mark_delivered(conn, [], "2026-09-25") == 0
    assert [n["id"] for n in notes.due_notes(conn, TODAY)] == [a, b]  # a still due: delivered for TODAY
    assert [n["id"] for n in notes.due_notes(conn, TODAY + timedelta(days=1))] == [b]  # not due once the day passes
    assert notes.delete_note(conn, a) is False  # delivered notes stay (until housekeeping)
    assert notes.delete_note(conn, b) is True and notes.pending_notes(conn) == []


def test_countdowns_validation(conn):
    with pytest.raises(notes.NoteError, match="label"):
        notes.add_countdown(conn, "   ", TODAY, NOW)
    with pytest.raises(notes.NoteError, match="60"):
        notes.add_countdown(conn, "x" * 61, TODAY, NOW)
    with pytest.raises(notes.NoteError, match="today or a later day"):
        notes.add_countdown(conn, "Past", TODAY - timedelta(days=1), NOW)
    c = notes.add_countdown(conn, "  Iceland ", TODAY + timedelta(days=13), NOW)
    assert [(r["id"], r["label"]) for r in notes.list_countdowns(conn, TODAY)] == [(c, "Iceland")]
    assert notes.delete_countdown(conn, c) is True and notes.list_countdowns(conn, TODAY) == []


@pytest.mark.parametrize("days,mentioned", [(101, False), (100, True), (99, False), (61, False), (60, True),
                                            (31, False), (30, True), (29, False), (22, False), (21, True),
                                            (15, False), (14, True), (13, False), (8, False), (7, True),
                                            (6, True), (1, True), (0, True)])
def test_due_countdowns_milestones(conn, days, mentioned):
    notes.add_countdown(conn, "Trip", TODAY + timedelta(days=days), NOW)
    expected = [{"label": "Trip", "days": days}] if mentioned else []
    assert notes.due_countdowns(conn, TODAY) == expected


def test_due_countdowns_sorted_and_past_ignored(conn):
    notes.add_countdown(conn, "Later", TODAY + timedelta(days=7), NOW)
    notes.add_countdown(conn, "Sooner", TODAY + timedelta(days=2), NOW)
    conn.execute("INSERT INTO countdowns (label, date, created_at) VALUES ('Gone', ?, ?)",
                 ((TODAY - timedelta(days=1)).isoformat(), NOW.isoformat()))
    assert notes.due_countdowns(conn, TODAY) == [{"label": "Sooner", "days": 2}, {"label": "Later", "days": 7}]


def test_housekeeping(conn):
    conn.execute("INSERT INTO countdowns (label, date, created_at) VALUES ('Gone', ?, ?)",
                 ((TODAY - timedelta(days=1)).isoformat(), NOW.isoformat()))
    keep = notes.add_countdown(conn, "Today", TODAY, NOW)
    old = notes.add_note(conn, "old", None, TODAY, NOW)
    recent = notes.add_note(conn, "recent", None, TODAY, NOW)
    notes.mark_delivered(conn, [old], (TODAY - timedelta(days=31)).isoformat())
    notes.mark_delivered(conn, [recent], (TODAY - timedelta(days=30)).isoformat())
    assert notes.housekeeping(conn, TODAY) == {"countdowns": 1, "notes": 1}
    assert [r["id"] for r in notes.list_countdowns(conn, TODAY)] == [keep]
    assert conn.execute("SELECT id FROM notes").fetchall()[0][0] == recent
