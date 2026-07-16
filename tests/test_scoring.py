"""Golden-master + adversarial suite for the house-rules scoring engine (item 1.3).

Three layers:
  1. `test_scoring_matches_espn_fixture` — a transcription LOCK: every one of the
     46 values in the committed ESPN fixture must equal the corresponding value
     encoded in `scoring.py`. A typo, a flipped sign, or a new ESPN statId all
     break this before they can silently corrupt a season of scoring.
  2. `GOLDEN_CASES` — table-driven stat line -> hand-computed points, covering
     every PA and YA bracket boundary (incl. the implicit-zero bands), the
     distance-tiered kicker with missed-XP-vs-missed-FG, 2-pt conversions, D/ST
     event + safety combos, the exotic 1-pt-safety / 2-pt-return, and negatives.
  3. Targeted tests for the trickier semantics (skip-absent brackets, dispatch).

Post-Week-1 these get validated against real ESPN box scores (the anchored TODO
in `scoring.py` / item 3.8).
"""

import json
import math
import re
from decimal import Decimal
from pathlib import Path

import pytest

from ziggurat.core.scoring import (
    HOUSE_RULES,
    ScoringRules,
    _bracket_points,
    score,
    score_dst,
)

FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "scoring_format.json"


# ---------------------------------------------------------------------------
# Layer 1: transcription lock against the captured ESPN settings.
# ---------------------------------------------------------------------------
def test_scoring_matches_espn_fixture():
    """Every ESPN statId's points == the value scoring.py encodes for it.

    Bracket/kicker statIds are checked by evaluating the engine's partition at a
    representative value inside the corresponding band (rep values deliberately
    avoid the implicit-zero gaps). `set` equality guarantees full coverage: an
    ESPN statId this map doesn't account for is a hard failure, not a silent gap.
    """
    r = HOUSE_RULES
    fixture = {it["abbr"]: it["points"] for it in json.loads(FIXTURE.read_text())}
    engine = {
        # offense linear
        "PY": r.points_per_passing_yard,
        "PTD": r.points_per_passing_td,
        "INTT": r.points_per_interception_thrown,
        "RY": r.points_per_rushing_yard,
        "RTD": r.points_per_rushing_td,
        "REC": r.points_per_reception,
        "REY": r.points_per_receiving_yard,
        "RETD": r.points_per_receiving_td,
        "FUML": r.points_per_fumble_lost,
        "2PC": r.points_per_two_point_conversion,
        "2PR": r.points_per_two_point_conversion,
        "2PRE": r.points_per_two_point_conversion,
        # kicker
        "PAT": r.points_per_pat_made,
        "FGM": r.points_per_missed_fg,
        "FG0": _bracket_points(20, r.fg_distance_brackets),
        "FG40": _bracket_points(45, r.fg_distance_brackets),
        "FG50": _bracket_points(55, r.fg_distance_brackets),
        "FG60": _bracket_points(65, r.fg_distance_brackets),
        # D/ST events
        "SK": r.points_per_sack,
        "INT": r.points_per_def_interception,
        "FR": r.points_per_fumble_recovery,
        "SF": r.points_per_safety,
        "BLKK": r.points_per_blocked_kick,
        # D/ST defensive + return TDs (all collapse to def_td)
        "FTD": r.points_per_def_td,
        "BLKKRTD": r.points_per_def_td,
        "KRTD": r.points_per_def_td,
        "PRTD": r.points_per_def_td,
        "INTTD": r.points_per_def_td,
        "FRTD": r.points_per_def_td,
        # D/ST exotics
        "1PSF": r.points_per_one_point_safety,
        "2PRET": r.points_per_two_point_return,
        # D/ST points-allowed brackets (rep value inside each band)
        "PA0": _bracket_points(0, r.points_allowed_brackets),
        "PA1": _bracket_points(3, r.points_allowed_brackets),
        "PA7": _bracket_points(10, r.points_allowed_brackets),
        "PA14": _bracket_points(15, r.points_allowed_brackets),
        "PA28": _bracket_points(30, r.points_allowed_brackets),
        "PA35": _bracket_points(40, r.points_allowed_brackets),
        "PA46": _bracket_points(50, r.points_allowed_brackets),
        # D/ST yards-allowed brackets (rep value inside each band)
        "YA100": _bracket_points(50, r.yards_allowed_brackets),
        "YA199": _bracket_points(150, r.yards_allowed_brackets),
        "YA299": _bracket_points(250, r.yards_allowed_brackets),
        "YA399": _bracket_points(375, r.yards_allowed_brackets),
        "YA449": _bracket_points(425, r.yards_allowed_brackets),
        "YA499": _bracket_points(475, r.yards_allowed_brackets),
        "YA549": _bracket_points(525, r.yards_allowed_brackets),
        "YA550": _bracket_points(600, r.yards_allowed_brackets),
    }
    assert set(engine) == set(fixture), (
        "ESPN statId coverage drift: "
        f"unmapped={set(fixture) - set(engine)}, stale={set(engine) - set(fixture)}"
    )
    for abbr, points in fixture.items():
        assert engine[abbr] == pytest.approx(points), f"{abbr}: engine {engine[abbr]} != ESPN {points}"


