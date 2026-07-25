"""Historical NFL backfill tests (item 3.2c).

Entirely OFFLINE. Every source here is either a fake ``pull`` closure or a real
spec with its ``pull`` swapped out by ``dataclasses.replace`` — the allowlist
checks a source's NAME, so replacing the callable is how a test injects hostile
behaviour without being refused. No test in this file touches the network.

WHAT THESE TESTS CAN AND CANNOT CATCH (the item-3.1b fixture lesson, stated
rather than assumed). They run real SQL against real SQLite and real registry
specs, so they catch: ordering, the season and allowlist fences, the run-log
grain, resumability after a mid-loop failure, the protected-partition
fingerprint, and — the highest-value one — the two-view contract on every
backfilled accessor (T2), which is the only thing standing between item 3.3 and
a silently empty read. They prove NOTHING about upstream: not that nflverse
still serves any of these files, not that a column still exists, not that a row
count is right. That evidence can only come from a real run, and one was made
against a COPY of the live database on 2026-07-25; its measured numbers are
recorded in the plan's Update block, not pinned here (pinning a live row count
in a unit test is how a green suite starts lying about a moved upstream).

Rule 5: every player/team identifier below is invented.
"""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.data.nfl import base, refresh

runner = CliRunner()

TODAY = "2026-07-25"          # nfl_season_of -> 2026, so 2021-2025 is backfillable


# ------------------------------------------------------------------ helpers


