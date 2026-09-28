import stat

import pytest

from morning_brief import auth

SECRET = b"s" * 32


def test_sign_verify_round_trip():
    value = auth.sign(SECRET, "pw", 2_000)
    assert auth.verify(SECRET, "pw", value, 1_999) is True


def test_expired_cookie_fails():
    value = auth.sign(SECRET, "pw", 2_000)
    assert auth.verify(SECRET, "pw", value, 2_000) is False
    assert auth.verify(SECRET, "pw", value, 5_000) is False


@pytest.mark.parametrize("bad", [None, "", "2000", "2000.", ".abc", "abc.def", "²000.abc", "2000.zz"])
def test_malformed_values_fail(bad):
    assert auth.verify(SECRET, "pw", bad, 1_000) is False


def test_tampered_expiry_or_mac_fails():
    value = auth.sign(SECRET, "pw", 2_000)
    mac = value.split(".", 1)[1]
    assert auth.verify(SECRET, "pw", f"9999.{mac}", 1_000) is False
    assert auth.verify(SECRET, "pw", value[:-1] + ("0" if value[-1] != "0" else "1"), 1_000) is False


def test_password_change_or_other_secret_invalidates():
    value = auth.sign(SECRET, "pw", 2_000)
    assert auth.verify(SECRET, "new pw", value, 1_000) is False
    assert auth.verify(b"o" * 32, "pw", value, 1_000) is False
    assert auth.verify(SECRET, "", value, 1_000) is False


def test_load_secret_creates_private_file_and_reuses_it(tmp_path):
    first = auth.load_secret(tmp_path)
    path = tmp_path / "session_secret"
    assert len(first) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.load_secret(tmp_path) == first


def test_load_secret_replaces_a_short_file(tmp_path):
    (tmp_path / "session_secret").write_bytes(b"short")
    assert len(auth.load_secret(tmp_path)) == 32


@pytest.mark.parametrize("value,expected", [
    ("/", "/"),
    ("/partials/today", "/partials/today"),
    ("/?a=1&b=2", "/?a=1&b=2"),
    (None, "/"),
    ("", "/"),
    ("//evil.example", "/"),
    ("/\\evil.example", "/"),
    ("https://evil.example", "/"),
    ("evil", "/"),
    ("/ok?next=https://evil", "/"),
    ("/a\r\nSet-Cookie: x", "/"),
    ("/a\\b", "/"),
])
def test_safe_next(value, expected):
    assert auth.safe_next(value) == expected


def test_failure_limiter_window():
    limiter = auth.FailureLimiter(max_failures=5, window=600)
    for n in range(5):
        assert limiter.blocked(100 + n) is False
        limiter.record_failure(100 + n)
    assert limiter.blocked(105) is True
    assert limiter.blocked(699) is True
    assert limiter.blocked(700) is False  # the failure at 100 has aged out


def test_open_prefixes():
    assert set(auth.OPEN_PREFIXES) == {"/static/", "/s/", "/feed/", "/audio/", "/api/"}
