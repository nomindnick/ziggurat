"""NFL source refresh orchestration + cadence (item 3.1b).

The ``ziggurat/league/sync.py`` analogue for the OTHER half of the data spine.
Item 1.4/1.5 built fourteen ``pull_*`` ingesters and tested them; nothing ever
called them in production, so fourteen tables sat empty and any November read
would have priced Week 10 off a July snapshot with a perfectly valid
``knowable_as_of``. Nothing complains about that — the data is not leaked, merely
stale — which is why staleness has to be measured rather than assumed.

WHAT THIS MODULE OWNS: the source registry (what exists, when it has anything to
say, what it depends on, whether missing it loses anything), ``run_ingest`` (the
scheduled entry point), the ``nfl_ingest_runs`` log, and the per-source staleness
report. The CLI parses, calls, prints (rule 3).

THE FOUR DEFECT CLASSES THIS IS DESIGNED AGAINST — all four were MEASURED on
this codebase during the 3.1b probes, none are hypothetical:

1. **A degraded pull destroying good data.** The item-3.1 lesson verbatim. In
   ``ziggurat/data/nfl/`` there is exactly ONE delete-then-write path
   (``espn_ranks.ingest_espn_ranks``) and it reproduced the bug exactly: a
   20-player response replaced a stored 1,026-player same-day board. The floor
   now lives in that module (``BoardCollapse``, checked BEFORE the delete);
   ``run_ingest``'s job is to surface the refusal as a FAILED run rather than
   swallow it.

   CORRECTION (3.1b audit): "every other table is append-only so it needs no
   floor" was HALF true and the wrong half was load-bearing. Append-only protects
   against a pull that is MISSING ROWS — untouched keys keep resolving to their
   older rows. It does NOT protect against a pull whose VALUES arrived empty,
   because ``select_as_of`` resolves the newest row PER KEY: a same-key row with
   null ids is not absent, it WINS. Measured on a copy of the live DB: a
   ``players`` pull with the id columns served empty (a column-present/values-null
   regression, which ``require_columns`` cannot see) took every crosswalk to zero
   — espn_by_gsis 7,897 -> 0, gsis_by_pfr 7,784 -> 0, sleeper->gsis 6,149 -> 0 —
   with the good rows still physically present underneath and the run logged
   ``ok``. ``players.CrosswalkCollapse`` is that floor: coverage per id column,
   checked before the write. A TRUNCATED pull (500 of 7,973 rows, ids intact) was
   verified harmless, as claimed.

2. **An unbounded network hang killing the cadence.** Also 3.1's. Three NFL
   seams had no timeout at all; they are bounded now through ``ziggurat.net``,
   and the units set ``TimeoutStartSec`` on top.

3. **A failed source's partial rows riding the next source's commit.** Measured:
   a mid-``executemany`` IntegrityError left 1,070 uncommitted rows on the shared
   connection, and the NEXT ingester's ``conn.commit()`` persisted them — leaving
   ``weekly_stats`` holding week 1 only (5.5% of the season), permanently, with
   valid stamps on every row, invisible to every leakage test, while the run log
   said "failed" and the table said "fresh". Hence ``conn.rollback()`` on every
   per-source exception, before the next source runs.

4. **"Wrote 0 rows" logged as success.** Six ingesters stamp ``knowable_as_of``
   from ``schedules``; with that table empty they drop 100% of their rows, return
   0 and raise nothing (measured: 19,421/19,421). That is indistinguishable from
   "upstream had nothing new" unless dependencies are checked in code and the
   drop count is recorded. Both happen here.

WHAT IS DELIBERATELY *NOT* COPIED FROM 3.1: the missing-days gap report. ESPN
serves no league history, so "these days are UNRECOVERABLE" is literally true
there. nflverse serves whole-season files on demand, so a missed NFL run is
staleness and re-pullable. Reusing that alarm would train the operator to ignore
the one report where the words are true. Only ``perishable=True`` sources here
lose anything by being missed, and they are marked individually.
"""

import logging
import re
import textwrap
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import (
    adp_rankings,
    base,
    espn_ranks,
    game_odds,
    injuries,
    ngs,
    players,
    projections,
    schedules,
    snap_counts,
    team_defense,
    weather,
    weekly_stats,
)

logger = logging.getLogger("ziggurat.data.nfl.refresh")

# Run statuses. The not-ok outcomes are deliberately DISTINCT so the operator can
# tell noise from signal — see the migration comment for why that separation is
# load-bearing (an undifferentiated alarm is how a report earns being ignored).
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"                # landed, but dropped some rows on the way
STATUS_EMPTY = "empty"                    # ran, wrote nothing, dropped nothing
STATUS_FAILED = "failed"                  # a real error (incl. a refused collapse)
STATUS_SKIPPED = "skipped"                # phase/dependency says nothing to do today
STATUS_FRESH = "fresh"                    # skipped: a good pull is still inside its interval
STATUS_BLOCKED = "blocked"                # recorded upstream schema break; not attempted
STATUS_ABSENT = "upstream_absent"         # this season is not published yet
STATUS_RUNNING = "running"                # started; a durable row until finish_run lands
STATUS_ABANDONED = "abandoned"            # a `running` row a later run found orphaned

#: Statuses that mean "this source did NOT do its job today". Owned here rather
#: than restated at the call sites: the CLI's exit code and ``format_run``'s
#: PROBLEMS line disagreed about `empty` (printed as a problem, exited 0), which
#: under ``Restart=on-failure`` meant an empty pull of a PERISHABLE source was
#: reported to systemd as success and never retried.
PROBLEM_STATUSES = (STATUS_FAILED, STATUS_EMPTY, STATUS_ABANDONED)

#: Fraction of a pull's rows that may be dropped before the pull counts as
#: failed rather than merely partial. The zero boundary alone was not enough: a
#: single surviving row out of 19,421 flipped the outcome to `partial`, which the
#: failure list, the exit code and the staleness verdict all treated as success
#: (measured 3.1b audit: 67 written / 19,354 dropped reported `fresh`).
_MAX_DROP_FRACTION = 0.2

# Season phases, derived from the schedules table (never from the wall clock and
# never from nflreadpy.get_current_season(), which returns 2025 until 2026-09-10
# and would quietly refresh LAST season all summer).
PHASE_PRESEASON = "preseason"
PHASE_INSEASON = "inseason"
PHASE_OFFSEASON = "offseason"
PHASE_UNKNOWN = "unknown"                 # schedules not ingested for this season yet
ALL_PHASES = frozenset({PHASE_PRESEASON, PHASE_INSEASON, PHASE_OFFSEASON})

# Cadence groups = what a timer runs. One unit per group, not per source: four
# templates instead of fourteen, and the run log distinguishes sources anyway.
GROUP_DAILY = "daily"
GROUP_WEEKLY = "weekly"
GROUP_GAMEDAY = "gameday"
GROUPS = (GROUP_DAILY, GROUP_WEEKLY, GROUP_GAMEDAY)

# Open-Meteo's forecast endpoint hard-refuses a start_date more than ~16 days out
# with HTTP 400 (measured 2026-07-24: +16d OK, +20d 400), and fetch_open_meteo has
# no error tolerance — one out-of-range call takes the whole run with it. Stay
# comfortably inside the wall.
_WEATHER_HORIZON_DAYS = 10


