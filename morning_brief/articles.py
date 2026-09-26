from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from .feeds import USER_AGENT
from .models import Item, Pick, Story

MIN_FULL_WORDS = 150
MAX_STORY_WORDS = 800


def truncate_words(text: str, n: int) -> str:
    return " ".join(text.split()[:n])


class RobotsCache:
    """robots.txt per host, fetched once per run. A missing robots.txt allows everything."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self._parsers: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._parsers:
            parser = RobotFileParser()
            try:
                response = self.client.get(root + "/robots.txt")
                parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            except httpx.HTTPError:
                parser.parse([])
            self._parsers[root] = parser
        return self._parsers[root].can_fetch(USER_AGENT, url)


def item_text(item: Item, client: httpx.Client, robots: RobotsCache) -> tuple[str, bool]:
    """Best available text for an item: feed body, then the article page, then the feed summary."""
    if len(item.body.split()) >= MIN_FULL_WORDS:
        return item.body, True
    if item.fetch_pages and robots.allowed(item.url):
        try:
            response = client.get(item.url)
            if response.status_code == 200:
                extracted = trafilatura.extract(response.text) or ""
                if len(extracted.split()) >= MIN_FULL_WORDS:
                    return extracted, True
        except httpx.HTTPError:
            pass
    return item.summary or item.title, False


def build_stories(picks: list[Pick], items_by_id: dict[str, Item], client: httpx.Client,
                  robots: RobotsCache) -> list[Story]:
    stories = []
    for pick in picks:
        items = [items_by_id[i] for i in pick.item_ids if i in items_by_id]
        if not items:
            continue
        per_item = MAX_STORY_WORDS // len(items)
        parts, full = [], False
        for item in items:
            text, is_full = item_text(item, client, robots)
            full = full or is_full
            parts.append(f"[{item.source}] {truncate_words(text, per_item)}")
        stories.append(Story(pick.story_id, pick.segment, items, "\n\n".join(parts), full))
    return stories
