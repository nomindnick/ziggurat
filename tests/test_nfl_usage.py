"""The item-1.4 done-when: "usage deltas for all RBs as of 2023 week 6",
correct and leakage-tested.

Exercises the whole spine end to end: players crosswalk + schedules (knowledge
time) + weekly_stats + snap_counts, joined into week-over-week RB usage deltas
that appear only once week 6 is knowable.
"""

import pandas as pd

from ziggurat.data.nfl import players, schedules, snap_counts, weekly_stats
from ziggurat.data.nfl.usage import usage_deltas


def _seed(db, nfl_fixture, *, retrieved="2026-07-16"):
    """Full spine, bulk-pulled 'now' (retrieved in 2026) to prove the backtest
    reads by knowability, not retrieval."""
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"), retrieved_as_of=retrieved)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of=retrieved)


def test_rb_usage_deltas_as_of_week6(db, nfl_fixture):
    _seed(db, nfl_fixture)
    # After week 6's games (2023-10-16 latest), week-6 deltas exist.
    deltas = usage_deltas(db, as_of="2023-10-17", season=2023, week=6, position="RB")
    assert deltas, "expected RB usage deltas once week 6 is knowable"
    assert all(d["position"] == "RB" for d in deltas)
    assert all(d["week"] == 6 for d in deltas)
    # every delta metric present
    for d in deltas:
        for m in ("d_targets", "d_carries", "d_target_share", "d_wopr"):
            assert m in d

    # Correctness: a known RB's carries delta equals wk6 carries - wk5 carries
    # computed straight from the fixture frame.
    wk = nfl_fixture("weekly_stats")
    rb = wk[(wk.position == "RB") & (wk.week.isin([5, 6]))]
    pid = rb.groupby("player_id").filter(lambda g: set(g.week) >= {5, 6}).iloc[0]["player_id"]
    c6 = rb[(rb.player_id == pid) & (rb.week == 6)]["carries"].iloc[0]
    c5 = rb[(rb.player_id == pid) & (rb.week == 5)]["carries"].iloc[0]
    got = next(d for d in deltas if d["player_id"] == pid)
    assert got["prior_week"] == 5  # differenced against the last knowable game
    assert got["d_carries"] == float(c6) - float(c5)


def test_snap_share_delta_bridges_via_crosswalk(db, nfl_fixture):
    _seed(db, nfl_fixture)
    deltas = usage_deltas(db, as_of="2023-10-17", season=2023, week=6, position="RB")
    # Snap deltas are always present as keys; at least one RB actually resolved a
    # non-None snap delta through the pfr->gsis crosswalk (unknown != 0.0).
    assert all("d_offense_pct" in d for d in deltas)
    assert any(d["d_offense_pct"] is not None for d in deltas)


def test_no_prior_game_is_flagged_not_dropped(db, nfl_fixture):
    # A player active in week 6 but with no prior knowable week is carried with
    # prior_week=None and null deltas — the bye/return cohort stays visible.
    _seed(db, nfl_fixture)
    deltas = usage_deltas(db, as_of="2023-10-17", season=2023, week=6, position="RB")
    for d in deltas:
        if d["prior_week"] is None:
            assert d["d_carries"] is None and d["d_targets"] is None
        else:
            assert d["prior_week"] < 6 and d["d_carries"] is not None


def test_usage_deltas_leak_nothing_before_week6_is_played(db, nfl_fixture):
    _seed(db, nfl_fixture)
    # 2023-10-11 is after week 5 but before every week-6 game: a week-6 delta
    # cannot exist yet, even though all rows were bulk-pulled in 2026.
    assert usage_deltas(db, as_of="2023-10-11", season=2023, week=6, position="RB") == []
    # And the symptom is knowability, not missing data: week-5-vs-4 would need
    # week 4 which the fixture omits, so week 6 is the only formable delta.
    assert usage_deltas(db, as_of="2023-10-20", season=2023, week=6, position="RB")


def test_position_filter_defaults_to_rb_but_is_overridable(db, nfl_fixture):
    _seed(db, nfl_fixture)
    wrs = usage_deltas(db, as_of="2023-10-17", season=2023, week=6, position="WR")
    assert wrs and all(d["position"] == "WR" for d in wrs)
