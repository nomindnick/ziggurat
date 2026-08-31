"""External validation of the 2.3 survival model against REAL completed drafts.

DELETABLE package (Rule 8). This module MEASURES; it changes no shipped
behaviour. Nothing in ``survival.py``, ``engine.py``, ``priors.py`` or
``simulator.py`` is touched, and every alternative parameter bag this module
produces is a clearly labelled constant for a later A/B — never a new default.

WHY IT EXISTS. ``survival.py``'s docstring reports its analytic fit at
``R^2 = 0.985``, made against 2,500 runs of *our own simulator*. Nobody had ever
scored the survival model against a room of real drafters. Two external sources
exist and this module reads both:

* the completed practice/mock draft journals under ``data/draft/practice/``
  (gitignored) — each a real 160-pick ESPN room mirrored into the cockpit by the
  DOM sync, with a real human (us) in one seat;
* ``espn_draft_ranks.adp`` — ESPN's own pooled average draft position, which is
  a DIFFERENT signal from the ``overall_rank`` the room model is keyed on.

AND WHY IT RUNS A NULL CONTROL (the correction that reshaped this module). The
first version reported the analytic route's -0.370 bias as a finding about real
drafters. It is not. The SAME shipped constants score bias ~-0.335 against this
module's OWN all-bot simulator, and ``SurvivalParams`` fitted on eleven
SIMULATED rooms recover ~96% of the re-fit's Brier improvement when scored on
the eleven REAL ones (Brier ~0.138 against the real-room re-fit's 0.131 and the
shipped constants' 0.345). The shipped analytic constants are not merely
inaccurate about humans: they are inconsistent with the simulator they were
fitted to, and the cheap repair — re-fit ``analytic_survival`` against that
simulator, offline, no journals and no draft needed — stayed invisible for
exactly as long as no control was run. So every "the real room does X" claim
below is printed NEXT TO the same measurement on rooms the model itself drew
(:func:`null_control`, section 2b). Where the control reproduces the result, the
finding is about OUR MODEL, not about drafters.

TWO MORE THINGS THIS MODULE LEARNED THE SAME WAY. (a) Measure the room model the
cockpit ACTUALLY holds: ``session._engine()`` runs live-recalibrated priors at
87% of these decisions, so the rollout is scored that way by default and the
cold-start pass is kept only as a labelled contrast
(:data:`LIVE_RECALIBRATION_IS_THE_ROOM_MODEL`). (b) Score the quantity the
engine consumes, and say so: the analytic route is unconditional and therefore
reads pessimistic against a conditional outcome, which is ~0.10 of its headline
bias (:data:`ANALYTIC_ROUTE_IS_UNCONDITIONAL`). Both rows are printed; neither
is presented alone.

WHAT IT ANSWERS (five questions, five sections below):

1. **Calibration.** Reconstruct every one of the operator's real decision
   windows, ask both survival routes what they would have said, and score the
   answer against what the room actually did. Reported overall, by reliability
   bin, by round and by position, for the sim-derived rollout (route 1, the live
   path, live-recalibrated and cold-start) and the analytic logistic (route 2,
   the fallback, unconditional as the engine uses it and conditioned).
2. **Re-fit.** Re-estimate :class:`~ziggurat.draft.survival.SurvivalParams`
   against the real journals by maximum likelihood, with the right censoring the
   problem actually has (see :func:`survival_observations`).
3. **The K/DST contradiction.** ESPN ADP says the first D/ST goes at ~pick 90;
   the room model's ``dst_center`` is 148.8 and the rehearsal saw the room's
   bulk at 140-154. :func:`kdst_timing` settles it from the journals.
4. **The room priors.** ``reach_sigma`` and ``autodraft_fraction`` measured on
   real seats, with the input/output trap that comparison hides (see
   :data:`REACH_SIGMA_IS_AN_INPUT_NOT_AN_OUTPUT`). A labelled one-knob-at-a-time
   A/B (:func:`compare_priors`) exists so a prior claim can never again be made
   from a table row that silently moved two knobs.
5. **The control.** :func:`null_control` re-runs questions 1-2 against all-bot
   rooms drawn from the very model under test, which is the only way to tell
   "the real room surprised us" from "our constants were wrong about anything".

RULES. Rule 1: the only DB reads are :func:`load_journal_board` and
:func:`load_espn_adp`, both keyword-only ``as_of`` with no default and both
defaulting to the ``historical`` view; a journal's board is loaded at the
``as_of`` the journal itself recorded and verified against the journal's
``board_hash``, so a board silently loaded at "now" fails loudly instead of
quietly re-pricing history. Rule 2: no scoring constant — points and VOR come
off the board entries. Rule 6: every constant this module publishes is labelled
with its cohort and its n. Rule 8: lives under ``ziggurat/draft/``, imports only
siblings + ``core``/``data`` upward. Rule 6 again, learned the hard way: a
number this module can recompute, it recomputes and PRINTS (the ADP census, the
Monte-Carlo noise floor, the prior-A/B knob diff) — three of the figures the
first version remembered in prose did not reproduce, and a docstring number that
fails a five-second check devalues the ones beside it that hold.

Determinism: every random draw comes from a caller-passed ``random.Random``;
the optimizer is a deterministic Nelder-Mead with a fixed start.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ziggurat.core.valuation import RosterStructure
from ziggurat.draft.bots import (
    KDST,
    POSITIONS,
    AutodraftBot,
    BoardEntry,
    BoardState,
    PickContext,
    allowed_positions,
    legal_positions,
    position_counts,
)
from ziggurat.draft.priors import (
    DEFAULT_KDST_EARLIEST_ROUND,
    ROOM_PRIORS_2025,
    RoomPriors,
)
from ziggurat.draft.simulator import snake_sequence
from ziggurat.draft.survival import (
    DEFAULT_KAPPA,
    DEFAULT_SURVIVAL_PARAMS,
    LiveRecalibration,
    SurvivalParams,
    analytic_survival,
    recalibrate_from_pick_log,
    rollout_survival,
)

# --------------------------------------------------------------------- knobs

#: Mirrors ``survival._FALLBACK_RANK_BASE`` / ``simulator._FALLBACK_BASE``:
#: players the ESPN board does not rank sit at or beyond this rank. They carry
#: no meaningful "when does the room take him" signal and are excluded from
#: every fit here, exactly as ``calibration.py`` and ``survival.py`` exclude
#: them.
FALLBACK_RANK_BASE = 10_000

#: How many available players (by ESPN rank) the calibration probe scores at
#: each decision, on TOP of the engine's own candidate set. The engine itself
#: only ever scores its candidate set (top-5 by rank + best-VOR per allowed
#: position); the wider probe exists so the by-position and by-round breakdowns
#: are not five players deep. Points carry ``in_engine_candidates`` so the
#: decision-relevant subset can always be recovered.
DEFAULT_PROBE_WIDTH = 20

#: Rollouts per decision window for :func:`rollout_points`. The live cockpit
#: runs 512; 200 keeps 165 windows x 11 journals a ~6 s measurement. THE
#: MONTE-CARLO NOISE IS NOT ZERO AND IS REPORTED AS A RANGE, NOT A POINT: see
#: :data:`MEASUREMENT_NOISE` for the measured seed spread. Every headline Brier
#: printed by this module carries roughly +/- 0.001 of seed noise, so a Brier
#: difference smaller than ~0.003 between two variants is not a result.
DEFAULT_MEASUREMENT_ROLLOUTS = 200

#: Matches ``engine.DEFAULT_CANDIDATE_WIDTH``, restated rather than imported so
#: this module never becomes a reason ``engine.py`` cannot be edited freely; the
#: test suite pins the two together.
DEFAULT_CANDIDATE_WIDTH = 5

#: Reliability-bin edges for :func:`calibrate`. Deliberately finer at the ends:
#: the engine's ``urgency = VONA * (1 - S_next)`` is most sensitive where
#: ``S_next`` is confident, so "the model said 0.99 and it was 0.87" is a much
#: worse defect than the same gap at 0.5.
DEFAULT_BINS: tuple[float, ...] = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99, 1.0 + 1e-9)


# ------------------------------------------------------ labelled alternatives

#: ALTERNATIVE, NOT A DEFAULT (Rule 6). SurvivalParams re-fitted by maximum
#: likelihood against the ELEVEN completed real ESPN practice rooms in
#: ``data/draft/practice/`` (1,760 seat-picks, 11,293 player-draft observations
#: of which 1,584 are room events and the rest right-censored), boards
#: reconstructed at each journal's own ``as_of`` and verified by ``board_hash``.
#: Measured 2026-08-30 by ``python -m ziggurat.draft.roomcheck``.
#:
#: Held-out evidence, leave-one-journal-out (fit on 10, score the 11th):
#: analytic-route Brier 0.345 -> 0.132 and mean bias -0.370 -> -0.043. So the
#: gain is not in-sample overfitting OF THESE ELEVEN JOURNALS.
#:
#: IT IS ALSO MOSTLY NOT A FINDING ABOUT REAL DRAFTERS — READ THIS FIRST.
#: Leave-one-journal-out holds out a ROOM, not the generating process, so it
#: cannot tell "the real room differs from our model" from "the baseline was a
#: bad fit to anything". :func:`null_control` runs that test and the answer is
#: the second one: params fitted on ELEVEN SIMULATED all-bot rooms, scored on
#: the eleven REAL ones, reach Brier ~0.138 against this refit's 0.131 and the
#: shipped constants' 0.345 — ~96% of the improvement is available with ZERO
#: real-room data. The cheap, safe repair is to re-fit ``analytic_survival``
#: against the simulator offline; only the last few percent is evidence about
#: humans, and section 2b recomputes the split on every run rather than
#: asking a later reader to trust this sentence.
#:
#: TWO MORE CAVEATS BEFORE SHIPPING IT: (a) the K and DST rows below are the
#: best constants available *inside the shipped rank-independent shape*, and
#: that shape is measurably wrong (:data:`KDST_SHAPE_IS_MISSPECIFIED`). Their
#: centers sit past pick 160 because most of the 32 D/ST and 160 K entries on
#: the board are never drafted at all, and a single flat center has to explain
#: the never-drafted majority as well as the ten that go. (b) Both these and the
#: shipped constants are scored the way the ENGINE uses them, which is
#: unconditional and therefore reads pessimistic by construction
#: (:data:`ANALYTIC_ROUTE_IS_UNCONDITIONAL`); a drop-in constants swap inherits
#: that shape.
REFIT_PRACTICE_2026 = SurvivalParams(
    skill_center_intercept=5.374,
    skill_center_slope=0.8208,
    skill_width=4.670,
    k_center=176.65,
    k_width=9.82,
    dst_center=170.03,
    dst_width=12.27,
)

REFIT_PRACTICE_2026_LABEL = (
    "HYPOTHESIS (not shipped): SurvivalParams re-fitted by censored maximum "
    "likelihood on 11 completed real 10-team ESPN practice drafts, 2026-08-16 "
    "to 2026-08-29 (1,584 room draft events, 9,709 right-censored). Held-out "
    "leave-one-journal-out Brier 0.132 vs the shipped constants' 0.345. "
    "MOSTLY NOT A HUMAN FINDING: params fitted on the same number of ALL-BOT "
    "simulated rooms score ~0.138-0.140 on these same real rooms (section 2b "
    "recomputes it), so ~96-97% of that gain is a repair of a baseline that "
    "never fitted our own simulator either — and it is available offline, with "
    "no journals at all. Held-out "
    "on rooms, not on the generating process; scored unconditionally, as the "
    "engine uses it (see ANALYTIC_ROUTE_IS_UNCONDITIONAL)."
)

#: ALTERNATIVE, NOT A DEFAULT (Rule 6). ``RoomPriors`` with the reach spread
#: MEASURED on the same 11 rooms (median across rooms 13.25, range 11.40-21.02,
#: n=126 room skill picks each) instead of the 2025 single-draft fit of 17.78.
#: Do not ship this without reading
#: :data:`REACH_SIGMA_IS_AN_INPUT_NOT_AN_OUTPUT` — 13.25 is an OUTPUT statistic
#: and ``reach_sigma`` is an INPUT knob, and they are not the same quantity.
MEASURED_ROOM_2026 = dataclasses.replace(ROOM_PRIORS_2025, reach_sigma=13.25)

REACH_SIGMA_IS_AN_INPUT_NOT_AN_OUTPUT = (
    "TRAP, measured 2026-08-30 (re-measured; see the note on precision below). "
    "`recalibrate_from_pick_log` reports the REALIZED spread of "
    "(espn_rank - overall_pick) in a finished draft, and then writes that "
    "number back into `priors.reach_sigma`, which is the INPUT sigma of the "
    "Gaussian a RankNoiseBot adds before REACH_WINDOW, legality, NEED_BONUS and "
    "the position-run nudge compress it. They are not the same scale. Transfer "
    "curve on the live 2026-08-29 board, 100 all-bot drafts per point (two "
    "seeds agreeing to +/-0.05): input 5 -> realized 12.3; 10 -> 13.1; "
    "13.25 -> 13.8; 17.78 -> 14.65; 23.10 (= 17.78 x kappa 1.3) -> 15.6; "
    "30 -> 16.7. The realized spread has a ~12.3 structural floor no input can "
    "go below. So the real rooms' 13.25 does NOT mean 'the prior is 4.5 too "
    "wide': like-for-like the shipped priors realize ~14.65 against a real "
    "13.25 (about 11% too loose), and the input that would land on 13.25 is "
    "about 10.8. PRECISION NOTE, which is itself the lesson: the first version "
    "of this constant quoted the same curve to two decimals off TWENTY drafts "
    "per point, and those digits moved by up to 0.4 between seeds. One decimal "
    "is what this measurement supports; `reach_sigma_transfer` recomputes the "
    "whole curve rather than asking a reader to trust these figures."
)

KDST_SHAPE_IS_MISSPECIFIED = (
    "FINDING ABOUT OUR MODEL, NOT ABOUT DRAFTERS (measured 2026-08-30, "
    "control-checked). `analytic_survival` treats K and DST as rank-independent "
    "(one flat center each). Adding a rank slope buys a large likelihood "
    "improvement for one extra parameter — but it buys the SAME improvement "
    "against this module's own all-bot simulator (section 2b prints both "
    "columns), so the rejected shape is rejected by RankNoiseBot drafting off "
    "espn_overall_rank just as firmly as by the eleven real rooms. Read it as "
    "'the flat K/DST center is the wrong functional form, in the model's own "
    "generative world', never as 'real drafters rank their kickers'. The reason "
    "it matters for US is unchanged and is a house-board fact: the room and the "
    "house board disagree about WHICH defense is best, so a single scalar has "
    "to aim at one of them, and the shipped 148.8 happens to be aimed at the "
    "one the engine actually drafts. Section 3 prints that disagreement."
)

ANALYTIC_ROUTE_IS_UNCONDITIONAL = (
    "SHAPE DEFECT in the scored quantity, measured 2026-08-30. "
    "`analytic_survival` returns P(D > O_next) UNCONDITIONALLY, but every row "
    "this module scores is by construction still available at the decision, so "
    "the outcome it is scored against is P(available at next | available now). "
    "P(D>O_next) <= P(D>O_next | D>O_now) always, so the fallback route reads "
    "pessimistic no matter how good its constants are. Measured on the 11 real "
    "rooms (n=3,942): shipped constants scored as the engine uses them give "
    "bias -0.370 / Brier 0.345; the SAME constants conditioned as "
    "S(next)/S(decision-1) give -0.272 / 0.248. So ~0.10 of the headline 0.37 "
    "is shape, not constants. Both rows are printed in section 1. The engine "
    "consumes the UNCONDITIONAL form, so that row is what the cockpit's fallback "
    "actually said and is the honest headline; the conditional row is the "
    "well-posed forecasting question and is what a re-fit should be judged on. "
    "This is a defect in `survival.py`'s fallback route, deliberately NOT fixed "
    "here (this module changes no shipped behaviour); it is only measured."
)

LIVE_RECALIBRATION_IS_THE_ROOM_MODEL = (
    "MEASUREMENT CONTRACT, measured 2026-08-30. `session._engine()` passes "
    "`recalibrate_from_pick_log(...).priors` whenever the live re-fit is "
    "engaged, which on these 11 journals is 143 of 165 operator decisions "
    "(87%) — everything from about round 3 onward. The live median reach_sigma "
    "is ~9.0 (range 1.4-20.9) against the cold-start prior's 17.78, i.e. the "
    "cockpit's effective rollout spread is roughly half the cold-start one. So "
    "`rollout_points` recomputes the recalibration AT EVERY WINDOW by default "
    "(`live_recalibration=True`) and the cold-start pass is kept only as the "
    "labelled contrast: scored cold, the reliability bins the engine is most "
    "sensitive to FLIP SIGN, and 'the room takes TEs and QBs earlier than the "
    "model expects' is a statement about a room model the cockpit stops using "
    "after round 3."
)

MEASUREMENT_NOISE = (
    "NOISE FLOOR, measured 2026-08-30 on the 11 real rooms (live-recalibrated "
    "route). The rollout is Monte Carlo, so its Brier is an estimate: five "
    "seeds at R=200 span 0.1007-0.1017 (spread 0.0010), and three seeds at "
    "R=600 span 0.1011-0.1013 — i.e. R=200 costs about 0.001 of resolution and "
    "no detectable bias. Numbers this module prints are therefore good to ~3 "
    "decimal places, NOT to 4; a Brier gap under ~0.003 between two variants is "
    "noise, and a headline quoted to 4 places is quoting a seed. `--noise-probe` "
    "re-measures the spread in the same run that quotes it, because the earlier "
    "version of this comment recalled two numbers that did not reproduce."
)


# ------------------------------------------------------------------- journals


class JournalError(ValueError):
    """A draft journal is missing, malformed, or internally inconsistent.

    Raised rather than repaired: every measurement below reconstructs a board
    state pick by pick, and a journal whose seat sequence contradicts its own
    header would silently produce a *plausible* wrong answer.
    """


@dataclass(frozen=True)
class JournalPick:
    """One ``kind == "pick"`` record: 1-based overall, 0-based seat."""

    overall: int
    seat: int
    player_id: str
    name: str | None = None


@dataclass(frozen=True)
class DraftJournal:
    """A completed draft as the cockpit journalled it (``session.py`` writes it).

    Everything needed to rebuild the exact board and replay the exact room is
    here: ``board_as_of`` + ``board_count`` + ``board_hash`` pin the board
    (``session._board_hash`` covers ids AND ranks), ``pick_order`` +
    ``operator_slot`` pin the snake, and ``picks`` is the room.

    ``roster.bench_slots`` / ``ir_slots`` are NOT journalled (the header records
    only the four fields the sim uses), so they fall back to
    ``RosterStructure`` defaults. Nothing here reads them.
    """

    path: str
    season: int
    board_as_of: str
    operator_slot: int
    pick_order: tuple[int, ...]
    rounds: int
    roster: RosterStructure
    board_count: int
    board_hash: str
    picks: tuple[JournalPick, ...]

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def teams(self) -> int:
        return self.roster.teams

    @property
    def complete(self) -> bool:
        """True when every pick of the scheduled snake is present."""
        return len(self.picks) == self.rounds * self.teams

    @property
    def by_overall(self) -> Mapping[int, JournalPick]:
        return {p.overall: p for p in self.picks}

    def operator_overall_picks(self) -> tuple[int, ...]:
        """The operator's own overall picks, ascending."""
        return tuple(p.overall for p in self.picks if p.seat == self.operator_slot)

    def room_picks(self) -> tuple[JournalPick, ...]:
        """Every pick made by a seat that is NOT the operator.

        The operator seat is the engine under test, not the room; including it
        would score the model against itself — the exact circularity this
        module exists to break.
        """
        return tuple(p for p in self.picks if p.seat != self.operator_slot)


