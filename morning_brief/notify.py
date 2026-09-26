"""Best-effort push notifications through ntfy (https://ntfy.sh). A push never raises into the caller."""
from __future__ import annotations

import logging
import threading

import httpx

from .config import Settings

log = logging.getLogger(__name__)
TIMEOUT_S = 10


def post(settings: Settings, title: str, message: str, tags: list[str], *,
         client: httpx.Client | None = None) -> bool:
    """Publish one push synchronously. Returns whether ntfy accepted it."""
    if not settings.ntfy_topic:
        return False
    body = {"topic": settings.ntfy_topic, "title": title, "message": message, "tags": tags,
            "click": f"{settings.public_base_url}/"}
    own = client is None
    try:
        client = client or httpx.Client(timeout=TIMEOUT_S)
        response = client.post(settings.ntfy_server, json=body)
        response.raise_for_status()
        return True
    except Exception as exc:
        log.warning("ntfy push failed: %s: %s", type(exc).__name__, exc)
        return False
    finally:
        if own and client is not None:
            client.close()


def send(settings: Settings, title: str, message: str, tags: list[str]) -> None:
    """Publish in the background so a slow or unreachable ntfy never delays the caller."""
    if not settings.ntfy_topic:
        return
    threading.Thread(target=post, args=(settings, title, message, tags), name="ntfy", daemon=True).start()
