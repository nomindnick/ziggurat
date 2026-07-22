"""`ziggurat` command-line entry point.

Standing rule: no logic in the CLI layer. Every command is a thin wrapper that
parses arguments, calls a package function, and prints the result.
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

import typer

from ziggurat.core.divergence import build_divergence, format_report
from ziggurat.core.scoring import score
from ziggurat.core.valuation import (
    _canon_position,
    build_valuation,
    build_value_view,
    format_valuation,
    format_value_view,
)
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl.adp_rankings import get_adp_rankings
from ziggurat.data.nfl.espn_ranks import get_espn_draft_ranks, pull_espn_ranks
from ziggurat.data.nfl.espn_source import load_espn_credentials
from ziggurat.data.store import apply_schema, connect
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


@db_app.command("init")
def db_init(
    path: Annotated[Path, typer.Option(help="SQLite file to create/upgrade.")] = DEFAULT_DB_PATH,
) -> None:
    """Create the facts database and apply db/schema.sql (idempotent)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    apply_schema(conn)
    conn.close()
    typer.echo(f"schema applied: {path}")


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


@app.command()
def valuation(
    as_of: Annotated[str, typer.Option(help="Knowledge-time cutoff (YYYY-MM-DD).")],
    season: Annotated[int, typer.Option(help="Season to value.")],
    path: Annotated[Path, typer.Option(help="SQLite facts database.")] = DEFAULT_DB_PATH,
    position: Annotated[Optional[str], typer.Option(help="Filter to one position (QB/RB/…/DST/K).")] = None,
    top: Annotated[Optional[int], typer.Option(help="Show only the top N rows.")] = None,
    weeks: Annotated[Optional[str], typer.Option(help="Regular-season week window, e.g. '1-17'.")] = None,
    source: Annotated[str, typer.Option(help="Projection source.")] = "sleeper_rotowire",
    espn: Annotated[bool, typer.Option("--espn", help="Live-pull the ESPN board and print the value view.")] = False,
    league_id: Annotated[Optional[int], typer.Option(help="ESPN leagueId (else ESPN_LEAGUE_ID env).")] = None,
) -> None:
    """Print the global static VOR valuation board (item 2.1).

    Default: the house VOR board. With ``--espn``, live-pull the ESPN default
    board and print the "what the room can't see" value view instead. All
    computation lives in ``core/valuation.py`` / ``data/nfl/espn_ranks.py``; this
    command only parses, calls, and prints (rule 3).
    """
    canon = _canon_position(position) if position is not None else None
    conn = connect(path)
    rows = build_valuation(
        conn, as_of=as_of, season=season, weeks=_parse_weeks(weeks), source=source
    )
    if canon is not None:
        rows = [r for r in rows if r.position == canon]

    if espn:
        creds = load_espn_credentials(league_id=league_id)
        pull_espn_ranks(conn, season=season, retrieved_as_of=as_of, **creds)
        espn_rows = get_espn_draft_ranks(conn, as_of=as_of, season=season)
        view = build_value_view(rows, espn_rows)
        conn.close()
        typer.echo(format_value_view(view, top=top))
    else:
        conn.close()
        typer.echo(format_valuation(rows, top=top))


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
    from ziggurat.draft.session import find_latest_journal, read_journal_header
    from ziggurat.draft.simulator import DEFAULT_ROSTER, load_board

    if not 1 <= slot <= DEFAULT_ROSTER.teams:
        raise typer.BadParameter(f"slot must be 1..{DEFAULT_ROSTER.teams}")

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


@app.command()
def smoke() -> None:
    """Exercise the three spine abstractions end to end (dev sanity check)."""
    as_of = normalize_as_of("2026-09-10")
    typer.echo(f"as_of   : normalize_as_of('2026-09-10') -> {as_of}")

    line = {"rushing_yards": 80, "rushing_tds": 1, "receptions": 5, "receiving_yards": 42}
    typer.echo(f"scoring : RB {line} -> {score('RB', line):.1f} pts (house rules)")

    resp = Router.from_toml().complete("smoke_test", "hello from ziggurat")
    typer.echo(f"router  : task 'smoke_test' -> backend '{resp.backend}' -> {resp.text!r}")
