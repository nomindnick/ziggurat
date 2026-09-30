"""Weekly starter recommender — the "who do I actually start" call (item 3.5, Module B).

WHAT THIS ANSWERS. The seater (``core/lineup.py``) tells you the highest
projected-points lineup. That is the right answer only when the week is a
coin-flip on total points. It is the WRONG answer when the matchup is lopsided:
a heavy underdog should trade expected points for VARIANCE (you need a ceiling to
win), and a heavy favorite should trade variance for a FLOOR (protect a lead).
This module prices that trade-off against your specific opponent this week and
recommends the lineup that maximises P(win), not E(points).

THE SHAPING FACT, and why the variance model is a labelled hypothesis, not a
measurement of this season. The 2026 projection feed is a flat SEASON RATE
(item 3.2 measured median week-to-week CV ~1% for the skill positions), so a
player's projected points barely move week to week and give NO usable per-week
dispersion. The week-to-week SPREAD that a win-probability model needs had to be
measured off historical realised weekly scoring (nflverse 2021-2025, re-scored
through ``scoring.py``) and frozen as ``DEFAULT_VARIANCE`` — every sigma here is
a HYPOTHESIS with its cohort quoted in the reason text (Rule 6), tunable in
Phase 4. It is deliberately kept OUT of ``scoring.py`` (Rule 2): sigma
parametrises a downstream win-probability model, it is not a scoring quantity.

THE DECISION, formally
----------------------
``mu(L)``   the seated lineup ``L``'s projected house points (through the seater)
``var(L)``  ``Σ_i sigma_i^2 + 2 Σ_{i<j} rho_ij sigma_i sigma_j`` over the seated
            starters; ``rho`` is non-zero ONLY for a QB and a pass-catcher on his
            own NFL team (a labelled "correlated starts" hypothesis)
``mu_opp``  the opponent's deterministic all-healthy best-lineup total (symmetric
            and legible), or an explicit ``opponent_total`` override
``var_opp`` the opponent seated lineup's variance, or a flat league-typical
            fallback when his roster cannot be read

    P(win) = Phi( (mu(L) - mu_opp) / sqrt(var(L) + var_opp) )

We seat the greedy E-points lineup first. If the margin ``mu_greedy - mu_opp`` is
inside a close band we return that lineup VERBATIM (points-for is the league
tiebreaker, so a coin-flip week just maximises points). Otherwise we hill-climb
single legal swaps to maximise the win-probability z-score, capping how much
E(points) a posture move may sacrifice.

THE LOCK FENCE (item 3.13, added 2026-09-15) and why it is a Rule-6 item rather
than an optimisation. ESPN freezes a player's slot at HIS OWN kickoff
(``lineupLocktimeType = INDIVIDUAL_GAME``). Until 3.13 this module had no notion
of that: ``_price_roster`` computed availability as ``has_proj and not on_bye and
not hard_out``, and ``game_locks()`` was read AFTER the search purely to relabel
slots. So on 2026-09-13, through the shipped command, the card benched a rival's
WR who had played the Wednesday opener and banked 5.6 PPR (his IR designation
landed two days later), promoted two bench receivers into slots ESPN had frozen,
and carried the same man at full projection inside ``_opponent_lineup`` — up to
11.3 house points, 13.6 pp of win probability, on that matchup. **Estimated points
recovered: 0.00.** The operator is a novice; he cannot check an instruction he is
physically unable to execute, and a card that tells him to do the impossible is
worse than one that says nothing. Locks now gate AVAILABILITY before the seat
join, locked starters are PINNED into the slot ESPN gave them, and "not seated"
is one taxonomy with four distinct sentences — LOCKED, HARD-OUT, BYE,
UNPRICEABLE — rather than three that sound alike.

Standing rules. Rule 1 — every accessor is keyword-only ``as_of`` with no default
and threads ``view``. Rule 2 — no scoring constant lives here; ``mu`` comes from
``valuation.weekly_lines`` (priced through ``scoring.py``) and sigma is a
dispersion prior, never a scoring number. Rule 3 — the CLI parses/calls/prints.
Rule 6 — a novice cannot smell a wrong lineup, so a seated starter who is on BYE
or ruled OUT is a HARD ERROR in code (``assert_no_illegal_starters``), every row
ships its reasons and driving numbers, and every prior quotes its label. Rule 8 —
permanent module, never imports from ``ziggurat/draft/``.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ziggurat.core import streaming, units
from ziggurat.core.lineup import (
    FLEX_LABEL,
    LineupFill,
    active_players,
    fill_lineup,
)
from ziggurat.core.marginal import (
    DEFAULT_AVAILABILITY,
    WeekResolutionError,
    bye_map,
    live_status_from,
    resolve_weeks,
)
from ziggurat.core.valuation import (
    DEFAULT_ROSTER,
    RosterStructure,
    canon_position,
    weekly_lines,
)
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import base, refresh
from ziggurat.data.nfl.schedules import get_schedule
from ziggurat.league import state as league_state

# --------------------------------------------------------------------- constants

_ET = "America/New_York"

# The gameday timezone. nflverse ``gametime`` is uniformly Eastern (verified in
# weather.py's ingester), so a schedules kickoff is an ET wall-clock time.
_SCHEDULE_TZ = _ET

# Statuses that mean "will not play this week" once ``live_status`` has turned on
# (the same availability boundary marginal gates hard-outs on — imported so the
# lineup card and the drop board can never disagree about who is out). Before the
# week-1 kickoff an ESPN status is a roster tag, not a game designation, so this
# is gated through ``live_status_from`` (a preseason OUT must NOT bench a stud).
HARD_OUT_STATUSES = DEFAULT_AVAILABILITY.hard_out_statuses

# Genuinely stochastic "might not play" designations — the ones that earn a
# contingency plan (a lock-time-ordered fallback), not a bench and not a gamble.
GTD_STATUSES = frozenset({"QUESTIONABLE", "GTD", "GAME_TIME_DECISION"})

# The NFL inactive report drops 90 minutes before kickoff. A Questionable
# starter's status is therefore KNOWN by kickoff-90min — the deadline a "safe
# wait" contingency is timed against.
INACTIVE_REPORT_LEAD = timedelta(minutes=90)

# The staleness banner shouts past this many days between the projection's pull
# date and the decision date (the same constant marginal/waiver/streaming use). A
# July projection pricing a November lineup carries a valid knowable_as_of and is
# Rule-1-invisible.
STALE_BANNER_DAYS = 7

# ESPN roster slot -> our slot base (item 3.13). ESPN locks a player's slot at HIS
# OWN kickoff (``lineupLocktimeType = INDIVIDUAL_GAME``), so once his game has
# started, whatever ESPN has him in IS the lineup — the operator cannot move him
# out and cannot move anyone else in. Anything not in this map (``BE``, ``IR``, an
# unknown label) is not a starting slot and therefore pins nothing.
_ESPN_STARTING_SLOTS = MappingProxyType({
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "FLEX": FLEX_LABEL, "D/ST": "DST", "DST": "DST", "K": "K",
})

# The one sentence the card owes the operator about a locked player's points
# (item 3.13 design point 4). We have no live-score source, so a locked player is
# carried at his PROJECTION. State the tradeoff honestly and do not dress it up:
# on the one live instance measured (2026-09-13, week 1) the projection (16.9) was
# FURTHER from the truth (5.6 realised) than deleting him would have been (0.0).
# This fix buys EXECUTABILITY, not accuracy.
LOCKED_CARRY_LABEL = (
    "a locked player is carried at his PROJECTION, not his live score — this tool "
    "has no live-score feed. On the one live instance measured (2026-09-13, week 1) "
    "the projection was FURTHER from the realised points than dropping him to zero "
    "would have been. This is about showing you a lineup you can actually execute, "
    "NOT about the total being right."
)


class OwnTeamUnresolved(league_state.OwnTeamUnresolved):
    """Alias so callers can catch the lineup module's own-team failure without
    importing the league layer. Same refuse-rather-than-guess semantics."""


class StartabilityError(RuntimeError):
    """A seated starter is on BYE or ruled OUT — a lineup that must never ship.

    Raised by ``assert_no_illegal_starters`` (Rule 6): a wrong starter is
    invisible to a novice operator, so it is a hard error in code, not a warning.
    """


# ------------------------------------------------------------- variance model


@dataclass(frozen=True)
class VarianceModel:
    """Per-week house-point dispersion priors — a LABELLED HYPOTHESIS.

    ``sigma(position, mu) = a + b*mu`` for the skill positions and D/ST (an OLS
    fit of realised weekly-score standard deviation on the mean, per player-season
    / team-season, >=8 REG games, 2021-2025, RE-SCORED through ``scoring.py``);
    kickers get a flat sigma that has NOT YET BEEN FITTED (``weekly_stats``
    gained FG make/distance/miss columns in migration ``013``, item 4.1; the fit
    is a recorded follow-up). ``correlation_qb_passcatcher`` is the one non-zero
    cross-player correlation (a QB and a WR/TE on his own NFL team), an unmeasured
    "correlated starts" hypothesis (the strongest underdog lever).

    None of these are scoring numbers (Rule 2): a sigma parametrises a downstream
    win-probability model, it never re-prices a projection. ``label`` and
    ``cohort`` are quoted verbatim in the reason strings (Rule 6). Frozen
    constants produced once by the measure stage — not a runtime DB read — so no
    accessor and no leakage test attach to them.
    """

    coefficients: Mapping[str, tuple[float, float]]     # position -> (a, b)
    r_squared: Mapping[str, float]
    k_flat_sigma: float
    correlation_qb_passcatcher: float
    opp_flat_sigma: float
    cohort: str
    label: str
    source: str

    def sigma(self, position: str, mu: float) -> float:
        """The player-week dispersion prior. Floored at a small positive so a
        degenerate all-zero lineup cannot divide by a zero variance."""
        pos = canon_position(position) or position
        if pos == "K":
            return self.k_flat_sigma
        ab = self.coefficients.get(pos)
        if ab is None:
            return self.k_flat_sigma
        a, b = ab
        # The fit was on non-negative means; a D/ST can bracket below zero, so
        # clamp the mean into the fitted domain before applying the affine form.
        return max(0.5, a + b * max(0.0, float(mu)))

    def r2(self, position: str) -> float:
        return self.r_squared.get(canon_position(position) or position, 0.0)

    def describe(self, position: str, mu: float, sigma: float) -> str:
        """The reason-string form: the sigma used, its affine form, and the cohort
        it was measured on (Rule 6 — every prior quotes its label + source)."""
        pos = canon_position(position) or position
        if pos == "K":
            form = (f"flat sigma {sigma:.1f} (kicker weekly swing — a hypothesis not yet "
                    "fitted; FG columns landed in migration 013)")
        else:
            a, b = self.coefficients.get(pos, (0.0, 0.0))
            form = (f"sigma {sigma:.1f} = {a:.2f} + {b:.3f} x {mu:.1f} proj "
                    f"(R2={self.r2(pos):.2f})")
        return f"{form} — {self.label}; {self.cohort}"


DEFAULT_VARIANCE = VarianceModel(
    # OLS a, b of weekly house-point stdev on mean, per player-season (skill) /
    # team-season (DST), >=8 REG games 2021-2025, re-scored through scoring.py.
    coefficients=MappingProxyType({
        "QB": (5.4871, 0.1190),
        "RB": (2.2529, 0.3950),
        "WR": (2.1571, 0.4207),
        "TE": (1.4315, 0.5025),
        "DST": (5.2866, 0.1535),
    }),
    # The fit quality per position. QB (0.10) and DST (0.06) are essentially FLAT
    # — the affine form is retained for consistency but their sigma is roughly
    # mean-independent; a flat sigma_QB~7.2 / sigma_DST~6.0-6.4 is equally valid.
    r_squared=MappingProxyType({
        "QB": 0.099, "RB": 0.696, "WR": 0.715, "TE": 0.767, "DST": 0.064,
    }),
    # Kicker sigma is NOT YET FITTED: weekly_stats gained FG make/distance/miss
    # columns in migration 013 (item 4.1), so score_kicker CAN now be exercised
    # on historical K rows — the OLS re-fit is a recorded follow-up. 3.5 = a
    # kicker's ~8-pt week swing on 1-2 makes plus -1/miss; a hypothesis until
    # that fit lands.
    k_flat_sigma=3.5,
    # A QB and a pass-catcher on his own NFL team score together (the same drives
    # produce both). rho=+0.35 is a hypothesis, NOT measured (Phase 4); all other
    # cross-player correlations are 0. It is the strongest underdog "stack" lever.
    correlation_qb_passcatcher=0.35,
    # A full opponent lineup TOTAL has a std of ~17-18 house points; used as the
    # opponent variance only when his roster cannot be read (a labelled fallback).
    opp_flat_sigma=17.5,
    cohort="cohort: nflverse weekly_stats (offense) + team_defense (DST), 2021-2025 "
           "REG only, per-(player|team)-season with >=8 scored games, weekly points "
           "re-scored through ziggurat.core.scoring under HOUSE_RULES (per-week, "
           "never sum-then-score); NOT fitted to 2026 (its projections are flat-rate)",
    label="hypothesis: weekly_house_point_sigma_priors, measured 2021-2025 REG, "
          "per-(player|team)-season, >=8 games",
    source="item 3.5 measure stage (2026-07-26)",
)


# ------------------------------------------------------------------ output rows


@dataclass(frozen=True)
class StarterRec:
    """One seated starter, priced and explained (Rule 6)."""

    slot: str
    player: str
    position: str
    espn_id: str | None
    gsis_id: str | None
    proj_points: float
    sigma: float
    floor: float                    # proj - 1 sigma (a labelled dispersion band)
    ceiling: float                  # proj + 1 sigma
    kickoff: str | None             # ISO ET kickoff, or None if unknown
    injury_status: str | None
    gtd: bool                       # a genuinely game-time-decision starter
    reasons: tuple[str, ...]
    locked: bool = False            # item 3.13: his game has started; slot frozen


@dataclass(frozen=True)
class BenchRec:
    """One benched (available) player and why he sits."""

    player: str
    position: str
    espn_id: str | None
    gsis_id: str | None
    proj_points: float
    sigma: float
    injury_status: str | None
    reasons: tuple[str, ...]
    locked: bool = False            # item 3.13: his game has started; cannot be seated
    has_proj: bool = True           # False -> proj_points 0.0 is NO FORECAST, not a zero


@dataclass(frozen=True)
class GTDContingency:
    """A lock-time-ordered fallback for a game-time-decision starter — NOT a point
    pick. If the starter is ruled out by the inactive-report deadline, a
    same-slot-eligible alternative whose game locks LATER is a "safe wait"."""

    player: str
    slot: str
    position: str
    status: str | None
    status_known_by: str | None     # ISO ET: kickoff - 90 min (inactive report)
    kickoff: str | None
    alternative: str | None
    alternative_kickoff: str | None
    safe_wait: bool
    window_closed: bool              # kickoff has passed the decision clock — historical, not live
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class StarterWatch:
    """A seated starter whose status is unresolved and whose game has not locked —
    the Sunday-morning inactives watch. Sorted ascending by kickoff (act soonest
    first)."""

    player: str
    slot: str
    position: str
    kickoff: str | None
    deadline: str | None            # status_known_by
    status: str | None
    contingency: str | None
    snapshot_vintage: str | None    # the league-state pull this status came from
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LineupRecommendation:
    """The week's start/sit recommendation (item 3.5's Module B deliverable)."""

    posture: str                    # FAVORITE | NEUTRAL | UNDERDOG
    own_projected_total: float
    opponent_total: float | None
    margin: float                   # mu_greedy - mu_opp (the posture driver)
    win_prob: float
    starters: tuple[StarterRec, ...]
    bench: tuple[BenchRec, ...]
    contingencies: tuple[GTDContingency, ...]
    watch_list: tuple[StarterWatch, ...]
    sanity_blocks: tuple[str, ...]  # players removed for bye/OUT (never seated)
    freshness: tuple[str, ...]
    notes: tuple[str, ...]
    as_of: str
    season: int
    week: int
    team_id: int | None
    # item 3.17 d2 — the lock deadline, always present, rendered at the top.
    locks_first: tuple[str, ...] = ()
    # item 3.13 — the LOCKED arm of the not-seated taxonomy.
    locked_notes: tuple[str, ...] = ()
    # --- item 4.7 part 3: the scale of the margin line (DISPLAY ONLY) ----------
    # The standard deviations the win probability ALREADY used: ``own_sigma`` is
    # sqrt(var) of the seated lineup, ``opp_sigma`` the opponent's (None when no
    # opponent was priced), ``opp_sigma_basis`` where his came from. Read back from
    # the computation, never re-derived, so the sentence cannot drift from the
    # number it explains. No seat, order or probability depends on them.
    own_sigma: float | None = None
    opp_sigma: float | None = None
    margin_sigma: float | None = None   # sqrt(var_own + var_opp): the win prob's denominator
    opp_sigma_basis: str = ""
    sigma_provenance: str = ""


# ------------------------------------------------------------- internal seat row


@dataclass
class _Seat:
    """One roster player as the seater/searcher sees him, priced once."""

    key: str
    player: str
    position: str
    team: str | None
    espn_id: str | None
    gsis_id: str | None
    points: float
    sigma: float
    injury_status: str | None
    lineup_slot: str | None
    on_bye: bool
    has_proj: bool
    hard_out: bool
    available: bool
    # --- item 3.13: the lock fence ---------------------------------------
    kickoff: datetime | None = None   # his NFL game's ET kickoff, or None (bye/unknown)
    locked: bool = False              # kickoff <= the `now` DECISION clock
    pin_slot: str | None = None       # locked AND already in an ESPN starting slot
    proj_key: tuple | None = None     # the weekly_lines key he was priced from


def _norm_team(raw) -> str | None:
    if raw is None:
        return None
    token = str(raw).strip().upper()
    if not token:
        return None
    return base.TEAM_ALIASES.get(token, token)


def _price_roster(
    rows: Sequence[Mapping],
    lines: Mapping,
    *,
    week: int,
    byes,
    variance: VarianceModel,
    live_status: bool,
    apply_hard_out: bool,
    locks: Mapping[str, datetime] | None = None,
    now: datetime | None = None,
) -> dict[str, _Seat]:
    """THE SHARED SEAT JOIN. Roster rows -> priced ``_Seat`` map, keyed on the same
    coverage discipline for BOTH your roster and the opponent's: a week is
    priceable only when the projection feed actually forecast a game that week
    (``played_weeks``), NEVER when the point sum is non-zero — a bye row and a
    "no forecast" row are byte-identical (item 3.2, ``WeeklyLine.played_weeks``).

    ``apply_hard_out`` gates whether an ESPN OUT tag benches the player: True for
    your own roster (you must not start someone ruled out), False for the opponent
    (his lineup is priced all-healthy — symmetric and legible, item 3.5 design).

    ``locks`` + ``now`` are the item-3.13 LOCK FENCE, and they are applied
    IDENTICALLY on both sides. ``now`` is the DECISION clock (the same one the GTD
    ladder uses), deliberately NOT ``as_of`` — ``as_of`` gates what we may know,
    ``now`` decides what the operator can still do. A player whose game has kicked
    off is ``locked``: ESPN froze his slot at his own kickoff, so he can neither be
    benched nor replaced, and a bench body whose game has started can no longer be
    moved INTO the lineup. A locked player already in a starting slot carries
    ``pin_slot``; the seater pins him there rather than re-solving around him."""
    seats: dict[str, _Seat] = {}
    for row in rows:
        row = dict(row)
        position = canon_position(row.get("position"))
        if position is None:
            continue
        espn_id = row.get("espn_player_id")
        gsis = row.get("gsis_id")
        key = str(espn_id) if espn_id is not None else str(gsis or row.get("player"))
        team = _norm_team(row.get("pro_team"))

        if position == "DST":
            proj_key = ("DST", team)
        elif gsis:
            proj_key = ("SKILL", gsis)
        else:
            proj_key = None
        line = lines.get(proj_key) if proj_key is not None else None

        has_proj = line is not None and week in line.played_weeks
        points = line.points.get(week, 0.0) if (line is not None and has_proj) else 0.0
        on_bye = byes.bye_of(team) == week or (line is not None and not has_proj
                                               and byes.bye_of(team) == week)
        status = row.get("injury_status")
        token = str(status or "").strip().upper()
        hard_out = apply_hard_out and live_status and token in HARD_OUT_STATUSES

        # --- item 3.13: the lock fence -----------------------------------
        kickoff = locks.get(team) if (locks and team is not None) else None
        locked = kickoff is not None and now is not None and kickoff <= now
        slot_token = str(row.get("lineup_slot") or "").strip().upper()
        pin_slot = _ESPN_STARTING_SLOTS.get(slot_token) if locked else None

        # A locked player cannot be moved in EITHER direction. Pinned starters are
        # seated by the pin, not by availability, so `available` means exactly one
        # thing everywhere: "the operator could still move this man".
        available = has_proj and not on_bye and not hard_out and not locked
        sigma = variance.sigma(position, points)

        seats[key] = _Seat(
            key=key,
            player=str(row.get("player") or (line.player if line is not None else None) or key),
            position=position,
            team=team,
            espn_id=str(espn_id) if espn_id is not None else None,
            gsis_id=str(gsis) if gsis else None,
            points=points,
            sigma=sigma,
            injury_status=status,
            lineup_slot=row.get("lineup_slot"),
            on_bye=on_bye,
            has_proj=has_proj,
            hard_out=hard_out,
            available=available,
            kickoff=kickoff,
            locked=locked,
            pin_slot=pin_slot,
            proj_key=proj_key,
        )
    return seats


# ---------------------------------------------------------- win-prob arithmetic


def win_probability(mu_own: float, mu_opp: float, var_own: float, var_opp: float) -> float:
    """P(you outscore your opponent) under a Normal margin — Phi via stdlib erf.

    Monotone the two ways the operator relies on: strictly decreasing in
    ``mu_opp``; and, holding the sign of the margin, lower variance moves P(win)
    AWAY from 0.5 for a favorite (mu_own > mu_opp) and TOWARD/below 0.5 for an
    underdog. That asymmetry is the whole posture lever."""
    denom = math.sqrt(max(var_own + var_opp, 1e-9))
    z = (mu_own - mu_opp) / denom
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _starters_variance(
    starters: Sequence[tuple[str, float, str | None]], rho: float
) -> float:
    """Var of a seated lineup's weekly total from ``(position, sigma, team)`` rows:
    Σ sigma^2 plus the QB<->own-pass-catcher correlation term (rho>0 only for that
    one pairing). THE one copy of this arithmetic — the win probability
    (``_lineup_stats``) and every page that quotes a team-week swing
    (``lineup_sigma``, item 4.7) read it, so the two can never disagree."""
    var = sum(sigma ** 2 for _pos, sigma, _team in starters)
    if rho:
        for i, (qpos, qsigma, qteam) in enumerate(starters):
            if qpos != "QB" or qteam is None:
                continue
            for j, (pos, sigma, team) in enumerate(starters):
                if j != i and pos in ("WR", "TE") and team == qteam:
                    var += 2.0 * rho * qsigma * sigma
    return var


def _lineup_stats(
    seated: Iterable[str], seats: Mapping[str, _Seat], variance: VarianceModel
) -> tuple[float, float]:
    """``(mu, var)`` for a seated set: mu = Σ points, var = Σ sigma^2 plus the
    QB<->own-pass-catcher correlation term (rho>0 only for that one pairing)."""
    seated = list(seated)
    mu = sum(seats[k].points for k in seated)
    var = _starters_variance(
        [(seats[k].position, seats[k].sigma, seats[k].team) for k in seated],
        variance.correlation_qb_passcatcher,
    )
    return mu, var


def lineup_sigma(
    starters: Iterable[tuple[str, float, str | None]],
    *,
    variance: VarianceModel = DEFAULT_VARIANCE,
) -> float:
    """One standard deviation of a seated lineup's weekly house-point total.

    ``starters`` is ``(position, projected points, NFL team)`` per SEATED player.
    Each player's sigma is ``variance.sigma(position, points)`` — exactly what the
    lineup card prices a seat with — combined by ``_starters_variance``, the same
    arithmetic behind the card's win probability. Item 4.7 part 3 quotes this as
    "a normal week-to-week swing in your team's score" on ``ziggurat waivers``;
    it is a dispersion prior, never a scoring number (Rule 2).
    """
    rows = []
    for position, points, team in starters:
        pos = canon_position(position) or position
        rows.append((pos, variance.sigma(pos, points), team))
    return math.sqrt(max(_starters_variance(rows, variance.correlation_qb_passcatcher), 0.0))


def sigma_provenance(
    variance: VarianceModel = DEFAULT_VARIANCE, *, position: str | None = None
) -> str:
    """The bracket every quoted swing carries: the prior's label and source (Rule 6).

    A KICKER's swing is the one figure in this model that was never measured, so
    it must not ride under a label that says "measured 2021-2025"."""
    if (canon_position(position) or position) == "K":
        return (f"[hypothesis NOT YET FITTED: item 3.5's flat kicker weekly swing — "
                f"FG columns landed in migration 013 and the fit is a recorded "
                f"follow-up; {variance.source}]")
    return f"[{variance.label}; {variance.source}]"


# ------------------------------------------------------------------- seating


def _slot_base(label: str) -> str:
    """'RB1' -> 'RB', 'QB' -> 'QB', 'FLEX' -> 'FLEX'."""
    return label.rstrip("0123456789")


def _slot_order(structure: RosterStructure) -> list[str]:
    """The seater's own slot labels, in its own order (QB.., FLEX, DST, K)."""
    labels: list[str] = []
    for pos in ("QB", "RB", "WR", "TE"):
        req = structure.starters.get(pos, 0)
        for i in range(req):
            labels.append(pos if req == 1 else f"{pos}{i + 1}")
    labels.extend([FLEX_LABEL] * structure.flex_slots)
    for pos in ("DST", "K"):
        req = structure.starters.get(pos, 0)
        for i in range(req):
            labels.append(pos if req == 1 else f"{pos}{i + 1}")
    return labels


