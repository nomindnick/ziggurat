"""The decision-archive collector seams — item 4.2b, unit B1.

`ziggurat waivers` produces stdout and nothing else: the swap matrix, the pool
as priced, every evaluated candidate row and every reason string are discarded
at process exit, and a missed Tuesday cannot be reconstructed afterwards
(`league_player_state` accumulates forward only — item 3.1). B1 adds two PASSIVE
collectors that receive what those functions already compute, plus
`ClaimRec.drop_espn_id` so an archived claim records what it cost by IDENTITY
rather than by display name.

The load-bearing property is that they change NOTHING. Two of the tests here
exist only to pin that: the board and the plan are equal, and the rendered page
is byte-identical, with and without a collector attached. The third is the one
that makes the archive trustworthy — the collector's own flagged set is the
SHIPPED board's flagged set, identity for identity, on four weeks of the real
2025 backfill (158 / 108 / 91 / 134). A second flag rule outside the module
would be two rules that can silently diverge; there is no second rule, and this
is how we know.

Offline throughout except `test_the_freeze_flag_set_equals_the_shipped_board`,
which is skipped (loudly) without the live database — the 2023 fixture slice has
no week-1 emergence cohort and no injury-beneficiary annotation, so it cannot
stand in for it.
"""

import ast
import sqlite3
from pathlib import Path
from types import MappingProxyType

import pytest

from ziggurat.core import candidates as C
from ziggurat.core import waiver
from ziggurat.core.waiver import WaiverArtifacts, build_waiver_plan, format_waiver_plan
from ziggurat.data.nfl import base, injuries, players, schedules, snap_counts, weekly_stats

REPO_ROOT = Path(__file__).resolve().parents[1]
ZIGGURAT = REPO_ROOT / "ziggurat"
LIVE_DB = REPO_ROOT / "db" / "ziggurat.sqlite"

# Same stamps as tests/test_signals.py: the bulk backfill is retrieved in the
# FUTURE of the 2023 season, so every read of it needs latest_truth.
_BULK_RETRIEVED = "2026-07-16"

SEASON = 2026
PULL = "2026-09-15"
WEEKS = range(3, 18)
TEAM = 10


# --------------------------------------------------------------- fixture I/O


def _seed(db, nfl_fixture):
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"),
                                     retrieved_as_of=_BULK_RETRIEVED)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"),
                                   retrieved_as_of=_BULK_RETRIEVED)
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of=_BULK_RETRIEVED)


def _read(db, **kw):
    """The past-season validation path: bind the WHOLE generator to latest_truth."""
    return base.latest_truth(C.build_candidates)(db, **kw)


def _board_usage_keys(board):
    """The USAGE_BREAKOUT block's identities, in the collector's key space."""
    return frozenset(r.gsis_id or f"?:{r.team}" for r in board.by_kind(C.SIGNAL_USAGE))


# ============================================================ the usage collector


def test_a_collector_changes_no_candidate_board(db, nfl_fixture):
    """The pin the whole unit rests on: attaching a collector is invisible.

    Board equality AND rendered-text equality, because a dataclass can compare
    equal while the render differs (ordering inside a tuple field would not).
    """
    _seed(db, nfl_fixture)
    plain = _read(db, as_of="2023-10-17", season=2023, week=6)
    collect = C.EvaluatedRows()
    with_collector = _read(db, as_of="2023-10-17", season=2023, week=6, collect=collect)

    assert with_collector == plain
    assert C.format_candidates(with_collector, reasons=True) == \
        C.format_candidates(plain, reasons=True)
    assert collect.rows, "the collector must actually have been filled"


