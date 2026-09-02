"""Per-player floor / ceiling + measured weekly variance — the dispersion spine.

WHAT THIS ANSWERS, and why it is TWO quantities and not one. Every board in this
system publishes a point estimate: ``valuation`` a season total, ``marginal`` a
roster-context delta, ``streaming`` a week's house points. A head-to-head league
is not won by the point estimate — it is won 14 separate times, by beating one
opponent in one week — so every consumer that reasons about WINNING needs a
spread as well as a level. There are two independent spreads and conflating them
is the failure this module exists to prevent:

(a) DRAFT-TIME OUTCOME UNCERTAINTY — "how far apart are informed people about
    what this player's SEASON is worth". Source: the FantasyPros expert-consensus
    panel in ``adp_rankings`` (``sd`` / ``best`` / ``worst``). It is the spread of
    a MEAN, and it is measured in RANK units, not points.
(b) WEEK-TO-WEEK SCORING VARIANCE — "given his true rate, how much does one week
    swing". Source: nflverse 2021-2025 weekly lines RE-SCORED through
    ``core/scoring.py``. It is the spread AROUND a mean, in points per week.

For a season total both compose, under a stated independence assumption:

    Var(season) = (market sigma on the season mean)^2 + weeks * (weekly sigma)^2

and the module reports the two components separately as well as the sum, because
their magnitudes are wildly different and a consumer that grabbed the wrong one
would be quietly wrong. Measured on the live 2026-08-30 board: the consensus RB1's
market band is 3.3 season points (the panel is nearly unanimous) for a market
sigma of 0.8, while his weekly-noise component over his 16 forecast weeks is 44.5
— so the market band alone is NOT a season outcome range, and saying otherwise
for a consensus player understates the spread by more than an order of magnitude.

THIS IS THE UPGRADE ``ziggurat/draft/engine.py`` DOCUMENTED AS BLOCKED. That
module ships ``POSITIONAL_DISPERSION_PRIOR``, a six-number relative-variance
proxy, with the note "when ``adp_rankings`` is later populated this upgrades to
per-player ``(worst - best)/4`` behind a populated-table check". ``adp_rankings``
is now populated (168,356 rows for season 2026, six weekly scrapes 2026-07-24 ..
2026-08-28, ``sd``/``best``/``worst`` non-null on every row). This module is the
permanent home of that upgrade. It NEVER imports the quarantined draft package
(Rule 8) and it changes no existing behaviour: it is a new, additive surface.

WHICH ``ecr_type`` IS THIS LEAGUE'S BOARD — decided by evidence, not by name.
The frame carries nine ranking flavours. ``fp_page`` on the upstream
DynastyProcess frame names the exact FantasyPros page each one was scraped from,
which settles it outright (pulled live 2026-08-30, see ``ECR_PAGES``):

    ro  -> /nfl/rankings/ppr-cheatsheets.php              FULL-PPR redraft, OVERALL
    rp  -> /nfl/rankings/ppr-{rb,wr,te}-cheatsheets.php   FULL-PPR redraft, POSITIONAL
           + qb/k/dst-cheatsheets.php                     (no PPR variant exists for these)
    rsf -> /nfl/rankings/ppr-superflex-cheatsheets.php    superflex (2-QB) — WRONG LEAGUE
    bo/bp -> /nfl/rankings/best-ball-*.php                best-ball roster construction
    do/dp/dsf/drk -> /nfl/rankings/dynasty-*.php, rookies.php   wrong horizon

``ro`` and ``rp`` are therefore the SAME market — the full-PPR redraft cheat
sheets, one aggregated overall and one split by position — and either is "the
correct redraft-PPR series". The board-shape corroborates the pages: ``ro``'s
top seven is five WR to two RB with Ja'Marr Chase over Jahmyr Gibbs (a full-PPR
signature; a standard-scoring board leads with backs), ``rsf``'s top five is five
quarterbacks, ``do``'s top twelve promotes rookies over proven veterans, and
``drk`` is rookies only.

``rp`` IS THE DEFAULT HERE, for two reasons that are about units and coverage,
not about which market is right:

  1. UNITS. ``rp``'s ``sd``/``best``/``worst`` are POSITIONAL ranks, and a
     positional rank has a well-defined rank-to-points curve (the k-th best RB on
     our own board scores a specific number). ``ro``'s are OVERALL ranks, which
     interleave six positions with incomparable point scales — measured on the
     2026-08-28 board, the median D/ST ``(worst-best)/4`` under ``ro`` is 42
     OVERALL ranks, or 132% of the entire 32-deep D/ST field. There is no honest
     conversion of that into points, so when a caller asks for ``ro`` this module
     reports the rank dispersion as what it is and REFUSES the points band.
  2. COVERAGE. Against the 1,029-player ESPN draftable universe (re-measured
     2026-08-30 through the id ladder ``draftable_coverage`` uses), ``rp`` matches
     70.5% and ``ro`` 48.2%. Over the ESPN top 400 — the range a 16-round draft
     can actually reach — ``rp`` matches 98.2% and ``ro`` 96.2%. NEITHER is 100%,
     which an earlier draft of this docstring claimed: all 7 ``rp`` misses are
     KICKERS whose ``adp_rankings`` rows carry a NULL ``gsis_id`` and a NULL
     ``espn_id``, and the last-resort name join in ``MarketBoard.resolve``
     recovers some but not all of them. K is the position CLAUDE.md names as the
     house scoring edge, so this gap is named rather than rounded off.

CONVERTING A RANK SPREAD INTO A POINTS SPREAD — done explicitly, labelled, and
refusable. ``PointsCurve`` is the house board's own within-position season-points
curve: ``curve[k]`` = the season house points of the k-th best player at that
position, priced through ``scoring.py`` per week and summed, over the
coverage-passing lines only (``WeeklyLine.played_weeks``, never the point sum —
item 3.2's bye-shaped-row trap). The band is taken as a DIFFERENCE and hung on
the player's OWN projection, never replacing it::

    raw_floor = proj + (curve(worst_rank) - curve(ecr_rank))
    ceiling   = proj + (curve(best_rank)  - curve(ecr_rank))
    floor     = max(0, raw_floor)          # published: a season total is not negative
    band      = ceiling - raw_floor        # the SPREAD: never truncated

so the level stays the house board's and only the SPREAD comes from the market.
``ecr_rank`` is literal and load-bearing: it is the PANEL'S consensus rank, the
only scale ``best`` and ``worst`` share. The locally-derived ``pos_rank`` is a
different scale (see ``RankDispersion``) and using it as the midpoint — which
this module did at first — shifted published floors and ceilings by up to 34
house points and produced a market floor ABOVE a player's own projection.

The published floor is clamped at 0 because a season total cannot be negative,
but the BAND that feeds ``market_sigma_points`` is taken from the UNCLAMPED
floor: the band is a spread, and truncating a spread at a level boundary reports
false confidence about exactly the players the two boards disagree about most
(the clamp fires on 51 of 428 live priced rows). A clamped row says so in words.
If any of the three ranks falls outside the curve's domain (the market board is
deeper than our priceable board at every position: RB 193 vs 125, WR 257 vs 194,
QB 105 vs 32) the conversion is REFUSED and said out loud, never extrapolated.

THE ONE MODELLING ASSUMPTION IN THAT CONVERSION, stated because it is the thing
most likely to be wrong: reading ``curve(market_rank)`` assumes the market's
within-position rank scale and this board's are the SAME scale — that the panel's
RB30 sits roughly where our own RB30 sits. They are not the same ordering, and
the delta form only cancels the mismatch to first order. Re-measured on the live
2026-08-28 board (n=428 priced players): the two orderings agree on 90.5% of
RB/WR/TE pairs, and the median gap between a player's own projection and the
curve value at his market ECR rank is 9.1 house points against a median
projection of 89.8 (10.1%), with a 90th percentile of 29.2. So the assumption is
good enough to price a SPREAD off and would not be good enough to price a LEVEL
off — which is exactly why the level is never taken from it.

THE SCALE OF ``relative_dispersion``, because a consumer will substitute it for
``POSITIONAL_DISPERSION_PRIOR`` and the two are NOT interchangeable numbers. The
legacy vector lives in [0, 1] by construction; this one is a per-player band
divided by the cohort's own median band, so the median draftable player is 1.0
and the tails run wider — measured on the live 2026-08-28 board (n=428): min
0.06, p10 0.36, median 1.00, p90 1.91, max 2.55. Dropped into
``b_risk * risk_sign(round) * dispersion(pos)`` at the shipped ``b_risk = 5.0``
that is a +/-13-point tilt where the proxy gave +/-5, so a consumer swapping them
must re-tune ``b_risk`` (about 0.4x) or the risk term starts outvoting VOR. That
re-tune is the consumer's call and is deliberately NOT made here.

THE POPULATED-TABLE CHECK, and what happens when it fails. Three floors, checked
in this order: the board must carry at least ``MIN_BOARD_ROWS``; the player's
position must carry at least ``MIN_POSITION_ROWS`` on that board; the curve must
be at least ``MIN_CURVE_DEPTH`` deep. Any failure, or a player simply absent from
the market board, falls back to ``POSITIONAL_BAND_PRIOR`` — and the fallback is
NAMED IN THE REASON TEXT on that row (Rule 6). A silent fallback is the failure
mode this module is built to avoid: a per-player number and a positional average
must never be indistinguishable in the output.

The fallback prior is MEASURED, not inherited. Reusing the draft engine's
``POSITIONAL_DISPERSION_PRIOR`` as the fallback would have been incoherent: on the
same relative scale as the per-player number, that vector puts RB at 0.40 while
the measured median RB band is 1.23 — a fallback three times smaller than the
measured value for the same position, i.e. a systematic bias between covered and
uncovered players with no error anywhere. ``POSITIONAL_BAND_PRIOR`` is the
measured median instead. The legacy vector is kept as
``LEGACY_POSITIONAL_DISPERSION_PRIOR`` for reference only, never used to price.

PART (b) DOES NOT SUPERSEDE ``lineup_support.DEFAULT_VARIANCE`` — IT VALIDATES IT.
That model is an OLS fit of realised weekly house-point sigma on the mean, per
player-season / team-season, >=8 REG games, 2021-2025. This module re-measured
the same cohort NON-PARAMETRICALLY, by (position, tier) band, and the affine form
is within 7.27% of the measured tier sigma in 15 of 15 cells (largest miss QB
11-20, +7.27%; DST, whose affine R^2 is 0.064, is within 3.97% everywhere). So
``DEFAULT_VARIANCE`` is IMPORTED and used unchanged as the primary reading;
``DEFAULT_TIER_SIGMA`` ships alongside it as an independent measured cross-check
quoted in every reason string, and as the reading for a player whose projected
mean falls outside the range the affine model was fitted on. The old constant
keeps working because nothing here touches it. Kicker sigma stays what 3.5 called
it — a flat hypothesis — but the REASON has changed: it is NOT YET FITTED, no
longer unmeasurable. ``weekly_stats`` gained FG make/distance/miss columns in
migration ``013`` (item 4.1), so ``score_kicker`` can now be exercised on
historical K rows; a measured K sigma through the same OLS is a recorded
follow-up, not part of this module yet.

Standing rules. Rule 1 — every accessor takes a keyword-only ``as_of`` with no
default, defaults to the ``historical`` view, threads ``view``, and ships a
leakage test; the 2021-2025 bulk cohort behind ``DEFAULT_TIER_SIGMA`` was read
through ``base.latest_truth`` and is frozen here as a constant, not re-read.
Rule 2 — no scoring constant lives here; every point comes from
``valuation.weekly_lines``, which prices through ``scoring.score``. A sigma or a
band is a DISPERSION prior, not a scoring number, and must never move into
``scoring.py``. Rule 3 — no CLI. Rule 6 — every row ships reasons, every prior
quotes its label, cohort and date, and a refused conversion says so. Rule 8 —
permanent module; never imports ``ziggurat/draft/``.
"""

