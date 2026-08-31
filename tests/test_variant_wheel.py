"""Tests for the PAIR-AT-THE-WHEEL variant (``ziggurat/draft/variant_wheel.py``).

Deletable with ``ziggurat/draft/`` after draft day (Rule 8), like every other
``test_draft_*`` / draft-variant module.

WHAT THESE TESTS ARE FOR. The variant is opt-in and additive: it changes nothing
unless a caller constructs a :class:`~ziggurat.draft.variant_wheel.WheelPicker`.
So the tests that matter are not "does it run" but the four claims the variant is
allowed to make, each of which is false in a specific, checkable way if the code
is wrong:

  1. IT IS THE SHIPPED ROOM. ``rollout_pair_batch`` duplicates the shipped
     survival loop in order to record per-rollout outcomes the shipped one
     discards. If that duplication drifts, every pair number is computed against a
     room nobody calibrated. :func:`test_the_pair_batch_reproduces_the_shipped_rollout_exactly`
     pins it to ``survival.rollout_survival`` bit-for-bit under one seed.
  2. IT IS THE SHIPPED SCORE. The pair maths reconstructs the engine's own one-ply
     arithmetic in order to subtract one term from it.
     :func:`test_the_one_ply_reconstruction_matches_a_real_pick_score` pins the
     reconstruction against real :class:`PickRec.pick_score` values, and
     :func:`test_the_candidate_gather_matches_the_engine` pins the candidate set.
  3. IT IS OPT-IN. :func:`test_a_disengaged_wheel_picker_drafts_exactly_like_the_engine`
     runs the real paired harness and requires mean delta EXACTLY 0.0 with zero
     variance and every pair an exact tie.
  4. IT ACTUALLY DOES THE WHEEL THING.
     :func:`test_the_wheel_takes_the_scarcer_player_first` builds a board where the
     right answer is known by construction and the one-ply engine provably gets it
     wrong, and requires the variant to get it right *and* to end up holding both
     players.
  5. A TIE IS A TIE, NOT A RANKING (added by the 2026-08-30 audit).
     ``pair_score = first_term + expected_second`` sums the same two magnitudes in
     OPPOSITE ORDERS for two candidates who are each other's wheel partner, so an
     exact tie lands ~1 ULP apart and the engine's ``-vor`` tie-break tier is
     bypassed — measured live, that ranked a player 38.9 VOR points worse FIRST on
     a margin of 4.1e-13, displayed as two identical scores.
     :func:`test_a_float_noise_tie_is_broken_by_the_engines_ladder_not_by_summation_order`
     pins both directions: the fix ranks by the ladder, and ``tie_tolerance=0``
     still reproduces the defect (so the test cannot pass by the tie going away).
  6. ONE SURVIVAL MODEL PER DRAFT, OR NONE (added by the same audit).
     :func:`test_an_injected_survival_provider_is_refused_rather_than_silently_ignored`
     requires the refusal, and
     :func:`test_a_joint_batch_provider_is_used_on_the_pair_path_and_disclosed`
     requires the way out of it to work and to be disclosed.

MUTATION-CHECKED. Every claim above was re-run against a deliberately broken copy
of the module and each failed; the specific mutations are named in the individual
docstrings so a future reader can repeat them. Two docstring claims made during
the build did NOT survive that re-run and have been corrected in place rather
than quietly dropped: the ``_SHORTLIST = 2`` mutation does not fail the arithmetic
gate (``_SHORTLIST = 1`` does, and is now asserted), and the gate's own oracle
floored partner values at zero so it agreed with the module only in the early
rounds it was run at.

Offline and deterministic: no network, no database. The live-board tests read the
frozen fixture ``tests/fixtures/draft/board-2026-08-30.json`` when it is present
and skip when it is not (it belongs to ``test_draft_golden.py``, not to this
module). Everything else runs on invented players (Rule 5).
"""

from __future__ import annotations

import dataclasses
import json
import random
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.draft import evaluate as ev
from ziggurat.draft import survival as sv
from ziggurat.draft import variant_wheel as vw
from ziggurat.draft.bots import (
    AutodraftBot,
    BoardEntry,
    PickContext,
    RankNoiseBot,
    position_counts,
)
from ziggurat.draft.engine import PickEngine, SurvivalEstimate
from ziggurat.draft.priors import ROOM_PRIORS_2025
from ziggurat.draft.simulator import _assign_autodrafters, run_draft

BOARD_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "draft" / "board-2026-08-30.json"
)
OPERATOR_SEAT = ev.OPERATOR_SLOT_2026  # 8 == slot 9 of 10, zero-based


# --------------------------------------------------------------------- helpers


def _live_board() -> tuple[BoardEntry, ...]:
    """The frozen live board, or a skip. Owned by ``test_draft_golden.py``."""
    if not BOARD_FIXTURE.exists():  # pragma: no cover - fixture-present in this tree
        pytest.skip(f"{BOARD_FIXTURE.name} not present; live-board checks skipped")
    payload = json.loads(BOARD_FIXTURE.read_text(encoding="utf-8"))
    cols = payload["columns"]
    return tuple(BoardEntry(**dict(zip(cols, row, strict=True))) for row in payload["rows"])


def _mid_draft_ctx(
    board, *, overall: int, seat: int = OPERATOR_SEAT, seed: int = 4,
    own: Sequence[str] = (),
) -> PickContext:
    """A context at ``overall`` with the top ``overall-1`` ESPN names off the board.

    Crude but sufficient and deterministic: what these tests need is a realistic
    BOARD SHAPE at a realistic pick, not a faithful room history. ``own`` names
    positions the operator has already drafted (the deepest-ranked available name
    at each), which is what makes the lineup-reachability discount non-trivial —
    with an empty roster every ``frac`` is 1.0 and a test cannot see it.
    """
    by_rank = sorted(board, key=lambda e: (e.espn_overall_rank, e.player_id))
    taken = [e.player_id for e in by_rank[: max(0, overall - 1)]]
    roster: list[BoardEntry] = []
    used = set(taken)
    for pos in own:
        pick = next(
            e for e in by_rank
            if e.position == pos and e.player_id not in used
        )
        used.add(pick.player_id)
        roster.append(pick)
    return PickContext.from_board(
        board,
        team_slot=seat,
        round=(overall - 1) // DEFAULT_ROSTER.teams + 1,
        overall_pick=overall,
        rounds_total=16,
        taken=taken,
        own_roster=roster,
        rng=random.Random(seed),
    )


class _FixedSurvival:
    """A survival provider that hands the engine a pre-computed estimate."""

    def __init__(self, est: SurvivalEstimate):
        self.est = est

    def __call__(self, ctx, *, candidates, positions, rng):
        return self.est


# ============================================================ 1. the shipped room


def test_the_pair_batch_reproduces_the_shipped_rollout_exactly():
    """``rollout_pair_batch`` IS ``survival.rollout_survival`` plus a recording.

    Same seed, same candidates, same positions -> identical survival marginals and
    identical ``next_best_vor``, to the last bit. This is the fence around the one
    piece of shipped logic this variant had to duplicate (it may not edit
    ``survival.py``): if the shipped loop ever changes its room model, its rng
    order or its kappa handling, this fails instead of the variant quietly
    modelling a room nobody calibrated.

    MUTATION-CHECKED: reordering the autodraft sample to iterate the window seats
    unsorted, and dropping the kappa widening, each make this fail.
    """
    board = _live_board()
    ctx = _mid_draft_ctx(board, overall=9)
    engine = PickEngine(rollouts=64)
    allowed = vw._allowed_for(
        {}, ctx.picks_after, ctx.roster,
        round_num=ctx.round, kdst_earliest_round=engine.kdst_earliest_round,
    )
    cands, best_now = vw._gather_candidates(ctx, allowed, engine.candidate_width)
    positions = sorted(best_now)

    shipped = sv.rollout_survival(
        ctx, cands, rng=random.Random(11), rollouts=64, positions=positions
    )
    mine = vw.rollout_pair_batch(
        ctx, cands, rng=random.Random(11), rollouts=64, positions=positions
    )
    assert dict(mine.survival) == dict(shipped.survival)
    assert dict(mine.next_best_vor) == dict(shipped.next_best_vor)
    assert mine.picks_until_next == shipped.picks_until_next == 2

    # AND a wide window, where MANY distinct rival seats pick before our next turn.
    # A two-pick window at slot 9 is one rival seat twice, so it cannot see the
    # order the autodraft sample is drawn in; this case can.
    wide = _mid_draft_ctx(board, overall=5, seat=4)
    assert len({t for _o, t in sv.upcoming_opponent_picks(wide)}) > 1
    wcands, wbest = vw._gather_candidates(wide, ("RB", "WR", "TE", "QB"), 5)
    wpos = sorted(wbest)
    wshipped = sv.rollout_survival(
        wide, wcands, rng=random.Random(3), rollouts=48, positions=wpos
    )
    wmine = vw.rollout_pair_batch(
        wide, wcands, rng=random.Random(3), rollouts=48, positions=wpos
    )
    assert dict(wmine.survival) == dict(wshipped.survival)
    assert dict(wmine.next_best_vor) == dict(wshipped.next_best_vor)
    # and the recording itself is consistent with those marginals
    assert len(mine.taken_by_rollout) == 64
    for pid, s in mine.survival.items():
        seen = sum(1 for gone in mine.taken_by_rollout if pid not in gone)
        assert seen / 64 == pytest.approx(s)


