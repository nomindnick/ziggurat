"""Phase-2 VARIANT: make the engine think in PAIRS at the wheel (opt-in, additive).

Import-quarantined package (Rule 8). Nothing outside ``ziggurat/draft/`` imports this, and
nothing inside the shipped path imports it either — it is a NEW module that WRAPS
:class:`~ziggurat.draft.engine.PickEngine` without editing a byte of it. A Phase-3
integrator wires it in behind a flag or does not wire it in at all.

THE PROBLEM. The shipped engine is ONE-PLY: it asks "which single player is best
now". From the operator's real seat (slot 9 of 10) the sixteen picks arrive in
EIGHT TIGHT PAIRS — overall 9 & 12, 29 & 32, 49 & 52, 69 & 72, 89 & 92, 109 & 112,
129 & 132, 149 & 152 — with only TWO rival picks between the halves of each pair
(measured from the snake geometry, not assumed). At a seat that truly wheels
(slot 1 or slot 10) the gap is ZERO. The right question at a pair is not "who is
best now" but "which PAIR of players do I end up holding".

The classic wheel effect a one-ply score misses: if A and B are both wanted and
both likely to survive the two rival picks, taking the SCARCER one first and the
safer one second strictly dominates — the one-ply score just takes the
higher-scoring one and can lose the other. This module computes that exactly.

WHAT IT DOES, precisely (and what it does NOT):

  1. It ENGAGES only when the operator's next pick is at most ``max_pair_gap``
     rival picks away (default 2 — the operator's real wheel shape) and a next
     pick exists. Everywhere else it delegates to the wrapped engine VERBATIM,
     so a draft is the shipped draft at every non-pair pick.
  2. At a pair it runs ONE survival-rollout batch (:func:`rollout_pair_batch`),
     which is the SHIPPED rollout loop — same room, same priors, same kappa, same
     rng consumption order — additionally recording, per rollout, WHICH tracked
     players the room took. That per-rollout record is the whole point: the
     marginal survival probabilities the shipped batch returns cannot answer
     "what is the best player still there at my second pick", because over two
     rival picks the candidates' fates are strongly dependent (at most ``gap`` of
     them can go). ``tests/test_variant_wheel.py`` asserts this batch reproduces
     :func:`ziggurat.draft.survival.rollout_survival`'s marginals EXACTLY under
     the same seed, so it cannot silently drift from the shipped room model. A
     caller may substitute a different JOINT model through ``batch_provider``, and
     must do so if the wrapped engine carries an injected marginal ``survival``
     provider — see :data:`PairBatchProvider`.
  3. It then maximises, over first-pick candidates ``A``::

         pair_score(A) = base(A | roster)
                       + E_rollouts[ max over B still available of base(B | roster + A) ]

     The expectation is over the SAME batch — no per-pair re-roll. ``B`` is drawn
     from a successor-state candidate pool that depends on ``A`` only through
     ``A``'s POSITION, so there are at most six distinct pools no matter how many
     candidates there are: the quadratic collapses to (positions x pool) work,
     and the cost of the whole decision is dominated by the rollout batch the
     engine was going to run anyway. Two candidates whose totals come out equal to
     within :data:`DEFAULT_TIE_TOLERANCE` are ordered by the engine's own ladder
     (higher VOR first), not by the order the two floats happened to be added in;
     that is a correctness fix, and its reasoning is at that constant.
  4. The first term is the shipped engine's one-ply score, and the pair
     expectation is added ON TOP of it (``urgency_mode="keep"``, the default). The
     obvious objection is that this counts the wait twice: the engine's
     ``b_vona * urgency(pos) * frac`` term is a heuristic SURROGATE for the value
     lost between this pick and the next one, which is exactly what the pair
     expectation now computes directly. ``urgency_mode="drop"`` subtracts it and
     is the theoretically clean form. It is not the default because the surrogate
     is not free-standing: ``b_vona`` was SWEPT and selected by the item-2.3
     tournament together with ``b_need`` and ``b_risk``, so removing it re-scales
     the whole score rather than merely de-duplicating one effect. Both were
     measured; the table below is the reason the default is what it is, and says
     plainly that the choice was made after seeing a result.

WHAT THIS DOES NOT MODEL, stated up front (Rule 6 honesty, and every one of these
is a real limitation of the numbers this module produces):

  * THE SECOND PICK IS VALUED MYOPICALLY. ``base(B | roster + A)`` carries no
    urgency term of its own, because the horizon beyond the wheel (16 rival picks
    at the operator's seat) is not in this batch and a second batch would blow the
    latency budget. So the pair value knows what the wheel pick is WORTH but not
    what waiting past it would cost. The actual second pick is still made later by
    the full shipped engine, urgency and all — this is an estimate used to CHOOSE
    the first pick, not the second pick itself.
  * THE SECOND-PICK POOL IS BOUNDED. It is the top-``second_width`` by ESPN rank
    across the successor state's allowed positions, unioned with the best-by-VOR
    at each of those positions. Over a gap of at most ``max_pair_gap`` rival picks
    at most ``max_pair_gap`` of that pool can be taken, so the pool's best
    survivor is the board's best survivor unless the pool is smaller than the gap;
    :attr:`PairDecision.pool_exhausted` counts every rollout where it was not, and
    that count is disclosed in the reasons rather than hidden.
  * IT IS NOT THE GRADER OBJECTIVE. Pair value is in the engine's own VOR-point
    units, not ``grader.grade_roster``'s expected wins. Grading a pair through the
    grader needs the roster the pair lands on to be COMPLETE (a two-man roster
    fields seven empty starter slots every week and every option grades to
    approximately zero wins), so it would need a roster-completion model this
    module does not have, and O(candidates^2) grader calls would cost far more
    than the whole latency budget. Stated, not glossed.
  * A PAIR IS TWO PICKS, NOT SIXTEEN. This is a 2-ply lookahead. It says nothing
    about the third pick.
  * THE SURVIVAL INPUT CARRIES A KNOWN, DIRECTIONAL, POSITION-SPECIFIC ERROR, and
    it is the input this whole module is most sensitive to. Everything here is a
    bet on the survival DIFFERENCE between two candidates. Phase 1's
    :mod:`ziggurat.draft.roomcheck`, calibrating the live rollout route against 11
    real ten-team ESPN drafts (1,760 picks), found it well calibrated overall
    (predicted 0.698 vs observed 0.670) but OPTIMISTIC BY POSITION exactly where
    it matters: real rooms take TEs +0.111 and QBs +0.054 earlier than the model
    expects. The module's own showcase decision is a TE against an RB. MEASURED
    HERE 2026-08-30, by pushing that measured deflation through the new
    ``batch_provider`` seam at overall #9 on the live board: Brock Bowers's
    survival moves 0.947 -> 0.836 and the top-two pair margin moves 1.811 -> 0.851.
    The pick does NOT flip, but 53% of the margin is accounted for by a known
    error in the input. That deflation is a construction, not a validated bias
    model — the honest reading is that this module's margins at TE are soft by
    roughly half, not that they are wrong.
  * IT HAS NO INJURY OR AVAILABILITY MODEL, and neither does the objective it was
    measured against. Both halves of a pair are priced as if they play sixteen
    games. See ``ziggurat/core/availability.py`` for what that costs.

WHAT WAS MEASURED (``ziggurat.draft.evaluate``, live 2026 board of 2026-08-30,
3,264 rows, ``PickEngine(rollouts=512)`` in both arms, the calibrated 2.2 room
in the other nine seats, paired per-seat streams, ``grader.grade_roster``
expected wins as the objective). Every number is a PAIRED mean delta against the
shipped engine on identical rooms; ``*`` marks a 95% interval excluding zero::

    config      seat 9 (seed 1)   seat 9 (seed 5)   seat 2 (seed 3)   seats 1+10
    keep        +0.033 *          +0.030 *          +0.018 *          not run
    drop        +0.025 *          +0.024 *          -0.002            +0.002
    drop,       +0.013            not run           not run           not run
     no displ.
    (n=300 paired drafts per cell; seats 1+10 is n=150 at each)

THAT TABLE IS THE BUILD'S OWN RUN AND IT OVERSTATES THE MARGIN BY ABOUT A THIRD.
An independent re-run on SIXTEEN seeds the build never used (10 at seat 9, 6 at
seat 2; n=50 paired drafts per seed at ``rollouts=512``; 800 pairs) puts the
shipped ``keep`` configuration at::

    seat 9, 10 held-out seeds, n=500   +0.0153   95% CI [+0.0020, +0.0286]  *
    seat 2,  6 held-out seeds, n=300   +0.0214   95% CI [+0.0086, +0.0343]  *
    both seats pooled,         n=800   +0.0176   95% CI [+0.0080, +0.0272]  *

So the EFFECT replicates out of sample at both gap-2 seats with intervals that
exclude zero, and the SIZE the build reported (+0.030 to +0.033 at seat 9) does
not: the honest point estimate at the operator's own seat is about +0.015, half
of what the table above says, and roughly 1% of the engine's own +1.28 margin
over drafting the ESPN board. A third independent re-run by an auditor on six
further seeds landed at +0.0254 (n=600). Read the effect as "real, replicated,
and somewhere around +0.015 to +0.025 expected wins".

Read the build's table as four separate findings, because it is:

  1. AT THE OPERATOR'S OWN SEAT THE EFFECT IS REAL AND SMALL. Both configurations
     are positive at seat 9 under two independent seeds, every interval excluding
     zero, and the fixed-field second opinion agreeing in sign (+0.022 to +0.027).
     THE SUPPORTING HOLE-COUNT CLAIM DOES NOT REPLICATE AND IS WITHDRAWN. The
     build offered falling unfillable-starter-week counts as mechanism
     confirmation ("holes fall in every positive cell"). On the sixteen held-out
     seeds the POOLED direction is still favourable — 0.428 vs 0.436 per season at
     seat 9 and 1.290 vs 1.387 at seat 2 — but it goes the WRONG WAY in 4 of those
     16 individual seeds and is exactly equal in 2 more. "It removes hole weeks"
     is not an established property of this variant; "it removes slightly more
     hole weeks than it adds, on average" is as much as the data supports.
  2. THE THEORETICALLY CLEAN CONFIGURATION IS THE WEAKER ONE. ``drop`` removes
     ``b_vona``, a weight the item-2.3 tournament SWEPT and selected, and replaces
     it with an unweighted exact expectation — so it does not merely de-duplicate
     the wait cost, it re-scales the whole score. At seat 2 (the only other seat
     in a ten-team snake whose picks are two apart) it goes to -0.002. ``keep``
     held at every gap-2 seat measured. That is why ``keep`` is the default, and
     the honest caveat is that the choice was made AFTER seeing seat 9: the
     confirmation is that it then held at a seat and a seed it was not chosen on.
  3. THE DISPLACEMENT CORRECTION EARNS ITS KEEP. Paired on the shared baseline
     grid at seat 9, ``drop`` minus ``drop,no-displacement`` is +0.0119 (95% CI
     +0.0034 .. +0.0204, excludes zero). Not modelling the rival pick our own
     first pick displaces is measurably worse, not just theoretically sloppy.
  4. A TRUE WHEEL IS THE ONE PLACE THIS CANNOT HELP, and the reason is structural
     rather than statistical. At gap 0 every survival is 1, so the engine's
     ``urgency = VONA * (1 - S)`` term is ALREADY zero and its greedy pick is
     already ``argmax base``; all the pair maths can add is the roster-interaction
     term. Measured at seats 1 and 10: +0.002, and 262 of 300 drafts came out
     byte-identical. The scarcity effect this module is named for needs at least
     one rival pick to exist in.

The K/DST divergence play is UNCHANGED — re-verified after the tie fix, which
matters because that fix fires at overall #89, i.e. ROUND 9, the D/ST round
itself. Over 40 drafts at seat 9 on identical rooms, all three arms take D/ST at
round 9 (median; engine min 9 max 9, both variant arms min 9 max 10) and the
kicker at round 10 (median), while the nine rival seats take D/ST at round 15 and
kickers at round 16 (medians, n=360 each). The divergence play is intact and the
tie fix does not touch it.

COST, re-measured after the audit fixes on the same board at ``rollouts=512``
over three full drafts (min of three repeats per pick): worst-case
``recommend()`` 173.9 ms for the variant against 171.9 ms for the bare engine,
against the cockpit's 243 ms budget — 69 ms of headroom. The worst case occurs at
a pick the variant DELEGATES, so it IS the engine's number and the 2 ms is
run-to-run noise, not a cost. The pair decisions themselves cost 21.0 ms median
and 26.9 ms worst. That is not a coincidence: at the operator's seat the pair
pick is the one with only TWO intervening rival picks, which is structurally the
cheapest survival batch of the draft, and the expensive pick (sixteen intervening)
is the one this module hands straight to the engine.

WHAT THE 2026-08-30 AUDIT CHANGED (three reviewers, twelve findings):

  * ONE REAL RANKING BUG, fixed: two candidates who are each other's wheel partner
    had their identical pair totals summed in opposite orders, so a mathematically
    exact tie landed ~1 ULP apart and the engine's tie-break ladder was bypassed —
    the cockpit displayed two identical scores and ranked the player 38.9 VOR
    points WORSE first. See :data:`DEFAULT_TIE_TOLERANCE`. Measured over 240 pair
    decisions in 30 full drafts: 52 near-ties, 14 of them resolved toward the
    lower-VOR player before the fix and 0 after. THE FIX IS FREE AND IT IS ALSO
    OBJECTIVE-INVISIBLE: paired over the same 800 held-out drafts, fixed minus
    pre-fix is -0.0000 expected wins (95% CI [-0.0007, +0.0007]; 781 of 800 pairs
    byte-identical), because it fires only at overalls #89 and #149, where the
    players it reorders are bench pieces ``grade_roster`` values at exactly 0.000
    unless they cover a bye. It is justified on correctness and on Rule 6 — a
    novice cannot smell a 30-point downgrade shown as an identical number — and
    NOT on the harness objective, which is structurally blind to it.
  * ONE INTEGRATION HAZARD, closed by refusing: an injected ``PickEngine.survival``
    provider was honoured at delegated picks and silently ignored at pair picks.
    See :data:`PairBatchProvider`.
  * SEVERAL DISCLOSURES MOVED OR CORRECTED: the "this deliberately counts the wait
    twice" sentence now reaches the on-clock reasons instead of only the
    diagnostics object; the pool-exhaustion note no longer calls a zero substitute
    a "floor" (it is not one in the late rounds, where the best man left is worth
    less than nothing); the TE/QB survival optimism is named above; the hole-count
    claim is withdrawn above.
  * FOUR TEST DEFECTS, fixed in ``tests/test_variant_wheel.py``: the brute-force
    arithmetic gate floored its oracle at zero and so covered none of the second
    half of the draft; the drop-vs-keep test was a tautology on its board; the
    urgency reconstruction had no test at all; and a stated ``_SHORTLIST``
    mutation-check was false. All four are now load-bearing and re-mutated.

WHAT THE MARGIN IS NOT. It is measured against the same projections spine that
prices the board it drafts from, and against the 2.2 MODEL of the room, so it is
evidence about that model and not a promise about draft night. It also inherits
that model's measured positional survival optimism (see "WHAT THIS DOES NOT
MODEL" above), which accounts for about half of the showcase decision's margin.
Phase 4 grades realized results; nothing here does.

Rule 1: no DB read here at all — the board arrives on ``ctx.state``. Rule 2: no
scoring constant; every point in this file comes from ``BoardEntry.vor``, which
came from ``valuation.py`` -> ``scoring.py``. Rule 3: not a CLI. Determinism: all
randomness is drawn from ``ctx.rng`` through one derived child, in a fixed order.
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ziggurat.core.valuation import RosterStructure
from ziggurat.draft.bots import (
    POSITIONS,
    AutodraftBot,
    BoardEntry,
    PickContext,
    RankNoiseBot,
    allowed_positions,
    legal_positions,
    position_counts,
)

# ``_dispersion`` / ``_need_fill`` / ``_value_fraction`` are the shipped engine's
# own private scoring helpers, imported DELIBERATELY rather than re-implemented: a
# copy would let this variant's notion of "need" or "lineup reachability" drift
# away from the engine it is measured against, and the drift would be invisible.
# ``test_variant_wheel.py`` pins the reconstruction against a real
# ``PickRec.pick_score``, so if engine.py changes these, this file fails loudly
# instead of quietly measuring a different score.
from ziggurat.draft.engine import (
    PickEngine,
    PickRec,
    _dispersion,
    _need_fill,
    _value_fraction,
    risk_sign,
)
from ziggurat.draft.priors import ROOM_PRIORS_2025, RoomPriors
from ziggurat.draft.survival import (
    DEFAULT_KAPPA,
    DEFAULT_ROLLOUTS,
    upcoming_opponent_picks,
)

# --------------------------------------------------------------------- knobs

#: How many rival picks may sit between the operator's two picks for this to
#: count as a PAIR. The operator's seat (slot 9 of 10) has a gap of exactly 2 at
#: eight of its sixteen picks; a true wheel seat (slot 1 or 10) has a gap of 0.
#: A gap of 2 already means at most two of the candidate pool can be sniped, which
#: is what makes the pair expectation cheap and sharp. Raising this widens the
#: engagement window and makes the myopic-second-pick caveat above bite harder.
DEFAULT_MAX_PAIR_GAP = 2

#: Candidate width for the SECOND (wheel) pick's pool — wider than the engine's
#: own ``candidate_width`` (5) because the pool has to still contain the best
#: available player after the room has taken up to ``max_pair_gap`` of it.
DEFAULT_SECOND_WIDTH = 8

#: Hard bound on how many first-pick candidates get the pair treatment. The
#: enumeration is already collapsed to (successor positions x pool), so this is a
#: belt-and-braces cap rather than the thing that makes it affordable.
DEFAULT_PAIR_LIMIT = 16

#: ``"drop"`` — the pair expectation REPLACES the engine's urgency surrogate on
#: the first pick (see the module docstring, point 4). Theoretically the cleaner
#: of the two: it is the one that does not count the wait cost twice.
#: ``"keep"`` — the engine's full one-ply score is the first term and the pair
#: expectation is added on top, so the variant is a TILT on the shipped, swept
#: engine rather than a replacement of its value function. THE SHIPPED DEFAULT,
#: on measurement rather than theory — see "WHAT WAS MEASURED" in the module
#: docstring, including the fact that this default was chosen AFTER seeing the
#: first result and what was done to confirm it out of sample.
URGENCY_MODES = ("drop", "keep")

#: How to price the fact that the survival batch was rolled on a board that still
#: held the candidate we are about to take.
#: ``"rank"`` — the displacement correction (default; see the loop below).
#: ``"none"`` — ignore it, which is the shipped engine's own convention for its
#: survival numbers. Optimistic in a specific direction: it over-credits the
#: second pick exactly for the contested players the wheel logic already favours.
DISPLACEMENT_MODES = ("rank", "none")

#: How many survivors of a successor pool are kept per rollout. Exactly ONE
#: exclusion can ever apply to the survivor list — the candidate himself when the
#: room did NOT take him, or the room's displaced substitute when it did, never
#: both, because a candidate the room took is not a survivor — so two would do and
#: the third is a deliberate spare. The truncation being lossless is not asserted
#: from that argument: ``test_the_collapsed_enumeration_equals_a_brute_force_over_every_pair``
#: re-derives every number from an untruncated scan of the whole pool at SEVEN
#: picks spanning the whole draft, and separately drives ``_SHORTLIST`` down until
#: that oracle disagrees. MEASURED 2026-08-30: it disagrees at 1 (by up to 104.7
#: value points) and agrees at 2 and 3, which is the "one exclusion, one survivor"
#: argument above turned into a measurement rather than a claim about it. An
#: earlier docstring here asserted that 2 also fails. It does not; withdrawn.
_SHORTLIST = 3

#: How close two pair totals must be before they are treated as THE SAME NUMBER
#: and the engine's tie-break ladder (higher VOR next) decides between them.
#:
#: This is not a nicety, it is a correctness fix (audit finding, 2026-08-30).
#: ``pair_score = first_term + expected_second`` adds the same two magnitudes in
#: OPPOSITE ORDERS for two candidates who are each other's modal wheel partner —
#: A's total is ``base(A) + E[base(B)]`` and B's is ``base(B) + E[base(A)]`` — and
#: ``expected_second`` is a running sum over every rollout, so a mathematically
#: exact tie lands a few ULP apart. Measured live at overall #129: Mike Gesicki
#: -76.63739999999999 vs Tua Tagovailoa -76.6374000000004, a margin of 4.1e-13,
#: which ranked a player 38.9 VOR points worse FIRST while the cockpit displayed
#: two identical-looking scores. The ladder tier that exists for exactly this
#: (``-entry.vor``) was never reached, because the delta was not literally zero.
#:
#: The value is a MEASUREMENT, not a taste. Over 640 adjacent pair-score gaps at
#: real pair picks on the live board the distribution has an eight-order-of-
#: magnitude hole: 37 gaps below 1e-12 (float noise; largest ~1e-12) and then
#: nothing at all until 6.2e-4. 1e-9 sits in the middle of that hole, so it can
#: neither merge two totals that genuinely differ nor miss one that does not.
#: Setting it to 0.0 restores the exact-float ordering, which is how the cost of
#: this fix was measured rather than asserted.
DEFAULT_TIE_TOLERANCE = 1e-9


# ------------------------------------------------------------- rollout batch


@dataclass(frozen=True)
class PairBatch:
    """One on-clock rollout batch, recorded JOINTLY rather than marginally.

    ``taken_by_rollout[r]`` is the set of TRACKED player ids the simulated room
    took in rollout ``r`` (usually 0-2 ids, since only ``picks_until_next`` rival
    picks happen). Storing the taken side rather than the survivor side keeps it
    small and makes "did B survive rollout r" a single set miss.

    ``survival`` and ``next_best_vor`` are the exact quantities
    :class:`ziggurat.draft.survival.SurvivalResult` publishes, computed from the
    same loop in the same order, so the shipped engine can be fed from this batch
    and a single decision never rolls twice.
    """

    survival: Mapping[str, float]
    next_best_vor: Mapping[str, float]
    taken_by_rollout: tuple[frozenset[str], ...]
    picks_until_next: int
    rollouts: int


def rollout_pair_batch(
    ctx: PickContext,
    tracked: Sequence[BoardEntry],
    *,
    rng: random.Random,
    rollouts: int = DEFAULT_ROLLOUTS,
    priors: RoomPriors = ROOM_PRIORS_2025,
    kappa: float = DEFAULT_KAPPA,
    positions: Sequence[str] = POSITIONS,
    upcoming: Sequence[tuple[int, int]] | None = None,
) -> PairBatch:
    """The shipped survival rollout, additionally recording per-rollout outcomes.

    This is :func:`ziggurat.draft.survival.rollout_survival`'s loop, line for line
    in the parts that touch ``rng``: the same ``kappa``-widened priors, the same
    ``RankNoiseBot`` / ``AutodraftBot`` split, the same fresh autodraft sample per
    rollout drawn over a SORTED seat list, the same per-seat roster advance from
    ``ctx.opponent_rosters``, the same clone-per-rollout discipline (the shared
    :class:`BoardState` is never mutated). The ONLY difference is that it also
    keeps, per rollout, which tracked ids were taken.

    It is duplicated rather than parameterised because ``survival.py`` is shipped
    production code this variant may not edit. The duplication is fenced by
    ``test_the_pair_batch_reproduces_the_shipped_rollout_exactly``, which asserts
    identical marginals under an identical seed — if the shipped loop changes,
    that test fails rather than this file silently modelling a different room.
    """
    teams = ctx.roster.teams
    if upcoming is None:
        upcoming = upcoming_opponent_picks(ctx)
    picks_until_next = len(upcoming)
    tracked_ids = [e.player_id for e in tracked]
    positions = tuple(positions)

    widened = (
        dataclasses.replace(priors, reach_sigma=priors.reach_sigma * kappa)
        if kappa != 1.0
        else priors
    )
    ranknoise = RankNoiseBot(priors=widened)
    autodraft = AutodraftBot()

    by_id = {e.player_id: e for e in ctx.state.all_entries()}

    if picks_until_next == 0:
        # A true wheel: nobody picks in between, so both halves of the pair are
        # certain. One clone answers "best available per position" exactly.
        clone = ctx.state.clone()
        next_best_vor = {}
        for pos in positions:
            e = clone.front_vor(pos)
            next_best_vor[pos] = float(e.vor) if e is not None else 0.0
        return PairBatch(
            survival={pid: 1.0 for pid in tracked_ids},
            next_best_vor=next_best_vor,
            taken_by_rollout=(frozenset(),),
            picks_until_next=0,
            rollouts=rollouts,
        )

    window_seats = sorted({t for _o, t in upcoming})
    tracked_set = set(tracked_ids)
    survived_counts = {pid: 0 for pid in tracked_ids}
    vor_sums = {pos: 0.0 for pos in positions}
    taken_by_rollout: list[frozenset[str]] = []

    for _ in range(rollouts):
        clone = ctx.state.clone()
        seat_rosters: dict[int, list[BoardEntry]] = {}
        autodraft_seats = {t for t in window_seats if rng.random() < priors.autodraft_fraction}

        for overall, t in upcoming:
            if t not in seat_rosters:
                seat_rosters[t] = list(ctx.opponent_rosters.get(t, ()))
            round_num = (overall - 1) // teams + 1
            mini = PickContext(
                team_slot=t,
                round=round_num,
                overall_pick=overall,
                rounds_total=ctx.rounds_total,
                roster=ctx.roster,
                own_roster=seat_rosters[t],
                state=clone,
                rng=rng,
            )
            bot = autodraft if t in autodraft_seats else ranknoise
            pid = bot.pick(mini)
            clone.take(pid)
            entry = by_id.get(pid)
            if entry is not None:
                seat_rosters[t].append(entry)

        gone = frozenset(pid for pid in tracked_set if pid in clone.taken)
        taken_by_rollout.append(gone)
        for pid in tracked_ids:
            if pid not in gone:
                survived_counts[pid] += 1
        for pos in positions:
            e = clone.front_vor(pos)
            vor_sums[pos] += float(e.vor) if e is not None else 0.0

    return PairBatch(
        survival={pid: survived_counts[pid] / rollouts for pid in tracked_ids},
        next_best_vor={pos: vor_sums[pos] / rollouts for pos in positions},
        taken_by_rollout=tuple(taken_by_rollout),
        picks_until_next=picks_until_next,
        rollouts=rollouts,
    )


#: A caller-supplied replacement for :func:`rollout_pair_batch`. Same signature,
#: so ``rollout_pair_batch`` is itself a valid provider and a caller can wrap it.
#:
#: WHY THIS SEAM EXISTS (audit finding, 2026-08-30). ``PickEngine`` already takes
#: a ``survival=`` provider, and Phase 3 is likely to use it —
#: :mod:`ziggurat.draft.roomcheck` exists precisely to offer refitted constants
#: for the analytic route. But that provider returns MARGINALS
#: (:class:`~ziggurat.draft.engine.SurvivalEstimate`), and the pair maths needs
#: the JOINT outcome per rollout: over two rival picks the candidates' fates are
#: dependent, and "who is the best man still there" cannot be recovered from
#: per-player marginals. So the pair path physically cannot consume the engine's
#: provider. Before this seam existed it silently ran its own rollout instead,
#: which meant an integrator who injected a survival model got THAT model at the
#: eight picks the variant delegates and the module's own rollout at the eight
#: picks the variant exists to change — two survival models inside one draft, no
#: error and no disclosure anywhere. ``pair_analysis`` now REFUSES that
#: combination and names this parameter as the way to supply a joint model.
PairBatchProvider = Callable[..., PairBatch]


# ---------------------------------------------------------------- scoring


def _gather_candidates(
    ctx: PickContext, allowed: Sequence[str] | set[str], width: int
) -> tuple[list[BoardEntry], dict[str, BoardEntry]]:
    """The engine's own candidate gather: best-by-VOR per allowed position, unioned
    with the top-``width`` by ESPN rank across those positions (engine.py D1).

    Returns ``(candidates, best_by_vor_per_position)``. Mirrors
    ``PickEngine.recommend``'s block exactly, using only public
    :class:`~ziggurat.draft.bots.BoardState` methods; pinned by
    ``test_the_candidate_gather_matches_the_engine``.
    """
    best_now: dict[str, BoardEntry] = {}
    for pos in allowed:
        e = ctx.state.front_vor(pos)
        if e is not None:
            best_now[pos] = e
    rank_candidates = ctx.state.window_by_rank(allowed, width)
    by_id: dict[str, BoardEntry] = {}
    for e in list(best_now.values()) + rank_candidates:
        by_id.setdefault(e.player_id, e)
    return list(by_id.values()), best_now


def _allowed_for(
    counts: Mapping[str, int],
    picks_after: int,
    roster: RosterStructure,
    *,
    round_num: int,
    kdst_earliest_round: int,
) -> set[str]:
    """``allowed_positions`` with the engine's own empty-set fallback."""
    allowed = allowed_positions(
        counts,
        picks_after,
        roster,
        round_num=round_num,
        kdst_earliest_round=kdst_earliest_round,
    )
    if not allowed:
        allowed = legal_positions(counts, picks_after, roster) or set(POSITIONS)
    return allowed