import math
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from ziggurat.core import scoring
from ziggurat.core.lineup_support import DEFAULT_VARIANCE, VarianceModel
from ziggurat.core.marginal import COVERAGE_FLOOR, bye_map
from ziggurat.core.valuation import (
    DEFAULT_WEEKS,
    WeeklyLine,
    canon_position,
    weekly_lines,
)
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import adp_rankings, base, refresh

# --------------------------------------------------------------- market series

#: ``ecr_type`` -> the FantasyPros page DynastyProcess scraped it from, read off
#: the live upstream frame's ``fp_page`` column on 2026-08-30. This is the
#: evidence behind ``DEFAULT_ECR_TYPE``; it is data, not decoration, and
#: ``tests/test_dispersion.py`` pins it so a future re-labelling upstream is
#: caught rather than silently re-pointing the board at a dynasty market.
ECR_PAGES: Mapping[str, str] = MappingProxyType({
    "ro": "/nfl/rankings/ppr-cheatsheets.php",
    "rp": "/nfl/rankings/ppr-{rb,wr,te}-cheatsheets.php + qb/k/dst-cheatsheets.php",
    "rsf": "/nfl/rankings/ppr-superflex-cheatsheets.php",
    "bo": "/nfl/rankings/best-ball-overall.php",
    "bp": "/nfl/rankings/best-ball-{qb,rb,wr,te,dst}.php",
    "do": "/nfl/rankings/dynasty-overall.php",
    "dp": "/nfl/rankings/dynasty-{qb,rb,wr,te,k,dst}.php",
    "dsf": "/nfl/rankings/dynasty-superflex.php",
    "drk": "/nfl/rankings/rookies.php",
})

#: The two flavours of THIS league's market: redraft, full PPR, 1-QB. Same pages,
#: different aggregation. Anything else is a different league than the one we play.
REDRAFT_PPR_ECR_TYPES = frozenset({"ro", "rp"})

#: What ``sd`` / ``best`` / ``worst`` are counted in, per series. Only
#: ``positional`` has a rank-to-points curve (see the module docstring).
RANK_UNITS: Mapping[str, str] = MappingProxyType({
    "ro": "overall", "rp": "positional", "rsf": "overall",
    "bo": "overall", "bp": "positional",
    "do": "overall", "dp": "positional", "dsf": "overall", "drk": "overall",
})

#: The positional full-PPR redraft series. Default because its dispersion is in
#: positional-rank units (convertible to points) and it covers 75.6% of the
#: draftable universe against ``ro``'s 53.0% (measured 2026-08-30).
DEFAULT_ECR_TYPE = "rp"


# --------------------------------------------------------- populated-table floors

#: Fewer rows than this on the resolved scrape and the whole board is treated as
#: unpopulated: every row falls back to the positional prior and the board carries
#: a banner saying so. The live 2026-08-28 ``rp`` board is 807 rows, so this fires
#: only on an empty/collapsed table — which is exactly the state item 2.3 shipped
#: its proxy for, and the state a fresh clone or a failed scrape is in.
MIN_BOARD_ROWS = 50

#: A position needs this many market rows before a per-player rank from it is
#: trusted. The thinnest real position is D/ST at 32.
MIN_POSITION_ROWS = 8

#: A points curve shallower than this cannot convert a rank into points with a
#: straight face (the interpolation would be dominated by two or three players).
MIN_CURVE_DEPTH = 12

#: Below this many priced bands the cohort median is too noisy to normalise
#: against, so ``REFERENCE_BAND_PRIOR`` is used and the board says which it used.
MIN_REFERENCE_ROWS = 30


@dataclass(frozen=True)
class Floors:
    """The four populated-table floors, bundled so a caller (or a test) can move
    them together without four keyword arguments spreading through the module.

    They are PARAMETERS and not bare constants for one reason: a floor that no
    test can cross in both directions is a floor nobody has checked. The shipped
    defaults are the module constants; ``tests/test_dispersion.py`` exercises the
    board on both sides of every one of them.
    """

    board_rows: int = MIN_BOARD_ROWS
    position_rows: int = MIN_POSITION_ROWS
    curve_depth: int = MIN_CURVE_DEPTH
    reference_rows: int = MIN_REFERENCE_ROWS


DEFAULT_FLOORS = Floors()


#: RANGE-TO-SIGMA. A best-to-worst expert range is read as roughly a +/-2 sigma
#: interval, so ``sigma = range / 4`` — the same rule of thumb item 2.3 named when
#: it wrote ``(worst - best)/4``, and a RULE OF THUMB, not a measurement of this
#: panel. It is a named constant rather than a bare ``/ 4.0`` because every
#: published ``market_sigma_points``, ``season_sigma_points``, ``floor_points`` and
#: ``ceiling_points`` scales with it: a silent change here is a silent change to
#: the headline uncertainty of every priceable player, and
#: ``tests/test_dispersion.py`` pins it from the curve arithmetic end to end.
RANGE_TO_SIGMA_DIVISOR = 4.0

#: Days between a pull and the decision before the staleness banner shouts. The
#: same threshold the marginal / waiver / streaming boards use, RE-STATED rather
#: than imported: it is a threshold on THIS module's disclosure, and importing it
#: would let a tuning change over there silently retune what this board discloses.
STALE_BANNER_DAYS = 7


# ------------------------------------------------------------- measured priors

#: Median points band (ceiling - floor), house season points, over the priced
#: 2026 board — the denominator that puts ``relative_dispersion`` on a scale where
#: the median draftable player is 1.0. Recomputed from the cohort being priced
#: whenever that cohort is big enough (``MIN_REFERENCE_ROWS``); this frozen value
#: is the fallback and the documented anchor.
#:
#: PROVENANCE IS PART OF THE VALUE HERE, so it is stated exactly and it is
#: REPLAYABLE: every number in this block and in ``POSITIONAL_BAND_PRIOR`` below
#: is what ``build_dispersion(conn, as_of="2026-08-30", season=2026)`` — the
#: shipped call, no extra filtering — produces on the live database against the
#: 2026-08-28 ``rp`` scrape. The first version of these constants was measured
#: through a pipeline that differed from the shipped one (it dropped
#: floor-clamped rows), so the label quoted an ``n`` the code could not
#: reproduce; ``tests/test_dispersion.py`` now pins them to a re-derivation
#: instead of to a comment.
REFERENCE_BAND_PRIOR = 53.44

#: Per-position median relative band — the LABELLED FALLBACK for a player the
#: market board does not carry, on the same scale as the per-player number so a
#: covered and an uncovered player at the same position are not systematically
#: different quantities. MEASURED, not assumed: it is the median of the
#: per-player bands actually priced at that position, divided by
#: ``REFERENCE_BAND_PRIOR``.
#: The cohort behind ``POSITIONAL_BAND_PRIOR``: position -> (rows priced, median
#: band in house season points), from the same 2026-08-30 run. It is a CONSTANT
#: rather than a comment because a comment cannot be tested and this one was
#: wrong: the shipped label quoted "n=375 priced players" and per-position n's
#: that the shipped ``build_dispersion`` produced 423 / different splits for,
#: because they had been measured through a pipeline that dropped floor-clamped
#: rows. ``tests/test_dispersion.py`` now checks the prior against these numbers
#: and these numbers against the label, so provenance rots loudly.
POSITIONAL_BAND_PRIOR_COHORT: Mapping[str, tuple[int, float]] = MappingProxyType({
    "QB": (29, 33.70),
    "RB": (96, 65.61),
    "WR": (155, 62.68),
    "TE": (92, 42.34),
    "K": (24, 13.78),
    "DST": (32, 38.55),
})

#: Each value is exactly ``round(POSITIONAL_BAND_PRIOR_COHORT[pos][1] /
#: REFERENCE_BAND_PRIOR, 3)`` — asserted, not asserted-by-comment.
POSITIONAL_BAND_PRIOR: Mapping[str, float] = MappingProxyType({
    "QB": 0.631,    # n=29,  median band 33.70 house pts
    "RB": 1.228,    # n=96,  median band 65.61
    "WR": 1.173,    # n=155, median band 62.68
    "TE": 0.792,    # n=92,  median band 42.34
    "K": 0.258,     # n=24,  median band 13.78
    "DST": 0.721,   # n=32,  median band 38.55
})

POSITIONAL_BAND_PRIOR_LABEL = (
    "hypothesis: positional_market_band_prior — median per-player market band by "
    "position, measured 2026-08-30 on the FantasyPros full-PPR redraft positional "
    "board (scrape 2026-08-28, n=428 priced players) converted through the house "
    "season-points curve; relative to a 53.4-point median band. It TRANSFERS "
    "across scrapes but is not constant: re-measured on the 2026-07-24 / 08-14 / "
    "08-21 boards these ratios read 0.84-1.18 of the value measured there, "
    "widest at QB on the July board"
)

#: The item-2.3 proxy this module replaces, kept for REFERENCE ONLY — never used
#: to price a row. The original lives in ``ziggurat/draft/engine.py`` as
#: ``POSITIONAL_DISPERSION_PRIOR`` and is deleted with that package (Rule 8
#: forbids importing it, so this is a copy with a lineage note, the same pattern
#: ``core/lineup.py`` uses). It is a RELATIVE-VARIANCE judgement about boom/bust
#: ("RB is volume-predictable, WR/TE are boom-bust"), which is a claim about RANK
#: dispersion; measured in rank units relative to field depth the ordering
#: broadly survives (WR 5.7% of field depth > RB 5.1% > TE 4.3% > QB 3.8%), but
#: measured in POINTS it inverts on RB, because the RB points curve is the
#: steepest of the six and a rank band converts through that steepness. Both
#: numbers are real and they answer different questions.
LEGACY_POSITIONAL_DISPERSION_PRIOR: Mapping[str, float] = MappingProxyType(
    {"RB": 0.40, "QB": 0.60, "WR": 1.00, "TE": 1.00, "DST": 0.0, "K": 0.0}
)


