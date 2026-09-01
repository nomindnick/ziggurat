"""Variant ``survivalfit`` — the survival model corrected against REAL rooms.

Import-quarantined package (Rule 8). **Additive and opt-in: this module changes no shipped
behaviour.** Nothing here is imported by ``engine.py``, ``survival.py``,
``session.py`` or ``webapp.py``; every constant it publishes is a labelled
alternative, and the only way any of it runs is a caller passing
``PickEngine(survival=...)`` — the injection seam item 2.3 already built.

WHAT IT IS. ``roomcheck.py`` scored the two shipped survival routes against
eleven completed real 10-team ESPN practice rooms and found two different
defects. This module packages one correction for each, so a Phase 3 integrator
can wire either behind a flag:

* **route 1, the ROLLOUT** (``survival.rollout_survival`` — the live path, the
  only one draft night reaches). It is *nearly* well calibrated: measured bias
  +0.014 over 3,942 real decision rows. The residual is concentrated by
  POSITION, and the biggest single cell is tight end. :class:`RolloutCalibration`
  applies a measured per-position **logit shift** to the survival map the
  rollout returns; :class:`CalibratedRollout` is the drop-in
  ``engine.SurvivalProvider``.
* **route 2, the ANALYTIC FALLBACK** (``survival.analytic_survival``). It is
  badly miscalibrated (bias -0.370, Brier 0.345) AND, separately, its K/DST
  functional form is wrong: the shipped shape makes a kicker's draft pick
  independent of his rank. :class:`SlopedSurvivalParams` +
  :func:`sloped_analytic_survival` give K and DST a rank slope, and
  :data:`REFIT_SLOPED_PRACTICE_2026` carries the censored-MLE constants.
  :class:`AnalyticSurvival` makes route 2 a complete, injectable provider for the
  first time (the shipped function answers survival but not the ``next_best_vor``
  the engine also needs).

**ROUTE 2 CANNOT AFFECT DRAFT NIGHT.** ``PickEngine.survival`` defaults to
``None``, which reaches ``engine._rollout_survival_provider`` — the rollout,
always. ``session.py``/``webapp.py`` never inject a provider. So
``analytic_survival`` is dead code on the live path and every constant in the
route-2 half of this module is a correctness repair for offline use and for
whatever future caller injects it. It is packaged here because it is cheap and
right, not because it is urgent.

WHAT WAS MEASURED, AND WHAT IT DOES NOT PROVE (Rule 6 — read before shipping):

1. **The route-1 correction is real but small, and its held-out gain sits at the
   measurement noise floor.** See :data:`ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR`.
2. **A simulator A/B cannot validate the route-1 correction, only price it.**
   See :data:`THE_SIM_ROOM_IS_THE_ROLLOUTS_OWN_MODEL` — this is the single most
   important caveat in this module and the reason its recommendation is what it is.
3. **Correcting TE survival DOWNWARD pushes the engine toward MORE tight ends**,
   and the engine already takes 3.0 TEs per draft. See
   :data:`THE_TE_CORRECTION_PUSHES_THE_WRONG_WAY`.
4. **The A/B says neither correction earns a ship, and on a HELD-OUT seed grid
   route 1 is measurably WORSE, not merely unproven.** On the exploration grid the
   per-position correction was -0.004 expected wins with the interval across zero;
   on a fresh grid it is **-0.011 with the 95% interval BELOW zero**, 107 of 120
   drafts byte-identical, and the one-shift global bag is inert (+0.001, 115 of
   120 identical). Route 2's re-fit loses to the shipped constants on both grids
   (-0.034 then -0.061) because the shipped fallback's miscalibration was
   accidentally useful. Numbers in :data:`AB_RESULT_2026_HELD_OUT`,
   :data:`AB_RESULT_2026` and :data:`CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY`.
   **The recommendation this module makes about itself is DO NOT WIRE EITHER
   CORRECTION IN.**
5. **The K/DST half of the route-1 correction is decision-irrelevant** — 120 of
   120 exact ties in the simulator, and 0 of 165 changed picks on the real rooms
   at every one of six seeds — so it cannot move the validated divergence play.
   :data:`THE_KDST_HALF_CHANGES_NOTHING`.
6. **The shipped fallback's accidental advantage is its LEVEL, and the first pass
   said the opposite.** A uniform pessimism knob on the well-calibrated re-fit
   reproduces the badly-calibrated shipped route to within noise (-0.002 expected
   wins, CI -0.024 .. +0.020, matched roster shape), so the mechanism is an
   urgency-SCALE effect and not a mysterious structure. The probe that "refuted"
   it could not have tested it, because route 1's wrapper leaves ``next_best_vor``
   uncorrected and so cannot move VONA at all.
   :data:`THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL`.
7. **The published shifts were fitted at R=200 and the wiring recipe applies them
   at R=512**, which under-delivers about 13% of the DST correction and 17% of the
   K one. It changes no conclusion; it is disclosed because ``brier_and_bias``
   refuses the identical mismatch when scoring.
   :data:`THE_SHIFTS_WERE_FITTED_AT_R200`.

RULES. Rule 1: **this module has no DB seam at all** — the board arrives on
``ctx.state`` and every fitting function takes already-loaded, caller-supplied
observations, exactly as ``survival.py`` does. There is therefore no ``as_of`` to
take and no accessor to leakage-test; the DB reads behind the measurements live
in ``roomcheck.load_journal_board`` / ``load_espn_adp``, which are already
keyword-only-``as_of`` and already leakage-tested. Rule 2: no scoring constant —
VOR and points come off the board entries. Rule 6: every constant is labelled
with its cohort, its n and its direction, and :meth:`CalibratedRollout.describe`
renders those labels in plain language for a novice reader. Rule 8: lives under
``ziggurat/draft/`` and imports only siblings plus ``core``.

Determinism: this module introduces no randomness of its own. The wrapper draws
from the caller-passed ``random.Random`` exactly as the shipped rollout does, in
the same order, so a fixed seed reproduces a wrapped estimate bit-for-bit; the
optimizer is roomcheck's deterministic Nelder-Mead.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ziggurat.draft.bots import POSITIONS, BoardEntry, PickContext
from ziggurat.draft.engine import SurvivalEstimate
from ziggurat.draft.priors import ROOM_PRIORS_2025, RoomPriors
from ziggurat.draft.survival import (
    DEFAULT_KAPPA,
    DEFAULT_ROLLOUTS,
    DEFAULT_SURVIVAL_PARAMS,
    SurvivalParams,
    rollout_survival,
    upcoming_opponent_picks,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import random

    from ziggurat.draft.roomcheck import SurvivalObservation, SurvivalPoint

__all__ = [
    "AB_RESULT_2026",
    "AB_RESULT_2026_HELD_OUT",
    "ANALYTIC_ROUTE_IS_DEAD_ON_THE_LIVE_PATH",
    "CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY",
    "DECISION_RELEVANCE_ON_REAL_ROOMS",
    "WHAT_THE_SHIPPED_ANALYTIC_ARM_ACTUALLY_IS",
    "THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL",
    "THE_SHIFTS_WERE_FITTED_AT_R200",
    "FALLBACK_RANK_BASE",
    "GLOBAL_LOGIT_SHIFT_2026",
    "LATENCY_2026",
    "GLOBAL_ROLLOUT_CALIBRATION_2026",
    "POSITION_ROLLOUT_CALIBRATION_2026",
    "REFIT_SLOPED_LABEL",
    "REFIT_SLOPED_PRACTICE_2026",
    "ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR",
    "ROLLOUT_CALIBRATION_LABEL",
    "THE_KDST_HALF_CHANGES_NOTHING",
    "THE_SIM_ROOM_IS_THE_ROLLOUTS_OWN_MODEL",
    "THE_TE_CORRECTION_PUSHES_THE_WRONG_WAY",
    "AnalyticSurvival",
    "CalibratedRollout",
    "RolloutCalibration",
    "SlopedFit",
    "SlopedSurvivalParams",
    "brier_and_bias",
    "expected_next_best_vor",
    "fit_logit_shifts",
    "fit_sloped_params",
    "score_analytic",
    "sloped_analytic_survival",
    "smooth_rollout_probability",
]


# --------------------------------------------------------------------- numerics


def _sigmoid(x: float) -> float:
    """Overflow-safe logistic (same two-branch form as ``survival._sigmoid``)."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def smooth_rollout_probability(p: float, rollouts: int) -> float:
    """Jeffreys-smooth a Monte-Carlo survival frequency before taking its logit.

    The rollout returns ``k / R``, so 0.0 means "0 of R survived", NOT "impossible":
    ``logit`` of it is ``-inf`` and any correction applied there is meaningless.
    Smoothing to ``(k + 1/2) / (R + 1)`` is the Jeffreys posterior mean of the
    underlying Bernoulli rate, which is the honest reading of a frequency estimate
    and bounds the logit at ``+/- log(2R + 1)`` — about +/- 6.9 at the live R=512.

    ``rollouts <= 0`` (an analytic or stubbed provider with no sample behind it) is
    treated as a probability, not a frequency, and only clamped away from the
    asymptotes, because there is no ``k`` to smooth.
    """
    if rollouts <= 0:
        return min(max(p, 1e-9), 1.0 - 1e-9)
    k = round(float(p) * rollouts)
    return (k + 0.5) / (rollouts + 1.0)


