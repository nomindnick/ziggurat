"""Snake mock-draft simulator + strategy summaries (item 2.2).

DELETABLE package (Rule 8). Runs a full 10-team / 16-round snake draft in pure
memory, then runs it many times to profile an operator strategy: the projected
starting-lineup-points distribution (mean / p10 / p50 / p90) and average roster
shape across N mock drafts. This IS the item-2.3 draft engine's test harness — a
strategy is any :class:`~ziggurat.draft.bots.Picker`.

Determinism: all randomness flows from one ``random.Random(seed)``; each draft
gets an independent child stream, so a ``(seed, n, slot, strategy)`` run is bit-
for-bit reproducible with no wall-clock or global-random dependence.

The pure sim (``run_draft`` / ``run_many``) takes a plain ``board`` (a tuple of
:class:`~ziggurat.draft.bots.BoardEntry`) so tests run offline. ``load_board`` is
the ONLY DB seam; it wires ``build_valuation`` + ``get_espn_draft_ranks`` and
takes an explicit keyword ``as_of`` (no implicit now — Rule 1).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.draft.bots import (
    DEFAULT_POSITION_CAPS,
    AutodraftBot,
    BoardEntry,
    BoardState,
    PickContext,
    Picker,
    RankNoiseBot,
    min_to_complete,
    position_counts,
)
from ziggurat.draft.priors import ROOM_PRIORS_2025, RoomPriors

# League constant (locked spike 1.1 / mocksim-2.2-recon.md §1): 9 starters + 7
# bench = 16 draftable rounds. Teams come from RosterStructure.teams.
ROUNDS = 16


# ------------------------------------------------------------------ snake order


def snake_sequence(pick_order: Sequence[int], rounds: int) -> list[int]:
    """Team id on the clock for each overall pick, snaking each round.

    ``pick_order`` maps round-1 draft position -> team id; odd rounds go forward,
    even rounds reversed. e.g. pick_order [0,1,2], rounds 3 ->
    [0,1,2, 2,1,0, 0,1,2].
    """
    seq: list[int] = []
    for r in range(rounds):
        order = list(pick_order) if r % 2 == 0 else list(reversed(pick_order))
        seq.extend(order)
    return seq


# --------------------------------------------------------------------- results


@dataclass(frozen=True)
class DraftResult:
    """One completed draft: each team's picks (in pick order) + the pick log."""

    rosters: dict[int, tuple[BoardEntry, ...]]
    pick_log: tuple[tuple[int, int, str], ...]  # (overall_pick, team_slot, player_id)


def _validate_board_supply(
    board: Sequence[BoardEntry], *, roster: RosterStructure, rounds: int
) -> None:
    """Fail fast, with the reason, on a board that cannot feed a full draft.

    Checks total size and per-position dedicated-starter supply. Deliberately a
    necessary-not-sufficient screen (flex/bench drain can still strand a thin
    position mid-draft); the post-draft legality assertion in ``run_draft`` is
    the backstop that catches whatever this screen can't prove up front.
    """
    teams = roster.teams
    total_needed = teams * rounds
    if len(board) < total_needed:
        raise ValueError(
            f"board has {len(board)} players; a {teams}-team x {rounds}-round "
            f"draft needs at least {total_needed} — is this season's data ingested?"
        )
    counts = position_counts(board)
    for pos, req in roster.starters.items():
        have, need = counts.get(pos, 0), teams * req
        if have < need:
            raise ValueError(
                f"board is short on {pos}: {have} available, but {teams} teams "
                f"each start {req} ({need} needed league-wide)"
            )


def run_draft(
    board: Sequence[BoardEntry],
    pickers: Sequence[Picker],
    *,
    rng: random.Random,
    roster: RosterStructure = DEFAULT_ROSTER,
    rounds: int = ROUNDS,
    pick_order: Sequence[int] | None = None,
) -> DraftResult:
    """Run one snake draft. ``pickers[t]`` drafts for team id ``t``.

    Threads the seeded ``rng`` into every :class:`PickContext`; the board is
    consumed via a shared :class:`~ziggurat.draft.bots.BoardState` so no re-sort
    happens per pick.
    """
    teams = roster.teams
    if len(pickers) != teams:
        raise ValueError(f"expected {teams} pickers, got {len(pickers)}")
    order = list(pick_order) if pick_order is not None else list(range(teams))
    if sorted(order) != list(range(teams)):
        raise ValueError(f"pick_order must be a permutation of 0..{teams - 1}; got {order}")
    _validate_board_supply(board, roster=roster, rounds=rounds)

    by_id = {e.player_id: e for e in board}
    # One shared board state across all 160 picks (no per-pick re-sort).
    state = BoardState(board)

    rosters: dict[int, list[BoardEntry]] = {t: [] for t in range(teams)}
    log: list[tuple[int, int, str]] = []

    sequence = snake_sequence(order, rounds)
    for overall, team in enumerate(sequence, start=1):
        round_num = (overall - 1) // teams + 1
        ctx = PickContext(
            team_slot=team,
            round=round_num,
            overall_pick=overall,
            rounds_total=rounds,
            roster=roster,
            own_roster=rosters[team],  # live list; pickers read it, never mutate
            state=state,
            rng=rng,
        )
        pid = pickers[team].pick(ctx)
        entry = by_id[pid]
        state.take(pid)
        rosters[team].append(entry)
        log.append((overall, team, pid))

    # Rule 6: sanity checks live in code. A board too thin at some position can
    # strand a team past the point of legality; that must fail LOUDLY here, never
    # get silently scored as a broken lineup.
    for team, entries in rosters.items():
        counts = position_counts(entries)
        if min_to_complete(counts, roster) > 0:
            raise RuntimeError(
                f"draft produced an illegal roster for team {team} ({counts}): the "
                "board ran out of a required position mid-draft — per-position "
                "supply is too thin for this room"
            )
        for pos, cap in DEFAULT_POSITION_CAPS.items():
            if counts.get(pos, 0) > cap:
                raise RuntimeError(
                    f"team {team} exceeded the {pos} cap of {cap} ({counts}) — "
                    "board supply forced an out-of-cap fallback pick"
                )

    return DraftResult(
        rosters={t: tuple(v) for t, v in rosters.items()},
        pick_log=tuple(log),
    )


