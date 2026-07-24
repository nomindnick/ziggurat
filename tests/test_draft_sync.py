"""Unit tests for the DOM-sync parse/resolve module (``ziggurat/draft/sync.py``).

All offline, synthetic names only (Rule 5). The parser cases reproduce the
REAL concatenation patterns captured in the 2026-07-24 practice-draft recon
(name+status+NFLteam+pos flattened by textContent), and the resolution ladder
is pinned: espn_id beats name, AUTO commits, ambiguity refuses (Rule 6 —
refuse rather than guess a wrong pick into the board).
"""

import pytest

from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.resolver import NameResolver
from ziggurat.draft.sync import (
    parse_history_cell,
    parse_payload_pick,
    resolve_synced_pick,
)

# ----------------------------------------------------------- cell parsing


@pytest.mark.parametrize(
    "raw, name, team, pos",
    [
        ("Ja'Marr ChaseCINWR", "Ja'Marr Chase", "CIN", "WR"),
        ("Texans D/STHOUD/ST", "Texans D/ST", "HOU", "D/ST"),
        ("Rams D/STLARD/ST", "Rams D/ST", "LA", "D/ST"),  # LAR -> LA via aliases
        ("Brandon AubreyDALK", "Brandon Aubrey", "DAL", "K"),
        ("Ka'imi FairbairnHOUK", "Ka'imi Fairbairn", "HOU", "K"),
        ("Cam SkatteboQNYGRB", "Cam Skattebo", "NYG", "RB"),      # Q status flag
        ("Marvin Harrison Jr.ARIWR", "Marvin Harrison Jr.", "ARI", "WR"),
        ("Kenneth Walker IIISEARB", "Kenneth Walker III", "SEA", "RB"),
        ("DK MetcalfPITWR", "DK Metcalf", "PIT", "WR"),
        ("Amon-Ra St. BrownDETWR", "Amon-Ra St. Brown", "DET", "WR"),
    ],
)
def test_parse_history_cell_real_patterns(raw, name, team, pos):
    assert parse_history_cell(raw) == (name, team, pos)


def test_parse_history_cell_unparseable_passes_through():
    # No trailing position token -> raw text unchanged, no fabricated fields.
    assert parse_history_cell("Round 1") == ("Round 1", None, None)
    assert parse_history_cell("") == ("", None, None)


def test_status_flag_never_eats_name_capitals():
    # "DK" ends in an uppercase K but is not a status flag context; only a
    # flag following a lowercase/period tail is stripped.
    name, _, _ = parse_history_cell("Cam SkatteboQNYGRB")
    assert name == "Cam Skattebo"
    name2, _, _ = parse_history_cell("DK MetcalfPITWR")
    assert name2 == "DK Metcalf"


# ----------------------------------------------------------- payload parsing


def test_parse_payload_pick_extracts_espn_id_and_clean_name():
    p = parse_payload_pick({
        "overall": 7,
        "player": "Ja'Marr ChaseCINWR",
        "player_clean": "Ja'Marr Chase",
        "href": "https://www.espn.com/nfl/player/_/id/4362628/jamarr-chase",
        "fantasy_team": "Team 2",
    })
    assert p is not None
    assert (p.overall, p.name, p.espn_id) == (7, "Ja'Marr Chase", "4362628")
    assert p.position == "WR" and p.team == "CIN"
    assert p.fantasy_team == "Team 2"


@pytest.mark.parametrize("bad", [
    {},                                  # no fields at all
    {"overall": "x", "player": "A B"},   # non-numeric overall
    {"overall": 0, "player": "A B"},     # overall below 1
    {"overall": 3},                      # no player text
    {"overall": 3, "player": "   "},     # blank player text
])
def test_parse_payload_pick_rejects_malformed(bad):
    assert parse_payload_pick(bad) is None


# ----------------------------------------------------------- resolution ladder


def _board():
    return (
        BoardEntry("1001", "Alpha Runner", "RB", 1, 200.0, 90.0, "GB"),
        BoardEntry("1002", "Bravo Catcher", "WR", 2, 190.0, 80.0, "DET"),
        # surname collision pair for the ambiguity case
        BoardEntry("1003", "Cato Rivera", "WR", 30, 150.0, 40.0, "NO"),
        BoardEntry("1004", "Dax Rivera", "WR", 31, 149.0, 39.0, "CAR"),
        BoardEntry("dst-sf", "SF D/ST", "DST", 175, 60.0, 5.0, "SF"),
    )


def _resolve(payload, taken=frozenset()):
    board = _board()
    pick = parse_payload_pick(payload)
    assert pick is not None
    return resolve_synced_pick(NameResolver(board), board, pick, taken=taken)


def test_espn_id_match_commits_when_name_agrees():
    res = _resolve({"overall": 1, "player": "Alpha RunnerGBRB",
                    "href": "/nfl/player/_/id/1001/alpha-runner"})
    assert res.confident and res.entry.player_id == "1001"


def test_espn_id_with_contradicting_text_refuses():
    # Audit finding 2: a stale/wrong href (first <a> in the cell) must not be
    # trusted over the harvested name/pos/team — refuse, never guess.
    res = _resolve({"overall": 1, "player": "Bravo CatcherDETWR",
                    "player_clean": "Bravo Catcher",
                    "href": "/nfl/player/_/id/1001/alpha-runner"})
    assert not res.confident and res.entry is None
    assert "refusing" in res.reason


