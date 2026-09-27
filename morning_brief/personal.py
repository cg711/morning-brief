"""Personal morning facts for the daily brief: last night's sleep and today's tip from the Oura dashboard, and
yesterday's spending from Actual Budget. Every source is optional and fails quietly — nothing here may fail a brief.
Secrets are never logged: failures log only the exception type.
"""
from __future__ import annotations

import argparse
import logging
import math
import ssl
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, Iterable

import httpx

from .config import TZ, Settings

log = logging.getLogger(__name__)
OURA_TIMEOUT_S = 10
TIP_TITLE_MAX, TIP_DETAIL_MAX = 120, 300


def _cut(text, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.") + "…"


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def oura_facts(http, base_url: str, today: date) -> dict | None:
    """Last night's sleep (only if the dashboard has today's) and the top insight, from GET /api/state."""
    try:
        response = http.get(f"{base_url.rstrip('/')}/api/state", timeout=OURA_TIMEOUT_S)
        response.raise_for_status()
        state = response.json()
    except Exception as exc:
        log.warning("oura dashboard unavailable: %s", type(exc).__name__)
        return None
    if not isinstance(state, dict):
        return None
    status = state.get("status")
    if isinstance(status, dict) and status.get("demo"):
        return None
    facts: dict = {}
    day = state.get("today")
    if isinstance(day, dict) and day.get("day") == today.isoformat() and _number(day.get("sleep_score")) is not None:
        contributors = day.get("readiness_contributors") if isinstance(day.get("readiness_contributors"), dict) else {}
        hours, readiness, hrv = _number(day.get("total_sleep_h")), _number(day.get("readiness_score")), \
            _number(contributors.get("hrv_balance"))
        facts["sleep"] = {"sleep_score": round(day["sleep_score"]),
                          "hours": round(hours, 1) if hours is not None else None,
                          "readiness": round(readiness) if readiness is not None else None,
                          "hrv_balance": round(hrv) if hrv is not None else None}
    insights = state.get("insights")
    if isinstance(insights, list) and insights and isinstance(insights[0], dict) and insights[0].get("title"):
        facts["tip"] = {"title": _cut(insights[0]["title"], TIP_TITLE_MAX),
                        "detail": _cut(insights[0].get("detail") or "", TIP_DETAIL_MAX)}
    return facts or None


@dataclass(frozen=True)
class Txn:
    amount: Decimal  # dollars; negative = money out
    payee: str | None
    is_transfer: bool = False
    is_parent: bool = False


def summarize(transactions: Iterable[Txn]) -> dict:
    """Yesterday's purchases: outflows only — no income, transfers between accounts, or split parents."""
    outflows = [t for t in transactions if t.amount < 0 and not t.is_transfer and not t.is_parent]
    if not outflows:
        return {"total": 0.0, "count": 0, "biggest": None}
    biggest = min(outflows, key=lambda t: t.amount)
    return {"total": float(round(-sum(t.amount for t in outflows), 2)), "count": len(outflows),
            "biggest": {"payee": biggest.payee or "unknown", "amount": float(round(-biggest.amount, 2))}}


def _tls_cert(verify: str) -> bool | ssl.SSLContext:
    """actualpy's `cert` for ACTUAL_TLS_VERIFY: 1 = system CAs, 0 = no verification, else a path to the server's
    own certificate — trust exactly that certificate, without a hostname check (the server is reached by IP)."""
    if verify == "1":
        return True
    if verify == "0":
        return False
    ctx = ssl.create_default_context(cafile=verify)
    ctx.check_hostname = False
    return ctx


def _actual_fetch(settings: Settings, day: date) -> list[Txn]:
    """On-budget transactions dated `day` from Actual Budget (read-only copy of the budget, in a temporary
    directory removed afterwards; actualpy)."""
    from actual import Actual
    from actual.queries import get_transactions

    with tempfile.TemporaryDirectory(prefix="actual-") as data_dir:
        with Actual(base_url=settings.actual_server_url, password=settings.actual_password,
                    file=settings.actual_sync_id, encryption_password=settings.actual_encryption_password or None,
                    data_dir=data_dir, cert=_tls_cert(settings.actual_tls_verify),
                    timeout=httpx.Timeout(10, read=30)) as actual:
            rows = get_transactions(actual.session, start_date=day, end_date=day + timedelta(days=1), transfer=False,
                                    off_budget=False)
            return [Txn(amount=row.get_amount(), payee=row.payee.name if row.payee else None)
                    for row in rows if not row.starting_balance_flag]


def actual_spending(settings: Settings, yesterday: date, *, fetch: Callable | None = None) -> dict | None:
    if not settings.actual_configured:
        return None
    try:
        return summarize((fetch or _actual_fetch)(settings, yesterday))
    except Exception as exc:
        log.warning("actual budget unavailable: %s", type(exc).__name__)
        return None


def gather_personal(settings: Settings, http, now: datetime, *, actual_fetch: Callable | None = None) -> dict | None:
    if not settings.personal_segment:
        return None
    today = now.astimezone(TZ).date()
    facts: dict = {}
    if settings.oura_dashboard_url:
        facts.update(oura_facts(http, settings.oura_dashboard_url, today) or {})
    spending = actual_spending(settings, today - timedelta(days=1), fetch=actual_fetch)
    if spending is not None:
        facts["spending"] = spending
    return facts or None


def check(settings: Settings, http, now: datetime, *, actual_fetch: Callable | None = None, out=print) -> None:
    """One line per source: reachable or not. Never prints amounts, payees or secrets."""
    today = now.astimezone(TZ).date()
    if not settings.oura_dashboard_url:
        out("Oura: not configured")
    else:
        facts = oura_facts(http, settings.oura_dashboard_url, today)
        if facts is None:
            out("Oura: unreachable or no data (see logs)")
        else:
            out("Oura: ok, last night's sleep is in" if "sleep" in facts else "Oura: ok, last night's sleep isn't in yet")
    if not settings.actual_configured:
        out("Actual: not configured")
    else:
        try:
            summary = summarize((actual_fetch or _actual_fetch)(settings, today - timedelta(days=1)))
            out(f"Actual: ok ({summary['count']} purchases yesterday)")
        except Exception as exc:
            out(f"Actual: failed ({type(exc).__name__})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal morning facts for the daily brief")
    parser.add_argument("--check", action="store_true", help="check that Oura and Actual are reachable")
    args = parser.parse_args(argv)
    if not args.check:
        parser.print_help()
        return 2
    settings = Settings.from_env()
    with httpx.Client(timeout=OURA_TIMEOUT_S) as http:
        check(settings, http, datetime.now(TZ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
