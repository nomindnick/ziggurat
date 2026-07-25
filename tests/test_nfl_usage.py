"""The item-1.4 done-when: "usage deltas for all RBs as of 2023 week 6",
correct and leakage-tested.

Exercises the whole spine end to end: players crosswalk + schedules (knowledge
time) + weekly_stats + snap_counts, joined into week-over-week RB usage deltas
that appear only once week 6 is knowable.
"""


import pytest

from ziggurat.data.nfl import base, players, schedules, snap_counts, weekly_stats
from ziggurat.data.nfl.usage import usage_deltas


def _seed(db, nfl_fixture, *, retrieved="2026-07-16"):
    """Full spine bulk-pulled now; callers explicitly request latest truth."""
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"), retrieved_as_of=retrieved)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of=retrieved)


def test_rb_usage_deltas_as_of_week6(db, nfl_fixture):
    _seed(db, nfl_fixture)
    # After week 6's games (2023-10-16 latest), week-6 deltas exist.
    deltas = usage_deltas(db, view="latest_truth", as_of="2023-10-17", season=2023, week=6, position="RB")
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
    deltas = usage_deltas(db, view="latest_truth", as_of="2023-10-17", season=2023, week=6, position="RB")
    # Snap deltas are always present as keys; at least one RB actually resolved a
    # non-None snap delta through the pfr->gsis crosswalk (unknown != 0.0).
    assert all("d_offense_pct" in d for d in deltas)
    assert any(d["d_offense_pct"] is not None for d in deltas)


def test_no_prior_game_is_flagged_not_dropped(db, nfl_fixture):
    # A player active in week 6 but with no prior knowable week is carried with
    # prior_week=None and null deltas — the bye/return cohort stays visible.
    _seed(db, nfl_fixture)
    deltas = usage_deltas(db, view="latest_truth", as_of="2023-10-17", season=2023, week=6, position="RB")
    for d in deltas:
        if d["prior_week"] is None:
            assert d["d_carries"] is None and d["d_targets"] is None
        else:
            assert d["prior_week"] < 6 and d["d_carries"] is not None


def test_usage_deltas_leak_nothing_before_week6_is_played(db, nfl_fixture):
    _seed(db, nfl_fixture)
    # 2023-10-11 is after week 5 but before every week-6 game: a week-6 delta
    # cannot exist yet, even though all rows were bulk-pulled in 2026.
    assert usage_deltas(db, view="latest_truth", as_of="2023-10-11", season=2023, week=6, position="RB") == []
    # And the symptom is knowability, not missing data: week-5-vs-4 would need
    # week 4 which the fixture omits, so week 6 is the only formable delta.
    assert usage_deltas(db, view="latest_truth", as_of="2023-10-20", season=2023, week=6, position="RB")


def test_position_filter_defaults_to_rb_but_is_overridable(db, nfl_fixture):
    _seed(db, nfl_fixture)
    wrs = usage_deltas(db, view="latest_truth", as_of="2023-10-17", season=2023, week=6, position="WR")
    assert wrs and all(d["position"] == "WR" for d in wrs)


# =====================================================================
# The mid-week club change (item 3.2c, F-E — verification phase)
# =====================================================================
#
# Migration 007 widened the `snap_counts` primary key to include `team`, so a
# player who moved mid-week now has BOTH clubs' snap lines stored and
# `get_snap_counts` returns both. `usage_deltas` indexed them into a dict keyed
# on (gsis_id, week), so the second row overwrote the first and the survivor was
# whichever SQL yielded last — an arbitrary answer to the question this module
# exists to answer (did his role change?), for the one event most likely to
# change it.
#
# Measured population on the real 2021-2025 backfill: ONE player-week
# (DaviJa06 / 2021 wk12, a cornerback with 0.0 offensive snaps at both MIA and
# CIN), so nothing in the stored data moves. The rows below are synthetic and
# give the player real snaps at both clubs, because a zero-impact real case
# cannot demonstrate the failure — and a mid-season RB trade will.


