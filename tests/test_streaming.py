"""Streaming ranker tests — item 3.5 (Module A: core/streaming.py).

Offline throughout: the synthetic ``marginal_world`` projection/league universe
plus hand-inserted schedules / odds / weather rows. Everything is synthetic by
necessity and by Rule 5 (no real colleague/player names) — the live DB is
pre-draft, and ``game_weather`` is EMPTY, so the demonstrable done-when (weather
moves a kicker's rank) runs on INJECTED synthetic wind, which is exactly the
``weather=`` / ``odds=`` injection seam the module ships for.

The tests that matter most pin MEASURED design decisions: that ``house_points``
is a verbatim ``scoring.py`` output (Rule 2) while ``stream_score`` is a labelled
matchup hypothesis; that a weaker opponent offense monotonically tilts a D/ST up
(the load-bearing primary adjustment); that a pre-gameday read cannot see a
Vegas line (leakage); and that a bye / OUT / unprojected candidate is refused,
never phantom-zeroed (Rule 6).
"""

import re
from pathlib import Path

import pytest

from ziggurat.core import scoring, streaming
from ziggurat.core.streaming import (
    DEFAULT_STREAM_ADJUST,
    StreamPositionError,
    format_stream_board,
    rank_streamers,
)
from ziggurat.data.nfl import projections

SEASON = 2026
PULL = "2026-09-15"
WEEK = 3


# --------------------------------------------------------------------- builders


def _sched(db, *, game_id, week, home, away, gameday, season=SEASON,
           knowable="2026-08-01", retrieved="2026-08-01"):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, retrieved_as_of, knowable_as_of) VALUES "
        "(?, ?, ?, 'REG', ?, ?, ?, ?, ?)",
        (game_id, season, week, gameday, home, away, retrieved, knowable),
    )


def _odds(db, *, game_id, home, away, total, spread, week=WEEK, season=SEASON,
          knowable, retrieved=PULL):
    db.execute(
        "INSERT INTO game_odds (game_id, season, week, home_team, away_team, "
        "spread_line, total_line, retrieved_as_of, knowable_as_of) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, home, away, spread, total, retrieved, knowable),
    )


def _weather_row(game_id, *, wind=None, precip=None, relevant=1, home="H"):
    return {"game_id": game_id, "home_team": home, "wind_mph": wind,
            "precip_mm": precip, "weather_relevant": relevant, "forecast_source": "forecast"}


def _basic_world(marginal_world, db, *, dst_injury="ACTIVE", k_injury="ACTIVE"):
    """One DST FA (MIA) facing NYG, one K FA (KC) facing DEN, an opponent offense
    for NYG, plus reference offenses so the opponent-quality std is nonzero."""
    specs = [
        {"name": "Bay D/ST", "pos": "D/ST", "team": "MIA", "pts": 5.0, "bye": 8,
         "injury": dst_injury},
        {"name": "Boot Leg", "pos": "K", "team": "KC", "pts": 8.0, "bye": 10,
         "injury": k_injury},
        # NYG offense (MIA's opponent) ~ 22 house pts this week.
        {"name": "Giant Runner", "pos": "RB", "team": "NYG", "pts": 12.0, "bye": 7},
        {"name": "Giant Catcher", "pos": "WR", "team": "NYG", "pts": 10.0, "bye": 7},
        # reference offenses on other teams (spread -> nonzero std).
        {"name": "Ref Runner A", "pos": "RB", "team": "SEA", "pts": 6.0, "bye": 9},
        {"name": "Ref Runner B", "pos": "RB", "team": "TB", "pts": 18.0, "bye": 9},
        {"name": "Ref Catcher C", "pos": "WR", "team": "LAC", "pts": 14.0, "bye": 9},
    ]
    marginal_world(specs, retrieved=PULL)
    _sched(db, game_id="G_MIA", week=WEEK, home="MIA", away="NYG", gameday="2026-09-21")
    _sched(db, game_id="G_KC", week=WEEK, home="KC", away="DEN", gameday="2026-09-21")
    _sched(db, game_id="G_W1", week=1, home="MIA", away="NYG", gameday="2026-09-10")
    db.commit()


# ------------------------------------------------------------- Rule 1 (leakage)


def test_rank_streamers_requires_as_of(db):
    with pytest.raises(TypeError):
        rank_streamers(db, season=SEASON, position="DST", week=WEEK)


def test_a_pre_gameday_read_sees_no_vegas_line(db, marginal_world):
    """(c) LEAKAGE: game_odds is stamped knowable=gameday, so a Wednesday read
    before a Sunday game gets NOTHING through the historical view — the D/ST
    stream discloses 'line not yet posted' rather than leaking a closing line."""
    _basic_world(marginal_world, db)
    # a closing line that only becomes knowable on gameday (a future Sunday)
    _odds(db, game_id="G_MIA", home="MIA", away="NYG", total=40.0, spread=-3.0,
          knowable="2026-09-20")
    db.commit()
    board = rank_streamers(db, as_of="2026-09-16", season=SEASON, position="DST", week=WEEK)
    assert board.odds_available is False
    blob = " ".join(r for rec in board.ranked for r in rec.reasons)
    assert "line not yet posted" in blob
    # but at/after gameday the SAME accessor surfaces it
    after = rank_streamers(db, as_of="2026-09-21", season=SEASON, position="DST", week=WEEK)
    assert after.odds_available is True


