import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from morning_brief import house_audio
from tests.helpers import wav_bytes

M4A_HEAD = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64


def tone(seconds=1.0, amp=0.5):
    return (np.sin(2 * np.pi * 440 * np.arange(int(24000 * seconds)) / 24000) * amp).astype(np.float32)


def fake_ffmpeg(seconds=1.0, amp=0.5, returncode=0, write=True, raises=None):
    """Stands in for run_ffmpeg: writes a WAV of the given length to the output path (the command's last item)."""
    calls = []

    def run(cmd, cwd):
        calls.append((cmd, cwd))
        if raises:
            raise raises
        if write:
            Path(cmd[-1]).write_bytes(wav_bytes(seconds, amp=amp))
        return subprocess.CompletedProcess(cmd, returncode, b"", b"boom")

    run.calls = calls
    return run


def incoming_file(tmp_path, head=M4A_HEAD):
    path = tmp_path / "in.bin"
    path.write_bytes(head)
    return path


@pytest.mark.parametrize("head,kind", [
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"), (b"ID3\x04\x00\x00", "mp3"), (b"\xff\xfb\x90\x00", "mp3"),
    (b"\xff\xf1\x50\x80", "aac"), (b"\xff\xf9\x50\x80", "aac"), (b"\x00\x00\x00\x20ftypM4A ", "mp4"), (b"OggS\x00\x02", "ogg"),
    (b"\x1a\x45\xdf\xa3\x01", "webm"), (b"caff\x00\x01", "caf")])
def test_sniff_accepts_the_allow_list(head, kind):
    assert house_audio.sniff(head) == kind


@pytest.mark.parametrize("head", [b"", b"\xff", b"MZ\x90\x00", b"<html>", b"%PDF-1.4", b"GIF89a",
                                  b"RIFF\x00\x00\x00\x00AVI LIST", b"\xff\x00\x00\x00", b"PK\x03\x04", b"\x00" * 16,
                                  b"\xff\xe0", b"\x00\x00\x00\x20ft"])
def test_sniff_rejects_everything_else(head):
    assert house_audio.sniff(head) is None


async def chunks(*parts):
    for part in parts:
        yield part


def collect(agen):
    async def go():
        return [c async for c in agen]
    return asyncio.run(go())


def test_capped_passes_a_stream_under_the_cap():
    assert collect(house_audio.capped(chunks(b"ab", b"cd"), 4)) == [b"ab", b"cd"]


def test_capped_raises_once_past_the_cap():
    with pytest.raises(house_audio.TooLarge):
        collect(house_audio.capped(chunks(b"ab", b"cde"), 4))


@pytest.mark.parametrize("kind,demuxer", sorted(house_audio.DEMUXERS.items()))
def test_ffmpeg_command_is_locked_down(tmp_path, kind, demuxer):
    src, dst = tmp_path / "in put.bin", tmp_path / "out.wav"
    cmd = house_audio.ffmpeg_command("/usr/bin/ffmpeg", src, dst, 60, kind)
    assert cmd[:2] == ["/bin/sh", "-c"]
    script = cmd[2]
    assert "ulimit -t 30" in script and "ulimit -f 262144" in script and "ulimit -v 2097152" in script
    assert script.endswith('&& exec "$0" "$@"') and ";" not in script and cmd[3] == "/usr/bin/ffmpeg"
    args = cmd[4:]
    i_input = args.index("-i")

    def before_input(flag, value):
        i = args.index(flag)
        assert args[i + 1] == value and i < i_input

    def anywhere(flag, value):
        assert args[args.index(flag) + 1] == value

    assert "-nostdin" in args
    before_input("-protocol_whitelist", "file")
    before_input("-t", "61")
    before_input("-threads", "1")
    before_input("-f", demuxer)  # the first -f is the forced input demuxer
    assert args[i_input + 1] == str(src.resolve())
    for flag in ("-vn", "-sn", "-dn"):
        assert flag in args
    anywhere("-map", "0:a:0")
    anywhere("-map_metadata", "-1")
    anywhere("-ac", "1")
    anywhere("-ar", "24000")
    anywhere("-c:a", "pcm_s16le")
    assert args[-3:-1] == ["-f", "wav"] and args.index("-f", i_input) == len(args) - 3
    assert args[-1] == str(dst.resolve())


