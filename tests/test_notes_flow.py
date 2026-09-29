import json
from dataclasses import replace
from datetime import date, timedelta

from morning_brief import daily_worker, notes, personal, podcast, speech
from morning_brief.feeds import item_id
from tests.helpers import NOW, seed_episode
from tests.test_daily_worker import COUNCIL, gathered, http, script_for

TODAY = date(2026, 9, 25)


def on(settings):
    return replace(settings, personal_segment=True)


def words(n, word):
    return " ".join([word] * n)


def notes_seg(n=40, **extra):
    return {"segment": "notes", "headline": "N", "text": words(n, "note"), **extra}


def personal_seg(n=50):
    return {"segment": "personal", "headline": "M", "text": words(n, "well")}


def test_gather_personal_adds_notes_and_countdowns(conn, settings):
    a = notes.add_note(conn, "Call the dentist", None, TODAY, NOW)
    notes.add_note(conn, "Later", None, TODAY + timedelta(days=3), NOW)
    notes.add_countdown(conn, "Iceland", TODAY + timedelta(days=7), NOW)
    notes.add_countdown(conn, "Far", TODAY + timedelta(days=50), NOW)
    facts = personal.gather_personal(on(settings), http(), NOW, conn=conn)
    assert facts["notes"] == [{"id": a, "text": "Call the dentist", "url": None}]
    assert facts["countdowns"] == [{"label": "Iceland", "days": 7}]


def test_gather_personal_without_items_or_conn(conn, settings):
    assert personal.gather_personal(on(settings), http(), NOW, conn=conn) is None
    notes.add_note(conn, "x", None, TODAY, NOW)
    assert personal.gather_personal(on(settings), http(), NOW) is None  # no conn: no notes
    assert personal.gather_personal(settings, http(), NOW, conn=conn) is None  # personal segment off


def test_gather_stores_ids_and_claim_strips_them(conn, settings):
    n = notes.add_note(conn, "Bring the charger", "https://example.com/a", TODAY, NOW)
    day = gathered(conn, on(settings))
    stored = json.loads(daily_worker.get_job(conn, day)["personal_json"])
    assert stored["notes"] == [{"id": n, "text": "Bring the charger", "url": "https://example.com/a"}]
    claim = daily_worker.claim(conn, NOW, "Minneapolis")
    assert claim["personal"]["notes"] == [{"text": "Bring the charger", "url": "https://example.com/a"}]


def test_validate_notes_segment():
    known = {"a"}
    news = script_for(["a"])
    both = {**news, "segments": [personal_seg(), notes_seg(), *news["segments"]]}
    assert daily_worker.validate_submission(both, known, personal_allowed=True, notes_allowed=True) == []
    first = {**news, "segments": [notes_seg(), *news["segments"]]}
    assert daily_worker.validate_submission(first, known, notes_allowed=True) == []
    assert daily_worker.validate_submission(news, known, notes_allowed=True) == []  # optional
    assert any("no notes today" in p for p in daily_worker.validate_submission(first, known))
    order_message = "'notes' segment must come right after"
    for segs in ([notes_seg(), personal_seg()], [notes_seg(), notes_seg()]):
        script = {**news, "segments": [*segs, *news["segments"]]}
        problems = daily_worker.validate_submission(script, known, personal_allowed=True, notes_allowed=True)
        assert any(order_message in p for p in problems), segs
    late = {**news, "segments": [personal_seg(), *news["segments"], notes_seg()]}
    assert any(order_message in p
               for p in daily_worker.validate_submission(late, known, personal_allowed=True, notes_allowed=True))
    for bad in (notes_seg(10), notes_seg(401), notes_seg(item_ids=["a"]), {**notes_seg(), "text": "hi\x07 " * 30}):
        script = {**news, "segments": [bad, *news["segments"]]}
        assert any("notes" in p for p in daily_worker.validate_submission(script, known, notes_allowed=True)), bad
    edge = {**news, "segments": [notes_seg(400), *news["segments"]]}
    assert daily_worker.validate_submission(edge, known, notes_allowed=True) == []


def test_personal_rules_unchanged():
    known = {"a"}
    news = script_for(["a"])
    twice = {**news, "segments": [personal_seg(), personal_seg(), *news["segments"]]}
    assert any("must be the first segment" in p
               for p in daily_worker.validate_submission(twice, known, personal_allowed=True))


def test_notes_words_do_not_count_toward_news():
    news = script_for(["a"], words=740)
    script = {**news, "segments": [personal_seg(150), notes_seg(240), *news["segments"]]}
    assert daily_worker.validate_submission(script, {"a"}, personal_allowed=True, notes_allowed=True) == []


