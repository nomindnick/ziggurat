"""Store bootstrap and migration tests."""

import pytest

from ziggurat.data.store import apply_schema, connect
from ziggurat.paths import SCHEMA_PATH


def test_schema_bootstraps_migrates_and_is_idempotent(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = connect(db_path)
    apply_schema(conn)
    apply_schema(conn)

    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == "2"
    indexes = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "idx_weekly_stats_lookup" in indexes
    assert "idx_injuries_lookup" in indexes
    conn.close()
    assert db_path.exists()


def test_existing_v1_database_is_upgraded():
    conn = connect()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"] == "1"

    apply_schema(conn)

    assert conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"] == "2"
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_schedules_lookup'"
    ).fetchone()


def test_newer_database_version_is_rejected():
    conn = connect()
    apply_schema(conn)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        apply_schema(conn)


def test_connect_defaults_to_memory():
    conn = connect()
    apply_schema(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM meta").fetchone()["n"] == 1
