import httpx

from morning_brief.articles import RobotsCache, build_stories, item_text
from morning_brief.models import Pick
from tests.helpers import make_item

LONG = " ".join(["detail"] * 300)
PAGE = f"<html><body><article><p>{LONG}</p></article></body></html>"


def client_for(routes, calls):
    def handler(request):
        calls.append(str(request.url))
        status, body = routes.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_feed_body_used_without_fetching():
    calls = []
    client = client_for({}, calls)
    item = make_item(body=" ".join(["body"] * 200), fetch_pages=True)
    text, full = item_text(item, client, RobotsCache(client))
    assert full is True and text.split()[0] == "body"
    assert calls == []


def test_page_fetched_when_allowed():
    calls = []
    routes = {"https://news.test/robots.txt": (200, "User-agent: *\nAllow: /\n"),
              "https://news.test/story": (200, PAGE)}
    client = client_for(routes, calls)
    item = make_item(url="https://news.test/story", fetch_pages=True)
    text, full = item_text(item, client, RobotsCache(client))
    assert full is True and len(text.split()) == 300


def test_robots_disallow_falls_back_to_summary():
    calls = []
    routes = {"https://news.test/robots.txt": (200, "User-agent: *\nDisallow: /\n"),
              "https://news.test/story": (200, PAGE)}
    client = client_for(routes, calls)
    item = make_item(url="https://news.test/story", summary="Short summary.", fetch_pages=True)
    assert item_text(item, client, RobotsCache(client)) == ("Short summary.", False)
    assert "https://news.test/story" not in calls


def test_fetch_pages_false_never_fetches():
    calls = []
    client = client_for({"https://npr.test/story": (200, PAGE)}, calls)
    item = make_item(url="https://npr.test/story", summary="NPR summary.", fetch_pages=False)
    assert item_text(item, client, RobotsCache(client)) == ("NPR summary.", False)
    assert calls == []


def test_blocked_page_falls_back_to_summary():
    calls = []
    client = client_for({"https://news.test/story": (402, "pay up")}, calls)
    item = make_item(url="https://news.test/story", summary="Summary.", fetch_pages=True)
    assert item_text(item, client, RobotsCache(client)) == ("Summary.", False)


def test_build_stories_splits_word_budget_and_skips_unknown_ids():
    calls = []
    client = client_for({}, calls)
    a = make_item("a1", source="MPR News", body=" ".join(["alpha"] * 1000))
    b = make_item("b2", source="Sahan Journal", body=" ".join(["beta"] * 1000))
    picks = [Pick("s1", "local", ["a1", "b2", "zz"], "r"), Pick("s2", "local", ["zz"], "r")]
    stories = build_stories(picks, {"a1": a, "b2": b}, client, RobotsCache(client))
    assert len(stories) == 1
    s = stories[0]
    assert [i.id for i in s.items] == ["a1", "b2"] and s.full is True
    assert s.text.count("alpha") == 400 and s.text.count("beta") == 400
    assert s.text.startswith("[MPR News] ")
