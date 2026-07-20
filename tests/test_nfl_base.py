"""The as-of read (base.select_as_of) — leakage exemplar for every nflverse
accessor.

Two explicit views prevent outcome corrections from silently entering decision
features. The default `historical` view gates both fact and retrieval time;
`latest_truth` gates fact time only and intentionally selects later corrections
for outcome grading or explicitly accepted immutable bulk history.
"""

import pandas as pd
import pytest

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


def test_historical_view_excludes_corrections_retrieved_after_as_of():
    # b was known on 09-10, but its correction arrived 09-20. Historical replay
    # as-of 09-15 must preserve the original value that was available then.
    assert _read(_syn(), "2025-09-15")["b"] == 20.0


def test_latest_truth_view_intentionally_uses_later_correction():
    rows = base.select_as_of(
        _syn(), "syn", as_of="2025-09-15", key_cols=["k"], view="latest_truth"
    )
    seen = {row["k"]: row["v"] for row in rows}
    assert seen["b"] == 22.0
    # Fact time still gates latest-truth reads.
    assert "b" not in {
        row["k"]
        for row in base.select_as_of(
            _syn(), "syn", as_of="2025-09-09", key_cols=["k"], view="latest_truth"
        )
    }


def test_latest_truth_helper_binds_the_view():
    # The helper injects view="latest_truth" so bulk/backtest callers cannot
    # forget it (and cannot silently get an empty historical read).
    seen = {}

    def fake_accessor(conn, *, as_of, view="historical"):
        seen["view"] = view
        return []

    base.latest_truth(fake_accessor)(_syn(), as_of="2025-09-15")
    assert seen["view"] == "latest_truth"


def test_latest_truth_helper_matches_explicit_view():
    # Wrapping select_as_of reproduces the explicit latest_truth semantics: the
    # 09-20 correction to b is applied for an as-of-09-15 read.
    reader = base.latest_truth(base.select_as_of)
    seen = {r["k"]: r["v"] for r in reader(_syn(), "syn", as_of="2025-09-15", key_cols=["k"])}
    assert seen["b"] == 22.0


def test_latest_truth_helper_rejects_conflicting_view():
    reader = base.latest_truth(base.select_as_of)
    with pytest.raises(ValueError, match="conflicting view"):
        reader(_syn(), "syn", as_of="2025-09-15", key_cols=["k"], view="historical")


def test_knowable_gate_hides_future_fact_even_though_already_retrieved():
    # c was pulled 09-01 but is not knowable until 09-25 — must stay hidden.
    assert "c" not in _read(_syn(), "2025-09-24")
    assert _read(_syn(), "2025-09-25")["c"] == 30.0


def test_unknown_view_is_rejected():
    with pytest.raises(ValueError, match="unknown as-of view"):
        base.select_as_of(_syn(), "syn", as_of="2025-09-30", key_cols=["k"], view="future")


def test_source_schema_drift_is_loud():
    with pytest.raises(ValueError, match="missing required columns.*week"):
        base.require_columns(pd.DataFrame({"season": [2023]}), ["season", "week"], source="fixture")


def test_extra_where_is_applied():
    rows = base.select_as_of(
        _syn(), "syn", as_of="2025-09-30", key_cols=["k"],
        extra_where="t.k = :want", params={"want": "b"},
    )
    assert {r["k"] for r in rows} == {"b"}
