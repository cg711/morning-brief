"""Personal morning facts for the daily brief: today's weather (Open-Meteo), today's calendar and last night's sleep and today's tip (the Oura dashboard), and yesterday's spending (Actual Budget).
Every source is optional and fails quietly — nothing here may fail a brief.
Secrets are never logged: failures log only the exception type.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
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

OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
WEATHER_TIMEOUT_S = 10
WEATHER_CACHE = "weather_location.json"
PRECIP_LIKELY, WINDY_MPH, HOT_F, COLD_F, DAY_END_HOUR = 50, 30, 90, 10, 22
WMO_WORDS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "rain showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}

EVENTS_MAX, ALL_DAY_MAX, EVENT_TITLE_MAX, CALENDAR_NAME_MAX = 5, 3, 80, 60
_CONTROL_RE = re.compile("[\x00-\x1f\x7f]")


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


def weather_location(settings: Settings, http) -> tuple[float, float, str] | None:
    """(lat, lon, name): WEATHER_LAT/LON, else the cached geocode for LISTENER_LOCATION, else Open-Meteo's geocoder."""
    name = settings.listener_location
    if settings.weather_lat is not None and settings.weather_lon is not None:
        return settings.weather_lat, settings.weather_lon, name
    cache = settings.data_dir / WEATHER_CACHE
    try:
        cached = json.loads(cache.read_text())
        if cached.get("name") == name:
            return float(cached["lat"]), float(cached["lon"]), name
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    try:
        response = http.get(OPEN_METEO_GEOCODE, params={"name": name, "count": 1, "language": "en", "format": "json"},
                            timeout=WEATHER_TIMEOUT_S)
        response.raise_for_status()
        first = (response.json().get("results") or [None])[0]
        lat, lon = float(first["latitude"]), float(first["longitude"])
    except Exception as exc:
        log.warning("weather location unavailable: %s", type(exc).__name__)
        return None
    try:
        cache.write_text(json.dumps({"name": name, "lat": lat, "lon": lon}))
    except OSError as exc:
        log.warning("could not cache the weather location: %s", type(exc).__name__)
    return lat, lon, name


def _at(values, index: int):
    return _number(values[index]) if isinstance(values, list) and index < len(values) else None


def _hour_label(hour: int) -> str:
    return datetime(2000, 1, 1, hour).strftime("%-I %p")


def weather_facts(http, lat: float, lon: float, now: datetime) -> dict | None:
    """Today's conditions, high/low, when rain or snow gets likely, and wind/heat/cold flags (°F, mph, BRIEF_TZ)."""
    try:
        response = http.get(OPEN_METEO_FORECAST, params={
            "latitude": lat, "longitude": lon,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "hourly": "precipitation_probability,snowfall,wind_gusts_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
            "timezone": TZ.key, "forecast_days": 1,
        }, timeout=WEATHER_TIMEOUT_S)
        response.raise_for_status()
        body = response.json()
        daily, hourly = body["daily"], body["hourly"]
        high, low = _at(daily["temperature_2m_max"], 0), _at(daily["temperature_2m_min"], 0)
        code = daily["weather_code"][0]
        times = hourly["time"]
    except Exception as exc:
        log.warning("weather unavailable: %s", type(exc).__name__)
        return None
    if high is None or low is None or not isinstance(times, list):
        return None
    local_now = now.astimezone(TZ)
    precip, windy = None, False
    for index, stamp in enumerate(times):
        try:
            hour = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if hour.date() != local_now.date() or not local_now.hour <= hour.hour <= DAY_END_HOUR:
            continue
        chance = _at(hourly.get("precipitation_probability"), index)
        if precip is None and chance is not None and chance >= PRECIP_LIKELY:
            snowing = (_at(hourly.get("snowfall"), index) or 0) > 0
            precip = {"kind": "snow" if snowing else "rain", "from": _hour_label(hour.hour)}
        gust = _at(hourly.get("wind_gusts_10m"), index)
        if gust is not None and gust >= WINDY_MPH:
            windy = True
    facts: dict = {}
    if isinstance(code, int) and not isinstance(code, bool) and code in WMO_WORDS:
        facts["conditions"] = WMO_WORDS[code]
    high, low = round(high), round(low)
    facts.update(high=high, low=low, precip=precip, windy=windy, hot=high >= HOT_F, cold=low <= COLD_F)
    return facts


def _clean(text, limit: int) -> str:
    return _cut(_CONTROL_RE.sub("", str(text)), limit)


def calendar_facts(http, base_url: str, now: datetime) -> dict | None:
    """Today's calendar from the Oura dashboard's GET /api/agenda: up to 5 timed events still ahead (or under way),
    up to 3 all-day titles, and how many timed events were left off."""
    local_now = now.astimezone(TZ)
    try:
        response = http.get(f"{base_url.rstrip('/')}/api/agenda", params={"date": local_now.date().isoformat()},
                            timeout=OURA_TIMEOUT_S)
        if response.status_code != 200:
            return None
        body = response.json()
    except Exception as exc:
        log.warning("calendar unavailable: %s", type(exc).__name__)
        return None
    if not isinstance(body, dict) or body.get("connected") is not True or not isinstance(body.get("events"), list):
        return None
    timed, all_day = [], []
    for event in body["events"]:
        if not isinstance(event, dict):
            continue
        title = _clean(event.get("title") or "(busy)", EVENT_TITLE_MAX)
        if event.get("all_day"):
            all_day.append(title)
            continue
        try:
            start = datetime.fromisoformat(event["start"]).astimezone(TZ)
            end = datetime.fromisoformat(event["end"]).astimezone(TZ)
        except (KeyError, TypeError, ValueError):
            continue
        if end <= local_now:
            continue
        timed.append((start, title, _clean(event.get("calendar") or "", CALENDAR_NAME_MAX)))
    timed.sort(key=lambda item: item[0])
    return {"events": [{"title": title, "start": start.strftime("%-I:%M %p"), "calendar": calendar}
                       for start, title, calendar in timed[:EVENTS_MAX]],
            "all_day": all_day[:ALL_DAY_MAX], "more": max(0, len(timed) - EVENTS_MAX)}


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
    location = weather_location(settings, http)
    if location is not None:
        weather = weather_facts(http, location[0], location[1], now)
        if weather is not None:
            facts["weather"] = weather
    if settings.oura_dashboard_url:
        calendar = calendar_facts(http, settings.oura_dashboard_url, now)
        if calendar is not None:
            facts["calendar"] = calendar
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
    if not settings.oura_dashboard_url:
        out("Calendar: not configured")
    else:
        calendar = calendar_facts(http, settings.oura_dashboard_url, now)
        out("Calendar: not connected or unavailable" if calendar is None
            else f"Calendar: ok ({len(calendar['events']) + calendar['more']} events left today)")
    location = weather_location(settings, http)
    if location is None:
        out("Weather: failed (no location)")
    else:
        weather = weather_facts(http, location[0], location[1], now)
        out(f"Weather: ok ({location[2]})" if weather is not None else "Weather: failed (no forecast)")
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
    parser.add_argument("--check", action="store_true", help="check that each source is reachable")
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
