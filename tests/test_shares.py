from datetime import timedelta

from morning_brief import deepdives, shares
from tests.helpers import NOW, deep_dive_script


def ready_topic(conn, name="Alpha", at=NOW):
    topic_id = deepdives.add_topic(conn, name, "", at)
    deepdives.claim(conn, at)
    deepdives.accept_script(conn, topic_id, deep_dive_script(), at)
    deepdives.publish(conn, topic_id, title="How the Fed Began", word_count=2400, duration_s=1200.0,
                      audio_bytes=4, now=at)
    return topic_id


def test_create_or_get_is_idempotent(conn):
    t = ready_topic(conn)
    token = shares.create_or_get(conn, t, NOW)
    assert token and len(token) >= 20
    assert shares.create_or_get(conn, t, NOW) == token
    assert shares.live_token(conn, t) == token
    assert shares.live_tokens(conn) == {t: token}


def test_only_ready_or_heard_topics_can_be_shared(conn):
    t = ready_topic(conn, "Beta")  # before queuing another: claim() takes the first queued topic
    queued = deepdives.add_topic(conn, "Queued", "", NOW)
    assert shares.create_or_get(conn, queued, NOW) is None
    assert shares.create_or_get(conn, 999, NOW) is None
    assert shares.live_tokens(conn) == {}
    deepdives.mark_heard(conn, t, NOW)
    assert shares.create_or_get(conn, t, NOW) is not None


def test_revoke_then_reshare_gives_a_new_token(conn):
    t = ready_topic(conn)
    old = shares.create_or_get(conn, t, NOW)
    assert shares.revoke(conn, t, NOW) is True
    assert shares.revoke(conn, t, NOW) is False
    assert shares.live_token(conn, t) is None and shares.resolve(conn, old) is None
    new = shares.create_or_get(conn, t, NOW)
    assert new != old
    assert shares.resolve(conn, new)["id"] == t


def test_resolve(conn):
    t = ready_topic(conn)
    token = shares.create_or_get(conn, t, NOW)
    row = shares.resolve(conn, token)
    assert row["id"] == t and row["episode_title"] == "How the Fed Began" and row["script_json"]
    deepdives.mark_heard(conn, t, NOW)
    assert shares.resolve(conn, token)["id"] == t
    assert shares.resolve(conn, "bogus") is None
    assert shares.resolve(conn, "") is None
    assert shares.resolve(conn, "x" * 500) is None


def test_resolve_fails_when_topic_is_no_longer_ready(conn):
    t = ready_topic(conn)
    token = shares.create_or_get(conn, t, NOW)
    conn.execute("UPDATE topics SET status = 'failed' WHERE id = ?", (t,))
    assert shares.resolve(conn, token) is None


def test_delete_topic_removes_its_shares(conn, settings):
    t = ready_topic(conn)
    token = shares.create_or_get(conn, t, NOW)
    deepdives.delete_topic(conn, settings.deep_dives_dir, t)
    assert conn.execute("SELECT COUNT(*) AS n FROM shares WHERE topic_id = ?", (t,)).fetchone()["n"] == 0
    assert shares.resolve(conn, token) is None


def test_housekeeping_keeps_shared_heard_episodes_until_revoked(conn, settings):
    t = ready_topic(conn, at=NOW - timedelta(days=40))
    deepdives.mark_heard(conn, t, NOW - timedelta(days=31))
    shares.create_or_get(conn, t, NOW - timedelta(days=31))
    assert deepdives.housekeeping(conn, settings.deep_dives_dir, NOW)["deleted"] == 0
    assert deepdives.get_topic(conn, t) is not None
    shares.revoke(conn, t, NOW)
    assert deepdives.housekeeping(conn, settings.deep_dives_dir, NOW)["deleted"] == 1
    assert deepdives.get_topic(conn, t) is None
