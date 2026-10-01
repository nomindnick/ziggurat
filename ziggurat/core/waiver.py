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
legal roster but wrong for the precheck: an IR occupant who may no longer STAY
(ESPN's documented rule: he has NO injury designation again, or is suspended —
NOT a QUESTIONABLE/DOUBTFUL occupant, who may stay) makes the roster INVALID and
can only leave by a move to the bench, so he counts against the 16 active slots
and a full roster needs a drop before ESPN processes anything. (Until 2026-09-24
this paragraph said Tuesday's OUT -> QUESTIONABLE reset caused it — the reverse
of ESPN's rule.) So ``check_legality``
recounts IR itself from the RAW rows, independent of ``build_board`` (which
RAISES ``WeekResolutionError`` on any state with ``scoring_period==0`` and no
resolvable schedule — i.e. the live DB and every synthetic test state).

IR ELIGIBILITY — WHAT IS SETTLED AND WHAT IS NOT (item 3.8a, 2026-09-02). ESPN
serves a per-player ``injured`` boolean, now ingested (migration 014), and it
marks EXACTLY the OUT/INJURY_RESERVE designations this module treats as
IR-eligible — 0 exceptions over the 1,036-player universe. That half is settled
and is re-checked on every snapshot by ``state.ir_rule_check`` /
``ziggurat league ir-check``. What ESPN's IR SLOT accepts is a second question
with two halves (2026-09-24): ENTRY ({OUT, INJURY_RESERVE}) was observed on the
ESPN website and the league's first occupants followed; OCCUPANCY is ESPN's own
documented rule (``league_state.IR_RULES_SOURCE``) — a QUESTIONABLE or DOUBTFUL
occupant may STAY, only an occupant with no designation (or a suspension) makes
the roster invalid. Until that date this module treated a QUESTIONABLE occupant as
the canonical illegal roster, the reverse of ESPN's rule. ``IR_FIX_MODEL_LABEL``
says which parts are documented, which observed, and what is still unseen.

(``eligibleSlots`` was the plan's assumed source of machine truth and is NOT one:
slot 21 (IR) is listed for 1,036 of 1,036 players, alongside slot 20 (BE). It is
a POSITIONAL map. It is deliberately not ingested.)

CLAIMS ARE A CHAIN, NOT A LIST (item 3.4b, 2026-09-02). Every ``SwapRow.gain`` is
priced as if that swap were the ONLY one you make. Ranking them and printing the
top k therefore quotes each claim's value against a roster that will not exist
once the claims above it win. Measured on 2026-09-01: three RB adds priced
+5.55 / +5.25 / +2.21 each alone; all three won; the JOINT gain was +1.22, and
the next morning the same module recommended the exact REVERSE of all three
(+2.65 / +1.68 / ... each alone, −1.22 jointly) off unchanged projections. The
tool was walking a flat ridge and would have kept recommending one reversal a day.

So the season-long list is now built SEQUENTIALLY: claim k is priced against the
roster after claims 1..k−1 have won (``MarginalBoard.value_after`` at the
reporting depth), and the chain STOPS at the first non-positive conditional gain.
``ClaimRec.gain`` is that conditional number; ``ClaimRec.gain_alone`` is the old
standalone one, kept and printed because it is what the claim is worth if the
lines above it LOSE to a rival with better priority. A SHORT list is now an
answer, not a truncation — see ``_select_claims``.

3.4 vs 3.6. 3.4 returns a deterministic, ``as_of``-gated PLAN OBJECT. 3.6 owns
scheduling, the briefing render, and event-triggered alerts, and CALLS 3.4.

Standing rules. Rule 1 — ``build_waiver_plan`` / ``check_legality`` are
keyword-only ``as_of`` (``check_legality`` is pure and needs none); ``view`` is
threaded into every accessor. Rule 2 — no scoring constant here; every point
comes from ``build_board``. Rule 3 — the CLI parses/calls/prints. Rule 6 — every
claim, drop, and the legality verdict ships plain reasons. Rule 8 — permanent
module, never imports from ``ziggurat/draft/``.
"""

import heapq
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from ziggurat.core import units
from ziggurat.core.candidates import (
    CandidateBoard,
    EpisodeHistory,
    EvaluatedRows,
    NoCompletedWeek,
    build_candidates,
    episode_legend,
)
from ziggurat.core.lineup import fill_lineup
from ziggurat.core.lineup_support import lineup_sigma, sigma_provenance
from ziggurat.core.marginal import (
    ACQ_FREE_AGENT,
    ACQ_UNKNOWN,
    ACQ_WAIVER,
    DEFAULT_POOL_LIMIT,
    POSITION_CAPS,
    STREAMED_POSITIONS,
    UNDROPPABLE_TAG,
    cap_allows_keep,
    MarginalBoard,
    MarginalRow,
    SwapRow,
    WeekResolutionError,
    build_board,
    break_even_reacquisition,
    classify_acquisition,
    describe_cap,
)
from ziggurat.core.streaming import DST_CARD_SENTENCES
from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import base, refresh
from ziggurat.league import state as league_state

# --------------------------------------------------------------------- constants

# The IR-eligible designation set now lives in ``league/state.py`` — an
# ESPN-FACING definition, and ``league/`` cannot import ``core/`` while
# ``sync.format_status`` needs the standing check (item 3.8a). Re-exported here so
# ``waiver.IR_ELIGIBLE_STATUSES`` still resolves for every existing caller.
IR_ELIGIBLE_STATUSES = league_state.IR_ELIGIBLE_STATUSES
# Who may STAY once in the slot — wider than who may ENTER (ESPN's documented rule;
# see ``league_state.IR_RULES_SOURCE``).
IR_OCCUPANCY_OK_STATUSES = league_state.IR_OCCUPANCY_OK_STATUSES

# THE SETTLED HALF (item 3.8a, measured 2026-09-02). ESPN's own per-player
# ``injured`` boolean is ingested and it marks exactly this set. The words
# "UNVERIFIED" and "confirm in the ESPN app post-draft" LEFT this label on that
# date and moved to IR_FIX_MODEL_LABEL, which is the half that is still unverified.
IR_ELIGIBLE_LABEL = (
    "ESPN's own `injured` flag marks exactly the OUT/INJURY_RESERVE designations "
    "this system treats as IR-slot eligible — measured 2026-09-02 with 0 exceptions "
    "(n=1,036 across the whole player universe; n=160 rostered, where only one "
    "player carried it), and re-checked on EVERY snapshot by `ziggurat league "
    "ir-check`. `ziggurat league status` says so only when it changes. That set "
    "is who may ENTER the IR slot; a player already IN it may stay at QUESTIONABLE "
    "or DOUBTFUL (ESPN's documented rule). PUP / NFI have never appeared in this "
    "league and are treated as ineligible to enter — a watch, not a confirmation: "
    "the first one to appear is reported with the flag ESPN gave it."
)

# Acquisition kinds — the claims-vs-FCFS distinction, keyed ONLY on roster_status
# THROUGH the ONE shared classifier in marginal.py (item 3.4 audit F8), so the
# drop-board reason and the claim planner can never disagree about a player.
KIND_WAIVER = ACQ_WAIVER            # roster_status 'WAIVERS': a queued, priority-ordered claim
KIND_FREE_AGENT = ACQ_FREE_AGENT    # roster_status 'FREEAGENT': first-come-first-served
KIND_UNKNOWN = ACQ_UNKNOWN          # anything else (incl. a leaked 'ONTEAM'): verify, never silent FCFS

# THE IR-SLOT MECHANISM (item 3.8a; rewritten 2026-09-24). The `injured` flag
# settles which DESIGNATIONS may enter; what the SLOT itself does was unverified
# until three things landed on one day:
#   * OBSERVED (operator, ESPN website): IR is not on the per-player MOVE menu — the
#     2026-09-03 "nobody was offered IR" reading of that menu was therefore void —
#     it has its own "Add to IR" button whose page listed exactly the one OUT
#     player (a DAY_TO_DAY and fourteen ACTIVE players withheld); the first
#     occupant (Reed, OUT) was accepted and held, 16/16 active + 1/1 IR, legal.
#   * DOCUMENTED (ESPN Fan Support, updated 2026-08-18, surfaced by the
#     narrative-experiment Cowork report and fetched by the session): a
#     QUESTIONABLE/DOUBTFUL occupant may STAY and the roster stays valid; an
#     occupant with NO designation makes the roster INVALID until the user moves
#     him; suspended players are never eligible. So hypothesis (a) as first
#     written — "ESPN forces an ineligible player onto your active roster" — was
#     wrong in mechanism: ESPN does not move him, it refuses transactions until YOU
#     do. The fix this module proposes is the same move either way.
#   * STILL UNSEEN in this league: the INVALID transition itself.
IR_FIX_MODEL_LABEL = (
    "IR-slot rules this fix follows — ENTRY observed 2026-09-24 on the ESPN website "
    "(the \"Add to IR\" button, not the MOVE menu, lists exactly the players ESPN "
    "will accept; an OUT player was accepted and held). OCCUPANCY is ESPN's "
    "documented rule, not yet seen here: a player already on IR may stay at "
    "QUESTIONABLE or DOUBTFUL, but one with NO injury designation (or a suspension) "
    "makes your roster INVALID — ESPN then processes no claim or add until you move "
    "him to the bench, which needs an open active slot (a drop if you have none). "
    "`ziggurat league ir-check` lists every occupant with ESPN's own flag."
)

# The staleness banner shouts past this many days between the data's pull date and
# the decision date (same constant marginal.py / candidates.py use).
STALE_BANNER_DAYS = 7

# --- item 3.4b: the sequential claim chain -----------------------------------
# An APPROXIMATE ceiling on how many roster valuations one selection may run,
# because the chain's cost scales with (chain length x candidates re-evaluated)
# and NOT with the matrix size. The fence is checked before each CANDIDATE
# evaluation, so a run can exceed it by a small constant (measured +2): the base
# value, each step's ``prev``, each pure add's ``alone`` and the joint
# ``chain_gain`` are bookkeeping calls that must happen for the plan to be
# renderable at all, and they are counted but not refused.
#
# Measured 2026-09-02 on the live board (82 distinct adds, 174 seasonal rows):
# one depth-3 SWAP valuation is ~56 ms and one PURE-ADD valuation ~68 ms (the
# roster grows by one, so the depth-3 enumeration is larger). The shipped
# full-roster chain took 45 of them (~2.6 s) on top of a 21.8 s `ziggurat
# waivers` run, so this ceiling is worth 11-15 s of wall time, not the ~2.6 s the
# live path spends. It is enforced and DISCLOSED (the chain stops with a note)
# rather than silently truncating.
CHAIN_EVAL_BUDGET = 200

# Phase A (open active slots) is exhaustive per step, so on a sub-full roster it
# could consume the WHOLE ceiling before a single swap was priced — measured
# 2026-09-02: at two open slots phase A burned 194 of 200 valuations and phase B,
# the cheap lazy half where the swaps live, never ran. Phase A is therefore
# fenced twice: its own share of the ceiling (this many valuations are RESERVED
# for the swap lane) and a per-step scan cap. A phase-A cost stop is never
# terminal — phase B still runs.
PHASE_B_EVAL_RESERVE = 80

# Phase A scans at most this many distinct adds per open slot, ordered by
# standalone gain. 82 distinct adds x ~68 ms = ~5.6 s PER OPEN SLOT unbounded;
# this caps it at ~1.7 s. The ordering is a HEURISTIC (a standalone swap gain is
# a LOWER bound on the same add's pure-add gain), so the truncation is DISCLOSED
# in a plan note naming K and how many were skipped — never silent.
PHASE_A_SCAN_TOP_K = 25

# How many rejected candidates are SHOWN with a measured number, and how many
# STALE ones may be re-priced to produce one. A candidate the chain already
# priced against the FINISHED list is free (its number is already in hand) and is
# always classified from it; only a stale row costs a valuation, and those are
# capped. Everything else is reported as a count — never as a silent cap and
# never with a number nobody measured.
CHAIN_REJECTED_PRICED = 3

# Why the chain ended — reported verbatim, because "the next claim is worth <= 0"
# and "the matrix had no legal drop left" are different facts and only the first
# is an economic conclusion.
STOP_NONPOSITIVE = "nonpositive"
STOP_BUDGET = "budget"
STOP_EXHAUSTED = "exhausted"
STOP_EVAL_BUDGET = "eval_budget"
STOP_NO_CANDIDATES = "no_candidates"

# Printed ONCE above a claim's opportunity-signal bullets (item 4.2b, B3). The
# claim order is provably inert to these bullets today — they are appended AFTER
# `_select_claims` has run and reach only players already in the printed chain —
# which is what makes this header a true statement rather than a promise. Wiring
# the column into the selection would make it a lie, so it is never a tie-break,
# a sort key or an input to `_select_claims` (pinned by test).
#
# Never, in any form: "the market will agree with this by Friday". That sentence
# was never shipped and the relabel must not smuggle it back as "the market
# usually follows" — the ECR panel measures AGREEMENT with the market, not a lead
# over it (item 4.1 audit), and this column has never been graded at all.
USAGE_EVIDENCE_HEADER = (
    "USAGE / ROLE EVIDENCE — does not change the claim order. The +/- number on "
    "the line above was priced from projections and is unchanged by what follows. "
    "These bullets are OBSERVED usage from ONE game (or one injury designation): "
    "not a forecast, not a probability, not a tie-break. Act in the printed # "
    "order, and do not drop a starter on the strength of a bullet here."
)


def _kind_of(add_status: str | None) -> str:
    """Claim vs FCFS grab vs unknown — through the ONE shared classifier (F8)."""
    return classify_acquisition(add_status)


def _slot(row: Mapping) -> str:
    return str(row.get("lineup_slot") or "").strip().upper()


def _ir_eligible(row: Mapping) -> bool:
    return _ir_status(row) == "ELIGIBLE"


def _ir_status(row: Mapping) -> str:
    """Three-way IR-slot ENTRY classification (item 3.4 audit F7; flag-first since
    3.8a): may this player be MOVED INTO the IR slot? Whether an occupant may STAY
    is a different, wider rule — ``_ir_occupancy`` (ESPN's documented rule,
    2026-09-24); ``check_legality`` uses that one.

    ELIGIBLE  — ESPN's own ``injured`` flag is set, or (when the flag was not
                captured) ESPN lists him OUT / INJURY_RESERVE.
    UNKNOWN   — no flag AND a blank injury_status: no evidence either way.
    INELIGIBLE — the flag is explicitly false, or (uncaptured) any other explicit
                status (QUESTIONABLE, ACTIVE, ...): ESPN will not offer him the
                slot (the "Add to IR" page withholds him, observed 2026-09-24).

    ESPN'S OWN FLAG WINS WHEN WE HAVE IT. It is the same field the app reads, and
    on 2026-09-02 it agreed with the designation rule on all 1,036 players — but
    "agrees today" is why we prefer it, not a reason to keep inferring. When it is
    absent (a pre-014 snapshot, or a rostered player missing from ESPN's pool
    response) the designation PROXY still answers, and ``_ir_proxy_note`` attaches
    a PER-PLAYER disclosure — never a blanket one, because the two cases are
    different and only one of them is about the migration.
    """
    flag = row.get("injured")
    if flag is not None:
        return "ELIGIBLE" if flag else "INELIGIBLE"
    tok = str(row.get("injury_status") or "").strip().upper()
    if not tok:
        return "UNKNOWN"
    if tok in IR_ELIGIBLE_STATUSES:
        return "ELIGIBLE"
    return "INELIGIBLE"


def _ir_occupancy(row: Mapping) -> str:
    """ESPN's OCCUPANCY rule for a row already in the IR slot: ``OK`` / ``INVALID``
    / ``UNKNOWN`` (see ``league_state.ir_occupancy_verdict``). Wider than ENTRY
    (``_ir_status``): a QUESTIONABLE/DOUBTFUL occupant may stay."""
    return league_state.ir_occupancy_verdict(row.get("injured"), row.get("injury_status"))


def _ir_occupant_reason(row: Mapping) -> str:
    """How we decided whether this OCCUPANT may stay in the IR slot (Rule 6)."""
    name = str(row.get("player") or row.get("espn_player_id") or "?")
    tag = row.get("injury_status") or "blank"
    verdict = _ir_occupancy(row)
    if verdict == "INVALID":
        return f"{name}: {_ir_ineligible_because_row(row)}."
    if row.get("injured"):
        return (f"{name}: ESPN's own `injured` flag reads TRUE (his injury tag is "
                f"{tag}), so he may stay in your IR slot.")
    if verdict == "OK" and str(tag).strip().upper() in IR_ELIGIBLE_STATUSES:
        return (f"{name}: ESPN's `injured` flag was NOT captured for this player in "
                f"this snapshot; his injury tag ({tag}) is an IR designation, so he may "
                f"stay in your IR slot (tag PROXY — re-run `ziggurat league sync`).")
    if verdict == "OK":
        return (f"{name}: ESPN lists him {tag} — he could not be MOVED INTO IR today, "
                f"but a player already in the slot may STAY at QUESTIONABLE or "
                f"DOUBTFUL and your roster stays valid (ESPN's IR rules).")
    return (f"{name}: {_ir_unknown_why(row)} — we did NOT count him against your "
            f"active roster; confirm on the ESPN roster page.")


def _ir_unknown_why(row: Mapping) -> str:
    """Why an occupant's STAY cannot be verified — the three UNKNOWN cases say
    different things, and a blank tag must never be quoted as a designation."""
    tag = str(row.get("injury_status") or "").strip()
    if row.get("injured") is None and not tag:
        return ("his ESPN injury status is blank and the `injured` flag was not "
                "captured, so there is no evidence either way")
    if tag.upper() in IR_ELIGIBLE_STATUSES and row.get("injured") is not None \
            and not row.get("injured"):
        return (f"ESPN's own `injured` flag reads FALSE while his injury tag says "
                f"{tag} — those two ESPN fields DISAGREE, which this league has never "
                f"shown us, and a FALSE flag alone does not mean he is healthy (every "
                f"QUESTIONABLE/DOUBTFUL player carries one)")
    return (f"ESPN lists him {tag}, a designation ESPN's IR rules do not cover "
            f"(they cover QUESTIONABLE/DOUBTFUL = may stay, no designation = invalid)")


def _ir_ineligible_because_row(row: Mapping) -> str:
    return _ir_ineligible_because(IRIneligible(
        player=str(row.get("player") or "?"), position=row.get("position"),
        espn_id=None, injury_status=row.get("injury_status"),
        injured=row.get("injured")))


def _ir_reason(row: Mapping) -> str:
    """How we decided this player's IR-slot eligibility, per player (Rule 6)."""
    name = str(row.get("player") or row.get("espn_player_id") or "?")
    status = _ir_status(row)
    if row.get("injured") is not None:
        return (
            f"{name}: ESPN's own `injured` flag reads "
            f"{'TRUE' if row['injured'] else 'FALSE'}, so we treat him as "
            f"IR-{'ELIGIBLE' if status == 'ELIGIBLE' else 'INELIGIBLE'} "
            f"(his injury tag is {row.get('injury_status') or 'blank'})."
        )
    return (
        f"{name}: ESPN's `injured` flag was NOT captured for this player in this "
        f"snapshot, so his IR eligibility is a PROXY on his injury tag "
        f"({row.get('injury_status') or 'blank'}) -> {status}. Re-run `ziggurat "
        f"league sync` and re-check; confirm in the app before acting on it."
    )


