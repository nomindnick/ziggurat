"""Cached-fixture integration + leakage tests for market-ECR (adp_rankings) ingestion.

ECR rows are stamped with their FantasyPros ``scrape_date`` as knowable_as_of (a
Friday scrape is knowable before that week's games). The crosswalk stub inserts a
handful of players so the FantasyPros id -> (gsis_id, espn_id) resolution can be
asserted; DST + IDP ids are deliberately absent from the stub (DST keeps a NULL
gsis_id and joins by team abbr; IDP is dropped entirely). Fixtures are built
inline — small, transparent NFL-data snippets, no league-private data.
"""

import pandas as pd
import pytest

from ziggurat.data.nfl import adp_rankings, base


def _stub_players(db, mapping, *, retrieved="2026-08-01"):
    """Insert a minimal players crosswalk: fantasypros_id -> (gsis_id, espn_id).
    fantasypros_id/espn_id are already bare digit strings (players.py normalizes)."""
    for fp, (gsis, espn) in mapping.items():
        db.execute(
            "INSERT INTO players (gsis_id, fantasypros_id, espn_id, retrieved_as_of, knowable_as_of) "
            "VALUES (?,?,?,?,?)",
            (gsis, fp, espn, retrieved, retrieved),
        )
    db.commit()


def _fixture_frame(scrape_date="2026-08-15"):
    """A few QB/RB/WR + 1 K + 2 DST (LAR, JAC to exercise both aliases) + 1 IDP LB,
    all redraft-overall ('ro'), one scrape_date. ECR order is deliberate so the
    derived pos_rank is checkable."""
    rows = [
        # id, player, pos, team, ecr, sd, best, worst, owned
        (17298, "Josh Allen", "QB", "BUF", 25.19, 1.16, 21, 31, 99.5),
        (10101, "Bijan Robinson", "RB", "ATL", 1.50, 0.80, 1, 3, 99.9),
        (10202, "Saquon Barkley", "RB", "PHI", 4.20, 1.90, 2, 8, 99.8),
        (10303, "Ja'Marr Chase", "WR", "CIN", 2.10, 0.95, 1, 4, 99.9),
        (26068, "Brandon Aubrey", "K", "DAL", 187.14, 1.21, 185, 193, 97.5),
        (8130, "Los Angeles Rams", "DST", "LAR", 150.0, 6.0, 145, 180, 90.0),
        (8000, "Jacksonville Jaguars", "DST", "JAC", 200.0, 8.0, 190, 240, 85.0),
        (19292, "Jordyn Brooks", "LB", "MIA", 1.67, 1.11, 1, 4, 82.7),  # IDP -> dropped
    ]
    df = pd.DataFrame(
        rows,
        columns=["id", "player", "pos", "team", "ecr", "sd", "best", "worst", "player_owned_avg"],
    )
    df["ecr_type"] = "ro"
    df["scrape_date"] = scrape_date
    return df


# Crosswalk: only the skill players + K resolve; DST team-ids and the IDP id are absent.
_CROSSWALK = {
    "17298": ("00-0034857", "3918298"),   # Josh Allen
    "10101": ("00-0038542", "4430807"),   # Bijan Robinson
    "10202": ("00-0034844", "3929630"),   # Saquon Barkley
    "10303": ("00-0036900", "4362628"),   # Ja'Marr Chase
    "26068": ("00-0037476", "4249087"),   # Brandon Aubrey
}


def test_ingest_drops_idp_and_resolves_ids(db):
    _stub_players(db, _CROSSWALK)
    df = _fixture_frame()
    n = adp_rankings.ingest_adp_rankings(db, df, retrieved_as_of="2026-08-15")
    # 8 source rows, 1 IDP LB dropped -> 7 kept.
    assert n == 7

    rows = adp_rankings.get_adp_rankings(db, as_of="2026-08-15", season=2026)
    by_player = {r["player"]: r for r in rows}

    # IDP LB dropped entirely.
    assert "Jordyn Brooks" not in by_player
    assert all(r["position"] in adp_rankings.LEAGUE_POSITIONS for r in rows)

    # Offense + K resolve BOTH ids.
    allen = by_player["Josh Allen"]
    assert allen["gsis_id"] == "00-0034857"
    assert allen["espn_id"] == "3918298"
    assert allen["fantasypros_id"] == "17298"
    aubrey = by_player["Brandon Aubrey"]
    assert aubrey["gsis_id"] == "00-0037476"
    assert aubrey["espn_id"] == "4249087"

    # player_owned_avg persisted.
    assert allen["player_owned_avg"] == 99.5

    # knowable_as_of == scrape_date.
    assert allen["knowable_as_of"] == "2026-08-15"
    assert allen["scrape_date"] == "2026-08-15"
    assert allen["season"] == 2026


