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
import sqlite3
from pathlib import Path
from unittest.mock import patch

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


def test_collapsed_crosswalk_keeps_the_snapshot_and_reports(db, league_world, caplog):
    """A severed crosswalk must NOT cost the day.

    gsis_id is DERIVED (recomputable from players at any time); the ESPN snapshot
    is PERISHABLE. Discarding the snapshot to protect the derived column inverted
    this system's own priority, so the write proceeds with gsis_id NULL and the
    collapse is reported loudly instead.
    """
    payload, pool = league_world()
    with caplog.at_level("ERROR"):
        counts = _ingest(db, payload, pool, day="2026-09-10")
    assert counts["players"] == len(pool)          # the perishable part survived
    assert counts["gsis_coverage"] == 0.0
    assert "crosswalk collapsed" in caplog.text
    rows = state.get_player_state(db, as_of="2026-09-10", season=2026)
    assert rows and all(r["gsis_id"] is None for r in rows)


# ------------------------------------------- collapse guards (the audit findings)


def test_degraded_pool_cannot_destroy_a_stored_snapshot(crosswalked_db, league_world):
    """THE audit finding: a degraded second pull of the day used to DELETE the
    good snapshot, reverting a dropped player to his stale holder — silently, and
    with the run still logged 'ok'."""
    payload, pool = league_world(holdings={"1005": 4})
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    dropped, pool2 = league_world(holdings={})
    _ingest(crosswalked_db, dropped, pool2, day="2026-09-11")
    assert state.who_held(crosswalked_db, as_of="2026-09-11", season=2026,
                          espn_player_id="1005") is None

    # ESPN answers 200 with an empty players array on the next run of the day.
    with pytest.raises(state.SnapshotCollapse, match="degraded pool"):
        state.ingest_player_state(crosswalked_db, [], retrieved_as_of="2026-09-11",
                                  season=2026, roster={}, scoring_period=3)

    # The stored day is untouched: the drop still reads as a drop.
    assert state.who_held(crosswalked_db, as_of="2026-09-11", season=2026,
                          espn_player_id="1005") is None
    assert len(state.get_free_agents(crosswalked_db, as_of="2026-09-11", season=2026)) == len(pool2)


def test_collapse_guard_is_a_floor_not_an_equality(crosswalked_db, league_world):
    """Normal churn (ESPN pruning a few players) must still write."""
    payload, pool = league_world(pool_size=40)
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    smaller, pool_smaller = league_world(pool_size=36)   # -10%, well above the floor
    counts = _ingest(crosswalked_db, smaller, pool_smaller, day="2026-09-11")
    assert counts["players"] == 36


def test_collapsed_roster_view_cannot_mark_the_league_as_free_agency(crosswalked_db, league_world):
    """An empty mRoster used to rewrite every rostered player as a free agent."""
    holdings = {str(1000 + i): (i % 10) + 1 for i in range(20)}
    payload, pool = league_world(holdings=holdings)
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")

    stripped, pool2 = league_world(holdings=holdings)
    for team in stripped["teams"]:
        team["roster"] = {"entries": []}              # ESPN drops/flushes mRoster
    with pytest.raises(state.SnapshotCollapse, match="mass free agency"):
        _ingest(crosswalked_db, stripped, pool2, day="2026-09-11")

    assert state.who_held(crosswalked_db, as_of="2026-09-10", season=2026,
                          espn_player_id="1000") == 1


def test_allow_shrink_overrides_the_guard(crosswalked_db, league_world):
    payload, pool = league_world(pool_size=40)
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    tiny, pool_tiny = league_world(pool_size=5)
    state.ingest_league_state(crosswalked_db, tiny, retrieved_as_of="2026-09-11", season=2026)
    counts = state.ingest_player_state(
        crosswalked_db, pool_tiny, retrieved_as_of="2026-09-11", season=2026,
        roster=state.roster_index(tiny), scoring_period=3, allow_shrink=True,
    )
    assert counts["players"] == 5


def test_empty_team_list_never_deletes_the_days_standings(crosswalked_db, league_world):
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    empty = {"scoringPeriodId": 3, "teams": [], "schedule": []}
    with pytest.raises(state.SnapshotCollapse, match="ZERO teams"):
        state.ingest_league_state(crosswalked_db, empty, retrieved_as_of="2026-09-10", season=2026)
    assert len(state.get_team_state(crosswalked_db, as_of="2026-09-10", season=2026)) == 10


def test_ingest_is_atomic_when_the_write_fails(crosswalked_db, league_world):
    """A crash between the DELETE and the insert must not leave the day empty —
    that day is unrecoverable, ESPN serves no history."""
    payload, pool = league_world(holdings={"1000": 4})
    _ingest(crosswalked_db, payload, pool, day="2026-09-10")

    with patch.object(base, "upsert", side_effect=sqlite3.InterfaceError("boom")):
        with pytest.raises(sqlite3.InterfaceError):
            state.ingest_player_state(
                crosswalked_db, pool, retrieved_as_of="2026-09-10", season=2026,
                roster=state.roster_index(payload), scoring_period=3,
            )
    assert crosswalked_db.execute(
        "SELECT COUNT(*) FROM league_player_state WHERE retrieved_as_of = '2026-09-10'"
    ).fetchone()[0] == len(pool)
    assert state.who_held(crosswalked_db, as_of="2026-09-10", season=2026,
                          espn_player_id="1000") == 4


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


# ------------------------------------------ reconciliation + stamp discipline


def test_conflict_counted_when_pool_says_free_agent_but_roster_says_held(
    crosswalked_db, league_world
):
    """The direction that matters most for drop detection — a half-flushed DROP —
    used to be resolved silently with conflicts=0."""
    payload, pool = league_world(holdings={"1003": 6})
    for entry in pool:
        if entry["id"] == 1003:
            entry["onTeamId"] = 0          # pool has already flushed the drop
            entry["status"] = "FREEAGENT"
    counts = _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    assert counts["conflicts"] == 1
    assert state.who_held(crosswalked_db, as_of="2026-09-10", season=2026,
                          espn_player_id="1003") == 6   # mRoster still wins


