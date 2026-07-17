"""Cached-fixture integration + leakage tests for depth chart ingestion.

Depth charts are forward-looking weekly data: knowable_as_of is anchored to the
week's FIRST kickoff (base.week_first_gameday_map), so schedules must always be
ingested first.
"""

from ziggurat.data.nfl import depth_charts, schedules


def test_ingest_and_query_week5_starters(db, nfl_fixture):
    # Schedules first so week_first_gameday_map resolves the knowable anchor.
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    n = depth_charts.ingest_depth_charts(
        db, nfl_fixture("depth_charts"), retrieved_as_of="2023-10-20"
    )
    assert n > 0

    # Both weeks are knowable by 2023-10-20; week-5 starter rows (depth_position
    # populated) land with the right season/week.
    wk5 = depth_charts.get_depth_chart(db, as_of="2023-10-20", season=2023, week=5)
    assert len(wk5) > 0
    assert all(r["season"] == 2023 and r["week"] == 5 for r in wk5)
    assert any(r["depth_position"] for r in wk5)  # starters/positions present

    # A known week-5 Atlanta QB resolves with its gsis_id, name, and club_code.
    ridder = [r for r in wk5 if r["gsis_id"] == "00-0038122"]
    assert ridder, "expected Desmond Ridder in ATL week-5 depth chart"
    assert ridder[0]["full_name"] == "Desmond Ridder"
    assert ridder[0]["club_code"] == "ATL"
    assert ridder[0]["formation"] == "Offense"


def test_team_filter_scopes_to_club_code(db, nfl_fixture):
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    depth_charts.ingest_depth_charts(
        db, nfl_fixture("depth_charts"), retrieved_as_of="2023-10-20"
    )
    atl = depth_charts.get_depth_chart(db, as_of="2023-10-20", season=2023, week=5, team="ATL")
    assert len(atl) > 0
    assert {r["club_code"] for r in atl} == {"ATL"}


def test_depth_chart_leakage_hides_future_week(db, nfl_fixture):
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    # Retrieve BEFORE the leakage as_of for BOTH weeks (2023-10-10 <= 2023-10-11),
    # so retrieved_as_of is not the binding constraint — only knowable_as_of can
    # hide week 6. That isolates the leakage crux.
    depth_charts.ingest_depth_charts(
        db, nfl_fixture("depth_charts"), retrieved_as_of="2023-10-10"
    )

    # Week 6's first kickoff is 2023-10-12. On 2023-10-11 week 5 is knowable and
    # the forward-looking week-6 depth chart is NOT -> it must stay hidden.
    visible = depth_charts.get_depth_chart(db, as_of="2023-10-11", season=2023)
    weeks = {r["week"] for r in visible}
    assert 5 in weeks
    assert 6 not in weeks

    # Same retrieval, a later as_of (after week 6's kickoff): week 6 now appears,
    # proving the week-6 rows were present all along and knowable_as_of — not a
    # missing row or retrieved_as_of — did the gating.
    later = depth_charts.get_depth_chart(db, as_of="2023-10-20", season=2023)
    assert {r["week"] for r in later} == {5, 6}


def test_null_gsis_rows_dedupe_on_reingest_and_stay_visible(db, nfl_fixture):
    # Real depth data has rows with no gsis_id, and gsis_id sits inside the
    # composite PK. Without the NULL->'' coalesce those rows would (a) DUPLICATE on
    # re-ingest (SQLite treats NULLs as distinct in the PK's UNIQUE index) and
    # (b) be invisible to select_as_of's `t2.k = t.k` match. This guards both.
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("depth_charts").copy().reset_index(drop=True)
    idx = df[df.week == 5].index[0]
    df.loc[idx, "gsis_id"] = None  # a week-5 player missing from the crosswalk
    target = df.loc[idx]

    depth_charts.ingest_depth_charts(db, df, retrieved_as_of="2023-10-20")
    c1 = db.execute("SELECT COUNT(*) c FROM depth_charts").fetchone()["c"]
    depth_charts.ingest_depth_charts(db, df, retrieved_as_of="2023-10-20")  # re-ingest
    c2 = db.execute("SELECT COUNT(*) c FROM depth_charts").fetchone()["c"]
    assert c1 == c2, "re-ingest must dedupe null-gsis rows, not duplicate them"

    rows = depth_charts.get_depth_chart(db, as_of="2023-10-20", season=2023, week=5, team=target["club_code"])
    assert any(r["gsis_id"] == "" and r["full_name"] == target["full_name"] for r in rows)
