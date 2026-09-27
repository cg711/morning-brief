from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from morning_brief import daily_worker
from morning_brief.app import create_app
from morning_brief.feeds import item_id
from tests.helpers import NOW
from tests.test_daily_worker import COUNCIL, STORM, gathered, script_for

WTOKEN = "w" * 40
AUTH = {"Authorization": f"Bearer {WTOKEN}"}


@pytest.fixture
def published():
    return []


@pytest.fixture
def api(settings, published):
    s = replace(settings, worker_token=WTOKEN, daily_mode="worker")
    app = create_app(s, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False,
                     start_daily_publish=published.append)
    with TestClient(app) as c:
        yield c


def test_auth(api, settings):
    assert api.post("/api/daily/claim").status_code == 401
    app = create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
    with TestClient(app) as disabled:
        assert disabled.post("/api/daily/claim", headers=AUTH).status_code == 503


def test_claim_items_script_flow(api, settings, published):
    conn = api.app.state.conn
    assert api.post("/api/daily/claim", headers=AUTH).status_code == 204
    date = gathered(conn, api.app.state.settings)
    claimed = api.post("/api/daily/claim", headers=AUTH)
    assert claimed.headers["content-type"].startswith("application/json") and "\n" in claimed.text
    body = claimed.json()
    assert body["date"] == date and {c["url"] for c in body["candidates"]} == {STORM, COUNCIL}
    text = api.get(f"/api/daily/{date}/items/{item_id(COUNCIL)}", headers=AUTH)
    assert text.status_code == 200 and text.headers["content-type"].startswith("text/plain")
    assert api.get(f"/api/daily/{date}/items/{item_id(STORM)}", headers=AUTH).status_code == 404
    assert api.get(f"/api/daily/{date}/items/{item_id(COUNCIL)}").status_code == 401
    bad = api.post(f"/api/daily/{date}/script", json=script_for(["nope"]), headers=AUTH)
    assert bad.status_code == 422 and any("unknown item ids" in p for p in bad.json()["problems"])
    junk = api.post(f"/api/daily/{date}/script", content=b"{", headers={**AUTH, "Content-Type": "application/json"})
    assert junk.status_code == 422
    ok = api.post(f"/api/daily/{date}/script", json=script_for([item_id(COUNCIL)]), headers=AUTH)
    assert ok.status_code == 202 and published == [date]
    assert daily_worker.get_job(conn, date)["status"] == "speaking"
    again = api.post(f"/api/daily/{date}/script", json=script_for([item_id(COUNCIL)]), headers=AUTH)
    assert again.status_code == 404


def test_fail_route(api):
    conn = api.app.state.conn
    date = gathered(conn, api.app.state.settings)
    assert api.post(f"/api/daily/{date}/fail", json={"reason": "x"}, headers=AUTH).status_code == 404  # not claimed
    api.post("/api/daily/claim", headers=AUTH)
    assert api.post(f"/api/daily/{date}/fail", json={"reason": "all paywalled"}, headers=AUTH).status_code == 204
    assert daily_worker.get_job(conn, date)["error"] == "all paywalled"


def test_claim_stamps_worker_check_in(api):
    from morning_brief import db
    api.post("/api/daily/claim", headers=AUTH)
    assert db.get_state(api.app.state.conn, "worker_last_seen") == NOW.isoformat()


def test_script_route_allows_personal_only_with_personal_data(api, monkeypatch):
    from morning_brief import personal as personal_mod
    from tests.test_daily_worker import http as feeds_http, personal_seg
    conn = api.app.state.conn
    date = gathered(conn, api.app.state.settings)
    api.post("/api/daily/claim", headers=AUTH)
    news = script_for([item_id(COUNCIL)])
    body = {**news, "segments": [personal_seg(), *news["segments"]]}
    bad = api.post(f"/api/daily/{date}/script", json=body, headers=AUTH)
    assert bad.status_code == 422 and any("no personal data" in p for p in bad.json()["problems"])
    monkeypatch.setattr(personal_mod, "gather_personal", lambda s, http, now: {"tip": {"title": "t", "detail": "d"}})
    api.post(f"/api/daily/{date}/fail", json={"reason": "retry"}, headers=AUTH)
    date = daily_worker.gather(api.app.state.settings, conn, feeds_http(), NOW, "manual")  # re-gather with personal data
    api.post("/api/daily/claim", headers=AUTH)
    assert api.post(f"/api/daily/{date}/script", json=body, headers=AUTH).status_code == 202


def test_restart_leaves_waiting_run_running_but_fails_speaking_job(settings):
    from morning_brief import db
    s = replace(settings, worker_token=WTOKEN, daily_mode="worker")
    conn = db.connect(s.db_path)
    db.migrate(conn)
    date = gathered(conn, s)
    speaking_date = "2026-09-24"
    speaking_run_id = db.start_run(conn, date=speaking_date, trigger="schedule", model=daily_worker.MODEL,
                                   started_at=NOW.isoformat())
    conn.execute(
        "INSERT INTO daily_jobs (date, run_id, status, candidates_json, bodies_json, previous_json, "
        "script_json, cutoff_at, created_at, updated_at) "
        "VALUES (?, ?, 'speaking', '[]', '{}', '[]', '{}', ?, ?, ?)",
        (speaking_date, speaking_run_id, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    conn.close()
    app = create_app(s, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
    with TestClient(app):
        c = app.state.conn
        assert daily_worker.get_job(c, date)["status"] == "waiting"
        assert db.latest_run_for(c, date)["status"] == "running"
        speaking_job = daily_worker.get_job(c, speaking_date)
        assert speaking_job["status"] == "failed" and "interrupted during speech" in speaking_job["error"]
        assert db.latest_run_for(c, speaking_date)["status"] == "failed"
