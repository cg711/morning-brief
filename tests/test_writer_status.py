import json
from datetime import timedelta

import pytest

from morning_brief import daily_worker, db, deepdives, writer_status
from morning_brief.writer_status import DETAIL_MAX, Report, apply_report, parse_report, status_line, work
from tests.helpers import NOW
from tests.test_daily_worker import gathered

STARTED = NOW - timedelta(minutes=1)


class Pushes(list):
    def __call__(self, settings, title, message, tags):
        self.append((title, message, tags))


@pytest.fixture
def pushes():
    return Pushes()


def report(outcome="failed", kind="deep_dive", started=STARTED, detail=""):
    return Report(kind=kind, outcome=outcome, started_at=started, detail=detail)


def claimed_topic(conn, title="Alpha", attempts=0):
    topic_id = deepdives.add_topic(conn, title, "", NOW - timedelta(minutes=5))
    deepdives.claim(conn, NOW)
    conn.execute("UPDATE topics SET claim_attempts = ? WHERE id = ?", (attempts, topic_id))
    return topic_id


def test_constants():
    assert writer_status.OUTCOMES == ("running", "ok", "failed", "timeout", "auth", "limit")
    assert writer_status.KINDS == ("daily", "deep_dive", "idle")
    assert DETAIL_MAX == 200


def test_parse_report_accepts_and_cleans():
    parsed = parse_report({"kind": "daily", "outcome": "ok", "started_at": "2026-09-25T12:59:00Z",
                           "detail": "fine\x00\x07 text\nsecond\x1b[31m " + "x" * 400})
    assert isinstance(parsed, Report) and parsed.kind == "daily" and parsed.outcome == "ok"
    assert parsed.started_at == NOW - timedelta(minutes=1)
    assert "\x00" not in parsed.detail and "\x1b" not in parsed.detail and "\n" not in parsed.detail
    assert parsed.detail.startswith("fine text second") and len(parsed.detail) == DETAIL_MAX
    minimal = parse_report({"kind": "idle", "outcome": "running"})
    assert minimal == Report(kind="idle", outcome="running", started_at=None, detail="")


@pytest.mark.parametrize("body", [
    None, [], "text", 5, {}, {"kind": "daily"}, {"outcome": "ok"},
    {"kind": "nope", "outcome": "ok"}, {"kind": "daily", "outcome": "nope"},
    {"kind": 1, "outcome": "ok"}, {"kind": "daily", "outcome": ["ok"]},
    {"kind": "daily", "outcome": "ok", "started_at": "yesterday"},
    {"kind": "daily", "outcome": "ok", "started_at": 12345},
    {"kind": "daily", "outcome": "ok", "started_at": "2026-09-25T12:59:00"},  # no offset
    {"kind": "daily", "outcome": "ok", "detail": 7},
    {"kind": "daily", "outcome": "ok", "detail": {"a": 1}},
])
def test_parse_report_problems(body):
    problems = parse_report(body)
    assert isinstance(problems, list) and problems and all(isinstance(p, str) for p in problems)


def test_work_counts(conn):
    assert work(conn, NOW) == {"daily": False, "deep_dives": 0}
    deepdives.add_topic(conn, "A", "", NOW)
    deepdives.add_topic(conn, "B", "", NOW)
    assert work(conn, NOW) == {"daily": False, "deep_dives": 2}


def test_failing_outcomes_release_and_count(conn, settings, pushes):
    first = claimed_topic(conn)
    apply_report(conn, settings, report("failed"), NOW, pushes)
    row = deepdives.get_topic(conn, first)
    assert (row["status"], row["claim_attempts"]) == ("queued", 1)
    deepdives.claim(conn, NOW)
    apply_report(conn, settings, report("timeout"), NOW, pushes)
    assert deepdives.get_topic(conn, first)["claim_attempts"] == 2
    for outcome in ("auth", "limit"):
        deepdives.claim(conn, NOW)
        apply_report(conn, settings, report(outcome), NOW, pushes)
        row = deepdives.get_topic(conn, first)
        assert (row["status"], row["claim_attempts"]) == ("queued", 2)


