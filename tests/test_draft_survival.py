"""Survival-model tests for the 2.3 draft pick engine (item 2.3).

All offline — synthetic boards only (Rule 5). Covers the two survival routes
(sim-derived rollout + analytic fallback), their monotonicities, the K/DST round-
window distortion, rollout determinism, snake geometry, the opponent-need effect
of the new ``opponent_rosters`` field, and the live online recalibration utility.
"""

import random

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER as ROSTER
from ziggurat.draft.bots import BoardState, PickContext
from ziggurat.draft.priors import ROOM_PRIORS_2025
from ziggurat.draft.survival import (
    analytic_survival,
    recalibrate_from_pick_log,
    rollout_survival,
    upcoming_opponent_picks,
    wait_ok,
)


def _by_pos(board):
    d = {}
    for e in board:
        d.setdefault(e.position, []).append(e)
    for lst in d.values():
        lst.sort(key=lambda e: e.espn_overall_rank)
    return d


# --------------------------------------------------------- analytic route (2)


def test_analytic_better_rank_lower_survival():
    """At a fixed next pick, a better (lower) ESPN rank means lower survival."""
    o_next = 40
    s = [analytic_survival(r, "WR", o_next) for r in (5, 20, 40, 80)]
    assert s[0] < s[1] < s[2] < s[3]  # strictly increasing survival as rank worsens


def test_analytic_survival_drops_with_more_intervening_picks():
    """A fixed player's survival falls as the operator's next pick moves later."""
    s = [analytic_survival(30, "RB", o) for o in (20, 30, 40, 60)]
    assert s[0] > s[1] > s[2] > s[3]


def test_analytic_kdst_window_distortion():
    """K/DST survival is window-governed, NOT rank-governed: a deep-ranked K/DST
    survives ~1 well before the room's round-9+ run, then collapses across it."""
    # Before the window (next pick early): survives ~1 despite a deep ESPN rank.
    assert analytic_survival(250, "K", 40) > 0.98
    assert analytic_survival(300, "DST", 50) > 0.98
    # Inside the run (next pick ~150+): survival collapses.
    assert analytic_survival(250, "K", 160) < 0.4
    assert analytic_survival(300, "DST", 156) < 0.5
    # Rank is (almost) irrelevant for K: two very different ranks, same next pick.
    assert abs(analytic_survival(200, "K", 100) - analytic_survival(320, "K", 100)) < 1e-9


def test_analytic_unranked_player_survives():
    """A board-unranked (fallback) skill player is ~never drafted by the room."""
    assert analytic_survival(10_050, "WR", 120) == 1.0


# ------------------------------------------------- snake geometry / window size


def test_upcoming_opponent_picks_matches_snake_gaps(make_draft_board):
    """Round-1 intervening-pick counts match the analytic 10-team snake gaps:
    slot 1 -> 18, slot 5 -> 10, slot 10 -> 0 (the operator picks 1.10 then 2.01)."""
    board = make_draft_board()
    for slot0, expected in ((0, 18), (4, 10), (9, 0)):
        ctx = PickContext.from_board(board, team_slot=slot0, round=1,
                                     overall_pick=slot0 + 1)
        assert len(upcoming_opponent_picks(ctx)) == expected
    # every intervening pick belongs to a rival, none to the operator
    ctx = PickContext.from_board(board, team_slot=4, round=1, overall_pick=5)
    up = upcoming_opponent_picks(ctx)
    assert all(t != 4 for _o, t in up)
    assert [o for o, _t in up] == list(range(6, 6 + len(up)))  # contiguous picks


# ----------------------------------------------------- rollout route (1)


def _operator_ctx(board, *, slot=0, round=1, overall=1, taken=(), rng_seed=0):
    return PickContext.from_board(
        board, team_slot=slot, round=round, overall_pick=overall,
        taken=taken, rng=random.Random(rng_seed),
    )


def test_rollout_survival_monotonic_in_window_length(make_draft_board):
    """More intervening picks -> a top player's rollout survival is non-increasing."""
    board = make_draft_board()
    ctx = _operator_ctx(board)
    top = board[0]  # best ESPN rank

    def surv(n):
        up = [(1 + i + 1, (i % 9) + 1) for i in range(n)]
        res = rollout_survival(ctx, [top], rng=random.Random(0), rollouts=300, upcoming=up)
        return res.survival[top.player_id]

    s = [surv(n) for n in (2, 6, 12, 18)]
    assert s[0] > s[1] > s[2] > s[3]  # strictly falls here (top player, deep board)


def test_rollout_better_rank_lower_survival(make_draft_board):
    """At a fixed window, the best-ranked candidate survives less than a deep one."""
    board = make_draft_board()
    ctx = _operator_ctx(board)
    up = [(1 + i + 1, (i % 9) + 1) for i in range(12)]
    res = rollout_survival(ctx, [board[0], board[39]], rng=random.Random(1),
                           rollouts=300, upcoming=up)
    assert res.survival[board[0].player_id] < res.survival[board[39].player_id]


