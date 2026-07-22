"""Unit tests for the item-2.4 TUI name resolver (``ziggurat/draft/resolver.py``).

All offline, deterministic, fast — a synthetic ~60-entry board with SYNTHETIC
player names (Rule 5: never real colleague names). The synthetic names are
engineered to reproduce the REAL structural cases the resolver must survive:
hyphen/apostrophe names, a Jr suffix (suffix-inclusive initials), surname
collisions including an elite/deep collision pair (the recon "james"/"saquan"/"dk"
counterexamples), DST entries keyed by team abbr, and an initialism alias.

The three verifier-mandated MUSTs are each pinned by a test: the empty-query guard,
elite-safety in the confirm list, and err-toward-confirm (auto only on
exact/curated/dominant hits).
"""

import dataclasses

import pytest

from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.resolver import (
    FAMOUS_ALIASES,
    NICKNAME_TO_ABBR,
    NameResolver,
    Resolution,
    normalize_query,
)

# --------------------------------------------------------------- synthetic board


def _p(pid, name, pos, rank, *, team=None):
    """A synthetic board entry (vor/points are irrelevant to the resolver)."""
    vor = max(0.0, 300.0 - rank)
    return BoardEntry(pid, name, pos, rank, vor, vor, team)


# Surnames used only for filler padding — deliberately disjoint from every token a
# test query exercises, so filler can never contaminate a fixture assertion.
_FILLER_SURNAMES = (
    "Ackerly Boone Castellano Dunmore Everhart Fenwick Garrison Holloway "
    "Iverson Kessler Lindgren Marsh Nunnally Ortega Pembroke Quilliam Rutherford "
    "Sundberg Thackeray Underhill Vandegrift Whitlock Yarborough Zabel Ashcroft "
    "Bramwell Cudworth Delacroix Ellsworth Fairbanks Grimsby Hawthorne Inglewood "
    "Jorgenson Kirkpatrick Lund Mockingbird Norridge Oglesby"
).split()
_FILLER_FIRSTS = (
    "Zeno Yuri Xander Wade Vernon Ulric Tobin Sven Rusty Quill Prosper Otto "
    "Nestor Milo2 Lonzo Kip Jules Ivo Huxley Godfrey"
).split()


