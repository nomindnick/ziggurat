"""Cached-fixture integration + leakage + gap-validation tests for the player
crosswalk (the exemplar every source copies)."""

from ziggurat.data.nfl import players


def test_ingest_and_get(db, nfl_fixture):
    df = nfl_fixture("ids")
    n = players.ingest_players(db, df, retrieved_as_of="2024-08-01")
    assert n > 0
    rows = players.get_players(db, as_of="2024-08-01")
    assert len(rows) == n  # one row per gsis at this single retrieval


def test_numeric_ids_are_normalized_to_bare_digit_strings(db, nfl_fixture):
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2024-08-01")
    espn = players.id_crosswalk(db, as_of="2024-08-01", id_from="gsis_id", id_to="espn_id")
    assert espn, "expected some gsis->espn mappings"
    # No stray '.0' float tails — these must join to ESPN's integer ids.
    assert all(v.isdigit() for v in espn.values())


def test_crosswalk_bridges_gsis_and_sleeper(db, nfl_fixture):
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2024-08-01")
    sleeper = players.id_crosswalk(db, as_of="2024-08-01", id_from="gsis_id", id_to="sleeper_id")
    assert sleeper  # Phase-4 Sleeper proxy keys on this


def test_dst_gap_is_expected(db, nfl_fixture):
    # The crosswalk has NO team-defense entries — defenses key by team abbr, not
    # here. A validation test so this known gap can't silently regress.
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2024-08-01")
    positions = {r["position"] for r in players.get_players(db, as_of="2024-08-01")}
    assert "DEF" not in positions and "DST" not in positions


def test_players_leakage_before_retrieval(db, nfl_fixture):
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2024-08-01")
    assert players.get_players(db, as_of="2024-07-31") == []
