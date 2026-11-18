"""The writer's firewall script, its isolation check and the README section.

Nothing here touches iptables or the network: the script is only syntax-checked and read, and the isolation check runs
with injected probe functions."""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from writer_service import check_isolation

ROOT = Path(__file__).resolve().parent.parent
FIREWALL = ROOT / "writer_service" / "firewall.sh"
SCRIPT = FIREWALL.read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
README = (ROOT / "README.md").read_text()


def var(name):
    match = re.search(rf'^{name}=["\']?([^"\'\s]+)["\']?\s*(?:#.*)?$', SCRIPT, re.MULTILINE)
    assert match, f"{name} is not set at the top of firewall.sh"
    return match.group(1)


def rule_lines():
    """The `add CHAIN ...` lines in file order."""
    return [l.strip() for l in SCRIPT.splitlines() if re.match(r"\s*add\s+(DOCKER-USER|INPUT)\b", l)]


def test_firewall_script_passes_bash_syntax_check():
    result = subprocess.run(["bash", "-n", str(FIREWALL)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_firewall_header_says_the_user_runs_it_with_sudo():
    header = "\n".join(SCRIPT.splitlines()[:25])
    assert SCRIPT.startswith("#!") and "sudo" in header


def test_rules_are_in_the_required_order_and_tagged():
    # the subnet and the app address are the ones docker-compose.yml fixes
    subnet = COMPOSE["networks"]["writer_net"]["ipam"]["config"][0]["subnet"]
    app_ip = COMPOSE["services"]["morning-brief"]["networks"]["writer_net"]["ipv4_address"]
    assert var("SUBNET") == subnet == "172.29.40.0/24"
    assert var("APP_IP") == app_ip == "172.29.40.10"

    # every rule goes through add(), which is the one place that appends the tag
    assert var("TAG") == "morning-brief-writer"
    add_fn = re.search(r"^add\(\)\s*\{.*?^\}", SCRIPT, re.MULTILINE | re.DOTALL)
    assert add_fn and '-m comment --comment "$TAG"' in add_fn.group(0)
    inserts = [l for l in SCRIPT.splitlines() if re.search(r"\biptables\b.*\s-[IA]\s", l)]
    assert inserts and all(l in add_fn.group(0) for l in inserts)

    docker_user = [l for l in rule_lines() if l.startswith("add DOCKER-USER")]
    ranges = ["172.16.0.0/12", "10.0.0.0/8", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]   # the first four are required
    app_accept = next(i for i, l in enumerate(docker_user) if '"$APP_IP"' in l and "-j ACCEPT" in l)
    drops = {r: next(i for i, l in enumerate(docker_user) if f"-d {r}" in l and "-j DROP" in l) for r in ranges}
    assert all(app_accept < i for i in drops.values())          # the app sits inside 172.16.0.0/12
    assert any("ESTABLISHED,RELATED" in l and "-j ACCEPT" in l for l in docker_user[app_accept + 1:min(drops.values())])
    assert docker_user[-1].endswith("-j ACCEPT") and '-d ' not in docker_user[-1]          # the rest is allowed
    assert all('-s "$SUBNET"' in l for l in docker_user)

    host = [l for l in rule_lines() if l.startswith("add INPUT")]
    assert "ESTABLISHED,RELATED" in host[-2] and host[-2].endswith("-j ACCEPT")
    assert host[-1].endswith("-j DROP") and '-s "$SUBNET"' in host[-1]


def test_firewall_script_is_idempotent_and_removes_only_its_own_rules():
    assert re.search(r"^remove_rules\(\)", SCRIPT, re.MULTILINE)
    assert 'grep -F -- "--comment $TAG"' in SCRIPT and "s/^-A /-D /" in SCRIPT
    apply_fn = re.search(r"^apply\(\)\s*\{.*?^\}", SCRIPT, re.MULTILINE | re.DOTALL).group(0)
    assert apply_fn.index("remove_rules") < apply_fn.index("add DOCKER-USER")


def test_firewall_script_has_auto_rollback_armed_before_any_rule():
    assert re.search(r'^ROLLBACK_S="\$\{ROLLBACK_S:-300\}"', SCRIPT, re.MULTILINE)      # five minutes, overridable
    assert re.search(r'^PIDFILE="\$\{PIDFILE:-/run/', SCRIPT, re.MULTILINE)
    apply_fn = re.search(r"^apply\(\)\s*\{.*?^\}", SCRIPT, re.MULTILINE | re.DOTALL).group(0)
    assert apply_fn.index("arm_revert") < apply_fn.index("remove_rules") < apply_fn.index("add DOCKER-USER")
    assert "trap on_apply_error ERR" in apply_fn and "confirm" in apply_fn
    assert re.search(r"^set -E", SCRIPT, re.MULTILINE)                 # so the ERR trap also fires inside add()
    revert = re.search(r"^revert_after\(\)\s*\{.*?^\}", SCRIPT, re.MULTILINE | re.DOTALL).group(0)
    assert re.search(r'sleep\s+"\$ROLLBACK_S"', revert)
    assert revert.index('rm -f "$PIDFILE"') < revert.index("remove_rules") < revert.index("logger")
    assert re.search(r"^\s*_revert\)", SCRIPT, re.MULTILINE)


def test_firewall_script_never_names_a_personal_host():
    assert not re.search(r"\.ts\.net|/home/|/Users/|\b100\.(?!64\.0\.0/10)\d+\.\d+\.\d+", SCRIPT, re.IGNORECASE)


# --- firewall.sh against a fake, stateful iptables (tmp dir only: no real iptables, no sudo) ---------------------------

FAKE_IPTABLES = r"""#!/bin/bash
# A stateful stand-in for iptables: one file per chain in $FAKE_IPT_DIR, one rule per line.
if [ "$1" = -w ]; then shift; fi
dir="$FAKE_IPT_DIR"
op="$1"; shift
case "$op" in
    -n) exit 0 ;;
    -S) chain="$1"
        if [ -f "$dir/$chain" ]; then
            while IFS= read -r line; do echo "-A $chain $line"; done < "$dir/$chain"
        fi
        exit 0 ;;
    -I) chain="$1"; pos="$2"; shift 2; rule="$*"
        n=$(cat "$dir/_inserts" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$dir/_inserts"
        if [ "$n" = "${FAKE_IPT_FAIL_ON_INSERT:-0}" ]; then exit 1; fi
        touch "$dir/$chain"
        awk -v pos="$pos" -v rule="$rule" 'NR == pos {print rule} {print} END {if (NR < pos) print rule}' \
            "$dir/$chain" > "$dir/$chain.new" && mv "$dir/$chain.new" "$dir/$chain" ;;
    -D) chain="$1"; shift; rule="$*"
        if [ -n "$FAKE_IPT_FAIL_DELETE" ]; then exit 1; fi
        grep -qxF -- "$rule" "$dir/$chain" 2>/dev/null || exit 1
        awk -v rule="$rule" '!done && $0 == rule {done = 1; next} {print}' \
            "$dir/$chain" > "$dir/$chain.new" && mv "$dir/$chain.new" "$dir/$chain" ;;
    *) exit 3 ;;
esac
"""

FOREIGN_DOCKER_USER = "-j RETURN"
FOREIGN_INPUT = "-p tcp -m tcp --dport 22 -j ACCEPT"


class Sandbox:
    def __init__(self, tmp_path, rollback_s="30"):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "iptables").write_text(FAKE_IPTABLES)
        (bin_dir / "id").write_text('#!/bin/sh\necho "${FAKE_UID:-0}"\n')
        for name in ("iptables", "id"):
            (bin_dir / name).chmod(0o755)
        self.dir = tmp_path / "ipt"
        self.dir.mkdir()
        (self.dir / "DOCKER-USER").write_text(FOREIGN_DOCKER_USER + "\n")
        (self.dir / "INPUT").write_text(FOREIGN_INPUT + "\n")
        (tmp_path / "resolv.conf").write_text("nameserver 127.0.0.53\nnameserver 192.168.1.1\nnameserver ::1\n")
        self.pidfile = tmp_path / "fw.pid"
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_IPT_DIR": str(self.dir),
                    "ROLLBACK_S": rollback_s, "PIDFILE": str(self.pidfile),
                    "RESOLV_FILES": str(tmp_path / "resolv.conf")}
        self.env.pop("ALLOW_DNS", None)

    def run(self, *args, **env):
        return subprocess.run(["bash", str(FIREWALL), *args], env={**self.env, **env}, capture_output=True,
                              text=True, timeout=30, stdin=subprocess.DEVNULL)

    def rules(self, chain):
        return (self.dir / chain).read_text().splitlines()

    def tagged(self, chain):
        return [r for r in self.rules(chain) if "--comment morning-brief-writer" in r]

    def snapshot(self):
        return {c: self.rules(c) for c in ("DOCKER-USER", "INPUT")}