def test_every_evaluated_row_is_emitted_flagged_or_not(db, nfl_fixture):
    """False NEGATIVES are the point: the board keeps survivors, the collector
    keeps everyone the floors looked at, and the flagged subset is EXACTLY the
    board's USAGE block — matched on identity, not counted."""
    _seed(db, nfl_fixture)
    collect = C.EvaluatedRows()
    board = _read(db, as_of="2023-10-17", season=2023, week=6, collect=collect)

    assert collect.ran is True
    passed_over = [r for r in collect.rows if not r.flagged]
    assert passed_over, "the fixture week must contain rows the floors passed over"
    assert len(collect.rows) > len(collect.flagged)
    assert collect.flagged_keys() == _board_usage_keys(board)
    assert len(collect.flagged) == len(board.by_kind(C.SIGNAL_USAGE))
    # every row carries the week it was evaluated in and the two ids
    assert {r.week for r in collect.rows} == {6}
    assert all(r.position in C.USAGE_POSITIONS for r in collect.rows)


def test_a_passed_over_row_carries_its_evidence_and_no_prose(db, nfl_fixture):
    """A non-flagged row is a MEASUREMENT, not an empty slot: it keeps its
    deltas, its raw levels and its snap-share resolution, clears no floor, and
    carries no reasons (the module writes no prose for a player it passed over —
    inventing one here would be a second generator)."""
    _seed(db, nfl_fixture)
    collect = C.EvaluatedRows()
    _read(db, as_of="2023-10-17", season=2023, week=6, collect=collect)

    row = next(r for r in collect.rows if not r.flagged)
    assert row.floors_cleared == {}
    assert row.magnitude == 0.0
    assert row.reasons == ()
    assert set(row.levels) == set(C.EVALUATED_LEVELS)
    assert "d_offense_pct" in row.deltas and "d_targets" in row.deltas
    assert row.snap_resolved is (row.snap_pct is not None)
    assert row.path in (C.PATH_DIFFERENCED, C.PATH_EMERGENCE)


def test_the_two_paths_are_labelled_and_never_share_a_floor_map(db, nfl_fixture):
    """`differenced` floors are compared against DELTAS and `emergence` floors
    against ABSOLUTE levels (item 3.3 F1) — a reader of the archive who mixes
    them up reads a role change as a usage jump. The path says which."""
    _seed(db, nfl_fixture)
    collect = C.EvaluatedRows()
    _read(db, as_of="2023-10-17", season=2023, week=6, collect=collect)

    for row in collect.rows:
        if row.path == C.PATH_DIFFERENCED:
            assert row.prior_week is not None
            assert set(row.floors_cleared) <= set(C.DEFAULT_BREAKOUT.floors)
        else:
            assert row.prior_week is None
            assert set(row.floors_cleared) <= set(C.EMERGENCE_FLOORS)


def test_the_floors_in_force_travel_with_the_rows(db, nfl_fixture):
    """Rule 6: an archived board read against the wrong hypothesis is worse than
    no archive. A tuned run's floors and its OWN provenance label are recorded —
    never the shipped hypothesis's."""
    _seed(db, nfl_fixture)
    tuned = C.BreakoutThresholds(
        floors=MappingProxyType({**dict(C.DEFAULT_BREAKOUT.floors), "carries": 1.0}),
        label="hypothesis: a tuning setting under evaluation",
        source="tests/test_collector_seams.py",
    )
    emergence = MappingProxyType({**dict(C.EMERGENCE_FLOORS), "carries": 1.0})
    collect = C.EvaluatedRows()
    _read(db, as_of="2023-10-17", season=2023, week=6, collect=collect,
          thresholds=tuned, emergence_floors=emergence)

    assert collect.floors["carries"] == 1.0
    assert collect.floors_label == tuned.label and collect.floors_source == tuned.source
    assert collect.emergence_floors["carries"] == 1.0
    assert "NON-DEFAULT" in collect.emergence_label
    assert collect.season == 2023 and collect.week == 6
    assert collect.view == "latest_truth" and collect.as_of == "2023-10-17"


