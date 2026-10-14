from datetime import date

import numpy as np

from morning_brief import house_script, speech

TODAY = date(2026, 9, 25)  # a Friday


def facts(**kw):
    base = {"chores": [], "upkeep": [], "events": [], "todos": [], "notes": [], "memos": []}
    base.update(kw)
    return base


def sections(script):
    return {s["heading"]: s for s in script["sections"]}


def test_numbers_are_written_as_words_up_to_twenty():
    assert [house_script.num(n) for n in (0, 1, 4, 20, 21)] == ["zero", "one", "four", "twenty", "21"]


def test_an_empty_day_has_no_sections():
    script = house_script.compose(facts(), TODAY)
    assert script["sections"] == []
    assert script["title"] == "House brief: Friday, September 25"
    assert script["intro"] == "Good morning, house. Here's what's on for Friday."
    assert script["outro"] == "That's the house for today."


def test_today_section():
    script = house_script.compose(facts(
        chores=[{"id": 1, "name": "trash night", "who": "Alex", "when": "today", "days_over": 0, "day": "2026-09-25"},
                {"id": 2, "name": "dishes", "who": None, "when": "overdue", "days_over": 2, "day": "2026-09-23"},
                {"id": 3, "name": "mail", "who": "Jo", "when": "overdue", "days_over": 1, "day": "2026-09-24"}],
        upkeep=[{"id": 1, "label": "furnace filter", "state": "due", "days": 0},
                {"id": 2, "label": "gutters", "state": "overdue", "days": 3}],
        events=[{"id": 1, "title": "Game night", "days": 0, "day": "2026-09-25", "host": "Sam",
                 "prep_open": ["chairs", "snacks"]}]), TODAY)
    assert sections(script)["Today"]["text"] == (
        "Alex has trash night today. Still open from two days ago: dishes. "
        "Still open from yesterday: mail, Jo's turn. Due today: furnace filter. "
        "Overdue by three days: gutters. Tonight: Game night. Sam is hosting. Still to do: chairs, snacks.")


def test_a_long_neglected_upkeep_item_has_no_day_count():
    script = house_script.compose(facts(upkeep=[
        {"id": 1, "label": "chimney sweep", "state": "overdue", "days": 368},
        {"id": 2, "label": "gutters", "state": "overdue", "days": 20},
        {"id": 3, "label": "filter", "state": "overdue", "days": 1}]), TODAY)
    assert sections(script)["Today"]["text"] == (
        "Overdue: chimney sweep. Overdue by twenty days: gutters. Overdue by one day: filter.")


def test_unassigned_chores_today_and_tomorrow():
    script = house_script.compose(facts(chores=[
        {"id": 1, "name": "recycling", "who": None, "when": "today", "days_over": 0, "day": "2026-09-25"},
        {"id": 2, "name": "mail", "who": None, "when": "tomorrow", "days_over": 0, "day": "2026-09-26"}]), TODAY)
    assert sections(script)["Today"]["text"] == "Today: recycling."
    assert sections(script)["Coming up"]["text"] == "Tomorrow: mail."


def test_coming_up_section():
    script = house_script.compose(facts(
        chores=[{"id": 3, "name": "recycling", "who": "Jo", "when": "tomorrow", "days_over": 0, "day": "2026-09-26"}],
        upkeep=[{"id": 2, "label": "smoke detectors", "state": "soon", "days": 2},
                {"id": 3, "label": "hose bibs", "state": "soon", "days": 1}],
        events=[{"id": 2, "title": "Game night", "days": 4, "day": "2026-09-29", "host": None, "prep_open": []},
                {"id": 3, "title": "Brunch", "days": 1, "day": "2026-09-26", "host": "Alex", "prep_open": ["eggs"]}]),
        TODAY)
    assert sections(script)["Coming up"]["text"] == (
        "Jo has recycling tomorrow. Due in two days: smoke detectors. Due tomorrow: hose bibs. "
        "Tuesday, in four days: Game night. Tomorrow: Brunch. Alex is hosting. Still to do: eggs.")
    assert "Today" not in sections(script)


