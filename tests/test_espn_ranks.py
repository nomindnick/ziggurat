"""ESPN draft-board mapper + ingest + as-of accessor tests (item 2.1).

Offline: the ``espn_source.fetch_player_universe`` network seam is never called.
The captured ``tests/fixtures/espn/player_universe.json`` is a scrubbed ~28-player
slice of the live 2026 ``kona_player_info`` pull (public draft-rank data only —
no onTeamId / roster / manager context; rule 5).
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ziggurat.data.nfl import base, espn_ranks, espn_source

_FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "player_universe.json"


def _raw_players():
    return json.loads(_FIXTURE.read_text())


def _by_pos(rows, position):
    return [r for r in rows if r["position"] == position]


# --------------------------------------------------------------- fixture / scrub


def test_fixture_is_well_formed_and_scrubbed():
    players = _raw_players()
    assert len(players) >= 20
    positions = {espn_ranks.DEFPOS.get(p["defaultPositionId"]) for p in players}
    assert {"QB", "RB", "WR", "TE", "K", "D/ST"} <= positions
    # rule 5: no roster/manager context leaked into the committed fixture.
    for p in players:
        assert "onTeamId" not in p
        assert set((p.get("ownership") or {}).keys()) <= {"averageDraftPosition"}
        # each player carries the two public signals we read.
        assert "PPR" in p["draftRanksByRankType"]
        assert "rank" in p["draftRanksByRankType"]["PPR"]


# -------------------------------------------------------------------- mapper


def test_position_via_default_position_id():
    for raw in _raw_players():
        mapped = espn_ranks.map_espn_player(raw)
        assert mapped["position"] == espn_ranks.DEFPOS[raw["defaultPositionId"]]


def test_skill_espn_id_is_str_id_dst_is_none():
    for raw in _raw_players():
        mapped = espn_ranks.map_espn_player(raw)
        if raw["defaultPositionId"] == 16:  # D/ST
            assert mapped["espn_id"] is None
            # negative synthetic id must NOT leak into espn_id
        else:
            assert mapped["espn_id"] == str(raw["id"])


def test_dst_negative_id_resolves_team_not_espn_id():
    dst_raw = next(p for p in _raw_players() if p["defaultPositionId"] == 16)
    assert dst_raw["id"] < 0  # synthetic negative id
    mapped = espn_ranks.map_espn_player(dst_raw)
    assert mapped["espn_id"] is None
    assert mapped["team"] is not None  # resolved via PRO_TEAM_MAP + TEAM_ALIASES


def test_team_normalizes_through_aliases():
    # PRO_TEAM_MAP emits WSH/LAR/JAC variants; base.TEAM_ALIASES normalizes them.
    for raw in _raw_players():
        mapped = espn_ranks.map_espn_player(raw)
        team = mapped["team"]
        if team is not None:
            # a normalized team is never a stale alias.
            assert team not in base.TEAM_ALIASES, f"{team} should have normalized"


def test_non_league_position_maps_to_none():
    fake = {"id": 999, "defaultPositionId": 99, "proTeamId": 7,
            "fullName": "Some Punter", "draftRanksByRankType": {}, "ownership": {}}
    assert espn_ranks.map_espn_player(fake) is None


def test_sparse_row_without_ppr_rank_maps_to_none():
    # A lone row missing the PPR block (real: fringe rookies ship only an
    # ELIMINATION rank, seen live 2026-07-24) is NO signal, not drift.
    sparse = {"id": 1, "defaultPositionId": 2, "proTeamId": 8, "fullName": "X",
              "draftRanksByRankType": {"ELIMINATION": {"rank": 5}}, "ownership": {}}
    assert espn_ranks.map_espn_player(sparse)["overall_rank"] is None

    # PPR block present but 'rank' key gone -> likewise None for that row.
    sparse2 = {"id": 2, "defaultPositionId": 2, "proTeamId": 8, "fullName": "Y",
               "draftRanksByRankType": {"PPR": {"auctionValue": 3}}, "ownership": {}}
    assert espn_ranks.map_espn_player(sparse2)["overall_rank"] is None


def test_wholesale_schema_drift_fails_loud_at_ingest(db):
    # If MOST of the snapshot has lost its PPR rank, ESPN changed the payload
    # shape -> the ingest raises instead of storing a corrupt board.
    drifted = [
        {"id": i, "defaultPositionId": 2, "proTeamId": 8, "fullName": f"P{i}",
         "draftRanksByRankType": {"STANDARD": {"rank": i}}, "ownership": {}}
        for i in range(1, 11)
    ]
    with pytest.raises(ValueError, match="schema drift"):
        espn_ranks.ingest_espn_ranks(db, drifted, retrieved_as_of="2026-07-24", season=2026)


def test_minority_sparse_rows_ingest_and_rank_last(db):
    # One sparse row in an otherwise-covered snapshot ingests, and its derived
    # pos rank sorts LAST within its position (None never precedes a ranked row).
    raws = _raw_players()
    sparse = {"id": 999999, "defaultPositionId": 2, "proTeamId": 8,
              "fullName": "Sparse Rookie",
              "draftRanksByRankType": {"ELIMINATION": {"rank": 1}}, "ownership": {}}
    n = espn_ranks.ingest_espn_ranks(
        db, raws + [sparse], retrieved_as_of="2026-07-24", season=2026
    )
    assert n == len(raws) + 1
    got = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-24", season=2026)
    rbs = _by_pos(got, "RB")
    sparse_row = next(r for r in rbs if r["player"] == "Sparse Rookie")
    assert sparse_row["overall_rank"] is None
    assert sparse_row["espn_pos_rank"] == len(rbs)


# ----------------------------------------------------- pos-rank derivation


def test_pos_rank_is_monotonic_within_position():
    rows = [espn_ranks.map_espn_player(r) for r in _raw_players()]
    for r in rows:
        r["season"] = 2026
    espn_ranks._assign_pos_ranks(rows)

    for pos in ("QB", "RB", "WR", "TE", "K", "D/ST"):
        group = sorted(_by_pos(rows, pos), key=lambda r: r["espn_pos_rank"])
        # ranks are a dense 1..n sequence
        assert [r["espn_pos_rank"] for r in group] == list(range(1, len(group) + 1))
        # ordering by editorial overall_rank is monotonic (lower rank = better)
        overalls = [r["overall_rank"] for r in group]
        assert overalls == sorted(overalls)
        # the ADP-derived rank is an independent 1..n sequence too
        adp_group = sorted(_by_pos(rows, pos), key=lambda r: r["espn_adp_pos_rank"])
        assert [r["espn_adp_pos_rank"] for r in adp_group] == list(range(1, len(adp_group) + 1))


# ------------------------------------------------------------ ingest + accessor


def _ingest_fixture(db, *, retrieved_as_of, season=2026):
    return espn_ranks.ingest_espn_ranks(
        db, _raw_players(), retrieved_as_of=retrieved_as_of, season=season
    )


def test_ingest_and_read_roundtrip(db):
    n = _ingest_fixture(db, retrieved_as_of="2026-07-20")
    assert n == len(_raw_players())

    got = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-20", season=2026)
    assert len(got) == n
    # DST rows survive the as-of read despite NULL espn_id (board_key keyed).
    dst = [r for r in got if r["position"] == "D/ST"]
    assert len(dst) == len(_by_pos([espn_ranks.map_espn_player(p) for p in _raw_players()], "D/ST"))
    assert all(r["espn_id"] is None and r["team"] is not None for r in dst)
    # skill rows keep their string espn_id.
    skill = [r for r in got if r["position"] == "RB"]
    assert all(r["espn_id"] is not None for r in skill)


def test_ingest_is_idempotent(db):
    _ingest_fixture(db, retrieved_as_of="2026-07-20")
    _ingest_fixture(db, retrieved_as_of="2026-07-20")  # re-pull same day
    got = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-20", season=2026)
    # DST rows (NULL espn_id) must NOT duplicate on re-ingest.
    assert len(got) == len(_raw_players())


def test_position_filter(db):
    _ingest_fixture(db, retrieved_as_of="2026-07-20")
    qbs = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-20", season=2026, position="QB")
    assert qbs and all(r["position"] == "QB" for r in qbs)


def test_leakage_by_retrieval(db):
    # forward regime: knowable == retrieved == pull day; an earlier as_of cannot
    # see a later pull, and an as_of before any pull returns [].
    _ingest_fixture(db, retrieved_as_of="2026-07-18")
    _ingest_fixture(db, retrieved_as_of="2026-07-20")

    before_any = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-17", season=2026)
    assert before_any == []

    early = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-19", season=2026)
    assert early and all(r["knowable_as_of"] == "2026-07-18" for r in early)

    # latest_truth still hides a board not yet knowable by as_of.
    read_lt = base.latest_truth(espn_ranks.get_espn_draft_ranks)
    assert read_lt(db, as_of="2026-07-17", season=2026) == []
    # but relaxes the retrieval gate: at 2026-07-19 it sees the latest knowable pull
    lt = read_lt(db, as_of="2026-07-19", season=2026)
    assert lt and all(r["knowable_as_of"] == "2026-07-18" for r in lt)


def test_conflicting_view_rejected_by_latest_truth(db):
    _ingest_fixture(db, retrieved_as_of="2026-07-20")
    with pytest.raises(ValueError):
        base.latest_truth(espn_ranks.get_espn_draft_ranks)(
            db, as_of="2026-07-20", season=2026, view="historical"
        )


# --------------------------------------------------- network seam (patched)


def test_pull_uses_patched_seam(db):
    # pull_espn_ranks must route through fetch_player_universe (the one seam);
    # patching it proves no live call and wires end-to-end into the accessor.
    with patch.object(espn_source, "fetch_player_universe", return_value=_raw_players()) as m:
        n = espn_ranks.pull_espn_ranks(
            db, league_id=1160156465, season=2026,
            espn_s2="x", swid="y", retrieved_as_of="2026-07-20",
        )
    m.assert_called_once()
    assert n == len(_raw_players())
    got = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-20", season=2026)
    assert len(got) == n


def test_truncation_guard_fails_loud():
    # A returned count >= limit means ESPN capped the page -> fail loud, never cap.
    fake_payload = {"players": [{"player": {"id": i}} for i in range(50)]}

    class _FakeLeague:
        class espn_request:
            @staticmethod
            def league_get(*, params, headers):
                return fake_payload

    with patch.object(espn_source, "league_client", return_value=_FakeLeague()):
        with pytest.raises(RuntimeError, match="truncat"):
            espn_source.fetch_player_universe(
                league_id=1, season=2026, espn_s2="x", swid="y", limit=50
            )


def test_no_truncation_under_limit():
    fake_payload = {"players": [{"player": {"id": i}} for i in range(10)]}

    class _FakeLeague:
        class espn_request:
            @staticmethod
            def league_get(*, params, headers):
                return fake_payload

    with patch.object(espn_source, "league_client", return_value=_FakeLeague()):
        out = espn_source.fetch_player_universe(
            league_id=1, season=2026, espn_s2="x", swid="y", limit=50
        )
    assert len(out) == 10
