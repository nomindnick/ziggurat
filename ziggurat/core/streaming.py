"""D/ST + K streaming ranker — the weekly one-slot matchup call (item 3.5).

WHAT THIS ANSWERS. Marginal valuation (item 3.2) deliberately prices your D/ST
and your kicker on a CURRENT-WEEK horizon and caps each at one, because you
replace them week to week (``marginal.STREAMED_POSITIONS`` /
``marginal.POSITION_CAPS``). It does NOT tell you *which* free-agent defense or
kicker to start this week. That is this module.

THE SHAPING RECON FINDING, and why the opponent-quality tilt is load-bearing.
The weekly projections are a flat season rate, not a week-specific forecast
(item 3.2 measured median week-to-week CV ~1% for every skill position; D/ST the
only real mover at ~12%). So a bare "rank D/STs by this week's projected house
points" is just a season-long defense ranking wearing a streaming label — it
would recommend the same defense every week regardless of matchup, which is the
one thing a streamer must not do. The PRIMARY adjustment here is therefore
opponent quality: a defense that draws a weak offense this week is tilted up.
Vegas and weather are secondary, pre-game-safe context tilts.

ITEM 3.14 AMENDS THAT, FOR D/ST ONLY: THE WEEKLY MARKET BOARD IS THE RANKER.
The tilt above was built because the feed has no week-specific content. It does
not, and that is not fixable inside the feed — but a genuinely week-specific
board was already being captured. ``fp_weekly_ecr`` (item 4.2b) holds the
FantasyPros WEEKLY D/ST consensus, stamped ``knowable_as_of = scrape_date``, so a
Tuesday read needs no leakage fence moved. Measured on 2021-23 (probe E08c, this
file's ``MARKET_LABEL``): Spearman vs realised house points **+0.2701** for the
board against **+0.1300** for the composite below it. So when a board for THIS
week exists it sets the order, and the opponent-quality composite becomes the
NO-BOARD FALLBACK — still computed, still shown, no longer the ranker. Kickers
are untouched: nothing comparable was measured for the ``k`` page.

WHAT THAT BUYS, STATED THE WAY THE CARD STATES IT. The alternative to streaming
is HOLDING, and against holding a drafted incumbent the board's paired margin is
**+1.84 house pts/wk, 95% CI [-0.47, +4.16], n=38 paired weeks** — the interval
CROSSES ZERO. Head-to-head against the composite it replaces, the same replay is
**-0.24 [-3.03, +2.40]** (13 W / 13 L / 12 ties): indistinguishable. The reason
to prefer the board is the rho over 826 pool rows, not that 38-week replay, and
the card says so rather than quoting the flattering half.

RULE 2 IS THE SPINE. ``house_points`` for every candidate comes VERBATIM from
``valuation.weekly_lines(weeks=[week])`` — the SAME priced-through-``scoring.py``
spine that marginal uses — so the raw number can never disagree between the two
modules. This file NEVER scores a stat line itself and hard-codes NO scoring
constant. ``stream_score`` is ``house_points`` times a product of BOUNDED,
LABELLED matchup multipliers and is disclosed everywhere as "matchup-adjusted
expected value (HYPOTHESIS — not house scoring)". Every multiplier is a frozen,
Phase-4-tunable hypothesis whose label and source are quoted in the reasons
(Rule 6).

RULE 1. Every accessor call is keyword-only ``as_of`` and threads ``view``
straight through. The Vegas tilt reads ``get_game_odds`` under the historical
view, which correctly returns NOTHING before gameday (``knowable_as_of ==
gameday``) — so a Tuesday/Wednesday waiver read cannot leak a closing line, and
the ranker discloses "line not yet posted" instead.

RULE 6. Never rank a defense or kicker who is on BYE this week or ruled OUT, and
never emit a phantom 0 for a candidate with no usable projection — refuse and
disclose. The operator is a novice; every ranked row ships its opponent, its
house points, and the matchup reasons that moved it.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import mean, pstdev
from types import MappingProxyType

from ziggurat.core import scoring
from ziggurat.core.marginal import (
    ACQ_FREE_AGENT,
    ACQ_WAIVER,
    DEFAULT_AVAILABILITY,
    bye_map,
    classify_acquisition,
    live_status_from,
    resolve_weeks,
)
from ziggurat.core.valuation import canon_position, weekly_lines
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import base
from ziggurat.data.nfl.fp_weekly import get_fp_weekly_ecr
from ziggurat.data.nfl.game_odds import get_game_odds
from ziggurat.data.nfl.schedules import get_schedule
from ziggurat.data.nfl.weather import get_game_weather
from ziggurat.league import state as league_state

# --------------------------------------------------------------------- constants

# The two positions this module streams — the SAME set marginal caps at one and
# prices on a current-week horizon, kept in sync by import so the two modules can
# never disagree about which positions are "streamed".
STREAM_POSITIONS = frozenset({"DST", "K"})

# The offense positions whose projected house points make up an opponent's weekly
# offensive strength (the opponent-quality signal). Position set, not a scoring
# constant.
_OFFENSE = scoring.OFFENSE_POSITIONS

# The staleness banner shouts past this many days between the projection's pull
# date and the decision date (same constant marginal.py / waiver.py use). A July
# projection pricing a November stream is Rule-1-invisible.
STALE_BANNER_DAYS = 7

# Hard-out designations that bench a candidate once live_status has turned on.
_HARD_OUT = DEFAULT_AVAILABILITY.hard_out_statuses

# ------------------------------------------- the weekly market board (item 3.14)

#: The ``fp_weekly_ecr`` page this module reads. **D/ST ONLY, deliberately.** The
#: rho and the paired margin below were both measured on the D/ST board; nothing
#: comparable was ever measured for the ``k`` page, and a ranker swapped in on the
#: strength of another position's evidence is exactly the move Rule 6 exists to
#: stop. Kickers keep the weather-primary composite.
MARKET_PAGE = "dst"

#: The labelled hypothesis (Rule 6), quoted VERBATIM in every ranked D/ST row's
#: reasons. It carries BOTH halves of the measurement, including the half that
#: does not flatter the change.
MARKET_LABEL = (
    "HYPOTHESIS (item 3.14 step 1, 2026-09-15): the FantasyPros weekly D/ST "
    "consensus ranks streamable defenses better than our opponent-quality "
    "composite. Spearman vs REALISED house points, 2021-23 waiver pool (826 rows, "
    "42 weeks): FantasyPros +0.2701, the shipped composite +0.1300, the closing "
    "implied total +0.3372. Against HOLDING a drafted incumbent the board is worth "
    "+1.84 house pts/wk, 95% CI [-0.47, +4.16], n=38 paired weeks, sign p=0.041 "
    "(24 W / 11 L) — the interval CROSSES ZERO. Head to head against the composite "
    "it replaces, that same replay is -0.24 [-3.03, +2.40] (13 W / 13 L / 12 ties), "
    "i.e. INDISTINGUISHABLE: the reason to prefer the board is the rho over 826 "
    "rows, not the 38-week top-one replay. [probe E08c, seasons 2021-23; 2024-25 "
    "stay locked]"
)

#: The three sentences item 3.14 makes MANDATORY on any page that ranks D/ST
#: streams (Rule 6). They are a tuple so `waiver.py` prints the identical text —
#: two surfaces paraphrasing one disclosure is how they start disagreeing.
#: Never say the market will confirm anything (item 4.1's instrument finding).
DST_CARD_SENTENCES = (
    "THE ALTERNATIVE IS HOLDING, and the interval CROSSES ZERO. Streaming this "
    "board beat holding a drafted incumbent by +1.84 house pts/wk over 38 paired "
    "weeks (2021-23), 95% CI [-0.47, +4.16]. That is a lead we cannot yet "
    "distinguish from zero — not a settled edge.",
    "ACTING ON THIS MEANS A D/ST SWAP IN ROUGHLY 8 WEEKS OUT OF 10 (38 of 45 "
    "measured decision weeks fired a swap). The attention cost is real, it is "
    "weekly, and it is the honest price of the number above.",
    "THIS IS TABLE STAKES, NOT A PRIVATE EDGE: a colleague reading FantasyPros "
    "gets most of it free. Our own private share — what the house scoring and the "
    "matchup work add on top of the public board — is 0 to +0.4 pts/wk.",
)

#: Item 3.14 step 2 asks that no streamed row is ever SHOWN without the
#: season-long number beside it. On `ziggurat waivers` it is beside it, because
#: that page holds the roster and the priced swap matrix. THIS page ranks the free
#: agents and knows neither, and pricing a season-long swap here would mean
#: building the whole marginal board — turning a ~4 s quick scan into a ~24 s one
#: for a number the operator reaches two commands later anyway. So the gap is
#: DISCLOSED with a pointer and a measured magnitude, rather than silently left as
#: "this page shows only the upside". Recorded as a deferral in the 3.14 Update.
ONE_HORIZON_NOTE = (
    "ONE HORIZON ONLY: this page ranks THIS WEEK. It does NOT price what dropping "
    "the player you currently hold costs you over the rest of the season, and that "
    "cost can dwarf the weekly gain — measured on the live 2026-09-15 board, a "
    "streamed row worth +3.2 house pts this week cost -23.1 over the remaining "
    "weeks, an 86% break-even on getting an equally good D/ST back. Both horizons, "
    "and that break-even, are printed per row on `ziggurat waivers`, which knows "
    "your roster. Decide the SWAP there; decide WHICH defense here."
)

#: Qualitative-sweep #3, folded in as a POSTURE disclosure and never as a rule
#: (item 3.14). The guides say "never start a D/ST against your own QB"; we
#: measured the correlation and it is a VARIANCE statement, not a mean one.
DST_POSTURE_NOTE = (
    "POSTURE, not a rule: if a defense here faces YOUR OWN quarterback, the two "
    "scores move against each other (we measured QB vs opposing D/ST rho = -0.4614). "
    "That LOWERS your week's variance — which helps when you are the FAVOURITE and "
    "hurts when you are the UNDERDOG. It is not a points penalty and it is never a "
    "'never'; `ziggurat lineup` is where your posture for the week is decided."
)


class StreamPositionError(ValueError):
    """``position`` was neither 'DST' nor 'K'. Raised rather than guessing."""


@dataclass(frozen=True)
class MarketBoard:
    """One captured FantasyPros weekly positional board, newest scrape only.

    ``by_team`` is normalized team abbr -> ``(rank, ecr, pos_rank_label)``. D/ST
    rows carry a FantasyPros TEAM id and a NULL ``gsis_id`` (the migration-016
    contract), so team abbr is the ONLY join — the same key ``_line_for`` already
    uses for a D/ST projection line.
    """

    page: str
    week: int | None
    scrape_date: str
    by_team: Mapping[str, tuple[int | None, float, str | None]]
    source_table: str = "fp_weekly_ecr"

    @property
    def size(self) -> int:
        return len(self.by_team)


def dst_market_board(
    conn, *, as_of, season: int, week: int | None,
    view: base.AsOfView = "historical",
) -> MarketBoard | None:
    """The newest weekly D/ST consensus board knowable at ``as_of`` (item 3.14).

    Rule 1: ``as_of`` is keyword-only and ``view`` threads straight into the
    accessor, which gates BOTH ``knowable_as_of`` (= the scrape day) and
    ``retrieved_as_of``. A board scraped after the decision is therefore invisible,
    which is the whole reason this source needed no fence moved.

    ``week=None`` asks "what IS stored, whatever week it ranks" — used only to
    tell the operator what the fallback is falling back FROM. A board for one week
    is never served for another: a week-1 board ranking week-2 defenses would be a
    season-long list wearing a weekly label, which is the exact failure the item
    exists to end.
    """
    rows = [
        dict(r) for r in get_fp_weekly_ecr(
            conn, as_of=as_of, season=int(season),
            nfl_week=(None if week is None else int(week)),
            page=MARKET_PAGE, view=view,
        )
    ]
    rows = [r for r in rows if r.get("ecr") is not None and _norm_team(r.get("team"))]
    if not rows:
        return None
    # `select_as_of` resolves the newest RETRIEVAL per (id, page, scrape_date), so
    # every scrape day <= as_of comes back. The freshest SCRAPE is the board.
    newest = max(str(r["scrape_date"]) for r in rows)
    fresh = [r for r in rows if str(r["scrape_date"]) == newest]
    by_team: dict[str, tuple[int | None, float, str | None]] = {}
    for r in sorted(fresh, key=lambda r: (float(r["ecr"]), str(r.get("team")))):
        team = _norm_team(r.get("team"))
        if team is None or team in by_team:
            continue
        rank = r.get("rank")
        by_team[team] = (
            int(rank) if rank is not None else None,
            float(r["ecr"]),
            (str(r["pos_rank_label"]) if r.get("pos_rank_label") else None),
        )
    if not by_team:
        return None
    weeks = {r.get("nfl_week") for r in fresh if r.get("nfl_week") is not None}
    return MarketBoard(
        page=MARKET_PAGE,
        week=(int(next(iter(weeks))) if len(weeks) == 1 else None),
        scrape_date=newest,
        by_team=MappingProxyType(by_team),
    )


# ------------------------------------------------------------- adjustment model


@dataclass(frozen=True)
class StreamAdjust:
    """The bounded, LABELLED matchup multipliers — every one a HYPOTHESIS.

    None of these are scoring numbers (Rule 2): they multiply a house-scored
    projection, they never re-price one. All are Phase-4-tunable and each carries
    a ``*_label`` quoted verbatim in the reason text it produces (Rule 6). The
    tilts are two-sided and bounded, so a matchup can move a stream but never
    invert the underlying house projection by more than its own magnitude.
    """

    # (1) OPPONENT QUALITY — the PRIMARY, load-bearing D/ST tilt. ``opp_tilt`` is
    # the max +/- fraction; the actual tilt is ``opp_tilt * tanh(z)`` where z is
    # the opponent offense's standardized weekly projection among the other
    # offenses, so a league-average opponent moves the stream 0%.
    opp_tilt: float
    # (2) VEGAS — CONTEXT-ONLY D/ST tilt off the opponent's implied team total.
    # ``vegas_pivot`` is the league-typical implied team total; below it the D/ST
    # is bumped. Leakage-fenced (see the module docstring).
    vegas_tilt: float
    vegas_pivot: float
    vegas_scale: float
    # (3) WEATHER — K PRIMARY (the demonstrable done-when), D/ST secondary. Wind
    # penalty on kicking begins at ``wind_calm_mph`` and steepens past
    # ``wind_steep_mph``; dome / weather-irrelevant games are untouched.
    wind_calm_mph: float
    wind_steep_mph: float
    wind_mild_rate: float     # kicking penalty per mph between calm and steep
    wind_steep_rate: float    # kicking penalty per mph beyond steep
    precip_rate: float        # kicking penalty per mm of precipitation
    k_weather_floor: float    # a wind/precip multiplier never sinks below this
    dst_weather_bump: float   # secondary D/ST bump in genuinely bad weather

    opp_label: str
    vegas_label: str
    weather_label: str
    source: str

    # -- opponent quality (D/ST primary) -----------------------------------

    def opponent_multiplier(
        self, opp_pts: float, reference: Sequence[float], opp_team: str
    ) -> tuple[float, str]:
        """Tilt UP for a weaker opponent offense. ``reference`` is the other
        teams' week-W projected offensive house points (the opponent itself
        excluded, so the comparison is stable when sweeping one matchup)."""
        ref = [float(x) for x in reference]
        avg = mean(ref) if ref else opp_pts
        spread = pstdev(ref) if len(ref) > 1 else 0.0
        if spread <= 0.0:
            tilt = 0.0
        else:
            z = (opp_pts - avg) / spread
            tilt = -self.opp_tilt * math.tanh(z)          # weak opp (low z) -> +tilt
        mult = 1.0 + tilt
        reason = (
            f"OPPONENT MATCHUP: {opp_team} projects {opp_pts:.1f} house pts on "
            f"offense this week vs a league average of {avg:.1f} — a "
            f"{'weaker' if tilt > 0 else 'stronger' if tilt < 0 else 'league-average'} "
            f"offense, so the stream is tilted {tilt:+.0%}. "
            f"[{self.opp_label}; {self.source}]"
        )
        return mult, reason

    # -- Vegas (D/ST context) ----------------------------------------------

    def vegas_multiplier(self, opp_implied: float, opp_team: str) -> tuple[float, str]:
        """Tilt UP when Vegas implies the opponent scores few points."""
        z = (opp_implied - self.vegas_pivot) / self.vegas_scale
        tilt = -self.vegas_tilt * math.tanh(z)
        mult = 1.0 + tilt
        reason = (
            f"VEGAS: the closing line implies {opp_team} scores about "
            f"{opp_implied:.1f} points (league-typical is {self.vegas_pivot:.0f}); a "
            f"lower implied total means more D/ST scoring, tilt {tilt:+.0%}. "
            f"[{self.vegas_label}; {self.source}]"
        )
        return mult, reason

    # -- weather (K primary, D/ST secondary) -------------------------------

    def kicker_weather_multiplier(
        self, wind_mph: float | None, precip_mm: float | None
    ) -> tuple[float, str]:
        """A bounded penalty on kicking value: wind past ``wind_calm_mph``
        (steepening past ``wind_steep_mph``) plus a precipitation term."""
        wind = float(wind_mph or 0.0)
        precip = float(precip_mm or 0.0)
        penalty = 0.0
        if wind > self.wind_calm_mph:
            penalty += self.wind_mild_rate * (min(wind, self.wind_steep_mph) - self.wind_calm_mph)
        if wind > self.wind_steep_mph:
            penalty += self.wind_steep_rate * (wind - self.wind_steep_mph)
        penalty += self.precip_rate * precip
        mult = max(self.k_weather_floor, 1.0 - penalty)
        if penalty <= 0.0:
            reason = (
                f"WEATHER: {wind:.0f} mph wind at kickoff — below the "
                f"{self.wind_calm_mph:.0f} mph threshold, no kicking penalty. "
                f"[{self.weather_label}; {self.source}]"
            )
        else:
            wet = f", {precip:.1f} mm precip" if precip > 0.0 else ""
            reason = (
                f"WEATHER: {wind:.0f} mph wind at kickoff{wet} — past the "
                f"{self.wind_calm_mph:.0f} mph threshold (steepens beyond "
                f"{self.wind_steep_mph:.0f} mph), field goals get harder; kicking "
                f"value x{mult:.2f}. [{self.weather_label}; {self.source}]"
            )
        return mult, reason

    def dst_weather_multiplier(
        self, wind_mph: float | None, precip_mm: float | None
    ) -> tuple[float, str] | None:
        """A small secondary D/ST bump in genuinely bad weather (turnovers,
        stalled drives). Returns None when the weather is unremarkable."""
        wind = float(wind_mph or 0.0)
        precip = float(precip_mm or 0.0)
        if wind < self.wind_steep_mph and precip <= 0.0:
            return None
        mult = 1.0 + self.dst_weather_bump
        reason = (
            f"WEATHER (secondary): {wind:.0f} mph wind"
            + (f" and {precip:.1f} mm precip" if precip > 0.0 else "")
            + f" tends to help defenses (turnovers, stalled drives); D/ST bumped "
            f"{self.dst_weather_bump:+.0%}. [{self.weather_label}; {self.source}]"
        )
        return mult, reason


DEFAULT_STREAM_ADJUST = StreamAdjust(
    opp_tilt=0.25,
    vegas_tilt=0.12,
    vegas_pivot=22.0,
    vegas_scale=6.0,
    wind_calm_mph=15.0,
    wind_steep_mph=20.0,
    wind_mild_rate=0.012,
    wind_steep_rate=0.030,
    precip_rate=0.020,
    k_weather_floor=0.60,
    dst_weather_bump=0.06,
    opp_label="hypothesis: opponent-offense strength tilts a streamed D/ST +/-25% "
              "at the extremes; NOT fitted to 2026 data, tune in Phase 4",
    vegas_label="hypothesis: opponent implied team total tilts a streamed D/ST "
                "+/-12% around a league-typical 22 points; context only",
    weather_label="hypothesis: kicking value falls with wind above 15 mph "
                  "(steepening past 20 mph) plus a precip term; NOT fitted to 2026 "
                  "data, tune in Phase 4",
    source="item 3.5 design (2026-07-26)",
)


# ------------------------------------------------------------------ output rows


@dataclass(frozen=True)
class StreamRec:
    """One streamable defense or kicker, priced and explained (Rule 6).

    ``house_points`` is a ``scoring.py`` output (via ``weekly_lines``);
    ``stream_score`` is the matchup-adjusted HYPOTHESIS number and the two are
    never conflated.
    """

    position: str                 # canonical DST | K
    player: str
    team: str | None
    espn_id: str | None
    gsis_id: str | None
    opponent: str | None
    house_points: float           # verbatim from weekly_lines (scoring.py)
    stream_score: float           # house_points x labelled matchup multipliers
    rank: int                     # 1-based by stream_score desc
    acquisition: str              # WAIVER | FREE_AGENT | UNKNOWN
    percent_owned: float
    startable_this_week: bool
    reasons: tuple[str, ...]
    # --- item 3.14: the weekly market board, when one covers this candidate ----
    # ``market_ecr`` is the ORDERING key whenever it is not None; ``stream_score``
    # is then a shown-but-not-ranking hypothesis. Both are always displayed, so the
    # operator can see the two disagree rather than being told only the winner.
    market_ecr: float | None = None
    market_rank: int | None = None
    market_label: str | None = None      # upstream's own 'DST4'


@dataclass(frozen=True)
class StreamBoard:
    """One position's ranked streaming shelf for one week."""

    position: str                 # canonical DST | K
    week: int
    ranked: tuple[StreamRec, ...]
    freshness: tuple[str, ...]
    notes: tuple[str, ...]
    as_of: str
    season: int
    odds_available: bool = False
    weather_available: bool = False   # an adjustment was actually APPLIED
    weather_readable: bool = False    # a weather row EXISTED for some candidate game
    # --- item 3.14 ------------------------------------------------------------
    # ``market`` is the board that ORDERED this page (None => the composite did).
    # ``market_alt`` is what IS stored for some other week when ``market`` is None:
    # "no board" and "a board for a week that is not this one" are different
    # problems and only the second one is about to fix itself.
    market: MarketBoard | None = None
    market_alt: MarketBoard | None = None

    @property
    def ranked_on_market(self) -> bool:
        return self.market is not None


