"""League sync orchestration + network seam + CLI (item 3.1).

Fully offline: every test patches the four ``ziggurat.league.source.fetch_*``
seams. The behaviours under test are the ones that protect PERISHABLE history —
ESPN serves no historical league state, so a lost or half-written snapshot day
can never be recovered:

  * an optional-part failure must not cost the snapshot,
  * a snapshot failure must be recorded AND raised (so a cron exits nonzero),
  * a truncated or auth-rejected pull must fail loud rather than write a
    plausible-but-wrong day.
"""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.data.store import apply_schema, connect
from ziggurat.league import source, state, sync

runner = CliRunner()

CREDS = {"league_id": 42, "espn_s2": "s2", "swid": "{swid}"}


def _patched_sources(payload, pool, transactions=(), activity=()):
    """Patch all four network seams for one offline run."""
    return (
        patch.object(source, "fetch_league_state", return_value=payload),
        patch.object(source, "fetch_player_pool", return_value=pool),
        patch.object(source, "fetch_transactions", return_value=list(transactions)),
        patch.object(source, "fetch_activity", return_value=list(activity)),
    )


def _run(db, payload, pool, *, day="2026-09-10", **kwargs):
    patches = _patched_sources(payload, pool, **kwargs)
    for p in patches:
        p.start()
    try:
        return sync.run_sync(db, season=2026, retrieved_as_of=day, **CREDS)
    finally:
        for p in patches:
            p.stop()


# ------------------------------------------------------------------- happy path


