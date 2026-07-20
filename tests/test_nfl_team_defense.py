"""Cached-fixture integration + leakage tests for D/ST team-defense derivation.

The D/ST line is derived from two frames (a team_stats grid + a schedules score
frame) with an opponent self-join, so these tests build small hand-authored
frames (the real 2023 wk1 DET@KC numbers, captured and trimmed) rather than
patching a network seam. Schedules are ingested first so ``base.game_date_map``
resolves the team gameday that stamps ``knowable_as_of``.

Ground truth (2023 wk1, game_id 2023_01_DET_KC, DET 21 @ KC 20, played
2023-09-07); verified live against ziggurat.core.scoring: KC D/ST = 2.0,
DET D/ST = 8.0.
"""

import logging

import pandas as pd
import pytest

from ziggurat.core.scoring import score_dst
from ziggurat.data.nfl import base, schedules, team_defense

# --- hand-authored fixtures (real 2023 wk1 DET@KC numbers) -------------------

_GAME_ID = "2023_01_DET_KC"
_GAMEDAY = "2023-09-07"

# team_stats columns the derivation reads.
_TEAM_STATS_COLS = list(team_defense._TEAM_STATS_COLUMNS)


def _team_stats_row(team, opp, **overrides):
    """A team_stats row with all required columns zeroed, then overridden."""
    row = {c: 0 for c in _TEAM_STATS_COLS}
    row.update(
        season=2023, week=1, team=team, season_type="REG",
        game_id=_GAME_ID, opponent_team=opp,
    )
    row.update(overrides)
    return row


def _team_stats_frame():
    """The DET + KC 2023 wk1 pair, real box-score counters."""
    det = _team_stats_row(
        "DET", "KC",
        def_sacks=0, def_interceptions=1, fumble_recovery_opp=0, def_safeties=0,
        def_tds=1, fumble_recovery_tds=0, special_teams_tds=0,
        fg_blocked=0, pat_blocked=0, pt_blocked=0,
        passing_yards=253, rushing_yards=118, sack_yards_lost=-3,
    )
    kc = _team_stats_row(
        "KC", "DET",
        def_sacks=1, def_interceptions=0, fumble_recovery_opp=1, def_safeties=0,
        def_tds=0, fumble_recovery_tds=0, special_teams_tds=0,
        fg_blocked=0, pat_blocked=0, pt_blocked=0,
        passing_yards=226, rushing_yards=90, sack_yards_lost=0,
    )
    return pd.DataFrame([det, kc])


# schedules frame — full column set so schedules.ingest_schedules accepts it
# (for game_date_map) AND the extra score columns team_defense reads.
_SCHEDULE_COLS = list(schedules._COLUMNS) + ["home_score", "away_score"]


def _schedules_frame():
    row = {c: None for c in _SCHEDULE_COLS}
    row.update(
        game_id=_GAME_ID, season=2023, week=1, game_type="REG",
        gameday=_GAMEDAY, weekday="Thursday", gametime="20:20",
        away_team="DET", home_team="KC", location="Home",
        roof="outdoors", surface="grass", stadium_id="KAN00",
        stadium="GEHA Field at Arrowhead Stadium",
        away_rest=7, home_rest=7, div_game=0,
        home_score=20, away_score=21,
    )
    return pd.DataFrame([row])


def _load_schedules(db, sched=None):
    schedules.ingest_schedules(
        db, sched if sched is not None else _schedules_frame(),
        retrieved_as_of="2023-08-01",
    )


def _rows_by_team(db, as_of, **kw):
    return {r["team"]: r for r in team_defense.get_team_defense(db, as_of=as_of, **kw)}


# --- (2) cached-fixture: exact derivation numbers ---------------------------

def test_derivation_exact_numbers(db):
    _load_schedules(db)
    n = team_defense.ingest_team_defense(
        db, _team_stats_frame(), _schedules_frame(), retrieved_as_of="2023-09-07"
    )
    assert n == 2  # both defenses resolve

    rows = _rows_by_team(db, "2023-09-07", season=2023, week=1)
    kc, det = rows["KC"], rows["DET"]

    assert kc["sacks"] == 1
    assert kc["def_interceptions"] == 0
    assert kc["fumble_recoveries"] == 1
    assert kc["def_tds"] == 0
    assert kc["blocked_kicks"] == 0
    assert kc["points_allowed"] == 21  # DET final score
    assert kc["yards_allowed"] == 368  # 253 + 118 - 3

    assert det["def_interceptions"] == 1
    assert det["def_tds"] == 1
    assert det["points_allowed"] == 20  # KC final score
    assert det["yards_allowed"] == 316  # 226 + 90 + 0
    # knowable_as_of is the team's gameday, not the pull date.
    assert det["knowable_as_of"] == _GAMEDAY


