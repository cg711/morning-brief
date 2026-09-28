from datetime import time

import pytest

from morning_brief.config import ConfigError, Settings


def test_from_env_reads_values(tmp_path):
    s = Settings.from_env({
        "FEED_TOKEN": "x" * 32, "DATA_DIR": str(tmp_path), "MODEL": "claude-opus-5",
        "RUN_AT": "07:30", "PUBLIC_BASE_URL": "https://h.ts.net:8443/",
    })
    assert s.model == "claude-opus-5"
    assert s.run_at == time(7, 30)
    assert s.retry_at == time(8, 30)
    assert s.public_base_url == "https://h.ts.net:8443"
    assert s.db_path == tmp_path / "morning-brief.db"
    assert s.audio_dir == tmp_path / "audio"
    assert s.fake_speech is False
    assert s.claude_offline is False


def test_short_token_rejected():
    with pytest.raises(ConfigError):
        Settings.from_env({"FEED_TOKEN": "short"})


def test_fake_speech_flag():
    assert Settings.from_env({"FEED_TOKEN": "x" * 32, "FAKE_SPEECH": "1"}).fake_speech is True


def test_from_env_defaults_voice_to_am_michael():
    s = Settings.from_env({"FEED_TOKEN": "x" * 32})
    assert s.voice == "am_michael"


def test_deep_dive_settings(tmp_path):
    s = Settings.from_env({"FEED_TOKEN": "x" * 32, "DATA_DIR": str(tmp_path)})
    assert s.daily_brief is True and s.worker_token == ""
    assert s.deep_dives_dir == tmp_path / "deep-dives"
    s = Settings.from_env({"FEED_TOKEN": "x" * 32, "DAILY_BRIEF": "0", "WORKER_TOKEN": "w" * 32})
    assert s.daily_brief is False and s.worker_token == "w" * 32


def test_short_worker_token_rejected():
    with pytest.raises(ConfigError, match="WORKER_TOKEN"):
        Settings.from_env({"FEED_TOKEN": "x" * 32, "WORKER_TOKEN": "short"})


def test_listener_location_setting():
    assert Settings.from_env({"FEED_TOKEN": "x" * 32}).listener_location == "Minneapolis"
    assert Settings.from_env({"FEED_TOKEN": "x" * 32, "LISTENER_LOCATION": "Chicago"}).listener_location == "Chicago"


def test_brief_tz_loader():
    from morning_brief.config import _load_tz
    assert _load_tz(None).key == "America/Chicago"
    assert _load_tz("America/New_York").key == "America/New_York"
    with pytest.raises(ConfigError, match="BRIEF_TZ"):
        _load_tz("Not/AZone")


def test_ntfy_settings():
    base = {"FEED_TOKEN": "x" * 32}
    s = Settings.from_env(base)
    assert s.ntfy_topic == "" and s.ntfy_server == "https://ntfy.sh"
    s = Settings.from_env({**base, "NTFY_TOPIC": "Ab_-" * 6, "NTFY_SERVER": "https://ntfy.example.com/"})
    assert s.ntfy_topic == "Ab_-" * 6 and s.ntfy_server == "https://ntfy.example.com"
    for bad in ("short", "has space in it but long enough", "x" * 65, "ünïcödé" * 4):
        with pytest.raises(ConfigError):
            Settings.from_env({**base, "NTFY_TOPIC": bad})
    with pytest.raises(ConfigError):
        Settings.from_env({**base, "NTFY_SERVER": "http://ntfy.sh"})


def test_music_setting(tmp_path):
    base = {"FEED_TOKEN": "x" * 32, "DATA_DIR": str(tmp_path)}
    assert Settings.from_env(base).music is True
    assert Settings.from_env({**base, "MUSIC": "0"}).music is False
    assert Settings.from_env(base).music_dir == tmp_path / "music"


def test_cohost_voice_setting():
    assert Settings.from_env({"FEED_TOKEN": "x" * 32}).cohost_voice == "af_heart"
    assert Settings.from_env({"FEED_TOKEN": "x" * 32, "COHOST_VOICE": "bf_emma"}).cohost_voice == "bf_emma"
    assert Settings.from_env({"FEED_TOKEN": "x" * 32, "COHOST_VOICE": ""}).cohost_voice == "af_heart"


@pytest.mark.parametrize("raw, brief, mode", [("0", False, "api"), ("1", True, "api"), ("api", True, "api"),
                                              (" Worker ", True, "worker"), ("worker", True, "worker")])
def test_daily_brief_modes(raw, brief, mode):
    s = Settings.from_env({"FEED_TOKEN": "x" * 32, "DAILY_BRIEF": raw})
    assert (s.daily_brief, s.daily_mode, s.worker_mode) == (brief, mode, brief and mode == "worker")


