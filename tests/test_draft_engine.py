"""Unit tests for the item-2.3 draft pick engine (``ziggurat/draft/engine.py``).

All offline — synthetic boards only (Rule 5), and a STUB survival provider so the
engine is exercised without the sibling ``survival.py`` (design: inject survival).
Covers: legality/under-cap at scale, the K/DST divergence play EMERGING from the
urgency mechanism (no special case), bit-for-bit determinism + the total-order
tie-break, novice-legible jargon-free reasons (Rule 6), and archetype presets
moving behaviour in the documented direction.
"""

import random

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER as ROSTER
from ziggurat.draft.bots import (
    BoardEntry,
    FollowEspnRank,
    FollowVor,
    PickContext,
    RankNoiseBot,
    position_counts,
)
from ziggurat.draft.engine import (
    NEED_SCHEDULE_HERO_RB,
    NEED_SCHEDULE_ROBUST_RB,
    NEED_SCHEDULE_ZERO_RB,
    PickEngine,
    SurvivalEstimate,
    risk_sign,
)
from ziggurat.draft.simulator import ROUNDS, run_draft, run_many

# ------------------------------------------------------------- helpers / stubs


def _entry(pid, pos, rank, vor=0.0, pts=None):
    return BoardEntry(pid, pid, pos, rank, pts if pts is not None else vor, vor)


class StubSurvival:
    """A deterministic, configurable stand-in for the sim-derived rollout.

    Lets a test dial per-position survival and per-position VONA directly so the
    engine's board-state behaviour is asserted via MECHANISM, not by reaching into
    a real rollout. ``next_best_vor[pos]`` is set to (best candidate VOR at pos) −
    ``vona_by_pos[pos]``, so ``vona_by_pos`` IS the VONA the engine will compute.
    """

    def __init__(self, *, survival_by_pos=None, survival_by_id=None,
                 vona_by_pos=None, default=1.0):
        self.survival_by_pos = dict(survival_by_pos or {})
        self.survival_by_id = dict(survival_by_id or {})
        self.vona_by_pos = dict(vona_by_pos or {})
        self.default = default
        self.calls = 0

    def __call__(self, ctx, *, candidates, positions, rng):
        self.calls += 1
        surv = {}
        for c in candidates:
            if c.player_id in self.survival_by_id:
                surv[c.player_id] = self.survival_by_id[c.player_id]
            elif c.position in self.survival_by_pos:
                surv[c.player_id] = self.survival_by_pos[c.position]
            else:
                surv[c.player_id] = self.default
        best_vor: dict[str, float] = {}
        for c in candidates:
            best_vor[c.position] = max(best_vor.get(c.position, float("-inf")), c.vor)
        nbv = {pos: best_vor.get(pos, 0.0) - self.vona_by_pos.get(pos, 0.0) for pos in positions}
        return SurvivalEstimate(surv, nbv)


def _mid_draft_ctx(board, own, *, round_num, overall_pick, seed=0):
    return PickContext.from_board(
        board, own_roster=own, roster=ROSTER,
        round=round_num, overall_pick=overall_pick, rng=random.Random(seed),
    )


# ------------------------------------------------------ risk-sign schedule (D4)


def test_risk_sign_is_floor_early_ceiling_late():
    assert risk_sign(1) == -1.0 and risk_sign(3) == -1.0     # protect the floor early
    assert risk_sign(10) == 0.0                              # mid tapers to zero
    assert -1.0 < risk_sign(6) < 0.0                         # tapering through mid
    late = risk_sign(14)
    assert 0.0 < late < 1.0 and late < 1.0                   # reward ceiling late, but WEAK


# --------------------------------------------------- Rule-6 rails (legality/window)


def test_engine_defers_kdst_before_earliest_round(make_draft_board):
    # A board where K/DST are the BEST value AND best rank; round 1 must defer them.
    board = [
        _entry("K-A", "K", 1, vor=99),
        _entry("DST-A", "DST", 2, vor=99),
        _entry("WR-A", "WR", 3, vor=10),
        _entry("RB-A", "RB", 4, vor=8),
    ]
    eng = PickEngine(survival=StubSurvival())
    ctx = _mid_draft_ctx(board, (), round_num=1, overall_pick=1)
    pid = eng.pick(ctx)
    assert pid in ("WR-A", "RB-A")  # never a round-1 kicker/defense (Rule 6 rail)