def test_absent_onteamid_is_not_counted_as_a_disagreement(crosswalked_db, league_world):
    """An entry with no onTeamId asserts nothing; it must not spam the counter."""
    payload, pool = league_world(holdings={"1003": 6})
    for entry in pool:
        entry.pop("onTeamId", None)
    counts = _ingest(crosswalked_db, payload, pool, day="2026-09-10")
    assert counts["conflicts"] == 0


def test_write_stamps_are_validated_like_reads(crosswalked_db, league_world):
    """A malformed stamp used to write a whole day that no accessor could see:
    the as-of gate compares dates lexically, so '2026-9-8' <= '2026-09-15' is False."""
    payload, pool = league_world()
    for bad in ("2026-9-8", "nonsense!!", None):
        with pytest.raises((ValueError, TypeError)):
            state.ingest_player_state(crosswalked_db, pool, retrieved_as_of=bad,
                                      season=2026, roster={}, scoring_period=3)
        with pytest.raises((ValueError, TypeError)):
            state.ingest_league_state(crosswalked_db, payload, retrieved_as_of=bad, season=2026)
    assert crosswalked_db.execute("SELECT COUNT(*) FROM league_player_state").fetchone()[0] == 0


def test_event_timestamps_use_the_local_day_not_utc():
    """Every other date here is a local calendar day; deriving the event day in
    UTC stamped evening events a day late (knowable_as_of > retrieved_as_of)."""
    from datetime import datetime

    local_evening = datetime(2026, 9, 15, 20, 0, 0).astimezone()
    ms = int(local_evening.timestamp() * 1000)
    assert state._epoch_ms_to_iso(ms, date_only=True) == "2026-09-15"
    assert state._epoch_ms_to_iso(ms).startswith("2026-09-15T20:00:00")


def test_transaction_knowable_day_matches_the_local_event_day(crosswalked_db):
    from datetime import datetime

    local_evening = datetime(2026, 9, 15, 20, 0, 0).astimezone()
    ms = int(local_evening.timestamp() * 1000)
    rows = state.map_transaction(
        {"id": "TX9", "teamId": 4, "type": "FREEAGENT", "status": "EXECUTED",
         "scoringPeriodId": 2, "proposedDate": ms, "processDate": ms,
         "items": [{"type": "ADD", "playerId": 1001}]},
        season=2026,
    )
    state.ingest_transactions(crosswalked_db, rows, retrieved_as_of="2026-09-15", season=2026)
    stored = crosswalked_db.execute("SELECT * FROM league_transactions").fetchone()
    assert stored["knowable_as_of"] == "2026-09-15"
    assert stored["knowable_as_of"] <= stored["retrieved_as_of"]
    assert state.get_transactions(crosswalked_db, as_of="2026-09-15", season=2026)


def test_activity_trade_names_the_acquiring_team():
    topic = {"id": "T1", "date": 1788000000000, "messages": [
        {"messageTypeId": 244, "targetId": 1001, "from": 3, "to": 7},
    ]}
    row = state.map_activity_topic(topic, season=2026)[0]
    assert (row["action"], row["team_id"]) == ("TRADE", 7)


# ---------------------------------------------- resolve_own_team (item 3.2)


def test_resolve_own_team_requires_as_of_and_gates_leakage(crosswalked_db, league_world):
    """Rule 1: every accessor ships a leakage test. This one decides WHOSE roster
    the marginal board values, so a snapshot it should not be able to see must not
    silently resolve a team."""
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")

    with pytest.raises(TypeError):
        state.resolve_own_team(crosswalked_db, season=2026, swid="{OWNER-4}")

    assert state.resolve_own_team(
        crosswalked_db, as_of="2026-09-15", season=2026, swid="{OWNER-4}") == 4
    with pytest.raises(state.OwnTeamUnresolved):
        state.resolve_own_team(
            crosswalked_db, as_of="2026-09-14", season=2026, swid="{OWNER-4}")

    # retrieval time is gated too: a row pulled AFTER the as-of date is invisible
    _ingest(crosswalked_db, payload, pool, day="2026-09-20")
    crosswalked_db.execute("DELETE FROM league_teams WHERE retrieved_as_of = '2026-09-15'")
    crosswalked_db.commit()
    with pytest.raises(state.OwnTeamUnresolved):
        state.resolve_own_team(
            crosswalked_db, as_of="2026-09-15", season=2026, swid="{OWNER-4}")


def test_resolve_own_team_refuses_rather_than_guessing(crosswalked_db, league_world):
    """Silently valuing SOMEONE ELSE'S roster is a wrong answer the operator
    cannot smell (Rule 6), so an unmatched or ambiguous SWID raises."""
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    with pytest.raises(state.OwnTeamUnresolved) as exc:
        state.resolve_own_team(
            crosswalked_db, as_of="2026-09-15", season=2026, swid="{NOBODY}")
    assert "--team" in str(exc.value)

    crosswalked_db.execute(
        "UPDATE league_teams SET primary_owner = '{OWNER-4}' WHERE team_id IN (4, 5)")
    crosswalked_db.commit()
    with pytest.raises(state.OwnTeamUnresolved) as exc:
        state.resolve_own_team(
            crosswalked_db, as_of="2026-09-15", season=2026, swid="{OWNER-4}")
    assert "2 teams" in str(exc.value)


# ------------------------------------------------ injury transitions (item 3.3)
#
# The LIVE in-season injury-shock source: diff consecutive daily snapshots for a
# player crossing the availability boundary (ACTIVE/QUESTIONABLE/None <->
# OUT/INJURY_RESERVE). Only three PRE-SEASON snapshots exist against real data
# (all free agents, no transitions), so this is unit-tested on synthetic
# snapshots and smoke-tested live until the season starts.


