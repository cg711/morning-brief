from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .articles import MIN_FULL_WORDS
from .config import TZ
from .models import SEGMENTS, Item, Pick, Story

log = logging.getLogger(__name__)

MIN_WORDS, MAX_WORDS = 300, 750
MAX_STORIES = 8
MAX_TOKENS = 16000
FULL_STORY_WORDS = 85
SUMMARY_STORY_WORDS = 25
FRAME_WORDS = 30  # intro + outro
TARGET_MIN, TARGET_MAX = 450, 650


class WriterError(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    on_add: Callable[[int, int], None] | None = field(default=None, repr=False, compare=False)


SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "story_id": {"type": "string"},
                    "segment": {"type": "string", "enum": list(SEGMENTS)},
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["story_id", "segment", "item_ids", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stories"],
    "additionalProperties": False,
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment": {"type": "string", "enum": list(SEGMENTS)},
                    "headline": {"type": "string"},
                    "text": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["segment", "headline", "text", "item_ids"],
                "additionalProperties": False,
            },
        },
        "outro": {"type": "string"},
    },
    "required": ["intro", "segments", "outro"],
    "additionalProperties": False,
}

SELECT_SYSTEM = """You are the editor of a short daily audio news briefing for a listener in {location}.
From the candidate items, pick 6 to 8 stories: about 3 national or world headlines, then 1 or 2 each for tech and science, business, and local news for {location}.
Prefer significance first, then recency. When several items cover the same story, make it one story and list every item id that covers it.
Skip a story the listener heard in the previous episode unless there is a genuine new development.
If a segment has nothing worthwhile, give it no stories.
Each candidate says whether its full article text is available ("full") or only a short summary ("summary"). Prefer stories with at least one full-text item; pick a summary-only story only when it is clearly among the day's most important."""

WRITE_SYSTEM = """You write the script for a spoken daily news briefing that a text-to-speech voice reads aloud.
The script must be 450 to 650 words in total. Give each full-text story 70 to 100 words and each summary-only story 20 to 35 words. Length comes from detail in the full-text stories, never from padding or outside knowledge.
- Use only facts stated in the provided story text. Do not add background from memory.
- Attribute every story to its source out loud, for example "MPR News reports".
- Stories marked summary-only get one or two sentences. Give full-text stories more depth.
- Use the publish times to choose time words such as last night, yesterday afternoon, or this morning. Call something today only if it was published today.
- Write for the ear: spell out numbers, dates, units and abbreviations the way a newsreader says them. No URLs, lists or markdown.
- The intro greets the listener with the weekday and date. The outro is one short sign-off line.
- Order the segments: headlines, tech, business, local."""


def _local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %b %-d %-I:%M %p")


def _text_available(item: Item) -> str:
    """'full' when the feed carries the article or the page may be fetched; otherwise 'summary'."""
    return "full" if len(item.body.split()) >= MIN_FULL_WORDS or item.fetch_pages else "summary"


def _call(client, model: str, system: str, user: str, schema: dict, usage: Usage) -> dict:
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    delta_in, delta_out = response.usage.input_tokens, response.usage.output_tokens
    usage.input_tokens += delta_in
    usage.output_tokens += delta_out
    if usage.on_add:
        usage.on_add(delta_in, delta_out)
    if response.stop_reason in ("refusal", "max_tokens"):
        raise WriterError(f"Claude stopped early: {response.stop_reason}")
    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise WriterError("Claude returned no text block")
    return json.loads(text)


def format_candidates(items: list[Item]) -> str:
    lines = []
    for it in items:
        summary = " ".join(it.summary.split()[:60])
        lines.append(
            f"{it.id} | {it.segment} | {it.source} | {_local(it.published_at)} | "
            f"{_text_available(it)} | {it.title} | {summary}"
        )
    return "\n".join(lines)


