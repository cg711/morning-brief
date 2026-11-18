"""The server writer's loop: poll the app for work, run headless Claude Code when there is some, report back.

Stdlib only (it runs in a slim container on Python 3.11). One process, one job at a time:

  every POLL_S   GET /api/work                      (costs no Claude usage; Claude starts only for a job)
  work found     report "running", run `claude -p` with the prompt on stdin, checking in "running" each minute
  run finished   report the outcome with the run's started_at; back off after anything but ok

Secrets: the child gets CLAUDE_CODE_OAUTH_TOKEN and nothing else of ours (never WORKER_TOKEN); neither token is ever
in argv, a prompt, a report or a log line (everything goes through scrub)."""
from __future__ import annotations

import codecs
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("writer")

DAILY_LIMIT_S = 14 * 60        # under the app's 15-minute daily claim limit
DEEP_LIMIT_S = 45 * 60
POLL_S = 60
BACKOFF_S = (300, 900, 3600)   # after a non-ok run: 5 min, then 15, then 60 (stays at 60), reset on ok
KILL_GRACE_S = 5               # SIGTERM first, SIGKILL for the whole group after this long
OUTPUT_CAP = 2 * 1024 * 1024   # characters kept per stream: the HEAD of stdout (one JSON object, which can carry large
                               # permission_denials tool inputs) and the TAIL of stderr
DETAIL_MAX = 200
HTTP_TIMEOUT_S = 10
FINAL_REPORT_TRIES = 3
FINAL_REPORT_GAP_S = 10

_TOKEN_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_LIMIT_TEXT = re.compile(r"usage limit|rate limit", re.IGNORECASE)
_LOGIN_TEXT = re.compile(r"not logged in|please run /login", re.IGNORECASE)


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return _TOKEN_SHAPE.sub("[redacted]", text)


def _is_hidden(ch: str) -> bool:
    return unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Mn", "Me")   # incl. combining marks, variation selectors


def scrub(text: str, secrets: tuple[str, ...] = (), limit: int = DETAIL_MAX) -> str:
    """Safe to log or send: token shapes and the given secrets removed, control, format and combining-mark characters gone, cut to
    `limit` characters. The order matters: a token split by an invisible character (zero-width, soft hyphen, a control
    character, a combining mark or variation selector) must be put back together before it is looked for, and the cut to `limit` comes last so it can never
    leave a piece of a token behind.

    Redaction runs on the text with every control/format character deleted (which catches a split token), and also on
    the text with newlines and tabs turned into spaces (which keeps it readable). The readable one is used only when
    it redacted exactly what the joined one did; otherwise a token was hidden by a line break and the joined text is used."""
    text = str(text)
    joined = _redact("".join(ch for ch in text if not _is_hidden(ch)), secrets)
    readable = _redact("".join(" " if ch in "\t\n\r" else ch for ch in text if ch in "\t\n\r" or not _is_hidden(ch)),
                       secrets)
    same = "".join(joined.split()) == "".join(readable.split())  # same redactions, only the spacing differs
    out = readable if same else joined
    return out[:limit].strip()


def classify(returncode: int | None, stdout: str, stderr: str, timed_out: bool,
             secrets: tuple[str, ...] = ()) -> tuple[str, str]:
    """(outcome, detail) for a finished `claude -p --output-format json` run. The detail is scrubbed of token shapes and
    of `secrets` over the WHOLE text before it is cut to size."""
    if timed_out:
        return "timeout", "the run hit its time limit and was killed"
    data = None
    if (stdout or "").strip():
        try:
            data = json.loads(stdout)
        except ValueError:
            data = None
    if isinstance(data, dict) and data.get("is_error"):
        text = str(data.get("result") or "") or "claude reported an error"
        status = data.get("api_error_status")
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        if status == 401 or _LOGIN_TEXT.search(text):
            return "auth", scrub(text, secrets)
        if status == 429 or _LIMIT_TEXT.search(text):
            return "limit", scrub(text, secrets)
        return "failed", scrub(text, secrets)
    if returncode != 0:
        if _LOGIN_TEXT.search((stderr or "")[-OUTPUT_CAP:]):
            return "auth", scrub((stderr or "").strip()[-DETAIL_MAX:], secrets)
        clean = scrub(stderr or "", secrets, limit=len(stderr or "") + 1)  # scrub it all, then take the tail
        # a start-up crash prints a stack trace after the message: prefer the `error: ...` line over the tail
        error_line = next((l.strip() for l in (stderr or "").splitlines() if l.lstrip().lower().startswith("error:")), "")
        if error_line:
            return "failed", scrub(error_line, secrets)
        return "failed", clean[-DETAIL_MAX:].strip() if clean else f"claude exited with status {returncode}"
    if not (stdout or "").strip():
        return "failed", "claude printed nothing"
    if not isinstance(data, dict):
        return "failed", "claude's output was not the expected JSON"
    return "ok", ""