def _pool_entry(pid, *, pos="RB", injury="ACTIVE", team_id=None):
    return {
        "id": int(pid),
        "onTeamId": team_id or 0,
        "status": "ONTEAM" if team_id else "FREEAGENT",
        "player": {
            "id": int(pid), "fullName": f"Player {pid}",
            "defaultPositionId": {"QB": 1, "RB": 2, "WR": 3, "TE": 4}[pos],
            "proTeamId": 1, "injuryStatus": injury,
            "ownership": {"percentOwned": 50.0, "percentStarted": 40.0, "percentChange": 0.0},
        },
    }


def _snap(db, entries, *, day, season=2026):
    state.ingest_player_state(db, entries, retrieved_as_of=day, season=season,
                              allow_shrink=True)


def test_injury_transitions_detects_a_ruled_out_crossing(db):
    _snap(db, [_pool_entry(7001, injury="ACTIVE"), _pool_entry(7002, injury="ACTIVE")],
          day="2026-09-10")
    _snap(db, [_pool_entry(7001, injury="OUT"), _pool_entry(7002, injury="ACTIVE")],
          day="2026-09-11")
    ts = state.injury_transitions(db, as_of="2026-09-11", season=2026)
    assert len(ts) == 1
    t = ts[0]
    assert t["espn_player_id"] == "7001"
    assert t["from_status"] == "ACTIVE" and t["to_status"] == "OUT"
    assert t["direction"] == "ruled_out"
    assert t["became_knowable"] == "2026-09-11"
    assert t["player"] == "Player 7001" and t["position"] == "RB"


def test_injury_transitions_detects_a_clearing_crossing(db):
    _snap(db, [_pool_entry(7001, injury="INJURY_RESERVE")], day="2026-09-10")
    _snap(db, [_pool_entry(7001, injury="ACTIVE")], day="2026-09-11")
    ts = state.injury_transitions(db, as_of="2026-09-11", season=2026)
    assert [t["direction"] for t in ts] == ["cleared"]
    assert ts[0]["from_status"] == "INJURY_RESERVE" and ts[0]["to_status"] == "ACTIVE"


def test_injury_transitions_ignores_within_class_moves(db):
    # ACTIVE -> QUESTIONABLE and OUT -> INJURY_RESERVE do NOT cross the boundary.
    _snap(db, [_pool_entry(7001, injury="ACTIVE"), _pool_entry(7002, injury="OUT")],
          day="2026-09-10")
    _snap(db, [_pool_entry(7001, injury="QUESTIONABLE"),
               _pool_entry(7002, injury="INJURY_RESERVE")], day="2026-09-11")
    assert state.injury_transitions(db, as_of="2026-09-11", season=2026) == []


def test_injury_transitions_are_leakage_safe(db):
    # A transition on 2026-09-11 must be invisible at an as_of before that day.
    _snap(db, [_pool_entry(7001, injury="ACTIVE")], day="2026-09-10")
    _snap(db, [_pool_entry(7001, injury="OUT")], day="2026-09-11")
    assert state.injury_transitions(db, as_of="2026-09-10", season=2026) == []
    assert len(state.injury_transitions(db, as_of="2026-09-11", season=2026)) == 1


def test_injury_transitions_only_two_snapshots_needed_and_none_on_a_single(db):
    # A single snapshot cannot form a transition (no prior to diff against).
    _snap(db, [_pool_entry(7001, injury="OUT")], day="2026-09-10")
    assert state.injury_transitions(db, as_of="2026-09-10", season=2026) == []


# =========================================================== item 3.8a — ground truth
#
# Every fixture below is SYNTHETIC (Rule 5). The SHAPES are the live ones the
# 3.8a recon measured on 2026-09-02: ESPN's `injured`/`droppable` booleans on the
# pool entry, the roster ENTRY's own `injuryStatus` (a different field), a
# positionLimits map keyed by defaultPositionId STRINGS with unmapped ids and a
# real 0, and a lineupSlotCounts map carrying a zero-count slot LINEUP_SLOTS does
# not map.


def test_player_columns_match_the_table(db):
    """``_PLAYER_COLUMNS`` must equal the table's own column set.

    CATCHES: a new league_player_state column added to the schema and to
    ``map_player_entry`` but forgotten in ``_PLAYER_COLUMNS``. ``base.upsert``
    takes its column list from ``rows[0]`` ALONE while ``ingest_player_state``
    writes a heterogeneous batch (mapped pool rows + roster-only rows built from a
    different literal), and the ``setdefault`` loop over this tuple is the only
    thing that makes them uniform — so the forgotten entry kills the WHOLE day's
    snapshot, not one column. No test covered this before item 3.8a.
    """
    declared = {r[1] for r in db.execute("PRAGMA table_info(league_player_state)")}
    assert set(state._PLAYER_COLUMNS) | {"retrieved_as_of", "knowable_as_of"} == declared


def test_espn_flags_land_on_the_snapshot_and_absent_means_null(crosswalked_db, league_world):
    """ESPN's own flags are stored, and an ABSENT key stays NULL — never 0.

    CATCHES: an ``int(bool(...))`` on a missing key, which would turn "we do not
    know whether ESPN will let you drop him" into "drop away".
    """
    payload, pool = league_world(
        holdings={"1000": 4, "1001": 4},
        injured={"1000": True}, droppable={"1000": False},
        entry_injury={"1000": "NORMAL"},
    )
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    rows = {r["espn_player_id"]: r for r in state.get_player_state(
        crosswalked_db, as_of="2026-09-15", season=2026)}
    assert rows["1000"]["injured"] == 1
    assert rows["1000"]["droppable"] == 0
    assert rows["1000"]["entry_injury_status"] == "NORMAL"
    # 1001 is rostered but ESPN served neither flag for him
    assert rows["1001"]["injured"] is None
    assert rows["1001"]["droppable"] is None
    # a free agent ESPN served no flags for is NULL too, not False
    assert rows["1005"]["injured"] is None


