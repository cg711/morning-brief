import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from morning_brief import house, house_audio, house_limits, house_public, house_render
from morning_brief.app import create_app
from tests.helpers import NOW, wav_bytes  # 2026-09-25 08:00 Central, a Friday

TODAY = date(2026, 9, 25)
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 200


def make_client(settings, limits=None, **overrides):
    s = replace(settings, **{"house_brief": True, "share_base_url": "https://house.test", **overrides})
    app = create_app(s, clock=lambda: NOW, start_run=lambda t: True, start_render=lambda t: None,
                     start_scheduler=False, house_limits=limits)
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


def post(client, path, **data):
    return client.post(path, data=data, follow_redirects=False)


def test_a_note_is_added_with_credit_and_the_page_confirms(client):
    sam, token = add_member(client)
    r = post(client, f"/h/{token}/notes", text="  We're   out of paper towels ")
    assert r.status_code == 303 and r.headers["location"] == f"/h/{token}?ok=note"
    (note,) = house.pending_notes(conn_of(client))
    assert (note["text"], note["member_id"]) == ("We're out of paper towels", sam)
    assert "Note added." in client.get(r.headers["location"]).text


@pytest.mark.parametrize("text,needle", [("", "write something"), ("x" * 301, "300"), ("bell\x07", "control characters")])
def test_a_bad_note_re_renders_the_page_with_the_reason(client, text, needle):
    _, token = add_member(client)
    r = post(client, f"/h/{token}/notes", text=text)
    assert r.status_code == 400 and needle in r.text and house.pending_notes(conn_of(client)) == []
    assert "Hi Sam" in r.text and r.headers["cache-control"] == "no-store"


def test_a_missing_field_is_a_400_not_a_500(client):
    _, token = add_member(client)
    assert client.post(f"/h/{token}/notes", data={}, follow_redirects=False).status_code == 400
    assert client.post(f"/h/{token}/todos", data={"nope": "x"}, follow_redirects=False).status_code == 400
    assert client.post(f"/h/{token}/notes", content=b"\xff\xfe\x00", headers={"content-type": "text/plain"},
                       follow_redirects=False).status_code == 400


def test_todos_can_be_added_claimed_and_finished(client):
    sam, token = add_member(client)
    r = post(client, f"/h/{token}/todos", text="Buy bulbs", due="2026-10-01")
    assert r.status_code == 303 and r.headers["location"].endswith("?ok=todo")
    (todo,) = house.list_todos(conn_of(client))
    assert (todo["added_by"], todo["due"]) == (sam, "2026-10-01")
    assert post(client, f"/h/{token}/todos/{todo['id']}/claim").headers["location"].endswith("?ok=claimed")
    assert house.list_todos(conn_of(client))[0]["claimed_by"] == sam
    assert post(client, f"/h/{token}/todos/{todo['id']}/done").headers["location"].endswith("?ok=done")
    assert house.list_todos(conn_of(client)) == []
    assert conn_of(client).execute("SELECT done_by FROM house_todos").fetchone()[0] == sam


def test_todo_input_errors_are_400s(client):
    _, token = add_member(client)
    for data in ({"text": ""}, {"text": "x" * 201}, {"text": "ok", "due": "tomorrow"}, {"text": "ok", "due": "9999-12-31"}):
        assert post(client, f"/h/{token}/todos", **data).status_code == 400, data
    assert house.list_todos(conn_of(client)) == []


def test_ids_that_belong_to_nothing_are_harmless(client):
    _, token = add_member(client)
    for path in ("todos/999/claim", "todos/999/done", "prep/999/done", "chores/999/done"):
        r = post(client, f"/h/{token}/{path}")
        assert r.status_code == 303, path
    assert client.post(f"/h/{token}/todos/abc/claim").status_code == 422        # not an int: no token information
    assert client.post(f"/h/{token}/todos/{2**70}/done").status_code == 422


def test_ticking_prep_items_is_credited(client):
    sam, token = add_member(client)
    house.add_event(conn_of(client), "Game night", date(2026, 9, 29), sam, ["chairs"], NOW, TODAY)
    prep_id = house.list_events(conn_of(client), TODAY)[0]["prep"][0]["id"]
    assert post(client, f"/h/{token}/prep/{prep_id}/done").headers["location"].endswith("?ok=done")
    assert house.list_events(conn_of(client), TODAY)[0]["prep"][0]["done"] is True
    assert conn_of(client).execute("SELECT done_by FROM house_prep").fetchone()[0] == sam


