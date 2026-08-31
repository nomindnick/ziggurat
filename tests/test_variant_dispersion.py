"""Tests for the per-player dispersion variant (``draft/variant_dispersion.py``).

OFFLINE AND DETERMINISTIC. The board-driven cases drive the COMMITTED fixture
``tests/fixtures/draft/board-2026-08-30.json`` (the same 3,264-row live board
``test_draft_golden.py`` freezes) with a stubbed survival provider or the real
one at a small rollout count; the unit cases build tiny synthetic boards. No
network, no live database, no wall clock, all randomness from seeded
``random.Random`` — so the whole file is bit-for-bit reproducible.

THE LOAD-BEARING TEST IS :func:`test_legacy_mode_reproduces_the_engine_exactly`.
The variant does not recompute the engine's score, it takes a DIFFERENCE: it
subtracts the engine's own risk term and adds its own. That arithmetic depends on
four private facts about ``engine.recommend`` — the term's exact form, the
lineup-reachability treatment of a positive versus a negative component, the
tie-break order, and that ``risk_note`` is the LAST reason.

WHAT THE LEGACY IDENTITY CAN AND CANNOT CATCH, corrected (audit finding 6). The
first version of this file claimed the identity pins all four. It cannot pin two
of them, BY CONSTRUCTION: the re-score is ``pick_score - X + X``, so ANY wrong X
cancels. Mutating ``engine.py`` to ``rk = 2.0 * b_risk * ...``, or to apply the
reachability fraction to the negative branch too, left all 36 tests green while
MODE_PER_PLAYER silently subtracted the wrong term from every re-scored row.
:func:`test_the_engines_risk_term_has_the_form_this_variant_subtracts` closes
that: it perturbs ``engine.POSITIONAL_DISPERSION_PRIOR`` and measures how the
ENGINE'S OWN score responds, so the form is read off engine behaviour instead of
being assumed by an oracle that shares the assumption. The tie-break and the
risk-note-is-last facts are pinned separately below.

MUTATION-VERIFIED (2026-08-30, re-verified and extended 2026-08-31). Each of
these mutations was applied and the named test observed to FAIL:
  * drop ``frac`` from the positive branch of ``_effective``  ->
    ``test_the_reachability_discount_applies_to_a_positive_risk_term_only``
  * apply ``frac`` to the negative branch too                 -> same test
  * flip the sign of the ``old_rk`` subtraction               ->
    ``test_legacy_mode_reproduces_the_engine_exactly``
  * ``scale = measured_sd / legacy_sd`` (inverted)            ->
    ``test_the_rescale_matches_the_legacy_spread_over_the_drafted_cohort``
  * centre on the grand median instead of the positional one  ->
    ``test_centered_mode_reverts_to_shipped_behaviour_for_an_unjoined_player``
  * ``kdst_abstain`` ignored in the picker                    ->
    ``test_the_kdst_abstention_zeroes_the_risk_term_for_k_and_dst``
  * ``engine.py``: ``rk = 2.0 * b_risk * ...``                ->
    ``test_the_engines_risk_term_has_the_form_this_variant_subtracts``
  * ``engine.py``: ``+ rk * frac`` unconditionally            -> same test
  * drop the rescale (``return s * raw`` -> ``return raw``)   ->
    ``test_the_rescale_is_applied_to_the_score_at_the_default_scale``
  * ignore ``scale=`` (``effective_scale`` -> ``bands.scale``) ->
    ``test_the_scale_override_is_applied_and_is_not_the_bands_sd_match``
  * re-rank key reduced to ``(-score,)``                      ->
    ``test_the_rerank_breaks_a_new_cross_position_tie_on_vor_then_rank_then_id``
  * ``_rerank_key`` drops its vor leg                         -> same, and
    ``test_the_rerank_key_is_the_engines_total_order``
  * ``kdst_abstain`` ignored while FITTING the scale          ->
    ``test_the_kdst_abstention_is_applied_when_the_scale_is_fitted``
  * centre on the whole board instead of the drafted cohort   ->
    ``test_the_centring_constant_is_the_drafted_cohort_not_the_whole_board``
  * treat a frozen-prior row as a band                        ->
    ``test_a_dispersion_row_carrying_the_frozen_positional_prior_is_not_a_band``
  * market ROW floor disabled                                 ->
    ``test_a_measuring_mode_refuses_a_board_with_too_few_market_rows``
  * market COVERAGE floor disabled                            ->
    ``test_a_measuring_mode_refuses_a_board_with_too_little_market_coverage``
  * MODE_CENTERED's sentence falls through to the absolute one ->
    ``test_the_centered_note_compares_him_with_his_own_position_not_the_field``
  * disclosures dropped from ``PickRec.reasons``              ->
    ``test_a_degraded_build_discloses_itself_in_pick_reasons``
  * drop ``**engine_overrides`` from the picker's ``__init__`` ->
    ``test_the_picker_is_a_drop_in_where_posture_clones_the_engine``
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
import random
import statistics
from pathlib import Path

import pytest

from ziggurat.core import dispersion as dz
from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.draft import engine as eng
from ziggurat.draft import variant_dispersion as vd
from ziggurat.draft.bots import BoardEntry, PickContext, allowed_positions, position_counts
from ziggurat.draft.engine import PickEngine, SurvivalEstimate

FIXTURES = Path(__file__).parent / "fixtures" / "draft"
BOARD_FIXTURE = FIXTURES / "board-2026-08-30.json"

AS_OF = "2026-08-30"
SEASON = 2026


# --------------------------------------------------------------------- helpers


def _fixture_board() -> tuple[BoardEntry, ...]:
    payload = json.loads(BOARD_FIXTURE.read_text(encoding="utf-8"))
    return tuple(
        BoardEntry(
            player_id=str(r[0]),
            name=r[1],
            position=str(r[2]),
            espn_overall_rank=int(r[3]),
            house_points=float(r[4]),
            vor=float(r[5]),
            team=r[6],
        )
        for r in payload["rows"]
    )


def _row(
    *,
    key,
    position,
    relative,
    espn_id=None,
    gsis_id=None,
    team=None,
    source="market",
    market_sigma=10.0,
    weekly_noise=20.0,
    board_rank=1,
) -> dz.PlayerDispersion:
    """A minimal :class:`~ziggurat.core.dispersion.PlayerDispersion`.

    Only the fields this variant reads carry meaning; the rest are filled with
    self-consistent placeholders so the object is a real one and a future field
    addition fails here loudly rather than silently defaulting.
    """
    return dz.PlayerDispersion(
        key=key,
        player=str(key),
        position=position,
        team=team,
        gsis_id=gsis_id,
        espn_id=espn_id,
        projected_points=100.0,
        board_rank=board_rank,
        market_floor_points=80.0,
        market_floor_raw_points=80.0,
        market_floor_clamped=False,
        market_ceiling_points=120.0,
        market_band_points=40.0,
        market_sigma_points=market_sigma,
        market_join="espn",
        weekly_sigma=5.0,
        weekly_sigma_source="tier_measured",
        weekly_noise_sigma_points=weekly_noise,
        season_sigma_points=25.0,
        floor_points=75.0,
        ceiling_points=125.0,
        relative_dispersion=relative,
        relative_source=source,
        rank=None,
        reasons=("synthetic test row",),
    )


def _dboard(rows, *, banners=(), scrape_date="2026-08-28") -> dz.DispersionBoard:
    return dz.DispersionBoard(
        season=SEASON,
        as_of=AS_OF,
        weeks=tuple(range(1, 18)),
        ecr_type="rp",
        rank_units="positional",
        scrape_date=scrape_date,
        reference_band=53.44,
        reference_source="cohort_median",
        rows={r.key: r for r in rows},
        curves={},
        coverage=(),
        banners=tuple(banners),
    )


def _entry(pid, pos, rank, *, vor=100.0, points=200.0, team=None) -> BoardEntry:
    return BoardEntry(
        player_id=pid,
        name=f"{pos} {pid}",
        position=pos,
        espn_overall_rank=rank,
        house_points=points,
        vor=vor,
        team=team,
    )


def _flat_survival(_ctx, *, candidates, positions, rng):
    """A deterministic stub: everyone survives at 0.5, no VONA anywhere.

    Keeps the unit tests off ``survival.py`` entirely, so a failure here is about
    THIS module. The engine's own contract test covers the real provider.
    """
    return SurvivalEstimate(
        survival={c.player_id: 0.5 for c in candidates},
        next_best_vor={p: 0.0 for p in positions},
    )


def _stub_engine(**kw) -> PickEngine:
    return PickEngine(survival=_flat_survival, **kw)


# ===================================================================
#  1. the control: legacy mode is the engine, bit for bit
# ===================================================================


@pytest.mark.parametrize("round_num,overall", [(1, 9), (2, 12), (4, 32), (9, 89), (13, 129), (16, 152)])
def test_legacy_mode_reproduces_the_engine_exactly(round_num, overall):
    """THE proof that the difference-based re-score is arithmetically right.

    A variant whose dispersion function returns the legacy positional value must
    be indistinguishable from ``PickEngine`` — same players, same scores to the
    last bit, same reasons — because ``- old_risk + new_risk`` cancels exactly.
    Any drift in the term's form, in the reachability treatment, or in the sign
    of the subtraction shows up here first.
    """
    board = _fixture_board()
    bands = vd.risk_bands_from_dispersion(_dboard([_row(key=("x",), position="RB", relative=1.0)]), board)
    engine = _stub_engine()
    variant = vd.DispersionRiskPicker(bands=bands, engine=engine, mode=vd.MODE_LEGACY)

    def ctx():
        return PickContext.from_board(
            board, round=round_num, overall_pick=overall, team_slot=8,
            rng=random.Random(11), rounds_total=16,
        )

    a = engine.recommend(ctx(), top=5)
    b = variant.recommend(ctx(), top=5)
    assert [r.player_id for r in a] == [r.player_id for r in b]
    for x, y in zip(a, b, strict=True):
        assert x.pick_score == pytest.approx(y.pick_score, abs=1e-12)
        assert x.reasons == y.reasons
        assert x.risk_note == y.risk_note
        assert x.alternatives == y.alternatives
    assert engine.pick(ctx()) == variant.pick(ctx())


def test_legacy_mode_reproduces_the_engine_under_the_real_survival_rollout():
    """The identity is not an artefact of the stub provider."""
    board = _fixture_board()
    bands = vd.risk_bands_from_dispersion(_dboard([_row(key=("x",), position="RB", relative=1.0)]), board)
    engine = PickEngine(rollouts=32)
    variant = vd.DispersionRiskPicker(bands=bands, engine=engine, mode=vd.MODE_LEGACY)

    def ctx():
        return PickContext.from_board(
            board, round=1, overall_pick=9, team_slot=8, rng=random.Random(3), rounds_total=16
        )

    a, b = engine.recommend(ctx(), top=5), variant.recommend(ctx(), top=5)
    assert [r.player_id for r in a] == [r.player_id for r in b]
    assert [r.pick_score for r in a] == [r.pick_score for r in b]


def test_recommend_consumes_the_rng_identically_to_the_engine():
    """A variant that burned a different amount of randomness would desynchronise
    every rival draw after it — ``evaluate`` measured that as 0.66 expected wins
    of pure noise. The engine draws ONE 64-bit child per decision regardless of
    ``top``, and so must this."""
    board = _fixture_board()
    dboard = _dboard([_row(key=(e.player_id,), position=e.position, relative=1.7,
                           espn_id=e.player_id, team=e.team) for e in board[:200]])
    bands = vd.risk_bands_from_dispersion(dboard, board)
    engine = _stub_engine()
    variant = vd.DispersionRiskPicker(bands=bands, engine=engine, mode=vd.MODE_PER_PLAYER)

    r1, r2 = random.Random(5), random.Random(5)
    engine.recommend(
        PickContext.from_board(board, round=1, overall_pick=9, rng=r1, rounds_total=16), top=5
    )
    variant.recommend(
        PickContext.from_board(board, round=1, overall_pick=9, rng=r2, rounds_total=16), top=5
    )
    assert r1.getstate() == r2.getstate()


# ===================================================================
#  2. the arithmetic, pinned independently of the engine
# ===================================================================


def _independent_score(rec, ctx, *, engine, new_disp):
    """Re-derive the expected variant score from the engine's published pieces,
    written out longhand so it cannot share a bug with the implementation."""
    frac = eng._value_fraction(rec.position, position_counts(ctx.own_roster), ctx.roster)
    sign = eng.risk_sign(ctx.round)
    old_rk = engine.b_risk * sign * eng.POSITIONAL_DISPERSION_PRIOR.get(rec.position, 0.0)
    new_rk = engine.b_risk * sign * new_disp
    old_eff = old_rk * frac if old_rk > 0 else old_rk
    new_eff = new_rk * frac if new_rk > 0 else new_rk
    return rec.pick_score - old_eff + new_eff


def test_the_rescore_is_the_engine_score_minus_its_risk_term_plus_ours():
    board = _fixture_board()
    dboard = _dboard(
        [_row(key=(pid,), position=e.position, relative=1.8, espn_id=pid, team=e.team)
         for pid, e in ((e.player_id, e) for e in board[:400])]
    )
    bands = vd.risk_bands_from_dispersion(dboard, board)
    engine = _stub_engine()
    variant = vd.DispersionRiskPicker(
        bands=bands, engine=engine, mode=vd.MODE_PER_PLAYER, scale=0.5
    )
    base_ctx = PickContext.from_board(
        board, round=1, overall_pick=9, team_slot=8, rng=random.Random(11), rounds_total=16
    )
    base = engine.recommend(
        PickContext.from_board(
            board, round=1, overall_pick=9, team_slot=8, rng=random.Random(11), rounds_total=16
        ),
        top=vd.RERANK_WIDTH,
    )
    got = {r.player_id: r.pick_score for r in variant.recommend(base_ctx, top=vd.RERANK_WIDTH)}
    assert got, "the variant returned nothing to check"
    for rec in base:
        if rec.player_id not in got:
            continue
        disp, _raw, _src = variant._dispersion_for(rec.player)
        assert got[rec.player_id] == pytest.approx(
            _independent_score(rec, base_ctx, engine=engine, new_disp=disp), abs=1e-9
        )


def test_the_reachability_discount_applies_to_a_positive_risk_term_only():
    """``engine.recommend`` discounts only POSITIVE components by the
    lineup-reachability fraction — "a discount must never make a player score
    better". A late round makes the risk term positive, an early round negative,
    and a saturated QB slot makes ``frac`` 0.25; the two branches must be treated
    differently or the sign convention has been lost."""
    board = tuple(
        [_entry(f"QB{i}", "QB", i, vor=90.0 - i) for i in range(1, 4)]
        + [_entry(f"RB{i}", "RB", 10 + i, vor=80.0 - i) for i in range(1, 6)]
        + [_entry(f"WR{i}", "WR", 20 + i, vor=70.0 - i) for i in range(1, 6)]
        + [_entry(f"TE{i}", "TE", 30 + i, vor=60.0 - i) for i in range(1, 4)]
    )
    dboard = _dboard([_row(key=(e.player_id,), position=e.position, relative=2.0,
                           espn_id=e.player_id) for e in board])
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    engine = _stub_engine()
    variant = vd.DispersionRiskPicker(
        bands=bands, engine=engine, mode=vd.MODE_PER_PLAYER, scale=1.0
    )
    own = [board[0]]  # QB1 already drafted -> a second QB cannot reach the lineup
    assert eng._value_fraction("QB", position_counts(own), DEFAULT_ROSTER) < 1.0

    for round_num in (1, 13):
        def ctx(_r=round_num):
            return PickContext.from_board(
                board, own_roster=own, taken=[board[0].player_id], round=_r,
                overall_pick=_r * 10, rng=random.Random(2), rounds_total=16,
            )
        base = {r.player_id: r for r in engine.recommend(ctx(), top=vd.RERANK_WIDTH)}
        got = {r.player_id: r for r in variant.recommend(ctx(), top=vd.RERANK_WIDTH)}
        qbs = [pid for pid in base if pid.startswith("QB")]
        assert qbs, f"round {round_num}: no QB candidate to exercise the discount on"
        c = ctx()
        for pid in qbs:
            disp, _raw, _src = variant._dispersion_for(base[pid].player)
            assert got[pid].pick_score == pytest.approx(
                _independent_score(base[pid], c, engine=engine, new_disp=disp), abs=1e-9
            )


def test_the_engine_still_appends_risk_note_last():
    """``_replace_risk_reason`` swaps the LAST reason. If ``engine._build_rec``
    ever reorders, this rots loudly instead of leaving a stale sentence in a live
    recommendation."""
    board = _fixture_board()
    engine = _stub_engine()
    for round_num in (1, 5, 12, 16):
        recs = engine.recommend(
            PickContext.from_board(
                board, round=round_num, overall_pick=round_num, rng=random.Random(1), rounds_total=16
            ),
            top=5,
        )
        for r in recs:
            assert r.reasons[-1] == r.risk_note
            assert r.reasons.count(r.risk_note) == 1


def test_the_rerank_uses_the_engines_tie_break_order():
    """Two candidates on identical scores must break on vor, then ESPN rank, then
    id — never on the order the engine happened to hand them over."""
    board = tuple(
        [_entry("a", "RB", 5, vor=50.0), _entry("b", "RB", 3, vor=50.0),
         _entry("c", "RB", 4, vor=60.0)]
        + [_entry(f"WR{i}", "WR", 40 + i, vor=10.0) for i in range(1, 4)]
        + [_entry("QB1", "QB", 60, vor=5.0), _entry("TE1", "TE", 61, vor=5.0)]
    )
    dboard = _dboard([_row(key=(e.player_id,), position="RB", relative=1.0, espn_id=e.player_id)
                      for e in board if e.position == "RB"])
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    variant = vd.DispersionRiskPicker(
        bands=bands, engine=_stub_engine(), mode=vd.MODE_PER_PLAYER, scale=1.0
    )
    recs = variant.recommend(
        PickContext.from_board(board, round=1, overall_pick=1, rng=random.Random(0), rounds_total=16),
        top=vd.RERANK_WIDTH,
    )
    keys = [(-r.pick_score, -r.vor, r.player.espn_overall_rank, r.player_id) for r in recs]
    assert keys == sorted(keys)


# ===================================================================
#  3. the rescale
# ===================================================================


def test_the_rescale_matches_the_legacy_spread_over_the_drafted_cohort():
    """``scale * measured`` must have EXACTLY the spread the legacy proxy had over
    the same players — that is the whole content of "rescale explicitly"."""
    rng = random.Random(4)
    board = []
    rows = []
    for i in range(1, 121):
        pos = ("RB", "WR", "TE", "QB")[i % 4]
        pid = f"p{i}"
        board.append(_entry(pid, pos, i, vor=100.0 - i))
        rows.append(_row(key=(pid,), position=pos, relative=rng.uniform(0.1, 2.5), espn_id=pid))
    board = tuple(board)
    bands = vd.risk_bands_from_dispersion(_dboard(rows), board, drafted_depth=120)

    cohort = [e for e in board if e.espn_overall_rank <= 120]
    legacy = [eng.POSITIONAL_DISPERSION_PRIOR.get(e.position, 0.0) for e in cohort]
    scaled = [bands.scale * bands.values[e.player_id] for e in cohort]
    assert bands.scale_source == "cohort_sd_match"
    assert statistics.pstdev(scaled) == pytest.approx(statistics.pstdev(legacy), rel=1e-9)
    # ... and it is a REDUCTION here, not an amplification.
    assert 0.0 < bands.scale < 1.0


def test_a_thin_cohort_falls_back_to_the_frozen_anchor_and_says_so():
    board = tuple(_entry(f"p{i}", "RB", i, vor=10.0) for i in range(1, 6))
    rows = [_row(key=(e.player_id,), position="RB", relative=1.0 + 0.1 * i, espn_id=e.player_id)
            for i, e in enumerate(board)]
    bands = vd.risk_bands_from_dispersion(_dboard(rows), board, min_cohort=40)
    assert bands.scale == vd.SD_MATCH_SCALE_ANCHOR
    assert bands.scale_source == "frozen_anchor"
    assert any("FROZEN anchor" in b for b in bands.banners), bands.banners


def test_a_zero_spread_cohort_falls_back_rather_than_dividing_by_zero():
    board = tuple(_entry(f"p{i}", "RB", i, vor=10.0) for i in range(1, 61))
    rows = [_row(key=(e.player_id,), position="RB", relative=1.0, espn_id=e.player_id)
            for e in board]
    bands = vd.risk_bands_from_dispersion(_dboard(rows), board)
    assert bands.measured_sd == 0.0
    assert bands.scale_source == "frozen_anchor"


def test_the_naive_multiple_is_the_unrescaled_substitution():
    """``NAIVE_SCALE_MULTIPLE`` must be exactly what turns the sd-matched tilt
    back into feeding ``relative_dispersion`` in raw."""
    assert vd.SD_MATCH_SCALE_ANCHOR * vd.NAIVE_SCALE_MULTIPLE == pytest.approx(1.0)


# ===================================================================
#  4. the K/DST decision
# ===================================================================


def _kdst_board():
    board = tuple(
        [_entry(f"RB{i}", "RB", i, vor=90.0 - i) for i in range(1, 6)]
        + [_entry(f"WR{i}", "WR", 10 + i, vor=80.0 - i) for i in range(1, 6)]
        + [_entry(f"TE{i}", "TE", 20 + i, vor=70.0 - i) for i in range(1, 4)]
        + [_entry(f"QB{i}", "QB", 30 + i, vor=60.0 - i) for i in range(1, 4)]
        + [_entry(f"K{i}", "K", 40 + i, vor=50.0 - i) for i in range(1, 4)]
        + [_entry("DST:SF", "DST", 50, vor=55.0, team="SF"),
           _entry("DST:BAL", "DST", 51, vor=54.0, team="BAL")]
    )
    rows = []
    for e in board:
        if e.position == "DST":
            rows.append(_row(key=(e.player_id,), position="DST", relative=0.86, team=e.team))
        else:
            rows.append(_row(key=(e.player_id,), position=e.position,
                             relative=1.9 if e.position == "K" else 1.2, espn_id=e.player_id))
    return board, _dboard(rows)


def test_the_kdst_abstention_zeroes_the_risk_term_for_k_and_dst():
    """The legacy 0.0 at K/DST is an ABSTENTION, not a measurement of zero. With
    ``kdst_abstain=True`` a K or D/ST candidate's score must be the ENGINE's
    score untouched, whatever band the market carries for him."""
    board, dboard = _kdst_board()
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    assert bands.kdst_abstain is True
    engine = _stub_engine(kdst_earliest_round=1)
    variant = vd.DispersionRiskPicker(
        bands=bands, engine=engine, mode=vd.MODE_PER_PLAYER, scale=1.0
    )
    for round_num in (1, 13):
        def ctx(_r=round_num):
            return PickContext.from_board(
                board, round=_r, overall_pick=_r, rng=random.Random(9), rounds_total=16
            )
        base = {r.player_id: r.pick_score for r in engine.recommend(ctx(), top=vd.RERANK_WIDTH)}
        got = {r.player_id: r.pick_score for r in variant.recommend(ctx(), top=vd.RERANK_WIDTH)}
        kdst = [p for p in got if p.startswith(("K", "DST"))]
        assert kdst, f"round {round_num}: no K/DST candidate reached the re-rank"
        for pid in kdst:
            assert got[pid] == pytest.approx(base[pid], abs=1e-12), pid


def test_pricing_kdst_moves_them_and_is_reachable():
    """The alternative is BUILT, not asserted away: with the abstention off, the
    same candidates move."""
    board, dboard = _kdst_board()
    bands = vd.risk_bands_from_dispersion(dboard, board, kdst_abstain=False, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    assert bands.kdst_abstain is False
    engine = _stub_engine(kdst_earliest_round=1)
    variant = vd.DispersionRiskPicker(
        bands=bands, engine=engine, mode=vd.MODE_PER_PLAYER, scale=1.0
    )

    def ctx():
        return PickContext.from_board(
            board, round=1, overall_pick=1, rng=random.Random(9), rounds_total=16
        )

    base = {r.player_id: r.pick_score for r in engine.recommend(ctx(), top=vd.RERANK_WIDTH)}
    got = {r.player_id: r.pick_score for r in variant.recommend(ctx(), top=vd.RERANK_WIDTH)}
    moved = [p for p in got if p.startswith(("K", "DST")) and abs(got[p] - base[p]) > 1e-9]
    assert moved, "priced K/DST did not move any score — the toggle is decorative"


def test_the_kdst_reason_says_the_abstention_out_loud():
    board, dboard = _kdst_board()
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    variant = vd.DispersionRiskPicker(
        bands=bands, engine=_stub_engine(kdst_earliest_round=1), mode=vd.MODE_PER_PLAYER
    )
    recs = variant.recommend(
        PickContext.from_board(board, round=1, overall_pick=1, rng=random.Random(9), rounds_total=16),
        top=vd.RERANK_WIDTH,
    )
    kdst = [r for r in recs if r.position in ("K", "DST")]
    assert kdst
    for r in kdst:
        assert "deliberately NOT scored" in r.risk_note
        assert r.reasons[-1] == r.risk_note


# ===================================================================
#  5. the join: refuse rather than guess
# ===================================================================


def test_a_position_conflict_is_refused_and_counted():
    board = tuple([_entry("77", "RB", 1, vor=50.0)] + [_entry(f"w{i}", "WR", 10 + i, vor=9.0)
                                                       for i in range(3)])
    dboard = _dboard([_row(key=("x",), position="WR", relative=2.4, espn_id="77")])
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    assert "77" not in bands.values
    assert any("different position" in b for b in bands.banners), bands.banners


def test_an_id_two_market_rows_both_claim_is_dropped_from_both():
    board = tuple([_entry("77", "RB", 1, vor=50.0)] + [_entry(f"w{i}", "WR", 10 + i, vor=9.0)
                                                       for i in range(3)])
    dboard = _dboard([
        _row(key=("a",), position="RB", relative=0.3, espn_id="77"),
        _row(key=("b",), position="RB", relative=2.4, espn_id="77"),
    ])
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    assert "77" not in bands.values
    assert any("ambiguous id" in b for b in bands.banners), bands.banners


def test_the_pos_rank_id_fallback_is_never_reconstructed():
    """``load_board`` keys an id-less player ``<POS>:<vor rank>``. That rank is
    the VOR board's own ordering and is not reconstructible from a market row, so
    a join must not be attempted — the labelled positional fallback is used."""
    board = tuple([_entry("RB:41", "RB", 1, vor=50.0)] + [_entry(f"w{i}", "WR", 10 + i, vor=9.0)
                                                          for i in range(3)])
    dboard = _dboard([_row(key=("RB", 41), position="RB", relative=2.4)])
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    assert bands.values == {}
    raw, source = bands.relative("RB:41", "RB")
    assert source == "frozen_positional_prior"
    assert raw == dz.POSITIONAL_BAND_PRIOR["RB"]


def test_a_dst_joins_on_its_team_key():
    board = tuple([_entry("DST:SF", "DST", 50, vor=30.0, team="SF")]
                  + [_entry(f"r{i}", "RB", i, vor=40.0) for i in range(1, 4)])
    dboard = _dboard([_row(key=("sf",), position="DST", relative=0.9, team="SF")])
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    assert bands.values["DST:SF"] == pytest.approx(0.9)


def test_an_unjoined_player_takes_the_positional_median_of_this_board():
    board = tuple([_entry(f"r{i}", "RB", i, vor=40.0) for i in range(1, 6)])
    dboard = _dboard([_row(key=(f"r{i}",), position="RB", relative=float(i), espn_id=f"r{i}")
                      for i in range(1, 4)])
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    raw, source = bands.relative("r5", "RB")
    assert source == "positional_median_fallback"
    assert raw == pytest.approx(statistics.median([1.0, 2.0, 3.0]))


def test_centered_mode_reverts_to_shipped_behaviour_for_an_unjoined_player():
    """The property that makes MODE_CENTERED safe: a player the market cannot
    reach centres to exactly 0, i.e. to the engine's own positional value.

    The WR rows exist to make the positional medians differ sharply (RB 2.0, WR
    5.0). Centering on a pooled median instead of the player's own position would
    then shift the unjoined RB off the engine's value, which is precisely the bug
    this pins — with one position on the board the two centrings coincide and the
    test would prove nothing."""
    board = tuple(
        [_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, 6)]
        + [_entry(f"w{i}", "WR", 10 + i, vor=80.0 - i) for i in range(1, 5)]
        + [_entry("QB1", "QB", 30, vor=40.0), _entry("TE1", "TE", 31, vor=39.0)]
    )
    dboard = _dboard(
        [_row(key=(f"r{i}",), position="RB", relative=float(i), espn_id=f"r{i}")
         for i in range(1, 4)]
        + [_row(key=(f"w{i}",), position="WR", relative=4.0 + i, espn_id=f"w{i}")
           for i in range(1, 4)]
    )
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    assert bands.positional_median["RB"] != bands.positional_median["WR"]
    engine = _stub_engine()
    variant = vd.DispersionRiskPicker(bands=bands, engine=engine, mode=vd.MODE_CENTERED, scale=1.0)

    def ctx():
        return PickContext.from_board(board, round=1, overall_pick=1, rng=random.Random(6),
                                      rounds_total=16)

    base = {r.player_id: r.pick_score for r in engine.recommend(ctx(), top=vd.RERANK_WIDTH)}
    got = {r.player_id: r.pick_score for r in variant.recommend(ctx(), top=vd.RERANK_WIDTH)}
    assert "r5" in got, "the unjoined RB never reached the re-rank"
    assert got["r5"] == pytest.approx(base["r5"], abs=1e-12)
    # ...while a JOINED player at the same position does move.
    joined = [p for p in ("r1", "r2", "r3") if p in got]
    assert joined and any(abs(got[p] - base[p]) > 1e-9 for p in joined)


# ===================================================================
#  6. Rule 6 — the recommendation explains itself
# ===================================================================


def test_every_reason_set_is_non_empty_and_names_the_risk_source():
    board = _fixture_board()
    # EVERY OTHER entry is given a market row, so both the per-player source and
    # the labelled fallback are guaranteed to appear in the same candidate sets.
    dboard = _dboard([_row(key=(e.player_id,), position=e.position, relative=1.7,
                           espn_id=e.player_id, team=e.team)
                      for i, e in enumerate(board[:400]) if i % 2 == 0])
    bands = vd.risk_bands_from_dispersion(dboard, board)
    variant = vd.DispersionRiskPicker(bands=bands, engine=_stub_engine(), mode=vd.MODE_PER_PLAYER)
    seen_market = seen_fallback = False
    for round_num in (1, 6, 10, 14):
        recs = variant.recommend(
            PickContext.from_board(board, round=round_num, overall_pick=round_num * 10,
                                   rng=random.Random(8), rounds_total=16),
            top=vd.RERANK_WIDTH,
        )
        assert recs
        for r in recs:
            assert r.reasons and all(isinstance(s, str) and s for s in r.reasons)
            assert r.reasons[-1] == r.risk_note
            note = r.risk_note
            if "labelled fallback" in note:
                seen_fallback = True
            elif "expert consensus panel" in note:
                seen_market = True
            elif r.position in ("K", "DST"):
                assert "deliberately NOT scored" in note
            else:  # pragma: no cover - a source with no sentence is the bug
                raise AssertionError(f"risk note names no source: {note!r}")
            assert "VONA" not in note and "sigma" not in note and "dispersion" not in note
    assert seen_market and seen_fallback


def test_the_note_flips_direction_between_an_early_and_a_late_round():
    board, dboard = _kdst_board()
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    variant = vd.DispersionRiskPicker(bands=bands, engine=_stub_engine(), mode=vd.MODE_PER_PLAYER)
    early = variant.recommend(
        PickContext.from_board(board, round=1, overall_pick=1, rng=random.Random(1), rounds_total=16)
    )[0]
    late = variant.recommend(
        PickContext.from_board(board, round=14, overall_pick=140, rng=random.Random(1), rounds_total=16)
    )[0]
    assert "counts against a player here" in early.risk_note
    assert "what you WANT" in late.risk_note


# ===================================================================
#  7. refusals, Rule 1 threading, determinism
# ===================================================================


def test_an_unknown_mode_is_refused():
    board = tuple(_entry(f"r{i}", "RB", i, vor=10.0) for i in range(1, 4))
    bands = vd.risk_bands_from_dispersion(
        _dboard([_row(key=("r1",), position="RB", relative=1.0, espn_id="r1")]),
        board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0,
    )
    with pytest.raises(vd.VariantInputError, match="unknown dispersion mode"):
        vd.DispersionRiskPicker(bands=bands, mode="per-player")


def test_an_empty_band_map_is_refused_for_a_measuring_mode_but_not_for_the_control():
    board = tuple(_entry(f"r{i}", "RB", i, vor=10.0) for i in range(1, 4))
    empty = vd.risk_bands_from_dispersion(_dboard([]), board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    with pytest.raises(vd.VariantInputError, match="EMPTY"):
        vd.DispersionRiskPicker(bands=empty, mode=vd.MODE_PER_PLAYER)
    vd.DispersionRiskPicker(bands=empty, mode=vd.MODE_LEGACY)  # the control still runs


def test_an_empty_board_is_refused():
    with pytest.raises(vd.VariantInputError, match="empty board"):
        vd.risk_bands_from_dispersion(_dboard([]), ())


def test_build_risk_bands_takes_as_of_keyword_only_with_no_default():
    """Rule 1's convention, checked structurally rather than by reading."""
    sig = inspect.signature(vd.build_risk_bands)
    p = sig.parameters["as_of"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty
    assert sig.parameters["season"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["view"].default == "historical"


def test_build_risk_bands_threads_as_of_and_view_without_widening_them(monkeypatch):
    """The gate is enforced inside ``dispersion.build_dispersion``; this layer's
    only job is to pass the caller's values through untouched."""
    seen = {}

    def spy(conn, **kw):
        seen.update(kw)
        return _dboard([_row(key=("r1",), position="RB", relative=1.0, espn_id="r1")])

    monkeypatch.setattr(vd.dz, "build_dispersion", spy)
    board = tuple(_entry(f"r{i}", "RB", i, vor=10.0) for i in range(1, 4))
    vd.build_risk_bands(
        object(), board, as_of="2025-11-02", season=2025, view="latest_truth", min_cohort=1
    )
    assert seen["as_of"] == "2025-11-02"
    assert seen["season"] == 2025
    assert seen["view"] == "latest_truth"


def test_the_variant_is_deterministic_and_never_offers_a_disallowed_position():
    board = _fixture_board()
    dboard = _dboard([_row(key=(e.player_id,), position=e.position, relative=0.4 + (i % 7) * 0.3,
                           espn_id=e.player_id, team=e.team)
                      for i, e in enumerate(board[:400])])
    bands = vd.risk_bands_from_dispersion(dboard, board)
    variant = vd.DispersionRiskPicker(bands=bands, engine=_stub_engine(), mode=vd.MODE_PER_PLAYER)
    own = [e for e in board[:2]]

    def ctx():
        return PickContext.from_board(
            board, own_roster=own, taken=[e.player_id for e in own], round=3, overall_pick=29,
            team_slot=8, rng=random.Random(21), rounds_total=16,
        )

    first = variant.recommend(ctx(), top=5)
    second = variant.recommend(ctx(), top=5)
    assert [(r.player_id, r.pick_score, r.reasons) for r in first] == [
        (r.player_id, r.pick_score, r.reasons) for r in second
    ]
    allowed = allowed_positions(
        position_counts(own), 13, DEFAULT_ROSTER, round_num=3,
        kdst_earliest_round=variant.engine.kdst_earliest_round,
    )
    assert allowed
    for r in first:
        assert r.position in allowed
        assert r.player_id not in {e.player_id for e in own}


def test_format_risk_bands_states_the_scale_its_source_and_the_kdst_policy():
    board = tuple(_entry(f"r{i}", "RB", i, vor=10.0) for i in range(1, 4))
    bands = vd.risk_bands_from_dispersion(
        _dboard([_row(key=("r1",), position="RB", relative=1.0, espn_id="r1")]),
        board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0,
    )
    text = vd.format_risk_bands(bands)
    assert "rescale" in text and bands.scale_source in text
    assert "ABSTAIN" in text
    assert "hypothesis: per_player_market_band" in text


def test_describe_quantities_refuses_a_cohort_it_cannot_characterise():
    board = tuple(_entry(f"r{i}", "RB", i, vor=10.0) for i in range(1, 4))
    with pytest.raises(vd.VariantInputError, match="nothing to characterise"):
        vd.describe_quantities(_dboard([]), board)


def test_describe_quantities_reproduces_the_docstrings_claim_shape():
    """Not the live numbers (no DB here) — the SHAPE: it separates the market
    component from the weekly-noise component and reports a within-position
    variance share in [0, 1]."""
    rng = random.Random(12)
    board, rows = [], []
    for i in range(1, 61):
        pos = ("RB", "WR", "TE", "QB")[i % 4]
        pid = f"p{i}"
        board.append(_entry(pid, pos, i, vor=100.0 - i))
        rows.append(_row(key=(pid,), position=pos, relative=rng.uniform(0.2, 2.4), espn_id=pid,
                         market_sigma=rng.uniform(4.0, 30.0), weekly_noise=rng.uniform(10.0, 45.0)))
    out = vd.describe_quantities(_dboard(rows), tuple(board), rank_depth=60)
    assert out["n"] == 60.0
    assert -1.0 <= out["pearson_market_vs_weekly_noise"] <= 1.0
    assert 0.0 <= out["within_position_variance_share"] <= 1.0
    assert math.isfinite(out["scale_sd_match"])


# ===================================================================
#  8. what the legacy identity CANNOT catch (audit finding 6)
# ===================================================================


def test_the_engines_risk_term_has_the_form_this_variant_subtracts(monkeypatch):
    """Read the engine's risk term off the ENGINE, not off an oracle that assumes it.

    The variant subtracts ``b_risk * risk_sign(round) * dispersion(pos)``, with the
    lineup-reachability fraction applied to that component only when it is
    POSITIVE. The legacy identity cannot verify either fact (``- X + X`` cancels
    whatever X is) and ``_independent_score`` re-derives the same formula, so it
    agrees with the bug. This measures the engine's RESPONSE instead: bump the
    positional prior at one position by a known delta and the engine's own score
    for a candidate there must move by exactly that much.

    Both branches are exercised on a roster whose QB slot is already filled, so
    ``frac`` is 0.25 and "frac applies to the positive branch only" has teeth.
    """
    board = tuple(
        [_entry(f"QB{i}", "QB", i, vor=90.0 - i) for i in range(1, 4)]
        + [_entry(f"RB{i}", "RB", 10 + i, vor=80.0 - i) for i in range(1, 6)]
        + [_entry(f"WR{i}", "WR", 20 + i, vor=70.0 - i) for i in range(1, 6)]
        + [_entry(f"TE{i}", "TE", 30 + i, vor=60.0 - i) for i in range(1, 4)]
    )
    engine = _stub_engine()
    own = [board[0]]  # QB1 already drafted -> a second QB cannot reach the lineup
    frac = eng._value_fraction("QB", position_counts(own), DEFAULT_ROSTER)
    assert 0.0 < frac < 1.0, "the discount must bite or this test proves nothing"

    delta = 0.25
    base_prior = dict(eng.POSITIONAL_DISPERSION_PRIOR)
    bumped = dict(base_prior, QB=base_prior["QB"] + delta)

    def scores(prior, round_num):
        monkeypatch.setattr(eng, "POSITIONAL_DISPERSION_PRIOR", dict(prior))
        ctx = PickContext.from_board(
            board, own_roster=own, taken=[board[0].player_id], round=round_num,
            overall_pick=round_num * 10, rng=random.Random(2), rounds_total=16,
        )
        return {r.player_id: r.pick_score for r in engine.recommend(ctx, top=vd.RERANK_WIDTH)}

    for round_num in (1, 13):
        sign = eng.risk_sign(round_num)
        before, after = scores(base_prior, round_num), scores(bumped, round_num)
        qbs = [p for p in before if p.startswith("QB") and p in after]
        assert qbs, f"round {round_num}: no QB candidate to measure the term on"
        raw_term = engine.b_risk * sign * delta
        # POSITIVE component -> discounted by frac; NEGATIVE -> never discounted.
        expected = raw_term * frac if raw_term > 0 else raw_term
        for pid in qbs:
            assert after[pid] - before[pid] == pytest.approx(expected, abs=1e-9), (
                f"round {round_num} ({pid}): the engine's risk term is not "
                f"b_risk * risk_sign * dispersion with the documented frac treatment"
            )
        # ...and the two treatments are genuinely distinguishable here.
        assert abs(raw_term - raw_term * frac) > 1e-6


# ===================================================================
#  9. the rescale is APPLIED, and ``scale=`` overrides it
#     (audit findings 3 and 10)
# ===================================================================


def _rescale_fixture(*, scale):
    """A board of joined RBs/WRs plus a picker at an explicit ``scale``."""
    board = tuple(
        [_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, 16)]
        + [_entry(f"w{i}", "WR", 20 + i, vor=80.0 - i) for i in range(1, 16)]
    )
    raws = {e.player_id: 0.2 + 0.3 * (i % 9) for i, e in enumerate(board)}
    dboard = _dboard([
        _row(key=(e.player_id,), position=e.position, relative=raws[e.player_id],
             espn_id=e.player_id)
        for e in board
    ])
    bands = vd.risk_bands_from_dispersion(
        dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0
    )
    engine = _stub_engine()
    picker = vd.DispersionRiskPicker(
        bands=bands, engine=engine, mode=vd.MODE_PER_PLAYER, scale=scale
    )
    return board, bands, engine, picker, raws


def _ctx_for(board, *, round_num=1):
    return PickContext.from_board(
        board, round=round_num, overall_pick=round_num, rng=random.Random(3), rounds_total=16
    )


def test_the_rescale_is_applied_to_the_score_at_the_default_scale():
    """``scale * raw``, not ``raw``. The rescale is this module's central design
    argument and the first version of this file never asserted the picker APPLIES
    it: deleting the multiplication from ``_dispersion_for`` left 36 tests green,
    because the only test that touched it pinned the band's scale VALUE and the
    score oracle read ``_dispersion_for`` back (circular).

    The oracle here computes ``bands.scale * raw`` from the band map directly, so
    a picker that forgets to scale disagrees with it.
    """
    board, bands, engine, picker, raws = _rescale_fixture(scale=None)
    assert bands.scale_source == "cohort_sd_match"
    assert 0.0 < bands.scale < 1.0, "the rescale must be a real reduction here"
    ctx = _ctx_for(board)
    base = {r.player_id: r for r in engine.recommend(_ctx_for(board), top=vd.RERANK_WIDTH)}
    got = {r.player_id: r.pick_score for r in picker.recommend(ctx, top=vd.RERANK_WIDTH)}
    assert got
    for pid, score in got.items():
        expected = _independent_score(
            base[pid], ctx, engine=engine, new_disp=bands.scale * raws[pid]
        )
        assert score == pytest.approx(expected, abs=1e-9), pid
        # and it is NOT the unscaled substitution
        unscaled = _independent_score(base[pid], ctx, engine=engine, new_disp=raws[pid])
        assert abs(expected - unscaled) > 1e-6


def test_the_scale_override_is_applied_and_is_not_the_bands_sd_match():
    """Every arm of the rescale sweep (naive / 4x / 8x / 16x) IS this argument.

    If ``effective_scale`` ignored ``scale=``, every swept arm would have measured
    the same configuration and the reported dose-response would have been an
    artefact with nothing failing. The check is a DIFFERENCE between two scales,
    which no property of ``_dispersion_for`` can satisfy vacuously.
    """
    lo, hi = 0.25, 0.75
    board, bands, engine, low, raws = _rescale_fixture(scale=lo)
    high = vd.DispersionRiskPicker(
        bands=bands, engine=engine, mode=vd.MODE_PER_PLAYER, scale=hi
    )
    assert low.effective_scale == lo and high.effective_scale == hi
    assert abs(bands.scale - lo) > 1e-6 and abs(bands.scale - hi) > 1e-6

    ctx = _ctx_for(board)
    a = {r.player_id: r.pick_score for r in low.recommend(_ctx_for(board), top=vd.RERANK_WIDTH)}
    b = {r.player_id: r.pick_score for r in high.recommend(ctx, top=vd.RERANK_WIDTH)}
    shared = sorted(set(a) & set(b))
    assert shared
    sign = eng.risk_sign(ctx.round)
    counts = position_counts(ctx.own_roster)
    for pid in shared:
        pos = next(e.position for e in board if e.player_id == pid)
        frac = eng._value_fraction(pos, counts, DEFAULT_ROSTER)
        rk_lo = engine.b_risk * sign * lo * raws[pid]
        rk_hi = engine.b_risk * sign * hi * raws[pid]
        expected = (rk_hi * frac if rk_hi > 0 else rk_hi) - (
            rk_lo * frac if rk_lo > 0 else rk_lo
        )
        assert b[pid] - a[pid] == pytest.approx(expected, abs=1e-9), pid
        assert abs(expected) > 1e-6, "the two scales must actually differ in score"


# ===================================================================
# 10. the re-rank's tie-break, on a tie the re-score CREATES
#     (audit findings 3 and 11)
# ===================================================================


class _FixedRecEngine:
    """A stand-in engine whose ``recommend`` returns hand-built recommendations.

    The picker reads only ``recommend`` and ``b_risk`` off its engine. Fixing the
    engine's output is the only way to construct the case the re-rank exists for
    and that the real engine can never hand over: two candidates at DIFFERENT
    positions whose scores tie ONLY AFTER re-scoring, where the incoming order
    (the engine's, on its own scores) disagrees with the tie-break. On any board
    the real engine produces, its output is already in tie-break order, so a
    stable sort on score alone reproduces it — which is why the original test
    could not fail for the property it named.
    """

    b_risk = eng.DEFAULT_B_RISK

    def __init__(self, recs):
        self._recs = tuple(recs)

    def recommend(self, ctx, *, top=5):  # noqa: ARG002 - deliberately state-free
        return self._recs[: max(1, top)]


def _fixed_rec(entry, score, vor):
    return eng.PickRec(
        player=entry,
        pick_score=score,
        vor=vor,
        survival_next=0.5,
        vona=0.0,
        need_note="synthetic need note",
        risk_note="synthetic risk note",
        divergence_note="synthetic divergence note",
        reasons=("synthetic need note", "synthetic risk note"),
    )


def test_the_rerank_breaks_a_new_cross_position_tie_on_vor_then_rank_then_id():
    """Ties created by the re-score must break on vor, then ESPN rank, then id.

    The four candidates are built to land on EXACTLY the same re-scored value
    (the test asserts that, or it would be testing nothing), with the engine's
    incoming order deliberately disagreeing with the tie-break at the very first
    position. A score-only sort returns the incoming order and fails here.
    """
    # (id, position, espn rank, vor, dispersion) — dispersions are exact binary
    # fractions so the arithmetic below is exact, not approximately exact.
    plan = [
        ("rb_a", "RB", 5, 20.0, 0.0),
        ("aa_qb", "QB", 1, 10.0, 0.5),
        ("te_c", "TE", 1, 10.0, 0.75),
        ("wr_b", "WR", 3, 10.0, 0.25),
    ]
    target = 102.0
    b_risk, sign = eng.DEFAULT_B_RISK, eng.risk_sign(1)
    assert sign == -1.0

    board = tuple(_entry(pid, pos, rank, vor=vor) for pid, pos, rank, vor, _d in plan)
    dboard = _dboard([
        _row(key=(pid,), position=pos, relative=d, espn_id=pid)
        for pid, pos, _rank, _vor, d in plan
    ])
    bands = vd.risk_bands_from_dispersion(
        dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0
    )

    entries = {e.player_id: e for e in board}
    recs = []
    for pid, pos, _rank, vor, d in plan:
        old_eff = b_risk * sign * eng.POSITIONAL_DISPERSION_PRIOR[pos]
        new_eff = b_risk * sign * d
        assert old_eff <= 0 and new_eff <= 0  # round 1: never discounted by frac
        recs.append(_fixed_rec(entries[pid], target + old_eff - new_eff, vor))
    # the ENGINE's own order, which is what it would hand over
    recs.sort(key=lambda r: (-r.pick_score, -r.vor, r.player.espn_overall_rank, r.player_id))
    assert [r.player_id for r in recs] == ["aa_qb", "te_c", "rb_a", "wr_b"]

    picker = vd.DispersionRiskPicker(
        bands=bands, engine=_FixedRecEngine(recs), mode=vd.MODE_PER_PLAYER, scale=1.0
    )
    ctx = _ctx_for(board)
    assert all(
        eng._value_fraction(p, position_counts(ctx.own_roster), ctx.roster) == 1.0
        for p in ("RB", "WR", "TE", "QB")
    )
    out = picker.recommend(ctx, top=vd.RERANK_WIDTH)
    assert {r.pick_score for r in out} == {target}, (
        "the four candidates must tie EXACTLY or this is not a tie-break test"
    )
    assert [r.player_id for r in out] == ["rb_a", "aa_qb", "te_c", "wr_b"]


def test_the_rerank_key_is_the_engines_total_order():
    """The extracted key, checked directly on a hostile input order."""
    # "x" has the WORST espn rank and the BEST vor, so vor must dominate rank or
    # the order changes; "y" and "z" tie on vor and break on rank.
    a = _fixed_rec(_entry("z", "RB", 5, vor=1.0), 5.0, 1.0)
    b = _fixed_rec(_entry("y", "RB", 2, vor=1.0), 5.0, 1.0)
    c = _fixed_rec(_entry("x", "RB", 9, vor=7.0), 5.0, 7.0)
    items = [(5.0, a, 0.0, "market"), (5.0, b, 0.0, "market"), (5.0, c, 0.0, "market")]
    assert [t[1].player_id for t in sorted(items, key=vd._rerank_key)] == ["x", "y", "z"]
    # ...and id is the last resort, on a genuine three-way tie.
    tie = [
        (5.0, _fixed_rec(_entry("b_id", "RB", 2, vor=1.0), 5.0, 1.0), 0.0, "market"),
        (5.0, _fixed_rec(_entry("a_id", "RB", 2, vor=1.0), 5.0, 1.0), 0.0, "market"),
    ]
    assert [t[1].player_id for t in sorted(tie, key=vd._rerank_key)] == ["a_id", "b_id"]


# ===================================================================
# 11. the K/DST policy also governs how the SCALE is fitted
#     (audit finding 3)
# ===================================================================


def _kdst_heavy_cohort():
    board, rows = [], []
    for i in range(1, 21):
        board.append(_entry(f"r{i}", "RB", i, vor=90.0 - i))
        rows.append(_row(key=(f"r{i}",), position="RB", relative=1.1 + 0.02 * i,
                         espn_id=f"r{i}"))
    for i in range(1, 21):
        board.append(_entry(f"k{i}", "K", 20 + i, vor=40.0 - i))
        # deliberately CLOSE to the skill bands: pricing them barely moves the
        # spread, abstaining (0.0) moves it a lot, so the two are far apart.
        rows.append(_row(key=(f"k{i}",), position="K", relative=1.30 + 0.01 * i,
                         espn_id=f"k{i}"))
    for i in range(1, 21):
        team = f"T{i:02d}"
        board.append(_entry(f"DST:{team}", "DST", 40 + i, vor=35.0 - i, team=team))
        rows.append(_row(key=(f"d{i}",), position="DST", relative=1.35 + 0.01 * i, team=team))
    return tuple(board), _dboard(rows)


def test_the_kdst_abstention_is_applied_when_the_scale_is_fitted():
    """``kdst_abstain`` governs the SD-MATCH, not only the picker.

    ``RiskBands``' own docstring says the policy lives on the bands because the
    SCALE was computed under it — but nothing tested that, and the live drafted
    cohort has one kicker and no defense, so the live board cannot show it. On a
    K/DST-heavy cohort, disabling the substitution inside the fit changes the
    measured spread and therefore the scale every pick is priced at.
    """
    board, dboard = _kdst_heavy_cohort()
    kw = dict(min_cohort=1, min_market_rows=1, min_market_coverage=0.0)
    abstained = vd.risk_bands_from_dispersion(dboard, board, kdst_abstain=True, **kw)
    priced = vd.risk_bands_from_dispersion(dboard, board, kdst_abstain=False, **kw)

    def spread(*, abstain):
        vals = []
        for e in board:
            if abstain and e.position in vd.KDST_POSITIONS:
                vals.append(eng.POSITIONAL_DISPERSION_PRIOR.get(e.position, 0.0))
            else:
                vals.append(abstained.values[e.player_id])
        return statistics.pstdev(vals)

    assert abstained.measured_sd == pytest.approx(spread(abstain=True), rel=1e-12)
    assert priced.measured_sd == pytest.approx(spread(abstain=False), rel=1e-12)
    assert abs(abstained.measured_sd - priced.measured_sd) > 0.1, (
        "the two policies must produce visibly different spreads here"
    )
    assert abs(abstained.scale - priced.scale) > 0.01


# ===================================================================
# 12. the centring constant is the DRAFTED cohort (audit findings 1, 4)
# ===================================================================


def test_the_centring_constant_is_the_drafted_cohort_not_the_whole_board():
    """MODE_CENTERED must centre on the same players the rescale is fitted on.

    Centring on the whole board while scaling on the drafted range is what put a
    positional tilt of up to 0.85 risk points into the mode whose entire purpose
    is to have none. Here the drafted RBs sit at 1.0 and the undraftable ones at
    3.0, so the two cohorts cannot be confused.
    """
    board = tuple(
        [_entry(f"in{i}", "RB", i, vor=90.0 - i) for i in range(1, 11)]
        + [_entry(f"out{i}", "RB", 100 + i, vor=20.0 - i) for i in range(1, 41)]
    )
    rows = (
        [_row(key=(f"in{i}",), position="RB", relative=1.0, espn_id=f"in{i}")
         for i in range(1, 11)]
        + [_row(key=(f"out{i}",), position="RB", relative=3.0, espn_id=f"out{i}")
           for i in range(1, 41)]
    )
    bands = vd.risk_bands_from_dispersion(
        _dboard(rows), board, drafted_depth=50, min_cohort=1,
        min_market_rows=1, min_market_coverage=0.0,
    )
    assert bands.positional_median["RB"] == pytest.approx(1.0)
    assert bands.positional_median_source["RB"] == "drafted_cohort"
    assert bands.positional_median_n["RB"] == 10
    # the whole-board median is a different number, so this cannot be a coincidence
    assert statistics.median(bands.values.values()) == pytest.approx(3.0)


def test_a_position_too_thin_inside_the_drafted_range_falls_back_and_says_so():
    """One drafted kicker is not a median. The live board has exactly that."""
    board = tuple(
        [_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, 21)]
        + [_entry("k1", "K", 30, vor=5.0)]
        + [_entry(f"k{i}", "K", 100 + i, vor=4.0) for i in range(2, 22)]
    )
    rows = (
        [_row(key=(f"r{i}",), position="RB", relative=1.2, espn_id=f"r{i}")
         for i in range(1, 21)]
        + [_row(key=("k1",), position="K", relative=0.05, espn_id="k1")]
        + [_row(key=(f"k{i}",), position="K", relative=0.9, espn_id=f"k{i}")
           for i in range(2, 22)]
    )
    bands = vd.risk_bands_from_dispersion(
        _dboard(rows), board, drafted_depth=50, min_cohort=1,
        min_market_rows=1, min_market_coverage=0.0,
    )
    assert bands.positional_median_source["RB"] == "drafted_cohort"
    assert bands.positional_median_source["K"] == "whole_board"
    assert bands.positional_median["K"] == pytest.approx(0.9)  # not the lone 0.05
    assert "whole_board" in vd.format_risk_bands(bands)