def test_rollout_kdst_survives_before_the_window(make_draft_board):
    """A deep-ranked K survives ~1 in an early-round rollout: the room defers K/DST
    until round 9, so no early rollout pick can consume it (window distortion)."""
    board = make_draft_board()
    ctx = _operator_ctx(board)  # slot 1, round 1: an 18-pick window, all rounds 1-2
    kcand = _by_pos(board)["K"][0]
    res = rollout_survival(ctx, [kcand], rng=random.Random(2), rollouts=200)
    assert res.survival[kcand.player_id] == 1.0


def test_rollout_zero_window_survival_is_one(make_draft_board):
    """At the snake turn (no intervening picks), every candidate survives w.p. 1 and
    the next-best VOR equals today's best available at each position."""
    board = make_draft_board()
    # slot 10 (0-based 9) at round 1 picks again immediately (gap 0).
    ctx = _operator_ctx(board, slot=9, overall=10)
    assert upcoming_opponent_picks(ctx) == []
    res = rollout_survival(ctx, [board[0], board[5]], rng=random.Random(3), rollouts=64)
    assert res.picks_until_next == 0
    assert res.survival[board[0].player_id] == 1.0
    assert res.survival[board[5].player_id] == 1.0
    # next-best VOR at RB equals the current best available RB's VOR (by VOR)
    best_rb_vor = max(e.vor for e in board if e.position == "RB")
    assert res.next_best_vor["RB"] == pytest.approx(best_rb_vor)


def test_rollout_is_deterministic_for_a_fixed_seed(make_draft_board):
    """Same seed -> identical S_next and next-best VOR (design D2 determinism)."""
    board = make_draft_board()
    ctx = _operator_ctx(board, slot=4, overall=5)
    cands = list(board[:8])
    a = rollout_survival(ctx, cands, rng=random.Random(777), rollouts=128)
    b = rollout_survival(ctx, cands, rng=random.Random(777), rollouts=128)
    assert dict(a.survival) == dict(b.survival)
    assert dict(a.next_best_vor) == dict(b.next_best_vor)


def test_rollout_opponent_need_lowers_that_positions_survival(make_draft_board):
    """The new ``opponent_rosters`` field bites: when the rollout's rivals need ONLY
    RB (every other slot pre-filled with deep players), they hammer RB, so a
    contested RB's survival is LOWER than in the identical need-blind rollout."""
    board = make_draft_board()
    bp = _by_pos(board)
    # Each rival holds deep, non-RB players so RB is its sole remaining need; the
    # held players sit deep, leaving the top RBs (the contested candidate) available.
    fillers = {}
    qi, wi, ti, di, ki = 15, 45, 15, 0, 0
    for t in range(1, 10):
        fillers[t] = (bp["QB"][qi], bp["WR"][wi], bp["WR"][wi + 1], bp["WR"][wi + 2],
                      bp["TE"][ti], bp["DST"][di], bp["K"][ki])
        qi, wi, ti, di, ki = qi + 1, wi + 3, ti + 1, di + 1, ki + 1
    taken_ids = [e.player_id for grp in fillers.values() for e in grp]

    def ctx(populate):
        st = BoardState(board)
        for pid in taken_ids:
            st.take(pid)
        opp = {t: v for t, v in fillers.items()} if populate else {}
        return PickContext(team_slot=0, round=1, overall_pick=1, rounds_total=16,
                           roster=ROSTER, own_roster=(), state=st,
                           rng=random.Random(0), opponent_rosters=opp)

    cand = bp["RB"][6]  # a contested RB (rank ~20), inside the room's reach window
    need = rollout_survival(ctx(True), [cand], rng=random.Random(5), rollouts=600)
    blind = rollout_survival(ctx(False), [cand], rng=random.Random(5), rollouts=600)
    s_need = need.survival[cand.player_id]
    s_blind = blind.survival[cand.player_id]
    assert s_need < s_blind - 0.05  # rivals needing RB take it sooner (clear gap)


def test_rollout_next_best_vor_is_reported_per_position(make_draft_board):
    """The single batch also yields E[best-available VOR] per position for VONA."""
    board = make_draft_board()
    ctx = _operator_ctx(board, slot=4, overall=5)
    res = rollout_survival(ctx, list(board[:5]), rng=random.Random(9), rollouts=128)
    for pos in ("QB", "RB", "WR", "TE", "DST", "K"):
        assert pos in res.next_best_vor
    # after ~10 intervening picks, best-available RB VOR should be <= today's best RB
    best_rb_now = max(e.vor for e in board if e.position == "RB")
    assert res.next_best_vor["RB"] <= best_rb_now + 1e-9


# --------------------------------------------------- the wait-gate


