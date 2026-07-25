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

ITEM 3.2c amended the registry in five places, each a defect recon measured in
shipped 3.1b code rather than a new feature:

* **F-A** ``injuries`` and ``game_odds`` gained ``PHASE_OFFSEASON``. The phase
  gate is checked BEFORE the force-able interval gate, so ``ingest run --season
  2023 --force`` could not pull either at all. Fixed by widening the specs, NOT
  by a bypass flag — a bypass forks the decision path, which is the failure class
  3.1b's audit spent a whole round on.
* **F-B** ``depth_charts`` unblocked onto the v2 dated-panel module, bringing two
  new seams with it: ``season_resolver`` (the March handover) and ``quiet_ok``
  (the ~2% of days upstream publishes no panel).
* **F-C** ``_ANCHOR_STATUSES``: the interval gate and the freshness report now
  read ONE definition of "this source landed rows".
* **F-D** ``VERDICT_ARCHIVED``: a completed season's data never goes stale.
* **F-G** a same-batch primary-key COLLAPSE counts against ``_MAX_DROP_FRACTION``
  alongside a drop. It is silent data loss, not by-design filtering.
"""

import hashlib
import logging
import re
import textwrap
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ziggurat.data.asof import nfl_season_of, normalize_as_of
from ziggurat.data.nfl import (
    adp_rankings,
    base,
    depth_charts,
    depth_charts_weekly,
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

#: Statuses recorded by a run that never opened a network connection and never
#: wrote a row — ``decide()``'s four non-pull answers. They are still LOGGED (a
#: source silently dropped from the cadence is the failure this module exists to
#: expose); what they must not do is count as evidence that something is actively
#: WRITING a season. ``backfill_seasons``' active-cadence fence reads this, so a
#: single diagnostic `ingest run` against a past season no longer strands that
#: season for thirty days (item 3.2c audit, C7-1c). Deliberately NOT here:
#: `empty`, `failed` and `abandoned` — all three mean a pull was attempted, which
#: is exactly what that fence is looking for.
_NON_WRITING_STATUSES = (STATUS_FRESH, STATUS_SKIPPED, STATUS_BLOCKED, STATUS_ABSENT)

#: Statuses that mean "this source LANDED ROWS for this season" — the single
#: definition of what anchors the interval gate and what counts as the last
#: successful pull. ONE constant read by ``decide``, ``source_freshness`` and
#: ``run_ingest``'s upstream-absent guard, because the three of them having their
#: own idea of it is precisely the defect this fixes (item 3.2c, F-A/F-C).
#:
#: MEASURED DEFECT (3.2c recon, probe 4 F3): ``decide`` read only ``ok`` while
#: ``source_freshness`` fell back to ``partial``. ``weekly_stats`` drops the same
#: 22 null-``player_id`` rows out of every season file and the three ``ngs_*``
#: sources drop the week-23 Super Bowl rows, so all four are ``partial`` BY
#: CONSTRUCTION on every single healthy run and therefore NEVER anchored:
#: ``weekly_stats last_ok=False last_partial=True decide=pull status_verdict=fresh``.
#: In-season that made the daily-firing weekly unit re-download four whole-season
#: parquets EVERY DAY off free volunteer infrastructure, while the status report
#: said everything was fine.
#:
#: THE TRADE-OFF, stated rather than discovered later: after this, a ``partial``
#: pull that lost up to ``_MAX_DROP_FRACTION`` (19%) of its rows ANCHORS the
#: interval instead of being retried tomorrow. A source that is partial because
#: something genuinely broke therefore stays broken for its whole interval rather
#: than self-healing. That is the right trade here because the four sources in
#: question are partial on every correct run — a self-heal that fires daily on a
#: healthy source is not a self-heal, it is a loop — and because anything losing
#: MORE than the ceiling is already ``failed``, which still does not anchor. This
#: is a deliberate live-cadence behaviour change made inside a historical-backfill
#: item; ``--force`` remains the manual override.
_ANCHOR_STATUSES = (STATUS_OK, STATUS_PARTIAL)

#: Fraction of a pull's rows that may be dropped before the pull counts as
#: failed rather than merely partial. The zero boundary alone was not enough: a
#: single surviving row out of 19,421 flipped the outcome to `partial`, which the
#: failure list, the exit code and the staleness verdict all treated as success
#: (measured 3.1b audit: 67 written / 19,354 dropped reported `fresh`).
#:
#: What counts toward it is DROPPED + COLLAPSED (item 3.2c, F-G) — see
#: ``run_ingest``. ``filtered`` (by-design) and ``duplicated`` (byte-identical
#: same-key rows) deliberately do not.
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
    #: Optional ``(conn, *, season, today) -> int``: which season's FILE upstream
    #: actually serves today, when that is not the season the operator asked for.
    #: Resolved ONCE per source per run, BEFORE ``start_run`` writes the run-log
    #: row and before the interval gate and ``applicable`` read it — otherwise the
    #: log records a season that was never pulled and the freshness report answers
    #: about a partition holding nothing (``last_run``'s own docstring records that
    #: exact failure). Only ``depth_charts`` needs it: for two weeks each March the
    #: live panel is still published inside the PREVIOUS season's file, because
    #: ziggurat's league year flips on Mar 1 and nflreadpy's on Mar 15.
    season_resolver: object = None
    #: True when "this pull wrote nothing" is a NORMAL upstream outcome rather
    #: than a failure — and the ``applicable`` predicate structurally cannot see it
    #: in advance because seeing it needs the download, which ``decide`` may not
    #: do (it is pure of the network so ``--dry-run`` reports exactly it).
    #: ``depth_charts`` is the population: upstream publishes no panel at all on
    #: ~2% of days (measured 5 of 224 days in 2025, 1 of 126 in 2026), on which the
    #: whole-file diff correctly yields zero new events. Without this the source
    #: records ``empty`` -> PROBLEM_STATUSES -> CLI exit 1 -> ``Restart=on-failure``
    #: and a standing "LAST ATTEMPT FAILED" on a perfectly healthy source, which is
    #: the wolf-cry this whole module is designed against. Only ever applies when
    #: NOTHING was dropped or collapsed: a pull that wrote 0 and lost rows is the
    #: silent-zero signature and stays ``failed``.
    quiet_ok: bool = False
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


def _pull_depth_charts(ctx) -> int:
    return depth_charts.pull_depth_charts(
        ctx.conn, ctx.season, retrieved_as_of=ctx.retrieved_as_of
    )


def _depth_charts_season(conn, *, season: int, today) -> int:
    """The ``season_resolver`` seam — see ``depth_charts.resolve_season``."""
    return depth_charts.resolve_season(season=season, today=today)


def _depth_charts_applicable(ctx) -> str | None:
    if ctx.season < depth_charts.PANEL_MIN_SEASON:
        # The REGIME gate, stated here rather than discovered by a raise. Upstream
        # replaced the weekly regime with the dated panel in 2025, so this spec has
        # nothing to say about 2021-2024 — those live in `depth_charts_weekly`, a
        # different table reached only by the backfill. Without this,
        # `ingest run --season 2023` records `failed` on a source that is working
        # perfectly and merely does not cover that season, and `ingest status
        # --season 2023` lists it as a backfill gap the cadence could close.
        return (f"season {ctx.season} predates the dated-panel regime (first panel season "
                f"is {depth_charts.PANEL_MIN_SEASON}); the 2021-{depth_charts_weekly.WEEKLY_MAX_SEASON} "
                "weekly regime is a separate table and a backfill-only source "
                "(depth_charts_weekly), never part of the cadence")
    return depth_charts.nothing_new_to_pull(ctx.conn, season=ctx.season, today=ctx.today)


def _pull_depth_charts_weekly(ctx) -> int:
    return depth_charts_weekly.pull_depth_charts_weekly(
        ctx.conn, [ctx.season], retrieved_as_of=ctx.retrieved_as_of
    )


def _depth_charts_weekly_applicable(ctx) -> str | None:
    """The REGIME gate, mirroring ``_depth_charts_applicable`` from the other side.

    MEASURED, not anticipated: the first real 2021-2025 backfill recorded
    ``depth_charts_weekly/2025 FAILED — PanelDepthChartFrame: seasons [2025] are
    past the weekly regime``, which made the whole five-season run exit 1 on a
    source that was working perfectly and merely does not cover that year. The
    ingester's raise is right (it refuses to store a panel frame in the weekly
    table); what was wrong was letting the orchestrator meet it. A regime boundary
    is a fact about upstream, and both specs now state it before the pull rather
    than discovering it in a traceback.

    The boundary is read from the modules' own constants, never a literal, so the
    two regimes cannot drift apart silently.
    """
    if ctx.season > depth_charts_weekly.WEEKLY_MAX_SEASON:
        return (f"season {ctx.season} is past the weekly depth-chart regime (last weekly "
                f"season is {depth_charts_weekly.WEEKLY_MAX_SEASON}); upstream replaced it "
                f"with the dated panel from {depth_charts.PANEL_MIN_SEASON}, which the "
                "`depth_charts` source stores in a different table")
    return None


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
        phases=frozenset({PHASE_INSEASON, PHASE_OFFSEASON}),
        notes="Closing lines ride the schedules frame. NOTE (verified): knowable_as_of is "
              "the GAMEDAY, so no pre-kickoff reader can see a line — item 3.5 needs a "
              "pre-game regime (like weather's forecast/archive split) before this is "
              "useful. Pulled anyway so the history accrues. OFFSEASON included (item "
              "3.2c, F-A): this is a whole-season file that is re-pullable forever, and "
              "the phase gate is checked BEFORE the force-able interval gate, so without "
              "it `ingest run --season 2023 --force` refused to pull a completed season "
              "at all.",
    ),
    SourceSpec(
        name="injuries", group=GROUP_DAILY, pull=_pull_injuries, scope=_season_scope,
        phases=frozenset({PHASE_INSEASON, PHASE_OFFSEASON}), needs_schedules=True,
        notes="nflverse's injury feed died after 2024; 2025 exists only as a post-season "
              "bulk backfill and no longer carries date_modified, so every row now falls "
              "back to the team-gameday anchor. Mid-week injury NEWS must come from the "
              "live ESPN league-state sync (item 3.1), not from this table. OFFSEASON "
              "included (item 3.2c, F-A) for the same reason as game_odds — and note that "
              "the 2025 file was itself published as a post-season bulk backfill, i.e. "
              "IN the offseason, which the old phase set could never have pulled.",
    ),
    SourceSpec(
        name="depth_charts", group=GROUP_DAILY, pull=_pull_depth_charts, scope=_season_scope,
        season_resolver=_depth_charts_season, applicable=_depth_charts_applicable,
        phases=ALL_PHASES, interval_days=1, perishable=False, needs_credentials=False,
        replaces_partition=False, needs_schedules=False, blocked=None, quiet_ok=True,
        notes="Daily snapshot panel keyed on `dt`, stored as a change log + tombstones "
              "(depth_chart_slots) with one observation row per snapshot "
              "(depth_chart_panels). The ONLY nflverse source that publishes year-round "
              "(219 of 224 days Aug-Mar in 2025; 125 of 126 Mar-Jul in 2026), hence "
              "phases=all. needs_schedules is FALSE on purpose: `dt` IS the knowledge "
              "time, so there is no gameday map to stamp against — and setting it True "
              "would also drop this source out of the PHASE_UNKNOWN bootstrap set. In "
              "March 1-14 the current panel still lives in the PREVIOUS season's file "
              "(season_resolver). NOT an injury/availability signal: a starter ruled Out "
              "is not demoted (measured — see IMPLEMENTATION_PLAN 3.2c). The 2021-2024 "
              "WEEKLY regime is a different table and a different, backfill-only spec.",
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


# Sources that exist ONLY to be replayed into history and have nothing to say
# today. They are deliberately NOT in ``SOURCES``: putting them there would make
# `ingest run` attempt them daily and `ingest status` report them stale forever,
# which is a standing alarm on something that is working perfectly. They are
# routed through ``run_ingest`` by the backfill path, so they still get the run
# log, the rollback fence and the drop ceiling.
#
# RECORD THE CONSEQUENCE (Rule 7): `ziggurat ingest sources` names them in a
# footer, but `ingest status` will NOT list them and `--source depth_charts_weekly`
# is an unknown name. The run log is where their pulls are visible.
_DEPTH_CHARTS_WEEKLY_SPEC = SourceSpec(
    name="depth_charts_weekly", group=GROUP_WEEKLY, pull=_pull_depth_charts_weekly,
    scope=_season_scope, phases=ALL_PHASES, interval_days=365, perishable=False,
    needs_schedules=True, applicable=_depth_charts_weekly_applicable,
    notes="LEGACY 2021-2024 weekly regime; upstream replaced it with the dated panel in "
          "2025. A SEPARATE TABLE permanently (depth_charts_weekly), never routed into "
          "the v2 panel: four of the panel's five key columns do not exist in the weekly "
          "frame and the panel's occupant column is its tombstone flag, so a join would "
          "have landed ~1 row in 6 as a FABRICATED vacancy. Raises for any season > 2024.",
)

#: Backfill-only specs, by name. The backfill path resolves them alongside
#: ``SOURCES_BY_NAME``; nothing in the daily cadence sees them.
BACKFILL_ONLY_SOURCES: tuple[SourceSpec, ...] = (_DEPTH_CHARTS_WEEKLY_SPEC,)
BACKFILL_ONLY_BY_NAME = {spec.name: spec for spec in BACKFILL_ONLY_SOURCES}


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
    #: The season this source's FILE will actually be requested for — normally
    #: the season asked about, but see ``SourceSpec.season_resolver``. Carried on
    #: the Decision so ``decide`` resolves it ONCE and ``run_ingest`` cannot log a
    #: different season from the one the interval gate and ``applicable`` consulted.
    season: int | None = None


def resolve_source_season(conn, spec: SourceSpec, *, season: int, today) -> int:
    """Which season's file ``spec`` should be asked for on ``today``.

    The identity for every source but ``depth_charts``. Kept as one function so
    the decision, the run log, the pull and the freshness report all read the
    same answer — a resolver applied in some of those places and not others is
    how ``ingest status`` ends up reporting a season nothing was ever pulled for.
    """
    if spec.season_resolver is None:
        return season
    return int(spec.season_resolver(conn, season=season, today=today))


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

    The PHASE gate is judged on the season the caller ASKED about ("where in the
    calendar are we"); everything below it — the dependency check, the pull
    context, ``applicable`` and the interval gate — is judged on the season whose
    FILE will actually be requested (``resolve_source_season``). They differ only
    for ``depth_charts`` in the first half of March.

    ``BACKFILL_EXCLUDED`` is enforced HERE as well as in the backfill (item 3.2c
    audit, C8) — one list, both doors. See the comment on that gate below.
    """
    if spec.blocked:
        return Decision(spec.name, STATUS_BLOCKED, spec.blocked, season=season)

    if spec.needs_credentials and not have_credentials:
        return Decision(spec.name, STATUS_SKIPPED,
                        "needs ESPN credentials (SWID / ESPN_S2 / league id) and none were resolved",
                        season=season)

    phase = season_phase(conn, season=season, today=today)
    if phase == PHASE_UNKNOWN:
        # Nothing phase-gated can be judged yet. Sources that neither depend on
        # schedules nor restrict their phases are exactly the bootstrap set.
        if spec.needs_schedules or spec.phases != ALL_PHASES:
            return Decision(spec.name, STATUS_SKIPPED,
                            f"season {season} phase unknown — schedules not ingested yet",
                            season=season)
    elif phase not in spec.phases:
        return Decision(spec.name, STATUS_SKIPPED,
                        f"nothing to pull in the {phase} phase "
                        f"(applies in: {', '.join(sorted(spec.phases))})",
                        season=season)

    # THE SAME HAZARD, THE OTHER DOOR (item 3.2c audit, C8). `BACKFILL_EXCLUDED`
    # was enforced only by `ingest backfill`, so `ingest run --season 2023
    # --source projections` walked straight past it and MEASURED 57,910 rows of
    # today's Sleeper board written into the 2023 partition stamped
    # knowable_as_of = today — invisible under both views, indistinguishable from
    # history in the table, and logged `ok`. That is the manufactured-leak shape
    # every leakage test passes, and it is exactly what disqualified
    # `ff_opportunity` during recon.
    #
    # Placed AFTER the phase gate on purpose: for espn_ranks the phase answer
    # ("nothing to pull in the offseason phase") is the more specific one, and the
    # module's standing rule is that the more informative refusal wins when both
    # apply. Placed before EVERYTHING that pulls, and before the interval gate, so
    # `--force` cannot reach it.
    if season < nfl_season_of(normalize_as_of(today)) and spec.name in BACKFILL_EXCLUDED:
        return Decision(
            spec.name, STATUS_BLOCKED,
            f"refused: season {season} is COMPLETE and {spec.name} serves TODAY's value "
            "only, so a pull would file today's data under a past season stamped "
            "knowable_as_of = today — rows that read empty under both views and look "
            "like history in the table. The same refusal `ingest backfill` gives, for "
            f"the same recorded reason: {BACKFILL_EXCLUDED[spec.name]} "
            f"Run it for the current season instead (--season "
            f"{nfl_season_of(normalize_as_of(today))}), which is the only one it can "
            "serve. Not overridable by --force.",
            season=season,
        )

    # Resolved ONCE, here, and carried on the Decision — see Decision.season.
    run_season = resolve_source_season(conn, spec, season=season, today=today)

    if spec.needs_schedules and not season_weeks(conn, season=run_season):
        # The measured silent-zero: without this the source drops 100% of its
        # rows, returns 0 and raises nothing.
        return Decision(spec.name, STATUS_SKIPPED,
                        f"schedules not ingested for season {run_season} — this source stamps "
                        "knowable_as_of from the gameday map and would drop every row",
                        season=run_season)

    ctx = IngestContext(conn=conn, season=run_season,
                        retrieved_as_of=str(today), today=str(today))

    if spec.applicable is not None:
        # Deliberately BEFORE the interval gate: "nothing to do" is not staleness,
        # and a source skipped for this reason must not be nagged about tomorrow.
        nothing_to_do = spec.applicable(ctx)
        if nothing_to_do:
            return Decision(spec.name, STATUS_SKIPPED, nothing_to_do, season=run_season)

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
    #
    # ANCHORED ON _ANCHOR_STATUSES, not on `ok` alone (item 3.2c, F-C). The same
    # constant source_freshness reads, so "the scheduler thinks it is due" and
    # "the report thinks it is fresh" cannot disagree again.
    if not force:
        recent = last_landing(conn, source=spec.name, season=run_season, through=today)
        if recent is not None:
            age = (normalize_as_of(today) - normalize_as_of(recent["retrieved_as_of"])).days
            if 0 <= age < spec.interval_days:
                return Decision(
                    spec.name, STATUS_FRESH,
                    f"last successful pull was {age}d ago and this source refreshes every "
                    f"{spec.interval_days}d (pass --force to pull anyway)", scope,
                    season=run_season,
                )
    return Decision(spec.name, "pull", "due", scope, season=run_season)


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

    THE REAP IS SCOPED TO (source, season) — item 3.2c, §2.6g. It was scoped to
    ``source`` alone, which is a standalone bug the historical backfill makes
    reachable every time it runs: a backfill's ``start_run`` for
    ``(schedules, 2021)`` flipped the IN-FLIGHT ``(schedules, 2026)`` row written
    by the 07:20 daily unit — which a multi-minute backfill trivially overlaps —
    to ``abandoned``, with a fabricated cause that says the process died. It
    self-heals when the live ``finish_run`` lands, but if that run is then killed
    by ``TimeoutStartSec`` the operator is left holding a run log that
    mis-describes what happened to the OTHER season. A season predicate costs
    nothing and the two partitions are genuinely independent pulls.
    """
    conn.execute(
        "UPDATE nfl_ingest_runs SET status = ?, error = COALESCE(error, ?) "
        "WHERE source = ? AND season IS ? AND status = ?",
        (STATUS_ABANDONED,
         "no finish_run ever landed — the process died mid-pull (systemd "
         "TimeoutStartSec, a kill, or a crash); a later run found the row orphaned",
         source, season, STATUS_RUNNING),
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


def last_run(conn, *, source: str, season: int | None = None,
             status: str | Sequence[str] | None = STATUS_OK, through=None):
    """The most recent run row for one source (by default the last SUCCESSFUL one).

    ``status`` accepts a single status, a sequence of them (matched with ``IN``,
    which is what ``_ANCHOR_STATUSES`` needs), or None for "any status". A
    sequence is NOT the same as calling this twice and preferring the first hit:
    `ok` on Monday and `partial` on Friday must answer FRIDAY, and the two-call
    form answered Monday — which is exactly how the interval gate and the
    freshness verdict drifted apart (item 3.2c, F-C).

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
        if isinstance(status, str):
            clauses.append("status = :status")
            params["status"] = status
        else:
            names = [f"status_{i}" for i in range(len(status))]
            clauses.append(f"status IN ({', '.join(':' + n for n in names)})")
            params.update(dict(zip(names, status, strict=True)))
    if through is not None:
        clauses.append("retrieved_as_of <= :through")
        params["through"] = normalize_as_of(through).isoformat()
    return conn.execute(
        f"SELECT * FROM nfl_ingest_runs WHERE {' AND '.join(clauses)} "
        "ORDER BY run_id DESC LIMIT 1",
        params,
    ).fetchone()


