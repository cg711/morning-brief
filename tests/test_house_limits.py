from morning_brief.house_limits import HouseLimits, RateLimiter


def test_a_limiter_allows_up_to_the_limit_then_refuses():
    r = RateLimiter(3, 60)
    assert [r.allow("a", t) for t in (0, 1, 2, 3)] == [True, True, True, False]


def test_the_window_slides():
    r = RateLimiter(2, 60)
    assert r.allow("a", 0) and r.allow("a", 10) and not r.allow("a", 59)
    assert r.allow("a", 61)            # the event at t=0 has aged out
    assert not r.allow("a", 62)        # t=10 and t=61 are still inside


def test_keys_are_independent():
    r = RateLimiter(1, 60)
    assert r.allow("a", 0) and r.allow("b", 0) and not r.allow("a", 1)


def test_refused_attempts_do_not_extend_the_block():
    r = RateLimiter(1, 60)
    assert r.allow("a", 0)
    assert not r.allow("a", 30) and not r.allow("a", 59)
    assert r.allow("a", 61)


def test_old_keys_are_forgotten():
    r = RateLimiter(1, 10)
    for n in range(2000):
        r.allow(f"k{n}", 0)
    r.allow("late", 100)
    assert len(r._events) < 2000


def test_writes_are_limited_per_link_and_overall():
    limits = HouseLimits(write_link=2, write_all=3)
    assert limits.allow_write("a", 0) and limits.allow_write("a", 1) and not limits.allow_write("a", 2)
    assert limits.allow_write("b", 3)            # overall: third write
    assert not limits.allow_write("b", 4)        # overall cap reached


def test_memos_are_limited_separately_from_writes():
    limits = HouseLimits(write_link=1, memo_link=2, memo_all=3)
    assert limits.allow_write("a", 0) and not limits.allow_write("a", 1)
    assert limits.allow_memo("a", 2) and limits.allow_memo("a", 3) and not limits.allow_memo("a", 4)
    assert limits.allow_memo("b", 5) and not limits.allow_memo("b", 6)


def test_defaults_match_the_spec():
    limits = HouseLimits()
    assert [limits.allow_write("a", float(n)) for n in range(31)].count(True) == 30
    assert [limits.allow_memo("b", float(n)) for n in range(7)].count(True) == 6
