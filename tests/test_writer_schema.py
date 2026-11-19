def test_migration_is_recorded(conn):
    assert "012_writer.sql" in [r[0] for r in conn.execute("SELECT * FROM schema_migrations")]


def test_attempt_counters_default_to_zero(conn):
    columns = {t: {r["name"]: r for r in conn.execute(f"PRAGMA table_info({t})")} for t in ("daily_jobs", "topics")}
    for table, column in (("daily_jobs", "attempts"), ("topics", "claim_attempts")):
        info = columns[table][column]
        assert (info["type"], info["notnull"], info["dflt_value"]) == ("INTEGER", 1, "0")