def last_landing(conn, *, source: str, season: int | None = None, through=None):
    """The most recent run for one source that actually LANDED ROWS.

    The single reading of ``_ANCHOR_STATUSES``, shared by the interval gate, the
    freshness verdict and ``run_ingest``'s upstream-absent guard. Anything asking
    "when did this source last do its job" calls THIS rather than ``last_run``
    with a hand-picked status — three call sites each picking their own is the
    defect F-C fixed.
    """
    return last_run(conn, source=source, season=season, status=_ANCHOR_STATUSES,
                    through=through)


def batch_runs(conn, *, batch_id: str) -> list:
    return conn.execute(
        "SELECT * FROM nfl_ingest_runs WHERE batch_id = ? ORDER BY run_id", (batch_id,)
    ).fetchall()


# ------------------------------------------------------------- orphan reaping
#
# A `running` row is written BEFORE the network call and only ``finish_run``
# clears it, so a killed process leaves one behind forever (that is deliberate —
# silence is not success). What was NOT deliberate is that ``run_backfill``
# refuses to start while ANY `running` row exists, and the only reaper in the
# package was ``start_run``'s (source, season)-scoped one — reachable only by
# running that exact pair again. For the two backfill-only sources
# (``game_weather_archive``, ``depth_charts_weekly``) no shipped command could
# reach that pair at all, so a Ctrl-C during the slowest, most interruptible
# operation in the repo left `ingest backfill` permanently refusing itself with
# no remedy short of hand-editing SQLite (item 3.2c audit, C7).

