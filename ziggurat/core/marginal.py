"""Marginal (roster-context) valuation — the in-season add/drop objective (item 3.2).

WHAT THIS ANSWERS. Global VOR (item 2.1) prices a player against the league. This
module prices him against **your roster**: how much of your remaining-season
starting-lineup total would you actually lose by dropping him, given who else you
have, when their byes fall, and who gets hurt.

THE OBJECTIVE, formally
-----------------------
``W``      the remaining-week window (explicit, never implicit — see ``resolve_weeks``)
``x[p][w]`` house-scored projected points for player ``p`` in week ``w``, priced
            per-week-then-sum through ``scoring.py`` (Rule 2); a missing week is 0.0
``R``      your roster (16 active slots + 1 IR slot; IR occupants are excluded)
``F``      the addable pool — free agents from ``league_player_state``, NOT the
            projection universe (the projection feed is ~1.9x larger than the
            league's, so generating adds from it recommends players you cannot add)
``S``      an availability scenario: each player available or not, for one week
``lineup(K, w, S)`` the best legal starting total from ``K`` in week ``w`` under ``S``
            (``core/lineup.py``; slots QB/RB2/WR2/TE/FLEX/DST/K)

    V(K)          = Σ_{w ∈ W} E_S [ lineup(K, w, S) ]
    marginal(p|R) = V(R) − max over f ∈ F ∪ {∅}, subject to POSITION_CAPS,
                                    of V( (R \\ {p}) ∪ {f} )

The baseline is the **best available free agent**, not the same position, not a
replacement level, and not an empty slot. Leave-one-out (``V(R) − V(R\\{p})``) is
rejected outright: it charges you for an empty mandatory lineup slot that waivers
refill for free, which scores your kicker and your defense as the two least
droppable players on the roster (measured: HOU D/ST 123.4, K ~124). Whole-pool
also makes the drop board and the add board the SAME computation, so they cannot
disagree — item 3.4 consumes the add side and this module owns the drop side.

**marginal(p|R) is allowed to be negative, and negative is the actionable add
signal** ("drop Godwin, add A.J. Brown, gain 2.1"). It is never clamped.

TWO LOAD-BEARING ASSUMPTIONS, stated because they are false in interesting ways
-----------------------------------------------------------------------------
**A1 — STATIC ROSTER.** ``V(K)`` assumes you hold exactly ``K`` for every
remaining week and make no further transactions. You transact weekly. So every
slot whose value is *optionality across weeks* — a second D/ST, a second K, deep
bench — is systematically OVER-valued, because the model cannot know you would
simply stream that slot instead. This is not academic: the weekly projections are
a flat season rate (measured median week-to-week CV: WR 0.90%, TE 0.84%,
QB 1.37%, RB 1.57%) and **D/ST is the only position with real week-to-week
variation (CV 11.98%)**. Under an uncapped best-available baseline that single
fact makes a SECOND DEFENSE the top add on essentially any roster — measured
15 of 16 best-replacements came back ``LA D/ST``. Hence ``POSITION_CAPS`` and
hence ``STREAMED_POSITIONS``. Both are guards, not optimizations.

**A2 — PROJECTIONS ARE CONDITIONAL ON PLAYING.** ``x[p][w]`` is the healthy line,
not a marginal expectation over absence (measured: team RB rushing shares sum to
~100% of a plausible full-team total, starter 69–86%). So availability applies
symmetrically to everyone: the starter is discounted by P(available) and the
backup's contingent value is a genuine addition, with no double-count to correct.

WHAT THIS MODULE DELIBERATELY DOES NOT MODEL (recorded so it is not mistaken for
an oversight): playoff-week MATCHUP STRENGTH for skill players (measured median
tilt +0.00%, |tilt| max 1.4% — indistinguishable from rounding, and a confident
novice-facing sentence built on rounding error is the most dangerous thing this
item could ship); point-scatter distributions (under the correct ex-ante
objective they wash out of add/drop, and ``weekly_stats`` is empty anyway);
playoff-probability weighting (seam shipped as ``playoff_weight``); and injuries
to players on OTHER rosters (the scenario set is your roster — a handcuff whose
starter you do not own says so in its reasons rather than being silently priced).

Standing rules: Rule 1 — every accessor here is keyword-only ``as_of`` with no
default and threads ``view`` straight into the underlying accessor. Rule 2 — no
scoring constant lives here; every point comes from ``valuation.weekly_lines``,
which prices through ``scoring.score``. Rule 6 — every ranked row ships plain
reasons, every prior is quoted with its source and sample size and the word
"hypothesis", and players that cannot be priced are reported as unpriceable
rather than floor-ranked.
"""

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from ziggurat.core import scoring
from ziggurat.core.lineup import active_players, fill_lineup
from ziggurat.core.valuation import (
    DEFAULT_ROSTER,
    RosterStructure,
    build_valuation,
    canon_position,
    weekly_lines,
)
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import base, projections, schedules
from ziggurat.league import state as league_state

# --------------------------------------------------------------------- constants

# Position caps applied to the roster AFTER a candidate swap. DST/K are hard 1:
# a second of either is only ever worth anything under assumption A1 (see the
# module docstring), and item 3.5 owns streaming them. The skill caps are loose
# anti-hoarding limits — they exist so the scan cannot propose a 9-RB roster, not
# to express strategy.
POSITION_CAPS: Mapping[str, int] = MappingProxyType(
    {"QB": 3, "RB": 8, "WR": 8, "TE": 3, "DST": 1, "K": 1}
)

# Valued on a CURRENT-WEEK horizon only, because you stream them weekly. Without
# this, 3.2 prices a kicker over 17 weeks while 3.5 recommends replacing him this
# Thursday, and the two modules contradict each other with no error anywhere.
STREAMED_POSITIONS = frozenset({"DST", "K"})

# Acquisition classification — the ONE place the waiver-vs-FCFS decision is made
# (item 3.4 audit F8). Consumed by BOTH the drop-board reason here and waiver.py's
# claim planner, so the two can never disagree about the same player. Only the two
# known ESPN pool tokens map; ANYTHING else (a leaked 'ONTEAM' from state.py's
# conflict path, or a differently-spelled token) is UNKNOWN -> "verify", never a
# silent free-agent default.
ACQ_WAIVER = "WAIVER"          # roster_status 'WAIVERS' -> a queued, priority-ordered claim
ACQ_FREE_AGENT = "FREE_AGENT"  # roster_status 'FREEAGENT' -> a first-come grab
ACQ_UNKNOWN = "UNKNOWN"        # anything else -> verify in the ESPN app


def classify_acquisition(roster_status: str | None) -> str:
    """Map an ESPN ``roster_status`` token to how you acquire the player (F8)."""
    tok = str(roster_status or "").strip().upper()
    if tok == "WAIVERS":
        return ACQ_WAIVER
    if tok == "FREEAGENT":
        return ACQ_FREE_AGENT
    return ACQ_UNKNOWN

# Fantasy playoff weeks in this league (regular season is 14 weeks; 6 of 10 teams
# make the playoffs). Reported as a separate subtotal, never blended.
PLAYOFF_WEEKS = frozenset({15, 16, 17})

# Ties in ``marginal_points`` are REAL and common: any player who never reaches
# the lineup in any scenario contributes exactly 0, so a whole cohort collapses to
# the same number. Anything inside this band is a tie, broken by the stated ladder.
TIE_BAND = 1e-6

# The staleness banner shouts past this many days between the data's pull date and
# the decision date. A July projection pricing a November lineup is Rule-1
# INVISIBLE — that snapshot genuinely is the newest thing at or before as_of.
STALE_BANNER_DAYS = 7

# Candidate-pool pruning (per position, by remaining-window points), plus the top
# few per (position, bye week) so a candidate that wins purely on bye TIMING is
# never pruned away. Set to None to scan the whole pool.
DEFAULT_POOL_LIMIT = 30
_BYE_KEEP = 3

# How much of the window a player's projections must actually cover before this
# module is willing to price him. This is NOT a tuning knob, it is a guard against
# a measured trap: the feed publishes a BYE-SHAPED row (team set, opponent NULL,
# every stat NULL) both for a real bye and for a player it has no forecast for, so
# "missing" and "worth zero" are indistinguishable from the points alone. Measured
# on the live 2026 feed over weeks 1-17: 525 identities cover 16 of 16 playable
# weeks, 4 cover 15, and exactly 2 cover ONE — one of which is A.J. Brown at 99.3%
# owned, who priced as the #1 drop on a full-season board with no disclosure at
# all. Any floor between those clusters separates them; 0.75 is set well clear of
# the 15/16 = 0.94 legitimate cluster.
COVERAGE_FLOOR = 0.75

# How many SIMULTANEOUS absences the reported numbers enumerate. The search
# estimator stays at 1 (it has to: the scan is O(roster x pool)), but one-out
# truncation is a tolerable +1.9% on the LEVEL V(K) and 2-3x on the DIFFERENCE
# that prices a bench body — measured on a live post-draft roster over weeks 8-17,
# a deep RB priced -1.28 at depth 1, -2.38 at 2, -2.97 at 3, against -3.25 under
# full enumeration of all 2^15 scenarios. Depth 3 is where the cost of the
# reporting pass is still a couple of seconds.
#
# It has to be a DETERMINISTIC estimator rather than Monte Carlo, even though MC
# is unbiased and costs about the same: two genuinely identical bench players must
# price identically, or the exact-tie band disappears and the stated tiebreak
# ladder is silently replaced by sampling noise.
REPORT_DEPTH = 3
REPORT_SEED = 7

_HANDCUFF_POSITIONS = frozenset({"QB", "RB", "TE"})


class WeekResolutionError(RuntimeError):
    """The remaining-week window could not be determined and was NOT guessed.

    ``weeks=None`` must never silently fall through to a full season: in-season
    that prices already-played weeks into every board and produces a
    wrong-but-plausible answer the operator cannot smell (Rule 6).
    """


# ------------------------------------------------------------ availability model