def test_view_threading_hides_a_late_retrieved_candidate_under_historical(db, marginal_world):
    """(f) a DST whose rows were retrieved AFTER the as-of (a later correction),
    knowable before it, is hidden by the historical view and shown by
    latest_truth — proving view threads into get_free_agents AND weekly_lines."""
    _basic_world(marginal_world, db)
    db.execute(
        "INSERT INTO projections (source, source_player_id, season, week, "
        "season_type, position, team, opponent, sacks, retrieved_as_of, knowable_as_of) "
        "VALUES ('sleeper_rotowire', 'LAT', 2026, 3, 'regular', 'DEF', 'LAT', 'OPP', "
        "6.0, '2026-09-16', '2026-09-10')"
    )
    db.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, "
        "position, pro_team, on_team_id, roster_status, injury_status, percent_owned, "
        "retrieved_as_of, knowable_as_of) VALUES "
        "(2026, '-17999', NULL, 'Late D/ST', 'D/ST', 'LAT', NULL, 'FREEAGENT', "
        "'ACTIVE', 50.0, '2026-09-16', '2026-09-10')"
    )
    db.commit()
    hist = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    truth = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK,
                           view="latest_truth")
    assert "Late D/ST" not in [r.player for r in hist.ranked]
    assert "Late D/ST" in [r.player for r in truth.ranked]


# ------------------------------------------------------------- Rule 2 (scoring)


def test_house_points_equal_scoring_of_the_raw_row_for_dst_and_k(db, marginal_world):
    """(b) RULE 2: house_points is a verbatim scoring.py output — for both a D/ST
    and a K it equals scoring.score(pos, raw_projection_row)."""
    _basic_world(marginal_world, db)
    rows = projections.get_projections(db, as_of=PULL, season=SEASON,
                                       source="sleeper_rotowire")

    dst_board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    dst = next(r for r in dst_board.ranked if r.team == "MIA")
    raw_dst = next(dict(r) for r in rows
                   if r["position"] == "DEF" and r["team"] == "MIA" and r["week"] == WEEK)
    assert dst.house_points == pytest.approx(scoring.score("DST", raw_dst, scoring.HOUSE_RULES))

    k_board = rank_streamers(db, as_of=PULL, season=SEASON, position="K", week=WEEK)
    k = next(r for r in k_board.ranked if r.team == "KC")
    raw_k = next(dict(r) for r in rows
                 if r["gsis_id"] == k.gsis_id and r["week"] == WEEK)
    assert k.house_points == pytest.approx(scoring.score("K", raw_k, scoring.HOUSE_RULES))


def test_streaming_module_hardcodes_no_scoring_constant():
    """(b) GREP-PROOF RULE 2: the module never scores a stat line itself — it
    prices through weekly_lines and references no scoring weight/bracket/scorer."""
    src = Path(streaming.__file__).read_text()
    assert "weekly_lines" in src                       # pricing is delegated
    for banned in ("points_per", "_brackets", "score_offense(", "score_kicker(",
                   "score_dst(", "scoring.score("):
        assert banned not in src, banned


def test_stream_score_is_labelled_a_hypothesis_not_house_points(db, marginal_world):
    """RULE 2 / RULE 6: every ranked row discloses that stream_score is a
    matchup-adjusted HYPOTHESIS and is not the house score."""
    _basic_world(marginal_world, db)
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    for rec in board.ranked:
        blob = " ".join(rec.reasons)
        assert "HYPOTHESIS" in blob and "not house scoring" in blob


# ------------------------------------------------------ (a) weather done-when (K)


def _k_pair_world(marginal_world, db):
    """Two kickers: K_WIND (KC, in a weather-relevant game) and K_DOME (DET, dome).
    K_WIND out-projects K_DOME, so wind is what can flip the order."""
    specs = [
        {"name": "Wind Kicker", "pos": "K", "team": "KC", "pts": 8.0, "bye": 10},
        {"name": "Dome Kicker", "pos": "K", "team": "DET", "pts": 7.5, "bye": 11},
    ]
    marginal_world(specs, retrieved=PULL)
    _sched(db, game_id="G_KC", week=WEEK, home="KC", away="DEN", gameday="2026-09-21")
    _sched(db, game_id="G_DET", week=WEEK, home="DET", away="CHI", gameday="2026-09-21")
    db.commit()


