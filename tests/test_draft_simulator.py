"""Simulator tests: snake order, determinism, legality at scale, summaries,
and the DB-edge board loader (item 2.2). Synthetic boards only (Rule 5)."""

import json
import random
import time
from collections import Counter
from pathlib import Path

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER as ROSTER
from ziggurat.data.nfl import projections
from ziggurat.data.nfl.espn_ranks import get_espn_draft_ranks, ingest_espn_ranks
from ziggurat.data.store import apply_schema, connect
from ziggurat.draft.bots import (
    FollowEspnRank,
    FollowVor,
    PickContext,
    RankNoiseBot,
    min_to_complete,
    position_counts,
)
from ziggurat.draft.priors import ROOM_PRIORS_2025
from ziggurat.draft.simulator import (
    ROUNDS,
    format_strategy_summary,
    load_board,
    run_draft,
    run_many,
    snake_sequence,
)

_VAL_FIXTURE = Path(__file__).parent / "fixtures" / "nfl" / "valuation_projections_sample.json"
_ESPN_FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "player_universe.json"


def _roster_is_legal(entries):
    """A finished roster covers all nine starters (min_to_complete == 0)."""
    return min_to_complete(position_counts(entries), ROSTER) == 0


# --------------------------------------------------------------- snake order


def test_snake_sequence_hand_computed():
    # 3 teams, 3 rounds: forward, reversed, forward.
    assert snake_sequence([0, 1, 2], 3) == [0, 1, 2, 2, 1, 0, 0, 1, 2]


def test_snake_sequence_honors_a_custom_pick_order():
    assert snake_sequence([2, 0, 1], 2) == [2, 0, 1, 1, 0, 2]


def test_run_draft_rejects_bad_pick_order(make_draft_board):
    board = make_draft_board()
    pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
    with pytest.raises(ValueError):
        run_draft(board, pickers, rng=random.Random(0), pick_order=[0, 0, 1, 2, 3, 4, 5, 6, 7, 8])


# ---------------------------------------------------------------- determinism


def test_run_draft_is_deterministic(make_draft_board):
    board = make_draft_board()

    def one():
        pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
        return run_draft(board, pickers, rng=random.Random(99)).pick_log

    assert one() == one()


def test_run_many_same_seed_same_summary(make_draft_board):
    board = make_draft_board()
    a = run_many(board, n=25, operator_slot=2, strategy=FollowVor(), seed=7)
    b = run_many(board, n=25, operator_slot=2, strategy=FollowVor(), seed=7)
    assert a == b


def test_run_many_autodraft_count_is_reproducible(make_draft_board):
    board = make_draft_board()
    a = run_many(board, n=20, operator_slot=0, strategy=FollowEspnRank(),
                 seed=3, autodraft_count=3)
    b = run_many(board, n=20, operator_slot=0, strategy=FollowEspnRank(),
                 seed=3, autodraft_count=3)
    assert a == b


# --------------------------------------------------------- legality at scale


def test_every_finished_roster_is_legal(make_draft_board):
    board = make_draft_board()
    pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
    result = run_draft(board, pickers, rng=random.Random(1))
    for team, entries in result.rosters.items():
        assert len(entries) == ROUNDS
        assert _roster_is_legal(entries), f"team {team} illegal: {position_counts(entries)}"


def test_kdst_scarce_board_distributes_one_each(make_draft_board):
    # Exactly 10 DST and 10 K for 10 teams: legality + the DST/K cap of 1 must
    # hand every team exactly one of each, with none starved.
    board = make_draft_board(dst=10, k=10)
    pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
    result = run_draft(board, pickers, rng=random.Random(2))
    for entries in result.rosters.values():
        c = Counter(e.position for e in entries)
        assert c["DST"] == 1 and c["K"] == 1
        assert _roster_is_legal(entries)


def test_thousand_drafts_from_every_slot_legal_and_fast(make_draft_board):
    board = make_draft_board()
    start = time.perf_counter()
    for slot in range(ROSTER.teams):  # 1..10 (0-based here)
        summary = run_many(board, n=1000, operator_slot=slot, strategy=FollowVor(),
                           priors=ROOM_PRIORS_2025, seed=slot)
        # every operator roster fills a legal starting lineup: DST/K always 1.
        assert summary.position_counts_mean["DST"] == pytest.approx(1.0)
        assert summary.position_counts_mean["K"] == pytest.approx(1.0)
        assert summary.operator_slot == slot + 1
    elapsed = time.perf_counter() - start
    assert elapsed < 60.0, f"10x1000 drafts took {elapsed:.1f}s (budget 60s)"


