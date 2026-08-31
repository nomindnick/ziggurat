"""ESPN projection ingest + two-source ensemble tests (item 3.9).

Offline: ``espn_source.fetch_player_universe`` — the ONE ESPN network seam — is
patched everywhere it is exercised, and nothing else here touches the network.

TWO KINDS OF FIXTURE, deliberately.

  * ``CAPTURED_PLAYERS`` is REAL, from the live 2026 ``kona_player_info`` pull on
    2026-08-30: six players (a QB, a WR, a kicker, a D/ST, an injured TE and a
    two-way player), each carrying ESPN's own ``appliedTotal`` alongside the
    projected stat line that produced it. The stat maps are TRIMMED to the ids
    the mapper reads PLUS the ids it must NOT read — the per-game yardage forms
    (22/40/61), the pre-summed aggregates (62 two-point, 73 turnovers, 94 and
    105 defensive TDs, 74 the 50+ FG bucket) and the unpriced FTD (63). Dropping
    a decoy would make several tests here vacuous, so they are kept on purpose.
    They carry no roster, ownership or manager context (rule 5) — this is public
    projection data of the same kind as the committed
    ``fixtures/espn/player_universe.json``.

    Because ESPN computes ``appliedTotal`` under THIS LEAGUE's scoring settings
    (the pull goes through the league endpoint), these rows are a genuine GOLDEN
    MASTER: the module's re-derivation through ``core/scoring.py`` must
    reproduce ESPN's own number. Mutate any stat-id mapping and
    ``test_golden_master_*`` fails.

  * everything for the ensemble is SYNTHETIC and built by ``seed_world`` so a
    test can state exactly the situation it is about — a horizon mismatch, a
    coverage hole, a systematic per-position offset — instead of hoping the live
    data happens to contain one.

The frozen-fixture lesson from item 3.1b applies and is respected: a captured
fixture proves the MAPPING, never that upstream still serves this shape. The
guard for that is ``_check_residual`` running against the live payload at ingest,
and it is tested here by mutation rather than by hoping.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from ziggurat.core import projection_ensemble as pe
from ziggurat.core import scoring, valuation
from ziggurat.data.nfl import base, espn_source
from ziggurat.data.nfl import espn_projections as ep

_SCORING_FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "scoring_format.json"
_UNIVERSE_FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "player_universe.json"

SEASON = 2026
DAY = "2026-08-30"

#: Tolerance for the golden-master comparisons against ESPN's own applied total.
#:
#: NOT a fudge factor, and NOT to be loosened. The captured stat values are
#: rounded to 6 decimal places to keep the fixture readable, which is the ONLY
#: source of error here: measured across all 11 captured rows the largest gap is
#: 6.4e-6, so this leaves ~16x headroom over the fixture's own precision. It is
#: still two orders of magnitude BELOW the smallest mapping mistake that could
#: hide behind it — dropping a single two-point conversion moves a total by
#: ~0.001 — so every real stat-id error still fails these tests.
GOLDEN_TOLERANCE = 1e-4


def house_scoring_points_for(stat_id: int) -> float:
    """What the LEAGUE pays for one unit of an ESPN stat id, read from the
    committed ESPN scoring fixture — the authority ``core/scoring.py`` itself is
    checked against. Read, never hard-coded, so a league settings change moves
    these tests instead of silently invalidating them (rule 2)."""
    for entry in json.loads(_SCORING_FIXTURE.read_text()):
        if entry["id"] == stat_id:
            return float(entry["points"])
    raise AssertionError(f"stat id {stat_id} is not in the league scoring fixture")


# --------------------------------------------------------------------------
# REAL captured 2026 projections (see the module docstring).
# --------------------------------------------------------------------------
CAPTURED_PLAYERS = [
    {
        "id": 3918298, "fullName": "Josh Allen", "defaultPositionId": 1, "proTeamId": 2,
        "stats": [
            {"appliedTotal": 19.2591432, "statSourceId": 1, "statSplitTypeId": 1,
             "seasonId": 2026, "scoringPeriodId": 1,
             "stats": {"3": 223.705586, "4": 1.272171, "19": 0.102547, "20": 0.72224,
                       "22": 223.705586, "24": 32.233885, "25": 0.611181, "26": 0.029399,
                       "40": 32.233885, "62": 0.131947, "63": 0.002315, "72": 0.250771,
                       "73": 0.973011, "210": 1.0}},
            {"appliedTotal": 369.69012399, "statSourceId": 1, "statSplitTypeId": 0,
             "seasonId": 2026, "scoringPeriodId": 0,
             "stats": {"3": 3946.319073, "4": 26.266168, "19": 2.098061, "20": 11.566578,
                       "22": 232.136416, "24": 579.902447, "25": 12.451159, "26": 0.593269,
                       "40": 34.111909, "62": 2.691331, "63": 0.038816, "72": 4.203455,
                       "73": 15.770034, "210": 17.0}},
        ],
    },
    {
        "id": 4362628, "fullName": "Ja'Marr Chase", "defaultPositionId": 3, "proTeamId": 4,
        "stats": [
            {"appliedTotal": 19.96562575, "statSourceId": 1, "statSplitTypeId": 1,
             "seasonId": 2026, "scoringPeriodId": 1,
             "stats": {"24": 1.222829, "25": 0.008686, "26": 0.000589, "40": 1.222829,
                       "42": 88.839758, "43": 0.657307, "44": 0.030702, "53": 7.008745,
                       "61": 88.839758, "62": 0.031291, "63": 0.000437, "72": 0.055268,
                       "73": 0.055268, "210": 1.0}},
            {"appliedTotal": 337.50394359, "statSourceId": 1, "statSplitTypeId": 0,
             "seasonId": 2026, "scoringPeriodId": 0,
             "stats": {"24": 20.922643, "25": 0.143288, "26": 0.009728, "40": 1.230744,
                       "42": 1508.690979, "43": 10.799765, "44": 0.505289, "53": 119.697055,
                       "61": 88.746528, "62": 0.515017, "63": 0.007459, "72": 0.943793,
                       "73": 0.943793, "210": 17.0}},
        ],
    },
    {
        "id": 3953687, "fullName": "Brandon Aubrey", "defaultPositionId": 5, "proTeamId": 6,
        "stats": [
            {"appliedTotal": 10.02495364, "statSourceId": 1, "statSplitTypeId": 1,
             "seasonId": 2026, "scoringPeriodId": 1,
             "stats": {"74": 0.407631, "77": 0.535311, "80": 1.115827, "85": 0.241431,
                       "86": 2.739505, "198": 0.407631, "210": 1.0}},
            {"appliedTotal": 171.6620134, "statSourceId": 1, "statSplitTypeId": 0,
             "seasonId": 2026, "scoringPeriodId": 0,
             "stats": {"74": 7.025525, "77": 9.226103, "80": 19.231304, "85": 4.161067,
                       "86": 46.09713, "198": 7.025525, "210": 17.0}},
        ],
    },
    {
        "id": -16034, "fullName": "Texans D/ST", "defaultPositionId": 16, "proTeamId": 34,
        "stats": [
            {"appliedTotal": 5.29523637, "statSourceId": 1, "statSplitTypeId": 1,
             "seasonId": 2026, "scoringPeriodId": 1,
             "stats": {"89": 0.008347, "90": 0.037489, "91": 0.143957, "92": 0.147956,
                       "93": 0.007503, "94": 0.09608, "95": 0.766217, "96": 0.637443,
                       "97": 0.107787, "98": 0.015524, "99": 2.361388, "101": 0.010414,
                       "102": 0.01246, "103": 0.064977, "104": 0.031103, "105": 0.126457,
                       "120": 23.159601, "121": 0.144957, "122": 0.230431, "123": 0.181446,
                       "124": 0.093472, "125": 0.011946, "127": 346.12322, "129": 0.021993,
                       "130": 0.19994, "131": 0.207938, "132": 0.238928, "133": 0.180946,
                       "134": 0.09997, "135": 0.038988, "136": 0.011297, "210": 1.0}},
            {"appliedTotal": 130.32037212, "statSourceId": 1, "statSplitTypeId": 0,
             "seasonId": 2026, "scoringPeriodId": 0,
             "stats": {"89": 0.29193, "90": 1.319215, "91": 4.357665, "92": 3.608691,
                       "93": 0.125897, "94": 1.612263, "95": 12.133688, "96": 10.696553,
                       "97": 1.808718, "98": 0.2605, "99": 43.254683, "101": 0.161153,
                       "102": 0.243869, "103": 1.090343, "104": 0.52192, "105": 2.143182,
                       "120": 322.256408, "121": 2.468209, "122": 2.970361, "123": 1.591569,
                       "124": 0.365976, "125": 0.026384, "127": 5463.279398, "129": 0.783646,
                       "130": 6.132879, "131": 4.497445, "132": 3.526405, "133": 1.584327,
                       "134": 0.340716, "135": 0.11925, "136": 0.015332, "210": 17.0}},
        ],
    },
    {
        # Injured TE: ESPN projects 16 games, not 17 — the availability opinion.
        # His WEEK entry is REAL and EMPTY (`stats: {}`), the live shape that the
        # "never write an unpriceable row" rule exists for.
        "id": 3040151, "fullName": "George Kittle", "defaultPositionId": 4, "proTeamId": 25,
        "stats": [
            {"appliedTotal": 0.0, "statSourceId": 1, "statSplitTypeId": 1,
             "seasonId": 2026, "scoringPeriodId": 1, "stats": {}},
            {"appliedTotal": 188.87795968, "statSourceId": 1, "statSplitTypeId": 0,
             "seasonId": 2026, "scoringPeriodId": 0,
             "stats": {"42": 809.721692, "43": 5.624819, "44": 0.288106, "53": 74.278746,
                       "61": 50.607606, "62": 0.288106, "63": 0.002809, "72": 0.357467,
                       "73": 0.357467, "210": 16.0}},
        ],
    },
    {
        # Two-way player: an OFFENSIVE projection carrying return/defensive TDs.
        "id": 4685415, "fullName": "Travis Hunter", "defaultPositionId": 3, "proTeamId": 30,
        "stats": [
            {"appliedTotal": 6.49538793, "statSourceId": 1, "statSplitTypeId": 1,
             "seasonId": 2026, "scoringPeriodId": 1,
             "stats": {"24": 0.675488, "25": 0.004516, "26": 0.00018, "40": 0.675488,
                       "42": 28.433202, "43": 0.199908, "44": 0.008254, "53": 2.330933,
                       "61": 28.433202, "62": 0.008434, "63": 0.000197, "72": 0.024854,
                       "73": 0.024854, "93": 0.00043, "94": 0.009353, "95": 0.056296,
                       "96": 0.016387, "97": 0.003012, "98": 0.000317, "99": 0.002736,
                       "103": 0.008085, "104": 0.001268, "105": 0.009783, "210": 1.0}},
            {"appliedTotal": 113.10563791, "statSourceId": 1, "statSplitTypeId": 0,
             "seasonId": 2026, "scoringPeriodId": 0,
             "stats": {"24": 10.800565, "25": 0.073831, "26": 0.002947, "40": 0.635327,
                       "42": 496.600464, "43": 3.31772, "44": 0.136946, "53": 41.590143,
                       "61": 29.211792, "62": 0.139893, "63": 0.003496, "72": 0.441312,
                       "73": 0.441312, "93": 0.007388, "94": 0.160602, "95": 0.893953,
                       "96": 0.281383, "97": 0.051719, "98": 0.005444, "99": 0.036435,
                       "103": 0.138826, "104": 0.021777, "105": 0.167991, "210": 17.0}},
        ],
    },
]


def captured(name: str) -> dict:
    for p in CAPTURED_PLAYERS:
        if p["fullName"] == name:
            return json.loads(json.dumps(p))  # deep copy: tests mutate freely
    raise AssertionError(name)


def season_entry(raw: dict) -> dict:
    return next(s for s in raw["stats"] if s["statSplitTypeId"] == 0)


def season_row(raw: dict) -> dict:
    rows = ep.map_espn_projection(raw, season=SEASON)
    return next(r for r in rows if r["week"] == ep.SEASON_WEEK)


# ==========================================================================
# GOLDEN MASTER — the re-derivation must reproduce ESPN's own league total
# ==========================================================================


@pytest.mark.parametrize("name", ["Brandon Aubrey", "Texans D/ST"])
def test_golden_master_kicker_and_dst_reproduce_espn_exactly(name):
    """The two positions where ESPN's DEFAULT scoring differs from this league's
    most — the distance kicker with -1/miss, and the dual points-AND-yards D/ST
    brackets. Reproducing ESPN's league-applied total to 1e-6 from the stat line
    is the evidence that (a) this endpoint really is house-scored and (b) the
    band-count route prices the non-linear brackets correctly."""
    raw = captured(name)
    row = season_row(raw)
    assert ep.house_points(row) == pytest.approx(
        season_entry(raw)["appliedTotal"], abs=GOLDEN_TOLERANCE
    )


@pytest.mark.parametrize("name", ["Josh Allen", "Ja'Marr Chase", "George Kittle",
                                  "Travis Hunter"])
def test_golden_master_offense_matches_espn_minus_the_one_unpriced_stat(name):
    """Offense reproduces ESPN exactly ONCE the single stat core/scoring.py has
    no key for is added back: FTD (id 63, 'Fumble Recovered for TD'), which is in
    the league's scoring fixture and is read from it here rather than hard-coded.

    This is a two-way assertion. It pins every offensive stat-id mapping (change
    one and the equality breaks), and it pins the SIZE of the deliberate
    omission, so if FTD ever grows into something material the number moves."""
    raw = captured(name)
    entry = season_entry(raw)
    ftd = entry["stats"].get("63", 0.0) * house_scoring_points_for(63)
    assert ep.house_points(season_row(raw)) == pytest.approx(
        entry["appliedTotal"] - ftd, abs=GOLDEN_TOLERANCE
    )
    assert ftd > 0.0, "the fixture must actually exercise the unpriced stat"


def test_the_unpriced_stat_is_declared_not_merely_forgotten():
    """The omission must be declared data, not folklore in a docstring — and the
    declaration must NOT carry the points value (rule 2 keeps house scoring
    numbers in core/scoring.py alone). The league fixture is the authority for
    the value, and it must actually price the stat, or the whole 'deliberately
    unpriced' story is about nothing."""
    assert "63" in ep.UNPRICED_STAT_IDS
    abbr, why = ep.UNPRICED_STAT_IDS["63"]
    assert abbr == "FTD"
    assert house_scoring_points_for(63) > 0.0
    assert "core/scoring.py" in why
    # and scoring.py really has no key for it, which is what makes it unpriced
    from ziggurat.data.nfl.projections import _scoring_key_allowlist
    assert not any("fumble_recovered" in k for k in _scoring_key_allowlist())


