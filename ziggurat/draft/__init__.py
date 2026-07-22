"""Draft tool — DELETABLE BY DESIGN (SPEC Feature 8).

Pick engine, live board TUI, and mock-draft simulator. Imports the permanent
valuation core; this whole package is deleted after draft day. Nothing outside
this package may import from it (enforced by tests/test_draft_boundary.py).

Public surface:
  * priors:    RoomPriors, ROOM_PRIORS_2025
  * bots:      BoardEntry, PickContext, Picker,
               RankNoiseBot, AutodraftBot, FollowEspnRank, FollowVor
  * simulator: run_draft, run_many, load_board, DraftResult, StrategySummary,
               snake_sequence, optimal_starting_points, format_strategy_summary
  * engine (item 2.3): PickEngine, PickRec, ARCHETYPE_NEED_SCHEDULES, risk_sign
  * survival (item 2.3): rollout_survival, analytic_survival, SurvivalResult,
               recalibrate_from_pick_log, LiveRecalibration
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
    DraftResult,
    StrategySummary,
    format_strategy_summary,
    load_board,
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
    "optimal_starting_points",
    "recalibrate_from_pick_log",
    "risk_sign",
    "rollout_survival",
    "run_draft",
    "run_many",
    "snake_sequence",
]