# ---------------------------------------------------------------------------
# Layer 2: hand-computed golden cases.  (label, position, stats, expected)
# ---------------------------------------------------------------------------
GOLDEN_CASES = [
    # ---- offense: full PPR incl. 2-pt conversions -------------------------
    (
        "qb_typical",
        "QB",
        {"passing_yards": 287, "passing_tds": 2, "interceptions": 1, "rushing_yards": 12},
        # 287*0.04 + 2*4 - 1*2 + 12*0.1 = 11.48 + 8 - 2 + 1.2
        18.68,
    ),
    (
        "qb_with_passing_2pt",
        "QB",
        {"passing_yards": 300, "passing_tds": 3, "passing_2pt_conversions": 1},
        # 12 + 12 + 2
        26.0,
    ),
    (
        "rb_full_ppr_with_fumble",
        "RB",
        {"rushing_yards": 80, "rushing_tds": 1, "receptions": 5, "receiving_yards": 42, "fumbles_lost": 1},
        # 8 + 6 + 5 + 4.2 - 2
        21.2,
    ),
    (
        "rb_with_rushing_2pt",
        "RB",
        {"rushing_yards": 50, "rushing_tds": 1, "receptions": 3, "receiving_yards": 20, "rushing_2pt_conversions": 1},
        # 5 + 6 + 3 + 2 + 2
        18.0,
    ),
    (
        "wr_receiving_line",
        "WR",
        {"receptions": 6, "receiving_yards": 88, "receiving_tds": 1},
        # 6 + 8.8 + 6
        20.8,
    ),
    (
        "wr_with_receiving_2pt",
        "WR",
        {"receptions": 7, "receiving_yards": 110, "receiving_tds": 1, "receiving_2pt_conversions": 1},
        # 7 + 11 + 6 + 2
        26.0,
    ),
    (
        # Stat type is NOT gated by position: dispatch picks the scorer family,
        # and a WR's rushing yards (jet sweep) score like anyone else's.
        "wr_rushing_yards_also_score",
        "WR",
        {"receptions": 4, "receiving_yards": 55, "rushing_yards": 12},
        # 4 + 5.5 + 1.2
        10.7,
    ),
    (
        "qb_rich_line_with_rush_td_and_2pt",
        "QB",
        {"passing_yards": 312, "passing_tds": 3, "interceptions": 1, "rushing_yards": 28, "rushing_tds": 1, "passing_2pt_conversions": 1},
        # 12.48 + 12 - 2 + 2.8 + 6 + 2
        33.28,
    ),
    (
        "offense_line_can_go_negative",
        "QB",
        {"passing_yards": 150, "interceptions": 3, "fumbles_lost": 1},
        # 6 - 6 - 2
        -2.0,
    ),
    (
        # nflverse splits lost fumbles into components; each scores −2.
        "rb_nflverse_component_fumbles",
        "RB",
        {"rushing_yards": 60, "rushing_tds": 1, "rushing_fumbles_lost": 1, "receiving_fumbles_lost": 1},
        # 6 + 6 - 2 - 2
        8.0,
    ),
    ("empty_line_scores_zero", "TE", {}, 0.0),
    (
        "non_scoring_columns_ignored",
        "QB",
        {"passing_yards": 100, "attempts": 30, "completions": 22, "snap_share": 0.97},
        4.0,
    ),
    # ---- kicker: distance tiers, PATs, and the −1/miss rule ---------------
    (
        "kicker_each_distance_bucket_via_counts",
        "K",
        {"fg_made_0_39": 1, "fg_made_40_49": 1, "fg_made_50_59": 1, "fg_made_60": 1},
        # 3 + 4 + 5 + 6
        18.0,
    ),
    (
        "kicker_distances_at_bucket_boundaries",
        "K",
        {"fg_made_distances": [39, 40, 49, 50, 59, 60]},
        # 3 + 4 + 4 + 5 + 5 + 6
        27.0,
    ),
    (
        "kicker_makes_pats_and_missed_fgs",
        "K",
        {"fg_made_0_39": 2, "pat_made": 3, "fg_missed": 2},
        # 2*3 + 3*1 + 2*(-1)
        7.0,
    ),
    (
        "kicker_missed_xp_scores_zero_missed_fg_scores_minus_one",
        "K",
        {"fg_made_40_49": 1, "pat_made": 2, "pat_missed": 1, "fg_missed": 1},
        # 4 + 2 + (missed XP: 0) + (missed FG: -1)
        5.0,
    ),
    (
        "kicker_count_and_distance_forms_are_additive",
        "K",
        {"fg_made_0_39": 1, "fg_made_distances": [45]},
        # 3 + 4
        7.0,
    ),
    ("kicker_only_pats", "K", {"pat_made": 4}, 4.0),
    ("kicker_empty_scores_zero", "K", {}, 0.0),
    # ---- D/ST points-allowed: every bracket + both sides of each edge -----
    ("dst_pa_0_shutout", "DST", {"points_allowed": 0}, 5.0),
    ("dst_pa_1", "DST", {"points_allowed": 1}, 4.0),
    ("dst_pa_6", "DST", {"points_allowed": 6}, 4.0),
    ("dst_pa_7", "DST", {"points_allowed": 7}, 3.0),
    ("dst_pa_13", "DST", {"points_allowed": 13}, 3.0),
    ("dst_pa_14", "DST", {"points_allowed": 14}, 1.0),
    ("dst_pa_17", "DST", {"points_allowed": 17}, 1.0),
    ("dst_pa_18_implicit_zero", "DST", {"points_allowed": 18}, 0.0),
    ("dst_pa_27_implicit_zero", "DST", {"points_allowed": 27}, 0.0),
    ("dst_pa_28", "DST", {"points_allowed": 28}, -1.0),
    ("dst_pa_34", "DST", {"points_allowed": 34}, -1.0),
    ("dst_pa_35", "DST", {"points_allowed": 35}, -3.0),
    ("dst_pa_45", "DST", {"points_allowed": 45}, -3.0),
    ("dst_pa_46", "DST", {"points_allowed": 46}, -5.0),
    ("dst_pa_60", "DST", {"points_allowed": 60}, -5.0),
    # ---- D/ST yards-allowed: every bracket + both sides of each edge ------
    ("dst_ya_0", "DST", {"yards_allowed": 0}, 5.0),
    ("dst_ya_99", "DST", {"yards_allowed": 99}, 5.0),
    ("dst_ya_100", "DST", {"yards_allowed": 100}, 3.0),
    ("dst_ya_199", "DST", {"yards_allowed": 199}, 3.0),
    ("dst_ya_200", "DST", {"yards_allowed": 200}, 2.0),
    ("dst_ya_299", "DST", {"yards_allowed": 299}, 2.0),
    ("dst_ya_300_implicit_zero", "DST", {"yards_allowed": 300}, 0.0),
    ("dst_ya_349_implicit_zero", "DST", {"yards_allowed": 349}, 0.0),
    ("dst_ya_350", "DST", {"yards_allowed": 350}, -1.0),
    ("dst_ya_399", "DST", {"yards_allowed": 399}, -1.0),
    ("dst_ya_400", "DST", {"yards_allowed": 400}, -3.0),
    ("dst_ya_449", "DST", {"yards_allowed": 449}, -3.0),
    ("dst_ya_450", "DST", {"yards_allowed": 450}, -5.0),
    ("dst_ya_499", "DST", {"yards_allowed": 499}, -5.0),
    ("dst_ya_500", "DST", {"yards_allowed": 500}, -6.0),
    ("dst_ya_549", "DST", {"yards_allowed": 549}, -6.0),
    ("dst_ya_550", "DST", {"yards_allowed": 550}, -7.0),
    ("dst_ya_700", "DST", {"yards_allowed": 700}, -7.0),
    # ---- D/ST event + bracket combinations --------------------------------
    (
        "dst_dominant_shutout_full_combo",
        "DST",
        {"points_allowed": 0, "yards_allowed": 180, "sacks": 5, "def_interceptions": 2, "fumble_recoveries": 1, "def_tds": 1},
        # PA0=5 + YA(100-199)=3 + 5*1 + 2*2 + 1*2 + 1*6
        25.0,
    ),
    (
        "dst_safety_plus_brackets",
        "DST",
        {"points_allowed": 10, "yards_allowed": 250, "safeties": 1, "sacks": 2},
        # PA(7-13)=3 + YA(200-299)=2 + safety 2 + 2 sacks
        9.0,
    ),
    (
        "dst_exotics_in_both_zero_bands",
        "DST",
        {"points_allowed": 21, "yards_allowed": 320, "one_point_safeties": 1, "two_point_returns": 1},
        # PA(18-27)=0 + YA(300-349)=0 + 1pt safety 1 + 2pt return 2
        3.0,
    ),
    (
        "dst_blowout_loss_is_deeply_negative",
        "DST",
        {"points_allowed": 49, "yards_allowed": 560},
        # PA(46+)=-5 + YA(550+)=-7
        -12.0,
    ),
    ("dst_only_points_allowed_yards_absent", "DST", {"points_allowed": 3}, 4.0),
    ("dst_empty_scores_zero", "DST", {}, 0.0),
]