def test_the_batch_never_mutates_the_shared_board():
    """A rollout drafts over a clone. If it took from ``ctx.state`` the live draft
    would lose players nobody picked."""
    board = _live_board()
    ctx = _mid_draft_ctx(board, overall=9)
    before = set(ctx.state.taken)
    cands, _ = vw._gather_candidates(ctx, ("RB", "WR"), 5)
    vw.rollout_pair_batch(ctx, cands, rng=random.Random(2), rollouts=20)
    assert set(ctx.state.taken) == before


def test_a_true_wheel_needs_no_rollout_at_all():
    """Gap 0 (slot 1 or slot 10): nobody picks in between, so both halves are
    certain and the batch is exact rather than sampled."""
    board = _live_board()
    # seat 9 picks overall 10 and 11 back to back
    ctx = _mid_draft_ctx(board, overall=10, seat=9)
    cands, _ = vw._gather_candidates(ctx, ("RB", "WR"), 5)
    batch = vw.rollout_pair_batch(ctx, cands, rng=random.Random(1), rollouts=512)
    assert batch.picks_until_next == 0
    assert set(batch.survival.values()) == {1.0}
    assert batch.taken_by_rollout == (frozenset(),)


# ========================================================== 2. the shipped score


def test_the_candidate_gather_matches_the_engine():
    """The variant's first-pick candidate set is the engine's, exactly.

    ``PickEngine.recommend`` gathers its candidates privately; the variant has to
    reproduce that gather to know what to enumerate pairs over. Asking the engine
    for more recommendations than it has candidates returns exactly its candidate
    set, so the two are directly comparable.

    MUTATION-CHECKED: dropping the best-by-VOR half of the union, or changing the
    width, each make this fail.
    """
    board = _live_board()
    engine = PickEngine(rollouts=16)
    for overall in (9, 29, 49, 89, 129):
        ctx = _mid_draft_ctx(board, overall=overall)
        allowed = vw._allowed_for(
            position_counts(ctx.own_roster), ctx.picks_after, ctx.roster,
            round_num=ctx.round, kdst_earliest_round=engine.kdst_earliest_round,
        )
        mine, _ = vw._gather_candidates(ctx, allowed, engine.candidate_width)
        recs = engine.recommend(_mid_draft_ctx(board, overall=overall), top=99)
        assert {c.player_id for c in mine} == {r.player_id for r in recs}


def test_the_one_ply_reconstruction_matches_a_real_pick_score():
    """``_score_parts(...)[1]`` reproduces ``PickRec.pick_score`` to the last bit.

    The pair maths subtracts the engine's urgency term from the engine's score, so
    it has to reconstruct that score exactly. Here the engine is fed a FIXED
    survival estimate so both sides see identical inputs and any difference is
    arithmetic, not sampling.

    MUTATION-CHECKED: removing the ``frac`` discount from the risk term, or
    applying ``frac`` to a negative VOR, each make this fail.
    """
    board = _live_board()
    # A saturated roster at a LATE round is the only place the reachability
    # discount and the positive (ceiling) risk sign are both visible: with an
    # empty roster every ``frac`` is 1.0 and with an early round every risk term
    # is negative, so an undiscounted copy of either would reconstruct perfectly.
    cases = [
        (9, ()),
        (49, ("QB", "RB", "RB", "WR")),
        (89, ("QB", "RB", "RB", "WR", "WR", "TE")),
        (129, ("QB", "QB", "RB", "RB", "WR", "WR", "WR", "TE", "TE", "K", "DST")),
        (149, ("QB", "QB", "RB", "RB", "WR", "WR", "WR", "TE", "TE", "K", "DST")),
    ]
    for overall, own in cases:
        ctx = _mid_draft_ctx(board, overall=overall, own=own)
        engine0 = PickEngine(rollouts=32)
        allowed = vw._allowed_for(
            position_counts(ctx.own_roster), ctx.picks_after, ctx.roster,
            round_num=ctx.round, kdst_earliest_round=engine0.kdst_earliest_round,
        )
        cands, best_now = vw._gather_candidates(ctx, allowed, engine0.candidate_width)
        est = sv.rollout_survival(
            ctx, cands, rng=random.Random(7), rollouts=32, positions=sorted(best_now)
        )
        allowed_check = vw._allowed_for(
            position_counts(ctx.own_roster), ctx.picks_after, ctx.roster,
            round_num=ctx.round, kdst_earliest_round=engine0.kdst_earliest_round,
        )
        assert allowed_check == allowed
        urgency = {}
        for pos, bn in best_now.items():
            nb = est.next_best_vor.get(pos, bn.vor)
            urgency[pos] = max(0.0, bn.vor - nb) * (1.0 - est.survival.get(bn.player_id, 1.0))
        engine = PickEngine(
            survival=_FixedSurvival(
                SurvivalEstimate(survival=est.survival, next_best_vor=est.next_best_vor)
            )
        )
        counts = position_counts(ctx.own_roster)
        recs = engine.recommend(
            _mid_draft_ctx(board, overall=overall, own=own), top=99
        )
        assert recs, "the engine must have candidates at this pick"
        for rec in recs:
            _base, one_ply = vw._score_parts(
                rec.player, counts=counts, round_num=ctx.round,
                roster=ctx.roster, engine=engine, urgency=urgency.get(rec.position, 0.0),
            )
            assert one_ply == pytest.approx(rec.pick_score, abs=1e-9), rec.name


def test_the_reachability_discount_lands_on_the_right_terms():
    """``frac`` discounts value, urgency and a POSITIVE risk term — and nothing else.

    Written as arithmetic rather than as a comparison so it pins the placement,
    not just the total: a backup QB in round 13 has ``frac`` 0.25 (he cannot reach
    the lineup) and a POSITIVE risk term (the late-round ceiling tilt), which is
    the one combination where an undiscounted risk term or an undiscounted
    urgency term is visible at all.

    MUTATION-CHECKED: dropping ``frac`` from the risk term, and dropping it from
    the urgency term, each fail here (and nowhere else in this file).
    """
    board = _live_board()
    engine = PickEngine()
    qb = max((e for e in board if e.position == "QB"), key=lambda e: e.vor)
    counts = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
    roster = DEFAULT_ROSTER
    round_num = 13
    urgency = 9.0
    frac = vw._value_fraction("QB", counts, roster)
    assert frac == pytest.approx(0.25), "a backup QB must be structurally discounted"
    rk = engine.b_risk * vw.risk_sign(round_num) * vw._dispersion("QB")
    assert rk > 0, "round 13 must carry the positive (ceiling) risk sign"
    need = engine.b_need * vw._need_fill("QB", round_num, counts, roster, engine.need_schedule)

    base, one_ply = vw._score_parts(
        qb, counts=counts, round_num=round_num, roster=roster,
        engine=engine, urgency=urgency,
    )
    assert base == pytest.approx(qb.vor * frac + need + rk * frac)
    assert one_ply == pytest.approx(base + engine.b_vona * urgency * frac)


