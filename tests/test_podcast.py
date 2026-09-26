from xml.etree import ElementTree as ET

from morning_brief.podcast import DEEP_DIVE_TITLE, ITUNES, build_deep_dive_feed, build_feed, describe, describe_deep_dive, episode_title
from tests.helpers import script_with_words

BASE, TOKEN = "https://brief.test", "t" * 40


def episode(updated_at="2026-09-25T13:05:00+00:00"):
    return {"date": "2026-09-25", "created_at": "2026-09-25T13:05:00+00:00", "updated_at": updated_at,
            "audio_bytes": 4, "duration_s": 200.4, "description": "Big news"}


def parse(xml: bytes):
    return ET.fromstring(xml).find("channel")


def test_episode_title():
    assert episode_title("2026-09-25") == "Friday, September 25"


def test_feed_has_required_tags():
    channel = parse(build_feed([episode()], BASE, TOKEN))
    assert channel.findtext("title") == "Morning Brief"
    assert channel.find(f"{{{ITUNES}}}image").get("href") == f"{BASE}/feed/cover.png"
    assert channel.findtext(f"{{{ITUNES}}}block") == "yes"
    item = channel.find("item")
    assert item.findtext("title") == "Friday, September 25"
    guid = item.find("guid")
    assert (guid.text, guid.get("isPermaLink")) == ("2026-09-25", "false")
    enc = item.find("enclosure")
    assert enc.get("url").startswith(f"{BASE}/audio/{TOKEN}/2026-09-25.mp3?v=")
    assert (enc.get("length"), enc.get("type")) == ("4", "audio/mpeg")
    assert item.findtext(f"{{{ITUNES}}}duration") == "200"
    assert item.findtext("pubDate") == "Fri, 25 Sep 2026 13:05:00 +0000"


def test_regenerate_keeps_guid_but_changes_enclosure():
    a = parse(build_feed([episode()], BASE, TOKEN)).find("item")
    b = parse(build_feed([episode("2026-09-25T15:00:00+00:00")], BASE, TOKEN)).find("item")
    assert a.findtext("guid") == b.findtext("guid")
    assert a.find("enclosure").get("url") != b.find("enclosure").get("url")


def test_describe_lists_sources():
    text = describe(script_with_words(20), [{"item_id": "a1", "source": "NPR", "title": "Big news",
                                             "url": "https://example.com/a1"}])
    assert text.startswith("Good morning.")
    assert "Big news" in text
    assert "NPR: Big news (https://example.com/a1)" in text


def deep_episode(updated_at="2026-09-25T15:00:00+00:00"):
    return {"id": 7, "title": "How the Fed Began", "published_at": "2026-09-25T14:00:00+00:00",
            "updated_at": updated_at, "audio_bytes": 9, "duration_s": 1210.6, "description": "notes"}


def test_deep_dive_feed():
    channel = parse(build_deep_dive_feed([deep_episode()], BASE, TOKEN))
    assert channel.findtext("title") == DEEP_DIVE_TITLE == "Morning Brief: Deep Dives"
    assert channel.find(f"{{{ITUNES}}}image").get("href") == f"{BASE}/feed/deep-dives-cover.png"
    assert channel.findtext(f"{{{ITUNES}}}block") == "yes"
    item = channel.find("item")
    assert item.findtext("title") == "How the Fed Began"
    assert item.findtext("guid") == "deep-dive-7"
    assert item.find("enclosure").get("url").startswith(f"{BASE}/audio/{TOKEN}/deep-dives/7.mp3?v=")
    assert item.findtext(f"{{{ITUNES}}}duration") == "1211"
    assert item.findtext("pubDate") == "Fri, 25 Sep 2026 14:00:00 +0000"


def test_describe_deep_dive_numbers_sources():
    text = describe_deep_dive({"intro": "Intro."}, [
        {"title": "Fed History", "publisher": "Federal Reserve", "url": "https://f.test"},
        {"title": "Encyclopedia", "publisher": "", "url": "https://e.test"},
    ])
    assert text.startswith("Intro.")
    assert "1. Fed History — Federal Reserve (https://f.test)" in text
    assert "2. Encyclopedia (https://e.test)" in text
