import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from morning_brief import house, house_audio, house_facts, house_render, speech
from morning_brief.app import create_app
from tests.helpers import NOW  # 2026-09-25 08:00 Central, a Friday

HX = {"HX-Request": "true"}
TODAY = date(2026, 9, 25)
FEED = "t" * 40


def make_client(settings, started=None, **overrides):
    started = [] if started is None else started
    app = create_app(replace(settings, **overrides), clock=lambda: NOW, start_run=lambda t: True,
                     start_render=lambda t: None, start_scheduler=False, start_house=lambda: started.append(1))
    return TestClient(app)


@pytest.fixture
def started():
    return []


@pytest.fixture
def ui(settings, started):
    with make_client(settings, started, house_brief=True) as c:
        yield c


def conn_of(client):
    return client.app.state.conn


def add_member(ui, name="Sam"):
    ui.post("/house/members", data={"name": name}, headers=HX)
    return [m for m in house.list_members(conn_of(ui)) if m["name"] == name][0]["id"]


def test_the_card_shows_only_when_the_house_brief_is_on(ui, settings):
    html = ui.get("/").text
    assert 'id="house-card"' in html and "What the next brief would say" in html
    with make_client(settings) as off:
        assert 'id="house-card"' not in off.get("/").text
        assert off.post("/house/members", data={"name": "Sam"}, headers=HX).status_code == 404
        assert off.get(f"/feed/house/{FEED}.xml").status_code == 404


def test_every_ui_route_needs_the_htmx_header(ui):
    sam = add_member(ui)
    for method, path in [("post", "/house/members"), ("delete", f"/house/members/{sam}"), ("post", "/house/chores"),
                         ("delete", "/house/chores/1"), ("post", "/house/chores/1/done"), ("post", "/house/events"), ("post", "/house/prep/1/tick"),
                         ("delete", "/house/events/1"), ("post", "/house/upkeep"), ("post", "/house/upkeep/1/done"),
                         ("delete", "/house/upkeep/1"), ("post", "/house/todos"), ("post", "/house/todos/1/claim"),
                         ("post", "/house/todos/1/done"), ("delete", "/house/todos/1"), ("delete", "/house/notes/1"),
                         ("post", "/house/generate")]:
        assert getattr(ui, method)(path).status_code == 403, path


def test_the_login_protects_the_house_routes(settings):
    with make_client(settings, house_brief=True, ui_password="pw") as c:
        assert c.post("/house/members", data={"name": "Sam"}, headers=HX).status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 303


def test_members(ui):
    html = ui.post("/house/members", data={"name": " Sam "}, headers=HX).text
    assert "Sam" in html
    assert "already a" in ui.post("/house/members", data={"name": "sam"}, headers=HX).text
    assert "write something" in ui.post("/house/members", data={"name": ""}, headers=HX).text
    (m,) = house.list_members(conn_of(ui))
    html = ui.delete(f"/house/members/{m['id']}", headers=HX).text
    assert house.list_members(conn_of(ui)) == [] and "No housemates yet" in html


def test_chores_show_whose_turn_it_is(ui):
    sam, alex = add_member(ui, "Sam"), add_member(ui, "Alex")
    html = ui.post("/house/chores", data={"name": "Trash night", "weekday": "1", "start": "2026-09-21",
                                         "rotation": [str(sam), str(alex)]}, headers=HX).text
    # next Tuesday is Sep 29, the second week of the rotation: Alex
    assert "Trash night" in html and "Tue Sep 29" in html and "Alex" in html
    (chore,) = house.list_chores(conn_of(ui))
    assert chore["rotation"] == [sam, alex]
    for bad, text in [({"name": "", "weekday": "1", "start": "2026-09-21"}, "write something"),
                      ({"name": "X", "weekday": "9", "start": "2026-09-21"}, "weekday"),
                      ({"name": "X", "weekday": "abc", "start": "2026-09-21"}, "weekday"),
                      ({"name": "X", "weekday": "1", "start": ""}, "start date"),
                      ({"name": "X", "weekday": "1", "start": "2026-09-21", "rotation": "999"}, "housemates")]:
        assert text in ui.post("/house/chores", data=bad, headers=HX).text
    html = ui.delete(f"/house/chores/{chore['id']}", headers=HX).text
    assert house.list_chores(conn_of(ui)) == [] and "<strong>Trash night</strong>" not in html