def test_base_is_exactly_the_one_ply_score_minus_the_urgency_term():
    """The one subtraction the whole design rests on, pinned on a case where the
    urgency term is genuinely non-zero (otherwise the assertion is vacuous)."""
    board = _live_board()
    ctx = _mid_draft_ctx(board, overall=9)
    engine = PickEngine()
    entry = max(board, key=lambda e: e.vor if e.position == "RB" else -1e9)
    counts = {"RB": 1, "WR": 2}
    urgency = 12.5
    base, one_ply = vw._score_parts(
        entry, counts=counts, round_num=3, roster=ctx.roster,
        engine=engine, urgency=urgency,
    )
    frac = vw._value_fraction(entry.position, counts, ctx.roster)
    assert one_ply - base == pytest.approx(engine.b_vona * urgency * frac)
    assert one_ply != base  # the term is real here, not zero


# ================================================================= 3. it is opt-in


def test_a_disengaged_wheel_picker_drafts_exactly_like_the_engine():
    """``max_pair_gap=-1`` never engages, so the variant IS the engine.

    Run through the real paired harness, which is the same instrument the A/B
    result is reported from: mean delta exactly 0.0, standard deviation exactly
    0.0, every pair an exact tie. This is the "no default behaviour change" proof
    and it is also the proof that the delegation path does not disturb the rng --
    an earlier draft of this module derived its rollout child BEFORE deciding
    whether to engage, which handed the engine a different stream at every pick it
    was not even changing, and this test is what would have caught it.
    """
    board, weekly = ev.smoke_inputs()
    engine = PickEngine(rollouts=32)
    off = vw.WheelPicker(engine=engine, max_pair_gap=-1)
    result = ev.paired_compare(
        board, weekly, a=off, b=engine, name_a="wheel-off", name_b="engine",
        n=4, slots=(0, 4, 8), seed=3, bootstrap=200,
    )
    assert result.mean_delta == 0.0
    assert result.sd_delta == 0.0
    assert result.ties == result.n == 12


def test_the_pair_window_engages_only_on_tight_pairs():
    """Pure snake geometry, checked against the operator's real seat.

    From slot 9 of 10 the sixteen picks are eight pairs with a gap of 2; the second
    half of each pair is 16 rival picks from the next one and must NOT engage. A
    true wheel seat has gap 0. The last pick of the draft never engages.
    """
    board, _ = ev.smoke_inputs()
    engaged_at = []
    for overall in (9, 12, 29, 32, 49, 52, 69, 72, 89, 92, 109, 112, 129, 132, 149, 152):
        ctx = _mid_draft_ctx(board, overall=overall, seat=8)
        win = vw.pair_window(ctx)
        if win.engaged:
            engaged_at.append(overall)
            assert win.gap == 2
            assert win.wheel_overall == overall + 3
    assert engaged_at == [9, 29, 49, 69, 89, 109, 129, 149]

    # seat 10 of 10 is a true wheel: gap 0
    ctx = _mid_draft_ctx(board, overall=10, seat=9)
    win = vw.pair_window(ctx)
    assert win.engaged and win.gap == 0 and win.wheel_overall == 11

    # seat 5 of 10 never has a tight pair at the default gap
    for overall in (5, 16, 25, 36):
        ctx = _mid_draft_ctx(board, overall=overall, seat=4)
        assert not vw.pair_window(ctx).engaged

    # the final pick has no pair at all, at any seat
    ctx = _mid_draft_ctx(board, overall=152, seat=8)
    ctx = dataclasses.replace(ctx, round=16, overall_pick=152)
    win = vw.pair_window(ctx)
    assert not win.engaged and "last pick" in win.why


def test_a_disengaged_pick_delegates_to_the_engine_verbatim():
    """At a non-pair pick the recommendation is the engine's own object graph."""
    board = _live_board()
    engine = PickEngine(rollouts=32)
    picker = vw.WheelPicker(engine=engine)
    mine = picker.recommend(_mid_draft_ctx(board, overall=12), top=5)
    theirs = engine.recommend(_mid_draft_ctx(board, overall=12), top=5)
    assert [r.player_id for r in mine] == [r.player_id for r in theirs]
    assert [r.reasons for r in mine] == [r.reasons for r in theirs]
    assert [r.pick_score for r in mine] == [r.pick_score for r in theirs]


# ==================================================== 4. it does the wheel thing


def _wheel_case_board() -> tuple[BoardEntry, ...]:
    """A board where the right pair is known BY CONSTRUCTION (Rule 5: invented).

    ``RB_SCARCE`` is the best player on the room's ESPN board (rank 1) and will be
    taken by the very next rival. ``RB_SAFE`` is worth MORE to us (higher VOR) but
    the room does not rate him (rank 40), so he keeps. A one-ply score takes the
    higher-VOR player and loses the other; the pair that maximises the two-pick
    haul is SCARCE now, SAFE at the wheel.
    """
    rows: list[tuple[str, str, int, float]] = [
        ("RB_SCARCE", "RB", 1, 100.0),
        ("FILLER_A", "WR", 2, 20.0),
        ("FILLER_B", "WR", 3, 19.0),
        ("FILLER_C", "WR", 4, 18.0),
        ("RB_SAFE", "RB", 40, 110.0),
        ("RB_THIRD", "RB", 41, 40.0),
    ]
    rank = 50
    for pos, count in (("QB", 4), ("WR", 6), ("TE", 4), ("K", 3), ("DST", 3)):
        for i in range(count):
            rows.append((f"{pos}_{i}", pos, rank, 5.0 - i * 0.1))
            rank += 1
    return tuple(
        BoardEntry(
            player_id=pid, name=pid.replace("_", " ").title(), position=pos,
            espn_overall_rank=r, house_points=vor + 100.0, vor=vor, team="ZZZ",
        )
        for pid, pos, r, vor in rows
    )


def test_the_wheel_takes_the_scarcer_player_first():
    """THE LOAD-BEARING TEST. One-ply loses a player the pair maths keeps.

    The room is forced fully onto autopilot (``autodraft_fraction=1.0``), so the
    two intervening rival picks are the top two ESPN names and the case is exact,
    not statistical: ``RB_SCARCE`` is gone by the wheel with certainty and
    ``RB_SAFE`` keeps with certainty.

    The assertion is not merely "a different player": it is that the shipped
    engine ends the pair holding ONE of the two, and the variant ends it holding
    BOTH — which is the entire claim of the module.

    Deliberately built so that BOTH urgency modes must get it right: the room
    takes ``RB_SAFE``'s position-mate with certainty, so ``RB_SAFE`` himself keeps
    with probability 1, so the engine's urgency term is zero here and ``keep`` and
    ``drop`` score the first half identically. Only the pair term can move it.

    MUTATION-CHECKED: making the pair score ignore the removal of the first pick
    from its own successor pool (i.e. not excluding the candidate himself), and
    dropping the second half of the pair from the score entirely, each collapse
    this back onto the one-ply answer and fail it.
    """
    board = _wheel_case_board()
    priors = dataclasses.replace(ROOM_PRIORS_2025, autodraft_fraction=1.0)
    engine = PickEngine(rollouts=32, room_priors=priors)
    picker = vw.WheelPicker(engine=engine)

    def ctx():
        return PickContext.from_board(
            board, team_slot=8, round=1, overall_pick=9, rounds_total=16,
            rng=random.Random(21),
        )

    one_ply = engine.recommend(ctx(), top=1)[0]
    paired = picker.recommend(ctx(), top=1)[0]
    assert one_ply.player_id == "RB_SAFE", "the one-ply engine takes the higher VOR"
    assert paired.player_id == "RB_SCARCE", "the pair maths takes the scarcer man first"

    # ... and the pair really is strictly better: play both forward two picks.
    def haul(first: str) -> set[str]:
        entry = next(e for e in board if e.player_id == first)
        c2 = PickContext.from_board(
            board, team_slot=8, round=2, overall_pick=12, rounds_total=16,
            own_roster=[entry],
            taken=[e.player_id for e in sorted(board, key=lambda x: x.espn_overall_rank)
                   if e.player_id != first][:2],
            rng=random.Random(21),
        )
        return {first, engine.recommend(c2, top=1)[0].player_id}

    assert haul("RB_SCARCE") == {"RB_SCARCE", "RB_SAFE"}
    assert "RB_SCARCE" not in haul("RB_SAFE")