def load_journal(path: str | Path) -> DraftJournal:
    """Parse one cockpit JSONL journal, validating it against its own header.

    Raises :class:`JournalError` on: an empty file, a first line that is not a
    header, a duplicated overall pick or player id, or a seat sequence that
    contradicts the header's ``pick_order`` under a standard snake. That last
    check is the load-bearing one — every reconstruction below assumes the
    journal's seats ARE the snake.
    """
    p = Path(path)
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as exc:  # pragma: no cover - filesystem-level failure
        raise JournalError(f"cannot read journal {p}: {exc}") from exc
    if not lines:
        raise JournalError(f"journal {p} is empty")

    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise JournalError(f"journal {p} first line is not JSON: {exc}") from exc
    if header.get("kind") != "header":
        raise JournalError(f"journal {p} first line is not a session header")

    picks: list[JournalPick] = []
    for i, line in enumerate(lines[1:], start=2):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalError(f"journal {p} line {i} is not JSON: {exc}") from exc
        if rec.get("kind") != "pick":
            continue
        picks.append(
            JournalPick(
                overall=int(rec["overall"]),
                seat=int(rec["seat"]),
                player_id=str(rec["player_id"]),
                name=rec.get("name"),
            )
        )
    picks.sort(key=lambda q: q.overall)

    rd = header["roster"]
    roster = RosterStructure(
        teams=int(rd["teams"]),
        starters=dict(rd["starters"]),
        flex_slots=int(rd["flex_slots"]),
        flex_positions=frozenset(rd["flex_positions"]),
    )
    journal = DraftJournal(
        path=str(p),
        season=int(header["season"]),
        board_as_of=str(header["as_of"]),
        operator_slot=int(header["operator_slot"]),
        pick_order=tuple(int(t) for t in header["pick_order"]),
        rounds=int(header["rounds"]),
        roster=roster,
        board_count=int(header["board_count"]),
        board_hash=str(header["board_hash"]),
        picks=tuple(picks),
    )
    _validate_journal(journal)
    return journal


def _validate_journal(journal: DraftJournal) -> None:
    overalls = [q.overall for q in journal.picks]
    if len(set(overalls)) != len(overalls):
        raise JournalError(f"journal {journal.name} repeats an overall pick number")
    ids = [q.player_id for q in journal.picks]
    if len(set(ids)) != len(ids):
        raise JournalError(f"journal {journal.name} drafts the same player twice")
    sequence = snake_sequence(journal.pick_order, journal.rounds)
    for q in journal.picks:
        if not 1 <= q.overall <= len(sequence):
            raise JournalError(
                f"journal {journal.name} pick {q.overall} is outside the "
                f"{len(sequence)}-pick snake"
            )
        if sequence[q.overall - 1] != q.seat:
            raise JournalError(
                f"journal {journal.name} pick {q.overall} is journalled to seat "
                f"{q.seat} but the header's snake puts seat "
                f"{sequence[q.overall - 1]} on the clock"
            )


def load_journals(
    directory: str | Path, *, complete_only: bool = True, pattern: str = "*.jsonl"
) -> tuple[DraftJournal, ...]:
    """Every parseable journal in ``directory``, sorted by filename.

    ``complete_only`` (the default) drops journals that did not reach the last
    pick — the practice directory is full of abandoned launches that hold a
    header and nothing else, and a 20-pick fragment is not a room. A file that
    fails to parse is skipped too, which is why :class:`RoomcheckReport` carries
    ``files_seen`` and the rendered report states how many files produced how
    many usable drafts: a silent drop from 11 rooms to 3 would otherwise change
    every number below with nothing to notice it by.
    """
    out: list[DraftJournal] = []
    for path in sorted(Path(directory).glob(pattern)):
        try:
            journal = load_journal(path)
        except (JournalError, KeyError, TypeError, ValueError):
            continue
        if complete_only and not journal.complete:
            continue
        out.append(journal)
    return tuple(out)


# ------------------------------------------------------------- the DB seam (Rule 1)


class BoardMismatch(RuntimeError):
    """The board rebuilt for a journal is not the board that journal was drafted on.

    Loud by design. ``session.resume`` refuses the same mismatch for the same
    reason: replaying picks onto a differently-priced board produces numbers
    that look fine and are wrong. Here it is also the Rule-1 tripwire — a board
    accidentally loaded at "now" instead of the journal's own ``as_of`` fails
    here rather than silently re-pricing a two-week-old draft with today's ranks.
    """


def load_journal_board(
    conn,
    journal: DraftJournal,
    *,
    as_of: str,
    verify: bool = True,
) -> tuple[BoardEntry, ...]:
    """Rebuild the board a journal was drafted on. The ONE board DB seam.

    ``as_of`` is REQUIRED and keyword-only (Rule 1 — no implicit now); pass
    ``journal.board_as_of``. ``verify`` re-derives ``session._board_hash`` and
    raises :class:`BoardMismatch` unless it matches the journal's header, which
    is what makes a wrong ``as_of`` a crash instead of a plausible answer.
    """
    from ziggurat.draft.simulator import load_board

    board = load_board(conn, as_of=as_of, season=journal.season)
    if verify:
        _verify_board(board, journal, as_of=as_of)
    return board


def _verify_board(
    board: Sequence[BoardEntry], journal: DraftJournal, *, as_of: str
) -> None:
    """Raise :class:`BoardMismatch` unless ``board`` is the journal's own board."""
    from ziggurat.draft.session import _board_hash

    got = _board_hash(board)
    if len(board) != journal.board_count or got != journal.board_hash:
        raise BoardMismatch(
            f"journal {journal.name} was drafted on a board of "
            f"{journal.board_count} entries (hash {journal.board_hash}); "
            f"as_of={as_of!r} season={journal.season} rebuilds {len(board)} "
            f"entries (hash {got}). Load the board at the journal's own "
            f"board_as_of."
        )


@dataclass(frozen=True)
class AdpCensus:
    """How much of an :class:`AdpTable` is the undrafted plateau, not a position.

    COMPUTED, never remembered. An earlier version of :class:`AdpTable` carried
    this census as prose ("631 of 1,027 sit at exactly 170.0 and the pool median
    IS 170.0") and it was wrong at every snapshot the module actually reads —
    which, under Rule 6, devalues the labelled numbers around it that DO
    reproduce. Anything the module can recompute, it now recomputes and prints.
    """

    n: int
    at_plateau: int
    exactly_at_ceiling: int
    median: float
    maximum: float

    @property
    def plateau_share(self) -> float:
        return self.at_plateau / self.n if self.n else 0.0


@dataclass(frozen=True)
class AdpTable:
    """ESPN's pooled average draft position, keyed the way ``load_board`` keys ids.

    Skill players join by ESPN id (which ``load_board`` uses as ``player_id``
    whenever it exists); D/ST rows carry a NULL ``espn_id`` in
    ``espn_draft_ranks`` and join by normalized team abbreviation. Coverage is
    reported rather than assumed — see :meth:`coverage`, which
    :func:`run_roomcheck` calls and :func:`render_report` prints.

    ADP IS PLATEAU-CENSORED, and the plateau is a BAND, not a single value.
    Re-read through this module's own accessor at each journal's own ``as_of``
    (``python -m ziggurat.draft.roomcheck`` prints the live census): at
    2026-08-29, 820 of 1,027 priced players sit at or above 169.5 while only 152
    sit at exactly 170.0, the median is 169.98 and the MAXIMUM is 171.75 — so
    170.0 is where ESPN parks the mostly-undrafted, not a hard cap, and an exact
    equality test would miss four fifths of them. :meth:`censored` therefore
    tests the band (``>= ceiling - 0.5``), and :meth:`census` recomputes all of
    it so no reader has to trust the sentence above.
    """

    by_espn_id: Mapping[str, float]
    by_dst_team: Mapping[str, float]
    ceiling: float = 170.0

    def get(self, entry: BoardEntry) -> float | None:
        if entry.position == "DST":
            return self.by_dst_team.get(entry.team) if entry.team else None
        return self.by_espn_id.get(entry.player_id)

    def censored(self, entry: BoardEntry) -> bool:
        """True when this player's ADP sits in the undrafted plateau band."""
        adp = self.get(entry)
        return adp is not None and adp >= self.ceiling - 0.5

    def coverage(self, board: Sequence[BoardEntry]) -> float:
        ranked = [e for e in board if e.espn_overall_rank < FALLBACK_RANK_BASE]
        if not ranked:
            return 0.0
        return sum(1 for e in ranked if self.get(e) is not None) / len(ranked)

    def census(self) -> AdpCensus:
        """The plateau census of this table, computed (see the class docstring)."""
        values = list(self.by_espn_id.values()) + list(self.by_dst_team.values())
        if not values:
            return AdpCensus(0, 0, 0, float("nan"), float("nan"))
        return AdpCensus(
            n=len(values),
            at_plateau=sum(1 for v in values if v >= self.ceiling - 0.5),
            exactly_at_ceiling=sum(1 for v in values if v == self.ceiling),
            median=statistics.median(values),
            maximum=max(values),
        )


def load_espn_adp(conn, *, as_of: str, season: int) -> AdpTable:
    """ESPN's native crowd ADP as of ``as_of`` (keyword-only; no implicit now).

    A thin read over the existing ``get_espn_draft_ranks`` accessor, so it
    inherits the safe ``historical`` view (gates both ``knowable_as_of`` and
    ``retrieved_as_of``). This is a SECOND ESPN signal, distinct from the
    ``overall_rank`` the room model is keyed on; §3 below exists precisely
    because the two disagree.
    """
    from ziggurat.data.nfl import base
    from ziggurat.data.nfl.espn_ranks import get_espn_draft_ranks

    by_espn: dict[str, float] = {}
    by_dst: dict[str, float] = {}
    for row in get_espn_draft_ranks(conn, as_of=as_of, season=season):
        adp = row["adp"]
        if adp is None:
            continue
        position = str(row["position"]).upper()
        if position in ("DST", "D/ST", "DEF"):
            team = row["team"]
            if team is not None:
                key = str(team).upper()
                by_dst[base.TEAM_ALIASES.get(key, key)] = float(adp)
        elif row["espn_id"] is not None:
            by_espn[str(row["espn_id"])] = float(adp)
    return AdpTable(by_espn_id=by_espn, by_dst_team=by_dst)


# ------------------------------------------------------- decision reconstruction