def test_hosting_with_prep_items(ui):
    sam = add_member(ui)
    html = ui.post("/house/events", data={"title": "Game night", "day": "2026-09-29", "host": str(sam),
                                          "prep": "chairs\n\nsnacks\n"}, headers=HX).text
    assert "Game night" in html and "chairs" in html and "snacks" in html
    (event,) = house.list_events(conn_of(ui), TODAY)
    assert [p["text"] for p in event["prep"]] == ["chairs", "snacks"] and event["host_id"] == sam
    html = ui.post(f"/house/prep/{event['prep'][0]['id']}/tick", headers=HX).text
    assert house.list_events(conn_of(ui), TODAY)[0]["prep"][0]["done"] is True
    for bad, text in [({"title": "", "day": "2026-09-29"}, "write something"),
                      ({"title": "X", "day": "2026-09-01"}, "later day"), ({"title": "X", "day": ""}, "date"),
                      ({"title": "X", "day": "2026-09-29", "host": "999"}, "host")]:
        assert text in ui.post("/house/events", data=bad, headers=HX).text
    ui.delete(f"/house/events/{event['id']}", headers=HX)
    assert house.list_events(conn_of(ui), TODAY) == []


def test_upkeep_every_n_days_and_yearly(ui):
    html = ui.post("/house/upkeep", data={"label": "Furnace filter", "kind": "every", "every_days": "90",
                                         "last_done": "2026-08-01"}, headers=HX).text
    assert "Furnace filter" in html and "Oct 30" in html  # Aug 1 + 90 days
    ui.post("/house/upkeep", data={"label": "Hose bibs", "kind": "yearly", "month": "10", "day": "15"}, headers=HX)
    items = house.list_upkeep(conn_of(ui))
    assert [(i["kind"], i["every_days"]) for i in items] == [("every", 90), ("yearly", None)]
    for bad, text in [({"label": "X", "kind": "every", "every_days": ""}, "every how many"),
                      ({"label": "X", "kind": "yearly", "month": "2", "day": "30"}, "month and day"),
                      ({"label": "X", "kind": "weekly"}, "how often"),
                      ({"label": "X", "kind": "every", "every_days": "5", "last_done": "2027-01-01"}, "future")]:
        assert text in ui.post("/house/upkeep", data=bad, headers=HX).text
    ui.post(f"/house/upkeep/{items[0]['id']}/done", headers=HX)
    assert house.list_upkeep(conn_of(ui))[0]["last_done"] == TODAY
    ui.delete(f"/house/upkeep/{items[1]['id']}", headers=HX)
    assert len(house.list_upkeep(conn_of(ui))) == 1


def test_todos(ui):
    sam = add_member(ui)
    html = ui.post("/house/todos", data={"text": "Buy bulbs", "due": "2026-10-01"}, headers=HX).text
    assert "Buy bulbs" in html and "Oct 1" in html
    (todo,) = house.list_todos(conn_of(ui))
    ui.post(f"/house/todos/{todo['id']}/claim", data={"member": str(sam)}, headers=HX)
    assert house.list_todos(conn_of(ui))[0]["claimed_by"] == sam
    ui.post(f"/house/todos/{todo['id']}/claim", data={"member": ""}, headers=HX)
    assert house.list_todos(conn_of(ui))[0]["claimed_by"] is None
    assert "housemate" in ui.post(f"/house/todos/{todo['id']}/claim", data={"member": "999"}, headers=HX).text
    assert "write something" in ui.post("/house/todos", data={"text": ""}, headers=HX).text
    assert "date" in ui.post("/house/todos", data={"text": "x", "due": "tomorrow"}, headers=HX).text
    ui.post(f"/house/todos/{todo['id']}/done", headers=HX)
    assert house.list_todos(conn_of(ui)) == []
    other = house.add_todo(conn_of(ui), "Sweep", None, None, NOW)
    ui.delete(f"/house/todos/{other}", headers=HX)
    assert house.list_todos(conn_of(ui)) == []


