"""Unit tests for the draft bots + roster-legality core (item 2.2).

All offline — synthetic boards only (Rule 5). Covers min_to_complete / legality
forcing, positional-need + cap + K/DST-window filtering, and each picker's
selection rule (incl. RankNoiseBot determinism).
"""

import random

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER as ROSTER
from ziggurat.draft.bots import (
    AutodraftBot,
    BoardEntry,
    FollowEspnRank,
    FollowVor,
    PickContext,
    RankNoiseBot,
    allowed_positions,
    legal_positions,
    min_to_complete,
    needed_positions,
    position_counts,
)
from ziggurat.draft.priors import RoomPriors
from ziggurat.draft.simulator import optimal_starting_points


def _entry(pid, pos, rank, pts=0.0, vor=0.0):
    return BoardEntry(pid, pid, pos, rank, pts, vor)


# ----------------------------------------------------------------- legality


def test_min_to_complete_empty_and_full():
    assert min_to_complete({}, ROSTER) == 9  # QB,2RB,2WR,TE,FLEX,DST,K
    full = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1}
    assert min_to_complete(full, ROSTER) == 1  # still owes the FLEX
    with_flex = {"QB": 1, "RB": 3, "WR": 2, "TE": 1, "DST": 1, "K": 1}
    assert min_to_complete(with_flex, ROSTER) == 0  # the 3rd RB covers FLEX


