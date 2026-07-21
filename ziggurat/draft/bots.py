"""Draft bots, the operator-strategy seam, and the roster-legality core (item 2.2).

DELETABLE package (Rule 8). This module is pure in-memory logic — it takes a
plain ``board`` (a tuple of :class:`BoardEntry`) and never touches the DB, so
every unit test runs offline. The DB wiring (``build_valuation`` +
``get_espn_draft_ranks``) lives at the ``simulator.load_board`` edge.

Cast of pickers (all satisfy the :class:`Picker` protocol — ``pick(ctx) -> str``,
returning the drafted player's id; this is the seam item 2.3's real pick engine
plugs into):

* :class:`AutodraftBot` — pure ESPN board order + legality, no noise, no window.
  Mirrors an ESPN seat left on full autopilot.
* :class:`RankNoiseBot` — the backbone opponent: ESPN board rank + Gaussian reach
  noise, honoring roster legality, positional need, position caps, and the K/DST
  round window. See :meth:`RankNoiseBot.pick` for the exact noise model — that is
  what 2.3 tunes against.
* :class:`FollowEspnRank` / :class:`FollowVor` — the two operator-seat baseline
  strategies: draft the best legal player by ESPN rank, or by house VOR.

Legality (the load-bearing invariant): a pick is legal only if, after taking it,
the team can still finish a 16-round roster that covers all nine starters —
QB, 2*RB, 2*WR, TE, FLEX(RB/WR/TE), DST, K — with the bench absorbing the rest.
:func:`min_to_complete` and :func:`legal_positions` encode that forward check, so
a bot is *forced* onto a K/DST/hole in its final rounds rather than drafting into
an unfillable roster.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.draft.priors import DEFAULT_KDST_EARLIEST_ROUND, ROOM_PRIORS_2025, RoomPriors

# Canonical league positions (matches valuation.CANONICAL_POSITIONS). Board
# positions are already canonical (QB/RB/WR/TE/DST/K) by the time they reach here.
POSITIONS = ("QB", "RB", "WR", "TE", "DST", "K")
KDST = frozenset({"K", "DST"})

# Sensible per-position ceilings a real room respects. DST/K are hard-capped at 1
# (you start exactly one and a backup is dead weight — no bot ever drafts a second);
# the skill caps only stop absurd hoarding and never collide with legality (you
# need at most one starter of any single position, the FLEX aside). Structural,
# not a calibration target.
DEFAULT_POSITION_CAPS: Mapping[str, int] = {
    "QB": 3, "RB": 8, "WR": 8, "TE": 3, "DST": 1, "K": 1,
}

# How far down the board (in available-by-rank slots) a RankNoiseBot will even
# consider reaching on any one pick. Bounds the reach so a deep player can never
# be drafted on a freak noise draw, independent of reach_sigma. Structural.
REACH_WINDOW = 24

# Rank-slot pull a RankNoiseBot applies toward a position that still fills an
# unmet starter/flex (positional need), and toward the position the room runs
# that round (position_run). Both are gentle so the ESPN board stays the spine.
NEED_BONUS = 8.0
RUN_NUDGE = 6.0


@dataclass(frozen=True)
class BoardEntry:
    """One draftable player as the sim sees it — plain data, no DB handle.

    ``espn_overall_rank`` is the ESPN editorial board rank (1 = best; what the
    room drafts off). ``house_points`` is the projected season house total (used
    to score the optimal starting lineup) and ``vor`` is value-over-replacement
    (what the FollowVor operator strategy drafts off).
    """

    player_id: str
    name: str | None
    position: str                 # canonical QB/RB/WR/TE/DST/K
    espn_overall_rank: int        # lower = better
    house_points: float
    vor: float


# --------------------------------------------------------------- roster legality


def position_counts(roster_entries: Iterable[BoardEntry]) -> dict[str, int]:
    """Count drafted players by canonical position."""
    counts: dict[str, int] = {}
    for e in roster_entries:
        counts[e.position] = counts.get(e.position, 0) + 1
    return counts


def _roster_positions(roster: RosterStructure) -> tuple[str, ...]:
    seen = set(roster.starters) | set(roster.flex_positions)
    return tuple(p for p in POSITIONS if p in seen)


def min_to_complete(counts: Mapping[str, int], roster: RosterStructure) -> int:
    """Minimum additional players needed to reach a LEGAL starting lineup.

    Dedicated starter deficits + one FLEX (a RB/WR/TE beyond the dedicated
    minimums) if not already covered. The bench never adds a requirement. From an
    empty roster this is 9 (QB, 2*RB, 2*WR, TE, FLEX, DST, K).
    """
    need = 0
    for pos, req in roster.starters.items():
        need += max(0, req - counts.get(pos, 0))
    surplus = sum(
        max(0, counts.get(p, 0) - roster.starters.get(p, 0)) for p in roster.flex_positions
    )
    flex_need = max(0, roster.flex_slots - surplus)
    return need + flex_need


def legal_positions(
    counts: Mapping[str, int], picks_after: int, roster: RosterStructure
) -> set[str]:
    """Positions a team may draft NOW and still finish a legal roster.

    ``picks_after`` is how many picks the team makes AFTER this one (in a snake it
    is ``rounds_total - round``). A position is legal iff taking one more of it
    leaves ``min_to_complete`` reachable within ``picks_after``.

    Derived from ONE ``min_to_complete`` call (not one per candidate position):
    adding a player either fills a still-open requirement — a ``needed`` position,
    dropping the count by exactly 1 — or nothing. So all positions are legal while
    ``base <= picks_after``; only the needed ones once ``base == picks_after + 1``;
    none beyond that (an infeasible state legal picks never reach).
    """
    base = min_to_complete(counts, roster)
    positions = _roster_positions(roster)
    if base <= picks_after:
        return set(positions)
    if base == picks_after + 1:
        needed = needed_positions(counts, roster)
        return {p for p in positions if p in needed}
    return set()


def needed_positions(counts: Mapping[str, int], roster: RosterStructure) -> set[str]:
    """Positions that still have an unmet starter or flex slot (positional need)."""
    needed: set[str] = set()
    for pos, req in roster.starters.items():
        if counts.get(pos, 0) < req:
            needed.add(pos)
    surplus = sum(
        max(0, counts.get(p, 0) - roster.starters.get(p, 0)) for p in roster.flex_positions
    )
    if surplus < roster.flex_slots:
        needed |= set(roster.flex_positions)
    return needed


def allowed_positions(
    counts: Mapping[str, int],
    picks_after: int,
    roster: RosterStructure,
    *,
    caps: Mapping[str, int] = DEFAULT_POSITION_CAPS,
    round_num: int | None = None,
    kdst_earliest_round: int | None = None,
) -> set[str]:
    """Legal ∩ under-cap positions, with the optional K/DST early-round window.

    Legality always wins: if the only legal positions are K/DST (a forced late
    completion), the window is not applied and caps that would empty the set are
    dropped, so a pick always exists.
    """
    legal = legal_positions(counts, picks_after, roster)
    capped = {p for p in legal if counts.get(p, 0) < caps.get(p, 99)}
    allowed = capped or legal
    if round_num is not None and kdst_earliest_round is not None and round_num < kdst_earliest_round:
        no_kdst = {p for p in allowed if p not in KDST}
        if no_kdst:  # only defer when a non-K/DST pick is still possible
            allowed = no_kdst
    return allowed


def _allowed_and_needed(
    counts: Mapping[str, int],
    picks_after: int,
    roster: RosterStructure,
    *,
    round_num: int,
    kdst_earliest_round: int | None,
) -> tuple[set[str], set[str]]:
    """Hot-path twin of :func:`allowed_positions` that also returns the ``needed``
    set, computing ``min_to_complete`` and ``needed_positions`` ONCE per pick
    (RankNoiseBot reuses ``needed`` for its need-bonus). Uses the default caps."""
    base = min_to_complete(counts, roster)
    needed = needed_positions(counts, roster)
    positions = _roster_positions(roster)
    if base <= picks_after:
        legal = set(positions)
    elif base == picks_after + 1:
        legal = {p for p in positions if p in needed}
    else:
        legal = set()
    capped = {p for p in legal if counts.get(p, 0) < DEFAULT_POSITION_CAPS.get(p, 99)}
    allowed = capped or legal
    if kdst_earliest_round is not None and round_num < kdst_earliest_round:
        no_kdst = {p for p in allowed if p not in KDST}
        if no_kdst:
            allowed = no_kdst
    return allowed, needed


# ------------------------------------------------------------------ board state


class BoardState:
    """Mutable draft-board bookkeeping shared across a single draft's picks.

    Holds the immutable board pre-sorted two ways (by ESPN rank asc, by VOR desc)
    with a per-position advancing head that lazily skips drafted players, so
    "best / top-W available of an allowed position" is answered without re-sorting
    on every pick (keeps 1000+ drafts in seconds).
    """

    def __init__(self, board: Sequence[BoardEntry]):
        self.taken: set[str] = set()
        self._rank: dict[str, list[BoardEntry]] = {}
        self._vor: dict[str, list[BoardEntry]] = {}
        for e in board:
            self._rank.setdefault(e.position, []).append(e)
            self._vor.setdefault(e.position, []).append(e)
        for lst in self._rank.values():
            lst.sort(key=lambda e: e.espn_overall_rank)
        for lst in self._vor.values():
            lst.sort(key=lambda e: -e.vor)
        self._rank_head: dict[str, int] = {p: 0 for p in self._rank}
        self._vor_head: dict[str, int] = {p: 0 for p in self._vor}

    def take(self, player_id: str) -> None:
        self.taken.add(player_id)

    def front_rank(self, pos: str) -> BoardEntry | None:
        entries = self._rank.get(pos)
        if not entries:
            return None
        i = self._rank_head[pos]
        while i < len(entries) and entries[i].player_id in self.taken:
            i += 1
        self._rank_head[pos] = i
        return entries[i] if i < len(entries) else None

    def front_vor(self, pos: str) -> BoardEntry | None:
        entries = self._vor.get(pos)
        if not entries:
            return None
        i = self._vor_head[pos]
        while i < len(entries) and entries[i].player_id in self.taken:
            i += 1
        self._vor_head[pos] = i
        return entries[i] if i < len(entries) else None

    def best_by_rank(self, allowed: Iterable[str]) -> BoardEntry | None:
        best: BoardEntry | None = None
        for pos in allowed:
            e = self.front_rank(pos)
            if e is not None and (best is None or e.espn_overall_rank < best.espn_overall_rank):
                best = e
        return best

    def best_by_vor(self, allowed: Iterable[str]) -> BoardEntry | None:
        best: BoardEntry | None = None
        for pos in allowed:
            e = self.front_vor(pos)
            if e is not None and (best is None or e.vor > best.vor):
                best = e
        return best

    def window_by_rank(self, allowed: Iterable[str], w: int) -> list[BoardEntry]:
        """Up to ``w`` best available entries by ESPN rank across ``allowed``."""
        gathered: list[tuple[int, BoardEntry]] = []
        for pos in allowed:
            entries = self._rank.get(pos)
            if not entries:
                continue
            i = self._rank_head[pos]
            while i < len(entries) and entries[i].player_id in self.taken:
                i += 1
            self._rank_head[pos] = i
            taken_here = 0
            j = i
            while j < len(entries) and taken_here < w:
                e = entries[j]
                if e.player_id not in self.taken:
                    gathered.append((e.espn_overall_rank, e))
                    taken_here += 1
                j += 1
        gathered.sort(key=lambda t: t[0])
        return [e for _, e in gathered[:w]]


# --------------------------------------------------------------- pick context


@dataclass(frozen=True)
class PickContext:
    """Everything a picker sees on the clock. Read-only for pickers.

    ``state`` is the shared :class:`BoardState` (players gone + fast available
    queries); ``own_roster`` is what this team has already drafted; ``round`` and
    ``overall_pick`` are 1-based; ``rng`` is the simulator's seeded stream (the
    ONLY randomness source — no wall clock, no global state).
    """

    team_slot: int
    round: int
    overall_pick: int
    rounds_total: int
    roster: RosterStructure
    own_roster: Sequence[BoardEntry]   # READ-ONLY for pickers (may be a live list)
    state: BoardState
    rng: random.Random

    @property
    def picks_after(self) -> int:
        """Picks this team still makes AFTER the current one (snake => one/round)."""
        return self.rounds_total - self.round

    @classmethod
    def from_board(
        cls,
        board: Sequence[BoardEntry],
        *,
        own_roster: Sequence[BoardEntry] = (),
        taken: Iterable[str] = (),
        team_slot: int = 0,
        round: int = 1,
        overall_pick: int = 1,
        rounds_total: int = 16,
        roster: RosterStructure = DEFAULT_ROSTER,
        rng: random.Random | None = None,
    ) -> "PickContext":
        """Build a context straight from a plain board (test/edge ergonomics)."""
        state = BoardState(board)
        for pid in taken:
            state.take(pid)
        for e in own_roster:
            state.take(e.player_id)
        return cls(
            team_slot=team_slot,
            round=round,
            overall_pick=overall_pick,
            rounds_total=rounds_total,
            roster=roster,
            own_roster=tuple(own_roster),
            state=state,
            rng=rng if rng is not None else random.Random(0),
        )


class Picker(Protocol):
    """The one seam: given a :class:`PickContext`, return the drafted player_id."""

    def pick(self, ctx: PickContext) -> str: ...


def _counts_and_allowed(
    ctx: PickContext, *, kdst_earliest_round: int | None
) -> tuple[dict[str, int], set[str], set[str]]:
    """(position counts, allowed positions, needed positions) for the clock."""
    counts = position_counts(ctx.own_roster)
    allowed, needed = _allowed_and_needed(
        counts,
        ctx.picks_after,
        ctx.roster,
        round_num=ctx.round,
        kdst_earliest_round=kdst_earliest_round,
    )
    return counts, allowed, needed


def _fallback_pick(ctx: PickContext, counts: Mapping[str, int]) -> str:
    """Last resort: best legal by rank, else best still-under-cap, else anything.

    Defensive only. Total board size is NOT a sufficient stock guarantee — a
    160-player board can still strand a team when flex/bench picks drain a thin
    position (per-position supply is what matters), so ``run_draft`` validates
    supply up front and asserts every finished roster is legal; this fallback can
    then degrade through its clauses without ever failing silently.
    """
    legal = legal_positions(counts, ctx.picks_after, ctx.roster)
    e = ctx.state.best_by_rank(legal or POSITIONS)
    if e is None:
        under_cap = {
            p for p in POSITIONS if counts.get(p, 0) < DEFAULT_POSITION_CAPS.get(p, 99)
        }
        e = ctx.state.best_by_rank(under_cap or POSITIONS)
    if e is None:
        e = ctx.state.best_by_rank(POSITIONS)
    if e is None:  # pragma: no cover - only an exhausted board hits this
        raise RuntimeError("draft board exhausted: no available player to pick")
    return e.player_id


@dataclass(frozen=True)
class AutodraftBot:
    """Pure ESPN board order + legality — an ESPN seat on full autopilot.

    No reach noise, no positional-need pull, no K/DST early-window: the ESPN board
    ranks K/DST hundreds deep, so autodraft defers them naturally and legality
    forces completion at the end.
    """

    def pick(self, ctx: PickContext) -> str:
        counts, allowed, _needed = _counts_and_allowed(ctx, kdst_earliest_round=None)
        e = ctx.state.best_by_rank(allowed)
        return e.player_id if e is not None else _fallback_pick(ctx, counts)


@dataclass(frozen=True)
class RankNoiseBot:
    """The backbone opponent: ESPN board rank + Gaussian reach noise.

    NOISE MODEL (the seam item 2.3 tunes against). On the clock, among the
    positionally-ALLOWED, roster-LEGAL, under-cap available players (see
    :func:`allowed_positions`), take the best ``REACH_WINDOW`` of them by ESPN
    rank and give each candidate ``c`` an effective rank::

        eff(c) = c.espn_overall_rank
                 + rng.gauss(0, reach_sigma / board_adherence)      # reach noise
                 - (NEED_BONUS if c.position fills an unmet starter/flex else 0)
                 - RUN_NUDGE * position_run[round].get(c.position, 0) # room run

    The bot drafts ``argmin eff(c)``. Lower ESPN rank is better; the Gaussian term
    lets a lower-ranked player leapfrog a higher one by a few sigma (bounded to
    ``REACH_WINDOW`` board slots, so a deep player can't win on a fluke draw); a
    larger ``board_adherence`` shrinks the noise (tighter to the ESPN board);
    ``NEED_BONUS`` pulls toward filling roster holes; ``position_run`` nudges
    toward the position the room clusters on that round. K/DST are deferred until
    ``kdst_earliest_round`` unless legality forces them sooner.
    """

    priors: RoomPriors = ROOM_PRIORS_2025

    def pick(self, ctx: PickContext) -> str:
        counts, allowed, needed = _counts_and_allowed(
            ctx, kdst_earliest_round=self.priors.kdst_earliest_round
        )
        candidates = ctx.state.window_by_rank(allowed, REACH_WINDOW)
        if not candidates:
            return _fallback_pick(ctx, counts)

        run = self.priors.position_run.get(ctx.round, {})
        sigma = self.priors.reach_sigma / max(self.priors.board_adherence, 1e-9)

        best: BoardEntry | None = None
        best_eff = float("inf")
        for c in candidates:
            eff = c.espn_overall_rank + ctx.rng.gauss(0.0, sigma)
            if c.position in needed:
                eff -= NEED_BONUS
            eff -= RUN_NUDGE * run.get(c.position, 0.0)
            if eff < best_eff:
                best_eff, best = eff, c
        return best.player_id  # type: ignore[union-attr]


@dataclass(frozen=True)
class FollowEspnRank:
    """Operator strategy: draft the best legal available player by ESPN rank.

    Applies the K/DST early-round window as a novice-safety guardrail (Rule 6 —
    never surface a round-1 kicker), which is otherwise inert since ESPN ranks
    K/DST deep.
    """

    kdst_earliest_round: int = DEFAULT_KDST_EARLIEST_ROUND

    def pick(self, ctx: PickContext) -> str:
        counts, allowed, _needed = _counts_and_allowed(
            ctx, kdst_earliest_round=self.kdst_earliest_round
        )
        e = ctx.state.best_by_rank(allowed)
        return e.player_id if e is not None else _fallback_pick(ctx, counts)


@dataclass(frozen=True)
class FollowVor:
    """Operator strategy: draft the best legal available player by house VOR.

    Same legality/cap/window guardrails as :class:`FollowEspnRank`; only the
    ordering signal differs (VOR desc instead of ESPN rank asc).
    """

    kdst_earliest_round: int = DEFAULT_KDST_EARLIEST_ROUND

    def pick(self, ctx: PickContext) -> str:
        counts, allowed, _needed = _counts_and_allowed(
            ctx, kdst_earliest_round=self.kdst_earliest_round
        )
        e = ctx.state.best_by_vor(allowed)
        return e.player_id if e is not None else _fallback_pick(ctx, counts)