@dataclass(frozen=True)
class AvailabilityModel:
    """P(a player is unavailable in a given week) — a LABELED HYPOTHESIS.

    Every rate here was calibrated on nflverse history pulled over the network
    during the item-3.2 recon, NOT from this database and NOT validated on 2026.
    ``label`` is quoted verbatim in the reason strings (Rule 6, operator decision
    2026-07-24: priors ship as labeled hypotheses with source and n, never as
    bare numbers).

    Base rates use the "this player is currently the starter; what is P(he misses
    next week)" cohort (probe 3), because that is the decision the operator faces.
    Two other cohorts measured 16.0%/19.0% for QB; they are not contradictions,
    they are different questions (ex-post usage rank, and any zero-participation
    week).

    The season is not flat — measured miss rates climb monotonically from Week 2
    (QB 1.9%, RB 4.5%, WR 4.7%) to the fantasy playoff window (QB 32.2%, RB 24.9%,
    WR 22.8%, TE 19.3%, K 16.4%). ``bucket_multiplier`` carries that shape.
    """

    base_rate: Mapping[str, float]
    bucket_multiplier: Mapping[str, float]
    questionable_rate: Mapping[str, float]
    hard_out_statuses: frozenset[str]
    absence_curve: tuple[float, ...]
    long_term_statuses: frozenset[str]
    cohort: str
    label: str
    source: str

    def bucket(self, week: int) -> str:
        if week in PLAYOFF_WEEKS:
            return "playoff"
        return "early" if week <= 6 else "mid"

    def base_p_out(self, position: str, week: int) -> float:
        rate = self.base_rate.get(position, 0.0)
        return min(1.0, rate * self.bucket_multiplier.get(self.bucket(week), 1.0))

    def p_out(self, position: str, week: int, *, status: str | None = None,
              live_status: bool = False, weeks_since: int = 0) -> float:
        """P(unavailable) for one player-week.

        ``live_status`` is False before the week-1 boundary, where an ESPN status
        is a roster tag and not a game designation (see ``live_status_from``).

        ``weeks_since`` is how many weeks past the designation ``week`` is. It is
        the fix for a measured defect: an ESPN OUT or INJURY_RESERVE tag used to
        cost the player exactly ONE week and then evaporate, so a player on
        injured reserve was modelled ~91% likely to play in every later week.
        A hard-out designation is an ABSENCE EPISODE, not a game: after an Out
        week only 28.8% are back at W+1, 46.8% at W+2, 54.5% at W+3, plateauing
        near 62% — ~38% never return that season. ``absence_curve`` carries that
        measured ladder, floored below by the position's own base rate (a player
        cannot become safer than a healthy one by being hurt).
        """
        base = self.base_p_out(position, week)
        if not live_status or status is None:
            return base
        token = str(status).strip().upper()
        if token in self.hard_out_statuses:
            idx = min(max(int(weeks_since), 0), len(self.absence_curve) - 1)
            return max(base, self.absence_curve[idx])
        if token == "QUESTIONABLE" and weeks_since == 0:
            # Questionable is a THIS-WEEK game designation and resolves weekly;
            # it says nothing about week N+1, so it does not propagate forward.
            return max(base, self.questionable_rate.get(position, 0.27))
        return base

    def describe(self, position: str, weeks: Sequence[int]) -> str:
        """The reason-string form: the rate(s) actually used across the WHOLE
        window, plus the cohort the number came from.

        Quoting a single week understates the model's own assumption: the miss
        rate climbs through the season, so a board that starts in week 4 was
        priced at 1.45x that rate in weeks 15-17.
        """
        rates = {w: self.base_p_out(position, w) for w in weeks}
        lo, hi = min(rates.values()), max(rates.values())
        head = (f"assumed {100 * lo:.0f}%/wk chance he sits" if lo == hi
                else f"assumed {100 * lo:.0f}-{100 * hi:.0f}%/wk chance he sits")
        parts = []
        for name in ("early", "mid", "playoff"):
            ws = [w for w in weeks if self.bucket(w) == name]
            if ws:
                parts.append(f"{100 * self.base_p_out(position, ws[0]):.0f}% in "
                             f"{'weeks ' if len(ws) > 1 else 'week '}"
                             f"{min(ws)}{f'-{max(ws)}' if len(ws) > 1 else ''}")
        spread = f" ({', '.join(parts)})" if len(parts) > 1 else ""
        return f"{head}{spread} — {self.label}; {self.cohort}"

    def absence_note(self, status: str | None, weeks: Sequence[int]) -> list[str]:
        """The mandatory disclosure when ESPN has this player tagged. Says the
        designation out loud and states exactly what the board did with it —
        Rule 6: an assumption the operator cannot see is an assumption he cannot
        challenge, and holding a season-ending IR player through the playoffs is
        the most expensive version of that."""
        if status is None:
            return []
        token = str(status).strip().upper()
        if token not in self.hard_out_statuses:
            return []
        ladder = ", ".join(
            f"{100 * (1.0 - self.absence_curve[i]):.0f}% at week {weeks[0] + i}"
            for i in range(1, min(len(self.absence_curve), len(weeks)))
        )
        out = [
            f"ESPN lists him {token}. This board treats that as an ABSENCE EPISODE, "
            f"not one missed game: he is OUT in week {weeks[0]}, then back with "
            f"probability {ladder or 'the measured plateau'}, plateauing near 62% "
            f"({self.label}) — about 38% of Out-designated players never return "
            f"that season."
        ]
        if token in self.long_term_statuses:
            out.append(
                f"{token} is a STRONGER signal than a weekly Out and the return "
                f"curve above was NOT fitted to it. If he is done for the year this "
                f"board still credits him roughly a third of his remaining value — "
                f"check his actual timeline before you keep him over a live body."
            )
        return out


DEFAULT_AVAILABILITY = AvailabilityModel(
    # probe-3 cohort: tier-1 starter, conditioned on having started the reference
    # week. TE is inflated by a measured 52.6% false-positive rate in the naive
    # absence definition. D/ST never "sits" — a team defense always plays.
    base_rate=MappingProxyType(
        {"QB": 0.075, "RB": 0.09, "WR": 0.08, "TE": 0.09, "K": 0.03, "DST": 0.0}
    ),
    # Ratio transfer WITHIN one cohort (probe 2, whose season-wide and
    # playoff-window rates are both measured): playoff/season = QB 1.69, RB 1.43,
    # WR 1.31, TE 1.39 -> pooled 1.45. The early figure is a deliberately
    # conservative floor: the measured Week-2 rates are 0.10-0.27x the season
    # average, but weeks 1-6 pooled is not Week 2, so 0.70 rather than 0.2.
    bucket_multiplier=MappingProxyType({"early": 0.70, "mid": 1.00, "playoff": 1.45}),
    # Questionable is the only genuinely stochastic designation and it is strongly
    # role-dependent: starters 21-24% (RB/TE/WR) vs non-starters 36-42%, with QB an
    # outlier at 47-62%. The pooled 27.1% roughly DOUBLES the discount on exactly
    # the players who dominate roster value, so the starter-conditioned rate is
    # used. It also drifted ~10pp across 2021-2025, so it is not a safe long-run
    # literal — re-fit it in Phase 4.
    questionable_rate=MappingProxyType(
        {"QB": 0.50, "RB": 0.22, "WR": 0.22, "TE": 0.22, "K": 0.22, "DST": 0.0}
    ),
    # Out (P(absent) 0.996, n=278) and Doubtful (0.986, n=73) are a GATE, not a
    # probability. INJURY_RESERVE and SUSPENSION likewise.
    hard_out_statuses=frozenset(
        {"OUT", "DOUBTFUL", "INJURY_RESERVE", "IR", "SUSPENSION", "NOT_ACTIVE"}
    ),
    # P(still absent), indexed by weeks since the designation. Index 0 is the
    # designation week itself (a GATE: Out is P(absent) 0.996 over n=278, Doubtful
    # 0.986 over n=73). The tail is the measured return curve: P(plays) 28.8% at
    # W+1, 46.8% at W+2, 54.5% at W+3, plateauing near 62%.
    absence_curve=(1.0, 0.712, 0.532, 0.455, 0.380),
    # Designations that mean "a stretch", not "a game". The return curve above was
    # fitted on Out weeks, not on these — so they get the curve AND a reason line
    # saying the curve does not really cover them.
    long_term_statuses=frozenset({"INJURY_RESERVE", "IR", "SUSPENSION", "NOT_ACTIVE"}),
    # NOT a sample size. The probe-3 availability table carries no n of its own, and
    # the four integers that used to sit here ({QB 101, RB 103, WR 36, TE 116}) were
    # the HANDCUFF event study's pair counts, lifted from a different measurement —
    # a wrong n is worse than no n, because it looks checkable and is not.
    cohort="cohort: currently-starting players, conditioned on having started the "
           "previous week (recon probe 3); the rate has no published sample size",
    label="hypothesis: nflverse 2021-2025, not fitted to 2026 data",
    source="item 3.2 recon probe 3 (2026-07-24)",
)


# ---------------------------------------------------------------- handcuff model


@dataclass(frozen=True)
class HandcuffModel:
    """The contingent-value (handcuff) uplift — also a LABELED HYPOTHESIS.

    ``uplift`` is the measured WITHIN-PAIR gain in house points: the same backup's
    own starter-out weeks versus his own starter-in weeks, which controls for
    player quality. Two independent replications on disjoint seasons and two
    different depth-chart schemas agreed on the ordering QB >> RB > TE >> WR ~ 0.

    **WR is deliberately absent, and that absence is the point.** Measured WR
    uplift is -0.14 (95% CI [-2.02, +1.75], 47% of pairs negative) and -0.16 on the
    2025 replication: WR opportunity does not concentrate when a WR1 sits, and a
    "WR2" is a second STARTER, not a bench handcuff. A position-agnostic handcuff
    bonus would systematically overvalue backup WRs — the largest slice of the free
    agent pool, 371 of 1,026 — and, because drops rank by LOWEST marginal value,
    would protect worthless WR4s while dropping genuine RB lottery tickets. That is
    precisely the inversion this item exists to prevent.

    D/ST and K get no coupling either, and that gate is what keeps this Rule-2 safe:
    offense scoring is linear, so adding raw points to a projection is sound, but
    the D/ST brackets are NOT linear and adding points outside ``scoring.py`` would
    be wrong. A test pins that coupling is never applied to DST/K.
    """

    uplift: Mapping[str, float]
    correlation: Mapping[str, float]
    pairs_n: Mapping[str, int]
    ci: Mapping[str, tuple[float, float]]
    workload_spread: Mapping[str, tuple[float, float]]   # (committee, bellcow)
    label: str
    source: str

    def uplift_for(self, position: str) -> float:
        return self.uplift.get(position, 0.0) if position in _HANDCUFF_POSITIONS else 0.0


DEFAULT_HANDCUFFS = HandcuffModel(
    uplift=MappingProxyType({"QB": 8.59, "RB": 4.43, "TE": 1.66}),
    correlation=MappingProxyType({"QB": -0.42, "RB": -0.16, "TE": -0.07}),
    pairs_n=MappingProxyType({"QB": 101, "RB": 103, "TE": 116}),
    ci=MappingProxyType(
        {"QB": (7.08, 10.10), "RB": (2.91, 5.96), "TE": (1.65, 4.02)}
    ),
    # Behind a bellcow (>=15 carries/game) the RB uplift is +6.44; behind a
    # committee back (<10/game) it is +1.56 — a 4x spread this module cannot
    # resolve, because snap/carry history is not ingested. Said out loud in the
    # reasons rather than hidden in the average.
    workload_spread=MappingProxyType({"RB": (1.56, 6.44)}),
    label="hypothesis: nflverse 2021-2024 within-pair event study, replicated on 2025",
    source="item 3.2 recon probe 3 (2026-07-24)",
)


# ------------------------------------------------------------------ output rows


@dataclass(frozen=True)
class MarginalRow:
    """One roster player's marginal value, decomposed and explained."""

    player_key: str
    player: str
    position: str
    team: str | None
    espn_id: str | None
    marginal_points: float          # THE number; may be negative
    lineup_component: float         # healthy weeks, nobody's bye
    bye_component: float            # weeks where he or his replacement is on bye
    contingent_component: float     # weeks where somebody on your roster is out
    playoff_subtotal: float         # weeks 15-17, reported not weighted
    weeks_started: int              # weeks he seats in the all-healthy lineup
    weeks_total: int
    horizon_weeks: int              # weeks this row's value covers (1 for DST/K)
    best_replacement: str | None
    replacement_status: str | None  # 'FREEAGENT' | 'WAIVERS' (they are not the same)
    tiebreak_rung: str | None
    unvalued: bool
    reasons: tuple[str, ...]
    weeks_projected: int = 0        # window weeks the feed actually forecasts
    weeks_projectable: int = 0      # window weeks his team plays

    @property
    def per_week(self) -> float:
        """``marginal_points`` over its own horizon. The board mixes a ONE-WEEK
        streamed number with a whole-season one; this is what makes them
        comparable at a glance."""
        return self.marginal_points / self.horizon_weeks if self.horizon_weeks else 0.0


@dataclass(frozen=True)
class SwapRow:
    """One (add, drop) pair and what it is worth. Item 3.4 owns presentation,
    claim planning, and roster legality — this is the data it stands on."""

    add: str
    drop: str
    gain: float
    add_position: str
    drop_position: str
    add_status: str | None
    add_startable_this_week: bool
    horizon_weeks: int              # 1 for a streamed slot, else the whole window
    reasons: tuple[str, ...]
    drop_unpriceable: bool = False  # the drop side had no usable projection
    # Identity, so item 3.4 joins the claim on the ESPN id, not the display name —
    # two different free agents can share a name (F3). Additive: defaults keep
    # existing SwapRow constructors and the marginal suite unchanged.
    add_espn_id: str | None = None
    drop_espn_id: str | None = None


@dataclass(frozen=True)
class ByeMap:
    """team -> bye week, derived over the week span actually present in the data.

    ``unknown`` is NOT "has no bye": it is "this snapshot's week span does not
    contain his bye". Deriving over weeks 10-17 returns 16 teams, not 32, so an
    ``assert len(byes) == 32`` takes the module down on any narrowed pull.
    """

    byes: Mapping[str, int]
    span: tuple[int, ...]
    source: str
    unknown: frozenset[str]
    note: str = ""

    def bye_of(self, team: str | None) -> int | None:
        return self.byes.get(team) if team else None