def test_an_empty_collector_says_whether_the_arm_ever_ran(db, nfl_fixture):
    """`rows == []` means two different things — "it evaluated nobody" and "it
    never got that far" (a pre-season NoCompletedWeek). An archive that cannot
    tell them apart records an absence as a measurement."""
    _seed(db, nfl_fixture)
    fresh = C.EvaluatedRows()
    assert fresh.ran is False and fresh.rows == []

    collect = C.EvaluatedRows()
    with pytest.raises(C.NoCompletedWeek):
        # a pre-season as_of: the week never resolves, so the arm never runs
        _read(db, as_of="2023-08-01", season=2023, collect=collect)
    assert collect.ran is False and collect.rows == []


def test_the_record_is_flat_and_deterministic(db, nfl_fixture):
    """`as_record()` is what a writer serialises: one flat dict per row, the same
    keys in the same order every time, with the metric names intact."""
    _seed(db, nfl_fixture)
    collect = C.EvaluatedRows()
    _read(db, as_of="2023-10-17", season=2023, week=6, collect=collect)

    record = collect.rows[0].as_record()
    assert list(record) == list(collect.rows[0].as_record())      # stable order
    for name in ("season", "week", "prior_week", "as_of", "view", "gsis_id",
                 "espn_id", "player", "position", "team", "snap_pct",
                 "snap_resolved", "path", "floors_cleared", "magnitude",
                 "flagged", "reasons"):
        assert name in record
    for metric in C.EVALUATED_LEVELS:
        assert metric in record and f"d_{metric}" in record
    assert "d_offense_snaps" in record and "d_offense_pct" in record
    assert isinstance(record["reasons"], list)
    # Rule 2: nothing here is a points column, and no floor is stored as one.
    assert not any("points" in k for k in record)


# ================================================== the shipped-board equality


@pytest.mark.skipif(
    not LIVE_DB.exists(),
    reason=(
        "live db/ziggurat.sqlite absent — SKIPPING THIS TEST REMOVES THE ONLY "
        "CHECK THAT THE ARCHIVED FLAG SET IS THE SHIPPED ONE on real data: the "
        "2023 fixture slice has no week-1 emergence cohort (2025 wk1 is 321 of "
        "321 emergence rows) and no injury-beneficiary annotation, so the "
        "offline tests above cannot stand in for it."
    ),
)
@pytest.mark.parametrize(("week", "as_of", "flagged", "evaluated"), [
    (1, "2025-09-09", 158, 321),
    (5, "2025-10-07", 108, 284),
    (9, "2025-11-04", 91, 282),
    (18, "2026-01-06", 134, 307),
])
def test_the_freeze_flag_set_equals_the_shipped_board(week, as_of, flagged, evaluated):
    """Item 4.2b §2.2's test: on the real 2025 backfill the collector's flagged
    rows ARE the board's USAGE_BREAKOUT rows — the identities, not the counts.

    The counts are pinned too, and they are frozen facts about a frozen backfill:
    if one moves, either the generator changed or the stored history did, and
    both are findings. Read under `latest_truth`, which is what a past-season
    read needs (bulk history is retrieved_as_of=today and reads EMPTY under the
    default historical view).
    """
    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        collect = C.EvaluatedRows()
        board = base.latest_truth(C.build_candidates)(
            conn, as_of=as_of, season=2025, week=week, collect=collect)
    finally:
        conn.close()

    assert len(board.by_kind(C.SIGNAL_USAGE)) == flagged
    assert len(collect.flagged) == flagged
    assert collect.flagged_keys() == _board_usage_keys(board)
    assert len(collect.rows) == evaluated
    assert len(collect.rows) > len(collect.flagged), "false negatives must be kept"


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live db/ziggurat.sqlite not present")
def test_the_collected_reasons_are_the_post_injury_pass_ones():
    """The injury arm rewrites a beneficiary's usage reasons IN PLACE, so a
    snapshot taken inside `_usage_arm` is missing that line — the sentence that
    says WHY the usage moved. 2025 wk9 has 23 such rows; a collector filled
    before the pass would have none.
    """
    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        collect = C.EvaluatedRows()
        board = base.latest_truth(C.build_candidates)(
            conn, as_of="2025-11-04", season=2025, week=9, collect=collect)
    finally:
        conn.close()

    annotated = [r for r in collect.flagged
                 if any("vacancy opened this week" in x for x in r.reasons)]
    assert len(annotated) >= 1
    # and every flagged row's reasons are the board's, verbatim (Rule 6)
    by_key = {r.gsis_id or f"?:{r.team}": r.reasons for r in board.by_kind(C.SIGNAL_USAGE)}
    for row in collect.flagged:
        assert row.reasons == by_key[row.gsis_id or f"?:{row.team}"]


