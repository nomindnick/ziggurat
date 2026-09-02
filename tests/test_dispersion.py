"""Dispersion module tests — per-player floor/ceiling + measured weekly sigma.

Offline throughout: the shared ``marginal_world`` projection universe plus
hand-inserted ``adp_rankings`` rows. Every player, team and id is invented
(Rule 5 — the live board carries real colleagues' league state and never enters a
committed file), and the SHAPE is the live one: a bye row is present with a NULL
opponent and NULL stats, a D/ST row carries a negative ESPN id and a NULL gsis so
it can only be joined by team abbreviation.

What these tests are FOR, in the order the module can hurt someone:

  * a silent fallback. A per-player market band and a positional average must
    never be indistinguishable in the output, so several tests are properties
    over the whole board rather than assertions about one row.
  * a clamped rank. Clamping an out-of-range rank into the curve collapses the
    band to zero, which reads as CERTAINTY about the player we know least;
    ``test_a_rank_past_the_curve_is_refused_not_clamped`` fails if anyone
    "helpfully" clamps it.
  * a mixed board. ``get_adp_rankings`` returns the whole weekly panel, and
    taking each player's newest row silently blends a five-week-old rank with
    today's; ``test_the_board_is_pinned_to_one_scrape`` fails on exactly that.
  * a leaked scrape / a leaked retrieval.
  * the two sigmas drifting apart with nobody noticing.

A SECOND BLOCK OF TESTS LIVES AT THE BOTTOM OF THIS FILE, added after three
adversarial reviews of the first cut. They exist because the tests above were
green while the module was wrong, so each one was written by MUTATING the
shipped code and confirming the test failed. The mutations they hold down:
hanging the points band on the locally-derived ``pos_rank`` instead of the
panel's ``ecr``; ``band / 2`` or ``band / 8`` instead of ``band / 4``; pricing an
unranked player at a zero-width band; deleting the D/ST-by-team join; ignoring
``position`` in the market join; deleting the name join or its disclosure;
quoting the tier cross-check at the player's mean under the tier's label; the
D/ST tiers drifting back to their REG+POST values; hard-coding "full-PPR
redraft" into every row's provenance; disabling either staleness banner;
substituting the frozen reference band under the ``cohort_median`` label;
swapping the two coverage columns; ``spread_over_4`` over 2; hard-coding
``latest_truth`` inside ``draftable_coverage``; dropping its gsis rung; taking
the market band from the CLAMPED floor; and dropping the clamp disclosure.
"""

import ast
import math
from pathlib import Path

import pytest

from ziggurat.core import dispersion, scoring
from ziggurat.core.dispersion import (
    DEFAULT_ECR_TYPE,
    DEFAULT_FLOORS,
    DEFAULT_TIER_SIGMA,
    ECR_PAGES,
    LEGACY_POSITIONAL_DISPERSION_PRIOR,
    POSITIONAL_BAND_PRIOR,
    RANK_UNITS,
    REDRAFT_PPR_ECR_TYPES,
    TIER_VS_AFFINE_MAX_DRIFT,
    Floors,
    PointsCurve,
    build_dispersion,
    market_rank_dispersion,
    priceable_lines,
    season_sigma,
    weekly_sigma,
)
from ziggurat.core.lineup_support import DEFAULT_VARIANCE
from ziggurat.data.nfl import base, refresh

SEASON = 2026
PULL = "2026-08-28"
AS_OF = "2026-08-30"
SCRAPE = "2026-08-25"

# Small enough to build by hand, big enough to clear the curve-depth floor.
TEST_FLOORS = Floors(board_rows=10, position_rows=4, curve_depth=8, reference_rows=4)


# --------------------------------------------------------------------- builders


def _ladder(n, *, pos="RB", top=20.0, step=1.0, team_prefix="T"):
    """``n`` players at one position on a strictly descending points ladder, each
    on his own NFL team so the projections-derived bye map resolves cleanly."""
    return [
        {"name": f"{pos} Ladder {i:02d}", "pos": pos, "team": f"{team_prefix}{i:02d}",
         "pts": top - i * step, "bye": 6}
        for i in range(n)
    ]


def _adp(db, *, fantasypros_id, position, ecr, best, worst, pos_rank,
         gsis_id=None, espn_id=None, team=None, sd=None, ecr_type=DEFAULT_ECR_TYPE,
         scrape=SCRAPE, retrieved=PULL, knowable=None, season=SEASON):
    db.execute(
        "INSERT INTO adp_rankings (fantasypros_id, gsis_id, espn_id, player, position, "
        "team, ecr_type, ecr, sd, best, worst, pos_rank, season, scrape_date, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(fantasypros_id), gsis_id, espn_id, f"FP {fantasypros_id}", position, team,
         ecr_type, ecr, sd if sd is not None else (worst - best) / 4.0, best, worst,
         pos_rank, season, scrape, retrieved, knowable or scrape),
    )


def _rank_ladder(db, specs, *, pos="RB", spread=2, skip=(), **kw):
    """One market row per ladder player, ranked in the same order the house board
    ranks them, with a symmetric ``spread`` band around each rank.

    Indices run over the FULL ``specs`` list because ``marginal_world`` assigns
    ``gsis``/``espn`` ids by that list's enumeration; ``skip`` drops a player from
    the market board without shifting anybody else's identity."""
    for i, spec in enumerate(specs, start=1):
        if spec["name"] in skip:
            continue
        _adp(db, fantasypros_id=f"{pos}{i:03d}", position=pos,
             gsis_id=f"00-1{i - 1:05d}", espn_id=str(2000 + i - 1),
             ecr=float(i), best=max(1, i - spread), worst=i + spread, pos_rank=i, **kw)


def _world(marginal_world, db, specs, *, spread=2, market=True, **kw):
    marginal_world(specs, retrieved=PULL)
    if market:
        _rank_ladder(db, specs, spread=spread, **kw)
    db.commit()
    return specs


def _board(db, **kw):
    kw.setdefault("floors", TEST_FLOORS)
    return build_dispersion(db, as_of=AS_OF, season=SEASON, **kw)


def _row(board, name):
    return next(r for r in board.rows.values() if r.player == name)


# ------------------------------------------------------------- Rule 1 (as-of)


def test_the_accessors_refuse_an_implicit_now(db):
    with pytest.raises(TypeError):
        market_rank_dispersion(db, season=SEASON)
    with pytest.raises(TypeError):
        build_dispersion(db, season=SEASON)
    with pytest.raises(TypeError):
        priceable_lines(db, season=SEASON)


def test_a_scrape_published_after_as_of_is_invisible(db, marginal_world):
    """The newest scrape wins, but only among the scrapes ``as_of`` can see."""
    specs = _ladder(10)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, scrape="2026-08-10", retrieved="2026-08-10")
    _rank_ladder(db, specs, scrape="2026-08-24", retrieved="2026-08-24")
    db.commit()

    late = market_rank_dispersion(db, as_of="2026-08-30", season=SEASON, floors=TEST_FLOORS)
    assert late.scrape_date == "2026-08-24"

    early = market_rank_dispersion(db, as_of="2026-08-20", season=SEASON, floors=TEST_FLOORS)
    assert early.scrape_date == "2026-08-10"
    assert early.rows, "the older scrape must still be readable"


def test_a_bulk_loaded_row_is_hidden_by_historical_and_shown_by_latest_truth(db, marginal_world):
    """The retrieval gate, not just the knowledge gate. A row whose FACT was
    knowable in the past but whose COPY arrived today must read empty at a past
    as-of under the safe default, and non-empty under the explicit opt-in."""
    specs = _ladder(10)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, scrape="2026-08-01", knowable="2026-08-01", retrieved="2026-08-29")
    db.commit()

    hidden = market_rank_dispersion(db, as_of="2026-08-05", season=SEASON, floors=TEST_FLOORS)
    assert hidden.rows == ()

    shown = market_rank_dispersion(
        db, as_of="2026-08-05", season=SEASON, floors=TEST_FLOORS, view="latest_truth"
    )
    assert len(shown.rows) == len(specs)


def test_the_view_threads_all_the_way_into_the_priced_board(db, marginal_world):
    """Not just into the accessor: a board built through ``latest_truth`` must
    actually carry the bands the ``historical`` board refuses."""
    specs = _ladder(10)
    marginal_world(specs, retrieved="2026-08-29")
    # The fixture stamps knowable = retrieved; separate them so the row is a
    # bulk-loaded PAST fact, which is exactly what latest_truth exists for.
    db.execute("UPDATE projections SET knowable_as_of = '2026-08-01'")
    _rank_ladder(db, specs, scrape="2026-08-01", knowable="2026-08-01", retrieved="2026-08-29")
    db.commit()

    hist = build_dispersion(db, as_of="2026-08-05", season=SEASON, floors=TEST_FLOORS)
    assert hist.rows == {}

    truth = build_dispersion(
        db, as_of="2026-08-05", season=SEASON, floors=TEST_FLOORS, view="latest_truth"
    )
    assert truth.rows
    assert any(r.has_market_band for r in truth.rows.values())


# ------------------------------------------------- one scrape, never a blend


