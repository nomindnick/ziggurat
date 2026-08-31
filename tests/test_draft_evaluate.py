"""Tests for the paired-comparison evaluation harness (draft/evaluate.py).

THE ONE THAT MATTERS IS THE PAIRING. Everything else this harness reports is
arithmetic over the differences it produces, so if the pairing is broken every
number is noise wearing a confidence interval. Two tests carry that weight and
they are deliberately a matched pair:

  * :func:`test_a_strategy_paired_against_itself_is_exactly_zero` — the stated
    contract.
  * :func:`test_pairing_survives_a_strategy_that_burns_extra_randomness` — the
    one with teeth. A strategy that drafts IDENTICALLY but consumes one extra
    random number must still difference to exactly 0.0. Under
    ``simulator.run_many``'s single shared stream it does not, because the
    operator's consumption shifts every subsequent rival draw; that is asserted
    directly in :func:`test_the_shared_stream_really_is_the_thing_being_fixed`,
    which exists so the test above cannot pass vacuously.

Everything runs offline on :func:`evaluate.smoke_inputs`' synthetic board (Rule
5: invented players, invented teams, no real league identity anywhere) and is
deterministic — no network, no database, no wall clock, no global random.
"""

import math
import random
import statistics
from dataclasses import dataclass

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.draft import evaluate as ev
from ziggurat.draft import grader
from ziggurat.draft.bots import (
    BoardEntry,
    FollowEspnRank,
    FollowVor,
    PickContext,
    position_counts,
)
from ziggurat.draft.priors import ROOM_PRIORS_2025
from ziggurat.draft.simulator import DraftResult

SLOTS = (0, 4, 8)


@pytest.fixture(scope="module")
def smoke():
    """The synthetic board + points map, built once for the whole module."""
    return ev.smoke_inputs()


# --------------------------------------------------------------- stub pickers


@dataclass(frozen=True)
class ChattyTwin:
    """Drafts EXACTLY like ``inner`` but burns one random draw first.

    The instrument for the pairing test: identical picks, different randomness
    consumption. Any difference the harness reports between ``inner`` and this
    is manufactured by the harness itself.
    """

    inner: object = FollowVor()

    def pick(self, ctx: PickContext) -> str:
        ctx.rng.random()
        return self.inner.pick(ctx)


# ============================================================ 1. the pairing


def test_a_strategy_paired_against_itself_is_exactly_zero(smoke):
    """THE contract. If this is not exactly 0.0 the pairing is broken and every
    number this harness produces is noise."""
    board, weekly = smoke
    strat = FollowVor()
    r = ev.paired_compare(
        board, weekly, a=strat, b=strat, name_a="vor", name_b="vor-again",
        n=3, slots=SLOTS, seed=11, bootstrap=100,
    )
    assert r.mean_delta == 0.0
    assert r.sd_delta == 0.0
    assert r.median_delta == 0.0
    assert r.deltas == (0.0,) * 9
    assert r.ties == r.n == 9
    assert r.win_rate == 0.0
    assert (r.ci_low, r.ci_high) == (0.0, 0.0)
    assert r.excludes_zero is False
    assert r.field_mean_delta == 0.0
    # ...and the two arms really did produce the same rosters, not merely the
    # same score, which a broken grader could also do.
    assert r.shape_a == r.shape_b
    assert r.holes_a == r.holes_b
    assert r.rank_a == r.rank_b


def test_pairing_survives_a_strategy_that_burns_extra_randomness(smoke):
    """The one with teeth: identical PICKS, different random consumption.

    ``PickEngine`` draws a child stream on every decision and a variant may draw
    a different amount; if that reaches the rivals, the room is no longer the
    same room and the measured difference is part signal, part reshuffle.
    """
    board, weekly = smoke
    r = ev.paired_compare(
        board, weekly, a=FollowVor(), b=ChattyTwin(),
        name_a="vor", name_b="vor-but-chatty",
        n=4, slots=SLOTS, seed=3, bootstrap=100,
    )
    assert r.mean_delta == 0.0
    assert r.sd_delta == 0.0
    assert r.ties == r.n == 12
    assert r.excludes_zero is False