@pytest.mark.parametrize(
    "position,stats,expected",
    [c[1:] for c in GOLDEN_CASES],
    ids=[c[0] for c in GOLDEN_CASES],
)
def test_golden_master(position, stats, expected):
    assert score(position, stats) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Layer 3: targeted semantics that a value-table alone won't pin down.
# ---------------------------------------------------------------------------
def test_none_and_nan_stat_values_count_as_zero():
    # nflverse rows contain NaN; projections may carry None.
    line = {"receptions": None, "receiving_yards": 30, "rushing_yards": float("nan")}
    assert score("WR", line) == pytest.approx(3.0)


def test_kicker_none_and_nan_distances_are_skipped():
    line = {"fg_made_distances": [45, None, float("nan")], "pat_made": None}
    assert score("K", line) == pytest.approx(4.0)  # only the 45-yd FG counts


def test_position_dispatch_is_case_insensitive():
    assert score("rb", {"rushing_yards": 10}) == pytest.approx(1.0)


def test_dst_and_dst_slash_aliases_agree():
    line = {"points_allowed": 0, "sacks": 3}
    assert score("dst", line) == pytest.approx(score("D/ST", line)) == pytest.approx(8.0)


def test_absent_bracket_is_skipped_not_scored_as_a_zero_value():
    # The dangerous case: absent points_allowed must NOT award the 0-allowed +5.
    # A real shutout is points_allowed==0 (present); no data is no points.
    assert score("DST", {}) == pytest.approx(0.0)
    assert score("DST", {"points_allowed": 0}) == pytest.approx(5.0)
    assert score("DST", {"yards_allowed": 50}) == pytest.approx(5.0)
    # yards absent here: score is the PA bracket ALONE, not PA + a phantom YA.
    assert score("DST", {"points_allowed": 7}) == pytest.approx(3.0)