def _build_board():
    board = [
        # --- exact / core-join / fi+last / prefixes / subsequence / typo anchor
        _p("p-mateo", "Patrick Mateo", "QB", 6, team="BUF"),
        # --- Jr suffix: initials both suffix-stripped (mh) and suffix-inclusive (mhj)
        _p("p-hill-jr", "Marcus Hill Jr", "WR", 18, team="MIA"),
        # --- shared-initials (mh) foil -> "mh" must be a CONFIRM, "mhj" an AUTO
        _p("p-hart", "Milo Hart", "WR", 60, team="DEN"),
        # --- surname collision on "hill" (a plain confirm, no elite gap)
        _p("p-devon-hill", "Devon Hill", "RB", 50, team="CHI"),
        # --- hyphenated surname
        _p("p-smithjones", "Amari Smith-Jones", "WR", 30, team="SEA"),
        # --- apostrophe first name
        _p("p-occ", "Ja'Bril Occ", "WR", 40, team="NYJ"),
        # --- period initials in the name
        _p("p-barnes", "A.J. Barnes", "WR", 25, team="DAL"),
        # --- ELITE / DEEP surname collision on "rivera" (MUST 2 elite-safety):
        #     one elite first-name "Rivera", three deep last-name "Rivera".
        _p("p-rivera-stone", "Rivera Stone", "RB", 8, team="KC"),
        _p("p-aaron-rivera", "Aaron Rivera", "WR", 210, team="TEN"),
        _p("p-bruno-rivera", "Bruno Rivera", "WR", 245, team="CAR"),
        _p("p-cato-rivera", "Cato Rivera", "TE", 270, team="NO"),
        # --- alias-expansion end-to-end target (Rule-5-safe synthetic name)
        _p("p-alpha-runner", "Alpha Runner", "RB", 12, team="GB"),
        # --- DST entries keyed by TEAM_ALIASES-normalized abbr (Rams=LA, etc.)
        _p("dst-sf", "SF D/ST", "DST", 175, team="SF"),
        _p("dst-gb", "GB D/ST", "DST", 180, team="GB"),
        _p("dst-was", "WAS D/ST", "DST", 185, team="WAS"),
        _p("dst-la", "LA D/ST", "DST", 190, team="LA"),
        # --- F1: an elite whose surname TRUNCATION scores below the old 780 floor.
        #     "jakob" -> last-prefix of "Jakobs" (760) for the elite, while three
        #     deep first-name "Jakob" scrubs score 780. Reproduces the refuter
        #     "jacob" -> Josh Jacobs #21 (760) case; the 300 floor must rescue it.
        _p("p-jakobs", "Zog Jakobs", "RB", 5, team="KC"),
        _p("p-jakob-1", "Jakob Cowling", "WR", 1343),
        _p("p-jakob-2", "Jakob Sailors", "RB", 1355),
        _p("p-jakob-3", "Jakob Kibode", "TE", 2468),
        # --- F1: an elite whose surname TYPO lands in the difflib tier (<400).
        #     "rabbins" -> ~343 for elite "Robbins", 820 for three deep "Rabbins".
        #     Reproduces the refuter "ribinson" -> Bijan #2 (350) case.
        _p("p-robbins", "Zico Robbins", "WR", 3, team="SF"),
        _p("p-rabbins-1", "Ace Rabbins", "WR", 1400),
        _p("p-rabbins-2", "Bode Rabbins", "RB", 1500),
        _p("p-rabbins-3", "Cyrus Rabbins", "TE", 1600),
        # --- F2: an alias target ("gamma striker") plus a top-100 initials rival
        #     ("qz" = Quinn Zable #40) and a just-outside-top-100 non-rival ("qy" =
        #     Quade Yates #101). Reproduces "aj" -> A.J. Brown burying Ashton Jeanty.
        _p("p-gamma", "Gamma Striker", "WR", 55, team="TB"),
        _p("p-quinn-z", "Quinn Zable", "RB", 40, team="DET"),
        _p("p-quade-y", "Quade Yates", "TE", 101, team="NYG"),
        # --- F3/NEW-1: real players colliding with a DST abbr / city-nickname key.
        #     "gb" = Garrett Boyd's initials; "washington" = Parker Washington's
        #     surname. Reproduce "kc"->KC Concepcion and "washington"->P. Washington.
        _p("p-garrett-boyd", "Garrett Boyd", "RB", 202, team="DET"),
        _p("p-washington", "Parker Washington", "WR", 112, team="JAX"),
        # --- F5: a ranked first-name, a ranked surname, and an UNRANKED deep surname
        #     all sharing "braxton"; the deep #10607 must not outrank the ranked #15.
        _p("p-braxton-first", "Braxton Fielder", "RB", 15, team="CLE"),
        _p("p-braxton-last", "Jordy Braxton", "WR", 277, team="ATL"),
        _p("p-braxton-deep", "Zeddicus Braxton", "K", 10_607),
        # --- a deep scrub with NO name: must be filtered out of the index entirely
        BoardEntry("scrub-1", None, "K", 10_042, 0.0, 0.0, None),
    ]
    rank = 300
    for i, (fn, sn) in enumerate(zip(_FILLER_FIRSTS, _FILLER_SURNAMES, strict=False)):
        pos = ("QB", "RB", "WR", "TE")[i % 4]
        board.append(_p(f"fill-{i}", f"{fn} {sn}", pos, rank + i))
    return tuple(board)


@pytest.fixture()
def resolver():
    return NameResolver(_build_board())


def _ids(res: Resolution) -> set[str]:
    return {c.player_id for c in res.candidates}


def _top1(res: Resolution) -> str:
    return res.candidates[0].player_id


# --------------------------------------------------------------- MUST 1: empty guard


@pytest.mark.parametrize("query", ["", "   ", "\t\n", "...", "  -- / .. ", "’‘"])
def test_empty_or_punctuation_only_query_never_matches(resolver, query):
    res = resolver.resolve(query)
    assert res.kind == "empty"
    assert res.candidates == ()


def test_normalize_query_folds_the_hard_cases():
    assert normalize_query("A.J.") == "aj"
    assert normalize_query("Ja'Marr") == "jamarr"
    assert normalize_query("Smith-Jones") == "smith jones"
    assert normalize_query("SF D/ST") == "sf dst"
    assert normalize_query("niners DEFENSE") == "niners dst"
    assert normalize_query("San Francisco D/ST") == "san francisco dst"
    assert normalize_query("   ") == ""
    assert normalize_query("Renée Décosté").isascii()  # accents stripped


# --------------------------------------------------------------- exact / auto tier


def test_exact_full_name_auto_resolves(resolver):
    res = resolver.resolve("Patrick Mateo")
    assert res.kind == "auto"
    assert _top1(res) == "p-mateo"


