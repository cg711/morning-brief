import os
import shutil
import subprocess

import numpy as np
import pytest

from morning_brief import house_audio
from tests.helpers import wav_bytes

pytestmark = [pytest.mark.container, pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")]


def test_a_stereo_wav_at_another_rate_becomes_mono_24k(tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(wav_bytes(1.0, rate=44100, channels=2))
    memo = house_audio.prepare(src, 60)
    assert 0.95 < memo.duration_s < 1.05 and memo.samples.dtype == np.float32


def test_an_m4a_from_a_phone_style_encoder_decodes(tmp_path):
    out = tmp_path / "in.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac",
                    str(out)], check=True)
    memo = house_audio.prepare(out, 60)
    assert 0.9 < memo.duration_s < 1.2


def test_audio_longer_than_the_cap_is_refused(tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(wav_bytes(6.0))
    with pytest.raises(house_audio.AudioError, match="longer than 2 seconds"):
        house_audio.prepare(src, 2)


def test_garbage_behind_a_valid_header_is_a_clean_422(tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + os.urandom(2000))
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.prepare(src, 60)
    assert err.value.status == 422


def test_a_text_file_is_refused_before_ffmpeg_runs(tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("hello")
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.prepare(src, 60)
    assert err.value.status == 415


def test_the_resource_limits_do_not_stop_a_long_clip(tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(wav_bytes(120.0, rate=48000))
    memo = house_audio.prepare(src, 600)
    assert 119 < memo.duration_s < 121


def test_the_resource_limits_are_applied_before_ffmpeg_would_start(tmp_path):
    cmd = house_audio.ffmpeg_command("/usr/bin/ffmpeg", tmp_path / "a", tmp_path / "b", 60, "wav")
    done = subprocess.run(["/bin/sh", "-c", cmd[2], "/bin/sh", "-c", "ulimit -t; ulimit -f; ulimit -v"],
                          capture_output=True, text=True, check=True)
    assert done.stdout.split() == ["30", "262144", "2097152"]


def test_a_full_length_clip_fits_the_output_cap(tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(wav_bytes(599.0, rate=8000))
    memo = house_audio.prepare(src, 600)
    assert 598 < memo.duration_s <= 600


@pytest.mark.parametrize("script", ["#EXTM3U\n#EXTINF:1,\n{path}\n", "ffconcat version 1.0\nfile '{path}'\n"])
def test_a_playlist_disguised_as_mp3_cannot_pull_in_another_local_file(tmp_path, script):
    secret = tmp_path / "other-memo.wav"
    secret.write_bytes(wav_bytes(1.0))
    src = tmp_path / "in.bin"
    src.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + script.format(path=secret).encode())
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.prepare(src, 60)
    assert err.value.status == 422