def test_missed_pat_key_is_accepted_but_ignored():
    assert score("K", {"pat_missed": 3}) == pytest.approx(0.0)


def test_unknown_position_raises():
    with pytest.raises(ValueError):
        score("XX", {})


def test_rules_are_swappable_without_touching_callers():
    # A different ScoringRules re-prices everything through the same callers.
    half_ppr = ScoringRules(points_per_reception=0.5)
    assert score("WR", {"receptions": 4}, half_ppr) == pytest.approx(2.0)
    assert HOUSE_RULES.points_per_reception == 1.0  # full PPR is league ground truth


def test_house_rules_is_frozen_and_hashable():
    # Frozen so it is a safe default arg / cache key; the bracket tables are tuples.
    with pytest.raises(Exception):
        HOUSE_RULES.points_per_reception = 0.5  # type: ignore[misc]
    assert hash(HOUSE_RULES)  # hashable => usable as a memoization key


def test_bracket_partitions_are_well_formed():
    # Guards the implicit-zero reconstruction against silent corruption: bounds
    # strictly ascend (no duplicate/overlapping bands); PA/YA points are
    # monotonically non-increasing as the allowed value rises and are
    # inf-terminated; FG points are non-decreasing as distance rises.
    for brackets, worse_as_value_rises, terminated in (
        (HOUSE_RULES.points_allowed_brackets, True, True),
        (HOUSE_RULES.yards_allowed_brackets, True, True),
        (HOUSE_RULES.fg_distance_brackets, False, False),
    ):
        uppers = [u for u, _ in brackets]
        points = [p for _, p in brackets]
        assert uppers == sorted(uppers), "bounds must ascend"
        assert len(set(uppers)) == len(uppers), "no duplicate/overlapping upper bounds"
        if terminated:
            assert uppers[-1] == math.inf, "final band must catch all remaining values"
        if worse_as_value_rises:
            assert all(points[i] >= points[i + 1] for i in range(len(points) - 1))
        else:
            assert all(points[i] <= points[i + 1] for i in range(len(points) - 1))
    # Every integer through the last finite PA/YA edge maps to exactly the band
    # the ascending scan selects (not merely "some float").
    for brackets in (HOUSE_RULES.points_allowed_brackets, HOUSE_RULES.yards_allowed_brackets):
        last_finite = int([u for u, _ in brackets][-2])
        for v in range(0, last_finite + 5):
            expected = next(p for upper, p in brackets if v <= upper)
            assert _bracket_points(float(v), brackets) == expected