def _score_parts(
    entry: BoardEntry,
    *,
    counts: Mapping[str, int],
    round_num: int,
    roster: RosterStructure,
    engine: PickEngine,
    urgency: float,
) -> tuple[float, float]:
    """``(base, one_ply)`` for ``entry`` — the engine's arithmetic, reconstructed.

    ``base`` is the shipped score WITHOUT the ``b_vona * urgency * frac`` term;
    ``one_ply`` is the shipped score itself. Identical in form to
    ``PickEngine.recommend``'s inner loop, including the rule that the
    lineup-reachability fraction discounts only POSITIVE components (a discount
    must never make a player score better).
    """
    pos = entry.position
    need = engine.b_need * _need_fill(pos, round_num, counts, roster, engine.need_schedule)
    rk = engine.b_risk * risk_sign(round_num) * _dispersion(pos)
    frac = _value_fraction(pos, counts, roster)
    base = (
        (entry.vor * frac if entry.vor > 0 else entry.vor)
        + need
        + (rk * frac if rk > 0 else rk)
    )
    return base, base + engine.b_vona * urgency * frac


# ------------------------------------------------------------- the decision


@dataclass(frozen=True)
class PairCandidate:
    """One first-pick option, priced as the FIRST HALF OF A PAIR."""

    entry: BoardEntry
    one_ply_score: float        # what the shipped engine scores him now
    base_now: float             # the pair maths' first term (see urgency_mode)
    expected_second: float      # E[value of the best player left at the wheel pick]
    pair_score: float           # base_now + expected_second — what this ranks on
    survival_next: float        # P(he himself is still there at the wheel pick)
    vona: float                 # engine's VONA at his position (carried for the rec)
    modal_second: BoardEntry | None   # the most frequent best-at-the-wheel partner
    modal_second_share: float         # fraction of rollouts that partner was the best
    modal_second_survival: float      # P(that partner is still there)
    #: Other candidates whose pair total is the SAME as this one to within
    #: ``tie_tolerance`` — i.e. the players this one was ranked against on the
    #: engine's tie-break ladder (higher VOR, then better ESPN rank) rather than on
    #: the pair total. Empty when the pair total decided it outright. Populated
    #: after ranking, which is why it is defaulted.
    tied_with: tuple[str, ...] = ()

    @property
    def player_id(self) -> str:
        return self.entry.player_id


