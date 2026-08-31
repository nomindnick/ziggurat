"""Availability / durability — how much of the season a player is actually there for.

WHAT THIS ANSWERS, and why it did not exist before
--------------------------------------------------
Every points number in this system is a HEALTHY-PLAYER number. ``valuation.py``
re-scores a projection week by week and sums it; ``draft/simulator.py`` seats one
lineup off that season sum and grades it. Nothing in that chain knows that an RB
plays about 13 of his team's 16 fantasy-season games, that a QB1 who starts the
opener on the shelf misses half a year, or that the bench exists because starters
disappear. So a 2-RB roster reads as fine, a handcuff prices at ~0, and the model
is systematically most confident about exactly the players it should be least
confident about.

This module measures the missing quantity from 2021-2025 nflverse history that is
already in the database, and hands the grader (``ziggurat/draft/grader.py``) a
per-week availability probability, a distribution over games played, and a
seeded episode sampler.

THE SHAPING MEASUREMENT: presence-vs-schedule, not the injury report
--------------------------------------------------------------------
``get_injuries`` is the obvious source and it is the wrong one. Measured on this
database over the cohort defined below (2022-2025 outcomes, 3,659 missed
player-weeks):

    of the weeks a starter-calibre player did NOT play,
    the injuries table carries an "Out"/"Doubtful" designation for   21.3%
    ...carries ANY row at all for                                    31.6%
    ...carries nothing for                                           68.4%

    per-season mean games missed, RB:  presence 3.93   injuries-only 0.69
                                  WR:  presence 3.64   injuries-only 0.90
                                  QB:  presence 4.76   injuries-only 0.91

So an injury-report estimate is **70-86% low, by position**. It is not the 2025
``date_modified`` regression recorded in CLAUDE.md item 3.3 — the gap is flat
across 2022-2025. It is structural: the weekly injury report lists players on the
ACTIVE roster carrying a designation. A player who goes on IR stops appearing on
it entirely, which is precisely the absence that costs the most.

The reverse check says the injury report is PRECISE where it speaks: of 779
cohort player-weeks it called Out or Doubtful, **99.9%** really were absent.
Precise and almost empty. So this module measures absence as **"his team played
and he did not appear"** — the union of a ``weekly_stats`` row and a
``snap_counts`` row with at least one snap, differenced against his team's REG
schedule.

THE SEASON HE MISSED ENTIRELY, which is the same measurement's hardest case
---------------------------------------------------------------------------
"He did not appear" has a degenerate case that the first cut of this module got
exactly backwards, and it is worth stating on its own because it inverts the sign
of the strongest signal the model has. If a player's record is assembled from the
seasons he APPEARED in, then a season he missed in full leaves BOTH the numerator
and the denominator, and the most fragile players on the board come out as the
most durable. Measured, before the fix: a kicker who spent all of 2025 on a
roster without kicking read 0 of 67 games missed, 0.47x, the FIFTH most durable
of 250 priced board rows; a receiver who lost 2022 to an ACL and 2023 to an
Achilles read 3 of 51, 0.74x — *better* than an average WR.

So a zero-appearance season is charged as a full slate of missed games under two
rules, and only those two (``player_seasons`` and ``season_panel`` carry the
detail):

    1. ROSTER EVIDENCE — the weekly injury report names him, with a club, that
       season. Clubs file it for players on the active roster, so it answers the
       one question it answers reliably: was he in the league. It is the only
       evidence source used, because it is the only one spanning 2021-2025
       uniformly; ``depth_chart_slots`` was measured and rejected (2025-2026 only,
       and it would have found 455 absent players in 2025 against 47 from the
       injury report — a season-shaped artefact, not a trend).
    2. BRACKETED — he appeared in an earlier season and in a later one inside the
       window and played nothing in between. This rule exists because rule 1
       fails exactly where it hurts: a player on IR drops off the injury report
       altogether, so the two-torn-ligament receiver above has no injury row in
       either lost season.

Rule 2 never reaches back before his first appearance or forward past his last:
outside that span "on a roster and never played" and "not in the NFL" are the
same empty row, and charging a rookie for the years he was in college would be
this module's own worst failure inverted. Rule 1 has no such restriction and does
not need one — a named club on a filed injury report is a roster spot whenever it
happened, and a college player has never been on one.

WHAT THAT STILL CANNOT SEE, said out loud rather than left in a caveat
----------------------------------------------------------------------
**Every rate in ``DEFAULT_DURABILITY`` is a LOWER bound.** 56 cohort-eligible
outcome seasons (WR 20, RB 18, K 8, QB 6, TE 4) are seasons a previous-year
starter simply vanishes from — no game, and nothing here proving a roster spot —
and they are not in the cohort. Charging all 56 a full missed season gives the
upper bound: QB 0.301, RB 0.275, WR 0.248, TE 0.166, K 0.228 against the shipped
0.267 / 0.223 / 0.210 / 0.141 / 0.177. The truth is inside that band, this
database cannot narrow it, and ``DurabilityPrior.describe`` prints both ends on
every row rather than showing the operator the low one alone.

WHAT THE DEFINITION INCLUDES, said out loud because it is not only injury
-------------------------------------------------------------------------
"Did not appear" is availability in the sense a fantasy roster experiences it:
the slot produced nothing. It therefore also counts healthy scratches, benchings,
demotions, holdouts, suspensions and being cut. That is the RIGHT quantity for a
draft grader (a benched QB scores no more than an injured one) and the WRONG
label to put on it, so nothing here calls it an injury rate. It does mean **QB is
the most contaminated position** — a starter replaced for performance dresses as
the backup, takes no snaps, and reads here as absent.

That same contamination is why the per-player adjustment carries a STARTING-ROLE
floor (``DurabilityPrior.min_role_weeks``) and not only a games floor: a backup's
did-not-dress weeks are a fact about his role, and before the floor existed ten
one-season players on the live 2026 board priced 0.8 to 3.8 expected games BELOW
a rookie with no record at all. Below the floor the row is priced as an AVERAGE
player and says so — and if his record contains a season he missed in full, the
reasons name it anyway, unpriced, rather than letting a guard swallow the one
fact the operator most needs.

THE COHORT, and why it is gated on the PRIOR season
----------------------------------------------------
Measuring "games missed" over everyone who ever appeared is meaningless: only one
QB plays per team per game, so a third-string QB "misses" 15 games without ever
being unavailable. The cohort is therefore **players who held a starting role the
PREVIOUS season** — at least 8 weeks as one of their club's top-k by offensive
snaps at their position (k = QB 1, RB 2, WR 3, TE 1, K 1). That gate uses only
information a drafter has before the season starts, so it cannot leak the outcome
it selects on, and it is the population the draft board is actually drawn from.
n = 1,017 player-seasons (2022-2025 outcomes on 2021-2024 histories).

THE MODEL: a duration-dependent two-state chain, stepped on TEAM GAMES
-----------------------------------------------------------------------
Independent weekly coin flips are wrong in a way that matters. Measured, pooled:

    P(back for the next game | out 1 game)  = 0.319  (n=960)
    P(back | out 2)  0.260 (n=615)   P(out 3)  0.224 (n=419)
    P(out 4)  0.186 (n=306)          P(out 5)  0.144 (n=230)
    P(out 6+) 0.100 (n=858)

Absence is an EPISODE with a decaying return hazard (mean block 3.51 games),
so the chain carries "how many games he has already missed" as state. The season
also opens with a distinct population: a player absent for his team's opener
misses **9.48 games on average** and returns for game 2 only 17.4% of the time —
nothing like an in-season tweak. Opening absentees are therefore seeded four
games deep into the return ladder, whose hazard (0.186) is the rung nearest what
the opener cohort actually exhibits (rung 5, 0.144, is twice as far).

Two things the chain does NOT do, deliberately. It does not carry a rising weekly
onset hazard: measured onset is roughly flat (5.4% into a club's second game,
7.6-8.1% around games 5-9, 5-7.3% through games 12-16). The reason late-season
availability is worse is that absences ACCUMULATE while the hazard does not.
And it stops at the fantasy season: the onset into a club's SEVENTEENTH game
spikes to 13.6% because clubs rest starters, which is not a durability fact and
never lands in a scoring week (our H2H season is weeks 1-14, playoffs 15-17), so
the whole prior is calibrated on a player's **first 16 team games** —
``PlayerSeason.horizon`` takes the first n, and taking the last n instead would
import exactly that effect (measured: QB +6.6% relative, TE +9.5%).

THE PER-PLAYER ADJUSTMENT, and how weak it honestly is
-------------------------------------------------------
Last season's missed-game rate does predict this season's, and it is not noise:

    prior 0 missed  -> 14.4% of games missed      prior 3-5 -> 21.1%
    prior 1-2       -> 15.9%                      prior 6+  -> 27.2%

But the year-over-year correlation is only **r = 0.23** (0.27 using every prior
season), and the whole adjustment is worth **5.5% of mean squared error** against
a position-only baseline. The shrinkage sweep is FLAT around its argmin — k=60
(0.065046), k=50 (0.065166), k=80 (0.065067) — so k=60 is shipped on a 0.02%
margin, not because it is a sharp optimum. **The sweep is IN-SAMPLE**: the
position rate, the prior-history mean and the mse are all read off the same 1,017
cohort rows, and no season is held out. (An earlier revision of this file
described k=60 as "MSE-optimal ... on 1,000 held-out player-seasons". It was
neither the argmin at the time nor held out; ``fit_player_adjustment`` is how the
claim gets checked instead of repeated.) At k=60 a full 17-game season of a
player's own record earns 22% of the weight. Real, small, and stated in every
reason rather than dressed up.

One correction that shrinkage alone gets wrong, and it is a big one. The cohort's
own PRIOR-history miss rate (13.9% pooled) is far below its OUTCOME rate (20.7%),
because the cohort gate selects players who were healthy enough to start 8 games
last year. Shrinking toward the prior-history mean and reading the result as an
absolute rate biases every player optimistic. The adjustment is therefore a
**multiplier** — a player's shrunk rate relative to the cohort's own prior-history
mean — applied to the OUTCOME rate. That is unbiased by construction: measured
mean predicted **0.2065** against actual **0.2072**, where the discarded absolute
form lands ~20% low on every position.

WHAT A PLAYER WITH NO HISTORY GETS, and why that is the load-bearing default
-----------------------------------------------------------------------------
The position prior, exactly — never a clean bill of health. A rookie's multiplier
is 1.00, which places him BELOW every veteran with a proven-durable record and
ABOVE every veteran with a bad one, and the basis line says ``POSITION_PRIOR``
with the sample size that produced it. Silence here would be the worst possible
failure: it would systematically favour the players we know least about, which is
the exact asymmetry that makes an unmeasured model dangerous rather than merely
imprecise. A test pins it.

STANDING RULES
--------------
Rule 1 — every accessor takes keyword-only ``as_of`` with no default and threads
``view`` into the underlying accessor. ``load_player_histories`` binds
``base.latest_truth`` explicitly, with the reason stated at the call site.
Rule 2 — nothing here is a scoring number and nothing here imports a scoring
constant. A durability rate is a dispersion/participation prior; it never enters
``scoring.py``, exactly as ``lineup_support.DEFAULT_VARIANCE`` does not.
Rule 6 — every rate ships as a LABELLED HYPOTHESIS carrying its cohort size and
source in the reason text; the injury-vs-presence gap, the lower-bound band, a
clip that bound, a season missed in full and a player we could not look up are
all disclosed rather than smoothed over. The standing requirement is that the
printed numbers RECONCILE: a reader who recomputes from the record, k and the
weight must land on the multiplier the row shipped, or be told in the same
breath why not.
Rule 8 — this is permanent ``core/``; it imports nothing from ``ziggurat/draft/``
and ``draft/grader.py`` imports FROM here.
"""

import dataclasses
import functools
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from ziggurat.core.valuation import canon_position
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import base, injuries, schedules, snap_counts, weekly_stats

# --------------------------------------------------------------------- constants

#: How many of a player's own team games the fantasy season can actually use.
#: A club plays 17 games across 18 calendar weeks; this league's H2H season is
#: weeks 1-14 and its playoffs are weeks 15-17, so the last team game (week 18)
#: is never scored. It is also the one week clubs rest starters — measured onset
#: 13.6% against ~7% for every other week — so including it would import a
#: resting-starters effect into a durability prior. The whole prior is calibrated
#: on a player's FIRST 16 team games.
FANTASY_HORIZON_GAMES = 16

#: Absence durations are tracked 1..6 and then pooled; the measured return hazard
#: has flattened by then (0.155 at 5, 0.129 at 6+) and the per-duration samples
#: past 6 are thin.
MAX_ABSENCE_DURATION = 6

#: Where the sample basis came from. Printed verbatim in the reasons.
BASIS_PLAYER = "PLAYER_HISTORY"
BASIS_POSITION = "POSITION_PRIOR"
BASIS_ALWAYS = "ALWAYS_AVAILABLE"