# ----------------------------------------------------- optimal starting lineup


def optimal_starting_points(
    roster_entries: Sequence[BoardEntry], roster: RosterStructure = DEFAULT_ROSTER
) -> float:
    """Best legal starting-lineup house-point total from a drafted roster.

    Greedy is optimal for this shape: fill each dedicated starter slot with that
    position's highest-scoring players, then the single FLEX takes the best
    remaining RB/WR/TE. Missing a required starter contributes 0 (an illegal
    roster the sim's legality rules prevent anyway).
    """
    by_pos: dict[str, list[float]] = {}
    for e in roster_entries:
        by_pos.setdefault(e.position, []).append(e.house_points)
    for lst in by_pos.values():
        lst.sort(reverse=True)

    used = {pos: 0 for pos in by_pos}
    total = 0.0
    for pos, req in roster.starters.items():
        pool = by_pos.get(pos, [])
        take = min(req, len(pool))
        total += sum(pool[:take])
        used[pos] = take

    # FLEX: best remaining flex-eligible across positions.
    for _ in range(roster.flex_slots):
        best_val = None
        best_pos = None
        for pos in roster.flex_positions:
            pool = by_pos.get(pos, [])
            idx = used.get(pos, 0)
            if idx < len(pool) and (best_val is None or pool[idx] > best_val):
                best_val, best_pos = pool[idx], pos
        if best_pos is not None:
            total += best_val
            used[best_pos] = used.get(best_pos, 0) + 1
    return total


# ------------------------------------------------------------- strategy runner


@dataclass(frozen=True)
class StrategySummary:
    """Operator-outcome profile over N mock drafts for one strategy."""

    strategy: str
    n: int
    operator_slot: int              # 1-based, human-facing
    points_mean: float
    points_p10: float
    points_p50: float
    points_p90: float
    points_min: float
    points_max: float
    position_counts_mean: dict[str, float]


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0,1]) of an ascending list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _assign_autodrafters(
    rng: random.Random,
    *,
    teams: int,
    operator_slot: int,
    fraction: float,
    autodraft_count: int | None,
) -> set[int]:
    """Which non-operator seats draft on autopilot this draft (seeded).

    ``autodraft_count`` forces exactly that many seats (deterministic given the
    seed) for tests; otherwise each non-operator seat autodrafts independently
    with probability ``fraction``.
    """
    others = [t for t in range(teams) if t != operator_slot]
    if autodraft_count is not None:
        k = max(0, min(autodraft_count, len(others)))
        return set(rng.sample(others, k))
    return {t for t in others if rng.random() < fraction}


