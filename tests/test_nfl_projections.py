"""Cached-fixture + leakage tests for Sleeper projections ingestion (item 1.5).

Offline: the ``source.import_sleeper_projections`` network seam is never called
(tests feed a hand-authored Sleeper-shaped JSON fixture directly). The fixture
carries a QB, a K, a DEF (each with hand-computable scoring lines) plus an FB
and a CB to prove non-scoring positions are filtered out.
"""

import json
from pathlib import Path

import pytest

from ziggurat.core import scoring
from ziggurat.data.nfl import base, projections

_FIXTURE = Path(__file__).parent / "fixtures" / "nfl" / "sleeper_projections_sample.json"


def _raw_rows():
    return json.loads(_FIXTURE.read_text())


def _by_id(rows, source_player_id):
    return next(r for r in rows if r["player_id"] == source_player_id)


def _stub_player(db, *, sleeper_id, gsis_id, retrieved="2023-09-01"):
    db.execute(
        "INSERT INTO players (gsis_id, sleeper_id, retrieved_as_of, knowable_as_of) "
        "VALUES (?, ?, ?, ?)",
        (gsis_id, sleeper_id, retrieved, retrieved),
    )
    db.commit()


def _week1_schedule(db):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, gameday, home_team, away_team, "
        "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2023_01_KC_DET", 2023, 1, "2023-09-07", "KC", "DET", "2023-08-01", "2023-08-01"),
    )
    db.commit()


# --------------------------------------------------------------- mapper / validator


def test_map_produces_only_canonical_scoring_keys():
    rows = _raw_rows()
    qb = projections.map_sleeper_projection(_by_id(rows, "3163"))
    assert qb == {
        "passing_yards": 300,
        "passing_tds": 2,
        "interceptions": 1,
        "rushing_yards": 20,
        "rushing_tds": 1,
        "passing_2pt_conversions": 1,
        "fumbles_lost": 1,
    }
    # pts_ppr is NOT mapped into the scoring dict (it is the projected_points
    # cross-check, never a scoring input).
    assert "projected_points" not in qb and "pts_ppr" not in qb


def test_map_kicker_buckets_and_derived_missed():
    k = projections.map_sleeper_projection(_by_id(_raw_rows(), "4227"))
    assert k["fg_made_0_39"] == 2.0        # fgm_20_29 + fgm_30_39
    assert k["fg_made_40_49"] == 1
    assert k["fg_made_50_59"] == 1         # fgm_50p (lossy: absorbs 60+)
    assert "fg_made_60" not in k           # source cannot fill
    assert k["pat_made"] == 3
    assert k["fg_missed"] == 1.0           # fga - fgm = 5 - 4


def test_map_kicker_includes_sub_20_yard_makes():
    """fgm_0_19 (sub-20-yd makes) must fold into the 0–39 bucket — dropping it
    would silently score a projected short FG at 0 instead of +3."""
    row = {
        "player_id": "K19", "position": "K", "team": "BUF",
        "stats": {"fgm_0_19": 0.5, "fgm_20_29": 1.0, "fgm_30_39": 2.0, "fga": 4.0, "fgm": 3.5},
    }
    mapped = projections.map_sleeper_projection(row)
    assert mapped["fg_made_0_39"] == 3.5  # 0.5 + 1.0 + 2.0, not 3.0


def test_map_dst_def_tds_no_double_count():
    d = projections.map_sleeper_projection(_by_id(_raw_rows(), "DET"))
    # def_td (1) + st_td (1) only — def_pr_td / pass_int_td / pr_td are decoys
    # that must NOT be added (they are already subsumed).
    assert d["def_tds"] == 2.0
    assert d["sacks"] == 3
    assert d["def_interceptions"] == 1
    assert d["fumble_recoveries"] == 1
    assert d["blocked_kicks"] == 1
    assert d["safeties"] == 0             # present-with-0 kept (real value)
    assert d["points_allowed"] == 17
    assert d["yards_allowed"] == 250


def test_non_scoring_positions_are_filtered():
    rows = _raw_rows()
    assert projections.map_sleeper_projection(_by_id(rows, "8025")) is None  # FB
    assert projections.map_sleeper_projection(_by_id(rows, "1696")) is None  # CB


