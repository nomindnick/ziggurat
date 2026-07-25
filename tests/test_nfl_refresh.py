"""NFL refresh cadence + orchestration tests (item 3.1b).

Entirely OFFLINE: every source in these tests is a fake ``pull`` closure, so no
test here touches the network. That is not just hygiene — the whole point of the
orchestrator is what it does AROUND the pull (ordering, dependency refusal,
rollback, run logging, staleness), and a fake pull is the only way to exercise
the failure branches deterministically.

Mirrors tests/test_league_sync.py: an in-memory ``db`` fixture, synthetic data
only (rule 5), CliRunner for the thin CLI, and one named behavioural test per
defect class the item was designed against.
"""

from dataclasses import replace
from datetime import date
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ziggurat.cli import main
from ziggurat.cli.main import app
from ziggurat.data.nfl import base, refresh

runner = CliRunner()


# ------------------------------------------------------------------ helpers


def _schedule_rows(db, season=2026, weeks=None, first="2026-09-10"):
    """Minimal REG schedule so the phase / week / gameday derivations resolve."""
    weeks = range(1, 19) if weeks is None else weeks
    start = date.fromisoformat(first)
    rows = []
    for week in weeks:
        gameday = date.fromordinal(start.toordinal() + (week - 1) * 7).isoformat()
        rows.append((
            f"{season}_{week:02d}_AAA_BBB", season, week, "REG", gameday, "BBB", "AAA",
            f"{season}-08-01", f"{season}-08-01",
        ))
    db.executemany(
        "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, knowable_as_of, retrieved_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()


def _spec(name, pull, **kw):
    kw.setdefault("group", refresh.GROUP_DAILY)
    return refresh.SourceSpec(name=name, pull=pull, **kw)


def _ok(n=5):
    return lambda ctx: n


# --------------------------------------------------------- registry integrity


def test_registry_names_are_unique_and_groups_are_known():
    names = [s.name for s in refresh.SOURCES]
    assert len(names) == len(set(names))
    assert {s.group for s in refresh.SOURCES} <= set(refresh.GROUPS)


def test_every_registered_source_is_either_pullable_or_explicitly_blocked():
    """No third state. A source with no pull and no recorded reason would be a
    silent hole in the cadence — exactly the failure this item exists to fix."""
    for spec in refresh.SOURCES:
        assert (spec.pull is None) == bool(spec.blocked), spec.name


def test_only_espn_ranks_replaces_a_partition():
    """The delete-then-write inventory is a claim the floor design rests on: if a
    second such source ever appears, this test makes someone decide about its
    floor rather than discover it after a wipe."""
    replacing = {s.name for s in refresh.SOURCES if s.replaces_partition}
    assert replacing == {"espn_ranks"}


def test_the_perishable_set_is_exactly_the_four_current_value_sources():
    """Guards the report's honesty: calling an nflverse source perishable would
    cry wolf about a re-pullable gap and train the operator to ignore the ones
    where 'gone' is literally true."""
    assert {s.name for s in refresh.SOURCES if s.perishable} == {
        "projections", "adp_rankings", "espn_ranks", "game_weather",
    }


def test_credentials_are_needed_by_exactly_one_source():
    assert {s.name for s in refresh.SOURCES if s.needs_credentials} == {"espn_ranks"}


def test_spine_sources_lead_the_registry_because_order_is_dependency_order():
    names = [s.name for s in refresh.SOURCES]
    assert names[:2] == ["players", "schedules"]
    for spec in refresh.SOURCES:
        if spec.needs_schedules:
            assert names.index(spec.name) > names.index("schedules"), spec.name


# ------------------------------------------------------- selection / cadence


def test_select_by_group_returns_only_that_group():
    weekly = refresh.select_sources(group=refresh.GROUP_WEEKLY)
    assert {s.name for s in weekly} == {
        "weekly_stats", "snap_counts", "team_defense",
        "ngs_passing", "ngs_rushing", "ngs_receiving",
    }


def test_select_by_name_restores_registry_order():
    """`--source weekly_stats --source schedules` typed in that order must NOT
    run weekly_stats first: it would stamp every stat row against an empty
    schedule and drop 100% of them."""
    picked = refresh.select_sources(names=["weekly_stats", "schedules"])
    assert [s.name for s in picked] == ["schedules", "weekly_stats"]


def test_unknown_source_and_group_fail_loudly():
    with pytest.raises(ValueError, match="unknown source"):
        refresh.select_sources(names=["not_a_source"])
    with pytest.raises(ValueError, match="unknown group"):
        refresh.select_sources(group="hourly")


def test_select_with_no_filter_is_the_whole_registry():
    assert refresh.select_sources() == refresh.SOURCES


# ------------------------------------------------------------- season shape


def test_phase_is_derived_from_the_schedule_not_the_wall_clock(db):
    assert refresh.season_phase(db, season=2026, today="2026-07-24") == refresh.PHASE_UNKNOWN
    _schedule_rows(db)
    assert refresh.season_phase(db, season=2026, today="2026-07-24") == refresh.PHASE_PRESEASON
    assert refresh.season_phase(db, season=2026, today="2026-09-10") == refresh.PHASE_INSEASON
    assert refresh.season_phase(db, season=2026, today="2026-11-01") == refresh.PHASE_INSEASON
    assert refresh.season_phase(db, season=2026, today="2027-02-01") == refresh.PHASE_OFFSEASON


def test_current_week_raises_rather_than_guessing(db):
    """Item 3.2 made the same call for weeks=None: a guessed week pulls the wrong
    week's data all season, and the error is rule-1-invisible because the stamps
    stay perfectly valid."""
    with pytest.raises(ValueError, match="schedules table is empty"):
        refresh.current_week(db, season=2026, today="2026-09-15")


def test_current_week_walks_the_schedule(db):
    _schedule_rows(db)
    assert refresh.current_week(db, season=2026, today="2026-09-10") == 1
    assert refresh.current_week(db, season=2026, today="2026-09-11") == 2
    assert refresh.current_week(db, season=2026, today="2026-07-24") == 1  # preseason


def _multiday_schedule(db, season=2026, weeks=None, first="2026-09-10"):
    """A REG schedule with a REAL week shape: Thursday, Sunday, Monday."""
    weeks = range(1, 4) if weeks is None else weeks
    start = date.fromisoformat(first)          # a Thursday
    rows = []
    for week in weeks:
        thursday = date.fromordinal(start.toordinal() + (week - 1) * 7)
        for offset, opp in ((0, "THU"), (3, "SUN"), (4, "MON")):
            gameday = date.fromordinal(thursday.toordinal() + offset).isoformat()
            rows.append((
                f"{season}_{week:02d}_{opp}", season, week, "REG", gameday, "BBB", "AAA",
                f"{season}-08-01", f"{season}-08-01",
            ))
    db.executemany(
        "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, knowable_as_of, retrieved_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()


def test_weather_weeks_stay_inside_the_open_meteo_forecast_wall(db):
    """Open-Meteo 400s beyond ~16 days out (measured +16d OK / +20d 400) and
    fetch_open_meteo has no error tolerance, so a naive 'all weeks' loop would
    crash on its first call."""
    _schedule_rows(db)
    assert refresh.weather_weeks(db, season=2026, today="2026-07-24") == []
    assert refresh.weather_weeks(db, season=2026, today="2026-09-08") == [1, 2]


def test_weather_weeks_keeps_the_current_week_until_its_last_game_is_played(db):
    """3.1b audit: the window was keyed on the week's FIRST game, so a week fell
    out of the request set the moment its Thursday game kicked off. From Friday on
    the gameday timer fetched only NEXT week — and forecast mode is perishable, so
    the freshest forecast any Sunday lineup call could read was three days old."""
    _multiday_schedule(db)                      # week 1: Thu 09-10, Sun 09-13, Mon 09-14
    for day in ("2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"):
        assert 1 in refresh.weather_weeks(db, season=2026, today=day), day
    # ...and drops out the day after its last game.
    assert 1 not in refresh.weather_weeks(db, season=2026, today="2026-09-15")


def test_season_weeks_is_derived_not_hardcoded(db):
    _schedule_rows(db, weeks=range(1, 19))
    assert refresh.season_weeks(db, season=2026) == list(range(1, 19))


# ------------------------------------------------------------- the decision


def test_a_source_that_needs_schedules_is_refused_not_silently_zeroed(db):
    """THE measured silent-zero: with schedules empty, a gameday-stamped ingester
    drops 100% of its rows (19,421/19,421), returns 0 and raises nothing — which
    is indistinguishable from 'upstream had nothing new'."""
    _schedule_rows(db)  # a DIFFERENT season, so the phase resolves but the dep does not
    spec = _spec("dep", _ok(), needs_schedules=True)
    d = refresh.decide(db, spec, season=2099, today="2026-09-15", have_credentials=True)
    assert d.action == refresh.STATUS_SKIPPED
    assert "schedules not ingested" in d.reason


def test_phase_gate_skips_a_source_with_nothing_to_say(db):
    _schedule_rows(db)
    spec = _spec("summer", _ok(), phases=frozenset({refresh.PHASE_INSEASON}))
    d = refresh.decide(db, spec, season=2026, today="2026-07-24", have_credentials=True)
    assert d.action == refresh.STATUS_SKIPPED
    assert "preseason" in d.reason


def test_missing_credentials_skip_only_the_source_that_needs_them(db):
    spec = _spec("board", _ok(), needs_credentials=True)
    d = refresh.decide(db, spec, season=2026, today="2026-07-24", have_credentials=False)
    assert d.action == refresh.STATUS_SKIPPED
    assert "credentials" in d.reason


def test_blocked_sources_are_never_attempted(db):
    spec = refresh.SourceSpec(name="broken", group=refresh.GROUP_DAILY, pull=None,
                              blocked="upstream schema replaced")
    d = refresh.decide(db, spec, season=2026, today="2026-07-24", have_credentials=True)
    assert d.action == refresh.STATUS_BLOCKED


def test_an_unknown_phase_still_lets_the_spine_bootstrap(db):
    """On a fresh database schedules is empty, so nothing phase-gated can be
    judged. players + schedules must still run — that is what makes a first run
    bootstrap in ONE pass instead of needing two."""
    plan = refresh.plan_ingest(db, sources=refresh.SOURCES, season=2026,
                               today="2026-07-24", have_credentials=True)
    pulling = {d.name for d in plan if d.action == "pull"}
    assert {"players", "schedules"} <= pulling


def test_the_phase_is_re_derived_after_schedules_lands_mid_run(db):
    """Not once up front: `decide` runs per source at the moment it is reached."""
    def _land_schedules(ctx):
        _schedule_rows(ctx.conn)
        return 272

    sources = (
        _spec("schedules_fake", _land_schedules),
        _spec("preseason_only", _ok(3), phases=frozenset({refresh.PHASE_PRESEASON})),
    )
    out = refresh.run_ingest(db, sources=sources, season=2026, retrieved_as_of="2026-07-24", today="2026-07-24")
    assert [s["status"] for s in out] == [refresh.STATUS_OK, refresh.STATUS_OK]


# ------------------------------------------------------------- orchestration


def test_a_failing_source_does_not_abort_the_others(db):
    """An nflverse hiccup on ngs_rushing must not cost the day's Sleeper
    projection snapshot, which cannot be re-pulled tomorrow."""
    def _boom(ctx):
        raise RuntimeError("nflverse 500")

    sources = (_spec("a", _ok(7)), _spec("b", _boom), _spec("c", _ok(9)))
    out = refresh.run_ingest(db, sources=sources, season=2026, retrieved_as_of="2026-07-24", today="2026-07-24")
    assert [s["status"] for s in out] == [
        refresh.STATUS_OK, refresh.STATUS_FAILED, refresh.STATUS_OK
    ]
    assert out[2]["rows"] == 9


def test_a_failed_sources_partial_rows_never_ride_the_next_sources_commit(db):
    """THE measured leak: a source that raised mid-executemany left 1,070
    uncommitted rows on the shared connection, and the NEXT ingester's commit
    persisted them — leaving weekly_stats permanently holding week 1 only, with
    valid stamps on every row and the run log saying 'failed'."""
    def _partial_then_boom(ctx):
        ctx.conn.execute(
            "INSERT INTO players (gsis_id, retrieved_as_of, knowable_as_of) VALUES (?,?,?)",
            ("00-0009999", "2026-07-24", "2026-07-24"),
        )
        raise RuntimeError("IntegrityError halfway through")

    def _innocent_commit(ctx):
        ctx.conn.execute(
            "INSERT INTO players (gsis_id, retrieved_as_of, knowable_as_of) VALUES (?,?,?)",
            ("00-0001111", "2026-07-24", "2026-07-24"),
        )
        ctx.conn.commit()
        return 1

    refresh.run_ingest(
        db, sources=(_spec("bad", _partial_then_boom), _spec("good", _innocent_commit)),
        season=2026, retrieved_as_of="2026-07-24", today="2026-07-24",
    )
    stored = {r[0] for r in db.execute("SELECT gsis_id FROM players")}
    assert stored == {"00-0001111"}, "the failed source's fragment must be rolled back"


def test_wrote_zero_but_dropped_everything_is_failed_not_ok(db):
    """'rows=0' is the signature of running a gameday-stamped source before the
    spine exists, and it looks identical to 'this source legitimately had
    nothing'. The drop tally is what separates them."""
    def _drops_all(ctx):
        base.note_drops("fake", 500, 500)
        return 0

    out = refresh.run_ingest(db, sources=(_spec("z", _drops_all),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED
    # "lost", not "dropped": item 3.2c F-G folded the collapse channel into the
    # same counter, and the reason string names which channel each loss came from.
    assert "lost 500/500" in out[0]["reason"]
    assert "500 unstampable" in out[0]["reason"]


def test_wrote_zero_and_dropped_nothing_is_empty_not_ok(db):
    out = refresh.run_ingest(db, sources=(_spec("z", _ok(0)),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_EMPTY


def test_a_partial_drop_is_recorded_as_partial(db):
    def _drops_some(ctx):
        base.note_drops("fake", 3, 100)
        return 97

    out = refresh.run_ingest(db, sources=(_spec("z", _drops_some),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_PARTIAL
    assert out[0]["dropped"] == 3


def test_the_drop_tally_does_not_leak_between_sources(db):
    def _drops(ctx):
        base.note_drops("fake", 2, 10)
        return 8

    out = refresh.run_ingest(db, sources=(_spec("a", _drops), _spec("b", _ok(4))),
                             season=2026, retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["dropped"] == 2
    assert out[1]["dropped"] == 0


def test_an_unpublished_season_is_upstream_absent_not_failed(db):
    """Six sources 404 or raise a client-side season-range ValueError every day
    until ~Sept 10. Logging those as 'failed' for seven weeks is how an operator
    learns to ignore the status output right before the season starts."""
    def _not_yet(ctx):
        raise ValueError("Season must be between 2009 and 2025")

    out = refresh.run_ingest(db, sources=(_spec("z", _not_yet),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_ABSENT


def test_a_refused_board_collapse_surfaces_as_a_failed_run(db):
    """The floor lives in espn_ranks; the orchestrator's job is not to swallow it."""
    from ziggurat.data.nfl import espn_ranks

    def _collapse(ctx):
        raise espn_ranks.BoardCollapse("refusing to replace the stored board")

    out = refresh.run_ingest(db, sources=(_spec("board", _collapse),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert "BoardCollapse" in out[0]["reason"]


def test_credentials_are_splatted_into_the_pull(db):
    seen = {}

    def _needs_creds(ctx):
        seen.update(ctx.credentials or {})
        return 1

    refresh.run_ingest(
        db, sources=(_spec("board", _needs_creds, needs_credentials=True),),
        season=2026, retrieved_as_of="2026-07-24", today="2026-07-24",
        credentials={"league_id": 7, "espn_s2": "s2", "swid": "sw"},
    )
    assert seen == {"league_id": 7, "espn_s2": "s2", "swid": "sw"}


# ---------------------------------------------------------------- back-stamp


def test_back_stamping_is_refused_by_default(db):
    with pytest.raises(ValueError, match="[Bb]ack-stamp"):
        refresh.run_ingest(db, sources=(_spec("a", _ok()),), season=2026,
                           retrieved_as_of="2026-07-01", today="2026-07-24")


def test_back_stamping_is_allowed_explicitly(db):
    out = refresh.run_ingest(db, sources=(_spec("a", _ok()),), season=2026,
                            retrieved_as_of="2026-07-01", today="2026-07-24",
                            allow_backfill=True)
    assert out[0]["status"] == refresh.STATUS_OK


def test_an_unparseable_stamp_is_rejected_not_truncated(db):
    # The as-of gate compares date strings lexically, so '2026-9-8' <= '2026-09-15'
    # is False and a whole day would be written that no accessor could ever see.
    with pytest.raises(ValueError):
        refresh.run_ingest(db, sources=(_spec("a", _ok()),), season=2026,
                           retrieved_as_of="2026-9-8", today="2026-07-24")


# ------------------------------------------------------------------ run log


def test_every_source_writes_a_row_including_the_ones_that_never_ran(db):
    """Silence is not success — a source silently dropped from the cadence is the
    failure this item exists to make visible."""
    sources = (
        _spec("ran", _ok(4)),
        _spec("gated", _ok(), phases=frozenset({refresh.PHASE_INSEASON})),
        refresh.SourceSpec(name="broken", group=refresh.GROUP_DAILY, pull=None,
                           blocked="upstream schema replaced"),
    )
    _schedule_rows(db)
    out = refresh.run_ingest(db, sources=sources, season=2026, retrieved_as_of="2026-07-24", today="2026-07-24")
    logged = db.execute("SELECT * FROM nfl_ingest_runs ORDER BY run_id").fetchall()
    assert [(r["source"], r["status"]) for r in logged] == [
        ("ran", refresh.STATUS_OK),
        ("gated", refresh.STATUS_SKIPPED),
        ("broken", refresh.STATUS_BLOCKED),
    ]
    # One batch_id ties the whole run together, so `ingest run` is reconstructable.
    assert len({r["batch_id"] for r in logged}) == 1
    assert {s["batch_id"] for s in out} == {logged[0]["batch_id"]}
    # Every non-run source still records WHY.
    assert all(r["error"] for r in logged if r["status"] != refresh.STATUS_OK)


def test_a_crash_between_start_and_finish_leaves_a_durable_running_row(db):
    run_id = refresh.start_run(db, batch_id="b1", source="s", season=2026, scope=None,
                               retrieved_as_of="2026-07-24", started_at="2026-07-24T00:00:00+00:00")
    row = db.execute("SELECT status FROM nfl_ingest_runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["status"] == "running"


def test_last_run_orders_by_run_id_not_started_at(db):
    """Run timestamps are second-resolution; two runs inside one second (a retry
    racing the timer) made 'the last run' ambiguous, so a failure could be
    reported as the earlier success."""
    same_second = "2026-07-24T05:15:00+00:00"
    ok = refresh.start_run(db, batch_id="b", source="s", season=2026, scope=None,
                           retrieved_as_of="2026-07-24", started_at=same_second)
    refresh.finish_run(db, ok, status=refresh.STATUS_OK, finished_at=same_second, rows_written=5)
    bad = refresh.start_run(db, batch_id="b", source="s", season=2026, scope=None,
                            retrieved_as_of="2026-07-24", started_at=same_second)
    refresh.finish_run(db, bad, status=refresh.STATUS_FAILED, finished_at=same_second)
    assert refresh.last_run(db, source="s", status=None)["run_id"] == bad
    assert refresh.last_run(db, source="s")["run_id"] == ok


def test_the_run_log_records_the_scope_actually_requested(db):
    _schedule_rows(db, weeks=range(1, 19))
    spec = _spec("proj", _ok(100), scope=refresh._scope_projections, needs_schedules=True)
    refresh.run_ingest(db, sources=(spec,), season=2026, retrieved_as_of="2026-07-24", today="2026-07-24")
    row = db.execute("SELECT scope FROM nfl_ingest_runs").fetchone()
    assert row["scope"] == "weeks 1-18"


def test_nfl_runs_do_not_pollute_the_league_run_log(db):
    """league.state.last_run filters on SEASON ONLY with no source column, so a
    shared table would make the first NFL row the answer to 'when did the league
    last sync' — turning the one honest signal about perishable league history
    into a lie with no test failing."""
    from ziggurat.league import state

    refresh.run_ingest(db, sources=(_spec("a", _ok()),), season=2026,
                       retrieved_as_of="2026-07-24", today="2026-07-24")
    assert state.last_run(db, season=2026, status=None) is None


# ---------------------------------------------------------------- staleness


def _log(db, source, day, status=refresh.STATUS_OK, rows=10, season=2026):
    rid = refresh.start_run(db, batch_id="b", source=source, season=season, scope=None,
                            retrieved_as_of=day, started_at=f"{day}T05:00:00+00:00")
    refresh.finish_run(db, rid, status=status, finished_at=f"{day}T05:00:01+00:00",
                       rows_written=rows)


def _verdict(db, source, today, season=2026):
    rows = refresh.source_freshness(db, season=season, today=today)
    return next(r for r in rows if r["source"] == source)


def test_freshness_verdicts_ladder_by_age(db):
    _schedule_rows(db)
    _log(db, "adp_rankings", "2026-07-24")          # interval 1d
    assert _verdict(db, "adp_rankings", "2026-07-24")["verdict"] == refresh.VERDICT_FRESH
    assert _verdict(db, "adp_rankings", "2026-07-25")["verdict"] == refresh.VERDICT_FRESH
    assert _verdict(db, "adp_rankings", "2026-07-26")["verdict"] == refresh.VERDICT_STALE
    assert _verdict(db, "adp_rankings", "2026-08-24")["verdict"] == refresh.VERDICT_EXPIRED


def test_never_is_resolved_against_the_phase_not_against_zero(db):
    """'never' is only alarming relative to phase. weekly_stats in July is not a
    warning; it is 'not applicable', and rendering it as an alarm is how a status
    report earns being ignored."""
    _schedule_rows(db)
    assert _verdict(db, "weekly_stats", "2026-07-24")["verdict"] == refresh.VERDICT_NA
    assert _verdict(db, "weekly_stats", "2026-10-01")["verdict"] == refresh.VERDICT_NEVER


def test_a_blocked_source_reports_blocked_not_stale(db):
    """REPOINTED at a SYNTHETIC spec (item 3.2c, F-B).

    This test used to name ``depth_charts``, i.e. it pinned a real source's
    DEFECT STATE into the test suite: fixing the defect broke a test that was
    never about depth charts at all, it was about the verdict ladder. A synthetic
    spec can never do that again, and the registry's own health is asserted
    separately (``test_every_registered_source_is_either_pullable_or_explicitly_blocked``).
    """
    _schedule_rows(db)
    spec = refresh.SourceSpec(name="synthetic_blocked", group=refresh.GROUP_DAILY,
                              pull=None, blocked="upstream schema replaced (synthetic)")
    with patch.object(refresh, "SOURCES", (spec,)):
        row = _verdict(db, "synthetic_blocked", "2026-10-01")
        out = refresh.format_status(db, season=2026, today="2026-10-01")
    assert row["verdict"] == refresh.VERDICT_BLOCKED
    assert row["blocked"]
    assert "BLOCKED      : synthetic_blocked" in out




def test_a_partial_pull_still_counts_as_the_last_landing(db):
    _schedule_rows(db)
    _log(db, "adp_rankings", "2026-07-24", status="partial")
    assert _verdict(db, "adp_rankings", "2026-07-24")["verdict"] == refresh.VERDICT_FRESH


def test_a_failed_pull_does_not_refresh_the_verdict(db):
    _schedule_rows(db)
    _log(db, "adp_rankings", "2026-07-01")
    _log(db, "adp_rankings", "2026-07-24", status=refresh.STATUS_FAILED)
    row = _verdict(db, "adp_rankings", "2026-07-24")
    assert row["last_ok"] == "2026-07-01"
    assert row["last_status"] == refresh.STATUS_FAILED
    assert row["verdict"] == refresh.VERDICT_EXPIRED


def test_status_report_names_the_perishable_expiries_and_nothing_else(db):
    _schedule_rows(db)
    _log(db, "adp_rankings", "2026-06-01")   # perishable, long expired
    _log(db, "weekly_stats", "2026-06-01")   # replayable, also long expired
    out = refresh.format_status(db, season=2026, today="2026-10-01")
    assert "PERISHABLE + EXPIRED" in out
    perishable_line = next(ln for ln in out.splitlines() if "PERISHABLE + EXPIRED" in ln)
    assert "adp_rankings" in perishable_line
    assert "weekly_stats" not in perishable_line


def test_status_report_never_uses_unrecoverable_language_for_nflverse(db):
    """3.1's 'missing days are unrecoverable' is true for ESPN league state and
    false for whole-season nflverse files. Reusing it here would train the
    operator to ignore the report where the words are literal."""
    _schedule_rows(db)
    _log(db, "weekly_stats", "2026-06-01")
    out = refresh.format_status(db, season=2026, today="2026-10-01").lower()
    assert "unrecoverable" not in out
    assert "missing days" not in out


# --------------------------------------------------------------- bounded net


def test_the_espn_universe_pull_runs_under_a_bounded_socket(db):
    """espn_api passes no timeout to requests; an unbounded hang would park the
    oneshot service forever and silently stop the cadence (item 3.1's defect,
    still unfixed at this seam until 3.1b)."""
    import socket

    from ziggurat import net
    from ziggurat.data.nfl import espn_source

    seen = {}

    class _FakeLeague:
        class espn_request:
            @staticmethod
            def league_get(*, params, headers):
                seen["timeout"] = socket.getdefaulttimeout()
                return {"players": []}

    before = socket.getdefaulttimeout()
    with patch.object(espn_source, "league_client", return_value=_FakeLeague()):
        espn_source.fetch_player_universe(league_id=1, season=2026, espn_s2="x", swid="y")
    assert seen["timeout"] == net.HTTP_TIMEOUT
    assert socket.getdefaulttimeout() == before   # restored, not leaked


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @staticmethod
    def read():
        return b"{}"


def _timeout_seen_by(module, call) -> int | None:
    """Invoke ``call`` with ``module``'s urlopen patched; return the timeout passed."""
    seen = {}

    def _fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _FakeResponse()

    with patch.object(module.urllib.request, "urlopen", _fake_urlopen):
        call()
    return seen.get("timeout")


def test_the_sleeper_projections_seam_passes_an_explicit_timeout():
    """Called ONCE PER WEEK inside the scheduled projections pull, and it used to
    call urlopen with no timeout at all."""
    from ziggurat import net
    from ziggurat.data.nfl import source as nfl_source

    assert _timeout_seen_by(
        nfl_source, lambda: nfl_source.import_sleeper_projections(2026, 1)
    ) == net.HTTP_TIMEOUT


def test_the_open_meteo_seam_passes_an_explicit_timeout():
    """Called once per OUTDOOR GAME — ~13 chances a week to park the run."""
    from ziggurat import net
    from ziggurat.data.nfl import weather

    assert _timeout_seen_by(
        weather,
        lambda: weather.fetch_open_meteo(
            42.0, -78.0, "2026-09-10", "America/New_York", mode="forecast"
        ),
    ) == net.HTTP_TIMEOUT


# ----------------------------------------------------------------------- CLI


def test_cli_dry_run_touches_no_network_and_writes_nothing(tmp_path):
    """`--dry-run` reports the plan. The plan comes from the SAME `decide` the
    real run uses, so the two cannot disagree."""
    db_path = tmp_path / "z.sqlite"
    with patch.object(main, "run_ingest", side_effect=AssertionError("must not run")):
        result = runner.invoke(app, ["ingest", "run", "--dry-run", "--path", str(db_path),
                                     "--season", "2026"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert "PULL" in result.output

    from ziggurat.data.store import connect

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM nfl_ingest_runs").fetchone()["c"] == 0
    conn.close()


def test_cli_status_reports_per_source_staleness(tmp_path):
    """The item's done-when, asserted on the VERDICTS rather than on the source
    names being printed: one ok run, one failed run, one other-season run, and the
    report must tell them apart."""
    from ziggurat.data.store import open_db

    db_path = tmp_path / "z.sqlite"
    runner.invoke(app, ["ingest", "run", "--dry-run", "--path", str(db_path)])  # creates the db
    conn = open_db(db_path)
    _schedule_rows(conn)
    _log(conn, "adp_rankings", "2026-07-24", rows=411)
    _log(conn, "espn_ranks", "2026-07-24", status=refresh.STATUS_FAILED)
    conn.execute("UPDATE nfl_ingest_runs SET error = ? WHERE source = 'espn_ranks'",
                 ("BoardCollapse: refusing to write an EMPTY ESPN board",))
    # ...and a run for a DIFFERENT season, which must not make 2026 look fresh.
    rid = refresh.start_run(conn, batch_id="b", source="players", season=2025, scope=None,
                            retrieved_as_of="2026-07-24",
                            started_at="2026-07-24T05:00:00+00:00")
    refresh.finish_run(conn, rid, status=refresh.STATUS_OK, rows_written=7732,
                       finished_at="2026-07-24T05:00:01+00:00")
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["ingest", "status", "--path", str(db_path),
                                 "--season", "2026", "--through", "2026-07-24"])
    assert result.exit_code == 0, result.output
    assert "nfl ingest status" in result.output
    for spec in refresh.SOURCES:
        assert spec.name in result.output

    lines = {ln.split()[0]: ln for ln in result.output.splitlines()
             if ln.startswith("  ") and len(ln.split()) > 2}
    assert refresh.VERDICT_FRESH in lines["adp_rankings"]
    assert "2026-07-24" in lines["adp_rankings"] and "411" in lines["adp_rankings"]
    assert refresh.VERDICT_NEVER in lines["espn_ranks"]
    assert "LAST ATTEMPT FAILED: espn_ranks" in result.output
    assert "EMPTY ESPN board" in result.output
    # the 2025 players run must NOT make the 2026 players row fresh
    assert refresh.VERDICT_NEVER in lines["players"]


def test_cli_sources_lists_the_registry():
    result = runner.invoke(app, ["ingest", "sources"])
    assert result.exit_code == 0, result.output
    assert "perishable" in result.output
    assert "replaces-partition" in result.output


def test_cli_run_exits_nonzero_when_a_source_failed(tmp_path):
    db_path = tmp_path / "z.sqlite"

    def _boom(ctx):
        raise RuntimeError("nflverse 500")

    fake = (refresh.SourceSpec(name="players", group=refresh.GROUP_DAILY, pull=_boom),)
    # Patch the CLI's OWN binding: `from ... import select_sources` copies the
    # name, so patching refresh.select_sources would leave the command calling
    # the real registry — and this test would silently hit the network.
    with patch.object(main, "select_sources", return_value=fake):
        result = runner.invoke(app, ["ingest", "run", "--path", str(db_path),
                                     "--season", "2026", "--source", "players"])
    assert result.exit_code == 1, result.output
    assert "failed" in result.output


def test_cli_run_exits_zero_when_everything_is_merely_skipped(tmp_path):
    """A timer must not report failure just because the season phase says there
    is nothing to pull — otherwise the operator is trained by seven weeks of red."""
    db_path = tmp_path / "z.sqlite"
    fake = (refresh.SourceSpec(name="weekly_stats", group=refresh.GROUP_WEEKLY,
                               pull=lambda ctx: 1, needs_schedules=True),)
    with patch.object(main, "select_sources", return_value=fake):
        result = runner.invoke(app, ["ingest", "run", "--path", str(db_path),
                                     "--season", "2026", "--source", "weekly_stats"])
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output


def test_ingest_status_works_on_a_database_predating_migration_006(tmp_path):
    """CLAUDE.md will tell the operator to run `ingest status` on the cadence
    machine; on a pre-006 database that must not die with a raw sqlite traceback
    (the same item-3.1 audit finding, same fix: open_db migrates first)."""
    from ziggurat.data.store import connect
    from ziggurat.paths import MIGRATIONS_DIR, SCHEMA_PATH

    db_path = tmp_path / "old.sqlite"
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for version in (2, 3, 4, 5):
        path = next(MIGRATIONS_DIR.glob(f"{version:03d}_*.sql"))
        conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '5')")
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["ingest", "status", "--path", str(db_path), "--season", "2026"])
    assert result.exit_code == 0, result.output
    assert "nfl ingest status" in result.output


# =====================================================================
# 3.1b AUDIT FIXES — one named test per finding, all offline.
# =====================================================================

# --------------------------------------------------- the back-stamp fence


def test_run_ingest_requires_today_so_its_own_fence_cannot_be_disabled(db):
    """`today=None` used to default to the stamp, making `stamp != today`
    trivially false — the documented 'back-stamping is refused by default' path
    did not refuse, and wrote today's upstream data under a past retrieved_as_of
    (a manufactured leak under the default historical view)."""
    with pytest.raises(TypeError, match="today"):
        refresh.run_ingest(db, sources=(_spec("a", _ok()),), season=2026,
                           retrieved_as_of="2026-03-01")


def test_a_back_stamped_run_would_be_readable_at_a_past_as_of(db):
    """Why the fence exists, stated as a property: with --allow-backfill the row
    IS visible to a historical-view read of a day it did not exist on."""
    refresh.run_ingest(db, sources=(_spec("a", _ok()),), season=2026,
                       retrieved_as_of="2026-03-01", today="2026-07-24",
                       allow_backfill=True)
    row = db.execute("SELECT retrieved_as_of FROM nfl_ingest_runs").fetchone()
    assert row["retrieved_as_of"] == "2026-03-01"


def test_resolve_stamp_is_the_one_gate_both_the_plan_and_the_run_use(db):
    with pytest.raises(ValueError, match="manufactured leak"):
        refresh.resolve_stamp("2026-07-01", "2026-07-24")
    assert refresh.resolve_stamp("2026-07-01", "2026-07-24", allow_backfill=True) == (
        "2026-07-01", "2026-07-24")
    assert refresh.resolve_stamp("2026-07-24", "2026-07-24") == ("2026-07-24", "2026-07-24")


# --------------------------------------------------- freshness is bounded


def test_freshness_never_reports_a_run_that_had_not_happened_yet(db):
    """`ingest status --through <past day>` answered from FUTURE runs: the age went
    negative and `age <= interval` then pinned the verdict at fresh forever. The
    operator's Monday retro ('was my data fresh when I set the Week 5 lineup?')
    was answered on the strength of a November pull."""
    _schedule_rows(db)
    _log(db, "adp_rankings", "2026-07-24")
    assert _verdict(db, "adp_rankings", "2026-07-01")["verdict"] == refresh.VERDICT_NEVER
    assert _verdict(db, "adp_rankings", "2026-07-01")["age_days"] is None
    assert _verdict(db, "adp_rankings", "2026-07-24")["verdict"] == refresh.VERDICT_FRESH


def test_freshness_is_scoped_to_the_season_it_was_asked_about(db):
    """One `ingest run --season 2025` backfill used to mark every 2026 source
    fresh, and after the March rollover the units (which pin --season at install
    time) would report last season's pulls as this season's."""
    _schedule_rows(db)
    rid = refresh.start_run(db, batch_id="b", source="adp_rankings", season=2025, scope=None,
                            retrieved_as_of="2026-07-24", started_at="2026-07-24T05:00:00+00:00")
    refresh.finish_run(db, rid, status=refresh.STATUS_OK, finished_at="2026-07-24T05:00:01+00:00",
                       rows_written=500)
    assert _verdict(db, "adp_rankings", "2026-07-24")["verdict"] == refresh.VERDICT_NEVER
    row = refresh.last_run(db, source="adp_rankings", season=2025)
    assert row["rows_written"] == 500


# --------------------------------------------------- drop ratio, not zero


def test_a_mostly_dropped_pull_is_failed_not_partial(db):
    """One surviving row out of 19,421 was recorded `partial`, which was excluded
    from the failure list, the exit code AND the staleness verdict — so a new team
    abbr missing from base.TEAM_ALIASES would print 'no failures' and read fresh."""
    def _drops_almost_all(ctx):
        base.note_drops("fake", 19354, 19421)
        return 67

    out = refresh.run_ingest(db, sources=(_spec("z", _drops_almost_all),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert refresh.run_failed(out)
    assert "PROBLEMS" in refresh.format_run(out)


def test_by_design_filtering_does_not_count_against_the_ceiling(db):
    """The FIRST LIVE RUN of item 3.1b failed adp_rankings at 35% on a healthy
    pull: FantasyPros ships IDP rows this league cannot start, and filtering them
    is CORRECT behaviour, not data loss. A guard that fails a good pull is how the
    operator is trained to ignore the report."""
    def _filters_idp_by_design(ctx):
        base.note_drops("fake", 1692, 6391, why="IDP position (not startable)",
                        by_design=True)
        return 4699

    out = refresh.run_ingest(db, sources=(_spec("z", _filters_idp_by_design),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_OK
    assert not refresh.run_failed(out)


def test_rows_kept_with_a_missing_field_are_not_counted_as_dropped(db):
    """adp_rankings called note_drops for rows it KEPT — its own line comment read
    "kept (NULL gsis_id), not dropped" — inflating the ratio with rows that were
    sitting in the table, readable."""
    def _keeps_but_incomplete(ctx):
        base.note_incomplete("fake", 861, 4699, why="unresolved crosswalk id")
        return 4699

    out = refresh.run_ingest(db, sources=(_spec("z", _keeps_but_incomplete),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_OK


def test_a_real_drop_still_fails_even_alongside_by_design_filtering(db):
    """The relaxation must not open a hole: genuine unresolvable rows still trip
    the ceiling when a by-design filter runs in the same pull."""
    def _both(ctx):
        base.note_drops("fake", 1692, 6391, why="IDP", by_design=True)
        base.note_drops("fake", 900, 1000, why="new team abbr")
        return 100

    out = refresh.run_ingest(db, sources=(_spec("z", _both),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert "900/1000" in out[0]["reason"]  # denominator is written+dropped, not the tally sum


def test_the_reason_string_numerator_and_percentage_agree(db):
    """The ceiling tested dropped/(written+dropped) but PRINTED dropped/tally-total,
    so the first live failure read "dropped 2553/11090 (35%)" — and 2553/11090 is
    23%. Two different denominators in one sentence, neither labelled."""
    def _drops_some(ctx):
        base.note_drops("fake", 60, 100)
        base.note_drops("fake", 0, 500)  # a second call inflates tally['total']
        return 40

    out = refresh.run_ingest(db, sources=(_spec("z", _drops_some),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert "60/100" in out[0]["reason"] and "60%" in out[0]["reason"]


def test_nothing_to_do_is_skipped_not_empty(db):
    """game_weather outside the forecast horizon returned STATUS_EMPTY, which
    run_failed() counts as a problem — so from July to ~Sept a correct cadence
    reported a standing 'LAST ATTEMPT FAILED' on a PERISHABLE source. Observed on
    the first live run (2026-07-24); weather_weeks' docstring already called the
    empty set 'a legitimate nothing to do rather than a failure'."""
    spec = _spec("z", _ok(0))
    spec = replace(spec, applicable=lambda ctx: "no week inside the forecast horizon")

    out = refresh.run_ingest(db, sources=(spec,), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_SKIPPED
    assert not refresh.run_failed(out)
    assert "forecast horizon" in out[0]["reason"]


def test_an_applicable_source_still_runs(db):
    """The predicate must not become a blanket off-switch."""
    spec = replace(_spec("z", _ok(5)), applicable=lambda ctx: None)
    out = refresh.run_ingest(db, sources=(spec,), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_OK and out[0]["rows"] == 5


def test_a_source_with_nothing_to_do_reads_na_not_never_pulled(db):
    """`never` is only alarming relative to what the source COULD have done. A
    perishable source that is in-phase but has nothing to fetch yet (game_weather
    all preseason) would otherwise carry a NEVER PULLED alarm for six weeks."""
    _multiday_schedule(db)                            # week 1 Thu 2026-09-10
    rows = refresh.source_freshness(db, season=2026, today="2026-07-24")
    weather = next(r for r in rows if r["source"] == "game_weather")
    assert weather["verdict"] == refresh.VERDICT_NA
    # Other sources in the registry are legitimately never-pulled here; the point
    # is that game_weather is not among the ones being alarmed about.
    never_line = next(
        (ln for ln in refresh.format_status(db, season=2026, today="2026-07-24").splitlines()
         if "NEVER PULLED" in ln), "")
    assert "game_weather" not in never_line

    # ...but once the horizon opens, silence would be a real omission.
    near = refresh.source_freshness(db, season=2026, today="2026-09-05")
    weather_near = next(r for r in near if r["source"] == "game_weather")
    assert weather_near["verdict"] == refresh.VERDICT_NEVER


def test_an_empty_pull_is_a_problem_for_the_exit_code_too(db):
    """format_run printed `PROBLEMS: x` while the CLI exited 0 on the same run —
    so under Restart=on-failure an empty pull of a PERISHABLE source was reported
    to systemd as success and never retried."""
    out = refresh.run_ingest(db, sources=(_spec("z", _ok(0)),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_EMPTY
    assert refresh.run_failed(out)


# ------------------------------------------- upstream_absent classification


def test_upstream_absent_is_classified_on_type_not_on_a_substring(db):
    """The marker list matched '404' and 'no such file' anywhere in any message,
    so real breakage was downgraded to an expected absence and exited 0 — most
    sharply espn_ranks' OWN drift guard, whose message contains '404/2051'."""
    import urllib.error

    absent = [
        ValueError("Season must be between 2012 and 2025"),
        urllib.error.HTTPError("u", 404, "Not Found", None, None),
        ConnectionError("Failed to download https://x/y.parquet: 404 Client Error: "
                        "Not Found for url: https://x/y.parquet"),
    ]
    real = [
        FileNotFoundError(2, "No such file or directory"),
        OSError("[Errno 2] No such file or directory: '/home/x/.cache/nflreadpy'"),
        ValueError("ESPN payload schema drift: only 404/2051 mapped rows carry a PPR "
                   "editorial rank (min coverage 50%)"),
        ValueError("projections: unresolved knowledge time for 4045 of 112566 rows"),
        RuntimeError("ESPN rejected the request (expired/invalid cookies?)"),
        ValueError("IntegrityError: NOT NULL constraint failed at row 1404"),
        ConnectionError("Failed to download https://x/y.parquet: 500 Server Error"),
    ]
    assert [refresh._is_upstream_absent(e) for e in absent] == [True, True, True]
    assert [refresh._is_upstream_absent(e) for e in real] == [False] * len(real)


def test_a_404_after_this_season_already_landed_is_failed_not_absent(db):
    """'Not published yet' is impossible once the source has succeeded for this
    season. A 404 then means the release was renamed or withdrawn — a real break
    wearing the expected costume, which would otherwise exit 0 forever."""
    def _gone(ctx):
        raise ConnectionError("Failed to download https://x/y.parquet: 404 Client Error")

    refresh.run_ingest(db, sources=(_spec("z", _ok(9)),), season=2026,
                       retrieved_as_of="2026-07-24", today="2026-07-24")
    out = refresh.run_ingest(db, sources=(_spec("z", _gone),), season=2026,
                             retrieved_as_of="2026-07-25", today="2026-07-25")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert "already succeeded" in out[0]["reason"]


def test_a_first_ever_404_is_absent_and_does_not_fail_the_run(db):
    def _not_yet(ctx):
        raise ConnectionError("Failed to download https://x/y.parquet: 404 Client Error")

    out = refresh.run_ingest(db, sources=(_spec("z", _not_yet),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_ABSENT
    assert not refresh.run_failed(out)
    # ...but it is never silent.
    assert "not published upstream yet" in refresh.format_run(out)


# ------------------------------------------------- the run log's honesty


def test_a_crash_between_start_and_finish_leaves_a_DURABLE_running_row(tmp_path):
    """Durable means COMMITTED — visible from another connection. Re-reading
    through the same connection proved nothing: an uncommitted INSERT is fully
    visible there, so the test passed with start_run's commit deleted."""
    from ziggurat.data.store import connect

    path = tmp_path / "runs.sqlite"
    writer = connect(path)
    apply = __import__("ziggurat.data.store", fromlist=["apply_schema"]).apply_schema
    apply(writer)
    reader = connect(path)

    refresh.start_run(writer, batch_id="b1", source="s", season=2026, scope=None,
                      retrieved_as_of="2026-07-24", started_at="2026-07-24T00:00:00+00:00")
    row = reader.execute("SELECT status FROM nfl_ingest_runs").fetchone()
    assert row is not None and row["status"] == refresh.STATUS_RUNNING
    writer.close()
    reader.close()


def test_an_orphaned_running_row_is_reaped_by_the_next_run(db):
    """TimeoutStartSec SIGTERMs a hung run and leaves a `running` row nothing ever
    updates. Left alone it is invisible forever and indistinguishable from a run
    happening right now."""
    refresh.start_run(db, batch_id="b1", source="s", season=2026, scope=None,
                      retrieved_as_of="2026-07-24", started_at="2026-07-24T00:00:00+00:00")
    refresh.start_run(db, batch_id="b2", source="s", season=2026, scope=None,
                      retrieved_as_of="2026-07-25", started_at="2026-07-25T00:00:00+00:00")
    statuses = [r["status"] for r in
                db.execute("SELECT status FROM nfl_ingest_runs ORDER BY run_id")]
    assert statuses == [refresh.STATUS_ABANDONED, refresh.STATUS_RUNNING]


# ------------------------------------------------- the status report speaks


def test_status_names_a_source_that_is_failing_behind_a_fresh_verdict(db):
    """players/weekly_stats carry a 7d interval, so a source can fail every run for
    six days and still print `fresh`. The failure is the actionable fact and it was
    dropped by the renderer — the item-3.1 'a degraded run still logged ok' defect
    moved out into the report the operator is told to trust."""
    _schedule_rows(db)
    _log(db, "players", "2026-07-20")
    _log(db, "players", "2026-07-24", status=refresh.STATUS_FAILED)
    db.execute("UPDATE nfl_ingest_runs SET error = ? WHERE status = ?",
               ("ConnectionError: nflverse unreachable", refresh.STATUS_FAILED))
    db.commit()

    out = refresh.format_status(db, season=2026, today="2026-07-24")
    assert "LAST ATTEMPT FAILED: players" in out
    assert "nflverse unreachable" in out
    assert "all applicable sources fresh" not in out


def test_status_reports_a_run_that_never_finished(db):
    _schedule_rows(db)
    _log(db, "players", "2026-07-20")
    refresh.start_run(db, batch_id="b", source="players", season=2026, scope=None,
                      retrieved_as_of="2026-07-24", started_at="2026-07-24T07:20:00+00:00")
    refresh.start_run(db, batch_id="c", source="players", season=2026, scope=None,
                      retrieved_as_of="2026-07-24", started_at="2026-07-24T07:30:00+00:00")
    out = refresh.format_status(db, season=2026, today="2026-07-24")
    assert "RUN NEVER FINISHED: players" in out


def test_an_unpublished_season_reads_awaiting_not_never(db):
    """Six sources are legitimately absent every day until ~Sept 10. Eighteen weeks
    of them sitting in a NEVER PULLED alarm is how the report earns being ignored."""
    _schedule_rows(db)
    _log(db, "weekly_stats", "2026-10-01", status=refresh.STATUS_ABSENT, rows=None)
    row = _verdict(db, "weekly_stats", "2026-10-01")
    assert row["verdict"] == refresh.VERDICT_AWAITING
    out = refresh.format_status(db, season=2026, today="2026-10-01")
    assert "NOT PUBLISHED UPSTREAM YET" in out
    never_lines = [ln for ln in out.splitlines() if "NEVER PULLED" in ln]
    assert not any("weekly_stats" in ln for ln in never_lines)


# ------------------------------------------------------- the interval gate


def test_a_source_inside_its_interval_is_skipped_not_re_pulled(db):
    """What makes `interval_days` load-bearing rather than decorative: the timers
    can fire daily and the registry decides how often upstream is actually hit."""
    _schedule_rows(db)
    spec = _spec("wk", _ok(5), interval_days=7)
    refresh.run_ingest(db, sources=(spec,), season=2026,
                       retrieved_as_of="2026-07-24", today="2026-07-24")
    again = refresh.run_ingest(db, sources=(spec,), season=2026,
                               retrieved_as_of="2026-07-25", today="2026-07-25")
    assert again[0]["status"] == refresh.STATUS_FRESH
    assert not refresh.run_failed(again)


def test_a_failed_pull_retries_the_next_day_instead_of_waiting_a_week(db):
    """The hole a fixed OnCalendar=Thu left: an nflverse outage that outlasted the
    unit's three restarts cost a whole in-season week of weekly_stats, while
    `ingest status` still said fresh (age 7 <= interval 7)."""
    _schedule_rows(db)

    def _boom(ctx):
        raise RuntimeError("nflverse 500")

    refresh.run_ingest(db, sources=(_spec("wk", _boom, interval_days=7),), season=2026,
                       retrieved_as_of="2026-07-24", today="2026-07-24")
    out = refresh.run_ingest(db, sources=(_spec("wk", _ok(5), interval_days=7),), season=2026,
                             retrieved_as_of="2026-07-25", today="2026-07-25")
    assert out[0]["status"] == refresh.STATUS_OK


def test_force_overrides_the_interval_gate(db):
    _schedule_rows(db)
    spec = _spec("wk", _ok(5), interval_days=7)
    refresh.run_ingest(db, sources=(spec,), season=2026,
                       retrieved_as_of="2026-07-24", today="2026-07-24")
    out = refresh.run_ingest(db, sources=(spec,), season=2026, retrieved_as_of="2026-07-25",
                             today="2026-07-25", force=True)
    assert out[0]["status"] == refresh.STATUS_OK


def test_the_interval_gate_is_scoped_to_the_season(db):
    """A 2025 backfill must not make the 2026 cadence think it just ran."""
    _schedule_rows(db)
    spec = _spec("wk", _ok(5), interval_days=7)
    refresh.run_ingest(db, sources=(spec,), season=2025,
                       retrieved_as_of="2026-07-24", today="2026-07-24")
    out = refresh.run_ingest(db, sources=(spec,), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_OK


# ------------------------------------------------------------ selection


def test_group_and_source_together_are_refused_rather_than_one_winning(db):
    with pytest.raises(ValueError, match="not both"):
        refresh.select_sources(group=refresh.GROUP_WEEKLY, names=["players"])


# ------------------------------------------------- partial multi-request pull


def test_a_mid_loop_weather_failure_reports_what_actually_landed(db):
    """game_weather commits per week, so a later week's failure cannot roll the
    earlier ones back. Recording rows_written=0 made nfl_ingest_runs a wrong answer
    to 'what is in the database' for the one PERISHABLE gameday source."""
    def _partial(ctx):
        raise refresh.PartialPull("week 2 blew up after 40 rows", rows_written=40,
                                  cause=RuntimeError("open-meteo 400"))

    out = refresh.run_ingest(db, sources=(_spec("weather", _partial),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert out[0]["rows"] == 40
    row = db.execute("SELECT rows_written FROM nfl_ingest_runs").fetchone()
    assert row["rows_written"] == 40


# -------------------------------------------------- the delete-then-write claim


def test_no_nfl_ingester_outside_espn_ranks_deletes_rows():
    """The floor design rests on an INVENTORY claim ('exactly one delete-then-write
    path'), and the registry flag that records it is hand-maintained. Assert the
    property behaviourally so a new DELETE cannot appear without someone deciding
    about its floor."""
    from pathlib import Path

    import ziggurat.data.nfl as nfl_pkg

    offenders = []
    for path in Path(nfl_pkg.__file__).parent.glob("*.py"):
        if path.name in ("espn_ranks.py", "base.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if "DELETE FROM" in text.upper():
            offenders.append(path.name)
    assert offenders == [], f"new delete-then-write path(s): {offenders}"
    assert {s.name for s in refresh.SOURCES if s.replaces_partition} == {"espn_ranks"}


# ------------------------------------------------------------ CLI, part two


def test_cli_dry_run_and_the_real_run_resolve_the_same_day(tmp_path):
    """`--dry-run --as-of X` planned against day X while the real run planned
    against today, so they disagreed on 3 of 8 daily sources — worst for the one
    destructive source: the preview said espn_ranks would be SKIPPED and the real
    run then deleted its board partition."""
    db_path = tmp_path / "z.sqlite"
    seen = {}

    def _capture(conn, *, sources, season, today, have_credentials, force=False):
        seen["today"] = today
        return []

    with patch.object(main, "plan_ingest", _capture):
        runner.invoke(app, ["ingest", "run", "--dry-run", "--path", str(db_path),
                            "--season", "2026", "--as-of", main._today()])
    assert seen["today"] == main._today()


def test_cli_refuses_a_past_as_of_in_the_dry_run_too(tmp_path):
    """The documented workflow is 'dry-run first, then without it'. The dry run
    used to print a clean plan of PULLs and the identical real command then died
    with an unhandled ValueError traceback."""
    db_path = tmp_path / "z.sqlite"
    for extra in ([], ["--dry-run"]):
        result = runner.invoke(app, ["ingest", "run", "--path", str(db_path),
                                     "--season", "2026", "--as-of", "2026-07-01",
                                     "--source", "players", *extra])
        assert result.exit_code == 2, result.output
        assert "manufactured leak" in result.output
        assert "Traceback" not in result.output


def test_cli_run_exits_nonzero_on_an_empty_pull_of_a_perishable_source(tmp_path):
    db_path = tmp_path / "z.sqlite"
    fake = (refresh.SourceSpec(name="adp_rankings", group=refresh.GROUP_DAILY,
                               pull=lambda ctx: 0, perishable=True),)
    with patch.object(main, "select_sources", return_value=fake):
        result = runner.invoke(app, ["ingest", "run", "--path", str(db_path),
                                     "--season", "2026", "--source", "adp_rankings"])
    assert result.exit_code == 1, result.output
    assert "PROBLEMS" in result.output


def test_cli_run_exits_zero_when_upstream_has_not_published_this_season(tmp_path):
    """Six sources are absent every day until ~Sept 10; seven weeks of red is how
    an operator is trained to ignore the unit."""
    db_path = tmp_path / "z.sqlite"

    def _absent(ctx):
        raise ValueError("Season must be between 2012 and 2025")

    fake = (refresh.SourceSpec(name="weekly_stats", group=refresh.GROUP_WEEKLY, pull=_absent),)
    with patch.object(main, "select_sources", return_value=fake):
        result = runner.invoke(app, ["ingest", "run", "--path", str(db_path),
                                     "--season", "2026", "--source", "weekly_stats"])
    assert result.exit_code == 0, result.output
    assert "upstream_absent" in result.output


# ------------------------------------------------------------ bounded network


def test_the_espn_seam_actually_times_out_against_a_black_holed_connection():
    """THE test the previous one only pretended to be. `bounded_socket` sets the
    process-wide socket default, which `requests` DISCARDS: it always hands
    urllib3 an explicit timeout, and with none supplied that value is None, so
    urllib3 calls sock.settimeout(None). Measured against a local
    accept-and-never-reply server, `with bounded_socket(3): requests.get(...)`
    was still blocked at 40s. The old test asserted socket.getdefaulttimeout()
    inside a fake that opened no socket, so it passed against a client that
    ignores the default entirely.

    Localhost only — no external network.
    """
    import socket
    import threading
    import time
    from importlib import import_module

    from ziggurat import net

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    held = []

    def _accept_and_never_reply():
        while True:
            try:
                held.append(server.accept()[0])
            except OSError:
                return

    threading.Thread(target=_accept_and_never_reply, daemon=True).start()
    try:
        espn_requests = import_module("espn_api.requests.espn_requests")
        client = espn_requests.EspnFantasyRequests(sport="nfl", year=2026, league_id=1,
                                                   cookies={})
        client.LEAGUE_ENDPOINT = f"http://127.0.0.1:{port}/apis/v3/x"
        started = time.monotonic()
        with pytest.raises(Exception) as excinfo:
            with net.bounded_espn(2):
                client.league_get(params={"view": "mTeam"})
        elapsed = time.monotonic() - started
        assert elapsed < 15, f"the ESPN seam is unbounded ({elapsed:.0f}s and counting)"
        assert "timeout" in type(excinfo.value).__name__.lower()
        # and the module is restored, not left shimmed for the rest of the process
        assert espn_requests.requests.__name__ == "requests"
    finally:
        server.close()
        for sock in held:
            sock.close()


def test_bounded_espn_leaves_an_explicit_timeout_alone():
    from importlib import import_module

    from ziggurat import net

    espn_requests = import_module("espn_api.requests.espn_requests")
    seen = {}

    class _FakeRequests:
        __name__ = "requests"

        @staticmethod
        def get(url, **kwargs):
            seen.update(kwargs)
            return None

    original = espn_requests.requests
    espn_requests.requests = _FakeRequests()
    try:
        with net.bounded_espn(7):
            espn_requests.requests.get("http://x")
            assert seen["timeout"] == 7
            espn_requests.requests.get("http://x", timeout=1)
            assert seen["timeout"] == 1
    finally:
        espn_requests.requests = original


def test_week_one_weather_is_captured_in_its_run_up_not_only_on_its_thursday(db):
    """The phase flips to inseason ON week 1's Thursday, so with an inseason-only
    gate the Sunday of week 1 would have had exactly one forecast capture. The
    10-day horizon keeps the preseason pull a no-op until the run-up."""
    _multiday_schedule(db)                        # week 1 Thu 2026-09-10
    spec = refresh.SOURCES_BY_NAME["game_weather"]
    assert refresh.PHASE_PRESEASON in spec.phases
    d = refresh.decide(db, spec, season=2026, today="2026-09-05", have_credentials=False)
    assert d.action == "pull" and "1" in d.scope
    # ...and far out it is a no-op, not a request outside Open-Meteo's wall. It is
    # SKIPPED rather than pulled-and-empty: STATUS_EMPTY counts as a problem for
    # run_failed(), so the old shape reported a standing "LAST ATTEMPT FAILED" on
    # this PERISHABLE source from July until the season (observed live 2026-07-24).
    early = refresh.decide(db, spec, season=2026, today="2026-07-24", have_credentials=False)
    assert early.action == refresh.STATUS_SKIPPED
    assert "forecast horizon" in early.reason


# ===================================================================== item 3.2c
#
# WHAT THESE TESTS CAN AND CANNOT CATCH (the 3.1b fixture lesson). Everything
# below runs real SQL against real SQLite with fake `pull` closures, so it
# catches regressions in the ORCHESTRATOR: the phase gate, the anchor set, the
# verdict ladder, the drop ceiling, the run log. It proves NOTHING about
# upstream — not that nflverse still publishes a depth-chart panel daily, not
# that `injuries` 2023 is still downloadable, not that a real pull's tally has
# the shape asserted here. Those are the per-ingester tests' job plus the live
# measurements recorded in the item's Update block, and per 3.1b a frozen
# fixture is weak evidence for any of them.


# --------------------------------------------------- F-A: the offseason phase


def _past_schedule(db, season=2023):
    """A COMPLETED season's REG schedule, so its phase resolves to offseason."""
    _schedule_rows(db, season=season, first=f"{season}-09-07")


def test_a_completed_season_can_be_pulled_for_the_whole_season_files(db):
    """F-A. `decide()` checks the phase BEFORE the force-able interval gate, so a
    phase set that excludes offseason cannot be overridden by --force at all:
    `ingest run --season 2023 --force` printed SKIPPED for injuries and game_odds
    and pulled nothing. Both are whole-season files that upstream re-serves
    forever, so refusing them in the offseason was wrong on the merits before the
    backfill ever needed them."""
    _past_schedule(db)
    for name in ("injuries", "game_odds"):
        spec = refresh.SOURCES_BY_NAME[name]
        assert refresh.PHASE_OFFSEASON in spec.phases, name
        d = refresh.decide(db, spec, season=2023, today="2026-07-25",
                           have_credentials=False)
        assert d.action == "pull", (name, d.reason)


def test_the_offseason_widening_is_not_a_force_bypass(db):
    """F-A explicitly rejected a bypass flag: a bypass forks the decision path,
    which is the failure class 3.1b's audit spent a round on. So a source that
    genuinely has nothing to say in a phase is STILL refused, --force or not."""
    _past_schedule(db)
    spec = refresh.SOURCES_BY_NAME["espn_ranks"]      # preseason only
    d = refresh.decide(db, spec, season=2023, today="2026-07-25",
                       have_credentials=True, force=True)
    assert d.action == refresh.STATUS_SKIPPED
    assert "offseason phase" in d.reason


def test_the_phase_gate_still_bites_in_season(db):
    """The widening must not become 'always applicable'. game_weather has no
    offseason business (the forecast endpoint 400s past ~16 days) and must stay
    out of it."""
    assert refresh.PHASE_OFFSEASON not in refresh.SOURCES_BY_NAME["game_weather"].phases
    assert refresh.PHASE_OFFSEASON not in refresh.SOURCES_BY_NAME["espn_ranks"].phases


# ------------------------------------------------------ F-B: depth_charts v2


def test_depth_charts_is_registered_and_wired_to_the_panel_module():
    """F-B. The 3.1b block said this needed a table + accessor rewrite; 3.2c did
    it. The spec's SHAPE is asserted because each field encodes a measured
    decision, and a silent revert of any of them is a real regression."""
    from ziggurat.data.nfl import depth_charts

    spec = refresh.SOURCES_BY_NAME["depth_charts"]
    assert spec.blocked is None
    assert spec.pull is not None
    # needs_schedules FALSE: `dt` IS the knowledge time. True would re-impose a
    # season_weeks() precondition AND drop the source out of the PHASE_UNKNOWN
    # bootstrap set that decide() relies on.
    assert spec.needs_schedules is False
    # The only nflverse source that publishes year-round (219/224 days Aug-Mar).
    assert spec.phases == refresh.ALL_PHASES
    assert spec.interval_days == 1
    assert spec.perishable is False          # the season file carries its history
    assert spec.replaces_partition is False  # append-only; no floor-before-delete
    assert spec.season_resolver is not None  # the March handover
    assert spec.applicable is not None       # "today's panel is already stored"
    assert spec.quiet_ok is True             # ~2% of days carry no panel at all
    # And it really calls the panel ingester, not the legacy one.
    calls = {}

    def _fake(conn, season, *, retrieved_as_of):
        calls.update(season=season, retrieved_as_of=retrieved_as_of)
        return 7

    with patch.object(depth_charts, "pull_depth_charts", _fake):
        ctx = refresh.IngestContext(conn=None, season=2026, retrieved_as_of="2026-07-25",
                                    today="2026-07-25")
        assert refresh._pull_depth_charts(ctx) == 7
    assert calls == {"season": 2026, "retrieved_as_of": "2026-07-25"}


def test_the_registry_never_lists_the_legacy_weekly_regime():
    """Two regimes, two tables, permanently (F2). The legacy weekly ingester is
    backfill-only: in the cadence it would be attempted daily and read stale
    forever, which is a standing alarm on something working perfectly."""
    assert "depth_charts_weekly" not in refresh.SOURCES_BY_NAME
    assert "depth_charts_weekly" in refresh.BACKFILL_ONLY_BY_NAME
    # The WHOLE backfill-only set is pinned, so a third member has to be a
    # decision rather than an accident. game_weather_archive joined in item 3.2c:
    # the registry's game_weather spec is FORECAST mode behind the ~10-day
    # Open-Meteo wall, which returns no weeks at all for a completed season, so
    # routing --with-weather through it would have been a flag that silently did
    # nothing.
    assert {s.name for s in refresh.BACKFILL_ONLY_SOURCES} == {
        "depth_charts_weekly", "game_weather_archive"}
    with pytest.raises(ValueError, match="unknown source"):
        refresh.select_sources(names=["depth_charts_weekly"])
    # ...but it is not INVISIBLE: `ingest sources` names it, because an operator
    # who cannot find it would reasonably conclude 2021-2024 has no ingester.
    out = refresh.format_sources()
    assert "BACKFILL-ONLY" in out
    assert "depth_charts_weekly" in out


def test_a_pre_panel_season_is_skipped_by_regime_not_failed(db):
    """Unblocking the source means `ingest run --season 2023` now REACHES it, and
    the panel ingester correctly refuses a legacy season. Refusing by raising
    would log `failed` on a source that is working perfectly and merely does not
    cover that year — and would list it in `ingest status --season 2023` as a
    backfill gap the cadence could close, which it cannot. The regime is stated
    in the decision instead."""
    from ziggurat.data.nfl import depth_charts, depth_charts_weekly

    spec = refresh.SOURCES_BY_NAME["depth_charts"]
    d = refresh.decide(db, spec, season=2023, today="2026-07-25", have_credentials=False)
    assert d.action == refresh.STATUS_SKIPPED
    assert "predates the dated-panel regime" in d.reason
    assert "depth_charts_weekly" in d.reason
    # the boundary is the module's own constant, not a literal repeated here
    assert depth_charts.PANEL_MIN_SEASON == depth_charts_weekly.WEEKLY_MAX_SEASON + 1
    ok = refresh.decide(db, spec, season=depth_charts.PANEL_MIN_SEASON, today="2026-07-25",
                        have_credentials=False)
    assert ok.action == "pull"
    # ...and the status report calls it n/a, not a gap someone should go and fill.
    _past_schedule(db)
    row = _verdict(db, "depth_charts", "2026-07-25", season=2023)
    assert row["verdict"] == refresh.VERDICT_NA
    out = refresh.format_status(db, season=2023, today="2026-07-25")
    assert "depth_charts_weekly" in out          # named, so it is not simply missing
    assert "NOT BACKFILLED" in out
    assert "depth_charts," not in out            # the panel spec is not in that list


def test_the_march_handover_resolves_the_season_before_the_run_is_logged(db):
    """F-B / the season_resolver seam. For two weeks each March the live panel is
    still published inside the PREVIOUS season's file (ziggurat's league year
    flips Mar 1, nflreadpy's Mar 15). If the resolver ran AFTER start_run, the log
    would record a season that was never pulled — verbatim the failure last_run's
    own docstring records — and the watermark would read NULL, so the ingester
    would rewrite the whole baseline every morning for a fortnight."""
    spec = refresh.SOURCES_BY_NAME["depth_charts"]
    d = refresh.decide(db, spec, season=2026, today="2026-03-05", have_credentials=False)
    assert d.action == "pull"
    assert d.season == 2025                      # the file that is still publishing
    assert d.scope == "season 2025"
    # ...and outside the window nothing is redirected.
    assert refresh.decide(db, spec, season=2026, today="2026-07-25",
                          have_credentials=False).season == 2026


def test_the_resolved_season_is_what_lands_in_the_run_log(db):
    """The seam is only worth having if the log, the interval gate and the pull
    all name the SAME season."""
    spec = replace(refresh.SOURCES_BY_NAME["depth_charts"],
                   pull=_ok(11), applicable=None)
    out = refresh.run_ingest(db, sources=(spec,), season=2026,
                             retrieved_as_of="2026-03-05", today="2026-03-05")
    assert out[0]["status"] == refresh.STATUS_OK
    row = db.execute("SELECT season, scope FROM nfl_ingest_runs WHERE source = 'depth_charts'"
                     ).fetchone()
    assert row["season"] == 2025 and row["scope"] == "season 2025"
    # and the interval gate reads it back under the same season the next day
    d = refresh.decide(db, spec, season=2026, today="2026-03-05", have_credentials=False)
    assert d.action == refresh.STATUS_FRESH


def test_a_source_without_a_resolver_is_untouched_by_the_seam(db):
    """Blast radius: `resolve_source_season` is the identity for all fourteen
    other sources, so nothing else can change season behind the operator's back."""
    _schedule_rows(db)
    for spec in refresh.SOURCES:
        if spec.name == "depth_charts":
            continue
        assert spec.season_resolver is None, spec.name
        assert refresh.resolve_source_season(db, spec, season=2026,
                                             today="2026-03-05") == 2026


# ------------------------------------------- F-C: partial anchors the interval


def _partial(db, source, day, season=2026, rows=18969, dropped=22):
    rid = refresh.start_run(db, batch_id="b", source=source, season=season, scope=None,
                            retrieved_as_of=day, started_at=f"{day}T05:00:00+00:00")
    refresh.finish_run(db, rid, status=refresh.STATUS_PARTIAL,
                       finished_at=f"{day}T05:00:20+00:00", rows_written=rows,
                       rows_dropped=dropped)


def test_a_partial_pull_anchors_the_interval_gate(db):
    """F-C, the measured defect. `decide` read `ok` only while `source_freshness`
    fell back to `partial`, so the four sources that are partial BY CONSTRUCTION
    on every healthy run (weekly_stats drops the same 22 null-player_id rows;
    the three ngs_* drop the week-23 Super Bowl rows) never anchored: measured
    `last_ok=False last_partial=True decide=pull status_verdict=fresh`. In-season
    that re-downloaded four whole-season parquets EVERY DAY while the report said
    everything was fine."""
    _schedule_rows(db)
    _partial(db, "weekly_stats", "2026-11-19")
    spec = refresh.SOURCES_BY_NAME["weekly_stats"]      # interval 7d
    d = refresh.decide(db, spec, season=2026, today="2026-11-20", have_credentials=False)
    assert d.action == refresh.STATUS_FRESH, d.reason
    assert _verdict(db, "weekly_stats", "2026-11-20")["verdict"] == refresh.VERDICT_FRESH
    # ...and it still comes due when the interval really has elapsed.
    assert refresh.decide(db, spec, season=2026, today="2026-11-27",
                          have_credentials=False).action == "pull"


def test_the_scheduler_and_the_report_read_one_constant(db):
    """The fix is the SHARED constant, not two matching edits. Anything that
    anchors reads _ANCHOR_STATUSES through last_landing; if a future status is
    added to one and not the other, this fails."""
    _schedule_rows(db)
    assert refresh._ANCHOR_STATUSES == (refresh.STATUS_OK, refresh.STATUS_PARTIAL)
    for status in refresh._ANCHOR_STATUSES:
        db.execute("DELETE FROM nfl_ingest_runs")
        _log(db, "adp_rankings", "2026-07-24", status=status)     # interval 1d
        spec = refresh.SOURCES_BY_NAME["adp_rankings"]
        d = refresh.decide(db, spec, season=2026, today="2026-07-24",
                           have_credentials=False)
        v = _verdict(db, "adp_rankings", "2026-07-24")["verdict"]
        assert (d.action, v) == (refresh.STATUS_FRESH, refresh.VERDICT_FRESH), status


def test_a_failed_or_empty_pull_still_does_not_anchor(db):
    """The trade-off has a floor: anything over the drop ceiling is `failed`, and
    `failed`/`empty`/`skipped` must keep retrying tomorrow."""
    _schedule_rows(db)
    spec = refresh.SOURCES_BY_NAME["adp_rankings"]
    for status in (refresh.STATUS_FAILED, refresh.STATUS_EMPTY, refresh.STATUS_SKIPPED,
                   refresh.STATUS_ABSENT, refresh.STATUS_ABANDONED):
        db.execute("DELETE FROM nfl_ingest_runs")
        _log(db, "adp_rankings", "2026-07-24", status=status)
        d = refresh.decide(db, spec, season=2026, today="2026-07-24",
                           have_credentials=False)
        assert d.action == "pull", status


def test_the_last_landing_is_the_latest_one_not_the_latest_ok(db):
    """The subtle half of F-C: 'ok Monday, partial Friday' must answer FRIDAY.
    The two-query form (prefer ok, else partial) answered Monday, which is the
    same divergence one layer out — the report would have called a Monday pull
    the current state of a table Friday rewrote."""
    _schedule_rows(db)
    _log(db, "weekly_stats", "2026-11-02")                        # ok
    _partial(db, "weekly_stats", "2026-11-19")                    # partial, later
    row = _verdict(db, "weekly_stats", "2026-11-20")
    assert row["last_ok"] == "2026-11-19"
    assert row["age_days"] == 1


def test_a_404_after_a_partial_landing_is_failed_not_absent(db):
    """The third reader of the anchor set. A PARTIAL pull proves the file exists,
    so a later 404 is a renamed/withdrawn release, not 'not published yet' — and
    weekly_stats is partial on every healthy run, so reading `ok` alone here would
    downgrade a real break to exit 0 forever."""
    _schedule_rows(db)
    _partial(db, "weekly_stats", "2026-11-19")

    def _gone(ctx):
        raise ConnectionError("Failed to download x: 404 Client Error: Not Found for url: y")

    out = refresh.run_ingest(db, sources=(_spec("weekly_stats", _gone),), season=2026,
                             retrieved_as_of="2026-11-20", today="2026-11-20")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert "already succeeded" in out[0]["reason"]


# ------------------------------------------------------- F-D: archived seasons


def test_a_completed_season_reads_archived_not_stale_then_expired(db):
    """F-D, the measured nonsense. A source backfilled for 2023 read `fresh 0d`
    the day it landed, `stale` eight days later and `expired` after twenty-two —
    none of which describes anything that happened to the data. A completed
    season's upstream files are finished artifacts, not a feed."""
    _past_schedule(db)
    _log(db, "weekly_stats", "2026-07-25", season=2023)
    for day in ("2026-07-25", "2026-08-02", "2026-08-16", "2027-06-01"):
        row = _verdict(db, "weekly_stats", day, season=2023)
        assert row["verdict"] == refresh.VERDICT_ARCHIVED, day


def test_the_current_season_still_goes_stale(db):
    """The guard on F-D: `archived` must be about the SEASON, not about age. The
    live 2026 partition is what the draft weapon reads, and silencing its
    staleness three weeks before the draft would be the worst possible fix."""
    _schedule_rows(db)
    _log(db, "adp_rankings", "2026-07-24")
    assert _verdict(db, "adp_rankings", "2026-07-26")["verdict"] == refresh.VERDICT_STALE
    assert _verdict(db, "adp_rankings", "2026-08-24")["verdict"] == refresh.VERDICT_EXPIRED


def test_archived_is_judged_on_the_season_asked_about_not_the_resolved_one(db):
    """In early March `depth_charts` pulls the PREVIOUS season's file while the
    current league year has already turned over. Judging `archived` on the
    resolved season would call today's live chart a historical artifact."""
    spec = refresh.SOURCES_BY_NAME["depth_charts"]
    assert refresh.resolve_source_season(db, spec, season=2026, today="2026-03-05") == 2025
    _log(db, "depth_charts", "2026-03-05", season=2025)
    row = _verdict(db, "depth_charts", "2026-03-05", season=2026)
    assert row["season"] == 2025          # the partition the log holds
    assert row["verdict"] == refresh.VERDICT_FRESH


def test_a_completed_season_never_says_expected_until_september(db):
    """The wording half of F-D. `ingest status --season 2023` said NOT PUBLISHED
    UPSTREAM YET ... Expected until ~Sept 10 about a season that ended years ago,
    and NEVER PULLED about sources that serve today's value and can never have
    2023's. Standing nonsense is how a report earns being ignored."""
    _past_schedule(db)
    out = refresh.format_status(db, season=2023, today="2026-07-25")
    assert "Expected until" not in out
    assert "NEVER PULLED" not in out
    assert "season 2023 is COMPLETE" in out
    # the two kinds of absence are separated, because only one can be acted on
    assert "NOT BACKFILLED" in out and "weekly_stats" in out
    unobtainable = next(ln for ln in out.splitlines() if "UNOBTAINABLE" in ln)
    assert "projections" in unobtainable and "adp_rankings" in unobtainable
    assert "weekly_stats" not in unobtainable


def test_an_archived_season_that_is_fully_backfilled_reports_nothing_to_do(db):
    _past_schedule(db)
    for spec in refresh.SOURCES:
        _log(db, spec.name, "2026-07-25", season=2023)
    out = refresh.format_status(db, season=2023, today="2026-07-25")
    assert "nothing to do" in out
    assert "EXPIRED" not in out
    assert "PERISHABLE + EXPIRED" not in out


def test_the_marginal_staleness_banner_stays_quiet_about_archived(db):
    """The consumer, updated in the SAME change (F-D says so explicitly, because
    this banner has already been fixed twice). marginal reads QUIET_VERDICTS
    rather than restating the tuple, so a fifth verdict cannot reintroduce it."""
    import inspect

    from ziggurat.core import marginal

    _past_schedule(db)
    for source in ("projections", "players"):        # the two the banner watches
        _log(db, source, "2026-07-25", season=2023)
    out = marginal._freshness_lines(db, season=2023, as_of="2023-10-01", today="2026-07-25",
                                    lines={}, roster_rows=[], pool_rows=[])
    assert not any("ingest says" in ln for ln in out), out
    # ...while a genuinely stale CURRENT season still speaks.
    _schedule_rows(db)
    _log(db, "projections", "2026-06-01", season=2026)
    out = marginal._freshness_lines(db, season=2026, as_of="2026-07-25", today="2026-07-25",
                                    lines={}, roster_rows=[], pool_rows=[])
    assert any("ingest says projections" in ln for ln in out), out

    assert refresh.VERDICT_ARCHIVED in refresh.QUIET_VERDICTS
    assert refresh.VERDICT_FRESH in refresh.QUIET_VERDICTS
    assert refresh.VERDICT_NA in refresh.QUIET_VERDICTS
    for warned in (refresh.VERDICT_STALE, refresh.VERDICT_EXPIRED, refresh.VERDICT_NEVER,
                   refresh.VERDICT_EXPIRED, refresh.VERDICT_BLOCKED):
        assert warned not in refresh.QUIET_VERDICTS
    src = inspect.getsource(marginal._freshness_lines)
    assert "QUIET_VERDICTS" in src
    assert "VERDICT_FRESH" not in src        # no restated literal tuple to drift


# ------------------------------------------- F-G: a key collision is data loss


def test_a_key_collision_counts_against_the_drop_ceiling(db):
    """F-G. `base.upsert` used to return the OFFERED row count, so rows that
    INSERT OR REPLACE overwrote on the primary key vanished with the run logged
    `ok` (measured on the legacy depth charts: 835-947 rows a season, ~700 of them
    differing only in depth_team — the depth ORDER, the one column the table
    exists for). Reporting that through note_drops(by_design=True) would have been
    worse than nothing: refresh deliberately excludes `filtered` from the ceiling,
    so a source colliding on 50% of its rows would write half the data and say
    `ok`."""
    def _collides(ctx):
        base.note_collapsed("fake", 900, 0, 1000)
        return 100

    out = refresh.run_ingest(db, sources=(_spec("z", _collides),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert refresh.run_failed(out)
    assert "900 collapsed on a primary-key collision" in out[0]["reason"]


def test_a_small_collision_is_partial_and_recorded(db):
    def _collides(ctx):
        base.note_collapsed("fake", 3, 0, 100)
        return 97

    out = refresh.run_ingest(db, sources=(_spec("z", _collides),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_PARTIAL
    assert out[0]["dropped"] == 3
    row = db.execute("SELECT rows_dropped FROM nfl_ingest_runs WHERE source='z'").fetchone()
    assert row["rows_dropped"] == 3      # the log records the loss, not just the write


def test_byte_identical_duplicates_never_reach_the_ceiling(db):
    """The other half of F-G, and the reason it is TWO counters. The legacy
    depth-chart files carry 145-207 byte-identical duplicate rows per season once
    the key is widened; storing them once loses nothing. Folding them into the
    ceiling would fail a correct pull every single time — the adp_rankings IDP
    mistake with a different label."""
    def _dupes(ctx):
        base.note_collapsed("fake", 0, 207, 37327)
        return 37120

    out = refresh.run_ingest(db, sources=(_spec("z", _dupes),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_OK
    assert out[0]["dropped"] == 0
    assert not refresh.run_failed(out)


def test_drops_and_collisions_share_one_ceiling_but_stay_named_apart(db):
    """Rule 6: 'lost 40 rows' is not actionable. 'unstampable' and 'collapsed on a
    primary-key collision' are two completely different investigations — a missing
    gameday map versus a wrong primary key — so the reason names both counts."""
    def _both(ctx):
        base.note_drops("fake", 6, 100)
        base.note_collapsed("fake", 4, 5, 100)      # 5 benign duplicates too
        return 90

    out = refresh.run_ingest(db, sources=(_spec("z", _both),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_PARTIAL       # 10/100, under the ceiling
    assert out[0]["dropped"] == 10                          # the duplicates are NOT in it
    assert "6 unstampable" in out[0]["reason"]
    assert "4 collapsed on a primary-key collision" in out[0]["reason"]


def test_a_collision_alone_can_trip_the_ceiling_that_drops_alone_would_not(db):
    """The integration is the point: before it, 19% dropped + 19% collapsed read
    `partial` (19% is under the 20% ceiling) while 38% of the pull was gone."""
    def _both(ctx):
        base.note_drops("fake", 19, 100)
        base.note_collapsed("fake", 19, 0, 100)
        return 62

    out = refresh.run_ingest(db, sources=(_spec("z", _both),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED
    assert "38/100" in out[0]["reason"]


def test_wrote_zero_and_collapsed_everything_is_failed(db):
    def _all_collapse(ctx):
        base.note_collapsed("fake", 500, 0, 500)
        return 0

    out = refresh.run_ingest(db, sources=(_spec("z", _all_collapse),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED


# -------------------------------------------- quiet_ok: the empty-day residual


def test_a_source_that_may_legitimately_write_nothing_is_skipped_not_empty(db):
    """`applicable` is pure of the network by contract (so --dry-run reports
    exactly what runs), and seeing 'upstream published no panel today' needs the
    download. Measured: 5 of 224 days in 2025 and 1 of 126 in 2026 carry no panel
    at all. Without quiet_ok each of those records `empty` -> PROBLEM_STATUSES ->
    exit 1 -> Restart=on-failure and a standing LAST ATTEMPT FAILED on a healthy
    source: the wolf-cry this module exists to avoid."""
    spec = _spec("quiet", _ok(0), quiet_ok=True)
    out = refresh.run_ingest(db, sources=(spec,), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_SKIPPED
    assert not refresh.run_failed(out)
    assert "normal day rather than a failure" in out[0]["reason"]


def test_quiet_ok_never_covers_a_pull_that_lost_rows(db):
    """The silent-zero signature stays FAILED even for a quiet_ok source: wrote 0
    AND lost rows is the measured 'ran before the spine existed' shape, and
    dressing it as a quiet day would hide exactly what 3.1b's defect class 4 is."""
    def _zero_but_dropped(ctx):
        base.note_drops("fake", 500, 500)
        return 0

    spec = _spec("quiet", _zero_but_dropped, quiet_ok=True)
    out = refresh.run_ingest(db, sources=(spec,), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_FAILED


def test_quiet_ok_is_opt_in_and_only_depth_charts_takes_it(db):
    """No existing source's behaviour changes: an empty pull is still a problem
    everywhere else, including for the four PERISHABLE ones where it means a lost
    observation."""
    assert {s.name for s in refresh.SOURCES if s.quiet_ok} == {"depth_charts"}
    out = refresh.run_ingest(db, sources=(_spec("z", _ok(0)),), season=2026,
                             retrieved_as_of="2026-07-24", today="2026-07-24")
    assert out[0]["status"] == refresh.STATUS_EMPTY
    assert refresh.run_failed(out)


def test_a_quiet_day_does_not_anchor_the_interval(db):
    """So tomorrow's run tries again rather than waiting out the interval."""
    spec = _spec("quiet", _ok(0), quiet_ok=True)
    refresh.run_ingest(db, sources=(spec,), season=2026,
                       retrieved_as_of="2026-07-24", today="2026-07-24")
    assert refresh.last_landing(db, source="quiet", season=2026) is None
    d = refresh.decide(db, spec, season=2026, today="2026-07-25", have_credentials=False)
    assert d.action == "pull"


# =====================================================================
# C8 — `ingest run --season <past>` must apply BACKFILL_EXCLUDED too
# =====================================================================
#
# The audit's C8, measured on the real network before the fix:
#
#   $ ziggurat ingest run --source projections --season 2023
#   [ ok ] projections rows=57910 (weeks 1-18)
#   sqlite> 2023 | 57910 | knowable_as_of 2026-07-25 .. 2026-07-25
#
# Fifty-eight thousand rows of TODAY's Sleeper board, filed under 2023, stamped
# knowable-today, logged `ok`. Every leakage test passes — the rows are honestly
# stamped, which is precisely why they are invisible under both views and
# indistinguishable from history in the table. It is the same shape that
# disqualified `ff_opportunity` during recon, reachable through a command the
# operator is told to run.


def _excluded_registry_sources():
    return sorted(set(refresh.BACKFILL_EXCLUDED) & set(refresh.SOURCES_BY_NAME))


@pytest.mark.parametrize("name", _excluded_registry_sources())
def test_a_past_season_is_refused_for_every_backfill_excluded_source(db, name):
    """One list, both doors. `select_backfill_sources` refuses these on
    `ingest backfill`; `decide` must refuse them on `ingest run` for any season
    before the current one."""
    _past_schedule(db)
    _schedule_rows(db, season=2026)
    spec = refresh.SOURCES_BY_NAME[name]
    d = refresh.decide(db, spec, season=2023, today="2026-07-25", have_credentials=True,
                       force=True)
    assert d.action != "pull", d.reason
    if d.action == refresh.STATUS_BLOCKED:
        # …and it quotes the RECORDED reason rather than inventing a new one, so
        # the two refusals can never drift apart.
        assert refresh.BACKFILL_EXCLUDED[name] in d.reason
    else:
        # espn_ranks is refused earlier, by the phase gate, with the more specific
        # answer. Recorded rather than asserted away: it must still not pull.
        assert name == "espn_ranks" and d.action == refresh.STATUS_SKIPPED


def test_the_past_season_refusal_writes_nothing_and_never_calls_the_pull(db):
    """Through the real orchestrator, because the defect was that the pull RAN."""
    _past_schedule(db)
    _schedule_rows(db, season=2026)
    calls = []

    def pull(ctx):
        calls.append(ctx.season)
        raise AssertionError("the pull must never be reached for a past season")

    spec = replace(refresh.SOURCES_BY_NAME["projections"], pull=pull)
    out = refresh.run_ingest(db, sources=(spec,), season=2023, retrieved_as_of="2026-07-25",
                             today="2026-07-25", force=True)

    assert calls == []
    assert out[0]["status"] == refresh.STATUS_BLOCKED
    assert out[0]["rows"] == 0
    logged = db.execute("SELECT season, status, error FROM nfl_ingest_runs").fetchone()
    assert (logged["season"], logged["status"]) == (2023, refresh.STATUS_BLOCKED)
    assert "knowable_as_of = today" in logged["error"]
    # The refusal is RECORDED, never silent: a source dropped without a row is the
    # failure this whole module exists to make visible.
    assert not refresh.run_failed(out)


def test_the_past_season_refusal_does_not_touch_the_current_season(db):
    """The fence must be a season predicate, not a source ban — these four are
    exactly the sources the daily cadence exists to pull."""
    _schedule_rows(db, season=2026)
    for name in _excluded_registry_sources():
        d = refresh.decide(db, refresh.SOURCES_BY_NAME[name], season=2026,
                           today="2026-07-25", have_credentials=True)
        assert d.action == "pull", (name, d.reason)


def test_a_non_excluded_source_still_runs_for_a_past_season(db):
    """Teeth: the fence must not become 'past seasons are refused'. The whole
    point of item 3.2c is that ten sources DO backfill."""
    _past_schedule(db)
    d = refresh.decide(db, refresh.SOURCES_BY_NAME["weekly_stats"], season=2023,
                       today="2026-07-25", have_credentials=False)
    assert d.action == "pull", d.reason