@dataclass(frozen=True)
class MarginalBoard:
    """One scan: the drop board, the swap matrix, and everything needed to explain
    them. Both boards come out of the SAME scan by construction — splitting the
    computation is how the add board and the drop board start disagreeing.

    ``swaps`` is LAZY. The matrix is item 3.4's input, not something ``ziggurat
    marginal`` prints, and re-pricing every (add, drop) pair at the reporting depth
    costs more than the whole rest of the scan. Deferring it to first access keeps
    the drop board's cost where the operator's attention is, without letting the
    two boards drift onto different estimators — the same scan state, the same
    depth, computed on demand.
    """

    rows: tuple[MarginalRow, ...]
    _swaps: "_SwapMatrix"
    weeks: tuple[int, ...]
    byes: ByeMap
    model: "ScenarioModel"
    roster_value: float
    notes: tuple[str, ...]
    freshness: tuple[str, ...]
    as_of: str
    season: int

    @property
    def swaps(self) -> tuple[SwapRow, ...]:
        return self._swaps.resolve()

    @property
    def ranked(self) -> tuple[MarginalRow, ...]:
        return tuple(r for r in self.rows if not r.unvalued)

    @property
    def unpriceable(self) -> tuple[MarginalRow, ...]:
        return tuple(r for r in self.rows if r.unvalued)


class _SwapMatrix:
    """The swap matrix's un-re-priced state plus everything needed to finish it."""

    def __init__(self, rows, keys, *, entries, model_full, model_now, depth):
        self._rows, self._keys = rows, keys
        self._ctx = (entries, model_full, model_now, depth)
        self._resolved: tuple[SwapRow, ...] | None = None

    def resolve(self) -> tuple[SwapRow, ...]:
        if self._resolved is None:
            entries, model_full, model_now, depth = self._ctx
            self._resolved = tuple(_reprice_swaps(
                self._rows, self._keys, entries=entries, model_full=model_full,
                model_now=model_now, depth=depth,
            ))
        return self._resolved


# ------------------------------------------------------------- week resolution


def resolve_weeks(
    conn,
    *,
    as_of,
    season,
    weeks: Iterable[int] | None = None,
    last_week: int = 17,
    view: base.AsOfView = "historical",
) -> tuple[int, ...]:
    """The remaining-week window ``W``. RAISES rather than guessing.

    Resolution order (item 3.2 §6.4):
      1. an explicit ``weeks`` argument wins;
      2. ``max(scoring_period)`` from ``league_player_state`` at ``as_of``, if > 0;
      3. the first week that is NOT YET OVER, from the (as-of gated) ``schedules``
         table — i.e. the first week whose LAST gameday is on or after ``as_of``;
      4. otherwise **raise** — naming both failures and the fix.

    Today (2) fails on the live database (``scoring_period`` is 0 on every row),
    which is why the raise exists at all: nothing may fall through to a full
    season and price already-played weeks into a Week-10 decision.

    Step 3 keys on the week's LAST gameday, not its first, and that is not a
    detail. The 2026 week-1 window is 09-09..09-14 and week 2 starts 09-17, so
    "the last week whose first game has kicked off" returns a FINISHED week on
    Tuesday 09-15 and Wednesday 09-16 — the two days CLAUDE.md's cadence is built
    around (waiver claims, post-waiver scan). That would price a played week into
    every board and, worse, hand D/ST and K — which are valued on a current-week
    horizon — last week's matchup.
    """
    if weeks is not None:
        window = tuple(sorted({int(w) for w in weeks}))
        if not window:
            raise WeekResolutionError("weeks was empty — pass at least one week")
        return window

    cutoff = normalize_as_of(as_of)
    rows = league_state.get_player_state(conn, as_of=as_of, season=season, view=view)
    periods = [r["scoring_period"] for r in rows if r["scoring_period"]]
    if periods:
        current = max(int(p) for p in periods)
        if current > 0:
            return tuple(range(min(current, last_week), last_week + 1))

    games = schedules.get_schedule(conn, as_of=as_of, season=season, view=view)
    last_gameday: dict[int, str] = {}
    for g in games:
        if g["game_type"] != "REG" or g["gameday"] is None:
            continue
        wk = int(g["week"])
        day = str(g["gameday"])
        if wk not in last_gameday or day > last_gameday[wk]:
            last_gameday[wk] = day
    if last_gameday:
        unfinished = [w for w, day in last_gameday.items()
                      if normalize_as_of(day) >= cutoff]
        # No unfinished week means the season is over; fall back to the last week
        # rather than inventing one.
        current = min(unfinished) if unfinished else max(last_gameday)
        return tuple(range(min(current, last_week), last_week + 1))

    raise WeekResolutionError(
        "cannot determine the current NFL week and will not guess: "
        "league_player_state.scoring_period is 0 (ESPN reports it only once the "
        "season is under way) and no schedules rows are knowable at "
        f"as_of={as_of}. Pass --from-week explicitly."
    )


def live_status_from(conn, *, as_of, season, view: base.AsOfView = "historical") -> str:
    """The day ESPN's ``injury_status`` starts meaning "will not play this week".

    Before Week 1 it is a ROSTER TAG, not a game designation — today it reads OUT
    on a 71%-owned running back and on a starting WR. Honoring it in preseason
    would zero out healthy studs (Rule 6: the operator cannot smell that). Derived
    from the week-1 kickoff in ``schedules`` when those rows are knowable at
    ``as_of``; otherwise a deliberately LATE fallback, because being late merely
    ignores a real designation for a day while being early corrupts every
    preseason board.
    """
    games = schedules.get_schedule(conn, as_of=as_of, season=season, week=1, view=view)
    days = [str(g["gameday"]) for g in games
            if g["game_type"] == "REG" and g["gameday"] is not None]
    if days:
        return min(days)
    return f"{int(season)}-09-10"


# ------------------------------------------------------------------- bye weeks


def bye_map(
    conn,
    *,
    as_of,
    season,
    source: str = "sleeper_rotowire",
    view: base.AsOfView = "historical",
) -> ByeMap:
    """team -> bye week, preferring ``schedules`` and falling back to projections.

    The projections fallback derives the bye as a COMPLEMENT — a team's bye is the
    week in which it has zero rows carrying an opponent. The naive filter
    (``opponent IS NULL``) is wrong in a way that looks right: the ~2,685
    placeholder identities carry ``team`` populated and ``opponent`` NULL in EVERY
    week, so every team reads as on bye every week (measured: 18 of 18 distinct
    weeks for all 32 teams).

    The bye row shape is also asymmetric by position — a skill player's bye row is
    PRESENT with NULL stats, a D/ST's bye row is ABSENT entirely — so a detector
    keyed on either shape alone mislabels the other class. Deriving once at team
    level and keeping availability as a boolean separate from the points value
    avoids both traps.

    Derived over the FULL week span present in the data, never over ``W``.

    ``schedules`` is preferred but never trusted blindly: it is only authoritative
    while it is COMPLETE. A half-ingested schedules table resolves nothing while a
    correct projections-derived map is sitting in the same database, so whenever
    the schedules map leaves any team unresolved the projections map is derived
    too and the one that resolves MORE teams wins (ties go to schedules). On a
    healthy full-span table the schedules map resolves all 32 and the second
    derivation never runs.
    """
    games = schedules.get_schedule(conn, as_of=as_of, season=season, view=view)
    sched_played: dict[str, set[int]] = {}
    sched_span: set[int] = set()
    for g in games:
        if g["game_type"] != "REG":
            continue
        sched_span.add(int(g["week"]))
        for team in (g["home_team"], g["away_team"]):
            if team is None:
                continue
            sched_played.setdefault(_norm_team(team), set()).add(int(g["week"]))

    def _proj_map() -> tuple[dict[str, set[int]], set[int]]:
        played: dict[str, set[int]] = {}
        span: set[int] = set()
        for r in projections.get_projections(
            conn, as_of=as_of, season=season, source=source, view=view
        ):
            st = r["season_type"]
            if st is None or str(st).strip().lower() != "regular":
                continue
            if r["team"] is None:
                continue
            span.add(int(r["week"]))
            opp = r["opponent"]
            if opp is not None and str(opp).strip():
                played.setdefault(_norm_team(r["team"]), set()).add(int(r["week"]))
        return played, span

    sched = _derive_byes(sched_played, sched_span)
    if sched_played and not sched.unknown:
        return sched                        # complete: no second derivation
    proj = _derive_byes(*_proj_map())
    if not sched_played or len(proj.byes) > len(sched.byes):
        note = ""
        if sched_played:
            note = (
                f"the schedules table resolved only {len(sched.byes)} team byes over "
                f"weeks {min(sched.span, default=0)}-{max(sched.span, default=0)}; the "
                f"projection feed resolved {len(proj.byes)}, so byes came from "
                f"projections. Schedules is probably half-ingested — run the schedules "
                f"ingest."
            )
        return replace(proj, source="projections", note=note)
    return sched


def _derive_byes(played: Mapping[str, set[int]], span: set[int]) -> ByeMap:
    byes: dict[str, int] = {}
    unknown: set[str] = set()
    for team, weeks_played in played.items():
        missing = sorted(span - weeks_played)
        if len(missing) == 1:
            byes[team] = missing[0]
        else:
            # 0 missing weeks means "his bye is outside this span" (deriving over
            # weeks 10-17 leaves 16 teams here); >1 means the span is too sparse
            # to tell. Neither is "has no bye" and neither may be reported as one.
            unknown.add(team)
    return ByeMap(
        byes=byes, span=tuple(sorted(span)), source="schedules",
        unknown=frozenset(unknown),
    )


def _norm_team(raw) -> str | None:
    if raw is None:
        return None
    token = str(raw).strip().upper()
    if not token:
        return None
    return base.TEAM_ALIASES.get(token, token)


# ---------------------------------------------------------- scenario arithmetic


def scenario_weights(p_out: Sequence[tuple[str, float]]) -> tuple[float, tuple[tuple[str, float], ...]]:
    """Normalized independent-Bernoulli weights, truncated at ONE player out.

        w_0 = Π (1 - p_i)      w_i = p_i · Π_{j≠i} (1 - p_j)      then renormalize

    The naive form ``w_0 = 1 - Σ p_i`` is not a distribution: on a real 17-man
    roster ``Σ p_i = 1.290``, giving ``w_0 = -0.290``. A NEGATIVE PROBABILITY, and
    it gets worse in the playoff bucket where the rates are ~1.45x higher.

    Truncation at one-out is a measured approximation, not an exact enumeration:
    against a full Bernoulli Monte Carlo (S=600) the normalized one-out estimator
    ran **+1.4% high** on a thin roster — biased toward over-valuing the roster,
    because the missing 2-or-more-out mass is exactly where bench depth pays. Full
    MC is affordable per call (0.106 s) but not as the estimator: a 16 x 381 swap
    scan is ~11 min under MC versus ~18 s enumerated. It ships as the test oracle
    (``ScenarioModel.value_monte_carlo``) with a pinned tolerance.
    """
    # A certainty is a GATE, not a scenario. p >= 1.0 must leave the w0 product as
    # well as the singles: including it drives w0 to 0, zeroes every single
    # (w0*p/(1-p)), and the norm<=0 guard then returns "nobody is ever out" —
    # silently discarding every OTHER player's injury scenario. ``ScenarioModel``
    # gates hard-outs upstream so it cannot reach that, but this is a documented
    # module-level function and a silent wrong distribution is not an acceptable
    # answer to a bad input.
    risky = [(k, p) for k, p in p_out if 0.0 < p < 1.0]
    w0 = 1.0
    for _key, p in risky:
        w0 *= (1.0 - p)
    singles: list[tuple[str, float]] = []
    for key, p in risky:
        singles.append((key, w0 * p / (1.0 - p)))
    norm = w0 + sum(w for _k, w in singles)
    if norm <= 0.0:
        return 1.0, ()
    return w0 / norm, tuple((k, w / norm) for k, w in singles)


# ------------------------------------------------------------------ the model


@dataclass
class _Entry:
    """One player as the scan sees him (roster or pool), normalized once."""

    key: str
    player: str
    position: str
    team: str | None
    espn_id: str | None
    gsis_id: str | None
    points: Mapping[int, float]
    bye: int | None
    injury_status: str | None
    roster_status: str | None
    percent_owned: float
    lineup_slot: str | None
    on_roster: bool
    unvalued: bool
    weeks_projected: int = 0        # window weeks with a REAL forecast row
    weeks_projectable: int = 0      # window weeks his team actually plays
    no_projection_at_all: bool = False
    pulled_as_of: str | None = None     # HIS newest projection vintage
    stale_projection: str | None = None  # set when his vintage lags the board's