@dataclass(frozen=True)
class PairDecision:
    """Everything behind one pair decision — the diagnostics surface (Rule 6)."""

    engaged: bool
    gap: int                    # rival picks between the operator's two picks
    wheel_overall: int          # 1-based overall of the operator's next pick
    candidates: tuple[PairCandidate, ...]   # ranked, best pair first
    rollouts: int
    pool_exhausted: int         # rollouts where a successor pool had no survivor
    urgency_mode: str
    displacement: str
    #: first-pick POSITION -> the shortlist that pick's wheel partner is drawn
    #: from, best first, as player ids. Exposed because it is the one part of the
    #: search a reader cannot otherwise see: the shortlist is built at the
    #: SUCCESSOR roster state (after the first pick lands), so a position the
    #: first pick saturates is absent from its own list.
    second_pools: Mapping[str, tuple[str, ...]]
    batch: PairBatch | None
    reasons: tuple[str, ...]
    #: The band inside which two pair totals count as the same number and the
    #: engine's ladder decides instead (see :data:`DEFAULT_TIE_TOLERANCE`).
    tie_tolerance: float = DEFAULT_TIE_TOLERANCE
    #: How many groups of two or more candidates the tie band actually merged on
    #: this decision. Zero means every ranking here was decided by the pair total.
    tie_groups: int = 0
    #: Which survival model produced :attr:`batch` — ``"rollout"`` for the shipped
    #: room rollout, ``"provider"`` when the caller injected a joint model.
    batch_source: str = "rollout"


