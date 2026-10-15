from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from morning_brief import events
from morning_brief.config import PROJECT_DIR

DAY = date(2026, 10, 15)  # a Thursday
TWO = ("sources:\n  - name: First Avenue\n    url: https://first-avenue.com/shows/\n"
       "  - name: Hennepin Arts\n    url: https://www.hennepinarts.org/events/\n")


def enabled(settings, tmp_path, text=TWO, **kw):
    path = tmp_path / "events.yaml"
    if text is not None:
        path.write_text(text)
    return replace(settings, events=True, events_path=path, **kw)


def pick(title="David Sedaris", day="2026-10-17", time="19:30", venue="State Theatre", source_id="e1"):
    return {"title": title, "date": day, "time": time, "venue": venue, "source_id": source_id}


def test_load_sources_gives_each_an_id(tmp_path):
    (tmp_path / "e.yaml").write_text(TWO)
    sources = events.load_sources(tmp_path / "e.yaml")
    assert [(s.id, s.name, s.url) for s in sources] == [
        ("e1", "First Avenue", "https://first-avenue.com/shows/"),
        ("e2", "Hennepin Arts", "https://www.hennepinarts.org/events/")]


def test_the_seed_file_loads():
    sources = events.load_sources(PROJECT_DIR / "events.yaml")
    assert len(sources) >= 5 and len({s.name for s in sources}) == len(sources)
    assert all(s.url.startswith("https://") for s in sources)


def test_the_image_ships_the_sources_file():
    assert "COPY events.yaml" in (PROJECT_DIR / "Dockerfile").read_text()


def test_title_key_ignores_case_punctuation_and_spacing():
    assert events.title_key("Dune: Part Two!") == events.title_key("  dune  part two ")
    assert events.title_key("Ｄune") == events.title_key("dune")  # full-width letter folds to the plain one
    assert events.title_key("A") != events.title_key("B")


def test_claim_block_is_none_unless_enabled(conn, settings, tmp_path):
    assert events.claim_block(replace(settings, events_path=tmp_path / "x.yaml"), conn, DAY) is None


def test_claim_block_shape(conn, settings, tmp_path):
    block = events.claim_block(enabled(settings, tmp_path, events_max=4), conn, DAY)
    assert block == {"window": {"from": "2026-10-15", "to": "2026-10-22"}, "max": 4,
                     "sources": [{"id": "e1", "name": "First Avenue", "url": "https://first-avenue.com/shows/"},
                                 {"id": "e2", "name": "Hennepin Arts", "url": "https://www.hennepinarts.org/events/"}],
                     "mentioned": []}


@pytest.mark.parametrize("text", [None, "", "sources:\n", "sources: []\n", "sources: nope\n", ": : :\n\t-",
                                  "sources:\n  - name: No url\n", "sources:\n  - 5\n"])
def test_a_missing_or_broken_sources_file_means_no_block_and_no_error(conn, settings, tmp_path, text):
    assert events.claim_block(enabled(settings, tmp_path, text=text), conn, DAY) is None


def test_a_broken_mentions_table_means_no_block_and_no_error(conn, settings, tmp_path):
    conn.execute("DROP TABLE event_mentions")
    assert events.claim_block(enabled(settings, tmp_path), conn, DAY) is None


def test_record_is_idempotent_and_refreshes_the_date_mentioned(conn):
    events.record(conn, [pick()], date(2026, 10, 10))
    events.record(conn, [pick(title="DAVID SEDARIS!")], date(2026, 10, 12))
    rows = conn.execute("SELECT title_key, title, date, mentioned_on FROM event_mentions").fetchall()
    assert [tuple(r) for r in rows] == [("david sedaris", "DAVID SEDARIS!", "2026-10-17", "2026-10-12")]


def test_mentioned_lists_recent_mentions_of_events_not_yet_past(conn, settings, tmp_path):
    events.record(conn, [pick(title="Recent", day="2026-10-17")], date(2026, 10, 10))      # 5 days ago: listed
    events.record(conn, [pick(title="Long ago", day="2026-10-18")], date(2026, 9, 20))     # 25 days ago: not
    events.record(conn, [pick(title="Past", day="2026-10-14")], date(2026, 10, 10))        # already happened: not
    events.record(conn, [pick(title="Today", day="2026-10-15")], date(2026, 10, 14))       # today: listed
    events.record(conn, [pick(title="Same day", day="2026-10-16")], DAY)                   # this very day: not
    block = events.claim_block(enabled(settings, tmp_path), conn, DAY)
    assert block["mentioned"] == [{"title": "Today", "date": "2026-10-15"}, {"title": "Recent", "date": "2026-10-17"}]


def test_a_mention_from_the_same_day_is_not_listed(conn, settings, tmp_path):
    events.record(conn, [pick(title="First version", day="2026-10-17")], DAY)
    assert events.claim_block(enabled(settings, tmp_path), conn, DAY)["mentioned"] == []
    assert events.claim_block(enabled(settings, tmp_path), conn, DAY + timedelta(days=1))["mentioned"] == [
        {"title": "First version", "date": "2026-10-17"}]