def test_publish_marks_notes_delivered(conn, settings):
    n = notes.add_note(conn, "Call the dentist", None, TODAY, NOW)
    s = on(settings)
    day = gathered(conn, s)
    daily_worker.claim(conn, NOW, "Minneapolis")
    news = script_for([item_id(COUNCIL)])
    assert daily_worker.accept(conn, day, {**news, "segments": [notes_seg(item_ids=None), *news["segments"]]}, NOW)
    stored = json.loads(daily_worker.get_job(conn, day)["script_json"])["segments"][0]
    assert stored == {"segment": "notes", "headline": "Your notes", "text": notes_seg()["text"], "item_ids": []}
    assert len(notes.due_notes(conn, TODAY)) == 1  # still pending while speaking
    assert daily_worker.speak_and_publish(s, day, synthesize=speech.fake_synthesize, now=lambda: NOW) == "ready"
    assert conn.execute("SELECT delivered_on FROM notes WHERE id = ?", (n,)).fetchone()[0] == day
    assert notes.due_notes(conn, TODAY) == [{"id": n, "text": "Call the dentist", "url": None}]  # still due today
    assert notes.due_notes(conn, TODAY + timedelta(days=1)) == []  # gone once the day passes


def test_publish_without_notes_segment_leaves_notes_pending(conn, settings):
    n = notes.add_note(conn, "Call the dentist", None, TODAY, NOW)
    s = on(settings)
    day = gathered(conn, s)
    daily_worker.claim(conn, NOW, "Minneapolis")
    news = script_for([item_id(COUNCIL)])  # the worker didn't write a "notes" segment
    assert daily_worker.accept(conn, day, news, NOW)
    assert daily_worker.speak_and_publish(s, day, synthesize=speech.fake_synthesize, now=lambda: NOW) == "ready"
    assert len(notes.due_notes(conn, TODAY)) == 1
    assert conn.execute("SELECT delivered_on FROM notes WHERE id = ?", (n,)).fetchone()[0] is None


def test_manual_regather_after_publish_keeps_the_note(conn, settings):
    n = notes.add_note(conn, "Call the dentist", None, TODAY, NOW)
    s = on(settings)
    day = gathered(conn, s)
    daily_worker.claim(conn, NOW, "Minneapolis")
    news = script_for([item_id(COUNCIL)])
    assert daily_worker.accept(conn, day, {**news, "segments": [notes_seg(item_ids=None), *news["segments"]]}, NOW)
    assert daily_worker.speak_and_publish(s, day, synthesize=speech.fake_synthesize, now=lambda: NOW) == "ready"
    assert conn.execute("SELECT delivered_on FROM notes WHERE id = ?", (n,)).fetchone()[0] == day
    new_day = daily_worker.gather(s, conn, http(), NOW, "manual")
    assert new_day == day
    stored = json.loads(daily_worker.get_job(conn, new_day)["personal_json"])
    assert stored["notes"] == [{"id": n, "text": "Call the dentist", "url": None}]


def test_failed_publish_keeps_notes_pending(conn, settings):
    notes.add_note(conn, "Call the dentist", None, TODAY, NOW)
    s = on(settings)
    day = gathered(conn, s)
    daily_worker.claim(conn, NOW, "Minneapolis")
    daily_worker.accept(conn, day, script_for([item_id(COUNCIL)]), NOW)

    def broken(*args):
        raise RuntimeError("espeak exploded")

    assert daily_worker.speak_and_publish(s, day, synthesize=broken, now=lambda: NOW) == "failed"
    assert len(notes.due_notes(conn, TODAY)) == 1


def test_describe_skips_notes():
    script = {"intro": "Hi.", "outro": "Bye.", "segments": [
        {"segment": "notes", "headline": "Your notes", "text": "Call the dentist.", "item_ids": []},
        {"segment": "headlines", "headline": "Big story", "text": "News.", "item_ids": []}]}
    text = podcast.describe(script, [])
    assert "dentist" not in text and "Big story" in text


def test_previous_headlines_leave_out_your_notes(conn, settings):
    seed_episode(conn, settings, "2026-09-24")
    script = script_for(["a1"])
    script["segments"].insert(0, {"segment": "notes", "headline": "Your notes", "text": "well", "item_ids": []})
    conn.execute("UPDATE episodes SET script_json = ? WHERE date = '2026-09-24'", (json.dumps(script),))
    gathered(conn, settings)
    assert daily_worker.claim(conn, NOW, "Minneapolis")["previous_headlines"] == ["Story 0"]
