"""Prepend an ID3v2.3 tag with a title and chapter markers (CTOC + CHAP) to an MP3. No dependencies."""
from __future__ import annotations

import logging
import struct
from typing import Callable

log = logging.getLogger(__name__)
NO_OFFSET = 0xFFFFFFFF
TOC_FLAGS = 0x03  # top-level | ordered


def _frame(frame_id: bytes, body: bytes) -> bytes:
    return frame_id + struct.pack(">I", len(body)) + b"\x00\x00" + body  # v2.3 sizes are plain 32-bit


def _text(frame_id: bytes, text: str) -> bytes:
    return _frame(frame_id, b"\x01" + text.encode("utf-16") + b"\x00\x00")  # encoding 1: UTF-16 with BOM


def _synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _check(chapters: list[tuple[str, float]], duration: float) -> None:
    if not chapters or len(chapters) > 255:
        raise ValueError("need 1 to 255 chapters")
    starts = [start for _, start in chapters]
    if any(b <= a for a, b in zip(starts, starts[1:])):
        raise ValueError("chapter starts must be strictly ascending")
    if starts[0] < 0 or starts[-1] >= duration:
        raise ValueError("chapter starts must lie within [0, duration)")


def tag(mp3: bytes, *, title: str, chapters: list[tuple[str, float]], duration: float) -> bytes:
    _check(chapters, duration)
    ids = [f"ch{n}".encode() for n in range(len(chapters))]
    ends = [start for _, start in chapters[1:]] + [duration]
    frames = [_text(b"TIT2", title)]
    frames.append(_frame(b"CTOC", b"toc\x00" + bytes([TOC_FLAGS, len(ids)]) + b"".join(i + b"\x00" for i in ids)))
    for element_id, (name, start), end in zip(ids, chapters, ends):
        times = struct.pack(">IIII", round(start * 1000), round(end * 1000), NO_OFFSET, NO_OFFSET)
        frames.append(_frame(b"CHAP", element_id + b"\x00" + times + _text(b"TIT2", name)))
    body = b"".join(frames)
    return b"ID3\x03\x00\x00" + _synchsafe(len(body)) + body + mp3


def try_tag(mp3: bytes, *, title: str, chapters: list[tuple[str, float]] | Callable[[], list[tuple[str, float]]],
            duration: float) -> tuple[bytes, list[tuple[str, float]] | None]:
    """Chapters are a nice-to-have: on any failure, log and return the plain MP3 without chapters.
    `chapters` may be the list itself, or a zero-argument callable that builds it (so building the list
    can fail too without ever failing the episode)."""
    try:
        resolved = chapters() if callable(chapters) else chapters
        return tag(mp3, title=title, chapters=resolved, duration=duration), resolved
    except Exception as exc:
        log.warning("chapters skipped for %r: %s: %s", title, type(exc).__name__, exc)
        return mp3, None