def _utc_now() -> str:
    """Wall-clock stamp for the run LOG only.

    Not a knowledge time and never used as one (rule 1 governs read accessors;
    ``retrieved_as_of`` is always passed in explicitly by the caller).
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- season shape
#
# All three derived from the schedules TABLE. These read it raw (no as-of gate),
# exactly as base.game_date_map does, because they answer an OPERATIONAL question
# ("what should the scheduler pull today") and not a decision question. Note the
# trap this avoids: 2026 REG rows are stamped knowable_as_of='2026-08-01', so a
# check written against get_schedule(as_of=today) reads 0 rows on a perfectly
# healthy ingest until Aug 1.


def season_bounds(conn, *, season: int) -> tuple[str, str] | None:
    """(first, last) REG gameday for ``season``, or None when unknown.

    OPERATIONAL READ — no as-of gate. Never call this from a decision path; use
    an as-of accessor (``schedules.get_schedule``) there.
    """
    row = conn.execute(
        "SELECT MIN(gameday) AS first, MAX(gameday) AS last FROM schedules "
        "WHERE season = ? AND game_type = 'REG' AND gameday IS NOT NULL",
        (season,),
    ).fetchone()
    if row is None or row["first"] is None:
        return None
    return (row["first"], row["last"])


def season_phase(conn, *, season: int, today) -> str:
    """Where in the season ``today`` falls, from the ingested schedule.

    OPERATIONAL READ — no as-of gate; ``today`` is a scheduler input, NOT an
    ``as_of``. Never call this from a decision path.

    ``PHASE_UNKNOWN`` when schedules has not been ingested for this season —
    which is the honest answer on a fresh database and is what lets the very
    first run bootstrap (players + schedules run, everything phase-gated waits).
    """
    bounds = season_bounds(conn, season=season)
    if bounds is None:
        return PHASE_UNKNOWN
    day = normalize_as_of(today).isoformat()
    first, last = bounds
    if day < first:
        return PHASE_PRESEASON
    if day <= last:
        return PHASE_INSEASON
    return PHASE_OFFSEASON


def season_weeks(conn, *, season: int) -> list[int]:
    """Every REG week in the ingested schedule, ascending. [] when unknown.

    OPERATIONAL READ — no as-of gate. Never call this from a decision path.

    The scope source for the projections pull. Deriving it is not pedantry: the
    live 2026-07-24 pull asked for weeks 1-17 while week 18 exists upstream, so
    one eighteenth of the board silently stayed three days stale and nothing said
    so (upserts destroy nothing, but "the latest snapshot" was the wrong mental
    model — the board is per-key MAX(retrieved_as_of), stitched across two days).
    """
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT week FROM schedules WHERE season = ? AND game_type = 'REG' "
            "ORDER BY week",
            (season,),
        )
    ]


def current_week(conn, *, season: int, today) -> int:
    """The week whose games have not all been played as of ``today``.

    OPERATIONAL READ — no as-of gate, and ``today`` is a wall-clock scheduler
    input, NOT an ``as_of``. Item 3.2 is the intended consumer (its recon
    recorded that no "current week" source existed); when it wires this up it
    must treat the answer as "what week is it now", never as "what was knowable
    at as_of". The ungated-vs-gated gap is real, not theoretical: 2026 REG rows
    carry knowable_as_of 2026-08-01, so this returns week 1 today while
    ``get_schedule(as_of=today)`` returns nothing.

    RAISES when the schedule is not ingested rather than defaulting to week 1 or
    to date arithmetic — the same must-raise decision item 3.2 made for
    ``weeks=None``. A guessed week silently pulls the wrong week's weather for a
    whole season, and that error is rule-1-invisible because the stamps stay
    perfectly valid.
    """
    day = normalize_as_of(today).isoformat()
    row = conn.execute(
        "SELECT MIN(week) AS wk FROM ("
        "  SELECT week, MAX(gameday) AS last FROM schedules "
        "  WHERE season = ? AND game_type = 'REG' AND gameday IS NOT NULL GROUP BY week"
        ") WHERE last >= ?",
        (season, day),
    ).fetchone()
    if row is not None and row["wk"] is not None:
        return int(row["wk"])
    weeks = season_weeks(conn, season=season)
    if not weeks:
        raise ValueError(
            f"cannot derive the current week for season {season}: the schedules table is "
            "empty for that season. Run `ziggurat ingest run --source schedules` first — "
            "guessing a week would silently pull the wrong one all season."
        )
    return weeks[-1]  # season is over; the last week is the current one


def weather_weeks(conn, *, season: int, today) -> list[int]:
    """REG weeks with an unplayed game inside the Open-Meteo forecast window.

    OPERATIONAL READ — no as-of gate; ``today`` is a scheduler input.

    A week qualifies while its LAST game is still ahead (``MAX(gameday) >= day``,
    the same predicate ``current_week`` uses) and its first game is inside the
    forecast wall. The original ``MIN(gameday) >= day`` form dropped the current
    week the moment its Thursday game kicked off, so from Friday onward the
    gameday timer fetched only NEXT week — and since forecast mode is perishable,
    the freshest forecast a Sunday lineup call could ever read was the one taken
    the previous Thursday. Measured against the real 2025 schedule: week 5 fell
    out of the set on Fri 10-03, Sat 10-04 and Sun 10-05, the day its games were
    played (3.1b audit finding).

    Empty outside the window, which is a legitimate 'nothing to do' rather than a
    failure — the forecast endpoint 400s beyond ~16 days and a preseason
    'refresh all weather' loop would crash on its very first call.
    """
    day = normalize_as_of(today)
    horizon = (day + timedelta(days=_WEATHER_HORIZON_DAYS)).isoformat()
    return [
        r[0] for r in conn.execute(
            "SELECT week FROM ("
            "  SELECT week, MIN(gameday) AS first, MAX(gameday) AS last FROM schedules "
            "  WHERE season = ? AND game_type = 'REG' AND gameday IS NOT NULL GROUP BY week"
            ") WHERE last >= ? AND first <= ? ORDER BY week",
            (season, day.isoformat(), horizon),
        )
    ]


# ------------------------------------------------------------------- registry


@dataclass(frozen=True)
class IngestContext:
    """Everything one source's pull needs, resolved once per run."""

    conn: object
    season: int
    retrieved_as_of: str
    today: str
    credentials: dict | None = None
    allow_shrink: bool = False
    allow_backfill: bool = False


@dataclass(frozen=True)
class SourceSpec:
    """One ingestable source: how to pull it, when, and what it costs to miss."""

    name: str
    group: str
    #: (ctx) -> rows written. None only when ``blocked`` is set.
    pull: Callable[[IngestContext], int] | None
    #: (ctx) -> short human description of the partition actually requested.
    scope: Callable[[IngestContext], str] | None = None
    #: Season phases in which this source has anything new to say.
    phases: frozenset = ALL_PHASES
    #: Days after the last successful pull before this source is judged stale.
    interval_days: int = 1
    #: True when upstream serves only the CURRENT value, so a missed run loses a
    #: point-in-time observation permanently. Everything nflverse is False: those
    #: are whole-season files, re-pullable any time, and a missed day costs
    #: nothing but freshness.
    perishable: bool = False
    #: Needs ESPN cookies (only espn_ranks does).
    needs_credentials: bool = False
    #: Replaces a stored partition rather than appending. The floor-before-delete
    #: population. Only espn_ranks.
    replaces_partition: bool = False
    #: Stamps knowable_as_of from schedules, so schedules MUST be ingested for
    #: this season first or the source silently drops 100% of its rows.
    needs_schedules: bool = False
    #: A recorded reason this source cannot currently be pulled at all. Set means
    #: never attempted, and reported loudly by `ingest status` — the honest
    #: alternative to a scheduled source that fails every single day.
    blocked: str | None = None
    #: Optional (ctx) -> reason predicate for "this source is in its right phase
    #: and healthy, but has NOTHING to do today". Returning a string skips the
    #: pull; returning None runs it. Distinct from ``blocked`` (a recorded defect)
    #: and from an empty pull (upstream had nothing when it should have had
    #: something). ``game_weather`` needs it: outside the ~10-day forecast wall
    #: there is no week to fetch, and running anyway produced STATUS_EMPTY, which
    #: run_failed() counts as a problem — so from July to September a correct
    #: cadence reported a standing "LAST ATTEMPT FAILED" on a PERISHABLE source
    #: (observed on the first live run, 2026-07-24). weather_weeks' own docstring
    #: already called this "a legitimate 'nothing to do' rather than a failure";
    #: the run path just had no way to say it.
    applicable: object = None
    notes: str = ""