def test_validator_passes_mapped_and_raises_on_unknown():
    mapped = projections.map_sleeper_projection(_by_id(_raw_rows(), "3163"))
    projections.validate_projection_keys(mapped)  # clean mapped dict — no raise

    projections.validate_projection_keys(dict(mapped))  # copy still clean
    with pytest.raises(ValueError):
        projections.validate_projection_keys({**mapped, "passing_yardz": 1})


def test_validator_rejects_raw_sleeper_stats():
    # Proves WHY the validator must be fed the mapped dict, not raw stats: the
    # raw Sleeper stats carry non-scoring keys (bonus_*, gp, pts_ppr, ...).
    raw_stats = _by_id(_raw_rows(), "3163")["stats"]
    with pytest.raises(ValueError):
        projections.validate_projection_keys(raw_stats)


# ---------------------------------------------------------------- ingest + scoring


def test_ingest_stores_canonical_rows_and_scores_directly(db):
    _stub_player(db, sleeper_id="3163", gsis_id="00-0033106")
    n = projections.ingest_projections(db, _raw_rows(), retrieved_as_of="2023-09-05")
    assert n == 3  # QB, K, DEF stored; FB + CB filtered out

    got = projections.get_projections(db, as_of="2023-09-05", season=2023, week=1)
    positions = {r["position"] for r in got}
    assert positions == {"QB", "K", "DEF"}

    rows = {r["source_player_id"]: r for r in got}

    # Skill player resolves gsis via the crosswalk; DEF stays NULL (kept anyway).
    assert rows["3163"]["gsis_id"] == "00-0033106"
    assert rows["DET"]["gsis_id"] is None
    # projected_points captured but never a scoring input.
    assert rows["3163"]["projected_points"] == 24.5
    # forward regime: knowable == retrieved == pull day.
    assert rows["3163"]["knowable_as_of"] == "2023-09-05"

    # A stored row scores DIRECTLY through scoring.score (hand-computed totals).
    assert scoring.score("QB", dict(rows["3163"])) == pytest.approx(26.0)
    assert scoring.score("K", dict(rows["4227"])) == pytest.approx(17.0)
    assert scoring.score("DEF", dict(rows["DET"])) == pytest.approx(24.0)


def test_projected_points_is_never_a_scoring_input(db):
    projections.ingest_projections(db, _raw_rows(), retrieved_as_of="2023-09-05")
    det = projections.get_projections(db, as_of="2023-09-05", position="DEF")[0]
    # Zero out every real stat but keep projected_points: the score must be 0,
    # proving projected_points is inert to scoring.
    stat_only = {k: (0 if isinstance(det[k], (int, float)) else det[k]) for k in det.keys()}
    stat_only["points_allowed"] = None
    stat_only["yards_allowed"] = None
    stat_only["projected_points"] = 99.0
    assert scoring.score("DEF", stat_only) == pytest.approx(0.0)


# ------------------------------------------------------------------------ leakage


def test_forward_regime_leakage_by_retrieval(db):
    # Forward rows stamp knowable = retrieved; an earlier as_of cannot see a
    # later pull.
    projections.ingest_projections(db, _raw_rows(), retrieved_as_of="2023-09-01")
    projections.ingest_projections(db, _raw_rows(), retrieved_as_of="2023-09-05")

    early = projections.get_projections(db, as_of="2023-09-03", season=2023, week=1, position="QB")
    assert len(early) == 1
    assert early[0]["knowable_as_of"] == "2023-09-01"

    before_any = projections.get_projections(db, as_of="2023-08-31", season=2023, week=1)
    assert before_any == []