def _eligible(key: str, label: str, seats: Mapping[str, _Seat],
              structure: RosterStructure) -> bool:
    pos = seats[key].position
    if label == FLEX_LABEL:
        return pos in structure.flex_positions
    return pos == _slot_base(label)


def build_pins(seats: Mapping[str, _Seat]) -> dict[str, tuple[str, ...]]:
    """slot base ('QB'/'RB'/.../'FLEX') -> the locked starters ESPN has frozen there.

    Item 3.13. The pin comes from the ROSTER row's own ``lineup_slot``, because
    that is what ESPN actually locked — not from anything this module would prefer.
    Deterministic order (points desc, then key) so two locked RBs always land in
    RB1/RB2 the same way between runs."""
    pins: dict[str, list[str]] = {}
    for key, seat in seats.items():
        if seat.pin_slot:
            pins.setdefault(seat.pin_slot, []).append(key)
    for lst in pins.values():
        lst.sort(key=lambda k: (-seats[k].points, k))
    return {base: tuple(lst) for base, lst in pins.items()}


def _reduced_structure(structure: RosterStructure,
                       pins: Mapping[str, Sequence[str]]) -> RosterStructure:
    """``structure`` minus the slots the pins already occupy — what is left to solve."""
    starters = dict(structure.starters)
    flex_slots = structure.flex_slots
    for base, keys in pins.items():
        if base == FLEX_LABEL:
            flex_slots = max(0, flex_slots - len(keys))
        else:
            starters[base] = max(0, starters.get(base, 0) - len(keys))
    return replace(structure, starters=starters, flex_slots=flex_slots)