def test_weather_moves_a_kickers_stream_score_and_rank(db, marginal_world):
    """(a) DONE-WHEN: the same kicker, priced under calm (5 mph) vs windy (28 mph)
    injected weather, gets a DIFFERENT stream_score and a DIFFERENT rank, and the
    reason names the wind value."""
    _k_pair_world(marginal_world, db)
    dome = _weather_row("G_DET", relevant=0, home="DET")
    calm = [_weather_row("G_KC", wind=5.0, precip=0.0, home="KC"), dome]
    windy = [_weather_row("G_KC", wind=28.0, precip=0.0, home="KC"), dome]

    b_calm = rank_streamers(db, as_of=PULL, season=SEASON, position="K", week=WEEK, weather=calm)
    b_windy = rank_streamers(db, as_of=PULL, season=SEASON, position="K", week=WEEK, weather=windy)

    wind_calm = next(r for r in b_calm.ranked if r.player == "Wind Kicker")
    wind_windy = next(r for r in b_windy.ranked if r.player == "Wind Kicker")

    # different stream_score
    assert wind_windy.stream_score < wind_calm.stream_score
    # house_points is untouched by weather (Rule 2)
    assert wind_windy.house_points == wind_calm.house_points
    # different rank: calm -> Wind Kicker leads; windy -> Dome Kicker leads
    assert b_calm.ranked[0].player == "Wind Kicker"
    assert b_windy.ranked[0].player == "Dome Kicker"
    # the reason names the wind value in both regimes
    assert any("5 mph" in r for r in wind_calm.reasons)
    assert any("28 mph" in r for r in wind_windy.reasons)


# ---------------------------------------- (d) opponent-quality monotonicity (D/ST)


def test_opponent_quality_tilts_a_dst_monotonically(db, marginal_world):
    """(d) sweeping a D/ST's opponent-offense projection low->high moves its
    stream_score DOWN monotonically — the primary, load-bearing adjustment. Five
    identical-house-points defenses face opponents of increasing offensive
    strength; a large fixed filler pool keeps the reference distribution stable."""
    specs = []
    opp_totals = [5.0, 10.0, 15.0, 20.0, 25.0]
    for i, tot in enumerate(opp_totals):
        specs.append({"name": f"Stream D{i}", "pos": "D/ST", "team": f"DS{i}",
                      "pts": 5.0, "bye": 8})                       # identical house pts
        specs.append({"name": f"Opp RB {i}", "pos": "RB", "team": f"OP{i}",
                      "pts": tot, "bye": 7})                       # opponent offense
    # a big fixed filler offense pool at 15.0 so excluding one opponent barely
    # shifts the reference mean/std -> the tilt is strictly monotonic in opp pts.
    for j in range(10):
        specs.append({"name": f"Filler {j}", "pos": "WR", "team": f"FL{j}",
                      "pts": 15.0, "bye": 9})
    marginal_world(specs, retrieved=PULL)
    for i in range(5):
        _sched(db, game_id=f"G_D{i}", week=WEEK, home=f"DS{i}", away=f"OP{i}",
               gameday="2026-09-21")
    db.commit()

    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    by_team = {r.team: r for r in board.ranked}
    scores = [by_team[f"DS{i}"].stream_score for i in range(5)]
    # strictly decreasing as the opponent offense strengthens
    assert all(a > b for a, b in zip(scores, scores[1:])), scores
    # and the reason names the opponent's projected points
    weak = by_team["DS0"]
    assert any("OP0" in r and "5.0 house pts" in r for r in weak.reasons)


# ------------------------------------------------------ (e) bye / OUT / unpriceable


def test_a_dst_on_bye_this_week_is_never_ranked(db, marginal_world):
    """(e) a D/ST whose bye is this week has NO projection row this week (the D/ST
    bye row is absent) — it is refused with a note, never phantom-zeroed."""
    specs = [
        {"name": "Bye D/ST", "pos": "D/ST", "team": "MIA", "pts": 9.0, "bye": WEEK},
        {"name": "Play D/ST", "pos": "D/ST", "team": "BUF", "pts": 4.0, "bye": 8},
    ]
    marginal_world(specs, retrieved=PULL)
    _sched(db, game_id="G_BUF", week=WEEK, home="BUF", away="NYJ", gameday="2026-09-21")
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    names = [r.player for r in board.ranked]
    assert "Bye D/ST" not in names
    assert "Play D/ST" in names
    assert all(r.stream_score != 0.0 or r.house_points != 0.0 for r in board.ranked)
    assert any("Bye D/ST" in n and "BYE" in n for n in board.notes)


def test_a_kicker_ruled_out_after_the_live_gate_is_never_ranked(db, marginal_world):
    """(e) a kicker ESPN lists OUT is benched once live_status is on (as_of is
    after week-1 kickoff) — refused, not ranked."""
    _basic_world(marginal_world, db, k_injury="OUT")
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="K", week=WEEK)
    assert "Boot Leg" not in [r.player for r in board.ranked]
    assert any("Boot Leg" in n and "OUT" in n for n in board.notes)


def test_an_unprojected_candidate_is_refused_not_phantom_zeroed(db, marginal_world):
    """(e) a D/ST with no projection at all at this as-of is refused with a note,
    never ranked at a phantom 0."""
    specs = [
        {"name": "Ghost D/ST", "pos": "D/ST", "team": "MIA", "pts": 5.0, "bye": 8,
         "forecast": set()},                          # no forecast week at all
        {"name": "Real D/ST", "pos": "D/ST", "team": "BUF", "pts": 4.0, "bye": 8},
    ]
    marginal_world(specs, retrieved=PULL)
    _sched(db, game_id="G_BUF", week=WEEK, home="BUF", away="NYJ", gameday="2026-09-21")
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    assert "Ghost D/ST" not in [r.player for r in board.ranked]
    assert any("Ghost D/ST" in n for n in board.notes)


