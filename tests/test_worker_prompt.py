import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "worker" / "deep-dive-task.template.md"


def test_template_documents_phase3_contract():
    text = TEMPLATE.read_text()
    for needle in ('"url"', '"fact_check"', '"two_hosts"', '"lines"', '"speaker": "host"', '"cohost"',
                   '"suggestions"', "claims_checked", "could not read the link"):
        assert needle in text, needle
    for placeholder in ("{{SERVER_URL}}", "{{HEADER_FILE}}", "{{CACHE_DIR}}"):
        assert placeholder in text
    # the template is published: no machine-specific paths or addresses, only placeholders
    assert "/Users/" not in text and "/home/" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)


DAILY = Path(__file__).resolve().parent.parent / "worker" / "daily-brief-task.template.md"


def test_daily_template_documents_contract():
    text = DAILY.read_text()
    for needle in ("/api/daily/claim", "/api/daily/<date>/items/<item_id>", "/api/daily/<date>/script",
                   "/api/daily/<date>/fail", "script-daily.json", '"segments"', '"item_ids"',
                   "previous_headlines", "450", "650", "headlines, tech, business, local",
                   "treat that story as `summary`", '"personal"', "Your morning", "First, you.",
                   '"weather"', '"calendar"', "clear calendar", "20 to 160 words", "60 to 130 words",
                   '"notes"', '"countdowns"', "Your notes", "is today", "20 to 400 words"):
        assert needle in text, needle
    for placeholder in ("{{SERVER_URL}}", "{{HEADER_FILE}}", "{{CACHE_DIR}}"):
        assert placeholder in text
    assert "/Users/" not in text and "/home/" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert "python3 -c \"import json; s=json.load(open('{{CACHE_DIR}}/script-daily.json'))" in text
    assert "script-daily-<date>" not in text
    assert "20 to 100" not in text


def test_daily_template_documents_events():
    text = DAILY.read_text()
    for needle in ('"events"', "Around town", "at most six", "YYYY-MM-DD", "HH:MM", '"source_id"', '"mentioned"',
                   "not write any prose for events", "only what the page states", "top-level `events`",
                   "about events"):
        assert needle in text, needle
    assert "/Users/" not in text and "/home/" not in text


DEEP_DIVE = Path(__file__).resolve().parent.parent / "worker" / "deep-dive-task.template.md"


def test_templates_use_fixed_file_names_and_no_star_in_the_word_count():
    """The writer's permission rules are globs, so no file name in a command may carry a date or id, and the
    word-count command must not contain a literal `*` (it would be a wildcard in a rule)."""
    daily, deep = DAILY.read_text(), DEEP_DIVE.read_text()
    for text, names in ((daily, ("daily-claim.json", "daily-item.txt", "script-daily.json", "daily-submit.json",
                                 "daily-fail.json")),
                        (deep, ("claim.json", "script-dd.json", "fail-dd.json", "submit.json"))):
        for name in names:
            assert f"{{{{CACHE_DIR}}}}/{name}" in text, name
        for line in text.splitlines():
            if "{{CACHE_DIR}}" in line and ("curl" in line or "--data-binary" in line or "-o " in line or "json.load" in line):
                assert not re.search(r"\{\{CACHE_DIR\}\}/[^ ']*<", line), line
            if "python3 -c" in line:
                assert "*" not in line, line
    assert "script-<id>" not in deep and "fail-<id>" not in deep
