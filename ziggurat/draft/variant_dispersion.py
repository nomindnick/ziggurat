"""VARIANT: per-player market dispersion in the engine's risk term.

Import-quarantined package (Rule 8), ADDITIVE and OPT-IN. Nothing here is imported by
``engine.py``, ``session.py``, ``webapp.py`` or the CLI; the shipped default path
is untouched, and :class:`DispersionRiskPicker` is a WRAPPING
:class:`~ziggurat.draft.bots.Picker` that a Phase-3 integrator wires in behind a
flag or does not wire in at all.

======================================================================
THE CONCLUSION, STATED BEFORE THE BUILD (the question this item was told
to resolve first): the two POSITIONAL vectors are NOT the same quantity
and must never be swapped for one another — but the PER-PLAYER band is
the upgrade ``engine.py`` itself specified, and that is what this module
installs.
======================================================================

``engine.POSITIONAL_DISPERSION_PRIOR`` is ``{RB 0.40, QB 0.60, WR 1.00, TE 1.00,
DST 0.0, K 0.0}``, cited to hypothesis H10: "RB is the most volume-predictable,
therefore the highest floor, therefore the LOWEST dispersion". That is a claim
about WEEK-TO-WEEK PREDICTABILITY OF A ROLE. ``dispersion.PlayerDispersion.
relative_dispersion`` is a per-player MARKET BAND — how far apart an expert panel
is about what one player's SEASON is worth — converted through the house
rank-to-points curve and normalised to the cohort median.

ITS POSITIONAL MEDIANS DEPEND ON THE COHORT YOU TAKE THEM OVER, and this module
quotes three cohorts rather than one number, because an earlier draft of it
quoted the widest and labelled it "on this board" (audit finding 8). Live board
of 2026-08-30, market-priced rows only:

    cohort                    RB     WR     TE     QB    DST      K
    ESPN top-160 (drafted)  1.235  0.982  0.997  0.507    ---  0.067  <- the one
                                                                        this
                                                                        module
                                                                        centres
                                                                        and
                                                                        rescales
                                                                        on
    ESPN top-250            1.398  1.100  1.024  0.507  0.856  0.226
    whole joined board      1.228  1.173  0.792  0.631  0.721  0.258

Two things to read off that table. First, EVERY cohort inverts H10 on RB (the
market band is WIDEST at RB, where the legacy proxy is lowest), so the
conclusion below does not depend on which one you take. Second, the drafted
cohort — the only players a 16-round 10-team draft can reach — contains ONE
kicker and NO defense at all, which is why measurement 3 below is quoted at
top-250 and why K/DST are held out of the rescale by default.

FOUR MEASUREMENTS SAY THOSE ARE DIFFERENT QUESTIONS (all on the live board of
2026-08-30, season 2026, ESPN rank <= 250, n=187 market-banded candidates,
reproducible from :func:`describe_quantities`):

  1. ACROSS PLAYERS THE TWO SPREADS RANK PEOPLE IN OPPOSITE DIRECTIONS. The
     market-disagreement component and the week-to-week-noise component of the
     SAME season sigma correlate at Pearson **-0.40** (Spearman -0.41). A player
     the panel argues about is, if anything, a player whose weekly line is
     STEADY. Two quantities with a negative rank correlation are not two
     measurements of one thing.
  2. THEY ARE NOT EVEN THE SAME SIZE. Median market sigma 14.5 season points
     against median weekly-noise sigma 27.6 — the market band is a MINORITY
     (median 33%) of a player's season spread. This is the Gibbs warning
     generalised: a 3.3-point unanimous band next to 44.5 points of weekly noise.
  3. LEGACY'S K/DST ZEROS ARE AN ABSTENTION, NOT A MEASUREMENT OF ZERO. The
     engine's own comment says K/DST are "OUT OF SCOPE for draft-day
     floor/ceiling (their week-to-week variance dwarfs any draft-day signal)".
     Measured, that comment is right and the substitution would be exactly
     backwards: D/ST has the HIGHEST weekly coefficient of variation of the six
     positions (0.93 against RB 0.62, WR 0.61, QB 0.43). Writing a measured 0.86
     (the top-250 D/ST median; there is no D/ST inside the top 160 at all) into a
     slot that currently means "we decline to answer" converts an abstention into
     an assertion, in the one place the abstention was correct.
  4. THE POSITIONAL SUMMARY IS NOT WHERE THE INFORMATION IS. **68.1%** of the
     variance in ``relative_dispersion`` is WITHIN position, only 31.9% between.
     A six-number positional vector — either vector — throws away two thirds of
     the signal. The upgrade worth having is GRANULARITY, not a re-ordering of
     six constants.

WHAT WEAKENS 1 AND 3, DISCLOSED RATHER THAN LEFT FOR A READER TO FIND: the
week-to-week side of both is ``lineup_support.DEFAULT_VARIANCE``, an AFFINE MODEL
OF THE PROJECTED MEAN, not an independent per-player measurement. So the -0.40 is
partly mechanical — a high-mu star gets a large modelled weekly sigma and, being a
consensus pick, a tight market band — and the D/ST coefficient of variation in 3
is a modelled number too, not a direct observation of D/ST weeks. The mechanism
does not rescue the substitution (a quantity that is a smooth function of the
projection is EXACTLY not the new information a per-player band would add, and 4
is measured directly off the market rows either way), but it does mean 1 and 3
are evidence about how this system computes the two spreads, not two independent
observations of the world. 2 and 4 do not depend on the variance model at all.

AND ONE THAT SAYS THE PER-PLAYER BAND IS THE RIGHT UPGRADE ANYWAY:
``engine.py``'s own note on the proxy reads "When ``adp_rankings`` is later
populated this upgrades to per-player ``(worst - best)/4`` behind a populated-
table check". ``dispersion.py`` computes precisely that (``RANGE_TO_SIGMA_
DIVISOR`` = 4.0) with the populated-table check (``Floors``) already built. The
positional vector was always a STAND-IN for the per-player band; using the band
is the documented intent, and it is defensible on the merits — uncertainty about
a player's season RATE does not average out over 14 head-to-head weeks, which is
exactly what "an early bust is unrecoverable" is worried about, whereas weekly
noise partly does.

READ THE TWO MODES AS AN EXPERIMENT, NOT AS TWO PRODUCTS.
``relative_dispersion`` carries BOTH parts of the disagreement: the positional
level (RB highest, K lowest) and the within-position deviation. So
MODE_PER_PLAYER bundles the granularity upgrade WITH the positional re-ordering
the four measurements above say is a different question, while MODE_CENTERED
isolates the granularity upgrade alone. That makes the A/B interpretable rather
than merely scored: if per-player wins and centered does not, the margin is
coming from the positional inversion — i.e. from re-answering H10 with a
measurement of something else — and should not be believed on this evidence. If
centered wins, the per-player signal is real. If neither moves, the risk term was
never load-bearing and the whole question is moot.

THAT READING RULE ONLY WORKS IF MODE_CENTERED IS ACTUALLY POSITION-NEUTRAL, and
in the first build it was not (audit findings 1 and 4). It centred every player
on the positional median of the WHOLE joined board while the rescale was fitted
on the ESPN top-160 the draft can actually reach — two different cohorts, so the
"deviation" carried a residual positional tilt of up to 0.85 risk points (TE
+0.171, QB -0.071, WR -0.049, RB +0.043 in mean applied dispersion, against the
legacy constants). A centered arm that re-tilts positions cannot support the
inference the mode exists for. FIXED: both the centering constant and the
unjoined-player fallback are now the median of the DRAFTED cohort — the same
players the rescale is fitted on — so over that cohort the residual mean shift
falls to +0.002..+0.063 (measured; see :func:`describe_centering`). It is not
exactly zero because centring on a MEDIAN does not zero a MEAN, and that residual
is stated rather than rounded away.

SO THIS MODULE DOES THREE THINGS AND REFUSES A FOURTH:
  * MODE_PER_PLAYER replaces the positional constant with the rescaled
    per-player band. This is the shipped-intent upgrade.
  * MODE_CENTERED keeps the swept positional posture and adds ONLY the
    within-position deviation ``relative(p) - median_relative(pos)`` over the
    drafted cohort. A player the market board cannot reach falls back to a
    deviation of exactly 0, i.e. to shipped behaviour, by construction.
  * MODE_LEGACY reproduces ``PickEngine`` bit-for-bit. It is not a feature, it is
    the CONTROL: ``tests/test_variant_dispersion.py`` proves the re-scoring
    arithmetic by showing that a variant whose dispersion function returns the
    legacy value picks identically, and ``paired_compare`` returns exactly 0.0.
  * IT REFUSES to swap ``POSITIONAL_BAND_PRIOR`` in for
    ``POSITIONAL_DISPERSION_PRIOR`` as a six-number vector. That is the category
    error above, and it is not offered as a mode.

ONLY A MEASURED BAND IS A BAND (audit finding 2 — the one major). When the
FantasyPros ECR scrape behind ``dispersion`` is missing or too thin,
``build_dispersion`` still returns a full board: every row carries
``relative_source='positional_prior'``, i.e. the frozen six-number
``POSITIONAL_BAND_PRIOR`` constant wearing a per-player row's clothes. The first
build joined those rows into ``values`` like any other, so with an EMPTY market
feed MODE_PER_PLAYER silently became exactly the six-number positional-vector
swap this module refuses to offer as a mode — and at roughly twice the intended
amplitude, because the sd-match cohort collapsed onto six constants and the scale
rose 0.588 -> 1.188 (effective risk weight 2.94 -> 5.94). Nothing raised, and
``format_risk_bands`` printed "100.0% coverage" because it was counting JOINS.
Measured live by stubbing the feed empty, and again at 25 rows. FIXED THREE WAYS:
``values`` now holds MARKET-priced bands only (a ``positional_prior`` row takes
the labelled positional fallback like any unjoined player, which in MODE_CENTERED
is exactly shipped behaviour); the sd-match refuses a cohort without
``min_cohort`` market rows behind it and falls back to the frozen anchor with a
banner; and the picker's construction guard is keyed on MARKET COVERAGE OF THE
DRAFTED RANGE (:data:`MIN_DRAFTED_MARKET_ROWS`,
:data:`MIN_DRAFTED_MARKET_COVERAGE`), not on the band map being non-empty.
``drafted_coverage`` now means market coverage; the join count is
``drafted_join_coverage`` and both are printed. This matters on draft night:
``adp_rankings`` is one of the four sources CLAUDE.md names as serving the
CURRENT value only, so a stale or empty market board at 19:00 is the realistic
case, not a contrived one.

THE RESCALE, AND WHY IT IS SD-MATCHING (mandatory scaling care). The legacy proxy
lives in [0, 1]; ``relative_dispersion`` spans [0.06, 2.59] with median 1.0. At
the shipped ``b_risk = 5.0`` a naive substitution is a +/-13-point tilt where the
proxy gave +/-5 — a 2.6x amplification of the risk term that no sweep chose. What
the score actually reads is not the LEVEL of the dispersion vector (a constant
offset across every candidate cancels in a ranking) but its SPREAD ACROSS THE
CANDIDATES BEING COMPARED. So the rescale matches that: ``scale = sd(legacy over
the cohort) / sd(measured over the same cohort)``, computed on the drafted range
(ESPN top ``DRAFTED_RANK_DEPTH``) of the board being priced, under the SAME K/DST
policy on both sides so the policy cannot distort it. Measured on the live board of
2026-08-30 (130 market-priced players inside the ESPN top 160): legacy sd 0.283,
measured sd 0.482, **scale 0.588** — an effective risk weight of ``5.0 x 0.588 =
2.94`` on a quantity whose median is 1.0, against the proxy's 4.0
for a WR and 2.0 for an RB. The scale is recomputed from the cohort at build time
(the ``cohort_sd_match`` path) and falls back to the frozen
:data:`SD_MATCH_SCALE_ANCHOR` with a banner when the cohort is too thin or has no
market rows behind it, the same cohort-median/frozen-prior discipline
``dispersion.build_dispersion`` uses for its reference band. ``scale=`` on the
picker overrides it, which is what the sweep does; ``scale =
SD_MATCH_SCALE_ANCHOR x NAIVE_SCALE_MULTIPLE`` = 1.0 is the naive substitution.

K/DST: THE ABSTENTION IS KEPT (``kdst_abstain=True`` by default). Measurement 3
above is the reason of principle; two more are reasons of fact. Phase 1 measured
that the market's relationship to real room timing COLLAPSES at exactly these two
positions (skill b=0.868 R^2=0.973; DST b=0.208; K b=0.140 over 11 real 10-team
rooms), so the panel this band comes from is the one source with no demonstrated
purchase there. And the house K board is currently understated by ~40 season
points by the confirmed ``projections._KICKER_DIRECT_MAP`` ``fgm_50p`` bug, so a
K risk term would be a spread priced off a level that is known wrong. The
alternative is BUILT and MEASURED anyway (``kdst_abstain=False``) rather than
asserted away — see the item's A/B.

WHAT THIS CANNOT DO, said up front. The engine's candidate set is at most
``candidate_width`` entries by ESPN rank across all allowed positions PLUS the
best-by-VOR at each — measured 8 to 10 players on the live board. A re-ranking
variant can only reorder those. It cannot pull in a player the gather never
reached, and ``risk_sign`` tapers to 0.0 at round 10, so the term this variant
changes is near-zero in rounds 8-12 by construction.

Rule 1: the one DB seam is :func:`build_risk_bands`, whose ``as_of`` is
keyword-only with no default and is threaded, with ``view``, straight into
``dispersion.build_dispersion``. Rule 2: no scoring constant lives here; every
point comes from ``dispersion`` -> ``valuation`` -> ``scoring``. A dispersion
scale is not a scoring number. Rule 3: no CLI. Rule 6: every re-ranked
recommendation carries a plain-language risk sentence that names its SOURCE — a
per-player expert range, a labelled positional fallback, or the K/DST
abstention — AND, when the build was degraded in any way (frozen-anchor rescale,
thin market coverage, a stale scrape, refused joins), a plain-language disclosure
sentence carried in ``PickRec.reasons`` itself. The banners used to live only in
:func:`format_risk_bands`, which an integrator wiring this behind a flag would
never call (audit finding 7); a degraded build now says so in the text the
cockpit renders.

======================================================================
MEASURED RESULT — RE-RUN 2026-08-31 AFTER THE AUDIT FIXES, on seeds held
out from every number the first build reported (live board 3,264 rows,
as_of 2026-08-30, season 2026, ``draft.evaluate`` paired harness,
``grader.grade_roster`` expected wins): THIS VARIANT DOES NOT HELP AT THE
OPERATOR'S SEAT. DO NOT SHIP IT.
======================================================================

Recorded here rather than only in a report, because a module that measured
itself and lost is more useful to the next reader than one that says nothing.
Every interval below is a paired 95% t interval agreeing with its bootstrap.

PAIRING VALIDATION (the number to check first if any of the rest looks wrong):
MODE_LEGACY against ``PickEngine`` returns mean delta **exactly 0.0000, sd
exactly 0.0000, 250 of 250 pairs an exact tie** at the production rollouts=512,
and 300 of 300 at rollouts=128. The wrapper is arithmetically transparent, and
the fixes did not change that.

THE DECISION THAT MATTERS — THE OPERATOR'S REAL 9-of-10 SEAT — IS A NULL, and
it is a null THREE TIMES over, at two rollout settings, on three seeds held out
from every number the first build reported:

    seed 20260831, rollouts=512 (PRODUCTION), n=250 pairs
        centered @ sd-match     +0.003   95% CI -0.007 .. +0.014   holes 0.50 vs 0.50
        per_player @ sd-match   +0.001   95% CI -0.010 .. +0.012   holes 0.54 vs 0.50
    seed 777001, rollouts=512 (PRODUCTION), n=250 pairs
        centered @ sd-match     +0.005   95% CI -0.006 .. +0.015   holes 0.52 vs 0.54
        per_player @ sd-match   -0.008   95% CI -0.018 .. +0.002   holes 0.58 vs 0.54
        centered @ 8x sd        -0.008   95% CI -0.027 .. +0.011   holes 0.53 vs 0.54
    seed 51515, rollouts=128, n=300 pairs
        centered @ sd-match    +0.0079   95% CI -0.0018 .. +0.0175  holes 0.52 vs 0.54
        per_player @ sd-match  -0.0012   95% CI -0.0113 .. +0.0089  holes 0.57 vs 0.54

The first build's ``centered @ 8x`` — the one arm that ever cleared zero pooled —
is re-confirmed DEAD at this seat on a held-out seed: -0.008 (-0.027 .. +0.011),
with holes going the wrong way. Nothing about the fixes rescued it.

WHAT IS NEW AND MUST NOT BE OVERSOLD: POOLED OVER THREE SEATS THE CENTERED ARM
NOW CLEARS ZERO. Seats 1/5/9, rollouts=128, n=100 each (300 pairs, held-out
seed 4242): ``centered @ sd-match`` **+0.0206, 95% CI +0.0079 .. +0.0334**
(bootstrap +0.0078 .. +0.0338), 245/300 exact ties, unfillable starter weeks
0.93 against the engine's 0.97, and positive at every seat individually
(+0.0093 / +0.0333 / +0.0193). ``per_player @ sd-match`` +0.0087
(-0.0051 .. +0.0224) and ``per_player @ naive`` +0.0071 (-0.0069 .. +0.0210) do
not clear zero. The centered arm's t is 3.17 over 3 challenger arms, so it
survives Bonferroni on its own run.

FOUR REASONS THAT IS STILL A ``do_not_ship``, in order of weight.
  1. IT IS NOT THE SEAT WE DRAFT FROM. Powered at seat 9 alone the same arm is
     +0.0079 (-0.0018 .. +0.0175) at rollouts=128 and +0.003 (-0.007 .. +0.014)
     at the production 512. This is exactly the failure mode the first build
     documented for its own ``centered @ 8x`` arm — a pooled margin hiding its
     seat structure — and it does not authorise a seat-9 decision.
  2. THE SIZE. The same harness measures the ENGINE beating FollowEspnRank by
     +1.283 wins. The pooled margin is 1.6% of that; the seat-9 point estimate
     at production settings is 0.2% of it, i.e. about one fortieth of one win.
  3. IT IS NOT WHAT THE FIXES BOUGHT — measured, not assumed. Running the
     PRE-FIX whole-board centring as its own arm on the identical grid (the
     reconstruction is exact: force ``min_position_rows`` above what any position
     supplies) gives **+0.0241 (+0.0106 .. +0.0377)** against the fixed arm's
     +0.0206. The two are statistically indistinguishable, so the margin was
     already there and the first build's pooled read of +0.0036 at a different
     seed was seed variation, not a different variant. What the centring fix
     bought is CORRECTNESS: the residual positional tilt over the drafted cohort
     falls from QB -0.070 / RB +0.043 / WR -0.049 / TE +0.171 to
     QB +0.002 / RB +0.039 / WR +0.064 / TE +0.050 — a 1.20-point TE-vs-QB
     spread at ``b_risk=5`` collapsing to 0.31, and all four now sharing a sign,
     i.e. a near-uniform level offset that a ranking largely cancels. The mode's
     reading rule ("if centered wins, the per-player signal is real") is now
     licensed; before the fix it was not, whatever the number said.
  4. 24 HOURS BEFORE A DRAFT is not when a 0.02-win pooled effect gets wired in
     behind a flag. It is worth a post-draft look, on a properly pre-registered
     seat-9 design; it is not worth a change to the engine that has passed two
     rehearsals and both acceptance tests.

Roster SHAPE is untouched (the QB 3.00 / TE 3.00 concentration pathology is not
what this term controls); unfillable starter weeks move 0.97 -> 0.93 pooled and
are flat to +0.03 at seat 9.

AND THE PICKS BARELY MOVE — but "barely" is not "not at all", and the first
build's bolded claim that they "do not move AT ALL" was an overgeneralisation
from 48 decisions that its own 74-78% exact-tie rates already contradicted
(audit finding 9; roughly one draft in four contains at least one changed pick).
Re-measured through the production room over the operator's 16 picks at
rollouts=512 for three room seeds — 48 real decisions, each variant asked for
the same decision on the IDENTICAL rng state (snapshot and restore, so a changed
pick is a real disagreement and not a different draw): ``per_player @ sd-match``
0/48, ``per_player @ naive`` 0/48, ``centered @ sd-match`` 0/48,
``centered @ 8x`` **2/48**, ``legacy`` 0/48. A null at the objective and a
near-null at the pick is still NOT a no-op — the paired runs above show 55 of
300 pairs differing at rollouts=128 — so an integrator must not read this module
as free to wire in behind a flag.

THE MECHANISM, WHICH IS THE FINDING WORTH CARRYING PAST THIS MODULE: the risk
term is an order of magnitude too small to reach the decision. Measured pick by
pick at seat 9, the engine's top-1-to-top-2 score gap runs 2.2 to 56.1 points
(median ~12.7), while the risk term's whole spread across the top-5 candidates
runs 0.0 to 8.1 (median ~1.9). At the sd-matched scale the risk term does not
reach the decision gap at **0 of 16** picks; even un-rescaled it reaches it at
1 of 16. So AT ``b_risk = 5.0`` THE SHIPPED RISK TERM IS VERY NEARLY INERT ON
THIS BOARD, whatever dispersion vector feeds it — which also means the item-2.3
sweep that chose ``b_risk = 5`` was choosing among options that barely differed.
Anyone who wants floor/ceiling to matter must change ``b_risk``, not the input.

AMPLIFYING IT DOES NOT RESCUE IT, and the way it fails is the reason to be
careful with a swept result. Multiplying the scale 4x / 8x / 16x (effective risk
weights 11.8 / 23.5 / 47.0 against the swept 5.0) produced ONE arm whose interval
cleared zero, ``centered @ 8x`` at +0.0253 (+0.0052..+0.0454). It was found by
sweeping ~12 configurations against one baseline grid, and it does not survive
Bonferroni even over the 4 arms of its own run (t=2.48, p=0.014; threshold
|t|>2.51). It DID replicate held out (fresh seed, five seats: +0.0472
(+0.0252..+0.0691), with a coherent dose-response (4x +0.026, 8x +0.047, 16x
**-0.068**, an interior optimum). And it still fails the only test that decides
anything, because the pooled margin was hiding its own seat structure: at the
operator's REAL seat, held out, the same arm reads +0.0367/+0.1065/+0.0370/
+0.0652 at seats 1/3/5/7 and **-0.0096 at seat 9**. Powered properly at seat 9
alone (n=300) it is +0.0036 (-0.0153..+0.0226), and at the PRODUCTION rollout
setting (512, n=150) it is **-0.0265 (-0.0546..+0.0016) with holes going the
wrong way, 0.60 against 0.55**. A margin pooled over seats does not authorise a
seat-9 decision, and this is what that looks like when you check.

LATENCY IS NOT THE OBSTACLE — re-measured 2026-08-31 on the final code, through
the production room at rollouts=512 on the real 3,264-row board, worst single
``recommend()`` over the operator's 48 decisions, best-of-3 per call site:
baseline **165.8 ms**, per_player @ sd 169.8, per_player @ naive 166.3,
centered @ sd 166.3, centered @ 8x 166.1, legacy 166.2 — a worst-case **+4.0 ms
(+2.4%)** against the 243 ms ship gate, i.e. 30% headroom kept. The re-rank is
free because the engine's candidate set is 8-10 players on this board (measured
8 in rounds 1-4, 10 from round 9) and ``recommend(top=64)`` costs what
``recommend(top=5)`` costs.

WHAT WOULD CHANGE THIS ANSWER, for whoever picks it up after the draft: raise
``b_risk`` so the term can reach the decision AND re-sweep it against this
objective (item 2.3 swept ``b_risk`` against a season-sum metric that was blind
to byes); or widen ``PickEngine.candidate_width``, since a re-ranker cannot
promote a player the gather never reached. Both are engine changes, not variant
changes, and neither belongs in the 24 hours before a draft.
"""