@dataclass(frozen=True)
class DecisionWindow:
    """One real on-clock decision, with the room's answer already known.

    ``state`` is this window's OWN :class:`BoardState`, holding exactly the picks
    made STRICTLY BEFORE ``decision_pick`` — including every pick before the
    operator's first turn, which is the whole point: forget that prefix and the
    top handful of players stay "available" for the entire draft, are predicted
    gone every round, and are scored as having survived. (That bug produced an
    apparent "model says 1% and 88% survive" catastrophe during this module's
    own build.) It is a ``clone()``, not the shared replay state: every window is
    independent, so a caller may hold them all, iterate them out of order, or
    score one alone. The obvious cheaper alternative — one shared state threaded
    through the replay — is a trap, because by the time a returned tuple reaches
    the caller that state holds the WHOLE draft, every probe then reads players
    nobody drafted, and the observed survival rate comes out at exactly 1.000.
    That is the second bug this module's own build produced.

    ``survived`` membership is decided by ``taken_in_window``: the picks strictly
    between this decision and the operator's next one. The player the operator
    took at ``decision_pick`` is NOT in it, and callers exclude him — his
    survival is counterfactual, never observed.

    ``prior_picks`` is the ``(overall, seat, player_id)`` log of everything
    drafted STRICTLY BEFORE this decision — the same log ``session._compute_recal``
    holds on the clock. It exists so :meth:`live_recalibration` can reproduce the
    room priors the cockpit ACTUALLY held here, which for 87% of these windows is
    not the cold-start prior (see :data:`LIVE_RECALIBRATION_IS_THE_ROOM_MODEL`).
    """

    journal: str
    round: int
    decision_pick: int
    next_pick: int
    state: BoardState
    own_roster: tuple[BoardEntry, ...]
    opponent_rosters: Mapping[int, tuple[BoardEntry, ...]]
    taken_in_window: frozenset[str]
    operator_took: str
    roster: RosterStructure
    rounds_total: int
    operator_slot: int
    prior_picks: tuple[tuple[int, int, str], ...] = ()

    @property
    def gap(self) -> int:
        """Intervening room picks between this decision and the next turn."""
        return self.next_pick - self.decision_pick - 1

    def live_recalibration(
        self,
        board: Sequence[BoardEntry],
        *,
        base_priors: RoomPriors = ROOM_PRIORS_2025,
    ) -> LiveRecalibration:
        """The room re-fit the COCKPIT held at this decision, recomputed exactly.

        Delegates to the shipped ``recalibrate_from_pick_log`` with the same
        arguments ``session._compute_recal`` uses (operator seat excluded,
        ``min_room_picks`` at the shipped ``LIVE_RECAL_MIN_PICKS``), over the
        picks made strictly before this window. ``engaged=False`` means the
        cockpit was still on cold-start priors here, which is exactly what
        ``session._engine()`` falls back to.
        """
        return recalibrate_from_pick_log(
            list(self.prior_picks), board, base_priors=base_priors,
            operator_slot=self.operator_slot,
        )

    def context(self, *, rng: random.Random) -> PickContext:
        """The exact :class:`PickContext` the cockpit held at this decision."""
        return PickContext(
            team_slot=self.operator_slot,
            round=self.round,
            overall_pick=self.decision_pick,
            rounds_total=self.rounds_total,
            roster=self.roster,
            own_roster=list(self.own_roster),
            state=self.state,
            rng=rng,
            opponent_rosters={t: list(v) for t, v in self.opponent_rosters.items()},
        )


def operator_windows(
    journal: DraftJournal, board: Sequence[BoardEntry]
) -> tuple[DecisionWindow, ...]:
    """Every operator decision that HAS a next pick, replayed onto a live board.

    The last round is excluded: survival to the next pick is undefined when
    there is no next pick (``rollout_survival`` answers 1.0 there by
    construction, which is true and carries no information).

    One :class:`BoardState` is advanced pick by pick through the whole replay
    (the same single-state discipline ``run_draft`` uses), but each window keeps
    a ``clone()`` of it taken at that window's own moment — see
    :class:`DecisionWindow` for why sharing the live state is a trap.
    """
    by_id = {e.player_id: e for e in board}
    missing = [q.player_id for q in journal.picks if q.player_id not in by_id]
    if missing:
        raise BoardMismatch(
            f"journal {journal.name} drafted {len(missing)} player(s) that are "
            f"not on the rebuilt board (first: {missing[0]!r})"
        )

    by_overall = journal.by_overall
    op_picks = journal.operator_overall_picks()
    if len(op_picks) < 2:
        return ()

    state = BoardState(board)
    rosters: dict[int, list[BoardEntry]] = defaultdict(list)
    windows: list[DecisionWindow] = []

    def consume(overall: int) -> None:
        rec = by_overall[overall]
        state.take(rec.player_id)
        rosters[rec.seat].append(by_id[rec.player_id])

    # The prefix before the operator's FIRST turn is part of the board state.
    for overall in range(1, op_picks[0]):
        consume(overall)

    log: list[tuple[int, int, str]] = [
        (o, by_overall[o].seat, by_overall[o].player_id) for o in range(1, op_picks[0])
    ]
    for idx, decision in enumerate(op_picks[:-1]):
        nxt = op_picks[idx + 1]
        windows.append(
            DecisionWindow(
                journal=journal.name,
                round=(decision - 1) // journal.teams + 1,
                decision_pick=decision,
                next_pick=nxt,
                state=state.clone(),
                own_roster=tuple(rosters[journal.operator_slot]),
                opponent_rosters={
                    t: tuple(v) for t, v in rosters.items() if t != journal.operator_slot
                },
                taken_in_window=frozenset(
                    by_overall[o].player_id for o in range(decision + 1, nxt)
                ),
                operator_took=by_overall[decision].player_id,
                roster=journal.roster,
                rounds_total=journal.rounds,
                operator_slot=journal.operator_slot,
                prior_picks=tuple(log),
            )
        )
        for overall in range(decision, nxt):
            consume(overall)
            rec = by_overall[overall]
            log.append((overall, rec.seat, rec.player_id))
    return tuple(windows)


def probe_candidates(
    window: DecisionWindow,
    *,
    probe_width: int = DEFAULT_PROBE_WIDTH,
    candidate_width: int = DEFAULT_CANDIDATE_WIDTH,
    kdst_earliest_round: int | None = DEFAULT_KDST_EARLIEST_ROUND,
) -> tuple[tuple[BoardEntry, ...], frozenset[str]]:
    """``(everyone scored, the engine's own candidate ids)`` at one decision.

    The engine set is rebuilt exactly as ``PickEngine.recommend`` builds it —
    D1: top-``candidate_width`` available by ESPN rank across ALLOWED positions,
    union best-by-VOR at each allowed position, with the same
    ``kdst_earliest_round`` window ``PickEngine`` defaults to — so the
    decision-relevant slice of every calibration table below is the real one and
    not an approximation. Passing ``kdst_earliest_round=None`` widens the engine
    set to include K/DST in every round, which is NOT what the engine does.

    The wider probe deliberately IGNORES the window: it adds the best-by-VOR and
    best-by-rank at EVERY position plus the top ``probe_width`` available
    overall, which is what gives the K/DST and per-round breakdowns any depth.
    A K the engine would not consider in round 3 still has a survival curve, and
    §3 is about exactly that curve.
    """
    counts = position_counts(window.own_roster)
    picks_after = window.rounds_total - window.round
    allowed = allowed_positions(
        counts,
        picks_after,
        window.roster,
        round_num=window.round,
        kdst_earliest_round=kdst_earliest_round,
    )
    if not allowed:
        # engine.py's own fallback ladder, mirrored exactly: a window that allows
        # nothing falls back to LEGAL positions first, and only then to every
        # position. Falling straight to POSITIONS (as this did) would silently
        # widen the engine set in exactly the late-round corner where legality
        # binds hardest.
        allowed = legal_positions(counts, picks_after, window.roster) or set(POSITIONS)

    state = window.state
    engine: dict[str, BoardEntry] = {}
    for pos in sorted(allowed):
        entry = state.front_vor(pos)
        if entry is not None:
            engine[entry.player_id] = entry
    for entry in state.window_by_rank(sorted(allowed), candidate_width):
        engine.setdefault(entry.player_id, entry)
    if not engine:  # degenerate board, mirroring engine.py's last resort
        entry = state.best_by_rank(sorted(allowed)) or state.best_by_rank(POSITIONS)
        if entry is not None:
            engine[entry.player_id] = entry

    probe = dict(engine)
    for pos in POSITIONS:
        for entry in (state.front_vor(pos), state.front_rank(pos)):
            if entry is not None:
                probe.setdefault(entry.player_id, entry)
    for entry in state.window_by_rank(POSITIONS, probe_width):
        probe.setdefault(entry.player_id, entry)

    probe.pop(window.operator_took, None)  # our own pick: never observed
    return tuple(probe.values()), frozenset(engine) - {window.operator_took}


# --------------------------------------------------------------- calibration


@dataclass(frozen=True)
class SurvivalPoint:
    """One (prediction, outcome) pair: did this player survive to our next pick?"""

    journal: str
    route: str
    round: int
    decision_pick: int
    next_pick: int
    player_id: str
    name: str | None
    position: str
    espn_overall_rank: int
    predicted: float
    survived: bool
    in_engine_candidates: bool


def _points_for_window(
    window: DecisionWindow,
    candidates: Sequence[BoardEntry],
    engine_ids: frozenset[str],
    predictions: Mapping[str, float],
    route: str,
) -> list[SurvivalPoint]:
    return [
        SurvivalPoint(
            journal=window.journal,
            route=route,
            round=window.round,
            decision_pick=window.decision_pick,
            next_pick=window.next_pick,
            player_id=c.player_id,
            name=c.name,
            position=c.position,
            espn_overall_rank=c.espn_overall_rank,
            predicted=float(predictions[c.player_id]),
            survived=c.player_id not in window.taken_in_window,
            in_engine_candidates=c.player_id in engine_ids,
        )
        for c in candidates
    ]


def _conditional_survival(
    entry: BoardEntry,
    window: DecisionWindow,
    params: SurvivalParams,
) -> float:
    """``P(available at next | available NOW)`` for the analytic route.

    ``S(O) = P(D > O)``, so conditioning on "still on the board at the decision"
    (i.e. ``D > decision_pick - 1``) is the ratio ``S(next)/S(decision-1)``. See
    :data:`ANALYTIC_ROUTE_IS_UNCONDITIONAL` for why the unconditional form is
    biased pessimistic against this module's outcome and why BOTH are reported.
    """
    s_next = analytic_survival(
        entry.espn_overall_rank, entry.position, window.next_pick, params=params
    )
    s_now = analytic_survival(
        entry.espn_overall_rank, entry.position, window.decision_pick - 1, params=params
    )
    if s_now <= 1e-12:
        # The model gave ~0 mass to him lasting this long, yet here he is. The
        # conditional question is then vacuous; 1.0 is the only honest answer
        # ("given the impossible happened, the model has nothing left to say").
        return 1.0
    return min(1.0, s_next / s_now)


def analytic_points(
    journal: DraftJournal,
    board: Sequence[BoardEntry],
    *,
    params: SurvivalParams = DEFAULT_SURVIVAL_PARAMS,
    probe_width: int = DEFAULT_PROBE_WIDTH,
    kdst_earliest_round: int | None = DEFAULT_KDST_EARLIEST_ROUND,
    conditional: bool = False,
) -> tuple[SurvivalPoint, ...]:
    """Score route 2 (the analytic logistic) over one journal's real decisions.

    ``kdst_earliest_round`` shapes only which rows carry
    ``in_engine_candidates``; it defaults to the engine's own window so that
    flag means what it says. The scored cohort is the wider probe either way.

    ``conditional`` picks WHICH QUANTITY is scored, and the default is
    deliberately the worse-looking one: ``False`` scores ``S(next_pick)`` exactly
    as ``PickEngine`` consumes it, which is unconditional and therefore reads
    pessimistic by construction against an outcome that is conditional on
    availability now; ``True`` scores ``S(next)/S(decision-1)``, the well-posed
    forecasting question. The default measures the cockpit; the option measures
    the constants. :data:`ANALYTIC_ROUTE_IS_UNCONDITIONAL` carries the numbers
    and the reason both are printed.
    """
    points: list[SurvivalPoint] = []
    route = "analytic/conditional" if conditional else "analytic"
    for window in operator_windows(journal, board):
        candidates, engine_ids = probe_candidates(
            window, probe_width=probe_width, kdst_earliest_round=kdst_earliest_round
        )
        if conditional:
            predictions = {
                c.player_id: _conditional_survival(c, window, params) for c in candidates
            }
        else:
            predictions = {
                c.player_id: analytic_survival(
                    c.espn_overall_rank, c.position, window.next_pick, params=params
                )
                for c in candidates
            }
        points.extend(
            _points_for_window(window, candidates, engine_ids, predictions, route)
        )
    return tuple(points)


def rollout_points(
    journal: DraftJournal,
    board: Sequence[BoardEntry],
    *,
    rng: random.Random,
    rollouts: int = DEFAULT_MEASUREMENT_ROLLOUTS,
    priors: RoomPriors = ROOM_PRIORS_2025,
    kappa: float = DEFAULT_KAPPA,
    probe_width: int = DEFAULT_PROBE_WIDTH,
    kdst_earliest_round: int | None = DEFAULT_KDST_EARLIEST_ROUND,
    live_recalibration: bool = True,
) -> tuple[SurvivalPoint, ...]:
    """Score route 1 (the live sim-derived rollout) over one journal's decisions.

    ``kappa`` defaults to the engine's live ``survival.DEFAULT_KAPPA`` (1.3) and
    ``live_recalibration`` defaults to True, which together are what make "this
    measures what the cockpit ACTUALLY said" true rather than aspirational:
    ``session._engine()`` hands ``PickEngine`` the live-recalibrated priors from
    pick 20 of the room onward, so scoring every window on cold-start
    ``ROOM_PRIORS_2025`` measures a room model the cockpit stops using after
    about round 3 (:data:`LIVE_RECALIBRATION_IS_THE_ROOM_MODEL`). ``priors`` is
    then the BASE the recalibration replaces ``reach_sigma`` on, and the cold
    fallback wherever the re-fit is not yet engaged — exactly ``session``'s
    ``recal.priors if recal.engaged else None`` ladder.

    Pass ``live_recalibration=False`` for the labelled cold-start contrast (the
    report prints both). The rollout count remains the one honest gap between
    this and the cockpit: 200 here against the live 512 (see
    :data:`DEFAULT_MEASUREMENT_ROLLOUTS` and :data:`MEASUREMENT_NOISE`).

    Every draw comes from ``rng``; the same seed reproduces the whole table.
    """
    points: list[SurvivalPoint] = []
    route = "rollout" if live_recalibration else "rollout/cold"
    for window in operator_windows(journal, board):
        candidates, engine_ids = probe_candidates(
            window, probe_width=probe_width, kdst_earliest_round=kdst_earliest_round
        )
        window_priors = priors
        if live_recalibration:
            recal = window.live_recalibration(board, base_priors=priors)
            if recal.engaged:
                window_priors = recal.priors
        ctx = window.context(rng=random.Random(rng.getrandbits(64)))
        result = rollout_survival(
            ctx,
            candidates,
            rng=random.Random(rng.getrandbits(64)),
            rollouts=rollouts,
            priors=window_priors,
            kappa=kappa,
        )
        points.extend(
            _points_for_window(window, candidates, engine_ids, result.survival, route)
        )
    return tuple(points)


