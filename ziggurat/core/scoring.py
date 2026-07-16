"""House-rules scoring engine — THE single source of truth (SPEC Feature 2).

League: 10-team full PPR with custom quirks that are the core edge:
D/ST yards-allowed brackets **in addition to** points-allowed brackets, and
distance-based kicker scoring with −1 per missed field goal. No other module may
hard-code a scoring value; everything (valuation, draft, streaming, lineup,
backtests) prices through this module.

Every numeric value in `ScoringRules` is transcribed from the real ESPN league
settings pulled in spike 1.1 and is guarded, value-for-value, against the
captured fixture `tests/fixtures/espn/scoring_format.json` by
`tests/test_scoring.py::test_scoring_matches_espn_fixture`. Change a number here
and that test fails unless the fixture changed too — transcription drift cannot
pass silently.

    ┌─────────────────────────────────────────────────────────────────────┐
    │ TODO(post-Week-1 validation, anchored by item 1.3): once real ESPN   │
    │ box scores exist (Phase 3, item 3.8), reconcile this engine against   │
    │ actual weekly D/ST and kicker point totals. Two definitional          │
    │ subtleties to pin against ground truth then, because they are decided │
    │ at the *ingestion* layer, not here (see score_dst):                   │
    │   1. `points_allowed` / `yards_allowed` derivation — exactly which    │
    │      opponent points/yards ESPN charges to a fantasy D/ST (e.g. does  │
    │      a defensive TD the opponent scores against *our* offense count   │
    │      as points we allowed? ESPN: no). This module only *brackets* the │
    │      already-derived value.                                           │
    │   2. Return-TD attribution — ESPN credits kick/punt-return TDs to the │
    │      D/ST here (`def_tds`); confirm the box-score feed agrees and that │
    │      individual returners are not also being credited (double count). │
    └─────────────────────────────────────────────────────────────────────┘

Conventions:
  * Stat lines are plain mappings (actual or projected interchangeably), with
    keys following nflverse/nfl_data_py naming so ingested rows score directly.
    Where nflverse splits a stat into components (lost fumbles → sack_/rushing_/
    receiving_fumbles_lost), each component key is scored; pre-summed projection
    aliases (fumbles_lost) are accepted too — supply one representation, not both.
  * Missing *linear* keys count as zero. Unknown keys are ignored — real stat
    rows carry many non-scoring columns (attempts, completions, snap counts).
  * None/NaN stat values count as zero (nflverse rows contain NaN).
  * **Bracket** inputs (`points_allowed`, `yards_allowed`) are the exception to
    "missing = zero": an *absent* bracket key is skipped entirely, NOT treated
    as a value of 0. Absent means "no data," not "a shutout / zero yards" — the
    latter would award a phantom +5. Present-with-0 (a real shutout) scores +5.
  * Returns raw float league points; display layers do any rounding.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

Number = float | int

# A bracket table maps an observed value to points via ascending inclusive
# upper bounds: the first (upper, points) pair whose `upper` is >= the value
# wins. The final bound is math.inf so every value resolves. Implicit-zero
# bands (ESPN lists only non-zero brackets) are encoded EXPLICITLY here so the
# partition is complete and contiguous — never assume the listed rows abut.
Bracket = tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ScoringRules:
    """Numeric scoring parameters — the real ESPN league settings (spike 1.1).

    Guarded value-for-value against the committed ESPN fixture; see module
    docstring. Frozen + hashable so it can be a default arg and cache key.
    """

    # ---- Offense (full PPR) --------------------------------------------
    points_per_passing_yard: float = 0.04  # 1 pt / 25 yds
    points_per_passing_td: float = 4.0
    points_per_interception_thrown: float = -2.0
    points_per_rushing_yard: float = 0.1
    points_per_rushing_td: float = 6.0
    points_per_reception: float = 1.0  # full PPR — SPEC ground truth
    points_per_receiving_yard: float = 0.1
    points_per_receiving_td: float = 6.0
    points_per_fumble_lost: float = -2.0
    points_per_two_point_conversion: float = 2.0  # pass / rush / rec, all +2

    # ---- Kicker (distance-based, −1 per miss) --------------------------
    # FG points by kick distance (yards), ascending inclusive upper bounds.
    fg_distance_brackets: Bracket = (
        (39.0, 3.0),   # 0–39
        (49.0, 4.0),   # 40–49
        (59.0, 5.0),   # 50–59
        (math.inf, 6.0),  # 60+
    )
    points_per_pat_made: float = 1.0
    points_per_missed_fg: float = -1.0
    # NOTE: this league has NO missed-PAT penalty stat — a missed XP scores 0.

    # ---- D/ST events (each) --------------------------------------------
    points_per_sack: float = 1.0
    points_per_def_interception: float = 2.0
    points_per_fumble_recovery: float = 2.0
    points_per_safety: float = 2.0
    points_per_blocked_kick: float = 2.0
    points_per_def_td: float = 6.0  # all defensive + special-teams return TDs
    points_per_one_point_safety: float = 1.0   # exotic; ~never fires
    points_per_two_point_return: float = 2.0   # exotic; ~never fires

    # ---- D/ST points-allowed brackets (implicit-zero band encoded) -----
    points_allowed_brackets: Bracket = (
        (0.0, 5.0),      # 0
        (6.0, 4.0),      # 1–6
        (13.0, 3.0),     # 7–13
        (17.0, 1.0),     # 14–17
        (27.0, 0.0),     # 18–27  (implicit zero; ESPN omits it)
        (34.0, -1.0),    # 28–34
        (45.0, -3.0),    # 35–45
        (math.inf, -5.0),  # 46+
    )

    # ---- D/ST yards-allowed brackets (the distinctive house rule) ------
    yards_allowed_brackets: Bracket = (
        (99.0, 5.0),     # < 100
        (199.0, 3.0),    # 100–199
        (299.0, 2.0),    # 200–299
        (349.0, 0.0),    # 300–349  (implicit zero; ESPN omits it)
        (399.0, -1.0),   # 350–399
        (449.0, -3.0),   # 400–449
        (499.0, -5.0),   # 450–499
        (549.0, -6.0),   # 500–549
        (math.inf, -7.0),  # 550+
    )


HOUSE_RULES = ScoringRules()

OFFENSE_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})
# "DEF" is Sleeper's team-defense tag (the Phase-4 backtest ownership proxy);
# "PK" is a common placekicker label. Accepted so consumers don't each re-map.
DST_POSITIONS = frozenset({"DST", "D/ST", "DEF"})
KICKER_POSITIONS = frozenset({"K", "PK"})

# stat key (nflverse naming) -> ScoringRules field, for the linear scorers.
_OFFENSE_WEIGHTS: dict[str, str] = {
    "passing_yards": "points_per_passing_yard",
    "passing_tds": "points_per_passing_td",
    "interceptions": "points_per_interception_thrown",
    "rushing_yards": "points_per_rushing_yard",
    "rushing_tds": "points_per_rushing_td",
    "receptions": "points_per_reception",
    "receiving_yards": "points_per_receiving_yard",
    "receiving_tds": "points_per_receiving_td",
    # nflverse weekly data splits lost fumbles into three component columns and
    # has NO combined `fumbles_lost` — so score each component. `fumbles_lost`
    # is also accepted as a pre-summed alias for projection sources that carry
    # one number; a caller must supply ONE representation, never both (they add).
    "sack_fumbles_lost": "points_per_fumble_lost",
    "rushing_fumbles_lost": "points_per_fumble_lost",
    "receiving_fumbles_lost": "points_per_fumble_lost",
    "fumbles_lost": "points_per_fumble_lost",
    "passing_2pt_conversions": "points_per_two_point_conversion",
    "rushing_2pt_conversions": "points_per_two_point_conversion",
    "receiving_2pt_conversions": "points_per_two_point_conversion",
}

_DST_EVENT_WEIGHTS: dict[str, str] = {
    "sacks": "points_per_sack",
    "def_interceptions": "points_per_def_interception",
    "fumble_recoveries": "points_per_fumble_recovery",
    "safeties": "points_per_safety",
    "blocked_kicks": "points_per_blocked_kick",
    "def_tds": "points_per_def_td",
    "one_point_safeties": "points_per_one_point_safety",
    "two_point_returns": "points_per_two_point_return",
}

# Bucketed made-FG count keys -> a representative in-band distance. Both the
# count form and the distance form price through the SAME _bracket_points path,
# so there is no positional coupling to fg_distance_brackets' length or order:
# a swapped bracket table re-prices both forms, and neither IndexErrors nor
# silently drops a bucket.
_FG_COUNT_KEY_DISTANCES: dict[str, float] = {
    "fg_made_0_39": 20.0,
    "fg_made_40_49": 45.0,
    "fg_made_50_59": 55.0,
    "fg_made_60": 65.0,
}


def _num(value: Number | None) -> float:
    """Coerce a stat value to float; None and NaN count as zero."""
    if value is None:
        return 0.0
    value = float(value)
    if math.isnan(value):
        return 0.0
    return value


def _is_present(value: Number | None) -> bool:
    """True if a bracket input carries real data (not absent / None / NaN).

    Uses the SAME float() coercion as `_num`, so a NaN in any carrier (numpy
    float32/float16, Decimal('nan'), ...) — not just a Python float — is treated
    as absent. Otherwise a present-but-NaN key would pass this guard, get zeroed
    by `_num`, and wrongly earn the 0-allowed / <100-yard bonus (the phantom
    shutout the skip-absent design exists to prevent). A non-numeric value is
    also treated as absent rather than crashing the scorer.
    """
    if value is None:
        return False
    try:
        return not math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def _bracket_points(value: float, brackets: Bracket) -> float:
    """Points for `value` under an ascending inclusive-upper-bound partition."""
    for upper, points in brackets:
        if value <= upper:
            return points
    return brackets[-1][1]  # unreachable: the final bound is math.inf


def _linear(stats: Mapping[str, Number | None], weights: Mapping[str, str], rules: ScoringRules) -> float:
    return sum(_num(stats.get(key)) * getattr(rules, field_) for key, field_ in weights.items())


def score_offense(stats: Mapping[str, Number | None], rules: ScoringRules = HOUSE_RULES) -> float:
    """League points for a QB/RB/WR/TE stat line (full PPR, incl. 2-pt)."""
    return _linear(stats, _OFFENSE_WEIGHTS, rules)


def score_kicker(stats: Mapping[str, Number | None], rules: ScoringRules = HOUSE_RULES) -> float:
    """League points for a kicker: distance-tiered FGs, PATs, −1 per missed FG.

    Made FGs may be supplied two ways (additive — a caller uses one):
      * bucketed counts (ESPN abbrs FG0/FG40/FG50/FG60): `fg_made_0_39`,
        `fg_made_40_49`, `fg_made_50_59`, `fg_made_60`; or
      * a sequence of made distances in yards under `fg_made_distances`, each
        bucketed by the same distance table (natural for nflverse actuals).
    `pat_made` (ESPN PAT) scores +1 each; `fg_missed` (ESPN FGM) −1 each. A
    missed PAT scores 0 — this league has no missed-PAT penalty stat — so
    `pat_missed` is accepted but ignored. A non-sequence / NaN / None
    `fg_made_distances` (e.g. a pandas missing-cell) contributes nothing rather
    than crashing, consistent with the module's None/NaN-safety contract.
    """
    points = 0.0

    # Made FGs as bucketed counts — priced through the same partition as raw
    # distances (via a representative in-band distance), so the two forms can
    # never disagree and neither couples to the bracket table's length/order.
    for key, rep_distance in _FG_COUNT_KEY_DISTANCES.items():
        points += _num(stats.get(key)) * _bracket_points(rep_distance, rules.fg_distance_brackets)

    # Made FGs as raw distances — bucket each. Accept any non-str iterable
    # (list/tuple/ndarray/Series); a scalar or NaN is not iterable and scores 0.
    distances = stats.get("fg_made_distances")
    if distances is not None and not isinstance(distances, (str, bytes)):
        try:
            sequence = list(distances)
        except TypeError:
            sequence = []  # scalar / non-iterable (e.g. a stray NaN) → no FGs
        for raw in sequence:
            if raw is None:
                continue
            dist = float(raw)
            if math.isnan(dist) or dist <= 0:
                continue  # not a real made-FG distance
            points += _bracket_points(dist, rules.fg_distance_brackets)

    points += _num(stats.get("pat_made")) * rules.points_per_pat_made
    points += _num(stats.get("fg_missed")) * rules.points_per_missed_fg
    # pat_missed intentionally ignored — no missed-PAT penalty in this league.
    return points


def score_dst(stats: Mapping[str, Number | None], rules: ScoringRules = HOUSE_RULES) -> float:
    """League points for a D/ST: events + defensive/return TDs + BOTH the
    points-allowed and yards-allowed bracket systems (the house edge).

    `points_allowed` and `yards_allowed` are the observed team-defense values
    (their *derivation* from a box score lives in the ingestion layer — see the
    module TODO). An absent bracket key is skipped, not scored as 0.
    """
    points = _linear(stats, _DST_EVENT_WEIGHTS, rules)

    pa = stats.get("points_allowed")
    if _is_present(pa):
        points += _bracket_points(_num(pa), rules.points_allowed_brackets)

    ya = stats.get("yards_allowed")
    if _is_present(ya):
        points += _bracket_points(_num(ya), rules.yards_allowed_brackets)

    return points


def score(
    position: str,
    stats: Mapping[str, Number | None],
    rules: ScoringRules = HOUSE_RULES,
) -> float:
    """League points for any stat line, dispatched by position (case-insensitive)."""
    pos = position.upper()
    if pos in OFFENSE_POSITIONS:
        return score_offense(stats, rules)
    if pos in DST_POSITIONS:
        return score_dst(stats, rules)
    if pos in KICKER_POSITIONS:
        return score_kicker(stats, rules)
    raise ValueError(f"unknown position {position!r}")