# ======================================================================
#            route 1 — the LIVE rollout, recalibrated per position
# ======================================================================


@dataclass(frozen=True)
class RolloutCalibration:
    """A labelled per-position recalibration of a survival map (Rule 6).

    The correction is a **logit shift**::

        S' = sigmoid(logit(smooth(S)) + shift(position))

    chosen over an additive probability offset for two reasons that are properties
    of what the engine does with the number, not of statistical taste:

    * it cannot leave [0, 1], so a corrected survival is always a probability; and
    * its effect is LARGEST near S = 0.5 and vanishes at the asymptotes, which is
      exactly the shape the engine wants — ``urgency = VONA * (1 - S_next)`` is
      most sensitive in the 0.1-0.7 band and a player the room certainly takes
      (or certainly does not) should not be argued with.

    SIGN CONVENTION, stated because getting it backwards is silent: a NEGATIVE
    shift LOWERS survival, which RAISES ``1 - S_next``, which RAISES urgency at
    that position, which makes the engine draft it EARLIER. Every position whose
    measured bias was positive (the model was OPTIMISTIC — it said they last and
    they do not) therefore gets a negative shift.

    ``shifts`` is read-only by contract; ``default`` applies to any position not
    named (0.0 = leave it alone). ``label`` is plain-language provenance and is
    what :meth:`CalibratedRollout.describe` renders.

    ``fitted_rollouts`` records the R the shifts were FITTED at, and it is here
    because the shift's size is not R-transferable: it is a shift in a logit whose
    scale is set by :func:`smooth_rollout_probability`, which bounds the logit at
    ``+/- log(2R + 1)`` — 5.99 at R=200, 6.94 at R=512. Applying a bag fitted at one
    R at another under-delivers part of the correction (measured in
    :data:`THE_SHIFTS_WERE_FITTED_AT_R200`). ``None`` means "provenance not
    recorded", which is the honest default for a hand-built bag and is why nothing
    raises on it; the published bags set it, and
    :meth:`CalibratedRollout.describe` says so out loud when it does not match the
    R the provider will actually run at. It is a disclosure, not a gate, because
    ``brier_and_bias`` already refuses the same mismatch where it is silent and
    unrecoverable (a wrong Brier), while here it is visible and small.
    """

    shifts: Mapping[str, float]
    default: float = 0.0
    label: str = "UNLABELLED calibration — provenance unknown, do not ship"
    fitted_rollouts: int | None = None

    def shift_for(self, position: str) -> float:
        return float(self.shifts.get(str(position).strip().upper(), self.default))

    def apply(self, survival: float, *, position: str, rollouts: int) -> float:
        """One corrected survival probability, clamped to [0, 1]."""
        delta = self.shift_for(position)
        if delta == 0.0:
            return float(survival)
        z = _logit(smooth_rollout_probability(float(survival), rollouts))
        return min(1.0, max(0.0, _sigmoid(z + delta)))

    def apply_map(
        self,
        survival: Mapping[str, float],
        *,
        position_of: Mapping[str, str],
        rollouts: int,
    ) -> dict[str, float]:
        """Correct a whole ``player_id -> S`` map.

        A player_id missing from ``position_of`` is left UNCORRECTED rather than
        given ``default``: a correction applied to a player whose position we could
        not establish is a guess, and this module refuses to guess quietly.
        """
        out: dict[str, float] = {}
        for pid, s in survival.items():
            pos = position_of.get(pid)
            out[pid] = float(s) if pos is None else self.apply(s, position=pos, rollouts=rollouts)
        return out


#: MEASURED, LABELLED, NOT A DEFAULT (Rule 6). Per-position logit shifts fitted by
#: minimising mean log-loss of the LIVE ROLLOUT route against the eleven completed
#: real 10-team ESPN practice rooms in ``data/draft/practice/`` (2026-08-16 to
#: 2026-08-29; 3,942 scored (player, decision-window) rows over 165 real operator
#: decisions), scored the way the cockpit runs — ``rollout_points`` with
#: ``live_recalibration=True``, ``kappa`` 1.3, R=200, seed 20260830.
#:
#: The measured per-position bias (predicted minus observed survival) that these
#: shifts remove, with n:
#:
#:     TE  +0.101 (n=393)   <- the one large cell
#:     QB  +0.046 (n=474)
#:     DST +0.036 (n=253)
#:     K   +0.028 (n=311)
#:     RB  -0.014 (n=1075)
#:     WR  -0.006 (n=1436)
#:
#: READ :data:`ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR` AND
#: :data:`THE_SIM_ROOM_IS_THE_ROLLOUTS_OWN_MODEL` BEFORE SHIPPING THIS.
POSITION_ROLLOUT_CALIBRATION_2026 = RolloutCalibration(
    shifts={
        "QB": -0.554,
        "RB": +0.164,
        "WR": +0.069,
        "TE": -1.103,
        "DST": -1.236,
        "K": -1.056,
    },
    default=0.0,
    fitted_rollouts=200,
    label=(
        "HYPOTHESIS (not shipped): per-position logit recalibration of the live "
        "survival rollout, fitted by log-loss on 11 completed real 10-team ESPN "
        "practice drafts (3,942 decision rows, 165 operator decisions, "
        "2026-08-16..29). Removes a measured +0.101 tight-end and +0.046 "
        "quarterback optimism — the room takes those two positions EARLIER than "
        "the rollout expects. Held-out (leave-one-room-out) Brier 0.1010 -> "
        "0.1002, which is BELOW this measurement's own +/-0.003 Monte-Carlo noise "
        "floor, so the gain is not established."
    ),
)

#: The same fit with ONE shift for every position (the null model for "is the
#: per-position structure real?"). Held-out it does as well as the per-position
#: fit and improves 10 of 11 rooms rather than 7, which is itself the finding:
#: the honest reading of the route-1 residual is a small GLOBAL optimism, not a
#: position-specific one.
#:
#: THE SIGN IS THE WHOLE CONSTANT AND IT MUST BE NEGATIVE. The measured residual
#: is OPTIMISM (+0.0135 bias over 3,942 rows: the rollout says players last more
#: often than they do), and a negative logit shift is what removes optimism —
#: it lowers survival, raises ``1 - S_next``, and makes the engine draft EARLIER
#: everywhere. Flipped positive it would make the engine wait LONGER at every
#: position while every label still read as a correction. Pinned by
#: ``test_the_global_shift_is_negative_and_the_label_quotes_the_real_number``.
GLOBAL_LOGIT_SHIFT_2026 = -0.1718

GLOBAL_ROLLOUT_CALIBRATION_2026 = RolloutCalibration(
    shifts={pos: GLOBAL_LOGIT_SHIFT_2026 for pos in POSITIONS},
    default=GLOBAL_LOGIT_SHIFT_2026,
    fitted_rollouts=200,
    label=(
        "HYPOTHESIS (not shipped): a SINGLE logit shift of -0.1718 applied to "
        "every position, fitted on the same 11 real rooms. Held-out Brier 0.1010 "
        "-> 0.1003, improving 10 of 11 rooms (the per-position fit improves 7). "
        "Same noise-floor caveat."
    ),
)

ROLLOUT_CALIBRATION_LABEL = POSITION_ROLLOUT_CALIBRATION_2026.label

ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR = (
    "NEGATIVE RESULT, measured 2026-08-30. The route-1 recalibration's held-out "
    "gain is real in sign and negligible in size. Leave-one-room-out over the 11 "
    "journals, all 3,942 rows: uncorrected Brier 0.1010 / bias +0.0135; one "
    "global logit shift 0.1003 / -0.0002 (better in 10 of 11 rooms); "
    "per-position shifts 0.1002 / -0.0007 (better in 7 of 11); adding a per-"
    "position SLOPE makes it worse out of sample (0.1006, 6 of 11) and a global "
    "slope worse still (0.1010, 3 of 11). roomcheck's own MEASUREMENT_NOISE "
    "constant puts this measurement's Monte-Carlo noise at ~0.001 of Brier and "
    "says a gap under ~0.003 is not a result. The whole per-position gain is "
    "0.0008. Restricted to the rows the engine actually scores "
    "(``in_engine_candidates``, n=1,202) the numbers are larger — 0.1304 -> "
    "0.1291 held out — but that cohort's K cell is 31 rows that survived 100% of "
    "the time, which is a DEGENERATE cell: its likelihood is maximised at plus "
    "infinity, not at any finite shift, and that degeneracy is why the shipped "
    "constants above are fitted on the full probe instead.\n"
    "  A CORRECTION TO THIS CONSTANT'S OWN EARLIER TEXT (2026-08-30, second "
    "pass). It used to say that cell 'runs to a degenerate +28.5 logit shift'. "
    "Nothing in this module produces +28.5 and a reader recomputing it cannot "
    "get there: ``fit_logit_shifts`` returns EXACTLY 0.0 for that cell by design "
    "(``_fit_one_shift`` finds no sign change inside its bound and refuses), and "
    "the same bisection run unbounded finds no finite root either — the gradient "
    "measures -0.0402 at 0, -3.6e-4 at +5, -2.4e-6 at +10 and 0.000000 from about "
    "+28 onward, so a wide-bounded search converges only to the point where the "
    "gradient underflows to zero in double precision (+36.3 at bound 60 on this "
    "build), which is a property of the float, not of the data. The substantive "
    "facts are unchanged and reproduce exactly: 1,202 engine-candidate rows, a K "
    "cell of 31, 31 of 31 survived."
)