@pytest.fixture
def sandbox(tmp_path):
    box = Sandbox(tmp_path)
    yield box
    box.run("remove")          # stops any timer still sleeping, however the test ended


@pytest.fixture
def quick_sandbox(tmp_path):
    box = Sandbox(tmp_path, rollback_s="2")      # long enough for a whole apply, short enough for a test
    yield box
    box.run("remove")


def test_apply_inserts_tagged_rules_in_order_above_foreign_ones(sandbox):
    result = sandbox.run()
    assert result.returncode == 0, result.stderr
    user = sandbox.rules("DOCKER-USER")
    n = len(sandbox.tagged("DOCKER-USER"))
    assert user[n:] == [FOREIGN_DOCKER_USER] and len(user) == n + 1       # ours on top, theirs untouched below
    joined = "\n".join(user)
    positions = [joined.index(x) for x in ("-d 172.29.40.10 ", "-d 192.168.1.1 -p udp", "ESTABLISHED,RELATED",
                                          "-d 172.16.0.0/12", "-d 10.0.0.0/8", "-d 192.168.0.0/16",
                                          "-d 100.64.0.0/10", "-d 169.254.0.0/16")]
    assert positions == sorted(positions)
    assert user[n - 1].startswith("-s 172.29.40.0/24 -j ACCEPT")
    assert "127.0.0.53" not in joined and "::1" not in joined              # loopback resolvers are not allowed
    assert "192.168.1.1" in result.stdout                                  # and the chosen resolvers are echoed
    inp = sandbox.rules("INPUT")
    assert inp[-1] == FOREIGN_INPUT and inp[-2].startswith("-s 172.29.40.0/24 -j DROP")
    assert "ESTABLISHED,RELATED" in inp[-3] and all("morning-brief-writer" in r for r in inp[:-1])
    assert sandbox.pidfile.exists()                                         # the revert timer is running