def test_daily_brief_invalid_and_ready_by():
    with pytest.raises(ConfigError, match="DAILY_BRIEF must be 0, 1, api or worker"):
        Settings.from_env({"FEED_TOKEN": "x" * 32, "DAILY_BRIEF": "yes"})
    assert Settings.from_env({"FEED_TOKEN": "x" * 32}).ready_by == time(8, 30)
    assert Settings.from_env({"FEED_TOKEN": "x" * 32, "READY_BY": "09:05"}).ready_by == time(9, 5)


def test_personal_settings():
    base = {"FEED_TOKEN": "x" * 32}
    s = Settings.from_env(base)
    assert (s.personal_segment, s.oura_dashboard_url, s.actual_configured, s.actual_tls_verify) == (False, "", False, "1")
    s = Settings.from_env({**base, "PERSONAL_SEGMENT": "1", "OURA_DASHBOARD_URL": "http://172.17.0.1:8090/",
                           "ACTUAL_SERVER_URL": "https://172.17.0.1:5006", "ACTUAL_PASSWORD": "pw-secret",
                           "ACTUAL_SYNC_ID": "sync-1", "ACTUAL_TLS_VERIFY": "0"})
    assert s.personal_segment and s.oura_dashboard_url == "http://172.17.0.1:8090" and s.actual_configured
    assert "pw-secret" not in repr(s)
    with pytest.raises(ConfigError, match="ACTUAL_SERVER_URL needs ACTUAL_PASSWORD and ACTUAL_SYNC_ID"):
        Settings.from_env({**base, "ACTUAL_SERVER_URL": "https://x:5006", "ACTUAL_PASSWORD": "pw"})


def test_login_and_share_settings():
    s = Settings.from_env({"FEED_TOKEN": "x" * 32, "UI_PASSWORD": "hunter2 horse", "SESSION_DAYS": "30",
                           "SHARE_BASE_URL": " https://box.example.ts.net/ ", "FUNNEL_FEEDS": "1"})
    assert s.ui_password == "hunter2 horse" and s.login_enabled is True
    assert s.session_days == 30
    assert s.share_base_url == "https://box.example.ts.net"
    assert s.funnel_feeds is True
    assert "hunter2" not in repr(s)


def test_login_and_share_defaults():
    s = Settings.from_env({"FEED_TOKEN": "x" * 32})
    assert s.ui_password == "" and s.login_enabled is False
    assert s.session_days == 90 and s.share_base_url == "" and s.funnel_feeds is False


@pytest.mark.parametrize("days", ["0", "366", "-5", "abc", "1.5"])
def test_session_days_bounds(days):
    with pytest.raises(ConfigError, match="SESSION_DAYS"):
        Settings.from_env({"FEED_TOKEN": "x" * 32, "SESSION_DAYS": days})


def test_session_days_edges_accepted():
    assert Settings.from_env({"FEED_TOKEN": "x" * 32, "SESSION_DAYS": "1"}).session_days == 1
    assert Settings.from_env({"FEED_TOKEN": "x" * 32, "SESSION_DAYS": "365"}).session_days == 365


def test_share_base_url_must_be_https():
    with pytest.raises(ConfigError, match="SHARE_BASE_URL"):
        Settings.from_env({"FEED_TOKEN": "x" * 32, "SHARE_BASE_URL": "http://box.example.ts.net"})


def test_share_base_url_must_have_a_host():
    with pytest.raises(ConfigError, match="SHARE_BASE_URL"):
        Settings.from_env({"FEED_TOKEN": "x" * 32, "SHARE_BASE_URL": "https://"})


def test_inbox_token():
    s = Settings.from_env({"FEED_TOKEN": "x" * 32, "INBOX_TOKEN": "i" * 32})
    assert s.inbox_token == "i" * 32 and "iiii" not in repr(s)
    assert Settings.from_env({"FEED_TOKEN": "x" * 32}).inbox_token == ""
    with pytest.raises(ConfigError, match="INBOX_TOKEN must be at least 32 characters"):
        Settings.from_env({"FEED_TOKEN": "x" * 32, "INBOX_TOKEN": "short"})


def test_weather_override():
    s = Settings.from_env({"FEED_TOKEN": "x" * 32, "WEATHER_LAT": "44.98", "WEATHER_LON": " -93.27 "})
    assert (s.weather_lat, s.weather_lon) == (44.98, -93.27)
    s = Settings.from_env({"FEED_TOKEN": "x" * 32})
    assert (s.weather_lat, s.weather_lon) == (None, None)


@pytest.mark.parametrize("env", [{"WEATHER_LAT": "44.98"}, {"WEATHER_LON": "-93.27"},
                                 {"WEATHER_LAT": "north", "WEATHER_LON": "-93.27"},
                                 {"WEATHER_LAT": "95", "WEATHER_LON": "-93.27"},
                                 {"WEATHER_LAT": "44.98", "WEATHER_LON": "-181"},
                                 {"WEATHER_LAT": "nan", "WEATHER_LON": "-93.27"}])
def test_weather_override_rejected(env):
    with pytest.raises(ConfigError, match="WEATHER_LAT"):
        Settings.from_env({"FEED_TOKEN": "x" * 32, **env})