THE_SHIFTS_WERE_FITTED_AT_R200 = (
    "PROVENANCE MISMATCH, measured 2026-08-30 and disclosed rather than papered "
    "over. The published shifts were fitted on predictions made at R=200 "
    "(``roomcheck.DEFAULT_MEASUREMENT_ROLLOUTS``) and this module's own wiring "
    "recipe applies them at the live R=512. They are not R-transferable: the shift "
    "lives in a logit whose scale is set by the Jeffreys smoothing, which bounds "
    "it at +/- log(2R + 1) — 5.99 at R=200 against 6.94 at R=512 — so the same "
    "number is a slightly SMALLER correction at the larger R.\n"
    "  What that costs, on all 3,942 real rows. Re-fitting under R=512 smoothing "
    "gives DST -1.442, K -1.254, TE -1.122, QB -0.571, RB +0.149, WR +0.070 "
    "against the published -1.236 / -1.056 / -1.103 / -0.554 / +0.164 / +0.069. "
    "Applying the PUBLISHED bag at R=512 leaves a residual bias of +0.0048 at DST, "
    "+0.0050 at K and +0.0018 at TE out of raw biases of +0.0358, +0.0291 and "
    "+0.1002 — about 13%, 17% and 2% of each correction never delivered; matched "
    "smoothing would leave +0.0010, +0.0012 and +0.0004. It is a smoothing-"
    "constant effect and not Monte-Carlo noise: re-fitting on FRESH R=512 "
    "predictions (DST -1.442, K -1.254) lands on the R=512-smoothed R=200 fit "
    "(DST -1.471, K -1.258), not on the published one.\n"
    "  IT CHANGES NO CONCLUSION and is stated anyway. Over the whole cohort at "
    "R=512 the uncorrected Brier is 0.1012 (bias +0.0146) and the corrected one is "
    "0.0995 (bias +0.0014) whichever smoothing the correction is applied under; "
    "the whole route-1 gain was already below this measurement's noise floor. The "
    "reason to say it out loud is consistency: ``brier_and_bias`` REFUSES this "
    "exact mismatch when SCORING, because there it is silent and produces a wrong "
    "number, and a module that refuses a mismatch in one place and performs it "
    "quietly in another is training its reader to trust the wrong half. "
    "``RolloutCalibration.fitted_rollouts`` records the fit R and "
    "``CalibratedRollout.describe`` names the mismatch on every rendered "
    "recommendation."
)

THE_SIM_ROOM_IS_THE_ROLLOUTS_OWN_MODEL = (
    "METHODOLOGICAL LIMIT — the reason the A/B below is a PRICE, not a "
    "VALIDATION. ``evaluate.paired_compare`` fills the other nine seats with "
    "``RankNoiseBot``/``AutodraftBot`` at ``ROOM_PRIORS_2025``, and "
    "``rollout_survival`` predicts that room by simulating those same two bots at "
    "those same priors. In that world the uncorrected rollout is very nearly "
    "correctly specified by construction, so ANY correction fitted on real human "
    "rooms is a pure misspecification there and can only cost. A paired A/B in "
    "the simulator therefore cannot tell us the correction helps; it can only "
    "tell us how much the engine's picks are worth moving by this much, which "
    "bounds the downside. The evidence FOR the correction is the held-out "
    "calibration on the real rooms, and that evidence is at the noise floor "
    "(see ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR). Two measurements, neither "
    "of which can be substituted for the other."
)

AB_RESULT_2026_HELD_OUT = (
    "THE HELD-OUT A/B, measured 2026-08-30 after the fix round, AND IT CHANGES "
    "THE VERDICT ON ROUTE 1 FROM 'not proven' TO 'measurably worse'. Everything "
    "in AB_RESULT_2026 was measured on seed 7, the same grid the exploration ran "
    "on. Re-run on a FRESH seed grid (seed 20260831, same live 3,264-row board, "
    "same 120 paired drafts at n=40 across seats 1, 5 and 9, rollouts=512, "
    "ROOM_PRIORS_2025, graded by expected wins), seven arms on one shared grid:\n"
    "    per-position calibrated rollout   -0.011   95% CI -0.020 .. -0.002   "
    "EXCLUDES ZERO, ahead in 4%, 107 of 120 exact ties\n"
    "    global calibrated rollout         +0.001   95% CI -0.004 .. +0.005   "
    "includes zero, 115 of 120 exact ties\n"
    "    analytic re-fit (route 2)         +0.009   95% CI -0.012 .. +0.029   "
    "includes zero\n"
    "    analytic SHIPPED-FLAT (route 2)   +0.070   95% CI +0.045 .. +0.095\n"
    "    twin of the baseline              +0.000   120 of 120 EXACT TIES "
    "(the pairing sanity check, passed)\n"
    "  ON SEED 7 the per-position correction was -0.004 with the interval ACROSS "
    "zero; out of sample it is -0.011 with the interval BELOW it, and the "
    "fixed-field check agrees (-0.010). Two grids, both negative, the held-out "
    "one significantly so. That is as close to a decision as this evidence gets: "
    "the route-1 per-position correction should NOT be wired in. The global bag "
    "is not harmful, it is INERT — 115 of 120 byte-identical drafts and an "
    "interval 5 thousandths of a win wide around zero — so shipping it would buy "
    "nothing measurable either.\n"
    "  Read it with THE_SIM_ROOM_IS_THE_ROLLOUTS_OWN_MODEL: this room is the "
    "rollout's own model, so a correction fitted on real humans is misspecified "
    "here by construction and this A/B bounds the DOWNSIDE rather than testing "
    "the upside. What it can say, and now does, is that the downside is real, "
    "one-way, and points the same direction as "
    "DECISION_RELEVANCE_ON_REAL_ROOMS's 16-of-16 adverse pick changes."
)

AB_RESULT_2026 = (
    "THE A/B, measured 2026-08-30 (the seed-7 exploration grid; the held-out "
    "re-run is AB_RESULT_2026_HELD_OUT and it is the one that decides). "
    "`evaluate.tournament`, live 2026-08-30 board "
    "(3,264 rows), n=40 drafts at EACH of seats 1, 5 and 9 = 120 paired drafts "
    "per arm, seed 7, rollouts=512, ROOM_PRIORS_2025, graded by "
    "`grader.grade_roster` (expected wins). Pairing validated in the same run: a "
    "twin of the baseline returns mean delta EXACTLY 0.000 with 120 of 120 exact "
    "ties.\n"
    "  route 1, per-position calibrated rollout vs current: mean -0.004 expected "
    "wins, 95% CI -0.016 .. +0.007 (INCLUDES ZERO), bootstrap -0.017 .. +0.007, "
    "ahead in 6% of drafts with 105 of 120 EXACT TIES, roster shape unchanged "
    "(QB 2.99 / TE 3.00 both sides), unfillable weeks 0.79 vs 0.76, fixed-field "
    "check -0.005 (same sign). NOT PROVEN BETTER; point estimate slightly "
    "negative; the whole distribution is 'almost always identical, occasionally "
    "a little worse'.\n"
    "  decomposition, same grid: the K/DST half of the correction is decision-"
    "IRRELEVANT — 120 of 120 exact ties, mean delta exactly 0.000 (see "
    "THE_KDST_HALF_CHANGES_NOTHING). TE alone -0.003 (CI -0.013 .. +0.006, 109 "
    "ties); the four skill positions together -0.004, i.e. every bit of the "
    "(tiny, negative) effect is on the skill side.\n"
    "  route 2, refit+sloped vs shipped-flat on the SAME analytic route: mean "
    "-0.034 expected wins, CI -0.062 .. -0.006, EXCLUDING ZERO — the better-"
    "calibrated constants are WORSE as a decision rule here. See "
    "CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY, which is the most surprising "
    "thing this variant measured and the reason (b) is offered as a correctness "
    "repair only."
)

THE_KDST_HALF_CHANGES_NOTHING = (
    "MEASURED, and it settles the standing worry. Phase 1 established that ESPN "
    "ADP is ceiling-censored at 170 and that its relationship to real 10-team "
    "room timing COLLAPSES at K and DST, so a survival change at those two "
    "positions is exactly where a correction could quietly damage the validated "
    "K/DST divergence play. Run alone — K -1.056 and DST -1.236 applied, every "
    "skill position left at 0.0 — the correction changes NOTHING: mean delta "
    "exactly 0.000 over 120 paired drafts, 120 of 120 exact ties, identical "
    "roster shape and identical hole count. The mechanism is structural rather "
    "than lucky: the engine's own `kdst_earliest_round` window forbids K/DST "
    "before round 9, and until the room's late run their rollout survival sits so "
    "close to 1 that a logit shift of this size cannot reorder anything. So this "
    "correction CANNOT move the divergence play in either direction — and the "
    "reason to know that is measurement, not the argument that it shouldn't.\n"
    "  CONFIRMED ON REAL ROOMS TOO (2026-08-30, second pass): replaying the K/DST "
    "half alone over all 165 real operator decisions from the 11 practice "
    "journals, at the live rollouts=512 and the live-recalibrated priors, changes "
    "0 picks — at every one of six independent Monte-Carlo seeds. The simulator "
    "and the real rooms agree, and they agree at zero."
)