def _rank_key(c: PairCandidate):
    """The engine's tie-break ladder, on the pair score (engine.py D2): higher
    score, then higher VOR, then lower ESPN rank, then player_id. No wall clock,
    no dict order."""
    return (-c.pair_score, -c.entry.vor, c.entry.espn_overall_rank, c.entry.player_id)


def _ladder_key(c: PairCandidate):
    """The ladder BELOW the pair total: higher VOR, then lower ESPN rank, then id.

    This is ``_rank_key`` with its first tier removed, and it is what decides
    between two candidates whose pair totals are the same number (see
    :data:`DEFAULT_TIE_TOLERANCE`).
    """
    return (-c.entry.vor, c.entry.espn_overall_rank, c.entry.player_id)


def _rank_pair_candidates(
    scored: Sequence[PairCandidate], *, tie_tolerance: float
) -> tuple[PairCandidate, ...]:
    """Rank on the pair total, resolving float-noise ties down the engine's ladder.

    Sorts by :func:`_rank_key`, then walks the sorted list grouping each run of
    candidates whose pair total is within ``tie_tolerance`` of that group's LEADER
    (leader-anchored, so a group can never be wider than the tolerance no matter
    how many members it has, and the grouping is a pure function of the numbers —
    no wall clock, no dict order, no chaining). Every group of two or more is
    re-sorted by :func:`_ladder_key` and each member records the others in
    :attr:`PairCandidate.tied_with`, so the fact that the pair total did NOT decide
    this is visible to the operator instead of being swallowed by a 4e-13 margin.

    ``tie_tolerance <= 0`` is the exact-float ordering this module shipped with
    before the fix, kept so its cost is measurable rather than asserted.
    """
    ordered = sorted(scored, key=_rank_key)
    if tie_tolerance <= 0.0 or len(ordered) < 2:
        return tuple(ordered)
    out: list[PairCandidate] = []
    i = 0
    while i < len(ordered):
        lead = ordered[i].pair_score
        j = i + 1
        while j < len(ordered) and abs(ordered[j].pair_score - lead) <= tie_tolerance:
            j += 1
        group = ordered[i:j]
        if len(group) > 1:
            ids = tuple(c.player_id for c in group)
            group = [
                dataclasses.replace(
                    c, tied_with=tuple(p for p in ids if p != c.player_id)
                )
                for c in sorted(group, key=_ladder_key)
            ]
        out.extend(group)
        i = j
    return tuple(out)