# ------------------------------------------------------------------ summaries


def test_summary_distribution_is_ordered_and_shaped(make_draft_board):
    board = make_draft_board()
    s = run_many(board, n=200, operator_slot=4, strategy=FollowVor(), seed=1)
    assert s.n == 200
    assert s.points_min <= s.points_p10 <= s.points_p50 <= s.points_p90 <= s.points_max
    assert s.points_min <= s.points_mean <= s.points_max
    # operator drafts 16 players across the tracked positions.
    assert sum(s.position_counts_mean.values()) == pytest.approx(ROUNDS)
    text = format_strategy_summary(s)
    assert "starting-lineup points" in text and "median" in text
    assert "draft slot 5" in text


def test_strategies_diverge_but_both_stay_legal(make_draft_board):
    # The two operator baselines draft off different signals (VOR desc vs ESPN
    # rank asc), so they produce different rosters — but both must be legal (a
    # full starting lineup, exactly one DST and one K). Note: naive best-available
    # VOR intentionally over-drafts the deepest position (that roster-need
    # weighting is the 2.3 engine's job), so it need NOT beat ESPN-follow on raw
    # starting-lineup points — only differ and stay legal.
    board = make_draft_board()
    vor = run_many(board, n=120, operator_slot=3, strategy=FollowVor(), seed=11)
    espn = run_many(board, n=120, operator_slot=3, strategy=FollowEspnRank(), seed=11)
    assert vor.position_counts_mean["DST"] == pytest.approx(1.0)
    assert vor.position_counts_mean["K"] == pytest.approx(1.0)
    assert espn.position_counts_mean["DST"] == pytest.approx(1.0)
    assert espn.position_counts_mean["K"] == pytest.approx(1.0)
    # different signals -> different roster shape or point profile
    assert vor.position_counts_mean != espn.position_counts_mean


# -------------------------------------------- opponent_rosters population (2.3)


class _OppRosterProbe:
    """Records that ``run_draft`` populates ``opponent_rosters`` correctly on every
    pick: the own seat is excluded, all rivals are present, and the drafted-so-far
    invariant holds (own + all rivals == overall_pick - 1). Delegates the actual
    pick to FollowEspnRank so the draft still completes."""

    def __init__(self):
        self.checks = 0
        self._delegate = FollowEspnRank()

    def pick(self, ctx: PickContext) -> str:
        teams = ctx.roster.teams
        assert ctx.team_slot not in ctx.opponent_rosters, "own seat must be excluded"
        assert set(ctx.opponent_rosters) == set(range(teams)) - {ctx.team_slot}
        opp_total = sum(len(r) for r in ctx.opponent_rosters.values())
        assert opp_total + len(ctx.own_roster) == ctx.overall_pick - 1
        # read-only view (same convention as own_roster)
        with pytest.raises(TypeError):
            ctx.opponent_rosters[ctx.team_slot] = ()
        self.checks += 1
        return self._delegate.pick(ctx)


def test_run_draft_populates_opponent_rosters(make_draft_board):
    board = make_draft_board()
    probe = _OppRosterProbe()
    pickers = [probe] + [RankNoiseBot() for _ in range(ROSTER.teams - 1)]
    run_draft(board, pickers, rng=random.Random(5))
    assert probe.checks == ROUNDS  # the probe seat was on the clock every round


# --------------------------------------------------------------- DB edge loader


def _build_board_db(db_path):
    """Projections + players (so build_valuation works) + an ESPN board snapshot."""
    conn = connect(db_path)
    apply_schema(conn)
    for gsis, sleeper, espn_id, name in [
        ("00-QB", "100", "3918298", "Test QB"),   # Josh Allen espn id -> board rank 36
        ("00-R1", "201", "e201", "Test RB1"),
        ("00-WR", "301", "e301", "Test WR"),
    ]:
        conn.execute(
            "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, retrieved_as_of, knowable_as_of) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (gsis, sleeper, espn_id, name, "2026-07-01", "2026-07-01"),
        )
    conn.commit()
    projections.ingest_projections(conn, json.loads(_VAL_FIXTURE.read_text()),
                                   retrieved_as_of="2026-08-01")
    ingest_espn_ranks(conn, json.loads(_ESPN_FIXTURE.read_text()),
                      retrieved_as_of="2026-08-01", season=2026)
    return conn


