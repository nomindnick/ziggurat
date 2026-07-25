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


# ===========================================================================
# Upstream ships the same player twice on one primary key (item 3.2c follow-up)
# ===========================================================================
#
# Measured on the live 2026-07-24 FantasyPros scrape: 179 duplicate key groups
# in the raw frame, exactly ONE of which survives the IDP filter — Travis Hunter
# (WR/CB, fantasypros_id 26034) in the `rp` list, shipped as
#     ecr=66.00 sd= 1.00 best=65 worst= 67
#     ecr=66.09 sd=12.36 best=38 worst=112
# Both rows were ranked, INSERT OR REPLACE stored one, and the published WR board
# had no rank 64 — so every WR below 63 read one rank better than the truth, in
# the column core/divergence.py leads its report with.


def _hunter_frame(scrape_date="2026-08-15", wide_first=True):
    """Three WRs, one of them shipped twice on the same primary key.

    `wide_first` puts the WIDE-dispersion row FIRST, i.e. in the position where
    upstream's row order would have INSERT OR REPLACE keep the NARROW one. The
    survivor rule is a property of the data, not of scan order, so the outcome
    must not depend on this flag.
    """
    wide = (26034, "Travis Hunter", "WR", "JAC", 66.09, 12.36, 38, 112, 67.2)
    narrow = (26034, "Travis Hunter", "WR", "JAC", 66.00, 1.00, 65, 67, 67.2)
    dupes = [wide, narrow] if wide_first else [narrow, wide]
    rows = [
        (10303, "Ja'Marr Chase", "WR", "CIN", 2.10, 0.95, 1, 4, 99.9),
        (10404, "Jalen McMillan", "WR", "TB", 65.45, 9.10, 40, 95, 55.0),
        *dupes,
        (10505, "Tre Tucker", "WR", "LV", 66.78, 9.50, 41, 99, 51.0),
    ]
    df = pd.DataFrame(
        rows,
        columns=["id", "player", "pos", "team", "ecr", "sd", "best", "worst", "player_owned_avg"],
    )
    df["ecr_type"] = "rp"
    df["scrape_date"] = scrape_date
    return df


@pytest.mark.parametrize("wide_first", [True, False])
def test_a_duplicated_player_leaves_no_hole_in_the_positional_board(db, wide_first):
    """The defect end to end: 5 rows offered, 4 keys stored, ranks 1..4 with no gap.

    Before the fix this stored 4 rows numbered 1,2,3,5 and returned 5.
    """
    _stub_players(db, _CROSSWALK)
    with base.collect_drops() as tally:
        written = adp_rankings.ingest_adp_rankings(
            db, _hunter_frame(wide_first=wide_first), retrieved_as_of="2026-08-15")

    stored = db.execute("SELECT COUNT(*) FROM adp_rankings").fetchone()[0]
    assert written == stored == 4          # the count is the count SQLite kept
    # The loss reaches run_ingest's drop ceiling — not the by-design `filtered`
    # channel, which is deliberately excluded from it.
    assert tally["collapsed"] == 1 and tally["duplicated"] == 0
    assert tally["dropped"] == 0

    board = {
        r["pos_rank"]: r["player"]
        for r in adp_rankings.get_adp_rankings(
            db, as_of="2026-08-15", position="WR", ecr_type="rp")
    }
    assert sorted(board) == [1, 2, 3, 4]   # contiguous: no rank vanished with the row
    assert board == {1: "Ja'Marr Chase", 2: "Jalen McMillan",
                     3: "Travis Hunter", 4: "Tre Tucker"}


@pytest.mark.parametrize("wide_first", [True, False])
def test_the_survivor_is_the_wider_dispersion_row_whatever_the_scan_order(db, wide_first):
    """Which row wins is decided by the DATA, not by where upstream put it.

    sd=1.00 on the most contested player on the board is a confident-sounding
    number a novice cannot smell; sd=12.36 with a 38-112 band is the full expert
    panel. Fixing the winner on scan order is the `base.gsis_by_pfr` failure
    class, and here it would silently restate an uncertainty already published.
    """
    _stub_players(db, _CROSSWALK)
    adp_rankings.ingest_adp_rankings(
        db, _hunter_frame(wide_first=wide_first), retrieved_as_of="2026-08-15")
    hunter = [
        r for r in adp_rankings.get_adp_rankings(db, as_of="2026-08-15", ecr_type="rp")
        if r["fantasypros_id"] == "26034"
    ]
    assert len(hunter) == 1
    assert (hunter[0]["sd"], hunter[0]["best"], hunter[0]["worst"]) == (12.36, 38, 112)
    assert hunter[0]["ecr"] == 66.09