class ScenarioModel:
    """``V(K)`` and the per-week machinery behind it.

    Built once per scan and reused for every candidate roster, which is what makes
    an exhaustive swap scan affordable. Pure arithmetic — no database, no ``as_of``
    (the gate was applied when the entries were read).
    """

    def __init__(
        self,
        entries: Mapping[str, _Entry],
        *,
        weeks: Sequence[int],
        roster_structure: RosterStructure,
        availability: AvailabilityModel,
        handcuffs: HandcuffModel,
        links: Mapping[str, str],
        live_status: bool,
        current_week: int,
        playoff_weight: float = 1.0,
    ):
        self.entries = entries
        self.weeks = tuple(weeks)
        self.rs = roster_structure
        self.availability = availability
        self.handcuffs = handcuffs
        self.links = dict(links)             # starter_key -> backup_key
        self.live_status = live_status
        self.current_week = current_week
        self.playoff_weight = playoff_weight

        self.positions = {k: e.position for k, e in entries.items()}
        # Per week: availability and P(out). Both precomputed — they depend only on
        # the player and the week, never on which roster is being evaluated.
        self._avail: dict[int, dict[str, bool]] = {}
        self._p_out: dict[int, dict[str, float]] = {}
        self._pts: dict[int, dict[str, float]] = {}
        for w in self.weeks:
            av, po, pt = {}, {}, {}
            for k, e in entries.items():
                on_bye = e.bye == w
                # An ESPN hard-out designation propagates FORWARD along the
                # measured return curve. It used to be applied only at
                # ``current_week``, which priced a season-ending INJURY_RESERVE as
                # a single missed game and modelled the player ~91% likely to play
                # in every later week — the single most expensive silent error this
                # module could make.
                p = availability.p_out(
                    e.position, w, status=e.injury_status, live_status=live_status,
                    weeks_since=max(0, w - current_week),
                )
                hard_out = on_bye or p >= 1.0
                av[k] = not hard_out
                po[k] = 0.0 if hard_out else p
                pt[k] = e.points.get(w, 0.0)
            self._avail[w], self._p_out[w], self._pts[w] = av, po, pt
        self._draw_cache: dict[tuple, tuple[float, ...]] = {}

    # -- availability introspection (the reasons layer reads these) -------------

    def available(self, key: str, week: int) -> bool:
        return self._avail[week].get(key, False)

    def points(self, key: str, week: int) -> float:
        return self._pts[week].get(key, 0.0)

    def week_weight(self, week: int) -> float:
        return self.playoff_weight if week in PLAYOFF_WEEKS else 1.0

    # -- the objective ---------------------------------------------------------

    def week_value(self, keys: Sequence[str], week: int) -> tuple[float, float]:
        """``(healthy, injured)`` expected lineup total for one week.

        ``healthy`` is the nobody-out scenario times its normalized weight;
        ``injured`` is the sum over the one-out scenarios. Split so the caller can
        report a decomposition that sums exactly to the total.
        """
        avail_map = self._avail[week]
        pts_all = self._pts[week]
        avail = [k for k in keys if avail_map.get(k, False)]
        if not avail:
            return 0.0, 0.0
        pts = {k: pts_all.get(k, 0.0) for k in avail}
        base_fill = fill_lineup(avail, self.positions, pts, roster=self.rs)

        po = self._p_out[week]
        w0, singles = scenario_weights([(k, po.get(k, 0.0)) for k in avail])
        healthy = w0 * base_fill.total
        if not singles:
            return healthy, 0.0

        avail_set = set(avail)
        injured = 0.0
        for out_key, weight in singles:
            backup = self.links.get(out_key)
            has_backup = backup is not None and backup in avail_set
            if out_key not in base_fill.starters and not has_backup:
                # Removing a player who is not seated cannot change the optimum,
                # and no backup of his is on this roster, so nothing moves.
                injured += weight * base_fill.total
                continue
            sub = [k for k in avail if k != out_key]
            if has_backup:
                sub_pts = dict(pts)
                sub_pts[backup] += self.handcuffs.uplift_for(self.positions[backup])
            else:
                sub_pts = pts
            injured += weight * fill_lineup(
                sub, self.positions, sub_pts, roster=self.rs
            ).total
        return healthy, injured

    def value(self, keys: Iterable[str]) -> float:
        """``V(K)`` — the whole objective, weighted."""
        keys = tuple(keys)
        total = 0.0
        for w in self.weeks:
            healthy, injured = self.week_value(keys, w)
            total += self.week_weight(w) * (healthy + injured)
        return total

    def per_week_value(self, keys: Iterable[str]) -> dict[int, tuple[float, float]]:
        """``week -> (healthy, injured)``, UNWEIGHTED (the search estimator)."""
        keys = tuple(keys)
        return {w: self.week_value(keys, w) for w in self.weeks}

    def week_value_at_depth(self, keys: Sequence[str], week: int, depth: int) -> float:
        """One week's ``E_S[lineup]``, enumerating up to ``depth`` simultaneous
        absences and renormalizing over what was enumerated.

        ``depth=1`` is exactly ``week_value``'s truncation (the search estimator).
        Higher depths are what the board REPORTS: the missing >=2-out mass is
        precisely where bench depth pays, so truncating at one-out prices a deep
        bench body at a third of what he is worth.
        """
        if depth <= 1:
            healthy, injured = self.week_value(keys, week)
            return healthy + injured
        avail_map = self._avail[week]
        pts_all = self._pts[week]
        avail = [k for k in keys if avail_map.get(k, False)]
        if not avail:
            return 0.0
        pts = {k: pts_all.get(k, 0.0) for k in avail}
        po = self._p_out[week]
        risky = [k for k in avail if 0.0 < po.get(k, 0.0) < 1.0]
        w0 = 1.0
        for k in risky:
            w0 *= (1.0 - po[k])
        base_fill = fill_lineup(avail, self.positions, pts, roster=self.rs)
        total, norm = w0 * base_fill.total, w0
        avail_set = set(avail)
        for r in range(1, min(depth, len(risky)) + 1):
            for combo in itertools.combinations(risky, r):
                weight = w0
                for k in combo:
                    weight *= po[k] / (1.0 - po[k])
                norm += weight
                out = set(combo)
                backups = {self.links.get(k) for k in out}
                if out.isdisjoint(base_fill.starters) and not (backups & avail_set):
                    # Nobody seated left and no rostered backup is promoted, so the
                    # optimum cannot move.
                    total += weight * base_fill.total
                    continue
                live = [k for k in avail if k not in out]
                sub_pts = {k: pts[k] for k in live}
                for gone in out:
                    backup = self.links.get(gone)
                    if backup is not None and backup in sub_pts:
                        sub_pts[backup] += self.handcuffs.uplift_for(self.positions[backup])
                total += weight * fill_lineup(
                    live, self.positions, sub_pts, roster=self.rs).total
        return total / norm if norm > 0.0 else base_fill.total

    def value_at_depth(self, keys: Iterable[str], depth: int) -> float:
        keys = tuple(keys)
        return sum(self.week_weight(w) * self.week_value_at_depth(keys, w, depth)
                   for w in self.weeks)

    def per_week_report(self, keys: Iterable[str], *,
                        depth: int) -> dict[int, tuple[float, float]]:
        """``week -> (expected lineup total, all-healthy lineup total)``.

        The decomposition's input. The second term is a MECHANISM quantity, not a
        probability slice: it is what the lineup does when nobody is hurt, so the
        difference between the two is exactly the value that exists only because
        somebody might be.
        """
        keys = tuple(keys)
        return {
            w: (self.week_value_at_depth(keys, w, depth), self.week_healthy(keys, w))
            for w in self.weeks
        }

    # -- the mechanism split ---------------------------------------------------

    def week_healthy(self, keys: Sequence[str], week: int) -> float:
        """The lineup total with NOBODY stochastically out.

        Byes and hard-out designations still apply (they are facts, not
        scenarios); only the injury LOTTERY is switched off. This is the honest
        "starting lineup" term of the decomposition: what changes because of who
        seats, as opposed to what changes because somebody might get hurt.
        """
        avail_map = self._avail[week]
        pts_all = self._pts[week]
        avail = [k for k in keys if avail_map.get(k, False)]
        if not avail:
            return 0.0
        pts = {k: pts_all.get(k, 0.0) for k in avail}
        return fill_lineup(avail, self.positions, pts, roster=self.rs).total

    # -- the reporting estimator ------------------------------------------------

    def _draws(self, week: int, key: str, samples: int, seed: int) -> tuple[float, ...]:
        """A player-week's OWN uniform stream — the common random numbers.

        Keying the stream on (seed, week, player) rather than drawing
        sequentially is what makes this usable for DIFFERENCES: the roster with
        the player and the roster without him see the identical injury draw for
        everybody else, so the noise cancels and only the effect survives.
        """
        ck = (week, key, samples, seed)
        got = self._draw_cache.get(ck)
        if got is None:
            import random

            rng = random.Random(f"{seed}|{week}|{key}")
            got = tuple(rng.random() for _ in range(samples))
            self._draw_cache[ck] = got
        return got

    def week_value_mc(self, keys: Sequence[str], week: int, *,
                      samples: int, seed: int) -> float:
        """One week's ``E_S[lineup]`` by full Bernoulli Monte Carlo — NO
        truncation, so no truncation bias, with common random numbers."""
        avail_map = self._avail[week]
        pts_all = self._pts[week]
        avail = [k for k in keys if avail_map.get(k, False)]
        if not avail:
            return 0.0
        po = self._p_out[week]
        draws = {k: self._draws(week, k, samples, seed) for k in avail}
        acc = 0.0
        for s in range(samples):
            live, out = [], []
            for k in avail:
                (live if draws[k][s] >= po.get(k, 0.0) else out).append(k)
            pts = {k: pts_all.get(k, 0.0) for k in live}
            for gone in out:
                backup = self.links.get(gone)
                if backup is not None and backup in pts:
                    pts[backup] += self.handcuffs.uplift_for(self.positions[backup])
            acc += fill_lineup(live, self.positions, pts, roster=self.rs).total
        return acc / samples

    def value_monte_carlo(self, keys: Iterable[str], *, samples: int = 2000,
                          seed: int = REPORT_SEED) -> float:
        """``V(K)`` under full Bernoulli Monte Carlo — the UNBIASED TEST ORACLE.

        Not an estimator this module ships with: a whole swap scan under MC is
        ~11 minutes against ~18 s enumerated, and — the reason it is not used even
        for the cheap reporting pass — sampling noise destroys the exact ties the
        §1.5 tiebreak ladder exists to resolve. The suite uses it to bound the
        truncation error of what IS shipped.
        """
        keys = tuple(keys)
        return sum(
            self.week_weight(w) * self.week_value_mc(keys, w, samples=samples, seed=seed)
            for w in self.weeks
        )

    def starts_profile(self, keys: Sequence[str]) -> dict[str, tuple[int, int]]:
        """``key -> (weeks seated when everyone is healthy, weeks seated in ANY
        one-out scenario)``. Drives the "never reaches your lineup" reason, which
        must be true in every scenario before it is said out loud."""
        keys = tuple(keys)
        healthy_count: dict[str, int] = dict.fromkeys(keys, 0)
        any_count: dict[str, int] = dict.fromkeys(keys, 0)
        for w in self.weeks:
            avail_map = self._avail[w]
            pts_all = self._pts[w]
            avail = [k for k in keys if avail_map.get(k, False)]
            if not avail:
                continue
            pts = {k: pts_all.get(k, 0.0) for k in avail}
            base_fill = fill_lineup(avail, self.positions, pts, roster=self.rs)
            seated_any = set(base_fill.starters)
            po = self._p_out[w]
            avail_set = set(avail)
            for out_key in avail:
                if po.get(out_key, 0.0) <= 0.0:
                    continue
                sub = [k for k in avail if k != out_key]
                backup = self.links.get(out_key)
                if backup is not None and backup in avail_set and backup != out_key:
                    sub_pts = dict(pts)
                    sub_pts[backup] += self.handcuffs.uplift_for(self.positions[backup])
                else:
                    sub_pts = pts
                seated_any |= fill_lineup(sub, self.positions, sub_pts, roster=self.rs).starters
            for k in base_fill.starters:
                healthy_count[k] += 1
            for k in seated_any:
                any_count[k] += 1
        return {k: (healthy_count[k], any_count[k]) for k in keys}


