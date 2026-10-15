import sqlite3
from datetime import date

import pytest

from morning_brief import house
from tests.helpers import NOW  # 2026-09-25 08:00 Central, a Friday

TODAY = date(2026, 9, 25)


def member(conn, name="Sam"):
    return house.add_member(conn, name, NOW)


def test_members(conn):
    a = member(conn, "  Sam  ")
    b = member(conn, "Alex")
    members = house.list_members(conn)
    assert [(m["id"], m["name"]) for m in members] == [(a, "Sam"), (b, "Alex")]
    assert all(len(m["token"]) >= 20 for m in members) and members[0]["token"] != members[1]["token"]
    with pytest.raises(house.HouseError, match="already a"):
        member(conn, "sam")


@pytest.mark.parametrize("name,match", [("", "write something"), ("   ", "write something"),
                                        ("x" * 31, "30"), ("Sam\x07", "control characters")])
def test_member_validation(conn, name, match):
    with pytest.raises(house.HouseError, match=match):
        house.add_member(conn, name, NOW)


def test_revoking_a_member_drops_them_from_rotations_and_keeps_their_name(conn):
    a, b = member(conn, "Sam"), member(conn, "Alex")
    house.add_chore(conn, "Trash", 1, TODAY, [a, b], NOW)
    assert house.revoke_member(conn, a, NOW) is True and house.revoke_member(conn, a, NOW) is False
    assert house.list_chores(conn)[0]["rotation"] == [b]
    assert house.member_names(conn)[a] == "Sam"
    assert [m["name"] for m in house.list_members(conn)] == ["Alex"]
    member(conn, "Sam")  # the name is free again


def test_revoke_unknown_or_already_revoked_member_returns_false(conn):
    a = member(conn)
    assert house.revoke_member(conn, 999, NOW) is False
    assert house.revoke_member(conn, a, NOW) is True
    assert house.revoke_member(conn, a, NOW) is False


def test_revoke_member_is_atomic(conn):
    a, b = member(conn, "Sam"), member(conn, "Alex")
    good = house.add_chore(conn, "Trash", 1, TODAY, [a, b], NOW)
    conn.execute("INSERT INTO house_chores (name, weekday, start_date, rotation_json, created_at) "
                 "VALUES ('Bad', 2, ?, 'not json', ?)", (TODAY.isoformat(), NOW.isoformat()))
    with pytest.raises(ValueError):  # json.JSONDecodeError
        house.revoke_member(conn, a, NOW)
    assert conn.execute("SELECT revoked_at FROM house_members WHERE id = ?", (a,)).fetchone()["revoked_at"] is None
    assert not conn.in_transaction
    row = conn.execute("SELECT rotation_json FROM house_chores WHERE id = ?", (good,)).fetchone()
    assert row["rotation_json"] == f"[{a}, {b}]"


def test_chores(conn):
    a, b = member(conn, "Sam"), member(conn, "Alex")
    chore = house.add_chore(conn, " Trash   night ", 1, TODAY, [b, a, b], NOW)
    (c,) = house.list_chores(conn)
    assert c == {"id": chore, "name": "Trash night", "weekday": 1, "start_date": TODAY, "rotation": [b, a]}
    assert house.delete_chore(conn, chore) is True and house.delete_chore(conn, chore) is False


@pytest.mark.parametrize("kwargs,match", [({"name": ""}, "write something"), ({"weekday": 7}, "weekday"),
                                          ({"weekday": -1}, "weekday"), ({"weekday": "1"}, "weekday"),
                                          ({"weekday": True}, "weekday"), ({"rotation": [999]}, "housemates")])
def test_chore_validation(conn, kwargs, match):
    args = {"name": "Trash", "weekday": 1, "rotation": []}
    args.update(kwargs)
    with pytest.raises(house.HouseError, match=match):
        house.add_chore(conn, args["name"], args["weekday"], TODAY, args["rotation"], NOW)


def test_chore_done_is_idempotent_and_needs_a_real_chore(conn):
    a = member(conn)
    chore = house.add_chore(conn, "Trash", 4, TODAY, [a], NOW)
    assert house.chore_is_done(conn, chore, TODAY) is False
    assert house.mark_chore_done(conn, chore, TODAY, a, NOW) is True
    assert house.mark_chore_done(conn, chore, TODAY, a, NOW) is False
    assert house.chore_is_done(conn, chore, TODAY) is True
    assert house.mark_chore_done(conn, 999, TODAY, a, NOW) is False