# ============================================================ the waiver seam


def _active_specs():
    """16 non-IR bodies on team 10 — the shape tests/test_waiver.py uses."""
    return [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": TEAM},
        {"name": "Backup Passer", "pos": "QB", "team": "TEN", "pts": 8.0, "bye": 6, "on_team": TEAM},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": TEAM},
        {"name": "Second Runner", "pos": "RB", "team": "ATL", "pts": 5.0, "bye": 11, "on_team": TEAM},
        {"name": "Third Runner", "pos": "RB", "team": "BUF", "pts": 12.0, "bye": 7, "on_team": TEAM},
        {"name": "Depth Runner", "pos": "RB", "team": "CHI", "pts": 3.0, "bye": 9, "on_team": TEAM},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": TEAM},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": TEAM},
        {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 11.0, "bye": 10, "on_team": TEAM},
        {"name": "Fourth Catcher", "pos": "WR", "team": "HOU", "pts": 4.0, "bye": 12, "on_team": TEAM},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": TEAM},
        {"name": "Tight Two", "pos": "TE", "team": "JAX", "pts": 3.0, "bye": 5, "on_team": TEAM},
        {"name": "Kick Er", "pos": "K", "team": "KC", "pts": 8.0, "bye": 14, "on_team": TEAM},
        {"name": "Miami D/ST", "pos": "D/ST", "team": "MIA", "pts": 2.0, "bye": 5, "on_team": TEAM,
         "weeks": {w: 20.0 for w in range(1, 18) if w % 2 == 1}},
        {"name": "Fifth Catcher", "pos": "WR", "team": "NO", "pts": 6.0, "bye": 7, "on_team": TEAM},
        {"name": "Sixth Catcher", "pos": "WR", "team": "SEA", "pts": 7.0, "bye": 8, "on_team": TEAM},
    ]


_POOL_SPECS = [
    {"name": "Free Passer", "pos": "QB", "team": "NE", "pts": 12.0, "bye": 9},
    {"name": "Free Runner", "pos": "RB", "team": "NYG", "pts": 20.0, "bye": 7},
    {"name": "Waiver Catcher", "pos": "WR", "team": "NYJ", "pts": 19.0, "bye": 11,
     "status": "WAIVERS"},
    {"name": "Waiver Wideout", "pos": "WR", "team": "PIT", "pts": 16.0, "bye": 10,
     "status": "WAIVERS"},
    {"name": "Free Tight", "pos": "TE", "team": "LV", "pts": 9.0, "bye": 8},
]


def _world(marginal_world, injury="OUT"):
    marginal_world(_active_specs()
                   + [{"name": "IR Guy", "pos": "WR", "team": "MIN", "pts": 14.0,
                       "bye": 6, "on_team": TEAM, "slot": "IR", "injury": injury}]
                   + _POOL_SPECS, retrieved=PULL)


def _plan(db, **kwargs):
    kwargs.setdefault("weeks", WEEKS)
    kwargs.setdefault("pool_limit", None)
    return build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=TEAM, **kwargs)


def test_the_waiver_collector_changes_no_plan_and_no_page(db, marginal_world):
    """The other half of the byte-identity pin, on the legal path: same plan,
    same rendered page, collector or not."""
    _world(marginal_world)
    plain = _plan(db)
    collect = WaiverArtifacts()
    with_collector = _plan(db, collect=collect)

    assert with_collector == plain
    assert format_waiver_plan(with_collector, reasons=True) == \
        format_waiver_plan(plain, reasons=True)
    assert format_waiver_plan(with_collector) == format_waiver_plan(plain)