# ------------------------------------------------------- entries & candidates


def _entry_from_row(row: Mapping, lines, byes: ByeMap, weeks: Sequence[int],
                    *, on_roster: bool) -> _Entry | None:
    """Normalize one ``league_player_state``-shaped row into an ``_Entry``.

    THE ROSTER SEAM. Rows arrive as ``Mapping`` and are ``dict()``-ed at the top
    of the scan, which matters concretely: ``sqlite3.Row`` has no ``.get()`` and
    raises ``IndexError`` (not ``KeyError``) on a missing key, so code written
    against dict fixtures passes in tests and crashes on live data. Normalizing
    once makes a live accessor result and a hand-built synthetic roster the same
    type — which is what lets the post-draft swap to the real roster be a
    zero-code-change swap.
    """
    position = canon_position(row.get("position"))
    if position is None:
        return None
    espn_id = row.get("espn_player_id")
    key = str(espn_id) if espn_id is not None else str(row.get("gsis_id") or row.get("player"))
    team = _norm_team(row.get("pro_team"))
    gsis = row.get("gsis_id")

    if position == "DST":
        proj_key = ("DST", team)
    elif gsis:
        proj_key = ("SKILL", gsis)
    else:
        proj_key = None

    line = lines.get(proj_key) if proj_key is not None else None
    points = {w: line.points.get(w, 0.0) for w in weeks} if line is not None else {}

    # COVERAGE, not the point sum, decides whether he can be priced. The feed's
    # bye-shaped row is byte-identical to its "no forecast" row, so a top-10 WR
    # with one real week and sixteen empty ones summed to a real number, dodged the
    # unpriceable gate, and read as the most droppable player on the roster. See
    # COVERAGE_FLOOR.
    bye = byes.bye_of(team)
    playable = [w for w in weeks if w != bye]
    covered = (
        sorted(set(line.played_weeks) & set(weeks)) if line is not None else []
    )
    thin = bool(playable) and len(covered) < COVERAGE_FLOOR * len(playable)
    unvalued = line is None or sum(points.values()) == 0.0 or thin

    name = row.get("player") or (line.player if line is not None else None) or key
    return _Entry(
        key=key,
        player=str(name),
        position=position,
        team=team,
        espn_id=str(espn_id) if espn_id is not None else None,
        gsis_id=str(gsis) if gsis else None,
        points=points,
        bye=bye,
        injury_status=row.get("injury_status"),
        roster_status=row.get("roster_status"),
        percent_owned=float(row.get("percent_owned") or 0.0),
        lineup_slot=row.get("lineup_slot"),
        on_roster=on_roster,
        unvalued=unvalued,
        weeks_projected=len(covered),
        weeks_projectable=len(playable),
        no_projection_at_all=line is None or not covered,
        pulled_as_of=(max(line.retrieved_as_of)
                      if line is not None and line.retrieved_as_of else None),
    )


def _prune_pool(pool: Sequence[_Entry], limit: int | None) -> list[_Entry]:
    """Keep the top ``limit`` free agents per position, PLUS the top few per
    (position, bye week).

    Pure cost control — the exhaustive scan is O(roster x pool) and the pre-draft
    pool is the whole 1,026-player universe. The bye-week carve-out is the part
    that makes it safe: within a position, a higher projection dominates a lower
    one whenever they share a bye, so the only way a low-projected player can win
    is on bye TIMING, and keeping the best few of every bye week preserves that.
    """
    if limit is None:
        return list(pool)
    ranked: dict[str, list[_Entry]] = {}
    for e in pool:
        ranked.setdefault(e.position, []).append(e)
    keep: dict[str, _Entry] = {}
    for entries in ranked.values():
        entries.sort(key=lambda e: (-sum(e.points.values()), e.key))
        for e in entries[:limit]:
            keep[e.key] = e
        by_bye: dict[int | None, int] = {}
        for e in entries:
            n = by_bye.get(e.bye, 0)
            if n < _BYE_KEEP:
                keep[e.key] = e
                by_bye[e.bye] = n + 1
    return list(keep.values())


def _depth_links(lines, entries: Mapping[str, _Entry],
                 weeks: Sequence[int]) -> tuple[dict[str, str], dict[str, tuple]]:
    """v1 handcuff link: same team + same position, ordered by remaining-window
    house projection. Rank 1 is the starter, rank 2 is his handcuff.

    Zero new data, and verified plausible on the live board (ATL: Bijan 325 ->
    Brian Robinson 85 -> Goodson 8; SF: McCaffrey 294 -> James 49). The 2026
    nflverse depth chart is the v2 upgrade and is deferred with its own item: the
    committed ingester speaks the dead pre-2025 schema, and rewriting it is not
    this item's job.

    Two honest limits, both stated in the reasons rather than smoothed over:
    naming ONE handcuff is right 53.7% of the time (tier-2 or tier-3 covers 77.2%),
    and once a starter is actually out the depth chart PROMOTES the backup, so
    re-reading "who is rank 2 now" mid-absence starts valuing the third man —
    which is why the link is derived from the season-long projection ordering
    (frozen) rather than from a rolling in-season signal.

    Returns ``(starter_key -> backup_key, backup_key -> (starter_name, on_roster))``.
    """
    depth: dict[tuple[str | None, str], list[tuple[float, str, object]]] = {}
    for pkey, line in lines.items():
        if line.position not in _HANDCUFF_POSITIONS or line.team is None:
            continue
        total = sum(line.points.get(w, 0.0) for w in weeks)
        if total <= 0.0:
            continue
        depth.setdefault((line.team, line.position), []).append(
            (total, str(line.player or pkey), pkey)
        )

    by_proj_key = {}
    for k, e in entries.items():
        if e.position == "DST":
            by_proj_key[("DST", e.team)] = k
        elif e.gsis_id:
            by_proj_key[("SKILL", e.gsis_id)] = k

    links: dict[str, str] = {}
    detail: dict[str, tuple] = {}
    for ranked in depth.values():
        ranked.sort(key=lambda t: (-t[0], t[1]))
        if len(ranked) < 2:
            continue
        starter_total, starter_name, starter_pkey = ranked[0]
        backup_total, _backup_name, backup_pkey = ranked[1]
        backup_key = by_proj_key.get(backup_pkey)
        if backup_key is None:
            continue
        starter_key = by_proj_key.get(starter_pkey)
        if starter_key is not None:
            links[starter_key] = backup_key
        detail[backup_key] = (starter_name, starter_key, starter_total, backup_total)
    return links, detail


# --------------------------------------------------------------------- reasons


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _handcuff_reason(entry: _Entry, detail, model: ScenarioModel,
                     handcuffs: HandcuffModel, weeks: Sequence[int],
                     roster_keys: Sequence[str]) -> list[str]:
    """The insurance sentence — the one place a prior is doing visible work, so it
    ships its source, its sample size, its confidence interval and the word
    'hypothesis' (Rule 6, operator decision)."""
    info = detail.get(entry.key)
    if info is None:
        return []
    starter_name, starter_key, _starter_total, _backup_total = info
    uplift = handcuffs.uplift_for(entry.position)
    if uplift <= 0.0:
        return []

    lo, hi = handcuffs.ci.get(entry.position, (0.0, 0.0))
    n = handcuffs.pairs_n.get(entry.position, 0)
    r = handcuffs.correlation.get(entry.position, 0.0)
    covered = [w for w in weeks if model.available(entry.key, w)]
    out = [
        f"insurance on {starter_name}: if {starter_name} misses TIME (not just one "
        f"game — after an Out week only 28.8% of players are back the next week, "
        f"and ~38% never return that season), players in this role have scored "
        f"{uplift:+.1f} house pts more per week than when their starter plays "
        f"({handcuffs.label}, n={n} pairs, 95% CI {lo:+.1f} to {hi:+.1f})",
        f"his weekly points move OPPOSITE {starter_name}'s (r = {r:+.2f}) — that is "
        f"what makes him insurance rather than depth",
        f"covers {len(covered)} of your {len(weeks)} remaining weeks"
        + (f" (he is on {entry.team}'s bye in week {entry.bye})" if entry.bye in weeks else ""),
    ]
    spread = handcuffs.workload_spread.get(entry.position)
    if spread:
        out.append(
            f"this board does not know whether {starter_name} is a bellcow or in a "
            f"committee — the measured uplift runs {spread[0]:+.1f} behind a committee "
            f"back and {spread[1]:+.1f} behind a bellcow, so treat the number as a range"
        )
    # The disclaimer is about ROSTER membership, not about whether the starter
    # happens to appear in the scanned free-agent pool. Gating it on the latter
    # printed the full insurance case, with no caveat, in exactly the situation
    # where the operator is most likely to be holding the handcuff: another manager
    # has just dropped the injured starter, so he IS in the pool.
    if starter_key is None or starter_key not in set(roster_keys):
        out.append(
            f"{starter_name} is NOT on your roster, so this board does not price his "
            f"injuries at all — the number above is what he is worth to you today, not "
            f"what he would be worth if {starter_name} went down"
        )
    out.append(
        "naming one handcuff is right about 54% of the time (the second and third "
        "backup together cover 77%), so treat the link itself as a guess"
    )
    return out


