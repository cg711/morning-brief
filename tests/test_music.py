import struct
import wave
from dataclasses import replace

import numpy as np
import pytest

from morning_brief import music

SR = 24000


def write_wav(path, samples, rate=SR, channels=1, width=2):
    data = np.asarray(samples, dtype=np.float64)
    if channels == 2:
        data = np.column_stack([data, data]).ravel()
    ints = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(ints.tobytes())


def test_sting_shape_and_level():
    for kind in ("intro", "outro"):
        s = music.sting(kind, SR)
        assert s.dtype == np.float32 and s.ndim == 1
        assert len(s) == int(music.STING_S * SR)
        assert np.abs(s).max() == pytest.approx(music.PEAK, rel=1e-3)
        assert s[0] == 0 and s[-1] == 0


def test_sting_is_deterministic_and_intro_differs_from_outro():
    assert np.array_equal(music.sting("intro", SR), music.sting("intro", SR))
    assert not np.array_equal(music.sting("intro", SR), music.sting("outro", SR))


def test_override_mono_same_rate(tmp_path):
    path = tmp_path / "intro.wav"
    write_wav(path, 0.9 * np.sin(np.linspace(0, 200, SR * 2)))
    s = music.load_override(path, SR)
    assert s.dtype == np.float32 and len(s) == SR * 2
    assert np.abs(s).max() == pytest.approx(music.PEAK, rel=1e-3)
    assert s[0] == 0 and s[-1] == 0


def test_override_stereo_48k_is_resampled_to_mono(tmp_path):
    path = tmp_path / "intro.wav"
    write_wav(path, 0.5 * np.sin(np.linspace(0, 300, 48000 * 3)), rate=48000, channels=2)
    s = music.load_override(path, SR)
    assert len(s) == SR * 3


def test_override_too_long_or_garbage_is_ignored_with_warning(tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(music.log, "warning", lambda *args: warnings.append(args))
    long = tmp_path / "long.wav"
    write_wav(long, np.zeros(SR * 31) + 0.1)
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav at all")
    assert music.load_override(long, SR) is None
    assert music.load_override(junk, SR) is None
    assert len(warnings) == 2


def test_override_zero_framerate_is_ignored_with_warning(tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(music.log, "warning", lambda *args: warnings.append(args))
    path = tmp_path / "silentrate.wav"
    fmt = struct.pack("<HHIIHH", 1, 1, 0, 0, 2, 16)
    data = b"\x00\x00" * 4
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    assert music.load_override(path, SR) is None
    assert len(warnings) == 1


def test_missing_override_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(music.log, "warning", lambda *args: (_ for _ in ()).throw(AssertionError("logged")))
    assert music.load_override(tmp_path / "nope.wav", SR) is None


def test_stings_respects_setting_and_prefers_override(settings):
    assert music.stings(replace(settings, music=False), SR) is None
    default = music.stings(settings, SR)
    assert np.array_equal(default.intro, music.sting("intro", SR))
    settings.music_dir.mkdir(parents=True)
    write_wav(settings.music_dir / "outro.wav", 0.3 * np.ones(SR))
    custom = music.stings(settings, SR)
    assert len(custom.outro) == SR and np.array_equal(custom.intro, music.sting("intro", SR))