# ------------------------------------------------------------- internal helpers


def _norm_team(raw) -> str | None:
    if raw is None:
        return None
    token = str(raw).strip().upper()
    if not token:
        return None
    return base.TEAM_ALIASES.get(token, token)


def _game_index(conn, *, as_of, season, week, view) -> dict[str, dict]:
    """normalized team -> ``{game_id, opponent, is_home}`` for the week's games."""
    out: dict[str, dict] = {}
    for g in get_schedule(conn, as_of=as_of, season=season, week=week, view=view):
        if g["game_type"] != "REG":
            continue
        home = _norm_team(g["home_team"])
        away = _norm_team(g["away_team"])
        gid = g["game_id"]
        if home:
            out[home] = {"game_id": gid, "opponent": away, "is_home": True}
        if away:
            out[away] = {"game_id": gid, "opponent": home, "is_home": False}
    return out


def _rows_by_game(rows: Iterable[Mapping], *, prefer_forecast: bool = False) -> dict[str, dict]:
    """Index injected/accessor rows by ``game_id``. When ``prefer_forecast`` a
    live 'forecast' regime row wins over an 'archive_actual' for the same game."""
    out: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        gid = r.get("game_id")
        if gid is None:
            continue
        if prefer_forecast and gid in out:
            if out[gid].get("forecast_source") == "forecast":
                continue
        out[gid] = r
    return out