def test_bulk_historical_revision_needs_latest_truth(db):
    from ziggurat.data.nfl import base

    _week1_schedule(db)  # week_first_gameday_map((2023, 1)) == 2023-09-07

    # Two bulk backfills of the same week, retrieved months apart; both are
    # stamped knowable = the week's first gameday.
    projections.ingest_projections(
        db, _raw_rows(), retrieved_as_of="2024-01-01", bulk_historical=True
    )
    revised = _raw_rows()
    _by_id(revised, "3163")["stats"]["pts_ppr"] = 30.0  # a later correction
    projections.ingest_projections(
        db, revised, retrieved_as_of="2024-02-01", bulk_historical=True
    )

    # knowable stamped at gameday, not the bulk-load day.
    stored = projections.get_projections(
        db, as_of="2024-03-01", season=2023, week=1, position="QB", source=projections.SOURCE
    )
    assert stored[0]["knowable_as_of"] == "2023-09-07"

    # historical view at an as_of between the two pulls: retrieval gate hides the
    # later revision, returns the first backfill.
    hist = projections.get_projections(
        db, as_of="2024-01-15", season=2023, week=1, position="QB"
    )
    assert len(hist) == 1 and hist[0]["projected_points"] == 24.5

    # latest_truth relaxes the retrieval gate: the newer correction surfaces.
    lt = base.latest_truth(projections.get_projections)(
        db, as_of="2024-01-15", season=2023, week=1, position="QB"
    )
    assert len(lt) == 1 and lt[0]["projected_points"] == 30.0

    # A row whose knowable_as_of (gameday) is after as_of is invisible under BOTH
    # views — latest_truth relaxes only retrieval, never the knowledge gate.
    pregame = "2023-09-06"
    assert projections.get_projections(db, as_of=pregame, season=2023, week=1) == []
    assert base.latest_truth(projections.get_projections)(
        db, as_of=pregame, season=2023, week=1
    ) == []


# ------------------------------------------------- the unbucketed 50+ makes
#
# The committed fixture is HAND-AUTHORED and carries `fgm_50p`, which the LIVE
# feed does not. That is precisely the frozen-fixture trap item 3.1b named: the
# fixture proved the mapping and could never have proved that upstream still
# serves the shape it maps. So the live shape is pinned here as its own fixture,
# beside the hypothetical one, and both are exercised.

#: One kicker-week EXACTLY as the live Sleeper feed serves it (captured
#: 2026-08-30, season 2026 week 1, a real row with the identity fields removed).
#: Note what is absent: any 50+ bucket, under `fgm_50p` or any other name. The
#: decoys (`fgm_yds`, `fgmiss_*`, `xpmiss`, `adp_dd_ppr`) are kept on purpose —
#: dropping them would make `test_the_live_feed_serves_no_50_plus_bucket`
#: vacuous.
LIVE_KICKER_STATS = {
    "adp_dd_ppr": 999.0, "fga": 2.04, "fgm": 1.74,
    "fgm_0_19": 0.06, "fgm_20_29": 0.3, "fgm_30_39": 0.54, "fgm_40_49": 0.48,
    "fgm_yds": 53.3, "fgmiss_30_39": 0.06, "fgmiss_40_49": 0.06, "gp": 1.0,
    "pos_adp_dd_ppr": 999.0, "pts_half_ppr": 6.96, "pts_ppr": 6.96,
    "pts_std": 6.96, "xpa": 2.58, "xpm": 2.46, "xpmiss": 0.12,
}

#: The residual those numbers imply: 1.74 total makes minus (0.06 + 0.30 + 0.54
#: + 0.48) bucketed = 0.36 makes of 50+ yards, priced at zero today.
LIVE_UNBUCKETED = 0.36


def _live_kicker_row(**overrides):
    stats = {**LIVE_KICKER_STATS, **overrides.pop("stats", {})}
    row = {
        "player_id": "12713", "season": "2026", "week": 1,
        "season_type": "regular", "team": "NE", "opponent": "SEA",
        "player": {"position": "K", "team": "NE",
                   "first_name": "Test", "last_name": "Livekicker"},
        "stats": stats,
    }
    row.update(overrides)
    return row


def test_the_live_feed_serves_no_50_plus_bucket():
    """The finding this whole block exists for, pinned as an assertion.

    `_KICKER_DIRECT_MAP` maps `fgm_50p`; the live feed serves no such key (0 of
    2,754 kicker rows across season 2026 weeks 1-18). If Sleeper ever starts
    serving one this test fails, which is the right alarm: the derivation must
    then stand down in favour of the source value.
    """
    assert "fgm_50p" in projections._KICKER_DIRECT_MAP, "the dead mapping must stay visible"
    assert "fgm_50p" not in LIVE_KICKER_STATS
    assert not any(k.startswith("fgm_5") or k.startswith("fgm_6")
                   for k in LIVE_KICKER_STATS)


