"""Cached-fixture integration + leakage tests for weekly-stats ingestion.

Weekly lines are post-game facts stamped with the team's gameday, so schedules
must be ingested first (base.game_date_map resolves (season, week, recent_team)).
Fixture is 2023 weeks 5 (2023-10-05..09) and 6 (2023-10-12..16).

Leakage note: the frozen base.select_as_of gates on BOTH knowable_as_of <= as_of
AND retrieved_as_of <= as_of (a fact is unreadable before it was pulled). To
isolate and prove the game-date knowable gate — the crux of this source — the
leakage test pulls on 2023-10-11 (retrieved_as_of <= as_of) so the gameday gate
is the binding constraint: week 5 (played) is readable, week 6 (not yet) is not.
"""

from ziggurat.data.nfl import schedules, weekly_stats

# Breece Hall (NYJ RB), week 5 2023: 3 receptions, 177 rushing yards.
_HALL = "00-0038120"


def _load_schedules(db, nfl_fixture):
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")


def test_ingest_and_get_rb_week5(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats")
    n = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")
    assert n == len(df) > 0  # every fixture team resolves to a gameday; none dropped

    rows = weekly_stats.get_weekly_stats(
        db, as_of="2023-10-20", season=2023, week=5, player_id=_HALL
    )
    assert len(rows) == 1
    row = rows[0]
    # A known RB's week-5 receptions / rushing yards are present and numeric.
    assert isinstance(row["receptions"], (int, float))
    assert isinstance(row["rushing_yards"], (int, float))
    assert row["receptions"] == 3
    assert row["rushing_yards"] == 177.0
    # knowable_as_of is that team's week-5 gameday, not the pull date.
    assert row["knowable_as_of"].startswith("2023-10-0")


def test_position_filter(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    weekly_stats.ingest_weekly_stats(
        db, nfl_fixture("weekly_stats"), retrieved_as_of="2023-10-20"
    )
    rbs = weekly_stats.get_weekly_stats(db, as_of="2023-10-20", week=5, position="RB")
    assert rbs and all(r["position"] == "RB" for r in rbs)


def test_weekly_stats_leakage_by_gameday(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    # Pull on 2023-10-11 so retrieved_as_of <= as_of and the gameday gate binds.
    weekly_stats.ingest_weekly_stats(
        db, nfl_fixture("weekly_stats"), retrieved_as_of="2023-10-11"
    )

    # 2023-10-11 is after every week-5 game (10-05..09) but before every week-6
    # game (10-12..16): week 5 is knowable, week 6 must be hidden.
    seen = weekly_stats.get_weekly_stats(db, as_of="2023-10-11", season=2023)
    weeks = {r["week"] for r in seen}
    assert weeks == {5}, "week 5 knowable, week 6 must be gated out"

    # By 2023-10-20 both weeks are knowable (and retrieved).
    later = {r["week"] for r in weekly_stats.get_weekly_stats(db, as_of="2023-10-20", season=2023)}
    assert later == {5, 6}


def test_unresolvable_rows_dropped_without_schedules(db, nfl_fixture):
    # With no schedules loaded, no (season, week, recent_team) resolves to a
    # gameday, so every row is dropped (counted out) rather than stored with a
    # NULL/leaky knowable_as_of.
    df = nfl_fixture("weekly_stats")
    n = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")
    assert n == 0
    assert weekly_stats.get_weekly_stats(db, as_of="2023-10-20", season=2023) == []