#: WHY a row fell back to the position prior. Five different facts about the
#: world used to share one sentence, and three of them were false statements
#: about the player: "no prior season in the measured window" was printed for a
#: player we never looked up, and "below the 8-game floor" was printed for an
#: 85-game record whose POSITION had no measured prior mean. A novice operator
#: (Rule 6) cannot tell a data-quality hole from a measurement about the player,
#: so each cause now carries its own words.
NO_ADJUSTMENT_NO_ID = "NO_ID"                 # we never had an id to look up
NO_ADJUSTMENT_NO_RECORD = "NO_RECORD"         # looked up; nothing in the window
NO_ADJUSTMENT_THIN = "BELOW_GAME_FLOOR"       # record too short to lean on
NO_ADJUSTMENT_ROLE = "BELOW_ROLE_FLOOR"       # never held the job (see below)
NO_ADJUSTMENT_NO_MEAN = "NO_POSITION_MEAN"    # the PRIOR is missing its centre

#: How a ``PlayerSeason`` knows the player was in the league that year.
#: ``APPEARANCES`` is the ordinary case. The other two are seasons he did not
#: appear in AT ALL, which the first cut of this module silently deleted from
#: both halves of his record — see ``player_seasons`` for the rule and
#: ``_bracketed_absences`` for the multi-season half of it.
SEASON_APPEARED = "APPEARANCES"
SEASON_ROSTER_EVIDENCE = "ROSTER_EVIDENCE"
SEASON_BRACKETED = "BRACKETED_ABSENCE"

#: Positions the cohort gate ranks, and how many of a club's players at that
#: position count as holding a starting role in a given week. Mirrors the league's
#: own starting lineup (QB/RB2/WR2/TE/FLEX) with one extra WR for the flex.
ROLE_DEPTH: Mapping[str, int] = MappingProxyType(
    {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "K": 1}
)

#: Minimum weeks in that role, in the PREVIOUS season, to enter the cohort.
COHORT_MIN_ROLE_WEEKS = 8


class DurabilityError(ValueError):
    """A durability question this module refuses to answer by guessing."""


class HistoryCollapse(DurabilityError):
    """A history or slate read came back too small to be a real read.

    The same shape as ``league.sync.SnapshotCollapse`` / ``espn_ranks.BoardCollapse``
    / ``players.CrosswalkCollapse``, and here for the same reason (CLAUDE.md's
    standing lesson): an EMPTY read of this module's inputs is not an alarm
    anywhere — it prices the whole board at 1.00x with a confident
    ``POSITION_PRIOR`` basis on every row and no aggregate warning at all. The
    two ways to produce one are both real and both silent: the 2021-2025 backfill
    has not been run on this box, or the slate was read under the safe-default
    ``historical`` view at a past ``as_of`` (every schedules row was retrieved in
    2026, so it is gated out).
    """


# ------------------------------------------------------------------- the prior


@dataclass(frozen=True)
class DurabilityPrior:
    """Per-position availability primitives — a LABELLED HYPOTHESIS.

    Every number was measured by ``measure_durability`` against this database's
    2021-2025 nflverse history (see the module docstring for the cohort and the
    presence-vs-injury-report decision). None of it is fitted to 2026, and none
    of it is a scoring number (Rule 2).

    ``season_miss_rate``   P(a cohort player does not play), per team game, over
                           his first ``horizon_games`` games. THE headline number.
    ``opening_absent``     P(absent for his team's first game). Deliberately its
                           own parameter: that cohort misses 9.48 games on
                           average, not ~3.5.
    ``return_hazard``      P(back for the next game | absent d games so far),
                           indexed d-1 and pooled across positions (per-position
                           samples are too thin past d=3).
    ``opening_duration``   Which rung of ``return_hazard`` an opening absentee
                           starts on. Not free: it is the rung NEAREST the
                           opener cohort's own measured P(back for game 2),
                           which is stored beside it as ``opening_return``.
    ``prior_history_rate`` The cohort's mean miss rate in its OWN prior seasons.
                           NOT the same as ``season_miss_rate`` and that gap is
                           load-bearing — see ``durability_multiplier``.
    The per-game ONSET hazard is deliberately NOT stored: it is solved so the
    chain reproduces ``season_miss_rate`` exactly (``solve_onset``), which keeps
    the two from ever drifting apart.
    """

    season_miss_rate: Mapping[str, float]
    opening_absent: Mapping[str, float]
    return_hazard: tuple[float, ...]
    opening_duration: int
    opening_return: float
    prior_history_rate: Mapping[str, float]
    shrink_k: float
    min_prior_games: int
    min_role_weeks: int
    multiplier_clip: tuple[float, float]
    never_absent: frozenset[str]
    cohort_n: Mapping[str, int]
    horizon_games: int
    yoy_correlation: float
    prediction_correlation: float
    mse_skill_vs_position: float
    injury_report_coverage: Mapping[str, float]
    mean_absence_block: float
    #: The SAME rate if every cohort-eligible season that vanished from the record
    #: entirely were charged as a full missed season. ``season_miss_rate`` is a
    #: LOWER bound and this is the upper one; the truth is between them and this
    #: module cannot narrow the gap with the data in this database. Printed in
    #: ``describe`` so the operator is never shown the lower bound alone.
    season_miss_rate_upper: Mapping[str, float]
    #: How many such seasons there were (the width of that band's cause).
    unprovable_cohort_seasons: int
    cohort: str
    label: str
    source: str

    def positions(self) -> tuple[str, ...]:
        return tuple(sorted(self.season_miss_rate))

    def miss_rate(self, position: str) -> float:
        """The position's per-game miss rate, or a refusal.

        Refusing beats defaulting: a position this prior never measured would
        otherwise silently read as the most durable thing on the board.
        """
        if position in self.never_absent:
            return 0.0
        try:
            return self.season_miss_rate[position]
        except KeyError:
            raise DurabilityError(
                f"no durability prior for position {position!r} "
                f"(measured: {', '.join(self.positions())}; "
                f"never-absent: {', '.join(sorted(self.never_absent))})"
            ) from None

    def describe(self, position: str) -> str:
        """The reason-string form of the position prior (Rule 6)."""
        if position in self.never_absent:
            return (
                f"A {position} is a team unit, not a player: it is on the field "
                f"every week its club plays, so this model never discounts it for "
                f"availability (its bye is already worth 0 points)."
            )
        rate = self.miss_rate(position)
        n = self.cohort_n.get(position, 0)
        upper = self.season_miss_rate_upper.get(position)
        band = ""
        if upper is not None and upper > rate:
            band = (
                f" That is a LOWER bound: {self.unprovable_cohort_seasons} "
                f"cohort-eligible seasons vanished from the record altogether "
                f"(no game, and nothing proving he was on a roster), and charging "
                f"every one of them a full missed season would put this at "
                f"{100 * upper:.1f}% — {upper * self.horizon_games:.1f} of "
                f"{self.horizon_games}. The truth is inside that band."
            )
        return (
            f"Position prior: {_article(position)} {position} who held a starting "
            f"role the previous "
            f"season missed {100 * rate:.1f}% of his club's first "
            f"{self.horizon_games} games — {rate * self.horizon_games:.1f} of "
            f"{self.horizon_games}.{band} {self.label}; {self.cohort} "
            f"(n={n} {position} player-seasons); {self.source}."
        )

    def method_note(self, position: str) -> str:
        """The mandatory disclosure about what "missed" was measured FROM."""
        cov = self.injury_report_coverage.get(position)
        tail = ""
        if cov is not None:
            tail = (
                f" The weekly injury report names a designation for only "
                f"{100 * cov:.0f}% of the weeks {_article(position)} {position} actually missed "
                f"(it drops players once they land on IR), so an injury-report "
                f"estimate reads far too durable."
            )
        return (
            "Measured as GAMES NOT PLAYED for any reason — injury, IR, healthy "
            "scratch, benching, suspension or being cut — by differencing "
            "appearances against his club's schedule, not from the injury report."
            + tail
        )

    def block_note(self) -> str:
        return (
            f"Absences arrive in BLOCKS, not as independent weekly coin flips: "
            f"measured P(back next game) falls from "
            f"{100 * self.return_hazard[0]:.0f}% after one missed game to "
            f"{100 * self.return_hazard[-1]:.0f}% once he is "
            f"{len(self.return_hazard)}+ games out (mean block "
            f"{self.mean_absence_block:.1f} games)."
        )


#: The shipped prior. Reproduced by ``measure_durability`` against the live
#: 2021-2025 backfill on 2026-08-30 — ``ziggurat/core/availability.py`` is the
#: only place these numbers live, and ``measure_durability`` is how they are
#: re-derived rather than trusted.
DEFAULT_DURABILITY = DurabilityPrior(
    # Per team-game P(does not play), over a player's first 16 games. QB is the
    # most contaminated of these (a benched starter reads identically to an
    # injured one); K is bimodal (most play every game, the rest lose the job).
    # These rose against the module's first cut — RB .2099 -> .2226, K .1408 ->
    # .1766 — because a season a cohort player missed IN FULL used to be deleted
    # from the measurement instead of counted. See ``player_seasons``.
    season_miss_rate=MappingProxyType(
        {"QB": 0.2671, "RB": 0.2226, "WR": 0.2097, "TE": 0.1405, "K": 0.1766}
    ),
    # P(absent for his club's OPENER). A different animal: that cohort misses
    # 9.48 games on average against a ~3.5 season mean.
    opening_absent=MappingProxyType(
        {"QB": 0.2097, "RB": 0.1406, "WR": 0.1418, "TE": 0.0620, "K": 0.1583}
    ),
    # P(back for the next game | already absent d games), d = 1..6+, pooled.
    return_hazard=(0.3187, 0.2602, 0.2243, 0.1863, 0.1435, 0.1002),
    # Which rung an opening absentee starts on. Not a free parameter: the opener
    # cohort's MEASURED P(back for game 2) is ``opening_return`` below, and rung 4
    # (0.1863) is the closest rung to it — rung 5 (0.1435) is twice as far.
    opening_duration=4,
    opening_return=0.1736,
    # The cohort's own prior-season miss rate. Systematically BELOW the outcome
    # rate above because the cohort gate selects players who started 8+ games
    # last year (pooled 0.1392 against 0.2072) — the whole reason the player
    # adjustment is a multiplier rather than a shrunk absolute rate.
    prior_history_rate=MappingProxyType(
        {"QB": 0.1919, "RB": 0.1593, "WR": 0.1428, "TE": 0.1023, "K": 0.0735}
    ),
    # Fitted IN-SAMPLE by ``fit_player_adjustment`` over k in {10..250} on the
    # 1,017-row cohort — NOT held out: the same rows supply the position rate, the
    # prior-history mean and the mse the sweep is read off, and no season is kept
    # back. k=60 is the argmin (mse 0.065046) and the curve is flat around it
    # (k=50 0.065166, k=80 0.065067), so it is the shipped value on a 0.02%
    # margin, not a sharp optimum. 60 game-equivalents means a full 17-game season
    # of a player's own record earns 17/(17+60) = 22% of the weight.
    shrink_k=60.0,
    # Below this many CLUB GAMES ON RECORD the adjustment is not applied at all
    # and the row says POSITION_PRIOR. Read against a full set of completed
    # seasons this floor never fires (one season is already 17 club games); what
    # it guards is a MID-SEASON read, where the denominator is only the games
    # played so far and a week-3 read would otherwise quote a three-game sample
    # as "his own record". The floor that does the work on a draft-day board is
    # ``min_role_weeks`` below.
    min_prior_games=8,
    # The floor that actually fires on a completed-season record, and the one the
    # per-player multiplier NEEDS: at least this many weeks of the record spent
    # as one of his club's top-k at his position. The multiplier was fitted on a
    # cohort gated exactly this way (8+ role weeks), so applying it to a player
    # who never held the job extrapolates off the population it was measured on —
    # and does so in the worst direction. Measured on the live 2026 board before
    # this floor existed: ten one-season players priced 0.8 to 3.8 expected games
    # BELOW a rookie with no record at all, because a backup's did-not-dress
    # weeks read identically to a starter's injuries. That is a fact about his
    # ROLE, not his durability. Same number as COHORT_MIN_ROLE_WEEKS, and it is
    # the same gate.
    min_role_weeks=COHORT_MIN_ROLE_WEEKS,
    # A GUARD, not a fitted parameter. Over the fitted cohort the raw multiplier
    # ranges [0.47, 2.78] (p5 0.60, p95 1.68), so the top of this range now binds
    # on the most extreme cohort rows as well as on the board; over the live 2026
    # board it binds on 20 of 770 priced rows, because a five-season
    # perfect-attendance record reaches 0.41 and a nearly all-absent one runs off
    # past 3. The clip refuses to extrapolate past the evidence rather than
    # letting one extreme record dominate a draft board — and every row it binds
    # on SAYS SO in its reasons, because otherwise the printed record, k and
    # weight recompute to a different number than the row shipped.
    multiplier_clip=(0.45, 2.50),
    # A team defense plays whenever its club does.
    never_absent=frozenset({"DST"}),
    cohort_n=MappingProxyType({"QB": 124, "RB": 249, "WR": 395, "TE": 129, "K": 120}),
    horizon_games=FANTASY_HORIZON_GAMES,
    # r between last season's missed-game rate and this season's. The signal is
    # real (prior 0 missed -> 14.4% of games missed; prior 6+ -> 27.2%) and weak.
    yoy_correlation=0.2254,
    # r between the SHRUNK multi-season prediction and the outcome, at k=60.
    prediction_correlation=0.2669,
    mse_skill_vs_position=0.0545,
    # Fraction of ACTUALLY-missed weeks for which `injuries` carries an
    # Out/Doubtful designation. The rest are invisible to that table. Measured
    # over appearance-derived seasons only — a season DETECTED through the injury
    # report cannot be used to score the injury report.
    injury_report_coverage=MappingProxyType(
        {"QB": 0.1915, "RB": 0.1748, "WR": 0.2455, "TE": 0.3333, "K": 0.1021}
    ),
    mean_absence_block=3.507,
    # The other end of the band. 56 cohort-eligible outcome seasons (WR 20, RB 18,
    # K 8, QB 6, TE 4) are seasons a Y-1 starter simply vanishes from: no game,
    # and nothing in this database proving he was on a roster. They are not in the
    # cohort, so every rate above is a LOWER bound. Charging all 56 a full missed
    # season is the upper one. This is a real, un-narrowable limitation of the
    # available data, so it is carried in the prior and printed by ``describe``
    # rather than living in a caveat nobody reads.
    season_miss_rate_upper=MappingProxyType(
        {"QB": 0.3010, "RB": 0.2750, "WR": 0.2477, "TE": 0.1664, "K": 0.2280}
    ),
    unprovable_cohort_seasons=56,
    cohort=(
        "cohort: players who spent >=8 weeks of the PREVIOUS season as one of "
        "their club's top-k by offensive snaps at their position "
        "(QB1/RB2/WR3/TE1/K1); 1,017 player-seasons, 2022-2025 outcomes on "
        "2021-2024 histories"
    ),
    label=(
        "hypothesis: nflverse 2021-2025 presence-vs-schedule (appearances "
        "differenced against the club's REG schedule, plus seasons missed in "
        "full where a roster spot is provable), not fitted to 2026"
    ),
    source="ziggurat.core.availability.measure_durability, run 2026-08-30",
)