from __future__ import annotations

import datetime as _dt
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from types import MappingProxyType

from ziggurat.core import dispersion as dz
from ziggurat.draft import engine as eng
from ziggurat.draft.bots import BoardEntry, PickContext, position_counts
from ziggurat.draft.engine import PickEngine, PickRec, SurvivalEstimate, risk_sign

__all__ = [
    "DRAFTED_RANK_DEPTH",
    "MIN_DRAFTED_MARKET_COVERAGE",
    "MIN_DRAFTED_MARKET_ROWS",
    "MIN_POSITION_MEDIAN_ROWS",
    "MIN_SCALE_COHORT",
    "MODES",
    "MODE_CENTERED",
    "MODE_LEGACY",
    "MODE_PER_PLAYER",
    "NAIVE_SCALE_MULTIPLE",
    "RERANK_WIDTH",
    "SD_MATCH_SCALE_ANCHOR",
    "DispersionRiskPicker",
    "RiskBands",
    "VariantInputError",
    "build_risk_bands",
    "describe_centering",
    "describe_quantities",
    "format_risk_bands",
    "risk_bands_from_dispersion",
]


class VariantInputError(ValueError):
    """The variant was asked for something it cannot do honestly.

    Refuse-rather-than-guess: a risk term silently priced off a mismatched K/DST
    policy, an unknown mode, or a band map with no market measurement behind it
    reads exactly like a real one.
    """


