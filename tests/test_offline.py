import httpx

from morning_brief import db, pipeline, speech, writer
from morning_brief.config import Settings
from morning_brief.models import Story
from morning_brief.offline import OfflineClaude
from tests.helpers import NOW, make_item

BODY = " ".join(["detail"] * 200)


def test_select_prefers_full_text_and_respects_quotas():
    items = ([make_item(f"h{n}", segment="headlines", source="NPR") for n in range(3)]
             + [make_item(f"b{n}", segment="headlines", source="BBC", body=BODY) for n in range(2)]
             + [make_item("l1", segment="local", source="MPR News", body=BODY)])
    picks = writer.select_stories(OfflineClaude(), "m", items, [], NOW, writer.Usage())
    assert [p.item_ids[0] for p in picks if p.segment == "headlines"] == ["b0", "b1", "h0"]
    assert [p.item_ids for p in picks if p.segment == "local"] == [["l1"]]


def test_write_produces_valid_script_near_target():
    stories = [Story(f"s{n}", "headlines", [make_item(f"a{n}", source="BBC", body=BODY)], f"[BBC] {BODY}", True)
               for n in range(4)]
    usage = writer.Usage()
    script = writer.write_script(OfflineClaude(), "m", stories, NOW, usage)
    assert writer.validate_script(script, {f"a{n}" for n in range(4)}) == []
    assert 400 <= writer.script_word_count(script) <= 650
    assert script["segments"][0]["text"].startswith("BBC reports: detail")
    assert (usage.input_tokens, usage.output_tokens) == (0, 0)


def test_offline_pipeline_end_to_end(conn, settings):
    items = "".join(
        f"<item><title>Story {n}</title><link>https://wire.test/{n}</link>"
        f"<pubDate>Fri, 25 Sep 2026 12:{n:02d}:00 +0000</pubDate><description>S{n}.</description>"
        f"<content:encoded><![CDATA[{BODY}]]></content:encoded></item>"
        for n in range(4)
    )
    rss = ('<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
           f"<channel><title>x</title>{items}</channel></rss>")

    def handler(request):
        return httpx.Response(200, text=rss) if request.url.host == "wire.test" else httpx.Response(404)

    deps = pipeline.Deps(settings=settings, claude=OfflineClaude(),
                         http=httpx.Client(transport=httpx.MockTransport(handler)),
                         synthesize=speech.fake_synthesize, now=lambda: NOW)
    run_id = pipeline.run_episode(deps, trigger="cli")
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "succeeded", run["error"]
    assert db.get_episode(conn, "2026-09-25")["word_count"] >= 300


def test_claude_offline_setting_selects_offline_client(tmp_path):
    s = Settings.from_env({"FEED_TOKEN": "x" * 32, "DATA_DIR": str(tmp_path), "CLAUDE_OFFLINE": "1"})
    deps = pipeline.default_deps(s)
    try:
        assert s.claude_offline is True and isinstance(deps.claude, OfflineClaude)
    finally:
        deps.http.close()