# ------------------------------------------------------------------ the chain
#
# State is (available) or (absent, d) with d = games missed so far in the current
# episode, capped at MAX_ABSENCE_DURATION. Everything below is an exact forward
# pass — no sampling — so two runs on the same inputs return identical floats.


def _step(state: Sequence[float], onset: float, hazard: Sequence[float]) -> list[float]:
    """One team game. ``state[0]`` is P(available); ``state[d]`` is P(absent, d)."""
    cap = len(state) - 1
    nxt = [0.0] * len(state)
    nxt[0] = state[0] * (1.0 - onset)
    nxt[1] += state[0] * onset
    for d in range(1, cap + 1):
        p = state[d]
        if p == 0.0:
            continue
        back = hazard[min(d, len(hazard)) - 1]
        nxt[0] += p * back
        nxt[min(d + 1, cap)] += p * (1.0 - back)
    return nxt


def _initial_state(opening_absent: float, opening_duration: int, cap: int) -> list[float]:
    state = [0.0] * (cap + 1)
    state[0] = 1.0 - opening_absent
    state[min(max(opening_duration, 1), cap)] = opening_absent
    return state


def availability_path(
    n_games: int, onset: float, opening_absent: float, prior: DurabilityPrior
) -> list[float]:
    """P(available) for each of ``n_games`` consecutive team games."""
    if n_games <= 0:
        raise DurabilityError("availability_path needs at least one team game")
    state = _initial_state(opening_absent, prior.opening_duration, MAX_ABSENCE_DURATION)
    out = [state[0]]
    for _ in range(n_games - 1):
        state = _step(state, onset, prior.return_hazard)
        out.append(state[0])
    return out


def expected_missed(
    n_games: int, onset: float, opening_absent: float, prior: DurabilityPrior
) -> float:
    return float(n_games) - sum(availability_path(n_games, onset, opening_absent, prior))


@functools.lru_cache(maxsize=8192)
def _solve_onset(
    n_games: int,
    target_missed: float,
    opening_absent: float,
    hazard: tuple[float, ...],
    opening_duration: int,
) -> float:
    """Cacheable core of ``solve_onset``.

    The five arguments FULLY determine the chain (``_step`` reads nothing but the
    onset and the hazard; ``_initial_state`` nothing but the opening mass and its
    rung), so the memo cannot serve an answer from a different prior. Keying on
    the hazard TUPLE rather than on the prior object is what makes that true —
    ``id(prior)`` would be reused after a garbage collection and could hand a
    re-measured prior the shipped prior's answer.

    Why it is worth caching at all: every player with no usable history at a
    given position solves the identical problem, and a 1,029-row draft board is
    mostly such players. Measured on this box: pricing the whole board falls from
    0.97 s to 0.17 s, and the answers are bit-identical.
    """
    if target_missed <= 0.0:
        return 0.0

    def missed(onset: float) -> float:
        state = _initial_state(opening_absent, opening_duration, MAX_ABSENCE_DURATION)
        total = 1.0 - state[0]
        for _ in range(n_games - 1):
            state = _step(state, onset, hazard)
            total += 1.0 - state[0]
        return total

    if target_missed <= missed(0.0):
        # The opening-absence mass alone already misses this much; no onset can
        # bring it lower, so use none rather than pretend.
        return 0.0
    lo, hi = 0.0, 0.999
    if missed(hi) <= target_missed:
        # Unreachable even at the maximum hazard (a one-game window, where the
        # onset has no leverage at all). Saturate rather than fake it;
        # ``player_availability`` reports the ACHIEVED rate and says why.
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if missed(mid) < target_missed:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def solve_onset(
    n_games: int, target_missed: float, opening_absent: float, prior: DurabilityPrior
) -> float:
    """The per-game onset hazard that makes the chain miss ``target_missed`` games.

    Solving rather than storing an onset is deliberate: ``season_miss_rate`` is
    the number that was measured and the number a human can check, and a stored
    onset would be a second parameter free to drift out of agreement with it.
    Bisection on a monotone function; 60 halvings of [0, 0.999] is exact to ~1e-18.
    """
    return _solve_onset(
        int(n_games), float(target_missed), float(opening_absent),
        tuple(prior.return_hazard), int(prior.opening_duration),
    )


def games_played_pmf(
    n_games: int, onset: float, opening_absent: float, prior: DurabilityPrior
) -> tuple[float, ...]:
    """Exact distribution over games PLAYED; index g = P(plays exactly g)."""
    cap = MAX_ABSENCE_DURATION
    # dp[missed][state] over the same chain, carrying the running miss count.
    state = _initial_state(opening_absent, prior.opening_duration, cap)
    dp: list[list[float]] = [[0.0] * (cap + 1) for _ in range(n_games + 1)]
    dp[0][0] = state[0]
    if opening_absent > 0.0:
        # Keyed on the probability, not on ``state[0] != 1.0``: a tiny opening
        # mass rounds state[0] to exactly 1.0 in float and would be dropped.
        idx = min(max(prior.opening_duration, 1), cap)
        dp[1][idx] = state[idx]
    for _ in range(n_games - 1):
        nxt = [[0.0] * (cap + 1) for _ in range(n_games + 1)]
        for missed, row in enumerate(dp):
            if not any(row):
                continue
            stepped = _step(row, onset, prior.return_hazard)
            # _step folds "went absent" into index 1 and "stayed absent" into
            # d+1; both are one more missed game, so they advance the counter.
            nxt[missed][0] += stepped[0]
            if missed + 1 <= n_games:
                for d in range(1, cap + 1):
                    nxt[missed + 1][d] += stepped[d]
        dp = nxt
    pmf = [0.0] * (n_games + 1)
    for missed, row in enumerate(dp):
        total = sum(row)
        if total:
            pmf[n_games - missed] += total
    return tuple(pmf)


def steady_state_absent(
    position: str, *, prior: DurabilityPrior = DEFAULT_DURABILITY, multiplier: float = 1.0
) -> float:
    """P(absent) once the season has run long enough to forget its opener.

    The right ``opening_absent`` for a MID-SEASON window. Passing the shipped
    ``opening_absent`` there would price a week-15 read as if the player were
    about to start his season, which is measurably a different (and much better)
    population — see ``DurabilityPrior.opening_absent``.
    """
    if position in prior.never_absent:
        return 0.0
    rate = min(max(prior.miss_rate(position) * multiplier, 0.0), 0.99)
    opening = min(max(prior.opening_absent.get(position, 0.0) * multiplier, 0.0), 0.95)
    onset = solve_onset(prior.horizon_games, rate * prior.horizon_games, opening, prior)
    # Run the chain long past the horizon and read the plateau.
    state = _initial_state(opening, prior.opening_duration, MAX_ABSENCE_DURATION)
    for _ in range(200):
        state = _step(state, onset, prior.return_hazard)
    return 1.0 - state[0]


# -------------------------------------------------------------- player history


@dataclass(frozen=True)
class PlayerHistory:
    """One player's measured participation record, keyed by gsis id.

    ``games`` is his clubs' REG games across the measured seasons; ``missed`` is
    how many he did not appear in. ``seasons`` are the seasons that contributed,
    so a reason line can say *which* years the number came from.

    ``absent_seasons`` are seasons inside ``seasons`` he missed ENTIRELY — the
    single most informative rows in the whole record and the ones the first cut
    of this module deleted (see ``player_seasons``). They are broken out because
    a reason line that does not name them reads as if the record were unbroken.
    ``unknown_seasons`` are gaps this module could NOT price and did not charge;
    normally empty, and never silently so.

    ``played`` and ``role_weeks`` are the sample's SHAPE rather than its rate:
    how many games he actually appeared in, and how many of those weeks he spent
    as one of his club's top-k at his position. ``role_weeks`` gates the
    per-player adjustment (``DurabilityPrior.min_role_weeks``). Both are
    ``None`` on a hand-built history, which means "not measured" — the gate is
    then skipped and the reason line says the record's role coverage is unknown,
    rather than silently passing or silently failing it.
    """

    gsis_id: str
    games: int
    missed: int
    seasons: tuple[int, ...]
    absent_seasons: tuple[int, ...] = ()
    unknown_seasons: tuple[int, ...] = ()
    played: int | None = None
    role_weeks: int | None = None
    window: tuple[int, int] | None = None

    @property
    def miss_rate(self) -> float:
        return self.missed / self.games if self.games else 0.0


@dataclass(frozen=True)
class MultiplierDetail:
    """``durability_multiplier`` with its working shown.

    Exists because the shipped multiplier and the numbers printed beside it were
    allowed to disagree: ``multiplier_clip`` bound silently, so a reason line
    quoting the record, k and the weight let the reader recompute 3.71x for a row
    that actually shipped 2.50x. ``raw`` is the unclipped value and ``clipped``
    says whether the guard bound, so the disclosure can never fall out of step
    with the number again.
    """

    multiplier: float
    raw: float
    basis: str
    code: str | None          # a NO_ADJUSTMENT_* when basis is not BASIS_PLAYER
    clipped: bool
    weight: float             # share of the estimate carried by his own record
    position_mean: float      # the cohort's prior-history mean this is relative to


def _multiplier_detail(
    position: str,
    history: PlayerHistory | None,
    *,
    prior: DurabilityPrior = DEFAULT_DURABILITY,
    identified: bool = True,
) -> MultiplierDetail:
    """The full working behind ``durability_multiplier`` — see that docstring."""
    if position in prior.never_absent:
        return MultiplierDetail(1.0, 1.0, BASIS_ALWAYS, None, False, 0.0, 0.0)
    prior.miss_rate(position)  # refuse an unmeasured position here, not later
    mu = prior.prior_history_rate.get(position) or 0.0

    def fallback(code: str) -> MultiplierDetail:
        return MultiplierDetail(1.0, 1.0, BASIS_POSITION, code, False, 0.0, mu)

    if history is None:
        return fallback(
            NO_ADJUSTMENT_NO_RECORD if identified else NO_ADJUSTMENT_NO_ID
        )
    if history.games < prior.min_prior_games:
        return fallback(NO_ADJUSTMENT_THIN)
    if history.role_weeks is not None and history.role_weeks < prior.min_role_weeks:
        return fallback(NO_ADJUSTMENT_ROLE)
    if not mu:
        # NOT the same fact as a thin sample, and it used to print as one: this
        # is the PRIOR missing its centre, so no relative adjustment can be
        # formed however long his record is. Reachable from this module's own
        # re-measurement path (``measure_durability`` sets 0.0 for a position
        # whose cohort happened to miss nothing).
        return fallback(NO_ADJUSTMENT_NO_MEAN)
    k = prior.shrink_k
    shrunk = (history.missed + k * mu) / (history.games + k)
    raw = shrunk / mu
    lo, hi = prior.multiplier_clip
    mult = min(max(raw, lo), hi)
    return MultiplierDetail(
        multiplier=mult, raw=raw, basis=BASIS_PLAYER, code=None,
        clipped=abs(mult - raw) > 1e-12,
        weight=history.games / (history.games + k),
        position_mean=mu,
    )


def durability_multiplier(
    position: str,
    history: PlayerHistory | None,
    *,
    prior: DurabilityPrior = DEFAULT_DURABILITY,
) -> tuple[float, str]:
    """(multiplier, basis) — 1.00 means "an average player at this position".

    The multiplier form, rather than a shrunk absolute rate, is the fix for a
    measured bias: the cohort's prior-history miss rate (13.9% pooled) sits far
    below its outcome rate (20.7%) because the cohort gate selects players who
    started 8+ games the year before. Shrinking toward the prior-history mean and
    reading the answer as an absolute rate under-predicted every player. Dividing
    by that same mean makes the adjustment relative and the population unbiased —
    ``fit_player_adjustment`` re-derives both numbers.

    Four DIFFERENT facts send a row to ``BASIS_POSITION`` instead, each with its
    own ``NO_ADJUSTMENT_*`` code and its own sentence in the reasons: no record at
    all, a record shorter than ``min_prior_games`` club games, a record with fewer
    than ``min_role_weeks`` weeks in a starting role, and a prior carrying no
    measured mean for the position. ``_multiplier_detail`` returns the code and
    the UNCLIPPED value; this is the two-value form for callers that only want
    the answer.
    """
    d = _multiplier_detail(position, history, prior=prior)
    return d.multiplier, d.basis