def _ir_ineligible_because(o: "IRIneligible") -> str:
    """WHY this occupant makes the roster INVALID, naming the signal that decided.

    Under ESPN's occupancy rule only two things do that: NO injury designation (he
    is healthy again) or a suspension. The flag/tag divergence (flag FALSE beside
    an OUT/IR tag) is NOT one of them — it is UNKNOWN (``_ir_unknown_why``), because
    a FALSE flag alone does not mean healthy.
    """
    tok = str(o.injury_status or "").strip().upper()
    if tok == "SUSPENSION":
        return ("ESPN lists him SUSPENDED, and suspended players may not be in the IR "
                "slot (ESPN's IR rules)")
    healthy = ("he is healthy again, and ESPN's IR rules make your roster INVALID "
               "while a healthy player sits in the IR slot")
    if o.injured is None:
        return (f"ESPN lists him {o.injury_status or 'with no injury designation'} — "
                f"{healthy} (ESPN's `injured` flag was NOT captured for this player, so "
                f"this rests on the injury-tag PROXY)")
    return (f"ESPN's own `injured` flag reads FALSE for him and his injury tag says "
            f"{o.injury_status or 'nothing'} — {healthy}")


# ------------------------------------------------------------------- output rows


@dataclass(frozen=True)
class IRIneligible:
    """An IR-slot occupant who may NOT stay there under ESPN's occupancy rule (no
    injury designation, or suspended) — the reference that names the CAUSE of an
    invalid roster. (Until 2026-09-24 a QUESTIONABLE occupant landed here; ESPN's
    documented rule lets him stay.)"""

    player: str
    position: str | None
    espn_id: str | None
    injury_status: str | None
    # ESPN's own ``injured`` flag for this occupant, or None when it was not
    # captured. Carried because ``_ir_status`` is FLAG-FIRST since item 3.8a, so
    # the tag is NOT the evidence the verdict rested on — quoting it in the
    # violation and the REQUIRED ROSTER MOVE produced a page that said "ESPN lists
    # him OUT, not IR-eligible" three lines under a label saying OUT is exactly the
    # IR-eligible designation (audit fix).
    injured: int | None = None
    # ESPN's undroppable flag, read from the RAW row. ``check_legality`` stays pure
    # and independent of pricing, so this comes from the roster row and never from
    # a ``MarginalRow``. ``== 0`` is a refusal; None is "not captured".
    droppable: int | None = None


@dataclass(frozen=True)
class LegalityVerdict:
    """Whether ESPN will process ANY transaction for this roster, and why not.

    ``active_count`` recounts IR itself: non-IR players PLUS IR occupants who may no
    longer stay (no injury designation, or suspended — ESPN's occupancy rule), since
    the only fix for one is a move to the bench. Blocked when that exceeds
    ``active_slots`` or more than ``ir_slots`` sit in IR; an invalid occupant on a
    roster with room is a REQUIRED move that must come first (``ir_advisories``).
    """

    legal: bool
    active_count: int
    active_slots: int
    ir_count: int
    ir_slots: int
    ir_ineligible: tuple[IRIneligible, ...]
    ir_advisories: tuple[str, ...]   # required, NON-blocking roster moves (F1)
    ir_unverified: tuple[str, ...]   # IR occupants whose stay we could not verify (F7)
    violations: tuple[str, ...]
    reasons: tuple[str, ...]
    # Per-occupant: WHICH signal decided his eligibility — ESPN's own `injured`
    # flag, or the injury-tag proxy when the flag was not captured for him
    # (item 3.8a). A per-player line, never a blanket one: on a post-014 snapshot
    # most rows have the flag and one may not.
    ir_flag_notes: tuple[str, ...] = ()


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
    # ESPN's own undroppable list — the app REFUSES this drop (item 3.8a). A
    # FIELD, not a reason string: reasons only render under --reasons, and this
    # has to be on the default page where the drop board is read.
    undroppable: bool = False


@dataclass(frozen=True)
class ClaimRec:
    """One recommended add, paired with the DISTINCT drop it costs.

    ``kind`` is the claims-vs-FCFS split (KIND_WAIVER queued vs KIND_FREE_AGENT
    grab-fast). ``horizon`` comes straight from the swap matrix.

    TWO GAINS, deliberately (item 3.4b). ``gain`` is CONDITIONAL: what this add is
    worth once every claim listed above it in the chain has won. ``gain_alone`` is
    the standalone swap-matrix number: what it is worth on the roster as it stands
    today, i.e. what you get if the claims above it LOSE. At ``chain_rank == 1``
    the two are equal by construction (nothing is above it). ``chain_rank`` is the
    1-based position in the joint chain across claims + grabs; a STREAMING row is
    not part of the season-long chain and carries ``chain_rank = 0`` with
    ``gain_alone == gain``.
    """

    add: str
    add_position: str
    add_espn_id: str | None
    kind: str                       # KIND_WAIVER | KIND_FREE_AGENT
    gain: float                     # CONDITIONAL: assumes the lines above it won
    drop: str | None
    drop_position: str | None
    startable_this_week: bool
    horizon: int                    # 1 => streamed (this-week only), else season-long
    drop_unpriceable: bool
    waiver_rank: int | None         # own-team priority, CONTEXT only
    reasons: tuple[str, ...]
    gain_alone: float = 0.0         # standalone (the pre-3.4b number)
    chain_rank: int = 0             # 1-based; 0 => not in the season-long chain
    drop_season_over: bool = False  # the drop is priced at ZERO as SEASON OVER (3.4c)
    # The DROP's ESPN id (item 4.2b, B1). ``add_espn_id`` has been carried since
    # the item-3.4 audit fixed a duplicate-display-name mis-join; the drop side
    # was left on the name alone, and a journal or an archive that records "what
    # this claim cost" has the same two-players-one-name problem. Nothing printed
    # changes. ``None`` on a PURE ADD (there is no drop) and whenever the swap
    # itself carried no id.
    drop_espn_id: str | None = None
    # --- item 3.14 step 2: the other horizon, on every SHOWN streamed row -----
    # ``gain`` above is ONE WEEK for a streamed row. ``season_long_delta`` is the
    # same (drop, add) re-valued over the whole window on ``model_full``, and
    # ``break_even`` is ``1 - gain/cost``: how often you must get an equally good
    # replacement back for the one-week move to pay for its season-long cost.
    # ``break_even`` is None when there is nothing to break even on — the delta was
    # not computed, or it is POSITIVE (a season-long gain, which is not warned
    # about). DISCLOSURE ONLY: neither field touches an ordering.
    season_long_delta: float | None = None
    break_even: float | None = None


@dataclass(frozen=True)
class ChainRejection:
    """An add that is positive ALONE but measured at <= 0 once the chain above it
    has won (item 3.4b) — the flat-ridge reversal the chain exists to refuse.

    ``gain_after`` is always a MEASURED number. A candidate the chain already
    priced against the finished list is classified for free; a STALE one costs a
    valuation and only ``CHAIN_REJECTED_PRICED`` of those are spent. Everything
    the pass touched lands in exactly one bucket and the counts close:

      len(chain_rejected) + chain_measured_not_shown
        + len(chain_under_ranked) + len(chain_capped) + chain_not_repriced

    equals the number of leftover candidates. The same record type carries all
    three shown buckets; ``reason`` says which one it is and why.
    """

    add: str
    add_position: str
    drop: str | None
    drop_position: str | None
    gain_alone: float
    gain_after: float
    reason: str


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
    # --- item 3.4b: the chain ------------------------------------------------
    chain_gain: float = 0.0         # joint gain if every claim AND grab listed wins
    chain_rejected: tuple[ChainRejection, ...] = ()   # measured <= 0 after the chain
    chain_measured_not_shown: int = 0  # also measured <= 0, past the display cap
    chain_under_ranked: tuple[ChainRejection, ...] = ()  # measured > 0 AFTER the chain
    chain_capped: tuple[ChainRejection, ...] = ()    # blocked by POSITION_CAPS, not value
    chain_not_repriced: int = 0     # genuinely UNMEASURED against the finished chain
    chain_stop: str = ""            # STOP_* — why the chain ended
    # The position fence actually enforced (item 3.8a): this board's own guard
    # tightened by the LEAGUE's own roster limits, wherever the league's is tighter.
    position_caps: Mapping[str, int] = POSITION_CAPS
    # The LEAGUE's own limits behind those caps, or None when no settings row was
    # readable at this as_of — the difference between "both fences agree at 3" and
    # "only one fence was ever checked" (audit fix).
    league_limits: Mapping[str, int] | None = None
    # The IR ground-truth report for this as_of (item 3.8a). Always computed;
    # its note is added to ``notes`` ONLY when it has news.
    ir_rule: league_state.IRRuleReport | None = None
    # Item 4.7 part 3: the team-week swing every gain on this page is set against —
    # your projected starting lineup for the first window week, combined with the
    # lineup card's own arithmetic. DISPLAY ONLY: computed after the chain is
    # chosen, read by nothing but the formatter, and not written to the decision
    # archive (``decisions/capture.py`` writes an explicit field list).
    team_scale: units.WeeklyScale | None = None

    @property
    def blocked(self) -> bool:
        """The done-when predicate: an illegal roster blocks all claims."""
        return not self.legality.legal


# ------------------------------------------------------- the archive collector

# The as-of gated tables ONE waiver run reads, and who reads them. A decision
# archive that records only ``as_of`` records the GATE, not the DATA: a July
# projection pull and a November one both carry a valid ``knowable_as_of``, and
# nothing in the output tells them apart (the item-3.1b lesson — a stale read is
# Rule-1-invisible). ``base.resolved_vintage`` answers "which pull was in force"
# per table; see its docstring for what the number is and is not.
#
#   league_player_state  roster (get_player_state) + FA pool (get_free_agents)
#   league_teams         waiver_rank / transaction lock (get_team_state)
#   league_settings      the league's own limits + FAAB flag (get_league_settings)
#   projections          every point on the page (marginal -> valuation weekly_lines)
#   schedules            the week window, byes, partial-week checks
#   weekly_stats         the usage arm's differences and raw levels
#   snap_counts          snap share, both usage paths
#   injuries             the historical/backtest half of the injury arm
#
# NOT here, deliberately: ``players``. The crosswalk is read AT-NOW by design
# (``base.espn_by_gsis`` / ``name_by_gsis`` — an immutable identity map, never
# as-of gated), so a "vintage under this view" would be a different quantity
# wearing the same name. It is captured separately as ``crosswalk_vintage``.
ARCHIVE_VINTAGE_TABLES = (
    "league_player_state", "league_teams", "league_settings",
    "projections", "schedules", "weekly_stats", "snap_counts", "injuries",
)


