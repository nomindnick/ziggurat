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


class StreamPositionError(ValueError):
    """``position`` was neither 'DST' nor 'K'. Raised rather than guessing."""


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

    fa_rows = [
        dict(r) for r in league_state.get_free_agents(conn, as_of=as_of, season=season, view=view)
        if canon_position(r["position"]) == pos
    ]

    notes: list[str] = []
    odds_available = False
    weather_available = False
    weather_readable = False
    scored: list[StreamRec] = []

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
            f"matchup multipliers)."
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
        ))

    scored.sort(key=lambda r: (-r.stream_score, -r.house_points, r.player))
    ranked = tuple(
        StreamRec(**{**r.__dict__, "rank": i}) for i, r in enumerate(scored, start=1)
    )

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

    freshness = tuple(_freshness_lines(lines, fa_rows, as_of=as_of, today=today, conn=conn, season=season))

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


def _freshness_lines(lines, fa_rows, *, as_of, today, conn, season) -> list[str]:
    """Projection + league-state pull recency, plus item 3.1b's per-source
    contract. A July projection pricing a November stream is Rule-1-invisible."""
    out: list[str] = []
    cutoff = normalize_as_of(as_of)

    pulled = sorted({d for line in lines.values() for d in line.retrieved_as_of})
    if pulled:
        gap = (cutoff - normalize_as_of(pulled[0])).days
        newest = (cutoff - normalize_as_of(pulled[-1])).days
        out.append(f"projections: pulled {pulled[-1]} — {_plural(newest, 'day')} before {as_of}")
        if gap > STALE_BANNER_DAYS:
            out.append(
                f"  WARNING: some projections on this board are {gap} days old (oldest "
                f"pull {pulled[0]}) — run `ziggurat ingest run` before trusting the rank."
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

    out.append("")
    out.append(f"{'#':<3} {'PLAYER':<22} {'NFL':<4} {'OPP':<4} {'HOUSE':>7} "
               f"{'STREAM*':>8} {'%OWN':>6}  ACQ")
    out.append("  * STREAM = matchup-adjusted expected value (HYPOTHESIS — not house scoring)")

    rows = board.ranked if top is None else board.ranked[:top]
    if not rows:
        out.append("  (no streamable candidate could be priced this week)")
    for rec in rows:
        out.append(
            f"{rec.rank:<3} {rec.player[:22]:<22} {(rec.team or '-'):<4} "
            f"{(rec.opponent or '-'):<4} {rec.house_points:>7.1f} {rec.stream_score:>8.1f} "
            f"{rec.percent_owned:>6.1f}  {rec.acquisition}"
        )
        if reasons:
            out.extend(f"      - {r}" for r in rec.reasons)

    for note in board.notes:
        out.append(f"! {note}")
    return "\n".join(out)