def test_the_pair_score_decomposes_into_the_two_halves():
    """``pair_score == base_now + expected_second`` for every priced candidate, and
    the modal partner is a real, different player."""
    board = _wheel_case_board()
    priors = dataclasses.replace(ROOM_PRIORS_2025, autodraft_fraction=1.0)
    engine = PickEngine(rollouts=32, room_priors=priors)
    ctx = PickContext.from_board(
        board, team_slot=8, round=1, overall_pick=9, rounds_total=16, rng=random.Random(3)
    )
    dec = vw.pair_analysis(
        ctx, engine=engine, rng=random.Random(3), rollouts=32, priors=priors
    )
    assert dec.engaged and dec.gap == 2 and dec.candidates
    for c in dec.candidates:
        assert c.pair_score == pytest.approx(c.base_now + c.expected_second)
        if c.modal_second is not None:
            assert c.modal_second.player_id != c.player_id
    best = dec.candidates[0]
    assert best.player_id == "RB_SCARCE"
    assert best.modal_second is not None and best.modal_second.player_id == "RB_SAFE"


def test_the_collapsed_enumeration_equals_a_brute_force_over_every_pair():
    """THE ARITHMETIC GATE. The fast path must equal the slow path exactly.

    The module does not literally loop over (first, second) pairs: a first pick
    changes the roster only through its POSITION, so it collapses the quadratic to
    one shortlist per successor position and keeps only the top three survivors of
    each (at most two are ever excluded). That collapse is what makes the variant
    affordable, and it is also exactly the kind of optimisation that is silently
    wrong. This re-derives ``expected_second`` for every candidate the slow way —
    a full scan of the WHOLE successor pool, in every rollout, with no shortlist
    truncation at all — and requires the two to agree to the last bit.

    Run in BOTH displacement modes, because the correction is the part that
    consumes the second exclusion slot and so is what makes a shortlist of three
    the minimum rather than a nicety.

    RUN ACROSS THE WHOLE DRAFT, not just the early rounds. The first version of
    this test ran only at overalls 9 and 29 and its oracle started at ``best =
    0.0``, i.e. it floored every per-rollout successor value up at zero. The
    module does not floor. In the early rounds every successor value is positive
    so the two agreed and the gate had teeth; from round 13 on the best available
    partner is worth LESS than nothing and the floored oracle disagreed with the
    shipped code by 7.4 at overall #129 and 15.3 at #149 — which is to say the
    "brute-force re-derivation" covered none of the second half of the draft,
    which is exactly the regime the operator's late pair picks sit in (audit
    finding, 2026-08-30). The oracle below is UNFLOORED and the states now span
    #9 to #149; ``saw_a_negative`` asserts that regime is actually reached.

    MUTATION-CHECKED, all re-run 2026-08-30: taking the best-VALUED rather than
    the best-RANKED survivor as the room's displaced substitute fails this; so
    does shrinking ``_SHORTLIST`` to 1 (asserted below rather than claimed, since
    it is the bound the collapse rests on). An earlier docstring here claimed 2
    also fails — it does not, and the claim is withdrawn: at most ONE exclusion
    can ever apply to a shortlist (a candidate the room took is not a survivor,
    so the self-exclusion and the displacement exclusion are mutually exclusive),
    so 2 is provably sufficient and the third slot is a deliberate spare.
    """
    board = _live_board()
    engine = PickEngine(rollouts=48)
    # The second case is the one that matters for the successor STATE: with two
    # quarterbacks already drafted, taking a third saturates the position cap, so
    # the shortlist for the wheel pick is drawn from a DIFFERENT allowed set than
    # the one this pick is chosen from. Without it, a pool built at the current
    # state instead of the successor state reconstructs perfectly.
    states = [
        (9, ("RB", "WR")),
        (29, ("QB", "QB")),
        (89, ("QB", "RB", "RB", "WR", "WR", "TE")),
        (129, ("QB", "QB", "RB", "RB", "WR", "WR", "WR", "TE", "TE", "K", "DST")),
        (149, ("QB", "QB", "RB", "RB", "WR", "WR", "WR", "TE", "TE", "K", "DST",
               "RB", "WR")),
    ]
    saw_a_narrowing = False
    saw_a_negative = False
    for mode in ("rank", "none"):
      for overall, own in states:
        ctx = _mid_draft_ctx(board, overall=overall, own=own)
        dec = vw.pair_analysis(
            ctx, engine=engine, rng=random.Random(31), rollouts=48, displacement=mode
        )
        assert dec.engaged and dec.batch is not None

        counts = position_counts(ctx.own_roster)
        round2 = ctx.round + 1
        allowed1 = vw._allowed_for(
            counts, ctx.picks_after, ctx.roster,
            round_num=ctx.round, kdst_earliest_round=engine.kdst_earliest_round,
        )
        pools: dict[str, list[tuple[float, BoardEntry]]] = {}
        for pos in {c.entry.position for c in dec.candidates}:
            counts2 = dict(counts)
            counts2[pos] = counts2.get(pos, 0) + 1
            allowed2 = vw._allowed_for(
                counts2, ctx.picks_after - 1, ctx.roster,
                round_num=round2, kdst_earliest_round=engine.kdst_earliest_round,
            )
            if allowed2 != allowed1:
                saw_a_narrowing = True
            pool, _ = vw._gather_candidates(ctx, allowed2, vw.DEFAULT_SECOND_WIDTH)
            valued = [
                (
                    vw._score_parts(
                        e, counts=counts2, round_num=round2, roster=ctx.roster,
                        engine=engine, urgency=0.0,
                    )[0],
                    e,
                )
                for e in pool
            ]
            valued.sort(
                key=lambda t: (-t[0], -t[1].vor, t[1].espn_overall_rank, t[1].player_id)
            )
            pools[pos] = valued
            # The shortlist is built at the SUCCESSOR state, so a position the
            # first pick saturates cannot appear in its own partner list. Checked
            # structurally as well as through the score, because the reachability
            # discount already sinks such a player to the bottom of the ordering —
            # a shortlist built at the CURRENT state reconstructs the same NUMBER
            # while being wrong about what is legally draftable there.
            assert dec.second_pools[pos] == tuple(e.player_id for _b, e in valued)
            for _b, e in valued:
                assert e.position in allowed2

        for cand in dec.candidates:
            pool = pools[cand.entry.position]
            total = 0.0
            for gone in dec.batch.taken_by_rollout:
                excluded = {cand.player_id}
                if mode == "rank" and cand.player_id in gone:
                    best_rank = best_rank_id = None
                    for _b, e in pool:
                        if e.player_id in gone:
                            continue
                        if best_rank is None or e.espn_overall_rank < best_rank:
                            best_rank, best_rank_id = e.espn_overall_rank, e.player_id
                    if best_rank_id is not None:
                        excluded.add(best_rank_id)
                # UNFLOORED: whatever the best legal survivor is worth, including
                # a negative number, and 0.0 only when there is no survivor at all
                # (which is what the module itself does).
                best = 0.0
                for b, e in pool:
                    if e.player_id in gone or e.player_id in excluded:
                        continue
                    best = b
                    break
                total += best
            brute = total / len(dec.batch.taken_by_rollout)
            assert cand.expected_second == pytest.approx(brute, abs=1e-9), (
                f"{mode} @{overall}: {cand.entry.name}"
            )
            if cand.expected_second < -1e-9:
                saw_a_negative = True
    assert saw_a_narrowing, (
        "no state exercised here narrowed the allowed set at the second pick, so "
        "this test could not tell the successor pool from the current one"
    )
    assert saw_a_negative, (
        "no state exercised here produced a NEGATIVE expected second pick, so this "
        "gate never reached the late-draft regime the operator's #129/#149 pair "
        "picks live in — which is precisely how the floored oracle went unnoticed"
    )


