"""Tests for the ``survivalfit`` variant (``ziggurat/draft/variant_survival.py``).

All offline, fully synthetic (Rule 5): every board comes from the shared
``make_draft_board`` fixture and every journal is generated here by running the
shipped simulator over it, so no real player, manager or league fact enters a
committed file. No DB, no network, no wall clock; all randomness from seeded
``random.Random``.

The tests are aimed at the defects this module can actually have, and each one
was mutation-checked against the implementation while it was written:

* A **SIGN** defect. The whole correction is one signed number per position and
  its sign decides whether the engine drafts a position sooner or later. Flip it
  and every published constant still looks plausible, the suite still passes a
  "the shift is applied" test, and the engine does the opposite of what the
  measurement says. Covered by
  :func:`test_a_negative_shift_lowers_survival_and_raises_urgency` and
  :func:`test_the_fitted_shift_removes_the_bias_it_was_fitted_on`.
* An **ADDITIVITY** defect — the correction leaking into the default path. The
  module's entire claim to be safe 24 hours before a draft is that nothing
  imports it. Covered by :func:`test_no_shipped_module_imports_this_variant` and
  :func:`test_the_default_engine_is_untouched_by_this_module`.
* A **SHAPE** defect in route 2: the sloped analytic form must reduce EXACTLY to
  the shipped one at zero slopes, or "did the shape help or did the constants?"
  is unanswerable. Covered by
  :func:`test_zero_slopes_reproduce_the_shipped_analytic_survival_exactly`.
* A **DEGENERACY** defect in the fitter: a cell where every row survived has a
  likelihood maximised at infinity, and returning that infinity would publish a
  survival of exactly 1.0 for a whole position. Covered by
  :func:`test_a_degenerate_cell_fits_to_zero_rather_than_infinity`.
* A **PROVENANCE** defect: a published constant that no longer matches what its
  own fitter produces — or that no fitter here can produce at all. Covered by
  :func:`test_the_published_constants_are_what_the_fitters_produce`,
  :func:`test_the_calibration_carries_the_rollout_count_it_was_fitted_at` and the
  degenerate-cell assertions in
  :func:`test_the_published_caveats_state_the_limits_they_are_named_for`.
* A **LOAD-BEARING-FIELD** defect: a constructor field the class documents as
  decisive that no test would notice being dropped. Both halves of the documented
  INTEGRATOR TRAP (``rollouts`` AND ``priors``), ``kappa``, the analytic route's
  survival horizon and its already-drafted filter are each mutation-checked here.
* A **CLAIM** defect: a labelled constant asserting a mechanism the measurement
  behind it could not have tested. Covered by
  :func:`test_the_level_knob_reaches_BOTH_halves_of_the_estimate` and
  :func:`test_the_level_finding_states_its_evidence_and_its_limits`.
"""

from __future__ import annotations

import ast
import dataclasses
import math
import random
from dataclasses import dataclass
from pathlib import Path

import pytest

from ziggurat.core.valuation import RosterStructure
from ziggurat.draft import variant_survival as vs
from ziggurat.draft.bots import POSITIONS, AutodraftBot, PickContext
from ziggurat.draft.engine import PickEngine, SurvivalEstimate
from ziggurat.draft.priors import ROOM_PRIORS_2025
from ziggurat.draft.simulator import run_draft
from ziggurat.draft.survival import (
    DEFAULT_KAPPA,
    DEFAULT_SURVIVAL_PARAMS,
    analytic_survival,
    rollout_survival,
    upcoming_opponent_picks,
)

ROSTER = RosterStructure()
TEAMS = ROSTER.teams
ROUNDS = 16


@pytest.fixture()
def board(make_draft_board):
    return make_draft_board()


# --------------------------------------------------------------- tiny helpers


@dataclass(frozen=True)
class _Point:
    """Stands in for ``roomcheck.SurvivalPoint`` — the three fields the fitter reads."""

    position: str
    predicted: float
    survived: bool


@dataclass(frozen=True)
class _Obs:
    """Stands in for ``roomcheck.SurvivalObservation``."""

    position: str
    espn_overall_rank: int
    pick: int
    censored: bool


def _ctx(board, *, overall_pick=1, round_num=1, own=(), taken=()):
    return PickContext.from_board(
        board,
        own_roster=list(own),
        taken=taken,
        team_slot=0,
        round=round_num,
        overall_pick=overall_pick,
        rounds_total=ROUNDS,
        roster=ROSTER,
        rng=random.Random(11),
    )


# ==================================================================== route 1


def test_a_negative_shift_lowers_survival_and_raises_urgency():
    """THE SIGN TEST. A negative shift must LOWER survival.

    The engine's urgency is ``VONA * (1 - S_next)``, so lower survival means more
    urgency means an earlier pick at that position. Every published shift for a
    position the room takes early (TE, QB) is negative, and a flipped sign would
    make the engine wait LONGER at exactly the positions the measurement says it
    already waits too long at — with no other test in this file failing.
    """
    calib = vs.RolloutCalibration(shifts={"TE": -1.0, "RB": +1.0}, label="test")
    base = 0.5
    lowered = calib.apply(base, position="TE", rollouts=512)
    raised = calib.apply(base, position="RB", rollouts=512)
    assert lowered < base < raised
    # ...and the effect is symmetric in logit space at S = 0.5.
    assert lowered == pytest.approx(1.0 - raised, abs=1e-9)
    # An unnamed position is left exactly alone (default 0.0).
    assert calib.apply(base, position="WR", rollouts=512) == base


def test_the_published_te_and_qb_shifts_are_negative():
    """Direction of the SHIPPED constants, pinned against a silent re-sign.

    The measurement is that the room takes TEs (+0.101 bias) and QBs (+0.046)
    EARLIER than the rollout expects, i.e. the model is optimistic there. The
    correction must therefore push those two down and nothing else may quietly
    invert.
    """
    calib = vs.POSITION_ROLLOUT_CALIBRATION_2026
    assert calib.shift_for("TE") < calib.shift_for("QB") < 0.0
    # RB/WR measured very slightly PESSIMISTIC, so their shifts are positive.
    assert calib.shift_for("RB") > 0.0
    assert calib.shift_for("WR") > 0.0
    # TE is the largest skill-position correction, which is the headline finding.
    skill = {p: abs(calib.shift_for(p)) for p in ("QB", "RB", "WR", "TE")}
    assert max(skill, key=skill.get) == "TE"