def run_many(
    board: Sequence[BoardEntry],
    *,
    n: int,
    operator_slot: int,
    strategy: Picker,
    strategy_name: str | None = None,
    priors: RoomPriors = ROOM_PRIORS_2025,
    seed: int = 0,
    roster: RosterStructure = DEFAULT_ROSTER,
    rounds: int = ROUNDS,
    autodraft_count: int | None = None,
    pick_order: Sequence[int] | None = None,
) -> StrategySummary:
    """Run ``n`` mock drafts with the operator at ``operator_slot`` (0-based).

    Each draft: opponents are :class:`RankNoiseBot`\\s except the seeded autodraft
    seats (:class:`AutodraftBot`); the operator seat runs ``strategy``. Returns the
    operator's starting-lineup-points distribution and average roster shape.
    """
    teams = roster.teams
    if not 0 <= operator_slot < teams:
        raise ValueError(f"operator_slot must be in 0..{teams - 1}; got {operator_slot}")
    name = strategy_name or type(strategy).__name__

    master = random.Random(seed)
    points: list[float] = []
    pos_totals: dict[str, float] = {}

    for _ in range(n):
        draft_rng = random.Random(master.getrandbits(64))
        autodrafters = _assign_autodrafters(
            draft_rng,
            teams=teams,
            operator_slot=operator_slot,
            fraction=priors.autodraft_fraction,
            autodraft_count=autodraft_count,
        )
        pickers: list[Picker] = []
        for t in range(teams):
            if t == operator_slot:
                pickers.append(strategy)
            elif t in autodrafters:
                pickers.append(AutodraftBot())
            else:
                pickers.append(RankNoiseBot(priors=priors))

        result = run_draft(
            board, pickers, rng=draft_rng, roster=roster, rounds=rounds, pick_order=pick_order
        )
        team = result.rosters[operator_slot]
        points.append(optimal_starting_points(team, roster))
        for pos, c in position_counts(team).items():
            pos_totals[pos] = pos_totals.get(pos, 0.0) + c

    points.sort()
    mean = sum(points) / len(points) if points else 0.0
    return StrategySummary(
        strategy=name,
        n=n,
        operator_slot=operator_slot + 1,
        points_mean=mean,
        points_p10=_percentile(points, 0.10),
        points_p50=_percentile(points, 0.50),
        points_p90=_percentile(points, 0.90),
        points_min=points[0] if points else 0.0,
        points_max=points[-1] if points else 0.0,
        position_counts_mean={p: pos_totals[p] / n for p in sorted(pos_totals)} if n else {},
    )


# --------------------------------------------------------------------- display


def format_strategy_summary(summary: StrategySummary) -> str:
    """Render a summary in plain language a football novice can read (Rule 6)."""
    s = summary
    counts = "   ".join(f"{pos} {s.position_counts_mean.get(pos, 0.0):.1f}" for pos in
                        ("QB", "RB", "WR", "TE", "DST", "K"))
    lines = [
        f"Strategy: {s.strategy} — {s.n} mock drafts from draft slot {s.operator_slot}",
        "",
        "Your projected starting-lineup points (higher = a stronger team):",
        f"  typical (median)    : {s.points_p50:.1f}",
        f"  low end  (10th pct) : {s.points_p10:.1f}",
        f"  high end (90th pct) : {s.points_p90:.1f}",
        f"  average             : {s.points_mean:.1f}",
        f"  worst / best seen   : {s.points_min:.1f} / {s.points_max:.1f}",
        "",
        "Players drafted, average by position (16 picks):",
        f"  {counts}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ DB edge


def load_board(
    conn,
    *,
    as_of,
    season,
    source: str = "sleeper_rotowire",
    weeks=None,
) -> tuple[BoardEntry, ...]:
    """Build the sim board from the DB: house VOR joined to the ESPN board rank.

    The ONLY DB seam. ``as_of`` is REQUIRED and threaded into both
    ``build_valuation`` and ``get_espn_draft_ranks`` (Rule 1 — no implicit now).
    Skill players join the ESPN editorial ``overall_rank`` by espn_id; DST joins
    by team. A player the ESPN board doesn't rank falls back to a rank deep enough
    to sit after all ranked players (ordered by the house board), so it stays on
    the board without displacing ranked names.
    """
    from ziggurat.core.valuation import build_valuation
    from ziggurat.data.nfl import base
    from ziggurat.data.nfl.espn_ranks import get_espn_draft_ranks

    val_rows = build_valuation(conn, as_of=as_of, season=season, source=source, weeks=weeks)
    espn_rows = get_espn_draft_ranks(conn, as_of=as_of, season=season)

    rank_by_espn: dict[str, int] = {}
    rank_by_dst_team: dict[str, int] = {}
    for r in espn_rows:
        overall = r["overall_rank"]
        if overall is None:
            continue
        if str(r["position"]).upper() in ("DST", "D/ST", "DEF"):
            team = r["team"]
            if team is not None:
                rank_by_dst_team[base.TEAM_ALIASES.get(str(team).upper(), str(team).upper())] = overall
        elif r["espn_id"] is not None:
            rank_by_espn[str(r["espn_id"])] = overall

    _FALLBACK_BASE = 10_000  # unranked players sort after every ESPN-ranked one
    board: list[BoardEntry] = []
    for v in val_rows:
        if v.position == "DST":
            team = base.TEAM_ALIASES.get(str(v.team).upper(), str(v.team).upper()) if v.team else None
            rank = rank_by_dst_team.get(team)
            pid = v.espn_id or v.gsis_id or f"DST:{team}"
        else:
            rank = rank_by_espn.get(str(v.espn_id)) if v.espn_id is not None else None
            pid = v.espn_id or v.gsis_id or f"{v.position}:{v.overall_rank}"
        if rank is None:
            rank = _FALLBACK_BASE + v.overall_rank
        board.append(
            BoardEntry(
                player_id=str(pid),
                name=v.player,
                position=v.position,
                espn_overall_rank=int(rank),
                house_points=float(v.proj_points),
                vor=float(v.vor),
            )
        )
    return tuple(board)