# --------------------------------------------------------------- the output row


@dataclass(frozen=True)
class PlayerAvailability:
    """One player's availability over an explicit list of his club's game weeks.

    ``week_available`` is keyed by NFL week and covers exactly ``game_weeks``; a
    bye week is simply not a key, and ``p_available`` returns 0.0 for it (his
    projection for that week is 0 anyway — the point of keeping them distinct is
    that a grader integrating points x availability must not step the illness
    chain on a week his club does not play).
    """

    player_key: str
    position: str
    basis: str
    sample_games: int
    sample_missed: int
    sample_seasons: tuple[int, ...]
    multiplier: float
    miss_rate: float           # ACHIEVED over this window: expected_missed / games
    target_miss_rate: float    # what the prior asked for, per game
    onset: float
    opening_absent: float
    game_weeks: tuple[int, ...]
    week_available: Mapping[int, float]
    games_played_pmf: tuple[float, ...]
    expected_games_played: float
    expected_games_missed: float
    reasons: tuple[str, ...]
    # The row carries the prior it was built from. Without it ``sample_available
    # _weeks`` would have to reach for the module default, and a caller running a
    # re-measured prior would get a sampler walking a DIFFERENT chain from the
    # marginals printed beside it — with nothing anywhere to say so.
    prior: DurabilityPrior = DEFAULT_DURABILITY
    #: WHY the position prior was used, machine-readably (a ``NO_ADJUSTMENT_*``);
    #: ``None`` when his own record WAS used. A consumer reporting coverage needs
    #: to separate "we could not identify him" from "he has no record" — they are
    #: a data-quality hole and a fact about the player, and one message for both
    #: is how a hole gets reported to a novice as a measurement (Rule 6).
    fallback_code: str | None = None
    #: The unclipped multiplier, and whether ``multiplier_clip`` bound.
    raw_multiplier: float = 1.0
    multiplier_clipped: bool = False
    #: The (first, last) seasons the history was read over, when the caller knew
    #: it. Reason lines name THIS window rather than a hardcoded literal.
    history_window: tuple[int, int] | None = None
    #: Seasons inside the record he missed entirely, and gaps that could not be
    #: priced — surfaced so a consumer can flag them without re-parsing prose.
    absent_seasons: tuple[int, ...] = ()
    unknown_seasons: tuple[int, ...] = ()

    def p_available(self, week: int) -> float:
        """P(he plays in ``week``); 0.0 on a bye or any week outside his slate."""
        return self.week_available.get(int(week), 0.0)

    def p_plays_at_least(self, games: int) -> float:
        g = max(int(games), 0)
        if g >= len(self.games_played_pmf):
            return 0.0
        return sum(self.games_played_pmf[g:])


def player_availability(
    player_key: str,
    position: str,
    game_weeks: Sequence[int],
    *,
    prior: DurabilityPrior = DEFAULT_DURABILITY,
    history: PlayerHistory | None = None,
    opening_absent: float | None = None,
    identified: bool = True,
    history_window: tuple[int, int] | None = None,
) -> PlayerAvailability:
    """Price one player's availability over ``game_weeks``.

    ``game_weeks`` are the NFL weeks his CLUB plays inside the window being
    graded, in order, byes already removed. ``opening_absent`` overrides the
    prior's season-opener rate — pass 0.0 or 1.0 when a live designation is known,
    and pass one explicitly for a mid-season window, where "this is his first game
    of the year" is not what the first entry means.

    ``identified`` is the caller saying whether he was ever LOOKED UP. It exists
    because ``history=None`` has two causes and they are not the same statement:
    "we searched the record and he is not in it" is a fact about the player, and
    "we never resolved him to a player id" is a fact about our crosswalk (42 of
    the 1,029 rows on the live 2026 board). Pass ``identified=False`` for the
    second so the reasons stop claiming the first.

    ``history_window`` is the (first, last) seasons ``history`` was read over, so
    a reason line can name the window that was actually read instead of a
    hardcoded literal that goes stale the moment a caller changes it.
    """
    weeks = tuple(int(w) for w in game_weeks)
    if not weeks:
        raise DurabilityError(
            f"{player_key}: no game weeks given — availability over an empty "
            "slate is not a question this module will answer by guessing"
        )
    if sorted(weeks) != list(weeks) or len(set(weeks)) != len(weeks):
        raise DurabilityError(
            f"{player_key}: game_weeks must be strictly increasing; got {weeks}"
        )
    pos = str(position).strip().upper()
    n = len(weeks)

    if pos in prior.never_absent:
        ones = MappingProxyType({w: 1.0 for w in weeks})
        pmf = tuple([0.0] * n + [1.0])
        return PlayerAvailability(
            player_key=player_key, position=pos, basis=BASIS_ALWAYS,
            sample_games=0, sample_missed=0, sample_seasons=(),
            multiplier=1.0, miss_rate=0.0, target_miss_rate=0.0,
            onset=0.0, opening_absent=0.0,
            game_weeks=weeks, week_available=ones, games_played_pmf=pmf,
            expected_games_played=float(n), expected_games_missed=0.0,
            reasons=(prior.describe(pos),), prior=prior,
        )

    base_rate = prior.miss_rate(pos)
    detail = _multiplier_detail(pos, history, prior=prior, identified=identified)
    mult, basis = detail.multiplier, detail.basis
    target = min(max(base_rate * mult, 0.0), 0.99)
    # The onset is a PER-GAME hazard and a property of the player, so it is
    # solved once at the prior's own calibration horizon and against the prior's
    # own opening rate. Solving it at the caller's window length instead would
    # make the same player's weekly injury risk depend on how many weeks the
    # caller happened to ask about, and would let an `opening_absent` override
    # silently re-parameterize the hazard: pass 1.0 ("he is out for the opener")
    # and the solver would drive the onset to ZERO to keep the season total on
    # target, so a player known to be hurt came back MORE durable for the rest of
    # the year than an average one (measured before this split: 6.32 games missed
    # against 7.7 after).
    prior_open = min(max(prior.opening_absent.get(pos, 0.0) * mult, 0.0), 0.95)
    onset = solve_onset(
        prior.horizon_games, target * prior.horizon_games, prior_open, prior
    )
    open_p = (
        min(max(float(opening_absent), 0.0), 1.0)
        if opening_absent is not None
        else prior_open
    )
    path = availability_path(n, onset, open_p, prior)
    pmf = games_played_pmf(n, onset, open_p, prior)
    played = sum(path)
    achieved = (float(n) - played) / n

    return PlayerAvailability(
        player_key=player_key, position=pos, basis=basis,
        sample_games=history.games if history else 0,
        sample_missed=history.missed if history else 0,
        sample_seasons=history.seasons if history else (),
        multiplier=mult, miss_rate=achieved, target_miss_rate=target,
        onset=onset, opening_absent=open_p,
        game_weeks=weeks,
        week_available=MappingProxyType(dict(zip(weeks, path, strict=True))),
        games_played_pmf=pmf,
        expected_games_played=played,
        expected_games_missed=float(n) - played,
        reasons=_reasons(pos, detail, history, achieved, target, n, played,
                         open_p, prior_open, prior, history_window),
        prior=prior,
        fallback_code=detail.code,
        raw_multiplier=detail.raw,
        multiplier_clipped=detail.clipped,
        history_window=history_window,
        absent_seasons=history.absent_seasons if history else (),
        unknown_seasons=history.unknown_seasons if history else (),
    )


def _reasons(
    position: str,
    detail: MultiplierDetail,
    history: PlayerHistory | None,
    rate: float,
    target: float,
    n_games: int,
    played: float,
    opening: float,
    prior_opening: float,
    prior: DurabilityPrior,
    history_window: tuple[int, int] | None,
) -> tuple[str, ...]:
    """Plain-language reasons (Rule 6). The operator cannot smell a wrong rate,
    so every line names what the number is, where it came from, and how much of
    it is this player rather than his position.

    The standing requirement here is that the printed numbers RECONCILE: a reader
    who recomputes from the record, k and the weight must land on the multiplier
    the row actually shipped. That is why the clip gets its own line and why a
    season he missed entirely is named rather than folded into a span.
    """
    mult = detail.multiplier
    out = [
        f"Expects to play {played:.1f} of his club's {n_games} games in this "
        f"window and miss {n_games - played:.1f} "
        f"({100 * rate:.0f}% of games, {mult:.2f}x an average {position})."
    ]
    window = (
        f"{history_window[0]}-{history_window[1]}" if history_window else None
    )
    if detail.basis == BASIS_PLAYER and history is not None:
        weight = detail.weight
        seasons = _season_span(history.seasons)
        out.append(
            f"Basis: HIS OWN RECORD — {history.missed} of {history.games} club "
            f"games missed ({100 * history.miss_rate:.0f}%) in {seasons}. "
            f"That record is shrunk toward the {position} prior at "
            f"k={prior.shrink_k:.0f} game-equivalents, so it carries "
            f"{100 * weight:.0f}% of the weight and the position prior "
            f"{100 * (1 - weight):.0f}%."
        )
        if history.absent_seasons:
            span = _season_span(history.absent_seasons)
            out.append(
                f"That record INCLUDES {len(history.absent_seasons)} season(s) he "
                f"missed in full ({span}) — he was on an NFL club and did not play "
                f"a single game. Those are counted, in both halves of the "
                f"fraction; an appearances-only record would have deleted them and "
                f"priced him as the most durable kind of player."
            )
        if history.unknown_seasons:
            out.append(
                f"NOT in this record: {_season_span(history.unknown_seasons)}. He "
                f"did not appear in an NFL game then and nothing here proves he "
                f"was on a roster, so those seasons are counted neither as played "
                f"nor as missed. His true rate could be worse than the number "
                f"above; it cannot be better."
            )
        if history.role_weeks is not None:
            out.append(
                f"Sample shape: he appeared in {history.games - history.missed} of "
                f"those games and spent {history.role_weeks} weeks as one of his "
                f"club's top-{ROLE_DEPTH.get(position, 1)} {position}s by snaps — "
                f"the same starting-role gate the position rate was measured on "
                f"(minimum {prior.min_role_weeks} weeks to use his own record)."
            )
        if detail.clipped:
            lo, hi = prior.multiplier_clip
            out.append(
                f"CLIPPED: those numbers imply {detail.raw:.2f}x, and this row "
                f"ships {mult:.2f}x — the edge of the [{lo:.2f}, {hi:.2f}] range "
                f"the fit was ever measured over. The guard refuses to "
                f"extrapolate past the evidence, so the recomputation above will "
                f"not match the shipped number for this player."
            )
        out.append(
            f"That shrinkage is heavy ON PURPOSE: last season's missed-game rate "
            f"predicts this season's at only r={prior.yoy_correlation:.2f} "
            f"(r={prior.prediction_correlation:.2f} once shrunk), and the whole "
            f"adjustment is worth {100 * prior.mse_skill_vs_position:.0f}% of mean "
            f"squared error over using the position rate alone. It is a tilt, not "
            f"a diagnosis."
        )
    else:
        out.append(
            "Basis: POSITION PRIOR ONLY — "
            + _no_adjustment_note(detail.code, history, prior, position, window)
            + f" He is priced as an AVERAGE {position}, NOT as a durable one: a "
            f"rookie, or anyone we have never seen play, is an absence of "
            f"evidence and never evidence of durability."
        )
        if history is not None and history.absent_seasons:
            # The strongest fact in his record, on a row that is NOT using his
            # record. Swallowing it here would be the module's own failure mode
            # wearing a guard's clothes: the operator would see "priced as an
            # average TE" and never learn the man missed a whole season.
            out.append(
                f"NOT PRICED IN, but you should know: he missed "
                f"{_season_span(history.absent_seasons)} in full "
                f"({history.missed} of {history.games} club games missed across "
                f"{_season_span(history.seasons)}). The adjustment above is "
                f"withheld for the reason just given, NOT because the record is "
                f"clean — judge this one yourself."
            )
    out.append(prior.describe(position))
    out.append(prior.method_note(position))
    out.append(prior.block_note())
    if abs(opening - prior_opening) > 1e-9:
        out.append(
            f"Starting condition OVERRIDDEN: this window opens with a "
            f"{100 * opening:.0f}% chance he is already out, against the "
            f"{100 * prior_opening:.0f}% a season opener carries. His weekly "
            f"injury hazard is unchanged — only where he starts."
        )
    if abs(rate - target) > 0.005:
        out.append(
            f"Over a {n_games}-game window the numbers above come out at "
            f"{100 * rate:.0f}% of games missed rather than the "
            f"{100 * target:.0f}% season rate, because the starting condition "
            f"dominates a window this short. For a mid-season window pass "
            f"opening_absent=steady_state_absent(...) instead of the default."
        )
    return tuple(out)