class PartialPull(RuntimeError):
    """A multi-request pull failed after committing some of its requests.

    Carries what actually landed so the run log does not understate the write.
    ``_pull_game_weather`` is the one loop-of-commits pull here (one request per
    outdoor game, per week), and recording ``rows_written=0`` after week 1
    committed and week 2 raised would make ``nfl_ingest_runs`` a wrong answer to
    "what is in the database".
    """

    def __init__(self, message: str, *, rows_written: int, cause: BaseException):
        super().__init__(message)
        self.rows_written = rows_written
        self.cause = cause


def _pull_players(ctx) -> int:
    return players.pull_players(ctx.conn, retrieved_as_of=ctx.retrieved_as_of,
                                allow_shrink=ctx.allow_shrink)


def _pull_schedules(ctx) -> int:
    return schedules.pull_schedules(ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of)


def _pull_projections(ctx) -> int:
    weeks = season_weeks(ctx.conn, season=ctx.season)
    if not weeks:
        raise ValueError(
            f"projections need the week list for season {ctx.season} and schedules is empty; "
            "pull schedules first (a hardcoded week range is how week 18 went stale)"
        )
    return projections.pull_projections(
        ctx.conn, ctx.season, weeks, retrieved_as_of=ctx.retrieved_as_of
    )


def _scope_projections(ctx) -> str:
    weeks = season_weeks(ctx.conn, season=ctx.season)
    return f"weeks {weeks[0]}-{weeks[-1]}" if weeks else "weeks unknown"


def _pull_adp(ctx) -> int:
    return adp_rankings.pull_adp_rankings(ctx.conn, retrieved_as_of=ctx.retrieved_as_of)


def _pull_espn_ranks(ctx) -> int:
    creds = ctx.credentials or {}
    return espn_ranks.pull_espn_ranks(
        ctx.conn, season=ctx.season, retrieved_as_of=ctx.retrieved_as_of, today=ctx.today,
        allow_shrink=ctx.allow_shrink, allow_backfill=ctx.allow_backfill, **creds,
    )


def _pull_game_odds(ctx) -> int:
    return game_odds.pull_game_odds(ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of)


def _pull_injuries(ctx) -> int:
    return injuries.pull_injuries(ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of)


def _pull_weekly_stats(ctx) -> int:
    return weekly_stats.pull_weekly_stats(
        ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of
    )


def _pull_snap_counts(ctx) -> int:
    return snap_counts.pull_snap_counts(ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of)


def _pull_team_defense(ctx) -> int:
    return team_defense.pull_team_defense(
        ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of
    )


def _pull_ngs_passing(ctx) -> int:
    return ngs.pull_ngs_passing(ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of)


def _pull_ngs_rushing(ctx) -> int:
    return ngs.pull_ngs_rushing(ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of)


def _pull_ngs_receiving(ctx) -> int:
    return ngs.pull_ngs_receiving(ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of)


def _pull_game_weather(ctx) -> int:
    weeks = weather_weeks(ctx.conn, season=ctx.season, today=ctx.today)
    total = 0
    for week in weeks:
        try:
            total += weather.pull_game_weather(
                ctx.conn, ctx.season, week, retrieved_as_of=ctx.retrieved_as_of,
                mode="forecast",
            )
        except Exception as exc:
            # Each week commits as it goes, so a later week's failure cannot roll
            # the earlier ones back. Report what landed instead of the reflexive 0.
            raise PartialPull(
                f"game_weather failed on week {week} after storing {total} rows for "
                f"weeks {weeks[:weeks.index(week)]}: {type(exc).__name__}: {exc}",
                rows_written=total, cause=exc,
            ) from exc
    return total


def _scope_weather(ctx) -> str:
    weeks = weather_weeks(ctx.conn, season=ctx.season, today=ctx.today)
    return f"forecast weeks {weeks}" if weeks else "no week inside the forecast horizon"


def _weather_applicable(ctx) -> str | None:
    if weather_weeks(ctx.conn, season=ctx.season, today=ctx.today):
        return None
    return (f"no week inside the ~{_WEATHER_HORIZON_DAYS}-day forecast horizon "
            "(the endpoint 400s beyond it) — nothing to fetch, not a failure")


def _season_scope(ctx) -> str:
    return f"season {ctx.season}"