def _row_reasons(
    entry: _Entry,
    *,
    marginal: float,
    lineup_c: float,
    bye_c: float,
    contingent_c: float,
    playoff_subtotal: float,
    starts: tuple[int, int],
    weeks: Sequence[int],
    horizon_weeks: Sequence[int],
    best: _Entry | None,
    model: ScenarioModel,
    availability: AvailabilityModel,
    handcuffs: HandcuffModel,
    handcuff_detail,
    same_pos_ahead: int,
    roster_keys: Sequence[str],
) -> tuple[str, ...]:
    healthy_starts, any_starts = starts
    reasons: list[str] = []

    unavailable_all = all(not model.available(entry.key, w) for w in horizon_weeks)
    if unavailable_all:
        # Blaming positional competition here produced the self-contradictory
        # "you have 0 better DSTs ahead of him, yet he never starts". Name the
        # real cause first — it is the bullet the operator actually reads.
        why = (f"he is on {entry.team}'s bye in week {entry.bye}"
               if entry.bye in horizon_weeks
               else f"ESPN has him tagged {entry.injury_status}")
        reasons.append(
            f"he cannot start at all in "
            f"{_plural(len(horizon_weeks), 'the remaining week')} this board covers — "
            f"{why}"
        )
    elif healthy_starts == 0 and any_starts == 0 and same_pos_ahead > 0:
        reasons.append(
            f"never reaches your starting lineup — you have {_plural(same_pos_ahead, 'better ' + entry.position)} "
            f"ahead of him, in any of your {len(horizon_weeks)} remaining weeks, in any "
            f"single-injury scenario"
        )
    elif healthy_starts == 0 and any_starts == 0:
        reasons.append(
            f"never reaches your starting lineup in any of your "
            f"{_plural(len(horizon_weeks), 'remaining week')} — he never outscores the "
            f"players already seated in the slots he is eligible for"
        )
    elif healthy_starts == 0:
        reasons.append(
            f"never starts when everyone is healthy, but would start in "
            f"{_plural(any_starts, 'week')} if someone ahead of him got hurt"
        )
    else:
        reasons.append(
            f"starts in {healthy_starts} of your "
            f"{_plural(len(horizon_weeks), 'remaining week')} "
            f"({any_starts} counting weeks where an injury opens a slot for him)"
        )

    if best is not None:
        verb = "you would GAIN" if marginal < 0 else "you would LOSE"
        reasons.append(
            f"drop him and add {best.player} ({best.position}) and {verb} "
            f"{abs(marginal):.1f} house pts over {_plural(len(horizon_weeks), 'week')}"
        )
        acq = classify_acquisition(best.roster_status)
        if acq == ACQ_WAIVER:
            reasons.append(
                f"{best.player} is on WAIVERS, not a free agent — that is a claim to "
                f"queue, not a click (item 3.4 plans the claim)"
            )
        elif acq == ACQ_UNKNOWN:
            reasons.append(
                f"{best.player} has an unrecognized roster status "
                f"({best.roster_status or 'none'!r}) — verify in the ESPN app whether "
                f"he is a free agent or a waiver claim before acting"
            )
    else:
        reasons.append(
            f"no legal free-agent replacement was found for him; dropping him costs "
            f"{abs(marginal):.1f} house pts"
        )

    reasons.append(
        f"where that comes from: the weeks he actually starts {lineup_c:+.1f}, bye "
        f"weeks (his or his replacement's) {bye_c:+.1f}, the EXTRA value that only "
        f"exists because somebody might get hurt {contingent_c:+.1f}"
    )

    if entry.bye is not None and entry.bye in horizon_weeks:
        reasons.append(f"his {entry.team} bye is week {entry.bye}")
    elif entry.bye is None:
        reasons.append(
            f"his bye week could not be determined from the data (team {entry.team}) — "
            f"treat the bye part of this number as unknown"
        )
    # The replacement's bye lands in bye_component too, and it can be the LARGEST
    # term on the row. Naming only the dropped player's own bye left a +20.7 "bye
    # weeks" line that nothing in the reasons could account for.
    if (best is not None and best.bye is not None and best.bye in horizon_weeks
            and best.bye != entry.bye):
        reasons.append(
            f"part of that bye figure is your best replacement's: {best.player} is on "
            f"{best.team}'s week-{best.bye} bye, which he cannot cover either"
        )

    playoff_in_window = [w for w in horizon_weeks if w in PLAYOFF_WEEKS]
    if playoff_in_window:
        reasons.append(
            f"weeks {min(playoff_in_window)}-{max(playoff_in_window)} (your fantasy "
            f"playoffs) account for {playoff_subtotal:+.1f} of that — reported "
            f"separately, NOT weighted more heavily"
        )

    if entry.stale_projection is not None:
        reasons.append(
            f"STALE: his projection has not been refreshed since "
            f"{entry.stale_projection}, while the rest of this board has — he may have "
            f"fallen out of the feed (season-ending injury, cut, retired). The feed is "
            f"upserted, never replaced, so his last-known line survives forever"
        )

    if entry.weeks_projectable and entry.weeks_projected < entry.weeks_projectable:
        reasons.append(
            f"HEADS UP: the projection feed only forecasts {entry.weeks_projected} of "
            f"the {entry.weeks_projectable} weeks his team plays in this window; the "
            f"other {entry.weeks_projectable - entry.weeks_projected} are scored as 0, "
            f"so this number is a FLOOR, not an estimate"
        )

    if entry.position in STREAMED_POSITIONS:
        reasons.append(
            f"your {entry.position} is priced on THIS WEEK ONLY, because you stream that "
            f"slot week to week; a second {entry.position} is never considered as an add"
        )
    else:
        reasons.append(availability.describe(entry.position, horizon_weeks))

    if model.live_status:
        reasons.extend(availability.absence_note(entry.injury_status, horizon_weeks))

    handcuff = _handcuff_reason(entry, handcuff_detail, model, handcuffs,
                                horizon_weeks, roster_keys)
    if handcuff:
        reasons.extend(handcuff)
        if contingent_c <= 0.0:
            # The insurance case above is about the ROLE. This sentence is about
            # YOUR roster, and it is the whole point of the item: the identical
            # handcuff at the identical projection is worth holding on a thin
            # roster and worth nothing on a deep one.
            reasons.append(
                f"...but on THIS roster that insurance is worth nothing: he is behind "
                f"{_plural(same_pos_ahead, 'better ' + entry.position)}, so even with a "
                f"starter out he does not reach your lineup"
            )
    return tuple(reasons)


# ------------------------------------------------------------------- the scan


def build_board(
    conn,
    *,
    as_of,
    season,
    roster: Sequence[Mapping],
    weeks: Iterable[int] | None = None,
    last_week: int = 17,
    pool: Sequence[Mapping] | None = None,
    roster_structure: RosterStructure = DEFAULT_ROSTER,
    availability: AvailabilityModel | None = None,
    handcuffs: HandcuffModel | None = None,
    position_caps: Mapping[str, int] = POSITION_CAPS,
    playoff_weight: float = 1.0,
    pool_limit: int | None = DEFAULT_POOL_LIMIT,
    swap_limit: int | None = 200,
    report_depth: int = REPORT_DEPTH,
    source: str = "sleeper_rotowire",
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    view: base.AsOfView = "historical",
    today=None,
) -> MarginalBoard:
    """ONE scan producing the drop board AND the swap matrix (item 3.2).

    Rule 1: ``as_of`` is keyword-only with no default and is threaded into every
    accessor (``weekly_lines`` -> ``get_projections``, ``get_free_agents``,
    ``get_schedule``); this layer never widens the gate. ``today`` is used only
    for the staleness banner (operational metadata, like ``ziggurat ingest
    status``) and is never a substitute for ``as_of``.

    TWO ESTIMATORS, deliberately. The SEARCH (which free agent replaces whom, over
    roster x pool) runs on the cheap one-out truncation — ~18 s enumerated against
    ~11 min under Monte Carlo, which is the whole reason the design chose it. Every
    number the board REPORTS is then re-priced by enumerating up to
    ``report_depth`` simultaneous absences, because the truncation bias is a
    tolerable +1.9% on the level ``V(K)`` and 2-3x on the DIFFERENCE that prices a
    bench body. ``report_depth=1`` collapses the two (tests that want the raw
    search estimator).
    """
    availability = availability or DEFAULT_AVAILABILITY
    handcuffs = handcuffs or DEFAULT_HANDCUFFS
    notes: list[str] = []

    window = resolve_weeks(conn, as_of=as_of, season=season, weeks=weeks,
                           last_week=last_week, view=view)
    byes = bye_map(conn, as_of=as_of, season=season, source=source, view=view)
    lines = weekly_lines(
        conn, as_of=as_of, season=season, weeks=window, source=source,
        rules=rules, view=view,
    )

    roster_rows = [dict(r) for r in roster]
    active = active_players(roster_rows)
    if len(active) < len(roster_rows):
        notes.append(
            f"{len(roster_rows) - len(active)} IR-slotted player(s) excluded from the "
            f"lineup and from the {roster_structure.active_slots}-slot active count"
        )
    if pool is None:
        pool_rows = [dict(r) for r in league_state.get_free_agents(
            conn, as_of=as_of, season=season, view=view)]
    else:
        pool_rows = [dict(r) for r in pool]

    # --- loud degradation on empty inputs (an as-of read of an empty table
    # returns [], not an error, and a complete-looking report with zero signal is
    # exactly what the operator cannot smell).
    if not lines:
        notes.append(
            "NO PROJECTIONS are knowable at this as-of — every player prices at 0 and "
            "this board means nothing. Run `ziggurat ingest run`."
        )
    if not byes.byes:
        notes.append(
            "NO BYE WEEKS could be derived — bye coverage is missing from every number "
            "below."
        )
    if byes.note:
        notes.append(byes.note)
    if not roster_rows:
        notes.append(
            "(no roster rows at this as-of — has the draft happened, and has a sync run?)"
        )
    if not pool_rows:
        notes.append(
            "(no free agents at this as-of — every drop below is priced against NOBODY, "
            "which understates what you would lose)"
        )

    entries: dict[str, _Entry] = {}
    roster_keys: list[str] = []
    for row in active:
        e = _entry_from_row(row, lines, byes, window, on_roster=True)
        if e is None:
            notes.append(f"skipped a roster row with an unreadable position: {row.get('player')}")
            continue
        entries[e.key] = e
        roster_keys.append(e.key)

    pool_entries: list[_Entry] = []
    unpriceable_fas: list[_Entry] = []
    for row in pool_rows:
        e = _entry_from_row(row, lines, byes, window, on_roster=False)
        if e is None or e.key in entries:
            continue
        if e.unvalued:
            unpriceable_fas.append(e)
            continue
        pool_entries.append(e)
    kept = _prune_pool(pool_entries, pool_limit)
    # Rule 6 applies to the ADD side too. The drop board names every roster player
    # it cannot price; the add board used to drop them silently, so "we scanned 168
    # of 359" read as the whole pool when the pool was 866 and 507 of them —
    # including heavily-owned names — had never been considered at all.
    if unpriceable_fas:
        loudest = sorted(unpriceable_fas, key=lambda e: -e.percent_owned)[:3]
        notes.append(
            f"{len(unpriceable_fas)} of the {len(unpriceable_fas) + len(pool_entries)} "
            f"free agents have no usable projection at this as-of and were NOT "
            f"considered as adds (most-owned of them: "
            f"{', '.join(f'{e.player} {e.percent_owned:.0f}%' for e in loudest)}) — "
            f"same honest gap as the CANNOT VALUE block, on the other side of the trade"
        )
    if pool_limit is not None and len(kept) < len(pool_entries):
        notes.append(
            f"free-agent pool scanned: {len(kept)} of the {len(pool_entries)} priceable "
            f"free agents (top {pool_limit} per position by projection, plus the best "
            f"few of every bye week — a deeper name cannot beat one already scanned "
            f"unless it wins on bye timing)"
        )
    for e in kept:
        entries[e.key] = e

    # Per-player staleness: the board-wide banner cannot see one player frozen at
    # a months-old vintage while everybody else refreshed, and the ingester upserts
    # (never replaces the partition), so falling out of the feed is silent.
    newest = max((e.pulled_as_of for e in entries.values() if e.pulled_as_of),
                 default=None)
    if newest is not None:
        for e in entries.values():
            if e.pulled_as_of is None:
                continue
            if (normalize_as_of(newest) - normalize_as_of(e.pulled_as_of)).days \
                    > STALE_BANNER_DAYS:
                e.stale_projection = e.pulled_as_of

    links, handcuff_detail = _depth_links(lines, entries, window)
    live = normalize_as_of(as_of) >= normalize_as_of(
        live_status_from(conn, as_of=as_of, season=season, view=view)
    )
    if not live:
        notes.append(
            "preseason: ESPN injury tags are roster labels this early, not game "
            "designations, so they are IGNORED here (today they read OUT on healthy, "
            "heavily-owned starters)"
        )

    def make_model(weeks_used):
        return ScenarioModel(
            entries, weeks=weeks_used, roster_structure=roster_structure,
            availability=availability, handcuffs=handcuffs, links=links,
            live_status=live, current_week=window[0], playoff_weight=playoff_weight,
        )

    model_full = make_model(window)
    model_now = make_model(window[:1])

    # A permanently empty starting slot dominates EVERY row below it: with no
    # D/ST on the roster, the best add for every single drop candidate is a D/ST,
    # and the board reads as if it were obsessed with defenses. That is the
    # correct answer to the question asked, but a novice reading fifteen rows all
    # saying "add Steelers D/ST" needs to be told why once, in words.
    # Computed over the PRICEABLE roster only: ``fill_lineup`` seats a 0-point
    # player into any otherwise-empty slot (0.0 >= 0.0), so one unprojected body
    # plugged the hole for the purposes of this check and silenced the note — the
    # note being the whole Rule-6 mitigation for the hole in the first place.
    priceable_keys = [k for k in roster_keys if not entries[k].unvalued]
    for slot in _permanent_holes(model_full, priceable_keys):
        notes.append(
            f"YOUR LINEUP HAS AN EMPTY {slot} SLOT IN EVERY REMAINING WEEK. That hole "
            f"is worth more than any other move, so it is the best add on essentially "
            f"every row below. Fill it first, then re-run this."
        )

    rows, matrix = _scan(
        entries=entries,
        roster_keys=roster_keys,
        candidates=[e for e in kept],
        model_full=model_full,
        model_now=model_now,
        window=window,
        position_caps=position_caps,
        availability=availability,
        handcuffs=handcuffs,
        handcuff_detail=handcuff_detail,
        conn=conn,
        as_of=as_of,
        season=season,
        source=source,
        rules=rules,
        view=view,
        swap_limit=swap_limit,
        report_depth=report_depth,
    )

    freshness = _freshness_lines(
        conn, season=season, as_of=as_of, today=today,
        lines=lines, roster_rows=roster_rows, pool_rows=pool_rows,
    )
    notes.append(
        "THIS BOARD ASSUMES YOU MAKE NO OTHER MOVES ALL SEASON. It therefore "
        "OVER-VALUES any slot you would simply refill from waivers — a backup "
        "quarterback, a second defense or kicker, the last bench spot. Treat a "
        "positive number on a bench body as an UPPER BOUND on what he is worth to "
        "you — see 'static roster' in ziggurat/core/marginal.py."
    )
    return MarginalBoard(
        rows=tuple(rows),
        _swaps=matrix,
        weeks=window,
        byes=byes,
        model=model_full,
        roster_value=model_full.value(roster_keys),
        notes=tuple(notes),
        freshness=tuple(freshness),
        as_of=str(as_of),
        season=int(season),
    )


