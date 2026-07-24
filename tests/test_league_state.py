"""League state mappers, ingest, and as-of accessors (item 3.1).

Offline: the four ``ziggurat.league.source.fetch_*`` network seams are never
called. League payloads come from the synthetic ``league_world`` factory (rule 5:
the real league's team names and managers are colleagues and never enter a
committed file); the real-SHAPE check rides on the scrubbed pre-draft pool slice
in ``tests/fixtures/espn/league_player_pool.json``, which contains public player
data only (every entry is a free agent — there is no roster to leak pre-draft).

The leakage tests here are the standing rule-1 requirement: every accessor has one.
"""

import json
from pathlib import Path

import pytest

from ziggurat.data.nfl import base
from ziggurat.league import state

_POOL_FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "league_player_pool.json"


def _fixture_pool():
    return json.loads(_POOL_FIXTURE.read_text())


def _ingest(db, payload, pool, *, day, season=2026):
    """Write one full snapshot the way sync.run_sync does."""
    counts = state.ingest_league_state(db, payload, retrieved_as_of=day, season=season)
    player_counts = state.ingest_player_state(
        db, pool, retrieved_as_of=day, season=season,
        roster=state.roster_index(payload), scoring_period=payload.get("scoringPeriodId"),
    )
    return {**counts, **player_counts}


# ------------------------------------------------------------ fixture / scrub


def test_pool_fixture_is_well_formed_and_carries_no_league_private_data():
    entries = _fixture_pool()
    assert len(entries) >= 20
    for entry in entries:
        # rule 5: a pre-draft slice has no roster context — assert it stayed that way.
        assert entry.get("onTeamId") in (0, None)
        assert entry.get("status") in (None, "FREEAGENT", "WAIVERS")
        player = entry["player"]
        assert {"id", "fullName", "defaultPositionId", "ownership"} <= set(player)
    positions = {state.DEFPOS.get(e["player"]["defaultPositionId"]) for e in entries}
    assert {"QB", "RB", "WR", "TE"} <= positions


def test_real_pool_slice_maps_cleanly():
    """The captured live shape maps without special-casing (the point of a real fixture)."""
    rows = [state.map_player_entry(e, season=2026, scoring_period=0) for e in _fixture_pool()]
    mapped = [r for r in rows if r is not None]
    assert len(mapped) >= 20
    for row in mapped:
        assert row["espn_player_id"].lstrip("-").isdigit()
        assert row["position"] in {"QB", "RB", "WR", "TE", "K", "D/ST"}
        assert row["on_team_id"] is None  # pre-draft: everyone is a free agent
        assert row["percent_owned"] is not None


# --------------------------------------------------------------------- mappers


def test_free_agent_sentinel_becomes_null(league_world):
    _, pool = league_world(holdings={"1000": 4})
    rostered = state.map_player_entry(pool[0], season=2026)
    free = state.map_player_entry(pool[1], season=2026)
    assert rostered["on_team_id"] == 4
    assert free["on_team_id"] is None  # ESPN's 0 sentinel must not survive as 0


def test_non_league_position_is_skipped():
    entry = {"id": 9, "onTeamId": 0, "player": {"id": 9, "defaultPositionId": 10}}  # LB
    assert state.map_player_entry(entry, season=2026) is None


def test_decode_slot_known_and_unknown(caplog):
    assert state.decode_slot(0) == "QB"
    assert state.decode_slot(23) == "FLEX"
    assert state.decode_slot(21) == "IR"
    assert state.decode_slot(None) is None
    with caplog.at_level("WARNING"):
        assert state.decode_slot(99) == "99"  # stored raw, never coerced to bench
    assert "unknown lineupSlotId" in caplog.text


def test_is_starting_slot():
    assert state.is_starting_slot("FLEX") and state.is_starting_slot("D/ST")
    assert not state.is_starting_slot("BE")
    assert not state.is_starting_slot("IR")


def test_map_team_reads_record_waiver_and_counters(league_world):
    payload, _ = league_world()
    row = state.map_team(payload["teams"][2], season=2026, scoring_period=3)
    assert row["team_id"] == 3
    assert row["waiver_rank"] == 3
    assert row["wins"] == 0 and row["losses"] == 2
    assert row["points_for"] == pytest.approx(103.0)
    assert row["acquisitions"] == 3 and row["drops"] == 3
    assert row["is_transaction_locked"] == 0


