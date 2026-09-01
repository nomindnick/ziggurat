"""Snake mock-draft simulator + strategy summaries (item 2.2).

Import-quarantined package (Rule 8). Runs a full 10-team / 16-round snake draft in pure
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

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
            # team_slot -> rival's live roster, excluding the team on the clock
            # (item 2.3). A read-only proxy over live references — no deep copy;
            # the values grow as the draft proceeds and each is read synchronously
            # within this pick. The 2.2 pickers ignore it; the 2.3 engine's survival
            # rollout reads it to advance each rival from its real current roster.
            opponent_rosters=MappingProxyType(
                {t: rosters[t] for t in range(teams) if t != team}
            ),
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
    kicker_board=None,
    lines=None,
) -> tuple[BoardEntry, ...]:
    """Build the sim board from the DB: house VOR joined to the ESPN board rank.

    ``kicker_board`` (item 3.11) is an optional
    :class:`~ziggurat.core.kicker_board.KickerBoard`. When given, the K rows are
    re-priced and the whole board re-ranked through
    :func:`ziggurat.core.kicker_board.apply_to_valuation` BEFORE the ESPN join,
    so every downstream consumer — VOR, the candidate gather, survival, the
    reasons — sees one consistent board. It is a parameter rather than a default
    because this function is also the harness's board loader and an experiment
    must be able to load the uncorrected board on purpose;
    :func:`load_draft_board` is the draft-night entry point that turns it ON.
    Passing ``None`` is exactly the pre-3.11 board.

    ``lines`` is the optional ``weekly_lines`` hand-over described on
    :func:`~ziggurat.core.valuation.build_valuation` — the caller owns the
    as_of/season/source/view contract. :func:`load_draft_board` uses it so the
    launch reads the projections table ONCE instead of three times.

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

    val_rows = build_valuation(
        conn, as_of=as_of, season=season, source=source, weeks=weeks, lines=lines
    )
    if kicker_board is not None:
        from ziggurat.core.kicker_board import apply_to_valuation

        # Re-prices the K rows and rebuilds replacement level / VOR / ranks.
        # Raises KickerBoardMismatch when the board was built over a different
        # season or week window than these rows — never a 60% overstatement
        # wearing the correction's confident reasons.
        val_rows = apply_to_valuation(val_rows, kicker_board)
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
        team = base.TEAM_ALIASES.get(str(v.team).upper(), str(v.team).upper()) if v.team else None
        if v.position == "DST":
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
                team=team,
            )
        )

    # BOARD COMPLETENESS (dress-rehearsal finding, 2026-07-24): ESPN can draft
    # players the projections-based board has never heard of (deep rookie
    # kickers especially — 45 such players on the real 2026 pool). A pick that
    # is not on the board CANNOT be entered, which dams the sync feed and
    # manual entry alike. Union the rest of the ESPN universe in as UNPRICED
    # entries: enterable, resolvable, rank-ordered after everything real, and
    # never recommended.
    #
    # "Never recommended" is the load-bearing half, and ``vor=0.0`` did not
    # deliver it (measured 2026-08-27 on the live board): only ~90 of 3,263
    # entries carry POSITIVE vor, because replacement level sits at the last
    # starter — so from roughly round 11 on, every priced player still
    # available is NEGATIVE. A zero then outranks all of them, both in the
    # engine's "single best by VOR" candidate slot and in its pick score: 46 of
    # 60 slot-9 picks in rounds 12-16 went to players with no projection at all
    # while Chris Godwin (174 house pts) and RJ Harvey (165) sat on the board.
    #
    # An absent projection is not a measurement of replacement level. Floor
    # these strictly BELOW the worst priced entry so the paragraph above is
    # true by construction rather than by the luck of a sign.
    unpriced_vor = min((e.vor for e in board), default=0.0) - 1.0
    used_ids = {e.player_id for e in board}
    tail = 0
    for r in espn_rows:
        eid = r["espn_id"]
        if eid is None or str(eid) in used_ids:
            continue  # DST rows (team-keyed, already on board) or already present
        pos = str(r["position"] or "").upper()
        pos = "DST" if pos in ("D/ST", "DEF") else pos
        rank = r["overall_rank"]
        if rank is None:
            tail += 1
            rank = _FALLBACK_BASE + len(board) + tail
        team = r["team"]
        board.append(
            BoardEntry(
                player_id=str(eid),
                name=r["player"],
                position=pos,
                espn_overall_rank=int(rank),
                house_points=0.0,
                vor=unpriced_vor,
                team=(base.TEAM_ALIASES.get(str(team).upper(), str(team).upper())
                      if team else None),
            )
        )
        used_ids.add(str(eid))
    return tuple(board)