def test_the_shared_stream_really_is_the_thing_being_fixed(smoke):
    """Mutation-strength for the test above: with ``run_many``'s single shared
    stream the SAME two identical-picking strategies differ on every draft."""
    board, weekly = smoke
    r = ev.paired_compare(
        board, weekly, a=FollowVor(), b=ChattyTwin(),
        name_a="vor", name_b="vor-but-chatty",
        n=4, slots=SLOTS, seed=3, bootstrap=100, paired_streams=False,
    )
    assert r.ties == 0, "the shared stream should have perturbed every draft"
    assert r.sd_delta > 0.05, (
        "two strategies that draft identically read as pure noise under a shared "
        f"stream; got sd {r.sd_delta}"
    )
    assert r.reasons[1].startswith("NOT stream-paired")


def test_both_arms_face_the_identical_room_pick_for_pick(smoke):
    """Mechanism-level: two identical-picking operators produce byte-identical
    pick logs, so every rival decision in the draft was the same decision."""
    board, _weekly = smoke
    common = dict(
        slot=8, draft_seed=987654321, priors=ROOM_PRIORS_2025,
        roster=DEFAULT_ROSTER, rounds=16, autodraft_count=None,
    )
    a = ev._draft_once(board, FollowVor(), paired_streams=True, **common)
    b = ev._draft_once(board, ChattyTwin(), paired_streams=True, **common)
    assert a.pick_log == b.pick_log

    # ...and the shared-stream room diverges from the operator's FIRST pick on.
    a2 = ev._draft_once(board, FollowVor(), paired_streams=False, **common)
    b2 = ev._draft_once(board, ChattyTwin(), paired_streams=False, **common)
    assert a2.pick_log[:8] == b2.pick_log[:8], "rivals before the operator must match"
    assert a2.pick_log != b2.pick_log, "expected the shared stream to diverge after"


def test_the_room_is_the_production_room(smoke):
    """The autodraft seats come from ``simulator._assign_autodrafters`` on a
    fresh ``Random(draft_seed)``, so they are identical in both stream modes and
    identical between the two arms of a pair."""
    board, _weekly = smoke
    from ziggurat.draft.simulator import _assign_autodrafters

    seed = 424242
    # ``autodraft_count`` forces exactly two autopilot seats, which is the
    # simulator's own deterministic hook — the probabilistic default draws an
    # EMPTY set at plenty of seeds (this one included), which would make the
    # assertion below vacuous rather than false.
    expected = _assign_autodrafters(
        random.Random(seed), teams=10, operator_slot=8,
        fraction=ROOM_PRIORS_2025.autodraft_fraction, autodraft_count=2,
    )
    assert len(expected) == 2 and 8 not in expected
    # An AutodraftBot seat follows pure ESPN board order, so its round-1 pick is
    # the best-ranked player left. Drive a draft and confirm the seats we think
    # are on autopilot behave like it.
    result = ev._draft_once(
        board, FollowVor(), slot=8, draft_seed=seed, priors=ROOM_PRIORS_2025,
        roster=DEFAULT_ROSTER, rounds=16, autodraft_count=2, paired_streams=True,
    )
    ranks = {e.player_id: e.espn_overall_rank for e in board}
    round1 = [(team, pid) for overall, team, pid in result.pick_log if overall <= 10]
    taken_before: list[str] = []
    for team, pid in round1:
        if team in expected:
            best = min(
                (r for p, r in ranks.items() if p not in taken_before), default=None
            )
            assert ranks[pid] == best, f"autodraft seat {team} did not take the top board player"
        taken_before.append(pid)


def test_build_paired_result_refuses_arms_that_are_not_lined_up(smoke):
    board, weekly = smoke
    a = ev.evaluate_strategy(board, weekly, strategy=FollowVor(), n=2, slots=(0, 4), seed=1)
    b = ev.evaluate_strategy(board, weekly, strategy=FollowEspnRank(), n=2, slots=(0, 4), seed=1)
    with pytest.raises(ev.EvaluationInputError, match="NOT paired"):
        ev.build_paired_result(
            a, tuple(reversed(b)), name_a="a", name_b="b", slots=(0, 4),
            n_per_slot=2, seed=1, bootstrap=10,
        )
    with pytest.raises(ev.EvaluationInputError, match="same grid"):
        ev.build_paired_result(
            a, b[:2], name_a="a", name_b="b", slots=(0, 4), n_per_slot=2, seed=1,
            bootstrap=10,
        )


# ==================================================== 2. it grades with grader


