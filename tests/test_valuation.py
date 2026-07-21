"""VOR/VBD valuation core + value-view tests (item 2.1, design §5).

Offline throughout: the Sleeper network seam is never called. Synthetic
``by_pos`` inputs drive the pure replacement/flex unit; hand-authored
Sleeper-shaped projection rows (ingested via ``projections.ingest_projections``)
drive the DB-backed tests. Numbers are hand-computable against ``scoring.py``.
"""

import copy
import json
from pathlib import Path

import pytest

from ziggurat.core import scoring, valuation
from ziggurat.data.nfl import base, projections

_FIXTURE = Path(__file__).parent / "fixtures" / "nfl" / "valuation_projections_sample.json"


def _fixture_rows():
    return json.loads(_FIXTURE.read_text())


def _stub_player(db, *, sleeper_id, gsis_id, espn_id=None, name=None, retrieved="2026-07-01"):
    db.execute(
        "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, retrieved_as_of, knowable_as_of) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (gsis_id, sleeper_id, espn_id, name, retrieved, retrieved),
    )
    db.commit()


def _by_pos_row(rows, position):
    return [r for r in rows if r.position == position]


def _one(rows, *, gsis_id=None, team=None, position=None):
    for r in rows:
        if gsis_id is not None and r.gsis_id != gsis_id:
            continue
        if team is not None and r.team != team:
            continue
        if position is not None and r.position != position:
            continue
        return r
    raise AssertionError(f"no row matched gsis={gsis_id} team={team} pos={position}")


# ----------------------------------------------------------- 1. VOR / flex unit


def _synthetic_by_pos(te11_pts: float) -> dict:
    """25 RB / 25 WR / 12 TE / 15 QB / 20 K / 20 DST, all DESC. ``te11_pts`` is
    the first non-starter TE's projection (high => it steals a flex slot)."""
    return {
        "RB": [300 - i * 5 for i in range(25)],   # RB1=300 ... RB21=200 ... RB25=180
        "WR": [290 - i * 5 for i in range(25)],   # WR1=290 ... WR21=190 ... WR25=170
        "TE": [250 - i * 3 for i in range(10)] + [te11_pts, 50.0],  # TE11 = te11_pts, TE12 = 50
        "QB": [400 - i * 2 for i in range(15)],   # 15 QBs, all high-scoring
        "K": [130 - i for i in range(20)],
        "DST": [120 - i * 2 for i in range(20)],
    }


def test_flex_allocation_superflex_guard_and_te_steal():
    roster = valuation.DEFAULT_ROSTER
    dedicated = {p: roster.teams * roster.starters[p] for p in roster.starters}

    # --- superflex guard: no QB ever enters the flex pool ---
    strong = _synthetic_by_pos(te11_pts=210.0)   # TE11=210 beats RB21=200, WR21=190
    repl_s, started_s = valuation.replacement_levels(strong, roster)
    assert started_s["QB"] == 10                 # teams * starters[QB]; unchanged by flex

    # flex slots always total teams * flex_slots across the flex positions
    flex_used = sum(started_s[p] - dedicated[p] for p in roster.flex_positions)
    assert flex_used == roster.teams * roster.flex_slots == 10

    # strong TE steals exactly one flex slot
    assert started_s["TE"] == dedicated["TE"] + 1 == 11
    # its baseline drops to the 12th TE (index 11 = 50.0)
    assert repl_s["TE"] == 50.0

    # --- control: a weak TE11 keeps TE out of the flex pool ---
    weak = _synthetic_by_pos(te11_pts=40.0)
    repl_w, started_w = valuation.replacement_levels(weak, roster)
    assert started_w["TE"] == dedicated["TE"] == 10
    flex_used_w = sum(started_w[p] - dedicated[p] for p in roster.flex_positions)
    assert flex_used_w == 10

    # the TE steal removes exactly one RB/WR flex slot vs the control
    rb_wr_strong = started_s["RB"] + started_s["WR"]
    rb_wr_weak = started_w["RB"] + started_w["WR"]
    assert rb_wr_strong == rb_wr_weak - 1

    # first-non-starter baseline: QB replacement = index-10 QB (11th best)
    assert repl_s["QB"] == strong["QB"][10]

    # K/DST denoise = mean of the rank window around the baseline index
    idx_k = started_s["K"]
    window = strong["K"][max(0, idx_k - 1):idx_k + 2]
    assert repl_s["K"] == pytest.approx(sum(window) / len(window))