def test_the_board_is_pinned_to_one_scrape(db, marginal_world):
    """A rank board is a JOINT object. Resolving "each player's newest row" would
    keep a player who fell off the board and blend two vintages of ranks; this
    test fails on exactly that."""
    specs = _ladder(10)
    _world(marginal_world, db, specs, market=False)
    # Old scrape: everybody, and the ladder leader is ranked 1.
    _rank_ladder(db, specs, scrape="2026-08-10", retrieved="2026-08-10")
    # New scrape: the leader has FALLEN OFF, and RB Ladder 01 moved 2 -> 1.
    for i, _spec in enumerate(specs[1:], start=1):
        _adp(db, fantasypros_id=f"RB{i + 1:03d}", position="RB",
             gsis_id=f"00-1{i:05d}", espn_id=str(2000 + i),
             ecr=float(i), best=i, worst=i + 3, pos_rank=i,
             scrape="2026-08-24", retrieved="2026-08-24")
    db.commit()

    market = market_rank_dispersion(db, as_of=AS_OF, season=SEASON, floors=TEST_FLOORS)
    assert market.scrape_date == "2026-08-24"
    assert "00-100000" not in market.by_gsis, (
        "a player absent from the resolved scrape must be ABSENT, not carried "
        "forward from an older board"
    )
    assert market.by_gsis["00-100001"].pos_rank == 1, "ranks must come from ONE scrape"
    assert any("older scrape" in n for n in market.notes)


# ---------------------------------------------- populated-table check + fallback


def test_an_empty_market_table_falls_back_and_says_so(db, marginal_world):
    specs = _ladder(12)
    _world(marginal_world, db, specs, market=False)
    board = _board(db)

    assert board.rows
    assert all(r.relative_source == "positional_prior" for r in board.rows.values())
    assert all(r.market_band_points is None for r in board.rows.values())
    assert all(
        r.relative_dispersion == POSITIONAL_BAND_PRIOR["RB"] for r in board.rows.values()
    )
    blob = " ".join(board.banners)
    assert "UNPOPULATED" in blob


def test_a_board_below_the_row_floor_is_treated_as_unpopulated(db, marginal_world):
    """Both sides of the floor, on the same world — a floor no test crosses in
    both directions is a floor nobody has checked."""
    specs = _ladder(12)
    _world(marginal_world, db, specs)

    under = _board(db, floors=Floors(board_rows=13, position_rows=4,
                                     curve_depth=8, reference_rows=4))
    assert not any(r.has_market_band for r in under.rows.values())
    assert any("UNPOPULATED" in b for b in under.banners)

    over = _board(db)
    assert any(r.has_market_band for r in over.rows.values())


def test_a_thin_position_board_is_refused(db, marginal_world):
    specs = _ladder(12)
    _world(marginal_world, db, specs)
    board = _board(db, floors=Floors(board_rows=10, position_rows=13,
                                     curve_depth=8, reference_rows=4))
    assert not any(r.has_market_band for r in board.rows.values())
    assert any("board carries only" in r for row in board.rows.values() for r in row.reasons)


def test_a_shallow_house_curve_is_refused(db, marginal_world):
    specs = _ladder(12)
    _world(marginal_world, db, specs)
    board = _board(db, floors=Floors(board_rows=10, position_rows=4,
                                     curve_depth=13, reference_rows=4))
    assert not any(r.has_market_band for r in board.rows.values())
    assert any("rank-to-points curve needs" in r
               for row in board.rows.values() for r in row.reasons)


def test_the_fallback_is_never_silent(db, marginal_world):
    """A property over the whole board: every row WITHOUT a per-player band names
    the labelled prior it used instead. This is the module's whole point."""
    specs = _ladder(12) + _ladder(6, pos="TE", top=12.0, team_prefix="E")
    _world(marginal_world, db, specs)
    board = _board(db)

    fell_back = [r for r in board.rows.values() if not r.has_market_band]
    assert fell_back, "this world must exercise the fallback"
    for row in fell_back:
        assert row.relative_source == "positional_prior"
        assert any("positional_market_band_prior" in r for r in row.reasons), row.reasons


def test_the_fallback_prior_is_measured_not_the_legacy_proxy():
    """A regression guard on a coherence bug this module was written to avoid.

    Reusing the draft engine's relative-variance vector as the fallback would put
    RB at 0.40 on a scale where the MEASURED median RB band is 1.26 — a fallback
    three times smaller than the per-player number for the same position, i.e. a
    systematic difference between covered and uncovered players with no error
    anywhere. The two vectors must stay different objects with different values.
    """
    assert POSITIONAL_BAND_PRIOR != LEGACY_POSITIONAL_DISPERSION_PRIOR
    assert POSITIONAL_BAND_PRIOR["RB"] > POSITIONAL_BAND_PRIOR["QB"]
    # The legacy vector says the opposite about RB, and zeroes K/DST outright.
    assert LEGACY_POSITIONAL_DISPERSION_PRIOR["RB"] < LEGACY_POSITIONAL_DISPERSION_PRIOR["QB"]
    assert LEGACY_POSITIONAL_DISPERSION_PRIOR["DST"] == 0.0
    assert POSITIONAL_BAND_PRIOR["DST"] > 0.0
    assert set(POSITIONAL_BAND_PRIOR) == {"QB", "RB", "WR", "TE", "K", "DST"}


# ------------------------------------------------------------ units + refusals


def test_overall_rank_units_refuse_the_points_conversion(db, marginal_world):
    """``ro`` is the same full-PPR redraft market, but its best/worst are OVERALL
    ranks across six positions with incomparable point scales. The rank
    dispersion is still reported; the points band is not invented."""
    specs = _ladder(12)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, ecr_type="ro")
    db.commit()

    board = _board(db, ecr_type="ro")
    assert board.rank_units == "overall"
    assert not any(r.has_market_band for r in board.rows.values())
    priced = [r for r in board.rows.values() if r.rank is not None]
    assert priced, "the rank dispersion itself must still be carried"
    assert all(r.rank.sd is not None for r in priced)
    assert any("OVERALL RANK units" in x for r in priced for x in r.reasons)


def test_a_non_redraft_series_is_flagged_as_the_wrong_game(db, marginal_world):
    specs = _ladder(12)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, ecr_type="do")
    db.commit()
    market = market_rank_dispersion(db, as_of=AS_OF, season=SEASON,
                                    ecr_type="do", floors=TEST_FLOORS)
    blob = " ".join(market.notes)
    assert "WARNING" in blob and "dynasty" in blob


def test_a_rank_past_the_curve_is_refused_not_clamped(db, marginal_world):
    """THE clamping test. Our board prices 12 RBs; the market ranks this one
    RB40, best 30, worst 60. Clamping every rank into [1, 12] makes
    ``curve(30) == curve(60)`` and the band collapses to exactly 0.0 — a number
    that reads as certainty about the player we know the least about."""
    specs = _ladder(12)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 00"})
    _adp(db, fantasypros_id="RB999", position="RB", gsis_id="00-100000",
         espn_id="2000", ecr=40.0, best=30, worst=60, pos_rank=40)
    db.commit()

    board = _board(db)
    deep = _row(board, "RB Ladder 00")
    assert deep.rank is not None and deep.rank.pos_rank == 40
    assert deep.market_band_points is None, "a clamped band would be 0.0, not None"
    assert deep.relative_source == "positional_prior"
    assert any("OUTSIDE the house board" in r for r in deep.reasons)
    assert any("Refusing to extrapolate" in r for r in deep.reasons)


def test_points_curve_refuses_to_extrapolate_and_interpolates_exactly():
    curve = PointsCurve(position="RB", points=(300.0, 200.0, 100.0))
    assert curve.at(1) == 300.0
    assert curve.at(3) == 100.0
    assert curve.at(1.5) == 250.0                # exact linear interpolation
    assert curve.at(2.25) == pytest.approx(175.0)
    for bad in (0, 0.99, 3.01, 4, -1, None):
        with pytest.raises(ValueError):
            curve.at(bad)
    # monotone non-increasing, by construction
    assert all(a >= b for a, b in zip(curve.points, curve.points[1:], strict=False))


# ------------------------------------------------------ the conversion itself


def test_a_unanimous_panel_gives_a_zero_width_band(db, marginal_world):
    """best == pos_rank == worst means the panel agrees exactly, and the band
    must collapse to the projection itself — no phantom spread."""
    specs = _ladder(12)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=0)
    db.commit()

    board = _board(db)
    row = _row(board, "RB Ladder 04")
    assert row.market_band_points == pytest.approx(0.0)
    assert row.market_floor_points == pytest.approx(row.projected_points)
    assert row.market_ceiling_points == pytest.approx(row.projected_points)
    assert row.market_sigma_points == pytest.approx(0.0)