def _schedule_rows(db, season, weeks=None):
    """A minimal REG schedule so phase / week / gameday derivations resolve."""
    weeks = range(1, 19) if weeks is None else weeks
    start = date(season, 9, 9)
    rows = [
        (f"{season}_{w:02d}_AAA_BBB", season, w, "REG",
         (start + timedelta(days=(w - 1) * 7)).isoformat(), "BBB", "AAA",
         f"{season}-08-01", TODAY)
        for w in weeks
    ]
    db.executemany(
        "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, knowable_as_of, retrieved_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()


def _fake(name, pull):
    """A real backfillable spec with its pull swapped out.

    ``dataclasses.replace`` rather than a fresh SourceSpec: the allowlist resolves
    a NAME, so this is the only way to inject behaviour without being (correctly)
    refused — and it keeps every other field of the real spec, so a test cannot
    accidentally pass by relaxing `needs_schedules` or a phase set.

    Resolves BACKFILL-ONLY specs too (`depth_charts_weekly`,
    `game_weather_archive`): they are not in the cadence registry, and they are
    exactly the two the C7 orphan strands.
    """
    spec = refresh.SOURCES_BY_NAME.get(name) or refresh.BACKFILL_ONLY_BY_NAME[name]
    return replace(spec, pull=pull)


def _counting(rows=5):
    calls = []

    def pull(ctx):
        calls.append((ctx.season, ctx.retrieved_as_of))
        return rows

    pull.calls = calls
    return pull


def _run(db, sources, *, first=2021, last=2021, force=False, today=TODAY):
    return refresh.run_backfill(
        db, first=first, last=last, sources=sources,
        retrieved_as_of=today, today=today, force=force,
    )


# ------------------------------------------------------- the allowlist (2.6a)


def test_the_backfill_source_set_is_in_dependency_order_with_schedules_first():
    """Registry order IS dependency order: six sources stamp knowable_as_of from
    the gameday map and drop 100% of their rows without it (measured 19,421 of
    19,421). If schedules ever stops leading, a backfill silently stores nothing."""
    names = [s.name for s in refresh.select_backfill_sources()]
    assert names[0] == "schedules"
    assert names.index("schedules") < names.index("weekly_stats")
    assert names.index("schedules") < names.index("depth_charts_weekly")


@pytest.mark.parametrize("name", sorted(refresh.BACKFILL_EXCLUDED))
def test_every_excluded_source_is_refused_with_its_recorded_reason(name):
    """Refusing WITH the measured reason is the point: a future builder must not
    be able to add one back without reading why it is out."""
    with pytest.raises(refresh.BackfillRefused) as exc:
        refresh.select_backfill_sources(names=[name])
    assert name in str(exc.value)
    assert str(exc.value).endswith(refresh.BACKFILL_EXCLUDED[name])


def test_an_unknown_source_is_refused_not_silently_ignored():
    with pytest.raises(refresh.BackfillRefused, match="unknown source"):
        refresh.select_backfill_sources(names=["not_a_source"])


def test_a_registry_source_that_is_neither_backfillable_nor_excluded_is_named_here():
    """No THIRD silent state. Anything registered must be on the allowlist, on the
    refusal list with a reason, or explicitly named below as having a
    backfill-only counterpart — otherwise a source can quietly fall out of history
    and nothing says so.

    ``game_weather`` is the one such case: its registry spec is FORECAST mode
    behind the ~10-day Open-Meteo wall, which returns no weeks at all for a
    completed season, so the backfill uses a separate archive spec instead.
    """
    unclassified = (set(refresh.SOURCES_BY_NAME)
                    - set(refresh.BACKFILL_SOURCES)
                    - set(refresh.BACKFILL_EXCLUDED))
    assert unclassified == {"game_weather"}
    assert "game_weather_archive" in refresh.BACKFILL_ONLY_BY_NAME


def test_the_weather_archive_is_opt_in_and_off_by_default():
    """The operator's decision: ~18 minutes for five seasons, twelve times
    everything else in the backfill combined, and item 3.3 reads none of it."""
    default = {s.name for s in refresh.select_backfill_sources()}
    assert "game_weather_archive" not in default
    opted = {s.name for s in refresh.select_backfill_sources(with_weather=True)}
    assert "game_weather_archive" in opted
    # Asking for it BY NAME is itself the opt-in.
    byname = refresh.select_backfill_sources(names=["game_weather_archive"])
    assert [s.name for s in byname] == ["game_weather_archive"]


def test_the_weather_archive_pulls_the_archive_regime_not_a_past_forecast():
    """A forecast and an ERA5 actual are different observations of different
    things, and game_weather's primary key carries forecast_source so they cannot
    overwrite each other. Routing --with-weather through the registry's forecast
    spec would have been a flag that silently did nothing."""
    spec = refresh.BACKFILL_ONLY_BY_NAME["game_weather_archive"]
    assert spec.pull is refresh._pull_game_weather_archive
    assert spec.needs_schedules is True
    assert spec.perishable is False        # an archive read is replayable forever


def test_every_backfillable_source_knows_which_table_it_writes():
    """The coverage report answers 'what history do I hold'; a missing entry would
    report a healthy source as an empty one."""
    for spec in refresh.select_backfill_sources(with_weather=True):
        assert refresh._BACKFILL_TABLES.get(spec.name), spec.name


# ------------------------------------------------------- T4: season refusals


def test_pre_2021_is_refused_because_the_week_numbering_shifts(db):
    with pytest.raises(refresh.BackfillRefused, match="oldest supported season"):
        refresh.backfill_seasons(db, first=2020, last=2024, today=TODAY)


def test_the_current_season_is_refused_so_the_draft_partition_is_unreachable(db):
    """This is what makes every other fence structural rather than behavioural: no
    code path in the backfill can pass the current season to any pull."""
    with pytest.raises(refresh.BackfillRefused, match="CURRENT season"):
        refresh.backfill_seasons(db, first=2021, last=2026, today=TODAY)


def test_an_inverted_range_is_refused_rather_than_silently_doing_nothing(db):
    with pytest.raises(refresh.BackfillRefused, match="empty season range"):
        refresh.backfill_seasons(db, first=2024, last=2021, today=TODAY)


def test_a_range_is_expanded_not_treated_as_a_list(db):
    """`(first, last)` is explicit because the first design took `seasons=[...]`
    and immediately did min()/max() on it, so `[2021, 2025]` silently became five
    seasons of network traffic."""
    assert refresh.backfill_seasons(db, first=2021, last=2023, today=TODAY) == (2021, 2022, 2023)


def test_a_season_the_live_cadence_is_writing_is_refused(db):
    """The corrected fence, and NOT a duplicate of the calendar one. The installed
    systemd units PIN --season at install time, so from 2027-03-01 nfl_season_of
    starts calling season 2026 backfillable while the timers are still writing it.
    This predicate is bound to what is actually being written."""
    refresh.record_run(db, batch_id="cadence1", source="schedules", season=2024,
                       scope=None, retrieved_as_of="2026-07-20", at="2026-07-20T00:00:00+00:00",
                       status=refresh.STATUS_OK)
    with pytest.raises(refresh.BackfillRefused) as exc:
        refresh.backfill_seasons(db, first=2021, last=2025, today=TODAY)
    assert "schedules season 2024" in str(exc.value)
    assert "--force does NOT override" in str(exc.value)


def test_the_backfills_own_rows_do_not_trip_the_active_cadence_fence(db):
    """Without the batch-id exclusion this fence would refuse the backfill's own
    resume, which destroys the idempotence the whole failure story rests on."""
    refresh.record_run(db, batch_id=refresh.BACKFILL_BATCH_PREFIX + "abc",
                       source="schedules", season=2024, scope=None,
                       retrieved_as_of="2026-07-20", at="2026-07-20T00:00:00+00:00",
                       status=refresh.STATUS_OK)
    assert refresh.backfill_seasons(db, first=2021, last=2025, today=TODAY)[0] == 2021


def test_an_old_cadence_row_outside_the_window_does_not_trip_the_fence(db):
    refresh.record_run(db, batch_id="cadence1", source="schedules", season=2024,
                       scope=None, retrieved_as_of="2026-05-01",
                       at="2026-05-01T00:00:00+00:00", status=refresh.STATUS_OK)
    assert refresh.backfill_seasons(db, first=2021, last=2025, today=TODAY)


def test_force_does_not_unlock_a_season_the_cadence_is_writing(db):
    """--force means 're-pull a pair that already landed'. Letting it also unlock
    the season fences would put the protection behind the one flag an operator
    reaches for on a re-run."""
    refresh.record_run(db, batch_id="cadence1", source="schedules", season=2023,
                       scope=None, retrieved_as_of="2026-07-20",
                       at="2026-07-20T00:00:00+00:00", status=refresh.STATUS_OK)
    with pytest.raises(refresh.BackfillRefused):
        _run(db, [_fake("schedules", _counting())], first=2021, last=2025, force=True)


def test_run_backfill_re_asserts_the_allowlist_so_the_cli_cannot_be_bypassed(db):
    """Fence (a) is applied by select_backfill_sources AND again on entry: a
    caller that builds its own spec list must not be able to route espn_ranks —
    the one delete-then-write source — through this path."""
    with pytest.raises(refresh.BackfillRefused, match="espn_ranks"):
        _run(db, [_fake("espn_ranks", _counting())])


# ---------------------------------------------------- ordering (2.8) + plan


def test_seasons_run_ascending_and_schedules_leads_within_each_season(db):
    order = []

    def watcher(name):
        def pull(ctx):
            order.append((ctx.season, name))
            if name == "schedules":
                _schedule_rows(db, ctx.season)
                return 285
            return 10
        return pull

    _run(db, [_fake(n, watcher(n)) for n in ("schedules", "weekly_stats", "snap_counts")],
         first=2021, last=2023)
    assert [s for s, _ in order] == sorted(s for s, _ in order)
    for season in (2021, 2022, 2023):
        names = [n for s, n in order if s == season]
        assert names[0] == "schedules", names


def test_one_run_ingest_call_per_season_suffices_because_the_phase_is_re_derived(db):
    """The measured problem: `ingest run --season 2025 --dry-run` reported every
    phase-gated source as 'season 2025 phase unknown — schedules not ingested
    yet'. It is a PREVIEW problem, not an execution one — schedules lands inside
    the same call and decide() re-derives the phase at the moment each source is
    reached."""
    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    stats = _counting(rows=99)
    summaries = _run(db, [_fake("schedules", sched), _fake("weekly_stats", stats)])
    assert [s["status"] for s in summaries] == [refresh.STATUS_OK, refresh.STATUS_OK]
    assert stats.calls == [(2021, TODAY)]


def test_the_dry_run_preview_says_pull_where_the_real_run_will_pull(db):
    """Without the bootstrap rewrite the plan reports ten SKIPPED for a run that
    pulls ten, and the operator concludes the command does nothing."""
    sources = refresh.select_backfill_sources(names=["schedules", "weekly_stats", "injuries"])
    plan = refresh.plan_backfill(db, first=2021, last=2021, sources=sources, today=TODAY)
    actions = {d.name: d.action for _, d in plan.decisions}
    assert actions == {"schedules": "pull", "weekly_stats": "pull", "injuries": "pull"}
    assert "unlocks once schedules lands" in refresh.format_backfill_plan(plan)


def test_the_preview_does_not_promise_a_pull_that_will_still_be_skipped(db):
    """The bootstrap rewrite is sound only because every planned season is
    COMPLETE and therefore resolves `offseason` once schedules land. A source
    whose phases exclude offseason would still be skipped, so predicting `pull`
    for it would be a lie."""
    inseason_only = replace(refresh.SOURCES_BY_NAME["weekly_stats"],
                            phases=frozenset({refresh.PHASE_INSEASON}))
    sources = (refresh.SOURCES_BY_NAME["schedules"], inseason_only)
    plan = refresh.plan_backfill(db, first=2021, last=2021, sources=sources, today=TODAY)
    actions = {d.name: d.action for _, d in plan.decisions}
    assert actions["schedules"] == "pull"
    assert actions["weekly_stats"] == refresh.STATUS_SKIPPED
    # And the registry's own offseason-excluded source is not backfillable at all,
    # which is why it needs a separate archive spec (see the allowlist tests).
    assert refresh.PHASE_OFFSEASON not in refresh.SOURCES_BY_NAME["game_weather"].phases


def test_the_plan_touches_no_network_and_writes_nothing(db):
    def explode(ctx):  # pragma: no cover - must never be called
        raise AssertionError("plan_backfill called a pull")

    before = db.execute("SELECT COUNT(*) FROM nfl_ingest_runs").fetchone()[0]
    refresh.plan_backfill(db, first=2021, last=2025,
                          sources=[_fake("schedules", explode)], today=TODAY)
    assert db.execute("SELECT COUNT(*) FROM nfl_ingest_runs").fetchone()[0] == before


def test_the_plan_applies_the_same_season_fences_as_the_run(db):
    """A refused range must be reported by the dry run rather than discovered by
    the real command."""
    with pytest.raises(refresh.BackfillRefused):
        refresh.plan_backfill(db, first=2019, last=2024,
                              sources=refresh.select_backfill_sources(), today=TODAY)


# ------------------------------------------------------------ regime gating


def test_the_two_depth_chart_regimes_gate_each_other_by_season(db):
    """MEASURED on the first real 2021-2025 run: depth_charts_weekly for 2025
    recorded FAILED (PanelDepthChartFrame), which made the whole five-season
    backfill exit 1 on a source that works perfectly and merely does not cover
    that year. Both boundaries are now read from the modules' own constants."""
    _schedule_rows(db, 2024)
    _schedule_rows(db, 2025)
    panel = refresh.SOURCES_BY_NAME["depth_charts"]
    weekly = refresh.BACKFILL_ONLY_BY_NAME["depth_charts_weekly"]

    old = refresh.decide(db, panel, season=2024, today=TODAY, have_credentials=False, force=True)
    assert old.action == refresh.STATUS_SKIPPED and "predates the dated-panel" in old.reason
    new = refresh.decide(db, weekly, season=2025, today=TODAY, have_credentials=False, force=True)
    assert new.action == refresh.STATUS_SKIPPED and "past the weekly" in new.reason


def test_the_regime_boundary_is_read_from_the_modules_not_a_literal(db):
    from ziggurat.data.nfl import depth_charts, depth_charts_weekly
    assert depth_charts.PANEL_MIN_SEASON == depth_charts_weekly.WEEKLY_MAX_SEASON + 1


# --------------------------------------------- T5: the protected-partition fence


def _seed_board(db, *, season=2026, stamp="2026-07-20"):
    db.executemany(
        "INSERT OR REPLACE INTO espn_draft_ranks (board_key, espn_id, player, position, "
        "season, overall_rank, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?)",
        [(str(900 + i), str(900 + i), f"Synthetic Player {i}", "RB", season, i, stamp, stamp)
         for i in range(20)],
    )
    db.commit()


def test_an_in_place_board_rewrite_is_caught_and_a_count_fence_would_not_be(db):
    """T5, and the reason `protected_partitions` hashes CONTENT. Every non-delete
    ingester writes INSERT OR REPLACE on a key containing retrieved_as_of, so an
    overwrite at the SAME (key, stamp) leaves COUNT(*) and MAX(retrieved_as_of)
    identical while replacing every value — verbatim the 3.1b finding where a
    players pull with empty id columns took every crosswalk to zero and the run
    logged `ok`."""
    _seed_board(db)
    before_count = db.execute(
        "SELECT COUNT(*), MAX(retrieved_as_of) FROM espn_draft_ranks").fetchone()

    def evil(ctx):
        ctx.conn.execute(
            "UPDATE espn_draft_ranks SET overall_rank = overall_rank + 1000 "
            "WHERE season = 2026 AND retrieved_as_of = '2026-07-20'")
        ctx.conn.commit()
        return 285

    with pytest.raises(refresh.BackfillTouchedProtectedSeason) as exc:
        _run(db, [_fake("schedules", evil)])

    after_count = db.execute(
        "SELECT COUNT(*), MAX(retrieved_as_of) FROM espn_draft_ranks").fetchone()
    assert tuple(before_count) == tuple(after_count)      # the count fence sees nothing
    assert "espn_draft_ranks" in exc.value.changed
    assert exc.value.summaries and exc.value.summaries[0]["source"] == "schedules"


def test_the_crosswalk_collapse_shape_is_caught_by_the_output_size(db):
    """The measured 3.1b damage did not remove rows — it emptied the id columns,
    and select_as_of resolves per key so the empty row WINS. Sizing the crosswalk
    OUTPUTS is what sees that."""
    db.executemany(
        "INSERT INTO players (gsis_id, espn_id, retrieved_as_of, knowable_as_of) "
        "VALUES (?,?,?,?)",
        [(f"00-0{i:06d}", str(500 + i), "2026-07-01", "2026-07-01") for i in range(50)],
    )
    db.commit()

    def evil(ctx):
        ctx.conn.execute("UPDATE players SET espn_id = NULL")
        ctx.conn.commit()
        return 1

    with pytest.raises(refresh.BackfillTouchedProtectedSeason) as exc:
        _run(db, [_fake("schedules", evil)])
    assert "crosswalk:espn_by_gsis" in exc.value.changed
    assert "players" in exc.value.changed


def test_an_honest_backfill_leaves_every_protected_partition_identical(db):
    _seed_board(db)
    before = refresh.protected_partitions(db, protect_season=2026)
    _run(db, [_fake("schedules", _counting(rows=285))])
    assert refresh.protected_partitions(db, protect_season=2026) == before


def test_the_fingerprint_is_content_not_cardinality(db):
    """Directly: same row count, same stamp, different values -> different
    fingerprint."""
    _seed_board(db)
    a = refresh.protected_partitions(db, protect_season=2026)
    db.execute("UPDATE espn_draft_ranks SET player = 'Someone Else' WHERE board_key = '905'")
    db.commit()
    b = refresh.protected_partitions(db, protect_season=2026)
    assert a["espn_draft_ranks"] != b["espn_draft_ranks"]
    assert a["espn_draft_ranks"].split(":")[0] == b["espn_draft_ranks"].split(":")[0]


def test_the_alarm_names_a_concurrent_ingest_as_the_innocent_explanation(db):
    """A legitimate `ingest run` from the daily timer writes the 2026 partition
    mid-backfill. Naming those rows turns a five-alarm mystery into a one-line
    diagnosis."""
    _seed_board(db)

    def evil(ctx):
        refresh.record_run(ctx.conn, batch_id="the-daily-timer", source="projections",
                           season=2026, scope=None, retrieved_as_of=TODAY,
                           at="2099-01-01T00:00:00+00:00", status=refresh.STATUS_OK)
        ctx.conn.execute("UPDATE espn_draft_ranks SET overall_rank = 1")
        ctx.conn.commit()
        return 285

    with pytest.raises(refresh.BackfillTouchedProtectedSeason) as exc:
        _run(db, [_fake("schedules", evil)])
    assert "ANOTHER INGEST RAN DURING THIS BACKFILL" in str(exc.value)
    assert "the-daily-timer" in str(exc.value)


def test_no_delete_or_drop_is_executed_during_a_backfill(db):
    """The claim §2.6c rests on, verified by RUNNING rather than by reading:
    espn_ranks is the only delete-then-write path in the package and it is
    unreachable from here. (Measured the same way on a real five-season run:
    92,144 statements, zero DELETE/DROP/TRUNCATE.)"""
    seen = []
    db.set_trace_callback(seen.append)

    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    try:
        _run(db, [_fake("schedules", sched), _fake("weekly_stats", _counting())],
             first=2021, last=2022)
    finally:
        db.set_trace_callback(None)
    assert seen, "the trace callback recorded nothing"
    assert not [s for s in seen
                if s.strip().upper().startswith(("DELETE", "DROP", "TRUNCATE"))]


def test_the_backfill_refuses_to_start_while_a_run_is_in_flight(db):
    refresh.start_run(db, batch_id="live", source="projections", season=2026, scope=None,
                      retrieved_as_of=TODAY, started_at="2026-07-25T07:20:00+00:00")
    with pytest.raises(refresh.BackfillRefused, match="still marked running"):
        _run(db, [_fake("schedules", _counting())])


# ------------------------------------- C7: an interrupted backfill is recoverable
#
# The audit's C7. A killed backfill leaves a `running` row; the refusal above then
# bounces EVERY subsequent `ingest backfill`, and `start_run`'s reap only fires
# when that exact (source, season) pair runs again — which for the two
# backfill-only sources no shipped command could do. Recovery was hand-editing
# SQLite, while the docstring promised "the next run reaps it".


def _stale(minutes: int = 24 * 60) -> str:
    """A run-log timestamp far enough in the past to be an orphan by any bound.

    Real wall clock, not TODAY: `started_at` is run-log metadata stamped by
    `_utc_now()`, and the staleness bound is measured against now — a test that
    pinned a date would silently stop being stale after that date passed.
    """
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_an_orphan_from_a_killed_backfill_does_not_refuse_the_next_backfill(db):
    """C7, the headline. `depth_charts_weekly` is last in the order and one of the
    two sources NO `ingest run` can reach, so before this its orphan was terminal:
    every `ingest backfill` refused, `--force` did not help, narrowing the range
    did not help (the refusal query is unscoped), and the only exit was editing
    the database by hand."""
    refresh.start_run(db, batch_id="backfill-dead", source="depth_charts_weekly",
                      season=2021, scope=None, retrieved_as_of=TODAY,
                      started_at=_stale())

    summaries = _run(db, [_fake("schedules", _counting()),
                          _fake("depth_charts_weekly", _counting())])

    assert [s["source"] for s in summaries] == ["schedules", "depth_charts_weekly"]
    orphan = db.execute("SELECT status, error FROM nfl_ingest_runs WHERE run_id = 1").fetchone()
    assert orphan["status"] == refresh.STATUS_ABANDONED
    # Rule 6: the reaped row must say what it does NOT mean.
    assert "NOTHING WAS DELETED" in orphan["error"]


def test_a_backfill_row_that_could_still_be_alive_is_not_reaped(db):
    """The other side of the same fix, and the reason the reap has a staleness
    bound at all: a run that started moments ago may be a CONCURRENT backfill, and
    reaping its row would mis-describe a live process. It still refuses — which is
    correct — and the refusal is now actionable."""
    refresh.start_run(db, batch_id="backfill-live", source="schedules", season=2021,
                      scope=None, retrieved_as_of=TODAY, started_at=_stale(minutes=1))
    with pytest.raises(refresh.BackfillRefused, match="still marked running"):
        _run(db, [_fake("schedules", _counting())])
    assert db.execute("SELECT status FROM nfl_ingest_runs").fetchone()[0] == \
        refresh.STATUS_RUNNING


def test_a_cadence_row_is_never_reaped_by_a_backfill(db):
    """The reap is scoped to `backfill-` batches. A row from the ordinary cadence —
    even a stale one, even for a season this backfill is about to touch — belongs
    to a different process and is left exactly as it is; the backfill refuses
    instead. Item 3.1's lesson: the recovery path must not become a second way to
    destroy state."""
    # Stamped well outside BACKFILL_ACTIVE_CADENCE_DAYS so the ACTIVE-CADENCE
    # fence (a different gate, checked earlier) is not what refuses here.
    refresh.start_run(db, batch_id="the-daily-timer", source="schedules", season=2021,
                      scope=None, retrieved_as_of="2026-05-01", started_at=_stale())
    with pytest.raises(refresh.BackfillRefused):
        _run(db, [_fake("schedules", _counting())])
    assert db.execute("SELECT status FROM nfl_ingest_runs").fetchone()[0] == \
        refresh.STATUS_RUNNING


def test_the_refusal_names_the_command_that_clears_an_orphan(db):
    """Rule 6. The old message said the next `ingest run` for that source and
    season reaps it — which was false for the backfill-only sources and unusable
    for the rest (that run then tripped a different fence for thirty days). A
    refusal an operator cannot act on is a dead end, so the remedy is named."""
    refresh.start_run(db, batch_id="the-daily-timer", source="game_weather_archive",
                      season=2021, scope=None, retrieved_as_of="2026-05-01",
                      started_at=_stale())
    with pytest.raises(refresh.BackfillRefused) as exc:
        _run(db, [_fake("schedules", _counting())])
    assert "ziggurat ingest reap" in str(exc.value)
    assert "--older-than-minutes 0" in str(exc.value)
    assert "no fact table is touched" in str(exc.value)


def test_reap_leaves_a_live_row_alone_and_clears_a_dead_one(db):
    """`reap_orphan_runs` itself, at the two bounds that matter."""
    refresh.start_run(db, batch_id="b1", source="depth_charts_weekly", season=2021,
                      scope=None, retrieved_as_of=TODAY, started_at=_stale())
    refresh.start_run(db, batch_id="b2", source="game_weather_archive", season=2022,
                      scope=None, retrieved_as_of=TODAY, started_at=_stale(minutes=1))

    assert [r["source"] for r in refresh.orphan_runs(db)] == ["depth_charts_weekly"]
    reaped = refresh.reap_orphan_runs(db)
    assert [r["run_id"] for r in reaped] == [1]
    assert [r["status"] for r in
            db.execute("SELECT status FROM nfl_ingest_runs ORDER BY run_id")] == [
        refresh.STATUS_ABANDONED, refresh.STATUS_RUNNING]

    # ...and 0 minutes is how the operator says "I know that one is dead too".
    refresh.reap_orphan_runs(db, older_than_minutes=0)
    assert [r["status"] for r in
            db.execute("SELECT status FROM nfl_ingest_runs ORDER BY run_id")] == [
        refresh.STATUS_ABANDONED, refresh.STATUS_ABANDONED]


def test_a_diagnostic_run_on_a_past_season_does_not_strand_it_for_thirty_days(db):
    """C7's compounding half (1c). The active-cadence fence read ANY non-backfill
    run-log row, including the four statuses that mean the source never opened a
    socket. So one `ingest run --source weekly_stats --season 2023` — which an
    earlier refusal message told the operator to run — locked 2023 out of the
    backfill for thirty days, with --force explicitly not overriding.

    A row that actually WROTE still fences, which is the whole point of the gate.
    """
    for status in refresh._NON_WRITING_STATUSES:
        refresh.record_run(db, batch_id="hand-run", source="weekly_stats", season=2023,
                           scope=None, retrieved_as_of=TODAY, at=_stale(),
                           status=status, reason="a diagnostic run")
    assert refresh.backfill_seasons(db, first=2021, last=2025, today=TODAY) == \
        (2021, 2022, 2023, 2024, 2025)

    refresh.record_run(db, batch_id="hand-run", source="weekly_stats", season=2023,
                       scope=None, retrieved_as_of=TODAY, at=_stale(),
                       status=refresh.STATUS_OK, reason=None)
    with pytest.raises(refresh.BackfillRefused, match="actively pulling it"):
        refresh.backfill_seasons(db, first=2021, last=2025, today=TODAY)


def test_cli_reap_lists_before_it_writes(tmp_path):
    """The command the refusal names, end to end, on the source no other command
    can reach."""
    from ziggurat.data.store import connect

    path = _cli_db(tmp_path)
    conn = connect(path)
    refresh.start_run(conn, batch_id="backfill-dead", source="depth_charts_weekly",
                      season=2021, scope=None, retrieved_as_of=TODAY, started_at=_stale())
    conn.close()

    preview = runner.invoke(app, ["ingest", "reap", "--dry-run", "--path", str(path)])
    assert preview.exit_code == 0, preview.output
    assert "depth_charts_weekly" in preview.output
    assert "DRY RUN" in preview.output
    conn = connect(path)
    assert conn.execute("SELECT status FROM nfl_ingest_runs").fetchone()[0] == \
        refresh.STATUS_RUNNING
    conn.close()

    done = runner.invoke(app, ["ingest", "reap", "--path", str(path)])
    assert done.exit_code == 0, done.output
    assert "reaped 1 orphaned ingest run" in done.output
    conn = connect(path)
    assert conn.execute("SELECT status FROM nfl_ingest_runs").fetchone()[0] == \
        refresh.STATUS_ABANDONED
    conn.close()

    again = runner.invoke(app, ["ingest", "reap", "--path", str(path)])
    assert "no orphaned ingest runs" in again.output


def test_ingest_status_names_the_orphan_and_its_remedy(db):
    """Discoverability, which is the difference between a fix and a fix an
    operator can reach (Rule 6). `ingest status` lists CADENCE sources for ONE
    season; the orphan that strands a backfill is a backfill-only source in
    another season, so it must be reported outside that table or it is invisible
    on the one report CLAUDE.md tells the operator to check."""
    _schedule_rows(db, 2026)
    refresh.start_run(db, batch_id="backfill-dead", source="game_weather_archive",
                      season=2021, scope=None, retrieved_as_of=TODAY, started_at=_stale())
    out = refresh.format_status(db, season=2026, today=TODAY)
    assert "ORPHANED RUNS" in out
    assert "game_weather_archive" in out
    assert "ziggurat ingest reap" in out


def test_ingest_status_does_not_cry_wolf_about_a_live_run(db):
    """Teeth for the test above, and the standard this repo holds a report to: a
    run that started a minute ago is not an orphan, and a report that says it is
    trains the operator to skip the line."""
    _schedule_rows(db, 2026)
    refresh.start_run(db, batch_id="backfill-live", source="game_weather_archive",
                      season=2021, scope=None, retrieved_as_of=TODAY,
                      started_at=_stale(minutes=1))
    assert "ORPHANED RUNS" not in refresh.format_status(db, season=2026, today=TODAY)


# ------------------------------------------------------ T10: season-scoped reap


def test_an_in_flight_run_for_another_season_survives_a_backfills_start_run(db):
    """T10. Before the fix the reap had no season predicate, so a backfill's
    start_run for (schedules, 2021) flipped the IN-FLIGHT (schedules, 2026) row
    written by the 07:20 daily unit to `abandoned`, with a fabricated cause."""
    refresh.start_run(db, batch_id="daily", source="schedules", season=2026, scope=None,
                      retrieved_as_of=TODAY, started_at="2026-07-25T07:20:00+00:00")
    refresh.start_run(db, batch_id="bf", source="schedules", season=2021, scope=None,
                      retrieved_as_of=TODAY, started_at="2026-07-25T07:21:00+00:00")
    rows = db.execute(
        "SELECT season, status FROM nfl_ingest_runs ORDER BY run_id").fetchall()
    assert [(r["season"], r["status"]) for r in rows] == [
        (2026, refresh.STATUS_RUNNING), (2021, refresh.STATUS_RUNNING)]


def test_an_orphan_for_the_same_season_is_still_reaped(db):
    """The season predicate must narrow the reap, not disable it."""
    refresh.start_run(db, batch_id="b1", source="schedules", season=2021, scope=None,
                      retrieved_as_of="2026-07-24", started_at="2026-07-24T00:00:00+00:00")
    refresh.start_run(db, batch_id="b2", source="schedules", season=2021, scope=None,
                      retrieved_as_of=TODAY, started_at="2026-07-25T00:00:00+00:00")
    statuses = [r["status"] for r in
                db.execute("SELECT status FROM nfl_ingest_runs ORDER BY run_id")]
    assert statuses == [refresh.STATUS_ABANDONED, refresh.STATUS_RUNNING]


# ------------------------------------------------------------- the back-stamp


def test_the_backfill_never_back_stamps(db):
    """'Backfill' here means 'download old seasons TODAY'. `--allow-backfill`
    means 'write today's data under a PAST retrieved_as_of', which manufactures a
    leak for every source. Different operations, same word.

    DOUBLE-FENCED, and measured to be so: reverting `run_backfill`'s own
    hard-coded ``allow_backfill=False`` still refuses, because ``run_ingest``
    applies ``resolve_stamp`` again per run. So the assertion that has teeth is
    not merely "it raises" but "it raises with NOTHING written to the run log" —
    i.e. the outer fence fired before a single (source, season) row was opened.
    """
    with pytest.raises(ValueError, match="refusing to stamp an ingest"):
        refresh.run_backfill(db, first=2021, last=2021,
                             sources=[_fake("schedules", _counting())],
                             retrieved_as_of="2021-09-09", today=TODAY)
    assert db.execute("SELECT COUNT(*) FROM nfl_ingest_runs").fetchone()[0] == 0


def test_run_backfill_takes_no_allow_backfill_parameter():
    """Structural, not conventional: the flag cannot be passed at all."""
    import inspect
    assert "allow_backfill" not in inspect.signature(refresh.run_backfill).parameters


def test_every_backfilled_row_is_stamped_with_the_run_day(db):
    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    _run(db, [_fake("schedules", sched)], first=2021, last=2022)
    stamps = {r[0] for r in db.execute("SELECT DISTINCT retrieved_as_of FROM schedules")}
    assert stamps == {TODAY}
    knowable = {r[0] for r in db.execute("SELECT DISTINCT knowable_as_of FROM schedules")}
    assert knowable == {"2021-08-01", "2022-08-01"}      # the real historical fact time


# ------------------------------------------- T6: the needs_schedules dependency


def test_a_season_without_schedules_records_skipped_not_a_zero_row_write(db):
    """T6. Measured (probe 7 F4): calling ingest_weekly_stats for 2021 with only
    2026 schedules present writes 0 rows, drops 18,969 and raises nothing. Through
    run_backfill it is unreachable twice — this fence, and run_ingest's
    `wrote 0 and lost everything => FAILED`."""
    stats = _counting()
    summaries = _run(db, [_fake("weekly_stats", stats)])
    assert [s["status"] for s in summaries] == [refresh.STATUS_SKIPPED]
    assert "schedules not ingested" in summaries[0]["reason"]
    assert stats.calls == []


def test_wrote_zero_but_lost_everything_is_failed_on_this_path_too(db):
    _schedule_rows(db, 2021)

    def zero(ctx):
        base.note_drops("weekly_stats", 18969, 18969)
        return 0

    summaries = _run(db, [_fake("weekly_stats", zero)])
    assert summaries[0]["status"] == refresh.STATUS_FAILED


# --------------------------------------------------- failure mid-loop + resume


def test_a_season_that_dies_does_not_stop_the_seasons_after_it(db):
    def sched(ctx):
        if ctx.season == 2022:
            raise RuntimeError("nflverse hiccup")
        _schedule_rows(db, ctx.season)
        return 285

    summaries = _run(db, [_fake("schedules", sched)], first=2021, last=2023)
    got = {(s["season"], s["status"]) for s in summaries}
    assert got == {(2021, refresh.STATUS_OK), (2022, refresh.STATUS_FAILED),
                   (2023, refresh.STATUS_OK)}


def test_re_running_after_a_failure_re_pulls_only_the_pair_that_did_not_land(db):
    attempts = []
    failures = {"2022": 1}

    def sched(ctx):
        attempts.append(ctx.season)
        if failures.get(str(ctx.season)):
            failures[str(ctx.season)] -= 1
            raise RuntimeError("nflverse hiccup")
        _schedule_rows(db, ctx.season)
        return 285

    _run(db, [_fake("schedules", sched)], first=2021, last=2023)
    assert attempts == [2021, 2022, 2023]
    attempts.clear()
    summaries = _run(db, [_fake("schedules", sched)], first=2021, last=2023)
    assert attempts == [2022]                       # only the pair that never landed
    statuses = {s["season"]: s["status"] for s in summaries}
    assert statuses == {2021: refresh.STATUS_FRESH, 2022: refresh.STATUS_OK,
                        2023: refresh.STATUS_FRESH}


def test_a_partial_landing_counts_as_landed_so_it_is_not_re_pulled_forever(db):
    """weekly_stats drops the same 22 null-player_id rows out of every season file
    and the three ngs_* drop the week-23 Super Bowl rows, so those four are
    `partial` on every correct run. Anchoring on `ok` alone would re-download four
    whole-season parquets on every resume."""
    _schedule_rows(db, 2021)
    calls = []

    def partial(ctx):
        calls.append(ctx.season)
        base.note_drops("weekly_stats", 22, 18969)
        return 18947

    _run(db, [_fake("weekly_stats", partial)])
    summaries = _run(db, [_fake("weekly_stats", partial)])
    assert calls == [2021]
    assert summaries[0]["status"] == refresh.STATUS_FRESH


def test_an_upstream_absent_pair_is_re_attempted_rather_than_treated_as_landed(db):
    _schedule_rows(db, 2021)
    calls = []

    def missing(ctx):
        calls.append(ctx.season)
        raise ConnectionError("Failed to download x: 404 Client Error: Not Found for url: y")

    first = _run(db, [_fake("weekly_stats", missing)])
    assert first[0]["status"] == refresh.STATUS_ABSENT
    _run(db, [_fake("weekly_stats", missing)])
    assert calls == [2021, 2021]


# --------------------------------------------------------- T7: idempotence


def test_a_second_run_the_same_day_touches_no_network_and_records_fresh(db):
    """T7. The gate is 'already landed', NOT the scheduler's interval: the
    interval is 1 day for schedules/injuries/game_odds, so a resume two days later
    would re-pull them and append a whole second retrieved_as_of partition
    (~+48 MB across five seasons)."""
    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    sched_stub = _counting(rows=285)

    def first_pull(ctx):
        sched_stub(ctx)
        return sched(ctx)

    _run(db, [_fake("schedules", first_pull)], first=2021, last=2023)
    assert len(sched_stub.calls) == 3
    summaries = _run(db, [_fake("schedules", first_pull)], first=2021, last=2023)
    assert len(sched_stub.calls) == 3                       # zero further calls
    assert {s["status"] for s in summaries} == {refresh.STATUS_FRESH}


def test_a_resume_a_different_day_still_does_not_append_a_second_partition(db):
    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    stub = _counting(rows=285)

    def pull(ctx):
        stub(ctx)
        return sched(ctx)

    _run(db, [_fake("schedules", pull)], today=TODAY)
    _run(db, [_fake("schedules", pull)], today="2026-07-27")
    assert len(stub.calls) == 1
    stamps = [r[0] for r in db.execute("SELECT DISTINCT retrieved_as_of FROM schedules")]
    assert stamps == [TODAY]


def test_force_re_pulls_a_landed_pair_under_todays_stamp(db):
    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    stub = _counting(rows=285)

    def pull(ctx):
        stub(ctx)
        return sched(ctx)

    _run(db, [_fake("schedules", pull)])
    _run(db, [_fake("schedules", pull)], force=True)
    assert len(stub.calls) == 2
    assert "--force" in refresh.format_backfill_plan(refresh.plan_backfill(
        db, first=2021, last=2021, sources=[refresh.SOURCES_BY_NAME["schedules"]],
        today=TODAY, force=True))


# --------------------------------------------------------------- the run log


def test_one_run_log_row_per_source_per_season_under_one_batch(db):
    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    summaries = _run(db, [_fake("schedules", sched), _fake("weekly_stats", _counting())],
                     first=2021, last=2022)
    batches = {s["batch_id"] for s in summaries}
    assert len(batches) == 1
    assert batches.pop().startswith(refresh.BACKFILL_BATCH_PREFIX)
    pairs = {(r["source"], r["season"]) for r in db.execute(
        "SELECT source, season FROM nfl_ingest_runs")}
    assert pairs == {("schedules", 2021), ("schedules", 2022),
                     ("weekly_stats", 2021), ("weekly_stats", 2022)}


def test_a_caller_supplied_batch_id_is_still_prefixed(db):
    """Otherwise a plain id makes the backfill's own re-run look like an active
    cadence and the fence refuses it."""
    summaries = refresh.run_backfill(
        db, first=2021, last=2021, sources=[_fake("schedules", _counting())],
        retrieved_as_of=TODAY, today=TODAY, batch_id="mine",
    )
    assert summaries[0]["batch_id"] == refresh.BACKFILL_BATCH_PREFIX + "mine"


def test_progress_is_reported_so_a_long_run_is_not_indistinguishable_from_a_hang(db):
    lines = []
    _run(db, [_fake("schedules", _counting(rows=285))], first=2021, last=2022)
    refresh.run_backfill(db, first=2021, last=2022,
                         sources=[_fake("schedules", _counting(rows=285))],
                         retrieved_as_of=TODAY, today=TODAY, progress=lines.append)
    assert any("season 2021" in ln for ln in lines)
    assert any("season 2022" in ln for ln in lines)


# ==========================================================================
# T2 — THE BACKFILL CONTRACT TEST
# ==========================================================================
#
# The single highest-value test in the item, and the only thing that catches the
# silent-empty class. Backfilled rows carry `retrieved_as_of = today` and a PAST
# `knowable_as_of`, so the safe-default `historical` view — which gates RETRIEVAL
# time as well as knowledge time — returns NOTHING, silently. Measured on seven
# accessors during recon and reproduced on all thirteen against real backfilled
# data on 2026-07-25 (weekly_stats 2023: 0 vs 6,002; snap_counts 2024: 0 vs
# 11,589; injuries 2023: 0 vs 2,430; usage_deltas 2025 wk9: 0 vs 83).
#
# The failure mode is an empty result that reads as "item 3.3 is broken" rather
# than "wrong view", which is why this is a shipped contract test and not a
# one-time check. It must NOT be "fixed" by back-stamping — `resolve_stamp`
# already refuses that, and it would manufacture a leak.

_PAST_KNOWABLE = "2023-10-15"
_BACKFILL_STAMP = "2026-07-25"


def _row(**cols):
    return cols


_T2_CASES = {
    "weekly_stats": (
        "weekly_stats",
        _row(player_id="00-0099001", season=2023, week=6, position="RB"),
        "get_weekly_stats", {"season": 2023},
    ),
    "snap_counts": (
        "snap_counts",
        _row(pfr_player_id="SyntPl00", season=2023, week=6, team="AAA"),
        "get_snap_counts", {"season": 2023},
    ),
    "team_defense": (
        "team_defense",
        _row(season=2023, week=6, team="AAA"),
        "get_team_defense", {"season": 2023},
    ),
    "ngs_passing": (
        "ngs_passing",
        _row(player_gsis_id="00-0099001", season=2023, week=6),
        "get_ngs_passing", {"season": 2023},
    ),
    "ngs_rushing": (
        "ngs_rushing",
        _row(player_gsis_id="00-0099001", season=2023, week=6),
        "get_ngs_rushing", {"season": 2023},
    ),
    "ngs_receiving": (
        "ngs_receiving",
        _row(player_gsis_id="00-0099001", season=2023, week=6),
        "get_ngs_receiving", {"season": 2023},
    ),
    "injuries": (
        "injuries",
        _row(gsis_id="00-0099001", season=2023, week=6),
        "get_injuries", {"season": 2023},
    ),
    "game_odds": (
        "game_odds",
        _row(game_id="2023_06_AAA_BBB", season=2023, week=6),
        "get_game_odds", {"season": 2023},
    ),
    "schedules": (
        "schedules",
        _row(game_id="2023_06_AAA_BBB", season=2023, week=6, game_type="REG",
             gameday=_PAST_KNOWABLE, home_team="BBB", away_team="AAA"),
        "get_schedule", {"season": 2023},
    ),
    "depth_charts_weekly": (
        "depth_charts_weekly",
        _row(season=2023, week=6, game_type="REG", club_code="AAA", formation="Offense",
             position="RB", depth_position="RB", depth_team="1", gsis_id="00-0099001"),
        "get_depth_chart_week", {"season": 2023, "week": 6},
    ),
    "depth_chart_slots": (
        "depth_chart_slots",
        _row(season=2023, team="AAA", pos_grp_id="21", pos_id="7", pos_rank=1,
             observed_at=f"{_PAST_KNOWABLE}T07:14:22Z", pos_abb="RB",
             espn_id="900001"),
        "get_depth_chart", {"season": 2023},
    ),
}

_T2_MODULES = {
    "weekly_stats": "weekly_stats", "snap_counts": "snap_counts",
    "team_defense": "team_defense", "ngs_passing": "ngs", "ngs_rushing": "ngs",
    "ngs_receiving": "ngs", "injuries": "injuries", "game_odds": "game_odds",
    "schedules": "schedules", "depth_charts_weekly": "depth_charts_weekly",
    "depth_chart_slots": "depth_charts",
}


def _accessor(case: str):
    import importlib
    module = importlib.import_module(f"ziggurat.data.nfl.{_T2_MODULES[case]}")
    return getattr(module, _T2_CASES[case][2])


def _insert_backfilled(db, case: str):
    table, cols, _, _ = _T2_CASES[case]
    cols = dict(cols, knowable_as_of=_PAST_KNOWABLE, retrieved_as_of=_BACKFILL_STAMP)
    db.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) "  # noqa: S608 - names are literals above
        f"VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )
    db.commit()