def _greedy_fill(seats: Mapping[str, _Seat], structure: RosterStructure,
                 *, pins: Mapping[str, Sequence[str]] | None = None) -> LineupFill:
    """Greedy best-projected fill, with item 3.13's locked slots PINNED.

    With no pins this is the pre-3.13 call, verbatim. With pins, the pinned men
    hold their ESPN slots and the seater solves only what is left — so a locked
    starter is never benched and nobody is ever promoted into a slot ESPN has
    frozen. Note the direction of the guarantee: the pin is the ROSTER's, not the
    optimiser's, which is why it is applied around ``fill_lineup`` rather than
    inside it (``core/lineup.py`` is the shared seater and knows nothing about a
    league's lock rules)."""
    positions = {k: s.position for k, s in seats.items()}
    points = {k: s.points for k, s in seats.items()}
    available = {k: s.available for k, s in seats.items()}
    if not pins:
        return fill_lineup(list(seats), positions, points, roster=structure,
                           available=available)

    pinned_keys = {k for keys in pins.values() for k in keys}
    free = [k for k in seats if k not in pinned_keys]
    sub = fill_lineup(free, positions, points,
                      roster=_reduced_structure(structure, pins),
                      available={k: available[k] for k in free})

    # Merge back into the FULL label space: pinned men first (they own the
    # lowest-numbered labels of their base), then the solved occupants in order.
    remaining: dict[str, list[str | None]] = {}
    for _label, key in sub.slots:
        base = _slot_base(_label) if _label != FLEX_LABEL else FLEX_LABEL
        remaining.setdefault(base, []).append(key)

    slots: list[tuple[str, str | None]] = []
    seen: dict[str, int] = {}
    for label in _slot_order(structure):
        base = FLEX_LABEL if label == FLEX_LABEL else _slot_base(label)
        i = seen.get(base, 0)
        seen[base] = i + 1
        pinned = pins.get(base, ())
        if i < len(pinned):
            slots.append((label, pinned[i]))
            continue
        queue = remaining.get(base, [])
        slots.append((label, queue.pop(0) if queue else None))

    starters = frozenset(k for _label, k in slots if k is not None)
    total = sum(points.get(k, 0.0) for k in starters)
    bench = tuple(sorted((k for k in free if available[k] and k not in starters),
                         key=lambda k: (-points.get(k, 0.0), k)))
    return LineupFill(total=total, slots=tuple(slots), bench=bench, starters=starters)