def test_wait_gate_threshold():
    assert wait_ok(0.85, tau_wait=0.8) is True
    assert wait_ok(0.75, tau_wait=0.8) is False
    assert wait_ok(0.8, tau_wait=0.8) is True  # inclusive at the threshold


# --------------------------------------------- live opponent recalibration


def _synthetic_pick_log(board, *, sigma, n_picks, seed):
    """A pick log whose reach = rank - overall ~ N(0, sigma): overall = rank - reach."""
    rng = random.Random(seed)
    skill = sorted((e for e in board if e.position in ("QB", "RB", "WR", "TE")),
                   key=lambda e: e.espn_overall_rank)[:n_picks]
    log = []
    for i, e in enumerate(skill):
        reach = rng.gauss(0.0, sigma)
        overall = max(1, int(round(e.espn_overall_rank - reach)))
        team = (i % 9) + 1  # rivals only (operator is slot 0)
        log.append((overall, team, e.player_id))
    return log


def test_recalibrate_recovers_generating_sigma(make_draft_board):
    """The online refit recovers the reach spread that generated a synthetic log."""
    board = make_draft_board()
    sigma_true = 12.0
    log = _synthetic_pick_log(board, sigma=sigma_true, n_picks=100, seed=99)
    rec = recalibrate_from_pick_log(log, board, min_room_picks=20)
    assert rec.engaged
    assert rec.n_room_picks == 100
    # population std of a 100-sample reach draw sits within sampling error of sigma
    assert 0.8 * sigma_true <= rec.reach_sigma <= 1.2 * sigma_true
    assert abs(rec.reach_center) < 3.0  # ~zero-centered, as generated
    # the returned priors carry the refit sigma; other room fields are untouched
    assert rec.priors.reach_sigma == pytest.approx(rec.reach_sigma)
    assert rec.priors.kdst_earliest_round == ROOM_PRIORS_2025.kdst_earliest_round


def test_recalibrate_is_deterministic(make_draft_board):
    board = make_draft_board()
    log = _synthetic_pick_log(board, sigma=15.0, n_picks=80, seed=3)
    a = recalibrate_from_pick_log(log, board)
    b = recalibrate_from_pick_log(log, board)
    assert a == b


def test_recalibrate_cold_start_below_threshold(make_draft_board):
    """Below the pick threshold the fit stays cold-start: base priors, not engaged."""
    board = make_draft_board()
    log = _synthetic_pick_log(board, sigma=12.0, n_picks=10, seed=1)
    rec = recalibrate_from_pick_log(log, board, min_room_picks=20)
    assert rec.engaged is False
    assert rec.priors is ROOM_PRIORS_2025
    assert rec.reach_sigma is None


def test_recalibrate_excludes_operator_and_kdst(make_draft_board):
    """Operator picks (not the room) and K/DST (structural deferral, not aggression)
    are excluded from the reach sample — mirroring calibration.py's conventions."""
    board = make_draft_board()
    bp = _by_pos(board)
    log = _synthetic_pick_log(board, sigma=10.0, n_picks=60, seed=7)
    room_only = recalibrate_from_pick_log(log, board, operator_slot=None)
    # inject operator picks (slot 0) and K/DST picks that must be ignored
    k, dst = bp["K"][0], bp["DST"][0]
    noisy = list(log) + [
        (1, 0, board[0].player_id),   # operator reach of a top player (huge outlier)
        (1, 0, board[1].player_id),
        (5, 3, k.player_id),          # K taken absurdly early (would blow up sigma)
        (6, 4, dst.player_id),
    ]
    filtered = recalibrate_from_pick_log(noisy, board, operator_slot=0)
    # the excluded rows leave the room-only reach sample (hence sigma) unchanged
    assert filtered.n_room_picks == room_only.n_room_picks
    assert filtered.reach_sigma == pytest.approx(room_only.reach_sigma)


def test_recalibrate_degenerate_spread_reports_not_engaged():
    # Every room pick exactly at its board rank -> reach identically 0, sigma 0.
    # The refit must NOT claim engaged=True while silently keeping the base
    # priors' sigma (audit finding: misleading "recalibrated to 0.0" render).
    from ziggurat.draft.bots import BoardEntry
    from ziggurat.draft.priors import ROOM_PRIORS_2025

    board = [
        BoardEntry(f"p{i}", f"p{i}", "RB" if i % 2 else "WR", i + 1, 100.0, 10.0)
        for i in range(40)
    ]
    log = [(i + 1, 1 + (i % 9), f"p{i}") for i in range(30)]  # reach == 0 for all
    rec = recalibrate_from_pick_log(log, board, base_priors=ROOM_PRIORS_2025)
    assert rec.engaged is False
    assert rec.reach_sigma is None
    assert rec.priors.reach_sigma == ROOM_PRIORS_2025.reach_sigma