# ------------------------------------------------------------- position guard


def test_a_non_streamed_position_is_refused(db):
    with pytest.raises(StreamPositionError):
        rank_streamers(db, as_of=PULL, season=SEASON, position="QB", week=WEEK)


# ------------------------------------------------------- week resolution (None)


def test_week_none_resolves_the_current_week_not_a_finished_one(db, marginal_world):
    """When week is None the current week is resolved via marginal.resolve_weeks —
    the first NOT-yet-finished week, never a played one."""
    _basic_world(marginal_world, db)
    # wk1 last gameday 09-10 (finished at as_of 09-15), wk3 game is 09-21 (unplayed)
    _sched(db, game_id="G_W2", week=2, home="MIA", away="NYG", gameday="2026-09-14")
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=None)
    assert board.week == 3


# --------------------------------------------------------------- (g) formatting


def test_format_stream_board_smoke_and_degradation_banner(db, marginal_world):
    """(g) the renderer prints a shelf and leads with a DEGRADED banner when the
    Vegas/weather context it would have used is absent."""
    _basic_world(marginal_world, db)
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    text = format_stream_board(board, reasons=True)
    assert "streaming D/ST" in text
    assert "Bay D/ST" in text
    assert "HYPOTHESIS" in text
    # no odds and no weather were available -> the degradation banner leads
    assert "DEGRADED" in text
    assert board.odds_available is False and board.weather_available is False


def test_format_names_the_opponent_and_ships_reasons(db, marginal_world):
    _basic_world(marginal_world, db)
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    dst = next(r for r in board.ranked if r.team == "MIA")
    assert dst.opponent == "NYG"                       # resolved from the schedule
    assert dst.reasons                                 # Rule 6: every row explains itself
    # opponent-quality reason names the opponent's projected offense (22 = 12 + 10)
    assert any("NYG" in r and "22.0 house pts" in r for r in dst.reasons)


# ----------------------------------------------------- adjustment-model units


def test_kicker_weather_multiplier_is_bounded_and_monotonic():
    adj = DEFAULT_STREAM_ADJUST
    calm, _ = adj.kicker_weather_multiplier(5.0, 0.0)
    mild, _ = adj.kicker_weather_multiplier(17.0, 0.0)
    hard, _ = adj.kicker_weather_multiplier(30.0, 0.0)
    assert calm == 1.0
    assert calm > mild > hard >= adj.k_weather_floor


def test_opponent_multiplier_tilts_up_for_a_weak_offense():
    adj = DEFAULT_STREAM_ADJUST
    reference = [20.0, 22.0, 24.0, 26.0]
    weak, _ = adj.opponent_multiplier(8.0, reference, "WEAK")
    strong, _ = adj.opponent_multiplier(40.0, reference, "STRONG")
    assert weak > 1.0 > strong


# ------------------------------------ CODE FIX 1: bye teams pollute the reference


def test_a_bye_team_is_excluded_from_the_opponent_quality_reference(db, marginal_world):
    """CODE FIX 1: a team whose whole offense is on BYE this week lands in the
    offense map at 0.0 but is NOT playing — it must be excluded from the
    opponent-quality reference AND from the printed 'league average', or the 0.0
    outlier inflates the spread and prints a wrong average."""
    specs = [
        {"name": "Stream D", "pos": "D/ST", "team": "MIA", "pts": 5.0, "bye": 8},
        {"name": "Opp Off", "pos": "RB", "team": "NYG", "pts": 20.0, "bye": 7},   # MIA's opponent
        {"name": "Ref A", "pos": "RB", "team": "AAA", "pts": 10.0, "bye": 6},
        {"name": "Ref B", "pos": "RB", "team": "BBB", "pts": 20.0, "bye": 6},
        {"name": "Ref C", "pos": "RB", "team": "CCC", "pts": 30.0, "bye": 6},
        # ON BYE this week: present in the offense map at 0.0, but absent from the
        # schedule (not playing) -> must be excluded from the reference.
        {"name": "Bye Off", "pos": "RB", "team": "ZZZ", "pts": 15.0, "bye": WEEK},
    ]
    marginal_world(specs, retrieved=PULL)
    _sched(db, game_id="G_MIA", week=WEEK, home="MIA", away="NYG", gameday="2026-09-21")
    _sched(db, game_id="G_AAA", week=WEEK, home="AAA", away="XA", gameday="2026-09-21")
    _sched(db, game_id="G_BBB", week=WEEK, home="BBB", away="XB", gameday="2026-09-21")
    _sched(db, game_id="G_CCC", week=WEEK, home="CCC", away="XC", gameday="2026-09-21")
    # NOTE: ZZZ has NO schedule row this week (it is on bye).
    db.commit()

    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    d = next(r for r in board.ranked if r.team == "MIA")
    blob = " ".join(d.reasons)
    # playing-only reference {AAA:10, BBB:20, CCC:30} -> mean 20.0
    assert "league average of 20.0" in blob
    # NOT the polluted mean including ZZZ's 0.0 (that would be 15.0)
    assert "league average of 15.0" not in blob


# ------------------------------------------- TEST FIX 4: Vegas home/away sign


