"""The server-side writer's check-ins: what work is waiting, what it reports back, and the status line.

The writer container polls GET /api/work and POSTs a report after each run (or every minute while one runs).
A report that ends a run badly (or an "ok" that left its item unfinished) releases what the run had claimed; problems (signed out, usage limit, repeated
failures) push once when they begin and clear on the next ok."""
from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from . import daily_worker, db, deepdives
from .config import TZ

log = logging.getLogger(__name__)

OUTCOMES = ("running", "ok", "failed", "timeout", "auth", "limit")
KINDS = ("daily", "deep_dive", "idle")
DETAIL_MAX = 200
REPORT_MAX_BYTES = 4096
FAIL_STREAK_PUSH = 3
STALE_AFTER_S = 300
TITLE_MAX = 60
COUNTED = ("failed", "timeout")           # outcomes that burn an attempt on whatever the run had claimed
RELEASING = ("failed", "timeout", "auth", "limit")

_LAST_REPORT, _LAST_OK, LAST_POLL = "writer_last_report", "writer_last_ok", "writer_last_poll"
_PROBLEM, _STREAK = "writer_problem", "writer_fail_streak"  # problem is '', 'auth', 'limit' or 'failing'


@dataclass(frozen=True)
class Report:
    kind: str
    outcome: str
    started_at: datetime | None
    detail: str


def _clean(text: str) -> str:
    """No control, format or surrogate characters (newlines and tabs become spaces), cut to DETAIL_MAX."""
    out = []
    for ch in text:
        if ch in "\t\n\r":
            out.append(" ")
        elif unicodedata.category(ch) not in ("Cc", "Cf", "Cs"):
            out.append(ch)
    return "".join(out)[:DETAIL_MAX].strip()


def parse_report(body: object) -> Report | list[str]:
    """A Report, or the list of problems that make the body invalid."""
    if not isinstance(body, dict):
        return ["the body must be a JSON object"]
    problems = []
    kind, outcome = body.get("kind"), body.get("outcome")
    if kind not in KINDS or not isinstance(kind, str):
        problems.append(f"kind must be one of {', '.join(KINDS)}")
    if outcome not in OUTCOMES or not isinstance(outcome, str):
        problems.append(f"outcome must be one of {', '.join(OUTCOMES)}")
    started_at = None
    if body.get("started_at") is not None:
        raw = body["started_at"]
        try:
            if not isinstance(raw, str):
                raise ValueError
            started_at = datetime.fromisoformat(raw)
            if started_at.tzinfo is None or started_at.utcoffset() is None:
                raise ValueError
        except ValueError:
            started_at = None
            problems.append("started_at must be an ISO 8601 time with a UTC offset")
    detail = body.get("detail", "")
    if detail is None:
        detail = ""
    if not isinstance(detail, str):
        problems.append("detail must be a string")
        detail = ""
    if problems:
        return problems
    return Report(kind=kind, outcome=outcome, started_at=started_at, detail=_clean(detail))


def work(conn, now: datetime) -> dict:
    """What a claim would hand out right now. Read-only."""
    return {"daily": daily_worker.work_available(conn, now), "deep_dives": deepdives.claimable_count(conn, now)}


def _push(notify, settings, title: str, message: str) -> None:
    try:
        notify(settings, title, message, ["warning"])
    except Exception:
        log.exception("writer push failed: %s", title)