def test_dst_null_gsis_and_team_alias(db):
    _stub_players(db, _CROSSWALK)
    adp_rankings.ingest_adp_rankings(db, _fixture_frame(), retrieved_as_of="2026-08-15")
    rows = adp_rankings.get_adp_rankings(db, as_of="2026-08-15", position="DST")
    by_team = {r["team"]: r for r in rows}

    # Both DST resolved: NULL gsis_id, joined by NORMALIZED team abbr.
    assert set(by_team) == {"LA", "JAX"}  # LAR->LA, JAC->JAX applied
    assert by_team["LA"]["gsis_id"] is None
    assert by_team["JAX"]["gsis_id"] is None
    assert by_team["LA"]["player"] == "Los Angeles Rams"


def test_pos_rank_over_league_positions_only(db):
    _stub_players(db, _CROSSWALK)
    adp_rankings.ingest_adp_rankings(db, _fixture_frame(), retrieved_as_of="2026-08-15")
    rbs = {
        r["player"]: r["pos_rank"]
        for r in adp_rankings.get_adp_rankings(db, as_of="2026-08-15", position="RB")
    }
    # Bijan (ecr 1.50) ranks ahead of Saquon (ecr 4.20).
    assert rbs["Bijan Robinson"] == 1
    assert rbs["Saquon Barkley"] == 2
    # Single-member positions are pos_rank 1.
    qb = adp_rankings.get_adp_rankings(db, as_of="2026-08-15", position="QB")
    assert qb[0]["pos_rank"] == 1


def test_require_columns_fails_loud(db):
    df = _fixture_frame().drop(columns=["ecr"])
    with pytest.raises(ValueError, match="missing required columns"):
        adp_rankings.ingest_adp_rankings(db, df, retrieved_as_of="2026-08-15")


def _one_row(fp_id, ecr, scrape_date):
    df = pd.DataFrame(
        [(fp_id, "Josh Allen", "QB", "BUF", ecr, 1.0, 20, 30, 99.0)],
        columns=["id", "player", "pos", "team", "ecr", "sd", "best", "worst", "player_owned_avg"],
    )
    df["ecr_type"] = "ro"
    df["scrape_date"] = scrape_date
    return df


def test_adp_leakage_across_scrape_dates_and_revision(db):
    _stub_players(db, {"17298": ("00-0034857", "3918298")})
    w1, w2 = "2026-09-05", "2026-09-12"

    # Two scrapes for the same player, both retrieved on W1 so the KNOWABLE gate
    # (scrape_date) is the binding constraint for the W1/W2 distinction.
    adp_rankings.ingest_adp_rankings(db, _one_row(17298, 25.0, w1), retrieved_as_of=w1)
    adp_rankings.ingest_adp_rankings(db, _one_row(17298, 22.0, w2), retrieved_as_of=w1)

    # as_of=W1 sees only the W1 scrape (W2 scrape not yet knowable).
    seen = adp_rankings.get_adp_rankings(db, as_of=w1, season=2026)
    assert {r["scrape_date"] for r in seen} == {w1}
    assert seen[0]["ecr"] == 25.0

    # as_of=W2 sees both scrapes.
    later = {r["scrape_date"] for r in adp_rankings.get_adp_rankings(db, as_of=w2, season=2026)}
    assert later == {w1, w2}

    # A later-RETRIEVED correction of the W1 scrape (different ecr).
    adp_rankings.ingest_adp_rankings(db, _one_row(17298, 30.0, w1), retrieved_as_of="2026-09-20")

    # historical at as_of=W1: correction was retrieved later -> still the original.
    hist = adp_rankings.get_adp_rankings(db, as_of=w1, season=2026)
    assert [r for r in hist if r["scrape_date"] == w1][0]["ecr"] == 25.0

    # latest_truth relaxes only the retrieval gate -> the correction surfaces.
    lt = base.latest_truth(adp_rankings.get_adp_rankings)(db, as_of=w1, season=2026)
    assert [r for r in lt if r["scrape_date"] == w1][0]["ecr"] == 30.0