def test_vegas_multiplier_direction_and_bound():
    """(a) a lower opponent implied total tilts UP (>1.0), a higher one tilts DOWN
    (<1.0), and the tilt is bounded by +/- vegas_tilt."""
    adj = DEFAULT_STREAM_ADJUST
    low, _ = adj.vegas_multiplier(adj.vegas_pivot - 8.0, "OPP")
    high, _ = adj.vegas_multiplier(adj.vegas_pivot + 8.0, "OPP")
    assert low > 1.0 > high
    assert low <= 1.0 + adj.vegas_tilt + 1e-9
    assert high >= 1.0 - adj.vegas_tilt - 1e-9


def test_opponent_implied_respects_home_away_sign():
    """(b) spread_line is home-oriented (positive = home favored). The streamed
    team's OPPONENT implied total is (total-spread)/2 when the streamed team is
    HOME (opponent away) and (total+spread)/2 when it is AWAY (opponent home).
    Inverting the branch flips these two and fails the test."""
    odds = {"total_line": 44.0, "spread_line": 6.0}       # home favored by 6
    assert streaming._opponent_implied(odds, is_home=True) == pytest.approx((44.0 - 6.0) / 2)
    assert streaming._opponent_implied(odds, is_home=False) == pytest.approx((44.0 + 6.0) / 2)


def test_a_low_implied_opponent_lifts_a_dst_stream_score(db, marginal_world):
    """(c) board-level: a streamed D/ST facing a LOW-implied opponent scores HIGHER
    with the line than without it. If the home/away sign were inverted the same line
    would imply a HIGH opponent total and LOWER the score — this fails then."""
    _basic_world(marginal_world, db)
    # MIA is HOME vs NYG; a big MIA favorite makes NYG's implied total LOW.
    _odds(db, game_id="G_MIA", home="MIA", away="NYG", total=40.0, spread=20.0,
          knowable="2026-09-20")
    db.commit()
    no_odds = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    with_odds = rank_streamers(db, as_of="2026-09-21", season=SEASON, position="DST", week=WEEK)
    assert no_odds.odds_available is False
    assert with_odds.odds_available is True
    mia_no = next(r for r in no_odds.ranked if r.team == "MIA")
    mia_yes = next(r for r in with_odds.ranked if r.team == "MIA")
    assert mia_yes.house_points == mia_no.house_points     # Rule 2: house pts untouched
    assert mia_yes.stream_score > mia_no.stream_score


# ---------------------- CODE FIX 3: all-dome slate must NOT read as DEGRADED


def test_an_all_dome_kicker_slate_is_not_degraded(db, marginal_world):
    """CODE FIX 3: when every candidate game is a dome (weather fully KNOWN but
    correctly inapplicable), the board is NOT degraded — weather_readable is True,
    the banner is suppressed, and a distinct indoor note is shown instead."""
    _k_pair_world(marginal_world, db)
    dome_both = [_weather_row("G_KC", relevant=0, home="KC"),
                 _weather_row("G_DET", relevant=0, home="DET")]
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="K", week=WEEK,
                           weather=dome_both)
    assert board.weather_readable is True
    assert board.weather_available is False                # nothing applied
    text = format_stream_board(board)
    assert "DEGRADED" not in text
    assert any("indoor" in n for n in board.notes)


def test_a_genuinely_absent_weather_feed_still_degrades(db, marginal_world):
    """CODE FIX 3 (the other side): a truly missing weather feed IS a data gap and
    still raises DEGRADED — the flag distinguishes 'known & inapplicable' from
    'unknown'."""
    _k_pair_world(marginal_world, db)
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="K", week=WEEK)
    assert board.weather_readable is False
    assert "DEGRADED" in format_stream_board(board)


# ------------------------------- TEST FIX 8: secondary D/ST bad-weather bump


def test_secondary_dst_weather_bump_in_bad_weather(db, marginal_world):
    """TEST FIX 8: a D/ST in a genuinely windy game gets its stream_score bumped by
    dst_weather_bump, the reason names the wind, and weather_available is True. A
    calm game gets NO bump (score unchanged, weather not 'applied')."""
    _basic_world(marginal_world, db)
    windy = [_weather_row("G_MIA", wind=28.0, precip=0.0, relevant=1, home="MIA")]
    calm = [_weather_row("G_MIA", wind=5.0, precip=0.0, relevant=1, home="MIA")]
    b_windy = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK, weather=windy)
    b_calm = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK, weather=calm)
    mia_windy = next(r for r in b_windy.ranked if r.team == "MIA")
    mia_calm = next(r for r in b_calm.ranked if r.team == "MIA")
    bump = DEFAULT_STREAM_ADJUST.dst_weather_bump
    assert mia_windy.stream_score == pytest.approx(mia_calm.stream_score * (1.0 + bump))
    assert b_windy.weather_available is True
    assert any("28 mph" in r for r in mia_windy.reasons)
    # calm / sub-threshold: no bump, score is just the (untilted) house projection
    assert b_calm.weather_available is False
    assert mia_calm.stream_score == pytest.approx(mia_calm.house_points)