@pytest.mark.parametrize("case", sorted(_T2_CASES))
def test_backfilled_history_is_invisible_under_the_default_view(db, case):
    """(i) The trap itself: `accessor(as_of=<past>)` returns NOTHING, silently."""
    _insert_backfilled(db, case)
    _, _, _, kwargs = _T2_CASES[case]
    assert _accessor(case)(db, as_of=_PAST_KNOWABLE, **kwargs) == []


@pytest.mark.parametrize("case", sorted(_T2_CASES))
def test_backfilled_history_is_readable_through_latest_truth(db, case):
    """(ii) And the escape: `base.latest_truth(accessor)` is the read path a
    backtest cannot forget."""
    _insert_backfilled(db, case)
    _, _, _, kwargs = _T2_CASES[case]
    rows = base.latest_truth(_accessor(case))(db, as_of=_PAST_KNOWABLE, **kwargs)
    assert len(rows) == 1, case


@pytest.mark.parametrize("case", sorted(_T2_CASES))
def test_latest_truth_still_hides_a_fact_that_was_not_yet_knowable(db, case):
    """(iii) latest_truth relaxes the RETRIEVAL gate and nothing else. Fact-time
    protection is unchanged, which is what keeps a Phase-4 backtest honest."""
    _insert_backfilled(db, case)
    _, _, _, kwargs = _T2_CASES[case]
    day_before = (date.fromisoformat(_PAST_KNOWABLE) - timedelta(days=1)).isoformat()
    rows = base.latest_truth(_accessor(case))(db, as_of=day_before, **kwargs)
    assert rows == [], case


