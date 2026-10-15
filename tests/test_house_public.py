import base64
import hashlib
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient

from morning_brief import house, house_public
from morning_brief.app import create_app
from tests.helpers import NOW  # 2026-09-25 08:00 Central, a Friday

FUNNEL = {"Tailscale-Funnel-Request": "?1"}
TODAY = date(2026, 9, 25)
SHARE = "https://house.test"


def make_client(settings, **overrides):
    s = replace(settings, **{"house_brief": True, "share_base_url": SHARE, **overrides})
    app = create_app(s, clock=lambda: NOW, start_run=lambda t: True, start_render=lambda t: None,
                     start_scheduler=False)
    return TestClient(app)


@pytest.fixture
def client(settings):
    with make_client(settings) as c:
        yield c


def conn_of(client):
    return client.app.state.conn


def add_member(client, name="Sam"):
    member_id = house.add_member(conn_of(client), name, NOW)
    return member_id, [m for m in house.list_members(conn_of(client)) if m["id"] == member_id][0]["token"]


def episode(client, settings, day=TODAY, body=b"\xff\xf3\x84\xc4"):
    script = {"title": "House brief: Friday, September 25", "intro": "Hi.", "outro": "Bye.",
              "sections": [{"heading": "Notes", "kind": "text", "text": "Sam says: hello."}]}
    house.save_episode(conn_of(client), day, script, duration_s=95.0, audio_bytes=len(body), word_count=5,
                       chapters=None, now=NOW)
    settings.house_dir.mkdir(parents=True, exist_ok=True)
    house.episode_path(settings.house_dir, day).write_bytes(body)


def test_the_page_greets_the_housemate_and_shows_the_house(client, settings):
    sam, token = add_member(client)
    house.add_event(conn_of(client), "Game night", date(2026, 9, 29), sam, ["chairs", "snacks"], NOW, TODAY)
    house.add_todo(conn_of(client), "Buy bulbs", None, None, NOW)
    house.add_chore(conn_of(client), "Trash night", 4, date(2026, 9, 1), [sam], NOW)   # a Friday: open today
    r = client.get(f"/h/{token}")
    html = r.text
    assert r.status_code == 200 and "Hi Sam" in html
    assert "Game night" in html and "chairs" in html and "Buy bulbs" in html and "Trash night" in html
    assert "plays for the whole house" in html


def test_the_page_leaks_nothing_from_the_owner_side(client, settings):
    _, token = add_member(client)
    html = client.get(f"/h/{token}").text
    for needle in (settings.feed_token, "hx-", "/login", "/house/", "/feed/", "/static/", "Oura"):
        assert needle not in html


def test_the_page_headers(client):
    _, token = add_member(client)
    r = client.get(f"/h/{token}")
    h = r.headers
    assert h["cache-control"] == "no-store" and h["x-robots-tag"] == "noindex, nofollow"
    assert h["referrer-policy"] == "no-referrer" and h["x-content-type-options"] == "nosniff"
    assert h["permissions-policy"] == "microphone=(self)"
    csp = h["content-security-policy"]
    expected = "sha256-" + base64.b64encode(hashlib.sha256(house_public.RECORD_SCRIPT.encode()).digest()).decode()
    assert f"script-src '{expected}'" in csp and house_public.SCRIPT_HASH == expected
    for part in ("default-src 'self'", "media-src 'self' blob:", "connect-src 'self'", "form-action 'self'",
                 "frame-ancestors 'none'", "base-uri 'none'"):
        assert part in csp
    assert "unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert r.text.count(house_public.RECORD_SCRIPT) == 1


ROUTES = [("GET", "/h/{t}"), ("GET", "/h/{t}/feed.xml"), ("GET", "/h/{t}/audio/2026-09-25.mp3"),
          ("GET", "/h/{t}/cover.png"), ("GET", "/h/{t}/nope"), ("GET", "/h/{t}/a/b/c"), ("POST", "/h/{t}/notes"),
          ("POST", "/h/{t}/memos"), ("POST", "/h/{t}/nope"), ("HEAD", "/h/{t}/feed.xml"),
          ("HEAD", "/h/{t}/audio/2026-09-25.mp3"), ("HEAD", "/h/{t}/cover.png")]