def test_centered_mode_prices_the_median_drafted_player_at_exactly_the_legacy_value():
    """The exact property that makes MODE_CENTERED position-neutral.

    With an odd number of drafted players at a position the median IS one of
    them, and his centred dispersion must be the shipped constant to the bit —
    on ANY scale. Under whole-board centring he is off it.
    """
    board = tuple(
        [_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, 12)]     # 11 drafted
        + [_entry(f"far{i}", "RB", 200 + i, vor=5.0) for i in range(1, 31)]  # far away
        + [_entry(f"w{i}", "WR", 20 + i, vor=70.0 - i) for i in range(1, 12)]
    )
    rows = (
        [_row(key=(f"r{i}",), position="RB", relative=float(i), espn_id=f"r{i}")
         for i in range(1, 12)]
        + [_row(key=(f"far{i}",), position="RB", relative=50.0, espn_id=f"far{i}")
           for i in range(1, 31)]
        + [_row(key=(f"w{i}",), position="WR", relative=0.5 + 0.1 * i, espn_id=f"w{i}")
           for i in range(1, 12)]
    )
    bands = vd.risk_bands_from_dispersion(
        _dboard(rows), board, drafted_depth=100, min_cohort=1,
        min_market_rows=1, min_market_coverage=0.0,
    )
    median_player = "r6"  # median of 1..11
    assert bands.values[median_player] == pytest.approx(bands.positional_median["RB"])
    entry = next(e for e in board if e.player_id == median_player)
    for scale in (0.25, 1.0, 4.0):
        picker = vd.DispersionRiskPicker(
            bands=bands, engine=_stub_engine(), mode=vd.MODE_CENTERED, scale=scale
        )
        disp, _raw, _src = picker._dispersion_for(entry)
        assert disp == pytest.approx(eng.POSITIONAL_DISPERSION_PRIOR["RB"], abs=1e-12)