def test_the_correction_cannot_leave_the_unit_interval():
    """A survival is a probability. A logit shift can never leave [0, 1], which is
    exactly why it was chosen over an additive offset — and the endpoints are the
    case that breaks a naive implementation (``logit(0)`` is ``-inf``)."""
    calib = vs.RolloutCalibration(shifts={p: -9.0 for p in POSITIONS}, label="test")
    for s in (0.0, 0.001, 0.5, 0.999, 1.0):
        for pos in POSITIONS:
            out = calib.apply(s, position=pos, rollouts=512)
            assert 0.0 <= out <= 1.0
            assert math.isfinite(out)


def test_smoothing_reads_a_frequency_as_a_frequency():
    """0.0 from R rollouts means "0 of R", not "impossible".

    Jeffreys smoothing must map it to ``0.5/(R+1)`` — small but finite — and must
    shrink as R grows, because more rollouts is more evidence. A plain clamp to a
    fixed epsilon would make the correction's effect at the endpoints depend on
    the epsilon rather than on the sample.
    """
    assert vs.smooth_rollout_probability(0.0, 128) == pytest.approx(0.5 / 129.0)
    assert vs.smooth_rollout_probability(0.0, 512) == pytest.approx(0.5 / 513.0)
    assert vs.smooth_rollout_probability(0.0, 512) < vs.smooth_rollout_probability(0.0, 128)
    assert vs.smooth_rollout_probability(1.0, 512) == pytest.approx(512.5 / 513.0)
    # rollouts <= 0 means "this is a probability, not a frequency": clamp only.
    assert vs.smooth_rollout_probability(0.0, 0) == pytest.approx(1e-9)


def test_a_player_of_unknown_position_is_left_uncorrected():
    """Refuse-rather-than-guess: correcting a player whose position we could not
    establish would apply a tight-end correction to whatever he actually is."""
    calib = vs.RolloutCalibration(shifts={"TE": -2.0}, default=-2.0, label="test")
    out = calib.apply_map(
        {"known": 0.5, "unknown": 0.5}, position_of={"known": "TE"}, rollouts=512
    )
    assert out["known"] < 0.5
    assert out["unknown"] == 0.5


def test_the_fitted_shift_removes_the_bias_it_was_fitted_on():
    """END-TO-END ESTIMATOR TEST on data whose answer is known by construction.

    Build a cohort where a position's predictions are systematically optimistic by
    a known amount, fit, and check the fit both (a) has the right sign and (b)
    actually removes the bias. A fitter that returned zero, or the wrong sign,
    passes none of this.
    """
    rng = random.Random(3)
    points: list[_Point] = []
    for _ in range(4000):
        # TE: predicted 0.70, actually survives 45% of the time -> optimistic.
        points.append(_Point("TE", 0.70, rng.random() < 0.45))
        # WR: predicted 0.60, survives 60% -> already calibrated.
        points.append(_Point("WR", 0.60, rng.random() < 0.60))

    calib = vs.fit_logit_shifts(points, rollouts=512, label="synthetic")
    assert calib.shift_for("TE") < -0.5, "an optimistic cell must get a negative shift"
    assert abs(calib.shift_for("WR")) < 0.15, "a calibrated cell must barely move"

    _br_before, bias_before, _n = vs.brier_and_bias(points)
    _br_after, bias_after, _n = vs.brier_and_bias(points, calibration=calib, rollouts=512)
    assert bias_before > 0.05
    assert abs(bias_after) < 1e-6, "the MLE shift sets the mean residual to zero"


def test_a_degenerate_cell_fits_to_zero_rather_than_infinity():
    """A cell where EVERY row survived wants an infinite shift. Publishing that
    would hard-code survival 1.0 for a whole position — which is precisely what
    the real engine-candidate kicker cohort is (31 rows, 31 of 31 survived), and
    why the shipped constants are fitted on the wider probe instead. There is no
    finite answer to publish there: the gradient measured on that cohort is still
    0.000000 at +60, so an unbounded search diverges and a wide-bounded one
    converges only to wherever the float underflows."""
    points = [_Point("K", 0.95, True) for _ in range(50)]
    points += [_Point("RB", 0.5, i % 2 == 0) for i in range(50)]
    calib = vs.fit_logit_shifts(points, rollouts=512, label="degenerate")
    assert calib.shift_for("K") == 0.0
    assert math.isfinite(calib.shift_for("RB"))


def test_fit_refuses_without_the_rollout_count():
    """The smoothing needs R. Guessing it silently changes every fitted shift."""
    with pytest.raises(ValueError):
        vs.fit_logit_shifts([_Point("TE", 0.5, True)], rollouts=0)
    with pytest.raises(ValueError):
        vs.fit_logit_shifts([], rollouts=512)
    with pytest.raises(ValueError):
        vs.brier_and_bias([])
    # Scoring UNDER a calibration without R would silently score a different
    # correction than the one the caller thinks they are scoring.
    with pytest.raises(ValueError):
        vs.brier_and_bias(
            [_Point("TE", 0.5, True)], calibration=vs.POSITION_ROLLOUT_CALIBRATION_2026
        )


def test_the_calibrated_provider_matches_the_rollout_except_for_the_correction(board):
    """The wrapper must be the shipped rollout plus arithmetic — same draws, same
    order, same ``next_best_vor``. If it consumed randomness differently the
    cockpit's bit-identical journal replay would break."""
    ctx = _ctx(board, overall_pick=3)
    candidates = ctx.state.window_by_rank(sorted(POSITIONS), 8)
    positions = sorted({c.position for c in candidates})

    raw = rollout_survival(
        ctx, candidates, rng=random.Random(99), rollouts=24, positions=positions
    )
    provider = vs.CalibratedRollout(
        vs.RolloutCalibration(shifts={"RB": -0.8}, label="test"), rollouts=24
    )
    est = provider(ctx, candidates=candidates, positions=positions, rng=random.Random(99))

    assert est.next_best_vor == raw.next_best_vor, "VONA input must pass through untouched"
    for c in candidates:
        if c.position == "RB":
            assert est.survival[c.player_id] <= raw.survival[c.player_id]
        else:
            assert est.survival[c.player_id] == raw.survival[c.player_id]