def test_the_reported_objective_is_the_graders_own_number(smoke):
    """No re-implementation: the harness's objective for a draft must equal
    ``grader.grade_roster``'s objective for that same roster against those same
    nine rivals."""
    board, weekly = smoke
    outcomes = ev.evaluate_strategy(
        board, weekly, strategy=FollowVor(), n=1, slots=(8,), seed=77
    )
    (out,) = outcomes
    replay = ev._draft_once(
        board, FollowVor(), slot=8, draft_seed=out.draft_seed,
        priors=ROOM_PRIORS_2025, roster=DEFAULT_ROSTER, rounds=16,
        autodraft_count=None, paired_streams=True,
    )
    rivals = {t: r for t, r in replay.rosters.items() if t != 8}
    direct = grader.grade_roster(
        list(replay.rosters[8]), weekly, opponent_rosters=rivals,
        positions=weekly.positions,
    )
    assert out.objective == direct.objective
    assert out.expected_wins == direct.expected_wins
    assert out.hole_weeks == tuple(direct.hole_weeks)
    assert out.holes == len(direct.hole_weeks)


def test_it_does_not_use_the_old_season_sum_metric(smoke):
    """The harness must be sensitive to something ``optimal_starting_points`` is
    blind to. Grade two rosters whose season totals are IDENTICAL and whose bye
    alignment is not; the harness's grade function must separate them."""
    from ziggurat.draft.simulator import optimal_starting_points

    def row(pid, pos, per_week, bye, team):
        played = [w for w in range(1, 18) if w != bye]
        return pid, pos, per_week, tuple(played), team

    spec = [
        row("QB1", "QB", 20.0, 6, "A"), row("RB1", "RB", 15.0, 8, "B"),
        row("RB2", "RB", 14.0, 9, "C"), row("WR1", "WR", 13.0, 10, "D"),
        row("WR2", "WR", 12.0, 11, "E"), row("TE1", "TE", 10.0, 12, "F"),
        row("FLX", "RB", 11.0, 13, "G"), row("DST1", "DST", 8.0, 7, "H"),
        row("K1", "K", 7.0, 5, "I"),
        # the swing man: same points, different bye
        row("RB3", "RB", 9.0, 8, "J"),     # collides with RB1
        row("RB3b", "RB", 9.0, 14, "K"),   # does not
    ]
    entries, weekly, positions = {}, {}, {}
    for pid, pos, per_week, played, team in spec:
        entries[pid] = BoardEntry(
            player_id=pid, name=pid, position=pos, espn_overall_rank=len(entries) + 1,
            house_points=per_week * len(played), vor=per_week * len(played), team=team,
        )
        weekly[pid] = {w: per_week for w in played}
        positions[pid] = pos

    base = [entries[p] for p, *_ in spec[:9]]
    collide = base + [entries["RB3"]]
    spread = base + [entries["RB3b"]]

    # Nine rivals, each a distinct copy of the same nine starters, so both
    # variants are graded against an identical (and non-empty) field.
    rivals = {}
    for r in range(1, DEFAULT_ROSTER.teams):
        squad = []
        for e in base:
            pid = f"r{r}_{e.player_id}"
            squad.append(
                BoardEntry(
                    player_id=pid, name=pid, position=e.position,
                    espn_overall_rank=e.espn_overall_rank + 100 * r,
                    house_points=e.house_points, vor=e.vor, team=e.team,
                )
            )
            weekly[pid] = dict(weekly[e.player_id])
            positions[pid] = e.position
        rivals[r] = squad

    # The old metric cannot tell them apart at all...
    assert optimal_starting_points(collide) == optimal_starting_points(spread)
    # ...and the harness's grade function can.
    grade = ev.make_grade_fn(weekly, positions=positions)
    assert grade(spread, rivals).objective > grade(collide, rivals).objective


def test_shape_and_holes_describe_the_operators_roster(smoke):
    board, weekly = smoke
    outcomes = ev.evaluate_strategy(
        board, weekly, strategy=FollowEspnRank(), n=2, slots=(8,), seed=5
    )
    for out in outcomes:
        assert sum(out.shape.values()) == 16, "16 rounds must produce 16 players"
        assert set(out.shape) == set(ev.SHAPE_POSITIONS)
        assert out.holes == len(out.hole_weeks)
        assert 1 <= out.rank <= DEFAULT_ROSTER.teams


# ================================================= 3. the anti-cheat: the rank


def _fake_result(objectives):
    """A ten-team DraftResult whose teams are separable by a stub grade fn."""
    rosters = {}
    for team, _obj in enumerate(objectives):
        rosters[team] = (
            BoardEntry(
                player_id=f"p{team}", name=f"p{team}", position="QB",
                espn_overall_rank=team + 1, house_points=0.0, vor=0.0, team="AAA",
            ),
        )
    return DraftResult(rosters=rosters, pick_log=())