def test_the_contract_covers_every_source_the_backfill_writes():
    """The parameterization must not drift away from the source set. A backfilled
    accessor with no T2 case is exactly the silent-empty hole this catches."""
    written = {refresh._BACKFILL_TABLES[s.name]
               for s in refresh.select_backfill_sources(with_weather=True)}
    covered = {t for t, _, _, _ in _T2_CASES.values()}
    # game_weather is opt-in and its rows are context-only (item 4.2 owns it).
    assert written - covered == {"game_weather"}


# --------------------------------------------------------- T2, second helping
#
# VERIFICATION-PHASE ADDITION (2026-07-25). The guard above is derived from
# `_BACKFILL_TABLES`, which maps ONE table to each source — and that mapping is
# structurally blind to two backfilled read paths, both measured empty under the
# default view on the real backfilled database:
#
#   depth_chart_panels  `depth_charts` writes TWO tables, not one. Measured on a
#                       backfilled copy: get_depth_chart_observed(as_of=
#                       '2025-11-04', season=2025) -> None under `historical`,
#                       and the real panel row (observed 2025-11-04T07:15:56Z,
#                       32 teams, 2,269 slots) under `latest_truth`. `None` is a
#                       worse silent-empty than `[]`: a Rule-6 consumer asking
#                       "when was this chart observed" gets no answer at all and
#                       has nothing to print the caveat from.
#   usage_deltas        not a table accessor at all — a DERIVED read over
#                       weekly_stats + snap_counts, and the one item 3.3 actually
#                       calls (design note §7.3.2). Measured 0 rows under
#                       `historical` vs 83 under `latest_truth` at
#                       (2025, week 9, RB), which is exactly the number recon
#                       measured before the backfill existed.
#
# They are here rather than in `_T2_CASES` because neither returns a list of
# rows for a single table, which is what that parameterization's insert-and-count
# shape assumes. The contract asserted is identical: invisible under
# `historical`, visible under `latest_truth`, still gated on fact time.