def test_map_settings_decodes_the_traps_and_strips_division_names(league_world):
    """The three decoding traps the recon measured, plus the Rule-5 strip.

    CATCHES: a naive ``DEFPOS[int(k)]`` (KeyError on the 12 unmapped ids), an
    "unmapped means unlimited" shortcut (id 0 carries a real 0), a
    ``decode_slot(22)`` warning on a zero-count slot, and a commissioner-authored
    division name reaching storage.
    """
    payload, _pool = league_world()
    row = state.map_settings(payload, season=2026, scoring_period=3)
    assert row is not None
    limits = json.loads(row["position_limits"])
    # only the six DEFPOS ids are decoded, by LABEL, with -1/0 preserved verbatim
    assert limits == {"QB": 4, "RB": 8, "WR": 8, "TE": 3, "K": 3, "D/ST": 3}
    slots = json.loads(row["lineup_slot_counts"])
    assert slots["IR"] == 1 and slots["BE"] == 7
    assert "22" not in slots and not any(k.isdigit() for k in slots)
    # the raw map survives for a later reader
    raw = json.loads(row["settings_json"])
    assert raw["rosterSettings"]["positionLimits"]["0"] == 0
    # Rule 5: division names are stripped, ids/sizes kept
    div = raw["scheduleSettings"]["divisions"][0]
    assert "name" not in div and div["size"] == 10
    assert "Synthetic Division" not in row["settings_json"]
    # trade deadline: ESPN's epoch preserved AND localised
    assert row["trade_deadline_epoch_ms"] == 1796230800000
    assert row["trade_deadline"].startswith("2026-12-02")
    assert row["waiver_last_execution"].startswith("2026-09-02")
    # `isUsingUndroppableList` is NESTED under rosterSettings, not at the top
    # level. CATCHES the live defect the 3.8a smoke run found: a top-level read
    # stored NULL and the page printed "off" for a list that is ON and refuses two
    # of the operator's own drops.
    assert row["is_using_undroppable_list"] == 1


def test_a_settings_row_that_does_not_validate_is_refused_not_half_written(
        crosswalked_db, league_world):
    """A DEGRADED settings row is worse than no row: it looks healthy and silently
    changes decisions through the league cap fence and the FAAB verdict.

    CATCHES: storing a partially-decoded rulebook. Teams and matchups must still
    land — the snapshot is perishable — and the run must degrade, never pass.
    """
    # position limits covering only three of the six positions
    payload, pool = league_world(position_limits={"1": 4, "2": 8, "3": 8})
    counts = state.ingest_league_state(
        crosswalked_db, payload, retrieved_as_of="2026-09-15", season=2026)
    assert counts["settings"] == 0
    assert "refused" in counts["settings_warning"]
    assert counts["teams"] == 10 and counts["matchups"] > 0
    assert state.get_league_settings(crosswalked_db, as_of="2026-09-15", season=2026) is None

    # and a payload with NO settings block at all: same shape, different sentence
    payload2, _ = league_world(settings=False)
    counts2 = state.ingest_league_state(
        crosswalked_db, payload2, retrieved_as_of="2026-09-16", season=2026)
    assert counts2["settings"] == 0
    assert "no `settings` block" in counts2["settings_warning"]
    assert counts2["teams"] == 10


def test_league_settings_leakage(crosswalked_db, league_world):
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    assert state.get_league_settings(
        crosswalked_db, as_of="2026-09-14", season=2026) is None
    assert state.get_league_settings(
        crosswalked_db, as_of="2026-09-15", season=2026) is not None


def test_league_settings_historical_view_hides_late_retrieval(crosswalked_db, league_world):
    """A settings snapshot pulled later is invisible to a historical read at an
    earlier as_of; latest_truth still gates the fact time and sees it at its day."""
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-20")
    assert state.get_league_settings(
        crosswalked_db, as_of="2026-09-10", season=2026) is None
    read = base.latest_truth(state.get_league_settings)
    assert read(crosswalked_db, as_of="2026-09-10", season=2026) is None
    assert read(crosswalked_db, as_of="2026-09-20", season=2026) is not None


def test_league_position_limits_leakage_and_refusal_to_be_partial(crosswalked_db, league_world):
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    assert state.league_position_limits(
        crosswalked_db, as_of="2026-09-14", season=2026) is None
    limits = state.league_position_limits(crosswalked_db, as_of="2026-09-15", season=2026)
    assert limits == {"QB": 4, "RB": 8, "WR": 8, "TE": 3, "K": 3, "D/ST": 3}
    read = base.latest_truth(state.league_position_limits)
    assert read(crosswalked_db, as_of="2026-09-15", season=2026) == limits


def test_injured_flag_crosstab_leakage_and_populations(crosswalked_db, league_world):
    """The cross-tab is leakage-gated AND states its population.

    CATCHES: a universe-vs-rostered mix-up. The two are materially different
    samples live (n≈1,036 with 63 injured vs n=160 with one), so a cross-tab that
    could not tell them apart would read as strong evidence off a near-vacuous one.
    """
    payload, pool = league_world(holdings={"1000": 4, "1001": 4}, injured={"1000": True})
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    assert state.injured_flag_crosstab(
        crosswalked_db, as_of="2026-09-14", season=2026) == []
    universe = state.injured_flag_crosstab(crosswalked_db, as_of="2026-09-15", season=2026)
    rostered = state.injured_flag_crosstab(
        crosswalked_db, as_of="2026-09-15", season=2026, rostered_only=True)
    assert sum(r["n"] for r in universe) == 40
    assert sum(r["n"] for r in rostered) == 2
    read = base.latest_truth(state.injured_flag_crosstab)
    assert read(crosswalked_db, as_of="2026-09-15", season=2026)


