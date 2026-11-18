"""Startup preparation for the writer container: lay out the home directory Claude Code and its prompts expect.

Run as `python3 -m writer_service.prepare` by the entrypoint, before the loop. Idempotent. The worker token is
written to exactly one file (the mode-600 header the prompts hand to curl) and nowhere else.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _scripts in (_HERE.parent / "scripts", Path("/opt/writer/scripts")):   # the repo (tests), then the image
    if (_scripts / "render_worker_prompt.py").is_file() and str(_scripts) not in sys.path:
        sys.path.insert(0, str(_scripts))
import render_worker_prompt  # noqa: E402  (scripts/ has no __init__)

DEFAULT_APP_URL = "http://morning-brief:8000"
DEFAULT_HOME = "/home/writer"
CONFIG_ERROR_SLEEP_S = 60
DEFAULT_TEMPLATES = _HERE.parent / "worker"
PROMPTS = {"daily": ("daily-brief-task.template.md", "daily.md"),
           "deep_dive": ("deep-dive-task.template.md", "deep-dive.md")}


def _write(path: Path, text: str, mode: int) -> None:
    """Replace `path` atomically with `text`; the file never exists with a wider mode than `mode`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def prepare_home(home: Path, app_url: str, worker_token: str, templates_dir: Path) -> dict:
    home, templates_dir = Path(home), Path(templates_dir)
    token = (worker_token or "").strip()
    if not token or any(ord(c) < 32 or ord(c) == 127 for c in token):
        raise ValueError("WORKER_TOKEN is missing or contains control characters")
    app_url = app_url.rstrip("/")
    scheme, _, host = app_url.rpartition("://")
    scheme = scheme or "http"

    config_dir = home / ".config" / "morning-brief"
    cache_dir = home / ".cache" / "morning-brief"
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    cache_dir.mkdir(parents=True, exist_ok=True)
    header = config_dir / "worker-header"
    _write(header, f"Authorization: Bearer {token}\n", 0o600)

    paths = {"header": header, "cache": cache_dir}
    for key, (template, name) in PROMPTS.items():
        paths[key] = home / "prompts" / name
        _write(paths[key], render_worker_prompt.render(templates_dir / template, app_url, home), 0o644)

    # The strict, template-derived rules (not the Mac's generic ones): the token and the app's other secrets sit in this
    # container's process environments, so a wildcard curl rule would let an injected agent send them out.
    settings = {"permissions": {"allow": render_worker_prompt.writer_allow_rules(host, home, templates_dir, scheme),
                                "deny": render_worker_prompt.writer_deny_rules(host, home)},
                "env": {"DISABLE_AUTOUPDATER": "1"}}
    paths["settings"] = home / "settings.json"
    _write(paths["settings"], json.dumps(settings, indent=2) + "\n", 0o644)
    return paths


def _config_error(message: str) -> None:
    """Say what is wrong, then wait before exiting non-zero: under `restart: unless-stopped` an instant exit would
    crash-loop and fill the logs, and nothing about a missing setting fixes itself in a second."""
    print(message, file=sys.stderr, flush=True)
    try:
        delay = float(os.environ.get("WRITER_CONFIG_ERROR_SLEEP_S", CONFIG_ERROR_SLEEP_S))
    except ValueError:
        delay = CONFIG_ERROR_SLEEP_S
    time.sleep(max(0.0, delay))
    sys.exit(1)


def main() -> None:
    home = Path(os.environ.get("WRITER_HOME") or DEFAULT_HOME)
    app_url = os.environ.get("APP_URL") or DEFAULT_APP_URL
    try:
        prepare_home(home, app_url, os.environ.get("WORKER_TOKEN", ""), DEFAULT_TEMPLATES)
    except ValueError as e:
        _config_error(f"writer prepare: {e}")
    except OSError as e:
        _config_error(f"writer prepare: cannot write {home}: {e}")


if __name__ == "__main__":
    main()
