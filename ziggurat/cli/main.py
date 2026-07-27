"""`ziggurat` command-line entry point.

Standing rule: no logic in the CLI layer. Every command is a thin wrapper that
parses arguments, calls a package function, and prints the result.
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

import typer

from ziggurat.core.candidates import build_candidates, format_candidates
from ziggurat.core.divergence import build_divergence, format_report
from ziggurat.core.marginal import (
    DEFAULT_POOL_LIMIT,
    WeekResolutionError,
    build_board,
    format_marginal,
)
from ziggurat.core.lineup_support import build_lineup, format_lineup_recommendation
from ziggurat.core.scoring import score
from ziggurat.core.streaming import (
    StreamPositionError,
    format_stream_board,
    rank_streamers,
)
from ziggurat.core.valuation import (
    build_valuation,
    build_value_view,
    canon_position,
    format_valuation,
    format_value_view,
)
from ziggurat.core.waiver import build_waiver_plan, format_waiver_plan
from ziggurat.data.asof import nfl_season_of, normalize_as_of
from ziggurat.data.nfl.adp_rankings import get_adp_rankings
from ziggurat.data.nfl.espn_ranks import BoardCollapse, get_espn_draft_ranks
from ziggurat.data.nfl.espn_ranks import ensure_board as ensure_espn_board
from ziggurat.data.nfl.espn_source import load_espn_credentials
from ziggurat.data.nfl.refresh import (
    BACKFILL_MIN_SEASON,
    ORPHAN_STALE_MINUTES,
    BackfillRefused,
    BackfillTouchedProtectedSeason,
    backfill_coverage,
    format_backfill_plan,
    format_backfill_run,
    format_coverage,
    format_plan,
    format_reap,
    format_sources,
    needs_credentials,
    orphan_runs,
    plan_backfill,
    plan_ingest,
    reap_orphan_runs,
    resolve_stamp,
    run_backfill,
    run_failed,
    run_ingest,
    select_backfill_sources,
    select_sources,
)
from ziggurat.data.nfl.refresh import format_run as format_ingest_run
from ziggurat.data.nfl.refresh import format_status as format_ingest_status
from ziggurat.data.store import apply_schema, connect, migration_alerts, open_db
from ziggurat.league.state import (
    OwnTeamUnresolved,
    format_free_agents,
    format_roster,
    format_timeline,
    get_free_agents,
    get_player_state,
    holder_timeline,
    resolve_own_team,
)
from ziggurat.league.sync import format_run, format_status, run_sync
from ziggurat.llm import Router
from ziggurat.paths import DEFAULT_DB_PATH, REPO_ROOT
from ziggurat.scaffold import ensure_intel_tree

app = typer.Typer(
    help="Ziggurat — fantasy football decision support.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="SQLite database maintenance.", no_args_is_help=True)
intel_app = typer.Typer(help="Local intel/ tree maintenance.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(intel_app, name="intel")


def _echo_migration_alerts(alerts: dict) -> None:
    """Print the rows a migration left behind to say it silently dropped data.

    A migration runs inside ``open_db`` on EVERY command, so it may not raise —
    which forces the 007 table rebuilds to use ``INSERT OR REPLACE``, which means
    a key collision COLLAPSES rather than failing. ``store.migration_alerts``
    records such a loss as a positive ``meta`` fact; without this echo the alarm
    exists in the database and is never seen by anyone. Print only (rule 3): the
    detection lives in ``store.py``.
    """
    for key, detail in sorted(alerts.items()):
        typer.echo(f"MIGRATION ALERT [{key}]: {detail}", err=True)


@db_app.command("init")
def db_init(
    path: Annotated[Path, typer.Option(help="SQLite file to create/upgrade.")] = DEFAULT_DB_PATH,
) -> None:
    """Create the facts database and apply db/schema.sql (idempotent)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    apply_schema(conn)
    alerts = migration_alerts(conn)
    conn.close()
    typer.echo(f"schema applied: {path}")
    _echo_migration_alerts(alerts)


@intel_app.command("init")
def intel_init(
    root: Annotated[Path, typer.Option(help="Repo root (for tests).")] = REPO_ROOT,
) -> None:
    """Recreate the gitignored intel/ skeleton from templates/intel/ (never overwrites)."""
    created = ensure_intel_tree(root)
    if created:
        for p in created:
            typer.echo(f"created {p}")
    else:
        typer.echo("intel/ already complete — nothing created")


@app.command()
def divergence(
    espn_ranks: Annotated[Path, typer.Option(help="JSON file of ESPN-side rank rows "
                                                  "(list of {espn_id|team, position, espn_pos_rank}).")],
    as_of: Annotated[str, typer.Option(help="Knowledge-time cutoff (YYYY-MM-DD).")],
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
    season: Annotated[Optional[int], typer.Option(help="Restrict market side to a season.")] = None,
    position: Annotated[Optional[str], typer.Option(help="Restrict to one position (QB/RB/…/DST).")] = None,
    ecr_type: Annotated[str, typer.Option(help="Market ECR flavor (ro=redraft overall).")] = "ro",
    gate: Annotated[float, typer.Option(help="Confidence-gate multiplier on market sd.")] = 1.0,
) -> None:
    """Print the ESPN-vs-market positional-rank divergence report (item 1.5 §6).

    Market side is FantasyPros ECR read as-of; the ESPN side is a hand-authored
    JSON snapshot until the live ESPN sync (item 3.1) lands.
    """
    conn = connect(path)
    market_rows = get_adp_rankings(
        conn, as_of=as_of, season=season, position=position, ecr_type=ecr_type
    )
    espn_rows = json.loads(espn_ranks.read_text())
    report = build_divergence(market_rows, espn_rows, gate_multiplier=gate)
    conn.close()
    typer.echo(format_report(report))


