"""SQLite connection, bootstrap, and ordered migration helpers."""

import re
import sqlite3
from pathlib import Path

from ziggurat.paths import MIGRATIONS_DIR, SCHEMA_PATH

_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
_INITIAL_SCHEMA_VERSION = 1


#: How long a writer waits for another process's lock before giving up.
#:
#: WHY IT IS SET AT ALL (item 3.2c): the stdlib default is 5 s, which is shorter
#: than a single bulk write in the historical backfill (~19k weekly_stats rows in
#: one transaction). The process that loses that race is not necessarily the
#: backfill — it can be the 4x/day league sync, and per CLAUDE.md a league-sync
#: day that is not captured is UNRECOVERABLE (ESPN serves league state as a
#: current snapshot only, with no historical backfill). Raising only the
#: backfill's own timeout, as the first design proposed, makes the patient
#: process the one that loses nothing; setting it in `connect` protects every
#: caller, which is where the perishable one lives.
#:
#: WHY 30 s: measured on this box, the largest single write the backfill
#: performs — a 19,421-row `weekly_stats` upsert, one `executemany` in one
#: transaction — commits in **0.051 s** on disk. 30 s is ~600x that, so it
#: absorbs the whole backfill's worst blocking write with room for a slow disk,
#: while still surrendering FAR inside the units' own bounds
#: (`TimeoutStartSec=600` on the league sync, `1800` on the NFL ingest) rather
#: than hanging a timer. It is a WAITING bound, not a correctness knob: SQLite
#: simply retries until the lock clears or the bound expires.
#:
#: NOT WAL. Journal mode stays as-is until after the draft — WAL changes backup
#: semantics (a .sqlite file copy is no longer self-contained) and this is not
#: the three weeks to discover that.
BUSY_TIMEOUT_MS = 30_000


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection with the project's standard settings."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
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


#: `meta` keys a migration writes when it LOST something it could not refuse to
#: lose: '<migration number>_<table>_collapsed'. See `migration_alerts`.
_MIGRATION_ALERT_LIKE = "00%collapsed"


def migration_alerts(conn: sqlite3.Connection) -> dict[str, str]:
    """Rows a migration left behind to say it silently dropped data.

    WHY THIS EXISTS. A migration runs inside ``open_db`` on every command, so it
    may not raise — a traceback three weeks before the draft is worse than the
    damage most migrations could do. That forces the table rebuilds in 007 to use
    ``INSERT OR REPLACE``, which means a key collision COLLAPSES instead of
    failing. Items 3.1 and 3.1b each shipped a defect of exactly that shape (a
    degraded write that destroyed data and still reported ``ok``), so the rule
    carried forward is: a loss that cannot be refused must at least be a POSITIVE
    FACT somewhere. 007 writes ``meta`` rows named ``<nnn>_<table>_collapsed``
    holding the number of rows lost, and only when a row WAS lost — so an empty
    dict here is the honest "nothing was dropped", not "nobody checked".

    Returns ``{meta_key: count_as_text}``. Empty on every healthy database,
    including every fresh one.
    """
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if has_meta is None:
        return {}
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
        (_MIGRATION_ALERT_LIKE,),
    ).fetchall()
    # Index access, not r["key"]: a caller may have set a different row_factory.
    return {row[0]: row[1] for row in rows}
