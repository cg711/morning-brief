from __future__ import annotations

import calendar
import hashlib
import html
import re
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
import yaml

from .models import SEGMENTS, FeedSource, Item

USER_AGENT = "morning-brief/0.1"
_TAG = re.compile(r"<[^>]+>")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?])")


def load_sources(path: str | Path) -> list[FeedSource]:
    data = yaml.safe_load(Path(path).read_text())
    sources = []
    for segment, entries in data["segments"].items():
        if segment not in SEGMENTS:
            raise ValueError(f"unknown segment {segment!r}; expected one of {SEGMENTS}")
        for e in entries:
            sources.append(FeedSource(segment, e["name"], e["url"], bool(e.get("fetch_pages", False))))
    return sources


def strip_html(s: str) -> str:
    text = html.unescape(_TAG.sub(" ", s or ""))
    return _SPACE_BEFORE_PUNCT.sub(r"\1", " ".join(text.split()))


def item_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def parse_feed(source: FeedSource, content: bytes) -> list[Item]:
    items = []
    for e in feedparser.parse(content).entries:
        parsed_time = e.get("published_parsed") or e.get("updated_parsed")
        link = e.get("link")
        if not parsed_time or not link:
            continue
        body = strip_html(e.content[0].get("value", "")) if e.get("content") else ""
        items.append(Item(
            id=item_id(link),
            segment=source.segment,
            source=source.name,
            title=strip_html(e.get("title", "")),
            summary=strip_html(e.get("summary", "")),
            body=body,
            url=link,
            published_at=datetime.fromtimestamp(calendar.timegm(parsed_time), tz=timezone.utc),
            fetch_pages=source.fetch_pages,
        ))
    return items


def fetch_all(sources: list[FeedSource], client: httpx.Client) -> tuple[list[Item], list[str]]:
    """Fetch every feed. A failing feed is recorded in the error list and skipped."""
    items, errors = [], []
    for source in sources:
        try:
            response = client.get(source.url)
            response.raise_for_status()
            items.extend(parse_feed(source, response.content))
        except Exception as exc:  # one bad feed must not sink the episode
            errors.append(f"{source.name}: {type(exc).__name__}: {exc}"[:200])
    return items, errors