def test_engine_never_recommends_into_an_illegal_or_capped_roster(make_draft_board):
    # Full snake draft with the engine on the clock: run_draft asserts every
    # finished roster is legal and under cap; a green draft proves the rails.
    board = make_draft_board()
    eng = PickEngine(survival=StubSurvival())
    for slot in (0, 4, 9):
        pickers = [eng if t == slot else RankNoiseBot() for t in range(ROSTER.teams)]
        result = run_draft(board, pickers, rng=random.Random(slot))
        for entries in result.rosters.values():
            counts = position_counts(entries)
            assert len(entries) == ROUNDS
            assert counts.get("DST", 0) <= 1 and counts.get("K", 0) <= 1
        eng_counts = position_counts(result.rosters[slot])
        # a legal starting lineup: exactly the required starters are coverable
        assert eng_counts.get("DST", 0) == 1 and eng_counts.get("K", 0) == 1


def test_engine_fills_a_legal_lineup_across_all_slots(make_draft_board):
    board = make_draft_board()
    eng = PickEngine(survival=StubSurvival())
    for slot in range(ROSTER.teams):
        summary = run_many(board, n=20, operator_slot=slot, strategy=eng, seed=slot)
        assert summary.position_counts_mean["DST"] == pytest.approx(1.0)
        assert summary.position_counts_mean["K"] == pytest.approx(1.0)


# ----------------------------------------------- the K/DST divergence play EMERGES


def _divergence_board():
    """A DST with high house VOR but a DEEP espn rank, next to a slightly-higher
    VOR WR that the room's board ranks near the top. Both fill an open starter, so
    only the survival-timed urgency term can flip the choice."""
    board = [
        _entry("WR-top", "WR", 5, vor=50.0),
        _entry("DST-edge", "DST", 140, vor=45.0),   # house loves it; room's board buries it
        _entry("K-x", "K", 150, vor=2.0),
        _entry("WR-fill", "WR", 40, vor=8.0),
        _entry("RB-fill", "RB", 45, vor=6.0),
        _entry("QB-fill", "QB", 60, vor=5.0),
        _entry("TE-fill", "TE", 70, vor=4.0),
    ]
    # own roster owes a WR starter + DST + K (K/DST window already open at R10).
    own = (
        [_entry("own-qb", "QB", 900)]
        + [_entry(f"own-rb{i}", "RB", 900) for i in range(6)]
        + [_entry("own-wr", "WR", 900), _entry("own-te", "TE", 900)]
    )
    return board, own


def test_kdst_divergence_play_emerges_from_survival_not_a_special_case():
    board, own = _divergence_board()

    # About to run: the DST won't survive AND a cliff of DST value drops off.
    about_to_run = StubSurvival(
        survival_by_pos={"DST": 0.15}, vona_by_pos={"DST": 20.0}, default=0.95
    )
    ctx_a = _mid_draft_ctx(board, own, round_num=10, overall_pick=93)
    take = PickEngine(survival=about_to_run).pick(ctx_a)
    assert take == "DST-edge"  # engine pounces one pick before the room's run

    # Same board, same value gap, same need — but the DST will safely last: WAIT.
    safe = StubSurvival(
        survival_by_pos={"DST": 0.95}, vona_by_pos={"DST": 20.0}, default=0.95
    )
    ctx_b = _mid_draft_ctx(board, own, round_num=10, overall_pick=93)
    wait = PickEngine(survival=safe).pick(ctx_b)
    assert wait == "WR-top"  # nothing forces the defense yet -> take the value

    # And the timing edge is one only the engine sees: both naive baselines take
    # the WR here (higher VOR AND a far better ESPN rank), never the buried DST.
    assert FollowVor().pick(_mid_draft_ctx(board, own, round_num=10, overall_pick=93)) == "WR-top"
    assert FollowEspnRank().pick(_mid_draft_ctx(board, own, round_num=10, overall_pick=93)) == "WR-top"


def test_divergence_note_explains_the_edge_in_plain_language():
    board, own = _divergence_board()
    about_to_run = StubSurvival(
        survival_by_pos={"DST": 0.15}, vona_by_pos={"DST": 20.0}, default=0.95
    )
    rec = PickEngine(survival=about_to_run).recommend(
        _mid_draft_ctx(board, own, round_num=10, overall_pick=93), top=1
    )[0]
    assert rec.position == "DST"
    assert "spots later" in rec.divergence_note
    assert any("scoring" in r for r in rec.reasons)


# ------------------------------------------------------- determinism & tie-break