#: How long a `running` row must have sat untouched before anything calls it an
#: orphan. Bounded from BELOW by the longest legitimate single pull — the ERA5
#: archive is ~12 s per (season, week), ~3.6 min for one season's 18 weeks, and
#: every network seam in the package is socket-bounded (``ziggurat/net.py``) — so
#: 60 minutes cannot mistake an in-flight pull for a corpse. Bounded from ABOVE
#: by nothing important: reaping is CHEAP TO GET WRONG IN ONE DIRECTION ONLY. If
#: the run is in fact alive its own ``finish_run`` updates the row BY run_id and
#: overwrites the reaped status with the real outcome; what a reap can never do
#: is delete or alter a fact table.
ORPHAN_STALE_MINUTES = 60

#: Written into ``error`` when a reap fires. Says which of the two possible
#: causes it is NOT, because "abandoned" alone reads like data loss (Rule 6).
_REAP_REASON = (
    "no finish_run ever landed and the row had sat `running` for at least "
    "{minutes} minutes, so the process that opened it is gone (a kill, a crash, or "
    "systemd TimeoutStartSec). Reaped {when}. NOTHING WAS DELETED — this row is "
    "run-log metadata; whatever that run had already committed is still in the "
    "tables, and the pair is simply re-pulled by the next run."
)


def _orphan_where(*, cutoff: str, sources, seasons, batch_prefix) -> tuple[str, dict]:
    """The shared predicate for finding orphaned `running` rows.

    ONE predicate, built once, used by both the report and the reap: a reaper
    that can act on rows its own preview did not show is how an operator learns
    to distrust ``--dry-run``.
    """
    clauses = ["status = :running", "started_at < :cutoff"]
    params: dict = {"running": STATUS_RUNNING, "cutoff": cutoff}
    if sources is not None:
        names = {f"src_{i}": name for i, name in enumerate(sources)}
        if not names:
            return "0", {}
        clauses.append(f"source IN ({', '.join(':' + k for k in names)})")
        params.update(names)
    if seasons is not None:
        years = {f"yr_{i}": year for i, year in enumerate(seasons)}
        if not years:
            return "0", {}
        clauses.append(f"season IN ({', '.join(':' + k for k in years)})")
        params.update(years)
    if batch_prefix is not None:
        clauses.append("batch_id LIKE :prefix")
        params["prefix"] = batch_prefix + "%"
    return " AND ".join(clauses), params


def orphan_runs(conn, *, older_than_minutes: int = ORPHAN_STALE_MINUTES, now=None,
                sources: Sequence[str] | None = None,
                seasons: Sequence[int] | None = None,
                batch_prefix: str | None = None) -> list:
    """`running` rows old enough that the process behind them is certainly gone.

    Read-only. ``older_than_minutes=0`` means "every running row", which is the
    honest way to say "I know this one is dead" — it also matches a run started
    this second, hence the flag rather than the default.
    """
    cutoff = _stale_cutoff(now, older_than_minutes)
    where, params = _orphan_where(cutoff=cutoff, sources=sources, seasons=seasons,
                                  batch_prefix=batch_prefix)
    return conn.execute(
        f"SELECT * FROM nfl_ingest_runs WHERE {where} ORDER BY run_id",  # noqa: S608
        params,
    ).fetchall()


def reap_orphan_runs(conn, *, older_than_minutes: int = ORPHAN_STALE_MINUTES, now=None,
                     sources: Sequence[str] | None = None,
                     seasons: Sequence[int] | None = None,
                     batch_prefix: str | None = None) -> list:
    """Flip orphaned `running` rows to ``abandoned``. Returns the rows AS THEY WERE.

    The rows are selected first and updated by ``run_id``, so the report and the
    write can never describe different sets (a second process starting a run
    between the two statements does not get reaped).
    """
    stamp = _resolve_now(now)
    rows = orphan_runs(conn, older_than_minutes=older_than_minutes, now=stamp,
                       sources=sources, seasons=seasons, batch_prefix=batch_prefix)
    if not rows:
        return []
    reason = _REAP_REASON.format(minutes=older_than_minutes, when=stamp)
    conn.executemany(
        "UPDATE nfl_ingest_runs SET status = ?, finished_at = ?, "
        "error = COALESCE(error, ?) WHERE run_id = ?",
        [(STATUS_ABANDONED, stamp, reason, row["run_id"]) for row in rows],
    )
    conn.commit()
    for row in rows:
        logger.warning("ingest: reaped orphaned run %s (%s season %s, batch %s, started %s)",
                       row["run_id"], row["source"], row["season"], row["batch_id"],
                       row["started_at"])
    return rows


def _resolve_now(now) -> str:
    """The run-log wall clock. Not a knowledge time — see ``_utc_now``."""
    return _utc_now() if now is None else str(now)


def _stale_cutoff(now, older_than_minutes: int) -> str:
    """``now - older_than_minutes`` as a run-log timestamp.

    Compared as TEXT against ``started_at``, which is safe only because every
    writer of that column goes through ``_utc_now()`` — one format, one offset,
    so lexical order is chronological order. Anything that wrote a different
    format would sort wrong, which is why nothing else may write this column.
    """
    stamp = _resolve_now(now)
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:      # a hand-written stamp; treat every running row as stale
        return "9999"
    return (moment - timedelta(minutes=older_than_minutes)).isoformat(timespec="seconds")


def format_reap(rows: list, *, older_than_minutes: int, dry_run: bool) -> str:
    """The ``ingest reap`` report. Names what was cleared and what it did not do."""
    verb = "would reap" if dry_run else "reaped"
    if not rows:
        return (f"no orphaned ingest runs: nothing has been marked `running` for "
                f"{older_than_minutes}+ minutes.\n"
                "  (a run that is genuinely in flight is not an orphan and is not "
                "listed — pass --older-than-minutes 0 to see and clear every "
                "`running` row, including a live one.)")
    lines = [f"{verb} {len(rows)} orphaned ingest run(s) "
             f"(marked `running` for {older_than_minutes}+ minutes):"]
    for row in rows:
        lines.append(f"  run {row['run_id']:>5}  {row['source']:<22} season {row['season']}  "
                     f"started {row['started_at']}  batch {row['batch_id']}")
    lines.append("  the run LOG is corrected; no fact table was touched. Re-run the pull "
                 "(`ziggurat ingest run` / `ziggurat ingest backfill`) to land what the "
                 "killed process did not.")
    if dry_run:
        lines.append("  DRY RUN — nothing was written. Re-run without --dry-run to clear.")
    return "\n".join(lines)


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