# ------------------------------------------------------ measured weekly sigma

@dataclass(frozen=True)
class SigmaTier:
    """One measured (position, rank band) weekly-sigma cell.

    ``lo``/``hi`` are inclusive within-position ranks by season MEAN house points
    (``hi=None`` means open-ended). ``sigma`` is the MEAN of the per-unit weekly
    standard deviations in the cell, ``median_sigma`` its median; ``mean_mu`` is
    the cell's mean per-week points, and ``mu_lo``/``mu_hi`` the fitted domain.
    """

    lo: int
    hi: int | None
    n: int
    mean_mu: float
    sigma: float
    median_sigma: float
    mu_lo: float
    mu_hi: float

    def contains_rank(self, rank: int) -> bool:
        return rank >= self.lo and (self.hi is None or rank <= self.hi)

    @property
    def label(self) -> str:
        return f"{self.lo}-{self.hi}" if self.hi is not None else f"{self.lo}+"


@dataclass(frozen=True)
class SigmaEstimate:
    """A weekly-sigma reading plus WHERE it came from (Rule 6)."""

    sigma: float
    source: str            # tier_measured | affine_prior | kicker_flat_hypothesis
    reason: str


@dataclass(frozen=True)
class TierSigmaModel:
    """Non-parametric weekly house-point sigma by (position, tier) — a MEASURED
    LABELLED HYPOTHESIS, and an independent cross-check on
    ``lineup_support.DEFAULT_VARIANCE``.

    Same cohort, same method, same re-scoring as that model (nflverse 2021-2025
    REG only, per player-season / team-season with >=8 scored games, every week
    re-priced through ``ziggurat.core.scoring`` under ``HOUSE_RULES``,
    per-week-never-sum-then-score), but with the affine form dropped: units are
    ranked within (position, season) by season mean and bucketed into bands
    chosen from THIS league's shape (10 teams x 1 QB / 2 RB / 2 WR / 1 TE /
    1 FLEX / 1 K / 1 D-ST, so "1-10" is the starter tier at a one-starter
    position and "1-20" at a two-starter one).

    The measurement's headline is that it does NOT overturn the affine model: the
    affine prediction at each cell's mean is within 7.27% of the measured cell
    sigma in all 15 of 15 cells (largest miss QB 11-20, +7.27%; DST, whose affine
    R^2 is 0.064, is within 3.97% everywhere). So ``sigma()`` KEEPS the affine
    model as the primary reading and uses this table for the two jobs a bucket
    does better than a line: as the cross-check quoted in every reason string,
    and as the reading when a projected mean falls OUTSIDE the range the affine
    model was fitted on, where extrapolating a line is unsupported.

    Preferring the tier INSIDE the fitted domain was tried and is wrong: the
    affine form is mu-sensitive within a band (R^2 0.696/0.715/0.767 for
    RB/WR/TE) where a tier hands twenty ranks of players one number. Measured on
    the live 2026 board it would have given the projected RB1 — 21.1 pts/wk, the
    top of a tier whose mean is 16.6 — his tier's 8.3 rather than the affine's
    10.6, understating the best player on the board by 28%.

    Kickers are absent because the fit has NOT YET BEEN RUN, not because it
    cannot be: ``weekly_stats`` gained FG make/distance/miss columns in migration
    ``013`` (item 4.1), so ``score_kicker`` can now be exercised on historical K
    rows. Until that follow-up lands, K falls through to
    ``DEFAULT_VARIANCE.k_flat_sigma``, the flat hypothesis item 3.5 shipped.

    Rule 2: none of these numbers is a scoring value. A sigma parametrises a
    downstream win-probability / floor-ceiling model; it never re-prices anything.
    """

    tiers: Mapping[str, tuple[SigmaTier, ...]]
    cohort: str
    label: str
    source: str

    def tier_of(self, position: str, rank: int | None) -> SigmaTier | None:
        if rank is None:
            return None
        pos = canon_position(position) or position
        for tier in self.tiers.get(pos, ()):  # ordered, non-overlapping
            if tier.contains_rank(int(rank)):
                return tier
        return None

    def sigma(
        self,
        position: str,
        *,
        mu: float,
        rank: int | None = None,
        variance: VarianceModel = DEFAULT_VARIANCE,
    ) -> SigmaEstimate:
        """Weekly house-point sigma for one player, with its provenance.

        ``rank`` is the player's within-position rank on the board being priced.
        Supplying it buys the direct tier measurement; omitting it (or asking for
        a kicker, or a position this model never measured) falls through to the
        affine ``VarianceModel``, and the reason string says which happened.
        """
        pos = canon_position(position) or position
        if pos == "K":
            return SigmaEstimate(
                sigma=variance.k_flat_sigma,
                source="kicker_flat_hypothesis",
                reason=(
                    f"weekly swing sigma {variance.k_flat_sigma:.1f} pts — a flat kicker "
                    "hypothesis NOT YET FITTED: weekly_stats gained FG make/distance/miss "
                    "columns in migration 013 (item 4.1), and a measured K sigma is a "
                    f"recorded follow-up; until then this is a guess: {variance.label}"
                ),
            )
        affine = variance.sigma(pos, mu)
        tier = self.tier_of(pos, rank)
        lo, hi = self.fitted_domain(pos)

        # OUT OF THE FITTED DOMAIN -> the non-parametric reading. An affine line
        # extrapolated past the means it was fitted on is unsupported, and the
        # 2026 feed is a flat season rate that can sit above any historical
        # per-game mean. Inside the domain the affine form WINS, and that is the
        # measurement talking, not a preference: it is mu-sensitive within a tier
        # (R^2 0.70/0.72/0.77 for RB/WR/TE) where a tier hands every player in a
        # 20-rank band the same number, and the tier study confirms it to within
        # 7.5% at every cell mean. Using the tier inside the domain would have
        # handed the projected RB1 (21.1 pts/wk) his tier's 8.3 instead of the
        # affine 10.6 — a 28% understatement of the best player on the board.
        if lo is not None and not (lo <= mu <= hi):
            fallback = tier or self._edge_tier(pos, mu, lo, hi)
            if fallback is not None:
                return SigmaEstimate(
                    sigma=fallback.sigma,
                    source="tier_measured",
                    reason=(
                        f"weekly swing sigma {fallback.sigma:.1f} pts, measured directly for "
                        f"{pos}{fallback.label} (n={fallback.n} player-seasons, mean "
                        f"{fallback.mean_mu:.1f} pts/wk) — used INSTEAD of the affine model "
                        f"because this projection ({mu:.1f} pts/wk) is outside the "
                        f"{lo:.1f}-{hi:.1f} range that model was fitted on, where "
                        f"extrapolating it ({affine:.1f}) is unsupported. "
                        f"{self.label}; {self.cohort}"
                    ),
                )
        cross = ""
        if tier is not None:
            # TWO DIFFERENT MEANS, and the sentence must not blur them. The
            # CROSS-CHECK is the affine model evaluated at the TIER'S mean; the
            # player's own mu is somewhere else in the band, and the affine model
            # reads something else there BY DESIGN (that mu-sensitivity is the
            # whole reason the affine form is preferred inside the domain).
            # Printing the second number under the first number's label made 348
            # of 499 live rows quote a "disagreement" past this module's own
            # 7.5% bound in the same sentence that claims the models agree.
            drift_at_tier_mean = (
                (variance.sigma(pos, tier.mean_mu) - tier.sigma) / tier.sigma
                if tier.sigma else 0.0
            )
            drift_here = (affine - tier.sigma) / tier.sigma if tier.sigma else 0.0
            cross = (
                f" Cross-checked against a direct measurement of {pos}{tier.label} "
                f"(n={tier.n} player-seasons, mean sigma {tier.sigma:.1f} at "
                f"{tier.mean_mu:.1f} pts/wk): AT THAT MEAN the affine model reads "
                f"{drift_at_tier_mean:+.1%} against it, which is the agreement this "
                f"table exists to check. THIS player projects {mu:.1f} pts/wk, not "
                f"{tier.mean_mu:.1f}, and there the affine model reads {affine:.1f} "
                f"against the tier's flat {tier.sigma:.1f} ({drift_here:+.1%}) — the "
                "affine form moves with the projection inside a tier and the tier, "
                f"being one number for the whole band, does not. {self.label}"
            )
        return SigmaEstimate(
            sigma=affine,
            source="affine_prior",
            reason=f"{variance.describe(pos, mu, affine)}.{cross}",
        )

    def fitted_domain(self, position: str) -> tuple[float | None, float | None]:
        """The pooled per-week mean range the tiers were measured over — the range
        inside which the affine model is interpolating rather than extrapolating."""
        pos = canon_position(position) or position
        tiers = self.tiers.get(pos)
        if not tiers:
            return None, None
        return min(t.mu_lo for t in tiers), max(t.mu_hi for t in tiers)

    def _edge_tier(self, position: str, mu: float, lo: float, hi: float) -> SigmaTier | None:
        """The tier nearest the out-of-domain mu (top tier above the range, bottom
        tier below it) — used when no board rank was supplied to pick one."""
        pos = canon_position(position) or position
        tiers = self.tiers.get(pos)
        if not tiers:
            return None
        return tiers[0] if mu > hi else tiers[-1]