def _opponent_implied(odds: Mapping, *, is_home: bool) -> float | None:
    """The opponent's implied team total from a closing line, or None if the line
    is not posted. ``spread_line`` is home-oriented (positive = home favored):
    home_implied = (total + spread)/2, away_implied = (total - spread)/2."""
    total = odds.get("total_line")
    spread = odds.get("spread_line")
    if total is None or spread is None:
        return None
    total, spread = float(total), float(spread)
    # The streamed team's OPPONENT is the other side.
    return (total - spread) / 2.0 if is_home else (total + spread) / 2.0


def _offense_by_team(lines, week: int) -> dict[str, float]:
    """team -> summed QB/RB/WR/TE projected house points for ``week``."""
    out: dict[str, float] = {}
    for line in lines.values():
        if line.position not in _OFFENSE or line.team is None:
            continue
        out[line.team] = out.get(line.team, 0.0) + line.points.get(week, 0.0)
    return out


def _line_for(candidate: Mapping, lines, position: str):
    """The WeeklyLine for a streamed candidate: D/ST joined on normalized team,
    K joined on gsis_id (the projection spine's keys)."""
    if position == "DST":
        return lines.get(("DST", _norm_team(candidate.get("pro_team"))))
    gsis = candidate.get("gsis_id")
    return lines.get(("SKILL", gsis)) if gsis else None


