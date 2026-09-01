"""Phase-2 variant: re-rank the engine's shortlist by the WEEK-BY-WEEK consequence.

Import-quarantined package (Rule 8). Nothing outside ``ziggurat/draft/`` imports this, and
this module imports no production draft file it could change: it WRAPS
:class:`~ziggurat.draft.engine.PickEngine` rather than editing it, so the shipped
engine, survival model, candidate generation and reason text are untouched and
``weight=0.0`` is provably the current engine (``test_weight_zero_is_the_engine``
pins it pick-for-pick, score-for-score, reason-for-reason).

THE VERDICT, UP FRONT, so nobody has to read to the bottom to find it. On two
HELD-OUT seeds at the operator's own seat (500 paired drafts, live 3,264-row
board), the fixed variant beats the unwrapped engine by **+0.044 expected wins,
95% CI +0.029 .. +0.060**, ahead in 54% of pairs. That is about one extra win
every twenty-three seasons for a change that moves the round-one pick in roughly
30% of drafts. **The margin is small, it is real, and — the finding that matters
most — a TWELVE-LINE CONTROL that adds a flat +10 to every running back's
``pick_score`` and never calls the grader at all gets +0.024 of it.** The variant
clears that control by +0.020 [+0.005, +0.035] and only since the completion
model was fixed; the version an audit reviewed in the morning of 2026-08-30 did
NOT clear it (its own contrast against the control measures -0.001 [-0.015,
+0.014] on the same grids). Read every claim below against that.

WHAT IS BROKEN, AND WHY WRAPPING FIXES IT
-----------------------------------------
``engine.pick_score`` is ADDITIVE over one player at a time::

    frac*vor + b_need*need_fill + frac*b_vona*urgency + risk

Every term is a property of the CANDIDATE and a coarse count of the roster
(``position_counts``). Nothing in it can see that your two starting running backs
are both off in week 8, that a third quarterback cannot enter a one-QB lineup in
any week, or that a season is fourteen separate head-to-head weeks rather than
one points total. Phase 1 measured the consequence three ways: the old
``simulator.optimal_starting_points`` metric is not merely blind to bye
collisions but BACKWARDS about them; the engine's own golden roster cannot fill
its nine starting slots in 3 of 14 H2H weeks; and every one of 25 simulated
drafts takes EXACTLY 3 QB and 3 TE — six of sixteen picks spent on two
one-starter lineup slots.

:func:`ziggurat.draft.grader.grade_roster` can see all of it, because it seats a
real lineup in every week. So this module asks the engine for its shortlist
(``recommend(ctx, top=K)``, which already carries the score and every number
behind it), grades the roster WITH each candidate against the roster without, and
blends that delta back into ``pick_score`` with one tunable weight.

THE FINDING THAT SHAPED THE DESIGN: A PARTIAL ROSTER IS BYE-BLIND
-----------------------------------------------------------------
The obvious implementation — grade ``ctx.own_roster`` and ``ctx.own_roster +
[candidate]`` — does not work, and it fails silently. Measured on the live 2026
board (2026-08-30, seat 9, after three picks: Gibbs bye 6, Smith-Njigba bye 11,
McCaffrey bye 8), the week-by-week marginal of eight available running backs:

    candidate            bye     vor   partial roster   completed roster
    Kyren Williams        11    34.9        188.3             +3.8
    David Montgomery       8    30.9        185.0            -11.6
    Josh Jacobs           11    29.7        183.4             -1.1
    Javonte Williams      14    28.0        182.1             -1.3
    Cam Skattebo           8    19.9        175.4            -21.2
    Jaylen Warren          9    14.5        171.5            -11.4
    Travis Etienne         8     3.7        163.2            -25.3
    Rico Dowdle            9     0.8        160.4            -14.6

The partial column is the raw points column with a different scale on it: it
ranks the backs in exactly VOR order, so Montgomery — whose bye collides with
McCaffrey's — grades ABOVE Jacobs and Javonte, who do not. The completed column
reverses every one of those pairs, and it reverses them by BYE: each bye-8 back
falls below a non-colliding back with LOWER VOR (Montgomery -11.6 under Jacobs
-1.1; Skattebo -21.2 under Warren -11.4; Etienne -25.3 under Dowdle -14.6).

The reason the naive form cannot see it is structural: a three-man roster has an
empty slot in every week, so every candidate is seated in every week he plays and
a collision costs nothing. The collision only has a price when the slots are
CONTESTED.

So the shipped default completes the roster first: every pick from this one to
the end of the draft is filled with the best-by-VOR player still expected to be
available at that pick, and the grade is of that whole hypothetical season — on
BOTH sides, so the difference is "this player instead of the one you would
otherwise take right now" rather than "this player instead of nothing".
``completion="none"`` keeps the naive form so the difference is measurable rather
than asserted; :func:`test_completion_is_what_makes_the_collision_visible` pins
both halves, and the A/B below measures what each is worth.

THE COMPLETION IS A MODEL, AND IT IS THE VARIANT'S BIGGEST ASSUMPTION
--------------------------------------------------------------------
It is a LABELLED HYPOTHESIS (:data:`COMPLETION_LABEL`), not a measurement. Two
halves, and the audit of 2026-08-30 corrected the first one:

* WHEN your remaining picks happen. This used to be a flat ``gap = teams``: your
  j-th remaining pick after ``j * 10`` more players are gone. **That is wrong at
  every seat except the middle one, and it is wrongest at ours.** A snake's turns
  ALTERNATE — from slot 9 of 10 the operator picks at overall 9 and 12, three
  apart, then waits seventeen for pick 29 — so at the round-one decision the flat
  model imagines a board ten players thinner than it will be, and at the
  round-two decision one seven players fatter. The audit measured what that costs:
  sweeping the flat gap over 6..14 changes the DRAFTED player in 16% of the
  operator's decisions, which is the size of the variant's entire effect on the
  pick. :func:`snake_offsets` now DERIVES the real offsets from ``round``,
  ``overall_pick`` and ``roster.teams`` (never a pick order, so any seat
  permutation works) and returns ``None`` — fall back to the flat model — rather
  than guessing when the context is not a consistent snake. It is checked against
  ``simulator.snake_sequence`` at all ten seats. Passing an explicit
  ``completion_gap`` selects the legacy flat model, which is how the A/B below
  measures what the fix is worth: **+0.020 expected wins [+0.004, +0.037] over
  the flat model, and it is the whole reason the variant clears the mechanism
  control.**
* WHO you take when you get there: the best VOR at a position that still needs
  filling. That is ``FollowVor`` with a need gate, so the completion is
  deliberately WEAKER than the engine being measured — it under-states how good
  your own later picks will be, which flatters no candidate in particular but
  does inflate the value of positions the greedy completion neglects. Measured at
  pick 32 the completion drafts 2 RB / 5 WR / 3 TE / 3 QB, so it leaves the RB2
  and FLEX slots thin and the RB marginal correspondingly high. Read a positional
  tilt in this variant's rosters against that, not as a discovery. This half is
  NOT fixed, and the section below says what it costs.

READ THE ORDERING, NOT THE LEVEL — BUT DO NOT READ THAT AS "THE GAP DOES NOT
MATTER". The first version of this module argued the completion was safe because
sweeping the gap left the ranking of eight available BACKS essentially fixed. That
is a within-position observation and the re-rank is CROSS-position; the audit
measured a different drafted player in 16% of decisions inside the same swept
range. Levels move by up to ~16 points because a different gap hands the baseline
a different default pick; a reason that quotes a level says what it is a
difference against.

THE MECHANISM CONTROL, WHICH IS THE MOST IMPORTANT NUMBER HERE
--------------------------------------------------------------
An audit asked the obvious question this module had never asked: how much of the
margin is the grader's week-by-week insight, and how much is just "take more
running backs"? The control is twelve lines — ask the engine for a shortlist, add
a flat +10 to every RB's ``pick_score``, re-rank; no grader, no completion, no
reasons, no latency. On the same 500 held-out paired drafts:

    arm                            vs the engine            holes  RB drafted
    weekwise, weight 2           +0.044  [+0.029, +0.060]    0.27      3.40
    nudge +10 on RB              +0.024  [+0.016, +0.033]    0.35      3.40
    weekwise, LEGACY flat gap    +0.024  [+0.008, +0.041]    0.19      3.52
    weekwise + waiver credit     +0.031  [+0.016, +0.046]    0.48      3.23
    engine (baseline)             0                          0.47      3.10

    direct contrast                        delta          95% CI
    weekwise(fixed)   - nudge10          +0.0199   +0.0049 .. +0.0349  *
    weekwise(flat)    - nudge10          -0.0005   -0.0153 .. +0.0144
    weekwise + credit - nudge10          +0.0064   -0.0085 .. +0.0213
    weekwise(fixed)   - weekwise(flat)   +0.0204   +0.0035 .. +0.0372  *

Read that table honestly, in three parts. (1) The audit's finding REPRODUCES on
held-out seeds: the variant as it was reviewed is statistically indistinguishable
from a one-line positional bonus, and it draws the same 3.40 running backs. (2)
The snake-offset fix is what separates them, and the separation is +0.020 wins
with an interval that clears zero by a hair. (3) The arm that prices an empty
skill week honestly does NOT clear the control. So the case for this variant over
a constant rests on one fix, one interval, and 500 drafts — not on the mechanism
story, which remains partly unproven.

WHAT THE OBJECTIVE CANNOT SEE (inherited whole from the grader, disclosed
because a novice cannot smell it)
------------------------------------------------------------------------
* NO INJURY OR AVAILABILITY MODEL, so a bench player is worth exactly 0.000
  unless he covers a bye. This variant is therefore structurally biased AGAINST
  bench depth, and the measured ~1-in-5 RB/WR seasons that lose 7+ games are
  invisible to it. ``ziggurat/core/availability.py`` is the fix and it plugs in
  at the ``grade`` seam, not here. :data:`BENCH_BLINDNESS_LABEL` now ships in
  ``PickRec.reasons`` wherever the term moved a pick — it previously lived only
  in ``format_adjustments``, a diagnostics helper no cockpit calls, so the
  disclosure reached the operator in 0 of 161 measured rendered reasons.
* AN EMPTY SKILL STARTING WEEK IS PRICED AT EXACTLY ZERO. ``grade_roster`` gives
  K and D/ST a waiver-tier credit and deliberately withholds it from
  QB/RB/WR/TE ("an empty starting slot costs exactly what it is worth: nothing.
  That is right for a skill slot" — ``grader.stream_levels``). In THIS league
  that is a modelling choice and this repo's own Tuesday cadence contradicts it:
  measured on the live board, the best unrostered player at each position is
  worth, per week, **QB 17.7 / RB 9.5 / WR 11.5 / TE 8.1 house points**. Since
  the variant is largely bought in removed holes, the objective OVER-PAYS it.
  ``hole_credit="waiver"`` (:data:`HOLE_CREDIT_LABEL`) corrects that inside this
  module, and the measurement is the interesting part: with holes priced
  honestly the variant **stops removing them** — 0.48 unfillable weeks a season
  against the engine's 0.47, where the uncorrected arm cuts them to 0.27 — and
  **still beats the engine, +0.031 [+0.016, +0.046]**. So the margin is not
  purely the metric's hole bonus, which is the sharpest thing that could have
  been said against it. It is also 0.014 wins WORSE than the
  uncorrected arm and no longer clears the mechanism control. The A/B metric
  itself is still the uncorrected one (``grader.py`` is not this module's to
  change), so both arms are graded by a referee that likes holes removed:
  :data:`HOLE_PRICING_WARNING`.

K AND D/ST ARE LEFT ALONE BY DEFAULT, AND THE FENCE IS INERT — BOTH ARE TRUE.
Phase 1 re-validated the K/DST divergence play in eleven real ten-team rooms
(first D/ST median pick 141; 0 of 100 rival seats took one before pick 90) and
ESPN ADP's relationship to real room timing COLLAPSES there, so ``adjust_positions``
defaults to the four skill positions. But the first version of this module claimed
the alternative arm had been measured to "delay them LESS", and that claim cannot
be true: re-measured 2026-08-30 on 20 drafts at seat 9, widening ``adjust_positions``
to include K and D/ST produces an **identical sixteen-pick sequence in 20 of 20
drafts**. The mechanism is that ``stream_kdst=True`` prices those slots off the
waiver tier either way, so of 63 K/DST candidates that reached a shortlist, 59
had a week-by-week marginal of exactly 0.0 and the other four were negative. The
fence therefore costs nothing and proves nothing; keep it, because it is free
insurance on a separately validated play, and do not cite the "K/DST included"
arm as evidence about kickers. (The K pick at overall 92 does still move in 5-8%
of drafts at weight 2 — not through the fence, but because the roster arriving at
round 10 is different.)

POSTURE. Wrapping the engine used to kill the 2.4 hysteresis posture monitor
silently; see :class:`WeekwisePicker`'s docstring for what ``posture`` actually
touches and what this class does about it.

COST, AND WHY THE GRADER IS CALLED WHERE IT IS
----------------------------------------------
Exactly ``len(shortlist) + 1`` :func:`grade_roster` calls per on-clock decision,
at the TOP LEVEL ONLY — never inside the survival rollout, which would multiply
the cost by ``rollouts``, and never inside a posture projection, which would
multiply it by thousands. The :class:`~ziggurat.draft.grader.WeeklyPointsMap` and
the :class:`~ziggurat.draft.grader.FieldPool` are built ONCE per session and
handed in (:class:`WeekwiseInputs`); rebuilding the pool per call is a measured 6x
regression. ``test_grade_calls_are_bounded_per_decision`` is the load-free cost
gate, in the style of ``test_draft_golden``'s rollout counters.

MEASURED 2026-08-30 as a PAIRED per-decision overhead — the wrapped engine's own
call is timed inside the wrapper's, so the number is a difference on ONE decision
rather than two runs' maxima compared across a loaded box (load average 12-18
here from other work, which is why the engine reads 208-229 ms against the 163.3
ms ``test_draft_golden`` records on an idle box). 160 real on-clock decisions at
``rollouts=512``, ``shortlist=12``:

    arm                          worst    p99    p95   median   replicate worst
    weight 2                      13.5   13.0   12.8      9.7        12.6
    weight 2 + waiver credit      15.2   14.9   13.8     10.3        12.1

The replicate ran at load average 2.8 rather than 12-18 and agrees, so the
overhead is the wrapper's own work and not the scheduler's. The projected idle
worst case is **163.3 + 13.5 = 176.8 ms against the 243 ms draft-night budget**,
and ``WeekwiseInputs.build`` is a one-off 5.6 ms.

WHAT IT MEASURED (2026-08-30, live 3,264-row board, ``as_of`` 2026-08-30, the
paired harness in :mod:`ziggurat.draft.evaluate`, room = ``ROOM_PRIORS_2025``,
``rollouts=128``, per-seat RNG streams, all arms against ONE shared engine grid)
-----------------------------------------------------------------------------
Two independent HELD-OUT seeds at the operator's own seat, 250 paired drafts
each, plus the pool:

    seed 101, n=250   weight 2  +0.045  [+0.023, +0.067]   holes 0.44 -> 0.26
    seed 137, n=250   weight 2  +0.044  [+0.021, +0.066]   holes 0.49 -> 0.28
    POOLED,   n=500   weight 2  +0.044  [+0.029, +0.060]   bootstrap +0.029 .. +0.059
                                ahead in 54%, 70 exact ties, +0.041 against the
                                fixed modelled field (same sign, so the gain is
                                not an artefact of starving these rivals)

And once across three seats, for comparability with the pre-fix build's headline
+0.090 (held-out seed 149, n=60 at each of seats 1, 5 and 9, 180 pairs):

    weight 2                  +0.075  [+0.049, +0.100]   holes 0.77 -> 0.36
    nudge +10 on RB           +0.053  [+0.036, +0.071]   holes 0.77 -> 0.44
    weight 2, LEGACY flat gap +0.066  [+0.036, +0.095]
    weight 2 + waiver credit  +0.059  [+0.034, +0.083]

with ``weekwise - nudge10`` at +0.022 [-0.002, +0.046] — the same point estimate
as the 500-draft seat-9 contrast, and at n=180 it does not clear zero. The
three-seat figure is bigger than the operator's own seat because the margin
tracks how many unfillable weeks the unwrapped engine was carrying there, and
seat 9 carries the fewest (0.47 a season against 0.77 pooled over the three).

Roster shape, pooled: QB 2.97 / RB 3.40 / WR 4.65 / TE 2.98 against the engine's
QB 2.98 / RB 3.10 / WR 4.94 / TE 2.98. **THE 3 QB / 3 TE CONCENTRATION IS
UNTOUCHED**, at every weight and every seat. What moves is RB against WR — and
the mechanism control moves it by exactly as much (RB 3.404 against 3.404), which
is the whole force of the control. This is a bye-structure fix and a one-dimensional
roster-shape fix, not the lineup-slot-concentration fix the Phase-1 diagnosis
also asked for; the objective prices a bench QB3 at zero either way.

THE WEIGHT IS STILL A PLATEAU, NOT A KNIFE EDGE, re-measured after the offsets
changed (held-out seed 163, n=150 at seat 9, against the same engine grid):

    weight 1   +0.026  [+0.003, +0.050]   holes 0.50 -> 0.39
    weight 2   +0.035  [+0.002, +0.067]   holes 0.50 -> 0.31
    weight 4   +0.032  [-0.005, +0.070]   holes 0.50 -> 0.11
    weight 8   +0.038  [+0.001, +0.076]   holes 0.50 -> 0.00

Four weights spanning an 8x range land within 0.012 wins of each other, and the
hole count falls monotonically across all four — which is what a real effect
looks like and what a tuned constant does not. Weight 2 stays the shipped default
(it was the pre-fix plateau's best point and is indistinguishable from the rest
of the plateau now); nothing here says it is optimal, only that the choice is not
load-bearing.

WHAT IT CHANGES ON THE NIGHT (2026-08-30, held-out seeds 211 and 223, n=60 each,
seat 9, ``rollouts=128``, the FIXED variant against the unwrapped engine)
-------------------------------------------------------------------------------
    pick        weight 2          weight 4
    overall 9   27% / 33%         32% / 35%
    overall 12  12% / 23%         27% / 27%
    overall 29  28% / 18%         40% / 30%
    overall 32  50% / 47%         53% / 60%
    overall 89   0% /  0%          0% /  2%     (the D/ST)
    overall 92   5% /  8%         18% / 20%     (the K)

The first version of this module reported 12% and 15% for the two round-one/two
picks. That number does not replicate — an audit measured roughly double it, and
so does this, on the fixed variant. **Roughly a third of the time, turning this
flag on changes who the operator takes with the ninth pick of the draft**, and
that is the fact to weigh against +0.044 expected wins.

RULE NOTES
----------
Rule 1: this module performs NO data read — the caller loads the board and the
points map through the accessors that carry ``as_of`` (``simulator.load_board``,
``grader.weekly_points_map``) and :meth:`WeekwiseInputs.build` re-checks the id
spaces agree (``grader.assert_board_coverage``) rather than grading every roster
as a season of holes. Rule 2: no scoring constant lives here; every point arrives
already priced through ``core/scoring.py``, and the waiver credit is read off the
grader's own board-derived ladders rather than written down. Rule 3: no CLI.
Rule 6: the week-by-week term adds a plain-language sentence to ``PickRec.reasons``
when — and only when — it moved that recommendation's place in the order, and it
now ships the bench-blindness disclosure alongside it; every sentence's ONE fact
about the named player is re-checked against the points map before it is
rendered. Rule 8: nothing outside ``ziggurat/draft/`` may import this.

DETERMINISM. :meth:`WeekwisePicker.recommend` consumes ``ctx.rng`` exactly as
``PickEngine.recommend`` does (one ``getrandbits(64)``, inside the wrapped call)
and everything this module adds is a pure function of the board, the roster and
the points map — no wall clock, no global random, no dict-order dependence (every
sort carries a ``player_id`` tiebreak). The cockpit's journal replay stays
bit-for-bit reproducible.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dc_replace

from ziggurat.core.lineup_support import VarianceModel
from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.draft import grader
from ziggurat.draft.bots import (
    POSITIONS,
    BoardEntry,
    PickContext,
    allowed_positions,
    position_counts,
)
from ziggurat.draft.engine import PickEngine, PickRec
from ziggurat.draft.grader import FieldPool, SeasonGrade

__all__ = [
    "BENCH_BLINDNESS_LABEL",
    "COMPLETION_LABEL",
    "CROWDING_NEEDS_NAIVE",
    "DEFAULT_ADJUST_POSITIONS",
    "DEFAULT_AVAILABLE_DEPTH",
    "DEFAULT_SHORTLIST",
    "HOLE_CREDITS",
    "HOLE_CREDIT_LABEL",
    "HOLE_PRICING_WARNING",
    "MODES",
    "POSTURE_CLONE_LABEL",
    "WeekwiseAdjustment",
    "WeekwiseInputs",
    "WeekwisePicker",
    "WeekwiseInputError",
    "complete_roster",
    "format_adjustments",
    "hole_credit_levels",
    "snake_offsets",
    "weekwise_adjustments",
]


# ------------------------------------------------------------------ priors
#
# STRATEGY PRIORS (not scoring — Rule 2). Every one is a documented, labelled
# hypothesis the A/B re-earns on the real board.

#: How many of the engine's own ranked candidates get re-graded. The engine's
#: candidate set is bounded (``candidate_width`` per allowed position plus the
#: best-by-VOR at each), so on the live board it holds at most ~36 players and a
#: shortlist of 12 covers every candidate the additive score rated close enough
#: for a week-by-week term to overturn. Each extra row costs one grade call
#: (~0.8 ms), so this is the variant's whole latency dial.
DEFAULT_SHORTLIST = 12

#: Positions the week-by-week term is allowed to move. K and D/ST are OUT by
#: default; see the module docstring's K/DST section — this is a guard on a
#: separately validated play, not an opinion about kickers.
DEFAULT_ADJUST_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE")

#: How deep into the still-available board the completion model looks. Must cover
#: ``picks_after * completion_gap`` (at most 15 * 10 = 150 on this board) plus
#: enough beyond it for the last pick to have a real choice at every position.
DEFAULT_AVAILABLE_DEPTH = 400

#: The labelled hypothesis the completion rests on (Rule 6 — it is quoted in the
#: reasons whenever the term moves a pick).
COMPLETION_LABEL = (
    "hypothesis (not measured): your remaining picks are modelled as the best "
    "value still on the board once the room has taken the players it is expected "
    "to take ahead of each of them"
)

#: How an EMPTY skill starting slot is priced. The grader deliberately prices an
#: empty QB/RB/WR/TE starting slot at exactly 0.0 while giving K and D/ST a
#: waiver-tier credit (``grader.stream_levels``: "an empty starting slot costs
#: exactly what it is worth: nothing. That is right for a skill slot"). In THIS
#: league that is a modelling choice, not a fact — the Tuesday waiver cadence
#: this repo already ships (``core/waiver.py``) would add the best unrostered
#: player at that position, and the variant's whole measured margin is bought in
#: the currency of removed holes. ``"waiver"`` prices a skill hole the same way
#: the grader already prices a K/DST hole, so the term stops being paid the full
#: value of a starter for covering a week the operator would have covered on
#: Tuesday for free.
HOLE_CREDITS: tuple[str, ...] = ("none", "waiver")

#: What ``hole_credit="waiver"`` assumes, quoted wherever it changed a number.
HOLE_CREDIT_LABEL = (
    "hypothesis (not measured): an empty skill starting slot is priced at the "
    "best player at that position who would still be unrostered if every team "
    "held its starters plus a flex — what the Tuesday waiver run would add — "
    "rather than at zero"
)

#: The limitation that survives even with the credit on, stated where a reader of
#: the numbers will meet it.
HOLE_PRICING_WARNING = (
    "the objective this variant maximises prices an empty QB/RB/WR/TE starting "
    "week at ZERO (grader.grade_roster), so it over-pays for removing one; "
    "hole_credit='waiver' corrects that inside this module only, and the A/B "
    "metric itself is still the uncorrected one"
)

#: What the objective cannot see, quoted wherever a number from it is reported.
BENCH_BLINDNESS_LABEL = (
    "this week-by-week score has NO injury model: a bench player is worth "
    "nothing in it unless he covers a bye, so it under-values depth"
)

#: The three ways the week-by-week delta can enter ``pick_score``.
#:
#: ``marginal_points``  + weight * (season points this pick adds to the seated
#:                      lineup, week by week). The shipped default: under the
#:                      completion it is already replacement-relative, because
#:                      the player it displaces is the pick you would otherwise
#:                      have made at that slot.
#: ``crowding``         - weight * (the player's own projected points MINUS what
#:                      the lineup can actually use). A pure penalty: it can
#:                      demote a candidate, never promote him above his own
#:                      value. This is the measured version of the engine's
#:                      hand-set ``_BENCH_VALUE_FRACTION``, and it is only
#:                      meaningful with ``completion="none"`` — see
#:                      :data:`CROWDING_NEEDS_NAIVE`.
#: ``marginal_wins``    + weight * (expected wins added). The literal objective,
#:                      and the one whose gradient collapses on a partial roster
#:                      (measured: 0.0028 wins for the best player on the board
#:                      at an empty roster, because you lose every week anyway).
MODES: tuple[str, ...] = ("marginal_points", "crowding", "marginal_wins")

#: Why ``crowding`` and the completion cannot be combined. ``crowded_out`` is
#: ``solo - marginal``, and it means "his production your lineup cannot use" ONLY
#: while ``marginal`` is measured against a roster that does not replace him. Under
#: the completion, ``marginal`` is measured against the player you would otherwise
#: take at this pick, so ``solo - marginal`` silently absorbs THAT player's whole
#: season and the "penalty" becomes a penalty on being a good player. The number
#: still looks like points, which is exactly why this is refused rather than
#: documented.
CROWDING_NEEDS_NAIVE = (
    "mode='crowding' prices a player's UNUSABLE production, which only means "
    "anything when the comparison roster does not already contain a replacement "
    "for him — i.e. with completion='none'. Under completion='future' the "
    "baseline spends this pick on somebody else, so solo minus marginal absorbs "
    "that player's entire season and the penalty grows with how GOOD the "
    "candidate's alternative is. Use mode='marginal_points' with the completion, "
    "or mode='crowding' with completion='none'."
)


class WeekwiseInputError(ValueError):
    """The variant was asked to re-rank something it cannot honestly re-rank.

    Refuse-rather-than-guess, the same discipline ``grader`` and ``evaluate``
    use: a diverged id space, an empty points map or a field pool that does not
    cover the graded weeks all read exactly like a real answer.
    """


# ========================================================================
#                       1.  the once-per-session inputs
# ========================================================================


@dataclass(frozen=True)
class WeekwiseInputs:
    """Everything the week-by-week term needs, built ONCE per draft session.

    ``weekly`` is :func:`grader.weekly_points_map`'s output (or any map on the
    SAME id space as the board); ``field`` is the roster-independent
    :class:`~ziggurat.draft.grader.FieldPool`, which costs 5.3 ms to build and is
    the reason this object exists rather than a pile of keyword arguments.

    Use :meth:`build` — it constructs the pool over exactly the weeks that will
    be graded and re-checks the board against the map, which is the failure that
    would otherwise grade every roster as a season of holes without raising.
    """

    weekly: Mapping[str, Mapping[int, float]]
    field: FieldPool
    roster: RosterStructure = DEFAULT_ROSTER
    regular_season_weeks: tuple[int, ...] = tuple(grader.DEFAULT_REGULAR_SEASON_WEEKS)
    playoff_weeks: tuple[int, ...] = tuple(grader.DEFAULT_PLAYOFF_WEEKS)
    playoff_teams: int = grader.DEFAULT_PLAYOFF_TEAMS
    variance: VarianceModel | None = None
    stream_kdst: bool = True

    @classmethod
    def build(
        cls,
        weekly: Mapping[str, Mapping[int, float]],
        *,
        board: Sequence[BoardEntry] | None = None,
        roster: RosterStructure = DEFAULT_ROSTER,
        regular_season_weeks: Iterable[int] = grader.DEFAULT_REGULAR_SEASON_WEEKS,
        playoff_weeks: Iterable[int] = grader.DEFAULT_PLAYOFF_WEEKS,
        playoff_teams: int = grader.DEFAULT_PLAYOFF_TEAMS,
        variance: VarianceModel | None = None,
        stream_kdst: bool = True,
    ) -> "WeekwiseInputs":
        """Build the pool over ``regular_season_weeks + playoff_weeks`` and check it.

        ``board``, when given, is checked against the points map: every PRICED
        entry must carry at least one week. That is
        :func:`grader.assert_board_coverage` wired up as a gate rather than left
        to a caller who will not run it.
        """
        if not weekly:
            raise WeekwiseInputError(
                "the weekly points map is EMPTY — every candidate would grade as a "
                "season of holes and the week-by-week term would be a difference of "
                "two fabricated numbers. Check the as_of, season and source."
            )
        positions = getattr(weekly, "positions", None)
        if positions is None:
            raise WeekwiseInputError(
                "the weekly points map carries no positions, so no opponent field "
                "and no streamed K/DST tier can be priced from it. Pass the "
                "WeeklyPointsMap that grader.weekly_points_map() returns."
            )
        reg = tuple(sorted({int(w) for w in regular_season_weeks}))
        po = tuple(sorted({int(w) for w in playoff_weeks}))
        if not reg:
            raise WeekwiseInputError(
                "regular_season_weeks is empty — there would be no season for the "
                "week-by-week term to look at."
            )
        if board is not None:
            grader.assert_board_coverage(board, weekly)
        pool = grader.build_field_pool(
            weekly, positions, weeks=tuple(sorted(set(reg) | set(po))),
            roster=roster, variance=variance,
        )
        return cls(
            weekly=weekly,
            field=pool,
            roster=roster,
            regular_season_weeks=reg,
            playoff_weeks=po,
            playoff_teams=playoff_teams,
            variance=variance,
            stream_kdst=stream_kdst,
        )

    def grade(self, entries: Sequence[BoardEntry]) -> SeasonGrade:
        """Grade one hypothetical roster against the fixed modelled field.

        The rivals are the grader's own snake-dealt field rather than
        ``ctx.opponent_rosters``: in the two points-based modes the opponents do
        not enter the quantity at all (``weekly_means`` is a property of YOUR
        seated lineup), and the real-rivals path costs 2.9 ms against 0.8 ms.
        """
        return grader.grade_roster(
            entries,
            self.weekly,
            roster=self.roster,
            field=self.field,
            variance=self.variance,
            regular_season_weeks=self.regular_season_weeks,
            playoff_weeks=self.playoff_weeks,
            playoff_teams=self.playoff_teams,
            stream_kdst=self.stream_kdst,
        )

    def solo_points(self, player_id: str) -> float:
        """The player's OWN projected points across the graded regular season.

        Absent weeks are absent, not zero (the grader's missing-week convention):
        a bye contributes nothing here for the same reason it contributes nothing
        to a lineup.
        """
        row = self.weekly.get(player_id) or {}
        return float(sum(row.get(w, 0.0) for w in self.regular_season_weeks))

    def plays(self, player_id: str, week: int) -> bool:
        return week in (self.weekly.get(player_id) or {})


# ========================================================================
#                       2.  the roster completion model
# ========================================================================


def _startable(pos: str, counts: Mapping[str, int], roster: RosterStructure) -> bool:
    """True when a ``pos`` pick can still reach the starting lineup.

    Deliberately the same rule ``engine._startable_now`` uses (an open dedicated
    slot, or flex eligibility with the flex not yet covered by surplus). It is
    re-derived here rather than imported because ``engine`` exports it privately
    and this module is forbidden from editing that file; ``test_startable_matches_the_engine``
    pins the two together so a drift in either fails loudly.
    """
    if counts.get(pos, 0) < roster.starters.get(pos, 0):
        return True
    if pos in roster.flex_positions:
        surplus = sum(
            max(0, counts.get(p, 0) - roster.starters.get(p, 0)) for p in roster.flex_positions
        )
        if surplus < roster.flex_slots:
            return True
    return False


@dataclass(frozen=True)
class _FuturePool:
    """Who is still expected to be on the board at each of your remaining picks.

    ``by_pick[j][pos]`` is the best-VOR available players at ``pos`` once roughly
    ``j * gap`` more players have come off the top of the ROOM's board — several
    of them, so a completion that has already used one can take the next. Built
    once per decision and shared by every candidate, which is what keeps the
    completion cheap (measured 0.09 ms per candidate).
    """

    by_pick: tuple[Mapping[str, tuple[BoardEntry, ...]], ...]
    gap: int
    depth: int
    #: How far down the still-available board each pick of the plan reaches, in
    #: players. ``offsets[0]`` is always 0 (the pick on the clock). Recorded so a
    #: caller can see whether the SNAKE model or the flat ``gap`` produced them.
    offsets: tuple[int, ...] = ()
    #: "snake" (offsets derived from this seat's real alternating turn order) or
    #: "flat" (the legacy ``j * gap``).
    offset_model: str = "flat"

    @property
    def picks(self) -> int:
        return len(self.by_pick)


#: How many alternates to keep per (pick, position). A completion takes at most
#: one player per pick, but consecutive picks draw from NESTED pools, so an early
#: pick can consume the front of a later pick's row. Eight covers a full
#: sixteen-pick completion at the deepest position (measured: no completion on
#: the live board is ever truncated, ``test_a_completion_is_never_truncated``).
_ALTERNATES = 8


def snake_offsets(ctx: PickContext, *, picks: int) -> tuple[int, ...] | None:
    """Rival picks between the pick on the clock and each of your remaining picks.

    THE FLAT GAP IS WRONG AT EVERY SEAT EXCEPT THE MIDDLE ONE, and it is wrongest
    at ours. A snake's turns ALTERNATE: from slot 9 of 10 the operator picks at
    overall 9 and 12 — three apart — then waits seventeen for pick 29. A flat
    ``gap = teams`` tells the completion that ten players go before its next pick
    in both cases, so at the round-one decision it models a board that is ten
    players thinner than it will be and at the round-two decision one that is
    seven players fatter. The audit measured the consequence: sweeping the flat
    gap over 6..14 changes the DRAFTED player in 16% of the operator's decisions,
    which is the size of the variant's whole effect on the pick.

    This derives the real offsets instead of choosing a gap. Only ``round``,
    ``overall_pick`` and ``roster.teams`` are read — never a pick order — so it
    works under any seat permutation, and it returns ``None`` (fall back to the
    flat gap) rather than guessing whenever the context is not a consistent
    snake, which is the case for hand-built unit-test contexts.

    ``offsets[j]`` counts RIVAL picks only: your own intervening picks are spent
    by the completion itself, which marks them used, so counting them here would
    remove them from the board twice.
    """
    teams = int(getattr(ctx.roster, "teams", 0) or 0)
    if teams < 1 or picks < 1 or ctx.round < 1:
        return None
    position = ctx.overall_pick - (ctx.round - 1) * teams
    if not 1 <= position <= teams:
        return None
    odd_position = position if ctx.round % 2 == 1 else teams + 1 - position
    out: list[int] = []
    for j in range(picks):
        rnd = ctx.round + j
        pos = odd_position if rnd % 2 == 1 else teams + 1 - odd_position
        out.append((rnd - 1) * teams + pos - ctx.overall_pick - j)
    if out[0] != 0 or any(b < a for a, b in zip(out, out[1:])):
        return None  # pragma: no cover - unreachable for a consistent snake
    return tuple(out)


def _future_pool(
    ctx: PickContext,
    *,
    picks: int,
    gap: int,
    depth: int = DEFAULT_AVAILABLE_DEPTH,
    offsets: Sequence[int] | None = None,
) -> _FuturePool:
    """Build the shared :class:`_FuturePool` from the live board state.

    ``ctx.state.window_by_rank`` is the public read (it only advances the lazy
    per-position "skip the drafted" heads, never the draft itself). The suffixes
    shrink as ``j`` grows, so the running top-``_ALTERNATES`` is accumulated from
    the LAST pick backwards and each earlier pick merely adds the slice of board
    between the two offsets — O(depth), not O(depth * picks).

    THE LAST PICK SEES THE WHOLE TAIL, not the slice between its own offset and
    the next one. Getting that wrong is silent and expensive: the running pool
    starts EMPTY, so the last pick would be offered only the ten players between
    two offsets, and the completion — which fills bottom-up — would then hand
    every candidate a nearly identical late roster. Measured while building this:
    two different candidates came back with byte-identical marginal points.
    """
    if picks < 1:
        return _FuturePool(by_pick=(), gap=gap, depth=depth, offsets=(),
                           offset_model="snake" if offsets is not None else "flat")
    avail = ctx.state.window_by_rank(POSITIONS, depth)
    # window_by_rank already sorts by ESPN rank; the player_id tiebreak makes the
    # order total even if two entries share a rank (the unranked sentinel band).
    avail = sorted(avail, key=lambda e: (e.espn_overall_rank, e.player_id))

    # ``starts[j]`` is how far down the board pick ``j`` of this plan reaches.
    # INDEX 0 IS THE PICK ON THE CLOCK — offset 0, because everyone available now
    # really is available now — and index j is your j-th REMAINING pick, after
    # roughly ``j * gap`` more players are gone. Getting that off by one in the
    # other direction (index 0 = your NEXT pick at offset 0) lets the completion
    # re-take the very player a candidate is being compared against, and two
    # candidates it would have collected EITHER WAY then come back with
    # byte-identical marginals (measured on the live board: a TE and a WR both at
    # exactly +31.2).
    raw = list(offsets[:picks]) if offsets is not None else [j * gap for j in range(picks)]
    while len(raw) < picks:  # pragma: no cover - offsets are built at this length
        raw.append(raw[-1] if raw else 0)
    starts = [min(max(0, int(o)), len(avail)) for o in raw]
    running: dict[str, list[BoardEntry]] = {}
    snapshots: list[Mapping[str, tuple[BoardEntry, ...]]] = [{} for _ in range(picks)]

    def _add(entry: BoardEntry) -> None:
        row = running.setdefault(entry.position, [])
        row.append(entry)
        row.sort(key=lambda e: (-e.vor, e.espn_overall_rank, e.player_id))
        del row[_ALTERNATES:]

    for entry in avail[starts[-1] :]:
        _add(entry)
    snapshots[picks - 1] = {pos: tuple(row) for pos, row in running.items()}
    for j in range(picks - 2, -1, -1):
        for entry in avail[starts[j] : starts[j + 1]]:
            _add(entry)
        snapshots[j] = {pos: tuple(row) for pos, row in running.items()}
    return _FuturePool(
        by_pick=tuple(snapshots),
        gap=gap,
        depth=depth,
        offsets=tuple(starts),
        offset_model="snake" if offsets is not None else "flat",
    )


def complete_roster(
    roster_entries: Sequence[BoardEntry],
    pool: _FuturePool,
    *,
    picks_left: int,
    roster: RosterStructure = DEFAULT_ROSTER,
    first_pick: int = 0,
) -> tuple[BoardEntry, ...]:
    """Fill ``picks_left`` picks with the labelled completion model.

    Need-first: among the positions that are legal, under cap and can still reach
    the starting lineup, take the best VOR expected to be available at that pick;
    once nothing is startable, take the best VOR that is merely legal. Returns the
    WHOLE roster (the given entries first, in order), so the caller can grade it
    directly. Deterministic: every choice is broken by ``(-vor, rank, player_id)``.

    ``first_pick`` selects where in the pool to start. The candidate branch passes
    1 — the pick on the clock has just been spent on the candidate — while the
    baseline branch passes 0 and spends it on the completion's own choice. That is
    what keeps the two rosters the SAME SIZE and makes the difference between them
    "this player instead of the one you would otherwise take right now" rather
    than "this player instead of a sixteenth-round scrap".
    """
    out = list(roster_entries)
    used = {e.player_id for e in out}
    for j in range(first_pick, first_pick + picks_left):
        if j >= pool.picks:
            break
        counts = position_counts(out)
        allowed = allowed_positions(
            counts, first_pick + picks_left - j - 1, roster, round_num=None
        )
        available = pool.by_pick[j]
        needed = {p for p in allowed if _startable(p, counts, roster)}
        best: BoardEntry | None = None
        for preference in (needed, allowed):
            if not preference:
                continue
            for pos in sorted(preference):
                for entry in available.get(pos, ()):
                    if entry.player_id in used:
                        continue
                    if best is None or (-entry.vor, entry.espn_overall_rank, entry.player_id) < (
                        -best.vor, best.espn_overall_rank, best.player_id
                    ):
                        best = entry
                    break
            if best is not None:
                break
        if best is None:
            break
        out.append(best)
        used.add(best.player_id)
    return tuple(out)


# ========================================================================
#                       3.  the adjustment
# ========================================================================


@dataclass(frozen=True)
class WeekwiseAdjustment:
    """What the week-by-week term saw for ONE candidate, and what it did with it.

    Every field is a number or a week list a human can be shown; the reasons the
    picker renders are built from exactly these, so a displayed sentence can
    always be traced back to one of them.
    """

    player_id: str
    position: str
    name: str | None
    engine_rank: int              # 0-based place in the engine's own order
    engine_score: float
    #: Σ over the graded regular season of the points this pick adds to the
    #: SEATED lineup, week by week (the completion is included on both sides).
    marginal_points: float
    #: The player's own projected points across the same weeks.
    solo_points: float
    #: ``max(0, solo_points - marginal_points)`` — his production the lineup
    #: cannot use, because a better player is already in that slot.
    crowded_out: float
    #: Expected wins added. Reported always; only ``mode="marginal_wins"`` acts on it.
    marginal_wins: float
    #: Regular-season weeks in which HE removes an empty starting slot: the slot
    #: was one he could be seated in, and he PLAYS that week. A week that merely
    #: stopped being a hole (the modelled completion covered it with a different
    #: later pick) is in :attr:`closed_downstream_weeks` instead — see the guard
    #: in :func:`weekwise_adjustments`.
    filled_hole_weeks: tuple[int, ...]
    #: Regular-season weeks he does NOT play in which the lineup is STILL short at
    #: a slot he could have filled — the bye collision, stated as a week list.
    collision_weeks: tuple[int, ...]
    adjustment: float             # what was added to pick_score (0.0 when it does not apply)
    applies: bool                 # False for a position outside ``adjust_positions``
    #: "future" or "none" — WHAT ``marginal_points`` is measured against, and
    #: therefore which sentences about it are true. Under "future" it is measured
    #: against the player you would otherwise take at this very pick; under "none"
    #: against not making the pick at all.
    completion: str = "future"
    #: Weeks that stopped being holes on the candidate's side but NOT because of
    #: him (he is off that week, or the empty slot was one he cannot occupy).
    #: Never rendered as his doing; carried so the diagnostics can show that the
    #: completion, not the candidate, is what moved.
    closed_downstream_weeks: tuple[int, ...] = ()
    #: How much ``marginal_points`` was reduced by pricing empty skill weeks at
    #: the waiver tier (:data:`HOLE_CREDIT_LABEL`). 0.0 under ``hole_credit="none"``.
    hole_credit_delta: float = 0.0

    @property
    def usable_share(self) -> float:
        """``marginal_points / solo_points`` — the share of his own production the
        lineup can use. MEANINGFUL ONLY under ``completion="none"``; under the
        completion the denominator is his season and the numerator is a difference
        against another player's, so the ratio is not a share of anything."""
        if self.solo_points <= 0.0:
            return 0.0
        return self.marginal_points / self.solo_points


def _slots_for(position: str, roster: RosterStructure) -> frozenset[str]:
    """The lineup slot labels a player of ``position`` could be seated in.

    ``core.lineup`` labels dedicated slots ``QB``/``RB1``/``RB2``/``WR1``/``WR2``/
    ``TE``/``DST``/``K`` and the flex ``FLEX``; a position with several dedicated
    slots is numbered from 1.
    """
    labels: set[str] = set()
    count = roster.starters.get(position, 0)
    if count == 1:
        labels.add(position)
    else:
        labels.update(f"{position}{i}" for i in range(1, count + 1))
    if position in roster.flex_positions and roster.flex_slots:
        labels.add("FLEX")
    return frozenset(labels)


def _slot_position(slot: str, roster: RosterStructure) -> str | None:
    """The position a lineup slot label belongs to; ``None`` for the flex.

    ``core.lineup`` numbers a multi-slot position ("RB1"/"RB2") and leaves a
    single one bare ("QB"), so the label is the position with its trailing digits
    stripped. "FLEX" belongs to no one position and is handled by the caller.
    """
    if slot == "FLEX":
        return None
    base = slot.rstrip("0123456789")
    return base if base in roster.starters else None


def hole_credit_levels(inputs: "WeekwiseInputs") -> dict[int, dict[str, float]]:
    """``week -> position -> points`` the Tuesday waiver run would supply.

    The same rule :func:`grader.stream_levels` applies to K and D/ST, applied to
    the skill positions the grader leaves at zero: the best player at that
    position who would still be unrostered if every one of the ten teams held its
    starters plus a flex. Read off the :class:`~ziggurat.draft.grader.FieldPool`
    ladders that are already built once per session, so it costs nothing per
    decision.

    THE TWO WAYS THIS IS APPROXIMATE, named rather than smoothed over: a week
    with TWO empty slots at one position is credited the same level twice (the
    real second add is one rung further down), and the per-team depth is the
    roster structure's rather than the shape the room actually drafts (Phase 1
    measured QB3/RB3/WR5/TE3, which is deeper and would credit LESS). Both push
    the credit UP, i.e. both make the correction to the variant's margin larger,
    not smaller.
    """
    roster = inputs.roster
    streamed = frozenset(getattr(grader, "STREAMED_POSITIONS", ("K", "DST")))
    out: dict[int, dict[str, float]] = {}
    for wk, ladder in inputs.field.ladders.items():
        row: dict[str, float] = {}
        for pos in roster.starters:
            if pos in streamed:
                continue  # grader.stream_levels already prices these
            rows = ladder.get(pos, ())
            if not rows:
                continue
            per_team = roster.starters.get(pos, 0) + (
                roster.flex_slots if pos in roster.flex_positions else 0
            )
            idx = min(max(roster.teams * per_team, 0), len(rows) - 1)
            row[pos] = max(0.0, float(rows[idx][0]))
        if row:
            out[int(wk)] = row
    return out


def _hole_credit_total(
    grade: SeasonGrade,
    *,
    weeks: frozenset[int],
    levels: Mapping[int, Mapping[str, float]],
    roster: RosterStructure,
) -> float:
    """What the empty skill slots of ``grade`` would be worth off the waiver wire."""
    total = 0.0
    for wk, slots in grade.hole_detail:
        if int(wk) not in weeks:
            continue
        row = levels.get(int(wk), {})
        if not row:
            continue
        for slot in slots:
            pos = _slot_position(slot, roster)
            if pos is None:  # FLEX: whichever eligible position is best that week
                total += max(
                    (row[p] for p in roster.flex_positions if p in row), default=0.0
                )
            else:
                total += row.get(pos, 0.0)
    return total


def _mode_adjustment(mode: str, weight: float, adj_points: float, crowded: float,
                     adj_wins: float) -> float:
    if mode == "marginal_points":
        return weight * adj_points
    if mode == "crowding":
        return -weight * crowded
    if mode == "marginal_wins":
        return weight * adj_wins
    raise WeekwiseInputError(  # pragma: no cover - guarded at construction
        f"unknown mode {mode!r}; expected one of {MODES}"
    )


def weekwise_adjustments(
    recs: Sequence[PickRec],
    ctx: PickContext,
    *,
    inputs: WeekwiseInputs,
    weight: float,
    mode: str = "marginal_points",
    completion: str = "future",
    completion_gap: int | None = None,
    adjust_positions: Sequence[str] = DEFAULT_ADJUST_POSITIONS,
    available_depth: int = DEFAULT_AVAILABLE_DEPTH,
    hole_credit: str = "none",
) -> tuple[WeekwiseAdjustment, ...]:
    """Grade every shortlisted candidate week by week. PURE — nothing is mutated.

    One grade of the roster as it stands (completed, when ``completion="future"``)
    plus one grade per candidate: ``len(recs) + 1`` calls, no more, ever. The
    returned tuple is in the SAME order as ``recs``; ranking is the caller's job.
    """
    if mode not in MODES:
        raise WeekwiseInputError(f"unknown mode {mode!r}; expected one of {MODES}")
    if completion not in ("future", "none"):
        raise WeekwiseInputError(
            f"unknown completion {completion!r}; expected 'future' (the shipped "
            "model) or 'none' (the measured-bye-blind naive form)"
        )
    if mode == "crowding" and completion != "none":
        raise WeekwiseInputError(CROWDING_NEEDS_NAIVE)
    if not math.isfinite(weight):
        raise WeekwiseInputError(f"weight must be a finite number; got {weight!r}")
    if hole_credit not in HOLE_CREDITS:
        raise WeekwiseInputError(
            f"unknown hole_credit {hole_credit!r}; expected one of {HOLE_CREDITS}"
        )

    roster = inputs.roster
    reg = inputs.regular_season_weeks
    adjust = frozenset(adjust_positions)
    own = tuple(ctx.own_roster)
    picks_left = ctx.picks_after

    pool: _FuturePool | None = None
    if completion == "future":
        # picks_left + 1: index 0 is the pick on the clock, 1.. are what follows.
        picks = picks_left + 1
        offsets: tuple[int, ...] | None = None
        if completion_gap is None:
            # The shipped model: this seat's REAL alternating snake turns. An
            # explicit completion_gap is the legacy flat model, kept so the A/B
            # can measure the difference rather than assert it.
            offsets = snake_offsets(ctx, picks=picks)
            gap = roster.teams
        else:
            gap = int(completion_gap)
        if gap < 1:
            raise WeekwiseInputError(
                f"completion_gap must be at least 1 player per remaining pick; got {gap}"
            )
        pool = _future_pool(
            ctx, picks=picks, gap=gap, depth=available_depth, offsets=offsets
        )

    def _hypothetical(extra: BoardEntry | None) -> tuple[BoardEntry, ...]:
        """The whole hypothetical 16-man season, with or without this candidate.

        BOTH BRANCHES SPEND THE PICK ON THE CLOCK — the candidate branch on the
        candidate, the baseline branch on whoever the completion would have taken
        instead — so the two rosters are the same size and the difference is the
        value of THIS pick going to THIS player. Letting the baseline simply skip
        the pick (measured while building this: every graded roster came back one
        player short) turns the reported marginal into "this player against a
        sixteenth-round scrap", which flatters every candidate and flatters them
        unevenly, by position.
        """
        if pool is None:
            return own + ((extra,) if extra is not None else ())
        if extra is None:
            return complete_roster(
                own, pool, picks_left=picks_left + 1, roster=roster, first_pick=0
            )
        return complete_roster(
            own + (extra,), pool, picks_left=picks_left, roster=roster, first_pick=1
        )

    base_grade = _grade(inputs, _hypothetical(None))
    base_means = base_grade.weekly_means
    base_holes = frozenset(base_grade.hole_weeks)
    base_hole_slots = {int(w): frozenset(sl) for w, sl in base_grade.hole_detail}
    reg_set = frozenset(reg)
    levels: Mapping[int, Mapping[str, float]] = {}
    base_credit = 0.0
    if hole_credit == "waiver":
        levels = hole_credit_levels(inputs)
        base_credit = _hole_credit_total(
            base_grade, weeks=reg_set, levels=levels, roster=roster
        )

    out: list[WeekwiseAdjustment] = []
    for rank, rec in enumerate(recs):
        entry = rec.player
        grade = _grade(inputs, _hypothetical(entry))
        adj_points = sum(
            _week_mean(grade.weekly_means, w) - _week_mean(base_means, w) for w in reg
        )
        credit_delta = 0.0
        if hole_credit == "waiver":
            credit_delta = _hole_credit_total(
                grade, weeks=reg_set, levels=levels, roster=roster
            ) - base_credit
            adj_points += credit_delta
        solo = inputs.solo_points(entry.player_id)
        crowded = max(0.0, solo - adj_points)
        adj_wins = grade.expected_wins - base_grade.expected_wins
        cand_holes = frozenset(grade.hole_weeks)
        slots = _slots_for(entry.position, roster)
        # A HOLE THAT CLOSED IS NOT A HOLE HE CLOSED. ``filled`` used to be the
        # raw set difference, and under completion="future" the candidate branch
        # runs its OWN completion — so a different hypothetical later pick can be
        # what covers the week, and the sentence built from it told the operator a
        # player "plays that week and plugs it" about a week he is on BYE for
        # (measured live at overall picks 9 and 12). Two conditions now have to
        # hold for the credit to be his: he must PLAY that week, and the slot that
        # was empty must be one he could actually be seated in — a quarterback was
        # being credited with an empty RB2.
        closed = tuple(w for w in reg if w in base_holes and w not in cand_holes)
        filled = tuple(
            w for w in closed
            if inputs.plays(entry.player_id, w) and (base_hole_slots.get(w, frozenset()) & slots)
        )
        closed_downstream = tuple(w for w in closed if w not in filled)
        still_short = {w: sl for w, sl in grade.hole_detail if set(sl) & slots}
        collisions = tuple(
            w for w in reg if w in still_short and not inputs.plays(entry.player_id, w)
        )
        applies = entry.position in adjust
        adjustment = (
            _mode_adjustment(mode, weight, adj_points, crowded, adj_wins) if applies else 0.0
        )
        out.append(
            WeekwiseAdjustment(
                player_id=entry.player_id,
                position=entry.position,
                name=entry.name,
                engine_rank=rank,
                engine_score=rec.pick_score,
                marginal_points=adj_points,
                solo_points=solo,
                crowded_out=crowded,
                marginal_wins=adj_wins,
                filled_hole_weeks=filled,
                collision_weeks=collisions,
                adjustment=adjustment,
                applies=applies,
                completion=completion,
                closed_downstream_weeks=closed_downstream,
                hole_credit_delta=credit_delta,
            )
        )
    return tuple(out)


def _grade(inputs: WeekwiseInputs, entries: Sequence[BoardEntry]) -> SeasonGrade:
    """The ONE grading seam. Routed through a module function on purpose: it is
    what ``test_grade_calls_are_bounded_per_decision`` counts, so the latency
    story is a load-free assertion rather than a stopwatch race."""
    return inputs.grade(entries)


def _week_mean(means: Sequence[float], week: int) -> float:
    """``weekly_means`` is DENSE from week 1; a week past the end scores 0.0."""
    idx = week - 1
    return float(means[idx]) if 0 <= idx < len(means) else 0.0


# ========================================================================
#                       4.  the reasons (Rule 6)
# ========================================================================


def _week_list(weeks: Sequence[int]) -> str:
    if len(weeks) == 1:
        return f"week {weeks[0]}"
    return "weeks " + ", ".join(str(w) for w in weeks[:-1]) + f" and {weeks[-1]}"


def _those_weeks(weeks: Sequence[int]) -> str:
    """"that week" / "those weeks" — the sentences read to a novice, so they agree."""
    return "that week" if len(weeks) == 1 else "those weeks"


#: What ``marginal_points`` is a difference AGAINST, in words, so no sentence
#: built from it can quietly claim the other one's meaning. Phrased as a NOUN
#: (not "than ...") so every sentence below can splice it after its own
#: comparative — the audit found a fall-through branch that spliced the "than"
#: form with no comparative in front of it and shipped "he is worth about 0
#: points ... than the player you would otherwise take", which does not parse.
_AGAINST = {
    "future": "the player you would otherwise take with this pick",
    "none": "leaving this pick unspent",
}

#: Below this many points the week-by-week difference is called "about the same"
#: rather than rendered as a signed number. A displayed "about 0 points more" is
#: not an explanation, and 0.5 is where ``f"{x:.0f}"`` would start printing 1.
_SAME_BAND = 0.5


def _hole_sentence(adj: WeekwiseAdjustment) -> str:
    """The promotion sentence for a candidate who genuinely closes a bye hole.

    "as things stand" is TRUE only under ``completion="none"``, where the
    comparison roster really is the roster as it stands. Under the shipped
    completion the holes are the holes of a MODELLED sixteen-man season, and the
    audit caught the sentence claiming otherwise at round 3 with two players
    rostered — a state in which the real lineup is empty in every week.
    """
    weeks = _week_list(adj.filled_hole_weeks)
    those = _those_weeks(adj.filled_hole_weeks)
    if adj.completion == "none":
        return (
            f"week-by-week check: as things stand your starting lineup has an "
            f"empty spot in {weeks} — their bye weeks, when the players you have "
            f"at that slot are all off. He plays {those} and fills it"
        )
    return (
        # NAME THE BYE. This sentence used to sit two bullets under the engine's
        # own "your starters at this spot are set", and a novice has no way to
        # reconcile "set" with "empty spot in weeks 5, 9" when neither sentence
        # says WHY (audit minor). They are both true and they are about different
        # things: the slot is filled for the season, and it is empty on the weeks
        # its occupants are on bye.
        f"week-by-week check: your starters at this slot are set for the season, "
        f"but with the rest of your draft modelled out your starting lineup still "
        f"has an empty spot in {weeks} — their bye weeks. He plays {those} and "
        f"fills it"
    )


def _promotion_reason(adj: WeekwiseAdjustment) -> str:
    """Why the week-by-week term moved this candidate UP.

    THE NEGATIVE BRANCH IS THE NORMAL CASE, not an edge. Under the shipped
    completion the baseline spends this pick on the completion's own choice, so
    most marginals are negative; a candidate rises when the players he passes are
    MORE negative. The audit measured 34 of 79 promotion sentences rendering a
    negative number inside a "worth about N points more" frame — a sentence that
    says the opposite of the recommendation it is attached to.

    The two non-hole branches say "the candidates he passes", never "the
    candidates above him": a promotion is a strictly larger ADJUSTMENT than every
    candidate it overtook (his engine score is the lower one, or he would not have
    been below them), and it says nothing at all about the ones still ahead.
    """
    if adj.filled_hole_weeks:
        return _hole_sentence(adj)
    against = _AGAINST[adj.completion]
    if adj.marginal_points > _SAME_BAND:
        return (
            f"week-by-week check: counting the season one week at a time, he adds "
            f"about {adj.marginal_points:.0f} points to the lineup you would "
            f"actually start, compared with {against}"
        )
    if adj.marginal_points < -_SAME_BAND:
        return (
            f"week-by-week check: counting the season one week at a time he costs "
            f"about {-adj.marginal_points:.0f} points against {against} — he still "
            f"moves up here because the candidates he passes cost more still"
        )
    return (
        f"week-by-week check: counting the season one week at a time he is worth "
        f"about the same as {against}, and he moves up because the candidates he "
        f"passes grade worse"
    )


def _demotion_reason(adj: WeekwiseAdjustment) -> str:
    """Why the week-by-week term moved this candidate DOWN."""
    against = _AGAINST[adj.completion]
    if adj.collision_weeks:
        return (
            f"week-by-week check: he is off in {_week_list(adj.collision_weeks)}, "
            f"and you are already short at {adj.position} in "
            f"{_those_weeks(adj.collision_weeks)} — taking him would leave you a "
            f"man short in the week you most need covering"
        )
    if adj.completion == "none" and adj.solo_points > 0 and adj.usable_share < 0.5:
        return (
            f"week-by-week check: of his roughly {adj.solo_points:.0f} projected "
            f"points only about {adj.marginal_points:.0f} could actually reach "
            f"your starting lineup — that spot is already covered in most weeks"
        )
    if adj.marginal_points < -_SAME_BAND:
        return (
            f"week-by-week check: counting the season one week at a time he costs "
            f"about {-adj.marginal_points:.0f} points against {against}"
        )
    if adj.marginal_points > _SAME_BAND:
        return (
            f"week-by-week check: counting the season one week at a time he adds "
            f"about {adj.marginal_points:.0f} points compared with {against} — "
            f"less than another option on this list"
        )
    return (
        f"week-by-week check: counting the season one week at a time he is worth "
        f"about the same as {against} — and less than another option on this list"
    )


def _weekwise_reason(adj: WeekwiseAdjustment, *, moved_up: bool) -> str:
    return _promotion_reason(adj) if moved_up else _demotion_reason(adj)


def _why_not(rec: PickRec) -> str:
    """One line for an alternative, rebuilt after the re-rank.

    Mirrors ``PickEngine._why_not`` off the public ``PickRec.survival_next``
    rather than reaching into the engine's private survival estimate; the wait
    threshold is the same 0.75 the engine uses.
    """
    s = rec.survival_next
    if s >= 0.75:
        return f"you can likely wait — about {round(s * 100)}% he's still there next pick"
    return "close in value, but a smaller edge here"


# ========================================================================
#                       5.  the picker
# ========================================================================


#: "this field was not passed" — distinct from ``None``, which ``PickEngine``
#: accepts as a real value for ``survival``.
_INHERIT: object = object()

#: What a posture clone of this picker is, in one line (see
#: :class:`WeekwisePicker`'s docstring).
POSTURE_CLONE_LABEL = (
    "posture projections continue the draft on the UNWRAPPED engine: the "
    "week-by-week term is an on-clock term only, because running it inside a "
    "rollout would cost order ten seconds per projection"
)


@dataclass(frozen=True)
class WeekwisePicker:
    """A :class:`~ziggurat.draft.bots.Picker` that re-ranks the engine's shortlist.

    ``weight=0.0`` is EXACTLY the wrapped :class:`PickEngine` — same picks, same
    scores, same reasons, same consumption of ``ctx.rng`` — which is what makes
    this variant a paired A/B against the shipped engine rather than a rewrite.

    ``engine`` is the wrapped engine (construct it with the draft-night settings:
    ``PickEngine(rollouts=512, room_priors=...)``). ``inputs`` is the
    once-per-session :class:`WeekwiseInputs`.

    HOW A PHASE-3 INTEGRATOR WIRES THIS IN. ``session.DraftSession`` reaches the
    engine through exactly one factory, ``_engine()``, which returns
    ``PickEngine(rollouts=self.rollouts, room_priors=priors)`` fresh on every
    compute so live recalibration takes effect. So the whole integration is to
    wrap that return value behind a flag::

        engine = PickEngine(rollouts=self.rollouts, room_priors=priors)
        if self.weekwise_weight:                     # 0.0 => untouched engine
            return WeekwisePicker(engine=engine, inputs=self._weekwise_inputs,
                                  weight=self.weekwise_weight)
        return engine

    with ``_weekwise_inputs`` built ONCE at session construction, next to the
    board load, from the same ``as_of``/season/source.

    THE CLAIM THAT USED TO STAND HERE — "nothing else in session or webapp
    touches a PickEngine attribute other than recommend" — WAS FALSE, and it was
    false in the silent direction. ``DraftSession.engine`` is a public property
    returning ``_engine()``; ``posture.project_postures`` reads
    ``engine.need_schedule`` and clones the engine with
    ``dataclasses.replace(engine, need_schedule=..., survival=...)``. Neither
    works on a plain wrapper, and BOTH cockpits swallow the exception
    (``app._safe_posture`` and ``webapp._recompute`` catch bare ``Exception``),
    so following the old recipe killed the 2.4 hysteresis posture monitor for the
    whole draft with no error, no log and no visible symptom.

    This class therefore satisfies that surface deliberately:

    * unknown attribute reads fall through to the wrapped engine
      (``__getattr__``), so ``engine.need_schedule`` and anything else a future
      reader wants is the engine's;
    * ``need_schedule`` and ``survival`` are accepted as replace-keywords and
      folded into a fresh WRAPPED engine, and the clone comes back with
      ``weight=0.0`` — i.e. provably the bare engine (:data:`POSTURE_CLONE_LABEL`).

    THAT LAST CHOICE IS A REAL SIMPLIFICATION AND IS NAMED, NOT HIDDEN. A posture
    projection continues the whole draft ``POSTURE_ROLLOUTS`` times for each of
    five need schedules; running the week-by-week term inside it would add
    ``len(shortlist) + 1`` grade calls to every one of those thousands of picks —
    order ten seconds per projection against a 90-second clock. So the posture
    monitor compares archetypes on the unwrapped engine, exactly as it was
    validated in 2.4, while the on-clock recommendation carries the term. Both
    postures are measured with the same continuation engine, so the comparison
    the monitor actually makes is unaffected; what it cannot see is a posture
    that only pays off once the week-by-week term is doing the picking.
    """

    engine: PickEngine
    inputs: WeekwiseInputs
    weight: float = 0.0
    mode: str = "marginal_points"
    completion: str = "future"
    completion_gap: int | None = None
    shortlist: int = DEFAULT_SHORTLIST
    adjust_positions: tuple[str, ...] = DEFAULT_ADJUST_POSITIONS
    available_depth: int = DEFAULT_AVAILABLE_DEPTH
    hole_credit: str = "none"
    #: Accepted ONLY so ``dataclasses.replace(picker, need_schedule=..., ...)``
    #: works — see the class docstring. Reading either gives the wrapped engine's
    #: value; passing either folds it into the engine and zeroes ``weight``.
    need_schedule: object = _INHERIT
    survival: object = _INHERIT

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise WeekwiseInputError(f"unknown mode {self.mode!r}; expected one of {MODES}")
        if self.completion not in ("future", "none"):
            raise WeekwiseInputError(
                f"unknown completion {self.completion!r}; expected 'future' or 'none'"
            )
        if self.mode == "crowding" and self.completion != "none":
            raise WeekwiseInputError(CROWDING_NEEDS_NAIVE)
        if self.hole_credit not in HOLE_CREDITS:
            raise WeekwiseInputError(
                f"unknown hole_credit {self.hole_credit!r}; expected one of {HOLE_CREDITS}"
            )
        if self.shortlist < 1:
            raise WeekwiseInputError(
                f"shortlist must ask the engine for at least one candidate; got {self.shortlist}"
            )
        if not math.isfinite(self.weight):
            raise WeekwiseInputError(f"weight must be a finite number; got {self.weight!r}")
        # The posture seam. Identity, not equality: re-passing the value already
        # on the engine (which ``dataclasses.replace(picker, weight=4)`` does,
        # because the fields hold the engine's values) must NOT be read as a
        # posture clone and must not zero the weight.
        overrides = {
            name: value
            for name, value in (
                ("need_schedule", self.need_schedule), ("survival", self.survival)
            )
            if value is not _INHERIT and value is not getattr(self.engine, name, _INHERIT)
        }
        if overrides:
            object.__setattr__(self, "engine", dc_replace(self.engine, **overrides))
            object.__setattr__(self, "weight", 0.0)
        # Reflect the engine's values back so a READ of either field is the
        # engine's, and so a later replace() re-passing them is a no-op rather
        # than a posture clone. ``getattr`` with a default, not attribute access:
        # a duck-typed test engine need not carry either field.
        object.__setattr__(self, "need_schedule",
                           getattr(self.engine, "need_schedule", _INHERIT))
        object.__setattr__(self, "survival", getattr(self.engine, "survival", _INHERIT))

    def __getattr__(self, name: str) -> object:
        """Unknown attributes are the wrapped engine's (see the class docstring).

        Only reached when normal lookup fails, so it can never shadow a field.
        ``engine`` itself is excluded or a half-built instance recurses forever.
        """
        if name.startswith("__") or name == "engine":
            raise AttributeError(name)
        try:
            engine = object.__getattribute__(self, "engine")
        except AttributeError:  # pragma: no cover - only during construction
            raise AttributeError(name) from None
        return getattr(engine, name)

    # -- Picker seam -------------------------------------------------------

    def pick(self, ctx: PickContext) -> str:
        """The drafted player_id. Consumes ``ctx.rng`` exactly as the engine does."""
        return self.recommend(ctx, top=1)[0].player_id

    def recommend(self, ctx: PickContext, *, top: int = 5) -> tuple[PickRec, ...]:
        """Top-``top`` recommendations after the week-by-week re-rank.

        The engine is asked for ``max(top, shortlist)`` candidates; only those are
        re-graded, and only the first ``top`` come back. A recommendation whose
        place in the order the week-by-week term CHANGED gains one plain-language
        sentence saying what it saw (Rule 6); one whose place it did not change
        is returned with the engine's reasons verbatim.
        """
        recs = self.engine.recommend(ctx, top=max(int(top), self.shortlist))
        if self.weight == 0.0 or not recs:
            return recs[: max(1, int(top))]

        adjustments = weekwise_adjustments(
            recs,
            ctx,
            inputs=self.inputs,
            weight=self.weight,
            mode=self.mode,
            completion=self.completion,
            completion_gap=self.completion_gap,
            adjust_positions=self.adjust_positions,
            available_depth=self.available_depth,
            hole_credit=self.hole_credit,
        )
        # Same total order the engine uses (D2), with the blended score in front:
        # higher score, then higher vor, then lower ESPN rank, then player_id.
        # The trailing player_id is defence in depth and is deliberately NOT
        # covered by a test: ``simulator.load_board`` assigns a unique
        # ``espn_overall_rank`` to every row (unranked players get the
        # 10_000 + house-rank sentinel, which is unique too), so the key is
        # already total one field earlier and no fixture built from a real board
        # can reach the last tiebreak. It stays because the cost of a
        # dict-ordered draft in a journal replay is unbounded and the cost of
        # this line is nothing.
        order = sorted(
            zip(recs, adjustments, strict=True),
            key=lambda pair: (
                -(pair[0].pick_score + pair[1].adjustment),
                -pair[0].vor,
                pair[0].player.espn_overall_rank,
                pair[0].player_id,
            ),
        )
        n = max(1, int(top))
        chosen = order[:n]
        out: list[PickRec] = []
        for new_rank, (rec, adj) in enumerate(chosen):
            reasons = rec.reasons
            if new_rank != adj.engine_rank and adj.applies:
                # Rule 6: the sentence AND what it cannot see. BENCH_BLINDNESS_LABEL
                # used to live only in ``format_adjustments``, a diagnostics helper
                # no cockpit calls — so the module's stated disclosure reached the
                # operator in 0 of 161 measured rendered reasons.
                reasons = reasons + (
                    _weekwise_reason(adj, moved_up=new_rank < adj.engine_rank),
                    BENCH_BLINDNESS_LABEL,
                )
                if self.completion == "future":
                    reasons = reasons + (COMPLETION_LABEL,)
                if self.hole_credit == "waiver":
                    reasons = reasons + (HOLE_CREDIT_LABEL,)
            alternatives = tuple(
                (alt.name or alt.player_id, _why_not(alt))
                for alt, _a in order[new_rank + 1 : new_rank + 4]
            )
            out.append(
                dc_replace(
                    rec,
                    pick_score=rec.pick_score + adj.adjustment,
                    reasons=reasons,
                    alternatives=alternatives,
                )
            )
        return tuple(out)


# ========================================================================
#                       6.  legibility
# ========================================================================


def format_adjustments(adjustments: Sequence[WeekwiseAdjustment]) -> str:
    """A readable table of one decision's week-by-week term. Diagnostics only."""
    lines = [
        f"{'candidate':22s} {'pos':4s} {'engine':>9s} {'marg pts':>9s} "
        f"{'solo':>7s} {'crowd':>7s} {'d wins':>8s} {'adj':>8s}  weeks",
    ]
    for a in adjustments:
        weeks = ""
        if a.filled_hole_weeks:
            weeks += f"fills {list(a.filled_hole_weeks)} "
        if a.closed_downstream_weeks:
            weeks += f"(completion covers {list(a.closed_downstream_weeks)}) "
        if a.collision_weeks:
            weeks += f"short {list(a.collision_weeks)}"
        lines.append(
            f"{(a.name or a.player_id)[:20]:22s} {a.position:4s} {a.engine_score:9.2f} "
            f"{a.marginal_points:9.1f} {a.solo_points:7.1f} {a.crowded_out:7.1f} "
            f"{a.marginal_wins:+8.4f} {a.adjustment:+8.2f}  {weeks}"
        )
    lines.append(f"  - {BENCH_BLINDNESS_LABEL}")
    lines.append(f"  - {HOLE_PRICING_WARNING}")
    if any(a.hole_credit_delta for a in adjustments):
        lines.append(f"  - {HOLE_CREDIT_LABEL}")
    return "\n".join(lines)
