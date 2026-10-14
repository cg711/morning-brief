def test_house_tables_exist(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"house_members", "house_chores", "house_chore_done", "house_todos", "house_events", "house_prep",
            "house_upkeep", "house_notes", "house_memos", "house_episodes"} <= names


def test_migration_is_recorded(conn):
    assert "010_house.sql" in [r[0] for r in conn.execute("SELECT * FROM schema_migrations")]


def test_deleting_a_chore_deletes_its_done_marks(conn):
    chore = conn.execute("INSERT INTO house_chores (name, weekday, start_date, rotation_json, created_at) "
                         "VALUES ('Trash', 1, '2026-09-01', '[]', 'x')").lastrowid
    conn.execute("INSERT INTO house_chore_done (chore_id, day, done_at) VALUES (?, '2026-09-01', 'x')", (chore,))
    conn.execute("DELETE FROM house_chores WHERE id = ?", (chore,))
    assert conn.execute("SELECT COUNT(*) FROM house_chore_done").fetchone()[0] == 0


def test_deleting_an_event_deletes_its_prep(conn):
    event = conn.execute("INSERT INTO house_events (title, day, created_at) VALUES ('Party', '2026-10-01', 'x')").lastrowid
    conn.execute("INSERT INTO house_prep (event_id, text, position) VALUES (?, 'ice', 0)", (event,))
    conn.execute("DELETE FROM house_events WHERE id = ?", (event,))
    assert conn.execute("SELECT COUNT(*) FROM house_prep").fetchone()[0] == 0