def test_notes_from_housemates_can_be_deleted_and_are_escaped(ui):
    sam = add_member(ui)
    house.add_note(conn_of(ui), "<script>alert(1)</script> & <b>hi</b>", sam, NOW)
    html = ui.get("/").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)" not in html
    (note,) = house.pending_notes(conn_of(ui))
    html = ui.delete(f"/house/notes/{note['id']}", headers=HX).text
    assert house.pending_notes(conn_of(ui)) == [] and "alert(1)" not in html


def test_hostile_titles_labels_and_names_are_escaped(ui):
    ui.post("/house/members", data={"name": "<i>Sam</i>"}, headers=HX)
    ui.post("/house/events", data={"title": "<img src=x>", "day": "2026-09-29"}, headers=HX)
    ui.post("/house/upkeep", data={"label": "<b>x</b>", "kind": "every", "every_days": "5"}, headers=HX)
    html = ui.get("/").text
    for raw in ("<i>Sam</i>", "<img src=x>", "<b>x</b>"):
        assert raw not in html
    assert "&lt;i&gt;Sam&lt;/i&gt;" in html


def test_the_preview_shows_what_the_next_brief_would_say(ui):
    assert "Nothing due" in ui.get("/").text
    house.add_note(conn_of(ui), "We're out of paper towels", None, NOW)
    html = ui.get("/").text
    assert "A note: We&#39;re out of paper towels." in html and "Nothing due" not in html


def test_generate_now_starts_the_background_job(ui, started):
    html = ui.post("/house/generate", headers=HX).text
    assert started == [1] and "Making the episode" in html


def test_the_status_line(ui):
    assert "No episode yet" in ui.get("/").text
    house.save_episode(conn_of(ui), TODAY, {"title": "t", "intro": "i", "sections": [], "outro": "o"},
                       duration_s=125.0, audio_bytes=10, word_count=3, chapters=None, now=NOW)
    conn_of(ui).execute("INSERT INTO app_state (key, value) VALUES ('house_last_error', '2026-09-24|RuntimeError: boom')")
    html = ui.get("/").text
    assert "Last episode: 2026-09-25 (2.1 min)" in html and "RuntimeError: boom" in html


def test_the_owner_feed_and_audio(ui, settings):
    script = {"title": "House brief: Friday, September 25", "intro": "Hi.", "outro": "Bye.",
              "sections": [{"heading": "Notes", "kind": "text", "text": "Sam says: hello."}]}
    house.save_episode(conn_of(ui), TODAY, script, duration_s=95.0, audio_bytes=4, word_count=5, chapters=None, now=NOW)
    settings.house_dir.mkdir(parents=True, exist_ok=True)
    house.episode_path(settings.house_dir, TODAY).write_bytes(b"\xff\xf3\x84\xc4")
    assert ui.get("/feed/house/wrong.xml").status_code == 404
    r = ui.get(f"/feed/house/{FEED}.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml")
    item = ET.fromstring(r.content).find("channel/item")
    assert item.find("title").text == "House brief: Friday, September 25"
    assert item.find("enclosure").get("url").startswith(f"https://brief.test/audio/{FEED}/house/2026-09-25.mp3?v=")
    assert "Sam says: hello." in item.find("description").text
    audio = ui.get(f"/audio/{FEED}/house/2026-09-25.mp3", headers={"Range": "bytes=0-1"})
    assert audio.status_code == 206 and audio.content == b"\xff\xf3"
    assert ui.get(f"/audio/wrong/house/2026-09-25.mp3").status_code == 404
    assert ui.get(f"/audio/{FEED}/house/2026-09-24.mp3").status_code == 404
    assert ui.get(f"/audio/{FEED}/house/not-a-date.mp3").status_code == 404
    assert ui.get("/feed/house-cover.png").content[:4] == b"\x89PNG"


