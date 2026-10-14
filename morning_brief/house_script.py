"""The House script: sentence templates over the day's facts. No Claude, no network; pure functions."""
from __future__ import annotations

from datetime import date

from .speech import Clip

_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
          "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"]


def num(n: int) -> str:
    return _WORDS[n] if 0 <= n <= 20 else str(n)


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


def chore_sentence(c: dict) -> str:
    who, name = c["who"], c["name"]
    if c["when"] == "today":
        return _sentence(f"{who} has {name} today" if who else f"Today: {name}")
    if c["when"] == "tomorrow":
        return _sentence(f"{who} has {name} tomorrow" if who else f"Tomorrow: {name}")
    ago = "yesterday" if c["days_over"] == 1 else f"{num(c['days_over'])} days ago"
    return _sentence(f"Still open from {ago}: {name}" + (f", {who}'s turn" if who else ""))


def upkeep_sentence(u: dict) -> str:
    label = u["label"]
    if u["state"] == "due":
        return _sentence(f"Due today: {label}")
    if u["state"] == "overdue":
        if u["days"] > 20:  # a long-neglected yearly item: a day count read aloud would be noise
            return _sentence(f"Overdue: {label}")
        return _sentence(f"Overdue by {num(u['days'])} {_plural(u['days'], 'day')}: {label}")
    return _sentence(f"Due tomorrow: {label}" if u["days"] == 1 else f"Due in {num(u['days'])} days: {label}")


def event_sentences(e: dict) -> list[str]:
    title = e["title"]
    if e["days"] == 0:
        head = f"Tonight: {title}"
    elif e["days"] == 1:
        head = f"Tomorrow: {title}"
    else:
        head = f"{date.fromisoformat(e['day']):%A}, in {num(e['days'])} days: {title}"
    parts = [_sentence(head)]
    if e["host"]:
        parts.append(_sentence(f"{e['host']} is hosting"))
    if e["prep_open"]:
        parts.append(_sentence("Still to do: " + ", ".join(e["prep_open"])))
    return parts


def todo_sentence(t: dict) -> str:
    claimed = f", {t['who']} has it" if t["who"] else ", still unclaimed"
    lead = {"overdue": "Overdue: ", "today": "Due today: "}.get(t["state"], "")
    return _sentence(f"{lead}{t['text'].rstrip('.!? ')}{claimed}")


def note_sentence(n: dict) -> str:
    return _sentence(f"{n['who']} says: {n['text']}" if n["who"] else f"A note: {n['text']}")


def compose(facts: dict, today: date) -> dict:
    now_chores = [c for c in facts["chores"] if c["when"] in ("today", "overdue")]
    soon_chores = [c for c in facts["chores"] if c["when"] == "tomorrow"]
    todays = [chore_sentence(c) for c in now_chores]
    todays += [upkeep_sentence(u) for u in facts["upkeep"] if u["state"] in ("due", "overdue")]
    for e in facts["events"]:
        if e["days"] == 0:
            todays += event_sentences(e)
    coming = [chore_sentence(c) for c in soon_chores]
    coming += [upkeep_sentence(u) for u in facts["upkeep"] if u["state"] == "soon"]
    for e in facts["events"]:
        if e["days"] > 0:
            coming += event_sentences(e)
    sections: list[dict] = []
    if todays:
        sections.append({"heading": "Today", "kind": "text", "text": " ".join(todays)})
    if coming:
        sections.append({"heading": "Coming up", "kind": "text", "text": " ".join(coming)})
    if facts["todos"]:
        n = len(facts["todos"])
        lead = _sentence(f"There {'is' if n == 1 else 'are'} {num(n)} open {_plural(n, 'to-do')}")
        sections.append({"heading": "To-dos", "kind": "text",
                         "text": " ".join([lead] + [todo_sentence(t) for t in facts["todos"]])})
    if facts["notes"]:
        sections.append({"heading": "Notes", "kind": "text",
                         "text": " ".join(note_sentence(n) for n in facts["notes"])})
    for memo in facts["memos"]:
        sections.append({"heading": f"Voice note from {memo['who']}", "kind": "memo", "memo_id": memo["id"]})
    return {"title": f"House brief: {today:%A}, {today:%B} {today.day}",
            "intro": f"Good morning, house. Here's what's on for {today:%A}.",
            "sections": sections, "outro": "That's the house for today."}


def passages(script: dict, clips: dict[int, Clip]) -> tuple[list, list[int]]:
    """What to speak, in order, and the passage index where each section starts (for chapter marks).

    `clips` must contain every memo id used in the script; callers filter out unusable memos first."""
    out: list = [script["intro"]]
    firsts: list[int] = []
    for section in script["sections"]:
        firsts.append(len(out))
        if section["kind"] == "memo":
            out.append(f"{section['heading']}.")
            out.append(clips[section["memo_id"]])
        else:
            out.append(f"{section['heading']}. {section['text']}")
    out.append(script["outro"])
    return out, firsts


def chapters(script: dict, starts: list[float], firsts: list[int]) -> list[tuple[str, float]]:
    marks = [("Introduction", 0.0)]
    marks += [(section["heading"], starts[firsts[n]]) for n, section in enumerate(script["sections"])]
    marks.append(("Wrap-up", starts[-1]))
    return marks


def word_count(script: dict) -> int:
    texts = [script["intro"], script["outro"]] + [s["text"] for s in script["sections"] if s["kind"] == "text"]
    return sum(len(t.split()) for t in texts)


def describe(script: dict) -> str:
    parts = []
    for section in script["sections"]:
        if section["kind"] == "text":
            parts.append(f"{section['heading']}\n{section['text']}")
        else:
            parts.append(f"{section['heading']} (recording)")
    return "\n\n".join(parts) if parts else script["intro"]
