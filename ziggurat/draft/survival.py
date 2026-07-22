"""Survival model for the 2.3 draft pick engine — Monte-Carlo rollouts + fallback.

DELETABLE package (Rule 8). Pure in-memory logic: it takes a :class:`PickContext`
(already holding a loaded board via ``ctx.state``) and a caller-seeded ``Random``,
and answers the two quantities the engine's board-state term needs, from ONE batch
of rollouts per on-clock decision (never per-candidate re-rolls):

* ``S_next(p)`` — probability candidate ``p`` is still available at the operator's
  NEXT pick (survival), for every candidate at once.
* ``E[best-available VOR at position pos]`` at that next pick — feeds ``VONA(pos)``.

Two routes, both keyed on ESPN default rank (the confirmed room driver — the room
bots draft off ``espn_overall_rank``):

* **route 1 — sim-derived (primary, shipped).** :func:`rollout_survival` clones the
  live :class:`BoardState` and rolls the calibrated room (``RankNoiseBot`` +
  sampled ``AutodraftBot``, ``ROOM_PRIORS_2025``) forward over the intervening
  opponent picks, honoring each rival's real current roster (need/legality) from
  ``ctx.opponent_rosters``. Captures position runs, the K/DST round window, and
  opponent need generatively — no analytic curve can.
* **route 2 — analytic fallback (offline unit tests / cross-check).**
  :func:`analytic_survival` is the fitted logistic ``S = sigmoid((center(R) - O)/w)``.

Live-draft honesty knobs (design Part II):
* ``kappa`` (~1.3) inflates the room's reach spread in the rollout — err toward
  securing value now under a weak (n=1) room fit.
* ``tau_wait`` (~0.8) is the wait-gate threshold (:func:`wait_ok`). The engine
  consumes it for reason PHRASING only ("no rush" appears when S_next clears it,
  via ``PickEngine.tau_wait``); the pick score has no hard gate — waiting is
  folded into the continuous ``urgency = VONA * (1 - S_next)`` term.

Analytic-fit provenance (re-fit by the build agent 2026-07-21, NOT trusted from the
recon artifact): 2,500 all-bot rooms on the real 3,218-player 2026 board
(``load_board`` as_of 2026-07-21), fitting each player's empirical survival curve to
``sigmoid((center - O)/width)`` (center = 0.5-survival crossing; width from the
0.75->0.25 span). Skill: ``center ~= 2.67 + 0.704*rank`` (n=139, R^2=0.985), width
mean 4.34. K: center 156.5, width 2.32. DST: center 148.8, width 2.27 — both
window-governed (rank-independent), so a K/DST before ~round 9 survives ~1 and then
collapses across the R14-16 run. These reproduce the recon's numbers (center ~=
2.65 + 0.704*rank, r^2 0.985) to <1%.

Rule 1: no DB accessor here — the board is already loaded on ``ctx.state``; the
rollout is pure in-memory. Rule 2: no scoring constant — VOR/points come from the
board entries. Rule 8: lives under ``ziggurat/draft/``.
"""

from __future__ import annotations

import dataclasses
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ziggurat.draft.bots import (
    KDST,
    POSITIONS,
    AutodraftBot,
    BoardEntry,
    PickContext,
    RankNoiseBot,
)

# Reuse the offline calibration statistics for the live re-fit (same package,
# Rule 8). ``_robust_spread`` gives the population std/robust-sigma of a reach
# sample; ``_pearson`` the board-adherence correlation.
from ziggurat.draft.calibration import _pearson, _robust_spread
from ziggurat.draft.priors import ROOM_PRIORS_2025, RoomPriors

# --------------------------------------------------------------------- knobs

DEFAULT_ROLLOUTS = 128          # tournament budget (design Part II); live uses 500-1000
DEFAULT_KAPPA = 1.3             # reach-spread inflation for live-draft uncertainty
DEFAULT_TAU_WAIT = 0.8          # wait-gate: only wait when S_next >= this
LIVE_RECAL_MIN_PICKS = 20       # online-refit threshold (room skill picks; addendum #1)