def _stub_grade(objectives):
    """A GradeFn that returns a preset objective keyed on the roster's player."""

    def grade(entries, opponents):
        team = int(entries[0].player_id[1:])
        return grader.SeasonGrade(
            objective=objectives[team], expected_wins=objectives[team],
            playoff_prob=0.0, title_prob=0.0, weekly_means=(), hole_weeks=(),
            reasons=(),
        )

    return grade


def test_rank_counts_every_graded_team_not_just_the_operator():
    """A strategy that scores well by leaving its rivals a worse board gains
    nothing in RANK — so rank must be computed from all ten grades."""
    objectives = [6.0, 9.0, 5.0, 8.5, 4.0, 7.0, 3.0, 2.0, 7.5, 1.0]
    out = ev._outcome(
        _fake_result(objectives), slot=8, draft_seed=0,
        grade=_stub_grade(objectives), field_check=False,
    )
    # 9.0 and 8.5 beat the operator's 7.5; nothing else does.
    assert out.objective == 7.5
    assert out.rank == 3
    assert out.field_objective is None


def test_a_tie_takes_the_better_rank_not_the_worse():
    """Mutation guard on the ``>`` in the rank count: a ``>=`` would call a
    first-place tie a second place."""
    objectives = [7.5, 7.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 7.5, 1.0]
    out = ev._outcome(
        _fake_result(objectives), slot=8, draft_seed=0,
        grade=_stub_grade(objectives), field_check=False,
    )
    assert out.rank == 1


def test_the_fixed_field_second_opinion_is_optional_and_separate(smoke):
    board, weekly = smoke
    on = ev.evaluate_strategy(
        board, weekly, strategy=FollowVor(), n=1, slots=(8,), seed=9, field_check=True
    )
    off = ev.evaluate_strategy(
        board, weekly, strategy=FollowVor(), n=1, slots=(8,), seed=9, field_check=False
    )
    assert on[0].field_objective is not None
    assert off[0].field_objective is None
    # Same draft either way — the second opinion must not perturb the experiment.
    assert on[0].objective == off[0].objective
    # It is a DIFFERENT number: the modelled field is not these rivals.
    assert on[0].field_objective != on[0].objective

    r = ev.build_paired_result(
        off, off, name_a="x", name_b="x", slots=(8,), n_per_slot=1, seed=9, bootstrap=10
    )
    assert r.field_mean_delta is None


# ============================================================== 4. statistics


@pytest.mark.parametrize(
    "df,expected",
    [(1, 12.7062), (2, 4.3027), (5, 2.5706), (10, 2.2281), (30, 2.0423), (100, 1.9840)],
)
def test_t_quantiles_match_published_tables(df, expected):
    """Published two-sided 95% t values. A normal quantile (1.9600) fails every
    row but the last, which is exactly why this is not a normal approximation."""
    assert ev._t_ppf(0.975, df) == pytest.approx(expected, abs=5e-4)


def test_t_cdf_is_a_distribution():
    assert ev._t_cdf(0.0, 7) == pytest.approx(0.5, abs=1e-12)
    assert ev._t_cdf(-2.0, 7) == pytest.approx(1.0 - ev._t_cdf(2.0, 7), abs=1e-12)
    assert ev._t_cdf(50.0, 7) > 0.9999
    assert ev._t_cdf(-50.0, 7) < 0.0001


def test_the_interval_is_a_hand_computable_paired_t():
    deltas = [0.5, -0.1, 0.3, 0.7, 0.2, -0.2, 0.4, 0.6, 0.1, 0.0]
    mean, lo, hi, sd = ev._t_interval(deltas, 0.95)
    want_mean = statistics.fmean(deltas)
    want_sd = statistics.stdev(deltas)
    want_half = 2.2622 * want_sd / math.sqrt(10)  # t(0.975, df=9)
    assert mean == pytest.approx(want_mean)
    assert sd == pytest.approx(want_sd)
    assert lo == pytest.approx(want_mean - want_half, abs=1e-3)
    assert hi == pytest.approx(want_mean + want_half, abs=1e-3)


def test_a_zero_variance_result_gets_a_point_interval_not_an_invented_one():
    mean, lo, hi, sd = ev._t_interval([0.25] * 8, 0.95)
    assert (mean, lo, hi, sd) == (0.25, 0.25, 0.25, 0.0)


