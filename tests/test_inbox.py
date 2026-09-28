import pytest

from morning_brief import inbox


@pytest.mark.parametrize("text,expected", [
    ("https://www.nytimes.com/2026/09/28/story.html", ("", "https://www.nytimes.com/2026/09/28/story.html")),
    ("  HTTP://example.com/a  \n", ("", "HTTP://example.com/a")),
    ("How the Fed began", ("How the Fed began", None)),
    ("  spaced   out \t topic ", ("spaced out topic", None)),
    ("\n\nFirst line here\nsecond line", ("First line here", None)),
    ("read https://example.com/a later", ("read https://example.com/a later", None)),
    ("https://a.example https://b.example", ("https://a.example https://b.example", None)),
    ("https://", ("https://", None)),
    ("ftp://example.com/file", ("ftp://example.com/file", None)),
    ("", ("", None)),
    ("   \n  ", ("", None)),
    ("Headline here\nhttps://example.com/a", ("Headline here", "https://example.com/a")),
    ("https://example.com/a\n\n  Headline  here ", ("Headline here", "https://example.com/a")),
    ("one\nhttps://a.example/x\nhttps://b.example/y", ("one", None)),
    ("see https://example.com/a\nmore", ("see https://example.com/a", None)),
])
def test_parse_input(text, expected):
    assert inbox.parse_input(text) == expected


def test_long_line_is_cut_at_a_word_with_ellipsis():
    topic, url = inbox.parse_input("word " * 100)
    assert url is None and topic.endswith("…") and len(topic) <= 200
    assert not topic[:-1].endswith(" ") and topic[:-1].split(" ")[-1] == "word"


def test_long_line_without_spaces_is_hard_cut():
    topic, _ = inbox.parse_input("x" * 300)
    assert topic == "x" * 199 + "…"


def test_exactly_200_is_kept():
    assert inbox.parse_input("y" * 200) == ("y" * 200, None)


@pytest.mark.parametrize("n,word", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
                                    (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (101, "101st"),
                                    (111, "111th")])
def test_ordinal(n, word):
    assert inbox.ordinal(n) == word
