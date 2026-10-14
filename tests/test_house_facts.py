from datetime import date, datetime, time, timedelta, timezone

import pytest

from morning_brief import house, house_facts
from tests.helpers import NOW  # 2026-09-25 08:00 Central, a Friday

TODAY = date(2026, 9, 25)


def person(conn, name="Sam"):
    return house.add_member(conn, name, NOW)


def at(day):
    return datetime(day.year, day.month, day.day, 13, 0, tzinfo=timezone.utc)


def test_assignee_rotates_weekly_from_the_start_week():
    chore = {"weekday": 4, "start_date": date(2026, 9, 21), "rotation": [1, 2, 3]}
    assert [house_facts.assignee(chore, d) for d in (date(2026, 9, 25), date(2026, 9, 28), date(2026, 10, 12))] == [1, 2, 1]
    assert house_facts.assignee({**chore, "rotation": []}, TODAY) is None


@pytest.mark.parametrize("today,expected", [
    (date(2026, 9, 24), date(2026, 9, 25)),   # Thursday: tomorrow
    (date(2026, 9, 25), date(2026, 9, 25)),   # Friday: today
    (date(2026, 9, 26), date(2026, 9, 25)),   # one day over
    (date(2026, 9, 27), date(2026, 9, 25)),   # two days over
    (date(2026, 9, 28), None),                # three days over: dropped
    (date(2026, 9, 23), None)])               # two days before: not yet
def test_occurrence_window(today, expected):
    chore = {"weekday": 4, "start_date": date(2026, 9, 1), "rotation": []}
    assert house_facts.occurrence_in_window(chore, today) == expected


def test_occurrences_before_the_start_date_are_ignored():
    chore = {"weekday": 4, "start_date": date(2026, 10, 30), "rotation": []}
    assert house_facts.occurrence_in_window(chore, TODAY) is None


def test_next_occurrence():
    chore = {"weekday": 1, "start_date": date(2026, 9, 1), "rotation": []}
    assert house_facts.next_occurrence(chore, TODAY) == date(2026, 9, 29)   # next Tuesday
    assert house_facts.next_occurrence(chore, date(2026, 9, 29)) == date(2026, 9, 29)
    assert house_facts.next_occurrence({**chore, "start_date": date(2026, 11, 3)}, TODAY) == date(2026, 11, 3)


def test_chore_items(conn):
    sam, alex = person(conn, "Sam"), person(conn, "Alex")
    trash = house.add_chore(conn, "trash night", 4, date(2026, 9, 21), [sam, alex], NOW)
    dishes = house.add_chore(conn, "dishes", 3, date(2026, 9, 1), [], NOW)        # Thursday
    mail = house.add_chore(conn, "mail", 5, date(2026, 9, 1), [alex], NOW)        # Saturday
    items = {i["name"]: i for i in house_facts.gather(conn, TODAY)["chores"]}
    assert items["trash night"] == {"id": trash, "name": "trash night", "who": "Sam", "when": "today",
                                    "days_over": 0, "day": "2026-09-25"}
    assert items["dishes"]["when"] == "overdue" and items["dishes"]["days_over"] == 1 and items["dishes"]["who"] is None
    assert items["mail"]["when"] == "tomorrow" and items["mail"]["who"] == "Alex"
    assert [i["when"] for i in house_facts.gather(conn, TODAY)["chores"]] == ["overdue", "today", "tomorrow"]
    house.mark_chore_done(conn, dishes, date(2026, 9, 24), sam, NOW)
    assert "dishes" not in {i["name"] for i in house_facts.gather(conn, TODAY)["chores"]}


def test_a_revoked_housemate_is_dropped_from_turns(conn):
    sam, alex = person(conn, "Sam"), person(conn, "Alex")
    house.add_chore(conn, "trash night", 4, date(2026, 9, 21), [sam, alex], NOW)
    house.revoke_member(conn, sam, NOW)
    (item,) = house_facts.gather(conn, TODAY)["chores"]
    assert item["who"] == "Alex"


@pytest.mark.parametrize("today,state,days", [
    (date(2026, 10, 21), None, None), (date(2026, 10, 22), "soon", 3), (date(2026, 10, 24), "soon", 1),
    (date(2026, 10, 25), "due", 0), (date(2026, 10, 27), "overdue", 2)])
