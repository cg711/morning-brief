from datetime import timedelta

import pytest

from morning_brief import deepdives
from tests.helpers import NOW, deep_dive_script


def add(conn, n=1):
    return [deepdives.add_topic(conn, f"Topic {i}", "", NOW + timedelta(seconds=i)) for i in range(n)]


def test_add_topic_validates_and_orders(conn):
    a, b = add(conn, 2)
    assert [r["id"] for r in deepdives.list_topics(conn)] == [a, b]
    assert deepdives.get_topic(conn, a)["status"] == "queued"
    with pytest.raises(deepdives.TopicError, match="required"):
        deepdives.add_topic(conn, "   ", "", NOW)
    with pytest.raises(deepdives.TopicError, match="200"):
        deepdives.add_topic(conn, "x" * 201, "", NOW)
    with pytest.raises(deepdives.TopicError, match="500"):
        deepdives.add_topic(conn, "ok", "n" * 501, NOW)


def test_ids_are_never_reused(conn, settings):
    (a,) = add(conn)
    deepdives.delete_topic(conn, settings.deep_dives_dir, a)
    (b,) = add(conn)
    assert b != a


def test_move_to_top(conn):
    a, b, c = add(conn, 3)
    assert deepdives.move_to_top(conn, c, NOW) is True
    assert [r["id"] for r in deepdives.list_topics(conn)] == [c, a, b]


def test_claim_respects_three_active(conn):
    ids = add(conn, 4)
    claimed = [deepdives.claim(conn, NOW)["id"] for _ in range(3)]
    assert claimed == ids[:3]
    assert deepdives.claim(conn, NOW) is None
    assert deepdives.get_topic(conn, ids[0])["status"] == "researching"


def test_claim_returns_none_when_queue_empty(conn):
    assert deepdives.claim(conn, NOW) is None


def test_expired_claim_returns_to_queue(conn):
    (a,) = add(conn)
    deepdives.claim(conn, NOW)
    assert deepdives.release_expired_claims(conn, NOW + timedelta(hours=2, minutes=59)) == 0
    assert deepdives.release_expired_claims(conn, NOW + timedelta(hours=3, minutes=1)) == 1
    assert deepdives.get_topic(conn, a)["status"] == "queued"


def test_fail_and_retry_without_script_requeues(conn):
    (a,) = add(conn)
    deepdives.claim(conn, NOW)
    deepdives.fail(conn, a, "x" * 900, NOW)
    row = deepdives.get_topic(conn, a)
    assert row["status"] == "failed" and len(row["error"]) == 500
    assert deepdives.retry(conn, a, NOW) == "queued"
    assert deepdives.get_topic(conn, a)["error"] is None


def test_retry_with_script_respeaks(conn):
    (a,) = add(conn)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, a, SCRIPT, NOW)
    assert deepdives.get_topic(conn, a)["status"] == "speaking"
    assert [s["source_id"] for s in deepdives.sources_for(conn, a)] == ["s1", "s2"]
    deepdives.fail(conn, a, "boom", NOW)
    assert deepdives.retry(conn, a, NOW) == "speaking"
    assert deepdives.retry(conn, a, NOW) is None  # not failed any more


def test_begin_render_increments_attempts_only_while_speaking(conn):
    (a,) = add(conn)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, a, SCRIPT, NOW)
    assert deepdives.begin_render(conn, a, NOW) is True
    assert deepdives.get_topic(conn, a)["render_attempts"] == 1
    assert deepdives.begin_render(conn, a, NOW) is True
    assert deepdives.get_topic(conn, a)["render_attempts"] == 2
    (b,) = add(conn)
    assert deepdives.begin_render(conn, b, NOW) is False  # still queued


def test_retry_resets_render_attempts(conn):
    (a,) = add(conn)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, a, SCRIPT, NOW)
    deepdives.begin_render(conn, a, NOW)
    assert deepdives.get_topic(conn, a)["render_attempts"] == 1
    deepdives.fail(conn, a, "boom", NOW)
    deepdives.retry(conn, a, NOW)
    assert deepdives.get_topic(conn, a)["render_attempts"] == 0