def _traded_player_week(db, *, retrieved="2026-07-16"):
    """One RB, week 5 at one club, week 6 split across two (a mid-week move)."""
    for week, gameday in ((5, "2023-10-08"), (6, "2023-10-15")):
        db.execute(
            "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, "
            "gameday, home_team, away_team, knowable_as_of, retrieved_as_of) "
            "VALUES (?,?,?,'REG',?,?,?,?,?)",
            (f"2023_{week:02d}_AAA_BBB", 2023, week, gameday, "BBB", "AAA",
             gameday, retrieved))
        db.execute(
            "INSERT OR REPLACE INTO weekly_stats (player_id, season, week, position, "
            "recent_team, carries, targets, knowable_as_of, retrieved_as_of) "
            "VALUES (?,?,?,'RB',?,?,?,?,?)",
            ("00-0099001", 2023, week, "AAA", 10, 3, gameday, retrieved))
    # week 5: one club, 20 of 50 plays.  week 6: 10 of 40 at AAA + 30 of 50 at
    # BBB = 40 snaps of 90 plays.
    lines = [("AAA", 5, 20.0, 0.40), ("AAA", 6, 10.0, 0.25), ("BBB", 6, 30.0, 0.60)]
    for team, week, snaps, pct in lines:
        db.execute(
            "INSERT OR REPLACE INTO snap_counts (pfr_player_id, gsis_id, season, week, "
            "team, offense_snaps, offense_pct, knowable_as_of, retrieved_as_of) "
            "VALUES ('SyntPl00','00-0099001',2023,?,?,?,?,?,?)",
            (week, team, snaps, pct,
             "2023-10-08" if week == 5 else "2023-10-15", retrieved))
    db.commit()


def test_a_mid_week_club_change_is_stored_as_two_snap_lines(db):
    """The premise, so the test below cannot pass for the wrong reason."""
    _traded_player_week(db)
    rows = snap_counts.get_snap_counts(
        db, view="latest_truth", as_of="2023-10-16", season=2023, week=6)
    assert {r["team"] for r in rows} == {"AAA", "BBB"}


def test_a_traded_players_delta_counts_both_clubs_snaps(db):
    """Not scan order: the week-6 workload is 10 + 30 = 40 snaps against week
    5's 20, so the delta is +20 — never +(-10) or +10, which is what keeping one
    arbitrary club's row produces."""
    _traded_player_week(db)
    rows = base.latest_truth(usage_deltas)(
        db, as_of="2023-10-16", season=2023, week=6, position="RB")
    assert len(rows) == 1
    assert rows[0]["d_offense_snaps"] == 20.0


def test_a_traded_players_share_is_reweighted_not_added(db):
    """Two shares of two different play counts cannot be summed (0.25 + 0.60 =
    0.85 is not a share of anything). The counts invert to 40 and 50 plays, so
    the week's real share is 40/90 and the delta is 40/90 - 0.40."""
    _traded_player_week(db)
    rows = base.latest_truth(usage_deltas)(
        db, as_of="2023-10-16", season=2023, week=6, position="RB")
    assert rows[0]["d_offense_pct"] == pytest.approx(40 / 90 - 0.40)


def test_an_uninvertible_share_reports_unknown_not_zero(db):
    """A club with snaps but a 0.0 share cannot yield its play count, so the
    combined share is UNKNOWN. `None` says so; 0.0 would be a real number the
    operator cannot tell from a real flat week (Rule 6)."""
    _traded_player_week(db)
    db.execute("UPDATE snap_counts SET offense_pct = 0.0 "
               "WHERE week = 6 AND team = 'BBB'")
    db.commit()
    rows = base.latest_truth(usage_deltas)(
        db, as_of="2023-10-16", season=2023, week=6, position="RB")
    assert rows[0]["d_offense_snaps"] == 20.0     # counts still add
    assert rows[0]["d_offense_pct"] is None       # the share does not


def test_the_ordinary_single_club_week_is_untouched(db, nfl_fixture):
    """132,615 of the 132,616 real rows take this path; the fold must pass a
    one-club week through unchanged."""
    _seed(db, nfl_fixture)
    deltas = usage_deltas(db, view="latest_truth", as_of="2023-10-17",
                          season=2023, week=6, position="RB")
    with_snaps = [d for d in deltas if d["d_offense_snaps"] is not None]
    assert with_snaps
    assert all(isinstance(d["d_offense_pct"], float) for d in with_snaps)