@dataclass(frozen=True)
class PairWindow:
    """Is this pick the first half of a tight pair? Pure snake geometry.

    Deliberately touches NO randomness, so :class:`WheelPicker` can decide whether
    to engage BEFORE it derives a rollout child from ``ctx.rng``. That ordering is
    what makes a delegated (non-pair) pick byte-identical to the bare engine's:
    the engine derives exactly one child inside its own ``recommend``, and a
    variant that had already drawn one would hand it a different stream and so a
    different survival sample at every pick it was not even changing.
    """

    engaged: bool
    gap: int              # rival picks between this pick and the operator's next
    wheel_overall: int    # 1-based overall of that next pick (-1 when there is none)
    why: str


def pair_window(ctx: PickContext, max_pair_gap: int = DEFAULT_MAX_PAIR_GAP) -> PairWindow:
    """Classify this pick as a pair-opener or not (no rng, no board scan)."""
    if ctx.picks_after <= 0:
        return PairWindow(
            engaged=False, gap=-1, wheel_overall=-1,
            why="this is your last pick of the draft — there is no pair to plan",
        )
    gap = len(upcoming_opponent_picks(ctx))
    wheel_overall = ctx.overall_pick + gap + 1
    if gap > max_pair_gap:
        return PairWindow(
            engaged=False, gap=gap, wheel_overall=wheel_overall,
            why=(
                f"your next pick is {gap} rival picks away (#{wheel_overall}) — too far "
                f"to plan as a pair, so this is the normal one-pick recommendation"
            ),
        )
    return PairWindow(engaged=True, gap=gap, wheel_overall=wheel_overall, why="")