def test_ir_rule_check_is_silent_when_clean_and_speaks_on_a_new_designation(
        crosswalked_db, league_world):
    """The WATCH is the surface this report exists for.

    CATCHES: a report whose only signal is the cross-tab. ``injured`` and
    ``injury_status`` are two encodings of ONE fact and have never disagreed, so
    "divergences: none" every day trains the operator to skip it. The event that
    matters is a designation this league has NEVER served — DOUBTFUL, PUP, NFI —
    because that is the case the shipped rule has never been tested on.
    """
    payload, pool = league_world()
    # every pool player carries an OBSERVED designation and a captured flag
    for entry in pool:
        tag = entry["player"]["injuryStatus"]
        entry["player"]["injured"] = tag in state.IR_ELIGIBLE_STATUSES
        entry["player"]["droppable"] = True
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    report = state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026)
    assert report.divergences == () and report.new_statuses == ()
    assert report.ir_occupants == ()
    assert report.has_news is False
    assert "clean" in report.headline

    # now a DOUBTFUL row appears — never observed in this league before
    payload2, pool2 = league_world()
    for entry in pool2:
        entry["player"]["injured"] = entry["player"]["injuryStatus"] in state.IR_ELIGIBLE_STATUSES
        entry["player"]["droppable"] = True
    pool2[0]["player"]["injuryStatus"] = "DOUBTFUL"
    pool2[0]["player"]["injured"] = False
    _ingest(crosswalked_db, payload2, pool2, day="2026-09-16")
    report2 = state.ir_rule_check(crosswalked_db, as_of="2026-09-16", season=2026)
    assert report2.has_news is True
    assert [d["injury_status"] for d in report2.new_statuses] == ["DOUBTFUL"]
    assert "DOUBTFUL" in report2.headline
    assert "IR-INELIGIBLE" in report2.headline
    assert report2.divergences == ()          # the flag still agrees with the rule


def test_ir_rule_check_reports_a_divergence_and_an_occupant(crosswalked_db, league_world):
    """An IR occupant whose ESPN flag is FALSE is the event that settles the
    IR-slot mechanism, and a flag that disagrees with the rule is the event the
    cross-tab exists for. Both must be reported, never silence."""
    payload, pool = league_world(holdings={"1000": 4}, slots={"1000": 21})  # 21 = IR
    for entry in pool:
        entry["player"]["injured"] = False
        entry["player"]["droppable"] = True
    pool[0]["player"]["injuryStatus"] = "OUT"     # rule says eligible, flag says no
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    report = state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026)
    assert report.has_news is True
    assert [d["injury_status"] for d in report.divergences] == ["OUT"]
    assert len(report.ir_occupants) == 1
    occupant = report.ir_occupants[0]
    assert occupant["lineup_slot"] == "IR" and occupant["injured"] == 0
    assert occupant["entry_injury_status"] == "NORMAL"
    assert occupant["team_transaction_locked"] == 0
    assert "settles the IR-slot mechanism" in report.headline
    text = state.format_ir_rule_report(report)
    assert "DIVERGENCES" in text and "IR-slot occupants league-wide (1)" in text


def test_ir_rule_check_coverage_is_a_fraction_not_a_migration_boolean(
        crosswalked_db, league_world):
    """CATCHES: reporting `captured` as a boolean about migration 014. A rostered
    player absent from ESPN's pool response is synthesized from the roster view
    alone and carries NULL flags on a POST-014 snapshot too."""
    payload, pool = league_world(
        holdings={"1000": 4, "1001": 4}, drop_from_pool=("1001",))
    for entry in pool:
        entry["player"]["injured"] = False
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    report = state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026)
    assert report.rostered_n == 2
    assert report.coverage == 0.5
    assert report.has_news is True
    assert "missing on 50% of rostered players" in report.headline


def test_settings_verdicts_close_the_faab_question_and_join_the_deadline(
        crosswalked_db, league_world):
    """Rule 3: the FAAB verdict and the deadline->NFL-week mapping are LOGIC and
    live in the package. The week is a JOIN, not arithmetic — 2026's week 1 opens
    on a Wednesday, so counting sevens from a Thursday is off by a day."""
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    crosswalked_db.executemany(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
        "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("g12", 2026, 12, "REG", "2026-11-26", "2026-07-01", "2026-07-01"),
         ("g13", 2026, 13, "REG", "2026-12-03", "2026-07-01", "2026-07-01"),
         ("g14", 2026, 14, "REG", "2026-12-10", "2026-07-01", "2026-07-01")],
    )
    crosswalked_db.commit()
    settings = state.get_league_settings(crosswalked_db, as_of="2026-09-15", season=2026)
    lines = state.settings_verdicts(settings, conn=crosswalked_db, as_of="2026-09-15")
    blob = " ".join(lines)
    assert "INERT" in blob and "isUsingAcquisitionBudget=false" in blob
    assert "no season or weekly acquisition cap" in blob
    assert "week 13" in blob
    text = state.format_settings(settings, verdicts=lines)
    assert "INERT" in text and "IR 1" in text and "QB 4" in text
    # the raw blobs never reach the page (Rule 5 / commissioner free text)
    assert "settings_json" not in text and "Synthetic Division" not in text


def test_a_not_captured_undroppable_setting_does_not_print_as_off(league_world):
    """NULL is NOT CAPTURED, and "off" is a claim (item 3.8a).

    CATCHES the same false-negative the `injured`/`droppable` NULL convention
    exists to stop, one level up: a settings snapshot that never carried the key
    must not tell the operator the undroppable list is off — it is ON in this
    league and refuses two of his own drops.
    """
    payload, _pool = league_world()
    del payload["settings"]["rosterSettings"]["isUsingUndroppableList"]
    row = state.map_settings(payload, season=2026, scoring_period=3)
    assert row["is_using_undroppable_list"] is None
    # format_settings takes the ACCESSOR's shape (JSON columns already decoded).
    decoded = {**row, "retrieved_as_of": "2026-09-15",
               **{c: json.loads(row[c]) for c in state._SETTINGS_JSON_COLUMNS}}
    text = state.format_settings(decoded)
    assert "NOT CAPTURED in this snapshot" in text
    assert "undroppable list      : off" not in text


# ============================================== item 3.8a audit fixes (state side)