def test_ffmpeg_command_uses_absolute_paths_for_relative_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = house_audio.ffmpeg_command("/usr/bin/ffmpeg", Path("data/word:x"), Path("out.wav"), 60, "wav")
    assert cmd[cmd.index("-i") + 1] == str(tmp_path.resolve() / "data" / "word:x")
    assert cmd[-1] == str(tmp_path.resolve() / "out.wav")


def test_every_sniffed_kind_has_a_forced_demuxer():
    for head in (b"RIFF\0\0\0\0WAVE", b"ID3\4\0\0", b"\xff\xfb\x90\x00", b"\xff\xf1\x50\x80",
                 b"\0\0\0\x20ftypM4A ", b"OggS\0\2", b"\x1a\x45\xdf\xa3", b"caff\0\1"):
        assert house_audio.sniff(head) in house_audio.DEMUXERS


def test_normalize_without_ffmpeg_says_unavailable(tmp_path):
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.normalize(incoming_file(tmp_path), tmp_path, 60, "mp4", run=fake_ffmpeg(), which=lambda name: None)
    assert err.value.status == 503 and "unavailable" in err.value.message


@pytest.mark.parametrize("run", [fake_ffmpeg(returncode=1), fake_ffmpeg(write=False),
                                 fake_ffmpeg(raises=subprocess.TimeoutExpired("ffmpeg", 30))])
def test_normalize_failures_are_clean_422s(tmp_path, run):
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.normalize(incoming_file(tmp_path), tmp_path, 60, "mp4", run=run, which=lambda name: "/usr/bin/ffmpeg")
    assert err.value.status == 422


def test_normalize_runs_ffmpeg_in_the_work_dir(tmp_path):
    run = fake_ffmpeg()
    out = house_audio.normalize(incoming_file(tmp_path), tmp_path, 60, "mp4", run=run,
                                which=lambda name: "/usr/bin/ffmpeg")
    assert out == tmp_path / "out.wav" and run.calls[0][1] == tmp_path
    cmd = run.calls[0][0]
    assert cmd[cmd.index("-f") + 1] == house_audio.DEMUXERS["mp4"]


def test_normalize_says_unavailable_when_ffmpeg_cannot_be_started(tmp_path):
    run = fake_ffmpeg(raises=PermissionError("nope"))
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.normalize(incoming_file(tmp_path), tmp_path, 60, "mp4", run=run,
                              which=lambda name: "/usr/bin/ffmpeg")
    assert err.value.status == 503 and "unavailable" in err.value.message


def test_prepare_forces_the_demuxer_that_matches_the_sniffed_type(tmp_path):
    run = fake_ffmpeg()
    house_audio.prepare(incoming_file(tmp_path, b"ID3\x04\x00\x00#EXTM3U\n" + b"\x00" * 16), 60, run=run,
                        which=lambda name: "/x")
    cmd = run.calls[0][0]
    assert cmd[cmd.index("-f") + 1] == "mp3"


def test_read_wav_rejects_the_wrong_format(tmp_path):
    (tmp_path / "stereo.wav").write_bytes(wav_bytes(channels=2))
    (tmp_path / "slow.wav").write_bytes(wav_bytes(rate=16000))
    (tmp_path / "junk.wav").write_bytes(b"not a wav")
    for name in ("stereo.wav", "slow.wav", "junk.wav", "missing.wav"):
        with pytest.raises(house_audio.AudioError):
            house_audio.read_wav(tmp_path / name)


def rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


def test_level_raises_a_quiet_recording_to_the_target():
    assert 0.09 < rms(house_audio.level(tone(amp=0.02))) < 0.105


def test_level_never_exceeds_the_peak_limit():
    spiky = tone(amp=0.05)
    spiky[12000] = 0.5
    assert np.abs(house_audio.level(spiky)).max() <= 0.9 + 1e-6


def test_level_brings_a_loud_recording_down_and_keeps_its_length():
    out = house_audio.level(tone(amp=0.99))
    assert len(out) == 24000 and rms(out) < 0.2


