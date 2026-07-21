"""Room-prior calibration tests (item 2.2).

Two layers:
  * SYNTHETIC-fixture tests (default): tiny hand-built pick lists + boards prove
    the filters (human-only, skill-only, IDP filter, lineupSlotId NEVER used) and
    the stats, fully offline. Rule 5: every name/team here is invented — no real
    colleague names, owner GUIDs, or real league team abbrevs.
  * REAL-artifact anchor test: recomputes the fit from the gitignored 2025 pulls
    under data/recon-2.2/ and asserts it reproduces the recon note's precomputed
    numbers. Guarded with skipif so fresh clones / CI stay green.

Also a Rule-8 boundary test: no ziggurat module OUTSIDE draft/ imports draft.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ziggurat.draft import calibration as cal

# --------------------------------------------------------------- synthetic data
# Rule 5: all invented. Universe player_id -> (name, defaultPositionId, team).
# Positions: 1 QB, 2 RB, 3 WR, 4 TE, 5 K, 16 DST, 99 = non-league (IDP-ish).

_SYNTH_UNIVERSE = [
    {"id": 101, "fullName": "Aaa Runner", "defaultPositionId": 2, "team": "ALPHA"},
    {"id": 102, "fullName": "Bbb Catcher", "defaultPositionId": 3, "team": "BETA"},
    {"id": 103, "fullName": "Ccc Thrower", "defaultPositionId": 1, "team": "GAMMA"},
    {"id": 104, "fullName": "Ddd Tightend", "defaultPositionId": 4, "team": "DELTA"},
    {"id": 105, "fullName": "Eee Kicker", "defaultPositionId": 5, "team": "ALPHA"},
    {"id": 106, "fullName": "Fff Wall", "defaultPositionId": 16, "team": "BETA"},
    # Ggg's universe team (GAMMA) differs from its board team (OMEGA) -> the
    # name-only fallback must recover it (post-snapshot team drift).
    {"id": 107, "fullName": "Ggg Rookie", "defaultPositionId": 2, "team": "GAMMA"},
    # Non-league position: DEFPOS has no 99 -> position None -> dropped everywhere,
    # even though its pick carries an RB lineupSlotId (the slot must be ignored).
    {"id": 110, "fullName": "Hhh Special", "defaultPositionId": 99, "team": "ALPHA"},
]

# Editorial board: id -> ppr_rank (skill ranked tight; K/DST buried, as ESPN does).
_SYNTH_EDITORIAL = [
    {"id": 102, "name": "Bbb Catcher", "pos": 3, "ppr_rank": 1},
    {"id": 101, "name": "Aaa Runner", "pos": 2, "ppr_rank": 2},
    {"id": 103, "name": "Ccc Thrower", "pos": 1, "ppr_rank": 4},
    {"id": 104, "name": "Ddd Tightend", "pos": 4, "ppr_rank": 5},
    {"id": 107, "name": "Ggg Rookie", "pos": 2, "ppr_rank": 6},
    {"id": 105, "name": "Eee Kicker", "pos": 5, "ppr_rank": 50},
    {"id": 106, "name": "Fff Wall", "pos": 16, "ppr_rank": 60},
]

# fpecr raw board (pre-filter, pre-rerank): includes ONE IDP row (Zzz, LB) that
# must be dropped, and Ggg on OMEGA (team-drift). ecr order defines the re-rank.
_SYNTH_FPECR_ROWS = [
    {"ro_rank_1based": 0, "player": "Aaa Runner", "pos": "RB", "team": "ALPHA", "ecr": 1.0, "best": 1.0, "worst": 3.0},
    {"ro_rank_1based": 1, "player": "Zzz Linebacker", "pos": "LB", "team": "ALPHA", "ecr": 1.5, "best": 1.0, "worst": 4.0},
    {"ro_rank_1based": 2, "player": "Bbb Catcher", "pos": "WR", "team": "BETA", "ecr": 2.0, "best": 1.0, "worst": 5.0},
    {"ro_rank_1based": 3, "player": "Ccc Thrower", "pos": "QB", "team": "GAMMA", "ecr": 3.0, "best": 2.0, "worst": 6.0},
    {"ro_rank_1based": 4, "player": "Ddd Tightend", "pos": "TE", "team": "DELTA", "ecr": 4.0, "best": 2.0, "worst": 8.0},
    {"ro_rank_1based": 5, "player": "Ggg Rookie", "pos": "RB", "team": "OMEGA", "ecr": 5.0, "best": 3.0, "worst": 9.0},
    {"ro_rank_1based": 6, "player": "Eee Kicker", "pos": "K", "team": "ALPHA", "ecr": 6.0, "best": 4.0, "worst": 12.0},
    {"ro_rank_1based": 7, "player": "Fff Wall", "pos": "DST", "team": "BETA", "ecr": 7.0, "best": 5.0, "worst": 14.0},
]

# Picks: (overall, round, team_id, player_id, autoDraftTypeId, lineupSlotId).
# lineupSlotId is set ADVERSARIALLY (pick 2 = Aaa RB carries a WR slot=4; pick 8
# = Hhh non-league carries an RB slot=2) so any curve that read the slot instead
# of the universe defaultPositionId would be caught.
_SYNTH_PICK_TUPLES = [
    (1, 1, "A", 102, 0, 23),   # Bbb WR, human, FLEX slot
    (2, 1, "B", 101, 0, 4),    # Aaa RB, human, WR slot (slot!=pos trap)
    (3, 2, "B", 103, 0, 0),    # Ccc QB, human
    (4, 2, "A", 107, 0, 2),    # Ggg RB, human (name-fallback join)
    (5, 3, "A", 104, 2, 6),    # Ddd TE, TYPE-2 autopick -> NON-human
    (6, 3, "B", 105, 0, 17),   # Eee K, human but K -> excluded from reach
    (7, 4, "B", 106, 0, 20),   # Fff DST, human but DST -> excluded from reach
    (8, 4, "A", 110, 0, 2),    # Hhh non-league pos, RB slot -> dropped (slot trap)
    (9, 1, "C", 101, 3, 2),    # seat C fully autodrafts (type 3) all four picks
    (10, 2, "C", 102, 3, 4),
    (11, 3, "C", 103, 3, 0),
    (12, 4, "C", 104, 3, 6),
]


def _picks_json():
    picks = [
        {
            "overallPickNumber": ov, "roundId": rd, "roundPickNumber": ov,
            "teamId": tid, "playerId": pid, "autoDraftTypeId": auto,
            "lineupSlotId": slot, "memberId": "", "keeper": False,
        }
        for (ov, rd, tid, pid, auto, slot) in _SYNTH_PICK_TUPLES
    ]
    return [{"draftDetail": {"drafted": True, "inProgress": False, "picks": picks}}]


@pytest.fixture()
def synth_paths(tmp_path):
    """Write the synthetic artifacts to tmp files; return their paths."""
    picks_p = tmp_path / "picks.json"
    kona_p = tmp_path / "kona.json"
    ed_p = tmp_path / "editorial.json"
    fpecr_p = tmp_path / "fpecr.parquet"
    picks_p.write_text(json.dumps(_picks_json()))
    kona_p.write_text(json.dumps(_SYNTH_UNIVERSE))
    ed_p.write_text(json.dumps(_SYNTH_EDITORIAL))
    pd.DataFrame(_SYNTH_FPECR_ROWS).to_parquet(fpecr_p)
    return {"picks": picks_p, "kona": kona_p, "editorial": ed_p, "fpecr": fpecr_p}


# --------------------------------------------------------------- normalization


def test_normalize_name_strips_suffix_and_punct():
    assert cal.normalize_name("Ja'Marr Chase") == "jamarr chase"
    assert cal.normalize_name("Marvin Harrison Jr.") == "marvin harrison"
    assert cal.normalize_name("A.J. Brown") == "aj brown"
    assert cal.normalize_name(None) == ""


def test_normalize_team_aliases():
    assert cal.normalize_team("JAC") == "JAX"
    assert cal.normalize_team("WSH") == "WAS"
    assert cal.normalize_team("LAR") == "LA"
    assert cal.normalize_team("FA") is None
    assert cal.normalize_team(0) is None
    assert cal.normalize_team("KC") == "KC"


def test_load_kona_universe_pro_team_id_path():
    """The proTeamId -> abbr path (espn_api PRO_TEAM_MAP) resolves; explicit team
    key is preferred when present."""
    import tempfile

    data = [
        {"id": 1, "fullName": "Pro Guy", "defaultPositionId": 2, "proTeamId": 1},  # ATL
        {"id": 2, "fullName": "Free Guy", "defaultPositionId": 3, "proTeamId": 0},  # FA -> None
        {"id": 3, "fullName": "Direct Guy", "defaultPositionId": 1, "team": "KC"},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(data, fh)
        p = fh.name
    uni = cal.load_kona_universe(p)
    assert uni[1]["team"] == "ATL" and uni[1]["position"] == "RB"
    assert uni[2]["team"] is None
    assert uni[3]["team"] == "KC" and uni[3]["position"] == "QB"


# --------------------------------------------------------------- fpecr board


def test_fpecr_idp_filtered_and_reranked(synth_paths):
    board = cal.load_fpecr_board(synth_paths["fpecr"])
    cov = board["coverage"]
    assert cov["rows_before"] == 8
    assert cov["rows_after_idp_filter"] == 7  # the LB row dropped
    assert cov["idp_removed"] == 1
    # re-rank is dense 1..n by ecr; Aaa (ecr 1.0) -> 1, Bbb (2.0) -> 2, ...
    assert board["by_name_team"][("aaa runner", "ALPHA")] == 1
    assert board["by_name_team"][("bbb catcher", "BETA")] == 2
    assert board["by_name_team"][("ggg rookie", "OMEGA")] == 5
    # no IDP row survives
    assert all(r["position"] in cal.DRAFTABLE_POSITIONS for r in board["rows"])
    assert ("zzz linebacker", "ALPHA") not in board["by_name_team"]


# --------------------------------------------------------------- reach: fpecr


def test_reach_vs_fpecr_human_skill_only_with_name_fallback(synth_paths):
    picks = cal.load_picks(synth_paths["picks"])
    uni = cal.load_kona_universe(synth_paths["kona"])
    fpecr = cal.load_fpecr_board(synth_paths["fpecr"])
    r = cal.reach_vs_fpecr(picks, uni, fpecr)

    # 4 human skill picks: Bbb(102), Aaa(101), Ccc(103), Ggg(107).
    # K/DST (Eee, Fff), the type-2 Ddd, the non-league Hhh, and the type-3 seat C
    # are all excluded.
    assert r["skill_human_picks"] == 4
    assert r["n"] == 4
    # reach = fpecr_rank - overall:
    #   Bbb rank2-1=+1, Aaa rank1-2=-1, Ccc rank3-3=0, Ggg rank5-4=+1
    assert sorted(  # reconstruct the multiset from stats we assert individually
        [r["min"], r["max"]]
    ) == [-1.0, 1.0]
    assert r["mean"] == pytest.approx(0.25)
    # Ggg only joins via the unique-name fallback (its team drifted GAMMA->OMEGA).
    assert r["matched_name_team"] == 3
    assert r["matched_name_fallback"] == 1
    assert r["join_coverage"] == "4/4"
    assert r["join_coverage_name_team_only"] == "3/4"


def test_reach_vs_fpecr_no_fallback_drops_team_drift(synth_paths):
    picks = cal.load_picks(synth_paths["picks"])
    uni = cal.load_kona_universe(synth_paths["kona"])
    fpecr = cal.load_fpecr_board(synth_paths["fpecr"])
    r = cal.reach_vs_fpecr(picks, uni, fpecr, name_fallback=False)
    assert r["n"] == 3  # Ggg is dropped without the fallback
    assert r["matched_name_fallback"] == 0
    assert r["join_coverage"] == "3/4"


# --------------------------------------------------------------- reach: editorial


def test_reach_vs_editorial_join_by_player_id(synth_paths):
    picks = cal.load_picks(synth_paths["picks"])
    uni = cal.load_kona_universe(synth_paths["kona"])
    ed = cal.load_editorial_board(synth_paths["editorial"])
    r = cal.reach_vs_editorial(picks, uni, ed)
    # Same 4 human skill picks; reach = editorial_rank - overall:
    #   Bbb 1-1=0, Aaa 2-2=0, Ccc 4-3=+1, Ggg 6-4=+2
    assert r["n"] == 4
    assert r["mean"] == pytest.approx(0.75)
    assert r["min"] == 0.0 and r["max"] == 2.0
    assert r["join_coverage"] == "4/4"
    assert r["board_adherence_pearson"] is not None


# --------------------------------------------------------------- autodraft share


def test_autodraft_share(synth_paths):
    picks = cal.load_picks(synth_paths["picks"])
    a = cal.autodraft_share(picks)
    assert a["total_seats"] == 3          # A, B, C
    assert a["full_auto_seats"] == 1      # seat C is all type-3
    assert a["autodraft_fraction"] == pytest.approx(1 / 3)
    assert a["type2_scatter_picks"] == 1  # the Ddd type-2 pick
    assert a["type2_rate"] == pytest.approx(1 / 12)
    assert a["auto_type_counts"] == {"0": 7, "2": 1, "3": 4}


# --------------------------------------------------------------- position curve


def test_position_run_curve_uses_defaultposition_not_lineupslot(synth_paths):
    picks = cal.load_picks(synth_paths["picks"])
    uni = cal.load_kona_universe(synth_paths["kona"])
    c = cal.position_run_curve(picks, uni)
    counts = c["counts_by_round"]
    # Human picks only; position from universe defaultPositionId.
    # R1: Bbb WR (slot 23) + Aaa RB (slot 4, WR-slot TRAP) -> must be {WR:1, RB:1}
    assert counts[1] == {"WR": 1, "RB": 1}
    assert counts[2] == {"QB": 1, "RB": 1}
    assert counts[3] == {"K": 1}          # Eee (human K); the type-2 Ddd excluded
    # R4: only Fff DST. Hhh (pick 8) carries an RB lineupSlotId but a non-league
    # defaultPositionId -> dropped. If the slot were read, an RB would appear here.
    assert counts[4] == {"DST": 1}
    assert "RB" not in counts[4]
    assert c["n_human_picks"] == 6        # 7 human picks minus the dropped Hhh


# --------------------------------------------------------------- K/DST windows


def test_kdst_windows(synth_paths):
    picks = cal.load_picks(synth_paths["picks"])
    uni = cal.load_kona_universe(synth_paths["kona"])
    w = cal.kdst_windows(picks, uni)
    assert w["human"]["first_k"] == {"overall": 6, "round": 3}
    assert w["human"]["first_dst"] == {"overall": 7, "round": 4}
    assert w["kdst_earliest_round"] == 3  # min(first human K round, first human DST round)


# --------------------------------------------------------------- orchestrator


def test_fit_room_priors_shape(synth_paths):
    fit = cal.fit_room_priors(
        picks_path=synth_paths["picks"],
        kona_path=synth_paths["kona"],
        editorial_path=synth_paths["editorial"],
        fpecr_path=synth_paths["fpecr"],
    )
    rp = fit["recommended_priors"]
    assert rp["autodraft_fraction"] == pytest.approx(1 / 3)
    assert rp["kdst_earliest_round"] == 3
    # primary reference defaults to fpecr -> reach_center reads off fpecr mean.
    assert rp["reach_center"] == pytest.approx(0.25)
    assert fit["meta"]["primary_reach_reference"] == "fpecr"
    assert set(fit["reach"]) == {"fpecr", "editorial"}


# The Rule-8 "no module outside draft/ imports draft" boundary test lives in the
# canonical tests/test_draft_boundary.py (AST module-level scan). It was removed
# from here during integration to avoid a duplicate.


# --------------------------------------------------------------- REAL artifacts

_REAL = {
    "picks": cal.DEFAULT_PICKS_PATH,
    "kona": cal.DEFAULT_KONA_PATH,
    "editorial": cal.DEFAULT_EDITORIAL_PATH,
    "fpecr": cal.DEFAULT_FPECR_PATH,
}
_real_missing = [name for name, p in _REAL.items() if not Path(p).exists()]

pytestmark_real = pytest.mark.skipif(
    bool(_real_missing),
    reason=f"gitignored 2025 recon artifacts absent: {_real_missing}",
)


@pytestmark_real
def test_real_fit_reproduces_recon_anchors():
    """The fit on the real 2025 pulls must reproduce the recon note's precomputed
    numbers (Pearson 0.855 editorial, mean reach ~15 / n=109 editorial, 2/10
    autodraft, first K overall 86 R9 / first DST overall 93 R10)."""
    fit = cal.fit_room_priors()

    ed = fit["reach"]["editorial"]
    assert ed["n"] == 109
    assert ed["mean"] == pytest.approx(15.06, abs=0.05)
    assert ed["board_adherence_pearson"] == pytest.approx(0.8555, abs=0.001)
    assert ed["board_adherence_n"] == 160

    fp = fit["reach"]["fpecr"]
    # fpecr reach is ~symmetric (mean ~0) — the reason it's the shipped primary.
    assert fp["matched_name_team"] == 106
    assert fp["join_coverage_name_team_only"] == "106/109"
    assert fp["mean"] == pytest.approx(1.17, abs=0.05)
    assert fp["board_coverage"]["idp_removed"] == 195

    auto = fit["autodraft"]
    assert auto["full_auto_seats"] == 2 and auto["total_seats"] == 10
    assert auto["autodraft_fraction"] == pytest.approx(0.2)

    w = fit["kdst_windows"]
    assert w["all_picks"]["first_k"] == {"overall": 86, "round": 9}
    assert w["all_picks"]["first_dst"] == {"overall": 93, "round": 10}
    assert w["kdst_earliest_round"] == 9


@pytestmark_real
def test_shipped_priors_agree_with_the_live_fit():
    """Integration guard (item 2.2): the values shipped in ``priors.py``
    (``ROOM_PRIORS_2025``) must stay consistent with what ``calibration.py``
    computes from the raw 2025 artifacts, so a future calibration change that moves
    the fit surfaces a priors.py drift instead of silently disagreeing.

    Fields sourced directly from the fit are checked; ``board_adherence`` is
    DELIBERATELY neutral (1.0), not the fitted Pearson (0.907) — reach_sigma already
    embeds the room's board adherence, so it is recorded, not composed (see
    ``RoomPriors.board_adherence`` / ``EMPIRICAL_BOARD_ADHERENCE_2025``)."""
    from ziggurat.draft.priors import (
        EMPIRICAL_BOARD_ADHERENCE_2025,
        ROOM_PRIORS_2025,
    )

    fit = cal.fit_room_priors()
    rp = fit["recommended_priors"]

    # fitted-sourced fields
    assert ROOM_PRIORS_2025.autodraft_fraction == pytest.approx(rp["autodraft_fraction"])
    assert ROOM_PRIORS_2025.kdst_earliest_round == rp["kdst_earliest_round"]
    # reach_sigma is the fpecr population std, rounded to 2 dp for a weak prior
    assert ROOM_PRIORS_2025.reach_sigma == pytest.approx(rp["reach_sigma"], abs=0.01)
    # board_adherence held neutral; the fitted Pearson is recorded separately
    assert ROOM_PRIORS_2025.board_adherence == 1.0
    assert EMPIRICAL_BOARD_ADHERENCE_2025 == pytest.approx(rp["board_adherence"], abs=0.001)

    # full 16-round position-run curve matches the fitted within-round fractions
    fitted_curve = fit["position_run_curve"]["fractions_by_round"]
    assert set(ROOM_PRIORS_2025.position_run) == {int(r) for r in fitted_curve}
    for rnd, fracs in ROOM_PRIORS_2025.position_run.items():
        fitted = fitted_curve[rnd]
        assert set(fracs) == set(fitted), f"round {rnd} positions drifted"
        for pos, val in fracs.items():
            assert val == pytest.approx(fitted[pos], abs=5e-4), f"round {rnd} {pos}"
