#!/usr/bin/env bash
# Firewall for the morning-brief writer container. YOU run this, on the server, with sudo:
#
#   sudo bash writer_service/firewall.sh            apply the rules (reverts by itself after 5 minutes)
#   sudo bash writer_service/firewall.sh confirm    keep the rules: cancels the automatic revert
#   sudo bash writer_service/firewall.sh remove     take the rules out again
#
# What it does: traffic from the writer's Docker subnet may reach the app container and the public internet (and DNS),
# and nothing else private: not the host itself, not the LAN, not other Docker networks, not the tailnet.
#
# The rules are tagged with an iptables comment (morning-brief-writer), so running the script again first removes its
# own earlier rules and never touches anyone else's. The automatic revert is armed BEFORE any rule is added, and a
# failure while applying removes whatever was added (and if that fails too, the timer stays armed and you are told), so a half-applied firewall cannot lock you out: if you lose your
# SSH session, do nothing and the rules come out on their own after ROLLBACK_S seconds.
#
# The rules do not survive a reboot or a `ufw reload`. The writer container restarts on its own, so until you run this
# again its outbound traffic is not restricted. Run it again (and the isolation check) after either. README: "Server writer".
#
# DNS: the container's lookups leave through the host's resolvers, which may sit in a private range this script drops.
# So the nameservers in /run/systemd/resolve/resolv.conf and /etc/resolv.conf are allowed on port 53, plus any you
# add yourself:  sudo ALLOW_DNS="192.168.1.1" bash writer_service/firewall.sh
set -Eeuo pipefail

SUBNET="172.28.40.0/24"     # writer_net in docker-compose.yml
APP_IP="172.28.40.10"       # the morning-brief container's fixed address on writer_net
TAG="morning-brief-writer"
ROLLBACK_S="${ROLLBACK_S:-300}"   # automatic revert unless confirmed
PIDFILE="${PIDFILE:-/run/morning-brief-writer-firewall.pid}"
ALLOW_DNS="${ALLOW_DNS:-}"        # optional: extra resolver addresses, space separated
RESOLV_FILES="${RESOLV_FILES:-/run/systemd/resolve/resolv.conf /etc/resolv.conf}"
[[ $ROLLBACK_S =~ ^[1-9][0-9]*$ ]] || { echo "ROLLBACK_S must be a positive whole number of seconds (no leading zero), got: $ROLLBACK_S" >&2; exit 2; }
export ROLLBACK_S PIDFILE

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
user_pos=1                  # next insert position in DOCKER-USER
input_pos=1                 # next insert position in INPUT

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Run this with sudo: sudo bash $SELF ${1:-}" >&2
        exit 1
    fi
}

# add CHAIN RULE...: insert one tagged rule at the next position at the top of CHAIN, so rules keep the order below.
add() {
    local chain="$1" pos
    shift
    if [ "$chain" = INPUT ]; then
        pos=$input_pos
        input_pos=$((input_pos + 1))
    else
        pos=$user_pos
        user_pos=$((user_pos + 1))
    fi
    iptables -w -I "$chain" "$pos" "$@" -m comment --comment "$TAG"
}

# The tagged rules of one chain, each as the matching delete command's arguments.
tagged_deletes() {
    { iptables -w -S "$1" 2>/dev/null | grep -F -- "--comment $TAG" | sed 's/^-A /-D /'; } || true
}

# Delete every rule carrying our tag, from both chains, until none is left (at most 5 passes). Returns non-zero when
# tagged rules are still there afterwards, so callers can say so instead of claiming a clean state.
remove_rules() {
    local pass chain rule found
    for pass in 1 2 3 4 5; do
        found=0
        for chain in DOCKER-USER INPUT; do
            while read -r rule; do
                found=1
                # shellcheck disable=SC2086  # the rule text is meant to split into words
                iptables -w $rule || true
            done < <(tagged_deletes "$chain")
        done
        if [ "$found" -eq 0 ]; then
            break
        fi
    done
    if [ -n "$(tagged_deletes DOCKER-USER)$(tagged_deletes INPUT)" ]; then
        return 1
    fi
    return 0
}

# Stop a pending revert timer. Only kills a process that really is our timer (when /proc can tell us).
cancel_timer() {
    local pid
    if [ ! -f "$PIDFILE" ]; then
        return 0
    fi
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && { [ ! -r "/proc/$pid/cmdline" ] || tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q _revert; }; then
        kill "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
}

# Runs in the background: wait, then take the rules out unless `confirm` killed this first.
revert_after() {
    echo $$ > "$PIDFILE"    # the timer's own pid, so `confirm` kills the right process
    sleep "$ROLLBACK_S" &
    trap 'kill "$!" 2>/dev/null; exit 0' TERM   # `confirm` ends this timer and its sleep together
    wait "$!"
    trap - TERM
    rm -f "$PIDFILE"
    if remove_rules; then
        echo "morning-brief writer firewall: not confirmed, rules removed" | logger -t morning-brief-writer 2>/dev/null || true
    else
        echo "morning-brief writer firewall: not confirmed, but some rules could not be removed" | logger -t morning-brief-writer 2>/dev/null || true
    fi
}

