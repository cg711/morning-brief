"""Fill in the deep-dive worker prompt for your machine and print the setup steps.

Usage:
    python3 scripts/render_worker_prompt.py --server http://100.64.0.10:8430

Writes worker/deep-dive-task.md (gitignored). Paste its contents into a new scheduled task
in the Claude desktop app (hourly is a good cadence).
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "worker" / "deep-dive-task.template.md"
OUT = ROOT / "worker" / "deep-dive-task.md"


def render(server: str, home: Path) -> str:
    text = TEMPLATE.read_text()
    values = {
        "{{SERVER_URL}}": server.rstrip("/"),
        "{{HEADER_FILE}}": str(home / ".config" / "morning-brief" / "worker-header"),
        "{{CACHE_DIR}}": str(home / ".cache" / "morning-brief"),
    }
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", required=True, help="URL the Mac uses to reach the app, e.g. http://100.64.0.10:8430")
    parser.add_argument("--home", default=str(Path.home()), help="home directory on the Mac running the worker")
    args = parser.parse_args()
    home = Path(args.home)
    OUT.write_text(render(args.server, home))
    header = home / ".config" / "morning-brief" / "worker-header"
    cache = home / ".cache" / "morning-brief"
    host = args.server.split("://", 1)[-1].rstrip("/")
    print(f"Wrote {OUT.relative_to(ROOT)}\n")
    print("1. Put the server's WORKER_TOKEN in the header file (never commit it):")
    print(f"   mkdir -p -m 700 {header.parent} {cache}")
    print(f"   (umask 077; printf 'Authorization: Bearer %s\\n' 'PASTE_WORKER_TOKEN' > {header})\n")
    print("2. Allow the unattended run's tools in ~/.claude/settings.json → permissions.allow:")
    for rule in ["WebSearch", "WebFetch",
                 f"Bash(mkdir -p {cache})",
                 f"Bash(curl *{host}/api/deep-dives/*)",
                 f"Bash(python3 -c \\\"import json; s=json.load(open('{cache}/script-*)",
                 f"Edit(/{cache}/**)", f"Read(/{cache}/**)", f"Read(/{header})",
                 f"Read(/{home}/.claude/projects/**/tool-results/**)"]:
        print(f'   "{rule}",')
    print("\n3. Create an hourly scheduled task in the Claude desktop app with the contents of "
          f"{OUT.relative_to(ROOT)} and press Run now once to confirm it works.")


if __name__ == "__main__":
    main()