def test_apply_twice_leaves_no_duplicates_and_remove_cleans_up(sandbox):
    assert sandbox.run().returncode == 0
    first = sandbox.snapshot()
    assert sandbox.run().returncode == 0
    assert sandbox.snapshot() == first
    assert sandbox.run("remove").returncode == 0
    assert sandbox.rules("DOCKER-USER") == [FOREIGN_DOCKER_USER] and sandbox.rules("INPUT") == [FOREIGN_INPUT]
    assert not sandbox.pidfile.exists()


def test_allow_dns_adds_extra_resolvers(sandbox):
    assert sandbox.run(ALLOW_DNS="10.9.8.7").returncode == 0
    text = "\n".join(sandbox.rules("DOCKER-USER") + sandbox.rules("INPUT"))
    assert "10.9.8.7" in text and "192.168.1.1" in text


def test_confirm_keeps_the_rules_past_the_timeout(quick_sandbox):
    sandbox = quick_sandbox
    assert sandbox.run().returncode == 0
    before = sandbox.rules("DOCKER-USER")
    result = sandbox.run("confirm")
    assert result.returncode == 0 and not sandbox.pidfile.exists()
    time.sleep(2.6)                                                         # longer than ROLLBACK_S=2
    assert sandbox.rules("DOCKER-USER") == before and sandbox.tagged("INPUT")
    assert sandbox.run("confirm").returncode == 1                           # nothing left to confirm


def test_unconfirmed_apply_reverts_only_our_rules(quick_sandbox):
    sandbox = quick_sandbox
    assert sandbox.run().returncode == 0 and sandbox.tagged("DOCKER-USER")
    deadline = time.time() + 8
    while time.time() < deadline and (sandbox.tagged("DOCKER-USER") or sandbox.tagged("INPUT")):
        time.sleep(0.1)
    assert sandbox.rules("DOCKER-USER") == [FOREIGN_DOCKER_USER] and sandbox.rules("INPUT") == [FOREIGN_INPUT]
    assert not sandbox.pidfile.exists()


def test_a_failure_part_way_through_apply_removes_what_was_added(sandbox):
    result = sandbox.run(FAKE_IPT_FAIL_ON_INSERT="4")
    assert result.returncode != 0 and "removing the rules added so far" in result.stderr
    assert sandbox.rules("DOCKER-USER") == [FOREIGN_DOCKER_USER] and sandbox.rules("INPUT") == [FOREIGN_INPUT]
    assert not sandbox.pidfile.exists()


@pytest.mark.parametrize("bad", ["abc", "-5", "1.5", "5; touch /tmp/should-not-exist", "5 6", "0", "08", "09", "007"])
def test_a_non_numeric_rollback_is_refused_before_anything_changes(sandbox, bad):
    before = sandbox.snapshot()
    result = sandbox.run(ROLLBACK_S=bad)
    assert result.returncode == 2 and "ROLLBACK_S" in result.stderr
    assert sandbox.snapshot() == before and not sandbox.pidfile.exists()