def test_level_fades_the_ends():
    out = house_audio.level(tone(0.5, 0.5))
    assert abs(out[0]) < 1e-3 and abs(out[-1]) < 1e-3 and np.abs(out[720:-720]).max() > 0.1


def test_level_copes_with_a_clip_shorter_than_the_fades():
    assert len(house_audio.level(tone(0.01, 0.5))) == 240


def test_level_rejects_silence():
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.level(np.zeros(24000, dtype=np.float32))
    assert err.value.status == 422


def test_save_wav_round_trips_atomically(tmp_path):
    path = tmp_path / "memos" / "7.wav"
    samples = tone(0.5, 0.3)
    house_audio.save_wav(path, samples)
    back = house_audio.read_wav(path)
    assert len(back) == len(samples) and np.abs(back - samples).max() < 1e-3
    assert [p.name for p in path.parent.iterdir()] == ["7.wav"]


def test_prepare_returns_levelled_samples_and_the_duration(tmp_path):
    memo = house_audio.prepare(incoming_file(tmp_path), 60, run=fake_ffmpeg(seconds=2.0, amp=0.05),
                               which=lambda name: "/usr/bin/ffmpeg")
    assert memo.duration_s == pytest.approx(2.0) and 0.05 < rms(memo.samples) < 0.11


def test_prepare_refuses_audio_longer_than_the_cap(tmp_path):
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.prepare(incoming_file(tmp_path), 5, run=fake_ffmpeg(seconds=6.0),
                            which=lambda name: "/usr/bin/ffmpeg")
    assert err.value.status == 422 and "longer than 5 seconds" in err.value.message


def test_prepare_refuses_a_recording_that_is_too_short(tmp_path):
    with pytest.raises(house_audio.AudioError, match="too short"):
        house_audio.prepare(incoming_file(tmp_path), 60, run=fake_ffmpeg(seconds=0.1),
                            which=lambda name: "/usr/bin/ffmpeg")


def test_prepare_refuses_the_wrong_kind_of_file_before_running_ffmpeg(tmp_path):
    run = fake_ffmpeg()
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.prepare(incoming_file(tmp_path, b"%PDF-1.4 nope"), 60, run=run, which=lambda name: "/x")
    assert err.value.status == 415 and run.calls == []


def test_prepare_says_busy_when_both_decode_slots_are_taken(tmp_path):
    assert house_audio.SLOTS.acquire(blocking=False) and house_audio.SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(house_audio.AudioError) as err:
            house_audio.prepare(incoming_file(tmp_path), 60, run=fake_ffmpeg(), which=lambda name: "/x")
        assert err.value.status == 429
    finally:
        house_audio.SLOTS.release()
        house_audio.SLOTS.release()


def test_prepare_releases_its_slot_after_a_failure(tmp_path):
    for _ in range(5):
        with pytest.raises(house_audio.AudioError):
            house_audio.prepare(incoming_file(tmp_path), 60, run=fake_ffmpeg(returncode=1), which=lambda name: "/x")
    assert house_audio.SLOTS.acquire(blocking=False) and house_audio.SLOTS.acquire(blocking=False)
    house_audio.SLOTS.release()
    house_audio.SLOTS.release()


def uploader(tmp_path, cap):
    app = FastAPI()
    incoming = tmp_path / "incoming"

    @app.post("/up")
    async def up(request: Request):
        try:
            path = await house_audio.receive(request, cap, incoming)
        except house_audio.AudioError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        return {"size": path.stat().st_size, "mode": oct(path.stat().st_mode & 0o777), "name": path.name}

    return TestClient(app), incoming


def leftovers(incoming):
    return sorted(incoming.glob("*")) if incoming.exists() else []


def test_receive_stores_the_file_privately(tmp_path):
    client, incoming = uploader(tmp_path, 10_000)
    r = client.post("/up", files={"audio": ("memo.m4a", b"x" * 1000, "audio/mp4")})
    assert r.status_code == 200 and r.json()["size"] == 1000 and r.json()["mode"] == "0o600"
    assert [p.name for p in leftovers(incoming)] == [r.json()["name"]]


def test_receive_refuses_a_declared_oversize_body(tmp_path):
    client, incoming = uploader(tmp_path, 1000)
    r = client.post("/up", files={"audio": ("memo.m4a", b"x" * 200_000, "audio/mp4")})
    assert r.status_code == 413 and leftovers(incoming) == []