def test_replacement_thin_board_clamp_and_empty():
    roster = valuation.DEFAULT_ROSTER
    by_pos = {"RB": [200.0, 150.0], "QB": []}  # far fewer than started
    repl, _ = valuation.replacement_levels(by_pos, roster, denoise_kdst=False)
    assert repl["RB"] == 150.0   # clamp to the last (worst) available
    assert repl["QB"] == 0.0     # empty pool -> 0.0


# --------------------------------------------------------------- 2. leakage


def test_build_valuation_leakage_threads_as_of(db):
    for sid, gsis in [("100", "00-QB")]:
        _stub_player(db, sleeper_id=sid, gsis_id=gsis)

    first = _fixture_rows()
    projections.ingest_projections(db, first, retrieved_as_of="2026-08-01")

    # a later pull bumps the QB (pass_td 2 -> 4: +8/wk => +24 season)
    later = copy.deepcopy(first)
    for r in later:
        if r["player_id"] == "100":
            r["stats"]["pass_td"] = 4
    projections.ingest_projections(db, later, retrieved_as_of="2026-08-15")

    # historical view between the pulls hides the later pull -> the first total (48)
    mid = valuation.build_valuation(db, as_of="2026-08-10", season=2026, weeks=range(1, 18))
    qb_mid = _one(mid, gsis_id="00-QB")
    assert qb_mid.proj_points == pytest.approx(48.0)

    # after the second pull, the bumped total (72) is visible
    after = valuation.build_valuation(db, as_of="2026-08-20", season=2026, weeks=range(1, 18))
    assert _one(after, gsis_id="00-QB").proj_points == pytest.approx(72.0)

    # before any pull -> empty board
    assert valuation.build_valuation(db, as_of="2026-07-01", season=2026) == []


# --------------------------------------- 3. REQUIRED spot-check (i): PPR RB rises


def _rb_row(pid, week, *, rec):
    return {
        "player_id": pid, "season": "2026", "week": week, "season_type": "regular",
        "team": "DAL" if pid == "R1" else "SF", "opponent": "X",
        "player": {"position": "RB", "team": "DAL" if pid == "R1" else "SF"},
        "stats": {"rush_yd": 100, "rush_td": 1, "rec": rec, "rec_yd": 10 * rec, "pts_ppr": 1.0},
    }


def test_pass_catching_rb_has_higher_vor_and_beats_espn(db):
    _stub_player(db, sleeper_id="R1", gsis_id="00-R1", espn_id="e-r1")  # ground RB
    _stub_player(db, sleeper_id="R2", gsis_id="00-R2", espn_id="e-r2")  # receiving RB

    rows = []
    for wk in (1, 2, 3):
        rows.append(_rb_row("R1", wk, rec=0))   # no receptions
        rows.append(_rb_row("R2", wk, rec=6))   # +6 rec/wk (full PPR)
    projections.ingest_projections(db, rows, retrieved_as_of="2026-08-01")

    board = valuation.build_valuation(db, as_of="2026-08-01", season=2026)
    ground = _one(board, gsis_id="00-R1")
    receiving = _one(board, gsis_id="00-R2")

    # per-week: ground = 16 (10 rush + 6 TD), receiving = 16 + 6 rec + 6 recyd = 28
    assert ground.proj_points == pytest.approx(48.0)
    assert receiving.proj_points == pytest.approx(84.0)
    assert receiving.vor > ground.vor
    assert receiving.pos_rank < ground.pos_rank  # better (lower) house pos rank

    # ESPN board ranks the receiving RB LOWER (worse number) than the ground RB.
    espn_rows = [
        {"espn_id": "e-r1", "position": "RB", "team": "DAL", "espn_pos_rank": 2,
         "espn_adp_pos_rank": 2, "overall_rank": 20, "adp": 20.0},
        {"espn_id": "e-r2", "position": "RB", "team": "SF", "espn_pos_rank": 5,
         "espn_adp_pos_rank": 5, "overall_rank": 55, "adp": 55.0},
    ]
    view = valuation.build_value_view(board, espn_rows)
    recv_view = _one_view(view, espn_id="e-r2")
    assert recv_view.house_pos_rank == 1
    assert recv_view.espn_pos_rank == 5
    assert recv_view.pos_rank_delta > 0                   # house values it more
    assert recv_view.flag == valuation.HOUSE_HIGHER