def test_pick_is_bit_for_bit_reproducible_for_a_fixed_seed(make_draft_board):
    board = make_draft_board()
    eng = PickEngine(survival=StubSurvival(default=0.6))
    own = [_entry("own1", "RB", 900), _entry("own2", "WR", 900)]
    a = eng.pick(_mid_draft_ctx(board, own, round_num=3, overall_pick=25, seed=1234))
    b = eng.pick(_mid_draft_ctx(board, own, round_num=3, overall_pick=25, seed=1234))
    assert a == b


def test_pick_equals_top_recommendation_and_consumes_rng_identically(make_draft_board):
    board = make_draft_board()
    eng = PickEngine(survival=StubSurvival(default=0.6))
    own = [_entry("own1", "RB", 900)]
    ctx_pick = _mid_draft_ctx(board, own, round_num=2, overall_pick=15, seed=7)
    ctx_rec = _mid_draft_ctx(board, own, round_num=2, overall_pick=15, seed=7)
    assert eng.pick(ctx_pick) == eng.recommend(ctx_rec, top=5)[0].player_id
    # both derived the rollout child from ONE getrandbits(64), so the underlying
    # ctx.rng is left in the SAME state (D2 — recommend never diverges from pick).
    assert ctx_pick.rng.getrandbits(64) == ctx_rec.rng.getrandbits(64)


def test_tie_break_prefers_lower_espn_rank_then_player_id():
    # Zero the weights so pick_score == vor exactly, forcing the tie-break chain.
    eng = PickEngine(b_need=0.0, b_vona=0.0, b_risk=0.0, survival=StubSurvival())

    # equal vor, different rank -> lower espn rank wins.
    board = [_entry("WR-late", "WR", 9, vor=50.0), _entry("WR-early", "WR", 3, vor=50.0),
             _entry("RB-x", "RB", 20, vor=5.0)]
    ctx = _mid_draft_ctx(board, (), round_num=1, overall_pick=1)
    assert eng.pick(ctx) == "WR-early"

    # equal vor AND equal rank -> player_id lexicographic wins.
    board2 = [_entry("WR-bbb", "WR", 5, vor=50.0), _entry("WR-aaa", "WR", 5, vor=50.0),
              _entry("RB-y", "RB", 20, vor=5.0)]
    ctx2 = _mid_draft_ctx(board2, (), round_num=1, overall_pick=1)
    assert eng.pick(ctx2) == "WR-aaa"


# ------------------------------------------------------------- reasons (Rule 6)


def test_reasons_are_present_and_free_of_jargon(make_draft_board):
    board = make_draft_board()
    eng = PickEngine(survival=StubSurvival(default=0.5))
    own = [_entry("own1", "RB", 900), _entry("own2", "WR", 900)]
    recs = eng.recommend(_mid_draft_ctx(board, own, round_num=4, overall_pick=35), top=4)
    assert len(recs) >= 1
    for rec in recs:
        assert rec.reasons, "every recommendation must ship non-empty reasons (Rule 6)"
        assert all(isinstance(r, str) and r for r in rec.reasons)
        blob = " ".join(rec.reasons + (rec.need_note, rec.risk_note, rec.divergence_note)).lower()
        assert "vona" not in blob and "sigma" not in blob  # no jargon leaks to the operator


def test_recommend_returns_alternatives_with_why_not(make_draft_board):
    board = make_draft_board()
    eng = PickEngine(survival=StubSurvival(default=0.9))
    own = [_entry("own1", "RB", 900)]
    top = eng.recommend(_mid_draft_ctx(board, own, round_num=5, overall_pick=45), top=3)[0]
    assert top.alternatives  # the next few, each with a one-line why-not
    for name, why in top.alternatives:
        assert isinstance(name, str) and isinstance(why, str) and why


# ----------------------------------------- archetype presets move in the direction


def _archetype_board():
    """Top RB and top WR with EQUAL house VOR; the WR is ranked one slot better so
    the balanced engine breaks the tie to WR. Only the need-schedule can flip it."""
    return [
        _entry("WR-top", "WR", 1, vor=50.0),
        _entry("RB-top", "RB", 2, vor=50.0),
        _entry("QB-x", "QB", 30, vor=6.0),
        _entry("TE-x", "TE", 40, vor=4.0),
    ]