def test_funnel_traffic_cannot_reach_the_house_routes(ui):
    funnel = {"Tailscale-Funnel-Request": "?1"}
    assert ui.get(f"/feed/house/{FEED}.xml", headers=funnel).status_code == 404
    assert ui.post("/house/members", data={"name": "Sam"}, headers={**HX, **funnel}).status_code == 404


def test_non_numeric_ids_in_the_path_are_not_server_errors(ui):
    for method, path in [("post", "/house/prep/abc/tick"), ("delete", "/house/members/abc"),
                         ("delete", "/house/chores/abc"), ("delete", "/house/events/abc"),
                         ("post", "/house/upkeep/abc/done"), ("delete", "/house/upkeep/abc"),
                         ("post", "/house/todos/abc/claim"), ("post", "/house/todos/abc/done"),
                         ("delete", "/house/todos/abc"), ("delete", "/house/notes/abc")]:
        assert getattr(ui, method)(path, headers=HX).status_code == 422, path


def test_unknown_ids_and_junk_form_values_render_the_card_not_a_500(ui):
    for method, path in [("post", "/house/prep/999/tick"), ("delete", "/house/members/999"),
                         ("delete", "/house/chores/999"), ("delete", "/house/events/999"),
                         ("post", "/house/upkeep/999/done"), ("delete", "/house/upkeep/999"),
                         ("post", "/house/todos/999/done"), ("delete", "/house/todos/999"),
                         ("delete", "/house/notes/999")]:
        r = getattr(ui, method)(path, headers=HX)
        assert r.status_code == 200 and 'id="house-card"' in r.text, path
    junk = [("/house/upkeep", {"label": "X", "kind": "every", "every_days": "abc"}),
            ("/house/upkeep", {"label": "X", "kind": "yearly", "month": "x", "day": "y"}),
            ("/house/upkeep", {"label": "X", "kind": "every", "every_days": "5", "last_done": "nope"}),
            ("/house/events", {"title": "X", "day": "2026-09-29", "host": "abc"}),
            ("/house/events", {"title": "X", "day": "31/12/2026"}),
            ("/house/chores", {"name": "X", "weekday": "1", "start": "2026-09-21", "rotation": ["a"]}),
            ("/house/todos/1/claim", {"member": "abc"}),
            ("/house/members", {"name": "bad\x00name"})]
    for path, data in junk:
        r = ui.post(path, data=data, headers=HX)
        assert r.status_code == 200 and 'id="house-card"' in r.text and 'class="warn small"' in r.text, (path, data)


def test_hostile_todo_text_and_prep_items_are_escaped(ui):
    ui.post("/house/todos", data={"text": "<script>x</script>"}, headers=HX)
    ui.post("/house/events", data={"title": "E", "day": "2026-09-29", "prep": "<b>chairs</b>"}, headers=HX)
    html = ui.get("/").text
    assert "<script>x" not in html and "<b>chairs</b>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html and "&lt;b&gt;chairs&lt;/b&gt;" in html


def test_house_feed_routes_stay_off_when_the_brief_is_off(settings):
    with make_client(settings) as off:
        assert off.get(f"/audio/{FEED}/house/2026-09-25.mp3").status_code == 404
        assert off.get("/feed/house-cover.png").status_code == 404
        for method, path in [("post", "/house/generate"), ("post", "/house/todos"), ("delete", "/house/notes/1")]:
            assert getattr(off, method)(path, headers=HX).status_code == 404, path