def _permanent_holes(model: ScenarioModel, roster_keys: Sequence[str]) -> list[str]:
    """Starting slots this roster cannot fill in ANY remaining week.

    A slot empty in only some weeks is a bye, which is exactly what the bye
    component prices. A slot empty in EVERY week is a structural hole and needs
    saying out loud (Rule 6).
    """
    if not roster_keys:
        return []
    always_empty: set[str] | None = None
    for week in model.weeks:
        avail = [k for k in roster_keys if model.available(k, week)]
        pts = {k: model.points(k, week) for k in avail}
        empty = set(fill_lineup(avail, model.positions, pts, roster=model.rs).empty_slots)
        always_empty = empty if always_empty is None else (always_empty & empty)
        if not always_empty:
            break
    return sorted(always_empty or ())


def _scan(
    *,
    entries,
    roster_keys,
    candidates,
    model_full,
    model_now,
    window,
    position_caps,
    availability,
    handcuffs,
    handcuff_detail,
    conn,
    as_of,
    season,
    source,
    rules,
    view,
    swap_limit,
    report_depth,
):
    """The exhaustive drop x add scan. Both boards fall out of this one loop.

    Two passes, deliberately: the SEARCH picks each drop's best replacement and
    enumerates the swap matrix on the cheap one-out estimator (O(roster x pool)),
    then a REPORTING pass re-prices only what is printed — 16 rows and the
    retained swaps — at a deeper truncation. See ``build_board``'s docstring for
    why the two cannot be the same estimator.
    """
    roster_set = list(roster_keys)
    counts: dict[str, int] = {}
    for k in roster_set:
        counts[entries[k].position] = counts.get(entries[k].position, 0) + 1

    base_full = model_full.value(roster_set)
    base_now = model_now.value(roster_set)
    # One profile per HORIZON: a streamed row is priced on this week alone, so
    # "starts in 13 of your 1 remaining weeks" is not a sentence anyone can read.
    starts_full = model_full.starts_profile(roster_set)
    starts_now = model_now.starts_profile(roster_set)

    report_full = model_full.per_week_report(roster_set, depth=report_depth)
    report_now = model_now.per_week_report(roster_set, depth=report_depth)

    rows: list[MarginalRow] = []
    swaps: list[SwapRow] = []
    swap_keys: list[tuple[str, str, list[str]]] = []   # (drop_key, add_key, remaining)
    unvalued_rows: list[MarginalRow] = []

    for drop_key in roster_set:
        d = entries[drop_key]
        streamed = d.position in STREAMED_POSITIONS
        model = model_now if streamed else model_full
        base = base_now if streamed else base_full
        report_base = report_now if streamed else report_full
        starts = starts_now if streamed else starts_full
        horizon = model.weeks

        remaining = [k for k in roster_set if k != drop_key]
        after_counts = dict(counts)
        after_counts[d.position] -= 1

        best_value = model.value(remaining)      # the f = EMPTY option
        best: _Entry | None = None
        for f in candidates:
            cap = position_caps.get(f.position)
            if cap is not None and after_counts.get(f.position, 0) + 1 > cap:
                continue
            v = model.value(remaining + [f.key])
            gain = v - base
            if gain > 0.0:
                swaps.append(SwapRow(
                    add=f.player, drop=d.player, gain=gain,
                    add_position=f.position, drop_position=d.position,
                    add_status=f.roster_status,
                    add_startable_this_week=model_now.available(f.key, window[0]),
                    horizon_weeks=len(horizon),
                    drop_unpriceable=d.unvalued,
                    add_espn_id=f.espn_id, drop_espn_id=d.espn_id,
                    reasons=(
                        f"add {f.player} ({f.position}), drop {d.player} "
                        f"({d.position}): {gain:+.1f} house pts over "
                        f"{_plural(len(horizon), 'week')}"
                        + (" — THIS WEEK ONLY, because you stream that slot" if streamed else ""),
                        (f"{f.player} cannot start this week (ruled out / on bye) — "
                         f"this is a hold-for-later add, not a lineup fix")
                        if not model_now.available(f.key, window[0])
                        else f"{f.player} is startable this week",
                    ) + ((
                        f"{d.player} has no usable projection, so this board scored him "
                        f"0 in every week — this gain is an UPPER BOUND and he is "
                        f"probably your cheapest drop, but we could not price him",
                    ) if d.unvalued else ()),
                ))
                swap_keys.append((drop_key, f.key, remaining))
            if v > best_value:
                best_value, best = v, f

        # The row's OWN best replacement always enters the matrix, even if the
        # cheap search estimator scored that pair at or below zero. After
        # re-pricing, `gain == -marginal_points` for this pair by construction, so
        # without it a row can report "drop him and GAIN 1.4" while the swap matrix
        # 3.4 reads contains no such move — the add board and the drop board
        # disagreeing, which is the one thing sharing the scan is meant to prevent.
        if best is not None and (drop_key, best.key) not in {
            (dk, ak) for dk, ak, _r in swap_keys
        }:
            swaps.append(SwapRow(
                add=best.player, drop=d.player, gain=best_value - base,
                add_position=best.position, drop_position=d.position,
                add_status=best.roster_status,
                add_startable_this_week=model_now.available(best.key, window[0]),
                horizon_weeks=len(horizon), drop_unpriceable=d.unvalued,
                add_espn_id=best.espn_id, drop_espn_id=d.espn_id,
                reasons=(
                    f"add {best.player} ({best.position}), drop {d.player} "
                    f"({d.position}): {best_value - base:+.1f} house pts over "
                    f"{_plural(len(horizon), 'week')}"
                    + (" — THIS WEEK ONLY, because you stream that slot" if streamed else ""),
                    f"{best.player} is startable this week"
                    if model_now.available(best.key, window[0])
                    else f"{best.player} cannot start this week (ruled out / on bye) — "
                         f"this is a hold-for-later add, not a lineup fix",
                ),
            ))
            swap_keys.append((drop_key, best.key, remaining))

        # An unpriceable roster player still belongs in the swap matrix above (he
        # is the obviously-correct drop side and item 3.4 must be able to see him),
        # but never in the RANKED board: scoring him 0 in every scenario would rank
        # him top drop for a reason that has nothing to do with football.
        if d.unvalued:
            unvalued_rows.append(MarginalRow(
                player_key=d.key, player=d.player, position=d.position, team=d.team,
                espn_id=d.espn_id, marginal_points=0.0, lineup_component=0.0,
                bye_component=0.0, contingent_component=0.0, playoff_subtotal=0.0,
                weeks_started=0, weeks_total=len(window), horizon_weeks=len(window),
                best_replacement=None,
                replacement_status=None, tiebreak_rung=None, unvalued=True,
                weeks_projected=d.weeks_projected,
                weeks_projectable=d.weeks_projectable,
                reasons=_unpriceable_reasons(d, window),
            ))
            continue

        after_keys = remaining if best is None else remaining + [best.key]
        report_after = model.per_week_report(after_keys, depth=report_depth)

        # THE DECOMPOSITION IS A MECHANISM SPLIT, not a probability-mass split.
        # ``healthy`` here is the lineup with nobody hurt, so the residual is
        # genuinely the value that only exists because somebody might be. The
        # previous split routed w0-weighted mass into "lineup" and everything else
        # into "injury", which told the operator that half of his best running
        # back's value was insurance — for a player with no linked backup at all.
        bye_weeks = {w for w in horizon
                     if d.bye == w or (best is not None and best.bye == w)}
        lineup_c = bye_c = contingent_c = playoff_sub = 0.0
        for w in horizon:
            tb, hb = report_base[w]
            ta, ha = report_after[w]
            weight = model.week_weight(w)
            d_total, d_healthy = (tb - ta), (hb - ha)
            if w in bye_weeks:
                bye_c += weight * d_total
            else:
                lineup_c += weight * d_healthy
                contingent_c += weight * (d_total - d_healthy)
            if w in PLAYOFF_WEEKS:
                # WEIGHTED, like the total it is a share of — otherwise the
                # sentence "weeks 15-17 account for +X of that" stops being true
                # the moment the playoff_weight seam is actually used.
                playoff_sub += weight * d_total
        marginal = lineup_c + bye_c + contingent_c

        same_pos_ahead = sum(
            1 for k in remaining
            if entries[k].position == d.position
            and sum(entries[k].points.values()) > sum(d.points.values())
        )
        rows.append(MarginalRow(
            player_key=d.key, player=d.player, position=d.position, team=d.team,
            espn_id=d.espn_id, marginal_points=marginal, lineup_component=lineup_c,
            bye_component=bye_c, contingent_component=contingent_c,
            playoff_subtotal=playoff_sub,
            weeks_started=starts.get(drop_key, (0, 0))[0], weeks_total=len(horizon),
            horizon_weeks=len(horizon),
            best_replacement=best.player if best is not None else None,
            replacement_status=best.roster_status if best is not None else None,
            tiebreak_rung=None, unvalued=False,
            weeks_projected=d.weeks_projected, weeks_projectable=d.weeks_projectable,
            reasons=_row_reasons(
                d, marginal=marginal, lineup_c=lineup_c, bye_c=bye_c,
                contingent_c=contingent_c, playoff_subtotal=playoff_sub,
                starts=starts.get(drop_key, (0, 0)), weeks=window,
                horizon_weeks=horizon, best=best, model=model,
                availability=availability, handcuffs=handcuffs,
                handcuff_detail=handcuff_detail, same_pos_ahead=same_pos_ahead,
                roster_keys=roster_set,
            ),
        ))

    rows = _apply_tiebreaks(
        rows, entries=entries, conn=conn, as_of=as_of, season=season,
        window=window, source=source, rules=rules, view=view,
    )
    order = sorted(range(len(swaps)), key=lambda i: (-swaps[i].gain, swaps[i].add,
                                                     swaps[i].drop))
    if swap_limit is not None:
        order = order[:swap_limit]
    matrix = _SwapMatrix(
        [swaps[i] for i in order], [swap_keys[i] for i in order],
        entries=entries, model_full=model_full, model_now=model_now,
        depth=report_depth,
    )
    return rows + unvalued_rows, matrix


def _unpriceable_reasons(d: _Entry, window: Sequence[int]) -> tuple[str, ...]:
    """Why a roster player could not be priced — stated specifically, because
    "no projection" and "one projection out of seventeen" are different problems
    and only the second one looks like a real number."""
    if d.no_projection_at_all:
        head = (
            "no projection is available for him at this as-of, so he is NOT scored "
            "and NOT ranked — check this player manually before dropping or keeping him"
        )
    else:
        head = (
            f"the projection feed forecasts only {d.weeks_projected} of the "
            f"{d.weeks_projectable} weeks his team plays in this window, so he is NOT "
            f"scored and NOT ranked. Scoring the missing weeks as 0 would have made a "
            f"fully-owned starter look like your most droppable player — check him "
            f"manually"
        )
    return (
        head,
        "this is an honest gap, not a verdict: a player scored 0 in every scenario "
        "ranks as your top drop for a reason that has nothing to do with football",
    )


def _reprice_swaps(swaps, keys, *, entries, model_full, model_now, depth):
    """Re-price the RETAINED swap rows on the REPORTING estimator, so the add side
    and the drop side quote the same number for the same move — splitting them is
    how the add board and the drop board start disagreeing."""
    if depth <= 1 or not swaps:
        return swaps
    roster = [k for k, e in entries.items() if e.on_roster]
    deep_full = model_full.value_at_depth(roster, depth)
    deep_now = model_now.value_at_depth(roster, depth)
    out = []
    for row, (drop_key, add_key, remaining) in zip(swaps, keys, strict=True):
        streamed = entries[drop_key].position in STREAMED_POSITIONS
        model = model_now if streamed else model_full
        deep_base = deep_now if streamed else deep_full
        gain = model.value_at_depth(list(remaining) + [add_key], depth) - deep_base
        if gain <= 0.0:
            # The cheap search estimator thought this move helped; the unbiased one
            # says it does not. A swap matrix is a list of moves worth making.
            continue
        out.append(replace(
            row, gain=gain,
            reasons=(row.reasons[0].replace(
                f"{row.gain:+.1f} house pts", f"{gain:+.1f} house pts"),) + row.reasons[1:],
        ))
    out.sort(key=lambda s: (-s.gain, s.add, s.drop))
    return out