# ==========================================================================
# THE DECOY IDS — each of these would silently corrupt a board
# ==========================================================================


def test_per_game_yardage_ids_are_not_mapped():
    """ESPN ships receiving yards twice: id 42 is the season TOTAL and id 61 is
    the same number divided by games. Mapping 61 would divide every skill
    projection by ~17 and nothing would raise."""
    raw = captured("Ja'Marr Chase")
    entry = season_entry(raw)
    assert entry["stats"]["42"] != entry["stats"]["61"], "fixture must distinguish them"
    row = season_row(raw)
    assert row["receiving_yards"] == pytest.approx(entry["stats"]["42"])

    allen = captured("Josh Allen")
    a_entry = season_entry(allen)
    assert a_entry["stats"]["3"] != a_entry["stats"]["22"]
    assert season_row(allen)["passing_yards"] == pytest.approx(a_entry["stats"]["3"])
    assert season_row(allen)["rushing_yards"] == pytest.approx(a_entry["stats"]["24"])


def test_two_point_conversions_are_not_double_counted():
    """Id 62 is the SUM of the passing/rushing/receiving two-point ids. Adding it
    on top would double every two-point conversion."""
    entry = season_entry(captured("Josh Allen"))
    components = entry["stats"]["19"] + entry["stats"]["26"]
    assert entry["stats"]["62"] == pytest.approx(components, abs=1e-5)
    row = season_row(captured("Josh Allen"))
    total_2pt = sum(row[c] or 0.0 for c in ("passing_2pt_conversions",
                                            "rushing_2pt_conversions",
                                            "receiving_2pt_conversions"))
    assert total_2pt == pytest.approx(components)


def test_dst_touchdown_aggregate_ids_are_not_double_counted():
    """94 (defensive TDs) and 105 (defensive + special-teams TDs) are ESPN's own
    subtotals of the five return-TD ids. Summing an aggregate with its own parts
    would inflate every defence."""
    entry = season_entry(captured("Texans D/ST"))
    s = entry["stats"]
    assert s["105"] == pytest.approx(s["93"] + s["94"] + s["101"] + s["102"], abs=1e-5)
    assert s["94"] == pytest.approx(s["103"] + s["104"], abs=1e-5)
    row = season_row(captured("Texans D/ST"))
    assert row["def_tds"] == pytest.approx(
        s["93"] + s["101"] + s["102"] + s["103"] + s["104"]
    )


def test_kicker_50_plus_aggregate_id_is_not_used():
    """Id 74 is 'made FGs from 50+' — the 50-59 (198) and 60+ (201) buckets added
    together. The house pays 5 for the first and 6 for the second, so the mapper
    must read the split buckets, not the aggregate."""
    s = season_entry(captured("Brandon Aubrey"))["stats"]
    assert s["74"] == pytest.approx(s["198"])  # no 60+ projection in this fixture
    row = season_row(captured("Brandon Aubrey"))
    assert row["fg_made_50_59"] == pytest.approx(s["198"])
    assert row["fg_made_60"] is None
    assert "74" not in ep._KICKER_STAT_IDS


def test_two_way_player_gets_credit_for_return_touchdowns():
    """A WR whose projection carries interception/fumble/blocked-kick return TDs.
    98 of 460 live offensive rows carry one; dropping them would leave a
    permanent negative residual against ESPN's own total."""
    s = season_entry(captured("Travis Hunter"))["stats"]
    row = season_row(captured("Travis Hunter"))
    assert row["special_teams_tds"] == pytest.approx(
        s["93"] + s["103"] + s["104"], abs=1e-5
    )
    assert row["special_teams_tds"] > 0.0


# ==========================================================================
# D/ST BANDS — the non-linear bracket, and the trap the table shape prevents
# ==========================================================================


def test_dst_band_columns_are_not_scoring_keys_and_the_scalars_are_absent():
    """Storing ESPN's season-TOTAL points allowed under `points_allowed` would
    let a naive score price ~322 through the '46+' bracket once. The columns do
    not exist, so that cannot happen."""
    row = season_row(captured("Texans D/ST"))
    assert "points_allowed" not in row
    assert "yards_allowed" not in row
    assert row["pa_games_0"] is not None and row["ya_games_100_199"] is not None
    # and the band columns are excluded from the scoring allow-list by name
    ep.validate_scoring_keys({c: 1.0 for c in ep._BAND_COLUMNS})


def test_naive_scoring_score_on_a_dst_row_is_wrong_and_house_points_is_not():
    """Pins the documented trap. `scoring.score` silently ignores the band
    columns, so it omits the whole bracket contribution — 24.4 points on this
    defence. The gap must be large enough that nobody can mistake the two calls
    for equivalent."""
    row = season_row(captured("Texans D/ST"))
    stats_only = {k: row[k] for k in ep._SCORING_COLUMNS if row.get(k) is not None}
    naive = scoring.score("D/ST", stats_only)
    full = ep.house_points(row)
    assert full == pytest.approx(season_entry(captured("Texans D/ST"))["appliedTotal"],
                                 abs=GOLDEN_TOLERANCE)
    assert abs(full - naive) > 10.0


def test_band_counts_price_as_an_expectation_over_the_bracket():
    """A defence projected to spend all 17 games in the shutout band must earn
    17 shutout bonuses — the expectation of a bracketed value. The per-game
    value is read from scoring.py, never written here (rule 2)."""
    per_game = scoring.score_dst({"points_allowed": 0.0})
    row = {"position": "D/ST", "pa_games_0": 17.0}
    assert ep.house_points(row) == pytest.approx(17.0 * per_game)


def test_a_band_straddling_a_house_bracket_raises_instead_of_mispricing():
    """The band/bracket containment check — this module's `require_columns`. If
    ESPN re-bands or the house re-brackets so an ESPN band no longer sits inside
    one house bracket, every D/ST projection would be silently wrong."""
    # A house rule set whose points-allowed brackets split ESPN's 7-13 band.
    drifted = scoring.ScoringRules(points_allowed_brackets=(
        (9.0, 3.0), (13.0, 2.0), (float("inf"), 0.0),
    ))
    with pytest.raises(ep.BandBracketMismatch):
        ep.house_points({"position": "D/ST", "pa_games_7_13": 4.0}, rules=drifted)
    # ... and the same rules are fine for a band they do NOT split.
    ep.house_points({"position": "D/ST", "pa_games_46_plus": 1.0}, rules=drifted)


def test_house_points_reads_a_sqlite_row(db):
    """`sqlite3.Row` has no `.get` and raises IndexError on an unknown column, so
    the accessor path must not be written as `row.get(...)`."""
    _ingest(db, [captured("Texans D/ST")], day=DAY)
    row = ep.get_espn_projections(db, as_of=DAY, season=SEASON,
                                  week=ep.SEASON_WEEK, position="D/ST")[0]
    assert isinstance(row, sqlite3.Row)
    assert ep.house_points(row) == pytest.approx(130.32037212, abs=GOLDEN_TOLERANCE)


# ==========================================================================
# MAPPER: identity, split selection, and what must never be stored
# ==========================================================================


def test_actual_stats_and_prior_season_projections_are_never_stored():
    """The same response carries ACTUALS (statSourceId 0) and ESPN's PRIOR-SEASON
    projection (seasonId 2025, statSourceId 1) served today. Admitting either
    would put a graded outcome, or a 2025 forecast, into the 2026 board."""
    raw = captured("Josh Allen")
    raw["stats"].extend([
        {"appliedTotal": 999.0, "statSourceId": 0, "statSplitTypeId": 0,
         "seasonId": 2026, "scoringPeriodId": 0, "stats": {"3": 9999.0}},
        {"appliedTotal": 888.0, "statSourceId": 1, "statSplitTypeId": 0,
         "seasonId": 2025, "scoringPeriodId": 0, "stats": {"3": 8888.0}},
    ])
    rows = ep.map_espn_projection(raw, season=SEASON)
    assert {r["week"] for r in rows} == {ep.SEASON_WEEK, 1}
    assert all(r["espn_applied_total"] not in (999.0, 888.0) for r in rows)
    assert all((r["passing_yards"] or 0) < 5000 for r in rows)


def test_season_split_is_week_zero_and_a_period_split_is_its_own_week():
    rows = ep.map_espn_projection(captured("Josh Allen"), season=SEASON)
    weeks = sorted(r["week"] for r in rows)
    assert weeks == [ep.SEASON_WEEK, 1]
    assert ep.SEASON_WEEK == 0


def test_dst_identity_keys_by_team_and_hides_the_synthetic_negative_id():
    row = season_row(captured("Texans D/ST"))
    assert row["espn_id"] is None, "the synthetic negative id must not leak"
    assert row["espn_key"] == "HOU"
    assert row["team"] == "HOU"


def test_skill_identity_keys_by_espn_id():
    row = season_row(captured("Ja'Marr Chase"))
    assert row["espn_id"] == "4362628" and row["espn_key"] == "4362628"


def test_a_non_league_position_maps_to_nothing():
    raw = captured("Josh Allen")
    raw["defaultPositionId"] = 9  # an IDP slot
    assert ep.map_espn_projection(raw, season=SEASON) == []


def test_an_entry_with_no_priceable_stat_is_never_written():
    """Kittle's real week-1 entry is `stats: {}`. Writing it would put an
    all-NULL row at a live primary key, which `select_as_of` would then resolve
    in preference to a good earlier pull — the emptied-values shadow."""
    empty = [s for s in captured("George Kittle")["stats"] if not s["stats"]]
    assert empty, "the fixture must contain the real empty week entry"
    rows = ep.map_espn_projection(captured("George Kittle"), season=SEASON)
    assert [r["week"] for r in rows] == [ep.SEASON_WEEK]


def test_validate_scoring_keys_rejects_a_misspelled_canonical_key():
    with pytest.raises(ValueError, match="non-scoring keys"):
        ep.validate_scoring_keys({"recieving_yards": 10.0})