def _no_adjustment_note(
    code: str | None,
    history: PlayerHistory | None,
    prior: DurabilityPrior,
    position: str,
    window: str | None,
) -> str:
    """One sentence per REASON the position prior was used.

    These were one sentence for five different facts, and three of the five were
    false statements about the player: an 85-game record was told it was "below
    the 8-game floor", and a player we never looked up was told he had "no prior
    season in the measured window (2021-2025)" — a window the caller may never
    have read, about a player the code never checked.
    """
    where = f"the measured window ({window})" if window else "the measured window"
    if code == NO_ADJUSTMENT_NO_ID:
        return (
            "he was never matched to a player id, so his participation record was "
            "NEVER LOOKED UP. This is a gap in our name-to-id crosswalk, not a "
            "finding about him: nothing here says whether he has missed games."
        )
    if code == NO_ADJUSTMENT_NO_RECORD:
        return (
            f"we looked him up and found no season in {where} in which he "
            f"appeared in an NFL game or was proven to be on a roster."
        )
    if code == NO_ADJUSTMENT_THIN and history is not None:
        return (
            f"{history.games} prior club games on record, below the "
            f"{prior.min_prior_games}-game floor."
        )
    if code == NO_ADJUSTMENT_ROLE and history is not None:
        appeared = (
            f" (he appeared in {history.played} of them)"
            if history.played is not None else ""
        )
        return (
            f"his {history.games}-game record{appeared} contains only "
            f"{history.role_weeks} "
            f"weeks as one of his club's top-{ROLE_DEPTH.get(position, 1)} "
            f"{position}s, below the {prior.min_role_weeks}-week starting-role "
            f"floor. A player who was never given the job did not \"miss\" the "
            f"games he did not dress for in any sense a durability rate means, so "
            f"his record measures his ROLE and is not used here."
        )
    if code == NO_ADJUSTMENT_NO_MEAN:
        games = f"{history.games}-game" if history is not None else "any"
        return (
            f"this prior carries NO measured prior-history mean for {position} "
            f"(the cohort it was built from missed nothing), so a relative "
            f"adjustment cannot be formed from a {games} record. The gap is in "
            f"the prior, not in him."
        )
    return f"no usable participation record in {where}."


def _article(word: str) -> str:
    """"an RB", "a QB" — the operator reads these lines as prose, so they should
    read like prose."""
    return "an" if word[:1].upper() in "AEFHILMNORSX" else "a"


def _season_span(seasons: Sequence[int]) -> str:
    """Render seasons as runs, so a GAP is visible.

    ``min-max`` was a lie with arithmetic attached: a record of (2021, 2023,
    2024, 2025) printed as "2021-2025" — five seasons, ~85 club games — beside a
    68-game denominator, and the two seasons missing from it were missing
    precisely because the player was hurt for all of both. This prints
    "2021, 2023-2025".
    """
    uniq = sorted(set(int(s) for s in seasons))
    if not uniq:
        return "no measured season"
    runs: list[list[int]] = [[uniq[0], uniq[0]]]
    for season in uniq[1:]:
        if season == runs[-1][1] + 1:
            runs[-1][1] = season
        else:
            runs.append([season, season])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def build_availability(
    players: Iterable[tuple[str, str, Sequence[int]]],
    *,
    prior: DurabilityPrior = DEFAULT_DURABILITY,
    histories: Mapping[str, PlayerHistory] | None = None,
    history_keys: Mapping[str, str] | None = None,
    opening_absent: Mapping[str, float] | None = None,
    history_window: tuple[int, int] | None = None,
) -> dict[str, PlayerAvailability]:
    """Price a whole board.

    ``players`` is (player_key, position, game_weeks). ``histories`` is keyed by
    gsis id; ``history_keys`` maps player_key -> gsis id when the board is keyed
    on something else (the draft board is keyed on espn ids). A key with no
    history entry falls to the position prior and SAYS so — never silently, and
    it says WHICH of the two reasons applies: a board key that ``history_keys``
    cannot resolve at all was never looked up, and the row must not report that
    as a finding about the player.
    """
    histories = histories or {}
    history_keys = history_keys or {}
    opening = opening_absent or {}
    out: dict[str, PlayerAvailability] = {}
    for key, position, weeks in players:
        pos = canon_position(position)
        if pos is None:
            raise DurabilityError(
                f"{key}: position {position!r} is not a league position; "
                "canonicalize with valuation.canon_position before pricing "
                "availability (refusing rather than defaulting to durable)"
            )
        # Identified means we HAVE an id for him: either the board key is the id
        # (no map given) or the map resolved it. A key absent from a supplied map
        # was never looked up.
        lookup = history_keys.get(key)
        identified = lookup is not None or not history_keys
        out[key] = player_availability(
            key, pos, weeks, prior=prior,
            history=histories.get(lookup if lookup is not None else key),
            opening_absent=opening.get(key),
            identified=identified,
            history_window=history_window,
        )
    return out


def sample_available_weeks(
    row: PlayerAvailability, rng: random.Random
) -> dict[int, bool]:
    """One correlated draw of his season: {week: played}.

    The marginals in ``week_available`` are exact, but a grader that draws each
    week independently from them recreates the very error this module exists to
    fix — it turns one six-week absence into six scattered one-week absences,
    which a bench absorbs and a real season does not. This walks the same chain
    the marginals came from, so blocks come out as blocks.

    All randomness comes from the passed-in ``random.Random`` (Rule: determinism);
    nothing here touches the global RNG or the clock.
    """
    hazard = row.prior.return_hazard
    out: dict[int, bool] = {}
    absent_for = 0
    first = True
    for week in row.game_weeks:
        if first:
            absent_for = (
                row.prior.opening_duration if rng.random() < row.opening_absent else 0
            )
            first = False
        elif absent_for:
            back = hazard[min(absent_for, len(hazard)) - 1]
            absent_for = 0 if rng.random() < back else absent_for + 1
        else:
            absent_for = 1 if rng.random() < row.onset else 0
        out[week] = absent_for == 0
    return out


# ------------------------------------------------------------------ DB layer
#
# Rule 1: every accessor below is keyword-only ``as_of`` with no default, defaults
# to the ``historical`` view, and threads ``view`` straight into the underlying
# accessor. Leakage tests live in tests/test_availability.py.


@dataclass(frozen=True)
class PlayerSeason:
    """One player's participation in one season — the measurement's unit of work.

    ``roster_basis`` says HOW we know he was in the league that season. Ordinary
    rows are ``SEASON_APPEARED``. The other two values are seasons with ZERO
    appearances, which are the most informative rows in the whole panel and which
    this module originally dropped on the floor — see ``player_seasons``.
    """

    gsis_id: str
    season: int
    position: str | None
    teams: tuple[str, ...]
    game_weeks: tuple[int, ...]
    played_weeks: tuple[int, ...]
    role_weeks: int
    roster_basis: str = SEASON_APPEARED

    @property
    def games(self) -> int:
        return len(self.game_weeks)

    @property
    def missed(self) -> int:
        return len(self.game_weeks) - len(self.played_weeks)

    def horizon(self, n: int) -> tuple[int, int]:
        """(missed, games) over his FIRST ``n`` club games — the calibrated window.

        First, not last, and the difference is the whole reason
        ``FANTASY_HORIZON_GAMES`` is 16: the game this drops is his club's LAST
        one, which is the rest-your-starters finale (measured onset 13.6% against
        ~7% every other week) and which never lands in a scoring week of this
        league. Taking the last n instead would import exactly that effect and
        move every position's rate (measured: QB +6.6% relative, TE +9.5%).
        """
        window = self.game_weeks[:n]
        played = set(self.played_weeks)
        return sum(1 for w in window if w not in played), len(window)


def team_game_weeks(
    conn,
    *,
    as_of,
    season: int,
    played_only: bool = False,
    view: base.AsOfView = "historical",
) -> dict[str, tuple[int, ...]]:
    """{team: the REG weeks it plays} for ``season``. Rule 1: keyword-only as_of.

    ``played_only`` is not a convenience — it is the difference between two
    incompatible questions, and defaulting it wrong is a silent, total corruption
    of the measurement. A REG schedule row is stamped
    ``knowable_as_of = <season>-08-01`` (schedules.py: structural facts are known
    at release), so the WHOLE slate is visible from preseason onward at any
    ``as_of``. A durability denominator built from that slate counts every game
    that has not happened yet as a game the player MISSED: read at week 6, every
    player in the league has "missed" twelve games and the model reports the
    entire NFL as made of glass. ``player_seasons`` therefore passes
    ``played_only=True``, which keeps only games whose ``gameday`` has arrived by
    ``as_of``.

    The forward slate (``played_only=False``) is the right answer for the other
    question — which weeks does this club play in the season being graded, so
    which one is its bye — and that is what ``load_durability_book`` reads.
    """
    cutoff = normalize_as_of(as_of).isoformat()
    weeks: dict[str, set[int]] = {}
    for row in schedules.get_schedule(conn, as_of=as_of, season=season, view=view):
        if (row["game_type"] or "").upper() != "REG":
            continue
        if played_only:
            gameday = base.iso_date(row["gameday"])
            # A row with no resolvable gameday cannot be proven played; dropping
            # is the leakage-safe default this package uses everywhere else.
            if gameday is None or gameday > cutoff:
                continue
        for side in ("home_team", "away_team"):
            team = row[side]
            if team:
                weeks.setdefault(team, set()).add(int(row["week"]))
    return {t: tuple(sorted(ws)) for t, ws in weeks.items()}


def player_seasons(
    conn,
    *,
    as_of,
    season: int,
    view: base.AsOfView = "historical",
    roster_evidence: bool = True,
) -> dict[str, PlayerSeason]:
    """Every player's appearances vs his club's schedule for one season.

    An appearance is a ``weekly_stats`` REG row OR a ``snap_counts`` row with at
    least one snap — the union, because a receiver who ran routes and drew no
    targets has no stat line but does have snaps, and counting him absent would
    manufacture missed games out of a quiet afternoon.

    ``role_weeks`` counts weeks he was one of his club's top-``ROLE_DEPTH``
    players at his position by offensive snaps. It is the PREVIOUS-season gate the
    cohort is built on; it is computed here so the gate never has to re-read.

    THE SEASON A PLAYER MISSED ENTIRELY, and why it needs its own rule
    ------------------------------------------------------------------
    Keying the result on "players who appeared" deletes a wholly-missed season
    from BOTH the numerator and the denominator, which inverts the sign of the
    strongest durability signal there is: a kicker who spent 2025 on a roster and
    never kicked came out of the first cut of this module 0/67, 0.47x, the FIFTH
    most durable player on the 2026 board. Counting his lost season puts him at
    0/67 -> 17/84, 2.02x, and 3.5 expected games lower.

    So a season with zero appearances is emitted as a full slate of missed games
    when — and only when — we can prove he was in the league. The proof this
    module accepts is the weekly injury report (``_roster_evidence``): a report
    row names a player, a club and a week, and clubs file it for players on the
    active roster. It is deliberately the ONLY evidence source, because it is the
    only one that spans 2021-2025 UNIFORMLY (1,413-1,459 distinct players every
    season). ``depth_chart_slots`` was measured and rejected for exactly that
    reason: it exists for 2025-2026 only and would have found 455 absent players
    in 2025 against 47 from the injury report, i.e. it would have made the newest
    season look ten times more absence-prone than the four before it — a
    measurement artefact dressed as a trend.

    Roster evidence is INCOMPLETE by construction, and in the direction that
    matters: this module's own headline finding is that a player who lands on IR
    stops appearing on the injury report altogether, so the worst absences are the
    least visible. ``season_panel`` closes part of that gap with the second rule
    (a season bracketed by two seasons he appeared in); what neither rule can
    reach stays out of the record and is DISCLOSED as ``unknown_seasons`` rather
    than being quietly counted as healthy.

    Pass ``roster_evidence=False`` for the appearances-only view (it is what the
    presence-vs-injury-report cross-check needs, since an evidence row is by
    definition one the injury report can see).
    """
    # played_only: the denominator is games that HAVE HAPPENED by as_of. See
    # team_game_weeks — the alternative silently turns every unplayed week into a
    # missed game.
    slate = team_game_weeks(
        conn, as_of=as_of, season=season, played_only=True, view=view
    )
    reg = {(team, w) for team, ws in slate.items() for w in ws}

    appearances: dict[str, set[int]] = {}
    teams: dict[str, set[str]] = {}
    votes: dict[str, dict[str, int]] = {}

    def note(gsis, week, team, position):
        appearances.setdefault(gsis, set()).add(int(week))
        if team:
            teams.setdefault(gsis, set()).add(team)
        pos = canon_position(position)
        if pos:
            tally = votes.setdefault(gsis, {})
            tally[pos] = tally.get(pos, 0) + 1

    for row in weekly_stats.get_weekly_stats(conn, as_of=as_of, season=season, view=view):
        if (row["season_type"] or "").upper() != "REG":
            continue
        gsis = row["player_id"]
        if not gsis:
            continue
        note(gsis, row["week"], row["recent_team"], row["position"])

    # snaps: appearance + the role ranking, in one pass over the season
    ranked: dict[tuple[int, str, str], list[tuple[float, str]]] = {}
    for row in snap_counts.get_snap_counts(conn, as_of=as_of, season=season, view=view):
        gsis, team, week = row["gsis_id"], row["team"], row["week"]
        if not gsis or (team, int(week)) not in reg:
            continue
        offense = row["offense_snaps"] or 0.0
        total = offense + (row["defense_snaps"] or 0.0) + (row["st_snaps"] or 0.0)
        if total <= 0:
            continue
        note(gsis, week, team, row["position"])
        pos = canon_position(row["position"])
        if pos in ROLE_DEPTH:
            ranked.setdefault((int(week), team, pos), []).append((offense, gsis))

    role: dict[str, int] = {}
    for (_week, _team, pos), entries in ranked.items():
        # Deterministic: snaps desc, then gsis — a snap tie must not depend on
        # the order sqlite happened to return the rows in.
        entries.sort(key=lambda e: (-e[0], e[1]))
        for offense, gsis in entries[: ROLE_DEPTH[pos]]:
            if pos == "K" or offense > 0:
                role[gsis] = role.get(gsis, 0) + 1

    out: dict[str, PlayerSeason] = {}
    for gsis, weeks in appearances.items():
        clubs = teams.get(gsis) or set()
        slate_weeks: set[int] = set()
        for club in clubs:
            # The UNION of his clubs' weeks, not the first club's and not the
            # intersection: a mid-season trade must not manufacture missed games
            # out of the weeks his new club had already played, and must not
            # delete the weeks his old one still had left. Measured on the live
            # 2021-2025 panel this is not cosmetic — 55 of 1,017 cohort rows have
            # more than one club and 49 of those carry an 18-week union (two
            # byes), where the intersection would erase real games.
            slate_weeks |= set(slate.get(club, ()))
        if not slate_weeks:
            continue
        tally = votes.get(gsis) or {}
        position = max(tally.items(), key=lambda kv: (kv[1], kv[0]))[0] if tally else None
        out[gsis] = PlayerSeason(
            gsis_id=gsis, season=int(season), position=position,
            teams=tuple(sorted(clubs)),
            game_weeks=tuple(sorted(slate_weeks)),
            played_weeks=tuple(sorted(weeks & slate_weeks)),
            role_weeks=role.get(gsis, 0),
            roster_basis=SEASON_APPEARED,
        )

    if roster_evidence:
        for gsis, (clubs, position) in _roster_evidence(
            conn, as_of=as_of, season=season, view=view
        ).items():
            if gsis in out:
                continue
            known = sorted(c for c in clubs if slate.get(c))
            if not known:
                # No club we can price him against — an absence with no
                # denominator is not a fact, so it is not written.
                continue
            slate_weeks = set()
            for club in known:
                slate_weeks |= set(slate[club])
            out[gsis] = PlayerSeason(
                gsis_id=gsis, season=int(season), position=position,
                teams=tuple(known),
                game_weeks=tuple(sorted(slate_weeks)),
                played_weeks=(),
                role_weeks=0,
                roster_basis=SEASON_ROSTER_EVIDENCE,
            )
    return out


