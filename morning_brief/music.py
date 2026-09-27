"""Intro/outro music: a generated sting (no licensing), or the user's own WAV from data/music/."""
from __future__ import annotations

import logging
import wave
from pathlib import Path
from typing import NamedTuple

import numpy as np

log = logging.getLogger(__name__)
STING_S = 4.0
PEAK = 0.5                 # sits under Kokoro's voice level
OVERRIDE_MAX_S = 30
FADE_S = 0.05
ATTACK_S = 0.6
DECAY_S = 1.4
TAIL_S = 0.5               # final linear taper so the last sample is exactly 0
# Hz. Intro: a Cmaj9 voicing (C4 E4 G4 B4 D5). Outro: a plain C major that resolves (G3 C4 E4 G4 C5).
VOICINGS = {
    "intro": (261.63, 329.63, 392.00, 493.88, 587.33),
    "outro": (196.00, 261.63, 329.63, 392.00, 523.25),
}


class Music(NamedTuple):
    intro: np.ndarray
    outro: np.ndarray


def sting(kind: str, sample_rate: int) -> np.ndarray:
    """A soft synth-pad swell that fades to silence. Deterministic: same input, same samples."""
    n = int(STING_S * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    tone = np.zeros(n)
    for f in VOICINGS[kind]:
        tone += np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t)
    envelope = np.clip(t / ATTACK_S, 0.0, 1.0) * np.exp(-np.maximum(t - ATTACK_S, 0.0) / DECAY_S)
    tail = int(TAIL_S * sample_rate)
    envelope[-tail:] *= np.linspace(1.0, 0.0, tail)
    out = tone * envelope
    return (out / np.abs(out).max() * PEAK).astype(np.float32)


def _pcm_to_float(raw: bytes, width: int) -> np.ndarray:
    if width == 1:
        return (np.frombuffer(raw, np.uint8).astype(np.float64) - 128) / 128
    if width == 2:
        return np.frombuffer(raw, "<i2").astype(np.float64) / 32768
    if width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        return np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float64) / (1 << 23)
    if width == 4:
        return np.frombuffer(raw, "<i4").astype(np.float64) / (1 << 31)
    raise ValueError(f"unsupported sample width {width}")


def load_override(path: Path, sample_rate: int) -> np.ndarray | None:
    """The user's WAV as float32 mono at sample_rate, peak-normalized with short fades. None if unusable."""
    if not path.exists():
        return None
    try:
        with wave.open(str(path), "rb") as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if rate <= 0:
                raise ValueError("invalid sample rate")
            if frames / rate > OVERRIDE_MAX_S:
                raise ValueError(f"longer than {OVERRIDE_MAX_S} s")
            samples = _pcm_to_float(w.readframes(frames), width)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if rate != sample_rate:
            count = round(len(samples) * sample_rate / rate)
            samples = np.interp(np.arange(count) * rate / sample_rate, np.arange(len(samples)), samples)
        peak = np.abs(samples).max() if len(samples) else 0.0
        if peak == 0:
            raise ValueError("the file is silent or empty")
        samples = samples / peak * PEAK
        fade = min(int(FADE_S * sample_rate), len(samples) // 2)
        ramp = np.linspace(0.0, 1.0, fade)
        samples[:fade] *= ramp
        samples[-fade:] *= ramp[::-1]
        return samples.astype(np.float32)
    except Exception as exc:
        log.warning("music override %s ignored: %s", path, exc)
        return None


def stings(settings, sample_rate: int) -> Music | None:
    if not settings.music:
        return None
    pick = {}
    for kind in ("intro", "outro"):
        override = load_override(settings.music_dir / f"{kind}.wav", sample_rate)
        pick[kind] = override if override is not None else sting(kind, sample_rate)
    return Music(**pick)