def test_the_collector_holds_what_the_run_computed(db, marginal_world):
    """It receives; it does not re-read. Everything here was already in memory
    when the plan was built."""
    _world(marginal_world)
    collect = WaiverArtifacts()
    plan = _plan(db, collect=collect)

    assert collect.plan is plan
    assert collect.as_of == PULL and collect.season == SEASON
    assert collect.team_id == TEAM and collect.view == "historical"
    assert collect.claim_budget == 3 and collect.source == "sleeper_rotowire"
    assert collect.weeks_requested == tuple(WEEKS)
    assert len(collect.roster_rows) == 17          # RAW: the IR row is included
    assert any(r["lineup_slot"] == "IR" for r in collect.roster_rows)
    assert len(collect.pool_rows) == len(_POOL_SPECS)
    assert collect.board is not None
    assert collect.swaps and tuple(collect.swaps) == tuple(collect.board.swaps)
    assert collect.open_slots == 0
    assert collect.league_settings is None         # no settings row in this world


def test_the_collector_records_which_pull_priced_the_page(db, marginal_world):
    """An `as_of` records the GATE, not the DATA: a July projection pull and a
    November one both carry a valid knowable_as_of and are Rule-1-invisible from
    the page. The vintages are the only thing that tells them apart."""
    _world(marginal_world)
    collect = WaiverArtifacts()
    _plan(db, collect=collect)

    assert set(collect.vintages) == set(waiver.ARCHIVE_VINTAGE_TABLES)
    assert collect.vintages["projections"] == PULL
    assert collect.vintages["league_player_state"] == PULL
    # nothing seeded these, and "no pull is visible" is a fact, not a blank
    assert collect.vintages["weekly_stats"] is None
    assert collect.crosswalk_vintage == PULL       # `players`, read at-now by design


def test_an_absent_candidate_board_is_distinguishable_from_an_empty_one(db, marginal_world):
    """Pre-Week-1 the generator raises NoCompletedWeek and there is no board at
    all — which must not read as "the scan found nothing" (hazard 20: the
    candidate half of the freeze produces nothing before the first live
    Tuesday)."""
    _world(marginal_world)
    collect = WaiverArtifacts()
    _plan(db, collect=collect)

    assert collect.candidates is None
    assert collect.candidate_error is None         # skipped silently, not degraded
    assert collect.evaluated is not None and collect.evaluated.ran is False
    assert collect.candidate_notes == {}


def test_the_blocked_path_never_resolves_the_lazy_swap_matrix(db, marginal_world):
    """A refusal must not cost more than the refusal. `MarginalBoard.swaps` is
    lazy and re-pricing it costs more than the whole rest of the scan; the
    blocked path plans no claims, so the collector takes the board and stops."""
    _world(marginal_world, injury="QUESTIONABLE")   # 17 of 16 -> illegal
    collect = WaiverArtifacts()
    plan = _plan(db, collect=collect)

    assert plan.blocked is True
    assert collect.plan is plan
    assert collect.swaps == ()
    assert collect.board is not None
    # the memo inside _SwapMatrix: None means resolve() was never called
    assert collect.board._swaps._resolved is None
    assert len(collect.roster_rows) == 17
    assert collect.vintages["projections"] == PULL   # provenance still recorded


# ============================================================== drop_espn_id


def test_a_claim_records_the_drop_by_identity_not_by_name(db, marginal_world):
    """Two free agents can share a display name — the item-3.4 audit already
    fixed that mis-join for the ADD side and left the DROP side on the name.
    `SwapRow` has carried the id all along; `ClaimRec` now does too."""
    _world(marginal_world)
    collect = WaiverArtifacts()
    plan = _plan(db, collect=collect)

    recs = [c for c in list(plan.claims) + list(plan.fcfs_grabs) if c.drop is not None]
    assert recs, "the synthetic world must produce at least one add/drop pair"
    ids = {str(r["player"]): str(r["espn_player_id"]) for r in collect.roster_rows}
    for rec in recs:
        assert rec.drop_espn_id == ids[rec.drop]
        # and it joins back to the swap the claim was built from
        assert any(s.drop_espn_id == rec.drop_espn_id and s.drop == rec.drop
                   for s in collect.swaps)


