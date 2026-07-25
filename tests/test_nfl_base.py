"""The as-of read (base.select_as_of) — leakage exemplar for every nflverse
accessor.

Two explicit views prevent outcome corrections from silently entering decision
features. The default `historical` view gates both fact and retrieval time;
`latest_truth` gates fact time only and intentionally selects later corrections
for outcome grading or explicitly accepted immutable bulk history.
"""

import logging

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


# ===========================================================================
# select_observed_as_of — the change-log read (item 3.2c)
# ===========================================================================
#
# WHAT THESE TESTS CAN AND CANNOT CATCH (the 3.1b fixture lesson). Every case
# below runs real SQL against real SQLite on a table shaped exactly like the
# shipped `depth_chart_slots`, so they catch resolution-order bugs, tombstone
# bugs, leakage in either time dimension and the view semantics — all of which
# are properties of THIS function. They prove NOTHING about upstream: not that
# nflverse still ships `dt`/`espn_id`, not that the ingester emits a tombstone
# when a slot vacates, and not that `observed_at` is stored verbatim. Those are
# the ingester's tests, and per 3.1b a frozen fixture is weak evidence there too.
#
# The scenario is the measured one, reduced: a KC quarterback room whose third
# slot vacates, and a CIN room whose top two swap. Player names are real NFL
# players (public data — Rule 5 draws the line at league members, not the NFL).

PANEL_SCHEMA = """
CREATE TABLE panel (
    season          INTEGER NOT NULL,
    team            TEXT    NOT NULL,
    slot            TEXT    NOT NULL,   -- stands in for (pos_grp_id, pos_id, pos_rank)
    observed_at     TEXT    NOT NULL,
    occupant        TEXT,               -- NULL == TOMBSTONE (the espn_id column)
    knowable_as_of  TEXT    NOT NULL,
    retrieved_as_of TEXT    NOT NULL,
    PRIMARY KEY (season, team, slot, observed_at, retrieved_as_of)
);
"""

# Three panels. The default retrieval stamp PRECEDES every observation, following
# tests/test_nfl_weekly_stats.py's leakage convention: with retrieved_as_of below
# the as_of under test, only `knowable_as_of` can be doing the hiding. `BULK` is
# the other regime — one backfill stamp for the whole season, which is what makes
# select_as_of tie on every version (and what F8 measured as a silent empty read).
EARLY = "2025-08-01"
BULK = "2026-07-25"
P1, P2 = "2025-09-01T07:10:00Z", "2025-09-08T07:12:00Z"
P3A, P3B = "2025-09-15T07:00:00Z", "2025-09-15T19:01:03Z"   # two panels, one day

PANEL_ROWS = [
    # (season, team, slot, observed_at, occupant)
    # P1 — the baseline: every slot is a change against nothing.
    (2025, "KC", "QB1", P1, "Mahomes"),
    (2025, "KC", "QB2", P1, "Minshew"),
    (2025, "KC", "QB3", P1, "Oladokun"),
    (2025, "CIN", "QB1", P1, "Burrow"),
    (2025, "CIN", "QB2", P1, "Browning"),
    # P2 — KC's third slot VACATES (tombstone); CIN's top two swap. KC QB1/QB2
    # emit no row at all: unchanged, and the read must carry them forward.
    (2025, "KC", "QB3", P2, None),
    (2025, "CIN", "QB1", P2, "Browning"),
    (2025, "CIN", "QB2", P2, "Burrow"),
    # P3 — two panels the same calendar day; only the later one is current.
    (2025, "CIN", "QB2", P3A, "Thorne"),
    (2025, "CIN", "QB2", P3B, "Browning2"),
]


def _panel(rows=PANEL_ROWS, retrieved=EARLY):
    conn = connect(":memory:")
    conn.executescript(PANEL_SCHEMA)
    conn.executemany(
        "INSERT INTO panel VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(s, t, sl, obs, occ, obs[:10], retrieved) for s, t, sl, obs, occ in rows],
    )
    conn.commit()
    return conn


