"""Depth charts, LEGACY weekly regime (2021-2024) — cached-fixture + leakage.

The four item-1.4 tests, retargeted at ``depth_charts_weekly`` (the table and
module the 2021-2024 archive moved to in item 3.2c), plus the tests for the two
things 3.2c changed: the widened primary key and the regime guards.

Depth charts in this regime are forward-looking weekly data: ``knowable_as_of``
is anchored to the week's FIRST kickoff (``base.week_first_gameday_map``), so
schedules must always be ingested first.

WHAT THIS FIXTURE CAN AND CANNOT CATCH (item 3.1b's lesson).

CAN: ``tests/fixtures/nfl/depth_charts.parquet`` is a real 2023 weeks 5-6 slice,
so the leakage gate, the coalescing of nullable key members and the widened key
are all exercised against data upstream actually published.

CANNOT: this regime is FINISHED — 2024 was its last season and upstream now
serves a dated panel — so there is no live drift left for a fixture to miss.
That is also why there is no live contract test here: the file this module reads
cannot change again except by an nflverse restatement, which every row's own
``retrieved_as_of`` would make visible rather than silent. What the fixture
genuinely cannot prove is the measured collapse magnitude (835/947/899/933 rows
per season under the old key), because it is a 1,155-row slice — so the key test
below builds the collision explicitly instead of hoping the slice contains one.
"""

import pandas as pd
import pytest

from ziggurat.data.nfl import base, depth_charts_weekly, schedules

ingest = depth_charts_weekly.ingest_depth_charts_weekly
read = depth_charts_weekly.get_depth_chart_week


def test_ingest_and_query_week5_starters(db, nfl_fixture):
    # Schedules first so week_first_gameday_map resolves the knowable anchor.
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    n = ingest(db, nfl_fixture("depth_charts"), retrieved_as_of="2023-10-20")
    assert n > 0

    # Both weeks are knowable by 2023-10-20; week-5 starter rows (depth_position
    # populated) land with the right season/week.
    wk5 = read(db, as_of="2023-10-20", season=2023, week=5)
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
    ingest(db, nfl_fixture("depth_charts"), retrieved_as_of="2023-10-20")
    atl = read(db, as_of="2023-10-20", season=2023, week=5, team="ATL")
    assert len(atl) > 0
    assert {r["club_code"] for r in atl} == {"ATL"}


def test_depth_chart_leakage_hides_future_week(db, nfl_fixture):
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    # Retrieve BEFORE the leakage as_of for BOTH weeks (2023-10-10 <= 2023-10-11),
    # so retrieved_as_of is not the binding constraint — only knowable_as_of can
    # hide week 6. That isolates the leakage crux.
    ingest(db, nfl_fixture("depth_charts"), retrieved_as_of="2023-10-10")

    # Week 6's first kickoff is 2023-10-12. On 2023-10-11 week 5 is knowable and
    # the forward-looking week-6 depth chart is NOT -> it must stay hidden.
    visible = read(db, as_of="2023-10-11", season=2023)
    weeks = {r["week"] for r in visible}
    assert 5 in weeks
    assert 6 not in weeks

    # Same retrieval, a later as_of (after week 6's kickoff): week 6 now appears,
    # proving the week-6 rows were present all along and knowable_as_of — not a
    # missing row or retrieved_as_of — did the gating.
    later = read(db, as_of="2023-10-20", season=2023)
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

    ingest(db, df, retrieved_as_of="2023-10-20")
    c1 = db.execute("SELECT COUNT(*) c FROM depth_charts_weekly").fetchone()["c"]
    ingest(db, df, retrieved_as_of="2023-10-20")  # re-ingest
    c2 = db.execute("SELECT COUNT(*) c FROM depth_charts_weekly").fetchone()["c"]
    assert c1 == c2, "re-ingest must dedupe null-gsis rows, not duplicate them"

    rows = read(db, as_of="2023-10-20", season=2023, week=5, team=target["club_code"])
    assert any(r["gsis_id"] == "" and r["full_name"] == target["full_name"] for r in rows)


# ===========================================================================
# what item 3.2c changed
# ===========================================================================