def test_funnel_feeds_flag_does_not_open_the_ui_routes(settings):
    with make_client(settings, house_brief=True, funnel_feeds=True) as c:
        funnel = {"Tailscale-Funnel-Request": "?1"}
        assert c.post("/house/generate", headers={**HX, **funnel}).status_code == 404
        assert c.get(f"/feed/house/{FEED}.xml", headers=funnel).status_code == 200


def test_the_audio_route_cannot_be_walked_out_of_the_house_dir(ui):
    assert ui.get(f"/audio/{FEED}/house/..%2F..%2Fsecret.mp3").status_code == 404
    assert ui.get(f"/audio/{FEED}/house/2026-13-45.mp3").status_code == 404


def test_far_future_dates_show_an_error_and_never_break_the_page(ui):
    html = ui.post("/house/chores", data={"name": "X", "weekday": "1", "start": "9999-12-31"}, headers=HX).text
    assert "within ten years" in html
    assert house.list_chores(conn_of(ui)) == []
    for path, data in [("/house/events", {"title": "X", "day": "9999-12-31"}),
                       ("/house/todos", {"text": "X", "due": "9999-12-31"}),
                       ("/house/upkeep", {"label": "X", "kind": "every", "every_days": "5", "last_done": "0001-01-01"})]:
        assert "within ten years" in ui.post(path, data=data, headers=HX).text, path
    assert ui.get("/").status_code == 200