@dataclass(frozen=True)
class CalibrationBin:
    """One reliability bin: what the model promised vs what the room delivered."""

    low: float
    high: float
    n: int
    mean_predicted: float
    observed_rate: float

    @property
    def bias(self) -> float:
        return self.mean_predicted - self.observed_rate


@dataclass(frozen=True)
class Calibration:
    """A calibration report over one cohort of :class:`SurvivalPoint`.

    ``bias = mean_predicted - observed_rate``. POSITIVE means the model is
    OPTIMISTIC: it says players survive more often than they do, so the engine
    feels less urgency than the room warrants and waits too long. NEGATIVE means
    PESSIMISTIC: manufactured urgency, and the engine reaches.
    """

    label: str
    n: int
    mean_predicted: float
    observed_rate: float
    brier: float
    bins: tuple[CalibrationBin, ...]

    @property
    def bias(self) -> float:
        return self.mean_predicted - self.observed_rate

    @property
    def verdict(self) -> str:
        """Plain-language direction, for a novice reader (Rule 6)."""
        if abs(self.bias) < 0.02:
            return "well calibrated"
        return "OPTIMISTIC (waits too long)" if self.bias > 0 else "PESSIMISTIC (reaches)"


def calibrate(
    points: Sequence[SurvivalPoint],
    *,
    label: str,
    bins: Sequence[float] = DEFAULT_BINS,
) -> Calibration:
    """Score a cohort. Raises on an empty cohort — a silent n=0 report is a lie."""
    if not points:
        raise ValueError(f"cannot calibrate {label!r}: no survival points")
    predicted = [p.predicted for p in points]
    outcomes = [1.0 if p.survived else 0.0 for p in points]
    edges = list(bins)
    built: list[CalibrationBin] = []
    for low, high in zip(edges[:-1], edges[1:], strict=True):
        chunk = [p for p in points if low <= p.predicted < high]
        if not chunk:
            continue
        built.append(
            CalibrationBin(
                low=low,
                high=min(high, 1.0),
                n=len(chunk),
                mean_predicted=statistics.fmean(p.predicted for p in chunk),
                observed_rate=sum(1 for p in chunk if p.survived) / len(chunk),
            )
        )
    return Calibration(
        label=label,
        n=len(points),
        mean_predicted=statistics.fmean(predicted),
        observed_rate=statistics.fmean(outcomes),
        brier=statistics.fmean((q - o) ** 2 for q, o in zip(predicted, outcomes, strict=True)),
        bins=tuple(built),
    )


def calibrate_by(
    points: Sequence[SurvivalPoint],
    key: str,
    *,
    prefix: str = "",
    bins: Sequence[float] = DEFAULT_BINS,
) -> dict[object, Calibration]:
    """Group points by a :class:`SurvivalPoint` attribute and calibrate each group."""
    groups: dict[object, list[SurvivalPoint]] = defaultdict(list)
    for point in points:
        groups[getattr(point, key)].append(point)
    return {
        value: calibrate(chunk, label=f"{prefix}{value}", bins=bins)
        for value, chunk in sorted(groups.items(), key=lambda kv: str(kv[0]))
    }


# --------------------------------------------------------------------- re-fit


@dataclass(frozen=True)
class SurvivalObservation:
    """When the ROOM took a player, or the fact that it never got the chance.

    The re-fit is a survival-analysis problem and the censoring is the whole
    design. Three cases, from the room's point of view:

    * the room drafted him at ``pick``            -> ``censored=False`` (an event);
    * the OPERATOR drafted him at ``pick``        -> ``censored=True`` (the room
      had not taken him by then and never got another chance — we removed him);
    * nobody drafted him                          -> ``censored=True`` at the
      last pick of the draft.

    Getting case 2 wrong is not academic: the engine drafts a D/ST around pick
    85 in most of these journals, so treating our own pick as a room event would
    hand the fit an early "the room takes defenses at 85" signal that the room
    never produced.
    """

    position: str
    espn_overall_rank: int
    pick: int
    censored: bool


def survival_observations(
    journal: DraftJournal, board: Sequence[BoardEntry]
) -> tuple[SurvivalObservation, ...]:
    """One observation per ESPN-ranked board entry per journal (see the class doc).

    Board-unranked (fallback) entries are excluded: the room never considers
    them, so "he lasted to 160" carries no information about the room's board.
    """
    taken = {q.player_id: q for q in journal.picks}
    last_pick = journal.rounds * journal.teams
    out: list[SurvivalObservation] = []
    for entry in board:
        if entry.espn_overall_rank >= FALLBACK_RANK_BASE:
            continue
        rec = taken.get(entry.player_id)
        if rec is None:
            out.append(
                SurvivalObservation(entry.position, entry.espn_overall_rank, last_pick, True)
            )
        else:
            out.append(
                SurvivalObservation(
                    entry.position,
                    entry.espn_overall_rank,
                    rec.overall,
                    rec.seat == journal.operator_slot,
                )
            )
    return tuple(out)


def _log_sigmoid(x: float) -> float:
    """``log(sigmoid(x))``, overflow-safe on both tails."""
    if x >= 0:
        return -math.log1p(math.exp(-x))
    return x - math.log1p(math.exp(x))


def _negative_log_likelihood(
    rows: Sequence[SurvivalObservation],
    center_of_rank,
    width: float,
) -> float:
    """NLL of a right-censored logistic location-scale model.

    ``S(O) = sigmoid((center - O) / width)`` is exactly the shipped survival
    form, which makes the draft pick ``D`` logistic with location ``center`` and
    scale ``width``. So an EVENT contributes the logistic log-density at its
    pick and a CENSORED row contributes ``log S(pick)`` — the same function the
    engine evaluates, fitted as the likelihood it implies rather than as a
    curve-fit through binned survival rates.
    """
    if width <= 1e-6:
        return math.inf
    log_width = math.log(width)
    total = 0.0
    for row in rows:
        z = (row.pick - center_of_rank(row.espn_overall_rank)) / width
        if row.censored:
            total -= _log_sigmoid(-z)
        else:
            # log logistic pdf, written on |z| so exp() never overflows
            total -= -abs(z) - log_width - 2.0 * math.log1p(math.exp(-abs(z)))
    return total


def _nelder_mead(
    objective,
    start: Sequence[float],
    step: Sequence[float],
    *,
    max_iter: int = 6000,
    tol: float = 1e-11,
) -> tuple[tuple[float, ...], float]:
    """Deterministic Nelder-Mead. No SciPy dependency, no randomness, no clock.

    Standard coefficients (reflect 1, expand 2, contract 0.5, shrink 0.5). The
    simplex is seeded from ``start`` plus one axis step each, so a given
    (objective, start, step) always walks the identical path.
    """
    n = len(start)
    simplex = [list(start)]
    for i in range(n):
        point = list(start)
        point[i] += step[i]
        simplex.append(point)
    values = [objective(p) for p in simplex]

    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: values[i])
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        if abs(values[-1] - values[0]) < tol:
            break
        centroid = [sum(simplex[i][j] for i in range(n)) / n for j in range(n)]
        worst = simplex[-1]
        reflected = [centroid[j] + (centroid[j] - worst[j]) for j in range(n)]
        f_reflected = objective(reflected)
        if f_reflected < values[0]:
            expanded = [centroid[j] + 2.0 * (centroid[j] - worst[j]) for j in range(n)]
            f_expanded = objective(expanded)
            if f_expanded < f_reflected:
                simplex[-1], values[-1] = expanded, f_expanded
            else:
                simplex[-1], values[-1] = reflected, f_reflected
        elif f_reflected < values[-2]:
            simplex[-1], values[-1] = reflected, f_reflected
        else:
            contracted = [centroid[j] + 0.5 * (worst[j] - centroid[j]) for j in range(n)]
            f_contracted = objective(contracted)
            if f_contracted < values[-1]:
                simplex[-1], values[-1] = contracted, f_contracted
            else:
                for i in range(1, n + 1):
                    simplex[i] = [
                        simplex[0][j] + 0.5 * (simplex[i][j] - simplex[0][j]) for j in range(n)
                    ]
                    values[i] = objective(simplex[i])
    best = min(range(n + 1), key=lambda i: values[i])
    return tuple(simplex[best]), values[best]


SKILL_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})


@dataclass(frozen=True)
class GroupFit:
    """One position group's fit: parameters, sample sizes, and both likelihoods."""

    group: str
    n: int
    n_events: int
    nll: float
    nll_baseline: float
    values: tuple[float, ...]

    @property
    def improvement(self) -> float:
        """How much log-likelihood the refit buys over the shipped constants."""
        return self.nll_baseline - self.nll


@dataclass(frozen=True)
class SurvivalFit:
    """A full :class:`SurvivalParams` re-fit plus the per-group diagnostics."""

    params: SurvivalParams
    baseline: SurvivalParams
    skill: GroupFit
    kicker: GroupFit
    defense: GroupFit
    sloped_kicker: GroupFit
    sloped_defense: GroupFit

    @property
    def groups(self) -> tuple[GroupFit, ...]:
        return (self.skill, self.kicker, self.defense)


def fit_survival_params(
    observations: Sequence[SurvivalObservation],
    *,
    baseline: SurvivalParams = DEFAULT_SURVIVAL_PARAMS,
) -> SurvivalFit:
    """Maximum-likelihood re-fit of the shipped survival constants.

    Three independent fits in the SHIPPED shape (skill center linear in rank;
    K and DST centers flat), plus two extra fits that give K and DST a rank
    slope. Those last two are diagnostics, not outputs: they are how
    :data:`KDST_SHAPE_IS_MISSPECIFIED` was established, and their numbers are
    reported so a later agent can decide whether to change the shape.

    Deterministic: the optimizer starts from ``baseline`` every time.
    """
    skill = [o for o in observations if o.position in SKILL_POSITIONS]
    kicker = [o for o in observations if o.position == "K"]
    defense = [o for o in observations if o.position == "DST"]
    for name, rows in (("skill", skill), ("K", kicker), ("DST", defense)):
        if not any(not o.censored for o in rows):
            raise ValueError(f"cannot fit {name}: no uncensored room picks in the sample")

    def flat(rows, start_center, start_width):
        def objective(p):
            return _negative_log_likelihood(rows, lambda _r: p[0], p[1])

        return _nelder_mead(objective, (start_center, start_width), (6.0, 1.5)), objective

    def linear(rows, start):
        def objective(p):
            return _negative_log_likelihood(rows, lambda r: p[0] + p[1] * r, p[2])

        return _nelder_mead(objective, start, (3.0, 0.05, 1.0)), objective

    def counts(rows):
        return len(rows), sum(1 for o in rows if not o.censored)

    (skill_v, skill_nll), skill_obj = linear(
        skill,
        (
            baseline.skill_center_intercept,
            baseline.skill_center_slope,
            baseline.skill_width,
        ),
    )
    (k_v, k_nll), k_obj = flat(kicker, baseline.k_center, baseline.k_width)
    (d_v, d_nll), d_obj = flat(defense, baseline.dst_center, baseline.dst_width)
    (ks_v, ks_nll), _ = linear(kicker, (baseline.k_center, 0.0, baseline.k_width))
    (ds_v, ds_nll), _ = linear(defense, (baseline.dst_center, 0.0, baseline.dst_width))

    skill_n, skill_e = counts(skill)
    k_n, k_e = counts(kicker)
    d_n, d_e = counts(defense)
    return SurvivalFit(
        params=SurvivalParams(
            skill_center_intercept=skill_v[0],
            skill_center_slope=skill_v[1],
            skill_width=skill_v[2],
            k_center=k_v[0],
            k_width=k_v[1],
            dst_center=d_v[0],
            dst_width=d_v[1],
        ),
        baseline=baseline,
        skill=GroupFit(
            "skill",
            skill_n,
            skill_e,
            skill_nll,
            skill_obj(
                (
                    baseline.skill_center_intercept,
                    baseline.skill_center_slope,
                    baseline.skill_width,
                )
            ),
            skill_v,
        ),
        kicker=GroupFit(
            "K", k_n, k_e, k_nll, k_obj((baseline.k_center, baseline.k_width)), k_v
        ),
        defense=GroupFit(
            "DST", d_n, d_e, d_nll, d_obj((baseline.dst_center, baseline.dst_width)), d_v
        ),
        sloped_kicker=GroupFit("K+rank", k_n, k_e, ks_nll, k_nll, ks_v),
        sloped_defense=GroupFit("DST+rank", d_n, d_e, ds_nll, d_nll, ds_v),
    )


@dataclass(frozen=True)
class HeldOutFold:
    """One leave-one-journal-out fold: fit on the rest, score the one held out."""

    journal: str
    params: SurvivalParams
    shipped: Calibration
    refit: Calibration


@dataclass(frozen=True)
class CrossValidation:
    """Leave-one-journal-out validation of the re-fit.

    The re-fit is only interesting if it generalises to a room it has never
    seen. Each fold fits :func:`fit_survival_params` on ten journals and scores
    the analytic route on the eleventh, which is the only honest way to tell a
    better model from a better memory of these particular eleven drafts.
    """

    folds: tuple[HeldOutFold, ...]

    @property
    def mean_shipped_brier(self) -> float:
        return statistics.fmean(f.shipped.brier for f in self.folds)

    @property
    def mean_refit_brier(self) -> float:
        return statistics.fmean(f.refit.brier for f in self.folds)

    @property
    def mean_shipped_bias(self) -> float:
        return statistics.fmean(f.shipped.bias for f in self.folds)

    @property
    def mean_refit_bias(self) -> float:
        return statistics.fmean(f.refit.bias for f in self.folds)

    @property
    def folds_improved(self) -> int:
        return sum(1 for f in self.folds if f.refit.brier < f.shipped.brier)


def cross_validate_refit(
    journals: Sequence[DraftJournal],
    boards: Mapping[str, Sequence[BoardEntry]],
    *,
    baseline: SurvivalParams = DEFAULT_SURVIVAL_PARAMS,
    probe_width: int = DEFAULT_PROBE_WIDTH,
) -> CrossValidation:
    """Leave-one-journal-out: fit on the others, score the analytic route here.

    The rollout route is deliberately NOT cross-validated — it has no fitted
    parameters of its own to leak; its constants live in ``priors.py`` and are
    2025 data, not these journals.
    """
    if len(journals) < 3:
        raise ValueError("leave-one-journal-out needs at least 3 journals")
    observations = {
        j.name: survival_observations(j, boards[j.name]) for j in journals
    }
    folds: list[HeldOutFold] = []
    for held in journals:
        train = [o for j in journals if j.name != held.name for o in observations[j.name]]
        fitted = fit_survival_params(train, baseline=baseline).params
        board = boards[held.name]
        folds.append(
            HeldOutFold(
                journal=held.name,
                params=fitted,
                shipped=calibrate(
                    analytic_points(held, board, params=baseline, probe_width=probe_width),
                    label=f"{held.name}/shipped",
                ),
                refit=calibrate(
                    analytic_points(held, board, params=fitted, probe_width=probe_width),
                    label=f"{held.name}/refit",
                ),
            )
        )
    return CrossValidation(folds=tuple(folds))


# ------------------------------------------- §2b THE NULL CONTROL (the fix)

#: Control rooms drawn per :func:`null_control` run. Matched to the real sample
#: (11 journals) so the control's sampling noise is the real sample's sampling
#: noise, not a smaller number dressed up as a bigger one.
DEFAULT_CONTROL_DRAFTS = 11


