"""Waiver / free-agent claim planning (item 3.4).

WHAT THIS ANSWERS. On a waiver day the operator faces one question — "given my
roster and the free-agent pool, what should I claim, what should I drop, and is
my roster even legal enough for ESPN to process any of it?" This module answers
it deterministically, with reasons a football novice can check (Rule 6).

3.4 IS PURE COMPOSITION with exactly ONE new piece of logic: the roster-legality
precheck (``check_legality``). It prices NOTHING itself:

* the DROP board and the (add, drop) swap matrix both come from ONE
  ``marginal.build_board`` scan (item 3.2) — ``board.ranked`` (ascending, lowest
  is most droppable) and ``board.swaps`` (positive-gain moves already scoped to
  the free-agent pool, already carrying ``add_status`` FREEAGENT/WAIVERS and
  ``add_startable_this_week``);
* add-opportunity CONTEXT comes from ``candidates.build_candidates`` (item 3.3)
  joined on ``espn_id`` — usage / injury / QB1 signals, QB/RB/WR/TE only;
* the roster, the free-agent pool and the team context (``waiver_rank``,
  ``is_transaction_locked``) come from ``league.state``.

THE ONE PIECE 3.4 OWNS — legality. ``active_players`` / ``build_board`` strip
EVERY ``lineup_slot=='IR'`` row unconditionally, which is correct for pricing a
legal roster but wrong for the precheck: an IR occupant whose ``injury_status``
is no longer IR-eligible (Tuesday's league-wide reset flips OUT -> QUESTIONABLE)
is forced by ESPN back onto the active roster, pushing it from 16 to 17 and
BLOCKING every transaction until a drop restores 16. So ``check_legality``
recounts IR itself from the RAW rows, independent of ``build_board`` (which
RAISES ``WeekResolutionError`` on any state with ``scoring_period==0`` and no
resolvable schedule — i.e. the live DB and every synthetic test state).

IR ELIGIBILITY IS A LABELLED HYPOTHESIS. ESPN's authoritative ``eligibleSlots``
(slot 21 = IR) is NOT ingested (``state.map_player_entry`` stores
``injury_status`` only), so eligibility is inferred from the injury designation
and disclosed as UNVERIFIED ("confirm in the ESPN app post-draft"). See
``IR_ELIGIBLE_STATUSES`` / ``IR_ELIGIBLE_LABEL``.

3.4 vs 3.6. 3.4 returns a deterministic, ``as_of``-gated PLAN OBJECT. 3.6 owns
scheduling, the briefing render, and event-triggered alerts, and CALLS 3.4.

Standing rules. Rule 1 — ``build_waiver_plan`` / ``check_legality`` are
keyword-only ``as_of`` (``check_legality`` is pure and needs none); ``view`` is
threaded into every accessor. Rule 2 — no scoring constant here; every point
comes from ``build_board``. Rule 3 — the CLI parses/calls/prints. Rule 6 — every
claim, drop, and the legality verdict ships plain reasons. Rule 8 — permanent
module, never imports from ``ziggurat/draft/``.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ziggurat.core.candidates import NoCompletedWeek, build_candidates
from ziggurat.core.marginal import (
    ACQ_FREE_AGENT,
    ACQ_UNKNOWN,
    ACQ_WAIVER,
    DEFAULT_POOL_LIMIT,
    STREAMED_POSITIONS,
    MarginalRow,
    SwapRow,
    WeekResolutionError,
    build_board,
    classify_acquisition,
)
from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import base, refresh
from ziggurat.league import state as league_state

# --------------------------------------------------------------------- constants

# ESPN's authoritative IR-slot eligibility signal is ``eligibleSlots`` (slot 21),
# which league_player_state does NOT ingest (state.map_player_entry stores
# injury_status only). So eligibility is a PROXY on the injury designation — a
# LABELLED HYPOTHESIS, disclosed as unverified on every plan (Rule 6).
#
# The set is deliberately {OUT, INJURY_RESERVE} and NOTHING else:
#   * NOT state.HARD_OUT_STATUSES (an availability boundary, a different concept);
#   * NOT marginal's hard_out set (it includes SUSPENSION/NOT_ACTIVE — not IR
#     designations);
#   * DOUBTFUL / PUP / NFI are the UNCONFIRMED EDGE — treated as INELIGIBLE here,
#     never silently included (open TODO: confirm post-draft).
# The Tuesday-reset crux flips an IR occupant OUT -> QUESTIONABLE; QUESTIONABLE is
# not in this set, so he becomes IR-ineligible and the roster goes oversized.
IR_ELIGIBLE_STATUSES = frozenset({"OUT", "INJURY_RESERVE"})
IR_ELIGIBLE_LABEL = (
    "hypothesis: a player is treated as IR-slot eligible only when ESPN lists him "
    "OUT or INJURY_RESERVE. ESPN's authoritative per-player eligibility "
    "(eligibleSlots) is NOT ingested — this is inferred from his injury "
    "designation and is UNVERIFIED; confirm in the ESPN app post-draft. "
    "DOUBTFUL/PUP/NFI are an unconfirmed edge and are treated as INELIGIBLE (item "
    "3.4 open TODO)."
)

# Acquisition kinds — the claims-vs-FCFS distinction, keyed ONLY on roster_status
# THROUGH the ONE shared classifier in marginal.py (item 3.4 audit F8), so the
# drop-board reason and the claim planner can never disagree about a player.
KIND_WAIVER = ACQ_WAIVER            # roster_status 'WAIVERS': a queued, priority-ordered claim
KIND_FREE_AGENT = ACQ_FREE_AGENT    # roster_status 'FREEAGENT': first-come-first-served
KIND_UNKNOWN = ACQ_UNKNOWN          # anything else (incl. a leaked 'ONTEAM'): verify, never silent FCFS

# The whole IR-legality FIX MODEL — the block condition, the sub-16-not-blocked
# call, and the move-vs-drop preference — rests on ASSUMPTIONS about ESPN's exact
# IR mechanics that are not yet confirmed against a live post-draft app (item 3.4
# audit F1). Shipped as a LABELLED HYPOTHESIS, same discipline as IR_ELIGIBLE_LABEL.
IR_FIX_MODEL_LABEL = (
    "hypothesis: this IR-legality fix model assumes ESPN (a) forces an IR-ineligible "
    "player onto your active roster, (b) accepts an IR-eligible bench body moved into "
    "a freed IR slot, and (c) only blocks transactions when your ACTIVE roster is "
    "oversized or your IR slot is over capacity. UNVERIFIED — confirm ESPN's exact "
    "IR-legality behavior in the app post-draft."
)

# The staleness banner shouts past this many days between the data's pull date and
# the decision date (same constant marginal.py / candidates.py use).
STALE_BANNER_DAYS = 7


def _kind_of(add_status: str | None) -> str:
    """Claim vs FCFS grab vs unknown — through the ONE shared classifier (F8)."""
    return classify_acquisition(add_status)


def _slot(row: Mapping) -> str:
    return str(row.get("lineup_slot") or "").strip().upper()


def _ir_eligible(row: Mapping) -> bool:
    return str(row.get("injury_status") or "").strip().upper() in IR_ELIGIBLE_STATUSES


def _ir_status(row: Mapping) -> str:
    """Three-way IR-slot classification (item 3.4 audit F7).

    ELIGIBLE  — ESPN lists him OUT / INJURY_RESERVE, a legitimate IR occupant.
    UNKNOWN   — his injury_status is blank/None: we CANNOT say he is ineligible, so
                he does NOT re-count against the active cap; we surface a verify note.
    INELIGIBLE — any other explicit status (QUESTIONABLE, ACTIVE, ...): ESPN forces
                him onto the active roster, so he DOES re-count.
    """
    tok = str(row.get("injury_status") or "").strip().upper()
    if not tok:
        return "UNKNOWN"
    if tok in IR_ELIGIBLE_STATUSES:
        return "ELIGIBLE"
    return "INELIGIBLE"


# ------------------------------------------------------------------- output rows


@dataclass(frozen=True)
class IRIneligible:
    """An IR-slot occupant whose injury designation is not IR-eligible — the
    reference that names the CAUSE of an illegal roster (the Tuesday-reset crux)."""

    player: str
    position: str | None
    espn_id: str | None
    injury_status: str | None


@dataclass(frozen=True)
class LegalityVerdict:
    """Whether ESPN will process ANY transaction for this roster, and why not.

    ``active_count`` recounts IR itself: non-IR players PLUS IR occupants who are
    no longer IR-eligible (ESPN forces them back onto the active roster). Illegal
    when that exceeds ``active_slots``, or more than ``ir_slots`` sit in IR, or any
    IR occupant is IR-ineligible.
    """

    legal: bool
    active_count: int
    active_slots: int
    ir_count: int
    ir_slots: int
    ir_ineligible: tuple[IRIneligible, ...]
    ir_advisories: tuple[str, ...]   # required, NON-blocking roster moves (F1)
    ir_unverified: tuple[str, ...]   # blank-status IR occupants we could not verify (F7)
    violations: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DropRec:
    """One droppable roster player, priced by ``build_board`` (item 3.2)."""

    player: str
    position: str
    team: str | None
    espn_id: str | None
    marginal_points: float          # from MarginalRow.marginal_points; may be < 0
    horizon_weeks: int              # 1 for a streamed slot, else the window
    unpriceable: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ClaimRec:
    """One recommended add, paired with the DISTINCT drop it costs.

    ``kind`` is the claims-vs-FCFS split (KIND_WAIVER queued vs KIND_FREE_AGENT
    grab-fast). ``gain`` / ``horizon`` come straight from the swap matrix.
    """

    add: str
    add_position: str
    add_espn_id: str | None
    kind: str                       # KIND_WAIVER | KIND_FREE_AGENT
    gain: float
    drop: str | None
    drop_position: str | None
    startable_this_week: bool
    horizon: int                    # 1 => streamed (this-week only), else season-long
    drop_unpriceable: bool
    waiver_rank: int | None         # own-team priority, CONTEXT only
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class WaiverPlan:
    """The deterministic, as_of-gated waiver plan (item 3.4's deliverable).

    ``claims`` is EMPTY whenever ``blocked`` — no transaction can process on an
    illegal roster, so the plan refuses to plan them until legality is restored
    (the done-when). 3.6 renders/schedules this; 3.4 does not.
    """

    legality: LegalityVerdict
    forced_drop: DropRec | None     # the DROP fix when blocked; None when legal or move-only
    ir_move_fix: tuple[str, ...]    # the PREFERRED zero-drop IR-move fix, blocked path (F1)
    claims: tuple[ClaimRec, ...]    # queued waiver claims (gain-ordered)
    fcfs_grabs: tuple[ClaimRec, ...]  # first-come free agents
    streaming: tuple[ClaimRec, ...]   # this-week-only D/ST & K swaps — 3.5's lane (F4)
    drop_board: tuple[DropRec, ...]   # ascending, lowest is most droppable
    waiver_priority: int | None     # team waiver_rank (1 = next claim wins)
    team_count: int | None          # league size, from data — the 'of N' denominator (F13)
    transaction_locked: bool        # ESPN's team-level lock — CONTEXT only
    freshness: tuple[str, ...]
    notes: tuple[str, ...]
    as_of: str
    season: int
    team_id: int | None
    weeks: tuple[int, ...]

    @property
    def blocked(self) -> bool:
        """The done-when predicate: an illegal roster blocks all claims."""
        return not self.legality.legal


# --------------------------------------------------------- the legality precheck


def check_legality(
    roster_rows: Sequence[Mapping],
    *,
    structure: RosterStructure = DEFAULT_ROSTER,
) -> LegalityVerdict:
    """Is this roster legal enough for ESPN to process a transaction? (item 3.4).

    PURE — no ``as_of``, no ``weeks``, no DB. It runs on the roster rows ALONE so
    the refuse-and-propose-fix path never depends on ``build_board`` (which raises
    when the week window cannot be resolved).

    It RECOUNTS IR itself rather than trusting ``active_players`` (which strips
    every IR row unconditionally):

        active_count = |non-IR rows| + |IR occupants who are INELIGIBLE|

    because an IR-ineligible occupant is forced by ESPN back onto the active
    roster. The roster is BLOCKED iff ``active_count > active_slots`` OR
    ``ir_count > ir_slots`` (item 3.4 audit F5). The ineligible occupant is
    ALREADY folded into ``active_count``, so he is NOT ALSO an independent
    illegality source — that made the old fix non-restorative (dropping bodies
    could never clear it). An ineligible occupant on a NON-oversized roster is
    LEGAL: ESPN simply benches him. He is tracked for the cause/reason text and
    surfaced as a required (non-blocking) roster move.
    """
    ir_rows = [r for r in roster_rows if _slot(r) == "IR"]
    non_ir = [r for r in roster_rows if _slot(r) != "IR"]
    ineligible = tuple(
        IRIneligible(
            player=str(r.get("player") or r.get("espn_player_id") or "?"),
            position=r.get("position"),
            espn_id=(str(r["espn_player_id"]) if r.get("espn_player_id") is not None else None),
            injury_status=r.get("injury_status"),
        )
        for r in ir_rows
        if _ir_status(r) == "INELIGIBLE"
    )
    unknown_rows = [r for r in ir_rows if _ir_status(r) == "UNKNOWN"]
    active_count = len(non_ir) + len(ineligible)
    ir_count = len(ir_rows)

    # BLOCK conditions ONLY — an ineligible occupant is not an independent one (F5).
    violations: list[str] = []
    if ir_count > structure.ir_slots:
        violations.append(
            f"{ir_count} players are in the IR slot (your league allows "
            f"{structure.ir_slots})"
        )
    if active_count > structure.active_slots:
        cause = ""
        if ineligible:
            names = "; ".join(
                f"{o.player} (ESPN lists him "
                f"{o.injury_status or 'with no injury designation'}, not IR-eligible, "
                f"so he counts on your active roster)"
                for o in ineligible
            )
            cause = f" — this count includes {names}"
        violations.append(
            f"your active roster is {active_count} of {structure.active_slots} — you "
            f"must free {active_count - structure.active_slots} active slot(s) (a drop, "
            f"or an IR move) before ESPN will process any waiver claim or free-agent "
            f"add{cause}"
        )

    legal = not violations

    # Required, NON-blocking roster moves: an ineligible occupant must leave the IR
    # slot even when the roster is legal (ESPN will bench him) (F1).
    ir_advisories = tuple(
        f"REQUIRED ROSTER MOVE: move {o.player} out of your IR slot to the bench — "
        f"ESPN lists him {o.injury_status or 'with no injury designation'} and will "
        f"not let an IR-ineligible player stay on IR (he counts against your "
        f"{structure.active_slots} active slots)"
        for o in ineligible
    )
    # Blank-status IR occupants: UNKNOWN, not a proof of illegality (F7).
    ir_unverified = tuple(
        f"could not verify IR eligibility for "
        f"{str(r.get('player') or r.get('espn_player_id') or '?')} — his ESPN injury "
        f"status is blank, so we did NOT count him against your active roster; confirm "
        f"in the ESPN app that ESPN accepts him on IR"
        for r in unknown_rows
    )

    reasons: list[str] = []
    if legal:
        reasons.append(
            f"roster legal: {active_count} of {structure.active_slots} active slots "
            f"used, {ir_count} of {structure.ir_slots} IR slot used — ESPN will "
            f"process claims and adds."
        )
    else:
        reasons.append(
            "ROSTER ILLEGAL — ESPN blocks EVERY waiver claim and free-agent add "
            "until it is fixed."
        )
        reasons.extend(violations)
    reasons.extend(ir_advisories)
    reasons.extend(ir_unverified)
    if ir_count:
        # Surface the eligibility hypothesis whenever IR is in play (Rule 6).
        reasons.append(IR_ELIGIBLE_LABEL)

    return LegalityVerdict(
        legal=legal,
        active_count=active_count,
        active_slots=structure.active_slots,
        ir_count=ir_count,
        ir_slots=structure.ir_slots,
        ir_ineligible=ineligible,
        ir_advisories=ir_advisories,
        ir_unverified=ir_unverified,
        violations=tuple(violations),
        reasons=tuple(reasons),
    )


# --------------------------------------------------------------- the plan builder


def _drop_rec(row: MarginalRow, *, extra_reasons: Sequence[str] = ()) -> DropRec:
    return DropRec(
        player=row.player,
        position=row.position,
        team=row.team,
        espn_id=row.espn_id,
        marginal_points=row.marginal_points,
        horizon_weeks=row.horizon_weeks,
        unpriceable=row.unvalued,
        reasons=tuple(extra_reasons) + tuple(row.reasons),
    )


def _zero_drop_reslot(
    roster_rows: Sequence[Mapping], structure: RosterStructure
) -> tuple[list[str], list[str]] | None:
    """The PREFERRED, zero-drop fix (item 3.4 audit F1): can a pure re-slot make the
    roster legal? Returns ``(benched_names, moved_to_ir_names)`` when it can, else
    ``None`` (a drop is genuinely required).

    Restorative by construction: it simulates the moves and only returns them when
    ``check_legality`` on the result is legal.

    1. Bench every non-ELIGIBLE IR occupant (ESPN forces them off IR anyway).
    2. Bench any still-excess IR occupants (ir_count > ir_slots).
    3. Seat IR-eligible active bodies into freed IR slots while the active roster is
       oversized — each seating frees one active slot.
    """
    rows = [dict(r) for r in roster_rows]
    benched: list[str] = []
    moved_to_ir: list[str] = []

    def name(r: Mapping) -> str:
        return str(r.get("player") or r.get("espn_player_id") or "?")

    for r in rows:
        if _slot(r) == "IR" and _ir_status(r) != "ELIGIBLE":
            r["lineup_slot"] = "BE"
            benched.append(name(r))
    while sum(1 for r in rows if _slot(r) == "IR") > structure.ir_slots:
        occ = next(r for r in reversed(rows) if _slot(r) == "IR")
        occ["lineup_slot"] = "BE"
        benched.append(name(occ))
    while (
        sum(1 for r in rows if _slot(r) == "IR") < structure.ir_slots
        and check_legality(rows, structure=structure).active_count > structure.active_slots
    ):
        cand = next((r for r in rows if _slot(r) != "IR" and _ir_eligible(r)), None)
        if cand is None:
            break
        cand["lineup_slot"] = "IR"
        moved_to_ir.append(name(cand))

    return (benched, moved_to_ir) if check_legality(rows, structure=structure).legal else None


def _cause_phrase(verdict: LegalityVerdict) -> str:
    """A one-line 'why you are over' naming the IR-ineligible occupant(s)."""
    if verdict.ir_ineligible:
        names = "; ".join(
            f"{o.player} reset to {o.injury_status or 'no injury designation'} in your "
            f"IR slot (no longer IR-eligible)"
            for o in verdict.ir_ineligible
        )
        return (
            f"your active roster is {verdict.active_count} of {verdict.active_slots} "
            f"because {names}, so ESPN counts him on your active roster"
        )
    return (
        f"your active roster is {verdict.active_count} of {verdict.active_slots}"
    )


def _forced_drop_reason(
    row: MarginalRow, verdict: LegalityVerdict, *, secondary: bool = False
) -> str:
    lead = (
        "ALTERNATIVE (costs a drop) — if you would rather not make the IR move above"
        if secondary
        else "DROP THIS PLAYER to get back to a legal roster"
    )
    return (
        f"{lead}: {_cause_phrase(verdict)}. ESPN blocks every waiver claim and "
        f"free-agent add until you are back to {verdict.active_slots}. {row.player} is "
        f"the lowest-value player you can drop ({row.marginal_points:+.1f} house pts "
        f"over {row.horizon_weeks} week(s))."
    )


def _claim_reasons(
    swap: SwapRow,
    *,
    kind: str,
    waiver_rank: int | None,
    team_count: int | None,
    is_pure_add: bool,
    candidate_notes: Sequence[str],
    annotation_caveat: str | None = None,
) -> tuple[str, ...]:
    out: list[str] = list(swap.reasons)
    if kind == KIND_WAIVER:
        out.append(
            "WAIVERS claim — queue it, do not click: it is free and non-FAAB, "
            "processed in ESPN's overnight batch. Submitting costs nothing, but "
            "each claim you WIN resets your waiver priority to worst-in-league."
        )
        if waiver_rank is not None:
            denom = f" of {team_count}" if team_count else ""
            out.append(
                f"your waiver priority is {waiver_rank}{denom} this week (1 wins "
                f"first) — teams ahead of you win a contested player. This orders "
                f"WHO wins a fight, not which of YOUR claims to prefer; your claims "
                f"are ranked by projected gain."
            )
    elif kind == KIND_UNKNOWN:
        out.append(
            f"UNRECOGNIZED roster status ({swap.add_status or 'none'!r}) — we could "
            f"NOT tell whether this is a waiver claim or a free-agent grab. VERIFY in "
            f"the ESPN app before acting; do not assume it is a click."
        )
    else:
        out.append(
            "FREE AGENT — first-come-first-served: grab him now, speed matters "
            "(no waiting period, no priority spent)."
        )
    # Streamed swaps live in their OWN section now (item 3.4 audit F4); a swap here
    # with horizon==1 is a late-season season-long move, not a stream.
    if is_pure_add:
        out.append(
            "your roster has an OPEN active slot — this is a pure ADD, no drop "
            "required. (The gain shown is at least this much; filling an open slot is "
            "worth no less than the paired swap it was priced from.)"
        )
    else:
        out.append(
            f"pair: drop {swap.drop} for this add — your roster is full (16 active), "
            f"so this claim needs its OWN drop and a won claim fills the freed slot."
        )
        if swap.drop_unpriceable:
            out.append(
                f"heads up: {swap.drop} could not be priced (no usable projection), so "
                f"this gain is an UPPER BOUND — confirm him manually before dropping."
            )
    if annotation_caveat:
        out.append(annotation_caveat)
    out.extend(candidate_notes)
    return tuple(out)


def _candidate_notes_by_espn(
    conn, *, as_of, season, view, today
) -> tuple[dict[str, list[str]], str | None]:
    """(espn_id -> opportunity-signal note(s), error_note) from item 3.3, best-effort.

    ``build_candidates`` needs a completed week; pre-season it raises
    ``NoCompletedWeek`` — the annotation is optional context, so we skip silently.
    ANY OTHER failure is a visible degrade, not a silent one (item 3.4 audit F10):
    it returns an error note so the plan can disclose that the signal load failed.
    """
    notes: dict[str, list[str]] = {}
    try:
        board = build_candidates(conn, as_of=as_of, season=season, view=view, today=today)
    except NoCompletedWeek:
        return notes, None
    except Exception as exc:  # noqa: BLE001 — surfaced as a NOTE, never silently swallowed
        return notes, (
            f"opportunity signals UNAVAILABLE — the usage/injury signal load failed "
            f"({type(exc).__name__}: {exc}); the claims below carry no injury/usage "
            f"context. This is a degrade, not 'no news' — verify manually."
        )
    for c in board.rows:
        if not c.espn_id:
            continue
        head = c.reasons[0] if c.reasons else c.signal_kind
        notes.setdefault(str(c.espn_id), []).append(
            f"opportunity signal [{c.signal_kind}]: {head}"
        )
    return notes, None


def _is_streamed(s: SwapRow) -> bool:
    """A this-week-only D/ST or K swap — 3.5's lane (item 3.4 audit F4). Keyed on
    the DROP position AND a 1-week horizon, so a late-season 1-week season-long swap
    is not misfiled as a stream."""
    return s.horizon_weeks == 1 and s.drop_position in STREAMED_POSITIONS


def _add_espn_id(s: SwapRow, dup_names: set[str]) -> tuple[str | None, str | None]:
    """The add's espn_id joined on IDENTITY (item 3.4 audit F3), plus a caveat when
    identity is unavailable and the display name is ambiguous."""
    if s.add_espn_id is not None:
        return s.add_espn_id, None
    if s.add in dup_names:
        return None, (
            f"note: another free agent shares the name '{s.add}', and this swap "
            f"carried no ESPN id — opportunity-signal context was withheld to avoid "
            f"attaching the wrong player's news."
        )
    return None, None


def _swap_rec(
    s: SwapRow, *, waiver_rank, team_count, candidate_notes, dup_names, is_pure_add: bool
) -> ClaimRec:
    kind = _kind_of(s.add_status)
    espn_id, caveat = _add_espn_id(s, dup_names)
    return ClaimRec(
        add=s.add,
        add_position=s.add_position,
        add_espn_id=espn_id,
        kind=kind,
        gain=s.gain,
        drop=None if is_pure_add else s.drop,
        drop_position=None if is_pure_add else s.drop_position,
        startable_this_week=s.add_startable_this_week,
        horizon=s.horizon_weeks,
        drop_unpriceable=False if is_pure_add else s.drop_unpriceable,
        waiver_rank=waiver_rank if kind == KIND_WAIVER else None,
        reasons=_claim_reasons(
            s, kind=kind, waiver_rank=waiver_rank, team_count=team_count,
            is_pure_add=is_pure_add,
            candidate_notes=candidate_notes.get(espn_id or "", ()),
            annotation_caveat=caveat,
        ),
    )


def _select_claims(
    swaps: Sequence[SwapRow],
    *,
    claim_budget: int,
    waiver_rank: int | None,
    team_count: int | None,
    open_slots: int,
    candidate_notes: Mapping[str, list[str]],
    dup_names: set[str],
) -> tuple[tuple[ClaimRec, ...], tuple[ClaimRec, ...], tuple[ClaimRec, ...]]:
    """Greedy selection over the swap matrix -> (waiver claims, FCFS grabs, streaming).

    * Streamed D/ST & K swaps are SEGREGATED into their own this-week-only section
      (item 3.4 audit F4) — never ranked against or budgeted with season-long adds.
    * ``open_slots`` (>0 on a sub-full roster) are filled FIRST as PURE ADDS with no
      paired drop and no "roster is full" reason (item 3.4 audit F17); only the
      remainder is paired, each with a DISTINCT drop.
    * Add/drop identity uses the ESPN id, not the display name (item 3.4 audit F3).
    * A claim whose only drop is UNPRICEABLE is de-prioritized so its non-comparable
      (upper-bound) gain cannot claim a top-k slot (item 3.4 audit F6).
    """
    streamed = [s for s in swaps if _is_streamed(s)]
    seasonal = [s for s in swaps if not _is_streamed(s)]
    # Consider priceable-drop swaps first so an inflated unpriceable-drop gain never
    # wins an add slot ahead of a real one, then gain desc.
    order = sorted(seasonal, key=lambda s: (s.drop_unpriceable, -s.gain, s.add, s.drop))

    def add_key(s: SwapRow) -> str:
        return s.add_espn_id if s.add_espn_id is not None else f"name:{s.add}"

    def drop_key(s: SwapRow) -> str:
        return s.drop_espn_id if s.drop_espn_id is not None else f"name:{s.drop}"

    seen_adds: set[str] = set()
    seen_drops: set[str] = set()
    pure_used = 0
    recs: list[ClaimRec] = []
    for s in order:
        if add_key(s) in seen_adds:
            continue
        if pure_used < open_slots:
            seen_adds.add(add_key(s))
            pure_used += 1
            recs.append(_swap_rec(
                s, waiver_rank=waiver_rank, team_count=team_count,
                candidate_notes=candidate_notes, dup_names=dup_names, is_pure_add=True,
            ))
            continue
        if drop_key(s) in seen_drops:
            continue
        seen_adds.add(add_key(s))
        seen_drops.add(drop_key(s))
        recs.append(_swap_rec(
            s, waiver_rank=waiver_rank, team_count=team_count,
            candidate_notes=candidate_notes, dup_names=dup_names, is_pure_add=False,
        ))

    def bucket(kind: str) -> tuple[ClaimRec, ...]:
        # Pure adds + priceable-drop first (drop_unpriceable False), unpriceable last.
        picked = sorted(
            (r for r in recs if r.kind == kind),
            key=lambda r: (r.drop_unpriceable, -r.gain),
        )
        return tuple(picked[:claim_budget])

    # UNKNOWN-kind adds ride with the grabs list but carry an explicit VERIFY reason
    # (item 3.4 audit F8) — never a silent FCFS default.
    grabs = tuple(sorted(
        (r for r in recs if r.kind in (KIND_FREE_AGENT, KIND_UNKNOWN)),
        key=lambda r: (r.drop_unpriceable, -r.gain),
    )[:claim_budget])

    stream_recs = tuple(
        _swap_rec(
            s, waiver_rank=waiver_rank, team_count=team_count,
            candidate_notes=candidate_notes, dup_names=dup_names, is_pure_add=False,
        )
        for s in sorted(streamed, key=lambda s: (-s.gain, s.add, s.drop))[:claim_budget]
    )
    return bucket(KIND_WAIVER), grabs, stream_recs


def build_waiver_plan(
    conn,
    *,
    as_of,
    season: int,
    own_team_id: int | None,
    weeks: Iterable[int] | None = None,
    last_week: int = 17,
    roster_structure: RosterStructure = DEFAULT_ROSTER,
    pool_limit: int | None = DEFAULT_POOL_LIMIT,
    source: str = "sleeper_rotowire",
    view: base.AsOfView = "historical",
    today=None,
    claim_budget: int = 3,
) -> WaiverPlan:
    """The waiver plan (item 3.4). Rule 1: ``as_of`` keyword-only, no default;
    ``view`` threaded into every accessor.

    1. Fetch the RAW roster (IR rows included) via ``get_player_state``.
    2. ``check_legality`` FIRST. If illegal: reslot IR-ineligible occupants
       IR->BE, price ONE ``build_board`` scan to name the forced drop (guarding
       ``WeekResolutionError`` so a missing week window does not crash), and
       RETURN ``blocked`` with ``claims=()`` — the done-when.
    3. If legal: ONE ``build_board`` scan -> drop board (``board.ranked``) +
       claims (``board.swaps``, split WAIVER/FREE_AGENT, each with a distinct
       drop, bounded to ``claim_budget``), annotated with ``build_candidates``
       opportunity signals joined on ``espn_id``.
    """
    # Refuse to value the whole free-agent universe as the roster (item 3.4 audit
    # F9) — mirror resolve_own_team's refuse-rather-than-guess convention (Rule 6).
    if own_team_id is None:
        raise league_state.OwnTeamUnresolved(
            "build_waiver_plan needs a resolved own_team_id; got None. Pass --team or "
            "resolve it via resolve_own_team — reading the whole league universe as "
            "your roster would produce a confidently-wrong plan."
        )

    resolved_weeks: tuple[int, ...] = tuple(sorted({int(w) for w in weeks})) if weeks is not None else ()
    notes: list[str] = []

    roster_rows = [dict(r) for r in league_state.get_player_state(
        conn, as_of=as_of, season=season, on_team_id=own_team_id, view=view,
    )]
    verdict = check_legality(roster_rows, structure=roster_structure)

    # team context (waiver priority + ESPN's own lock) — CONTEXT only, never the gate.
    # Read ALL teams so the 'of N' denominator comes from data, not a hardcode (F13).
    waiver_priority: int | None = None
    transaction_locked = False
    all_team_rows = league_state.get_team_state(
        conn, as_of=as_of, season=season, view=view,
    )
    team_count: int | None = len(all_team_rows) or None
    own_rows = [t for t in all_team_rows if t["team_id"] == own_team_id]
    if own_rows:
        t = own_rows[0]
        waiver_priority = int(t["waiver_rank"]) if t["waiver_rank"] is not None else None
        transaction_locked = bool(t["is_transaction_locked"])
    if transaction_locked:
        notes.append(
            "ESPN also reports your team as transaction-locked (a team-level flag "
            "that also fires during live games) — this plan does not gate on it, "
            "but if a legal claim will not submit, that is likely why."
        )

    freshness = tuple(_freshness_lines(conn, season=season, as_of=as_of, today=today))

    if not verdict.legal:
        # REFUSE to plan claims. Offer fixes in PREFERENCE ORDER (item 3.4 audit F1):
        # (a) the ZERO-DROP IR-move if a pure re-slot restores legality — PRIMARY;
        # (b) the forced DROP of the lowest-value body — only when the ACTIVE roster
        #     is oversized (a body-drop cannot clear a pure IR-overcount), demoted to
        #     secondary whenever (a) applies.
        ir_move = _zero_drop_reslot(roster_rows, roster_structure)
        ir_move_fix: tuple[str, ...] = ()
        if ir_move is not None:
            benched, moved_to_ir = ir_move
            if moved_to_ir:
                ir_move_fix = (
                    f"BEST FIX (no drop): move {'; '.join(benched)} out of your IR slot "
                    f"to the bench and move {'; '.join(moved_to_ir)} (IR-eligible) into "
                    f"your IR slot — this makes you legal with NO drop.",
                    IR_FIX_MODEL_LABEL,
                )
            elif benched:
                ir_move_fix = (
                    f"BEST FIX (no drop): move {'; '.join(benched)} out of your IR slot "
                    f"to the bench — this makes you legal with NO drop (your IR slot is "
                    f"over capacity, not your active roster).",
                    IR_FIX_MODEL_LABEL,
                )

        # A forced DROP is a valid, restorative fix ONLY when the ACTIVE roster is
        # oversized. Reslot the ineligible IR occupant(s) IR->BE so the flipped
        # player is visible to the drop board that must decide the fix.
        ineligible_ids = {o.espn_id for o in verdict.ir_ineligible if o.espn_id}
        reslotted = []
        for r in roster_rows:
            eid = str(r["espn_player_id"]) if r.get("espn_player_id") is not None else None
            if _slot(r) == "IR" and eid in ineligible_ids:
                r = {**r, "lineup_slot": "BE"}
            reslotted.append(r)

        forced_drop: DropRec | None = None
        drop_board: tuple[DropRec, ...] = ()
        blocked_weeks: tuple[int, ...] = resolved_weeks
        if verdict.active_count > roster_structure.active_slots:
            try:
                board = build_board(
                    conn, as_of=as_of, season=season, roster=reslotted,
                    weeks=weeks, last_week=last_week, roster_structure=roster_structure,
                    pool_limit=pool_limit, source=source, view=view, today=today,
                )
                drop_board = tuple(_drop_rec(r) for r in board.ranked)
                blocked_weeks = tuple(board.weeks)   # the window that PRICED the drop (F11)
                if board.ranked:
                    forced_drop = _drop_rec(
                        board.ranked[0],
                        extra_reasons=(_forced_drop_reason(
                            board.ranked[0], verdict, secondary=bool(ir_move_fix)),),
                    )
                else:
                    notes.append(
                        "could not name a single forced drop: every priceable player is "
                        "unvalued at this as-of (see CANNOT VALUE) — verify manually and "
                        "drop your lowest-value body to reach "
                        f"{roster_structure.active_slots}."
                    )
            except WeekResolutionError as exc:
                # Legality does NOT depend on pricing. Name the cause and the bodies to
                # consider dropping without fabricating a priced drop.
                reslot_names = ", ".join(
                    str(r.get("player")) for r in reslotted if _slot(r) != "IR"
                )
                notes.append(
                    f"the roster is illegal and ESPN is blocking all transactions; "
                    f"the week window could not be resolved to price a specific forced "
                    f"drop ({exc}). Pass --from-week. Drop your lowest-value active "
                    f"body to reach {roster_structure.active_slots}. Active bodies: "
                    f"{reslot_names}."
                )
        # The ineligible IR occupant himself is an explicit drop/keep candidate (F1).
        for o in verdict.ir_ineligible:
            notes.append(
                f"you may instead DROP {o.player} himself (the IR-ineligible occupant) "
                f"— dropping him also frees the active slot he now counts against."
            )
        if verdict.ir_count > roster_structure.ir_slots and ir_move_fix == ():
            names = "; ".join(o.player for o in verdict.ir_ineligible) or "one IR occupant"
            notes.append(
                f"your IR slot holds {verdict.ir_count} players (max "
                f"{roster_structure.ir_slots}); drop or bench "
                f"{verdict.ir_count - roster_structure.ir_slots} of them ({names})."
            )

        return WaiverPlan(
            legality=verdict,
            forced_drop=forced_drop,
            ir_move_fix=ir_move_fix,
            claims=(),
            fcfs_grabs=(),
            streaming=(),
            drop_board=drop_board,
            waiver_priority=waiver_priority,
            team_count=team_count,
            transaction_locked=transaction_locked,
            freshness=freshness,
            notes=tuple(notes),
            as_of=normalize_as_of(as_of).isoformat(),
            season=int(season),
            team_id=own_team_id,
            weeks=blocked_weeks,
        )

    # --- legal path: ONE scan, then compose. -------------------------------------
    # Fetch the pool explicitly (single source, single as_of). Guard a leaked
    # 'ONTEAM' token out of the FA pool (item 3.4 audit F8): state.py's conflict path
    # nulls on_team_id but keeps roster_status='ONTEAM', so such a row is a data
    # artifact, not a real free agent.
    pool_rows = []
    for r in league_state.get_free_agents(conn, as_of=as_of, season=season, view=view):
        r = dict(r)
        if str(r.get("roster_status") or "").strip().upper() == "ONTEAM":
            notes.append(
                f"held out '{r.get('player')}' from the free-agent pool: ESPN reports "
                f"him ONTEAM with no roster holder (a transient mid-sync conflict) — "
                f"re-check after the next `ziggurat league sync`."
            )
            continue
        pool_rows.append(r)

    # Duplicate display names in the pool — for the refuse-to-annotate fallback when
    # a swap carries no ESPN id (item 3.4 audit F3).
    ids_by_name: dict[str, set[str]] = {}
    for r in pool_rows:
        nm, eid = r.get("player"), r.get("espn_player_id")
        if nm and eid is not None:
            ids_by_name.setdefault(str(nm), set()).add(str(eid))
    dup_names = {nm for nm, ids in ids_by_name.items() if len(ids) > 1}

    board = build_board(
        conn, as_of=as_of, season=season, roster=roster_rows, pool=pool_rows,
        weeks=weeks, last_week=last_week, roster_structure=roster_structure,
        pool_limit=pool_limit, source=source, view=view, today=today,
    )
    swaps = board.swaps           # LAZY + expensive — touch ONCE, cache
    drop_board = tuple(_drop_rec(r) for r in board.ranked)

    candidate_notes, candidate_err = _candidate_notes_by_espn(
        conn, as_of=as_of, season=season, view=view, today=today,
    )
    if candidate_err:
        notes.append(candidate_err)

    open_slots = max(roster_structure.active_slots - verdict.active_count, 0)
    claims, grabs, streaming = _select_claims(
        swaps, claim_budget=claim_budget, waiver_rank=waiver_priority,
        team_count=team_count, open_slots=open_slots,
        candidate_notes=candidate_notes, dup_names=dup_names,
    )

    notes.extend(board.notes)
    if open_slots:
        notes.append(
            f"you have {open_slots} OPEN active slot(s): the top add(s) below are PURE "
            f"ADDS — no drop is required to add them."
        )
    if not claims and not grabs and not streaming:
        notes.append(
            "no positive-value add was found in the free-agent pool at this as-of — "
            "hold your roster. (Queuing extra low-gain claims is free and optional; "
            "there simply are none worth the priority right now.)"
        )
    else:
        notes.append(
            f"showing the top {claim_budget} claim(s) and grab(s) by projected gain; "
            "additional low-gain claims are free/optional (non-FAAB) — each won claim "
            "resets your priority to worst-in-league, so spend it on the best target."
        )

    return WaiverPlan(
        legality=verdict,
        forced_drop=None,
        ir_move_fix=(),
        claims=claims,
        fcfs_grabs=grabs,
        streaming=streaming,
        drop_board=drop_board,
        waiver_priority=waiver_priority,
        team_count=team_count,
        transaction_locked=transaction_locked,
        freshness=freshness,
        notes=tuple(notes),
        as_of=normalize_as_of(as_of).isoformat(),
        season=int(season),
        team_id=own_team_id,
        weeks=tuple(board.weeks),
    )


# ------------------------------------------------------------------- staleness


def _freshness_lines(conn, *, season, as_of, today) -> list[str]:
    """Freshness banner. Independent of ``build_board`` so it renders even on the
    blocked path (where pricing may not run). Reads league-state snapshot recency
    AND item 3.1b's per-source contract — a July projection pricing a November
    waiver day carries a valid ``knowable_as_of`` and is Rule-1-invisible."""
    out: list[str] = []
    cutoff = normalize_as_of(as_of)

    days = league_state.snapshot_days(conn, season=season)
    knowable = [d for d in days if normalize_as_of(d) <= cutoff]
    if knowable:
        gap = (cutoff - normalize_as_of(knowable[-1])).days
        out.append(f"league state: last snapshot {knowable[-1]} — {gap} day(s) before {as_of}")
        if gap > STALE_BANNER_DAYS:
            out.append(
                f"  WARNING: your roster and the free-agent pool are {gap} days stale. "
                f"Run `ziggurat league sync` — a stale snapshot mis-plans claims off a "
                f"player who has since moved."
            )
    else:
        out.append("league state: NO snapshot readable at this as-of — run `ziggurat league sync`.")

    if today is not None:
        watched = {"projections", "weekly_stats", "injuries"}
        for s in refresh.source_freshness(conn, season=season, today=today):
            if s["source"] in watched and s["verdict"] not in refresh.QUIET_VERDICTS:
                age = "never pulled" if s["age_days"] is None else f"{s['age_days']}d old"
                out.append(
                    f"  ingest says {s['source']}: {s['verdict']} ({age})"
                    + ("  [this source cannot be re-pulled — a missed day is gone]"
                       if s["perishable"] else "")
                )
    return out


# --------------------------------------------------------------------- display


def _claim_line(rec: ClaimRec) -> str:
    startable = "" if rec.startable_this_week else "  (cannot start this week)"
    horizon = "this week" if rec.horizon == 1 else f"{rec.horizon} wks"
    # An unpriceable drop is flagged in the DEFAULT view (item 3.4 audit F6),
    # mirroring the inline "(cannot start this week)" flag.
    unpriced = "  [drop UNPRICED — verify before dropping]" if rec.drop_unpriceable else ""
    if rec.drop is None:            # pure add — open slot, no drop (item 3.4 audit F17)
        return (
            f"  add {rec.add} ({rec.add_position})  (open slot — no drop)   "
            f"{rec.gain:+.1f} pts / {horizon}{startable}"
        )
    return (
        f"  add {rec.add} ({rec.add_position})  <-  drop {rec.drop} "
        f"({rec.drop_position or '-'})   {rec.gain:+.1f} pts / {horizon}"
        f"{startable}{unpriced}"
    )


def format_waiver_plan(plan: WaiverPlan, *, reasons: bool = False) -> str:
    """Render the waiver plan (display only — no logic, Rule 3).

    The legality verdict prints FIRST and LOUDLY when blocked: the operator's
    waiver day is dead in ESPN until the roster is legal.
    """
    out: list[str] = [
        f"waiver plan — season {plan.season}, as of {plan.as_of}"
        + (f", weeks {plan.weeks[0]}-{plan.weeks[-1]}" if plan.weeks else "")
    ]
    for line in plan.freshness:
        out.append(line)
    out.append("")

    # The IR-eligibility disclosure is load-bearing for a destructive action, so it
    # renders UNCONDITIONALLY whenever an IR occupant is present (item 3.4 audit F2).
    def ir_disclosure() -> list[str]:
        if v.ir_count <= 0:
            return []
        return [
            "  NOTE: IR eligibility is INFERRED from the injury tag and is UNVERIFIED "
            "— confirm in the ESPN app before you drop or bench anyone."
        ]

    # --- legality FIRST -------------------------------------------------------
    v = plan.legality
    if plan.blocked:
        out.append("!!! ROSTER ILLEGAL — ESPN IS BLOCKING ALL WAIVER CLAIMS AND ADDS !!!")
        out.append(f"  active roster {v.active_count} of {v.active_slots} "
                   f"({v.ir_count} of {v.ir_slots} IR slot used)")
        for problem in v.violations:
            out.append(f"  - {problem}")
        # PREFERRED zero-drop IR-move fix FIRST (item 3.4 audit F1).
        if plan.ir_move_fix:
            out.append("")
            out.append("  THE FIX (preferred — no drop):")
            for line in plan.ir_move_fix:
                out.append(f"    {line}")
        if plan.forced_drop:
            fd = plan.forced_drop
            out.append("")
            label = ("  ALTERNATIVE FIX — drop this player instead:"
                     if plan.ir_move_fix
                     else "  THE FIX — drop this player to get legal, then re-run:")
            out.append(label)
            out.append(f"    DROP {fd.player} ({fd.position}, {fd.team or '-'})  "
                       f"{fd.marginal_points:+.1f} house pts")
            if reasons:
                out.extend(f"        - {r}" for r in fd.reasons)
        out.extend(ir_disclosure())          # UNCONDITIONAL (F2)
        out.append("")
        out.append("  No claims are planned until the roster is legal.")
        if reasons:
            out.append("")
            out.append("  legality detail:")
            out.extend(f"    - {r}" for r in v.reasons)
        return "\n".join(out)

    out.append(v.reasons[0])
    # Required, non-blocking IR moves (an ineligible occupant on a legal roster, F1).
    for adv in v.ir_advisories:
        out.append(f"  {adv}")
    out.extend(ir_disclosure())              # UNCONDITIONAL on the legal path too (F2)
    if plan.waiver_priority is not None:
        denom = f" of {plan.team_count}" if plan.team_count else ""
        out.append(f"  waiver priority: {plan.waiver_priority}{denom} (1 = next claim wins) "
                   f"— success-likelihood context, not a claim order.")
    for note in plan.notes:
        out.append(f"! {note}")
    out.append("")

    # --- waiver claims --------------------------------------------------------
    out.append(f"WAIVER CLAIMS (queue these — free, priority-ordered)  ({len(plan.claims)})")
    if not plan.claims:
        out.append("  (none worth the priority right now)")
    for rec in plan.claims:
        out.append(_claim_line(rec))
        if reasons:
            out.extend(f"      - {r}" for r in rec.reasons)
    out.append("")

    # --- FCFS grabs -----------------------------------------------------------
    out.append(f"FREE-AGENT GRABS (first-come — act fast, no priority)  ({len(plan.fcfs_grabs)})")
    if not plan.fcfs_grabs:
        out.append("  (none)")
    for rec in plan.fcfs_grabs:
        out.append(_claim_line(rec))
        if reasons:
            out.extend(f"      - {r}" for r in rec.reasons)
    out.append("")

    # --- streaming (this week only — item 3.5's lane) (item 3.4 audit F4) ------
    if plan.streaming:
        out.append(f"STREAMING (this week only — item 3.5's lane, NOT a season-long "
                   f"claim)  ({len(plan.streaming)})")
        for rec in plan.streaming:
            out.append(_claim_line(rec))
            if reasons:
                out.extend(f"      - {r}" for r in rec.reasons)
        out.append("")

    # --- drop board -----------------------------------------------------------
    # Legend reconciles the sign vs the claims section (item 3.4 audit F18): a
    # drop-board number is the player's OWN value (what you GIVE UP by dropping him);
    # a claim's +gain is the NET of adding someone better in his place.
    out.append("DROP BOARD — each number is what you'd GIVE UP by dropping that player "
               "(most droppable first)")
    out.append("  legend: a claim's +gain above already subtracts this drop-cost; the "
               "same player is not counted twice.")
    season_rows = [d for d in plan.drop_board
                   if not (d.horizon_weeks == 1 and d.position in STREAMED_POSITIONS)]
    streamed_rows = [d for d in plan.drop_board
                     if d.horizon_weeks == 1 and d.position in STREAMED_POSITIONS]

    def drop_line(d: DropRec) -> str:
        return (f"  {d.player} ({d.position}, {d.team or '-'})  {d.marginal_points:+.1f} "
                f"pts / {d.horizon_weeks} wk(s)")

    for d in season_rows:
        out.append(drop_line(d))
        if reasons:
            out.extend(f"      - {r}" for r in d.reasons)
    if streamed_rows:
        out.append("  -- streamed weekly (K/DST) — a 1-week number, NOT comparable to "
                   "the season-long rows above --")
        for d in streamed_rows:
            out.append(drop_line(d))
            if reasons:
                out.extend(f"      - {r}" for r in d.reasons)
    return "\n".join(out)