def test_describe_centering_reports_the_residual_positional_tilt():
    """The residual is NOT zero (centring a median does not zero a mean), so it
    is measured and reported rather than claimed away."""
    board = tuple(
        [_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, 12)]
        + [_entry(f"w{i}", "WR", 20 + i, vor=70.0 - i) for i in range(1, 12)]
    )
    rows = (
        [_row(key=(f"r{i}",), position="RB", relative=float(i), espn_id=f"r{i}")
         for i in range(1, 12)]
        + [_row(key=(f"w{i}",), position="WR", relative=0.5 + 0.1 * i, espn_id=f"w{i}")
           for i in range(1, 12)]
    )
    bands = vd.risk_bands_from_dispersion(
        _dboard(rows), board, drafted_depth=100, min_cohort=1,
        min_market_rows=1, min_market_coverage=0.0,
    )
    out = vd.describe_centering(bands, board, scale=1.0)
    assert out["RB.n"] == 11.0 and out["WR.n"] == 11.0
    # symmetric ramp 1..11 about its median -> mean == median -> zero shift
    assert out["RB.mean_shift"] == pytest.approx(0.0, abs=1e-9)
    assert out["RB.risk_points"] == pytest.approx(0.0, abs=1e-9)
    assert set(out) == {
        f"{p}.{k}" for p in ("RB", "WR") for k in ("n", "mean_shift", "risk_points")
    }