def test_a_wider_expert_spread_gives_a_strictly_wider_band(db, marginal_world):
    """The load-bearing behaviour: the SPREAD comes from the market. Two players
    on the same curve, one with a four-rank band and one with an eight-rank
    band — the second must price strictly wider, in points and in the
    normalised relative number."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    for i, spec in enumerate(specs, start=1):
        spread = 4 if spec["name"] == "RB Ladder 06" else 2
        _adp(db, fantasypros_id=f"RB{i:03d}", position="RB",
             gsis_id=f"00-1{i - 1:05d}", espn_id=str(2000 + i - 1),
             ecr=float(i), best=max(1, i - spread), worst=i + spread, pos_rank=i)
    db.commit()

    board = _board(db)
    tight = _row(board, "RB Ladder 05")
    wide = _row(board, "RB Ladder 06")
    assert wide.market_band_points > tight.market_band_points
    assert wide.relative_dispersion > tight.relative_dispersion
    assert wide.season_sigma_points > tight.season_sigma_points


def test_the_level_stays_the_house_projection(db, marginal_world):
    """The band hangs on OUR number, not on the market's. Every priced row's
    market floor/ceiling must bracket its own projection, and the projection must
    be exactly the sum of the scoring.py-priced weeks."""
    specs = _ladder(12)
    _world(marginal_world, db, specs)
    board = _board(db)

    for row in board.rows.values():
        if row.has_market_band:
            assert row.market_floor_points <= row.projected_points + 1e-9
            assert row.market_ceiling_points >= row.projected_points - 1e-9
        assert row.floor_points <= row.projected_points <= row.ceiling_points
        assert row.floor_points >= 0.0


def test_a_floor_never_goes_negative(db, marginal_world):
    """The band is a DIFFERENCE hung on our own level, so a player the market
    ranks far better than we do can have his floor driven below zero. A negative
    season total is not a thing; it is clamped, and the ceiling is untouched."""
    specs = _ladder(12, top=13.0, step=1.0)
    specs.append({"name": "Market Darling", "pos": "RB", "team": "MKT",
                  "pts": 1.0, "bye": 6})
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"Market Darling"})
    # our board ranks him LAST; the market's panel ranks him RB2 (1-13)
    _adp(db, fantasypros_id="RB013", position="RB", gsis_id="00-100012",
         espn_id="2012", ecr=2.0, best=1, worst=13, pos_rank=2)
    db.commit()

    row = _row(_board(db), "Market Darling")
    assert row.board_rank == 13
    assert row.market_floor_points == 0.0
    assert row.market_ceiling_points > row.projected_points
    assert row.floor_points >= 0.0


# ------------------------------------- item 3.2's bye-shaped-row trap


def test_a_no_forecast_player_is_not_priced_and_not_in_the_curve(db, marginal_world):
    """The feed's bye row and its "no forecast" row are byte-identical, so a
    player with one real week sums to a plausible number. COVERAGE decides."""
    specs = _ladder(12)
    specs.append({"name": "Ghost Back", "pos": "RB", "team": "GHO", "pts": 30.0,
                  "bye": 6, "forecast": {1}})
    _world(marginal_world, db, specs, market=False)
    db.commit()

    priced = priceable_lines(db, as_of=AS_OF, season=SEASON)
    assert not any(p.line.player == "Ghost Back" for p in priced.values())

    board = _board(db)
    assert not any(r.player == "Ghost Back" for r in board.rows.values())
    assert board.curves["RB"].depth == 12
    assert board.curves["RB"].points[0] < 30.0 * 16, "the ghost must not head the curve"


def test_played_weeks_not_calendar_weeks_drive_the_rate_and_the_noise(db, marginal_world):
    """A bye means 16 forecast weeks, not 17. Dividing by 17 hands the affine
    sigma model a rate ~6% low and inflates the season-noise term — small, silent,
    and in the same direction for every skill player."""
    specs = _ladder(12, top=16.0, step=1.0)
    _world(marginal_world, db, specs, market=False)
    board = _board(db)
    row = _row(board, "RB Ladder 00")

    priced = priceable_lines(db, as_of=AS_OF, season=SEASON)
    line = next(p for p in priced.values() if p.line.player == "RB Ladder 00")
    assert line.played == 16
    assert row.projected_points == pytest.approx(16.0 * 16)
    assert row.weekly_noise_sigma_points == pytest.approx(row.weekly_sigma * math.sqrt(16))
    assert row.weekly_sigma == pytest.approx(DEFAULT_VARIANCE.sigma("RB", 16.0))


# --------------------------------------------------------- Rule 2 (scoring)


def test_every_point_is_priced_through_scoring_py(db, marginal_world):
    """Rule 2 as a behaviour, not a grep: swing the house PPR rate and the board's
    levels must move. A module that hard-coded a points value would not."""
    specs = [{"name": "PPR Guy", "pos": "WR", "team": "AAA", "pts": 10.0, "bye": 6}]
    specs += _ladder(11, pos="WR", top=9.0, team_prefix="W")
    marginal_world(specs, retrieved=PULL)
    # give the leader real receptions so the PPR rate bites
    db.execute("UPDATE projections SET receptions = 5 WHERE gsis_id = '00-100000' "
               "AND opponent IS NOT NULL")
    db.commit()

    full = _board(db)
    zero = _board(db, rules=scoring.ScoringRules(points_per_reception=0.0))
    assert _row(full, "PPR Guy").projected_points > _row(zero, "PPR Guy").projected_points


# ------------------------------------------------- part (b): weekly sigma


def test_the_tier_table_and_the_affine_model_still_agree():
    """The measurement that justified NOT superseding ``DEFAULT_VARIANCE``.

    Two independently-derived models of the same cohort — one an OLS line on the
    mean (item 3.5), one a non-parametric bucket mean (this module) — evaluated at
    each bucket's own mean. A future re-fit of either that drifts past the frozen
    bound is a FINDING, not a rounding change.
    """
    worst = 0.0
    for pos, tiers in DEFAULT_TIER_SIGMA.tiers.items():
        for tier in tiers:
            affine = DEFAULT_VARIANCE.sigma(pos, tier.mean_mu)
            worst = max(worst, abs(affine - tier.sigma) / tier.sigma)
    assert worst <= TIER_VS_AFFINE_MAX_DRIFT, f"tier vs affine drift {worst:.4f}"
    assert worst > 0.05, (
        "the bound has gone slack — it must stay tight enough to catch a real drift"
    )


def test_inside_the_fitted_domain_the_affine_model_wins():
    """It is mu-sensitive within a tier where a bucket hands twenty ranks one
    number; the tier study only confirms it, it does not replace it."""
    est = weekly_sigma("RB", mu=22.4, rank=1)
    assert est.source == "affine_prior"
    assert est.sigma == pytest.approx(DEFAULT_VARIANCE.sigma("RB", 22.4))
    # and it must differ from the tier's flat reading, or the choice is moot
    assert abs(est.sigma - DEFAULT_TIER_SIGMA.tier_of("RB", 1).sigma) > 1.0
    assert "Cross-checked" in est.reason and "n=100" in est.reason


def test_outside_the_fitted_domain_the_measured_tier_wins():
    lo, hi = DEFAULT_TIER_SIGMA.fitted_domain("WR")
    est = weekly_sigma("WR", mu=hi + 10.0, rank=1)
    assert est.source == "tier_measured"
    assert est.sigma == pytest.approx(DEFAULT_TIER_SIGMA.tier_of("WR", 1).sigma)
    assert "outside" in est.reason
    # and it must NOT be the extrapolated line, which is what makes this matter
    assert est.sigma < DEFAULT_VARIANCE.sigma("WR", hi + 10.0)

    below = weekly_sigma("WR", mu=lo - 5.0, rank=None)
    assert below.source == "tier_measured"
    assert below.sigma == pytest.approx(DEFAULT_TIER_SIGMA.tiers["WR"][-1].sigma)


def test_kicker_sigma_is_labelled_not_yet_fitted_and_reuses_the_old_constant():
    """Item 4.1 audit, KICK-2: the K sigma is still the 3.5 flat hypothesis,
    but the REASON is no longer 'unmeasurable' — migration 013 landed the FG
    columns, so the honest label is 'not yet fitted' naming the follow-up."""
    est = weekly_sigma("K", mu=7.0, rank=1)
    assert est.source == "kicker_flat_hypothesis"
    assert est.sigma == DEFAULT_VARIANCE.k_flat_sigma
    assert "NOT YET FITTED" in est.reason and "migration 013" in est.reason
    assert "UNMEASURABLE" not in est.reason and "has no FG" not in est.reason
    assert "K" not in DEFAULT_TIER_SIGMA.tiers
    # The 3.5 model's own describe() line moved with it.
    form = DEFAULT_VARIANCE.describe("K", 7.0, DEFAULT_VARIANCE.k_flat_sigma)
    assert "not yet fitted" in form and "migration 013" in form
    assert "UNMEASURABLE" not in form


def test_the_item_3_5_variance_model_is_left_working_untouched():
    """"Keep the old constant working" as an assertion, not a promise."""
    assert DEFAULT_VARIANCE.coefficients["RB"] == (2.2529, 0.3950)
    assert DEFAULT_VARIANCE.coefficients["WR"] == (2.1571, 0.4207)
    assert DEFAULT_VARIANCE.k_flat_sigma == 3.5
    assert DEFAULT_VARIANCE.opp_flat_sigma == 17.5
    assert DEFAULT_VARIANCE.correlation_qb_passcatcher == 0.35


def test_weekly_sigma_rises_with_the_projection_for_skill_positions():
    for pos in ("RB", "WR", "TE"):
        lo = weekly_sigma(pos, mu=6.0, rank=50).sigma
        hi = weekly_sigma(pos, mu=18.0, rank=3).sigma
        assert hi > lo, pos


def test_tier_bands_are_contiguous_and_non_overlapping():
    for pos, tiers in DEFAULT_TIER_SIGMA.tiers.items():
        assert tiers[0].lo == 1, pos
        assert tiers[-1].hi is None, pos
        for a, b in zip(tiers, tiers[1:], strict=False):
            assert a.hi is not None and b.lo == a.hi + 1, pos
        for rank in (1, 5, 15, 25, 200):
            assert DEFAULT_TIER_SIGMA.tier_of(pos, rank) is not None, (pos, rank)
        assert DEFAULT_TIER_SIGMA.tier_of(pos, None) is None


def test_season_sigma_composes_in_quadrature():
    assert season_sigma(weekly=3.0, weeks=16, market_sigma=None) == pytest.approx(12.0)
    assert season_sigma(weekly=3.0, weeks=16, market_sigma=5.0) == pytest.approx(13.0)
    assert season_sigma(weekly=0.0, weeks=16, market_sigma=5.0) == pytest.approx(5.0)
    with pytest.raises(ValueError):
        season_sigma(weekly=3.0, weeks=-1)


def test_the_board_composes_the_two_sigmas_the_same_way(db, marginal_world):
    specs = _ladder(12)
    _world(marginal_world, db, specs)
    for row in _board(db).rows.values():
        assert row.season_sigma_points == pytest.approx(
            season_sigma(weekly=row.weekly_sigma, weeks=16,
                         market_sigma=row.market_sigma_points)
        )
        assert row.ceiling_points - row.floor_points <= 2 * row.season_sigma_points + 1e-9


def test_a_row_without_a_market_band_says_its_spread_is_too_narrow(db, marginal_world):
    """With no rate uncertainty priced, the band is noise-only and NARROWER than
    the truth. Saying so is the difference between a caveat and a lie."""
    specs = _ladder(12)
    _world(marginal_world, db, specs, market=False)
    row = next(iter(_board(db).rows.values()))
    assert any("NARROWER than the truth" in r for r in row.reasons)


def test_availability_is_disclosed_as_out_of_scope_on_every_row(db, marginal_world):
    """marginal.py already prices missed games; a consumer that added this band to
    that model would double-count, so every row says where the line is."""
    specs = _ladder(12)
    _world(marginal_world, db, specs)
    for row in _board(db).rows.values():
        assert any("AVAILABILITY IS NOT IN THIS BAND" in r for r in row.reasons)


# ------------------------------------------------------------------ coverage


def test_the_coverage_report_accounts_for_every_row(db, marginal_world):
    specs = _ladder(12) + _ladder(6, pos="TE", top=12.0, team_prefix="E")
    _world(marginal_world, db, specs)
    board = _board(db)

    assert sum(c.total for c in board.coverage) == len(board.rows)
    for cell in board.coverage:
        assert cell.total == (cell.market_band + cell.market_rank_only
                              + cell.positional_prior + cell.no_house_line)
    text = dispersion.format_coverage(board.coverage)
    assert "per-player band" in text and "prior" in text and "unpriced" in text


# -------------------------------------------------------- series identification


def test_the_league_series_is_named_by_the_page_it_was_scraped_from():
    """The evidence behind the default, pinned so an upstream re-labelling is
    caught instead of silently re-pointing the board at a different game."""
    assert REDRAFT_PPR_ECR_TYPES == {"ro", "rp"}
    assert DEFAULT_ECR_TYPE in REDRAFT_PPR_ECR_TYPES
    for series in REDRAFT_PPR_ECR_TYPES:
        assert "ppr-" in ECR_PAGES[series]
        assert "superflex" not in ECR_PAGES[series]
        assert "dynasty" not in ECR_PAGES[series] and "best-ball" not in ECR_PAGES[series]
    # the near-misses, each excluded for a stated reason
    assert "superflex" in ECR_PAGES["rsf"]      # 2-QB; this league starts one
    assert "best-ball" in ECR_PAGES["bo"]       # different roster construction
    assert "dynasty" in ECR_PAGES["do"]         # wrong horizon
    assert ECR_PAGES["drk"].endswith("rookies.php")
    assert set(RANK_UNITS) == set(ECR_PAGES)
    assert RANK_UNITS["rp"] == "positional" and RANK_UNITS["ro"] == "overall"


# ----------------------------------------------------- Rule 8 + determinism


def test_this_module_never_imports_the_quarantined_draft_package():
    """Rule 8, checked here as well as in test_draft_boundary.py: this module has
    a legitimate reason to want the engine's prior and must never take it."""
    src = Path(dispersion.__file__).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("ziggurat.draft") for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("ziggurat.draft")
            assert not ((node.module or "") == "ziggurat"
                        and any(a.name == "draft" for a in node.names))


