"""Fill in the worker prompts for your machine and print the setup steps.

Usage:
    python3 scripts/render_worker_prompt.py --server http://100.64.0.10:8430

Writes worker/deep-dive-task.md and worker/daily-brief-task.md (both gitignored). Paste each into its own
scheduled task in the Claude desktop app: the deep-dive task hourly, the daily-brief task shortly after
RUN_AT, with a retry before READY_BY (e.g. cron "5,45 7,8 * * *" for RUN_AT 07:30 / READY_BY 08:30).
"""
from __future__ import annotations

import argparse
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = {"deep-dive-task.md": ROOT / "worker" / "deep-dive-task.template.md",
             "daily-brief-task.md": ROOT / "worker" / "daily-brief-task.template.md"}


def render(template: Path, server: str, home: Path) -> str:
    text = template.read_text()
    values = {
        "{{SERVER_URL}}": server.rstrip("/"),
        "{{HEADER_FILE}}": str(home / ".config" / "morning-brief" / "worker-header"),
        "{{CACHE_DIR}}": str(home / ".cache" / "morning-brief"),
    }
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


def allow_rules(host: str, home: Path) -> list[str]:
    """The permission rules an unattended run needs: the tools, the app's worker endpoints on `host` (host:port),
    and the cache/header files under `home`."""
    header = home / ".config" / "morning-brief" / "worker-header"
    cache = home / ".cache" / "morning-brief"
    return ["WebSearch", "WebFetch",
            f"Bash(mkdir -p {cache})",
            f"Bash(curl *{host}/api/deep-dives/*)",
            f"Bash(curl *{host}/api/daily/*)",
            f"Bash(python3 -c \\\"import json; s=json.load(open('{cache}/script-*)",
            f"Edit(/{cache}/**)", f"Read(/{cache}/**)", f"Read(/{header})",
            f"Read(/{home}/.claude/projects/**/tool-results/**)"]


# --- the writer container's (much stricter) allow-list -------------------------------------------------------
# Claude Code Bash rules are globs where `*` matches any characters and everything else is literal. The Mac rules
# above start curl with a wildcard, so an injected agent could append arguments (`-d @/proc/1/environ`). The
# writer's rules are instead the whole command shapes the prompts give, with `*` only where the prompt has a
# placeholder (<date>, <id>, <item_id>).

_FENCE = re.compile(r"^[ \t]*```bash\n(.*?)^[ \t]*```", re.S | re.M)
_PLACEHOLDER = re.compile(r"<[a-z_]+>")
_PYTHON = re.compile(r'`(python3 -c ".*")`')


def command_shapes(template: Path, server: str, home: Path) -> list[str]:
    """Every curl and python3 command the rendered template tells Claude to run, as glob shapes (placeholders -> `*`).
    A multi-line curl command gives two shapes: as written (backslash-newline continuations) and joined on one line."""
    text = render(template, server, home)
    commands = [textwrap.dedent(block).strip() for block in _FENCE.findall(text) if block.lstrip().startswith("curl")]
    commands += [m.group(1) for m in _PYTHON.finditer(text)]
    shapes: list[str] = []
    for command in commands:
        for shape in (command, re.sub(r"\s*\\\n\s*", " ", command)):
            shape = _PLACEHOLDER.sub("*", shape)
            if shape not in shapes:
                shapes.append(shape)
    return shapes


def writer_allow_rules(host: str, home: Path, templates_dir: Path | None = None, scheme: str = "http") -> list[str]:
    """allow_rules for the unattended writer: no generic curl or python3 rule, only the prompts' exact commands."""
    templates_dir = templates_dir or ROOT / "worker"
    cache = home / ".cache" / "morning-brief"
    rules = ["WebSearch", "WebFetch", f"Bash(mkdir -p {cache})"]
    for name in ("daily-brief-task.template.md", "deep-dive-task.template.md"):
        rules += [f"Bash({shape})" for shape in command_shapes(templates_dir / name, f"{scheme}://{host}", home)]
    # No Read of the header file: only curl (through -H @file) needs it; Claude's Read tool must not see the token.
    rules += [f"Edit(/{cache}/**)", f"Read(/{cache}/**)", f"Read(/{home}/.claude/projects/**/tool-results/**)"]
    return list(dict.fromkeys(rules))