def test_a_single_pair_refuses_to_pretend_it_has_an_interval():
    mean, lo, hi, sd = ev._t_interval([0.4], 0.95)
    assert mean == 0.4
    assert lo == float("-inf") and hi == float("inf")


def test_excludes_zero_is_only_true_when_the_interval_clears_zero(smoke):
    board, weekly = smoke
    straddling = ev.build_paired_result(
        *_synthetic_arms([0.4, -0.5, 0.2, -0.3, 0.1, -0.1]),
        name_a="a", name_b="b", slots=(0,), n_per_slot=6, seed=1, bootstrap=200,
    )
    assert straddling.ci_low < 0.0 < straddling.ci_high
    assert straddling.excludes_zero is False
    assert "did NOT show a difference" in straddling.reasons[2]

    clear = ev.build_paired_result(
        *_synthetic_arms([0.40, 0.42, 0.39, 0.41, 0.43, 0.38]),
        name_a="a", name_b="b", slots=(0,), n_per_slot=6, seed=1, bootstrap=200,
    )
    assert clear.excludes_zero is True
    assert "EXCLUDES zero" in clear.reasons[2]


def _synthetic_arms(deltas):
    """Two aligned outcome grids whose objective differences are ``deltas``."""
    a, b = [], []
    for i, d in enumerate(deltas):
        a.append(
            ev.DraftOutcome(
                slot=0, draft_seed=i, objective=10.0 + d, expected_wins=10.0 + d,
                playoff_prob=0.5, title_prob=0.1, rank=1, holes=0, hole_weeks=(),
                shape={p: 0 for p in ev.SHAPE_POSITIONS},
            )
        )
        b.append(
            ev.DraftOutcome(
                slot=0, draft_seed=i, objective=10.0, expected_wins=10.0,
                playoff_prob=0.5, title_prob=0.1, rank=1, holes=0, hole_weeks=(),
                shape={p: 0 for p in ev.SHAPE_POSITIONS},
            )
        )
    return a, b


def test_the_bootstrap_is_deterministic_and_agrees_with_the_t_interval():
    deltas = [0.4, 0.2, 0.5, 0.3, 0.35, 0.45, 0.25, 0.5, 0.3, 0.4] * 3
    a, b = _synthetic_arms(deltas)
    kw = dict(name_a="a", name_b="b", slots=(0,), n_per_slot=len(deltas), seed=4)
    one = ev.build_paired_result(a, b, bootstrap=500, **kw)
    two = ev.build_paired_result(a, b, bootstrap=500, **kw)
    assert (one.boot_ci_low, one.boot_ci_high) == (two.boot_ci_low, two.boot_ci_high)
    assert one.boot_ci_low < one.mean_delta < one.boot_ci_high
    # On symmetric data the two intervals should be close; a wild disagreement is
    # the signal the reasons tell the reader to act on.
    assert abs(one.boot_ci_low - one.ci_low) < 0.05
    assert abs(one.boot_ci_high - one.ci_high) < 0.05


# =============================================================== 5. refusals


def test_an_empty_points_map_is_refused(smoke):
    board, _weekly = smoke
    with pytest.raises(ev.EvaluationInputError, match="EMPTY"):
        ev.paired_compare(board, {}, a=FollowVor(), b=FollowEspnRank(), n=1, slots=(0,))


def test_a_diverged_id_space_is_refused_at_the_door(smoke):
    """The failure that grades every roster as a season of holes and raises
    nowhere. ``grader.assert_board_coverage`` is wired in as a gate."""
    board, weekly = smoke
    priced = next(e for e in board if e.house_points > 0)
    broken = {k: v for k, v in weekly.items() if k != priced.player_id}
    broken = grader.WeeklyPointsMap(
        broken, positions=weekly.positions, names=weekly.names, teams=weekly.teams
    )
    with pytest.raises(grader.GradeInputError, match="PRICED but missing"):
        ev.paired_compare(board, broken, a=FollowVor(), b=FollowEspnRank(), n=1, slots=(0,))


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (dict(n=0, slots=(0,)), "at least 1"),
        (dict(n=1, slots=()), "slots is empty"),
        (dict(n=1, slots=(0, 0)), "unique"),
        (dict(n=1, slots=(10,)), "ZERO-BASED"),
        (dict(n=1, slots=(-1,)), "ZERO-BASED"),
    ],
)
def test_impossible_designs_are_refused(smoke, kwargs, match):
    board, weekly = smoke
    with pytest.raises(ev.EvaluationInputError, match=match):
        ev.paired_compare(board, weekly, a=FollowVor(), b=FollowEspnRank(), **kwargs)