def _parse_band(label: str) -> tuple[int, float]:
    """Extract inclusive (low, high) integer bounds from an ESPN bracket label.

    Handles the label vocabulary present in the fixture: "0 points allowed",
    "1-6 points allowed", "46+ points allowed", "Less than 100 total yards
    allowed", "100-199 ...", "550+ ...". High bound may be math.inf.
    """
    text = label.lower().strip()
    if text.startswith("less than"):
        return (0, int(re.search(r"less than (\d+)", text).group(1)) - 1)
    m = re.match(r"(\d+)\+", text)
    if m:
        return (int(m.group(1)), math.inf)
    m = re.match(r"(\d+)-(\d+)", text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"(\d+)\b", text)
    if m:
        return (int(m.group(1)), int(m.group(1)))
    raise ValueError(f"unparsed band label: {label!r}")


def test_bracket_boundaries_match_espn_fixture_labels():
    """Derive each PA/YA boundary from the ESPN label (ground truth) and assert
    the engine's partition reproduces it — so boundaries are locked to ESPN, not
    just to hand-transcribed golden cases drawn from the same source."""
    fixture = json.loads(FIXTURE.read_text())
    checks = 0
    for it in fixture:
        abbr = it["abbr"]
        if abbr.startswith("PA") and abbr[2:].isdigit():
            brackets = HOUSE_RULES.points_allowed_brackets
        elif abbr.startswith("YA"):
            brackets = HOUSE_RULES.yards_allowed_brackets
        else:
            continue
        lo, hi = _parse_band(it["label"])
        pts = it["points"]
        assert _bracket_points(float(lo), brackets) == pytest.approx(pts), f"{abbr} low edge"
        hi_probe = lo + 1000 if hi == math.inf else hi
        assert _bracket_points(float(hi_probe), brackets) == pytest.approx(pts), f"{abbr} high edge"
        if lo >= 1:  # the value one below the low edge belongs to a different band
            assert _bracket_points(float(lo - 1), brackets) != pytest.approx(pts), f"{abbr} below-edge"
        checks += 1
    assert checks == 15  # 7 points-allowed bands + 8 yards-allowed bands