def simulate_journals(
    board: Sequence[BoardEntry],
    *,
    rng: random.Random,
    drafts: int = DEFAULT_CONTROL_DRAFTS,
    priors: RoomPriors = ROOM_PRIORS_2025,
    roster: RosterStructure | None = None,
    rounds: int = 16,
    operator_slot: int = 0,
    season: int = 2026,
    board_as_of: str = "",
) -> tuple[DraftJournal, ...]:
    """All-bot rooms, shaped as :class:`DraftJournal` so EVERY measurement runs on them.

    This is the control arm, and the point is that it is not a special code
    path: a simulated room enters :func:`analytic_points`,
    :func:`survival_observations` and :func:`kdst_timing` through the same door a
    real journal does, so "the real rooms say X" and "our own simulator says X"
    are produced by identical code and are directly comparable.

    One seat is nominated ``operator_slot``. It is still a bot — there is no
    engine in the control — but nominating it reproduces the real sample's
    geometry: the same seat is excluded from the room, the same windows are
    scored, and the same picks are censored rather than counted as room events.

    ``board_hash`` is left EMPTY on purpose. A control room was never drafted on
    a persisted board, so :func:`load_journal_board` must refuse to "verify" one
    rather than appear to succeed; nothing in the control path needs the hash.
    """
    from ziggurat.draft.bots import RankNoiseBot
    from ziggurat.draft.simulator import run_draft

    structure = roster if roster is not None else RosterStructure()
    out: list[DraftJournal] = []
    for i in range(drafts):
        pickers = [
            AutodraftBot() if rng.random() < priors.autodraft_fraction
            else RankNoiseBot(priors=priors)
            for _ in range(structure.teams)
        ]
        result = run_draft(
            board,
            pickers,
            rng=random.Random(rng.getrandbits(64)),
            roster=structure,
            rounds=rounds,
        )
        journal = DraftJournal(
            path=f"control-{i:02d}.jsonl",
            season=season,
            board_as_of=board_as_of,
            operator_slot=operator_slot,
            pick_order=tuple(range(structure.teams)),
            rounds=rounds,
            roster=structure,
            board_count=len(board),
            board_hash="",
            picks=tuple(
                JournalPick(overall=o, seat=t, player_id=pid)
                for o, t, pid in result.pick_log
            ),
        )
        _validate_journal(journal)  # the control obeys the real journals' invariants
        out.append(journal)
    return tuple(out)


@dataclass(frozen=True)
class NullControl:
    """The same measurement, run against rooms the model under test drew itself.

    WHY THIS EXISTS (it is the correction that reshaped this module). Without a
    control, "the shipped analytic constants are 0.370 pessimistic on real
    rooms" reads as a finding about human drafters, and the repair it suggests
    is "collect real journals and re-fit". Both are wrong. The same constants are
    ~0.33 pessimistic against RankNoiseBot rooms drawn from the very priors the
    curve was fitted to, and constants fitted on those SIMULATED rooms recover
    almost all of the re-fit's improvement when scored on the REAL ones. The
    defect is internal inconsistency in ``survival.py``'s analytic route, and the
    cheap repair is an offline re-fit against the simulator — no journals, no
    draft, no waiting.

    ``share_of_refit_gain_available_offline`` is the number to read: the
    fraction of the real-room Brier improvement that a control-only fit already
    buys. Near 1.0 means "we learned about our own model"; near 0.0 would mean
    "we learned about real drafters".
    """

    drafts: int
    board_as_of: str
    operator_slot: int
    control_shipped: Calibration
    control_fit: SurvivalFit
    control_fit_on_real: Calibration
    real_shipped: Calibration
    real_refit: Calibration
    real_fit: SurvivalFit

    @property
    def refit_gain(self) -> float:
        """Brier the real-room re-fit buys on real rooms."""
        return self.real_shipped.brier - self.real_refit.brier

    @property
    def offline_gain(self) -> float:
        """Brier a CONTROL-ONLY re-fit buys on the same real rooms."""
        return self.real_shipped.brier - self.control_fit_on_real.brier

    @property
    def share_of_refit_gain_available_offline(self) -> float:
        if self.refit_gain <= 0:
            return float("nan")
        return self.offline_gain / self.refit_gain

    @property
    def verdict(self) -> str:
        """Plain language, for a novice reader (Rule 6)."""
        share = self.share_of_refit_gain_available_offline
        if share != share:  # NaN
            return "the re-fit buys nothing on real rooms; nothing to attribute"
        if share >= 0.75:
            return (
                "MOSTLY A FINDING ABOUT OUR OWN MODEL: a fit that never saw a "
                "real draft recovers most of the gain"
            )
        if share >= 0.25:
            return "MIXED: part model repair, part real-room signal"
        return "MOSTLY A REAL-ROOM FINDING: the control does not reproduce it"


def null_control(
    journals: Sequence[DraftJournal],
    boards: Mapping[str, Sequence[BoardEntry]],
    *,
    rng: random.Random,
    drafts: int = DEFAULT_CONTROL_DRAFTS,
    priors: RoomPriors = ROOM_PRIORS_2025,
    baseline: SurvivalParams = DEFAULT_SURVIVAL_PARAMS,
    probe_width: int = DEFAULT_PROBE_WIDTH,
) -> NullControl:
    """Run sections 1-2 against all-bot rooms and attribute the re-fit's gain.

    The control board is the LATEST journal's board (the most recent market the
    real rooms drafted on), and the control's roster/rounds/operator seat are
    that journal's, so the two arms differ in exactly one thing: who is picking.
    """
    if not journals:
        raise ValueError("null_control needs at least one real journal to compare against")
    latest = max(journals, key=lambda j: j.board_as_of)
    board = boards[latest.name]

    control = simulate_journals(
        board,
        rng=rng,
        drafts=drafts,
        priors=priors,
        roster=latest.roster,
        rounds=latest.rounds,
        operator_slot=latest.operator_slot,
        season=latest.season,
        board_as_of=latest.board_as_of,
    )
    control_points = [
        pt
        for j in control
        for pt in analytic_points(j, board, params=baseline, probe_width=probe_width)
    ]
    control_fit = fit_survival_params(
        [o for j in control for o in survival_observations(j, board)], baseline=baseline
    )
    real_fit = fit_survival_params(
        [o for j in journals for o in survival_observations(j, boards[j.name])],
        baseline=baseline,
    )

    def on_real(params: SurvivalParams, label: str) -> Calibration:
        return calibrate(
            [
                pt
                for j in journals
                for pt in analytic_points(
                    j, boards[j.name], params=params, probe_width=probe_width
                )
            ],
            label=label,
        )

    return NullControl(
        drafts=len(control),
        board_as_of=latest.board_as_of,
        operator_slot=latest.operator_slot,
        control_shipped=calibrate(control_points, label="control/shipped"),
        control_fit=control_fit,
        control_fit_on_real=on_real(control_fit.params, "real/control-fit"),
        real_shipped=on_real(baseline, "real/shipped"),
        real_refit=on_real(real_fit.params, "real/refit"),
        real_fit=real_fit,
    )


# ------------------------------------------------------- §3 the K/DST question


@dataclass(frozen=True)
class KdstPlayerRow:
    """One K or D/ST: what ESPN says about him, and when the room actually took him."""

    name: str | None
    position: str
    espn_overall_rank: int
    espn_adp: float | None
    adp_at_ceiling: bool
    house_vor: float
    room_picks: tuple[int, ...]
    operator_picks: tuple[int, ...]

    @property
    def median_room_pick(self) -> float | None:
        return statistics.median(self.room_picks) if self.room_picks else None

    @property
    def adp_error(self) -> float | None:
        """Median room pick minus ESPN ADP. Positive = ADP is EARLY."""
        median = self.median_room_pick
        if median is None or self.espn_adp is None:
            return None
        return median - self.espn_adp


@dataclass(frozen=True)
class KdstTiming:
    """The measured answer to 'when does a 10-team room take a K or a defense?'

    PICKS AND SEATS ARE DIFFERENT DENOMINATORS AND BOTH ARE CARRIED. An earlier
    version reported ``n_seats = len(all_room_picks)`` and the renderer printed
    "100 rival seats over 11 drafts" for D/ST — arithmetically impossible, since
    11 drafts x 9 rivals is 99 seats. The extra pick is real: one rival seat
    drafted two defenses. For the novice reader Rule 6 targets, "10/100 rival
    seats took a D/ST before pick 140" reads as ten managers out of a hundred;
    it was ten PICKS out of a hundred made by 99 managers. So:

    * ``n_room_picks``  — room picks at this position (the pick denominator);
    * ``n_seats``       — distinct (journal, seat) pairs that took at least one;
    * ``n_rival_seats`` — every rival seat in the sample, whether it took one or
      not (``journals x (teams - 1)``), which is the only denominator a
      "how many managers do this?" sentence may use;
    * ``picks_before[c]`` — room picks before overall ``c``;
    * ``seats_before[c]`` — distinct seats whose FIRST pick here is before ``c``.
    """

    position: str
    n_journals: int
    n_room_picks: int
    n_seats: int
    n_rival_seats: int
    first_room_pick_per_draft: tuple[int, ...]
    all_room_picks: tuple[int, ...]
    operator_picks: tuple[int, ...]
    picks_before: Mapping[int, int]
    seats_before: Mapping[int, int]
    players: tuple[KdstPlayerRow, ...]

    @property
    def median_first(self) -> float:
        return statistics.median(self.first_room_pick_per_draft)

    @property
    def median_seat(self) -> float:
        return statistics.median(self.all_room_picks)

    @property
    def earliest(self) -> int:
        return min(self.all_room_picks)


DEFAULT_KDST_CUTS: tuple[int, ...] = (90, 100, 110, 120, 130, 140)


def _adp_for(
    adp_by_as_of: Mapping[str, AdpTable] | None, journal: DraftJournal
) -> AdpTable | None:
    """The ADP snapshot that was knowable when THIS journal was drafted.

    ADP moves between the 2026-08-16 and 2026-08-29 journals, so a single table
    would silently price an older draft with a newer market. Keyed on the
    journal's own recorded ``board_as_of``, the same key its board is loaded at.
    """
    if adp_by_as_of is None:
        return None
    return adp_by_as_of.get(journal.board_as_of)


def kdst_timing(
    journals: Sequence[DraftJournal],
    boards: Mapping[str, Sequence[BoardEntry]],
    *,
    position: str,
    adp_by_as_of: Mapping[str, AdpTable] | None = None,
    cuts: Sequence[int] = DEFAULT_KDST_CUTS,
    top_by_vor: int = 6,
) -> KdstTiming:
    """When the ROOM takes this position, across every journal.

    ``boards`` maps ``journal.name -> board``; ``adp_by_as_of`` maps
    ``journal.board_as_of -> AdpTable`` so each draft is compared against the ADP
    that existed on its own day. A player's reported ADP is the median across the
    journals he appears in. The operator's own picks are reported separately and
    NEVER counted as room behaviour — this module exists to test the engine
    against the room, and the engine's whole K/DST divergence play is to take
    these positions ~55 picks before the room does.
    """
    firsts: list[int] = []
    seats: list[int] = []
    ours: list[int] = []
    per_player: dict[str, dict] = {}
    seat_first: dict[tuple[str, int], int] = {}
    rival_seats = sum(journal.teams - 1 for journal in journals)

    for journal in journals:
        board = boards[journal.name]
        table = _adp_for(adp_by_as_of, journal)
        by_id = {e.player_id: e for e in board}
        room_here: list[int] = []
        for pick in journal.picks:
            entry = by_id.get(pick.player_id)
            if entry is None or entry.position != position:
                continue
            bucket = per_player.setdefault(
                pick.player_id, {"entry": entry, "room": [], "ours": [], "adp": []}
            )
            if table is not None:
                value = table.get(entry)
                if value is not None:
                    bucket["adp"].append((value, table.censored(entry)))
            if pick.seat == journal.operator_slot:
                ours.append(pick.overall)
                bucket["ours"].append(pick.overall)
            else:
                room_here.append(pick.overall)
                seats.append(pick.overall)
                bucket["room"].append(pick.overall)
                key = (journal.name, pick.seat)
                seat_first[key] = min(seat_first.get(key, pick.overall), pick.overall)
        if room_here:
            firsts.append(min(room_here))

    if not seats:
        raise ValueError(f"no room picks at position {position!r} in these journals")

    rows = [
        KdstPlayerRow(
            name=b["entry"].name,
            position=position,
            espn_overall_rank=b["entry"].espn_overall_rank,
            espn_adp=statistics.median(v for v, _c in b["adp"]) if b["adp"] else None,
            adp_at_ceiling=bool(b["adp"]) and all(c for _v, c in b["adp"]),
            house_vor=b["entry"].vor,
            room_picks=tuple(sorted(b["room"])),
            operator_picks=tuple(sorted(b["ours"])),
        )
        for b in per_player.values()
    ]
    rows.sort(key=lambda r: -r.house_vor)
    return KdstTiming(
        position=position,
        n_journals=len(journals),
        n_room_picks=len(seats),
        n_seats=len(seat_first),
        n_rival_seats=rival_seats,
        first_room_pick_per_draft=tuple(sorted(firsts)),
        all_room_picks=tuple(sorted(seats)),
        operator_picks=tuple(sorted(ours)),
        picks_before={c: sum(1 for p in seats if p < c) for c in cuts},
        seats_before={
            c: sum(1 for first in seat_first.values() if first < c) for c in cuts
        },
        players=tuple(rows[:top_by_vor]),
    )


@dataclass(frozen=True)
class AdpRescale:
    """OLS of ``median room pick`` on ``ESPN ADP`` for one position group.

    The point of this is the SLOPE. A pure league-size contamination ("ESPN
    pools 12-team rooms") is a uniform rescaling: every position's slope should
    land near ``10 / pooled_teams``. A slope that collapses at one position
    means the contamination is not league size at that position — BUT ONLY IF
    THE DATA COULD HAVE EXPRESSED THE BIGGER SLOPE, which at K and D/ST it
    cannot. A 10-team room takes every kicker inside a ~10-pick window at the
    end of a 160-pick draft, so the dependent variable has almost no range:
    ``max_expressible_slope`` (the y-span over the x-span) is the largest slope
    these observations could produce at all. Where it sits below 0.833, the
    comparison against 0.833 CANNOT test the league-size hypothesis, and
    :func:`render_report` says so instead of inviting the reader to reject it.
    The independent evidence for a K/DST shape problem is the likelihood test in
    section 2 (and its control), not this slope.
    """

    group: str
    n: int
    intercept: float
    slope: float
    r_squared: float
    mean_adp: float
    mean_room_pick: float
    adp_span: float = 0.0
    room_pick_span: float = 0.0

    @property
    def max_expressible_slope(self) -> float:
        """Largest slope the observed ranges could produce (range restriction)."""
        if self.adp_span <= 0:
            return float("inf")
        return self.room_pick_span / self.adp_span