def test_a_tournament_needs_a_baseline_that_is_in_it(smoke):
    board, weekly = smoke
    strategies = {"vor": FollowVor(), "espn": FollowEspnRank()}
    with pytest.raises(ev.EvaluationInputError, match="not one of the strategies"):
        ev.tournament(board, weekly, strategies=strategies, baseline="nope", n=1, slots=(0,))
    with pytest.raises(ev.EvaluationInputError, match="at least one challenger"):
        ev.tournament(
            board, weekly, strategies={"vor": FollowVor()}, baseline="vor", n=1, slots=(0,)
        )


# ============================================================ 6. determinism


def test_the_same_seed_gives_bit_identical_results(smoke):
    board, weekly = smoke
    kw = dict(a=FollowVor(), b=FollowEspnRank(), n=2, slots=SLOTS, seed=21, bootstrap=100)
    one = ev.paired_compare(board, weekly, **kw)
    two = ev.paired_compare(board, weekly, **kw)
    assert one == two


def test_a_different_seed_gives_a_different_experiment(smoke):
    board, weekly = smoke
    kw = dict(a=FollowVor(), b=FollowEspnRank(), n=2, slots=SLOTS, bootstrap=100)
    one = ev.paired_compare(board, weekly, seed=21, **kw)
    two = ev.paired_compare(board, weekly, seed=22, **kw)
    assert one.deltas != two.deltas


def test_the_global_random_module_cannot_reach_this_harness(smoke):
    board, weekly = smoke
    kw = dict(a=FollowVor(), b=FollowEspnRank(), n=2, slots=(8,), seed=21, bootstrap=100)
    random.seed(1)
    one = ev.paired_compare(board, weekly, **kw)
    random.seed(999999)
    [random.random() for _ in range(1000)]
    two = ev.paired_compare(board, weekly, **kw)
    assert one == two


def test_one_slot_cannot_change_another_slots_draws(smoke):
    """The seed grid is derived per slot, so shortening ``slots`` must leave the
    remaining slots' numbers untouched. A single running stream would not."""
    board, weekly = smoke
    kw = dict(a=FollowVor(), b=FollowEspnRank(), n=3, seed=31, bootstrap=100)
    wide = ev.paired_compare(board, weekly, slots=SLOTS, **kw)
    narrow = ev.paired_compare(board, weekly, slots=(4,), **kw)
    assert wide.per_slot[4] == pytest.approx(narrow.per_slot[4])
    assert wide.per_slot_n[4] == narrow.per_slot_n[4] == 3


def test_slots_are_used_in_the_order_given(smoke):
    board, weekly = smoke
    outs = ev.evaluate_strategy(
        board, weekly, strategy=FollowVor(), n=2, slots=(8, 0), seed=1
    )
    assert [o.slot for o in outs] == [8, 8, 0, 0]


# ============================================================= 7. tournament


def test_a_tournament_pairs_every_challenger_against_the_same_baseline_drafts(smoke):
    board, weekly = smoke
    strategies = {
        "espn": FollowEspnRank(),
        "vor": FollowVor(),
        "vor-chatty": ChattyTwin(),
    }
    t = ev.tournament(
        board, weekly, strategies=strategies, baseline="espn",
        n=2, slots=(0, 8), seed=13, bootstrap=100,
    )
    assert set(t.results) == {"vor", "vor-chatty"}
    assert t.n == 4
    # The baseline arm is the SAME drafts in both pairings, not a fresh sample.
    assert t.results["vor"].mean_b == t.results["vor-chatty"].mean_b
    # ...and the chatty twin drafts identically to plain VOR, so the two
    # challengers must be indistinguishable.
    assert t.results["vor"].mean_delta == t.results["vor-chatty"].mean_delta
    assert set(t.outcomes) == set(strategies)