def _backfilled_panel(db):
    db.execute(
        "INSERT INTO depth_chart_panels (season, observed_at, n_teams, n_slots, "
        "n_changes, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?)",
        (2023, f"{_PAST_KNOWABLE}T07:14:22Z", 32, 2269, 219,
         _BACKFILL_STAMP, _PAST_KNOWABLE),
    )
    db.commit()


def test_the_backfilled_panel_row_is_invisible_under_the_default_view(db):
    from ziggurat.data.nfl import depth_charts

    _backfilled_panel(db)
    assert depth_charts.get_depth_chart_observed(
        db, as_of=_PAST_KNOWABLE, season=2023) is None


def test_the_backfilled_panel_row_is_readable_through_latest_truth(db):
    from ziggurat.data.nfl import depth_charts

    _backfilled_panel(db)
    row = base.latest_truth(depth_charts.get_depth_chart_observed)(
        db, as_of=_PAST_KNOWABLE, season=2023)
    assert row is not None and row["observed_at"] == f"{_PAST_KNOWABLE}T07:14:22Z"


def test_latest_truth_still_hides_a_panel_that_was_not_yet_published(db):
    from ziggurat.data.nfl import depth_charts

    _backfilled_panel(db)
    day_before = (date.fromisoformat(_PAST_KNOWABLE) - timedelta(days=1)).isoformat()
    assert base.latest_truth(depth_charts.get_depth_chart_observed)(
        db, as_of=day_before, season=2023) is None