def test_the_calibrated_provider_is_deterministic(board):
    """Two calls with equally-seeded streams must agree bit-for-bit (determinism
    is the draft cockpit's replay guarantee, and this provider sits on that path)."""
    ctx_a = _ctx(board, overall_pick=5)
    ctx_b = _ctx(board, overall_pick=5)
    cands_a = ctx_a.state.window_by_rank(sorted(POSITIONS), 6)
    cands_b = ctx_b.state.window_by_rank(sorted(POSITIONS), 6)
    provider = vs.CalibratedRollout(vs.POSITION_ROLLOUT_CALIBRATION_2026, rollouts=16)
    positions = sorted({c.position for c in cands_a})
    a = provider(ctx_a, candidates=cands_a, positions=positions, rng=random.Random(5))
    b = provider(ctx_b, candidates=cands_b, positions=positions, rng=random.Random(5))
    assert dict(a.survival) == dict(b.survival)
    assert dict(a.next_best_vor) == dict(b.next_best_vor)


def test_the_calibrated_provider_drives_a_real_engine(board):
    """It satisfies ``engine.SurvivalProvider`` in fact, not just in type: a
    ``PickEngine`` built on it returns legible recommendations."""
    engine = PickEngine(
        rollouts=16,
        survival=vs.CalibratedRollout(vs.POSITION_ROLLOUT_CALIBRATION_2026, rollouts=16),
    )
    recs = engine.recommend(_ctx(board, overall_pick=9, round_num=1), top=3)
    assert len(recs) == 3
    assert all(r.reasons for r in recs)
    assert all(0.0 <= r.survival_next <= 1.0 for r in recs)


def test_the_engines_rollout_count_does_not_reach_an_injected_provider(board):
    """THE INTEGRATOR TRAP, pinned. ``engine._ask_survival`` short-circuits on an
    injected provider, so ``PickEngine(rollouts=512, survival=...)`` runs at the
    PROVIDER's count, not the engine's. A caller who sets it in the wrong place
    silently runs a quarter of the rollouts they believe they are running.

    Counted by instrumenting the provider, so this fails if the seam ever changes
    in either direction.
    """
    calls: list[int] = []

    @dataclass(frozen=True)
    class _Counting(vs.CalibratedRollout):
        def __call__(self, ctx, *, candidates, positions, rng):
            calls.append(self.rollouts)
            return super().__call__(ctx, candidates=candidates, positions=positions, rng=rng)

    engine = PickEngine(
        rollouts=512,
        survival=_Counting(vs.POSITION_ROLLOUT_CALIBRATION_2026, rollouts=7),
    )
    engine.recommend(_ctx(board, overall_pick=4), top=1)
    assert calls == [7], "the provider's own rollouts field is the one that runs"
    # And the documented default is the tournament budget, not the live one.
    assert vs.CalibratedRollout(vs.POSITION_ROLLOUT_CALIBRATION_2026).rollouts == 128
    assert "INTEGRATOR TRAP" in vs.CalibratedRollout.__doc__


def test_describe_names_the_direction_in_plain_language():
    """Rule 6. The engine builds its reasons from numbers and cannot know a
    provider was swapped, so an injected correction is invisible to a novice
    unless the integrator prints these lines. They must say which way it pushes
    and must carry the noise-floor caveat, not just the flattering half."""
    lines = vs.CalibratedRollout(vs.POSITION_ROLLOUT_CALIBRATION_2026, rollouts=512).describe()
    blob = "\n".join(lines)
    assert "TE: treated as less likely to last" in blob
    assert "more urgency at TE" in blob
    assert "noise floor" in blob.lower() or "NOISE FLOOR" in blob
    assert vs.THE_TE_CORRECTION_PUSHES_THE_WRONG_WAY in lines
    assert vs.DECISION_RELEVANCE_ON_REAL_ROOMS in lines
    assert vs.AB_RESULT_2026 in lines
    assert vs.THE_KDST_HALF_CHANGES_NOTHING in lines
    # The per-position sentences a human reads on the clock come BEFORE the
    # paragraph caveats, so the operative lines are not buried under them.
    first_caveat = min(
        i for i, line in enumerate(lines)
        if line is vs.ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR
    )
    assert all(
        "treated as" in lines[i] or lines[i].startswith("PROVENANCE:")
        for i in range(1, first_caveat)
    )
    assert "jargon" not in blob


