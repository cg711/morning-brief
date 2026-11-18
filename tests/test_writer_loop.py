"""The server writer's loop: polls the app, runs headless Claude Code when there is work, reports back.

No network and no real Claude: a fake HTTP client, a fake runner, a fake clock and sleep. Only the process-group tests
run real processes (tiny shell scripts standing in for `claude`)."""
import http.server
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime

import pytest

from writer_service import loop

WORKER_TOKEN = "wt-0123456789abcdef-worker"
OAUTH_TOKEN = "sk-ant-oat01-ABCDEFGHIJKLMNOP-qrstuv"
NOW = 1_800_000_000.0  # fixed epoch seconds for the fake clock


# ---- fakes ---------------------------------------------------------------------------------------------------------

class FakeHttp:
    """http(method, url, token, payload=None) -> (status, text). `work` is a list of replies (the last one repeats);
    a reply may be a dict, or an Exception to raise."""

    def __init__(self, work=None):
        self.work = list(work if work is not None else [{"daily": False, "deep_dives": 0}])
        self.calls = []
        self.reports = []

    def __call__(self, method, url, token, payload=None):
        self.calls.append((method, url, token, payload))
        if url.endswith("/api/work"):
            reply = self.work.pop(0) if len(self.work) > 1 else self.work[0]
            if isinstance(reply, Exception):
                raise reply
            return 200, json.dumps(reply)
        if url.endswith("/api/writer/report"):
            self.reports.append(payload)
            return 204, ""
        return 404, ""


class FakeRunner:
    """Stands in for run_claude: returns queued results, records how it was called, can tick or raise."""

    def __init__(self, results=None, ticks=0, raises=None):
        self.results = list(results if results is not None else [(0, json.dumps({"is_error": False, "result": "done"}), "", False)])
        self.ticks = ticks
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, stdin, env, cwd, timeout, on_tick, **kw):
        self.calls.append({"cmd": cmd, "stdin": stdin, "env": env, "cwd": cwd, "timeout": timeout, **kw})
        for _ in range(self.ticks):
            on_tick()
        if self.raises:
            raise self.raises
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class Sleeps:
    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


OK = (0, json.dumps({"is_error": False, "result": "done"}), "", False)
AUTH = (1, json.dumps({"is_error": True, "api_error_status": 401, "result": "Failed to authenticate. API Error: 401"}), "", False)
FAIL = (1, json.dumps({"is_error": True, "result": "boom"}), "", False)


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "daily.md").write_text("DAILY PROMPT BODY")
    (home / "prompts" / "deep-dive.md").write_text("DEEP PROMPT BODY")
    return loop.Config(app_url="http://app.test:8000", worker_token=WORKER_TOKEN, oauth_token=OAUTH_TOKEN,
                       model="sonnet", home=str(home), permission_mode="dontAsk")


def make(cfg, http=None, runner=None):
    sleeps = Sleeps()
    w = loop.Writer(cfg, http=http or FakeHttp(), runner=runner or FakeRunner(), clock=lambda: NOW, sleep=sleeps)
    return w, sleeps


# ---- constants and config --------------------------------------------------------------------------------------------

def test_constants():
    assert loop.DAILY_LIMIT_S == 14 * 60
    assert loop.DEEP_LIMIT_S == 45 * 60
    assert loop.POLL_S == 60
    assert loop.BACKOFF_S == (300, 900, 3600)


def test_config_from_env_defaults_and_overrides():
    c = loop.Config.from_env({"WORKER_TOKEN": "w", "CLAUDE_CODE_OAUTH_TOKEN": "o"})
    assert (c.app_url, c.model, c.home, c.permission_mode) == ("http://morning-brief:8000", "sonnet", "/home/writer", "dontAsk")
    c = loop.Config.from_env({"APP_URL": "http://x:1/", "WRITER_MODEL": "opus", "WRITER_HOME": "/h",
                              "WRITER_PERMISSION_MODE": "plan"})
    assert (c.app_url, c.model, c.home, c.permission_mode) == ("http://x:1", "opus", "/h", "plan")


def test_command_has_the_prompt_off_argv_and_never_bare(cfg):
    cmd = loop.claude_command("daily", cfg)
    assert cmd == ["claude", "-p", "--output-format", "json", "--model", "sonnet", "--settings", cfg.settings_path,
                   "--permission-mode", "dontAsk", "--no-session-persistence"]
    assert "--bare" not in cmd
    assert loop.claude_command("deep_dive", cfg) == cmd