def test_tournament_order_is_best_first_and_flags_only_clear_winners(smoke):
    board, weekly = smoke
    strategies = {"espn": FollowEspnRank(), "vor": FollowVor(), "off2": ev.OffsetRankPicker(2)}
    t = ev.tournament(
        board, weekly, strategies=strategies, baseline="espn",
        n=3, slots=(0, 8), seed=17, bootstrap=100,
    )
    deltas = [t.results[k].mean_delta for k in t.order]
    assert deltas == sorted(deltas, reverse=True)
    text = ev.format_tournament(t)
    assert "Tournament vs espn" in text
    for name in t.order:
        assert name in text
    assert "espn (baseline)" in text


def test_adding_a_strategy_leaves_the_others_numbers_untouched(smoke):
    board, weekly = smoke
    kw = dict(baseline="espn", n=2, slots=(0, 8), seed=19, bootstrap=100)
    small = ev.tournament(
        board, weekly, strategies={"espn": FollowEspnRank(), "vor": FollowVor()}, **kw
    )
    big = ev.tournament(
        board, weekly,
        strategies={"off3": ev.OffsetRankPicker(3), "espn": FollowEspnRank(), "vor": FollowVor()},
        **kw,
    )
    assert small.results["vor"].deltas == big.results["vor"].deltas


# ================================================== 8. Rule 6 — the reasons


def test_reasons_disclose_what_the_objective_cannot_see(smoke):
    board, weekly = smoke
    r = ev.paired_compare(
        board, weekly, a=FollowVor(), b=FollowEspnRank(), name_a="vor", name_b="espn",
        n=2, slots=SLOTS, seed=23, bootstrap=100,
    )
    joined = " ".join(r.reasons)
    assert "no injury" in joined.lower(), "the objective's headline blind spot must be stated"
    assert "zero-sum" in joined, "the anti-cheat framing must be stated"
    assert "not the real room" in joined, "the opponent model is a model, and must say so"
    assert joined.count("CAVEAT") >= 4
    assert "expected wins" in joined
    # Rule 6: no bare jargon — the numbers a human acts on are in the text.
    assert f"{r.mean_delta:+.3f}" in joined
    assert f"{r.ci_low:+.3f}" in joined
    # ...and the shape/holes diagnosis rides along with the score.
    assert "roster shape" in joined
    assert "could not be filled" in joined


def test_format_paired_result_renders_every_headline_number(smoke):
    board, weekly = smoke
    r = ev.paired_compare(
        board, weekly, a=FollowVor(), b=FollowEspnRank(), name_a="vor", name_b="espn",
        n=2, slots=(8,), seed=29, bootstrap=100,
    )
    text = ev.format_paired_result(r)
    for token in ("vor", "espn", "paired drafts", "mean difference", "bootstrap",
                  "average finish", "unfillable weeks", "shape"):
        assert token in text
    assert ("excludes zero" in text) == r.excludes_zero
    assert len(ev.format_paired_result(r, reasons=False)) < len(text)


# ============================================================== 9. smoke mode


def test_smoke_inputs_are_a_legal_gradeable_board():
    board, weekly = ev.smoke_inputs()
    assert len(board) == 242
    # The grader's own gate: every priced entry must be in the map.
    grader.assert_board_coverage(board, weekly)
    matched, total, _sample = grader.board_key_coverage(board, weekly)
    assert matched == total
    counts = position_counts(board)
    for pos, req in DEFAULT_ROSTER.starters.items():
        assert counts[pos] >= DEFAULT_ROSTER.teams * req


def test_the_smoke_board_obeys_the_missing_week_convention():
    """A week absent means the player does not play it — never 'zero points'."""
    _board, weekly = ev.smoke_inputs()
    for pid, line in weekly.items():
        assert len(line) == 16, f"{pid} should carry 16 of 17 weeks (one bye)"
        missing = set(range(1, 18)) - set(line)
        assert len(missing) == 1
        assert 5 <= missing.pop() <= 14
        assert all(v > 0 for v in line.values()), "no zero-point week may be published"


def test_smoke_mode_is_fast_enough_to_live_in_a_test(smoke):
    """Not a stopwatch race — a guard that the smoke path stays cheap enough
    that a variant author actually runs it."""
    board, weekly = smoke
    r = ev.paired_compare(
        board, weekly, a=ev.OffsetRankPicker(0), b=ev.OffsetRankPicker(1),
        n=2, slots=(0, 8), seed=41, bootstrap=100,
    )
    assert r.n == 4
    assert r.mean_delta != 0.0, "two different stubs should not tie"