def test_the_legacy_prior_copy_still_matches_the_engine_it_was_copied_from():
    """While ``ziggurat/draft/`` exists, the reference copy must not drift from
    the original. Skips (rather than fails) in a checkout without the package."""
    engine = pytest.importorskip("ziggurat.draft.engine")
    assert dict(LEGACY_POSITIONAL_DISPERSION_PRIOR) == dict(
        engine.POSITIONAL_DISPERSION_PRIOR
    )


def test_the_board_is_deterministic(db, marginal_world):
    specs = _ladder(12) + _ladder(6, pos="TE", top=12.0, team_prefix="E")
    _world(marginal_world, db, specs)
    a, b = _board(db), _board(db)
    assert list(a.rows) == list(b.rows)
    for key in a.rows:
        assert a.rows[key] == b.rows[key]
    assert a.reference_band == b.reference_band
    assert [r.board_rank for r in a.by_position("RB")] == list(range(1, 13))


def test_the_default_floors_are_the_shipped_constants():
    assert DEFAULT_FLOORS == Floors(
        board_rows=dispersion.MIN_BOARD_ROWS,
        position_rows=dispersion.MIN_POSITION_ROWS,
        curve_depth=dispersion.MIN_CURVE_DEPTH,
        reference_rows=dispersion.MIN_REFERENCE_ROWS,
    )


# ==========================================================================
# The defects three adversarial reviewers found in the first cut of this
# module. Each test below is named for the failure, not for the function, and
# each one FAILS if the fix is reverted — several were written by first
# mutating the shipped code and checking the test caught it.
# ==========================================================================


# ------------------------------------------- the two rank scales (findings 1/7/23)


def _shifted_market_row(db, specs, *, name, ecr, best, worst, pos_rank, pos="RB"):
    """One market row whose DERIVED ``pos_rank`` deliberately disagrees with the
    panel's own ``ecr``/``best``/``worst`` — the live shape (105 of 807 rows on
    the 2026-08-28 board have ``pos_rank`` outside ``[best, worst]``)."""
    i = next(j for j, s in enumerate(specs) if s["name"] == name)
    _adp(db, fantasypros_id=f"SHIFT{i:03d}", position=pos,
         gsis_id=f"00-1{i:05d}", espn_id=str(2000 + i),
         ecr=ecr, best=best, worst=worst, pos_rank=pos_rank)


def test_the_band_hangs_on_the_panel_consensus_not_the_locally_derived_ordinal(
    db, marginal_world
):
    """THE rank-scale test. ``best``/``worst``/``ecr`` are the panel's numbers;
    ``pos_rank`` is a dense ordinal ``adp_rankings._assign_pos_rank`` derives over
    the rows Ziggurat kept. Hanging the band's midpoint on ``pos_rank`` prices two
    scales against each other — measured on the live board it moved published
    floors/ceilings by up to 34 house points and put one player's market FLOOR
    above his own projection.

    Fails if ``_market_band`` goes back to ``curve.at(rank.pos_rank)``.
    """
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 03"})
    # consensus RB4.0, graders 2..6 -- but the derived ordinal says RB11.
    _shifted_market_row(db, specs, name="RB Ladder 03",
                        ecr=4.0, best=2, worst=6, pos_rank=11)
    db.commit()

    board = _board(db)
    row = _row(board, "RB Ladder 03")
    curve = board.curves["RB"]
    assert row.has_market_band

    ecr_anchored_ceiling = row.projected_points + (curve.at(2) - curve.at(4.0))
    ordinal_anchored_ceiling = row.projected_points + (curve.at(2) - curve.at(11))
    assert row.market_ceiling_points == pytest.approx(ecr_anchored_ceiling)
    # the two anchors must actually disagree here, or this test proves nothing
    assert abs(ecr_anchored_ceiling - ordinal_anchored_ceiling) > 1.0
    assert row.market_ceiling_points != pytest.approx(ordinal_anchored_ceiling)

    ecr_anchored_floor = row.projected_points + (curve.at(6) - curve.at(4.0))
    assert row.market_floor_raw_points == pytest.approx(ecr_anchored_floor)


def test_a_consensus_worse_than_every_grader_cannot_bracket_the_projection(
    db, marginal_world
):
    """The live symptom of the scale mix, as an invariant. When the derived
    ordinal sits BELOW the panel's own ``worst`` (105 live rows), anchoring on it
    lifts the whole band and publishes a market FLOOR ABOVE the player's own
    projection — a floor he is guaranteed to clear, which a novice cannot smell.
    """
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 09"})
    # exactly the live shape: pos_rank (13) is WORSE than worst (12).
    _shifted_market_row(db, specs, name="RB Ladder 09",
                        ecr=10.0, best=8, worst=12, pos_rank=13)
    db.commit()

    row = _row(_board(db), "RB Ladder 09")
    assert row.has_market_band
    assert row.market_floor_points <= row.projected_points + 1e-9
    assert row.market_ceiling_points >= row.projected_points - 1e-9


def test_the_level_stays_the_projection_even_when_the_two_scales_disagree(
    db, marginal_world
):
    """``test_the_level_stays_the_house_projection`` cannot catch the scale bug:
    its fixture builds ``best = i - spread, pos_rank = i, worst = i + spread``, so
    the bracket holds by construction. This one drives the ordinal off the panel
    scale in BOTH directions across the whole board and asserts the same property.
    """
    specs = _ladder(16)
    _world(marginal_world, db, specs, market=False)
    for i, _spec in enumerate(specs, start=1):
        # pos_rank drifts up to 4 ranks away from ecr, in alternating directions
        drift = 4 if i % 2 else -4
        _adp(db, fantasypros_id=f"RB{i:03d}", position="RB",
             gsis_id=f"00-1{i - 1:05d}", espn_id=str(2000 + i - 1),
             ecr=float(i), best=max(1, i - 3), worst=min(16, i + 3),
             pos_rank=max(1, min(16, i + drift)))
    db.commit()

    board = _board(db)
    banded = [r for r in board.rows.values() if r.has_market_band]
    assert len(banded) >= 10, "this world must actually price bands"
    for row in banded:
        assert row.market_floor_points <= row.projected_points + 1e-9, row.player
        assert row.market_ceiling_points >= row.projected_points - 1e-9, row.player