def adp_rescale(
    journals: Sequence[DraftJournal],
    boards: Mapping[str, Sequence[BoardEntry]],
    adp_by_as_of: Mapping[str, AdpTable],
    *,
    positions: Iterable[str],
    group: str,
    min_drafts: int = 6,
) -> AdpRescale | None:
    """Fit the ADP -> room-pick rescale for ``positions``. ``None`` if too thin.

    Only players the room took in at least ``min_drafts`` journals enter (a
    single observation is a coin flip, not a location), and players whose ADP
    sits in the undrafted plateau band in every journal are dropped — a censored
    value is not a position. Each journal contributes its own day's ADP, keyed on
    its own ``board_as_of``: ADP moves between 2026-08-16 and 2026-08-29, and
    pricing an older draft with a newer market is the failure :func:`_adp_for`
    exists to prevent.
    """
    wanted = {str(p).upper() for p in positions}
    picks: dict[str, list[int]] = defaultdict(list)
    adps: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for journal in journals:
        by_id = {e.player_id: e for e in boards[journal.name]}
        table = _adp_for(adp_by_as_of, journal)
        for pick in journal.picks:
            if pick.seat == journal.operator_slot:
                continue
            entry = by_id.get(pick.player_id)
            if entry is None or entry.position not in wanted:
                continue
            picks[pick.player_id].append(pick.overall)
            if table is not None:
                value = table.get(entry)
                if value is not None:
                    adps[pick.player_id].append((value, table.censored(entry)))

    xs: list[float] = []
    ys: list[float] = []
    for pid, overalls in picks.items():
        if len(overalls) < min_drafts:
            continue
        observed = adps.get(pid)
        if not observed or all(c for _v, c in observed):
            continue
        xs.append(statistics.median(v for v, _c in observed))
        ys.append(statistics.median(overalls))
    if len(xs) < 3:
        return None

    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = mean_y - slope * mean_x
    sst = sum((y - mean_y) ** 2 for y in ys)
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return AdpRescale(
        group=group,
        n=len(xs),
        intercept=intercept,
        slope=slope,
        r_squared=(1.0 - sse / sst) if sst > 0 else float("nan"),
        mean_adp=mean_x,
        mean_room_pick=mean_y,
        adp_span=max(xs) - min(xs),
        room_pick_span=max(ys) - min(ys),
    )


@dataclass(frozen=True)
class SimulatedRoom:
    """The room model's OWN behaviour, measured with the same statistics.

    A generative check, and the only fair comparison for the room priors: the
    calibration in §1 scores survival probabilities, but this asks the blunter
    question — if we run the calibrated room forward with no operator in it, does
    it draft kickers and defenses when a real room does?
    """

    drafts: int
    first_dst: tuple[int, ...]
    first_k: tuple[int, ...]
    all_dst: tuple[int, ...]
    all_k: tuple[int, ...]
    realized_reach_sigma: float
    realized_reach_center: float

    @property
    def median_first_dst(self) -> float:
        return statistics.median(self.first_dst)

    @property
    def median_first_k(self) -> float:
        return statistics.median(self.first_k)

    @property
    def median_dst(self) -> float:
        return statistics.median(self.all_dst)

    @property
    def median_k(self) -> float:
        return statistics.median(self.all_k)


def simulate_room(
    board: Sequence[BoardEntry],
    *,
    rng: random.Random,
    drafts: int = 200,
    priors: RoomPriors = ROOM_PRIORS_2025,
    roster: RosterStructure | None = None,
    rounds: int = 16,
) -> SimulatedRoom:
    """Run the calibrated room against itself and measure it like a real room.

    Every seat is a room bot (``AutodraftBot`` sampled at
    ``priors.autodraft_fraction``, ``RankNoiseBot`` otherwise) — there is no
    operator, because the question is what the ROOM does. ``realized_reach_*``
    come from the SHIPPED ``recalibrate_from_pick_log``, i.e. the same estimator
    :func:`room_fit` runs on the real journals, which is what makes the two
    numbers comparable at all (see
    :data:`REACH_SIGMA_IS_AN_INPUT_NOT_AN_OUTPUT`).
    """
    from ziggurat.draft.bots import RankNoiseBot
    from ziggurat.draft.simulator import run_draft

    structure = roster if roster is not None else RosterStructure()
    firsts_dst: list[int] = []
    firsts_k: list[int] = []
    every_dst: list[int] = []
    every_k: list[int] = []
    sigmas: list[float] = []
    centers: list[float] = []
    by_id = {e.player_id: e for e in board}

    for _ in range(drafts):
        pickers = [
            AutodraftBot() if rng.random() < priors.autodraft_fraction
            else RankNoiseBot(priors=priors)
            for _ in range(structure.teams)
        ]
        result = run_draft(
            board,
            pickers,
            rng=random.Random(rng.getrandbits(64)),
            roster=structure,
            rounds=rounds,
        )
        dst = [o for o, _t, pid in result.pick_log if by_id[pid].position == "DST"]
        kick = [o for o, _t, pid in result.pick_log if by_id[pid].position == "K"]
        if dst:
            firsts_dst.append(min(dst))
            every_dst.extend(dst)
        if kick:
            firsts_k.append(min(kick))
            every_k.extend(kick)
        recal = recalibrate_from_pick_log(
            list(result.pick_log), board, operator_slot=None, min_room_picks=20
        )
        if recal.reach_sigma is not None:
            sigmas.append(recal.reach_sigma)
            centers.append(recal.reach_center)

    if not sigmas:
        raise ValueError("simulated room produced no usable reach sample")
    return SimulatedRoom(
        drafts=drafts,
        first_dst=tuple(sorted(firsts_dst)),
        first_k=tuple(sorted(firsts_k)),
        all_dst=tuple(sorted(every_dst)),
        all_k=tuple(sorted(every_k)),
        realized_reach_sigma=statistics.median(sigmas),
        realized_reach_center=statistics.median(centers),
    )


@dataclass(frozen=True)
class PriorVariant:
    """One row of the prior A/B, with the knobs it moves DERIVED, not asserted.

    An earlier A/B of these priors reported a row labelled
    "autodraft_fraction=0.0 -> Brier 0.1036 / bias +0.0225" in a table whose
    other rows moved one knob at a time. Re-measured 2026-08-30 (cold-start
    priors, R=200, the settings that table used): moving ONLY
    ``autodraft_fraction`` to 0.0 gives 0.1095 / +0.0320, which is WORSE than
    that table's own 0.1057 reference row; 0.1036 is reproducible only with
    ``reach_sigma`` moved to 13.25 as well (measured 0.1038 / +0.0227). Read as
    listed, the row argued for believing something the shipped priors say is
    worse — about a question CLAUDE.md records as measured and closed. So the
    label is no longer written by hand: :attr:`changes` diffs this variant
    against the shipped baseline field by field, and :func:`render_report`
    prints that diff, which makes a silently-two-knob row impossible to state.
    """

    label: str
    priors: RoomPriors = ROOM_PRIORS_2025
    kappa: float = DEFAULT_KAPPA
    live_recalibration: bool = True

    def changes(
        self,
        *,
        baseline: RoomPriors = ROOM_PRIORS_2025,
        baseline_kappa: float = DEFAULT_KAPPA,
        baseline_live: bool = True,
    ) -> tuple[str, ...]:
        """Every knob that differs from the shipped baseline, as ``name a -> b``."""
        out: list[str] = []
        for f in dataclasses.fields(self.priors):
            mine = getattr(self.priors, f.name)
            theirs = getattr(baseline, f.name)
            if mine != theirs:
                out.append(f"{f.name} {theirs} -> {mine}")
        if self.kappa != baseline_kappa:
            out.append(f"kappa {baseline_kappa} -> {self.kappa}")
        if self.live_recalibration != baseline_live:
            out.append(
                "live recalibration "
                f"{'on' if baseline_live else 'off'} -> "
                f"{'on' if self.live_recalibration else 'off'}"
            )
        return tuple(out)


def default_prior_variants() -> tuple[PriorVariant, ...]:
    """The one-knob-at-a-time A/B this module publishes (never a default swap)."""
    return (
        PriorVariant("shipped priors, live recalibration (the cockpit)"),
        PriorVariant("cold-start priors only", live_recalibration=False),
        PriorVariant("no kappa widening", kappa=1.0),
        PriorVariant("reach_sigma 13.25 (MEASURED_ROOM_2026)", priors=MEASURED_ROOM_2026),
        PriorVariant(
            "autodraft_fraction 0.00",
            priors=dataclasses.replace(ROOM_PRIORS_2025, autodraft_fraction=0.0),
        ),
    )


@dataclass(frozen=True)
class PriorComparison:
    """One A/B row: a labelled variant and what it scored on the real rooms."""

    variant: PriorVariant
    calibration: Calibration


def compare_priors(
    journals: Sequence[DraftJournal],
    boards: Mapping[str, Sequence[BoardEntry]],
    *,
    rng: random.Random,
    variants: Sequence[PriorVariant],
    rollouts: int = DEFAULT_MEASUREMENT_ROLLOUTS,
    probe_width: int = DEFAULT_PROBE_WIDTH,
) -> tuple[PriorComparison, ...]:
    """Score every variant on the SAME rooms with the SAME draws (paired).

    Pairing matters more than it looks: at R=200 the whole-sample Brier carries
    ~0.001 of seed noise (:data:`MEASUREMENT_NOISE`) and several of these knobs
    move it by less than 0.005, so an unpaired table would be reporting seeds.
    One base seed is drawn from ``rng`` and every variant replays it.
    """
    base = rng.getrandbits(64)
    out: list[PriorComparison] = []
    for variant in variants:
        stream = random.Random(base)
        points = [
            pt
            for j in journals
            for pt in rollout_points(
                j,
                boards[j.name],
                rng=stream,
                rollouts=rollouts,
                priors=variant.priors,
                kappa=variant.kappa,
                probe_width=probe_width,
                live_recalibration=variant.live_recalibration,
            )
        ]
        out.append(
            PriorComparison(variant=variant, calibration=calibrate(points, label=variant.label))
        )
    return tuple(out)


def reach_sigma_transfer(
    board: Sequence[BoardEntry],
    *,
    rng: random.Random,
    inputs: Sequence[float] = (5.0, 10.0, 13.25, 17.78, 23.1, 30.0),
    drafts: int = 100,
    priors: RoomPriors = ROOM_PRIORS_2025,
    roster: RosterStructure | None = None,
    rounds: int = 16,
) -> tuple[tuple[float, float], ...]:
    """``((input reach_sigma, realized reach_sigma), ...)`` — recompute the trap.

    :data:`REACH_SIGMA_IS_AN_INPUT_NOT_AN_OUTPUT` is the single most quotable
    number in section 4 and it is the one most likely to be misread as "the
    prior is too wide by the difference". This runs the transfer curve behind
    it, so the claim can be re-derived instead of remembered. 100 drafts per
    point holds the realized median to about +/-0.05 between seeds; 20 (what
    the first version used) does not hold two decimals at all.
    """
    out: list[tuple[float, float]] = []
    for value in inputs:
        room = simulate_room(
            board,
            rng=random.Random(rng.getrandbits(64)),
            drafts=drafts,
            priors=dataclasses.replace(priors, reach_sigma=value),
            roster=roster,
            rounds=rounds,
        )
        out.append((float(value), room.realized_reach_sigma))
    return tuple(out)


# ------------------------------------------------------------- §4 room priors


@dataclass(frozen=True)
class SeatBehaviour:
    """How one rival seat drafted, measured against a pure-board autodrafter.

    ``autodraft_match`` is the fraction of that seat's picks that are EXACTLY
    what :class:`~ziggurat.draft.bots.AutodraftBot` would have taken from the
    same board state and the same roster. It is a strict test and it is a test
    of OUR model of a bot: a real ESPN autodrafter uses ESPN's own ordering and
    roster logic, so a low match rate is evidence of "not a pure ESPN-board
    autodrafter as we model one", never proof of a human.

    ``median_slots_past_best`` is the softer companion: the median number of
    board slots between the player the seat took and the player a pure-board
    autodrafter would have taken. 0 means "hugged the board".
    """

    seat: int
    n_picks: int
    autodraft_match: float
    median_slots_past_best: float
    kdst_picks: Mapping[str, int]


@dataclass(frozen=True)
class RoomFit:
    """One journal's room, measured with the SHIPPED live-recalibration estimator."""

    journal: str
    n_room_picks: int
    reach_sigma: float | None
    reach_center: float | None
    board_adherence_pearson: float | None
    seats: tuple[SeatBehaviour, ...]

    @property
    def autodraft_like(self) -> int:
        """Seats whose picks match a pure-board autodrafter at least 80% of the time."""
        return sum(1 for s in self.seats if s.autodraft_match >= 0.8)


def room_fit(journal: DraftJournal, board: Sequence[BoardEntry]) -> RoomFit:
    """Measure a real room: reach spread + per-seat board adherence.

    The reach half is delegated to the SHIPPED
    ``survival.recalibrate_from_pick_log`` — the same estimator the cockpit runs
    live, with the same operator/K-DST/unranked exclusions — so the number this
    reports is exactly the number draft night would have computed.
    """
    log = [(q.overall, q.seat, q.player_id) for q in journal.picks]
    recal = recalibrate_from_pick_log(
        log, board, operator_slot=journal.operator_slot, min_room_picks=20
    )

    by_id = {e.player_id: e for e in board}
    bot = AutodraftBot()
    state = BoardState(board)
    rosters: dict[int, list[BoardEntry]] = defaultdict(list)
    matches: dict[int, list[bool]] = defaultdict(list)
    deviations: dict[int, list[int]] = defaultdict(list)
    kdst: dict[int, dict[str, int]] = defaultdict(dict)

    for pick in journal.picks:
        entry = by_id[pick.player_id]
        if pick.seat != journal.operator_slot:
            ctx = PickContext(
                team_slot=pick.seat,
                round=(pick.overall - 1) // journal.teams + 1,
                overall_pick=pick.overall,
                rounds_total=journal.rounds,
                roster=journal.roster,
                own_roster=list(rosters[pick.seat]),
                state=state,
                rng=random.Random(0),  # AutodraftBot is deterministic; never drawn from
            )
            wanted = bot.pick(ctx)
            matches[pick.seat].append(wanted == pick.player_id)
            deviations[pick.seat].append(
                entry.espn_overall_rank - by_id[wanted].espn_overall_rank
            )
            if entry.position in KDST:
                kdst[pick.seat][entry.position] = pick.overall
        state.take(pick.player_id)
        rosters[pick.seat].append(entry)

    seats = tuple(
        SeatBehaviour(
            seat=seat,
            n_picks=len(flags),
            autodraft_match=sum(flags) / len(flags),
            median_slots_past_best=statistics.median(deviations[seat]),
            kdst_picks=dict(kdst[seat]),
        )
        for seat, flags in sorted(matches.items())
    )
    return RoomFit(
        journal=journal.name,
        n_room_picks=recal.n_room_picks,
        reach_sigma=recal.reach_sigma,
        reach_center=recal.reach_center,
        board_adherence_pearson=recal.board_adherence_pearson,
        seats=seats,
    )


# ------------------------------------------------------------------- reporting