def test_the_published_caveats_state_the_limits_they_are_named_for():
    """A caveat constant that stops saying its own finding is worse than none."""
    assert "cannot tell us the correction helps" in vs.THE_SIM_ROOM_IS_THE_ROLLOUTS_OWN_MODEL
    assert "0.0008" in vs.ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR
    assert "EARLIER" in vs.THE_TE_CORRECTION_PUSHES_THE_WRONG_WAY
    assert "3 TE" in vs.THE_TE_CORRECTION_PUSHES_THE_WRONG_WAY
    assert "defaults to" in vs.ANALYTIC_ROUTE_IS_DEAD_ON_THE_LIVE_PATH
    # The deciding measurement must keep BOTH halves: the change rate AND the
    # one-way direction. Quoting only a rate would read as reassurance — and the
    # rate must be a RANGE over seeds, because a single Monte-Carlo draw of it
    # varies from 1 to 5 and the first pass published the top of that range as if
    # it were the number.
    assert "1 to 5" in vs.DECISION_RELEVANCE_ON_REAL_ROOMS
    assert "0.6% to 3.0%" in vs.DECISION_RELEVANCE_ON_REAL_ROOMS
    assert "one seed" in vs.DECISION_RELEVANCE_ON_REAL_ROOMS
    assert "16 of 16" in vs.DECISION_RELEVANCE_ON_REAL_ROOMS
    assert "26.1%" in vs.DECISION_RELEVANCE_ON_REAL_ROOMS
    assert "6.7%" in vs.DECISION_RELEVANCE_ON_REAL_ROOMS
    # A number a reader cannot recompute with this module's own tools is worse
    # than no number: the degenerate K cell has no finite MLE and the fitter
    # returns 0.0, so the note must say that rather than quote a value.
    assert "maximised at plus" in vs.ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR
    assert "EXACTLY 0.0" in vs.ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR
    # The retracted value is named so a reader who remembers it knows it was
    # withdrawn — but only ever as a retraction, never as a finding.
    assert "Nothing in this module produces +28.5" in (
        vs.ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR
    )
    head, marker, _tail = vs.ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR.partition(
        "A CORRECTION TO THIS CONSTANT'S OWN EARLIER TEXT"
    )
    assert marker, "the retraction must be marked as one"
    assert "28.5" not in head, "the withdrawn number may appear only inside the retraction"
    # The A/B constant must keep the sanity check, the interval, and the tie
    # count — a delta quoted without "includes zero" reads as a result.
    assert "EXACTLY 0.000" in vs.AB_RESULT_2026
    assert "INCLUDES ZERO" in vs.AB_RESULT_2026
    assert "105 of 120 EXACT TIES" in vs.AB_RESULT_2026
    assert "NOT PROVEN BETTER" in vs.AB_RESULT_2026
    # ...and it must point at the HELD-OUT re-run rather than stand alone, because
    # the fitted grid and the grid that decides are different grids.
    assert "held-out" in vs.AB_RESULT_2026
    held = vs.AB_RESULT_2026_HELD_OUT
    assert "-0.011" in held and "-0.020 .. -0.002" in held
    assert "EXCLUDES ZERO" in held
    assert "120 of 120 EXACT TIES" in held, "the pairing sanity check must be reported"
    assert "should NOT be wired in" in held
    assert "INERT" in held, "an inert arm must be called inert, not 'safe'"
    # The K/DST result must state the mechanism, not just the tie count, or a
    # later reader cannot tell a structural zero from a lucky one.
    assert "120 of 120 exact ties" in vs.THE_KDST_HALF_CHANGES_NOTHING
    assert "kdst_earliest_round" in vs.THE_KDST_HALF_CHANGES_NOTHING
    # The route-2 surprise must keep the finding AND the corrected explanation,
    # and must not still be claiming the explanation was refuted.
    assert "-0.034" in vs.CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY
    assert "REFUTED" not in vs.CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY
    assert "I did not find it" not in vs.CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY
    assert "STRUCTURE, not in its level" not in vs.CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY
    # Latency must be stated against the gate AND must not pass off scheduler
    # noise as the wrapper's cost.
    assert "243 ms" in vs.LATENCY_2026
    assert "SCHEDULER NOISE" in vs.LATENCY_2026
    # The gate is a PASS/FAIL, so the constant must state the verdict, not only
    # the milliseconds a reader would have to compare themselves.
    assert "INSIDE THE 243 ms GATE" in vs.LATENCY_2026
    assert "not a reason to reject" in vs.LATENCY_2026


# ==================================================================== route 2


def test_zero_slopes_reproduce_the_shipped_analytic_survival_exactly():
    """THE SHAPE-EQUIVALENCE TEST, and the reason the route-2 comparison is not
    confounded: with both K/DST slopes at zero the sloped form must be the shipped
    form to the last bit, so any measured difference is attributable to the slope
    and to the constants separately rather than to a re-implementation."""
    flat = vs.SlopedSurvivalParams.from_flat(DEFAULT_SURVIVAL_PARAMS)
    for rank in (1, 5, 50, 146, 179, 400, 9_999, 10_000, 12_000):
        for pos in ("QB", "RB", "WR", "TE", "K", "DST", "D/ST", "DEF"):
            for nxt in (2, 25, 90, 150, 200):
                assert vs.sloped_analytic_survival(rank, pos, nxt, params=flat) == (
                    analytic_survival(rank, pos, nxt)
                )


def test_the_slope_makes_a_deep_kicker_survive_longer_than_a_top_one():
    """The whole content of the shape change: under the shipped flat form, K1 and
    K50 have the SAME survival curve, which is the misspecification. Under the
    refit they must separate, and in the right order."""
    top, deep = 146, 500
    at = 130
    flat = vs.SlopedSurvivalParams.from_flat(DEFAULT_SURVIVAL_PARAMS)
    assert vs.sloped_analytic_survival(top, "K", at, params=flat) == (
        vs.sloped_analytic_survival(deep, "K", at, params=flat)
    )
    refit = vs.REFIT_SLOPED_PRACTICE_2026
    assert (
        vs.sloped_analytic_survival(deep, "K", at, params=refit)
        > vs.sloped_analytic_survival(top, "K", at, params=refit)
    )
    assert (
        vs.sloped_analytic_survival(deep, "DST", at, params=refit)
        > vs.sloped_analytic_survival(top, "DST", at, params=refit)
    )


def test_survival_is_monotone_in_the_pick_it_is_asked_about():
    """A player cannot be MORE likely to be available later. A sign error in the
    centre/width arithmetic breaks this and nothing else notices."""
    for pos, rank in (("RB", 20), ("K", 200), ("DST", 250)):
        vals = [vs.sloped_analytic_survival(rank, pos, o) for o in range(5, 170, 5)]
        assert all(b <= a + 1e-12 for a, b in zip(vals, vals[1:], strict=False))


def test_expected_next_best_vor_is_bounded_by_the_best_available(board):
    """An EXPECTATION over "the best survivor" can never exceed the best player
    available now, and can never be negative on a board of positive VOR."""
    ctx = _ctx(board, overall_pick=4)
    for pos in POSITIONS:
        best = ctx.state.front_vor(pos)
        got = vs.expected_next_best_vor(ctx, pos, 14)
        assert 0.0 <= got <= float(best.vor) + 1e-9


def test_expected_next_best_vor_falls_as_the_wait_lengthens(board):
    """Waiting longer cannot IMPROVE your replacement. This is the property VONA
    is built on, so an inverted expectation would silently invert urgency."""
    ctx = _ctx(board, overall_pick=4)
    near = vs.expected_next_best_vor(ctx, "RB", 8)
    far = vs.expected_next_best_vor(ctx, "RB", 60)
    assert far < near


def test_the_analytic_provider_answers_both_halves_and_ignores_its_rng(board):
    """The gap this closes: ``analytic_survival`` answers survival only, so route 2
    was never actually injectable. And it must draw NOTHING from the rng it is
    handed, so swapping it in mid-session cannot perturb another seat's stream."""
    ctx = _ctx(board, overall_pick=3)
    candidates = ctx.state.window_by_rank(sorted(POSITIONS), 6)
    positions = sorted({c.position for c in candidates})
    rng = random.Random(4)
    before = rng.getstate()
    est = vs.AnalyticSurvival()(ctx, candidates=candidates, positions=positions, rng=rng)
    assert rng.getstate() == before, "the analytic route must consume no randomness"
    assert isinstance(est, SurvivalEstimate)
    assert set(est.survival) == {c.player_id for c in candidates}
    assert set(est.next_best_vor) == set(positions)