# ------------------------------------------------------------------ modes

#: Reproduce ``PickEngine`` exactly. The CONTROL, not a feature.
MODE_LEGACY = "legacy"
#: Replace the positional constant with the rescaled per-player market band.
MODE_PER_PLAYER = "per_player"
#: Keep the swept positional posture; add the within-position deviation only.
MODE_CENTERED = "centered"

MODES: tuple[str, ...] = (MODE_LEGACY, MODE_PER_PLAYER, MODE_CENTERED)

#: Positions the legacy proxy deliberately ABSTAINS on (both carry 0.0 there).
KDST_POSITIONS: frozenset[str] = frozenset({"K", "DST"})

#: The one ``dispersion.relative_source`` that is an actual per-player
#: MEASUREMENT. Everything else that source can say ("positional_prior") is the
#: frozen six-number ``POSITIONAL_BAND_PRIOR`` constant, which this module must
#: never mistake for a band (see the module docstring, audit finding 2).
MARKET_SOURCE = "market"


# ------------------------------------------------------------------ scaling

#: The cohort the rescale AND the centring constant are computed on: the ESPN
#: top-N, i.e. the range a 16-round 10-team draft can actually reach (10 teams x
#: 16 rounds). Scaling on the whole 3,264-row board would be scaling on 3,100
#: players no seat will draft — and centring on it puts a positional tilt into
#: the mode whose whole purpose is to have none (audit findings 1 and 4).
DRAFTED_RANK_DEPTH = 160