def test_daily_job_is_released_by_a_failing_report(conn, settings, pushes):
    date = gathered(conn, settings)
    assert daily_worker.claim(conn, NOW, "Minneapolis") is not None
    apply_report(conn, settings, report("limit", kind="daily"), NOW, pushes)
    job = daily_worker.get_job(conn, date)
    assert (job["status"], job["attempts"]) == ("waiting", 0)


def test_running_and_idle_reports_release_nothing(conn, settings, pushes):
    topic_id = claimed_topic(conn)
    for outcome, kind in (("running", "deep_dive"), ("running", "idle"), ("ok", "idle"), ("failed", "idle")):
        apply_report(conn, settings, report(outcome, kind=kind), NOW, pushes)
    assert deepdives.get_topic(conn, topic_id)["status"] == "researching"
    assert deepdives.get_topic(conn, topic_id)["claim_attempts"] == 0


def test_a_failed_report_releases_only_its_own_kind(conn, settings, pushes):
    topic_id = claimed_topic(conn)
    date = gathered(conn, settings)
    assert daily_worker.claim(conn, NOW, "Minneapolis") is not None
    apply_report(conn, settings, report("failed", kind="daily"), NOW, pushes)
    assert daily_worker.get_job(conn, date)["status"] == "waiting"
    assert deepdives.get_topic(conn, topic_id)["status"] == "researching"   # the Mac's topic claim stays
    assert daily_worker.claim(conn, NOW, "Minneapolis") is not None
    apply_report(conn, settings, report("failed", kind="deep_dive"), NOW, pushes)
    assert deepdives.get_topic(conn, topic_id)["status"] == "queued"
    assert daily_worker.get_job(conn, date)["status"] == "claimed"          # and the Mac's daily claim


def test_a_failed_report_releases_only_the_earliest_claim_since_it_started(conn, settings, pushes):
    first = deepdives.add_topic(conn, "First", "", NOW - timedelta(minutes=5))
    second = deepdives.add_topic(conn, "Second", "", NOW - timedelta(minutes=4))
    deepdives.claim(conn, NOW)                          # the writer's
    deepdives.claim(conn, NOW + timedelta(seconds=30))  # the Mac's, later in the same window
    apply_report(conn, settings, report("failed"), NOW + timedelta(minutes=1), pushes)
    assert deepdives.get_topic(conn, first)["status"] == "queued"
    assert deepdives.get_topic(conn, second)["status"] == "researching"


# An "ok" that left its item claimed never submitted it, so the run is a failure and the item is not stuck for good.

def test_ok_with_a_deep_dive_still_researching_is_a_counted_failure(conn, settings, pushes):
    topic_id = claimed_topic(conn)
    apply_report(conn, settings, report("ok"), NOW, pushes)
    row = deepdives.get_topic(conn, topic_id)
    assert (row["status"], row["claim_attempts"]) == ("queued", 1)
    stored = json.loads(db.get_state(conn, "writer_last_report"))
    assert stored["outcome"] == "failed" and stored["detail"] == "the run ended without submitting"
    assert db.get_state(conn, "writer_last_ok") is None
    assert db.get_state(conn, "writer_fail_streak") == "1"


def test_ok_with_the_daily_job_still_claimed_is_a_counted_failure(conn, settings, pushes):
    date = gathered(conn, settings)
    assert daily_worker.claim(conn, NOW, "Minneapolis") is not None
    apply_report(conn, settings, report("ok", kind="daily"), NOW, pushes)
    job = daily_worker.get_job(conn, date)
    assert (job["status"], job["attempts"]) == ("waiting", 1)
    assert db.get_state(conn, "writer_last_ok") is None and db.get_state(conn, "writer_fail_streak") == "1"
    assert json.loads(db.get_state(conn, "writer_last_report"))["outcome"] == "failed"


def test_repeated_unfinished_ok_runs_give_up_and_push(conn, settings, pushes):
    topic_id = claimed_topic(conn, "Stuck topic", attempts=2)
    apply_report(conn, settings, report("ok"), NOW, pushes)
    assert deepdives.get_topic(conn, topic_id)["status"] == "failed"
    assert [p[0] for p in pushes] == ["A deep dive failed after 3 tries: Stuck topic"]


