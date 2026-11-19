import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from writer_service import prepare

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "worker"
APP_URL = "http://morning-brief:8000"
TOKEN = "wt-SECRET-0123456789"


@pytest.fixture(autouse=True)
def no_config_error_sleep(monkeypatch):
    monkeypatch.setenv("WRITER_CONFIG_ERROR_SLEEP_S", "0")


def run(home, token=TOKEN, app_url=APP_URL):
    return prepare.prepare_home(home, app_url, token, TEMPLATES)


def test_prepare_home_writes_rendered_prompts_with_the_in_network_url(tmp_path):
    home = tmp_path / "home"
    paths = run(home)
    for key, name in (("daily", "daily.md"), ("deep_dive", "deep-dive.md")):
        path = Path(paths[key])
        assert path == home / "prompts" / name
        text = path.read_text()
        assert "{{" not in text
        assert "http://morning-brief:8000/api/" in text
        assert str(home / ".config" / "morning-brief" / "worker-header") in text
        assert str(home / ".cache" / "morning-brief") in text
    assert "http://morning-brief:8000/api/daily/claim" in (home / "prompts" / "daily.md").read_text()
    assert (home / ".cache" / "morning-brief").is_dir()
    assert Path(paths["settings"]) == home / "settings.json"


def test_header_file_is_600_and_only_place_with_the_token(tmp_path):
    home = tmp_path / "home"
    paths = run(home)
    header = Path(paths["header"])
    assert header == home / ".config" / "morning-brief" / "worker-header"
    assert header.read_text().strip() == f"Authorization: Bearer {TOKEN}"
    assert stat.S_IMODE(header.stat().st_mode) == 0o600
    assert TOKEN not in json.dumps({k: str(v) for k, v in paths.items()})
    for f in home.rglob("*"):
        if f.is_file() and f != header:
            assert TOKEN not in f.read_text(), f


def test_settings_use_the_strict_writer_rules_and_name_the_app_host(tmp_path):
    home = tmp_path / "home"
    paths = run(home)
    settings = json.loads(Path(paths["settings"]).read_text())
    rwp = prepare.render_worker_prompt
    assert (home / ".cache" / "morning-brief").is_dir()          # Claude's working directory
    assert settings == {"permissions": {"allow": rwp.writer_allow_rules("morning-brief:8000", home, TEMPLATES),
                                        "deny": rwp.writer_deny_rules("morning-brief:8000", home)},
                        "env": {"DISABLE_AUTOUPDATER": "1"}}
    allow = settings["permissions"]["allow"]
    assert not any(r.startswith("Bash(curl *") for r in allow)     # the Mac's generic wildcard rules are not used
    assert any(f"http://morning-brief:8000/api/daily/claim)" in r for r in allow)
    assert not any("worker-header" in r and r.startswith("Read(") for r in allow)   # Claude must not read the token
    assert all(r.startswith("Bash(") and "\\\"" not in r for r in allow if "python3" in r)  # real quotes, not JSON escapes


def test_prepare_is_idempotent(tmp_path):
    home = tmp_path / "home"
    first = run(home)
    snapshot = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    second = run(home)
    assert first == second
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == snapshot
    assert stat.S_IMODE(Path(second["header"]).stat().st_mode) == 0o600


def test_a_new_token_replaces_the_old_one_and_keeps_mode(tmp_path):
    home = tmp_path / "home"
    run(home, token="old-token")
    paths = run(home, token="new-token")
    assert Path(paths["header"]).read_text().strip() == "Authorization: Bearer new-token"
    assert stat.S_IMODE(Path(paths["header"]).stat().st_mode) == 0o600


def test_refuses_a_missing_or_header_breaking_token_without_echoing_it(tmp_path):
    for bad in ("", "   ", "abc\nX-Evil: 1"):
        try:
            run(tmp_path / "h", token=bad)
        except ValueError as e:
            assert "abc" not in str(e)
        else:
            raise AssertionError(f"accepted {bad!r}")
    assert not (tmp_path / "h" / ".config" / "morning-brief" / "worker-header").exists()


def test_main_reads_the_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("WRITER_HOME", str(home))
    monkeypatch.setenv("WORKER_TOKEN", TOKEN + "\n")
    monkeypatch.delenv("APP_URL", raising=False)
    prepare.main()
    assert (home / "prompts" / "daily.md").exists()
    assert (home / ".config" / "morning-brief" / "worker-header").read_text() == f"Authorization: Bearer {TOKEN}\n"


def test_main_without_a_worker_token_exits_with_a_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WRITER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WORKER_TOKEN", raising=False)
    with pytest.raises(SystemExit) as e:
        prepare.main()
    assert e.value.code == 1
    assert "WORKER_TOKEN" in capsys.readouterr().err


def test_a_config_error_waits_before_exiting_non_zero_so_restarts_do_not_spin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WRITER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WORKER_TOKEN", raising=False)
    monkeypatch.delenv("WRITER_CONFIG_ERROR_SLEEP_S")
    events = []
    monkeypatch.setattr(prepare.time, "sleep", lambda s: events.append(("sleep", s, capsys.readouterr().err)))
    with pytest.raises(SystemExit) as e:
        prepare.main()
    assert e.value.code == 1
    assert [(k, s) for k, s, _ in events] == [("sleep", 60)]
    assert "WORKER_TOKEN" in events[0][2]          # the message was already printed when it began to wait
    monkeypatch.setenv("WRITER_CONFIG_ERROR_SLEEP_S", "7")
    events.clear()
    with pytest.raises(SystemExit):
        prepare.main()
    assert events[0][1] == 7


def test_runs_as_a_module_with_only_the_repo_on_the_path(tmp_path):
    env = {"PATH": os.environ["PATH"], "WRITER_HOME": str(tmp_path / "h"), "WORKER_TOKEN": TOKEN}
    subprocess.run([sys.executable, "-m", "writer_service.prepare"], cwd=ROOT, env=env, check=True)
    assert (tmp_path / "h" / "settings.json").exists()


def test_render_script_still_prints_the_same_rules(tmp_path):
    import importlib
    mod = importlib.import_module("render_worker_prompt")
    home = Path("/tmp/fakehome")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "render_worker_prompt.py"),
                          "--server", "http://10.0.0.5:8430/", "--home", str(home), "--out-dir", str(tmp_path)],
                         capture_output=True, text=True, check=True).stdout
    printed = [line.strip().rstrip(",")[1:-1] for line in out.splitlines() if line.startswith('   "')]
    assert printed == mod.allow_rules("10.0.0.5:8430", home)
    assert len(printed) == 10


def test_main_with_an_unwritable_home_exits_with_a_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("WRITER_HOME", str(blocker / "home"))
    monkeypatch.setenv("WORKER_TOKEN", TOKEN)
    with pytest.raises(SystemExit) as e:
        prepare.main()
    err = capsys.readouterr().err
    assert e.value.code == 1
    assert "writer prepare: cannot write" in err and str(blocker) in err
    assert TOKEN not in err
