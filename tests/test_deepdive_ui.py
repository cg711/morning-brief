from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from morning_brief import deepdives
from morning_brief.app import create_app
from tests.helpers import NOW, deep_dive_script

HX = {"HX-Request": "true"}


@pytest.fixture
def rendered():
    return []


@pytest.fixture
def ui(settings, rendered):
    s = replace(settings, worker_token="w" * 40)
    app = create_app(s, clock=lambda: NOW, start_run=lambda t: True, start_render=rendered.append,
                     start_scheduler=False)
    with TestClient(app) as c:
        yield c


def conn_of(client):
    return client.app.state.conn


def test_section_on_index_with_feed_url(ui):
    html = ui.get("/").text
    assert 'id="deep-dives"' in html and "Add to queue" in html
    assert f"https://brief.test/feed/deep-dives/{'t' * 40}.xml" in html
    assert "No topics yet" in html


def test_add_topic(ui):
    assert ui.post("/deep-dives", data={"topic": "The Fed"}).status_code == 403
    html = ui.post("/deep-dives", data={"topic": "The Fed", "notes": "2008 crisis"}, headers=HX).text
    assert "The Fed" in html and "2008 crisis" in html
    assert "topic is required" in ui.post("/deep-dives", data={"topic": "  "}, headers=HX).text


def test_queue_actions(ui):
    conn = conn_of(ui)
    a = deepdives.add_topic(conn, "Alpha", "", NOW)
    b = deepdives.add_topic(conn, "Beta", "", NOW)
    ui.post(f"/deep-dives/{b}/top", headers=HX)
    assert [r["id"] for r in deepdives.list_topics(conn)] == [b, a]
    html = ui.delete(f"/deep-dives/{a}", headers=HX).text
    assert "Alpha" not in html and deepdives.get_topic(conn, a) is None
    assert ui.delete(f"/deep-dives/{b}").status_code == 403


def test_in_progress_polls(ui):
    conn = conn_of(ui)
    deepdives.add_topic(conn, "Alpha", "", NOW)
    deepdives.claim(conn, NOW)
    html = ui.get("/partials/deep-dives").text
    assert 'hx-trigger="every 10s"' in html and "Researching on your Mac" in html
    assert "?sig=" in html


def test_poll_returns_204_when_sig_matches_and_200_otherwise(ui):
    conn = conn_of(ui)
    deepdives.add_topic(conn, "Alpha", "", NOW)
    deepdives.claim(conn, NOW)
    html = ui.get("/partials/deep-dives").text
    sig = html.split("?sig=")[1].split('"')[0]
    assert ui.get(f"/partials/deep-dives?sig={sig}").status_code == 204
    assert ui.get("/partials/deep-dives?sig=stale").status_code == 200
    assert ui.get("/partials/deep-dives").status_code == 200
    # a status change invalidates the old signature
    deepdives.accept_script(conn, deepdives.list_topics(conn)[0]["id"], deep_dive_script(), NOW)
    assert ui.get(f"/partials/deep-dives?sig={sig}").status_code == 200


def test_delete_speaking_topic_removes_row_and_sources(ui):
    conn = conn_of(ui)
    a = deepdives.add_topic(conn, "Alpha", "", NOW)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, a, deep_dive_script(), NOW)
    assert deepdives.get_topic(conn, a)["status"] == "speaking"
    assert deepdives.sources_for(conn, a)
    ui.delete(f"/deep-dives/{a}", headers=HX)
    assert deepdives.get_topic(conn, a) is None
    assert deepdives.sources_for(conn, a) == []


def test_delete_ready_topic_removes_row_sources_episode_and_mp3(ui, settings):
    conn = conn_of(ui)
    a = deepdives.add_topic(conn, "Alpha", "", NOW)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, a, deep_dive_script(), NOW)
    settings.deep_dives_dir.mkdir(parents=True, exist_ok=True)
    deepdives.audio_path(settings.deep_dives_dir, a).write_bytes(b"mp3data")
    deepdives.publish(conn, a, title="How the Fed Began", word_count=2400, duration_s=1200.0,
                      audio_bytes=7, now=NOW)
    assert deepdives.get_topic(conn, a)["status"] == "ready"
    ui.delete(f"/deep-dives/{a}", headers=HX)
    assert deepdives.get_topic(conn, a) is None
    assert deepdives.sources_for(conn, a) == []
    assert conn.execute("SELECT * FROM deep_dives WHERE topic_id = ?", (a,)).fetchone() is None
    assert not deepdives.audio_path(settings.deep_dives_dir, a).exists()


def test_topic_with_script_tag_is_escaped(ui):
    html = ui.post("/deep-dives", data={"topic": "<script>alert(1)</script>", "notes": ""}, headers=HX).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_ready_mark_heard_and_retry(ui, settings, rendered):
    conn = conn_of(ui)
    a = deepdives.add_topic(conn, "Alpha", "", NOW)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, a, deep_dive_script(), NOW)
    deepdives.publish(conn, a, title="How the Fed Began", word_count=2400, duration_s=1210.0,
                      audio_bytes=4, now=NOW)
    html = ui.get("/").text
    assert "How the Fed Began" in html and "20:10" in html and "Mark heard" in html
    assert f"/audio/{'t' * 40}/deep-dives/{a}.mp3?v=" in html and "Federal Reserve History" in html
    html = ui.post(f"/deep-dives/{a}/heard", headers=HX).text
    assert "Heard (1)" in html and "Sep 25" in html
    b = deepdives.add_topic(conn, "Beta", "", NOW)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, b, deep_dive_script(), NOW)
    deepdives.fail(conn, b, "espeak exploded", NOW)
    assert "espeak exploded" in ui.get("/partials/deep-dives").text
    ui.post(f"/deep-dives/{b}/retry", headers=HX)
    assert deepdives.get_topic(conn, b)["status"] == "speaking" and rendered == [b]


def test_daily_brief_off(settings):
    app = create_app(replace(settings, daily_brief=False), clock=lambda: NOW, start_run=lambda t: True,
                     start_scheduler=False)
    with TestClient(app) as c:
        assert "Daily brief is off (DAILY_BRIEF=0)." in c.get("/").text
        assert c.post("/generate", headers=HX).status_code == 409