def test_dst_weather_multiplier_none_gate_and_bump():
    """TEST FIX 8 (unit): dst_weather_multiplier returns None below the wind-steep
    threshold and a +dst_weather_bump multiplier above it, naming the wind."""
    adj = DEFAULT_STREAM_ADJUST
    assert adj.dst_weather_multiplier(5.0, 0.0) is None            # calm -> gated off
    got = adj.dst_weather_multiplier(28.0, 0.0)
    assert got is not None
    mult, reason = got
    assert mult == pytest.approx(1.0 + adj.dst_weather_bump)
    assert "28 mph" in reason


# ============================ item 3.14 — the weekly market board =============
#
# Step 1 swaps the D/ST RANKER: the already-captured FantasyPros weekly D/ST
# consensus (`fp_weekly_ecr`, page 'dst') orders the page when a board for THIS
# week exists, and the opponent-quality composite becomes the no-board fallback.
# Everything below is synthetic (Rule 5); the numbers that justify the swap are
# measured and live in `streaming.MARKET_LABEL`.


def _fp_dst(db, *, team, rank, ecr, week=WEEK, scrape=PULL, retrieved=None,
            season=SEASON, label=None):
    """One row of the weekly D/ST consensus board, shaped like migration 016's."""
    db.execute(
        "INSERT INTO fp_weekly_ecr (fantasypros_id, page, scrape_date, season, "
        "nfl_week, week_basis, player, position, team, rank, ecr, pos_rank_label, "
        "retrieved_as_of, knowable_as_of) VALUES "
        "(?, 'dst', ?, ?, ?, 'fantasypros_page', ?, 'DST', ?, ?, ?, ?, ?, ?)",
        (f"FP-{team}-{scrape}", scrape, season, week, f"{team} Defense", team,
         rank, ecr, label or f"DST{rank}", retrieved or scrape, scrape),
    )


def _two_dst_world(marginal_world, db):
    """Two free-agent D/STs whose HOUSE projections (and therefore the composite)
    rank MIA over PIT, plus the offenses that make the opponent tilt non-trivial."""
    specs = [
        {"name": "Bay D/ST", "pos": "D/ST", "team": "MIA", "pts": 9.0, "bye": 8},
        {"name": "Steel D/ST", "pos": "D/ST", "team": "PIT", "pts": 5.0, "bye": 9},
        {"name": "Giant Runner", "pos": "RB", "team": "NYG", "pts": 12.0, "bye": 7},
        {"name": "Giant Catcher", "pos": "WR", "team": "NYG", "pts": 10.0, "bye": 7},
        {"name": "Charge Runner", "pos": "RB", "team": "LAC", "pts": 11.0, "bye": 7},
        {"name": "Ref Runner B", "pos": "RB", "team": "TB", "pts": 18.0, "bye": 9},
        {"name": "Ref Catcher C", "pos": "WR", "team": "SEA", "pts": 14.0, "bye": 9},
    ]
    marginal_world(specs, retrieved=PULL)
    _sched(db, game_id="G_MIA", week=WEEK, home="MIA", away="NYG", gameday="2026-09-21")
    _sched(db, game_id="G_PIT", week=WEEK, home="PIT", away="LAC", gameday="2026-09-21")
    _sched(db, game_id="G_TB", week=WEEK, home="TB", away="SEA", gameday="2026-09-21")
    db.commit()


def test_the_weekly_market_board_sets_the_dst_order(db, marginal_world):
    """DONE-WHEN (a). The board RANKS the page, and the teeth are that it ranks it
    AGAINST the composite: PIT is ranked first on a lower stream_score and a lower
    house projection than MIA, which can only come from the market board."""
    _two_dst_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=1, ecr=2.10)
    _fp_dst(db, team="MIA", rank=9, ecr=11.40)
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    assert board.ranked_on_market is True
    assert [r.team for r in board.ranked] == ["PIT", "MIA"]
    top, second = board.ranked
    # the two rankers genuinely DISAGREE here — without that this test would pass
    # on a board that changed nothing.
    assert top.stream_score < second.stream_score
    assert top.house_points < second.house_points
    assert (top.market_rank, top.market_label) == (1, "DST1")


def test_with_no_weekly_board_the_order_is_the_pre_314_composite(db, marginal_world):
    """The no-board FALLBACK is the OLD ranker, unchanged — not a re-implementation
    of it. With no board stored, the order is stream_score-descending and the page
    says which ranker it used and why."""
    _two_dst_world(marginal_world, db)
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    assert board.ranked_on_market is False
    assert board.market is None
    assert [r.team for r in board.ranked] == ["MIA", "PIT"]
    assert [r.stream_score for r in board.ranked] == sorted(
        [r.stream_score for r in board.ranked], reverse=True)
    assert all(r.market_ecr is None for r in board.ranked)
    text = format_stream_board(board)
    assert "FALLBACK RANKER" in text
    assert "No D/ST board of any week is stored" in text