# ------------------------------------------------------------------- the ranker


def rank_streamers(
    conn,
    *,
    as_of,
    season: int,
    position: str,
    week: int | None = None,
    last_week: int = 17,
    source: str = "sleeper_rotowire",
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    adjust: StreamAdjust = DEFAULT_STREAM_ADJUST,
    weather: Sequence[Mapping] | None = None,
    odds: Sequence[Mapping] | None = None,
    view: base.AsOfView = "historical",
    today=None,
) -> StreamBoard:
    """Rank the free-agent D/STs (or kickers) to stream THIS week (item 3.5).

    Rule 1: ``as_of`` is keyword-only, no default; ``view`` threads into every
    accessor. Rule 2: ``house_points`` is ``weekly_lines`` output; ``stream_score``
    is the labelled-hypothesis matchup adjustment, never presented as house points.

    ``week`` defaults to the single current week via ``resolve_weeks`` (which
    RAISES rather than guess a finished week on a waiver Tuesday/Wednesday).
    ``weather``/``odds`` may be injected as explicit row-lists (so the done-when
    can run on synthetic wind while ``game_weather`` is empty); when None the
    corresponding accessor is read under ``view``.
    """
    pos = canon_position(position)
    if pos not in STREAM_POSITIONS:
        raise StreamPositionError(
            f"rank_streamers streams DST or K only; got {position!r}. "
            "(K/DST are the two positions marginal caps at one and streams weekly.)"
        )

    resolved_week = (
        int(week) if week is not None
        else resolve_weeks(conn, as_of=as_of, season=season, last_week=last_week, view=view)[0]
    )

    lines = weekly_lines(
        conn, as_of=as_of, season=season, weeks=[resolved_week], source=source,
        rules=rules, view=view,
    )
    offense = _offense_by_team(lines, resolved_week)
    games = _game_index(conn, as_of=as_of, season=season, week=resolved_week, view=view)
    byes = bye_map(conn, as_of=as_of, season=season, source=source, view=view)

    odds_rows = (
        list(odds) if odds is not None
        else list(get_game_odds(conn, as_of=as_of, season=season, week=resolved_week, view=view))
    )
    weather_rows = (
        list(weather) if weather is not None
        else list(get_game_weather(conn, as_of=as_of, season=season, week=resolved_week, view=view))
    )
    odds_by_game = _rows_by_game(odds_rows)
    weather_by_game = _rows_by_game(weather_rows, prefer_forecast=True)

    live = normalize_as_of(as_of) >= normalize_as_of(
        live_status_from(conn, as_of=as_of, season=season, view=view)
    )

    # ITEM 3.14: the weekly consensus board is the D/ST ranker when one exists for
    # THIS week. ``market_alt`` is only read when it does not, and only to say what
    # the fallback is falling back from.
    market = (
        dst_market_board(conn, as_of=as_of, season=season, week=resolved_week, view=view)
        if pos == "DST" else None
    )
    market_alt = (
        dst_market_board(conn, as_of=as_of, season=season, week=None, view=view)
        if pos == "DST" and market is None else None
    )

    fa_rows = [
        dict(r) for r in league_state.get_free_agents(conn, as_of=as_of, season=season, view=view)
        if canon_position(r["position"]) == pos
    ]

    notes: list[str] = []
    odds_available = False
    weather_available = False
    weather_readable = False
    scored: list[StreamRec] = []
    # (display name, WeeklyLine) per scored candidate, kept index-aligned with
    # ``scored`` and re-ordered with it: item 3.17's staleness banner has to say
    # whether a RANKED row is among the stale ones, and it cannot re-derive the
    # join from a StreamRec (a D/ST line is keyed on team, a kicker's on gsis).
    ranked_lines: list[tuple[str, object]] = []

    for cand in fa_rows:
        name = cand.get("player") or cand.get("espn_player_id") or "?"
        team = _norm_team(cand.get("pro_team"))
        line = _line_for(cand, lines, pos)

        # --- coverage / bye / OUT gates (Rule 6): refuse, never phantom-zero. ----
        # A single-week price cannot tell a bye from a missing forecast (both are
        # simply an absent line), so bye detection reads the whole-span bye_map.
        if line is None or resolved_week not in line.played_weeks:
            why = ("on BYE this week" if byes.bye_of(team) == resolved_week
                   else "no projection / coverage at this as-of")
            notes.append(f"skipped {name} ({team or '-'}) — {why}; not rankable this week.")
            continue
        status = str(cand.get("injury_status") or "").strip().upper()
        if live and status in _HARD_OUT:
            notes.append(f"skipped {name} ({team or '-'}) — ESPN lists him {status} this week.")
            continue

        house_points = line.points.get(resolved_week, 0.0)
        game = games.get(team)
        opponent = game["opponent"] if game else None
        reasons: list[str] = [
            f"house projection {house_points:.1f} pts this week (week {resolved_week}), "
            f"priced through the house scoring engine.",
        ]

        market_rank = market_ecr = market_label = None
        if pos == "DST":
            entry = market.by_team.get(team) if market else None
            if entry is not None:
                market_rank, market_ecr, market_label = entry
                reasons.append(
                    f"WEEKLY MARKET BOARD — THIS is what ranks the page: FantasyPros "
                    f"lists {team} at {market_label or f'#{market_rank}'} for week "
                    f"{resolved_week} (consensus {market_ecr:.2f}"
                    + (f", rank {market_rank}" if market_rank is not None else "")
                    + f"), off the {market.scrape_date} scrape of {market.size} "
                    f"defenses. The matchup composite below is shown but does NOT set "
                    f"this order. [{MARKET_LABEL}]"
                )
            elif market is not None:
                reasons.append(
                    f"WEEKLY MARKET BOARD: {team or 'this team'} is NOT on the week-"
                    f"{resolved_week} FantasyPros D/ST board stored at this as-of "
                    f"({market.scrape_date}, {market.size} defenses), so this row sits "
                    f"BELOW every board row and is ordered on the matchup composite "
                    f"alone. That is an absence of a market opinion, not a low one."
                )
            else:
                reasons.append(
                    f"WEEKLY MARKET BOARD: none stored for week {resolved_week} at this "
                    f"as-of — this page falls back to the opponent-quality composite, "
                    f"which is item 3.14's NO-BOARD FALLBACK and not the primary "
                    f"ranker. Its measured rho is +0.1300 against the board's +0.2701."
                )

        stream_score = house_points
        if pos == "DST":
            # (1) OPPONENT QUALITY — primary. Excludes the opponent's own offense
            # from the reference so the comparison is stable.
            if opponent is not None and opponent in offense:
                opp_pts = offense[opponent]
                # Reference over teams actually PLAYING this week only. A team
                # whose whole offense is on bye lands in ``offense`` at 0.0 but is
                # absent from ``games`` (no schedule row) — including those zeros
                # inflated the reference spread ~3.5x, crushing the primary tilt
                # toward zero and printing a wrong "league average" (item 3.5 audit).
                reference = [v for t, v in offense.items()
                             if t != opponent and t in games]
                mult, reason = adjust.opponent_multiplier(opp_pts, reference, opponent)
                stream_score *= mult
                reasons.append(reason)
            else:
                reasons.append(
                    "OPPONENT MATCHUP: this week's opponent offense could not be "
                    "resolved (schedule or projections missing) — matchup tilt not "
                    "applied."
                )
            # (2) VEGAS — context, leakage-fenced.
            odds_row = odds_by_game.get(game["game_id"]) if game else None
            opp_implied = (
                _opponent_implied(odds_row, is_home=game["is_home"])
                if odds_row is not None and game else None
            )
            if opp_implied is not None:
                odds_available = True
                mult, reason = adjust.vegas_multiplier(opp_implied, opponent or "the opponent")
                stream_score *= mult
                reasons.append(reason)
            else:
                reasons.append(
                    "VEGAS: line not yet posted at this as-of (closing lines become "
                    "knowable on gameday); rank refreshes when the line posts."
                )
            # (3) WEATHER — secondary for D/ST.
            wx = weather_by_game.get(game["game_id"]) if game else None
            if wx is not None:
                weather_readable = True          # the feed was fully KNOWN
            if wx is not None and wx.get("weather_relevant"):
                got = adjust.dst_weather_multiplier(wx.get("wind_mph"), wx.get("precip_mm"))
                if got is not None:
                    weather_available = True      # a bump was actually APPLIED
                    mult, reason = got
                    stream_score *= mult
                    reasons.append(reason)
        else:  # K — weather PRIMARY (the demonstrable done-when).
            wx = weather_by_game.get(game["game_id"]) if game else None
            if wx is not None:
                weather_readable = True
            if wx is not None and wx.get("weather_relevant"):
                weather_available = True
                mult, reason = adjust.kicker_weather_multiplier(
                    wx.get("wind_mph"), wx.get("precip_mm")
                )
                stream_score *= mult
                reasons.append(reason)
            elif wx is not None:
                reasons.append("WEATHER: dome / weather-irrelevant game — no adjustment.")
            else:
                reasons.append(
                    "WEATHER: forecast not available at this as-of — kicking value "
                    "not adjusted; rank uses the house projection only."
                )

        reasons.append(
            f"stream score {stream_score:.1f} = matchup-adjusted expected value "
            f"(HYPOTHESIS — not house scoring; {house_points:.1f} house pts x labelled "
            f"matchup multipliers)"
            + (" — SHOWN ONLY: the weekly market board above sets this page's order."
               if market_ecr is not None else ".")
        )
        acquisition = classify_acquisition(cand.get("roster_status"))
        reasons.append(_acq_reason(acquisition))

        scored.append(StreamRec(
            position=pos,
            player=str(name),
            team=team,
            espn_id=(str(cand["espn_player_id"]) if cand.get("espn_player_id") is not None else None),
            gsis_id=(str(cand["gsis_id"]) if cand.get("gsis_id") else None),
            opponent=opponent,
            house_points=house_points,
            stream_score=stream_score,
            rank=0,
            acquisition=acquisition,
            percent_owned=float(cand.get("percent_owned") or 0.0),
            startable_this_week=True,
            reasons=tuple(reasons),
            market_ecr=market_ecr,
            market_rank=market_rank,
            market_label=market_label,
        ))
        ranked_lines.append((str(name), line))

    # THE ORDER (item 3.14). A row the weekly board covers is ranked by the board;
    # a row it does not cover falls BELOW every board row and keeps the composite's
    # order among its own kind. With no board at all every row takes the second
    # branch, which is byte-identical to the pre-3.14 sort — the fallback is the old
    # ranker, unchanged, not a re-implementation of it.
    def _order(r: StreamRec):
        return (
            0 if r.market_ecr is not None else 1,
            r.market_ecr if r.market_ecr is not None else 0.0,
            -r.stream_score, -r.house_points, r.player,
        )

    order = sorted(range(len(scored)), key=lambda i: _order(scored[i]))
    ranked = tuple(
        StreamRec(**{**scored[i].__dict__, "rank": position})
        for position, i in enumerate(order, start=1)
    )
    ranked_lines = [ranked_lines[i] for i in order]

    if not live:
        notes.append(
            "preseason: ESPN injury tags are roster labels this early, not game "
            "designations, so OUT tags are IGNORED here."
        )
    if not ranked:
        notes.append(
            f"no streamable {pos} free agent could be priced at this as-of — every "
            "candidate is on bye, ruled out, or unprojected. Verify manually."
        )
    if pos == "DST" and market is None:
        alt = ""
        if market_alt is not None:
            alt = (
                f" The newest D/ST board stored at this as-of is the "
                f"{market_alt.scrape_date} scrape, which ranks week "
                f"{market_alt.week if market_alt.week is not None else '?'} — it is NOT "
                f"served for week {resolved_week}: a board for another week is a "
                f"season-long list wearing a weekly label."
            )
        else:
            alt = (
                " No D/ST board of any week is stored at this as-of (`ziggurat ingest "
                "status` names the `fp_weekly_ecr` pull; it is captured daily and a "
                "missed day is a LOST observation, not staleness)."
            )
        notes.append(
            f"FALLBACK RANKER: no FantasyPros weekly D/ST board for week "
            f"{resolved_week} is readable at this as-of, so this page is ordered by "
            f"the opponent-quality composite — item 3.14's no-board fallback (rho "
            f"+0.1300 vs the board's +0.2701), not its primary ranker." + alt
        )
    elif pos == "DST" and ranked:
        # Only when something was actually ranked: "ranked on the market board"
        # printed above an empty table is a claim about nothing.
        missing = [r.player for r in ranked if r.market_ecr is None]
        notes.append(
            f"RANKED ON THE WEEKLY MARKET BOARD: FantasyPros' week-{resolved_week} "
            f"D/ST consensus, {market.scrape_date} scrape, {market.size} defenses "
            f"(item 3.14). The STREAM* column is the matchup composite and is shown "
            f"for contrast only — it does not order this page."
            + (f" {len(missing)} candidate(s) are absent from that board and sit below "
               f"every board row: {', '.join(missing[:4])}"
               f"{'...' if len(missing) > 4 else ''}." if missing else "")
        )
    if pos == "DST" and not odds_available:
        notes.append(
            "Vegas lines are not posted yet — the matchup tilt uses opponent "
            "projections only (this is expected before gameday)."
        )
    if not weather_readable:
        notes.append(
            "weather forecast not available — "
            + ("kicking wind/precip adjustment not applied."
               if pos == "K" else "the secondary D/ST weather bump was skipped.")
        )
    elif not weather_available:
        notes.append(
            "all candidate games are indoors (or weather-irrelevant) — weather was "
            "fully known and correctly not applicable; this is NOT a data gap."
        )

    freshness = tuple(_freshness_lines(
        lines, fa_rows, as_of=as_of, today=today, conn=conn, season=season,
        ranked_lines=ranked_lines,
    ))

    return StreamBoard(
        position=pos,
        week=resolved_week,
        ranked=ranked,
        freshness=freshness,
        notes=tuple(notes),
        as_of=normalize_as_of(as_of).isoformat(),
        season=int(season),
        odds_available=odds_available,
        weather_available=weather_available,
        weather_readable=weather_readable,
        market=market,
        market_alt=market_alt,
    )


