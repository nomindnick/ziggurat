"""Item 2.3 — the on-clock draft pick engine (Fry–Lundberg–Ohlmann one-ply).

DELETABLE package (Rule 8): lives under ``ziggurat/draft/`` and nothing outside
that package imports it. Pure in-memory logic over a plain board — the only
randomness is the survival rollout, drawn from a child of ``ctx.rng`` (Rule 1: no
implicit "now"; determinism preserved bit-for-bit per design D2).

WHAT THIS IS (design ``intel/research/pick-engine-2.3-design.md`` Part I/V):
a :class:`~ziggurat.draft.bots.Picker` that scores every legal, under-cap,
window-allowed candidate by an **additive, one-ply, survival-timed VONA score in
VOR-point units** (D1)::

    pick_score(p) = vor(p)                                    # value  (from the 2.1 board, Rule 2)
                  + b_need * need_fill(p, round)              # positional need (round-conditioned = the archetype knob)
                  + b_vona * urgency(pos)                     # board state: survival-timed scarcity
                  + b_risk * risk_sign(round) * dispersion(pos)  # floor-early / ceiling-late

with ``urgency(pos) = max(0, VONA(pos)) * (1 - S_next(best_now(pos)))`` where
``VONA(pos) = vor(best_now(pos)) - E[best available VOR at pos at your next pick]``.

NO SCORING CONSTANT enters here (Rule 2): ``vor`` comes from ``valuation.py`` →
``scoring.py``. The only numeric priors the engine introduces are strategy/room
priors — the ``b_*`` weight vector, the round-conditioned ``need_schedule``, the
``risk_sign`` schedule, and the labeled positional-variance dispersion prior (D5) —
all documented, cited to ``draft-strategy.md``, and all tunable.

THE K/DST DIVERGENCE PLAY IS NOT SPECIAL-CASED (design §I / D1). It *emerges*: a
defense with high house ``vor`` but a deep ``espn_overall_rank`` (low room demand)
carries high urgency exactly when its survival collapses at the top of the room's
D/ST run, so ``pick_score`` peaks "one pick before the run" — no bespoke rule.

SURVIVAL IS INJECTED (Part II). The primary model is the sim-derived Monte-Carlo
rollout in the sibling module ``ziggurat.draft.survival`` (imported lazily so this
file never hard-depends on it at import time). Tests pass a stub provider, so the
engine is exercised offline without that file. The contract the engine consumes
is :class:`SurvivalEstimate` (below); the integrator reconciles any drift between
this consumer contract and the sibling's producer.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from ziggurat.core.valuation import RosterStructure
from ziggurat.draft.bots import (
    POSITIONS,
    BoardEntry,
    PickContext,
    allowed_positions,
    legal_positions,
    position_counts,
)
from ziggurat.draft.priors import DEFAULT_KDST_EARLIEST_ROUND, RoomPriors

# --------------------------------------------------------------------- priors
#
# STRATEGY/ROOM PRIORS (NOT scoring — Rule 2). Everything below is a documented,
# cited prior the tournament sweep (design Part VI) re-earns on the real board.

# Candidate-set width: for each allowed position take the top-C by ESPN rank and
# the single best by VOR, then union (D1 "candidate set", bounded so scoring is
# cheap). C~5 captures both the value play and any about-to-cliff need without
# scanning the whole board.
DEFAULT_CANDIDATE_WIDTH = 5

# Positional-variance prior (D5 / draft-strategy.md H10) — the SHIPPED, explicitly
# LABELED floor/ceiling proxy, mandated because house projections are point
# estimates and ``adp_rankings`` is empty. It is a *relative* dispersion vector,
# never a fabricated per-player floor/ceiling: RB is the most volume-predictable
# (highest floor => LOWEST dispersion); WR/TE are boom-bust (HIGHEST dispersion);
# QB is mid; K/DST are OUT OF SCOPE for draft-day floor/ceiling (their week-to-week
# variance dwarfs any draft-day signal and 2.1 already flags their VOR low-confidence),
# so they carry 0. When ``adp_rankings`` is later populated this upgrades to
# per-player ``(worst - best)/4`` behind a populated-table check (Part VII).
POSITIONAL_DISPERSION_PRIOR: Mapping[str, float] = MappingProxyType(
    {"RB": 0.40, "QB": 0.60, "WR": 1.00, "TE": 1.00, "DST": 0.0, "K": 0.0}
)

# Depth (beyond-starter) need weighting. A candidate that fills an OPEN starter or
# the OPEN flex scores need_fill 1.0; legal depth beyond that decays fast toward 0
# ("0 for a position already full" — D1). Small, tunable.
_DEPTH_BASE = 0.35
_DEPTH_DECAY = 0.5

# risk_sign(round) schedule (design Part IV): floor-early / ceiling-late.
#  * rounds <= 3   -> -1.0  (penalize downside dispersion; an early bust is
#                            unrecoverable — this half is robust for 10-team H2H)
#  * rounds 4..10  -> taper -1.0 -> 0.0 (the RB "dead-zone" tilt rides here, small)
#  * rounds >= 11  -> +LATE (reward upside on lottery-ticket bench picks) —
#                     DELIBERATELY WEAK: a fixed 9-man H2H lineup rewards ceiling
#                     far less than best-ball (H11 transfer caveat).
_EARLY_RISK_LAST_ROUND = 3
_MID_RISK_ZERO_ROUND = 10
_LATE_RISK_FIRST_ROUND = 11
_LATE_CEILING_WEIGHT = 0.40


def risk_sign(round_num: int) -> float:
    """Round-appropriate risk sign in [-1, +_LATE_CEILING_WEIGHT] (design Part IV)."""
    if round_num <= _EARLY_RISK_LAST_ROUND:
        return -1.0
    if round_num >= _LATE_RISK_FIRST_ROUND:
        return _LATE_CEILING_WEIGHT
    # linear taper from -1.0 at the early boundary to 0.0 at _MID_RISK_ZERO_ROUND
    span = _MID_RISK_ZERO_ROUND - _EARLY_RISK_LAST_ROUND
    return -1.0 * (_MID_RISK_ZERO_ROUND - round_num) / span


# ------------------------------------------------------- archetype need-schedules
#
# H5/H6: Zero-/Hero-/Robust-RB are NOT separate algorithms — they are settings of
# ONE round-conditioned need-weight curve (``need_schedule[pos][round]``, a
# multiplier on need_fill; a missing (pos, round) defaults to 1.0). Lowering RB in
# the early rounds defers RB (Zero-RB); a single early spike then defer is Hero-RB;
# a flat-high early curve is Robust-RB. The archetype is a *setting of one
# parameter vector*, never separate code. ``balanced`` (all 1.0) is the shipped
# PLACEHOLDER default; the tournament sweep selects the shipped vector on our board.

NeedSchedule = Mapping[str, Mapping[int, float]]

NEED_SCHEDULE_BALANCED: NeedSchedule = MappingProxyType({})
NEED_SCHEDULE_ZERO_RB: NeedSchedule = MappingProxyType(
    {"RB": MappingProxyType({1: 0.15, 2: 0.20, 3: 0.35, 4: 0.60, 5: 0.85})}
)
NEED_SCHEDULE_HERO_RB: NeedSchedule = MappingProxyType(
    {"RB": MappingProxyType({1: 1.35, 2: 0.30, 3: 0.35, 4: 0.55, 5: 0.80})}
)
NEED_SCHEDULE_ROBUST_RB: NeedSchedule = MappingProxyType(
    {"RB": MappingProxyType({1: 1.40, 2: 1.35, 3: 1.25, 4: 1.05})}
)
ARCHETYPE_NEED_SCHEDULES: Mapping[str, NeedSchedule] = MappingProxyType(
    {
        "balanced": NEED_SCHEDULE_BALANCED,
        "zero_rb": NEED_SCHEDULE_ZERO_RB,
        "hero_rb": NEED_SCHEDULE_HERO_RB,
        "robust_rb": NEED_SCHEDULE_ROBUST_RB,
    }
)

# SHIPPED default weights (VOR-point units), SELECTED by the item-2.3 tournament
# sweep on the real 2026 board (design Part VI; results in the design "Build
# results" section, 2026-07-21). The sweep maximized the minimum-over-slots paired
# margin vs FollowEspnRank (95% CI excluding 0 in every slot), tie-broken by the
# margin vs FollowVor, over the grid b_need∈{8,15,25} × b_vona∈{0.5,1,2} × b_risk=5
# with the balanced need-schedule. Winner: b_need=25, b_vona=2, b_risk=5 (highest
# min-margin vs FollowEspnRank, +154 across the screening slots; the engine's edge
# is robust — every grid config beat BOTH baselines in all screened slots). Units:
# ``b_vona`` scales urgency (already in VOR points); ``b_need``/``b_risk`` are tilts
# around the board's native value axis.
DEFAULT_B_NEED = 25.0   # swept (design Part VI)
DEFAULT_B_VONA = 2.0    # swept — urgency already in VOR points
DEFAULT_B_RISK = 5.0    # swept — small floor-early / ceiling-late tilt


# ------------------------------------------------------------- survival contract


@dataclass(frozen=True)
class SurvivalEstimate:
    """The board-state signal the engine consumes for one on-clock decision.

    Produced by a :class:`SurvivalProvider` (primary: the sim-derived Monte-Carlo
    rollout in ``ziggurat.draft.survival``, Part II). Read-only, plain numbers.

    * ``survival``      — ``player_id -> P(that player is still available at the
                          operator's NEXT pick)`` in [0, 1], for every candidate.
    * ``next_best_vor`` — ``position -> E[best available VOR at that position at
                          the operator's next pick]`` (feeds VONA).
    """

    survival: Mapping[str, float]
    next_best_vor: Mapping[str, float]


class SurvivalProvider(Protocol):
    """Callable the engine asks, once per on-clock decision, for survival.

    Contract (design Part II — the rollout runs ONCE per decision, batching every
    candidate + next-best-per-position from one set of rollouts; it must CLONE the
    board and never mutate ``ctx.state``). ``rng`` is the per-pick child derived
    from ``ctx.rng`` (D2), so the estimate is deterministic given the seed.
    """

    def __call__(
        self,
        ctx: PickContext,
        *,
        candidates: Sequence[BoardEntry],
        positions: Sequence[str],
        rng: random.Random,
    ) -> SurvivalEstimate: ...


def _rollout_survival_provider(
    ctx: PickContext,
    *,
    candidates: Sequence[BoardEntry],
    positions: Sequence[str],
    rng: random.Random,
    rollouts: int | None = None,
    kappa: float | None = None,
    priors: RoomPriors | None = None,
) -> SurvivalEstimate:
    """Default provider: the sibling sim-derived rollout (lazy import, Part II).

    Imported in-body so ``engine.py`` never hard-depends on ``survival.py`` at
    import time (parallel build). Adapts ``survival.rollout_survival`` — whose
    frozen ``SurvivalResult`` already carries ``.survival`` and ``.next_best_vor``
    — onto this module's :class:`SurvivalEstimate` contract. Tests inject a stub
    and never reach this path; if the sibling's entry point drifts further, the
    integrator reconciles here.
    """
    try:
        from ziggurat.draft import survival as _survival  # sibling-owned (Part II)
    except ImportError as exc:  # pragma: no cover - integration seam
        raise RuntimeError(
            "PickEngine's default survival model needs ziggurat.draft.survival "
            "(the sim-derived rollout, design Part II). Pass an explicit "
            "`survival=` provider, or the integrator must wire survival.py."
        ) from exc
    extra: dict = {}
    if rollouts is not None:
        extra["rollouts"] = rollouts
    if kappa is not None:
        extra["kappa"] = kappa
    if priors is not None:
        extra["priors"] = priors
    result = _survival.rollout_survival(ctx, candidates, rng=rng, positions=positions, **extra)
    return SurvivalEstimate(survival=result.survival, next_best_vor=result.next_best_vor)


# ------------------------------------------------------------------- pick record


@dataclass(frozen=True)
class PickRec:
    """One legible recommendation — the 2.4 TUI's render contract (Rule 6).

    Carries the drafted-player entry plus every number behind the score and a
    non-empty tuple of plain-language ``reasons`` (a football novice must be able
    to read them; no jargon). ``player_id`` / ``name`` / ``position`` are exposed
    as properties over ``player`` so both the design's flattened contract and the
    whole-entry convenience are satisfied without duplication.
    """

    player: BoardEntry
    pick_score: float
    vor: float
    survival_next: float          # P(available at your NEXT pick), 0..1
    vona: float                   # value that drops off this position before your next turn
    need_note: str                # "fills an open RB starter" | "flex depth" | "bench depth"
    risk_note: str                # "steady, high-floor RB" | "upside swing for your bench"
    divergence_note: str          # "the room's board has him ~N spots later" (edge only your scoring sees)
    reasons: tuple[str, ...]      # rendered plain-language sentences (non-empty)
    alternatives: tuple[tuple[str, str], ...] = ()  # (name, one-line why-not) for the next few

    @property
    def player_id(self) -> str:
        return self.player.player_id

    @property
    def name(self) -> str | None:
        return self.player.name

    @property
    def position(self) -> str:
        return self.player.position


# --------------------------------------------------------------- need / dispersion


def _base_need(pos: str, counts: Mapping[str, int], roster: RosterStructure) -> float:
    """need_fill BEFORE the round schedule: 1.0 for an open starter/flex slot, a
    fast-decaying weight for legal depth, ~0 once a position is full (D1)."""
    have = counts.get(pos, 0)
    if have < roster.starters.get(pos, 0):
        return 1.0  # fills an open dedicated starter
    if pos in roster.flex_positions:
        surplus = sum(
            max(0, counts.get(p, 0) - roster.starters.get(p, 0)) for p in roster.flex_positions
        )
        if surplus < roster.flex_slots:
            return 1.0  # fills the open flex, and this position is flex-eligible
    beyond = max(0, have - roster.starters.get(pos, 0))
    return _DEPTH_BASE * (_DEPTH_DECAY ** beyond)


def _need_fill(
    pos: str, round_num: int, counts: Mapping[str, int],
    roster: RosterStructure, need_schedule: NeedSchedule,
) -> float:
    """Round-conditioned need value = base openness * the archetype schedule mult."""
    mult = need_schedule.get(pos, {}).get(round_num, 1.0)
    return _base_need(pos, counts, roster) * mult


def _dispersion(pos: str) -> float:
    """Labeled positional-variance prior (D5) — a PROXY, never a per-player number."""
    return POSITIONAL_DISPERSION_PRIOR.get(pos, 0.0)


# ------------------------------------------------------------------- reason text


def _need_note(pos: str, counts: Mapping[str, int], roster: RosterStructure) -> str:
    have = counts.get(pos, 0)
    if have < roster.starters.get(pos, 0):
        # RB2 / WR2 legibility: which starter number this fills.
        which = have + 1
        req = roster.starters.get(pos, 0)
        label = f"{pos}{which}" if req > 1 else pos
        return f"fills your open {label} starter slot"
    if pos in roster.flex_positions:
        surplus = sum(
            max(0, counts.get(p, 0) - roster.starters.get(p, 0)) for p in roster.flex_positions
        )
        if surplus < roster.flex_slots:
            return f"fills your open FLEX slot (a {pos} counts)"
    return f"adds {pos} bench depth (your starters at this spot are set)"


def _risk_note(pos: str, round_num: int) -> str:
    disp = _dispersion(pos)
    sign = risk_sign(round_num)
    if pos in ("K", "DST"):
        return "low-risk: you start exactly one, and this is the best your scoring sees"
    if disp <= 0.5:  # steady, high-floor position (RB)
        return f"steady, high-floor {pos} — a safe early-round anchor"
    # boom-bust position (WR/TE)
    if sign < 0:
        return f"some boom-or-bust in a {pos} here — early on you're protecting your floor"
    if sign > 0:
        return f"an upside swing at {pos} — good use of a late bench pick"
    return f"a {pos} with a wider range of outcomes"


# Unranked players carry espn_overall_rank = 10_000 + house rank (the sentinel
# simulator.load_board and survival._FALLBACK_RANK_BASE share) — never phrase a
# board-position claim from it (audit 2026-07-21: "~9955 spots later" absurdity).
_UNRANKED_RANK_BASE = 10_000


def _divergence_note(
    entry: BoardEntry, overall_pick: int, *, draft_size: int, take_now: bool = True
) -> tuple[str, int]:
    """(sentence, N) where N = how many spots LATER the room's board lists him.

    N = espn_overall_rank - overall_pick. Large positive => your scoring wants him
    here but the room's screen ranks him much later — the edge only you can see
    (the K/DST house-bracket divergence surfaces here). Returns "" when the room
    roughly agrees with taking him now, and for UNRANKED players (fallback
    sentinel — no board position exists to compare against). Ranks beyond the
    draft itself get absolute phrasing, never a spots-later count. ``take_now``
    False (survival says he'd keep) drops the timing framing so the sentence
    never contradicts a "no rush" survival line."""
    rank = int(entry.espn_overall_rank)
    if rank >= _UNRANKED_RANK_BASE:
        return ("", 0)
    n = rank - int(overall_pick)
    if n < 15:
        return ("", n)
    if rank > draft_size:
        return (
            f"the room's board buries him around slot {rank}, past the end of "
            f"this draft — an edge only your scoring sees",
            n,
        )
    if take_now:
        sentence = (
            f"your league's scoring values this pick now, but the room's board "
            f"lists him about {n} spots later — an edge only your scoring sees"
        )
    else:
        sentence = (
            f"your league's scoring rates him well above the room's board, which "
            f"lists him about {n} spots later — an edge only your scoring sees"
        )
    return (sentence, n)


def _survival_reason(
    survival_next: float,
    vona: float,
    pos: str,
    *,
    tau_wait: float = 0.8,
    final_pick: bool = False,
) -> str | None:
    """Plain-language board-state sentence (no jargon: never 'VONA'/'sigma').

    ``final_pick`` replaces next-pick phrasing (there is no next pick — audit
    2026-07-21); the "no rush" wording is gated on the ``tau_wait`` wait-gate."""
    if final_pick:
        return "this is your last pick of the draft — take the best value on the board"
    pct = round(survival_next * 100)
    if survival_next <= 0.35:
        tail = ""
        if vona > 0:
            tail = f", and about {vona:.0f} points of value drop off this spot before you pick again"
        return (
            f"he very likely will NOT last until your next pick (about {pct}% "
            f"chance he's still there){tail}"
        )
    if survival_next >= tau_wait:
        return f"he is likely ({pct}%) to still be there at your next pick — no rush"
    return f"there is roughly a {pct}% chance he lasts to your next pick"


# ----------------------------------------------------------------- the engine


@dataclass(frozen=True)
class PickEngine:
    """The item-2.3 on-clock pick engine (a :class:`Picker`).

    ``pick(ctx)`` returns ``recommend(ctx)[0].player_id``; ``recommend`` is the
    richer surface the 2.4 TUI renders. Weights and the ``need_schedule`` are
    constructor params (PLACEHOLDER defaults, swept by the integrator). The
    survival model is injected (default: the sibling sim-derived rollout).
    """

    b_need: float = DEFAULT_B_NEED
    b_vona: float = DEFAULT_B_VONA
    b_risk: float = DEFAULT_B_RISK
    need_schedule: NeedSchedule = NEED_SCHEDULE_BALANCED
    candidate_width: int = DEFAULT_CANDIDATE_WIDTH
    kdst_earliest_round: int = DEFAULT_KDST_EARLIEST_ROUND
    survival: SurvivalProvider | None = None  # None -> sim-derived rollout (lazy)
    # Rollout knobs, threaded into the default provider (audit 2026-07-21: these
    # existed only as survival.py module defaults, so the documented "R=512 live"
    # budget and live-recalibrated priors were unreachable through the engine).
    # None -> the survival module's defaults (R=128, kappa=1.3, ROOM_PRIORS_2025).
    # A live/TUI caller passes rollouts=512 and room_priors=<recalibration>.priors.
    rollouts: int | None = None
    kappa: float | None = None
    room_priors: RoomPriors | None = None
    # Wait-gate threshold for reason PHRASING (mirrors survival.DEFAULT_TAU_WAIT):
    # "no rush" appears only when S_next clears it. The score itself folds waiting
    # into the continuous (1 - S_next) urgency term — there is no hard score gate.
    tau_wait: float = 0.8

    # -- Picker seam -------------------------------------------------------

    def pick(self, ctx: PickContext) -> str:
        """The drafted player_id. Consumes ``ctx.rng`` identically to ``recommend``
        (both derive the rollout child ONCE — D2), so a ``(state, rosters, seed)``
        run is bit-for-bit reproducible."""
        recs = self.recommend(ctx, top=1)
        return recs[0].player_id

    def recommend(self, ctx: PickContext, *, top: int = 5) -> tuple[PickRec, ...]:
        """Top-``top`` recommendations, best first, each with legible reasons.

        Never surfaces an illegal / over-cap / out-of-window player (the candidate
        gather runs through ``allowed_positions``), and every ``PickRec.reasons``
        is non-empty (Rule 6)."""
        # D2: derive the rollout child stream ONCE, so survival draws never perturb
        # the main draft stream and pick()/recommend() consume ctx.rng identically.
        rollout_rng = random.Random(ctx.rng.getrandbits(64))

        counts = position_counts(ctx.own_roster)
        allowed = allowed_positions(
            counts,
            ctx.picks_after,
            ctx.roster,
            round_num=ctx.round,
            kdst_earliest_round=self.kdst_earliest_round,
        )
        if not allowed:
            allowed = legal_positions(counts, ctx.picks_after, ctx.roster) or set(POSITIONS)

        # Candidate set (D1): top-C by ESPN rank ∪ best-by-VOR at each allowed
        # position. BoardState exposes per-position VOR fronts (not a global VOR
        # window), so "best-VOR-per-allowed-position" stands in for top-C-by-VOR —
        # it captures the same value+need candidates without mutating shared heads.
        best_now: dict[str, BoardEntry] = {}
        for pos in allowed:
            e = ctx.state.front_vor(pos)
            if e is not None:
                best_now[pos] = e
        rank_candidates = ctx.state.window_by_rank(allowed, self.candidate_width)

        by_id: dict[str, BoardEntry] = {}
        for e in list(best_now.values()) + rank_candidates:
            by_id.setdefault(e.player_id, e)
        candidates = list(by_id.values())

        if not candidates:  # degenerate: board thin at every allowed position
            e = ctx.state.best_by_rank(allowed) or ctx.state.best_by_rank(POSITIONS)
            if e is None:  # pragma: no cover - only an exhausted board
                raise RuntimeError("draft board exhausted: no available player to pick")
            candidates = [e]
            best_now.setdefault(e.position, e)

        positions = sorted(best_now)
        est = self._ask_survival(ctx, candidates, positions, rollout_rng)

        # Per-position urgency = board state. VONA(pos) = value of the current best
        # at pos minus its expected replacement next pick; urgency high only when a
        # position has BOTH a cliff (large VONA) AND low survival of its top man.
        urgency: dict[str, float] = {}
        vona_by_pos: dict[str, float] = {}
        for pos, bn in best_now.items():
            nb = est.next_best_vor.get(pos, bn.vor)
            vona = max(0.0, bn.vor - nb)
            s_top = est.survival.get(bn.player_id, 1.0)
            vona_by_pos[pos] = vona
            urgency[pos] = vona * (1.0 - s_top)

        scored: list[tuple[float, BoardEntry, float, float]] = []
        for c in candidates:
            pos = c.position
            need = self.b_need * _need_fill(pos, ctx.round, counts, ctx.roster, self.need_schedule)
            urg = self.b_vona * urgency.get(pos, 0.0)
            rk = self.b_risk * risk_sign(ctx.round) * _dispersion(pos)
            score = c.vor + need + urg + rk
            scored.append((score, c, urgency.get(pos, 0.0), vona_by_pos.get(pos, 0.0)))

        # Tie-break total order (D2): higher pick_score, then higher vor, then lower
        # espn_overall_rank, then player_id lexicographic. No wall-clock/dict-order.
        scored.sort(key=lambda t: (-t[0], -t[1].vor, t[1].espn_overall_rank, t[1].player_id))

        n = max(1, top)
        chosen = scored[:n]
        recs: list[PickRec] = []
        for i, (score, c, _urg, vona) in enumerate(chosen):
            s_next = est.survival.get(c.player_id, 1.0)
            alts = tuple(
                (alt.name or alt.player_id, self._why_not(alt, est))
                for _s, alt, _u, _v in scored[i + 1 : i + 4]
            )
            recs.append(
                self._build_rec(ctx, c, score, s_next, vona, counts, alts)
            )
        return tuple(recs)

    # -- internals ---------------------------------------------------------

    def _ask_survival(
        self,
        ctx: PickContext,
        candidates: Sequence[BoardEntry],
        positions: Sequence[str],
        rng: random.Random,
    ) -> SurvivalEstimate:
        if self.survival is not None:
            return self.survival(ctx, candidates=candidates, positions=positions, rng=rng)
        return _rollout_survival_provider(
            ctx,
            candidates=candidates,
            positions=positions,
            rng=rng,
            rollouts=self.rollouts,
            kappa=self.kappa,
            priors=self.room_priors,
        )

    def _build_rec(
        self,
        ctx: PickContext,
        entry: BoardEntry,
        score: float,
        survival_next: float,
        vona: float,
        counts: Mapping[str, int],
        alternatives: tuple[tuple[str, str], ...],
    ) -> PickRec:
        pos = entry.position
        need_note = _need_note(pos, counts, ctx.roster)
        risk_note = _risk_note(pos, ctx.round)
        final_pick = ctx.picks_after == 0
        # Reason coherence (audit 2026-07-21): when survival clears the wait-gate
        # the divergence line drops its "now" timing framing, so a value-edge note
        # and a "no rush" note can coexist without contradicting each other.
        would_keep = survival_next >= self.tau_wait
        div_sentence, _n = _divergence_note(
            entry,
            ctx.overall_pick,
            draft_size=ctx.roster.teams * ctx.rounds_total,
            take_now=not would_keep,
        )

        reasons: list[str] = []
        if div_sentence:
            reasons.append(div_sentence)
        surv = _survival_reason(
            survival_next, vona, pos, tau_wait=self.tau_wait, final_pick=final_pick
        )
        if surv:
            reasons.append(surv)
        reasons.append(need_note)
        reasons.append(risk_note)
        if not reasons:  # defensive — reasons must never be empty (Rule 6)
            reasons.append(f"best available value at {pos}")

        return PickRec(
            player=entry,
            pick_score=score,
            vor=entry.vor,
            survival_next=survival_next,
            vona=vona,
            need_note=need_note,
            risk_note=risk_note,
            divergence_note=div_sentence,
            reasons=tuple(reasons),
            alternatives=alternatives,
        )

    @staticmethod
    def _why_not(alt: BoardEntry, est: SurvivalEstimate) -> str:
        s = est.survival.get(alt.player_id, 1.0)
        if s >= 0.75:
            return f"you can likely wait — about {round(s * 100)}% he's still there next pick"
        return "close in value, but a smaller edge here"
