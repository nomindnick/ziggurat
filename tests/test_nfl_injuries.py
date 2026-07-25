"""Cached-fixture integration + leakage tests for injury-report ingestion.

The leakage test proves REPORT-DATE gating: an injury report is knowable the
moment it was filed (its ``date_modified``), not on gameday — so the boundary
that flips is the report's own timestamp, not the 10-11 game boundary.
"""

from datetime import date, timedelta

from ziggurat.data.nfl import base, injuries, schedules


def test_ingest_and_get(db, nfl_fixture):
    df = nfl_fixture("injuries")
    n = injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")
    assert n == len(df)  # every fixture row carries a date_modified -> none dropped

    # report_status / practice_status populated for a known player-week.
    src = df[(df.week == 6) & df.report_status.notna() & df.practice_status.notna()].iloc[0]
    rows = injuries.get_injuries(db, as_of="2023-11-01", season=2023, week=6, gsis_id=src.gsis_id)
    assert len(rows) == 1
    got = rows[0]
    assert got["report_status"] == src.report_status
    assert got["practice_status"] == src.practice_status
    assert got["full_name"] == src.full_name
    assert got["date_modified"] == base.iso_date(src.date_modified)  # stored day-granular


def test_report_date_gates_knowability(db, nfl_fixture):
    df = nfl_fixture("injuries")
    # Retrieval anchored well before any week-6 report so ONLY the report-date
    # (knowable_as_of) gate can flip in the window we probe.
    injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-01")

    # Pick the latest-modified week-6 report; its date_modified D is its knowledge time.
    wk6 = df[df.week == 6].sort_values("date_modified")
    row = wk6.iloc[-1]
    gsis = row.gsis_id
    D = base.iso_date(row.date_modified)
    day_before = (date.fromisoformat(D) - timedelta(days=1)).isoformat()

    # A day before the report was filed, it is not yet knowable...
    assert injuries.get_injuries(db, as_of=day_before, season=2023, week=6, gsis_id=gsis) == []
    # ...on the report date it appears.
    shown = injuries.get_injuries(db, as_of=D, season=2023, week=6, gsis_id=gsis)
    assert len(shown) == 1
    assert shown[0]["gsis_id"] == gsis
    assert shown[0]["knowable_as_of"] == D


def test_nothing_knowable_before_any_report_filed(db, nfl_fixture):
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of="2023-10-20")
    # Reports are filed in October; before then none are knowable (knowable_as_of
    # is the report date, and retrieved_as_of does not gate).
    assert injuries.get_injuries(db, as_of="2023-09-30", season=2023) == []


def test_missing_date_modified_falls_back_to_own_team_gameday_not_week_first(db, nfl_fixture):
    # The leak the audit caught: with no date_modified, the fallback must anchor
    # to the player's OWN team gameday, never the week's first kickoff (which is
    # earlier and would expose the report before it was filed for late-week teams).
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    gdm = base.game_date_map(db)
    week6 = {t: d for (s, w, t), d in gdm.items() if s == 2023 and w == 6}
    week_first = min(week6.values())

    inj = nfl_fixture("injuries")
    # A week-6 injured player whose team plays LATER than the week's first game.
    late = next(
        r for _, r in inj[inj.week == 6].iterrows()
        if week6.get(r["team"], week_first) > week_first
    )
    team_gameday = week6[late["team"]]

    df = inj.copy()
    mask = (df.week == 6) & (df.gsis_id == late["gsis_id"])
    df.loc[mask, "date_modified"] = None  # force the fallback path
    injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")

    rows = injuries.get_injuries(db, as_of="2023-11-01", season=2023, week=6, gsis_id=late["gsis_id"])
    assert rows
    assert rows[0]["knowable_as_of"] == team_gameday          # own team gameday
    assert rows[0]["knowable_as_of"] > week_first             # NOT the leaky week-first anchor
    # And it stays hidden the day before that team plays (no leak).
    before = (date.fromisoformat(team_gameday) - timedelta(days=1)).isoformat()
    assert injuries.get_injuries(db, as_of=before, season=2023, week=6, gsis_id=late["gsis_id"]) == []


def test_a_release_without_date_modified_still_ingests(db, nfl_fixture):
    """VERIFIED 2026-07-24 against live nflverse: the 2025+ injury release no
    longer carries ``date_modified`` at all (2024 has it, 2025 does not — the
    upstream feed died after 2024 and 2025 exists only as a post-season bulk
    backfill). require_columns therefore raised on EVERY pull, and the frozen
    2023 fixture hid it: the suite stayed green over a broken production path.

    The column is optional now; every row falls back to the team-gameday anchor,
    which is coarser but still leakage-safe.
    """
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("injuries").drop(columns=["date_modified"])
    n = injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")
    assert n > 0

    game_dates = base.game_date_map(db)
    rows = injuries.get_injuries(db, as_of="2023-11-01", season=2023, week=6)
    assert rows
    for row in rows:
        assert row["date_modified"] is None
        assert row["knowable_as_of"] == game_dates[(2023, 6, row["team"])]


def test_a_release_missing_a_genuinely_required_column_still_fails_loud(db, nfl_fixture):
    """Only date_modified is optional. Real drift must still be a red build."""
    import pytest

    df = nfl_fixture("injuries").drop(columns=["report_status"])
    with pytest.raises(ValueError, match="report_status"):
        injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")