def _one_view(rows, *, espn_id=None, team=None):
    for r in rows:
        if espn_id is not None and r.espn_id == espn_id:
            return r
        if team is not None and r.team == team:
            return r
    raise AssertionError(f"no value-view row matched espn_id={espn_id} team={team}")


# ------------------------- 4. REQUIRED spot-check (ii): low-yards D/ST rises (edge)


def _dst_row(team, week, *, pts_allow, yds_allow):
    return {
        "player_id": team, "season": "2026", "week": week, "season_type": "regular",
        "team": team, "opponent": "X",
        "player": {"position": "DEF", "team": team},
        "stats": {"pts_allow": pts_allow, "yds_allow": yds_allow, "pts_ppr": 1.0},
    }


def test_low_yards_dst_rises_and_proves_per_week_then_sum(db):
    rows = []
    for wk in (1, 2, 3):
        rows.append(_dst_row("PHI", wk, pts_allow=3, yds_allow=90))    # low yards
        rows.append(_dst_row("BUF", wk, pts_allow=3, yds_allow=290))   # high yards
    projections.ingest_projections(db, rows, retrieved_as_of="2026-08-01")

    board = valuation.build_valuation(db, as_of="2026-08-01", season=2026)
    low = _one(board, team="PHI", position="DST")
    high = _one(board, team="BUF", position="DST")

    # per-week: PHI = (pa3 -> +4) + (yds90 -> +5) = +9/wk => 27; BUF = +4 + (yds290 -> +2) = +6/wk => 18
    assert low.proj_points == pytest.approx(27.0)
    assert high.proj_points == pytest.approx(18.0)
    assert low.vor > high.vor
    assert low.pos_rank < high.pos_rank

    # DOUBLES as proof of per-week-then-sum (D1): scoring the SUMMED stat line
    # mis-brackets (pa 9 -> +3, yds 270 -> +2 = +5), which is NOT the real total.
    summed_stat = {"points_allowed": 9, "yards_allowed": 270}
    buggy = scoring.score("DST", summed_stat)
    assert buggy == pytest.approx(5.0)
    assert low.proj_points != pytest.approx(buggy)
    # and equals the explicit Σ of per-week scores
    per_week = sum(
        scoring.score("DST", {"points_allowed": 3, "yards_allowed": 90}) for _ in range(3)
    )
    assert low.proj_points == pytest.approx(per_week)

    # ESPN points-only board can't see the yards edge -> ranks PHI below BUF.
    espn_rows = [
        {"espn_id": None, "position": "D/ST", "team": "PHI", "espn_pos_rank": 2,
         "espn_adp_pos_rank": 2, "overall_rank": 140, "adp": 140.0},
        {"espn_id": None, "position": "D/ST", "team": "BUF", "espn_pos_rank": 1,
         "espn_adp_pos_rank": 1, "overall_rank": 120, "adp": 120.0},
    ]
    view = valuation.build_value_view(board, espn_rows)
    phi = _one_view(view, team="PHI")
    assert phi.house_pos_rank == 1 and phi.espn_pos_rank == 2
    assert phi.pos_rank_delta > 0                    # PHI moves UP vs ESPN
    assert phi.flag == valuation.HOUSE_HIGHER


# --------------------------------------------------- 5. season aggregation