def test_run_sync_writes_every_table_and_logs_ok(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1000": 4, "1001": 4})
    summary = _run(crosswalked_db, payload, pool)

    assert summary["status"] == "ok"
    assert summary["teams"] == 10
    assert summary["matchups"] == 70
    assert summary["players"] == len(pool)
    assert summary["rostered"] == 2
    assert summary["conflicts"] == 0
    assert summary["scoring_period"] == 3

    run = state.last_run(crosswalked_db, season=2026)
    assert run["status"] == "ok" and run["finished_at"] and run["error"] is None
    assert run["players"] == len(pool)
    # the two done-when questions, answered from the stored snapshot
    assert state.who_held(crosswalked_db, as_of="2026-09-10", season=2026,
                          espn_player_id="1000") == 4
    assert len(state.get_free_agents(crosswalked_db, as_of="2026-09-10", season=2026)) == len(pool) - 2


def test_run_sync_is_idempotent_for_the_day(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1000": 4})
    _run(crosswalked_db, payload, pool)
    _run(crosswalked_db, payload, pool)
    rows = crosswalked_db.execute(
        "SELECT COUNT(*) FROM league_player_state WHERE retrieved_as_of = '2026-09-10'"
    ).fetchone()[0]
    assert rows == len(pool)
    teams = crosswalked_db.execute("SELECT COUNT(*) FROM league_teams").fetchone()[0]
    assert teams == 10


def test_transactions_flow_through_to_the_event_log(crosswalked_db, league_world):
    payload, pool = league_world()
    txn = {"id": "TX1", "teamId": 4, "type": "WAIVER", "status": "EXECUTED",
           "scoringPeriodId": 2, "proposedDate": 1788000000000, "processDate": 1788086400000,
           "items": [{"type": "ADD", "playerId": 1001}]}
    summary = _run(crosswalked_db, payload, pool, transactions=[txn])
    assert summary["transactions"] == 1
    stored = state.get_transactions(crosswalked_db, as_of="2026-09-10", season=2026)
    assert len(stored) == 1 and stored[0]["action"] == "ADD"


# ------------------------------------------------------- failure containment


def test_transaction_failure_downgrades_to_partial_but_keeps_the_snapshot(
    crosswalked_db, league_world
):
    payload, pool = league_world(holdings={"1000": 4})
    patches = [
        patch.object(source, "fetch_league_state", return_value=payload),
        patch.object(source, "fetch_player_pool", return_value=pool),
        patch.object(source, "fetch_transactions", side_effect=RuntimeError("ESPN 500")),
        patch.object(source, "fetch_activity", return_value=[]),
    ]
    for p in patches:
        p.start()
    try:
        summary = sync.run_sync(crosswalked_db, season=2026, retrieved_as_of="2026-09-10", **CREDS)
    finally:
        for p in patches:
            p.stop()

    assert summary["status"] == "partial"
    assert summary["players"] == len(pool)          # the perishable part survived
    run = state.last_run(crosswalked_db, season=2026, status=None)
    assert run["status"] == "partial" and "ESPN 500" in run["error"]
    assert state.who_held(crosswalked_db, as_of="2026-09-10", season=2026,
                          espn_player_id="1000") == 4


def test_snapshot_failure_is_recorded_and_raised(crosswalked_db):
    """A cron must exit nonzero AND leave evidence."""
    with patch.object(source, "fetch_league_state",
                      side_effect=RuntimeError("ESPN rejected the league-state request")):
        with pytest.raises(RuntimeError, match="ESPN rejected"):
            sync.run_sync(crosswalked_db, season=2026, retrieved_as_of="2026-09-10", **CREDS)

    run = state.last_run(crosswalked_db, season=2026, status=None)
    assert run["status"] == "failed"
    assert "ESPN rejected" in run["error"]
    assert state.last_run(crosswalked_db, season=2026, status="ok") is None


def test_pool_failure_does_not_write_a_partial_day(crosswalked_db, league_world):
    """Half a snapshot is worse than none: it would read as mass free agency."""
    payload, _ = league_world(holdings={"1000": 4})
    with patch.object(source, "fetch_league_state", return_value=payload), \
         patch.object(source, "fetch_player_pool", side_effect=RuntimeError("truncated")):
        with pytest.raises(RuntimeError):
            sync.run_sync(crosswalked_db, season=2026, retrieved_as_of="2026-09-10", **CREDS)

    assert crosswalked_db.execute(
        "SELECT COUNT(*) FROM league_player_state").fetchone()[0] == 0


# --------------------------------------------------------------- network seam


class _FakeLeague:
    """Stands in for espn_api's League; records the request it was asked to make."""

    def __init__(self, response):
        self._response = response
        self.calls = []

        outer = self

        class _Req:
            def league_get(self, params=None, headers=None, extend=None):
                outer.calls.append({"params": params, "headers": headers, "extend": extend})
                return outer._response

        self.espn_request = _Req()


def test_fetch_league_state_rejects_a_payload_without_teams():
    with patch.object(source, "_client", return_value=_FakeLeague({"status": {}})):
        with pytest.raises(RuntimeError, match="carries no teams"):
            source.fetch_league_state(league_id=1, season=2026, espn_s2="x", swid="y")


def test_fetch_league_state_accepts_the_history_array_form(league_world):
    payload, _ = league_world()
    with patch.object(source, "_client", return_value=_FakeLeague([payload])):
        out = source.fetch_league_state(league_id=1, season=2026, espn_s2="x", swid="y")
    assert out["scoringPeriodId"] == 3


def test_fetch_player_pool_fails_loud_on_truncation():
    entries = [{"id": i, "player": {"id": i}} for i in range(10)]
    with patch.object(source, "_client", return_value=_FakeLeague({"players": entries})):
        with pytest.raises(RuntimeError, match="truncated"):
            source.fetch_player_pool(league_id=1, season=2026, espn_s2="x", swid="y", limit=10)


def test_fetch_player_pool_passes_the_scoring_period_and_filter():
    fake = _FakeLeague({"players": []})
    with patch.object(source, "_client", return_value=fake):
        source.fetch_player_pool(league_id=1, season=2026, espn_s2="x", swid="y",
                                 scoring_period=7)
    call = fake.calls[0]
    assert call["params"]["scoringPeriodId"] == 7
    assert "FREEAGENT" in call["headers"]["x-fantasy-filter"]


def test_empty_transaction_feed_is_not_an_error():
    """ESPN serves this empty today and served it empty all of 2025."""
    with patch.object(source, "_client", return_value=_FakeLeague({"status": {}})):
        assert source.fetch_transactions(league_id=1, season=2026, espn_s2="x", swid="y") == []
    with patch.object(source, "_client", return_value=_FakeLeague({"topics": []})):
        assert source.fetch_activity(league_id=1, season=2026, espn_s2="x", swid="y") == []


def test_auth_failure_is_re_raised_with_a_refresh_hint():
    from espn_api.requests.espn_requests import ESPNAccessDenied

    class _Denied(_FakeLeague):
        def __init__(self):
            super().__init__(None)

            class _Req:
                def league_get(self, params=None, headers=None, extend=None):
                    raise ESPNAccessDenied("denied")

            self.espn_request = _Req()

    with patch.object(source, "_client", return_value=_Denied()):
        with pytest.raises(RuntimeError, match="refresh SWID/ESPN_S2"):
            source.fetch_league_state(league_id=1, season=2026, espn_s2="x", swid="y")


# ------------------------------------------------------------------- reporting


def test_format_run_and_status(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1000": 4})
    summary = _run(crosswalked_db, payload, pool, day="2026-09-08")
    assert "[ok]" in sync.format_run(summary) and "players=" in sync.format_run(summary)

    _run(crosswalked_db, payload, pool, day="2026-09-11")
    report = sync.format_status(crosswalked_db, season=2026, through="2026-09-12")
    assert "last success" in report
    assert "MISSING DAYS : 3" in report          # 09-09, 09-10, 09-12
    assert "unrecoverable" in report


def test_format_status_when_never_run(db):
    report = sync.format_status(db, season=2026, through="2026-09-12")
    assert "NEVER RUN" in report


# ------------------------------------------------------------------------ CLI


def test_cli_sync_status_and_reads(tmp_path, league_world):
    db_path = tmp_path / "z.sqlite"
    conn = connect(db_path)
    apply_schema(conn)
    conn.executemany(
        "INSERT INTO players (gsis_id, espn_id, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?)",
        [(f"00-00{i:04d}", str(1000 + i), "2026-07-01", "2026-07-01") for i in range(60)],
    )
    conn.commit()
    conn.close()

    payload, pool = league_world(holdings={"1000": 4}, slots={"1000": 0})
    # No --as-of: the scheduled path stamps TODAY, which is the only stamp the
    # sync accepts without --allow-backfill.
    today = date.today().isoformat()
    patches = list(_patched_sources(payload, pool))
    patches.append(patch("ziggurat.cli.main.load_espn_credentials", return_value=CREDS))
    for p in patches:
        p.start()
    try:
        result = runner.invoke(app, ["league", "sync", "--season", "2026",
                                     "--path", str(db_path)])
        assert result.exit_code == 0, result.output
        assert "[ok]" in result.output

        status = runner.invoke(app, ["league", "status", "--season", "2026",
                                     "--through", today, "--path", str(db_path)])
        assert status.exit_code == 0 and "MISSING DAYS : none" in status.output
        assert "BACK-STAMPED" not in status.output

        roster = runner.invoke(app, ["league", "roster", "--team", "4", "--season", "2026",
                                     "--as-of", today, "--path", str(db_path)])
        assert roster.exit_code == 0 and "QB" in roster.output

        fa = runner.invoke(app, ["league", "free-agents", "--season", "2026",
                                 "--as-of", today, "--limit", "5", "--path", str(db_path)])
        assert fa.exit_code == 0 and "%OWN" in fa.output

        holdings = runner.invoke(app, ["league", "holdings", "--player-id", "1000",
                                       "--season", "2026", "--path", str(db_path)])
        assert holdings.exit_code == 0 and "team 4" in holdings.output
    finally:
        for p in patches:
            p.stop()


def test_cli_sync_surfaces_a_failed_run(tmp_path):
    db_path = tmp_path / "z.sqlite"
    with patch("ziggurat.cli.main.load_espn_credentials", return_value=CREDS), \
         patch.object(source, "fetch_league_state", side_effect=RuntimeError("cookies expired")):
        result = runner.invoke(app, ["league", "sync", "--season", "2026",
                                     "--path", str(db_path)])
    assert result.exit_code != 0  # a cron must see a nonzero exit

    conn = connect(db_path)
    assert state.last_run(conn, season=2026, status=None)["status"] == "failed"
    conn.close()


def test_repo_boundary_fixture_stays_public():
    """The committed pool fixture must never gain roster/manager context (rule 5)."""
    import json

    fixture = Path(__file__).parent / "fixtures" / "espn" / "league_player_pool.json"
    entries = json.loads(fixture.read_text())
    # An explicit allowlist, not a substring scan: the fixture may carry public
    # player data and nothing else. Any new key must be added here deliberately.
    assert all(e.get("onTeamId") in (0, None) for e in entries)
    for entry in entries:
        assert set(entry) == {"id", "onTeamId", "status", "player"}
        assert set(entry["player"]) == {
            "id", "fullName", "defaultPositionId", "proTeamId", "injuryStatus", "ownership",
        }
        assert set(entry["player"]["ownership"]) == {
            "percentOwned", "percentStarted", "percentChange",
        }


# --------------------------------------- audit fixes: guards on the sync path


def test_backfill_is_refused_by_default(crosswalked_db, league_world):
    """ESPN serves only CURRENT state, so back-stamping fabricates a day rather
    than recovering it — and silently closes the gap report that exists to say
    the day is missing."""
    payload, pool = league_world()
    patches = _patched_sources(payload, pool)
    for p in patches:
        p.start()
    try:
        with pytest.raises(ValueError, match="fabricates history"):
            sync.run_sync(crosswalked_db, season=2026, retrieved_as_of="2026-09-01",
                          today="2026-09-10", **CREDS)
    finally:
        for p in patches:
            p.stop()
    assert crosswalked_db.execute("SELECT COUNT(*) FROM league_player_state").fetchone()[0] == 0


def test_forced_backfill_is_marked_in_the_status_report(crosswalked_db, league_world):
    payload, pool = league_world()
    patches = _patched_sources(payload, pool)
    for p in patches:
        p.start()
    try:
        summary = sync.run_sync(crosswalked_db, season=2026, retrieved_as_of="2026-09-01",
                                today="2026-09-10", allow_backfill=True, **CREDS)
    finally:
        for p in patches:
            p.stop()
    assert summary["status"] == "partial"
    report = sync.format_status(crosswalked_db, season=2026, through="2026-09-10")
    assert "BACK-STAMPED : 1 — 2026-09-01" in report
    assert "NOT point-in-time" in report


def test_degraded_pool_fails_the_run_and_keeps_the_stored_day(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1000": 4})
    _run(crosswalked_db, payload, pool, day="2026-09-10")

    patches = [
        patch.object(source, "fetch_league_state", return_value=payload),
        patch.object(source, "fetch_player_pool", return_value=[]),   # ESPN 200, empty
        patch.object(source, "fetch_transactions", return_value=[]),
        patch.object(source, "fetch_activity", return_value=[]),
    ]
    for p in patches:
        p.start()
    try:
        with pytest.raises(state.SnapshotCollapse):
            sync.run_sync(crosswalked_db, season=2026, retrieved_as_of="2026-09-10", **CREDS)
    finally:
        for p in patches:
            p.stop()

    run = state.last_run(crosswalked_db, season=2026, status=None)
    assert run["status"] == "failed" and "SnapshotCollapse" in run["error"]
    assert state.who_held(crosswalked_db, as_of="2026-09-10", season=2026,
                          espn_player_id="1000") == 4
    assert crosswalked_db.execute(
        "SELECT COUNT(*) FROM league_player_state WHERE retrieved_as_of='2026-09-10'"
    ).fetchone()[0] == len(pool)


def test_crosswalk_collapse_downgrades_to_partial_but_keeps_the_snapshot(db, league_world):
    """No players table: gsis_id is derived and backfillable, the snapshot is not."""
    payload, pool = league_world(holdings={"1000": 4})
    summary = _run(db, payload, pool)
    assert summary["status"] == "partial"
    assert summary["players"] == len(pool)
    run = state.last_run(db, season=2026, status=None)
    assert "crosswalk coverage" in run["error"]
    assert state.who_held(db, as_of="2026-09-10", season=2026, espn_player_id="1000") == 4


def test_empty_teams_payload_is_refused_at_the_seam():
    with patch.object(source, "_client", return_value=_FakeLeague({"teams": []})):
        with pytest.raises(RuntimeError, match="carries no teams"):
            source.fetch_league_state(league_id=1, season=2026, espn_s2="x", swid="y")


def test_requests_run_under_a_bounded_socket_timeout():
    """espn_api passes no timeout to requests; an unbounded hang would park the
    oneshot service forever and silently stop the cadence."""
    import socket

    seen = {}

    class _Recording(_FakeLeague):
        def __init__(self):
            super().__init__({"teams": [{"id": 1}]})

            class _Req:
                def league_get(self, params=None, headers=None, extend=None):
                    seen["timeout"] = socket.getdefaulttimeout()
                    return {"teams": [{"id": 1}]}

            self.espn_request = _Req()

    before = socket.getdefaulttimeout()
    with patch.object(source, "_client", return_value=_Recording()):
        source.fetch_league_state(league_id=1, season=2026, espn_s2="x", swid="y")
    assert seen["timeout"] == source._SOCKET_TIMEOUT
    assert socket.getdefaulttimeout() == before   # restored, not leaked


def test_read_commands_work_on_a_database_predating_migration_005(tmp_path):
    """CLAUDE.md tells the operator to run `league status` on the sync machine;
    on any pre-005 database it used to die with a raw sqlite traceback."""
    from ziggurat.data.store import connect
    from ziggurat.paths import MIGRATIONS_DIR, SCHEMA_PATH

    db_path = tmp_path / "old.sqlite"
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql"))[:3]:      # -> schema_version 4
        conn.executescript(migration.read_text())
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4')")
    conn.commit()
    conn.close()

    for argv in (
        ["league", "status", "--season", "2026", "--path", str(db_path)],
        ["league", "free-agents", "--season", "2026", "--as-of", "2026-09-10", "--path", str(db_path)],
        ["league", "roster", "--team", "1", "--season", "2026", "--as-of", "2026-09-10",
         "--path", str(db_path)],
        ["league", "holdings", "--player-id", "1000", "--season", "2026", "--path", str(db_path)],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, f"{argv} -> {result.output}"


def test_missing_database_file_does_not_traceback(tmp_path):
    result = runner.invoke(app, ["league", "status", "--season", "2026",
                                 "--path", str(tmp_path / "nope.sqlite")])
    assert result.exit_code == 0
    assert "NEVER RUN" in result.output