# Mirrors simulator._FALLBACK_BASE: players the ESPN board doesn't rank sit at
# rank >= this. They are effectively never drafted by the room, so their survival
# is ~1 and they carry no meaningful "reach".
_FALLBACK_RANK_BASE = 10_000


@dataclass(frozen=True)
class SurvivalParams:
    """Fitted logistic-survival constants for the analytic route (route 2).

    ``center(R)`` is the overall pick at which a player's survival crosses 0.5
    (roughly, when the room drafts them); ``width`` is the logistic scale. Skill
    center is linear in ESPN rank; K/DST centers are fixed (window-governed). See
    the module docstring for the re-fit provenance (2,500 sims, real 2026 board).
    """

    skill_center_intercept: float = 2.67
    skill_center_slope: float = 0.704
    skill_width: float = 4.34
    k_center: float = 156.5
    k_width: float = 2.32
    dst_center: float = 148.8
    dst_width: float = 2.27


DEFAULT_SURVIVAL_PARAMS = SurvivalParams()


# --------------------------------------------------------------- snake geometry


def _snake_team_at(overall_pick: int, teams: int) -> int:
    """Team slot (0-based) on the clock at ``overall_pick`` in a standard snake.

    Assumes ``pick_order = range(teams)`` (the simulator default). Round index is
    0-based: even rounds run forward, odd rounds reversed.
    """
    r = (overall_pick - 1) // teams
    idx = (overall_pick - 1) % teams
    return idx if r % 2 == 0 else teams - 1 - idx


def upcoming_opponent_picks(ctx: PickContext) -> list[tuple[int, int]]:
    """The (overall_pick, team_slot) opponent picks between now and the operator's
    NEXT pick, in order — a standard 10-team snake (``pick_order = range(teams)``).

    Excludes the current pick and the operator's next pick (survival is measured AT
    the next pick). Derived purely from the snake geometry at ``ctx.overall_pick``;
    the operator's identity for the stop condition is the snake team of the current
    pick, so it is self-consistent even if the board's default order is in force.
    A custom ``pick_order`` (not used by the tournament) would shift this — the
    survival estimate degrades gracefully rather than erroring.
    """
    teams = ctx.roster.teams
    current = _snake_team_at(ctx.overall_pick, teams)
    max_pick = ctx.rounds_total * teams
    out: list[tuple[int, int]] = []
    p = ctx.overall_pick + 1
    while p <= max_pick:
        t = _snake_team_at(p, teams)
        if t == current:
            break
        out.append((p, t))
        p += 1
    return out


# (next_overall_pick was removed 2026-07-21: dead code with wrong snake-turn
# semantics — it conflated "operator picks again immediately" with "no further
# pick". 2.4 should derive the next pick from the snake sequence if it needs it.)


# --------------------------------------------------------------- route 1: rollout


@dataclass(frozen=True)
class SurvivalResult:
    """Output of one on-clock survival batch (route 1 or 2)."""

    survival: Mapping[str, float]          # player_id -> S_next in [0,1]
    next_best_vor: Mapping[str, float]     # position -> E[best available VOR next pick]
    picks_until_next: int                  # intervening opponent picks in the window
    rollouts: int                          # 0 for the analytic route