# THE REGISTRY. Order IS dependency order: players and schedules are the spine
# everything else stamps or crosswalks against, so they lead. Cadence per source
# is pinned to the MEASURED upstream publish rhythm (probed 2026-07-24), not to
# a wish — over-pulling a whole-season parquet four times a day is tens of MB of
# pointless traffic against free volunteer infrastructure that publishes once.
SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        name="players", group=GROUP_DAILY, pull=_pull_players, scope=lambda ctx: "all",
        interval_days=7,
        notes="DynastyProcess id crosswalk; upstream commits weekly (measured: Fridays "
              "~04:00 UTC). Cheap (~2s) and EVERY other source's join key, so it rides "
              "the daily unit rather than earning its own schedule.",
    ),
    SourceSpec(
        name="schedules", group=GROUP_DAILY, pull=_pull_schedules, scope=_season_scope,
        interval_days=1,
        notes="The spine: (season, week, team) -> gameday stamps knowable_as_of on six "
              "other sources, and season phase / current week / projection scope are all "
              "derived from it. Upstream refreshes every ~5 min in-season. DAILY interval "
              "on purpose (players is 7d): flex scheduling moves Sunday kickoffs ~12 days "
              "out, and a stale gameday silently mis-stamps six other sources.",
    ),
    SourceSpec(
        name="projections", group=GROUP_DAILY, pull=_pull_projections,
        scope=_scope_projections, needs_schedules=True, perishable=True,
        notes="PERISHABLE: Sleeper serves the current value only and regenerates all 18 "
              "weeks in ONE daily batch (measured: every row's last_modified inside a "
              "3-second window ~07:45 UTC). A missed day is a lost observation. This is "
              "the source the November-staleness failure mode is actually about.",
    ),
    SourceSpec(
        name="adp_rankings", group=GROUP_DAILY, pull=_pull_adp, scope=lambda ctx: "current scrape",
        perishable=True,
        notes="PERISHABLE: FantasyPros serves today's ECR scrape only (the historical "
              "panel is a separate Phase-4 ingester). Hottest until the draft.",
    ),
    SourceSpec(
        name="espn_ranks", group=GROUP_DAILY, pull=_pull_espn_ranks, scope=_season_scope,
        phases=frozenset({PHASE_PRESEASON}), perishable=True,
        needs_credentials=True, replaces_partition=True,
        notes="PERISHABLE and the ONLY delete-then-write source here — the draft board the "
              "cockpit reads. Fenced by espn_ranks.BoardCollapse (floor BEFORE the delete). "
              "Preseason only: after the draft the board is a historical artifact. The "
              "interval gate means a second cadence run the same day records 'fresh'; "
              "use `ingest run --source espn_ranks --force` for a deliberate refresh "
              "(the draft cockpit's own refresh goes through espn_ranks.ensure_board).",
    ),
    SourceSpec(
        name="game_odds", group=GROUP_DAILY, pull=_pull_game_odds, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON}),
        notes="Closing lines ride the schedules frame. NOTE (verified): knowable_as_of is "
              "the GAMEDAY, so no pre-kickoff reader can see a line — item 3.5 needs a "
              "pre-game regime (like weather's forecast/archive split) before this is "
              "useful. Pulled anyway so the history accrues.",
    ),
    SourceSpec(
        name="injuries", group=GROUP_DAILY, pull=_pull_injuries, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON}), needs_schedules=True,
        notes="nflverse's injury feed died after 2024; 2025 exists only as a post-season "
              "bulk backfill and no longer carries date_modified, so every row now falls "
              "back to the team-gameday anchor. Mid-week injury NEWS must come from the "
              "live ESPN league-state sync (item 3.1), not from this table.",
    ),
    SourceSpec(
        name="depth_charts", group=GROUP_DAILY, pull=None,
        phases=frozenset({PHASE_PRESEASON, PHASE_INSEASON}), needs_schedules=True,
        blocked=(
            "upstream schema replaced (verified 2026-07-24): nflverse now serves depth "
            "charts as a DAILY SNAPSHOT PANEL keyed on a `dt` timestamp with NO season and "
            "NO week column and IDP rows included (2025: 554,215 rows x 12 cols). The "
            "stored table is keyed (season, week, club_code, formation, position, "
            "depth_position, gsis_id) and cannot hold it, and base.select_as_of cannot "
            "express 'newest dt per key at as_of' — so this is a table + accessor rewrite, "
            "not a column remap. Deliberately NOT attempted by the cadence; see "
            "IMPLEMENTATION_PLAN 3.1b."
        ),
        notes="Item 3.2 already deferred the depth-chart consumer, so nothing reads this "
              "today and nothing is blocked by the block.",
    ),
    SourceSpec(
        name="weekly_stats", group=GROUP_WEEKLY, pull=_pull_weekly_stats, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON, PHASE_OFFSEASON}), interval_days=7,
        needs_schedules=True,
        notes="Whole-season file, so a weekly re-pull also self-heals every earlier week. "
              "Thursday on purpose: NFL stat corrections land Mon-Wed, so Thursday's copy "
              "is the clean one.",
    ),
    SourceSpec(
        name="snap_counts", group=GROUP_WEEKLY, pull=_pull_snap_counts, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON, PHASE_OFFSEASON}), interval_days=7,
        needs_schedules=True,
    ),
    SourceSpec(
        name="team_defense", group=GROUP_WEEKLY, pull=_pull_team_defense, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON, PHASE_OFFSEASON}), interval_days=7,
        needs_schedules=True,
        notes="The D/ST line that prices through scoring.score_dst.",
    ),
    SourceSpec(
        name="ngs_passing", group=GROUP_WEEKLY, pull=_pull_ngs_passing, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON, PHASE_OFFSEASON}), interval_days=7,
        needs_schedules=True,
    ),
    SourceSpec(
        name="ngs_rushing", group=GROUP_WEEKLY, pull=_pull_ngs_rushing, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON, PHASE_OFFSEASON}), interval_days=7,
        needs_schedules=True,
    ),
    SourceSpec(
        name="ngs_receiving", group=GROUP_WEEKLY, pull=_pull_ngs_receiving, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON, PHASE_OFFSEASON}), interval_days=7,
        needs_schedules=True,
    ),
    SourceSpec(
        name="game_weather", group=GROUP_GAMEDAY, pull=_pull_game_weather, scope=_scope_weather,
        phases=frozenset({PHASE_PRESEASON, PHASE_INSEASON}), needs_schedules=True,
        perishable=True, applicable=_weather_applicable,
        notes="PRESEASON is included so week 1's run-up is captured — the phase flips only "
              "ON week 1's Thursday, and without this the Sunday of week 1 would have no "
              "forecast history at all. Outside the 10-day horizon weather_weeks returns [] "
              "and the pull is a no-op. PERISHABLE in forecast mode (a past forecast cannot "
              "be re-fetched; ERA5 "
              "archive actuals are a separate, replayable mode). Restricted to weeks "
              "inside the ~16-day Open-Meteo forecast wall, one request per OUTDOOR game "
              "(~18s a week; fixed domes fetch nothing).",
    ),
)

SOURCES_BY_NAME = {spec.name: spec for spec in SOURCES}


def select_sources(*, group: str | None = None, names: Sequence[str] | None = None
                   ) -> tuple[SourceSpec, ...]:
    """Resolve a ``--group`` / ``--source`` selection to specs IN REGISTRY ORDER.

    Registry order is dependency order, so re-sorting the caller's ``--source``
    list is not cosmetic: ``--source weekly_stats --source schedules`` typed in
    that order would otherwise stamp every stat row against an empty schedule.
    An unknown name fails loudly rather than being silently ignored, and so does
    passing BOTH filters: ``--group weekly --source players`` used to run
    ``players`` and say nothing about the ignored group, so an operator debugging
    the weekly unit could believe they had run it.
    """
    if names and group is not None:
        raise ValueError(
            "pass --group or --source, not both: they select different things and "
            "there is no sensible precedence (--source used to silently win)."
        )
    if names:
        unknown = sorted(set(names) - set(SOURCES_BY_NAME))
        if unknown:
            raise ValueError(
                f"unknown source(s) {unknown}; known: {sorted(SOURCES_BY_NAME)}"
            )
        wanted = set(names)
        return tuple(s for s in SOURCES if s.name in wanted)
    if group is not None:
        if group not in GROUPS:
            raise ValueError(f"unknown group {group!r} (known: {list(GROUPS)})")
        return tuple(s for s in SOURCES if s.group == group)
    return SOURCES


def needs_credentials(specs: Sequence[SourceSpec]) -> bool:
    """Does this selection require ESPN cookies? (Keeps the registry read out of
    the CLI — rule 3.)"""
    return any(spec.needs_credentials for spec in specs)


# ------------------------------------------------------------ cadence decision


@dataclass(frozen=True)
class Decision:
    """What the scheduler will do with one source on one day, and why."""

    name: str
    action: str          # "pull" or one of the STATUS_* non-run outcomes
    reason: str
    scope: str = ""