@dataclass(frozen=True)
class DraftInputs:
    """Everything the draft-night cockpit reads from the database, in one read.

    Item 3.11. The cockpit used to need exactly one thing from the DB (the
    board); the composed engine needs three, and they must all be read at the
    SAME ``as_of``/season/source or the decision board and the objective that
    re-ranks it disagree about who a player is. So they are built together, once,
    here at the single DB seam (Rule 1: ``as_of`` is threaded, never assumed).

    * ``board``    — the priced board, kicker-corrected when ``kicker_board``
                     is not None.
    * ``weekly``   — the per-week house points map the week-by-week re-rank
                     grades rosters with, spliced with the SAME kicker
                     correction. ``None`` under ``--legacy-engine``.
    * ``kicker_board`` — the correction itself, kept so a caller can say what
                     it did. ``None`` when off or unavailable.
    * ``notes``    — plain-language lines the front-end PRINTS at launch
                     (Rule 6). Never empty: the operator is told what engine he
                     is drafting with, including when a correction was asked
                     for and could not be served.
    * ``kicker_corrected`` — whether the K rows on ``board`` carry the item-3.10
                     correction. FALSE means every kicker is understated ~25-43
                     points and the K board is MISORDERED, which the cockpit
                     must re-state on the kicker recommendation itself (item
                     3.11 audit finding 5): a launch-time line printed three
                     hours earlier is not a reason attached to a recommendation,
                     and Rule 6 asks for the latter.
    """

    board: tuple[BoardEntry, ...]
    espn_names: Mapping[str, str]
    weekly: object | None = None
    kicker_board: object | None = None
    notes: tuple[str, ...] = ()
    kicker_corrected: bool = False


