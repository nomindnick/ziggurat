"""Paired-comparison evaluation harness for draft strategies (Phase-2 keystone).

Import-quarantined package (Rule 8). Nothing outside ``ziggurat/draft/`` imports this.

WHAT THIS IS
------------
The measuring instrument every Phase-2 variant and the Phase-3 ship decision
hang off. It runs two operator strategies through the SAME simulated room and
grades the rosters they come out with using :mod:`ziggurat.draft.grader` — the
week-by-week win-probability objective — instead of
``simulator.optimal_starting_points``, the season-sum metric that is not merely
blind to bye collisions but BACKWARDS about them (grader's own module docstring
carries the measured sign flip).

``simulator.run_many`` cannot be used for this. It grades with
``optimal_starting_points``, it reports only the operator seat, and it compares
strategies UNPAIRED — two independent samples of a room whose draw-to-draw
spread swamps the effects we are hunting.

HOW MUCH PAIRING BUYS, measured on the live 2026 board (2026-08-30, seat 9,
n=30, comparing the standard deviation of the paired difference against
sqrt(var_A + var_B), which is what an unpaired contrast of the same two arms
would carry):

    PickEngine(512) vs FollowVor        paired 0.632  unpaired 0.950   1.5x
    PickEngine(512) vs PickEngine(128)  paired 0.017  unpaired 0.528  30.4x

The second row is the one that matters. Pairing helps a little when the two
strategies are wildly different and enormously when they are NEARLY THE SAME —
which is exactly the Phase-2 case, where every variant is the same engine with
one term changed. The bye fix this harness exists to measure is worth ~0.15
expected wins per collision avoided; against an unpaired spread of 0.53 that is
a third of one standard deviation, and n=25 cannot see it. Against the paired
spread of 0.017 it is enormous.

PAIRING, AND WHY IT NEEDS MORE THAN A SHARED SEED
-------------------------------------------------
Two things must be identical between arm A and arm B:

1. **The room draw.** Same seeded rivals, same autodraft seats. That comes free
   from replaying one ``draft_seed`` through the real
   :func:`~ziggurat.draft.simulator._assign_autodrafters` and the real
   ``RankNoiseBot`` / ``AutodraftBot`` construction — this module reuses both
   rather than re-implementing the room, so a change to the opponent model
   reaches the harness automatically.

2. **The rivals' random draws.** This one does NOT come free, and it is the
   reason this module exists rather than a ten-line loop over ``run_draft``.
   ``run_draft`` threads ONE ``random.Random`` through all ten seats. A strategy
   that consumes randomness — ``PickEngine`` draws a child stream for its
   survival rollout on every pick — therefore shifts every subsequent BOT draw.
   Swap the operator and the nine rivals reach for different players for reasons
   that have nothing to do with the operator's decision. The room is no longer
   the same room, and the difference you measure is part signal, part a reshuffle
   you caused by looking.

   So every seat here drafts from its OWN stream, derived from
   ``(draft_seed, seat)``: :class:`_SeatStream` swaps ``ctx.rng`` for the seat's
   stream on the way into the real picker and changes nothing else. The
   operator's consumption is then structurally unable to reach the rivals.
   ``paired_streams=False`` restores ``run_many``'s single shared stream so the
   cost of NOT doing this can be measured rather than asserted. Measured on the
   live board (FollowVor against an identical-picks twin that merely draws one
   extra random number before delegating — same 16 picks, every time — n=30 at
   each of 3 seats, 90 pairs):

       paired_streams=True    mean +0.0000   sd 0.0000   90 of 90 exact ties
       paired_streams=False   mean -0.0170   sd 0.6557    0 of 90 exact ties

   Two strategies that draft IDENTICALLY read as 0.66 expected wins of spread
   under the shared stream. That noise is bias-free, so more drafts shrink the
   interval around it — but it is noise injected into the very quantity being
   differenced, and at any n it is larger than most of the effects Phase 2 is
   hunting for.

   :func:`~tests.test_draft_evaluate` pins both halves. If the self-comparison
   test does not return exactly 0.0, the pairing is broken and every number this
   harness produces is noise.

WHAT IS REPORTED, AND WHY EACH PIECE IS THERE
---------------------------------------------
* ``mean_delta`` with a paired t confidence interval, and ``excludes_zero``
  stated plainly. A second, distribution-free percentile bootstrap interval
  rides alongside (``boot_ci_low`` / ``boot_ci_high``): the two disagreeing is
  the signal that the deltas are too skewed for the t interval to be read.
* ``rank_a`` / ``rank_b`` — where the operator finished among the ten. Expected
  wins in this model is ZERO-SUM (all ten teams sum to exactly 14.0 x 10 / 2 =
  70.0, which the grader's own test proves), so a strategy can raise its own
  number purely by starving the rivals it is graded against. Rank is the ordinal
  witness to that, and ``field_mean_delta`` is the second one: the same roster
  re-graded against a FIXED modelled field that both arms share.
* ``shape_a`` / ``shape_b`` and ``holes_a`` / ``holes_b`` — the diagnosis, not
  the score. The pathology Phase 1 measured (every one of 25 simulated drafts
  taking exactly 3 QB and 3 TE; three of fourteen H2H weeks with an unfillable
  starting slot) can survive an objective improvement, and a variant that raises
  expected wins while keeping the shape has not fixed what we set out to fix.

COST (measured 2026-08-30, this box, live 3,264-row board)
---------------------------------------------------------
One 150-draft arm (n=50 at each of 3 seats), grading all ten teams every time:

    PickEngine(rollouts=512)   204 s      PickEngine(rollouts=128)    56 s
    FollowEspnRank               5 s      grading is 30 ms of each draft

So the headline number — a paired ``n=50`` across 3 slots, 150 pairs, 300 drafts
— is **408 s (6.8 min)** with both arms at ``rollouts=512`` and **111 s (1.9
min)** with both at 128. ``rollouts`` belongs to the STRATEGY object, not to this
harness: pass ``PickEngine(rollouts=128)`` to both arms to buy the cheap setting,
and see :data:`CHEAP_SETTING_NOTE` for what that costs in fidelity — measured,
not assumed.

The harness's own overhead is the ``dataclasses.replace`` :class:`_SeatStream`
does per pick: measured at 1.08 us, 160 of them per draft = 0.17 ms against a
draft that costs 1.3 s. It does not move ``recommend()``: measured through this
harness on the live board, 160 real on-clock decisions at ``rollouts=512`` ran to
a worst case of **180.6 ms** (median 35.1, mean 81.1) against the 243 ms
draft-night budget.

RULE NOTES
----------
Rule 1 does not apply here: this module performs NO data read. It takes a board
and a points map the caller has already loaded through the accessors that DO
carry ``as_of`` (``simulator.load_board`` and ``grader.weekly_points_map``), and
:func:`paired_compare` refuses at the door unless those two agree on the id space
(``grader.assert_board_coverage``) — the divergence that would otherwise grade
every roster as a season of holes without raising. Rule 2 — no scoring constant
lives here; every point arrives already priced. Rule 6 — every result carries
plain-language reasons including the objective's disclosed blind spots.

DETERMINISM. All randomness flows from ``random.Random`` streams seeded from the
caller's ``seed`` (as strings, so no PYTHONHASHSEED dependence); no wall clock,
no global random. Slots are used in the order given and each slot's draws are
derived from ``(seed, slot)`` alone, so shortening or reordering ``slots`` never
changes another slot's draws; a tournament's strategies are graded in the
mapping's iteration order but the seed grid does not depend on it, so adding a
strategy leaves every existing number untouched. A ``(seed, n, slots,
strategies)`` run is bit-for-bit reproducible, and no field of any result carries
a timing.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from dataclasses import replace as dc_replace

from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.draft import grader
from ziggurat.draft.bots import (
    AutodraftBot,
    BoardEntry,
    PickContext,
    Picker,
    RankNoiseBot,
    allowed_positions,
    position_counts,
)
from ziggurat.draft.grader import SeasonGrade
from ziggurat.draft.priors import (
    DEFAULT_KDST_EARLIEST_ROUND,
    ROOM_PRIORS_2025,
    RoomPriors,
)
from ziggurat.draft.simulator import (
    ROUNDS,
    DraftResult,
    _assign_autodrafters,  # deliberate: the room must be the PRODUCTION room
    run_draft,
)

__all__ = [
    "CHEAP_SETTING_NOTE",
    "DraftOutcome",
    "EvaluationInputError",
    "GradeFn",
    "OPERATOR_SLOT_2026",
    "OffsetRankPicker",
    "PairedResult",
    "TournamentResult",
    "evaluate_strategy",
    "format_paired_result",
    "format_tournament",
    "make_grade_fn",
    "paired_compare",
    "smoke_inputs",
    "tournament",
]


#: The operator's real seat, ZERO-BASED, so nobody has to convert it twice.
#: CLAUDE.md says "Draft slot: 9 of 10"; ``run_draft``/``run_many`` index seats
#: from 0, so that seat is 8. Every ``slots=`` argument in this module is
#: zero-based for exactly that consistency.
OPERATOR_SLOT_2026 = 8

#: Positions reported in a roster shape, in a fixed order (never dict order).
SHAPE_POSITIONS = ("QB", "RB", "WR", "TE", "DST", "K")

DEFAULT_CONFIDENCE = 0.95

#: Bootstrap resamples for the distribution-free interval. 2,000 is enough for a
#: 95% percentile interval to be stable to ~0.003 wins at n=150 and costs ~0.3 s.
DEFAULT_BOOTSTRAP = 2000

#: What the documented cheap evaluation setting costs, MEASURED rather than
#: assumed (live board, slot 9, PickEngine vs FollowEspnRank, seed 1, n=30):
CHEAP_SETTING_NOTE = (
    "rollouts=512 is the draft-night setting; rollouts=128 is the documented "
    "CHEAP evaluation setting and is 3.7x faster (204 s -> 56 s for a 150-draft "
    "arm). What it costs, measured on the live board 2026-08-30 over the same "
    "150-pair grid (n=50 at seats 1, 5 and 9, seed 7): PickEngine(512) beats "
    "FollowEspnRank by +1.283 wins (95% CI +1.182..+1.383) and PickEngine(128) "
    "by +1.276 (+1.175..+1.378) — same sign, same size, both clear of zero. Head "
    "to head the two settings are +0.0061 wins apart with a CI of "
    "(-0.0023, +0.0145) that INCLUDES zero, and 138 of the 150 paired drafts are "
    "byte-identical. So: explore at 128, and re-confirm at 512 anything you "
    "intend to ship."
)


class EvaluationInputError(ValueError):
    """The harness was asked to compare something it cannot honestly compare.

    Refuse-rather-than-guess: a harness that silently grades against a diverged
    id space, or that reports a confidence interval computed from one draft,
    reads exactly like a real answer.
    """


# ========================================================================
#                       1.  the paired room
# ========================================================================


@dataclass(frozen=True)
class _SeatStream:
    """A picker bound to its OWN random stream.

    The whole of the pairing machinery. ``run_draft`` builds one
    :class:`PickContext` per pick carrying the draft's single shared ``rng``;
    this wrapper replaces that one field and delegates. Everything else the
    context carries — the shared :class:`BoardState`, the live own-roster list,
    the opponent-roster proxy — is passed through by reference, so the draft is
    the same draft in every respect except which stream this seat draws from.
    """

    inner: Picker
    rng: random.Random

    def pick(self, ctx: PickContext) -> str:
        return self.inner.pick(dc_replace(ctx, rng=self.rng))


def _seat_rng(draft_seed: int, seat: int) -> random.Random:
    """This seat's stream for this draft. Seeded from a string so it is stable
    across processes (``random.Random`` hashes str/bytes seeds with SHA-512;
    ``hash()`` of a tuple would be PYTHONHASHSEED-dependent)."""
    return random.Random(f"ziggurat-seat:{draft_seed}:{seat}")


def _draft_once(
    board: Sequence[BoardEntry],
    operator: Picker,
    *,
    slot: int,
    draft_seed: int,
    priors: RoomPriors,
    roster: RosterStructure,
    rounds: int,
    autodraft_count: int | None,
    paired_streams: bool,
) -> DraftResult:
    """One full draft with ``operator`` in seat ``slot`` and the production room.

    The room is built exactly as ``simulator.run_many`` builds it — the same
    ``_assign_autodrafters`` call on a fresh ``Random(draft_seed)``, the same
    ``AutodraftBot`` / ``RankNoiseBot`` split — so the autodraft seats are
    identical in both modes and, more importantly, identical between two
    strategies replaying the same ``draft_seed``.
    """
    teams = roster.teams
    room_rng = random.Random(draft_seed)
    autodrafters = _assign_autodrafters(
        room_rng,
        teams=teams,
        operator_slot=slot,
        fraction=priors.autodraft_fraction,
        autodraft_count=autodraft_count,
    )
    seats: list[Picker] = []
    for t in range(teams):
        if t == slot:
            seats.append(operator)
        elif t in autodrafters:
            seats.append(AutodraftBot())
        else:
            seats.append(RankNoiseBot(priors=priors))

    if paired_streams:
        pickers: list[Picker] = [_SeatStream(p, _seat_rng(draft_seed, t)) for t, p in enumerate(seats)]
        # Every seat now draws from its own stream, so this one is never read.
        # It is still passed because ``run_draft`` requires an rng, and it is a
        # FRESH stream rather than the partly-consumed ``room_rng`` so that a
        # future picker added outside this wrapper cannot silently inherit the
        # autodraft draw's position.
        shared = random.Random(draft_seed)
    else:
        # ``run_many``'s exact plumbing: one stream, already advanced by the
        # autodraft assignment, threaded through all ten seats.
        pickers = seats
        shared = room_rng

    return run_draft(board, pickers, rng=shared, roster=roster, rounds=rounds)


# ========================================================================
#                       2.  grading one draft
# ========================================================================

#: ``(roster entries, opponent rosters or None) -> SeasonGrade``. ``None``
#: opponents means "grade against the grader's modelled field" — the fixed
#: exogenous second opinion, not these rivals.
GradeFn = Callable[
    [Sequence[BoardEntry], Mapping[int, Sequence[BoardEntry]] | None], SeasonGrade
]


def make_grade_fn(
    weekly: Mapping[str, Mapping[int, float]],
    *,
    positions: Mapping[str, str] | None = None,
    roster: RosterStructure = DEFAULT_ROSTER,
    variance=None,
    regular_season_weeks: Iterable[int] = grader.DEFAULT_REGULAR_SEASON_WEEKS,
    playoff_weeks: Iterable[int] = grader.DEFAULT_PLAYOFF_WEEKS,
    playoff_teams: int = grader.DEFAULT_PLAYOFF_TEAMS,
    objective_playoff_weight: float = 0.0,
    objective_title_weight: float = 0.0,
    stream_kdst: bool = True,
) -> GradeFn:
    """Bind :func:`grader.grade_roster` to one weekly points map and league shape.

    Every argument is passed straight through. The defaults are the real league:
    weeks 1-14 head to head, 15-17 a six-team bracket, and the objective is
    ``expected_wins`` exactly (both blend weights 0.0).

    The bound function keeps ``weekly``, ``roster`` and the variance model as the
    SAME OBJECTS on every call, which is what lets the grader's ``FieldPool``
    cache (keyed on their ``id()``) hit — a 6x saving on the fixed-field second
    opinion.
    """
    reg = tuple(regular_season_weeks)
    po = tuple(playoff_weeks)

    def _grade(
        entries: Sequence[BoardEntry],
        opponents: Mapping[int, Sequence[BoardEntry]] | None,
    ) -> SeasonGrade:
        return grader.grade_roster(
            entries,
            weekly,
            roster=roster,
            opponent_rosters=opponents,
            variance=variance,
            regular_season_weeks=reg,
            playoff_weeks=po,
            playoff_teams=playoff_teams,
            positions=positions,
            objective_playoff_weight=objective_playoff_weight,
            objective_title_weight=objective_title_weight,
            stream_kdst=stream_kdst,
        )

    return _grade


@dataclass(frozen=True)
class DraftOutcome:
    """One graded draft, from one seat. The unit of a paired observation.

    ``rank`` is 1 (best) to ``teams``, computed by grading ALL ten rosters
    against their own nine rivals — which is the anti-cheat: the objective is
    zero-sum, so a seat that gains by leaving the room a worse board gains
    nothing in rank.
    """

    slot: int
    draft_seed: int
    objective: float
    expected_wins: float
    playoff_prob: float
    title_prob: float
    rank: int
    holes: int
    hole_weeks: tuple[int, ...]
    shape: Mapping[str, int]
    #: The SAME roster re-graded against the grader's fixed modelled field
    #: instead of these rivals. ``None`` when ``field_check=False``.
    field_objective: float | None = None


def _outcome(
    result: DraftResult,
    *,
    slot: int,
    draft_seed: int,
    grade: GradeFn,
    field_check: bool,
) -> DraftOutcome:
    """Grade every team in a finished draft and describe the operator's seat."""
    graded: dict[int, SeasonGrade] = {}
    for team in sorted(result.rosters):
        entries = result.rosters[team]
        rivals = {t: result.rosters[t] for t in sorted(result.rosters) if t != team}
        graded[team] = grade(list(entries), rivals)

    mine = graded[slot]
    # Ties take the better rank: a tie is not evidence of being behind.
    rank = 1 + sum(
        1 for t, g in graded.items() if t != slot and g.objective > mine.objective
    )
    shape = {p: 0 for p in SHAPE_POSITIONS}
    for pos, c in position_counts(result.rosters[slot]).items():
        shape[pos] = shape.get(pos, 0) + c

    field_obj = None
    if field_check:
        field_obj = grade(list(result.rosters[slot]), None).objective

    return DraftOutcome(
        slot=slot,
        draft_seed=draft_seed,
        objective=mine.objective,
        expected_wins=mine.expected_wins,
        playoff_prob=mine.playoff_prob,
        title_prob=mine.title_prob,
        rank=rank,
        holes=len(mine.hole_weeks),
        hole_weeks=tuple(mine.hole_weeks),
        shape=shape,
        field_objective=field_obj,
    )