WHAT_THE_SHIPPED_ANALYTIC_ARM_ACTUALLY_IS = (
    "READ THIS BEFORE QUOTING ANY 'SHIPPED ANALYTIC ROUTE' NUMBER IN THIS MODULE. "
    "There is no such engine arm in the repo to A/B. ``survival.analytic_survival`` "
    "answers survival ONLY; ``engine.SurvivalEstimate`` needs a ``next_best_vor`` "
    "too, and nothing shipped produces one analytically — which is exactly why "
    "route 2 was never actually injectable and why this module had to write "
    ":func:`expected_next_best_vor`. So every arm here labelled 'analytic AS "
    "SHIPPED' is a HYBRID: the shipped CONSTANTS and the shipped survival curve, "
    "plus this module's own new replacement estimator. That estimator is an "
    "independence approximation over the position's VOR-ordered board, and it is "
    "the half the level effect works through, so it is doing real work in every "
    "route-2 number reported here. It is called 'shipped' as shorthand for 'the "
    "shipped constants', never as a claim that this arm exists on disk today."
)

CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY = (
    "THE SURPRISE, and an honest negative for correction (b). On the simulated "
    "room the BADLY calibrated shipped analytic route (bias -0.370, Brier 0.345) "
    "BEATS the live rollout by +0.058 expected wins (CI +0.030 .. +0.087, 120 "
    "pairs) with a third of the unfillable starter weeks (0.25 vs 0.76) — it "
    "drafts about one more RB and 0.7 fewer WR (RB 3.90 / WR 4.38 against the "
    "rollout's 2.93 / 5.08). Re-fitting it to be well calibrated gives back most "
    "of that: refit+sloped vs shipped-flat is -0.034 expected wins (CI -0.062 .. "
    "-0.006) and holes go 0.25 -> 0.68. So on THIS objective, in THIS room, the "
    "fallback's miscalibration was doing something useful and the repair removes "
    "it.\n"
    "  IT IS THE LEVEL, and this constant used to say the opposite. See "
    "THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL for the probe and the numbers: a "
    "single uniform pessimism knob on the RE-FIT reproduces the shipped-flat arm "
    "to within noise, so the miscalibration was doing exactly the urgency-scale "
    "job the first pass guessed at and then wrongly dismissed. The re-fit is still "
    "a correctness repair and still loses as a decision rule at its own honest "
    "level, which is what this constant is named for; what changed is that we now "
    "know WHY, and the lead belongs to whoever owns the urgency weight `b_vona`.\n"
    "  None of this reaches draft night either way "
    "(ANALYTIC_ROUTE_IS_DEAD_ON_THE_LIVE_PATH), and 'the shipped analytic route' "
    "is a hybrid arm, not shipped code (WHAT_THE_SHIPPED_ANALYTIC_ARM_ACTUALLY_IS)."
)

THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL = (
    "CORRECTED FINDING, measured 2026-08-30 (second pass). An earlier version of "
    "CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY said the shipped analytic "
    "fallback's advantage lives in its STRUCTURE rather than its level, and cited "
    "a probe that applied uniform logit shifts to the ROLLOUT. THAT PROBE COULD "
    "NOT TEST THE CLAIM. ``CalibratedRollout`` passes ``next_best_vor`` through "
    "UNCORRECTED by design, so a shift there scales urgency only through "
    "(1 - S_next) and leaves ``VONA = best_now.vor - next_best_vor`` untouched — "
    "half the lever, missing. On route 2 the level enters BOTH halves, because "
    "``expected_next_best_vor`` prices the replacement with the same survival "
    "curve. Measured on identical board states in a real slot-9 draft, that is a "
    "20-30 VOR gap exactly where it matters: at round 7 / pick 69 the best "
    "available RB has vor 14.49 and the rollout expects a replacement worth "
    "+14.01 (VONA 0.48 — no cliff at all) while the shipped-flat analytic expects "
    "-16.52 (VONA 31.01, a 65x larger cliff); round 6 / pick 52 is +13.27 against "
    "-8.22; round 8 / pick 72 is -22.49 against -37.86.\n"
    "  THE PROBE, run properly. logit(shipped-flat) - logit(re-fit) at the points "
    "actually evaluated in that draft is mean -3.48 / median -3.70, so if the "
    "level is the whole story the re-fit plus a uniform -3.5 should BE the "
    "shipped-flat arm. It is. Same grid as the original claim (120 paired drafts, "
    "n=40 at each of seats 1, 5 and 9, seed 7, graded by expected wins):\n"
    "      shipped-flat vs current       +0.058   holes 0.25   RB 3.90  WR 4.38\n"
    "      re-fit + level -3.5 vs current +0.057   holes 0.32   RB 3.89  WR 4.27\n"
    "      re-fit + level -2.0 vs current +0.044   holes 0.42   RB 3.35  WR 4.72\n"
    "      re-fit (level 0) vs current    +0.025   holes 0.68   RB 3.10  WR 4.92\n"
    "  and re-fit+(-3.5) paired directly against shipped-flat is -0.002 expected "
    "wins, 95% CI -0.024 .. +0.020 — INDISTINGUISHABLE, with the roster shape "
    "matched at the second decimal. One scalar recovers the entire gap the first "
    "pass called unexplained.\n"
    "  IT REPLICATES OUT OF SAMPLE. Re-run on a fresh seed grid (seed 20260831, "
    "120 new paired drafts) the same three arms give shipped-flat +0.070, re-fit "
    "+ level -3.5 +0.066, re-fit alone +0.009 against the same baseline; paired "
    "directly, re-fit+(-3.5) vs shipped-flat is -0.004, 95% CI -0.022 .. +0.014, "
    "still indistinguishable, shape RB 3.88/WR 4.24 against 3.85/4.36 — while "
    "re-fit ALONE vs shipped-flat is -0.061, CI -0.086 .. -0.037. The level "
    "explains it on both grids; the calibration explains it on neither.\n"
    "  WHAT IT ACTUALLY MEANS, stated so nobody reads it as free wins. This is an "
    "urgency-SCALE result, not a survival result: pessimism raises VONA and "
    "(1 - S) together, which is `b_vona` turned up while wearing a calibration "
    "costume. The optimum is interior — level -5.0 gives back half the gain "
    "(+0.029, CI -0.005 .. +0.063) — which is what a mis-set knob looks like, not "
    "what a better forecast looks like. It is measured against `grader` expected "
    "wins in the simulated room, where the extra RBs mostly buy bye coverage. And "
    "IT CANNOT BE HARVESTED ON ROUTE 1: the wrapper has no honest way to correct "
    "``next_best_vor``, because the rollout's replacement estimate comes out of "
    "the simulation itself and would need a second simulation to re-derive. So "
    "this is a lead for the urgency weight, and it belongs to whoever owns the "
    "engine's weights — not to a survival-calibration variant, and not to draft "
    "night (ANALYTIC_ROUTE_IS_DEAD_ON_THE_LIVE_PATH). Reproduce it with "
    "``AnalyticSurvival(logit_shift=-3.5)``."
)

LATENCY_2026 = (
    "LATENCY, measured 2026-08-30 on the real 3,264-row board at the live "
    "rollouts=512, seat 9 of 10, 12 full drafts x 16 on-clock decisions = 192 "
    "timed `PickEngine.recommend` calls per engine, every engine timed on the "
    "IDENTICAL board state. Worst case: uncorrected 178.4 ms, per-position "
    "calibrated 185.6 ms, analytic route 1.3 ms — against the 243 ms ship gate. "
    "Means 79.6 / 80.2 / 0.8 ms. The paired worst-case delta printed +37.6 ms, "
    "and that number is SCHEDULER NOISE, not this wrapper: the machine was "
    "running five concurrent test suites, the arithmetically identical global-"
    "shift arm showed the same +31 ms, and the wrapper's own work timed in "
    "isolation is 2.9 us at 5 candidates and 13.4 us at 25 (`apply_map`, median "
    "of 2,000 calls) — about 0.008% of one recommendation. On a quiet machine the "
    "same harness gave 169.4 ms uncorrected against 169.5 ms corrected, mean "
    "delta +0.10 ms. The correction is free; it is not the reason to reject it.\n"
    "  RE-MEASURED 2026-08-30 after the fix round, same harness, load average 8 on "
    "32 cpus, 192 timed calls per arm: uncorrected 183.0 ms worst / 80.7 ms mean; "
    "per-position calibrated 181.4 / 80.3; global calibrated 178.3 / 80.3; "
    "analytic re-fit 1.1 / 0.7; analytic re-fit with the level knob 0.7 / 0.5. "
    "EVERY ARM IS INSIDE THE 243 ms GATE, the worst case with 25% headroom, and "
    "the paired median end-to-end delta of the correction is -0.03 ms — i.e. "
    "unmeasurable against scheduler noise, in the direction of noise rather than "
    "cost. The wrapper's own arithmetic timed in isolation is 2.7 us at 5 "
    "candidates and 13.5 us at 25 (median of 2,000 `apply_map` calls), which is "
    "0.001% to 0.006% of the gate. Latency is not a reason to reject either "
    "correction, and it never was; the reason is AB_RESULT_2026_HELD_OUT."
)