DEFAULT_TIER_SIGMA = TierSigmaModel(
    tiers=MappingProxyType({
        "QB": (
            SigmaTier(1, 10, 50, 20.4321, 8.1466, 8.0760, 17.7631, 25.3165),
            SigmaTier(11, 20, 50, 16.2743, 6.9209, 6.7727, 13.9288, 19.3965),
            SigmaTier(21, None, 81, 11.5670, 7.0329, 7.0546, 1.6550, 15.1507),
        ),
        "RB": (
            SigmaTier(1, 20, 100, 16.5853, 8.2635, 8.0717, 12.8000, 24.5059),
            SigmaTier(21, 40, 100, 10.8313, 6.8557, 6.7324, 8.0941, 13.6167),
            SigmaTier(41, None, 288, 4.0800, 3.9452, 4.0022, -0.0250, 9.0875),
        ),
        "WR": (
            SigmaTier(1, 20, 100, 17.0066, 8.8056, 8.6493, 12.9235, 25.8529),
            SigmaTier(21, 40, 100, 12.5262, 7.3424, 7.2626, 10.4176, 14.5833),
            SigmaTier(41, None, 595, 5.0852, 4.4043, 4.4383, -0.0133, 11.5588),
        ),
        "TE": (
            SigmaTier(1, 10, 50, 12.9071, 7.5572, 7.3181, 9.8800, 18.6059),
            SigmaTier(11, 20, 50, 9.3019, 6.2143, 6.4539, 7.4067, 11.0647),
            SigmaTier(21, None, 307, 3.9172, 3.4416, 3.2800, 0.0000, 9.2714),
        ),
        # RE-MEASURED 2026-08-30 on REG ONLY. The values that shipped here first
        # were the REG+POST fit (1-10 read 7.9920 / 6.5546 / 11.4737), which did
        # not match the cohort string printed on every D/ST row, did not match the
        # offense tiers beside it, and did not match ``DEFAULT_VARIANCE``'s own
        # REG-only D/ST fit that these numbers exist to cross-check. QB/RB/WR/TE
        # reproduce byte-exactly under REG-only; D/ST was the only cell that moved.
        "DST": (
            SigmaTier(1, 10, 50, 8.1195, 6.3961, 6.3802, 6.4706, 10.7059),
            SigmaTier(11, 20, 50, 5.8894, 6.4467, 6.4143, 4.5294, 7.4706),
            SigmaTier(21, None, 60, 3.8235, 5.7741, 5.7192, 0.2353, 5.7059),
        ),
    }),
    cohort=(
        "cohort: nflverse weekly_stats (offense) + team_defense (DST), 2021-2025 REG "
        "only, per-(player|team)-season with >=8 scored games, every week re-scored "
        "through ziggurat.core.scoring under HOUSE_RULES (per-week, never "
        "sum-then-score), read under base.latest_truth; units ranked within "
        "(position, season) by season mean. NOT fitted to 2026"
    ),
    label=(
        "hypothesis: weekly_house_point_sigma_by_tier, measured 2021-2025 REG, "
        "non-parametric companion to item 3.5's affine DEFAULT_VARIANCE (agrees "
        "within 7.3% in 15 of 15 cells)"
    ),
    source="item: dispersion module measure stage (2026-08-30)",
)

#: The largest |affine - tier| disagreement across the 15 measured cells, as a
#: fraction (measured 0.0727 at QB 11-20). Frozen with a hair of headroom so a
#: test can assert the two independent models still agree — a future re-fit of
#: either that drifts past this is a FINDING, not a rounding change.
TIER_VS_AFFINE_MAX_DRIFT = 0.075


# ------------------------------------------------------------------ data rows

@dataclass(frozen=True)
class RankDispersion:
    """One market row's expert-panel dispersion, in the SOURCE'S OWN rank units.

    This is deliberately NOT converted to points. ``sd``, ``best`` and ``worst``
    are where individual experts placed the player on a ranking board; the spread
    is expert disagreement about his season, which is a proxy for outcome
    uncertainty and not a measurement of it. ``rank_units`` says whether the
    numbers are positional or overall ranks, and ``board_depth`` is how many
    players that position's board carried on this scrape — a spread of 12 means
    something different against a 32-deep D/ST field than a 257-deep WR field.

    TWO OF THESE FIELDS ARE NOT ON THE SAME SCALE, and mistaking them for one
    scale is a bug this module has already shipped once. ``ecr``, ``best`` and
    ``worst`` are the PANEL'S OWN numbers: the consensus (mean) expert rank and
    the min/max any single grader gave him. ``pos_rank`` is DERIVED LOCALLY by
    ``ziggurat/data/nfl/adp_rankings.py::_assign_pos_rank``, which re-ranks the
    rows Ziggurat retained (IDP dropped, duplicates folded) in ECR order — so it
    is a dense 1..N ordinal over OUR row set, not the panel's ordinal. They drift:
    measured on the live 2026-08-28 ``rp`` board, ``best <= ecr <= worst`` holds
    on 807 of 807 rows while ``pos_rank`` falls OUTSIDE ``[best, worst]`` on 105
    of them (e.g. a K with ecr 30.67, best 25, worst 33, derived pos_rank 36).
    **Anything that converts a rank spread into points must hang the midpoint on
    ``ecr``**, which is the scale ``best``/``worst`` live on; ``pos_rank`` is kept
    because it is what the ingester published and what a rank-delta report joins
    on, and ``_market_band`` asserts the ``best <= ecr <= worst`` invariant rather
    than trusting it.
    """

    fantasypros_id: str
    player: str | None
    position: str
    team: str | None
    gsis_id: str | None
    espn_id: str | None
    ecr: float | None
    sd: float | None
    best: int | None
    worst: int | None
    pos_rank: int | None
    ecr_type: str
    rank_units: str
    scrape_date: str | None
    board_depth: int

    @property
    def spread_over_4(self) -> float | None:
        """``(worst - best) / 4`` — the range-to-sigma rule of thumb, in RANK
        units. The literal quantity item 2.3 named as the upgrade. Still a rank."""
        if self.best is None or self.worst is None:
            return None
        return (self.worst - self.best) / 4.0

    @property
    def panel_scale_is_consistent(self) -> bool:
        """``best <= ecr <= worst`` — the panel's consensus must sit inside the
        panel's own min/max. False means the three numbers on this row are not one
        scale and no honest midpoint can be taken from them."""
        if self.ecr is None or self.best is None or self.worst is None:
            return False
        return self.best <= self.ecr <= self.worst


@dataclass(frozen=True)
class PointsCurve:
    """A position's own rank-to-points curve: ``points[k-1]`` = the season house
    points of the k-th best player at this position on the house board.

    Built from coverage-passing ``WeeklyLine``s only (item 3.2: the feed's
    bye-shaped row and its "no forecast" row are byte-identical, so coverage —
    never the point sum — decides whether a player is priceable). Descending by
    construction, so ``at()`` is monotone non-increasing and a better rank is
    never worth fewer points.
    """

    position: str
    points: tuple[float, ...]

    @property
    def depth(self) -> int:
        return len(self.points)

    def contains(self, rank: float | int | None) -> bool:
        return rank is not None and 1 <= float(rank) <= self.depth

    def at(self, rank: float | int) -> float:
        """Season house points at a (possibly fractional) rank, linearly
        interpolated. RAISES outside [1, depth] rather than clamping: clamping
        made deep players' bands collapse to exactly 0.0, which reads as
        CERTAINTY about the player we know the least about."""
        if rank is None:
            raise ValueError(f"no rank supplied for the {self.position} curve")
        r = float(rank)
        if not self.contains(r):
            raise ValueError(
                f"rank {rank} is outside the {self.position} curve's domain "
                f"[1, {self.depth}] — refusing to extrapolate a points band"
            )
        lo = int(math.floor(r))
        hi = min(lo + 1, self.depth)
        frac = r - lo
        return self.points[lo - 1] * (1.0 - frac) + self.points[hi - 1] * frac


@dataclass(frozen=True)
class PricedLine:
    """A coverage-passing projection line reduced to what this module prices off.

    ``played`` — the number of weeks in the window the feed actually FORECAST a
    game for this player — is carried, not derived from the window, and that is
    load-bearing twice. The affine sigma model was fitted on a mean over games
    PLAYED, so dividing a season total by 17 (bye included) hands it a rate ~6%
    low; and the season noise term is ``sigma * sqrt(games)``, so using 17 there
    inflates a bye-week player's spread. Both errors are small, both are silent,
    and both point the same way on every skill player.
    """

    line: WeeklyLine
    points: float
    played: int

    @property
    def per_week(self) -> float:
        return self.points / self.played if self.played else 0.0


@dataclass(frozen=True)
class MarketMatch:
    """The result of joining one house line to the market board, WITH HOW.

    ``how`` is one of ``dst_team`` / ``gsis`` / ``espn`` / ``name_position``
    (matched), or ``position_conflict`` / ``ambiguous_name`` / ``none`` (refused).
    ``note``, when present, is a disclosure that MUST reach the row's ``reasons``:
    either why the match was refused, or the caveat on a weaker join. A match
    whose provenance the operator cannot see is the silent fallback this module
    exists to prevent.
    """

    row: RankDispersion | None
    how: str
    note: str | None

    @property
    def matched(self) -> bool:
        return self.row is not None