def decide(conn, spec: SourceSpec, *, season: int, today, have_credentials: bool,
           force: bool = False) -> Decision:
    """Decide whether ``spec`` runs today. PURE of the network — ``--dry-run``
    reports exactly this.

    Evaluated per source at the moment that source is reached, not once up front:
    ``schedules`` may land during this very run, and re-deriving the phase after
    it does is what lets a fresh database bootstrap in ONE pass instead of two.

    That per-source re-evaluation is also why a dry run on a BOOTSTRAP database
    under-reports: it is computed against the state on disk now, so sources that
    only unlock once ``schedules`` lands (during the run itself) show as SKIPPED
    and then pull for real. The divergence is always in the safe direction — the
    real run does more, never less — and ``format_plan`` says so in a footer.
    """
    if spec.blocked:
        return Decision(spec.name, STATUS_BLOCKED, spec.blocked)

    if spec.needs_credentials and not have_credentials:
        return Decision(spec.name, STATUS_SKIPPED,
                        "needs ESPN credentials (SWID / ESPN_S2 / league id) and none were resolved")

    phase = season_phase(conn, season=season, today=today)
    if phase == PHASE_UNKNOWN:
        # Nothing phase-gated can be judged yet. Sources that neither depend on
        # schedules nor restrict their phases are exactly the bootstrap set.
        if spec.needs_schedules or spec.phases != ALL_PHASES:
            return Decision(spec.name, STATUS_SKIPPED,
                            f"season {season} phase unknown — schedules not ingested yet")
    elif phase not in spec.phases:
        return Decision(spec.name, STATUS_SKIPPED,
                        f"nothing to pull in the {phase} phase "
                        f"(applies in: {', '.join(sorted(spec.phases))})")

    if spec.needs_schedules and not season_weeks(conn, season=season):
        # The measured silent-zero: without this the source drops 100% of its
        # rows, returns 0 and raises nothing.
        return Decision(spec.name, STATUS_SKIPPED,
                        f"schedules not ingested for season {season} — this source stamps "
                        "knowable_as_of from the gameday map and would drop every row")

    ctx = IngestContext(conn=conn, season=season,
                        retrieved_as_of=str(today), today=str(today))

    if spec.applicable is not None:
        # Deliberately BEFORE the interval gate: "nothing to do" is not staleness,
        # and a source skipped for this reason must not be nagged about tomorrow.
        nothing_to_do = spec.applicable(ctx)
        if nothing_to_do:
            return Decision(spec.name, STATUS_SKIPPED, nothing_to_do)

    scope = ""
    if spec.scope is not None:
        try:
            scope = spec.scope(ctx)
        except Exception as exc:  # a scope description must never break the plan
            scope = f"<scope unavailable: {type(exc).__name__}>"

    # INTERVAL GATE — the reason the timers can fire more often than a source
    # needs. Before this, `interval_days` was reporting-only: the weekly group ran
    # ONCE a week on a fixed calendar day, so an nflverse outage that outlasted the
    # unit's three restarts cost a whole week of weekly_stats/snap_counts with
    # `ingest status` still reading 'fresh' (age 7 <= interval 7). The units now
    # fire daily and this decides; a failed Thursday retries Friday, and a
    # successful Thursday keeps the anchor. Checked LAST so a phase or dependency
    # refusal (the more informative answer) wins when both apply.
    if not force:
        recent = last_run(conn, source=spec.name, season=season, status=STATUS_OK,
                          through=today)
        if recent is not None:
            age = (normalize_as_of(today) - normalize_as_of(recent["retrieved_as_of"])).days
            if 0 <= age < spec.interval_days:
                return Decision(
                    spec.name, STATUS_FRESH,
                    f"last successful pull was {age}d ago and this source refreshes every "
                    f"{spec.interval_days}d (pass --force to pull anyway)", scope,
                )
    return Decision(spec.name, "pull", "due", scope)


def plan_ingest(conn, *, sources, season: int, today, have_credentials: bool,
                force: bool = False) -> list[Decision]:
    """The full ``--dry-run`` answer: one Decision per selected source, in order."""
    return [
        decide(conn, spec, season=season, today=today, have_credentials=have_credentials,
               force=force)
        for spec in sources
    ]


# ------------------------------------------------------------------ run log
#
# Operational metadata, NOT facts about the world: no as_of, never through
# select_as_of (same stance as league_sync_runs). ``today``/``through`` are still
# passed in explicitly — the package layer never materializes "now".


def start_run(conn, *, batch_id: str, source: str, season, scope, retrieved_as_of,
              started_at: str) -> int:
    """Write the 'running' row BEFORE the network call, and commit it.

    A crash between start and finish therefore leaves a durable ``running`` row.
    That is the point: silence is not success, and a half-finished multi-source
    run must be visible afterwards — a ``TimeoutStartSec`` SIGTERM mid-run is the
    realistic case, and the sources after the hang never ran at all.

    Reaps any older ``running`` row for the same source first: without that, the
    orphan is invisible forever (nothing else ever updates it) and a reader
    cannot tell a run that died last week from the one happening right now.
    """
    conn.execute(
        "UPDATE nfl_ingest_runs SET status = ?, error = COALESCE(error, ?) "
        "WHERE source = ? AND status = ?",
        (STATUS_ABANDONED,
         "no finish_run ever landed — the process died mid-pull (systemd "
         "TimeoutStartSec, a kill, or a crash); a later run found the row orphaned",
         source, STATUS_RUNNING),
    )
    cur = conn.execute(
        "INSERT INTO nfl_ingest_runs (batch_id, source, season, scope, retrieved_as_of, "
        "started_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_id, source, season, scope,
         normalize_as_of(retrieved_as_of).isoformat(), started_at, STATUS_RUNNING),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, *, status: str, finished_at: str | None = None,
               rows_written=None, rows_dropped=None, error=None) -> None:
    conn.execute(
        "UPDATE nfl_ingest_runs SET status = ?, finished_at = ?, rows_written = ?, "
        "rows_dropped = ?, error = ? WHERE run_id = ?",
        (status, finished_at, rows_written, rows_dropped, error, run_id),
    )
    conn.commit()


def record_run(conn, *, batch_id: str, source: str, season, scope, retrieved_as_of,
               at: str, status: str, reason: str | None = None) -> int:
    """Write a single terminal row for a source that never ran (skipped/blocked).

    Recorded, not omitted: a source silently dropped from the cadence is the
    failure this whole item exists to make visible.
    """
    run_id = start_run(conn, batch_id=batch_id, source=source, season=season, scope=scope,
                       retrieved_as_of=retrieved_as_of, started_at=at)
    finish_run(conn, run_id, status=status, finished_at=at, error=reason)
    return run_id


def last_run(conn, *, source: str, season: int | None = None, status: str | None = STATUS_OK,
             through=None):
    """The most recent run row for one source (by default the last SUCCESSFUL one).

    Ordered by the monotonic ``run_id``, NOT ``started_at`` — run timestamps are
    second-resolution and two runs inside one second (a retry racing the timer)
    made "the last run" ambiguous, so a failure could be reported as the earlier
    success (item 3.1 audit finding, same fix).

    ``season`` FILTERS (3.1b audit finding). The column was written on every row
    and read by nothing, so any other-season run answered "how stale is this
    source for season X": one ``ingest run --season 2025`` backfill made the 2026
    board read ``fresh 0d`` against a table holding zero 2026 rows, and after the
    March season rollover the units (which pin ``--season`` at install time) would
    report the previous season's pulls as this season's for months.

    ``through`` BOUNDS the answer to runs that had already happened on that day,
    the run-log equivalent of ``select_as_of``'s retrieval gate. Without it,
    asking "was my data fresh when I set the Week 5 lineup?" was answered with a
    November pull and a negative age, which then pinned the verdict at 'fresh'
    forever.
    """
    clauses = ["source = :source"]
    params: dict = {"source": source}
    if season is not None:
        clauses.append("season = :season")
        params["season"] = season
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    if through is not None:
        clauses.append("retrieved_as_of <= :through")
        params["through"] = normalize_as_of(through).isoformat()
    return conn.execute(
        f"SELECT * FROM nfl_ingest_runs WHERE {' AND '.join(clauses)} "
        "ORDER BY run_id DESC LIMIT 1",
        params,
    ).fetchone()


def batch_runs(conn, *, batch_id: str) -> list:
    return conn.execute(
        "SELECT * FROM nfl_ingest_runs WHERE batch_id = ? ORDER BY run_id", (batch_id,)
    ).fetchall()


# --------------------------------------------------------------- orchestration


