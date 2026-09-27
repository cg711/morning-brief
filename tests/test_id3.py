import pytest
from mutagen.id3 import ID3

from morning_brief import id3

MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 400
CHAPTERS = [("Introduction", 0.0), ("Café origins", 12.5), ("Wrap-up", 300.25)]


def read(tmp_path, data):
    path = tmp_path / "ep.mp3"
    path.write_bytes(data)
    return ID3(str(path))


def test_tag_writes_title_toc_and_chapters(tmp_path):
    data = id3.tag(MP3, title="How the Fed Began — Part 1", chapters=CHAPTERS, duration=320.0)
    tags = read(tmp_path, data)
    assert tags["TIT2"].text == ["How the Fed Began — Part 1"]
    (toc,) = tags.getall("CTOC")
    assert toc.element_id == "toc" and int(toc.flags) == 3
    assert toc.child_element_ids == ["ch0", "ch1", "ch2"]
    chaps = sorted(tags.getall("CHAP"), key=lambda c: c.start_time)
    assert [(c.element_id, c.start_time, c.end_time) for c in chaps] == [
        ("ch0", 0, 12500), ("ch1", 12500, 300250), ("ch2", 300250, 320000)]
    assert [c.sub_frames["TIT2"].text[0] for c in chaps] == ["Introduction", "Café origins", "Wrap-up"]


def test_audio_follows_tag_unchanged():
    data = id3.tag(MP3, title="T", chapters=CHAPTERS, duration=320.0)
    assert data[:3] == b"ID3" and data[3:5] == b"\x03\x00"
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    assert all(b < 0x80 for b in data[6:10])
    assert data[10 + size:] == MP3


@pytest.mark.parametrize("chapters, duration", [
    ([], 10.0),
    ([("A", 0.0), ("B", 0.0)], 10.0),
    ([("A", 5.0), ("B", 2.0)], 10.0),
    ([("A", 0.0), ("B", 10.0)], 10.0),
    ([("A", -1.0)], 10.0),
])
def test_bad_chapters_raise(chapters, duration):
    with pytest.raises(ValueError):
        id3.tag(MP3, title="T", chapters=chapters, duration=duration)


def test_try_tag_falls_back_to_plain_audio(monkeypatch):
    warnings = []
    monkeypatch.setattr(id3.log, "warning", lambda *args: warnings.append(args))
    data, chapters = id3.try_tag(MP3, title="T", chapters=[], duration=10.0)
    assert data == MP3 and chapters is None and len(warnings) == 1
    data, chapters = id3.try_tag(MP3, title="T", chapters=CHAPTERS, duration=320.0)
    assert data.startswith(b"ID3") and chapters == CHAPTERS


def test_try_tag_accepts_a_callable_that_builds_chapters(monkeypatch):
    warnings = []
    monkeypatch.setattr(id3.log, "warning", lambda *args: warnings.append(args))

    def raises():
        raise IndexError("starts too short")

    data, chapters = id3.try_tag(MP3, title="T", chapters=raises, duration=10.0)
    assert data == MP3 and chapters is None and len(warnings) == 1

    data, chapters = id3.try_tag(MP3, title="T", chapters=lambda: CHAPTERS, duration=320.0)
    assert data.startswith(b"ID3") and chapters == CHAPTERS