def test_todos(conn):
    a = member(conn)
    later = house.add_todo(conn, "Buy bulbs", None, date(2026, 10, 1), NOW)
    soon = house.add_todo(conn, "Call landlord", a, date(2026, 9, 26), NOW)
    plain = house.add_todo(conn, "Sweep porch", a, None, NOW)
    assert [t["id"] for t in house.list_todos(conn)] == [soon, later, plain]
    assert house.claim_todo(conn, plain, a) is True
    assert house.list_todos(conn)[2]["claimed_by"] == a
    assert house.claim_todo(conn, plain, None) is True and house.list_todos(conn)[2]["claimed_by"] is None
    with pytest.raises(house.HouseError, match="housemate"):
        house.claim_todo(conn, plain, 999)
    assert house.finish_todo(conn, soon, a, NOW) is True and house.finish_todo(conn, soon, a, NOW) is False
    assert [t["id"] for t in house.list_todos(conn)] == [later, plain]
    assert house.claim_todo(conn, soon, a) is False  # finished
    assert house.delete_todo(conn, later) is True and house.delete_todo(conn, later) is False


@pytest.mark.parametrize("text,match", [("", "write something"), ("x" * 201, "200"), ("a\x00b", "control characters")])
def test_todo_validation(conn, text, match):
    with pytest.raises(house.HouseError, match=match):
        house.add_todo(conn, text, None, None, NOW)


def test_notes(conn):
    a = member(conn)
    one = house.add_note(conn, "  We're   out of paper towels ", a, NOW)
    two = house.add_note(conn, "Owner note", None, NOW)
    notes = house.pending_notes(conn)
    assert [(n["id"], n["text"], n["member_id"]) for n in notes] == [(one, "We're out of paper towels", a),
                                                                      (two, "Owner note", None)]
    assert house.delete_note(conn, one) is True and house.delete_note(conn, one) is False
    conn.execute("UPDATE house_notes SET announced_on = '2026-09-25' WHERE id = ?", (two,))
    assert house.pending_notes(conn) == [] and house.delete_note(conn, two) is False  # announced notes stay


def test_hostile_text_is_stored_as_data_and_bad_text_is_refused(conn):
    note = house.add_note(conn, "<script>alert(1)</script> & <b>hi</b>", None, NOW)
    assert house.pending_notes(conn)[0]["text"] == "<script>alert(1)</script> & <b>hi</b>"
    for bad in ("x" * 301, "bell\x07", "nul\x00", "esc\x1b[31m"):
        with pytest.raises(house.HouseError):
            house.add_note(conn, bad, None, NOW)
    assert [n["id"] for n in house.pending_notes(conn)] == [note]


import json
from datetime import datetime, timedelta, timezone


def test_events_and_prep(conn):
    a = member(conn)
    ev = house.add_event(conn, " Game   night ", date(2026, 9, 29), a, ["chairs", "  ", "snacks"], NOW, TODAY)
    past = house.add_event(conn, "Today thing", TODAY, None, [], NOW, TODAY)
    conn.execute("UPDATE house_events SET day = '2026-09-20' WHERE id = ?", (past,))
    (event,) = house.list_events(conn, TODAY)
    assert (event["id"], event["title"], event["day"], event["host_id"]) == (ev, "Game night", date(2026, 9, 29), a)
    assert [(p["text"], p["done"]) for p in event["prep"]] == [("chairs", False), ("snacks", False)]
    assert house.tick_prep(conn, event["prep"][0]["id"], a, NOW) is True
    assert house.tick_prep(conn, event["prep"][0]["id"], a, NOW) is False
    assert house.list_events(conn, TODAY)[0]["prep"][0]["done"] is True
    assert house.delete_event(conn, ev) is True
    assert conn.execute("SELECT COUNT(*) FROM house_prep").fetchone()[0] == 0


@pytest.mark.parametrize("kwargs,match", [
    ({"title": ""}, "write something"), ({"title": "x" * 81}, "80"),
    ({"day": date(2026, 9, 24)}, "later day"), ({"host_id": 999}, "host"),
    ({"prep": ["x" * 101]}, "100"), ({"prep": ["item"] * 21}, "20"), ({"prep": ["bad\x07"]}, "control")])
def test_event_validation(conn, kwargs, match):
    args = {"title": "Party", "day": TODAY, "host_id": None, "prep": []}
    args.update(kwargs)
    with pytest.raises(house.HouseError, match=match):
        house.add_event(conn, args["title"], args["day"], args["host_id"], args["prep"], NOW, TODAY)
    assert conn.execute("SELECT COUNT(*) FROM house_events").fetchone()[0] == 0