# nflreadpy refuses a season past get_current_season() client-side, and nflverse
# 404s a season whose parquet does not exist yet. Both mean "not published yet",
# which is the NORMAL state of six sources every day until ~Sept 10 — logging
# those as 'failed' for seven weeks is how an operator learns to ignore the
# status output right before the season it matters in.
#
# STRUCTURED, NOT A SUBSTRING SCAN (3.1b audit finding). The first version matched
# ("must be between", "404", "not found", "no such file", "does not exist")
# against the whole lowercased message of ANY exception from ANY source, which
# downgraded real breakage to an expected absence: a FileNotFoundError from an
# unwritable cache dir, and — the sharpest one — espn_ranks' own payload-drift
# guard, whose message reads "only 404/2051 mapped rows carry a PPR editorial
# rank" because 2051 is the live board size. Verified upstream shapes:
#   * nflreadpy season guard -> ValueError("Season must be between 2012 and 2025")
#   * nflverse missing release -> requests HTTPError, wrapped by nflreadpy's
#     downloader as ConnectionError("Failed to download <url>: 404 Client Error:
#     Not Found for url: ...")
_SEASON_GUARD_RE = re.compile(r"^\s*season must be between\b", re.IGNORECASE)
_HTTP_404_RE = re.compile(r"\b404 (?:client error|not found)\b", re.IGNORECASE)


