"""What the house has to say on a given day: pure date rules plus the reads that gather them into facts."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from . import house
from .config import TZ

MENTION_BEFORE, MENTION_AFTER = 1, 2
EVENT_WINDOW_DAYS = 7
SOON_DAYS = 3
MAX_TODOS = MAX_NOTES = 8
_CHORE_ORDER = {"overdue": 0, "today": 1, "tomorrow": 2}
_UPKEEP_ORDER = {"overdue": 0, "due": 1, "soon": 2}


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def assignee(chore: dict, day: date) -> int | None:
    """Whose turn it is for the occurrence on `day`: the rotation advances one place each week."""
    rotation = chore["rotation"]
    if not rotation:
        return None
    week = (monday_of(day) - monday_of(chore["start_date"])).days // 7
    return rotation[week % len(rotation)]


def occurrence_in_window(chore: dict, today: date) -> date | None:
    """The occurrence worth mentioning today: tomorrow's, today's, or one of the last two days'."""
    for offset in range(MENTION_BEFORE, -MENTION_AFTER - 1, -1):
        day = today + timedelta(days=offset)
        if day.weekday() == chore["weekday"] and day >= chore["start_date"]:
            return day
    return None


def next_occurrence(chore: dict, today: date) -> date:
    first = max(today, chore["start_date"])
    try:
        return first + timedelta(days=(chore["weekday"] - first.weekday()) % 7)
    except OverflowError:  # a start date at the very end of the calendar; never let it break a page
        return today + timedelta(days=7)


def upkeep_due_on(item: dict, today: date) -> date:
    """When an upkeep item is next due (or has been due since, if it was missed)."""
    last, created = item["last_done"], item["created"]
    if item["kind"] == "every":
        return (last or created) + timedelta(days=item["every_days"])
    month, day = item["month"], item["day"]
    for year in (today.year, today.year - 1):
        occurrence = date(year, month, day)
        if occurrence <= today:
            if occurrence >= created and (last is None or last < occurrence):
                return occurrence
            break
    following = date(today.year, month, day)
    return following if following > today else date(today.year + 1, month, day)


def chore_items(conn, today: date, names: dict) -> list[dict]:
    items = []
    for chore in house.list_chores(conn):
        day = occurrence_in_window(chore, today)
        if day is None or house.chore_is_done(conn, chore["id"], day):
            continue
        offset = (day - today).days
        who = assignee(chore, day)
        items.append({"id": chore["id"], "name": chore["name"], "who": names.get(who) if who else None,
                      "when": "tomorrow" if offset == 1 else "today" if offset == 0 else "overdue",
                      "days_over": max(0, -offset), "day": day.isoformat()})
    return sorted(items, key=lambda i: _CHORE_ORDER[i["when"]])


def upkeep_items(conn, today: date) -> list[dict]:
    items = []
    for item in house.list_upkeep(conn):
        days = (upkeep_due_on(item, today) - today).days
        state = "overdue" if days < 0 else "due" if days == 0 else "soon" if days <= SOON_DAYS else None
        if state:
            items.append({"id": item["id"], "label": item["label"], "state": state, "days": abs(days)})
    return sorted(items, key=lambda i: _UPKEEP_ORDER[i["state"]])


def event_items(conn, today: date, names: dict) -> list[dict]:
    items = []
    for event in house.list_events(conn, today):
        days = (event["day"] - today).days
        if days > EVENT_WINDOW_DAYS:
            continue
        items.append({"id": event["id"], "title": event["title"], "days": days, "day": event["day"].isoformat(),
                      "host": names.get(event["host_id"]) if event["host_id"] else None,
                      "prep_open": [p["text"] for p in event["prep"] if not p["done"]]})
    return items


def todo_items(conn, today: date, names: dict) -> list[dict]:
    iso = today.isoformat()
    items = []
    for todo in house.list_todos(conn):
        due = date.fromisoformat(todo["due"]) if todo["due"] else None
        new = todo["announced_on"] in (None, iso)
        if not (new or (due is not None and due <= today)):
            continue
        state = "overdue" if due and due < today else "today" if due == today else "new"
        items.append({"id": todo["id"], "text": todo["text"],
                      "who": names.get(todo["claimed_by"]) if todo["claimed_by"] else None, "state": state})
    return items[:MAX_TODOS]


def note_items(conn, today: date, names: dict) -> list[dict]:
    rows = conn.execute("SELECT id, member_id, text FROM house_notes WHERE announced_on IS NULL OR announced_on = ? "
                        "ORDER BY id LIMIT ?", (today.isoformat(), MAX_NOTES)).fetchall()
    return [{"id": r["id"], "who": names.get(r["member_id"]) if r["member_id"] else None, "text": r["text"]}
            for r in rows]


def memo_items(conn, today: date, names: dict) -> list[dict]:
    rows = conn.execute("SELECT id, member_id, duration_s FROM house_memos "
                        "WHERE (status = 'approved' AND played_on IS NULL) OR (status = 'played' AND played_on = ?) "
                        "ORDER BY id", (today.isoformat(),)).fetchall()
    return [{"id": r["id"], "who": names.get(r["member_id"], "a housemate"), "duration_s": r["duration_s"]}
            for r in rows]


def gather(conn, today: date) -> dict:
    names = house.member_names(conn)
    return {"chores": chore_items(conn, today, names), "upkeep": upkeep_items(conn, today),
            "events": event_items(conn, today, names), "todos": todo_items(conn, today, names),
            "notes": note_items(conn, today, names), "memos": memo_items(conn, today, names)}


def is_empty(facts: dict) -> bool:
    return not any(facts.values())


def next_day(now: datetime, at: time, has_episode: bool) -> date:
    """The day of the next House brief: today's if it hasn't been made and it's before `at`, otherwise tomorrow's."""
    local = now.astimezone(TZ)
    return local.date() if local.time() < at and not has_episode else local.date() + timedelta(days=1)
