"""Cached-fixture integration + leakage tests for snap-count ingestion.

Snap counts are PFR-keyed and post-game, so two upstream sources must be
ingested first: players (to resolve gsis_id via the crosswalk) and schedules
(to resolve the team gameday that becomes knowable_as_of).

WHAT THE FIXTURE CANNOT CATCH (item 3.1b's lesson): tests/fixtures/nfl is a
frozen 2023 weeks 5-6 slice, so nothing here proves the live 2026 frame still
carries the columns ``require_columns`` demands, and nothing here would have
caught the two-club week that finding F-E is about — the fixture contains no
mid-week trade. The two-club tests below therefore build the collision from the
REAL measured case (DaviJa06 / Jalen Davis, 2021 week 12, MIA 10 def snaps and
CIN 23 def snaps — reproduced against live upstream on 2026-07-25) rather than
waiting for a fixture to happen to contain one.
"""

import pandas as pd

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


def _two_club_week(db, nfl_fixture):
    """Reproduce the measured two-club week on top of the 2023 fixture.

    Live shape, measured 2026-07-25 on the real 2021 file::

        DaviJa06  Jalen Davis  MIA  CAR  2021 12  def 10.0  2021_12_CAR_MIA
        DaviJa06  Jalen Davis  CIN  PIT  2021 12  def 23.0  2021_12_PIT_CIN

    The fixture has no such row, so we synthesize one onto a real week-5 player
    by cloning his line to a second club that also plays that week (so both
    rows resolve a real gameday). Returns (frame, pfr_id, team_a, team_b).
    """
    df = nfl_fixture("snap_counts")
    gdm = base.game_date_map(db)
    week5_teams = sorted({t for (s, w, t) in gdm if s == 2023 and w == 5})

    original = df[df.week == 5].iloc[0]
    team_a = original.team
    team_b = next(t for t in week5_teams if t != team_a and t not in base.TEAM_ALIASES)

    traded = original.copy()
    traded["team"] = team_b
    traded["opponent"] = "ZZZ"
    traded["game_id"] = f"2023_05_{team_b}_TRADE"
    traded["defense_snaps"] = 23.0
    traded["offense_snaps"] = 0.0
    original = original.copy()
    original["defense_snaps"] = 10.0
    original["offense_snaps"] = 0.0

    rest = df.drop(index=original.name)
    frame = pd.concat([rest, pd.DataFrame([original, traded])], ignore_index=True)
    return frame, original.pfr_player_id, team_a, team_b


def test_a_player_who_changed_clubs_mid_week_keeps_both_snap_lines(db, nfl_fixture):
    """F-E. Before migration 007 the PK was (pfr, season, week, retrieved_as_of),
    so the second club's line REPLACED the first: the ingester returned N and the
    table held N-1, with note_drops reporting 0. Silent loss."""
    _seed_upstream(db, nfl_fixture)
    frame, pfr, team_a, team_b = _two_club_week(db, nfl_fixture)

    written = snap_counts.ingest_snap_counts(db, frame, retrieved_as_of="2023-10-20")
    stored = db.execute("SELECT COUNT(*) FROM snap_counts").fetchone()[0]
    assert written == stored == len(frame)  # nothing silently collapsed

    rows = snap_counts.get_snap_counts(
        db, as_of="2023-10-20", season=2023, week=5, pfr_player_id=pfr
    )
    assert {r["team"]: r["defense_snaps"] for r in rows} == {team_a: 10.0, team_b: 23.0}


def test_team_filter_selects_one_club_of_a_two_club_week(db, nfl_fixture):
    _seed_upstream(db, nfl_fixture)
    frame, pfr, team_a, team_b = _two_club_week(db, nfl_fixture)
    snap_counts.ingest_snap_counts(db, frame, retrieved_as_of="2023-10-20")

    only_b = snap_counts.get_snap_counts(
        db, as_of="2023-10-20", season=2023, week=5, pfr_player_id=pfr, team=team_b
    )
    assert len(only_b) == 1
    assert only_b[0]["defense_snaps"] == 23.0


def test_accessor_key_cols_include_team_so_neither_club_is_shadowed(db, nfl_fixture):
    """The accessor half of F-E, and the half a PK-only fix would miss.

    ``select_as_of`` resolves ``MAX(retrieved_as_of)`` per key. With ``team``
    absent from ``key_cols`` the two clubs' rows resolve against EACH OTHER, so
    the club pulled on the earlier day is in the table and invisible to every
    read. Measured by the 3.2c foundation agent: MIA @ 2026-07-25 + CIN @
    2026-07-26 returned 1 row, CIN only.
    """
    _seed_upstream(db, nfl_fixture)
    frame, pfr, team_a, team_b = _two_club_week(db, nfl_fixture)
    one = frame[~((frame.pfr_player_id == pfr) & (frame.week == 5) & (frame.team == team_b))]
    other = frame[(frame.pfr_player_id == pfr) & (frame.week == 5) & (frame.team == team_b)]

    # Two separate pulls on two different days — the ordinary weekly cadence.
    snap_counts.ingest_snap_counts(db, one, retrieved_as_of="2023-10-20")
    snap_counts.ingest_snap_counts(db, other, retrieved_as_of="2023-10-21")

    assert db.execute("SELECT COUNT(*) FROM snap_counts").fetchone()[0] == len(frame)
    rows = snap_counts.get_snap_counts(
        db, as_of="2023-10-21", season=2023, week=5, pfr_player_id=pfr
    )
    assert {r["team"] for r in rows} == {team_a, team_b}, (
        "the club retrieved on the earlier day was shadowed — key_cols is missing 'team'"
    )
    assert snap_counts._KEY_COLS == ["pfr_player_id", "season", "week", "team"]


def test_a_re_pull_of_a_two_club_week_still_resolves_to_one_row_per_club(db, nfl_fixture):
    """The widened key must not turn a REVISION into a duplicate: pulling the
    same week twice still yields exactly one (newest) row per club."""
    _seed_upstream(db, nfl_fixture)
    frame, pfr, team_a, team_b = _two_club_week(db, nfl_fixture)
    snap_counts.ingest_snap_counts(db, frame, retrieved_as_of="2023-10-20")
    snap_counts.ingest_snap_counts(db, frame, retrieved_as_of="2023-10-21")

    rows = snap_counts.get_snap_counts(
        db, as_of="2023-10-21", season=2023, week=5, pfr_player_id=pfr
    )
    assert len(rows) == 2
    assert {r["retrieved_as_of"] for r in rows} == {"2023-10-21"}


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