def test_espn_id_already_taken_refuses():
    res = _resolve({"overall": 2, "player": "Alpha RunnerGBRB",
                    "href": "/id/1001/"}, taken={"1001"})
    assert not res.confident and res.entry is None


def test_auto_name_match_commits_when_consistent():
    res = _resolve({"overall": 1, "player": "Bravo CatcherDETWR"})
    assert res.confident and res.entry.player_id == "1002"


def test_auto_name_match_refuses_on_position_mismatch():
    # Parsed position RB contradicts the board's WR -> refuse, never guess.
    res = _resolve({"overall": 1, "player": "Bravo CatcherDETRB"})
    assert not res.confident


def test_ambiguous_surname_refuses_without_team_evidence():
    res = _resolve({"overall": 1, "player": "Rivera"})
    assert not res.confident


def test_surname_plus_team_still_refuses_without_full_name_identity():
    # Audit finding 1 (critical): position/team "uniqueness" alone committed
    # wrong players when the real pick was off the resolver's 3-panel or off
    # the board entirely. The commit gate now demands NAME identity — a bare
    # surname, however consistent, blocks for the operator.
    res = _resolve({"overall": 1, "player": "RiveraNOWR"})
    assert not res.confident


def test_full_name_with_team_commits():
    res = _resolve({"overall": 1, "player": "Cato RiveraNOWR"})
    assert res.confident and res.entry.player_id == "1003"


def test_absent_player_never_commits_a_teammate():
    # Audit finding 1 PoC (c): a drafted player entirely absent from the board
    # must not confidently commit his board teammate.
    res = _resolve({"overall": 1, "player": "Enzo RiveraNOWR"})
    assert not res.confident


def test_suffix_drift_still_commits():
    # ESPN 'Jr.' vs a suffix-less board name is IDENTITY, not drift.
    board = (_board()[0],)  # Alpha Runner RB GB
    from ziggurat.draft.sync import parse_payload_pick as ppp
    pick = ppp({"overall": 1, "player": "Alpha Runner Jr.GBRB"})
    res = resolve_synced_pick(NameResolver(board), board, pick)
    assert res.confident and res.entry.player_id == "1001"


def test_teamless_board_entry_needs_name_identity():
    # Audit finding 3: entry.team=None used to "agree" with any harvested
    # team. Name identity now decides: same name commits, different blocks.
    board = (
        BoardEntry("2001", "Quinn Vale", "WR", 5, 100.0, 50.0, None),
        BoardEntry("2002", "Rex Vale", "WR", 20, 90.0, 40.0, "DET"),
    )
    resolver = NameResolver(board)
    ok = resolve_synced_pick(
        resolver, board,
        parse_payload_pick({"overall": 1, "player": "Quinn ValeCARWR"}),
    )
    assert ok.confident and ok.entry.player_id == "2001"
    bad = resolve_synced_pick(
        resolver, board,
        parse_payload_pick({"overall": 1, "player": "Sam ValeCARWR"}),
    )
    assert not bad.confident


def test_phantom_team_is_not_parsed_from_lowercase_name_tail():
    # Audit finding 4: "Luther Burden" must not lose "den" to a phantom DEN.
    assert parse_history_cell("Luther BurdenWR") == ("Luther Burden", None, "WR")


def test_dst_resolves_via_marked_team_name():
    res = _resolve({"overall": 9, "player": "SF D/STSFD/ST"})
    assert res.confident and res.entry.player_id == "dst-sf"


def test_unknown_name_refuses():
    res = _resolve({"overall": 1, "player": "Zzyzx NobodyLACWR"})
    assert not res.confident


def test_anchor_drift_blocks_even_with_matching_team_and_pos():
    # Re-audit finding 2: cell text names one player, but the anchor (clean
    # name + href id) names ANOTHER with the same team/pos. The gate must
    # compare against BOTH names and refuse.
    board = (
        BoardEntry("100", "Real Guy", "WR", 10, 100.0, 50.0, "KC"),
        BoardEntry("200", "Other Guy", "WR", 11, 99.0, 49.0, "KC"),
    )
    resolver = NameResolver(board)
    pick = parse_payload_pick({
        "overall": 1, "player": "Real GuyKCWR",
        "player_clean": "Other Guy", "href": "/id/200/other-guy",
    })
    res = resolve_synced_pick(resolver, board, pick)
    assert not res.confident


def test_same_name_twins_block_instead_of_committing_the_elite():
    # Re-audit finding 3: two draftable "Josh Allen"-style twins — name
    # identity cannot choose, so the pick must block.
    board = (
        BoardEntry("300", "Twin Player", "QB", 5, 300.0, 60.0, "BUF"),
        BoardEntry("301", "Twin Player", "QB", 250, 40.0, 1.0, "BUF"),
    )
    resolver = NameResolver(board)
    pick = parse_payload_pick({"overall": 1, "player": "Twin PlayerBUFQB"})
    res = resolve_synced_pick(resolver, board, pick)
    assert not res.confident and "2 different players" in res.reason