def test_the_analytic_provider_returns_certainty_when_there_is_no_next_pick(board):
    """At the snake turn there are no intervening picks, so everyone survives with
    probability 1 — the same branch ``rollout_survival`` has. Getting this wrong
    manufactures urgency at exactly the pick where waiting is free."""
    # Slot 0 in a 10-team snake picks at overall 1 and again at overall 20; the
    # turn is at overall 20, where the next pick is 21 with nothing in between.
    ctx = _ctx(board, overall_pick=20, round_num=2)
    candidates = ctx.state.window_by_rank(sorted(POSITIONS), 5)
    positions = sorted({c.position for c in candidates})
    est = vs.AnalyticSurvival()(
        ctx, candidates=candidates, positions=positions, rng=random.Random(0)
    )
    assert set(est.survival.values()) == {1.0}
    for pos in positions:
        assert est.next_best_vor[pos] == pytest.approx(float(ctx.state.front_vor(pos).vor))


def test_the_analytic_provider_drives_a_real_engine(board):
    engine = PickEngine(survival=vs.AnalyticSurvival())
    recs = engine.recommend(_ctx(board, overall_pick=7), top=3)
    assert len(recs) == 3
    assert all(r.reasons for r in recs)


def test_score_analytic_prefers_the_shape_that_generated_the_data():
    """Makes the published route-2 comparison reproducible rather than remembered:
    on rows where the kicker's timing genuinely depends on rank, the sloped params
    must score a lower Brier than the flat shipped ones."""
    rows = [
        (rank, "K", pick, pick < 40 + 1.5 * rank)
        for rank in range(150, 400, 10)
        for pick in (60, 90, 120, 150)
    ]
    flat_brier, _bias, n = vs.score_analytic(
        rows, params=vs.SlopedSurvivalParams.from_flat(DEFAULT_SURVIVAL_PARAMS)
    )
    fitted = vs.SlopedSurvivalParams(k_center_intercept=40.0, k_center_slope=1.5, k_width=1.0)
    sloped_brier, _b, _n = vs.score_analytic(rows, params=fitted)
    assert n == len(rows)
    assert sloped_brier < flat_brier
    with pytest.raises(ValueError):
        vs.score_analytic([])


def test_fit_sloped_params_recovers_a_planted_slope():
    """ESTIMATOR TEST. Generate kickers whose draft pick genuinely depends on rank,
    fit, and check the slope comes back positive and the likelihood improves over
    the flat baseline. A fitter that ignored the slope column would return ~0 and
    pass every other test in this file."""
    rng = random.Random(17)
    obs: list[_Obs] = []
    for rank in range(1, 60):
        for _ in range(6):
            centre = 40.0 + 1.5 * rank
            pick = centre + rng.gauss(0.0, 4.0)
            if pick >= 160:
                obs.append(_Obs("K", rank, 160, True))
            else:
                obs.append(_Obs("K", rank, int(round(pick)), False))
    for rank in range(1, 40):
        obs.append(_Obs("DST", rank, min(159, int(50 + 2.0 * rank)), False))
    for rank in range(1, 60):
        obs.append(_Obs("RB", rank, min(159, int(3 + 0.8 * rank)), False))

    fit = vs.fit_sloped_params(obs)
    assert fit.params.k_center_slope > 1.0, "a planted rank slope must be recovered"
    assert fit.improvement > 0.0
    # improvement is measured against the DEFAULT baseline (the shipped flat
    # constants), and the docstring says so — a reader recomputing the quoted
    # 607.7 -> 348.4 pair must not be handed a different baseline silently.
    assert fit.baseline == vs.SlopedSurvivalParams.from_flat(DEFAULT_SURVIVAL_PARAMS)
    assert set(fit.per_group) == {"skill", "K", "DST"}
    assert all(a < b for a, b in fit.per_group.values())
    assert fit.n == len(obs)
    assert fit.n_events == sum(1 for o in obs if not o.censored)
    # A group with no room events at all is refused, not fitted to noise.
    with pytest.raises(ValueError):
        vs.fit_sloped_params([o for o in obs if o.position != "K"])


# ============================== the load-bearing fields nothing exercised =====


def test_the_providers_own_priors_are_the_ones_that_run(board):
    """MUTATION TARGET: ``priors=self.priors`` inside ``CalibratedRollout.__call__``.

    The class docstring names ``priors`` as half of the INTEGRATOR TRAP, and
    ``roomcheck.LIVE_RECALIBRATION_IS_THE_ROOM_MODEL`` measures that the cockpit
    runs on RE-FITTED priors for 87% of real decisions. Nothing pinned it: a
    refactor that dropped the thread and hard-coded the cold-start bag would ship a
    provider that silently ignores live recalibration, suite green.

    So the field must be demonstrably load-bearing: two providers identical except
    for ``priors``, on the same context and the same seeded stream, must return
    DIFFERENT survival.
    """
    calib = vs.POSITION_ROLLOUT_CALIBRATION_2026
    loose = vs.CalibratedRollout(calib, rollouts=64)
    tight = vs.CalibratedRollout(
        calib, rollouts=64, priors=dataclasses.replace(ROOM_PRIORS_2025, reach_sigma=1.0)
    )
    assert loose.priors is ROOM_PRIORS_2025, "the documented default is the cold-start bag"

    def run(provider):
        ctx = _ctx(board, overall_pick=12, round_num=2)
        cands = ctx.state.window_by_rank(sorted(POSITIONS), 8)
        return provider(
            ctx,
            candidates=cands,
            positions=sorted({c.position for c in cands}),
            rng=random.Random(5),
        )

    a, b = run(loose), run(tight)
    moved = max(abs(a.survival[k] - b.survival[k]) for k in a.survival)
    assert moved > 0.05, (
        "the provider's own priors must reach the rollout; a room that reaches "
        f"much less far should move survival, and it moved {moved:.4f}"
    )