def test_a_byte_identical_repeat_is_not_reported_as_a_lost_fact(db):
    """Upstream shipping the same row twice loses nothing; it must not push a
    healthy scrape toward the drop ceiling. Separated by FULL-ROW equality."""
    _stub_players(db, _CROSSWALK)
    df = _hunter_frame()
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)   # repeat Ja'Marr Chase verbatim
    with base.collect_drops() as tally:
        written = adp_rankings.ingest_adp_rankings(db, df, retrieved_as_of="2026-08-15")
    assert written == 4
    assert tally["duplicated"] == 1     # the verbatim repeat
    assert tally["collapsed"] == 1      # still only the Hunter pair


def test_a_hole_in_the_positional_board_is_refused_not_stored(db):
    """The post-condition has teeth of its own.

    `_check_pos_rank_contiguous` is unreachable through the shipped path (the
    dedupe makes contiguity structural), so it is exercised directly — otherwise
    it is a guard nothing proves works, which is the class of defect this round
    exists to close.
    """
    holed = [
        {"ecr_type": "rp", "scrape_date": "2026-08-15", "position": "WR", "pos_rank": 1},
        {"ecr_type": "rp", "scrape_date": "2026-08-15", "position": "WR", "pos_rank": 3},
    ]
    with pytest.raises(adp_rankings.PosRankDiscontinuity, match="missing \\[2\\]"):
        adp_rankings._check_pos_rank_contiguous(holed)
    # ...and it accepts the shape the ingester actually produces.
    ok = [dict(r, pos_rank=i) for i, r in enumerate(holed, start=1)]
    adp_rankings._check_pos_rank_contiguous(ok)


def test_the_ingester_checks_the_board_before_it_stores_it(db, monkeypatch):
    """The guard is wired, not merely present.

    A test that only calls `_check_pos_rank_contiguous` directly stays green when
    somebody deletes the call from `ingest_adp_rankings` — the shape of defect
    this round is closing. Ranking is broken here on purpose (it is otherwise
    unbreakable from outside), and the ingest must refuse rather than store.
    """
    _stub_players(db, _CROSSWALK)

    def _holed(rows):
        for i, row in enumerate(rows, start=1):
            row["pos_rank"] = i + 1          # 2..n+1: no rank 1 anywhere

    monkeypatch.setattr(adp_rankings, "_assign_pos_rank", _holed)
    with pytest.raises(adp_rankings.PosRankDiscontinuity):
        adp_rankings.ingest_adp_rankings(db, _hunter_frame(), retrieved_as_of="2026-08-15")
    assert db.execute("SELECT COUNT(*) FROM adp_rankings").fetchone()[0] == 0


def test_a_null_key_column_is_passed_through_rather_than_folded(db):
    """`adp_rankings.scrape_date` is a NULLABLE primary-key column, and SQLite
    treats every NULL in a UNIQUE index as distinct — so two NULL-dated rows for
    one player are two rows SQLite keeps. Folding them in Python would delete a
    row the database would have kept, which is worse than the miscount it fixes.

    Exercised at the unit level because the shipped ingest path cannot reach it
    today: knowable_as_of is derived from scrape_date and declared NOT NULL, so a
    NULL-dated row raises at the insert. That invariant rests on a DIFFERENT
    column's constraint than the one being keyed on, which is exactly the coupling
    a future migration breaks without noticing.
    """
    def _row(ecr):
        return {"fantasypros_id": "10303", "ecr_type": "rp", "scrape_date": None,
                "retrieved_as_of": "2026-08-15", "ecr": ecr, "sd": 1.0,
                "best": 1, "worst": 9, "position": "WR"}

    with base.collect_drops() as tally:
        kept = adp_rankings._dedupe_on_key([_row(2.10), _row(3.10)])
    assert len(kept) == 2
    assert tally["collapsed"] == 0 and tally["duplicated"] == 0


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