def writer_deny_rules(host: str, home: Path) -> list[str]:
    """Deny rules (checked before allow rules). The allow rules are the protection: after the prompt templates were
    changed to fixed file names, no allow rule has a wildcard before the URL and the python rule has none at all, so
    the only variable text is the path after `host/`. These deny rules are defence in depth for that tail (a glob `*`
    cannot exclude spaces): nothing after `host/` may contain whitespace, a shell metacharacter or a quote. The rest
    are cheap extra refusals (process environments, curl options that send files or redirect), not a complete list."""
    tail = [f"Bash(curl *{host}/*{ch}*)" for ch in (" ", "\t", "\n", "\r", ";", "&", "|", "<", ">", "'", '"', "\\", "`",
                                                      "$", "(", ")", "{", "}", "[", "]", "?", "~", "#", "!", "^", "%")]
    # curl normalises dot segments, so `.../items/../../x` would reach any path on the app: no `..` and no `%` (encoded
    # dots, slashes or backslashes, including double encoding) after the host.
    tail.append(f"Bash(curl *{host}/*..*)")
    # The token header, settings and rules live outside Claude's working directory (the cache dir). These make the
    # refusal explicit for Read (which Claude Code also applies to Grep and Glob) and Edit, and for the usual file
    # readers in Bash. No Bash(*worker-header*) rule: the allowed curl commands name that file.
    files = [f"Read(/{home}/.config/**)", f"Edit(/{home}/.config/**)", f"Read(/{home}/settings.json)",
             f"Edit(/{home}/settings.json)", f"Edit(/{home}/prompts/**)"]
    readers = [r for c in ("cat", "head", "tail", "less", "more", "strings", "od", "xxd", "hexdump", "base64", "grep", "egrep",
                           "rg", "sed", "awk", "cut", "sort", "uniq", "tr", "wc", "stat", "file", "find", "ls", "tee", "cp",
                           "mv", "dd", "nl", "tac", "diff", "cmp", "ln", "env", "printenv", "sh", "bash", "node")
               for r in (f"Bash({c})", f"Bash({c} *)")]
    options = [f"Bash(curl *{o}*)" for o in ("--connect-to", "--resolve", "--json", "--socks", "--proxy", "--cookie",
                                             "--dns-servers", "--interface", "--output-dir", "--data ", "--data-raw",
                                             "--data-urlencode", "--data-ascii", "--form", "--upload-file", "--config",
                                             "--next", "--unix-socket", "--oauth2-bearer", "--netrc", "--url")]
    options += ["Bash(curl * -d*)", "Bash(curl * -F*)", "Bash(curl * -T*)", "Bash(curl * -K*)", "Bash(curl * -x *)",
                "Bash(curl * -u*)", "Bash(curl * -b*)", "Bash(curl * -c *)", "Bash(curl * -L*)", "Bash(curl *--data-binary*--data-binary*)",
                "Bash(curl *://*://*)", "Bash(curl *-H *-H @*-H @*)"]
    return (tail + files + readers + options +
            ["Bash(*/proc*)", "Bash(*environ*)", "Read(//proc/**)", "Read(//sys/**)", "Read(//etc/**)",
             "Bash(python3 *__import__*)", "Bash(python3 *import os*)", "Bash(python3 *import sys*)",
             "Bash(python3 *subprocess*)", "Bash(python3 *exec*)", "Bash(python3 *eval*)", "Bash(python3 *compile*)",
             "Bash(python3 *socket*)", "Bash(python3 *urllib*)", "Bash(python3 *__*)", "Bash(python3 *;*;*;*)"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", required=True, help="URL the Mac uses to reach the app, e.g. http://100.64.0.10:8430")
    parser.add_argument("--home", default=str(Path.home()), help="home directory on the Mac running the worker")
    parser.add_argument("--out-dir", default=str(ROOT / "worker"), help="where to write the rendered prompts")
    args = parser.parse_args()
    home, out_dir = Path(args.home), Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, template in TEMPLATES.items():
        (out_dir / name).write_text(render(template, args.server, home))
        print(f"Wrote {out_dir / name}")
    header = home / ".config" / "morning-brief" / "worker-header"
    cache = home / ".cache" / "morning-brief"
    host = args.server.split("://", 1)[-1].rstrip("/")
    print("\n1. Put the server's WORKER_TOKEN in the header file (never commit it):")
    print(f"   mkdir -p -m 700 {header.parent} {cache}")
    print(f"   (umask 077; printf 'Authorization: Bearer %s\\n' 'PASTE_WORKER_TOKEN' > {header})\n")
    print("2. Allow the unattended runs' tools in ~/.claude/settings.json → permissions.allow:")
    for rule in allow_rules(host, home):
        print(f'   "{rule}",')
    print("\n3. In the Claude desktop app create two scheduled tasks: an hourly one with deep-dive-task.md, and one "
          "with daily-brief-task.md shortly after RUN_AT with a retry before READY_BY. Press Run now once on each.")


if __name__ == "__main__":
    main()