@dataclass(frozen=True)
class Config:
    app_url: str = "http://morning-brief:8000"
    worker_token: str = ""
    oauth_token: str = ""
    model: str = "sonnet"
    home: str = "/home/writer"
    permission_mode: str = "dontAsk"
    settings: str = ""             # WRITER_SETTINGS; defaults to {home}/settings.json

    @property
    def settings_path(self) -> str:
        return self.settings or os.path.join(self.home, "settings.json")

    @property
    def work_dir(self) -> str:
        """Claude's working directory: the cache dir the prompts use. Claude Code reads inside its working directory
        without asking (Read, Grep, Glob and read-only Bash such as cat), so it must not enclose the token header
        (~/.config), settings, prompts or ~/.claude."""
        return os.path.join(self.home, ".cache", "morning-brief")

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.worker_token, self.oauth_token) if s)

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            app_url=(env.get("APP_URL") or cls.app_url).rstrip("/"),
            worker_token=env.get("WORKER_TOKEN", "").strip(),    # a trailing newline from an env file must not reach a header
            oauth_token=env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip(),
            model=env.get("WRITER_MODEL") or cls.model,
            home=env.get("WRITER_HOME") or cls.home,
            permission_mode=env.get("WRITER_PERMISSION_MODE") or cls.permission_mode,
            settings=env.get("WRITER_SETTINGS", ""),
        )


def claude_command(kind: str, cfg: Config) -> list[str]:
    """argv for a run. The prompt goes on stdin, never here; never --bare (it ignores the subscription token)."""
    return ["claude", "-p", "--output-format", "json", "--model", cfg.model, "--settings", cfg.settings_path,
            "--permission-mode", cfg.permission_mode, "--no-session-persistence"]


