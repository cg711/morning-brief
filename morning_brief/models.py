from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SEGMENTS = ("headlines", "tech", "business", "local")


@dataclass(frozen=True)
class FeedSource:
    segment: str
    name: str
    url: str
    fetch_pages: bool = False


@dataclass(frozen=True)
class Item:
    id: str
    segment: str
    source: str
    title: str
    summary: str
    body: str
    url: str
    published_at: datetime  # timezone-aware
    fetch_pages: bool = False


@dataclass
class Pick:
    story_id: str
    segment: str
    item_ids: list[str]
    reason: str


@dataclass
class Story:
    story_id: str
    segment: str
    items: list[Item]
    text: str
    full: bool