def _parse_weeks(weeks: Optional[str]):
    """Parse a ``"1-17"`` or ``"1"`` week spec into a range, or None (accessor
    default). Pure argument parsing — the valuation logic lives in the package."""
    if weeks is None:
        return None
    spec = weeks.strip()
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return range(int(lo), int(hi) + 1)
    w = int(spec)
    return range(w, w + 1)


def _parse_now(now: Optional[str]):
    """Parse a ``--now`` ISO datetime into a tz-aware ET datetime (the lineup
    decision clock), or None. Pure parsing — the GTD/inactives logic lives in the
    package. A naive datetime is interpreted as Eastern (the gameday timezone)."""
    if now is None:
        return None
    from zoneinfo import ZoneInfo
    dt = datetime.fromisoformat(now.strip())
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=ZoneInfo("America/New_York"))


@app.command()
def valuation(
    as_of: Annotated[str, typer.Option(help="Knowledge-time cutoff (YYYY-MM-DD).")],
    season: Annotated[int, typer.Option(help="Season to value.")],
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
    position: Annotated[Optional[str], typer.Option(help="Filter to one position (QB/RB/…/DST/K).")] = None,
    top: Annotated[Optional[int], typer.Option(help="Show only the top N rows.")] = None,
    weeks: Annotated[Optional[str], typer.Option(help="Regular-season week window, e.g. '1-17'.")] = None,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    espn: Annotated[bool, typer.Option("--espn", help="Refresh the ESPN board and print the value view.")] = False,
    league_id: Annotated[Optional[int], typer.Option(help="ESPN leagueId (else ESPN_LEAGUE_ID env).")] = None,
    allow_shrink: Annotated[bool, typer.Option("--allow-shrink",
        help="Accept an ESPN board materially smaller than the stored one (default: refuse).")] = False,
) -> None:
    """Print the global static VOR valuation board (item 2.1).

    Default: the house VOR board. With ``--espn``, make the ESPN default board
    for ``--as-of`` available (a live pull only when ``--as-of`` IS today; a past
    day reads the stored snapshot rather than back-stamping today's board over
    it) and print the "what the room can't see" value view instead. All
    computation lives in ``core/valuation.py`` / ``data/nfl/espn_ranks.py``; this
    command only parses, calls, and prints (rule 3).
    """
    canon = canon_position(position) if position is not None else None
    conn = connect(path)
    rows = build_valuation(
        conn, as_of=as_of, season=season, weeks=_parse_weeks(weeks), source=source
    )
    if canon is not None:
        rows = [r for r in rows if r.position == canon]

    if espn:
        creds = load_espn_credentials(league_id=league_id)
        try:
            typer.echo(ensure_espn_board(conn, season=season, as_of=as_of, today=_today(),
                                         allow_shrink=allow_shrink, **creds), err=True)
        except BoardCollapse as exc:   # legible, not a traceback, on a draft-day command
            conn.close()
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        espn_rows = get_espn_draft_ranks(conn, as_of=as_of, season=season)
        view = build_value_view(rows, espn_rows)
        conn.close()
        typer.echo(format_value_view(view, top=top))
    else:
        conn.close()
        typer.echo(format_valuation(rows, top=top))