def _roster_evidence(
    conn,
    *,
    as_of,
    season: int,
    view: base.AsOfView = "historical",
) -> dict[str, tuple[tuple[str, ...], str | None]]:
    """{gsis: (clubs, position)} for everyone the injury report places on a roster.

    Rule 1: keyword-only ``as_of``, ``view`` threaded into ``get_injuries``. The
    designation itself is ignored on purpose — this is not asking "was he hurt",
    which the module docstring shows the report answers for only ~23% of missed
    weeks. It asks the one question the report answers RELIABLY: was he on an NFL
    club's roster in this season at all.
    """
    clubs: dict[str, dict[str, int]] = {}
    positions: dict[str, dict[str, int]] = {}
    for row in injuries.get_injuries(conn, as_of=as_of, season=season, view=view):
        gsis = row["gsis_id"]
        team = row["team"]
        if not gsis or not team:
            continue
        tally = clubs.setdefault(gsis, {})
        tally[team] = tally.get(team, 0) + 1
        pos = canon_position(row["position"])
        if pos:
            ptally = positions.setdefault(gsis, {})
            ptally[pos] = ptally.get(pos, 0) + 1
    out: dict[str, tuple[tuple[str, ...], str | None]] = {}
    for gsis, tally in clubs.items():
        ptally = positions.get(gsis) or {}
        # Deterministic: most rows wins, ties broken by name (Rule: determinism —
        # never by the order sqlite happened to return the rows in).
        position = max(ptally.items(), key=lambda kv: (kv[1], kv[0]))[0] if ptally else None
        out[gsis] = (tuple(sorted(tally)), position)
    return out


def player_histories(
    conn,
    *,
    as_of,
    first_season: int,
    last_season: int,
    view: base.AsOfView = "historical",
) -> dict[str, PlayerHistory]:
    """Per-player participation record pooled across ``first_season..last_season``.

    Built on ``season_panel``, so it inherits BOTH absent-season rules (roster
    evidence and bracketing) — the record is "his clubs' games across every
    season we can prove he was in the league", not "across the seasons he
    happened to appear in". The difference is the module's largest single defect
    and it ran the wrong way: 258 of 3,796 players in the live panel had a season
    silently deleted from both halves of their record, and the deletion made the
    most fragile of them read as the most durable.

    Rule 1: keyword-only ``as_of``, no default, ``view`` threaded. Under the
    default ``historical`` view a bulk-backfilled history reads EMPTY (every row
    was retrieved 2026-07-25); ``load_player_histories`` is the deliberate
    latest_truth binding.
    """
    if int(last_season) < int(first_season):
        raise DurabilityError(
            f"last_season {last_season} precedes first_season {first_season}"
        )
    panel = season_panel(
        conn, as_of=as_of, first_season=first_season, last_season=last_season,
        view=view,
    )
    return histories_from_panel(panel, window=(int(first_season), int(last_season)))


def histories_from_panel(
    panel: Mapping[int, Mapping[str, PlayerSeason]],
    *,
    window: tuple[int, int] | None = None,
) -> dict[str, PlayerHistory]:
    """Pool a panel into one record per player. Pure — no DB, no clock."""
    totals: dict[str, dict] = {}
    for season in sorted(panel):
        for gsis, ps in panel[season].items():
            slot = totals.setdefault(
                gsis,
                {"games": 0, "missed": 0, "played": 0, "role": 0,
                 "seasons": [], "absent": [], "unknown": []},
            )
            slot["games"] += ps.games
            slot["missed"] += ps.missed
            slot["played"] += len(ps.played_weeks)
            slot["role"] += ps.role_weeks
            slot["seasons"].append(ps.season)
            if ps.roster_basis != SEASON_APPEARED:
                slot["absent"].append(ps.season)
            if ps.game_weeks and not ps.played_weeks and ps.roster_basis == SEASON_APPEARED:
                # An appearances-derived row with nothing played cannot happen
                # (he is in it because he appeared) — but if a future change makes
                # one, it is still a season he missed in full and must be named.
                slot["absent"].append(ps.season)
    for gsis, unknown in _unpriceable_gaps(panel).items():
        if gsis in totals:
            totals[gsis]["unknown"].extend(unknown)
    return {
        gsis: PlayerHistory(
            gsis_id=gsis,
            games=v["games"],
            missed=v["missed"],
            seasons=tuple(sorted(v["seasons"])),
            absent_seasons=tuple(sorted(set(v["absent"]))),
            unknown_seasons=tuple(sorted(set(v["unknown"]))),
            played=v["played"],
            role_weeks=v["role"],
            window=window,
        )
        for gsis, v in totals.items()
    }


def load_player_histories(
    conn,
    *,
    as_of,
    first_season: int = 2021,
    last_season: int = 2025,
) -> dict[str, PlayerHistory]:
    """``player_histories`` bound to ``latest_truth`` — the right view HERE because:

    completed seasons are IMMUTABLE BULK HISTORY. Every 2021-2025 row in this
    database arrived in one backfill (``retrieved_as_of`` = 2026-07-25 on
    weekly_stats, snap_counts and injuries alike), so the safe-default
    ``historical`` view — which gates retrieval time as well as knowledge time —
    returns NOTHING for any 2021-2025 ``as_of``, silently, and a durability model
    reading it would price every player as never having missed a game. That is
    the exact footgun ``base.latest_truth`` exists to close (CLAUDE.md Rule 1).

    The fact-time gate is NOT relaxed by this: ``latest_truth`` still hides a game
    that had not been played by ``as_of``, so a draft-day read cannot see a
    future season and a backtest at week 6 cannot see week 7. The leakage test
    pins that.
    """
    return base.latest_truth(player_histories)(
        conn, as_of=as_of, first_season=first_season, last_season=last_season
    )


# ------------------------------------------------------------- the measurement


@dataclass(frozen=True)
class DurabilityMeasurement:
    """What ``measure_durability`` found — the audit trail behind the shipped prior.

    TWO WINDOWS, and they are not interchangeable. ``season_miss_rate``,
    ``mean_games_missed`` and ``games_missed_pmf`` are measured over a player's
    FIRST ``horizon_games`` club games — the fantasy window, which excludes the
    rest-your-starters finale. ``presence_mean_missed`` and
    ``injury_only_mean_missed`` are measured over his WHOLE season, because they
    exist to be compared with each other (how much of real missed time the injury
    report can see), not with the calibrated rate. Comparing across the two is
    comparing 16 games with 17, not finding a discrepancy.

    The three injury-report figures (``injury_report_coverage``,
    ``injury_only_mean_missed``, ``presence_mean_missed``) additionally EXCLUDE
    cohort rows whose whole season was detected through the injury report itself
    (``SEASON_ROSTER_EVIDENCE``). Counting a season that was selected for carrying
    an injury row would let the report grade its own coverage.
    """

    horizon_games: int
    cohort_n: Mapping[str, int]
    season_miss_rate: Mapping[str, float]
    mean_games_missed: Mapping[str, float]
    opening_absent: Mapping[str, float]
    prior_history_rate: Mapping[str, float]
    return_hazard: tuple[float, ...]
    return_hazard_n: tuple[int, ...]
    opening_return: float
    opening_mean_missed: float
    games_missed_pmf: Mapping[str, tuple[float, ...]]
    injury_report_coverage: Mapping[str, float]
    injury_only_mean_missed: Mapping[str, float]
    presence_mean_missed: Mapping[str, float]
    mean_absence_block: float
    seasons: tuple[int, ...]
    #: The upper bound described on ``DurabilityPrior.season_miss_rate_upper``,
    #: and the count of cohort-eligible outcome seasons it is built from: players
    #: who held a starting role in Y-1 and then have NO row for Y at all. They are
    #: not in the cohort, so they bias every rate DOWNWARD by an amount this
    #: reports rather than leaves for someone to discover.
    season_miss_rate_upper: Mapping[str, float] = MappingProxyType({})
    unprovable_cohort_seasons: int = 0
    unprovable_by_position: Mapping[str, int] = MappingProxyType({})

    def as_prior(self, *, label: str, cohort: str, source: str) -> DurabilityPrior:
        """Freeze this measurement into a usable prior (how DEFAULT_DURABILITY
        was produced — the constants are re-derivable, not transcribed)."""
        return DurabilityPrior(
            season_miss_rate=MappingProxyType(dict(self.season_miss_rate)),
            opening_absent=MappingProxyType(dict(self.opening_absent)),
            return_hazard=self.return_hazard,
            opening_duration=DEFAULT_DURABILITY.opening_duration,
            prior_history_rate=MappingProxyType(dict(self.prior_history_rate)),
            shrink_k=DEFAULT_DURABILITY.shrink_k,
            min_prior_games=DEFAULT_DURABILITY.min_prior_games,
            min_role_weeks=DEFAULT_DURABILITY.min_role_weeks,
            multiplier_clip=DEFAULT_DURABILITY.multiplier_clip,
            never_absent=DEFAULT_DURABILITY.never_absent,
            cohort_n=MappingProxyType(dict(self.cohort_n)),
            horizon_games=self.horizon_games,
            yoy_correlation=DEFAULT_DURABILITY.yoy_correlation,
            prediction_correlation=DEFAULT_DURABILITY.prediction_correlation,
            mse_skill_vs_position=DEFAULT_DURABILITY.mse_skill_vs_position,
            injury_report_coverage=MappingProxyType(dict(self.injury_report_coverage)),
            mean_absence_block=self.mean_absence_block,
            season_miss_rate_upper=MappingProxyType(dict(self.season_miss_rate_upper)),
            unprovable_cohort_seasons=self.unprovable_cohort_seasons,
            opening_return=self.opening_return,
            cohort=cohort, label=label, source=source,
        )


def season_panel(
    conn,
    *,
    as_of,
    first_season: int,
    last_season: int,
    view: base.AsOfView = "historical",
    bracket_absences: bool = True,
) -> dict[int, dict[str, PlayerSeason]]:
    """{season: {gsis: PlayerSeason}} — the one expensive read, done once.

    Walking five seasons of ``snap_counts`` through ``select_as_of`` costs ~10 s
    on this box, so both measurement entry points accept an already-built panel
    rather than each paying for their own (measured: 10.0 s for 2021-2025).
    Rule 1: keyword-only ``as_of``, ``view`` threaded.

    THE SECOND ABSENT-SEASON RULE lives here because it is the one that needs
    more than one season to see. ``player_seasons`` can only charge a wholly
    missed season it has positive roster evidence for, and this module's own
    headline finding is that the evidence goes missing exactly when the absence is
    worst (a player on IR drops off the injury report entirely — the WR who tore
    an ACL in 2022 and an Achilles in 2023 has no injury row in either year). So a
    season with no appearances that is BRACKETED — he appeared in an earlier
    season and in a later one, inside this window — is charged too: he was
    demonstrably an NFL player on both sides of it and played zero games in
    between, which is precisely what this module measures ("games not played for
    any reason — injury, IR, healthy scratch, benching, suspension or being cut").

    ``bracket_absences=False`` turns that inference off and leaves only what the
    injury report can prove.

    This rule stops at the edges of his career as this window sees it: a season
    before his first appearance or after his last is NOT bracketed, because out
    there "on a roster and never played" and "not in the NFL" are the same empty
    row and charging a rookie for the years he was in college would be the
    module's own worst failure inverted. The roster-evidence rule in
    ``player_seasons`` has no such restriction and needs none — a named club on a
    filed injury report is a roster spot whenever it happened.
    """
    seasons = list(range(int(first_season), int(last_season) + 1))
    panel = {
        s: player_seasons(conn, as_of=as_of, season=s, view=view)
        for s in seasons
    }
    if bracket_absences:
        slates = {
            s: team_game_weeks(
                conn, as_of=as_of, season=s, played_only=True, view=view
            )
            for s in seasons
        }
        _fill_bracketed_absences(panel, slates)
    _carry_positions(panel)
    return panel