def test_the_committed_universe_fixture_carries_no_projections():
    """The item-2.1 board fixture is a trimmed slice with no `stats` array. It is
    exercised here so the 'no priceable stat' path is proven on real committed
    data as well as on a synthetic empty map."""
    players = json.loads(_UNIVERSE_FIXTURE.read_text())
    assert players and all("stats" not in p for p in players)
    assert all(ep.map_espn_projection(p, season=SEASON) == [] for p in players)


# ==========================================================================
# INGEST: collapse floors, drift guard, honest counts
# ==========================================================================


def _ingest(conn, players, *, day, **kw):
    return ep.ingest_espn_projections(conn, players, retrieved_as_of=day,
                                      season=SEASON, **kw)


def test_ingest_round_trips_through_the_accessor(db):
    written = _ingest(db, CAPTURED_PLAYERS, day=DAY)
    season_rows = ep.get_espn_projections(db, as_of=DAY, season=SEASON,
                                          week=ep.SEASON_WEEK)
    assert len(season_rows) == len(CAPTURED_PLAYERS)
    assert written == len(season_rows) + len(
        ep.get_espn_projections(db, as_of=DAY, season=SEASON, week=1)
    )
    aubrey = next(r for r in season_rows if r["position"] == "K")
    assert ep.house_points(aubrey) == pytest.approx(171.6620134, abs=GOLDEN_TOLERANCE)


def test_an_empty_pull_is_refused_unconditionally(db):
    _ingest(db, CAPTURED_PLAYERS, day=DAY)
    before = db.execute("SELECT COUNT(*) FROM espn_projections").fetchone()[0]
    with pytest.raises(ep.ProjectionCollapse, match="EMPTY"):
        _ingest(db, [], day="2026-08-31")
    # even the operator override cannot write an empty snapshot
    with pytest.raises(ep.ProjectionCollapse):
        _ingest(db, [], day="2026-08-31", allow_shrink=True)
    assert db.execute("SELECT COUNT(*) FROM espn_projections").fetchone()[0] == before


def test_a_degraded_pull_is_refused_and_leaves_the_stored_snapshot_intact(db):
    _ingest(db, CAPTURED_PLAYERS, day=DAY)
    stored = db.execute("SELECT COUNT(*) FROM espn_projections").fetchone()[0]
    with pytest.raises(ep.ProjectionCollapse, match="degraded"):
        _ingest(db, [captured("Josh Allen")], day="2026-08-31")
    assert db.execute("SELECT COUNT(*) FROM espn_projections").fetchone()[0] == stored
    # the previous day is still readable and unchanged
    assert len(ep.get_espn_projections(db, as_of="2026-08-31", season=SEASON,
                                       week=ep.SEASON_WEEK)) == len(CAPTURED_PLAYERS)


def test_allow_shrink_lets_a_real_shrink_through(db):
    _ingest(db, CAPTURED_PLAYERS, day=DAY)
    assert _ingest(db, [captured("Josh Allen")], day="2026-08-31", allow_shrink=True) > 0


def test_an_emptied_repull_cannot_shadow_a_good_row(db):
    """The measured item-3.1b shape: not fewer rows, but the SAME keys with
    emptied values, which `select_as_of` then prefers. One player going empty
    does not trip the size floor, so the protection has to be that the row is
    never written at all — and the earlier good value must survive the re-pull."""
    _ingest(db, CAPTURED_PLAYERS, day=DAY)
    degraded = [captured(p["fullName"]) for p in CAPTURED_PLAYERS]
    for p in degraded:
        if p["fullName"] == "Ja'Marr Chase":
            for entry in p["stats"]:
                entry["stats"] = {}
    _ingest(db, degraded, day=DAY)  # same day: INSERT OR REPLACE territory
    chase = next(r for r in ep.get_espn_projections(db, as_of=DAY, season=SEASON,
                                                    week=ep.SEASON_WEEK)
                 if r["espn_key"] == "4362628")
    assert chase["receiving_yards"] == pytest.approx(1508.690979)
    assert ep.house_points(chase) > 300.0


def test_schema_drift_in_a_stat_id_is_caught_at_ingest(db):
    """The residual guard: ESPN computes `appliedTotal` under this league's
    settings, so the re-derivation must reproduce it. Renumbering a stat id
    upstream breaks that equality — here simulated by renaming receiving yards
    (42 -> 4242), which no mapping reads."""
    drifted = []
    for p in CAPTURED_PLAYERS:
        q = captured(p["fullName"])
        for entry in q["stats"]:
            if "42" in entry["stats"]:
                entry["stats"]["4242"] = entry["stats"].pop("42")
        drifted.append(q)
    with pytest.raises(ValueError, match="schema drift"):
        _ingest(db, drifted, day=DAY)
    assert db.execute("SELECT COUNT(*) FROM espn_projections").fetchone()[0] == 0


def test_the_residual_guard_tolerates_the_known_unpriced_stat(db):
    """Teeth in the other direction: the guard must NOT fire on the real payload,
    whose only residual is the deliberately unpriced FTD. A guard that cries wolf
    on every run is how the one that matters gets ignored."""
    assert _ingest(db, CAPTURED_PLAYERS, day=DAY) > 0


def test_duplicate_players_in_one_response_do_not_inflate_the_count(db):
    """`base.upsert` is handed the full primary key so its return value is
    DISTINCT keys written, not rows offered."""
    once = _ingest(db, [captured("Josh Allen")], day=DAY)
    twice = _ingest(db, [captured("Josh Allen"), captured("Josh Allen")],
                    day="2026-08-31", allow_shrink=True)
    assert once == twice


# ==========================================================================
# RULE 1: as-of gating, both views, and the back-stamp refusal
# ==========================================================================


def test_nothing_is_visible_before_the_pull(db):
    _ingest(db, CAPTURED_PLAYERS, day=DAY)
    assert ep.get_espn_projections(db, as_of="2026-08-29", season=SEASON) == []


def test_an_earlier_as_of_cannot_see_a_later_pull(db):
    _ingest(db, CAPTURED_PLAYERS, day="2026-08-28")
    _ingest(db, CAPTURED_PLAYERS, day="2026-08-30")
    early = ep.get_espn_projections(db, as_of="2026-08-29", season=SEASON,
                                    week=ep.SEASON_WEEK)
    assert early and all(r["retrieved_as_of"] == "2026-08-28" for r in early)
    late = ep.get_espn_projections(db, as_of="2026-08-30", season=SEASON,
                                   week=ep.SEASON_WEEK)
    assert late and all(r["retrieved_as_of"] == "2026-08-30" for r in late)


def test_latest_truth_relaxes_retrieval_but_still_gates_knowledge(db):
    _ingest(db, CAPTURED_PLAYERS, day="2026-08-28")
    read = base.latest_truth(ep.get_espn_projections)
    assert read(db, as_of="2026-08-27", season=SEASON) == []
    got = read(db, as_of="2026-08-29", season=SEASON, week=ep.SEASON_WEEK)
    assert got and all(r["knowable_as_of"] == "2026-08-28" for r in got)


def test_latest_truth_refuses_a_conflicting_view(db):
    _ingest(db, CAPTURED_PLAYERS, day=DAY)
    with pytest.raises(ValueError):
        base.latest_truth(ep.get_espn_projections)(
            db, as_of=DAY, season=SEASON, view="historical"
        )


def test_pull_routes_through_the_one_patched_network_seam(db):
    with patch.object(espn_source, "fetch_player_universe",
                      return_value=CAPTURED_PLAYERS) as seam:
        n = ep.pull_espn_projections(db, league_id=1, season=SEASON, espn_s2="x",
                                     swid="y", retrieved_as_of=DAY, today=DAY)
    seam.assert_called_once()
    assert n > 0
    assert len(ep.get_espn_projections(db, as_of=DAY, season=SEASON,
                                       week=ep.SEASON_WEEK)) == len(CAPTURED_PLAYERS)


def test_back_stamping_a_live_pull_is_refused(db):
    """Back-stamping overwrites a perishable snapshot AND makes today's numbers
    readable at a past as_of under the safe historical view."""
    with patch.object(espn_source, "fetch_player_universe",
                      return_value=CAPTURED_PLAYERS) as seam:
        with pytest.raises(ValueError, match="refusing to store LIVE"):
            ep.pull_espn_projections(db, league_id=1, season=SEASON, espn_s2="x",
                                     swid="y", retrieved_as_of="2026-08-01", today=DAY)
        seam.assert_not_called()  # refused BEFORE the network call


def test_back_stamping_is_allowed_only_with_the_explicit_override(db):
    with patch.object(espn_source, "fetch_player_universe",
                      return_value=CAPTURED_PLAYERS):
        assert ep.pull_espn_projections(
            db, league_id=1, season=SEASON, espn_s2="x", swid="y",
            retrieved_as_of="2026-08-01", today=DAY, allow_backfill=True) > 0


def test_a_future_stamp_is_allowed_because_it_under_claims_knowledge(db):
    with patch.object(espn_source, "fetch_player_universe",
                      return_value=CAPTURED_PLAYERS):
        ep.pull_espn_projections(db, league_id=1, season=SEASON, espn_s2="x",
                                 swid="y", retrieved_as_of="2026-09-15", today=DAY)
    assert ep.get_espn_projections(db, as_of=DAY, season=SEASON) == []
    assert ep.get_espn_projections(db, as_of="2026-09-15", season=SEASON)


# ==========================================================================
# ENSEMBLE — synthetic worlds, each stating exactly the situation it tests
# ==========================================================================

WEEKS = tuple(range(1, 18))


def seed_world(conn, players, *, day=DAY, weeks=WEEKS, knowable=None):
    """Seed a house-side world: `players` crosswalk rows + Sleeper projections.

    Each entry is ``dict(gsis, espn_id, name, position, team, per_week, played,
    opponent_of)`` where ``per_week`` is a canonical scoring-key dict applied to
    EVERY played week (the live feed is a flat season rate) and ``played`` is the
    weeks that carry an opponent. Position is the raw feed label ('DEF' for a
    defence, as Sleeper serves it) so the canonicalization is exercised too.
    """
    prows, crows = [], []
    for p in players:
        if p["position"] != "DEF" and p["gsis"] is not None:
            # No gsis means no crosswalk row — which is exactly the state the
            # NO_ESPN_JOIN_KEY tests are about, not a fixture shortcut.
            crows.append({"gsis_id": p["gsis"], "espn_id": p["espn_id"],
                          "name": p["name"], "position": p["position"],
                          "retrieved_as_of": day, "knowable_as_of": day})
        for week in weeks:
            played = week in p["played"]
            row = {
                "source": pe.HOUSE_SOURCE, "source_player_id": p["gsis"] or p["team"],
                "gsis_id": p["gsis"], "season": SEASON, "week": week,
                "season_type": "regular", "position": p["position"],
                "team": p["team"], "opponent": "OPP" if played else None,
                "retrieved_as_of": day, "knowable_as_of": knowable or day,
            }
            row.update({c: None for c in
                        ("passing_yards", "passing_tds", "interceptions",
                         "rushing_yards", "rushing_tds", "receptions",
                         "receiving_yards", "receiving_tds", "fumbles_lost",
                         "passing_2pt_conversions", "rushing_2pt_conversions",
                         "receiving_2pt_conversions", "fg_made_0_39",
                         "fg_made_40_49", "fg_made_50_59", "fg_made_60",
                         "pat_made", "fg_missed", "sacks", "def_interceptions",
                         "fumble_recoveries", "safeties", "blocked_kicks",
                         "def_tds", "points_allowed", "yards_allowed",
                         "projected_points")})
            if played:
                row.update(p["per_week"])
            prows.append(row)
    if crows:
        base.upsert(conn, "players", crows)
    base.upsert(conn, "projections", prows)


def espn_row(conn, *, espn_key, espn_id, name, position, team, points_per_game,
             games=17.0, day=DAY, knowable=None):
    """Store ONE ESPN season projection whose re-derived value is a chosen
    points-per-game. Uses receiving yards (a purely linear key) so the intended
    total is exact and the test's arithmetic is legible."""
    per_yard = scoring.HOUSE_RULES.points_per_receiving_yard
    row = {c: None for c in ep._ROW_COLUMNS}
    row.update({
        "source": ep.SOURCE, "espn_key": espn_key, "espn_id": espn_id,
        "gsis_id": None, "player": name, "position": position, "team": team,
        "season": SEASON, "week": ep.SEASON_WEEK, "projected_games": games,
        "espn_applied_total": None,
        "receiving_yards": points_per_game * games / per_yard,
        "retrieved_as_of": day, "knowable_as_of": knowable or day,
    })
    base.upsert(conn, "espn_projections", [row], key_cols=ep._PK_COLS)