def test_a_consensus_outside_its_own_grader_range_is_refused(db, marginal_world):
    """If ``best <= ecr <= worst`` fails, the three numbers on that row are not one
    scale and no honest midpoint exists. Refuse and say so — never average two
    scales into a plausible-looking band."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 05"})
    _shifted_market_row(db, specs, name="RB Ladder 05",
                        ecr=9.0, best=4, worst=7, pos_rank=6)
    db.commit()

    row = _row(_board(db), "RB Ladder 05")
    assert row.rank is not None and row.rank.ecr == 9.0
    assert not row.rank.panel_scale_is_consistent
    assert row.market_band_points is None
    assert row.relative_source == "positional_prior"
    assert any("not one rank scale" in r for r in row.reasons), row.reasons


# --------------------------------------- the /4 divisor (findings 8/17)


def test_the_market_sigma_is_the_band_over_four_derived_from_the_curve(
    db, marginal_world
):
    """``market_sigma = band / 4`` scales every published season sigma, floor and
    ceiling. Re-reading ``row.market_sigma_points`` to check it is self-consistent
    under any divisor, so this derives the band INDEPENDENTLY from the curve and
    the market row, and pins the divisor against /2 and /8."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, spread=3)
    board = _board(db)
    curve = board.curves["RB"]

    row = _row(board, "RB Ladder 06")            # ecr 7, best 4, worst 10
    expected_band = curve.at(4) - curve.at(10)
    assert expected_band > 1.0, "a degenerate band proves nothing"

    assert row.market_band_points == pytest.approx(expected_band)
    assert row.market_sigma_points == pytest.approx(expected_band / 4.0)
    assert row.market_sigma_points != pytest.approx(expected_band / 2.0)
    assert row.market_sigma_points != pytest.approx(expected_band / 8.0)
    assert dispersion.RANGE_TO_SIGMA_DIVISOR == 4.0
    # and the composed season sigma must actually carry it
    assert row.season_sigma_points == pytest.approx(
        season_sigma(weekly=row.weekly_sigma, weeks=16,
                     market_sigma=expected_band / 4.0)
    )


# ------------------------------------- the floor clamp (findings 12/22)


def test_a_clamped_floor_is_disclosed_and_does_not_narrow_the_published_spread(
    db, marginal_world
):
    """The module refuses to clamp a RANK because "clamping collapses the band to
    zero, which reads as certainty about the player we know least". The same
    argument applies one layer down: truncating the FLOOR at 0 and then dividing
    the truncated range into sigma understated the market spread on 40 of 423 live
    rows by up to 43%, with nothing on the row saying the floor was an artefact."""
    specs = _ladder(12, top=13.0, step=1.0)
    specs.append({"name": "Market Darling", "pos": "RB", "team": "MKT",
                  "pts": 1.0, "bye": 6})
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"Market Darling"})
    _adp(db, fantasypros_id="RB013", position="RB", gsis_id="00-100012",
         espn_id="2012", ecr=2.0, best=1, worst=13, pos_rank=2)
    db.commit()

    board = _board(db)
    row = _row(board, "Market Darling")
    curve = board.curves["RB"]

    assert row.market_floor_clamped is True
    assert row.market_floor_points == 0.0
    assert row.market_floor_raw_points < 0.0
    # the SPREAD is untruncated: the whole curve drop from best to worst
    assert row.market_band_points == pytest.approx(curve.at(1) - curve.at(13))
    assert row.market_band_points > row.market_ceiling_points - row.market_floor_points
    assert row.market_sigma_points == pytest.approx(row.market_band_points / 4.0)
    blob = " ".join(row.reasons)
    assert "CLAMPED" in blob and "UNTRUNCATED" in blob

    # and an unclamped row must NOT carry the disclosure or the flag
    clean = _row(board, "RB Ladder 05")
    assert clean.market_floor_clamped is False
    assert clean.market_floor_points == pytest.approx(clean.market_floor_raw_points)
    assert "CLAMPED" not in " ".join(clean.reasons)


# ------------------------------ absent from the board (finding 15)


def test_a_player_absent_from_the_market_board_is_refused_never_priced_at_zero(
    db, marginal_world
):
    """The module's headline guarantee, which had NO test: replacing the refusal
    with a silently-priced zero-width band left all 39 original tests green, and
    every unranked player would then have reported band 0.0 / sigma 0.0 / relative
    0.0 — maximum confidence about the players the market never rated."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 07"})
    db.commit()

    board = _board(db)
    ghost = _row(board, "RB Ladder 07")
    assert ghost.rank is None
    assert ghost.market_join == "none"
    assert ghost.market_band_points is None, "a silently-priced band would be 0.0"
    assert ghost.market_sigma_points is None
    assert ghost.market_floor_points is None and ghost.market_ceiling_points is None
    assert ghost.relative_source == "positional_prior"
    assert ghost.relative_dispersion == POSITIONAL_BAND_PRIOR["RB"]
    assert ghost.relative_dispersion > 0.0
    blob = " ".join(ghost.reasons)
    assert "NO per-player market dispersion" in blob
    assert "positional_market_band_prior" in blob
    # his neighbours DID price, so this is a per-player absence and not a dead board
    assert _row(board, "RB Ladder 06").has_market_band


# ----------------------------------- the D/ST join (finding 16)


def _dst_world(marginal_world, db, *, n=12, alias_pair=None):
    """``n`` D/ST units, each its own team, plus a market board joined ONLY by
    team abbreviation (D/ST rows carry a NULL gsis and a negative ESPN id)."""
    teams = [f"D{i:02d}" for i in range(n)]
    specs = [{"name": f"{t} Defense", "pos": "D/ST", "team": t,
              "pts": 12.0 - i * 0.5, "bye": 6} for i, t in enumerate(teams)]
    marginal_world(specs, retrieved=PULL)
    for i, t in enumerate(teams, start=1):
        market_team = t
        if alias_pair and alias_pair[0] == t:
            market_team = alias_pair[1]
        _adp(db, fantasypros_id=f"DST{i:03d}", position="DST", team=market_team,
             gsis_id=None, espn_id=None,
             ecr=float(i), best=max(1, i - 2), worst=min(n, i + 2), pos_rank=i)
    db.commit()
    return specs


def test_the_dst_market_join_is_by_team_abbreviation(db, marginal_world):
    """D/ST is the one position joined by team rather than by id — and it is the
    position at 100% band coverage on the live board. Deleting the branch left all
    39 original tests green because no test built a single D/ST row."""
    _dst_world(marginal_world, db, n=12)
    board = _board(db)
    dst = board.by_position("DST")
    assert len(dst) == 12
    assert all(r.has_market_band for r in dst), [r.player for r in dst]
    assert all(r.market_join == "dst_team" for r in dst)
    assert all(r.gsis_id is None for r in dst), "the live D/ST shape carries no gsis"
    # the band must be a real conversion, not a constant
    assert len({round(r.market_band_points, 3) for r in dst}) > 1


def test_the_dst_join_normalises_the_team_abbreviation(db, marginal_world):
    """``LAR`` on the market board and ``LA`` on ours is the live case
    ``base.TEAM_ALIASES`` exists for; a raw join loses the Rams silently."""
    assert base.TEAM_ALIASES.get("LAR") == "LA"
    teams = ["LA"] + [f"D{i:02d}" for i in range(1, 12)]
    specs = [{"name": f"{t} Defense", "pos": "D/ST", "team": t,
              "pts": 12.0 - i * 0.5, "bye": 6} for i, t in enumerate(teams)]
    marginal_world(specs, retrieved=PULL)
    for i, t in enumerate(teams, start=1):
        _adp(db, fantasypros_id=f"DST{i:03d}", position="DST",
             team="LAR" if t == "LA" else t, gsis_id=None, espn_id=None,
             ecr=float(i), best=max(1, i - 2), worst=min(12, i + 2), pos_rank=i)
    db.commit()

    board = _board(db)
    rams = next(r for r in board.by_position("DST") if r.team == "LA")
    assert rams.has_market_band, "LAR on the market board must join LA on ours"
    assert rams.market_join == "dst_team"
    # every other D/ST joined too, so this is the alias and not a dead board
    assert all(r.has_market_band for r in board.by_position("DST"))


# ------------------------- position conflict + name join (findings 6/14/21)


def test_a_market_row_at_another_position_is_refused_not_read_off_our_curve(
    db, marginal_world
):
    """``lookup`` took ``position`` and ignored it, so a player the two feeds
    disagree about (live: one is RB in ``projections`` and TE on the ``rp`` board)
    had a TE rank priced through the RB points curve — measured 3.3x wrong — while
    the reason named the TE curve that was NOT used."""
    specs = _ladder(14) + _ladder(14, pos="TE", top=9.0, team_prefix="E")
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs[:14], spread=2, skip={"RB Ladder 04"})
    for i, _spec in enumerate(specs[14:], start=15):
        _adp(db, fantasypros_id=f"TE{i:03d}", position="TE",
             gsis_id=f"00-1{i - 1:05d}", espn_id=str(2000 + i - 1),
             ecr=float(i - 14), best=max(1, i - 16), worst=i - 12, pos_rank=i - 14)
    # the conflict: our RB's own gsis, ranked TE3 on the market board
    _adp(db, fantasypros_id="XPOS", position="TE", gsis_id="00-100004",
         espn_id="2004", ecr=3.0, best=1, worst=5, pos_rank=3)
    db.commit()

    board = _board(db)
    row = _row(board, "RB Ladder 04")
    assert row.position == "RB"
    assert row.market_join == "position_conflict"
    assert row.market_band_points is None
    assert row.rank is None, "the conflicting row must not be carried as his rank"
    blob = " ".join(row.reasons)
    assert "RB" in blob and "TE" in blob and "REFUSED" in blob
    # and the wrong answer this guards against: the RB curve read at the TE rank
    rb_curve = board.curves["RB"]
    wrong = rb_curve.at(1) - rb_curve.at(5)
    assert not any(
        r.market_band_points is not None
        and r.player == "RB Ladder 04"
        and r.market_band_points == pytest.approx(wrong)
        for r in board.rows.values()
    )


def test_an_idless_market_row_is_joined_by_name_and_the_row_says_so(
    db, marginal_world
):
    """``adp_rankings`` carries a NULL gsis AND a NULL espn on 52 of 807 live rows
    (14 of 44 kickers). The id join misses them and the module used to report that
    as "this player is not on the board" — a false statement about the market, at
    the position CLAUDE.md names as the house scoring edge."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 08"})
    _adp(db, fantasypros_id="NOID", position="RB", gsis_id=None, espn_id=None,
         ecr=9.0, best=6, worst=12, pos_rank=9)
    db.execute("UPDATE adp_rankings SET player = 'RB Ladder 08' "
               "WHERE fantasypros_id = 'NOID'")
    db.commit()

    row = _row(_board(db), "RB Ladder 08")
    assert row.market_join == "name_position"
    assert row.has_market_band, "the board plainly ranks him; refusing would be a lie"
    blob = " ".join(row.reasons)
    assert "JOINED BY NAME, NOT BY ID" in blob
    assert "not on the" not in blob