def test_a_normal_ok_leaves_a_submitted_item_alone(conn, settings, pushes):
    topic_id = claimed_topic(conn)
    conn.execute("UPDATE topics SET status = 'speaking' WHERE id = ?", (topic_id,))
    date = gathered(conn, settings)
    daily_worker.claim(conn, NOW, "Minneapolis")
    conn.execute("UPDATE daily_jobs SET status = 'speaking' WHERE date = ?", (date,))
    for kind in ("deep_dive", "daily"):
        apply_report(conn, settings, report("ok", kind=kind), NOW, pushes)
    assert deepdives.get_topic(conn, topic_id)["status"] == "speaking"
    assert daily_worker.get_job(conn, date)["status"] == "speaking"
    stored = json.loads(db.get_state(conn, "writer_last_report"))
    assert stored["outcome"] == "ok" and db.get_state(conn, "writer_last_ok") == NOW.isoformat()
    assert db.get_state(conn, "writer_fail_streak") == "0" and pushes == []


def test_an_ok_with_nothing_claimed_is_just_ok(conn, settings, pushes):
    apply_report(conn, settings, report("ok", kind="daily"), NOW, pushes)
    apply_report(conn, settings, report("ok", kind="deep_dive"), NOW, pushes)
    assert json.loads(db.get_state(conn, "writer_last_report"))["outcome"] == "ok"


def test_report_is_stored_as_json(conn, settings, pushes):
    apply_report(conn, settings, report("running", detail="hello"), NOW, pushes)
    stored = json.loads(db.get_state(conn, "writer_last_report"))
    assert stored["outcome"] == "running" and stored["detail"] == "hello" and stored["kind"] == "deep_dive"
    assert stored["started_at"] == STARTED.isoformat() and stored["at"] == NOW.isoformat()


def test_pushes_once_per_problem_change(conn, settings, pushes):
    for _ in range(3):
        apply_report(conn, settings, report("auth", kind="idle"), NOW, pushes)
    assert [p[0] for p in pushes] == ["Writer needs you to sign in again"]
    assert "claude setup-token" in pushes[0][1] and "writer.env" in pushes[0][1]
    assert "docker compose --profile writer up -d writer" in pushes[0][1]
    assert "restart" not in pushes[0][1]            # a restart would not re-read env_file
    apply_report(conn, settings, report("running"), NOW, pushes)  # a retried job's check-in proves nothing
    apply_report(conn, settings, report("auth", kind="idle"), NOW, pushes)
    assert len(pushes) == 1
    apply_report(conn, settings, report("ok", kind="idle"), NOW, pushes)
    apply_report(conn, settings, report("auth", kind="idle"), NOW, pushes)
    assert [p[0] for p in pushes] == ["Writer needs you to sign in again"] * 2
    apply_report(conn, settings, report("limit", kind="idle"), NOW, pushes)
    apply_report(conn, settings, report("limit", kind="idle"), NOW, pushes)
    assert [p[0] for p in pushes][2:] == ["Writer hit the usage limit; it will retry on its own"]


def test_three_failures_push_once_and_ok_resets_the_streak(conn, settings, pushes):
    for _ in range(2):
        apply_report(conn, settings, report("failed", kind="idle", detail="boom"), NOW, pushes)
    assert pushes == []
    apply_report(conn, settings, report("timeout", kind="idle", detail="took too long"), NOW, pushes)
    assert [p[0] for p in pushes] == ["Writer failed 3 times in a row"]
    assert "took too long" not in pushes[0][1]      # Claude's own words stay on the page, never in a push
    assert pushes[0][1] == "Open the app to see what it reported."
    for _ in range(3):
        apply_report(conn, settings, report("failed", kind="idle"), NOW, pushes)
    assert len(pushes) == 1
    apply_report(conn, settings, report("ok", kind="idle"), NOW, pushes)
    for _ in range(2):
        apply_report(conn, settings, report("failed", kind="idle"), NOW, pushes)
    assert len(pushes) == 1  # the streak started over
    apply_report(conn, settings, report("failed", kind="idle"), NOW, pushes)
    assert len(pushes) == 2