def test_a_board_scraped_after_the_as_of_cannot_rank_the_page(db, marginal_world):
    """RULE 1 / LEAKAGE. `knowable_as_of = scrape_date`, so a board scraped AFTER
    the decision is invisible — this is exactly why item 3.14 needed no fence
    moved. The SAME rows appear once the as-of reaches the scrape."""
    _two_dst_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=1, ecr=2.10, scrape="2026-09-17")
    _fp_dst(db, team="MIA", rank=9, ecr=11.40, scrape="2026-09-17")
    db.commit()
    early = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    assert early.market is None
    assert [r.team for r in early.ranked] == ["MIA", "PIT"]      # composite order
    later = rank_streamers(db, as_of="2026-09-17", season=SEASON, position="DST",
                           week=WEEK)
    assert later.market is not None and later.market.scrape_date == "2026-09-17"
    assert [r.team for r in later.ranked] == ["PIT", "MIA"]


def test_the_newest_scrape_of_the_week_is_the_board(db, marginal_world):
    """Upstream rewrites this file twice a day and the ranks MOVE (81 of 159 ppr-rb
    ids in one 5.7-hour window, measured 2026-09-04). Two scrapes of the same week
    are two versions of one board: the FRESHEST one <= as_of wins, and the stale
    one must not leak a rank into the order."""
    _two_dst_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=9, ecr=11.4, scrape="2026-09-14")
    _fp_dst(db, team="MIA", rank=1, ecr=2.1, scrape="2026-09-14")
    _fp_dst(db, team="PIT", rank=1, ecr=2.10, scrape=PULL)
    _fp_dst(db, team="MIA", rank=9, ecr=11.40, scrape=PULL)
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    assert board.market.scrape_date == PULL
    assert [r.team for r in board.ranked] == ["PIT", "MIA"]


def test_a_board_for_another_week_is_never_served_for_this_one(db, marginal_world):
    """The whole point of the item: a week-1 board ranking week-3 defenses is a
    season-long list wearing a weekly label. It falls back, and it NAMES what is
    stored so the operator can tell 'no capture' from 'not this week's'."""
    _two_dst_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=1, ecr=2.10, week=1, scrape="2026-09-14")
    _fp_dst(db, team="MIA", rank=9, ecr=11.40, week=1, scrape="2026-09-14")
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    assert board.market is None
    assert board.market_alt is not None and board.market_alt.week == 1
    assert [r.team for r in board.ranked] == ["MIA", "PIT"]
    text = format_stream_board(board)
    assert "which ranks week 1" in text
    assert "it is NOT served for week 3" in text


def test_a_candidate_absent_from_the_board_ranks_below_every_board_row(db, marginal_world):
    """An absence of a market opinion is not a low one — but it is also not a
    reason to rank him on a different ruler than everyone else, so he sits below
    the boarded rows and the page says he is missing rather than bad."""
    _two_dst_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=20, ecr=24.0)      # boarded, and rated badly
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    assert [r.team for r in board.ranked] == ["PIT", "MIA"]
    mia = next(r for r in board.ranked if r.team == "MIA")
    assert mia.market_ecr is None
    assert any("is NOT on the week-3 FantasyPros D/ST board" in r for r in mia.reasons)
    assert any("absent from that board" in n for n in board.notes)


def test_every_boarded_row_quotes_the_rho_the_cohort_and_the_paired_margin(db, marginal_world):
    """RULE 6. A ranker swapped in on a measurement must carry the measurement —
    INCLUDING the half that does not flatter it (the head-to-head against the
    composite it replaces is indistinguishable, and the row says so)."""
    _two_dst_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=1, ecr=2.10)
    _fp_dst(db, team="MIA", rank=9, ecr=11.40)
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    blob = " ".join(r for rec in board.ranked if rec.market_ecr is not None
                    for r in rec.reasons)
    for token in ("+0.2701", "+0.1300", "826 rows", "n=38 paired weeks",
                  "CROSSES ZERO", "INDISTINGUISHABLE", "probe E08c"):
        assert token in blob, token
    # and it never promises the market will agree with us later (item 4.1).
    for banned in ("will confirm", "will agree", "the market will"):
        assert banned not in blob.lower()


def test_the_three_mandatory_card_sentences_are_on_the_dst_card(db, marginal_world):
    """DONE-WHEN (d). All three, above the table, every time — and NOT on the
    kicker card, which this item did not measure and did not change."""
    _two_dst_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=1, ecr=2.10)
    db.commit()
    text = format_stream_board(
        rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK))
    for sentence in streaming.DST_CARD_SENTENCES:
        assert sentence in text
    assert len(streaming.DST_CARD_SENTENCES) == 3
    assert "THE ALTERNATIVE IS HOLDING" in text and "CROSSES ZERO" in text
    assert "8 WEEKS OUT OF 10" in text and "38 of 45" in text
    assert "TABLE STAKES" in text and "0 to +0.4 pts/wk" in text
    # the posture disclosure rides with them, keyed on posture and never a "never"
    assert "-0.4614" in text and "never a" in text
    k_text = format_stream_board(
        rank_streamers(db, as_of=PULL, season=SEASON, position="K", week=WEEK))
    assert "THE ALTERNATIVE IS HOLDING" not in k_text