def test_a_pure_add_has_no_drop_and_says_so_on_both_fields(db, marginal_world):
    """An open active slot means there IS no drop — the id must be absent, not
    quoted from the swap the add was found through (the same defect the 3.4b
    audit fixed for the gain)."""
    specs = _active_specs()[:-1] + _POOL_SPECS      # 15 bodies -> one open slot
    marginal_world(specs, retrieved=PULL)
    collect = WaiverArtifacts()
    plan = _plan(db, collect=collect)

    pure = [c for c in list(plan.claims) + list(plan.fcfs_grabs) if c.drop is None]
    assert pure, "a 15-man roster must produce at least one pure add"
    for rec in pure:
        assert rec.drop_position is None and rec.drop_espn_id is None


# ================================================ base.resolved_vintage (Rule 1)


def test_resolved_vintage_requires_keyword_as_of(db):
    with pytest.raises(TypeError):
        base.resolved_vintage(db, "projections", "2026-09-15")   # as_of positional


def _projection_row(db, player, *, knowable, retrieved, season=SEASON):
    db.execute(
        "INSERT INTO projections (source, source_player_id, season, week, "
        "season_type, position, team, retrieved_as_of, knowable_as_of) "
        "VALUES ('sleeper_rotowire', ?, ?, 1, 'regular', 'RB', 'NYG', ?, ?)",
        (player, season, retrieved, knowable))
    db.commit()


def test_resolved_vintage_leakage_both_views(db):
    """The leakage pair, exercised on BOTH views and on BOTH gates.

    `historical` gates retrieval time as well as knowledge time, so a pull that
    had not happened yet is invisible — which is exactly why a bulk-loaded past
    season reads EMPTY under it. `latest_truth` drops the retrieval gate and
    keeps the fact gate: it sees the later correction, never a future fact.
    """
    _projection_row(db, "A", knowable="2026-08-01", retrieved="2026-08-01")
    _projection_row(db, "B", knowable="2026-08-01", retrieved="2026-09-15")  # a later pull
    _projection_row(db, "C", knowable="2026-12-01", retrieved="2026-08-01")  # not yet true

    # (a) the RETRIEVAL gate: the later pull is hidden under historical...
    assert base.resolved_vintage(db, "projections", as_of="2026-09-01",
                                 season=SEASON) == "2026-08-01"
    # ...and visible under the bulk-history view
    assert base.resolved_vintage(db, "projections", as_of="2026-09-01", season=SEASON,
                                 view="latest_truth") == "2026-09-15"
    # (b) the FACT gate binds under BOTH views: row C is retrieved long ago and
    # still invisible, because it is not knowable until December.
    assert base.resolved_vintage(db, "projections", as_of="2026-07-01",
                                 season=SEASON) is None
    assert base.resolved_vintage(db, "projections", as_of="2026-07-01", season=SEASON,
                                 view="latest_truth") is None
    # (c) after every pull the two views agree
    assert base.resolved_vintage(db, "projections", as_of="2026-09-15",
                                 season=SEASON) == "2026-09-15"
    # (d) a season with no rows is None, not the other season's vintage
    assert base.resolved_vintage(db, "projections", as_of="2026-09-15",
                                 season=2021) is None
    # (e) an unknown view is refused rather than silently honoured
    with pytest.raises(ValueError):
        base.resolved_vintage(db, "projections", as_of="2026-09-15", view="whatever")