def test_the_widened_key_keeps_two_rows_that_differ_only_in_depth_team(db, nfl_fixture):
    """The measured defect, reproduced deliberately.

    The item-1.4 PK omitted ``game_type`` and ``depth_team``, so INSERT OR REPLACE
    silently collapsed 835 / 947 / 899 / 933 rows per season (2021-2024) — ~700 a
    season differing ONLY in ``depth_team``, i.e. the depth ORDER, i.e. the one
    column this table exists for — while the ingester returned the full count and
    ``note_drops`` reported 0.
    """
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("depth_charts").copy().reset_index(drop=True)
    row = df[(df.week == 5) & (df.depth_team == "1")].iloc[0]
    second = row.copy()
    second["depth_team"] = "2"     # same player, same slot, a DIFFERENT depth order
    pair = pd.DataFrame([row, second])

    with base.collect_drops() as tally:
        written = ingest(db, pair, retrieved_as_of="2023-10-20")
    assert written == 2
    assert tally["collapsed"] == 0
    stored = read(db, as_of="2023-10-20", season=2023, week=5, team=row["club_code"])
    mine = [r for r in stored if r["gsis_id"] == row["gsis_id"]
            and r["position"] == row["position"]]
    assert {r["depth_team"] for r in mine} == {"1", "2"}


def test_a_byte_identical_upstream_duplicate_is_counted_but_not_a_defect(db, nfl_fixture):
    """The residual collapse under the widened key is 145/171/182/207 per season,
    ALL byte-identical duplicates. Separated by full-row equality, not by
    relabelling the class — and deliberately kept off the drop ceiling."""
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("depth_charts").copy().reset_index(drop=True)
    row = df[df.week == 5].iloc[0]
    with base.collect_drops() as tally:
        written = ingest(db, pd.DataFrame([row, row]), retrieved_as_of="2023-10-20")
    assert written == 1
    assert tally["duplicated"] == 1
    assert tally["collapsed"] == 0
    assert tally["dropped"] == 0


def test_the_accessor_key_matches_the_stored_primary_key(db):
    """If these drift, the accessor's correlated MAX resolves rows against each
    other and hides whichever carries the older ``retrieved_as_of``."""
    declared = base._primary_key_columns(db, "depth_charts_weekly")
    assert set(depth_charts_weekly._PK_COLS) == set(declared)
    assert set(depth_charts_weekly._KEY_COLS) == set(declared) - {"retrieved_as_of"}


def test_the_panel_frame_is_refused_with_a_pointer(db, nfl_fixture):
    with pytest.raises(depth_charts_weekly.PanelDepthChartFrame) as exc:
        ingest(db, nfl_fixture("depth_chart_panel"), retrieved_as_of="2026-07-25")
    assert "depth_charts" in str(exc.value)


def test_a_post_2024_season_is_refused(db, nfl_fixture):
    """2025+ is the dated panel. A 2025 row reaching here means the caller asked
    the wrong module, and the row would be stored under a schema that cannot
    express it."""
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("depth_charts").copy()
    df.loc[df.index[:1], "season"] = 2025
    with pytest.raises(depth_charts_weekly.PanelDepthChartFrame) as exc:
        ingest(db, df, retrieved_as_of="2026-07-25")
    assert "2025" in str(exc.value)

    with pytest.raises(depth_charts_weekly.PanelDepthChartFrame):
        depth_charts_weekly.pull_depth_charts_weekly(db, [2025], retrieved_as_of="2026-07-25")


def test_rows_with_no_resolvable_gameday_are_dropped_and_counted(db, nfl_fixture):
    """Never stored with a NULL knowable_as_of — that would be an ungateable
    leak. The measured population is the ``SBBYE`` null-week rows (222/232/214/234
    per season)."""
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("depth_charts").copy().reset_index(drop=True)
    df.loc[df.index[:5], "week"] = None
    with base.collect_drops() as tally:
        written = ingest(db, df, retrieved_as_of="2023-10-20")
    assert tally["dropped"] == 5
    # The real slice carries 8 byte-identical upstream duplicates of its own —
    # the same class as the 145/171/182/207 per full season, and the reason
    # ``written`` is distinct keys rather than rows offered. ZERO of them are
    # non-identical collisions, which is the property the widened key bought.
    assert tally["duplicated"] == 8
    assert tally["collapsed"] == 0
    assert written == len(df) - 5 - 8


def test_backfilled_history_is_invisible_under_historical_and_visible_under_latest_truth(
        db, nfl_fixture):
    """T2 for this regime: 2021-2024 can ONLY be loaded by a backfill, so every
    row carries ``retrieved_as_of = today`` against a fact time years old. Under
    the default view that reads EMPTY — correctly, and silently."""
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2026-07-25")
    ingest(db, nfl_fixture("depth_charts"), retrieved_as_of="2026-07-25")

    assert read(db, as_of="2023-10-20", season=2023) == []
    truth = base.latest_truth(read)
    assert len(truth(db, as_of="2023-10-20", season=2023)) > 0
    assert truth(db, as_of="2023-01-01", season=2023) == []