#: Below this many market-priced rows in that cohort the sd-match is too noisy
#: to trust and :data:`SD_MATCH_SCALE_ANCHOR` is used instead, with a banner.
MIN_SCALE_COHORT = 40

#: Below this many market-priced rows at a position INSIDE the drafted range, its
#: median is a noise estimate (the live board has exactly one drafted kicker and
#: no drafted defense), so the centring constant falls back to that position's
#: median over the whole joined board, labelled.
MIN_POSITION_MEDIAN_ROWS = 5

#: The picker's construction guard, keyed on MARKET COVERAGE OF THE DRAFTED RANGE
#: rather than on the band map being non-empty. An empty or thin ECR scrape leaves
#: ``build_dispersion`` returning a full board of frozen positional constants; a
#: measuring mode priced off that is a six-number vector swap wearing a per-player
#: name, which is the one thing this module refuses to be.
MIN_DRAFTED_MARKET_ROWS = 20
MIN_DRAFTED_MARKET_COVERAGE = 0.25

#: MEASURED ANCHOR, and the documented fallback. ``sd(legacy) / sd(measured)``
#: over the ESPN top-160 of the live 2026-08-30 board under the default K/DST
#: abstention (legacy spread 0.283 / measured spread 0.482, n=130 market-priced).
#: Frozen so a thin/empty market board degrades to a stated number
#: rather than to an accidental one; ``tests/test_variant_dispersion.py`` pins it
#: against a re-derivation from a synthetic cohort so the ARITHMETIC is tested
#: even where the live board is not present.
SD_MATCH_SCALE_ANCHOR = 0.588

#: The naive substitution, expressed as a multiple of the sd-match scale:
#: ``scale = 1.0`` means "feed ``relative_dispersion`` straight into
#: ``b_risk * risk_sign * dispersion``", which is 1 / 0.588 = 1.70x the sd-matched
#: tilt and the 2.6x amplification the item was warned about (2.6x is against the
#: proxy's [0, 1] RANGE; 1.72x is against its spread over the drafted cohort,
#: which is what a ranking actually reads).
NAIVE_SCALE_MULTIPLE = 1.0 / SD_MATCH_SCALE_ANCHOR

#: How many of the engine's own candidates to re-rank. The gather produces 8-10
#: on the live board (``candidate_width`` by rank across all allowed positions,
#: plus best-by-VOR at each), so 64 is "all of them" with room to spare and
#: ``recommend(top=64)`` measured the same cost as ``recommend(top=5)``.
RERANK_WIDTH = 64


# ------------------------------------------------------------------ the bands


@dataclass(frozen=True)
class RiskBands:
    """Per-player relative market dispersion, keyed in ``BoardEntry.player_id``
    space, plus the rescale and everything needed to explain a row (Rule 6).

    ``values`` holds the RAW, UNSCALED ``relative_dispersion`` for every board
    entry that could be joined to a MARKET-PRICED dispersion row. A row that
    carries ``dispersion``'s own frozen positional prior is deliberately NOT in
    here: it is not a per-player measurement, and treating it as one is how an
    empty ECR scrape turns this variant into a positional-vector swap at double
    amplitude with nothing raising (audit finding 2). ``scale`` is applied by the
    picker, not baked in, so one build can be swept over several scales.

    ``kdst_abstain`` LIVES HERE, not on the picker, because the SCALE was computed
    under it: sd-matching over a cohort that prices K/DST is a different number
    than one that abstains on them. Putting the policy on the picker would allow a
    picker to read a scale that means something else, with no error anywhere — so
    the policy and the scale it produced are one object, and switching policies
    means building the bands again (cheap: :func:`risk_bands_from_dispersion` is
    pure and takes the same :class:`~ziggurat.core.dispersion.DispersionBoard`).
    """

    values: Mapping[str, float]
    sources: Mapping[str, str]
    positional_median: Mapping[str, float]
    positional_median_source: Mapping[str, str]
    positional_median_n: Mapping[str, int]

    scale: float
    scale_source: str            # cohort_sd_match | frozen_anchor
    cohort_n: int
    legacy_sd: float
    measured_sd: float
    kdst_abstain: bool

    season: int
    as_of: str
    ecr_type: str
    rank_units: str
    scrape_date: str | None

    matched: int                 # market-priced joins, whole board
    total: int
    drafted_matched: int         # market-priced joins inside the drafted range
    drafted_total: int
    drafted_joined: int          # ANY join inside the drafted range (>= drafted_matched)
    prior_only_joined: int       # rows dropped board-wide for carrying the frozen prior
    prior_only_drafted: int      # ...of which are inside the drafted range

    #: The floors a MEASURING mode must clear, carried on the bands for the same
    #: reason ``kdst_abstain`` is: they describe THIS build, and a picker that
    #: read them from somewhere else could pass a guard that was never about the
    #: data it is pricing. Defaults are :data:`MIN_DRAFTED_MARKET_ROWS` /
    #: :data:`MIN_DRAFTED_MARKET_COVERAGE`; a caller lowering them is doing so
    #: explicitly and on the record (tiny synthetic boards in the tests do).
    min_market_rows: int = MIN_DRAFTED_MARKET_ROWS
    min_market_coverage: float = MIN_DRAFTED_MARKET_COVERAGE
    banners: tuple[str, ...] = ()
    #: Short plain-language sentences naming every way this build is degraded.
    #: These reach ``PickRec.reasons`` — the text a cockpit renders verbatim —
    #: because a banner an integrator never prints protects nobody (finding 7).
    disclosures: tuple[str, ...] = ()

    #: What ``relative_dispersion`` IS, quoted into reason text (Rule 6).
    label: str = (
        "hypothesis: per_player_market_band — the expert consensus panel's "
        "best-to-worst range for this player, converted through the house "
        "season-points curve and divided by the median draftable player's range"
    )

    def relative(self, player_id: str, position: str) -> tuple[float, str]:
        """``(raw relative dispersion, source)`` for one board entry.

        A player the market board could not reach falls back to his POSITION'S
        median over the DRAFTED cohort of this same board — a labelled fallback
        on the same scale, never a silent zero and never a number from a
        different question. In :data:`MODE_CENTERED` that fallback is exactly the
        centring constant, so an unjoined player reverts to shipped behaviour by
        construction; that is only true because the two are read from this one
        mapping.
        """
        v = self.values.get(player_id)
        if v is not None:
            return v, self.sources.get(player_id, MARKET_SOURCE)
        med = self.positional_median.get(position)
        if med is not None:
            return med, "positional_median_fallback"
        return dz.POSITIONAL_BAND_PRIOR.get(position, 1.0), "frozen_positional_prior"

    @property
    def drafted_coverage(self) -> float:
        """Share of the drafted range carrying a real MARKET band.

        This is the honest headline number and it is deliberately the one named
        ``drafted_coverage``: the first build reported joins here, so a build with
        zero market rows behind it printed "100.0%" (audit finding 2).
        """
        return self.drafted_matched / self.drafted_total if self.drafted_total else 0.0

    @property
    def drafted_join_coverage(self) -> float:
        """Share of the drafted range joined to ANY dispersion row, market-priced
        or frozen-prior. Diagnostic only — never the coverage claim."""
        return self.drafted_joined / self.drafted_total if self.drafted_total else 0.0


def _effective(rk: float, frac: float) -> float:
    """The engine's own treatment of the risk term: the lineup-reachability
    discount applies to POSITIVE components only ("a discount must never make a
    player score better"). Mirrors ``engine.recommend``'s
    ``(rk * frac if rk > 0 else rk)`` exactly; the legacy-mode identity test is
    what proves the DIFFERENCE cancels, and
    ``test_the_engines_risk_term_has_the_form_this_variant_subtracts`` is what
    proves the form itself is still the engine's (finding 6 — the identity alone
    cannot, because ``- X + X`` cancels whatever X is)."""
    return rk * frac if rk > 0 else rk


