"""Turning something shared from the phone into a deep-dive topic or link (pure helpers for /api/inbox)."""
from __future__ import annotations

from urllib.parse import urlsplit

TOPIC_MAX = 200


def _is_link(text: str) -> bool:
    if any(ch.isspace() for ch in text) or not text.lower().startswith(("http://", "https://")):
        return False
    try:
        return bool(urlsplit(text).hostname)
    except ValueError:
        return False


def _cut(line: str) -> str:
    if len(line) > TOPIC_MAX:
        cut = line[:TOPIC_MAX - 1]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0].rstrip()
        line = cut + "…"
    return line


def parse_input(text: str) -> tuple[str, str | None]:
    """("", url) for a lone http(s) link; otherwise (first non-empty line, None), cut to TOPIC_MAX at a word.

    A share of a headline plus a link together (joined by newlines) keeps both: if exactly one line is a
    lone link, that line becomes the url and the topic is the first other non-empty line.
    """
    text = text.strip()
    if _is_link(text):
        return "", text
    lines = [ln.strip() for ln in text.splitlines()]
    link_lines = [ln for ln in lines if _is_link(ln)]
    other_lines = lines if len(link_lines) != 1 else [ln for ln in lines if ln and ln != link_lines[0]]
    line = next((" ".join(ln.split()) for ln in other_lines if ln.strip()), "")
    url = link_lines[0] if len(link_lines) == 1 else None
    return _cut(line), url


def parse_note(text: str) -> tuple[str, str | None]:
    """For a brief note: when one line is a lone link, (the other lines joined, link); otherwise (all text, None)."""
    _, url = parse_input(text)
    kept = [line.strip() for line in text.strip().splitlines() if line.strip() and line.strip() != url]
    return " ".join(" ".join(kept).split()), url


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
