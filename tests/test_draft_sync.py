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
        # SUFFIX + STATUS TOGETHER — both cells captured verbatim from a live
        # practice draft, 2026-08-27, where both BLOCKED the pick feed.
        ("Kenneth Walker IIIQKCRB", "Kenneth Walker III", "KC", "RB"),
        ("Luther Burden IIIQCHIWR", "Luther Burden III", "CHI", "WR"),
        ("Patrick Mahomes IIOKCQB", "Patrick Mahomes II", "KC", "QB"),
        ("Marvin Harrison Jr.OARIWR", "Marvin Harrison Jr.", "ARI", "WR"),
        # MULTI-LETTER flags (item 4.0 Fix A). "Josh JacobsDTD..." is verbatim
        # from LIVE pick 72 of the 2026-08-31 draft: the flag list had bare "D"
        # ahead of (a then-missing) "DTD", the strip loop stops at the first
        # endswith hit, so the parse kept the whole suffix and the commit
        # gate's two-name self-check dammed the feed for ~2 minutes.
        ("Josh JacobsDTDGBRB", "Josh Jacobs", "GB", "RB"),
        ("Kenneth Walker IIIDTDSEARB", "Kenneth Walker III", "SEA", "RB"),
        ("Tyreek HillPUPMIAWR", "Tyreek Hill", "MIA", "WR"),
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


def test_generational_suffix_plus_status_flag_parses():
    """MEASURED, live practice draft 2026-08-27 — the defect three adversarial
    audits missed because the fixtures covered each half separately.

    This file already pinned a generational suffix ("Kenneth Walker IIISEARB")
    and a status flag ("Cam SkatteboQNYGRB") — but never the two TOGETHER,
    which is the combination that actually occurs whenever an injured player
    with a suffix is drafted. The old guard required the character before the
    flag to be lowercase or a period; a suffix is uppercase, so the flag
    survived into the name ("Kenneth Walker IIIQ"), disagreed with the clean
    anchor text, and the commit gate refused — DAMMING THE ENTIRE PICK FEED
    until a human entered the pick by hand. Two of 160 picks, in one draft.
    """
    assert parse_history_cell("Kenneth Walker IIIQKCRB")[0] == "Kenneth Walker III"
    assert parse_history_cell("Luther Burden IIIQCHIWR")[0] == "Luther Burden III"
    # and the flag must still not be invented where there is none
    assert parse_history_cell("Kenneth Walker IIIKCRB")[0] == "Kenneth Walker III"


def test_suffixed_injured_pick_commits_end_to_end():
    """The parse is a means; COMMITTING is the end. Both live-blocked picks
    must now clear the gate confidently against a board that spells the name
    with and without the suffix (the board's nflverse names do both)."""
    board = (
        BoardEntry("2001", "Walker Delta III", "RB", 21, 240.0, 47.0, "KC"),
        BoardEntry("2002", "Burden Echo", "WR", 54, 215.0, 19.0, "CHI"),
    )
    for payload, want in (
        ({"overall": 20, "player": "Walker Delta IIIQKCRB",
          "player_clean": "Walker Delta III"}, "2001"),
        # ESPN carries the suffix, the board does not — suffix-blind identity
        ({"overall": 63, "player": "Burden Echo IIIQCHIWR",
          "player_clean": "Burden Echo III"}, "2002"),
    ):
        pick = parse_payload_pick(payload)
        assert pick is not None
        # the cell-parsed name and the anchor name must AGREE; their
        # disagreement is what the gate refused on
        assert pick.cell_name == pick.name
        res = resolve_synced_pick(NameResolver(board), board, pick, taken=frozenset())
        assert res.confident, f"{payload['player']} still blocks: {res.reason}"
        assert res.entry.player_id == want


def test_dtd_suffixed_pick_commits_on_the_espn_id_rung():
    """Live pick 72, 2026-08-31 draft (item 4.0 Fix A), replayed end-to-end.

    The harvested row carried the player href, so resolution took the exact
    espn_id rung — and STILL refused, because the cell text parsed as
    'Josh JacobsDTD' while the anchor said 'Josh Jacobs', and the gate's
    cell-vs-anchor self-check saw two different names. With "DTD" stripped as
    a status flag the two names agree and the pick must commit confidently."""
    board = (BoardEntry("4047365", "Josh Jacobs", "RB", 93, 210.0, 38.0, "GB"),)
    pick = parse_payload_pick({
        "overall": 72,
        "player": "Josh JacobsDTDGBRB",
        "player_clean": "Josh Jacobs",
        "href": "https://www.espn.com/nfl/player/_/id/4047365/josh-jacobs",
    })
    assert pick is not None
    assert pick.espn_id == "4047365"
    # the self-check precondition: cell name and anchor name now agree
    assert pick.cell_name == pick.name == "Josh Jacobs"
    res = resolve_synced_pick(NameResolver(board), board, pick, taken=frozenset())
    assert res.confident, f"pick 72 still blocks: {res.reason}"
    assert res.entry.player_id == "4047365"