@dataclass
class WaiverArtifacts:
    """PASSIVE collector for one ``build_waiver_plan`` run (item 4.2b, B1).

    ``ziggurat waivers`` produces stdout and NOTHING else: the swap matrix, the
    pool as priced, the candidate board, the chain's own bookkeeping and every
    reason string are discarded at process exit, so the Tuesday that produced a
    decision cannot be reconstructed afterwards — and a missed Tuesday is
    unrecoverable (``league_player_state`` accumulates forward only, item 3.1).
    Pass one of these and it comes back holding what the run already computed;
    the writer that turns it into a freeze is item 4.2b's ``decisions`` package,
    which depends on ``core``, never the other way round.

    It is a RECEIVER. It prices nothing, re-reads nothing on the decision path,
    changes no recommendation and no reason, and nothing in this module reads
    back out of it. ``collect=None`` — every production caller today — is
    byte-identical to not having it, pinned by test.

    Two things are NOT here because they cannot be honest:

    * a ``knowable_as_of``. A freeze is not a fact about the NFL; it is "what the
      tool said at ``as_of`` T". It records the gate it ran at and the vintages
      it resolved, and it is never re-read as an input to a later decision.
    * the swap matrix on the BLOCKED path. ``MarginalBoard.swaps`` is lazy and
      costs more than the rest of the scan; the blocked path never resolves it
      (there are no claims to plan), and a collector that touched it would make
      an archive of a refusal cost more than the refusal.
    """

    # --- the run's own inputs, as resolved -----------------------------------
    as_of: str = ""
    season: int | None = None
    team_id: int | None = None
    view: str = ""
    weeks_requested: tuple[int, ...] = ()
    claim_budget: int | None = None
    pool_limit: int | None = None
    source: str = ""
    today: str | None = None

    # --- what the run computed ----------------------------------------------
    plan: WaiverPlan | None = None
    roster_rows: tuple[dict, ...] = ()          # RAW, IR rows included, as read
    pool_rows: tuple[dict, ...] = ()            # the FA pool AS PRICED (post-filter)
    board: MarginalBoard | None = None
    swaps: tuple[SwapRow, ...] = ()             # the priced matrix, legal path only
    candidates: CandidateBoard | None = None
    evaluated: EvaluatedRows | None = None      # every EVALUATED row, flagged or not
    candidate_notes: Mapping[str, list[str]] = field(default_factory=dict)
    candidate_error: str | None = None          # the degrade note, when the load failed
    league_settings: dict | None = None
    open_slots: int | None = None

    # --- provenance ----------------------------------------------------------
    #: table -> the newest ``retrieved_as_of`` this view could see at ``as_of``.
    #: Measured AFTER the run's reads, so a pull that lands mid-run is reported
    #: as the newer vintage even though the earlier reads did not see it — the
    #: run is ~25 s and the cadence writes four times a day, so this is rare and
    #: it is stated rather than assumed away.
    vintages: Mapping[str, str | None] = field(default_factory=dict)
    #: ``players`` at ``MAX(retrieved_as_of)``, UNGATED — and that is the number
    #: that belongs here, not an oversight (item 4.2b audit, DC-8 examined and
    #: refuted). This field means "which pull priced this page", and the crosswalk
    #: accessors (``base.espn_by_gsis`` / ``name_by_gsis``) read ``players`` at-now
    #: with no as-of gate by design — see ARCHIVE_VINTAGE_TABLES above. Resolving
    #: it through ``base.resolved_vintage`` would record a vintage the run did NOT
    #: use: at a past ``as_of`` the gated answer is None (or an old day) while the
    #: crosswalk still resolved today's rows.
    crosswalk_vintage: str | None = None