def test_a_duplicated_name_refuses_the_name_join_rather_than_guessing(
    db, marginal_world
):
    """Two players share a (name, position) key on the live board. Picking either
    is a coin flip on whose expert spread gets published."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 08"})
    for tag, ecr in (("DUP1", 9.0), ("DUP2", 2.0)):
        _adp(db, fantasypros_id=tag, position="RB", gsis_id=None, espn_id=None,
             ecr=ecr, best=int(ecr) - 1, worst=int(ecr) + 1, pos_rank=int(ecr))
        db.execute("UPDATE adp_rankings SET player = 'RB Ladder 08' "
                   "WHERE fantasypros_id = ?", (tag,))
    db.commit()

    board = _board(db)
    row = _row(board, "RB Ladder 08")
    assert row.market_join == "ambiguous_name"
    assert row.market_band_points is None
    assert any("MORE THAN ONE" in r for r in row.reasons), row.reasons
    market = market_rank_dispersion(db, as_of=AS_OF, season=SEASON, floors=TEST_FLOORS)
    assert market.by_name_pos[("rb ladder 08", "RB")] is None
    assert any("duplicated (name, position)" in n for n in market.notes)


def test_the_name_join_strips_suffixes_and_punctuation_but_is_not_fuzzy():
    norm = dispersion._norm_name
    assert norm("A.J. Brown") == norm("AJ Brown")
    assert norm("Marvin Harrison Jr.") == norm("Marvin Harrison")
    assert norm("Amon-Ra St. Brown") == norm("Amon Ra St Brown")
    assert norm(None) is None
    # NOT fuzzy: a different name must stay a different key
    assert norm("Michael Pittman") != norm("Mike Pittman")


# ------------------------- the tier/affine cross-check sentence (findings 2/13)


def test_the_cross_check_quotes_the_drift_at_the_tier_mean_it_names():
    """The sentence names ONE mean — the tier's — and the number after it must be
    the drift AT THAT MEAN. Printing the drift at the player's own mu under that
    label made 348 of 499 live rows quote a disagreement past this module's own
    7.5% bound, in the same sentence claiming the two models agree within 7.3%."""
    tier = DEFAULT_TIER_SIGMA.tier_of("TE", 21)
    mu = 8.5                                    # well away from the tier's mean
    assert abs(mu - tier.mean_mu) > 3.0
    est = weekly_sigma("TE", mu=mu, rank=21)

    expected_at_mean = (DEFAULT_VARIANCE.sigma("TE", tier.mean_mu) - tier.sigma) / tier.sigma
    assert abs(expected_at_mean) <= TIER_VS_AFFINE_MAX_DRIFT
    assert f"AT THAT MEAN the affine model reads {expected_at_mean:+.1%}" in est.reason

    # the player's own reading is also there, explicitly attached to HIS mean
    at_mu = (DEFAULT_VARIANCE.sigma("TE", mu) - tier.sigma) / tier.sigma
    assert abs(at_mu) > TIER_VS_AFFINE_MAX_DRIFT, "this case must actually differ"
    assert f"THIS player projects {mu:.1f} pts/wk" in est.reason
    assert f"({at_mu:+.1%})" in est.reason


def test_every_cross_check_on_a_real_board_quotes_a_drift_inside_the_bound(
    db, marginal_world
):
    """A property over a whole board: whatever number follows "AT THAT MEAN", it
    is a claim about the two models agreeing, so it must never exceed the bound
    the module froze for exactly that claim."""
    import re as _re

    specs = _ladder(14) + _ladder(14, pos="TE", top=9.0, team_prefix="E")
    _world(marginal_world, db, specs, market=False)
    board = _board(db)
    seen = 0
    for row in board.rows.values():
        for reason in row.reasons:
            for hit in _re.findall(r"AT THAT MEAN the affine model reads ([+-][\d.]+)%",
                                   reason):
                seen += 1
                assert abs(float(hit)) <= TIER_VS_AFFINE_MAX_DRIFT * 100 + 0.05, reason
    assert seen > 0, "this world must actually print cross-check sentences"


# --------------------------------------- the D/ST cohort (finding 3)


def test_the_dst_tiers_are_the_reg_only_measurement_the_cohort_string_claims():
    """The D/ST cells first shipped as a REG+POST fit while every D/ST reason
    string quoted "2021-2025 REG only" — the offense cells beside them and
    ``DEFAULT_VARIANCE``'s own D/ST fit are both REG-only, so the "same cohort"
    cross-check claim was false for the one position it was quoted on.

    Pinned as literals rather than re-derived: re-running the measurement inside
    the test would agree with whatever the code did.
    """
    tiers = DEFAULT_TIER_SIGMA.tiers["DST"]
    measured = [(t.lo, t.hi, t.n, t.mean_mu, t.sigma, t.median_sigma, t.mu_lo, t.mu_hi)
                for t in tiers]
    assert measured == [
        (1, 10, 50, 8.1195, 6.3961, 6.3802, 6.4706, 10.7059),
        (11, 20, 50, 5.8894, 6.4467, 6.4143, 4.5294, 7.4706),
        (21, None, 60, 3.8235, 5.7741, 5.7192, 0.2353, 5.7059),
    ]
    # the REG+POST values these must never drift back to
    reg_post_top = (1, 10, 50, 7.9920, 6.5546, 6.3570, 6.1765, 11.4737)
    assert measured[0] != reg_post_top
    assert "REG only" in " ".join(DEFAULT_TIER_SIGMA.cohort.split())

    # and the claim those cells exist to support must hold for D/ST too
    for tier in tiers:
        drift = abs(DEFAULT_VARIANCE.sigma("DST", tier.mean_mu) - tier.sigma) / tier.sigma
        assert drift <= TIER_VS_AFFINE_MAX_DRIFT, (tier.label, drift)


# ---------------------------- the series named in the reason (finding 4)


def test_the_row_names_the_market_series_it_actually_came_from(db, marginal_world):
    """The per-row provenance line — the one Rule 6 puts in front of the operator
    — used to hard-code "FantasyPros full-PPR redraft consensus" whatever series
    was requested, so a dynasty board priced 431 rows each asserting the wrong
    market by name under a board banner that said the opposite."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, ecr_type="dp")   # dynasty POSITIONAL
    db.commit()

    board = _board(db, ecr_type="dp")
    assert board.rank_units == "positional", "dp must still convert, or this proves nothing"
    priced = [r for r in board.rows.values() if r.has_market_band]
    assert priced
    for row in priced:
        blob = " ".join(row.reasons)
        assert "'dp'" in blob or "dynasty" in blob, blob
        assert "full-PPR redraft consensus" not in blob
        assert "NOT this league's redraft full-PPR market" in blob
    # and the redraft board still says what it is
    _rank_ladder(db, specs, spread=2, ecr_type="rp")
    db.commit()
    rp_row = next(r for r in _board(db).rows.values() if r.has_market_band)
    assert "FantasyPros full-PPR redraft consensus" in " ".join(rp_row.reasons)


# ------------------------- the frozen prior's own provenance (findings 5/11/24)


