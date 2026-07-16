"""House-rules scoring engine — THE single source of truth (SPEC Feature 2).

League: 10-team full PPR with custom quirks that are the core edge:
D/ST yards-allowed brackets **in addition to** points-allowed brackets, and
distance-based kicker scoring with −1 per missed kick. No other module may
hard-code a scoring value; everything (valuation, draft, streaming, lineup,
backtests) prices through this module.

Phase-0 status: golden-master harness + offense skeleton.
==> Every numeric value in ScoringRules is a PLACEHOLDER. <==
Item 1.3 replaces them with values transcribed from the real league settings
pulled in spike 1.1, and adds the D/ST bracket and kicker rules (which raise
NotImplementedError until then, deliberately). Post-Week-1, golden tests get
validated against actual ESPN box scores.

Conventions:
  * Stat lines are plain mappings (actual or projected interchangeably), with
    keys following nflverse/nfl_data_py naming so ingested rows score directly.
  * Missing keys count as zero. Unknown keys are ignored — real stat rows carry
    many non-scoring columns (attempts, completions, snap counts, ...).
  * None/NaN stat values count as zero (nflverse rows contain NaN).
  * Returns raw float league points; display layers do any rounding.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

Number = float | int


@dataclass(frozen=True)
class ScoringRules:
    """Numeric scoring parameters. Defaults are PLACEHOLDERS pending item 1.3."""

    points_per_passing_yard: float = 0.04
    points_per_passing_td: float = 4.0
    points_per_interception_thrown: float = -2.0
    points_per_rushing_yard: float = 0.1
    points_per_rushing_td: float = 6.0
    points_per_reception: float = 1.0  # full PPR — SPEC ground truth
    points_per_receiving_yard: float = 0.1
    points_per_receiving_td: float = 6.0
    points_per_fumble_lost: float = -2.0
    # 1.3 adds: two-point conversions, return TDs, bonuses (if any), the full
    # D/ST points-allowed + yards-allowed bracket tables, kicker distance
    # tiers and the miss penalty — transcribed from the league settings page.


PLACEHOLDER_RULES = ScoringRules()

# stat key (nflverse naming) -> ScoringRules field
_OFFENSE_WEIGHTS: dict[str, str] = {
    "passing_yards": "points_per_passing_yard",
    "passing_tds": "points_per_passing_td",
    "interceptions": "points_per_interception_thrown",
    "rushing_yards": "points_per_rushing_yard",
    "rushing_tds": "points_per_rushing_td",
    "receptions": "points_per_reception",
    "receiving_yards": "points_per_receiving_yard",
    "receiving_tds": "points_per_receiving_td",
    "fumbles_lost": "points_per_fumble_lost",
}

OFFENSE_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})


def _num(value: Number | None) -> float:
    """Coerce a stat value to float; None and NaN count as zero."""
    if value is None:
        return 0.0
    value = float(value)
    if math.isnan(value):
        return 0.0
    return value


def score_offense(stats: Mapping[str, Number | None], rules: ScoringRules = PLACEHOLDER_RULES) -> float:
    """League points for a QB/RB/WR/TE stat line."""
    return sum(
        _num(stats.get(key)) * getattr(rules, field) for key, field in _OFFENSE_WEIGHTS.items()
    )


def score_dst(stats: Mapping[str, Number | None], rules: ScoringRules = PLACEHOLDER_RULES) -> float:
    raise NotImplementedError(
        "D/ST scoring (points-allowed AND yards-allowed brackets) lands in item 1.3, "
        "transcribed from the real league settings pulled in spike 1.1"
    )


def score_kicker(stats: Mapping[str, Number | None], rules: ScoringRules = PLACEHOLDER_RULES) -> float:
    raise NotImplementedError(
        "Kicker scoring (distance tiers, -1 per missed kick) lands in item 1.3, "
        "transcribed from the real league settings pulled in spike 1.1"
    )


def score(
    position: str,
    stats: Mapping[str, Number | None],
    rules: ScoringRules = PLACEHOLDER_RULES,
) -> float:
    """League points for any stat line, dispatched by position."""
    pos = position.upper()
    if pos in OFFENSE_POSITIONS:
        return score_offense(stats, rules)
    if pos in {"DST", "D/ST"}:
        return score_dst(stats, rules)
    if pos == "K":
        return score_kicker(stats, rules)
    raise ValueError(f"unknown position {position!r}")
