"""Draft backtest: grade the pick engine on what ACTUALLY HAPPENED, 2021-2025.

THE POINT OF THIS MODULE IS TO BE ABLE TO BE WRONG.

Every margin this project has ever claimed for the draft engine was SELF-GRADED:
item 2.2's "+128", item 2.3's "+135 to +197", Phase 1's "+1.283 expected wins"
are all the engine's own projections, scored by the engine's own board, in a
simulated room. If the projections are biased, every one of those numbers is
biased the same way on both sides of the comparison and the bias is invisible.

This module replaces the projections with the seasons. It builds a PRESEASON
consensus draft board for each of 2021-2025 from the immutable FantasyPros ECR
archive (``ziggurat/data/nfl/fpecr.py``, migration 011), runs a full ten-team,
sixteen-round snake draft with the engine in the operator's 9-of-10 seat against
the calibrated room, and then grades all ten drafted rosters on REALIZED weekly
house points — real byes, real injuries, real busts. A player who tore an ACL in
week 3 scores exactly what he actually scored.

--------------------------------------------------------------------------
WHAT THIS CAN MEASURE, AND WHAT IT STRUCTURALLY CANNOT
--------------------------------------------------------------------------
It measures ROSTER CONSTRUCTION: positional allocation, when to take a position
relative to when the room takes it, bye structure, and the K/DST divergence
play. It CANNOT measure player evaluation, and that is not a choice — no free
historical point-in-time projected STAT LINE exists for 2021-2025 (item 1.5
established that and the operator confirmed it; the `projections` table holds
season 2026 and nothing else). So the board's per-player value is a function of
the player's PRESEASON CONSENSUS POSITIONAL RANK and nothing else, and within a
position every strategy here agrees on the ordering. The engine's edge, if it
has one, must come from the cross-position decisions. That is a narrower claim
than "the engine drafts better", and it is the claim the data can support.

Three further limitations, stated because each one bends the conclusion:

1. THE ROOM IS A 2026 MODEL PLAYED AGAINST 2021-2025 SEATS. ``ROOM_PRIORS_2025``
   was fitted to one ESPN draft (2025) in this specific office league. Applying
   it to 2021 assumes rooms drafted the same way five years ago. It affects both
   arms identically — the baselines face the same rivals — so it biases the
   MARGIN only through interaction (an engine tuned to exploit a particular room
   would look better against that room). It is a real limit on generalisation
   and not a bias in favour of either arm.

2. THE SCORING RULES ARE 2026'S. ``core/scoring.py`` holds this league's current
   settings, applied to 2021-2025 box scores. The league did not exist before
   2025, so there are no historical house rules to use instead. The
   distinctive house rules (distance kicker with -1/miss, D/ST on BOTH points-
   and yards-allowed brackets) are applied uniformly to all five seasons and to
   both arms. It changes what "a good roster" is; it does not favour a strategy.

3. FANTASYPROS ECR IS NOT THE BOARD THE ROOM DRAFTED OFF. Real rooms draft off
   ESPN's editorial board, which this project only has for 2026 (it is a live,
   history-free endpoint). ECR and ESPN disagree systematically — item 2.2
   measured a ~+15 editorial-vs-market offset, and Phase 1 measured that ESPN's
   ADP relationship to real room timing COLLAPSES at K and D/ST. Using ECR makes
   the modelled room MORE market-rational than a real one. Because the engine's
   signature play is exploiting the room's LATE K/DST timing, a more rational
   room is the harder test, not the easier one — this limitation, unlike the
   other two, cuts AGAINST the engine.

--------------------------------------------------------------------------
THE BOARD'S VALUE COLUMN — the one modelling choice that had to be invented
--------------------------------------------------------------------------
``BoardEntry`` needs a ``vor``. With no historical projections, value has to
come from somewhere, and every candidate leaks or begs the question. The choice
here is a LEAVE-ONE-SEASON-OUT POSITIONAL VALUE CURVE: for season s, the value
of "the consensus RB12" is the average realized house points of the consensus
RB12 in the OTHER FOUR seasons, smoothed and forced monotone.

Why this is not leakage: it carries no information about WHICH player is good in
season s. It is a statement about the SHAPE of positional scarcity, estimated
from seasons that are not the one being drafted, and it is identical for every
strategy in the comparison. Leave-one-out is what makes that true rather than
merely plausible — a curve fitted on all five seasons would be in-sample at the
aggregate level even though no player-specific fact crosses.

Two consequences worth naming out loud:

* the curve embeds AVAILABILITY. The consensus RB5 misses games; his four-season
  average realized total already prices that in. The live 2026 engine's board
  does NOT (Phase 1 measured it assuming 16.00 games for everyone). So this
  board is, in one narrow respect, better than the one the engine will use on
  Monday, and a margin measured here is not automatically transferable.
* ``house_points`` is 0.0 for EVERY entry, uniformly. There is no projected
  season total for these boards, and inventing one per player would have leaked
  the outcome through ``grader.assert_board_coverage`` (which keys on
  ``house_points > 0``): a player who missed the whole season has no realized
  weeks, so pricing him would have made him — and only him — trip that gate.
  :func:`assert_backtest_coverage` is the stricter replacement: EVERY board
  entry must be present in the realized map, priced or not.

A SENSITIVITY BOARD is also built (``curve_source="espn_2026"``): the same
machinery with the positional shape taken from the live 2026 house projections
instead of from realized history. It is contaminated by the known kicker feed
bug (``projections._KICKER_DIRECT_MAP``'s dead ``fgm_50p`` mapping understates
every kicker by ~40 season points) and by 2026's own positional fashions. It is
here so the headline can be checked for dependence on the curve, not because it
is better.

--------------------------------------------------------------------------
HOW AN EMPTY STARTING SLOT IS PRICED — the study's largest free parameter
--------------------------------------------------------------------------
A roster with one quarterback fields nothing in his bye week. Charging that as a
dead week and crediting it at a free agent's production are both defensible, and
the choice moves the headline further than any other decision in this module, so
it is a named, reported parameter (``--stream``) rather than a default.

Two things about it were WRONG in the first version of this study and are worth
stating rather than quietly fixing, because both bent a published number:

1. THE DEPTH WAS THE K/DST DEPTH APPLIED TO QB AND TE. ``grader``'s rule credits
   an empty slot at the 11th-best player of that week, on the reasoning "the best
   one still unrostered if all ten teams held exactly one". Position caps make
   that exactly right for K and D/ST (measured league-wide over 40 drafts per
   arm: 10.00 and 10.00). The room rosters QB 19.75-20.15 and TE 16.07-16.55, so
   extending the same index to them credited a bye week at 17.99 QB points —
   Patrick Mahomes's actual 2023 weekly average — instead of 10.79.
2. THE DIRECTION OF THAT ERROR WAS REPORTED BACKWARDS. The credit is paid only to
   a roster that leaves a slot EMPTY. The engine carries QB 2.75 / TE 3.00 and
   the consensus baseline QB 1.96 / TE 1.55, so an over-generous QB/TE level is a
   thumb on the scale AGAINST the engine. The first version listed it under "two
   ways the measurement flatters the engine", which is the opposite.

``--stream`` now takes: ``kdst`` (the shipped grader model), ``qb_te`` (also QB
and TE, at the measured depth), ``qb_te_blind``/``kdst_blind`` (the same slots,
but the free agent is chosen by PRESEASON CONSENSUS rank rather than by what he
turned out to score — a real Tuesday add is blind), and ``none``.

--------------------------------------------------------------------------
TWO READINGS OF EVERY INTERVAL, AND ONLY ONE OF THEM IS ABOUT NEXT SEASON
--------------------------------------------------------------------------
All ``n`` drafts of one season share ONE realized-outcome draw. The pooled
interval treats each draft as an observation and is a claim about THESE FIVE
SEASONS; the season-block interval (:func:`season_block_interval`, t on five
numbers, df = 4) treats each SEASON as an observation and is the claim about
seasons. Both are printed under every contrast. At the block level nothing in
this study excludes zero — that is the honest summary and it is not hidden.

--------------------------------------------------------------------------
THE TWO OBJECTIVES
--------------------------------------------------------------------------
``realized_wins`` (PRIMARY) — deterministic. Each week, seat the best legal
lineup from realized points, compare against each of the nine real rivals in
that draft, and score 1 / 0.5 / 0. Summed over weeks 1-14 this is expected wins
against a uniformly random opponent, computed with no model at all. Ten teams'
values sum to exactly 70.

``modelled_wins`` (SECONDARY) — ``grader.grade_roster`` on the same realized
points: the normal-CDF win model with item 3.5's variance prior. It smooths
one-point margins, which is either noise reduction or double-counted variance
depending on your taste, so both are reported and the primary is the one with no
model in it.

Everything is run through ``ziggurat/draft/evaluate.py``: the same seed grid,
the same production room construction, the same per-seat RNG streams, the same
paired statistics. No private tournament loop lives here.

Run it::

    .venv/bin/python -m backtest.draft_backtest --n 25
    .venv/bin/python -m backtest.draft_backtest --n 100 --kdst-probe
    .venv/bin/python -m backtest.draft_backtest --n 100 --stream qb_te_blind
    # and the replication check the first version of this study did not run:
    .venv/bin/python -m backtest.draft_backtest --n 100 --seed-base 500000
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from backtest import stats
from ziggurat.core import scoring
from ziggurat.core.lineup import fill_lineup
from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure, replacement_levels
from ziggurat.data.nfl import base, fpecr
from ziggurat.data.nfl.team_defense import get_team_defense
from ziggurat.data.nfl.weekly_stats import get_weekly_stats
from ziggurat.data.store import open_db
from ziggurat.draft import evaluate as ev
from ziggurat.draft import grader
from ziggurat.draft.bots import BoardEntry, FollowEspnRank, FollowVor
from ziggurat.draft.engine import PickEngine
from ziggurat.paths import REPO_ROOT

#: The five seasons the ECR archive spans continuously (spike 1.2).
SEASONS: tuple[int, ...] = (2021, 2022, 2023, 2024, 2025)

#: League shape. Weeks 1-14 head to head, 15-17 a six-team bracket.
REGULAR_SEASON_WEEKS = tuple(range(1, 15))
PLAYOFF_WEEKS = (15, 16, 17)
ALL_WEEKS = tuple(range(1, 18))

#: The operator's real seat, zero-based, matching ``run_draft``/``evaluate``.
OPERATOR_SLOT = ev.OPERATOR_SLOT_2026

#: Where the mirrored archives live. Under the gitignored top-level ``data/``
#: tree (Rule 5): these are third-party bulk pulls, never committed.
DATA_DIR = REPO_ROOT / "data"
KICKING_DIR = DATA_DIR / "backtest"
#: A PINNED, DATED mirror of the archive — `intel/research/market-archives.md`
#: asks for exactly this ("upstream git history is truncated ~Dec 2024 back, so
#: pin/mirror your own copy for durable provenance"), and a bare `db_fpecr.parquet`
#: would silently become a different file on the next re-download. There is
#: already an undated 2026-08-27 mirror beside it from the pre-draft spot checks;
#: the two differ by ~45 KB of appended rows and NOTHING ELSE, which is a third
#: independent confirmation that this archive is append-only.
FPECR_MIRROR = KICKING_DIR / "db_fpecr-2026-08-30.parquet"

#: Rolling-window half-width used to smooth the positional value curve before
#: it is forced monotone. Five seasons give at most four samples per rank, which
#: is thin at rank 30+; the window trades a little resolution for a curve that
#: is not dominated by one player's torn ACL.
_CURVE_WINDOW = 2


class BacktestInputError(ValueError):
    """The backtest was asked for something it cannot honestly compute."""


# ========================================================================
#                     1.  realized outcomes (the ground truth)
# ========================================================================
#
# READ THROUGH ``base.latest_truth``, AND WHY.
#
# These tables were bulk-loaded by `ziggurat ingest backfill` in 2026, so every
# row carries `retrieved_as_of` = the day of that backfill. The safe-default
# `historical` view gates BOTH knowledge time and retrieval time, so a read at
# `as_of="2022-01-10"` returns *nothing* — silently, reading exactly like "there
# was no football in 2021". `latest_truth` keeps the fact-time gate (a week-3 box
# score is still invisible before week 3) and drops only the retrieval gate,
# which is precisely the "deliberately accepted immutable bulk history" case
# Rule 1 names. It is also the right view for a different reason: these are
# GRADES, not decision inputs. A corrected box score should grade the decision;
# pretending we could not have seen the correction would grade the wrong thing.


def _season_end(season: int) -> str:
    """An as_of late enough that every week of ``season`` is knowable.

    February of the following calendar year: after the Super Bowl, before the
    next league year turns over in mid-March. Stated as a function rather than a
    literal so the fact-time gate is still doing real work — a week-17 line is
    invisible at a week-3 as_of even under ``latest_truth``.
    """
    return f"{season + 1}-02-28"


def _offense_points(conn, season: int) -> dict[str, dict[int, float]]:
    """gsis_id -> {week: realized house points} for QB/RB/WR/TE, REG weeks only.

    Scored with ``scoring.score_offense`` on the stored nflverse row, which is
    already in ``core/scoring.py``'s vocabulary (Rule 2: no scoring number is
    computed here). A row that exists at all means the player was ACTIVE that
    week; a week with no row means he did not play, which is the map's whole
    convention.
    """
    rows = base.latest_truth(get_weekly_stats)(
        conn, as_of=_season_end(season), season=season
    )
    out: dict[str, dict[int, float]] = {}
    for row in rows:
        if row["season_type"] != "REG":
            continue
        pid = row["player_id"]
        if pid is None:
            continue
        week = int(row["week"])
        out.setdefault(pid, {})[week] = scoring.score_offense(dict(row))
    return out


def _dst_points(conn, season: int) -> dict[str, dict[int, float]]:
    """team abbr -> {week: realized house points} for team defenses, REG only.

    ``team_defense`` (migration 003) is named column-for-column to score directly
    through ``score_dst``, including BOTH bracket systems — the house edge, and
    the reason a D/ST cannot be priced from a generic points column.
    """
    rows = base.latest_truth(get_team_defense)(
        conn, as_of=_season_end(season), season=season
    )
    out: dict[str, dict[int, float]] = {}
    for row in rows:
        if row["season_type"] != "REG":
            continue
        team = base.TEAM_ALIASES.get(str(row["team"]).upper(), str(row["team"]).upper())
        out.setdefault(team, {})[int(row["week"])] = scoring.score_dst(dict(row))
    return out


#: nflverse made-FG bucket columns -> ``core/scoring.py``'s bucket keys. The
#: house pays by DISTANCE, and nflverse splits 0-39 into three sub-buckets that
#: all price identically, so they are summed onto one key rather than given
#: three keys scoring.py does not have.
_NFLVERSE_FG_BUCKETS = {
    "fg_made_0_39": ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39"),
    "fg_made_40_49": ("fg_made_40_49",),
    "fg_made_50_59": ("fg_made_50_59",),
    "fg_made_60": ("fg_made_60_",),
}


def kicking_frame(season: int, *, cache_dir=KICKING_DIR, refresh: bool = False):
    """The weekly KICKING stat lines for one season, cached under ``data/``.

    WHY THIS IS NOT A DATABASE READ, and it is a finding rather than a shortcut:
    **the stored ``weekly_stats`` table cannot price a kicker.** Item 1.4 chose a
    column list (``weekly_stats._COLUMNS``) that carries no ``fg_made_*``, no
    ``pat_made`` and no ``fg_missed``, so a kicker's stored row is all zeroes for
    every stat the house pays him for. Re-scoring the table therefore grades EVERY
    kicker at 0.000 in EVERY week — silently, with no missing column and nothing
    to raise. In a league whose distinctive rule is a distance-based kicker with a
    -1 miss penalty, and whose draft engine's signature move is taking a kicker
    early as a divergence play, grading the K slot at zero would have quietly
    decided the headline.

    The columns DO exist upstream (nflverse's current ``player_stats`` ships the
    full distance-bucketed kicking grid); only this project's persistence layer
    drops them. So the supplement is pulled through the SAME seam the ingester
    uses (``ziggurat.data.nfl.source.import_weekly_data``) and frozen to a local
    parquet, and the fix — persisting the columns — is reported for an owner of
    ``weekly_stats.py`` rather than made here.

    Leakage: none is possible. This feeds the GRADE, never the board. See
    :func:`build_season_inputs`, which takes the board and the outcomes from
    different functions on purpose.

    THE FILTER RUNS ON EVERY PATH, INCLUDING THE CACHE, and that is a fix rather
    than a nicety. It used to run only on the branch that WRITES the parquet, so
    on every run after the first the returned frame was whatever the file held,
    unchecked. Demonstrated: a cache file containing one RB row makes
    ``_kicker_points`` return that RB with 0.0 points, and ``season_outcomes``'
    ``out.update(...)`` then REPLACES his real offensive week with zero — the
    exact failure this module documents as having made every QB/RB/WR/TE value
    curve come back identically zero with nothing raised. A stale, hand-edited or
    half-written cache is not a hypothetical: these files live under
    ``data/backtest/`` and are re-used across every run.
    """
    import pandas as pd

    os.makedirs(str(cache_dir), exist_ok=True)
    path = os.path.join(str(cache_dir), f"kicking-{season}.parquet")
    if refresh or not os.path.exists(path):
        from ziggurat.data.nfl import source as nfl

        frame = _kicker_rows_only(nfl.import_weekly_data([season]), season)
        frame.to_parquet(path, index=False)
        return frame
    return _kicker_rows_only(pd.read_parquet(path), season)


#: The columns the kicking supplement needs. Named once so the fresh-pull and
#: cached-read paths cannot drift apart.
_KICKING_COLUMNS = (
    ["player_id", "season", "week", "season_type", "position", "pat_made", "fg_missed"]
    + [c for cols in _NFLVERSE_FG_BUCKETS.values() for c in cols]
)


def _kicker_rows_only(frame, season: int):
    """Validate the columns and keep ONLY ``position == 'K'``. See
    :func:`kicking_frame` for why this must run on the cached path too."""
    missing = sorted(set(_KICKING_COLUMNS) - set(frame.columns))
    if missing:
        raise BacktestInputError(
            f"nflverse weekly stats for {season} are missing kicking columns "
            f"{missing}; the K slot cannot be graded and grading it at zero "
            "would silently decide the K/DST result. Refusing."
        )
    # KICKERS ONLY, and this filter is load-bearing rather than tidy. The frame
    # is every player's weekly line; merged whole into the outcomes map it
    # OVERWRITES every skill player's offensive points with his kicker score,
    # which is 0.0 for everyone who does not kick.
    kept = frame[frame["position"].astype(str).str.upper() == "K"]
    if kept.empty:
        raise BacktestInputError(
            f"nflverse weekly stats for {season} contain no position=='K' rows; "
            "the kicker grade would be empty."
        )
    return kept[_KICKING_COLUMNS]


def _kicker_points(season: int, **kwargs) -> dict[str, dict[int, float]]:
    """gsis_id -> {week: realized house points} for kickers, REG weeks only."""
    frame = kicking_frame(season, **kwargs)
    out: dict[str, dict[int, float]] = {}
    for row in frame.to_dict("records"):
        if row.get("season_type") != "REG":
            continue
        pid = row.get("player_id")
        if pid is None or (isinstance(pid, float) and math.isnan(pid)):
            continue
        stats = {
            key: sum(float(row.get(c) or 0.0) for c in cols)
            for key, cols in _NFLVERSE_FG_BUCKETS.items()
        }
        stats["pat_made"] = float(row.get("pat_made") or 0.0)
        # nflverse counts a BLOCKED field goal separately from a missed one; the
        # house charges -1 per missed FG (ESPN "FGM"). Only `fg_missed` is
        # charged here, so a blocked kick is worth 0 rather than -1 — a small,
        # stated understatement of the penalty rather than a guess at ESPN's
        # blocked-kick convention, which is not in the settings fixture.
        stats["fg_missed"] = float(row.get("fg_missed") or 0.0)
        out.setdefault(str(pid), {})[int(row["week"])] = scoring.score_kicker(stats)
    return out


# ========================================================================
#                     2.  the leave-one-season-out value curve
# ========================================================================


def _pava_blocks(values: Sequence[float]) -> list[tuple[float, int]]:
    """Pool-adjacent-violators, decreasing: ``[(block value, block length), ...]``.

    The least-squares monotone fit. A value curve that goes UP with rank says the
    consensus WR30 is worth more than the WR29, which is a statement about four
    seasons of noise, not about scarcity — and VOR differences it, so the noise
    lands straight in a pick decision. PAVA is the standard fix and it is exact,
    not a smoother.
    """
    blocks: list[list[float]] = []      # each: [sum, count]
    for v in values:
        blocks.append([float(v), 1.0])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    return [(s / c, int(c)) for s, c in blocks]


def _isotonic_decreasing(values: Sequence[float]) -> list[float]:
    """PAVA as a STEP function — the plain monotone fit, kept for comparison."""
    out: list[float] = []
    for value, length in _pava_blocks(values):
        out.extend([value] * length)
    return out


def _monotone_curve(values: Sequence[float]) -> list[float]:
    """PAVA, then linear interpolation THROUGH the block centres.

    WHY NOT THE PLAIN STEP FUNCTION. PAVA pools adjacent ranks whose sample means
    are out of order, and with at most four samples per rank that pooling is
    large exactly where the noise is: on the shipped 2023 fit it made the
    consensus RB1 through RB5 one block with an IDENTICAL value. Two things then
    break that are artefacts of the estimator, not statements about football:
    ``FollowVor`` drains a whole position before moving on (RB1..RB5 tie, so RB
    "wins" five picks in a row against a WR1 worth a point less), and an exact
    cross-position tie makes ``bots.best_by_vor`` depend on set iteration order.

    Interpolating between block centres keeps the fit's values where PAVA put
    them — the centre of each block still carries the block's mean — while
    restoring a strict ordering the data cannot resolve but the market can.

    CHOSEN ON HELD-OUT FIT, BEFORE ANY STRATEGY WAS RUN, so this is not a knob
    turned until a result appeared. Predicting each season's realized totals from
    a curve trained on the other four (all 2,730 board rows, five folds):

        position mean (no rank signal)   MAE 64.58   RMSE 80.87
        log-log power fit                MAE 78.16   RMSE 258.64   <- blows up at k=1
        PAVA step function               MAE 38.61   RMSE  51.98
        PAVA + block interpolation       MAE 38.45   RMSE  51.77   <- shipped

    The smoothing window barely matters (MAE 38.53-38.61 for half-widths 0, 1, 2,
    4, 8), so ``_CURVE_WINDOW`` is not doing hidden work either.
    """
    blocks = _pava_blocks(values)
    centres: list[tuple[float, float]] = []
    start = 0
    for value, length in blocks:
        centres.append((start + (length - 1) / 2.0, value))
        start += length

    out: list[float] = []
    for i in range(len(values)):
        if i <= centres[0][0]:
            out.append(centres[0][1])
            continue
        if i >= centres[-1][0]:
            out.append(centres[-1][1])
            continue
        for j in range(len(centres) - 1):
            x0, y0 = centres[j]
            x1, y1 = centres[j + 1]
            if x0 <= i <= x1:
                out.append(y0 + (y1 - y0) * (i - x0) / (x1 - x0))
                break
    return out


@dataclass(frozen=True)
class ValueCurve:
    """Expected realized season points by (position, preseason positional rank).

    ``source`` labels how it was estimated and ``training_seasons`` says which
    seasons it saw — both ride into the run's reasons, because a curve is a
    modelling choice and Rule 6 says a number a human reads carries its basis.
    """

    by_position: Mapping[str, tuple[float, ...]]
    source: str
    training_seasons: tuple[int, ...]

    def value(self, position: str, pos_rank: int) -> float:
        """Points for the consensus ``position``-``pos_rank``. Deep ranks flatten
        onto the last estimated value rather than extrapolating to nonsense."""
        curve = self.by_position.get(position)
        if not curve:
            return 0.0
        return curve[min(max(int(pos_rank), 1), len(curve)) - 1]


def _season_totals(
    outcomes: Mapping[str, Mapping[int, float]], key: str, weeks: Sequence[int]
) -> float:
    row = outcomes.get(key)
    if not row:
        return 0.0
    return sum(p for w, p in row.items() if w in set(weeks))


def realized_value_curve(
    boards: Mapping[int, Sequence["BoardRow"]],
    outcomes: Mapping[int, Mapping[str, Mapping[int, float]]],
    *,
    training_seasons: Sequence[int],
    weeks: Sequence[int] = ALL_WEEKS,
    window: int = _CURVE_WINDOW,
) -> ValueCurve:
    """Fit the positional value curve on ``training_seasons`` only.

    For each position and each preseason positional rank k, average the realized
    season totals of the player ranked k in each training season, smooth with a
    centred window, then force the result monotone decreasing. Ranks deeper than
    the shortest training board still get a value (the average over whichever
    seasons reached that deep), which is why the smoothing runs before PAVA: the
    tail is thinner and noisier, and PAVA would otherwise lock in a step.
    """
    samples: dict[str, dict[int, list[float]]] = {}
    for season in training_seasons:
        board = boards[season]
        out = outcomes[season]
        for row in board:
            samples.setdefault(row.position, {}).setdefault(row.pos_rank, []).append(
                _season_totals(out, row.outcome_key, weeks)
            )
    by_position: dict[str, tuple[float, ...]] = {}
    for position, ranks in samples.items():
        depth = max(ranks)
        # A RANK NO TRAINING BOARD REACHED RAISES rather than entering the curve
        # as 0.0. The old code substituted a hard zero, which is not a small
        # number for a season total — the smoothing window then spread it over
        # two ranks either side and PAVA pooled the dent outward. It can only
        # happen if a board's pos_rank series has a hole, which
        # `preseason_board_rows` now closes; refusing is how that stays true
        # instead of silently degrading if a future caller supplies raw ranks.
        holes = [k for k in range(1, depth + 1) if k not in ranks]
        if holes:
            raise BacktestInputError(
                f"the {position} value curve has no sample at rank(s) {holes[:8]} "
                f"(depth {depth}); a hole in a value curve is a dent every VOR "
                "difference below it inherits. Board pos_rank must be contiguous — "
                "see preseason_board_rows."
            )
        raw = [statistics.fmean(ranks[k]) for k in range(1, depth + 1)]
        smoothed = [
            statistics.fmean(raw[max(0, i - window): i + window + 1])
            for i in range(len(raw))
        ]
        by_position[position] = tuple(_monotone_curve(smoothed))
    return ValueCurve(
        by_position=by_position,
        source="realized_loo",
        training_seasons=tuple(sorted(training_seasons)),
    )


def espn_2026_value_curve(conn, *, as_of: str, season: int = 2026) -> ValueCurve:
    """The SENSITIVITY curve: positional shape from the live 2026 house board.

    Uses no realized outcome at all — it is what a modern projection feed says
    the consensus P-k player is worth — so it is the cross-check on whether the
    headline depends on the leave-one-out curve. It is NOT the better estimate:
    it carries the confirmed kicker feed defect (``fgm_50p`` maps to a bucket the
    Sleeper feed never ships, understating every kicker by ~25% of made FGs) and
    2026's own positional fashions, and it is applied to seasons whose positional
    economics differed.
    """
    from ziggurat.core.valuation import build_valuation

    rows = base.latest_truth(build_valuation)(conn, as_of=as_of, season=season)
    by_pos: dict[str, list[float]] = {}
    for row in rows:
        by_pos.setdefault(row.position, []).append(float(row.proj_points))
    by_position = {
        pos: tuple(_monotone_curve(sorted(pts, reverse=True)))
        for pos, pts in by_pos.items()
    }
    return ValueCurve(
        by_position=by_position, source="espn_2026", training_seasons=(season,)
    )


# ========================================================================
#                     3.  the preseason board
# ========================================================================


@dataclass(frozen=True)
class BoardRow:
    """One preseason consensus row, before it becomes a ``BoardEntry``.

    ``outcome_key`` is the key into the realized-points map: the gsis id for a
    skill player, ``"<TEAM>"`` for a defense. ``player_id`` is the draft key —
    the same string, namespaced for defenses so a two-letter team abbr can never
    collide with an id.
    """

    player_id: str
    outcome_key: str
    name: str
    position: str
    team: str | None
    board_rank: int
    pos_rank: int
    ecr: float | None
    sd: float | None


def preseason_board_rows(
    conn, *, season: int, week1_date: str, view: base.AsOfView = "latest_truth"
) -> tuple[str, tuple[BoardRow, ...], dict[str, int]]:
    """``(scrape_date, rows, coverage counts)`` — the last board before week 1.

    Reads the PRESEASON full-PPR overall cheatsheet only. The rest-of-season
    overall board carries the same ``ecr_type`` and is a different fact; keying
    the panel on ``fp_page`` is what makes asking for one of them possible (see
    the migration header).

    The board is gated at ``as_of = the day before week 1's first kickoff`` — so
    even under ``latest_truth`` no in-season scrape can reach it — and then
    filtered to scrapes strictly BEFORE that kickoff. Two gates, because they
    mean different things: ``as_of`` is what this system could know, ``before``
    is where the football calendar sits.

    A row whose FantasyPros id resolves to no gsis is DROPPED, and this is the
    one place the backtest discards a real market fact. The reason is that it
    cannot be graded: an unresolvable player has no realized points, so keeping
    him would grade him at zero, which for a player who actually played is a
    fabricated outcome, not a measurement of one. The count is returned so the
    caller can report it rather than discover it.
    """
    from datetime import date, timedelta

    # The day BEFORE the opener: a belt to the `before` brace. `as_of` gates what
    # this system could know; `before` is where the calendar sits. Either alone
    # excludes an in-season scrape; both make it structural.
    day_before = (date.fromisoformat(week1_date) - timedelta(days=1)).isoformat()
    scrape = fpecr.latest_scrape_date(
        conn, as_of=day_before, season=season,
        ecr_type=fpecr.PRESEASON_BOARD_ECR_TYPE,
        fp_page=fpecr.PRESEASON_BOARD_PAGE, before=week1_date, view=view,
    )
    if scrape is None:
        raise BacktestInputError(
            f"no preseason ECR board stored for season {season} before {week1_date}. "
            "Run backtest.draft_backtest --ingest first (it mirrors and loads the "
            "db_fpecr panel), and check that the season's schedules are ingested."
        )
    rows = fpecr.get_fpecr(
        conn, as_of=day_before, season=season,
        ecr_type=fpecr.PRESEASON_BOARD_ECR_TYPE,
        fp_page=fpecr.PRESEASON_BOARD_PAGE, scrape_date=scrape, view=view,
    )

    kept: list[BoardRow] = []
    unresolved = 0
    for row in sorted(rows, key=lambda r: r["page_rank"]):
        position = row["position"]
        team = row["team"]
        if position == "DST":
            if not team or team == "FA":
                unresolved += 1
                continue
            outcome_key = team
            player_id = f"DST:{team}"
        else:
            gsis = row["gsis_id"]
            if not gsis:
                unresolved += 1
                continue
            outcome_key = gsis
            player_id = gsis
        kept.append(
            BoardRow(
                player_id=player_id,
                outcome_key=outcome_key,
                name=row["player"],
                position=position,
                team=team,
                board_rank=int(row["page_rank"]),
                pos_rank=int(row["pos_rank"]),
                ecr=row["ecr"],
                sd=row["sd"],
            )
        )
    # BOTH RANKS must stay CONTIGUOUS after the drops, and they are used for two
    # different things, so both matter:
    #   board_rank  — the slot the room reaches into. A hole means the reach noise
    #                 is measured in slots that do not exist.
    #   pos_rank    — the key into the positional value curve. A hole means
    #                 `realized_value_curve` has a rank no training board ever
    #                 reached; before this was re-derived it injected a hard 0.0
    #                 into the raw curve at that rank, which the smoother then
    #                 spread over its neighbours. Measured gaps on the shipped
    #                 boards: RB 81/122/130 and TE 88 (2021), RB 141 (2022),
    #                 WR 181 (2023), K 20/28/30/36/39, and more — small, deep in
    #                 the tail, and entirely avoidable.
    # Both re-rankings keep the consensus order exactly: `kept` is already in
    # page_rank order, so enumerating it (globally, and again within a position)
    # reproduces the market's ordering with the drops closed up.
    per_position: dict[str, int] = {}
    renumbered: list[BoardRow] = []
    for i, row in enumerate(kept, start=1):
        per_position[row.position] = per_position.get(row.position, 0) + 1
        renumbered.append(
            dataclasses.replace(row, board_rank=i, pos_rank=per_position[row.position])
        )
    kept = renumbered
    counts = {"kept": len(kept), "unresolved_dropped": unresolved}
    return scrape, tuple(kept), counts


def board_entries(rows: Sequence[BoardRow], curve: ValueCurve) -> tuple[BoardEntry, ...]:
    """Turn consensus rows into a draftable board.

    ``espn_overall_rank`` is the consensus board rank — for 2021-2025 that IS the
    board the room sees, because ESPN's editorial board is a live endpoint with
    no history (see the module docstring's limitation 3).

    ``vor`` is the curve's value minus the replacement level computed from THIS
    board through ``valuation.replacement_levels`` — the same empirical-flex,
    superflex-guarded, K/DST-denoised function the production board uses, so the
    scarcity structure is the shipped one and not a backtest invention.

    ``house_points`` is 0.0 for every entry. See the module docstring: there is
    no projected season total for these seasons, and a fabricated one would leak
    the outcome through the grader's priced-entry gate.
    """
    by_pos: dict[str, list[float]] = {}
    for row in rows:
        by_pos.setdefault(row.position, []).append(curve.value(row.position, row.pos_rank))
    for values in by_pos.values():
        values.sort(reverse=True)
    replacement, _started = replacement_levels(by_pos, DEFAULT_ROSTER, denoise_kdst=True)

    depth = len(rows)
    entries = tuple(
        BoardEntry(
            player_id=row.player_id,
            name=row.name,
            position=row.position,
            espn_overall_rank=row.board_rank,
            house_points=0.0,
            vor=(
                curve.value(row.position, row.pos_rank)
                - replacement.get(row.position, 0.0)
                + _TIE_BREAK_EPS * (depth - row.board_rank)
            ),
            team=row.team,
        )
        for row in rows
    )
    _assert_distinct_vor(entries)
    return entries


#: A consensus-ordered tie-break added to every ``vor``, and it is NOT cosmetic.
#:
#: The value curve is a least-squares MONOTONE fit, so it legitimately produces
#: FLAT RUNS — over four training seasons the consensus RB1 through RB5 really do
#: average the same realized points, and the fit says so by giving them one
#: value. Two things then go wrong that would not go wrong on the production
#: board (whose VOR comes from float projections and effectively never ties):
#:
#: 1. WITHIN a position a tie means the engine has no reason to prefer the
#:    consensus RB1 to the consensus RB5. It should: the market's within-position
#:    ordering is the only player-level signal this backtest has, and the curve's
#:    job is the cross-position SHAPE, not the ordering inside a position.
#: 2. ACROSS positions a tie is worse than arbitrary, it is NON-DETERMINISTIC.
#:    ``bots.BoardState.best_by_vor`` iterates ``allowed``, which is a ``set`` of
#:    position strings, and keeps the first entry with a strictly greater vor — so
#:    on an exact tie the winner depends on set iteration order, which depends on
#:    str hashing, which depends on PYTHONHASHSEED. The draft cockpit's bit-for-bit
#:    journal replay rests on determinism, so this is reported upstream — and the
#:    earlier version of this note said "even though the live board cannot trip
#:    it", WHICH IS FALSE AND WAS MEASURED FALSE. On the live 2026 board
#:    (``load_board(as_of="2026-08-30", season=2026)``, 3,264 rows) there are only
#:    532 distinct vor values and FOUR exact CROSS-POSITION ties: 0.0 shared by a
#:    QB/RB/TE/WR whose shallowest member sits at espn_overall_rank 48, D/ST
#:    against K at -0.22 and -1.13, and a 35-way tie at -283.48. Six FollowVor
#:    drafts on that board under PYTHONHASHSEED 0/1/2/3 produce FOUR DIFFERENT
#:    md5s. So any FollowVor arm measured on the live board — Phase 1's
#:    PickEngine-vs-FollowVor +1.093 included — carries a small non-reproducible
#:    component, and nothing in the repo pins PYTHONHASHSEED. The DRAFT-NIGHT path
#:    is unaffected: ``espn_overall_rank`` is unique across all 3,264 rows, so
#:    ``engine.py``'s ``best_by_rank`` cannot tie. THIS module is unaffected for
#:    the reason below rather than by luck.
#:
#: 1e-6 x (depth - board_rank) is at most ~5e-4 points on a ~540-row board
#: against curve values of 20-300, so it decides nothing except an exact tie, and
#: there it restores the consensus order. Uniqueness is then ASSERTED rather than
#: assumed.
_TIE_BREAK_EPS = 1e-6


def _assert_distinct_vor(entries: Sequence[BoardEntry]) -> None:
    seen: dict[float, str] = {}
    for entry in entries:
        clash = seen.get(entry.vor)
        if clash is not None:
            raise BacktestInputError(
                f"two board entries share an exact vor ({entry.vor!r}): {clash} and "
                f"{entry.player_id}. Cross-position vor ties make bots.best_by_vor "
                "depend on set iteration order (PYTHONHASHSEED), so the experiment "
                "would not be reproducible. Widen _TIE_BREAK_EPS."
            )
        seen[entry.vor] = entry.player_id


# ========================================================================
#                     4.  wiring a season together
# ========================================================================


@dataclass(frozen=True)
class SeasonInputs:
    """Everything one season's experiment needs, plus how it was built."""

    season: int
    scrape_date: str
    week1_date: str
    rows: tuple[BoardRow, ...]
    board: tuple[BoardEntry, ...]
    weekly: grader.WeeklyPointsMap
    curve: ValueCurve
    coverage: Mapping[str, int]


def realized_weekly_map(
    rows: Sequence[BoardRow],
    outcomes: Mapping[str, Mapping[int, float]],
) -> grader.WeeklyPointsMap:
    """A :class:`grader.WeeklyPointsMap` of REALIZED points over the board.

    Absent week = DID NOT PLAY, exactly the grader's convention — and here it is
    literal rather than a forecast artefact: a bye, an injury, a benching and a
    season on IR all present as missing weeks, which is the whole reason this
    grading is worth more than a season-total comparison.

    A player with NO weeks at all is kept with an empty inner dict. That is the
    single most important row shape in this module: it is the consensus RB who
    tore an ACL in August, and the board must still be able to draft him at his
    August price.
    """
    points = {row.player_id: dict(outcomes.get(row.outcome_key, {})) for row in rows}
    return grader.WeeklyPointsMap(
        points,
        positions={row.player_id: row.position for row in rows},
        names={row.player_id: row.name for row in rows},
        teams={row.player_id: row.team for row in rows},
    )


#: Floor on the fraction of board entries carrying at least one realized week.
#: MEASURED, generously: the five shipped seasons run 90.0% (2024) to 93.3%
#: (2025), so 0.60 is a third below the worst real season and cannot fire on
#: ordinary attrition. It exists to catch a namespace divergence, which does not
#: land anywhere near it — it lands at 0.
_MIN_PRICED_FRACTION = 0.60

#: Positions where "no realized week all season" is STRUCTURALLY IMPOSSIBLE, so
#: one such row is a join failure rather than an outcome. A franchise defense
#: plays all seventeen weeks; measured, 159 of 159 D/ST board rows across
#: 2021-2025 carry realized weeks. A skill player can genuinely record none
#: (preseason ACL), which is why the rule is position-scoped rather than global.
_MUST_PLAY_POSITIONS = ("DST",)


def assert_backtest_coverage(
    board: Sequence[BoardEntry],
    weekly: Mapping,
    *,
    min_priced_fraction: float = _MIN_PRICED_FRACTION,
    must_play: Sequence[str] = _MUST_PLAY_POSITIONS,
) -> None:
    """Refuse a board whose realized map cannot be the map for this board.

    THREE CHECKS, AND THE FIRST ONE ALONE WAS NOT A CHECK AT ALL. Presence was
    the original rule ("every entry must be a KEY of the map"), on the reasoning
    that a player with zero weeks is a real outcome and requiring a week would
    reject exactly the rows this backtest exists to price. That reasoning is
    right and the rule was still structurally unable to fire, because
    :func:`realized_weekly_map` builds its keys from the SAME rows that build the
    board: ``{row.player_id: dict(outcomes.get(row.outcome_key, {}))}`` writes a
    KEY for every row whether or not the outcome key resolved to anything. So a
    total id-space divergence produced 538 keys mapping to 538 empty dicts, this
    function passed, and every one of the ten teams graded at objective exactly
    7.0 with 17 hole weeks — verbatim the failure the docstring claimed to guard.

    So presence is kept and two checks that CAN fail are added:

    * a floor on the fraction of entries with at least one realized week
      (``min_priced_fraction``). A namespace divergence gives 0.0; the worst real
      season gives 0.900.
    * every entry at a position in ``must_play`` must have a realized week. This
      is the partial-divergence case the aggregate floor would ride through: a
      relocated-team D/ST abbreviation would grade those units at zero all season
      and bias the K/DST result invisibly, while 500-odd skill rows kept the
      overall fraction healthy.
    """
    missing = [e.player_id for e in board if e.player_id not in weekly]
    if missing:
        raise BacktestInputError(
            f"{len(missing)} of {len(board)} board entries are absent from the "
            f"realized points map (e.g. {missing[:5]}). The id spaces have diverged "
            "and every roster would grade as an empty season."
        )
    if not board:
        return
    priced = [e for e in board if weekly.get(e.player_id)]
    fraction = len(priced) / len(board)
    if fraction < min_priced_fraction:
        raise BacktestInputError(
            f"only {len(priced)} of {len(board)} board entries ({fraction:.1%}) carry "
            f"ANY realized week; the floor is {min_priced_fraction:.0%} and the worst "
            "real season on this data is 90.0%. A key that is present but maps to an "
            "empty dict reads exactly like a player who never played, so the id "
            "spaces have almost certainly diverged and every roster would grade "
            "against a season of empty lineups."
        )
    positions = getattr(weekly, "positions", {})
    wanted = {str(p) for p in must_play}
    silent = [
        e.player_id for e in board
        if str(positions.get(e.player_id, e.position)) in wanted
        and not weekly.get(e.player_id)
    ]
    if silent:
        raise BacktestInputError(
            f"{len(silent)} board entries at {sorted(wanted)} carry no realized week at "
            f"all (e.g. {silent[:5]}). A franchise defense plays every week, so this is "
            "a join failure — most likely a team-abbreviation divergence between the "
            "consensus board and the realized map — not an outcome. Grading these at "
            "zero all season would bias the K/DST result with nothing reporting it."
        )


def week1_first_gameday(conn, season: int) -> str:
    """The season's week-1 opener, from the ingested schedule. No as-of gate: the
    NFL calendar is the clock the leakage gate is stated against, not an input."""
    row = conn.execute(
        "SELECT MIN(gameday) FROM schedules WHERE season = ? AND game_type = 'REG' "
        "AND week = 1 AND gameday IS NOT NULL",
        (season,),
    ).fetchone()
    if row is None or row[0] is None:
        raise BacktestInputError(
            f"season {season} has no ingested REG week-1 schedule; the preseason "
            "cutoff cannot be established. Run `ziggurat ingest backfill`."
        )
    return str(row[0])


def season_outcomes(conn, season: int, **kwargs) -> dict[str, dict[int, float]]:
    """The realized points map for one season, all three scoring paths merged.

    Skill players and kickers are keyed by gsis id and cannot collide; defenses
    are keyed by team abbr, which is why the board namespaces them (``DST:LA``)
    and this map does not — the board row carries both keys.
    """
    out: dict[str, dict[int, float]] = {}
    out.update(_offense_points(conn, season))
    # Kickers key by gsis too. `weekly_stats` also holds a (zeroed) row for every
    # kicker, so the kicking supplement must WIN — merge it after, and replace
    # rather than update, so an all-zero stored week cannot survive beside it.
    out.update(_kicker_points(season, **kwargs))
    out.update(_dst_points(conn, season))
    return out


def build_season_inputs(
    conn,
    *,
    season: int,
    curve: ValueCurve,
    outcomes: Mapping[str, Mapping[int, float]],
    rows: Sequence[BoardRow] | None = None,
    scrape_date: str | None = None,
    coverage: Mapping[str, int] | None = None,
) -> SeasonInputs:
    """Assemble one season. The board and the outcomes arrive from DIFFERENT
    functions on purpose — nothing in :func:`preseason_board_rows` can see a
    realized point, and nothing in :func:`season_outcomes` can see a rank."""
    week1 = week1_first_gameday(conn, season)
    if rows is None:
        scrape_date, rows, coverage = preseason_board_rows(
            conn, season=season, week1_date=week1
        )
    board = board_entries(rows, curve)
    weekly = realized_weekly_map(rows, outcomes)
    assert_backtest_coverage(board, weekly)
    return SeasonInputs(
        season=season,
        scrape_date=str(scrape_date),
        week1_date=week1,
        rows=tuple(rows),
        board=board,
        weekly=weekly,
        curve=curve,
        coverage=dict(coverage or {}),
    )


# ========================================================================
#                     5.  the realized (model-free) objective
# ========================================================================


#: Positions the grader credits at the waiver tier when a starting slot is empty.
#: This is ``grader.STREAMED_POSITIONS`` and it is the shipped model.
STREAM_KDST = ("DST", "K")

#: The SENSITIVITY model, and the reason it exists is the sharpest measurement
#: choice in this module.
#:
#: ``grader`` credits an empty K or D/ST slot at that week's waiver-tier
#: replacement, because you do not field eight players when your kicker byes —
#: you add one on Tuesday for free. In a TEN-TEAM league the identical argument
#: holds for QB and TE: ten teams start one of each, and the ones nobody rosters
#: sit on waivers all season. The grader does not credit them, so a roster
#: carrying ONE quarterback is charged a full dead week for his bye.
#:
#: That is not a neutral omission here. Phase 1 measured that every one of 25
#: simulated engine drafts took EXACTLY 3 QB and 3 TE — six of sixteen picks on
#: two one-starter slots — and named it the engine's pathology. Under the K/DST
#: model those six picks buy real bye-week insurance the baselines do not have;
#: under this one they buy much less. So BOTH are reported, and if the engine's
#: margin lives only under the first, that is the finding.
STREAM_WITH_QB_TE = ("DST", "K", "QB", "TE")

#: HOW an empty slot is priced, once it has been decided that it is priced at
#: all. This is a knob the result is genuinely sensitive to, so it is named and
#: reported rather than buried in an index.
#:
#: ``"waiver_tier"`` — the shipped rule, corrected. The level is the best player
#:     at that position who would still be unrostered given how many of that
#:     position the ROOM ACTUALLY ROSTERED in this draft, ranked by what he
#:     scored that week. It is HINDSIGHT-OPTIMISTIC: a real Tuesday add is blind.
#: ``"blind_add"`` — no hindsight in the CHOICE. Take the best unrostered player
#:     at that position by PRESEASON CONSENSUS rank who plays that week, and
#:     credit what he actually scored. This is the Tuesday add an operator could
#:     really have made, and it is the honest model.
STREAM_MODES = ("waiver_tier", "blind_add")


def league_rostered_counts(
    rosters: Sequence[Sequence[BoardEntry]],
) -> dict[str, int]:
    """``position -> how many of it the whole ten-team room rostered``.

    THIS IS THE NUMBER THE WAIVER TIER IS, and getting it from ``roster.teams``
    was the single largest measurement defect in this module. See
    :func:`_stream_levels`.
    """
    counts: dict[str, int] = {}
    for entries in rosters:
        for entry in entries:
            counts[entry.position] = counts.get(entry.position, 0) + 1
    return counts


def _stream_levels(
    weekly: Mapping[str, Mapping[int, float]],
    positions: Mapping[str, str],
    *,
    weeks: Sequence[int],
    roster: RosterStructure,
    stream_positions: Sequence[str],
    rostered: frozenset[str] | None = None,
    depths: Mapping[str, int] | None = None,
    pos_ranks: Mapping[str, int] | None = None,
    mode: str = "waiver_tier",
) -> dict[int, dict[str, tuple[float, float, str]]]:
    """``week -> position -> (points, 0.0, player_id)`` for an EMPTY starting slot.

    Variance is returned as 0.0 rather than the model's sigma: this table feeds
    the DETERMINISTIC objective, which has no variance term, and returning a
    number nothing reads would invite someone to read it. A negative level is
    dropped, because an empty slot scores 0 and 0 beats a negative D/ST. A
    position with no ladder at all yields nothing, which is a hole that really is
    a hole.

    ---------------------------------------------------------------------------
    ``mode="waiver_tier"`` — the shipped rule, WITH ITS DEPTH CORRECTED.
    ---------------------------------------------------------------------------
    The level is the ``d``-th entry (0-indexed) of that week's realized-points
    ladder, where ``d`` is how many of that position the room rosters — so the
    credited player is the best one who would still be unrostered.

    ``grader.stream_levels_from_board`` uses ``d = roster.teams = 10`` and says
    so as "if all ten teams held exactly one". THAT IS TRUE FOR K AND D/ST ONLY,
    because position caps force exactly one of each per team; measured over 40
    drafts per arm on the real boards, league-wide K and D/ST counts are 10.00
    and 10.00 with zero variation. It is FALSE for QB and TE, and this module
    used to extend it to them anyway. Measured on the same drafts, league-wide
    counts are QB 19.75-20.15 and TE 16.07-16.55 — the credit was 5 to 10 ranks
    too shallow.

    THE SIZE OF THAT ERROR, and why it decided a headline (2023 map, mean over
    weeks 1-14): the credited QB level is 17.99 pts/week at index 10 against
    10.79 at index 19; TE is 10.58 against 6.47. Index 10 hands any team whose
    quarterback byes a week of Patrick Mahomes (his real 2023 average was 18.14).

    AND IT IS NOT SYMMETRIC ACROSS ARMS — this is the part the module previously
    got backwards in prose. The credit is paid only to a roster that LEAVES A
    SLOT EMPTY. The engine carries QB 2.75 / TE 3.00 and the consensus baseline
    QB 1.96 / TE 1.55, so crediting QB/TE moved the baseline's unfilled weeks
    4.93 -> 1.72 and the engine's only 4.09 -> 3.61. Roughly 2.7 more credited
    weeks a season accrue to the BASELINE. An over-generous QB/TE level is
    therefore a thumb on the scale AGAINST the engine, not for it.

    ``d`` comes from ``depths`` when given, else from ``rostered``, else falls
    back to ``roster.teams`` — which is the shipped assumption, kept as the
    default so ``test_stream_levels_match_the_grader_at_the_shipped_kdst_setting``
    still pins this against ``grader.stream_levels_from_board`` unchanged.

    ---------------------------------------------------------------------------
    ``mode="blind_add"`` — the same slot, priced WITHOUT hindsight.
    ---------------------------------------------------------------------------
    Even at the right depth, the waiver-tier rule picks the player who turned out
    to score most THAT WEEK. A real Tuesday add is blind. ``blind_add`` instead
    takes the best PRESEASON-CONSENSUS-ranked player at that position who is on
    nobody's roster and who plays that week, and credits what he actually scored.
    That is a decision an operator could really have made on the Tuesday, and it
    removes the whole optimism argument rather than describing it.

    Its own simplifications, stated: every team needing a stream at a position in
    one week is credited the SAME free agent (a real league would race for him),
    and "plays that week" uses the schedule, which is known in advance anyway.
    Both are shared by the waiver-tier rule.
    """
    if mode not in STREAM_MODES:
        raise BacktestInputError(f"unknown stream mode {mode!r}; expected {STREAM_MODES}")
    wanted = set(stream_positions)
    if not wanted:
        return {}
    if mode == "blind_add" and (rostered is None or pos_ranks is None):
        raise BacktestInputError(
            "stream mode 'blind_add' needs the league-wide rostered set and the "
            "preseason positional ranks; it is defined against a real free-agent pool."
        )

    if depths is None:
        depths = {}
        if rostered is not None:
            counted: dict[str, int] = {}
            for pid in rostered:
                pos = positions.get(pid)
                if pos is not None:
                    counted[pos] = counted.get(pos, 0) + 1
            depths = counted

    out: dict[int, dict[str, tuple[float, float, str]]] = {}
    for week in sorted({int(w) for w in weeks}):
        ladders: dict[str, list[tuple[float, str]]] = {}
        for pid, pos in positions.items():
            if pos not in wanted:
                continue
            row = weekly.get(pid)
            if row is None or week not in row:
                continue
            ladders.setdefault(pos, []).append((float(row[week]), pid))
        week_out: dict[str, tuple[float, float, str]] = {}
        for pos, rows in ladders.items():
            if mode == "blind_add":
                free = [r for r in rows if r[1] not in rostered]
                if not free:
                    continue
                # Best CONSENSUS rank among the free agents who play this week.
                # An unranked id sorts last; the id breaks ties so the choice is
                # a pure function of the data, never of dict order.
                points, pid = min(
                    free, key=lambda r: (pos_ranks.get(r[1], 1 << 30), r[1])
                )
            else:
                rows.sort(key=lambda r: (-r[0], r[1]))
                depth = int(depths.get(pos, roster.teams))
                points, pid = rows[min(max(depth, 0), len(rows) - 1)]
            if points <= 0.0:
                continue
            week_out[pos] = (points, 0.0, pid)
        if week_out:
            out[week] = week_out
    return out


def _seat_totals(
    entries: Sequence[BoardEntry],
    weekly: Mapping[str, Mapping[int, float]],
    weeks: Sequence[int],
    roster: RosterStructure,
    streams,
) -> tuple[dict[int, float], tuple[int, ...]]:
    """``({week: seated total}, unfilled weeks)`` for one roster.

    MIRRORS ``grader._seat_week``'s mu deliberately, and
    ``test_seat_totals_match_the_grader`` pins the mirror against the real
    ``grade_roster`` — a silent divergence here would make the two objectives
    describe different lineups while both looked plausible. The three rules it
    reproduces: a player with no entry for the week is NOT SEATABLE (the slot is
    empty, not filled at zero); a K or D/ST beyond the first is capped out; an
    empty K/D/ST slot is credited that week's waiver-tier replacement.
    """
    capped = grader.capped_out(entries, weekly, weeks, roster=roster)
    totals: dict[int, float] = {}
    holes: list[int] = []
    positions = {e.player_id: e.position for e in entries}
    keys = [e.player_id for e in entries]
    for week in weeks:
        points: dict[str, float] = {}
        available: dict[str, bool] = {}
        for key in keys:
            wpts = weekly.get(key)
            plays = wpts is not None and week in wpts
            points[key] = float(wpts[week]) if plays else 0.0
            available[key] = plays and key not in capped
        fill = fill_lineup(keys, positions, points, roster=roster, available=available)
        total = fill.total
        week_streams = (streams or {}).get(week, {})
        unfilled = False
        for label in fill.empty_slots:
            level = week_streams.get(label.rstrip("0123456789"))
            if level is None:
                unfilled = True
            else:
                total += level[0]
        if unfilled:
            holes.append(week)
        totals[week] = total
    return totals, tuple(holes)


def realized_grade_fn(
    weekly: grader.WeeklyPointsMap,
    *,
    roster: RosterStructure = DEFAULT_ROSTER,
    regular_season_weeks: Sequence[int] = REGULAR_SEASON_WEEKS,
    playoff_weeks: Sequence[int] = PLAYOFF_WEEKS,
    playoff_teams: int = 6,
    stream_positions: Sequence[str] = STREAM_KDST,
    stream_mode: str = "waiver_tier",
    pos_ranks: Mapping[str, int] | None = None,
) -> ev.GradeFn:
    """A :data:`ev.GradeFn` whose objective is REALIZED all-play wins.

    Each regular-season week: seat the best legal lineup from realized points,
    compare against each of the nine real rivals from the same draft, score
    1 / 0.5 / 0, average. Summed over the weeks this is expected wins against a
    uniformly random opponent — the same quantity ``grade_roster`` models, with
    the model taken out. Across the ten teams of one draft it sums to exactly
    ``teams/2 * weeks`` (70.0 here), which is what
    ``test_realized_wins_sum_to_seventy`` asserts.

    ``expected_wins`` on the returned grade carries the MODELLED number from
    ``grader.grade_roster`` on the same points, so one draft run yields both
    objectives and the secondary is never a second set of drafts.

    ``playoff_prob`` is the deterministic top-``playoff_teams`` indicator on
    all-play wins — an all-play standings, not a simulated H2H schedule, and
    labelled as such in the reasons. ``title_prob`` is 0.0 and is NOT a
    computed 0: a bracket needs randomness this deterministic grade does not
    have. Read it as "not computed".

    ``opponents=None`` (the harness's fixed-field second opinion) RAISES rather
    than inventing a number: an all-play objective is defined against real
    rivals, and the harness's synthetic field has a known level bias that would
    make the resulting "field check" meaningless. Pass ``field_check=False``.

    ``stream_positions`` says WHICH empty starting slots are credited a free
    agent; ``stream_mode`` (see :data:`STREAM_MODES`) says HOW. ``pos_ranks``
    (player_id -> preseason positional rank) is required by ``blind_add`` and
    ignored otherwise. Whatever is chosen rides into every grade's reasons, and
    both choices move the headline — see the note in :func:`_stream_levels`.
    """
    weeks = tuple(sorted(set(regular_season_weeks) | set(playoff_weeks)))
    reg = tuple(sorted(set(regular_season_weeks)))
    if stream_mode not in STREAM_MODES:
        raise BacktestInputError(
            f"unknown stream mode {stream_mode!r}; expected {STREAM_MODES}"
        )

    # THE STREAM TABLE IS A PROPERTY OF THE DRAFT, NOT OF THE SEASON. The
    # waiver tier sits below whatever the ROOM rostered, and the room is
    # different in every draft (and, at QB/TE, different between arms). It used
    # to be built once per season from `roster.teams`, which is right only where
    # a position cap forces one per team — see `_stream_levels`. Both the table
    # and the seated totals are therefore cached against the draft's league-wide
    # rostered set: all ten `grade` calls of one draft share it, and a roster
    # tuple alone is no longer a sufficient cache key.
    stream_cache: dict[frozenset, dict | None] = {}
    cache: dict[tuple, tuple[dict[int, float], tuple[int, ...]]] = {}

    def streams_for(rostered: frozenset):
        hit = stream_cache.get(rostered)
        if hit is None and rostered not in stream_cache:
            hit = (
                _stream_levels(
                    weekly, weekly.positions, weeks=weeks, roster=roster,
                    stream_positions=stream_positions, rostered=rostered,
                    pos_ranks=pos_ranks, mode=stream_mode,
                )
                if stream_positions
                else None
            )
            stream_cache[rostered] = hit
        return hit

    def totals_for(entries: Sequence[BoardEntry], rostered: frozenset, streams):
        key = (rostered, tuple(sorted(e.player_id for e in entries)))
        hit = cache.get(key)
        if hit is None:
            hit = _seat_totals(entries, weekly, weeks, roster, streams)
            cache[key] = hit
        return hit

    def _grade(entries, opponents):
        if opponents is None:
            raise BacktestInputError(
                "the realized all-play objective is defined against the real rivals "
                "of the same draft; there is no honest way to compute it against the "
                "grader's synthetic field. Run the harness with field_check=False."
            )
        all_rosters = [list(entries)] + [list(r) for r in opponents.values()]
        rostered = frozenset(e.player_id for r in all_rosters for e in r)
        streams = streams_for(rostered)
        mine, holes = totals_for(entries, rostered, streams)
        # Seat 0 is always the roster under test; the rest are its real rivals.
        table = [mine] + [totals_for(r, rostered, streams)[0] for r in opponents.values()]

        def all_play(index: int) -> tuple[float, tuple[float, ...]]:
            """One team's all-play win total, and its per-week win fractions."""
            per_week: list[float] = []
            for week in reg:
                me = table[index][week]
                others = [t[week] for j, t in enumerate(table) if j != index]
                beat = sum(1 for o in others if me > o)
                tie = sum(1 for o in others if me == o)
                per_week.append((beat + 0.5 * tie) / len(others))
            return sum(per_week), tuple(per_week)

        realized, win_probs = all_play(0)
        above = sum(1 for i in range(1, len(table)) if all_play(i)[0] > realized)
        playoff = 1.0 if above < playoff_teams else 0.0

        # The SECONDARY number always uses the grader's own shipped model
        # (K/DST streaming only), whatever `stream_positions` this objective was
        # built with — so the two never silently become the same measurement
        # wearing two names.
        modelled = grader.grade_roster(
            entries, weekly, roster=roster, opponent_rosters=opponents,
            regular_season_weeks=reg, playoff_weeks=playoff_weeks,
            playoff_teams=playoff_teams, stream_kdst=True,
        )
        means = [0.0] * (max(weeks) if weeks else 0)
        for week, value in mine.items():
            means[week - 1] = value
        return grader.SeasonGrade(
            objective=realized,
            expected_wins=modelled.expected_wins,
            playoff_prob=playoff,
            title_prob=0.0,
            weekly_means=tuple(means),
            hole_weeks=holes,
            reasons=(
                "OBJECTIVE = realized all-play wins: each week your seated lineup is "
                "compared with all nine real rivals from this draft on the points "
                "those players ACTUALLY scored (1 for a win, 0.5 for a tie), summed "
                f"over weeks {reg[0]}-{reg[-1]}. No projection and no win model.",
                "expected_wins beside it is the MODELLED number from the same "
                "realized points (grader.grade_roster's normal-CDF win model), for "
                "comparison only.",
                "playoff_prob is an ALL-PLAY standings indicator (top "
                f"{playoff_teams} of {roster.teams} by realized all-play wins), not a "
                "simulated head-to-head schedule. title_prob is NOT COMPUTED (0.0): a "
                "bracket needs randomness a deterministic grade does not have.",
                (
                    "EMPTY STARTING SLOTS: "
                    + (
                        "score 0 (no waiver credit at all)."
                        if not stream_positions
                        else f"{'/'.join(stream_positions)} are credited a free-agent "
                        + (
                            "replacement chosen by PRESEASON CONSENSUS rank among the "
                            "players nobody in the room rostered — a blind Tuesday add, "
                            "no hindsight in the choice."
                            if stream_mode == "blind_add"
                            else "replacement at the depth the room ACTUALLY rostered "
                            "that position (measured per draft, not assumed at one per "
                            "team), ranked by what he scored that week — so the credit "
                            "is hindsight-optimistic and is paid to whoever leaves a "
                            "slot empty."
                        )
                    )
                ),
            ) + modelled.reasons,
            win_probs=tuple(win_probs),
            regular_season_weeks=reg,
            playoff_weeks=tuple(playoff_weeks),
        )

    return _grade


# ========================================================================
#                     6.  the experiment
# ========================================================================

#: The strategies compared. `FollowEspnRank` on this board IS "draft the
#: preseason consensus board straight down" — for 2021-2025 the consensus board
#: and the room's board are the same object (module docstring, limitation 3), so
#: the ESPN-rank baseline and the consensus baseline coincide and are reported
#: once under the honest name.
BASELINE = "consensus_board"


#: The engine's K/DST window, and the alternative the probe tests. The shipped
#: engine may take a K or D/ST from round 9 (``priors.DEFAULT_KDST_EARLIEST_ROUND``)
#: and, driven by urgency, actually takes them around rounds 9-10 while the room
#: waits until 13-15. That timing is a STANDING CLAIM of this project ("the K/DST
#: divergence play is validated"), self-graded on the engine's own projections
#: every time it has been checked. ``--kdst-probe`` adds a second engine arm that
#: is identical except it may not take either before round 13. It is a
#: CONFIRMATORY probe of an existing claim, not a search over knobs.
#:
#: WHAT IT DOES *NOT* TEST, measured and printed by
#: :func:`kdst_probe_diagnostic` rather than left to the reader: THE DIVERGENCE
#: PLAY. The modelled room takes its first kicker around overall pick 142 and its
#: first defense around 141, and round 13 starts at pick 121 — so the late arm
#: collects the SAME kicker and the SAME defense four rounds later and banks two
#: extra offensive picks. Measured on paired drafts: identical kicker in every
#: pair and identical D/ST in nearly all of them. Waiting cannot lose a player
#: this room never takes, so the sign of the probe is FORCED BY THE ROOM MODEL.
#: What the probe measures is the OPPORTUNITY COST OF THE ROUNDS, which is a real
#: and useful quantity — just not the one the name suggests.
#:
#: The second half of the same caveat is the board. On the leave-one-out realized
#: curve the top five defenses are one pooled value (2-5 VOR, separated only by
#: the 1e-6 consensus tie-break); the live 2026 house board prices the top three
#: at 30.5 / 29.2 / 27.3 with a cliff to 17.1. A study that finds "no defense was
#: worth reaching for" on a board where no defense is worth anything has not
#: tested the live board's claim. Phase 1's own measurement of eleven REAL
#: ten-team rooms (first D/ST median pick 141, 0 of 100 rival seats taking one
#: before pick 90) is the evidence that bears on the live play; this is not.
KDST_PROBE_ROUND = 13


def strategies(*, rollouts: int, kdst_probe: bool = False) -> dict[str, object]:
    out: dict[str, object] = {
        "pick_engine": PickEngine(rollouts=rollouts),
        "follow_vor": FollowVor(),
        BASELINE: FollowEspnRank(),
    }
    if kdst_probe:
        out["engine_late_kdst"] = PickEngine(
            rollouts=rollouts, kdst_earliest_round=KDST_PROBE_ROUND
        )
    return out


@dataclass(frozen=True)
class SeasonResult:
    season: int
    scrape_date: str
    board_size: int
    outcomes: Mapping[str, tuple[ev.DraftOutcome, ...]]


def run_season(
    inputs: SeasonInputs,
    *,
    n: int,
    slots: Sequence[int] = (OPERATOR_SLOT,),
    seed: int | None = None,
    rollouts: int = 512,
    stream_positions: Sequence[str] = STREAM_KDST,
    stream_mode: str = "waiver_tier",
    kdst_probe: bool = False,
) -> SeasonResult:
    """Run every strategy over one season's shared seed grid.

    ``field_check=False``: the harness's fixed synthetic field is a MODEL of
    rivals, and the realized objective is only defined against the real ones (see
    :func:`realized_grade_fn`). The anti-cheat that ``field_objective`` provides
    is already present another way — ``DraftOutcome.rank`` grades all ten rosters
    in the same draft, and the objective is zero-sum, so a seat cannot gain in
    rank by leaving the room a worse board.
    """
    grade = realized_grade_fn(
        inputs.weekly,
        stream_positions=stream_positions,
        stream_mode=stream_mode,
        pos_ranks={row.player_id: row.pos_rank for row in inputs.rows},
    )
    out: dict[str, tuple[ev.DraftOutcome, ...]] = {}
    for name, strategy in strategies(rollouts=rollouts, kdst_probe=kdst_probe).items():
        out[name] = ev.evaluate_strategy(
            inputs.board,
            inputs.weekly,
            strategy=strategy,
            n=n,
            slots=slots,
            seed=inputs.season if seed is None else seed,
            grade=grade,
            field_check=False,
        )
    return SeasonResult(
        season=inputs.season,
        scrape_date=inputs.scrape_date,
        board_size=len(inputs.board),
        outcomes=out,
    )


def pair(
    results: Sequence[SeasonResult],
    *,
    challenger: str,
    baseline: str = BASELINE,
    objective: str = "objective",
    seed: int = 0,
    slots: Sequence[int] = (OPERATOR_SLOT,),
) -> ev.PairedResult:
    """Pool the seasons and hand the paired statistics to the harness.

    ``objective="expected_wins"`` re-pairs the SAME graded drafts on the modelled
    number instead of the realized one, by swapping the field the harness reads.
    Nothing is re-drafted and nothing is re-graded.

    THE POOLED INTERVAL TREATS SEASONS AS REPLICATES, AND THAT IS THE SINGLE
    BIGGEST CAVEAT ON EVERY NUMBER THIS MODULE PRINTS. All ``n`` drafts of one
    season share ONE realized-outcome draw — the same injuries, the same busts,
    the same football — so they are not ``n`` independent observations of "how
    this strategy does in a season". They are ``n`` re-draws of the room inside
    ONE season. The pooled interval is therefore a statement about THESE FIVE
    SEASON-REALIZATIONS, not about the population of seasons a Monday draft is
    drawn from.

    Two corrections to what this docstring used to say. First the arithmetic: it
    said "five blocks would give a block-level interval two degrees of freedom".
    Five blocks give FOUR (the harness's own note said df=2 for THREE slots and
    the reasoning was copied without re-deriving). Second, and materially: the
    consequence was never stated. Recomputed at the block level — the mean of the
    five season means, t(4) = 2.776 — NO contrast in this study excludes zero,
    including the ones the run prints as "EXCLUDES ZERO". :func:`season_block_interval`
    computes exactly that and :func:`main` now prints it under every contrast, so
    the two readings sit side by side instead of only the flattering one being
    visible.

    Which interval is "right" depends on the question. For "does this strategy
    beat that one against this room, on these five seasons" the pooled interval
    is the answer and it is sound. For "should I expect this to hold next season"
    the block interval is the answer, and the honest reply is that five seasons
    cannot resolve a sixth of a win.
    """
    def arm(name: str) -> list[ev.DraftOutcome]:
        rows: list[ev.DraftOutcome] = []
        for result in results:
            for outcome in result.outcomes[name]:
                rows.append(
                    outcome if objective == "objective"
                    else dataclasses.replace(
                        outcome, objective=getattr(outcome, objective)
                    )
                )
        return rows

    a, b = arm(challenger), arm(baseline)
    return ev.build_paired_result(
        a, b,
        name_a=challenger, name_b=baseline,
        slots=slots, n_per_slot=len(a) // max(1, len(slots)), seed=seed,
    )


# ========================================================================
#                     7.  reporting
# ========================================================================
@dataclass(frozen=True)
class BlockResult:
    """The SEASON-BLOCK reading of a contrast: n = 5, not n = 500.

    ``mean`` is the mean of the per-season mean deltas and the interval is a
    Student t on those five numbers. It is the interval to quote when the
    question is "will this hold next season"; :func:`pair`'s pooled interval is
    the one to quote when the question is about these five seasons. Both are
    printed, always, because printing only one of them is how a study reads as
    stronger than it is.
    """

    challenger: str
    baseline: str
    objective: str
    mean: float
    ci_low: float
    ci_high: float
    sd: float
    n_blocks: int
    per_block: Mapping[int, float]

    @property
    def excludes_zero(self) -> bool:
        # the same guard as backtest.stats.Interval.excludes_zero: a collapsed
        # (lo == hi) interval from identical per-season deltas is not evidence
        # and never reads EXCLUDES ZERO
        return self.ci_low < self.ci_high and (self.ci_low > 0.0 or self.ci_high < 0.0)


def season_block_interval(
    results: Sequence[SeasonResult],
    *,
    challenger: str,
    baseline: str = BASELINE,
    objective: str = "objective",
    confidence: float = 0.95,
) -> BlockResult:
    """Treat each SEASON as one observation. See :class:`BlockResult`.

    The interval itself is :func:`backtest.stats.season_block_interval` —
    the one implementation shared with the weekly replay harness (item 4.1).
    """
    per = _per_season_delta(results, challenger, baseline, objective)
    iv = stats.season_block_interval(per, confidence=confidence)
    return BlockResult(
        challenger=challenger, baseline=baseline, objective=objective,
        mean=iv.mean, ci_low=iv.lo, ci_high=iv.hi, sd=iv.sd,
        n_blocks=iv.n, per_block=per,
    )




def format_board_head(inputs: SeasonInputs, *, top: int = 5) -> str:
    head = ", ".join(
        f"{r.board_rank}. {r.name} ({r.position}{r.pos_rank} {r.team})"
        for r in inputs.rows[:top]
    )
    return (
        f"{inputs.season}  board {inputs.scrape_date} (week 1 opens "
        f"{inputs.week1_date}), {len(inputs.rows)} draftable, "
        f"{inputs.coverage.get('unresolved_dropped', 0)} dropped unresolvable\n"
        f"        {head}"
    )


def format_pair(result: ev.PairedResult, *, label: str) -> str:
    return (
        f"  {label:<34s} {result.mean_delta:+7.3f}  "
        f"[{result.ci_low:+.3f}, {result.ci_high:+.3f}]  "
        f"{'EXCLUDES ZERO' if result.excludes_zero else 'includes zero':<14s}  "
        f"n={result.n:<4d} ahead {result.win_rate:5.1%}  "
        f"levels {result.mean_a:.3f} vs {result.mean_b:.3f}"
    )


def kdst_probe_diagnostic(
    inputs: Mapping[int, SeasonInputs],
    *,
    n: int = 4,
    rollouts: int = 256,
    slots: Sequence[int] = (OPERATOR_SLOT,),
    seed: int | None = None,
) -> str:
    """WHAT THE K/DST PROBE IS ACTUALLY MEASURING — printed, not assumed.

    The probe moves the engine's ``kdst_earliest_round`` from 9 to 13 and reports
    the difference in realized wins. It is natural to read that as a test of the
    DIVERGENCE PLAY ("is taking a kicker at pick 92 while the room waits until
    round 15 worth anything?"). It is not, and this diagnostic is here so nobody
    reads it that way again.

    The modelled room takes its first K and D/ST very late. If it never takes one
    before round 13, then waiting until round 13 CANNOT COST YOU THE PLAYER: both
    arms end up with the same kicker and the same defense, and the late arm has
    simply banked four extra offensive picks. The sign of the result is then
    forced by the room model rather than measured from the seasons. This function
    prints how often the two arms finish with the SAME K and the SAME D/ST, and
    the earliest overall pick at which any rival seat took one, so the reader can
    see which regime the number came from.

    It also prints the board's D/ST VOR head, which is the OTHER half of the
    transfer problem: on the leave-one-out realized curve a defense is worth 2-5
    VOR with the top several nearly tied, while the live 2026 house board prices
    the top three at 30.5 / 29.2 / 27.3 with a cliff to 17.1. A backtest that
    finds "the defense was not worth reaching for" on a board where no defense is
    worth anything has not tested the live board's claim.
    """
    lines = [
        "K/DST PROBE DIAGNOSTIC — is the probe measuring the divergence play, or "
        "the round's opportunity cost?",
    ]
    same_k = same_d = both = pairs = 0
    overlap: list[int] = []
    depth_seen: dict[str, list[int]] = {}
    first_room_k: list[int] = []
    first_room_d: list[int] = []
    for season, inp in sorted(inputs.items()):
        engine = PickEngine(rollouts=rollouts)
        late = PickEngine(rollouts=rollouts, kdst_earliest_round=KDST_PROBE_ROUND)
        for slot, _rep, draft_seed in ev._seed_grid(
            season if seed is None else seed, tuple(slots), n
        ):
            rosters = []
            for strategy in (engine, late):
                res = ev._draft_once(
                    inp.board, strategy, slot=slot, draft_seed=draft_seed,
                    priors=ev.ROOM_PRIORS_2025, roster=DEFAULT_ROSTER,
                    rounds=16, autodraft_count=None, paired_streams=True,
                )
                rosters.append(res)
            a, b = (r.rosters[slot] for r in rosters)
            pairs += 1

            def one(entries, pos):
                got = [e.player_id for e in entries if e.position == pos]
                return got[0] if got else None

            k_same = one(a, "K") == one(b, "K")
            d_same = one(a, "DST") == one(b, "DST")
            same_k += k_same
            same_d += d_same
            both += k_same and d_same
            overlap.append(len({e.player_id for e in a} & {e.player_id for e in b}))
            for pos, count in league_rostered_counts(
                list(rosters[0].rosters.values())
            ).items():
                depth_seen.setdefault(pos, []).append(count)
            # When the ROOM first takes one, over the nine rival seats.
            by_id = {e.player_id: e.position for e in inp.board}
            for bucket, pos in ((first_room_k, "K"), (first_room_d, "DST")):
                picks = [
                    overall for overall, team, pid in rosters[0].pick_log
                    if team != slot and by_id.get(pid) == pos
                ]
                if picks:
                    bucket.append(min(picks))
    lines.append(
        f"  paired drafts {pairs}: SAME kicker in {same_k}/{pairs}, SAME D/ST in "
        f"{same_d}/{pairs}, both in {both}/{pairs}; mean roster overlap "
        f"{statistics.fmean(overlap):.1f}/16 players"
    )
    if first_room_k:
        lines.append(
            f"  earliest RIVAL K taken: overall pick {min(first_room_k)} "
            f"(median {statistics.median(first_room_k):.0f}); rival D/ST: "
            f"{min(first_room_d)} (median {statistics.median(first_room_d):.0f}) "
            f"— round {KDST_PROBE_ROUND} starts at overall pick "
            f"{(KDST_PROBE_ROUND - 1) * DEFAULT_ROSTER.teams + 1}"
        )
    if depth_seen:
        lines.append(
            "  league-wide rostered depth in these drafts (the waiver tier sits "
            "just below it): "
            + "  ".join(
                f"{pos} {statistics.fmean(depth_seen[pos]):.2f}"
                for pos in ("QB", "RB", "WR", "TE", "K", "DST") if pos in depth_seen
            )
            + f"  — roster.teams is {DEFAULT_ROSTER.teams}, which is the right depth "
              "for K and D/ST (position caps) and nowhere near right for QB or TE"
        )
    if same_k == pairs and same_d >= pairs * 0.8:
        lines.append(
            "  READ THIS AS: the two arms bought the same K and (nearly always) the "
            "same D/ST four rounds apart, so the probe is pricing the OPPORTUNITY "
            "COST OF THE ROUNDS, not the divergence play. Waiting cannot lose a "
            "player this room never takes."
        )
    for season, inp in sorted(inputs.items()):
        dst = sorted((e.vor for e in inp.board if e.position == "DST"), reverse=True)[:5]
        ties = sum(1 for x, y in zip(dst, dst[1:], strict=False) if abs(x - y) < 1e-3)
        note = (
            f"{ties} of the top 5 adjacent pairs separated by <1e-3 — PAVA pooled "
            "them to ONE value and only the 1e-6 consensus tie-break orders them"
            if ties else "the top 5 are genuinely distinct this season"
        )
        lines.append(
            f"  {season} board D/ST VOR head: "
            + ", ".join(f"{v:.3f}" for v in dst)
            + f"   ({note})"
        )
    lines.append(
        "  For scale, the LIVE 2026 house board prices the top three defenses at "
        "30.5 / 29.2 / 27.3 VOR with a cliff to 17.1 at rank 4. Nothing measured "
        "on these boards transfers to a board with that shape."
    )
    return "\n".join(lines)


def format_block(result: BlockResult) -> str:
    """The season-block line printed under every pooled one. See :class:`BlockResult`."""
    per = "  ".join(f"{s}:{d:+.3f}" for s, d in sorted(result.per_block.items()))
    verdict = "EXCLUDES ZERO" if result.excludes_zero else "includes zero"
    return (
        f"        season-block (n={result.n_blocks}, df={result.n_blocks - 1}): "
        f"{result.mean:+.3f}  [{result.ci_low:+.3f}, {result.ci_high:+.3f}]  {verdict}\n"
        f"        per season: {per}"
    )


def report_contrast(
    results: Sequence[SeasonResult], *, challenger: str, baseline: str = BASELINE,
    objective: str = "objective", label: str | None = None,
) -> None:
    """Print BOTH readings of one contrast. Never print only the pooled one."""
    paired = pair(results, challenger=challenger, baseline=baseline, objective=objective)
    print(format_pair(paired, label=label or f"{challenger} vs {baseline}"))
    print(format_block(season_block_interval(
        results, challenger=challenger, baseline=baseline, objective=objective,
    )))


def _per_season_delta(
    results: Sequence[SeasonResult], challenger: str, baseline: str, objective: str
) -> dict[int, float]:
    out: dict[int, float] = {}
    for result in results:
        a = result.outcomes[challenger]
        b = result.outcomes[baseline]
        values = [
            getattr(x, objective) - getattr(y, objective)
            for x, y in zip(a, b, strict=True)
        ]
        out[result.season] = statistics.fmean(values)
    return out


# ========================================================================
#                     8.  entry point
# ========================================================================


def load_panel(conn, *, retrieved_as_of: str, mirror=FPECR_MIRROR, refresh: bool = False) -> int:
    """Mirror + ingest the ECR panel for the backtest window (STEP 1)."""
    os.makedirs(os.path.dirname(str(mirror)), exist_ok=True)
    return fpecr.pull_fpecr(
        conn, retrieved_as_of=retrieved_as_of, path=mirror,
        seasons=range(SEASONS[0], 2027), refresh=refresh,
    )


def prepare(
    conn, *, seasons: Sequence[int] = SEASONS, curve_source: str = "realized_loo",
    as_of_2026: str = "2026-08-30", curve_seasons: Sequence[int] = SEASONS,
) -> dict[int, SeasonInputs]:
    """Build each season's board + realized map, with its own leave-one-out curve.

    ``curve_seasons`` is the pool the curve trains on and defaults to ALL five —
    deliberately independent of ``seasons``, which selects what to RUN. Tying the
    two together would silently change the curve when a caller narrowed the run
    to one season for a spot check, so the spot check would not be measuring the
    same board the full run measured.
    """
    pool = sorted(set(seasons) | set(curve_seasons))
    outcomes = {s: season_outcomes(conn, s) for s in pool}
    raw: dict[int, tuple[str, tuple[BoardRow, ...], Mapping[str, int]]] = {}
    for season in pool:
        week1 = week1_first_gameday(conn, season)
        raw[season] = preseason_board_rows(conn, season=season, week1_date=week1)
    boards = {s: raw[s][1] for s in pool}

    inputs: dict[int, SeasonInputs] = {}
    for season in seasons:
        if curve_source == "realized_loo":
            training = [s for s in curve_seasons if s != season]
            if not training:
                raise BacktestInputError(
                    "leave-one-season-out needs at least two seasons in "
                    f"curve_seasons; got {list(curve_seasons)}."
                )
            curve = realized_value_curve(boards, outcomes, training_seasons=training)
        elif curve_source == "espn_2026":
            curve = espn_2026_value_curve(conn, as_of=as_of_2026)
        else:
            raise BacktestInputError(f"unknown curve_source {curve_source!r}")
        scrape, rows, coverage = raw[season]
        inputs[season] = build_season_inputs(
            conn, season=season, curve=curve, outcomes=outcomes[season],
            rows=rows, scrape_date=scrape, coverage=coverage,
        )
    return inputs


#: CLI name -> (which slots are credited, how). See :data:`STREAM_MODES`.
_STREAM_CHOICES: dict[str, tuple[tuple[str, ...], str]] = {
    "kdst":         (STREAM_KDST,         "waiver_tier"),
    "qb_te":        (STREAM_WITH_QB_TE,   "waiver_tier"),
    "qb_te_blind":  (STREAM_WITH_QB_TE,   "blind_add"),
    "kdst_blind":   (STREAM_KDST,         "blind_add"),
    "none":         ((),                  "waiver_tier"),
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None)
    parser.add_argument("--n", type=int, default=25, help="drafts per season per strategy")
    parser.add_argument("--rollouts", type=int, default=512)
    parser.add_argument("--seasons", default=",".join(str(s) for s in SEASONS))
    parser.add_argument("--curve", default="realized_loo", choices=("realized_loo", "espn_2026"))
    parser.add_argument(
        "--stream", default="kdst", choices=tuple(_STREAM_CHOICES),
        help="how an empty starting slot is priced. kdst = the shipped grader "
             "model (K and D/ST credited at the tier below what the room "
             "rostered); qb_te = also QB and TE, which a ten-team league really "
             "can stream; *_blind = the same slots, but the free agent is chosen "
             "by PRESEASON CONSENSUS rank rather than by what he turned out to "
             "score that week (no hindsight); none = an empty slot scores 0",
    )
    parser.add_argument(
        "--kdst-probe", action="store_true",
        help="add a second engine arm that may not take a K or D/ST before round "
             f"{KDST_PROBE_ROUND}, testing the divergence play on realized outcomes",
    )
    parser.add_argument(
        "--seed-base", type=int, default=None,
        help="offset the per-season seed grid, e.g. --seed-base 500000 draws a "
             "HELD-OUT room grid (seed = base + season instead of seed = season). "
             "Every claim in this study was re-run on three of these; see the note.",
    )
    parser.add_argument("--ingest", action="store_true", help="mirror + load the ECR panel first")
    parser.add_argument("--retrieved-as-of", default=None)
    args = parser.parse_args(argv)

    from ziggurat.paths import DEFAULT_DB_PATH

    conn = open_db(args.db or DEFAULT_DB_PATH)
    if args.ingest:
        stamp = args.retrieved_as_of
        if stamp is None:
            raise SystemExit("--ingest requires --retrieved-as-of (Rule 1: no implicit now)")
        written = load_panel(conn, retrieved_as_of=stamp)
        print(f"fpecr_panel: {written} rows written")

    seasons = tuple(int(s) for s in args.seasons.split(","))
    t0 = time.time()
    inputs = prepare(conn, seasons=seasons, curve_source=args.curve)
    print(f"\nPRESEASON BOARDS ({args.curve} value curve)")
    for season in seasons:
        print("   ", format_board_head(inputs[season]))

    results = []
    for season in seasons:
        started = time.time()
        results.append(
            run_season(
                inputs[season], n=args.n, rollouts=args.rollouts,
                seed=None if args.seed_base is None else args.seed_base + season,
                stream_positions=_STREAM_CHOICES[args.stream][0],
                stream_mode=_STREAM_CHOICES[args.stream][1],
                kdst_probe=args.kdst_probe,
            )
        )
        print(f"    {season}: {args.n} drafts x "
              f"{len(strategies(rollouts=1, kdst_probe=args.kdst_probe))} strategies "
              f"in {time.time() - started:.1f}s")

    print(f"\nREALIZED ALL-PLAY WINS (weeks 1-14, {args.n} drafts/season, "
          f"{len(seasons)} seasons, slot {OPERATOR_SLOT + 1} of 10, "
          f"streamed slots: {args.stream})")
    print("  Two intervals are printed for every contrast and BOTH are real: the "
          "pooled one\n  treats each draft as an observation (a claim about these "
          "five seasons), the\n  season-block one treats each SEASON as an "
          "observation (a claim about seasons).")
    challengers = ["pick_engine", "follow_vor"]
    if args.kdst_probe:
        challengers.append("engine_late_kdst")
    for challenger in challengers:
        report_contrast(results, challenger=challenger)
    report_contrast(results, challenger="pick_engine", baseline="follow_vor")
    if args.kdst_probe:
        report_contrast(results, challenger="pick_engine", baseline="engine_late_kdst")

    print("\nMODELLED EXPECTED WINS on the same realized points (secondary)")
    for challenger in ("pick_engine", "follow_vor"):
        result = pair(results, challenger=challenger, objective="expected_wins")
        print(format_pair(result, label=f"{challenger} vs {BASELINE}"))

    if args.kdst_probe:
        print()
        print(kdst_probe_diagnostic(inputs, n=max(2, min(4, args.n))))

    print("\nROSTER SHAPE (mean picks by position, operator seat)")
    for name in strategies(rollouts=1, kdst_probe=args.kdst_probe):
        rows = [o for r in results for o in r.outcomes[name]]
        shape = {p: statistics.fmean([float(o.shape.get(p, 0)) for o in rows])
                 for p in ("QB", "RB", "WR", "TE", "DST", "K")}
        holes = statistics.fmean([o.holes for o in rows])
        rank = statistics.fmean([o.rank for o in rows])
        print(f"  {name:<18s} " + "  ".join(f"{p} {v:.2f}" for p, v in shape.items())
              + f"   holes {holes:.2f}  mean finish {rank:.2f}/10")

    print(f"\ntotal {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