def test_offset_rank_picker_is_deterministic_and_ignores_randomness(smoke):
    """``OffsetRankPicker(0)`` is ``FollowEspnRank``, and it must not read the
    rng at all — which is what makes it a clean instrument."""
    board, weekly = smoke
    a = ev.evaluate_strategy(
        board, weekly, strategy=ev.OffsetRankPicker(0), n=1, slots=(8,), seed=2
    )
    b = ev.evaluate_strategy(
        board, weekly, strategy=FollowEspnRank(), n=1, slots=(8,), seed=2
    )
    assert a[0].objective == b[0].objective
    assert a[0].shape == b[0].shape

    class Exploding(random.Random):
        def random(self, *args, **kwargs):  # pragma: no cover - must never fire
            raise AssertionError("OffsetRankPicker must not touch the rng")

        def gauss(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("OffsetRankPicker must not touch the rng")

    ctx = PickContext.from_board(board, rng=Exploding(0), rounds_total=16)
    assert ev.OffsetRankPicker(2).pick(ctx) in {e.player_id for e in board}


# ========================================== 10. the seams a variant will use


def test_a_caller_can_supply_its_own_grade_function(smoke):
    """The ``grade=`` seam: a variant that wants a different objective (playoff
    weight, a different bracket) swaps the grade function, not the harness."""
    board, weekly = smoke
    calls = []

    base = ev.make_grade_fn(weekly, positions=weekly.positions)

    def counting(entries, opponents):
        calls.append(len(entries))
        return base(entries, opponents)

    ev.evaluate_strategy(
        board, weekly, strategy=FollowVor(), n=1, slots=(8,), seed=1,
        grade=counting, field_check=True,
    )
    # ten teams graded against their rivals, plus one fixed-field regrade.
    assert len(calls) == 11


def test_the_objective_weights_reach_grade_roster(smoke):
    board, weekly = smoke
    plain = ev.make_grade_fn(weekly, positions=weekly.positions)
    weighted = ev.make_grade_fn(
        weekly, positions=weekly.positions, objective_playoff_weight=5.0
    )
    outs = ev.evaluate_strategy(
        board, weekly, strategy=FollowVor(), n=1, slots=(8,), seed=1, grade=plain
    )
    outs_w = ev.evaluate_strategy(
        board, weekly, strategy=FollowVor(), n=1, slots=(8,), seed=1, grade=weighted
    )
    assert outs_w[0].objective != outs[0].objective
    assert outs_w[0].expected_wins == outs[0].expected_wins


def test_a_non_default_league_shape_is_honoured(smoke):
    """``roster=`` threads to the room, the draft and the grade, so an eight-team
    experiment really is eight teams."""
    board, weekly = ev.smoke_inputs()
    eight = RosterStructure(
        teams=8,
        starters=DEFAULT_ROSTER.starters,
        flex_slots=DEFAULT_ROSTER.flex_slots,
        flex_positions=DEFAULT_ROSTER.flex_positions,
        bench_slots=DEFAULT_ROSTER.bench_slots,
        ir_slots=DEFAULT_ROSTER.ir_slots,
    )
    grade = ev.make_grade_fn(
        weekly, positions=weekly.positions, roster=eight, playoff_teams=4
    )
    outs = ev.evaluate_strategy(
        board, weekly, strategy=FollowVor(), n=1, slots=(7,), seed=1,
        roster=eight, grade=grade,
    )
    assert 1 <= outs[0].rank <= 8
    # The rendering must say "best of 8", not the ten-team default.
    r = ev.build_paired_result(
        outs, outs, name_a="x", name_b="y", slots=(7,), n_per_slot=1, seed=1,
        bootstrap=10, roster=eight,
    )
    assert r.teams == 8
    assert "1 = best of 8" in ev.format_paired_result(r, reasons=False)
    assert "8-team league" in " ".join(r.reasons)

    with pytest.raises(ev.EvaluationInputError, match="8-team"):
        ev.evaluate_strategy(
            board, weekly, strategy=FollowVor(), n=1, slots=(9,), seed=1,
            roster=eight, grade=grade,
        )


def test_the_deltas_are_kept_so_a_caller_can_re_analyse(smoke):
    board, weekly = smoke
    r = ev.paired_compare(
        board, weekly, a=FollowVor(), b=FollowEspnRank(), n=3, slots=SLOTS, seed=37,
        bootstrap=100,
    )
    assert len(r.deltas) == r.n == 9
    assert statistics.fmean(r.deltas) == pytest.approx(r.mean_delta)
    assert sum(1 for d in r.deltas if d > 0) / r.n == pytest.approx(r.win_rate)