def test_flex_only_counts_surplus_beyond_dedicated():
    # 2 RB exactly fills dedicated RB; it does NOT also cover the flex.
    assert min_to_complete({"RB": 2}, ROSTER) == 7  # owes QB,2WR,TE,DST,K + flex... = 1+2+1+1+1+1
    # a WR beyond its 2 dedicated covers flex.
    assert min_to_complete({"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1, "K": 1}, ROSTER) == 0


def test_legal_positions_forces_kdst_at_the_end():
    # Roster has every starter but DST and K (min_to_complete == 2). With exactly
    # ONE pick after this one, this pick MUST be a DST or K or a hole is unfillable.
    counts = {"QB": 1, "RB": 3, "WR": 2, "TE": 1}  # flex covered by 3rd RB
    assert min_to_complete(counts, ROSTER) == 2
    assert legal_positions(counts, picks_after=1, roster=ROSTER) == {"DST", "K"}
    # With two picks after, this pick is still free — anything legal.
    assert legal_positions(counts, picks_after=2, roster=ROSTER) == {
        "QB", "RB", "WR", "TE", "DST", "K"
    }
    # Final pick (0 after), only K missing -> only K legal.
    counts_k = {"QB": 1, "RB": 3, "WR": 2, "TE": 1, "DST": 1}
    assert legal_positions(counts_k, picks_after=0, roster=ROSTER) == {"K"}


def test_needed_positions_tracks_unmet_starters_and_flex():
    assert needed_positions({}, ROSTER) == {"QB", "RB", "WR", "TE", "DST", "K"}
    # dedicated all met, flex still open -> flex-eligible positions are needed
    counts = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1}
    assert needed_positions(counts, ROSTER) == {"RB", "WR", "TE"}
    # flex covered -> nothing needed
    covered = {"QB": 1, "RB": 3, "WR": 2, "TE": 1, "DST": 1, "K": 1}
    assert needed_positions(covered, ROSTER) == set()


def test_allowed_positions_applies_caps_and_kdst_window():
    # Round 1, empty roster, plenty of picks: window drops K/DST.
    early = allowed_positions({}, picks_after=15, roster=ROSTER,
                              round_num=1, kdst_earliest_round=9)
    assert early == {"QB", "RB", "WR", "TE"}
    # Round 10, a realistic 9-player roster still owing DST+K: window is gone.
    mid = {"QB": 1, "RB": 4, "WR": 3, "TE": 1}  # 9 players, flex covered
    late = allowed_positions(mid, picks_after=6, roster=ROSTER,
                             round_num=10, kdst_earliest_round=9)
    assert "DST" in late and "K" in late
    # A team already holding a DST is capped out of a second one.
    capped_counts = {"QB": 1, "RB": 4, "WR": 3, "TE": 1, "DST": 1}  # 10 players
    capped = allowed_positions(capped_counts, picks_after=5, roster=ROSTER,
                               round_num=11, kdst_earliest_round=9)
    assert "DST" not in capped and "K" in capped


# ------------------------------------------------------------------ pickers


def _round1_board():
    # skill with a rank/vor DISAGREEMENT + deep K/DST so the board is legal-able.
    board = [
        _entry("WR-A", "WR", 1, pts=250, vor=40),   # best rank, modest vor
        _entry("WR-B", "WR", 2, pts=260, vor=90),   # 2nd rank, best vor
        _entry("RB-A", "RB", 3, pts=240, vor=60),
        _entry("QB-A", "QB", 4, pts=300, vor=20),
        _entry("K-A", "K", 200, pts=120, vor=2),    # deep, best rank K
        _entry("DST-A", "DST", 201, pts=130, vor=3),
    ]
    return board


def test_autodraft_takes_best_available_by_rank():
    ctx = PickContext.from_board(_round1_board(), roster=ROSTER, round=10, overall_pick=1)
    # round 10 so K/DST aren't windowed out; autodraft ignores the window anyway.
    assert AutodraftBot().pick(ctx) == "WR-A"  # rank 1


def test_follow_espn_vs_follow_vor_diverge():
    board = _round1_board()
    ctx_espn = PickContext.from_board(board, roster=ROSTER, round=1, overall_pick=1)
    ctx_vor = PickContext.from_board(board, roster=ROSTER, round=1, overall_pick=1)
    assert FollowEspnRank().pick(ctx_espn) == "WR-A"  # best ESPN rank
    assert FollowVor().pick(ctx_vor) == "WR-B"        # best house VOR


def test_strategies_respect_kdst_window_round_one():
    # A board where K/DST are ranked BEST; at round 1 the window must defer them.
    board = [
        _entry("K-A", "K", 1, pts=120, vor=99),
        _entry("DST-A", "DST", 2, pts=130, vor=99),
        _entry("WR-A", "WR", 3, pts=250, vor=10),
    ]
    ctx = PickContext.from_board(board, roster=ROSTER, round=1, overall_pick=1)
    assert FollowEspnRank().pick(ctx) == "WR-A"
    ctx2 = PickContext.from_board(board, roster=ROSTER, round=1, overall_pick=1)
    assert FollowVor().pick(ctx2) == "WR-A"


def test_ranknoise_defers_kdst_before_earliest_round(make_draft_board):
    board = make_draft_board()
    bot = RankNoiseBot()
    # Many round-1 picks: none should be a K or DST (window), all legal skill.
    for s in range(50):
        ctx = PickContext.from_board(board, roster=ROSTER, round=1, overall_pick=1,
                                     rng=random.Random(s))
        pid = bot.pick(ctx)
        pos = next(e.position for e in board if e.player_id == pid)
        assert pos not in ("K", "DST")


def test_ranknoise_never_drafts_second_dst(make_draft_board):
    board = make_draft_board()
    own = [e for e in board if e.position == "DST"][:1]  # already hold one DST
    ctx = PickContext.from_board(board, own_roster=own, roster=ROSTER,
                                 round=12, overall_pick=1, rng=random.Random(3))
    pid = RankNoiseBot().pick(ctx)
    pos = next(e.position for e in board if e.player_id == pid)
    assert pos != "DST"


def test_ranknoise_is_deterministic_for_a_fixed_rng(make_draft_board):
    board = make_draft_board()
    bot = RankNoiseBot()
    picks = []
    for _ in range(2):
        ctx = PickContext.from_board(board, roster=ROSTER, round=3, overall_pick=25,
                                     rng=random.Random(1234))
        picks.append(bot.pick(ctx))
    assert picks[0] == picks[1]


def test_ranknoise_reach_widens_with_sigma(make_draft_board):
    # With near-zero sigma the bot hugs the board; with large sigma it reaches.
    board = make_draft_board()
    tight = RankNoiseBot(RoomPriors(reach_sigma=0.01))
    loose = RankNoiseBot(RoomPriors(reach_sigma=60.0))
    ctx_t = PickContext.from_board(board, roster=ROSTER, round=1, overall_pick=1,
                                   rng=random.Random(5))
    # tight bot takes (near) the best-ranked allowed skill player (rank 1 skill).
    tight_pid = tight.pick(ctx_t)
    tight_rank = next(e.espn_overall_rank for e in board if e.player_id == tight_pid)
    assert tight_rank <= 2
    # loose bot's reach set spans the window; verify it can land off rank-1.
    ranks = set()
    for s in range(40):
        ctx = PickContext.from_board(board, roster=ROSTER, round=1, overall_pick=1,
                                     rng=random.Random(s))
        pid = loose.pick(ctx)
        ranks.add(next(e.espn_overall_rank for e in board if e.player_id == pid))
    assert max(ranks) > tight_rank  # reached past the chalk pick at least once


# ------------------------------------------------------- optimal lineup scoring


def test_optimal_starting_points_greedy_flex():
    roster = [
        _entry("qb", "QB", 1, pts=20),
        _entry("rb1", "RB", 2, pts=15), _entry("rb2", "RB", 3, pts=12), _entry("rb3", "RB", 4, pts=9),
        _entry("wr1", "WR", 5, pts=14), _entry("wr2", "WR", 6, pts=10),
        _entry("te", "TE", 7, pts=8),
        _entry("dst", "DST", 8, pts=5),
        _entry("k", "K", 9, pts=4),
    ]
    # QB20 + RB(15+12) + WR(14+10) + TE8 + DST5 + K4 + FLEX(best leftover = RB3 @9)
    assert optimal_starting_points(roster, ROSTER) == pytest.approx(97.0)


def test_position_counts():
    board = [_entry("a", "RB", 1), _entry("b", "RB", 2), _entry("c", "WR", 3)]
    assert position_counts(board) == {"RB": 2, "WR": 1}