def rollout_survival(
    ctx: PickContext,
    candidates: Sequence[BoardEntry],
    *,
    rng: random.Random,
    rollouts: int = DEFAULT_ROLLOUTS,
    priors: RoomPriors = ROOM_PRIORS_2025,
    kappa: float = DEFAULT_KAPPA,
    positions: Sequence[str] = POSITIONS,
    upcoming: Sequence[tuple[int, int]] | None = None,
) -> SurvivalResult:
    """Monte-Carlo survival for ALL ``candidates`` from ONE batch of ``rollouts``.

    Each rollout clones ``ctx.state`` (never mutates the shared board — Part V) and
    drafts every intervening opponent pick with the calibrated room: ``RankNoiseBot``
    on a reach spread widened by ``kappa`` for the modal seat, ``AutodraftBot`` for
    seats sampled at ``priors.autodraft_fraction`` (sampled fresh per rollout, since
    a live operator cannot see which seats are on autopilot). Each rival advances
    from its REAL current roster (``ctx.opponent_rosters[t]``, honoring saturation /
    need / legality); an empty mapping runs need-blind.

    Determinism: every draw comes from the caller-passed ``rng`` (the engine derives
    it once per pick from ``ctx.rng`` — design D2), sequentially, so a fixed seed
    reproduces ``S_next`` bit-for-bit. Autodraft seats are sampled over a SORTED seat
    list so the draw sequence is order-stable.

    Returns per-candidate ``S_next`` and, per position in ``positions``, the mean
    best-available VOR at the operator's next pick (``E_next_best_vor`` for VONA).
    """
    teams = ctx.roster.teams
    if upcoming is None:
        upcoming = upcoming_opponent_picks(ctx)
    picks_until_next = len(upcoming)
    cand_ids = [c.player_id for c in candidates]
    positions = tuple(positions)

    # Widen the room's reach spread for live-draft uncertainty (κ). RoomPriors is
    # frozen — replace() gives a fresh widened bag; κ==1 keeps the base priors.
    widened = (
        dataclasses.replace(priors, reach_sigma=priors.reach_sigma * kappa)
        if kappa != 1.0
        else priors
    )
    ranknoise = RankNoiseBot(priors=widened)
    autodraft = AutodraftBot()

    # player_id -> BoardEntry, built once per decision (not per rollout) so a bot's
    # picked id can be appended to the drafting seat's rollout roster.
    by_id = {e.player_id: e for e in ctx.state.all_entries()}

    # No intervening picks (the operator picks again immediately / last round):
    # every candidate survives with probability 1 and the "next best" is today's best.
    if picks_until_next == 0:
        clone = ctx.state.clone()
        next_best_vor = {}
        for pos in positions:
            e = clone.front_vor(pos)
            next_best_vor[pos] = float(e.vor) if e is not None else 0.0
        return SurvivalResult(
            survival={pid: 1.0 for pid in cand_ids},
            next_best_vor=next_best_vor,
            picks_until_next=0,
            rollouts=rollouts,
        )

    window_seats = sorted({t for _o, t in upcoming})
    survived_counts = {pid: 0 for pid in cand_ids}
    vor_sums = {pos: 0.0 for pos in positions}

    for _ in range(rollouts):
        clone = ctx.state.clone()
        seat_rosters: dict[int, list[BoardEntry]] = {}
        # Which window seats draft on autopilot this rollout (fresh sample; the
        # operator can't observe the true autodraft set). Sorted draw order = stable.
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

        for pid in cand_ids:
            if pid not in clone.taken:
                survived_counts[pid] += 1
        for pos in positions:
            e = clone.front_vor(pos)
            vor_sums[pos] += float(e.vor) if e is not None else 0.0

    return SurvivalResult(
        survival={pid: survived_counts[pid] / rollouts for pid in cand_ids},
        next_best_vor={pos: vor_sums[pos] / rollouts for pos in positions},
        picks_until_next=picks_until_next,
        rollouts=rollouts,
    )


def wait_ok(survival_next: float, *, tau_wait: float = DEFAULT_TAU_WAIT) -> bool:
    """The wait-gate (design Part II): safe to WAIT on a player only when its
    survival to the next pick clears ``tau_wait``. Below it, secure value now."""
    return survival_next >= tau_wait


# ------------------------------------------------------------- route 2: analytic


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def analytic_survival(
    espn_overall_rank: int,
    position: str,
    overall_next_pick: int,
    *,
    params: SurvivalParams = DEFAULT_SURVIVAL_PARAMS,
) -> float:
    """Fitted logistic survival ``S = sigmoid((center - O_next)/width)`` (route 2).

    Keyed on ESPN default rank (the room driver). Skill: ``center`` is linear in
    rank. K/DST: fixed window-governed centers (rank-independent — the room defers
    them regardless of a deep editorial rank). Unranked/fallback skill players
    (rank >= fallback base) are ~never drafted -> survival 1. Deterministic, no
    rollout; the offline unit-test route and a sanity cross-check on route 1.
    """
    pos = str(position).strip().upper()
    if pos == "K":
        center, width = params.k_center, params.k_width
    elif pos in ("DST", "D/ST", "DEF"):
        center, width = params.dst_center, params.dst_width
    else:
        if espn_overall_rank >= _FALLBACK_RANK_BASE:
            return 1.0
        center = params.skill_center_intercept + params.skill_center_slope * espn_overall_rank
        width = params.skill_width
    z = (center - overall_next_pick) / max(width, 1e-9)
    return _sigmoid(z)