def wr(gsis, espn_id, name, per_game, *, played=WEEKS, team="AAA"):
    per_yard = scoring.HOUSE_RULES.points_per_receiving_yard
    return {"gsis": gsis, "espn_id": espn_id, "name": name, "position": "WR",
            "team": team, "played": set(played),
            "per_week": {"receiving_yards": per_game / per_yard}}


def test_the_horizon_mismatch_is_normalized_away(db):
    """THE load-bearing ensemble test. The house sums weeks 1-17 and sees 16
    games; ESPN projects 17. At an IDENTICAL per-game rate the two sources agree
    exactly — and if the normalization is removed they differ by 1/16 of the
    season, which would swamp every real disagreement."""
    seed_world(db, [wr("G1", "E1", "Even Steven", 10.0, played=set(WEEKS) - {9})])
    espn_row(db, espn_key="E1", espn_id="E1", name="Even Steven", position="WR",
             team="AAA", points_per_game=10.0, games=17.0)
    row = _one(pe.build_ensemble(db, as_of=DAY, season=SEASON))
    assert row.house.games == 16.0 and row.espn.games == 17.0
    assert row.house.points == pytest.approx(160.0)
    assert row.espn.points == pytest.approx(170.0)      # each source's own horizon
    assert row.house_points == pytest.approx(160.0)     # both on the common one
    assert row.espn_points == pytest.approx(160.0)
    assert row.delta_points == pytest.approx(0.0, abs=1e-9)
    assert row.blended_points == pytest.approx(160.0)


def _one(report):
    priced = [r for r in report if r.agreement != pe.UNPRICEABLE]
    assert len(priced) == 1, [(r.player, r.agreement) for r in priced]
    return priced[0]


def test_a_missing_second_opinion_passes_the_house_number_through_unchanged(db):
    """An absence is not an observation of zero, and it is not the house number
    halved by its blend weight either — about half the live pool has no ESPN
    projection and would otherwise vanish from any consumer of `blended_points`."""
    seed_world(db, [wr("G1", "E1", "Unseen", 10.0, played=set(WEEKS) - {9})])
    row = _one(pe.build_ensemble(db, as_of=DAY, season=SEASON))
    assert row.agreement == pe.SINGLE_SOURCE_HOUSE
    assert row.blended_points == pytest.approx(row.house_points) == pytest.approx(160.0)
    assert row.espn_points is None and row.delta_points is None
    assert any("ONE SOURCE ONLY" in r for r in row.reasons)


def test_a_player_only_espn_sees_is_surfaced_not_dropped(db):
    seed_world(db, [wr("G1", "E1", "Known", 10.0, played=set(WEEKS) - {9})])
    espn_row(db, espn_key="E9", espn_id="E9", name="House Blind Spot",
             position="WR", team="BBB", points_per_game=12.0)
    rows = {r.player: r for r in pe.build_ensemble(db, as_of=DAY, season=SEASON)}
    only = rows["House Blind Spot"]
    assert only.agreement == pe.SINGLE_SOURCE_ESPN
    assert only.house_points is None
    assert only.blended_points == pytest.approx(only.espn_points)


def test_a_thin_house_line_is_refused_rather_than_compared(db):
    """The item-3.2 trap: the feed's bye row and its 'no forecast' row are
    byte-identical, so a player with one real week sums to a real-looking number.
    Pricing that against ESPN would manufacture a spectacular false disagreement
    on exactly the rows a disagreement report ranks first."""
    seed_world(db, [wr("G1", "E1", "One Real Week", 10.0, played={1})])
    espn_row(db, espn_key="E1", espn_id="E1", name="One Real Week", position="WR",
             team="AAA", points_per_game=10.0)
    row = next(r for r in pe.build_ensemble(db, as_of=DAY, season=SEASON)
               if r.player == "One Real Week" or r.espn_id == "E1")
    assert row.house_points is None
    assert row.agreement == pe.SINGLE_SOURCE_ESPN
    assert any("coverage floor" in r for r in row.reasons)


@pytest.mark.parametrize("weight,expected", [(1.0, 160.0), (0.0, 240.0), (0.25, 220.0)])
def test_blend_weight_moves_the_blend_and_only_the_blend(db, weight, expected):
    seed_world(db, [wr("G1", "E1", "Split", 10.0, played=set(WEEKS) - {9})])
    espn_row(db, espn_key="E1", espn_id="E1", name="Split", position="WR",
             team="AAA", points_per_game=240.0 / 16.0, games=16.0)
    row = _one(pe.build_ensemble(db, as_of=DAY, season=SEASON, house_weight=weight))
    assert row.blended_points == pytest.approx(expected)
    assert row.house_points == pytest.approx(160.0)   # inputs never move
    assert row.espn_points == pytest.approx(240.0)


def test_an_out_of_range_blend_weight_is_refused(db):
    seed_world(db, [wr("G1", "E1", "X", 10.0)])
    with pytest.raises(ValueError, match="house_weight"):
        pe.build_ensemble(db, as_of=DAY, season=SEASON, house_weight=1.5)


def _offset_world(db, *, n=12, offset=20.0, probe_delta=5.0):
    """A position whose two sources differ by a large SYSTEMATIC offset, plus one
    probe player whose raw gap is positive but far below that offset."""
    people = [wr(f"G{i}", f"E{i}", f"P{i}", 10.0, played=set(WEEKS) - {9})
              for i in range(n)]
    seed_world(db, people)
    for i in range(n):
        # spread the deltas so the standard deviation is non-zero
        delta = offset + (i - n / 2)
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}", position="WR",
                 team="AAA", points_per_game=(160.0 + delta) / 16.0, games=16.0)
    espn_row(db, espn_key="E0", espn_id="E0", name="P0", position="WR", team="AAA",
             points_per_game=(160.0 + probe_delta) / 16.0, games=16.0)
    return pe.build_ensemble(db, as_of=DAY, season=SEASON)


def test_lean_follows_the_standardized_gap_not_the_raw_sign(db):
    """Measured on live kickers: the house feed drops every 50+ field goal, so
    ESPN is higher for EVERY kicker. A kicker only slightly above the house is
    then a full standard deviation BELOW the position's norm. Reading the
    direction off the raw sign would print 'ESPN likes him more' on a row flagged
    unusual precisely because ESPN likes him less than it likes his peers."""
    report = _offset_world(db)
    probe = next(r for r in report if r.espn_id == "E0")
    assert probe.delta_points > 0                      # raw: ESPN is higher
    assert probe.delta_z < 0                           # standardized: unusually low
    assert probe.lean == pe.LEAN_HOUSE
    assert probe.agreement in (pe.MILD, pe.STRONG)
    assert any("READ THIS CAREFULLY" in r for r in probe.reasons)


def test_the_systematic_offset_is_disclosed_in_the_reasons(db):
    probe = next(r for r in _offset_world(db) if r.espn_id == "E0")
    assert any("property of the two FEEDS" in r for r in probe.reasons)


def test_z_is_withheld_when_the_position_has_too_few_players(db):
    """Below MIN_POSITION_N the mean and standard deviation are noise; reporting
    a z a novice would read as meaningful is worse than reporting none."""
    n = pe.MIN_POSITION_N - 1
    people = [wr(f"G{i}", f"E{i}", f"P{i}", 10.0, played=set(WEEKS) - {9})
              for i in range(n)]
    seed_world(db, people)
    for i in range(n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}", position="WR",
                 team="AAA", points_per_game=(160.0 + 5.0 * i) / 16.0, games=16.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    priced = [r for r in report if r.both_sources]
    assert len(priced) == n
    assert all(r.delta_z is None for r in priced)
    # NOT_COMPARABLE, never AGREED. These deltas span 0 to +30 points: calling a
    # gap that was never standardized "AGREED" is an affirmative claim about two
    # numbers that have not been compared (finding [12]).
    assert all(r.agreement == pe.NOT_COMPARABLE and r.lean == pe.LEAN_NONE
               for r in priced)
    assert pe.AGREED not in {r.agreement for r in priced}
    assert any("cannot be said" in x for x in priced[0].reasons)
    # ...and the label a novice actually sees says so too.
    assert "NOT_COMPARABLE" in pe.format_ensemble(report)


def test_espn_availability_is_flagged_but_never_folded_into_the_points(db):
    """ESPN's projected-games column is a second opinion about AVAILABILITY. It
    is surfaced as its own flag and its own reason; the blended points must be
    the same as an identical player ESPN expects to play a full season, or a
    'projection blend' would silently be a projection times an injury discount."""
    seed_world(db, [wr("G1", "E1", "Fragile", 10.0, played=set(WEEKS) - {9}),
                    wr("G2", "E2", "Durable", 10.0, played=set(WEEKS) - {9})])
    for key, games in (("E1", 12.0), ("E2", 17.0)):
        espn_row(db, espn_key=key, espn_id=key, name=key, position="WR", team="AAA",
                 points_per_game=10.0, games=games)
    rows = {r.espn_id: r for r in pe.build_ensemble(db, as_of=DAY, season=SEASON)}
    fragile, durable = rows["E1"], rows["E2"]
    assert pe.ESPN_EXPECTS_ABSENCE in fragile.flags
    assert pe.ESPN_EXPECTS_ABSENCE not in durable.flags
    assert fragile.espn_expected_games == 12.0
    assert fragile.blended_points == pytest.approx(durable.blended_points)
    assert any("AVAILABILITY" in r and "NOT folded" in r for r in fragile.reasons)


def test_a_defence_joins_by_team_and_a_skill_player_by_espn_id(db):
    """ESPN gives team defences synthetic negative ids, so both sides key a D/ST
    by team abbr — migration 004's contract, unchanged here."""
    seed_world(db, [{"gsis": None, "espn_id": None, "name": "AAA D/ST",
                     "position": "DEF", "team": "AAA", "played": set(WEEKS) - {9},
                     "per_week": {"sacks": 3.0}}])
    espn_row(db, espn_key="AAA", espn_id=None, name="AAA D/ST", position="D/ST",
             team="AAA", points_per_game=4.0, games=17.0)
    row = _one(pe.build_ensemble(db, as_of=DAY, season=SEASON))
    assert row.position == "DST" and row.both_sources
    assert row.house_points == pytest.approx(
        16.0 * 3.0 * scoring.HOUSE_RULES.points_per_sack
    )


def test_as_of_gates_both_sources_at_once(db):
    """Rule 1: `as_of` threads into BOTH reads. A date before either pull must
    see nothing at all — a half-gated join would silently compare a stale house
    number to a fresh ESPN one."""
    seed_world(db, [wr("G1", "E1", "Timed", 10.0, played=set(WEEKS) - {9})],
               day="2026-08-30")
    espn_row(db, espn_key="E1", espn_id="E1", name="Timed", position="WR",
             team="AAA", points_per_game=12.0, day="2026-08-30")
    early = pe.build_ensemble(db, as_of="2026-08-29", season=SEASON)
    assert [r for r in early if r.agreement != pe.UNPRICEABLE] == []
    later = _one(pe.build_ensemble(db, as_of="2026-08-30", season=SEASON))
    assert later.both_sources


def test_view_threads_into_both_sources(db):
    """A bulk-loaded backtest database reads EMPTY under the safe default view;
    binding `latest_truth` must relax BOTH sides, not one."""
    seed_world(db, [wr("G1", "E1", "Bulk", 10.0, played=set(WEEKS) - {9})],
               day="2026-08-30")
    espn_row(db, espn_key="E1", espn_id="E1", name="Bulk", position="WR",
             team="AAA", points_per_game=12.0, day="2026-08-30")
    # knowable on the 30th, retrieved on the 30th; read at the 29th under
    # latest_truth still sees nothing (knowledge gate holds)...
    read = base.latest_truth(pe.build_ensemble)
    assert [r for r in read(db, as_of="2026-08-29", season=SEASON)
            if r.agreement != pe.UNPRICEABLE] == []
    assert _one(read(db, as_of="2026-08-31", season=SEASON)).both_sources


def test_the_build_is_deterministic(db):
    """Rule 9: no wall clock, no global random, no dict-order dependence."""
    _offset_world(db)
    a = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    b = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    assert [(r.key, r.blended_points, r.delta_z, r.agreement) for r in a] == \
           [(r.key, r.blended_points, r.delta_z, r.agreement) for r in b]
    assert a.horizon_games == b.horizon_games == 16.0