def test_running_between_failures_keeps_the_streak_and_auth_resets_it(conn, settings, pushes):
    apply_report(conn, settings, report("failed", kind="idle"), NOW, pushes)
    apply_report(conn, settings, report("running", kind="idle"), NOW, pushes)
    apply_report(conn, settings, report("failed", kind="idle"), NOW, pushes)
    apply_report(conn, settings, report("auth", kind="idle"), NOW, pushes)
    assert [p[0] for p in pushes] == ["Writer needs you to sign in again"]
    apply_report(conn, settings, report("failed", kind="idle"), NOW, pushes)
    apply_report(conn, settings, report("failed", kind="idle"), NOW, pushes)
    assert len(pushes) == 1  # auth reset the streak: only two failures since


def test_giving_up_pushes_once_per_item(conn, settings, pushes):
    long_title = "T" * 100
    first = claimed_topic(conn, long_title, attempts=2)
    second = deepdives.add_topic(conn, "Beta", "", NOW - timedelta(minutes=4))
    deepdives.claim(conn, NOW + timedelta(seconds=1))
    conn.execute("UPDATE topics SET claim_attempts = 2 WHERE id = ?", (second,))
    date = gathered(conn, settings)
    assert daily_worker.claim(conn, NOW, "Minneapolis") is not None
    conn.execute("UPDATE daily_jobs SET attempts = 2 WHERE date = ?", (date,))
    apply_report(conn, settings, report("failed", kind="daily"), NOW, pushes)
    assert daily_worker.get_job(conn, date)["status"] == "failed"
    apply_report(conn, settings, report("failed"), NOW, pushes)    # one deep-dive run, one claim
    assert deepdives.get_topic(conn, first)["status"] == "failed"
    assert deepdives.get_topic(conn, second)["status"] == "researching"
    apply_report(conn, settings, report("failed"), NOW, pushes)
    assert deepdives.get_topic(conn, second)["status"] == "failed"
    pushes[:] = [p for p in pushes if "3 times in a row" not in p[0]]   # the third failure also trips the streak
    titles = [p[0] for p in pushes]
    assert titles.count("Today's brief failed after 3 tries") == 1
    assert f"A deep dive failed after 3 tries: {'T' * 60}" in titles
    assert "A deep dive failed after 3 tries: Beta" in titles
    assert not any("T" * 61 in t for t in titles)
    assert len(pushes) == 3  # nothing else: no streak push
    apply_report(conn, settings, report("failed"), NOW, pushes)
    assert len(pushes) == 3


def test_a_failing_push_does_not_break_the_report(conn, settings):
    def boom(*args):
        raise RuntimeError("ntfy down")

    for _ in range(3):
        apply_report(conn, settings, report("failed", kind="idle"), NOW, boom)
    assert db.get_state(conn, "writer_last_report") is not None


def test_status_line_texts(conn, settings, pushes):
    from morning_brief.deepdives import worker_status

    def seen(delta):
        db.set_state(conn, "worker_last_seen", (NOW - delta).isoformat())

    def polled(delta):
        db.set_state(conn, "writer_last_poll", (NOW - delta).isoformat())

    # no report and no writer poll yet: the Mac wording
    assert status_line(conn, NOW) == worker_status(None, NOW)
    seen(timedelta(minutes=14))
    assert status_line(conn, NOW) == {"text": "Worker last checked in 14 min ago", "stale": False}

    # healthy; 7:36 AM Central is 12:36 UTC
    ok_at = NOW - timedelta(minutes=24)
    apply_report(conn, settings, report("ok", started=ok_at - timedelta(minutes=5)), ok_at, pushes)
    polled(timedelta(seconds=20))
    assert status_line(conn, NOW) == {"text": "Writer: checked in 20 s ago · last run OK 7:36 AM", "stale": False}
    polled(timedelta(minutes=3))
    assert status_line(conn, NOW)["text"] == "Writer: checked in 3 min ago · last run OK 7:36 AM"
    assert status_line(conn, NOW)["stale"] is False

    # stale: nothing for 5 minutes
    polled(timedelta(minutes=5, seconds=1))
    line = status_line(conn, NOW)
    assert line["stale"] is True and "checked in 5 min ago" in line["text"]

    polled(timedelta(seconds=20))
    apply_report(conn, settings, report("auth", kind="idle"), NOW - timedelta(seconds=20), pushes)
    line = status_line(conn, NOW)
    assert line["stale"] is True and line["text"].startswith("Writer: needs you to sign in again")
    assert "checked in 20 s ago" in line["text"]

    apply_report(conn, settings, report("limit", kind="idle"), NOW, pushes)
    line = status_line(conn, NOW)
    assert line["stale"] is True and line["text"].startswith("Writer: hit the usage limit")

    for _ in range(3):
        apply_report(conn, settings, report("failed", kind="idle", detail="claude exited 1"), NOW, pushes)
    line = status_line(conn, NOW)
    assert line["stale"] is True and line["text"].startswith("Writer: failing, 3 runs in a row")
    assert "claude exited 1" in line["text"]

    apply_report(conn, settings, report("ok", kind="idle"), NOW, pushes)
    assert status_line(conn, NOW)["stale"] is False


