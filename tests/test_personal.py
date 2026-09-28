import ssl
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx

from morning_brief import personal
from tests.helpers import NOW

TODAY = date(2026, 9, 25)  # NOW is 08:00 Central on Sep 25


def state(day="2026-09-25", score=67.0, insights=True):
    return {"today": {"day": day, "sleep_score": score, "readiness_score": 85.0, "total_sleep_h": 6.06,
                      "readiness_contributors": {"hrv_balance": 82}},
            "insights": ([{"title": "Bedtime tonight: 22:30",
                           "detail": "A steady bedtime helps your sleep score. " + "More words here. " * 40}]
                         if insights else [])}


def http_for(payload=None, status=200):
    def handler(request):
        assert str(request.url) == "http://oura.test/api/state"
        return httpx.Response(status, json=payload) if payload is not None else httpx.Response(status)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_oura_facts_fresh_day():
    facts = personal.oura_facts(http_for(state()), "http://oura.test/", TODAY)
    assert facts["sleep"] == {"sleep_score": 67, "hours": 6.1, "readiness": 85, "hrv_balance": 82}
    assert facts["tip"]["title"] == "Bedtime tonight: 22:30"
    assert len(facts["tip"]["detail"]) <= 300 and facts["tip"]["detail"].endswith("…")


def test_oura_facts_stale_or_missing_parts():
    assert "sleep" not in personal.oura_facts(http_for(state(day="2026-09-24")), "http://oura.test", TODAY)
    assert "sleep" not in personal.oura_facts(http_for(state(score=None)), "http://oura.test", TODAY)
    assert personal.oura_facts(http_for(state(day="2026-09-24", insights=False)), "http://oura.test", TODAY) is None
    assert personal.oura_facts(http_for(status=500), "http://oura.test", TODAY) is None


def test_summarize_outflows_only():
    txns = [personal.Txn(Decimal("-28.40"), "Target"), personal.Txn(Decimal("-9.99"), None),
            personal.Txn(Decimal("-3.50"), "Cafe"), personal.Txn(Decimal("1200.00"), "Employer"),
            personal.Txn(Decimal("-500.00"), "Savings", is_transfer=True),
            personal.Txn(Decimal("-40.00"), "Split parent", is_parent=True)]
    assert personal.summarize(txns) == {"total": 41.89, "count": 3,
                                        "biggest": {"payee": "Target", "amount": 28.4}}
    assert personal.summarize([]) == {"total": 0.0, "count": 0, "biggest": None}
    assert personal.summarize([personal.Txn(Decimal("-5"), None)])["biggest"]["payee"] == "unknown"


def configured(settings):
    return replace(settings, personal_segment=True, oura_dashboard_url="http://oura.test",
                   actual_server_url="https://actual.test", actual_password="pw-secret", actual_sync_id="sync-1")


def test_actual_spending_uses_yesterday_and_hides_secrets(settings, monkeypatch):
    seen, warnings = [], []
    s = configured(settings)
    assert personal.actual_spending(s, TODAY - timedelta(days=1),
                                    fetch=lambda st, d: seen.append(d) or [personal.Txn(Decimal("-2"), "X")])["count"] == 1
    assert seen == [date(2026, 9, 24)]
    monkeypatch.setattr(personal.log, "warning", lambda *a: warnings.append(a))

    def boom(st, d):
        raise RuntimeError("login failed for pw-secret")

    assert personal.actual_spending(s, date(2026, 9, 24), fetch=boom) is None
    assert warnings and "pw-secret" not in repr(warnings)
    assert personal.actual_spending(settings, date(2026, 9, 24), fetch=boom) is None  # not configured


def test_gather_personal(settings):
    s = configured(settings)
    fetch = lambda st, d: [personal.Txn(Decimal("-12.00"), "Cub")]
    facts = personal.gather_personal(s, http_for(state()), NOW, actual_fetch=fetch)
    assert set(facts) == {"sleep", "tip", "spending"} and facts["spending"]["total"] == 12.0
    assert personal.gather_personal(settings, http_for(state()), NOW, actual_fetch=fetch) is None  # off
    only_oura = replace(s, actual_server_url="")
    assert set(personal.gather_personal(only_oura, http_for(state()), NOW)) == {"sleep", "tip"}

    def boom(st, d):
        raise RuntimeError("x")

    assert personal.gather_personal(replace(s, oura_dashboard_url=""), http_for(state()), NOW, actual_fetch=boom) is None


