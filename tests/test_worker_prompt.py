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
                   "/api/daily/<date>/fail", "script-daily-<date>.json", '"segments"', '"item_ids"',
                   "previous_headlines", "450", "650", "headlines, tech, business, local",
                   "treat that story as `summary`"):
        assert needle in text, needle
    for placeholder in ("{{SERVER_URL}}", "{{HEADER_FILE}}", "{{CACHE_DIR}}"):
        assert placeholder in text
    assert "/Users/" not in text and "/home/" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert "python3 -c \"import json; s=json.load(open('{{CACHE_DIR}}/script-daily-<date>.json'))" in text