def _entry_ids(row: dz.PlayerDispersion) -> tuple[str, ...]:
    """Every ``BoardEntry.player_id`` a dispersion row could legitimately be.

    ``simulator.load_board`` keys an entry ``espn_id or gsis_id``, falling back to
    ``DST:<team>`` for a defense and ``<POS>:<overall rank>`` otherwise. The last
    of those is a rank in the VOR board's own ordering and is NOT reconstructible
    from a dispersion row, so it is deliberately not attempted: an unreachable
    player takes the labelled positional fallback instead of a guessed join.
    """
    ids: list[str] = []
    if row.espn_id is not None:
        ids.append(str(row.espn_id))
    if row.gsis_id is not None:
        ids.append(str(row.gsis_id))
    if row.position == "DST" and row.team:
        ids.append(f"DST:{row.team}")
    return tuple(dict.fromkeys(ids))


def _days_between(earlier: str | None, later: str | None) -> int | None:
    """Whole days between two ISO dates that both came from DATA, never a clock.

    Returns ``None`` when either is missing or unparseable — a disclosure that
    cannot be computed is simply not made, rather than guessed at.
    """
    if not earlier or not later:
        return None
    try:
        a = _dt.date.fromisoformat(str(earlier)[:10])
        b = _dt.date.fromisoformat(str(later)[:10])
    except ValueError:
        return None
    return (b - a).days


def risk_bands_from_dispersion(
    dboard: dz.DispersionBoard,
    board: Sequence[BoardEntry],
    *,
    kdst_abstain: bool = True,
    drafted_depth: int = DRAFTED_RANK_DEPTH,
    min_cohort: int = MIN_SCALE_COHORT,
    min_position_rows: int = MIN_POSITION_MEDIAN_ROWS,
    min_market_rows: int = MIN_DRAFTED_MARKET_ROWS,
    min_market_coverage: float = MIN_DRAFTED_MARKET_COVERAGE,
) -> RiskBands:
    """Join a :class:`~ziggurat.core.dispersion.DispersionBoard` onto a draft
    board and compute the sd-matching rescale. PURE — no DB, no clock, no RNG.

    THE JOIN REFUSES RATHER THAN GUESSES, four ways. A dispersion row that is not
    MARKET-priced is not a band at all and is dropped (it is the frozen
    ``POSITIONAL_BAND_PRIOR`` constant; see the module docstring). A row whose
    position disagrees with the board entry's is dropped (a market row filed
    under a different position is a different player's number). An id that two
    dispersion rows both claim is dropped from BOTH (the ambiguity is the finding,
    not a coin flip). And the ``<POS>:<rank>`` id fallback is never reconstructed
    (see :func:`_entry_ids`). Every drop lands on the labelled positional
    fallback, and the coverage counters say how many.
    """
    if not board:
        raise VariantInputError(
            "cannot build risk bands against an empty board — there is nothing to "
            "join a market band onto."
        )

    # id -> the single dispersion row that claims it (ambiguous ids dropped).
    claimed: dict[str, list[dz.PlayerDispersion]] = {}
    for row in dboard.rows.values():
        for pid in _entry_ids(row):
            claimed.setdefault(pid, []).append(row)
    unique = {pid: rows[0] for pid, rows in claimed.items() if len(rows) == 1}
    ambiguous = sum(1 for rows in claimed.values() if len(rows) > 1)

    values: dict[str, float] = {}
    sources: dict[str, str] = {}
    joined_ids: set[str] = set()          # ANY join, market-priced or not
    prior_only_ids: set[str] = set()      # joined, but to a frozen-prior row
    position_conflicts = 0
    prior_only = 0
    for entry in board:  # board order: deterministic, no dict-order dependence
        row = unique.get(entry.player_id)
        if row is None:
            continue
        if row.position != entry.position:
            position_conflicts += 1
            continue
        joined_ids.add(entry.player_id)
        if row.relative_source != MARKET_SOURCE:
            # dispersion's own frozen positional prior wearing a row's clothes.
            prior_only += 1
            prior_only_ids.add(entry.player_id)
            continue
        values[entry.player_id] = float(row.relative_dispersion)
        sources[entry.player_id] = row.relative_source

    # --- the cohorts. EVERYTHING that centres or scales is computed on the
    # DRAFTED range, because that is the only range a draft ranks players in and
    # because centring and scaling on two different cohorts puts a positional
    # tilt into MODE_CENTERED (audit findings 1 and 4).
    drafted = [e for e in board if e.espn_overall_rank <= drafted_depth]
    cohort = [e for e in drafted if e.player_id in values]

    drafted_by_pos: dict[str, list[float]] = {}
    for e in cohort:
        drafted_by_pos.setdefault(e.position, []).append(values[e.player_id])
    board_by_pos: dict[str, list[float]] = {}
    for e in board:
        v = values.get(e.player_id)
        if v is not None:
            board_by_pos.setdefault(e.position, []).append(v)

    positional_median: dict[str, float] = {}
    positional_median_source: dict[str, str] = {}
    positional_median_n: dict[str, int] = {}
    for pos in sorted(set(drafted_by_pos) | set(board_by_pos)):
        drafted_vals = drafted_by_pos.get(pos, [])
        if len(drafted_vals) >= min_position_rows:
            positional_median[pos] = statistics.median(drafted_vals)
            positional_median_source[pos] = "drafted_cohort"
            positional_median_n[pos] = len(drafted_vals)
            continue
        board_vals = board_by_pos.get(pos, [])
        if board_vals:
            positional_median[pos] = statistics.median(board_vals)
            positional_median_source[pos] = "whole_board"
            positional_median_n[pos] = len(board_vals)

    # --- the rescale, on the drafted range only, under the stated K/DST policy.
    def _legacy(pos: str) -> float:
        return eng.POSITIONAL_DISPERSION_PRIOR.get(pos, 0.0)

    def _measured(entry: BoardEntry) -> float:
        if kdst_abstain and entry.position in KDST_POSITIONS:
            return _legacy(entry.position)  # 0.0 for both — same value on both sides
        return values[entry.player_id]

    legacy_sd = measured_sd = 0.0
    if len(cohort) >= 2:
        legacy_sd = statistics.pstdev([_legacy(e.position) for e in cohort])
        measured_sd = statistics.pstdev([_measured(e) for e in cohort])

    banners: list[str] = list(dboard.banners)
    if len(cohort) >= min_cohort and measured_sd > 0.0:
        scale = legacy_sd / measured_sd
        scale_source = "cohort_sd_match"
    else:
        scale = SD_MATCH_SCALE_ANCHOR
        scale_source = "frozen_anchor"
        why = (
            f"only {len(cohort)} MARKET-priced player(s) inside the ESPN top "
            f"{drafted_depth}, floor {min_cohort}"
            if len(cohort) < min_cohort
            else f"the {len(cohort)} MARKET-priced players inside the ESPN top "
                 f"{drafted_depth} have NO spread at all ({measured_sd:.4f}), so there "
                 "is nothing to match"
        )
        banners.append(
            f"risk-band rescale fell back to the FROZEN anchor {SD_MATCH_SCALE_ANCHOR} "
            f"({why}; measured spread {measured_sd:.4f}) — the tilt this variant "
            "applies is therefore scaled to a board that is not this one"
        )

    if position_conflicts or ambiguous:
        banners.append(
            f"risk-band join refused {position_conflicts} player(s) whose market row "
            f"is filed at a different position and {ambiguous} ambiguous id(s) claimed "
            "by more than one market row — each takes the labelled positional fallback"
        )
    if prior_only:
        banners.append(
            f"{prior_only} joined row(s) board-wide carry dispersion's own frozen "
            "positional prior rather than a per-player expert range and are NOT "
            "treated as bands — they take the labelled positional fallback"
        )

    bands = RiskBands(
        values=MappingProxyType(dict(values)),
        sources=MappingProxyType(dict(sources)),
        positional_median=MappingProxyType(positional_median),
        positional_median_source=MappingProxyType(positional_median_source),
        positional_median_n=MappingProxyType(positional_median_n),
        scale=scale,
        scale_source=scale_source,
        cohort_n=len(cohort),
        legacy_sd=legacy_sd,
        measured_sd=measured_sd,
        kdst_abstain=bool(kdst_abstain),
        season=dboard.season,
        as_of=dboard.as_of,
        ecr_type=dboard.ecr_type,
        rank_units=dboard.rank_units,
        scrape_date=dboard.scrape_date,
        matched=len(values),
        total=len(board),
        drafted_matched=len(cohort),
        drafted_total=len(drafted),
        drafted_joined=sum(1 for e in drafted if e.player_id in joined_ids),
        prior_only_joined=prior_only,
        prior_only_drafted=sum(1 for e in drafted if e.player_id in prior_only_ids),
        min_market_rows=int(min_market_rows),
        min_market_coverage=float(min_market_coverage),
        banners=tuple(banners),
    )
    return dc_replace(bands, disclosures=_disclosures(bands))