def _chart(conn, as_of, *, team=None, view="historical", tombstones=False):
    """The read a module accessor performs: current occupants, tombstones filtered."""
    clauses = [] if tombstones else ["g.occupant IS NOT NULL"]
    params = {}
    if team is not None:
        clauses.append("g.team = :team")
        params["team"] = team
    return base.select_observed_as_of(
        conn, "panel", as_of=as_of, key_cols=["season", "team", "slot"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


def test_one_observation_per_key_not_the_whole_history():
    # The KC case that inflated the measured board 58%: at as_of=P1's day the
    # room is exactly the three slots P1 published, all from ONE observation.
    rows = _chart(_panel(), "2025-09-01", team="KC")
    assert {r["slot"]: r["occupant"] for r in rows} == {
        "QB1": "Mahomes", "QB2": "Minshew", "QB3": "Oladokun"
    }
    assert len({r["observed_at"] for r in rows}) == 1


def test_select_as_of_would_return_the_whole_history_which_is_why_this_exists():
    # The measured failure the sibling function corrects: under one bulk
    # retrieval stamp every version of a key ties on MAX(retrieved_as_of), so
    # select_as_of returns them all — a QB2 who is two different players at once.
    naive = base.select_as_of(
        _panel(), "panel", as_of="2025-09-30", key_cols=["season", "team", "slot"],
        extra_where="t.team = 'CIN' AND t.slot = 'QB2'",
    )
    assert len(naive) == 4                                   # every CIN QB2 row ever
    correct = _chart(_panel(), "2025-09-30", team="CIN")
    assert len([r for r in correct if r["slot"] == "QB2"]) == 1


def test_an_unchanged_slot_carries_forward():
    # KC QB1/QB2 published no row at P2. A change log means "unchanged", not
    # "gone" — a snapshot-shaped read would lose Mahomes on 09-08.
    rows = _chart(_panel(), "2025-09-08", team="KC")
    assert {r["slot"]: r["occupant"] for r in rows} == {"QB1": "Mahomes", "QB2": "Minshew"}
    assert {r["observed_at"] for r in rows} == {P1}           # carried, not re-observed


def test_tombstone_retires_a_slot_in_one_direction_only():
    # THE PHANTOM-QB4 TEST. KC QB3 exists at P1 and vacates at P2. It must be
    # visible before and invisible after; a read that resolved on retrieval time
    # alone (or filtered tombstones before resolving) carries Oladokun forward
    # for the rest of the season.
    conn = _panel()
    assert "QB3" in {r["slot"] for r in _chart(conn, "2025-09-01", team="KC")}
    assert "QB3" not in {r["slot"] for r in _chart(conn, "2025-09-08", team="KC")}
    assert "QB3" not in {r["slot"] for r in _chart(conn, "2025-11-30", team="KC")}
    # The tombstone is a POSITIVE FACT and is retrievable as one (Rule 6: the
    # reader can say "this slot was vacated on 09-08", not merely stay silent).
    with_stones = _chart(conn, "2025-09-08", team="KC", tombstones=True)
    stone = [r for r in with_stones if r["slot"] == "QB3"]
    assert len(stone) == 1 and stone[0]["occupant"] is None and stone[0]["observed_at"] == P2


def test_without_the_tombstone_the_ghost_survives_which_pins_why_it_is_emitted():
    # Same data, tombstone row deleted — the measured "phantom rank-4 carried
    # forward seven weeks". If an ingester change ever stops emitting tombstones,
    # this test is what says the accessor was never the thing protecting us.
    ghosted = [r for r in PANEL_ROWS if not (r[2] == "QB3" and r[4] is None)]
    rows = _chart(_panel(ghosted), "2025-11-30", team="KC")
    assert "QB3" in {r["slot"] for r in rows}


def test_two_panels_on_one_day_resolve_to_the_later_observation():
    # Four measured days carry 2-3 panels. as_of is day-granular, so only
    # observed_at can order them.
    rows = _chart(_panel(), "2025-09-15", team="CIN")
    assert {r["slot"]: r["occupant"] for r in rows}["QB2"] == "Browning2"
    assert [r["observed_at"] for r in rows if r["slot"] == "QB2"] == [P3B]


def test_future_observations_are_hidden_until_knowable():
    # Leakage, fact-time direction: the whole file is loaded, and only
    # knowable_as_of hides P2/P3.
    conn = _panel()
    assert {r["slot"]: r["occupant"] for r in _chart(conn, "2025-09-07", team="CIN")} == {
        "QB1": "Burrow", "QB2": "Browning"
    }
    assert {r["slot"]: r["occupant"] for r in _chart(conn, "2025-09-08", team="CIN")} == {
        "QB1": "Browning", "QB2": "Burrow"
    }


def test_backfilled_history_is_invisible_historically_and_visible_under_latest_truth():
    # THE T2 CONTRACT for this accessor (design note F8, measured on seven others).
    # Rows retrieved TODAY with a 2025 knowable time read EMPTY under the default
    # view — silently. That empty result reads as "3.3 is broken", not "wrong
    # view", which is exactly why it is pinned here rather than discovered.
    conn = _panel(retrieved="2026-07-25")
    assert _chart(conn, "2025-09-01", team="KC") == []
    reader = base.latest_truth(base.select_observed_as_of)
    rows = reader(conn, "panel", as_of="2025-09-01", key_cols=["season", "team", "slot"],
                  extra_where="g.occupant IS NOT NULL AND g.team = 'KC'")
    assert len(rows) == 3
    # And latest_truth still gates FACT time: nothing was knowable before 09-01.
    assert reader(conn, "panel", as_of="2025-08-31",
                  key_cols=["season", "team", "slot"]) == []


def test_historical_view_ignores_a_correction_that_arrived_later():
    # Stage 3: two VERSIONS of the same observation. Upstream restated P1's KC
    # QB2 on 09-20; a replay as-of 09-15 must still see what we held then.
    conn = _panel([r for r in PANEL_ROWS if r[3][:10] <= "2025-09-01"], retrieved="2025-09-01")
    conn.execute(
        "INSERT INTO panel VALUES (2025, 'KC', 'QB2', ?, 'Corrected', ?, '2025-09-20')",
        (P1, P1[:10]),
    )
    conn.commit()
    seen = {r["slot"]: r["occupant"] for r in _chart(conn, "2025-09-15", team="KC")}
    assert seen["QB2"] == "Minshew"
    later = {r["slot"]: r["occupant"] for r in _chart(conn, "2025-09-25", team="KC")}
    assert later["QB2"] == "Corrected"
    lt = {r["slot"]: r["occupant"]
          for r in _chart(conn, "2025-09-15", team="KC", view="latest_truth")}
    assert lt["QB2"] == "Corrected"


def test_a_correction_does_not_promote_a_stale_observation():
    # The trap in ordering the two resolutions the other way: a LATE-RETRIEVED
    # version of an OLD observation must not outrank a newer observation.
    conn = _panel()
    conn.execute(
        "INSERT INTO panel VALUES (2025, 'CIN', 'QB2', ?, 'StaleFix', ?, '2026-08-01')",
        (P1, P1[:10]),
    )
    conn.commit()
    seen = {r["slot"]: r["occupant"]
            for r in _chart(conn, "2026-09-01", team="CIN", view="latest_truth")}
    assert seen["QB2"] == "Browning2"          # P3B still wins; the fix applies to P1


def test_observed_read_rejects_a_bad_view_and_an_empty_key():
    conn = _panel()
    with pytest.raises(ValueError, match="unknown as-of view"):
        base.select_observed_as_of(conn, "panel", as_of="2025-09-30",
                                   key_cols=["season"], view="future")
    with pytest.raises(ValueError, match="at least one key column"):
        base.select_observed_as_of(conn, "panel", as_of="2025-09-30", key_cols=[])


def test_observed_read_requires_an_explicit_as_of():
    with pytest.raises(TypeError):
        base.select_observed_as_of(_panel(), "panel", key_cols=["season"])


def test_observed_read_works_on_the_shipped_depth_chart_slots_table(db):
    """Against the REAL migration-007 DDL, not a hand-rolled stand-in.

    Catches: key/column drift between this accessor's contract and the shipped
    table, and that `espn_id IS NULL` is usable as the tombstone predicate.
    Does NOT catch anything about the ingester or upstream.
    """
    cols = ("season, team, pos_grp_id, pos_id, pos_rank, observed_at, pos_abb, "
            "espn_id, retrieved_as_of, knowable_as_of")
    rows = [
        (2025, "KC", "21", "1", 1, P1, "QB", "3139477", BULK, P1[:10]),
        (2025, "KC", "21", "1", 2, P1, "QB", "2578570", BULK, P1[:10]),
        (2025, "KC", "21", "1", 3, P1, "QB", "4361050", BULK, P1[:10]),
        (2025, "KC", "21", "1", 3, P2, "QB", None, BULK, P2[:10]),   # tombstone
    ]
    db.executemany(
        f"INSERT INTO depth_chart_slots ({cols}) VALUES ({', '.join('?' * 10)})", rows
    )
    db.commit()
    key = ["season", "team", "pos_grp_id", "pos_id", "pos_rank"]
    reader = base.latest_truth(base.select_observed_as_of)
    before = reader(db, "depth_chart_slots", as_of="2025-09-01", key_cols=key,
                    extra_where="g.espn_id IS NOT NULL")
    after = reader(db, "depth_chart_slots", as_of="2025-09-08", key_cols=key,
                   extra_where="g.espn_id IS NOT NULL")
    assert sorted(r["pos_rank"] for r in before) == [1, 2, 3]
    assert sorted(r["pos_rank"] for r in after) == [1, 2]


# ===========================================================================
# upsert(key_cols=) — the honest count (item 3.2c, F-G)
# ===========================================================================

COLLIDE_SCHEMA = """
CREATE TABLE collide (
    k               TEXT NOT NULL,
    v               REAL,
    retrieved_as_of TEXT NOT NULL,
    knowable_as_of  TEXT NOT NULL,
    PRIMARY KEY (k, retrieved_as_of)
);
"""


def _collide():
    conn = connect(":memory:")
    conn.executescript(COLLIDE_SCHEMA)
    return conn


def _row(k, v):
    return {"k": k, "v": v, "retrieved_as_of": "2026-07-25", "knowable_as_of": "2025-09-01"}


def _stored(conn):
    return conn.execute("SELECT COUNT(*) FROM collide").fetchone()[0]


def test_upsert_without_key_cols_still_reports_rows_offered():
    # The pre-3.2c behaviour, unchanged for the twelve callers that have not
    # opted in: three rows offered, TWO stored, and the return value says 3.
    conn = _collide()
    assert base.upsert(conn, "collide", [_row("a", 1), _row("a", 2), _row("b", 3)]) == 3
    assert _stored(conn) == 2


def test_upsert_with_key_cols_returns_distinct_keys_written():
    conn = _collide()
    with base.collect_drops() as tally:
        written = base.upsert(conn, "collide", [_row("a", 1), _row("a", 2), _row("b", 3)],
                              key_cols=["k", "retrieved_as_of"])
    assert written == 2 == _stored(conn)
    assert tally["collapsed"] == 1
    assert tally["duplicated"] == 0
    # And the collapse did NOT arrive on either existing channel — folding it in
    # as by-design filtering would keep it off run_ingest's drop ceiling.
    assert tally["dropped"] == 0 and tally["filtered"] == 0


def test_the_naive_post_hoc_fix_is_measurably_wrong():
    # Pins WHY detection is pre-insert: SQLite reports three changes for three
    # rows that collapse to two, so total_changes cannot see the loss.
    conn = _collide()
    before = conn.total_changes
    base.upsert(conn, "collide", [_row("a", 1), _row("a", 2), _row("b", 3)])
    assert conn.total_changes - before == 3
    assert _stored(conn) == 2


def test_byte_identical_duplicates_are_separated_from_real_collapse():
    # The legacy depth charts ship 145-207 byte-identical duplicate rows a
    # season. Nothing is lost by storing one, so they must not push a healthy
    # source over the drop ceiling — but they are still counted.
    conn = _collide()
    with base.collect_drops() as tally:
        written = base.upsert(
            conn, "collide", [_row("a", 1), _row("a", 1), _row("b", 2)],
            key_cols=["k", "retrieved_as_of"],
        )
    assert written == 2 == _stored(conn)
    assert tally["duplicated"] == 1
    assert tally["collapsed"] == 0


def test_a_mixed_batch_counts_each_class_on_its_own_channel():
    conn = _collide()
    rows = [_row("a", 1), _row("a", 1), _row("b", 2), _row("b", 3), _row("c", 4)]
    with base.collect_drops() as tally:
        written = base.upsert(conn, "collide", rows, key_cols=["k", "retrieved_as_of"])
    assert written == 3 == _stored(conn)
    assert (tally["duplicated"], tally["collapsed"]) == (1, 1)


def test_the_collapse_is_logged_loudly_and_the_duplicate_is_not(caplog):
    conn = _collide()
    with caplog.at_level(logging.WARNING, logger="ziggurat.data.nfl"):
        base.upsert(conn, "collide", [_row("a", 1), _row("a", 2), _row("b", 1), _row("b", 1)],
                    key_cols=["k", "retrieved_as_of"])
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("COLLAPSED 1/4" in m for m in warnings)
    assert not any("byte-identical" in m for m in warnings)


def test_upsert_does_not_change_which_row_wins():
    # The count changes; the data does not. injuries orders its batch so the
    # most recent report lands last (F-F) — that must keep working.
    conn = _collide()
    base.upsert(conn, "collide", [_row("a", 1), _row("a", 2)], key_cols=["k", "retrieved_as_of"])
    assert conn.execute("SELECT v FROM collide WHERE k = 'a'").fetchone()[0] == 2.0


def test_key_cols_must_match_the_declared_primary_key():
    # A SUBSET counts collisions SQLite does not make; a SUPERSET misses the ones
    # it does. Either way the returned number is uninterpretable, so it raises
    # instead of being reported.
    conn = _collide()
    with pytest.raises(ValueError, match="does not match the declared primary key"):
        base.upsert(conn, "collide", [_row("a", 1)], key_cols=["k"])
    with pytest.raises(ValueError, match="does not match the declared primary key"):
        base.upsert(conn, "collide", [_row("a", 1)], key_cols=["k", "retrieved_as_of", "v"])


def test_a_three_row_chain_counts_one_loss_and_one_duplicate():
    """A, B, B loses exactly ONE fact — the tally used to say two.

    `INSERT OR REPLACE` overwrites whatever currently occupies the key, so the
    question "was a fact lost here?" is asked against the row the previous
    statement left there. Comparing every row to the FIRST one seen made the
    second B — byte-identical to the row already stored — read as a second
    collapse. It failed toward alarm, but this tally gates run_ingest's 20% drop
    ceiling, and a counter that over-reports is one an operator learns to ignore.
    """
    conn = _collide()
    rows = [_row("a", 1), _row("a", 2), _row("a", 2)]
    with base.collect_drops() as tally:
        written = base.upsert(conn, "collide", rows, key_cols=["k", "retrieved_as_of"])
    assert written == 1 == _stored(conn)
    assert tally["collapsed"] == 1     # only the v=1 fact was lost
    assert tally["duplicated"] == 1    # the repeated v=2 row lost nothing
    # And the stored row is still the last one offered — counting never moves data.
    assert conn.execute("SELECT v FROM collide WHERE k = 'a'").fetchone()[0] == 2.0


#: `collide` declares its key column NOT NULL; six live tables do not — including
#: `adp_rankings.scrape_date` and `snap_counts.team`, both of which sit in a key
#: that `base.upsert` is now asked to count on. This is that shape.
NULLABLE_KEY_SCHEMA = """
CREATE TABLE loosekey (
    k               TEXT,
    v               REAL,
    retrieved_as_of TEXT NOT NULL,
    knowable_as_of  TEXT NOT NULL,
    PRIMARY KEY (k, retrieved_as_of)
);
"""


def test_a_null_key_column_is_exempt_from_collision_accounting(caplog):
    """SQLite's PK index is a UNIQUE index, and every NULL in one is distinct.

    So three rows with k=NULL are three STORED rows, not two collapses. Python
    tuple equality disagrees (`(None,) == (None,)`), and without the exemption a
    table that started serving NULLs in a key column would report a phantom
    collapse for every row after the first — enough of them to fail a healthy
    pull against the 20% drop ceiling.
    """
    conn = connect(":memory:")
    conn.executescript(NULLABLE_KEY_SCHEMA)
    rows = [_row(None, 1), _row(None, 2), _row(None, 3), _row("b", 4)]
    with caplog.at_level(logging.WARNING, logger="ziggurat.data.nfl"):
        with base.collect_drops() as tally:
            written = base.upsert(conn, "loosekey", rows, key_cols=["k", "retrieved_as_of"])
    stored = conn.execute("SELECT COUNT(*) FROM loosekey").fetchone()[0]
    assert stored == 4              # SQLite kept all three NULL-keyed rows
    assert written == 4             # ...and the count says so
    assert tally["collapsed"] == 0 and tally["duplicated"] == 0
    # Not silent: a NULL key column means the row is stored and unreadable,
    # because select_as_of's correlated key match can never resolve a NULL.
    assert any("NULL in a PRIMARY KEY column" in r.getMessage() for r in caplog.records)


def test_key_cols_absent_from_the_rows_is_loud():
    # A key column the rows do not carry would silently read as None for every
    # row, collapsing the whole batch to one key and reporting a phantom loss.
    conn = _collide()
    thin = [{"k": "a", "v": 1.0, "knowable_as_of": "2025-09-01"}]   # no retrieved_as_of
    with pytest.raises(ValueError, match="absent from the rows"):
        base.upsert(conn, "collide", thin, key_cols=["k", "retrieved_as_of"])


def test_the_widened_snap_counts_key_holds_a_two_team_week(db):
    """F-E end to end through upsert: the measured 2021 wk12 two-team row.

    Before migration 007 the second team's line silently replaced the first and
    the ingester still returned 2. Now both land and the count says 2.
    """
    rows = [
        {"pfr_player_id": "DaviJa06", "season": 2021, "week": 12, "team": "MIA",
         "defense_snaps": 10, "retrieved_as_of": "2026-07-25", "knowable_as_of": "2021-11-28"},
        {"pfr_player_id": "DaviJa06", "season": 2021, "week": 12, "team": "CIN",
         "defense_snaps": 23, "retrieved_as_of": "2026-07-25", "knowable_as_of": "2021-11-28"},
    ]
    with base.collect_drops() as tally:
        written = base.upsert(db, "snap_counts", rows,
                              key_cols=["pfr_player_id", "season", "week", "team",
                                        "retrieved_as_of"])
    assert written == 2
    assert db.execute("SELECT COUNT(*) FROM snap_counts").fetchone()[0] == 2
    assert tally["collapsed"] == 0


# ===========================================================================
# gsis_by_pfr collision order (item 3.2c, F-J)
# ===========================================================================

def _players(db, rows):
    db.executemany(
        "INSERT INTO players (gsis_id, pfr_id, retrieved_as_of, knowable_as_of) "
        "VALUES (?, ?, '2026-07-25', '2026-07-25')", rows,
    )
    db.commit()
    return db


def test_pfr_collision_resolves_to_the_real_gsis_in_either_insertion_order(db):
    # 15 live pfr ids map to both a pseudo-id and a real one. The winner used to
    # be SQLite's scan order, and snap_counts FREEZES it into the stored row.
    _players(db, [("ALT577722", "SomePfr"), ("00-0041453", "SomePfr")])
    assert base.gsis_by_pfr(db)["SomePfr"] == "00-0041453"

    other = connect(":memory:")
    from ziggurat.data.store import apply_schema
    apply_schema(other)
    _players(other, [("00-0041453", "SomePfr"), ("ALT577722", "SomePfr")])
    assert base.gsis_by_pfr(other)["SomePfr"] == "00-0041453"
    other.close()


def test_two_real_gsis_on_one_pfr_id_still_resolve_deterministically(db):
    # No preference rule can be right here — but it can be STABLE, which is the
    # property snap_counts needs. Lexicographic is the tiebreak, both ways round.
    _players(db, [("00-0041453", "Twin"), ("00-0039999", "Twin")])
    assert base.gsis_by_pfr(db)["Twin"] == "00-0039999"


def test_the_pfr_collision_is_still_logged(caplog, db):
    _players(db, [("ALT577722", "SomePfr"), ("00-0041453", "SomePfr")])
    with caplog.at_level(logging.WARNING, logger="ziggurat.data.nfl"):
        base.gsis_by_pfr(db)
    assert any("maps to multiple gsis" in r.getMessage() for r in caplog.records)


def test_an_uncollided_pfr_id_is_unaffected(db):
    _players(db, [("00-0041453", "Alone"), ("ALT577722", "Other")])
    got = base.gsis_by_pfr(db)
    assert got["Alone"] == "00-0041453"
    assert got["Other"] == "ALT577722"     # a pseudo-id with no rival is kept