def test_load_board_joins_espn_rank(tmp_path):
    conn = _build_board_db(tmp_path / "board.sqlite")
    board = load_board(conn, as_of="2026-08-01", season=2026)
    conn.close()
    assert board, "expected a non-empty board"
    by_name = {e.name: e for e in board}
    assert "Test QB" in by_name
    # Josh Allen's editorial PPR rank in the fixture is 36 -> joined onto the QB.
    assert by_name["Test QB"].espn_overall_rank == 36
    # every entry is a canonical league position with finite numbers.
    for e in board:
        assert e.position in ("QB", "RB", "WR", "TE", "DST", "K")
        assert e.espn_overall_rank >= 1


def test_load_board_requires_explicit_as_of(tmp_path):
    conn = _build_board_db(tmp_path / "board.sqlite")
    with pytest.raises(TypeError):
        load_board(conn, season=2026)  # no as_of -> library never assumes "now"
    conn.close()


def test_load_board_leakage_by_retrieval(tmp_path):
    """Rule 1 on the sim's one DB seam: nothing retrieved after ``as_of`` is
    visible, and a later correction never rewrites what an earlier as_of saw
    (mirrors test_espn_ranks.test_leakage_by_retrieval)."""
    conn = _build_board_db(tmp_path / "board.sqlite")  # everything retrieved 2026-08-01
    assert load_board(conn, as_of="2026-07-31", season=2026) == ()
    board = load_board(conn, as_of="2026-08-01", season=2026)
    assert board
    assert {e.name: e.espn_overall_rank for e in board}["Test QB"] == 36

    # A later re-pull corrects the QB's editorial rank; the old as_of still
    # serves the rank as it was known then.
    raw = json.loads(_ESPN_FIXTURE.read_text())
    for pl in raw:
        if pl["id"] == 3918298:
            pl["draftRanksByRankType"]["PPR"]["rank"] = 99
    ingest_espn_ranks(conn, raw, retrieved_as_of="2026-08-15", season=2026)
    old = {e.name: e.espn_overall_rank for e in load_board(conn, as_of="2026-08-01", season=2026)}
    new = {e.name: e.espn_overall_rank for e in load_board(conn, as_of="2026-08-15", season=2026)}
    conn.close()
    assert old["Test QB"] == 36
    assert new["Test QB"] == 99


# ----------------------------------------------------------- thin-board failure


def test_short_board_rejected_up_front(make_draft_board):
    pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
    with pytest.raises(ValueError, match="needs at least 160"):
        run_draft(make_draft_board(qb=10, rb=40, wr=40, te=10, dst=10, k=10)[:150],
                  pickers, rng=random.Random(0))
    with pytest.raises(ValueError, match="short on DST"):
        run_draft(make_draft_board(dst=5), pickers, rng=random.Random(0))


def test_thin_board_fails_loud_never_silently_illegal(make_draft_board):
    """The audit's repro: 160 players with >= 10 of each position, but flex/bench
    drain the 10-deep TE pool. The guarantee is not that every draft completes —
    it is that a stranded draft raises instead of silently scoring an illegal or
    cap-busting roster."""
    board = make_draft_board(qb=10, rb=55, wr=55, te=10, dst=10, k=20)
    for seed in range(8):
        pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
        try:
            result = run_draft(board, pickers, rng=random.Random(seed))
        except RuntimeError:
            continue  # loud failure is the acceptable outcome
        for entries in result.rosters.values():
            counts = position_counts(entries)
            assert min_to_complete(counts, ROSTER) == 0
            assert counts.get("K", 0) <= 1
            assert counts.get("DST", 0) <= 1


def test_load_board_unions_the_full_espn_universe(tmp_path):
    # Dress-rehearsal finding (2026-07-24): ESPN can draft players the
    # projections board has never heard of (deep rookie kickers), and an
    # off-board pick cannot be ENTERED — damming the sync feed. Every
    # ESPN-universe skill player must be on the board, zero-valued when the
    # house has no projection for him.
    conn = _build_board_db(tmp_path / "board.sqlite")
    board = load_board(conn, as_of="2026-08-01", season=2026)
    espn = get_espn_draft_ranks(conn, as_of="2026-08-01", season=2026)
    conn.close()

    ids = {e.player_id for e in board}
    for r in espn:
        if r["espn_id"] is not None:
            assert str(r["espn_id"]) in ids, f"{r['player']} missing from board"
    # union entries carry zero house value and never a fabricated projection
    extras = [e for e in board if e.house_points == 0.0 and e.vor == 0.0]
    assert extras, "expected at least one ESPN-only union entry in the fixture"
    for e in extras:
        assert e.position in ("QB", "RB", "WR", "TE", "DST", "K")
        assert e.espn_overall_rank >= 1
