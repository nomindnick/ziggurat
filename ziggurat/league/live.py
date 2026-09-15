"""Live in-game scoreboard read (item 3.17, deliverable 1).

WHY THIS EXISTS. The scheduled sync cannot see live points: ESPN serves
``totalPoints`` as ``0.0`` until the scoring period closes, so ``league_matchups``
reads 0-0 all Sunday (Week-1 journal D6). The live number lives on a different
field of a different view — ``totalPointsLive`` on ``mMatchupScore`` +
``mBoxscore``, with per-player ``appliedStatTotal`` on
``rosterForCurrentScoringPeriod`` — and the Week-1 Sunday was driven off two
scratch scripts that read exactly that. This module folds those scripts in so the
Sunday loop does not depend on a file in /tmp.

READ-ONLY BY CONSTRUCTION. Nothing here writes a row, applies a migration, or
touches an as-of partition: it is one GET plus two reads of already-stored facts
(``schedules`` for kickoffs, ``league_teams`` for names). The STORED live-score
fix is item 3.8 wave B's ``map_matchup`` coalesce; this is the command that shows
the number while the games are on.

TWO THINGS ARE LABELLED RATHER THAN ASSERTED (Rule 6 — the operator is a novice
and cannot smell a wrong one):

* **Game status is a CLOCK ESTIMATE, not a live game-state read.** The fantasy
  views carry no per-game clock, so ``final`` / ``in progress`` / ``yet to play``
  is derived from the stored ET kickoff plus a fixed elapsed window
  (``TYPICAL_GAME_MINUTES``). An overtime game or a long weather delay reads
  ``final`` early. The renderer says so every time.
* **The projection column is ESPN's own**, taken from the same payload
  (``statSourceId == 1``), NOT the house projection the lineup card prices with.
  ``league/`` must not import ``core/`` (the dependency direction is
  ``core -> league -> data``), and quietly labelling someone else's number as
  ours is exactly the sort of thing a novice cannot check.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ziggurat.data.nfl import base
from ziggurat.data.nfl.schedules import get_schedule
from ziggurat.league import source, state

#: nflverse ``schedules.gametime`` is Eastern wall-clock (the convention
#: ``data/nfl/weather.py`` verified and ``core/lineup_support.py`` follows).
#: Written fresh here rather than imported: ``league/`` may not import ``core/``.
SCHEDULE_TZ = "America/New_York"

#: LABELLED HYPOTHESIS. Minutes after kickoff at which a game is treated as over.
#: ~3h15m is a typical NFL broadcast; overtime and weather delays run longer, so a
#: game that went to OT can read ``final`` while it is still being played. This is
#: the one number in the module that is a guess, and it is quoted in the output.
TYPICAL_GAME_MINUTES = 195

GAME_STATUS_LABEL = (
    f"HYPOTHESIS: game status is derived from the STORED kickoff time (ET) and a "
    f"fixed {TYPICAL_GAME_MINUTES}-minute window, not from a live game clock — "
    f"ESPN's fantasy views carry none. An overtime or weather-delayed game reads "
    f"'final' early."
)

PROJECTION_LABEL = (
    "PROJ is ESPN's OWN weekly projection, read from the same payload — not the "
    "house projection `ziggurat lineup` seats with. The two disagree by design."
)

#: Points of slack allowed between ESPN's live team total and the sum of the
#: starters this module decoded, before the render calls it a disagreement.
SUM_TOLERANCE = 0.05

STATUS_FINAL = "final"
STATUS_LIVE = "in progress"
STATUS_UPCOMING = "yet to play"
STATUS_BYE = "no game"
STATUS_UNKNOWN = "kickoff unknown"

#: ESPN stat rows: 0 = actual, 1 = projected; split type 1 = the single week.
_STAT_SOURCE_ACTUAL = 0
_STAT_SOURCE_PROJECTED = 1


class LiveMatchupUnavailable(RuntimeError):
    """The live read could not be resolved into a matchup for this team."""


# ------------------------------------------------------------------ dataclasses


@dataclass(frozen=True)
class LiveStarter:
    """One roster entry as the live payload carries it."""

    slot: str | None
    starting: bool
    player: str
    espn_player_id: str | None
    pro_team: str | None
    points: float | None          # realised so far (ESPN appliedStatTotal)
    projected: float | None       # ESPN's own projection for this period
    status: str
    kickoff: str | None           # ISO ET

    @property
    def gap(self) -> float | None:
        """Realised minus ESPN-projected, or None when either side is missing."""
        if self.points is None or self.projected is None:
            return None
        return round(self.points - self.projected, 2)


@dataclass(frozen=True)
class LiveSide:
    """One side of the matchup."""

    team_id: int
    team_name: str | None
    live_points: float | None
    final_points: float | None    # ESPN totalPoints — 0.0 until the period closes
    points_source: str            # 'totalPointsLive' | 'totalPoints' | 'unavailable'
    starters: tuple[LiveStarter, ...]
    bench: tuple[LiveStarter, ...]

    @property
    def starters_total(self) -> float:
        return round(sum(s.points or 0.0 for s in self.starters), 2)

    def count(self, status: str) -> int:
        return sum(1 for s in self.starters if s.status == status)

    @property
    def label(self) -> str:
        return self.team_name or f"team {self.team_id}"


@dataclass(frozen=True)
class LiveMatchup:
    season: int
    week: int | None
    scoring_period: int | None
    own: LiveSide
    opponent: LiveSide | None
    closed: bool
    winner: str | None
    read_at: str
    notes: tuple[str, ...]

    @property
    def margin(self) -> float | None:
        if self.opponent is None or self.own.live_points is None \
                or self.opponent.live_points is None:
            return None
        return round(self.own.live_points - self.opponent.live_points, 2)


# ------------------------------------------------------------------ helpers


def _et_kickoff(gameday, gametime) -> datetime | None:
    """A schedules ``(gameday, gametime)`` pair as a tz-aware ET datetime, or None.

    Keyed on the kickoff datetime and never on a weekday: the 2026 opener was a
    Wednesday."""
    if not gameday:
        return None
    day = base.iso_date(gameday)
    if not day:
        return None
    hhmm = str(gametime).strip() if gametime else "13:00"
    try:
        hour = int(hhmm[:2])
        minute = int(hhmm[3:5]) if len(hhmm) >= 5 else 0
    except (ValueError, TypeError):
        hour, minute = 13, 0
    try:
        return datetime.fromisoformat(day).replace(
            hour=hour, minute=minute, tzinfo=ZoneInfo(SCHEDULE_TZ)
        )
    except (ValueError, ZoneInfoNotFoundError):
        return None


def week_kickoffs(conn, *, as_of, season, week, view: base.AsOfView = "historical"
                  ) -> dict[str, datetime]:
    """normalized pro-team abbr -> tz-aware ET kickoff for that week's REG games.

    A team on bye is simply ABSENT, which is what makes a bye detectable
    downstream (an absent row, never a blank one)."""
    out: dict[str, datetime] = {}
    if week is None:
        return out
    for game in get_schedule(conn, as_of=as_of, season=season, week=week, view=view):
        if game["game_type"] != "REG":
            continue
        kick = _et_kickoff(game["gameday"], game["gametime"])
        if kick is None:
            continue
        for team in (game["home_team"], game["away_team"]):
            norm = state._norm_team(team)
            if norm is not None:
                out[norm] = kick
    return out


def _game_status(kickoff, *, now, known_schedule: bool, points) -> str:
    """Clock-derived game status for one starter. See ``GAME_STATUS_LABEL``.

    The ``points`` argument is a guard, not a signal: if the stored kickoff says
    the game has not started but ESPN has already applied points, the stored
    kickoff is the thing that is wrong, and saying 'yet to play' about a player
    who has already scored is the one output a novice WOULD smell."""
    if kickoff is None:
        return STATUS_BYE if known_schedule else STATUS_UNKNOWN
    if now < kickoff:
        return STATUS_LIVE if (points or 0.0) != 0.0 else STATUS_UPCOMING
    if now < kickoff + timedelta(minutes=TYPICAL_GAME_MINUTES):
        return STATUS_LIVE
    return STATUS_FINAL


def _applied_totals(pool_entry, *, scoring_period) -> tuple[float | None, float | None]:
    """(realised, ESPN-projected) for one ``playerPoolEntry``.

    Realised prefers the entry's own ``appliedStatTotal`` (what the Week-1 scratch
    read and what ESPN's own box score shows) and falls back to the actual stat
    row. A missing projection stays None rather than becoming 0.0 — "ESPN has no
    projection for him" and "ESPN projects him at zero" are different facts."""
    realised = pool_entry.get("appliedStatTotal")
    projected = None
    for row in pool_entry.get("player", {}).get("stats") or []:
        if scoring_period is not None and row.get("scoringPeriodId") != scoring_period:
            continue
        source_id = row.get("statSourceId")
        if source_id == _STAT_SOURCE_PROJECTED and projected is None:
            projected = row.get("appliedTotal")
        elif source_id == _STAT_SOURCE_ACTUAL and realised is None:
            realised = row.get("appliedTotal")
    return _round(realised), _round(projected)