def _slotmap(fill: LineupFill) -> dict[str, str]:
    return {label: key for label, key in fill.slots if key is not None}


def _steepest_ascent(
    fill: LineupFill,
    seats: Mapping[str, _Seat],
    structure: RosterStructure,
    *,
    mu_opp: float,
    var_opp: float,
    variance: VarianceModel,
    mu_cap: float,
    pinned_keys: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Hill-climb legal single swaps to maximise the win-probability z-score.

    A move swaps a benched (available) player for a seated one he is eligible to
    replace. Accepted only when it RAISES z and sacrifices no more than ``mu_cap``
    E(points) versus the greedy seed. The roster is tiny, so a handful of passes
    reach a local optimum. Underdog (negative margin) naturally promotes
    higher-sigma players; favorite promotes floors.

    ``pinned_keys`` are item 3.13's locked starters: their slots are never offered
    as a swap target. The bench side needs no separate fence — a locked player is
    ``available=False``, so the search cannot promote one either."""
    slotmap = _slotmap(fill)
    seated = set(slotmap.values())
    mu_greedy, _ = _lineup_stats(seated, seats, variance)
    floor = mu_greedy - mu_cap

    def zscore(keys: set[str]) -> float:
        mu, var = _lineup_stats(keys, seats, variance)
        return (mu - mu_opp) / math.sqrt(max(var + var_opp, 1e-9))

    cur_z = zscore(seated)
    while True:
        bench = [k for k, s in seats.items() if s.available and k not in seated]
        best_gain, best_move = 1e-9, None
        for label, q in list(slotmap.items()):
            if q in pinned_keys:
                continue          # ESPN has frozen this slot — not a legal move
            for p in bench:
                if not _eligible(p, label, seats, structure):
                    continue
                trial = (seated - {q}) | {p}
                mu, _ = _lineup_stats(trial, seats, variance)
                if mu < floor:
                    continue
                gain = zscore(trial) - cur_z
                if gain > best_gain:
                    best_gain, best_move = gain, (label, q, p)
        if best_move is None:
            return slotmap
        label, q, p = best_move
        slotmap[label] = p
        seated = set(slotmap.values())
        cur_z = zscore(seated)


# ------------------------------------------------------------------ slot-lock


def _et_kickoff(gameday, gametime) -> datetime | None:
    """A schedules (gameday, gametime) pair as a tz-aware ET datetime, or None.

    nflverse gametime is ET wall-clock (weather.py convention). Keyed generically
    on the kickoff datetime — the 2026 opener is a WEDNESDAY, so nothing here may
    hard-code a weekday."""
    if not gameday:
        return None
    day = base.iso_date(gameday)
    if not day:
        return None
    hhmm = str(gametime).strip() if gametime else "13:00"
    try:
        hour, minute = int(hhmm[:2]), int(hhmm[3:5]) if len(hhmm) >= 5 else 0
    except (ValueError, TypeError):
        hour, minute = 13, 0
    try:
        return datetime.fromisoformat(day).replace(
            hour=hour, minute=minute, tzinfo=ZoneInfo(_SCHEDULE_TZ)
        )
    except (ValueError, ZoneInfoNotFoundError):
        return None


def game_locks(conn, *, as_of, season, week, view: base.AsOfView = "historical"
               ) -> dict[str, datetime]:
    """normalized team -> tz-aware ET kickoff for the week's REG games.

    Teams on bye are simply absent (no game row), which is exactly what makes a
    seated-D/ST-on-bye detectable downstream (its bye row is absent, not blank)."""
    locks: dict[str, datetime] = {}
    for g in get_schedule(conn, as_of=as_of, season=season, week=week, view=view):
        if g["game_type"] != "REG":
            continue
        kick = _et_kickoff(g["gameday"], g["gametime"])
        if kick is None:
            continue
        for team in (g["home_team"], g["away_team"]):
            t = _norm_team(team)
            if t is not None:
                locks[t] = kick
    return locks


def order_slots_by_lock(
    fill: LineupFill, positions: Mapping[str, str], player_locks: Mapping[str, datetime],
    *, pinned: Iterable[str] = (),
) -> tuple[LineupFill, str | None]:
    """Relabel — WITHOUT changing WHO starts or the total — so the FLEX slot holds
    the LATEST-locking of the interchangeable surplus-position starters, earlier
    ones sitting in the dedicated slots. This preserves late optionality (the FLEX
    is the slot you would change last). Best-effort: never raises; returns a plain
    note when optionality cannot be preserved (no known kickoffs).

    ``player_locks`` maps a seated player key to his tz-aware ET kickoff.

    ``pinned`` (item 3.13) are keys whose slot ESPN has already locked. They keep
    the label ESPN gave them and never enter the relabel group — a locked man has
    no optionality to preserve, and moving his LABEL would print a slot assignment
    that disagrees with the app the operator is looking at."""
    slots = list(fill.slots)
    pinned_set = set(pinned)
    flex = next((i for i, (label, key) in enumerate(slots)
                 if label == FLEX_LABEL and key is not None), None)
    if flex is None or slots[flex][1] in pinned_set:
        return fill, None
    surplus_pos = positions.get(slots[flex][1])
    if surplus_pos is None:
        return fill, None

    group = [(label, key) for label, key in slots
             if key is not None and key not in pinned_set
             and positions.get(key) == surplus_pos
             and (label == FLEX_LABEL or _slot_base(label) == surplus_pos)]
    if len(group) < 2:
        return fill, None

    group_labels = [label for label, _ in group]
    keys = [key for _, key in group]
    known = sorted((player_locks[k], k) for k in keys if player_locks.get(k) is not None)
    if not known:
        return fill, (
            f"could not preserve FLEX optionality — no kickoff times are known for "
            f"the interchangeable {surplus_pos} starters."
        )
    latest_key = known[-1][1]
    unknown = [k for k in keys if player_locks.get(k) is None]
    rest = [k for _, k in known[:-1]] + unknown   # everyone but the latest-locking

    assign = {FLEX_LABEL: latest_key}
    dedicated = [label for label in group_labels if label != FLEX_LABEL]
    for label, key in zip(dedicated, rest):
        assign[label] = key

    new_slots = tuple((label, assign.get(label, key)) for label, key in slots)
    relabeled = LineupFill(
        total=fill.total, slots=new_slots, bench=fill.bench, starters=fill.starters
    )
    return relabeled, None


# ------------------------------------------------------------------ sanity gate


def assert_no_illegal_starters(
    fill: LineupFill, *, byes: Iterable[str], statuses: Mapping[str, str | None],
    week: int, live_status: bool, locked: Iterable[str] = (),
) -> None:
    """HARD-RAISE if any seated starter is on bye or (once live) ruled OUT.

    ``byes`` is the set of seated-player keys on bye this week; ``statuses`` maps a
    key to its ESPN injury status. Belt-and-suspenders with the ``available`` map
    fed to the seater — a wrong starter is invisible to a novice (Rule 6).

    ``locked`` (item 3.13) is the set of seated keys whose NFL game has already
    kicked off. They are EXEMPT, and the exemption is the point: a designation
    that landed AFTER a man played is not an illegal start, it is a status
    change — the live instance was a WR who played the Wednesday opener and was
    placed on IR two days later. Raising there would refuse to print a card over
    a slot the operator is physically unable to change."""
    bye_set = set(byes)
    locked_set = set(locked)
    for label, key in fill.slots:
        if key is None or key in locked_set:
            continue
        if key in bye_set:
            raise StartabilityError(
                f"seated {key} in {label} is on BYE in week {week} — a bye player "
                "scores zero and must never be started."
            )
        token = str(statuses.get(key) or "").strip().upper()
        if live_status and token in HARD_OUT_STATUSES:
            raise StartabilityError(
                f"seated {key} in {label} is ESPN-{token} in week {week} — a player "
                "ruled out must never be started."
            )


# ------------------------------------------------------------- opponent resolve


def resolve_opponent(
    conn, *, as_of, season, week, own_team_id, view: base.AsOfView = "historical"
) -> int | None:
    """The opponent's league team id for ``own_team_id`` in week ``week``, or None
    when there is no matchup row (fantasy playoff weeks 15-17 carry none, and a
    pre-schedule read returns none too). Composes the as-of-gated ``get_matchups``
    accessor; Rule 1 ``as_of`` keyword-only, ``view`` threaded straight through."""
    for m in league_state.get_matchups(conn, as_of=as_of, season=season, week=week, view=view):
        home, away = m["home_team_id"], m["away_team_id"]
        if home == own_team_id:
            return away
        if away == own_team_id:
            return home
    return None


def _opponent_lineup(
    conn, *, as_of, season, week, opp_team_id, source, byes, variance, structure, view,
    locks=None, now=None,
) -> tuple[float, float] | None:
    """The opponent's deterministic ALL-HEALTHY best-lineup ``(mu, var)`` — the
    symmetric, legible baseline. Byes still score zero (their rows are blank/
    absent), but no injury discount is applied to either side. None when his roster
    cannot be read.

    Item 3.13 applies the IDENTICAL lock pinning here. It has to: the live defect
    was a rival's already-played WR being re-seated by OUR optimiser inside his
    projected total, which mis-stated the opponent by up to 11.3 house points —
    13.6 pp of win probability — on the one matchup it fired on."""
    rows = active_players([dict(r) for r in league_state.get_player_state(
        conn, as_of=as_of, season=season, on_team_id=opp_team_id, view=view,
    )])
    if not rows:
        return None
    lines = weekly_lines(conn, as_of=as_of, season=season, weeks=[week],
                         source=source, view=view)
    seats = _price_roster(rows, lines, week=week, byes=byes, variance=variance,
                          live_status=False, apply_hard_out=False,
                          locks=locks, now=now)
    if not seats:
        return None
    fill = _greedy_fill(seats, structure, pins=build_pins(seats))
    return _lineup_stats(fill.starters, seats, variance)


# ---------------------------------------------------------------- the recommender


def build_lineup(
    conn,
    *,
    as_of,
    season: int,
    own_team_id: int | None,
    week: int | None = None,
    opponent_total: float | None = None,
    last_week: int = 17,
    source: str = "sleeper_rotowire",
    roster_structure: RosterStructure = DEFAULT_ROSTER,
    variance: VarianceModel = DEFAULT_VARIANCE,
    now: datetime | None = None,
    view: base.AsOfView = "historical",
    today=None,
) -> LineupRecommendation:
    """Recommend the week's starting lineup to maximise P(win) (item 3.5, Module B).

    Rule 1: ``as_of`` keyword-only, no default; ``view`` threaded into every
    accessor. Rule 2: every point comes from ``weekly_lines`` (priced through
    ``scoring.py``); sigma is a labelled dispersion prior, never a scoring number.

    ``week`` defaults to the current week via ``resolve_weeks`` (which RAISES
    rather than return a finished week on a waiver Tue/Wed). ``opponent_total``
    overrides the auto-computed opponent lineup total (the posture lever the
    done-when drives). ``now`` is a tz-aware ET DECISION input for the GTD /
    inactives logic, kept SEPARATE from ``as_of`` (which gates data)."""
    if own_team_id is None:
        raise OwnTeamUnresolved(
            "build_lineup needs a resolved own_team_id; got None. Pass --team or "
            "resolve it via resolve_own_team — reading the whole league universe as "
            "your roster would produce a confidently-wrong lineup."
        )

    resolved_week = (
        int(week) if week is not None
        else resolve_weeks(conn, as_of=as_of, season=season, last_week=last_week, view=view)[0]
    )

    roster_rows = [dict(r) for r in league_state.get_player_state(
        conn, as_of=as_of, season=season, on_team_id=own_team_id, view=view,
    )]
    active_rows = active_players(roster_rows)

    lines = weekly_lines(conn, as_of=as_of, season=season, weeks=[resolved_week],
                         source=source, view=view)
    byes = bye_map(conn, as_of=as_of, season=season, source=source, view=view)
    live = normalize_as_of(as_of) >= normalize_as_of(
        live_status_from(conn, as_of=as_of, season=season, view=view))

    # --- item 3.13: the lock fence is read BEFORE the seat join, not after ---
    # Pre-3.13 this read happened after the search, so locks could only relabel
    # slots the optimiser had already decided. They have to gate availability
    # instead: a locked slot is not a preference, it is a wall.
    locks = game_locks(conn, as_of=as_of, season=season, week=resolved_week, view=view)
    now_et = _resolve_now(now, as_of)

    seats = _price_roster(active_rows, lines, week=resolved_week, byes=byes,
                          variance=variance, live_status=live, apply_hard_out=True,
                          locks=locks, now=now_et)
    pins = build_pins(seats)
    pinned_keys = frozenset(k for keys in pins.values() for k in keys)

    notes: list[str] = []
    sanity_blocks = _sanity_blocks(seats, week=resolved_week)
    locked_notes = _locked_notes(seats, week=resolved_week)

    # --- opponent total + variance -------------------------------------------
    opp_var = variance.opp_flat_sigma ** 2
    opp_source: str
    if opponent_total is not None:
        mu_opp = float(opponent_total)
        opp_source = "override"
    else:
        opp_id = resolve_opponent(conn, as_of=as_of, season=season, week=resolved_week,
                                  own_team_id=own_team_id, view=view)
        opp = (_opponent_lineup(
            conn, as_of=as_of, season=season, week=resolved_week, opp_team_id=opp_id,
            source=source, byes=byes, variance=variance, structure=roster_structure,
            view=view, locks=locks, now=now_et) if opp_id is not None else None)
        if opp is None:
            mu_opp = None
            opp_source = "unresolved"
        else:
            mu_opp, opp_var = opp
            opp_source = "computed"

    # --- greedy seed, posture, and the win-prob search -----------------------
    greedy = _greedy_fill(seats, roster_structure, pins=pins)
    mu_greedy, var_greedy = _lineup_stats(greedy.starters, seats, variance)

    var_final: float | None = None
    if mu_opp is None:
        # No opponent to price against: fall back to the greedy best-projected
        # lineup, NEUTRAL, and say so (Rule 6). Playoff weeks land here.
        posture = "NEUTRAL"
        final = greedy
        margin = 0.0
        win_prob = 0.5
        notes.append(
            "no opponent matchup was readable for this week (fantasy playoff week, "
            "or the schedule/roster is not synced) — showing the greedy "
            "best-projected lineup, no favorite/underdog tilt applied."
        )
    else:
        spread = math.sqrt(max(var_greedy + opp_var, 1e-9))
        close_band = max(5.0, 0.3 * spread)
        margin = mu_greedy - mu_opp
        if abs(margin) < close_band:
            posture = "NEUTRAL"
            final = greedy
        else:
            posture = "FAVORITE" if margin > 0 else "UNDERDOG"
            slotmap = _steepest_ascent(
                greedy, seats, roster_structure, mu_opp=mu_opp, var_opp=opp_var,
                variance=variance, mu_cap=_MU_SACRIFICE_CAP, pinned_keys=pinned_keys)
            final = _fill_from_slotmap(slotmap, seats, roster_structure)
        mu_final, var_final = _lineup_stats(final.starters, seats, variance)
        win_prob = win_probability(mu_final, mu_opp, var_final, opp_var)

    # --- slot-lock relabel (points-neutral) ----------------------------------
    player_locks = {k: seats[k].kickoff for k in seats}
    positions = {k: s.position for k, s in seats.items()}
    final, lock_note = order_slots_by_lock(final, positions, player_locks,
                                           pinned=pinned_keys)
    if lock_note:
        notes.append(lock_note)

    # --- item 4.7 part 3: the scale of the margin line (DISPLAY ONLY) --------
    # Read back from the variances the win probability used; nothing below this
    # block reads them, and the seated set is fixed above. The relabel is
    # points-neutral, so the starters behind ``var_final`` are the starters shown.
    if var_final is None:
        _mu_shown, var_final = _lineup_stats(final.starters, seats, variance)
    own_sigma = math.sqrt(max(var_final, 0.0))
    opp_sigma = margin_sigma = None
    opp_sigma_basis = ""
    if mu_opp is not None:
        opp_sigma = math.sqrt(max(opp_var, 0.0))
        margin_sigma = math.sqrt(max(var_final + opp_var, 1e-9))
        opp_sigma_basis = (
            "their all-healthy projected starting lineup" if opp_source == "computed"
            else "a flat league-typical figure, because --opponent-total supplied their "
                 "total and not their lineup"
        )

    # --- final sanity gate (belt-and-suspenders) -----------------------------
    bye_keys = {k for k, s in seats.items() if s.on_bye}
    statuses = {k: s.injury_status for k, s in seats.items()}
    assert_no_illegal_starters(final, byes=bye_keys, statuses=statuses,
                               week=resolved_week, live_status=live,
                               locked=pinned_keys)

    # --- GTD contingencies + inactives watch ---------------------------------
    snapshot_vintage = _snapshot_vintage(roster_rows)
    contingencies, watch = _gtd_and_watch(
        final, seats, roster_structure, locks=locks, now=now_et,
        snapshot_vintage=snapshot_vintage, posture=posture, live=live)

    # --- rows ----------------------------------------------------------------
    starters = _starter_rows(final, seats, variance, opponent_total=mu_opp,
                             posture=posture, margin=margin, locks=locks)
    bench = _bench_rows(final, seats, variance)

    # --- K/DST optional upgrade note (never an implicit add/drop) -------------
    notes.extend(_streaming_upgrade_notes(
        conn, seats, final, as_of=as_of, season=season, week=resolved_week,
        source=source, view=view, today=today))

    if opp_source == "computed":
        notes.append(
            f"opponent total {mu_opp:.1f} is his DETERMINISTIC all-healthy "
            "best-projected lineup (symmetric with yours), priced through the house "
            "scoring engine.")
    elif opp_source == "override":
        notes.append(f"opponent total {mu_opp:.1f} was supplied directly (--opponent-total).")

    seated_proj_keys = tuple(seats[k].proj_key for k in final.starters
                             if seats[k].proj_key is not None)
    freshness = tuple(_freshness_lines(conn, lines, roster_rows, as_of=as_of,
                                       today=today, season=season,
                                       seated_proj_keys=seated_proj_keys))

    return LineupRecommendation(
        posture=posture,
        own_projected_total=final.total,
        opponent_total=mu_opp,
        margin=margin,
        win_prob=win_prob,
        starters=starters,
        bench=bench,
        contingencies=contingencies,
        watch_list=watch,
        sanity_blocks=sanity_blocks,
        freshness=freshness,
        notes=tuple(notes),
        as_of=normalize_as_of(as_of).isoformat(),
        season=int(season),
        week=resolved_week,
        team_id=own_team_id,
        locks_first=_locks_first(final, seats, now=now_et),
        locked_notes=locked_notes,
        own_sigma=own_sigma,
        opp_sigma=opp_sigma,
        margin_sigma=margin_sigma,
        opp_sigma_basis=opp_sigma_basis,
        sigma_provenance=sigma_provenance(variance),
    )


# The most E(points) a posture move may trade away versus the greedy lineup — a
# tunable (Phase 4) guard so a variance play can never quietly cost a stud's
# points. Stated in every posture-swap reason (Rule 6).
_MU_SACRIFICE_CAP = 2.0


def _fill_from_slotmap(
    slotmap: Mapping[str, str], seats: Mapping[str, _Seat], structure: RosterStructure
) -> LineupFill:
    """Build a LineupFill from an explicit slot->key assignment (the search's
    output), preserving that assignment rather than re-optimising by points."""
    order = _slot_order(structure)
    slots = tuple((label, slotmap.get(label)) for label in order)
    seated = frozenset(k for k in slotmap.values() if k is not None)
    total = sum(seats[k].points for k in seated)
    bench = tuple(sorted(
        (k for k, s in seats.items() if s.available and k not in seated),
        key=lambda k: (-seats[k].points, k)))
    return LineupFill(total=total, slots=slots, bench=bench, starters=seated)


def _who(s: _Seat) -> str:
    return f"{s.player} ({s.position}, {s.team or '-'})"


def _sanity_blocks(seats: Mapping[str, _Seat], *, week: int) -> tuple[str, ...]:
    """THE "NOT SEATED" TAXONOMY (item 3.13), one sentence per case.

    Before 3.13 these three read almost alike and one of them was simply wrong:
    an already-played starter was printed under the same "cannot start week 1"
    heading as a man ESPN had ruled out on Thursday. They are now three different
    situations with three different sentences and, for LOCKED, a different block
    entirely (see ``_locked_notes``) — because LOCKED is not a removal, it is a
    thing the operator cannot act on at all."""
    out: list[str] = []
    for s in seats.values():
        if s.available or s.locked:
            continue          # LOCKED has its own block; it is not a removal
        if s.hard_out:
            out.append(f"HARD-OUT — {_who(s)}: ESPN lists him {s.injury_status} and "
                       f"his game has NOT started, so this one you can still fix — "
                       f"swap him out; he cannot start week {week}.")
        elif s.on_bye:
            out.append(f"BYE — {_who(s)}: his NFL team does not play in week {week}. "
                       "He would score zero; start someone else.")
        else:
            out.append(f"UNPRICEABLE — {_who(s)}: the projection feed carries no "
                       f"forecast for him in week {week} at this as-of, so this tool "
                       "cannot price him either way. It is NOT a statement that he is "
                       "out — verify him manually before the lock.")
    return tuple(out)


def _locked_notes(seats: Mapping[str, _Seat], *, week: int) -> tuple[str, ...]:
    """The LOCKED arm of the taxonomy (item 3.13): players whose game has started.

    Locked STARTERS are named individually — they are on the card, at a projection
    that is not a live score, and the operator must be told why a man ESPN lists
    as INJURY_RESERVE is still seated. Locked BENCH players get one counted line:
    they are why a body has vanished from the swap options, and naming seven of
    them on a Sunday evening would bury the starters."""
    starters = [s for s in seats.values() if s.pin_slot]
    bench = [s for s in seats.values() if s.locked and not s.pin_slot]
    if not starters and not bench:
        return ()

    out: list[str] = []
    for s in sorted(starters, key=lambda s: (s.kickoff is None,
                                             s.kickoff.isoformat() if s.kickoff else "",
                                             s.player)):
        kick = s.kickoff.isoformat() if s.kickoff else "an unknown time"
        tag = f", ESPN has him {s.injury_status}" if s.injury_status else ""
        priced = (f"carried at his projected {s.points:.1f}"
                  if s.has_proj else
                  "carried at 0.0 — the feed had no week-"
                  f"{week} forecast for him, so this is an absence of data, not a zero")
        out.append(
            f"LOCKED — {_who(s)} in your {s.pin_slot} slot{tag}: his game kicked off "
            f"{kick}, so ESPN froze that slot and there is NOTHING you can do about "
            f"it. He is {priced}.")
    if bench:
        names = ", ".join(sorted(s.player for s in bench))
        out.append(
            f"LOCKED (bench) — {_plural(len(bench), 'player')} on your bench "
            f"{'has' if len(bench) == 1 else 'have'} also kicked off and can no "
            f"longer be moved INTO the lineup: {names}.")
    if starters:
        out.append(LOCKED_CARRY_LABEL)
    return tuple(out)


def _locks_first(fill: LineupFill, seats: Mapping[str, _Seat], *, now: datetime
                 ) -> tuple[str, ...]:
    """The LOCKS FIRST line (item 3.17 deliverable 2) — UNCONDITIONAL, at the top.

    Week 1 was caught ~11 hours before a Thursday lock by the luck of reading
    order, not by process: the card computed every kickoff and printed a lock time
    only inside a GTD contingency, i.e. only if somebody happened to be
    Questionable. So this line always prints, and it names the earliest-locking
    seated starters and their ET kickoff — the deadline the whole week's work has
    to beat."""
    seated = [(label, seats[key]) for label, key in fill.slots if key is not None]
    if not seated:
        return ("LOCKS FIRST: no starters are seated — nothing to lock.",)

    upcoming = [(label, s) for label, s in seated
                if s.kickoff is not None and s.kickoff > now]
    already = [(label, s) for label, s in seated
               if s.kickoff is not None and s.kickoff <= now]
    unknown = [label for label, s in seated if s.kickoff is None]

    out: list[str] = []
    if upcoming:
        first = min(s.kickoff for _label, s in upcoming)
        names = ", ".join(f"{s.player} ({label})" for label, s in
                          sorted(upcoming, key=lambda ls: (ls[1].player, ls[0]))
                          if s.kickoff == first)
        delta = first - now
        hours = delta.total_seconds() / 3600.0
        when = (f"{hours:.1f} h" if hours < 48 else f"{delta.days} days")
        out.append(f"LOCKS FIRST: {names} — kickoff {first.isoformat()} ET, "
                   f"{when} from the decision clock ({now.isoformat()}). "
                   "Every change to those slots must be made before then.")
    elif already:
        last = max(s.kickoff for _label, s in already)
        out.append(f"LOCKS FIRST: every seated starter's game has already kicked off "
                   f"(the last at {last.isoformat()} ET) — this lineup is locked and "
                   "this card is a record, not a decision.")
    else:
        out.append("LOCKS FIRST: no kickoff time is known for any seated starter "
                   "(the schedule is not readable at this as-of) — check lock times "
                   "in the ESPN app yourself before acting on this card.")

    if already and upcoming:
        out.append(f"  ({_plural(len(already), 'seated starter')} already locked — "
                   "see the LOCKED block below.)")
    if unknown:
        out.append(f"  (no kickoff known for {_plural(len(unknown), 'slot')}: "
                   f"{', '.join(sorted(unknown))} — verify {'it' if len(unknown) == 1 else 'them'} manually.)")
    return tuple(out)


def _resolve_now(now: datetime | None, as_of) -> datetime:
    """The decision clock. Explicit ``now`` wins (must be tz-aware ET); otherwise
    midnight ET on the as-of day, so every game that day and later is 'upcoming'."""
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=ZoneInfo(_ET))
        return now
    day = normalize_as_of(as_of)
    return datetime.combine(day, time(0, 0), tzinfo=ZoneInfo(_ET))


def _snapshot_vintage(roster_rows: Sequence[Mapping]) -> str | None:
    days = [r.get("retrieved_as_of") for r in roster_rows if r.get("retrieved_as_of")]
    return max(days) if days else None


def _gtd_and_watch(
    fill: LineupFill, seats: Mapping[str, _Seat], structure: RosterStructure,
    *, locks: Mapping[str, datetime], now: datetime, snapshot_vintage: str | None,
    posture: str, live: bool,
) -> tuple[tuple[GTDContingency, ...], tuple[StarterWatch, ...]]:
    """Contingency plans for game-time-decision starters + the inactives watch.

    For each seated GTD starter: the inactive report resolves his status 90 min
    before kickoff; a same-slot-eligible bench alternative whose game locks AFTER
    that deadline is a SAFE WAIT (start the GTD man, swap only if he is ruled out).
    No safe wait -> gamble mode, deferred to posture. The watch lists every
    unresolved, not-yet-locked seated starter, soonest kickoff first."""
    contingencies: list[GTDContingency] = []
    watch: list[StarterWatch] = []
    seated = set(fill.starters)
    for label, key in fill.slots:
        if key is None:
            continue
        s = seats[key]
        token = str(s.injury_status or "").strip().upper()
        if token not in GTD_STATUSES:
            continue
        kickoff = locks.get(s.team)
        deadline = (kickoff - INACTIVE_REPORT_LEAD) if kickoff is not None else None
        # The same decision clock the watch uses: once his game has kicked off the
        # slot is LOCKED, so a "start X, swap if OUT" plan is no longer actionable —
        # present it as a closed window, never as a live safe-wait/gamble.
        window_closed = kickoff is not None and kickoff <= now

        alt = _best_safe_wait(label, key, seats, structure, locks, deadline, seated)
        if alt is not None:
            asafe = locks.get(seats[alt].team)
            reason = (
                f"START {s.player} — he is a game-time decision ({s.injury_status}). "
                f"His status is public by "
                f"{deadline.isoformat() if deadline else 'kickoff'} (the inactive "
                f"report, 90 min pre-kickoff). If he is ruled OUT, SWAP to "
                f"{seats[alt].player}, whose game locks later "
                f"({asafe.isoformat() if asafe else 'unknown'}) — a safe wait, not a "
                "pre-emptive bench.")
            if window_closed:
                reason = (
                    f"decision window has closed — {label} is locked ({s.player}'s "
                    f"game kicked off {kickoff.isoformat()}); no longer actionable.")
            contingencies.append(GTDContingency(
                player=s.player, slot=label, position=s.position,
                status=s.injury_status,
                status_known_by=deadline.isoformat() if deadline else None,
                kickoff=kickoff.isoformat() if kickoff else None,
                alternative=seats[alt].player,
                alternative_kickoff=asafe.isoformat() if asafe else None,
                safe_wait=True, window_closed=window_closed, reasons=(reason,)))
        else:
            reason = (
                f"START {s.player} — game-time decision ({s.injury_status}) with NO "
                f"safe-wait fallback (every same-slot bench option locks at or before "
                f"his status is known). This is a gamble; as a "
                f"{posture.lower()} the lineup {'wants his ceiling' if posture == 'UNDERDOG' else 'favours a floor' if posture == 'FAVORITE' else 'is a coin flip'}"
                " — decide by his final designation.")
            if window_closed:
                reason = (
                    f"decision window has closed — {label} is locked ({s.player}'s "
                    f"game kicked off {kickoff.isoformat()}); no longer actionable.")
            contingencies.append(GTDContingency(
                player=s.player, slot=label, position=s.position,
                status=s.injury_status,
                status_known_by=deadline.isoformat() if deadline else None,
                kickoff=kickoff.isoformat() if kickoff else None,
                alternative=None, alternative_kickoff=None, safe_wait=False,
                window_closed=window_closed, reasons=(reason,)))

        if kickoff is None or kickoff > now:
            cont = contingencies[-1].reasons[0]
            watch.append(StarterWatch(
                player=s.player, slot=label, position=s.position,
                kickoff=kickoff.isoformat() if kickoff else None,
                deadline=deadline.isoformat() if deadline else None,
                status=s.injury_status, contingency=cont,
                snapshot_vintage=snapshot_vintage,
                reasons=(f"watch {s.player} ({s.injury_status}) — resolve by "
                         f"{deadline.isoformat() if deadline else 'kickoff'}; status "
                         f"as of the {snapshot_vintage or 'unknown'} league snapshot.",)))
    watch.sort(key=lambda w: (w.kickoff is None, w.kickoff or ""))
    return tuple(contingencies), tuple(watch)


def _best_safe_wait(
    label: str, gtd_key: str, seats: Mapping[str, _Seat], structure: RosterStructure,
    locks: Mapping[str, datetime], deadline: datetime | None, seated: set[str],
) -> str | None:
    """The highest-projected BENCH alternative eligible for ``label`` whose game
    locks strictly AFTER ``deadline`` (so you can wait out the GTD man's status).
    A currently-seated starter is not a fallback — only benched bodies qualify."""
    if deadline is None:
        return None
    best, best_pts = None, None
    for k, s in seats.items():
        if k == gtd_key or k in seated or not s.available:
            continue
        if not _eligible(k, label, seats, structure):
            continue
        kick = locks.get(s.team)
        if kick is None or kick <= deadline:
            continue
        if best_pts is None or s.points > best_pts:
            best, best_pts = k, s.points
    return best


def _streaming_upgrade_notes(
    conn, seats: Mapping[str, _Seat], fill: LineupFill, *, as_of, season, week,
    source, view, today,
) -> list[str]:
    """Surface the top streamable D/ST and K free agent as an OPTIONAL upgrade
    note — never an implicit add/drop inside a lineup card. Best-effort: a failure
    to price the shelf is disclosed, not silently swallowed (Rule 6)."""
    seated = {seats[k].position: seats[k] for label, k in fill.slots
              if k is not None and seats[k].position in ("DST", "K")}
    out: list[str] = []
    for pos in ("DST", "K"):
        held = seated.get(pos)
        try:
            board = streaming.rank_streamers(
                conn, as_of=as_of, season=season, position=pos, week=week,
                source=source, view=view, today=today)
        except Exception as exc:  # noqa: BLE001 — disclosed as a note, never silent
            out.append(f"streaming {pos} upgrade check unavailable "
                       f"({type(exc).__name__}) — verify a stream manually.")
            continue
        if not board.ranked:
            continue
        top = board.ranked[0]
        held_pts = held.points if held is not None else 0.0
        if held is None or top.house_points > held_pts + 1e-9:
            label = "D/ST" if pos == "DST" else pos
            out.append(
                f"OPTIONAL {label} upgrade: free agent {top.player} "
                f"({top.house_points:.1f} house pts vs your "
                f"{held.player if held else 'open slot'} {held_pts:.1f}) is the top "
                f"stream this week — see `ziggurat stream`; this card does not add/drop "
                "for you.")
    return out


# ------------------------------------------------------------------ row builders


def _starter_rows(
    fill: LineupFill, seats: Mapping[str, _Seat], variance: VarianceModel,
    *, opponent_total, posture: str, margin: float, locks: Mapping[str, datetime],
) -> tuple[StarterRec, ...]:
    rows: list[StarterRec] = []
    for label, key in fill.slots:
        if key is None:
            continue
        s = seats[key]
        token = str(s.injury_status or "").strip().upper()
        gtd = token in GTD_STATUSES
        kick = locks.get(s.team)
        reasons = [
            f"projects {s.points:.1f} house pts (week priced through the house "
            "scoring engine).",
            variance.describe(s.position, s.points, s.sigma),
            f"floor {s.points - s.sigma:.1f} / ceiling {s.points + s.sigma:.1f} "
            "(+/-1 sigma dispersion band).",
        ]
        if label == FLEX_LABEL:
            reasons.append("seated in FLEX (RB/WR/TE) — the interchangeable slot; if "
                           "healthy it holds the latest-locking of your surplus "
                           "position so you keep the most late optionality.")
        if posture == "FAVORITE":
            reasons.append("FAVORITE posture: the lineup leans to FLOORS (lower "
                           f"variance) to protect a projected lead of {margin:+.1f}; a "
                           f"posture swap sacrifices at most {_MU_SACRIFICE_CAP:.0f} "
                           "projected points.")
        elif posture == "UNDERDOG":
            reasons.append("UNDERDOG posture: the lineup leans to CEILINGS (higher "
                           f"variance) to chase a projected deficit of {margin:+.1f}; a "
                           f"posture swap sacrifices at most {_MU_SACRIFICE_CAP:.0f} "
                           "projected points.")
        if gtd and not s.locked:
            reasons.append(f"GAME-TIME DECISION ({s.injury_status}) — see the "
                           "contingency plan; do not bench pre-emptively if a safe "
                           "wait exists.")
        if s.locked:
            reasons.append(
                f"LOCKED: his game kicked off "
                f"{s.kickoff.isoformat() if s.kickoff else 'already'} — ESPN froze "
                "this slot at his own kickoff, so he cannot be benched and nobody "
                "can be moved in. This row is not a recommendation.")
            reasons.append(LOCKED_CARRY_LABEL)
        rows.append(StarterRec(
            slot=label, player=s.player, position=s.position, espn_id=s.espn_id,
            gsis_id=s.gsis_id, proj_points=s.points, sigma=s.sigma,
            floor=s.points - s.sigma, ceiling=s.points + s.sigma,
            kickoff=kick.isoformat() if kick else None, injury_status=s.injury_status,
            gtd=gtd, reasons=tuple(reasons), locked=s.locked))
    return tuple(rows)


def _bench_rows(
    fill: LineupFill, seats: Mapping[str, _Seat], variance: VarianceModel
) -> tuple[BenchRec, ...]:
    """Benched players who are still MOVABLE, plus (item 3.13) the locked ones.

    A locked bench body is kept on the page deliberately: he is no longer an
    option, and silently dropping a 16-point name off the bench list is exactly
    the kind of disappearance a novice cannot interrogate."""
    seated = set(fill.starters)
    rows: list[BenchRec] = []
    for k in sorted((k for k, s in seats.items()
                     if (s.available or s.locked) and k not in seated),
                    key=lambda k: (seats[k].locked, -seats[k].points, k)):
        s = seats[k]
        if s.locked:
            # A locked man with no forecast prints 0.0 in the points column, and
            # LOCKED removes him from the UNPRICEABLE block — so the only place
            # left to say "that zero is an absence of data" is here.
            priced = (f"{s.points:.1f} projected house pts" if s.has_proj else
                      "0.0 in the points column ONLY because the feed carries no "
                      "forecast for him at this as-of — an absence of data, not a "
                      "measured zero")
            reason = (f"LOCKED on your bench: {priced}, but his game kicked off "
                      f"{s.kickoff.isoformat() if s.kickoff else 'already'} — he can no "
                      "longer be moved into the lineup, so he is not an option this "
                      "week.")
        else:
            reason = (f"benched: {s.points:.1f} house pts, sigma {s.sigma:.1f} — did "
                      "not make the seated lineup this week.")
        rows.append(BenchRec(
            player=s.player, position=s.position, espn_id=s.espn_id, gsis_id=s.gsis_id,
            proj_points=s.points, sigma=s.sigma, injury_status=s.injury_status,
            reasons=(reason,), locked=s.locked, has_proj=s.has_proj))
    return tuple(rows)


# ------------------------------------------------------------------- staleness


def _stale_projection_rows(lines, *, as_of) -> list[tuple]:
    """The projection keys whose NEWEST pull is older than ``STALE_BANNER_DAYS``.

    Item 3.17 deliverable 3. The pre-3.17 banner took the OLDEST pull date across
    the whole feed and shouted about it, so ONE orphan row out of 3,229 read as a
    blanket "do not trust this card". Per-key, newest-first, is the honest unit."""
    cutoff = normalize_as_of(as_of)
    stale: list[tuple] = []
    for key, line in lines.items():
        if not line.retrieved_as_of:
            continue
        newest = max(normalize_as_of(d) for d in line.retrieved_as_of)
        if (cutoff - newest).days > STALE_BANNER_DAYS:
            stale.append(key)
    return stale


def _freshness_lines(conn, lines, roster_rows, *, as_of, today, season,
                     seated_proj_keys: Iterable[tuple] = ()) -> list[str]:
    out: list[str] = []
    cutoff = normalize_as_of(as_of)

    pulled = sorted({d for line in lines.values() for d in line.retrieved_as_of})
    if pulled:
        newest = (cutoff - normalize_as_of(pulled[-1])).days
        out.append(f"projections: pulled {pulled[-1]} — {_plural(newest, 'day')} before {as_of}")
        stale = _stale_projection_rows(lines, as_of=as_of)
        if stale:
            # Item 3.17 d3: COUNT the stale rows, and say whether a SEATED player
            # is among them. A stale bench body is not a reason to distrust the
            # card; a stale STARTER is the only version of this warning that
            # should change what the operator does.
            seated = set(seated_proj_keys)
            hit = sorted(k for k in stale if k in seated)
            gap = (cutoff - normalize_as_of(pulled[0])).days
            head = (f"  WARNING: {len(stale)} of {len(lines)} projection rows are more "
                    f"than {STALE_BANNER_DAYS} days old (oldest pull {pulled[0]}, "
                    f"{gap} days)")
            if hit:
                out.append(f"{head} — and {_plural(len(hit), 'SEATED starter')} "
                           f"{'is' if len(hit) == 1 else 'are'} priced off one of them. "
                           "Run `ziggurat ingest run` before trusting this card.")
            else:
                out.append(f"{head}, but NO seated starter is priced off one — every "
                           "player on this card comes from a fresher pull. A stale "
                           "bench row is not a reason to distrust the lineup; "
                           "`ziggurat ingest run` clears it.")
    else:
        out.append("projections: NONE readable at this as-of")

    state_days = sorted({r.get("retrieved_as_of") for r in roster_rows
                         if r.get("retrieved_as_of")})
    if state_days:
        gap = (cutoff - normalize_as_of(state_days[-1])).days
        out.append(f"league state: pulled {state_days[-1]} — {_plural(gap, 'day')} before {as_of}")
        if gap > STALE_BANNER_DAYS:
            out.append("  WARNING: your roster snapshot is stale — run `ziggurat league sync`.")

    if today is not None:
        watched = {"projections", "weekly_stats", "injuries"}
        try:
            for srow in refresh.source_freshness(conn, season=season, today=today):
                if srow["source"] in watched and srow["verdict"] not in refresh.QUIET_VERDICTS:
                    age = "never pulled" if srow["age_days"] is None else f"{srow['age_days']}d old"
                    out.append(f"  ingest says {srow['source']}: {srow['verdict']} ({age})")
        except Exception:  # noqa: BLE001 — freshness is advisory, never fatal
            pass
    return out


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --------------------------------------------------------------------- display


def format_lineup_recommendation(rec: LineupRecommendation, *, reasons: bool = False) -> str:
    """Render the start/sit card (display only — Rule 3)."""
    opp = "-" if rec.opponent_total is None else f"{rec.opponent_total:.1f}"
    out = [
        f"lineup — season {rec.season}, week {rec.week}, as of {rec.as_of}",
    ]
    # Item 3.17 d2: the lock deadline is the first thing on the page, always.
    out.extend(rec.locks_first)
    out.extend(rec.freshness)
    out.append("")
    out.append(
        f"{rec.posture}  —  you {rec.own_projected_total:.1f}  vs  opp {opp}  "
        f"(margin {rec.margin:+.1f}, win prob {100 * rec.win_prob:.0f}%)")
    # Item 4.7: the scale of the line above and the kind of number it is, beside it.
    if rec.own_sigma is not None:
        own = units.WeeklyScale(
            sigma=rec.own_sigma,
            basis="the seated starters' weekly swings combined",
            provenance=rec.sigma_provenance,
        )
        out.append("  " + units.matchup_sentence(
            own=own, opp_sigma=rec.opp_sigma, opp_basis=rec.opp_sigma_basis,
            margin_sigma=rec.margin_sigma))
    out.append(f"  {units.UNITS_LEGEND}")
    out.append("")

    out.append(f"{'SLOT':<5} {'PLAYER':<22} {'POS':<4} {'PROJ':>6} {'FLOOR':>6} "
               f"{'CEIL':>6} {'sigma':>6}  STATUS")
    for s in rec.starters:
        flag = "  LOCKED" if s.locked else ("" if not s.gtd else "  GTD")
        out.append(
            f"{s.slot:<5} {s.player[:22]:<22} {s.position:<4} {s.proj_points:>6.1f} "
            f"{s.floor:>6.1f} {s.ceiling:>6.1f} {s.sigma:>6.1f}  "
            f"{s.injury_status or '-'}{flag}")
        if reasons:
            out.extend(f"      - {r}" for r in s.reasons)

    if rec.locked_notes:
        out.append("")
        out.append("LOCKED (his game has started — nothing you can do about these):")
        for b in rec.locked_notes:
            out.append(f"  - {b}")

    if rec.sanity_blocks:
        out.append("")
        out.append("! REMOVED (never seated — Rule 6 sanity gate):")
        for b in rec.sanity_blocks:
            out.append(f"  - {b}")

    if rec.contingencies:
        out.append("")
        out.append("CONTINGENCIES (game-time decisions):")
        for c in rec.contingencies:
            if c.window_closed:
                tag = "window closed — locked"
            elif c.safe_wait:
                tag = "safe wait"
            else:
                tag = "GAMBLE — no safe wait"
            out.append(f"  {c.player} ({c.slot}) [{tag}]")
            if reasons:
                out.extend(f"      - {r}" for r in c.reasons)

    if rec.watch_list:
        out.append("")
        out.append("INACTIVES WATCH (unresolved starters, soonest first):")
        for w in rec.watch_list:
            out.append(f"  {w.player} ({w.slot}) — {w.status}, decide by "
                       f"{w.deadline or 'kickoff'}")

    if rec.bench:
        out.append("")
        out.append("BENCH:")
        for b in rec.bench:
            tag = "  [LOCKED — cannot be moved in]" if b.locked else ""
            # A 0.0 with no forecast behind it must never read as a measured zero
            # (item 3.13) — and it has to say so at EVERY verbosity, because the
            # points column is the part a novice reads first.
            if not b.has_proj:
                tag += "  (0.0 = NO FORECAST at this as-of, not a measured zero)"
            out.append(f"  {b.player} ({b.position})  {b.proj_points:.1f} pts{tag}")
            if reasons:
                out.extend(f"      - {r}" for r in b.reasons)

    for note in rec.notes:
        out.append(f"! {note}")
    return "\n".join(out)
