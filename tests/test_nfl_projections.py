"""Cached-fixture + leakage tests for Sleeper projections ingestion (item 1.5).

Offline: the ``source.import_sleeper_projections`` network seam is never called
(tests feed a hand-authored Sleeper-shaped JSON fixture directly). The fixture
carries a QB, a K, a DEF (each with hand-computable scoring lines) plus an FB
and a CB to prove non-scoring positions are filtered out.
"""

import json
from pathlib import Path

import pytest

from ziggurat.core import scoring
from ziggurat.data.nfl import projections

_FIXTURE = Path(__file__).parent / "fixtures" / "nfl" / "sleeper_projections_sample.json"


def _raw_rows():
    return json.loads(_FIXTURE.read_text())


def _by_id(rows, source_player_id):
    return next(r for r in rows if r["player_id"] == source_player_id)


def _stub_player(db, *, sleeper_id, gsis_id, retrieved="2023-09-01"):
    db.execute(
        "INSERT INTO players (gsis_id, sleeper_id, retrieved_as_of, knowable_as_of) "
        "VALUES (?, ?, ?, ?)",
        (gsis_id, sleeper_id, retrieved, retrieved),
    )
    db.commit()


def _week1_schedule(db):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, gameday, home_team, away_team, "
        "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2023_01_KC_DET", 2023, 1, "2023-09-07", "KC", "DET", "2023-08-01", "2023-08-01"),
    )
    db.commit()


# --------------------------------------------------------------- mapper / validator


def test_map_produces_only_canonical_scoring_keys():
    rows = _raw_rows()
    qb = projections.map_sleeper_projection(_by_id(rows, "3163"))
    assert qb == {
        "passing_yards": 300,
        "passing_tds": 2,
        "interceptions": 1,
        "rushing_yards": 20,
        "rushing_tds": 1,
        "passing_2pt_conversions": 1,
        "fumbles_lost": 1,
    }
    # pts_ppr is NOT mapped into the scoring dict (it is the projected_points
    # cross-check, never a scoring input).
    assert "projected_points" not in qb and "pts_ppr" not in qb


def test_map_kicker_buckets_and_derived_missed():
    k = projections.map_sleeper_projection(_by_id(_raw_rows(), "4227"))
    assert k["fg_made_0_39"] == 2.0        # fgm_20_29 + fgm_30_39
    assert k["fg_made_40_49"] == 1
    assert k["fg_made_50_59"] == 1         # fgm_50p (lossy: absorbs 60+)
    assert "fg_made_60" not in k           # source cannot fill
    assert k["pat_made"] == 3
    assert k["fg_missed"] == 1.0           # fga - fgm = 5 - 4


def test_map_kicker_includes_sub_20_yard_makes():
    """fgm_0_19 (sub-20-yd makes) must fold into the 0–39 bucket — dropping it
    would silently score a projected short FG at 0 instead of +3."""
    row = {
        "player_id": "K19", "position": "K", "team": "BUF",
        "stats": {"fgm_0_19": 0.5, "fgm_20_29": 1.0, "fgm_30_39": 2.0, "fga": 4.0, "fgm": 3.5},
    }
    mapped = projections.map_sleeper_projection(row)
    assert mapped["fg_made_0_39"] == 3.5  # 0.5 + 1.0 + 2.0, not 3.0


def test_map_dst_def_tds_no_double_count():
    d = projections.map_sleeper_projection(_by_id(_raw_rows(), "DET"))
    # def_td (1) + st_td (1) only — def_pr_td / pass_int_td / pr_td are decoys
    # that must NOT be added (they are already subsumed).
    assert d["def_tds"] == 2.0
    assert d["sacks"] == 3
    assert d["def_interceptions"] == 1
    assert d["fumble_recoveries"] == 1
    assert d["blocked_kicks"] == 1
    assert d["safeties"] == 0             # present-with-0 kept (real value)
    assert d["points_allowed"] == 17
    assert d["yards_allowed"] == 250


def test_non_scoring_positions_are_filtered():
    rows = _raw_rows()
    assert projections.map_sleeper_projection(_by_id(rows, "8025")) is None  # FB
    assert projections.map_sleeper_projection(_by_id(rows, "1696")) is None  # CB


def test_validator_passes_mapped_and_raises_on_unknown():
    mapped = projections.map_sleeper_projection(_by_id(_raw_rows(), "3163"))
    projections.validate_projection_keys(mapped)  # clean mapped dict — no raise

    projections.validate_projection_keys(dict(mapped))  # copy still clean
    with pytest.raises(ValueError):
        projections.validate_projection_keys({**mapped, "passing_yardz": 1})


def test_validator_rejects_raw_sleeper_stats():
    # Proves WHY the validator must be fed the mapped dict, not raw stats: the
    # raw Sleeper stats carry non-scoring keys (bonus_*, gp, pts_ppr, ...).
    raw_stats = _by_id(_raw_rows(), "3163")["stats"]
    with pytest.raises(ValueError):
        projections.validate_projection_keys(raw_stats)


