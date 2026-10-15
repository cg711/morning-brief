"""Voice-memo pipeline: receive an upload under a byte cap, check its type, decode it with a locked-down ffmpeg,
level it, and keep only the WAV this module produced.

Everything a housemate sends is hostile input. The app never decodes an upload itself: ffmpeg does, in a separate
resource-limited process, into a mono 24 kHz 16-bit WAV that the standard library then reads.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import NamedTuple

import numpy as np
from starlette.formparsers import MultiPartException, MultiPartParser

log = logging.getLogger(__name__)
SAMPLE_RATE = 24000
FFMPEG_TIMEOUT_S = 30
CPU_S, FSIZE_BLOCKS, ADDRESS_KB = 30, 262144, 2 * 1024 * 1024  # ulimit -t, -f, -v for the ffmpeg process
TARGET_RMS, PEAK, MAX_GAIN, FADE_S = 0.1, 0.9, 8.0, 0.03
MIN_S = 0.3
TOLERANCE_S = 0.75  # a browser recording can overshoot the cap by a few hundred ms
SLOTS = threading.BoundedSemaphore(2)  # at most two decodes at once
OVERHEAD = 64 * 1024  # multipart framing allowed on top of the file cap
CHUNK = 64 * 1024
# The ffmpeg input demuxer to force for each sniffed type, so ffmpeg never probes its way into a playlist or concat
# script that could read other local files.
DEMUXERS = {"wav": "wav", "mp3": "mp3", "aac": "aac", "mp4": "mov,mp4,m4a,3gp,3g2,mj2", "ogg": "ogg",
            "webm": "matroska,webm", "caf": "caf"}


class AudioError(Exception):
    """A problem with an upload, with a fixed message that is safe to show and the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message, self.status = message, status


class TooLarge(Exception):
    pass


class Memo(NamedTuple):
    samples: np.ndarray
    duration_s: float


def sniff(head: bytes) -> str | None:
    """The container named by an upload's first bytes, if it is on the allow-list."""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 4 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "aac" if head[1] & 0x06 == 0 else "mp3"  # ADTS has layer bits 00; MP3 frames never do
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if head[:4] == b"caff":
        return "caf"
    return None


async def capped(stream, cap: int):
    """Pass a byte stream through, raising TooLarge as soon as more than `cap` bytes have gone by."""
    total = 0
    try:
        async for chunk in stream:
            total += len(chunk)
            if total > cap:
                raise TooLarge()
            yield chunk
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


def _too_large(max_bytes: int) -> AudioError:
    return AudioError(f"that file is larger than {max(1, max_bytes // (1024 * 1024))} MB", 413)


