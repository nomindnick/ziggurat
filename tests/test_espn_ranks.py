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
            espn_s2="x", swid="y", retrieved_as_of="2026-07-20", today="2026-07-20",
        )
    m.assert_called_once()
    assert n == len(_raw_players())
    got = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-20", season=2026)
    assert len(got) == n


# ------------------------------------------------- collapse floor (item 3.1b)
#
# ingest_espn_ranks is the ONLY delete-then-write path in ziggurat/data/nfl/, and
# the delete is scoped to (season, TODAY) — today's partition is the one the
# draft cockpit reads. The 3.1b probe reproduced the item-3.1 destroy-the-day bug
# here against the live DB with real credentials: a 20-player degraded response
# replaced a stored 1,026-player same-day board, and an empty response wiped it
# to zero (the coverage guard sat behind `if rows:`; the DELETE did not).


def _board(n, *, start=1):
    """A well-formed n-player board slice (every row carries a PPR rank, so the
    editorial-coverage guard cannot be what refuses it)."""
    return [
        {"id": i, "defaultPositionId": 2, "proTeamId": 8, "fullName": f"P{i}",
         "draftRanksByRankType": {"PPR": {"rank": i}}, "ownership": {"averageDraftPosition": i}}
        for i in range(start, start + n)
    ]