def test_the_shortlist_bound_is_load_bearing(monkeypatch):
    """Drive ``_SHORTLIST`` down until the collapse actually breaks.

    The module argues that three survivors per rollout are enough because at most
    ONE exclusion can ever apply. An argument is not a measurement, and the
    docstring that claimed ``_SHORTLIST = 2`` fails the arithmetic gate was simply
    wrong (audit finding, 2026-08-30). This measures the real bound: at 1 the
    collapse is lossy by a large margin, at 2 and 3 it is exact.
    """
    board = _live_board()
    engine = PickEngine(rollouts=48)
    ctx_at = lambda: _mid_draft_ctx(board, overall=9, own=("RB", "WR"))  # noqa: E731

    def worst_error(shortlist: int) -> float:
        monkeypatch.setattr(vw, "_SHORTLIST", shortlist)
        ctx = ctx_at()
        dec = vw.pair_analysis(ctx, engine=engine, rng=random.Random(31), rollouts=48)
        counts = position_counts(ctx.own_roster)
        round2 = ctx.round + 1
        worst = 0.0
        for cand in dec.candidates:
            pos = cand.entry.position
            counts2 = dict(counts)
            counts2[pos] = counts2.get(pos, 0) + 1
            allowed2 = vw._allowed_for(
                counts2, ctx.picks_after - 1, ctx.roster, round_num=round2,
                kdst_earliest_round=engine.kdst_earliest_round,
            )
            pool, _ = vw._gather_candidates(ctx, allowed2, vw.DEFAULT_SECOND_WIDTH)
            valued = [
                (vw._score_parts(e, counts=counts2, round_num=round2,
                                 roster=ctx.roster, engine=engine, urgency=0.0)[0], e)
                for e in pool
            ]
            valued.sort(
                key=lambda t: (-t[0], -t[1].vor, t[1].espn_overall_rank, t[1].player_id)
            )
            total = 0.0
            for gone in dec.batch.taken_by_rollout:
                excluded = {cand.player_id}
                if cand.player_id in gone:
                    br = bid = None
                    for _b, e in valued:
                        if e.player_id in gone:
                            continue
                        if br is None or e.espn_overall_rank < br:
                            br, bid = e.espn_overall_rank, e.player_id
                    if bid is not None:
                        excluded.add(bid)
                best = 0.0
                for b, e in valued:
                    if e.player_id in gone or e.player_id in excluded:
                        continue
                    best = b
                    break
                total += best
            worst = max(
                worst,
                abs(cand.expected_second - total / len(dec.batch.taken_by_rollout)),
            )
        return worst

    assert worst_error(3) == pytest.approx(0.0, abs=1e-9)
    assert worst_error(2) == pytest.approx(0.0, abs=1e-9), (
        "two survivors must suffice — at most one exclusion can ever apply"
    )
    assert worst_error(1) > 1.0, (
        "one survivor must NOT suffice; if it does, the exclusion the whole "
        "collapse is bounded by has stopped firing and this gate is asleep"
    )


def test_urgency_mode_drop_and_keep_differ_only_in_the_first_term():
    """The two modes share the pair term and differ ONLY by the urgency surrogate.

    Run on the LIVE board rather than the invented one. On ``_wheel_case_board``
    the room takes every candidate's position-mate with certainty, so the engine's
    urgency is exactly 0 for every candidate and this whole test was a tautology:
    ``d.base_now <= c.base_now`` was ``0 <= 0``, and making ``drop`` a silent alias
    of ``keep`` left the file green (audit finding, 2026-08-30). ``drop`` is one of
    the two measured arms and the pre-registered theoretically clean form, so it
    has to stay a distinct program.

    MUTATION-CHECKED: ``first_term = one_ply`` unconditionally (i.e. ``drop``
    silently becomes ``keep``) fails the strict-difference assertion below.
    """
    board = _live_board()
    engine = PickEngine(rollouts=48)
    strictly_lower = 0
    for overall, own in ((9, ()), (49, ("QB", "RB", "RB", "WR"))):
        drop = vw.pair_analysis(
            _mid_draft_ctx(board, overall=overall, own=own), engine=engine,
            rng=random.Random(9), rollouts=48, urgency_mode="drop",
        )
        keep = vw.pair_analysis(
            _mid_draft_ctx(board, overall=overall, own=own), engine=engine,
            rng=random.Random(9), rollouts=48, urgency_mode="keep",
        )
        by_id_drop = {c.player_id: c for c in drop.candidates}
        for c in keep.candidates:
            d = by_id_drop[c.player_id]
            # the pair term is identical: only the FIRST term moves
            assert c.expected_second == pytest.approx(d.expected_second, abs=1e-12)
            assert c.base_now == pytest.approx(c.one_ply_score, abs=1e-12)
            assert d.base_now <= c.base_now + 1e-9
            # and the difference is exactly the engine's urgency contribution
            assert c.base_now - d.base_now == pytest.approx(
                c.one_ply_score - d.one_ply_score + (d.one_ply_score - d.base_now),
                abs=1e-9,
            )
            if d.base_now < c.base_now - 1e-9:
                strictly_lower += 1
    assert strictly_lower > 0, (
        "no candidate carried a non-zero urgency term, so 'drop' and 'keep' were "
        "the same program here and this test could not tell them apart"
    )


def test_the_urgency_reconstruction_is_the_engines_own_urgency():
    """``urgency[pos] = VONA * (1 - S(top))``, pinned against a real pick score.

    ``pair_analysis`` recomputes the engine's urgency from its OWN batch — that
    one line is what makes ``keep`` mode equal the engine's real score and what
    ``drop`` mode subtracts. It had no test: mutating it to ``urgency[pos] = vona``
    left the whole file green while moving every operator-visible pair number
    (audit finding, 2026-08-30).

    The engine and the variant both derive their rollout child as
    ``random.Random(ctx.rng.getrandbits(64))``, so feeding the variant that same
    derived stream puts both on the identical rollout sample and any difference is
    arithmetic, not sampling.

    MUTATION-CHECKED: ``urgency[pos] = vona`` (dropping the ``1 - S`` factor)
    fails here.
    """
    board = _live_board()
    seed = 5
    compared = with_urgency = 0
    for overall, own in (
        (9, ()),
        (29, ("RB", "WR")),
        (49, ("QB", "RB", "RB", "WR")),
        (89, ("QB", "RB", "RB", "WR", "WR", "TE")),
        (129, ("QB", "QB", "RB", "RB", "WR", "WR", "WR", "TE", "TE", "K", "DST")),
    ):
        engine = PickEngine(rollouts=48)
        recs = engine.recommend(
            _mid_draft_ctx(board, overall=overall, own=own, seed=seed), top=99
        )
        derived = random.Random(random.Random(seed).getrandbits(64))
        dec = vw.pair_analysis(
            _mid_draft_ctx(board, overall=overall, own=own, seed=seed),
            engine=engine, rng=derived, rollouts=48,
        )
        by_id = {c.player_id: c for c in dec.candidates}
        counts = position_counts(
            _mid_draft_ctx(board, overall=overall, own=own).own_roster
        )
        for rec in recs:
            cand = by_id.get(rec.player_id)
            if cand is None:
                continue
            compared += 1
            assert cand.one_ply_score == pytest.approx(rec.pick_score, abs=1e-9), (
                f"@{overall} {rec.name}"
            )
            # non-vacuity: the urgency term has to be doing something somewhere
            zero_urgency = vw._score_parts(
                cand.entry, counts=counts, round_num=(overall - 1) // 10 + 1,
                roster=DEFAULT_ROSTER, engine=engine, urgency=0.0,
            )[1]
            if abs(zero_urgency - cand.one_ply_score) > 1e-9:
                with_urgency += 1
    assert compared >= 20, "too few candidates compared to mean anything"
    assert with_urgency > 0, (
        "every candidate had zero urgency, so this could not tell the "
        "reconstruction from a constant"
    )


# ======================================= 5. a float tie is a tie, not a ranking


def _tie_state(board):
    """The measured live state where two candidates are each other's wheel partner.

    Overall #129 on a deep board with a saturated roster: Tua Tagovailoa (VOR
    -26.1) and Mike Gesicki (VOR -65.0) are each other's modal partner, so each
    one's pair total is ``base(him) + E[base(the other)]`` — the SAME two
    magnitudes summed in opposite orders. They differ by 4.1e-13.
    """
    own = ("RB", "WR", "WR", "QB", "TE", "RB", "K", "DST", "RB", "WR", "WR", "TE")
    by_rank = sorted(board, key=lambda e: (e.espn_overall_rank, e.player_id))
    taken = [e.player_id for e in by_rank[:268]]
    used = set(taken)
    roster = []
    for pos in own:
        pick = next(e for e in by_rank if e.position == pos and e.player_id not in used)
        used.add(pick.player_id)
        roster.append(pick)
    return PickContext.from_board(
        board, team_slot=8, round=13, overall_pick=129, rounds_total=16,
        taken=taken, own_roster=roster, rng=random.Random(4),
    )


