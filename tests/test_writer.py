import pytest

from morning_brief.models import Pick, Story
from morning_brief.writer import (
    SCRIPT_SCHEMA, SELECT_SCHEMA, Usage, WriterError, format_candidates, format_stories,
    script_word_count, select_stories, target_words, validate_script, write_script,
)
from tests.helpers import NOW, FakeClaude, claude_reply, make_item, script_with_words


def test_select_stories_parses_and_filters_unknown_ids():
    items = [make_item("a1"), make_item("b2", segment="local", source="MPR News")]
    fake = FakeClaude([claude_reply({"stories": [
        {"story_id": "s1", "segment": "headlines", "item_ids": ["a1", "zz"], "reason": "big"},
        {"story_id": "s2", "segment": "local", "item_ids": ["zz"], "reason": "gone"},
    ]})])
    usage = Usage()
    picks = select_stories(fake, "claude-sonnet-5", items, ["Old story"], NOW, usage)
    assert picks == [Pick("s1", "headlines", ["a1"], "big")]
    assert (usage.input_tokens, usage.output_tokens) == (1000, 200)
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["schema"] == SELECT_SCHEMA
    prompt = call["messages"][0]["content"]
    assert "Old story" in prompt
    assert "a1 | headlines | NPR | Fri Sep 25 7:00 AM | summary | Title | Summary text." in prompt


def test_format_candidates_marks_text_availability():
    full_body = make_item("a1", body=" ".join(["word"] * 200))
    full_fetch = make_item("b2", fetch_pages=True)
    summary_only = make_item("c3")
    lines = format_candidates([full_body, full_fetch, summary_only]).splitlines()
    assert "| full |" in lines[0]
    assert "| full |" in lines[1]
    assert "| summary |" in lines[2]


def test_select_stories_raises_when_nothing_usable():
    fake = FakeClaude([claude_reply({"stories": []})])
    with pytest.raises(WriterError):
        select_stories(fake, "m", [make_item()], [], NOW, Usage())


def test_select_stories_caps_at_eight():
    items = [make_item(f"a{n}") for n in range(10)]
    fake = FakeClaude([claude_reply({"stories": [
        {"story_id": f"s{n}", "segment": "headlines", "item_ids": [f"a{n}"], "reason": "r"}
        for n in range(10)
    ]})])
    picks = select_stories(fake, "m", items, [], NOW, Usage())
    assert len(picks) == 8
    assert "pick 6 to 8 stories" in fake.calls[0]["system"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_early_stop_raises(stop_reason):
    fake = FakeClaude([claude_reply({"stories": []}, stop_reason=stop_reason)])
    with pytest.raises(WriterError, match=stop_reason):
        select_stories(fake, "m", [make_item()], [], NOW, Usage())


def test_validate_script():
    assert validate_script(script_with_words(500), {"a1"}) == []
    problems = validate_script(script_with_words(100, item_ids=("zz",)), {"a1"})
    assert any("between 300 and 750" in p for p in problems)
    assert any("unknown item ids" in p for p in problems)
    empty = script_with_words(500)
    empty["segments"].append({"segment": "local", "headline": "x", "text": " ", "item_ids": ["a1"]})
    assert any("empty text" in p for p in validate_script(empty, {"a1"}))


def test_script_word_count():
    assert script_word_count(script_with_words(420)) == 420


def story():
    return Story("s1", "headlines", [make_item("a1")], "[NPR] Summary text.", False)


def test_target_words_clamps():
    def stories(full_n, summary_n):
        return ([Story(f"s{i}", "headlines", [], "", True) for i in range(full_n)]
                + [Story(f"t{i}", "headlines", [], "", False) for i in range(summary_n)])

    assert target_words(stories(8, 0)) == 650
    assert target_words(stories(1, 1)) == 450
    assert target_words(stories(5, 2)) == 505


def test_write_prompt_includes_target_and_counts():
    fake = FakeClaude([claude_reply(script_with_words(500))])
    write_script(fake, "m", [story()], NOW, Usage())
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "Target length: about 450 words (0 full-text stories, 1 summary-only)" in prompt


def test_write_script_retries_once_with_reason():
    fake = FakeClaude([claude_reply(script_with_words(120)), claude_reply(script_with_words(500))])
    usage = Usage()
    script = write_script(fake, "m", [story()], NOW, usage)
    assert script_word_count(script) == 500
    assert usage.input_tokens == 2000
    retry_prompt = fake.calls[1]["messages"][0]["content"]
    assert "rejected" in retry_prompt and "between 300 and 750" in retry_prompt
    assert "too short" in retry_prompt and "add detail drawn from the full-text stories" in retry_prompt
    assert fake.calls[1]["output_config"]["format"]["schema"] == SCRIPT_SCHEMA


def test_write_script_gives_up_after_two_rejections():
    fake = FakeClaude([claude_reply(script_with_words(120)), claude_reply(script_with_words(120))])
    with pytest.raises(WriterError, match="rejected twice"):
        write_script(fake, "m", [story()], NOW, Usage())


def test_format_stories_marks_summary_only():
    text = format_stories([story()])
    assert "(summary-only)" in text
    assert "item_ids: a1" in text
    assert "NPR Fri Sep 25 7:00 AM" in text


def test_select_prompt_uses_listener_location():
    fake = FakeClaude([claude_reply({"stories": [
        {"story_id": "s1", "segment": "local", "item_ids": ["a1"], "reason": "r"}]})])
    select_stories(fake, "m", [make_item()], [], NOW, Usage(), location="Chicago")
    system = fake.calls[0]["system"]
    assert "listener in Chicago" in system and "local news for Chicago" in system
    assert "Minneapolis" not in system