def test_the_timeout_message_is_in_minutes_or_in_seconds_when_under_a_minute(sandbox):
    assert "automatically in 5 minutes" in sandbox.run(ROLLBACK_S="300").stdout
    short = sandbox.run(ROLLBACK_S="45")
    assert short.returncode == 0 and "automatically in 45 seconds" in short.stdout and "0 minutes" not in short.stdout


def test_remove_says_so_and_fails_when_tagged_rules_remain(sandbox):
    assert sandbox.run().returncode == 0
    result = sandbox.run("remove", FAKE_IPT_FAIL_DELETE="1")          # every delete is refused
    assert result.returncode == 1 and "could not be removed" in result.stderr
    assert "Rules removed" not in result.stdout and sandbox.tagged("DOCKER-USER")
    assert sandbox.run("remove").returncode == 0 and not sandbox.tagged("DOCKER-USER")


def test_a_failed_cleanup_keeps_the_timer_armed_and_warns(sandbox):
    result = sandbox.run(FAKE_IPT_FAIL_ON_INSERT="4", FAKE_IPT_FAIL_DELETE="1")
    assert result.returncode != 0
    assert "WARNING" in result.stderr and "half-applied" in result.stderr
    assert "Nothing is left half-applied" not in result.stderr
    assert sandbox.pidfile.exists() and sandbox.tagged("DOCKER-USER")     # the revert will retry
    assert sandbox.run("remove").returncode == 0 and not sandbox.pidfile.exists()


def test_dns_is_detected_from_every_resolv_conf_not_just_the_first(sandbox, tmp_path):
    (tmp_path / "second.conf").write_text("nameserver 10.77.0.2\nnameserver 192.168.1.1\n")
    resolv = f"{tmp_path / 'resolv.conf'} {tmp_path / 'second.conf'} {tmp_path / 'missing.conf'}"
    result = sandbox.run(RESOLV_FILES=resolv)
    assert result.returncode == 0, result.stderr
    text = "\n".join(sandbox.rules("DOCKER-USER"))
    assert "-d 192.168.1.1 -p udp" in text and "-d 10.77.0.2 -p udp" in text
    assert text.count("-d 192.168.1.1 -p udp") == 1                    # the union has no duplicates


def test_without_root_nothing_changes(sandbox):
    before = sandbox.snapshot()
    result = sandbox.run(FAKE_UID="1000")
    assert result.returncode == 1 and "sudo" in result.stderr
    assert sandbox.snapshot() == before and not sandbox.pidfile.exists()


# --- check_isolation --------------------------------------------------------------------------------------------------

ENV = {"HOST_TAILNET_IP": "100.64.1.2", "LAN_PROBE": "192.168.1.10:80", "OTHER_CONTAINER_PROBE": "other:5432"}


def probes(open_tcp=(), app_status=401, dns_ok=True):
    """Injected socket, HTTP and DNS functions; open_tcp is the set of (host, port) that connect."""
    calls = []

    def tcp(host, port, timeout):
        calls.append((host, port, timeout))
        return (host, port) in open_tcp

    return dict(tcp=tcp, http=lambda url, timeout: app_status, dns=lambda name: dns_ok), calls


GOOD = {("1.1.1.1", 443)}


def run(env, **kw):
    lines = []
    code = check_isolation.run(env, out=lines.append, **kw)
    return code, "\n".join(lines)


def test_check_isolation_all_expectations_met_exits_zero():
    fns, calls = probes(open_tcp=GOOD)
    code, text = run(ENV, **fns)
    assert code == 0 and "FAIL" not in text
    probed = {(h, p) for h, p, _ in calls}
    assert {("1.1.1.1", 443), ("172.29.40.1", 22), ("172.29.40.1", 8430), ("100.64.1.2", 8430),
            ("192.168.1.10", 80), ("other", 5432)} <= probed
    assert all(t <= 3 for _, _, t in calls)


def test_check_isolation_accepts_app_status_200_or_401_only():
    for status, expected in ((200, 0), (401, 0), (403, 1), (None, 1), (500, 1)):
        fns, _ = probes(open_tcp=GOOD, app_status=status)
        assert run(ENV, **fns)[0] == expected, status


def test_check_isolation_fails_when_a_forbidden_target_is_reachable():
    for target in [("172.29.40.1", 22), ("172.29.40.1", 8430), ("100.64.1.2", 8430), ("192.168.1.10", 80),
                   ("other", 5432)]:
        fns, _ = probes(open_tcp=GOOD | {target})
        code, text = run(ENV, **fns)
        assert code != 0 and "FAIL" in text, target