def test_the_new_designation_watch_speaks_ONCE_and_then_goes_quiet(
        crosswalked_db, league_world):
    """"Never seen before" is a fact about THIS LEAGUE'S HISTORY, not a constant.

    Comparing against a frozen module set makes the watch LATCH: the first
    DOUBTFUL puts the identical headline on `league status` and on every waiver
    plan for the rest of the season — the crying-wolf failure the watch was
    written to avoid, with the genuine events (a divergence, a flag-0 occupant)
    then arriving inside a sentence the operator has been trained to skip.

    CATCHES: reverting to ``tok not in OBSERVED_INJURY_STATUSES``.
    """
    def _doubtful_day(day):
        payload, pool = league_world()
        for entry in pool:
            entry["player"]["injured"] = \
                entry["player"]["injuryStatus"] in state.IR_ELIGIBLE_STATUSES
            entry["player"]["droppable"] = True
        pool[0]["player"]["injuryStatus"] = "DOUBTFUL"
        pool[0]["player"]["injured"] = False
        _ingest(crosswalked_db, payload, pool, day=day)
        return state.ir_rule_check(crosswalked_db, as_of=day, season=2026)

    first = _doubtful_day("2026-09-15")
    assert first.has_news is True and "DOUBTFUL" in first.headline

    second = _doubtful_day("2026-09-16")
    assert second.new_statuses == (), "DOUBTFUL was seen yesterday — it is not new"
    assert second.has_news is False
    assert "DOUBTFUL" not in second.headline

    # a DIFFERENT never-served designation still fires while DOUBTFUL stays quiet
    payload, pool = league_world()
    for entry in pool:
        entry["player"]["injured"] = \
            entry["player"]["injuryStatus"] in state.IR_ELIGIBLE_STATUSES
        entry["player"]["droppable"] = True
    pool[0]["player"]["injuryStatus"] = "DOUBTFUL"
    pool[0]["player"]["injured"] = False
    pool[1]["player"]["injuryStatus"] = "PUP"
    pool[1]["player"]["injured"] = False
    _ingest(crosswalked_db, payload, pool, day="2026-09-17")
    third = state.ir_rule_check(crosswalked_db, as_of="2026-09-17", season=2026)
    assert [d["injury_status"] for d in third.new_statuses] == ["PUP"]


def test_one_diverging_designation_split_across_entry_statuses_counts_ONCE(
        crosswalked_db, league_world):
    """The cross-tab groups by (tag, flag, roster-entry status), so ONE diverging
    designation occupies several rows — and the divergence list appended one entry
    PER ROW while its sibling watch correctly summed by token.

    Live shape: OUT already splits into (entry NULL) and (entry NORMAL), so the
    FIRST real divergence would have printed "2 designation(s) ... (OUT, OUT)"
    with each n undercounted.

    CATCHES: ``divergences.append`` per crosstab row.
    """
    payload, pool = league_world(holdings={"1000": 4})   # 1000 is rostered, the rest not
    for entry in pool:
        entry["player"]["injured"] = False
        entry["player"]["droppable"] = True
    pool[0]["player"]["injuryStatus"] = "OUT"     # rostered  -> entry status NORMAL
    pool[6]["player"]["injuryStatus"] = "OUT"     # unrostered -> entry status NULL
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    universe = state.injured_flag_crosstab(crosswalked_db, as_of="2026-09-15", season=2026)
    out_rows = [r for r in universe if r["injury_status"] == "OUT"]
    assert len(out_rows) == 2, "the fixture must actually split the designation"
    report = state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026)
    assert [d["injury_status"] for d in report.divergences] == ["OUT"]
    assert report.divergences[0]["n"] == 2
    assert report.headline.count("OUT") == 1


def test_the_crosstab_keeps_the_roster_entry_dimension_and_renders_it(
        crosswalked_db, league_world):
    """``entry_injury_status`` is the single most plausible ALTERNATIVE IR gate,
    and the whole reason the cross-tab has a third dimension: the first occupant
    is meant to settle BOTH candidate gates at once.

    CATCHES: collapsing the SELECT/GROUP BY back to two dimensions — which the
    suite could not see, because the population sums are invariant under it and
    the only ``entry_injury_status`` assertion reads the OCCUPANT record, built
    from ``get_player_state`` rather than from the cross-tab.
    """
    payload, pool = league_world(
        holdings={"1001": 4, "1002": 4},
        entry_injury={"1002": "PLAYER_ILLNESS"},
    )
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    rostered = state.injured_flag_crosstab(
        crosswalked_db, as_of="2026-09-15", season=2026, rostered_only=True)
    assert sorted(r["entry_injury_status"] for r in rostered) == \
        ["NORMAL", "PLAYER_ILLNESS"]
    assert len(rostered) == 2, "the third dimension is what splits these two rows"
    text = state.format_ir_rule_report(
        state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026))
    assert "PLAYER_ILLNESS" in text          # the ROSTER ENTRY column really renders


def test_a_snapshot_with_no_captured_flag_reports_NO_COMPARISON_not_agreement(
        crosswalked_db, league_world):
    """An absence of difference is not an absence of signal.

    On a pre-014 snapshot no row carries ``injured``, so ZERO comparisons are
    possible — and the page said "divergences: none — ESPN's `injured` flag marks
    exactly INJURY_RESERVE/OUT on this snapshot", a positive evidential claim
    about a column that is entirely NULL, on the one command an operator reads to
    decide whether the shipped IR rule is still right.

    CATCHES: an unconditional else-branch.
    """
    payload, pool = league_world(holdings={"1000": 4})   # no injured/droppable flags
    _ingest(crosswalked_db, payload, pool, day="2026-08-15")
    report = state.ir_rule_check(crosswalked_db, as_of="2026-08-15", season=2026)
    assert report.compared_n == 0
    text = state.format_ir_rule_report(report)
    assert "NOT MEASURED" in text and "No comparison was possible" in text
    assert "marks exactly" not in text
    # ...and the coverage line is printed rather than suppressed, so a silent
    # section can never double as a clean bill of health.
    assert "ESPN flag coverage on rostered players" in text