@app.command()
def marginal(
    as_of: Annotated[Optional[str], typer.Option(help="Knowledge-time cutoff (default today).")] = None,
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    team: Annotated[Optional[int], typer.Option(help="League team id (default: your SWID's team).")] = None,
    from_week: Annotated[Optional[int], typer.Option("--from-week",
        help="First remaining week. REQUIRED whenever the week cannot be derived.")] = None,
    last_week: Annotated[int, typer.Option("--last-week",
        help="Final week priced (applies whether or not --from-week is given).")] = 17,
    top: Annotated[Optional[int], typer.Option(help="Show only the N most droppable.")] = None,
    reasons: Annotated[bool, typer.Option("--reasons", help="Print every row's reasons.")] = False,
    pool_limit: Annotated[int, typer.Option("--pool-limit",
        help="Free agents scanned per position (0 = the whole pool).")] = DEFAULT_POOL_LIMIT,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Print the roster-context drop board: what each of your players is worth
    over the remaining season, against the best free agent you could add (3.2).

    Every number, reason, cap and prior lives in ``core/marginal.py``; this
    command parses, calls, and prints (rule 3).
    """
    day = as_of or _today()
    resolved_season = _season(season)
    conn = open_db(path)
    try:
        team_id = team
        if team_id is None:
            creds = load_espn_credentials()
            team_id = resolve_own_team(
                conn, as_of=day, season=resolved_season, swid=creds["swid"]
            )
        board = build_board(
            conn,
            as_of=day,
            season=resolved_season,
            roster=get_player_state(
                conn, as_of=day, season=resolved_season, on_team_id=team_id
            ),
            weeks=None if from_week is None else range(from_week, last_week + 1),
            last_week=last_week,
            pool_limit=None if pool_limit == 0 else pool_limit,
            source=source,
            today=_today(),
        )
    except (WeekResolutionError, OwnTeamUnresolved) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(format_marginal(board, top=top, reasons=reasons))


@app.command()
def waivers(
    as_of: Annotated[Optional[str], typer.Option(help="Knowledge-time cutoff (default today).")] = None,
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    team: Annotated[Optional[int], typer.Option(help="League team id (default: your SWID's team).")] = None,
    from_week: Annotated[Optional[int], typer.Option("--from-week",
        help="First remaining week. REQUIRED whenever the week cannot be derived.")] = None,
    last_week: Annotated[int, typer.Option("--last-week",
        help="Final week priced (applies whether or not --from-week is given).")] = 17,
    claim_budget: Annotated[int, typer.Option("--claim-budget",
        help="Max claims/grabs in the action shortlist (extra claims are free).")] = 3,
    reasons: Annotated[bool, typer.Option("--reasons", help="Print every claim/drop's reasons.")] = False,
    pool_limit: Annotated[int, typer.Option("--pool-limit",
        help="Free agents scanned per position (0 = the whole pool).")] = DEFAULT_POOL_LIMIT,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Plan the week's waiver claims: roster-legality precheck (IR eligibility +
    forced drop) FIRST, then queued waiver claims and first-come grabs ranked with
    their drops, plus the drop board (item 3.4).

    If the roster is illegal (an IR occupant reset out of IR-eligibility), the plan
    refuses to plan claims and proposes the fix. All legality, claim/drop and
    ordering logic lives in ``core/waiver.py``; this command parses, calls, and
    prints (rule 3).
    """
    day = as_of or _today()
    resolved_season = _season(season)
    conn = open_db(path)
    try:
        team_id = team
        if team_id is None:
            creds = load_espn_credentials()
            team_id = resolve_own_team(
                conn, as_of=day, season=resolved_season, swid=creds["swid"]
            )
        plan = build_waiver_plan(
            conn,
            as_of=day,
            season=resolved_season,
            own_team_id=team_id,
            weeks=None if from_week is None else range(from_week, last_week + 1),
            last_week=last_week,
            pool_limit=None if pool_limit == 0 else pool_limit,
            source=source,
            claim_budget=claim_budget,
            today=_today(),
        )
    except (WeekResolutionError, OwnTeamUnresolved) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(format_waiver_plan(plan, reasons=reasons))


@app.command()
def stream(
    as_of: Annotated[Optional[str], typer.Option(help="Knowledge-time cutoff (default today).")] = None,
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    week: Annotated[Optional[int], typer.Option(
        help="Week to stream (default: the current week resolved from state/schedule).")] = None,
    position: Annotated[Optional[str], typer.Option(
        help="DST or K (default: rank both).")] = None,
    last_week: Annotated[int, typer.Option("--last-week", help="Final week for week resolution.")] = 17,
    top: Annotated[Optional[int], typer.Option(help="Show only the top N per position.")] = None,
    reasons: Annotated[bool, typer.Option("--reasons", help="Print every candidate's reasons.")] = False,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Rank the free-agent D/ST and kickers to stream this week: opponent matchup,
    Vegas, and weather tilts on the house projection (item 3.5).

    All pricing, thresholds and labelled hypotheses live in ``core/streaming.py``;
    this command parses, calls, and prints (rule 3).
    """
    day = as_of or _today()
    resolved_season = _season(season)
    positions = [position] if position is not None else ["DST", "K"]
    conn = open_db(path)
    try:
        boards = [
            rank_streamers(
                conn, as_of=day, season=resolved_season, position=pos, week=week,
                last_week=last_week, source=source, today=_today(),
            )
            for pos in positions
        ]
    except (WeekResolutionError, StreamPositionError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo("\n\n".join(format_stream_board(b, top=top, reasons=reasons) for b in boards))


@app.command()
def lineup(
    as_of: Annotated[Optional[str], typer.Option(help="Knowledge-time cutoff (default today).")] = None,
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    team: Annotated[Optional[int], typer.Option(help="League team id (default: your SWID's team).")] = None,
    week: Annotated[Optional[int], typer.Option(
        help="Week to seat (default: the current week resolved from state/schedule).")] = None,
    opponent_total: Annotated[Optional[float], typer.Option("--opponent-total",
        help="Override the opponent's projected total (else auto-computed from his roster).")] = None,
    last_week: Annotated[int, typer.Option("--last-week", help="Final week for week resolution.")] = 17,
    reasons: Annotated[bool, typer.Option("--reasons", help="Print every starter's reasons.")] = False,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    now: Annotated[Optional[str], typer.Option(
        help="Decision clock (ET ISO datetime) for GTD/inactives logic; default midnight ET of --as-of.")] = None,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Recommend the week's starting lineup to maximise P(win): opponent-aware
    favorite/underdog posture, slot-lock optionality, GTD contingencies and an
    inactives watch (item 3.5).

    All seating, variance priors, posture and sanity logic live in
    ``core/lineup_support.py``; this command parses, calls, and prints (rule 3).
    """
    day = as_of or _today()
    resolved_season = _season(season)
    now_et = _parse_now(now)
    conn = open_db(path)
    try:
        team_id = team
        if team_id is None:
            creds = load_espn_credentials()
            team_id = resolve_own_team(
                conn, as_of=day, season=resolved_season, swid=creds["swid"]
            )
        rec = build_lineup(
            conn,
            as_of=day,
            season=resolved_season,
            own_team_id=team_id,
            week=week,
            opponent_total=opponent_total,
            last_week=last_week,
            source=source,
            now=now_et,
            today=_today(),
        )
    except (WeekResolutionError, OwnTeamUnresolved) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(format_lineup_recommendation(rec, reasons=reasons))


@app.command()
def candidates(
    as_of: Annotated[Optional[str], typer.Option(help="Knowledge-time cutoff (default today).")] = None,
    season: Annotated[Optional[int], typer.Option(help="Season (default: current NFL season).")] = None,
    week: Annotated[Optional[int], typer.Option(
        help="Last fully-played week to analyze (default: resolved from the schedule).")] = None,
    since: Annotated[Optional[str], typer.Option(
        help="QB1-change baseline day; enables the QB1 labelled-hypothesis arm (season >= 2025).")] = None,
    position: Annotated[Optional[str], typer.Option(
        help="Restrict the usage arm to one position (RB/WR/TE).")] = None,
    top: Annotated[Optional[int], typer.Option(help="Show only the top N per signal block.")] = None,
    reasons: Annotated[bool, typer.Option("--reasons", help="Print every candidate's reasons.")] = False,
    validate: Annotated[bool, typer.Option(
        "--validate/--latest-truth", "--validate",
        help="Bind the latest_truth view for a PAST-season validation read "
             "(backfilled history reads EMPTY under the default historical view). "
             "Leave off for the live current-season path.")] = False,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Rank this week's breakout candidates from usage deltas and injury shocks,
    plus the QB1-change hypothesis (item 3.3).

    All ranking, thresholds and merge logic live in ``core/candidates.py``; this
    command parses, calls, and prints (rule 3).
    """
    from ziggurat.core.candidates import NoCompletedWeek
    from ziggurat.data.nfl import base

    day = as_of or _today()
    conn = open_db(path)
    # --validate selects the latest_truth wrapper (past-season validation); the
    # live path stays on the default historical view (Rule 3: the flag only picks
    # which wrapper to call — no logic here).
    generator = base.latest_truth(build_candidates) if validate else build_candidates
    try:
        board = generator(
            conn,
            as_of=day,
            season=_season(season),
            week=week,
            positions=None if position is None else [canon_position(position)],
            since=since,
            today=_today(),
        )
    except NoCompletedWeek as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(format_candidates(board, top=top, reasons=reasons))


@app.command("mock-draft")
def mock_draft(
    season: Annotated[int, typer.Option(help="Season whose board to draft from.")],
    as_of: Annotated[Optional[str], typer.Option(
        help="Knowledge-time cutoff (YYYY-MM-DD); defaults to today.")] = None,
    n: Annotated[Optional[int], typer.Option(
        help="How many mock drafts to run (default 1000; 100 for --strategy engine).")] = None,
    slot: Annotated[int, typer.Option(help="Your draft slot, 1..teams.")] = 1,
    strategy: Annotated[str, typer.Option(help="Operator strategy: 'vor', 'espn', or "
                                               "'engine' (the item-2.3 pick engine; compute-heavy — "
                                               "use a small --n).")] = "vor",
    seed: Annotated[int, typer.Option(help="RNG seed (reproducible).")] = 42,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    weeks: Annotated[Optional[str], typer.Option(help="Regular-season week window, e.g. '1-17'.")] = None,
) -> None:
    """Run headless mock drafts and print a strategy's outcome distribution (item 2.2).

    All logic lives in the DELETABLE ``ziggurat.draft`` package (Rule 8), imported
    lazily here so nothing outside it couples statically. Parse, load the board,
    run, print (Rule 3).
    """
    # Lazy (in-body) import: keeps the deletable draft package off every other
    # module's import graph — Rule 8. Deleting ziggurat/draft/ only breaks this
    # one command, not the rest of the CLI.
    from ziggurat.draft.bots import FollowEspnRank, FollowVor
    from ziggurat.draft.engine import PickEngine
    from ziggurat.draft.priors import ROOM_PRIORS_2025
    from ziggurat.draft.simulator import (
        DEFAULT_ROSTER,
        format_strategy_summary,
        load_board,
        run_many,
    )

    strategies = {
        "vor": (FollowVor(), "follow-VOR"),
        "espn": (FollowEspnRank(), "follow-ESPN-rank"),
        "engine": (PickEngine(), "pick-engine"),
    }
    if strategy not in strategies:
        raise typer.BadParameter("strategy must be 'vor', 'espn', or 'engine'")
    strat, strat_name = strategies[strategy]
    if not 1 <= slot <= DEFAULT_ROSTER.teams:
        raise typer.BadParameter(f"slot must be 1..{DEFAULT_ROSTER.teams}")
    # The engine's survival rollouts cost ~0.5-1s per draft vs milliseconds for
    # the baselines, so it gets a smaller default and an up-front time notice
    # instead of a silent multi-minute hang (Rule 6).
    resolved_n = n if n is not None else (100 if strategy == "engine" else 1000)
    if strategy == "engine" and resolved_n > 20:
        typer.echo(
            f"pick-engine strategy: running {resolved_n} drafts at roughly a "
            f"second each — expect ~{max(1, round(resolved_n / 90))} min "
            f"(use --n to shrink)...",
            err=True,
        )

    # The CLI edge is where "now" is allowed to materialize; library functions
    # still require an explicit as_of (Rule 1).
    resolved_as_of = as_of or date.today().isoformat()

    conn = connect(path)
    board = load_board(
        conn, as_of=resolved_as_of, season=season, source=source, weeks=_parse_weeks(weeks)
    )
    conn.close()
    if not board:
        typer.echo(
            f"No draftable board for season {season} as of {resolved_as_of}: the "
            "database has no projections/ESPN ranks visible at that date. Check the "
            "season and --as-of, and that this season's data has been pulled.",
            err=True,
        )
        raise typer.Exit(code=1)

    summary = run_many(
        board, n=resolved_n, operator_slot=slot - 1, strategy=strat, strategy_name=strat_name,
        priors=ROOM_PRIORS_2025, seed=seed,
    )
    typer.echo(format_strategy_summary(summary))


def _resolve_draft_launch(
    *, season, as_of, journal, resume, pick_order, path, source, weeks,
):
    """Shared parse-level resolution for the two draft front-ends (Rule 3: this
    resolves WHICH journal/board to use — discovery and header parsing live in
    session.py; board loading in simulator.py). Returns
    ``(board, resolved_season, resolved_as_of, resolved_journal, order)``."""
    from ziggurat.draft.session import find_latest_journal, read_journal_header
    from ziggurat.draft.simulator import load_board

    draft_dir = REPO_ROOT / "data" / "draft"

    if resume:
        # Recover an interrupted draft. The journal HEADER — not today's clock — is
        # the source of truth for the board snapshot, so the replay runs on the
        # original board and a midnight rollover cannot orphan it (recon §crash
        # NEW-1 / §arch NEW-1). Discovery + header parse are loud in session.py.
        if journal is not None:
            resolved_journal = journal
        else:
            found = find_latest_journal(draft_dir)
            if found is None:
                typer.echo(
                    f"No draft journal found under {draft_dir} to resume. Start a new "
                    "draft (omit --resume), or point --journal at the session file.",
                    err=True,
                )
                raise typer.Exit(code=1)
            resolved_journal = found
        header = read_journal_header(resolved_journal)
        resolved_as_of = header["as_of"]
        resolved_season = header["season"]
    else:
        if season is None:
            raise typer.BadParameter("--season is required to start a new draft")
        # The CLI edge is where "now" materializes (Rule 1). A timestamped default
        # name means a re-launch never lands on a live journal (no clobber, and a
        # midnight rollover cannot orphan it — --resume discovers the newest).
        resolved_as_of = as_of or date.today().isoformat()
        resolved_season = season
        resolved_journal = journal or (
            draft_dir / f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        )

    order = [int(x) for x in pick_order.split(",")] if pick_order else None

    conn = connect(path)
    board = load_board(
        conn, as_of=resolved_as_of, season=resolved_season, source=source,
        weeks=_parse_weeks(weeks),
    )
    conn.close()
    if not board:
        typer.echo(
            f"No draftable board for season {resolved_season} as of {resolved_as_of}: "
            "the database has no projections/ESPN ranks visible at that date. Check "
            "the season and --as-of, and that this season's data has been pulled.",
            err=True,
        )
        raise typer.Exit(code=1)
    return board, resolved_season, resolved_as_of, resolved_journal, order


@app.command("draft-board")
def draft_board(
    season: Annotated[Optional[int], typer.Option(
        help="Season whose board to draft from (required to START a new draft; on "
             "--resume it is read from the journal header).")] = None,
    as_of: Annotated[Optional[str], typer.Option(
        help="Knowledge-time cutoff (YYYY-MM-DD); defaults to today. Ignored on "
             "--resume (the journalled as_of is used).")] = None,
    slot: Annotated[int, typer.Option(help="Your draft slot / seat id, 1..teams.")] = 1,
    pick_order: Annotated[Optional[str], typer.Option(
        help="Real draft order as a CSV of 0-based seat ids "
             "(draft position -> seat id); defaults to identity 0,1,2,...")] = None,
    journal: Annotated[Optional[Path], typer.Option(
        help="Crash-recovery journal file; a new draft defaults to a timestamped "
             "data/draft/session-<YYYYMMDD>-<HHMMSS>.jsonl, and --resume without "
             "--journal recovers the newest session in data/draft.")] = None,
    resume: Annotated[bool, typer.Option("--resume", help="Resume from the journal (replay).")] = False,
    rollouts: Annotated[int, typer.Option(help="Survival rollouts per recommendation.")] = 512,
    seed: Annotated[int, typer.Option(help="RNG seed (reproducible).")] = 42,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    weeks: Annotated[Optional[str], typer.Option(help="Regular-season week window, e.g. '1-17'.")] = None,
) -> None:
    """Launch the live draft-board TUI (item 2.4).

    All logic lives in the DELETABLE ``ziggurat.draft`` package (Rule 8), imported
    lazily here so nothing outside it couples statically. Parse, resolve the journal
    (discovering the newest on --resume), load the board at the right as_of, hand off
    to the app loop (Rule 3 — the discovery/header helpers live in session.py).
    """
    # Lazy (in-body) import: keeps the deletable draft package off every other
    # module's import graph — Rule 8 (same pattern as mock-draft above).
    from ziggurat.draft import app as draft_app
    from ziggurat.draft.simulator import DEFAULT_ROSTER

    if not 1 <= slot <= DEFAULT_ROSTER.teams:
        raise typer.BadParameter(f"slot must be 1..{DEFAULT_ROSTER.teams}")

    board, resolved_season, resolved_as_of, resolved_journal, order = (
        _resolve_draft_launch(
            season=season, as_of=as_of, journal=journal, resume=resume,
            pick_order=pick_order, path=path, source=source, weeks=weeks,
        )
    )

    draft_app.launch(
        board,
        operator_slot=slot - 1,
        pick_order=order,
        season=resolved_season,
        as_of=resolved_as_of,
        journal_path=resolved_journal,
        resume=resume,
        rollouts=rollouts,
        seed=seed,
        roster=DEFAULT_ROSTER,
    )


@app.command("draft-web")
def draft_web(
    season: Annotated[Optional[int], typer.Option(
        help="Season whose board to draft from (required to START a new draft; on "
             "--resume it is read from the journal header).")] = None,
    as_of: Annotated[Optional[str], typer.Option(
        help="Knowledge-time cutoff (YYYY-MM-DD); defaults to today. Ignored on "
             "--resume (the journalled as_of is used).")] = None,
    slot: Annotated[int, typer.Option(help="Your draft slot / seat id, 1..teams.")] = 1,
    pick_order: Annotated[Optional[str], typer.Option(
        help="Real draft order as a CSV of 0-based seat ids "
             "(draft position -> seat id); defaults to identity 0,1,2,...")] = None,
    journal: Annotated[Optional[Path], typer.Option(
        help="Crash-recovery journal file; a new draft defaults to a timestamped "
             "data/draft/session-<YYYYMMDD>-<HHMMSS>.jsonl, and --resume without "
             "--journal recovers the newest session in data/draft.")] = None,
    resume: Annotated[bool, typer.Option("--resume", help="Resume from the journal (replay).")] = False,
    rollouts: Annotated[int, typer.Option(help="Survival rollouts per recommendation.")] = 512,
    seed: Annotated[int, typer.Option(help="RNG seed (reproducible).")] = 42,
    port: Annotated[int, typer.Option(help="Local port for the cockpit page.")] = 8811,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    weeks: Annotated[Optional[str], typer.Option(help="Regular-season week window, e.g. '1-17'.")] = None,
) -> None:
    """Launch the live-search web draft cockpit (Checkpoint 2).

    Same headless session, journal, and engine as ``draft-board`` — rendered as a
    local web page (127.0.0.1 only) with per-keystroke autocomplete for burst pick
    entry. All logic lives in the DELETABLE ``ziggurat.draft`` package (Rule 8),
    imported lazily; this command parses, loads the board, and hands off (Rule 3).
    """
    from ziggurat.draft import webapp
    from ziggurat.draft.simulator import DEFAULT_ROSTER

    if not 1 <= slot <= DEFAULT_ROSTER.teams:
        raise typer.BadParameter(f"slot must be 1..{DEFAULT_ROSTER.teams}")

    board, resolved_season, resolved_as_of, resolved_journal, order = (
        _resolve_draft_launch(
            season=season, as_of=as_of, journal=journal, resume=resume,
            pick_order=pick_order, path=path, source=source, weeks=weeks,
        )
    )

    webapp.launch(
        board,
        operator_slot=slot - 1,
        pick_order=order,
        season=resolved_season,
        as_of=resolved_as_of,
        journal_path=resolved_journal,
        resume=resume,
        rollouts=rollouts,
        seed=seed,
        roster=DEFAULT_ROSTER,
        port=port,
    )


league_app = typer.Typer(help="League state sync + reads (item 3.1).", no_args_is_help=True)
app.add_typer(league_app, name="league")


def _today() -> str:
    """Today, for CLI defaults only. The package layer never assumes 'now' —
    every accessor takes an explicit as_of (rule 1); this is where the operator's
    implicit "today" is made explicit before it crosses into the package."""
    return date.today().isoformat()


def _season(season):
    """Resolve a --season default to the current NFL season (asof.nfl_season_of),
    so January runs do not silently jump to the next season number."""
    return season if season is not None else nfl_season_of(_today())


@league_app.command("sync")
def league_sync(
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    as_of: Annotated[Optional[str], typer.Option(help="Snapshot day stamp (default today).")] = None,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
    league_id: Annotated[Optional[int], typer.Option(help="ESPN league id (default $ESPN_LEAGUE_ID).")] = None,
    transactions: Annotated[bool, typer.Option(help="Also pull the (best-effort) transaction feed.")] = True,
    allow_shrink: Annotated[bool, typer.Option("--allow-shrink",
        help="Accept a snapshot materially smaller than the stored one (default: refuse).")] = False,
    allow_backfill: Annotated[bool, typer.Option("--allow-backfill",
        help="Permit --as-of on a past day, writing TODAY's state under that date.")] = False,
) -> None:
    """Pull one full league-state snapshot: rosters, standings, matchups, FA pool.

    This is the scheduled command. ESPN serves NO historical league state, so a
    day this does not capture is unrecoverable — run it on a timer, and check
    `ziggurat league status`.
    """
    creds = load_espn_credentials(league_id=league_id)
    conn = open_db(path)
    try:
        summary = run_sync(
            conn, season=_season(season), league_id=creds["league_id"],
            espn_s2=creds["espn_s2"], swid=creds["swid"],
            retrieved_as_of=as_of or _today(), today=_today(),
            include_transactions=transactions,
            allow_shrink=allow_shrink, allow_backfill=allow_backfill,
        )
    finally:
        conn.close()
    typer.echo(format_run(summary))


@league_app.command("status")
def league_status(
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    through: Annotated[Optional[str], typer.Option(help="Judge coverage through this day.")] = None,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Report sync health: last run, snapshot coverage, and permanently missing days."""
    conn = open_db(path)
    typer.echo(format_status(conn, season=_season(season), through=through or _today()))
    conn.close()


@league_app.command("roster")
def league_roster(
    team: Annotated[int, typer.Option(help="League team id.")],
    as_of: Annotated[Optional[str], typer.Option(help="Knowledge-time cutoff (default today).")] = None,
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Print a team's roster as of a date."""
    conn = open_db(path)
    rows = get_player_state(conn, as_of=as_of or _today(), season=_season(season), on_team_id=team)
    conn.close()
    typer.echo(format_roster(rows))


@league_app.command("free-agents")
def league_free_agents(
    as_of: Annotated[Optional[str], typer.Option(help="Knowledge-time cutoff (default today).")] = None,
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    position: Annotated[Optional[str], typer.Option(help="Restrict to QB/RB/WR/TE/K/D-ST.")] = None,
    limit: Annotated[int, typer.Option(help="Rows to print.")] = 40,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Print the free-agent pool as of a date, most-owned first."""
    conn = open_db(path)
    rows = get_free_agents(conn, as_of=as_of or _today(), season=_season(season), position=position)
    conn.close()
    typer.echo(format_free_agents(rows, limit=limit))


@league_app.command("holdings")
def league_holdings(
    player_id: Annotated[str, typer.Option("--player-id", help="ESPN player id.")],
    season: Annotated[Optional[int], typer.Option(help="League season (default: current NFL season).")] = None,
    since: Annotated[Optional[str], typer.Option(help="Only snapshots on/after this day.")] = None,
    until: Annotated[Optional[str], typer.Option(help="Only snapshots on/before this day.")] = None,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Show who held a player over time (the observed snapshot history)."""
    conn = open_db(path)
    segments = holder_timeline(conn, season=_season(season), espn_player_id=player_id,
                               since=since, until=until)
    conn.close()
    typer.echo(format_timeline(segments, player_label=f"espn_id {player_id}"))


ingest_app = typer.Typer(help="NFL source refresh cadence (item 3.1b).", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")


@ingest_app.command("run")
def ingest_run(
    group: Annotated[Optional[str], typer.Option(help="Cadence group: daily / weekly / gameday.")] = None,
    source: Annotated[Optional[list[str]], typer.Option(help="Pull only these sources (repeatable).")] = None,
    season: Annotated[Optional[int], typer.Option(help="NFL season (default: current).")] = None,
    as_of: Annotated[Optional[str], typer.Option(help="retrieved_as_of stamp (default today).")] = None,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
    league_id: Annotated[Optional[int], typer.Option(help="ESPN league id (default $ESPN_LEAGUE_ID).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run",
        help="Report what WOULD be pulled and exit. Touches no network and writes nothing.")] = False,
    allow_shrink: Annotated[bool, typer.Option("--allow-shrink",
        help="Accept an ESPN board materially smaller than the stored one (default: refuse).")] = False,
    allow_backfill: Annotated[bool, typer.Option("--allow-backfill",
        help="Permit --as-of on a past day (a manufactured leak; espn_ranks would also "
             "DELETE that day's board).")] = False,
    force: Annotated[bool, typer.Option("--force",
        help="Pull even a source whose last good pull is still inside its interval.")] = False,
) -> None:
    """Refresh NFL sources. This is the scheduled command.

    Sources whose season phase says they have nothing to pull, whose dependency
    is missing, whose last good pull is still inside their interval, or whose
    upstream has not published this season yet are RECORDED (skipped / fresh /
    upstream_absent), not silently omitted. Exits nonzero if any source failed,
    so a systemd unit reports the failure.
    """
    conn = open_db(path)
    try:
        specs = select_sources(group=group, names=source or None)
        # Both branches resolve the day ONCE, together: the dry run used to plan
        # against `--as-of` while the real run planned against today, so the two
        # disagreed on 3 of 8 daily sources — and the preview said espn_ranks
        # would be SKIPPED right before the real run deleted its board partition.
        stamp, day = resolve_stamp(as_of or _today(), _today(), allow_backfill=allow_backfill)
        credentials = None
        if needs_credentials(specs):
            try:
                credentials = load_espn_credentials(league_id=league_id)
            except RuntimeError as exc:  # recorded as 'skipped' by the package layer
                typer.echo(f"note: {exc}", err=True)
        if dry_run:
            typer.echo(format_plan(plan_ingest(
                conn, sources=specs, season=_season(season), today=day,
                have_credentials=credentials is not None, force=force,
            )))
            return
        summaries = run_ingest(
            conn, sources=specs, season=_season(season),
            retrieved_as_of=stamp, today=day, credentials=credentials,
            allow_shrink=allow_shrink, allow_backfill=allow_backfill, force=force,
        )
    except ValueError as exc:   # a refused selection or a refused back-stamp
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        conn.close()
    typer.echo(format_ingest_run(summaries))
    if run_failed(summaries):
        raise typer.Exit(code=1)


@ingest_app.command("status")
def ingest_status(
    season: Annotated[Optional[int], typer.Option(help="NFL season (default: current).")] = None,
    through: Annotated[Optional[str], typer.Option(help="Judge staleness as of this day.")] = None,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Per-source last successful pull + staleness verdict (3.2's staleness source)."""
    conn = open_db(path)
    report = format_ingest_status(conn, season=_season(season), today=through or _today())
    alerts = migration_alerts(conn)
    conn.close()
    typer.echo(report)
    _echo_migration_alerts(alerts)


@ingest_app.command("sources")
def ingest_sources() -> None:
    """List the source registry: cadence group, phases, interval, and flags."""
    typer.echo(format_sources())


@ingest_app.command("backfill")
def ingest_backfill(
    first: Annotated[int, typer.Option(help="Oldest season to land (>= 2021).")] = BACKFILL_MIN_SEASON,
    last: Annotated[Optional[int], typer.Option(
        help="Newest season to land (default: last COMPLETED season). The current "
             "season is refused — the 3.1b cadence owns it.")] = None,
    source: Annotated[Optional[list[str]], typer.Option(
        help="Backfill only these sources (repeatable). Excluded names are refused "
             "with the measured reason.")] = None,
    with_weather: Annotated[bool, typer.Option("--with-weather",
        help="Also pull the ERA5 weather ARCHIVE (~18 min for five seasons, 12x "
             "everything else combined; item 3.3 reads none of it).")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run",
        help="Report what WOULD be pulled and exit. Touches no network, writes nothing.")] = False,
    force: Annotated[bool, typer.Option("--force",
        help="Re-pull (source, season) pairs that ALREADY LANDED, under today's stamp. "
             "Does NOT override the season bounds or the active-cadence refusal.")] = False,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Land historical NFL seasons (item 3.2c). Not the scheduled command.

    Every row is written with ``retrieved_as_of = TODAY`` and its real historical
    ``knowable_as_of``, so the default ``historical`` view returns NOTHING for a
    past ``as_of`` — read backfilled history through ``base.latest_truth(accessor)``.

    NOTE, because the word is overloaded: this has nothing to do with
    ``ingest run --allow-backfill``, which writes TODAY's data under a PAST
    ``retrieved_as_of`` (a manufactured leak). That flag is not available here and
    the package layer hard-codes it off.

    Ordering, fences, the run log and the draft-critical fingerprint all live in
    ``data/nfl/refresh.py``; this command parses, calls, prints (rule 3).
    """
    resolved_last = last if last is not None else nfl_season_of(_today()) - 1
    conn = open_db(path)
    try:
        specs = select_backfill_sources(names=source or None, with_weather=with_weather)
        if dry_run:
            typer.echo(format_backfill_plan(plan_backfill(
                conn, first=first, last=resolved_last, sources=specs,
                today=_today(), force=force,
            )))
            return
        summaries = run_backfill(
            conn, first=first, last=resolved_last, sources=specs,
            retrieved_as_of=_today(), today=_today(), force=force,
            progress=lambda line: typer.echo(line, err=True),
        )
    except BackfillTouchedProtectedSeason as exc:
        typer.echo(format_backfill_run(exc.summaries))
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except (BackfillRefused, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        conn.close()
    typer.echo(format_backfill_run(summaries))
    if run_failed(summaries):
        raise typer.Exit(code=1)


@ingest_app.command("reap")
def ingest_reap(
    older_than_minutes: Annotated[int, typer.Option(
        help="Only rows that have been `running` at least this long. 0 clears every "
             "running row, including one that is genuinely in flight.")] = ORPHAN_STALE_MINUTES,
    dry_run: Annotated[bool, typer.Option("--dry-run",
        help="List the orphans and exit. Writes nothing.")] = False,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """Clear ingest runs left marked `running` by a killed process (item 3.2c).

    A run row is written BEFORE the network call and only the run itself clears
    it, so a Ctrl-C or a systemd ``TimeoutStartSec`` SIGTERM leaves one behind on
    purpose — silence is not success. But ``ingest backfill`` refuses to start
    while any run is marked `running`, and the only automatic reaper fires when
    that exact (source, season) pair runs again — which for the backfill-only
    sources no command could reach. This is that missing command.

    It rewrites the run LOG only. No fact table is touched, nothing is deleted,
    and a run that turns out to be alive overwrites its own row when it finishes.
    """
    conn = open_db(path)
    try:
        rows = (orphan_runs(conn, older_than_minutes=older_than_minutes) if dry_run else
                reap_orphan_runs(conn, older_than_minutes=older_than_minutes))
    finally:
        conn.close()
    typer.echo(format_reap(rows, older_than_minutes=older_than_minutes, dry_run=dry_run))


@ingest_app.command("coverage")
def ingest_coverage(
    first: Annotated[int, typer.Option(help="Oldest season to report.")] = BACKFILL_MIN_SEASON,
    last: Annotated[Optional[int], typer.Option(
        help="Newest season to report (default: last completed season).")] = None,
    source: Annotated[Optional[list[str]], typer.Option(
        help="Report only these sources (repeatable).")] = None,
    with_weather: Annotated[bool, typer.Option("--with-weather",
        help="Include the opt-in ERA5 weather archive row.")] = False,
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
) -> None:
    """What history is actually stored, per source per season (item 3.2c).

    The question ``ingest status`` structurally cannot answer: that report is
    per-source for ONE season and never lists the backfill-only sources.
    """
    resolved_last = last if last is not None else nfl_season_of(_today()) - 1
    conn = open_db(path)
    try:
        specs = select_backfill_sources(names=source or None, with_weather=with_weather)
        rows = backfill_coverage(conn, first=first, last=resolved_last, sources=specs)
    except (BackfillRefused, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        conn.close()
    typer.echo(format_coverage(rows))


@app.command()
def smoke() -> None:
    """Exercise the three spine abstractions end to end (dev sanity check)."""
    as_of = normalize_as_of("2026-09-10")
    typer.echo(f"as_of   : normalize_as_of('2026-09-10') -> {as_of}")

    line = {"rushing_yards": 80, "rushing_tds": 1, "receptions": 5, "receiving_yards": 42}
    typer.echo(f"scoring : RB {line} -> {score('RB', line):.1f} pts (house rules)")

    resp = Router.from_toml().complete("smoke_test", "hello from ziggurat")
    typer.echo(f"router  : task 'smoke_test' -> backend '{resp.backend}' -> {resp.text!r}")
