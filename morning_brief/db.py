from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import PROJECT_DIR

MIGRATIONS_DIR = PROJECT_DIR / "migrations"


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY)")
    done = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}
    applied = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in done:
            continue
        name = path.name.replace("'", "''")
        script = "BEGIN;\n" + path.read_text() + f"\nINSERT INTO schema_migrations (filename) VALUES ('{name}');\nCOMMIT;"
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(path.name)
    return applied


# --- runs ---------------------------------------------------------------

def start_run(conn, *, date: str, trigger: str, model: str, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (date, trigger, status, stage, started_at, model) VALUES (?, ?, 'running', 'starting', ?, ?)",
        (date, trigger, started_at, model),
    )
    return cur.lastrowid


def set_stage(conn, run_id: int, stage: str) -> None:
    conn.execute("UPDATE runs SET stage = ? WHERE id = ?", (stage, run_id))


def set_feed_errors(conn, run_id: int, errors: list[str]) -> None:
    conn.execute("UPDATE runs SET feed_errors = ? WHERE id = ?", (json.dumps(errors), run_id))


def add_usage(conn, run_id: int, input_tokens: int, output_tokens: int) -> None:
    conn.execute(
        "UPDATE runs SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ? WHERE id = ?",
        (input_tokens, output_tokens, run_id),
    )


def finish_run(conn, run_id: int, status: str, finished_at: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
        (status, finished_at, error, run_id),
    )


def fail_interrupted_runs(conn, finished_at: str) -> int:
    cur = conn.execute(
        "UPDATE runs SET status = 'failed', error = 'interrupted', finished_at = ? WHERE status = 'running'",
        (finished_at,),
    )
    return cur.rowcount


def latest_run_for(conn, date: str):
    return conn.execute("SELECT * FROM runs WHERE date = ? ORDER BY id DESC LIMIT 1", (date,)).fetchone()


def latest_run(conn):
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def failed_run_count(conn, date: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE date = ? AND status = 'failed'", (date,)
    ).fetchone()["n"]


def usage_by_model(conn, since_date: str) -> list:
    return conn.execute(
        "SELECT model, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens "
        "FROM runs WHERE date >= ? GROUP BY model ORDER BY model",
        (since_date,),
    ).fetchall()


def delete_runs_before(conn, date: str) -> int:
    return conn.execute("DELETE FROM runs WHERE date < ? AND status != 'running'", (date,)).rowcount


# --- episodes -----------------------------------------------------------

def publish_episode(conn, *, date: str, cutoff_at: str, script_json: str, word_count: int,
                    duration_s: float, audio_bytes: int, now: str, sources: list[dict]) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute(
            """INSERT INTO episodes (date, cutoff_at, script_json, word_count, duration_s, audio_bytes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET cutoff_at = excluded.cutoff_at, script_json = excluded.script_json,
                 word_count = excluded.word_count, duration_s = excluded.duration_s,
                 audio_bytes = excluded.audio_bytes, updated_at = excluded.updated_at""",
            (date, cutoff_at, script_json, word_count, duration_s, audio_bytes, now, now),
        )
        conn.execute("DELETE FROM sources WHERE episode_date = ?", (date,))
        conn.executemany(
            "INSERT INTO sources (episode_date, item_id, source, title, url, published_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(date, s["item_id"], s["source"], s["title"], s["url"], s["published_at"]) for s in sources],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_episode(conn, date: str):
    return conn.execute("SELECT * FROM episodes WHERE date = ?", (date,)).fetchone()


def list_episodes(conn) -> list:
    return conn.execute("SELECT * FROM episodes ORDER BY date DESC").fetchall()


def previous_episode(conn, before_date: str):
    return conn.execute(
        "SELECT * FROM episodes WHERE date < ? ORDER BY date DESC LIMIT 1", (before_date,)
    ).fetchone()


def episode_sources(conn, date: str) -> list:
    return conn.execute(
        "SELECT * FROM sources WHERE episode_date = ? ORDER BY rowid", (date,)
    ).fetchall()


def delete_episode_row(conn, date: str) -> bool:
    return conn.execute("DELETE FROM episodes WHERE date = ?", (date,)).rowcount > 0


def episode_dates_before(conn, date: str) -> list[str]:
    return [r["date"] for r in conn.execute("SELECT date FROM episodes WHERE date < ? ORDER BY date", (date,))]


# --- app state (small key/value facts such as the worker's last check-in) ----

def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