def test_degraded_same_day_pull_is_refused_and_the_stored_board_survives(db):
    """THE property this item exists to protect: the DB holds a draft board three
    weeks before draft day, and a bad refresh must not destroy it."""
    espn_ranks.ingest_espn_ranks(db, _board(1000), retrieved_as_of="2026-07-24", season=2026)
    before = db.execute("SELECT COUNT(*) c FROM espn_draft_ranks").fetchone()["c"]
    assert before == 1000

    with pytest.raises(espn_ranks.BoardCollapse, match="degraded"):
        espn_ranks.ingest_espn_ranks(db, _board(20), retrieved_as_of="2026-07-24", season=2026)

    after = db.execute("SELECT COUNT(*) c FROM espn_draft_ranks").fetchone()["c"]
    assert after == before, "the refused pull must leave the stored board untouched"
    # And the board still READS whole — not a stale/mixed hybrid.
    assert len(espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-24", season=2026)) == 1000


def test_empty_pull_is_refused_even_with_no_stored_board(db):
    # An empty board is never right, so this floor is unconditional and is NOT
    # covered by allow_shrink. It is also the case the old `if rows:` guard
    # skipped entirely on its way to an unconditional DELETE.
    with pytest.raises(espn_ranks.BoardCollapse, match="EMPTY"):
        espn_ranks.ingest_espn_ranks(db, [], retrieved_as_of="2026-07-24", season=2026)
    assert db.execute("SELECT COUNT(*) c FROM espn_draft_ranks").fetchone()["c"] == 0


def test_empty_pull_never_wipes_a_stored_board(db):
    espn_ranks.ingest_espn_ranks(db, _board(500), retrieved_as_of="2026-07-24", season=2026)
    with pytest.raises(espn_ranks.BoardCollapse):
        espn_ranks.ingest_espn_ranks(db, [], retrieved_as_of="2026-07-24", season=2026)
    assert db.execute("SELECT COUNT(*) c FROM espn_draft_ranks").fetchone()["c"] == 500


def test_floor_measures_against_todays_own_earlier_snapshot(db):
    # The yardstick deliberately includes TODAY's snapshot: the cockpit refreshes
    # the board several times a day, so a same-day re-pull must not be allowed to
    # shrink what an earlier run of the same day already captured.
    espn_ranks.ingest_espn_ranks(db, _board(400), retrieved_as_of="2026-07-24", season=2026)
    with pytest.raises(espn_ranks.BoardCollapse):
        espn_ranks.ingest_espn_ranks(db, _board(100), retrieved_as_of="2026-07-24", season=2026)
    assert db.execute("SELECT COUNT(*) c FROM espn_draft_ranks").fetchone()["c"] == 400


def test_a_mild_shrink_inside_the_floor_is_accepted(db):
    # The floor must not be so tight that normal churn (ESPN trimming the pool)
    # blocks a legitimate refresh. 0.75 of 400 is 300.
    espn_ranks.ingest_espn_ranks(db, _board(400), retrieved_as_of="2026-07-24", season=2026)
    n = espn_ranks.ingest_espn_ranks(db, _board(350), retrieved_as_of="2026-07-24", season=2026)
    assert n == 350
    assert db.execute("SELECT COUNT(*) c FROM espn_draft_ranks").fetchone()["c"] == 350


def test_allow_shrink_is_the_operators_explicit_override(db):
    espn_ranks.ingest_espn_ranks(db, _board(1000), retrieved_as_of="2026-07-24", season=2026)
    n = espn_ranks.ingest_espn_ranks(
        db, _board(20), retrieved_as_of="2026-07-24", season=2026, allow_shrink=True
    )
    assert n == 20
    assert db.execute("SELECT COUNT(*) c FROM espn_draft_ranks").fetchone()["c"] == 20


def test_first_board_of_a_season_has_nothing_to_compare_against(db):
    n = espn_ranks.ingest_espn_ranks(db, _board(30), retrieved_as_of="2026-07-24", season=2026)
    assert n == 30


def test_a_new_seasons_first_board_is_not_floored_by_last_seasons(db):
    espn_ranks.ingest_espn_ranks(db, _board(1000), retrieved_as_of="2025-08-01", season=2025)
    # Different season = a different partition; a small first 2026 board is fine.
    assert espn_ranks.ingest_espn_ranks(
        db, _board(40), retrieved_as_of="2026-07-24", season=2026
    ) == 40


def test_pull_passes_allow_shrink_through_to_the_floor(db):
    espn_ranks.ingest_espn_ranks(db, _board(1000), retrieved_as_of="2026-07-24", season=2026)
    with patch.object(espn_source, "fetch_player_universe", return_value=_board(20)):
        with pytest.raises(espn_ranks.BoardCollapse):
            espn_ranks.pull_espn_ranks(
                db, league_id=1, season=2026, espn_s2="x", swid="y",
                retrieved_as_of="2026-07-24", today="2026-07-24",
            )
        assert espn_ranks.pull_espn_ranks(
            db, league_id=1, season=2026, espn_s2="x", swid="y",
            retrieved_as_of="2026-07-24", today="2026-07-24", allow_shrink=True,
        ) == 20


def test_the_floor_protects_the_partition_BEING_REPLACED_not_just_the_newest(db):
    """3.1b audit: the yardstick was MAX(retrieved_as_of) while the DELETE targets
    ``stamp``. With a large old board and a small current one, a mid-sized
    back-stamped write cleared the floor computed from the CURRENT board and wiped
    the historical partition it was actually deleting (2051 -> 600, measured)."""
    espn_ranks.ingest_espn_ranks(db, _board(2000), retrieved_as_of="2026-07-01", season=2026)
    espn_ranks.ingest_espn_ranks(db, _board(500), retrieved_as_of="2026-07-24", season=2026,
                                 allow_shrink=True)
    with pytest.raises(espn_ranks.BoardCollapse):
        # 600 clears 0.75 x 500 (the newest partition) but not 0.75 x 2000 (the
        # partition this write deletes).
        espn_ranks.ingest_espn_ranks(db, _board(600), retrieved_as_of="2026-07-01",
                                     season=2026)
    kept = db.execute(
        "SELECT COUNT(*) c FROM espn_draft_ranks WHERE retrieved_as_of = '2026-07-01'"
    ).fetchone()["c"]
    assert kept == 2000


def test_a_key_collapsing_response_is_refused_and_rolled_back(db):
    """The floor compares distinct board_keys, and the write is re-counted inside
    the transaction. A response with duplicate ids used to clear the floor on
    ``len(rows)``, collapse the board onto a handful of keys, and still report
    rows_written=1000."""
    espn_ranks.ingest_espn_ranks(db, _board(1000), retrieved_as_of="2026-07-24", season=2026)
    dupes = _board(1000)
    for row in dupes:
        row["id"] = 4242            # every row lands on ONE board_key
    with pytest.raises(espn_ranks.BoardCollapse):
        espn_ranks.ingest_espn_ranks(db, dupes, retrieved_as_of="2026-07-24", season=2026)
    assert db.execute("SELECT COUNT(*) c FROM espn_draft_ranks").fetchone()["c"] == 1000


def test_ingest_returns_the_stored_count_not_the_incoming_list_length(db):
    dupes = _board(40) + _board(10)          # 10 rows repeat
    assert espn_ranks.ingest_espn_ranks(
        db, dupes, retrieved_as_of="2026-07-24", season=2026) == 40


def test_a_live_pull_cannot_be_back_stamped_over_a_stored_board(db):
    """`valuation --espn --as-of <past day>` did exactly this: it replaced the
    stored past board with today's, permanently losing a perishable point-in-time
    observation AND making today's ranks readable at that past as_of under the
    default historical view."""
    espn_ranks.ingest_espn_ranks(db, _board(1000), retrieved_as_of="2026-07-21", season=2026)
    with patch.object(espn_source, "fetch_player_universe", return_value=_board(1000, start=5000)):
        with pytest.raises(ValueError, match="refusing to store a LIVE ESPN board"):
            espn_ranks.pull_espn_ranks(db, league_id=1, season=2026, espn_s2="x", swid="y",
                                       retrieved_as_of="2026-07-21", today="2026-07-24")
    stored = espn_ranks.get_espn_draft_ranks(db, as_of="2026-07-21", season=2026)
    assert {r["board_key"] for r in stored} == {str(i) for i in range(1, 1001)}


def test_ensure_board_reads_a_past_day_instead_of_overwriting_it(db):
    espn_ranks.ingest_espn_ranks(db, _board(30), retrieved_as_of="2026-07-21", season=2026)
    with patch.object(espn_source, "fetch_player_universe") as fetch:
        note = espn_ranks.ensure_board(db, league_id=1, season=2026, espn_s2="x", swid="y",
                                       as_of="2026-07-21", today="2026-07-24")
    fetch.assert_not_called()
    assert "stored" in note and "30 rows" in note


def test_ensure_board_refreshes_when_as_of_is_today(db):
    with patch.object(espn_source, "fetch_player_universe", return_value=_board(30)) as fetch:
        note = espn_ranks.ensure_board(db, league_id=1, season=2026, espn_s2="x", swid="y",
                                       as_of="2026-07-24", today="2026-07-24")
    fetch.assert_called_once()
    assert "refreshed" in note


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
