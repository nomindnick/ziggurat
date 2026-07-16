"""SQLite connection and schema helpers.

Facts live in SQLite (db/ziggurat.sqlite, gitignored); the schema (db/schema.sql)
is public and applied idempotently.
"""

import sqlite3
from pathlib import Path

from ziggurat.paths import SCHEMA_PATH


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection with the project's standard settings."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> None:
    """Apply db/schema.sql. Idempotent (IF NOT EXISTS / OR REPLACE throughout)."""
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()