def test_season_aggregation_sums_present_weeks(db):
    for sid, gsis in [("100", "00-QB"), ("201", "00-R1"), ("202", "00-R2"),
                      ("301", "00-WR"), ("302", "00-TE"), ("401", "00-K")]:
        _stub_player(db, sleeper_id=sid, gsis_id=gsis)
    projections.ingest_projections(db, _fixture_rows(), retrieved_as_of="2026-08-01")

    board = valuation.build_valuation(db, as_of="2026-08-01", season=2026)

    rb1 = _one(board, gsis_id="00-R1")
    assert rb1.weeks_counted == 3
    assert rb1.proj_points == pytest.approx(72.0)   # 24 * 3

    # RB2 is missing week 3 (a bye): sums only the two present weeks.
    rb2 = _one(board, gsis_id="00-R2")
    assert rb2.weeks_counted == 2
    assert rb2.proj_points == pytest.approx(36.0)   # 18 * 2

    # proj_points == Σ per-week scoring.score, independently recomputed.
    expected_rb1 = sum(
        scoring.score("RB", dict(r), scoring.HOUSE_RULES)
        for r in projections.get_projections(db, as_of="2026-08-01", season=2026, gsis_id="00-R1")
    )
    assert rb1.proj_points == pytest.approx(expected_rb1)

    det = _one(board, team="DET", position="DST")
    assert det.weeks_counted == 3
    assert det.proj_points == pytest.approx(24.0)   # 8 * 3
    k = _one(board, gsis_id="00-K")
    assert k.proj_points == pytest.approx(27.0)      # 9 * 3


# --------------------------------------------------- 6. integration (done-when)


def test_build_valuation_end_to_end(db):
    for sid, gsis, espn, name in [
        ("100", "00-QB", "e100", "Test QB"),
        ("201", "00-R1", "e201", "Test RB1"),
        ("301", "00-WR", "e301", "Test WR"),
    ]:
        _stub_player(db, sleeper_id=sid, gsis_id=gsis, espn_id=espn, name=name)
    projections.ingest_projections(db, _fixture_rows(), retrieved_as_of="2026-08-01")

    board = valuation.build_valuation(db, as_of="2026-08-01", season=2026)
    assert board  # non-empty

    # positions present, canonicalized (DEF -> DST)
    positions = {r.position for r in board}
    assert {"QB", "RB", "WR", "TE", "K", "DST"} <= positions

    # overall ranks are a contiguous 1..N by descending VOR
    overall = sorted(r.overall_rank for r in board)
    assert overall == list(range(1, len(board) + 1))
    vors = [r.vor for r in sorted(board, key=lambda r: r.overall_rank)]
    assert vors == sorted(vors, reverse=True)

    # espn_id resolved for skill via base.espn_by_gsis; DST carries none
    qb = _one(board, gsis_id="00-QB")
    assert qb.espn_id == "e100" and qb.player == "Test QB"
    det = _one(board, team="DET", position="DST")
    assert det.espn_id is None and det.player == "DET D/ST"

    # every row ships legible reasons (rule 6)
    assert all(len(r.reasons) >= 2 for r in board)
    assert any("low-confidence" in " ".join(det.reasons) for _ in [0])

    # display layer renders without error
    text = valuation.format_valuation(board, top=5)
    assert "player" in text and "vor" in text


def test_espn_by_gsis_crosswalk(db):
    _stub_player(db, sleeper_id="100", gsis_id="00-QB", espn_id="e100")
    _stub_player(db, sleeper_id="201", gsis_id="00-R1", espn_id="e201")
    mapping = base.espn_by_gsis(db)
    assert mapping == {"00-QB": "e100", "00-R1": "e201"}


def test_canon_position():
    assert valuation._canon_position("DEF") == "DST"
    assert valuation._canon_position("D/ST") == "DST"
    assert valuation._canon_position("PK") == "K"
    assert valuation._canon_position("qb") == "QB"
    assert valuation._canon_position("LB") is None
    assert valuation._canon_position(None) is None


# ---------------------------------------- 8. audit fixes: value-view robustness
# The 2.1 adversarial audit confirmed four value-view defects (the primary VOR
# board was clean). These lock the fixes: a draftable filter + a position guard
# so the "what the room can't see" report surfaces actionable gaps, not noise,
# and report-specific flag labels (no misleading "MARKET" in a house-vs-ESPN
# report). See build_value_view docstring.


def _make_row(gsis, espn_id, pos, vor, pos_rank, *, team=None, player=None):
    """A minimal ValuationRow for value-view unit tests."""
    return valuation.ValuationRow(
        gsis_id=gsis, espn_id=espn_id, team=team, player=player or gsis,
        position=pos, season=2026, weeks_counted=17,
        proj_points=100.0 + vor, replacement_points=100.0, vor=vor,
        pos_rank=pos_rank, overall_rank=pos_rank, reasons=(),
    )


