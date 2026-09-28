from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from morning_brief import deepdives, inbox_routes
from morning_brief.app import create_app
from tests.helpers import NOW, deep_dive_script

ITOKEN = "i" * 40
AUTH = {"Authorization": f"Bearer {ITOKEN}"}
LINK = "https://www.example.com/story"


@pytest.fixture
def api(settings):
    app = create_app(replace(settings, inbox_token=ITOKEN), clock=lambda: NOW, start_run=lambda t: True,
                     start_render=lambda t: None, start_scheduler=False)
    with TestClient(app) as c:
        yield c


def conn_of(client):
    return client.app.state.conn


def send(client, headers=AUTH, **body):
    return client.post("/api/inbox", json=body, headers=headers)


def test_off_without_token(settings):
    app = create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
    with TestClient(app) as c:
        r = send(c, input=LINK)
        assert r.status_code == 503 and "INBOX_TOKEN" in r.json()["message"]


def test_auth(api):
    assert send(api, headers={}, input=LINK).status_code == 401
    r = send(api, headers={"Authorization": "Bearer nope"}, input=LINK)
    assert r.status_code == 401 and r.json()["message"] == "Wrong token."
    assert send(api, headers={**AUTH, "Tailscale-Funnel-Request": "?1"}, input=LINK).status_code == 404
    assert deepdives.list_topics(conn_of(api)) == []


def test_link_is_queued_with_page_defaults(api):
    conn = conn_of(api)
    deepdives.set_default(conn, "fact_check", True)
    r = send(api, input=f"  {LINK}  ", note="focus on the money")
    assert r.status_code == 201
    body = r.json()
    assert body["duplicate"] is False
    assert body["message"] == "Queued, 1st in line: From link: example.com/story"
    topic = deepdives.get_topic(conn, body["id"])
    assert topic["source_url"] == LINK and topic["notes"] == "focus on the money"
    assert topic["fact_check"] == 1 and topic["two_hosts"] == 0


def test_headline_and_link_together_keep_the_link(api):
    conn = conn_of(api)
    combined = f"Headline here\n{LINK}"
    r = send(api, input=combined)
    assert r.status_code == 201
    body = r.json()
    assert body["message"] == "Queued, 1st in line: Headline here"
    topic = deepdives.get_topic(conn, body["id"])
    assert topic["source_url"] == LINK
    r2 = send(api, input=combined)
    assert r2.status_code == 200 and r2.json()["duplicate"] is True


def test_text_becomes_a_topic_and_ordinal_counts_queue(api):
    conn = conn_of(api)
    deepdives.add_topic(conn, "Alpha", "", NOW)
    deepdives.add_topic(conn, "Beta", "", NOW)
    r = send(api, input="How the Fed began\nmore words here")
    assert r.status_code == 201 and r.json()["message"] == "Queued, 3rd in line: How the Fed began"


def test_ordinal_is_robust_if_topic_leaves_the_queue_before_counting(api, monkeypatch):
    """A concurrent worker claim, between add_topic and the ordinal count, must not blow up the request."""
    conn = conn_of(api)
    real_get_topic = deepdives.get_topic

    def sneaky(conn_, topic_id):
        row = real_get_topic(conn_, topic_id)
        deepdives.claim(conn_, NOW)  # simulates another thread claiming this exact topic in between
        return row

    monkeypatch.setattr(inbox_routes.deepdives, "get_topic", sneaky)
    r = send(api, input="Sneaky topic")
    assert r.status_code == 201
    assert r.json()["message"] == "Queued, 1st in line: Sneaky topic"


def test_top_moves_to_front(api):
    conn = conn_of(api)
    a = deepdives.add_topic(conn, "Alpha", "", NOW)
    r = send(api, input="Urgent topic", position="top")
    assert r.status_code == 201 and r.json()["message"] == "Queued at the top: Urgent topic"
    assert [row["id"] for row in deepdives.list_topics(conn)] == [r.json()["id"], a]


def test_duplicate_link_is_not_added_even_with_top(api):
    conn = conn_of(api)
    first = send(api, input=LINK).json()
    deepdives.add_topic(conn, "Alpha", "", NOW)
    r = send(api, input=LINK, position="top")
    assert r.status_code == 200
    assert r.json() == {"message": "Already in your queue: From link: example.com/story", "id": first["id"],
                        "duplicate": True}
    assert len(deepdives.list_topics(conn)) == 2
    assert deepdives.list_topics(conn)[0]["id"] == first["id"]  # order unchanged


def test_heard_link_can_be_queued_again(api):
    conn = conn_of(api)
    first = send(api, input=LINK).json()["id"]
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, first, deep_dive_script(), NOW)
    deepdives.publish(conn, first, title="T", word_count=2400, duration_s=1.0, audio_bytes=1, now=NOW)
    deepdives.mark_heard(conn, first, NOW)
    r = send(api, input=LINK)
    assert r.status_code == 201 and r.json()["duplicate"] is False and r.json()["id"] != first


@pytest.mark.parametrize("body,message", [
    ({"input": "   "}, "Nothing to add: share a link or some text."),
    ({"input": 5}, 'Send JSON with an "input" string.'),
    ({}, 'Send JSON with an "input" string.'),
    ({"input": "Topic", "note": 7}, 'Send JSON with an "input" string.'),
    ({"input": "Topic", "note": "n" * 501}, "The note is too long (500 characters at most)."),
    ({"input": "Topic", "position": "middle"}, 'position must be "top" or "end".'),
])
def test_bad_bodies(api, body, message):
    r = send(api, **body)
    assert r.status_code == 422 and r.json()["message"] == message
    assert deepdives.list_topics(conn_of(api)) == []


def test_non_json_and_non_object_bodies(api):
    r = api.post("/api/inbox", content=b"not json", headers=AUTH)
    assert r.status_code == 422 and r.json()["message"] == 'Send JSON with an "input" string.'
    r = api.post("/api/inbox", json=["a"], headers=AUTH)
    assert r.status_code == 422


def test_deeply_nested_json_is_rejected_not_crashed(api):
    r = api.post("/api/inbox", content=b"[" * 100000, headers=AUTH)
    assert r.status_code == 422 and r.json()["message"] == 'Send JSON with an "input" string.'


def test_topic_error_is_reported(api):
    r = send(api, input="bad\x07topic")
    assert r.status_code == 422 and r.json()["message"] == "topic contains control characters"


def test_hostless_link_is_treated_as_text(api):
    r = send(api, input="https://")
    assert r.status_code == 201 and r.json()["message"].endswith(": https://")


def test_inbox_needs_no_login(settings):
    app = create_app(replace(settings, inbox_token=ITOKEN, ui_password="pw"), clock=lambda: NOW,
                     start_run=lambda t: True, start_render=lambda t: None, start_scheduler=False)
    with TestClient(app) as c:
        assert send(c, input="Topic").status_code == 201