def child_env(cfg: Config, token_fd: int | None = None) -> dict:
    """The only environment Claude gets: no tokens at all (an injected agent could read them from /proc/self/environ),
    no API key, nothing inherited but PATH. The OAuth token travels in an inherited pipe whose descriptor number is
    `token_fd` (run_claude creates the pipe and adds that variable). SUBPROCESS_ENV_SCRUB is explicitly 0: turning it on
    makes Claude Code demand bubblewrap and crash at start-up without it, and it has nothing to scrub anyway because no
    token is in this environment."""
    env = {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin", "HOME": cfg.home,
           "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "0"}
    if token_fd is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"] = str(token_fd)
    return env


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_claude(cmd: list[str], stdin: str, env: dict, cwd: str, timeout: float, on_tick, *,
               tick_s: float = 60, should_stop=None, oauth_token: str = "") -> tuple[int | None, str, str, bool]:
    """Run cmd in its own session with `stdin` as its input. Calls on_tick() every tick_s while it runs. On timeout (or
    when should_stop() turns true) the whole process group gets SIGTERM, then SIGKILL after KILL_GRACE_S, so nothing the
    script started survives. Returns (returncode, stdout, stderr, timed_out). A missing binary raises
    FileNotFoundError.

    `oauth_token` goes to the child through an inherited pipe (read end passed with pass_fds, its number in
    CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR); it is never in argv or the environment. Output is bounded: stdout keeps
    its first OUTPUT_CAP characters (the JSON object), stderr its last OUTPUT_CAP; the rest is read and dropped."""
    read_fd = write_fd = None
    pass_fds: tuple[int, ...] = ()
    try:
        if oauth_token:
            read_fd, write_fd = os.pipe()
            if read_fd < 3:   # never 0/1/2: the child's own stdin/stdout/stderr are dup2'd over those numbers
                moved = fcntl.fcntl(read_fd, fcntl.F_DUPFD, 3)
                os.close(read_fd)
                read_fd = moved
            os.write(write_fd, oauth_token.encode())   # far below the pipe buffer, so it cannot block
            os.close(write_fd)
            write_fd = None
            env = {**env, "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": str(read_fd)}
            pass_fds = (read_fd,)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                cwd=cwd, start_new_session=True, pass_fds=pass_fds)
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    pgid = proc.pid  # start_new_session makes the child the leader of a group of the same number
    bufs: dict[str, list[str]] = {"out": [], "err": []}
    sizes = {"out": 0, "err": 0}

    def feed():
        try:
            proc.stdin.write(stdin.encode("utf-8", "replace"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    def drain(stream, key):
        # read1 returns whatever has arrived (a plain read(n) would sit waiting for n bytes), so output is kept even if a
        # descendant holds the pipe open after the child is gone
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            for raw in iter(lambda: stream.read1(65536), b""):
                chunk = decoder.decode(raw)
                parts = bufs[key]
                if key == "out":                      # the head: stop collecting once the cap is reached
                    room = OUTPUT_CAP - sizes["out"]
                    if room > 0:
                        parts.append(chunk[:room])
                        sizes["out"] += min(len(chunk), room)
                else:                                 # the tail: drop whole old chunks once the cap is covered
                    parts.append(chunk)
                    sizes["err"] += len(chunk)
                    while len(parts) > 1 and sizes["err"] - len(parts[0]) >= OUTPUT_CAP:
                        sizes["err"] -= len(parts.pop(0))
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=feed, daemon=True),
               threading.Thread(target=drain, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, "err"), daemon=True)]
    for t in threads:
        t.start()

    start = time.monotonic()
    next_tick = start + tick_s
    timed_out = False
    stopped = False
    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if now - start >= timeout:
            timed_out = True
        elif should_stop is not None and should_stop():
            stopped = True
        if timed_out or stopped:
            _kill_group(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
            break
        if now >= next_tick:
            next_tick = now + tick_s
            try:
                on_tick()
            except Exception:  # a failed check-in must not stop the run
                log.warning("check-in failed", exc_info=False)
    # Whatever the way out, nothing from this run's group may outlive it (a script's background children included).
    _kill_group(pgid, signal.SIGKILL)
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    # A descendant that escaped the group (setsid, double fork) may still hold a pipe open, so a drain thread can stay
    # blocked in read(). Wait for them only briefly, in total, and close only the streams whose thread has finished:
    # close() would block on the reader's lock. The rest is left to the garbage collector.
    deadline = time.monotonic() + 2
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    for stream, t in ((proc.stdout, threads[1]), (proc.stderr, threads[2])):
        if not t.is_alive():
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    return proc.returncode, "".join(bufs["out"]), "".join(bufs["err"])[-OUTPUT_CAP:], timed_out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error status for us, never followed (the Authorization header must not travel)."""

    def redirect_request(self, *args, **kwargs):
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """No redirect following and no proxies (HTTP(S)_PROXY in the environment is ignored)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


_OPENER = build_opener()


def urllib_http(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, str]:
    """(status, body) for one request to the app. HTTP error statuses (redirects included) are returned; network errors
    raise."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "writer-loop")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(65536).decode("utf-8", "replace")
        except OSError:   # the server hung up while we read its error body; the status is what matters
            body = ""
        return e.code, body


class Writer:
    def __init__(self, cfg: Config, *, http=urllib_http, runner=run_claude, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.http, self.runner, self.clock = http, runner, clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._failures = 0  # consecutive non-ok runs (or errors); picks the backoff step

    # -- plumbing --------------------------------------------------------------------------------------------------------
    def _log(self, level: int, msg: str) -> None:
        log.log(level, "%s", scrub(msg, self.cfg.secrets, limit=500))

    def request_stop(self) -> None:
        self._stop.set()

    def _nap(self, seconds: float) -> None:
        if self._sleep is time.sleep:
            self._stop.wait(seconds)   # a SIGTERM during a long backoff wakes us at once
        else:
            self._sleep(seconds)

    def _call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
        return self.http(method, self.cfg.app_url + path, self.cfg.worker_token, payload)

    def _report(self, kind: str, outcome: str, started_at: str, detail: str = "") -> bool:
        """Send one report; never raises (a lost check-in is logged and the loop carries on). True when it should not
        be sent again: delivered, or refused for a reason a retry cannot fix (a 4xx)."""
        body = {"kind": kind, "outcome": outcome, "started_at": started_at,
                "detail": scrub(detail, self.cfg.secrets)}
        try:
            status, _ = self._call("POST", "/api/writer/report", body)
            if status != 204:
                self._log(logging.WARNING, f"report {kind}/{outcome} answered {status}")
            return status < 500
        except Exception as e:
            self._log(logging.WARNING, f"report {kind}/{outcome} failed: {e}")
            return False

    def _report_final(self, kind: str, outcome: str, started_at: str, detail: str = "") -> None:
        """The report that ends a run carries its outcome (and releases its claim), so an app restart at that instant
        must not lose it: try up to FINAL_REPORT_TRIES times, FINAL_REPORT_GAP_S apart."""
        for attempt in range(FINAL_REPORT_TRIES):
            if self._report(kind, outcome, started_at, detail):
                return
            if attempt < FINAL_REPORT_TRIES - 1:
                self._nap(FINAL_REPORT_GAP_S)

    def _work(self) -> str:
        """'daily', 'deep_dive' or 'idle', from GET /api/work. Raises on anything unexpected."""
        status, text = self._call("GET", "/api/work")
        if status != 200:
            raise RuntimeError(f"/api/work answered {status}")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("/api/work did not return an object")
        if data.get("daily") is True:
            return "daily"
        dives = data.get("deep_dives")
        if isinstance(dives, int) and not isinstance(dives, bool) and dives > 0:
            return "deep_dive"
        return "idle"

    def _backoff(self) -> None:
        self._nap(BACKOFF_S[min(self._failures, len(BACKOFF_S) - 1)])
        self._failures += 1

    # -- one pass --------------------------------------------------------------------------------------------------------
    def step(self) -> str:
        """Poll once, run if there is work, then wait. 'idle', 'daily', 'deep_dive', or 'error'. Never raises."""
        try:
            kind = self._work()
            if kind == "idle":
                self._nap(POLL_S)
                return "idle"
            return self._run(kind)
        except Exception as e:
            self._log(logging.ERROR, f"step failed: {type(e).__name__}: {e}")
            self._backoff()
            return "error"

    def _run(self, kind: str) -> str:
        started_at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        self._report(kind, "running", started_at)
        limit = DAILY_LIMIT_S if kind == "daily" else DEEP_LIMIT_S
        prompt_path = os.path.join(self.cfg.home, "prompts", "daily.md" if kind == "daily" else "deep-dive.md")
        try:
            try:
                with open(prompt_path, encoding="utf-8") as f:
                    prompt = f.read()
            except OSError as e:
                outcome, detail = "failed", f"couldn't read the prompt file {os.path.basename(prompt_path)}: {e.strerror or e}"
            else:
                os.makedirs(self.cfg.work_dir, mode=0o700, exist_ok=True)
                rc, out, err, timed_out = self.runner(
                    claude_command(kind, self.cfg), prompt, child_env(self.cfg), self.cfg.work_dir, limit,
                    lambda: self._report(kind, "running", started_at), tick_s=POLL_S, should_stop=self._stop.is_set,
                    oauth_token=self.cfg.oauth_token)
                outcome, detail = classify(rc, out, err, timed_out, self.cfg.secrets)
                if outcome != "ok" and self._stop.is_set():
                    outcome, detail = "failed", "writer stopped before the run finished"
        except FileNotFoundError:
            outcome, detail = "failed", "claude is not installed (or not on PATH)"
        except Exception as e:
            outcome, detail = "failed", f"couldn't run claude: {type(e).__name__}: {e}"
        self._log(logging.INFO, f"{kind} run finished: {outcome} {detail}")
        self._report_final(kind, outcome, started_at, detail)
        if outcome == "ok":
            self._failures = 0
            self._nap(POLL_S)  # also bounds a job that stays offered after an ok run to one run a minute
        else:
            self._backoff()
        return kind

    # -- the loop ---------------------------------------------------------------------------------------------------------
    def run_forever(self) -> None:
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda *_: self.request_stop())
        self._log(logging.INFO, f"writer started; app {self.cfg.app_url}, model {self.cfg.model}")
        # one idle check-in at start-up: it clears a "sign in again" problem left over from before a token change
        self._report("idle", "running", datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), "started")
        try:
            while not self._stop.is_set():
                self.step()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self._log(logging.INFO, "writer stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Writer(Config.from_env()).run_forever()


if __name__ == "__main__":
    main()