def test_every_n_days_upkeep(conn, today, state, days):
    house.add_upkeep(conn, "Furnace filter", "every", NOW, TODAY, every_days=30)  # created 2026-09-25: due Oct 25
    items = house_facts.gather(conn, today)["upkeep"]
    assert [(i["state"], i["days"]) for i in items] == ([] if state is None else [(state, days)])


def test_yearly_upkeep_cases(conn):
    created = datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
    sep15 = house.add_upkeep(conn, "Gutters", "yearly", created, date(2026, 9, 1), month=9, day=15)
    items = house_facts.gather(conn, date(2026, 10, 14))["upkeep"]
    assert [(i["label"], i["state"], i["days"]) for i in items] == [("Gutters", "overdue", 29)]
    house.mark_upkeep_done(conn, sep15, date(2026, 10, 14))
    assert house_facts.gather(conn, date(2026, 10, 14))["upkeep"] == []
    assert house_facts.upkeep_due_on(house.list_upkeep(conn)[0], date(2026, 10, 14)) == date(2027, 9, 15)


def test_yearly_upkeep_created_after_this_years_date_waits_for_next_year(conn):
    house.add_upkeep(conn, "Gutters", "yearly", NOW, TODAY, month=9, day=15)  # created Sep 25
    assert house_facts.upkeep_due_on(house.list_upkeep(conn)[0], date(2026, 10, 14)) == date(2027, 9, 15)


def test_yearly_upkeep_across_the_new_year(conn):
    created = datetime(2026, 12, 1, 13, 0, tzinfo=timezone.utc)
    house.add_upkeep(conn, "Detectors", "yearly", created, date(2026, 12, 1), month=1, day=2)
    item = house.list_upkeep(conn)[0]
    assert house_facts.upkeep_due_on(item, date(2027, 1, 1)) == date(2027, 1, 2)
    assert [(i["state"], i["days"]) for i in house_facts.gather(conn, date(2027, 1, 1))["upkeep"]] == [("soon", 1)]
    late = datetime(2025, 12, 1, 13, 0, tzinfo=timezone.utc)
    house.add_upkeep(conn, "Lights", "yearly", late, date(2025, 12, 1), month=12, day=20)
    assert house_facts.upkeep_due_on(house.list_upkeep(conn)[1], date(2027, 1, 5)) == date(2026, 12, 20)


def test_upkeep_is_ordered_overdue_then_due_then_soon(conn):
    for label, days in (("Due", 30), ("Soon", 33), ("Late-ish", 28), ("Very late", 10)):
        house.add_upkeep(conn, label, "every", NOW, TODAY, every_days=days)
    today = TODAY + timedelta(days=30)   # created Sep 25: Very late was due Oct 5, Late-ish Oct 23, Due Oct 25, Soon Oct 28
    assert [(i["label"], i["state"], i["days"]) for i in house_facts.gather(conn, today)["upkeep"]] == [
        ("Late-ish", "overdue", 2), ("Very late", "overdue", 20), ("Due", "due", 0), ("Soon", "soon", 3)]


def test_event_items(conn):
    sam = person(conn)
    near = house.add_event(conn, "Game night", TODAY + timedelta(days=4), sam, ["chairs", "snacks"], NOW, TODAY)
    edge = house.add_event(conn, "Brunch", TODAY + timedelta(days=7), None, [], NOW, TODAY)
    house.add_event(conn, "Too far", TODAY + timedelta(days=8), None, [], NOW, TODAY)
    tonight = house.add_event(conn, "Movie", TODAY, None, ["popcorn"], NOW, TODAY)
    house.tick_prep(conn, house.list_events(conn, TODAY)[0]["prep"][0]["id"], None, NOW)  # popcorn done
    items = house_facts.gather(conn, TODAY)["events"]
    assert [(i["id"], i["days"], i["host"], i["prep_open"]) for i in items] == [
        (tonight, 0, None, []), (near, 4, "Sam", ["chairs", "snacks"]), (edge, 7, None, [])]
    assert items[1]["day"] == "2026-09-29" and items[1]["title"] == "Game night"


