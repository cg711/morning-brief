"""Fill in the worker prompts for your machine and print the setup steps.

Usage:
    python3 scripts/render_worker_prompt.py --server http://100.64.0.10:8430

Writes worker/deep-dive-task.md and worker/daily-brief-task.md (both gitignored). Paste each into its own
scheduled task in the Claude desktop app: the deep-dive task hourly, the daily-brief task shortly after
RUN_AT, with a retry before READY_BY (e.g. cron "5,45 7,8 * * *" for RUN_AT 07:30 / READY_BY 08:30).
"""
from __future__ import annotations

import argparse
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
    for rule in ["WebSearch", "WebFetch",
                 f"Bash(mkdir -p {cache})",
                 f"Bash(curl *{host}/api/deep-dives/*)",
                 f"Bash(curl *{host}/api/daily/*)",
                 f"Bash(python3 -c \\\"import json; s=json.load(open('{cache}/script-*)",
                 f"Edit(/{cache}/**)", f"Read(/{cache}/**)", f"Read(/{header})",
                 f"Read(/{home}/.claude/projects/**/tool-results/**)"]:
        print(f'   "{rule}",')
    print("\n3. In the Claude desktop app create two scheduled tasks: an hourly one with deep-dive-task.md, and one "
          "with daily-brief-task.md shortly after RUN_AT with a retry before READY_BY. Press Run now once on each.")


if __name__ == "__main__":
    main()
