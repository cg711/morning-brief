from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from morning_brief import personal
from tests.helpers import NOW  # 2026-09-25 08:00 Central
from tests.test_personal_weather import FORECAST, GEOCODE, forecast, routed

AGENDA = "oura.test/api/agenda"


def ev(title, start, end, calendar="Me", all_day=False):
    return {"title": title, "start": start, "end": end, "all_day": all_day, "calendar": calendar}


def agenda(events, connected=True):
    return {AGENDA: httpx.Response(200, json={"date": "2026-09-25", "connected": connected, "events": events})}


def test_calendar_facts_trims_and_formats():
    events = [
        ev("Columbus Day", "2026-09-25", "2026-09-26", "Holidays", all_day=True),
        ev("Early gym", "2026-09-25T06:00:00-05:00", "2026-09-25T07:30:00-05:00"),  # already over at 8:00
        ev("Standup", "2026-09-25T09:30:00-05:00", "2026-09-25T09:45:00-05:00"),
        ev("Walk", "2026-09-25T15:00:00-05:00", "2026-09-25T15:30:00-05:00", "Oura"),
        ev("Lunch\x07 with Sam", "2026-09-25T12:00:00-05:00", "2026-09-25T13:00:00-05:00"),
        ev("x" * 120, "2026-09-25T16:00:00-05:00", "2026-09-25T17:00:00-05:00"),
        ev("Call", "2026-09-25T17:00:00-05:00", "2026-09-25T17:30:00-05:00"),
        ev("Dinner", "2026-09-25T19:00:00-05:00", "2026-09-25T20:00:00-05:00"),
        ev("Still going", "2026-09-25T07:00:00-05:00", "2026-09-25T09:00:00-05:00"),  # started, not over
    ]
    http, calls = routed(agenda(events))
    facts = personal.calendar_facts(http, "http://oura.test/", NOW)
    assert calls[0].url.params["date"] == "2026-09-25"
    assert [(e["start"], e["title"]) for e in facts["events"]] == [
        ("7:00 AM", "Still going"), ("9:30 AM", "Standup"), ("12:00 PM", "Lunch with Sam"),
        ("3:00 PM", "Walk"), ("4:00 PM", facts["events"][4]["title"])]
    assert len(facts["events"][4]["title"]) <= 80 and facts["events"][4]["title"].endswith("…")
    assert facts["events"][3]["calendar"] == "Oura"
    assert facts["more"] == 2 and facts["all_day"] == ["Columbus Day"]


def test_all_day_capped_at_three():
    events = [ev(f"Day {n}", "2026-09-25", "2026-09-26", all_day=True) for n in range(5)]
    http, _ = routed(agenda(events))
    assert personal.calendar_facts(http, "http://oura.test", NOW) == \
        {"events": [], "all_day": ["Day 0", "Day 1", "Day 2"], "more": 0}


def test_empty_day():
    http, _ = routed(agenda([]))
    assert personal.calendar_facts(http, "http://oura.test", NOW) == {"events": [], "all_day": [], "more": 0}


@pytest.mark.parametrize("routes", [
    agenda([], connected=False),
    {},                                                   # 404: an older dashboard
    {AGENDA: httpx.Response(500)},
    {AGENDA: httpx.Response(200, json=["not", "a", "dict"])},
    {AGENDA: httpx.Response(200, content=b"not json")},
])
def test_unavailable_gives_none(routes):
    http, _ = routed(routes)
    assert personal.calendar_facts(http, "http://oura.test", NOW) is None


def everything_routes():
    state = {"today": {"day": "2026-09-25", "sleep_score": 80.0, "readiness_score": 82.0, "total_sleep_h": 7.5,
                       "readiness_contributors": {"hrv_balance": 85}}, "insights": []}
    return {
        "oura.test/api/state": httpx.Response(200, json=state),
        **agenda([ev("Standup", "2026-09-25T09:30:00-05:00", "2026-09-25T09:45:00-05:00")]),
        GEOCODE: httpx.Response(200, json={"results": [{"latitude": 44.98, "longitude": -93.26}]}),
        FORECAST: httpx.Response(200, json=forecast()),
    }


def configured(settings):
    return replace(settings, personal_segment=True, oura_dashboard_url="http://oura.test",
                   actual_server_url="https://actual.test", actual_password="pw-secret", actual_sync_id="sync-1")


def test_gather_personal_includes_weather_and_calendar(settings):
    http, _ = routed(everything_routes())
    facts = personal.gather_personal(configured(settings), http, NOW,
                                     actual_fetch=lambda st, d: [personal.Txn(Decimal("-5"), "Cafe")])
    assert set(facts) == {"weather", "calendar", "sleep", "spending"}
    assert facts["weather"]["high"] == 58 and facts["calendar"]["events"][0]["title"] == "Standup"


def test_gather_personal_weather_only(settings):
    http, _ = routed({GEOCODE: everything_routes()[GEOCODE], FORECAST: everything_routes()[FORECAST]})
    s = replace(settings, personal_segment=True)  # no Oura, no Actual
    assert set(personal.gather_personal(s, http, NOW)) == {"weather"}


def test_gather_personal_all_failing_is_none(settings):
    http, _ = routed({})
    assert personal.gather_personal(replace(settings, personal_segment=True, oura_dashboard_url="http://oura.test"),
                                    http, NOW) is None


def test_check_lines_have_no_titles(settings):
    http, _ = routed(everything_routes())
    lines = []
    personal.check(configured(settings), http, NOW,
                   actual_fetch=lambda st, d: [personal.Txn(Decimal("-77.77"), "Secret Shop")], out=lines.append)
    assert lines == ["Oura: ok, last night's sleep is in", "Calendar: ok (1 events left today)",
                     "Weather: ok (Minneapolis)", "Actual: ok (1 purchases yesterday)"]
    assert "Standup" not in "\n".join(lines)