def test_the_fallback_prior_reproduces_from_the_cohort_it_quotes():
    """The prior is what an uncovered player is priced with and the label is what
    the operator is told to trust, so the label's arithmetic is checked rather
    than believed. The first version quoted an n and per-position medians that the
    shipped ``build_dispersion`` did not produce."""
    cohort = dispersion.POSITIONAL_BAND_PRIOR_COHORT
    assert set(cohort) == set(POSITIONAL_BAND_PRIOR)
    for pos, (n, median_band) in cohort.items():
        assert n > 0
        assert POSITIONAL_BAND_PRIOR[pos] == round(
            median_band / dispersion.REFERENCE_BAND_PRIOR, 3
        ), pos
    total = sum(n for n, _ in cohort.values())
    label = dispersion.POSITIONAL_BAND_PRIOR_LABEL
    assert f"n={total} priced players" in label
    assert f"{dispersion.REFERENCE_BAND_PRIOR:.1f}-point median band" in label
    # the label must not go back to promising a constant it is not
    assert "0.84-1.18" in label


def test_the_reference_band_is_the_cohort_median_by_the_same_definition(
    db, marginal_world
):
    """``reference_band`` is what every ``relative_dispersion`` divides by. Pinned
    to its definition on a real board so a silent substitution cannot pass."""
    import statistics as _stats

    specs = _ladder(14) + _ladder(14, pos="TE", top=9.0, team_prefix="E")
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs[:14], spread=2)
    for i, _spec in enumerate(specs[14:], start=15):
        _adp(db, fantasypros_id=f"TE{i:03d}", position="TE",
             gsis_id=f"00-1{i - 1:05d}", espn_id=str(2000 + i - 1),
             ecr=float(i - 14), best=max(1, i - 17), worst=i - 11, pos_rank=i - 14)
    db.commit()

    board = _board(db)
    bands = [r.market_band_points for r in board.rows.values() if r.has_market_band]
    assert len(bands) >= TEST_FLOORS.reference_rows
    assert board.reference_source == "cohort_median"
    assert board.reference_band == pytest.approx(_stats.median(bands))
    for row in board.rows.values():
        if row.has_market_band:
            assert row.relative_dispersion == pytest.approx(
                row.market_band_points / board.reference_band
            )


def test_the_reference_row_floor_is_crossed_in_both_directions_and_labelled(
    db, marginal_world
):
    """The fourth floor, which no test crossed — forcing every board onto the
    frozen prior passed all 39 original tests, and so did keeping the
    ``cohort_median`` label while using the frozen value."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, spread=2)

    over = _board(db, floors=Floors(board_rows=10, position_rows=4,
                                    curve_depth=8, reference_rows=4))
    assert over.reference_source == "cohort_median"
    assert over.reference_band != pytest.approx(dispersion.REFERENCE_BAND_PRIOR)
    assert not any("FROZEN reference band" in b for b in over.banners)

    under = _board(db, floors=Floors(board_rows=10, position_rows=4,
                                     curve_depth=8, reference_rows=99))
    assert under.reference_source == "frozen_prior"
    assert under.reference_band == dispersion.REFERENCE_BAND_PRIOR
    assert any("FROZEN reference band" in b for b in under.banners)
    # the label and the number must agree: a row's relative number is computed
    # against whichever one was actually used
    row = next(r for r in under.rows.values() if r.has_market_band)
    assert row.relative_dispersion == pytest.approx(
        row.market_band_points / dispersion.REFERENCE_BAND_PRIOR
    )


# ------------------------------------- the staleness banners (findings 10/18)


def test_a_stale_projection_pull_raises_its_own_warning(db, marginal_world):
    """A July projection pricing an August decision carries a perfectly valid
    ``knowable_as_of`` — Rule-1-invisible. Deleting this banner, or setting
    ``STALE_BANNER_DAYS`` to 9999, left all 39 original tests green."""
    specs = _ladder(12)
    marginal_world(specs, retrieved="2026-07-01")
    _rank_ladder(db, specs, scrape="2026-08-28", retrieved="2026-08-28")
    db.commit()

    board = _board(db)
    blob = "\n".join(board.banners)
    assert "WARNING" in blob
    assert "projections on this board are" in blob
    assert "ziggurat ingest run" in blob

    fresh = _board(db, floors=TEST_FLOORS)     # same world, generous threshold
    assert any("projections: newest pull 2026-07-01" in b for b in fresh.banners)


def test_a_stale_ecr_scrape_raises_its_own_warning(db, marginal_world):
    """FantasyPros serves the CURRENT scrape only, so a missed pull is gone — a
    three-week-old board on draft night must shout, not sit there."""
    specs = _ladder(12)
    marginal_world(specs, retrieved="2026-08-28")
    _rank_ladder(db, specs, scrape="2026-07-20", retrieved="2026-07-20")
    db.commit()

    blob = "\n".join(_board(db).banners)
    assert "WARNING" in blob
    assert "expert-consensus board is" in blob and "days old" in blob
    assert "ingest status" in blob


def test_a_fresh_board_raises_neither_warning(db, marginal_world):
    """The other side of the threshold — a banner that always fires is a banner
    nobody reads."""
    specs = _ladder(12)
    marginal_world(specs, retrieved="2026-08-28")
    _rank_ladder(db, specs, scrape="2026-08-28", retrieved="2026-08-28")
    db.commit()

    board = _board(db)
    assert not any("WARNING" in b for b in board.banners), board.banners
    assert any("market ECR:" in b for b in board.banners)


def test_the_ingest_freshness_verdict_reaches_the_banner(db, marginal_world, monkeypatch):
    """``today=`` is the OPERATIONAL clock, separate from the ``as_of`` data gate.
    Nothing exercised it, so the ``refresh.source_freshness`` lines — including the
    "cannot be re-pulled" flag on the perishable sources this module reads — were
    dead code."""
    specs = _ladder(12)
    marginal_world(specs, retrieved="2026-08-28")
    _rank_ladder(db, specs, scrape="2026-08-28", retrieved="2026-08-28")
    db.commit()

    def fake(conn, *, season, today):
        return [
            {"source": "adp_rankings", "verdict": "stale", "perishable": True,
             "age_days": 40},
            {"source": "projections", "verdict": refresh.VERDICT_FRESH,
             "perishable": True, "age_days": 1},
            {"source": "weekly_stats", "verdict": "stale", "perishable": False,
             "age_days": 90},
        ]

    monkeypatch.setattr(refresh, "source_freshness", fake)
    quiet = _board(db)
    assert not any("ingest says" in b for b in quiet.banners)

    loud = _board(db, today="2026-08-30")
    blob = "\n".join(loud.banners)
    assert "ingest says adp_rankings: stale" in blob
    assert "cannot be re-pulled" in blob
    assert "projections: fresh" not in blob      # a quiet verdict stays quiet
    assert "weekly_stats" not in blob            # not a source this module reads


# ------------------------------------- the coverage columns (finding 19)


def test_the_coverage_columns_are_not_interchangeable(db, marginal_world):
    """``test_the_coverage_report_accounts_for_every_row`` only checks the four
    columns sum to the total, so swapping the "market rank only" and "prior"
    accumulators passed. They mean opposite things to the operator."""
    absent_names = {"RB Ladder 09"}
    deep_names = {"RB Ladder 10", "RB Ladder 11", "RB Ladder 12"}
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    # DELIBERATELY ASYMMETRIC counts (1 vs 3): equal counts make the two columns
    # indistinguishable, which is how the swap survived in the first place.
    for i, spec in enumerate(specs, start=1):
        if spec["name"] in absent_names | deep_names:
            continue
        _adp(db, fantasypros_id=f"RB{i:03d}", position="RB",
             gsis_id=f"00-1{i - 1:05d}", espn_id=str(2000 + i - 1),
             ecr=float(i), best=max(1, i - 2), worst=min(14, i + 2), pos_rank=i)
    # ON the board, but ranked past the 14-deep curve -> "rank only".
    for j, name in enumerate(sorted(deep_names)):
        idx = int(name.split()[-1])
        _adp(db, fantasypros_id=f"RBDEEP{j}", position="RB",
             gsis_id=f"00-1{idx:05d}", espn_id=str(2000 + idx),
             ecr=40.0 + j, best=30 + j, worst=60 + j, pos_rank=40 + j)
    db.commit()

    board = _board(db)
    absent = _row(board, "RB Ladder 09")
    deep = _row(board, "RB Ladder 10")
    assert absent.rank is None and deep.rank is not None
    assert not absent.has_market_band and not deep.has_market_band

    cell = next(c for c in board.coverage if c.position == "RB")
    assert cell.positional_prior == 1, "the ABSENT player belongs in 'prior'"
    assert cell.market_rank_only == 3, "the out-of-curve players belong in 'rank only'"
    assert cell.market_band == cell.total - 4
    assert cell.total == (cell.market_band + cell.market_rank_only
                          + cell.positional_prior + cell.no_house_line)


def test_the_coverage_legend_does_not_claim_a_fact_it_cannot_know():
    """"not on the market board at all" was false for 4 of the 22 live rows that
    printed it — they were on the board with NULL ids. The column now describes
    what it measures (no row could be JOINED) and defers to the row's reasons."""
    text = dispersion.format_coverage(
        (dispersion.CoverageRow(position="RB", total=3, market_band=1,
                                market_rank_only=1, positional_prior=1),)
    )
    assert "no market row could be JOINED" in text
    assert "not on the market board at all" not in text


# --------------------------- draftable coverage (findings 9/20)


def _espn_universe(db, rows, *, season=SEASON, retrieved=PULL, knowable=None):
    """Minimal ``espn_draft_ranks`` rows. ``board_key`` is str(espn_id) for skill
    and the team abbr for D/ST — the live contract (D/ST carries a NULL espn_id)."""
    for i, (player, pos, team, espn_id) in enumerate(rows, start=1):
        key = team if pos == "D/ST" else str(espn_id)
        db.execute(
            "INSERT INTO espn_draft_ranks (board_key, espn_id, player, position, "
            "team, season, overall_rank, retrieved_as_of, knowable_as_of) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (key, None if pos == "D/ST" else str(espn_id), player, pos, team,
             season, i, retrieved, knowable or retrieved),
        )
    db.commit()