def test_a_pre_baseline_snapshot_is_not_news_and_names_no_false_remedy(
        crosswalked_db, league_world):
    """A snapshot that predates flag capture can NEVER be re-flagged — ESPN serves
    no historical league state — so it is not an interrupt and it must not tell
    the operator to run something that cannot help.

    CATCHES: ``coverage < 1.0`` alone setting ``has_news``, which put a red `!`
    line on every past-``as_of`` waiver run naming no action he could take.
    """
    payload, pool = league_world(holdings={"1000": 4})
    _ingest(crosswalked_db, payload, pool, day="2026-08-15")
    report = state.ir_rule_check(crosswalked_db, as_of="2026-08-15", season=2026)
    assert report.pre_baseline is True
    assert report.coverage == 0.0
    assert report.has_news is False, "a permanent, unrepairable gap names no action"
    assert "run a sync" not in state.format_ir_rule_report(report)


def test_a_flag_hole_on_another_managers_bench_does_not_interrupt_the_operator(
        crosswalked_db, league_world):
    """Coverage is computed league-wide, so ONE rostered player missing from
    ESPN's pool response — on any of the ten rosters — used to raise the IR banner
    on the operator's own waiver plan, about a row nothing on that page reads and
    a flag he cannot make ESPN serve for somebody else's bench player.

    CATCHES: an un-scoped coverage clause. ``league status`` names no team and
    keeps the league-wide signal (a degraded pull IS actionable there).
    """
    payload, pool = league_world(
        holdings={"1000": 10, "1001": 4}, drop_from_pool=("1001",))
    for entry in pool:
        entry["player"]["injured"] = \
            entry["player"]["injuryStatus"] in state.IR_ELIGIBLE_STATUSES
        entry["player"]["droppable"] = True
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")

    own = state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026,
                              own_team_id=10)
    assert own.coverage == 0.5 and own.own_coverage == 1.0
    assert own.has_news is False

    league_wide = state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026)
    assert league_wide.has_news is True
    assert "rostered players league-wide" in league_wide.headline

    # ...but a hole on the operator's OWN roster still speaks to him.
    payload2, pool2 = league_world(
        holdings={"1000": 10, "1001": 4}, drop_from_pool=("1000",))
    for entry in pool2:
        entry["player"]["injured"] = \
            entry["player"]["injuryStatus"] in state.IR_ELIGIBLE_STATUSES
        entry["player"]["droppable"] = True
    _ingest(crosswalked_db, payload2, pool2, day="2026-09-16")
    mine = state.ir_rule_check(crosswalked_db, as_of="2026-09-16", season=2026,
                               own_team_id=10)
    assert mine.has_news is True and "YOUR rostered players" in mine.headline


def test_the_coverage_disclosure_names_the_DROP_consequence_and_the_pull_remedy(
        crosswalked_db, league_world):
    """``injured`` and ``droppable`` come from the SAME pool entry, so they are
    NULL together — but only the IR consequence was ever disclosed. The other one
    is the fence that stops the tool proposing a drop ESPN refuses, and its
    absence is the whole point of item 3.8a.

    Also CATCHES the wrong remedy: `ziggurat league ir-check` re-reports the same
    percentage and offers no next step; a coverage gap is repaired by a PULL.
    """
    payload, pool = league_world(
        holdings={"1000": 10, "1001": 10}, drop_from_pool=("1001",))
    for entry in pool:
        entry["player"]["injured"] = False
        entry["player"]["droppable"] = True
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    report = state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026,
                                 own_team_id=10)
    assert report.droppable_coverage == 0.5
    assert report.has_news is True
    assert "UNDROPPABLE check did not run" in report.headline
    assert "ziggurat league sync" in report.headline
    text = state.format_ir_rule_report(report)
    assert "droppable 50%" in text


def test_the_first_IR_occupant_this_league_produces_is_reported_once(
        crosswalked_db, league_world):
    """0 of 10 rosters have ever used the IR slot, which is exactly why the
    IR-SLOT MECHANISM is still UNVERIFIED — so the day one appears is the event
    the item has been waiting for. The report fired only on an occupant whose flag
    was FALSE, i.e. the least likely first case; a genuinely injured first
    occupant passed as "clean".

    CATCHES: firing on every occupant forever (a standing nag for as long as
    somebody sits on IR), and firing on none.
    """
    payload, pool = league_world(holdings={"1000": 4})
    for entry in pool:
        entry["player"]["injured"] = \
            entry["player"]["injuryStatus"] in state.IR_ELIGIBLE_STATUSES
        entry["player"]["droppable"] = True
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    assert state.ir_rule_check(
        crosswalked_db, as_of="2026-09-15", season=2026).has_news is False

    def _with_occupant(day):
        p, pl = league_world(holdings={"1000": 4}, slots={"1000": 21})   # 21 = IR
        for entry in pl:
            entry["player"]["injured"] = \
                entry["player"]["injuryStatus"] in state.IR_ELIGIBLE_STATUSES
            entry["player"]["droppable"] = True
        pl[0]["player"]["injuryStatus"] = "OUT"
        pl[0]["player"]["injured"] = True        # a GENUINELY injured occupant
        _ingest(crosswalked_db, p, pl, day=day)
        return state.ir_rule_check(crosswalked_db, as_of=day, season=2026)

    arrived = _with_occupant("2026-09-16")
    assert len(arrived.new_occupants) == 1
    assert arrived.has_news is True
    assert "appeared for the first time" in arrived.headline

    still_there = _with_occupant("2026-09-17")
    assert still_there.ir_occupants and still_there.new_occupants == ()
    assert still_there.has_news is False, "a standing occupant is not daily news"


