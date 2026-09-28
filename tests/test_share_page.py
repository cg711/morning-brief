import base64
import hashlib
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from morning_brief import deepdives, share_routes, shares
from morning_brief.app import create_app
from tests.helpers import NOW, deep_dive_script

FEED = "t" * 40
FUNNEL = {"Tailscale-Funnel-Request": "?1"}
AUDIO = b"\xff\xf3\x84\xc4"


def make_client(settings, **overrides):
    app = create_app(replace(settings, **overrides), clock=lambda: NOW, start_run=lambda t: True,
                     start_render=lambda t: None, start_scheduler=False)
    return TestClient(app)


@pytest.fixture
def client(settings):
    with make_client(settings, share_base_url="https://box.example.ts.net") as c:
        yield c


def publish(client, settings, title="How the Fed Began"):
    conn = client.app.state.conn
    topic_id = deepdives.add_topic(conn, "The Fed", "", NOW)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, topic_id, deep_dive_script(title=title), NOW)
    settings.deep_dives_dir.mkdir(parents=True, exist_ok=True)
    deepdives.audio_path(settings.deep_dives_dir, topic_id).write_bytes(AUDIO)
    deepdives.publish(conn, topic_id, title=title, word_count=2400, duration_s=1200.0, audio_bytes=4, now=NOW,
                      chapters=[("Introduction", 0.0), ("Part 1", 12.5), ("Part 2", 300.0), ("Part 3", 600.0),
                                ("Part 4", 900.0), ("Outro", 1190.0)])
    return topic_id, shares.create_or_get(conn, topic_id, NOW)


def test_share_page_renders_episode_without_internal_details(client, settings):
    _, token = publish(client, settings)
    r = client.get(f"/s/{token}")
    assert r.status_code == 200
    html = r.text
    assert "<title>How the Fed Began</title>" in html
    assert f'src="/s/{token}/audio.mp3"' in html
    assert "Part 1" in html and "Federal Reserve History" in html and "Welcome to the deep dive." in html
    assert 'data-seek="12.5"' in html
    assert '<meta property="og:title" content="How the Fed Began">' in html
    assert '<meta property="og:description" content="Welcome to the deep dive.">' in html
    assert FEED not in html
    assert "Go deeper" not in html and "hx-" not in html and "/static/" not in html


def test_share_page_escapes_the_title(client, settings):
    _, token = publish(client, settings, title="<script>alert(1)</script>")
    html = client.get(f"/s/{token}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)" not in html


def test_share_page_security_headers_and_csp_hash(client, settings):
    _, token = publish(client, settings)
    r = client.get(f"/s/{token}")
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "form-action 'none'" in csp
    scripts = re.findall(r"<script>(.*?)</script>", r.text, re.S)
    assert len(scripts) == 1
    digest = "sha256-" + base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    assert digest == share_routes.SCRIPT_HASH and f"script-src '{digest}'" in csp


def test_share_audio_supports_ranges(client, settings):
    _, token = publish(client, settings)
    full = client.get(f"/s/{token}/audio.mp3")
    assert full.status_code == 200 and full.content == AUDIO and full.headers["content-type"] == "audio/mpeg"
    part = client.get(f"/s/{token}/audio.mp3", headers={"Range": "bytes=0-1"})
    assert part.status_code == 206 and part.content == AUDIO[:2]


def test_unknown_revoked_or_missing_audio_gives_the_same_404(client, settings):
    topic_id, token = publish(client, settings)
    for path in ("/s/bogus", "/s/bogus/audio.mp3"):
        r = client.get(path)
        assert r.status_code == 404 and "This link isn't available." in r.text
        for name, value in share_routes.SHARE_HEADERS.items():
            assert r.headers[name] == value
    deepdives.audio_path(settings.deep_dives_dir, topic_id).unlink()
    assert client.get(f"/s/{token}").status_code == 404
    shares.revoke(client.app.state.conn, topic_id, NOW)
    assert client.get(f"/s/{token}").status_code == 404
    assert client.get(f"/s/{token}/audio.mp3").status_code == 404


def test_unmatched_share_paths_get_uniform_404(client):
    for path in ("/s/x/other", "/s/x/y/z"):
        r = client.get(path)
        assert r.status_code == 404 and "This link isn't available." in r.text
        assert "content-security-policy" in r.headers


def test_funnel_login_and_share_seam(settings):
    with make_client(settings, ui_password="pw", share_base_url="https://box.example.ts.net") as c:
        _, token = publish(c, settings)
        assert c.get(f"/s/{token}", headers=FUNNEL, follow_redirects=False).status_code == 200
        assert c.get(f"/s/{token}/audio.mp3", headers=FUNNEL, follow_redirects=False).status_code == 200
        assert c.get("/", headers=FUNNEL, follow_redirects=False).status_code == 404
        assert c.get("/login", headers=FUNNEL, follow_redirects=False).status_code == 404


def test_funnel_reaches_only_share_paths(client, settings):
    _, token = publish(client, settings)
    assert client.get(f"/s/{token}", headers=FUNNEL).status_code == 200
    assert client.get(f"/s/{token}/audio.mp3", headers=FUNNEL).status_code == 200
    for path in ("/", "/login", f"/feed/{FEED}.xml", f"/feed/deep-dives/{FEED}.xml", "/static/app.css"):
        assert client.get(path, headers=FUNNEL, follow_redirects=False).status_code == 404


def test_funnel_feeds_opt_in(settings):
    with make_client(settings, funnel_feeds=True) as c:
        assert c.get(f"/feed/{FEED}.xml", headers=FUNNEL).status_code == 200
        assert c.get("/", headers=FUNNEL).status_code == 404


def test_share_page_needs_no_login(settings):
    with make_client(settings, ui_password="pw", share_base_url="https://box.example.ts.net") as c:
        _, token = publish(c, settings)
        assert c.get(f"/s/{token}", follow_redirects=False).status_code == 200
        assert c.get(f"/s/{token}/audio.mp3", follow_redirects=False).status_code == 200


def test_first_sentence():
    assert share_routes.first_sentence("Welcome to the deep dive. More here.") == "Welcome to the deep dive."
    assert share_routes.first_sentence("  Why?  Because.") == "Why?"
    assert share_routes.first_sentence("no punctuation at all") == "no punctuation at all"
    long = share_routes.first_sentence("word " * 100)
    assert len(long) <= 201 and long.endswith("…")
