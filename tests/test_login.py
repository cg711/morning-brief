from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from morning_brief.app import create_app
from tests.helpers import NOW

PASSWORD = "correct horse"
TOKEN = "t" * 40
HX = {"HX-Request": "true"}
AUTH = {"Authorization": "Bearer " + "w" * 40}


def make_client(settings, now, password=PASSWORD):
    app = create_app(replace(settings, ui_password=password, worker_token="w" * 40),
                     clock=lambda: now[0], start_run=lambda t: True, start_render=lambda t: None,
                     start_scheduler=False)
    return TestClient(app)


@pytest.fixture
def now():
    return [NOW]


@pytest.fixture
def client(settings, now):
    with make_client(settings, now) as c:
        yield c


def log_in(client, next_path="/"):
    r = client.post("/login", data={"password": PASSWORD, "next": next_path}, follow_redirects=False)
    assert r.status_code == 303
    return r


def test_login_off_by_default(settings):
    app = create_app(settings, clock=lambda: NOW, start_run=lambda t: True, start_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert "/logout" not in c.get("/").text
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"


def test_pages_redirect_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F"
    r = client.get("/partials/deep-dives?sig=abc", follow_redirects=False)
    assert r.headers["location"] == "/login?next=%2Fpartials%2Fdeep-dives%3Fsig%3Dabc"


def test_htmx_requests_get_401_with_redirect(client):
    r = client.get("/partials/today", headers=HX)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"
    assert client.post("/deep-dives", data={"topic": "X"}, headers=HX).status_code == 401


def test_open_paths_are_not_gated(client):
    assert client.get("/login").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/feed/cover.png").status_code == 200
    assert client.get(f"/feed/{TOKEN}.xml").status_code == 200
    assert client.get(f"/audio/{TOKEN}/2026-09-25.mp3", follow_redirects=False).status_code == 404
    assert client.post("/api/deep-dives/claim", headers=AUTH).status_code == 204
    assert client.get("/s/bogus", follow_redirects=False).status_code == 404


def test_login_sets_cookie_and_grants_access(client):
    r = log_in(client)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("mb_session=")
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    assert "max-age=7776000" in cookie.lower() and "path=/" in cookie.lower()
    assert PASSWORD not in cookie
    page = client.get("/")
    assert page.status_code == 200 and 'hx-post="/logout"' in page.text


def test_login_redirects_only_to_local_paths(client):
    assert log_in(client, "/partials/today").headers["location"] == "/partials/today"
    assert log_in(client, "//evil.example").headers["location"] == "/"
    assert log_in(client, "https://evil.example").headers["location"] == "/"


def test_already_logged_in_login_page_redirects(client):
    log_in(client)
    r = client.get("/login?next=/partials/today", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/partials/today"


def test_wrong_password(client):
    r = client.post("/login", data={"password": "nope", "next": "/"}, follow_redirects=False)
    assert r.status_code == 401 and "Wrong password." in r.text
    assert "set-cookie" not in r.headers
    assert client.get("/", follow_redirects=False).status_code == 303


def test_rate_limit_blocks_even_the_right_password_until_the_window_passes(client, now):
    for _ in range(5):
        assert client.post("/login", data={"password": "nope"}, follow_redirects=False).status_code == 401
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 429 and "Too many attempts" in r.text
    now[0] = NOW + timedelta(minutes=10)
    assert log_in(client).status_code == 303


def test_cookie_expires_after_session_days(client, now):
    log_in(client)
    now[0] = NOW + timedelta(days=90, seconds=1)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_password_change_logs_out_but_restart_does_not(settings, now):
    with make_client(settings, now) as a:
        value = log_in(a).cookies["mb_session"]
    cookie = {"Cookie": f"mb_session={value}"}
    with make_client(settings, now, password="a new password") as b:
        assert b.get("/", headers=cookie, follow_redirects=False).status_code == 303
    with make_client(settings, now) as c:  # same password, same data_dir secret
        assert c.get("/", headers=cookie, follow_redirects=False).status_code == 200


def test_logout(client):
    log_in(client)
    assert client.post("/logout").status_code == 403
    r = client.post("/logout", headers=HX)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/login"
    assert "mb_session=" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert client.get("/", follow_redirects=False).status_code == 303
