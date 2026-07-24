"""SQLite connection, bootstrap, and ordered migration helpers."""

import re
import sqlite3
from pathlib import Path

from ziggurat.paths import MIGRATIONS_DIR, SCHEMA_PATH

_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
_INITIAL_SCHEMA_VERSION = 1


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection with the project's standard settings."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def open_db(path: str | Path) -> sqlite3.Connection:
    """Connect AND bring the schema up to date — the safe default for any command.

    ``connect`` alone creates an empty file if none exists and never migrates, so
    a read against a database written before a newer migration dies with a raw
    ``no such table``. Commands that only read should still migrate: the cost is
    nil on an up-to-date database and the alternative is a traceback in the
    operator's face (item 3.1 audit finding).
    """
    conn = connect(path)
    apply_schema(conn)
    return conn


def _schema_version(conn: sqlite3.Connection) -> int:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if has_meta is None:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row is not None else 0


def _migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    migrations = []
    if not migrations_dir.is_dir():
        return migrations
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"invalid migration filename {path.name!r}")
        migrations.append((int(match.group(1)), path))

    versions = [version for version, _ in migrations]
    expected = list(range(_INITIAL_SCHEMA_VERSION + 1, _INITIAL_SCHEMA_VERSION + 1 + len(versions)))
    if versions != expected:
        raise RuntimeError(f"migration versions must be contiguous; found {versions}, expected {expected}")
    return migrations


def apply_schema(
    conn: sqlite3.Connection,
    schema_path: Path = SCHEMA_PATH,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> None:
    """Bootstrap a new database and apply each pending migration exactly once."""
    if _schema_version(conn) == 0:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.commit()

    migrations = _migrations(migrations_dir)
    latest = migrations[-1][0] if migrations else _INITIAL_SCHEMA_VERSION
    current = _schema_version(conn)
    if current > latest:
        raise RuntimeError(
            f"database schema version {current} is newer than supported version {latest}"
        )

    for version, path in migrations:
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        script = f"""BEGIN;
{sql}
INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '{version}');
COMMIT;
"""
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        current = version
