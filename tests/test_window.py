from datetime import timedelta

from morning_brief.window import select_window, window_start
from tests.helpers import NOW, make_item


def test_window_start_defaults_to_24h():
    assert window_start(None, NOW) == NOW - timedelta(hours=24)


def test_window_start_uses_previous_cutoff():
    prev = NOW - timedelta(hours=20)
    assert window_start(prev, NOW) == prev - timedelta(hours=2)


def test_window_start_capped_at_72h():
    assert window_start(NOW - timedelta(days=5), NOW) == NOW - timedelta(hours=72)


def test_select_window_filters_and_sorts_newest_first():
    start = NOW - timedelta(hours=10)
    old = make_item("old", title="Old", published_at=NOW - timedelta(hours=11))
    a = make_item("a", title="A story", published_at=NOW - timedelta(hours=5))
    b = make_item("b", title="B story", published_at=NOW - timedelta(hours=1))
    assert [i.id for i in select_window([old, a, b], start)] == ["b", "a"]


def test_select_window_dedupes_by_title_and_id():
    start = NOW - timedelta(hours=10)
    first = make_item("x1", title="Storm hits coast", published_at=NOW - timedelta(hours=2))
    same_title = make_item("x2", title="Storm hits coast!", source="BBC", published_at=NOW - timedelta(hours=3))
    same_id = make_item("x1", title="Different headline", published_at=NOW - timedelta(hours=4))
    assert [i.id for i in select_window([same_title, same_id, first], start)] == ["x1"]


def test_window_start_overlaps_previous_cutoff_by_two_hours():
    prev = NOW - timedelta(hours=20)
    assert window_start(prev, NOW) == prev - timedelta(hours=2)
    assert window_start(NOW - timedelta(hours=71), NOW) == NOW - timedelta(hours=72)  # still capped
