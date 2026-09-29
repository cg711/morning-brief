from __future__ import annotations

from datetime import date, datetime
from email.utils import format_datetime
from xml.etree import ElementTree as ET

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)
TITLE = "Morning Brief"
DEEP_DIVE_TITLE = "Morning Brief: Deep Dives"


def episode_title(date_str: str) -> str:
    d = date.fromisoformat(date_str)
    return f"{d:%A, %B} {d.day}"


def describe(script: dict, sources: list[dict]) -> str:
    by_id = {s["item_id"]: s for s in sources}
    parts = [script["intro"]]
    for seg in script["segments"]:
        if seg.get("segment") in ("personal", "notes"):  # sleep, spending and notes stay out of the feed
            continue
        parts.append(f"{seg['headline']}\n{seg['text']}")
        cited = [by_id[i] for i in seg["item_ids"] if i in by_id]
        if cited:
            parts.append("Sources: " + "; ".join(f"{s['source']}: {s['title']} ({s['url']})" for s in cited))
    parts.append(script["outro"])
    return "\n\n".join(parts)


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = text
    return el


def _channel(title: str, description: str, base_url: str, cover_path: str):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = _sub(rss, "channel")
    _sub(channel, "title", title)
    _sub(channel, "link", base_url)
    _sub(channel, "description", description)
    _sub(channel, "language", "en-us")
    _sub(channel, f"{{{ITUNES}}}author", "morning-brief")
    _sub(channel, f"{{{ITUNES}}}image", href=f"{base_url}{cover_path}")
    _sub(channel, f"{{{ITUNES}}}explicit", "false")
    _sub(channel, f"{{{ITUNES}}}type", "episodic")
    _sub(channel, f"{{{ITUNES}}}block", "yes")
    return rss, channel


def build_feed(episodes: list[dict], base_url: str, token: str) -> bytes:
    rss, channel = _channel(TITLE, "A private daily news briefing.", base_url, "/feed/cover.png")
    for ep in episodes:
        version = int(datetime.fromisoformat(ep["updated_at"]).timestamp())
        item = _sub(channel, "item")
        _sub(item, "title", episode_title(ep["date"]))
        _sub(item, "guid", ep["date"], isPermaLink="false")
        _sub(item, "pubDate", format_datetime(datetime.fromisoformat(ep["created_at"])))
        _sub(item, "enclosure", url=f"{base_url}/audio/{token}/{ep['date']}.mp3?v={version}",
             length=ep["audio_bytes"], type="audio/mpeg")
        _sub(item, f"{{{ITUNES}}}duration", str(round(ep["duration_s"])))
        _sub(item, "description", ep["description"])
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def describe_deep_dive(script: dict, sources: list[dict]) -> str:
    lines = [script["intro"], "", "Sources:"]
    for n, s in enumerate(sources, start=1):
        publisher = f" — {s['publisher']}" if s.get("publisher") else ""
        lines.append(f"{n}. {s['title']}{publisher} ({s['url']})")
    return "\n".join(lines)


def build_deep_dive_feed(episodes: list[dict], base_url: str, token: str) -> bytes:
    rss, channel = _channel(DEEP_DIVE_TITLE, "Private long-form deep dives on topics you queue.",
                            base_url, "/feed/deep-dives-cover.png")
    for ep in episodes:
        version = int(datetime.fromisoformat(ep["updated_at"]).timestamp())
        item = _sub(channel, "item")
        _sub(item, "title", ep["title"])
        _sub(item, "guid", f"deep-dive-{ep['id']}", isPermaLink="false")
        _sub(item, "pubDate", format_datetime(datetime.fromisoformat(ep["published_at"])))
        _sub(item, "enclosure", url=f"{base_url}/audio/{token}/deep-dives/{ep['id']}.mp3?v={version}",
             length=ep["audio_bytes"], type="audio/mpeg")
        _sub(item, f"{{{ITUNES}}}duration", str(round(ep["duration_s"])))
        _sub(item, "description", ep["description"])
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