# ========================================================================
#                       3.  the seed grid
# ========================================================================


def _seed_grid(seed: int, slots: Sequence[int], n: int) -> tuple[tuple[int, int, int], ...]:
    """``((slot, replicate, draft_seed), ...)`` — the shared experiment design.

    Derived per SLOT rather than from one running stream, so adding a slot or
    shortening ``slots`` never changes the draws another slot sees. Every
    strategy in a comparison replays this identical grid; that is the pairing.
    """
    grid: list[tuple[int, int, int]] = []
    for slot in slots:
        stream = random.Random(f"ziggurat-paired:{seed}:{slot}")
        for rep in range(n):
            grid.append((slot, rep, stream.getrandbits(64)))
    return tuple(grid)


def evaluate_strategy(
    board: Sequence[BoardEntry],
    weekly: Mapping[str, Mapping[int, float]],
    *,
    strategy: Picker,
    n: int,
    slots: Sequence[int] = (OPERATOR_SLOT_2026,),
    seed: int = 0,
    roster: RosterStructure = DEFAULT_ROSTER,
    priors: RoomPriors = ROOM_PRIORS_2025,
    rounds: int = ROUNDS,
    grade: GradeFn | None = None,
    autodraft_count: int | None = None,
    paired_streams: bool = True,
    field_check: bool = True,
) -> tuple[DraftOutcome, ...]:
    """Run one strategy over the shared seed grid and grade every draft.

    ``n`` is drafts PER SLOT. The returned tuple is in grid order (slots in the
    order given, replicates ascending) and is a pure function of the inputs.
    """
    slots = _check_design(board, weekly, n=n, slots=slots, roster=roster)
    grade_fn = grade if grade is not None else _default_grade(weekly, roster=roster)
    out: list[DraftOutcome] = []
    for slot, _rep, draft_seed in _seed_grid(seed, slots, n):
        result = _draft_once(
            board,
            strategy,
            slot=slot,
            draft_seed=draft_seed,
            priors=priors,
            roster=roster,
            rounds=rounds,
            autodraft_count=autodraft_count,
            paired_streams=paired_streams,
        )
        out.append(
            _outcome(
                result,
                slot=slot,
                draft_seed=draft_seed,
                grade=grade_fn,
                field_check=field_check,
            )
        )
    return tuple(out)