def test_core_join_typed_without_space_auto_resolves(resolver):
    # "marcushill" is the Jr-suffix-STRIPPED core join -> dominant top tier -> auto.
    res = resolver.resolve("marcushill")
    assert res.kind == "auto"
    assert _top1(res) == "p-hill-jr"


def test_hyphen_and_apostrophe_names_resolve(resolver):
    assert _top1(resolver.resolve("Amari Smith-Jones")) == "p-smithjones"
    assert _top1(resolver.resolve("Ja'Bril Occ")) == "p-occ"
    assert _top1(resolver.resolve("A.J. Barnes")) == "p-barnes"
    # surname half of a hyphen name still finds it
    assert "p-smithjones" in _ids(resolver.resolve("smith jones"))


# --------------------------------------------------- initials: stripped AND full


def test_suffix_inclusive_initials_resolve_the_jr(resolver):
    # "mhj" = Marcus Hill Jr (suffix-inclusive) — a dominant, unique top-tier hit.
    res = resolver.resolve("mhj")
    assert res.kind == "auto"
    assert _top1(res) == "p-hill-jr"


def test_shared_initials_confirm_not_auto(resolver):
    # "mh" is BOTH Marcus Hill and Milo Hart -> a tie -> err toward confirm (MUST 3).
    res = resolver.resolve("mh")
    assert res.kind == "confirm"
    assert {"p-hill-jr", "p-hart"} <= _ids(res)


# --------------------------------------------------- lower structural tiers


def test_first_initial_plus_last_tier(resolver):
    assert _top1(resolver.resolve("pmateo")) == "p-mateo"
    assert _top1(resolver.resolve("p mateo")) == "p-mateo"


def test_last_name_prefix_and_first_name_prefix(resolver):
    assert _top1(resolver.resolve("mate")) == "p-mateo"   # last-name prefix
    assert _top1(resolver.resolve("patr")) == "p-mateo"   # full-name/first prefix


def test_subsequence_tier(resolver):
    # "ptmt" is a subsequence of "patrickmateo" but hits no higher tier.
    assert _top1(resolver.resolve("ptmt")) == "p-mateo"


def test_difflib_typo_tier(resolver):
    # "mateos" is a one-edit typo of the surname "mateo" -> difflib last-resort tier.
    assert "p-mateo" in _ids(resolver.resolve("mateos"))


# --------------------------------------------------------------- MUST 2: elite-safety


def test_elite_near_match_is_never_dropped_from_confirm(resolver):
    # Three deep last-name "Rivera" score higher (820) than the elite first-name
    # "Rivera" (780); without elite-safety the elite falls out of the top-3. The
    # confirm list MUST still carry it (recon "james"/"saquan"/"dk" fix).
    res = resolver.resolve("rivera")
    assert res.kind == "confirm"
    assert "p-rivera-stone" in _ids(res)          # the elite survived
    assert len(res.candidates) <= 3


def test_plain_surname_collision_confirms_both(resolver):
    res = resolver.resolve("hill")
    assert res.kind == "confirm"
    assert {"p-hill-jr", "p-devon-hill"} <= _ids(res)


# --------------------------------------------------------------- MUST 3: err toward confirm


def test_solid_unique_last_name_still_confirms(resolver):
    # A unique surname is a SOLID hit but not top-tier -> confirm, not auto: a wrong
    # entry corrupts the board, so one extra keystroke is cheap insurance.
    res = resolver.resolve("mateo")
    assert res.kind == "confirm"
    assert _top1(res) == "p-mateo"


def test_no_structural_or_typo_match_returns_none(resolver):
    res = resolver.resolve("zzzzzqqq")
    assert res.kind == "none"
    assert res.candidates == ()


# --------------------------------------------------------------- DST entry


@pytest.mark.parametrize(
    "query,pid",
    [
        ("niners", "dst-sf"),
        ("49ers", "dst-sf"),
        ("san francisco", "dst-sf"),
        ("sf", "dst-sf"),
        ("packers", "dst-gb"),
        ("green bay", "dst-gb"),
        ("commanders", "dst-was"),
        ("skins", "dst-was"),
        ("rams", "dst-la"),          # Rams DST team abbr is "LA", not "LAR"
    ],
)
def test_team_nickname_resolves_the_defense(resolver, query, pid):
    res = resolver.resolve(query)
    assert res.kind == "auto"
    assert _top1(res) == pid