def test_publish_mark_heard_and_delete(conn, settings):
    (a,) = add(conn)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, a, SCRIPT, NOW)
    deepdives.publish(conn, a, title="T", word_count=2400, duration_s=1200.0, audio_bytes=10, now=NOW)
    row = [r for r in deepdives.list_topics(conn) if r["id"] == a][0]
    assert (row["status"], row["episode_title"], row["published_at"]) == ("ready", "T", NOW.isoformat())
    assert deepdives.mark_heard(conn, a, NOW) is True
    assert deepdives.mark_heard(conn, a, NOW) is False
    settings.deep_dives_dir.mkdir(parents=True)
    deepdives.audio_path(settings.deep_dives_dir, a).write_bytes(b"mp3")
    assert deepdives.delete_topic(conn, settings.deep_dives_dir, a) is True
    assert deepdives.get_topic(conn, a) is None and deepdives.sources_for(conn, a) == []
    assert not deepdives.audio_path(settings.deep_dives_dir, a).exists()


def test_accept_script_false_when_topic_deleted(conn, settings):
    (a,) = add(conn)
    deepdives.claim(conn, NOW)
    deepdives.delete_topic(conn, settings.deep_dives_dir, a)
    assert deepdives.accept_script(conn, a, SCRIPT, NOW) is False


def test_accept_script_false_when_not_researching(conn):
    (a,) = add(conn)
    assert deepdives.get_topic(conn, a)["status"] == "queued"
    assert deepdives.accept_script(conn, a, SCRIPT, NOW) is False
    assert deepdives.get_topic(conn, a)["status"] == "queued"


def test_fail_from_status_only_changes_matching_status(conn):
    (a,) = add(conn)
    deepdives.claim(conn, NOW)
    deepdives.release_expired_claims(conn, NOW + timedelta(hours=4))  # back to queued
    assert deepdives.fail(conn, a, "x", NOW, from_status="researching") is False
    assert deepdives.get_topic(conn, a)["status"] == "queued"
    deepdives.claim(conn, NOW)
    assert deepdives.fail(conn, a, "x", NOW, from_status="researching") is True
    assert deepdives.get_topic(conn, a)["status"] == "failed"


def test_speaking_ids(conn):
    a, b = add(conn, 2)
    deepdives.claim(conn, NOW)
    deepdives.accept_script(conn, a, SCRIPT, NOW)
    assert deepdives.speaking_ids(conn) == [a]


def published(conn, published_at):
    (a,) = add(conn)
    deepdives.claim(conn, published_at)
    deepdives.accept_script(conn, a, SCRIPT, published_at)
    deepdives.publish(conn, a, title="T", word_count=2400, duration_s=1.0, audio_bytes=1, now=published_at)
    return a


def test_housekeeping_auto_heard_boundary(conn, settings):
    fresh = published(conn, NOW - timedelta(days=7) + timedelta(minutes=1))
    old = published(conn, NOW - timedelta(days=7) - timedelta(minutes=1))
    result = deepdives.housekeeping(conn, settings.deep_dives_dir, NOW)
    assert result["auto_heard"] == 1
    assert deepdives.get_topic(conn, fresh)["status"] == "ready"
    assert deepdives.get_topic(conn, old)["status"] == "heard"


def test_housekeeping_exactly_7_days_does_not_mark_heard(conn, settings):
    topic_id = published(conn, NOW - timedelta(days=7))
    result = deepdives.housekeeping(conn, settings.deep_dives_dir, NOW)
    assert result["auto_heard"] == 0
    assert deepdives.get_topic(conn, topic_id)["status"] == "ready"


def test_housekeeping_exactly_30_days_after_heard_does_not_delete(conn, settings):
    topic_id = published(conn, NOW - timedelta(days=40))
    deepdives.mark_heard(conn, topic_id, NOW - timedelta(days=30))
    result = deepdives.housekeeping(conn, settings.deep_dives_dir, NOW)
    assert result["deleted"] == 0
    assert deepdives.get_topic(conn, topic_id) is not None


def test_housekeeping_deletes_heard_after_30_days(conn, settings):
    keep = published(conn, NOW - timedelta(days=40))
    drop = published(conn, NOW - timedelta(days=40))
    deepdives.mark_heard(conn, keep, NOW - timedelta(days=30) + timedelta(minutes=1))
    deepdives.mark_heard(conn, drop, NOW - timedelta(days=30) - timedelta(minutes=1))
    result = deepdives.housekeeping(conn, settings.deep_dives_dir, NOW)
    assert result["deleted"] == 1
    assert deepdives.get_topic(conn, keep) is not None and deepdives.get_topic(conn, drop) is None