def test_resolved_vintage_reports_the_newest_pull_a_key_could_resolve(db, marginal_world):
    """It is a SUMMARY and says so: `select_as_of` resolves MAX(retrieved_as_of)
    PER KEY, so a table can serve one player from today's pull and another from
    one three weeks old. This is the upper bound — the number that moves when a
    new pull lands."""
    marginal_world(_active_specs(), retrieved="2026-09-15")
    marginal_world([{"name": "Late Runner", "pos": "RB", "team": "NYG", "pts": 9.0,
                     "bye": 7}], retrieved="2026-09-16")

    assert base.resolved_vintage(db, "projections", as_of="2026-09-16",
                                 season=SEASON) == "2026-09-16"
    # the older rows are still what THEY resolve to; the summary is the newest
    older = db.execute(
        "SELECT COUNT(*) FROM projections WHERE retrieved_as_of = '2026-09-15'"
    ).fetchone()[0]
    assert older > 0


def test_resolved_vintage_refuses_a_non_identifier_table(db):
    """Code-authored SQL, validated as such rather than trusted — the table name
    is interpolated, so it is checked."""
    with pytest.raises(ValueError):
        base.resolved_vintage(db, "projections; DROP TABLE players", as_of="2026-09-15")
    with pytest.raises(ValueError):
        base.resolved_vintage(db, "projections", as_of="2026-09-15", season=1,
                              season_col="season = 1 OR 1")


# ===================================================== Rule 8 / import direction

# What `ziggurat/core/` is allowed to reach at import time. `decisions` (item
# 4.2b's writer package) is deliberately absent: it imports core, and the arrow
# only points one way. `backtest/` is absent for the same reason — the dependency
# runs backtest -> ziggurat — and `draft/` is the standing quarantine (Rule 8).
_CORE_MAY_IMPORT = {"core", "data", "league"}


def _ziggurat_imports(path: Path) -> set[str]:
    """The `ziggurat.<subpackage>` names a module reaches, at import time or
    lazily inside a function — both are runtime coupling for this question."""
    reached: set[str] = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "ziggurat" and len(parts) > 1:
                    reached.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            if parts[0] == "ziggurat":
                if len(parts) > 1:
                    reached.add(parts[1])
                else:
                    reached.update(a.name for a in node.names)
    return reached


def test_core_gained_no_new_import_edges():
    """B1 adds seams INSIDE core (a dataclass and a parameter), and that is the
    whole point of a passive collector: the archive depends on core, never the
    reverse. If this fails, an inversion has been introduced — fix the direction,
    do not widen the allowlist."""
    offenders = {}
    for py in sorted((ZIGGURAT / "core").glob("*.py")):
        reached = _ziggurat_imports(py) - _CORE_MAY_IMPORT
        if reached:
            offenders[py.name] = sorted(reached)
    assert offenders == {}, (
        f"ziggurat/core/ reached outside {sorted(_CORE_MAY_IMPORT)}: {offenders}")


def test_nothing_in_the_package_imports_backtest():
    """`backtest/` imports `ziggurat/` (item 4.1). The reverse would be a cycle,
    and the 4.2b freeze writer must never be confused with the 4.1 one."""
    offenders = [
        str(py.relative_to(ZIGGURAT)) for py in ZIGGURAT.rglob("*.py")
        if any(isinstance(n, (ast.Import, ast.ImportFrom))
               and (getattr(n, "module", "") or "").split(".")[0] == "backtest"
               for n in ast.walk(ast.parse(py.read_text(), filename=str(py))))
    ]
    assert offenders == []


def test_the_import_scanner_would_catch_a_real_inversion(tmp_path):
    """Guard the guard: both the import-time and the lazy shape are caught."""
    top = tmp_path / "top.py"
    top.write_text("from ziggurat.decisions.capture import freeze_run\n")
    assert _ziggurat_imports(top) == {"decisions"}

    lazy = tmp_path / "lazy.py"
    lazy.write_text("def f():\n    import ziggurat.draft.session\n")
    assert _ziggurat_imports(lazy) == {"draft"}

    clean = tmp_path / "clean.py"
    clean.write_text("from ziggurat.core.marginal import build_board\n")
    assert _ziggurat_imports(clean) == {"core"}