THE_TE_CORRECTION_PUSHES_THE_WRONG_WAY = (
    "DIRECTION WARNING, and the reason to read the TE cell twice. The rollout is "
    "OPTIMISTIC about tight ends by +0.101: it believes they last and the room "
    "takes them sooner. Correcting that LOWERS S_next at TE, RAISES "
    "``urgency = VONA * (1 - S_next)`` at TE, and makes the engine draft tight "
    "ends EARLIER. But Phase 1 measured that every one of 25 simulated drafts at "
    "slot 9 already takes EXACTLY 3 TE and 3 QB — six of sixteen picks on two "
    "one-starter lineup slots — and the new grader objective scores a third tight "
    "end at approximately zero unless he covers a bye. So the two positions this "
    "correction is largest at (TE -1.103, QB -0.554) are the two positions the "
    "roster shape says we already over-draft, and the correction pushes both the "
    "same way the pathology already runs. A calibration improvement and a "
    "decision improvement are different claims, and BOTH were measured. See "
    "DECISION_RELEVANCE_ON_REAL_ROOMS: every single pick this correction moved on "
    "the real decision windows moved ONTO a TE or a QB. It is not an academic "
    "worry — it is what the correction does: over six independent replays of the "
    "165 real decisions, 16 of the 16 picks it moved went ONTO a TE (14) or a QB "
    "(2), and none went the other way."
)

DECISION_RELEVANCE_ON_REAL_ROOMS = (
    "THE DECIDING MEASUREMENT, 2026-08-30, re-run across SIX seeds after the "
    "first pass published a single draw. Calibration is not the question; the "
    "question is whether the PICK changes and in which direction. All 165 real "
    "operator decisions from the 11 practice journals were replayed with the "
    "exact PickContext and the live-recalibrated priors the cockpit held, at the "
    "live rollouts=512, and each engine asked for its top recommendation — the "
    "whole replay repeated under six independent Monte-Carlo seed tags, because "
    "which near-tie flips is a draw, not a rate.\n"
    "  route 1, per-position correction: changed the pick in 5, 1, 2, 4, 2 and 2 "
    "of 165 — a RANGE of 1 to 5 (0.6% to 3.0%), mean 2.7 (1.6%). An earlier "
    "version of this constant quoted '5 of 165 (3.0%)' as the rate; that is the "
    "TOP of the observed range and it is one seed. Quote the range.\n"
    "  THE LOAD-BEARING HALF SURVIVES THE RE-RUN AND GETS STRONGER: over all six "
    "seeds the correction moved 16 picks in total, and 16 of 16 moved ONTO a "
    "tight end (14) or a quarterback (2), off an RB (8), a QB (7) or a WR (1). "
    "Not one change in six independent replays pushed AWAY from the 3-QB/3-TE "
    "concentration Phase 1 measured as the roster-shape pathology. A ~1.6% change "
    "rate with a 16-of-16 adverse direction is a small ONE-WAY risk, not a small "
    "two-way one.\n"
    "  the K/DST half alone: 0 of 165 at every one of the six seeds. So "
    "THE_KDST_HALF_CHANGES_NOTHING holds on REAL rooms as well as in the "
    "simulator, and the divergence play cannot be moved by this correction.\n"
    "  route 2: the SHIPPED analytic fallback disagrees with the live rollout on "
    "43 of 165 decisions (26.1%) and the sloped re-fit on 11 of 165 (6.7%) — and "
    "unlike the route-1 count these are IDENTICAL at all six seeds, which is "
    "itself the explanation of the wobble above: seeds move only the near-ties, "
    "and two engines that disagree structurally are not deciding near-ties. So "
    "the re-fit makes the degraded route behave nearly four times more like the "
    "route it is standing in for, which is the only thing a fallback is for. That "
    "is the clearest argument for correction (b) and it is a correctness "
    "argument, not a strength argument."
)

ANALYTIC_ROUTE_IS_DEAD_ON_THE_LIVE_PATH = (
    "SCOPE, verified in code 2026-08-30. ``PickEngine.survival`` defaults to "
    "None, which routes to ``engine._rollout_survival_provider`` -> "
    "``survival.rollout_survival``. Neither ``session.py`` nor ``webapp.py`` nor "
    "the ``draft-web`` CLI ever passes ``survival=``, so ``analytic_survival`` "
    "is not reachable on draft night by any path. Everything in the route-2 half "
    "of this module is therefore a correctness repair with a blast radius of "
    "zero on Monday, and must not be described as a draft-night improvement."
)


@dataclass(frozen=True)
class CalibratedRollout:
    """``engine.SurvivalProvider``: the shipped rollout + a labelled correction.

    Drop-in for ``PickEngine(survival=...)``. It calls
    ``survival.rollout_survival`` with the caller's ``rng`` exactly as
    ``engine._rollout_survival_provider`` does — same arguments, same order, same
    number of draws — so a seeded run is bit-identical to the uncorrected engine
    in everything except the returned probabilities.

    INTEGRATOR TRAP, stated because it is silent and expensive. ``PickEngine``
    threads its own ``rollouts``/``kappa``/``room_priors`` fields into the DEFAULT
    provider ONLY (``engine._ask_survival`` short-circuits the moment
    ``survival is not None``). So ``PickEngine(rollouts=512, survival=
    CalibratedRollout(...))`` runs the rollout at THIS object's ``rollouts``, and
    this object's default is the tournament budget of 128, not the live 512. A
    caller must set the count HERE — ``CalibratedRollout(calib, rollouts=512)`` —
    or the cockpit will quietly run a quarter of the rollouts it thinks it does,
    with nothing anywhere raising. The same applies to ``priors``: a live-
    recalibrated bag has to be handed to this object, not to the engine.

    ``next_best_vor`` IS PASSED THROUGH UNCORRECTED, deliberately. It is an
    expected VOR in points, not a probability, and no measurement in this repo
    scores it; propagating a survival correction into it would be an unmeasured
    second change riding on a measured first one. The consequence is disclosed
    rather than hidden: after correction ``S_next`` and ``next_best_vor`` are no
    longer two views of one simulation, so ``VONA`` keeps the rollout's opinion of
    the replacement while ``1 - S_next`` uses the corrected one.
    """

    calibration: RolloutCalibration
    rollouts: int = DEFAULT_ROLLOUTS
    kappa: float = DEFAULT_KAPPA
    priors: RoomPriors = ROOM_PRIORS_2025

    def __call__(
        self,
        ctx: PickContext,
        *,
        candidates: Sequence[BoardEntry],
        positions: Sequence[str],
        rng: "random.Random",
    ) -> SurvivalEstimate:
        result = rollout_survival(
            ctx,
            candidates,
            rng=rng,
            rollouts=self.rollouts,
            priors=self.priors,
            kappa=self.kappa,
            positions=positions,
        )
        position_of = {c.player_id: c.position for c in candidates}
        return SurvivalEstimate(
            survival=self.calibration.apply_map(
                result.survival, position_of=position_of, rollouts=result.rollouts
            ),
            next_best_vor=result.next_best_vor,
        )

    def describe(self) -> tuple[str, ...]:
        """Plain-language lines a caller must render beside any recommendation
        this provider shaped (Rule 6 — the engine's own reasons cannot say it).

        The engine builds ``PickRec.reasons`` from the numbers it is handed and has
        no idea a provider was swapped, so an injected correction is invisible to a
        novice reader unless the integrator prints these.
        """
        # Order matters: the one-line-per-position statements are what a human
        # actually reads on the clock; the paragraph caveats follow so they are
        # present in the record without burying the operative sentences.
        lines = [self.calibration.label]
        moved = sorted((p, d) for p, d in self.calibration.shifts.items() if d != 0.0)
        for pos, delta in moved:
            direction = "less likely to last" if delta < 0 else "more likely to last"
            lines.append(
                f"{pos}: treated as {direction} than the simulated room says "
                f"(logit shift {delta:+.3f}), so the engine feels "
                f"{'more' if delta < 0 else 'less'} urgency at {pos}."
            )
        fitted_at = self.calibration.fitted_rollouts
        if fitted_at is not None and fitted_at != self.rollouts:
            # Not a gate: the mismatch is small and measured. But it is exactly
            # the mismatch ``brier_and_bias`` REFUSES when scoring, so it may not
            # pass silently when applying.
            lines.append(
                f"PROVENANCE: these shifts are treated as fitted at {fitted_at} "
                f"rollouts while this provider runs at {self.rollouts}, so part of "
                f"the correction is not delivered (measured: about 13% of it at "
                f"DST, 17% at K, 2% at TE). It changes no conclusion and is said "
                f"out loud anyway — see THE_SHIFTS_WERE_FITTED_AT_R200 below."
            )
        lines.append(ROLLOUT_CALIBRATION_IS_AT_THE_NOISE_FLOOR)
        if fitted_at is not None and fitted_at != self.rollouts:
            lines.append(THE_SHIFTS_WERE_FITTED_AT_R200)
        lines.append(THE_TE_CORRECTION_PUSHES_THE_WRONG_WAY)
        lines.append(DECISION_RELEVANCE_ON_REAL_ROOMS)
        lines.append(AB_RESULT_2026_HELD_OUT)
        lines.append(AB_RESULT_2026)
        lines.append(THE_KDST_HALF_CHANGES_NOTHING)
        return tuple(lines)