def test_unbucketed_makes_is_none_when_the_row_has_no_made_fg_total():
    assert projections.unbucketed_fg_makes({}) is None
    assert projections.unbucketed_fg_makes({"fgm_40_49": 1.0}) is None
    # ...and NOT 0.0: "we cannot tell" is not "there are none".


def test_unbucketed_makes_recovers_the_long_makes_from_the_live_row():
    got = projections.unbucketed_fg_makes(LIVE_KICKER_STATS)
    assert got == pytest.approx(LIVE_UNBUCKETED)
    assert got > 0.0


def test_unbucketed_makes_is_zero_when_the_feed_does_serve_the_bucket():
    """The double-count guard: a feed that serves `fgm_50p` leaves no residual,
    so the derivation adds nothing on top of the source value."""
    stats = {"fgm": 4.0, "fgm_20_29": 1.0, "fgm_30_39": 1.0,
             "fgm_40_49": 1.0, "fgm_50p": 1.0}
    assert projections.unbucketed_fg_makes(stats) == pytest.approx(0.0)


def test_buckets_that_overrun_the_made_total_are_flagged_inconsistent():
    """The live shape of this failure, from the 2021 feed: buckets that sum to
    roughly `fga` instead of `fgm`. The residual there measures nothing."""
    stats = {"fgm": 1.81, "fga": 2.16, "fgm_20_29": 0.38, "fgm_30_39": 0.71,
             "fgm_40_49": 0.71, "fgm_50p": 0.38}
    assert projections.unbucketed_fg_makes(stats) == pytest.approx(-0.37)
    assert projections.fg_buckets_are_consistent(stats) is False


def test_a_rounding_sized_overrun_is_still_consistent():
    """2-decimal source values can disagree by a hair with nothing wrong, and
    438 of the 567 historical over-runs are exactly that. The predicate must
    separate the two populations, not lump them."""
    stats = {"fgm": 1.74, "fgm_0_19": 0.06, "fgm_20_29": 0.30,
             "fgm_30_39": 0.54, "fgm_40_49": 0.85}   # sum 1.75, over by 0.01
    assert projections.unbucketed_fg_makes(stats) == pytest.approx(-0.01)
    assert projections.fg_buckets_are_consistent(stats) is True


def test_the_consistency_boundary_does_not_depend_on_float_noise():
    """A residual of exactly -0.05 must classify the same way every time.

    MEASURED on the live feed (3,231 kicker rows, 2021-2026): 21 rows have a
    residual within 1e-6 of exactly -0.05, and comparing raw floats called 9 of
    them CONSISTENT and 12 INCONSISTENT — decided entirely by which way the
    binary error of a difference of 2-decimal values happened to fall. The
    source publishes 2 decimals, so the exact residual is always a multiple of
    0.01; rounding to that before the comparison is what makes the boundary a
    decision rather than a coin flip.
    """
    # Two ways of writing the SAME exact residual, -0.05, that land on opposite
    # sides of the threshold in raw binary floating point.
    low = {"fgm": 1.30, "fgm_0_19": 0.45, "fgm_20_29": 0.45, "fgm_30_39": 0.45}
    high = {"fgm": 2.20, "fgm_20_29": 0.75, "fgm_30_39": 0.75, "fgm_40_49": 0.75}
    residuals = [projections.unbucketed_fg_makes(low),
                 projections.unbucketed_fg_makes(high)]
    assert all(r == pytest.approx(-0.05) for r in residuals)
    assert min(residuals) < -0.05 < max(residuals), (
        "this test is only meaningful if the two rows straddle the raw threshold")
    assert [projections.fg_buckets_are_consistent(low),
            projections.fg_buckets_are_consistent(high)] == [True, True]


def test_the_boundary_is_permissive_but_reaches_nothing_stored():
    """The ambiguous band is called CONSISTENT, which is the permissive side —
    and it is safe because a NEGATIVE residual is never derived from in either
    mode, so the classification moves only the batch gate's own count."""
    borderline = {"fgm": 1.30, "fgm_0_19": 0.45, "fgm_20_29": 0.45,
                  "fgm_30_39": 0.45, "gp": 1.0}
    assert projections.fg_buckets_are_consistent(borderline) is True
    row = _live_kicker_row(stats=borderline)
    mapped = projections.map_sleeper_projection(row, derive_fg_50_plus=True)
    assert "fg_made_50_59" not in mapped


