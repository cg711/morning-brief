"""Offline stand-in for anthropic.Anthropic, for free local end-to-end runs (CLAUDE_OFFLINE=1).

It reads the same prompts writer.py sends and returns schema-valid JSON built from the story
text itself, so every stage except Claude's own judgment and prose can be exercised for free.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

QUOTAS = {"headlines": 3, "tech": 2, "business": 1, "local": 2}
FULL_WORDS, SUMMARY_WORDS, FRAME_WORDS = 85, 25, 20
INTRO = "Good morning. This is an offline test episode of your brief."
OUTRO = "That's the offline test brief."
_HEADER = re.compile(r"^### (\S+) \[(\w+)\] \((full text|summary-only)\)$", re.M)
_TARGET = re.compile(r"Target length: about (\d+) words")
_SOURCE_PREFIX = re.compile(r"^\[([^\]]+)\]\s*")


class OfflineClaude:
    def __init__(self):
        self.messages = self

    def create(self, *, output_config, messages, **_):
        schema = output_config["format"]["schema"]
        prompt = messages[0]["content"]
        data = self._select(prompt) if "stories" in schema["properties"] else self._write(prompt)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(data))],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )

    def _select(self, prompt: str) -> dict:
        rows = []
        for line in prompt.split("Candidates (", 1)[1].splitlines()[1:]:
            parts = line.split(" | ", 5)
            if len(parts) == 6:
                item_id, segment, _source, _published, text, _rest = parts
                rows.append((item_id, segment, text))
        stories = []
        for segment, quota in QUOTAS.items():
            seg_rows = sorted((r for r in rows if r[1] == segment), key=lambda r: r[2] != "full")
            for item_id, _, _ in seg_rows[:quota]:
                stories.append({"story_id": f"s{len(stories) + 1}", "segment": segment,
                                "item_ids": [item_id], "reason": "offline pick"})
        return {"stories": stories}

    def _write(self, prompt: str) -> dict:
        headers = list(_HEADER.finditer(prompt))
        blocks = []
        for n, m in enumerate(headers):
            end = headers[n + 1].start() if n + 1 < len(headers) else len(prompt)
            lines = prompt[m.end():end].strip().split("\n")
            ids = lines[0].removeprefix("item_ids: ").split(", ")
            text = "\n".join(lines[2:]).strip()
            blocks.append((m.group(2), m.group(3) == "full text", ids, text))
        found = _TARGET.search(prompt)
        target = int(found.group(1)) if found else 450
        n_full = sum(1 for b in blocks if b[1])
        n_summary = len(blocks) - n_full
        per_full = max(FULL_WORDS, (target - FRAME_WORDS - SUMMARY_WORDS * n_summary) // max(n_full, 1))
        segments = []
        for segment, full, ids, text in blocks:
            source_match = _SOURCE_PREFIX.match(text)
            source = source_match.group(1) if source_match else "The source"
            words = _SOURCE_PREFIX.sub("", text).split()
            spoken = " ".join(words[: per_full if full else SUMMARY_WORDS])
            segments.append({"segment": segment, "headline": " ".join(words[:8]),
                             "text": f"{source} reports: {spoken}", "item_ids": ids})
        return {"intro": INTRO, "segments": segments, "outro": OUTRO}
