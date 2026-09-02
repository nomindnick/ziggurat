"""Draft tool — IMPORT-QUARANTINED BY DESIGN (SPEC Feature 8; Rule 8).

Pick engine, live board TUI, and mock-draft simulator. Imports the permanent
valuation core; nothing in ``ziggurat/`` outside this package may import from
it (enforced by tests/test_draft_boundary.py) — top-level ``backtest/`` MAY,
because it grades draft decisions (Rule 8, amended 2026-08-31). Retained across
seasons for reuse (same amendment — originally deletable; the quarantine was
always the load-bearing half). Retention is NOT next-August readiness: the
userscripts pin ESPN's 2026 draft-room DOM, the opponent priors are 2025-room
fits, and the goldens freeze the 2026-08-30 board — next season starts with
recalibration and DOM re-verification. If a permanent module ever needs
something living here, it is PORTED out, never imported.

Public surface:
  * priors:    RoomPriors, ROOM_PRIORS_2025
  * bots:      BoardEntry, PickContext, Picker,
               RankNoiseBot, AutodraftBot, FollowEspnRank, FollowVor
  * simulator: run_draft, run_many, load_board, load_draft_board, DraftInputs,
               DraftResult, StrategySummary, snake_sequence,
               optimal_starting_points, format_strategy_summary
  * engine (item 2.3): PickEngine, PickRec, ARCHETYPE_NEED_SCHEDULES, risk_sign
  * survival (item 2.3): rollout_survival, analytic_survival, SurvivalResult,
               recalibrate_from_pick_log, LiveRecalibration
  * evaluate (Phase-2 keystone; reached as a SUBMODULE —
               ``from ziggurat.draft import evaluate``): evaluate_strategy,
               paired_compare, build_paired_result, tournament, DraftOutcome,
               PairedResult, GradeFn, OPERATOR_SLOT_2026
  * grader (item 3.11; likewise ``from ziggurat.draft import grader``):
               grade_roster, WeeklyPointsMap, SeasonGrade,
               stream_levels_from_board, STREAMED_POSITIONS, capped_out,
               assert_board_coverage

``evaluate`` and ``grader`` are deliberately NOT re-exported into ``__all__``:
their consumer, ``backtest/draft_backtest.py``, imports both as modules and,
beyond the public names (including ``ROOM_PRIORS_2025`` via ``evaluate``),
reaches two private helpers: ``evaluate._draft_once`` and
``evaluate._seed_grid``. It MIRRORS ``grader._seat_week`` rather than importing
it (pinned by ``test_seat_totals_mirror_the_graders_own_weekly_means``), and
``evaluate._t_interval`` is reached only by ``tests/test_backtest_scorecards.py``,
which pins ``backtest/stats.py``'s copy against it bit-for-bit. That is
tolerated for a grading script but it is not a contract — re-exporting the
public names would advertise a surface the one consumer does not use, while
saying nothing about the private ones it does. This ledger is checked by
``tests/test_draft_boundary.py`` (every private evaluate helper it lists must
be referenced through the script's ``ev.`` alias, and vice versa), so it rots
loudly, not silently.

Item 3.11: ``load_draft_board`` is the DRAFT-NIGHT entry point — the board, the
week-by-week objective the composed engine re-ranks with, and the kicker
correction, all read at ONE ``as_of``. ``load_board`` stays the harness's plain
board loader (an experiment must be able to load the uncorrected board on
purpose), and its defaults are exactly the pre-3.11 ones.
"""

from ziggurat.draft.bots import (
    AutodraftBot,
    BoardEntry,
    FollowEspnRank,
    FollowVor,
    PickContext,
    Picker,
    RankNoiseBot,
)
from ziggurat.draft.engine import (
    ARCHETYPE_NEED_SCHEDULES,
    PickEngine,
    PickRec,
    risk_sign,
)
from ziggurat.draft.priors import ROOM_PRIORS_2025, RoomPriors
from ziggurat.draft.simulator import (
    DraftInputs,
    DraftResult,
    StrategySummary,
    format_strategy_summary,
    load_board,
    load_draft_board,
    optimal_starting_points,
    run_draft,
    run_many,
    snake_sequence,
)
from ziggurat.draft.survival import (
    LiveRecalibration,
    SurvivalResult,
    analytic_survival,
    recalibrate_from_pick_log,
    rollout_survival,
)

__all__ = [
    "ARCHETYPE_NEED_SCHEDULES",
    "AutodraftBot",
    "BoardEntry",
    "DraftInputs",
    "DraftResult",
    "FollowEspnRank",
    "FollowVor",
    "LiveRecalibration",
    "ROOM_PRIORS_2025",
    "PickContext",
    "PickEngine",
    "PickRec",
    "Picker",
    "RankNoiseBot",
    "RoomPriors",
    "StrategySummary",
    "SurvivalResult",
    "analytic_survival",
    "format_strategy_summary",
    "load_board",
    "load_draft_board",
    "optimal_starting_points",
    "recalibrate_from_pick_log",
    "risk_sign",
    "rollout_survival",
    "run_draft",
    "run_many",
    "snake_sequence",
]
