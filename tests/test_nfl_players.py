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


# ------------------------------------------------- the crosswalk-collapse floor
#
# "Append-only tables need no floor" was half true, and the wrong half was
# load-bearing (item 3.1b audit). Append-only protects against a pull that is
# MISSING ROWS. It does not protect against a pull whose VALUES arrived empty,
# because select_as_of resolves the newest row PER KEY: a same-key row with null
# ids is not absent, it wins — and every crosswalk goes to zero while the good
# rows sit unreachable underneath.


def test_a_truncated_pull_is_harmless_as_the_append_only_argument_claims(db, nfl_fixture):
    df = nfl_fixture("ids")
    players.ingest_players(db, df, retrieved_as_of="2024-08-01")
    before = players.id_crosswalk(db, as_of="2024-08-01", id_from="gsis_id", id_to="espn_id")
    players.ingest_players(db, df.head(5), retrieved_as_of="2024-08-02")
    after = players.id_crosswalk(db, as_of="2024-08-02", id_from="gsis_id", id_to="espn_id")
    assert len(after) == len(before), "untouched keys must keep resolving to their older rows"


def test_a_pull_with_emptied_id_columns_is_refused_before_it_shadows_the_crosswalk(db, nfl_fixture):
    import pytest

    df = nfl_fixture("ids")
    players.ingest_players(db, df, retrieved_as_of="2024-08-01")
    before = players.id_crosswalk(db, as_of="2024-08-01", id_from="gsis_id", id_to="espn_id")
    assert before

    emptied = df.assign(espn_id=None, pfr_id=None, fantasypros_id=None, sleeper_id=None)
    with pytest.raises(players.CrosswalkCollapse, match="espn_id"):
        players.ingest_players(db, emptied, retrieved_as_of="2024-08-02")

    # nothing written, so the good crosswalk still resolves the day after
    assert players.id_crosswalk(
        db, as_of="2024-08-02", id_from="gsis_id", id_to="espn_id") == before


def test_allow_shrink_is_the_operators_override_for_a_real_id_drop(db, nfl_fixture):
    df = nfl_fixture("ids")
    players.ingest_players(db, df, retrieved_as_of="2024-08-01")
    n = players.ingest_players(db, df.assign(espn_id=None), retrieved_as_of="2024-08-02",
                               allow_shrink=True)
    assert n > 0
    assert players.id_crosswalk(
        db, as_of="2024-08-02", id_from="gsis_id", id_to="espn_id") == {}