# ---------------------------------------------------------------- ingest + scoring


def test_ingest_stores_canonical_rows_and_scores_directly(db):
    _stub_player(db, sleeper_id="3163", gsis_id="00-0033106")
    n = projections.ingest_projections(db, _raw_rows(), retrieved_as_of="2023-09-05")
    assert n == 3  # QB, K, DEF stored; FB + CB filtered out

    got = projections.get_projections(db, as_of="2023-09-05", season=2023, week=1)
    positions = {r["position"] for r in got}
    assert positions == {"QB", "K", "DEF"}

    rows = {r["source_player_id"]: r for r in got}

    # Skill player resolves gsis via the crosswalk; DEF stays NULL (kept anyway).
    assert rows["3163"]["gsis_id"] == "00-0033106"
    assert rows["DET"]["gsis_id"] is None
    # projected_points captured but never a scoring input.
    assert rows["3163"]["projected_points"] == 24.5
    # forward regime: knowable == retrieved == pull day.
    assert rows["3163"]["knowable_as_of"] == "2023-09-05"

    # A stored row scores DIRECTLY through scoring.score (hand-computed totals).
    assert scoring.score("QB", dict(rows["3163"])) == pytest.approx(26.0)
    assert scoring.score("K", dict(rows["4227"])) == pytest.approx(17.0)
    assert scoring.score("DEF", dict(rows["DET"])) == pytest.approx(24.0)


def test_projected_points_is_never_a_scoring_input(db):
    projections.ingest_projections(db, _raw_rows(), retrieved_as_of="2023-09-05")
    det = projections.get_projections(db, as_of="2023-09-05", position="DEF")[0]
    # Zero out every real stat but keep projected_points: the score must be 0,
    # proving projected_points is inert to scoring.
    stat_only = {k: (0 if isinstance(det[k], (int, float)) else det[k]) for k in det.keys()}
    stat_only["points_allowed"] = None
    stat_only["yards_allowed"] = None
    stat_only["projected_points"] = 99.0
    assert scoring.score("DEF", stat_only) == pytest.approx(0.0)


# ------------------------------------------------------------------------ leakage


def test_forward_regime_leakage_by_retrieval(db):
    # Forward rows stamp knowable = retrieved; an earlier as_of cannot see a
    # later pull.
    projections.ingest_projections(db, _raw_rows(), retrieved_as_of="2023-09-01")
    projections.ingest_projections(db, _raw_rows(), retrieved_as_of="2023-09-05")

    early = projections.get_projections(db, as_of="2023-09-03", season=2023, week=1, position="QB")
    assert len(early) == 1
    assert early[0]["knowable_as_of"] == "2023-09-01"

    before_any = projections.get_projections(db, as_of="2023-08-31", season=2023, week=1)
    assert before_any == []


def test_bulk_historical_revision_needs_latest_truth(db):
    from ziggurat.data.nfl import base

    _week1_schedule(db)  # week_first_gameday_map((2023, 1)) == 2023-09-07

    # Two bulk backfills of the same week, retrieved months apart; both are
    # stamped knowable = the week's first gameday.
    projections.ingest_projections(
        db, _raw_rows(), retrieved_as_of="2024-01-01", bulk_historical=True
    )
    revised = _raw_rows()
    _by_id(revised, "3163")["stats"]["pts_ppr"] = 30.0  # a later correction
    projections.ingest_projections(
        db, revised, retrieved_as_of="2024-02-01", bulk_historical=True
    )

    qb = _by_id(_raw_rows(), "3163")["player_id"]

    # knowable stamped at gameday, not the bulk-load day.
    stored = projections.get_projections(
        db, as_of="2024-03-01", season=2023, week=1, position="QB", source=projections.SOURCE
    )
    assert stored[0]["knowable_as_of"] == "2023-09-07"

    # historical view at an as_of between the two pulls: retrieval gate hides the
    # later revision, returns the first backfill.
    hist = projections.get_projections(
        db, as_of="2024-01-15", season=2023, week=1, position="QB"
    )
    assert len(hist) == 1 and hist[0]["projected_points"] == 24.5

    # latest_truth relaxes the retrieval gate: the newer correction surfaces.
    lt = base.latest_truth(projections.get_projections)(
        db, as_of="2024-01-15", season=2023, week=1, position="QB"
    )
    assert len(lt) == 1 and lt[0]["projected_points"] == 30.0

    # A row whose knowable_as_of (gameday) is after as_of is invisible under BOTH
    # views — latest_truth relaxes only retrieval, never the knowledge gate.
    pregame = "2023-09-06"
    assert projections.get_projections(db, as_of=pregame, season=2023, week=1) == []
    assert base.latest_truth(projections.get_projections)(
        db, as_of=pregame, season=2023, week=1
    ) == []