@dataclass(frozen=True)
class MarketBoard:
    """One resolved market scrape, indexed for the three join paths this system
    uses (gsis for skill, ESPN id for the league layer, team abbr for D/ST).

    FOUR indexes, not three: ``by_name_pos`` is the LAST-RESORT join and it earns
    its place from a measurement — 52 of the 807 rows on the live 2026-08-28 board
    carry neither a gsis nor an ESPN id (14 of 44 kickers, 23 of 176 TEs), so
    without it the module reported "this player is not on the board" for players
    the board plainly ranks. Its value is ``None`` where a (name, position) key is
    shared, which ``resolve`` refuses rather than resolving to a coin flip.

    A RANK BOARD IS A JOINT OBJECT and this pins it to ONE ``scrape_date`` on
    purpose. ``get_adp_rankings`` resolves per (fantasypros_id, ecr_type,
    scrape_date), so it hands back the whole weekly panel — six scrapes on the
    live table — and taking "each player's newest row" silently mixes a
    2026-07-24 rank with a 2026-08-28 rank on the same board. Ranks from
    different scrapes are not comparable to each other or to a curve built today.
    A player who was on an older board and is absent from the resolved one is
    absent, which is a fact about the market, not a gap to paper over.
    """

    ecr_type: str
    rank_units: str
    scrape_date: str | None
    rows: tuple[RankDispersion, ...]
    by_gsis: Mapping[str, RankDispersion]
    by_espn: Mapping[str, RankDispersion]
    by_dst_team: Mapping[str, RankDispersion]
    #: ``(normalised name, position) -> row``, with the value ``None`` where the
    #: board carries more than one player under that key. The LAST-RESORT join
    #: (see ``resolve``); a ``None`` value is an ambiguity that must be refused,
    #: never silently resolved to the first row.
    by_name_pos: Mapping[tuple[str, str], "RankDispersion | None"]
    #: Non-D/ST rows on THIS scrape carrying neither a gsis nor an ESPN id — the
    #: rows only the name join can reach. Counted rather than quoted from a past
    #: measurement, because the number is what a disclosure on a name-joined row
    #: claims about the board in front of the operator.
    idless_rows: int
    depth_by_position: Mapping[str, int]
    populated: bool
    floors: Floors
    notes: tuple[str, ...]

    def resolve(
        self, *, position: str, gsis_id: str | None = None,
        espn_id: str | None = None, team: str | None = None,
        player: str | None = None,
    ) -> "MarketMatch":
        """The join, WITH ITS PROVENANCE — the form ``_market_band`` uses.

        Three things here are not obvious and each of them was a real defect:

        1. THE POSITION IS CHECKED, not just accepted. An id join answers "which
           market row is this player", not "which market row is this player AT
           THIS POSITION", and the two feeds do disagree: on the live 2026-08-30
           data one player is ``RB`` in ``projections`` and ``TE`` on the ``rp``
           board. Taking that row would price a TE's rank spread through the RB
           points curve — measured 3.3x wrong on a synthetic reproduction — while
           the reason string named the curve that was NOT used. A conflict is
           refused and SAID, never resolved by guessing which feed is right.
        2. A NAME JOIN EXISTS, because the alternative was a lie. ``adp_rankings``
           carries a NULL ``gsis_id`` AND a NULL ``espn_id`` on 52 of the 807 live
           rows (14 of 44 kickers, 23 of 176 TEs), so the id join misses players
           the board plainly ranks, and the old code reported that as "this player
           is not on the board" — a false statement about the market. The join is
           last-resort, requires a UNIQUE (name, position) match, fires ONLY when
           the market row carries no ids of its own (a crosswalk GAP, not a
           crosswalk disagreement), and is DISCLOSED on the row: validated against
           the id join on the 704 live rows where both fire, it agrees on 703 and
           the single disagreement is the one ambiguous name on the board, which
           the uniqueness rule already refuses.
        3. AMBIGUITY IS A REFUSAL, not a coin flip. Two players share a
           (name, position) key on the live board; picking either is a 50%
           chance of pricing the wrong man's expert spread.
        """
        pos = canon_position(position) or position
        if pos == "DST":
            row = self.by_dst_team.get(_norm_team(team)) if team else None
            return MarketMatch(row, "dst_team" if row is not None else "none", None)

        for how, row in (
            ("gsis", self.by_gsis.get(gsis_id) if gsis_id else None),
            ("espn", self.by_espn.get(str(espn_id)) if espn_id else None),
        ):
            if row is None:
                continue
            if row.position != pos:
                return MarketMatch(None, "position_conflict", (
                    f"NO per-player market dispersion: the projection feed calls this "
                    f"player a {pos} and the '{self.ecr_type}' board of "
                    f"{self.scrape_date} ranks the same id as a {row.position} "
                    f"({row.position}{row.pos_rank}). Reading that rank off the {pos} "
                    f"points curve would price one position's spread on another "
                    f"position's scale, so the conversion is REFUSED rather than "
                    f"guessing which feed is right. "
                    f"Using {POSITIONAL_BAND_PRIOR_LABEL}."
                ))
            return MarketMatch(row, how, None)

        key = (_norm_name(player), pos)
        if key[0] is not None and key in self.by_name_pos:
            row = self.by_name_pos[key]
            if row is None:
                return MarketMatch(None, "ambiguous_name", (
                    f"NO per-player market dispersion: the '{self.ecr_type}' board "
                    f"carries no id for this player and MORE THAN ONE {pos} under the "
                    f"same name, so a name match could price the wrong man's expert "
                    f"spread. Refusing. Using {POSITIONAL_BAND_PRIOR_LABEL}."
                ))
            if row.gsis_id or row.espn_id:
                # The name matches but the market row carries ids of its OWN that
                # the id join already failed on. That is a crosswalk DISAGREEMENT,
                # not a crosswalk gap, and overriding it by name is how a name
                # join prices the wrong man. The name join exists only for rows
                # the id join cannot reach at all.
                return MarketMatch(None, "id_mismatch", (
                    f"NO per-player market dispersion: the '{self.ecr_type}' board of "
                    f"{self.scrape_date} carries a {pos} with this player's name, but "
                    f"that row's own gsis/ESPN id is not his — the two feeds disagree "
                    f"about identity rather than merely lacking it, so matching them "
                    f"by name could price a different player's expert spread. "
                    f"Refusing. Using {POSITIONAL_BAND_PRIOR_LABEL}."
                ))
            return MarketMatch(row, "name_position", (
                f"JOINED BY NAME, NOT BY ID: the '{self.ecr_type}' market row for "
                f"{row.player} carries no gsis id and no ESPN id "
                f"({self.idless_rows} of {len(self.rows)} rows on this scrape do not), "
                f"so he was matched on name + position instead. That is weaker than an "
                f"id match and it is why this line is here. VALIDATION (labelled "
                f"hypothesis, measured 2026-08-30 on the live rp board of 2026-08-28): "
                f"on the 704 players where the id join and the name join both fire they "
                f"agreed on 703, and the single exception was a duplicated name, which "
                f"this join refuses outright rather than guessing."
            ))
        return MarketMatch(None, "none", None)

    def lookup(
        self, *, position: str, gsis_id: str | None = None,
        espn_id: str | None = None, team: str | None = None,
        player: str | None = None,
    ) -> RankDispersion | None:
        """``resolve(...).row`` — the bare row, for a caller that does not need the
        provenance. Everything that PRINTS uses ``resolve``, because the note it
        carries is a disclosure Rule 6 requires to reach the operator."""
        return self.resolve(
            position=position, gsis_id=gsis_id, espn_id=espn_id,
            team=team, player=player,
        ).row


@dataclass(frozen=True)
class PlayerDispersion:
    """One player's floor / ceiling, with every component and its provenance.

    ``floor_points`` / ``ceiling_points`` are ``projected_points -/+
    season_sigma_points`` — a +/-1 sigma SEASON band, combining the market's
    spread on the mean with the measured week-to-week noise. The two components
    are also published separately because they are different quantities and their
    magnitudes differ by an order of magnitude on consensus players.

    ``market_*`` fields are ``None`` when the rank-to-points conversion was
    refused (out of curve domain, wrong rank units, market row absent or
    unjoinable, position conflict, table unpopulated). They are never silently
    zero: a missing band is a stated absence, and ``reasons`` says which it was.

    ``market_band_points`` IS NOT ``ceiling - floor`` ON EVERY ROW, and that is
    deliberate. ``market_floor_points`` is clamped at 0 because a season total
    cannot be negative; the BAND is measured from ``market_floor_raw_points``, the
    untruncated conversion, because it is a SPREAD and truncating a spread at a
    level boundary reports false confidence. On the live 2026-08-30 board the
    clamp fires on 40 of 423 priced rows and would have understated their market
    sigma by a median 7.8% and a maximum 43.4%. ``market_floor_clamped`` flags
    those rows and their ``reasons`` say so in words.

    ``market_join`` records HOW the market row was found — ``gsis`` / ``espn`` /
    ``dst_team`` / ``name_position`` — or why none was used: ``position_conflict``,
    ``ambiguous_name``, ``none``. A ``name_position`` join is weaker than an id
    join and the row's reasons disclose it.
    """

    key: tuple
    player: str | None
    position: str
    team: str | None
    gsis_id: str | None
    espn_id: str | None

    projected_points: float          # house season points (the level; from scoring.py)
    board_rank: int                  # within-position rank on the house board

    market_floor_points: float | None        # clamped at 0 (a season total is not negative)
    market_floor_raw_points: float | None    # the UNTRUNCATED conversion
    market_floor_clamped: bool
    market_ceiling_points: float | None
    market_band_points: float | None         # ceiling - RAW floor (a spread, never truncated)
    market_sigma_points: float | None        # band / RANGE_TO_SIGMA_DIVISOR
    market_join: str                         # how the market row was found, or why not

    weekly_sigma: float
    weekly_sigma_source: str
    weekly_noise_sigma_points: float     # sqrt(weeks) * weekly_sigma

    season_sigma_points: float
    floor_points: float
    ceiling_points: float

    relative_dispersion: float
    relative_source: str             # market | positional_prior
    rank: RankDispersion | None
    reasons: tuple[str, ...]

    @property
    def has_market_band(self) -> bool:
        return self.market_band_points is not None


@dataclass(frozen=True)
class CoverageRow:
    """One position's four-way split.

    ``no_house_line`` is separate from ``positional_prior`` on purpose: "the
    market does not rank him" and "this system cannot price him at all" are
    different failures with different fixes, and folding them together made the
    report blame the dispersion fallback for 97 quarterbacks that the PROJECTION
    feed never covered.

    ``positional_prior`` counts every row that got NO market row: absent from the
    board, present but unjoinable (no id and no unique name), or ranked there at
    a different position. It does NOT assert "not on the board" — that claim was
    false for 4 of the 22 live rows that made it, and each row's own reasons name
    which of the three it was."""

    position: str
    total: int
    market_band: int
    market_rank_only: int
    positional_prior: int
    no_house_line: int = 0

    @property
    def market_band_share(self) -> float:
        return self.market_band / self.total if self.total else 0.0


@dataclass(frozen=True)
class DispersionBoard:
    """Every priceable player's dispersion, plus what the board could not do."""

    season: int
    as_of: str
    weeks: tuple[int, ...]
    ecr_type: str
    rank_units: str
    scrape_date: str | None
    reference_band: float
    reference_source: str            # cohort_median | frozen_prior
    rows: Mapping[tuple, PlayerDispersion]
    curves: Mapping[str, PointsCurve]
    coverage: tuple[CoverageRow, ...]
    banners: tuple[str, ...] = ()

    def by_position(self, position: str) -> list[PlayerDispersion]:
        pos = canon_position(position) or position
        return sorted(
            (r for r in self.rows.values() if r.position == pos),
            key=lambda r: (r.board_rank, str(r.key)),
        )


# ----------------------------------------------------------------- small utils

def _norm_team(team) -> str | None:
    if team is None:
        return None
    raw = str(team).strip().upper()
    if not raw:
        return None
    return base.TEAM_ALIASES.get(raw, raw)


#: Generational suffixes: the two feeds disagree about them freely ("Marvin
#: Harrison Jr." vs "Marvin Harrison"), and they carry no identifying information
#: that the first+last name does not.
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def _norm_name(name) -> str | None:
    """A conservative display-name key for the LAST-RESORT market join.

    Deliberately crude — lowercase, strip punctuation and generational suffixes,
    collapse whitespace. It is not fuzzy and never will be: this key only ever
    decides whether two rows are the SAME player, and it is paired with a
    uniqueness requirement so a collision refuses instead of guessing. Anything
    looser (edit distance, nicknames) trades a stated absence for a silent wrong
    answer, which is the trade this module exists to refuse.
    """
    if name is None:
        return None
    raw = str(name).lower().replace(".", "").replace("'", "").replace("-", " ")
    toks = [t for t in re.sub(r"[^a-z0-9 ]", " ", raw).split() if t not in _NAME_SUFFIXES]
    return " ".join(toks) or None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _sd_text(sd: float | None) -> str:
    """``sd`` is nullable in the DDL; a reason string must never crash on it
    and must never imply a number that is not there."""
    return "sd not reported" if sd is None else f"sd {sd:.1f} ranks"


# ------------------------------------------------------------------ accessors