def test_value_view_excludes_below_replacement_by_default():
    """min_vor=0.0 (default) keeps undraftable replacement-floor players — whose
    house pos_rank is a meaningless tiebreak over a board 3-4x deeper than ESPN's
    — out of the report, so the |delta| sort surfaces real targets not noise."""
    draftable = _make_row("00-A", "eA", "WR", vor=40.0, pos_rank=5)
    scrub = _make_row("00-Z", "eZ", "WR", vor=-185.0, pos_rank=1350)  # replacement floor
    espn_rows = [
        {"espn_id": "eA", "position": "WR", "team": "CIN", "espn_pos_rank": 20,
         "espn_adp_pos_rank": 20, "overall_rank": 60, "adp": 60.0},
        {"espn_id": "eZ", "position": "WR", "team": "NYJ", "espn_pos_rank": 195,
         "espn_adp_pos_rank": 195, "overall_rank": 800, "adp": 800.0},
    ]
    view = valuation.build_value_view([draftable, scrub], espn_rows)
    ids = {r.espn_id for r in view}
    assert "eA" in ids                       # draftable target kept
    assert "eZ" not in ids                    # replacement-floor scrub filtered
    # the huge |delta| of the scrub (195-1350) must not lead the report
    assert view[0].espn_id == "eA"

    # min_vor=None disables the filter (e.g. to inspect fades below replacement)
    unfiltered = valuation.build_value_view([draftable, scrub], espn_rows, min_vor=None)
    assert {r.espn_id for r in unfiltered} == {"eA", "eZ"}


def test_value_view_skips_cross_position_espn_match():
    """A skill player Sleeper calls TE but ESPN tags RB must NOT be compared: the
    ESPN rank is from a different position pool, so the delta would be a
    meaningless cross-pool subtraction with a factually wrong reason string."""
    te = _make_row("00-T", "eT", "TE", vor=30.0, pos_rank=6)
    espn_rows = [
        # same espn_id, but ESPN ranks this player as an RB (position disagreement)
        {"espn_id": "eT", "position": "RB", "team": "PIT", "espn_pos_rank": 95,
         "espn_adp_pos_rank": 95, "overall_rank": 150, "adp": 150.0},
    ]
    view = valuation.build_value_view([te], espn_rows)
    assert view == []                        # skipped, not cross-pool compared

    # when positions AGREE, it compares normally
    espn_ok = [dict(espn_rows[0], position="TE")]
    view_ok = valuation.build_value_view([te], espn_ok)
    assert len(view_ok) == 1 and view_ok[0].position == "TE"


def test_value_view_flags_are_report_specific_not_market():
    """The value view ships HOUSE_HIGHER/ESPN_HIGHER/ALIGNED — never the
    divergence 'MARKET_*' strings (this report has no market side; rule 6)."""
    house_hi = _make_row("00-H", "eH", "WR", vor=40.0, pos_rank=5)   # house ranks better
    espn_hi = _make_row("00-E", "eE", "WR", vor=35.0, pos_rank=25)   # ESPN ranks better
    aligned = _make_row("00-Q", "eQ", "WR", vor=30.0, pos_rank=10)
    espn_rows = [
        {"espn_id": "eH", "position": "WR", "team": "CIN", "espn_pos_rank": 22,
         "espn_adp_pos_rank": 22, "overall_rank": 60, "adp": 60.0},
        {"espn_id": "eE", "position": "WR", "team": "KC", "espn_pos_rank": 8,
         "espn_adp_pos_rank": 8, "overall_rank": 20, "adp": 20.0},
        {"espn_id": "eQ", "position": "WR", "team": "LA", "espn_pos_rank": 10,
         "espn_adp_pos_rank": 10, "overall_rank": 25, "adp": 25.0},
    ]
    view = valuation.build_value_view([house_hi, espn_hi, aligned], espn_rows)
    flags = {r.espn_id: r.flag for r in view}
    assert flags["eH"] == valuation.HOUSE_HIGHER
    assert flags["eE"] == valuation.ESPN_HIGHER
    assert flags["eQ"] == valuation.ALIGNED
    assert all(r.flag in valuation.VALUE_FLAGS for r in view)
    assert all("MARKET" not in r.flag for r in view)