def test_todo_items(conn):
    sam = person(conn)
    overdue = house.add_todo(conn, "Pay deposit", None, date(2026, 9, 20), NOW)
    today = house.add_todo(conn, "Call landlord", None, TODAY, NOW)
    new = house.add_todo(conn, "Buy bulbs", sam, None, NOW)
    quiet = house.add_todo(conn, "Paint hallway", None, date(2026, 10, 30), NOW)
    conn.execute("UPDATE house_todos SET announced_on = '2026-09-20' WHERE id = ?", (quiet,))
    house.claim_todo(conn, new, sam)
    items = house_facts.gather(conn, TODAY)["todos"]
    assert [(i["id"], i["state"], i["who"]) for i in items] == [(overdue, "overdue", None), (today, "today", None),
                                                                (new, "new", "Sam")]
    conn.execute("UPDATE house_todos SET announced_on = '2026-09-25' WHERE id = ?", (new,))  # same-day regenerate
    assert new in [i["id"] for i in house_facts.gather(conn, TODAY)["todos"]]
    conn.execute("UPDATE house_todos SET announced_on = '2026-09-24' WHERE id = ?", (new,))  # announced yesterday
    assert new not in [i["id"] for i in house_facts.gather(conn, TODAY)["todos"]]


def test_a_todo_claimed_by_a_revoked_housemate_still_shows_their_name(conn):
    sam = person(conn)
    todo = house.add_todo(conn, "Buy bulbs", None, None, NOW)
    house.claim_todo(conn, todo, sam)
    house.revoke_member(conn, sam, NOW)
    assert house_facts.gather(conn, TODAY)["todos"][0]["who"] == "Sam"


def test_todos_and_notes_are_capped_at_eight(conn):
    for n in range(12):
        house.add_todo(conn, f"todo {n}", None, None, NOW)
        house.add_note(conn, f"note {n}", None, NOW)
    facts = house_facts.gather(conn, TODAY)
    assert len(facts["todos"]) == 8 and len(facts["notes"]) == 8
    assert [n["text"] for n in facts["notes"]][:2] == ["note 0", "note 1"]


def test_note_items(conn):
    sam = person(conn)
    one = house.add_note(conn, "We're out of paper towels", sam, NOW)
    two = house.add_note(conn, "Owner note", None, NOW)
    old = house.add_note(conn, "Old news", None, NOW)
    conn.execute("UPDATE house_notes SET announced_on = '2026-09-20' WHERE id = ?", (old,))
    assert house_facts.gather(conn, TODAY)["notes"] == [
        {"id": one, "who": "Sam", "text": "We're out of paper towels"}, {"id": two, "who": None, "text": "Owner note"}]


def add_memo(conn, member_id, status, played_on=None, duration=12.0):
    return conn.execute("INSERT INTO house_memos (member_id, duration_s, status, created_at, played_on) "
                        "VALUES (?, ?, ?, 'x', ?)", (member_id, duration, status, played_on)).lastrowid


def test_memo_items_are_uncapped_and_include_today_s_plays(conn):
    sam = person(conn)
    ids = [add_memo(conn, sam, "approved") for _ in range(12)]
    add_memo(conn, sam, "pending")
    add_memo(conn, sam, "rejected")
    add_memo(conn, sam, "played", "2026-09-24")
    played_today = add_memo(conn, sam, "played", "2026-09-25")
    memos = house_facts.gather(conn, TODAY)["memos"]
    assert [m["id"] for m in memos] == ids + [played_today]
    assert memos[0] == {"id": ids[0], "who": "Sam", "duration_s": 12.0}


def test_is_empty(conn):
    assert house_facts.is_empty(house_facts.gather(conn, TODAY)) is True
    house.add_note(conn, "hi", None, NOW)
    assert house_facts.is_empty(house_facts.gather(conn, TODAY)) is False


@pytest.mark.parametrize("hour,minute,has_episode,expected", [
    (6, 59, False, date(2026, 9, 25)), (7, 0, False, date(2026, 9, 26)),
    (6, 0, True, date(2026, 9, 26)), (23, 30, False, date(2026, 9, 26))])
def test_next_day(hour, minute, has_episode, expected):
    from morning_brief.config import TZ
    now = datetime(2026, 9, 25, hour, minute, tzinfo=TZ)
    assert house_facts.next_day(now, time(7, 0), has_episode) == expected


def test_next_occurrence_survives_a_start_date_at_the_end_of_the_calendar():
    chore = {"weekday": 1, "start_date": date(9999, 12, 31), "rotation": []}
    assert house_facts.next_occurrence(chore, TODAY) == TODAY + timedelta(days=7)