def market_rank_dispersion(
    conn,
    *,
    as_of,
    season: int,
    ecr_type: str = DEFAULT_ECR_TYPE,
    floors: Floors = DEFAULT_FLOORS,
    view: base.AsOfView = "historical",
) -> MarketBoard:
    """The expert-panel rank dispersion board, resolved to ONE scrape.

    Rule 1: ``as_of`` is keyword-only with no default and threads straight into
    ``adp_rankings.get_adp_rankings`` (which gates ``knowable_as_of`` and, under
    the default ``historical`` view, ``retrieved_as_of`` too). This layer never
    widens the gate — it only picks the newest ``scrape_date`` among the rows the
    gate already allowed, so a scrape published after ``as_of`` is invisible here
    for the same reason it is invisible there.

    ``ecr_type`` defaults to the full-PPR redraft POSITIONAL series (see the
    module docstring for the ``fp_page`` evidence). Any series may be requested;
    ``rank_units`` records what the returned ``sd``/``best``/``worst`` are counted
    in, and the points conversion downstream refuses anything but ``positional``.
    """
    rows = adp_rankings.get_adp_rankings(
        conn, as_of=as_of, season=season, ecr_type=ecr_type, view=view
    )
    notes: list[str] = []
    if ecr_type not in ECR_PAGES:
        notes.append(
            f"ecr_type {ecr_type!r} is not one of the nine series this source "
            f"publishes ({', '.join(sorted(ECR_PAGES))}) — treating it as unknown units"
        )
    elif ecr_type not in REDRAFT_PPR_ECR_TYPES:
        notes.append(
            f"WARNING: ecr_type {ecr_type!r} is {ECR_PAGES[ecr_type]} — this league is "
            "REDRAFT, FULL PPR, 1-QB, whose board is 'ro'/'rp' "
            "(/nfl/rankings/ppr-cheatsheets.php). You are pricing against a "
            "different game's market."
        )

    scrapes = sorted({r["scrape_date"] for r in rows if r["scrape_date"]})
    scrape_date = scrapes[-1] if scrapes else None
    board = [r for r in rows if r["scrape_date"] == scrape_date] if scrape_date else []
    if len(scrapes) > 1:
        notes.append(
            f"resolved to the newest scrape {scrape_date} ({len(board)} rows); "
            f"{len(scrapes) - 1} older scrape(s) in range were dropped — ranks from "
            "different scrapes are not comparable on one board"
        )

    depth: dict[str, int] = {}
    for r in board:
        pos = canon_position(r["position"]) or r["position"]
        depth[pos] = depth.get(pos, 0) + 1

    out: list[RankDispersion] = []
    for r in board:
        pos = canon_position(r["position"]) or r["position"]
        out.append(RankDispersion(
            fantasypros_id=str(r["fantasypros_id"]),
            player=r["player"],
            position=pos,
            team=_norm_team(r["team"]),
            gsis_id=r["gsis_id"],
            espn_id=None if r["espn_id"] is None else str(r["espn_id"]),
            ecr=None if r["ecr"] is None else float(r["ecr"]),
            sd=None if r["sd"] is None else float(r["sd"]),
            best=None if r["best"] is None else int(r["best"]),
            worst=None if r["worst"] is None else int(r["worst"]),
            pos_rank=None if r["pos_rank"] is None else int(r["pos_rank"]),
            ecr_type=ecr_type,
            rank_units=RANK_UNITS.get(ecr_type, "unknown"),
            scrape_date=r["scrape_date"],
            board_depth=depth.get(pos, 0),
        ))
    out.sort(key=lambda d: (d.position, d.pos_rank or 10**6, d.fantasypros_id))

    populated = len(out) >= floors.board_rows
    if not populated:
        notes.append(
            f"adp_rankings is UNPOPULATED at this as-of: {len(out)} row(s) on the "
            f"resolved board, below the {floors.board_rows}-row floor. Every player "
            "falls back to the labelled positional prior."
        )

    by_gsis: dict[str, RankDispersion] = {}
    by_espn: dict[str, RankDispersion] = {}
    by_dst: dict[str, RankDispersion] = {}
    by_name: dict[tuple[str, str], RankDispersion | None] = {}
    for d in out:
        if d.position == "DST":
            if d.team is not None:
                by_dst.setdefault(d.team, d)
            continue
        if d.gsis_id:
            by_gsis.setdefault(d.gsis_id, d)
        if d.espn_id:
            by_espn.setdefault(d.espn_id, d)
        name = _norm_name(d.player)
        if name is not None:
            key = (name, d.position)
            # A SECOND player under one key POISONS it to None rather than losing
            # to setdefault: the first row winning silently is exactly how a name
            # join prices the wrong man's spread.
            if key in by_name:
                if by_name[key] is None or by_name[key].fantasypros_id != d.fantasypros_id:
                    by_name[key] = None
            else:
                by_name[key] = d
    collisions = sum(1 for v in by_name.values() if v is None)
    if collisions:
        notes.append(
            f"{_plural(collisions, 'duplicated (name, position) key')} on this board — "
            "the last-resort name join refuses those players rather than guessing "
            "which of them a rank belongs to"
        )

    return MarketBoard(
        ecr_type=ecr_type,
        rank_units=RANK_UNITS.get(ecr_type, "unknown"),
        scrape_date=scrape_date,
        rows=tuple(out),
        by_gsis=MappingProxyType(by_gsis),
        by_espn=MappingProxyType(by_espn),
        by_dst_team=MappingProxyType(by_dst),
        by_name_pos=MappingProxyType(by_name),
        idless_rows=sum(
            1 for d in out
            if d.position != "DST" and not d.gsis_id and not d.espn_id
        ),
        depth_by_position=MappingProxyType(depth),
        populated=populated,
        floors=floors,
        notes=tuple(notes),
    )


def priceable_lines(
    conn,
    *,
    as_of,
    season: int,
    weeks: Iterable[int] | None = None,
    source: str = "sleeper_rotowire",
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    view: base.AsOfView = "historical",
) -> dict[tuple, PricedLine]:
    """``key -> PricedLine`` for lines with enough COVERAGE.

    Coverage, never the point sum, decides (item 3.2's critical finding: the
    feed's bye row and its "no forecast" row are byte-identical, so a 99%-owned
    WR with one real week summed to a plausible number and read as worthless).
    A player must carry projections for at least ``COVERAGE_FLOOR`` of the weeks
    he could actually play — the requested window minus his own bye.

    Rule 1: ``as_of``/``view`` thread into ``weekly_lines`` and ``bye_map``; this
    layer adds no gate of its own and cannot widen theirs.
    """
    week_list = sorted(set(DEFAULT_WEEKS if weeks is None else weeks))
    lines = weekly_lines(
        conn, as_of=as_of, season=season, weeks=week_list,
        source=source, rules=rules, view=view,
    )
    byes = bye_map(conn, as_of=as_of, season=season, source=source, view=view)
    out: dict[tuple, PricedLine] = {}
    for key, line in lines.items():
        bye = byes.bye_of(line.team)
        playable = [w for w in week_list if w != bye]
        covered = set(line.played_weeks) & set(week_list)
        if not playable or len(covered) < COVERAGE_FLOOR * len(playable):
            continue
        out[key] = PricedLine(
            line=line,
            points=sum(line.points.get(w, 0.0) for w in week_list),
            played=len(covered),
        )
    return out


def build_points_curves(
    priced: Mapping[tuple, PricedLine],
) -> dict[str, PointsCurve]:
    """The house board's own within-position rank-to-points curves.

    Pure — it takes the already-priced lines rather than reading, so the curve and
    the rows are provably built from the SAME cohort (two independent reads could
    diverge and nothing would say so).
    """
    buckets: dict[str, list[float]] = {}
    for pl in priced.values():
        buckets.setdefault(pl.line.position, []).append(float(pl.points))
    return {
        pos: PointsCurve(position=pos, points=tuple(sorted(vals, reverse=True)))
        for pos, vals in buckets.items()
    }


# --------------------------------------------------------------- the main board

def _market_label(market: MarketBoard) -> str:
    """What series this row's band actually came from — read off the requested
    ``ecr_type``, never hard-coded. Hard-coding "FantasyPros full-PPR redraft
    consensus" meant a caller asking for ``dp`` got 431 rows each naming a market
    they did not come from, under a board banner that said the opposite."""
    page = ECR_PAGES.get(market.ecr_type)
    named = {
        "rp": "FantasyPros full-PPR redraft consensus, positional ranks",
        "ro": "FantasyPros full-PPR redraft consensus, overall ranks",
    }.get(market.ecr_type)
    if named is None:
        named = (
            f"FantasyPros '{market.ecr_type}' consensus ({page})"
            if page else f"FantasyPros '{market.ecr_type}' consensus (series not recognised)"
        )
        named += " — NOT this league's redraft full-PPR market"
    return named


@dataclass(frozen=True)
class _Band:
    """The conversion's output: the published floor, the UNTRUNCATED floor, the
    ceiling, and whether the clamp fired. ``build_dispersion`` needs all four
    because the published floor and the spread are different questions."""

    floor: float
    raw_floor: float
    ceiling: float
    clamped: bool

    @property
    def band(self) -> float:
        """The market's spread, measured from the UNTRUNCATED floor."""
        return self.ceiling - self.raw_floor


