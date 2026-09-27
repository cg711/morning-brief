import os
from pathlib import Path

import numpy as np
import pytest

from morning_brief import speech
from morning_brief.music import Music
from tests.helpers import script_with_words


def is_mp3(data: bytes) -> bool:
    return len(data) > 4 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def test_script_passages_order():
    script = script_with_words(20)
    assert speech.script_passages(script) == ["Good morning.", script["segments"][0]["text"], "See you tomorrow."]


def test_assemble_without_music_matches_old_layout():
    out, starts = speech.assemble([np.ones(100, np.float32), np.ones(50, np.float32)], 1000)
    assert len(out) == 100 + 600 + 50
    assert out[100:700].max() == 0
    assert starts == [0.0, 0.7]


def test_assemble_with_music_offsets_passages():
    m = Music(intro=np.full(2000, 0.5, np.float32), outro=np.full(1000, 0.5, np.float32))
    out, starts = speech.assemble([np.ones(100, np.float32), np.ones(50, np.float32)], 1000, m)
    # intro 2000 + gap 300, passage 100, pause 600, passage 50, gap 600, outro 1000
    assert len(out) == 2000 + 300 + 100 + 600 + 50 + 600 + 1000
    assert starts == [2.3, 3.0]
    assert out[:2000].max() == 0.5 and out[-1000:].max() == 0.5


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
    audio = speech.fake_synthesize([" ".join(["word"] * 250)], "af_heart", tmp_path)
    assert audio.duration == pytest.approx(250 / 2.4, abs=1e-3) and is_mp3(audio.mp3)
    assert audio.starts == [0.0]


def test_fake_synthesize_with_music_reports_starts(tmp_path):
    m = Music(intro=np.zeros(24000, np.float32), outro=np.zeros(12000, np.float32))
    audio = speech.fake_synthesize(["word " * 24, "word " * 12], "af_heart", tmp_path, m)
    assert audio.starts == pytest.approx([1.3, 1.3 + 10.0 + 0.6])
    assert audio.duration == pytest.approx(1.3 + 10.0 + 0.6 + 5.0 + 0.6 + 0.5)


def test_speed_is_user_choice():
    assert speech.SPEED == 1.1


def test_fake_synthesize_dialogue_passage(tmp_path):
    passages = ["word " * 12, [("am_michael", "word " * 24), ("af_heart", "word " * 12)]]
    audio = speech.fake_synthesize(passages, "am_michael", tmp_path)
    # 5 s + 0.6 pause, then 10 s + 0.25 line pause + 5 s
    assert audio.starts == pytest.approx([0.0, 5.6])
    assert audio.duration == pytest.approx(5.6 + 10.0 + 0.25 + 5.0)


@pytest.mark.container
def test_real_kokoro_two_voices():
    models = Path(os.environ.get("MODELS_DIR", "/srv/data/models"))
    one = speech.synthesize(["Hello there, and welcome."], "am_michael", models)
    both = speech.synthesize([[("am_michael", "Hello there, and welcome."), ("af_heart", "Thanks, glad to be here.")]],
                             "am_michael", models)
    assert is_mp3(both.mp3) and both.duration > one.duration + 1.0 and both.starts == [0.0]


@pytest.mark.container
def test_real_kokoro_speaks():
    models = Path(os.environ.get("MODELS_DIR", "/srv/data/models"))
    audio = speech.synthesize(["Good morning. This is a test of the morning brief."], "af_heart", models)
    assert 1.5 < audio.duration < 10 and is_mp3(audio.mp3) and audio.starts == [0.0]


@pytest.mark.container
def test_container_runs_as_non_root():
    assert os.getuid() != 0


@pytest.mark.container
def test_real_kokoro_with_music_and_chapters(tmp_path):
    from mutagen.id3 import ID3
    from morning_brief import id3, music
    models = Path(os.environ.get("MODELS_DIR", "/srv/data/models"))
    m = music.Music(music.sting("intro", speech.SAMPLE_RATE), music.sting("outro", speech.SAMPLE_RATE))
    audio = speech.synthesize(["Welcome.", "Part one is here.", "Goodbye."], "am_michael", models, m)
    data = id3.tag(audio.mp3, title="Test", chapters=[("Introduction", 0.0), ("Part one", audio.starts[1]),
                                                      ("Wrap-up", audio.starts[2])], duration=audio.duration)
    path = tmp_path / "t.mp3"
    path.write_bytes(data)
    assert len(ID3(str(path)).getall("CHAP")) == 3 and audio.starts[0] > music.STING_S


def test_script_chapters_with_a_personal_segment():
    from morning_brief import daily_worker

    script = {"intro": "Hi.", "outro": "Bye.", "segments": [
        {"segment": "personal", "headline": "Morning", "text": "well", "item_ids": None},
        {"segment": "headlines", "headline": "Storm hits coast", "text": "t", "item_ids": ["a"]},
        {"segment": "local", "headline": "Council passes budget", "text": "t", "item_ids": ["b"]}]}
    marks = speech.script_chapters(daily_worker._clean_script(script), [0.0, 5.0, 20.0, 40.0, 60.0])
    assert [title for title, _ in marks] == ["Introduction", "Your morning", "Storm hits coast",
                                             "Council passes budget", "Wrap-up"]
    assert [start for _, start in marks] == [0.0, 5.0, 20.0, 40.0, 60.0]