def test_child_env_is_minimal_and_has_no_token_at_all(cfg, monkeypatch):
    monkeypatch.setenv("WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-should-not-pass")
    env = loop.child_env(cfg)
    assert set(env) == {"PATH", "HOME", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"}
    assert env["DISABLE_AUTOUPDATER"] == "1" and env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert env["HOME"] == cfg.home
    assert WORKER_TOKEN not in env.values() and OAUTH_TOKEN not in env.values()
    with_fd = loop.child_env(cfg, token_fd=7)   # the number of the inherited pipe that holds the token
    assert set(with_fd) == set(env) | {"CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"}
    assert with_fd["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"] == "7"
    assert OAUTH_TOKEN not in with_fd.values()


def test_config_from_env_strips_both_tokens():
    c = loop.Config.from_env({"WORKER_TOKEN": "  wtok\n", "CLAUDE_CODE_OAUTH_TOKEN": "otok\r\n"})
    assert (c.worker_token, c.oauth_token) == ("wtok", "otok")


# ---- scrub and classify -------------------------------------------------------------------------------------------------

def test_scrub_removes_tokens_and_control_characters():
    text = f"bad {OAUTH_TOKEN} and {WORKER_TOKEN}\x00\x1b[31m\nsecond\tline\x07"
    out = loop.scrub(text, (WORKER_TOKEN, ""))
    assert OAUTH_TOKEN not in out and WORKER_TOKEN not in out and "sk-ant" not in out
    assert not any(ord(c) < 32 or ord(c) == 127 for c in out)
    assert "second" in out
    assert len(loop.scrub("x" * 500)) == 200
    assert loop.scrub("  plain  ") == "plain"


def _leaks(out, token, window=8):
    """True when out holds the token or any `window`-character piece of it."""
    return any(token[i:i + window] in out for i in range(len(token) - window + 1))


SPLITTERS = ["\u200b", "\u200d", "\ufeff", "\u00ad", "\x00", "\x1b", "\x07", "\n", "\t", "\r",
             "\u034f", "\ufe0f", "\ufe00", "\u0301", "\u20dd"]  # the last five are combining marks (Mn, Me)
TOKENS = (OAUTH_TOKEN, WORKER_TOKEN)


@pytest.mark.parametrize("splitter", SPLITTERS)
def test_scrub_cannot_be_bypassed_by_splitting_a_token(splitter):
    for token in TOKENS:
        for i in range(1, len(token)):
            split = token[:i] + splitter + token[i:]
            out = loop.scrub(f"before {split} after", TOKENS)
            assert not _leaks(out, token), (i, out)
            assert "before" in out and "after" in out
    # the token shape alone is caught with no configured secrets, split anywhere
    shape = "sk-ant-oat01-ZZZZYYYYXXXXWWWW"
    for i in range(1, len(shape)):
        out = loop.scrub(shape[:i] + splitter + shape[i:])
        assert not _leaks(out, shape), (i, out)


def test_scrub_keeps_readable_line_breaks_when_nothing_is_hidden():
    assert loop.scrub("line one\nline two\tend") == "line one line two end"


@pytest.mark.parametrize("secrets", [(), TOKENS])
def test_a_token_straddling_the_detail_boundary_in_stderr_is_fully_redacted(secrets):
    for token in TOKENS:
        for pad in range(150, 200):   # the 200-character tail cut lands at every position inside the token
            stderr = "ab" + token + "z" * pad
            outcome, detail = loop.classify(2, "", stderr, False, secrets)
            assert outcome == "failed" and len(detail) <= 200
            if secrets or token == OAUTH_TOKEN:   # an unconfigured, unshaped secret cannot be known
                assert not _leaks(detail, token), (pad, detail)


@pytest.mark.parametrize("secrets", [(), TOKENS])
def test_a_token_straddling_the_detail_boundary_in_the_result_text_is_fully_redacted(secrets):
    for token in TOKENS:
        for pad in range(150, 200):
            out = json.dumps({"is_error": True, "result": "a" * pad + token + " tail"})
            outcome, detail = loop.classify(1, out, "", False, secrets)
            assert outcome == "failed" and len(detail) <= 200
            if secrets or token == OAUTH_TOKEN:
                assert not _leaks(detail, token), (pad, detail)


def test_the_report_never_carries_a_straddling_or_split_token(cfg):
    for stderr in ("z" * 175 + WORKER_TOKEN + "tail",
                   "z" * 190 + OAUTH_TOKEN,
                   "sk-ant\u200b-oat01-" + "A" * 16 + " and " + WORKER_TOKEN[:5] + "\u00ad" + WORKER_TOKEN[5:]):
        http = FakeHttp([{"daily": True, "deep_dives": 0}])
        w, _ = make(cfg, http, FakeRunner([(1, "", stderr, False)]))
        w.step()
        for r in http.reports:
            for token in TOKENS:
                assert not _leaks(json.dumps(r["detail"]), token), r


def _j(**kw):
    return json.dumps(kw)


@pytest.mark.parametrize("rc,out,err,timed_out,expected", [
    (1, _j(is_error=True, api_error_status=401, result="Failed to authenticate"), "", False, "auth"),
    (1, _j(is_error=True, api_error_status=429, result="slow down"), "", False, "limit"),
    (1, _j(is_error=True, api_error_status=None, result="Claude AI USAGE LIMIT reached"), "", False, "limit"),
    (1, _j(is_error=True, result="You have hit the Rate Limit"), "", False, "limit"),
    (1, _j(is_error=True, api_error_status=500, result="server broke"), "", False, "failed"),
    (0, _j(is_error=True, result="agent error"), "", False, "failed"),
    (0, "", "", False, "failed"),
    (0, "   \n", "", False, "failed"),
    (0, "this is not json", "", False, "failed"),
    (0, "[1, 2]", "", False, "failed"),
    (2, _j(is_error=False, result="fine"), "", False, "failed"),
    (2, "", "segfault", False, "failed"),
    (None, "", "", False, "failed"),
    (None, _j(is_error=False, result="x"), "", True, "timeout"),
    (-15, "", "", True, "timeout"),
    (0, _j(is_error=False, result="all good"), "", False, "ok"),
    (1, _j(is_error=True, result="Not logged in \u00b7 Please run /login"), "", False, "auth"),
    (1, _j(is_error=True, api_error_status=None, result="NOT LOGGED IN"), "", False, "auth"),
    (1, _j(is_error=True, result="Error: please run /login to continue"), "", False, "auth"),
    (1, "", "Not logged in. Please run /login", False, "auth"),
    (0, _j(is_error=False, result="the script says: you are not logged in"), "", False, "ok"),
])
def test_each_classification_row(rc, out, err, timed_out, expected):
    outcome, detail = loop.classify(rc, out, err, timed_out)
    assert outcome == expected
    assert len(detail) <= 200
    if expected == "ok":
        assert detail == ""
    else:
        assert detail  # always says something about why


# ---- the Writer ---------------------------------------------------------------------------------------------------------

def test_daily_goes_before_deep_dives(cfg):
    http = FakeHttp([{"daily": True, "deep_dives": 2}, {"daily": False, "deep_dives": 2}])
    runner = FakeRunner([OK])
    w, _ = make(cfg, http, runner)
    assert w.step() == "daily"
    assert w.step() == "deep_dive"
    assert [c["stdin"] for c in runner.calls] == ["DAILY PROMPT BODY", "DEEP PROMPT BODY"]
    assert [c["timeout"] for c in runner.calls] == [loop.DAILY_LIMIT_S, loop.DEEP_LIMIT_S]
    assert [r["kind"] for r in http.reports if r["outcome"] == "ok"] == ["daily", "deep_dive"]
    assert all(c["cwd"] == cfg.work_dir for c in runner.calls)


def test_claude_runs_in_the_cache_dir_which_does_not_enclose_the_token_header_or_settings(cfg):
    from pathlib import Path
    http, runner = FakeHttp([{"daily": True, "deep_dives": 0}]), FakeRunner([OK])
    w, _ = make(cfg, http, runner)
    w.step()
    cwd = Path(runner.calls[0]["cwd"])
    home = Path(cfg.home)
    assert cwd == home / ".cache" / "morning-brief" and cwd.is_dir()        # the loop creates it when missing
    for secret in (home / ".config" / "morning-brief" / "worker-header", Path(cfg.settings_path),
                   home / "prompts" / "daily.md", home / ".claude"):
        assert cwd != secret and cwd not in secret.parents
    assert runner.calls[0]["env"]["HOME"] == cfg.home                       # the CLI still finds its own config


def test_no_work_means_no_claude_and_an_idle_report_is_not_sent(cfg):
    http, runner = FakeHttp([{"daily": False, "deep_dives": 0}]), FakeRunner()
    w, sleeps = make(cfg, http, runner)
    assert w.step() == "idle"
    assert runner.calls == []
    assert http.reports == []
    assert sleeps.slept == [loop.POLL_S]
    method, url, token, _ = http.calls[0]
    assert (method, url, token) == ("GET", "http://app.test:8000/api/work", WORKER_TOKEN)


def test_a_running_checkin_each_minute_during_a_run(cfg):
    http = FakeHttp([{"daily": True, "deep_dives": 0}])
    runner = FakeRunner([OK], ticks=3)
    w, _ = make(cfg, http, runner)
    w.step()
    outcomes = [r["outcome"] for r in http.reports]
    assert outcomes == ["running", "running", "running", "running", "ok"]  # one at once, one per tick, then the end
    assert runner.calls[0]["tick_s"] == 60
    assert all(c[0] == "POST" and c[1] == "http://app.test:8000/api/writer/report" and c[2] == WORKER_TOKEN
               for c in http.calls if c[3] is not None)


def test_report_carries_started_at_and_outcome(cfg):
    http = FakeHttp([{"daily": False, "deep_dives": 1}])
    w, _ = make(cfg, http, FakeRunner([FAIL]))
    assert w.step() == "deep_dive"
    last = http.reports[-1]
    assert set(last) == {"kind", "outcome", "started_at", "detail"}
    assert (last["kind"], last["outcome"]) == ("deep_dive", "failed")
    started = datetime.fromisoformat(last["started_at"])
    assert started.tzinfo is not None and started.timestamp() == NOW
    assert http.reports[0]["started_at"] == last["started_at"]  # the running check-in names the same run
    assert last["detail"] == "boom"


class FlakyReports(FakeHttp):
    """The first `fail` report POSTs raise (an app restart) or answer `status`; later ones succeed."""

    def __init__(self, fail, status=None, **kw):
        super().__init__(**kw)
        self.fail, self.status, self.attempts = fail, status, []

    def __call__(self, method, url, token, payload=None):
        if url.endswith("/api/writer/report") and payload["outcome"] != "running":
            self.attempts.append(payload["outcome"])
            if len(self.attempts) <= self.fail:
                if self.status is not None:
                    return self.status, ""
                raise ConnectionRefusedError("app restarting")
        return super().__call__(method, url, token, payload)


def test_the_final_report_is_retried_three_times_ten_seconds_apart(cfg):
    http = FlakyReports(fail=2, work=[{"daily": True, "deep_dives": 0}])
    w, sleeps = make(cfg, http, FakeRunner([FAIL]))
    w.step()
    assert http.attempts == ["failed"] * 3                       # two lost, the third delivered
    assert [r["outcome"] for r in http.reports][-1] == "failed"
    assert sleeps.slept == [10, 10, 300]                         # the two gaps, then the normal backoff


def test_the_final_report_gives_up_after_three_tries(cfg):
    http = FlakyReports(fail=99, status=503, work=[{"daily": True, "deep_dives": 0}])
    w, sleeps = make(cfg, http, FakeRunner([OK]))
    assert w.step() == "daily"
    assert http.attempts == ["ok"] * 3
    assert sleeps.slept == [10, 10, loop.POLL_S]


def test_a_refused_final_report_is_not_retried(cfg):
    http = FlakyReports(fail=99, status=422, work=[{"daily": True, "deep_dives": 0}])
    w, sleeps = make(cfg, http, FakeRunner([OK]))
    w.step()
    assert http.attempts == ["ok"] and sleeps.slept == [loop.POLL_S]


def test_a_running_checkin_is_not_retried(cfg):
    class Down(FakeHttp):
        def __call__(self, method, url, token, payload=None):
            if payload and payload["outcome"] == "running":
                self.reports.append(payload)
                raise ConnectionRefusedError("app restarting")
            return super().__call__(method, url, token, payload)
    http = Down([{"daily": True, "deep_dives": 0}])
    w, sleeps = make(cfg, http, FakeRunner([OK]))
    w.step()
    assert len([r for r in http.reports if r["outcome"] == "running"]) == 1
    assert sleeps.slept == [loop.POLL_S]


def test_start_up_sends_one_idle_check_in(cfg):
    http = FakeHttp([{"daily": False, "deep_dives": 0}])

    def sleep(seconds):
        os.kill(os.getpid(), signal.SIGTERM)

    w = loop.Writer(cfg, http=http, runner=FakeRunner(), clock=lambda: NOW, sleep=sleep)
    w.run_forever()
    assert [(r["kind"], r["outcome"]) for r in http.reports] == [("idle", "running")]


def test_backoff_steps_5_15_60_and_resets_on_ok(cfg):
    http = FakeHttp([{"daily": True, "deep_dives": 0}])
    runner = FakeRunner([FAIL, FAIL, FAIL, FAIL, OK, FAIL])
    w, sleeps = make(cfg, http, runner)
    for _ in range(6):
        w.step()
    assert sleeps.slept == [300, 900, 3600, 3600, loop.POLL_S, 300]


def test_app_unreachable_does_not_crash_and_backs_off(cfg):
    http = FakeHttp([OSError("connection refused"), OSError("connection refused"), {"daily": False, "deep_dives": 0}])
    runner = FakeRunner()
    w, sleeps = make(cfg, http, runner)
    assert w.step() == "error"
    assert w.step() == "error"
    assert sleeps.slept == [300, 900]
    assert runner.calls == []


def test_a_bad_work_reply_is_an_error_not_a_crash(cfg):
    class Junk(FakeHttp):
        def __call__(self, method, url, token, payload=None):
            return 200, "<html>nope</html>"
    w, sleeps = make(cfg, Junk(), FakeRunner())
    assert w.step() == "error"
    assert sleeps.slept == [300]


def test_claude_not_installed_is_a_failed_report(cfg):
    http = FakeHttp([{"daily": True, "deep_dives": 0}])
    w, sleeps = make(cfg, http, FakeRunner(raises=FileNotFoundError(2, "No such file or directory", "claude")))
    assert w.step() == "daily"
    assert http.reports[-1]["outcome"] == "failed"
    assert "claude" in http.reports[-1]["detail"]
    assert sleeps.slept == [300]


def test_a_missing_prompt_file_is_a_failed_report(cfg, tmp_path):
    os.remove(os.path.join(cfg.home, "prompts", "daily.md"))
    http = FakeHttp([{"daily": True, "deep_dives": 0}])
    runner = FakeRunner()
    w, _ = make(cfg, http, runner)
    assert w.step() == "daily"
    assert runner.calls == []
    assert http.reports[-1]["outcome"] == "failed" and "prompt" in http.reports[-1]["detail"]


def test_token_never_in_argv_env_of_child_beyond_oauth_or_reports(cfg, caplog):
    leaky = (1, json.dumps({"is_error": True, "result": f"oops {OAUTH_TOKEN} {WORKER_TOKEN} sk-ant-api03-zzzzzzzz"}), "", False)
    http = FakeHttp([{"daily": True, "deep_dives": 0}])
    runner = FakeRunner([leaky])
    w, _ = make(cfg, http, runner)
    with caplog.at_level(logging.DEBUG):
        w.step()
    call = runner.calls[0]
    assert not any(WORKER_TOKEN in a or OAUTH_TOKEN in a or "sk-ant" in a for a in call["cmd"])
    assert "WORKER_TOKEN" not in call["env"] and WORKER_TOKEN not in call["env"].values()
    assert set(call["env"]) == {"PATH", "HOME", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"}
    assert OAUTH_TOKEN not in call["env"].values()
    assert call["oauth_token"] == OAUTH_TOKEN   # handed to the runner in memory, which pipes it to the child
    for r in http.reports:
        blob = json.dumps(r)
        assert WORKER_TOKEN not in blob and OAUTH_TOKEN not in blob and "sk-ant" not in blob
    assert WORKER_TOKEN not in caplog.text and OAUTH_TOKEN not in caplog.text and "sk-ant" not in caplog.text


def test_an_exception_message_with_a_token_is_scrubbed_in_the_log(cfg, caplog):
    http = FakeHttp([RuntimeError(f"bad header Bearer {WORKER_TOKEN}")])
    w, _ = make(cfg, http, FakeRunner())
    with caplog.at_level(logging.DEBUG):
        assert w.step() == "error"
    assert WORKER_TOKEN not in caplog.text


def test_sigterm_stops_the_loop_between_polls(cfg):
    http = FakeHttp([{"daily": False, "deep_dives": 0}])
    seen = []

    def sleep(seconds):
        seen.append(seconds)
        os.kill(os.getpid(), signal.SIGTERM)  # the container is being stopped while the loop naps

    before = signal.getsignal(signal.SIGTERM)
    w = loop.Writer(cfg, http=http, runner=FakeRunner(), clock=lambda: NOW, sleep=sleep)
    w.run_forever()
    assert seen == [loop.POLL_S]
    assert len([c for c in http.calls if c[1].endswith("/api/work")]) == 1
    assert signal.getsignal(signal.SIGTERM) == before  # handlers put back


def test_a_stop_during_a_run_is_passed_to_the_runner_and_reported(cfg):
    http = FakeHttp([{"daily": True, "deep_dives": 0}])
    holder = {}

    def runner(cmd, stdin, env, cwd, timeout, on_tick, **kw):
        holder["should_stop"] = kw["should_stop"]
        assert kw["should_stop"]() is False
        holder["w"].request_stop()
        assert kw["should_stop"]() is True
        return (-15, "", "", False)

    w, _ = make(cfg, http, runner)
    holder["w"] = w
    w.step()
    assert http.reports[-1]["outcome"] == "failed"
    assert "stop" in http.reports[-1]["detail"]


# ---- run_claude against real processes ------------------------------------------------------------------------------

def _script(tmp_path, body):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    path = bindir / "claude"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bindir


def _alive(pid):
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _wait_dead(pid, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _env(bindir, **extra):
    return {"PATH": f"{bindir}:/usr/bin:/bin", **extra}


def test_run_claude_returns_output_and_sends_the_prompt_on_stdin(tmp_path):
    bindir = _script(tmp_path, 'cat\necho "argv: $*" >&2\n')
    rc, out, err, timed_out = loop.run_claude(["claude", "-p"], "hello prompt", _env(bindir), str(tmp_path), 10, lambda: None)
    assert (rc, out, timed_out) == (0, "hello prompt", False)
    assert "argv: -p" in err and "hello prompt" not in err


def test_run_claude_ticks_while_it_runs(tmp_path):
    bindir = _script(tmp_path, "sleep 1.2\n")
    ticks = []
    t0 = time.time()
    rc, _, _, timed_out = loop.run_claude(["claude"], "", _env(bindir), str(tmp_path), 10, lambda: ticks.append(1), tick_s=0.3)
    assert rc == 0 and not timed_out
    assert 2 <= len(ticks) <= 5
    assert time.time() - t0 < 4


def test_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    bindir = _script(tmp_path, f'sleep 60 &\necho $! > "{pidfile}"\nwait\n')
    t0 = time.time()
    rc, out, err, timed_out = loop.run_claude(["claude", "-p"], "x", _env(bindir), str(tmp_path), 1, lambda: None)
    assert timed_out is True
    assert time.time() - t0 < 6
    pid = int(pidfile.read_text())
    assert _wait_dead(pid), "the script's background child survived the timeout"


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "KILL_GRACE_S", 0.5)
    pidfile = tmp_path / "child.pid"
    bindir = _script(tmp_path, f"trap '' TERM\nsleep 60 &\necho $! > \"{pidfile}\"\nwhile :; do wait; done\n")
    t0 = time.time()
    rc, out, err, timed_out = loop.run_claude(["claude"], "", _env(bindir), str(tmp_path), 1, lambda: None)
    assert timed_out is True
    assert time.time() - t0 < 6
    assert _wait_dead(int(pidfile.read_text())), "a SIGTERM-proof child survived"


def test_run_claude_stops_on_request_and_kills_the_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    bindir = _script(tmp_path, f'sleep 60 &\necho $! > "{pidfile}"\nwait\n')
    t0 = time.time()
    flag = {"stop": False}

    def tick():
        flag["stop"] = True

    rc, _, _, timed_out = loop.run_claude(["claude"], "", _env(bindir), str(tmp_path), 30, tick, tick_s=0.3,
                                          should_stop=lambda: flag["stop"])
    assert timed_out is False
    assert time.time() - t0 < 6
    assert _wait_dead(int(pidfile.read_text()))


def test_run_claude_missing_binary_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        loop.run_claude(["claude-does-not-exist"], "", {"PATH": str(tmp_path)}, str(tmp_path), 5, lambda: None)


# ---- the token travels in a pipe, not the environment ---------------------------------------------------------------

READ_FD_SCRIPT = (
    'eval "cat <&$CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" > "$OUT/token"\n'
    'env > "$OUT/env"\n'
    'echo "$@" > "$OUT/argv"\n'
    'echo \'{"is_error": false, "result": "ok"}\'\n'
)


def _open_fds():
    return len(os.listdir("/dev/fd"))


def test_the_token_reaches_the_child_only_through_the_inherited_pipe(tmp_path, cfg):
    bindir = _script(tmp_path, READ_FD_SCRIPT)
    out = tmp_path / "out"
    out.mkdir()
    env = {**loop.child_env(cfg), "PATH": f"{bindir}:/usr/bin:/bin", "OUT": str(out)}
    cmd = loop.claude_command("daily", cfg)
    rc, stdout, _, timed_out = loop.run_claude(cmd, "the prompt", env, str(tmp_path), 10, lambda: None,
                                               oauth_token=OAUTH_TOKEN)
    assert (rc, timed_out) == (0, False)
    assert json.loads(stdout)["result"] == "ok"
    assert (out / "token").read_text() == OAUTH_TOKEN          # exactly the token, no newline
    child_env_text = (out / "env").read_text()
    assert OAUTH_TOKEN not in child_env_text and WORKER_TOKEN not in child_env_text
    assert "CLAUDE_CODE_OAUTH_TOKEN=" not in child_env_text
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1" in child_env_text
    fd_lines = [l for l in child_env_text.splitlines() if l.startswith("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=")]
    assert len(fd_lines) == 1 and fd_lines[0].split("=", 1)[1].isdigit()
    assert OAUTH_TOKEN not in (out / "argv").read_text()


def test_no_token_given_means_no_descriptor_variable(tmp_path, cfg):
    bindir = _script(tmp_path, 'env > "$OUT/env"\necho \'{}\'\n')
    out = tmp_path / "out"
    out.mkdir()
    env = {**loop.child_env(cfg), "PATH": f"{bindir}:/usr/bin:/bin", "OUT": str(out)}
    loop.run_claude(["claude"], "", env, str(tmp_path), 10, lambda: None)
    assert "FILE_DESCRIPTOR" not in (out / "env").read_text()


def test_no_descriptor_leaks_across_runs(tmp_path, cfg):
    bindir = _script(tmp_path, READ_FD_SCRIPT)
    out = tmp_path / "out"
    out.mkdir()
    env = {**loop.child_env(cfg), "PATH": f"{bindir}:/usr/bin:/bin", "OUT": str(out)}
    loop.run_claude(["claude"], "", env, str(tmp_path), 10, lambda: None, oauth_token=OAUTH_TOKEN)  # warm up
    before = _open_fds()
    for _ in range(20):
        loop.run_claude(["claude"], "x", env, str(tmp_path), 10, lambda: None, oauth_token=OAUTH_TOKEN)
    for _ in range(5):   # the pipe is closed even when the program cannot start
        with pytest.raises(FileNotFoundError):
            loop.run_claude(["no-such-claude"], "x", {"PATH": str(tmp_path)}, str(tmp_path), 5, lambda: None,
                            oauth_token=OAUTH_TOKEN)
    assert _open_fds() == before


# ---- bounded output buffers -----------------------------------------------------------------------------------------------

def test_output_buffers_are_bounded_head_of_stdout_and_tail_of_stderr(tmp_path):
    bindir = _script(tmp_path, (
        'echo HEAD-OUT\nhead -c 5000000 /dev/zero | tr "\\0" a\necho TAIL-OUT\n'
        'echo HEAD-ERR >&2\nhead -c 5000000 /dev/zero | tr "\\0" b >&2\necho TAIL-ERR >&2\n'))
    t0 = time.time()
    rc, out, err, timed_out = loop.run_claude(["claude"], "", _env(bindir), str(tmp_path), 30, lambda: None)
    assert (rc, timed_out) == (0, False)
    assert time.time() - t0 < 10
    assert loop.OUTPUT_CAP == 2 * 1024 * 1024
    assert len(out) <= loop.OUTPUT_CAP and len(err) <= loop.OUTPUT_CAP
    assert out.startswith("HEAD-OUT") and "TAIL-OUT" not in out           # stdout keeps its head
    assert err.rstrip().endswith("TAIL-ERR") and "HEAD-ERR" not in err    # stderr keeps its tail


def test_a_normal_sized_json_result_still_parses_under_the_cap():
    big = json.dumps({"is_error": False, "result": "x" * 100_000})
    assert len(big) < loop.OUTPUT_CAP
    assert loop.classify(0, big, "", False)[0] == "ok"


# ---- http: no redirects followed, no proxies ------------------------------------------------------------------------------

class _Recorder(http.server.BaseHTTPRequestHandler):
    seen = None   # set per server class

    def do_GET(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))   # consume a POST body so closing does not reset
        type(self).seen.append(dict(self.headers))
        if getattr(type(self), "redirect_to", None):
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    do_POST = do_GET

    def log_message(self, *a):
        pass


def _serve(redirect_to=None):
    handler = type("H", (_Recorder,), {"seen": [], "redirect_to": redirect_to})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


def test_a_redirect_is_an_error_status_and_the_token_never_follows_it():
    second, second_h = _serve()
    first, first_h = _serve(redirect_to=f"http://127.0.0.1:{second.server_port}/steal")
    try:
        status, _ = loop.urllib_http("GET", f"http://127.0.0.1:{first.server_port}/api/work", WORKER_TOKEN)
        assert status == 302
        assert first_h.seen and first_h.seen[0]["Authorization"] == f"Bearer {WORKER_TOKEN}"
        assert second_h.seen == []   # the second server never got a request, so never the header
        status, _ = loop.urllib_http("POST", f"http://127.0.0.1:{first.server_port}/api/writer/report", WORKER_TOKEN, {"a": 1})
        assert status == 302 and second_h.seen == []
    finally:
        first.shutdown()
        second.shutdown()


def test_the_http_opener_ignores_proxy_settings(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    import urllib.request
    # a ProxyHandler with no proxies registers no methods, so it is absent from the list; one with proxies is not
    default = [h for h in urllib.request.build_opener().handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert default and default[0].proxies, "control: urllib's default opener would use the proxy"
    handlers = loop.build_opener().handlers
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers)


# ---- a daemon that escapes the process group must not hang run_claude -----------------------------------------------------

def test_a_setsid_daemon_holding_the_pipes_does_not_hang_run_claude(tmp_path):
    pidfile = tmp_path / "daemon.pid"
    # The daemon starts its own session (so the group kill cannot reach it), keeps stdout and stderr open and outlives
    # the script. It writes its pid only once detached, and the script waits for that before exiting.
    daemon_code = f"import os, time; os.setsid(); open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(40)"
    bindir = _script(tmp_path, (
        f'{sys.executable} -c "{daemon_code}" &\n'
        f'while [ ! -s "{pidfile}" ]; do sleep 0.05; done\n'
        'echo \'{"is_error": false, "result": "ok"}\'\nexit 0\n'))
    result = {}

    def go():
        result["r"] = loop.run_claude(["claude"], "x", _env(bindir), str(tmp_path), 30, lambda: None)

    t0 = time.time()
    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    try:
        worker.join(timeout=20)
        elapsed = time.time() - t0
        assert not worker.is_alive(), "run_claude hung waiting for a descendant that escaped the group"
        # generous for a loaded machine, still far under what a real hang costs (the 30 s run timeout, or the
        # daemon's 40 s sleep)
        assert elapsed < 15, elapsed
        rc, out, _, timed_out = result["r"]
        assert (rc, timed_out) == (0, False)
        assert json.loads(out)["result"] == "ok"   # what the child printed before exiting still arrives
        assert _alive(int(pidfile.read_text())), "control: the daemon really did escape the group kill"
    finally:
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)   # the test cleans up the daemon
        except (OSError, ValueError):
            pass
        worker.join(timeout=5)


# ---- the token pipe never lands on fd 0, 1 or 2 ---------------------------------------------------------------------------

@pytest.mark.parametrize("closed", [0, 1, 2])
def test_the_token_pipe_is_never_fd_0_1_or_2(tmp_path, cfg, closed):
    bindir = _script(tmp_path, READ_FD_SCRIPT)
    out = tmp_path / "out"
    out.mkdir()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = f"""
import os, sys, json
sys.path.insert(0, {repo!r})
from writer_service import loop
cfg = loop.Config(home={cfg.home!r})
env = {{**loop.child_env(cfg), 'PATH': {str(bindir) + ':/usr/bin:/bin'!r}, 'OUT': {str(out)!r}}}
saved = os.dup({closed})            # keep the real descriptor safe on a higher number
os.close({closed})                  # now the lowest free descriptor, which os.pipe() will hand out first, is {closed}
try:
    r = loop.run_claude(['claude'], 'x', env, {str(tmp_path)!r}, 10, lambda: None, oauth_token={OAUTH_TOKEN!r})
finally:
    os.dup2(saved, {closed})
open(os.path.join({str(out)!r}, 'rc'), 'w').write(json.dumps([r[0], r[3]]))
"""
    proc = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((out / "rc").read_text()) == [0, False]
    assert (out / "token").read_text() == OAUTH_TOKEN
    fd_line = [l for l in (out / "env").read_text().splitlines() if l.startswith("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=")][0]
    assert int(fd_line.split("=", 1)[1]) >= 3
