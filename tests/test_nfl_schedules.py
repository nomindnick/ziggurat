"""Cached-fixture integration + leakage tests for schedule ingestion."""

from ziggurat.data.nfl import base, schedules


def test_ingest_and_query_by_week(db, nfl_fixture):
    df = nfl_fixture("schedules")
    n = schedules.ingest_schedules(db, df, retrieved_as_of="2023-08-01")
    assert n == len(df)

    wk5 = schedules.get_schedule(db, as_of="2023-08-01", season=2023, week=5)
    expected_wk5 = int((df[df.week == 5].shape[0]))
    assert len(wk5) == expected_wk5 > 0


def test_game_date_map_indexes_both_sides(db, nfl_fixture):
    df = nfl_fixture("schedules")
    schedules.ingest_schedules(db, df, retrieved_as_of="2023-08-01")
    gdm = base.game_date_map(db)
    # Every home/away team of a week-5 game resolves to that game's gameday.
    row = df[df.week == 5].iloc[0]
    assert gdm[(2023, 5, row["home_team"])] == row["gameday"][:10]
    assert gdm[(2023, 5, row["away_team"])] == row["gameday"][:10]


def test_week_first_gameday_is_the_earliest(db, nfl_fixture):
    df = nfl_fixture("schedules")
    schedules.ingest_schedules(db, df, retrieved_as_of="2023-08-01")
    firsts = base.week_first_gameday_map(db)
    assert firsts[(2023, 6)] == df[df.week == 6]["gameday"].min()[:10]


def test_schedule_leakage_before_release(db, nfl_fixture):
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    # Structural schedule is knowable at the 2023-08-01 anchor; a day earlier, nothing.
    assert schedules.get_schedule(db, as_of="2023-07-31", season=2023) == []
    assert len(schedules.get_schedule(db, as_of="2023-08-01", season=2023)) > 0


def test_playoff_bracket_is_not_leaked_at_the_preseason_anchor(db, nfl_fixture):
    # The playoff bracket (who plays whom) is NOT known at the preseason release;
    # non-REG rows must be stamped with their gameday, not the Aug-1 anchor.
    df = nfl_fixture("schedules")
    schedules.ingest_schedules(db, df, retrieved_as_of="2023-08-01")
    preseason = schedules.get_schedule(db, as_of="2023-08-01", season=2023)
    assert preseason, "regular-season games are knowable preseason"
    assert all(r["game_type"] == "REG" for r in preseason), "no playoff game leaks preseason"

    # A wildcard game (played 2024-01) only becomes visible on its gameday.
    wc = df[df.game_type == "WC"].iloc[0]
    gid = wc["game_id"]
    assert not _has_game(schedules.get_schedule(db, as_of="2024-01-01", season=2023), gid)
    assert _has_game(schedules.get_schedule(db, as_of=wc["gameday"][:10], season=2023), gid)


def _has_game(rows, game_id):
    return any(r["game_id"] == game_id for r in rows)