def test_upkeep(conn):
    every = house.add_upkeep(conn, "Furnace filter", "every", NOW, TODAY, every_days=90, last_done=date(2026, 8, 1))
    yearly = house.add_upkeep(conn, "Hose bibs", "yearly", NOW, TODAY, month=10, day=15)
    items = house.list_upkeep(conn)
    assert items[0] == {"id": every, "label": "Furnace filter", "kind": "every", "every_days": 90, "month": None,
                        "day": None, "last_done": date(2026, 8, 1), "created": TODAY}
    assert (items[1]["kind"], items[1]["month"], items[1]["day"], items[1]["every_days"]) == ("yearly", 10, 15, None)
    assert house.mark_upkeep_done(conn, every, date(2026, 9, 26)) is True
    assert house.list_upkeep(conn)[0]["last_done"] == date(2026, 9, 26)
    assert house.mark_upkeep_done(conn, 999, TODAY) is False
    assert house.delete_upkeep(conn, yearly) is True and house.delete_upkeep(conn, yearly) is False


@pytest.mark.parametrize("kwargs,match", [
    ({"kind": "weekly"}, "how often"), ({"kind": "every", "every_days": None}, "every how many"),
    ({"kind": "every", "every_days": 0}, "every how many"), ({"kind": "every", "every_days": 3651}, "every how many"),
    ({"kind": "every", "every_days": True}, "every how many"), ({"kind": "yearly"}, "month and day"),
    ({"kind": "yearly", "month": 2, "day": 29}, "month and day"), ({"kind": "yearly", "month": 13, "day": 1}, "month and day"),
    ({"kind": "every", "every_days": 5, "last_done": date(2026, 9, 26)}, "future"), ({"label": ""}, "write something")])
def test_upkeep_validation(conn, kwargs, match):
    args = {"label": "Filter", "kind": "every", "every_days": 30}
    args.update(kwargs)
    with pytest.raises(house.HouseError, match=match):
        house.add_upkeep(conn, args.pop("label"), args.pop("kind"), NOW, TODAY, **args)


def script_for(title="House brief: Friday, September 25"):
    return {"title": title, "intro": "Hi.", "sections": [], "outro": "Bye."}


def test_episodes_round_trip_and_are_replaced_by_date(conn, tmp_path):
    assert house.get_episode(conn, TODAY) is None
    house.save_episode(conn, TODAY, script_for(), duration_s=95.5, audio_bytes=1234, word_count=40,
                       chapters=[("Introduction", 0.0), ("Wrap-up", 90.0)], now=NOW)
    later = NOW + timedelta(hours=1)
    house.save_episode(conn, TODAY, script_for("Second"), duration_s=100.0, audio_bytes=2000, word_count=50,
                       chapters=None, now=later)
    (row,) = house.list_episodes(conn)
    assert json.loads(row["script_json"])["title"] == "Second" and row["audio_bytes"] == 2000
    assert row["chapters_json"] is None
    assert row["created_at"] == NOW.isoformat() and row["updated_at"] == later.isoformat()
    assert house.episode_path(tmp_path, TODAY) == tmp_path / "2026-09-25.mp3"
    assert house.memo_path(tmp_path, 7) == tmp_path / "memos" / "7.wav"


def test_mark_announced_marks_only_unannounced_items(conn):
    a = member(conn)
    n1, n2 = house.add_note(conn, "one", None, NOW), house.add_note(conn, "two", None, NOW)
    t1 = house.add_todo(conn, "do it", None, None, NOW)
    conn.execute("UPDATE house_notes SET announced_on = '2026-09-20' WHERE id = ?", (n2,))
    memo = conn.execute("INSERT INTO house_memos (member_id, duration_s, status, created_at) "
                        "VALUES (?, 5, 'approved', 'x')", (a,)).lastrowid
    facts = {"notes": [{"id": n1}, {"id": n2}], "todos": [{"id": t1}], "memos": [{"id": memo}]}
    house.mark_announced(conn, facts, TODAY)
    house.mark_announced(conn, facts, TODAY + timedelta(days=1))  # a second pass changes nothing
    notes = {r["id"]: r["announced_on"] for r in conn.execute("SELECT id, announced_on FROM house_notes")}
    assert notes == {n1: "2026-09-25", n2: "2026-09-20"}
    assert conn.execute("SELECT announced_on FROM house_todos").fetchone()[0] == "2026-09-25"
    assert tuple(conn.execute("SELECT status, played_on FROM house_memos").fetchone()) == ("played", "2026-09-25")
    house.mark_announced(conn, {"notes": [], "todos": [], "memos": []}, TODAY)  # empty lists are fine