def test_check_prints_no_amounts_or_secrets(settings):
    lines = []
    s = configured(settings)
    personal.check(s, http_for(state()), NOW, actual_fetch=lambda st, d: [personal.Txn(Decimal("-77.77"), "Secret Shop")],
                   out=lines.append)
    text = "\n".join(lines)
    assert "Oura: ok" in text and "Actual: ok (1 purchases yesterday)" in text
    assert "77" not in text and "Secret Shop" not in text and "pw-secret" not in text
    lines.clear()
    personal.check(settings, http_for(state()), NOW, out=lines.append)
    assert lines == ["Oura: not configured", "Actual: not configured"]


def test_oura_facts_ignores_non_finite_numbers():
    body = (b'{"today": {"day": "2026-09-25", "sleep_score": 67.0, "total_sleep_h": NaN, "readiness_score": Infinity,'
            b' "readiness_contributors": {"hrv_balance": 82}}, "insights": []}')
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    assert personal.oura_facts(http, "http://oura.test", TODAY)["sleep"] == \
        {"sleep_score": 67, "hours": None, "readiness": None, "hrv_balance": 82}
    nan_score = body.replace(b'"sleep_score": 67.0', b'"sleep_score": NaN')
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=nan_score)))
    assert personal.oura_facts(http, "http://oura.test", TODAY) is None


def test_oura_facts_skips_demo_data():
    assert personal.oura_facts(http_for({**state(), "status": {"demo": True}}), "http://oura.test", TODAY) is None
    assert personal.oura_facts(http_for({**state(), "status": {"demo": False}}), "http://oura.test", TODAY) is not None


def self_signed_pem(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "actual.test")])
    start = datetime(2026, 1, 1)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(start)
            .not_valid_after(start + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    path = tmp_path / "actual.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def test_tls_cert(tmp_path):
    assert personal._tls_cert("1") is True
    assert personal._tls_cert("0") is False
    ctx = personal._tls_cert(str(self_signed_pem(tmp_path)))
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False and ctx.verify_mode == ssl.CERT_REQUIRED


def test_actual_fetch_reads_on_budget_rows_in_a_temporary_dir(settings, monkeypatch, tmp_path):
    import actual
    import actual.queries

    seen = {}

    def row(amount, payee, starting=0):
        return SimpleNamespace(get_amount=lambda: Decimal(amount), payee=SimpleNamespace(name=payee) if payee else None,
                               starting_balance_flag=starting)

    class FakeActual:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs
            self.session = "session"

        def __enter__(self):
            seen["dir_during"] = Path(seen["kwargs"]["data_dir"]).is_dir()
            return self

        def __exit__(self, *exc):
            return False

    def fake_get_transactions(session, **kwargs):
        seen["query"] = kwargs
        return [row("-12.50", "Cub"), row("-1000.00", None, starting=1), row("-3", None)]

    monkeypatch.setattr(actual, "Actual", FakeActual)
    monkeypatch.setattr(actual.queries, "get_transactions", fake_get_transactions)
    s = replace(configured(settings), actual_tls_verify="0")
    txns = personal._actual_fetch(s, date(2026, 9, 24))
    assert txns == [personal.Txn(Decimal("-12.50"), "Cub"), personal.Txn(Decimal("-3"), None)]
    kwargs = seen["kwargs"]
    assert seen["dir_during"] and not Path(kwargs["data_dir"]).exists()
    assert Path(kwargs["data_dir"]).name.startswith("actual-") and kwargs["cert"] is False
    assert kwargs["timeout"] == httpx.Timeout(10, read=30)
    assert seen["query"] == {"start_date": date(2026, 9, 24), "end_date": date(2026, 9, 25), "transfer": False,
                             "off_budget": False}
