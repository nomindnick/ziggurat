"""Pure grading of frozen replay decisions against the market (item 4.1).

This is the GRADE phase of the weekly replay.  It takes the
:class:`backtest.decisions.WeekRecord` rows the decide phase froze and reads
the market panels at a ``grade_as_of`` that is asserted to be LATER than
every decision — it never imports or calls the candidate generator, so no
grading read can leak back into a decision.

Timing model (item 4.1 recon; the panel's own scrape cadence):

* ``r0`` — the market's WEEK-T page, in practice its last scrape of the week:
  the Friday OF week T (after Thursday's game, before Sunday's), i.e. the
  last scrape ``<= as_of(T)``.  Measured 2026-09-01 over the 316 wp pages
  graded 2021-25: 312 last scrapes fall on a Friday and 4 on a Saturday; 304
  are one day AFTER the week's first gameday, 8 two days after, and only 4
  precede it.  So a Thursday-team player is measured from a page that had
  already seen their game.  The reference every hit is measured from.  A
  player the market did not rank at all enters at ``page_size + 1`` — one
  place past the last ranked player, the most conservative placement that
  still lets "unranked -> ranked" count as movement.  That is a CENSORED
  observation, not a rank, and the censor is applied at ``r0`` ONLY: a player
  unranked at a LEAD page is a miss (``absent``), never a censored entrant, so
  a double-unranked hit is impossible.  The floor also MOVES week to week —
  pages run 31 to 243 rows and 123 of 184 consecutive TRAIN page pairs differ
  by >= 5 rows — so some measured movement is the page resizing under the
  player.  Measured (external review C27, 2026-09-04): of 5,578
  censored-then-ranked TRAIN lines 82.6% hit and 17.4% do NOT, and 3.4% hit
  only because the r0 page was the larger of the two.  The "unranked" depth
  band therefore MIXES never-ranked lines (an automatic miss) with ranked ones.
* ``as_of(T)`` — the Tuesday after week T's last game: the decision clock.
  The rule is "first Tuesday STRICTLY after", so a TUESDAY game slips the clock
  a full week and the slipped clock can then coincide with the NEXT week's.
  Exactly one TRAIN week does it (external review C16, 2026-09-04): 2021 wk15
  lands on 2021-12-28 — seven days on, after every week-16 game, sharing week
  16's own clock — against 48 TRAIN weeks at 1 day and 5 at 2.  Its picks are
  dropped as ``reference_precedes_decision``, so nothing is graded across it.
* ``r1`` — the week-T+1 page, scraped the Friday after ``as_of(T)``.  Three
  days after the flag.  A hit here is a FIRST-SNAPSHOT CROSSING — concurrence,
  not lead: the market and the tool saw the same box score.
  A lead page scraped at or before ``as_of(T)`` is not a post-flag
  observation at all; such a pick is UNGRADEABLE
  (``reference_precedes_decision``), never read from the next page instead.
* ``r2`` — the week-T+2 page.  A hit first seen here is a SECOND-SNAPSHOT-ONLY
  CROSSING: the player did not clear the bar on the first post-flag page and
  does on the next.  It is readable as a lead over the market's weekly re-rank
  ONLY when the T+1 page could have ranked the player: a T+1 bye (the weekly
  page drops bye teams) makes the T+2 page the market's first rankable scrape,
  so that hit counts as a hit but its crossing is BYE-DEFERRED — reported
  apart, never as a lead.

The lead readings are never blended; the report prints them apart.

NAMING (external review C3 / C30, 2026-09-04).  These are SNAPSHOT labels, not
measurements of a lead over the market, and the old names ("lead 1 =
CONCURRENT", "lead 2 = a genuine one-week lead") overclaimed.  The panel is
scraped roughly weekly: 52 of the 54 TRAIN weeks have NO scrape at all between
the week's last game and the Tuesday clock, so what the market believed AT the
flag is UNOBSERVED; on the one Tuesday-vintage page that does exist (2021 wk4)
91 of 150 r0 -> r1 crossings had ALREADY happened by the flag, and 80 of 98
week-5 Friday crossings were already on it.  So a first-snapshot crossing is
concurrence at best, and a second-snapshot-only crossing is a crossing the
market's FIRST post-flag page did not show — not a demonstrated one-week lead.
The ``lead`` FIELD keeps its 1 / 2 / 3 encoding so frozen records stay readable.

Every threshold is a labelled hypothesis with a default and sensitivities
(Rule 6): ``HIT_PLACES`` (how many places the market must move a player for
the flag to count), ``OWNED_DELTA`` (how many ownership points the Sleeper
crowd must add for a pick to be *corroborated*), and the eligibility window
carried on the decisions themselves.  Precision@k is defined for ``k <= 3``
only.  The BASE RATE is the same hit rule applied to every player who
recorded a carry or a target in week T and passes the same eligibility
window — the eligibility-matched null that turns a precision into a lift.
That null is NOT depth-matched: "up >= N places" is easier the deeper (or
more unranked) a player starts, so the null is also cut by ``DEPTH_BANDS``
of the r0 rank and a depth-matched lift (hit minus the pick's own band rate)
is printed BESIDE the raw one — the raw lift rewards picking deeper.  Both
the pooled per-pick interval and the season-block interval are printed
always, through :mod:`backtest.stats`.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from backtest import stats
from backtest.decisions import (
    ELIGIBILITY_LABEL,
    HOLDOUT_SEASONS,
    TRAIN_SEASONS,
    WEEK_DECIDED,
    Decision,
    ReplayParams,
    WeekRecord,
    require_holdout_unlock,
    split_of,
)
from ziggurat.data.nfl import base
from ziggurat.data.nfl.fpecr import get_fpecr
from ziggurat.data.nfl.players import get_players
from ziggurat.data.nfl.sleeper_ownership import (
    CENSOR_FLOOR_PCT,
    get_sleeper_ownership,
    ownership_deltas,
)
from ziggurat.data.nfl.weekly_stats import get_weekly_stats

__all__ = [
    "COND_LEAD2_LABEL",
    "DATA_VINTAGE_LABEL",
    "DEPTH_BANDS",
    "DEPTH_LABEL",
    "HIT_PLACES_DEFAULT",
    "HIT_PLACES_SENSITIVITY",
    "LEAD_BYE_DEFERRED",
    "MARKETS",
    "NULL_LABEL",
    "OWNED_DELTA_DEFAULT",
    "OWNED_DELTA_SENSITIVITY",
    "DepthLifts",
    "GradeInputError",
    "MarketScorecard",
    "MarketSpec",
    "NullRate",
    "PickGrade",
    "Reference",
    "ReferenceCache",
    "Summary",
    "build_scorecard",
    "depth_band",
    "depth_matched_lifts",
    "null_rate",
    "read_reference",
    "render",
    "season_band_totals",
    "split_pooled_band_totals",
]


class GradeInputError(ValueError):
    """The grade phase was handed inputs it must refuse (clock order, k)."""


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketSpec:
    """One FantasyPros ECR panel the replay can grade against."""

    name: str
    ecr_type: str
    pages: Mapping[str, str]
    drops_bye_teams: bool
    label: str


MARKETS: Mapping[str, MarketSpec] = {
    "wp": MarketSpec(
        name="wp",
        ecr_type="wp",
        pages={"QB": "qb", "RB": "ppr-rb", "WR": "ppr-wr", "TE": "ppr-te"},
        drops_bye_teams=True,
        label=(
            "weekly positional PPR ECR — the plan-mandated primary market; the page "
            "DROPS bye-week teams, so a bye at a lead is reported, not counted as a miss"
        ),
    ),
    "ros": MarketSpec(
        name="ros",
        ecr_type="rp",
        pages={"QB": "ros-qb", "RB": "ros-ppr-rb", "WR": "ros-ppr-wr", "TE": "ros-ppr-te"},
        drops_bye_teams=False,
        label=(
            "rest-of-season positional PPR ECR — the season-value view; keeps bye-week "
            "teams and moves less week to week (a harder bar)"
        ),
    ),
}

#: HYPOTHESIS: a market re-rank of at least this many PLACES up the page is a
#: "hit".  The wp pages graded 2021-25 hold RB 92-156, WR 140-243 and TE
#: 82-134 rows (measured 2026-09-01; DB facts, not pinned), so five places is
#: ~one tier inside the top ~80 and loses discrimination deeper — the
#: eligibility-matched null already hits 70% at r0 101-150 and 92% past 150.
#: Untuned; sensitivities are printed on every scorecard, and the depth bands
#: below are what make a deep-pool hit legible as the weak evidence it is.
HIT_PLACES_DEFAULT = 5
HIT_PLACES_SENSITIVITY: tuple[int, ...] = (3, 8, 10)
HIT_DEPTH_CAVEAT = (
    "the pages graded run ~RB 92-156 / WR 140-243 / TE 82-134 rows, so >= 5 places "
    "is ~one tier inside the top ~80 and weak evidence deeper (the eligibility-matched "
    "null already hits 70% at r0 101-150 and 92% past 150, measured 2026-09-01) — read "
    "the depth-matched lift beside the raw one"
)

#: HYPOTHESIS: a Sleeper ownership rise of at least this many POINTS between
#: the week-T and week-T+1 snapshots means the crowd acted on the same
#: information.  Sleeper's population is not this league's; use the delta,
#: never the level.  Untuned; sensitivities printed.
OWNED_DELTA_DEFAULT = 10.0
OWNED_DELTA_SENSITIVITY: tuple[float, ...] = (5.0, 20.0)

#: HYPOTHESIS (item 4.1 audit, STAT-2): the null hit rate rises monotonically
#: with r0 depth because "up >= N places" is easier for a deeper or unranked
#: player, so a lift against the pooled null rewards picking deeper.  The
#: bands cut the week-T page at these r0 ranks (a player ranked past the last
#: band is ">150"; one the page did not rank is "unranked").  Untuned; the
#: edges are round numbers around a 10-team league's roster depth.
DEPTH_BANDS: tuple[int, ...] = (36, 48, 60, 80, 100, 150)
DEPTH_UNRANKED = "unranked"


def depth_band(r0_rank: int | None) -> str:
    """The DEPTH band label of an r0 rank (``None`` = not on the page)."""
    if r0_rank is None:
        return DEPTH_UNRANKED
    lo = 1
    for edge in DEPTH_BANDS:
        if r0_rank <= edge:
            return f"{lo}-{edge}"
        lo = edge + 1
    return f">{DEPTH_BANDS[-1]}"


DEPTH_BAND_LABELS: tuple[str, ...] = tuple(
    [depth_band(e) for e in DEPTH_BANDS] + [f">{DEPTH_BANDS[-1]}", DEPTH_UNRANKED]
)
DEPTH_LABEL = (
    "DEPTH (hypothesis, item 4.1 audit; untuned): the null hit rate rises with r0 "
    "depth because 'up >= N places' is easier for a deeper or unranked player, so the "
    f"raw lift rewards picking deeper.  Bands on the r0 rank: {', '.join(DEPTH_BAND_LABELS)}.  "
    "The depth-matched lift is hit minus the pick's OWN band's null rate that week "
    "(falling back to the season's band rate when the week's band has no gradeable "
    "line; fallbacks are counted on the page) — read it beside the raw lift, and read "
    "any tuning on it."
)

NULL_LABEL = "eligibility-matched null (week-T universe; NOT depth-matched — see DEPTH)"

#: DATA VINTAGE (external review C17, 2026-09-04) — printed beside
#: ``HIT_DEPTH_CAVEAT`` on every card.  Not a hypothesis and not tunable: it is
#: what the graded values ARE.  A replay that reads finalised season files is a
#: game-date cut, not a point-in-time capture, and saying so on the card is the
#: only place a reader of one scorecard can learn it.
DATA_VINTAGE_LABEL = (
    "DATA VINTAGE (honest limit, not a hypothesis): the week-T usage these decisions were "
    "made on is a 2026 BULK PULL of nflverse's finalised season files, read through "
    "base.latest_truth and gated on knowable_as_of = the team's GAMEDAY. That is a "
    "GAME-DATE CUT OF FINALISED DATA, not a point-in-time capture of what upstream had "
    "published at the Tuesday clock: 87.8% of week-T lines were played 1-2 days before the "
    "clock, i.e. inside nflverse's own stated Monday-to-Wednesday correction window. "
    "In-place revision is demonstrated rather than hypothetical — the two stored vintages "
    "(2026-07-25, 2026-09-01) differ on 55 of 94,734 shared keys, 54 of them "
    "air_yards_share and three by at least the shipped 0.10 floor's width, all in 2024-25 "
    "and none in TRAIN."
)

LEAD_BYE_DEFERRED = 3
LEAD_LABELS: Mapping[int, str] = {
    1: ("lead 1 = FIRST-SNAPSHOT CROSSING (concurrent): the market's first post-event "
        "scrape, 3 days after the Tuesday flag — the same box score the tool read"),
    2: ("lead 2 = SECOND-SNAPSHOT-ONLY CROSSING: not crossed on the first post-flag page, "
        "crossed on the next, and read that way ONLY when the T+1 page could have ranked "
        "the player (a T+1 bye is bye-deferred, below; a missing T+1 page is truncated_r1 "
        "— the pick is graded on one scrape and disclosed).  NOT a demonstrated one-week "
        "lead: the market's state AT the flag is unobserved in 52 of 54 TRAIN weeks (C30)"),
    3: ("lead 3 = BYE-DEFERRED: the player's team was on bye at T+1 so the T+2 page is the "
        "market's first rankable scrape — a hit, but the crossing is unmeasurable; never counted "
        "as a lead over the market"),
}

#: Grade statuses per lead.  ``precedes`` = the lead page's scrape is at or
#: before the decision clock, so it is not a post-flag observation.
S_HIT, S_MISS, S_ABSENT, S_BYE, S_NO_PAGE, S_PRECEDES = (
    "hit", "miss", "absent", "bye", "no_page", "precedes",
)
#: Gradeability of a pick as a whole.
G_GRADEABLE = "gradeable"
G_TRUNCATED_R2 = "truncated_r2"
G_TRUNCATED_R1 = "truncated_r1"
G_NO_REFERENCE = "no_reference"
G_NO_RERANK = "no_rerank"
G_BYE_ONLY = "bye_only"
G_REFERENCE_PRECEDES = "reference_precedes_decision"
GRADEABLE_STATUSES = frozenset({G_GRADEABLE, G_TRUNCATED_R2, G_TRUNCATED_R1})
#: Corroboration statuses.
C_YES, C_NO, C_NOT_COVERED, C_UNAVAILABLE = (
    "corroborated", "not_corroborated", "not_covered", "unavailable",
)

PRECISION_KS: tuple[int, ...] = (1, 2, 3)


def canon_team(team: str | None) -> str | None:
    if team is None:
        return None
    return base.TEAM_ALIASES.get(team, team)


# ---------------------------------------------------------------------------
# market references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reference:
    """One market page for (season, week, position), deduped to its LAST
    scrape of the week (a Tuesday + Friday double-scrape keeps the Friday)."""

    market: str
    season: int
    week: int
    position: str
    fp_page: str
    scrape_date: str
    ranks: Mapping[str, int]
    page_size: int
    teams: frozenset[str]

    def entry_rank(self, gsis_id: str) -> int:
        """The rank a hit is measured FROM: the page rank, or one past the page."""
        rank = self.ranks.get(gsis_id)
        return rank if rank is not None else self.page_size + 1


def read_reference(
    conn: sqlite3.Connection,
    *,
    market: MarketSpec,
    season: int,
    week: int,
    position: str,
    as_of: str,
    not_after: str | None = None,
) -> Reference | None:
    """The market's page for ``(season, week, position)`` knowable at ``as_of``.

    Read through ``base.latest_truth`` (the panel is bulk-loaded history —
    Rule 1's documented exception).  ``not_after`` additionally drops scrapes
    dated after a decision clock, so the grade phase can rebuild the exact
    reference the decide phase used and assert they agree.
    """
    page = market.pages.get(position)
    if page is None:
        return None
    rows = base.latest_truth(get_fpecr)(
        conn, as_of=as_of, season=season, ecr_type=market.ecr_type,
        fp_page=page, nfl_week=week,
    )
    if not_after is not None:
        rows = [r for r in rows if r["scrape_date"] <= not_after]
    if not rows:
        return None
    last = max(r["scrape_date"] for r in rows)
    keep = [r for r in rows if r["scrape_date"] == last]
    ranks: dict[str, int] = {}
    teams: set[str] = set()
    for r in keep:
        team = canon_team(r["team"])
        if team is not None and team != "FA":
            teams.add(team)
        gsis = r["gsis_id"]
        if gsis is None:
            continue
        rank = int(r["page_rank"])
        prev = ranks.get(gsis)
        if prev is None or rank < prev:
            ranks[gsis] = rank
    return Reference(
        market=market.name, season=season, week=week, position=position,
        fp_page=page, scrape_date=last, ranks=ranks, page_size=len(keep),
        teams=frozenset(teams),
    )


class ReferenceCache:
    """Memoised :func:`read_reference` at one fixed ``as_of``."""

    def __init__(self, conn: sqlite3.Connection, *, as_of: str) -> None:
        self.conn = conn
        self.as_of = as_of
        self._cache: dict[tuple, Reference | None] = {}

    def get(
        self, market: MarketSpec, season: int, week: int, position: str,
        *, not_after: str | None = None,
    ) -> Reference | None:
        key = (market.name, season, week, position, not_after)
        if key not in self._cache:
            self._cache[key] = read_reference(
                self.conn, market=market, season=season, week=week,
                position=position, as_of=self.as_of, not_after=not_after,
            )
        return self._cache[key]


# ---------------------------------------------------------------------------
# ownership corroboration
# ---------------------------------------------------------------------------


class OwnershipCache:
    """Sleeper ownership deltas ``owned(T+1) - owned(T)`` keyed by gsis_id.

    Degrades to a printed reason rather than a zero: a season with no rows
    (or a database without the table) reports ``unavailable``.
    """

    def __init__(self, conn: sqlite3.Connection, *, as_of: str) -> None:
        self.conn = conn
        self.as_of = as_of
        self._weeks: dict[tuple[int, int], list] = {}
        self._season_reason: dict[int, str | None] = {}
        self._deltas: dict[tuple[int, int], dict[str, float] | None] = {}
        self._crosswalk: frozenset[str] | None = None

    def crosswalked(self, gsis_id: str) -> bool:
        """Whether Sleeper CAN report this player: a ``players.sleeper_id``
        exists for the gsis (read once, under ``latest_truth`` — the
        crosswalk is bulk-loaded identity history).  A crosswalked player
        absent from BOTH snapshots is below the censor floor both weeks — a
        real "did not move", not an unknown."""
        if self._crosswalk is None:
            try:
                rows = base.latest_truth(get_players)(self.conn, as_of=self.as_of)
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                rows = []
            self._crosswalk = frozenset(
                str(r["gsis_id"]) for r in rows
                if r["gsis_id"] is not None and r["sleeper_id"] is not None
            )
        return gsis_id in self._crosswalk

    def _rows(self, season: int, week: int) -> list:
        key = (season, week)
        if key not in self._weeks:
            try:
                rows = base.latest_truth(get_sleeper_ownership)(
                    self.conn, as_of=self.as_of, season=season, week=week,
                )
            except sqlite3.OperationalError as exc:  # table absent (schema < 12)
                if "no such table" in str(exc):
                    rows = []
                else:
                    raise
            self._weeks[key] = list(rows)
        return self._weeks[key]

    def season_reason(self, season: int) -> str | None:
        """None when the season is covered; else the disclosed reason."""
        if season not in self._season_reason:
            try:
                n = self.conn.execute(
                    "SELECT COUNT(*) FROM sleeper_ownership WHERE season = ? "
                    "AND knowable_as_of <= ?",
                    (season, self.as_of),
                ).fetchone()[0]
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                n = 0
            self._season_reason[season] = (
                None if n else f"unavailable: sleeper_ownership missing or empty for {season}"
            )
        return self._season_reason[season]

    def deltas(self, season: int, week: int) -> dict[str, float] | None:
        """gsis_id -> owned(week+1) - owned(week); None when uncovered."""
        key = (season, week)
        if key not in self._deltas:
            if self.season_reason(season) is not None:
                self._deltas[key] = None
            else:
                prev, cur = self._rows(season, week), self._rows(season, week + 1)
                if not prev or not cur:
                    self._deltas[key] = None
                else:
                    out: dict[str, float] = {}
                    for d in ownership_deltas(prev, cur, floor_pct=CENSOR_FLOOR_PCT):
                        gsis = d["gsis_id"]
                        if gsis is None:
                            continue
                        if gsis not in out or d["delta"] > out[gsis]:
                            out[gsis] = float(d["delta"])
                    self._deltas[key] = out
        return self._deltas[key]

    def corroboration(
        self, season: int, week: int, gsis_id: str, *, owned_delta: float
    ) -> tuple[str, float | None, bool]:
        """``(status, delta, imputed)`` for one player under the three-way rule.

        Both snapshots exist and the player is in the deltas -> YES/NO by the
        threshold; both exist, absent from the deltas but crosswalked -> NO
        with an IMPUTED delta of 0.0 (below the censor floor both weeks —
        ``imputed`` is True so the render can disclose it); otherwise
        NOT_COVERED (no crosswalk row, or no T+1 snapshot).  A season with no
        ownership at all is UNAVAILABLE.
        """
        if self.season_reason(season) is not None:
            return C_UNAVAILABLE, None, False
        deltas = self.deltas(season, week)
        if deltas is None:
            return C_NOT_COVERED, None, False
        if gsis_id in deltas:
            delta = deltas[gsis_id]
            return (C_YES if delta >= owned_delta else C_NO), delta, False
        if self.crosswalked(gsis_id):
            return (C_YES if 0.0 >= owned_delta else C_NO), 0.0, True
        return C_NOT_COVERED, None, False


# ---------------------------------------------------------------------------
# the hit rule
# ---------------------------------------------------------------------------


def lead_status(
    entry_rank: int,
    ref: Reference | None,
    *,
    gsis_id: str,
    team: str | None,
    market: MarketSpec,
    places: int,
    not_before: str | None = None,
) -> tuple[str, int | None]:
    """``(status, rank_at_lead)`` for one lead page.

    ``not_before`` is the decision clock: a lead page scraped at or before it
    is not a post-flag observation and reads ``precedes`` — never a hit or a
    miss — so the pick is refused as ungradeable rather than graded against
    a page the decision could have seen.
    """
    if ref is None:
        return S_NO_PAGE, None
    if not_before is not None and ref.scrape_date <= not_before:
        return S_PRECEDES, None
    rank = ref.ranks.get(gsis_id)
    if rank is not None:
        return (S_HIT if entry_rank - rank >= places else S_MISS), rank
    if market.drops_bye_teams and team is not None and canon_team(team) not in ref.teams:
        return S_BYE, None
    return S_ABSENT, None


def gradeability(r0: Reference | None, s1: str, s2: str) -> str:
    if r0 is None:
        return G_NO_REFERENCE
    if S_PRECEDES in (s1, s2):
        # the T+1 page post-dates nothing: it was scraped at or before the
        # flag.  Do NOT fall through to r2 as "lead 1" — its lead semantics
        # would be wrong too (it is the first post-flag scrape, not the second).
        return G_REFERENCE_PRECEDES
    if s1 == S_NO_PAGE and s2 == S_NO_PAGE:
        return G_NO_RERANK
    readable = [s for s in (s1, s2) if s not in (S_NO_PAGE, S_BYE)]
    if not readable:
        return G_BYE_ONLY
    if s2 == S_NO_PAGE:
        return G_TRUNCATED_R2
    if s1 == S_NO_PAGE:
        return G_TRUNCATED_R1
    return G_GRADEABLE


def lead_of(s1: str, s2: str) -> int | None:
    """1 if hit at lead 1; 2 if not hit at 1 and hit at 2 with a RANKABLE T+1
    page; ``LEAD_BYE_DEFERRED`` if the T+1 page dropped the player's team for
    a bye and the T+2 page hit; else None.  Never blended: each is a
    different claim."""
    if s1 == S_HIT:
        return 1
    if s2 == S_HIT:
        return LEAD_BYE_DEFERRED if s1 == S_BYE else 2
    return None


@dataclass(frozen=True)
class PickGrade:
    """One decision, graded on one market at one ``places`` threshold."""

    season: int
    week: int
    strategy: str
    rank_in_board: int
    gsis_id: str
    player: str
    position: str
    team: str | None
    market: str
    places: int
    r0_scrape: str | None
    r0_rank: int | None
    entry_rank: int | None
    status_l1: str
    rank_l1: int | None
    status_l2: str
    rank_l2: int | None
    gradeability: str
    hit: bool | None
    lead: int | None
    corroboration: str
    owned_delta: float | None
    reasons: tuple[str, ...]
    #: the 0.0 delta was imputed: crosswalked, but below the censor floor both weeks
    owned_imputed: bool = False

    @property
    def gradeable(self) -> bool:
        return self.gradeability in GRADEABLE_STATUSES

    @property
    def depth_band(self) -> str:
        return depth_band(self.r0_rank)


def grade_one(
    d: Decision,
    *,
    market: MarketSpec,
    places: int,
    refs: ReferenceCache,
    owned: OwnershipCache,
    owned_delta: float,
    eligibility_market: str | None = None,
) -> PickGrade:
    """Grade one frozen decision on one market.

    Self-checking against the freeze: the r0 page is read gated to the
    decision clock, and it is REFUSED if it post-dates that clock or — on the
    market the decision's eligibility was read from — if its scrape date is
    not the one the decision froze (``r0 reference changed under the
    freeze``).  A silent re-scrape of the panel can therefore never move a
    grade.
    """
    r0 = refs.get(market, d.season, d.week, d.position, not_after=d.as_of)
    if r0 is not None:
        if r0.scrape_date > d.as_of:
            raise GradeInputError(
                f"{d.season} wk{d.week} {d.player}: r0 page scraped {r0.scrape_date}, after "
                f"the decision clock {d.as_of} — the grade phase read a page the decision "
                "could not have seen"
            )
        if eligibility_market == market.name and r0.scrape_date != d.r0_scrape_date:
            raise GradeInputError(
                f"{d.season} wk{d.week} {d.player}: r0 reference changed under the freeze "
                f"(grade-time scrape {r0.scrape_date} != frozen {d.r0_scrape_date}); "
                "re-run the decide phase"
            )
    r1 = refs.get(market, d.season, d.week + 1, d.position)
    r2 = refs.get(market, d.season, d.week + 2, d.position)
    entry = r0.entry_rank(d.gsis_id) if r0 is not None else None
    if entry is None:
        s1, k1, s2, k2 = S_NO_PAGE, None, S_NO_PAGE, None
    else:
        s1, k1 = lead_status(
            entry, r1, gsis_id=d.gsis_id, team=d.team, market=market, places=places,
            not_before=d.as_of,
        )
        s2, k2 = lead_status(
            entry, r2, gsis_id=d.gsis_id, team=d.team, market=market, places=places,
            not_before=d.as_of,
        )
    g = gradeability(r0, s1, s2)
    lead = lead_of(s1, s2) if g in GRADEABLE_STATUSES else None
    hit = (lead is not None) if g in GRADEABLE_STATUSES else None
    corr, delta, imputed = owned.corroboration(
        d.season, d.week, d.gsis_id, owned_delta=owned_delta,
    )
    return PickGrade(
        season=d.season, week=d.week, strategy=d.strategy, rank_in_board=d.rank_in_board,
        gsis_id=d.gsis_id, player=d.player, position=d.position, team=d.team,
        market=market.name, places=places,
        r0_scrape=r0.scrape_date if r0 is not None else None,
        r0_rank=r0.ranks.get(d.gsis_id) if r0 is not None else None,
        entry_rank=entry, status_l1=s1, rank_l1=k1, status_l2=s2, rank_l2=k2,
        gradeability=g, hit=hit, lead=lead, corroboration=corr, owned_delta=delta,
        reasons=d.reasons, owned_imputed=imputed,
    )


# ---------------------------------------------------------------------------
# the matched null (base rate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NullRate:
    """The hit rule applied to every week-T stat line that passes the same
    eligibility window as the pool — the base rate a lift is measured from.

    ``by_band`` carries ``(band, hits, gradeable)`` per DEPTH band so a pick
    can be measured against lines that started as deep as it did.
    ``precedes`` counts lines excluded because the T+1 page was scraped at or
    before the decision clock — the same rule the picks are refused under.

    The CONDITIONAL lead-2 counters (item 4.2, pre-registration §9.1) answer
    "of the lines the market had NOT already re-ranked at r1, how many did it
    re-rank at r2?" — the raw lead-2 rate is structurally depressed because a
    lead-1 hit consumes the denominator.  Two forms, symmetric with the pick
    side: PRIMARY (``l1_rankable`` / ``lead2_conditional``) keeps a line whose
    r1 status is ``miss`` or ``absent`` — the r1 page COULD have ranked him
    and did not; a bye at r1 is excluded because the page could not have,
    and a missing r1 page (``no_page``) because a lead-1 hit was structurally
    impossible.  SECONDARY (``*_incl_bye``) keeps the bye-at-r1 lines in.
    ``by_band_conditional`` is the PRIMARY form per DEPTH band, so the
    conditional lift can be depth-matched like the raw one.
    """

    season: int
    week: int
    market: str
    places: int
    universe: int
    eligible: int
    gradeable: int
    hits: int
    lead1: int
    lead2: int
    lead_bye_deferred: int
    corroborated: int
    corroboration_covered: int
    by_band: tuple[tuple[str, int, int], ...] = ()
    precedes: int = 0
    #: PRIMARY conditional lead-2 (bye at r1 EXCLUDED): denominator / hits
    l1_rankable: int = 0
    lead2_conditional: int = 0
    #: SECONDARY conditional lead-2 (bye at r1 INCLUDED): denominator / hits
    l1_rankable_incl_bye: int = 0
    lead2_conditional_incl_bye: int = 0
    #: (band, lead2_conditional, l1_rankable) — the PRIMARY form per DEPTH band
    by_band_conditional: tuple[tuple[str, int, int], ...] = ()

    @property
    def rate(self) -> float | None:
        return self.hits / self.gradeable if self.gradeable else None

    def band(self, label: str) -> tuple[int, int]:
        """``(hits, gradeable)`` for one DEPTH band this week."""
        for b, h, g in self.by_band:
            if b == label:
                return h, g
        return 0, 0

    def band_conditional(self, label: str) -> tuple[int, int]:
        """``(lead2_conditional, l1_rankable)`` for one DEPTH band this week
        — the PRIMARY (bye-excluded) conditional form."""
        for b, h, g in self.by_band_conditional:
            if b == label:
                return h, g
        return 0, 0


#: r1 statuses under which the market COULD have re-ranked the player and did
#: not: the PRIMARY conditional lead-2 denominator (pre-registration §9.1).
L1_RANKABLE_STATUSES = frozenset({S_MISS, S_ABSENT})
#: ... and the SECONDARY form, which keeps a bye at r1 in the denominator.
L1_RANKABLE_INCL_BYE_STATUSES = L1_RANKABLE_STATUSES | {S_BYE}


def null_universe(
    conn: sqlite3.Connection,
    *,
    season: int,
    week: int,
    as_of: str,
    positions: Sequence[str],
) -> list[tuple[str, str, str | None]]:
    """``(gsis_id, position, team)`` for every REG week-T line with a carry or
    a target — sorted, so the null is deterministic."""
    rows = base.latest_truth(get_weekly_stats)(conn, as_of=as_of, season=season, week=week)
    out: set[tuple[str, str, str | None]] = set()
    keys = None
    for r in rows:
        if keys is None:
            keys = set(r.keys())
        if "season_type" in keys and r["season_type"] not in (None, "REG"):
            continue
        gsis = r["player_id"]
        pos = r["position"]
        if gsis is None or pos not in positions:
            continue
        carries = r["carries"] or 0
        targets = r["targets"] or 0
        if carries <= 0 and targets <= 0:
            continue
        out.add((str(gsis), str(pos), r["recent_team"]))
    return sorted(out, key=lambda t: (t[1], t[0]))


def null_rate(
    conn: sqlite3.Connection,
    *,
    season: int,
    week: int,
    as_of: str,
    market: MarketSpec,
    eligibility_market: MarketSpec,
    eligibility: Mapping[str, int],
    positions: Sequence[str],
    places: int,
    refs: ReferenceCache,
    owned: OwnershipCache,
    owned_delta: float,
) -> NullRate:
    universe = null_universe(conn, season=season, week=week, as_of=refs.as_of, positions=positions)
    eligible = gradeable = hits = lead1 = lead2 = deferred = corr = covered = precedes = 0
    l1_rankable = lead2_cond = l1_rankable_bye = lead2_cond_bye = 0
    band_hits: Counter = Counter()
    band_n: Counter = Counter()
    band_cond_hits: Counter = Counter()
    band_cond_n: Counter = Counter()
    for gsis, pos, team in universe:
        elig_ref = refs.get(eligibility_market, season, week, pos, not_after=as_of)
        if elig_ref is not None:
            rank = elig_ref.ranks.get(gsis)
            if rank is not None and rank <= eligibility.get(pos, 0):
                continue
        eligible += 1
        r0 = refs.get(market, season, week, pos, not_after=as_of)
        if r0 is None:
            continue
        entry = r0.entry_rank(gsis)
        s1, _ = lead_status(
            entry, refs.get(market, season, week + 1, pos),
            gsis_id=gsis, team=team, market=market, places=places, not_before=as_of,
        )
        s2, _ = lead_status(
            entry, refs.get(market, season, week + 2, pos),
            gsis_id=gsis, team=team, market=market, places=places, not_before=as_of,
        )
        g = gradeability(r0, s1, s2)
        if g == G_REFERENCE_PRECEDES:
            precedes += 1
        if g not in GRADEABLE_STATUSES:
            continue
        gradeable += 1
        band = depth_band(r0.ranks.get(gsis))
        band_n[band] += 1
        lead = lead_of(s1, s2)
        if lead is not None:
            hits += 1
            band_hits[band] += 1
        if lead == 1:
            lead1 += 1
        elif lead == 2:
            lead2 += 1
        elif lead == LEAD_BYE_DEFERRED:
            deferred += 1
        # conditional lead-2 (§9.1), from the s1/s2 already in hand — no extra read
        if s1 in L1_RANKABLE_STATUSES:
            l1_rankable += 1
            band_cond_n[band] += 1
            if s2 == S_HIT:
                lead2_cond += 1
                band_cond_hits[band] += 1
        if s1 in L1_RANKABLE_INCL_BYE_STATUSES:
            l1_rankable_bye += 1
            if s2 == S_HIT:
                lead2_cond_bye += 1
        status, _delta, _imp = owned.corroboration(season, week, gsis, owned_delta=owned_delta)
        if status in (C_YES, C_NO):
            covered += 1
            if status == C_YES:
                corr += 1
    return NullRate(
        season=season, week=week, market=market.name, places=places,
        universe=len(universe), eligible=eligible, gradeable=gradeable, hits=hits,
        lead1=lead1, lead2=lead2, lead_bye_deferred=deferred,
        corroborated=corr, corroboration_covered=covered,
        by_band=tuple((b, band_hits[b], band_n[b]) for b in DEPTH_BAND_LABELS if band_n[b]),
        precedes=precedes,
        l1_rankable=l1_rankable, lead2_conditional=lead2_cond,
        l1_rankable_incl_bye=l1_rankable_bye, lead2_conditional_incl_bye=lead2_cond_bye,
        by_band_conditional=tuple(
            (b, band_cond_hits[b], band_cond_n[b]) for b in DEPTH_BAND_LABELS if band_cond_n[b]
        ),
    )


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    """The numbers for one group of seasons (a season, a split, or ALL)."""

    label: str
    split: str
    seasons: tuple[int, ...]
    weeks_total: int
    weeks_decided: int
    weeks_undecided: tuple[tuple[str, int], ...]
    decisions: int
    gradeable: int
    ungradeable: tuple[tuple[str, int], ...]
    precision: tuple[tuple[int, int, int], ...]
    lead1: int
    lead2: int
    bye_at_l1: int
    null_universe: int
    null_eligible: int
    null_gradeable: int
    null_hits: int
    null_lead1: int
    null_lead2: int
    lift_pooled: stats.Interval | None
    lift_block: stats.Interval | None
    per_season_lift: tuple[tuple[int, float], ...]
    corroborated: int
    corroboration_covered: int
    corroboration_unavailable: tuple[str, ...]
    null_corroborated: int
    null_corroboration_covered: int
    #: hits whose crossing is BYE-DEFERRED (a hit; never a lead over the market)
    lead_bye_deferred: int = 0
    null_lead_bye_deferred: int = 0
    #: graded picks that were graded on ONE scrape: (truncated_r2 | truncated_r1, n)
    truncated: tuple[tuple[str, int], ...] = ()
    #: DEPTH-matched lift (hit minus the pick's own band's null rate), both
    #: intervals, the per-season centres, and how many picks fell back to the
    #: season-pooled band rate (or had no band rate at all)
    lift_depth_pooled: stats.Interval | None = None
    lift_depth_block: stats.Interval | None = None
    per_season_lift_depth: tuple[tuple[int, float], ...] = ()
    depth_fallbacks: int = 0
    depth_unmatched: int = 0
    #: (band, hits, n) over the graded picks and over the null lines
    picks_by_band: tuple[tuple[str, int, int], ...] = ()
    null_by_band: tuple[tuple[str, int, int], ...] = ()
    #: the per-week vector behind ``lift_depth_pooled`` (item 4.2 §5.2):
    #: ((season, week), mean of that week's per-pick depth-matched lifts),
    #: weeks with no depth-matched pick ABSENT — the SAME ``depth_matched_lifts``
    #: call that produced the pooled interval, so the two cannot drift
    per_week_lift_depth: tuple[tuple[tuple[int, int], float], ...] = ()
    #: CONDITIONAL lead-2 (item 4.2 §9.1).  PRIMARY form (bye at r1 EXCLUDED):
    #: graded picks whose r1 status is miss/absent, and how many of those hit
    #: at r2; the STRICT variant additionally requires an r2 page; the
    #: SECONDARY form (bye at r1 INCLUDED) beside it.  Null counters mirror
    #: the pick side line for line.
    cond_l2_rankable: int = 0
    cond_l2_hits: int = 0
    cond_l2_rankable_strict: int = 0
    cond_l2_hits_strict: int = 0
    cond_l2_rankable_incl_bye: int = 0
    cond_l2_hits_incl_bye: int = 0
    null_l1_rankable: int = 0
    null_lead2_conditional: int = 0
    null_l1_rankable_incl_bye: int = 0
    null_lead2_conditional_incl_bye: int = 0
    #: DEPTH-matched conditional lead-2 lift (PRIMARY form): per rankable
    #: pick, 1[hit at r2] minus the null's conditional rate in the pick's own
    #: r0 band that week (season band-rate fallback, counted)
    lift_cond_l2_depth_pooled: stats.Interval | None = None
    lift_cond_l2_depth_block: stats.Interval | None = None
    cond_l2_depth_fallbacks: int = 0
    cond_l2_depth_unmatched: int = 0
    #: The SAME conditional lift matched against the SPLIT-POOLED conditional
    #: band table instead of the pick's own (season, week) band cell.  Both are
    #: printed because they are two different nulls and they do not agree: the
    #: primary field above matches per (season, week, band) — the kernel §5.2
    #: fixes for the PRIMARY metric — while the frozen §9.1 measured block
    #: ("+19.01pp [+0.62, +37.39] n=29") was computed against the split-pooled
    #: band table this field reproduces.  §9.1's own wording ("the null
    #: conditional rate of that pick's own r0 band") does not fix the matching
    #: level, so neither reading is a defect and BOTH ship: the note quotes
    #: this field beside the per-week one and says which is which, rather than
    #: quoting one number the other entry point contradicts.
    lift_cond_l2_depth_bandpooled: stats.Interval | None = None

    def cond_lead2_rate(self, *, form: str = "primary") -> stats.Interval:
        """Wilson interval on the conditional lead-2 rate: ``form`` is
        ``primary`` (bye at r1 excluded), ``strict`` (primary + an r2 page
        required) or ``incl_bye`` (the secondary, bye-included form)."""
        if form == "primary":
            return stats.wilson_interval(self.cond_l2_hits, self.cond_l2_rankable)
        if form == "strict":
            return stats.wilson_interval(self.cond_l2_hits_strict, self.cond_l2_rankable_strict)
        if form == "incl_bye":
            return stats.wilson_interval(self.cond_l2_hits_incl_bye, self.cond_l2_rankable_incl_bye)
        raise GradeInputError(
            f"conditional lead-2 form must be primary, strict or incl_bye, got {form!r}"
        )

    @property
    def null_cond_lead2_rate(self) -> float | None:
        """The null's PRIMARY conditional lead-2 rate (bye at r1 excluded)."""
        return (
            self.null_lead2_conditional / self.null_l1_rankable
            if self.null_l1_rankable else None
        )

    @property
    def null_cond_lead2_rate_incl_bye(self) -> float | None:
        return (
            self.null_lead2_conditional_incl_bye / self.null_l1_rankable_incl_bye
            if self.null_l1_rankable_incl_bye else None
        )

    def precision_at(self, k: int) -> stats.Interval:
        if k not in PRECISION_KS:
            raise GradeInputError(f"precision@k is defined for k in {PRECISION_KS}, got {k}")
        for kk, hits, n in self.precision:
            if kk == k:
                return stats.wilson_interval(hits, n)
        raise GradeInputError(f"precision@{k} not computed (k exceeds the replay's k)")

    @property
    def null_rate(self) -> float | None:
        return self.null_hits / self.null_gradeable if self.null_gradeable else None

    @property
    def corroboration_rate(self) -> float | None:
        return (
            self.corroborated / self.corroboration_covered
            if self.corroboration_covered else None
        )

    @property
    def null_corroboration_rate(self) -> float | None:
        return (
            self.null_corroborated / self.null_corroboration_covered
            if self.null_corroboration_covered else None
        )


class DepthLifts(NamedTuple):
    """The depth-matched lift vector: ``per_pick`` in graded-pick order (the
    observations ``lift_depth_pooled`` is the t-interval of), ``per_week``
    keyed ``(season, week)`` — the same observations grouped by week, a week
    with no matched pick ABSENT — and the two counts of picks that fell back
    to the season band rate or could not be matched at all."""

    per_pick: list[float]
    per_week: dict[tuple[int, int], list[float]]
    fallbacks: int
    unmatched: int


def season_band_totals(
    nulls: Mapping[tuple[int, int], NullRate] | Sequence[NullRate],
    *,
    conditional: bool = False,
) -> dict[tuple[int, str], tuple[int, int]]:
    """Season-pooled ``(hits, n)`` per ``(season, band)`` over the null weeks
    — the fallback table for a band the pick's own week never populated.
    ``conditional`` pools ``by_band_conditional`` (the PRIMARY conditional
    lead-2 form) instead of ``by_band``."""
    rows = nulls.values() if isinstance(nulls, Mapping) else nulls
    out: dict[tuple[int, str], list[int]] = {}
    for n in rows:
        for b, h, g_ in (n.by_band_conditional if conditional else n.by_band):
            t = out.setdefault((n.season, b), [0, 0])
            t[0] += h
            t[1] += g_
    return {key: (h, g_) for key, (h, g_) in out.items()}


def split_pooled_band_totals(
    nulls: Mapping[tuple[int, int], NullRate] | Sequence[NullRate],
    *,
    conditional: bool = False,
) -> dict[tuple[int, str], tuple[int, int]]:
    """The band table pooled over the WHOLE split (every season together),
    returned in the ``(season, band)`` shape :func:`_band_matched_lifts` reads
    so every season maps to the one pooled pair.

    Used for the §9.1 conditional variant, where the pre-registered measured
    block was computed against a split-pooled table rather than per
    (season, week) — see ``Summary.lift_cond_l2_depth_bandpooled``."""
    rows = list(nulls.values() if isinstance(nulls, Mapping) else nulls)
    pooled: dict[str, list[int]] = {}
    for n in rows:
        for b, h, g_ in (n.by_band_conditional if conditional else n.by_band):
            t = pooled.setdefault(b, [0, 0])
            t[0] += h
            t[1] += g_
    seasons = {n.season for n in rows}
    return {(s, b): (h, g_) for b, (h, g_) in pooled.items() for s in seasons}


def _band_matched_lifts(
    picks: Sequence[PickGrade],
    nulls: Mapping[tuple[int, int], NullRate],
    *,
    hit_of,
    week_band,
    totals: Mapping[tuple[int, str], tuple[int, int]],
) -> DepthLifts:
    """The ONE matching kernel: per pick, ``hit_of(pick)`` minus the null rate
    of the pick's OWN r0-depth band that week (``week_band(null, band)`` ->
    ``(hits, n)``); a band the week's null never populated falls back to the
    season-pooled rate in ``totals`` (counted); a band with no line all
    season is unmatched (counted, excluded)."""
    per_pick: list[float] = []
    per_week: dict[tuple[int, int], list[float]] = {}
    fallbacks = unmatched = 0
    for p in picks:
        n = nulls.get((p.season, p.week))
        b = p.depth_band
        rate: float | None = None
        if n is not None:
            h, g_ = week_band(n, b)
            if g_:
                rate = h / g_
        if rate is None:
            h, g_ = totals.get((p.season, b), (0, 0))
            if g_:
                rate = h / g_
                fallbacks += 1
        if rate is None:
            unmatched += 1
            continue
        lift = hit_of(p) - rate
        per_pick.append(lift)
        per_week.setdefault((p.season, p.week), []).append(lift)
    return DepthLifts(per_pick, per_week, fallbacks, unmatched)


def depth_matched_lifts(
    graded: Sequence[PickGrade],
    nulls: Mapping[tuple[int, int], NullRate],
    *,
    band_totals: Mapping[tuple[int, str], tuple[int, int]] | None = None,
) -> DepthLifts:
    """THE definition of the depth-matched lift (item 4.1 STAT-2; the item-4.2
    primary metric, pre-registration §5): per graded pick, ``hit`` minus the
    null hit rate of the pick's OWN r0-depth band that week, falling back to
    the season-pooled band rate when the week's band has no gradeable line
    (counted in ``fallbacks``) and skipping the pick when no line in that
    band exists all season (counted in ``unmatched``).

    ``_summarise`` averages ``per_pick`` into ``Summary.lift_depth_pooled`` and
    stores ``per_week`` as ``Summary.per_week_lift_depth``; the search runner
    reads those — nothing else computes this number.  ``band_totals`` is the
    fallback table (``season_band_totals(nulls)`` when omitted; it is keyed by season,
    so passing a superset of seasons changes nothing).  Ungradeable picks are
    the caller's to exclude: ``graded`` is taken as given.
    """
    totals = band_totals if band_totals is not None else season_band_totals(nulls)
    return _band_matched_lifts(
        graded, nulls,
        hit_of=lambda p: 1.0 if p.hit else 0.0,
        week_band=lambda n, b: n.band(b),
        totals=totals,
    )


def _summarise(
    label: str,
    split: str,
    seasons: Sequence[int],
    records: Sequence[WeekRecord],
    picks: Sequence[PickGrade],
    nulls: Mapping[tuple[int, int], NullRate],
    *,
    k: int,
) -> Summary:
    seasons = tuple(sorted(seasons))
    recs = [r for r in records if r.season in seasons]
    pk = [p for p in picks if p.season in seasons]
    undecided = Counter(f"{r.status}: {r.reason}" for r in recs if not r.decided)
    graded = [p for p in pk if p.gradeable]
    ungradeable = Counter(p.gradeability for p in pk if not p.gradeable)
    precision = []
    for kk in PRECISION_KS:
        if kk > k:
            break
        sub = [p for p in graded if p.rank_in_board <= kk]
        precision.append((kk, sum(1 for p in sub if p.hit), len(sub)))
    lead1 = sum(1 for p in graded if p.lead == 1)
    lead2 = sum(1 for p in graded if p.lead == 2)
    lead_bye_deferred = sum(1 for p in graded if p.lead == LEAD_BYE_DEFERRED)
    bye_l1 = sum(1 for p in graded if p.status_l1 == S_BYE)
    # graded on ONE scrape: the other lead page is missing, so lead 2 is
    # structurally impossible for these picks (RULE6-1 — disclosed, not hidden)
    truncated = Counter(p.gradeability for p in graded if p.gradeability != G_GRADEABLE)
    season_nulls = [n for (s, _w), n in sorted(nulls.items()) if s in seasons]
    null_universe_n = sum(n.universe for n in season_nulls)
    null_eligible = sum(n.eligible for n in season_nulls)
    null_gradeable = sum(n.gradeable for n in season_nulls)
    null_hits = sum(n.hits for n in season_nulls)
    null_lead1 = sum(n.lead1 for n in season_nulls)
    null_lead2 = sum(n.lead2 for n in season_nulls)
    null_lead_bye_deferred = sum(n.lead_bye_deferred for n in season_nulls)
    # pooled per-pick lift: hit - that week's null rate, one observation per pick
    per_pick: list[float] = []
    for p in graded:
        n = nulls.get((p.season, p.week))
        if n is None or n.rate is None:
            continue
        per_pick.append((1.0 if p.hit else 0.0) - n.rate)
    lift_pooled = (
        stats.t_interval(per_pick, kind="pooled per-pick") if per_pick else None
    )
    per_season: dict[int, float] = {}
    for s in seasons:
        g = [p for p in graded if p.season == s]
        ns = [n for n in season_nulls if n.season == s]
        ng = sum(n.gradeable for n in ns)
        if not g or not ng:
            continue
        per_season[s] = sum(1 for p in g if p.hit) / len(g) - sum(n.hits for n in ns) / ng
    lift_block = stats.season_block_interval(per_season) if per_season else None
    # DEPTH-matched lift (STAT-2): hit minus the null rate of the pick's OWN
    # r0-depth band that week — ONE definition, `depth_matched_lifts`, whose
    # per-pick vector is averaged here and whose per-week grouping is stored
    # on the Summary for the item-4.2 paired comparison.
    totals = season_band_totals(season_nulls)
    depth = depth_matched_lifts(graded, nulls, band_totals=totals)
    per_season_depth_obs: dict[int, list[float]] = {}
    for (s, _w), lifts in depth.per_week.items():
        per_season_depth_obs.setdefault(s, []).extend(lifts)
    depth_fallbacks, depth_unmatched = depth.fallbacks, depth.unmatched
    lift_depth_pooled = (
        stats.t_interval(depth.per_pick, kind="pooled per-pick depth-matched")
        if depth.per_pick else None
    )
    per_season_depth = {
        s: statistics.fmean(v) for s, v in per_season_depth_obs.items() if v
    }
    lift_depth_block = (
        stats.season_block_interval(per_season_depth) if per_season_depth else None
    )
    per_week_depth = tuple(
        (key, statistics.fmean(v)) for key, v in sorted(depth.per_week.items()) if v
    )
    picks_by_band: dict[str, list[int]] = {}
    for p in graded:
        t = picks_by_band.setdefault(p.depth_band, [0, 0])
        t[0] += 1 if p.hit else 0
        t[1] += 1
    null_by_band: dict[str, list[int]] = {}
    for (_s, b), (h, g_) in totals.items():
        t = null_by_band.setdefault(b, [0, 0])
        t[0] += h
        t[1] += g_
    # CONDITIONAL lead-2 (item 4.2 §9.1) — pick side from the statuses already
    # on each PickGrade, null side from the NullRate counters; both forms
    rankable = [p for p in graded if p.status_l1 in L1_RANKABLE_STATUSES]
    rankable_bye = [p for p in graded if p.status_l1 in L1_RANKABLE_INCL_BYE_STATUSES]
    strict = [p for p in rankable if p.status_l2 != S_NO_PAGE]
    cond_totals = season_band_totals(season_nulls, conditional=True)
    cond_depth = _band_matched_lifts(
        rankable, nulls,
        hit_of=lambda p: 1.0 if p.status_l2 == S_HIT else 0.0,
        week_band=lambda n, b: n.band_conditional(b),
        totals=cond_totals,
    )
    cond_per_season: dict[int, list[float]] = {}
    for (s, _w), lifts in cond_depth.per_week.items():
        cond_per_season.setdefault(s, []).extend(lifts)
    lift_cond_pooled = (
        stats.t_interval(cond_depth.per_pick, kind="pooled per-pick depth-matched conditional")
        if cond_depth.per_pick else None
    )
    # the same conditional lift against the SPLIT-pooled band table (§9.1's own
    # measured block): nulls={} sends every pick to the table, which is the
    # pooled rate for its band, so the two forms differ only in the null's
    # matching level and both are reported (Summary.lift_cond_l2_depth_bandpooled)
    cond_depth_bandpooled = _band_matched_lifts(
        rankable, {},
        hit_of=lambda p: 1.0 if p.status_l2 == S_HIT else 0.0,
        week_band=lambda n, b: (0, 0),
        totals=split_pooled_band_totals(season_nulls, conditional=True),
    )
    lift_cond_bandpooled = (
        stats.t_interval(cond_depth_bandpooled.per_pick,
                         kind="pooled per-pick split-pooled-band conditional")
        if cond_depth_bandpooled.per_pick else None
    )
    cond_season_means = {s: statistics.fmean(v) for s, v in cond_per_season.items() if v}
    lift_cond_block = (
        stats.season_block_interval(cond_season_means) if cond_season_means else None
    )
    # corroboration is a rate over GRADED picks (STAT-3): an ungradeable pick
    # has no hit to corroborate, so it is not in the denominator
    corr = sum(1 for p in graded if p.corroboration == C_YES)
    covered = sum(1 for p in graded if p.corroboration in (C_YES, C_NO))
    unavailable = tuple(sorted({
        f"unavailable: sleeper_ownership missing or empty for {p.season}"
        for p in pk if p.corroboration == C_UNAVAILABLE
    }))
    return Summary(
        label=label, split=split, seasons=seasons,
        weeks_total=len(recs), weeks_decided=sum(1 for r in recs if r.decided),
        weeks_undecided=tuple(sorted(undecided.items())),
        decisions=len(pk), gradeable=len(graded),
        ungradeable=tuple(sorted(ungradeable.items())),
        precision=tuple(precision), lead1=lead1, lead2=lead2, bye_at_l1=bye_l1,
        null_universe=null_universe_n, null_eligible=null_eligible,
        null_gradeable=null_gradeable, null_hits=null_hits,
        null_lead1=null_lead1, null_lead2=null_lead2,
        lift_pooled=lift_pooled, lift_block=lift_block,
        per_season_lift=tuple(sorted(per_season.items())),
        corroborated=corr, corroboration_covered=covered,
        corroboration_unavailable=unavailable,
        null_corroborated=sum(n.corroborated for n in season_nulls),
        null_corroboration_covered=sum(n.corroboration_covered for n in season_nulls),
        lead_bye_deferred=lead_bye_deferred,
        null_lead_bye_deferred=null_lead_bye_deferred,
        truncated=tuple(sorted(truncated.items())),
        lift_depth_pooled=lift_depth_pooled, lift_depth_block=lift_depth_block,
        per_season_lift_depth=tuple(sorted(per_season_depth.items())),
        depth_fallbacks=depth_fallbacks, depth_unmatched=depth_unmatched,
        picks_by_band=tuple(
            (b, picks_by_band[b][0], picks_by_band[b][1])
            for b in DEPTH_BAND_LABELS if b in picks_by_band
        ),
        null_by_band=tuple(
            (b, null_by_band[b][0], null_by_band[b][1])
            for b in DEPTH_BAND_LABELS if b in null_by_band
        ),
        per_week_lift_depth=per_week_depth,
        cond_l2_rankable=len(rankable),
        cond_l2_hits=sum(1 for p in rankable if p.status_l2 == S_HIT),
        cond_l2_rankable_strict=len(strict),
        cond_l2_hits_strict=sum(1 for p in strict if p.status_l2 == S_HIT),
        cond_l2_rankable_incl_bye=len(rankable_bye),
        cond_l2_hits_incl_bye=sum(1 for p in rankable_bye if p.status_l2 == S_HIT),
        null_l1_rankable=sum(n.l1_rankable for n in season_nulls),
        null_lead2_conditional=sum(n.lead2_conditional for n in season_nulls),
        null_l1_rankable_incl_bye=sum(n.l1_rankable_incl_bye for n in season_nulls),
        null_lead2_conditional_incl_bye=sum(
            n.lead2_conditional_incl_bye for n in season_nulls
        ),
        lift_cond_l2_depth_pooled=lift_cond_pooled,
        lift_cond_l2_depth_block=lift_cond_block,
        cond_l2_depth_fallbacks=cond_depth.fallbacks,
        cond_l2_depth_unmatched=cond_depth.unmatched,
        lift_cond_l2_depth_bandpooled=lift_cond_bandpooled,
    )


@dataclass(frozen=True)
class WeekRow:
    season: int
    week: int
    split: str
    as_of: str
    status: str
    reason: str | None
    pool_size: int
    decisions: int
    gradeable: int
    hits: int
    lead1: int
    lead2: int
    null_rate: float | None
    null_gradeable: int
    r1_scrape: str | None
    r2_scrape: str | None
    picks: tuple[PickGrade, ...]
    lead_bye_deferred: int = 0


@dataclass(frozen=True)
class SensitivityRow:
    places: int
    split: str
    k: int
    hits: int
    n: int
    null_rate: float | None
    lift_pooled: stats.Interval | None
    lift_block: stats.Interval | None
    lift_depth_pooled: stats.Interval | None = None
    lift_depth_block: stats.Interval | None = None


@dataclass(frozen=True)
class OwnedSensitivityRow:
    owned_delta: float
    split: str
    corroborated: int
    covered: int
    null_corroborated: int
    null_covered: int


@dataclass(frozen=True)
class MarketScorecard:
    strategy: str
    market: str
    k: int
    places: int
    owned_delta: float
    grade_as_of: str
    weeks: tuple[WeekRow, ...]
    seasons: tuple[Summary, ...]
    splits: tuple[Summary, ...]
    sensitivity: tuple[SensitivityRow, ...]
    owned_sensitivity: tuple[OwnedSensitivityRow, ...]
    picks: tuple[PickGrade, ...]
    generator_failures: tuple[tuple[str, int], ...]
    log_lines: tuple[tuple[str, int], ...]
    hypotheses: tuple[str, ...] = field(default_factory=tuple)
    #: every NullRate the grade used — one per DECIDED week, in (season, week)
    #: order, at this card's ``places`` / ``owned_delta`` — carried so a
    #: reader (the item-4.2 runner) never grades a second time to get them
    nulls: tuple[NullRate, ...] = ()

    @property
    def overall(self) -> Summary:
        """The ALL split.  On a TRAIN-only run this is byte-identical to the
        TRAIN Summary and would silently become a blend the moment the seasons
        change — a reader that means a split reads :meth:`split` by name."""
        for s in self.splits:
            if s.split == "ALL":
                return s
        raise GradeInputError("scorecard has no ALL split")

    def split(self, name: str) -> Summary:
        """The Summary whose ``split`` is ``name`` (TRAIN / HOLDOUT / OTHER /
        ALL).  RAISES when the card holds no such split — it never falls back
        to ALL, so a TRAIN-only run asked for HOLDOUT fails loudly."""
        for s in self.splits:
            if s.split == name:
                return s
        present = [s.split for s in self.splits]
        raise GradeInputError(
            f"scorecard for {self.strategy}/{self.market} has no {name!r} split; "
            f"splits present: {present}"
        )

    def null_at(self, season: int, week: int) -> NullRate:
        """The NullRate this card graded ``(season, week)`` against.  RAISES
        for a week the card holds no null for (undecided, or outside the
        replay) rather than returning an empty rate."""
        for n in self.nulls:
            if (n.season, n.week) == (season, week):
                return n
        raise GradeInputError(
            f"scorecard for {self.strategy}/{self.market} holds no null for "
            f"{season} wk{week} (it graded {len(self.nulls)} decided weeks)"
        )

    def per_week_depth_lift(self, split: str) -> dict[tuple[int, int], float]:
        """``{(season, week): L(g, w)}`` — the mean of that week's per-pick
        depth-matched lifts (pre-registration §5.2), read from the named
        split's Summary (the same ``depth_matched_lifts`` call that produced
        its ``lift_depth_pooled``).  A week with no depth-matched pick is
        ABSENT, never 0.0; the split is resolved by :meth:`split` and raises
        when missing."""
        return dict(self.split(split).per_week_lift_depth)


def _grade_market(
    conn: sqlite3.Connection,
    records: Sequence[WeekRecord],
    params: ReplayParams,
    *,
    market: MarketSpec,
    places: int,
    owned_delta: float,
    refs: ReferenceCache,
    owned: OwnershipCache,
    null_cache: dict[tuple, NullRate],
) -> tuple[list[PickGrade], dict[tuple[int, int], NullRate]]:
    elig_market = MARKETS[params.market]
    picks: list[PickGrade] = []
    nulls: dict[tuple[int, int], NullRate] = {}
    for rec in records:
        if not rec.decided:
            continue
        for d in rec.decisions:
            # the grade phase rebuilds the decide phase's eligibility reference
            # and refuses if the panel moved underneath the freeze
            e = refs.get(elig_market, d.season, d.week, d.position, not_after=d.as_of)
            seen = (e.ranks.get(d.gsis_id), e.page_size) if e is not None else (None, None)
            if seen != (d.market_rank_r0, d.market_page_size_r0):
                raise GradeInputError(
                    f"{d.season} wk{d.week} {d.player}: eligibility reference at grade time "
                    f"{seen} != frozen {(d.market_rank_r0, d.market_page_size_r0)} — the "
                    "panel changed under the freeze; re-run the decide phase"
                )
            picks.append(grade_one(
                d, market=market, places=places, refs=refs, owned=owned,
                owned_delta=owned_delta, eligibility_market=elig_market.name,
            ))
        key = (rec.season, rec.week, market.name, places, owned_delta)
        if key not in null_cache:
            null_cache[key] = null_rate(
                conn, season=rec.season, week=rec.week, as_of=rec.as_of, market=market,
                eligibility_market=elig_market, eligibility=params.eligibility_map,
                positions=params.positions, places=places, refs=refs, owned=owned,
                owned_delta=owned_delta,
            )
        nulls[(rec.season, rec.week)] = null_cache[key]
    return picks, nulls


def _splits(seasons: Sequence[int]) -> list[tuple[str, str, tuple[int, ...]]]:
    out = []
    train = tuple(s for s in seasons if s in TRAIN_SEASONS)
    hold = tuple(s for s in seasons if s in HOLDOUT_SEASONS)
    other = tuple(s for s in seasons if split_of(s) == "OTHER")
    if train:
        out.append(("TRAIN 2021-23", "TRAIN", train))
    if hold:
        out.append(("HOLDOUT 2024-25", "HOLDOUT", hold))
    if other:
        out.append(("OTHER", "OTHER", other))
    out.append(("ALL", "ALL", tuple(seasons)))
    return out


def build_scorecard(
    conn: sqlite3.Connection,
    records: Sequence[WeekRecord],
    params: ReplayParams,
    *,
    strategy: str,
    market: str,
    grade_as_of: str,
    places: int = HIT_PLACES_DEFAULT,
    owned_delta: float = OWNED_DELTA_DEFAULT,
    places_sensitivity: Sequence[int] = HIT_PLACES_SENSITIVITY,
    owned_sensitivity: Sequence[float] = OWNED_DELTA_SENSITIVITY,
    unlock_holdout: bool = False,
) -> MarketScorecard:
    """Grade one strategy's frozen records on one market.

    ``grade_as_of`` must be strictly later than every record's decision
    clock — refused otherwise, because a grade at or before a decision could
    only have been made from the same information the decision saw.

    A HOLDOUT season (2024-25) in ``params`` or in the records is refused
    before any database read unless ``unlock_holdout`` is passed — grading
    the holdout is a read of the final exam, and the caller (the CLI) logs it.
    """
    # the lock runs before every other check so a refusal never depends on
    # the state of the database or the freeze
    require_holdout_unlock(
        set(params.seasons) | {r.season for r in records}, unlock_holdout=unlock_holdout,
    )
    if market not in MARKETS:
        raise GradeInputError(f"unknown market {market!r}; known: {sorted(MARKETS)}")
    if places < 1:
        raise GradeInputError(f"hit places must be >= 1, got {places}")
    recs = sorted(
        (r for r in records if r.strategy == strategy), key=lambda r: (r.season, r.week)
    )
    if not recs:
        raise GradeInputError(f"no frozen records for strategy {strategy!r}")
    latest = max(r.as_of for r in recs)
    if grade_as_of <= latest:
        raise GradeInputError(
            f"grade_as_of {grade_as_of} must be strictly later than the latest decision "
            f"clock {latest}"
        )
    mkt = MARKETS[market]
    refs = ReferenceCache(conn, as_of=grade_as_of)
    owned = OwnershipCache(conn, as_of=grade_as_of)
    null_cache: dict[tuple, NullRate] = {}
    picks, nulls = _grade_market(
        conn, recs, params, market=mkt, places=places, owned_delta=owned_delta,
        refs=refs, owned=owned, null_cache=null_cache,
    )
    seasons = sorted({r.season for r in recs})
    season_summaries = tuple(
        _summarise(f"{s} ({split_of(s)})", split_of(s), (s,), recs, picks, nulls, k=params.k)
        for s in seasons
    )
    splits = tuple(
        _summarise(label, split, ss, recs, picks, nulls, k=params.k)
        for label, split, ss in _splits(seasons)
    )
    # week rows
    by_week: dict[tuple[int, int], list[PickGrade]] = {}
    for p in picks:
        by_week.setdefault((p.season, p.week), []).append(p)
    weeks = []
    for r in recs:
        pk = tuple(sorted(by_week.get((r.season, r.week), []), key=lambda p: p.rank_in_board))
        graded = [p for p in pk if p.gradeable]
        n = nulls.get((r.season, r.week))
        r1 = r2 = None
        for pos in params.positions:
            ref1 = refs.get(mkt, r.season, r.week + 1, pos)
            ref2 = refs.get(mkt, r.season, r.week + 2, pos)
            r1 = r1 or (ref1.scrape_date if ref1 else None)
            r2 = r2 or (ref2.scrape_date if ref2 else None)
        weeks.append(WeekRow(
            season=r.season, week=r.week, split=split_of(r.season), as_of=r.as_of,
            status=r.status, reason=r.reason, pool_size=r.pool_size,
            decisions=len(pk), gradeable=len(graded),
            hits=sum(1 for p in graded if p.hit),
            lead1=sum(1 for p in graded if p.lead == 1),
            lead2=sum(1 for p in graded if p.lead == 2),
            null_rate=n.rate if n else None, null_gradeable=n.gradeable if n else 0,
            r1_scrape=r1, r2_scrape=r2, picks=pk,
            lead_bye_deferred=sum(1 for p in graded if p.lead == LEAD_BYE_DEFERRED),
        ))
    # sensitivities: the hit threshold ...
    sens: list[SensitivityRow] = []
    for pl in sorted({places, *places_sensitivity}):
        if pl == places:
            pk_s, nulls_s = picks, nulls
        else:
            pk_s, nulls_s = _grade_market(
                conn, recs, params, market=mkt, places=pl, owned_delta=owned_delta,
                refs=refs, owned=owned, null_cache=null_cache,
            )
        for label, split, ss in _splits(seasons):
            summ = _summarise(label, split, ss, recs, pk_s, nulls_s, k=params.k)
            hits, n = 0, 0
            for kk, h, nn in summ.precision:
                if kk == params.k:
                    hits, n = h, nn
            sens.append(SensitivityRow(
                places=pl, split=split, k=params.k, hits=hits, n=n,
                null_rate=summ.null_rate, lift_pooled=summ.lift_pooled,
                lift_block=summ.lift_block,
                lift_depth_pooled=summ.lift_depth_pooled,
                lift_depth_block=summ.lift_depth_block,
            ))
    # ... and the corroboration threshold
    osens: list[OwnedSensitivityRow] = []
    for od in sorted({owned_delta, *owned_sensitivity}):
        if od == owned_delta:
            pk_o, nulls_o = picks, nulls
        else:
            pk_o, nulls_o = _grade_market(
                conn, recs, params, market=mkt, places=places, owned_delta=od,
                refs=refs, owned=owned, null_cache=null_cache,
            )
        for label, split, ss in _splits(seasons):
            summ = _summarise(label, split, ss, recs, pk_o, nulls_o, k=params.k)
            osens.append(OwnedSensitivityRow(
                owned_delta=od, split=split, corroborated=summ.corroborated,
                covered=summ.corroboration_covered,
                null_corroborated=summ.null_corroborated,
                null_covered=summ.null_corroboration_covered,
            ))
    failures = Counter(f"{r.status}: {r.reason}" for r in recs if not r.decided)
    logs: Counter = Counter()
    for r in recs:
        for msg, n in r.log_lines:
            logs[msg] += n
    hypotheses = (
        ELIGIBILITY_LABEL,
        params.generator_label,
        f"HIT (hypothesis, untuned): market moves the player >= {places} places up "
        f"the {market} page from the week-T reference (unranked enters at page_size+1 — "
        "a CENSOR, not a rank, applied at r0 ONLY, so an unranked lead page is a miss; "
        "the floor MOVES week to week as the page resizes, and the 'unranked' depth band "
        "mixes never-ranked lines with ranked ones); "
        f"sensitivities {tuple(sorted(set(places_sensitivity)))}.  The week-T page is "
        "the market's last scrape at or before as_of(T) — the Friday OF week T, after "
        f"Thursday's game, so a Thursday breakout is already priced into r0.  {HIT_DEPTH_CAVEAT}.",
        DATA_VINTAGE_LABEL,
        LEAD_LABELS[1],
        LEAD_LABELS[2],
        LEAD_LABELS[LEAD_BYE_DEFERRED],
        f"CORROBORATION (hypothesis, untuned): Sleeper owned_pct(T+1) - owned_pct(T) >= "
        f"{owned_delta:g} points; the population is Sleeper's, not this league's, so only "
        f"the DELTA is read (absent side imputed at the {CENSOR_FLOOR_PCT:g}% censor floor; "
        "a crosswalked player absent from BOTH grids is a real 0.0-point delta, below the "
        "floor both weeks); rate over GRADED picks; "
        f"sensitivities {tuple(sorted(set(owned_sensitivity)))}.",
        f"BASE RATE ({NULL_LABEL}): the same hit rule over every REG week-T stat line with a "
        "carry or a target that passes the same eligibility window as the pool.",
        DEPTH_LABEL,
        "INTERVALS: 'pooled per-pick' treats every pick as independent (pseudo-replicated: "
        "weeks within a season are not independent); 'season-block' is a Student-t on "
        "df = seasons - 1 over per-season lift.  The two centres differ by construction — "
        "per-pick weights weeks by the tool's picks, season-block weights weeks by null "
        "lines (a single season's block centre is exactly p@k minus the season's base rate).",
        f"MARKET {market}: {mkt.label}.",
    )
    return MarketScorecard(
        strategy=strategy, market=market, k=params.k, places=places,
        owned_delta=owned_delta, grade_as_of=grade_as_of,
        weeks=tuple(weeks), seasons=season_summaries, splits=splits,
        sensitivity=tuple(sens), owned_sensitivity=tuple(osens), picks=tuple(picks),
        generator_failures=tuple(sorted(failures.items())),
        log_lines=tuple(sorted(logs.items())), hypotheses=hypotheses,
        nulls=tuple(n for _key, n in sorted(nulls.items())),
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "  n/a" if x is None else f"{100.0 * x:5.1f}%"


def _rate_iv(iv: stats.Interval) -> str:
    if iv.n == 0:
        return "  n/a (0 gradeable)"
    return f"{100.0 * iv.mean:5.1f}% [{100.0 * iv.lo:4.1f}, {100.0 * iv.hi:4.1f}] n={iv.n}"


def _lift(iv: stats.Interval | None) -> str:
    if iv is None:
        return "n/a"
    if iv.is_degenerate:
        return f"{100.0 * iv.mean:+.1f}pp [n={iv.n}: no interval]"
    return (
        f"{100.0 * iv.mean:+.1f}pp [{100.0 * iv.lo:+.1f}, {100.0 * iv.hi:+.1f}] "
        f"n={iv.n}{' *' if iv.excludes_zero else ''}"
    )


COND_LEAD2_LABEL = (
    "conditional lead-2 (the market had not already re-ranked him at r1; bye at r1 excluded)"
)


def _cond_rate(hits: int, n: int) -> str:
    if not n:
        return "n/a (0 rankable)"
    return f"{hits}/{n} = {_rate_iv(stats.wilson_interval(hits, n))}"


def _cond_null(hits: int, n: int) -> str:
    return f"{_pct(hits / n)} ({hits}/{n})" if n else "n/a (0 rankable lines)"


def _cond_lead2(s: Summary) -> str:
    """The ONE conditional lead-2 line: PRIMARY (bye-excluded) rate, its
    null, the strict variant and the DEPTH-matched lift, then the SECONDARY
    (bye-included) form in parentheses."""
    fallback_txt = (
        f"; {s.cond_l2_depth_fallbacks} fallback(s)" if s.cond_l2_depth_fallbacks else ""
    ) + (f"; {s.cond_l2_depth_unmatched} unmatched" if s.cond_l2_depth_unmatched else "")
    return (
        f"{_cond_rate(s.cond_l2_hits, s.cond_l2_rankable)} (Wilson) vs null "
        f"{_cond_null(s.null_lead2_conditional, s.null_l1_rankable)}; strict (r2 page required) "
        f"{_cond_rate(s.cond_l2_hits_strict, s.cond_l2_rankable_strict)}; DEPTH-MATCHED lift "
        f"{_lift(s.lift_cond_l2_depth_pooled)} (null matched per season/week/band)"
        f"{fallback_txt}; same lift vs the SPLIT-POOLED band table "
        f"{_lift(s.lift_cond_l2_depth_bandpooled)} "
        f"(bye at r1 included: {_cond_rate(s.cond_l2_hits_incl_bye, s.cond_l2_rankable_incl_bye)} "
        f"vs null {_cond_null(s.null_lead2_conditional_incl_bye, s.null_l1_rankable_incl_bye)})"
    )


def _render_band_table(
    s: Summary, out: list[str], *, indent: str = "    ", picks_label: str = "picks"
) -> None:
    """The ALL-split null-by-band table: why the raw lift rewards depth."""
    out.append(f"{indent}null hit rate by r0 depth band (eligibility-matched lines; "
               f"{picks_label} beside):")
    picks = {b: (h, n) for b, h, n in s.picks_by_band}
    for b, h, n in s.null_by_band:
        ph, pn = picks.get(b, (0, 0))
        pick_txt = (
            f"  {picks_label} {ph}/{pn} = {_pct(ph / pn)}" if pn else ""
        )
        out.append(f"{indent}  r0 {b:>7}: null {h:5d}/{n:<5d} = "
                   f"{_pct(h / n) if n else '  n/a'}{pick_txt}")
    unmatched = [b for b in picks if b not in {bb for bb, _, _ in s.null_by_band}]
    for b in unmatched:
        ph, pn = picks[b]
        out.append(f"{indent}  r0 {b:>7}: null     -/-     =   n/a  {picks_label} {ph}/{pn} "
                   "(no null line in band)")


def _render_summary(s: Summary, k: int, out: list[str]) -> None:
    out.append(f"  {s.label}: weeks {s.weeks_decided}/{s.weeks_total} decided; "
               f"decisions {s.decisions}, gradeable {s.gradeable}")
    if s.weeks_undecided:
        for reason, n in s.weeks_undecided:
            out.append(f"    undecided weeks: {n} x {reason}")
    if s.ungradeable:
        out.append("    ungradeable picks: " + ", ".join(f"{st}={n}" for st, n in s.ungradeable))
    trunc = dict(s.truncated)
    n_r2, n_r1 = trunc.get(G_TRUNCATED_R2, 0), trunc.get(G_TRUNCATED_R1, 0)
    if s.truncated:
        out.append("    graded on ONE scrape (the other lead page is missing): "
                   + ", ".join(
                       f"{st}={n} ({'lead 2' if st == G_TRUNCATED_R2 else 'lead 1'} impossible)"
                       for st, n in s.truncated))
    for kk, _hits, _n in s.precision:
        out.append(f"    precision@{kk}: {_rate_iv(s.precision_at(kk))}  (Wilson, pooled per-pick)")
    out.append(f"    hits by lead: lead1={s.lead1} (first snapshot)  "
               f"lead2={s.lead2} (second snapshot only)"
               f"  bye-deferred={s.lead_bye_deferred}"
               + (f"  bye at lead1={s.bye_at_l1}" if s.bye_at_l1 else "")
               + (f"  (lead2 impossible for {n_r2} truncated_r2 picks)" if n_r2 else "")
               + (f"  (lead1 impossible for {n_r1} truncated_r1 picks)" if n_r1 else ""))
    out.append(f"    {COND_LEAD2_LABEL}: {_cond_lead2(s)}")
    out.append(f"    base rate ({NULL_LABEL}): {_pct(s.null_rate)} "
               f"({s.null_hits}/{s.null_gradeable} gradeable of {s.null_eligible} eligible "
               f"of {s.null_universe} stat lines; null lead1={s.null_lead1} lead2={s.null_lead2} "
               f"bye-deferred={s.null_lead_bye_deferred})")
    out.append(f"    lift@{k} per-pick vs OWN-WEEK null (pooled t, pseudo-replicated): "
               f"{_lift(s.lift_pooled)}")
    out.append(f"    lift@{k} season-block = season p@{k} - season pooled null (t, df=seasons-1): "
               f"{_lift(s.lift_block)}")
    if s.per_season_lift and len(s.per_season_lift) > 1:
        out.append("      per-season lift: " + ", ".join(
            f"{yr}={100.0 * v:+.1f}pp" for yr, v in s.per_season_lift))
    fallback_txt = (
        f"; {s.depth_fallbacks} pick(s) fell back to the season band rate"
        if s.depth_fallbacks else "; 0 fallbacks"
    ) + (f"; {s.depth_unmatched} pick(s) unmatched (no null line in band all season)"
         if s.depth_unmatched else "")
    out.append(f"    lift@{k} DEPTH-MATCHED per-pick (hit - own r0 band's null; pooled t): "
               f"{_lift(s.lift_depth_pooled)}{fallback_txt}")
    out.append(f"    lift@{k} DEPTH-MATCHED season-block (t, df=seasons-1):             "
               f"{_lift(s.lift_depth_block)}")
    if s.per_season_lift_depth and len(s.per_season_lift_depth) > 1:
        out.append("      per-season depth-matched lift: " + ", ".join(
            f"{yr}={100.0 * v:+.1f}pp" for yr, v in s.per_season_lift_depth))
    if s.split == "ALL" and (s.null_by_band or s.picks_by_band):
        _render_band_table(s, out)
    if s.corroboration_covered or s.corroborated:
        out.append(f"    Sleeper corroboration: {_pct(s.corroboration_rate)} "
                   f"({s.corroborated}/{s.corroboration_covered} covered, graded picks) vs null "
                   f"{_pct(s.null_corroboration_rate)} "
                   f"({s.null_corroborated}/{s.null_corroboration_covered} gradeable lines)")
    for reason in s.corroboration_unavailable:
        out.append(f"    Sleeper corroboration: {reason}")
    if not s.corroboration_covered and not s.corroboration_unavailable and s.decisions:
        out.append("    Sleeper corroboration: no pick covered (no week T+1 snapshot or no "
                   "gsis match)")


def render(card: MarketScorecard, *, reasons: bool = False) -> str:
    """Deterministic text report for one (strategy, market)."""
    out: list[str] = []
    k = card.k
    out.append("=" * 78)
    out.append(f"SCORECARD  strategy={card.strategy}  market={card.market}  k={k}  "
               f"hit_places={card.places}  owned_delta={card.owned_delta:g}  "
               f"grade_as_of={card.grade_as_of}")
    out.append("=" * 78)
    out.append(f"HIT = the player moves >= {card.places} places up the {card.market} page by "
               "the second post-flag scrape; lead1 = FIRST-SNAPSHOT crossing, the same "
               "scrape as the market's first re-rank (does NOT beat the market), lead2 = "
               "SECOND-SNAPSHOT-ONLY crossing, one full scrape later (a lead over the "
               "market ONLY if the market had not already moved before the flag — "
               "unobserved in 52 of 54 TRAIN weeks, so this is not a demonstrated lead); "
               "bye-deferred = a hit whose crossing is unmeasurable (T+1 bye)")
    out.append("* = interval excludes zero (a collapsed or missing interval never earns it)")
    if any(w.split == "HOLDOUT" for w in card.weeks):
        out.append("holdout seasons present: do not tune thresholds on these — and "
                   "note the 12 shipped generator floors already carry documented "
                   "2025 provenance (external review C1, 2026-09-04), so a HOLDOUT "
                   "card is a re-read, not a clean out-of-sample read")
    out.append("")
    out.append("PER WEEK  (split season wk  as_of  status  pool  dec  grad  hits  L1  L2  BD  "
               "base%  r1 r2)")
    out.append("  pool=candidate pool size, dec=decisions, grad=gradeable picks, "
               "L1/L2=hits at lead 1/2, BD=bye-deferred hits, "
               f"base%={NULL_LABEL} hit rate that week, "
               "r1 r2=post-flag scrape dates (- = no page)")
    for w in card.weeks:
        if w.status != WEEK_DECIDED:
            out.append(f"  {w.split:7} {w.season} wk{w.week:02d}  {w.as_of}  "
                       f"UNGRADEABLE: {w.status}: {w.reason}")
            continue
        out.append(
            f"  {w.split:7} {w.season} wk{w.week:02d}  {w.as_of}  {w.status:8} "
            f"{w.pool_size:4d} {w.decisions:4d} {w.gradeable:5d} {w.hits:5d} "
            f"{w.lead1:3d} {w.lead2:3d} {w.lead_bye_deferred:3d}  {_pct(w.null_rate)}  "
            f"{w.r1_scrape or '-'} {w.r2_scrape or '-'}"
        )
        if reasons:
            for p in w.picks:
                status = (
                    f"{p.gradeability}"
                    if not p.gradeable
                    else ("HIT" if p.hit else "miss")
                    + (f" lead{p.lead}" if p.lead else "")
                )
                r0 = "unranked" if p.r0_rank is None else f"#{p.r0_rank}"
                l1 = p.status_l1 + (f"#{p.rank_l1}" if p.rank_l1 else "")
                l2 = p.status_l2 + (f"#{p.rank_l2}" if p.rank_l2 else "")
                if p.owned_delta is None:
                    delta_txt = ""
                elif p.owned_imputed:
                    delta_txt = (f" ({p.owned_delta:+.1f}pt, below the {CENSOR_FLOOR_PCT:g}% "
                                 "floor both weeks)")
                else:
                    delta_txt = f" ({p.owned_delta:+.1f}pt)"
                out.append(
                    f"      [{p.rank_in_board}] {p.player} ({p.position}, {p.team or '?'}) "
                    f"r0={r0} -> L1 {l1}, L2 {l2}  {status}; Sleeper: {p.corroboration}"
                    + delta_txt
                )
                for line in p.reasons:
                    out.append(f"          - {line}")
    out.append("")
    out.append("PER SEASON")
    for s in card.seasons:
        _render_summary(s, k, out)
    out.append("")
    out.append("SUMMARY")
    for s in card.splits:
        _render_summary(s, k, out)
    out.append("")
    out.append(f"SENSITIVITY: hit threshold (places), precision@{k} / base / lift "
               "(raw, then DEPTH-matched)")
    for row in card.sensitivity:
        rate = stats.wilson_interval(row.hits, row.n)
        out.append(
            f"  H={row.places:2d} {row.split:7} p@{k}={_rate_iv(rate)}  base={_pct(row.null_rate)}"
            f"  pooled={_lift(row.lift_pooled)}  block={_lift(row.lift_block)}"
            f"  depth-pooled={_lift(row.lift_depth_pooled)}"
            f"  depth-block={_lift(row.lift_depth_block)}"
        )
    out.append("SENSITIVITY: corroboration threshold (owned points; graded picks / gradeable lines)")
    for row in card.owned_sensitivity:
        rate = row.corroborated / row.covered if row.covered else None
        nrate = row.null_corroborated / row.null_covered if row.null_covered else None
        out.append(
            f"  D={row.owned_delta:4g} {row.split:7} picks {_pct(rate)} "
            f"({row.corroborated}/{row.covered})  null {_pct(nrate)} "
            f"({row.null_corroborated}/{row.null_covered})"
        )
    if card.generator_failures:
        out.append("")
        out.append("UNGRADEABLE WEEKS (generator could not serve; recorded, never skipped)")
        for reason, n in card.generator_failures:
            out.append(f"  {n} x {reason}")
    if card.log_lines:
        out.append("")
        out.append("GENERATOR LOG LINES (distinct, with counts)")
        for msg, n in card.log_lines:
            out.append(f"  {n:5d} x {msg}")
    out.append("")
    out.append("HYPOTHESES (every threshold above is one; none is tuned — 4.2 owns tuning, "
               "on TRAIN seasons only)")
    for h in card.hypotheses:
        out.append(f"  - {h}")
    out.append("  * marks an interval that excludes zero")
    return "\n".join(out)


def render_comparison(cards: Sequence[MarketScorecard]) -> str:
    """One table across strategies: precision@k, base rate and both lifts per
    split — so ``random_k``'s pick-level null sits beside the tool's answer.

    The RAW lifts are NOT comparable across strategies that pick at different
    r0 depths (the null hit rate rises with depth); the DEPTH-matched columns
    are the ones to compare, and the band table under each market shows why.
    """
    out: list[str] = [
        "COMPARISON ACROSS STRATEGIES (same frozen pool, same weeks)",
        "  RAW lifts (pooled/block) are against ONE eligibility-matched null per week and are "
        "NOT comparable across strategies that pick at different r0 depths — compare the "
        "DEPTH-matched columns (depth-pooled/depth-block); see the band table under each market.",
    ]
    by_market: dict[str, list[MarketScorecard]] = {}
    for c in cards:
        by_market.setdefault(c.market, []).append(c)
    for market in sorted(by_market):
        out.append(f"  market={market}")
        ordered = sorted(by_market[market], key=lambda c: c.strategy)
        for c in ordered:
            for s in c.splits:
                out.append(
                    f"    {c.strategy:12} {s.split:7} p@{c.k}={_rate_iv(s.precision_at(c.k))}"
                    f"  base={_pct(s.null_rate)}  pooled={_lift(s.lift_pooled)}"
                    f"  block={_lift(s.lift_block)}"
                    f"  depth-pooled={_lift(s.lift_depth_pooled)}"
                    f"  depth-block={_lift(s.lift_depth_block)}"
                    + (f"  depth-fallbacks={s.depth_fallbacks}" if s.depth_fallbacks else "")
                )
        # the null-by-band table is a property of the week set, not the
        # strategy: identical across the strategies of one market, printed once
        for c in ordered:
            try:
                overall = c.overall
            except GradeInputError:
                continue
            if overall.null_by_band:
                _render_band_table(
                    overall, out, indent="    ", picks_label=f"picks({c.strategy})",
                )
                for c2 in ordered:
                    if c2 is not c:
                        pk = {b: (h, n) for b, h, n in c2.overall.picks_by_band}
                        out.append(f"      picks by band, {c2.strategy}: " + ", ".join(
                            f"{b}={pk[b][0]}/{pk[b][1]}" for b in DEPTH_BAND_LABELS if b in pk
                        ))
                break
    return "\n".join(out)