def test_receive_cuts_off_a_chunked_body_that_never_ends(tmp_path):
    client, incoming = uploader(tmp_path, 1000)
    head = (b'--xx\r\nContent-Disposition: form-data; name="audio"; filename="a.m4a"\r\n'
            b"Content-Type: audio/mp4\r\n\r\n")

    def body():
        yield head
        for _ in range(2000):
            yield b"x" * 1000

    r = client.post("/up", content=body(), headers={"content-type": "multipart/form-data; boundary=xx"})
    assert r.status_code == 413 and leftovers(incoming) == []


def test_receive_refuses_other_content_types(tmp_path):
    client, incoming = uploader(tmp_path, 1000)
    r = client.post("/up", content=b"hello", headers={"content-type": "application/octet-stream"})
    assert r.status_code == 415 and leftovers(incoming) == []


@pytest.mark.parametrize("kwargs", [
    {"files": {"other": ("a.m4a", b"data")}},
    {"files": [("audio", ("a.m4a", b"x" * 10)), ("audio", ("b.m4a", b"y"))]},
    {"files": {"audio": ("a.m4a", b"")}},
    {"data": {"audio": "just text"}, "files": {"zzz": ("a", b"data")}},
])
def test_receive_refuses_bad_shapes_with_a_400(tmp_path, kwargs):
    client, incoming = uploader(tmp_path, 10_000)
    r = client.post("/up", **kwargs)
    assert r.status_code == 400 and leftovers(incoming) == []


def test_receive_cuts_off_a_file_larger_than_the_cap_that_passed_the_other_checks(tmp_path):
    client, incoming = uploader(tmp_path, 1000)
    r = client.post("/up", files={"audio": ("memo.m4a", b"x" * 5000, "audio/mp4")})
    assert r.status_code == 413 and leftovers(incoming) == []


def test_receive_leaves_no_file_when_writing_it_fails(tmp_path, monkeypatch):
    client, incoming = uploader(tmp_path, 1_000_000)
    real_fdopen = os.fdopen

    class Flaky:
        def __init__(self, handle):
            self.handle, self.writes = handle, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, data):
            self.writes += 1
            if self.writes > 1:
                raise OSError("disk full")
            return self.handle.write(data)

    monkeypatch.setattr(house_audio.os, "fdopen", lambda fd, mode: Flaky(real_fdopen(fd, mode)))
    with pytest.raises(OSError):
        client.post("/up", files={"audio": ("memo.m4a", b"x" * 200_000, "audio/mp4")})
    assert leftovers(incoming) == []


def test_receive_treats_a_non_ascii_digit_content_length_as_oversize():
    request = SimpleNamespace(headers={"content-length": "\u00b2", "content-type": "multipart/form-data"})
    with pytest.raises(house_audio.AudioError) as err:
        asyncio.run(house_audio.receive(request, 1000, Path("unused")))
    assert err.value.status == 413


def test_sweep_removes_only_old_files(tmp_path):
    old, fresh = tmp_path / "old.upload", tmp_path / "fresh.upload"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert house_audio.sweep(tmp_path, time.time()) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["fresh.upload"]
    assert house_audio.sweep(tmp_path / "missing", time.time()) == 0


def test_the_image_installs_ffmpeg_lean_and_before_dropping_privileges():
    text = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert "apt-get install -y --no-install-recommends ffmpeg" in text
    assert text.index("ffmpeg") < text.index("USER app")
    assert "rm -rf /var/lib/apt/lists/*" in text


def test_prepare_tolerates_a_recording_a_few_hundred_ms_over_the_cap(tmp_path):
    memo = house_audio.prepare(incoming_file(tmp_path), 5, run=fake_ffmpeg(seconds=5.5), which=lambda name: "/x")
    assert memo.duration_s == pytest.approx(5.5)
    with pytest.raises(house_audio.AudioError) as err:
        house_audio.prepare(incoming_file(tmp_path), 5, run=fake_ffmpeg(seconds=5.8), which=lambda name: "/x")
    assert err.value.status == 422 and "longer than 5 seconds" in err.value.message