def load_draft_board(
    conn,
    *,
    as_of,
    season,
    source: str = "sleeper_rotowire",
    weeks=None,
    legacy: bool = False,
) -> DraftInputs:
    """The DRAFT-NIGHT board and objective (item 3.11) — one read, one ``as_of``.

    ``legacy=True`` reproduces the pre-3.11 cockpit exactly: the uncorrected
    board, no week-by-week objective, and a note saying so. That is what
    ``--legacy-engine`` passes, and it is the ladder's rung 0 in
    ``docs/draft-day-runbook.md``.

    WHY A FAILURE HERE IS A FALLBACK AND NOT A CRASH. Both additions are
    IMPROVEMENTS to a cockpit that already works. At 18:45 on draft night the
    cost of refusing to start is total and the cost of drafting on the engine
    that ran four rehearsals is zero, so an unavailable correction degrades to
    the legacy path — but LOUDLY, in ``notes``, which both front-ends print. A
    silent degrade would be the Rule-1-invisible failure this repo keeps
    finding; a crash would be worse than the thing it is protecting against.
    """
    from ziggurat.core import kicker_board as kbm
    from ziggurat.core.valuation import weekly_lines

    notes: list[str] = []
    kboard = None
    kicker_ok = False

    # ONE pass over the projections table, shared by all three consumers below
    # (item 3.11 audit finding 1). Each of build_kicker_board / build_valuation /
    # weekly_points_map used to make its own — 3 x 3.55 s measured on the live
    # 2026 board, so the composed cockpit printed nothing for ~11.8 s on an idle
    # box and ~23.6 s under load, on every launch AND every crash-resume. All
    # three want the SAME span (``weeks`` or valuation.DEFAULT_WEEKS) and the same
    # as_of/season/source/view, which is precisely the hand-over contract each of
    # them documents; the span half is re-checked inside each callee.
    lines = weekly_lines(conn, as_of=as_of, season=season, weeks=weeks, source=source)

    if legacy:
        notes.append(
            "ENGINE: --legacy-engine — the pre-2026-08-31 cockpit exactly. "
            "No kicker correction, no week-by-week re-rank."
        )
    else:
        try:
            kboard = kbm.build_kicker_board(
                conn, as_of=as_of, season=season, weeks=weeks,
                projection_source=source, lines=lines,
            )
        except kbm.KickerBoardUnavailable as exc:
            # Say it in one line and say it is not tonight's problem. This
            # message is read at 18:45 on a 90-second clock; an alarm the
            # operator cannot act on is how he learns to skip the banner.
            notes.append(
                "KICKER BOARD: uncorrected (every kicker understated ~25-43 pts, "
                "and the K board is misordered — item 3.10). Nothing to do about "
                "it tonight; the K pick is round 10 and its ORDER is what is "
                f"affected, not whether to make it. Reason: {exc}"
            )
        else:
            kicker_ok = True
            notes.append(
                f"KICKER BOARD: corrected — {kboard.corrected_count} of "
                f"{kboard.line_count} kickers re-priced from ESPN's own projected "
                f"stat line, re-scored through core/scoring.py. {kbm.CORRECTION_LABEL}"
            )

    board = load_board(
        conn, as_of=as_of, season=season, source=source, weeks=weeks,
        kicker_board=kboard, lines=lines,
    )
    names = espn_display_names(conn, board, as_of=as_of, season=season)
    if legacy or not board:
        return DraftInputs(
            board=board, espn_names=names, notes=tuple(notes),
            kicker_corrected=kicker_ok,
        )

    from ziggurat.draft import grader

    # THE DOCSTRING'S PROMISE, IMPLEMENTED FOR BOTH HALVES (item 3.11 audit
    # finding 2). Until this catch existed only the kicker half degraded; a
    # GradeInputError / BoardKeyCollision out of weekly_points_map propagated
    # through _resolve_draft_launch as a bare traceback that killed the launch and
    # never named --legacy-engine — at 18:45, on the one evening with no slack.
    # `Exception` is deliberately broad: at launch time the cost of refusing to
    # start is TOTAL and the cost of drafting on the engine that ran four
    # rehearsals is ~0.06 expected wins. It is not silent — the fallback is a
    # note, and both front-ends print notes.
    try:
        weekly = grader.weekly_points_map(
            conn, as_of=as_of, season=season, source=source,
            weeks=weeks, rank_weeks=weeks, board=board, lines=lines,
        )
        if kboard is not None:
            # The grading twin MUST carry the same correction as the board, or the
            # re-rank measures the disagreement instead of the roster (kicker_board
            # docstring: the silent no-op flips the sign of the evidence).
            weekly = kbm.apply_to_weekly_points(weekly, kboard)
    except Exception as exc:  # noqa: BLE001 — see the paragraph above
        notes.append(
            "ENGINE: week-by-week re-rank UNAVAILABLE — falling back to the "
            "pre-2026-08-31 engine for this session, which is the engine that "
            "drafted four rehearsals. Nothing else is affected and no action is "
            f"needed; draft normally. Reason: {type(exc).__name__}: {exc}"
        )
        return DraftInputs(
            board=board, espn_names=names, notes=tuple(notes),
            kicker_corrected=kicker_ok,
        )

    notes.append(
        "ENGINE: default (composed) — the week-by-week re-rank is on at every "
        "pick, and the pair re-rank at the first pick of each of your pairs. "
        "Re-launch with --legacy-engine to undo both (runbook §6 rung 0)."
    )
    return DraftInputs(
        board=board,
        espn_names=names,
        weekly=weekly,
        kicker_board=kboard,
        notes=tuple(notes),
        kicker_corrected=kicker_ok,
    )


def espn_display_names(conn, board, *, as_of, season) -> dict[str, str]:
    """``player_id -> the display name ESPN's own draft room uses``, for the
    queue writer (auto-entry spec §6a).

    The house board's names are nflverse vocabulary ("HOU D/ST", "Marquise
    Brown"); ESPN's DOM says "Texans D/ST" / "Hollywood Brown" — and the queue
    writer searches and verifies by exactly that text, so it must be served the
    other side's identifier (spec §5c), not ours. Same joins as
    :func:`load_board` (skill by espn_id, DST by team), same DB seam, same
    Rule-1 ``as_of`` threading. Entries ESPN's board doesn't carry are simply
    absent — the consumer treats a missing name as "fall back to ``name``".
    """
    from ziggurat.data.nfl import base
    from ziggurat.data.nfl.espn_ranks import get_espn_draft_ranks

    espn_rows = get_espn_draft_ranks(conn, as_of=as_of, season=season)
    by_espn_id: dict[str, str] = {}
    by_dst_team: dict[str, str] = {}
    for r in espn_rows:
        name = r["player"]
        if not name:
            continue
        if str(r["position"]).upper() in ("DST", "D/ST", "DEF"):
            team = r["team"]
            if team is not None:
                by_dst_team[base.TEAM_ALIASES.get(str(team).upper(), str(team).upper())] = str(name)
        elif r["espn_id"] is not None:
            by_espn_id[str(r["espn_id"])] = str(name)

    out: dict[str, str] = {}
    for e in board:
        if e.position == "DST":
            got = by_dst_team.get(e.team) if e.team else None
        else:
            got = by_espn_id.get(e.player_id)
        if got is not None:
            out[e.player_id] = got
    return out
