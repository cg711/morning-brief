import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from morning_brief import db, deepdives
from morning_brief.app import create_app
from tests.helpers import NOW

WTOKEN = "w" * 40
AUTH = {"Authorization": f"Bearer {WTOKEN}"}
FUNNEL = {"Tailscale-Funnel-Request": "?1"}
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def pushes():
    return []


@pytest.fixture
def api(settings, pushes):
    s = replace(settings, worker_token=WTOKEN)
    app = create_app(s, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False,
                     notifier=lambda st, title, message, tags: pushes.append((title, message, tags)))
    with TestClient(app) as c:
        yield c


def post(api, body, headers=AUTH):
    data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return api.post("/api/writer/report", content=data, headers={**headers, **JSON})


def dump(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT * FROM app_state WHERE key NOT IN ('worker_last_seen', 'writer_last_poll') ORDER BY key")], \
        [tuple(r) for r in conn.execute("SELECT * FROM topics ORDER BY id")]


def test_work_reports_what_a_claim_would_hand_out(api):
    conn = api.app.state.conn
    first = api.get("/api/work", headers=AUTH)
    assert first.status_code == 200 and first.json() == {"daily": False, "deep_dives": 0}
    deepdives.add_topic(conn, "A", "", NOW)
    deepdives.add_topic(conn, "B", "", NOW)
    assert api.get("/api/work", headers=AUTH).json() == {"daily": False, "deep_dives": 2}


def test_work_changes_nothing_but_last_seen(api):
    conn = api.app.state.conn
    deepdives.add_topic(conn, "A", "", NOW)
    before = dump(conn)
    assert api.get("/api/work", headers=AUTH).status_code == 200
    assert dump(conn) == before
    assert db.get_state(conn, "worker_last_seen") == NOW.isoformat()
    assert db.get_state(conn, "writer_last_poll") == NOW.isoformat()


def test_only_the_work_route_stamps_the_writers_poll(api):
    conn = api.app.state.conn
    post(api, {"kind": "idle", "outcome": "running"})
    assert db.get_state(conn, "writer_last_poll") is None and db.get_state(conn, "worker_last_seen") is not None
    api.get("/api/work", headers=AUTH)
    assert db.get_state(conn, "writer_last_poll") == NOW.isoformat()
    assert "Writer: checked in" in api.get("/partials/worker-status").text


def test_wrong_or_missing_worker_token_is_401_and_funnel_is_404(api, settings):
    good = {"kind": "idle", "outcome": "running"}
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer "}):
        assert api.get("/api/work", headers=headers).status_code == 401
        assert post(api, good, headers).status_code == 401
    assert api.get("/api/work", headers={**AUTH, **FUNNEL}).status_code == 404
    assert post(api, good, {**AUTH, **FUNNEL}).status_code == 404
    assert db.get_state(api.app.state.conn, "writer_last_report") is None
    off = create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
    with TestClient(off) as disabled:
        assert disabled.get("/api/work", headers=AUTH).status_code == 503
        assert disabled.post("/api/writer/report", json=good, headers=AUTH).status_code == 503


def test_valid_report_is_204_and_stored(api):
    response = post(api, {"kind": "deep_dive", "outcome": "ok", "started_at": "2026-09-25T12:50:00+00:00",
                          "detail": "done"})
    assert response.status_code == 204 and response.content == b""
    stored = json.loads(db.get_state(api.app.state.conn, "writer_last_report"))
    assert stored["outcome"] == "ok" and stored["detail"] == "done"


@pytest.mark.parametrize("body", [
    b"not json", b"", b"[1, 2]", b'"text"', b"null", b"{}", b'{"kind": "daily"}',
    b'{"kind": "x", "outcome": "ok"}', b'{"kind": "daily", "outcome": "x"}',
    b'{"kind": "daily", "outcome": "ok", "started_at": "soon"}',
    b'{"kind": "daily", "outcome": "ok", "started_at": 5}',
    b'{"kind": "daily", "outcome": "ok", "detail": 5}',
    b'{"kind": ["daily"], "outcome": "ok"}',
    b"\xff\xfe\x00",
])
def test_report_validation_table(api, body):
    before = dump(api.app.state.conn)
    response = post(api, body)
    assert response.status_code == 422
    problems = response.json()["problems"]
    assert isinstance(problems, list) and problems
    assert dump(api.app.state.conn) == before
    assert db.get_state(api.app.state.conn, "writer_last_report") is None


def test_huge_report_is_413_and_stores_nothing(api):
    huge = json.dumps({"kind": "daily", "outcome": "ok", "detail": "x" * (5 * 1024 * 1024)})
    assert post(api, huge).status_code == 413
    assert db.get_state(api.app.state.conn, "writer_last_report") is None
    # just under the cap still works
    small = json.dumps({"kind": "idle", "outcome": "running", "detail": "y" * 300})
    assert len(small) < 4096 and post(api, small).status_code == 204
    # over the cap with a lying or missing content-length
    chunked = api.post("/api/writer/report", content=iter([b'{"kind":"idle","outcome":"running","detail":"',
                                                           b"z" * 5000, b'"}']), headers={**AUTH, **JSON})
    assert chunked.status_code == 413


def test_control_characters_stripped_and_detail_cut(api):
    body = {"kind": "idle", "outcome": "running", "detail": "a\x00b\x1bc" + "d" * 500}
    assert post(api, body).status_code == 204
    detail = json.loads(db.get_state(api.app.state.conn, "writer_last_report"))["detail"]
    assert detail.startswith("abcd") and len(detail) == 200 and "\x00" not in detail and "\x1b" not in detail


def test_failing_report_releases_and_pushes_through_the_app_notifier(api, pushes):
    conn = api.app.state.conn
    topic_id = deepdives.add_topic(conn, "Alpha", "", NOW - timedelta(minutes=5))
    deepdives.claim(conn, NOW)
    conn.execute("UPDATE topics SET claim_attempts = 2 WHERE id = ?", (topic_id,))
    body = {"kind": "deep_dive", "outcome": "failed", "started_at": (NOW - timedelta(minutes=1)).isoformat()}
    assert post(api, body).status_code == 204
    assert deepdives.get_topic(conn, topic_id)["status"] == "failed"
    assert [p[0] for p in pushes] == ["A deep dive failed after 3 tries: Alpha"]


def test_hostile_detail_is_escaped_in_the_page(api):
    hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    for _ in range(3):
        assert post(api, {"kind": "idle", "outcome": "failed", "detail": hostile}).status_code == 204
    for page in (api.get("/").text, api.get("/partials/worker-status").text, api.get("/partials/deep-dives").text):
        assert "<script>alert(1)" not in page and "<img src=x" not in page
    partial = api.get("/partials/worker-status").text
    assert "&lt;script&gt;" in partial and "Writer: failing" in partial