# --- (3) scoring round-trip -------------------------------------------------

def test_score_dst_round_trip(db):
    _load_schedules(db)
    team_defense.ingest_team_defense(
        db, _team_stats_frame(), _schedules_frame(), retrieved_as_of="2023-09-07"
    )
    rows = _rows_by_team(db, "2023-09-07", season=2023, week=1)
    assert score_dst(dict(rows["KC"])) == 2.0
    assert score_dst(dict(rows["DET"])) == 8.0


# --- (1) leakage + revision -------------------------------------------------

def test_leakage_and_revision(db):
    _load_schedules(db)
    # Pull on the gameday so retrieved_as_of <= as_of and the KNOWLEDGE gate binds.
    team_defense.ingest_team_defense(
        db, _team_stats_frame(), _schedules_frame(), retrieved_as_of=_GAMEDAY
    )

    # A day before kickoff: the line is not yet knowable.
    assert team_defense.get_team_defense(db, as_of="2023-09-06", season=2023) == []
    # On the gameday: visible.
    assert _rows_by_team(db, _GAMEDAY, season=2023, week=1)["KC"]["sacks"] == 1

    # A later-retrieved correction (KC sacks revised) with the SAME PK.
    corrected = _team_stats_frame()
    corrected.loc[corrected.team == "KC", "def_sacks"] = 99
    team_defense.ingest_team_defense(
        db, corrected, _schedules_frame(), retrieved_as_of="2023-09-14"
    )

    # historical at a retrieval time between the two pulls sees the ORIGINAL.
    hist = _rows_by_team(db, "2023-09-10", season=2023, week=1)
    assert hist["KC"]["sacks"] == 1

    # latest_truth surfaces the correction (relaxes only the retrieval gate).
    read = base.latest_truth(team_defense.get_team_defense)
    lt = {r["team"]: r for r in read(db, as_of="2023-09-10", season=2023, week=1)}
    assert lt["KC"]["sacks"] == 99


# --- (4) join-guard drop ----------------------------------------------------

def test_join_guard_drops_unresolved_rows(db, caplog):
    _load_schedules(db)
    # A stray team whose game_id has no schedules score row AND no opponent row.
    stray = _team_stats_row("SEA", "SF", game_id="2023_01_SF_SEA", def_sacks=3)
    frame = pd.concat([_team_stats_frame(), pd.DataFrame([stray])], ignore_index=True)

    with caplog.at_level(logging.WARNING, logger="ziggurat.data.nfl"):
        n = team_defense.ingest_team_defense(
            db, frame, _schedules_frame(), retrieved_as_of="2023-09-07"
        )
    assert n == 2  # only DET + KC persist; the stray is dropped, never NULL-inserted
    assert any("dropped" in rec.message for rec in caplog.records)

    # The dropped defense is absent from the store.
    assert team_defense.get_team_defense(
        db, as_of="2023-09-07", season=2023, team="SEA"
    ) == []


def test_missing_schedule_score_drops_row(db):
    # Opponent self-join resolves, but the schedules score frame lacks the game.
    _load_schedules(db)
    empty_scores = _schedules_frame().iloc[0:0]
    n = team_defense.ingest_team_defense(
        db, _team_stats_frame(), empty_scores, retrieved_as_of="2023-09-07"
    )
    assert n == 0  # no scores -> both rows dropped, never NULL points_allowed


# --- (5) schema drift -------------------------------------------------------

def test_drift_missing_def_sacks_raises(db):
    _load_schedules(db)
    bad = _team_stats_frame().drop(columns=["def_sacks"])
    with pytest.raises(ValueError, match="def_sacks"):
        team_defense.ingest_team_defense(
            db, bad, _schedules_frame(), retrieved_as_of="2023-09-07"
        )