# ===================================================================
# 13. only a MEASURED band is a band (audit finding 2 — the major)
# ===================================================================


def _prior_only_board(n=40):
    board = tuple(_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, n + 1))
    rows = [
        _row(key=(e.player_id,), position="RB", espn_id=e.player_id,
             relative=dz.POSITIONAL_BAND_PRIOR["RB"], source="positional_prior")
        for e in board
    ]
    return board, _dboard(rows)


def test_a_dispersion_row_carrying_the_frozen_positional_prior_is_not_a_band():
    """An empty or thin ECR scrape does not make ``build_dispersion`` fail — it
    makes every row the frozen six-number ``POSITIONAL_BAND_PRIOR`` constant. If
    those join like market rows, MODE_PER_PLAYER silently becomes the positional
    vector swap this module refuses to offer, at ~2x amplitude, with
    ``format_risk_bands`` printing 100% coverage.
    """
    board, dboard = _prior_only_board()
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1)
    assert bands.values == {}
    assert bands.prior_only_joined == len(board)
    assert bands.prior_only_drafted == len(board)
    # the HEADLINE coverage number is market coverage, and it is honest
    assert bands.drafted_coverage == 0.0
    assert bands.drafted_join_coverage == 1.0
    text = vd.format_risk_bands(bands)
    assert "MARKET-priced 0/" in text and "(0.0%)" in text
    # the scale does not inflate off a cohort of six constants
    assert bands.scale_source == "frozen_anchor"
    assert bands.scale == vd.SD_MATCH_SCALE_ANCHOR
    for mode in (vd.MODE_PER_PLAYER, vd.MODE_CENTERED):
        with pytest.raises(vd.VariantInputError, match="EMPTY"):
            vd.DispersionRiskPicker(bands=bands, mode=mode)
    vd.DispersionRiskPicker(bands=bands, mode=vd.MODE_LEGACY)  # the control still runs


