import os
from pathlib import Path

import numpy as np
import pytest

from morning_brief import speech
from tests.helpers import script_with_words


def is_mp3(data: bytes) -> bool:
    return len(data) > 4 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def test_script_passages_order():
    script = script_with_words(20)
    assert speech.script_passages(script) == ["Good morning.", script["segments"][0]["text"], "See you tomorrow."]


def test_join_with_pauses_inserts_gaps():
    out = speech.join_with_pauses([np.ones(100, np.float32), np.ones(50, np.float32)], 1000)
    assert len(out) == 100 + 600 + 50
    assert out[100:700].max() == 0


def test_encode_mp3_produces_mpeg_frames():
    t = np.arange(24000) / 24000
    mp3 = speech.encode_mp3((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 24000)
    assert is_mp3(mp3) and len(mp3) > 1000


def test_ensure_models_downloads_once(tmp_path):
    fetched = []

    def fake_fetch(url, dest):
        fetched.append(url)
        Path(dest).write_bytes(b"model")

    model, voices = speech.ensure_models(tmp_path / "models", fetch=fake_fetch)
    speech.ensure_models(tmp_path / "models", fetch=fake_fetch)
    assert [u.rsplit("/", 1)[1] for u in fetched] == ["kokoro-v1.0.onnx", "voices-v1.0.bin"]
    assert model.read_bytes() == b"model" and voices.name == "voices-v1.0.bin"
    assert not list((tmp_path / "models").glob("*.part"))


def test_ensure_models_removes_partial_file_on_fetch_failure(tmp_path):
    def failing_fetch(url, dest):
        Path(dest).write_bytes(b"partial")
        raise OSError("connection reset")

    with pytest.raises(OSError):
        speech.ensure_models(tmp_path / "models", fetch=failing_fetch)
    assert not list((tmp_path / "models").glob("*.part"))


def test_fake_synthesize_sizes_audio_like_speech(tmp_path):
    mp3, duration = speech.fake_synthesize([" ".join(["word"] * 250)], "af_heart", tmp_path)
    assert duration == pytest.approx(250 / 2.4) and is_mp3(mp3)


def test_speed_is_user_choice():
    assert speech.SPEED == 1.1


@pytest.mark.container
def test_real_kokoro_speaks():
    models = Path(os.environ.get("MODELS_DIR", "/srv/data/models"))
    mp3, duration = speech.synthesize(["Good morning. This is a test of the morning brief."], "af_heart", models)
    assert 1.5 < duration < 10 and is_mp3(mp3)


@pytest.mark.container
def test_container_runs_as_non_root():
    assert os.getuid() != 0