def test_draftable_coverage_accounts_for_the_whole_espn_universe(db, marginal_world):
    """The number the module's own docstring calls "whether this module is usable
    on draft night" had no test of any kind."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    for i, spec in enumerate(specs, start=1):
        if spec["name"] == "RB Ladder 11":
            continue                      # absent from the market board -> prior
        _adp(db, fantasypros_id=f"RB{i:03d}", position="RB",
             gsis_id=f"00-1{i - 1:05d}", espn_id=str(2000 + i - 1),
             ecr=float(i), best=max(1, i - 2), worst=min(14, i + 2), pos_rank=i)
    db.commit()
    board = _board(db)
    assert sum(1 for r in board.rows.values() if r.has_market_band) == 13

    universe = [(s["name"], "RB", s["team"], 2000 + i) for i, s in enumerate(specs)]
    universe.append(("Undrafted Nobody", "RB", "ZZZ", 999999))   # no house line
    _espn_universe(db, universe)

    cov = dispersion.draftable_coverage(db, as_of=AS_OF, season=SEASON, board=board)
    cell = next(c for c in cov if c.position == "RB")
    assert cell.total == 15
    assert cell.no_house_line == 1, "a draftable player we cannot price at all"
    assert cell.positional_prior == 1, "on ESPN's board, absent from the market's"
    assert cell.market_rank_only == 0
    assert cell.market_band == 13
    assert cell.total == (cell.market_band + cell.market_rank_only
                          + cell.positional_prior + cell.no_house_line)
    assert cell.market_band_share == pytest.approx(13 / 15)


def test_draftable_coverage_joins_dst_by_team_and_skill_by_id(db, marginal_world):
    """Two different join ladders in one function, neither exercised: D/ST by team
    abbreviation (its ESPN row carries a NULL espn_id) and skill by ESPN id with a
    gsis fallback through the players crosswalk."""
    _dst_world(marginal_world, db, n=12)
    board = _board(db)
    _espn_universe(db, [(f"D{i:02d} Defense", "D/ST", f"D{i:02d}", -16000 - i)
                        for i in range(12)])

    cov = dispersion.draftable_coverage(db, as_of=AS_OF, season=SEASON, board=board)
    cell = next(c for c in cov if c.position == "DST")
    assert cell.total == 12
    assert cell.no_house_line == 0, "a D/ST joined by team must not read as unpriced"
    assert cell.market_band == 12


def test_draftable_coverage_falls_back_to_the_gsis_crosswalk(db, marginal_world):
    """The second rung of the skill ladder: when a dispersion row carries no ESPN
    id, ``base.gsis_by_espn`` is the bridge from the ESPN board to it. Deleting
    that rung would silently reclassify a fully-priced player as "no house line",
    i.e. as a projection-coverage gap he does not have."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    for i, _spec in enumerate(specs, start=1):
        _adp(db, fantasypros_id=f"RB{i:03d}", position="RB",
             gsis_id=f"00-1{i - 1:05d}", espn_id=str(2000 + i - 1),
             ecr=float(i), best=max(1, i - 2), worst=min(14, i + 2), pos_rank=i)
    # drop his crosswalk row BEFORE pricing, so his dispersion row has no espn_id
    db.execute("DELETE FROM players WHERE gsis_id = '00-100000'")
    db.commit()
    board = _board(db)
    # his display name came from that crosswalk row too, so find him by gsis
    orphan = next(r for r in board.rows.values() if r.gsis_id == "00-100000")
    assert orphan.espn_id is None
    assert orphan.has_market_band, "he is fully priced; only his ESPN id is missing"

    _espn_universe(db, [(s["name"], "RB", s["team"], 2000 + i)
                        for i, s in enumerate(specs)])

    # with no crosswalk he reads as UNPRICED, which is the wrong answer
    blind = dispersion.draftable_coverage(db, as_of=AS_OF, season=SEASON, board=board)
    assert next(c for c in blind if c.position == "RB").no_house_line == 1

    # restore the crosswalk row; the gsis rung must now find him
    db.execute(
        "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, retrieved_as_of, "
        "knowable_as_of) VALUES (?,?,?,?,?,?)",
        ("00-100000", "S0", "2000", "RB Ladder 00", PULL, PULL),
    )
    db.commit()
    cov = dispersion.draftable_coverage(db, as_of=AS_OF, season=SEASON, board=board)
    cell = next(c for c in cov if c.position == "RB")
    assert cell.no_house_line == 0
    assert cell.market_band == 14


def test_draftable_coverage_threads_the_as_of_and_the_view(db, marginal_world):
    """Rule 1's leakage test for this accessor. Hard-coding ``view="latest_truth"``
    here — the exact bug the module's other two accessors each have a test for —
    would have shipped green."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, spread=2)
    board = _board(db)

    # a board row that was KNOWABLE in the past but RETRIEVED today
    _espn_universe(db, [(s["name"], "RB", s["team"], 2000 + i)
                        for i, s in enumerate(specs)],
                   retrieved="2026-08-29", knowable="2026-08-01")

    hidden = dispersion.draftable_coverage(db, as_of="2026-08-05", season=SEASON,
                                           board=board)
    assert hidden == (), "a row retrieved after as_of must be invisible"

    shown = dispersion.draftable_coverage(db, as_of="2026-08-05", season=SEASON,
                                          board=board, view="latest_truth")
    assert sum(c.total for c in shown) == 14

    # and the knowledge gate independently: a row knowable only later
    future = dispersion.draftable_coverage(db, as_of=AS_OF, season=SEASON, board=board)
    assert sum(c.total for c in future) == 14


def test_draftable_coverage_refuses_an_implicit_now(db, marginal_world):
    specs = _ladder(12)
    _world(marginal_world, db, specs)
    board = _board(db)
    with pytest.raises(TypeError):
        dispersion.draftable_coverage(db, season=SEASON, board=board)


# ------------------------------------------- spread_over_4 (finding 20)


def test_spread_over_4_is_the_grader_range_in_rank_units(db, marginal_world):
    """The literal quantity item 2.3 named as the upgrade. Changing the divisor to
    /2 passed all 39 original tests."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=3)
    db.commit()

    market = market_rank_dispersion(db, as_of=AS_OF, season=SEASON, floors=TEST_FLOORS)
    row = next(r for r in market.rows if r.pos_rank == 7)
    assert (row.best, row.worst) == (4, 10)
    assert row.spread_over_4 == pytest.approx(6 / 4.0)
    assert row.spread_over_4 != pytest.approx(6 / 2.0)

    no_range = dispersion.RankDispersion(
        fantasypros_id="x", player=None, position="RB", team=None, gsis_id=None,
        espn_id=None, ecr=5.0, sd=None, best=None, worst=None, pos_rank=5,
        ecr_type="rp", rank_units="positional", scrape_date=None, board_depth=20,
    )
    assert no_range.spread_over_4 is None
    assert no_range.panel_scale_is_consistent is False


def test_the_name_join_disclosure_counts_the_board_it_is_printed_on(db, marginal_world):
    """The disclosure quotes "N of M rows on this scrape carry no id". Quoting a
    number measured on a different board would be the same class of defect this
    module already had — a provenance claim that does not reproduce."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 08", "RB Ladder 12"})
    for tag, target, ecr in (("NOID8", "RB Ladder 08", 9.0), ("NOID12", "RB Ladder 12", 13.0)):
        _adp(db, fantasypros_id=tag, position="RB", gsis_id=None, espn_id=None,
             ecr=ecr, best=int(ecr) - 2, worst=int(ecr) + 1, pos_rank=int(ecr))
        db.execute("UPDATE adp_rankings SET player = ? WHERE fantasypros_id = ?",
                   (target, tag))
    db.commit()

    market = market_rank_dispersion(db, as_of=AS_OF, season=SEASON, floors=TEST_FLOORS)
    assert market.idless_rows == 2
    row = _row(_board(db), "RB Ladder 08")
    assert f"{market.idless_rows} of {len(market.rows)} rows on this scrape" in \
        " ".join(row.reasons)


def test_a_name_match_on_a_row_with_its_own_ids_is_refused(db, marginal_world):
    """The name join closes a crosswalk GAP (a market row with no ids at all).
    A market row that HAS ids the id join already failed on is a crosswalk
    DISAGREEMENT — overriding it by name is exactly how a name join prices a
    different player's expert spread."""
    specs = _ladder(14)
    _world(marginal_world, db, specs, market=False)
    _rank_ladder(db, specs, spread=2, skip={"RB Ladder 08"})
    # same name, DIFFERENT ids
    _adp(db, fantasypros_id="OTHER", position="RB",
         gsis_id="00-999999", espn_id="88888",
         ecr=9.0, best=7, worst=11, pos_rank=9)
    db.execute("UPDATE adp_rankings SET player = 'RB Ladder 08' "
               "WHERE fantasypros_id = 'OTHER'")
    db.commit()

    row = _row(_board(db), "RB Ladder 08")
    assert row.market_join == "id_mismatch"
    assert row.market_band_points is None
    assert row.relative_source == "positional_prior"
    blob = " ".join(row.reasons)
    assert "disagree about identity" in blob
    assert "JOINED BY NAME" not in blob