def _disclosures(bands: RiskBands) -> tuple[str, ...]:
    """The plain-language sentences a DEGRADED build must say in the reasons.

    Rule 6: the operator is a novice and cannot smell a stale or unmeasured
    input. A fully-measured, fully-covered, same-day build produces the empty
    tuple — silence here is a positive statement that nothing was degraded.
    """
    out: list[str] = []
    if bands.scale_source != "cohort_sd_match":
        out.append(
            f"HEADS UP: the SIZE of this floor-vs-ceiling tilt was not measured on "
            f"today's board — only {bands.drafted_matched} draftable "
            f"{'player has' if bands.drafted_matched == 1 else 'players have'} an "
            f"expert range, so it falls back to a frozen {bands.scale:.3f} measured on "
            "a different day's board."
        )
    if bands.drafted_total and bands.drafted_coverage < 1.0:
        missing = bands.drafted_total - bands.drafted_matched
        prior = bands.prior_only_drafted
        tail = (
            f" ({prior} of them matched only a positional default row)" if prior else ""
        )
        out.append(
            f"HEADS UP: {missing} of the {bands.drafted_total} draftable players carry "
            "no per-player expert range; each of those is scored on the typical range "
            "for his position instead, which is a labelled default and not a "
            f"measurement of that player{tail}."
        )
    lag = _days_between(bands.scrape_date, bands.as_of)
    if lag:
        days = "1 day" if lag == 1 else f"{lag} days"
        out.append(
            f"HEADS UP: the expert rankings behind this are a scrape from "
            f"{bands.scrape_date}, {days} before {bands.as_of} — news since then is "
            "not in the range."
        )
    return tuple(out)


def build_risk_bands(
    conn,
    board: Sequence[BoardEntry],
    *,
    as_of,
    season: int,
    weeks=None,
    ecr_type: str = dz.DEFAULT_ECR_TYPE,
    source: str = "sleeper_rotowire",
    view: str = "historical",
    kdst_abstain: bool = True,
    drafted_depth: int = DRAFTED_RANK_DEPTH,
    min_cohort: int = MIN_SCALE_COHORT,
    min_position_rows: int = MIN_POSITION_MEDIAN_ROWS,
    min_market_rows: int = MIN_DRAFTED_MARKET_ROWS,
    min_market_coverage: float = MIN_DRAFTED_MARKET_COVERAGE,
    today=None,
) -> RiskBands:
    """The ONE DB seam (Rule 1).

    ``as_of`` is keyword-only with no default and is threaded, with ``view``,
    straight into :func:`ziggurat.core.dispersion.build_dispersion`, which is
    where the gate is enforced; this layer never widens it. Build the ``board``
    at the SAME ``as_of``/``season``/``source`` or the two are describing
    different days.

    COST: ``build_dispersion`` is a ~3.6 s read on the live database. Call this
    ONCE at construction and reuse the result — it is deliberately not something
    ``recommend()`` can reach.
    """
    dboard = dz.build_dispersion(
        conn,
        as_of=as_of,
        season=season,
        weeks=weeks,
        ecr_type=ecr_type,
        source=source,
        view=view,
        today=today,
    )
    return risk_bands_from_dispersion(
        dboard,
        board,
        kdst_abstain=kdst_abstain,
        drafted_depth=drafted_depth,
        min_cohort=min_cohort,
        min_position_rows=min_position_rows,
        min_market_rows=min_market_rows,
        min_market_coverage=min_market_coverage,
    )


# ------------------------------------------------------------------ reasons


#: Where "unusually wide" and "unusually tight" start, on the relative scale
#: whose median draftable player is 1.0. Reason-text phrasing ONLY — no number
#: in the score reads these, so moving them cannot change a pick.
WIDE_BAND = 1.35
TIGHT_BAND = 0.70

#: The same idea for MODE_CENTERED, whose SCORE reads the deviation from the
#: player's own position, not the absolute band. Quoting the absolute band there
#: told four QBs "the experts agree unusually closely on him" while the score was
#: penalising them for being wider than a typical QB (audit finding 4).
WITHIN_POSITION_WIDE = 0.15
WITHIN_POSITION_TIGHT = -0.15


def _range_clause(raw: float) -> str:
    if raw >= WIDE_BAND:
        return "the experts are unusually SPLIT on him"
    if raw <= TIGHT_BAND:
        return "the experts agree unusually closely on him"
    return "the experts are about as split on him as on a typical player"


def _within_position_clause(delta: float, position: str) -> str:
    if delta >= WITHIN_POSITION_WIDE:
        return f"the experts are more split on him than on a typical {position}"
    if delta <= WITHIN_POSITION_TIGHT:
        return f"the experts agree more closely on him than on a typical {position}"
    return f"the experts are about as split on him as on a typical {position}"


def _direction_tail(sign: float) -> str:
    if sign < 0:
        return (
            "early in the draft you are protecting your floor, so a wider range "
            "counts against a player here"
        )
    if sign > 0:
        return (
            "this late a wide range is what you WANT — a bench flier is worth more "
            "for its ceiling than its floor"
        )
    return (
        "at this point in the draft floor and ceiling are weighted equally, so "
        "his range does not move this pick either way"
    )


def _variant_risk_note(
    position: str,
    round_num: int,
    *,
    raw: float,
    source: str,
    bands: RiskBands,
    mode: str,
) -> str:
    """One plain-language sentence a football novice can act on (Rule 6).

    It always says THREE things: how wide this player's expert range is IN THE
    TERMS THE SCORE ACTUALLY USES (absolute in MODE_PER_PLAYER, relative to his
    own position in MODE_CENTERED), whether a wide range counts for or against him
    at this point in the draft, and WHERE the number came from — a per-player
    measurement, a labelled positional fallback, or the K/DST abstention. A novice
    cannot otherwise tell those apart.
    """
    where = (
        f"expert consensus panel, '{bands.ecr_type}' board scraped {bands.scrape_date}"
    )
    if source == "kdst_abstention":
        return (
            f"floor-vs-ceiling is deliberately NOT scored for {position}: their "
            "week-to-week swing dwarfs any draft-day range, and the expert panel's "
            "K/D-ST rankings have no measured relationship to when this room "
            "actually takes them — so this pick is decided on value and timing alone"
        )

    sign = risk_sign(round_num)
    measured = source == MARKET_SOURCE

    if mode == MODE_CENTERED:
        median = bands.positional_median.get(position)
        if not measured or median is None:
            # The score is the engine's own, exactly — say so instead of implying
            # a tilt that is not being applied.
            return (
                "no per-player expert range could be matched to him, so his "
                f"floor-vs-ceiling is left exactly where the shipped {position} model "
                f"puts it — this pick is unchanged by the expert range ({where})"
            )
        delta = raw - median
        return (
            f"{_within_position_clause(delta, position)} — his best-to-worst range is "
            f"about {raw:.1f}x a typical draftable player's, against {median:.1f}x for "
            f"a typical draftable {position} ({where}); {_direction_tail(sign)}"
        )

    if measured:
        opener = _range_clause(raw)
        gap = (
            f"the gap between their best and worst call on his season is about "
            f"{raw:.1f}x a typical draftable player's ({where})"
        )
    else:
        opener = (
            f"no per-player expert range could be matched to him, so this falls back "
            f"to the typical {position} range on this board"
        )
        gap = (
            f"about {raw:.1f}x a typical draftable player's — a labelled fallback, "
            f"NOT a measurement of him ({where})"
        )
    return f"{opener} — {gap}; {_direction_tail(sign)}"


# ------------------------------------------------------------------ the picker