def test_a_row_with_no_made_total_is_consistent_by_definition():
    """A QB row has no decomposition to break, and must never read as one."""
    assert projections.fg_buckets_are_consistent({}) is True
    assert projections.fg_buckets_are_consistent(_by_id(_raw_rows(), "3163")["stats"]) is True


def test_an_inconsistent_row_is_never_derived_from_even_with_the_flag_on():
    row = _live_kicker_row(stats={"fgm": 0.5})   # buckets now over-run the total
    mapped = projections.map_sleeper_projection(row, derive_fg_50_plus=True)
    assert "fg_made_50_59" not in mapped


def test_the_positivity_check_is_the_consistency_check():
    """The mapper stores a derived count on `residual > 0` alone, with no second
    consistency guard, and this is why that is sound rather than sloppy: an
    inconsistent row over-runs its own `fgm`, so its residual is negative, so the
    two conditions cannot disagree. If they ever could, the mapper would be
    deriving from noise and nothing would say so."""
    cases = [
        LIVE_KICKER_STATS,                                        # clean, positive
        {"fgm": 4.0, "fgm_20_29": 1.0, "fgm_30_39": 1.0,
         "fgm_40_49": 1.0, "fgm_50p": 1.0},                       # clean, exactly zero
        {"fgm": 1.74, "fgm_0_19": 0.06, "fgm_20_29": 0.30,
         "fgm_30_39": 0.54, "fgm_40_49": 0.85},                   # rounding over-run
        {"fgm": 1.81, "fga": 2.16, "fgm_20_29": 0.38,
         "fgm_30_39": 0.71, "fgm_40_49": 0.71, "fgm_50p": 0.38},  # broken
        {"pass_yd": 300},                                          # not a kicker
    ]
    for stats in cases:
        residual = projections.unbucketed_fg_makes(stats)
        positive = residual is not None and residual > 0.0
        assert not (positive and not projections.fg_buckets_are_consistent(stats))


def test_the_default_mapper_still_drops_the_long_makes(): 
    """DEFAULT BEHAVIOUR IS UNCHANGED, asserted rather than assumed: turning the
    derivation on by default would silently re-price the whole kicker board at
    the next scheduled ingest."""
    mapped = projections.map_sleeper_projection(_live_kicker_row())
    assert "fg_made_50_59" not in mapped
    assert mapped["fg_made_0_39"] == pytest.approx(0.90)
    assert mapped["fg_made_40_49"] == pytest.approx(0.48)
    assert mapped["fg_missed"] == pytest.approx(0.30)   # fga - fgm, still charged


def test_the_opt_in_mapper_prices_the_long_makes_at_the_house_rate():
    on = projections.map_sleeper_projection(_live_kicker_row(), derive_fg_50_plus=True)
    off = projections.map_sleeper_projection(_live_kicker_row())
    assert on["fg_made_50_59"] == pytest.approx(LIVE_UNBUCKETED)
    # The gain is exactly the house value of those makes — read from scoring.py,
    # never re-typed here (rule 2).
    one_long = scoring.score("K", {"fg_made_50_59": 1.0})
    assert scoring.score("K", on) - scoring.score("K", off) == pytest.approx(
        LIVE_UNBUCKETED * one_long
    )
    assert one_long > 0.0


def test_the_opt_in_mapper_never_double_counts_a_served_bucket():
    """A feed that serves `fgm_50p` and whose buckets then ADD UP must come
    through unchanged — the derivation is a residual, not an addition."""
    row = _live_kicker_row(stats={"fgm": 1.74, "fgm_50p": 0.36})
    on = projections.map_sleeper_projection(row, derive_fg_50_plus=True)
    off = projections.map_sleeper_projection(row)
    assert on["fg_made_50_59"] == pytest.approx(off["fg_made_50_59"])
    assert on["fg_made_50_59"] == pytest.approx(0.36)


def test_the_opt_in_mapper_adds_only_what_a_served_bucket_leaves_over():
    """And a feed that serves a 50-59 bucket but still under-accounts (say it
    starts serving 50-59 alone while 60+ makes keep landing in neither) gets the
    EXCESS, once."""
    row = _live_kicker_row(stats={"fgm": 2.10, "fgm_50p": 0.36})
    mapped = projections.map_sleeper_projection(row, derive_fg_50_plus=True)
    assert mapped["fg_made_50_59"] == pytest.approx(0.72)   # 0.36 served + 0.36 over


