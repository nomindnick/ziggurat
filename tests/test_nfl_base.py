"""The as-of read (base.select_as_of) — leakage exemplar for every nflverse
accessor.

`knowable_as_of` is the SOLE leakage gate (SPEC: "returns only what was knowable
at that moment"). `retrieved_as_of` does NOT gate visibility — history is
bulk-pulled now, yet a backtest as-of a past date must still see facts knowable
then; retrieved_as_of only selects the latest VERSION of a key (corrections).
This is the two-column extension of tests/test_asof_pattern.py.
"""

from ziggurat.data.nfl import base
from ziggurat.data.store import connect

SYN_SCHEMA = """
CREATE TABLE syn (
    k               TEXT NOT NULL,
    v               REAL NOT NULL,
    knowable_as_of  TEXT NOT NULL,
    retrieved_as_of TEXT NOT NULL,
    PRIMARY KEY (k, retrieved_as_of)
);
"""

SYN_ROWS = [
    # (k, v, knowable_as_of, retrieved_as_of)
    # 'a': a value that became knowable in two steps (e.g. a projection updated).
    ("a", 10.0, "2025-09-01", "2025-09-01"),
    ("a", 12.0, "2025-09-05", "2025-09-05"),
    # 'b': an immutable fact knowable 09-10, CORRECTED by a later re-pull (09-20).
    ("b", 20.0, "2025-09-10", "2025-09-10"),
    ("b", 22.0, "2025-09-10", "2025-09-20"),
    # 'c': a future fact — knowable 09-25, but pulled early (09-01).
    ("c", 30.0, "2025-09-25", "2025-09-01"),
]


def _syn():
    conn = connect(":memory:")
    conn.executescript(SYN_SCHEMA)
    conn.executemany("INSERT INTO syn VALUES (?, ?, ?, ?)", SYN_ROWS)
    return conn


def _read(conn, as_of):
    rows = base.select_as_of(conn, "syn", as_of=as_of, key_cols=["k"])
    return {r["k"]: r["v"] for r in rows}


def test_before_anything_knowable_is_empty():
    assert _read(_syn(), "2025-08-31") == {}


def test_knowable_gate_reveals_facts_as_they_become_knowable():
    assert _read(_syn(), "2025-09-01") == {"a": 10.0}          # only a's first step
    assert _read(_syn(), "2025-09-04") == {"a": 10.0}          # b/c not knowable yet
    assert _read(_syn(), "2025-09-05")["a"] == 12.0            # a's update now knowable


def test_correction_uses_latest_version_even_if_retrieved_after_as_of():
    # b is knowable 09-10; the 09-20 correction is the best version. A backtest
    # as-of 09-15 should use the corrected 22.0 (retrieved_as_of does not gate).
    seen = _read(_syn(), "2025-09-15")
    assert seen["b"] == 22.0
    # ...but before b is knowable at all, it is absent regardless of the pull.
    assert "b" not in _read(_syn(), "2025-09-09")


def test_knowable_gate_hides_future_fact_even_though_already_retrieved():
    # c was pulled 09-01 but is not knowable until 09-25 — must stay hidden.
    assert "c" not in _read(_syn(), "2025-09-24")
    assert _read(_syn(), "2025-09-25")["c"] == 30.0


def test_extra_where_is_applied():
    rows = base.select_as_of(
        _syn(), "syn", as_of="2025-09-30", key_cols=["k"],
        extra_where="t.k = :want", params={"want": "b"},
    )
    assert {r["k"] for r in rows} == {"b"}