def test_the_horizon_is_derived_from_the_data_not_assumed(db):
    """A world whose house lines cover 14 games must report a 14-game horizon —
    if 16 were hard-coded, every number would be 14% wrong in a shortened window."""
    seed_world(db, [wr("G1", "E1", "Short", 10.0, played=set(range(1, 15)))],
               weeks=tuple(range(1, 15)))
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON, weeks=range(1, 15))
    assert report.horizon_games == 14.0


def test_correlation_cohort_is_selected_on_the_incumbent_board(db):
    """`top` must slice by `house_rank`, never by the blended number being
    evaluated — selecting a cohort with the statistic under test is circular."""
    report = _offset_world(db, n=12)
    ranked = sorted((r for r in report if r.house_rank), key=lambda r: r.house_rank)
    assert [r.house_rank for r in ranked] == list(range(1, len(ranked) + 1))
    top3 = pe.source_correlation(report, top=3)
    assert top3.n == 3
    assert pe.source_correlation(report).n >= top3.n
    assert top3.cohort == "top 3 by house rank"


def test_disagreements_are_ranked_by_how_unusual_not_by_raw_points(db):
    """A raw-points ranking lists only the biggest-scale position. The report
    must rank within position by |z|, and a QB's larger scale must not push a
    WR's genuinely unusual gap off the list."""
    report = _offset_world(db, n=12, probe_delta=-60.0)
    wrs = pe.top_disagreements(report, top=None, per_position=3)["WR"]
    assert wrs[0].espn_id == "E0"
    assert abs(wrs[0].delta_z) >= abs(wrs[-1].delta_z)


def test_every_priced_row_explains_the_horizon_and_labels_its_hypotheses(db):
    """Rule 6: the operator is a novice. A number without its provenance, and a
    convention presented as a measurement, are both failures."""
    report = _offset_world(db)
    priced = [r for r in report if r.both_sources]
    assert priced
    for row in priced:
        assert any("comparison horizon" in r for r in row.reasons)
        assert any("HYPOTHESIS (untested)" in r for r in row.reasons)
        assert any("CONVENTION (unvalidated)" in r for r in row.reasons)
        assert any("Pulled 2026-08-30" in r for r in row.reasons)
    assert any("COMMON horizon" in n for n in report.notes)


def test_kicker_and_dst_carry_their_comparability_caveat(db):
    """Both sides are house-scored, but the K and D/ST comparisons carry known
    structural differences that a novice must be told about rather than infer."""
    seed_world(db, [{"gsis": None, "espn_id": None, "name": "AAA D/ST",
                     "position": "DEF", "team": "AAA", "played": set(WEEKS) - {9},
                     "per_week": {"sacks": 3.0}}])
    espn_row(db, espn_key="AAA", espn_id=None, name="AAA D/ST", position="D/ST",
             team="AAA", points_per_game=4.0)
    row = _one(pe.build_ensemble(db, as_of=DAY, season=SEASON))
    assert any(pe.POSITION_CAVEATS["DST"] == r for r in row.reasons)


def test_the_formatters_render_without_a_priced_row(db):
    """A pre-ingest database is a real state (the ESPN source may not have run
    yet); the display must say so rather than raise."""
    seed_world(db, [wr("G1", "E1", "Alone", 10.0, played=set(WEEKS) - {9})])
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    assert report.espn_full_season_games is None
    assert any("NO ESPN projections" in n for n in report.notes)
    assert "Alone" in pe.format_ensemble(report, top=5)
    assert pe.format_disagreements(report)


def test_pearson_is_undefined_rather_than_wrong_on_degenerate_input():
    assert pe.pearson([1.0], [2.0]) is None
    assert pe.pearson([1.0, 1.0], [2.0, 3.0]) is None
    assert pe.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert pe.pearson([1.0, 2.0, 3.0], [6.0, 4.0, 2.0]) == pytest.approx(-1.0)


def test_blend_never_scales_a_single_opinion_by_its_weight():
    assert pe.blend(100.0, None, house_weight=0.5) == 100.0
    assert pe.blend(None, 80.0, house_weight=0.5) == 80.0
    assert pe.blend(None, None, house_weight=0.5) is None
    assert pe.blend(100.0, 80.0, house_weight=0.25) == pytest.approx(85.0)


def test_mode_breaks_ties_deterministically_toward_the_larger_value():
    assert pe._mode([16.0, 17.0]) == 17.0
    assert pe._mode([17.0, 16.0]) == 17.0
    assert pe._mode([16.0, 16.0, 17.0]) == 16.0
    assert pe._mode([]) is None


def test_canonical_positions_agree_with_the_valuation_spine():
    """The two sides of the join must canonicalize identically or a D/ST would
    silently never match."""
    for raw in ("D/ST", "DST", "DEF"):
        assert valuation.canon_position(raw) == "DST"
    assert valuation.canon_position(
        ep.map_espn_projection(captured("Texans D/ST"), season=SEASON)[0]["position"]
    ) == "DST"


# ==========================================================================
# REGRESSION TESTS — one per confirmed audit finding.
#
# Every test below was written against a MUTATION: the shipped behaviour is
# asserted only where flipping it makes a test here fail. Ten mutations that the
# original file passed (both view arguments, the median-ratio direction, the band
# edges, the house_rank cohort, the |z| ranking, the row ordering, the relative
# floor) are each pinned by name.
# ==========================================================================


def bulk_wr(gsis, espn_id, name, per_game, *, played=WEEKS, team="AAA"):
    return wr(gsis, espn_id, name, per_game, played=played, team=team)


# ---- finding [11] / [14]: the two-view seam was decorative ----------------


def test_the_house_read_is_view_gated_and_latest_truth_reaches_it(db):
    """MUTATION PINNED: `view=view` -> `view="historical"` on the house read.

    The shipped test could not see that mutation because it stamped
    knowable == retrieved on both sides, where the two views are identical BY
    CONSTRUCTION. A bulk-loaded backtest row is the shape that separates them:
    knowable long ago, retrieved today. Under `historical` the retrieval gate
    hides it at a past `as_of`; under `latest_truth` it must come back.
    """
    seed_world(db, [wr("G1", "E1", "Bulk", 10.0, played=set(WEEKS) - {9})],
               day="2026-08-30", knowable="2025-09-01")
    espn_row(db, espn_key="E1", espn_id="E1", name="Bulk", position="WR",
             team="AAA", points_per_game=12.0, day="2026-08-30",
             knowable="2025-09-01")

    # historical: retrieved 2026-08-30 is in the future of this read, so NOTHING.
    plain = pe.build_ensemble(db, as_of="2025-10-01", season=SEASON)
    assert [r for r in plain if r.agreement != pe.UNPRICEABLE] == []

    # latest_truth: both sides must appear. If the HOUSE read is pinned to
    # "historical" the incumbent half vanishes and this row reads
    # SINGLE_SOURCE_ESPN with house_points None — a backtest silently grading
    # one feed while believing it graded two.
    row = _one(base.latest_truth(pe.build_ensemble)(db, as_of="2025-10-01",
                                                    season=SEASON))
    assert row.both_sources, (
        "latest_truth must relax BOTH sides; a house_points of None here means "
        "the view stopped threading into valuation.weekly_lines"
    )
    assert row.house_points is not None and row.espn_points is not None
    # One WR is below MIN_POSITION_N, so the gap is NOT_COMPARABLE rather than
    # AGREED — the point here is that BOTH numbers arrived, not how they band.
    assert row.agreement == pe.NOT_COMPARABLE


def test_the_espn_read_is_view_gated_and_historical_hides_a_later_retrieval(db):
    """MUTATION PINNED: `view=view` -> `view="latest_truth"` on the ESPN read,
    and `get_espn_projections`'s own safe default.

    The RETRIEVAL gate is the half the shipped tests never exercised. A row
    knowable on 08-01 but retrieved on 08-30, read at 08-15, is visible under
    `latest_truth` and invisible under `historical` — the only construction that
    tells the two views apart.
    """
    seed_world(db, [wr("G1", "E1", "Late", 10.0, played=set(WEEKS) - {9})],
               day="2026-08-01")
    espn_row(db, espn_key="E1", espn_id="E1", name="Late", position="WR",
             team="AAA", points_per_game=12.0,
             day="2026-08-30", knowable="2026-08-01")

    direct = ep.get_espn_projections(db, as_of="2026-08-15", season=SEASON)
    assert direct == [], "the historical view must gate retrieved_as_of too"
    assert len(base.latest_truth(ep.get_espn_projections)(
        db, as_of="2026-08-15", season=SEASON)) == 1

    row = _one(pe.build_ensemble(db, as_of="2026-08-15", season=SEASON))
    assert row.agreement == pe.SINGLE_SOURCE_HOUSE and row.espn_points is None, (
        "the ESPN read must inherit the caller's view; seeing this row means it "
        "was pinned to latest_truth"
    )
    assert _one(base.latest_truth(pe.build_ensemble)(
        db, as_of="2026-08-15", season=SEASON)).both_sources


# ---- finding [15]: median_ratio direction was untested --------------------


def test_median_ratio_is_espn_over_house_and_the_direction_is_pinned(db):
    """MUTATION PINNED: `b / a` -> `a / b` in source_correlation.

    The label says "median ESPN/house ratio ... (1.0 = the two sources agree)".
    Inverting it survived the whole suite and flips the headline conclusion a
    novice reads: on the live pool 0.9739 ("ESPN is ~3% LOWER") became 1.0268
    ("~3% HIGHER") under an unchanged label. This is the same class the item-3.5
    audit charged for (the Vegas home/away sign with no correctness test).
    """
    n = pe.MIN_POSITION_N + 2
    seed_world(db, [wr(f"G{i}", f"E{i}", f"P{i}", 10.0, played=set(WEEKS) - {9})
                    for i in range(n)])
    for i in range(n):
        # ESPN is deliberately HALF AGAIN the house on every player.
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA", points_per_game=15.0, games=16.0)
    corr = pe.source_correlation(pe.build_ensemble(db, as_of=DAY, season=SEASON))
    assert corr.median_ratio == pytest.approx(1.5), (
        "ESPN is 1.5x the house here, so the ESPN/house ratio must be 1.5, not "
        f"{corr.median_ratio!r} (0.667 means the division is inverted)"
    )
    assert corr.median_ratio > 1.0


# ---- finding [16]: the band edges were untested at the boundary -----------


def test_the_agreement_bands_are_pinned_at_their_exact_edges():
    """MUTATION PINNED: BAND_EDGES (1.0, 2.0) -> (2.0, 4.0) or (0.0, 0.0).

    BAND_EDGES is the module's primary output — AGREED / MILD / STRONG is what a
    novice reads — and both mutations passed the whole file. Under (0.0, 0.0)
    every compared row becomes STRONG and carries the "riskier pick" text: 454
    live rows instead of 26, a constant wearing a signal's name.
    """
    mild, strong = pe.BAND_EDGES
    assert (mild, strong) == (1.0, 2.0)
    assert pe._band(0.0) == pe.AGREED
    assert pe._band(mild - 1e-9) == pe.AGREED
    assert pe._band(mild) == pe.MILD                 # inclusive lower edge
    assert pe._band(-mild) == pe.MILD                # symmetric in sign
    assert pe._band(strong - 1e-9) == pe.MILD
    assert pe._band(strong) == pe.STRONG
    assert pe._band(-strong) == pe.STRONG
    assert pe._band(None) == pe.NOT_COMPARABLE       # finding [12]


def test_an_ordinary_gap_is_not_labelled_strong(db):
    """End-to-end guard on the same constant: a cohort with one modest outlier
    must NOT come out all-STRONG (which (0.0, 0.0) would produce)."""
    n = pe.MIN_POSITION_N + 4
    seed_world(db, [wr(f"G{i}", f"E{i}", f"P{i}", 10.0, played=set(WEEKS) - {9})
                    for i in range(n)])
    for i in range(n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA",
                 points_per_game=(160.0 + i) / 16.0, games=16.0)
    bands = {r.agreement for r in pe.build_ensemble(db, as_of=DAY, season=SEASON)
             if r.both_sources}
    assert pe.AGREED in bands
    assert bands != {pe.STRONG}


# ---- finding [17]: the anti-circularity claim was untested ----------------