def test_map_matchup_requires_a_home_side():
    assert state.map_matchup({"matchupPeriodId": 1, "away": {"teamId": 2}}, season=2026) is None
    row = state.map_matchup(
        {"matchupPeriodId": 4, "winner": "HOME",
         "home": {"teamId": 1, "totalPoints": 100.0}, "away": {"teamId": 2, "totalPoints": 90.0}},
        season=2026,
    )
    assert (row["week"], row["home_team_id"], row["away_team_id"]) == (4, 1, 2)


def test_map_transaction_emits_one_row_per_item():
    raw = {
        "id": "TX1", "teamId": 4, "type": "WAIVER", "status": "EXECUTED",
        "scoringPeriodId": 5, "proposedDate": 1788000000000, "processDate": 1788086400000,
        "bidAmount": 0,
        "items": [{"type": "ADD", "playerId": 1001}, {"type": "DROP", "playerId": 1002}],
    }
    rows = state.map_transaction(raw, season=2026)
    assert len(rows) == 2
    assert len({r["transaction_key"] for r in rows}) == 2  # keys must not collide
    assert [r["action"] for r in rows] == ["ADD", "DROP"]
    assert rows[0]["processed_at"].startswith("2026-")  # full ISO timestamp kept
    assert rows[0]["week"] == 5 and rows[0]["team_id"] == 4


def test_map_activity_topic_distinguishes_waiver_from_fcfs():
    topic = {"id": "T9", "date": 1788000000000, "messages": [
        {"messageTypeId": 178, "targetId": 1001, "to": 3},           # FA add (FCFS)
        {"messageTypeId": 180, "targetId": 1002, "to": 4, "from": 7},  # waiver add
        {"messageTypeId": 239, "targetId": 1003, "for": 5},           # drop
    ]}
    rows = state.map_activity_topic(topic, season=2026)
    assert [(r["action"], r["source"]) for r in rows] == [
        ("ADD", "FREEAGENT"), ("ADD", "WAIVER"), ("DROP", "TEAM"),
    ]
    assert rows[0]["team_id"] == 3
    assert rows[1]["bid_amount"] == 7    # waiver bid rides in msg['from']
    assert rows[2]["team_id"] == 5       # drop names the team in msg['for']


# ---------------------------------------------------------------------- ingest