def test_defense_marker_and_exact_dst_resolve(resolver):
    assert _top1(resolver.resolve("niners defense")) == "dst-sf"
    assert _top1(resolver.resolve("SF D/ST")) == "dst-sf"
    # a bare "dst"/"def" with no team is too ambiguous to match any single defense
    assert resolver.resolve("dst").kind in ("none", "confirm")
    if resolver.resolve("dst").kind == "confirm":
        # ... and if it does surface, it must not silently auto-commit one defense
        assert True


def test_defense_is_not_matched_by_person_tiers(resolver):
    # "dst" is a DST entry's only trailing token; it must NOT match every defense as
    # a surname (that would make "def" resolve to an arbitrary team).
    res = resolver.resolve("dst")
    assert res.kind != "auto"


# --------------------------------------------------------------- taken filtering


def test_taken_players_never_match(resolver):
    taken = {"p-mateo"}
    res = resolver.resolve("Patrick Mateo", taken=taken)
    assert "p-mateo" not in _ids(res)
    assert res.kind in ("none", "confirm")


def test_taken_defense_drops_out(resolver):
    res = resolver.resolve("niners", taken={"dst-sf"})
    assert "dst-sf" not in _ids(res)
    assert res.kind in ("none", "confirm")


def test_taken_elite_leaves_the_deep_collisions(resolver):
    res = resolver.resolve("rivera", taken={"p-rivera-stone"})
    assert "p-rivera-stone" not in _ids(res)
    assert {"p-aaron-rivera", "p-bruno-rivera", "p-cato-rivera"} & _ids(res)


# --------------------------------------------------------------- alias maps


def test_curated_alias_map_is_populated_and_sane():
    # Data test (Rule 5): verify the curated famous-alias map without putting real
    # names on the board.
    assert "mccaffrey" in FAMOUS_ALIASES["cmc"]
    assert "mccaffrey" in FAMOUS_ALIASES["cmac"]
    assert FAMOUS_ALIASES["nuk"] == FAMOUS_ALIASES["dhop"]      # both DeAndre Hopkins
    assert "harrison" in FAMOUS_ALIASES["mhj"]


def test_team_nickname_map_is_populated_and_normalized():
    assert NICKNAME_TO_ABBR["niners"] == "SF"
    assert NICKNAME_TO_ABBR["49ers"] == "SF"
    assert NICKNAME_TO_ABBR["packers"] == "GB"
    assert NICKNAME_TO_ABBR["commanders"] == "WAS"
    assert NICKNAME_TO_ABBR["rams"] == "LA"          # board abbr, not LAR
    assert NICKNAME_TO_ABBR["lar"] == "LA"           # ... but the variant abbr folds
    assert NICKNAME_TO_ABBR["sf"] == "SF"            # the abbr resolves to itself


def test_alias_expansion_resolves_end_to_end(resolver, monkeypatch):
    # Prove the alias-expansion MECHANISM end-to-end with a SYNTHETIC target (Rule
    # 5): a test-only alias whose expansion is the synthetic "Alpha Runner".
    from ziggurat.draft import resolver as resolver_mod

    monkeypatch.setitem(resolver_mod._ALIASES, "zzq", "alpha runner")
    res = resolver.resolve("zzq")
    assert res.kind == "auto"
    assert _top1(res) == "p-alpha-runner"


# ------------------------------------------------ audit fixes (refuter F1/F2/F3/F5)
#
# Regression pins for the four independently-refuted 2.4 resolver findings. Each
# reproduces the REAL-board failure class on the synthetic board (Rule 5).


def test_elite_surname_truncation_below_first_name_tier_is_rescued(resolver):
    # F1: "jakob" is a truncation of the elite's surname "Jakobs" -> last-prefix
    # tier (760) for Zog Jakobs #5, while three deep first-name "Jakob" scrubs score
    # 780. Under the old 780 elite floor the elite fell out of the confirm panel;
    # the lowered 300 floor must carry it back in (refuter F1: "jacob" -> Josh
    # Jacobs #21 at 760, absent).
    res = resolver.resolve("jakob")
    assert res.kind == "confirm"
    assert "p-jakobs" in _ids(res)
    assert len(res.candidates) <= 3


def test_elite_surname_difflib_typo_is_rescued(resolver):
    # F1: "rabbins" is a one-substitution typo of the elite surname "Robbins" and
    # lands in the difflib tier (~343) — well below the old 780 floor — while three
    # deep "Rabbins" score 820 exact. The 300 floor rescues the elite (refuter F1:
    # "ribinson" -> Bijan #2 at 350).
    res = resolver.resolve("rabbins")
    assert res.kind == "confirm"
    assert "p-robbins" in _ids(res)