def test_the_correlation_cohort_ranks_on_house_points_not_on_the_blend(db):
    """MUTATION PINNED: ranking `house_rank` by blended_points instead of
    house_points.

    The docstring's whole point is that `top` selects on the INCUMBENT board, so
    a "top 200" slice is not chosen by the quantity being evaluated. The shipped
    test could not see the mutation because its fixture gave all 12 players an
    IDENTICAL house rate, making house-order and blend-order indistinguishable.
    Here the two orders are deliberately OPPOSITE.
    """
    n = pe.MIN_POSITION_N + 2
    # House rate DESCENDS with i; ESPN rate ASCENDS steeply, so the 50/50 blend
    # ranks the players in very nearly the reverse order.
    seed_world(db, [wr(f"G{i}", f"E{i}", f"P{i}", 20.0 - i,
                       played=set(WEEKS) - {9}) for i in range(n)])
    for i in range(n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA", points_per_game=1.0 + 4.0 * i,
                 games=16.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    by_rank = sorted((r for r in report if r.house_rank), key=lambda r: r.house_rank)

    assert [r.espn_id for r in by_rank[:3]] == ["E0", "E1", "E2"], (
        "house_rank 1..3 must be the three highest HOUSE numbers"
    )
    blend_order = [r.espn_id for r in report if r.blended_points is not None]
    assert blend_order[:3] != [r.espn_id for r in by_rank[:3]], (
        "this fixture is only meaningful while the two orderings differ"
    )
    top3 = pe.source_correlation(report, top=3)
    assert top3.n == 3
    assert {r.house_points for r in by_rank[:3]} == {
        r.house_points for r in report if r.house_rank and r.house_rank <= 3
    }


# ---- finding [18]: |z| ranking vs |delta| ranking -------------------------


def test_disagreements_rank_by_the_standardized_gap_not_the_raw_points(db):
    """MUTATION PINNED: `-abs(r.delta_z)` -> `-abs(r.delta_points)`.

    The shipped test built a probe holding BOTH the largest |delta| and the
    largest |z|, so the two orderings agreed and the mutation survived. Here they
    are deliberately opposed, exactly as the live kicker board is: a systematic
    per-position offset means the biggest RAW gap can be the least UNUSUAL one.
    """
    n = pe.MIN_POSITION_N + 4
    seed_world(db, [wr(f"G{i}", f"E{i}", f"P{i}", 10.0, played=set(WEEKS) - {9})
                    for i in range(n)])
    # Every player carries a large positive offset (~+40), like a live kicker.
    for i in range(n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA",
                 points_per_game=(160.0 + 40.0 + i * 0.5) / 16.0, games=16.0)
    # CONFORMIST: the biggest RAW gap (+60) but close to the cohort's own norm.
    espn_row(db, espn_key="E0", espn_id="E0", name="P0", position="WR",
             team="AAA", points_per_game=(160.0 + 60.0) / 16.0, games=16.0)
    # ODD ONE OUT: a SMALLER raw gap (+5) that is far from the norm.
    espn_row(db, espn_key="E1", espn_id="E1", name="P1", position="WR",
             team="AAA", points_per_game=(160.0 + 5.0) / 16.0, games=16.0)

    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    rows = {r.espn_id: r for r in report if r.both_sources}
    conformist, odd = rows["E0"], rows["E1"]
    assert abs(conformist.delta_points) > abs(odd.delta_points)   # raw says E0
    assert abs(odd.delta_z) > abs(conformist.delta_z)             # unusual says E1

    ranked = pe.top_disagreements(report, top=None, per_position=n + 2)["WR"]
    order = [r.espn_id for r in ranked]
    assert order.index("E1") < order.index("E0"), (
        "the most UNUSUAL gap must outrank the largest raw gap; this ordering "
        "means the ranking key fell back to |delta_points|"
    )


# ---- finding [24]: row set and ordering were untested ---------------------


def test_rows_are_ordered_by_the_blend_because_the_formatter_slices_them(db):
    """MUTATION PINNED: the final sort key -> `str(r.key)`.

    `format_ensemble(report, top=N)` takes the FIRST N rows, so a broken sort
    silently shows N alphabetically-arbitrary players as "the top N".
    `test_the_build_is_deterministic` compares two runs of the same code and so
    cannot see it.
    """
    # Alphabetical order by key is deliberately the REVERSE of blend order.
    seed_world(db, [wr("G1", "E1", "Alpha", 5.0, played=set(WEEKS) - {9}),
                    wr("G2", "E2", "Bravo", 10.0, played=set(WEEKS) - {9}),
                    wr("G3", "E3", "Cosmo", 20.0, played=set(WEEKS) - {9})])
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    priced = [r for r in report if r.blended_points is not None]
    assert [r.player for r in priced] == ["Cosmo", "Bravo", "Alpha"]
    values = [r.blended_points for r in priced]
    assert values == sorted(values, reverse=True)
    assert "Cosmo" in pe.format_ensemble(report, top=1)
    assert "Alpha" not in pe.format_ensemble(report, top=1)


def test_an_unpriceable_identity_is_kept_and_counted_not_dropped(db):
    """MUTATION PINNED: filtering out records with both points None.

    The note promises "an absence is a fact worth seeing". Dropping those rows
    passed the whole file and simply stopped the note printing.
    """
    seed_world(db, [wr("G1", "E1", "Real", 10.0, played=set(WEEKS) - {9}),
                    wr("G2", "E2", "Ghost", 10.0, played=set())])
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    ghost = next(r for r in report if r.player == "Ghost")
    assert ghost.agreement == pe.UNPRICEABLE
    assert ghost.house_points is None and ghost.blended_points is None
    assert report.rows[-1] is ghost, "unpriceable rows must sort last"
    assert any("UNPRICEABLE" in n for n in report.notes)
    assert any("1 of 2 rows are UNPRICEABLE" in n for n in report.notes)


# ---- finding [2]: a bye that swallows the whole window --------------------


def test_a_bye_week_player_is_not_given_espns_season_rate_for_that_week(db):
    """THE finding with the widest blast radius. ESPN's stored row is a
    WHOLE-SEASON projection, so its per-game rate projects happily onto any
    window — including one the team spends entirely on bye. The house side
    correctly refused, and the ESPN rate then filled the hole: measured on the
    live board at weeks=[11], 93 rows for the six week-11 bye teams carried a
    positive blend and FOUR of the top thirteen players were on bye (Nacua at
    #4, Bijan at #5), each with `flags=()` and a reason reading "not a
    projection of zero".
    """
    players = [wr("G1", "E1", "Plays", 10.0, played=set(WEEKS), team="AAA"),
               wr("G2", "E2", "OnBye", 10.0, played=set(WEEKS) - {11}, team="BBB")]
    seed_world(db, players)
    for key in ("E1", "E2"):
        espn_row(db, espn_key=key, espn_id=key, name=key, position="WR",
                 team="AAA" if key == "E1" else "BBB", points_per_game=12.0)

    report = pe.build_ensemble(db, as_of=DAY, season=SEASON, weeks=[11])
    rows = {r.espn_id: r for r in report}
    plays, on_bye = rows["E1"], rows["E2"]

    assert plays.blended_points is not None          # the control still prices
    assert on_bye.blended_points is None, (
        "a player whose team plays none of the requested weeks must not carry a "
        "confident positive projection built from a season rate"
    )
    assert on_bye.espn_points is None and on_bye.house_points is None
    assert on_bye.agreement == pe.UNPRICEABLE
    assert pe.NO_GAME_IN_WINDOW in on_bye.flags
    assert any("NO GAME IN THIS WINDOW" in r for r in on_bye.reasons)
    assert any(pe.NO_GAME_IN_WINDOW in n for n in report.notes)
    # ...and the operator can SEE it on the table, not only on the object.
    assert pe.NO_GAME_IN_WINDOW in pe.format_ensemble(report)


def test_the_full_season_default_is_untouched_by_the_window_gate(db):
    """The gate must bite only on a NARROWED window: over weeks 1-17 every team
    plays somewhere, so nothing may be suppressed and no narrowing note fires."""
    seed_world(db, [wr("G2", "E2", "OnBye", 10.0, played=set(WEEKS) - {11},
                       team="BBB")])
    espn_row(db, espn_key="E2", espn_id="E2", name="OnBye", position="WR",
             team="BBB", points_per_game=12.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    row = _one(report)
    assert row.both_sources and row.blended_points is not None
    assert pe.NO_GAME_IN_WINDOW not in row.flags
    assert not any("NARROWED WINDOW" in n for n in report.notes)


def test_a_team_the_house_feed_never_mentions_is_flagged_not_guessed(db):
    """An ESPN-only player on a team with no house lines at all: whether he plays
    inside a narrowed window is UNKNOWN, and saying so is the honest output."""
    seed_world(db, [wr("G1", "E1", "Known", 10.0, played=set(WEEKS), team="AAA")])
    espn_row(db, espn_key="E9", espn_id="E9", name="Stranger", position="WR",
             team="ZZZ", points_per_game=12.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON, weeks=[11])
    stranger = next(r for r in report if r.espn_id == "E9")
    assert pe.WINDOW_PLAYABILITY_UNKNOWN in stranger.flags
    assert any("WINDOW UNVERIFIED" in r for r in stranger.reasons)
    assert any(pe.WINDOW_PLAYABILITY_UNKNOWN in n for n in report.notes)


def test_team_weeks_reads_the_schedule_out_of_the_lines_already_loaded(db):
    """The playability derivation must union over a TEAM's players, so one
    player's missing coverage cannot mark the whole team as on bye."""
    seed_world(db, [wr("G1", "E1", "Full", 10.0, played=set(WEEKS), team="AAA"),
                    wr("G2", "E2", "Thin", 10.0, played={1}, team="AAA")])
    lines = valuation.weekly_lines(db, as_of=DAY, season=SEASON, weeks=WEEKS,
                                   source=pe.HOUSE_SOURCE, view="historical")
    house = {k: (line, None) for k, line in lines.items()}
    assert pe._team_weeks(house)["AAA"] == frozenset(WEEKS)


# ---- finding [3]: the "typical" offset was not robust ---------------------


def test_two_outliers_cannot_redefine_what_is_typical_for_a_position(db):
    """MUTATION PINNED: median/MAD -> mean/pstdev in _position_moments.

    Measured on the live QB cohort (n=32): two rows at -148.4 and -116.5 dragged
    the centre to -21.4 (sd 33.1) against a median of -15.2 (robust scale 21.3),
    and the ranking INVERTED — a 40.6-point gap sorted BELOW a 1.0-point
    agreement in a report headed "the largest disagreements".
    """
    n = pe.MIN_POSITION_N + 2
    seed_world(db, [wr(f"G{i}", f"E{i}", f"P{i}", 10.0, played=set(WEEKS) - {9})
                    for i in range(n)])
    # A conformist cohort spread over +4..+11, plus two enormous negative rows.
    for i in range(n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA",
                 points_per_game=(160.0 + 2.0 + i) / 16.0, games=16.0)
    for key in ("E0", "E1"):
        espn_row(db, espn_key=key, espn_id=key, name=key, position="WR",
                 team="AAA", points_per_game=(160.0 - 150.0) / 16.0, games=16.0)

    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    rows = {r.espn_id: r for r in report if r.both_sources}
    deltas = [r.delta_points for r in rows.values()]
    centre, scale, count = pe._position_moments(
        [{"position": "WR", "delta_points": d} for d in deltas])["WR"]

    import statistics as _st
    assert count == n
    assert centre == pytest.approx(_st.median(deltas))
    assert centre != pytest.approx(_st.fmean(deltas)), (
        "the centre must be the MEDIAN; the mean is dragged by the two outliers"
    )
    conformists = [r for k, r in rows.items() if k not in ("E0", "E1")]
    # THE DISCRIMINATING ASSERTION. Measured on this exact fixture: the robust
    # scale makes each outlier |z| = 42.2, while mean/pstdev — inflated by the
    # very rows it is meant to make extreme — collapses them to |z| = 2.00 and
    # pushes the whole conforming cohort out to 0.44-0.56. A scale an outlier can
    # inflate cannot measure that outlier.
    assert all(abs(rows[k].delta_z) > 10.0 for k in ("E0", "E1")), (
        "a mean/pstdev scale is inflated by the outliers themselves, shrinking "
        "them to ~2 sigma and blunting the whole signal"
    )
    assert all(abs(r.delta_z) < 2.0 for r in conformists)
    ranked = [r.espn_id for r in pe.top_disagreements(
        report, top=None, per_position=n)["WR"]]
    assert set(ranked[:2]) == {"E0", "E1"}
    assert any("median" in x and "MAD" in x for x in conformists[0].reasons)


# ---- findings [5] / [25]: a missing crosswalk is not a fact about ESPN ----


def test_a_house_row_with_no_espn_id_says_so_instead_of_blaming_espn(db):
    """Two different absences were reported with one sentence. A house line with
    no `espn_id` cannot be LOOKED UP; that is a missing crosswalk, not evidence
    that ESPN publishes nothing. Nine priced live rows are in this state.
    """
    seed_world(db, [{"gsis": None, "espn_id": None, "name": "NoId Kicker",
                     "position": "K", "team": "AAA",
                     "played": set(WEEKS) - {9},
                     "per_week": {"fg_made_0_39": 2.0}}])
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    row = _one(report)
    assert row.agreement == pe.SINGLE_SOURCE_HOUSE
    assert pe.NO_ESPN_JOIN_KEY in row.flags
    assert any("MISSING CROSSWALK" in r for r in row.reasons)
    assert not any("publishes no season projection" in r for r in row.reasons), (
        "we established that he could not be looked up, not that ESPN is silent"
    )
    assert any("NO ESPN JOIN KEY" in n for n in report.notes)


def test_one_player_split_across_two_rows_is_flagged_as_a_possible_duplicate(db):
    """Measured live: the priceable kicker board carries 37 rows for 32 teams,
    and the GB pair is provably one roster slot — a no-join-key house row and an
    ESPN-only row for the same (position, team). They are FLAGGED, never merged:
    there is no id to merge on and matching by name is the guess this system
    refuses to make."""
    seed_world(db, [{"gsis": None, "espn_id": None, "name": None,
                     "position": "K", "team": "AAA",
                     "played": set(WEEKS) - {9},
                     "per_week": {"fg_made_0_39": 2.0}}])
    espn_row(db, espn_key="9999", espn_id="9999", name="Same Slot Kicker",
             position="K", team="AAA", points_per_game=8.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    priced = [r for r in report if r.blended_points is not None]
    assert len(priced) == 2, "the split identity is the situation under test"
    assert all(pe.POSSIBLE_DUPLICATE_IDENTITY in r.flags for r in priced)
    assert all(any("POSSIBLE DUPLICATE" in x for x in r.reasons) for r in priced)
    assert any(pe.POSSIBLE_DUPLICATE_IDENTITY in n for n in report.notes)
    # ...and neither row is silently merged away.
    assert {r.house_points is not None for r in priced} == {True, False}


# ---- finding [6]: a Pearson r on n=5 was printed unlabelled ---------------


def test_a_positions_correlation_is_withheld_below_the_sample_floor(db):
    """MIN_POSITION_N already withholds `delta_z` from a novice for exactly this
    reason; the printed correlation ignored it. Live at top=200 the DST cohort is
    5 players and printed r=0.2978 while the full 32-defence figure is 0.8271.
    """
    # The LIVE shape: the position has plenty of players overall (so delta_z is
    # computed and the block renders), but the `top` COHORT is tiny — exactly
    # how DST came to print r=0.2978 on 5 rows beside a full-pool 0.8271.
    n = pe.MIN_POSITION_N + 8
    seed_world(db, [wr(f"G{i}", f"E{i}", f"P{i}", 20.0 - i,
                       played=set(WEEKS) - {9}) for i in range(n)])
    for i in range(n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA",
                 points_per_game=(21.0 - i) + (i % 3), games=16.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)

    small = pe.MIN_POSITION_N - 3
    corr = pe.source_correlation(report, top=small)
    count, r = corr.by_position["WR"]
    assert count == small
    assert r is None, "a correlation on fewer than MIN_POSITION_N rows is noise"

    full_count, full_r = pe.source_correlation(report).by_position["WR"]
    assert full_count == n and full_r is not None, (
        "the floor must withhold the small COHORT's r, not the position's"
    )
    rendered = pe.format_disagreements(report, top=small)
    assert "WITHHELD" in rendered and str(pe.MIN_POSITION_N) in rendered
    assert "r=" not in rendered.split("\nWR")[1].splitlines()[0]


def test_a_position_at_the_floor_does_report_its_correlation(db):
    """The floor must not swallow a legitimate cohort: exactly MIN_POSITION_N
    players is enough."""
    n = pe.MIN_POSITION_N
    seed_world(db, [wr(f"G{i}", f"E{i}", f"P{i}", 10.0 + i,
                       played=set(WEEKS) - {9}) for i in range(n)])
    for i in range(n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA", points_per_game=11.0 + i, games=16.0)
    corr = pe.source_correlation(pe.build_ensemble(db, as_of=DAY, season=SEASON))
    count, r = corr.by_position["WR"]
    assert count == n and r is not None


# ---- finding [10]: the headline table hid the availability flag -----------


def test_the_headline_table_shows_the_flags_it_claims_to_surface(db):
    """The module claims ESPN's availability opinion "is surfaced as its own
    field and its own reason line". That was untrue of the only table it ships:
    _COLUMNS had no flags column and no note mentioned availability, so a
    2-game projection rendered as an 8x extrapolation with nothing to smell.
    Live 2026 carries ten such players (Reagor at 2 games, Beck at 4).
    """
    seed_world(db, [wr("G1", "E1", "Full Timer", 10.0, played=set(WEEKS) - {9}),
                    wr("G2", "E2", "Hurt Guy", 10.0, played=set(WEEKS) - {9})])
    espn_row(db, espn_key="E1", espn_id="E1", name="Full Timer", position="WR",
             team="AAA", points_per_game=10.0, games=17.0)
    espn_row(db, espn_key="E2", espn_id="E2", name="Hurt Guy", position="WR",
             team="AAA", points_per_game=10.0, games=2.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    hurt = next(r for r in report if r.espn_id == "E2")
    assert pe.ESPN_EXPECTS_ABSENCE in hurt.flags
    assert hurt.espn_expected_games == 2.0

    rendered = pe.format_ensemble(report)
    assert "FLAGS" in rendered.splitlines()[1], "the table needs a flags column"
    hurt_line = next(ln for ln in rendered.splitlines() if "Hurt Guy" in ln)
    assert pe.ESPN_EXPECTS_ABSENCE in hurt_line, (
        "the flag must be on the row a novice reads, not only on the object"
    )
    assert any(pe.ESPN_EXPECTS_ABSENCE in n and "EXTRAPOLATED" in n
               for n in report.notes)
    # The availability opinion still must NOT move the points (item 3.2 lesson).
    full = next(r for r in report if r.espn_id == "E1")
    assert hurt.blended_points == pytest.approx(full.blended_points)


# ---- findings [13] / [20]: a labelled cohort that did not exist -----------


def test_the_kicker_caveat_does_not_claim_seasons_the_database_lacks():
    """The caveat is printed on every live kicker row under an explicit
    "MEASURED, not speculative" banner, where the strength of the cited cohort is
    the whole point of quoting it. It claimed "104,652 stored kicker rows across
    2021-2026"; the `projections` table holds SEASON 2026 ONLY (item 1.5 — free
    historical point-in-time projections proved infeasible), so all 104,652 rows
    are one season and the claim overstated its breadth fivefold.
    """
    caveat = pe.POSITION_CAVEATS["K"]
    assert "2021-2026" not in caveat
    assert "104,652" in caveat            # the count itself is correct
    assert "SEASON 2026 ONLY" in caveat
    assert "sleeper_rotowire" in caveat
    assert "one season" in caveat


def test_the_kicker_caveat_matches_the_projections_table_it_describes(db):
    """The structural claim behind the caveat, checked against the schema rather
    than trusted: the house feed HAS 50+ bucket columns and a misses column, so
    "publishes no 50+ bucket" is a statement about the DATA, not the schema."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(projections)")}
    assert {"fg_made_50_59", "fg_made_60", "fg_missed"} <= cols
    assert db.execute(
        "SELECT COUNT(*) FROM projections WHERE fg_made_50_59 IS NOT NULL"
    ).fetchone()[0] == 0


# ---- finding [23]: the caveat was absent exactly where it was load-bearing -


def kicker(gsis, espn_id, name, fg_per_week, *, played=WEEKS, team="AAA"):
    return {"gsis": gsis, "espn_id": espn_id, "name": name, "position": "K",
            "team": team, "played": set(played),
            "per_week": {"fg_made_0_39": fg_per_week, "pat_made": 2.0}}


def test_a_kicker_priced_only_by_the_house_feed_still_gets_the_caveat(db):
    """The caveat used to be gated on ESPN having an opinion, putting it where it
    was redundant and withholding it where it matters most. Live: 5 kickers are
    SINGLE_SOURCE_HOUSE (house ranks 197, 200, 239, 376, 382 — two inside the
    top-200 board) and 0 of them carried it, though their number is the defective
    one and they have no second opinion to notice the gap from.
    """
    seed_world(db, [kicker("G1", "E1", "Lonely Kicker", 2.0,
                           played=set(WEEKS) - {9})])
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    row = _one(report)
    assert row.agreement == pe.SINGLE_SOURCE_HOUSE and row.espn_points is None
    assert any(r.startswith(pe.POSITION_CAVEATS["K"][:40]) for r in row.reasons), (
        "a row priced ENTIRELY by the feed with the known defect must disclose it"
    )
    # The comparison half is withheld — there is no ESPN number to compare to.
    assert not any(pe.POSITION_CAVEATS_BOTH_SOURCES["K"] in r for r in row.reasons)


def _espn_kicker(conn, *, espn_key, espn_id, name, fg_per_season, day=DAY):
    """An ESPN season row for a kicker, priced through real kicker keys so both
    sides of the comparison are non-zero."""
    row = {c: None for c in ep._ROW_COLUMNS}
    row.update({
        "source": ep.SOURCE, "espn_key": espn_key, "espn_id": espn_id,
        "gsis_id": None, "player": name, "position": "K", "team": "AAA",
        "season": SEASON, "week": ep.SEASON_WEEK, "projected_games": 17.0,
        "espn_applied_total": None, "fg_made_0_39": fg_per_season,
        "pat_made": 30.0, "retrieved_as_of": day, "knowable_as_of": day,
    })
    base.upsert(conn, "espn_projections", [row], key_cols=ep._PK_COLS)


def test_a_kicker_with_both_opinions_gets_the_comparison_half_too(db):
    seed_world(db, [kicker("G1", "E1", "Two View Kicker", 2.0,
                           played=set(WEEKS) - {9})])
    _espn_kicker(db, espn_key="E1", espn_id="E1", name="Two View Kicker",
                 fg_per_season=40.0)
    row = _one(pe.build_ensemble(db, as_of=DAY, season=SEASON))
    assert row.both_sources
    assert any(pe.POSITION_CAVEATS_BOTH_SOURCES["K"] in r for r in row.reasons)


# ---- finding [21]: a floored ratio printed under an unfloored label -------


def test_the_relative_gap_says_so_when_the_floor_binds(db):
    """MUTATION PINNED: removing MIN_RELATIVE_DENOMINATOR entirely survived the
    whole file, so the constant was an untested knob whose only visible effect
    was to make its own label untrue. Live: the floor binds on 21 rows and each
    printed a wrong percentage with no disclosure (Charlie Jones 18% against an
    actual 33%).
    """
    n = pe.MIN_POSITION_N + 1
    people = [wr(f"G{i}", f"E{i}", f"P{i}", 10.0, played=set(WEEKS) - {9})
              for i in range(1, n)]
    # A pair of near-zero projections: house 1.6 total, ESPN 3.2 total.
    people.append(wr("G0", "E0", "Tiny", 0.1, played=set(WEEKS) - {9}))
    seed_world(db, people)
    for i in range(1, n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA", points_per_game=11.0, games=16.0)
    espn_row(db, espn_key="E0", espn_id="E0", name="Tiny", position="WR",
             team="AAA", points_per_game=0.2, games=16.0)

    tiny = next(r for r in pe.build_ensemble(db, as_of=DAY, season=SEASON)
                if r.espn_id == "E0")
    average = (abs(tiny.house_points) + abs(tiny.espn_points)) / 2.0
    assert average < pe.MIN_RELATIVE_DENOMINATOR, "the floor must bind here"
    assert tiny.delta_relative == pytest.approx(
        abs(tiny.delta_points) / pe.MIN_RELATIVE_DENOMINATOR)

    line = next(r for r in tiny.reasons if r.startswith("Disagreement"))
    assert "FLOOR" in line, "a floored ratio must not be labelled as an average"
    assert f"{abs(tiny.delta_points) / average:.0%}" in line, (
        "the unfloored figure must be disclosed alongside it"
    )

    # A normal row keeps the plain, true label.
    normal = next(r for r in pe.build_ensemble(db, as_of=DAY, season=SEASON)
                  if r.espn_id == "E1")
    normal_line = next(r for r in normal.reasons if r.startswith("Disagreement"))
    assert "FLOOR" not in normal_line
    assert "two sources' average" in normal_line


# ---- finding [22]: the horizon note asserted 16/17 whatever the window ----


def test_the_horizon_note_describes_the_window_it_is_actually_in(db):
    """The note that exists to stop a novice misreading the numbers asserted
    "the house (16 games, weeks 1-17) and ESPN (17 games) ... ~6%" verbatim — so
    the module's own 14-week test world printed "a COMMON horizon of 14 games"
    followed by a false sentence about 16 and 17."""
    weeks = tuple(range(1, 15))
    seed_world(db, [wr("G1", "E1", "Short", 10.0, played=set(weeks))],
               weeks=weeks)
    espn_row(db, espn_key="E1", espn_id="E1", name="Short", position="WR",
             team="AAA", points_per_game=12.0, games=17.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON, weeks=weeks)
    assert report.horizon_games == 14.0
    note = next(n for n in report.notes if "COMMON horizon" in n)
    assert "14 games" in note
    assert "16 games" not in note, "the illustration must be derived, not fixed"
    assert "the house covers 14 games here and ESPN projects 17" in note


def test_a_horizon_borrowed_from_espn_alone_says_where_it_came_from(db):
    """With ESPN rows stored and no priceable house line the code falls back to
    ESPN's horizon "and say so" — but the emitted note was the ordinary one,
    claiming a COMMON horizon "for both sources" when there was no house source
    at all."""
    espn_row(db, espn_key="E1", espn_id="E1", name="Orphan", position="WR",
             team="AAA", points_per_game=12.0, games=17.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)
    assert report.horizon_games == 17.0
    note = next(n for n in report.notes if "COMMON horizon" in n)
    assert "ESPN ALONE" in note
    assert "no house line could be priced" in note


# ==========================================================================
# INGEST GUARDS — findings [4] / [7] / [9] / [19]
# ==========================================================================


def _universe(n=40, *, position_id=3, base_id=1000, template="Ja'Marr Chase"):
    """A synthetic ESPN universe big enough to exercise the coverage floors.

    Cloned from a CAPTURED row so the stat ids, the split shapes and
    `appliedTotal` are all real; only the identity changes. The template must
    match `position_id` or the mapper finds no priceable stat and drops it.
    """
    template = captured(template)
    out = []
    for i in range(n):
        p = json.loads(json.dumps(template))
        p["id"] = base_id + i
        p["fullName"] = f"Player {i}"
        p["defaultPositionId"] = position_id
        out.append(p)
    return out


def _strip_applied_total(players, *, only_position_id=None):
    for p in players:
        if only_position_id is not None and p["defaultPositionId"] != only_position_id:
            continue
        for entry in p.get("stats") or []:
            entry.pop("appliedTotal", None)
    return players


def _rename_stat(players, old, new):
    for p in players:
        for entry in p.get("stats") or []:
            stats = entry.get("stats") or {}
            if old in stats:
                stats[new] = stats.pop(old)
    return players


def test_a_renamed_stat_id_is_still_caught_while_the_reference_exists(db):
    """The control: with `appliedTotal` present, a renumbered stat id must raise.
    This guard already worked and must keep working."""
    players = _rename_stat(_universe(), "42", "9942")
    with pytest.raises(ValueError, match="schema drift"):
        ep.ingest_espn_projections(db, players, retrieved_as_of=DAY, season=SEASON)
    assert db.execute("SELECT COUNT(*) FROM espn_projections").fetchone()[0] == 0


def test_dropping_espns_applied_total_disarms_nothing_it_refuses_the_pull(db):
    """FINDING [4] / [9]. `_check_residual` skipped any row with no
    `espn_applied_total`, then returned 0.0 when there were none left — so the
    ONE event class the guard exists to catch (ESPN changing its payload shape)
    was the event that DELETED the guard's own reference. Reproduced on the live
    1,030-player universe: renaming stat 42 AND dropping `appliedTotal` wrote
    1,041 rows with no exception, and the stored WR median house_points was 186.6
    against 337.5 intact — every receiver silently ~45% low, on a board the
    ensemble then reports as house-vs-ESPN "disagreement".
    """
    players = _strip_applied_total(_rename_stat(_universe(), "42", "9942"))
    with pytest.raises(ValueError, match="DISAPPEARED"):
        ep.ingest_espn_projections(db, players, retrieved_as_of=DAY, season=SEASON)
    assert db.execute("SELECT COUNT(*) FROM espn_projections").fetchone()[0] == 0


def test_the_coverage_floor_is_per_position_because_drift_is_position_shaped(db):
    """The partial case, which is likelier than the total one: offence loses
    `appliedTotal` while K and D/ST keep theirs. A POOLED floor is cleared by the
    kickers and defences while every offensive row goes unchecked — measured, the
    stored WR median was 186.6 / TE 107.9 while K and DST were exact."""
    wrs = _universe(30, position_id=3, base_id=1000)          # WR
    kickers = _universe(30, position_id=5, base_id=2000,
                        template="Brandon Aubrey")            # K
    players = _strip_applied_total(wrs + kickers, only_position_id=3)
    with pytest.raises(ValueError) as exc:
        ep.ingest_espn_projections(db, players, retrieved_as_of=DAY, season=SEASON)
    assert "WR" in str(exc.value) and "some positions" in str(exc.value)
    assert db.execute("SELECT COUNT(*) FROM espn_projections").fetchone()[0] == 0


def test_a_healthy_pull_still_passes_the_coverage_floor(db):
    """The floor must not cry wolf: the captured rows carry 100% coverage, which
    is what the live pool measures on all six positions."""
    assert ep.ingest_espn_projections(
        db, _universe(), retrieved_as_of=DAY, season=SEASON) > 0


def test_a_pull_with_no_scorable_total_at_all_is_refused(db):
    """Coverage passes but every total is below the residual floor: nothing could
    be checked, which is a broken pull rather than a quiet one."""
    players = _universe(12)
    for p in players:
        for entry in p.get("stats") or []:
            entry["appliedTotal"] = 0.5
    with pytest.raises(ValueError, match="at least 5 points"):
        ep.ingest_espn_projections(db, players, retrieved_as_of=DAY, season=SEASON)


def test_a_pull_carrying_only_the_season_split_is_not_a_degraded_pool(db):
    """FINDING [7]. The floor counted season rows and current-scoring-period rows
    in ONE population, so a legitimate offseason / between-periods pull — ESPN
    serving the season projection but no current period — arrived as 524 rows
    against a 1,041 pooled yardstick and was refused as "a degraded pool". Worse,
    `_stored_yardstick` takes a max over stored partitions, so it stayed refused
    on EVERY retry and only `allow_shrink` cleared it.
    """
    full = _universe()
    assert ep.ingest_espn_projections(db, full, retrieved_as_of=DAY, season=SEASON)
    stored = dict(db.execute(
        "SELECT week, COUNT(*) FROM espn_projections GROUP BY week").fetchall())
    assert set(stored) == {ep.SEASON_WEEK, 1}, "the fixture must carry both splits"

    season_only = json.loads(json.dumps(full))
    for p in season_only:
        p["stats"] = [s for s in p["stats"] if s.get("statSplitTypeId") != 1]

    written = ep.ingest_espn_projections(
        db, season_only, retrieved_as_of="2026-08-31", season=SEASON)
    assert written == stored[ep.SEASON_WEEK], (
        "a complete season split is a complete season split; the missing current "
        "scoring period is not a shrink of the player pool"
    )
    assert db.execute(
        "SELECT COUNT(*) FROM espn_projections WHERE retrieved_as_of = '2026-08-31'"
    ).fetchone()[0] == stored[ep.SEASON_WEEK]


def test_a_genuinely_degraded_player_pool_is_still_refused(db):
    """The other half of finding [7]: narrowing the floor must not blunt it. A
    season split that loses most of its PLAYERS is the real collapse."""
    ep.ingest_espn_projections(db, _universe(40), retrieved_as_of=DAY, season=SEASON)
    with pytest.raises(ep.ProjectionCollapse, match="WHOLE-SEASON projection"):
        ep.ingest_espn_projections(db, _universe(5), retrieved_as_of="2026-08-31",
                                   season=SEASON)
    assert db.execute(
        "SELECT COUNT(*) FROM espn_projections WHERE retrieved_as_of = '2026-08-31'"
    ).fetchone()[0] == 0


def test_a_degraded_current_period_is_refused_when_it_is_served_at_all(db):
    """A pull that DOES carry the scoring-period split but with a collapsed pool
    is still a collapse — the exemption is for an absent partition only."""
    ep.ingest_espn_projections(db, _universe(40), retrieved_as_of=DAY, season=SEASON)
    thin = _universe(40)
    for p in thin[5:]:
        p["stats"] = [s for s in p["stats"] if s.get("statSplitTypeId") != 1]
    with pytest.raises(ep.ProjectionCollapse, match="week-1 projection"):
        ep.ingest_espn_projections(db, thin, retrieved_as_of="2026-08-31",
                                   season=SEASON)


def test_a_second_source_lands_additively_instead_of_overwriting(db):
    """FINDING [19]. The migration's own comment says the `source` column exists
    so a second opinion "lands additively, without a migration" — but `source`
    was not in the PRIMARY KEY, so two rows identical in
    (season, week, espn_key, retrieved_as_of) collapsed to ONE and the second
    silently REPLACED the first."""
    def row(source, yards):
        r = {c: None for c in ep._ROW_COLUMNS}
        r.update({"source": source, "espn_key": "X", "espn_id": "X",
                  "position": "WR", "team": "AAA", "season": SEASON,
                  "week": ep.SEASON_WEEK, "receiving_yards": yards,
                  "projected_games": 17.0,
                  "retrieved_as_of": DAY, "knowable_as_of": DAY})
        return r

    base.upsert(db, "espn_projections", [row("espn", 100.0), row("espn_v2", 999.0)],
                key_cols=ep._PK_COLS)
    stored = dict(db.execute(
        "SELECT source, receiving_yards FROM espn_projections").fetchall())
    assert stored == {"espn": 100.0, "espn_v2": 999.0}

    # ...and the accessor scopes to one source by default, so a caller that never
    # asked for a second opinion cannot be handed two rows for one player.
    default = ep.get_espn_projections(db, as_of=DAY, season=SEASON)
    assert [r["source"] for r in default] == ["espn"]
    both = ep.get_espn_projections(db, as_of=DAY, season=SEASON, source=None)
    assert sorted(r["source"] for r in both) == ["espn", "espn_v2"]
    assert ep._PK_COLS[0] == "source"


def test_two_rows_either_side_of_a_band_edge_do_not_print_the_same_z(db):
    """Finding [16]'s tail: the bands turn at exactly 1.0 and 2.0, so rounding
    the displayed z to ONE place put rows on opposite sides of an edge under one
    printed number. Live board: four such collisions, including two WRs both
    showing z -1.0 where one reads AGREED and the other MILD — which a novice can
    only read as the table contradicting itself.
    """
    n = 12
    seed_world(db, [wr(f"G{i}", f"E{i}", f"P{i}", 10.0, played=set(WEEKS) - {9})
                    for i in range(n)])
    for i in range(n):
        espn_row(db, espn_key=f"E{i}", espn_id=f"E{i}", name=f"P{i}",
                 position="WR", team="AAA",
                 points_per_game=(160.0 + i) / 16.0, games=16.0)
    report = pe.build_ensemble(db, as_of=DAY, season=SEASON)

    shown: dict[str, set[str]] = {}
    for row in report:
        if row.delta_z is None:
            continue
        shown.setdefault(f"{row.delta_z:.{pe._Z_DECIMALS}f}", set()).add(row.agreement)
    assert len(shown) > 1, "the fixture must produce a spread of z values"
    collisions = {z: bands for z, bands in shown.items() if len(bands) > 1}
    assert not collisions, (
        f"these printed z values carry more than one agreement band: {collisions}"
    )
    # ...and the precision is genuinely finer than the band edges.
    assert pe._Z_DECIMALS >= 2
