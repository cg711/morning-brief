"""Proves the writer container is isolated, from inside it. Stdlib only; needs no sudo.

  docker compose --profile writer exec writer python3 /opt/writer/writer_service/check_isolation.py

Must work: the app answers (HTTP 401 or 200), a public address connects, a DNS lookup resolves.
Must not connect (refused, or no answer within 3 seconds; a refusal means the packet reached the host): the Docker gateway (the host) on ports 22 and 8430, and
whatever you name in the optional environment variables below. An unset probe is skipped with a notice.

  HOST_TAILNET_IP         the server's tailnet address (tried on port 8430)
  LAN_PROBE               host:port of something on your LAN that normally answers, e.g. your router's 192.168.1.1:80
  OTHER_CONTAINER_PROBE   host:port of another container's service, e.g. 172.17.0.2:5432

The variables are not set in the container by default; pass them in:
  docker compose --profile writer exec -e HOST_TAILNET_IP=100.x.y.z -e LAN_PROBE=192.168.x.y:80 writer python3 ...

Exits 0 only if every checked expectation holds."""
from __future__ import annotations

import os
import http.client
import socket
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 3
APP_URL = "http://morning-brief:8000/api/work"
GATEWAY = "172.28.40.1"
PUBLIC = ("1.1.1.1", 443)
DNS_NAME = "example.com"


def tcp_connects(host: str, port: int, timeout: float) -> str:
    """'open' (connected), 'refused' (something answered with a reset) or 'timeout' (no answer, or unreachable)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except (OSError, OverflowError, ValueError):
        return "timeout"


def http_status(url: str, timeout: float) -> int | None:
    """The HTTP status, or None when nothing answered. No credentials are sent, so a healthy app says 401."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except (OSError, ValueError, http.client.HTTPException):
        return None


def resolves(name: str) -> bool:
    try:
        return bool(socket.getaddrinfo(name, 443))
    except (OSError, ValueError):
        return False


def _split(probe: str) -> tuple[str, int] | None:
    host, sep, port = probe.rpartition(":")
    if not (sep and host and port.isascii() and port.isdigit()) or not 1 <= int(port) <= 65535:
        return None
    return host, int(port)


def _state(result) -> str:
    """Normalize a probe result: True/'open' is open, 'refused' is refused, anything else is no answer."""
    if result is True or result == "open":
        return "open"
    return "refused" if result == "refused" else "timeout"


def run(env, *, tcp=tcp_connects, http=http_status, dns=resolves, out=print) -> int:
    """Run every check, print a table through `out`, and return the exit code (0 only when nothing FAILed)."""
    rows: list[tuple[str, str, str]] = []   # (result, expectation, detail)

    def must_work(label: str, ok: bool, detail: str = "") -> None:
        rows.append(("ok" if ok else "FAIL", f"works: {label}", detail))

    def must_be_blocked(label: str, host: str, port: int) -> None:
        state = _state(tcp(host, port, TIMEOUT_S))
        detail = {"open": "connected", "timeout": "no answer",
                  "refused": "refused: the packet reached the host, nothing is listening"}[state]
        rows.append(("FAIL" if state == "open" else "ok", f"blocked: {label}", detail))

    status = http(APP_URL, TIMEOUT_S)
    must_work("the app (HTTP 401 or 200)", status in (200, 401), f"answered {status}" if status else "no answer")
    must_work(f"public internet ({PUBLIC[0]}:{PUBLIC[1]})", _state(tcp(PUBLIC[0], PUBLIC[1], TIMEOUT_S)) == "open")
    dns_ok = bool(dns(DNS_NAME))
    must_work(f"DNS lookup ({DNS_NAME})", dns_ok,
              "" if dns_ok else "if the firewall blocks your resolver, re-run it with ALLOW_DNS=<resolver address>")

    for port in (22, 8430):
        must_be_blocked(f"Docker gateway {GATEWAY}:{port}", GATEWAY, port)

    tailnet = (env.get("HOST_TAILNET_IP") or "").strip()
    if tailnet:
        must_be_blocked(f"server tailnet address {tailnet}:8430", tailnet, 8430)
    else:
        rows.append(("SKIP", "blocked: server tailnet address", "set HOST_TAILNET_IP to check"))
    for name, label in (("LAN_PROBE", "LAN"), ("OTHER_CONTAINER_PROBE", "another container")):
        probe = (env.get(name) or "").strip()
        if not probe:
            rows.append(("SKIP", f"blocked: {label}", f"set {name}=host:port to check"))
        elif (target := _split(probe)) is None:
            rows.append(("FAIL", f"blocked: {label}", f"{name} must look like host:port, got {probe!r}"))
        else:
            must_be_blocked(f"{label} {probe}", *target)

    width = max(len(expectation) for _, expectation, _ in rows)
    for result, expectation, detail in rows:
        out(f"{result:<5} {expectation:<{width}}  {detail}")
    failed = sum(1 for result, _, _ in rows if result == "FAIL")
    skipped = sum(1 for result, _, _ in rows if result == "SKIP")
    if skipped:
        out(f"\n{skipped} check(s) skipped: not everything was proven. Set the variables named above to cover them.")
    out("\nIsolation holds." if not failed else f"\n{failed} check(s) FAILED. Is writer_service/firewall.sh applied and confirmed?")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(os.environ))
