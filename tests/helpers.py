import json
from datetime import datetime, timezone
from types import SimpleNamespace

from morning_brief import db
from morning_brief.models import Item

NOW = datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc)  # 8:00 AM Central (CDT)


def make_item(id="a1", segment="headlines", source="NPR", title="Title", summary="Summary text.",
              body="", url=None, published_at=None, fetch_pages=False):
    return Item(
        id=id, segment=segment, source=source, title=title, summary=summary, body=body,
        url=url or f"https://example.com/{id}",
        published_at=published_at or datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        fetch_pages=fetch_pages,
    )


def claude_reply(obj, stop_reason="end_turn", input_tokens=1000, output_tokens=200):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(obj))],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeClaude:
    """Stands in for anthropic.Anthropic: fake.messages.create(**kw) returns queued replies in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def script_with_words(n, item_ids=("a1",), segment="headlines"):
    """A valid-shaped script whose total word count is exactly n (n >= 6)."""
    return {
        "intro": "Good morning.",
        "segments": [{"segment": segment, "headline": "Big news", "text": " ".join(["word"] * (n - 5)),
                      "item_ids": list(item_ids)}],
        "outro": "See you tomorrow.",
    }


def seed_episode(conn, settings, date, words=500, audio=b"\xff\xf3\x84\xc4", cutoff_at=None, now=None):
    db.publish_episode(
        conn, date=date, cutoff_at=cutoff_at or f"{date}T13:00:00+00:00",
        script_json=json.dumps(script_with_words(words)), word_count=words, duration_s=200.0,
        audio_bytes=len(audio), now=now or f"{date}T13:05:00+00:00",
        sources=[{"item_id": "a1", "source": "NPR", "title": "Big news", "url": "https://example.com/a1",
                  "published_at": f"{date}T12:00:00+00:00"}],
    )
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    (settings.audio_dir / f"{date}.mp3").write_bytes(audio)


def deep_dive_script(words=2400, sections=4, title="How the Fed Began"):
    """A valid deep-dive script whose word count (intro + sections + outro) is exactly `words`."""
    intro, outro = "Welcome to the deep dive.", "Thanks for listening."  # 5 + 3 words
    body = words - 8
    per = body // sections
    parts = [{"heading": f"Part {n}", "text": " ".join(["word"] * per), "source_ids": ["s1"]}
             for n in range(1, sections + 1)]
    parts[0]["text"] += " word" * (body - per * sections)
    return {
        "title": title, "intro": intro, "sections": parts, "outro": outro,
        "sources": [
            {"id": "s1", "title": "Federal Reserve History", "publisher": "Federal Reserve",
             "url": "https://www.federalreservehistory.org/"},
            {"id": "s2", "title": "The Federal Reserve", "publisher": "Britannica",
             "url": "https://www.britannica.com/topic/Federal-Reserve-System"},
        ],
    }


def two_host_script(words=2400, sections=4, title="How the Fed Began"):
    """deep_dive_script() with each section's text split into a host line and a co-host line (same word count)."""
    script = deep_dive_script(words, sections, title)
    for section in script["sections"]:
        spoken = section.pop("text").split()
        half = len(spoken) // 2
        section["lines"] = [{"speaker": "host", "text": " ".join(spoken[:half])},
                            {"speaker": "cohost", "text": " ".join(spoken[half:])}]
    return script