def _partial_market_board(*, draftable, market):
    board = tuple(_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, draftable + 1))
    rows = [_row(key=(f"r{i}",), position="RB", relative=1.0 + 0.1 * i, espn_id=f"r{i}")
            for i in range(1, market + 1)]
    return board, _dboard(rows)


def test_a_measuring_mode_refuses_a_board_with_too_few_market_rows():
    """The ROW floor, pinned on its own: 15 of 40 draftable players clears the
    COVERAGE floor (37.5% > 25%) and still is not a measurement of this board."""
    board, dboard = _partial_market_board(draftable=40, market=15)
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1)
    assert bands.drafted_matched == 15 < vd.MIN_DRAFTED_MARKET_ROWS
    assert bands.drafted_coverage > vd.MIN_DRAFTED_MARKET_COVERAGE  # the other leg passes
    with pytest.raises(vd.VariantInputError, match="not enough MARKET-priced"):
        vd.DispersionRiskPicker(bands=bands, mode=vd.MODE_PER_PLAYER)


def test_a_measuring_mode_refuses_a_board_with_too_little_market_coverage():
    """The COVERAGE floor, pinned on its own: 30 rows clears the ROW floor and
    still covers only 15% of the players a draft will reach. Two legs, two tests —
    a board that failed both could not tell which one was doing the work."""
    board, dboard = _partial_market_board(draftable=200, market=30)
    bands = vd.risk_bands_from_dispersion(dboard, board, min_cohort=1)
    assert bands.drafted_matched == 30 >= vd.MIN_DRAFTED_MARKET_ROWS  # the other leg passes
    assert bands.drafted_coverage < vd.MIN_DRAFTED_MARKET_COVERAGE
    with pytest.raises(vd.VariantInputError, match="not enough MARKET-priced"):
        vd.DispersionRiskPicker(bands=bands, mode=vd.MODE_PER_PLAYER)
    # ...and the floors travel WITH the bands, so a caller who lowers them is on
    # the record rather than passing a guard that was never about this data.
    relaxed = vd.risk_bands_from_dispersion(
        dboard, board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0
    )
    vd.DispersionRiskPicker(bands=relaxed, mode=vd.MODE_PER_PLAYER)