def _appearance_seasons(
    panel: Mapping[int, Mapping[str, PlayerSeason]]
) -> dict[str, list[int]]:
    """{gsis: the seasons he actually APPEARED in}, sorted.

    Only appearance seasons bracket: an evidence-only row proves a roster spot,
    which is a weaker statement than "he took a snap", and letting one widen the
    bracket would charge unproven seasons off the back of an unproven one.
    """
    out: dict[str, list[int]] = {}
    for season in sorted(panel):
        for gsis, ps in panel[season].items():
            if ps.roster_basis == SEASON_APPEARED:
                out.setdefault(gsis, []).append(season)
    return out


def _fill_bracketed_absences(
    panel: dict[int, dict[str, PlayerSeason]],
    slates: Mapping[int, Mapping[str, tuple[int, ...]]],
) -> None:
    """Charge a zero-appearance season sitting between two appearance seasons."""
    for gsis, seasons in _appearance_seasons(panel).items():
        for season in range(seasons[0] + 1, seasons[-1]):
            if gsis in panel.get(season, {}):
                continue
            nearest = _nearest_appearance(panel, gsis, seasons, season)
            if nearest is None:
                continue
            slate = slates.get(season) or {}
            clubs = sorted(c for c in nearest.teams if slate.get(c))
            if not clubs:
                # We know he was in the league and we know he played nothing —
                # but with no club slate there is no denominator, so this is
                # left OUT and reported as an unknown season rather than guessed.
                continue
            weeks: set[int] = set()
            for club in clubs:
                weeks |= set(slate[club])
            panel[season][gsis] = PlayerSeason(
                gsis_id=gsis, season=int(season), position=nearest.position,
                teams=tuple(clubs), game_weeks=tuple(sorted(weeks)),
                played_weeks=(), role_weeks=0,
                roster_basis=SEASON_BRACKETED,
            )


def _nearest_appearance(
    panel: Mapping[int, Mapping[str, PlayerSeason]],
    gsis: str,
    seasons: Sequence[int],
    season: int,
) -> PlayerSeason | None:
    """His appearance row closest to ``season``, preferring the one BEFORE it.

    The club he was last with is the better guess for whose games he was missing;
    it also makes the choice deterministic instead of dict-order dependent.
    """
    before = [s for s in seasons if s < season]
    after = [s for s in seasons if s > season]
    for candidate in ([max(before)] if before else []) + ([min(after)] if after else []):
        row = panel.get(candidate, {}).get(gsis)
        if row is not None:
            return row
    return None


def _carry_positions(panel: dict[int, dict[str, PlayerSeason]]) -> None:
    """Give a zero-appearance row the position his APPEARANCE seasons show.

    Without this an evidence row carries whatever the injury report called him
    (raw NFL positions: 'T', 'G', 'LS'), and a row whose position falls outside
    ``ROLE_DEPTH`` is skipped by ``cohort_seasons`` — so the absent seasons this
    fix exists to add would have been silently dropped again, one layer down.
    """
    appearances = _appearance_seasons(panel)
    for season in sorted(panel):
        for gsis, ps in list(panel[season].items()):
            if ps.roster_basis == SEASON_APPEARED:
                continue
            seasons = appearances.get(gsis)
            if not seasons:
                continue
            nearest = _nearest_appearance(panel, gsis, seasons, season)
            if nearest is None or nearest.position is None:
                continue
            if nearest.position != ps.position:
                panel[season][gsis] = dataclasses.replace(
                    ps, position=nearest.position
                )


def _unpriceable_gaps(
    panel: Mapping[int, Mapping[str, PlayerSeason]]
) -> dict[str, list[int]]:
    """{gsis: interior seasons with no row at all} — the gaps nothing could price.

    The span here is every season we KNOW he was in the league — an appearance or
    a proven roster spot — while ``_fill_bracketed_absences`` only charges a gap
    bracketed by two APPEARANCES. That asymmetry is deliberate and it is the
    honest direction: charging a season needs the stronger claim, noticing that
    one is missing needs only the weaker one. So a player who appeared in 2021 and
    was demonstrably on a roster in 2023 has 2022 REPORTED here (17 players on the
    live 2021-2025 panel) rather than either charged or silently skipped.

    Reported rather than assumed away because a gap counted as neither played nor
    missed makes the record look better than it is, and that is the one direction
    this module must never fail silently in.
    """
    known: dict[str, list[int]] = {}
    for season in sorted(panel):
        for gsis in panel[season]:
            known.setdefault(gsis, []).append(season)
    out: dict[str, list[int]] = {}
    for gsis, seasons in known.items():
        missing = [
            s for s in range(seasons[0] + 1, seasons[-1])
            if gsis not in panel.get(s, {})
        ]
        if missing:
            out[gsis] = missing
    return out


def cohort_seasons(
    panel: Mapping[int, Mapping[str, PlayerSeason]],
    *,
    min_role_weeks: int = COHORT_MIN_ROLE_WEEKS,
) -> list[PlayerSeason]:
    """Season-Y rows for players who held a starting role in season Y-1.

    The gate reads only Y-1, so it cannot leak the Y outcome it selects on; the
    earliest season in the panel therefore supplies histories only, never
    outcomes.
    """
    seasons = sorted(panel)
    out: list[PlayerSeason] = []
    for season in seasons[1:]:
        prev = panel.get(season - 1) or {}
        for gsis, ps in panel[season].items():
            if ps.position not in ROLE_DEPTH:
                continue
            before = prev.get(gsis)
            if before is None or before.role_weeks < min_role_weeks:
                continue
            out.append(ps)
    # Deterministic order: the fits below iterate this list and must not depend
    # on dict insertion order across runs.
    out.sort(key=lambda ps: (ps.season, ps.gsis_id))
    return out


def prior_record(
    panel: Mapping[int, Mapping[str, PlayerSeason]], row: PlayerSeason
) -> tuple[int, int]:
    """(missed, games) for ``row``'s player in every panel season BEFORE his."""
    missed = games = 0
    for season in sorted(panel):
        if season >= row.season:
            break
        before = panel[season].get(row.gsis_id)
        if before is not None:
            missed += before.missed
            games += before.games
    return missed, games


@dataclass(frozen=True)
class PlayerAdjustmentFit:
    """The audit trail behind ``shrink_k`` — how much a player's own record is worth.

    ``by_k`` is (k, mean squared error, corr(prediction, outcome)) so the choice
    of ``shrink_k`` can be re-read rather than taken on faith, and
    ``position_only_mse`` is the baseline it has to beat.
    """

    n: int
    horizon_games: int
    yoy_correlation: float
    position_only_mse: float
    by_k: tuple[tuple[float, float, float], ...]
    best_k: float
    best_mse: float
    best_correlation: float
    mean_predicted: float
    mean_actual: float
    multiplier_p05: float
    multiplier_p95: float
    multiplier_min: float
    multiplier_max: float

    @property
    def mse_skill_vs_position(self) -> float:
        if not self.position_only_mse:
            return 0.0
        return 1.0 - self.best_mse / self.position_only_mse


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return 0.0
    return sxy / (sxx * syy) ** 0.5


def fit_player_adjustment(
    conn,
    *,
    as_of,
    first_season: int,
    last_season: int,
    horizon_games: int = FANTASY_HORIZON_GAMES,
    min_role_weeks: int = COHORT_MIN_ROLE_WEEKS,
    candidate_k: Sequence[float] = (10, 20, 30, 40, 50, 60, 80, 100, 150, 250),
    view: base.AsOfView = "historical",
) -> PlayerAdjustmentFit:
    """Re-derive ``shrink_k`` and the year-over-year signal it is sized against.

    Rule 1, to the letter: keyword-only ``as_of`` with NO default. It used to
    carry ``as_of=None`` so one function could serve both the read-the-database
    and reuse-a-panel calls; that made it the only accessor in the package whose
    ``as_of`` had a default, and a rule with one waiver in it is a rule people
    learn to waive. The panel path is ``fit_player_adjustment_from_panel``, which
    takes no ``as_of`` at all because it reads nothing.
    """
    panel = season_panel(conn, as_of=as_of, first_season=first_season,
                         last_season=last_season, view=view)
    return fit_player_adjustment_from_panel(
        panel, horizon_games=horizon_games, min_role_weeks=min_role_weeks,
        candidate_k=candidate_k,
    )


