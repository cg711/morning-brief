import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from morning_brief import db, deepdives
from morning_brief.app import create_app
from tests.helpers import NOW, deep_dive_script, two_host_script

WTOKEN = "w" * 40
FEED = "t" * 40
AUTH = {"Authorization": f"Bearer {WTOKEN}"}


@pytest.fixture
def rendered():
    return []


@pytest.fixture
def api(settings, rendered):
    s = replace(settings, worker_token=WTOKEN)
    app = create_app(s, clock=lambda: NOW, start_run=lambda t: True, start_render=rendered.append,
                     start_scheduler=False)
    with TestClient(app) as c:
        yield c


def queue(client, *topics):
    return [deepdives.add_topic(client.app.state.conn, t, "", NOW) for t in topics]


def test_auth(api, settings):
    assert api.post("/api/deep-dives/claim").status_code == 401
    assert api.post("/api/deep-dives/claim", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.post("/api/deep-dives/claim", headers={**AUTH, "Tailscale-Funnel-Request": "?1"}).status_code == 404
    app = create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
    with TestClient(app) as disabled:
        assert disabled.post("/api/deep-dives/claim", headers=AUTH).status_code == 503


def test_claim_survives_set_state_failure(api, monkeypatch):
    def exploding(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "set_state", exploding)
    assert api.post("/api/deep-dives/claim", headers=AUTH).status_code == 204


def test_claim_flow(api):
    assert api.post("/api/deep-dives/claim", headers=AUTH).status_code == 204
    ids = queue(api, "A", "B", "C", "D")
    got = [api.post("/api/deep-dives/claim", headers=AUTH).json() for _ in range(3)]
    assert [g["id"] for g in got] == ids[:3] and got[0] == {"id": ids[0], "topic": "A", "notes": "",
                                                            "url": None, "fact_check": False, "two_hosts": False}
    assert api.post("/api/deep-dives/claim", headers=AUTH).status_code == 204


def test_claim_returns_link_and_flags(api):
    conn = api.app.state.conn
    t = deepdives.add_topic(conn, "", "", NOW, url="https://example.com/story", fact_check=True, two_hosts=True)
    got = api.post("/api/deep-dives/claim", headers=AUTH).json()
    assert got == {"id": t, "topic": "From link: example.com/story", "notes": "",
                   "url": "https://example.com/story", "fact_check": True, "two_hosts": True}


def test_submit_script(api, rendered):
    (topic_id,) = queue(api, "A")
    assert api.post(f"/api/deep-dives/{topic_id}/script", json=deep_dive_script(), headers=AUTH).status_code == 404
    api.post("/api/deep-dives/claim", headers=AUTH)
    bad = api.post(f"/api/deep-dives/{topic_id}/script", json=deep_dive_script(1500), headers=AUTH)
    assert bad.status_code == 422 and any("1500 words" in p for p in bad.json()["problems"])
    junk = api.post(f"/api/deep-dives/{topic_id}/script", content=b"not json",
                    headers={**AUTH, "Content-Type": "application/json"})
    assert junk.status_code == 422
    ok = api.post(f"/api/deep-dives/{topic_id}/script", json=deep_dive_script(), headers=AUTH)
    assert ok.status_code == 202 and rendered == [topic_id]
    assert deepdives.get_topic(api.app.state.conn, topic_id)["status"] == "speaking"
    again = api.post(f"/api/deep-dives/{topic_id}/script", json=deep_dive_script(), headers=AUTH)
    assert again.status_code == 404


def test_submit_after_delete_returns_404(api):
    (topic_id,) = queue(api, "A")
    api.post("/api/deep-dives/claim", headers=AUTH)
    deepdives.delete_topic(api.app.state.conn, api.app.state.settings.deep_dives_dir, topic_id)
    r = api.post(f"/api/deep-dives/{topic_id}/script", json=deep_dive_script(), headers=AUTH)
    assert r.status_code == 404


def test_worker_fail(api):
    (topic_id,) = queue(api, "A")
    api.post("/api/deep-dives/claim", headers=AUTH)
    r = api.post(f"/api/deep-dives/{topic_id}/fail", json={"reason": "too vague"}, headers=AUTH)
    assert r.status_code == 204
    row = deepdives.get_topic(api.app.state.conn, topic_id)
    assert (row["status"], row["error"]) == ("failed", "too vague")
    assert api.post(f"/api/deep-dives/{topic_id}/fail", json={}, headers=AUTH).status_code == 404


def test_worker_api_skips_hx_guard(api):
    # no HX-Request header anywhere above, yet 200/202/204 were returned
    queue(api, "A")
    assert api.post("/api/deep-dives/claim", headers=AUTH).status_code == 200


def publish_one(client, settings):
    conn = client.app.state.conn
    (topic_id,) = queue(client, "A")
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, topic_id, deep_dive_script(), NOW)
    settings.deep_dives_dir.mkdir(parents=True, exist_ok=True)
    deepdives.audio_path(settings.deep_dives_dir, topic_id).write_bytes(b"\xff\xf3\x84\xc4")
    deepdives.publish(conn, topic_id, title="How the Fed Began", word_count=2400, duration_s=1200.0,
                      audio_bytes=4, now=NOW)
    return topic_id


def test_feed_and_audio(api, settings):
    topic_id = publish_one(api, settings)
    assert api.get("/feed/deep-dives/wrong.xml").status_code == 404
    r = api.get(f"/feed/deep-dives/{FEED}.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml")
    assert f"deep-dive-{topic_id}" in r.text and "Federal Reserve History" in r.text
    audio = api.get(f"/audio/{FEED}/deep-dives/{topic_id}.mp3", headers={"Range": "bytes=0-1"})
    assert audio.status_code == 206 and audio.content == b"\xff\xf3"
    assert api.get(f"/audio/{FEED}/deep-dives/999.mp3").status_code == 404
    assert api.get("/feed/deep-dives-cover.png").content[:4] == b"\x89PNG"
    funnel = {"Tailscale-Funnel-Request": "?1"}
    assert api.get(f"/feed/deep-dives/{FEED}.xml", headers=funnel).status_code == 200


def test_worker_calls_record_check_in(api):
    from morning_brief import db
    conn = api.app.state.conn
    api.post("/api/deep-dives/claim", headers={"Authorization": "Bearer nope"})
    assert db.get_state(conn, "worker_last_seen") is None
    assert api.post("/api/deep-dives/claim", headers=AUTH).status_code == 204  # idle claim still counts
    assert db.get_state(conn, "worker_last_seen") == NOW.isoformat()


def test_submit_validates_against_the_topics_flags(api, rendered):
    conn = api.app.state.conn
    t = deepdives.add_topic(conn, "Fed", "", NOW, two_hosts=True, fact_check=True)
    api.post("/api/deep-dives/claim", headers=AUTH)
    single = api.post(f"/api/deep-dives/{t}/script", json=deep_dive_script(), headers=AUTH)
    assert single.status_code == 422
    problems = single.json()["problems"]
    assert any("must use 'lines'" in p for p in problems) and any("'fact_check'" in p for p in problems)
    ok = api.post(f"/api/deep-dives/{t}/script",
                  json={**two_host_script(), "fact_check": {"claims_checked": 9, "corrected": 1, "removed": 0}},
                  headers=AUTH)
    assert ok.status_code == 202 and rendered == [t]


def test_submit_for_unknown_topic_is_404(api):
    assert api.post("/api/deep-dives/999/script", json=deep_dive_script(), headers=AUTH).status_code == 404