def test_prune_removes_rows_for_events_more_than_thirty_days_old(conn):
    events.record(conn, [pick(title="Old", day="2026-09-01"), pick(title="Edge", day="2026-09-15"),
                         pick(title="New", day="2026-10-17")], date(2026, 9, 1))
    assert events.prune(conn, date(2026, 10, 15)) == 1   # only 2026-09-01 is more than 30 days before Oct 15
    assert [r["title"] for r in conn.execute("SELECT title FROM event_mentions ORDER BY date")] == ["Edge", "New"]
    assert events.prune(conn, date(2026, 10, 15)) == 0


BLOCK = {"window": {"from": "2026-10-15", "to": "2026-10-22"}, "max": 3,
         "sources": [{"id": "e1", "name": "First Avenue", "url": "u1"},
                     {"id": "e2", "name": "Hennepin Arts", "url": "u2"},
                     {"id": "e3", "name": "Walker Art Center", "url": "u3"}],
         "mentioned": [{"title": "Old Show", "date": "2026-10-17"}, {"title": "Tomorrow Show", "date": "2026-10-16"},
                       {"title": "Tonight Show", "date": "2026-10-15"}]}


def test_nothing_to_check():
    assert events.validate(None, BLOCK) == [] and events.validate([], BLOCK) == []
    assert events.validate(None, None) == [] and events.validate([], None) == []


def test_picks_without_a_claim_block_are_refused():
    assert any("no events block" in p for p in events.validate([pick()], None))


def test_good_picks_pass():
    assert events.validate([pick()], BLOCK) == []
    assert events.validate([pick(time=None), {"title": "Bare", "date": "2026-10-22", "source_id": "e2"}], BLOCK) == []
    assert events.validate([pick(day="2026-10-15"), pick(title="Edge", day="2026-10-22")], BLOCK) == []   # window edges
    assert events.validate([pick(title="Tomorrow Show", day="2026-10-16"),
                            pick(title="Tonight Show", day="2026-10-15")], BLOCK) == []                   # reminders
    assert events.validate([pick(title="<script>alert(1)</script> & <b>x</b>")], BLOCK) == []             # data, not markup


@pytest.mark.parametrize("items,needle", [
    ({"a": 1}, "must be a list"), ("a show", "must be a list"), (5, "must be a list"), (True, "must be a list"),
    ([pick()] * 4, "at most 3"),
    (["x"], "must be an object"), ([None], "must be an object"), ([[]], "must be an object"), ([5], "must be an object"),
    ([{**pick(), "extra": 1}], "unknown fields"),
    ([pick(title="")], "title"), ([pick(title="   ")], "title"), ([pick(title="x" * 121)], "title"),
    ([pick(title="bell\x07")], "title"), ([pick(title=5)], "title"), ([pick(title={"a": 1})], "title"),
    ([{k: v for k, v in pick().items() if k != "title"}], "title"),
    ([pick(venue="")], "venue"), ([pick(venue="x" * 81)], "venue"), ([pick(venue="nul\x00")], "venue"),
    ([pick(venue=5)], "venue"),
    ([pick(day="10/17/2026")], "'date'"), ([pick(day="2026-13-01")], "'date'"), ([pick(day=None)], "'date'"),
    ([pick(day=20261017)], "'date'"), ([pick(day="2026-10-17 19:30")], "'date'"),
    ([pick(day="2026-10-14")], "outside"), ([pick(day="2026-10-23")], "outside"),
    ([pick(time="7:30pm")], "'time'"), ([pick(time="24:00")], "'time'"), ([pick(time="19:60")], "'time'"),
    ([pick(time=1930)], "'time'"), ([pick(time="")], "'time'"),
    ([pick(source_id="e9")], "source_id"), ([pick(source_id=None)], "source_id"), ([pick(source_id=["e1"])], "source_id"),
    ([pick(), pick(title="  DAVID   sedaris!! ")], "twice"),
    ([pick(title="Old Show!")], "already mentioned"),
    ([pick(title="!!!")], "needs letters or numbers"), ([pick(title="🎃")], "needs letters or numbers"),
    ([pick(title="  ?  ")], "needs letters or numbers"),
    ([pick(venue="?!")], "needs letters or numbers"), ([pick(venue="...")], "needs letters or numbers"),
    ([pick(venue="  -  ")], "needs letters or numbers"), ([pick(venue="🎃")], "needs letters or numbers"),
    ([pick(time="19:30\n")], "'time'"), ([pick(time="1\u0669:30")], "'time'"),
    ([pick(day="2026-10-1\u0667")], "'date'"), ([pick(day="2026-10-17\n")], "'date'"),
])
def test_bad_picks_are_refused(items, needle):
    problems = events.validate(items, BLOCK)
    assert problems and any(needle in p for p in problems), problems


def test_a_pick_that_repeats_on_another_date_is_fine():
    assert events.validate([pick(), pick(day="2026-10-18")], BLOCK) == []