# ===================================================================
# 14. a degraded build says so in the text a cockpit renders
#     (audit finding 7)
# ===================================================================


def _disclosure_fixture(*, scrape_date, min_cohort):
    board = tuple(_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, 41))
    rows = [_row(key=(e.player_id,), position="RB", relative=0.5 + 0.05 * i,
                 espn_id=e.player_id) for i, e in enumerate(board)]
    bands = vd.risk_bands_from_dispersion(
        _dboard(rows, scrape_date=scrape_date), board, min_cohort=min_cohort,
        min_market_rows=1, min_market_coverage=0.0,
    )
    return board, bands


def test_a_degraded_build_discloses_itself_in_pick_reasons():
    """Every honesty banner used to live only in ``format_risk_bands``, which a
    Phase-3 integrator wiring this behind a flag would never call. A build whose
    rescale fell back to the frozen anchor and whose market scrape is two days
    old must say both things in ``PickRec.reasons`` — and must NOT displace the
    risk sentence from last, which the cockpit and several tests read."""
    board, bands = _disclosure_fixture(scrape_date="2026-08-28", min_cohort=10_000)
    assert bands.scale_source == "frozen_anchor"
    assert bands.disclosures
    picker = vd.DispersionRiskPicker(
        bands=bands, engine=_stub_engine(), mode=vd.MODE_PER_PLAYER
    )
    recs = picker.recommend(_ctx_for(board), top=5)
    assert recs
    for r in recs:
        assert r.reasons[-1] == r.risk_note
        for d in bands.disclosures:
            assert d in r.reasons
        joined = " ".join(r.reasons)
        assert "frozen" in joined and "scrape from 2026-08-28" in joined
        assert "2 days before 2026-08-30" in joined