def test_a_refusal_names_the_field_that_disagreed():
    """Rule 6. MEASURED 2026-08-27: the live refusal read "'Kenneth Walker III'
    has no exact board match (closest: Kenneth Walker III)" — two IDENTICAL
    strings, because it printed the anchor name while the failing check was
    against the cell name. It cost this session a wrong diagnosis (a team
    mismatch that did not exist), and it would cost the operator far more at
    19:30 with 90 seconds on the clock."""
    from ziggurat.draft.sync import ParsedPick

    board = (BoardEntry("2001", "Walker Delta III", "RB", 21, 240.0, 47.0, "KC"),)

    def reason(**kw):
        base = dict(overall=20, name="Walker Delta III", cell_name="",
                    position="RB", team="KC", espn_id=None, fantasy_team=None)
        base.update(kw)
        return resolve_synced_pick(
            NameResolver(board), board, ParsedPick(**base), taken=frozenset()
        ).reason

    # the two names printed must never be the only thing an operator sees
    cell = reason(cell_name="Walker Delta IIIQ")
    assert "cell text parses as 'Walker Delta IIIQ'" in cell
    assert "IIIQ" in cell, "the refusal must show the string that actually failed"
    assert "position WR on ESPN vs RB on the board" in reason(position="WR")
    assert "team SEA on ESPN vs KC on the board" in reason(team="SEA")


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


def test_diminutive_first_name_commits():
    # Rehearsal 2 (2026-07-24): ESPN "Kenny X" vs the board's "Kenneth X"
    # is the same person, not drift — the gate must commit.
    board = (BoardEntry("400", "Kenneth Stonefield", "RB", 60, 80.0, 20.0, "PHI"),)
    resolver = NameResolver(board)
    pick = parse_payload_pick({"overall": 1, "player": "Kenny StonefieldPHIRB"})
    res = resolve_synced_pick(resolver, board, pick)
    assert res.confident and res.entry.player_id == "400"


def test_prefix_diminutives_commit_both_directions():
    board = (BoardEntry("401", "Christopher Wexley", "TE", 70, 70.0, 10.0, "DEN"),)
    resolver = NameResolver(board)
    pick = parse_payload_pick({"overall": 1, "player": "Chris WexleyDENTE"})
    assert resolve_synced_pick(resolver, board, pick).confident


def test_pure_nickname_still_blocks():
    # "Hollywood"-class pure nicknames are NOT diminutives — refuse, confirm.
    board = (BoardEntry("402", "Marquise Vale", "WR", 40, 90.0, 30.0, "KC"),)
    resolver = NameResolver(board)
    pick = parse_payload_pick({"overall": 1, "player": "Hollywood ValeKCWR"})
    assert not resolve_synced_pick(resolver, board, pick).confident


def test_diminutive_leeway_never_bridges_different_surnames():
    board = (BoardEntry("403", "Kenneth Marsh", "RB", 45, 85.0, 25.0, "PHI"),)
    resolver = NameResolver(board)
    pick = parse_payload_pick({"overall": 1, "player": "Kenny MarlowPHIRB"})
    assert not resolve_synced_pick(resolver, board, pick).confident


def test_diminutive_twins_still_block():
    # Two entries both passing the gate (Kenny/Kenneth twins) must refuse.
    board = (
        BoardEntry("404", "Kenneth Dunmore", "RB", 45, 85.0, 25.0, "PHI"),
        BoardEntry("405", "Kenny Dunmore", "RB", 200, 20.0, 1.0, "PHI"),
    )
    resolver = NameResolver(board)
    pick = parse_payload_pick({"overall": 1, "player": "Kenny DunmorePHIRB"})
    res = resolve_synced_pick(resolver, board, pick)
    assert not res.confident and "different players" in res.reason