async def receive(request, max_bytes: int, incoming_dir: Path) -> Path:
    """Parse the multipart body with a hard cap on the bytes read, then copy its single 'audio' file part to a new
    private file in `incoming_dir`. The caller deletes the file."""
    declared = request.headers.get("content-length")
    if declared is not None and (not (declared.isascii() and declared.isdigit()) or int(declared) > max_bytes + OVERHEAD):
        raise _too_large(max_bytes)
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise AudioError("send the recording as a file", 415)
    parser = MultiPartParser(request.headers, capped(request.stream(), max_bytes + OVERHEAD),
                             max_files=1, max_fields=4, max_part_size=4096)
    try:
        form = await parser.parse()
    except TooLarge:
        raise _too_large(max_bytes) from None
    except MultiPartException:
        raise AudioError("couldn't read that upload", 400) from None
    try:
        upload = form.get("audio")
        if upload is None or not hasattr(upload, "read"):
            raise AudioError("choose or record some audio first", 400)
        incoming_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = incoming_dir / f"{secrets.token_hex(16)}.upload"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            written = 0
            with os.fdopen(fd, "wb") as out:
                while chunk := await upload.read(CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise _too_large(max_bytes)
                    out.write(chunk)
            if written == 0:
                raise AudioError("that file is empty", 400)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    finally:
        await form.close()


def ffmpeg_command(ffmpeg: str, src: Path, dst: Path, max_s: int, kind: str) -> list[str]:
    """The sh wrapper that applies resource limits, then execs ffmpeg with arguments that read one local file
    and write one mono 24 kHz 16-bit WAV. Paths are made absolute (the process runs in a work directory, and a
    relative name like "word:x" could be parsed as a protocol); the input format is forced from the sniffed `kind`."""
    limits = (f"ulimit -t {CPU_S} && ulimit -f {FSIZE_BLOCKS} && ulimit -v {ADDRESS_KB} && "
              'exec "$0" "$@"')
    out_cap = int((max_s + 1) * SAMPLE_RATE * 2 + 4096)
    return ["/bin/sh", "-c", limits, ffmpeg,
            "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file", "-t", str(max_s + 1), "-threads", "1", "-f", DEMUXERS[kind],
            "-i", str(Path(src).resolve()),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-fs", str(out_cap),
            "-f", "wav", str(Path(dst).resolve())]


def run_ffmpeg(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(cwd), env={},
                          timeout=FFMPEG_TIMEOUT_S, close_fds=True, check=False)


def normalize(src: Path, work: Path, max_s: int, kind: str, *, run=None, which=None) -> Path:
    """Decode `src` to out.wav in `work`, or raise AudioError."""
    exe = (which or shutil.which)("ffmpeg")
    if not exe:
        raise AudioError("audio uploads are unavailable", 503)
    dst = work / "out.wav"
    try:
        done = (run or run_ffmpeg)(ffmpeg_command(exe, src, dst, max_s, kind), work)
    except subprocess.TimeoutExpired:
        raise AudioError("that audio took too long to process", 422) from None
    except OSError:
        log.exception("could not start ffmpeg")
        raise AudioError("audio uploads are unavailable", 503) from None
    if done.returncode != 0 or not dst.is_file():
        log.warning("ffmpeg could not read an upload (exit %s)", done.returncode)
        raise AudioError("couldn't read audio from that file", 422)
    return dst


def read_wav(path: Path) -> np.ndarray:
    """A WAV written by ffmpeg (mono, 24 kHz, 16-bit) as float32 samples."""
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SAMPLE_RATE):
                raise AudioError("couldn't read audio from that file", 422)
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError):
        raise AudioError("couldn't read audio from that file", 422) from None
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def level(samples: np.ndarray) -> np.ndarray:
    """Scale toward a target loudness without clipping, and fade the ends so a memo never clicks."""
    peak = float(np.abs(samples).max()) if len(samples) else 0.0
    if peak < 1e-4:
        raise AudioError("that recording is silent", 422)
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    gain = min(TARGET_RMS / rms, MAX_GAIN, PEAK / peak)
    out = (samples * gain).astype(np.float32)
    n = min(int(FADE_S * SAMPLE_RATE), len(out) // 2)
    if n:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        out[:n] *= ramp
        out[-n:] *= ramp[::-1]
    return out


def save_wav(path: Path, samples: np.ndarray) -> None:
    """Write mono 24 kHz 16-bit PCM, atomically (a temp file then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(partial), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    os.replace(partial, path)


def prepare(incoming: Path, max_s: int, *, run=None, which=None) -> Memo:
    """Check, decode and level an upload. Raises AudioError with a fixed message for anything wrong with it."""
    with incoming.open("rb") as handle:
        head = handle.read(16)
    kind = sniff(head)
    if kind is None:
        raise AudioError("that doesn't look like an audio file", 415)
    if not SLOTS.acquire(blocking=False):
        raise AudioError("busy right now, try again in a minute", 429)
    try:
        with tempfile.TemporaryDirectory(prefix="house-") as work:
            samples = read_wav(normalize(incoming, Path(work), max_s, kind, run=run, which=which))
    finally:
        SLOTS.release()
    duration = len(samples) / SAMPLE_RATE
    if duration > max_s + TOLERANCE_S:
        raise AudioError(f"that recording is longer than {max_s} seconds", 422)
    if duration < MIN_S:
        raise AudioError("that recording is too short", 422)
    return Memo(level(samples), duration)


def sweep(incoming_dir: Path, now: float, max_age_s: float = 3600) -> int:
    """Delete stale files left by an interrupted upload."""
    removed = 0
    if not incoming_dir.is_dir():
        return 0
    for path in incoming_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < now - max_age_s:
                path.unlink()
                removed += 1
        except OSError:
            log.warning("could not remove stale upload %s", path.name)
    return removed