def test_a_clean_build_carries_no_disclosures_at_all():
    """Silence here is a positive claim: nothing about this build was degraded.
    A disclosure that always fires is one an operator learns to ignore."""
    board, bands = _disclosure_fixture(scrape_date=AS_OF, min_cohort=1)
    assert bands.scale_source == "cohort_sd_match"
    assert bands.drafted_coverage == 1.0
    assert bands.disclosures == ()
    engine = _stub_engine()
    picker = vd.DispersionRiskPicker(bands=bands, engine=engine, mode=vd.MODE_PER_PLAYER)
    base = engine.recommend(_ctx_for(board), top=5)
    got = picker.recommend(_ctx_for(board), top=5)
    assert [len(r.reasons) for r in got] == [len(r.reasons) for r in base]


def test_the_control_never_carries_a_disclosure_even_on_a_degraded_build():
    """MODE_LEGACY must stay byte-identical to the engine: it prices nothing off
    the bands, so it has nothing to disclose, and an extra reason would break the
    identity that the whole A/B rests on."""
    board, bands = _disclosure_fixture(scrape_date="2026-08-28", min_cohort=10_000)
    assert bands.disclosures
    engine = _stub_engine()
    control = vd.DispersionRiskPicker(bands=bands, engine=engine, mode=vd.MODE_LEGACY)
    a = engine.recommend(_ctx_for(board), top=5)
    b = control.recommend(_ctx_for(board), top=5)
    assert [r.reasons for r in a] == [r.reasons for r in b]
    assert [r.pick_score for r in a] == [r.pick_score for r in b]


