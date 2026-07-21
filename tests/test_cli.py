"""CLI smoke tests — thin commands wired to package functions, nothing more."""

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.data.nfl import espn_source, projections
from ziggurat.data.store import apply_schema, connect
from ziggurat.paths import INTEL_TEMPLATES_DIR

runner = CliRunner()

_VAL_FIXTURE = Path(__file__).parent / "fixtures" / "nfl" / "valuation_projections_sample.json"
_ESPN_FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "player_universe.json"


def _build_valuation_db(db_path):
    """A temp facts DB with projections + players so the valuation CLI has data.

    QB gsis '00-QB' is given the ESPN fixture's Josh Allen id (3918298) so the
    --espn value view produces at least one matched skill join.
    """
    conn = connect(db_path)
    apply_schema(conn)
    players = [
        ("00-QB", "100", "3918298", "Test QB"),
        ("00-R1", "201", "e201", "Test RB1"),
        ("00-WR", "301", "e301", "Test WR"),
    ]
    for gsis, sleeper, espn_id, name in players:
        conn.execute(
            "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, retrieved_as_of, knowable_as_of) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (gsis, sleeper, espn_id, name, "2026-07-01", "2026-07-01"),
        )
    conn.commit()
    projections.ingest_projections(conn, json.loads(_VAL_FIXTURE.read_text()),
                                   retrieved_as_of="2026-08-01")
    conn.close()


def test_help_runs():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Ziggurat" in result.output


def test_db_init_creates_schema(tmp_path):
    db_path = tmp_path / "nested" / "test.sqlite"
    result = runner.invoke(app, ["db", "init", "--path", str(db_path)])
    assert result.exit_code == 0
    assert db_path.exists()


def test_intel_init_scaffolds_from_templates(tmp_path):
    shutil.copytree(INTEL_TEMPLATES_DIR, tmp_path / "templates" / "intel")
    result = runner.invoke(app, ["intel", "init", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "intel" / "heuristics.md").exists()
    rerun = runner.invoke(app, ["intel", "init", "--root", str(tmp_path)])
    assert "nothing created" in rerun.output


def test_valuation_prints_board(tmp_path):
    db_path = tmp_path / "val.sqlite"
    _build_valuation_db(db_path)
    result = runner.invoke(
        app,
        ["valuation", "--as-of", "2026-08-01", "--season", "2026", "--path", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    # the fixed-width VOR table header prints
    assert "player" in result.output and "vor" in result.output
    assert "Test QB" in result.output


def test_valuation_position_and_top_filter(tmp_path):
    db_path = tmp_path / "val.sqlite"
    _build_valuation_db(db_path)
    result = runner.invoke(
        app,
        ["valuation", "--as-of", "2026-08-01", "--season", "2026",
         "--path", str(db_path), "--position", "QB", "--top", "5"],
    )
    assert result.exit_code == 0, result.output
    assert "Test QB" in result.output
    # a WR must not appear once filtered to QB
    assert "Test WR" not in result.output


def test_valuation_espn_value_view(tmp_path, monkeypatch):
    db_path = tmp_path / "val.sqlite"
    _build_valuation_db(db_path)
    monkeypatch.setenv("SWID", "{TEST-SWID}")
    monkeypatch.setenv("ESPN_S2", "test-s2-cookie")
    raw_players = json.loads(_ESPN_FIXTURE.read_text())
    with patch.object(espn_source, "fetch_player_universe", return_value=raw_players) as fetch:
        result = runner.invoke(
            app,
            ["valuation", "--as-of", "2026-08-01", "--season", "2026",
             "--path", str(db_path), "--espn", "--league-id", "123456"],
        )
    assert result.exit_code == 0, result.output
    fetch.assert_called_once()
    # the value-view table (with the flag column) prints, not the plain board
    assert "flag" in result.output and "delta" in result.output


def test_smoke_exercises_the_three_spines():
    result = runner.invoke(app, ["smoke"])
    assert result.exit_code == 0
    assert "as_of" in result.output
    # Scoring is the real league settings as of item 1.3 (no longer placeholder);
    # the RB line prices through the house rules: 80*0.1 + 6 + 5 + 42*0.1 = 23.2.
    assert "23.2 pts (house rules)" in result.output
    assert "echo" in result.output