def test_the_kicker_lane_never_reads_the_market_board(db, marginal_world):
    """The rho and the paired margin were measured on the D/ST page. A kicker board
    exists upstream and is captured; using it here would be swapping a ranker on
    another position's evidence."""
    _basic_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=1, ecr=2.10)
    db.execute(
        "INSERT INTO fp_weekly_ecr (fantasypros_id, page, scrape_date, season, "
        "nfl_week, week_basis, player, position, team, rank, ecr, retrieved_as_of, "
        "knowable_as_of) VALUES ('FPK', 'k', ?, ?, ?, 'fantasypros_page', "
        "'Boot Leg', 'K', 'KC', 1, 1.0, ?, ?)", (PULL, SEASON, WEEK, PULL, PULL))
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="K", week=WEEK)
    assert board.market is None and board.market_alt is None
    assert all(r.market_ecr is None for r in board.ranked)
    assert streaming.MARKET_PAGE == "dst"


# ------------------- item 3.17 deliverable 3 (stream half): the age banner -----


def _age_one_projection(db, *, team, position, retrieved):
    """Re-stamp one player's projection rows as an OLD pull, the way a source that
    stopped refreshing leaves an orphan row behind. Keyed on (team, position)
    because `projections` has no display-name column — a D/ST row's
    `source_player_id` IS the team abbr (the 003 contract)."""
    db.execute(
        "UPDATE projections SET retrieved_as_of = ? WHERE team = ? AND position = ?",
        (retrieved, team, position))
    db.commit()


def test_the_projection_age_banner_counts_and_clears_an_unranked_orphan(db, marginal_world):
    """ITEM 3.17 (3). The shipped banner read the OLDEST pull anywhere on the board
    and said 'some projections on this board are N days old' — on the live
    2026-09-15 board that was ONE orphan row of 3,229, and it rendered as a blanket
    'do not trust this page'. It must COUNT, and it must say whether a RANKED row
    is among them: a stale row nobody is ranking does not move an order."""
    _two_dst_world(marginal_world, db)
    _age_one_projection(db, team="TB", position="RB", retrieved="2026-08-20")
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    banner = " ".join(board.freshness)
    assert "1 of " in banner and "projection rows on this board" in banner
    assert "NO ranked candidate on this page is among them" in banner
    assert "not a reason to distrust the rank" in banner
    # the old, uncountable wording is gone for good
    assert "some projections on this board" not in banner


def test_the_projection_age_banner_names_a_stale_RANKED_candidate(db, marginal_world):
    """The other branch, and the one that matters: when the stale row IS on the
    page, the banner says the rank is affected and names him."""
    _two_dst_world(marginal_world, db)
    _age_one_projection(db, team="PIT", position="DEF", retrieved="2026-08-20")
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    banner = " ".join(board.freshness)
    assert "IS a ranked candidate on this page (Steel D/ST)" in banner
    assert "this rank IS affected" in banner
    assert "NO ranked candidate" not in banner


def test_the_mkt_column_shows_the_CONSENSUS_and_is_monotone_down_the_page(db, marginal_world):
    """Measured on the live 2026-09-14 board: upstream's own integer label and its
    consensus average DISAGREE (TEN is labelled DST11 at ecr 10.89, DET DST10 at
    11.05). The consensus is what orders the page and what the rho was measured on,
    so the column must show THAT — a column that is not monotone down its own
    ordering is a column the reader stops believing — and the page must say the
    printed DSTn label can sit a place away from it."""
    _two_dst_world(marginal_world, db)
    _fp_dst(db, team="PIT", rank=11, ecr=10.89, label="DST11")
    _fp_dst(db, team="MIA", rank=10, ecr=11.05, label="DST10")
    db.commit()
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=WEEK)
    assert [r.team for r in board.ranked] == ["PIT", "MIA"]     # ecr, not rank
    text = format_stream_board(board)
    assert "10.89" in text and "11.05" in text
    assert "DST11" not in text.split("WHAT THIS RANKING IS")[0]
    assert "CONSENSUS, lower is better" in text
    assert "a place or two away from upstream's own printed DSTn label" in text
    # the label is not lost — it rides in the row's own reasons
    assert "DST11" in " ".join(board.ranked[0].reasons)


def test_the_stream_card_discloses_that_it_prices_ONE_horizon(db, marginal_world):
    """DONE-WHEN (b), the half this page cannot meet and must not hide. `ziggurat
    waivers` prints both horizons because it holds the roster and the priced swap
    matrix; this page holds neither, and building them would turn a ~4 s quick scan
    into a ~24 s one. So the gap is stated, with the measured magnitude and the
    command that closes it — never left as an unqualified upside."""
    _basic_world(marginal_world, db)
    for pos in ("DST", "K"):
        board = rank_streamers(db, as_of=PULL, season=SEASON, position=pos, week=WEEK)
        assert board.ranked, pos
        text = format_stream_board(board)
        assert "ONE HORIZON ONLY" in text, pos
        assert "ziggurat waivers" in text, pos
    assert "-23.1" in streaming.ONE_HORIZON_NOTE
    assert "86% break-even" in streaming.ONE_HORIZON_NOTE


def test_the_one_horizon_note_is_silent_on_an_empty_board(db, marginal_world):
    """Nothing was shown, so there is no upside to qualify. An explanation attached
    to zero rows is the noise that teaches an operator to skim the header."""
    _basic_world(marginal_world, db)
    board = rank_streamers(db, as_of=PULL, season=SEASON, position="DST", week=99)
    assert board.ranked == ()
    assert "ONE HORIZON ONLY" not in format_stream_board(board)
