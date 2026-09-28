from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from morning_brief import deepdives, shares
from morning_brief.app import create_app
from tests.helpers import NOW, deep_dive_script

HX = {"HX-Request": "true"}
BASE = "https://box.example.ts.net"


def make_client(settings, **overrides):
    app = create_app(replace(settings, **overrides), clock=lambda: NOW, start_run=lambda t: True,
                     start_render=lambda t: None, start_scheduler=False)
    return TestClient(app)


@pytest.fixture
def ui(settings):
    with make_client(settings, share_base_url=BASE) as c:
        yield c


def ready(client, name="Alpha"):
    conn = client.app.state.conn
    topic_id = deepdives.add_topic(conn, name, "", NOW)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, topic_id, deep_dive_script(), NOW)
    deepdives.publish(conn, topic_id, title=f"{name} episode", word_count=2400, duration_s=1200.0,
                      audio_bytes=4, now=NOW)
    return topic_id


def test_share_button_only_on_ready_or_heard_when_sharing_is_configured(ui, settings):
    t = ready(ui)
    queued = deepdives.add_topic(ui.app.state.conn, "Queued one", "", NOW)
    html = ui.get("/").text
    assert f'hx-post="/deep-dives/{t}/share"' in html
    assert f'hx-post="/deep-dives/{queued}/share"' not in html
    deepdives.mark_heard(ui.app.state.conn, t, NOW)
    assert f'hx-post="/deep-dives/{t}/share"' in ui.get("/").text
    with make_client(settings) as off:  # same data dir, sharing off
        assert "/share\"" not in off.get("/").text


def test_share_shows_link_and_revoke_then_unshare_removes_it(ui):
    t = ready(ui)
    assert ui.post(f"/deep-dives/{t}/share").status_code == 403
    html = ui.post(f"/deep-dives/{t}/share", headers=HX).text
    token = shares.live_token(ui.app.state.conn, t)
    assert f'value="{BASE}/s/{token}"' in html
    assert "Shared" in html and f'hx-post="/deep-dives/{t}/unshare"' in html
    assert f'hx-post="/deep-dives/{t}/share"' not in html
    assert "This also kills its share link." in html
    assert 'data-copy="share-' in html
    ui.post(f"/deep-dives/{t}/share", headers=HX)
    assert shares.live_token(ui.app.state.conn, t) == token
    assert ui.post(f"/deep-dives/{t}/unshare").status_code == 403
    html = ui.post(f"/deep-dives/{t}/unshare", headers=HX).text
    assert shares.live_token(ui.app.state.conn, t) is None
    assert f"/s/{token}" not in html and f'hx-post="/deep-dives/{t}/share"' in html


def test_share_refused_for_unready_topics_or_when_sharing_is_off(ui, settings):
    with make_client(settings) as off:  # same data dir, sharing off
        t = ready(off, "Beta")  # before queuing another: claim() takes the first queued topic
        assert off.post(f"/deep-dives/{t}/share", headers=HX).status_code == 404
    queued = deepdives.add_topic(ui.app.state.conn, "Queued", "", NOW)
    assert ui.post(f"/deep-dives/{queued}/share", headers=HX).status_code == 404
    assert ui.post("/deep-dives/999/share", headers=HX).status_code == 404


def test_feed_copy_buttons_use_the_shared_copy_handler(ui):
    html = ui.get("/").text
    assert 'data-copy="feed-url"' in html and 'data-copy="deep-feed-url"' in html
    assert "navigator.clipboard.writeText(document" not in html
