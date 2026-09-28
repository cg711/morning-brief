import json
from dataclasses import replace

import httpx
import pytest

from morning_brief import personal
from tests.helpers import NOW  # 2026-09-25 13:00 UTC = 08:00 Central

FORECAST = "api.open-meteo.com/v1/forecast"
GEOCODE = "geocoding-api.open-meteo.com/v1/search"


def routed(routes):
    """An httpx.Client whose responses are chosen by host+path; unknown paths get 404. Returns (client, requests)."""
    calls = []

    def handler(request):
        calls.append(request)
        value = routes.get(request.url.host + request.url.path)
        if value is None:
            return httpx.Response(404)
        return value(request) if callable(value) else value

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def series(default, at=None):
    values = [default] * 24
    for hour, value in (at or {}).items():
        values[hour] = value
    return values


def forecast(code=3, high=57.6, low=40.6, probs=None, snow=None, gusts=None):
    return {"daily": {"weather_code": [code], "temperature_2m_max": [high], "temperature_2m_min": [low]},
            "hourly": {"time": [f"2026-09-25T{h:02d}:00" for h in range(24)],
                       "precipitation_probability": series(0, probs), "snowfall": series(0.0, snow),
                       "wind_gusts_10m": series(5.0, gusts)}}


def facts_for(body):
    http, calls = routed({FORECAST: httpx.Response(200, json=body)})
    return personal.weather_facts(http, 44.98, -93.27, NOW), calls


def test_basic_facts_and_request():
    facts, calls = facts_for(forecast())
    assert facts == {"conditions": "cloudy", "high": 58, "low": 41, "precip": None, "windy": False,
                     "hot": False, "cold": False}
    params = calls[0].url.params
    assert params["latitude"] == "44.98" and params["longitude"] == "-93.27"
    assert params["temperature_unit"] == "fahrenheit" and params["wind_speed_unit"] == "mph"
    assert params["timezone"] == "America/Chicago" and params["forecast_days"] == "1"
    assert params["daily"] == "weather_code,temperature_2m_max,temperature_2m_min"
    assert params["hourly"] == "precipitation_probability,snowfall,wind_gusts_10m"


@pytest.mark.parametrize("probs,expected", [
    ({15: 50}, {"kind": "rain", "from": "3 PM"}),
    ({15: 49}, None),
    ({7: 90}, None),                                   # before the current hour (8 AM)
    ({23: 90}, None),                                  # after 10 PM
    ({22: 60}, {"kind": "rain", "from": "10 PM"}),
    ({8: 50, 15: 90}, {"kind": "rain", "from": "8 AM"}),  # the current hour counts, first one wins
])
def test_precip_window_and_threshold(probs, expected):
    assert facts_for(forecast(probs=probs))[0]["precip"] == expected


def test_snow_when_snowfall_that_hour():
    assert facts_for(forecast(probs={9: 70}, snow={9: 0.2}))[0]["precip"] == {"kind": "snow", "from": "9 AM"}


@pytest.mark.parametrize("gusts,windy", [({10: 30}, True), ({10: 29.9}, False), ({23: 45}, False), ({6: 50}, False)])
def test_windy(gusts, windy):
    assert facts_for(forecast(gusts=gusts))[0]["windy"] is windy


@pytest.mark.parametrize("high,low,hot,cold", [(89.6, 50, True, False), (89.4, 50, False, False),
                                               (50, 10.4, False, True), (50, 10.6, False, False)])
def test_hot_and_cold_use_rounded_values(high, low, hot, cold):
    facts = facts_for(forecast(high=high, low=low))[0]
    assert (facts["hot"], facts["cold"]) == (hot, cold)


def test_codes():
    assert facts_for(forecast(code=0))[0]["conditions"] == "clear"
    assert facts_for(forecast(code=63))[0]["conditions"] == "rain"
    assert facts_for(forecast(code=96))[0]["conditions"] == "thunderstorms with hail"
    assert "conditions" not in facts_for(forecast(code=42))[0]


def test_bad_responses_give_none():
    assert facts_for({"hourly": {}})[0] is None
    assert facts_for({**forecast(), "daily": {"weather_code": [3], "temperature_2m_max": [None],
                                              "temperature_2m_min": [40]}})[0] is None
    http, _ = routed({FORECAST: httpx.Response(500)})
    assert personal.weather_facts(http, 1.0, 2.0, NOW) is None


def geocoder(results):
    return {GEOCODE: httpx.Response(200, json={"results": results} if results is not None else {})}


def test_location_override_makes_no_request(settings):
    http, calls = routed({})
    s = replace(settings, weather_lat=44.98, weather_lon=-93.27)
    assert personal.weather_location(s, http) == (44.98, -93.27, "Minneapolis")
    assert calls == []


def test_location_geocoded_then_cached(settings):
    http, calls = routed(geocoder([{"latitude": 44.98, "longitude": -93.26, "name": "Minneapolis"}]))
    assert personal.weather_location(settings, http) == (44.98, -93.26, "Minneapolis")
    assert calls[0].url.params["name"] == "Minneapolis" and calls[0].url.params["count"] == "1"
    cached = json.loads((settings.data_dir / "weather_location.json").read_text())
    assert cached == {"name": "Minneapolis", "lat": 44.98, "lon": -93.26}
    http, calls = routed({})
    assert personal.weather_location(settings, http) == (44.98, -93.26, "Minneapolis")
    assert calls == []


def test_location_looked_up_again_when_name_changes(settings):
    (settings.data_dir / "weather_location.json").write_text(json.dumps({"name": "Duluth", "lat": 1, "lon": 2}))
    http, calls = routed(geocoder([{"latitude": 44.98, "longitude": -93.26}]))
    assert personal.weather_location(settings, http) == (44.98, -93.26, "Minneapolis") and len(calls) == 1


@pytest.mark.parametrize("results", [[], None])
def test_location_without_results_is_none(settings, results):
    http, _ = routed(geocoder(results))
    assert personal.weather_location(settings, http) is None
    assert not (settings.data_dir / "weather_location.json").exists()