def test_alias_yields_to_top100_initials_rival(resolver, monkeypatch):
    # F2: a curated alias must NOT silently auto-commit its target when another
    # top-100 player initials-matches the same key (refuter F2: "aj" -> A.J. Brown
    # buried Ashton Jeanty #17). "qz" is Quinn Zable's (#40) initials.
    from ziggurat.draft import resolver as resolver_mod

    monkeypatch.setitem(resolver_mod._ALIASES, "qz", "gamma striker")
    res = resolver.resolve("qz")
    assert res.kind == "confirm"
    assert _top1(res) == "p-gamma"          # alias target shown FIRST (Rule 6)
    assert "p-quinn-z" in _ids(res)         # the buried rival is surfaced


def test_alias_auto_fires_when_no_top100_rival(resolver, monkeypatch):
    # F2 other direction (the "cmc"/"mhj" contract): with NO top-100 rival on the
    # key, the alias still auto-fires.
    from ziggurat.draft import resolver as resolver_mod

    monkeypatch.setitem(resolver_mod._ALIASES, "qx", "gamma striker")
    res = resolver.resolve("qx")
    assert res.kind == "auto"
    assert _top1(res) == "p-gamma"


def test_alias_auto_survives_a_rival_ranked_outside_top100(resolver, monkeypatch):
    # F2 threshold: the rival guard is espn_overall_rank <= 100. Quade Yates #101
    # initials-matches "qy" but is just outside, so the alias keeps auto-firing.
    from ziggurat.draft import resolver as resolver_mod

    monkeypatch.setitem(resolver_mod._ALIASES, "qy", "gamma striker")
    res = resolver.resolve("qy")
    assert res.kind == "auto"
    assert _top1(res) == "p-gamma"


def test_dst_city_nickname_yields_to_colliding_player(resolver):
    # F3/NEW-1: "washington" is BOTH the WAS D/ST city key and Parker Washington's
    # surname. It must confirm (D/ST first, player reachable), not silently auto the
    # defense (refuter NEW-1: "washington" buried Parker Washington #112).
    res = resolver.resolve("washington")
    assert res.kind == "confirm"
    assert _top1(res) == "dst-was"          # the D/ST leads the panel
    assert "p-washington" in _ids(res)      # the colliding player is reachable


def test_dst_abbr_yields_to_same_initials_player(resolver):
    # F3: "gb" is the GB D/ST abbr AND Garrett Boyd's initials (refuter F3: "kc"
    # buried KC Concepcion #171). Confirm, not a silent DST auto.
    res = resolver.resolve("gb")
    assert res.kind == "confirm"
    assert _top1(res) == "dst-gb"
    assert "p-garrett-boyd" in _ids(res)


def test_dst_nickname_without_a_collision_still_auto_fires(resolver):
    # F3 must not over-trigger: a nickname/abbr with NO colliding real player still
    # auto-resolves the defense (guards against demoting every DST entry).
    assert resolver.resolve("niners").kind == "auto"
    assert resolver.resolve("skins").kind == "auto"


def test_unranked_scrub_never_outranks_a_ranked_player(resolver):
    # F5: "braxton" matches a ranked first-name (Braxton Fielder #15), a ranked
    # surname (Jordy Braxton #277) and an UNRANKED deep surname (Zeddicus Braxton
    # #10607, capped 820->700). The deep scrub must not sit above the ranked #15 in
    # panel order (refuter F5: Jesse James #10607 above James Cook #15).
    res = resolver.resolve("braxton")
    order = [c.player_id for c in res.candidates]
    assert "p-braxton-first" in order
    assert "p-braxton-deep" in order
    assert order.index("p-braxton-first") < order.index("p-braxton-deep")


def test_unranked_scrub_is_capped_not_filtered_and_stays_reachable(resolver):
    # F5: the deep entry is capped, NOT removed from the index — a late deep pick
    # stays reachable when nothing ranked matches the query.
    res = resolver.resolve("zeddicus")
    assert "p-braxton-deep" in _ids(res)


# --------------------------------------------------------------- hygiene


def test_none_named_scrubs_are_filtered_from_the_index(resolver):
    # No query can ever surface the name=None scrub, and no candidate anywhere is
    # unnamed.
    for q in ("mateo", "rivera", "niners", "hill", "mh", "smith jones"):
        for c in resolver.resolve(q).candidates:
            assert c.name is not None


def test_resolution_is_frozen():
    res = Resolution("none", ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.kind = "auto"  # type: ignore[misc]