@pytest.mark.parametrize("outcome", ["ok", "failed", "timeout", "limit", "running"])
def test_the_auth_problem_clears_on_the_next_report_of_any_other_outcome(conn, settings, pushes, outcome):
    apply_report(conn, settings, report("auth", kind="idle"), NOW, pushes)
    assert status_line(conn, NOW)["text"].startswith("Writer: needs you to sign in again")
    apply_report(conn, settings, report(outcome, kind="idle"), NOW, pushes)
    assert not status_line(conn, NOW)["text"].startswith("Writer: needs you to sign in again")
    assert db.get_state(conn, "writer_problem") != "auth"


def test_a_retried_jobs_running_check_in_does_not_clear_auth_or_repeat_the_push(conn, settings, pushes):
    apply_report(conn, settings, report("auth", kind="daily"), NOW, pushes)
    apply_report(conn, settings, report("running", kind="daily"), NOW, pushes)
    apply_report(conn, settings, report("auth", kind="daily"), NOW, pushes)
    assert db.get_state(conn, "writer_problem") == "auth" and len(pushes) == 1


def test_status_line_uses_the_writer_wording_from_its_first_poll(conn):
    db.set_state(conn, "worker_last_seen", NOW.isoformat())                  # the Mac's stamp must not be used
    db.set_state(conn, "writer_last_poll", (NOW - timedelta(seconds=12)).isoformat())
    assert status_line(conn, NOW) == {"text": "Writer: checked in 12 s ago", "stale": False}
    db.set_state(conn, "writer_last_poll", (NOW - timedelta(minutes=6)).isoformat())
    line = status_line(conn, NOW)
    assert line["text"] == "Writer: checked in 6 min ago" and line["stale"] is True


def test_status_line_checked_in_follows_the_writers_poll_not_the_macs_stamp(conn, settings, pushes):
    apply_report(conn, settings, report("ok"), NOW - timedelta(minutes=20), pushes)
    db.set_state(conn, "writer_last_poll", (NOW - timedelta(seconds=30)).isoformat())
    db.set_state(conn, "worker_last_seen", NOW.isoformat())
    assert "checked in 30 s ago" in status_line(conn, NOW)["text"]


def test_a_long_run_stays_fresh_on_its_running_reports(conn, settings, pushes):
    db.set_state(conn, "writer_last_poll", (NOW - timedelta(minutes=20)).isoformat())   # polling stops while Claude runs
    apply_report(conn, settings, report("running"), NOW - timedelta(seconds=10), pushes)
    db.set_state(conn, "worker_last_seen", NOW.isoformat())
    line = status_line(conn, NOW)
    assert line == {"text": "Writer: checked in 10 s ago", "stale": False}


def test_a_stale_report_does_not_hide_a_fresh_poll_or_the_reverse(conn, settings, pushes):
    apply_report(conn, settings, report("ok"), NOW - timedelta(minutes=30), pushes)
    db.set_state(conn, "writer_last_poll", (NOW - timedelta(seconds=5)).isoformat())
    assert "checked in 5 s ago" in status_line(conn, NOW)["text"]
    db.set_state(conn, "writer_last_poll", (NOW - timedelta(minutes=30)).isoformat())
    apply_report(conn, settings, report("running"), NOW - timedelta(seconds=40), pushes)
    assert "checked in 40 s ago" in status_line(conn, NOW)["text"]
