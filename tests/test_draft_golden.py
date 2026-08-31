"""GOLDEN MASTER of the draft engine — BOTH engines (captured 2026-08-30/31).

WHY THIS FILE EXISTS. The draft is hours out and the engine that will run it
works. Several workstreams changed how it values a roster (the known diagnosis:
``optimal_starting_points`` grades ONE lineup off a season-sum, so the objective
is blind to byes, injuries, bench value and the fact that a season is 14 separate
head-to-head weeks). Every one of those changes must be measured as a DELTA
against what the engine did before, and nothing may move silently. So this module
freezes behaviour and fails loudly when it moves — it asserts nothing about
whether that behaviour is *good*.

TWO ENGINES ARE FROZEN, AND THAT IS THE POINT (item 3.11, 2026-08-31).

  * ``composed`` — the DEFAULT. ``ziggurat draft-web --season 2026 --slot 9``
    runs it, and it is what decides tonight's picks. It is the shipped
    :class:`~ziggurat.draft.engine.PickEngine` under two re-ranks composed over
    DISJOINT decision domains: the pair term (``variant_wheel``) owns the 8
    first-of-pair picks and the week-by-week term (``variant_weekwise``, blend
    weight 2) owns the other 8. ``session.ENGINE_COMPOSED`` and
    ``DraftSession._engine`` carry the measured margins and why the nesting is
    this way round.
  * ``legacy`` — ``--legacy-engine``, rung 0 of the runbook's fallback ladder.
    Its fixture is UNCHANGED from the 2026-08-30 capture, and that is exactly
    what makes the escape hatch trustworthy: the flag is not a code path that
    resembles the old engine, it is bit-for-bit the engine that drafted four
    rehearsals, proved on the same frozen board it was proved on then.

An escape hatch nobody tests is a rumour. This module tests it.

WHAT MOVED WHEN THE DEFAULT CHANGED, and why — read this before re-blessing
anything. The legacy fixture did NOT move. The composed fixture is new, and the
diff between the two is the change: driven on the frozen board at seat 9, seed
42, ``rollouts=512``, the composed engine takes a DIFFERENT player at 7 of the
operator's 16 picks (overall 32, 49, 52, 129, 132, 149, 152 — see
:func:`test_the_composed_engine_differs_from_legacy_exactly_here`, which pins the
count so a later change cannot quietly widen it). **The K/DST divergence play is untouched**: D/ST
still goes at overall 89 and the kicker at 92 under both engines, which is the
single clearest edge the system has and the one thing a re-rank must not eat.

ONE NUMBER WILL LOOK WRONG IN THE COMPOSED FIXTURE AND IS NOT. At the 8
first-of-pair picks ``pick_score`` is a TWO-PICK TOTAL (``variant_wheel``), so it
is roughly double the legacy score at the same pick — overall 9 reads 333.49
against legacy's 199.24 for the same player. The reasons say so in words on every
such recommendation ("that total is what ranks him here, so it is NOT comparable
to a normal single-pick score"), which is the only reason it is allowed to reach
a novice at all.

It is also the automated half of the standing pre-draft rule that new work lands
BEHIND A FLAG — inverted, now that a flag has been thrown: the DEFAULT path is
frozen here too, so the next change cannot move it without being read.

WHAT IS FROZEN (all on the REAL live board of 2026-08-30, season 2026, from
draft slot 9 of 10 — the operator's actual seat, CLAUDE.md "Draft slot"):

  1. the board itself — 3,264 rows, provenance hash ``045eb8838696f634`` (the
     same ``session._board_hash`` the draft journal header records, so a board
     re-pull is detectable), frozen row-for-row in
     ``tests/fixtures/draft/board-2026-08-30.json``. The hash covers only
     (player_id, espn_overall_rank), so the COLUMN SHAPE is frozen separately
     (``BOARD_NAMELESS`` / ``BOARD_PRICED`` / ``BOARD_PRICED_NAMELESS``) and the
     live-drift check compares all seven columns — ``name``, ``position`` and
     ``team`` included;
  1b. the WEEK-BY-WEEK POINTS MAP the composed engine grades rosters with
     (``grader.weekly_points_map`` at the same ``as_of``/season/source), frozen
     in ``tests/fixtures/draft/weekly-points-2026-08-30.json`` so the default
     engine is testable OFFLINE. Without it the composed golden would be a
     DB-guarded skip — i.e. the engine that runs tonight would be the only one
     this module could not check on a fresh clone, which is limitation (a)
     below pointed at the worst possible target;

  2. EACH engine's full top-5 recommendation at EVERY one of the operator's 16
     picks (overall 9, 12, 29, 32, 49, 52, 69, 72, 89, 92, 109, 112, 129, 132,
     149, 152) — the player's ``name`` and ``position``, ``pick_score``, ``vor``,
     ``survival_next``, ``vona``, the three notes and the ``reasons`` VERBATIM,
     plus the live-recalibration state at each pick. Two fixtures:
     ``engine-golden-2026-08-30.json`` (legacy) and
     ``engine-composed-2026-08-30.json`` (the default);
  3. the COST of a recommendation, in two instruments. The load-free one is the
     regression gate: the exact number of survival-rollout calls and simulated
     opponent picks the 16 decisions consume (``ROLLOUT_SURVIVAL_CALLS`` /
     ``ROLLOUT_SIM_PICKS``), which is a property of the search, not of the box.
     Wall clock is measured and REPORTED, but only the plan's real ship bar
     (``RECOMMEND_SHIP_BAR_MS`` = 5 s, IMPLEMENTATION_PLAN 2.4) is asserted; see
     "THE TIMING GATE IS NOT A STOPWATCH RACE" below for why 243 ms is not;
  4. a full 25-draft ``run_many`` profile at slot 9, seed 42 — the points
     distribution and the mean roster shape. This is where the diagnosis is
     legible as a number: QB and TE both come back at EXACTLY 3.0 per draft.

THE PRODUCTION PATH IS THE ONE UNDER TEST. (2) drives a real
:class:`~ziggurat.draft.session.DraftSession` — the controller ``draft-web``
runs on the night — at ``rollouts=512`` with the calibrated 2.2 room
(``ROOM_PRIORS_2025``) filling the other nine seats, so the golden covers
session's fresh-state-seeded ctx construction and its live recalibration
(which engages from overall 29, at ``LIVE_RECAL_MIN_PICKS`` = 20 room picks)
as well as the engine arithmetic itself.

OFFLINE AND DETERMINISTIC. Everything except the one DB-guarded drift check
(:func:`test_the_live_board_still_matches_the_frozen_board`, which skips when
``db/ziggurat.sqlite`` is absent and opens it READ-ONLY when present) runs off
the committed fixture: no network, no live database. All randomness flows from
seeded ``random.Random`` streams (Rule 1 / determinism), so a re-run is
bit-for-bit reproducible.

WHAT THIS MODULE DOES **NOT** COVER — read this before treating it as a health
check. Three limitations, each of them structural rather than accidental:

  (a) THE PRICING SPINE IS COVERED BY EXACTLY ONE SKIPPABLE TEST. The board
      fixture is an OUTPUT of ``core/scoring.py`` -> ``core/valuation.py`` ->
      ``draft.simulator.load_board``. Every other test in this file drives off
      that frozen output, so a change that re-prices the whole board is visible
      ONLY to :func:`test_the_live_board_still_matches_the_frozen_board`, which
      requires the ~600 MB ``db/ziggurat.sqlite``. On a fresh clone, in CI, or on
      a draft-day laptop without the DB copied, this module reports green while a
      re-pricing of all 3,264 rows goes completely undetected. Measured
      2026-08-30: a +0.05/week mutation of ``scoring.score`` re-prices 3,229 of
      3,264 rows and leaves the board hash and row count IDENTICAL (the hash
      covers ids + ESPN rank only), so nothing but that one test can see it.
      :func:`test_the_pricing_spine_coverage_is_disclosed` always runs and warns
      loudly when the DB is missing rather than letting the skip pass silently.

  (b) A GREEN GOLDEN IS NOT A HEALTHY SYSTEM. :func:`_load_live_board` opens the
      DB through a ``mode=ro`` URI and never ``store.open_db``, deliberately (a
      test must not migrate production). The side effect is that this module is
      structurally blind to anything ``open_db`` would raise — a broken migration
      sequence, for instance, takes every ``ziggurat`` command down (the cockpit
      included) while every test here stays green. That is the suite's job
      elsewhere, not this module's; do not read this file as a preflight.

  (c) THE TIMING GATE IS NOT A STOPWATCH RACE. See ``RECOMMEND_SHIP_BAR_MS``.

RE-BLESSING (deliberate, never automatic — no env var can rewrite a golden
during an ordinary suite run). THE FIXTURES BLESS SEPARATELY, on purpose::

    .venv/bin/python tests/test_draft_golden.py --bless-board    # board only
    .venv/bin/python tests/test_draft_golden.py --bless-weekly   # points map only
    .venv/bin/python tests/test_draft_golden.py --bless-engine   # both goldens
    .venv/bin/python tests/test_draft_golden.py --bless          # everything

The board and the engine golden are two independent causes of the same symptom.
``espn_ranks``, ``projections`` and ``adp_rankings`` are all current-value-only
daily sources (CLAUDE.md), and a near-day board refresh is scheduled work — so a
board move and an objective change can easily land in the same window. Blessing
both at once destroys the attribution, which is this module's entire purpose.
The two-step recipe keeps it:

  1. ``--bless-board`` with the ENGINE CODE UNCHANGED. Re-run this file: the
     recommendation diffs it now prints are attributable to the BOARD alone,
     because nothing else moved. Record them.
  2. ``--bless-engine`` to adopt the new baseline. A later diff on top of that is
     attributable to the objective.

Then review ``git diff tests/fixtures/draft/`` and say in the commit message WHY
the behaviour changed. A bless with an unexplained diff is the failure this file
exists to prevent.

RULE 5: the fixtures hold public data only — player names, the public ESPN
editorial board rank, and house projections/VOR derived from public Sleeper
projections through ``ziggurat/core/scoring.py``. No league member names, no
rival rosters, no league-private data. The nine rival seats here are the
CALIBRATED SIMULATION, not real managers.

Deletable with ``ziggurat/draft/`` after draft day (Rule 8), like every other
``test_draft_*`` module.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
import warnings
from pathlib import Path

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.data.store import BUSY_TIMEOUT_MS
from ziggurat.draft import survival as _survival_mod
from ziggurat.draft.bots import (
    AutodraftBot,
    BoardEntry,
    PickContext,
    RankNoiseBot,
    position_counts,
)
from ziggurat.draft.engine import PickEngine
from ziggurat.draft.priors import ROOM_PRIORS_2025
from ziggurat.draft.session import DraftSession, _board_hash
from ziggurat.draft.simulator import (
    ROUNDS,
    _assign_autodrafters,
    load_board,
    run_many,
    snake_sequence,
)
from ziggurat.paths import REPO_ROOT

# ------------------------------------------------------------------- constants
#
# The capture co-ordinates. Every one of these is a FROZEN input: change any of
# them and you are capturing a different experiment, not re-blessing this one.

AS_OF = "2026-08-30"
SEASON = 2026

#: Operator's real seat (CLAUDE.md "Draft slot: 9 of 10", re-read live 2026-08-29
#: from ``draftSettings.pickOrder``). 0-based internally, 1-based for humans.
OPERATOR_SEAT = 8
OPERATOR_SLOT_1BASED = 9

#: Identity pick order — the draft-night command deliberately passes no
#: ``--pick-order`` (seat ids are arbitrary internal labels and synced picks
#: arrive positionally, so identity is provably equivalent; CLAUDE.md).
PICK_ORDER = tuple(range(DEFAULT_ROSTER.teams))

#: The operator's 16 overall picks, as the constitution states them. Derived
#: independently from the snake geometry by a test below — if the two ever
#: disagree, the seat or the geometry moved.
OPERATOR_OVERALL_PICKS = (9, 12, 29, 32, 49, 52, 69, 72, 89, 92, 109, 112, 129, 132, 149, 152)

#: Session config, matching what ``draft-web`` constructs live.
SESSION_SEED = 42
ROLLOUTS = 512

#: THE ship bar, and the only wall-clock number this module ASSERTS.
#: IMPLEMENTATION_PLAN 2.4 records the requirement and the observation as two
#: different things: "recommend() <=243 ms @ R=512 on the real 3,218-entry board
#: (~20x under the 5 s bar) -> SYNCHRONOUS recompute". 5 s is the bar (it is what
#: makes a synchronous recompute safe); 243 ms was that day's measurement on that
#: box. The confirmed draft clock is 90 s/pick, so 5 s is itself 18x conservative.
RECOMMEND_SHIP_BAR_MS = 5_000.0

#: The item-2.4 OBSERVATION, kept as a reported reference point, never asserted.
#: Measured here 2026-08-30, best of 3, 16 picks, R=512, real board:
#:   idle on the 32-thread capture box .................. worst pick 168.9 ms
#:   with 24 competing CPU-bound processes .............. worst pick 271.4 ms
#: The contention penalty is NOT scheduler wait — ``time.process_time`` inflates
#: identically (270.9 ms), i.e. the process really does burn ~60% more CPU for the
#: same work under memory/cache pressure — so best-of-N cannot absorb it and
#: neither can switching to a CPU clock (both measured, 2026-08-30). Asserting
#: 243 ms therefore turns an unrelated concurrent pytest run into a RED suite on
#: draft eve while the engine is still ~18x inside the real bar. Exceeding it
#: warns; only ``RECOMMEND_SHIP_BAR_MS`` fails.
RECOMMEND_OBSERVED_MS = 243.0

#: Best-of-N for the reported wall clock. Absorbs a scheduler hiccup; it does NOT
#: absorb sustained contention (measured above), which is why it is not a gate.
TIMING_REPEATS = 3

#: THE COST GATE — deterministic, load-free, and the actual algorithmic-regression
#: detector the wall clock was being asked to be. ``rollout_survival`` is called
#: once per on-clock decision and simulates ``rollouts * picks_until_next``
#: opponent picks; both numbers are pure functions of the search and the snake
#: geometry, so they are identical on an idle box and a thrashing one. Measured
#: 2026-08-30 on the frozen board: 16 calls (one per operator pick),
#: 512 * (8*2 + 7*16 + 8) = 512 * 136 = 69,632 simulated picks, bit-stable across
#: repeated drives. A change that adds rollouts, widens the survival window, or
#: calls the rollout per-candidate instead of per-decision moves these; CPU
#: contention never does.
ROLLOUT_SURVIVAL_CALLS = 16
ROLLOUT_SIM_PICKS = 69_632

#: How many of the composed engine's 16 decisions take the PAIR path (item 3.11).
#: At seat 9 of 10 the operator's turns arrive in eight tight pairs three overalls
#: apart, and the wheel term engages at the FIRST of each: overalls 9, 29, 49, 69,
#: 89, 109, 129, 149. The other eight are the week-by-week term's. Frozen because
#: 0 and 16 are both silent failures — one of the two shipped improvements
#: becoming dead code, with every behavioural assertion in this file still green.
COMPOSED_PAIR_DECISIONS = 8

FIXTURES = Path(__file__).parent / "fixtures" / "draft"
BOARD_FIXTURE = FIXTURES / f"board-{AS_OF}.json"

#: The LEGACY engine's golden — ``--legacy-engine``, unchanged since 2026-08-30.
GOLDEN_FIXTURE = FIXTURES / f"engine-golden-{AS_OF}.json"

#: The DEFAULT (composed) engine's golden — what ``draft-web`` runs tonight.
COMPOSED_FIXTURE = FIXTURES / f"engine-composed-{AS_OF}.json"

#: The composed engine's objective input, frozen so the default path is testable
#: with no database (see "WHAT IS FROZEN" 1b).
WEEKLY_FIXTURE = FIXTURES / f"weekly-points-{AS_OF}.json"

#: How many of the operator's 16 picks the composed engine takes differently from
#: the legacy engine on this frozen board/room. Pinned so a later change cannot
#: quietly widen the divergence; see
#: :func:`test_the_composed_engine_differs_from_legacy_exactly_here`.
COMPOSED_PICKS_MOVED = 7
COMPOSED_MOVED_OVERALLS = (32, 49, 52, 129, 132, 149, 152)

#: The K/DST divergence play, asserted under BOTH engines. This is the system's
#: clearest measured edge (D/ST and K taken ~50 picks before the room does), it
#: emerges from the score rather than a special case, and a re-rank that ate it
#: would be a silent regression the recommendation diff would bury among 160
#: other numbers. So it gets its own named assertion.
DST_OVERALL = 89
KICKER_OVERALL = 92

#: The frozen board's provenance hash, duplicated here on purpose: the fixture
#: carries its own hash, so a tampered fixture with a re-computed hash would be
#: self-consistent. This constant is the second witness.
BOARD_HASH = "045eb8838696f634"
BOARD_COUNT = 3264

#: The board's COLUMN SHAPE, frozen because ``_board_hash`` covers only
#: (player_id, espn_overall_rank) and three of the seven frozen columns —
#: ``name``, ``position``, ``team`` — are otherwise unwatched by any hash.
#: ``name`` is the column draft night actually runs on: ``resolver.NameResolver``
#: indexes only non-None names, so a nameless row is UNENTERABLE in the cockpit,
#: and ``valuation._resolve_names`` selects the latest ``players`` row per gsis_id
#: through a subquery that is NOT itself filtered to non-null names — the same
#: shadowing shape as the measured ``players.CrosswalkCollapse`` incident
#: (CLAUDE.md 3.1b). A ``players`` pull that blanked names would leave ids, ranks,
#: points and VOR identical and blank the whole ``name`` column silently.
#: So the counts below are frozen rather than an ``all(e.name)`` assertion, which
#: is FALSE today and would have to be deleted rather than kept honest.
#: Measured 2026-08-30 on the frozen board:
BOARD_NAMELESS = 1215          #: rows with name=None (deep ESPN-universe scrubs)
BOARD_PRICED = 531             #: rows carrying a non-zero house projection
BOARD_PRICED_NAMELESS = 8      #: PRICED **and** nameless -> priced but unenterable

LIVE_DB = REPO_ROOT / "db" / "ziggurat.sqlite"

_GOLDEN_SCHEMA = 1


# --------------------------------------------------------------- fixture I/O


def _entry(row: list) -> BoardEntry:
    """One frozen fixture row -> a :class:`BoardEntry` (column order is in the file)."""
    return BoardEntry(
        player_id=str(row[0]),
        name=row[1],
        position=str(row[2]),
        espn_overall_rank=int(row[3]),
        house_points=float(row[4]),
        vor=float(row[5]),
        team=row[6],
    )


def _load_fixture_board() -> tuple[tuple[BoardEntry, ...], dict]:
    payload = json.loads(BOARD_FIXTURE.read_text(encoding="utf-8"))
    return tuple(_entry(r) for r in payload["rows"]), payload


def _load_live_board() -> tuple[BoardEntry, ...]:
    """The board as the live DB serves it today, at the SAME frozen ``as_of``.

    Opened through a ``mode=ro`` URI, never ``store.open_db``: the systemd timers
    run ``ziggurat`` from this working tree, so the live database IS production
    (CLAUDE.md standing rule) and a test must be structurally incapable of
    writing to it — including the schema-migration write ``open_db`` would do.
    """
    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    # Bypassing ``store.open_db`` also bypasses ``store.connect``'s busy_timeout,
    # which would leave this read SIX TIMES less patient than every other reader of
    # the same file. Measured 2026-08-30 against a locked scratch DB: without the
    # pragma the identical read raises ``OperationalError: database is locked``
    # after exactly 5.0 s; with it, the read waits and succeeds. The nfl-ingest
    # timer writes ~58k projection rows (13-41 s of writing) on draft morning, so
    # the collision window is real and the un-patient failure mode is a test ERROR
    # rather than the intended "the board moved" message.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    try:
        return load_board(conn, as_of=AS_OF, season=SEASON)
    finally:
        conn.close()


@contextlib.contextmanager
def count_rollout_work():
    """Count survival-rollout work during a drive — the load-free cost instrument.

    Wraps BOTH producers of that work and tallies calls plus
    ``rollouts * picks_until_next`` simulated opponent picks:

      * :func:`ziggurat.draft.survival.rollout_survival` — the module attribute
        the engine resolves lazily per call;
      * :func:`ziggurat.draft.variant_wheel.rollout_pair_batch` — the SAME loop,
        additionally recording per-rollout outcomes, which the composed engine
        runs INSTEAD at its 8 pair picks.

    Wrapping only the first was measurably wrong once the pair term shipped
    (2026-08-31): the composed drive read 8 calls / 61,440 picks against legacy's
    16 / 69,632 and looked like a search that had SHRUNK BY HALF, when the other
    half had simply moved to a function this instrument could not see. A cost gate
    that reads a relocation as an improvement is worse than no cost gate.

    Restores both originals on exit even if the drive raises.
    """
    from ziggurat.draft import variant_wheel as _wheel_mod

    tally = {"calls": 0, "sim_picks": 0, "pair_calls": 0}
    original = _survival_mod.rollout_survival
    original_pair = _wheel_mod.rollout_pair_batch

    def counting(*args, **kwargs):
        result = original(*args, **kwargs)
        tally["calls"] += 1
        tally["sim_picks"] += result.rollouts * result.picks_until_next
        return result

    def counting_pair(*args, **kwargs):
        result = original_pair(*args, **kwargs)
        tally["calls"] += 1
        tally["pair_calls"] += 1
        tally["sim_picks"] += result.rollouts * result.picks_until_next
        return result

    _survival_mod.rollout_survival = counting
    _wheel_mod.rollout_pair_batch = counting_pair
    try:
        yield tally
    finally:
        _survival_mod.rollout_survival = original
        _wheel_mod.rollout_pair_batch = original_pair


# ------------------------------------------------------------ the golden drive


def _room(seed: int) -> tuple[random.Random, frozenset[int], dict[int, object]]:
    """The nine rival seats, built exactly the way ``run_many`` builds a room.

    ``_assign_autodrafters`` is imported rather than re-implemented so this room
    and the ``run_many`` profile below cannot drift apart: both face the SAME
    calibrated 2.2 model (``ROOM_PRIORS_2025``: reach sigma 17.78, autodraft
    share 0.2, the fitted 16-round position-run curve).
    """
    room_rng = random.Random(seed)
    autos = _assign_autodrafters(
        room_rng,
        teams=DEFAULT_ROSTER.teams,
        operator_slot=OPERATOR_SEAT,
        fraction=ROOM_PRIORS_2025.autodraft_fraction,
        autodraft_count=None,
    )
    pickers = {
        t: (AutodraftBot() if t in autos else RankNoiseBot(priors=ROOM_PRIORS_2025))
        for t in range(DEFAULT_ROSTER.teams)
        if t != OPERATOR_SEAT
    }
    return room_rng, frozenset(autos), pickers


def _rec_payload(rec) -> dict:
    return {
        "player_id": rec.player_id,
        "name": rec.name,
        "position": rec.position,
        "pick_score": rec.pick_score,
        "vor": rec.vor,
        "survival_next": rec.survival_next,
        "vona": rec.vona,
        "need_note": rec.need_note,
        "risk_note": rec.risk_note,
        "divergence_note": rec.divergence_note,
        "reasons": list(rec.reasons),
        "alternatives": [list(a) for a in rec.alternatives],
    }


def _load_fixture_weekly():
    """The frozen week-by-week points map, rebuilt as the grader's own type.

    ``WeeklyPointsMap`` carries ``positions``/``names``/``teams`` alongside the
    points; losing them silently degrades the grader's opponent-field model to
    ``None``, so they are frozen and restored rather than reconstructed.
    """
    from ziggurat.draft.grader import WeeklyPointsMap

    payload = json.loads(WEEKLY_FIXTURE.read_text(encoding="utf-8"))
    points = {
        pid: {int(w): float(p) for w, p in weeks.items()}
        for pid, weeks in payload["points"].items()
    }
    return WeeklyPointsMap(
        points,
        positions=payload["positions"],
        names=payload["names"],
        teams=payload["teams"],
    )


def _load_live_weekly(board):
    """The points map as the live DB serves it, at the SAME frozen ``as_of``.

    Read-only URI, never ``store.open_db`` — same discipline and same reason as
    :func:`_load_live_board`.
    """
    from ziggurat.draft import grader

    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    try:
        return grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON, board=board)
    finally:
        conn.close()


def drive_golden_draft(
    board, *, seed: int = SESSION_SEED, repeats: int = 1, weekly=None
) -> dict:
    """Run one full 16-round draft through the LIVE controller and record it.

    ``weekly=None`` drives the LEGACY engine (``--legacy-engine``); passing the
    week-by-week points map drives the DEFAULT composed engine. Nothing else
    differs between the two arms — same board, same seed, same room, same
    ``DraftSession`` — so the diff between the two fixtures is the engine.

    The operator seat is :class:`DraftSession` itself (``recommend`` ->
    ``append_pick``); the nine rivals are the calibrated 2.2 room. Returns the
    per-pick top-5 payloads, the best-of-``repeats`` wall clock for each
    ``recommend()`` call, and the resulting operator roster.

    ``repeats`` > 1 calls ``recommend`` that many times per pick. Session builds
    a FRESH state-seeded ctx per compute, so every repeat must return an
    IDENTICAL result — that is checked, not assumed (item 2.4 recorded the
    opposite for a HELD ctx: ``engine.recommend`` mutates ``ctx.rng``).
    """
    room_rng, autos, pickers = _room(seed)
    picks: list[dict] = []
    repeat_payloads: list[list[dict]] = []
    timings: dict[int, float] = {}

    with tempfile.TemporaryDirectory() as td:
        sess = DraftSession.start(
            board,
            operator_slot=OPERATOR_SEAT,
            pick_order=PICK_ORDER,
            season=SEASON,
            as_of=AS_OF,
            journal_path=Path(td) / "golden.jsonl",
            session_seed=seed,
            rollouts=ROLLOUTS,
            weekly=weekly,
        )
        engine_profile = sess.engine_profile
        while not sess.complete:
            if sess.is_operator_turn:
                overall = sess.overall_pick
                best_ms = None
                runs: list[list[dict]] = []
                for _ in range(max(1, repeats)):
                    t0 = time.perf_counter()
                    recs = sess.recommend(top=5)
                    ms = (time.perf_counter() - t0) * 1000.0
                    best_ms = ms if best_ms is None else min(best_ms, ms)
                    runs.append([_rec_payload(r) for r in recs])
                timings[overall] = best_ms
                if repeats > 1:
                    repeat_payloads.append(runs)
                recal = sess.recalibration()
                picks.append(
                    {
                        "overall": overall,
                        "round": sess.round,
                        "recalibration": {
                            "engaged": recal.engaged,
                            "n_room_picks": recal.n_room_picks,
                            "reach_sigma": recal.reach_sigma,
                        },
                        "top5": runs[0],
                    }
                )
                sess.append_pick(runs[0][0]["player_id"])
            else:
                seat = sess.current_seat
                ctx = PickContext.from_board(
                    board,
                    own_roster=sess.opponent_rosters[seat],
                    taken=sess.taken,
                    team_slot=seat,
                    round=sess.round,
                    overall_pick=sess.overall_pick,
                    rounds_total=ROUNDS,
                    roster=DEFAULT_ROSTER,
                    rng=room_rng,
                )
                sess.append_pick(pickers[seat].pick(ctx))

        roster = [
            {
                "overall": p.overall,
                "player_id": p.player_id,
                "name": e.name,
                "position": e.position,
                "house_points": e.house_points,
            }
            for p, e in zip(
                [p for p in sess.picks if p.seat == OPERATOR_SEAT],
                sess.own_roster,
                strict=True,
            )
        ]

    return {
        "engine_profile": engine_profile,
        "autodraft_seats": sorted(autos),
        "picks": picks,
        "operator_roster": roster,
        "roster_shape": dict(sorted(position_counts(_entry_list(roster)).items())),
        "recommend_ms": timings,
        "repeat_payloads": repeat_payloads,
    }


def _entry_list(roster_rows) -> list[BoardEntry]:
    """Minimal entries for ``position_counts`` over a captured roster payload."""
    return [
        BoardEntry(
            player_id=r["player_id"],
            name=r["name"],
            position=r["position"],
            espn_overall_rank=0,
            house_points=r["house_points"],
            vor=0.0,
        )
        for r in roster_rows
    ]


def profile_run_many(board) -> dict:
    """The 25-draft strategy profile: the engine as the operator's strategy.

    Same room model as the session drive, but 25 independent rooms rather than
    one — this is the surface on which "the engine takes 3 QB and 3 TE in EVERY
    draft" is a measurable number rather than an anecdote.
    """
    summary = run_many(
        board,
        n=25,
        operator_slot=OPERATOR_SEAT,
        strategy=PickEngine(rollouts=ROLLOUTS, room_priors=ROOM_PRIORS_2025),
        strategy_name="PickEngine",
        seed=SESSION_SEED,
    )
    return {
        "n": summary.n,
        "operator_slot": summary.operator_slot,
        "points_mean": summary.points_mean,
        "points_p10": summary.points_p10,
        "points_p50": summary.points_p50,
        "points_p90": summary.points_p90,
        "points_min": summary.points_min,
        "points_max": summary.points_max,
        "position_counts_mean": dict(sorted(summary.position_counts_mean.items())),
    }


# --------------------------------------------------------------- test fixtures


@pytest.fixture(scope="module")
def board() -> tuple[BoardEntry, ...]:
    entries, _payload = _load_fixture_board()
    return entries


@pytest.fixture(scope="module")
def board_payload() -> dict:
    _entries, payload = _load_fixture_board()
    return payload


@pytest.fixture(scope="module")
def golden() -> dict:
    """The LEGACY engine's frozen behaviour (``--legacy-engine``)."""
    return json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def composed_golden() -> dict:
    """The DEFAULT engine's frozen behaviour — what runs tonight."""
    return json.loads(COMPOSED_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def weekly():
    """The frozen week-by-week points map the composed engine grades with."""
    return _load_fixture_weekly()


@pytest.fixture(scope="module")
def run(board) -> dict:
    """One LEGACY drive, shared by every test that reads it (~1.5 s)."""
    return drive_golden_draft(board)


@pytest.fixture(scope="module")
def composed_run(board, weekly) -> dict:
    """One DEFAULT (composed) drive, shared by every test that reads it."""
    return drive_golden_draft(board, weekly=weekly)


@pytest.fixture(scope="module")
def profile(board) -> dict:
    """ONE live 25-draft profile (~30 s), shared by every test that reads it.

    Module-scoped and shared on purpose: the tests that assert the roster SHAPE
    must assert it against the engine running NOW, not against the JSON file they
    just loaded, or they cannot fail when the objective changes.
    """
    return profile_run_many(board)


def _approx(x: float):
    """Golden floats round-trip exactly through JSON; the tolerance is only to
    keep a last-bit difference from reading as a behaviour change."""
    return pytest.approx(x, rel=1e-9, abs=1e-9)


# --------------------------------------------------------------- board identity


def test_the_frozen_board_is_internally_consistent(board, board_payload):
    """The fixture is the board it claims to be, and this module's own second
    witness agrees. Two hashes because a tampered fixture that re-computed its
    own header would otherwise be self-consistent."""
    assert board_payload["as_of"] == AS_OF
    assert board_payload["season"] == SEASON
    assert board_payload["count"] == len(board) == BOARD_COUNT
    assert board_payload["hash"] == BOARD_HASH
    assert _board_hash(board) == BOARD_HASH

    ids = [e.player_id for e in board]
    assert len(set(ids)) == len(ids), "duplicate player_id on the frozen board"
    assert all(e.position for e in board)


def test_the_board_name_and_team_columns_are_frozen_and_disclosed(board):
    """``name``, ``position`` and ``team`` are watched — nothing else watches them.

    ``session._board_hash`` covers (player_id, espn_overall_rank) ONLY, so without
    this test a board whose whole ``name`` column went None, or whose ``team`` map
    moved on a late-August trade, is invisible to every assertion in this file.

    NAMES ARE FROZEN AS A COUNT, NOT AS ``all(e.name)``, because ``all(e.name)`` is
    FALSE on the real board and a test that asserts a falsehood gets deleted rather
    than fixed. 1,215 of 3,264 rows carry ``name=None``: they are ESPN-universe
    rows with no gsis/espn crosswalk, whose ``player_id`` is the synthetic
    ``"{position}:{overall_rank}"`` fallback from ``simulator.load_board``.

    THE DISCLOSURE THIS TEST EXISTS TO KEEP LOUD: eight of those nameless rows are
    PRICED. ``resolver.NameResolver`` skips every ``name is None`` row when it
    builds the draft-night index — its docstring calls them "deep scrubs with no
    draftable identity", and for 1,207 of them that is true — so those 8 are
    priced-but-unenterable in the cockpit. The deepest of them is a kicker at
    ~108 house points (roughly the 16th K by VOR), which is inside the range the
    engine's K/DST divergence play actually reaches in rounds 9-10. If the operator
    ever cannot find a kicker he was recommended, this is why.

    TEAM is asserted as a hard invariant on the priced rows because the successor
    bye-aware workstream keys on it: an unpriced row may lack a team, a PRICED one
    may not, or a bye lookup silently loses the player.
    """
    nameless = [e for e in board if e.name is None]
    priced = [e for e in board if e.house_points != 0.0]
    priced_nameless = [e for e in priced if e.name is None]

    assert len(nameless) == BOARD_NAMELESS, (
        f"{len(nameless)} nameless board rows, frozen at {BOARD_NAMELESS}. A JUMP "
        "toward 3,264 means the `players` name column collapsed (see BOARD_NAMELESS "
        "for the shadowing subquery in valuation._resolve_names) and the draft-night "
        "resolver just lost that many players; a DROP means the crosswalk improved."
    )
    assert len(priced) == BOARD_PRICED, (
        f"{len(priced)} priced rows, frozen at {BOARD_PRICED} — the projections "
        "feed changed how many players it can price at all"
    )
    assert len(priced_nameless) == BOARD_PRICED_NAMELESS, (
        f"{len(priced_nameless)} rows are priced but nameless (frozen at "
        f"{BOARD_PRICED_NAMELESS}); these are unenterable in the cockpit: "
        + ", ".join(f"{e.player_id}@{e.house_points:.1f}" for e in priced_nameless[:10])
    )
    missing_team = [e.player_id for e in priced if not e.team]
    assert not missing_team, (
        f"{len(missing_team)} PRICED board rows carry no team "
        f"(first few: {missing_team[:5]}) — every bye-week and opponent-quality "
        "lookup keys on team, and would silently drop these players"
    )
    assert all(e.name is None or e.name.strip() for e in board), (
        "a board name is whitespace-only: the resolver would index an empty key"
    )


def test_the_frozen_board_carries_no_league_private_data(board_payload):
    """Rule 5 spot-check: the fixture holds only the seven public board columns.

    A future bless that widened the row (an ownership share, a rival's holding,
    an owner name) would be a public-repo leak, and the fixture is 3,264 rows
    deep — nobody re-reads it by eye.
    """
    assert board_payload["columns"] == [
        "player_id", "name", "position", "espn_overall_rank", "house_points", "vor", "team",
    ]
    assert all(len(r) == 7 for r in board_payload["rows"])


def test_the_frozen_points_map_carries_no_league_private_data():
    """Rule 5, applied to the second big fixture this module added (item 3.11).

    ``weekly-points-2026-08-30.json`` is 3,229 keys deep and nobody re-reads it by
    eye either. It must hold exactly four top-level data blocks — per-week house
    points and the three public identity maps — and every points key must be an
    NFL WEEK NUMBER. A future bless that widened it (an ownership share, a rival's
    holding, an opponent's team name — this is a ten-team OFFICE league, so a team
    name can encode a colleague) would be a public-repo leak.
    """
    payload = json.loads(WEEKLY_FIXTURE.read_text(encoding="utf-8"))
    assert set(payload) == {"_comment", "as_of", "season", "points", "positions", "names", "teams"}
    assert payload["as_of"] == AS_OF and payload["season"] == SEASON
    for weeks in payload["points"].values():
        assert all(w.isdigit() and 1 <= int(w) <= 22 for w in weeks)
        assert all(isinstance(v, (int, float)) for v in weeks.values())
    assert set(payload["positions"].values()) <= {"QB", "RB", "WR", "TE", "DST", "K"}


@pytest.mark.skipif(
    not LIVE_DB.exists(),
    reason=(
        "live db/ziggurat.sqlite absent — SKIPPING THIS TEST REMOVES THIS MODULE'S "
        "ENTIRE COVERAGE OF THE PRICING SPINE (core/scoring.py, core/valuation.py, "
        "draft.simulator.load_board): every other test here drives off the frozen "
        "board fixture, which is that spine's OUTPUT. See the module docstring, "
        "limitation (a), and test_the_pricing_spine_coverage_is_disclosed."
    ),
)
def test_the_live_board_still_matches_the_frozen_board(board):
    """A board refresh must be DETECTABLE, not silent — on ALL SEVEN columns.

    The golden's recommendations are only meaningful against the board they were
    captured on. ``espn_ranks``, ``projections`` and ``adp_rankings`` are all
    current-value-only sources (CLAUDE.md), so the ingest cadence can move this
    board under the golden between one run and the next. When this fails, the
    board moved: re-bless the BOARD ALONE first
    (``python tests/test_draft_golden.py --bless-board``), re-run to read the
    board-attributable delta, and only then ``--bless-engine``.

    THIS IS ALSO THE MODULE'S ONLY WITNESS TO THE PRICING SPINE. A mutation of
    ``scoring.score`` re-prices 3,229 of 3,264 rows while leaving the board hash
    and row count byte-identical (measured 2026-08-30) — the hash covers ids and
    ESPN rank only. So every frozen column is compared here, not just the two the
    hash misses: ``name`` (what the draft-night resolver matches on), ``position``
    and ``team`` (what the bye-week work keys on) included.
    """
    live = _load_live_board()
    assert len(live) == len(board), (
        f"live board has {len(live)} rows, the frozen board {len(board)} — "
        "the board was re-pulled since the golden was captured"
    )
    assert _board_hash(live) == BOARD_HASH, (
        f"live board hash {_board_hash(live)} != frozen {BOARD_HASH} — "
        "membership or ESPN rank moved since the golden was captured"
    )
    # The hash covers ids and ranks only. Compare every remaining frozen column.
    frozen_by_id = {e.player_id: e for e in board}
    columns = ("name", "position", "team", "house_points", "vor")
    moved: dict[str, list[str]] = {c: [] for c in columns}
    for e in live:
        was = frozen_by_id[e.player_id]
        for column in columns:
            if getattr(e, column) != getattr(was, column):
                moved[column].append(
                    f"{e.player_id} ({was.name}): {getattr(was, column)!r} -> "
                    f"{getattr(e, column)!r}"
                )
    hints = {
        "name": "the players crosswalk moved (a blanked name is unenterable in the cockpit)",
        "position": "a player changed position on the board",
        "team": "a trade or an nflverse abbreviation change — bye weeks move with it",
        "house_points": "the projections feed moved, or the pricing spine changed",
        "vor": "replacement levels moved, or the pricing spine changed",
    }
    report = [
        f"  {column}: {len(rows)} row(s) moved ({hints[column]})\n"
        + "\n".join(f"    {r}" for r in rows[:5])
        for column, rows in moved.items()
        if rows
    ]
    assert not report, (
        "the live board no longer matches the frozen board:\n"
        + "\n".join(report)
        + "\n\nRe-bless the BOARD FIRST and alone so the recommendation delta stays "
        "attributable:\n  .venv/bin/python tests/test_draft_golden.py --bless-board"
    )


def test_the_pricing_spine_coverage_is_disclosed():
    """ALWAYS runs. Makes the missing-DB skip loud instead of silent.

    A plain ``skipif`` is invisible without ``pytest -rs``, and what it skips here
    is not a nice-to-have: it is the ONLY assertion in this module that can see a
    change to ``core/scoring.py``, ``core/valuation.py`` or ``load_board``. A
    reader who sees "10 passed, 1 skipped" on a fresh clone would reasonably
    conclude the engine is verified. It is not.
    """
    if LIVE_DB.exists():
        return
    warnings.warn(
        "test_draft_golden: db/ziggurat.sqlite is absent, so "
        "test_the_live_board_still_matches_the_frozen_board did not run. This "
        "module's ENTIRE coverage of the pricing spine (core/scoring.py, "
        "core/valuation.py, draft.simulator.load_board) is void for this run: a "
        "change that re-prices all 3,264 board rows leaves every remaining test "
        "green, because they all drive off the frozen board fixture, which is that "
        "spine's output. The recommendation golden and the 25-draft profile still "
        "cover the ENGINE arithmetic on the frozen board.",
        stacklevel=2,
    )


def test_the_operator_picks_are_the_sixteen_in_the_constitution():
    """Slot 9 of 10 over identity pick order yields exactly the 16 overall picks
    CLAUDE.md records. This is the one number in the golden a human transcribed,
    so it is derived independently rather than trusted."""
    seq = snake_sequence(PICK_ORDER, ROUNDS)
    derived = tuple(o for o, seat in enumerate(seq, start=1) if seat == OPERATOR_SEAT)
    assert derived == OPERATOR_OVERALL_PICKS
    assert OPERATOR_SEAT + 1 == OPERATOR_SLOT_1BASED


# ------------------------------------------------------ the recommendation golden


def _diff_recs(where: str, got: dict, want: dict) -> list[str]:
    out: list[str] = []
    if got["player_id"] != want["player_id"]:
        out.append(
            f"{where}: player {want['name']} ({want['player_id']}) -> "
            f"{got['name']} ({got['player_id']})"
        )
        return out  # a different player makes every number below incomparable
    # ``name`` is compared because it is the column draft night runs on: the
    # cockpit's resolver indexes only non-None names, so a recommendation whose
    # name went None is a recommendation the operator cannot type in. Without this
    # line every assertion in this file passes on an all-None-name board.
    for field in ("name", "position"):
        if got[field] != want[field]:
            out.append(f"{where}: {field} {want[field]!r} -> {got[field]!r}")
    for field in ("pick_score", "vor", "survival_next", "vona"):
        if got[field] != _approx(want[field]):
            out.append(
                f"{where} {want['name']}: {field} {want[field]!r} -> {got[field]!r}"
            )
    for field in ("need_note", "risk_note", "divergence_note"):
        if got[field] != want[field]:
            out.append(f"{where} {want['name']}: {field}\n    was: {want[field]!r}\n    now: {got[field]!r}")
    if got["reasons"] != want["reasons"]:
        out.append(
            f"{where} {want['name']}: reasons changed\n"
            f"    was: {want['reasons']}\n    now: {got['reasons']}"
        )
    if got["alternatives"] != want["alternatives"]:
        out.append(
            f"{where} {want['name']}: alternatives changed\n"
            f"    was: {want['alternatives']}\n    now: {got['alternatives']}"
        )
    return out


def test_the_golden_describes_the_frozen_board_and_the_frozen_room(golden, run):
    """Provenance: the golden was captured on THIS board with THIS room."""
    assert golden["schema"] == _GOLDEN_SCHEMA
    assert golden["as_of"] == AS_OF and golden["season"] == SEASON
    assert golden["board"] == {"count": BOARD_COUNT, "hash": BOARD_HASH}
    assert golden["room"]["seed"] == SESSION_SEED
    assert golden["room"]["rollouts"] == ROLLOUTS
    assert golden["room"]["priors"] == "ROOM_PRIORS_2025"
    assert golden["room"]["operator_seat_0based"] == OPERATOR_SEAT
    assert golden["room"]["pick_order"] == list(PICK_ORDER)
    # The room the drive actually built must be the room the golden recorded —
    # a changed autodraft draw is a changed experiment, not a changed engine.
    assert run["autodraft_seats"] == golden["room"]["autodraft_seats"]


def test_the_sixteen_operator_recommendations_are_unchanged(golden, run):
    """THE golden assertion: every number and every sentence the cockpit would
    put in front of the operator at each of his 16 picks, verbatim.

    This is deliberately a wide, brittle assertion. A change here is not a bug
    in this test — it is the delta the change under way was supposed to produce,
    and it must be read and explained before it is blessed.
    """
    assert [p["overall"] for p in run["picks"]] == list(OPERATOR_OVERALL_PICKS)
    assert len(run["picks"]) == len(golden["picks"])

    diffs: list[str] = []
    for got_pick, want_pick in zip(run["picks"], golden["picks"], strict=True):
        overall = want_pick["overall"]
        assert got_pick["overall"] == overall
        assert got_pick["round"] == want_pick["round"]
        gr, wr = got_pick["recalibration"], want_pick["recalibration"]
        if gr["engaged"] != wr["engaged"] or gr["n_room_picks"] != wr["n_room_picks"]:
            diffs.append(
                f"pick {overall}: live recalibration {wr} -> {gr}"
            )
        elif (wr["reach_sigma"] is None) != (gr["reach_sigma"] is None) or (
            wr["reach_sigma"] is not None
            and gr["reach_sigma"] != _approx(wr["reach_sigma"])
        ):
            diffs.append(
                f"pick {overall}: reach_sigma {wr['reach_sigma']!r} -> {gr['reach_sigma']!r}"
            )
        assert len(got_pick["top5"]) == len(want_pick["top5"]) == 5
        for i, (got, want) in enumerate(
            zip(got_pick["top5"], want_pick["top5"], strict=True), start=1
        ):
            diffs.extend(_diff_recs(f"pick {overall} #{i}", got, want))

    assert not diffs, (
        f"{len(diffs)} engine-behaviour change(s) vs the 2026-08-30 golden:\n  "
        + "\n  ".join(diffs)
        + "\n\nFIRST: is the BOARD still the frozen one? If "
        "test_the_live_board_still_matches_the_frozen_board also failed, these "
        "diffs are the board moving, not the engine.\n"
        "If this is the intended ENGINE delta, re-bless deliberately:\n"
        "  .venv/bin/python tests/test_draft_golden.py --bless-engine\n"
        "and record WHY in the commit message."
    )


def test_every_recommendation_still_ships_reasons(run, composed_run):
    """Rule 6 invariant, independent of the frozen text: the operator is a
    football novice, so no recommendation may ever reach him bare.

    Checked on BOTH engines. The composed engine assembles its reasons by
    APPENDING to the engine's tuple, so an empty tuple is not the failure to
    worry about — a re-rank that dropped the engine's sentences and shipped only
    its own would be, and this catches that too (the engine's need/risk notes are
    always present, so the count can never fall)."""
    for label, drive in (("legacy", run), ("composed", composed_run)):
        for pick in drive["picks"]:
            for rec in pick["top5"]:
                assert rec["reasons"], (
                    f"{label} pick {pick['overall']}: {rec['name']} has no reasons"
                )
                assert all(isinstance(r, str) and r.strip() for r in rec["reasons"])


# ------------------------------------------------- the DEFAULT (composed) engine
#
# Item 3.11. Everything below drives ``draft-web``'s real default path.


def test_the_frozen_weekly_points_map_is_the_objective_the_composed_engine_grades_with(
    weekly, board
):
    """The frozen objective input describes THIS board, in THIS id space.

    ``grader.assert_board_coverage`` is the gate that separates "this candidate
    is worth nothing" from "this candidate is not in the map" — a diverged id
    space grades every roster as a season of holes and reads exactly like a real
    answer (``variant_weekwise.WeekwiseInputs.build``'s reason for existing). It
    is asserted here so the fixture cannot rot into a plausible-looking lie.
    """
    from ziggurat.draft import grader

    grader.assert_board_coverage(board, weekly)
    assert weekly.positions and weekly.names and weekly.teams, (
        "the frozen points map lost its positions/names/teams metadata — the "
        "grader's opponent-field model degrades to None without it, silently"
    )
    priced_with_weeks = sum(1 for e in board if e.house_points and weekly.get(e.player_id))
    assert priced_with_weeks >= 500, (
        f"only {priced_with_weeks} priced board entries carry any week in the "
        "frozen map; the composed engine would be grading a season of holes"
    )


@pytest.mark.skipif(not LIVE_DB.exists(), reason="live database not present")
def test_the_live_weekly_points_map_still_matches_the_frozen_one(board, weekly):
    """The board fixture's twin drift check, for the objective input.

    The composed engine's decisions are a function of the board AND this map.
    Freezing only the board would leave half the default path's input unwatched
    — a re-pricing of the projections spine would move every recommendation with
    nothing but this to catch it (limitation (a), aimed at the engine that runs
    tonight). Skips when the DB is absent, exactly like the board check.
    """
    live = _load_live_weekly(board)
    frozen = weekly
    assert set(live) == set(frozen), (
        f"the live points map has {len(live)} keys, the frozen one {len(frozen)}; "
        "the id space moved. Re-bless with --bless-weekly and say why."
    )
    diffs = []
    for pid in sorted(frozen):
        a, b = dict(live.get(pid) or {}), dict(frozen.get(pid) or {})
        if set(a) != set(b):
            diffs.append(f"{pid}: weeks {sorted(b)} -> {sorted(a)}")
        else:
            for w in sorted(b):
                if a[w] != _approx(b[w]):
                    diffs.append(f"{pid} wk{w}: {b[w]:.4f} -> {a[w]:.4f}")
        if len(diffs) >= 12:
            break
    assert not diffs, (
        f"the live week-by-week points map has moved away from the frozen one "
        f"(first {len(diffs)}):\n  " + "\n  ".join(diffs) +
        "\n\nThis is the OBJECTIVE the default engine re-ranks with, so every "
        "composed recommendation is downstream of it. Re-bless deliberately:\n"
        "  .venv/bin/python tests/test_draft_golden.py --bless-weekly"
    )


def test_the_composed_drive_is_the_default_engine(composed_run, composed_golden):
    """Provenance, and the assertion that this drive is not secretly the legacy one.

    ``DraftSession`` selects the engine from whether it was handed a points map,
    so a fixture that failed to load would silently produce a LEGACY drive and
    every assertion below would pass against a re-blessed legacy golden. The
    session's own ``engine_profile`` is the witness.
    """
    from ziggurat.draft.session import ENGINE_COMPOSED

    assert composed_run["engine_profile"] == ENGINE_COMPOSED
    assert composed_golden["engine_profile"] == ENGINE_COMPOSED
    assert composed_golden["schema"] == _GOLDEN_SCHEMA
    assert composed_golden["board"] == {"count": BOARD_COUNT, "hash": BOARD_HASH}
    assert composed_golden["room"]["rollouts"] == ROLLOUTS
    assert composed_golden["weekwise_weight"] == 2.0
    assert composed_run["autodraft_seats"] == composed_golden["room"]["autodraft_seats"]


def test_the_sixteen_composed_recommendations_are_unchanged(composed_golden, composed_run):
    """THE golden assertion for the engine that drafts tonight.

    Same wide, brittle shape as its legacy twin: every number and every sentence
    the cockpit puts in front of the operator at each of his 16 picks. A change
    here is the delta the change under way was supposed to produce, and it must
    be read and explained before it is blessed.
    """
    assert [p["overall"] for p in composed_run["picks"]] == list(OPERATOR_OVERALL_PICKS)
    diffs: list[str] = []
    for got_pick, want_pick in zip(
        composed_run["picks"], composed_golden["picks"], strict=True
    ):
        overall = want_pick["overall"]
        assert got_pick["overall"] == overall and got_pick["round"] == want_pick["round"]
        gr, wr = got_pick["recalibration"], want_pick["recalibration"]
        if gr["engaged"] != wr["engaged"] or gr["n_room_picks"] != wr["n_room_picks"]:
            diffs.append(f"pick {overall}: live recalibration {wr} -> {gr}")
        for i, (got, want) in enumerate(
            zip(got_pick["top5"], want_pick["top5"], strict=True), start=1
        ):
            diffs.extend(_diff_recs(f"pick {overall} #{i}", got, want))

    assert not diffs, (
        f"{len(diffs)} DEFAULT-engine behaviour change(s) vs the composed golden:\n  "
        + "\n  ".join(diffs)
        + "\n\nFIRST: did the BOARD or the POINTS MAP move? If either drift check "
        "also failed, these diffs are the inputs moving, not the engine.\n"
        "If this is the intended delta, re-bless deliberately:\n"
        "  .venv/bin/python tests/test_draft_golden.py --bless-engine\n"
        "and record WHY in the commit message."
    )


def test_the_composed_drafted_roster_is_unchanged(composed_golden, composed_run):
    """The 16 players the default engine actually takes, in order."""
    got = [(r["overall"], r["position"], r["player_id"]) for r in composed_run["operator_roster"]]
    want = [(r["overall"], r["position"], r["player_id"]) for r in composed_golden["operator_roster"]]
    assert got == want
    assert composed_run["roster_shape"] == composed_golden["roster_shape"]
    assert sum(composed_run["roster_shape"].values()) == ROUNDS


def test_the_composed_engine_differs_from_legacy_exactly_here(run, composed_run):
    """The DELTA between the two engines, pinned as a number and a pick list.

    This is the assertion that makes the two fixtures mean something together
    rather than separately. It is computed from two LIVE drives — never from the
    two JSON files — so re-blessing both fixtures cannot make it vacuously true.

    It is deliberately an EQUALITY, not a bound. "At most N picks move" would
    stay green while the change under way quietly stopped doing anything, which
    is the failure mode a variant this small is most exposed to.
    """
    legacy = [(r["overall"], r["player_id"]) for r in run["operator_roster"]]
    composed = [(r["overall"], r["player_id"]) for r in composed_run["operator_roster"]]
    moved = tuple(o for (o, a), (_o, b) in zip(legacy, composed, strict=True) if a != b)
    assert moved == COMPOSED_MOVED_OVERALLS, (
        f"the composed engine now diverges from legacy at overalls {list(moved)}, "
        f"frozen at {list(COMPOSED_MOVED_OVERALLS)}. Both fixtures may be green "
        "and this still failed — that means the two engines' relationship moved, "
        "which is the change itself."
    )
    assert len(moved) == COMPOSED_PICKS_MOVED


def test_the_kdst_divergence_play_survives_the_rerank(run, composed_run):
    """The system's clearest edge, asserted by name under BOTH engines.

    The engine takes a D/ST around overall 89 and a kicker around 92 while the
    room takes theirs around 141-152, because the house scoring values both D/ST
    brackets and the distance kicker in ways ESPN's default board does not
    (CLAUDE.md 2.3; the runbook tells the operator to let it happen). It emerges
    from urgency rather than a special case, which is exactly why a re-rank could
    eat it — and why that must fail here rather than be buried among 160 other
    numbers in the recommendation diff.
    """
    for label, drive in (("legacy", run), ("composed", composed_run)):
        by_overall = {r["overall"]: r["position"] for r in drive["operator_roster"]}
        assert by_overall.get(DST_OVERALL) == "DST", (
            f"{label}: overall {DST_OVERALL} is a "
            f"{by_overall.get(DST_OVERALL)}, not the D/ST divergence pick"
        )
        assert by_overall.get(KICKER_OVERALL) == "K", (
            f"{label}: overall {KICKER_OVERALL} is a "
            f"{by_overall.get(KICKER_OVERALL)}, not the kicker divergence pick"
        )


def test_the_composed_engine_costs_exactly_the_same_search_as_legacy(board, weekly):
    """The composed engine's cost gate: the SEARCH did not grow — at all.

    Neither re-rank is allowed to multiply the search. The week-by-week term is a
    re-rank of the shortlist the engine already produced, at the top level only
    (never inside the rollout, which would multiply it by ``rollouts``, and never
    inside a posture projection, which would multiply it by thousands). The pair
    term REPLACES the engine's rollout at its 8 picks with the same loop over the
    same room, so it costs the same batch rather than a second one.

    Hence the totals must be IDENTICAL to legacy's, to the simulated pick — with
    exactly 8 of the 16 calls coming through the pair path, which is the
    disjointness the whole composition rests on. A future change that graded
    inside the rollout, or that rolled twice for one decision, would still be
    green on wall clock on a fast box; it cannot be green here.
    """
    with count_rollout_work() as work:
        drive_golden_draft(board, weekly=weekly)
    assert (work["calls"], work["sim_picks"]) == (
        ROLLOUT_SURVIVAL_CALLS, ROLLOUT_SIM_PICKS
    ), (
        f"the composed engine consumed {work['calls']} rollout calls / "
        f"{work['sim_picks']:,} simulated picks against legacy's "
        f"{ROLLOUT_SURVIVAL_CALLS} / {ROLLOUT_SIM_PICKS:,}. Either a re-rank has "
        "moved inside the search, or a decision is now rolling twice."
    )
    assert work["pair_calls"] == COMPOSED_PAIR_DECISIONS, (
        f"{work['pair_calls']} of the 16 decisions took the pair path, frozen at "
        f"{COMPOSED_PAIR_DECISIONS} (overalls 9, 29, 49, 69, 89, 109, 129, 149). "
        "0 would mean the pair term is silently inert; 16 would mean the "
        "week-by-week term is."
    )


def test_the_legacy_engine_never_takes_the_pair_path(board):
    """``--legacy-engine`` is the pre-3.11 engine, and this is what proves it is
    not merely a differently-configured new one."""
    with count_rollout_work() as work:
        drive_golden_draft(board)
    assert work["pair_calls"] == 0
    assert (work["calls"], work["sim_picks"]) == (ROLLOUT_SURVIVAL_CALLS, ROLLOUT_SIM_PICKS)


def test_the_composed_engine_is_idempotent_and_inside_the_ship_bar(board, weekly):
    """The default path's twin of the legacy timing/idempotence check.

    IDEMPOTENCE matters more here, not less: the re-rank calls
    ``grade_roster`` ``shortlist + 1`` times per decision over a roster
    COMPLETION model, and any dict-ordering or wall-clock dependence in that
    path would show up as two different answers on identical state — which is
    exactly what the cockpit's journal replay must never see.
    """
    result = drive_golden_draft(board, weekly=weekly, repeats=TIMING_REPEATS)
    for pick, runs in zip(result["picks"], result["repeat_payloads"], strict=True):
        first = runs[0]
        for j, other in enumerate(runs[1:], start=2):
            assert other == first, (
                f"composed pick {pick['overall']}: recommend() call #{j} disagreed "
                "with call #1 — the week-by-week re-rank is not a pure function of "
                "state"
            )
    worst_overall, worst_ms = max(result["recommend_ms"].items(), key=lambda kv: kv[1])
    profile_ms = ", ".join(f"{o}:{ms:.0f}" for o, ms in sorted(result["recommend_ms"].items()))
    assert worst_ms <= RECOMMEND_SHIP_BAR_MS, (
        f"composed recommend() took {worst_ms:.1f} ms at overall pick "
        f"{worst_overall} (best of {TIMING_REPEATS}). Ship bar "
        f"{RECOMMEND_SHIP_BAR_MS:.0f} ms. Profile (ms): {profile_ms}"
    )
    if worst_ms > RECOMMEND_OBSERVED_MS:
        warnings.warn(
            f"test_draft_golden: worst COMPOSED recommend() {worst_ms:.1f} ms at "
            f"overall pick {worst_overall} exceeds the item-2.4 observation of "
            f"{RECOMMEND_OBSERVED_MS:.0f} ms (still "
            f"{RECOMMEND_SHIP_BAR_MS / worst_ms:.0f}x inside the ship bar, NOT a "
            "failure). The measured re-rank overhead is ~11 ms; a busy box moves "
            f"the baseline far more than that. Profile (ms): {profile_ms}",
            stacklevel=2,
        )


def test_the_drafted_roster_is_unchanged(golden, run):
    """The 16 players the engine actually takes, in order — the legible summary
    of the golden, and the shape the current diagnosis is about (3 QB and 3 TE
    on a roster that starts one of each, against 3 RB on a roster that starts
    two plus a flex)."""
    got = [(r["overall"], r["position"], r["player_id"]) for r in run["operator_roster"]]
    want = [(r["overall"], r["position"], r["player_id"]) for r in golden["operator_roster"]]
    assert got == want
    assert run["roster_shape"] == golden["roster_shape"]
    assert sum(run["roster_shape"].values()) == ROUNDS


# --------------------------------------------------------------- the cost gates


def test_the_survival_rollout_cost_is_unchanged(board):
    """THE cost gate: deterministic, load-free, and it means something.

    The engine's cost is dominated by Monte-Carlo survival: ``rollout_survival``
    runs ONCE per on-clock decision (batching every candidate from one set of
    rollouts) and simulates ``rollouts * picks_until_next`` opponent picks. Both
    numbers fall straight out of the search and the snake geometry, so they are
    bit-identical on an idle box and on a thrashing one — which is exactly what a
    wall clock is not (see :func:`test_recommend_is_idempotent_and_inside_the_ship_bar`).

    This is what catches the regressions the stopwatch was being asked to catch:
    a bumped ``rollouts``, a widened survival window, or a rollout moved inside the
    per-candidate loop (16 calls -> hundreds). It is deliberately BLIND to a
    changed objective that keeps the same search — the recommendation golden and
    the 25-draft profile own that.
    """
    with count_rollout_work() as work:
        drive_golden_draft(board)

    assert work["calls"] == ROLLOUT_SURVIVAL_CALLS, (
        f"survival rollout ran {work['calls']} times over the operator's 16 picks, "
        f"frozen at {ROLLOUT_SURVIVAL_CALLS} (once per on-clock decision). More "
        "than one per pick means the batched-per-decision contract in "
        "survival.rollout_survival's docstring is broken and cost scales with the "
        "candidate set."
    )
    assert work["sim_picks"] == ROLLOUT_SIM_PICKS, (
        f"the 16 decisions simulated {work['sim_picks']:,} opponent picks, frozen "
        f"at {ROLLOUT_SIM_PICKS:,} (= rollouts x picks-until-next, summed). "
        "rollouts, the survival window, or the pick geometry moved."
    )


def test_recommend_is_idempotent_and_inside_the_ship_bar(board):
    """Two properties the draft night depends on, measured together on the real
    board at the production ``rollouts=512``.

    IDEMPOTENCE: ``DraftSession.recommend`` builds a fresh state-seeded ctx per
    compute, so calling it twice on the same state must give the same answer.
    (Item 2.4 recorded that ``engine.recommend`` on a HELD ctx does NOT — it
    consumes ``ctx.rng`` — so this is a real property of session's construction,
    not a tautology. Mutating the seed derivation breaks this test.)

    WALL CLOCK: asserted against ``RECOMMEND_SHIP_BAR_MS`` (5 s), which is the
    requirement IMPLEMENTATION_PLAN 2.4 actually records — the thing that makes a
    synchronous recompute safe on the confirmed 90 s pick clock. The 243 ms in
    that same sentence is an OBSERVATION on one box on one day, and asserting it
    was measurably wrong: with 24 competing CPU-bound processes this drive's worst
    pick goes 168.9 -> 271.4 ms (measured 2026-08-30, best of 3), and
    ``time.process_time`` moves identically (270.9 ms) so neither best-of-N nor a
    CPU clock absorbs it — the process genuinely burns more CPU per unit of work
    under cache pressure. On draft eve, with several agents running suites at once,
    that is a RED ship gate caused by nothing but a neighbour, while the engine is
    still ~18x inside the real bar. Exceeding 243 ms therefore WARNS with the
    numbers; only 5 s fails. The regression detector that actually holds is
    :func:`test_the_survival_rollout_cost_is_unchanged`, which no neighbour moves.
    """
    result = drive_golden_draft(board, repeats=TIMING_REPEATS)

    for pick, runs in zip(result["picks"], result["repeat_payloads"], strict=True):
        first = runs[0]
        for j, other in enumerate(runs[1:], start=2):
            assert other == first, (
                f"pick {pick['overall']}: recommend() call #{j} disagreed with call #1 — "
                "the session's per-compute ctx is no longer state-seeded"
            )

    worst_overall, worst_ms = max(result["recommend_ms"].items(), key=lambda kv: kv[1])
    profile_ms = ", ".join(
        f"{o}:{ms:.0f}" for o, ms in sorted(result["recommend_ms"].items())
    )
    assert worst_ms <= RECOMMEND_SHIP_BAR_MS, (
        f"recommend() took {worst_ms:.1f} ms at overall pick {worst_overall} "
        f"(best of {TIMING_REPEATS}) at rollouts={ROLLOUTS} on the real "
        f"{len(board)}-row board. The item-2.4 ship bar is "
        f"{RECOMMEND_SHIP_BAR_MS:.0f} ms — blowing THAT means the cockpit can no "
        f"longer recompute synchronously. Full profile (ms): {profile_ms}"
    )
    if worst_ms > RECOMMEND_OBSERVED_MS:
        warnings.warn(
            f"test_draft_golden: worst recommend() {worst_ms:.1f} ms at overall pick "
            f"{worst_overall} exceeds the item-2.4 OBSERVATION of "
            f"{RECOMMEND_OBSERVED_MS:.0f} ms (still "
            f"{RECOMMEND_SHIP_BAR_MS / worst_ms:.0f}x inside the {RECOMMEND_SHIP_BAR_MS:.0f} ms "
            "ship bar, so this is NOT a failure). On a busy box this is expected: a "
            "24-process load moved this exact drive 168.9 -> 271.4 ms on the capture "
            "box. If the box was idle, look at "
            "test_the_survival_rollout_cost_is_unchanged — if that is green the "
            "search did not grow, and the slowdown is per-candidate work. "
            f"Full profile (ms): {profile_ms}",
            stacklevel=2,
        )


# --------------------------------------------------------- the 25-draft profile


def test_the_25_draft_profile_is_unchanged(golden, profile):
    """The strategy profile: 25 independent rooms, seed 42, slot 9 (~30 s).

    The one-draft golden above pins a single path; this pins the DISTRIBUTION,
    which is what a change to the objective function actually moves. It is the
    slowest test in this file and it earns that: it is the only place a roster-
    shape regression across many rooms is visible.
    """
    got = profile
    want = golden["run_many_25"]
    assert got["n"] == want["n"] == 25
    assert got["operator_slot"] == want["operator_slot"] == OPERATOR_SLOT_1BASED
    for field in (
        "points_mean", "points_p10", "points_p50", "points_p90", "points_min", "points_max",
    ):
        assert got[field] == _approx(want[field]), (
            f"{field} moved: {want[field]!r} -> {got[field]!r}"
        )
    assert set(got["position_counts_mean"]) == set(want["position_counts_mean"])
    for pos, mean in want["position_counts_mean"].items():
        assert got["position_counts_mean"][pos] == _approx(mean), (
            f"mean {pos} per draft moved: {mean!r} -> {got['position_counts_mean'][pos]!r}"
        )


def test_the_profile_records_the_one_starter_slot_stacking(profile, golden):
    """The diagnosis, stated as an assertion so a fix is visibly a fix.

    The league starts exactly ONE QB and ONE TE (no superflex). Per-draft counts
    are integers, so a mean of EXACTLY 3.0 over 25 drafts means every single
    draft took three of each — 6 of 16 picks, 37.5%, on two one-starter slots —
    while RB, which fills two starters plus most flexes, comes back at 3.16.

    This test is expected to FAIL once the objective learns about weekly lineups
    and byes. When it does, that is the win: update the recorded numbers here in
    the same commit that earns them, and say what moved.

    IT ASSERTS AGAINST ``profile`` — the engine running NOW, 25 live rooms — and
    NOT against ``golden``. The earlier version read the numbers out of the JSON
    file it had just loaded, so it executed no engine code at all: patching
    ``engine._value_fraction`` to return 1.0 (disabling the lineup-reachability
    discount, precisely the class of change this golden exists for) failed the
    profile and the roster tests while THIS one stayed green, inverting the
    failure order its own docstring promises. The fixture cross-check below is
    kept as a SECOND assertion so a re-bless that quietly moved the shape without
    updating these literals still fails.
    """
    counts = profile["position_counts_mean"]
    assert counts["QB"] == 3.0
    assert counts["TE"] == 3.0
    assert counts["RB"] == _approx(3.16)
    assert counts["WR"] == _approx(4.84)
    assert counts["DST"] == 1.0 and counts["K"] == 1.0
    assert sum(counts.values()) == _approx(float(ROUNDS))
    # Second witness: the frozen fixture must still describe the same shape, so a
    # bless cannot move the recorded diagnosis without moving these literals too.
    assert golden["run_many_25"]["position_counts_mean"] == pytest.approx(
        counts, rel=1e-9, abs=1e-9
    )


# ------------------------------------------------------------------- blessing
#
# Deliberately NOT reachable from pytest: no environment variable can rewrite a
# golden during an ordinary suite run. One command, run by a human, that prints
# what moved and leaves the diff for review.


def _write_board_fixture() -> tuple[tuple[BoardEntry, ...], dict]:
    if not LIVE_DB.exists():
        raise SystemExit(f"cannot re-capture the board: {LIVE_DB} is absent")
    entries = _load_live_board()
    rows = [
        [e.player_id, e.name, e.position, int(e.espn_overall_rank),
         e.house_points, e.vor, e.team]
        for e in entries
    ]
    payload = {
        "_comment": (
            "Frozen live draft board (ziggurat.draft.simulator.load_board) at "
            f"as_of={AS_OF}, season={SEASON}. PUBLIC DATA ONLY (Rule 5): player "
            "names, the public ESPN editorial overall rank, and house "
            "projections/VOR derived from public Sleeper projections through "
            "ziggurat/core/scoring.py. Re-bless: "
            "python tests/test_draft_golden.py --bless"
        ),
        "as_of": AS_OF,
        "season": SEASON,
        "count": len(entries),
        "hash": _board_hash(entries),
        "columns": ["player_id", "name", "position", "espn_overall_rank",
                    "house_points", "vor", "team"],
        "rows": rows,
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    BOARD_FIXTURE.write_text(_dump_rows(payload), encoding="utf-8")
    return entries, payload


def _write_weekly_fixture() -> None:
    """Re-capture the week-by-week points map the composed engine grades with."""
    if not LIVE_DB.exists():
        raise SystemExit(f"cannot re-capture the points map: {LIVE_DB} is absent")
    entries, _payload = _load_fixture_board()
    weekly = _load_live_weekly(entries)
    # Every key is written, INCLUDING the ones with no weeks: "present with no
    # weeks" and "absent" are different facts to the grader's coverage gate, and
    # collapsing them would freeze a map that cannot fail the way the live one can.
    payload = {
        "_comment": (
            "Frozen week-by-week house points (ziggurat.draft.grader."
            f"weekly_points_map) at as_of={AS_OF}, season={SEASON}. This is the "
            "OBJECTIVE the default (composed) engine re-ranks with; it is frozen "
            "so that engine is testable with no database. PUBLIC DATA ONLY "
            "(Rule 5): names/positions/teams and points derived from public "
            "Sleeper projections through ziggurat/core/scoring.py. Re-bless: "
            "python tests/test_draft_golden.py --bless-weekly"
        ),
        "as_of": AS_OF,
        "season": SEASON,
        "points": {
            pid: {str(w): p for w, p in sorted((weekly.get(pid) or {}).items())}
            for pid in sorted(weekly)
        },
        "positions": {k: weekly.positions[k] for k in sorted(weekly.positions)},
        "names": {k: weekly.names[k] for k in sorted(weekly.names)},
        "teams": {k: weekly.teams[k] for k in sorted(weekly.teams)},
    }
    FIXTURES.mkdir(parents=True, exist_ok=True)
    WEEKLY_FIXTURE.write_text(
        json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8"
    )
    priced = sum(1 for pid in weekly if weekly.get(pid))
    print(
        f"  {len(payload['points'])} keys ({priced} carrying at least one week)\n"
        f"wrote {WEEKLY_FIXTURE.relative_to(REPO_ROOT)}"
    )


def _bless_weekly() -> None:
    print(f"re-capturing the points map from {LIVE_DB} (read-only) at as_of={AS_OF} ...")
    _write_weekly_fixture()
    print(
        "\nThe engine goldens were NOT touched. Re-run this file now: the COMPOSED "
        "recommendation diffs are the objective-attributable delta (the legacy "
        "engine does not read this map at all, so its golden cannot move).\n"
    )


def _dump_rows(payload: dict) -> str:
    """JSON with one board row per line — 3,264 rows have to be diffable."""
    head = ",\n".join(
        f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in payload.items() if k != "rows"
    )
    body = ",\n".join("    " + json.dumps(r) for r in payload["rows"])
    return "{\n" + head + ',\n  "rows": [\n' + body + "\n  ]\n}\n"


_TWO_STEP = (
    "THE STEPWISE RECIPE (why the fixtures bless separately):\n"
    "  1. --bless-board   with the engine code UNCHANGED, then re-run this file.\n"
    "     The recommendation diffs it prints are then attributable to the BOARD\n"
    "     alone. Record them.\n"
    "  2. --bless-weekly  likewise for the composed engine's OBJECTIVE input. The\n"
    "     legacy golden cannot move from this (it never reads the map), so a\n"
    "     composed-only diff here is attributable to the objective.\n"
    "  3. --bless-engine  to adopt the new baseline for BOTH engines. A later diff\n"
    "     on top of that is attributable to the engine code.\n"
    "Blessing them in one action confounds 'the inputs moved' with 'the engine\n"
    "moved', which is the one distinction this module exists to preserve."
)


def _bless_board() -> None:
    """Re-capture ONLY the board fixture. Leaves the engine golden untouched."""
    print(f"re-capturing the board from {LIVE_DB} (read-only) at as_of={AS_OF} ...")
    _entries, payload = _write_board_fixture()
    print(f"  {payload['count']} rows, hash {payload['hash']}")
    if payload["hash"] != BOARD_HASH or payload["count"] != BOARD_COUNT:
        print(
            f"  !! BOARD MOVED: this module's constants say "
            f"{BOARD_COUNT} rows / {BOARD_HASH}. Update BOARD_HASH / BOARD_COUNT "
            f"in tests/test_draft_golden.py in the same commit."
        )
    nameless = sum(1 for r in payload["rows"] if r[1] is None)
    priced = [r for r in payload["rows"] if r[4] != 0.0]
    priced_nameless = sum(1 for r in priced if r[1] is None)
    print(
        f"  column shape: {nameless} nameless (frozen {BOARD_NAMELESS}), "
        f"{len(priced)} priced (frozen {BOARD_PRICED}), "
        f"{priced_nameless} priced-and-nameless (frozen {BOARD_PRICED_NAMELESS})"
    )
    if (nameless, len(priced), priced_nameless) != (
        BOARD_NAMELESS, BOARD_PRICED, BOARD_PRICED_NAMELESS
    ):
        print(
            "  !! COLUMN SHAPE MOVED: update BOARD_NAMELESS / BOARD_PRICED / "
            "BOARD_PRICED_NAMELESS, and say which way the crosswalk went. A jump "
            "in nameless rows means players the cockpit resolver can no longer find."
        )
    print(f"\nwrote {BOARD_FIXTURE.relative_to(REPO_ROOT)}\n")
    print(
        "The engine golden was NOT touched. Re-run this file now: the "
        "recommendation diffs are the board-attributable delta.\n"
    )
    print(_TWO_STEP)


def _bless_engine() -> None:
    """Re-drive and rewrite BOTH engine goldens, off the fixtures on disk.

    They bless together because they are two drives of the SAME experiment: the
    thing this module is for is the DIFFERENCE between them, and blessing one
    without the other silently changes what that difference means.
    """
    _bless_one_engine(weekly=None, fixture=GOLDEN_FIXTURE, label="legacy")
    print()
    _bless_one_engine(
        weekly=_load_fixture_weekly(), fixture=COMPOSED_FIXTURE, label="composed"
    )


def _bless_one_engine(*, weekly, fixture: Path, label: str) -> None:
    """Re-drive and rewrite ONE engine golden, off the board fixture on disk."""
    entries, payload = _load_fixture_board()
    print(
        f"[{label}] driving the golden draft off {BOARD_FIXTURE.name} "
        f"({payload['count']} rows, hash {payload['hash']}); slot "
        f"{OPERATOR_SLOT_1BASED}, seed {SESSION_SEED}, rollouts={ROLLOUTS} ..."
    )
    with count_rollout_work() as work:
        result = drive_golden_draft(entries, repeats=TIMING_REPEATS, weekly=weekly)
    worst = max(result["recommend_ms"].values())
    print(
        f"  worst recommend(): {worst:.1f} ms (ship bar "
        f"{RECOMMEND_SHIP_BAR_MS:.0f} ms; item-2.4 observation "
        f"{RECOMMEND_OBSERVED_MS:.0f} ms — wall clock is machine- and load-dependent, "
        "it is not the gate)"
    )
    calls = work["calls"] // max(1, TIMING_REPEATS)
    sim = work["sim_picks"] // max(1, TIMING_REPEATS)
    print(
        f"  survival cost: {calls} rollout call(s) per drive (frozen "
        f"{ROLLOUT_SURVIVAL_CALLS}), {sim:,} simulated picks (frozen "
        f"{ROLLOUT_SIM_PICKS:,})"
    )
    if (calls, sim) != (ROLLOUT_SURVIVAL_CALLS, ROLLOUT_SIM_PICKS):
        print(
            "  !! SEARCH COST MOVED: update ROLLOUT_SURVIVAL_CALLS / "
            "ROLLOUT_SIM_PICKS in tests/test_draft_golden.py and say what grew."
        )
    for p in result["picks"]:
        top = p["top5"][0]
        print(f"  pick {p['overall']:>3} R{p['round']:<2} -> {top['name']} "
              f"({top['position']}) score={top['pick_score']:.2f}")

    golden = {
        "_comment": (
            f"GOLDEN MASTER of the {label.upper()} ziggurat.draft engine. Generated "
            "by `python tests/test_draft_golden.py --bless-engine`; asserted by "
            "tests/test_draft_golden.py. Rule 5: public data only — the nine "
            "rival seats are the calibrated 2.2 simulation, not real managers."
        ),
        "schema": _GOLDEN_SCHEMA,
        "engine_profile": result["engine_profile"],
        "weekwise_weight": 0.0 if weekly is None else 2.0,
        "as_of": AS_OF,
        "season": SEASON,
        "board": {"count": payload["count"], "hash": payload["hash"]},
        "room": {
            "seed": SESSION_SEED,
            "rollouts": ROLLOUTS,
            "priors": "ROOM_PRIORS_2025",
            "operator_seat_0based": OPERATOR_SEAT,
            "operator_slot_1based": OPERATOR_SLOT_1BASED,
            "pick_order": list(PICK_ORDER),
            "autodraft_seats": result["autodraft_seats"],
        },
        "operator_overall_picks": list(OPERATOR_OVERALL_PICKS),
        "picks": result["picks"],
        "operator_roster": result["operator_roster"],
        "roster_shape": result["roster_shape"],
    }
    if weekly is None:
        # The 25-draft profile is the LEGACY fixture's alone: it drives
        # ``run_many`` with a bare PickEngine (no session, no points map), which
        # is the surface the 3-QB/3-TE diagnosis is a number on. Re-running it
        # for the composed arm would need a different harness and would freeze a
        # second ~30 s cost for no assertion this module makes.
        print("running the 25-draft profile (~30 s) ...")
        profile = profile_run_many(entries)
        print(f"  points mean {profile['points_mean']:.1f}, "
              f"shape {profile['position_counts_mean']}")
        golden["run_many_25"] = profile

    fixture.write_text(
        json.dumps(golden, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(
        f"\nwrote {fixture.relative_to(REPO_ROOT)}\n"
        "NOW: review `git diff tests/fixtures/draft/` and say WHY the behaviour "
        "moved in the commit message. A bless with an unexplained diff defeats "
        "the point of the golden."
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--bless-board" in args:
        _bless_board()
    elif "--bless-weekly" in args:
        _bless_weekly()
    elif "--bless-engine" in args:
        _bless_engine()
    elif "--bless" in args:
        print(
            "!! --bless re-captures the BOARD, the POINTS MAP and BOTH ENGINE\n"
            "   GOLDENS in one action. If either input may have moved (espn_ranks /\n"
            "   projections / adp_rankings are daily, current-value-only sources and\n"
            "   a near-day refresh is scheduled work), this destroys the attribution.\n"
        )
        print(_TWO_STEP + "\n")
        print("proceeding with all of them ...\n")
        _bless_board()
        print()
        _bless_weekly()
        print()
        _bless_engine()
    else:
        print(__doc__)
        exe, here = sys.executable, os.path.relpath(__file__)
        print(f"\nto re-bless the board only:      {exe} {here} --bless-board")
        print(f"to re-bless the points map only: {exe} {here} --bless-weekly")
        print(f"to re-bless both goldens:        {exe} {here} --bless-engine")
        print(f"to re-bless everything:          {exe} {here} --bless")
        raise SystemExit(2)