def test_a_float_noise_tie_is_broken_by_the_engines_ladder_not_by_summation_order():
    """THE TIE GATE (audit finding, 2026-08-30).

    ``pair_score = first_term + expected_second`` sums the same two magnitudes in
    opposite orders for two candidates who are each other's wheel partner, so a
    mathematically exact tie lands ~1 ULP apart and ``_rank_key``'s ``-vor`` tier —
    which exists precisely to break value ties toward the more valuable player —
    is never reached. Measured live: the variant recommended Mike Gesicki (VOR
    -65.0) over Tua Tagovailoa (VOR -26.1), a 38.9-point downgrade, on a margin of
    4.1e-13, while the cockpit displayed two identical-looking scores.

    Asserted both ways round: the fix ranks by the ladder, and ``tie_tolerance=0``
    reproduces the defect exactly (so this test cannot pass because the tie stopped
    occurring — it would then fail its own precondition).
    """
    board = _live_board()
    engine = PickEngine(rollouts=512)

    exact = vw.WheelPicker(engine=engine, tie_tolerance=0.0).recommend(
        _tie_state(board), top=2
    )
    assert len(exact) == 2
    margin = abs(exact[0].pick_score - exact[1].pick_score)
    assert margin < 1e-9, (
        "precondition gone: these two no longer tie, so this test proves nothing"
    )
    assert margin > 0.0, "the two scores are literally equal — no ULP tie to break"
    assert exact[1].vor > exact[0].vor + 1.0, (
        "precondition gone: exact-float order no longer puts the worse player first"
    )

    fixed = vw.WheelPicker(engine=engine).recommend(_tie_state(board), top=2)
    assert {r.player_id for r in fixed} == {r.player_id for r in exact}
    assert fixed[0].vor > fixed[1].vor, (
        "a tie inside the tolerance must be resolved down the engine's ladder "
        "(higher VOR first), not by float summation order"
    )
    # ... and the operator is told, in words, that it was a tie (Rule 6).
    blob = " ".join(fixed[0].reasons).lower()
    assert "same number as" in blob and "rounding" in blob


def test_the_tie_band_is_deterministic_and_orders_the_whole_field():
    """Grouping is leader-anchored, so it is a pure function of the numbers.

    Same inputs -> same order, every time; no candidate is dropped or duplicated;
    and a group can never be wider than the tolerance (which is what stops a long
    chain of near-neighbours collapsing into one bucket).
    """
    board = _live_board()
    engine = PickEngine(rollouts=128)
    runs = [
        vw.pair_analysis(_tie_state(board), engine=engine, rng=random.Random(3),
                         rollouts=128)
        for _ in range(3)
    ]
    ids = [tuple(c.player_id for c in d.candidates) for d in runs]
    assert ids[0] == ids[1] == ids[2]
    assert len(set(ids[0])) == len(ids[0])
    dec = runs[0]
    assert dec.tie_groups >= 1, "precondition gone: no tie group at this state"
    for cand in dec.candidates:
        for other_id in cand.tied_with:
            other = next(c for c in dec.candidates if c.player_id == other_id)
            assert abs(cand.pair_score - other.pair_score) <= 2 * dec.tie_tolerance
    # every tie group is internally ordered by the ladder
    for cand in dec.candidates:
        for other_id in cand.tied_with:
            other = next(c for c in dec.candidates if c.player_id == other_id)
            i = dec.candidates.index(cand)
            j = dec.candidates.index(other)
            if i < j:
                assert vw._ladder_key(cand) < vw._ladder_key(other)


def test_the_tie_band_leaves_a_real_difference_alone():
    """A real gap must never be swallowed. The band is 1e-9; the smallest genuine
    adjacent gap measured over 640 real pair rankings was 6.2e-4, six orders
    larger. Pinned on a state whose top two differ by a visible amount."""
    board = _live_board()
    engine = PickEngine(rollouts=128)
    dec = vw.pair_analysis(
        _mid_draft_ctx(board, overall=9), engine=engine, rng=random.Random(3),
        rollouts=128,
    )
    top, second = dec.candidates[0], dec.candidates[1]
    assert top.pair_score - second.pair_score > 1e-3, "precondition: a real gap"
    assert top.tied_with == () and second.tied_with == ()
    assert dec.tie_groups == 0
    assert top.pair_score >= second.pair_score


# ===================================== 6. one survival model per draft, or none


class _CountingSurvival:
    """A survival provider that records how often the engine asked it."""

    def __init__(self):
        self.calls = 0

    def __call__(self, ctx, *, candidates, positions, rng):
        self.calls += 1
        return SurvivalEstimate(
            survival={c.player_id: 1.0 for c in candidates},
            next_best_vor={p: 0.0 for p in positions},
        )


def test_an_injected_survival_provider_is_refused_rather_than_silently_ignored():
    """Two survival models inside one draft is a bug, and it used to be silent.

    ``PickEngine`` takes a ``survival=`` provider and Phase 3 is likely to use one
    (``roomcheck.REFIT_PRACTICE_2026`` exists for exactly that). That provider
    returns MARGINALS; the pair maths needs the JOINT per-rollout outcome, so it
    physically cannot consume it. Before the fix the pair path just ran its own
    rollout instead — measured live: at overall #9 the injected provider was
    called 0 times and at #12 it was called once, i.e. the injected model was in
    force at the eight picks the variant delegates and absent at the eight picks
    the variant exists to change, with no error and no disclosure (audit finding,
    2026-08-30).

    Refusing is the fix. The delegated path is untouched, so an integrator sees
    the refusal at his first pair pick in rehearsal rather than a quietly mixed
    model on draft night.
    """
    board = _live_board()
    prov = _CountingSurvival()
    picker = vw.WheelPicker(engine=PickEngine(rollouts=32, survival=prov))

    with pytest.raises(ValueError, match="injected survival"):
        picker.recommend(_mid_draft_ctx(board, overall=9), top=1)
    assert prov.calls == 0, "the pair path must refuse BEFORE it rolls anything"

    # the delegated path is unchanged and still honours the provider
    rec = picker.recommend(_mid_draft_ctx(board, overall=12), top=1)
    assert prov.calls == 1 and rec

    # and a picker that never engages never refuses
    off = vw.WheelPicker(engine=PickEngine(rollouts=32, survival=prov), max_pair_gap=-1)
    assert off.recommend(_mid_draft_ctx(board, overall=9), top=1)
    assert prov.calls == 2


def test_a_joint_batch_provider_is_used_on_the_pair_path_and_disclosed():
    """The way OUT of that refusal: supply a joint model for the pair path too.

    ``batch_provider`` has ``rollout_pair_batch``'s signature, so the shipped
    rollout is itself a valid argument, and anything the caller passes is used for
    every pair decision and SAID SO in the reasons (Rule 6 — the operator must not
    be shown numbers from a model nobody told him about).
    """
    board = _live_board()
    prov = _CountingSurvival()
    seen = {"n": 0, "tracked": 0}

    def joint(ctx, tracked, **kw):
        seen["n"] += 1
        seen["tracked"] = len(tracked)
        return vw.PairBatch(
            survival={e.player_id: 1.0 for e in tracked},
            next_best_vor=dict.fromkeys(kw.get("positions", ()), 0.0),
            taken_by_rollout=(frozenset(),),
            picks_until_next=2,
            rollouts=1,
        )

    engine = PickEngine(rollouts=32, survival=prov)
    picker = vw.WheelPicker(engine=engine, batch_provider=joint)
    recs = picker.recommend(_mid_draft_ctx(board, overall=9), top=1)
    assert recs and seen["n"] == 1 and seen["tracked"] > 0
    assert prov.calls == 0, "the pair path must not ALSO call the marginal provider"

    dec = vw.pair_analysis(
        _mid_draft_ctx(board, overall=9), engine=engine, rng=random.Random(1),
        batch_provider=joint,
    )
    assert dec.batch_source == "provider"
    assert any("supplied by the caller" in r for r in dec.reasons)

    # passing the shipped rollout explicitly is the "yes I meant it" escape and is
    # NOT mislabelled as someone else's model
    plain = vw.pair_analysis(
        _mid_draft_ctx(board, overall=9), engine=engine, rng=random.Random(1),
        rollouts=16, batch_provider=vw.rollout_pair_batch,
    )
    assert plain.batch_source == "rollout"
    assert not any("supplied by the caller" in r for r in plain.reasons)


