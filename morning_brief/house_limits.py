"""In-memory rate limits for the housemate pages. A restart forgets them, which is fine for a household."""
from __future__ import annotations

import collections
import threading

MAX_KEYS = 1024


class RateLimiter:
    """At most `limit` events per `window` seconds for each key. Refused attempts are not counted."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._events: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            if len(self._events) > MAX_KEYS:
                self._forget_old(now)
            queue = self._events.setdefault(key, collections.deque())
            while queue and queue[0] <= now - self.window:
                queue.popleft()
            if len(queue) >= self.limit:
                return False
            queue.append(now)
            return True

    def _forget_old(self, now: float) -> None:
        for key in [k for k, q in self._events.items() if not q or q[-1] <= now - self.window]:
            del self._events[key]


class HouseLimits:
    """Writes (notes, to-dos, claims, ticks): per link and overall. Voice memos: stricter, since each one costs a
    decode."""

    def __init__(self, write_link: int = 30, write_all: int = 120, memo_link: int = 6, memo_all: int = 30):
        self._write_link, self._write_all = RateLimiter(write_link, 60), RateLimiter(write_all, 60)
        self._memo_link, self._memo_all = RateLimiter(memo_link, 3600), RateLimiter(memo_all, 3600)

    def allow_write(self, token: str, now: float) -> bool:
        return self._write_link.allow(token, now) and self._write_all.allow("*", now)

    def allow_memo(self, token: str, now: float) -> bool:
        return self._memo_link.allow(token, now) and self._memo_all.allow("*", now)