def _titles(conn, ids: list[int]) -> dict[int, str]:
    return {row["id"]: row["topic"] for row in conn.execute(
        f"SELECT id, topic FROM topics WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}


def _release(conn, report: Report, now: datetime, *, count: bool) -> tuple[str | None, dict]:
    """Put back what this run claimed: only its own kind's queue, and only its earliest claim since it started. An
    idle report (or one without started_at) owns nothing."""
    none = {"released": [], "failed": []}
    if report.started_at is None:
        return None, none
    if report.kind == "daily":
        return daily_worker.release_run(conn, report.started_at, now, count_attempt=count), none
    if report.kind == "deep_dive":
        return None, deepdives.release_run(conn, report.started_at, now, count_attempt=count)
    return None, none


def apply_report(conn, settings, report: Report, now: datetime, notify) -> None:
    """Store the report, release a failed run's claim, and push once per change of problem."""
    outcome, detail = report.outcome, report.detail
    daily, dd = None, {"released": [], "failed": []}
    if outcome == "ok":
        # A run that submitted has its item in speaking, one that called fail has it failed: anything of this run's
        # kind still claimed was never finished, whatever claude's exit status said.
        daily, dd = _release(conn, report, now, count=True)
        if daily or dd["released"] or dd["failed"]:
            outcome, detail = "failed", "the run ended without submitting"
    elif outcome in RELEASING:
        daily, dd = _release(conn, report, now, count=outcome in COUNTED)

    db.set_state(conn, _LAST_REPORT, json.dumps({
        "kind": report.kind, "outcome": outcome, "detail": detail, "at": now.isoformat(),
        "started_at": report.started_at.isoformat() if report.started_at else None}))
    problem = db.get_state(conn, _PROBLEM) or ""
    streak = int(db.get_state(conn, _STREAK) or 0)
    pushes: list[tuple[str, str]] = []

    if problem == "auth" and outcome != "auth" and (outcome != "running" or report.kind == "idle"):
        # The sign-in problem is over once the writer reports anything else: it was fixed (token edited, container
        # recreated) or it got far enough to fail some other way. A mid-run check-in of a retried job proves nothing,
        # so only the writer's start-up check-in (kind idle) clears it on a bare "running".
        problem = ""
    if outcome == "ok":
        problem, streak = "", 0
        db.set_state(conn, _LAST_OK, now.isoformat())
    elif outcome in ("auth", "limit"):
        streak = 0
        if problem != outcome:
            problem = outcome
            pushes.append(("Writer needs you to sign in again",
                           "Run `claude setup-token`, put the new token in writer.env, then run "
                           "`docker compose --profile writer up -d writer`.")
                          if problem == "auth" else
                          ("Writer hit the usage limit; it will retry on its own",
                           "Nothing is lost: the work stays queued until the limit resets."))
    elif outcome in COUNTED:
        if problem == "limit":
            problem = ""  # the run got far enough to fail some other way
        streak += 1
        if streak >= FAIL_STREAK_PUSH and problem != "failing":
            problem = "failing"
            # fixed text: the detail is Claude's own words and stays on the page, never in a push
            pushes.append(("Writer failed 3 times in a row", "Open the app to see what it reported."))

    if daily == "failed":
        pushes.append(("Today's brief failed after 3 tries", deepdives.GAVE_UP))
    titles = _titles(conn, dd["failed"])
    for topic_id in dd["failed"]:
        pushes.append((f"A deep dive failed after 3 tries: {titles.get(topic_id, 'a topic')[:TITLE_MAX]}",
                       deepdives.GAVE_UP))

    db.set_state(conn, _PROBLEM, problem)
    db.set_state(conn, _STREAK, str(streak))
    for title, message in pushes:
        _push(notify, settings, title, message)


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _local_clock(iso: str, now: datetime) -> str:
    local = datetime.fromisoformat(iso).astimezone(TZ)
    label = local.strftime("%-I:%M %p")
    return label if local.date() == now.astimezone(TZ).date() else f"{local.strftime('%b %-d')}, {label}"


def status_line(conn, now: datetime) -> dict:
    """{'text', 'stale'}: the writer's line once it has polled or reported, else the Mac worker's."""
    last_seen = db.get_state(conn, "worker_last_seen")
    raw = db.get_state(conn, _LAST_REPORT)
    polled = db.get_state(conn, LAST_POLL)
    if raw is None and polled is None:
        return deepdives.worker_status(last_seen, now)
    last = json.loads(raw) if raw else {}
    # "checked in" is the writer's own activity: its polls and its reports (during a long run it only reports, every
    # minute). worker_last_seen is stamped by the Mac too, so it is never used here.
    seen = max(datetime.fromisoformat(t) for t in (polled, last.get("at")) if t)
    age = (now - seen).total_seconds()
    checked = f"checked in {_ago(age)}"
    problem = db.get_state(conn, _PROBLEM) or ""
    if problem == "auth":
        text = f"Writer: needs you to sign in again · {checked}"
    elif problem == "limit":
        text = f"Writer: hit the usage limit, retrying on its own · {checked}"
    elif problem == "failing":
        streak = int(db.get_state(conn, _STREAK) or 0)
        detail = f": {last['detail']}" if last.get("detail") and last.get("outcome") in COUNTED else ""
        text = f"Writer: failing, {streak} runs in a row{detail} · {checked}"
    else:
        text = f"Writer: {checked}"
        ok = db.get_state(conn, _LAST_OK)
        if ok:
            text += f" · last run OK {_local_clock(ok, now)}"
    return {"text": text, "stale": bool(problem) or age > STALE_AFTER_S}
