"""THE as-of leakage test exemplar (item 0.2).

Every real accessor built later (projections, usage, injuries, rosters, ...)
copies this pattern: keyword-only `as_of`, knowledge-time column filtered with
`<= as_of`, latest-snapshot-per-key via a correlated subquery, and a test that
inserts facts on both sides of a knowledge boundary and asserts the future
stays invisible. See ziggurat/data/asof.py for the convention.
"""

from datetime import date, datetime

import pytest

from ziggurat.data.asof import normalize_as_of
from ziggurat.data.store import connect

# A toy version of a snapshot table: one row per (player, week) per retrieval.
TOY_SCHEMA = """
CREATE TABLE toy_projections (
    player          TEXT NOT NULL,
    week            INTEGER NOT NULL,
    points          REAL NOT NULL,
    retrieved_as_of TEXT NOT NULL   -- ISO-8601: lexicographic == chronological
);
"""

TOY_ROWS = [
    # (player, week, points, retrieved_as_of)
    ("qb_a", 1, 18.5, "2025-09-01"),  # first snapshot
    ("qb_a", 1, 21.0, "2025-09-05"),  # revised later — invisible before 09-05
    ("rb_b", 1, 15.0, "2025-09-05"),  # first knowable on 09-05
]


def get_toy_projections(conn, *, as_of):
    """The accessor pattern later modules copy.

    Keyword-only `as_of`; returns the latest snapshot per (player, week) that
    was knowable on or before `as_of` (inclusive end-of-day semantics).
    """
    cutoff = normalize_as_of(as_of).isoformat()
    rows = conn.execute(
        """
        SELECT player, week, points
        FROM toy_projections t
        WHERE retrieved_as_of = (
            SELECT MAX(retrieved_as_of) FROM toy_projections t2
            WHERE t2.player = t.player AND t2.week = t.week
              AND t2.retrieved_as_of <= :as_of
        )
        """,
        {"as_of": cutoff},
    ).fetchall()
    return {(r["player"], r["week"]): r["points"] for r in rows}


@pytest.fixture()
def toy_conn():
    conn = connect(":memory:")
    conn.executescript(TOY_SCHEMA)
    conn.executemany("INSERT INTO toy_projections VALUES (?, ?, ?, ?)", TOY_ROWS)
    return conn


def test_no_leakage_across_knowledge_boundary(toy_conn):
    """Querying as-of 09-03 must not surface anything learned on 09-05."""
    seen = get_toy_projections(toy_conn, as_of="2025-09-03")
    assert seen == {("qb_a", 1): 18.5}  # original snapshot, not the revision
    assert ("rb_b", 1) not in seen  # not knowable yet


def test_as_of_is_inclusive_and_picks_latest_snapshot(toy_conn):
    seen = get_toy_projections(toy_conn, as_of="2025-09-05")
    assert seen == {("qb_a", 1): 21.0, ("rb_b", 1): 15.0}


def test_before_any_knowledge_returns_nothing(toy_conn):
    assert get_toy_projections(toy_conn, as_of="2025-08-31") == {}


# ── normalize_as_of: the argument contract every accessor shares ─────────────


def test_normalize_accepts_date_datetime_and_iso_string():
    assert normalize_as_of(date(2025, 9, 3)) == date(2025, 9, 3)
    assert normalize_as_of(datetime(2025, 9, 3, 14, 30)) == date(2025, 9, 3)
    assert normalize_as_of("2025-09-03") == date(2025, 9, 3)


def test_normalize_rejects_none_and_junk():
    with pytest.raises(TypeError):
        normalize_as_of(None)  # no implicit "now" — ever
    with pytest.raises(TypeError):
        normalize_as_of(20250903)
    with pytest.raises(ValueError):
        normalize_as_of("not-a-date")