@dataclass
class RoomcheckReport:
    """Everything the measurement produced, in one bag the renderer walks.

    ``rollout`` is the LIVE-recalibrated route (what the cockpit ran) and
    ``rollout_cold`` the cold-start contrast; ``analytic`` is the unconditional
    route as ``PickEngine`` consumes it and ``analytic_conditional`` the
    well-posed version of the same question. Every pair exists because reporting
    only one of them is how this module previously mis-attributed its headline.
    """

    journals: tuple[DraftJournal, ...] = ()
    files_seen: int = 0
    rollout: Calibration | None = None
    rollout_cold: Calibration | None = None
    rollout_engine: Calibration | None = None
    rollout_by_position: Mapping[object, Calibration] = field(default_factory=dict)
    rollout_by_round: Mapping[object, Calibration] = field(default_factory=dict)
    recalibration_engaged: tuple[int, int] = (0, 0)
    live_reach_sigma: tuple[float, ...] = ()
    analytic: Calibration | None = None
    analytic_conditional: Calibration | None = None
    analytic_refit: Calibration | None = None
    analytic_refit_conditional: Calibration | None = None
    fit: SurvivalFit | None = None
    control: NullControl | None = None
    cross_validation: CrossValidation | None = None
    dst: KdstTiming | None = None
    kicker: KdstTiming | None = None
    skill_rescale: AdpRescale | None = None
    dst_rescale: AdpRescale | None = None
    k_rescale: AdpRescale | None = None
    adp_census: Mapping[str, AdpCensus] = field(default_factory=dict)
    adp_coverage: Mapping[str, float] = field(default_factory=dict)
    rooms: tuple[RoomFit, ...] = ()
    simulated: SimulatedRoom | None = None
    prior_ab: tuple[PriorComparison, ...] = ()
    noise_probe: tuple[tuple[int, float], ...] = ()


def _fmt_cal(cal: Calibration) -> str:
    return (
        f"n={cal.n:<6d} predicted {cal.mean_predicted:.3f}  observed "
        f"{cal.observed_rate:.3f}  bias {cal.bias:+.3f}  Brier {cal.brier:.4f}"
    )


def render_report(report: RoomcheckReport) -> list[str]:
    """Plain-language rendering of the whole measurement (Rule 6).

    Pure: takes the report, returns lines. Every number is stated with its n,
    every direction is stated in words as well as sign, and every claim about
    "the real room" is stated NEXT TO the same measurement on rooms our own
    simulator drew — because this report is the artifact a later agent reads,
    and a qualifier that lives only in a docstring dies with the session.
    """
    out: list[str] = []
    add = out.append
    add("SURVIVAL MODEL vs REAL ROOMS — roomcheck")
    add("=" * 78)
    add(
        f"{len(report.journals)} completed real ESPN drafts out of "
        f"{report.files_seen} journal files (the rest are abandoned launches or "
        f"fragments); {sum(len(j.picks) for j in report.journals)} picks; boards "
        "rebuilt at each journal's own as_of and verified by board_hash."
    )

    if report.rollout is not None:
        add("")
        add("1. CALIBRATION — is S_next right?")
        add("-" * 78)
        add(f"  rollout, LIVE priors      {_fmt_cal(report.rollout)}")
        add(f"      -> {report.rollout.verdict}")
        add(
            "      (this is the cockpit: session._engine() hands PickEngine the "
            "live-recalibrated priors)"
        )
        engaged, windows = report.recalibration_engaged
        if windows:
            share = engaged / windows
            add(
                f"      live re-fit engaged at {engaged}/{windows} decisions "
                f"({share:.0%})"
                + (
                    f"; live reach_sigma median "
                    f"{statistics.median(report.live_reach_sigma):.2f} "
                    f"(range {min(report.live_reach_sigma):.2f}-"
                    f"{max(report.live_reach_sigma):.2f}) vs cold-start "
                    f"{ROOM_PRIORS_2025.reach_sigma:.2f}"
                    if report.live_reach_sigma
                    else ""
                )
            )
        if report.rollout_cold is not None:
            add(f"  rollout, COLD-START only  {_fmt_cal(report.rollout_cold)}")
            add(
                "      (the same windows scored on ROOM_PRIORS_2025 throughout — a "
                "room model the cockpit stops using after ~round 3; kept only as "
                "the contrast)"
            )
        if report.rollout_engine is not None:
            add(f"  rollout, engine set only  {_fmt_cal(report.rollout_engine)}")
            add(
                "      (the candidates PickEngine actually scored — the "
                "decision-relevant cohort)"
            )
        if report.analytic is not None:
            add(f"  analytic (fallback)       {_fmt_cal(report.analytic)}")
            add(f"      -> {report.analytic.verdict}")
        if report.analytic_conditional is not None:
            add(f"  analytic, CONDITIONED     {_fmt_cal(report.analytic_conditional)}")
        if report.analytic_refit is not None:
            add(f"  analytic, REFIT constants {_fmt_cal(report.analytic_refit)}")
        if report.analytic_refit_conditional is not None:
            add(
                "  analytic, REFIT + cond.   "
                f"{_fmt_cal(report.analytic_refit_conditional)}"
            )
        if report.analytic is not None and report.analytic_conditional is not None:
            add("")
            add(f"  {ANALYTIC_ROUTE_IS_UNCONDITIONAL}")
        add("")
        add(f"  {MEASUREMENT_NOISE}")
        if report.noise_probe:
            briers = [report.rollout.brier] + [b for _s, b in report.noise_probe]
            seeds = ", ".join(str(s) for s, _b in report.noise_probe)
            add(
                f"  noise probe (same measurement, seeds {seeds}): Brier "
                + ", ".join(f"{b:.4f}" for b in briers)
                + f"  -> spread {max(briers) - min(briers):.4f} over "
                f"{len(briers)} seeds. Quote three decimals, not four."
            )
        add("")
        add("  rollout reliability bins (what it promised vs what the room did):")
        for b in report.rollout.bins:
            add(
                f"    S in [{b.low:.2f},{b.high:.2f})  n={b.n:<5d} "
                f"predicted {b.mean_predicted:.3f}  observed {b.observed_rate:.3f}  "
                f"bias {b.bias:+.3f}"
            )
        if report.rollout_cold is not None:
            add(
                "    (these bins move, and some change SIGN, under cold-start "
                "priors — see LIVE_RECALIBRATION_IS_THE_ROOM_MODEL; read them as "
                "a property of the live room model, not of the room)"
            )
        if report.rollout_by_position:
            add("")
            add("  rollout by position:")
            for pos, cal in report.rollout_by_position.items():
                add(f"    {str(pos):<5s} {_fmt_cal(cal)}  {cal.verdict}")
        if report.rollout_by_round:
            add("")
            add("  rollout by round:")
            for rnd, cal in sorted(
                report.rollout_by_round.items(), key=lambda kv: int(kv[0])
            ):
                add(f"    R{int(rnd):<3d} {_fmt_cal(cal)}")

    if report.fit is not None:
        fit = report.fit
        add("")
        add("2. RE-FIT — SurvivalParams against real rooms (NOT shipped)")
        add("-" * 78)
        add(REFIT_PRACTICE_2026_LABEL)
        add("")
        add(f"  {'parameter':<26s} {'shipped':>10s} {'refit':>10s}")
        for name in (
            "skill_center_intercept",
            "skill_center_slope",
            "skill_width",
            "k_center",
            "k_width",
            "dst_center",
            "dst_width",
        ):
            add(
                f"  {name:<26s} {getattr(fit.baseline, name):>10.4f} "
                f"{getattr(fit.params, name):>10.4f}"
            )
        add("")
        for group in fit.groups:
            add(
                f"  {group.group:<9s} n={group.n:<6d} room events={group.n_events:<5d} "
                f"NLL {group.nll:>9.2f} (shipped {group.nll_baseline:>9.2f}, "
                f"better by {group.improvement:>8.2f})"
            )
        add("")
        add("  shape check — does a rank slope help K/DST?")
        control_fit = report.control.control_fit if report.control is not None else None
        for group, control_group in (
            (fit.sloped_kicker, control_fit.sloped_kicker if control_fit else None),
            (fit.sloped_defense, control_fit.sloped_defense if control_fit else None),
        ):
            add(
                f"    {group.group:<9s} center = {group.values[0]:.2f} + "
                f"{group.values[1]:.4f} * rank, width {group.values[2]:.2f}; "
                f"NLL {group.nll:.2f} vs {group.nll_baseline:.2f} flat"
            )
            if control_group is not None:
                add(
                    f"      CONTROL (all-bot rooms): slope {control_group.values[1]:.4f}; "
                    f"NLL {control_group.nll:.2f} vs {control_group.nll_baseline:.2f} "
                    "flat — the same rejection, with no humans in the room"
                )
        add(f"  {KDST_SHAPE_IS_MISSPECIFIED}")

    if report.control is not None:
        control = report.control
        add("")
        add("2b. NULL CONTROL — is any of this a finding about real drafters?")
        add("-" * 78)
        add(
            f"  {control.drafts} all-bot rooms drawn from the room model under "
            f"test, on the {control.board_as_of} board, scored through exactly the "
            "same code as the real journals."
        )
        add(f"    shipped constants on REAL rooms     {_fmt_cal(control.real_shipped)}")
        add(f"    shipped constants on CONTROL rooms  {_fmt_cal(control.control_shipped)}")
        add(
            "    -> the shipped analytic constants fail against the simulator they "
            "were fitted to. That is internal inconsistency, not a room surprise."
        )
        add("")
        add(f"    re-fit on REAL rooms, scored on REAL      {_fmt_cal(control.real_refit)}")
        add(
            f"    re-fit on CONTROL rooms, scored on REAL   "
            f"{_fmt_cal(control.control_fit_on_real)}"
        )
        share = control.share_of_refit_gain_available_offline
        add(
            f"    -> Brier gained by re-fitting: {control.refit_gain:.4f} total, of "
            f"which {control.offline_gain:.4f} "
            + (f"({share:.0%})" if share == share else "")
            + " needs NO real-room data at all."
        )
        add(f"    -> {control.verdict}")
        add("")
        add("    what the real rooms actually add, parameter by parameter:")
        add(
            f"      {'parameter':<24s} {'shipped':>9s} {'control fit':>12s} "
            f"{'real fit':>10s}"
        )
        for name in (
            "skill_center_intercept",
            "skill_center_slope",
            "skill_width",
            "k_center",
            "dst_center",
        ):
            add(
                f"      {name:<24s} "
                f"{getattr(control.control_fit.baseline, name):>9.3f} "
                f"{getattr(control.control_fit.params, name):>12.3f} "
                f"{getattr(control.real_fit.params, name):>10.3f}"
            )
        add(
            "      (both fits are far from the shipped column and near each other; "
            "that gap-to-shipped is the model defect, and the control-to-real "
            "column difference is all the room signal there is)"
        )
        add(
            "    So the cheap repair a Phase-2 agent should try FIRST is an offline "
            "re-fit of analytic_survival against the simulator — no journals, no "
            "draft, no waiting — and only then ask what the real rooms add. Note "
            "the control's own 'operator' seat is a bot: the control reproduces "
            "the sample's GEOMETRY (same seat excluded, same windows scored), not "
            "an engine."
        )

    if report.cross_validation is not None:
        cv = report.cross_validation
        add("")
        add("   held-out check (leave one journal out, fit on the other 10):")
        for fold in cv.folds:
            add(
                f"    {fold.journal:<34s} Brier {fold.shipped.brier:.4f} -> "
                f"{fold.refit.brier:.4f}   bias {fold.shipped.bias:+.3f} -> "
                f"{fold.refit.bias:+.3f}"
            )
        add(
            f"    MEAN over {len(cv.folds)} held-out rooms: Brier "
            f"{cv.mean_shipped_brier:.4f} -> {cv.mean_refit_brier:.4f}; bias "
            f"{cv.mean_shipped_bias:+.3f} -> {cv.mean_refit_bias:+.3f}; "
            f"{cv.folds_improved}/{len(cv.folds)} rooms improved"
        )
        add(
            "    THIS HOLDS OUT A ROOM, NOT THE GENERATING PROCESS: it shows the "
            "re-fit is not a memory of one journal, and says nothing about whether "
            "the room is human. Section 2b is the test that separates those."
        )

    for timing, title in ((report.dst, "D/ST"), (report.kicker, "K")):
        if timing is None:
            continue
        add("")
        add(f"3. WHEN DOES A 10-TEAM ROOM TAKE A {title}?")
        add("-" * 78)
        add(
            f"  {timing.n_room_picks} rival {title} picks by {timing.n_seats} of "
            f"{timing.n_rival_seats} rival seats over {timing.n_journals} drafts "
            f"(picks and seats differ when one seat takes two). First {title} per "
            f"draft: median {timing.median_first:.0f} "
            f"(earliest {min(timing.first_room_pick_per_draft)}). "
            f"Every rival {title}: median {timing.median_seat:.0f}."
        )
        for cut in sorted(timing.seats_before):
            add(
                f"    rival SEATS whose first {title} comes before pick {cut}: "
                f"{timing.seats_before[cut]}/{timing.n_rival_seats}   "
                f"({timing.picks_before[cut]}/{timing.n_room_picks} of the picks)"
            )
        if timing.operator_picks:
            add(f"  our own {title} picks: {list(timing.operator_picks)}")
        add(f"  the {title}s our house board likes most:")
        for row in timing.players:
            adp = f"{row.espn_adp:.1f}" if row.espn_adp is not None else "n/a"
            err = f"{row.adp_error:+.1f}" if row.adp_error is not None else "n/a"
            median = row.median_room_pick
            add(
                f"    {str(row.name):<18s} espn_rank {row.espn_overall_rank:>4d}  "
                f"adp {adp:>6s}  house_vor {row.house_vor:>6.1f}  "
                f"room median {median if median is None else round(median)}  "
                f"(room is {err} vs ADP)  n_room={len(row.room_picks)}"
            )

    if report.adp_census:
        add("")
        add("   how much of ESPN's ADP is the undrafted plateau? (computed, not recalled)")
        for as_of, census in report.adp_census.items():
            coverage = report.adp_coverage.get(as_of)
            add(
                f"    {as_of}: n={census.n}  at/above {170.0 - 0.5:.1f} "
                f"{census.at_plateau} ({census.plateau_share:.0%})  exactly 170.0 "
                f"{census.exactly_at_ceiling}  median {census.median:.2f}  max "
                f"{census.maximum:.2f}"
                + (f"  board coverage {coverage:.3f}" if coverage is not None else "")
            )
        add(
            "    The plateau is a BAND around 170, not a hard cap (values run past "
            "it), so 'ADP 170' means 'mostly undrafted in ESPN's pool', never "
            "'goes at pick 170'. Plateau players are dropped from the fit below."
        )

    if report.skill_rescale is not None:
        add("")
        add("   is ESPN ADP just a bigger league? (room_pick = a + b * adp)")
        for rescale in (report.skill_rescale, report.dst_rescale, report.k_rescale):
            if rescale is None:
                continue
            bound = rescale.max_expressible_slope
            add(
                f"    {rescale.group:<6s} b={rescale.slope:.4f}  a={rescale.intercept:.2f}  "
                f"R^2={rescale.r_squared:.3f}  n={rescale.n}   "
                f"[largest slope these data could express: {bound:.3f}]"
            )
        add(
            "    A 12-team pool rescaled to 10 teams predicts b = 10/12 = 0.833 at "
            "EVERY position."
        )
        restricted = [
            r
            for r in (report.skill_rescale, report.dst_rescale, report.k_rescale)
            if r is not None and r.max_expressible_slope < 0.833
        ]
        if restricted:
            add(
                "    RANGE RESTRICTION — READ BEFORE CONCLUDING ANYTHING: at "
                + ", ".join(r.group for r in restricted)
                + " the room compresses every pick into a handful of picks at the "
                "end of a 160-pick draft, so the largest slope the data could "
                "produce is already below 0.833. The low b there is NOT evidence "
                "against the league-size explanation; no covariate could have "
                "cleared the bar. (Note the R^2 in the same row: ADP still orders "
                "those players well — it just cannot spread them out.) The "
                "independent K/DST shape evidence is the likelihood test in "
                "section 2, and it has its own control."
            )

    if report.rooms:
        add("")
        add("4. THE ROOM PRIORS — reach spread and autodraft share")
        add("-" * 78)
        sigmas = [r.reach_sigma for r in report.rooms if r.reach_sigma is not None]
        centers = [r.reach_center for r in report.rooms if r.reach_center is not None]
        add(
            f"  reach_sigma across {len(sigmas)} real rooms: median "
            f"{statistics.median(sigmas):.2f} (range {min(sigmas):.2f}-{max(sigmas):.2f}); "
            f"shipped prior {ROOM_PRIORS_2025.reach_sigma:.2f}"
        )
        add(f"  reach_center (rank - pick): median {statistics.median(centers):+.2f}")
        add(f"  {REACH_SIGMA_IS_AN_INPUT_NOT_AN_OUTPUT}")
        seats = [s for r in report.rooms for s in r.seats]
        rates = sorted(s.autodraft_match for s in seats)
        hugging = sum(1 for s in seats if s.median_slots_past_best <= 2)
        add(
            f"  autodraft-likeness over {len(seats)} rival seats: median exact match "
            f"{statistics.median(rates):.2f}, max {max(rates):.2f}; seats at or above "
            f"0.80 = {sum(1 for r in rates if r >= 0.8)}"
        )
        add(
            f"  softer board-hugging measure: {hugging}/{len(seats)} seats sit within "
            "2 board slots of the pure-board pick at the median"
        )
        add(
            f"  shipped autodraft_fraction = {ROOM_PRIORS_2025.autodraft_fraction:.2f}. "
            "No rival seat here drafts like a pure-board autodrafter AS WE MODEL "
            "ONE — and that qualifier is the whole content of the measurement: a "
            "real ESPN autodrafter uses ESPN's own ordering and roster logic, so a "
            "low match rate is evidence of 'not our AutodraftBot', NEVER of 'a "
            "human'. This is not a reason to move the prior. The question was "
            "measured and closed on 2026-08-29: the engine's BELIEF about the "
            "autodraft share is decision-irrelevant (identical recommendations in "
            "77-80 of 80 paired draws for any belief in 0.0-0.3), and the room's "
            "true share is unknowable before the draft."
        )

    if report.prior_ab:
        add("")
        add("   room-prior A/B — one knob at a time, paired on the same draws:")
        for row in report.prior_ab:
            changes = row.variant.changes()
            add(f"    {row.variant.label:<44s} {_fmt_cal(row.calibration)}")
            add(
                "      knobs moved vs shipped: "
                + (", ".join(changes) if changes else "none (this is the reference row)")
            )
        briers = [row.calibration.brier for row in report.prior_ab]
        spread = max(briers) - min(briers)
        add(
            f"    Whole-sample Brier across every variant spans "
            f"{min(briers):.4f}-{max(briers):.4f} (spread {spread:.4f}, "
            f"{spread / min(briers):.0%} of the best row) against a measured seed "
            "noise of ~0.001. So the ordering is real but the effects are small."
        )
        add(
            "    A row that scores slightly better here is NOT by itself a reason "
            "to change a shipped prior: this is a survival-probability score, and "
            "the quantity that matters is which player the engine RECOMMENDS. "
            "That was measured separately on 2026-08-29 and is invariant across "
            "autodraft beliefs 0.0-0.3 (77-80 of 80 paired draws identical), "
            "which is why CLAUDE.md records the prior as closed at 0.20. Judge "
            "any proposed change on recommendations, not on this table."
        )
        add(
            "    Every row lists the knobs it moved because the diff is DERIVED "
            "from the variant, not typed by hand: a row that silently moved two "
            "knobs (and was read as one) is what made an earlier version of this "
            "A/B misreport the autodraft prior as an improvement."
        )
    elif report.rollout is not None:
        add("")
        add(
            "   room-prior A/B: NOT RUN (each variant is a full extra rollout pass). "
            "Run `python -m ziggurat.draft.roomcheck --priors-ab`, or pass "
            "prior_variants=default_prior_variants(). Do not quote a prior "
            "comparison that this table did not produce."
        )

    if report.simulated is not None:
        sim = report.simulated
        real_sigmas = [r.reach_sigma for r in report.rooms if r.reach_sigma is not None]
        real_centers = [r.reach_center for r in report.rooms if r.reach_center is not None]
        add("")
        add(
            f"  generative check — the SAME statistics on {sim.drafts} all-bot drafts "
            "of the calibrated room (no operator seat):"
        )

        def _pair(label: str, simulated: float, real: float | None) -> str:
            tail = f"  vs real {real:.0f}" if real is not None else "  (no real rooms)"
            return f"    {label:<12s} sim {simulated:.0f}{tail}"

        add(_pair("first D/ST", sim.median_first_dst,
                  report.dst.median_first if report.dst else None))
        add(_pair("every D/ST", sim.median_dst,
                  report.dst.median_seat if report.dst else None))
        add(_pair("first K", sim.median_first_k,
                  report.kicker.median_first if report.kicker else None))
        add(_pair("every K", sim.median_k,
                  report.kicker.median_seat if report.kicker else None))
        add(
            f"    realized reach sigma  sim {sim.realized_reach_sigma:.2f}"
            + (
                f"  vs real {statistics.median(real_sigmas):.2f}   (this, not "
                f"{ROOM_PRIORS_2025.reach_sigma:.2f}, is the like-for-like comparison)"
                if real_sigmas
                else "  (no real rooms to compare against)"
            )
        )
        add(
            f"    realized reach center sim {sim.realized_reach_center:+.2f}"
            + (
                f"  vs real {statistics.median(real_centers):+.2f}"
                if real_centers
                else "  (no real rooms to compare against)"
            )
        )
    return out