def test_every_problem_is_reported_at_once():
    problems = events.validate([pick(title="", day="2026-12-01", time="99:99", source_id="zz")], BLOCK)
    assert len(problems) == 4


def test_clean_keeps_exactly_the_known_keys_in_date_order():
    raw = [pick(title="  Late   show ", day="2026-10-18", time="21:00"),
           {"title": "No time", "date": "2026-10-17", "source_id": "e2"},
           pick(title="Early", day="2026-10-17", time="10:00", venue=" The   Hall "),
           pick(title="Later", day="2026-10-17", time="18:00", venue=None)]
    assert events.clean(raw) == [
        {"title": "Early", "date": "2026-10-17", "time": "10:00", "venue": "The Hall", "source_id": "e1"},
        {"title": "Later", "date": "2026-10-17", "time": "18:00", "venue": None, "source_id": "e1"},
        {"title": "No time", "date": "2026-10-17", "time": None, "venue": None, "source_id": "e2"},
        {"title": "Late show", "date": "2026-10-18", "time": "21:00", "venue": "State Theatre", "source_id": "e1"}]


@pytest.mark.parametrize("value,words", [
    ("00:00", "midnight"), ("12:00", "noon"), ("00:30", "twelve thirty at night"),
    ("04:59", "four fifty nine at night"), ("05:00", "five in the morning"),
    ("09:05", "nine oh five in the morning"), ("11:59", "eleven fifty nine in the morning"),
    ("12:01", "twelve oh one in the afternoon"), ("16:45", "four forty five in the afternoon"),
    ("17:00", "five in the evening"), ("19:30", "seven thirty in the evening"),
    ("20:59", "eight fifty nine in the evening"), ("21:00", "nine at night"), ("23:15", "eleven fifteen at night")])
def test_time_words(value, words):
    assert events.time_words(value) == words


def test_compose_reads_the_facts_in_order():
    items = events.clean([
        pick(title="Open Mic", day="2026-10-15", time="20:00", venue="Bryant-Lake Bowl", source_id="e1"),
        {"title": "Gallery Walk", "date": "2026-10-16", "source_id": "e3"},
        pick(title="David Sedaris", day="2026-10-17", time="19:30", venue="State Theatre", source_id="e2"),
        pick(title="Farmers Market!", day="2026-10-22", time="09:05", venue="Mill City", source_id="e1")])
    assert events.compose(items, BLOCK, date(2026, 10, 15)) == (
        "Around town this week. Today at eight in the evening, Open Mic, at Bryant-Lake Bowl. "
        "Tomorrow, Gallery Walk. Saturday at seven thirty in the evening, David Sedaris, at State Theatre. "
        "Next Thursday at nine oh five in the morning, Farmers Market, at Mill City. "
        "Listings from First Avenue, Walker Art Center, and Hennepin Arts.")


def test_compose_names_one_or_two_sources_plainly():
    one = events.clean([pick()])
    assert events.compose(one, BLOCK, date(2026, 10, 15)).endswith("Listings from First Avenue.")
    two = events.clean([pick(), pick(title="B", source_id="e2")])
    assert events.compose(two, BLOCK, date(2026, 10, 15)).endswith("Listings from First Avenue and Hennepin Arts.")


def test_compose_reads_hostile_text_as_written():
    items = events.clean([pick(title="<b>Hi</b> & bye", venue="Café “X”")])
    text = events.compose(items, BLOCK, date(2026, 10, 15))
    assert "<b>Hi</b> & bye, at Café “X”." in text


def test_segment_shape():
    seg = events.segment(events.clean([pick()]), BLOCK, date(2026, 10, 15))
    assert seg == {"segment": "events", "headline": "Around town", "item_ids": [],
                   "text": events.compose(events.clean([pick()]), BLOCK, date(2026, 10, 15))}


def test_load_sources_skips_entries_that_are_not_usable(tmp_path):
    (tmp_path / "e.yaml").write_text(
        "sources:\n"
        "  - name: Blank Url\n    url: '  '\n"
        "  - name: null\n    url: https://nullname.example/\n"
        "  - name: Plain Http\n    url: http://plain.example/\n"
        "  - just a string\n"
        "  - name: Missing Url\n"
        "  - name: 5\n    url: https://number.example/\n"
        "  - name: Good One\n    url: ' https://good.example/events '\n")
    sources = events.load_sources(tmp_path / "e.yaml")
    assert [(s.id, s.name, s.url) for s in sources] == [("e1", "Good One", "https://good.example/events")]


def test_load_sources_numbers_only_the_kept_entries(tmp_path):
    (tmp_path / "e.yaml").write_text(
        "sources:\n  - name: A\n    url: https://a.example/\n  - name: Bad\n    url: ftp://b.example/\n"
        "  - name: C\n    url: https://c.example/\n")
    assert [(s.id, s.name) for s in events.load_sources(tmp_path / "e.yaml")] == [("e1", "A"), ("e2", "C")]

