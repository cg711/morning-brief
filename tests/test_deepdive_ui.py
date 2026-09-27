from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from morning_brief import db, deepdives
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


def test_worker_status_line_and_partial(ui):
    conn = conn_of(ui)
    html = ui.get("/").text
    assert 'id="worker-status"' in html and "checked in yet" in html  # the apostrophe is HTML-escaped
    assert 'hx-get="/partials/worker-status"' in html and 'hx-trigger="every 60s"' in html
    db.set_state(conn, "worker_last_seen", (NOW - timedelta(minutes=14)).isoformat())
    part = ui.get("/partials/worker-status").text
    assert "14 min ago" in part and "caution" not in part
    db.set_state(conn, "worker_last_seen", (NOW - timedelta(hours=3)).isoformat())
    assert "caution" in ui.get("/partials/worker-status").text


def test_worker_status_hidden_when_worker_disabled(settings):
    app = create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
    with TestClient(app) as c:
        assert 'id="worker-status"' not in c.get("/").text
        part = c.get("/partials/worker-status")
        assert part.status_code == 200 and part.text == ""


def test_running_long_label_and_sig(ui):
    conn = conn_of(ui)
    deepdives.add_topic(conn, "Alpha", "", NOW - timedelta(minutes=30))
    deepdives.claim(conn, NOW - timedelta(minutes=30))
    html = ui.get("/partials/deep-dives").text
    assert "running long" not in html
    sig = html.split("?sig=")[1].split('"')[0]
    conn.execute("UPDATE topics SET claimed_at = ?, updated_at = ?",
                 ((NOW - timedelta(minutes=61)).isoformat(),) * 2)
    later = ui.get(f"/partials/deep-dives?sig={sig}")
    assert later.status_code == 200 and '<span class="caution">running long</span>' in later.text


def publish_with(conn, topic, chapters, heard=False):
    t = deepdives.add_topic(conn, topic, "", NOW)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, t, deep_dive_script(sections=3), NOW)
    deepdives.publish(conn, t, title=f"{topic} episode", word_count=2400, duration_s=4000.0, audio_bytes=1,
                      now=NOW, chapters=chapters)
    if heard:
        deepdives.mark_heard(conn, t, NOW)
    return t


def test_seek_buttons_from_chapters(ui):
    conn = conn_of(ui)
    publish_with(conn, "Alpha", [("Introduction", 0.0), ("Part 1", 12.0), ("Part 2", 754.5),
                                 ("Part 3", 3723.0), ("Wrap-up", 3900.0)])
    html = ui.get("/").text
    assert 'data-seek="12.0"' in html and "▶ 0:12" in html
    assert 'data-seek="754.5"' in html and "▶ 12:34" in html
    assert "▶ 1:02:03" in html
    assert '<script src="/static/app.js" defer></script>' in html
    assert ui.get("/static/app.js").status_code == 200


def test_no_seek_buttons_without_chapters(ui):
    publish_with(conn_of(ui), "Alpha", None)
    assert "data-seek" not in ui.get("/").text


def test_heard_cards_show_transcript_with_seek(ui):
    publish_with(conn_of(ui), "Beta", [("Introduction", 0.0), ("Part 1", 30.0), ("Part 2", 60.0),
                                       ("Part 3", 90.0), ("Wrap-up", 120.0)], heard=True)
    html = ui.get("/").text
    heard = html.split('<details class="heard" id="heard-list">')[1]
    assert "Transcript &amp; sources" in heard and 'data-seek="30.0"' in heard


def test_players_and_transcripts_get_stable_ids_for_swap_preservation(ui):
    conn = conn_of(ui)
    ready = publish_with(conn, "Alpha", None)
    heard = publish_with(conn, "Beta", None, heard=True)
    html = ui.get("/").text
    assert f'id="player-{ready}" hx-preserve' in html
    assert f'id="player-{heard}" hx-preserve' in html
    assert f'id="transcript-{ready}"' in html
    assert f'id="transcript-{heard}"' in html
    assert 'id="heard-list"' in html


def test_app_js_preserves_open_details_across_swaps(ui):
    js = ui.get("/static/app.js").text
    assert "htmx:beforeSwap" in js and "htmx:afterSettle" in js


def test_go_deeper_button_queues_follow_up(ui):
    conn = conn_of(ui)
    parent = publish_with(conn, "Alpha", None)
    html = ui.get("/").text
    assert f'hx-post="/deep-dives/{parent}/deeper/0"' in html
    assert ui.post(f"/deep-dives/{parent}/deeper/0").status_code == 403  # htmx-only
    after = ui.post(f"/deep-dives/{parent}/deeper/0", headers=HX).text
    assert "Follow-up queued" in after and f'hx-post="/deep-dives/{parent}/deeper/0"' not in after
    assert f'hx-post="/deep-dives/{parent}/deeper/1"' in after
    assert "Follow-up to Alpha episode" in after
    queued = [r for r in deepdives.list_topics(conn) if r["status"] == "queued"]
    assert queued[0]["topic"] == "Part 1" and queued[0]["parent_topic_id"] == parent


def test_go_deeper_on_heard_episode(ui):
    conn = conn_of(ui)
    parent = publish_with(conn, "Beta", None, heard=True)
    ui.post(f"/deep-dives/{parent}/deeper/2", headers=HX)
    assert [r["topic"] for r in deepdives.list_topics(conn) if r["status"] == "queued"] == ["Part 3"]