def _acq_reason(acquisition: str) -> str:
    if acquisition == ACQ_WAIVER:
        return ("WAIVERS claim — queue it (free, non-FAAB, overnight batch); a won "
                "claim resets your waiver priority to worst-in-league.")
    if acquisition == ACQ_FREE_AGENT:
        return "FREE AGENT — first-come-first-served; grab him now, speed matters."
    return ("UNRECOGNIZED roster status — verify in the ESPN app whether this is a "
            "waiver claim or a free-agent grab before acting.")


# ------------------------------------------------------------------- staleness


def _freshness_lines(lines, fa_rows, *, as_of, today, conn, season,
                     ranked_lines=()) -> list[str]:
    """Projection + league-state pull recency, plus item 3.1b's per-source
    contract. A July projection pricing a November stream is Rule-1-invisible.

    ITEM 3.17 DELIVERABLE 3. The warning used to read "some projections on this
    board are N days old" off the OLDEST pull anywhere in ``lines`` — which on the
    live 2026-09-15 board was ONE orphan row of 3,229, and rendered as a blanket
    "do not trust this page". A staleness banner that cannot be checked is a
    banner the operator learns to skip, so it now states the COUNT and, decisively,
    whether any RANKED candidate is among the stale rows: a stale bench body
    nobody is ranking is not a reason to distrust the order.
    """
    out: list[str] = []
    cutoff = normalize_as_of(as_of)

    pulled = sorted({d for line in lines.values() for d in line.retrieved_as_of})
    if pulled:
        gap = (cutoff - normalize_as_of(pulled[0])).days
        newest = (cutoff - normalize_as_of(pulled[-1])).days
        out.append(f"projections: pulled {pulled[-1]} — {_plural(newest, 'day')} before {as_of}")
        if gap > STALE_BANNER_DAYS:
            stale = [
                line for line in lines.values()
                if line.retrieved_as_of
                and (cutoff - normalize_as_of(min(line.retrieved_as_of))).days
                > STALE_BANNER_DAYS
            ]
            stale_ids = {id(line) for line in stale}
            hit = [name for name, line in ranked_lines if id(line) in stale_ids]
            out.append(
                f"  WARNING: {len(stale)} of {len(lines)} projection rows on this board "
                f"carry a pull older than {STALE_BANNER_DAYS} days — the oldest is "
                f"{gap} days old (pulled {pulled[0]})."
            )
            if hit:
                shown = ", ".join(hit[:4]) + ("..." if len(hit) > 4 else "")
                out.append(
                    f"  {len(hit)} of them IS a ranked candidate on this page ({shown}) "
                    f"— this rank IS affected; run `ziggurat ingest run` before trusting "
                    f"it." if len(hit) == 1 else
                    f"  {len(hit)} of them ARE ranked candidates on this page ({shown}) "
                    f"— this rank IS affected; run `ziggurat ingest run` before trusting "
                    f"it."
                )
            else:
                out.append(
                    "  NO ranked candidate on this page is among them, so the ORDER "
                    "above is not affected — this is a data-hygiene note, not a reason "
                    "to distrust the rank. `ziggurat ingest run` clears it."
                )
    else:
        out.append("projections: NONE readable at this as-of")

    state_days = sorted({r.get("retrieved_as_of") for r in fa_rows if r.get("retrieved_as_of")})
    if state_days:
        gap = (cutoff - normalize_as_of(state_days[-1])).days
        out.append(f"league state: pulled {state_days[-1]} — {_plural(gap, 'day')} before {as_of}")
        if gap > STALE_BANNER_DAYS:
            out.append(
                f"  WARNING: your free-agent pool is {gap} days stale — run "
                "`ziggurat league sync`."
            )
    return out


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --------------------------------------------------------------------- display