def _loss_detail(dropped: int, collapsed: int) -> str:
    """Name the two loss channels separately in the operator-facing reason.

    Rule 6: "lost 40 rows" is not actionable, but "22 unstampable, 18 collapsed
    on a primary-key collision" tells the operator which of two completely
    different investigations to open — a missing gameday map versus a wrong
    primary key.
    """
    parts = []
    if dropped:
        parts.append(f"{dropped} unstampable")
    if collapsed:
        parts.append(f"{collapsed} collapsed on a primary-key collision")
    return ", ".join(parts) if parts else "none"


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
        # The season whose FILE this pull will ask for — resolved inside `decide`
        # and carried here, so the run-log row, the interval gate that will read
        # it back tomorrow and the pull itself can never name different seasons.
        run_season = season if decision.season is None else decision.season
        if decision.action != "pull":
            now = _utc_now()
            run_id = record_run(conn, batch_id=batch_id, source=spec.name, season=run_season,
                                scope=decision.scope or None, retrieved_as_of=stamp, at=now,
                                status=decision.action, reason=decision.reason)
            summaries.append({"source": spec.name, "status": decision.action,
                              "reason": decision.reason, "rows": 0, "run_id": run_id,
                              "scope": decision.scope, "batch_id": batch_id})
            continue

        run_id = start_run(conn, batch_id=batch_id, source=spec.name, season=run_season,
                           scope=decision.scope or None, retrieved_as_of=stamp,
                           started_at=_utc_now())
        ctx = IngestContext(conn=conn, season=run_season, retrieved_as_of=stamp, today=today,
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
                # costume, which would otherwise exit 0 forever. `last_landing`,
                # not `ok` alone: a PARTIAL pull also proves the file exists, and
                # weekly_stats/ngs_* are partial on every healthy run (F-C).
                if last_landing(conn, source=spec.name, season=run_season) is None:
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

        # WHAT COUNTS AS LOSS. Two channels, and only two:
        #
        #   * ``dropped``   — the ingester could not stamp the row, so it never
        #                     reached the table.
        #   * ``collapsed`` — the row DID reach the table and another row in the
        #                     same batch overwrote it on the primary key, with a
        #                     DIFFERENT payload (item 3.2c, F-G). Silent data
        #                     loss: base.upsert used to return the offered count,
        #                     so 835-947 depth-chart rows a season vanished while
        #                     the run said `ok` and note_drops reported 0.
        #
        # NOT counted, each for a measured reason:
        #   * ``filtered``   — by-design filtering (the IDP rows adp_rankings
        #                      drops). Folding it in failed a healthy pull at 35%
        #                      on the first live run (item 3.1b, 2026-07-24).
        #   * ``incomplete`` — rows KEPT with a missing optional field. Nothing
        #                      was lost; they are in the table and readable.
        #   * ``duplicated`` — same key, BYTE-IDENTICAL payload. Storing it once
        #                      loses nothing, and the legacy depth-chart files
        #                      carry 145-207 of them per season, which would fail
        #                      a correct pull every time. Tallied so an explosion
        #                      is still visible in the log, never on the ceiling.
        dropped = tally["dropped"]
        collapsed = tally["collapsed"]
        lost = dropped + collapsed
        seen = written + lost
        # Denominator matches the ratio that is actually tested. ``tally['total']``
        # is a SUM over every note_drops call, so for an ingester that reports
        # twice over different populations it exceeds the rows that ever existed.
        detail = _loss_detail(dropped, collapsed)
        reason = None
        if written == 0 and lost > 0:
            # The silent-zero signature: the pull succeeded, the ingester threw
            # every row away. Never 'ok'.
            status = STATUS_FAILED
            reason = (f"wrote 0 rows and lost {lost}/{seen} ({detail}) — every row was "
                      "unstampable (is schedules ingested for this season?) or collided "
                      "with another row in the same batch")
        elif written == 0 and spec.quiet_ok:
            # "Upstream published nothing new" is this source's NORMAL outcome on
            # ~2% of days and cannot be predicted without the download, which
            # `decide` may not do. Recorded, never silent — and it does not anchor
            # the interval, so tomorrow's run tries again. See SourceSpec.quiet_ok.
            status = STATUS_SKIPPED
            reason = ("upstream published no new observation since the stored watermark — "
                      "nothing to store, and for this source that is a normal day rather "
                      "than a failure (measured: ~2% of days carry no panel at all)")
        elif written == 0:
            status = STATUS_EMPTY
            reason = "upstream returned nothing to store"
        elif lost and lost / seen > _MAX_DROP_FRACTION:
            # A DROP RATIO, not the zero boundary. One surviving row out of 19,421
            # used to be `partial`, and `partial` was excluded from the failure
            # list, the exit code AND the staleness verdict — so a new team abbr
            # missing from base.TEAM_ALIASES could drop 99.7% of a pull and the
            # report would say "no failures" and "fresh".
            status = STATUS_FAILED
            reason = (f"wrote {written} rows but lost {lost}/{seen} ({detail}) "
                      f"({lost / seen:.0%} — over the {_MAX_DROP_FRACTION:.0%} ceiling); "
                      "an unresolvable key (new team abbr? missing crosswalk?) or a wrong "
                      "primary key, rather than a few odd rows")
        elif lost:
            status = STATUS_PARTIAL
            reason = f"wrote {written} rows, lost {lost}/{seen} ({detail})"
        else:
            status = STATUS_OK
        finish_run(conn, run_id, status=status, finished_at=_utc_now(),
                   rows_written=written, rows_dropped=lost, error=reason)
        summaries.append({"source": spec.name, "status": status, "reason": reason,
                          "rows": written, "dropped": lost, "run_id": run_id,
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
#: This season is FINISHED and the source landed rows for it. A completed
#: season's data never goes stale — the files upstream serves for it do not
#: change — so running the age ladder over a backfilled season is meaningless
#: (item 3.2c, F-D; measured before the fix: a 2023 backfill read `fresh 0d` on
#: the day it landed, `stale` eight days later and `expired` after twenty-two,
#: none of which described anything that had happened to the data).
VERDICT_ARCHIVED = "archived"

#: Verdicts that are NOT a staleness warning — the set a consumer's banner should
#: stay quiet about. Owned here rather than restated at each call site, because
#: adding a verdict without updating the consumers is how ``ziggurat marginal``
#: would have started printing "ingest says projections: archived" as a staleness
#: alarm on every past-season run (``core/marginal.py`` reads this).
QUIET_VERDICTS = (VERDICT_FRESH, VERDICT_NA, VERDICT_ARCHIVED)

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

    A COMPLETED season reports ``archived`` rather than running the age ladder
    (item 3.2c, F-D). "Archived" is judged on the season the caller ASKED about,
    never on the season a ``season_resolver`` redirected the pull to — otherwise
    ``ingest status --season 2026`` on March 5th, when the live panel is still
    inside the 2025 file, would call today's chart a historical artifact.
    """
    day = normalize_as_of(today)
    phase = season_phase(conn, season=season, today=day)
    # A season strictly before the one `day` falls in is over. Its upstream files
    # are finished artifacts, not a feed, so nothing about them can go stale.
    archived_season = season < nfl_season_of(day)
    out = []
    for spec in SOURCES:
        # The partition the run log actually holds for this source today — see
        # decide(). Identity for everything but depth_charts in early March; if
        # the report skipped it, `ingest status` would say "never pulled" about a
        # source that pulled successfully an hour ago.
        try:
            run_season = resolve_source_season(conn, spec, season=season, today=day)
        except Exception:  # a status report must never fail on a resolver
            run_season = season

        # ANCHORED ON _ANCHOR_STATUSES (item 3.2c, F-C). One IN-query, not two
        # queries with `ok` preferred: `ok` on Monday and `partial` on Friday must
        # read Friday, and the two-query form read Monday — which is the same
        # divergence, one layer out.
        last_ok = last_landing(conn, source=spec.name, season=run_season, through=day)
        last_any = last_run(conn, source=spec.name, season=run_season, status=None, through=day)
        applicable = phase in spec.phases or phase == PHASE_UNKNOWN
        if applicable and spec.applicable is not None:
            # Same principle as the phase gate, one notch finer: game_weather is
            # in-phase all preseason but has nothing to fetch until week 1 is
            # inside the forecast wall, and reporting NEVER PULLED on it for six
            # weeks is the wolf-cry this verdict exists to avoid.
            try:
                ctx = IngestContext(conn=conn, season=run_season,
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
        elif archived_season:
            # Checked AFTER "never": a completed season we hold nothing for is a
            # gap in history, which is a different (and still reportable) fact
            # from a completed season we do hold.
            verdict = VERDICT_ARCHIVED
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
            "season": run_season,
            "archived_season": archived_season,
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

    A COMPLETED season reads differently on purpose (item 3.2c, F-D). Before the
    fix, ``ingest status --season 2023`` said ``NEVER PULLED`` about sources that
    serve today's value and can never have 2023's, and ``NOT PUBLISHED UPSTREAM
    YET … Expected until ~Sept 10`` about a season that ended two and a half years
    ago. Neither sentence is true of a finished season, and both are the kind of
    standing nonsense that teaches an operator to stop reading the report.
    """
    day = normalize_as_of(today)
    phase = season_phase(conn, season=season, today=day)
    rows = source_freshness(conn, season=season, today=day)
    archived_season = season < nfl_season_of(day)

    lines = [
        f"nfl ingest status — season {season}, phase {phase} (as of {day.isoformat()})",
    ]
    if archived_season:
        lines.append(
            f"  season {season} is COMPLETE — upstream's files for it are finished "
            "artifacts, not a feed, so a source that landed reads 'archived' and never "
            "goes stale. Nothing below is a cadence failure."
        )
        if BACKFILL_ONLY_SOURCES:
            # Rule 7: say what this report does NOT cover, on the one command an
            # operator would use it to answer "what history do I hold for 2023?".
            lines.append(
                "  not listed (backfill-only, never in the cadence): "
                + ", ".join(s.name for s in BACKFILL_ONLY_SOURCES)
                + " — see the run log."
            )
    lines.append(
        f"  {'source':<14} {'verdict':<9} {'last ok':<12} {'age':>4}  {'rows':>7}  "
        f"{'last try':<14} notes"
    )
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
    if never and not archived_season:
        lines.append(f"  NEVER PULLED : {', '.join(never)}")
    elif never:
        # A completed season splits "we never backfilled it" from "it can never be
        # had", and the second is not a gap anyone can close. Grouping them under
        # one NEVER PULLED heading asks the operator to fix something impossible.
        gone = [r["source"] for r in rows
                if r["verdict"] == VERDICT_NEVER and r["perishable"]]
        # A THIRD bucket, and it exists because the report and the backfill were
        # contradicting each other on the live data (measured after the first real
        # 2021-2025 run): `players` is season-agnostic and `last_run` is
        # season-filtered, so it reads `never` for every past season — and the
        # report told the operator to backfill it while `ingest backfill` refuses
        # it by name with a measured reason. Sending an operator to a command that
        # will refuse them is exactly how a report earns being ignored.
        refused = [n for n in never if n not in gone and n in BACKFILL_EXCLUDED]
        missing = [n for n in never if n not in gone and n not in refused]
        if missing:
            lines.append(
                f"  NOT BACKFILLED: {', '.join(missing)} — season {season} is complete and "
                "these are whole-season files that can still be pulled for it. Absent from "
                "this database, not stale. `ziggurat ingest backfill` lands them."
            )
        if refused:
            lines.append(
                f"  NOT BACKFILLABLE: {', '.join(refused)} — deliberately excluded from "
                "`ingest backfill` (run it with --source <name> to read the recorded "
                "reason). Nothing to do here."
            )
        if gone:
            lines.append(
                f"  UNOBTAINABLE : {', '.join(gone)} — these serve the CURRENT value only, "
                f"so season {season}'s can no longer be captured by anything. Permanently "
                "absent, and not a gap to fix."
            )
    if awaiting and not archived_season:
        lines.append(f"  NOT PUBLISHED UPSTREAM YET: {', '.join(awaiting)} — attempted and "
                     "upstream has no data for this season yet. Expected until ~Sept 10.")
    elif awaiting:
        lines.append(f"  NOT PUBLISHED UPSTREAM: {', '.join(awaiting)} — attempted and "
                     f"upstream has no file for season {season} at all. That season is "
                     "over, so this will not resolve by waiting.")
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
        lines.append(
            f"  every applicable source is archived for season {season} — nothing to do."
            if archived_season else
            "  all applicable sources fresh or stale-but-recoverable."
        )

    # ORPHANS ARE REPORTED ACROSS EVERY SOURCE AND SEASON, unlike everything
    # above (item 3.2c audit, C7). The rows in this report are the CADENCE
    # sources for ONE season; the orphan that actually strands an operator is a
    # killed BACKFILL — a source this report never lists, for a season it was not
    # asked about — and it silently refuses every subsequent `ingest backfill`.
    # A remedy the operator cannot discover is not a remedy (Rule 6), so the
    # command is printed here, on the report CLAUDE.md tells them to check.
    # Only rows idle past the staleness bound appear, so a run in flight right
    # now is never called an orphan.
    orphans = orphan_runs(conn)
    if orphans:
        lines.append(
            f"  ORPHANED RUNS: {len(orphans)} — marked `running` for "
            f"{ORPHAN_STALE_MINUTES}+ minutes, i.e. the process that opened them was "
            "killed mid-pull. They also make `ziggurat ingest backfill` refuse to "
            "start. Clear with `ziggurat ingest reap` (`--dry-run` to look first); it "
            "rewrites the run log only and touches no data."
        )
        for row in orphans[:5]:
            lines.append(f"    run {row['run_id']:>5}  {row['source']:<14} "
                         f"season {row['season']}  started {row['started_at']}  "
                         f"batch {row['batch_id']}")
        if len(orphans) > 5:
            lines.append(f"    ... and {len(orphans) - 5} more")
    return "\n".join(lines)


def _source_flags(spec: SourceSpec) -> list[str]:
    flags = []
    if spec.perishable:
        flags.append("perishable")
    if spec.replaces_partition:
        flags.append("replaces-partition")
    if spec.needs_credentials:
        flags.append("needs-credentials")
    if spec.needs_schedules:
        flags.append("needs-schedules")
    if spec.season_resolver is not None:
        flags.append("season-resolver")
    if spec.quiet_ok:
        flags.append("quiet-ok")
    if spec.blocked:
        flags.append("BLOCKED")
    return flags


def _source_row(spec: SourceSpec) -> str:
    phases = "all" if spec.phases == ALL_PHASES else ",".join(sorted(spec.phases))
    return (f"  {spec.name:<20} {spec.group:<8} {phases:<28} "
            f"{str(spec.interval_days) + 'd':<6} {', '.join(_source_flags(spec))}")


def format_sources() -> str:
    """The registry as a table — what exists, when it runs, what missing it costs.

    Names the BACKFILL-ONLY sources too. They are not in the cadence and
    ``ingest status`` will never list them, so without this footer the only
    honest inventory of what ziggurat can ingest would omit them entirely — and
    an operator would reasonably conclude that the 2021-2024 depth charts have no
    ingester at all (item 3.2c, Rule 7).
    """
    lines = [f"  {'source':<20} {'group':<8} {'phases':<28} {'every':<6} flags"]
    lines.extend(_source_row(spec) for spec in SOURCES)
    if BACKFILL_ONLY_SOURCES:
        lines.append("")
        lines.append("  BACKFILL-ONLY (never run by the cadence; not shown by `ingest status`)")
        lines.extend(_source_row(spec) for spec in BACKFILL_ONLY_SOURCES)
    return "\n".join(lines)


# ============================================================================
# HISTORICAL BACKFILL (item 3.2c)
# ============================================================================
#
# WHY THIS IS A SEPARATE MECHANISM AND NOT A FLAG ON `ingest run`.
# Item 3.1b built a CURRENT-SEASON refresher. Item 1.4's "multi-season history
# (>=2021)" was satisfied by the ingester code and never by a populated database,
# so on 2026-07-25 fourteen tables held zero rows and `schedules` held 2026 only.
# Nothing backfills history on its own, and three shapes of "just do it" were
# measured and rejected during recon:
#
#   * A SHELL SCRIPT calling the ingesters directly leaves `nfl_ingest_runs`
#     blind. Reproduced by accident during recon: 15 tables populated and
#     `ingest status` reporting NEVER PULLED for all of them — the run log is the
#     ONLY freshness source this package trusts (MAX(retrieved_as_of) on a fact
#     table lies three measured ways), so data that arrives outside it is
#     invisible to every report the operator is told to read.
#   * A `--seasons` FLAG on `ingest run` puts five policy conditionals (season
#     bounds, an allowlist, an already-landed gate, a protected-partition
#     fingerprint, a running-row refusal) into the path a systemd timer takes
#     four times a day. Forking the daily decision path is the failure class
#     3.1b's audit spent a whole round on.
#   * REUSING `decide()`'s interval gate as the resume mechanism. The interval is
#     1 day for schedules/injuries/game_odds, so a backfill re-run two days later
#     would re-pull them and append a WHOLE SECOND `retrieved_as_of` partition
#     (~+48 MB). A completed season does not change; "already landed => never
#     re-pull without --force" is a backfill policy, not a scheduler policy.
#
# THE STANDING HAZARD THIS IS BUILT AGAINST. The draft is ~3 weeks out and the
# 2026 partition (the `players` crosswalk, `projections`, the `espn_draft_ranks`
# board, league state) is what the draft weapon runs on; `db/ziggurat.sqlite` is
# gitignored, so there is no other copy of it anywhere. Items 3.1 and 3.1b EACH
# shipped a "a degraded pull destroys the day" defect that only an audit caught.
# So the safety here is ENFORCED, not conventional: an allowlist that raises, a
# season bound that raises, a refusal to start while another run is in flight,
# and a CONTENT fingerprint of every draft-critical partition asserted before and
# after the whole operation.
#
# BACK-STAMPING IS NOT WHAT "BACKFILL" MEANS HERE, and the two unfortunately
# share a word. This path writes `retrieved_as_of = TODAY` on every row it lands
# — honest provenance for "we downloaded 2021's file in 2026" — while
# `knowable_as_of` stays the real historical fact time the shipped ingester
# derives. `--allow-backfill` (the flag) defeats `resolve_stamp` and writes
# TODAY's upstream data under a PAST `retrieved_as_of`, which manufactures a leak
# for every source. It is NEVER available on this path and `run_backfill` hard-
# codes `allow_backfill=False` rather than accepting it as an argument.
#
# THE CONSEQUENCE THE CALLER MUST KNOW (F8, measured on seven accessors): because
# every backfilled row carries `retrieved_as_of = today`, the default `historical`
# view — which gates RETRIEVAL time as well as knowledge time — returns NOTHING
# for any past `as_of`. Measured: weekly_stats 2023 at as_of=2023-10-15 reads 0
# under `historical` and 6,002 under `latest_truth`; snap_counts 2024 reads 0 vs
# 11,589; injuries 2023 0 vs 2,430; usage_deltas 2025 wk9 0 vs 83. That is
# `select_as_of` working exactly as designed, it must NOT be "fixed" by
# back-stamping, and the failure mode is an empty result that reads as "3.3 is
# broken" rather than "wrong view". EVERY historical read of backfilled data goes
# through `base.latest_truth(accessor)`.

#: Oldest season this may pull. 2019/2020 number the regular season 1-17 (POST is
#: 18-21) and 2019 still carries `OAK`, either of which silently breaks a
#: `week <= 18 => regular season` assumption or activates the legacy team-alias
#: path. 2021-2025 is homogeneous: measured byte-identical column lists across all
#: five seasons for schedules, weekly_stats, snap_counts, ngs_*, team_defense, and
#: the same canonical 32 team abbreviations in every season. Pre-2021 is a Phase-4
#: decision with its own measurements, not a number to relax here.
BACKFILL_MIN_SEASON = 2021

#: Every backfill batch_id starts with this. Two jobs, both load-bearing:
#: (1) the run log says at a glance which rows came from a history replay rather
#: than from the cadence, and (2) the active-cadence fence below can tell "the
#: installed timer is writing this season right now" from "this backfill already
#: ran" — without which the fence would refuse the backfill's own re-run and
#: destroy its resumability.
BACKFILL_BATCH_PREFIX = "backfill-"

#: How far back the active-cadence fence looks. See ``backfill_seasons``.
BACKFILL_ACTIVE_CADENCE_DAYS = 30

#: Registry sources a historical backfill pulls, in registry (= dependency) order.
#: `schedules` leads because six of the others stamp `knowable_as_of` from its
#: gameday map and drop 100% of their rows without it (measured: 19,421/19,421).
BACKFILL_SOURCES: tuple[str, ...] = (
    "schedules",
    "weekly_stats",
    "snap_counts",
    "team_defense",
    "ngs_passing",
    "ngs_rushing",
    "ngs_receiving",
    "injuries",
    "game_odds",
    "depth_charts",
)

#: name -> the MEASURED reason it is refused, quoted verbatim when the refusal
#: fires. Refusing WITH the reason is the whole point: a future builder must not
#: be able to add one of these back without reading why it is here.
#:
#: READ BY TWO GATES, not one (item 3.2c audit, C8). ``select_backfill_sources``
#: refuses it on ``ingest backfill``; ``decide()`` refuses it on ``ingest run``
#: for any season before the current one. The second gate was missing, and the
#: hazard this list exists to prevent was fully reachable through it — measured:
#: ``ingest run --source projections --season 2023`` wrote 57,910 rows stamped
#: ``knowable_as_of`` = today into the 2023 partition and logged ``ok``.
BACKFILL_EXCLUDED: dict[str, str] = {
    "players": (
        "the id crosswalk is season-agnostic and a re-pull adds nothing historical: "
        "measured 100.0/100.0/99.8/99.8/100.0% crosswalk coverage of fantasy-position "
        "weekly_stats ids for 2021-2025, and 0.00% of PPR points behind a missing id in "
        "any season. What it DOES carry is risk — players.CrosswalkCollapse, a "
        "pfr->gsis collision flip baked permanently into whatever snap_counts stores "
        "next, and a refreshed row that silences item 3.2's staleness banner. The daily "
        "cadence already pulls it."
    ),
    "adp_rankings": (
        "_pull_adp ignores `season` entirely, so a five-season backfill would run five "
        "IDENTICAL FantasyPros scrapes of TODAY's board and log them under five "
        "different seasons — five lies in the run log and zero historical rows. The "
        "historical ECR panel is a separate Phase-4 ingester (item 4.1)."
    ),
    "espn_ranks": (
        # NOTE: this reason deliberately does NOT quote the SQL verbatim.
        # `tests/test_nfl_refresh.py::test_no_nfl_ingester_outside_espn_ranks_deletes_rows`
        # scans every module in this package for the delete keyword and is meant to
        # be blunt — weakening a safety guard so a prose string can quote SQL is
        # exactly the wrong trade.
        "the ONLY delete-then-write path in ziggurat/data/nfl/ — it removes the stored "
        "espn_draft_ranks partition for (season, retrieved_as_of) before rewriting it — "
        "and it is the board the draft cockpit reads, three weeks before the draft. It "
        "is also preseason-only and ESPN serves no historical board, so a past season "
        "could return nothing to put back."
    ),
    "projections": (
        "historical Sleeper projections need bulk_historical=True, which a SourceSpec's "
        "`pull` callable cannot express. A naive run would write ~59k rows per season "
        "stamped knowable_as_of = TODAY, which is invisible under BOTH views and would "
        "sit in the table looking like history. Lands in item 4.1, latest_truth-only by "
        "construction."
    ),
    "ff_opportunity": (
        "out of scope for 3.2c and never registered: ffverse expected-TD data is a MODEL "
        "OUTPUT published months after each season ends, by a model trained on the season "
        "it scores (ep_weekly_2021.parquet written 2023-01-05; 2025's written 2026-02-10). "
        "Stamping a 2021 week-5 row knowable_as_of = 2021-10-10 passes every leakage test "
        "while contaminating a Phase-4 backtest with the outcome distribution of the "
        "season it is grading. Lands in Phase 4, latest_truth-only, stamped from the "
        "ASSET's updated_at."
    ),
}

#: Backfill-only sources that are NOT pulled unless explicitly asked for. Not a
#: correctness fence — a cost one, and the operator's decision (§4.3 of the recon
#: note): the ERA5 weather archive is 12.0 s per (season, week), ~18 minutes for
#: five seasons, TWELVE TIMES everything else in the backfill combined, and item
#: 3.3 — the reason this backfill exists — reads none of it. The flag exists so
#: item 4.2 can pull it without new code.
BACKFILL_OPTIONAL: frozenset[str] = frozenset({"game_weather_archive"})


class BackfillRefused(ValueError):
    """A backfill was asked to do something its fences forbid.

    A ValueError so the CLI's existing `except ValueError` arm reports it as a
    refused selection (exit 2) rather than a traceback.
    """


class BackfillTouchedProtectedSeason(RuntimeError):
    """A draft-critical partition's CONTENT changed across the backfill.

    Carries ``summaries`` (what the run actually did) so the caller can still
    print the run report next to the alarm — the operator needs both to tell a
    concurrent cadence run from real damage.
    """

    def __init__(self, message: str, *, changed: dict, summaries: list):
        super().__init__(message)
        self.changed = changed
        self.summaries = summaries


def _pull_game_weather_archive(ctx) -> int:
    """ERA5 archive actuals for every REG week of a COMPLETED season.

    A different pull from the registry's `game_weather`, not the same one aimed
    at a past year, and the distinction is not cosmetic:

    * The registry spec fetches `mode="forecast"` for the weeks inside the ~10-day
      Open-Meteo FORECAST WALL. For any completed season `weather_weeks` returns
      `[]` (every gameday is in the past), so routing `--with-weather` through the
      registry spec would have been a flag that silently did nothing at all.
    * The two modes are different observations of different things and the table
      knows it: `game_weather`'s primary key carries `forecast_source`, so an
      archive row can never overwrite a stored forecast (verified in the DDL).
      A forecast is knowable on its pull day; an ERA5 actual is knowable on the
      gameday.
    """
    weeks = season_weeks(ctx.conn, season=ctx.season)
    total = 0
    for week in weeks:
        try:
            total += weather.pull_game_weather(
                ctx.conn, ctx.season, week, retrieved_as_of=ctx.retrieved_as_of,
                mode="archive",
            )
        except Exception as exc:
            # One commit per week, same as the forecast loop: report what landed.
            raise PartialPull(
                f"game_weather_archive failed on week {week} after storing {total} rows "
                f"for weeks {weeks[:weeks.index(week)]}: {type(exc).__name__}: {exc}",
                rows_written=total, cause=exc,
            ) from exc
    return total


_GAME_WEATHER_ARCHIVE_SPEC = SourceSpec(
    name="game_weather_archive", group=GROUP_GAMEDAY, pull=_pull_game_weather_archive,
    scope=lambda ctx: f"archive, all REG weeks of {ctx.season}",
    phases=ALL_PHASES, interval_days=365, perishable=False, needs_schedules=True,
    notes="OPT-IN (--with-weather), OFF by default. ERA5 archive actuals for a completed "
          "season, stored under forecast_source='archive_actual' so they cannot collide "
          "with a stored forecast. ~12 s per (season, week) => ~18 min for 2021-2025, "
          "twelve times the rest of the backfill combined, and item 3.3 reads none of "
          "it — this exists so item 4.2 needs no new code.",
)

# Registered alongside the legacy depth-chart regime: both are replayed into
# history and have nothing to say today, so neither belongs in SOURCES (where
# `ingest run` would attempt them daily and `ingest status` would report them
# stale forever — a standing alarm on something working perfectly).
BACKFILL_ONLY_SOURCES = BACKFILL_ONLY_SOURCES + (_GAME_WEATHER_ARCHIVE_SPEC,)
BACKFILL_ONLY_BY_NAME = {spec.name: spec for spec in BACKFILL_ONLY_SOURCES}

#: The order a backfill runs sources in, and the only place that order is stated.
#: Registry order first (it IS dependency order, and `schedules` must land before
#: the six sources that stamp against its gameday map), then the backfill-only
#: specs — both of which also need `schedules`, so trailing them is safe by
#: construction rather than by luck.
_BACKFILL_ORDER: tuple[str, ...] = BACKFILL_SOURCES + tuple(
    s.name for s in BACKFILL_ONLY_SOURCES
)

#: source -> the table its rows land in, for the coverage report. Kept here
#: rather than derived, because "which table did this source write" is exactly
#: the question a coverage report exists to answer and a wrong guess would report
#: a healthy source as an empty one.
_BACKFILL_TABLES: dict[str, str] = {
    "schedules": "schedules",
    "weekly_stats": "weekly_stats",
    "snap_counts": "snap_counts",
    "team_defense": "team_defense",
    "ngs_passing": "ngs_passing",
    "ngs_rushing": "ngs_rushing",
    "ngs_receiving": "ngs_receiving",
    "injuries": "injuries",
    "game_odds": "game_odds",
    "depth_charts": "depth_chart_slots",
    "depth_charts_weekly": "depth_charts_weekly",
    "game_weather_archive": "game_weather",
}


def backfill_spec(name: str) -> SourceSpec:
    """Resolve one backfillable source name to its spec, or raise with the reason.

    The single place a name becomes a spec on this path, so the allowlist cannot
    be bypassed by a caller that resolves specs itself.
    """
    if name in BACKFILL_EXCLUDED:
        raise BackfillRefused(
            f"refusing to backfill {name!r}: {BACKFILL_EXCLUDED[name]}"
        )
    if name in BACKFILL_ONLY_BY_NAME:
        return BACKFILL_ONLY_BY_NAME[name]
    if name in BACKFILL_SOURCES:
        return SOURCES_BY_NAME[name]
    if name in SOURCES_BY_NAME:
        raise BackfillRefused(
            f"{name!r} is a registered cadence source but is not on the backfill "
            f"allowlist. Backfillable: {sorted(_BACKFILL_ORDER)}. If it belongs there, "
            "say why in BACKFILL_SOURCES rather than passing it through."
        )
    raise BackfillRefused(
        f"unknown source {name!r}; backfillable: {sorted(_BACKFILL_ORDER)}"
    )


def select_backfill_sources(*, names: Sequence[str] | None = None,
                            with_weather: bool = False) -> tuple[SourceSpec, ...]:
    """Resolve the backfill source set IN DEPENDENCY ORDER.

    Raises ``BackfillRefused`` for any name in ``BACKFILL_EXCLUDED``, quoting the
    recorded reason, and for any unknown name. ``run_backfill`` re-asserts this on
    entry so a caller that builds its own spec list cannot bypass the CLI.

    ``with_weather`` adds the opt-in ERA5 archive pull. An explicit
    ``names=["game_weather_archive"]`` also works without the flag — asking for it
    by name IS the opt-in.
    """
    if names is None:
        wanted = [n for n in _BACKFILL_ORDER
                  if n not in BACKFILL_OPTIONAL or with_weather]
    else:
        requested = set(names)
        unknown = [n for n in names if n not in _BACKFILL_ORDER]
        for n in unknown:                      # raises with the recorded reason
            backfill_spec(n)
        wanted = [n for n in _BACKFILL_ORDER if n in requested]
    return tuple(backfill_spec(n) for n in wanted)


def backfill_seasons(conn, *, first: int, last: int, today) -> tuple[int, ...]:
    """Validate and expand ``[first, last]`` into the seasons a backfill may touch.

    ``(first, last)`` is an explicit inclusive RANGE and never a list, because the
    first design took ``seasons=[...]`` and immediately did
    ``backfill_seasons(first=min(seasons), last=max(seasons))`` — so
    ``seasons=[2021, 2025]`` silently expanded to five seasons of network traffic.

    Four refusals, each bound to something measured rather than to a convention:

    1. ``first < BACKFILL_MIN_SEASON`` — pre-2021 week numbering shifts and `OAK`
       reappears (see the constant).
    2. ``last >= nfl_season_of(today)`` — the current season belongs to the 3.1b
       cadence. This is what makes every fence below structural rather than
       behavioural: no code path in the backfill can pass the current season to
       any pull, so the one delete-then-write source in the package cannot be
       reached with a season whose partition exists.
    3. **The active-cadence fence**, and it is not a duplicate of (2). Rule (2) is
       bound to a DATE FUNCTION; the installed systemd units PIN ``--season`` at
       install time (their own comments say "The season is PINNED rather than
       derived"). So from 2027-03-01 ``nfl_season_of`` starts calling season 2026
       backfillable while the installed timers are still writing it four times a
       day. This predicate is bound to WHAT IS ACTUALLY BEING WRITTEN: any
       ``nfl_ingest_runs`` row for a requested season in the last
       ``BACKFILL_ACTIVE_CADENCE_DAYS`` days that did NOT come from a backfill.
       Backfill batches are excluded by their ``batch_id`` prefix — without that
       exclusion this fence would refuse the backfill's own resume and destroy the
       idempotence the whole failure story rests on. ``_NON_WRITING_STATUSES`` are
       excluded too (item 3.2c audit, C7-1c): the predicate is "something is
       actively WRITING this season", and a `fresh`/`skipped`/`blocked`/
       `upstream_absent` row is the log of a source that touched nothing. Before
       that exclusion, ONE diagnostic ``ingest run --source weekly_stats --season
       2023`` — the command an earlier refusal message actually told the operator
       to run — stranded that season behind this fence for thirty days, with
       ``--force`` explicitly not overriding it.
    4. ``first > last`` — a typo that would otherwise silently do nothing.

    NOT OVERRIDABLE BY ``--force``. ``force`` on this path means "re-pull a
    (source, season) pair that already landed"; letting it also unlock a season
    the cadence is writing would put the protection behind the one flag an
    operator reaches for on a re-run. The refusal names the exact run-log rows it
    saw, so a false positive (e.g. someone ran ``ingest run --season 2023`` by
    hand last week) is diagnosable in one read rather than mysterious.
    """
    day = normalize_as_of(today)
    if first > last:
        raise BackfillRefused(
            f"refusing an empty season range: first={first} is after last={last}"
        )
    if first < BACKFILL_MIN_SEASON:
        raise BackfillRefused(
            f"refusing to backfill season {first}: {BACKFILL_MIN_SEASON} is the oldest "
            "supported season. 2019/2020 number the REGULAR season 1-17 (POST is 18-21) "
            "and 2019 still carries the OAK abbreviation, so either would silently break "
            "a 'week <= 18 means regular season' assumption or activate the legacy "
            "team-alias path. Pre-2021 is a Phase-4 decision with its own measurements."
        )
    current = nfl_season_of(day)
    if last >= current:
        raise BackfillRefused(
            f"refusing to backfill season {last}: season {current} is the CURRENT season "
            f"as of {day.isoformat()} and the 3.1b cadence owns it. A history backfill "
            "must not be able to reach a partition the draft weapon reads — that is what "
            f"makes the safety structural. Ask for {current - 1} or earlier."
        )
    cutoff = (day - timedelta(days=BACKFILL_ACTIVE_CADENCE_DAYS)).isoformat()
    quiet = {f"quiet_{i}": s for i, s in enumerate(_NON_WRITING_STATUSES)}
    active = conn.execute(
        "SELECT source, season, status, retrieved_as_of, batch_id FROM nfl_ingest_runs "
        "WHERE season BETWEEN :first AND :last AND retrieved_as_of >= :cutoff "
        "AND retrieved_as_of <= :day AND batch_id NOT LIKE :prefix "
        f"AND status NOT IN ({', '.join(':' + k for k in quiet)}) "  # noqa: S608
        "ORDER BY run_id DESC LIMIT 5",
        {"first": first, "last": last, "cutoff": cutoff, "day": day.isoformat(),
         "prefix": BACKFILL_BATCH_PREFIX + "%", **quiet},
    ).fetchall()
    if active:
        seen = "; ".join(
            f"{r['source']} season {r['season']} ({r['status']}, stamped "
            f"{r['retrieved_as_of']}, batch {r['batch_id']})" for r in active
        )
        raise BackfillRefused(
            f"refusing to backfill {first}-{last}: the ordinary ingest cadence has "
            f"written run-log rows for a season in that range within the last "
            f"{BACKFILL_ACTIVE_CADENCE_DAYS} days, so something is actively pulling it. "
            f"Saw: {seen}. The installed systemd units PIN their --season at install "
            "time, so a calendar check alone cannot see this. --force does NOT override "
            "it. Either narrow the range, or find out what is writing that season."
        )
    return tuple(range(first, last + 1))


# ------------------------------------------------------- protected partitions

#: (label, sql, needs_protect_season) for every partition a backfill must leave
#: byte-identical. Ordered rows are NOT hashed in table order — see
#: ``_content_fingerprint``.
_PROTECTED_SQL: tuple[tuple[str, str, bool], ...] = (
    ("espn_draft_ranks", "SELECT * FROM espn_draft_ranks", False),
    ("projections", "SELECT * FROM projections WHERE season = :season", True),
    ("schedules", "SELECT * FROM schedules WHERE season = :season", True),
    ("adp_rankings", "SELECT * FROM adp_rankings", False),
    ("players", "SELECT * FROM players", False),
)

#: The crosswalk OUTPUTS, which is where the measured 3.1b damage actually
#: manifested — the rows stayed physically present and every crosswalk went to
#: zero. Sized, not hashed: three of these four resolvers still keep-first with
#: an undefined scan order (recorded, not fixed, by the base.py half of 3.2c), so
#: hashing their contents could cry wolf on a re-scan that changed nothing.
_PROTECTED_CROSSWALKS: tuple[tuple[str, Callable], ...] = (
    ("crosswalk:espn_by_gsis", base.espn_by_gsis),
    ("crosswalk:gsis_by_pfr", base.gsis_by_pfr),
    ("crosswalk:ids_by_fantasypros", base.ids_by_fantasypros),
    ("crosswalk:gsis_by_espn", base.gsis_by_espn),
)


def _content_fingerprint(conn, sql: str, params: dict) -> str:
    """An order-independent CONTENT hash of a query's rows.

    CONTENT, not cardinality, and that correction is the whole reason this
    function exists. The first design fingerprinted ``COUNT(*), MAX(retrieved_as_of)``.
    Every non-delete ingester in this package writes ``INSERT OR REPLACE`` on a
    key that contains ``retrieved_as_of``, so an in-place overwrite at the SAME
    (key, stamp) leaves both of those numbers identical while replacing every
    value in the row — which is verbatim the 3.1b headline finding quoted in this
    module's docstring (a ``players`` pull served with empty id columns took every
    crosswalk to zero with the good rows still physically present underneath, and
    the run logged ``ok``). The count-based fence would have reported "identical".

    Order-independent by construction (hash each row, sort the digests) so no
    ``ORDER BY`` is needed on a 173,712-row table and the answer cannot drift with
    SQLite's scan order. Duplicate-sensitive, because the digest list is sorted
    rather than XOR-folded: two identical rows do not cancel.
    """
    digests = [
        hashlib.blake2b(repr(tuple(row)).encode("utf-8"), digest_size=16).digest()
        for row in conn.execute(sql, params)
    ]
    digests.sort()
    acc = hashlib.blake2b(digest_size=16)
    acc.update(str(len(digests)).encode("ascii"))
    for digest in digests:
        acc.update(digest)
    return f"{len(digests)}:{acc.hexdigest()}"


def protected_partitions(conn, *, protect_season: int) -> dict[str, str]:
    """Fingerprint every partition a historical backfill must not change.

    Captured before the loop and re-asserted after it. What is covered and why:

    * ``espn_draft_ranks`` (all seasons) — the board the draft cockpit reads, and
      the only delete-then-write table in this package.
    * ``projections`` / ``schedules`` for ``protect_season`` — the 2026 partition
      the valuation and marginal boards price from.
    * ``adp_rankings`` and ``players`` — whole tables; both are season-agnostic,
      so "the backfill only touches old seasons" is not a defence for them.
    * the four id crosswalk OUTPUTS — sized, because that is the form the measured
      3.1b damage took (every crosswalk to zero with the rows still present).

    A missing table fingerprints as ``"<absent>"`` rather than raising: this must
    be callable on a partially-migrated or synthetic database, and a fence that
    crashes is a fence that gets removed.
    """
    out: dict[str, str] = {}
    for label, sql, needs_season in _PROTECTED_SQL:
        params = {"season": protect_season} if needs_season else {}
        try:
            out[label] = _content_fingerprint(conn, sql, params)
        except Exception as exc:
            out[label] = f"<absent: {type(exc).__name__}>"
    for label, resolver in _PROTECTED_CROSSWALKS:
        try:
            out[label] = f"{len(resolver(conn))} entries"
        except Exception as exc:
            out[label] = f"<absent: {type(exc).__name__}>"
    return out


def _foreign_runs_since(conn, *, batch_id: str, started_at: str) -> list:
    """Run-log rows from a DIFFERENT batch that started during this operation.

    Not decoration: a legitimate concurrent ``ingest run`` from the daily timer
    writes 2026 ``projections`` mid-backfill, which changes a protected
    fingerprint through no fault of the backfill. Naming those rows in the alarm
    turns "the draft data changed" from a five-alarm mystery into a one-line
    diagnosis.
    """
    return conn.execute(
        "SELECT source, season, status, started_at, batch_id FROM nfl_ingest_runs "
        "WHERE batch_id <> ? AND started_at >= ? ORDER BY run_id",
        (batch_id, started_at),
    ).fetchall()


# --------------------------------------------------------------- the backfill


@dataclass(frozen=True)
class BackfillPlan:
    """The ``--dry-run`` answer: what a backfill would do, with no network."""

    seasons: tuple[int, ...]
    sources: tuple[str, ...]
    #: (season, Decision) in execution order — seasons ascending, sources in
    #: dependency order within a season.
    decisions: tuple[tuple[int, Decision], ...]
    protect_season: int
    with_weather: bool
    force: bool

    @property
    def pulls(self) -> int:
        return sum(1 for _, d in self.decisions if d.action == "pull")


def _landed(conn, spec: SourceSpec, *, season: int, today):
    """The run-log row proving this (source, season) pair already landed rows.

    ``last_landing`` — so ``ok`` AND ``partial`` count. ``weekly_stats`` drops the
    same 22 null-``player_id`` rows out of every season file and the three
    ``ngs_*`` drop the week-23 Super Bowl rows, so those four are ``partial`` on
    every correct run; anchoring on ``ok`` alone would re-pull them for ever.
    ``failed`` / ``empty`` / ``upstream_absent`` / ``skipped`` deliberately do not
    count, which is what makes a mid-loop failure resumable by simply re-running.
    """
    run_season = resolve_source_season(conn, spec, season=season, today=today)
    return last_landing(conn, source=spec.name, season=run_season)


def plan_backfill(conn, *, first: int, last: int, sources: Sequence[SourceSpec],
                  today, force: bool = False) -> BackfillPlan:
    """What ``run_backfill`` would do. Touches no network and writes nothing.

    Applies every fence ``run_backfill`` applies except the running-row refusal
    and the protected-partition fingerprint (both of which are about the moment of
    execution), so a refused range is reported by the dry run instead of being
    discovered by the real command.

    THE BOOTSTRAP PREVIEW, and why it needs code at all. ``decide()`` is computed
    against the database AS IT IS NOW, so on a season with no schedules it reports
    ten SKIPPED for a run that will pull ten — ``schedules`` lands INSIDE the same
    ``run_ingest`` call and ``decide`` re-derives the phase per source at the
    moment that source is reached. That is a PREVIEW problem, not an execution
    one, and it is fixed here rather than by a second network pass: when the
    season has no schedule rows and ``schedules`` is itself in this run, every
    source that is skipped *because of that* is reported as "will pull once
    schedules lands in this run".

    The rewrite is sound only because ``backfill_seasons`` has already refused any
    season >= the current one, so every planned season is necessarily COMPLETE and
    therefore necessarily ``offseason`` once its schedule is present. A source
    whose phases exclude ``offseason`` is therefore NOT rewritten — it would still
    be skipped after schedules land, and predicting "pull" would be a lie.
    """
    seasons = backfill_seasons(conn, first=first, last=last, today=today)
    names = {spec.name for spec in sources}
    for name in names:                                 # re-assert the allowlist
        backfill_spec(name)
    decisions: list[tuple[int, Decision]] = []
    for season in seasons:
        bootstrap = not season_weeks(conn, season=season)
        schedules_pending = (
            "schedules" in names
            and (force or _landed(conn, SOURCES_BY_NAME["schedules"],
                                  season=season, today=today) is None)
        )
        for spec in sources:
            if not force:
                landing = _landed(conn, spec, season=season, today=today)
                if landing is not None:
                    decisions.append((season, Decision(
                        spec.name, STATUS_FRESH,
                        f"already landed {landing['rows_written']} rows for season "
                        f"{season} on {landing['retrieved_as_of']} (status "
                        f"{landing['status']}) — a completed season does not change, so "
                        "this is skipped rather than re-pulled. --force re-pulls it "
                        "under today's stamp.",
                        season=season,
                    )))
                    continue
            # force=True into decide(): the interval gate is the SCHEDULER's
            # policy and would re-pull schedules/injuries/game_odds on any re-run
            # two days later, appending a whole second retrieved_as_of partition.
            # The backfill's gate is `_landed`, above.
            decision = decide(conn, spec, season=season, today=today,
                              have_credentials=False, force=True)
            if (bootstrap and schedules_pending and spec.name != "schedules"
                    and decision.action == STATUS_SKIPPED
                    and (spec.needs_schedules or spec.phases != ALL_PHASES)
                    and PHASE_OFFSEASON in spec.phases):
                decision = Decision(
                    spec.name, "pull",
                    "due (unlocks once schedules lands in this same run — season "
                    f"{season} is complete, so its phase resolves to offseason)",
                    decision.scope, season=season,
                )
            decisions.append((season, decision))
    return BackfillPlan(
        seasons=seasons,
        sources=tuple(spec.name for spec in sources),
        decisions=tuple(decisions),
        protect_season=nfl_season_of(normalize_as_of(today)),
        with_weather=any(s.name in BACKFILL_OPTIONAL for s in sources),
        force=force,
    )


def run_backfill(conn, *, first: int, last: int, sources: Sequence[SourceSpec],
                 retrieved_as_of, today, force: bool = False,
                 batch_id: str | None = None, progress=None) -> list[dict]:
    """Land historical seasons. One run-log row per (source, season) pair, one
    ``batch_id`` for the WHOLE operation.

    ORDER: seasons ASCENDING, and within a season the dependency order
    ``_BACKFILL_ORDER`` (``schedules`` first). One ``run_ingest`` call per season
    suffices — ``decide()`` re-derives the phase per source at the moment that
    source is reached, so ``schedules`` landing inside the call unlocks the six
    phase-gated sources behind it in the SAME pass. Measured before the fix:
    ``ingest run --season 2025 --dry-run`` reported every phase-gated source as
    "season 2025 phase unknown — schedules not ingested yet".

    NEVER BACK-STAMPS: ``allow_backfill`` is hard-coded ``False`` rather than
    accepted as a parameter. "Backfill" here means "download old seasons TODAY";
    the flag of the same name means "write today's data under a PAST
    ``retrieved_as_of``", which manufactures a leak for every source. They are
    different operations that unfortunately share a word.

    FAILURE MID-LOOP is a designed state, not an accident:

    ===========================  ==========================  =========================
    failure                      what is in the DB           re-running
    ===========================  ==========================  =========================
    one source raises            its rows rolled back to     that pair has no
                                 zero by ``run_ingest``'s    ok/partial row, so it
                                 partial-commit fence        is re-pulled
    ``schedules`` fails for S    nothing for S               S is fully re-attempted
    season 3 of 5 dies           1-2 complete, 3 partial     only non-landed pairs
                                 by source, 4-5 STILL RUN    re-pull
    process killed mid-pull      a durable ``running`` row   THIS function reaps its
                                 survives                    own stale orphans (same
                                                             pairs, ``backfill-``
                                                             batches only), then
                                                             re-pulls; anything else
                                                             needs ``ingest reap``
    ===========================  ==========================  =========================

    THE ORPHAN, PRECISELY (item 3.2c audit, C7). The claim in the old version of
    this table — "the next run reaps it" — was false for the case that actually
    happens. ``start_run``'s reap is scoped to (source, season), so it only fires
    when that exact pair runs again, and the refusal below rejects the very
    command that would run it. For ``game_weather_archive`` and
    ``depth_charts_weekly`` — backfill-only, last in the order, slowest, i.e. the
    two most Ctrl-C-able pulls in the repo — NO shipped command could reach the
    pair, so recovery meant hand-editing SQLite. Now: this function clears its own
    stale orphans for the pairs it is about to open, and ``ziggurat ingest reap``
    clears anything else. The refusal is kept (a concurrent cadence run makes the
    protected-partition fingerprint unreadable) and now names that command.

    IDEMPOTENCE. A second call the same day touches no network at all (``_landed``
    skips every landed pair and records ``fresh``). A second call a DIFFERENT day
    would otherwise append a whole second ``retrieved_as_of`` partition — roughly
    +48 MB — which is exactly why the gate is "already landed", not the
    scheduler's interval. ``--force`` is the deliberate override.

    ``progress`` is an optional ``(str) -> None`` sink; a 60-90 s network run that
    prints nothing is indistinguishable from a hang.
    """
    stamp, day = resolve_stamp(retrieved_as_of, today, allow_backfill=False)
    seasons = backfill_seasons(conn, first=first, last=last, today=day)
    sources = tuple(sources)
    for spec in sources:                               # (a) the allowlist raises
        backfill_spec(spec.name)

    def say(line: str) -> None:
        if progress is not None:
            progress(line)

    # SELF-HEAL FIRST (item 3.2c audit, C7). A backfill killed mid-pull leaves a
    # `running` row that the refusal below then bounces off forever, and for the
    # two backfill-only sources no shipped command could clear it. Reaping is
    # scoped to exactly the (source, season) pairs THIS run is about to open —
    # `start_run` would flip those same rows to `abandoned` moments later anyway,
    # so this adds no new authority — and to batches this operation itself wrote
    # (`backfill-` prefix), so a concurrent CADENCE row is never touched. A live
    # backfill's own rows are protected by the staleness bound, not by luck.
    reaped = reap_orphan_runs(conn, older_than_minutes=ORPHAN_STALE_MINUTES,
                              sources=[spec.name for spec in sources], seasons=seasons,
                              batch_prefix=BACKFILL_BATCH_PREFIX)
    if reaped:
        say(f"reaped {len(reaped)} orphaned backfill run(s) from a killed process: "
            + "; ".join(f"{r['source']} season {r['season']} (started {r['started_at']})"
                        for r in reaped))

    # Refuse to start while ANY run is in flight. Two reasons, and the second is
    # the one that bites: a concurrent `ingest run` would (1) have its in-flight
    # row reaped or reap ours if the season predicate ever regressed, and (2)
    # legitimately write the 2026 partition mid-operation, which the
    # protected-partition fingerprint would then report as damage.
    running = conn.execute(
        "SELECT source, season, started_at, batch_id FROM nfl_ingest_runs "
        "WHERE status = ? ORDER BY run_id", (STATUS_RUNNING,),
    ).fetchall()
    if running:
        seen = "; ".join(f"{r['source']} season {r['season']} (started {r['started_at']}, "
                         f"batch {r['batch_id']})" for r in running)
        raise BackfillRefused(
            f"refusing to start: {len(running)} ingest run(s) are still marked running — "
            f"{seen}. Either one is genuinely in flight (wait for it; a backfill "
            "concurrent with the cadence makes the protected-partition check "
            "unreadable), or it is an orphan from a killed process. TO CLEAR ORPHANS: "
            "`ziggurat ingest reap --dry-run` to look, then `ziggurat ingest reap` to "
            f"clear anything idle for {ORPHAN_STALE_MINUTES}+ minutes (add "
            "`--older-than-minutes 0` for a row you know is dead). Reaping only "
            "rewrites the run LOG — no fact table is touched, and a run that turns out "
            "to be alive corrects its own row when it finishes."
        )

    batch_id = batch_id or uuid.uuid4().hex[:12]
    if not batch_id.startswith(BACKFILL_BATCH_PREFIX):
        # Forced, not merely defaulted: the active-cadence fence tells a backfill's
        # rows from the cadence's by this prefix, and a caller-supplied plain id
        # would make the backfill's own re-run look like an active cadence and
        # refuse itself.
        batch_id = BACKFILL_BATCH_PREFIX + batch_id
    started_at = _utc_now()
    protect_season = nfl_season_of(normalize_as_of(day))
    before = protected_partitions(conn, protect_season=protect_season)

    say(f"backfill {batch_id}: seasons {seasons[0]}-{seasons[-1]}, "
        f"{len(sources)} sources, stamped {stamp} "
        f"(protecting season {protect_season})")

    summaries: list[dict] = []
    for season in seasons:
        say(f"-- season {season}")
        to_pull: list[SourceSpec] = []
        for spec in sources:
            landing = None if force else _landed(conn, spec, season=season, today=day)
            if landing is None:
                to_pull.append(spec)
                continue
            reason = (f"already landed {landing['rows_written']} rows on "
                      f"{landing['retrieved_as_of']} (status {landing['status']}); a "
                      "completed season does not change, so this is not re-pulled "
                      "(--force overrides)")
            run_id = record_run(conn, batch_id=batch_id, source=spec.name, season=season,
                                scope=None, retrieved_as_of=stamp, at=_utc_now(),
                                status=STATUS_FRESH, reason=reason)
            summaries.append({"source": spec.name, "season": season,
                              "status": STATUS_FRESH, "reason": reason, "rows": 0,
                              "run_id": run_id, "scope": "", "batch_id": batch_id})
            say(f"[{STATUS_FRESH:>15}] {spec.name:<22} {season}")
        if to_pull:
            season_summaries = run_ingest(
                conn, sources=to_pull, season=season, retrieved_as_of=stamp, today=day,
                credentials=None, allow_shrink=False, allow_backfill=False,
                force=True, batch_id=batch_id,
            )
            for summary in season_summaries:
                summaries.append(dict(summary, season=season))
                say(f"[{summary['status']:>15}] {summary['source']:<22} {season}  "
                    f"rows={summary.get('rows', 0)}")

    after = protected_partitions(conn, protect_season=protect_season)
    changed = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    if changed:
        detail = "; ".join(f"{k}: {b!r} -> {a!r}" for k, (b, a) in sorted(changed.items()))
        foreign = _foreign_runs_since(conn, batch_id=batch_id, started_at=started_at)
        note = ""
        if foreign:
            note = (" ANOTHER INGEST RAN DURING THIS BACKFILL, which is the most likely "
                    "innocent explanation — "
                    + "; ".join(f"{r['source']} season {r['season']} ({r['status']}, "
                                f"batch {r['batch_id']})" for r in foreign[:5]) + ".")
        raise BackfillTouchedProtectedSeason(
            f"a draft-critical partition CHANGED across this backfill: {detail}. The "
            "backfill's own rows are committed and the run log is complete — this is a "
            "report, not a rollback (nothing here can undo another process's writes). "
            f"Check `db/ziggurat.sqlite.pre-3.2c` against the live file.{note}",
            changed=changed, summaries=summaries,
        )
    say(f"backfill {batch_id}: done, {len(summaries)} (source, season) pairs; "
        f"draft-critical partitions unchanged")
    return summaries


def backfill_coverage(conn, *, first: int, last: int,
                      sources: Sequence[SourceSpec] | None = None) -> list[dict]:
    """Per (source, season): stored rows, the knowable_as_of span, and run status.

    The answer to "what history do I actually hold", which ``ingest status``
    structurally cannot give: that report is per-source for ONE season and does
    not list backfill-only sources at all.

    Reads the fact tables directly and UNGATED — an operational question ("what is
    in the database"), never a decision one, exactly like ``season_bounds``. Rule
    1 governs decision reads; a coverage report that applied an as-of gate would
    report every backfilled row as absent, which is the very confusion F8 is about.
    """
    specs = tuple(sources) if sources is not None else select_backfill_sources()
    out = []
    for spec in specs:
        table = _BACKFILL_TABLES.get(spec.name)
        for season in range(first, last + 1):
            row = {"source": spec.name, "season": season, "table": table,
                   "rows": None, "first_knowable": None, "last_knowable": None,
                   "stamps": None, "status": None, "retrieved_as_of": None}
            if table is not None:
                try:
                    got = conn.execute(
                        f"SELECT COUNT(*) AS n, MIN(knowable_as_of) AS lo, "  # noqa: S608
                        f"MAX(knowable_as_of) AS hi, "
                        f"COUNT(DISTINCT retrieved_as_of) AS stamps "
                        f"FROM {table} WHERE season = ?", (season,),
                    ).fetchone()
                    row.update(rows=got["n"], first_knowable=got["lo"],
                               last_knowable=got["hi"], stamps=got["stamps"])
                except Exception as exc:
                    row["table"] = f"<absent: {type(exc).__name__}>"
            last_row = last_run(conn, source=spec.name, season=season, status=None)
            if last_row is not None:
                row.update(status=last_row["status"],
                           retrieved_as_of=last_row["retrieved_as_of"])
            out.append(row)
    return out


# ------------------------------------------------------ backfill report layer


def format_backfill_plan(plan: BackfillPlan) -> str:
    """The ``ingest backfill --dry-run`` report."""
    lines = [
        f"backfill plan (dry run — no network, no writes): seasons "
        f"{plan.seasons[0]}-{plan.seasons[-1]}, {len(plan.sources)} sources, "
        f"{plan.pulls} pull(s)",
        f"  protecting season {plan.protect_season} (the draft-critical partition): "
        "no season at or after it can be reached from this path.",
    ]
    for season in plan.seasons:
        lines.append(f"  season {season}")
        for s, decision in plan.decisions:
            if s != season:
                continue
            verb = "PULL" if decision.action == "pull" else decision.action.upper()
            lines.append(f"    {verb:>15}  {decision.name:<22} {decision.scope or ''}")
            if decision.action != "pull" or decision.reason != "due":
                lines.extend(textwrap.wrap(
                    decision.reason, width=92,
                    initial_indent="                     └─ ",
                    subsequent_indent="                        ",
                ))
    if not plan.with_weather:
        lines.append(
            "  game_weather_archive is NOT in this plan (--with-weather adds it): ~12 s "
            "per (season, week), roughly 18 minutes for five seasons — twelve times "
            "everything else here combined — and item 3.3 reads none of it."
        )
    if plan.force:
        lines.append(
            "  --force: pairs that ALREADY LANDED will be re-pulled under TODAY's stamp, "
            "appending a second retrieved_as_of partition (~+48 MB for a full five-season "
            "set). The old rows are not replaced; both partitions stay readable."
        )
    lines.append(
        "  every row lands with retrieved_as_of = TODAY and its real historical "
        "knowable_as_of, so the default `historical` view returns NOTHING for a past "
        "as_of. Read backfilled history through base.latest_truth(accessor)."
    )
    return "\n".join(lines)


def format_backfill_run(summaries: Sequence[dict]) -> str:
    """The backfill's completion report, grouped by season."""
    if not summaries:
        return "backfill: nothing to do"
    lines = []
    order = {name: i for i, name in enumerate(_BACKFILL_ORDER)}
    for season in sorted({s["season"] for s in summaries}):
        # Reported in DEPENDENCY order, not execution order. `run_backfill` writes
        # the already-landed `fresh` rows in a pre-pass before it calls
        # `run_ingest`, so the run log's run_id order truthfully reads
        # fresh-then-pulled — but a reader comparing two seasons wants the same
        # source on the same line each time.
        rows = sorted((s for s in summaries if s["season"] == season),
                      key=lambda s: order.get(s["source"], len(order)))
        landed = sum(s.get("rows", 0) or 0 for s in rows)
        lines.append(f"season {season}: {landed} rows over {len(rows)} sources")
        for s in rows:
            line = f"  [{s['status']:>15}] {s['source']:<22} rows={s.get('rows', 0)}"
            if s.get("reason"):
                line += f"  — {s['reason']}"
            lines.append(line)
    bad = sorted({f"{s['source']}/{s['season']}" for s in summaries
                  if s["status"] in PROBLEM_STATUSES})
    absent = sorted({f"{s['source']}/{s['season']}" for s in summaries
                     if s["status"] == STATUS_ABSENT})
    lines.append(
        f"backfill {summaries[0]['batch_id']}: {len(summaries)} (source, season) pairs, "
        + (f"PROBLEMS: {', '.join(bad)}" if bad else "no failures")
    )
    if absent:
        lines.append(f"  no file upstream for: {', '.join(absent)}")
    return "\n".join(lines)


def format_coverage(rows: Sequence[dict]) -> str:
    """``ingest coverage``: what history is actually stored, per source per season."""
    if not rows:
        return "coverage: no sources selected"
    seasons = sorted({r["season"] for r in rows})
    lines = [
        f"nfl history coverage — seasons {seasons[0]}-{seasons[-1]} "
        "(read UNGATED from the tables; the run log supplies the status)",
        f"  {'source':<22} {'season':>6} {'rows':>9} {'knowable span':<25} "
        f"{'stamps':>6}  last run",
    ]
    for source in dict.fromkeys(r["source"] for r in rows):
        for r in (x for x in rows if x["source"] == source):
            span = "—"
            if r["first_knowable"]:
                span = f"{r['first_knowable']} .. {r['last_knowable']}"
            lines.append(
                f"  {r['source']:<22} {r['season']:>6} "
                f"{('—' if r['rows'] is None else r['rows']):>9} {span:<25} "
                f"{('—' if r['stamps'] is None else r['stamps']):>6}  "
                f"{r['status'] or 'never'}"
            )
    empty = [f"{r['source']}/{r['season']}" for r in rows
             if not r["rows"] and r["status"] not in (STATUS_SKIPPED, None)]
    if empty:
        lines.append(
            "  RAN BUT STORED NOTHING: " + ", ".join(empty)
            + " — a logged run with zero stored rows. Check the run log's error column."
        )
    lines.append(
        "  more than one stamp for a season means the source was pulled on two "
        "different days; both partitions are stored and `latest_truth` reads the newest "
        "per key."
    )
    return "\n".join(lines)