def _backfilled_usage(db):
    """Two weeks of one player's line, backfill-stamped: the minimum a delta needs."""
    _schedule_rows(db, 2023, weeks=[5, 6])
    gameday = {5: "2023-10-08", 6: "2023-10-15"}
    for week, snaps in ((5, 20), (6, 55)):
        db.execute(
            "INSERT INTO weekly_stats (player_id, season, week, position, recent_team, "
            "carries, targets, knowable_as_of, retrieved_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
            ("00-0099001", 2023, week, "RB", "AAA", snaps // 4, snaps // 5,
             gameday[week], _BACKFILL_STAMP),
        )
        db.execute(
            "INSERT INTO snap_counts (pfr_player_id, gsis_id, season, week, team, "
            "offense_snaps, offense_pct, knowable_as_of, retrieved_as_of) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("SyntPl00", "00-0099001", 2023, week, "AAA", snaps, snaps / 100.0,
             gameday[week], _BACKFILL_STAMP),
        )
    db.commit()


def test_the_derived_usage_read_item_3_3_calls_is_invisible_under_the_default_view(db):
    """The one that reads as 'item 3.3 is broken': every input row is present and
    correct, and the answer is an empty list with no error anywhere."""
    from ziggurat.data.nfl import usage

    _backfilled_usage(db)
    assert usage.usage_deltas(db, as_of="2023-10-15", season=2023, week=6) == []


