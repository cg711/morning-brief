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