def _round(value):
    """None-preserving 2dp round. A non-numeric cell becomes None, never 0.0 —
    the whole module's stance is that 'we do not know' must not render as a
    number the operator would read as a fact."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _entry_to_starter(entry, *, scoring_period, kickoffs, now, known_schedule) -> LiveStarter:
    pool_entry = entry.get("playerPoolEntry") or {}
    player = pool_entry.get("player") or {}
    slot = state.decode_slot(entry.get("lineupSlotId"))
    points, projected = _applied_totals(pool_entry, scoring_period=scoring_period)
    pro_team = state._norm_team(_pro_abbr(player.get("proTeamId")))
    kickoff = kickoffs.get(pro_team) if pro_team else None
    return LiveStarter(
        slot=slot,
        starting=state.is_starting_slot(slot),
        player=player.get("fullName") or f"player {player.get('id')}",
        espn_player_id=None if player.get("id") is None else str(player.get("id")),
        pro_team=pro_team,
        points=points,
        projected=projected,
        status=_game_status(kickoff, now=now, known_schedule=known_schedule, points=points),
        kickoff=None if kickoff is None else kickoff.isoformat(timespec="minutes"),
    )


def _pro_abbr(pro_team_id):
    if pro_team_id in (None, 0):
        return None
    try:
        return state._pro_team_map().get(int(pro_team_id))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _side(raw, *, team_names, scoring_period, kickoffs, now, known_schedule) -> LiveSide:
    entries = (raw.get("rosterForCurrentScoringPeriod") or {}).get("entries") or []
    rows = [
        _entry_to_starter(e, scoring_period=scoring_period, kickoffs=kickoffs,
                          now=now, known_schedule=known_schedule)
        for e in entries
    ]
    live = raw.get("totalPointsLive")
    final = raw.get("totalPoints")
    if live is not None:
        source_label = "totalPointsLive"
    elif final is not None:
        live, source_label = final, "totalPoints"
    else:
        source_label = "unavailable"
    team_id = raw.get("teamId")
    return LiveSide(
        team_id=team_id,
        team_name=team_names.get(team_id),
        live_points=None if live is None else round(float(live), 2),
        final_points=None if final is None else round(float(final), 2),
        points_source=source_label,
        starters=tuple(r for r in rows if r.starting),
        bench=tuple(r for r in rows if not r.starting),
    )


def _find_matchup(payload, *, team_id, week):
    """The schedule entry for this team in this matchup period, and the sides."""
    for raw in payload.get("schedule") or []:
        if week is not None and raw.get("matchupPeriodId") != week:
            continue
        home, away = raw.get("home") or {}, raw.get("away") or {}
        if home.get("teamId") == team_id:
            return raw, home, (away or None)
        if away.get("teamId") == team_id:
            return raw, away, (home or None)
    return None, None, None


def resolve_week(payload, *, week=None) -> int | None:
    """The matchup period to read: explicit override, ESPN's own current period,
    then the scoring period as a last resort (they coincide in the regular
    season and diverge only across multi-week playoff matchups)."""
    if week is not None:
        return int(week)
    current = (payload.get("status") or {}).get("currentMatchupPeriod")
    if current is not None:
        return int(current)
    period = payload.get("scoringPeriodId")
    return None if period is None else int(period)


# ------------------------------------------------------------------ build


def build_live_matchup(
    conn,
    payload,
    *,
    season: int,
    own_team_id: int,
    as_of,
    now: datetime,
    week=None,
    view: base.AsOfView = "historical",
) -> LiveMatchup:
    """Turn one raw live payload into the rendered-ready matchup.

    ``as_of`` gates the two STORED reads (kickoffs, team names) exactly like every
    other accessor (Rule 1); ``now`` is the decision clock that decides which
    games have kicked off, and is deliberately a separate argument."""
    week = resolve_week(payload, week=week)
    scoring_period = payload.get("scoringPeriodId")
    raw, own_raw, opp_raw = _find_matchup(payload, team_id=own_team_id, week=week)
    if raw is None:
        raise LiveMatchupUnavailable(
            f"no matchup for team {own_team_id} in matchup period {week} — ESPN's "
            "schedule carries none (wrong --team, wrong --week, or the season has "
            "not started). `ziggurat league status` shows the last stored snapshot."
        )

    nfl_week = scoring_period if scoring_period else week
    kickoffs = week_kickoffs(conn, as_of=as_of, season=season, week=nfl_week, view=view)
    known_schedule = bool(kickoffs)
    team_names = {
        int(r["team_id"]): r["name"]
        for r in state.get_team_state(conn, as_of=as_of, season=season, view=view)
        if r["team_id"] is not None
    }

    def _build(side_raw):
        if not side_raw:
            return None
        return _side(side_raw, team_names=team_names, scoring_period=scoring_period,
                     kickoffs=kickoffs, now=now, known_schedule=known_schedule)

    own = _build(own_raw)
    opponent = _build(opp_raw)

    winner = raw.get("winner")
    closed = winner not in (None, "", "UNDECIDED")

    notes: list[str] = []
    if not known_schedule:
        notes.append(
            f"NO KICKOFF TIMES: `schedules` has no REG rows for {season} week "
            f"{nfl_week} at as_of={as_of}, so every starter reads "
            f"'{STATUS_UNKNOWN}'. Run `ziggurat ingest run --source schedules`."
        )
    if own is not None and own.points_source == "totalPoints":
        notes.append(
            "ESPN served no `totalPointsLive` on this read — the number shown is "
            "`totalPoints`, which reads 0.0 until the scoring period closes."
        )
    if own is not None and own.points_source == "unavailable":
        notes.append(
            "ESPN served NEITHER `totalPointsLive` nor `totalPoints` for your side; "
            "the per-starter column is all this read can vouch for."
        )
    if closed:
        notes.append(
            f"This matchup is FINAL (ESPN winner={winner}). The live fields stop "
            "moving now; `ziggurat league sync` stores the final score in "
            "`league_matchups`, and THAT stored value — not this command — is the "
            "one to cite from here on."
        )
    if opponent is None:
        notes.append(
            "This matchup has no opponent side in the payload (a bye week in the "
            "league schedule, or ESPN served a half matchup)."
        )
    for who, side in (("your", own), ("the opponent's", opponent)):
        if side is not None and not side.starters and not side.bench:
            notes.append(
                f"ESPN served NO roster rows for {who} side — the per-player table "
                "below is empty because the mBoxscore view came back without "
                "`rosterForCurrentScoringPeriod`, not because nobody is playing."
            )
    if week is not None and scoring_period is not None and week != scoring_period:
        notes.append(
            f"The matchup shown is period {week} but the roster rows ESPN served are "
            f"the CURRENT scoring period ({scoring_period}) — pass --scoring-period "
            f"{week} to pin them, or the per-starter table describes a different week "
            "from the score above it."
        )
    # SELF-CHECK. ESPN's live team total IS the sum of the starters' applied
    # totals, so a disagreement is not a rounding curiosity — it means this
    # module decoded the lineup slots wrongly and is showing the operator a
    # starter set his league does not have. Report it; never quietly pick one.
    for who, side in (("your", own), ("the opponent's", opponent)):
        if side is None or side.live_points is None:
            continue
        drift = round(side.live_points - side.starters_total, 2)
        if abs(drift) > SUM_TOLERANCE:
            notes.append(
                f"DISAGREEMENT on {who} side: ESPN's {side.points_source} is "
                f"{side.live_points:.2f} but the starters shown sum to "
                f"{side.starters_total:.2f} ({drift:+.2f}). ESPN's total is the "
                "authority; the per-starter rows above have mis-read a lineup slot "
                "or are missing a player."
            )

    notes.append(GAME_STATUS_LABEL)
    notes.append(PROJECTION_LABEL)

    return LiveMatchup(
        season=season, week=week, scoring_period=scoring_period,
        own=own, opponent=opponent, closed=closed, winner=winner,
        read_at=now.isoformat(timespec="seconds"), notes=tuple(notes),
    )


def read_live_matchup(
    conn,
    *,
    season: int,
    league_id: int,
    espn_s2,
    swid,
    as_of,
    now: datetime,
    team_id=None,
    week=None,
    scoring_period=None,
    view: base.AsOfView = "historical",
    fetch=None,
) -> LiveMatchup:
    """Resolve the team, pull one live payload, and build the matchup.

    The operator's own team is resolved the way `league sync` resolves it — from
    the SWID against the stored ``league_teams.primary_owner`` — so no team number
    is ever hard-coded; ``team_id`` overrides it. ``fetch`` is the injection seam
    the offline tests use in place of the network."""
    if team_id is None:
        team_id = state.resolve_own_team(
            conn, as_of=as_of, season=season, swid=swid, view=view
        )
    fetcher = fetch or source.fetch_live_scoreboard
    payload = fetcher(
        league_id=league_id, season=season, espn_s2=espn_s2, swid=swid,
        scoring_period=scoring_period,
    )
    return build_live_matchup(
        conn, payload, season=season, own_team_id=int(team_id),
        as_of=as_of, now=now, week=week, view=view,
    )


def now_et(stamp=None) -> datetime:
    """The decision clock in ET. An explicit ISO ``stamp`` wins (naive input is
    read AS Eastern, matching the schedules convention); otherwise the wall clock.
    The CLI is where the operator's implicit 'now' is made explicit."""
    if stamp:
        moment = datetime.fromisoformat(str(stamp))
        if moment.tzinfo is None:
            return moment.replace(tzinfo=ZoneInfo(SCHEDULE_TZ))
        return moment.astimezone(ZoneInfo(SCHEDULE_TZ))
    return datetime.now(ZoneInfo(SCHEDULE_TZ))