def test_todo_section():
    script = house_script.compose(facts(todos=[
        {"id": 1, "text": "Buy light bulbs", "who": "Sam", "state": "new"},
        {"id": 2, "text": "Call landlord.", "who": None, "state": "overdue"},
        {"id": 3, "text": "Pay deposit", "who": "Jo", "state": "today"}]), TODAY)
    assert sections(script)["To-dos"]["text"] == (
        "There are three open to-dos. Buy light bulbs, Sam has it. "
        "Overdue: Call landlord, still unclaimed. Due today: Pay deposit, Jo has it.")
    one = house_script.compose(facts(todos=[{"id": 1, "text": "Sweep", "who": None, "state": "new"}]), TODAY)
    assert sections(one)["To-dos"]["text"] == "There is one open to-do. Sweep, still unclaimed."


def test_note_section_reads_housemate_text_as_written():
    script = house_script.compose(facts(notes=[
        {"id": 1, "who": "Sam", "text": "we're out of paper towels"},
        {"id": 2, "who": None, "text": "Trash goes out early this week!"},
        {"id": 3, "who": "Jo", "text": "<b>hi</b> & bye"}]), TODAY)
    assert sections(script)["Notes"]["text"] == (
        "Sam says: we're out of paper towels. A note: Trash goes out early this week! Jo says: <b>hi</b> & bye.")


def test_memo_sections_come_last_one_each():
    script = house_script.compose(facts(
        notes=[{"id": 1, "who": "Sam", "text": "hello"}],
        memos=[{"id": 12, "who": "Sam", "duration_s": 20.0}, {"id": 13, "who": "Alex", "duration_s": 5.0}]), TODAY)
    assert [(s["heading"], s["kind"]) for s in script["sections"]] == [
        ("Notes", "text"), ("Voice note from Sam", "memo"), ("Voice note from Alex", "memo")]
    assert script["sections"][1]["memo_id"] == 12 and "text" not in script["sections"][1]


def test_many_memos_make_many_chapters():
    memos = [{"id": n, "who": "Sam", "duration_s": 3.0} for n in range(1, 26)]
    script = house_script.compose(facts(memos=memos), TODAY)
    clips = {n: speech.Clip(np.zeros(10, dtype=np.float32)) for n in range(1, 26)}
    passages, firsts = house_script.passages(script, clips)
    assert len(script["sections"]) == 25 and len(passages) == 1 + 25 * 2 + 1 and len(firsts) == 25
    assert firsts == [1 + 2 * n for n in range(25)]


def test_passages_and_chapters():
    script = house_script.compose(facts(
        chores=[{"id": 1, "name": "trash night", "who": "Alex", "when": "today", "days_over": 0, "day": "2026-09-25"}],
        notes=[{"id": 1, "who": "Sam", "text": "hello"}],
        memos=[{"id": 12, "who": "Sam", "duration_s": 20.0}]), TODAY)
    clip = speech.Clip(np.zeros(100, dtype=np.float32))
    passages, firsts = house_script.passages(script, {12: clip})
    assert passages[0] == script["intro"] and passages[-1] == script["outro"]
    assert passages[1] == "Today. Alex has trash night today."
    assert passages[2] == "Notes. Sam says: hello."
    assert passages[3] == "Voice note from Sam." and passages[4] is clip
    assert firsts == [1, 2, 3]
    starts = [0.0, 1.0, 2.0, 3.0, 4.0, 9.0]
    assert house_script.chapters(script, starts, firsts) == [
        ("Introduction", 0.0), ("Today", 1.0), ("Notes", 2.0), ("Voice note from Sam", 3.0), ("Wrap-up", 9.0)]


def test_word_count_and_description():
    script = house_script.compose(facts(
        notes=[{"id": 1, "who": "Sam", "text": "hello there"}], memos=[{"id": 12, "who": "Sam", "duration_s": 20.0}]),
        TODAY)
    expected = (len(script["intro"].split()) + len(script["sections"][0]["text"].split())
                + len(script["outro"].split()))
    assert house_script.word_count(script) == expected
    text = house_script.describe(script)
    assert "Notes\nSam says: hello there." in text and "Voice note from Sam (recording)" in text
    assert house_script.describe(house_script.compose(facts(), TODAY)) == script["intro"]