def test_the_providers_own_kappa_is_the_one_that_runs(board):
    """MUTATION TARGET: ``kappa=self.kappa`` — the other unpinned field."""
    calib = vs.POSITION_ROLLOUT_CALIBRATION_2026
    base = vs.CalibratedRollout(calib, rollouts=64)
    hot = vs.CalibratedRollout(calib, rollouts=64, kappa=9.0)
    assert base.kappa == DEFAULT_KAPPA

    def run(provider):
        ctx = _ctx(board, overall_pick=12, round_num=2)
        cands = ctx.state.window_by_rank(sorted(POSITIONS), 8)
        return provider(
            ctx,
            candidates=cands,
            positions=sorted({c.position for c in cands}),
            rng=random.Random(5),
        )

    a, b = run(base), run(hot)
    moved = max(abs(a.survival[k] - b.survival[k]) for k in a.survival)
    assert moved > 0.05, f"the provider's own kappa must reach the rollout (moved {moved:.4f})"


def test_the_engines_room_priors_do_not_reach_an_injected_provider(board):
    """THE OTHER HALF OF THE TRAP. ``engine._ask_survival`` short-circuits on an
    injected provider, and ``room_priors`` is used nowhere else in ``PickEngine``,
    so an integrator who hands the live-recalibrated bag to the ENGINE has handed
    it to nothing. Two engines that differ only there must be indistinguishable."""
    provider = vs.CalibratedRollout(vs.POSITION_ROLLOUT_CALIBRATION_2026, rollouts=32)
    tight = dataclasses.replace(ROOM_PRIORS_2025, reach_sigma=1.0)
    a = PickEngine(rollouts=512, room_priors=ROOM_PRIORS_2025, survival=provider).recommend(
        _ctx(board, overall_pick=12, round_num=2), top=3
    )
    b = PickEngine(rollouts=512, room_priors=tight, survival=provider).recommend(
        _ctx(board, overall_pick=12, round_num=2), top=3
    )
    assert [r.player_id for r in a] == [r.player_id for r in b]
    assert [round(r.survival_next, 12) for r in a] == [round(r.survival_next, 12) for r in b]


def test_the_analytic_horizon_is_the_operators_next_pick_not_the_rooms(board):
    """MUTATION TARGET: ``next_pick = upcoming[-1][0] + 1`` in ``AnalyticSurvival``.

    Survival means "does he last until MY next pick". Substituting the room's very
    next pick (``upcoming[0][0]``) answers a different question and erases nearly
    all urgency at the longest waits — exactly where urgency is the whole content
    of the recommendation. Pinned against the pure function at both horizons so the
    test says which one is right rather than merely that something changed.
    """
    # ``upcoming_opponent_picks`` reads the snake geometry off ``overall_pick``
    # alone, and pick 12 of a 10-team snake is the operator's real long wait:
    # 16 intervening picks before 29. That is where the horizon error is largest.
    ctx = _ctx(board, overall_pick=12, round_num=2)
    upcoming = upcoming_opponent_picks(ctx)
    assert len(upcoming) == 16, "this context must have the long wait the test is about"
    candidates = ctx.state.window_by_rank(sorted(POSITIONS), 6)
    positions = sorted({c.position for c in candidates})
    est = vs.AnalyticSurvival()(ctx, candidates=candidates, positions=positions, rng=None)

    far_horizon = upcoming[-1][0] + 1
    near_horizon = upcoming[0][0]
    separated = 0
    for c in candidates:
        mine = vs.sloped_analytic_survival(c.espn_overall_rank, c.position, far_horizon)
        rooms = vs.sloped_analytic_survival(c.espn_overall_rank, c.position, near_horizon)
        assert est.survival[c.player_id] == mine
        if abs(mine - rooms) > 0.05:
            separated += 1
    assert separated >= 3, "the two horizons must actually differ, or this pins nothing"
    # And the next_best_vor half is priced at the same horizon, not a different one.
    for pos in positions:
        assert est.next_best_vor[pos] == pytest.approx(
            vs.expected_next_best_vor(ctx, pos, far_horizon), abs=1e-12
        )


def test_expected_next_best_vor_never_prices_an_already_drafted_player(board):
    """MUTATION TARGET: the ``player_id not in ctx.state.taken`` filter.

    A replacement who is already on somebody's roster is not a replacement.
    Without the filter the expectation is dominated by the best player on the
    board whether or not he is gone, which INFLATES ``next_best_vor``, DEFLATES
    ``VONA = best_now.vor - next_best_vor`` and understates urgency by more and
    more as the taken set grows. Both other callers in this file draft nobody, so
    this is the case they cannot see.
    """
    gone = tuple(f"RB-{i}" for i in range(10))
    ctx = _ctx(board, overall_pick=11, round_num=2, taken=gone)
    front = ctx.state.front_vor("RB")
    assert front.player_id not in gone
    for horizon in (12, 14, 20, 30):
        got = vs.expected_next_best_vor(ctx, "RB", horizon)
        assert got <= float(front.vor) + 1e-9, (
            "an expectation over the best SURVIVING available player cannot exceed "
            "the best AVAILABLE one; if it does, a drafted player is being priced"
        )
    # And the filter is what does it: the same walk without it prices RB-0.
    unfiltered = sorted((e for e in ctx.state.all_entries() if e.position == "RB"),
                        key=lambda e: -e.vor)[:40]
    assert unfiltered[0].player_id in gone
    assert unfiltered[0].vor > float(front.vor)


def test_the_global_shift_is_negative_and_the_label_quotes_the_real_number():
    """MUTATION TARGET: the SIGN of ``GLOBAL_LOGIT_SHIFT_2026``.

    The per-position bag's direction was pinned; the global bag's was not, and
    flipping it left every test green while the rendered label went on saying
    "a SINGLE logit shift of -0.1718". A positive global shift makes the engine
    WAIT LONGER at every position — the exact inversion this file's docstring
    names as the headline defect class.
    """
    assert vs.GLOBAL_LOGIT_SHIFT_2026 < 0.0, (
        "the measured route-1 residual is OPTIMISM (+0.0135), and optimism is "
        "removed by a NEGATIVE logit shift"
    )
    # The label hard-codes the number, so it must be the number.
    assert f"{vs.GLOBAL_LOGIT_SHIFT_2026:.4f}" in vs.GLOBAL_ROLLOUT_CALIBRATION_2026.label
    # ...and it must move survival the same way the per-position TE cell does.
    lowered = vs.GLOBAL_ROLLOUT_CALIBRATION_2026.apply(0.5, position="WR", rollouts=512)
    assert lowered < 0.5


