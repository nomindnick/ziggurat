"""`ziggurat` command-line entry point.

Standing rule: no logic in the CLI layer. Every command is a thin wrapper that
parses arguments, calls a package function, and prints the result.
"""

from pathlib import Path
from typing import Annotated

import typer

from ziggurat.core.scoring import score
from ziggurat.data.asof import normalize_as_of
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
def smoke() -> None:
    """Exercise the three spine abstractions end to end (dev sanity check)."""
    as_of = normalize_as_of("2026-09-10")
    typer.echo(f"as_of   : normalize_as_of('2026-09-10') -> {as_of}")

    line = {"rushing_yards": 80, "rushing_tds": 1, "receptions": 5, "receiving_yards": 42}
    typer.echo(f"scoring : RB {line} -> {score('RB', line):.1f} pts (PLACEHOLDER rules)")

    resp = Router.from_toml().complete("smoke_test", "hello from ziggurat")
    typer.echo(f"router  : task 'smoke_test' -> backend '{resp.backend}' -> {resp.text!r}")