def test_a_chore_tick_marks_only_the_open_occurrence(client):
    sam, token = add_member(client)
    open_chore = house.add_chore(conn_of(client), "Trash night", 4, date(2026, 9, 1), [sam], NOW)   # Friday: today
    later = house.add_chore(conn_of(client), "Mail", 0, date(2026, 9, 1), [sam], NOW)               # Monday: 3 days off
    assert post(client, f"/h/{token}/chores/{open_chore}/done").headers["location"].endswith("?ok=done")
    assert house.chore_is_done(conn_of(client), open_chore, TODAY) is True
    post(client, f"/h/{token}/chores/{open_chore}/done")                        # again: harmless
    post(client, f"/h/{token}/chores/{later}/done")                              # nothing open: a no-op
    assert conn_of(client).execute("SELECT COUNT(*) FROM house_chore_done").fetchone()[0] == 1


def test_unknown_and_revoked_tokens_cannot_write_and_look_identical(client):
    sam, token = add_member(client)
    gone_id, gone = add_member(client, "Gone")
    house.revoke_member(conn_of(client), gone_id, NOW)
    paths = ["notes", "todos", "todos/1/claim", "todos/1/done", "prep/1/done", "chores/1/done", "memos"]
    for path in paths:
        seen = {post(client, f"/h/{bad}/{path}", text="hi").status_code for bad in ("x" * 22, gone)}
        bodies = {post(client, f"/h/{bad}/{path}", text="hi").text for bad in ("x" * 22, gone)}
        assert seen == {404} and len(bodies) == 1, path
    assert house.pending_notes(conn_of(client)) == [] and house.list_todos(conn_of(client)) == []


def test_hostile_todo_text_is_stored_as_data_and_shown_escaped(client):
    _, token = add_member(client)
    post(client, f"/h/{token}/todos", text="<img src=x onerror=alert(1)> & <b>")
    page = client.get(f"/h/{token}").text
    assert "<img src=x" not in page and "&lt;img src=x onerror=alert(1)&gt; &amp; &lt;b&gt;" in page


def test_flooding_is_rate_limited_per_link_and_overall(settings):
    limits = house_limits.HouseLimits(write_link=3, write_all=4)
    with make_client(settings, limits=limits) as c:
        _, a = add_member(c, "Sam")
        _, b = add_member(c, "Alex")
        statuses = [post(c, f"/h/{a}/notes", text=f"n{i}").status_code for i in range(5)]
        assert statuses == [303, 303, 303, 429, 429]
        assert post(c, f"/h/{b}/notes", text="mine").status_code == 303        # another link is unaffected...
        assert post(c, f"/h/{b}/notes", text="more").status_code == 429        # ...until the overall cap is reached
        assert len(house.pending_notes(conn_of(c))) == 4
        assert "Slow down" in post(c, f"/h/{a}/notes", text="x").text