def test_need_schedule_archetypes_shift_early_rb_appetite():
    board = _archetype_board()
    stub = StubSurvival()  # no urgency; isolate the need term
    # Neutralize the floor-early risk tilt (RB has a floor edge) so the ONLY mover
    # between archetypes is the round-conditioned RB need weight.
    common = dict(b_vona=0.0, b_risk=0.0, survival=stub)

    def pick(schedule):
        eng = PickEngine(need_schedule=schedule, **common)
        return eng.pick(_mid_draft_ctx(board, (), round_num=1, overall_pick=1))

    # balanced: equal need, equal VOR -> tie-break to the better-ranked WR.
    assert pick({}) == "WR-top"
    # robust-RB: hammers RB early -> flips the round-1 pick to the RB.
    assert pick(NEED_SCHEDULE_ROBUST_RB) == "RB-top"
    # hero-RB: one elite RB early -> also takes the RB in round 1.
    assert pick(NEED_SCHEDULE_HERO_RB) == "RB-top"
    # zero-RB: defers RB -> stays on the WR (reinforced away from RB).
    assert pick(NEED_SCHEDULE_ZERO_RB) == "WR-top"


def test_hero_rb_defers_rb_after_its_early_spike():
    # Hero-RB is high in R1 but LOW in R2-R5; at round 3 (RB mult 0.35) an
    # equal-VOR WR should be preferred, unlike robust-RB (mult 1.25 -> RB).
    board = _archetype_board()
    stub = StubSurvival()
    common = dict(b_vona=0.0, b_risk=0.0, survival=stub)
    own = [_entry("own-rb", "RB", 900), _entry("own-wr", "WR", 900)]  # both starters still open-ish
    hero = PickEngine(need_schedule=NEED_SCHEDULE_HERO_RB, **common)
    robust = PickEngine(need_schedule=NEED_SCHEDULE_ROBUST_RB, **common)
    ctx_h = _mid_draft_ctx(board, own, round_num=3, overall_pick=25)
    ctx_r = _mid_draft_ctx(board, own, round_num=3, overall_pick=25)
    assert hero.pick(ctx_h) == "WR-top"     # spike is spent; defer RB
    assert robust.pick(ctx_r) == "RB-top"   # robust keeps hammering RB


# ------------------------------------------------- audit-fix regressions (2.3)


def test_divergence_note_suppressed_for_unranked_players():
    # An unranked player carries the 10_000+ sentinel rank; a "spots later" claim
    # from it is nonsense ("~9955 spots later" — audit finding) and must not appear.
    from ziggurat.draft.engine import _divergence_note

    unranked = _entry("K-deep", "K", 10_155, vor=5.0)
    sentence, _ = _divergence_note(unranked, 155, draft_size=160)
    assert sentence == ""

    # A ranked-but-beyond-the-draft player gets absolute phrasing, no N-count.
    deep = _entry("DST-deep", "DST", 300, vor=20.0)
    sentence, _ = _divergence_note(deep, 90, draft_size=160)
    assert "spots later" not in sentence
    assert "slot 300" in sentence


def test_final_pick_reason_never_mentions_a_next_pick(make_draft_board):
    board = make_draft_board()
    # 15 picks in hand -> round 16, the operator's last pick (picks_after == 0).
    own = [_entry(f"own{i}", pos, 500 + i)
           for i, pos in enumerate(
               ["QB", "RB", "RB", "WR", "WR", "TE", "DST", "K",
                "RB", "WR", "TE", "QB", "RB", "WR", "WR"])]
    ctx = _mid_draft_ctx(board, own, round_num=16, overall_pick=160)
    recs = PickEngine(survival=StubSurvival()).recommend(ctx, top=1)
    joined = " ".join(recs[0].reasons)
    assert "next pick" not in joined
    assert "last pick" in joined


def test_no_rush_and_take_now_reasons_never_contradict(make_draft_board):
    # Survival above the wait-gate: the divergence line must not say "now" while
    # the survival line says "no rush" (audit finding: self-contradiction).
    board = make_draft_board()
    ctx = _mid_draft_ctx(board, [], round_num=9, overall_pick=86)
    engine = PickEngine(survival=StubSurvival(default=0.95))
    for rec in engine.recommend(ctx, top=5):
        joined = " ".join(rec.reasons)
        if "no rush" in joined:
            assert "values this pick now" not in joined


def test_engine_rollout_knobs_thread_into_default_provider(make_draft_board):
    # PickEngine(rollouts=, kappa=, room_priors=) must reach rollout_survival —
    # before the fix the documented R=512 live budget was unreachable (audit).
    import ziggurat.draft.survival as survival_mod
    from ziggurat.draft.priors import ROOM_PRIORS_2025

    seen = {}
    real = survival_mod.rollout_survival

    def spy(ctx, candidates, *, rng, **kw):
        seen.update(kw)
        return real(ctx, candidates, rng=rng, **kw)

    board = make_draft_board()
    ctx = _mid_draft_ctx(board, [], round_num=1, overall_pick=1)
    import dataclasses as _dc
    priors = _dc.replace(ROOM_PRIORS_2025, reach_sigma=25.0)
    engine = PickEngine(rollouts=16, kappa=1.0, room_priors=priors)
    import unittest.mock as mock
    with mock.patch.object(survival_mod, "rollout_survival", spy):
        engine.recommend(ctx, top=1)
    assert seen.get("rollouts") == 16
    assert seen.get("kappa") == 1.0
    assert seen.get("priors") is priors


