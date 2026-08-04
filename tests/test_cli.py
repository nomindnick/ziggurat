"""CLI smoke tests — thin commands wired to package functions, nothing more."""

import json
import shutil
from datetime import date
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ziggurat.cli import main as main_module
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


def _build_signals_db(db_path):
    """A temp facts DB seeded from the 2023 wk5-6 nflverse slice, retrieved in the
    PAST so the CLI's default historical view (the live path) actually sees it."""
    import pandas as pd

    from ziggurat.data.nfl import injuries, players, schedules, snap_counts, weekly_stats

    fx = Path(__file__).parent / "fixtures" / "nfl"

    def load(name):
        return pd.read_parquet(fx / f"{name}.parquet")

    conn = connect(db_path)
    apply_schema(conn)
    players.ingest_players(conn, load("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(conn, load("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(conn, load("weekly_stats"), retrieved_as_of="2023-10-17")
    snap_counts.ingest_snap_counts(conn, load("snap_counts"), retrieved_as_of="2023-10-17")
    injuries.ingest_injuries(conn, load("injuries"), retrieved_as_of="2023-10-17")
    conn.close()


def test_candidates_prints_board(tmp_path):
    db_path = tmp_path / "signals.sqlite"
    _build_signals_db(db_path)
    result = runner.invoke(
        app,
        ["candidates", "--as-of", "2023-10-20", "--season", "2023", "--week", "6",
         "--path", str(db_path), "--reasons"],
    )
    assert result.exit_code == 0, result.output
    assert "USAGE BREAKOUTS" in result.output
    assert "INJURY SHOCKS" in result.output
    # Rule 6: lead-time disclosure travels with the injury block
    assert "lead-time reality" in result.output
    # F11: the ranking-key legend is present and the misleading SCORE header is gone
    assert "SIGNAL" in result.output and "NOT fantasy points" in result.output


def _build_signals_db_future_stamp(db_path):
    """Like _build_signals_db but retrieved in the FUTURE (production backfill), so
    a past-season historical read sees NOTHING and only --validate surfaces rows."""
    import pandas as pd

    from ziggurat.data.nfl import injuries, players, schedules, snap_counts, weekly_stats

    fx = Path(__file__).parent / "fixtures" / "nfl"

    def load(name):
        return pd.read_parquet(fx / f"{name}.parquet")

    conn = connect(db_path)
    apply_schema(conn)
    stamp = "2026-07-16"  # retrieved in the FUTURE, as a real backfill is
    players.ingest_players(conn, load("ids"), retrieved_as_of=stamp)
    schedules.ingest_schedules(conn, load("schedules"), retrieved_as_of=stamp)
    weekly_stats.ingest_weekly_stats(conn, load("weekly_stats"), retrieved_as_of=stamp)
    snap_counts.ingest_snap_counts(conn, load("snap_counts"), retrieved_as_of=stamp)
    injuries.ingest_injuries(conn, load("injuries"), retrieved_as_of=stamp)
    conn.close()


def test_candidates_validate_flag_surfaces_past_season_rows(tmp_path):
    # F4: a production-stamped (future-retrieved) DB reads EMPTY under the default
    # historical view for a past season; --validate binds latest_truth and surfaces
    # the real candidates the guidance note points at.
    db_path = tmp_path / "signals.sqlite"
    _build_signals_db_future_stamp(db_path)
    common = ["candidates", "--as-of", "2023-10-20", "--season", "2023", "--week", "6",
              "--path", str(db_path)]

    historical = runner.invoke(app, common)
    assert historical.exit_code == 0, historical.output
    assert "latest_truth" in historical.output or "--validate" in historical.output

    validated = runner.invoke(app, [*common, "--validate"])
    assert validated.exit_code == 0, validated.output
    assert "USAGE BREAKOUTS" in validated.output
    # the validated board actually has rows (a non-empty USAGE block)
    assert "(0)" not in validated.output.split("INJURY SHOCKS")[0]


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
    # Pin the CLI's "today" to the fixture date: `ensure_board` only live-pulls
    # when as_of >= today, and a hard-coded as_of silently becomes "the past"
    # once the wall clock passes it (this test broke exactly that way).
    monkeypatch.setattr(main_module, "_today", lambda: "2026-08-01")
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


def test_mock_draft_runs_with_a_monkeypatched_loader(tmp_path, monkeypatch):
    """The mock-draft command parses, loads a board (patched), runs, and prints.

    Patches the DB-edge loader so no facts DB is required — exercises the thin
    CLI wiring around the deletable draft package.
    """
    from ziggurat.draft import simulator
    from ziggurat.draft.bots import BoardEntry

    def _fake_board():
        specs = {"QB": 32, "RB": 80, "WR": 90, "TE": 32, "DST": 32, "K": 32}
        board, rank = [], 1
        for pos, count in specs.items():
            for i in range(count):
                board.append(BoardEntry(f"{pos}-{i}", f"{pos}{i}", pos, rank, 200 - i, 100 - i))
                rank += 1
        return tuple(board)

    called = {}

    def fake_load_board(conn, *, as_of, season, source="sleeper_rotowire", weeks=None):
        called["as_of"] = as_of
        called["season"] = season
        return _fake_board()

    monkeypatch.setattr(simulator, "load_board", fake_load_board)
    db_path = tmp_path / "mock.sqlite"
    result = runner.invoke(
        app,
        ["mock-draft", "--season", "2026", "--as-of", "2026-08-15", "--path", str(db_path),
         "--n", "5", "--slot", "3", "--strategy", "vor", "--seed", "1"],
    )
    assert result.exit_code == 0, result.output
    assert called == {"as_of": "2026-08-15", "season": 2026}
    assert "starting-lineup points" in result.output
    assert "draft slot 3" in result.output


def test_mock_draft_engine_strategy_runs(tmp_path, monkeypatch):
    """The 'engine' arm wires the item-2.3 PickEngine through the same thin command.

    Uses a small board + n=1 so the real survival rollout stays fast; asserts the
    command parses, runs the engine in the operator seat, and prints the summary.
    """
    from ziggurat.draft import simulator
    from ziggurat.draft.bots import BoardEntry

    def _fake_board():
        specs = {"QB": 24, "RB": 60, "WR": 64, "TE": 24, "DST": 16, "K": 16}
        board, rank = [], 1
        for pos, count in specs.items():
            for i in range(count):
                board.append(BoardEntry(f"{pos}-{i}", f"{pos}{i}", pos, rank, 200 - i, 100 - i))
                rank += 1
        return tuple(board)

    monkeypatch.setattr(simulator, "load_board", lambda conn, **kw: _fake_board())
    db_path = tmp_path / "mock.sqlite"
    result = runner.invoke(
        app,
        ["mock-draft", "--season", "2026", "--as-of", "2026-08-15", "--path", str(db_path),
         "--n", "1", "--slot", "5", "--strategy", "engine", "--seed", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "pick-engine" in result.output
    assert "draft slot 5" in result.output


def test_mock_draft_rejects_unknown_strategy(tmp_path):
    db_path = tmp_path / "mock.sqlite"
    result = runner.invoke(
        app, ["mock-draft", "--season", "2026", "--path", str(db_path), "--strategy", "bogus"]
    )
    assert result.exit_code != 0


def test_mock_draft_rejects_out_of_range_slot(tmp_path):
    # A fumbled --slot gets a legible message at the parse edge (Rule 6), never a
    # traceback from deep inside run_many.
    db_path = tmp_path / "mock.sqlite"
    for slot in ("0", "11"):
        result = runner.invoke(
            app, ["mock-draft", "--season", "2026", "--path", str(db_path), "--slot", slot]
        )
        assert result.exit_code != 0
        assert "slot must be 1..10" in result.output


def test_smoke_exercises_the_three_spines():
    result = runner.invoke(app, ["smoke"])
    assert result.exit_code == 0
    assert "as_of" in result.output
    # Scoring is the real league settings as of item 1.3 (no longer placeholder);
    # the RB line prices through the house rules: 80*0.1 + 6 + 5 + 42*0.1 = 23.2.
    assert "23.2 pts (house rules)" in result.output
    assert "echo" in result.output


def test_valuation_espn_reads_a_past_board_instead_of_overwriting_it(tmp_path, monkeypatch):
    """`valuation --espn --as-of <past day>` used to live-pull and back-stamp,
    DELETING that day's stored board (perishable — ESPN serves no history) and
    manufacturing a retrieval-time leak. It now reads what was stored."""
    db_path = tmp_path / "val.sqlite"
    _build_valuation_db(db_path)
    monkeypatch.setenv("SWID", "{TEST-SWID}")
    monkeypatch.setenv("ESPN_S2", "test-s2-cookie")
    with patch.object(espn_source, "fetch_player_universe") as fetch:
        result = runner.invoke(
            app,
            ["valuation", "--as-of", "2020-08-01", "--season", "2026",
             "--path", str(db_path), "--espn", "--league-id", "123456"],
        )
    assert result.exit_code == 0, result.output
    fetch.assert_not_called()


def test_valuation_espn_reports_a_board_collapse_legibly(tmp_path, monkeypatch):
    """A draft-adjacent command must not die in a traceback when the new floor
    refuses a degraded board."""
    from ziggurat.cli import main
    from ziggurat.data.nfl import espn_ranks

    db_path = tmp_path / "val.sqlite"
    _build_valuation_db(db_path)
    monkeypatch.setenv("SWID", "{TEST-SWID}")
    monkeypatch.setenv("ESPN_S2", "test-s2-cookie")
    # patch the CLI's OWN binding: `from ... import ensure_board` copied the name.
    with patch.object(main, "ensure_espn_board",
                      side_effect=espn_ranks.BoardCollapse("refusing to write an EMPTY board")):
        result = runner.invoke(
            app,
            ["valuation", "--as-of", date.today().isoformat(), "--season", "2026",
             "--path", str(db_path), "--espn", "--league-id", "123456"],
        )
    assert result.exit_code == 1
    assert "refusing to write an EMPTY board" in result.output
    assert "Traceback" not in result.output


def _build_marginal_db(db_path):
    """A temp facts DB with a projected universe and a drafted team 10 (item 3.2)."""
    conn = connect(db_path)
    apply_schema(conn)
    specs = [
        ("Cli Passer", "QB", "TEN", 20.0, 6, 10),
        ("Cli Runner", "RB", "ATL", 18.0, 11, 10),
        ("Cli Runner Two", "RB", "BUF", 12.0, 7, 10),
        ("Cli Catcher", "WR", "DAL", 17.0, 8, 10),
        ("Cli Catcher Two", "WR", "DEN", 15.0, 9, 10),
        ("Cli Tight", "TE", "IND", 10.0, 13, 10),
        ("Cli Kicker", "K", "KC", 8.0, 14, 10),
        ("Cli Defense", "D/ST", "MIA", 7.0, 5, 10),
        ("Cli Spare", "WR", "NYJ", 6.0, 12, None),
    ]
    for i, (name, pos, team, pts, bye, on_team) in enumerate(specs):
        is_dst = pos == "D/ST"
        gsis = None if is_dst else f"00-C{i:04d}"
        espn_id = str(-17000 - i) if is_dst else str(7000 + i)
        if not is_dst:
            conn.execute(
                "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, retrieved_as_of, "
                "knowable_as_of) VALUES (?, ?, ?, ?, '2026-09-15', '2026-09-15')",
                (gsis, f"C{i}", espn_id, name),
            )
        for week in range(1, 18):
            on_bye = week == bye
            if on_bye and is_dst:
                continue
            stat = "sacks" if is_dst else ("pat_made" if pos == "K" else "rushing_yards")
            value = None if on_bye else (pts * 10.0 if stat == "rushing_yards" else pts)
            conn.execute(
                f"INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
                f"season_type, position, team, opponent, {stat}, retrieved_as_of, "
                f"knowable_as_of) VALUES ('sleeper_rotowire', ?, ?, 2026, ?, 'regular', ?, "
                f"?, ?, ?, '2026-09-15', '2026-09-15')",
                (f"C{i}", gsis, week, "DEF" if is_dst else pos, team,
                 None if on_bye else "OPP", value),
            )
        conn.execute(
            "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, "
            "position, pro_team, on_team_id, roster_status, lineup_slot, injury_status, "
            "percent_owned, scoring_period, retrieved_as_of, knowable_as_of) VALUES "
            "(2026, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 20.0, 4, '2026-09-15', '2026-09-15')",
            (espn_id, gsis, name, pos, team, on_team,
             "ONTEAM" if on_team else "FREEAGENT", "BE" if on_team else None),
        )
    conn.commit()
    conn.close()


def test_marginal_prints_a_drop_board_with_its_caveats(tmp_path):
    db_path = tmp_path / "marginal.sqlite"
    _build_marginal_db(db_path)
    result = runner.invoke(app, ["marginal", "--path", str(db_path), "--as-of",
                                 "2026-09-15", "--season", "2026", "--team", "10",
                                 "--from-week", "4"])
    assert result.exit_code == 0, result.output
    assert "drop board" in result.output
    # Rule 6: the static-roster assumption is printed above every board, and the
    # staleness banner says which pull the numbers came from.
    assert "NO OTHER MOVES ALL SEASON" in result.output
    assert "projections: pulled 2026-09-15" in result.output
    assert "Cli Runner" in result.output


def test_waivers_prints_a_plan_with_legality_and_sections(tmp_path):
    db_path = tmp_path / "marginal.sqlite"
    _build_marginal_db(db_path)
    result = runner.invoke(app, ["waivers", "--path", str(db_path), "--as-of",
                                 "2026-09-15", "--season", "2026", "--team", "10",
                                 "--from-week", "4"])
    assert result.exit_code == 0, result.output
    # a legal roster (8 active) plans claims; the legality verdict prints first
    assert "roster legal" in result.output
    assert "WAIVER CLAIMS" in result.output
    assert "DROP BOARD" in result.output
    assert "Traceback" not in result.output


def test_marginal_refuses_to_guess_the_current_week(tmp_path):
    """`--from-week` omitted, with no scoring period and no readable schedules:
    the command must fail legibly rather than pricing a whole season."""
    db_path = tmp_path / "marginal.sqlite"
    _build_marginal_db(db_path)
    conn = connect(db_path)
    conn.execute("UPDATE league_player_state SET scoring_period = 0")
    conn.commit()
    conn.close()
    result = runner.invoke(app, ["marginal", "--path", str(db_path), "--as-of",
                                 "2026-09-15", "--season", "2026", "--team", "10"])
    assert result.exit_code == 1
    assert "--from-week" in result.output


def test_marginal_prunes_the_pool_by_default_and_says_so(tmp_path):
    """``--pool-limit`` defaulted to None and was passed straight through, so
    ``DEFAULT_POOL_LIMIT`` was unreachable from the only user-facing entry point,
    ``--pool-limit 0`` meant what the default already meant, and the disclosure
    note about a narrowed scan never appeared."""
    from ziggurat.core.marginal import DEFAULT_POOL_LIMIT

    db_path = tmp_path / "marginal.sqlite"
    _build_marginal_db(db_path)
    seen = {}
    real = main_module.build_board

    def spy(*args, **kwargs):
        seen["pool_limit"] = kwargs.get("pool_limit")
        seen["last_week"] = kwargs.get("last_week")
        return real(*args, **kwargs)

    with patch.object(main_module, "build_board", spy):
        runner.invoke(app, ["marginal", "--path", str(db_path), "--as-of",
                            "2026-09-15", "--season", "2026", "--team", "10",
                            "--from-week", "4"])
        assert seen["pool_limit"] == DEFAULT_POOL_LIMIT
        runner.invoke(app, ["marginal", "--path", str(db_path), "--as-of",
                            "2026-09-15", "--season", "2026", "--team", "10",
                            "--from-week", "4", "--pool-limit", "0"])
        assert seen["pool_limit"] is None


def test_marginal_last_week_is_not_a_silent_no_op(tmp_path):
    """``--last-week`` was consumed only inside the ``--from-week`` branch and
    ``build_board`` had no parameter to forward it to, so
    ``ziggurat marginal --last-week 14`` priced through week 17 with no error and
    no hint the flag had been dropped."""
    db_path = tmp_path / "marginal.sqlite"
    _build_marginal_db(db_path)
    conn = connect(db_path)
    conn.execute("UPDATE league_player_state SET scoring_period = 4")
    conn.commit()
    conn.close()
    result = runner.invoke(app, ["marginal", "--path", str(db_path), "--as-of",
                                 "2026-09-15", "--season", "2026", "--team", "10",
                                 "--last-week", "14"])
    assert result.exit_code == 0, result.output
    assert "weeks 4-14" in result.output