def _default_grade(weekly, *, roster: RosterStructure) -> GradeFn:
    positions = getattr(weekly, "positions", None)
    return make_grade_fn(weekly, positions=positions, roster=roster)


def _check_design(
    board: Sequence[BoardEntry],
    weekly: Mapping[str, Mapping[int, float]],
    *,
    n: int,
    slots: Sequence[int],
    roster: RosterStructure,
) -> tuple[int, ...]:
    """Refuse a design that cannot produce an honest number. Returns the slots."""
    if not weekly:
        raise EvaluationInputError(
            "the weekly points map is EMPTY — every roster would grade as a season "
            "of holes and the comparison would be a difference of two fabricated "
            "numbers. Check the as_of, season and source you built it with."
        )
    if n < 1:
        raise EvaluationInputError(f"n must be at least 1 draft per slot; got {n}")
    if not slots:
        raise EvaluationInputError(
            "slots is empty — pass the zero-based seat(s) to evaluate "
            f"(the operator's real seat is {OPERATOR_SLOT_2026}, i.e. 9 of 10)."
        )
    seen = sorted(set(int(s) for s in slots))
    if len(seen) != len(list(slots)):
        raise EvaluationInputError(f"slots must be unique; got {list(slots)}")
    bad = [s for s in seen if not 0 <= s < roster.teams]
    if bad:
        raise EvaluationInputError(
            f"slots {bad} are not seats in a {roster.teams}-team league. Slots are "
            f"ZERO-BASED here (the operator's 9-of-10 seat is {OPERATOR_SLOT_2026})."
        )
    # The divergence that grades every roster as empty without raising anywhere.
    grader.assert_board_coverage(board, weekly)
    return tuple(int(s) for s in slots)


