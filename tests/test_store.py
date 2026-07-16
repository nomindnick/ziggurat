"""Store/schema smoke tests."""

from ziggurat.data.store import apply_schema, connect


def test_schema_applies_and_is_idempotent(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = connect(db_path)
    apply_schema(conn)
    apply_schema(conn)  # second run must be a no-op, not an error
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == "0"
    conn.close()
    assert db_path.exists()


def test_connect_defaults_to_memory():
    conn = connect()
    apply_schema(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM meta").fetchone()["n"] == 1