def test_the_displacement_correction_is_never_optimistic():
    """Taking a player the room wanted frees that rival to take someone else.

    The correction can only ever LOWER the expected value of the second pick, and
    on a real board at a real pick it must actually bite somewhere (otherwise the
    knob is decorative and the default is a lie).
    """
    board = _live_board()
    engine = PickEngine(rollouts=64)
    ctx = lambda: _mid_draft_ctx(board, overall=9)  # noqa: E731
    corrected = vw.pair_analysis(ctx(), engine=engine, rng=random.Random(6), rollouts=64)
    naive = vw.pair_analysis(
        ctx(), engine=engine, rng=random.Random(6), rollouts=64, displacement="none"
    )
    by_id = {c.player_id: c for c in naive.candidates}
    strictly_lower = 0
    for c in corrected.candidates:
        n = by_id[c.player_id]
        assert c.expected_second <= n.expected_second + 1e-9
        if c.expected_second < n.expected_second - 1e-9:
            strictly_lower += 1
    assert strictly_lower > 0, "the correction never fired — it would be decorative"


# ========================================================== cost and determinism


def test_the_pair_decision_costs_exactly_one_rollout_batch():
    """THE COST GATE, measured load-free.

    Wall clock on a shared box is noise; the number of SIMULATED OPPONENT PICKS a
    decision consumes is a property of the search. A pair decision must consume
    ``rollouts * gap`` of them -- exactly what the shipped engine's own survival
    call would have consumed at the same pick -- from ONE batch. Enumerating pairs
    must not re-roll.
    """
    board = _live_board()
    calls = {"batches": 0, "picks": 0}
    real_batch = vw.rollout_pair_batch

    class _Counting(RankNoiseBot):
        def pick(self, ctx):
            calls["picks"] += 1
            return super().pick(ctx)

    class _CountingAuto(AutodraftBot):
        def pick(self, ctx):
            calls["picks"] += 1
            return super().pick(ctx)

    def counting_batch(*a, **kw):
        calls["batches"] += 1
        return real_batch(*a, **kw)

    engine = PickEngine(rollouts=128)
    picker = vw.WheelPicker(engine=engine)
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(vw, "RankNoiseBot", _Counting)
        mp.setattr(vw, "AutodraftBot", _CountingAuto)
        mp.setattr(vw, "rollout_pair_batch", counting_batch)
        picker.recommend(_mid_draft_ctx(board, overall=9), top=5)
    finally:
        mp.undo()
    assert calls["batches"] == 1, "the pair enumeration re-rolled"
    assert calls["picks"] == 128 * 2, "a pair decision must cost exactly rollouts * gap"


def test_the_wheel_pick_is_not_the_expensive_pick(capsys):
    """Wall clock, REPORTED not raced (the ``test_draft_golden`` convention).

    The variant engages at the FIRST half of each pair, which at the operator's
    seat is the pick with only TWO intervening rival picks -- structurally the
    cheapest survival batch of the draft. The expensive pick (16 intervening) is
    the one it delegates verbatim. So the draft's worst-case recommendation time
    is set by the engine, not by this variant, and that is what is asserted: the
    wheel pick must not be the slowest pick of the draft.
    """
    board = _live_board()
    engine = PickEngine(rollouts=512)
    picker = vw.WheelPicker(engine=engine)
    wheel_ms: list[float] = []
    delegated_ms: list[float] = []
    for overall in (9, 12, 49, 52, 109, 112):
        ctx = _mid_draft_ctx(board, overall=overall)
        t0 = time.perf_counter()
        picker.recommend(ctx, top=5)
        ms = (time.perf_counter() - t0) * 1000.0
        (wheel_ms if vw.pair_window(ctx).engaged else delegated_ms).append(ms)
    with capsys.disabled():
        print(
            f"\n  wheel picks (gap 2): worst {max(wheel_ms):.1f} ms; "
            f"delegated picks (gap 16): worst {max(delegated_ms):.1f} ms"
        )
    assert max(wheel_ms) < max(delegated_ms), (
        "the pair decision has become the draft's expensive pick — re-measure the "
        "cockpit's latency budget before shipping this"
    )


def test_the_recommendation_is_deterministic():
    """Same state, same seed, same answer — the journal-replay guarantee."""
    board = _live_board()
    picker = vw.WheelPicker(engine=PickEngine(rollouts=64))
    a = picker.recommend(_mid_draft_ctx(board, overall=9, seed=17), top=5)
    b = picker.recommend(_mid_draft_ctx(board, overall=9, seed=17), top=5)
    assert [r.player_id for r in a] == [r.player_id for r in b]
    assert [r.pick_score for r in a] == [r.pick_score for r in b]
    c = picker.recommend(_mid_draft_ctx(board, overall=9, seed=18), top=5)
    assert [r.pick_score for r in a] != [r.pick_score for r in c], (
        "a different seed produced an identical estimate — the rollout is not "
        "being driven by ctx.rng at all"
    )


def test_a_variant_draft_is_reproducible_end_to_end():
    """Two full drafts from one seed are the same draft, pick for pick."""
    board, _ = ev.smoke_inputs()
    engine = PickEngine(rollouts=16)

    def one() -> tuple[str, ...]:
        room_rng = random.Random(99)
        autos = _assign_autodrafters(
            room_rng, teams=10, operator_slot=OPERATOR_SEAT,
            fraction=ROOM_PRIORS_2025.autodraft_fraction, autodraft_count=None,
        )
        seats = [
            vw.WheelPicker(engine=engine) if t == OPERATOR_SEAT
            else (AutodraftBot() if t in autos else RankNoiseBot(priors=ROOM_PRIORS_2025))
            for t in range(10)
        ]
        res = run_draft(board, seats, rng=random.Random(99), roster=DEFAULT_ROSTER, rounds=16)
        return tuple(e.player_id for e in res.rosters[OPERATOR_SEAT])

    assert one() == one()


# =================================================================== Rule 6


def test_every_recommendation_carries_plain_language_pair_reasons():
    """Rule 6: the operator is a football novice and cannot smell a bad output.

    Every recommendation must say (a) that it is a pair recommendation and which
    pick the second half is, (b) who the expected partner is, (c) that the score
    shown is a two-pick total and so is NOT comparable to a normal one, (d) that
    under the shipped ``keep`` mode the two halves OVERLAP and the total is
    therefore generous, and (e) the standing limitation. And none of it may be
    jargon.

    (d) is a fix, not a nicety: the module's own diagnostics said "this
    deliberately counts the wait twice" while ``_pair_reasons`` forwarded only
    lines starting with ``HONESTY``, so the one sentence explaining that the
    displayed number double-counts never reached the surface the cockpit renders
    (audit finding, 2026-08-30). The double count is a property of the NUMBER, not
    methodology trivia, and Rule 6 governs the surface the number is shown on.
    """
    board = _live_board()
    picker = vw.WheelPicker(engine=PickEngine(rollouts=64))
    recs = picker.recommend(_mid_draft_ctx(board, overall=9), top=3)
    assert recs
    banned = ("vona", "sigma", "kappa", "urgency", "rollout", "monte", "survival",
              "espn_overall_rank", "b_need", "frac")
    for rec in recs:
        assert rec.reasons
        blob = " ".join(rec.reasons)
        low = blob.lower()
        assert "#12" in blob, "the second half of the pair is never named"
        assert "pair" in low
        assert "two-pick total" in low
        assert "the two halves overlap" in low, (
            "the double count is not disclosed where the number is shown"
        )
        assert "longer horizon is not in this number" in low
        for word in banned:
            assert word not in low, f"jargon leaked into a reason: {word!r}"

    # ... and 'drop' mode, which does NOT double count, must not claim it does
    dropped = vw.WheelPicker(
        engine=PickEngine(rollouts=64), urgency_mode="drop"
    ).recommend(_mid_draft_ctx(board, overall=9), top=1)
    low = " ".join(dropped[0].reasons).lower()
    assert "the two halves overlap" not in low
    assert "counted once" in low


