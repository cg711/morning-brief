"""UI login: a password from UI_PASSWORD, an HMAC-signed session cookie, and a failed-attempt limiter.

The cookie value is "<expiry unix seconds>.<hex HMAC-SHA256 of the expiry>", keyed with the session secret
plus sha256(password), so changing the password logs every device out.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import os
import re
import secrets
import threading
from pathlib import Path

COOKIE = "mb_session"
SECRET_FILE = "session_secret"
SECRET_BYTES = 32
# Token-gated or public paths that never need the login cookie (podcast apps, the Mac worker, share links).
OPEN_PREFIXES = ("/static/", "/s/", "/feed/", "/audio/", "/api/")
_EXPIRY_RE = re.compile(r"[0-9]{1,12}")
_SAFE_NEXT_RE = re.compile(r"/(?![/\\])[^\\\x00-\x1f\x7f]*")


def load_secret(data_dir: Path) -> bytes:
    """The cookie-signing secret from data_dir/session_secret, created (mode 0600) on first use."""
    path = Path(data_dir) / SECRET_FILE
    try:
        secret = path.read_bytes()
        if len(secret) >= SECRET_BYTES:
            return secret
    except FileNotFoundError:
        pass
    secret = secrets.token_bytes(SECRET_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    os.chmod(path, 0o600)
    return secret


def _key(secret: bytes, password: str) -> bytes:
    return secret + hashlib.sha256(password.encode()).digest()


def sign(secret: bytes, password: str, expiry: int) -> str:
    mac = hmac.new(_key(secret, password), str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{mac}"


def verify(secret: bytes, password: str, value: str | None, now: int) -> bool:
    if not value or not password:
        return False
    expiry, _, mac = value.partition(".")
    if not mac or not _EXPIRY_RE.fullmatch(expiry):
        return False
    if not hmac.compare_digest(sign(secret, password, int(expiry)).encode(), value.encode()):
        return False
    return int(expiry) > now


def safe_next(value: str | None) -> str:
    """A local path to return to after login; anything that could leave the site becomes '/'."""
    if not value or "://" in value or not _SAFE_NEXT_RE.fullmatch(value):
        return "/"
    return value


class FailureLimiter:
    """At most max_failures wrong passwords per window seconds, app-wide (single user, in memory)."""

    def __init__(self, max_failures: int = 5, window: float = 600):
        self.max_failures, self.window = max_failures, window
        self._times: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._times and self._times[0] <= now - self.window:
            self._times.popleft()

    def blocked(self, now: float) -> bool:
        with self._lock:
            self._trim(now)
            return len(self._times) >= self.max_failures

    def record_failure(self, now: float) -> None:
        with self._lock:
            self._trim(now)
            self._times.append(now)