BAD_TOKENS = ["x" * 22, "a", "é" * 22, "A" * 500, "..%2f..%2fetc", "%00", "a b", "%78" * 22]


def test_unknown_revoked_and_odd_tokens_get_the_identical_404(client, settings):
    sam, token = add_member(client)
    episode(client, settings)
    gone_id, gone = add_member(client, "Gone")
    house.revoke_member(conn_of(client), gone_id, NOW)
    for method, template in ROUTES:
        reference = None
        for bad in [gone, *BAD_TOKENS]:
            r = client.request(method, template.format(t=bad))
            seen = (r.status_code, r.text, r.headers.get("content-type"), r.headers.get("content-security-policy"),
                    r.headers.get("cache-control"))
            if bad == "a b":   # contains a space, so it is a different URL; only require a 404
                assert r.status_code == 404
                continue
            reference = reference or seen
            assert seen == reference, (method, template, bad)
            assert r.status_code == 404
    assert client.get(f"/h/{token}").status_code == 200   # the live link still works


def test_revoking_ends_access_at_once(client):
    sam, token = add_member(client)
    assert client.get(f"/h/{token}").status_code == 200
    house.revoke_member(conn_of(client), sam, NOW)
    assert client.get(f"/h/{token}").status_code == 404
    assert client.get(f"/h/{token}/feed.xml").status_code == 404


def test_everything_is_a_404_when_the_house_brief_is_off(settings):
    with make_client(settings, house_brief=False) as c:
        for method, template in ROUTES:
            assert c.request(method, template.format(t="x" * 22)).status_code == 404


def test_the_housemate_feed(client, settings):
    _, token = add_member(client)
    episode(client, settings)
    r = client.get(f"/h/{token}/feed.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml")
    assert r.headers["cache-control"] == "no-store"
    channel = ET.fromstring(r.content).find("channel")
    assert channel.find("title").text == "House Brief"
    item = channel.find("item")
    assert item.find("enclosure").get("url").startswith(f"{SHARE}/h/{token}/audio/2026-09-25.mp3?v=")
    assert "Sam says: hello." in item.find("description").text
    assert settings.feed_token not in r.text
    cover = channel.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image").get("href")
    assert cover == f"{SHARE}/h/{token}/cover.png"


def test_the_feed_works_without_a_share_origin_by_using_the_public_base_url(settings):
    with make_client(settings, share_base_url="") as c:
        _, token = add_member(c)
        episode(c, settings)
        item = ET.fromstring(c.get(f"/h/{token}/feed.xml").content).find("channel/item")
        assert item.find("enclosure").get("url").startswith(f"{settings.public_base_url}/h/{token}/audio/")


def test_the_housemate_audio_and_cover(client, settings):
    _, token = add_member(client)
    episode(client, settings)
    audio = client.get(f"/h/{token}/audio/2026-09-25.mp3", headers={"Range": "bytes=0-1"})
    assert audio.status_code == 206 and audio.content == b"\xff\xf3"
    assert audio.headers["x-content-type-options"] == "nosniff" and audio.headers["x-robots-tag"] == "noindex, nofollow"
    assert client.get(f"/h/{token}/audio/2026-09-24.mp3").status_code == 404
    assert client.get(f"/h/{token}/audio/not-a-date.mp3").status_code == 404
    assert client.get(f"/h/{token}/audio/2026-13-45.mp3").status_code == 404
    assert client.get(f"/h/{token}/cover.png").content[:4] == b"\x89PNG"