# ========================================================================
#                       4.  paired statistics
# ========================================================================


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    tiny = 1e-300
    eps = 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lb + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_cdf(t: float, df: float) -> float:
    """P(T <= t) for Student's t with ``df`` degrees of freedom."""
    x = df / (df + t * t)
    tail = 0.5 * _betai(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def _t_ppf(p: float, df: float) -> float:
    """Inverse of :func:`_t_cdf` by bisection.

    stdlib only — scipy is not a dependency of this project and a normal
    quantile is anti-conservative at the n a smoke run uses (z=1.96 against
    t=2.26 at df=9 understates the interval by 15%).
    """
    lo, hi = -1.0e4, 1.0e4
    for _ in range(300):
        mid = (lo + hi) / 2.0
        if _t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an ascending sequence."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _t_interval(deltas: Sequence[float], confidence: float) -> tuple[float, float, float, float]:
    """``(mean, ci_low, ci_high, sd)`` — the paired t interval on the differences."""
    n = len(deltas)
    mean = statistics.fmean(deltas)
    if n < 2:
        return mean, float("-inf"), float("inf"), 0.0
    sd = statistics.stdev(deltas)
    if sd == 0.0:
        # Every pair returned the same difference. The interval is that point:
        # widening it would invent uncertainty that the experiment did not find.
        return mean, mean, mean, 0.0
    se = sd / math.sqrt(n)
    t = _t_ppf(0.5 + confidence / 2.0, n - 1)
    return mean, mean - t * se, mean + t * se, sd


def _bootstrap_interval(
    deltas: Sequence[float], confidence: float, *, resamples: int, rng: random.Random
) -> tuple[float, float]:
    """Seeded percentile bootstrap on the paired differences (assumption-free)."""
    n = len(deltas)
    if n < 2 or resamples < 1:
        return float("-inf"), float("inf")
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += deltas[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo_q = (1.0 - confidence) / 2.0
    return _quantile(means, lo_q), _quantile(means, 1.0 - lo_q)


# ========================================================================
#                       5.  the paired comparison
# ========================================================================


@dataclass(frozen=True)
class PairedResult:
    """A vs B over the same rooms. Every number here is in EXPECTED WINS.

    ``n`` is the TOTAL number of paired drafts (``n_per_slot`` x the number of
    slots), which is the n the confidence interval is computed on.
    """

    name_a: str
    name_b: str
    n: int
    slots: tuple[int, ...]
    mean_delta: float
    ci_low: float
    ci_high: float
    excludes_zero: bool
    win_rate: float
    median_delta: float
    sd_delta: float
    per_slot: Mapping[int, float]
    shape_a: Mapping[str, float]
    shape_b: Mapping[str, float]
    holes_a: float
    holes_b: float
    reasons: tuple[str, ...]

    # --- additive detail; all defaulted, the contract above is unchanged ---
    n_per_slot: int = 0
    confidence: float = DEFAULT_CONFIDENCE
    #: League size the comparison was run at, so the rendering can say "best of
    #: N" honestly when a caller passes a non-default ``roster=``.
    teams: int = DEFAULT_ROSTER.teams
    mean_a: float = 0.0
    mean_b: float = 0.0
    rank_a: float = 0.0
    rank_b: float = 0.0
    ties: int = 0
    #: Distribution-free percentile bootstrap interval on the same differences.
    boot_ci_low: float = 0.0
    boot_ci_high: float = 0.0
    #: The same rosters re-graded against the grader's fixed modelled field
    #: (identical for both arms), so a gain made purely by starving these rivals
    #: shows up as a mean near zero here. ``None`` when ``field_check=False``.
    field_mean_delta: float | None = None
    playoff_delta: float = 0.0
    title_delta: float = 0.0
    per_slot_n: Mapping[int, int] = dc_field(default_factory=dict)
    #: Every paired difference, in grid order — kept so a caller can re-analyse
    #: (a different interval, a sign test, a pooled meta-analysis) without paying
    #: for the drafts again.
    deltas: tuple[float, ...] = ()


def paired_compare(
    board: Sequence[BoardEntry],
    weekly: Mapping[str, Mapping[int, float]],
    *,
    a: Picker,
    b: Picker,
    name_a: str = "A",
    name_b: str = "B",
    n: int = 25,
    slots: Sequence[int] = (OPERATOR_SLOT_2026,),
    seed: int = 0,
    roster: RosterStructure = DEFAULT_ROSTER,
    priors: RoomPriors = ROOM_PRIORS_2025,
    rounds: int = ROUNDS,
    grade: GradeFn | None = None,
    autodraft_count: int | None = None,
    paired_streams: bool = True,
    confidence: float = DEFAULT_CONFIDENCE,
    bootstrap: int = DEFAULT_BOOTSTRAP,
    field_check: bool = True,
) -> PairedResult:
    """Compare strategy ``a`` against strategy ``b`` on identical rooms.

    ``n`` drafts are run at EACH slot in ``slots``, and each of those drafts is
    run twice — once with ``a`` in the seat and once with ``b`` — from the same
    ``draft_seed``. The reported delta is ``objective(a) - objective(b)``,
    positive meaning ``a`` is better.

    The single most important property, and the one to check first if a number
    here ever looks wrong: comparing a strategy against ITSELF must return
    ``mean_delta`` of exactly 0.0 with zero variance.
    """
    slots = _check_design(board, weekly, n=n, slots=slots, roster=roster)
    grade_fn = grade if grade is not None else _default_grade(weekly, roster=roster)
    common = dict(
        n=n,
        slots=slots,
        seed=seed,
        roster=roster,
        priors=priors,
        rounds=rounds,
        grade=grade_fn,
        autodraft_count=autodraft_count,
        paired_streams=paired_streams,
        field_check=field_check,
    )
    out_a = evaluate_strategy(board, weekly, strategy=a, **common)
    out_b = evaluate_strategy(board, weekly, strategy=b, **common)
    return build_paired_result(
        out_a,
        out_b,
        name_a=name_a,
        name_b=name_b,
        slots=slots,
        n_per_slot=n,
        seed=seed,
        confidence=confidence,
        bootstrap=bootstrap,
        paired_streams=paired_streams,
        roster=roster,
    )


def build_paired_result(
    out_a: Sequence[DraftOutcome],
    out_b: Sequence[DraftOutcome],
    *,
    name_a: str,
    name_b: str,
    slots: Sequence[int],
    n_per_slot: int,
    seed: int,
    confidence: float = DEFAULT_CONFIDENCE,
    bootstrap: int = DEFAULT_BOOTSTRAP,
    paired_streams: bool = True,
    roster: RosterStructure = DEFAULT_ROSTER,
) -> PairedResult:
    """Assemble a :class:`PairedResult` from two already-graded grids.

    Public because :func:`tournament` runs each strategy's grid ONCE and then
    forms every pairing from the stored outcomes — with k challengers that is
    k+1 arms instead of 2k, and the baseline arm is byte-identical in every
    pairing rather than merely equal in distribution.
    """
    if len(out_a) != len(out_b):
        raise EvaluationInputError(
            f"cannot pair {len(out_a)} outcomes against {len(out_b)} — the two arms "
            "did not run the same grid"
        )
    if not out_a:
        raise EvaluationInputError("nothing to compare: both arms are empty")
    for x, y in zip(out_a, out_b, strict=True):
        if x.slot != y.slot or x.draft_seed != y.draft_seed:
            raise EvaluationInputError(
                "the two arms are NOT paired: a comparison at "
                f"slot {x.slot}/seed {x.draft_seed} was lined up against "
                f"slot {y.slot}/seed {y.draft_seed}. Every delta would be a "
                "difference between two unrelated rooms."
            )

    deltas = [x.objective - y.objective for x, y in zip(out_a, out_b, strict=True)]
    mean, lo, hi, sd = _t_interval(deltas, confidence)
    boot_lo, boot_hi = _bootstrap_interval(
        deltas,
        confidence,
        resamples=bootstrap,
        rng=random.Random(f"ziggurat-boot:{seed}:{name_a}:{name_b}"),
    )
    wins = sum(1 for d in deltas if d > 0)
    ties = sum(1 for d in deltas if d == 0.0)

    per_slot: dict[int, float] = {}
    per_slot_n: dict[int, int] = {}
    by_slot: dict[int, list[float]] = {}
    for x, d in zip(out_a, deltas, strict=True):
        by_slot.setdefault(x.slot, []).append(d)
    for s in sorted(by_slot):
        per_slot[s] = statistics.fmean(by_slot[s])
        per_slot_n[s] = len(by_slot[s])

    shape_a = _mean_shape(out_a)
    shape_b = _mean_shape(out_b)
    holes_a = statistics.fmean([o.holes for o in out_a])
    holes_b = statistics.fmean([o.holes for o in out_b])

    field_delta = None
    if all(o.field_objective is not None for o in out_a) and all(
        o.field_objective is not None for o in out_b
    ):
        field_delta = statistics.fmean(
            [
                float(x.field_objective) - float(y.field_objective)  # type: ignore[arg-type]
                for x, y in zip(out_a, out_b, strict=True)
            ]
        )

    result = PairedResult(
        name_a=name_a,
        name_b=name_b,
        n=len(deltas),
        slots=tuple(sorted(set(int(s) for s in slots))),
        mean_delta=mean,
        ci_low=lo,
        ci_high=hi,
        excludes_zero=bool(lo > 0.0 or hi < 0.0),
        win_rate=wins / len(deltas),
        median_delta=statistics.median(deltas),
        sd_delta=sd,
        per_slot=per_slot,
        shape_a=shape_a,
        shape_b=shape_b,
        holes_a=holes_a,
        holes_b=holes_b,
        reasons=(),
        n_per_slot=n_per_slot,
        confidence=confidence,
        teams=roster.teams,
        mean_a=statistics.fmean([o.objective for o in out_a]),
        mean_b=statistics.fmean([o.objective for o in out_b]),
        rank_a=statistics.fmean([o.rank for o in out_a]),
        rank_b=statistics.fmean([o.rank for o in out_b]),
        ties=ties,
        boot_ci_low=boot_lo,
        boot_ci_high=boot_hi,
        field_mean_delta=field_delta,
        playoff_delta=statistics.fmean(
            [x.playoff_prob - y.playoff_prob for x, y in zip(out_a, out_b, strict=True)]
        ),
        title_delta=statistics.fmean(
            [x.title_prob - y.title_prob for x, y in zip(out_a, out_b, strict=True)]
        ),
        per_slot_n=per_slot_n,
        deltas=tuple(deltas),
    )
    return dc_replace(
        result, reasons=_paired_reasons(result, paired_streams=paired_streams, roster=roster)
    )


def _mean_shape(outcomes: Sequence[DraftOutcome]) -> Mapping[str, float]:
    return {
        p: statistics.fmean([float(o.shape.get(p, 0)) for o in outcomes])
        for p in SHAPE_POSITIONS
    }


def _shape_str(shape: Mapping[str, float]) -> str:
    return "  ".join(f"{p} {shape.get(p, 0.0):.2f}" for p in SHAPE_POSITIONS)


def _paired_reasons(
    r: PairedResult, *, paired_streams: bool, roster: RosterStructure
) -> tuple[str, ...]:
    """Plain language a football novice can act on (Rule 6), caveats included."""
    seats = ", ".join(f"{s + 1} of {roster.teams}" for s in r.slots)
    verdict = (
        "the interval EXCLUDES zero, so this is a real difference at the "
        f"{r.confidence:.0%} level"
        if r.excludes_zero
        else "the interval INCLUDES zero, so this experiment did NOT show a difference"
    )
    lines = [
        f"Compared {r.name_a} against {r.name_b} over {r.n} paired mock drafts "
        f"({r.n_per_slot} at each seat; seats {seats}).",
        (
            "PAIRED: both strategies faced the same rooms — same seeded rivals, "
            "same autodraft seats, and each seat drew from its own random stream "
            "so your strategy could not reshuffle the rivals just by thinking "
            "harder. The only difference between the two runs is who sat in your "
            "seat."
            if paired_streams
            else "NOT stream-paired (paired_streams=False): the rooms share a seed "
            "but one shared random stream, so a strategy that consumes randomness "
            "differently also changes what the rivals do. Read this as a noisier "
            "comparison, not a clean one."
        ),
        f"{r.name_a} is worth {r.mean_delta:+.3f} expected wins a season versus "
        f"{r.name_b} ({r.confidence:.0%} CI {r.ci_low:+.3f} to {r.ci_high:+.3f}) — {verdict}.",
        f"Distribution-free check (bootstrap): {r.boot_ci_low:+.3f} to {r.boot_ci_high:+.3f}. "
        "If this disagrees with the interval above, the differences are too skewed "
        "for the t interval and the bootstrap is the one to believe.",
        f"{r.name_a} came out ahead in {r.win_rate:.0%} of the paired drafts "
        f"(median difference {r.median_delta:+.3f}, spread {r.sd_delta:.3f}).",
        f"Average finish in the {roster.teams}-team league (1 = best): "
        f"{r.name_a} {r.rank_a:.2f}, {r.name_b} {r.rank_b:.2f}. Expected wins is "
        "zero-sum across the ten teams, so a strategy that only starves its rivals "
        "gains here by less than its own score suggests.",
        f"Weeks a season with a starting slot that could not be filled: "
        f"{r.name_a} {r.holes_a:.2f}, {r.name_b} {r.holes_b:.2f}.",
        f"Average roster shape over 16 picks — {r.name_a}: {_shape_str(r.shape_a)}",
        f"Average roster shape over 16 picks — {r.name_b}: {_shape_str(r.shape_b)}",
        f"Playoff odds {r.playoff_delta:+.3f}, title odds {r.title_delta:+.3f} "
        "(reported, never blended into the objective).",
    ]
    if r.field_mean_delta is not None:
        agrees = (r.field_mean_delta > 0) == (r.mean_delta > 0)
        lines.append(
            f"Second opinion against a FIXED modelled field (the same for both "
            f"arms, so it cannot be starved): {r.field_mean_delta:+.3f} — "
            + (
                "same sign as the head-to-head result."
                if agrees
                else "OPPOSITE sign to the head-to-head result, which means the gain "
                "came from what the rivals were left rather than from the roster."
            )
        )
    if len(r.per_slot) > 1:
        detail = ", ".join(
            f"seat {s + 1}: {v:+.3f} (n={r.per_slot_n.get(s, 0)})"
            for s, v in sorted(r.per_slot.items())
        )
        lines.append(f"By seat — {detail}.")
    lines += [
        "CAVEAT the objective has NO injury or availability model: it prices your "
        "starting nine and your bye structure, and a bench player is worth exactly "
        "zero to it unless he covers a bye (grader.py, 'what this objective still "
        "cannot see'). Do not read this as a ranking of late-round bench picks.",
        "CAVEAT the fixed-field second opinion uses a modelled field that runs 3-8 "
        "points a week too strong and never byes; read its SIGN, not its level.",
        f"CAVEAT the interval treats all {r.n} paired drafts as independent "
        "observations; the per-seat means above are printed so a seat effect is "
        "visible rather than averaged away.",
        "CAVEAT this is the room the 2.2 priors describe (reach noise, 20% "
        "autodraft, the fitted position-run curve), not the real room. A margin "
        "here is evidence about the model of the room, not a promise about Monday.",
    ]
    return tuple(lines)


def format_paired_result(result: PairedResult, *, reasons: bool = True) -> str:
    """Render a comparison for a human (Rule 6)."""
    r = result
    head = f"{r.name_a}  vs  {r.name_b}"
    w = max(18, len(r.name_a) + 6, len(r.name_b) + 6)

    def row(label: str, body: str) -> str:
        return f"  {label:<{w}}: {body}"

    seats = ", ".join(str(s + 1) for s in r.slots)
    lines = [
        head,
        "=" * len(head),
        row("paired drafts", f"{r.n}  ({r.n_per_slot} at each of "
                            f"{len(r.slots)} seat(s): {seats})"),
        row("mean difference", f"{r.mean_delta:+.3f} expected wins"),
        row(f"{r.confidence:.0%} interval",
            f"{r.ci_low:+.3f} .. {r.ci_high:+.3f}"
            f"   ({'excludes' if r.excludes_zero else 'includes'} zero)"),
        row("bootstrap", f"{r.boot_ci_low:+.3f} .. {r.boot_ci_high:+.3f}"),
        row("ahead in", f"{r.name_a} in {r.win_rate:.0%} of drafts"
                        + (f"  ({r.ties} exact ties)" if r.ties else "")),
        row("expected wins", f"{r.name_a} {r.mean_a:.3f}   {r.name_b} {r.mean_b:.3f}"),
        row("average finish", f"{r.name_a} {r.rank_a:.2f}   {r.name_b} {r.rank_b:.2f}"
                              f"   (1 = best of {r.teams})"),
        row("unfillable weeks", f"{r.name_a} {r.holes_a:.2f}   {r.name_b} {r.holes_b:.2f}"),
        row(f"shape {r.name_a}", _shape_str(r.shape_a)),
        row(f"shape {r.name_b}", _shape_str(r.shape_b)),
    ]
    if r.field_mean_delta is not None:
        lines.append(row("vs fixed field", f"{r.field_mean_delta:+.3f}"))
    if reasons:
        lines.append("")
        lines += [f"  - {t}" for t in r.reasons]
    return "\n".join(lines)


# ========================================================================
#                       6.  the tournament
# ========================================================================


@dataclass(frozen=True)
class TournamentResult:
    """Every strategy paired against one baseline on ONE shared seed grid."""

    baseline: str
    n: int
    slots: tuple[int, ...]
    #: challenger name -> its paired comparison against the baseline
    results: Mapping[str, PairedResult]
    #: challenger names, best mean delta first
    order: tuple[str, ...]
    reasons: tuple[str, ...]
    #: strategy name -> its graded outcomes, so a caller can re-pair any two
    #: challengers against each other without re-running a single draft
    outcomes: Mapping[str, tuple[DraftOutcome, ...]] = dc_field(default_factory=dict)


def tournament(
    board: Sequence[BoardEntry],
    weekly: Mapping[str, Mapping[int, float]],
    *,
    strategies: Mapping[str, Picker],
    baseline: str,
    n: int = 25,
    slots: Sequence[int] = (OPERATOR_SLOT_2026,),
    seed: int = 0,
    roster: RosterStructure = DEFAULT_ROSTER,
    priors: RoomPriors = ROOM_PRIORS_2025,
    rounds: int = ROUNDS,
    grade: GradeFn | None = None,
    autodraft_count: int | None = None,
    paired_streams: bool = True,
    confidence: float = DEFAULT_CONFIDENCE,
    bootstrap: int = DEFAULT_BOOTSTRAP,
    field_check: bool = True,
) -> TournamentResult:
    """Pair every strategy against ``baseline`` over ONE shared seed grid.

    Each strategy drafts the grid exactly once, so k challengers cost k+1 arms
    rather than 2k, and every challenger is compared against the *identical*
    baseline drafts rather than against a fresh sample of them.

    Strategies are iterated in the order the mapping gives them, and the seed
    grid does not depend on that order, so adding a strategy never changes an
    existing one's numbers.
    """
    if baseline not in strategies:
        raise EvaluationInputError(
            f"baseline {baseline!r} is not one of the strategies "
            f"({sorted(strategies)}) — there is nothing to compare against."
        )
    if len(strategies) < 2:
        raise EvaluationInputError(
            "a tournament needs a baseline and at least one challenger; got "
            f"{sorted(strategies)}"
        )
    slots = _check_design(board, weekly, n=n, slots=slots, roster=roster)
    grade_fn = grade if grade is not None else _default_grade(weekly, roster=roster)

    grids: dict[str, tuple[DraftOutcome, ...]] = {}
    for name, strat in strategies.items():
        grids[name] = evaluate_strategy(
            board,
            weekly,
            strategy=strat,
            n=n,
            slots=slots,
            seed=seed,
            roster=roster,
            priors=priors,
            rounds=rounds,
            grade=grade_fn,
            autodraft_count=autodraft_count,
            paired_streams=paired_streams,
            field_check=field_check,
        )

    results: dict[str, PairedResult] = {}
    for name in strategies:
        if name == baseline:
            continue
        results[name] = build_paired_result(
            grids[name],
            grids[baseline],
            name_a=name,
            name_b=baseline,
            slots=slots,
            n_per_slot=n,
            seed=seed,
            confidence=confidence,
            bootstrap=bootstrap,
            paired_streams=paired_streams,
            roster=roster,
        )

    order = tuple(sorted(results, key=lambda k: (-results[k].mean_delta, k)))
    winners = [k for k in order if results[k].excludes_zero and results[k].mean_delta > 0]
    reasons = (
        f"{len(strategies)} strategies over one shared grid of "
        f"{n} drafts at each of {len(slots)} seat(s); every challenger is paired "
        f"against the SAME {baseline} drafts, not a fresh sample of them.",
        (
            f"Beat {baseline} with an interval clear of zero: "
            + ", ".join(winners)
            if winners
            else f"NOTHING beat {baseline} with an interval clear of zero. That is a "
            "complete result: the baseline stands."
        ),
        "Read each comparison's own reasons before shipping one — the roster "
        "shape and the unfillable-week count can stay broken while the objective "
        "improves.",
    )
    return TournamentResult(
        baseline=baseline,
        n=n * len(slots),
        slots=tuple(slots),
        results=results,
        order=order,
        reasons=reasons,
        outcomes=grids,
    )


def format_tournament(result: TournamentResult, *, reasons: bool = True) -> str:
    """Render a tournament as a table a novice can read (Rule 6)."""
    lines = [
        f"Tournament vs {result.baseline} — {result.n} paired drafts "
        f"at seat(s) {', '.join(str(s + 1) for s in result.slots)}",
        "",
        f"  {'strategy':<22}{'delta':>9}{'interval':>22}{'ahead':>8}{'holes':>8}{'finish':>8}",
    ]
    base = next(iter(result.results.values()), None)
    for name in result.order:
        r = result.results[name]
        flag = "*" if r.excludes_zero else " "
        lines.append(
            f"{flag} {name:<22}{r.mean_delta:>+9.3f}"
            f"{f'{r.ci_low:+.3f} .. {r.ci_high:+.3f}':>22}"
            f"{r.win_rate:>7.0%}{r.holes_a:>8.2f}{r.rank_a:>8.2f}"
        )
    if base is not None:
        lines.append(
            f"  {result.baseline + ' (baseline)':<22}{0.0:>+9.3f}{'':>22}"
            f"{'':>7}{base.holes_b:>8.2f}{base.rank_b:>8.2f}"
        )
    lines.append("")
    lines.append("  * = the confidence interval excludes zero")
    if reasons:
        lines.append("")
        lines += [f"  - {t}" for t in result.reasons]
    return "\n".join(lines)


# ========================================================================
#                       7.  smoke mode
# ========================================================================


@dataclass(frozen=True)
class OffsetRankPicker:
    """A deterministic stub strategy: the ``offset``-th best legal player by rank.

    ``offset=0`` is exactly ``FollowEspnRank``. It touches no randomness at all,
    which is what makes it useful in a test: any variance a harness reports while
    both arms are ``OffsetRankPicker`` came from the harness, not the strategy.
    """

    offset: int = 0
    kdst_earliest_round: int = DEFAULT_KDST_EARLIEST_ROUND

    def pick(self, ctx: PickContext) -> str:
        counts = position_counts(ctx.own_roster)
        allowed = allowed_positions(
            counts,
            ctx.picks_after,
            ctx.roster,
            round_num=ctx.round,
            kdst_earliest_round=self.kdst_earliest_round,
        )
        window = ctx.state.window_by_rank(allowed, self.offset + 1)
        if not window:
            window = ctx.state.window_by_rank(SHAPE_POSITIONS, self.offset + 1)
        if not window:  # pragma: no cover - an exhausted board
            raise EvaluationInputError("draft board exhausted while picking")
        return window[min(self.offset, len(window) - 1)].player_id


#: How many players of each position the smoke board carries. Comfortably past
#: ``simulator._validate_board_supply``'s floors for a 10-team, 16-round draft.
#: (position, how many, best player's per-week rate, drop per rank). The counts
#: clear ``DEFAULT_POSITION_CAPS`` x ten teams at every capped position — QB and
#: TE are capped at 3, so a 24-deep QB board really can be drained dry by ten
#: teams and ``run_draft`` then raises "the board ran out of a required position"
#: (measured while building this).
_SMOKE_SUPPLY = (
    ("QB", 40, 22.0, 0.35),
    ("RB", 60, 20.0, 0.22),
    ("WR", 70, 19.0, 0.20),
    ("TE", 40, 14.0, 0.30),
    ("DST", 16, 9.5, 0.20),
    ("K", 16, 8.5, 0.15),
)

_SMOKE_WEEKS = tuple(range(1, 18))


def smoke_inputs(
    *, roster: RosterStructure = DEFAULT_ROSTER, weeks: Sequence[int] = _SMOKE_WEEKS
) -> tuple[tuple[BoardEntry, ...], grader.WeeklyPointsMap]:
    """A synthetic board + points map for fast, offline, deterministic runs.

    242 invented players on invented teams (Rule 5: no real identity anywhere),
    each with a flat per-week rate and one bye spread across weeks 5-14, so the
    map obeys the grader's missing-week convention exactly: a week absent means
    the player does not play it.

    Deliberately NOT random. Two strategies compared on this board differ for
    structural reasons — VOR ranks positions differently from raw points — which
    is what a smoke test wants: a real, reproducible, explainable difference
    that costs milliseconds.
    """
    weeks = tuple(int(w) for w in weeks)
    entries: list[BoardEntry] = []
    points: dict[str, dict[int, float]] = {}
    positions: dict[str, str] = {}
    names: dict[str, str] = {}
    teams: dict[str, str] = {}

    rows: list[tuple[str, str, float, int, str]] = []  # pid, pos, per_week, bye, team
    for pos, count, top, step in _SMOKE_SUPPLY:
        for i in range(count):
            pid = f"{pos}{i:03d}"
            per_week = round(top - step * i, 4)
            bye = 5 + ((i + len(pos)) % 10)  # weeks 5..14
            rows.append((pid, pos, per_week, bye, f"T{(i * 7 + len(pos)) % 32:02d}"))

    # Replacement level = the value of the last player any team would start, so
    # VOR orders positions differently from raw points (which is the whole point
    # of having both signals on the board).
    replacement: dict[str, float] = {}
    for pos, count, top, step in _SMOKE_SUPPLY:
        per_team = roster.starters.get(pos, 0) + (
            roster.flex_slots if pos in roster.flex_positions else 0
        )
        idx = min(max(per_team * roster.teams - 1, 0), count - 1)
        replacement[pos] = round(top - step * idx, 4)

    # ESPN rank: raw season points descending, ties broken by id (deterministic).
    scored = []
    for pid, pos, per_week, bye, team in rows:
        played = tuple(w for w in weeks if w != bye)
        scored.append((per_week * len(played), pid, pos, per_week, bye, team, played))
    scored.sort(key=lambda t: (-t[0], t[1]))

    for rank, (season, pid, pos, per_week, _bye, team, played) in enumerate(scored, start=1):
        entries.append(
            BoardEntry(
                player_id=pid,
                name=f"Smoke {pid}",
                position=pos,
                espn_overall_rank=rank,
                house_points=round(season, 4),
                vor=round(season - replacement[pos] * len(played), 4),
                team=team,
            )
        )
        points[pid] = {w: per_week for w in played}
        positions[pid] = pos
        names[pid] = f"Smoke {pid}"
        teams[pid] = team

    board = tuple(entries)
    weekly = grader.WeeklyPointsMap(points, positions=positions, names=names, teams=teams)
    return board, weekly
