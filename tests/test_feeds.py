from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from morning_brief.config import PROJECT_DIR
from morning_brief.feeds import fetch_all, item_id, load_sources, parse_feed, strip_html
from morning_brief.models import SEGMENTS, FeedSource

REAL = Path(__file__).parent / "fixtures" / "rss" / "samples"
WORDS = " ".join(["council"] * 200)
FULL_RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Fixture</title><link>https://example.com</link><description>d</description>
<item><title>City council passes &amp; budget</title><link>https://example.com/story-1</link>
<pubDate>Fri, 25 Sep 2026 01:39:49 +0000</pubDate>
<description><![CDATA[<p>The council voted <b>7-6</b>.</p>]]></description>
<content:encoded><![CDATA[<p>{WORDS}</p>]]></content:encoded></item>
<item><title>No date</title><link>https://example.com/no-date</link></item>
<item><title>No link</title><pubDate>Fri, 25 Sep 2026 02:00:00 +0000</pubDate></item>
</channel></rss>""".encode()
LOCAL = FeedSource("local", "MPR News", "https://local.test/rss", False)


def test_load_sources(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text(
        "segments:\n"
        "  local:\n    - name: MPR News\n      url: https://x/feed\n"
        "  headlines:\n    - name: BBC\n      url: https://b/rss\n      fetch_pages: true\n"
    )
    assert load_sources(p) == [
        FeedSource("local", "MPR News", "https://x/feed", False),
        FeedSource("headlines", "BBC", "https://b/rss", True),
    ]


def test_unknown_segment_rejected(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("segments:\n  sports:\n    - name: X\n      url: https://x\n")
    with pytest.raises(ValueError, match="sports"):
        load_sources(p)


def test_repo_feeds_yaml_covers_every_segment():
    sources = load_sources(PROJECT_DIR / "feeds.yaml")
    assert {s.segment for s in sources} == set(SEGMENTS)
    assert len(sources) == 12
    assert not any(s.fetch_pages for s in sources if "npr.org" in s.url or "dowjones" in s.url)


def test_strip_html():
    assert strip_html("<p>The council voted <b>7-6</b>.</p>") == "The council voted 7-6."
    assert strip_html("Tom &amp; Jerry<br/>again") == "Tom & Jerry again"


def test_parse_feed_full_content_and_skips_incomplete_items():
    items = parse_feed(LOCAL, FULL_RSS)
    assert len(items) == 1
    it = items[0]
    assert it.id == item_id("https://example.com/story-1")
    assert it.title == "City council passes & budget"
    assert it.summary == "The council voted 7-6."
    assert len(it.body.split()) == 200
    assert it.published_at == datetime(2026, 9, 25, 1, 39, 49, tzinfo=timezone.utc)
    assert (it.segment, it.source, it.fetch_pages) == ("local", "MPR News", False)


def test_item_id_is_stable_and_short():
    assert item_id("https://a/1") == item_id("https://a/1") != item_id("https://a/2")
    assert len(item_id("https://a/1")) == 10


def test_fetch_all_collects_errors():
    def handler(request):
        if request.url.host == "local.test":
            return httpx.Response(200, content=FULL_RSS)
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    broken = FeedSource("local", "Broken Feed", "https://broken.test/rss")
    items, errors = fetch_all([LOCAL, broken], client)
    assert len(items) == 1
    assert len(errors) == 1 and errors[0].startswith("Broken Feed: HTTPStatusError")


@pytest.mark.parametrize("path", sorted(REAL.glob("*.xml")), ids=lambda p: p.stem)
def test_real_samples_parse(path):
    items = parse_feed(FeedSource("headlines", path.stem, "https://x"), path.read_bytes())
    assert items
    assert all(i.title and i.url.startswith("http") and i.published_at.tzinfo for i in items)