def _market_band(
    match: MarketMatch,
    curve: PointsCurve | None,
    projected: float,
    *,
    market: MarketBoard,
) -> tuple[_Band | None, list[str]]:
    """``(band, reasons)`` for the rank-to-points conversion.

    Returns ``(None, [why])`` for every refusal, and the reason names which of the
    refusals it was. Nothing here clamps a RANK into the curve or invents a band;
    a refusal is a stated absence (Rule 6).

    THE MIDPOINT IS ``ecr``, NOT ``pos_rank`` — see ``RankDispersion``. They are
    two different rank scales and hanging ``best``/``worst`` off the wrong one
    shifted the published floor and ceiling by up to 34 house points and produced
    a market FLOOR ABOVE the player's own projection on the live board.
    """
    rank = match.row
    if not market.populated:
        return None, [
            f"NO per-player market dispersion: the ECR board is unpopulated at this "
            f"as-of ({len(market.rows)} rows, floor {market.floors.board_rows}). "
            f"Using {POSITIONAL_BAND_PRIOR_LABEL}."
        ]
    if rank is None:
        # A REFUSED join and an ABSENT player are different facts; the note from
        # ``resolve`` says which, and "not on the board" is only claimed when the
        # board really was searched by name and came up empty.
        if match.note is not None:
            return None, [match.note]
        return None, [
            f"NO per-player market dispersion: no row for this player on the "
            f"'{market.ecr_type}' board of {market.scrape_date} — searched by "
            f"gsis id, ESPN id and by name+position, and none matched. "
            f"Using {POSITIONAL_BAND_PRIOR_LABEL}."
        ]
    # A weaker join still MATCHED; its caveat leads the row's band reasons.
    lead = [match.note] if match.note else []
    if market.rank_units != "positional":
        return None, [*lead,
            f"market dispersion reported in {market.rank_units.upper()} RANK units "
            f"(sd {rank.sd}, {rank.best}-{rank.worst} of a {rank.board_depth}-deep "
            f"{rank.position} field on the '{market.ecr_type}' board) and NOT converted "
            "to points: an overall-rank spread interleaves six positions with "
            "incomparable point scales, so no honest rank-to-points curve exists. "
            f"Using {POSITIONAL_BAND_PRIOR_LABEL} for the relative number."
        ]
    if rank.board_depth < market.floors.position_rows:
        return None, [*lead,
            f"NO per-player market dispersion: the {rank.position} board carries only "
            f"{rank.board_depth} rows (floor {market.floors.position_rows}). "
            f"Using {POSITIONAL_BAND_PRIOR_LABEL}."
        ]
    if rank.best is None or rank.worst is None or rank.ecr is None:
        return None, [*lead,
            f"NO per-player market dispersion: the {market.ecr_type} row for this "
            "player has no best/worst/consensus rank. "
            f"Using {POSITIONAL_BAND_PRIOR_LABEL}."
        ]
    if not rank.panel_scale_is_consistent:
        # The three numbers must be one scale or the midpoint means nothing. This
        # holds on 807 of 807 live rows and is checked anyway, because the bug it
        # guards against (a locally-DERIVED ordinal used as the midpoint) already
        # shipped once and was invisible in every output.
        return None, [*lead,
            f"NO per-player market dispersion: the '{market.ecr_type}' row for this "
            f"player reports consensus {rank.position}{rank.ecr:.1f} with a grader "
            f"range of {rank.best}-{rank.worst} — the consensus sits OUTSIDE its own "
            "min/max, so those three numbers are not one rank scale and no honest "
            f"midpoint exists. Using {POSITIONAL_BAND_PRIOR_LABEL}."
        ]
    if curve is None or curve.depth < market.floors.curve_depth:
        depth = 0 if curve is None else curve.depth
        return None, [*lead,
            f"market rank {rank.position}{rank.ecr:.1f} ({rank.best}-{rank.worst}) NOT "
            f"converted to points: the house board prices only {depth} {rank.position}s, "
            f"below the {market.floors.curve_depth}-deep floor a rank-to-points curve needs. "
            f"Using {POSITIONAL_BAND_PRIOR_LABEL} for the relative number."
        ]
    if not (curve.contains(rank.best) and curve.contains(rank.worst)
            and curve.contains(rank.ecr)):
        return None, [*lead,
            f"market rank {rank.position}{rank.ecr:.1f} ({rank.best}-{rank.worst}) is "
            f"OUTSIDE the house board's {rank.position} curve, which prices ranks "
            f"1-{curve.depth}. Refusing to extrapolate a floor/ceiling rather than "
            f"clamping (clamping collapses the band to zero, which reads as certainty "
            f"about the player we know least). Using {POSITIONAL_BAND_PRIOR_LABEL}."
        ]

    mid = curve.at(rank.ecr)
    ceiling = projected + (curve.at(rank.best) - mid)
    raw_floor = projected + (curve.at(rank.worst) - mid)
    band = _Band(
        floor=max(0.0, raw_floor), raw_floor=raw_floor,
        ceiling=ceiling, clamped=raw_floor < 0.0,
    )
    reasons = [*lead,
        f"expert panel's consensus rank is {rank.position}{rank.ecr:.1f} (the average "
        f"of the graders' ranks) and individual graders put him anywhere from "
        f"{rank.position}{rank.best} to {rank.position}{rank.worst} "
        f"({_sd_text(rank.sd)}) on a {rank.board_depth}-deep board — "
        f"{_market_label(market)}, scrape {rank.scrape_date}.",
        f"converted through the house board's own {rank.position} rank-to-points "
        f"curve ({curve.depth} priced {rank.position}s): that rank spread is worth "
        f"{band.floor:.0f}-{band.ceiling:.0f} season points around his "
        f"{projected:.0f}-point projection. The spread is the market's; the level is "
        f"this system's — the conversion assumes the panel's {rank.position} ordering "
        "and this board's are the same scale (they agree on ~92% of pairs; the level "
        "is never taken from it).",
    ]
    if band.clamped:
        reasons.append(
            f"THE PUBLISHED FLOOR IS CLAMPED. Hung on his own projection the "
            f"market's worst-case rank ({rank.position}{rank.worst}) prices out at "
            f"{raw_floor:.0f} season points, and a season total cannot be negative, "
            f"so the floor shown above is 0 rather than {raw_floor:.0f}. That is an "
            "artefact of the clamp, NOT the panel saying he might score nothing — it "
            "means the market's downside for him is bigger than this board's entire "
            "projection for him, i.e. the two disagree about his LEVEL and the "
            "additive band has overshot. The spread used for his sigma below is the "
            f"UNTRUNCATED {band.band:.0f} points, because truncating a spread at a "
            "level boundary would report false confidence about exactly the players "
            "the market and this board disagree about most."
        )
    return band, reasons


def build_dispersion(
    conn,
    *,
    as_of,
    season: int,
    weeks: Iterable[int] | None = None,
    ecr_type: str = DEFAULT_ECR_TYPE,
    floors: Floors = DEFAULT_FLOORS,
    source: str = "sleeper_rotowire",
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    variance: VarianceModel = DEFAULT_VARIANCE,
    tier_model: TierSigmaModel = DEFAULT_TIER_SIGMA,
    view: base.AsOfView = "historical",
    today=None,
) -> DispersionBoard:
    """Floor / ceiling for every priceable player, with provenance on every row.

    Rule 1: ``as_of`` keyword-only, no default, threaded into every read
    (projections, byes, ECR) and never widened. ``today`` is the OPERATIONAL clock
    for the staleness banner only — deliberately separate from the ``as_of`` data
    gate, the same split ``core/marginal.py`` uses, because a stale read carries a
    perfectly valid ``knowable_as_of`` and is otherwise Rule-1-invisible.
    """
    week_list = tuple(sorted(set(DEFAULT_WEEKS if weeks is None else weeks)))
    cutoff = normalize_as_of(as_of).isoformat()

    priced = priceable_lines(
        conn, as_of=as_of, season=season, weeks=week_list,
        source=source, rules=rules, view=view,
    )
    curves = build_points_curves(priced)
    market = market_rank_dispersion(
        conn, as_of=as_of, season=season, ecr_type=ecr_type, floors=floors, view=view
    )

    # Board rank per position: our own descending-points order. Deterministic —
    # ties break on the player key, never on dict order.
    ordered: dict[str, list[tuple]] = {}
    for key, pl in priced.items():
        ordered.setdefault(pl.line.position, []).append((-float(pl.points), str(key), key))
    board_rank: dict[tuple, int] = {}
    for entries in ordered.values():
        entries.sort()
        for i, (_neg, _s, key) in enumerate(entries, start=1):
            board_rank[key] = i

    # Pass 1: the market bands, so the relative reference is the cohort's own
    # median. The join is resolved ONCE per player and reused, so the row's
    # ``rank`` and the band it was priced from can never come from two calls.
    matches: dict[tuple, MarketMatch] = {}
    bands: dict[tuple, tuple[_Band | None, list[str]]] = {}
    for key, pl in priced.items():
        line = pl.line
        matches[key] = market.resolve(
            position=line.position, gsis_id=line.gsis_id,
            espn_id=line.espn_id, team=line.team, player=line.player,
        )
        bands[key] = _market_band(
            matches[key], curves.get(line.position), float(pl.points), market=market
        )

    sized = sorted(b.band for b, _ in bands.values() if b is not None)
    if len(sized) >= floors.reference_rows:
        reference_band = statistics.median(sized)
        reference_source = "cohort_median"
    else:
        reference_band = REFERENCE_BAND_PRIOR
        reference_source = "frozen_prior"

    rows: dict[tuple, PlayerDispersion] = {}
    for key, pl in priced.items():
        line = pl.line
        pos = line.position
        projected = float(pl.points)
        match = matches[key]
        rank_row = match.row
        band_row, band_reasons = bands[key]
        rank_here = board_rank[key]

        # Both the fitted mean and the noise aggregation are per GAME PLAYED, not
        # per calendar week (see PricedLine).
        n_weeks = pl.played
        per_week_mu = pl.per_week
        sigma_est = tier_model.sigma(pos, mu=per_week_mu, rank=rank_here, variance=variance)
        weekly_noise = sigma_est.sigma * math.sqrt(n_weeks)

        if band_row is not None:
            band = band_row.band
            # RANGE-TO-SIGMA, the same /4 item 2.3 named: a symmetric best/worst
            # range is read as roughly +/-2 sigma, so sigma = range / 4. It is a
            # RULE OF THUMB, not a measurement, and it is the divisor every
            # published season sigma, floor and ceiling scales with.
            market_sigma = band / RANGE_TO_SIGMA_DIVISOR
            relative = band / reference_band if reference_band else 0.0
            relative_source = "market"
        else:
            band = market_sigma = None
            relative = POSITIONAL_BAND_PRIOR.get(pos, 1.0)
            relative_source = "positional_prior"

        season_sigma_pts = season_sigma(
            weekly=sigma_est.sigma, weeks=n_weeks, market_sigma=market_sigma
        )

        reasons = [
            f"projected {projected:.0f} house points over "
            f"{_plural(n_weeks, 'forecast week')} of weeks {week_list[0]}-{week_list[-1]} "
            f"({per_week_mu:.1f}/wk), {pos}{rank_here} on this board — priced through "
            "ziggurat.core.scoring, per week then summed.",
        ]
        reasons.extend(band_reasons)
        reasons.append(sigma_est.reason)
        if market_sigma is not None:
            reasons.append(
                f"season spread combines the two INDEPENDENT parts in quadrature: "
                f"market disagreement about his rate (sigma {market_sigma:.1f} season pts, "
                f"= the {band:.0f}-point band / {RANGE_TO_SIGMA_DIVISOR:.0f}, reading a "
                "best-to-worst range as roughly +/-2 sigma — a rule of thumb, not a "
                f"measurement) and week-to-week scoring noise (sigma {weekly_noise:.1f} "
                f"season pts, = {sigma_est.sigma:.1f}/wk x sqrt({n_weeks})) "
                f"-> {season_sigma_pts:.1f}. "
                "ASSUMPTION, not a measurement: the weekly noise is treated as "
                "independent week to week and independent of the rate uncertainty."
            )
        else:
            reasons.append(
                f"season spread is week-to-week noise ONLY (sigma {weekly_noise:.1f} season "
                f"pts, = {sigma_est.sigma:.1f}/wk x sqrt({n_weeks})); with no market band "
                "the rate uncertainty is UNPRICED here, so this floor/ceiling is "
                "NARROWER than the truth."
            )
        reasons.append(
            "AVAILABILITY IS NOT IN THIS BAND. Missed games are priced separately by "
            "core/marginal.py's AvailabilityModel; adding them here would double-count."
        )

        rows[key] = PlayerDispersion(
            key=key,
            player=line.player,
            position=pos,
            team=line.team,
            gsis_id=line.gsis_id,
            espn_id=line.espn_id,
            projected_points=projected,
            board_rank=rank_here,
            market_floor_points=None if band_row is None else band_row.floor,
            market_floor_raw_points=None if band_row is None else band_row.raw_floor,
            market_floor_clamped=bool(band_row is not None and band_row.clamped),
            market_ceiling_points=None if band_row is None else band_row.ceiling,
            market_band_points=band,
            market_sigma_points=market_sigma,
            market_join=match.how,
            weekly_sigma=sigma_est.sigma,
            weekly_sigma_source=sigma_est.source,
            weekly_noise_sigma_points=weekly_noise,
            season_sigma_points=season_sigma_pts,
            floor_points=max(0.0, projected - season_sigma_pts),
            ceiling_points=projected + season_sigma_pts,
            relative_dispersion=relative,
            relative_source=relative_source,
            rank=rank_row,
            reasons=tuple(reasons),
        )

    coverage = _coverage(rows, market)
    banners = _staleness_banners(
        conn, priced=priced, market=market, as_of=cutoff, season=season, today=today,
    )
    banners = list(market.notes) + list(banners)
    if reference_source == "frozen_prior":
        banners.append(
            f"relative dispersion normalised against the FROZEN reference band "
            f"{REFERENCE_BAND_PRIOR:.1f} (only {len(sized)} priced band(s) on this "
            f"board, floor {floors.reference_rows}) — {POSITIONAL_BAND_PRIOR_LABEL}"
        )

    return DispersionBoard(
        season=season,
        as_of=cutoff,
        weeks=week_list,
        ecr_type=market.ecr_type,
        rank_units=market.rank_units,
        scrape_date=market.scrape_date,
        reference_band=reference_band,
        reference_source=reference_source,
        rows=rows,
        curves=curves,
        coverage=coverage,
        banners=tuple(banners),
    )


