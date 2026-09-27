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