# Start the timer and wait until it has written its pid. Exits (changing nothing) if it cannot be started.
arm_revert() {
    local i
    rm -f "$PIDFILE"
    if command -v setsid >/dev/null 2>&1; then
        nohup setsid bash "$SELF" _revert </dev/null >/dev/null 2>&1 &
    else
        nohup bash "$SELF" _revert </dev/null >/dev/null 2>&1 &
    fi
    for i in $(seq 1 50); do
        if [ -s "$PIDFILE" ]; then
            return 0
        fi
        sleep 0.1
    done
    echo "Could not start the automatic revert timer, so no rule was changed." >&2
    exit 1
}

# Non-loopback IPv4 nameservers of the host (from every resolv.conf in RESOLV_FILES), plus ALLOW_DNS, one per line.
detect_dns() {
    local f addr found=""
    for f in $RESOLV_FILES; do
        if [ ! -r "$f" ]; then
            continue
        fi
        for addr in $(awk '$1 == "nameserver" {print $2}' "$f"); do
            case "$addr" in
                127.*|*:*) ;;
                *) found="$found $addr" ;;
            esac
        done
    done
    for addr in $found $ALLOW_DNS; do
        echo "$addr"
    done | sort -u
}

on_apply_error() {
    trap - ERR
    echo "A firewall command failed: removing the rules added so far." >&2
    if remove_rules; then
        cancel_timer
        echo "Nothing is left half-applied." >&2
    else
        # Rules remain: leave the revert timer running, it will try again, and say what to do.
        echo "WARNING: some rules could not be removed, so the firewall is half-applied. The automatic revert is still" >&2
        echo "armed and will try again in $ROLLBACK_S seconds; or run: sudo bash $SELF remove" >&2
    fi
    exit 1
}

apply() {
    local dns dns_list
    if ! iptables -w -n -L DOCKER-USER >/dev/null 2>&1; then
        echo "No DOCKER-USER chain: is Docker running on this machine?" >&2
        exit 1
    fi
    dns_list="$(detect_dns)"
    cancel_timer
    arm_revert                      # first: from here on, anything that goes wrong comes out by itself
    trap on_apply_error ERR
    remove_rules

    # 1. The app is inside 172.16.0.0/12, so it must be allowed before that range is dropped.
    add DOCKER-USER -s "$SUBNET" -d "$APP_IP" -j ACCEPT
    for dns in $dns_list; do
        add DOCKER-USER -s "$SUBNET" -d "$dns" -p udp --dport 53 -j ACCEPT
        add DOCKER-USER -s "$SUBNET" -d "$dns" -p tcp --dport 53 -j ACCEPT
    done
    # 2. Replies on connections that are already allowed.
    add DOCKER-USER -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    # 3. Everything private is off limits.
    add DOCKER-USER -s "$SUBNET" -d 172.16.0.0/12 -j DROP
    add DOCKER-USER -s "$SUBNET" -d 10.0.0.0/8 -j DROP
    add DOCKER-USER -s "$SUBNET" -d 192.168.0.0/16 -j DROP
    add DOCKER-USER -s "$SUBNET" -d 100.64.0.0/10 -j DROP
    add DOCKER-USER -s "$SUBNET" -d 169.254.0.0/16 -j DROP
    # 4. The rest: the public internet.
    add DOCKER-USER -s "$SUBNET" -j ACCEPT

    # The host itself (Docker gateway, tailnet address, SSH): only replies to what the host started.
    for dns in $dns_list; do
        add INPUT -s "$SUBNET" -d "$dns" -p udp --dport 53 -j ACCEPT
        add INPUT -s "$SUBNET" -d "$dns" -p tcp --dport 53 -j ACCEPT
    done
    add INPUT -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    add INPUT -s "$SUBNET" -j DROP
    trap - ERR

    # shellcheck disable=SC2086  # word splitting puts the resolvers on one line
    echo "Rules applied. DNS resolvers allowed: " ${dns_list:-none}
    echo "Check the writer from inside its container:"
    echo "  docker compose --profile writer exec writer python3 /opt/writer/writer_service/check_isolation.py"
    echo
    if [ "$ROLLBACK_S" -ge 60 ]; then
        echo "They are removed automatically in $((ROLLBACK_S / 60)) minutes unless you confirm. To keep them, run:"
    else
        echo "They are removed automatically in $ROLLBACK_S seconds unless you confirm. To keep them, run:"
    fi
    echo "  sudo bash $SELF confirm"
}

confirm() {
    if [ ! -f "$PIDFILE" ]; then
        echo "Nothing to confirm: no automatic revert is pending (already confirmed, expired, or never applied)." >&2
        exit 1
    fi
    cancel_timer
    echo "Confirmed: the rules stay until you run 'remove', reboot or reload ufw."
}

case "${1:-apply}" in
    apply)   need_root apply;   apply ;;
    confirm) need_root confirm; confirm ;;
    remove)  need_root remove;  cancel_timer
             if remove_rules; then
                 echo "Rules removed."
             else
                 echo "Some rules could not be removed; list them with: iptables -S | grep $TAG" >&2
                 exit 1
             fi ;;
    _revert) need_root _revert; revert_after ;;
    *) echo "Usage: sudo bash $SELF [apply|confirm|remove]" >&2; exit 2 ;;
esac
