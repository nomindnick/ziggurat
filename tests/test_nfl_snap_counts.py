"""Cached-fixture integration + leakage tests for snap-count ingestion.

Snap counts are PFR-keyed and post-game, so two upstream sources must be
ingested first: players (to resolve gsis_id via the crosswalk) and schedules
(to resolve the team gameday that becomes knowable_as_of).
"""

from ziggurat.data.nfl import base, players, schedules, snap_counts


def _seed_upstream(db, nfl_fixture):
    """Ingest the two prerequisites: crosswalk + gameday source."""
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")


def test_ingest_lands_rows_and_bridges_gsis(db, nfl_fixture):
    _seed_upstream(db, nfl_fixture)
    n = snap_counts.ingest_snap_counts(
        db, nfl_fixture("snap_counts"), retrieved_as_of="2023-10-20"
    )
    assert n > 0

    rows = snap_counts.get_snap_counts(db, as_of="2023-10-20")
    assert len(rows) == n  # single retrieval -> one row per (pfr, season, week)

    # Crosswalk bridge: some snap rows resolved a non-null gsis_id from their
    # PFR id, so they join to gsis-keyed weekly stats / NGS.
    assert any(r["gsis_id"] is not None for r in rows)


def test_specific_row_values_and_gsis(db, nfl_fixture):
    _seed_upstream(db, nfl_fixture)
    snap_counts.ingest_snap_counts(
        db, nfl_fixture("snap_counts"), retrieved_as_of="2023-10-20"
    )

    # Kenny Pickett, PIT vs BAL, week 5: 66 offensive snaps at 100%.
    rows = snap_counts.get_snap_counts(
        db, as_of="2023-10-20", season=2023, week=5, pfr_player_id="PickKe00"
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["team"] == "PIT"
    assert r["opponent"] == "BAL"
    assert r["offense_snaps"] == 66.0
    assert r["offense_pct"] == 1.0
    assert r["game_id"] == "2023_05_BAL_PIT"
    # PickKe00 is in the players crosswalk, so gsis_id is resolved.
    assert r["gsis_id"] == "00-0038102"


def test_snap_counts_leakage_by_gameday(db, nfl_fixture):
    _seed_upstream(db, nfl_fixture)
    # Bulk-pulled immutable outcomes opt into latest_truth, where game date is
    # the leakage boundary. Historical view remains empty before retrieval.
    snap_counts.ingest_snap_counts(
        db, nfl_fixture("snap_counts"), retrieved_as_of="2026-07-16"
    )

    # Before any week-5 game is played, nothing is knowable yet.
    assert snap_counts.get_snap_counts(db, as_of="2023-10-04", view="latest_truth") == []
    assert snap_counts.get_snap_counts(db, as_of="2023-10-20") == []

    # On 10-11 week 5 has been played and shows; week 6 has not and is hidden
    # purely by its future gameday — despite every row being pulled in 2026.
    weeks_wk5 = {
        r["week"]
        for r in snap_counts.get_snap_counts(
            db, as_of="2023-10-11", view="latest_truth"
        )
    }
    assert 5 in weeks_wk5
    assert 6 not in weeks_wk5

    # Once every gameday is in the past, both weeks are visible.
    weeks_all = {
        r["week"]
        for r in snap_counts.get_snap_counts(
            db, as_of="2023-10-20", view="latest_truth"
        )
    }
    assert weeks_all == {5, 6}


def test_latest_truth_helper_closes_the_bulk_backtest_footgun(db, nfl_fixture):
    # The exact backtest scenario: all history bulk-pulled "now" (2026). The
    # default historical view gates retrieval time, so a 2023 read is silently
    # empty. base.latest_truth binds the correct view so the read returns rows.
    _seed_upstream(db, nfl_fixture)
    snap_counts.ingest_snap_counts(
        db, nfl_fixture("snap_counts"), retrieved_as_of="2026-07-16"
    )

    # The footgun: default historical read of bulk history is empty, not an error.
    assert snap_counts.get_snap_counts(db, as_of="2023-10-11", season=2023) == []

    read = base.latest_truth(snap_counts.get_snap_counts)
    weeks = {r["week"] for r in read(db, as_of="2023-10-11", season=2023)}
    assert 5 in weeks and 6 not in weeks
    # Identical to spelling the view out by hand.
    explicit = {
        r["week"]
        for r in snap_counts.get_snap_counts(
            db, as_of="2023-10-11", season=2023, view="latest_truth"
        )
    }
    assert weeks == explicit