def add_memo(conn, status, played_on=None, member_id=1):
    return conn.execute("INSERT INTO house_memos (member_id, duration_s, status, created_at, played_on) "
                        "VALUES (?, 5, ?, 'x', ?)", (member_id, status, played_on)).lastrowid


def test_reject_memo(conn):
    pending, played = add_memo(conn, "pending"), add_memo(conn, "played", "2026-09-25")
    assert house.reject_memo(conn, pending) is True and house.reject_memo(conn, pending) is False
    assert house.reject_memo(conn, played) is False
    assert conn.execute("SELECT status FROM house_memos WHERE id = ?", (pending,)).fetchone()[0] == "rejected"


def test_prune_keeps_the_newest_episodes_and_the_right_memo_files(conn, tmp_path):
    for n in range(1, 17):  # 16 episodes, Sep 10 to Sep 25
        day = date(2026, 9, 9 + n)
        house.save_episode(conn, day, script_for(), duration_s=1, audio_bytes=1, word_count=1, chapters=None, now=NOW)
        house.episode_path(tmp_path, day).write_bytes(b"mp3")
    (tmp_path / "memos").mkdir()
    ids = {name: add_memo(conn, status, played) for name, status, played in [
        ("played_before", "played", "2026-09-24"), ("played_today", "played", "2026-09-25"),
        ("approved", "approved", None), ("pending", "pending", None), ("rejected", "rejected", None)]}
    for memo_id in ids.values():
        house.memo_path(tmp_path, memo_id).write_bytes(b"wav")
    result = house.prune(conn, tmp_path, TODAY)
    assert result == {"episodes": 2, "memo_files": 2}
    assert [r["date"] for r in house.list_episodes(conn)][-1] == "2026-09-12"
    assert not house.episode_path(tmp_path, date(2026, 9, 10)).exists()
    assert house.episode_path(tmp_path, date(2026, 9, 12)).exists()
    assert not house.memo_path(tmp_path, ids["played_before"]).exists()
    assert not house.memo_path(tmp_path, ids["rejected"]).exists()
    for kept in ("played_today", "approved", "pending"):  # today's regenerate may still need played_today
        assert house.memo_path(tmp_path, ids[kept]).exists()
    assert house.prune(conn, tmp_path, TODAY) == {"episodes": 0, "memo_files": 0}


def test_prune_with_no_house_folder_is_fine(conn, tmp_path):
    assert house.prune(conn, tmp_path / "nowhere", TODAY) == {"episodes": 0, "memo_files": 0}