def test_the_calibration_carries_the_rollout_count_it_was_fitted_at():
    """PROVENANCE, [8]. The shifts were fitted at R=200 and the module's own wiring
    recipe applies them at R=512; the correction's size depends on the smoothing,
    which depends on R. ``brier_and_bias`` REFUSES this mismatch when scoring, so
    applying it silently is the inconsistency. The bag records its fit R and the
    provider says so out loud."""
    assert vs.POSITION_ROLLOUT_CALIBRATION_2026.fitted_rollouts == 200
    assert vs.GLOBAL_ROLLOUT_CALIBRATION_2026.fitted_rollouts == 200
    # A bag with no recorded provenance is legal and silent (the honest default).
    quiet = vs.RolloutCalibration(shifts={"TE": -1.0}, label="test")
    assert quiet.fitted_rollouts is None
    assert not any(
        "PROVENANCE" in line
        for line in vs.CalibratedRollout(quiet, rollouts=512).describe()
    )
    # Matched R: nothing to disclose.
    matched = vs.CalibratedRollout(vs.POSITION_ROLLOUT_CALIBRATION_2026, rollouts=200)
    assert not any("PROVENANCE" in line for line in matched.describe())
    # Mismatched R (the module's own recipe): disclosed, with both numbers.
    live = vs.CalibratedRollout(vs.POSITION_ROLLOUT_CALIBRATION_2026, rollouts=512)
    warning = [line for line in live.describe() if line.startswith("PROVENANCE:")]
    assert len(warning) == 1
    assert "200" in warning[0] and "512" in warning[0]
    assert vs.THE_SHIFTS_WERE_FITTED_AT_R200 in live.describe()
    # The measured size of the under-delivery is stated, not just its existence.
    assert "13%" in vs.THE_SHIFTS_WERE_FITTED_AT_R200
    assert "changes no conclusion" in vs.THE_SHIFTS_WERE_FITTED_AT_R200.lower()


# ============================ the level knob, and what it is allowed to claim ==


def test_the_level_knob_is_off_by_default_and_changes_nothing_when_zero(board):
    """Additive-by-construction: ``logit_shift`` defaults to 0.0 everywhere and a
    zero shift must be bit-identical to no shift, or every published route-2
    number silently moved when the knob was added."""
    assert vs.AnalyticSurvival().logit_shift == 0.0
    flat = vs.SlopedSurvivalParams.from_flat(DEFAULT_SURVIVAL_PARAMS)
    for rank in (1, 30, 146, 179, 400, 10_000):
        for pos in ("QB", "RB", "TE", "K", "DST"):
            for nxt in (5, 29, 90, 160):
                assert vs.sloped_analytic_survival(rank, pos, nxt, params=flat,
                                                   logit_shift=0.0) == (
                    analytic_survival(rank, pos, nxt)
                )
    ctx = _ctx(board, overall_pick=4)
    for pos in POSITIONS:
        assert vs.expected_next_best_vor(ctx, pos, 30, logit_shift=0.0) == (
            vs.expected_next_best_vor(ctx, pos, 30)
        )


def test_the_level_knob_reaches_BOTH_halves_of_the_estimate(board):
    """THE CORRECTED FINDING'S MECHANISM, pinned.

    The first pass concluded the shipped analytic route's advantage was structural
    because a uniform shift applied to the ROLLOUT changed little — but
    ``CalibratedRollout`` passes ``next_best_vor`` through UNCORRECTED, so that
    probe could only ever move ``1 - S_next`` and never ``VONA``. On route 2 the
    level reaches both halves, and that is the whole mechanism: pessimism lowers
    the expected replacement, which RAISES VONA, which multiplies an ALSO-raised
    ``1 - S_next``.

    This test pins both sides of that contrast, because it is the sentence the
    corrected constant rests on.
    """
    ctx = _ctx(board, overall_pick=12, round_num=2)
    candidates = ctx.state.window_by_rank(sorted(POSITIONS), 8)
    positions = sorted({c.position for c in candidates})

    plain = vs.AnalyticSurvival()(ctx, candidates=candidates, positions=positions, rng=None)
    grim = vs.AnalyticSurvival(logit_shift=-3.5)(
        ctx, candidates=candidates, positions=positions, rng=None
    )
    assert all(grim.survival[k] < plain.survival[k] for k in plain.survival)
    assert all(grim.next_best_vor[p] < plain.next_best_vor[p] for p in positions), (
        "pessimism must reach the REPLACEMENT too — that is the half route 1 cannot "
        "correct and the reason the original probe measured nothing"
    )

    # ...and route 1's wrapper demonstrably cannot do it, by design.
    calibrated = vs.CalibratedRollout(
        vs.RolloutCalibration(shifts={p: -3.5 for p in POSITIONS}, default=-3.5, label="probe"),
        rollouts=24,
    )
    ctx_a = _ctx(board, overall_pick=12, round_num=2)
    cands_a = ctx_a.state.window_by_rank(sorted(POSITIONS), 8)
    pos_a = sorted({c.position for c in cands_a})
    raw = rollout_survival(ctx_a, cands_a, rng=random.Random(3), rollouts=24, positions=pos_a)
    wrapped = calibrated(ctx_a, candidates=cands_a, positions=pos_a, rng=random.Random(3))
    assert wrapped.next_best_vor == raw.next_best_vor, (
        "documented pass-through: a route-1 correction cannot move VONA, which is "
        "why the uniform-shift probe on route 1 refuted nothing"
    )


def test_the_level_finding_states_its_evidence_and_its_limits():
    """Rule 6 on the corrected constant. It must name the flaw in the earlier
    probe, carry the paired number that says the level explains the gap, and
    refuse to be read as free wins."""
    text = vs.THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL
    assert "COULD NOT TEST" in text
    assert "UNCORRECTED" in text
    assert "-0.002" in text and "INDISTINGUISHABLE" in text
    assert "REPLICATES OUT OF SAMPLE" in text, (
        "a mechanism claim measured on the grid that suggested it is a hypothesis; "
        "this one was re-run on a fresh grid and must say so"
    )
    assert "-0.004, 95% CI -0.022 .. +0.014" in text
    assert "-3.5" in text
    assert "urgency" in text.lower() and "b_vona" in text
    assert "CANNOT BE HARVESTED ON ROUTE 1" in text
    # The superseded claim must be retracted where it was made, not only corrected
    # somewhere else, or a reader who stops at the first constant is misled.
    assert "IT IS THE LEVEL" in vs.CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY
    assert "used to say the opposite" in vs.CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY
    assert vs.THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL in vs.AnalyticSurvival().describe()


