"""Draft tool — DELETABLE BY DESIGN (SPEC Feature 8).

Pick engine, live board TUI, and mock-draft simulator. Imports the permanent
valuation core; this whole package is deleted after draft day. Nothing outside
this package may import from it (enforced by tests/test_draft_boundary.py).

Public surface (item 2.2 mock-draft simulator):
  * priors:    RoomPriors, ROOM_PRIORS_2025
  * bots:      BoardEntry, PickContext, Picker,
               RankNoiseBot, AutodraftBot, FollowEspnRank, FollowVor
  * simulator: run_draft, run_many, load_board, DraftResult, StrategySummary,
               snake_sequence, optimal_starting_points, format_strategy_summary
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

__all__ = [
    "AutodraftBot",
    "BoardEntry",
    "DraftResult",
    "FollowEspnRank",
    "FollowVor",
    "ROOM_PRIORS_2025",
    "PickContext",
    "Picker",
    "RankNoiseBot",
    "RoomPriors",
    "StrategySummary",
    "format_strategy_summary",
    "load_board",
    "optimal_starting_points",
    "run_draft",
    "run_many",
    "snake_sequence",
]