def test_snapshot_writes_the_whole_universe_not_just_rosters(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1000": 4, "1001": 4})
    counts = _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    assert counts["players"] == len(pool)  # every player, rostered or not
    rows = state.get_player_state(crosswalked_db, as_of="2026-09-10", season=2026)
    assert len(rows) == len(pool)
    assert sum(1 for r in rows if r["on_team_id"] is not None) == 2


def test_roster_view_supplies_slot_and_acquisition(crosswalked_db, league_world):
    payload, pool = league_world(
        holdings={"1002": 7}, slots={"1002": 23}, acquisitions={"1002": "ADD"},
    )
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    row = state.get_player_state(
        crosswalked_db, as_of="2026-09-10", season=2026, espn_player_id="1002")[0]
    assert row["on_team_id"] == 7
    assert row["lineup_slot"] == "FLEX"
    assert row["acquisition_type"] == "ADD"
    assert row["acquisition_date"] == "2026-08-29"  # epoch ms -> ISO date


def test_gsis_crosswalk_is_applied(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1005": 2})
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    row = state.get_player_state(
        crosswalked_db, as_of="2026-09-10", season=2026, espn_player_id="1005")[0]
    assert row["gsis_id"] == "00-000005"


def test_collapsed_crosswalk_fails_loud(db, league_world):
    """No players table -> the join to the NFL spine is severed; refuse the write."""
    payload, pool = league_world()
    with pytest.raises(ValueError, match="crosswalk collapsed"):
        _ingest(db, payload, pool, day="2026-09-10")


def test_pool_and_roster_disagreement_is_counted_and_roster_wins(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1003": 6})
    for entry in pool:  # ESPN mid-flush: the pool claims a different holder
        if entry["id"] == 1003:
            entry["onTeamId"] = 9
    counts = _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    assert counts["conflicts"] == 1
    row = state.get_player_state(
        crosswalked_db, as_of="2026-09-10", season=2026, espn_player_id="1003")[0]
    assert row["on_team_id"] == 6  # the authoritative mRoster view wins


def test_pool_claims_rostered_but_no_roster_entry(crosswalked_db, league_world):
    payload, pool = league_world(holdings={})
    for entry in pool:
        if entry["id"] == 1004:
            entry["onTeamId"] = 8
    counts = _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    assert counts["conflicts"] == 1
    assert state.who_held(
        crosswalked_db, as_of="2026-09-10", season=2026, espn_player_id="1004") is None


def test_rostered_player_missing_from_pool_is_still_written(crosswalked_db, league_world):
    """Losing a rostered player from the snapshot would read as a phantom drop."""
    payload, pool = league_world(holdings={"1006": 5}, drop_from_pool=("1006",))
    counts = _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    assert counts["conflicts"] == 1
    assert state.who_held(
        crosswalked_db, as_of="2026-09-10", season=2026, espn_player_id="1006") == 5


def test_same_day_rerun_replaces_rather_than_duplicates(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1000": 4})
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    payload2, pool2 = league_world(holdings={"1000": 5})  # a trade later that same day
    _ingest(crosswalked_db, payload2, pool2, day="2026-09-10")

    total = crosswalked_db.execute(
        "SELECT COUNT(*) FROM league_player_state WHERE retrieved_as_of = '2026-09-10'"
    ).fetchone()[0]
    assert total == len(pool)
    assert state.who_held(
        crosswalked_db, as_of="2026-09-10", season=2026, espn_player_id="1000") == 5


# ------------------------------------------------- the drop / stale-holder case


def test_who_held_across_add_drop_readd(crosswalked_db, league_world):
    """The bug the whole-universe snapshot exists to prevent: after a drop, the
    stale 'team 4 holds him' row must NOT remain the newest row at later as_ofs."""
    timeline = [("2026-09-08", {"1000": 4}), ("2026-09-15", {}), ("2026-09-22", {"1000": 7})]
    for day, holdings in timeline:
        payload, pool = league_world(holdings=holdings)
        _ingest(crosswalked_db, payload, pool, day=day)

    held = lambda day: state.who_held(  # noqa: E731 - table-driven assertion
        crosswalked_db, as_of=day, season=2026, espn_player_id="1000")
    assert held("2026-09-08") == 4
    assert held("2026-09-14") == 4    # still team 4 the day before the drop
    assert held("2026-09-15") is None  # dropped — not a stale 4
    assert held("2026-09-21") is None
    assert held("2026-09-22") == 7
    assert held("2026-09-07") is None  # before any snapshot: unknown, not a guess


def test_free_agent_pool_tracks_the_same_events(crosswalked_db, league_world):
    for day, holdings in [("2026-09-08", {"1000": 4}), ("2026-09-15", {})]:
        payload, pool = league_world(holdings=holdings)
        _ingest(crosswalked_db, payload, pool, day=day)

    def fa_ids(day):
        return {r["espn_player_id"] for r in
                state.get_free_agents(crosswalked_db, as_of=day, season=2026)}

    assert "1000" not in fa_ids("2026-09-08")  # rostered
    assert "1000" in fa_ids("2026-09-15")      # dropped -> back on the shelf


def test_free_agents_sorted_by_ownership_and_filtered_by_position(crosswalked_db, league_world):
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    rows = state.get_free_agents(crosswalked_db, as_of="2026-09-10", season=2026)
    owned = [r["percent_owned"] for r in rows]
    assert owned == sorted(owned, reverse=True)
    qbs = state.get_free_agents(crosswalked_db, as_of="2026-09-10", season=2026, position="QB")
    assert qbs and all(r["position"] == "QB" for r in qbs)


def test_holder_timeline_collapses_segments(crosswalked_db, league_world):
    for day, holdings in [
        ("2026-09-08", {"1000": 4}), ("2026-09-09", {"1000": 4}),
        ("2026-09-15", {}), ("2026-09-22", {"1000": 7}),
    ]:
        payload, pool = league_world(holdings=holdings)
        _ingest(crosswalked_db, payload, pool, day=day)

    segments = state.holder_timeline(crosswalked_db, season=2026, espn_player_id="1000")
    assert [(s["team_id"], s["from"], s["to"]) for s in segments] == [
        (4, "2026-09-08", "2026-09-09"), (None, "2026-09-15", "2026-09-15"),
        (7, "2026-09-22", "2026-09-22"),
    ]
    assert segments[0]["snapshots"] == 2


# -------------------------------------------------------------- leakage tests


def test_player_state_leakage(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1000": 4})
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    assert state.get_player_state(crosswalked_db, as_of="2026-09-14", season=2026) == []
    assert state.get_player_state(crosswalked_db, as_of="2026-09-15", season=2026) != []


def test_player_state_historical_view_hides_late_retrieval(crosswalked_db, league_world):
    """A snapshot pulled later is invisible to a historical read at an earlier
    as_of, but visible under the explicit latest_truth view."""
    payload, pool = league_world(holdings={"1000": 4})
    _ingest(crosswalked_db, payload, pool, day="2026-09-20")
    assert state.who_held(
        crosswalked_db, as_of="2026-09-10", season=2026, espn_player_id="1000") is None
    read = base.latest_truth(state.get_player_state)
    assert read(crosswalked_db, as_of="2026-09-10", season=2026, espn_player_id="1000") == []
    # latest_truth still gates the FACT time, so the 09-20 snapshot stays hidden
    # at 09-10 — it was not knowable then either. It becomes visible at 09-20.
    assert read(crosswalked_db, as_of="2026-09-20", season=2026, espn_player_id="1000")


def test_team_state_leakage_and_read(crosswalked_db, league_world):
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    assert state.get_team_state(crosswalked_db, as_of="2026-09-14", season=2026) == []
    teams = state.get_team_state(crosswalked_db, as_of="2026-09-15", season=2026)
    assert len(teams) == 10
    one = state.get_team_state(crosswalked_db, as_of="2026-09-15", season=2026, team_id=3)
    assert len(one) == 1 and one[0]["waiver_rank"] == 3


def test_matchups_leakage_and_future_week_has_no_scores(crosswalked_db, league_world):
    payload, pool = league_world(scoring_period=3)
    _ingest(crosswalked_db, payload, pool, day="2026-09-22")
    assert state.get_matchups(crosswalked_db, as_of="2026-09-21", season=2026) == []
    week_10 = state.get_matchups(crosswalked_db, as_of="2026-09-22", season=2026, week=10)
    assert len(week_10) == 5
    assert all(r["home_points"] == 0.0 for r in week_10)   # unplayed: zeros, not leaked results
    week_1 = state.get_matchups(crosswalked_db, as_of="2026-09-22", season=2026, week=1)
    assert all(r["home_points"] == 110.0 for r in week_1)


def test_matchup_scores_do_not_leak_backwards(crosswalked_db, league_world):
    """Week 3 played later must not be visible in the week-1 information set."""
    early, pool_early = league_world(scoring_period=1)
    _ingest(crosswalked_db, early, pool_early, day="2026-09-08")
    later, pool_later = league_world(scoring_period=4)
    _ingest(crosswalked_db, later, pool_later, day="2026-09-29")

    seen_early = state.get_matchups(crosswalked_db, as_of="2026-09-08", season=2026, week=3)[0]
    assert seen_early["home_points"] == 0.0
    seen_later = state.get_matchups(crosswalked_db, as_of="2026-09-29", season=2026, week=3)[0]
    assert seen_later["home_points"] == 110.0


# --------------------------------------------------------------- transactions


def _txn(status="PENDING", processed=None):
    return {
        "id": "TX1", "teamId": 4, "type": "WAIVER", "status": status, "scoringPeriodId": 5,
        "proposedDate": 1788000000000, "processDate": processed, "bidAmount": 0,
        "items": [{"type": "ADD", "playerId": 1001}],
    }


def test_transactions_write_on_change(crosswalked_db):
    rows = state.map_transaction(_txn(), season=2026)
    assert state.ingest_transactions(crosswalked_db, rows, retrieved_as_of="2026-09-10", season=2026) == 1
    # same payload the next day -> no new version
    assert state.ingest_transactions(crosswalked_db, rows, retrieved_as_of="2026-09-11", season=2026) == 0
    # the overnight batch flips PENDING -> EXECUTED: that IS a change
    changed = state.map_transaction(_txn(status="EXECUTED", processed=1788086400000), season=2026)
    assert state.ingest_transactions(crosswalked_db, changed, retrieved_as_of="2026-09-11", season=2026) == 1

    versions = crosswalked_db.execute(
        "SELECT COUNT(*) FROM league_transactions WHERE transaction_key = ?",
        (rows[0]["transaction_key"],),
    ).fetchone()[0]
    assert versions == 2


def test_transactions_knowable_at_event_time_not_pull_time(crosswalked_db):
    """The one table whose knowledge time is the EVENT's, so a late pull of an
    old event does not pretend we knew it late."""
    rows = state.map_transaction(_txn(status="EXECUTED", processed=1788086400000), season=2026)
    state.ingest_transactions(crosswalked_db, rows, retrieved_as_of="2026-09-30", season=2026)
    stored = crosswalked_db.execute("SELECT * FROM league_transactions").fetchone()
    assert stored["knowable_as_of"] == "2026-08-30"      # the event day
    assert stored["retrieved_as_of"] == "2026-09-30"     # the pull day

    read = base.latest_truth(state.get_transactions)
    assert read(crosswalked_db, as_of="2026-08-29", season=2026) == []
    assert read(crosswalked_db, as_of="2026-08-30", season=2026)


def test_transactions_leakage_under_historical_view(crosswalked_db):
    rows = state.map_transaction(_txn(status="EXECUTED", processed=1788086400000), season=2026)
    state.ingest_transactions(crosswalked_db, rows, retrieved_as_of="2026-09-30", season=2026)
    assert state.get_transactions(crosswalked_db, as_of="2026-09-29", season=2026) == []
    assert state.get_transactions(crosswalked_db, as_of="2026-09-30", season=2026)


def test_transaction_rows_without_a_key_are_skipped(crosswalked_db):
    assert state.ingest_transactions(
        crosswalked_db, [{"transaction_key": None, "season": 2026}],
        retrieved_as_of="2026-09-10", season=2026,
    ) == 0


# ------------------------------------------------------------------- run log


def test_run_log_records_success_and_failure(db):
    run_id = state.start_run(db, season=2026, retrieved_as_of="2026-09-10", started_at="t0")
    state.finish_run(db, run_id, status="ok", finished_at="t1",
                     counts={"teams": 10, "players": 1026, "matchups": 70,
                             "transactions": 0, "conflicts": 0})
    assert state.last_run(db, season=2026)["players"] == 1026

    bad = state.start_run(db, season=2026, retrieved_as_of="2026-09-11", started_at="t2")
    state.finish_run(db, bad, status="failed", finished_at="t3", error="RuntimeError: cookies")
    assert state.last_run(db, season=2026, status="ok")["run_id"] == run_id  # last SUCCESS
    assert state.last_run(db, season=2026, status=None)["run_id"] == bad     # last ANY


def test_snapshot_gaps_reports_unrecoverable_days(crosswalked_db, league_world):
    for day in ("2026-09-08", "2026-09-09", "2026-09-12"):
        payload, pool = league_world()
        _ingest(crosswalked_db, payload, pool, day=day)
    gaps = state.snapshot_gaps(crosswalked_db, season=2026, through="2026-09-13")
    assert gaps == ["2026-09-10", "2026-09-11", "2026-09-13"]
    assert state.snapshot_days(crosswalked_db, season=2026)[0] == "2026-09-08"


def test_snapshot_gaps_empty_before_any_snapshot(db):
    assert state.snapshot_gaps(db, season=2026, through="2026-09-13") == []


# ------------------------------------------------------- as-of discipline (rule 1)


@pytest.mark.parametrize("accessor", [
    state.get_player_state, state.get_team_state, state.get_matchups, state.get_transactions,
])
def test_accessors_require_explicit_as_of(db, accessor):
    with pytest.raises(TypeError):
        accessor(db, season=2026)


def test_who_held_requires_as_of(db):
    with pytest.raises(TypeError):
        state.who_held(db, season=2026, espn_player_id="1000")


# ------------------------------------------------------------------ formatters


def test_formatters_render_evidence(crosswalked_db, league_world):
    payload, pool = league_world(holdings={"1000": 4, "1001": 4}, slots={"1000": 0})
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")

    roster = state.format_roster(
        state.get_player_state(crosswalked_db, as_of="2026-09-10", season=2026, on_team_id=4))
    assert "QB" in roster and "Synthetic Player 000" in roster
    assert roster.index("QB") < roster.index("BE")  # starters print before bench

    fa = state.format_free_agents(
        state.get_free_agents(crosswalked_db, as_of="2026-09-10", season=2026), limit=5)
    assert "%OWN" in fa and "more" in fa

    timeline = state.format_timeline(
        state.holder_timeline(crosswalked_db, season=2026, espn_player_id="1000"))
    assert "team 4" in timeline
    assert "FREE AGENT" in state.format_timeline([{"from": "d", "to": "d", "team_id": None,
                                                   "snapshots": 1}])
    assert "no observed snapshots" in state.format_timeline([])
    assert "no roster rows" in state.format_roster([])
    assert "no free agents" in state.format_free_agents([])