def test_nan_or_none_bracket_input_never_awards_phantom_shutout():
    # The central skip-absent invariant: a NaN/None/absent bracket key must NOT
    # be coerced to 0 and awarded the 0-allowed (+5) / <100-yd (+5) bonus.
    assert score("DST", {"points_allowed": float("nan")}) == pytest.approx(0.0)
    assert score("DST", {"yards_allowed": float("nan")}) == pytest.approx(0.0)
    assert score("DST", {"points_allowed": None, "yards_allowed": None}) == pytest.approx(0.0)
    # NaN in a NON-float carrier (numpy float32/Decimal) must also read as absent.
    assert score("DST", {"points_allowed": Decimal("nan"), "yards_allowed": Decimal("nan")}) == pytest.approx(0.0)
    # NaN alongside real events scores only the events — no phantom +5 injected.
    assert score("DST", {"points_allowed": float("nan"), "sacks": 3}) == pytest.approx(3.0)


def test_rules_swap_applies_to_kicker_and_dst_too():
    # Swappability must hold for all three families, not just offense.
    assert score("K", {"fg_missed": 1}, ScoringRules(points_per_missed_fg=-2.0)) == pytest.approx(-2.0)
    # A swapped FG bracket table re-prices BOTH the count form and the distance
    # form identically (proving they share one pricing path).
    flat_fg = ScoringRules(fg_distance_brackets=((math.inf, 10.0),))
    assert score("K", {"fg_made_0_39": 1}, flat_fg) == pytest.approx(10.0)
    assert score("K", {"fg_made_distances": [55]}, flat_fg) == pytest.approx(10.0)
    # Swapped PA bracket + a swapped event weight both take effect.
    d_rules = ScoringRules(points_allowed_brackets=((math.inf, 1.0),), points_per_sack=5.0)
    assert score("DST", {"points_allowed": 0, "sacks": 2}, d_rules) == pytest.approx(1.0 + 10.0)


def test_kicker_zero_and_negative_distances_are_dropped():
    assert score("K", {"fg_made_distances": [0, -5, 45]}) == pytest.approx(4.0)  # only the 45 counts


def test_kicker_scalar_or_string_distances_do_not_crash_or_misscore():
    # A pandas missing list-cell arrives as a scalar NaN, not a list — must not crash.
    assert score("K", {"fg_made_distances": float("nan")}) == pytest.approx(0.0)
    assert score("K", {"fg_made_distances": 45}) == pytest.approx(0.0)  # scalar, not a sequence
    assert score("K", {"fg_made_distances": "45"}) == pytest.approx(0.0)  # not iterated as chars
    assert score("K", {"fg_made_distances": (39, 55)}) == pytest.approx(3.0 + 5.0)  # any iterable ok


def test_component_and_alias_fumbles_agree():
    # nflverse components sum to the same penalty as a pre-summed projection alias.
    components = {"rushing_fumbles_lost": 1, "receiving_fumbles_lost": 1, "sack_fumbles_lost": 1}
    assert score("RB", components) == pytest.approx(score("RB", {"fumbles_lost": 3})) == pytest.approx(-6.0)


def test_def_and_pk_position_aliases_route_correctly():
    assert score("DEF", {"points_allowed": 0}) == pytest.approx(score("DST", {"points_allowed": 0})) == 5.0
    assert score("PK", {"pat_made": 3}) == pytest.approx(score("K", {"pat_made": 3})) == 3.0


def test_score_dst_direct_matches_dispatch():
    line = {"points_allowed": 13, "yards_allowed": 410, "sacks": 4}
    assert score_dst(line) == pytest.approx(score("DST", line))