# ------------------------------------------------------------------ render


def _fmt(value, width=6, places=1) -> str:
    return " " * width if value is None else f"{value:>{width}.{places}f}"


def _status_cell(row: LiveStarter) -> str:
    """The status, plus the kickoff for a player who has not started yet — "when
    does he play" is the question the Sunday read is actually being asked, and
    the Week-1 scratch answered it by printing raw kickoff strings."""
    if row.status == STATUS_UPCOMING and row.kickoff:
        return f"{row.status} {row.kickoff[11:16]}ET"
    return row.status


def _side_block(side: LiveSide, *, title: str, bench: bool) -> list[str]:
    out = [f"{title}: {side.label}"]
    out.append(f"  {'SLOT':<5} {'PLAYER':<24} {'NFL':<4} {'STATUS':<20} "
               f"{'LIVE':>6} {'PROJ':>6} {'GAP':>7}")
    rows = list(side.starters) + (list(side.bench) if bench else [])
    for row in rows:
        tag = row.slot or "?"
        if not row.starting:
            tag = f"({tag})"
        gap = row.gap
        out.append(
            f"  {tag:<5} {row.player[:24]:<24} {(row.pro_team or '--'):<4} "
            f"{_status_cell(row):<20} {_fmt(row.points)} {_fmt(row.projected)} "
            f"{'' if gap is None else format(gap, '+7.1f')}"
        )
    counts = (f"{side.count(STATUS_FINAL)} final / {side.count(STATUS_LIVE)} in progress / "
              f"{side.count(STATUS_UPCOMING)} yet to play")
    unresolved = side.count(STATUS_BYE) + side.count(STATUS_UNKNOWN)
    if unresolved:
        counts += f" / {unresolved} no game or kickoff unknown"
    out.append(f"  starters: {counts}")
    out.append(
        f"  starters sum {side.starters_total:.2f}   "
        f"ESPN {side.points_source} "
        f"{'--' if side.live_points is None else format(side.live_points, '.2f')}"
    )
    if not bench and side.bench:
        out.append(f"  ({len(side.bench)} bench/IR row(s) hidden — pass --bench)")
    return out


