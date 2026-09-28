"""Public share links: /s/<token> (a standalone page) and /s/<token>/audio.mp3.

These are the only paths Tailscale Funnel may reach. The page shows the episode, its chapters and its
transcript, and nothing else from the app: no feed token, no htmx, no links back in.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from markupsafe import Markup

from . import deepdives, shares
from .config import TZ

SEEK_SCRIPT = """
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-seek]");
  if (!button) return;
  var audio = document.getElementById("player");
  var seconds = Number(button.dataset.seek);
  audio.play().catch(function () {});
  if (audio.readyState >= 1) {
    audio.currentTime = seconds;
  } else {
    audio.addEventListener("loadedmetadata", function () { audio.currentTime = seconds; }, { once: true });
  }
});
"""
SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(SEEK_SCRIPT.encode()).digest()).decode()
SHARE_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        f"default-src 'self'; script-src '{SCRIPT_HASH}'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
        "media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}
AUDIO_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "X-Content-Type-Options": "nosniff"}
NOT_FOUND_HTML = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                  '<meta name="viewport" content="width=device-width, initial-scale=1">'
                  "<title>Not available</title></head><body><p>This link isn't available.</p></body></html>")
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


def first_sentence(text: str, limit: int = 200) -> str:
    """The text's first sentence, whitespace-collapsed and cut to about `limit` characters at a word."""
    text = " ".join(text.split())
    match = _SENTENCE_END_RE.search(text)
    sentence = text[: match.end()] if match else text
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return sentence


def make_router(*, settings, conn, templates, episode_view) -> APIRouter:
    router = APIRouter()

    def not_found() -> HTMLResponse:
        return HTMLResponse(NOT_FOUND_HTML, status_code=404, headers=SHARE_HEADERS)

    def shared(token: str):
        """The shared topic row if the link is live and its audio exists, else None."""
        row = shares.resolve(conn, token)
        if row is None or not deepdives.audio_path(settings.deep_dives_dir, row["id"]).is_file():
            return None
        return row

    @router.get("/s/{token}", response_class=HTMLResponse)
    def share_page(token: str, request: Request):
        row = shared(token)
        if row is None:
            return not_found()
        ep = episode_view(row)
        published = datetime.fromisoformat(ep["published_at"]).astimezone(TZ).strftime("%B %-d, %Y")
        return templates.TemplateResponse(request, "share.html", {
            "ep": ep, "token": token, "published": published,
            "description": first_sentence(ep["intro"]),
            "chapters": [s for s in ep["sections"] if s["start"] is not None],
            "seek_script": Markup(SEEK_SCRIPT),
        }, headers=SHARE_HEADERS)

    @router.get("/s/{token}/audio.mp3")
    def share_audio(token: str):
        row = shared(token)
        if row is None:
            return not_found()
        return FileResponse(deepdives.audio_path(settings.deep_dives_dir, row["id"]), media_type="audio/mpeg",
                            headers=AUDIO_HEADERS)

    @router.get("/s/{rest:path}")
    def share_catch_all(rest: str):
        return not_found()

    return router