def test_the_derived_usage_read_is_readable_through_latest_truth(db):
    from ziggurat.data.nfl import usage

    _backfilled_usage(db)
    rows = base.latest_truth(usage.usage_deltas)(
        db, as_of="2023-10-15", season=2023, week=6)
    assert len(rows) == 1
    assert rows[0]["d_offense_snaps"] == 35


def test_latest_truth_still_hides_a_usage_week_that_had_not_been_played(db):
    from ziggurat.data.nfl import usage

    _backfilled_usage(db)
    assert base.latest_truth(usage.usage_deltas)(
        db, as_of="2023-10-14", season=2023, week=6) == []


def test_a_source_that_writes_a_second_table_cannot_escape_the_t2_contract(db):
    """`_BACKFILL_TABLES` names one table per source; `depth_charts` writes two.

    Traced, not asserted from a comment: run the real ingester over the committed
    panel fixture with a statement trace attached and read back which tables it
    inserted into. If a future change gives a backfilled source a THIRD table,
    this fails and says so — which is the only mechanism that can see the hole,
    because the drift guard above reads a hand-written one-to-one map.
    """
    import re

    from tests.conftest import load_nfl_fixture
    from ziggurat.data.nfl import depth_charts

    seen: set[str] = set()

    def trace(sql):
        m = re.match(r"\s*INSERT(?: OR \w+)? INTO (\w+)", sql, re.IGNORECASE)
        if m:
            seen.add(m.group(1))

    df = load_nfl_fixture("depth_chart_panel")
    db.set_trace_callback(trace)
    try:
        depth_charts.ingest_depth_charts(
            db, df, season=2025, retrieved_as_of=_BACKFILL_STAMP)
    finally:
        db.set_trace_callback(None)

    assert seen == {"depth_chart_slots", "depth_chart_panels"}
    covered = ({t for t, _, _, _ in _T2_CASES.values()}
               | {"depth_chart_panels"})          # the three tests just above
    assert seen <= covered, f"{seen - covered} is written but has no T2 contract test"