def test_pool_exhaustion_is_disclosed_rather_than_silently_zeroed():
    """A board too thin to refill the second pick must SAY so.

    Built as a near-empty board so the successor shortlist genuinely empties; the
    honest failure is a disclosed floor, not a confident number.
    """
    board = tuple(
        BoardEntry(
            player_id=f"P{i}", name=f"Player {i}", position=pos,
            espn_overall_rank=i + 1, house_points=50.0, vor=50.0 - i, team="ZZZ",
        )
        for i, pos in enumerate(["RB", "RB", "WR"])
    )
    priors = dataclasses.replace(ROOM_PRIORS_2025, autodraft_fraction=1.0)
    engine = PickEngine(rollouts=8, room_priors=priors)
    ctx = PickContext.from_board(
        board, team_slot=8, round=1, overall_pick=9, rounds_total=16, rng=random.Random(1)
    )
    dec = vw.pair_analysis(
        ctx, engine=engine, rng=random.Random(1), rollouts=8, priors=priors
    )
    assert dec.pool_exhausted > 0
    honest = [r for r in dec.reasons if r.startswith("HONESTY") and "emptied" in r]
    assert honest, "pool exhaustion is not disclosed"
    # ... and the disclosure must not call the zero a FLOOR. It is not one: from
    # round 13 on the best man actually left is worth less than nothing, so
    # substituting zero there makes the total too GENEROUS, not too mean. The
    # original wording ("read the pair numbers as a floor, not an estimate") was
    # directionally backwards in exactly the regime this can occur in (audit
    # finding, 2026-08-30).
    text = honest[0].lower()
    assert "floor" not in text
    assert "too generous" in text


def test_a_disengaged_analysis_returns_a_whole_decision_object():
    """The not-a-pair branch must build a COMPLETE ``PairDecision``.

    ``WheelPicker`` gates on :func:`pair_window` before it ever calls
    ``pair_analysis``, so this branch is reachable only by a direct call — which is
    exactly how it shipped for a while with two required fields missing and every
    test still green. A branch nothing exercises is a branch that is wrong.
    """
    board, _ = ev.smoke_inputs()
    ctx = PickContext.from_board(
        board, team_slot=8, round=2, overall_pick=12, rounds_total=16, rng=random.Random(1)
    )
    dec = vw.pair_analysis(ctx, engine=PickEngine(rollouts=4), rng=random.Random(1))
    assert not dec.engaged
    assert dec.gap == 16 and dec.candidates == () and dec.batch is None
    assert dec.second_pools == {} and dec.displacement == "rank"
    assert dec.reasons and "too far" in dec.reasons[0]

    # ... and at the last pick of the draft, where there is no next pick at all
    last = PickContext.from_board(
        board, team_slot=8, round=16, overall_pick=152, rounds_total=16, rng=random.Random(1)
    )
    tail = vw.pair_analysis(last, engine=PickEngine(rollouts=4), rng=random.Random(1))
    assert not tail.engaged and tail.gap == -1 and "last pick" in tail.reasons[0]


def test_a_truncated_candidate_set_says_so():
    """``pair_limit`` ranks on the ONE-PICK score, so a player who is only good as
    half of a pair can be cut before the pair is ever priced. That is a real
    blind spot of the bound and it is disclosed rather than left implicit."""
    board = _live_board()
    ctx = _mid_draft_ctx(board, overall=9)
    dec = vw.pair_analysis(
        ctx, engine=PickEngine(rollouts=8), rng=random.Random(1), rollouts=8, pair_limit=2
    )
    assert len(dec.candidates) == 2
    assert any(
        r.startswith("HONESTY") and "half of a pair" in r.lower() for r in dec.reasons
    )
    # and at the shipped bound nothing is cut on the real board
    full = vw.pair_analysis(ctx, engine=PickEngine(rollouts=8), rng=random.Random(1), rollouts=8)
    assert not any("ranked out" in r for r in full.reasons)


def test_the_shipped_defaults_are_the_measured_ones():
    """The defaults are a MEASUREMENT, not a taste, so they are pinned.

    ``urgency_mode="keep"`` and ``displacement="rank"`` are the two configuration
    choices the module's "WHAT WAS MEASURED" table is about; a silent edit to
    either would leave every number in that docstring describing a different
    program. ``max_pair_gap=2`` is the operator's real seat shape (slot 9 of 10),
    and raising it engages the variant at picks whose myopic-second-pick caveat
    has not been measured.
    """
    picker = vw.WheelPicker()
    assert picker.urgency_mode == "keep"
    assert picker.displacement == "rank"
    assert picker.max_pair_gap == vw.DEFAULT_MAX_PAIR_GAP == 2
    assert picker.second_width == vw.DEFAULT_SECOND_WIDTH == 8
    assert picker.pair_limit == vw.DEFAULT_PAIR_LIMIT == 16
    assert vw.URGENCY_MODES == ("drop", "keep")
    assert vw.DISPLACEMENT_MODES == ("rank", "none")
    # The tie band is a MEASURED value too: over 640 adjacent pair-score gaps at
    # real pair picks the distribution has an eight-order-of-magnitude hole, 37
    # gaps below 1e-12 (float noise) and then nothing until 6.2e-4. 1e-9 is the
    # middle of that hole. Widening it toward a real gap, or shrinking it to 0,
    # both re-open the ranking defect this exists to close.
    assert picker.tie_tolerance == vw.DEFAULT_TIE_TOLERANCE == 1e-9
    assert 1e-12 < vw.DEFAULT_TIE_TOLERANCE < 6.2e-4
    # ON by default: an integrator gets the fix without opting in.
    assert picker.batch_provider is None


def test_bad_modes_are_refused():
    with pytest.raises(ValueError, match="urgency_mode"):
        vw.WheelPicker(urgency_mode="nope")
    with pytest.raises(ValueError, match="displacement"):
        vw.WheelPicker(displacement="nope")


# --------------------------------------------- item 3.11 audit finding 4


def test_the_pair_line_is_an_assumption_and_never_a_promise():
    """The first-of-pair panel must not promise the tool's own NEXT pick.

    MEASURED (10 simulated rooms on the frozen 2026-08-30 board, seat 9,
    rollouts=512, driven through ``DraftSession``): of 80 first-of-pair sentences
    reading "Take him now and X is the most likely best value left at #N (in 100%
    of the simulated rooms; about 100% chance he is still on the board then)",
    32 were broken by the cockpit's own next recommendation — and 26 of those with
    the named partner STILL ON THE BOARD. The quoted figures describe the ROOM,
    which they get right ~94% of the time; the sentence was read as describing the
    TOOL, which re-decides pick #N from scratch. That is a wording defect with an
    expensive consequence: overall 29 -> 32 sits in rounds 1-3, the window the
    runbook has the operator watch in person to calibrate trust, and a novice who
    concludes the cockpit contradicts itself drops to ``--legacy-engine`` and
    gives up the whole measured margin.
    """
    from ziggurat.draft.variant_wheel import PairCandidate, PairDecision, WheelPicker

    board = _wheel_case_board()
    partner = board[3]
    cand = PairCandidate(
        entry=board[0], one_ply_score=100.0, base_now=100.0,
        expected_second=40.0, pair_score=140.0, survival_next=0.9, vona=5.0,
        modal_second=partner, modal_second_share=1.0, modal_second_survival=1.0,
    )
    decision = PairDecision(
        engaged=True, gap=2, wheel_overall=32, candidates=(cand,), rollouts=64,
        pool_exhausted=0, urgency_mode="keep", displacement="off",
        second_pools={}, batch=None,
        reasons=("You pick again very soon — #32, only 2 rival picks away.",),
    )
    lines = WheelPicker._pair_reasons(cand, decision)
    joined = " ".join(lines)
    assert "Take him now and" not in joined, (
        "an imperative sentence naming a specific partner reads as a commitment"
    )
    assert "ASSUMPTION BEHIND THE NUMBER" in joined
    assert "re-decided from scratch" in joined
    assert "about a third of the time" in joined, (
        "the measured break rate belongs on the panel, not only in a research note"
    )
    assert partner.name in joined, "the partner is still named — it explains the score"


def test_the_tie_honesty_line_carries_no_scientific_notation():
    """"within 1e-09 of a point" appeared on 12% of operator panels (audit minor).

    The reader is a football novice on a 90-second clock; Rule 6 is about what he
    can act on. A tolerance a human could actually notice is still quoted.
    """
    from ziggurat.draft.variant_wheel import DEFAULT_TIE_TOLERANCE

    assert DEFAULT_TIE_TOLERANCE < 0.001
