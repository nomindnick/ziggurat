"""CLI smoke tests — thin commands wired to package functions, nothing more."""

import shutil

from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.paths import INTEL_TEMPLATES_DIR

runner = CliRunner()


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


def test_smoke_exercises_the_three_spines():
    result = runner.invoke(app, ["smoke"])
    assert result.exit_code == 0
    assert "as_of" in result.output
    # Scoring is the real league settings as of item 1.3 (no longer placeholder);
    # the RB line prices through the house rules: 80*0.1 + 6 + 5 + 42*0.1 = 23.2.
    assert "23.2 pts (house rules)" in result.output
    assert "echo" in result.output