def test_the_card_does_not_touch_the_house_tables_when_the_brief_is_off(settings, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the House tables were read")

    for name in ("list_members", "member_names", "list_chores", "list_events", "list_upkeep", "list_todos",
                 "pending_notes", "list_episodes", "get_episode"):
        monkeypatch.setattr(house, name, boom)
    with make_client(settings) as off:
        r = off.get("/")
        assert r.status_code == 200 and 'id="house-card"' not in r.text


def test_huge_path_ids_are_rejected_not_server_errors(ui):
    big = 2**64
    for method, path in [("delete", f"/house/members/{big}"), ("delete", f"/house/chores/{big}"),
                         ("post", f"/house/prep/{big}/tick"), ("delete", f"/house/events/{big}"),
                         ("post", f"/house/upkeep/{big}/done"), ("delete", f"/house/todos/{big}"),
                         ("post", f"/house/todos/{big}/claim"), ("delete", f"/house/notes/{big}"),
                         ("delete", "/house/members/0")]:
        assert getattr(ui, method)(path, headers=HX).status_code == 422, path


def test_upkeep_due_tomorrow_says_tomorrow(ui):
    ui.post("/house/upkeep", data={"label": "Filter", "kind": "every", "every_days": "5",
                                   "last_done": "2026-09-21"}, headers=HX)  # due Sep 26
    html = ui.get("/").text
    assert "(tomorrow)" in html and "in 1 days" not in html


def add_friday_chore(ui):
    """A chore due today (Fri Sep 25), so it is in the mention window."""
    ui.post("/house/chores", data={"name": "Dishes", "weekday": "4", "start": "2026-09-21"}, headers=HX)
    return house.list_chores(conn_of(ui))[0]["id"]


def test_the_owner_can_tick_off_a_chore(ui):
    chore = add_friday_chore(ui)
    assert [c["name"] for c in house_facts.gather(conn_of(ui), TODAY)["chores"]] == ["Dishes"]
    html = ui.get("/").text
    assert f'hx-post="/house/chores/{chore}/done"' in html and "Done (today)" in html
    html = ui.post(f"/house/chores/{chore}/done", headers=HX).text
    assert house_facts.gather(conn_of(ui), TODAY)["chores"] == []
    assert "Dishes" in html and "/done" not in html.split('<h3 class="sub">Chores</h3>')[1].split("Hosting")[0]
    assert ui.post(f"/house/chores/{chore}/done", headers=HX).status_code == 200  # twice is harmless
    assert conn_of(ui).execute("SELECT COUNT(*) FROM house_chore_done").fetchone()[0] == 1


def test_ticking_a_chore_with_nothing_open_or_nothing_there_does_nothing(ui):
    ui.post("/house/chores", data={"name": "Mow", "weekday": "1", "start": "2026-09-21"}, headers=HX)  # next Tuesday
    (chore,) = house.list_chores(conn_of(ui))
    r = ui.post(f"/house/chores/{chore['id']}/done", headers=HX)
    assert r.status_code == 200 and "Mow" in r.text
    r = ui.post("/house/chores/999/done", headers=HX)
    assert r.status_code == 200 and 'id="house-card"' in r.text
    assert conn_of(ui).execute("SELECT COUNT(*) FROM house_chore_done").fetchone()[0] == 0


def test_the_chore_done_route_is_off_when_the_brief_is_off(settings):
    with make_client(settings) as off:
        assert off.post("/house/chores/1/done", headers=HX).status_code == 404


def test_the_card_shows_the_house_feed_url(ui):
    html = ui.get("/").text
    assert f'id="house-feed-url" readonly value="https://brief.test/feed/house/{FEED}.xml"' in html
    assert 'data-copy="house-feed-url"' in html


def test_the_owner_feed_still_matches_after_the_shared_helper(ui, settings):
    from morning_brief import house_feed

    script = {"title": "House brief: Friday, September 25", "intro": "Hi.", "outro": "Bye.", "sections": []}
    house.save_episode(conn_of(ui), TODAY, script, duration_s=95.0, audio_bytes=4, word_count=5, chapters=None, now=NOW)
    (ep,) = house_feed.episodes(conn_of(ui))
    assert ep["date"] == "2026-09-25" and ep["title"] == "House brief: Friday, September 25"
    assert set(ep) == {"date", "title", "created_at", "updated_at", "audio_bytes", "duration_s", "description"}


def memo_with_audio(ui, settings, member_id, status="pending", seconds=2.0):
    memo_id = house.add_memo(conn_of(ui), member_id, seconds, NOW, 10)
    if status == "approved":
        house.approve_memo(conn_of(ui), memo_id, NOW)
    elif status == "rejected":
        house.reject_memo(conn_of(ui), memo_id)
    samples = (np.sin(2 * np.pi * 440 * np.arange(int(24000 * seconds)) / 24000) * 0.3).astype(np.float32)
    house_audio.save_wav(house.memo_path(settings.house_dir, memo_id), samples)
    return memo_id


def test_the_card_shows_each_housemates_link_and_feed(settings):
    with make_client(settings, house_brief=True, share_base_url="https://house.test") as c:
        add_member(c, "Sam")
        token = house.list_members(conn_of(c))[0]["token"]
        html = c.get("/").text
        assert f"https://house.test/h/{token}" in html and f"https://house.test/h/{token}/feed.xml" in html
        assert 'data-copy="member-link-' in html
    with make_client(settings, house_brief=True, share_base_url="") as c:
        add_member(c, "Sam")
        token = house.list_members(conn_of(c))[0]["token"]
        html = c.get("/").text
        assert f"/h/{token}" not in html and "SHARE_BASE_URL" in html


def test_pending_voice_notes_are_listed_with_a_player(ui, settings):
    sam = add_member(ui, "Sam")
    memo = memo_with_audio(ui, settings, sam, seconds=12.4)
    html = ui.get("/").text
    assert "Voice notes waiting" in html and "Sam" in html and "12 s" in html
    assert f'src="/house/memos/{memo}/audio"' in html
    assert f"/house/memos/{memo}/approve" in html and f"/house/memos/{memo}/reject" in html


def test_approve_and_reject(ui, settings):
    sam = add_member(ui, "Sam")
    keep, drop = memo_with_audio(ui, settings, sam), memo_with_audio(ui, settings, sam)
    html = ui.post(f"/house/memos/{keep}/approve", headers=HX).text
    assert house.get_memo(conn_of(ui), keep)["status"] == "approved" and "1 approved, waiting to play" in html
    assert house.memo_path(settings.house_dir, drop).exists()
    ui.post(f"/house/memos/{drop}/reject", headers=HX)
    assert house.get_memo(conn_of(ui), drop)["status"] == "rejected"
    assert not house.memo_path(settings.house_dir, drop).exists() and house.memo_path(settings.house_dir, keep).exists()
    assert "Voice notes waiting" not in ui.get("/").text
    for path in (f"/house/memos/{keep}/approve", f"/house/memos/{keep}/reject", f"/house/memos/{drop}/approve",
                 "/house/memos/999/approve", "/house/memos/999/reject"):
        assert ui.post(path, headers=HX).status_code == 200            # decided or unknown: a harmless no-op
    assert house.get_memo(conn_of(ui), drop)["status"] == "rejected"


def test_memo_routes_need_the_htmx_header_and_the_brief_on(ui, settings):
    sam = add_member(ui, "Sam")
    memo = memo_with_audio(ui, settings, sam)
    assert ui.post(f"/house/memos/{memo}/approve").status_code == 403
    assert ui.post(f"/house/memos/{memo}/reject").status_code == 403
    with make_client(settings, house_brief=False) as off:
        assert off.post("/house/memos/1/approve", headers=HX).status_code == 404
        assert off.get("/house/memos/1/audio").status_code == 404


def test_the_memo_audio_route_serves_only_pending_or_approved_files(ui, settings):
    sam = add_member(ui, "Sam")
    pending = memo_with_audio(ui, settings, sam)
    approved = memo_with_audio(ui, settings, sam, status="approved")
    rejected = memo_with_audio(ui, settings, sam, status="rejected")
    for memo in (pending, approved):
        r = ui.get(f"/house/memos/{memo}/audio")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
        assert r.headers["cache-control"] == "no-store"
    assert ui.get(f"/house/memos/{rejected}/audio").status_code == 404
    assert ui.get("/house/memos/999/audio").status_code == 404
    assert ui.get("/house/memos/abc/audio").status_code == 422
    house.memo_path(settings.house_dir, pending).unlink()
    assert ui.get(f"/house/memos/{pending}/audio").status_code == 404           # a missing file is a 404, not a 500
    conn_of(ui).execute("UPDATE house_memos SET status = 'played', played_on = '2026-09-24' WHERE id = ?", (approved,))
    assert ui.get(f"/house/memos/{approved}/audio").status_code == 404


def test_the_memo_audio_route_is_behind_the_login_and_off_funnel(settings):
    with make_client(settings, house_brief=True, ui_password="pw") as c:
        assert c.get("/house/memos/1/audio", follow_redirects=False).status_code == 303
    with make_client(settings, house_brief=True) as c:
        assert c.get("/house/memos/1/audio", headers={"Tailscale-Funnel-Request": "?1"}).status_code == 404


def test_an_approved_memo_is_spoken_in_the_next_episode(ui, settings):
    sam = add_member(ui, "Sam")
    memo = memo_with_audio(ui, settings, sam, status="approved")
    assert house_render.generate(settings, synthesize=speech.fake_synthesize, now=lambda: NOW,
                                 sleep=lambda s: None) == "ready"
    episode = house.get_episode(conn_of(ui), TODAY)
    assert "Voice note from Sam" in episode["script_json"] and house.get_memo(conn_of(ui), memo)["status"] == "played"


def test_the_remove_confirmation_says_what_is_deleted(ui):
    add_member(ui, "Sam")
    html = ui.get("/").text
    assert ("Remove Sam? Their link stops working at once, and their unread notes and unapproved voice notes "
            "are deleted.") in html