# ------------------------------------------------------------- fitting route 1


def fit_logit_shifts(
    points: "Sequence[SurvivalPoint]",
    *,
    rollouts: int,
    per_position: bool = True,
    positions: Sequence[str] = POSITIONS,
    label: str = "UNLABELLED calibration — provenance unknown, do not ship",
) -> RolloutCalibration:
    """Re-derive :data:`POSITION_ROLLOUT_CALIBRATION_2026` from scored rollout rows.

    ``points`` are ``roomcheck.SurvivalPoint``s (``.predicted``, ``.survived``,
    ``.position``) — this function never touches the DB or a journal itself, so the
    caller owns the Rule-1 reads. ``rollouts`` is the R the predictions were made
    at, needed for the Jeffreys smoothing (see
    :func:`smooth_rollout_probability`); pass the same R you scored at.

    Fitted by minimising mean log-loss, which is the maximum-likelihood estimate of
    an intercept-only Platt recalibration and the reason the fit is unique: log-loss
    is strictly convex in the shift, so the deterministic bisection below cannot
    land in a local minimum. A position with no rows, or with a degenerate outcome
    (all survived / none survived), gets a shift of exactly 0.0 rather than the
    +/-infinity its likelihood actually wants — a cell that never varied has no
    correction to offer, and the engine-candidate K cell is exactly that case.
    """
    if rollouts <= 0:
        raise ValueError("fit_logit_shifts needs the rollout count the points were scored at")
    rows = [
        (str(p.position).strip().upper(), _logit(smooth_rollout_probability(p.predicted, rollouts)),
         1.0 if p.survived else 0.0)
        for p in points
    ]
    if not rows:
        raise ValueError("cannot fit a recalibration: no survival points")

    groups: dict[str, list[tuple[float, float]]] = {}
    if per_position:
        for pos, z, y in rows:
            groups.setdefault(pos, []).append((z, y))
    else:
        groups["*"] = [(z, y) for _pos, z, y in rows]

    fitted = {pos: _fit_one_shift(chunk) for pos, chunk in groups.items()}
    if per_position:
        shifts = {pos: fitted.get(pos, 0.0) for pos in positions}
        shifts.update({pos: d for pos, d in fitted.items() if pos not in shifts})
        return RolloutCalibration(shifts=shifts, default=0.0, label=label)
    shared = fitted["*"]
    return RolloutCalibration(
        shifts={pos: shared for pos in positions}, default=shared, label=label
    )


def _fit_one_shift(rows: Sequence[tuple[float, float]], *, bound: float = 12.0) -> float:
    """MLE logit shift for one cell, by deterministic bisection on the gradient.

    ``d/d_delta mean_logloss = mean(sigmoid(z + delta) - y)`` is continuous and
    strictly increasing in ``delta``, so a sign change brackets the unique root.
    No sign change inside ``+/- bound`` means the cell is degenerate (every row
    survived, or none did) and the likelihood is maximised at an asymptote; 0.0 is
    returned instead, because "this cell never varied" is not evidence of a shift.
    """
    if not rows:
        return 0.0

    def gradient(delta: float) -> float:
        return statistics.fmean(_sigmoid(z + delta) - y for z, y in rows)

    lo, hi = -bound, bound
    g_lo, g_hi = gradient(lo), gradient(hi)
    if g_lo > 0.0 or g_hi < 0.0:
        return 0.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if gradient(mid) < 0.0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def brier_and_bias(
    points: "Sequence[SurvivalPoint]",
    *,
    calibration: RolloutCalibration | None = None,
    rollouts: int = 0,
) -> tuple[float, float, int]:
    """``(brier, bias, n)`` for a cohort, optionally under a correction.

    ``bias = mean(predicted) - mean(observed)``, the same sign convention as
    ``roomcheck.Calibration.bias``: POSITIVE means optimistic (the model says they
    last more often than they do, so the engine waits too long).

    ``rollouts`` is REQUIRED whenever a ``calibration`` is passed, and refusing the
    default instead of quietly using it is the point: the correction's size at the
    endpoints depends entirely on the smoothing, so scoring at R=0 while the
    predictions came from R=200 silently reports a DIFFERENT correction's Brier.
    """
    if not points:
        raise ValueError("cannot score an empty cohort")
    if calibration is not None and rollouts <= 0:
        raise ValueError(
            "scoring under a calibration needs the rollout count the predictions "
            "were made at — the smoothing, and so the correction, depends on it"
        )
    br = bs = 0.0
    for p in points:
        q = float(p.predicted)
        if calibration is not None:
            q = calibration.apply(q, position=p.position, rollouts=rollouts)
        y = 1.0 if p.survived else 0.0
        br += (q - y) ** 2
        bs += q - y
    n = len(points)
    return br / n, bs / n, n


# ======================================================================
#     route 2 — the analytic fallback, re-fitted WITH a K/DST rank slope
# ======================================================================


@dataclass(frozen=True)
class SlopedSurvivalParams:
    """``survival.SurvivalParams`` with a RANK SLOPE for K and DST.

    The shipped shape gives K and DST one flat center each, i.e. it asserts that
    when the room takes a kicker does not depend on WHICH kicker. That is the
    wrong functional form, and the evidence is a likelihood improvement of the
    size you only get from a missing term. Against a FLAT RE-FIT (i.e. the slope
    is the only difference, one extra parameter each): K NLL 607.7 -> 348.4 on 634
    rows / 99 room events, DST 574.0 -> 382.2 on 352 rows / 100 events. Against
    the SHIPPED constants the same fits are 1391.9 -> 348.4 and 1601.6 -> 382.2,
    which is the number :attr:`SlopedFit.improvement` reports because that is the
    baseline it defaults to. Both are recomputable by :func:`fit_sloped_params`;
    ``roomcheck.fit_survival_params`` prints the flat-vs-sloped pair directly.

    THE MECHANISM, because the numbers look alarming without it. The board carries
    58 rank-eligible kickers and 32 defenses, of which about 9 of each are drafted
    per room. A flat center must explain the ~49 kickers who are NEVER taken as
    well as the 9 who are, so it is dragged past the end of the draft (the flat
    re-fit lands at 176.65, past pick 160). A rank slope explains the deep ones
    with the slope and frees the intercept to fit the ones that actually go.

    DO NOT READ THE INTERCEPT AS A PREDICTION. ``k_center_intercept`` 53.73 is the
    center at rank 0 and there is no rank-0 kicker: the best-ranked K on the live
    2026 board is rank 146 (center 113.6) and the best DST is rank 179 (center
    128.8). Those are the only values the model is ever evaluated at.

    AND DO NOT READ IT AS A REASON TO CHASE ADP AT K/DST. This is fitted on real
    10-team room TIMING, not on ESPN ADP, which Phase 1 established is
    ceiling-censored at 170 and whose relationship to real room timing collapses
    at exactly K and DST. It moves the top kicker's center from the shipped 156.5
    to 113.6, which is EARLIER, and the divergence play (K/DST around rounds 9-10)
    survives that unchanged in direction — but a reader should know the shape
    change makes the top K/DST look scarcer, not less scarce.
    """

    skill_center_intercept: float = DEFAULT_SURVIVAL_PARAMS.skill_center_intercept
    skill_center_slope: float = DEFAULT_SURVIVAL_PARAMS.skill_center_slope
    skill_width: float = DEFAULT_SURVIVAL_PARAMS.skill_width
    k_center_intercept: float = DEFAULT_SURVIVAL_PARAMS.k_center
    k_center_slope: float = 0.0
    k_width: float = DEFAULT_SURVIVAL_PARAMS.k_width
    dst_center_intercept: float = DEFAULT_SURVIVAL_PARAMS.dst_center
    dst_center_slope: float = 0.0
    dst_width: float = DEFAULT_SURVIVAL_PARAMS.dst_width

    @classmethod
    def from_flat(cls, params: SurvivalParams) -> "SlopedSurvivalParams":
        """Lift a shipped :class:`SurvivalParams` into this shape with zero slopes.

        Exact: with both slopes 0 this shape reproduces ``analytic_survival``
        pointwise, which is what makes "did the SHAPE help, or just the constants?"
        an answerable question rather than a confounded one.
        """
        return cls(
            skill_center_intercept=params.skill_center_intercept,
            skill_center_slope=params.skill_center_slope,
            skill_width=params.skill_width,
            k_center_intercept=params.k_center,
            k_center_slope=0.0,
            k_width=params.k_width,
            dst_center_intercept=params.dst_center,
            dst_center_slope=0.0,
            dst_width=params.dst_width,
        )