def test_add_event_is_all_or_nothing(conn):
    conn.execute("CREATE TRIGGER boom BEFORE INSERT ON house_prep WHEN NEW.text = 'boom' "
                 "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        house.add_event(conn, "Party", TODAY, None, ["ok", "boom"], NOW, TODAY)
    assert conn.execute("SELECT COUNT(*) FROM house_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM house_prep").fetchone()[0] == 0
    assert house.add_event(conn, "Party", TODAY, None, ["ok"], NOW, TODAY)  # the connection is still usable


def test_mark_announced_is_all_or_nothing(conn):
    a = member(conn)
    n1 = house.add_note(conn, "one", None, NOW)
    memo = add_memo(conn, "approved", member_id=a)
    conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON house_memos BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        house.mark_announced(conn, {"notes": [{"id": n1}], "todos": [], "memos": [{"id": memo}]}, TODAY)
    assert conn.execute("SELECT announced_on FROM house_notes").fetchone()[0] is None


def test_prune_is_all_or_nothing_and_keeps_files_on_failure(conn, tmp_path):
    for n in range(1, 4):
        day = date(2026, 9, n)
        house.save_episode(conn, day, script_for(), duration_s=1, audio_bytes=1, word_count=1, chapters=None, now=NOW)
        house.episode_path(tmp_path, day).write_bytes(b"mp3")
    conn.execute("CREATE TRIGGER boom BEFORE DELETE ON house_episodes WHEN OLD.date = '2026-09-01' "
                 "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        house.prune(conn, tmp_path, TODAY, keep=1)
    assert len(house.list_episodes(conn)) == 3
    assert all(house.episode_path(tmp_path, date(2026, 9, n)).exists() for n in range(1, 4))


FAR = date(9999, 12, 31)
LONG_AGO = date(1900, 1, 1)
SPAN = "within ten years"


def test_far_away_dates_are_refused_and_nothing_is_stored(conn):
    for bad in (FAR, LONG_AGO, date(2037, 1, 1)):
        with pytest.raises(house.HouseError, match=SPAN):
            house.add_chore(conn, "Trash", 1, bad, [], NOW)
    for bad in (FAR, date(2037, 1, 1)):
        with pytest.raises(house.HouseError, match=SPAN):
            house.add_event(conn, "Party", bad, None, [], NOW, TODAY)
        with pytest.raises(house.HouseError, match=SPAN):
            house.add_todo(conn, "Bulbs", None, bad, NOW)
    with pytest.raises(house.HouseError, match=SPAN):
        house.add_todo(conn, "Bulbs", None, LONG_AGO, NOW)
    with pytest.raises(house.HouseError, match=SPAN):
        house.add_upkeep(conn, "Filter", "every", NOW, TODAY, every_days=5, last_done=LONG_AGO)
    for table in ("house_chores", "house_events", "house_todos", "house_upkeep"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_dates_near_the_edge_of_the_window_are_accepted(conn):
    edge = date(2036, 9, 20)  # within 3660 days of TODAY
    house.add_chore(conn, "Trash", 1, edge, [], NOW)
    house.add_event(conn, "Party", edge, None, [], NOW, TODAY)
    house.add_todo(conn, "Bulbs", None, edge, NOW)
    house.add_upkeep(conn, "Filter", "every", NOW, TODAY, every_days=5, last_done=date(2016, 10, 1))


def test_member_for_token_finds_only_active_housemates(conn):
    sam = member(conn, "Sam")
    token = house.list_members(conn)[0]["token"]
    found = house.member_for_token(conn, token)
    assert found["id"] == sam and found["name"] == "Sam"
    for bad in ("", "x", "a" * 22, token[:-1], token + "x", "é" * 22, None, 5, "../" + token, token + "\x00"):
        assert house.member_for_token(conn, bad) is None
    house.revoke_member(conn, sam, NOW)
    assert house.member_for_token(conn, token) is None


def test_memos_wait_for_approval(conn):
    sam = member(conn, "Sam")
    first = house.add_memo(conn, sam, 12.5, NOW, 3)
    second = house.add_memo(conn, sam, 3.0, NOW, 3)
    assert house.pending_memo_count(conn, sam) == 2
    assert [m["id"] for m in house.list_pending_memos(conn)] == [first, second]
    assert tuple(house.get_memo(conn, first)[k] for k in ("status", "duration_s", "member_id")) == ("pending", 12.5, sam)
    assert house.approve_memo(conn, first, NOW) is True and house.approve_memo(conn, first, NOW) is False
    assert house.get_memo(conn, first)["approved_at"] == NOW.isoformat()
    assert house.approved_waiting_count(conn) == 1 and house.pending_memo_count(conn, sam) == 1
    assert house.reject_memo(conn, second) is True and house.pending_memo_count(conn, sam) == 0
    assert house.approve_memo(conn, second, NOW) is False and house.approve_memo(conn, 999, NOW) is False
    assert house.get_memo(conn, 999) is None


def test_the_pending_cap_is_per_housemate(conn):
    sam, alex = member(conn, "Sam"), member(conn, "Alex")
    house.add_memo(conn, sam, 5.0, NOW, 2)
    house.add_memo(conn, sam, 5.0, NOW, 2)
    with pytest.raises(house.HouseError, match="2 voice notes"):
        house.add_memo(conn, sam, 5.0, NOW, 2)
    assert house.add_memo(conn, alex, 5.0, NOW, 2)   # someone else is unaffected
    assert conn.execute("SELECT COUNT(*) FROM house_memos").fetchone()[0] == 3


@pytest.mark.parametrize("duration", [0, -1, 3601, float("nan")])
def test_a_memo_needs_a_sane_length(conn, duration):
    sam = member(conn, "Sam")
    with pytest.raises(house.HouseError):
        house.add_memo(conn, sam, duration, NOW, 3)
    assert conn.execute("SELECT COUNT(*) FROM house_memos").fetchone()[0] == 0


def test_delete_memo_only_removes_pending_ones(conn):
    sam = member(conn, "Sam")
    pending = house.add_memo(conn, sam, 5.0, NOW, 5)
    approved = house.add_memo(conn, sam, 5.0, NOW, 5)
    house.approve_memo(conn, approved, NOW)
    house.delete_memo(conn, pending)
    house.delete_memo(conn, approved)
    assert [r["id"] for r in conn.execute("SELECT id FROM house_memos")] == [approved]


def test_the_to_do_list_and_the_note_queue_have_flood_caps(conn):
    a = member(conn)
    assert (house.MAX_OPEN_TODOS, house.MAX_PENDING_NOTES) == (100, 100)
    ids = [house.add_todo(conn, f"t{i}", a, None, NOW) for i in range(house.MAX_OPEN_TODOS)]
    with pytest.raises(house.HouseError, match="to-do list is full for now: finish a few first"):
        house.add_todo(conn, "one too many", a, None, NOW)
    assert len(house.list_todos(conn)) == 100
    house.finish_todo(conn, ids[0], a, NOW)                      # finishing frees a slot
    house.add_todo(conn, "fits now", a, None, NOW)
    for i in range(house.MAX_PENDING_NOTES):
        house.add_note(conn, f"n{i}", a, NOW)
    with pytest.raises(house.HouseError, match="lot of notes waiting: try again after the next episode"):
        house.add_note(conn, "one too many", a, NOW)
    assert len(house.pending_notes(conn)) == 100
    conn.execute("UPDATE house_notes SET announced_on = '2026-09-25' WHERE id = (SELECT MIN(id) FROM house_notes)")
    house.add_note(conn, "fits after an episode", a, NOW)        # announced notes don't count


def test_rejecting_a_memo_deletes_its_audio_at_once(conn, tmp_path):
    (tmp_path / "memos").mkdir()
    pending, approved, played, nofile = (add_memo(conn, "pending"), add_memo(conn, "approved"),
                                         add_memo(conn, "played", "2026-09-25"), add_memo(conn, "pending"))
    for memo_id in (pending, approved, played):
        house.memo_path(tmp_path, memo_id).write_bytes(b"wav")
    assert house.reject_memo(conn, pending, tmp_path) is True
    assert not house.memo_path(tmp_path, pending).exists()
    assert house.reject_memo(conn, played, tmp_path) is False and house.memo_path(tmp_path, played).exists()
    assert house.reject_memo(conn, nofile, tmp_path) is True        # a missing file is fine
    assert house.reject_memo(conn, approved) is True and house.memo_path(tmp_path, approved).exists()  # no dir given


def test_removing_a_member_clears_their_unread_notes_and_unapproved_memos(conn):
    a, b = member(conn, "Sam"), member(conn, "Alex")
    mine_unread, mine_read = house.add_note(conn, "mine unread", a, NOW), house.add_note(conn, "mine read", a, NOW)
    theirs, owner = house.add_note(conn, "alex", b, NOW), house.add_note(conn, "owner", None, NOW)
    conn.execute("UPDATE house_notes SET announced_on = '2026-09-24' WHERE id = ?", (mine_read,))
    todo = house.add_todo(conn, "Sam's todo", a, None, NOW)
    pending, approved, played, other = (add_memo(conn, "pending", member_id=a), add_memo(conn, "approved", member_id=a),
                                        add_memo(conn, "played", "2026-09-24", member_id=a),
                                        add_memo(conn, "pending", member_id=b))
    assert house.revoke_member(conn, a, NOW) is True
    assert {n["id"] for n in house.pending_notes(conn)} == {theirs, owner}
    assert conn.execute("SELECT id FROM house_notes WHERE id = ?", (mine_read,)).fetchone() is not None   # announced: kept
    assert conn.execute("SELECT id FROM house_notes WHERE id = ?", (mine_unread,)).fetchone() is None
    statuses = dict(conn.execute("SELECT id, status FROM house_memos").fetchall())
    assert statuses == {pending: "rejected", approved: "approved", played: "played", other: "pending"}
    assert [t["id"] for t in house.list_todos(conn)] == [todo]


def test_a_failed_removal_keeps_the_notes_and_memos(conn):
    a = member(conn, "Sam")
    house.add_note(conn, "keep me", a, NOW)
    memo = add_memo(conn, "pending", member_id=a)
    conn.execute("INSERT INTO house_chores (name, weekday, start_date, rotation_json, created_at) "
                 "VALUES ('Bad', 2, ?, 'not json', ?)", (TODAY.isoformat(), NOW.isoformat()))
    with pytest.raises(ValueError):
        house.revoke_member(conn, a, NOW)
    assert len(house.pending_notes(conn)) == 1
    assert conn.execute("SELECT status FROM house_memos WHERE id = ?", (memo,)).fetchone()[0] == "pending"
