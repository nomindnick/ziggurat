"""Golden-master harness for the scoring engine (item 0.2).

Table-driven: known stat line in -> hand-computed points out. Item 1.3 swaps
the PLACEHOLDER weights for the real league settings and extends these cases
(D/ST brackets, kicker distance/miss); post-Week-1 they get validated against
actual ESPN box scores.
"""

import pytest

from ziggurat.core.scoring import PLACEHOLDER_RULES, ScoringRules, score

# (label, position, stat line, hand-computed expected points under PLACEHOLDER_RULES)
GOLDEN_CASES = [
    (
        "qb_typical",
        "QB",
        {"passing_yards": 287, "passing_tds": 2, "interceptions": 1, "rushing_yards": 12},
        # 287*0.04 + 2*4 - 2 + 12*0.1 = 11.48 + 8 - 2 + 1.2
        18.68,
    ),
    (
        "rb_full_ppr_with_fumble",
        "RB",
        {
            "rushing_yards": 80,
            "rushing_tds": 1,
            "receptions": 5,
            "receiving_yards": 42,
            "fumbles_lost": 1,
        },
        # 8 + 6 + 5 + 4.2 - 2
        21.2,
    ),
    (
        "wr_receiving_line",
        "WR",
        {"receptions": 6, "receiving_yards": 88, "receiving_tds": 1},
        # 6 + 8.8 + 6
        20.8,
    ),
    ("empty_line_scores_zero", "TE", {}, 0.0),
    (
        "non_scoring_columns_ignored",
        "QB",
        {"passing_yards": 100, "attempts": 30, "completions": 22, "snap_share": 0.97},
        4.0,
    ),
]


@pytest.mark.parametrize(
    "position,stats,expected",
    [c[1:] for c in GOLDEN_CASES],
    ids=[c[0] for c in GOLDEN_CASES],
)
def test_golden_master(position, stats, expected):
    assert score(position, stats) == pytest.approx(expected)


def test_none_and_nan_stat_values_count_as_zero():
    # nflverse rows contain NaN; projections may carry None.
    line = {"receptions": None, "receiving_yards": 30, "rushing_yards": float("nan")}
    assert score("WR", line) == pytest.approx(3.0)


def test_position_dispatch_is_case_insensitive():
    assert score("rb", {"rushing_yards": 10}) == pytest.approx(1.0)


def test_dst_and_kicker_deliberately_unimplemented_until_1_3():
    # The custom house rules ARE the edge; they must come from the real league
    # settings (spike 1.1), not guesses. Until then, refusing is correct.
    with pytest.raises(NotImplementedError):
        score("DST", {"points_allowed": 13})
    with pytest.raises(NotImplementedError):
        score("D/ST", {"points_allowed": 13})
    with pytest.raises(NotImplementedError):
        score("K", {"fg_made_40_49": 2})


def test_unknown_position_raises():
    with pytest.raises(ValueError):
        score("XX", {})


def test_rules_are_swappable_for_1_3():
    # The harness must accept a different ScoringRules — 1.3 replaces the
    # placeholders with transcribed league settings without touching callers.
    half_ppr = ScoringRules(points_per_reception=0.5)
    assert score("WR", {"receptions": 4}, half_ppr) == pytest.approx(2.0)
    assert PLACEHOLDER_RULES.points_per_reception == 1.0  # full PPR is league ground truth