def _http_status(exc: BaseException):
    """The HTTP status carried by an exception, whatever client raised it."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code is None:
        code = getattr(exc, "code", None)   # urllib.error.HTTPError
    return code if isinstance(code, int) else None


def _is_upstream_absent(exc: BaseException) -> bool:
    """Does this exception mean 'upstream has not published this yet'?

    Matched on exception TYPE plus an anchored pattern, never on a bare substring
    of arbitrary text. It only ever downgrades an error's LOUDNESS: an
    ``upstream_absent`` row is still not ``ok``, still carries the message, still
    shows in ``ingest status`` and still prints in the run report's PROBLEMS-
    adjacent NOT PUBLISHED line. ``run_ingest`` additionally refuses to downgrade
    a source that ALREADY succeeded this season — a 404 after a success is a
    regression (a renamed release), not an absence.
    """
    if isinstance(exc, ValueError) and _SEASON_GUARD_RE.match(str(exc)):
        return True
    if _http_status(exc) == 404:
        return True
    # nflreadpy re-wraps requests' HTTPError as ConnectionError, so the status is
    # only available as text — but the TYPE still constrains which exceptions can
    # reach this branch, and the pattern is anchored to requests' phrasing.
    return isinstance(exc, ConnectionError) and bool(_HTTP_404_RE.search(str(exc)))


def resolve_stamp(retrieved_as_of, today, *, allow_backfill: bool = False) -> tuple[str, str]:
    """Normalize (stamp, today) and REFUSE a back-stamped run by default.

    Hoisted out of ``run_ingest`` so the dry run applies the same rule and reports
    the refusal instead of printing a clean plan that the real command then dies
    on (3.1b audit finding).

    Both are required. ``today=None`` used to default to the stamp, which made
    ``stamp != today`` trivially false and disabled the fence entirely — the
    documented "back-stamping is refused by default" path did not refuse.
    """
    stamp = normalize_as_of(retrieved_as_of).isoformat()
    day = normalize_as_of(today).isoformat()
    if stamp != day and not allow_backfill:
        raise ValueError(
            f"refusing to stamp an ingest {stamp} on a run made {day}. Back-stamping "
            "writes TODAY's upstream data under a past retrieved_as_of, which is a "
            "manufactured leak for EVERY source — the default `historical` view gates "
            "on retrieved_as_of, so a reader reconstructing that day is served data "
            "that did not exist yet. For espn_ranks it also DELETES that day's stored "
            "board, which ESPN cannot re-serve. Pass --allow-backfill if you mean it."
        )
    return stamp, day


def run_ingest(conn, *, sources, season: int, retrieved_as_of, today,
               credentials: dict | None = None, allow_shrink: bool = False,
               allow_backfill: bool = False, force: bool = False,
               batch_id: str | None = None) -> list[dict]:
    """Pull each selected source in registry (= dependency) order. Returns one
    summary dict per source.

    Does NOT raise on a per-source failure: the sources are independent, and an
    nflverse hiccup on ngs_rushing must not cost the day's Sleeper projection
    snapshot, which cannot be re-pulled tomorrow (item 3.1's "protect the
    irreplaceable part" stance, applied per source instead of per run). The
    caller decides what to do with the returned statuses; the CLI exits nonzero
    when any source failed, so a timer still reports failure.

    BACK-STAMPING IS REFUSED by default (``resolve_stamp``). ``today`` is
    required: with a default it silently disabled its own fence.
    """
    stamp, today = resolve_stamp(retrieved_as_of, today, allow_backfill=allow_backfill)

    batch_id = batch_id or uuid.uuid4().hex[:12]
    have_credentials = bool(credentials)
    summaries: list[dict] = []

    for spec in sources:
        decision = decide(conn, spec, season=season, today=today,
                          have_credentials=have_credentials, force=force)
        if decision.action != "pull":
            now = _utc_now()
            run_id = record_run(conn, batch_id=batch_id, source=spec.name, season=season,
                                scope=decision.scope or None, retrieved_as_of=stamp, at=now,
                                status=decision.action, reason=decision.reason)
            summaries.append({"source": spec.name, "status": decision.action,
                              "reason": decision.reason, "rows": 0, "run_id": run_id,
                              "scope": decision.scope, "batch_id": batch_id})
            continue

        run_id = start_run(conn, batch_id=batch_id, source=spec.name, season=season,
                           scope=decision.scope or None, retrieved_as_of=stamp,
                           started_at=_utc_now())
        ctx = IngestContext(conn=conn, season=season, retrieved_as_of=stamp, today=today,
                            credentials=credentials, allow_shrink=allow_shrink,
                            allow_backfill=allow_backfill)
        try:
            with base.collect_drops() as tally:
                written = spec.pull(ctx)
        except Exception as exc:
            # THE PARTIAL-COMMIT FENCE. A source that raised mid-executemany left
            # rows in the open transaction on this shared connection, and the next
            # source's commit would persist them — measured as a permanently
            # truncated weekly_stats holding week 1 only. Discard them here,
            # before any other source touches the connection.
            try:
                conn.rollback()
            except Exception:  # pragma: no cover - rollback on a dead conn
                logger.exception("ingest: rollback after %s failure also failed", spec.name)
            status = STATUS_FAILED
            message = f"{type(exc).__name__}: {exc}"
            if _is_upstream_absent(exc):
                # ...unless this source ALREADY landed this season, in which case
                # "not published yet" is impossible and a 404 means the release
                # was renamed or withdrawn — a real break wearing the expected
                # costume, which would otherwise exit 0 forever.
                if last_run(conn, source=spec.name, season=season, status=STATUS_OK) is None:
                    status = STATUS_ABSENT
                else:
                    message += (" — reported as FAILED, not upstream_absent: this source "
                                "already succeeded for this season, so the data cannot be "
                                "merely unpublished (a renamed/withdrawn release?)")
            rows_written = getattr(exc, "rows_written", 0)
            finish_run(conn, run_id, status=status, finished_at=_utc_now(),
                       rows_written=rows_written, rows_dropped=None, error=message)
            logger.warning("ingest: %s -> %s (%s)", spec.name, status, message)
            summaries.append({"source": spec.name, "status": status, "reason": message,
                              "rows": rows_written, "run_id": run_id, "scope": decision.scope,
                              "batch_id": batch_id})
            continue

        # Only UNINTENTIONAL drops reach the ceiling. ``tally['filtered']`` is
        # by-design filtering (IDP rows, etc.) and ``tally['incomplete']`` counts
        # rows that were KEPT with a missing optional field — neither is data
        # loss, and folding them in failed a healthy adp_rankings pull at 35% on
        # the first live run (item 3.1b, 2026-07-24).
        dropped = tally["dropped"]
        seen = written + dropped
        # Denominator matches the ratio that is actually tested. ``tally['total']``
        # is a SUM over every note_drops call, so for an ingester that reports
        # twice over different populations it exceeds the rows that ever existed.
        reason = None
        if written == 0 and dropped > 0:
            # The silent-zero signature: the pull succeeded, the ingester threw
            # every row away. Never 'ok'.
            status = STATUS_FAILED
            reason = (f"wrote 0 rows and dropped {dropped}/{seen} — every row was "
                      "unstampable (is schedules ingested for this season?)")
        elif written == 0:
            status = STATUS_EMPTY
            reason = "upstream returned nothing to store"
        elif dropped and dropped / seen > _MAX_DROP_FRACTION:
            # A DROP RATIO, not the zero boundary. One surviving row out of 19,421
            # used to be `partial`, and `partial` was excluded from the failure
            # list, the exit code AND the staleness verdict — so a new team abbr
            # missing from base.TEAM_ALIASES could drop 99.7% of a pull and the
            # report would say "no failures" and "fresh".
            status = STATUS_FAILED
            reason = (f"wrote {written} rows but dropped {dropped}/{seen} "
                      f"({dropped / seen:.0%} — over the {_MAX_DROP_FRACTION:.0%} ceiling); "
                      "an unresolvable key (new team abbr? missing crosswalk?) rather than "
                      "a few odd rows")
        elif dropped:
            status = STATUS_PARTIAL
            reason = f"wrote {written} rows, dropped {dropped}/{seen}"
        else:
            status = STATUS_OK
        finish_run(conn, run_id, status=status, finished_at=_utc_now(),
                   rows_written=written, rows_dropped=dropped, error=reason)
        summaries.append({"source": spec.name, "status": status, "reason": reason,
                          "rows": written, "dropped": dropped, "run_id": run_id,
                          "scope": decision.scope, "batch_id": batch_id})

    return summaries


# ----------------------------------------------------------------- reporting

VERDICT_FRESH = "fresh"
VERDICT_STALE = "stale"
VERDICT_EXPIRED = "expired"
VERDICT_NEVER = "never"
VERDICT_NA = "n/a"
VERDICT_BLOCKED = "blocked"
#: Never succeeded, and the last attempt says upstream has not published this
#: season yet. Distinct from ``never`` on purpose: six sources are legitimately
#: in this state every day until ~Sept 10, and eighteen weeks of them sitting in
#: a NEVER PULLED alarm is exactly how the report earns being ignored.
VERDICT_AWAITING = "awaiting"

# Multiple of a source's interval past which 'stale' becomes 'expired'. Two
# levels rather than one so a caller can proceed on a warning but refuse on an
# error (item 3.2's staleness banner reads this).
_EXPIRED_MULTIPLE = 3


def source_freshness(conn, *, season: int, today) -> list[dict]:
    """Per-source: last successful pull, age, and a verdict. The staleness
    contract item 3.2's banner reads.

    NOT an as-of accessor and deliberately so — ``nfl_ingest_runs`` is
    operational metadata, exactly like ``league_sync_runs``. But it IS bounded by
    ``today``: a run that had not happened yet on the day being asked about is
    invisible, the run-log equivalent of the retrieval gate. Without that bound,
    "was my data fresh when I set the Week 5 lineup?" was answered `fresh` on the
    strength of a November pull, with a negative age that pinned the verdict at
    fresh forever (3.1b audit finding).

    ``never`` is only alarming relative to phase, so it is resolved against the
    registry rather than against zero: a source that has nothing to say in this
    phase reports ``n/a``, not a warning.
    """
    day = normalize_as_of(today)
    phase = season_phase(conn, season=season, today=day)
    out = []
    for spec in SOURCES:
        def find(status, name=spec.name):
            return last_run(conn, source=name, season=season, status=status, through=day)

        last_ok = find(STATUS_OK)
        if last_ok is None:  # a 'partial' pull still landed rows worth counting
            last_ok = find(STATUS_PARTIAL)
        last_any = find(None)
        applicable = phase in spec.phases or phase == PHASE_UNKNOWN
        if applicable and spec.applicable is not None:
            # Same principle as the phase gate, one notch finer: game_weather is
            # in-phase all preseason but has nothing to fetch until week 1 is
            # inside the forecast wall, and reporting NEVER PULLED on it for six
            # weeks is the wolf-cry this verdict exists to avoid.
            try:
                ctx = IngestContext(conn=conn, season=season,
                                    retrieved_as_of=str(day), today=str(day))
                applicable = not spec.applicable(ctx)
            except Exception:  # a status report must never fail on a predicate
                pass

        age = None
        if last_ok is not None:
            age = (day - normalize_as_of(last_ok["retrieved_as_of"])).days
            # `through` makes this unreachable; assert rather than trust, because a
            # negative age silently reads as maximal freshness.
            assert age >= 0, f"{spec.name}: run log leaked a future run ({age}d)"

        if spec.blocked:
            verdict = VERDICT_BLOCKED
        elif not applicable:
            verdict = VERDICT_NA
        elif last_ok is None:
            verdict = (VERDICT_AWAITING
                       if last_any is not None and last_any["status"] == STATUS_ABSENT
                       else VERDICT_NEVER)
        elif age <= spec.interval_days:
            verdict = VERDICT_FRESH
        elif age <= spec.interval_days * _EXPIRED_MULTIPLE:
            verdict = VERDICT_STALE
        else:
            verdict = VERDICT_EXPIRED

        out.append({
            "source": spec.name,
            "group": spec.group,
            "verdict": verdict,
            "perishable": spec.perishable,
            "interval_days": spec.interval_days,
            "age_days": age,
            "last_ok": last_ok["retrieved_as_of"] if last_ok is not None else None,
            "last_attempt": last_any["started_at"] if last_any is not None else None,
            "last_status": last_any["status"] if last_any is not None else None,
            "rows": last_ok["rows_written"] if last_ok is not None else None,
            "error": last_any["error"] if last_any is not None else None,
            "blocked": spec.blocked,
        })
    return out


def run_failed(summaries: Sequence[dict]) -> bool:
    """Did this run fail? The single definition the CLI's exit code calls.

    Rule 3 (no logic in the CLI) applied to a policy question: "which statuses
    count as a failure" was a bare string literal in ``cli/main.py`` and had
    already drifted from ``format_run``'s own problem list, so a run could print
    ``PROBLEMS: adp_rankings`` and still exit 0 — which under ``Restart=on-failure``
    means an empty pull of a PERISHABLE source is reported to systemd as success
    and never retried.
    """
    return any(s["status"] in PROBLEM_STATUSES for s in summaries)


def format_run(summaries: Sequence[dict]) -> str:
    """The scheduled run's log line(s) — what lands in the journal."""
    if not summaries:
        return "ingest: no sources selected"
    lines = []
    for s in summaries:
        line = f"[{s['status']:>15}] {s['source']:<14} rows={s.get('rows', 0)}"
        if s.get("scope"):
            line += f"  ({s['scope']})"
        if s.get("reason"):
            line += f"  — {s['reason']}"
        lines.append(line)
    bad = [s["source"] for s in summaries if s["status"] in PROBLEM_STATUSES]
    # Not a failure (upstream simply has nothing yet), but never silent: this is
    # the bucket a misclassified real error would hide in, so it is always named.
    absent = [s["source"] for s in summaries if s["status"] == STATUS_ABSENT]
    lines.append(
        f"ingest {summaries[0]['batch_id']}: {len(summaries)} sources, "
        + (f"PROBLEMS: {', '.join(bad)}" if bad else "no failures")
    )
    if absent:
        lines.append(f"  not published upstream yet: {', '.join(absent)}")
    return "\n".join(lines)


def format_plan(decisions: Sequence[Decision]) -> str:
    """The ``--dry-run`` report: what WOULD be pulled, with no network touched."""
    lines = ["ingest plan (dry run — no network, no writes)"]
    for d in decisions:
        verb = "PULL" if d.action == "pull" else d.action.upper()
        lines.append(f"  {verb:>15}  {d.name:<14} {d.scope or ''}")
        if d.action != "pull" or d.reason != "due":
            lines.extend(textwrap.wrap(
                d.reason, width=92, initial_indent="                   └─ ",
                subsequent_indent="                      ",
            ))
    if any(d.action != "pull" for d in decisions):
        lines.append("  (this plan is computed against the database AS IT IS NOW. Sources "
                     "unlocked by this same run — anything waiting on schedules, on a "
                     "bootstrap database — will pull for real and are not shown.)")
    return "\n".join(lines)


def format_status(conn, *, season: int, today) -> str:
    """The operational health report: per-source last successful pull + staleness.

    Says PERISHABLE next to the four sources where a missed day is a lost
    observation, and says nothing of the sort next to the nflverse ones, where a
    missed day is re-pullable. That distinction is the whole point — an
    undifferentiated alarm trains the operator to ignore the real one.

    Renders the LAST ATTEMPT as well as the last success, which the first version
    did not: a source that succeeded once and has failed every run since printed
    a bare ``fresh``, and a run killed mid-flight by ``TimeoutStartSec`` left an
    orphaned ``running`` row that nothing ever reported. That is the item-3.1
    "a degraded run still logged ok" defect moved one layer out into the report
    the operator is told to trust (3.1b audit finding).
    """
    day = normalize_as_of(today)
    phase = season_phase(conn, season=season, today=day)
    rows = source_freshness(conn, season=season, today=day)

    lines = [
        f"nfl ingest status — season {season}, phase {phase} (as of {day.isoformat()})",
        f"  {'source':<14} {'verdict':<9} {'last ok':<12} {'age':>4}  {'rows':>7}  "
        f"{'last try':<14} notes",
    ]
    if not any(r["last_ok"] for r in rows):
        # Freshness is measured from the RUN LOG, never from MAX(retrieved_as_of)
        # on the fact table — that lies three verified ways (a partially-committed
        # failed ingest stamps today on 5% of rows; a legitimately empty pull never
        # advances it; a table is fresh while one partition is days old). So a
        # populated table with no logged run correctly reads 'never'.
        lines.append("  (no runs logged yet — freshness is measured from nfl_ingest_runs, "
                     "not from the tables, so pre-cadence data reads as 'never'.)")
    for r in rows:
        age = "—" if r["age_days"] is None else f"{r['age_days']}d"
        note = "PERISHABLE" if r["perishable"] else ""
        if r["verdict"] == VERDICT_BLOCKED:
            note = "BLOCKED"
        elif r["verdict"] in (VERDICT_STALE, VERDICT_EXPIRED):
            note = (note + " " if note else "") + f"(interval {r['interval_days']}d)"
        # The last ATTEMPT, so "never succeeded but tried today" is distinguishable
        # from "never ran at all", and a run of failures behind a fresh verdict is
        # visible in the same row.
        tried = r["last_status"] or "—"
        lines.append(
            f"  {r['source']:<14} {r['verdict']:<9} {str(r['last_ok'] or '—'):<12} "
            f"{age:>4}  {str(r['rows'] if r['rows'] is not None else '—'):>7}  "
            f"{tried:<14} {note}"
        )

    expired = [r["source"] for r in rows if r["verdict"] == VERDICT_EXPIRED]
    never = [r["source"] for r in rows if r["verdict"] == VERDICT_NEVER]
    awaiting = [r["source"] for r in rows if r["verdict"] == VERDICT_AWAITING]
    # Only a source that WAS being captured and then stopped has lost anything.
    # A never-pulled perishable source has no gap — it has no history at all, and
    # saying "lost" about it would be the crying-wolf this report exists to avoid.
    lost = [r["source"] for r in rows if r["perishable"] and r["verdict"] == VERDICT_EXPIRED]
    # The LAST ATTEMPT channel, independent of the freshness verdict: a source can
    # read `fresh` (interval 7d) while failing every run for six days, and the
    # failure is the actionable fact.
    # STATUS_RUNNING counts: after a TimeoutStartSec SIGTERM the row stays
    # `running` until some LATER run reaps it, so it is the last attempt for a
    # whole day and reporting only `abandoned` would miss exactly the window that
    # matters. A genuinely in-flight run reads the same, which is honest.
    broken = [r for r in rows if r["last_status"] in
              (STATUS_FAILED, STATUS_EMPTY, STATUS_ABANDONED, STATUS_RUNNING)]
    if never:
        lines.append(f"  NEVER PULLED : {', '.join(never)}")
    if awaiting:
        lines.append(f"  NOT PUBLISHED UPSTREAM YET: {', '.join(awaiting)} — attempted and "
                     "upstream has no data for this season yet. Expected until ~Sept 10.")
    if expired:
        lines.append(f"  EXPIRED      : {', '.join(expired)}")
    if lost:
        lines.append(
            f"  PERISHABLE + EXPIRED: {', '.join(lost)} — these serve the CURRENT value "
            "only, so the days since the last pull were not merely missed, they are gone."
        )
    for r in broken:
        unfinished = r["last_status"] in (STATUS_ABANDONED, STATUS_RUNNING)
        label = "RUN NEVER FINISHED" if unfinished else "LAST ATTEMPT FAILED"
        detail = (r["error"] or "").replace("\n", " ")
        if len(detail) > 220:
            detail = detail[:217] + "..."
        if not detail and unfinished:
            detail = ("no finish recorded — either in flight right now or killed mid-pull "
                      "(systemd TimeoutStartSec), in which case every source after it in "
                      "the run never started")
        lines.append(f"  {label}: {r['source']} ({r['last_status']}, attempted "
                     f"{r['last_attempt']}) — {detail or 'no error recorded'}")
    blocked = [r for r in rows if r["verdict"] == VERDICT_BLOCKED]
    for r in blocked:
        lines.append(f"  BLOCKED      : {r['source']} — {r['blocked']}")
    if not (never or expired or blocked or broken or awaiting):
        lines.append("  all applicable sources fresh or stale-but-recoverable.")
    return "\n".join(lines)


def format_sources() -> str:
    """The registry as a table — what exists, when it runs, what missing it costs."""
    lines = [f"  {'source':<14} {'group':<8} {'phases':<28} {'every':<6} flags"]
    for spec in SOURCES:
        flags = []
        if spec.perishable:
            flags.append("perishable")
        if spec.replaces_partition:
            flags.append("replaces-partition")
        if spec.needs_credentials:
            flags.append("needs-credentials")
        if spec.needs_schedules:
            flags.append("needs-schedules")
        if spec.blocked:
            flags.append("BLOCKED")
        phases = "all" if spec.phases == ALL_PHASES else ",".join(sorted(spec.phases))
        lines.append(
            f"  {spec.name:<14} {spec.group:<8} {phases:<28} {str(spec.interval_days) + 'd':<6} "
            f"{', '.join(flags)}"
        )
    return "\n".join(lines)
