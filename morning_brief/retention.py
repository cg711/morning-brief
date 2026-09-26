from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from . import db

KEEP_DAYS = 10
RUN_KEEP_DAYS = 62


def delete_episode(conn, audio_dir: Path, episode_date: str) -> bool:
    existed = db.delete_episode_row(conn, episode_date)
    (audio_dir / f"{episode_date}.mp3").unlink(missing_ok=True)
    return existed


def prune(conn, audio_dir: Path, today: date) -> list[str]:
    """Keep today plus the previous KEEP_DAYS days of episodes; keep runs RUN_KEEP_DAYS for spend totals."""
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    removed = db.episode_dates_before(conn, cutoff)
    for episode_date in removed:
        delete_episode(conn, audio_dir, episode_date)
    for stray in audio_dir.glob("*.mp3"):
        if stray.stem < cutoff:
            stray.unlink(missing_ok=True)
    today_str = today.isoformat()
    for pattern in ("*.mp3.tmp", "*.mp3.bak"):
        for stray in audio_dir.glob(pattern):
            # today's may belong to a run in progress; leave it alone
            if stray.name[:10] < today_str:
                stray.unlink(missing_ok=True)
    db.delete_runs_before(conn, (today - timedelta(days=RUN_KEEP_DAYS)).isoformat())
    return removed
