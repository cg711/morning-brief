from __future__ import annotations

import re
from datetime import datetime, timedelta

from .models import Item

DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(hours=72)
_NON_WORD = re.compile(r"[^a-z0-9 ]")


def window_start(previous_cutoff: datetime | None, now: datetime) -> datetime:
    """Cover everything since the previous episode, but never more than 72 hours."""
    if previous_cutoff is None:
        return now - DEFAULT_WINDOW
    return max(previous_cutoff, now - MAX_WINDOW)


def _normalize(title: str) -> str:
    return " ".join(_NON_WORD.sub("", title.lower()).split())


def select_window(items: list[Item], start: datetime) -> list[Item]:
    kept, seen_ids, seen_titles = [], set(), set()
    for item in sorted(items, key=lambda i: i.published_at, reverse=True):
        if item.published_at <= start:
            continue
        title = _normalize(item.title)
        if item.id in seen_ids or title in seen_titles:
            continue
        seen_ids.add(item.id)
        seen_titles.add(title)
        kept.append(item)
    return kept