def format_stream_board(board: StreamBoard, *, top: int | None = None,
                        reasons: bool = False) -> str:
    """Render one position's streaming shelf (display only — Rule 3).

    A DEGRADATION banner leads whenever the context sources (Vegas / weather) the
    rank would have used are absent, so the operator never mistakes a
    projection-only rank for a fully-adjusted one.
    """
    label = "D/ST" if board.position == "DST" else board.position
    out = [
        f"streaming {label} — season {board.season}, week {board.week}, as of {board.as_of}",
    ]
    out.extend(board.freshness)

    degraded: list[str] = []
    if board.position == "DST" and not board.odds_available:
        degraded.append("Vegas lines not posted (matchup tilt from projections only)")
    # DEGRADED is a true DATA GAP: no weather row was readable at all. An all-dome
    # slate (weather fully known, correctly inapplicable) is NOT degraded.
    if not board.weather_readable:
        degraded.append(
            "kicker weather adjustment not applied" if board.position == "K"
            else "secondary weather bump skipped"
        )
    if degraded:
        out.append("! DEGRADED: " + "; ".join(degraded))

    # ITEM 3.14: the three sentences the card MUST carry, above the table, before
    # the operator has read a single name. They are the price of the ranking, and
    # a price printed under the rows is a price nobody reads.
    if board.position == "DST":
        out.append("")
        out.append("WHAT THIS RANKING IS (item 3.14 — all three, every time):")
        out.extend(f"  * {sentence}" for sentence in DST_CARD_SENTENCES)
        out.append(f"  * {DST_POSTURE_NOTE}")
    if board.ranked:
        out.append("")
        out.append(f"  * {ONE_HORIZON_NOTE}")

    out.append("")
    if board.position == "DST":
        out.append(f"{'#':<3} {'PLAYER':<22} {'NFL':<4} {'OPP':<4} {'HOUSE':>7} "
                   f"{'STREAM*':>8} {'MKT':>6} {'%OWN':>6}  ACQ")
        if board.market is not None:
            mkt_week = board.market.week if board.market.week is not None else board.week
            out.append(
                f"  MKT = FantasyPros week-{mkt_week} D/ST CONSENSUS, lower is better "
                f"({board.market.scrape_date} scrape) — THIS orders the page (item 3.14)"
            )
            out.append(
                "        the consensus is an average of many analysts, so it can put a "
                "defense a place or two away from upstream's own printed DSTn label "
                "(shown per row under --reasons); the average is what was measured"
            )
            out.append("  * STREAM = matchup-adjusted expected value (HYPOTHESIS — not "
                       "house scoring); shown for contrast, it does NOT order the page")
        else:
            out.append("  MKT = FantasyPros weekly D/ST consensus rank — NONE stored for "
                       "this week at this as-of (see the note below)")
            out.append("  * STREAM = matchup-adjusted expected value (HYPOTHESIS — not "
                       "house scoring); with no market board it orders the page (the "
                       "item-3.14 FALLBACK)")
    else:
        out.append(f"{'#':<3} {'PLAYER':<22} {'NFL':<4} {'OPP':<4} {'HOUSE':>7} "
                   f"{'STREAM*':>8} {'%OWN':>6}  ACQ")
        out.append("  * STREAM = matchup-adjusted expected value (HYPOTHESIS — not house scoring)")

    rows = board.ranked if top is None else board.ranked[:top]
    if not rows:
        out.append("  (no streamable candidate could be priced this week)")
    for rec in rows:
        head = (
            f"{rec.rank:<3} {rec.player[:22]:<22} {(rec.team or '-'):<4} "
            f"{(rec.opponent or '-'):<4} {rec.house_points:>7.1f} {rec.stream_score:>8.1f} "
        )
        if board.position == "DST":
            # The CONSENSUS, not upstream's integer label — the consensus is what
            # orders the page and what the measurement was taken on, and a column
            # that is not monotone down its own ordering is a column a reader
            # stops believing. The label rides in the row's reasons.
            mkt = f"{rec.market_ecr:.2f}" if rec.market_ecr is not None else "-"
            head += f"{mkt:>6} "
        out.append(head + f"{rec.percent_owned:>6.1f}  {rec.acquisition}")
        if reasons:
            out.extend(f"      - {r}" for r in rec.reasons)

    for note in board.notes:
        out.append(f"! {note}")
    return "\n".join(out)
