"""Item 2.4 — draft-day posture check with hysteresis (the mandated guard).

DELETABLE package (Rule 8): lives under ``ziggurat/draft/`` and nothing outside
that package imports it. Pure in-memory logic over a duck-typed draft session;
there is **no runtime import of ``session.py``** (that module is built in
parallel and would create an import cycle) — the session surface this monitor
reads is captured by the :class:`PostureSession` structural Protocol below, so a
type-checker sees it while the runtime only touches attributes.

WHAT THIS IS (recon ``intel/research/tui-2.4-recon.md`` §2 "Posture-check +
hysteresis"; carried from the 2.3 design note Part V addendum #2): at a snake turn
the operator's engine already re-plans from the true board — it is state
contingent — but the operator can still *drift* into a lopsided roster and, more
importantly, get squeezed by the room draining a position before they pivot. This
monitor gives an explicit, legible nudge. It compares the operator's projected
final starting lineup under the engine's CURRENT round-conditioned need-schedule
against each shipped archetype schedule (Zero-RB / Hero-RB / Robust-RB, from
``engine.ARCHETYPE_NEED_SCHEDULES``), FROM THE CURRENT TRUE STATE, and — only when
an alternative lean has clearly and *persistently* beaten the current one —
surfaces a one-sentence suggestion.

This is **advice about posture, not a strategy switch.** The engine is never
retuned by this module; it only tells the operator "you're leaning RB-heavy; a
receiver lean projects a stronger final lineup." Accept/dismiss is the operator's
call (single keystroke in the TUI).

THE COMPARATOR — a SHORT PAIRED ROOM CONTINUATION (documented per the build brief:
"a small fixed-rollout evaluation with the session's deterministic seeding").
Why not the cheaper "engine scoring of the top recommendation under each schedule"?
Because positional *drift* is a scarcity story: hoarding RB only hurts if the good
receivers are gone by the time you pivot — which one pick, or a frozen board,
cannot show. So for each posture we run a handful of deterministic continuations
of the REST of the draft from the current true state: the operator seat drafts with
the SAME engine, only ``need_schedule`` swapped (survival neutralised with the
zero-cost :class:`_FrontSurvival` stub so no nested Monte-Carlo runs and rng use is
identical across postures), while the rest of the room drafts with the calibrated
2.2 bots (``RankNoiseBot`` + sampled ``AutodraftBot``, ``ROOM_PRIORS_2025``), which
generatively drain positions and create the scarcity that makes drift real. Each
continuation is scored with ``simulator.optimal_starting_points`` — the same
self-graded house-projection currency the 2.3 margin is measured in. The rollouts
are seeded deterministically from the session so the same state always yields the
same projection (needed for the hysteresis machine to see a *stable* signal). The
"edge" of an alternative is ``alt_points - current_points`` in season points the
operator can read. We deliberately do NOT compare raw ``pick_score`` across
schedules (a schedule that up-weights a position inflates its own score — apples to
oranges); we compare the resulting rosters. Because the archetype schedules differ
from balanced only in the early RB rounds, the comparator naturally goes quiet once
those rounds are past — correct: posture is an early-rounds question.

THE HYSTERESIS STATE MACHINE (the mandated guard — Rule 6: never a contradictory or
flip-flopping nudge). Advice fires ONLY when the best-alternative edge exceeds
``margin`` AND has held for ``>= consecutive`` evaluations for the SAME
alternative; it fires once, then stays quiet until the operator acts. After
:meth:`dismiss`, a ``cooldown`` of evaluations suppresses everything. Acceptance
(:meth:`accept`) resets the machine clean. Defaults are tuned conservative so
advice is rare. The football projection is INJECTED (``evaluator``, default
:func:`project_postures`) so the hysteresis machine is exercised in tests without a
live draft — the same seam-injection pattern the pick engine uses for survival.

Rule 1: no DB accessor here (state comes off the already-loaded session). Rule 2:
no scoring constant (points come from the board via the engine + valuation).
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ziggurat.draft.bots import (
    AutodraftBot,
    BoardEntry,
    BoardState,
    PickContext,
    RankNoiseBot,
)
from ziggurat.draft.engine import (
    ARCHETYPE_NEED_SCHEDULES,
    NeedSchedule,
    PickEngine,
    SurvivalEstimate,
)
from ziggurat.draft.priors import ROOM_PRIORS_2025, RoomPriors
from ziggurat.draft.simulator import ROUNDS, optimal_starting_points, snake_sequence

# --------------------------------------------------------------------- defaults
#
# Conservative by design (recon §2: "advice should be rare"). ``margin`` is in
# season points of projected final starting-lineup value; ``consecutive`` is how
# many snake-turn evaluations the same lean must win before we say anything (a
# snake turn is ~10-18 picks apart, so 2 is already "two of your turns in a row");
# ``cooldown`` is how many evaluations a dismissal buys of silence.
DEFAULT_MARGIN = 12.0
DEFAULT_CONSECUTIVE = 2
DEFAULT_COOLDOWN = 3

# Comparator budget. Small fixed rollout count with deterministic seeding; the
# room bots make each continuation cheap and the estimate stable. Tunable at
# rehearsal if the draft-day machine shows jank.
POSTURE_ROLLOUTS = 10

# Minimum drafted players before a posture read is meaningful (nothing has drifted
# on pick 1). Small; purely a "don't nag at the very top" guard.
_MIN_ROSTER_FOR_POSTURE = 2

# Novice-legible position words (Rule 6 — the message never shows a code).
_POSITION_WORDS: Mapping[str, str] = {
    "QB": "quarterback",
    "RB": "running back",
    "WR": "wide receiver",
    "TE": "tight end",
    "K": "kicker",
    "DST": "defense",
}


# ---------------------------------------------------------- duck-typed session
#
# The exact surface the DEFAULT comparator reads off a live draft session. Kept as
# a structural Protocol so this module never imports session.py at runtime (the
# parallel builder owns that file). INTEGRATOR NOTE: session.py must expose these
# as attributes/properties for the default comparator to engage; if a name is
# absent the comparator degrades to "no advice" (non-gating) rather than crashing.


@runtime_checkable
class PostureSession(Protocol):
    """Read-only draft state the posture comparator needs (duck-typed)."""

    complete: bool
    overall_pick: int          # 1-based next pick
    operator_slot: int         # 0-based seat id
    own_roster: Sequence[BoardEntry]
    opponent_rosters: Mapping[int, Sequence[BoardEntry]]
    taken: object              # AbstractSet[str]; iterated only
    board: Sequence[BoardEntry]
    roster: object             # RosterStructure (needs .teams / starters / flex_*)
    rounds_total: int
    pick_order: Sequence[int]  # draft-order position -> seat id
    engine: PickEngine         # the operator's live pick engine (weights + schedule)
    session_seed: int          # deterministic seed base for the continuation


# --------------------------------------------------------------- projection type


@dataclass(frozen=True)
class PostureProjection:
    """One posture comparison from the current true state (comparator output).

    ``edge = alternative_points - current_points`` in season points of projected
    final optimal starting-lineup value. ``lean_position`` is the canonical
    position the winning alternative leans toward (for the message). The monitor
    gates this through the hysteresis machine; a projection is not itself advice.
    """

    current_label: str
    alternative_label: str | None
    lean_position: str | None
    edge: float
    current_points: float
    alternative_points: float
    overall_pick: int


# ------------------------------------------------------------------- the advice


@dataclass(frozen=True)
class PostureAdvice:
    """A surfaced posture nudge — one novice-legible sentence plus its numbers.

    ``message`` is a complete sentence a football novice can act on (Rule 6); the
    remaining fields are the supporting numbers the TUI can show beneath it.
    """

    message: str
    current_label: str
    alternative_label: str
    lean_position: str | None
    edge_points: float
    current_points: float
    alternative_points: float
    overall_pick: int
    evaluations_held: int


# ------------------------------------------------------- the default comparator


class _FrontSurvival:
    """Zero-cost survival stub: everything survives, so VONA/urgency are 0.

    Injected into the throwaway per-posture operator engine so a continuation
    never fires a nested Monte-Carlo rollout and consumes rng identically across
    postures (keeping the paired room draws aligned). ``next_best_vor[pos]`` is set
    to the best candidate VOR at that position, so ``VONA = max(0, best - nb) = 0``.
    """

    def __call__(
        self, ctx: PickContext, *, candidates, positions, rng
    ) -> SurvivalEstimate:
        best_vor: dict[str, float] = {}
        for c in candidates:
            best_vor[c.position] = max(best_vor.get(c.position, float("-inf")), c.vor)
        survival = {c.player_id: 1.0 for c in candidates}
        next_best_vor = {pos: best_vor.get(pos, 0.0) for pos in positions}
        return SurvivalEstimate(survival=survival, next_best_vor=next_best_vor)


def _schedule_label(schedule: NeedSchedule) -> str:
    """The archetype name for a need-schedule, or ``"current"`` if custom."""
    for name, sched in ARCHETYPE_NEED_SCHEDULES.items():
        if sched is schedule:
            return name
    return "current"


def _continue_and_score(
    *,
    board: Sequence[BoardEntry],
    board_by_id: Mapping[str, BoardEntry],
    base_state: BoardState,
    own_roster: Sequence[BoardEntry],
    opponent_rosters: Mapping[int, Sequence[BoardEntry]],
    roster,
    pick_order: Sequence[int],
    operator_slot: int,
    rounds_total: int,
    start_overall: int,
    operator_engine: PickEngine,
    autodraft_seats: frozenset[int],
    priors: RoomPriors,
    rng: random.Random,
) -> float:
    """Finish the draft from the true state once and score the operator's lineup.

    The operator seat drafts with ``operator_engine`` (a posture-swapped clone);
    every other seat drafts with the calibrated room (``RankNoiseBot`` or, if
    sampled into ``autodraft_seats``, ``AutodraftBot``). Draws come from ``rng``.
    """
    teams = roster.teams
    state = base_state.clone()  # cheap: shares the immutable sorted lists
    rosters: dict[int, list[BoardEntry]] = {
        t: list(opponent_rosters.get(t, ())) for t in range(teams)
    }
    rosters[operator_slot] = list(own_roster)

    sequence = snake_sequence(pick_order, rounds_total)
    max_pick = teams * rounds_total
    for overall in range(start_overall, max_pick + 1):
        team = sequence[overall - 1]
        round_num = (overall - 1) // teams + 1
        ctx = PickContext(
            team_slot=team,
            round=round_num,
            overall_pick=overall,
            rounds_total=rounds_total,
            roster=roster,
            own_roster=rosters[team],
            state=state,
            rng=rng,
            opponent_rosters=MappingProxyType(
                {t: rosters[t] for t in range(teams) if t != team}
            ),
        )
        if team == operator_slot:
            picker = operator_engine
        elif team in autodraft_seats:
            picker = AutodraftBot()
        else:
            picker = RankNoiseBot(priors=priors)
        pid = picker.pick(ctx)
        state.take(pid)
        entry = board_by_id.get(pid)
        if entry is not None:
            rosters[team].append(entry)
    return optimal_starting_points(rosters[operator_slot], roster)


def project_postures(
    session: PostureSession, *, rollouts: int = POSTURE_ROLLOUTS
) -> PostureProjection | None:
    """Compare the operator's current need-schedule vs each archetype (continuation).

    The DEFAULT :class:`PostureMonitor` evaluator. Reads the duck-typed
    :class:`PostureSession` surface; returns ``None`` (no advice) when the draft is
    over, too little roster has been drafted to have drifted, or a required session
    seam is absent (non-gating — the check simply stays quiet). Otherwise returns
    the best-alternative projection for the monitor to gate.
    """
    # Non-gating seam guard: a missing attribute => stay quiet, never crash.
    try:
        if getattr(session, "complete", False):
            return None
        board = list(session.board)
        own_roster = list(session.own_roster)
        roster = session.roster
        operator_slot = int(session.operator_slot)
        overall_pick = int(session.overall_pick)
        opponent_rosters = {t: list(v) for t, v in session.opponent_rosters.items()}
        engine = getattr(session, "engine", None) or PickEngine()
        rounds_total = int(getattr(session, "rounds_total", ROUNDS))
        taken = set(getattr(session, "taken", ()) or ())
        teams = int(getattr(roster, "teams", 10))
        pick_order = list(getattr(session, "pick_order", range(teams)))
        seed = int(getattr(session, "session_seed", 0))
        priors = getattr(session, "room_priors", None) or ROOM_PRIORS_2025
    except (AttributeError, TypeError):
        return None

    if not board or len(own_roster) < _MIN_ROSTER_FOR_POSTURE:
        return None
    if overall_pick > teams * rounds_total:
        return None

    board_by_id = {e.player_id: e for e in board}
    base_state = BoardState(board)
    for pid in taken:
        base_state.take(pid)

    # Per-rollout seeds, shared across postures so the room starts each continuation
    # from the identical draw state (paired variance reduction). Autodraft seats are
    # sampled once per rollout, also shared across postures.
    rival_seats = [t for t in range(teams) if t != operator_slot]
    rollout_specs: list[tuple[int, frozenset[int]]] = []
    for r in range(max(1, rollouts)):
        seed_r = (seed ^ (0x9E3779B9 * (r + 1))) & 0xFFFFFFFFFFFFFFFF
        sampler = random.Random(seed_r)
        auto = frozenset(
            t for t in rival_seats if sampler.random() < priors.autodraft_fraction
        )
        rollout_specs.append((seed_r, auto))

    def _mean_points(schedule: NeedSchedule) -> float:
        operator_engine = dataclasses.replace(
            engine, need_schedule=schedule, survival=_FrontSurvival()
        )
        total = 0.0
        for seed_r, auto in rollout_specs:
            total += _continue_and_score(
                board=board,
                board_by_id=board_by_id,
                base_state=base_state,
                own_roster=own_roster,
                opponent_rosters=opponent_rosters,
                roster=roster,
                pick_order=pick_order,
                operator_slot=operator_slot,
                rounds_total=rounds_total,
                start_overall=overall_pick,
                operator_engine=operator_engine,
                autodraft_seats=auto,
                priors=priors,
                # a fresh Random per (posture, rollout) at the shared seed
                rng=random.Random(seed_r),
            )
        return total / len(rollout_specs)

    current_schedule = engine.need_schedule
    current_label = _schedule_label(current_schedule)
    current_points = _mean_points(current_schedule)

    best_label: str | None = None
    best_points = current_points
    for name, schedule in ARCHETYPE_NEED_SCHEDULES.items():
        if schedule is current_schedule:
            continue  # not an *alternative*
        alt_points = _mean_points(schedule)
        if alt_points > best_points:
            best_points = alt_points
            best_label = name

    return PostureProjection(
        current_label=current_label,
        alternative_label=best_label,
        lean_position=_lean_position(best_label),
        edge=best_points - current_points,
        current_points=current_points,
        alternative_points=best_points,
        overall_pick=overall_pick,
    )


# --------------------------------------------------------------- message wording


def _lean_position(alternative_label: str | None) -> str | None:
    """The position an archetype leans TOWARD (for the message wording)."""
    if alternative_label in ("hero_rb", "robust_rb"):
        return "RB"        # lean into running backs
    if alternative_label == "zero_rb":
        return "WR"        # lean away from RB, toward receivers
    return None


def _word(position: str | None) -> str:
    if position is None:
        return "a different position"
    return _POSITION_WORDS.get(position, position.lower())


def _compose_message(proj: PostureProjection) -> str:
    """One complete, novice-legible sentence (Rule 6 — no jargon, never blank)."""
    edge = round(proj.edge)
    label = proj.alternative_label or ""
    if label == "zero_rb":
        return (
            f"Your roster is leaning running-back-heavy — easing off running backs "
            f"from here projects about {edge} more season points for your final "
            f"starting lineup."
        )
    if label in ("hero_rb", "robust_rb"):
        return (
            f"Your roster is thin at running back — leaning into running backs from "
            f"here projects about {edge} more season points for your final starting "
            f"lineup."
        )
    if proj.lean_position is not None:
        return (
            f"A different lean — favouring the {_word(proj.lean_position)} from here — "
            f"projects about {edge} more season points for your final starting lineup "
            f"than your current plan."
        )
    return (
        f"A different draft lean projects about {edge} more season points for your "
        f"final starting lineup than your current plan."
    )


# ----------------------------------------------------------------- the monitor


Evaluator = Callable[["PostureSession"], "PostureProjection | None"]


class PostureMonitor:
    """Posture-check + hysteresis state machine (recon §2, the mandated guard).

    Call :meth:`evaluate` at each snake turn (the caller decides cadence). It
    returns a :class:`PostureAdvice` ONLY when the best-alternative edge has
    exceeded ``margin`` for ``>= consecutive`` consecutive evaluations of the SAME
    alternative and the monitor is not in a post-dismissal cooldown — otherwise
    ``None``. Advice fires once and then stays quiet (``active``) until the
    operator acts: :meth:`dismiss` suppresses for ``cooldown`` evaluations,
    :meth:`accept` resets the machine clean.
    """

    def __init__(
        self,
        *,
        margin: float = DEFAULT_MARGIN,
        consecutive: int = DEFAULT_CONSECUTIVE,
        cooldown: int = DEFAULT_COOLDOWN,
        evaluator: Evaluator | None = None,
    ) -> None:
        if consecutive < 1:
            raise ValueError("consecutive must be >= 1")
        if cooldown < 0:
            raise ValueError("cooldown must be >= 0")
        self.margin = float(margin)
        self.consecutive = int(consecutive)
        self.cooldown = int(cooldown)
        self._evaluator: Evaluator = evaluator or project_postures
        # hysteresis state
        self._streak = 0
        self._streak_label: str | None = None
        self._cooldown_remaining = 0
        self._active = False

    # -- introspection (read-only; handy for the TUI status line + tests) ----

    @property
    def streak(self) -> int:
        """Consecutive above-margin evaluations for the current leading lean."""
        return self._streak

    @property
    def cooldown_remaining(self) -> int:
        """Evaluations of silence still owed after a dismissal."""
        return self._cooldown_remaining

    @property
    def active(self) -> bool:
        """True once advice has fired and before the operator acts on it."""
        return self._active

    # -- the state machine ---------------------------------------------------

    def evaluate(self, session: PostureSession) -> PostureAdvice | None:
        """Advance the machine one snake-turn evaluation; advise iff earned."""
        # A dismissal buys ``cooldown`` evaluations of guaranteed silence. During
        # the cooldown the streak is held at zero so a *fresh* persistent signal is
        # required afterward (no carrying a stale streak through the quiet window).
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            self._streak = 0
            self._streak_label = None
            return None

        # Already surfaced and awaiting the operator: hold quiet and — crucially —
        # do NOT re-run the (expensive) comparator. The app keeps the fired advice
        # on screen across intervening picks until the operator acts (p/x), which
        # calls accept()/dismiss() to release the monitor. Guarding here, BEFORE the
        # comparator, both removes the per-call waste (recon §ux F1: ~0.15s/call
        # spent while active) and makes "a fired tip persists until acknowledged"
        # the monitor's contract — a later below-margin read no longer silently
        # clears an unacknowledged nudge (recon §ux NEW-1).
        if self._active:
            return None

        proj = self._evaluator(session)

        # No meaningful alternative, or the edge is below the margin: the drift (if
        # any) does not clear the bar. Reset the streak (the monitor is not active
        # here — a fired nudge short-circuited above).
        if proj is None or proj.alternative_label is None or proj.edge <= self.margin:
            self._streak = 0
            self._streak_label = None
            return None

        # Above margin. A change of leading alternative restarts the streak (no
        # flip-flopping between competing leans — Rule 6).
        if proj.alternative_label != self._streak_label:
            self._streak = 1
            self._streak_label = proj.alternative_label
        else:
            self._streak += 1

        if self._streak >= self.consecutive:
            self._active = True
            return PostureAdvice(
                message=_compose_message(proj),
                current_label=proj.current_label,
                alternative_label=proj.alternative_label,
                lean_position=proj.lean_position,
                edge_points=proj.edge,
                current_points=proj.current_points,
                alternative_points=proj.alternative_points,
                overall_pick=proj.overall_pick,
                evaluations_held=self._streak,
            )
        return None

    def dismiss(self) -> None:
        """Operator waved the nudge off: suppress advice for ``cooldown`` evals."""
        self._cooldown_remaining = self.cooldown
        self._active = False
        self._streak = 0
        self._streak_label = None

    def accept(self) -> None:
        """Operator adopted the lean: reset the machine clean (no cooldown)."""
        self._active = False
        self._streak = 0
        self._streak_label = None
        self._cooldown_remaining = 0

    # ``reset`` is an alias for the acceptance reset (clean slate, no cooldown).
    reset = accept


__all__ = [
    "DEFAULT_COOLDOWN",
    "DEFAULT_CONSECUTIVE",
    "DEFAULT_MARGIN",
    "POSTURE_ROLLOUTS",
    "Evaluator",
    "PostureAdvice",
    "PostureMonitor",
    "PostureProjection",
    "PostureSession",
    "project_postures",
]