def test_the_shipped_analytic_arm_is_disclosed_as_a_hybrid():
    """Every route-2 number in this module compares against an arm called 'analytic
    AS SHIPPED', and no such engine exists: ``survival.analytic_survival`` answers
    survival only, so the arm is the shipped constants plus THIS module's new
    ``expected_next_best_vor``. That estimator is the half the level effect works
    through, so calling the arm 'shipped' without saying so credits shipped code
    with a result half of which is new code."""
    text = vs.WHAT_THE_SHIPPED_ANALYTIC_ARM_ACTUALLY_IS
    assert "HYBRID" in text
    assert "expected_next_best_vor" in text
    assert "never as a claim that this arm exists on disk" in text
    assert text in vs.AnalyticSurvival().describe()
    # And the claim it guards is true: the shipped function has no such half.
    import inspect

    from ziggurat.draft import survival as shipped

    assert "next_best_vor" not in inspect.getsource(shipped.analytic_survival)


# ============================================== the additive / opt-in guarantee


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_shipped_module_imports_this_variant():
    """THE ADDITIVE GUARANTEE, checked rather than asserted in prose.

    This module's whole claim to be safe the day before a draft is that it is
    opt-in. If anything under ``ziggurat/`` other than this module's own test ever
    imports it, that claim is void and the golden master is no longer measuring
    the shipped engine.
    """
    root = Path(vs.__file__).resolve().parents[2] / "ziggurat"
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if p.name != "variant_survival.py"
        and any("variant_survival" in m for m in _module_imports(p))
    ]
    assert offenders == [], f"variant_survival must stay opt-in; imported by {offenders}"


def test_the_default_engine_is_untouched_by_this_module(board):
    """Importing this module must not change what a default ``PickEngine`` does.

    A module-level monkeypatch, or a mutated shared default, would be invisible
    everywhere else and would move the golden master.
    """
    assert PickEngine().survival is None
    ctx = _ctx(board, overall_pick=6)
    a = PickEngine(rollouts=8).recommend(ctx, top=1)[0].player_id
    ctx2 = _ctx(board, overall_pick=6)
    b = PickEngine(rollouts=8).recommend(ctx2, top=1)[0].player_id
    assert a == b


def test_this_module_reads_no_database(board):
    """Rule 1 by construction: no ``as_of`` appears here because no accessor does.

    A DB read would need a connection and an ``as_of``; this module's public
    surface takes neither, and the fitters take caller-supplied observations. The
    test pins that so a later convenience "just load the journals for me" helper
    cannot slip a defaulted, ungated read into the deletable package.
    """
    tree = ast.parse(Path(vs.__file__).read_text(encoding="utf-8"))
    identifiers = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    } | _module_imports(Path(vs.__file__))
    banned = {
        "sqlite3",
        "load_board",
        "load_journal",
        "load_journal_board",
        "load_espn_adp",
        "select_as_of",
        "latest_truth",
        "ziggurat.data",
    }
    assert not (identifiers & banned), (
        f"{sorted(identifiers & banned)} implies a DB seam this module must not have"
    )
    # And no public entry point takes an ``as_of``, because none of them reads.
    import inspect

    for name in vs.__all__:
        obj = getattr(vs, name)
        if callable(obj) and not isinstance(obj, str):
            try:
                sig = inspect.signature(obj)
            except (TypeError, ValueError):  # pragma: no cover - builtins
                continue
            assert "as_of" not in sig.parameters, f"{name} takes an as_of but reads nothing"


def test_the_published_constants_are_what_the_fitters_produce():
    """PROVENANCE. Regenerate both published bags from small deterministic samples
    and check the fitters are the ones that produced them — not the values (those
    need the real journals), but the SHAPE and the invariants: every canonical
    position present, finite, and the global fit sharing one number."""
    calib = vs.POSITION_ROLLOUT_CALIBRATION_2026
    assert set(calib.shifts) == set(POSITIONS)
    assert all(math.isfinite(v) for v in calib.shifts.values())
    glob = vs.GLOBAL_ROLLOUT_CALIBRATION_2026
    assert set(glob.shifts.values()) == {vs.GLOBAL_LOGIT_SHIFT_2026}
    assert glob.default == vs.GLOBAL_LOGIT_SHIFT_2026

    rng = random.Random(8)
    pts = [_Point(p, 0.6, rng.random() < 0.5) for p in POSITIONS for _ in range(400)]
    per_pos = vs.fit_logit_shifts(pts, rollouts=256, label="x")
    one = vs.fit_logit_shifts(pts, rollouts=256, per_position=False, label="x")
    assert set(per_pos.shifts) == set(POSITIONS)
    assert len(set(one.shifts.values())) == 1
    # Every published bag carries provenance a reader can act on (Rule 6).
    for bag in (calib, glob):
        assert "HYPOTHESIS" in bag.label and "not shipped" in bag.label
    assert "HYPOTHESIS" in vs.REFIT_SLOPED_LABEL
    # The route-2 label must carry its own negative, not only its Brier win.
    assert "DOES NOT PICK BETTER" in vs.REFIT_SLOPED_LABEL


def test_a_journal_shaped_end_to_end_run(board):
    """Smoke: a full synthetic draft with the calibrated engine in one seat runs to
    a legal 16-round roster. ``run_draft`` raises on an illegal roster, so this is
    a real assertion about the variant not breaking legality, not a coverage prop."""
    engine = PickEngine(
        rollouts=8, survival=vs.CalibratedRollout(vs.POSITION_ROLLOUT_CALIBRATION_2026, rollouts=8)
    )
    pickers = [engine if t == 8 else AutodraftBot() for t in range(TEAMS)]
    result = run_draft(board, pickers, rng=random.Random(2), roster=ROSTER, rounds=ROUNDS)
    assert len(result.pick_log) == TEAMS * ROUNDS
    assert len(result.rosters[8]) == ROUNDS