def test_check_isolation_fails_when_the_internet_or_dns_is_unreachable():
    fns, _ = probes(open_tcp=set())
    assert run(ENV, **fns)[0] != 0
    fns, _ = probes(open_tcp=GOOD, dns_ok=False)
    assert run(ENV, **fns)[0] != 0


def test_check_isolation_skips_unset_probes_with_a_notice():
    fns, calls = probes(open_tcp=GOOD)
    code, text = run({}, **fns)
    assert code == 0
    for name in ("HOST_TAILNET_IP", "LAN_PROBE", "OTHER_CONTAINER_PROBE"):
        assert name in text and "SKIP" in text
    assert {h for h, _, _ in calls} == {"1.1.1.1", "172.29.40.1"}


def test_check_isolation_treats_a_malformed_probe_as_a_failure():
    fns, _ = probes(open_tcp=GOOD)
    code, text = run({"LAN_PROBE": "no-port-here"}, **fns)
    assert code != 0 and "LAN_PROBE" in text


def test_check_isolation_says_when_a_blocked_probe_was_refused_not_dropped():
    fns, _ = probes(open_tcp=GOOD)
    real = fns["tcp"]
    fns["tcp"] = lambda h, p, t: "refused" if (h, p) == ("172.29.40.1", 22) else real(h, p, t)
    code, text = run(ENV, **fns)
    assert code == 0 and "refused: the packet reached the host" in text


def test_check_isolation_dns_failure_prints_an_allow_dns_hint():
    fns, _ = probes(open_tcp=GOOD, dns_ok=False)
    code, text = run(ENV, **fns)
    assert code != 0 and "ALLOW_DNS" in text


def test_check_isolation_rejects_out_of_range_and_odd_ports_without_probing():
    for bad in ("host:99999", "host:0", "host:\u00b2", "host:", ":80", "host:-1"):
        fns, calls = probes(open_tcp=GOOD)
        code, text = run({"LAN_PROBE": bad}, **fns)
        assert code != 0 and "LAN_PROBE" in text, bad
        assert ("host", 99999) not in {(h, p) for h, p, _ in calls}


def test_check_isolation_real_probes_swallow_odd_errors(monkeypatch):
    import http.client

    def boom(*a, **k):
        raise http.client.BadStatusLine("x")

    monkeypatch.setattr(check_isolation.urllib.request, "urlopen", boom)
    assert check_isolation.http_status("http://x", 1) is None
    assert check_isolation.tcp_connects("localhost", 99999, 1) == "timeout"      # OverflowError, not OSError
    assert check_isolation.tcp_connects("a\u0000b", 80, 1) == "timeout"


def test_check_isolation_is_stdlib_only():
    source = (ROOT / "writer_service" / "check_isolation.py").read_text()
    imports = set(re.findall(r"^(?:import|from)\s+(\w+)", source, re.MULTILINE))
    assert imports <= {"os", "socket", "sys", "urllib", "http", "__future__"}


# --- README -----------------------------------------------------------------------------------------------------------

def test_readme_mentions_the_one_time_steps():
    assert "## Server writer" in README
    section = README.split("## Server writer", 1)[1].split("\n## ", 1)[0]
    for needle in ("writer.env", "claude setup-token", "--profile writer", "firewall.sh", "check_isolation",
                   "chmod 600", "--exclude writer.env", "Known limits", "Writer: checked in", "2.24",
                   "docker network inspect", "re-run the isolation check"):
        assert needle in section, needle
    assert "WebFetch" in section and "before" in section.lower()
    assert "docker compose --profile writer exec" in section and "/opt/writer/writer_service/check_isolation.py" in section
    assert "ip route" in section and "ufw reload" in section and "reboot" in section and "ALLOW_DNS" in section


def test_readme_tells_the_truth_about_env_reload_and_the_daily_runs_exposure():
    section = README.split("## Server writer", 1)[1].split("\n## ", 1)[0]
    assert "docker compose --profile writer up -d writer" in section
    assert "restart writer" not in section and "restart the writer" not in section
    known = section.split("### Known limits", 1)[1]
    for needle in ("personal block", "arbitrary web pages", "same exposure", "public internet"):
        assert needle in known, needle
    assert "172.29.40.0/24" in section and "SUBNET" in section and "APP_IP" in section


def test_env_example_points_at_the_writer_env_example():
    assert "writer.env.example" in (ROOT / ".env.example").read_text()