@dataclass(frozen=True, init=False)
class DispersionRiskPicker:
    """A wrapping :class:`~ziggurat.draft.bots.Picker`: run the real engine, then
    re-price ONLY its risk term off the per-player market band and re-rank.

    It calls ``engine.recommend(ctx, top=rerank_width)`` exactly once per pick, so
    it consumes ``ctx.rng`` identically to ``PickEngine.pick`` (the engine draws
    one ``getrandbits(64)`` for the rollout child regardless of ``top``). That is
    what makes :data:`MODE_LEGACY` bit-for-bit identical to the engine and what
    keeps a paired A/B honest — a variant that burned a different amount of
    randomness would read as 0.66 expected wins of pure noise under shared
    streams (``evaluate``'s measured finding).

    THE RE-SCORE IS A DIFFERENCE, NOT A RECOMPUTE. The engine's score is
    ``frac*vor + need + frac*urgency + eff(b_risk * risk_sign * dispersion)``;
    ``need`` and ``urgency`` need the survival estimate, which is not on the
    ``PickRec``. So this subtracts the engine's own risk term and adds its own,
    applying the identical lineup-reachability treatment. Everything else on the
    row is the engine's, unchanged.

    IT IS A DROP-IN FOR ``PickEngine`` AT THE ATTRIBUTE SURFACE OTHER MODULES
    CLONE (audit finding 5). ``posture.project_postures`` does
    ``dataclasses.replace(session.engine, need_schedule=..., survival=...)``
    OUTSIDE its seam guard, and both cockpits swallow the resulting exception
    whole (``except Exception: return None``), so a Phase-3 integrator who added
    the obvious picker-injection seam at ``DraftSession._engine()`` would have
    shipped a cockpit whose posture tip never fires again, silently. So this
    accepts any ``PickEngine`` field as a constructor keyword and forwards it to
    the wrapped engine — which makes ``dataclasses.replace(picker, ...)`` work for
    engine fields as well as its own — and delegates unknown attribute reads
    (``need_schedule``, ``b_risk``, ``kdst_earliest_round``, ...) to the engine.
    """

    bands: RiskBands
    engine: PickEngine = PickEngine()
    mode: str = MODE_PER_PLAYER
    scale: float | None = None          # None -> bands.scale
    rerank_width: int = RERANK_WIDTH

    def __init__(
        self,
        *,
        bands: RiskBands,
        engine: PickEngine | None = None,
        mode: str = MODE_PER_PLAYER,
        scale: float | None = None,
        rerank_width: int = RERANK_WIDTH,
        **engine_overrides,
    ) -> None:
        wrapped = PickEngine() if engine is None else engine
        if engine_overrides:
            try:
                wrapped = dc_replace(wrapped, **engine_overrides)
            except TypeError as exc:
                raise VariantInputError(
                    f"{sorted(engine_overrides)} is not a field of the wrapped engine "
                    f"({type(wrapped).__name__}). Engine fields are forwarded so that "
                    "dataclasses.replace(picker, need_schedule=...) — what "
                    "posture.project_postures does to session.engine — keeps working; "
                    "anything else is a typo, and a typo that silently did nothing "
                    "would be worse than this."
                ) from exc
        object.__setattr__(self, "bands", bands)
        object.__setattr__(self, "engine", wrapped)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "scale", scale)
        object.__setattr__(self, "rerank_width", rerank_width)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise VariantInputError(
                f"unknown dispersion mode {self.mode!r}; expected one of {list(MODES)}. "
                f"({MODE_LEGACY!r} is the control that reproduces PickEngine exactly.)"
            )
        if self.rerank_width < 1:
            raise VariantInputError(
                f"rerank_width must be at least 1; got {self.rerank_width}"
            )
        if self.mode == MODE_LEGACY:
            return  # the CONTROL needs no market data: it never reads a band.
        b = self.bands
        if not b.values:
            raise VariantInputError(
                "the risk-band map is EMPTY: every candidate would take the labelled "
                "positional fallback and this variant would be a relabelled copy of "
                "the shipped proxy wearing a per-player name. Check the as_of, season "
                f"and board you built the bands with (as_of={b.as_of!r}, "
                f"season={b.season})."
            )
        if (
            b.drafted_matched < b.min_market_rows
            or b.drafted_coverage < b.min_market_coverage
        ):
            raise VariantInputError(
                "not enough MARKET-priced expert ranges behind this board to run a "
                f"measuring mode: {b.drafted_matched} of {b.drafted_total} draftable "
                f"players ({b.drafted_coverage:.1%}), against floors "
                f"{b.min_market_rows} rows and "
                f"{b.min_market_coverage:.0%}. With a thin or missing ECR scrape "
                "dispersion.build_dispersion still returns a full board, but every row "
                "is the frozen POSITIONAL_BAND_PRIOR constant — pricing off that is the "
                "six-number positional-vector swap this module refuses to offer as a "
                f"mode. ({b.prior_only_joined} joined row(s) were prior-only.)"
            )

    # -- attribute surface -------------------------------------------------

    def __getattr__(self, name: str):
        """Delegate unknown attribute reads to the wrapped engine.

        Only for names this class does not define and that do not start with an
        underscore, so nothing private leaks and a genuine typo on this class
        still raises. See the class docstring: ``posture`` reads
        ``engine.need_schedule`` off whatever sits at ``session.engine``.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            wrapped = object.__getattribute__(self, "engine")
        except AttributeError:  # pragma: no cover - only during construction
            raise AttributeError(name) from None
        try:
            return getattr(wrapped, name)
        except AttributeError:
            raise AttributeError(
                f"neither {type(self).__name__} nor the engine it wraps has {name!r}"
            ) from None

    # -- Picker seam -------------------------------------------------------

    @property
    def effective_scale(self) -> float:
        """The scale the picker ACTUALLY applies: the cohort sd-match on the
        bands unless an explicit ``scale=`` overrides it (which is what the
        rescale sweep does — every swept arm is this property, so it is pinned by
        a test that does not read it, or the whole sweep could have been one
        configuration measured six times)."""
        return self.bands.scale if self.scale is None else float(self.scale)

    def pick(self, ctx: PickContext) -> str:
        return self.recommend(ctx, top=1)[0].player_id

    def recommend(self, ctx: PickContext, *, top: int = 5) -> tuple[PickRec, ...]:
        recs = self.engine.recommend(ctx, top=max(1, self.rerank_width))
        if not recs:  # pragma: no cover - the engine raises on an exhausted board
            return ()

        counts = position_counts(ctx.own_roster)
        sign = risk_sign(ctx.round)
        b_risk = self.engine.b_risk

        rescored: list[tuple[float, PickRec, float, str]] = []
        for rec in recs:
            pos = rec.position
            frac = eng._value_fraction(pos, counts, ctx.roster)
            old_rk = b_risk * sign * eng.POSITIONAL_DISPERSION_PRIOR.get(pos, 0.0)
            new_disp, raw, source = self._dispersion_for(rec.player)
            new_rk = b_risk * sign * new_disp
            score = rec.pick_score - _effective(old_rk, frac) + _effective(new_rk, frac)
            rescored.append((score, rec, raw, source))

        # The ENGINE's total tie-break order (D2), re-applied to the new scores:
        # higher score, then higher vor, then lower ESPN rank, then player_id.
        # No wall clock, no dict order.
        rescored.sort(key=_rerank_key)

        est = SurvivalEstimate(
            survival={r.player_id: r.survival_next for _s, r, _w, _u in rescored},
            next_best_vor={},
        )
        disclosures = () if self.mode == MODE_LEGACY else self.bands.disclosures
        out: list[PickRec] = []
        for i, (score, rec, raw, source) in enumerate(rescored[: max(1, top)]):
            # MODE_LEGACY is the CONTROL: it must be indistinguishable from the
            # engine, reason text included, or the identity test proves less than
            # it claims. Every other source rewrites the sentence.
            note = (
                rec.risk_note
                if source == "legacy_positional_proxy"
                else _variant_risk_note(
                    rec.position, ctx.round, raw=raw, source=source, bands=self.bands,
                    mode=self.mode,
                )
            )
            out.append(
                dc_replace(
                    rec,
                    pick_score=score,
                    risk_note=note,
                    reasons=_replace_risk_reason(rec, note, disclosures),
                    alternatives=tuple(
                        (alt.name or alt.player_id, PickEngine._why_not(alt.player, est))
                        for _s, alt, _w, _u in rescored[i + 1 : i + 4]
                    ),
                )
            )
        return tuple(out)

    # -- internals ---------------------------------------------------------

    def _dispersion_for(self, entry: BoardEntry) -> tuple[float, float, str]:
        """``(dispersion used, raw relative band, source)`` for one candidate."""
        pos = entry.position
        legacy = eng.POSITIONAL_DISPERSION_PRIOR.get(pos, 0.0)
        if self.mode == MODE_LEGACY:
            return legacy, legacy, "legacy_positional_proxy"
        if self.bands.kdst_abstain and pos in KDST_POSITIONS:
            # The legacy value here is 0.0 for both positions, i.e. the ABSTENTION
            # is preserved exactly rather than re-derived.
            return legacy, legacy, "kdst_abstention"
        raw, source = self.bands.relative(entry.player_id, pos)
        s = self.effective_scale
        if self.mode == MODE_PER_PLAYER:
            return s * raw, raw, source
        # MODE_CENTERED: the swept positional posture plus the within-position
        # deviation only. An unjoined player centres to exactly 0 -> shipped value.
        median = self.bands.positional_median.get(pos, raw)
        return legacy + s * (raw - median), raw, source


def _rerank_key(item: tuple[float, PickRec, float, str]):
    """The engine's D2 total order, re-applied to the variant's scores.

    Extracted so the order is one object that can be tested directly: the picker's
    incoming list is ALREADY in this order on the engine's own scores, so a
    score-only sort reproduces it on any board where re-scoring does not create a
    new cross-position tie — which is why the original test could not fail (audit
    finding 11) and why the new one constructs exactly that tie.
    """
    score, rec, _raw, _source = item
    return (-score, -rec.vor, rec.player.espn_overall_rank, rec.player_id)


def _replace_risk_reason(
    rec: PickRec, note: str, disclosures: Sequence[str] = ()
) -> tuple[str, ...]:
    """Swap the engine's risk sentence for the variant's, keeping every other
    reason verbatim and the ordering intact, and insert any build-level
    disclosures immediately BEFORE it.

    ``engine._build_rec`` appends ``risk_note`` LAST and that is the only place it
    appears; ``tests/test_variant_dispersion.py`` pins that so a reordering there
    rots this loudly instead of leaving a stale sentence in a live recommendation.
    The risk sentence STAYS last (the cockpit and several tests read
    ``reasons[-1]``), so the disclosures sit at -2 and inward.
    """
    reasons = tuple(rec.reasons)
    head = reasons[:-1] if (reasons and reasons[-1] == rec.risk_note) else tuple(
        r for r in reasons if r != rec.risk_note
    )
    return head + tuple(disclosures) + (note,)


# ------------------------------------------------------------------ reporting


def format_risk_bands(bands: RiskBands) -> str:
    """A legible summary of what a build actually produced (Rule 6)."""
    lines = [
        f"risk bands  season {bands.season}  as_of {bands.as_of}  "
        f"market '{bands.ecr_type}' ({bands.rank_units} ranks) scrape {bands.scrape_date}",
        f"  MARKET-priced {bands.matched}/{bands.total} board entries; "
        f"{bands.drafted_matched}/{bands.drafted_total} "
        f"({bands.drafted_coverage:.1%}) inside the ESPN top {DRAFTED_RANK_DEPTH}"
        f"  [joined-to-anything there: {bands.drafted_joined}"
        f" ({bands.drafted_join_coverage:.1%}); "
        f"{bands.prior_only_joined} prior-only row(s) dropped board-wide]",
        f"  rescale {bands.scale:.4f} ({bands.scale_source}, n={bands.cohort_n}): "
        f"legacy spread {bands.legacy_sd:.4f} / measured spread {bands.measured_sd:.4f}"
        f"  -> effective risk weight {eng.DEFAULT_B_RISK * bands.scale:.2f} at the "
        f"shipped b_risk={eng.DEFAULT_B_RISK:g}",
        f"  K/DST: {'ABSTAIN (legacy 0.0 kept)' if bands.kdst_abstain else 'PRICED from the market band'}",
        f"  {bands.label}",
    ]
    med = bands.positional_median
    if med:
        lines.append(
            "  positional median band (the MODE_CENTERED centring constant): "
            + "  ".join(
                f"{p} {med[p]:.2f}"
                f"[{bands.positional_median_source.get(p, '?')}"
                f" n={bands.positional_median_n.get(p, 0)}]"
                for p in sorted(med)
            )
        )
    for d in bands.disclosures:
        lines.append(f"  DISCLOSED IN EVERY PICK: {d}")
    for b in bands.banners:
        lines.append(f"  BANNER: {b}")
    return "\n".join(lines)


def describe_centering(
    bands: RiskBands, board: Sequence[BoardEntry], *, scale: float | None = None
) -> Mapping[str, float]:
    """How much positional tilt MODE_CENTERED still carries, per position.

    Exists because the mode's whole claim is "this changes nothing about which
    positions the engine prefers early", and the first build's version of that
    claim was false by up to 0.85 risk points (audit findings 1 and 4). Returns
    ``{"<POS>.mean_shift": mean(applied) - legacy, "<POS>.risk_points": ...,
    "<POS>.n": ...}`` over the DRAFTED cohort, all in the units the score reads.
    A reader who doubts the fix runs this and reads the residual back.

    Not zero by construction: centring on a MEDIAN does not zero a MEAN.
    """
    s = bands.scale if scale is None else float(scale)
    by_pos: dict[str, list[float]] = {}
    for e in board:
        if e.espn_overall_rank > DRAFTED_RANK_DEPTH:
            continue
        if bands.kdst_abstain and e.position in KDST_POSITIONS:
            continue
        raw, _src = bands.relative(e.player_id, e.position)
        median = bands.positional_median.get(e.position, raw)
        legacy = eng.POSITIONAL_DISPERSION_PRIOR.get(e.position, 0.0)
        by_pos.setdefault(e.position, []).append(legacy + s * (raw - median))
    out: dict[str, float] = {}
    for pos, applied in sorted(by_pos.items()):
        legacy = eng.POSITIONAL_DISPERSION_PRIOR.get(pos, 0.0)
        shift = statistics.fmean(applied) - legacy
        out[f"{pos}.n"] = float(len(applied))
        out[f"{pos}.mean_shift"] = shift
        out[f"{pos}.risk_points"] = abs(eng.DEFAULT_B_RISK * shift)
    return MappingProxyType(out)


def describe_quantities(
    dboard: dz.DispersionBoard,
    board: Sequence[BoardEntry],
    *,
    rank_depth: int = 250,
) -> Mapping[str, float]:
    """The module docstring's four measurements, re-derived from live inputs.

    Exists so the conceptual claim above is REPLAYABLE rather than a comment: a
    reader who doubts "these are different quantities" runs this against a live
    dispersion board and reads the same numbers back. Returns a plain mapping —
    no printing, no DB, no clock.

    THE COHORT IS THE POINT AND IT IS NARROW ON PURPOSE: board entries inside the
    ESPN top ``rank_depth`` that carry a per-player market band. Measured over the
    WHOLE 428-row market-priced board instead, the market-vs-noise correlation
    reads +0.05 rather than -0.40 — the sign flips once 240 undraftable
    deep-bench rows are let in. The draft only ever ranks players in the drafted
    range, so that is the cohort the claim is about, and the difference is stated
    here rather than left for someone to rediscover. NOTE the default
    ``rank_depth`` of 250 is WIDER than :data:`DRAFTED_RANK_DEPTH`: it is a
    characterisation cohort chosen to contain some D/ST and K (the top 160 holds
    one kicker and no defense), and it is NOT the cohort the rescale or the
    centring constant is computed on.
    """
    rank_by_id = {e.player_id: int(e.espn_overall_rank) for e in board}
    bands = risk_bands_from_dispersion(dboard, board)
    keep = []
    for row in dboard.rows.values():
        if row.relative_source != MARKET_SOURCE or row.market_sigma_points is None:
            continue
        pid = next((p for p in _entry_ids(row) if p in bands.values), None)
        if pid is None or rank_by_id.get(pid, 10 ** 9) > rank_depth:
            continue
        keep.append(row)
    if len(keep) < 3:
        raise VariantInputError(
            f"only {len(keep)} market-banded row(s) inside the ESPN top {rank_depth} "
            "are reachable from this board — there is nothing to characterise."
        )
    keep.sort(key=lambda r: (r.position, r.board_rank, str(r.key)))  # deterministic
    mkt = [float(r.market_sigma_points) for r in keep]  # type: ignore[arg-type]
    noise = [float(r.weekly_noise_sigma_points) for r in keep]
    rel = [float(r.relative_dispersion) for r in keep]

    by_pos: dict[str, list[float]] = {}
    for r in keep:
        by_pos.setdefault(r.position, []).append(float(r.relative_dispersion))
    grand = statistics.fmean(rel)
    ssb = sum(len(v) * (statistics.fmean(v) - grand) ** 2 for v in by_pos.values())
    ssw = sum(sum((x - statistics.fmean(v)) ** 2 for x in v) for v in by_pos.values())

    return MappingProxyType({
        "n": float(len(keep)),
        "rank_depth": float(rank_depth),
        "pearson_market_vs_weekly_noise": _pearson(mkt, noise),
        "median_market_sigma_points": statistics.median(mkt),
        "median_weekly_noise_sigma_points": statistics.median(noise),
        "within_position_variance_share": ssw / (ssb + ssw) if (ssb + ssw) else 0.0,
        # The shipped rescale, computed on the DRAFTED range (top
        # DRAFTED_RANK_DEPTH), not on this wider characterisation cohort.
        "scale_sd_match": bands.scale,
    })


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    mx, my = statistics.fmean(x), statistics.fmean(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    den = (
        sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)
    ) ** 0.5
    return num / den if den else 0.0