# --------------------------------------------------- live opponent recalibration


@dataclass(frozen=True)
class LiveRecalibration:
    """Result of an online room re-fit from the live pick log (addendum #1).

    ``engaged`` is False below the pick threshold (cold-start: ``priors`` is the
    passed base priors, and the engine leans on ``kappa`` widening). When engaged,
    ``priors`` carries the re-estimated ``reach_sigma``. ``board_adherence_pearson``
    is reported as a diagnostic but NOT folded into ``priors`` — ``reach_sigma``
    already embeds the room's board adherence (see ``priors.RoomPriors`` rationale),
    so ``board_adherence`` stays neutral at its base value.
    """

    engaged: bool
    priors: RoomPriors
    n_room_picks: int
    reach_sigma: float | None
    reach_center: float | None
    board_adherence_pearson: float | None


def recalibrate_from_pick_log(
    pick_log: Sequence[tuple[int, int, str]],
    board: Sequence[BoardEntry],
    *,
    base_priors: RoomPriors = ROOM_PRIORS_2025,
    min_room_picks: int = LIVE_RECAL_MIN_PICKS,
    operator_slot: int | None = None,
) -> LiveRecalibration:
    """Re-estimate the room's reach spread from the picks made so far (deterministic).

    On draft day the room is humans, not the calibrated bots. Every observed pick is
    evidence: ``reach = espn_overall_rank - overall_pick`` (positive => the room took
    a player EARLIER than the board — same convention as ``calibration.py``). Its
    population std IS the live ``reach_sigma``. Below ``min_room_picks`` the fit is
    too thin, so this stays cold-start (``engaged=False``) and the caller keeps the
    base priors + ``kappa`` widening.

    Filtering mirrors ``calibration.py``: the operator's own picks are excluded (they
    are not the room), K/DST are excluded (structural deferral, not aggression), and
    board-unranked/fallback players are excluded (their reach against an overall board
    is meaningless). Pure function of ``(pick_log, board)`` — no randomness, no clock.
    """
    rank_of = {e.player_id: e.espn_overall_rank for e in board}
    pos_of = {e.player_id: e.position for e in board}

    reaches: list[float] = []
    overalls: list[float] = []
    ranks: list[float] = []
    for overall, team, pid in pick_log:
        if operator_slot is not None and team == operator_slot:
            continue
        rank = rank_of.get(pid)
        if rank is None or rank >= _FALLBACK_RANK_BASE:
            continue
        if pos_of.get(pid) in KDST:
            continue
        reaches.append(float(rank - overall))
        overalls.append(float(overall))
        ranks.append(float(rank))

    n = len(reaches)
    if n < min_room_picks:
        return LiveRecalibration(
            engaged=False,
            priors=base_priors,
            n_room_picks=n,
            reach_sigma=None,
            reach_center=None,
            board_adherence_pearson=None,
        )

    spread = _robust_spread(reaches)
    sigma = spread["std"]
    center = spread["mean"]
    pearson = _pearson(overalls, ranks)
    # A degenerate spread (e.g. every reach identical) must not report an engaged
    # refit while the rollouts silently keep the base priors (audit 2026-07-21):
    # engaged mirrors what the returned priors ACTUALLY use.
    if not sigma or sigma <= 0:
        return LiveRecalibration(
            engaged=False,
            priors=base_priors,
            n_room_picks=n,
            reach_sigma=None,
            reach_center=center,
            board_adherence_pearson=pearson,
        )
    return LiveRecalibration(
        engaged=True,
        priors=dataclasses.replace(base_priors, reach_sigma=sigma),
        n_room_picks=n,
        reach_sigma=sigma,
        reach_center=center,
        board_adherence_pearson=pearson,
    )