def test_funnel_traffic_reaches_h_but_nothing_else(client, settings):
    _, token = add_member(client)
    episode(client, settings)
    assert client.get(f"/h/{token}", headers=FUNNEL).status_code == 200
    assert client.get(f"/h/{token}/feed.xml", headers=FUNNEL).status_code == 200
    assert client.get(f"/h/{token}/audio/2026-09-25.mp3", headers=FUNNEL).status_code == 200
    for path in ("/", "/login", "/house/members", f"/feed/house/{settings.feed_token}.xml",
                 f"/audio/{settings.feed_token}/house/2026-09-25.mp3", "/static/app.css", "/api/daily/claim",
                 "/feed/house-cover.png", "/house/memos/1/audio", "/hh/x", "/h"):
        assert client.get(path, headers=FUNNEL).status_code == 404, path


def test_opening_h_does_not_open_the_owner_side(settings):
    with make_client(settings, ui_password="pw") as c:
        _, token = add_member(c)
        assert c.get(f"/h/{token}").status_code == 200       # no cookie needed
        assert c.get("/", follow_redirects=False).status_code == 303
        assert c.post("/house/members", data={"name": "X"}, headers={"HX-Request": "true"}).status_code == 401
        assert c.get("/house/memos/1/audio", follow_redirects=False).status_code == 303


def test_the_page_escapes_what_housemates_wrote(client):
    _, token = add_member(client, "<i>Sam</i>")
    house.add_todo(conn_of(client), "<script>alert(1)</script>", None, None, NOW)
    html = client.get(f"/h/{token}").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;i&gt;Sam&lt;/i&gt;" in html and "<i>Sam</i>" not in html


def test_the_page_says_when_voice_notes_are_unavailable(client, monkeypatch):
    _, token = add_member(client)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert "Voice notes are unavailable" in client.get(f"/h/{token}").text
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    page = client.get(f"/h/{token}").text
    assert "Voice notes are unavailable" not in page and 'name="audio"' in page


def test_ok_messages_come_from_a_fixed_list(client):
    _, token = add_member(client)
    assert "Note added." in client.get(f"/h/{token}?ok=note").text
    page = client.get(f"/h/{token}?ok=<script>alert(1)</script>").text
    assert "alert(1)" not in page


def test_the_page_shows_at_most_fifty_open_todos(client):
    _, token = add_member(client)
    for i in range(60):
        house.add_todo(conn_of(client), f"chore number {i:03d}", None, None, NOW)
    page = client.get(f"/h/{token}").text
    assert page.count("chore number ") == 50
    assert "chore number 049" in page and "chore number 050" not in page


def test_head_on_the_public_routes_is_uniform_for_bad_tokens_and_empty_for_good_ones(client, settings):
    _, token = add_member(client)
    episode(client, settings)
    gone_id, gone = add_member(client, "Gone")
    house.revoke_member(conn_of(client), gone_id, NOW)
    assert client.head(f"/h/{token}").status_code == 200 and client.head(f"/h/{'x' * 22}").status_code == 404
    tails = ["/feed.xml", "/audio/2026-09-25.mp3", "/cover.png"]
    for tail in tails:
        ok = client.head(f"/h/{token}{tail}")
        assert ok.status_code == 200 and ok.content == b"", tail
        reference = None
        for bad in ("x" * 22, "%78" * 22, gone, "é" * 22, "A" * 500):
            r = client.head(f"/h/{bad}{tail}")
            seen = (r.status_code, r.headers.get("content-length"), r.headers.get("content-type"),
                    r.headers.get("content-security-policy"), r.headers.get("cache-control"))
            reference = reference or seen
            assert seen == reference and r.status_code == 404, (tail, bad)
    for bad in ("x" * 22, "%78" * 22):
        assert client.get(f"/h/{bad}/feed.xml").status_code == 404
        assert client.get(f"/h/{bad}/feed.xml").text == client.get(f"/h/{'y' * 22}/cover.png").text


def test_the_recorder_stops_a_second_before_the_cap(client):
    assert "Math.max(1, maxSeconds - 1) * 1000" in house_public.RECORD_SCRIPT
    assert "maxSeconds * 1000" not in house_public.RECORD_SCRIPT
