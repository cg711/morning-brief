from __future__ import annotations

import gc
import urllib.request
from pathlib import Path
from typing import NamedTuple

import lameenc
import numpy as np

from .music import Music

MODEL_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
# fp32 on purpose: on an older CPU without AVX2/VNNI (tested: i5-3570) int8 ran at 2.1x real time; fp32 at 0.62x.
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
SAMPLE_RATE = 24000
SEGMENT_PAUSE_S = 0.6
INTRO_GAP_S = 0.3
OUTRO_GAP_S = 0.6
LINE_PAUSE_S = 0.25
BITRATE_KBPS = 64
SPEED = 1.1  # user-chosen pace (~146 wpm); 1.0 ran 666 words to 5:01
FAKE_WORDS_PER_SECOND = 2.4
DOWNLOAD_TIMEOUT_S = 60
DOWNLOAD_CHUNK_BYTES = 1 << 16


def script_passages(script: dict) -> list[str]:
    return [script["intro"], *(seg["text"] for seg in script["segments"]), script["outro"]]


def script_chapters(script: dict, starts: list[float]) -> list[tuple[str, float]]:
    """Chapter marks for script_passages() output (daily brief): intro, one per segment, outro."""
    marks = [("Introduction", 0.0)]
    marks += [(seg["headline"], starts[1 + n]) for n, seg in enumerate(script["segments"])]
    marks.append(("Wrap-up", starts[-1]))
    return marks


class Audio(NamedTuple):
    mp3: bytes
    duration: float        # seconds
    starts: list[float]    # start second of each passage in the final audio


Line = tuple[str, str]  # (voice, text): one turn of a two-host dialogue


def _join_lines(parts: list[np.ndarray], sample_rate: int) -> np.ndarray:
    out = []
    for n, part in enumerate(parts):
        if n:
            out.append(_silence(LINE_PAUSE_S, sample_rate))
        out.append(part.astype(np.float32, copy=False))
    return np.concatenate(out)


def _silence(seconds: float, sample_rate: int) -> np.ndarray:
    return np.zeros(int(sample_rate * seconds), dtype=np.float32)


def assemble(chunks: list[np.ndarray], sample_rate: int, music: Music | None = None) -> tuple[np.ndarray, list[float]]:
    """[intro sting, gap] + passages with pauses between + [gap, outro sting]. Returns samples and passage starts."""
    parts: list[np.ndarray] = []
    starts: list[float] = []
    position = 0

    def add(samples: np.ndarray) -> None:
        nonlocal position
        parts.append(samples.astype(np.float32, copy=False))
        position += len(samples)

    if music is not None:
        add(music.intro)
        add(_silence(INTRO_GAP_S, sample_rate))
    for n, chunk in enumerate(chunks):
        if n:
            add(_silence(SEGMENT_PAUSE_S, sample_rate))
        starts.append(position / sample_rate)
        add(chunk)
    if music is not None:
        add(_silence(OUTRO_GAP_S, sample_rate))
        add(music.outro)
    return np.concatenate(parts), starts


def encode_mp3(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(BITRATE_KBPS)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return bytes(encoder.encode(pcm) + encoder.flush())


def _fetch(url: str, dest: Path) -> None:
    """Default fetcher: streams to dest with a connect/read timeout so a stalled download
    can't hold the run lock indefinitely."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response, open(dest, "wb") as out:
        while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
            out.write(chunk)


def ensure_models(models_dir: Path, fetch=_fetch) -> tuple[Path, Path]:
    models_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in (MODEL_FILE, VOICES_FILE):
        path = models_dir / name
        if not path.exists():
            partial = path.with_name(path.name + ".part")
            try:
                fetch(MODEL_BASE + name, partial)
            except Exception:
                partial.unlink(missing_ok=True)
                raise
            partial.rename(path)
        paths.append(path)
    return paths[0], paths[1]


def synthesize(passages: list[str | list[Line]], voice: str, models_dir: Path,
               music: Music | None = None) -> Audio:
    """Speak each passage with Kokoro, lay it out with pauses (and music), encode MP3. The model is loaded
    only for this call."""
    from kokoro_onnx import Kokoro  # heavy import; espeak fails on macOS, so only the container gets here

    model, voices = ensure_models(models_dir)
    kokoro = Kokoro(str(model), str(voices))
    try:
        chunks, sample_rate = [], SAMPLE_RATE
        for passage in passages:
            if isinstance(passage, str):
                audio, sample_rate = kokoro.create(passage, voice=voice, speed=SPEED, lang="en-us")
            else:
                parts = []
                for line_voice, text in passage:
                    line_audio, sample_rate = kokoro.create(text, voice=line_voice, speed=SPEED, lang="en-us")
                    parts.append(line_audio)
                audio = _join_lines(parts, sample_rate)
            chunks.append(audio)
    finally:
        del kokoro
        gc.collect()
    if music is not None and sample_rate != SAMPLE_RATE:
        raise RuntimeError(f"Kokoro returned {sample_rate} Hz; music is {SAMPLE_RATE} Hz")
    samples, starts = assemble(chunks, sample_rate, music)
    return Audio(encode_mp3(samples, sample_rate), len(samples) / sample_rate, starts)


def _fake_speech(text: str) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * max(0.5, len(text.split()) / FAKE_WORDS_PER_SECOND)), dtype=np.float32)


def fake_synthesize(passages: list[str | list[Line]], voice: str, models_dir: Path,
                    music: Music | None = None) -> Audio:
    """Silent speech sized like the real thing, laid out like the real thing. For development on macOS."""
    chunks = [_fake_speech(p) if isinstance(p, str) else _join_lines([_fake_speech(t) for _, t in p], SAMPLE_RATE)
              for p in passages]
    samples, starts = assemble(chunks, SAMPLE_RATE, music)
    return Audio(encode_mp3(samples, SAMPLE_RATE), len(samples) / SAMPLE_RATE, starts)