def _capture_vintages(collect: WaiverArtifacts, conn, *, as_of, season, view) -> None:
    """Fill ``collect.vintages`` / ``crosswalk_vintage``. Called ONLY when a
    collector was passed (measured 0.15 s on the live 840 MB database, ~99 % of
    it the 2.5 M-row ``projections`` table), so the production path pays nothing.
    """
    collect.vintages = {
        table: base.resolved_vintage(conn, table, as_of=as_of, season=season, view=view)
        for table in ARCHIVE_VINTAGE_TABLES
    }
    row = conn.execute("SELECT MAX(retrieved_as_of) FROM players").fetchone()
    collect.crosswalk_vintage = row[0] if row else None


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

    because an occupant who may no longer STAY (ESPN's occupancy rule: no injury
    designation, or suspended — NOT QUESTIONABLE/DOUBTFUL, who may stay) can only
    leave by a move to the bench. The roster is BLOCKED iff ``active_count >
    active_slots`` OR ``ir_count > ir_slots`` (item 3.4 audit F5). The invalid
    occupant is ALREADY folded into ``active_count``, so he is NOT ALSO an
    independent illegality source — that made the old fix non-restorative
    (dropping bodies could never clear it). On a roster with room the move costs no
    drop, so the verdict stays "legal" for PLANNING and the move is surfaced as a
    REQUIRED roster move that must be made FIRST: ESPN marks the roster INVALID
    and processes nothing until he is off the slot.
    """
    ir_rows = [r for r in roster_rows if _slot(r) == "IR"]
    non_ir = [r for r in roster_rows if _slot(r) != "IR"]
    ineligible = tuple(
        IRIneligible(
            player=str(r.get("player") or r.get("espn_player_id") or "?"),
            position=r.get("position"),
            espn_id=(str(r["espn_player_id"]) if r.get("espn_player_id") is not None else None),
            injury_status=r.get("injury_status"),
            injured=r.get("injured"),
            droppable=r.get("droppable"),
        )
        for r in ir_rows
        if _ir_occupancy(r) == "INVALID"
    )
    unknown_rows = [r for r in ir_rows if _ir_occupancy(r) == "UNKNOWN"]
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
                f"{o.player} ({_ir_ineligible_because(o)}, so he counts on your "
                f"active roster)"
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

    # Required roster moves: an invalid occupant must leave the IR slot even when
    # the roster has room — and FIRST, because ESPN processes nothing while the
    # roster is invalid (F1; ESPN's documented rule since 2026-09-24).
    ir_advisories = tuple(
        f"REQUIRED ROSTER MOVE — DO THIS FIRST: move {o.player} out of your IR slot "
        f"to the bench — {_ir_ineligible_because(o)}; ESPN processes no claim or add "
        f"until he is off the IR slot (he counts against your "
        f"{structure.active_slots} active slots)"
        for o in ineligible
    )
    # Blank-status IR occupants: UNKNOWN, not a proof of illegality (F7).
    ir_unverified = tuple(
        f"could not verify that ESPN lets "
        f"{str(r.get('player') or r.get('espn_player_id') or '?')} stay in your IR slot "
        f"— {_ir_unknown_why(r)} — so we did NOT count him against your active "
        f"roster; confirm on the ESPN roster page"
        for r in unknown_rows
    )

    reasons: list[str] = []
    if legal and ineligible:
        # Room exists, so the move costs no drop — but ESPN marks the roster INVALID
        # until it is made, so "ESPN will process claims" would be false here.
        reasons.append(
            f"roster INVALID until you make the REQUIRED ROSTER MOVE below (it needs no "
            f"drop): after it, {active_count} of {structure.active_slots} active slots "
            f"used — until then ESPN processes no claim or add. The claims below "
            f"assume the move is made."
        )
    elif legal:
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
    # How each occupant's eligibility was decided — ESPN's own flag, or the proxy
    # (item 3.8a). Rule 6: a destructive instruction ships the evidence behind it.
    ir_flag_notes = tuple(_ir_occupant_reason(r) for r in ir_rows)

    reasons.extend(ir_advisories)
    reasons.extend(ir_unverified)
    if ir_count:
        # Surface the eligibility evidence and the still-unverified mechanism
        # whenever IR is in play (Rule 6).
        reasons.extend(ir_flag_notes)
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
        ir_flag_notes=ir_flag_notes,
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
        undroppable=row.undroppable,
        reasons=tuple(extra_reasons) + tuple(row.reasons),
    )


def _zero_drop_reslot(
    roster_rows: Sequence[Mapping], structure: RosterStructure
) -> tuple[list[str], list[str], list[str]] | None:
    """The PREFERRED, zero-drop fix (item 3.4 audit F1): can a pure re-slot make the
    roster legal? Returns ``(benched_names, moved_to_ir_names, moved_to_ir_reasons)``
    when it can, else ``None`` (a drop is genuinely required).

    Restorative by construction: it simulates the moves and only returns them when
    ``check_legality`` on the result is legal.

    1. Bench every IR occupant who may not STAY (occupancy INVALID, or UNKNOWN —
       a fix is only proposed on a blocked roster, and it must be restorative).
    2. Bench any still-excess IR occupants (ir_count > ir_slots).
    3. Seat IR-eligible active bodies into freed IR slots while the active roster is
       oversized — each seating frees one active slot.
    """
    rows = [dict(r) for r in roster_rows]
    benched: list[str] = []
    moved_to_ir: list[str] = []
    # WHY each destination player is IR-eligible, per player — the fix names him
    # "(IR-eligible)" and under flag-first that word can sit beside a visible
    # ACTIVE tag, so the evidence travels with the instruction (audit fix).
    moved_reasons: list[str] = []

    def name(r: Mapping) -> str:
        return str(r.get("player") or r.get("espn_player_id") or "?")

    for r in rows:
        if _slot(r) == "IR" and _ir_occupancy(r) != "OK":
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
        moved_reasons.append(_ir_reason(cand))

    if not check_legality(rows, structure=structure).legal:
        return None
    return benched, moved_to_ir, moved_reasons


def _cause_phrase(verdict: LegalityVerdict) -> str:
    """A one-line 'why you are over' naming the occupant(s) who may not stay on IR."""
    if verdict.ir_ineligible:
        names = "; ".join(
            f"{o.player} is in your IR slot with "
            f"{o.injury_status or 'no injury designation'} (may no longer stay on IR)"
            for o in verdict.ir_ineligible
        )
        return (
            f"your active roster is {verdict.active_count} of {verdict.active_slots} "
            f"because {names}, so he counts against your active roster until he is "
            f"moved to the bench"
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
    gain: float | None = None,
    gain_alone: float | None = None,
    chain_rank: int = 0,
    faab: int | None = 0,
) -> tuple[str, ...]:
    shown = swap.gain if gain is None else gain
    if is_pure_add:
        # The swap matrix's own lead sentence quotes the PAIRED swap's number and
        # names a drop this row does not make (item 3.4b audit). Rebuild it from
        # the number actually reported, and drop every inherited sentence that
        # names the phantom drop (marginal.py appends an unpriceable-drop one too).
        out = [
            f"add {swap.add} ({swap.add_position}) into your OPEN active slot: "
            f"{shown:+.1f} house pts over {swap.horizon_weeks} weeks — nobody is "
            f"dropped"
        ]
        out += [r for r in swap.reasons[1:] if swap.drop not in r]
    else:
        out = list(swap.reasons)
        # ``reasons[0]`` carries the STANDALONE gain (marginal.py wrote it). From
        # chain position 2 down that is NOT the headline number, so re-label it
        # rather than leave an unqualified figure above its own qualifier.
        if chain_rank > 1 and gain is not None and gain_alone is not None and out:
            out[0] = out[0].replace(
                f"{gain_alone:+.1f} house pts",
                f"{gain:+.1f} house pts once the {chain_rank - 1} move(s) ranked "
                f"above it win ({gain_alone:+.1f} on your roster as it stands today)",
            )
    # The chain sentence goes FIRST after the swap's own reasons, because it
    # changes what every number below it means (item 3.4b).
    if chain_rank > 1 and gain is not None and gain_alone is not None:
        out.append(
            f"CHAIN POSITION {chain_rank}: the {chain_rank - 1} add(s) ranked above it "
            f"in the chain are assumed to LAND (this line holds if "
            f"{_above_phrase(chain_rank)}) — some of them may be printed in the other "
            f"section, so read the #numbers, not the page order. "
            f"{gain:+.1f} is what this add is worth on the roster that leaves "
            f"you with. On your roster as it stands today it is worth "
            f"{gain_alone:+.1f} — so if one of those does not land (a claim lost on "
            f"priority, a free agent taken first), this one is still worth having."
        )
    elif chain_rank == 1:
        out.append(
            "CHAIN POSITION 1: nothing is assumed above this one, so this number is "
            "what the move is worth on your roster exactly as it stands today. (A "
            "FREE-AGENT GRAB anywhere in this chain is a click you make NOW, so if "
            "one is listed it lands before any claim clears overnight.)"
        )
    if kind == KIND_WAIVER:
        # Whether a claim COSTS anything is a league rule, and this sentence used
        # to assert it from a literal (audit fix). ``faab`` is ESPN's own
        # ``isUsingAcquisitionBudget``: 0 = inert (this league, measured), 1 = FAAB
        # is on, None = the settings row did not carry it.
        if faab:
            out.append(
                "WAIVERS claim — queue it, do not click. FAAB IS ON in this league, so "
                "this claim COSTS BID DOLLARS and NOTHING here tracks your budget: "
                "re-read `ziggurat league settings` before you queue it. Each claim you "
                "WIN also resets your waiver priority to worst-in-league."
            )
        elif faab is None:
            out.append(
                "WAIVERS claim — queue it, do not click, and note that ESPN's "
                "acquisition settings were NOT CAPTURED at this as-of, so whether the "
                "claim costs FAAB is UNKNOWN here. Each claim you WIN resets your "
                "waiver priority to worst-in-league."
            )
        else:
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
                f"WHO wins a fight, not which of YOUR claims to prefer. Your claims "
                f"are not sorted: they are a CHAIN, each one the best add remaining "
                f"given the ones numbered before it, so the number beside a line is "
                f"measured on a different roster than the line above it."
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
            "required, and the gain above is priced that way (your roster plus him, "
            "nobody removed)."
        )
    elif kind == KIND_WAIVER:
        out.append(
            f"pair: drop {swap.drop} for this add — your roster is full (16 active), "
            f"so this claim needs its OWN drop and a won claim fills the freed slot."
        )
    else:
        out.append(
            f"pair: drop {swap.drop} for this add — your roster is full (16 active), "
            f"so this add needs its OWN drop, made in the app at the same moment you "
            f"click him. This is one immediate transaction, not a queued claim."
        )
    if not is_pure_add:
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
    conn, *, as_of, season, view, today, collect: EvaluatedRows | None = None,
    history: EpisodeHistory | None = None,
) -> tuple[dict[str, list[str]], list[str], CandidateBoard | None]:
    """(espn_id -> opportunity-signal note(s), error_notes, the board) from item
    3.3, best-effort.

    THE SECOND RETURN IS A LIST, not one error string (item 4.2b audit, OPS-11).
    The board's OWN notes — the badge degrade, the partial-week warning — used to
    reach `ziggurat candidates` and die here, so the surface the Tuesday decision
    is actually made on printed a caveat-free page while the other one explained
    itself. Same shape as the 3.8A headline: two renderers of one scan disagreed.

    ``build_candidates`` needs a completed week; pre-season it raises
    ``NoCompletedWeek`` — the annotation is optional context, so we skip silently.
    ANY OTHER failure is a visible degrade, not a silent one (item 3.4 audit F10):
    it returns an error note so the plan can disclose that the signal load failed.

    The board itself is handed back for the archive (item 4.2b, B1) — it was
    already built, and only its ``reasons[0]`` per espn_id survives into the
    notes. ``collect`` is the evaluated-row collector, threaded straight through.
    Note the broad ``except`` below is a DEGRADE path for the decision, so a
    collector fault would be reported as a signal-load failure rather than
    crashing the plan; that is deliberate — nothing on the archive side may take
    the Tuesday page down — and it is why the collector does no work of its own.
    """
    notes: dict[str, list[str]] = {}
    try:
        board = build_candidates(conn, as_of=as_of, season=season, view=view,
                                 today=today, collect=collect, history=history)
    except NoCompletedWeek:
        return notes, [], None
    except Exception as exc:  # noqa: BLE001 — surfaced as a NOTE, never silently swallowed
        return notes, [
            f"opportunity signals UNAVAILABLE — the usage/injury signal load failed "
            f"({type(exc).__name__}: {exc}); the claims below carry no injury/usage "
            f"context. The claim ORDER is unaffected (it never used this column), "
            f"but this is a degrade, not 'no news' — verify manually."
        ], None
    legend = episode_legend(board.week, badged=board.badged)
    for c in board.rows:
        if not c.espn_id:
            continue
        head = c.reasons[0] if c.reasons else c.signal_kind
        # The header + legend land ONCE per claim, above that claim's evidence
        # rows, so a reader who sees a bullet has already read what it is not.
        bucket = notes.setdefault(str(c.espn_id), [])
        if not bucket:
            bucket.append(USAGE_EVIDENCE_HEADER)
            bucket.append(legend)
        bucket.append(f"  [{c.signal_kind}] {c.episode_tag or 'FIRST SEEN'}: {head}")
    return notes, list(board.notes), board


def _with_season_long(board: MarginalBoard, s: SwapRow) -> SwapRow:
    """Attach the SEASON-LONG re-valuation of a shown streamed row (item 3.14 §2).

    Swallows a failure rather than taking the page down: this is a DISCLOSURE, and
    a plan that refuses to print because one extra number could not be computed is
    worse than a plan that prints the number it has and says the other is missing.
    The renderer states "not computed" when it comes back None — it never prints a
    zero, which would read as "this costs you nothing".
    """
    if s.season_long_delta is not None:
        return s
    try:
        return replace(s, season_long_delta=board.season_long_delta(s))
    except (KeyError, ValueError):
        return s


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
    s: SwapRow, *, waiver_rank, team_count, candidate_notes, dup_names, is_pure_add: bool,
    gain: float | None = None, gain_alone: float | None = None, chain_rank: int = 0,
    faab: int | None = 0,
) -> ClaimRec:
    """One ``ClaimRec``. ``gain`` defaults to the standalone swap gain (the
    streaming lane, which is not part of the season-long chain); the chain passes
    the CONDITIONAL gain and the standalone one separately (item 3.4b)."""
    kind = _kind_of(s.add_status)
    espn_id, caveat = _add_espn_id(s, dup_names)
    shown = s.gain if gain is None else gain
    alone = s.gain if gain_alone is None else gain_alone
    break_even = break_even_reacquisition(shown, s.season_long_delta)
    return ClaimRec(
        add=s.add,
        add_position=s.add_position,
        add_espn_id=espn_id,
        kind=kind,
        gain=shown,
        drop=None if is_pure_add else s.drop,
        drop_position=None if is_pure_add else s.drop_position,
        startable_this_week=s.add_startable_this_week,
        horizon=s.horizon_weeks,
        drop_unpriceable=False if is_pure_add else s.drop_unpriceable,
        drop_season_over=False if is_pure_add else s.drop_season_over,
        waiver_rank=waiver_rank if kind == KIND_WAIVER else None,
        reasons=_claim_reasons(
            s, kind=kind, waiver_rank=waiver_rank, team_count=team_count,
            is_pure_add=is_pure_add,
            candidate_notes=candidate_notes.get(espn_id or "", ()),
            annotation_caveat=caveat,
            gain=shown, gain_alone=alone, chain_rank=chain_rank, faab=faab,
        ),
        gain_alone=alone,
        chain_rank=chain_rank,
        drop_espn_id=None if is_pure_add else s.drop_espn_id,
        season_long_delta=s.season_long_delta,
        break_even=break_even,
    )


@dataclass(frozen=True)
class _ChainResult:
    """``_select_claims``'s return — the chain plus everything needed to disclose
    how it ended (item 3.4b)."""

    claims: tuple[ClaimRec, ...]
    grabs: tuple[ClaimRec, ...]
    streaming: tuple[ClaimRec, ...]
    chain_gain: float
    chain_rejected: tuple[ChainRejection, ...]
    chain_not_repriced: int
    chain_stop: str
    evaluations: int
    chain_measured_not_shown: int = 0
    chain_under_ranked: tuple[ChainRejection, ...] = ()
    chain_capped: tuple[ChainRejection, ...] = ()
    streamed_hidden: int = 0        # positive streams cut by the claim_budget slice
    # Positive rows the swap MATRIX dropped at its row limit before any re-price
    # (item 3.4c review) — (streamed, season-long). Disclosed, never silent.
    matrix_cut_streamed: int = 0
    matrix_cut_season: int = 0
    phase_a_skipped: int = 0        # distinct adds phase A did not scan (top-K cap)
    phase_a_truncated: bool = False  # phase A stopped on cost, NOT on economics
    exhaust_reused: int = 0         # drain skips caused by a spent add/drop identity
    exhaust_capped: int = 0         # drain skips caused by POSITION_CAPS
    # The caps actually enforced — board guard tightened by the league's own
    # roster limits (item 3.8a). Carried so the notes name real numbers.
    position_caps: Mapping[str, int] = POSITION_CAPS
    # The LEAGUE's own limits behind those caps, or None when no settings row was
    # readable. Carried, not inferred: without it the notes cannot tell "both
    # fences agree at 3" from "no league limit was ever read" (audit fix).
    league_limits: Mapping[str, int] | None = None


def _caps_phrase(caps: Mapping[str, int], league_limits: Mapping[str, int] | None = None) -> str:
    """"max 3 QB, 8 RB, ..." from the caps that were actually enforced (3.8a).

    Built from the mapping rather than written as a literal: the sentence used to
    hard-code "max 3 QB, 3 TE, 8 RB, 8 WR, one K, one D/ST", which stops being
    true the moment a league limit is the tighter fence.

    Each entry names its SOURCE when the league's limits were read (audit fix):
    printing "max 1 DST" under a sentence that credits ESPN's positionLimits
    presented a module-only guard as a league rule, on a league whose real D/ST
    limit is 3.
    """
    if league_limits is None:
        return ", ".join(f"max {caps[p]} {p}" for p in sorted(caps))
    out = []
    for p in sorted(caps):
        own = POSITION_CAPS.get(p)
        lim = league_limits.get(p)
        if lim is not None and lim == caps[p] and own == caps[p]:
            src = "both fences agree"
        elif lim is not None and lim == caps[p]:
            src = "your league's limit"
        elif lim is not None:
            src = f"this board's guard; your league allows {lim}"
        else:
            src = "this board's guard; your league sets no limit here"
        out.append(f"max {caps[p]} {p} ({src})")
    return ", ".join(out)


def _roster_shape_mismatch(settings, structure: RosterStructure) -> str | None:
    """One note when ESPN's own roster shape no longer matches the one in force.

    Compared on the two TOTALS ``check_legality`` actually uses, which sidesteps
    the slot-label mismatch that has bitten this seam before ("D/ST" vs "DST"):
    every non-IR slot count summed, and the IR count.
    """
    slots = (settings or {}).get("lineup_slot_counts")
    if not isinstance(slots, dict) or not slots:
        return None
    try:
        active = sum(int(v) for k, v in slots.items() if str(k).upper() != "IR")
        ir = int(slots.get("IR", 0))
    except (TypeError, ValueError):
        return None
    if active == structure.active_slots and ir == structure.ir_slots:
        return None
    return (
        f"your league's roster SHAPE changed: ESPN now serves {active} active + {ir} "
        f"IR slot(s), but this plan's legality check, its open-slot count and the "
        f"valuation board behind it are all still using {structure.active_slots} "
        f"active + {structure.ir_slots} IR. Its legal/illegal verdict is stale until "
        f"that is reconciled — check `ziggurat league settings`."
    )


def _fences_phrase(league_limits: Mapping[str, int] | None) -> str:
    """How MANY fences actually applied — a fact about this snapshot, not a rule.

    The page used to assert "TWO fences apply" unconditionally. On any database
    with no ``league_settings`` row (every stored day before 2026-09-02, a
    backtest DB, a degraded settings row) exactly ONE applied, and the header
    credited ESPN for numbers it had never read (audit fix).
    """
    if league_limits is None:
        return ("Only ONE fence applied here: this board's own modelling guard (item "
                "3.2). Your LEAGUE's own roster limits were NOT CAPTURED in this "
                "snapshot, so ESPN's positionLimits were not checked — see "
                "`ziggurat league settings`.")
    return ("TWO fences apply and the tighter one binds: this board's own modelling "
            "guard (item 3.2) and your LEAGUE's own roster limits (ESPN "
            "positionLimits, ingested by item 3.8a).")


def _add_key(s: SwapRow) -> str:
    return s.add_espn_id if s.add_espn_id is not None else f"name:{s.add}"


def _drop_key(s: SwapRow) -> str:
    return s.drop_espn_id if s.drop_espn_id is not None else f"name:{s.drop}"


def _chain_key(s: SwapRow, gain: float, index: int) -> tuple:
    """The deterministic ladder, as a MIN-heap key (item 3.4b).

    ``drop_unpriceable`` leads it exactly as it led the pre-3.4b one-shot sort, so
    an unpriceable drop's upper-bound gain still cannot claim a slot ahead of a
    real pairing — and it matters MORE now: accepting one would price every
    conditional gain below it against a fictional post-chain roster. Then
    conditional gain desc; then STANDALONE gain desc, because near-ties are real
    (measured live: two rows for the same add, +1.0126 vs +1.0056, both rendering
    as "+1.0", differing only in which real player gets dropped) and breaking such
    a tie by drop NAME is arbitrary and invisible to the operator. ``index`` last,
    for a total order.
    """
    return (s.drop_unpriceable, -gain, -s.gain, s.add, s.drop, index)


def _select_claims(
    swaps: Sequence[SwapRow],
    *,
    board,
    claim_budget: int,
    waiver_rank: int | None,
    team_count: int | None,
    open_slots: int,
    candidate_notes: Mapping[str, list[str]],
    dup_names: set[str],
    position_counts: Mapping[str, int],
    faab: int | None = 0,
) -> _ChainResult:
    """SEQUENTIAL (chain) selection over the swap matrix (item 3.4b).

    Every ``SwapRow.gain`` prices its move as if it were the only one you make, so
    the pre-3.4b "rank them and print the top k" quoted each claim against a roster
    that stops existing the moment the claim above it wins. Measured 2026-09-01:
    three adds at +5.55 / +5.25 / +2.21 alone were worth +1.22 together, and the
    next day the module recommended reversing all three. See the module docstring.

    The algorithm is a LAZY GREEDY (CELF) at the board's reporting depth:

      * open active slots are filled FIRST as PURE ADDS (roster + add, nobody
        removed) — and are now PRICED as pure adds, where they used to quote the
        paired swap they were found through. Open-slots-first is an ASSUMPTION,
        disclosed in the plan notes: a swap that upgrades a starter can be worth
        more than filling the last bench spot;
      * then a max-heap over the seasonal rows keyed on last-known gain. Pop the
        top; skip it if its add or drop identity is already used, or if accepting
        it would breach ``POSITION_CAPS`` across the chain; if its estimate is
        STALE (priced against a shorter chain) re-price it against the accepted
        set and push it back; if it is FRESH and <= 0, STOP; else accept it,
        which staleness-marks everything else;
      * the chain also stops at ``claim_budget`` (a TOTAL cap over WAIVER +
        FREE_AGENT + UNKNOWN, stricter than the old per-bucket cap) and at
        ``CHAIN_EVAL_BUDGET`` valuations, and reports WHICH.

    ``position_counts`` is REQUIRED, not defaulted: it seeds the cross-chain
    ``POSITION_CAPS`` re-check with the roster's BASE counts, and an empty mapping
    silently disables that guard (every cap is >= 1, so `0 + 1 <= cap` always
    holds). A safety check whose default value is "off" is exactly the shape this
    repo's 3.1/3.1b audits kept finding, so the caller must say so on purpose.

    COST. Phase A is exhaustive per open slot and is fenced twice — a per-step
    scan cap (``PHASE_A_SCAN_TOP_K``) and its own share of ``CHAIN_EVAL_BUDGET``
    (``PHASE_B_EVAL_RESERVE`` valuations are held back for the swap lane). A
    phase-A cost stop is NEVER terminal: phase B is the cheap lazy half and is
    where swaps live, so a cost exhaustion in phase A is not evidence that no swap
    pays. Both truncations are disclosed in the plan notes.

    Lazy re-evaluation is exact when adds have diminishing returns (the usual
    case: two RBs into the same bench slot). For two adds that HELP each other (a
    starter and his handcuff) it is a heuristic that can rank the pair lower than
    it deserves, and the chain can stop before ever reaching the second half of
    such a pair. That is not swallowed: the rejection pass re-prices the leftovers
    against the FINISHED chain, and anything that comes back POSITIVE is reported
    in ``chain_under_ranked`` (with the stop note softened to match), never
    discarded.

    The STREAMED D/ST & K lane is UNTOUCHED (item 3.4 audit F4): same rows, same
    one-week ``model_now`` horizon, same ``claim_budget`` slice, ``chain_rank=0``.
    It is deliberately outside the chain — the board cannot re-price a one-week
    move against a season-long roster, and ``value_after`` raises rather than try.
    """
    budget = max(int(claim_budget), 0)
    # The caps THIS board was filtered with (item 3.8a) — this board's own guard
    # already tightened by the league's own positionLimits. Read from the board so
    # the matrix filter, the per-step re-check below and the refusal reason cannot
    # name three different numbers. Bound before the early returns so every
    # _ChainResult reports the caps that were really in force.
    position_caps = board.position_caps
    league_limits = getattr(board, "league_limits", None)
    streamed = [s for s in swaps if _is_streamed(s)]
    seasonal = [s for s in swaps if not _is_streamed(s)]

    stream_sorted = sorted(streamed, key=lambda s: (-s.gain, s.add, s.drop))
    stream_recs = tuple(
        _swap_rec(
            _with_season_long(board, s),
            waiver_rank=waiver_rank, team_count=team_count,
            candidate_notes=candidate_notes, dup_names=dup_names, is_pure_add=False,
            faab=faab,
        )
        # ITEM 3.14 STEP 2: the ORDER is unchanged (still -gain, add, drop — a
        # one-week lane sorted on its one-week number) and the slice is unchanged.
        # The season-long re-valuation happens INSIDE the slice, on the rows this
        # page will actually print, because each one costs a roster valuation.
        for s in stream_sorted[:budget]
    )
    streamed_hidden = max(len(stream_sorted) - len(stream_recs), 0)
    cut_streamed, cut_season = getattr(board, "swaps_cut", (0, 0))
    cut_fields = {"matrix_cut_streamed": cut_streamed, "matrix_cut_season": cut_season}
    if budget <= 0:
        # "You asked me not to look" is NOT "there were no candidates" — the note
        # this feeds must never claim the pool holds nothing worth having.
        return _ChainResult(
            (), (), stream_recs, 0.0, (), 0, STOP_BUDGET, 0,
            streamed_hidden=streamed_hidden, position_caps=position_caps,
            league_limits=league_limits, **cut_fields,
        )
    if not seasonal:
        return _ChainResult(
            (), (), stream_recs, 0.0, (), 0, STOP_NO_CANDIDATES, 0,
            streamed_hidden=streamed_hidden, position_caps=position_caps,
            league_limits=league_limits, **cut_fields,
        )

    caps = dict(position_counts)
    calls = 0

    def value_after(sw=(), pure=()):
        nonlocal calls
        calls += 1
        return board.value_after(tuple(sw), pure_adds=tuple(pure))

    base = value_after()
    accepted: list[tuple[SwapRow, float, float, bool]] = []   # row, gain, alone, pure
    accepted_swaps: list[SwapRow] = []
    accepted_pure: list[SwapRow] = []
    used_adds: set[str] = set()
    used_drops: set[str] = set()
    stop: str | None = None          # set ONLY by a real event; resolved at the end
    phase_a_settled = False          # phase A proved nothing more is worth adding
    phase_a_truncated = False        # phase A stopped on COST, not on economics
    phase_a_skipped = 0              # distinct adds phase A never scanned
    exhaust_reused = 0
    exhaust_capped = 0
    phase_a_ceiling = max(CHAIN_EVAL_BUDGET - PHASE_B_EVAL_RESERVE, 1)

    def cap_ok(s: SwapRow, pure: bool) -> bool:
        cap = position_caps.get(s.add_position)
        if cap is None:
            return True
        held = caps.get(s.add_position, 0)
        after = held + 1
        if not pure and s.drop_position == s.add_position:
            after -= 1
        # Same rule as ``marginal._scan``: this board's own guard forbids RAISING
        # a count above it, never keeping one the roster already holds; a LEAGUE
        # limit stays absolute (``marginal.cap_allows_keep``, 2026-10-01).
        return after <= cap or (
            after <= held and cap_allows_keep(s.add_position, cap, league_limits))

    def apply_caps(s: SwapRow, pure: bool) -> None:
        caps[s.add_position] = caps.get(s.add_position, 0) + 1
        if not pure and s.drop_position:
            caps[s.drop_position] = caps.get(s.drop_position, 0) - 1

    # --- phase A: open slots, priced AS pure adds -----------------------------
    # Exhaustive per step, not lazy: a standalone SWAP gain is a LOWER bound on the
    # same add's PURE-add gain (adding without dropping is never worse), and a lazy
    # heap needs an UPPER bound to stay exact. So it is bounded by a disclosed
    # top-K scan and by its own share of the valuation ceiling instead. Zero-cost
    # on a full roster (open_slots == 0), which is the live case.
    while len(accepted_pure) < open_slots and len(accepted) < budget:
        reps: dict[str, SwapRow] = {}
        cap_excluded = False
        for s in seasonal:
            k = _add_key(s)
            if k in used_adds:
                continue
            if not cap_ok(s, True):
                # Legal as a SAME-POSITION swap (the drop offsets the add) but not
                # as a pure add. Phase A cannot see it, so phase A's "a pure add
                # dominates the same swap" argument does not cover it.
                if cap_ok(s, False):
                    cap_excluded = True
                continue
            cur = reps.get(k)
            if cur is None or (-s.gain, s.add, s.drop) < (-cur.gain, cur.add, cur.drop):
                reps[k] = s
        if not reps:
            break                       # no pure-add candidate left; swaps may still pay
        ranked = sorted(reps.values(), key=lambda r: (-r.gain, r.add, r.drop))
        scan = ranked[:PHASE_A_SCAN_TOP_K]
        if len(ranked) > len(scan):
            phase_a_skipped = max(phase_a_skipped, len(ranked) - len(scan))
        prev = value_after(accepted_swaps, accepted_pure)
        best: tuple[tuple, SwapRow, float] | None = None
        over_budget = False
        for s in scan:
            if calls >= phase_a_ceiling:
                over_budget = True
                break
            g = value_after(accepted_swaps, accepted_pure + [s]) - prev
            key = (-g, s.add, s.drop)
            if best is None or key < best[0]:
                best = (key, s, g)
        if over_budget:
            # A COST stop, never an economic one, and never terminal: phase B has
            # its own reserve and is where the swaps live.
            phase_a_truncated = True
            break
        if best is None:
            break
        _key, s, g = best
        if g <= 0.0:
            # The best PURE add is worth nothing, and a pure add is never worse
            # than the same add as a swap (a drop cannot raise V) — so no swap can
            # pay either. That argument covers ONLY the adds phase A could see: a
            # same-position swap at a capped position is legal in phase B and was
            # filtered out of `reps` above, and a top-K scan may not have reached
            # the winner. Settle here only when neither exclusion applied.
            if cap_excluded or len(ranked) > len(scan):
                break                   # leave stop None so phase B decides
            stop = STOP_NONPOSITIVE
            phase_a_settled = True
            break
        alone = value_after((), [s]) - base
        accepted_pure.append(s)
        used_adds.add(_add_key(s))
        apply_caps(s, True)
        accepted.append((s, g, alone, True))

    if stop is None and len(accepted) >= budget:
        stop = STOP_BUDGET

    # --- phase B: the lazy greedy over swaps ---------------------------------
    est = [s.gain for s in seasonal]
    # ``at[i]`` = the chain length ``est[i]`` was measured against. A candidate is
    # FRESH iff at[i] == len(accepted). Seeded at 0 because the rank-1 conditional
    # gain IS the standalone gain by construction: _reprice_swaps prices each row
    # as value_at_depth(roster - drop + add) - value_at_depth(roster), which is
    # exactly value_after([row]) - value_after(()).
    at = [0] * len(seasonal)
    heap = [(_chain_key(seasonal[i], est[i], i), i) for i in range(len(seasonal))]
    heapq.heapify(heap)

    while not phase_a_settled and stop is None and len(accepted) < budget:
        prev = value_after(accepted_swaps, accepted_pure)
        chosen: tuple[int, SwapRow, float] | None = None
        over_budget = False
        pass_reused = 0
        pass_capped = 0
        while heap:
            _key, i = heapq.heappop(heap)
            s = seasonal[i]
            if _add_key(s) in used_adds or _drop_key(s) in used_drops:
                pass_reused += 1
                continue
            if not cap_ok(s, False):
                pass_capped += 1
                continue
            if at[i] == len(accepted):
                chosen = (i, s, est[i])
                break
            if calls >= CHAIN_EVAL_BUDGET:
                over_budget = True
                break
            g = value_after(accepted_swaps + [s], accepted_pure) - prev
            est[i], at[i] = g, len(accepted)
            heapq.heappush(heap, (_chain_key(s, g, i), i))
        if over_budget:
            stop = STOP_EVAL_BUDGET
            break
        if chosen is None:
            # The heap drained. WHY it drained is two different facts and the note
            # must not assert the wrong one: a spent add/drop identity is
            # bookkeeping about what the chain already took, a POSITION_CAPS
            # refusal is a modelling guard that has nothing to do with the chain.
            exhaust_reused, exhaust_capped = pass_reused, pass_capped
            stop = STOP_EXHAUSTED
            break
        i, s, g = chosen
        if g <= 0.0:
            stop = STOP_NONPOSITIVE
            heapq.heappush(heap, (_chain_key(s, g, i), i))   # keep it for the rejects
            break
        accepted_swaps.append(s)
        used_adds.add(_add_key(s))
        used_drops.add(_drop_key(s))
        apply_caps(s, False)
        accepted.append((s, g, s.gain, False))

    if stop is None:
        stop = STOP_BUDGET if len(accepted) >= budget else STOP_EXHAUSTED

    # --- the joint number, measured (never accumulated) -----------------------
    # value_after(all) - base, NOT base += g. The telescoping identity
    # (chain_gain == sum of the conditionals) is the whole point of the design, and
    # accumulating is exactly how you make that identity true while the numbers drift.
    chain_gain = (value_after(accepted_swaps, accepted_pure) - base) if accepted else 0.0

    # --- the leftovers, each in exactly ONE disclosed bucket -------------------
    # Nothing this pass MEASURES may leave it unreported. A candidate that comes
    # back positive against the finished chain is a complement the lazy search
    # under-ranked, and the pre-3.4b build showed such a row (last, with its
    # upper-bound caveat); silently dropping it would be a regression AND would
    # falsify the stop note printed above it.
    rejected: list[ChainRejection] = []
    under_ranked: list[ChainRejection] = []
    capped: list[ChainRejection] = []
    measured_not_shown = 0
    not_repriced = 0
    stale_priced = 0
    if accepted and stop != STOP_BUDGET:
        prev = value_after(accepted_swaps, accepted_pure)
        remaining: dict[str, tuple[int, SwapRow]] = {}
        for i, s in enumerate(seasonal):
            if s.gain <= 0.0:                       # not "positive alone"
                continue
            k = _add_key(s)
            if k in used_adds or _drop_key(s) in used_drops:
                continue
            cur = remaining.get(k)
            if cur is None or _chain_key(s, s.gain, i) < _chain_key(cur[1], cur[1].gain, cur[0]):
                remaining[k] = (i, s)
        order = sorted(remaining.values(),
                       key=lambda t: _chain_key(t[1], t[1].gain, t[0]))
        for i, s in order:
            fresh = at[i] == len(accepted)
            if fresh:
                g = est[i]                          # already measured — free
            elif stale_priced < CHAIN_REJECTED_PRICED and calls < CHAIN_EVAL_BUDGET:
                g = value_after(accepted_swaps + [s], accepted_pure) - prev
                est[i], at[i] = g, len(accepted)
                stale_priced += 1
            else:
                not_repriced += 1
                continue
            if not cap_ok(s, False):
                # A roster-cap refusal, not a valuation. Saying "winning him too
                # would undo part of them" here would blame the chain for a cap.
                cap = position_caps.get(s.add_position)
                held = caps.get(s.add_position, 0)
                which_fence = describe_cap(
                    s.add_position, cap,
                    league_limit=(league_limits or {}).get(s.add_position),
                    limits_read=league_limits is not None,
                )
                capped.append(ChainRejection(
                    add=s.add, add_position=s.add_position,
                    drop=s.drop, drop_position=s.drop_position,
                    gain_alone=s.gain, gain_after=g,
                    reason=(
                        f"{s.add} is not available to you once the moves above win: "
                        f"your roster would hold {held + 1} at {s.add_position} and "
                        f"the binding limit is {cap}. That is {which_fence}. It is "
                        f"NOT a judgement about his value ({g:+.1f} after the chain, "
                        f"{s.gain:+.1f} today) — he becomes possible again only if a "
                        f"move above him does not land."
                    ),
                ))
                continue
            if g > 0.0:
                under_ranked.append(ChainRejection(
                    add=s.add, add_position=s.add_position,
                    drop=s.drop, drop_position=s.drop_position,
                    gain_alone=s.gain, gain_after=g,
                    reason=(
                        f"{s.add} measures {g:+.1f} AFTER the {len(accepted)} move(s) "
                        f"above have won ({s.gain:+.1f} on today's roster). The search "
                        f"ranked him on his standalone number, which put him below the "
                        f"point where it stopped, so he was never offered — that is "
                        f"the shape of an add that HELPS one of the moves above rather "
                        f"than competing with it. Consider queueing him after them."
                        + (f" His drop side ({s.drop}) has no usable projection, so "
                           f"that number is an UPPER BOUND — confirm {s.drop} manually "
                           f"before dropping him." if s.drop_unpriceable else "")
                    ),
                ))
                continue
            if len(rejected) < CHAIN_REJECTED_PRICED:
                rejected.append(ChainRejection(
                    add=s.add, add_position=s.add_position,
                    drop=s.drop, drop_position=s.drop_position,
                    gain_alone=s.gain, gain_after=g,
                    reason=(
                        f"add {s.add} ({s.add_position}) <- drop {s.drop} "
                        f"({s.drop_position or '-'}) is worth {s.gain:+.1f} on your "
                        f"roster as it stands today, but only {g:+.1f} once the "
                        f"{len(accepted)} move(s) above have won. Two things move that "
                        f"number and the report does not split them: the drop left "
                        f"for him is {s.drop}, whom those moves make more expensive "
                        f"to lose, and he now competes with what you just added. "
                        f"ESPN processes "
                        f"EVERY claim you queue in the same overnight batch, so queue "
                        f"him INSTEAD OF a move above him, never in addition: if both "
                        f"win you get the {g:+.1f}, which is the reversal this list "
                        f"exists to refuse."
                    ),
                ))
            else:
                measured_not_shown += 1

    # --- assemble, IN CHAIN ORDER --------------------------------------------
    recs = [
        _swap_rec(
            s, waiver_rank=waiver_rank, team_count=team_count,
            candidate_notes=candidate_notes, dup_names=dup_names, is_pure_add=pure,
            gain=g, gain_alone=alone, chain_rank=idx + 1, faab=faab,
        )
        for idx, (s, g, alone, pure) in enumerate(accepted)
    ]
    # The two tuples are a split by ACTION (queue overnight vs click now), not by
    # order: `chain_rank` is the order, it runs across BOTH, and every rendered
    # line carries it (item 3.4b audit).
    claims = tuple(r for r in recs if r.kind == KIND_WAIVER)
    grabs = tuple(r for r in recs if r.kind in (KIND_FREE_AGENT, KIND_UNKNOWN))
    return _ChainResult(
        claims=claims, grabs=grabs, streaming=stream_recs,
        chain_gain=chain_gain, chain_rejected=tuple(rejected),
        chain_not_repriced=not_repriced, chain_stop=stop, evaluations=calls,
        chain_measured_not_shown=measured_not_shown,
        chain_under_ranked=tuple(under_ranked), chain_capped=tuple(capped),
        streamed_hidden=streamed_hidden, phase_a_skipped=phase_a_skipped,
        phase_a_truncated=phase_a_truncated,
        exhaust_reused=exhaust_reused, exhaust_capped=exhaust_capped,
        position_caps=position_caps, league_limits=league_limits, **cut_fields,
    )


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
    collect: WaiverArtifacts | None = None,
    history: EpisodeHistory | None = None,
) -> WaiverPlan:
    """The waiver plan (item 3.4). Rule 1: ``as_of`` keyword-only, no default;
    ``view`` threaded into every accessor.

    1. Fetch the RAW roster (IR rows included) via ``get_player_state``.
    2. ``check_legality`` FIRST. If illegal: reslot occupants who may not stay on IR
       IR->BE, price ONE ``build_board`` scan to name the forced drop (guarding
       ``WeekResolutionError`` so a missing week window does not crash), and
       RETURN ``blocked`` with ``claims=()`` — the done-when.
    3. If legal: ONE ``build_board`` scan -> drop board (``board.ranked``) +
       claims (``board.swaps``, split WAIVER/FREE_AGENT, each with a distinct
       drop), annotated with ``build_candidates`` opportunity signals joined on
       ``espn_id``.

    The season-long claims are a CHAIN, not a ranked list (item 3.4b): claim k is
    priced against the roster after claims 1..k−1 have won, and the list ENDS where
    the next add would be worth <= 0 given the ones above it. ``claim_budget`` is
    now a TOTAL cap over claims + grabs (it used to cap each bucket separately);
    the streaming lane keeps its own separate slice. ``plan.chain_gain`` is the
    joint number and equals the sum of the per-claim conditional gains by
    construction. See ``_select_claims``.

    ``history`` (item 4.2b, B3) is the decision archive's flag history, threaded
    into ``build_candidates`` for the NEW / REPEAT badge on the opportunity-signal
    bullets. It reaches the CONTEXT only: those bullets are appended after the
    chain has been selected, so nothing about them can move a claim.

    ``collect`` (item 4.2b, B1) is an optional passive ``WaiverArtifacts``: pass
    one and it comes back holding what this run already computed, for the
    per-Tuesday freeze. It changes no recommendation, no reason and no order, and
    ``None`` is byte-identical to not having it (pinned by test).
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

    if collect is not None:
        collect.as_of = normalize_as_of(as_of).isoformat()
        collect.season, collect.team_id, collect.view = int(season), own_team_id, str(view)
        collect.weeks_requested = resolved_weeks
        collect.claim_budget, collect.pool_limit, collect.source = (
            claim_budget, pool_limit, source)
        collect.today = None if today is None else str(today)
        collect.roster_rows = tuple(roster_rows)

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

    # Item 3.8a: re-run the IR ground-truth comparison on THIS as_of, and speak
    # ONLY when it has news — a divergence, an injury designation this league has
    # never served before, an IR occupant whose ESPN flag says he does not belong
    # there, or a snapshot where the flag was not captured. A line that says
    # "nothing changed" on every plan is how the operator learns to skip the one
    # report that matters (the operator-attention contract).
    ir_rule = league_state.ir_rule_check(
        conn, as_of=as_of, season=season, own_team_id=own_team_id, view=view)
    if ir_rule.has_news:
        notes.append(ir_rule.headline)

    # Item 3.8a audit: the ONE settings change that alters what this module owes
    # had no daily surface at all — `settings_verdicts` was reachable only from
    # `ziggurat league settings`, a command the cadence never runs. FAAB being ON
    # (or its flag not being captured) makes every "queue liberally, claims are
    # free" sentence on this page wrong, so it speaks HERE. Silent when ESPN says
    # the budget is inert, which is the measured state of this league.
    settings = league_state.get_league_settings(
        conn, as_of=as_of, season=season, view=view)
    if collect is not None:
        collect.league_settings = None if settings is None else dict(settings)
    faab_flag = (settings or {}).get("is_using_acquisition_budget")
    faab_note = league_state.faab_verdict(settings)
    if faab_note is not None:
        notes.append(faab_note)
    # The league's own roster SHAPE is ingested and printed by `league settings`
    # but consumed by nothing: `check_legality` runs on DEFAULT_ROSTER. Reconcile
    # by DISCLOSURE rather than by deriving the structure — RosterStructure also
    # drives replacement levels and the weekly seater, and re-shaping those from a
    # live snapshot is a far larger blast radius than the gap (audit fix).
    shape_note = _roster_shape_mismatch(settings, roster_structure)
    if shape_note:
        notes.append(shape_note)

    if not verdict.legal:
        # REFUSE to plan claims. Offer fixes in PREFERENCE ORDER (item 3.4 audit F1):
        # (a) the ZERO-DROP IR-move if a pure re-slot restores legality — PRIMARY;
        # (b) the forced DROP of the lowest-value body — only when the ACTIVE roster
        #     is oversized (a body-drop cannot clear a pure IR-overcount), demoted to
        #     secondary whenever (a) applies.
        ir_move = _zero_drop_reslot(roster_rows, roster_structure)
        ir_move_fix: tuple[str, ...] = ()
        if ir_move is not None:
            benched, moved_to_ir, moved_reasons = ir_move
            if moved_to_ir:
                ir_move_fix = (
                    f"BEST FIX (no drop): move {'; '.join(benched)} out of your IR slot "
                    f"to the bench and move {'; '.join(moved_to_ir)} (IR-eligible) into "
                    f"your IR slot — this makes you legal with NO drop.",
                    # the per-player evidence for that "(IR-eligible)" — the same
                    # `_ir_reason` the occupant lines carry (Rule 6, audit fix)
                    *moved_reasons,
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
        # Default to the module guard for the branches that build NO board (an
        # IR-only overcount, or a WeekResolutionError): those enforced nothing, so
        # the plan must not report caps that came from somewhere else.
        blocked_caps: Mapping[str, int] = POSITION_CAPS
        blocked_limits: Mapping[str, int] | None = None
        if verdict.active_count > roster_structure.active_slots:
            try:
                board = build_board(
                    conn, as_of=as_of, season=season, roster=reslotted,
                    weeks=weeks, last_week=last_week, roster_structure=roster_structure,
                    pool_limit=pool_limit, source=source, view=view, today=today,
                )
                if collect is not None:
                    # The BOARD only — never ``board.swaps``: the matrix is lazy
                    # and costs more than the whole rest of the scan, and this
                    # path plans no claims to price (see WaiverArtifacts).
                    collect.board = board
                # The board's own disclosures (no projections knowable, the
                # static-roster caveat, a tighter league limit, a degraded settings
                # row) qualify the forced drop this branch is about to NAME — they
                # were being dropped on the one page that recommends a destructive,
                # un-workaroundable move (audit fix).
                notes.extend(board.notes)
                blocked_caps = board.position_caps
                blocked_limits = board.league_limits
                drop_board = tuple(_drop_rec(r) for r in board.ranked)
                blocked_weeks = tuple(board.weeks)   # the window that PRICED the drop (F11)
                # THE SECOND DROP PATH (item 3.8a). This picks a BOARD row, not a
                # swap, so the matrix fence does not cover it — and this is the one
                # instruction the operator cannot work around: obeying a drop ESPN
                # refuses leaves the roster illegal and every claim blocked.
                droppable_ranked = [r for r in board.ranked if not r.undroppable]
                if droppable_ranked:
                    forced_drop = _drop_rec(
                        droppable_ranked[0],
                        extra_reasons=(_forced_drop_reason(
                            droppable_ranked[0], verdict, secondary=bool(ir_move_fix)),),
                    )
                    # ONLY the rows actually passed over. The board is ordered
                    # cheapest-drop-first and ESPN's undroppable list is a list of
                    # elite players, so an unfiltered sweep told the operator the
                    # tool had considered dropping his two BEST players — about
                    # rows that were never candidates, in a sentence whose verb did
                    # not agree with its subject (audit fix). Every row before the
                    # chosen one is undroppable by construction.
                    idx = board.ranked.index(droppable_ranked[0])
                    skipped = [r.player for r in board.ranked[:idx]]
                    if skipped:
                        n = len(skipped)
                        notes.append(
                            f"{n} cheaper drop(s) on your board could not be named "
                            f"({', '.join(skipped)}): "
                            f"{'they are' if n > 1 else 'he is'} on ESPN's UNDROPPABLE "
                            f"list, so the app refuses "
                            f"{'those drops' if n > 1 else 'that drop'} and naming "
                            f"{'one' if n > 1 else 'him'} would leave you illegal. The "
                            f"drop named above is the cheapest one ESPN will accept."
                        )
                elif board.ranked:
                    notes.append(
                        "ESPN will REFUSE every drop this board can price: all "
                        f"{len(board.ranked)} priceable player(s) on your roster are on "
                        f"ESPN's undroppable list. The fix has to be an IR move, or a "
                        f"drop of a player this board could not price (he is not on the "
                        f"drop board below) — check the app."
                    )
                else:
                    notes.append(
                        "could not name a single forced drop: every priceable player is "
                        "unvalued at this as-of (this board could price nobody) — verify "
                        "manually and drop your lowest-value body to reach "
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
        # The ineligible IR occupant himself is an explicit drop/keep candidate (F1)
        # — and this is the FIFTH place the system can name a drop (item 3.8a audit).
        # It names one in PROSE rather than through a swap or a board row, so the
        # matrix fence and the forced-drop fence both miss it, and ESPN's undroppable
        # list is composed of exactly the elite players most likely to occupy IR.
        for o in verdict.ir_ineligible:
            if o.droppable == 0:
                notes.append(
                    f"{o.player} is on ESPN's UNDROPPABLE list, so dropping HIM is not "
                    f"an option either — the app refuses it. The fix has to be the IR "
                    f"move above, or another body."
                )
            else:
                notes.append(
                    f"you may instead DROP {o.player} himself (the occupant who may "
                    f"no longer stay on IR) — dropping him also frees the active slot "
                    f"he now counts against."
                )
        if verdict.ir_count > roster_structure.ir_slots and ir_move_fix == ():
            droppable_names = [o.player for o in verdict.ir_ineligible if o.droppable != 0]
            fenced = [o.player for o in verdict.ir_ineligible if o.droppable == 0]
            names = "; ".join(droppable_names) or "one IR occupant"
            notes.append(
                f"your IR slot holds {verdict.ir_count} players (max "
                f"{roster_structure.ir_slots}); drop or bench "
                f"{verdict.ir_count - roster_structure.ir_slots} of them ({names})."
                + (f" {', '.join(fenced)} can only be BENCHED, not dropped — ESPN's "
                   f"undroppable list refuses that drop." if fenced else "")
            )

        blocked_plan = WaiverPlan(
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
            position_caps=blocked_caps,
            league_limits=blocked_limits,
            ir_rule=ir_rule,
        )
        if collect is not None:
            collect.plan = blocked_plan
            _capture_vintages(collect, conn, as_of=as_of, season=season, view=view)
        return blocked_plan

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

    evaluated = EvaluatedRows() if collect is not None else None
    candidate_notes, candidate_errs, candidate_board = _candidate_notes_by_espn(
        conn, as_of=as_of, season=season, view=view, today=today, collect=evaluated,
        history=history,
    )
    notes.extend(candidate_errs)
    # `collect.candidate_error` stays the LOAD-FAILURE note and only that: the
    # board's own notes now ride in the same list (OPS-11), and a board that
    # BUILT is not a degrade — `_candidates_meta` reads this field to say WHY the
    # candidate half is absent, so a mere caveat here would mislabel a good one.
    candidate_err = (candidate_errs[0]
                     if candidate_board is None and candidate_errs else None)
    if collect is not None:
        collect.pool_rows = tuple(pool_rows)
        collect.board, collect.swaps = board, tuple(swaps)
        collect.candidates, collect.evaluated = candidate_board, evaluated
        collect.candidate_notes = candidate_notes
        collect.candidate_error = candidate_err

    open_slots = max(roster_structure.active_slots - verdict.active_count, 0)
    if collect is not None:
        collect.open_slots = open_slots
    chain = _select_claims(
        swaps, board=board, claim_budget=claim_budget, waiver_rank=waiver_priority,
        team_count=team_count, open_slots=open_slots,
        candidate_notes=candidate_notes, dup_names=dup_names,
        position_counts=board.roster_position_counts, faab=faab_flag,
    )
    claims, grabs, streaming = chain.claims, chain.grabs, chain.streaming

    notes.extend(board.notes)
    if open_slots:
        notes.append(
            f"you have {open_slots} OPEN active slot(s): the top add(s) below are PURE "
            f"ADDS — no drop is required to add them, and they are PRICED as pure adds "
            f"(your roster plus him), not quoted from the paired swap they were found "
            f"through. Open slots are filled FIRST; that ordering is an assumption — a "
            f"swap that upgrades a starter can be worth more than filling the last "
            f"bench spot."
        )
    if claim_budget <= 0:
        # A degenerate INPUT, not a measurement. Nothing was priced, so the plan
        # must not state an economic conclusion about the pool.
        notes.append(
            f"--claim-budget is {claim_budget}, so NO claim was searched for or "
            f"priced. This says nothing about the free-agent pool — re-run "
            f"`ziggurat waivers --claim-budget <n>` with a positive budget to find "
            f"out whether an add would help."
        )
    elif not claims and not grabs:
        # Judge the season-long CHAIN on its own: a positive one-week stream in the
        # lane below does not make "nothing improves this roster" true, and its
        # presence must not suppress the one sentence that IS the answer today.
        if streaming:
            notes.append(
                "no SEASON-LONG add in the free-agent pool improves this roster at "
                "this as-of — hold your roster on the claim side. The STREAMING "
                "row(s) below are one-week D/ST or K moves, priced on this week "
                "alone and deliberately outside the chain: they are alternatives "
                "for one slot, not a queue."
            )
        else:
            notes.append(
                "no add in the free-agent pool improves this roster at this as-of — "
                "hold your roster. Queuing a claim is free, but every add here would "
                "cost a drop worth more than the add."
            )
    notes.extend(_chain_notes(chain, claim_budget=claim_budget,
                              weeks=len(board.weeks)))

    plan = WaiverPlan(
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
        chain_gain=chain.chain_gain,
        chain_rejected=chain.chain_rejected,
        chain_measured_not_shown=chain.chain_measured_not_shown,
        chain_under_ranked=chain.chain_under_ranked,
        chain_capped=chain.chain_capped,
        chain_not_repriced=chain.chain_not_repriced,
        chain_stop=chain.chain_stop,
        position_caps=board.position_caps,
        league_limits=board.league_limits,
        ir_rule=ir_rule,
        team_scale=_team_week_scale(board),
    )
    if collect is not None:
        collect.plan = plan
        _capture_vintages(collect, conn, as_of=as_of, season=season, view=view)
    return plan


def _team_week_scale(board: MarginalBoard) -> units.WeeklyScale | None:
    """The swing in your team's weekly score that every gain here is set against
    (item 4.7 part 3). DISPLAY ONLY — it reads the board after the chain is chosen
    and nothing reads it back.

    Your projected starting lineup for the FIRST window week — the week the
    streaming lane decides and the week ``ziggurat lineup`` seats — seated by the
    shared seater on the board's own points and availability (byes and hard-outs
    excluded, exactly as the board prices them), then combined with
    ``lineup_support.lineup_sigma``: the lineup card's own arithmetic. A roster
    player with no forecast that week is left out rather than seated at 0.0, the
    same discipline the lineup card applies. None when no lineup can be seated.
    """
    if not board.weeks:
        return None
    week = board.weeks[0]
    model = board.model
    keys = [k for k in board.roster_keys
            if model.available(k, week) and model.points(k, week) != 0.0]
    if not keys:
        return None
    fill = fill_lineup(keys, model.positions, {k: model.points(k, week) for k in keys},
                       roster=model.rs)
    # Slot order, not the ``starters`` frozenset: a set's order is PYTHONHASHSEED's.
    starters = [(model.positions[k], model.points(k, week), model.entries[k].team)
                for _label, k in fill.slots if k is not None]
    if not starters:
        return None
    return units.WeeklyScale(
        sigma=lineup_sigma(starters),
        basis=(f"your projected week-{week} starting lineup, combined the way "
               f"`ziggurat lineup` combines it for its win prob"),
        provenance=sigma_provenance(),
    )


def _chain_notes(chain: _ChainResult, *, claim_budget: int, weeks: int) -> list[str]:
    """The plan notes that explain the chain (item 3.4b, Rule 6).

    Two facts are kept apart on purpose. "The next claim is worth <= 0" is an
    ECONOMIC conclusion. "The matrix had no legal drop left" is not — measured
    2026-09-02, the live swap matrix carried 179 rows but only THREE distinct drop
    identities, so a chain can end for bookkeeping reasons that say nothing about
    whether a fourth claim would have helped. Reporting both as one sentence would
    state a conclusion nobody measured.

    The same discipline applies to every other sentence here, which is why several
    of them are conditional: the chain-ordering paragraph is emitted only when a
    chain exists; the "short list is the answer" verdict is withheld whenever the
    rejection pass MEASURED a leftover as still positive; the exhaustion sentence
    names whichever of the two drain causes actually fired; and the not-re-priced
    sentence quotes how many rows were really re-priced, never the display cap.
    """
    n = len(chain.claims) + len(chain.grabs)
    out: list[str] = []
    if n:
        out.append(
            f"these {n} add(s) are priced as a CHAIN, in the NUMBERED order shown on "
            f"each line (#1, #2, ...): each line's gain assumes every LOWER-numbered "
            f"line landed. The numbers run across BOTH the WAIVER CLAIMS and "
            f"FREE-AGENT GRABS sections, so #2 can be printed under #3 — act on them "
            f"in NUMBER order, not page order. The 'alone' number beside a line is "
            f"what it is worth if the lines above it do NOT land. Each won claim "
            f"resets your waiver priority to worst-in-league, so spend it on the "
            f"best target."
        )
        if chain.grabs:
            out.append(
                "one or more lines below are FREE-AGENT GRABS: you click those NOW, "
                "while the waiver claims clear in tonight's batch — so a grab lands "
                "before every claim whatever its chain number. The chain number is a "
                "PRICING position, not a schedule. Each line's 'alone' number is what "
                "it is worth on your roster exactly as it stands today, i.e. if NONE "
                "of the lines above it land — grabs included."
            )
    if chain.chain_stop == STOP_NONPOSITIVE and not n:
        out.append(
            "nothing was added because the best add available is worth nothing or "
            "less than the player it would cost you — a measured verdict, not a "
            "search that ran out."
        )
    elif chain.chain_stop == STOP_NONPOSITIVE:
        if chain.chain_under_ranked:
            out.append(
                "the search stopped because the next candidate it reached was worth "
                "nothing or less once these had won — but it then MEASURED "
                f"{len(chain.chain_under_ranked)} further add(s) as still positive "
                f"against the finished list (see below). So this is where the search "
                f"ended, not a verdict that nothing else pays."
            )
        else:
            out.append(
                "the list ENDS where it does because the next-best add is worth "
                "nothing or less once these have won — a SHORT list is the answer "
                "here, not a truncation, and no candidate this search re-priced came "
                "back positive."
            )
    elif chain.chain_stop == STOP_BUDGET and n:
        out.append(
            f"the list stopped at a claim ceiling of {claim_budget}, NOT because the "
            f"next add was worthless. If the LAST line above is still clearly "
            f"positive, re-run `ziggurat waivers --claim-budget <n>` deeper. (The "
            f"flag lives on `ziggurat waivers`; the Wednesday briefing composes at a "
            f"fixed budget of 3 and has no such flag.)"
        )
    elif chain.chain_stop == STOP_EXHAUSTED:
        if chain.exhaust_capped and not chain.exhaust_reused:
            out.append(
                f"the list ended because every remaining add would put you over the "
                f"binding limit for its position "
                f"({_caps_phrase(chain.position_caps, chain.league_limits)}). "
                + _fences_phrase(chain.league_limits)
                + " That is a LIMIT, not a measurement that the next claim was "
                  "worthless."
            )
        elif chain.exhaust_capped:
            out.append(
                f"the list ended on bookkeeping, NOT on value: of the pairs left, "
                f"{chain.exhaust_reused} reuse a player already spent above and "
                f"{chain.exhaust_capped} would put you over the binding limit for "
                f"their position "
                f"({_caps_phrase(chain.position_caps, chain.league_limits)}). "
                + _fences_phrase(chain.league_limits)
            )
        elif n:
            out.append(
                "the list ended because the priced add/drop pairs ran out (every "
                "remaining pair reuses a player already spent above), NOT because the "
                "next claim was measured as worthless — those two are different facts "
                "and only the first one is bookkeeping."
            )
        else:
            out.append(
                "no add/drop pair was left to price at all — every candidate was "
                "blocked before it could be valued. That is bookkeeping, NOT a "
                "measurement that an add would not have helped."
            )
    elif chain.chain_stop == STOP_EVAL_BUDGET:
        out.append(
            f"the chain stopped EARLY: pricing it hit this module's ceiling of about "
            f"{CHAIN_EVAL_BUDGET} roster valuations. The claims above are real, but "
            f"there may be more worth having — this is a cost limit, not a verdict."
        )
    elif chain.chain_stop == STOP_NO_CANDIDATES and not n:
        out.append(
            "no season-long add/drop pair priced positive at all at this as-of, so "
            "there was nothing to chain. Nothing was refused; there was nothing to "
            "refuse."
        )
    if chain.phase_a_truncated:
        out.append(
            f"you have OPEN active slots and pricing the pure adds for them hit this "
            f"module's cost fence ({CHAIN_EVAL_BUDGET - PHASE_B_EVAL_RESERVE} of "
            f"{CHAIN_EVAL_BUDGET} valuations reserved for that half). The swap search "
            f"below still ran on its own reserve — this is a cost limit on the "
            f"open-slot half only, not a verdict on it."
        )
    if chain.phase_a_skipped:
        out.append(
            f"the open-slot search priced only the top {PHASE_A_SCAN_TOP_K} distinct "
            f"adds per slot (by standalone gain) and skipped {chain.phase_a_skipped} "
            f"more, to stay inside a few seconds. That ordering is a HEURISTIC — a "
            f"standalone swap gain is a LOWER bound on the same add's value as a pure "
            f"add — so treat the skipped ones as unmeasured, not as rejected."
        )
    if n:
        out.append(
            "how the next claim is chosen: the one that adds the most GIVEN the ones "
            "above it, re-priced on the same estimator the drop board uses. The search "
            "skips re-pricing a candidate whose last-known value already lost — exact "
            "when two adds compete for the same lineup spot (the usual case), and for "
            "two adds that HELP each other (a starter and his own backup) it can rank "
            "the pair lower than it deserves and stop before reaching the second half "
            "of the pair. Anything it later measures as still positive is named "
            "separately below rather than dropped."
        )
    if chain.chain_measured_not_shown:
        out.append(
            f"{chain.chain_measured_not_shown} further add(s) were also MEASURED at "
            f"<= 0 once the moves above win and are not listed individually — the "
            f"{CHAIN_REJECTED_PRICED} closest calls are shown. They are refusals, not "
            f"open questions."
        )
    if chain.chain_not_repriced:
        priced = (
            len(chain.chain_rejected) + chain.chain_measured_not_shown
            + len(chain.chain_under_ranked) + len(chain.chain_capped)
        )
        how = (
            f"the top {priced} were re-priced; the report stops there to stay inside "
            f"a few seconds"
            if priced else
            f"NONE of them were — pricing hit this module's ceiling of about "
            f"{CHAIN_EVAL_BUDGET} roster valuations first"
        )
        out.append(
            f"{chain.chain_not_repriced} further add(s) are positive on their own but "
            f"carry NO number against the finished chain ({how}) — treat them as "
            f"unmeasured, not as rejected."
        )
    hidden = chain.streamed_hidden + chain.matrix_cut_streamed
    if hidden:
        out.append(
            f"{hidden} further one-week STREAM(s) are positive but not "
            f"shown: that lane is sliced by --claim-budget too"
            + (f" ({chain.matrix_cut_streamed} of them were dropped earlier, at the "
               f"swap matrix's row limit, before any re-pricing — a search-estimator "
               f"count)" if chain.matrix_cut_streamed else "")
            + ". It sits OUTSIDE the "
            f"chain — those rows are ranked alternatives for one slot, best first, so "
            f"the top one is still the best of them. `ziggurat stream` is the full "
            f"lane and the place that decision belongs."
        )
    if chain.matrix_cut_season:
        out.append(
            f"{chain.matrix_cut_season} season-long add/drop pair(s) that the cheap "
            f"search priced positive were cut at the swap matrix's row limit before "
            f"the chain could see them: UNMEASURED, not rejected. If the list above "
            f"ended because 'the priced add/drop pairs ran out', some of these were "
            f"never offered to it."
        )
    return out


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


def _weeks_phrase(n: int) -> str:
    """ONE rendering of a horizon for the whole report (Rule 6).

    Three spellings of the same number in one page ("1 wks" beside "this week"
    beside "1 wk(s)") is exactly the kind of thing a novice cannot smell, and the
    one-week window is reachable on any plain in-season run in week 17.
    """
    return "this week" if n == 1 else f"{n} wks"


def _above_phrase(chain_rank: int) -> str:
    """The moves a chained line assumes have landed, as ONE spelling (Rule 6).

    ``#1 lands`` at position 2; ``#1-#3 land`` at position 4. A range of one
    (``#1-#1``) is not a spelling a novice should have to decode.
    """
    above = chain_rank - 1
    return "#1 lands" if above == 1 else f"#1-#{above} land"


def _claim_line(rec: ClaimRec) -> str:
    startable = "" if rec.startable_this_week else "  (cannot start this week)"
    horizon = _weeks_phrase(rec.horizon)
    # An unpriceable drop is flagged in the DEFAULT view (item 3.4 audit F6),
    # mirroring the inline "(cannot start this week)" flag.
    unpriced = "  [drop UNPRICED — verify before dropping]" if rec.drop_unpriceable else ""
    if rec.drop_season_over:
        # Same register, DEFAULT view (item 3.4c review): the zero is a labelled
        # hypothesis, and short-term IR looks the same for a few days.
        unpriced += "  [drop priced at ZERO as SEASON OVER — check the news first]"
    # The chain RANK, on every chained line in the DEFAULT view (item 3.4b audit).
    # The two sections are split by ACTION (queue overnight vs click now), so the
    # chain runs ACROSS them and the printed order is NOT the chain order whenever
    # a grab interleaves. Without the number the operator cannot recover it, while
    # the notes tell him to act in chain order. Streaming rows are rank 0 and print
    # exactly as they did before.
    rank = f"#{rec.chain_rank} " if rec.chain_rank > 0 else ""
    # Both numbers, in the DEFAULT view, from chain position 2 down (item 3.4b).
    # At rank 1 the conditional gain IS the standalone one, so a suffix is noise.
    chained = (
        f" if {_above_phrase(rec.chain_rank)} ({rec.gain_alone:+.1f} alone)"
        if rec.chain_rank > 1 else ""
    )
    if rec.drop is None:            # pure add — open slot, no drop (item 3.4 audit F17)
        return (
            f"  {rank}add {rec.add} ({rec.add_position})  (open slot — no drop)   "
            f"{rec.gain:+.1f} pts / {horizon}{chained}{startable}"
        )
    return (
        f"  {rank}add {rec.add} ({rec.add_position})  <-  drop {rec.drop} "
        f"({rec.drop_position or '-'})   {rec.gain:+.1f} pts / {horizon}{chained}"
        f"{startable}{unpriced}"
    )


def _pos_display(position: str | None) -> str:
    return "D/ST" if (position or "").upper() == "DST" else (position or "player")


def _horizons_lines(rec: ClaimRec) -> list[str]:
    """BOTH horizons for one streamed row — item 3.14 step 2, DEFAULT view.

    A streamed ``gain`` is ONE WEEK. What it never said is what the same move does
    to the rest of the season, and on the live board that number is not small: the
    item's own measurement is a "+1.57 this week" stream costing 26.55 house points
    over weeks 1-17 if the dropped defense is never reacquired — a 94.09%
    break-even reacquisition probability the page did not print.

    A POSITIVE season-long delta says so and is NOT warned about (measured: one
    such swap is a season-long gain of 4.27). ``None`` prints as UNKNOWN, never as
    zero: "this costs you nothing" is the one thing a missing number must not say.
    """
    if rec.horizon != 1:
        return []
    who = rec.drop or "the player you drop"
    if rec.season_long_delta is None:
        return [
            f"      BOTH HORIZONS: the season-long cost of this swap could NOT be "
            f"computed at this as-of. That is UNKNOWN, not zero — treat the "
            f"{rec.gain:+.1f} above as a one-week number with an unmeasured tail."
        ]
    delta = rec.season_long_delta
    if delta >= 0.0:
        return [
            f"      BOTH HORIZONS: {rec.gain:+.1f} this week AND {delta:+.1f} over the "
            f"rest of the season — this one is a season-long GAIN as well, so there "
            f"is nothing to reacquire and no break-even to clear."
        ]
    if rec.break_even is not None and rec.break_even <= 0.0:
        return [
            f"      BOTH HORIZONS: {rec.gain:+.1f} this week against {delta:+.1f} over "
            f"the rest of the season — this week alone already covers the whole "
            f"season-long cost, so it pays even if you never get {who} back."
        ]
    return [
        f"      BOTH HORIZONS: {rec.gain:+.1f} this week, but {delta:+.1f} over the "
        f"rest of the season if you never get {who} back.",
        f"      You would need to get an equally good {_pos_display(rec.drop_position)} "
        f"back {rec.break_even:.0%} of the time for this to be worth it.",
    ]


#: Item 3.14 step 3. The one shape the waiver page could not price at all: a
#: SECOND D/ST rented for a week into a bench slot while the season-long one is
#: kept. `marginal.POSITION_CAPS` caps D/ST at 1, so the pair never reaches the
#: swap matrix — the tool printed nothing, not even a refusal, and the operator
#: hand-priced it in the Week-2 journal (hypothesis H-wk02-1).
#:
#: THE DESIGN DECISION, recorded where it is read: this is a DISCLOSURE, not a
#: flag and not a new "rotation slot" concept in the model. The cap is not a bug —
#: item 3.2 measured that an uncapped board makes a second defense the top add on
#: 15 of 16 rosters, because the projection feed is a flat SEASON RATE and prefers
#: a second D/ST mechanically. Relaxing the cap to rank this move would put that
#: artefact back on the board for every roster in order to price one hypothesis.
#: So the page PRICES the shape out of numbers it already holds (the streamed
#: row's one-week gain and the drop board's season-long body value) and refuses to
#: rank it.
ROTATION_SLOT_HEADER = (
    "ROTATION SLOT — NOT A RECOMMENDATION, priced for the record "
    "(item 3.14 step 3; hypothesis H-wk02-1)"
)


def _rotation_slot_lines(plan: "WaiverPlan") -> list[str]:
    """Price the second-D/ST rental the swap matrix cannot contain (item 3.14 §3).

    Prints only when the MODULE guard is what forbids the move and the LEAGUE does
    not: a cap of 1 that the league also imposes is a rule, and pricing a move the
    app would refuse is the failure mode item 3.8a fixed everywhere else.
    """
    # The shape priced here is ADDING a second defense, so it needs a roster that
    # holds exactly ONE. With two already held the rotation slot exists, and since
    # item 3.4c the matrix prices a D/ST-for-D/ST swap directly — the streaming
    # rows above ARE the price, and this block's "filtered out before anything is
    # priced" would be false directly beneath them (item 3.4c review). And the
    # streamed row must itself ADD a defense: a "keep the D/ST, ADD Braelon Allen
    # into a bench slot" rental is not this shape at all.
    held_dst = sum(1 for d in plan.drop_board if d.position == "DST")
    if held_dst != 1:
        return []
    stream = next(
        (r for r in plan.streaming
         if r.drop_position == "DST" and r.add_position == "DST" and r.drop), None)
    if stream is None:
        return []
    caps = plan.position_caps or {}
    limits = plan.league_limits
    module_cap = caps.get("DST")
    league_cap = None if limits is None else limits.get("DST")
    if module_cap != 1 or league_cap is None or league_cap < 2:
        # Either the league caps D/ST at one too (then it is a RULE and there is
        # nothing to disclose), or no settings row was readable — in which case we
        # do not know it is legal, and item 3.8a's lesson is that an unverified
        # legality claim is worse than silence.
        return []
    spent = {r.drop for r in list(plan.claims) + list(plan.fcfs_grabs) if r.drop}
    droppable = [
        d for d in plan.drop_board
        if not d.unpriceable and not d.undroppable and d.horizon_weeks != 1
        and d.position not in STREAMED_POSITIONS
    ]
    body = next((d for d in droppable if d.player not in spent), None)
    weeks = len(plan.weeks) or 1

    out = ["", ROTATION_SLOT_HEADER]
    out.append(
        f"  The tool CANNOT rank this move: our own `marginal.POSITION_CAPS` caps "
        f"D/ST at {module_cap}, so a SECOND defense is filtered out of the swap "
        f"matrix before anything is priced. This league's own limit is {league_cap} "
        f"(`ziggurat league settings`), so the block is OUR modelling guard, not a "
        f"rule ESPN would enforce."
    )
    out.append(
        # The drop board on this same page says, in `marginal.py`'s own words, "a
        # second DST is never considered as an add". That stays TRUE and stays
        # printed — it is a statement about the ranked SEARCH. Two renderers of one
        # scan appearing to contradict each other is the failure the item-3.8A
        # audit paid for, so the reconciliation is stated here rather than left for
        # the reader to notice.
        "  This does NOT contradict the drop board below, which says a D/ST is only "
        "ever considered as an add in exchange for one you hold: that is true of the "
        "ranked SEARCH and stays true. This block is a PRICE, not a ranking."
    )
    out.append(
        f"  THE SHAPE: keep {stream.drop} (the D/ST you already hold), ADD "
        f"{stream.add} into a bench slot for week-only use, and pay for the body "
        f"you displace."
    )
    out.append(
        f"    - the rental is worth {stream.gain:+.1f} house pts THIS WEEK over "
        f"starting {stream.drop} — the SAME one-week number as the streaming row "
        f"above, from the same one-week model."
    )
    if body is None:
        out.append(
            "    - but no priceable, droppable, season-long bench body is available "
            "at this as-of, so there is nothing to pay with and the shape cannot be "
            "completed. That is the answer for today, not a missing feature."
        )
    else:
        cost = body.marginal_points
        chain_note = (
            f" (The chain above already spends {len(spent)} of your droppable bodies; "
            f"this is the cheapest one it does not.)" if spent else ""
        )
        if cost <= 0.0:
            out.append(
                f"    - the body it displaces is {body.player} ({body.position}), priced "
                f"at {cost:+.1f} house pts over {weeks} wks on the drop board — at or "
                f"BELOW what a replacement-level free agent is worth, so giving him up "
                f"costs nothing measurable in points." + chain_note
            )
            out.append(
                "    - so the price of this rental is NOT points: it is a roster slot "
                "and the attention of re-deciding the slot every week. Those are the "
                "two things this page cannot put a number on."
            )
        else:
            out.append(
                f"    - the body it displaces is {body.player} ({body.position}), worth "
                f"{cost:+.1f} house pts over {weeks} wks — his own season-long drop-board "
                f"value, i.e. what you give up PERMANENTLY." + chain_note
            )
            need = math.ceil(cost / stream.gain) if stream.gain > 0 else None
            out.append(
                f"    - DIFFERENT HORIZONS — do not subtract them. One week of rental "
                f"against a whole season of body."
                + (f" At this week's rate the rotation must beat {stream.drop} in about "
                   f"{need} week(s) like this one to cover him." if need else "")
            )
    out.append(
        f"  Unlike the SWAP above, this keeps {stream.drop}, so that row's "
        f"season-long cost does not apply here — you pay with a bench body instead."
    )
    out.append(
        "  WHY THIS IS NOT A RECOMMENDATION: item 3.2 measured that an UNCAPPED board "
        "makes a second defense the top add on 15 of 16 rosters, because the "
        "projection feed is a flat SEASON RATE and prefers a second D/ST "
        "mechanically, not because a matchup is good — and the one-week number above "
        "comes from that same flat feed. The cap is load-bearing PRECISELY BECAUSE "
        "the feed is flat. Settle this with a journalled experiment (H-wk02-1 carries "
        "a Week-5 falsifying trigger), never by editing the constant."
    )
    return out


def _rejection_line(r: ChainRejection) -> str:
    """One refusal, in the SAME labelled vocabulary the claim lines use.

    The bare ``{add} <- {drop}`` form the item shipped with is the only ``<-`` on
    the page whose two sides are unlabelled, and it is the one place the two roles
    are hardest to tell apart (both are real NFL players, and only the operator's
    own roster knowledge says which is which). The shipped briefing summarizer
    measurably inverted it.
    """
    if r.drop is None:
        return f"  add {r.add} ({r.add_position})   {r.gain_alone:+.1f} alone / {r.gain_after:+.1f} after"
    return (
        f"  add {r.add} ({r.add_position})  <-  drop {r.drop} ({r.drop_position or '-'})   "
        f"{r.gain_alone:+.1f} alone / {r.gain_after:+.1f} after"
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

    # The IR disclosure is load-bearing for a destructive action, so it renders
    # UNCONDITIONALLY whenever an IR occupant is present (item 3.4 audit F2) — and
    # since item 3.8a it is SPLIT, because only half of it is still unverified.
    # Line 1 per occupant: which signal decided (ESPN's own flag, or the proxy).
    # Line 2: the IR-SLOT MECHANISM, which no roster in this league has exercised.
    def ir_disclosure() -> list[str]:
        if v.ir_count <= 0:
            return []
        out = [f"  NOTE: {note}" for note in v.ir_flag_notes]
        out.append(
            f"  NOTE: {IR_FIX_MODEL_LABEL} Before you drop or bench anyone, check the "
            "ESPN roster page: the \"Add to IR\" button lists who may enter the slot "
            "(the MOVE button did not offer IR to anyone, observed 2026-09-24)."
        )
        return out

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
        # When NEITHER fix could be named, the page used to print an alarm, no
        # fix, and no reason — refuse-and-propose became refuse-and-say-nothing on
        # the one page where ESPN is blocking every transaction (audit fix). The
        # explanation is in the notes; promote it into the FIX block so it cannot
        # read as an omission.
        if not plan.ir_move_fix and plan.forced_drop is None:
            out.append("")
            out.append("  NO FIX THIS TOOL CAN NAME — why:")
            for note in plan.notes:
                out.append(f"    {note}")
        else:
            # The blocked branch returns before the legal path's notes loop, so
            # every disclosure routed into notes — the IR RULE CHECK headline, the
            # undroppable-skip note, the board's own caveats, item 3.4's
            # alternative-fix option — was unreachable text (audit fix).
            for note in plan.notes:
                out.append(f"! {note}")
        out.append("")
        # Item 4.7 part 4: the forced drop above is a projected number too.
        out.append(f"  {units.UNITS_LEGEND}")
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

    # --- item 4.7 part 4: which KIND of number follows, before the first one ---
    out.append("")
    out.append(units.UNITS_LEGEND)

    # --- the chain header (item 3.4b) — DEFAULT view, not behind --reasons ----
    # The joint number and the refused reversals are the whole point of the item:
    # a novice reading three claims each worth "+5" will queue all three, and the
    # only place the tool can say "these three are worth +1.2 together" is here.
    scale_cited = False
    if plan.claims or plan.fcfs_grabs:
        span = _weeks_phrase(len(plan.weeks)) if plan.weeks else "the window"
        out.append("")
        out.append(
            f"IF EVERY CLAIM AND GRAB LISTED WINS: {plan.chain_gain:+.1f} pts over "
            f"{span} (priced in NUMBER order — each line assumes the lower-numbered "
            f"lines landed, and their gains add up to this total; the 'alone' number "
            f"is what a claim is worth if those do not land)"
        )
        # Item 4.7 part 3: the scale of the number the cadence reads aloud, per
        # week BEFORE it meets a one-week swing. A different line, deliberately:
        # the one above is quoted by CLAUDE.md and pinned by tests.
        if plan.weeks:
            out.append("  " + units.season_gain_sentence(
                plan.chain_gain, len(plan.weeks), plan.team_scale))
            scale_cited = plan.team_scale is not None
    out.append("")

    # --- waiver claims --------------------------------------------------------
    # The ACTION prints before every refusal and cap notice: the operator opens
    # this page to learn what to queue, and the moves the refusals are measured
    # against ("once the moves above win") are then literally above them.
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

    # --- what the chain measured and did NOT recommend (item 3.4b) -------------
    if plan.chain_rejected:
        out.append("")
        shared = {r.drop for r in plan.chain_rejected}
        if len(plan.chain_rejected) > 1 and len(shared) == 1 and None not in shared:
            drop = plan.chain_rejected[0]
            out.append(
                f"REFUSED — positive ALONE, measured at <= 0 once the moves above win "
                f"({len(plan.chain_rejected)}). All {len(plan.chain_rejected)} need the "
                f"SAME drop ({drop.drop}, {drop.drop_position or '-'}): the moves above "
                f"already spent your other droppable bodies, so this is ONE fact about "
                f"that drop, not {len(plan.chain_rejected)} independent judgements. "
                f"Queue one INSTEAD OF a move above it, never in addition — ESPN "
                f"processes every queued claim in the same batch."
            )
        else:
            out.append(
                f"REFUSED — positive ALONE, measured at <= 0 once the moves above win "
                f"({len(plan.chain_rejected)}). Each is a fallback ONLY: queue it "
                f"INSTEAD OF a move above it, never in addition — ESPN processes every "
                f"queued claim in the same batch and can grant both."
            )
        for r in plan.chain_rejected:
            out.append(_rejection_line(r))
            if reasons:
                out.append(f"      - {r.reason}")
    if plan.chain_under_ranked:
        out.append("")
        out.append(
            f"POSITIVE AFTER THE CHAIN, but the search reached them too late to rank "
            f"({len(plan.chain_under_ranked)}) — these HELP the moves above rather "
            f"than compete with them, so consider queueing them after the list:"
        )
        for r in plan.chain_under_ranked:
            out.append(_rejection_line(r))
            if reasons:
                out.append(f"      - {r.reason}")
    if plan.chain_capped:
        out.append("")
        out.append(
            f"BLOCKED BY A POSITION LIMIT, not by value ({len(plan.chain_capped)}) — "
            + _fences_phrase(plan.league_limits)
            + f" In force here: "
              f"{_caps_phrase(plan.position_caps, plan.league_limits)}."
        )
        for r in plan.chain_capped:
            out.append(_rejection_line(r))
            if reasons:
                out.append(f"      - {r.reason}")
    out.append("")

    # --- streaming (this week only — item 3.5's lane) (item 3.4 audit F4) ------
    if plan.streaming:
        out.append(f"STREAMING (this week only — item 3.5's lane, NOT a season-long "
                   f"claim)  ({len(plan.streaming)})")
        if any(r.season_long_delta is not None for r in plan.streaming):
            # What the BOTH HORIZONS numbers are, said once rather than per row.
            # The season-long half is priced off the SAME flat season-rate feed the
            # one-week half is (item 3.2), so it is a comparison of rates, not a
            # forecast — and a break-even percentage printed without that is a
            # confident-looking number a novice cannot discount.
            out.append(
                "  * BOTH-HORIZONS NOTE: the season-long figure on each row is priced "
                "off the same FLAT SEASON-RATE projection feed as the weekly one (item "
                "3.2 measured D/ST as the only position with real week-to-week movement, "
                "~12% CV). It is a comparison of season RATES — the right order of "
                "magnitude for 'what does giving him up cost' — and it is NOT a forecast "
                "of what the defense you drop will actually score."
            )
        if any(r.drop_position == "DST" for r in plan.streaming):
            # Item 3.14: the three mandatory sentences, VERBATIM from
            # `streaming.DST_CARD_SENTENCES` — this page and `ziggurat stream` print
            # one text, because two surfaces paraphrasing one disclosure is how they
            # start disagreeing about what the number cost.
            for sentence in DST_CARD_SENTENCES:
                out.append(f"  * {sentence}")
        # Item 4.7 part 3: one week each, so no per-week conversion — and the
        # prior's bracket only if the chain line above did not already print it.
        out.append("  * " + units.week_gain_sentence(
            max(r.gain for r in plan.streaming), plan.team_scale, cite=not scale_cited))
        for rec in plan.streaming:
            out.append(_claim_line(rec))
            # BOTH horizons, in the DEFAULT view (item 3.14 step 2): a one-week gain
            # printed alone is the number that reads as free.
            out.extend(_horizons_lines(rec))
            if reasons:
                out.extend(f"      - {r}" for r in rec.reasons)
        out.extend(_rotation_slot_lines(plan))
        out.append("")

    # --- drop board -----------------------------------------------------------
    # Legend reconciles the sign vs the claims section (item 3.4 audit F18): a
    # drop-board number is the player's OWN value (what you GIVE UP by dropping him);
    # a claim's +gain is the NET of adding someone better in his place.
    out.append("DROP BOARD — each number is what you'd GIVE UP by dropping that player "
               "(most droppable first)")
    out.append("  legend: a claim's +gain above already subtracts this drop-cost; the "
               "same player is not counted twice.")
    # The drop board is priced BEFORE the chain (one build_board scan, item 3.2) and
    # the chain is priced sequentially on top of it, so the two lanes stand on
    # different rosters. Without saying so, this section reads as a menu that can be
    # combined with the claims above it — and can point at a drop the chain has just
    # measured as unaffordable (item 3.4b audit).
    chain_recs = list(plan.claims) + list(plan.fcfs_grabs)
    spent_drops = {r.drop for r in chain_recs if r.drop}
    spent_adds = {r.add for r in chain_recs}
    # When several refusals share one drop, quote the BEST of them — the least-bad
    # post-chain price for giving that body up — rather than whichever happened to
    # be last in the list.
    refused_drops: dict[str, float] = {}
    for r in plan.chain_rejected:
        if r.drop:
            refused_drops[r.drop] = max(refused_drops.get(r.drop, r.gain_after),
                                        r.gain_after)
    if chain_recs:
        out.append(
            f"  these numbers assume you make NONE of the {len(chain_recs)} move(s) "
            f"above — each is what the player is worth on your roster exactly as it "
            f"stands today. The chain already spends {len(spent_drops)} of them, and a "
            f"free agent named in a reason below can only be added ONCE. This is a "
            f"reference, not a menu to combine with the list above."
        )
    season_rows = [d for d in plan.drop_board
                   if not (d.horizon_weeks == 1 and d.position in STREAMED_POSITIONS)]
    streamed_rows = [d for d in plan.drop_board
                     if d.horizon_weeks == 1 and d.position in STREAMED_POSITIONS]

    def drop_line(d: DropRec) -> str:
        tag = ""
        # ESPN's undroppable list first (item 3.8a): it is the only tag here that
        # says the move is IMPOSSIBLE rather than merely priced against something.
        # He is still listed and still priced — "what he is worth" is a fair
        # question — but no claim, chain step or forced drop can name him.
        if d.undroppable:
            # ONE shared stem with `marginal.format_marginal` (audit fix) plus the
            # tail that is only true on THIS page, which has a claim list above it.
            tag = f"   {UNDROPPABLE_TAG}, so no move above can use him]"
        elif d.player in spent_drops:
            tag = "   [already spent — a move above drops him; not a separate move]"
        elif d.player in refused_drops:
            tag = (f"   [the chain REFUSES this drop once the moves above win — the "
                   f"BEST add it buys is measured at "
                   f"{refused_drops[d.player]:+.1f} there]")
        return (f"  {d.player} ({d.position}, {d.team or '-'})  {d.marginal_points:+.1f} "
                f"pts / {_weeks_phrase(d.horizon_weeks)}{tag}")

    def drop_reason_lines(d: DropRec) -> list[str]:
        rows = [f"      - {r}" for r in d.reasons]
        # A drop-board reason names the best free agent available BEFORE the chain,
        # so the same add can be proposed on many rows AND on a row whose add the
        # chain has already claimed. Say so rather than let it read as a menu.
        taken = sorted({a for a in spent_adds if any(a in r for r in d.reasons)})
        if taken:
            rows.append(
                f"      - note: {', '.join(taken)} is already claimed by a numbered "
                f"move above and can only be added ONCE — this line assumes you do "
                f"NOT make that move."
            )
        return rows

    for d in season_rows:
        out.append(drop_line(d))
        if reasons:
            out.extend(drop_reason_lines(d))
    if streamed_rows:
        out.append("  -- streamed weekly (K/DST) — a 1-week number, NOT comparable to "
                   "the season-long rows above --")
        for d in streamed_rows:
            out.append(drop_line(d))
            if reasons:
                out.extend(drop_reason_lines(d))
    return "\n".join(out)
