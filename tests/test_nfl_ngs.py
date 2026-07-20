"""Cached-fixture integration + leakage tests for Next Gen Stats ingestion.

NGS lines are post-game facts stamped with the team's gameday, so schedules must
be ingested first (base.game_date_map). Fixture is 2023 weeks 5 (2023-10-05..09)
and 6 (2023-10-12..16); as_of 2023-10-11 must show week 5 and hide week 6.

NGS labels the Rams "LAR" while schedules use "LA"; base.game_date_map aliases
LAR->LA so Rams rows resolve (regression-guarded below) rather than being
silently dropped. A genuinely unmappable team is still dropped rather than
stored with a NULL/leaky knowledge time.
"""

from ziggurat.data.nfl import base, ngs, schedules


def _load_schedules(db, nfl_fixture):
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")


def _resolvable(df, db):
    """Count of week>0 rows whose (season, week, team_abbr) game date resolves."""
    gdm = base.game_date_map(db)
    real = df[df.week > 0]
    return sum(
        (int(r.season), int(r.week), r.team_abbr) in gdm for r in real.itertuples()
    )


def test_receiving_ingest_and_get(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("ngs_receiving")
    n = ngs.ingest_ngs_receiving(db, df, retrieved_as_of="2023-10-20")
    expected = _resolvable(df, db)
    # Every week>0 row resolves a gameday now (LAR aliases to LA) -> all land.
    assert 0 < n == expected == len(df)

    src5 = df[(df.week == 5) & (df.team_abbr != "LAR")].iloc[0]
    got = ngs.get_ngs_receiving(
        db, as_of="2023-10-20", season=2023, week=5,
        player_gsis_id=src5["player_gsis_id"],
    )
    assert len(got) == 1
    row = got[0]
    assert row["team_abbr"] == src5["team_abbr"]
    assert row["catch_percentage"] == src5["catch_percentage"]
    # knowable_as_of is that team's week-5 gameday (2023-10-05..09), not the pull date.
    assert row["knowable_as_of"].startswith("2023-10-0")


def test_lar_alias_resolves_and_unknown_team_is_dropped(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("ngs_receiving")
    assert (df.team_abbr == "LAR").any(), "fixture should contain LAR rows"
    # Rams NGS ("LAR") resolves via the LAR->LA alias and must be stored, not
    # silently dropped (regression guard for base.TEAM_ALIASES).
    ngs.ingest_ngs_receiving(db, df, retrieved_as_of="2023-10-20")
    stored = ngs.get_ngs_receiving(db, as_of="2023-10-20", season=2023)
    assert any(r["team_abbr"] == "LAR" for r in stored)

    # A genuinely unmappable team still drops rather than storing a NULL/leaky
    # knowledge time.
    bogus = df.copy()
    bogus["team_abbr"] = "ZZZ"
    assert ngs.ingest_ngs_receiving(db, bogus, retrieved_as_of="2023-10-20") == 0


def test_receiving_leakage_gates_by_gameday(db, nfl_fixture):
    # Retrieve at 2023-10-11 (between the weeks) so the retrieval gate never masks
    # the read and the *gameday* stamp is the only thing under test. The fixture's
    # week-6 rows are deliberately present to prove they stay hidden until their
    # gamedays — physical presence must not equal readability.
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("ngs_receiving")
    ngs.ingest_ngs_receiving(db, df, retrieved_as_of="2023-10-11")

    # 2023-10-11 is after every week-5 game but before every week-6 game.
    seen = ngs.get_ngs_receiving(db, as_of="2023-10-11", season=2023)
    assert {r["week"] for r in seen} == {5}, "week 5 knowable, week 6 must be hidden"

    # By 2023-10-20 both weeks' gamedays have passed -> both knowable.
    later = {r["week"] for r in ngs.get_ngs_receiving(db, as_of="2023-10-20", season=2023)}
    assert later == {5, 6}


def test_latest_truth_sees_immutable_bulk_history(db, nfl_fixture):
    # Bulk-pulled immutable outcomes can opt into latest_truth: week 5 is
    # visible by its gameday while future week 6 remains hidden.
    _load_schedules(db, nfl_fixture)
    ngs.ingest_ngs_receiving(db, nfl_fixture("ngs_receiving"), retrieved_as_of="2026-07-16")
    seen = {
        r["week"]
        for r in ngs.get_ngs_receiving(
            db, as_of="2023-10-11", season=2023, view="latest_truth"
        )
    }
    assert seen == {5}


def test_receiving_dropped_when_schedules_absent(db, nfl_fixture):
    # Without schedules, no game date resolves -> every row dropped, none stored.
    df = nfl_fixture("ngs_receiving")
    n = ngs.ingest_ngs_receiving(db, df, retrieved_as_of="2023-10-20")
    assert n == 0
    assert ngs.get_ngs_receiving(db, as_of="2023-10-20", season=2023) == []


def test_rushing_ingest_and_count(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("ngs_rushing")
    n = ngs.ingest_ngs_rushing(db, df, retrieved_as_of="2023-10-11")
    assert 0 < n == _resolvable(df, db)
    seen = ngs.get_ngs_rushing(db, as_of="2023-10-11", season=2023)
    assert {r["week"] for r in seen} == {5}  # gameday gating holds for rushing too


def test_passing_ingest_and_count(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("ngs_passing")
    n = ngs.ingest_ngs_passing(db, df, retrieved_as_of="2023-10-11")
    assert 0 < n == _resolvable(df, db)
    seen = ngs.get_ngs_passing(db, as_of="2023-10-11", season=2023)
    assert {r["week"] for r in seen} == {5}