#: ALTERNATIVE, NOT A DEFAULT (Rule 6). Censored-MLE re-fit of the analytic
#: fallback on all 11 real practice rooms, in the SLOPED shape. Reproduced
#: independently here from ``roomcheck.survival_observations`` + its Nelder-Mead;
#: the skill row is identical to ``roomcheck.REFIT_PRACTICE_2026`` (same rows,
#: same likelihood, unchanged shape) and the K/DST rows are this module's addition.
#:
#: Held-out (leave-one-room-out, analytic route scored UNCONDITIONALLY exactly as
#: ``PickEngine`` consumes it, 3,942 rows):
#:
#:     shipped constants          Brier 0.3454   bias -0.3700
#:     flat re-fit                Brier 0.1319   bias -0.0427   better in 11/11 rooms
#:     REFIT_PRACTICE_2026        Brier 0.1312   bias -0.0426   better in 11/11 rooms
#:     THIS (sloped re-fit)       Brier 0.1275   bias -0.0542   better in 11/11 rooms
#:
#: and sloped beats flat in 9 of 11 rooms (mean Brier -0.0044). Unlike route 1
#: this route is DETERMINISTIC — there is no Monte-Carlo noise floor to clear, so
#: a 0.0044 improvement is a real 0.0044.
#:
#: NOT A HUMAN FINDING: ``roomcheck.KDST_SHAPE_IS_MISSPECIFIED`` control-checked
#: the same shape against all-bot simulated rooms and found the SAME improvement,
#: so this is a repair of a form that never fitted our own simulator either — which
#: is precisely what makes it safe to adopt. See
#: :data:`ANALYTIC_ROUTE_IS_DEAD_ON_THE_LIVE_PATH` for the blast radius (zero).
#:
#: AND THE HONEST NEGATIVE THAT SITS BESIDE IT: better calibrated is not the same
#: as better decisions. Injected as a route-2 provider and A/B'd against the
#: shipped flat constants on the same route, these constants LOSE — -0.034
#: expected wins, CI -0.062 .. -0.006, and unfillable weeks 0.25 -> 0.68. Adopt
#: this as a repair of a wrong functional form, which it demonstrably is; do not
#: adopt it expecting better picks. :data:`CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY`
#: has the numbers and the explanation I tried and could not make stick.
REFIT_SLOPED_PRACTICE_2026 = SlopedSurvivalParams(
    skill_center_intercept=5.374,
    skill_center_slope=0.8208,
    skill_width=4.670,
    k_center_intercept=53.73,
    k_center_slope=0.4100,
    k_width=3.51,
    dst_center_intercept=65.03,
    dst_center_slope=0.3561,
    dst_width=4.59,
)

REFIT_SLOPED_LABEL = (
    "HYPOTHESIS (not shipped): analytic-fallback survival constants re-fitted by "
    "censored maximum likelihood on 11 completed real 10-team ESPN practice "
    "drafts (2026-08-16..29), in a shape that gives K and DST a rank slope the "
    "shipped model does not have. Held-out leave-one-room-out Brier 0.1275 "
    "against the shipped constants' 0.3454 and a flat re-fit's 0.1319; better "
    "than shipped in 11 of 11 rooms and better than flat in 9 of 11. The shape "
    "half of that gain reproduces against all-bot simulated rooms "
    "(roomcheck.KDST_SHAPE_IS_MISSPECIFIED), so it is a repair of our own model, "
    "not a claim about how humans rank kickers. CANNOT AFFECT DRAFT NIGHT: the "
    "analytic route is unreachable on the live path. AND IT DOES NOT PICK BETTER: "
    "A/B'd on the same route against the shipped constants it LOSES 0.034 "
    "expected wins with the interval below zero "
    "(CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY). Ship it as a correctness "
    "repair or not at all."
)

#: Mirrors ``survival._FALLBACK_RANK_BASE`` / ``simulator._FALLBACK_BASE`` /
#: ``roomcheck.FALLBACK_RANK_BASE``: board-unranked players sit at or beyond this
#: rank, the room never considers them, and their survival is ~1.
FALLBACK_RANK_BASE = 10_000

_DST_ALIASES = frozenset({"DST", "D/ST", "DEF"})


def sloped_analytic_survival(
    espn_overall_rank: int,
    position: str,
    overall_next_pick: int,
    *,
    params: SlopedSurvivalParams = REFIT_SLOPED_PRACTICE_2026,
    logit_shift: float = 0.0,
) -> float:
    """``S = sigmoid((center(rank, position) - O_next) / width + logit_shift)``.

    Byte-for-byte the shipped ``survival.analytic_survival`` when ``params`` has
    both K/DST slopes at 0 and ``logit_shift`` is 0 (see
    :meth:`SlopedSurvivalParams.from_flat`); the only change is that ``center`` is
    now linear in rank for K and DST too.

    ``logit_shift`` is the LEVEL knob, and it exists because the level turned out
    to be the whole story on this route: a NEGATIVE value makes every player less
    likely to last (uniform pessimism), and because the argument of the sigmoid is
    already a logit, ``logit_shift`` is exactly "move the centre ``logit_shift``
    widths earlier". It defaults to 0.0, so every published constant and every
    existing caller is unchanged. See
    :data:`THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL` for what it measured.

    Unranked/fallback SKILL players return 1.0, as the shipped function does, and
    the shift is deliberately NOT applied to that shortcut: it means "the room does
    not know this player exists", which is a statement about the board rather than
    a probability the level knob has any claim on. K and DST at a fallback rank do
    NOT get the shortcut and fall through the slope, because a fallback rank there
    means "off the ESPN board", and a defense off the board with a slope this steep
    is already at survival ~1 by arithmetic — a special case would be a second,
    silent way to say the same thing.
    """
    pos = str(position).strip().upper()
    if pos == "K":
        center = params.k_center_intercept + params.k_center_slope * espn_overall_rank
        width = params.k_width
    elif pos in _DST_ALIASES:
        center = params.dst_center_intercept + params.dst_center_slope * espn_overall_rank
        width = params.dst_width
    else:
        if espn_overall_rank >= FALLBACK_RANK_BASE:
            return 1.0
        center = params.skill_center_intercept + params.skill_center_slope * espn_overall_rank
        width = params.skill_width
    return _sigmoid((center - overall_next_pick) / max(width, 1e-9) + logit_shift)


def expected_next_best_vor(
    ctx: PickContext,
    position: str,
    overall_next_pick: int,
    *,
    params: SlopedSurvivalParams = REFIT_SLOPED_PRACTICE_2026,
    depth: int = 40,
    logit_shift: float = 0.0,
) -> float:
    """Expected best-available VOR at ``position`` at the operator's next pick.

    The piece ``survival.analytic_survival`` never had, and the reason route 2 was
    not actually injectable: ``engine.SurvivalEstimate`` needs BOTH survival and
    ``next_best_vor``, and the analytic function answers only the first.

    Walks the position's available entries in VOR order and takes the expectation
    of the first survivor under INDEPENDENT survival::

        E[best] = sum_i vor_i * S_i * prod_{j<i} (1 - S_j)

    Independence is a real approximation and it is the honest one available without
    a simulation: the rollout gets the dependence right (that is route 1's whole
    advantage) and an analytic fallback that pretended to would be inventing a
    correlation nobody measured. ``depth`` bounds the walk; the residual tail mass
    after 40 candidates is negligible because the product collapses geometrically.
    Returns 0.0 for a position with nothing available, matching the rollout.

    ``logit_shift`` is threaded straight through to
    :func:`sloped_analytic_survival`, and threading it here rather than only at the
    survival map is the entire point of the knob: pessimism that reaches BOTH
    halves lowers the expected replacement, which RAISES
    ``VONA = best_now.vor - next_best_vor``, which is multiplied by the ALSO-raised
    ``1 - S_next``. That two-sided leverage is what
    :data:`THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL` measured, and it is the
    lever route 1's wrapper structurally cannot pull.

    Already-drafted players are excluded: ``ctx.state.taken`` grows all draft and a
    replacement who is already on somebody's roster is not a replacement. Leaving
    them in inflates ``next_best_vor``, which deflates VONA and understates urgency
    by more and more as the draft goes on.
    """
    entries = sorted(
        (e for e in ctx.state.all_entries()
         if e.position == position and e.player_id not in ctx.state.taken),
        key=lambda e: -e.vor,
    )[: max(1, depth)]
    total = 0.0
    gone = 1.0
    for e in entries:
        s = sloped_analytic_survival(
            e.espn_overall_rank,
            e.position,
            overall_next_pick,
            params=params,
            logit_shift=logit_shift,
        )
        total += float(e.vor) * s * gone
        gone *= 1.0 - s
        if gone <= 1e-9:
            break
    return total