# ------------------------------------------------------------------ coverage


def test_coverage_reports_stored_rows_and_the_knowable_span(db):
    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    _run(db, [_fake("schedules", sched)], first=2021, last=2022)
    rows = refresh.backfill_coverage(
        db, first=2021, last=2022, sources=[refresh.SOURCES_BY_NAME["schedules"]])
    by_season = {r["season"]: r for r in rows}
    assert by_season[2021]["rows"] == 18
    assert by_season[2021]["first_knowable"] == "2021-08-01"
    assert by_season[2021]["stamps"] == 1
    assert by_season[2021]["status"] == refresh.STATUS_OK


def test_coverage_reads_ungated_because_it_answers_an_operational_question(db):
    """A coverage report that applied an as-of gate would report every backfilled
    row as absent — the exact confusion the two-view trap is about."""
    _insert_backfilled(db, "weekly_stats")
    rows = refresh.backfill_coverage(
        db, first=2023, last=2023, sources=[refresh.SOURCES_BY_NAME["weekly_stats"]])
    assert rows[0]["rows"] == 1


def test_coverage_names_a_source_that_ran_and_stored_nothing(db):
    _schedule_rows(db, 2021)
    _run(db, [_fake("weekly_stats", lambda ctx: 0)])
    rows = refresh.backfill_coverage(
        db, first=2021, last=2021, sources=[refresh.SOURCES_BY_NAME["weekly_stats"]])
    assert "RAN BUT STORED NOTHING" in refresh.format_coverage(rows)


def test_coverage_is_silent_about_a_season_a_source_was_skipped_for(db):
    """`depth_charts` is skipped by regime for 2021-2024. Listing that as a gap
    would be the wolf-cry this module exists to avoid."""
    _schedule_rows(db, 2021)
    _run(db, [refresh.SOURCES_BY_NAME["depth_charts"]])
    rows = refresh.backfill_coverage(
        db, first=2021, last=2021, sources=[refresh.SOURCES_BY_NAME["depth_charts"]])
    assert rows[0]["status"] == refresh.STATUS_SKIPPED
    assert "RAN BUT STORED NOTHING" not in refresh.format_coverage(rows)


# ---------------------------------------------------------------- reporting


def test_the_plan_says_the_backfilled_rows_need_latest_truth(db):
    """Rule 6: the operator cannot smell an absurd output, and 'zero rows' is the
    most plausible-looking absurd output there is."""
    plan = refresh.plan_backfill(db, first=2021, last=2021,
                                 sources=refresh.select_backfill_sources(), today=TODAY)
    assert "latest_truth" in refresh.format_backfill_plan(plan)


def test_the_plan_says_what_with_weather_would_cost(db):
    plan = refresh.plan_backfill(db, first=2021, last=2021,
                                 sources=refresh.select_backfill_sources(), today=TODAY)
    out = refresh.format_backfill_plan(plan)
    assert "--with-weather" in out and "18 minutes" in out


def test_the_run_report_groups_by_season_and_names_the_problem_pairs(db):
    _schedule_rows(db, 2021)
    summaries = _run(db, [_fake("weekly_stats", lambda ctx: 0)])
    out = refresh.format_backfill_run(summaries)
    assert "season 2021" in out and "PROBLEMS: weekly_stats/2021" in out


def test_the_run_report_reads_in_dependency_order_not_execution_order(db):
    """run_backfill writes the already-landed `fresh` rows in a pre-pass, so the
    run log's run_id order is fresh-then-pulled. A reader comparing two seasons
    wants the same source on the same line each time."""
    def sched(ctx):
        _schedule_rows(db, ctx.season)
        return 285

    _run(db, [_fake("schedules", sched)])
    summaries = _run(db, [_fake("schedules", sched), _fake("weekly_stats", _counting())])
    lines = [ln for ln in refresh.format_backfill_run(summaries).splitlines()
             if "schedules" in ln or "weekly_stats" in ln]
    assert "schedules" in lines[0] and "weekly_stats" in lines[1]


def test_a_completed_season_never_tells_the_operator_to_run_a_refused_command(db):
    """MEASURED after the first real backfill: `ingest status --season 2021` said
    NOT BACKFILLED about `players`, which `ingest backfill` refuses by name with a
    recorded reason. Sending an operator to a command that will refuse them is how
    a report earns being ignored."""
    _schedule_rows(db, 2021)
    _run(db, [_fake("schedules", lambda ctx: 285)])
    out = refresh.format_status(db, season=2021, today=TODAY)
    assert "NOT BACKFILLABLE: players" in out
    backfilled = next((ln for ln in out.splitlines() if "NOT BACKFILLED" in ln), "")
    assert "players" not in backfilled


# ---------------------------------------------------------------------- CLI


def _cli_db(tmp_path):
    from ziggurat.data.store import open_db
    path = tmp_path / "cli.sqlite"
    conn = open_db(path)
    conn.close()
    return path


def test_cli_backfill_dry_run_touches_no_network_and_writes_nothing(tmp_path):
    path = _cli_db(tmp_path)
    result = runner.invoke(app, ["ingest", "backfill", "--dry-run", "--path", str(path),
                                 "--first", "2021", "--last", "2022"])
    assert result.exit_code == 0, result.output
    assert "backfill plan (dry run" in result.output
    from ziggurat.data.store import connect
    conn = connect(path)
    assert conn.execute("SELECT COUNT(*) FROM nfl_ingest_runs").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("args,needle", [
    (["--first", "2019"], "oldest supported season"),
    (["--last", "2026"], "CURRENT season"),
    (["--source", "espn_ranks"], "delete-then-write"),
    (["--source", "players"], "crosswalk is season-agnostic"),
    (["--source", "nope"], "unknown source"),
])
def test_cli_backfill_refusals_exit_two_with_the_reason(tmp_path, args, needle):
    path = _cli_db(tmp_path)
    result = runner.invoke(app, ["ingest", "backfill", "--dry-run",
                                 "--path", str(path), *args])
    assert result.exit_code == 2, result.output
    assert needle in result.output


def test_cli_coverage_prints_the_stored_history(tmp_path):
    path = _cli_db(tmp_path)
    result = runner.invoke(app, ["ingest", "coverage", "--path", str(path),
                                 "--first", "2021", "--last", "2021"])
    assert result.exit_code == 0, result.output
    assert "nfl history coverage" in result.output
    assert "weekly_stats" in result.output


def test_cli_backfill_help_separates_the_two_meanings_of_backfill(tmp_path):
    """An operator WILL conflate `ingest backfill` with `ingest run
    --allow-backfill`; the help text has to say they are different."""
    result = runner.invoke(app, ["ingest", "backfill", "--help"])
    assert "allow-backfill" in result.output
    assert "latest_truth" in result.output


def test_the_cli_holds_no_backfill_policy(tmp_path):
    """Rule 3. The command parses, calls and prints: the source set, the ordering,
    the fences and the fingerprint all live in refresh.py."""
    import inspect

    from ziggurat.cli import main
    src = inspect.getsource(main.ingest_backfill)
    for banned in ("BACKFILL_SOURCES", "for season in", "protected_partitions", "2021,"):
        assert banned not in src, banned


def test_cli_surfaces_a_migration_collapse_alarm(tmp_path):
    """A migration runs inside open_db on every command so it may not raise, which
    forces the 007 rebuilds to use INSERT OR REPLACE — so a collision COLLAPSES.
    store.migration_alerts records that as a positive fact; without an echo the
    alarm exists and nobody ever sees it."""
    from ziggurat.data.store import connect
    path = _cli_db(tmp_path)
    conn = connect(path)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                 "('007_depth_charts_collapsed', '3 rows lost on the rebuild')")
    conn.commit()
    conn.close()
    result = runner.invoke(app, ["ingest", "status", "--path", str(path)])
    assert "MIGRATION ALERT [007_depth_charts_collapsed]" in result.output
    assert "3 rows lost on the rebuild" in result.output