def select_stories(client, model: str, items: list[Item], previous_headlines: list[str],
                   now: datetime, usage: Usage, *, location: str = "Minneapolis") -> list[Pick]:
    previous = "\n".join(f"- {h}" for h in previous_headlines) or "- (no previous episode)"
    user = (
        f"Now: {_local(now)} Central.\n\n"
        f"The previous episode covered:\n{previous}\n\n"
        f"Candidates (id | segment | source | published | text | title | summary):\n{format_candidates(items)}"
    )
    data = _call(client, model, SELECT_SYSTEM.format(location=location), user, SELECT_SCHEMA, usage)
    known = {it.id for it in items}
    picks = []
    for s in data["stories"]:
        ids = [i for i in s["item_ids"] if i in known]
        if ids:
            picks.append(Pick(s["story_id"], s["segment"], ids, s["reason"]))
    if not picks:
        raise WriterError("selection returned no usable stories")
    return picks[:MAX_STORIES]


def format_stories(stories: list[Story]) -> str:
    blocks = []
    for s in stories:
        kind = "full text" if s.full else "summary-only"
        ids = ", ".join(it.id for it in s.items)
        published = ", ".join(f"{it.source} {_local(it.published_at)}" for it in s.items)
        blocks.append(f"### {s.story_id} [{s.segment}] ({kind})\nitem_ids: {ids}\npublished: {published}\n\n{s.text}")
    return "\n\n".join(blocks)


def target_words(stories: list[Story]) -> int:
    full = sum(1 for s in stories if s.full)
    raw = full * FULL_STORY_WORDS + (len(stories) - full) * SUMMARY_STORY_WORDS + FRAME_WORDS
    return max(TARGET_MIN, min(TARGET_MAX, raw))


def script_word_count(script: dict) -> int:
    texts = [script["intro"], *(seg["text"] for seg in script["segments"]), script["outro"]]
    return sum(len(t.split()) for t in texts)


def validate_script(script: dict, known_ids: set[str]) -> list[str]:
    problems = []
    words = script_word_count(script)
    if words < MIN_WORDS:
        problems.append(
            f"the script is {words} words, which is too short; it must be between {MIN_WORDS} and {MAX_WORDS} "
            "— add detail drawn from the full-text stories"
        )
    elif words > MAX_WORDS:
        problems.append(
            f"the script is {words} words, which is too long; it must be between {MIN_WORDS} and {MAX_WORDS} "
            "— tighten the summary-only stories first"
        )
    if not script["segments"]:
        problems.append("there are no segments")
    for n, seg in enumerate(script["segments"], start=1):
        if not seg["text"].strip():
            problems.append(f"segment {n} has empty text")
        if not seg["item_ids"]:
            problems.append(f"segment {n} cites no item ids")
        unknown = [i for i in seg["item_ids"] if i not in known_ids]
        if unknown:
            problems.append(f"segment {n} cites unknown item ids {unknown}")
    return problems


def write_script(client, model: str, stories: list[Story], now: datetime, usage: Usage) -> dict:
    known = {it.id for s in stories for it in s.items}
    full = sum(1 for s in stories if s.full)
    base = (
        f"Now: {now.astimezone(TZ):%A, %B %-d, %Y, %-I:%M %p} Central.\n"
        f"Target length: about {target_words(stories)} words "
        f"({full} full-text stories, {len(stories) - full} summary-only).\n\n"
        f"{format_stories(stories)}"
    )
    problems: list[str] = []
    for attempt in range(1, 3):
        user = base if not problems else (
            base + "\n\nYour previous draft was rejected because " + "; ".join(problems)
            + ". Fix that without adding facts that are not in the story text."
        )
        script = _call(client, model, WRITE_SYSTEM, user, SCRIPT_SCHEMA, usage)
        problems = validate_script(script, known)
        log.info("draft %d: %d words, problems: %s", attempt, script_word_count(script), problems or "none")
        if not problems:
            return script
    raise WriterError("script rejected twice: " + "; ".join(problems))