def test_the_derived_bucket_still_passes_the_strict_key_validator():
    mapped = projections.map_sleeper_projection(_live_kicker_row(), derive_fg_50_plus=True)
    projections.validate_projection_keys(mapped)


def test_the_default_ingest_reports_the_unpriced_makes_instead_of_losing_them(db):
    """The Rule-1-invisible half of the defect: with the derivation off the makes
    are still gone, but the run log now SAYS so."""
    with base.collect_drops() as tally:
        projections.ingest_projections(db, [_live_kicker_row()],
                                       retrieved_as_of="2026-08-30")
    assert tally["incomplete"] == 1
    row = db.execute("SELECT fg_made_50_59 FROM projections").fetchone()
    assert row["fg_made_50_59"] is None


def test_the_opt_in_ingest_stores_the_makes_and_reports_nothing_incomplete(db):
    with base.collect_drops() as tally:
        projections.ingest_projections(db, [_live_kicker_row()],
                                       retrieved_as_of="2026-08-30",
                                       derive_fg_50_plus=True)
    assert tally["incomplete"] == 0
    row = db.execute("SELECT fg_made_50_59 FROM projections").fetchone()
    assert row["fg_made_50_59"] == pytest.approx(LIVE_UNBUCKETED)


def test_a_batch_of_broken_kicker_rows_refuses_the_DERIVED_ingest(db):
    """require_columns precedent, scoped to the path that actually depends on the
    decomposition: with the derivation ON, a batch this module cannot decompose
    stores NOTHING rather than a board half-derived from noise."""
    bad = _live_kicker_row(stats={"fgm": 0.5})   # buckets over-run the total
    with pytest.raises(projections.KickerBucketMismatch) as exc:
        projections.ingest_projections(db, [bad], retrieved_as_of="2026-08-30",
                                       derive_fg_50_plus=True)
    assert "2021" in str(exc.value)   # names the seasons this is measured on
    assert db.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 0


def test_the_same_batch_ingests_normally_with_the_derivation_OFF(db):
    """THE REGRESSION THIS SCOPING PREVENTS. Raising on the default path would
    abort `ziggurat ingest backfill` for 2021, 2022 and 2024 — measured 3.9%,
    6.6% and 3.4% of their kicker rows are inconsistent — over rows that no
    stored value depends on (`fg_missed` is `fga - fgm`, which a bad bucket
    cannot touch). The rows must land, and be REPORTED."""
    bad = _live_kicker_row(stats={"fgm": 0.5})
    with base.collect_drops() as tally:
        n = projections.ingest_projections(db, [bad], retrieved_as_of="2026-08-30")
    assert n == 1
    assert tally["incomplete"] == 1
    row = db.execute("SELECT fg_missed, fg_made_50_59 FROM projections").fetchone()
    assert row["fg_missed"] == pytest.approx(1.54)   # fga 2.04 - fgm 0.50, unaffected
    assert row["fg_made_50_59"] is None


def test_a_few_broken_rows_in_a_clean_batch_do_not_refuse_the_derived_ingest(db):
    """The ceiling is a high-water mark, not a zero-tolerance gate: one bad row
    among sixty must not throw away fifty-nine good derivations."""
    rows = [_live_kicker_row(player_id=str(1000 + i)) for i in range(60)]
    rows.append(_live_kicker_row(player_id="9999", stats={"fgm": 0.5}))
    n = projections.ingest_projections(db, rows, retrieved_as_of="2026-08-30",
                                       derive_fg_50_plus=True)
    assert n == 61
    derived = db.execute(
        "SELECT COUNT(*) FROM projections WHERE fg_made_50_59 IS NOT NULL"
    ).fetchone()[0]
    assert derived == 60


def test_a_non_kicker_row_is_never_charged_with_a_bucket_mismatch(db):
    """The guard is scoped by the presence of `fgm`, not by position strings — a
    QB row has no made-FG total and must sail past it."""
    qb = _by_id(_raw_rows(), "3163")
    assert projections.unbucketed_fg_makes(qb["stats"]) is None
    with base.collect_drops() as tally:
        projections.ingest_projections(db, [qb], retrieved_as_of="2026-08-30")
    assert tally["incomplete"] == 0
