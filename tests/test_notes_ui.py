from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from morning_brief import notes
from morning_brief.app import create_app
from tests.helpers import NOW

HX = {"HX-Request": "true"}
TODAY = date(2026, 9, 25)


def make_client(settings, **overrides):
    app = create_app(replace(settings, **overrides), clock=lambda: NOW, start_run=lambda t: True,
                     start_render=lambda t: None, start_scheduler=False)
    return TestClient(app)


@pytest.fixture
def ui(settings):
    with make_client(settings, daily_mode="worker", personal_segment=True) as c:
        yield c


def conn_of(client):
    return client.app.state.conn


def test_card_only_when_personal_segment_is_on(ui, settings):
    assert 'id="notes-card"' in ui.get("/").text
    with make_client(settings) as off:
        assert 'id="notes-card"' not in off.get("/").text


def test_notes_and_countdowns_posts_404_when_off(settings):
    with make_client(settings) as off:
        assert off.post("/notes", data={"text": "Call the dentist"}, headers=HX).status_code == 404
        assert off.post("/countdowns", data={"label": "Iceland", "date": "2026-10-05"}, headers=HX).status_code == 404


def test_add_and_delete_note(ui):
    assert ui.post("/notes", data={"text": "Call the dentist"}).status_code == 403
    html = ui.post("/notes", data={"text": "Call the dentist", "url": "", "date": ""}, headers=HX).text
    (row,) = notes.pending_notes(conn_of(ui))
    assert row["for_date"] == "2026-09-26"  # NOW (08:00) is not before RUN_AT (08:00): tomorrow
    assert "Call the dentist" in html and "tomorrow" in html
    html = ui.delete(f"/notes/{row['id']}", headers=HX).text
    assert notes.pending_notes(conn_of(ui)) == [] and "No notes waiting" in html


def test_note_with_link_and_date(ui):
    html = ui.post("/notes", data={"text": "", "url": "https://www.example.com/a", "date": "2026-10-05"},
                   headers=HX).text
    (row,) = notes.pending_notes(conn_of(ui))
    assert (row["url"], row["for_date"]) == ("https://www.example.com/a", "2026-10-05")
    assert "example.com" in html and "Oct 5" in html


def test_note_errors_render_inline(ui):
    html = ui.post("/notes", data={"text": "", "url": "", "date": ""}, headers=HX).text
    assert "write a note or share a link" in html
    html = ui.post("/notes", data={"text": "x", "date": "2026-09-01"}, headers=HX).text
    assert "pick today or a later day" in html
    html = ui.post("/notes", data={"text": "x", "date": "soon"}, headers=HX).text
    assert "pick a date" in html
    assert notes.pending_notes(conn_of(ui)) == []


def test_add_and_delete_countdown(ui):
    html = ui.post("/countdowns", data={"label": "Iceland", "date": "2026-10-05"}, headers=HX).text
    assert "Iceland" in html and "Oct 5" in html and "in 10 days" in html
    (row,) = notes.list_countdowns(conn_of(ui), TODAY)
    assert "pick a date" in ui.post("/countdowns", data={"label": "X", "date": ""}, headers=HX).text
    assert "give the countdown a label" in ui.post("/countdowns", data={"label": "", "date": "2026-10-05"},
                                                   headers=HX).text
    html = ui.delete(f"/countdowns/{row['id']}", headers=HX).text
    assert "Iceland" not in html
    assert ui.delete(f"/countdowns/{row['id']}").status_code == 403


def test_countdown_wording(ui):
    conn = conn_of(ui)
    notes.add_countdown(conn, "Today thing", TODAY, NOW)
    notes.add_countdown(conn, "Tomorrow thing", TODAY + timedelta(days=1), NOW)
    html = ui.get("/").text
    assert "Today thing" in html and "· today" in html and "· tomorrow" in html


def test_note_text_is_escaped(ui):
    html = ui.post("/notes", data={"text": "<script>alert(1)</script>"}, headers=HX).text
    assert "&lt;script&gt;" in html and "<script>alert(1)" not in html