def test_housekeeping_releases_claims_and_stray_tmp(conn, settings):
    (a,) = add(conn)
    deepdives.claim(conn, NOW - timedelta(hours=4))
    settings.deep_dives_dir.mkdir(parents=True)
    stray = settings.deep_dives_dir / "9.mp3.tmp"
    stray.write_bytes(b"x")
    import os
    old = (NOW - timedelta(days=2)).timestamp()
    os.utime(stray, (old, old))
    result = deepdives.housekeeping(conn, settings.deep_dives_dir, NOW)
    assert result["released"] == 1 and result["stray"] == 1 and not stray.exists()


SCRIPT = {
    "title": "T", "intro": "i", "outro": "o",
    "sections": [{"heading": "h", "text": "t", "source_ids": ["s1"]}],
    "sources": [{"id": "s1", "title": "One", "publisher": "P", "url": "https://a.test/1"},
                {"id": "s2", "title": "Two", "url": "https://b.test/2"}],
}


def test_valid_script_passes():
    script = deep_dive_script(2400)
    assert deepdives.script_word_count(script) == 2400
    assert deepdives.validate_script(script) == []


def test_word_bounds():
    assert deepdives.validate_script(deep_dive_script(2000)) == []
    assert deepdives.validate_script(deep_dive_script(3000)) == []
    assert any("1999 words" in p for p in deepdives.validate_script(deep_dive_script(1999)))
    assert any("3001 words" in p for p in deepdives.validate_script(deep_dive_script(3001)))


def test_collects_every_problem():
    script = deep_dive_script(2400, sections=2, title="")
    script["sections"][0]["source_ids"] = ["nope"]
    script["sections"][1]["heading"] = " "
    script["sources"].append({"id": "s1", "title": "", "url": "ftp://x"})
    problems = deepdives.validate_script(script)
    joined = " | ".join(problems)
    for expected in ("'title' must be a non-empty string", "there are 2 sections", "unknown source ids ['nope']",
                     "section 2 needs a non-empty 'heading'", "source id 's1' is used more than once",
                     "source 3 needs an http(s) 'url'", "source 3 needs a 'title'"):
        assert expected in joined, expected


def test_rejects_non_object_and_long_title():
    assert deepdives.validate_script([1, 2]) == ["the body must be a JSON object"]
    assert any("at most 120" in p for p in deepdives.validate_script(deep_dive_script(title="x" * 121)))


def test_section_must_cite():
    script = deep_dive_script()
    script["sections"][0]["source_ids"] = []
    assert any("section 1 must cite at least one source" in p for p in deepdives.validate_script(script))


def test_passages_order():
    script = deep_dive_script(sections=3)
    out = deepdives.passages(script)
    assert out[0] == "Morning Brief deep dive: How the Fed Began."
    assert out[1] == "Welcome to the deep dive."
    assert out[2].startswith("Part 1. word")
    assert out[-1] == "Thanks for listening." and len(out) == 6


def test_control_char_in_title_is_a_problem():
    script = deep_dive_script(title="Bad\x0btitle")
    assert any("contains control characters" in p for p in deepdives.validate_script(script))


def test_lone_surrogate_in_intro_is_a_problem():
    script = deep_dive_script()
    script["intro"] = "Hello \ud800 world"
    assert any("contains control characters" in p for p in deepdives.validate_script(script))


def test_newline_and_tab_are_allowed():
    script = deep_dive_script()
    script["intro"] = "Hello\nworld\ttab"
    assert deepdives.validate_script(script) == []


def test_add_topic_rejects_control_chars(conn):
    with pytest.raises(deepdives.TopicError, match="control characters"):
        deepdives.add_topic(conn, "Bad\x0btopic", "", NOW)
    with pytest.raises(deepdives.TopicError, match="control characters"):
        deepdives.add_topic(conn, "ok", "Bad\x0bnotes", NOW)


def test_validator_never_raises_on_non_list_sections():
    """Regression: validate_script must return problems, never raise TypeError on untrusted input."""
    for sections_value in (5, True, 3.5):
        script = {"title": "t", "intro": "i", "outro": "o", "sources": [], "sections": sections_value}
        problems = deepdives.validate_script(script)
        assert isinstance(problems, list), f"Expected list of problems, got {type(problems)}"
        assert any("'sections' must be a list" in p for p in problems), f"Expected sections list error in {problems}"