def test_ir_check_names_the_app_check_that_settles_the_item_today(
        crosswalked_db, league_world):
    """The report is the surface the open IR-SLOT question lives on, and the only
    action that can close it TODAY is a ~30-second check on the ESPN roster page —
    which appeared nowhere in its output. ``IR_FIX_MODEL_LABEL`` carries it, but
    that label renders only when a verdict rests on an IR occupant, and there are
    none: the sentence was unreachable by construction in exactly the state that
    keeps the question open.

    Amended 2026-09-03: the operator ran the check on the WEBSITE (no app), and
    the "refusal" turned out to be an ABSENT menu option — a player's MOVE button
    lists only the moves ESPN accepts, and it offered IR to nobody on a roster with
    no OUT/IR player. The report must now state that observation AND the half it
    left open (an OUT/IR player being OFFERED the slot), not re-ask a question
    that has been answered, and not phrase it as an app drag the operator cannot
    perform.
    """
    payload, pool = league_world(holdings={"1000": 4})
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    text = state.format_ir_rule_report(
        state.ir_rule_check(crosswalked_db, as_of="2026-09-15", season=2026))
    assert "IR-slot occupants league-wide: 0" in text
    assert "observed 2026-09-03" in text
    assert "offered IR to NOBODY" in text
    assert "open his MOVE menu" in text
    assert "dragging" not in text and "the ESPN app" not in text
    assert "IMPLEMENTATION_PLAN.md §3.8" in text


def test_uncaptured_acquisition_fields_are_not_reported_as_espns_sentinels(
        crosswalked_db, league_world):
    """``validate_settings_row`` never required these, so a row with them missing
    STORES and the run logs ``ok`` — and the verdict then told the operator, in a
    decision sentence, that FAAB is off and no cap binds, off three fields nobody
    captured. That is the identical NULL-is-not-False bug this build already fixed
    one field over, left in place on the higher-stakes one.

    CATCHES: ``bool(settings.get(...))`` and a caps sentence that attributes -1 to
    a NULL.
    """
    payload, _pool = league_world()
    acq = payload["settings"]["acquisitionSettings"]
    del acq["isUsingAcquisitionBudget"]
    del acq["acquisitionLimit"]
    del acq["matchupAcquisitionLimit"]
    del payload["settings"]["rosterSettings"]["moveLimit"]
    row = state.map_settings(payload, season=2026, scoring_period=3)
    # the row is deliberately still STORED — refusing it would discard the
    # position limits, waiver days and trade deadline the same snapshot did carry
    assert state.validate_settings_row(row) is None
    assert row["is_using_acquisition_budget"] is None

    blob = " ".join(state.settings_verdicts(row))
    assert "NOT CAPTURED" in blob
    assert "isUsingAcquisitionBudget=false" not in blob
    assert "INERT" not in blob
    assert "-1 'unlimited' sentinel" not in blob
    assert "None" not in blob


def test_the_faab_settled_date_does_not_move_with_the_injury_baseline(
        crosswalked_db, league_world, monkeypatch):
    """Two unrelated facts that merely share a date. The injury baseline is a
    WATCH floor that is expected to be re-measured (DOUBTFUL, PUP, NFI); the
    acquisition observation is settled and never moves. Borrowing one constant for
    the other silently re-dates an unrelated sentence.
    """
    payload, _pool = league_world()
    row = state.map_settings(payload, season=2026, scoring_period=3)
    monkeypatch.setattr(state, "IR_RULE_BASELINE_DATE", "2099-01-01")
    blob = " ".join(state.settings_verdicts(row))
    assert state.ACQUISITION_SETTLED_DATE in blob
    assert "2099-01-01" not in blob


def test_the_trade_deadline_names_the_first_week_after_it_not_the_last_before(
        crosswalked_db, league_world):
    """The derived number is the FIRST REG week starting after the deadline, and a
    deadline precedes EVERY week after it — so "the last NFL week it precedes is
    week 13" named the wrong end of the season and reads to a novice as "trades
    are fine through week 13".

    CATCHES: re-inverting the quantifier. The number (13) does not change; the
    frame around it does.
    """
    payload, pool = league_world()
    _ingest(crosswalked_db, payload, pool, day="2026-09-15")
    crosswalked_db.executemany(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
        "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("g12", 2026, 12, "REG", "2026-11-26", "2026-07-01", "2026-07-01"),
         ("g13", 2026, 13, "REG", "2026-12-03", "2026-07-01", "2026-07-01")],
    )
    crosswalked_db.commit()
    settings = state.get_league_settings(crosswalked_db, as_of="2026-09-15", season=2026)
    blob = " ".join(state.settings_verdicts(settings, conn=crosswalked_db,
                                            as_of="2026-09-15"))
    assert "FIRST NFL games after it are week 13's" in blob
    assert "last NFL week it precedes" not in blob


def test_the_settings_page_makes_no_unmeasured_claim_about_which_cap_is_tighter(
        crosswalked_db, league_world):
    """``format_settings`` appended "this board's own caps are tighter" to every
    settings row — a comparison it never performed and structurally cannot
    (``league/`` may not import ``core/``). It is already false at RB 8 / WR 8 /
    TE 3, where the two fences TIE, and it inverts in the only case the
    ``effective_position_caps`` seam exists for: a commissioner tightening a limit.

    CATCHES: a hard-coded comparative, and a "-1 = unlimited" legend on a page
    that renders the WORD "unlimited" and so can never show a -1.
    """
    payload, _pool = league_world()
    row = state.map_settings(payload, season=2026, scoring_period=3)
    decoded = {**row, "retrieved_as_of": "2026-09-15",
               **{c: json.loads(row[c]) for c in state._SETTINGS_JSON_COLUMNS}}
    plain = state.format_settings(decoded)
    assert "this board's own caps are tighter" not in plain
    assert "-1 = unlimited" not in plain          # nothing on the page renders as -1

    from ziggurat.core.marginal import describe_league_limits
    tight = state.format_settings(
        decoded, limits_note=describe_league_limits({"QB": 1, "RB": 2, "WR": 8,
                                                     "TE": 3, "K": 3, "D/ST": 3}))
    assert "QB 1" in tight and "RB 2" in tight