def test_a_huge_form_body_is_refused(client):
    _, token = add_member(client)
    r = client.post(f"/h/{token}/notes", data={"text": "x" * 40_000}, follow_redirects=False)
    assert r.status_code == 413 and house.pending_notes(conn_of(client)) == []

    def endless():
        for _ in range(100):
            yield b"text=" + b"x" * 1000

    r = client.post(f"/h/{token}/notes", content=endless(),
                    headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 413


# --- voice memos -----------------------------------------------------------------------------------------------

def fake_ffmpeg(seconds=2.0, amp=0.3, returncode=0, write=True):
    calls = []

    def run(cmd, cwd):
        calls.append(cmd)
        if write:
            Path(cmd[-1]).write_bytes(wav_bytes(seconds, amp=amp))
        return subprocess.CompletedProcess(cmd, returncode, b"", b"")

    run.calls = calls
    return run


@pytest.fixture
def audio(monkeypatch):
    run = fake_ffmpeg()
    monkeypatch.setattr(house_audio, "run_ffmpeg", run)
    monkeypatch.setattr(house_audio.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    return run


def upload(client, token, data=M4A, name="memo.m4a"):
    return client.post(f"/h/{token}/memos", files={"audio": (name, data, "audio/mp4")}, follow_redirects=False)


def incoming(settings):
    d = settings.house_dir / "incoming"
    return sorted(d.glob("*")) if d.exists() else []


def test_a_voice_memo_is_stored_pending_and_credited(client, settings, audio):
    sam, token = add_member(client)
    r = upload(client, token)
    assert r.status_code == 303 and r.headers["location"] == f"/h/{token}?ok=memo"
    (memo,) = house.list_pending_memos(conn_of(client))
    assert (memo["member_id"], memo["status"]) == (sam, "pending") and memo["duration_s"] == pytest.approx(2.0)
    wav = house.memo_path(settings.house_dir, memo["id"])
    assert wav.is_file() and len(house_render.load_clip(wav).samples) == 48000   # the renderer accepts what we stored
    assert incoming(settings) == []
    assert "waiting for the owner" in client.get(r.headers["location"]).text


@pytest.mark.parametrize("make,status,needle", [
    (lambda: (b"%PDF-1.4 hello", "x.pdf"), 415, "audio file"),
    (lambda: (b"", "x.m4a"), 400, "empty"),
])
def test_bad_uploads_get_a_clean_error_and_leave_nothing_behind(client, settings, audio, make, status, needle):
    _, token = add_member(client)
    data, name = make()
    r = upload(client, token, data, name)
    assert r.status_code == status and needle in r.headers["x-memo-error"] and needle in r.text
    assert incoming(settings) == [] and house.list_pending_memos(conn_of(client)) == []


def test_an_oversize_upload_is_a_413(settings, audio):
    with make_client(settings, house_upload_max_mb=1) as c:
        _, token = add_member(c)
        r = upload(c, token, M4A + b"\x00" * 1_600_000)
        assert r.status_code == 413 and "1 MB" in r.headers["x-memo-error"] and incoming(settings) == []


def test_over_length_silent_and_crashing_audio_are_422s(client, settings, monkeypatch):
    _, token = add_member(client)
    monkeypatch.setattr(house_audio.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    for run, needle in [(fake_ffmpeg(seconds=61.0), "longer than 60 seconds"), (fake_ffmpeg(amp=0.0), "silent"),
                        (fake_ffmpeg(returncode=1), "couldn't read"), (fake_ffmpeg(write=False), "couldn't read")]:
        monkeypatch.setattr(house_audio, "run_ffmpeg", run)
        r = upload(client, token)
        assert r.status_code == 422 and needle in r.headers["x-memo-error"], needle
    assert incoming(settings) == [] and house.list_pending_memos(conn_of(client)) == []


def test_without_ffmpeg_uploads_are_unavailable_and_nothing_else_breaks(client, settings, monkeypatch):
    _, token = add_member(client)
    monkeypatch.setattr(house_audio.shutil, "which", lambda name: None)
    r = upload(client, token)
    assert r.status_code == 503 and "unavailable" in r.headers["x-memo-error"]
    assert post(client, f"/h/{token}/notes", text="still works").status_code == 303


def test_the_pending_cap_stops_a_memo_before_any_decoding(settings, audio):
    with make_client(settings, house_pending_memos=2) as c:
        sam, token = add_member(c)
        assert upload(c, token).status_code == 303 and upload(c, token).status_code == 303
        calls = len(audio.calls)
        r = upload(c, token)
        assert r.status_code == 429 and "2 voice notes" in r.headers["x-memo-error"]
        assert len(audio.calls) == calls                       # refused before ffmpeg ran
        house.approve_memo(conn_of(c), house.list_pending_memos(conn_of(c))[0]["id"], NOW)
        assert upload(c, token).status_code == 303             # approving frees a slot


def test_memo_uploads_are_rate_limited(settings, audio):
    limits = house_limits.HouseLimits(memo_link=2)
    with make_client(settings, limits=limits) as c:
        _, token = add_member(c)
        assert [upload(c, token).status_code for _ in range(3)] == [303, 303, 429]


def test_an_unexpected_failure_is_a_clean_500_without_orphans(client, settings, audio, monkeypatch):
    _, token = add_member(client)

    def boom(*args, **kwargs):
        raise RuntimeError("disk exploded /srv/secret/path")

    monkeypatch.setattr(house_audio, "save_wav", boom)
    r = upload(client, token)
    assert r.status_code == 500 and "disk exploded" not in r.text and "/srv" not in r.text
    assert house.list_pending_memos(conn_of(client)) == [] and incoming(settings) == []
    monkeypatch.setattr(house_audio, "prepare", boom)
    r = upload(client, token)
    assert r.status_code == 500 and incoming(settings) == []


def test_two_files_or_no_file_are_400s(client, settings, audio):
    _, token = add_member(client)
    two = client.post(f"/h/{token}/memos", files=[("audio", ("a.m4a", M4A)), ("audio", ("b.m4a", M4A))],
                      follow_redirects=False)
    none = client.post(f"/h/{token}/memos", data={"audio": "just text"}, follow_redirects=False)
    assert two.status_code == 400 and none.status_code in (400, 415)
    assert incoming(settings) == [] and house.list_pending_memos(conn_of(client)) == []


def test_unknown_tokens_do_not_use_up_the_write_limits(settings):
    limits = house_limits.HouseLimits(write_link=2, write_all=2, memo_link=1, memo_all=1)
    with make_client(settings, limits=limits) as c:
        _, token = add_member(c)
        for i in range(10):
            assert post(c, f"/h/{'x' * 21}{i}/notes", text="hi").status_code == 404
            assert c.post(f"/h/{'y' * 21}{i}/memos", files={"audio": ("a.m4a", M4A)}).status_code == 404
        assert post(c, f"/h/{token}/notes", text="one").status_code == 303     # budgets untouched by strangers
        assert post(c, f"/h/{token}/notes", text="two").status_code == 303


def test_a_full_to_do_list_is_a_clean_400(client):
    _, token = add_member(client)
    for i in range(house.MAX_OPEN_TODOS):
        house.add_todo(conn_of(client), f"t{i}", None, None, NOW)
    r = post(client, f"/h/{token}/todos", text="one more")
    assert r.status_code == 400 and "to-do list is full" in r.text
    assert len(house.list_todos(conn_of(client))) == 100


# --- slow and concurrent uploads, and small leftovers ------------------------------------------------------------

def slow_post(app, token, chunks=20, delay=0.3):
    """POST a never-quite-finishing chunked multipart body straight to the ASGI app, one chunk every `delay` s."""
    import asyncio

    import httpx

    async def body():
        yield b'--b\r\nContent-Disposition: form-data; name="audio"; filename="m.m4a"\r\n\r\n'
        for _ in range(chunks):
            await asyncio.sleep(delay)
            yield b"\x00" * 100

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post(f"/h/{token}/memos", content=body(),
                                headers={"content-type": "multipart/form-data; boundary=b"})

    return asyncio.run(go())


def test_a_slow_upload_times_out_with_a_408_and_leaves_nothing(settings, audio, monkeypatch):
    monkeypatch.setattr(house_public, "RECEIVE_TIMEOUT_S", 0.5)
    with make_client(settings) as c:
        _, token = add_member(c)
        r = slow_post(c.app, token)
        assert r.status_code == 408 and r.headers["x-memo-error"] == "that upload took too long"
        assert incoming(settings) == [] and house.list_pending_memos(conn_of(c)) == []
        assert upload(c, token).status_code == 303                  # the link still works afterwards


def test_too_many_uploads_in_flight_is_a_429_before_reading_the_body(settings, audio, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("receive must not run when the server is busy")

    monkeypatch.setattr(house_public, "MAX_RECEIVING", 0)
    monkeypatch.setattr(house_audio, "receive", never)
    with make_client(settings) as c:
        _, token = add_member(c)
        r = upload(c, token)
        assert r.status_code == 429 and r.headers["x-memo-error"] == "busy right now, try again in a minute"
        assert "busy right now" in r.text


def test_the_in_flight_counter_returns_to_zero_after_success_and_failure(settings, audio, monkeypatch):
    monkeypatch.setattr(house_public, "MAX_RECEIVING", 1)       # a leaked count would make every later upload a 429
    with make_client(settings) as c:
        _, token = add_member(c)
        assert upload(c, token).status_code == 303
        assert upload(c, token, b"%PDF-1.4 hello", "x.pdf").status_code == 415
        assert upload(c, token, b"", "x.m4a").status_code == 400
        assert upload(c, token).status_code == 303
        monkeypatch.setattr(house_public, "RECEIVE_TIMEOUT_S", 0.5)
        assert slow_post(c.app, token).status_code == 408
        assert upload(c, token).status_code == 303


def test_a_failed_save_removes_the_partial_wav_too(client, settings, audio, monkeypatch):
    _, token = add_member(client)

    def partial(path, samples):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_name(path.name + ".tmp").write_bytes(b"half a wav")
        raise OSError("disk full")

    monkeypatch.setattr(house_audio, "save_wav", partial)
    assert upload(client, token).status_code == 500
    memos = settings.house_dir / "memos"
    assert not memos.exists() or list(memos.iterdir()) == []
    assert incoming(settings) == [] and house.list_pending_memos(conn_of(client)) == []


def test_a_non_ascii_digit_content_length_is_refused_not_a_crash():
    import asyncio

    from starlette.requests import Request

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"",
             "headers": [(b"content-length", "²".encode("latin-1")),
                         (b"content-type", b"application/x-www-form-urlencoded")]}

    async def go():
        return await house_public.small_form(Request(scope))

    with pytest.raises(house_public.FormTooLarge):
        asyncio.run(go())