def fit_player_adjustment_from_panel(
    panel: Mapping[int, Mapping[str, PlayerSeason]],
    *,
    horizon_games: int = FANTASY_HORIZON_GAMES,
    min_role_weeks: int = COHORT_MIN_ROLE_WEEKS,
    candidate_k: Sequence[float] = (10, 20, 30, 40, 50, 60, 80, 100, 150, 250),
) -> PlayerAdjustmentFit:
    """The fit itself, over an already-read panel. Pure: no DB, no clock.

    Each cohort row is predicted from the player's OWN earlier panel seasons only
    (``prior_record``), so the prediction for a row never sees that row. The SWEEP
    is nevertheless IN-SAMPLE: ``out_rate``, ``mu_prior`` and the k that wins are
    all derived from the same 1,017 rows the mse is then read off. No season is
    held out. That is disclosed here, in ``DEFAULT_DURABILITY.shrink_k``'s comment
    and in the module docstring, because the shipped k was once described as
    "MSE-optimal ... on held-out player-seasons" and it was neither.
    """
    if not panel:
        raise DurabilityError("fit_player_adjustment needs a non-empty panel")
    cohort = cohort_seasons(panel, min_role_weeks=min_role_weeks)

    positions = tuple(sorted(ROLE_DEPTH))
    out_rate, mu_prior = {}, {}
    for p in positions:
        rows = [ps for ps in cohort if ps.position == p]
        if not rows:
            continue
        pairs = [ps.horizon(horizon_games) for ps in rows]
        games = sum(g for _, g in pairs)
        out_rate[p] = (sum(m for m, _ in pairs) / games) if games else 0.0
        pm = pg = 0
        for ps in rows:
            m, g = prior_record(panel, ps)
            pm += m
            pg += g
        mu_prior[p] = (pm / pg) if pg else 0.0

    samples = []
    for ps in cohort:
        if ps.position not in out_rate or not mu_prior.get(ps.position):
            continue
        m, g = prior_record(panel, ps)
        om, og = ps.horizon(horizon_games)
        if og == 0:
            continue
        samples.append((ps.position, m, g, om / og))
    if not samples:
        raise DurabilityError("no cohort rows with prior history — nothing to fit")

    actual = [s[3] for s in samples]
    baseline = sum((out_rate[s[0]] - s[3]) ** 2 for s in samples) / len(samples)

    prev_rate, next_rate = [], []
    for ps in cohort:
        before = panel.get(ps.season - 1, {}).get(ps.gsis_id)
        if before is None or before.games == 0:
            continue
        om, og = ps.horizon(horizon_games)
        if og:
            prev_rate.append(before.missed / before.games)
            next_rate.append(om / og)

    by_k: list[tuple[float, float, float]] = []
    for k in candidate_k:
        preds = []
        for position, m, g, _ in samples:
            mu = mu_prior[position]
            mult = 1.0
            if g >= DEFAULT_DURABILITY.min_prior_games:
                mult = ((m + k * mu) / (g + k)) / mu
            preds.append(out_rate[position] * mult)
        mse = sum((p - a) ** 2 for p, a in zip(preds, actual, strict=True)) / len(preds)
        by_k.append((float(k), mse, _pearson(preds, actual)))
    best = min(by_k, key=lambda row: row[1])

    best_preds = []
    mults = []
    for position, m, g, _ in samples:
        mu = mu_prior[position]
        raw = 1.0
        if g >= DEFAULT_DURABILITY.min_prior_games:
            raw = ((m + best[0] * mu) / (g + best[0])) / mu
        mults.append(raw)
        best_preds.append(out_rate[position] * raw)
    mults.sort()
    return PlayerAdjustmentFit(
        n=len(samples),
        horizon_games=horizon_games,
        yoy_correlation=_pearson(prev_rate, next_rate),
        position_only_mse=baseline,
        by_k=tuple(by_k),
        best_k=best[0], best_mse=best[1], best_correlation=best[2],
        mean_predicted=sum(best_preds) / len(best_preds),
        mean_actual=sum(actual) / len(actual),
        multiplier_p05=mults[len(mults) // 20],
        multiplier_p95=mults[(19 * len(mults)) // 20],
        multiplier_min=mults[0], multiplier_max=mults[-1],
    )


def measure_durability(
    conn,
    *,
    as_of,
    first_season: int,
    last_season: int,
    horizon_games: int = FANTASY_HORIZON_GAMES,
    min_role_weeks: int = COHORT_MIN_ROLE_WEEKS,
    panel: Mapping[int, Mapping[str, PlayerSeason]] | None = None,
    view: base.AsOfView = "historical",
) -> DurabilityMeasurement:
    """Re-derive every number in ``DEFAULT_DURABILITY`` from the database.

    Rule 1: keyword-only ``as_of``, ``view`` threaded. For a completed-season
    measurement bind ``base.latest_truth(measure_durability)`` (see
    ``load_player_histories`` for why that is the correct view for bulk history).

    The cohort is season Y players who held a starting role for at least
    ``min_role_weeks`` weeks of season Y-1, so the first season in the range only
    ever supplies histories, never outcomes.
    """
    seasons = list(range(int(first_season), int(last_season) + 1))
    if panel is None:
        panel = season_panel(conn, as_of=as_of, first_season=first_season,
                             last_season=last_season, view=view)
    cohort = cohort_seasons(panel, min_role_weeks=min_role_weeks)

    positions = tuple(sorted(ROLE_DEPTH))
    cohort_n = {p: sum(1 for ps in cohort if ps.position == p) for p in positions}

    miss_rate, mean_missed, opening, pmf_out = {}, {}, {}, {}
    for p in positions:
        rows = [ps for ps in cohort if ps.position == p]
        if not rows:
            continue
        pairs = [ps.horizon(horizon_games) for ps in rows]
        missed = sum(m for m, _ in pairs)
        games = sum(g for _, g in pairs)
        miss_rate[p] = missed / games if games else 0.0
        mean_missed[p] = missed / len(rows)
        opening[p] = sum(
            1 for ps in rows if ps.game_weeks and ps.game_weeks[0] not in set(ps.played_weeks)
        ) / len(rows)
        hist = [0.0] * (horizon_games + 1)
        for m, _ in pairs:
            hist[min(m, horizon_games)] += 1.0 / len(rows)
        pmf_out[p] = tuple(hist)

    # prior-history rate: the cohort's OWN record in the seasons before its
    # outcome season (the denominator the multiplier is centred on).
    prior_rate = {}
    for p in positions:
        pm = pg = 0
        for ps in cohort:
            if ps.position != p:
                continue
            m, g = prior_record(panel, ps)
            pm += m
            pg += g
        prior_rate[p] = pm / pg if pg else 0.0

    # return hazard by absence duration, pooled; plus the opener cohort
    hazard_hit = [0] * MAX_ABSENCE_DURATION
    hazard_n = [0] * MAX_ABSENCE_DURATION
    blocks: list[int] = []
    open_back = open_n = 0
    open_missed: list[int] = []
    for ps in cohort:
        weeks = ps.game_weeks
        played = set(ps.played_weeks)
        run = 0
        for i, w in enumerate(weeks):
            if w in played:
                if run:
                    blocks.append(run)
                run = 0
                continue
            run += 1
            if i + 1 < len(weeks):
                idx = min(run, MAX_ABSENCE_DURATION) - 1
                hazard_n[idx] += 1
                if weeks[i + 1] in played:
                    hazard_hit[idx] += 1
        if run:
            blocks.append(run)
        if weeks and weeks[0] not in played:
            open_missed.append(ps.missed)
            if len(weeks) > 1:
                open_n += 1
                if weeks[1] in played:
                    open_back += 1

    # the injury-report cross-check: what does `injuries` know about the weeks a
    # cohort player actually missed?
    designations: dict[tuple[str, int, int], str] = {}
    for season in seasons:
        for row in injuries.get_injuries(conn, as_of=as_of, season=season, view=view):
            gsis = row["gsis_id"]
            if gsis:
                designations[(gsis, int(season), int(row["week"]))] = (
                    (row["report_status"] or "").strip().lower()
                )
    covered = {p: [0, 0] for p in positions}
    injury_only = {p: [0, 0] for p in positions}
    presence = {p: [0, 0] for p in positions}
    for ps in cohort:
        if ps.roster_basis == SEASON_ROSTER_EVIDENCE:
            # A season DETECTED through the injury report cannot be used to
            # measure how much the injury report detects — it was selected for
            # carrying a row. Bracketed absences stay in (nothing selected them
            # on the report, and they are the honest bad news for its coverage).
            continue
        p = ps.position
        played = set(ps.played_weeks)
        presence[p][0] += ps.missed
        presence[p][1] += 1
        for w in ps.game_weeks:
            token = designations.get((ps.gsis_id, ps.season, w))
            if token in ("out", "doubtful"):
                injury_only[p][0] += 1
            if w in played:
                continue
            covered[p][1] += 1
            if token in ("out", "doubtful"):
                covered[p][0] += 1
        injury_only[p][1] += 1

    # What the measurement CANNOT see, sized rather than left implicit: a player
    # who held a starting role in Y-1 and then has no Y row at all — no game, and
    # nothing proving a roster spot. He is not in the cohort, so every rate above
    # is a LOWER bound. The upper bound charges each of them a full missed season.
    unprovable: dict[str, int] = {p: 0 for p in positions}
    for season in seasons[1:]:
        prev = panel.get(season - 1) or {}
        current = panel.get(season) or {}
        for gsis, before in prev.items():
            if before.position not in unprovable:
                continue
            if before.role_weeks < min_role_weeks or gsis in current:
                continue
            unprovable[before.position] += 1
    upper = {}
    for p in positions:
        rows = [ps for ps in cohort if ps.position == p]
        if not rows:
            continue
        pairs = [ps.horizon(horizon_games) for ps in rows]
        missed = sum(m for m, _ in pairs) + unprovable[p] * horizon_games
        games = sum(g for _, g in pairs) + unprovable[p] * horizon_games
        upper[p] = missed / games if games else 0.0

    return DurabilityMeasurement(
        horizon_games=horizon_games,
        cohort_n=MappingProxyType(cohort_n),
        season_miss_rate=MappingProxyType(miss_rate),
        mean_games_missed=MappingProxyType(mean_missed),
        opening_absent=MappingProxyType(opening),
        prior_history_rate=MappingProxyType(prior_rate),
        return_hazard=tuple(
            (hit / n if n else 0.0) for hit, n in zip(hazard_hit, hazard_n, strict=True)
        ),
        return_hazard_n=tuple(hazard_n),
        opening_return=(open_back / open_n if open_n else 0.0),
        opening_mean_missed=(sum(open_missed) / len(open_missed) if open_missed else 0.0),
        games_missed_pmf=MappingProxyType(pmf_out),
        injury_report_coverage=MappingProxyType(
            {p: (c[0] / c[1] if c[1] else 0.0) for p, c in covered.items()}
        ),
        injury_only_mean_missed=MappingProxyType(
            {p: (v[0] / v[1] if v[1] else 0.0) for p, v in injury_only.items()}
        ),
        presence_mean_missed=MappingProxyType(
            {p: (v[0] / v[1] if v[1] else 0.0) for p, v in presence.items()}
        ),
        mean_absence_block=(sum(blocks) / len(blocks) if blocks else 0.0),
        seasons=tuple(seasons),
        season_miss_rate_upper=MappingProxyType(upper),
        unprovable_cohort_seasons=sum(unprovable.values()),
        unprovable_by_position=MappingProxyType(unprovable),
    )


# ------------------------------------------------------------------ the book


@dataclass(frozen=True)
class DurabilityBook:
    """Everything a grader needs, loaded once: the prior, the histories, the slate.

    Built by ``load_durability_book``. Holding it is the whole point — the
    history read walks five seasons of snap counts and takes seconds, while
    ``availability_for`` is pure arithmetic on already-loaded state.
    """

    prior: DurabilityPrior
    histories: Mapping[str, PlayerHistory]
    slate: Mapping[str, tuple[int, ...]]
    season: int
    as_of: str
    #: The (first, last) seasons ``histories`` was read over, so every reason line
    #: can name the window that was actually read. It used to be a literal
    #: "(2021-2025)" in the reason text, which stayed put when the caller changed
    #: the window and told the operator a measured-sounding falsehood.
    history_window: tuple[int, int] | None = None
    #: The view the SLATE was read under (the histories are always latest_truth).
    view: base.AsOfView = "historical"

    def game_weeks(self, team: str | None, weeks: Sequence[int]) -> tuple[int, ...]:
        """His club's game weeks inside ``weeks`` — byes removed.

        An unknown club is a refusal, not a full slate: silently handing back
        every week would erase the bye, and a bye erased is a starter seated on
        a week he cannot play (Rule 6's hard sanity check).
        """
        if team is None:
            raise DurabilityError(
                "no club for this player — cannot tell his bye week from a "
                "game week (refusing rather than assuming he plays every week)"
            )
        played = self.slate.get(team)
        if played is None:
            raise DurabilityError(
                f"club {team!r} has no {self.season} schedule at "
                f"as_of={self.as_of} under view={self.view!r} "
                f"({len(self.slate)} clubs in this book's slate). For a PAST "
                f"season every schedules row was retrieved in 2026, so the "
                f"safe-default 'historical' view gates the whole slate out — a "
                f"backtest must pass view='latest_truth' to load_durability_book."
            )
        window = set(int(w) for w in weeks)
        return tuple(w for w in played if w in window)

    def availability_for(
        self,
        player_key: str,
        position: str,
        team: str | None,
        weeks: Sequence[int],
        *,
        gsis_id: str | None = None,
        opening_absent: float | None = None,
    ) -> PlayerAvailability:
        pos = canon_position(position)
        if pos is None:
            raise DurabilityError(
                f"{player_key}: {position!r} is not a league position"
            )
        return player_availability(
            player_key, pos, self.game_weeks(team, weeks), prior=self.prior,
            history=self.histories.get(gsis_id) if gsis_id else None,
            opening_absent=opening_absent,
            # No gsis means he was never looked up. That is a hole in the
            # crosswalk, not a finding about him, and the reasons must say which.
            identified=gsis_id is not None,
            history_window=self.history_window,
        )


#: Floors for ``load_durability_book``. A book is the ONLY object a consumer
#: holds, and an empty one is silent: every row prices at 1.00x with a confident
#: POSITION_PRIOR basis and nothing anywhere says the inputs were missing. These
#: are the SnapshotCollapse / BoardCollapse / CrosswalkCollapse pattern this repo
#: already pays for elsewhere. Live values on this box: 3,796 histories, 32 clubs.
MIN_BOOK_HISTORIES = 500
MIN_BOOK_CLUBS = 28


def load_durability_book(
    conn,
    *,
    as_of,
    season: int,
    prior: DurabilityPrior = DEFAULT_DURABILITY,
    first_history_season: int = 2021,
    last_history_season: int = 2025,
    view: base.AsOfView = "historical",
    min_histories: int = MIN_BOOK_HISTORIES,
    min_clubs: int = MIN_BOOK_CLUBS,
) -> DurabilityBook:
    """One call for a consumer: prior + per-player history + ``season``'s slate.

    The histories are read through ``load_player_histories`` (latest_truth, bulk
    immutable history — see that docstring). The SLATE is read under ``view``,
    defaulting to ``historical``, and that split is deliberate: the schedule is a
    live, mutable decision input for the season being graded, so it must not
    borrow the bulk-history exemption. **A backtest reading a PAST season must
    pass ``view="latest_truth"``** — every schedules row in this database was
    retrieved in 2026, so the default view returns an empty slate for a 2024
    ``as_of`` and every ``availability_for`` call then fails on a message about
    the schedule rather than about the view.

    ``min_histories`` / ``min_clubs`` are collapse floors, not tuning. Set either
    to 0 for a deliberately tiny book (a fixture); do not set them to 0 to make a
    real load stop complaining, because the thing it is complaining about is that
    the model has no data and is about to say nothing is wrong.
    """
    histories = load_player_histories(
        conn, as_of=as_of,
        first_season=first_history_season, last_season=last_history_season,
    )
    slate = team_game_weeks(conn, as_of=as_of, season=int(season), view=view)
    stamp = normalize_as_of(as_of).isoformat()
    if len(histories) < min_histories:
        raise HistoryCollapse(
            f"durability history collapsed: {len(histories)} player records for "
            f"{first_history_season}-{last_history_season} at as_of={stamp}, "
            f"floor {min_histories}. Either the historical backfill has not run "
            f"on this box (`ziggurat ingest backfill --first "
            f"{first_history_season} --last {last_history_season}`) or the window "
            f"contains no completed seasons. Refusing to hand back a book that "
            f"would price every player on the board at 1.00x and call it "
            f"POSITION_PRIOR with no warning."
        )
    if len(slate) < min_clubs:
        raise HistoryCollapse(
            f"{season} slate collapsed: {len(slate)} clubs at as_of={stamp} under "
            f"view={view!r}, floor {min_clubs}. For a past season pass "
            f"view='latest_truth' — the default 'historical' view gates on "
            f"RETRIEVAL time and every schedules row here was retrieved in 2026. "
            f"Refusing to hand back a book whose every availability_for() call "
            f"will fail on a message about the schedule."
        )
    return DurabilityBook(
        prior=prior,
        histories=histories,
        slate=slate,
        season=int(season),
        as_of=stamp,
        history_window=(int(first_history_season), int(last_history_season)),
        view=view,
    )