def pair_analysis(
    ctx: PickContext,
    *,
    engine: PickEngine,
    rng: random.Random,
    max_pair_gap: int = DEFAULT_MAX_PAIR_GAP,
    second_width: int = DEFAULT_SECOND_WIDTH,
    pair_limit: int = DEFAULT_PAIR_LIMIT,
    urgency_mode: str = "keep",
    displacement: str = "rank",
    rollouts: int | None = None,
    kappa: float | None = None,
    priors: RoomPriors | None = None,
    tie_tolerance: float = DEFAULT_TIE_TOLERANCE,
    batch_provider: PairBatchProvider | None = None,
) -> PairDecision:
    """Price every plausible (first, second) pair for this pick from ONE batch.

    Returns ``engaged=False`` (and an empty candidate tuple) when this pick is not
    the first half of a tight pair — the caller then delegates to the shipped
    engine. ``rng`` is the caller's already-derived rollout child; this function
    draws from it and from nothing else.

    THE SURVIVAL MODEL IS THE ONE ARGUMENT THAT CANNOT BE INFERRED. If ``engine``
    carries an injected ``survival=`` provider, this function REFUSES rather than
    quietly running its own rollout beside it — see :data:`PairBatchProvider` for
    why the engine's marginal provider cannot be consumed here and what to pass
    instead. The refusal happens only on the engaged path: a disengaged pick
    delegates to the engine, which honours its own provider normally.
    """
    if urgency_mode not in URGENCY_MODES:
        raise ValueError(
            f"urgency_mode must be one of {URGENCY_MODES!r}, got {urgency_mode!r}"
        )
    if displacement not in DISPLACEMENT_MODES:
        raise ValueError(
            f"displacement must be one of {DISPLACEMENT_MODES!r}, got {displacement!r}"
        )

    reasons: list[str] = []
    window = pair_window(ctx, max_pair_gap)
    gap, wheel_overall = window.gap, window.wheel_overall
    if not window.engaged:
        return PairDecision(
            engaged=False, gap=gap, wheel_overall=wheel_overall, candidates=(),
            rollouts=0, pool_exhausted=0, urgency_mode=urgency_mode,
            displacement=displacement, second_pools={}, batch=None,
            reasons=(window.why,), tie_tolerance=tie_tolerance,
        )

    if batch_provider is None:
        if getattr(engine, "survival", None) is not None:
            raise ValueError(
                "this PickEngine carries an injected survival= provider, which the "
                "pair path cannot use: that provider returns per-player marginals "
                "and the pair maths needs the JOINT per-rollout outcome (over the "
                "wheel gap the candidates' fates are dependent, so 'who is the best "
                "man still there' is not recoverable from marginals). Running the "
                "module's own rollout anyway would put TWO survival models inside "
                "one draft — the injected one at every delegated pick and the "
                "rollout at every pair pick — so this refuses instead. Pass "
                "batch_provider=<callable with rollout_pair_batch's signature "
                "returning a PairBatch> to supply a joint model for both halves, "
                "or batch_provider=rollout_pair_batch to say explicitly that the "
                "shipped room rollout is what you want at pair picks."
            )
        batch_provider = rollout_pair_batch
    batch_source = "rollout" if batch_provider is rollout_pair_batch else "provider"

    roster = ctx.roster
    counts = position_counts(ctx.own_roster)
    allowed1 = _allowed_for(
        counts, ctx.picks_after, roster,
        round_num=ctx.round, kdst_earliest_round=engine.kdst_earliest_round,
    )
    candidates, best_now = _gather_candidates(ctx, allowed1, engine.candidate_width)
    if not candidates:  # degenerate: board thin at every allowed position
        e = ctx.state.best_by_rank(allowed1) or ctx.state.best_by_rank(POSITIONS)
        if e is None:  # pragma: no cover - only an exhausted board
            raise RuntimeError("draft board exhausted: no available player to pick")
        candidates = [e]
        best_now.setdefault(e.position, e)

    # --- the successor pools. A candidate changes the roster ONLY through his
    # position, so there are at most six distinct states to consider no matter how
    # many candidates there are. This is what keeps the pair enumeration linear.
    round2 = ctx.round + 1
    picks_after2 = ctx.picks_after - 1
    pools: dict[str, list[BoardEntry]] = {}
    for pos in sorted({c.position for c in candidates}):
        counts2 = dict(counts)
        counts2[pos] = counts2.get(pos, 0) + 1
        allowed2 = _allowed_for(
            counts2, picks_after2, roster,
            round_num=round2, kdst_earliest_round=engine.kdst_earliest_round,
        )
        pool, _ = _gather_candidates(ctx, allowed2, second_width)
        # value each pool member AT the successor state, best first
        valued = []
        for e in pool:
            b, _ = _score_parts(
                e, counts=counts2, round_num=round2, roster=roster,
                engine=engine, urgency=0.0,
            )
            valued.append((b, e))
        valued.sort(key=lambda t: (-t[0], -t[1].vor, t[1].espn_overall_rank, t[1].player_id))
        pools[pos] = valued

    tracked: dict[str, BoardEntry] = {c.player_id: c for c in candidates}
    for valued in pools.values():
        for _b, e in valued:
            tracked.setdefault(e.player_id, e)

    positions = sorted(best_now)
    batch = batch_provider(
        ctx,
        list(tracked.values()),
        rng=rng,
        rollouts=DEFAULT_ROLLOUTS if rollouts is None else rollouts,
        priors=ROOM_PRIORS_2025 if priors is None else priors,
        kappa=DEFAULT_KAPPA if kappa is None else kappa,
        positions=positions,
    )

    # --- the engine's own urgency, from the same batch (so "keep" mode is the
    # engine's real score and the delegated reasons stay truthful).
    urgency: dict[str, float] = {}
    vona_by_pos: dict[str, float] = {}
    for pos, bn in best_now.items():
        nb = batch.next_best_vor.get(pos, bn.vor)
        vona = max(0.0, bn.vor - nb)
        s_top = batch.survival.get(bn.player_id, 1.0)
        vona_by_pos[pos] = vona
        urgency[pos] = vona * (1.0 - s_top)

    # --- per (rollout, successor position): the shortlist the wheel pick would be
    # drawn from in that rollout. Built ONCE per position rather than once per
    # candidate, which is what collapses the pair enumeration from quadratic to
    # linear: two candidates at the same position leave the roster in the same
    # state and so share a shortlist entirely.
    n_roll = len(batch.taken_by_rollout)
    #: position -> per-rollout ``(top-_SHORTLIST survivors by value, the survivor
    #: the ROOM would most likely take next)``.
    shortlists: dict[str, list[tuple[tuple[tuple[float, str], ...], str | None]]] = {}
    for pos, valued in pools.items():
        rows: list[tuple[tuple[tuple[float, str], ...], str | None]] = []
        for gone in batch.taken_by_rollout:
            best: list[tuple[float, str]] = []
            rank_best_id: str | None = None
            rank_best: int | None = None
            for b, e in valued:
                if e.player_id in gone:
                    continue
                if len(best) < _SHORTLIST:
                    best.append((float(b), e.player_id))
                # the room drafts off the ESPN board, so its substitute pick is the
                # best-RANKED survivor, not the best-valued one
                if rank_best is None or e.espn_overall_rank < rank_best:
                    rank_best, rank_best_id = e.espn_overall_rank, e.player_id
            rows.append((tuple(best), rank_best_id))
        shortlists[pos] = rows

    # --- price each first-pick candidate.
    scored: list[PairCandidate] = []
    ranked_by_one_ply = []
    for c in candidates:
        base, one_ply = _score_parts(
            c, counts=counts, round_num=ctx.round, roster=roster,
            engine=engine, urgency=urgency.get(c.position, 0.0),
        )
        ranked_by_one_ply.append((one_ply, base, c))
    ranked_by_one_ply.sort(
        key=lambda t: (-t[0], -t[2].vor, t[2].espn_overall_rank, t[2].player_id)
    )
    considered = ranked_by_one_ply[: max(1, pair_limit)]
    truncated = len(ranked_by_one_ply) - len(considered)

    pool_exhausted = 0
    for one_ply, base, c in considered:
        rows = shortlists[c.position]
        total = 0.0
        partner_counts: dict[str, int] = {}
        for (best, rank_best_id), gone in zip(rows, batch.taken_by_rollout, strict=True):
            # The candidate himself is ours the moment we take him, so he is never
            # available to his own second pick even when he tops that shortlist.
            excluded = {c.player_id}
            if displacement == "rank" and c.player_id in gone and rank_best_id is not None:
                # THE DISPLACEMENT CORRECTION. The batch was rolled on a board that
                # still held this candidate, so in the rollouts where the room took
                # him it spent a pick we are about to make impossible. Had he been
                # ours already, that rival would have taken its next choice — off
                # the ESPN board — and one more of this shortlist would be gone.
                # Ignoring it makes the wheel systematically optimistic about
                # exactly the contested players it is most inclined to reach for.
                excluded.add(rank_best_id)
            value, partner = 0.0, None
            for v, pid in best:
                if pid in excluded:
                    continue
                value, partner = v, pid
                break
            total += value
            if partner is None:
                pool_exhausted += 1
            else:
                partner_counts[partner] = partner_counts.get(partner, 0) + 1
        expected_second = total / n_roll if n_roll else 0.0
        first_term = base if urgency_mode == "drop" else one_ply
        modal_id = None
        modal_share = 0.0
        if partner_counts:
            # deterministic mode: highest count, ties broken by player_id
            modal_id = min(partner_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            modal_share = partner_counts[modal_id] / n_roll
        scored.append(
            PairCandidate(
                entry=c,
                one_ply_score=one_ply,
                base_now=first_term,
                expected_second=expected_second,
                pair_score=first_term + expected_second,
                survival_next=batch.survival.get(c.player_id, 1.0),
                vona=vona_by_pos.get(c.position, 0.0),
                modal_second=tracked.get(modal_id) if modal_id else None,
                modal_second_share=modal_share,
                modal_second_survival=(
                    batch.survival.get(modal_id, 1.0) if modal_id else 0.0
                ),
            )
        )

    ranked = _rank_pair_candidates(scored, tie_tolerance=tie_tolerance)
    tie_groups = len(
        {frozenset((*c.tied_with, c.player_id)) for c in ranked if c.tied_with}
    )

    if gap == 0:
        reasons.append(
            f"You pick twice back to back — this pick (#{ctx.overall_pick}) and "
            f"#{wheel_overall}. Nobody picks in between, so both of those players "
            f"are yours: this recommendation is the best PAIR, not the best single."
        )
    else:
        reasons.append(
            f"You pick again very soon — #{wheel_overall}, only {gap} rival "
            f"{'pick' if gap == 1 else 'picks'} away. This recommendation is the "
            f"best PAIR of players to end up holding, not just the best one now."
        )
    reasons.append(
        f"The pair was scored over {batch.rollouts} simulated versions of what "
        f"those {gap} rival {'pick' if gap == 1 else 'picks'} could be."
        if gap
        else "With no rival picks in between there is nothing to simulate — both halves are certain."
    )
    if urgency_mode == "drop":
        reasons.append(
            "Because the wait is now measured directly, the engine's usual "
            "'this position is about to run dry' shortcut is switched off for this "
            "pick so the same effect is not counted twice."
        )
    else:
        reasons.append(
            "The engine's usual one-pick score is kept in full and the second pick's "
            "value is added on top (this deliberately counts the wait twice)."
        )
    if truncated:
        reasons.append(
            f"HONESTY: {truncated} further candidate(s) were ranked out before the "
            f"pair was priced, on the one-pick score alone — a player who is only "
            f"good as HALF OF A PAIR and poor on his own would not have been seen."
        )
    if pool_exhausted:
        reasons.append(
            f"HONESTY: in {pool_exhausted} of {n_roll * len(considered)} "
            f"simulated cases the shortlist for your second pick was emptied by the "
            f"room, so that case was scored as zero. Zero is not automatically the "
            f"cautious choice: late in a draft the best man actually left can be "
            f"worth LESS than nothing to you, and where that is true these totals "
            f"are too generous rather than too mean."
        )
    if tie_groups:
        # Plain words, not scientific notation. The default tolerance is 1e-9 and
        # "within 1e-09 of a point" appeared on 12% of operator panels — the
        # operator is a football novice reading this on a 90-second clock, and
        # Rule 6 is about what he can ACT on (audit minor). A caller who widens
        # the tolerance to something a human could notice gets the number quoted.
        how_close = (
            "identical to well under a thousandth of a point"
            if tie_tolerance < 0.001
            else f"within {tie_tolerance:.3f} of a point"
        )
        reasons.append(
            f"HONESTY: {tie_groups} set(s) of options came out with the SAME "
            f"two-pick total ({how_close}, i.e. arithmetic rounding, not a real "
            f"difference). Those were put in order by who is worth more on his own, "
            f"not by the pair total."
        )
    if batch_source == "provider":
        reasons.append(
            "HONESTY: the odds of each player lasting until your next pick came "
            "from a model supplied by the caller, not from this tool's own "
            "simulation of the room."
        )
    if gap and displacement == "rank":
        reasons.append(
            "Allowance is made for the fact that taking a player the room wanted "
            "frees that rival to take someone else you might have had."
        )
    reasons.append(
        "The second half of the pair is valued on what he is worth, not on what "
        "waiting past him would cost — that longer horizon is not in this number. "
        "This is a two-pick lookahead; it says nothing about the pick after that."
    )

    return PairDecision(
        engaged=True,
        gap=gap,
        wheel_overall=wheel_overall,
        candidates=ranked,
        rollouts=batch.rollouts,
        pool_exhausted=pool_exhausted,
        urgency_mode=urgency_mode,
        displacement=displacement,
        second_pools={p: tuple(e.player_id for _b, e in v) for p, v in pools.items()},
        batch=batch,
        reasons=tuple(reasons),
        tie_tolerance=tie_tolerance,
        tie_groups=tie_groups,
        batch_source=batch_source,
    )


# ------------------------------------------------------------------ picker


@dataclass(frozen=True)
class WheelPicker:
    """A :class:`~ziggurat.draft.bots.Picker` that thinks in pairs at the wheel.

    Wraps a shipped :class:`~ziggurat.draft.engine.PickEngine` and delegates to it
    VERBATIM at every pick that is not the first half of a tight pair, so the only
    behaviour that can move is the behaviour this variant exists to change.

    RANDOMNESS, precisely. Exactly one rollout child is derived from ``ctx.rng``
    per decision, on both paths: at a delegated pick the shipped engine derives it
    inside its own ``recommend`` (so that pick is byte-identical to a bare-engine
    draft), and at a pair pick this class derives it instead and never calls into
    that path. ``pick`` is ``recommend(top=1)``, so the two consume the stream
    identically and a ``(state, rosters, seed)`` replay is bit-for-bit
    reproducible — the property the draft cockpit's journal replay depends on.
    A variant draft is of course not the same draft as an engine draft; that
    difference is what the paired harness measures, and it comes only from the
    pair decisions themselves, never from a disturbed stream.
    """

    engine: PickEngine = dataclasses.field(default_factory=PickEngine)
    max_pair_gap: int = DEFAULT_MAX_PAIR_GAP
    second_width: int = DEFAULT_SECOND_WIDTH
    pair_limit: int = DEFAULT_PAIR_LIMIT
    urgency_mode: str = "keep"
    displacement: str = "rank"
    #: See :data:`DEFAULT_TIE_TOLERANCE`. 0.0 restores the exact-float ordering
    #: this module shipped with before the 2026-08-30 audit, so the cost of the
    #: fix is measurable through the paired harness rather than asserted.
    tie_tolerance: float = DEFAULT_TIE_TOLERANCE
    #: A joint survival model for the pair path. REQUIRED when ``engine`` carries
    #: an injected ``survival=`` provider — see :data:`PairBatchProvider`.
    batch_provider: PairBatchProvider | None = None

    def __post_init__(self) -> None:
        if self.urgency_mode not in URGENCY_MODES:
            raise ValueError(
                f"urgency_mode must be one of {URGENCY_MODES!r}, got {self.urgency_mode!r}"
            )
        if self.displacement not in DISPLACEMENT_MODES:
            raise ValueError(
                f"displacement must be one of {DISPLACEMENT_MODES!r}, got "
                f"{self.displacement!r}"
            )

    # -- Picker seam -------------------------------------------------------

    def pick(self, ctx: PickContext) -> str:
        return self.recommend(ctx, top=1)[0].player_id

    def recommend(self, ctx: PickContext, *, top: int = 5) -> tuple[PickRec, ...]:
        """Top-``top`` recommendations, best pair first, each with legible reasons.

        At a non-pair pick this is the wrapped engine's own output, untouched.
        """
        if not pair_window(ctx, self.max_pair_gap).engaged:
            # Not a pair: the shipped engine decides, and it derives its own
            # rollout child from ctx.rng exactly as it always does — so this path
            # is byte-identical to a bare-engine draft, and the only behaviour the
            # A/B can attribute to this variant is the pair decision itself.
            return self.engine.recommend(ctx, top=top)
        rollout_rng = random.Random(ctx.rng.getrandbits(64))
        decision = pair_analysis(
            ctx,
            engine=self.engine,
            rng=rollout_rng,
            max_pair_gap=self.max_pair_gap,
            second_width=self.second_width,
            pair_limit=self.pair_limit,
            urgency_mode=self.urgency_mode,
            displacement=self.displacement,
            rollouts=self.engine.rollouts,
            kappa=self.engine.kappa,
            priors=self.engine.room_priors,
            tie_tolerance=self.tie_tolerance,
            batch_provider=self.batch_provider,
        )
        if not decision.engaged:  # pragma: no cover - the window already agreed
            return self.engine.recommend(ctx, top=top)

        counts = position_counts(ctx.own_roster)
        n = max(1, top)
        chosen = decision.candidates[:n]
        recs: list[PickRec] = []
        for i, cand in enumerate(chosen):
            alts = tuple(
                (
                    other.entry.name or other.entry.player_id,
                    self._why_not(other, decision),
                )
                for other in decision.candidates[i + 1 : i + 4]
            )
            # The engine's own reason renderer, called rather than re-implemented
            # (same quarantined package): the operator must read the SAME need /
            # risk / survival sentences he would read from the shipped engine,
            # with the pair lines prepended, not a second dialect of them.
            base_rec = self.engine._build_rec(
                ctx,
                cand.entry,
                cand.pair_score,
                cand.survival_next,
                cand.vona,
                counts,
                alts,
            )
            recs.append(
                dataclasses.replace(
                    base_rec,
                    reasons=self._pair_reasons(cand, decision) + base_rec.reasons,
                )
            )
        return tuple(recs)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _pair_reasons(cand: PairCandidate, decision: PairDecision) -> tuple[str, ...]:
        """The variant's own reason lines, prepended to the engine's.

        DELIBERATELY SHORT. The cockpit renders ``PickRec.reasons`` verbatim and
        the operator reads them on a 90-second clock, so only the lines that change
        what he should DO live here: the pair framing, who the partner is, what the
        score means (and that it is not a normal score), and any warning that the
        number should be read as a floor. The methodology — how many rooms were
        simulated, which term was switched off, whether the displacement correction
        is on — stays on :attr:`PairDecision.reasons`, which is the diagnostics
        surface, not the on-clock one.
        """
        out = [decision.reasons[0]]
        partner = cand.modal_second
        if partner is not None:
            pname = partner.name or partner.player_id
            share = round(cand.modal_second_share * 100)
            # WHY THIS IS PHRASED AS AN ASSUMPTION AND NOT A PROMISE (item 3.11
            # audit finding 4). The earlier wording — "Take him now and X is the
            # most likely best value left at #N (in 100% of the simulated rooms;
            # about 100% chance he is still on the board then)" — read as a
            # commitment about the cockpit's OWN next recommendation, and it is
            # not one: the quoted figures are about the ROOM (right 94% of the
            # time), while pick #N is re-decided from scratch through the full
            # composed engine at the state that actually exists then. Measured
            # over 10 simulated drafts at this seat, 80 first-of-pair panels: 32
            # of those sentences were broken by the tool's own next pick, and 26
            # of the 32 with the named partner STILL ON THE BOARD — a third of
            # every promise made, at a median quoted share of 100%. The number is
            # right; the sentence was making a claim the number does not support,
            # in rounds 1-3, the window the runbook tells the operator to watch in
            # person precisely to calibrate his trust in the tool.
            if decision.gap == 0:
                out.append(
                    f"The pairing this score assumes: him now, then {pname} at your "
                    f"very next pick (#{decision.wheel_overall})."
                )
            else:
                surv = round(cand.modal_second_survival * 100)
                out.append(
                    f"The pairing this score assumes: him now, then {pname} at "
                    f"#{decision.wheel_overall} — {pname} is what the room most often "
                    f"leaves ({share}% of the simulated rooms; about {surv}% chance it "
                    f"has not taken him by then)."
                )
            out.append(
                f"That pairing is the ASSUMPTION BEHIND THE NUMBER, not a plan for "
                f"#{decision.wheel_overall}. That pick gets re-decided from scratch "
                f"when it arrives, against your roster as it then is, and it lands on "
                f"somebody other than {pname} about a third of the time even with "
                f"{pname} still available — that is normal and is not the tool "
                f"contradicting itself. What is being claimed here is the VALUE of "
                f"holding a pair, not who the second one turns out to be."
            )
        overlap = (
            # The double count is a property of the NUMBER being displayed, not
            # methodology trivia, so it belongs on the on-clock surface next to the
            # number rather than only on the diagnostics object (audit, 2026-08-30).
            " The two halves overlap: the first number already includes an "
            "allowance for the wait that the second one measures directly, so this "
            "total is deliberately generous."
            if decision.urgency_mode == "keep"
            else " The wait is counted once here, not twice."
        )
        out.append(
            f"Two-pick total behind this: about {cand.pair_score:.0f} "
            f"({cand.base_now:.0f} from him now, {cand.expected_second:.0f} expected "
            f"from your next pick).{overlap} That total is what ranks him here, so it "
            f"is NOT comparable to a normal single-pick score."
        )
        if cand.tied_with:
            names = [
                (o.entry.name or o.entry.player_id)
                for o in decision.candidates
                if o.player_id in set(cand.tied_with)
            ]
            joined = ", ".join(names) if names else "another option"
            out.append(
                f"Careful: his two-pick total is the SAME number as {joined} — the "
                f"difference is arithmetic rounding, not a real gap. He is listed "
                f"first because he is worth more on his own, so treat this as a "
                f"coin-flip between them rather than a ranking."
            )
        out.append(
            "The second half of the pair is valued on what he is worth, not on what "
            "waiting past him would cost — that longer horizon is not in this number."
        )
        out.extend(r for r in decision.reasons if r.startswith("HONESTY"))
        return tuple(out)

    @staticmethod
    def _why_not(cand: PairCandidate, decision: PairDecision) -> str:
        gap_pts = cand.pair_score
        if cand.survival_next >= 0.75 and decision.gap:
            return (
                f"you can likely wait — about {round(cand.survival_next * 100)}% he "
                f"is still there at #{decision.wheel_overall}"
            )
        return f"a slightly smaller two-pick total ({gap_pts:.0f})"