# ------------------------------------------- lineup-reachability discount (C2)


def _saturated_own():
    """QB1 + RB2 + WR2 + TE2: every dedicated starter filled, flex covered by
    the TE surplus — any further QB/RB/WR/TE pick is pure bench."""
    return [
        _entry("q1", "QB", 1, 50.0), _entry("r1", "RB", 2, 60.0),
        _entry("r2", "RB", 3, 55.0), _entry("w1", "WR", 4, 45.0),
        _entry("w2", "WR", 5, 40.0), _entry("t1", "TE", 6, 35.0),
        _entry("t2", "TE", 7, 20.0),
    ]


def test_bench_qb_discounted_below_bench_rb():
    # Rehearsal-1 finding (2026-07-24): a high-VOR backup QB in a 1-QB league
    # must NOT outscore a moderate bench RB — the QB can never reach the lineup.
    board = [_entry("qb2", "QB", 10, 35.0), _entry("rb3", "RB", 30, 19.0)]
    ctx = _mid_draft_ctx(board, _saturated_own(), round_num=7, overall_pick=65)
    eng = PickEngine(b_vona=0.0, b_risk=0.0, survival=StubSurvival())
    recs = eng.recommend(ctx, top=2)
    assert recs[0].position == "RB", (
        "bench QB outscored bench RB — lineup-reachability discount not applied"
    )


def test_negative_vor_is_never_shrunk_by_the_discount():
    # frac applies only to POSITIVE value: a bench QB with vor=-10 keeps the
    # full -10 (shrinking it would make a WORSE player score better).
    # NOTE: this pin is identical with the discount absent — it discriminates
    # only against the shrink-negatives-too variant; the sibling tests catch a
    # full revert (audit 2026-07-24).
    board = [_entry("qb2", "QB", 10, -10.0)]
    ctx = _mid_draft_ctx(board, _saturated_own(), round_num=7, overall_pick=65)
    eng = PickEngine(b_vona=0.0, b_risk=0.0, survival=StubSurvival())
    rec = eng.recommend(ctx, top=1)[0]
    assert rec.pick_score == pytest.approx(-10.0 + 25.0 * 0.35)


def test_positive_bench_qb_vor_keeps_only_the_insurance_fraction():
    board = [_entry("qb2", "QB", 10, 40.0)]
    ctx = _mid_draft_ctx(board, _saturated_own(), round_num=7, overall_pick=65)
    eng = PickEngine(b_vona=0.0, b_risk=0.0, survival=StubSurvival())
    rec = eng.recommend(ctx, top=1)[0]
    assert rec.pick_score == pytest.approx(40.0 * 0.25 + 25.0 * 0.35)


def test_flex_eligible_te_with_open_flex_keeps_full_value():
    # TE2 while the flex is OPEN is startable — no discount. Own roster: all
    # dedicated starters filled but zero surplus, so the flex slot is open.
    own = [
        _entry("q1", "QB", 1, 50.0), _entry("r1", "RB", 2, 60.0),
        _entry("r2", "RB", 3, 55.0), _entry("w1", "WR", 4, 45.0),
        _entry("w2", "WR", 5, 40.0), _entry("t1", "TE", 6, 35.0),
    ]
    board = [_entry("te2", "TE", 10, 30.0)]
    ctx = _mid_draft_ctx(board, own, round_num=7, overall_pick=65)
    eng = PickEngine(b_vona=0.0, b_risk=0.0, survival=StubSurvival())
    rec = eng.recommend(ctx, top=1)[0]
    assert rec.pick_score == pytest.approx(30.0 + 25.0)  # full vor + open-flex need


def test_bench_qb_reason_names_the_insurance_discount():
    # Rule 6: when a backup QB IS the recommendation, the discount is said aloud.
    board = [_entry("qb2", "QB", 10, 35.0)]
    ctx = _mid_draft_ctx(board, _saturated_own(), round_num=7, overall_pick=65)
    eng = PickEngine(b_vona=0.0, b_risk=0.0, survival=StubSurvival())
    rec = eng.recommend(ctx, top=1)[0]
    assert any("injury insurance" in r for r in rec.reasons)
