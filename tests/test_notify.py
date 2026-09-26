import json
import threading
from dataclasses import replace

import httpx

from morning_brief import notify

TOPIC = "topic_" + "a" * 20


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_post_sends_ntfy_json(settings):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    s = replace(settings, ntfy_topic=TOPIC)
    assert notify.post(s, "Deep dive ready", "Café history (17 min)", ["headphones"], client=client_for(handler))
    (req,) = seen
    assert req.method == "POST" and str(req.url) == "https://ntfy.sh"
    assert json.loads(req.content) == {
        "topic": TOPIC, "title": "Deep dive ready", "message": "Café history (17 min)",
        "tags": ["headphones"], "click": "https://brief.test/",
    }


def test_post_is_noop_without_topic(settings):
    def handler(request):
        raise AssertionError("must not be called")

    assert notify.post(settings, "t", "m", [], client=client_for(handler)) is False


def test_post_swallows_errors(settings, monkeypatch):
    # the morning_brief logger doesn't propagate once create_app has run, so capture the call directly
    warnings = []
    monkeypatch.setattr(notify.log, "warning", lambda *args: warnings.append(args))
    s = replace(settings, ntfy_topic=TOPIC)

    def server_error(request):
        return httpx.Response(503)

    def timeout(request):
        raise httpx.ConnectTimeout("slow")

    assert notify.post(s, "t", "m", [], client=client_for(server_error)) is False
    assert notify.post(s, "t", "m", [], client=client_for(timeout)) is False
    assert len(warnings) == 2 and all("ntfy push failed" in w[0] for w in warnings)


def test_post_survives_client_construction_error(settings, monkeypatch):
    # the client is only built inside post() when no client= is supplied, so patch httpx.Client itself
    def exploding(*args, **kwargs):
        raise RuntimeError("no sockets")

    monkeypatch.setattr(notify.httpx, "Client", exploding)
    s = replace(settings, ntfy_topic=TOPIC)
    assert notify.post(s, "t", "m", []) is False


def test_send_posts_on_a_thread(settings, monkeypatch):
    calls, done = [], threading.Event()

    def fake_post(s, title, message, tags):
        calls.append((title, message, tags, threading.current_thread().name))
        done.set()
        return True

    monkeypatch.setattr(notify, "post", fake_post)
    notify.send(replace(settings, ntfy_topic=TOPIC), "t", "m", ["x"])
    assert done.wait(2)
    assert calls == [("t", "m", ["x"], "ntfy")]


def test_send_without_topic_starts_nothing(settings, monkeypatch):
    monkeypatch.setattr(notify, "post", lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    notify.send(settings, "t", "m", [])
