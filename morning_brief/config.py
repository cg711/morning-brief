from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Chicago"


def _load_tz(name: str | None) -> ZoneInfo:
    """Time zone for episode dates and the daily schedule (BRIEF_TZ, an IANA name)."""
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ValueError, KeyError):  # ZoneInfoNotFoundError subclasses KeyError
        raise ConfigError(f"BRIEF_TZ={name!r} is not an IANA time zone like 'America/New_York'") from None


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

# USD per million tokens (input, output), checked 2026-09-25. Used for the footer's spend estimate.
PRICES = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class ConfigError(Exception):
    pass


TZ = _load_tz(os.environ.get("BRIEF_TZ"))
NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _parse_time(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    feed_token: str
    model: str = "claude-sonnet-5"
    voice: str = "am_michael"
    cohost_voice: str = "af_heart"
    run_at: time = time(8, 0)
    retry_at: time = time(8, 30)
    public_base_url: str = "http://localhost:8430"
    feeds_path: Path = PROJECT_DIR / "feeds.yaml"
    fake_speech: bool = False
    claude_offline: bool = False
    daily_brief: bool = True
    daily_mode: str = "api"
    ready_by: time = time(8, 30)
    listener_location: str = "Minneapolis"
    worker_token: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    music: bool = True
    personal_segment: bool = False
    oura_dashboard_url: str = ""
    actual_server_url: str = ""
    actual_password: str = field(default="", repr=False)
    actual_sync_id: str = ""
    actual_encryption_password: str = field(default="", repr=False)
    actual_tls_verify: str = "1"
    ui_password: str = field(default="", repr=False)
    session_days: int = 90
    share_base_url: str = ""
    funnel_feeds: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "morning-brief.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def deep_dives_dir(self) -> Path:
        return self.data_dir / "deep-dives"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def music_dir(self) -> Path:
        return self.data_dir / "music"

    @property
    def worker_mode(self) -> bool:
        """The daily brief is written by the Mac worker (DAILY_BRIEF=worker)."""
        return self.daily_brief and self.daily_mode == "worker"

    @property
    def actual_configured(self) -> bool:
        return bool(self.actual_server_url and self.actual_password and self.actual_sync_id)

    @property
    def login_enabled(self) -> bool:
        """UI_PASSWORD is set, so the web UI asks for it."""
        return bool(self.ui_password)

    @classmethod
    def from_env(cls, env=None) -> Settings:
        env = os.environ if env is None else env
        token = env.get("FEED_TOKEN", "")
        if len(token) < 32:
            raise ConfigError(
                "FEED_TOKEN must be at least 32 characters "
                "(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
            )
        worker_token = env.get("WORKER_TOKEN", "")
        if worker_token and len(worker_token) < 32:
            raise ConfigError("WORKER_TOKEN must be at least 32 characters (or empty to disable the worker API)")
        ntfy_topic = env.get("NTFY_TOPIC", "").strip()
        if ntfy_topic and not NTFY_TOPIC_RE.match(ntfy_topic):
            raise ConfigError(
                "NTFY_TOPIC must be 20-64 letters, digits, '-' or '_' (or empty to turn pushes off); "
                "generate one: python3 -c 'import secrets; print(secrets.token_urlsafe(24))'"
            )
        ntfy_server = env.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
        if not ntfy_server.startswith("https://"):
            raise ConfigError("NTFY_SERVER must be an https:// URL")
        daily_raw = env.get("DAILY_BRIEF", "1").strip().lower()
        daily_mode = {"0": "off", "1": "api", "api": "api", "worker": "worker"}.get(daily_raw)
        if daily_mode is None:
            raise ConfigError("DAILY_BRIEF must be 0, 1, api or worker")
        actual_url = env.get("ACTUAL_SERVER_URL", "").strip().rstrip("/")
        actual_password = env.get("ACTUAL_PASSWORD", "")
        actual_sync_id = env.get("ACTUAL_SYNC_ID", "").strip()
        if actual_url and not (actual_password and actual_sync_id):
            raise ConfigError("ACTUAL_SERVER_URL needs ACTUAL_PASSWORD and ACTUAL_SYNC_ID")
        days_raw = env.get("SESSION_DAYS", "90").strip()
        if not (days_raw.isascii() and days_raw.isdigit() and 1 <= int(days_raw) <= 365):
            raise ConfigError("SESSION_DAYS must be a whole number of days from 1 to 365")
        share_base_url = env.get("SHARE_BASE_URL", "").strip().rstrip("/")
        if share_base_url and not share_base_url.startswith("https://"):
            raise ConfigError("SHARE_BASE_URL must be an https:// URL (your Funnel address), or empty to turn sharing off")
        if share_base_url and not urlsplit(share_base_url).hostname:
            raise ConfigError("SHARE_BASE_URL must be an https:// URL (your Funnel address), or empty to turn sharing off")
        return cls(
            data_dir=Path(env.get("DATA_DIR", "data")),
            feed_token=token,
            model=env.get("MODEL", "claude-sonnet-5"),
            voice=env.get("VOICE", "am_michael"),
            cohost_voice=env.get("COHOST_VOICE", "af_heart").strip() or "af_heart",
            run_at=_parse_time(env.get("RUN_AT", "08:00")),
            retry_at=_parse_time(env.get("RETRY_AT", "08:30")),
            public_base_url=env.get("PUBLIC_BASE_URL", "http://localhost:8430").rstrip("/"),
            feeds_path=Path(env.get("FEEDS_PATH", PROJECT_DIR / "feeds.yaml")),
            fake_speech=env.get("FAKE_SPEECH", "") == "1",
            claude_offline=env.get("CLAUDE_OFFLINE", "") == "1",
            daily_brief=daily_mode != "off",
            daily_mode="api" if daily_mode == "off" else daily_mode,
            ready_by=_parse_time(env.get("READY_BY", "08:30")),
            listener_location=env.get("LISTENER_LOCATION", "Minneapolis").strip() or "Minneapolis",
            worker_token=worker_token,
            ntfy_topic=ntfy_topic,
            ntfy_server=ntfy_server,
            music=env.get("MUSIC", "1") != "0",
            personal_segment=env.get("PERSONAL_SEGMENT", "0") == "1",
            oura_dashboard_url=env.get("OURA_DASHBOARD_URL", "").strip().rstrip("/"),
            actual_server_url=actual_url,
            actual_password=actual_password,
            actual_sync_id=actual_sync_id,
            actual_encryption_password=env.get("ACTUAL_ENCRYPTION_PASSWORD", ""),
            actual_tls_verify=env.get("ACTUAL_TLS_VERIFY", "1").strip() or "1",
            ui_password=env.get("UI_PASSWORD", ""),
            session_days=int(days_raw),
            share_base_url=share_base_url,
            funnel_feeds=env.get("FUNNEL_FEEDS", "0") == "1",
        )