def format_live_matchup(matchup: LiveMatchup, *, bench: bool = False) -> str:
    """Render the live card. Every number that is an estimate says so (Rule 6)."""
    header = (f"LIVE SCORE — {matchup.season} matchup period {matchup.week} "
              f"(scoring period {matchup.scoring_period}), read {matchup.read_at}")
    out = [header, "=" * len(header), ""]

    own, opp = matchup.own, matchup.opponent
    own_pts = "--" if own.live_points is None else format(own.live_points, ".2f")
    if opp is not None:
        opp_pts = "--" if opp.live_points is None else format(opp.live_points, ".2f")
        margin = matchup.margin
        lead = "" if margin is None else (
            f"   ({'+' if margin >= 0 else ''}{margin:.2f} "
            f"{'ahead' if margin > 0 else 'behind' if margin < 0 else 'level'})"
        )
        out.append(f"{own.label}  {own_pts}   vs   {opp.label}  {opp_pts}{lead}")
    else:
        out.append(f"{own.label}  {own_pts}")
    out.append("")

    out.extend(_side_block(own, title="YOU", bench=bench))
    if opp is not None:
        out.append("")
        out.extend(_side_block(opp, title="OPPONENT", bench=bench))

    out.append("")
    out.append("NOTES")
    for note in matchup.notes:
        out.append(f"  * {note}")
    return "\n".join(line.rstrip() for line in out)