# ===================================================================
# 15. MODE_CENTERED's sentence describes what its SCORE reads
#     (audit finding 4)
# ===================================================================


def test_the_centered_note_compares_him_with_his_own_position_not_the_field():
    """MODE_CENTERED scores ``raw - median(position)``. Quoting the ABSOLUTE band
    told four QBs inside the live top 160 "the experts agree unusually closely on
    him" while the score was penalising them for being WIDER than a typical QB.
    """
    board = tuple(
        [_entry(f"q{i}", "QB", i, vor=90.0 - i) for i in range(1, 12)]
        + [_entry(f"r{i}", "RB", 20 + i, vor=80.0 - i) for i in range(1, 12)]
    )
    # every QB band is "tight" in absolute terms (<= TIGHT_BAND) but q11 is the
    # widest OF THE QBs — the two readings disagree, which is the point.
    rows = (
        [_row(key=(f"q{i}",), position="QB", relative=0.20 + 0.04 * i, espn_id=f"q{i}")
         for i in range(1, 12)]
        + [_row(key=(f"r{i}",), position="RB", relative=1.5, espn_id=f"r{i}")
           for i in range(1, 12)]
    )
    bands = vd.risk_bands_from_dispersion(
        _dboard(rows), board, min_cohort=1, min_market_rows=1, min_market_coverage=0.0
    )
    assert bands.values["q11"] <= vd.TIGHT_BAND  # "unusually close" in absolute terms
    assert bands.values["q11"] > bands.positional_median["QB"]  # ...but wide for a QB

    picker = vd.DispersionRiskPicker(
        bands=bands, engine=_stub_engine(), mode=vd.MODE_CENTERED, scale=1.0
    )
    note = vd._variant_risk_note(
        "QB", 1, raw=bands.values["q11"], source="market", bands=bands,
        mode=vd.MODE_CENTERED,
    )
    assert "more split on him than on a typical QB" in note
    assert "agree unusually closely on him" not in note
    assert f"{bands.positional_median['QB']:.1f}x for a typical draftable QB" in note

    # the per-player mode keeps the absolute reading, because that is what IT scores
    per_player = vd._variant_risk_note(
        "QB", 1, raw=bands.values["q11"], source="market", bands=bands,
        mode=vd.MODE_PER_PLAYER,
    )
    assert "agree unusually closely on him" in per_player
    assert picker.mode == vd.MODE_CENTERED


def test_an_unmatched_player_in_centered_mode_is_told_his_pick_is_unchanged():
    board = tuple(_entry(f"r{i}", "RB", i, vor=90.0 - i) for i in range(1, 42))
    rows = [_row(key=(f"r{i}",), position="RB", relative=1.0 + 0.02 * i, espn_id=f"r{i}")
            for i in range(1, 41)]  # r41 is unmatched
    bands = vd.risk_bands_from_dispersion(_dboard(rows), board, min_cohort=1)
    note = vd._variant_risk_note(
        "RB", 1, raw=bands.positional_median["RB"], source="positional_median_fallback",
        bands=bands, mode=vd.MODE_CENTERED,
    )
    assert "unchanged by the expert range" in note
    assert "counts against a player here" not in note


# ===================================================================
# 16. substitutable where other modules CLONE the engine (audit finding 5)
# ===================================================================


def _posture_session(board, engine):
    import types

    return types.SimpleNamespace(
        complete=False,
        overall_pick=29,
        operator_slot=8,
        own_roster=list(board[:2]),
        opponent_rosters={t: [] for t in range(10) if t != 8},
        taken={e.player_id for e in board[:2]},
        board=list(board),
        roster=DEFAULT_ROSTER,
        rounds_total=16,
        pick_order=list(range(10)),
        engine=engine,
        session_seed=7,
    )


def _fixture_bands():
    board = _fixture_board()
    dboard = _dboard([
        _row(key=(e.player_id,), position=e.position, relative=0.4 + (i % 9) * 0.2,
             espn_id=e.player_id, team=e.team)
        for i, e in enumerate(board[:400])
    ])
    return board, vd.risk_bands_from_dispersion(dboard, board)


def test_the_picker_is_a_drop_in_where_posture_clones_the_engine():
    """``posture.project_postures`` does ``dataclasses.replace(session.engine,
    need_schedule=..., survival=...)`` OUTSIDE its seam guard, and both cockpits
    swallow the exception whole (``except Exception: return None``). A picker that
    is not replace-compatible therefore kills the posture tip SILENTLY the moment
    an integrator wires it in at ``session.engine`` — no error, nothing for the
    operator to notice. Measured before the fix: ``TypeError:
    DispersionRiskPicker.__init__() got an unexpected keyword argument
    'need_schedule'``.
    """
    from ziggurat.draft import posture

    board, bands = _fixture_bands()
    picker = vd.DispersionRiskPicker(
        bands=bands, engine=PickEngine(rollouts=4), mode=vd.MODE_CENTERED, scale=0.5
    )
    # 1. the attribute posture reads off session.engine
    assert picker.need_schedule is picker.engine.need_schedule
    assert picker.b_risk == picker.engine.b_risk

    # 2. the clone posture takes, with the variant's own configuration intact
    clone = dataclasses.replace(
        picker, need_schedule=eng.ARCHETYPE_NEED_SCHEDULES["zero_rb"], survival=_flat_survival
    )
    assert isinstance(clone, vd.DispersionRiskPicker)
    assert clone.bands is bands and clone.mode == vd.MODE_CENTERED and clone.scale == 0.5
    assert clone.engine.need_schedule is eng.ARCHETYPE_NEED_SCHEDULES["zero_rb"]
    assert clone.engine.survival is _flat_survival
    assert picker.engine.need_schedule is not clone.engine.need_schedule

    # 3. end to end: the real posture comparator runs against it
    proj = posture.project_postures(_posture_session(board, picker), rollouts=2)
    assert proj is not None
    assert isinstance(proj, posture.PostureProjection)
    assert proj.current_label == "balanced"


def test_an_engine_field_typo_is_refused_rather_than_silently_ignored():
    board, bands = _fixture_bands()
    with pytest.raises(vd.VariantInputError, match="not a field of the wrapped engine"):
        vd.DispersionRiskPicker(bands=bands, need_scheduel=None)


def test_delegation_does_not_invent_attributes_neither_object_has():
    board, bands = _fixture_bands()
    picker = vd.DispersionRiskPicker(bands=bands, engine=_stub_engine())
    missing = "no_such_attribute"          # not a constant, so ruff B009 is happy
    with pytest.raises(AttributeError, match="neither"):
        getattr(picker, missing)