def _coverage(
    rows: Mapping[tuple, PlayerDispersion], market: MarketBoard,
) -> tuple[CoverageRow, ...]:
    """Per-position: how many rows got a per-player market band, how many carried
    a market rank the conversion refused, how many fell back to the prior."""
    acc: dict[str, list[int]] = {}
    for row in rows.values():
        cell = acc.setdefault(row.position, [0, 0, 0, 0])
        cell[0] += 1
        if row.has_market_band:
            cell[1] += 1
        elif row.rank is not None:
            cell[2] += 1
        else:
            cell[3] += 1
    return tuple(
        CoverageRow(position=pos, total=c[0], market_band=c[1],
                    market_rank_only=c[2], positional_prior=c[3])
        for pos, c in sorted(acc.items())
    )


def draftable_coverage(
    conn,
    *,
    as_of,
    season: int,
    board: DispersionBoard,
    view: base.AsOfView = "historical",
) -> tuple[CoverageRow, ...]:
    """Coverage measured against the DRAFTABLE UNIVERSE, not against what we could
    price — the number that says whether this module is usable on draft night.

    ``build_dispersion`` only emits rows for players the projection feed covers,
    so its own coverage table cannot see a draftable player who has no priceable
    line at all. This reads the live ESPN board (the room's actual universe) and
    reports the four-way split: market band / market rank only / positional prior
    / no priceable house line. Kept a separate function so ``build_dispersion``
    never depends on the ESPN board being ingested.
    """
    from ziggurat.data.nfl import espn_ranks

    universe = espn_ranks.get_espn_draft_ranks(
        conn, as_of=as_of, season=season, view=view
    )
    by_gsis = {r.gsis_id: r for r in board.rows.values() if r.gsis_id}
    by_espn = {r.espn_id: r for r in board.rows.values() if r.espn_id}
    by_dst = {r.team: r for r in board.rows.values() if r.position == "DST"}
    # Built ONCE. It scans the whole players table and logs a warning per
    # ambiguous id, so calling it per row turned a coverage report into an
    # 11 MB log (measured 2026-08-30 on the live 1,029-player universe).
    gsis_by_espn = base.gsis_by_espn(conn)

    acc: dict[str, list[int]] = {}
    for u in universe:
        pos = canon_position(u["position"]) or str(u["position"])
        team = _norm_team(u["team"])
        espn_id = None if u["espn_id"] is None else str(u["espn_id"])
        if pos == "DST":
            row = by_dst.get(team)
        else:
            row = by_espn.get(espn_id) if espn_id else None
            if row is None and espn_id:
                gsis = gsis_by_espn.get(espn_id)
                row = by_gsis.get(gsis) if gsis else None
        cell = acc.setdefault(pos, [0, 0, 0, 0, 0])
        cell[0] += 1
        if row is None:
            cell[4] += 1                     # no priceable house line at all
        elif row.has_market_band:
            cell[1] += 1
        elif row.rank is not None:
            cell[2] += 1
        else:
            cell[3] += 1
    return tuple(
        CoverageRow(position=pos, total=c[0], market_band=c[1],
                    market_rank_only=c[2], positional_prior=c[3], no_house_line=c[4])
        for pos, c in sorted(acc.items())
    )


def format_coverage(coverage: Sequence[CoverageRow]) -> str:
    """A legible coverage table (Rule 6 — a coverage number the operator cannot
    see is a coverage number he cannot challenge)."""
    head = (f"{'pos':<5}{'total':>7}{'per-player band':>17}{'rank only':>11}"
            f"{'prior':>8}{'unpriced':>10}")
    lines = [head, "-" * len(head)]
    tot = [0, 0, 0, 0, 0]
    for row in coverage:
        tot[0] += row.total
        tot[1] += row.market_band
        tot[2] += row.market_rank_only
        tot[3] += row.positional_prior
        tot[4] += row.no_house_line
        lines.append(
            f"{row.position:<5}{row.total:>7}{row.market_band:>10} "
            f"({row.market_band_share:5.1%}){row.market_rank_only:>11}"
            f"{row.positional_prior:>8}{row.no_house_line:>10}"
        )
    share = tot[1] / tot[0] if tot[0] else 0.0
    lines.append("-" * len(head))
    lines.append(
        f"{'ALL':<5}{tot[0]:>7}{tot[1]:>10} ({share:5.1%}){tot[2]:>11}"
        f"{tot[3]:>8}{tot[4]:>10}"
    )
    lines.append(
        "  per-player band = the market's own best/worst for this player, converted "
        "through the house rank-to-points curve"
    )
    lines.append(
        "  rank only       = a market row WAS found for him but the conversion was "
        "refused (units, or a rank past the curve's depth) -> labelled positional prior"
    )
    lines.append(
        "  prior           = no market row could be JOINED to him -- absent from the "
        "board, or on it with no usable id and no unique name match, or ranked there "
        "at a different position -> labelled positional prior. Each row's own reasons "
        "say which; this column deliberately does not claim 'not on the board'"
    )
    lines.append(
        "  unpriced        = no priceable projection line, so no floor/ceiling of any "
        "kind (a PROJECTION-coverage gap, not a dispersion one)"
    )
    return "\n".join(lines)


def _staleness_banners(
    conn, *, priced, market: MarketBoard, as_of: str, season: int, today,
) -> list[str]:
    """A stale board is Rule-1-invisible: a July ECR scrape pricing an August
    decision carries a perfectly valid ``knowable_as_of``. Same shape as
    ``core/marginal.py``'s banner, and measured off the OLDEST vintage on the
    board for the same reason (one refreshed row must not silence it)."""
    out: list[str] = []
    cutoff = normalize_as_of(as_of)

    pulled = sorted({d for pl in priced.values() for d in pl.line.retrieved_as_of})
    if pulled:
        gap = (cutoff - normalize_as_of(pulled[0])).days
        out.append(
            f"projections: newest pull {pulled[-1]}"
            + (f" (oldest row on this board: {pulled[0]})" if len(pulled) > 1 else "")
        )
        if gap > STALE_BANNER_DAYS:
            out.append(
                f"  WARNING: some projections on this board are {gap} days old — the "
                "levels these floors and ceilings hang off may predate injuries, "
                "depth-chart moves and trades. Run `ziggurat ingest run`."
            )
    else:
        out.append("projections: NONE readable at this as-of")

    if market.scrape_date is None:
        out.append("market ECR: NO scrape readable at this as-of")
    else:
        gap = (cutoff - normalize_as_of(market.scrape_date)).days
        out.append(
            f"market ECR: '{market.ecr_type}' scrape {market.scrape_date} "
            f"({_plural(gap, 'day')} before {as_of}), "
            f"{len(market.rows)} rows"
        )
        if gap > STALE_BANNER_DAYS:
            out.append(
                f"  WARNING: the expert-consensus board is {gap} days old. FantasyPros "
                "serves the CURRENT scrape only, so a missed pull is gone — check "
                "`ziggurat ingest status` for adp_rankings."
            )

    if today is not None:
        watched = {"projections", "adp_rankings"}
        for s in refresh.source_freshness(conn, season=season, today=today):
            if s["source"] in watched and s["verdict"] not in refresh.QUIET_VERDICTS:
                age = "never pulled" if s["age_days"] is None else f"{s['age_days']}d old"
                out.append(
                    f"  ingest says {s['source']}: {s['verdict']} ({age})"
                    + ("  [this source cannot be re-pulled — a missed day is gone]"
                       if s["perishable"] else "")
                )
    return out



# ------------------------------------------------------------ standalone sigma

def weekly_sigma(
    position: str,
    *,
    mu: float,
    rank: int | None = None,
    tier_model: TierSigmaModel = DEFAULT_TIER_SIGMA,
    variance: VarianceModel = DEFAULT_VARIANCE,
) -> SigmaEstimate:
    """Per-week house-point sigma for one player — the sigma a win-probability
    objective needs, with its provenance attached.

    Thin, deliberate delegation so a caller who has only a position and a
    projection (no board, no DB) gets the same number and the same reason string
    the full board would give. ``rank`` is the within-position board rank; supply
    it to get the direct tier measurement.
    """
    return tier_model.sigma(position, mu=mu, rank=rank, variance=variance)


def season_sigma(
    *,
    weekly: float,
    weeks: int,
    market_sigma: float | None = None,
) -> float:
    """Season-total sigma from its two parts, in quadrature.

    ``Var(season) = market_sigma^2 + weeks * weekly^2`` — uncertainty about the
    RATE plus week-to-week noise AROUND it. Both independence assumptions are
    assumptions, not measurements, and every reason string that quotes this number
    says so. ``market_sigma`` is None when no market band could be priced, in
    which case the result is the noise term only and is NARROWER than the truth.
    """
    if weeks < 0:
        raise ValueError("weeks must be non-negative")
    return math.sqrt((market_sigma or 0.0) ** 2 + weeks * weekly * weekly)