@dataclass(frozen=True)
class AnalyticSurvival:
    """``engine.SurvivalProvider`` for route 2 — deterministic, no rollouts.

    The offline/degraded-mode fallback made whole: it answers both halves of
    :class:`~ziggurat.draft.engine.SurvivalEstimate` with no Monte Carlo, so it is
    ROUGHLY 100x cheaper than the rollout and bit-identical run to run. It ignores
    the ``rng`` it is handed (and draws nothing from it), which is deliberate — a
    caller can swap it in mid-session without perturbing any other seat's stream.

    THE 100x IS MEASURED, and an earlier draft of this docstring said 1,000x,
    which was wrong by an order of magnitude and contradicted this module's own
    :data:`LATENCY_2026`. Re-measured 2026-08-30 on the live 3,264-row board, both
    providers timed on the IDENTICAL board state at every one of 16 on-clock
    decisions in a real slot-9 draft: rollout at R=512 80.6 ms mean / 175.3 ms
    worst against this route's 0.7 ms mean / 0.9 ms worst — 111x on the mean.
    Budget a degraded route at about a millisecond a decision, not a microsecond.

    NOT FOR DRAFT NIGHT. It cannot see position runs, opponent need, or the K/DST
    round window (:data:`ANALYTIC_ROUTE_IS_DEAD_ON_THE_LIVE_PATH` explains why the
    live path never reaches it anyway). Its purpose is a fast offline route and a
    cross-check on route 1.
    """

    params: SlopedSurvivalParams = REFIT_SLOPED_PRACTICE_2026
    label: str = REFIT_SLOPED_LABEL
    logit_shift: float = 0.0

    def __call__(
        self,
        ctx: PickContext,
        *,
        candidates: Sequence[BoardEntry],
        positions: Sequence[str],
        rng: "random.Random | None" = None,
    ) -> SurvivalEstimate:
        upcoming = upcoming_opponent_picks(ctx)
        if not upcoming:
            # The operator picks again immediately (snake turn / last round):
            # everyone survives and the "next best" is today's best. Mirrors
            # ``rollout_survival``'s own zero-window branch exactly.
            next_best = {}
            for pos in positions:
                e = ctx.state.front_vor(pos)
                next_best[pos] = float(e.vor) if e is not None else 0.0
            return SurvivalEstimate(
                survival={c.player_id: 1.0 for c in candidates}, next_best_vor=next_best
            )
        # THE HORIZON IS THE OPERATOR'S OWN NEXT PICK, not the room's next pick.
        # ``upcoming_opponent_picks`` returns every intervening rival pick, so the
        # last of them plus one IS our next turn. Asking about ``upcoming[0][0]``
        # instead would answer "does he last one more pick", which is not what
        # survival means and is wrong by most of the gap: on the operator's real
        # 12 -> 29 window (16 intervening picks) a rank-30 RB moves 0.553 -> 0.974.
        next_pick = upcoming[-1][0] + 1
        return SurvivalEstimate(
            survival={
                c.player_id: sloped_analytic_survival(
                    c.espn_overall_rank,
                    c.position,
                    next_pick,
                    params=self.params,
                    logit_shift=self.logit_shift,
                )
                for c in candidates
            },
            next_best_vor={
                pos: expected_next_best_vor(
                    ctx, pos, next_pick, params=self.params, logit_shift=self.logit_shift
                )
                for pos in positions
            },
        )

    def describe(self) -> tuple[str, ...]:
        lines = [self.label]
        if self.logit_shift != 0.0:
            direction = "LESS" if self.logit_shift < 0 else "MORE"
            lines.append(
                f"LEVEL KNOB ENGAGED: every player is treated as {direction} likely "
                f"to last than these constants say (uniform logit shift "
                f"{self.logit_shift:+.2f}, i.e. the centre moved "
                f"{abs(self.logit_shift):.2f} widths "
                f"{'earlier' if self.logit_shift < 0 else 'later'}). This is a "
                f"deliberate MIScalibration, not a forecast: it exists to price the "
                f"urgency-scale effect measured in "
                f"THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL, and a caller who "
                f"leaves it engaged is running an urgency knob, not a survival model."
            )
        lines.append(ANALYTIC_ROUTE_IS_DEAD_ON_THE_LIVE_PATH)
        lines.append(DECISION_RELEVANCE_ON_REAL_ROOMS)
        lines.append(CALIBRATING_ROUTE_2_COSTS_DECISION_QUALITY)
        lines.append(THE_ANALYTIC_ROUTES_ADVANTAGE_IS_ITS_LEVEL)
        lines.append(WHAT_THE_SHIPPED_ANALYTIC_ARM_ACTUALLY_IS)
        return tuple(lines)


def score_analytic(
    rows: Sequence[tuple[int, str, int, bool]],
    *,
    params: SlopedSurvivalParams = REFIT_SLOPED_PRACTICE_2026,
) -> tuple[float, float, int]:
    """``(brier, bias, n)`` for the analytic route over scored decision rows.

    ``rows`` are ``(espn_overall_rank, position, next_pick, survived)`` — the four
    fields a ``roomcheck.DecisionWindow`` probe yields, flattened to plain tuples
    so this is testable without a journal and so the caller keeps the Rule-1 read.
    The quantity scored is UNCONDITIONAL ``S(next_pick)``, which is exactly how
    ``PickEngine`` consumes it and is therefore pessimistic by construction against
    an outcome that is conditional on being available now — the same shape defect
    ``roomcheck.ANALYTIC_ROUTE_IS_UNCONDITIONAL`` measures at ~0.10 of the shipped
    route's headline bias. It is scored this way anyway because it is what the
    cockpit's fallback would actually say.

    This is the function that makes the leave-one-room-out figures quoted on
    :data:`REFIT_SLOPED_PRACTICE_2026` reproducible rather than remembered.
    """
    if not rows:
        raise ValueError("cannot score an empty cohort")
    br = bs = 0.0
    for rank, position, next_pick, survived in rows:
        q = sloped_analytic_survival(int(rank), position, int(next_pick), params=params)
        y = 1.0 if survived else 0.0
        br += (q - y) ** 2
        bs += q - y
    n = len(rows)
    return br / n, bs / n, n


# ------------------------------------------------------------- fitting route 2


@dataclass(frozen=True)
class SlopedFit:
    """A :func:`fit_sloped_params` result plus the likelihoods that justify it."""

    params: SlopedSurvivalParams
    baseline: SlopedSurvivalParams
    nll: float
    nll_baseline: float
    n: int
    n_events: int
    per_group: Mapping[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def improvement(self) -> float:
        """Log-likelihood bought over ``baseline`` (higher is better)."""
        return self.nll_baseline - self.nll


def fit_sloped_params(
    observations: "Sequence[SurvivalObservation]",
    *,
    baseline: SlopedSurvivalParams = None,  # type: ignore[assignment]
) -> SlopedFit:
    """Censored-MLE re-fit of :class:`SlopedSurvivalParams` (recomputes the constants).

    Reuses ``roomcheck``'s likelihood and optimizer rather than re-deriving them, so
    the skill group here is provably the SAME estimator that produced
    ``roomcheck.REFIT_PRACTICE_2026`` and any difference between the two is a
    difference in the data, not in the arithmetic. The censoring is the whole
    problem and ``roomcheck.SurvivalObservation`` already encodes it (the operator's
    own picks are censored, not events).

    ``observations`` are caller-supplied; this function performs no DB read, so the
    Rule-1 ``as_of`` lives at ``roomcheck.load_journal_board``, where it is already
    keyword-only and leakage-tested.
    """
    from ziggurat.draft.roomcheck import (  # sibling-owned estimator (Rule 8)
        SKILL_POSITIONS,
        _negative_log_likelihood,
        _nelder_mead,
    )

    if baseline is None:
        baseline = SlopedSurvivalParams.from_flat(DEFAULT_SURVIVAL_PARAMS)

    def _pos(o) -> str:
        return str(o.position).strip().upper()

    skill = [o for o in observations if _pos(o) in SKILL_POSITIONS]
    kicker = [o for o in observations if _pos(o) == "K"]
    defense = [o for o in observations if _pos(o) in _DST_ALIASES]
    for name, rows in (("skill", skill), ("K", kicker), ("DST", defense)):
        if not any(not o.censored for o in rows):
            raise ValueError(f"cannot fit {name}: no uncensored room picks in the sample")

    def linear(rows, start):
        def objective(p):
            return _negative_log_likelihood(rows, lambda r: p[0] + p[1] * r, p[2])

        return _nelder_mead(objective, start, (3.0, 0.05, 1.0)), objective

    (sv, s_nll), s_obj = linear(
        skill,
        (baseline.skill_center_intercept, baseline.skill_center_slope, baseline.skill_width),
    )
    (kv, k_nll), k_obj = linear(
        kicker, (baseline.k_center_intercept, baseline.k_center_slope, baseline.k_width)
    )
    (dv, d_nll), d_obj = linear(
        defense, (baseline.dst_center_intercept, baseline.dst_center_slope, baseline.dst_width)
    )

    base_s = s_obj(
        (baseline.skill_center_intercept, baseline.skill_center_slope, baseline.skill_width)
    )
    base_k = k_obj((baseline.k_center_intercept, baseline.k_center_slope, baseline.k_width))
    base_d = d_obj((baseline.dst_center_intercept, baseline.dst_center_slope, baseline.dst_width))
    rows = list(observations)
    return SlopedFit(
        params=SlopedSurvivalParams(
            skill_center_intercept=sv[0],
            skill_center_slope=sv[1],
            skill_width=sv[2],
            k_center_intercept=kv[0],
            k_center_slope=kv[1],
            k_width=kv[2],
            dst_center_intercept=dv[0],
            dst_center_slope=dv[1],
            dst_width=dv[2],
        ),
        baseline=baseline,
        nll=s_nll + k_nll + d_nll,
        nll_baseline=base_s + base_k + base_d,
        n=len(rows),
        n_events=sum(1 for o in rows if not o.censored),
        per_group={
            "skill": (s_nll, base_s),
            "K": (k_nll, base_k),
            "DST": (d_nll, base_d),
        },
    )