def _apply_tiebreaks(rows, *, entries, conn, as_of, season, window, source, rules, view):
    """Sort ascending by marginal value, breaking EXACT ties by a stated ladder.

    Ties are not a corner case here: every player who never reaches the lineup in
    any scenario contributes exactly 0, so a whole cohort collapses onto one
    number — which is the same defect (a tied floor decided by sort order) that
    makes median-only ranking dangerous in the first place. The ladder:

      1. contingent value descending — a handcuff outranks a dead bench body;
      2. rest-of-season value over replacement descending;
      3. percent owned descending — the market's own tiebreak, and last.
    """
    if not rows:
        return rows
    ros: dict[str, float] = {}
    val_rows = build_valuation(
        conn, as_of=as_of, season=season, weeks=window, source=source,
        rules=rules, view=view,
    )
    by_gsis = {r.gsis_id: r.vor for r in val_rows if r.gsis_id}
    by_dst = {r.team: r.vor for r in val_rows if r.position == "DST"}
    for row in rows:
        e = entries[row.player_key]
        ros[row.player_key] = (
            by_dst.get(e.team, 0.0) if e.position == "DST" else by_gsis.get(e.gsis_id, 0.0)
        )

    def sort_key(r: MarginalRow):
        # ASCENDING throughout, because this board is sorted most-droppable-first:
        # the LOWER insurance value / rest-of-season value / ownership is the one
        # you drop. (Stated the other way round — as a value ranking — every rung
        # is descending: a handcuff outranks a dead bench body.)
        e = entries[r.player_key]
        return (
            r.marginal_points,
            r.contingent_component,
            ros.get(r.player_key, 0.0),
            e.percent_owned,
            r.player,
        )

    ordered = sorted(rows, key=sort_key)

    # Label only rows that really were inside a tie band, and say which rung broke
    # it — a number the operator cannot reproduce is worse than no number.
    out: list[MarginalRow] = []
    for i, r in enumerate(ordered):
        neighbours = [
            o for j, o in enumerate(ordered)
            if j != i and abs(o.marginal_points - r.marginal_points) < TIE_BAND
        ]
        if not neighbours:
            out.append(r)
            continue
        e = entries[r.player_key]
        # Only claim a rung the neighbours ACTUALLY differ on. The old
        # unconditional `else` told a cohort of identical never-starting bench
        # bodies they had been ranked by ownership when ownership was identical
        # too and the real order was alphabetical — a number the operator cannot
        # reproduce is worse than no number, and so is a reason he cannot.
        if any(abs(o.contingent_component - r.contingent_component) >= TIE_BAND
               for o in neighbours):
            rung = ("the extra value that only exists if somebody on your roster "
                    "gets hurt")
        elif any(abs(ros.get(o.player_key, 0.0) - ros.get(r.player_key, 0.0)) >= TIE_BAND
                 for o in neighbours):
            rung = "rest-of-season value over a replacement-level player"
        elif any(abs(entries[o.player_key].percent_owned - e.percent_owned) >= TIE_BAND
                 for o in neighbours):
            rung = "how widely owned he is across ESPN leagues"
        else:
            rung = ("nothing — they are identical on every tiebreak too, so the order "
                    "is alphabetical and means nothing")
        out.append(replace(
            r,
            tiebreak_rung=rung,
            reasons=r.reasons + (
                f"tied with {_plural(len(neighbours), 'other player')} on starting-lineup "
                f"impact — they are worth exactly the same to your lineup; ranked among "
                f"them by {rung} (his: injury-only value {r.contingent_component:+.1f}, "
                f"owned in {e.percent_owned:.0f}% of ESPN leagues)",
            ),
        ))
    return out


# ------------------------------------------------------------- public wrappers


def build_marginal(conn, *, as_of, season, roster: Sequence[Mapping], **kwargs) -> list[MarginalRow]:
    """The drop board: roster players ascending by marginal value, unpriceable
    players last and flagged. Thin wrapper over :func:`build_board`."""
    return list(build_board(conn, as_of=as_of, season=season, roster=roster, **kwargs).rows)


def build_swaps(conn, *, as_of, season, roster: Sequence[Mapping], **kwargs) -> list[SwapRow]:
    """The swap matrix as DATA. Item 3.4 owns presentation, claim planning and
    roster legality — but the COMPUTATION stays here, because it is literally the
    same scan as the drop board and splitting it is how the add board and the drop
    board start disagreeing."""
    return list(build_board(conn, as_of=as_of, season=season, roster=roster, **kwargs).swaps)


# ------------------------------------------------------------------- staleness


def _freshness_lines(conn, *, season, as_of, today, lines, roster_rows, pool_rows) -> list[str]:
    """The staleness banner. Reads item 3.1b's per-source contract rather than
    inventing its own verdicts.

    This is the one failure mode Rule 1 cannot see: a July projection snapshot
    pricing a November decision carries a perfectly valid ``knowable_as_of`` and
    genuinely IS the newest thing at or before ``as_of``. Nothing errors; the
    number is just wrong.

    The gap is measured off the OLDEST vintage on the board, not the newest. The
    ingester upserts rather than replacing the partition, so a player who falls
    out of the feed (season-ending injury, cut, retired) keeps his last-known rows
    forever and ``select_as_of`` keeps serving them — and warning off the newest
    pull meant a single refreshed row silenced the banner for the whole board.
    """
    out: list[str] = []
    cutoff = normalize_as_of(as_of)

    pulled = sorted({d for line in lines.values() for d in line.retrieved_as_of})
    if pulled:
        gap = (cutoff - normalize_as_of(pulled[0])).days
        newest_gap = (cutoff - normalize_as_of(pulled[-1])).days
        line = f"projections: pulled {pulled[-1]}"
        if len(pulled) > 1:
            line += f" (oldest row in this board: {pulled[0]})"
        line += f" — {_plural(newest_gap, 'day')} before the as-of date {as_of}"
        out.append(line)
        if gap > STALE_BANNER_DAYS:
            out.append(
                f"  WARNING: some of the projections on this board are {gap} days old "
                f"(oldest pull {pulled[0]}). They were made before anything that has "
                f"happened since — injuries, depth-chart changes, trades. A player who "
                f"has fallen out of the feed keeps his last-known line forever. Run "
                f"`ziggurat ingest run` before trusting this board."
            )
    else:
        out.append("projections: NONE readable at this as-of")

    state_days = sorted({
        r.get("retrieved_as_of") for r in (list(roster_rows) + list(pool_rows))
        if r.get("retrieved_as_of")
    })
    if state_days:
        gap = (cutoff - normalize_as_of(state_days[-1])).days
        out.append(
            f"league state: pulled {state_days[-1]} — {_plural(gap, 'day')} before {as_of}"
        )
        if gap > STALE_BANNER_DAYS:
            out.append(
                f"  WARNING: your roster and the free-agent pool are {gap} days stale. "
                f"Run `ziggurat league sync`."
            )
    else:
        out.append("league state: NO snapshot readable at this as-of")

    if today is not None:
        from ziggurat.data.nfl import refresh

        # QUIET_VERDICTS, not a literal tuple restated here (item 3.2c, F-D).
        # `archived` joined the ladder for a COMPLETED season, whose files no
        # longer change — so `ziggurat marginal --season 2023` would otherwise
        # have printed "ingest says projections: archived" as a staleness warning
        # on every past-season run. That is the third time this banner has warned
        # about something that was not a problem, and the refresh module now owns
        # the definition so a fourth verdict cannot reintroduce it here.
        watched = {"projections", "players"}
        for s in refresh.source_freshness(conn, season=season, today=today):
            if s["source"] in watched and s["verdict"] not in refresh.QUIET_VERDICTS:
                age = "never pulled" if s["age_days"] is None else f"{s['age_days']}d old"
                out.append(
                    f"  ingest says {s['source']}: {s['verdict']} ({age})"
                    + ("  [this source cannot be re-pulled — a missed day is gone]"
                       if s["perishable"] else "")
                )
    return out


# --------------------------------------------------------------------- display

_COLUMNS = (
    ("player", "player", 22),
    ("position", "pos", 4),
    ("team", "team", 5),
    ("marginal_points", "value", 8),
    ("per_week", "per wk", 7),
    ("lineup_component", "starts", 8),
    ("bye_component", "byes", 7),
    ("contingent_component", "if hurt", 8),
    ("playoff_subtotal", "wk15-17", 8),
    ("weeks_started", "wks in", 7),
    ("horizon_weeks", "wks", 4),
    ("best_replacement", "best add", 20),
)


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:+.1f}"
    return str(value)


def _table(rows, *, reasons: bool) -> list[str]:
    out = ["  ".join(f"{label:<{width}}" for _, label, width in _COLUMNS),
           "  ".join("-" * width for _, _, width in _COLUMNS)]
    for row in rows:
        out.append("  ".join(
            f"{_fmt(getattr(row, attr)):<{width}}" for attr, _, width in _COLUMNS))
        if reasons:
            out.extend(f"      - {reason}" for reason in row.reasons)
    return out


def format_marginal(board: MarginalBoard | Sequence[MarginalRow], *, top: int | None = None,
                    reasons: bool = False) -> str:
    """Render the drop board (display only — no logic, Rule 3 applies below it).

    Streamed slots print in their OWN block. A K or D/ST row is a one-week number
    and every other row is a whole-season number; sorting them into one column
    under "lowest is most droppable" asks the operator to compare -1.6 over one
    week against -0.5 over fourteen, which is a 44x difference per week presented
    as a 3x one. They are not the same quantity and the board no longer pretends
    they are.
    """
    is_board = isinstance(board, MarginalBoard)
    rows = list(board.rows if is_board else board)
    lines: list[str] = []

    if is_board:
        weeks = board.weeks
        lines.append(
            f"marginal value — season {board.season}, as of {board.as_of}, "
            f"weeks {weeks[0]}-{weeks[-1]} ({_plural(len(weeks), 'week')} remaining)"
        )
        lines.append(
            f"your roster projects {board.roster_value:.1f} house pts over that window "
            f"(byes from {board.byes.source}; {len(board.byes.byes)} teams mapped)"
        )
        lines.extend(board.freshness)
        for note in board.notes:
            lines.append(f"! {note}")
        lines.append("")

    ranked = [r for r in rows if not r.unvalued]
    season_rows = [r for r in ranked if r.position not in STREAMED_POSITIONS]
    streamed_rows = [r for r in ranked if r.position in STREAMED_POSITIONS]
    if top is not None:
        season_rows = season_rows[:top]

    lines.append(
        "drop board — LOWEST value is the most droppable"
        + (f" (all {len(board.weeks)} remaining weeks)" if is_board else "")
    )
    lines.extend(_table(season_rows, reasons=reasons))

    if streamed_rows:
        lines.append("")
        lines.append(
            "streamed slots — priced on THIS WEEK ONLY, because you replace them week "
            "to week (item 3.5 owns streaming). NOT comparable with the season numbers "
            "above except per week."
        )
        lines.extend(_table(streamed_rows, reasons=reasons))

    unpriceable = [r for r in rows if r.unvalued]
    if unpriceable:
        lines.append("")
        lines.append("CANNOT VALUE — verify these manually before dropping or keeping:")
        for row in unpriceable:
            why = ("no projection at all" if row.weeks_projected == 0
                   else f"only {row.weeks_projected} of {row.weeks_projectable} "
                        f"weeks forecast")
            lines.append(f"  {row.player} ({row.position}, {row.team or '-'}) — {why}")
    return "\n".join(lines)


def format_swaps(swaps: Sequence[SwapRow], *, top: int | None = None) -> str:
    """Deliberately minimal: item 3.4 owns swap presentation, claim planning, and
    the roster-legality precheck. This exists so the matrix can be eyeballed."""
    rows = list(swaps)[: top if top is not None else None]
    lines = [f"{'add':<22}  {'drop':<22}  {'gain':>7}  status"]
    for s in rows:
        lines.append(
            f"{s.add:<22}  {s.drop:<22}  {s.gain:>+7.1f}  {s.add_status or '-'}"
            + ("" if s.add_startable_this_week else "  (cannot start this week)")
        )
    return "\n".join(lines)