# ------------------------------------------------------------------ the runner


def run_roomcheck(
    conn,
    directory: str | Path,
    *,
    rollouts: int = DEFAULT_MEASUREMENT_ROLLOUTS,
    seed: int = 20260830,
    priors: RoomPriors = ROOM_PRIORS_2025,
    kappa: float = DEFAULT_KAPPA,
    sim_drafts: int = 200,
    control_drafts: int = DEFAULT_CONTROL_DRAFTS,
    prior_variants: Sequence[PriorVariant] | None = None,
    noise_seeds: Sequence[int] = (),
) -> RoomcheckReport:
    """Load every completed journal, rebuild its board, and run all five sections.

    The DB is read only through :func:`load_journal_board` / :func:`load_espn_adp`,
    each at the journal's OWN recorded ``as_of`` (Rule 1). Nothing is written.
    Boards are cached per ``(season, as_of)`` — three distinct snapshots serve
    eleven journals — but every journal is still verified against its own
    ``board_hash``, so the cache can never substitute one draft's board for
    another's.

    ``prior_variants`` is OFF by default (each variant is a full extra rollout
    pass over every window). Pass :func:`default_prior_variants` for the labelled
    one-knob-at-a-time A/B; the report says plainly when it did not run.

    ``noise_seeds`` re-runs the headline rollout at other seeds so the
    Monte-Carlo noise floor (:data:`MEASUREMENT_NOISE`) is MEASURED in the same
    run that quotes it. An earlier version of this module recalled that floor
    from a comment, and the recalled numbers did not reproduce.
    """
    files_seen = len(list(Path(directory).glob("*.jsonl")))
    journals = load_journals(directory)
    if not journals:
        raise ValueError(f"no completed draft journals under {directory}")

    boards: dict[str, tuple[BoardEntry, ...]] = {}
    board_cache: dict[tuple[int, str], tuple[BoardEntry, ...]] = {}
    adp_by_as_of: dict[str, AdpTable] = {}
    for journal in journals:
        key = (journal.season, journal.board_as_of)
        cached = board_cache.get(key)
        if cached is None:
            cached = load_journal_board(conn, journal, as_of=journal.board_as_of)
            board_cache[key] = cached
        else:
            # Same snapshot, different journal: re-verify the hash rather than
            # trusting the cache (a journal whose header disagrees must still
            # fail loudly — that check is this module's Rule-1 tripwire).
            _verify_board(cached, journal, as_of=journal.board_as_of)
        boards[journal.name] = cached
        if journal.board_as_of not in adp_by_as_of:
            adp_by_as_of[journal.board_as_of] = load_espn_adp(
                conn, as_of=journal.board_as_of, season=journal.season
            )

    latest = max(journals, key=lambda j: j.board_as_of)
    rng = random.Random(seed)
    rollout: list[SurvivalPoint] = []
    rollout_cold: list[SurvivalPoint] = []
    analytic: list[SurvivalPoint] = []
    analytic_conditional: list[SurvivalPoint] = []
    refit_points: list[SurvivalPoint] = []
    refit_conditional: list[SurvivalPoint] = []
    observations: list[SurvivalObservation] = []
    rooms: list[RoomFit] = []
    engaged = 0
    windows_total = 0
    live_sigmas: list[float] = []
    for journal in journals:
        board = boards[journal.name]
        rollout.extend(
            rollout_points(
                journal, board, rng=rng, rollouts=rollouts, priors=priors, kappa=kappa
            )
        )
        analytic.extend(analytic_points(journal, board))
        analytic_conditional.extend(analytic_points(journal, board, conditional=True))
        refit_points.extend(analytic_points(journal, board, params=REFIT_PRACTICE_2026))
        refit_conditional.extend(
            analytic_points(journal, board, params=REFIT_PRACTICE_2026, conditional=True)
        )
        observations.extend(survival_observations(journal, board))
        rooms.append(room_fit(journal, board))
        for window in operator_windows(journal, board):
            windows_total += 1
            recal = window.live_recalibration(board, base_priors=priors)
            if recal.engaged and recal.reach_sigma is not None:
                engaged += 1
                live_sigmas.append(recal.reach_sigma)

    # The cold-start contrast replays the SAME seed, so the live-vs-cold gap is
    # a priors gap and not a Monte-Carlo one (MEASUREMENT_NOISE).
    cold_rng = random.Random(seed)
    for journal in journals:
        rollout_cold.extend(
            rollout_points(
                journal,
                boards[journal.name],
                rng=cold_rng,
                rollouts=rollouts,
                priors=priors,
                kappa=kappa,
                live_recalibration=False,
            )
        )

    noise: list[tuple[int, float]] = []
    for other in noise_seeds:
        other_rng = random.Random(other)
        noise.append(
            (
                other,
                calibrate(
                    [
                        pt
                        for journal in journals
                        for pt in rollout_points(
                            journal,
                            boards[journal.name],
                            rng=other_rng,
                            rollouts=rollouts,
                            priors=priors,
                            kappa=kappa,
                        )
                    ],
                    label=f"rollout/seed-{other}",
                ).brier,
            )
        )

    ab: tuple[PriorComparison, ...] = ()
    if prior_variants:
        ab = compare_priors(
            journals,
            boards,
            rng=random.Random(seed + 2),
            variants=list(prior_variants),
            rollouts=rollouts,
        )

    return RoomcheckReport(
        journals=journals,
        files_seen=files_seen,
        rollout=calibrate(rollout, label="rollout/live"),
        rollout_cold=calibrate(rollout_cold, label="rollout/cold"),
        rollout_engine=calibrate(
            [p for p in rollout if p.in_engine_candidates], label="rollout/engine"
        ),
        rollout_by_position=calibrate_by(rollout, "position"),
        rollout_by_round=calibrate_by(rollout, "round"),
        recalibration_engaged=(engaged, windows_total),
        live_reach_sigma=tuple(sorted(live_sigmas)),
        analytic=calibrate(analytic, label="analytic"),
        analytic_conditional=calibrate(analytic_conditional, label="analytic/conditional"),
        analytic_refit=calibrate(refit_points, label="analytic/refit"),
        analytic_refit_conditional=calibrate(
            refit_conditional, label="analytic/refit/conditional"
        ),
        fit=fit_survival_params(observations),
        control=null_control(
            journals, boards, rng=random.Random(seed + 3), drafts=control_drafts, priors=priors
        ),
        cross_validation=cross_validate_refit(journals, boards),
        dst=kdst_timing(journals, boards, position="DST", adp_by_as_of=adp_by_as_of),
        kicker=kdst_timing(journals, boards, position="K", adp_by_as_of=adp_by_as_of),
        skill_rescale=adp_rescale(
            journals, boards, adp_by_as_of, positions=SKILL_POSITIONS, group="skill"
        ),
        dst_rescale=adp_rescale(
            journals, boards, adp_by_as_of, positions=("DST",), group="DST"
        ),
        k_rescale=adp_rescale(journals, boards, adp_by_as_of, positions=("K",), group="K"),
        adp_census={as_of: table.census() for as_of, table in sorted(adp_by_as_of.items())},
        adp_coverage={
            as_of: table.coverage(board_cache[(latest.season, as_of)])
            for as_of, table in sorted(adp_by_as_of.items())
            if (latest.season, as_of) in board_cache
        },
        rooms=tuple(rooms),
        simulated=simulate_room(
            boards[latest.name],
            rng=random.Random(seed + 1),
            drafts=sim_drafts,
            priors=priors,
            roster=latest.roster,
            rounds=latest.rounds,
        ),
        prior_ab=ab,
        noise_probe=tuple(noise),
    )


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator entry
    """``python -m ziggurat.draft.roomcheck [journal-dir] [--db PATH]``.

    Opens the database READ-ONLY. The systemd timers run ``ziggurat`` from this
    working tree, so a measurement script that can write is a measurement script
    that can corrupt the production cadence.
    """
    import argparse
    import sqlite3

    from ziggurat.paths import DEFAULT_DB_PATH, REPO_ROOT

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "directory",
        nargs="?",
        default=str(REPO_ROOT / "data" / "draft" / "practice"),
        help="directory of completed cockpit journals (*.jsonl)",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--rollouts", type=int, default=DEFAULT_MEASUREMENT_ROLLOUTS)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument(
        "--control-drafts",
        type=int,
        default=DEFAULT_CONTROL_DRAFTS,
        help="all-bot rooms in the null control (section 2b)",
    )
    parser.add_argument(
        "--priors-ab",
        action="store_true",
        help="also run the labelled one-knob-at-a-time room-prior A/B (slower)",
    )
    parser.add_argument(
        "--noise-probe",
        action="store_true",
        help="re-run the headline rollout at two more seeds and print the spread",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = run_roomcheck(
            conn,
            args.directory,
            rollouts=args.rollouts,
            seed=args.seed,
            control_drafts=args.control_drafts,
            prior_variants=default_prior_variants() if args.priors_ab else None,
            noise_seeds=(args.seed + 11, args.seed + 12) if args.noise_probe else (),
        )
    finally:
        conn.close()
    for line in render_report(report):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry
    raise SystemExit(main())
