"""The "durable" draft variant — availability re-ranking + contingent (handcuff) value.

OFFLINE AND SYNTHETIC THROUGHOUT except two DB-guarded tests at the bottom, which
skip when ``db/ziggurat.sqlite`` is absent. Every player, club and id invented
here is fictional (Rule 5: the live board and the league's state never enter a
committed file), and the SHAPE is the real one — the board's ``player_id`` is an
ESPN id, the availability rows are keyed in that same space, and the handcuff map
is a starter -> backup pair on it.

WHAT THESE TESTS ARE FOR, in the order the module can hurt someone:

  * A SILENT DEFAULT. The whole variant is a discount, and a discount that
    silently fails to apply reads exactly like the unwrapped engine. So the
    tests assert the adjustment MOVES a decision, not merely that it runs.
  * A DISCOUNT WITH THE WRONG SIGN. Most of the late board carries NEGATIVE
    VOR; scaling those by an availability fraction would make a fragile player
    score BETTER. ``test_a_negative_vor_candidate_is_never_improved`` is the
    mutation guard.
  * A MISSING ROW READ AS A DURABLE PLAYER. ``fraction()`` returns ``None``, not
    1.0, and the recommendation says so out loud — naming the RIGHT cause of the
    three, because a discount that skips a club promotes it.
  * A PARTIAL SLATE. Hazard 3: a club on the board that the schedule has never
    heard of raises rather than quietly pricing its players as perfectly durable.
  * A REASON THAT SOUNDS MEASURED AND IS NOT. The row's own disclosure — the
    seasons his record covers, the shrink weight, the role floor, a CLIPPED
    multiplier, a season missed in full — is carried VERBATIM, and this module's
    own sentence names no window, sample or shrinkage it re-derived.
  * A DECORATIVE TEST. Three guards in the first cut of this file survived
    mutations of exactly what they were named for (the ``frac`` factor, the
    sampler's per-player keying, the content of a Rule-6 reason). Every test
    below that names a mutation was re-verified by applying it.
  * THE QB POLICY BECOMING INVISIBLE. It is a policy, not a measurement, and it
    must be disclosed on every QB row and inside the prior's own label.
  * DOUBLE COUNTING WITH ``core/dispersion.py``. The module must never read a
    dispersion number; the engine's own risk term rides through untouched.
  * DETERMINISM. The cockpit replays journals bit-for-bit, so the wrapper must
    consume ``ctx.rng`` exactly as the unwrapped engine does.

MUTATION-VERIFIED. Each test marked "(mutation:...)" was written by breaking the
shipped code in the named way and confirming it went red first.
"""

from __future__ import annotations

import ast
import inspect
import random
import sqlite3
from pathlib import Path

import pytest

from ziggurat.core import availability as av
from ziggurat.core import marginal as mg
from ziggurat.draft import grader
from ziggurat.draft import variant_durable as vd
from ziggurat.draft.bots import BoardEntry, PickContext
from ziggurat.draft.engine import PickEngine, SurvivalEstimate

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = REPO_ROOT / "db" / "ziggurat.sqlite"

WEEKS = tuple(range(1, 18))
GAME_WEEKS = tuple(w for w in WEEKS if w != 7)  # one bye, like a real slate


# --------------------------------------------------------------- fixtures


def _entry(pid, name, pos, rank, points, vor, team="AAA"):
    return BoardEntry(
        player_id=pid, name=name, position=pos, espn_overall_rank=rank,
        house_points=points, vor=vor, team=team,
    )


@pytest.fixture
def board():
    """A small, fully legal board: two of every skill position plus K/DST.

    The two running backs are the experiment: same rank neighbourhood, ``rb_iron``
    the LOWER projected of the two, so any reordering between them can only come
    from availability.
    """
    return (
        _entry("rb_glass", "Glass Back", "RB", 1, 260.0, 120.0, "AAA"),
        _entry("rb_iron", "Iron Back", "RB", 2, 250.0, 110.0, "BBB"),
        _entry("rb_backup", "Backup Back", "RB", 60, 90.0, -40.0, "AAA"),
        _entry("rb_spare", "Spare Back", "RB", 61, 88.0, -42.0, "CCC"),
        _entry("wr_one", "First Wide", "WR", 3, 240.0, 100.0, "CCC"),
        _entry("wr_two", "Second Wide", "WR", 4, 230.0, 90.0, "DDD"),
        _entry("wr_three", "Third Wide", "WR", 20, 200.0, 60.0, "EEE"),
        _entry("qb_one", "First Passer", "QB", 5, 320.0, 80.0, "EEE"),
        _entry("qb_two", "Second Passer", "QB", 40, 290.0, 50.0, "FFF"),
        _entry("te_one", "First End", "TE", 6, 180.0, 70.0, "FFF"),
        _entry("te_two", "Second End", "TE", 45, 150.0, 40.0, "GGG"),
        _entry("dst_one", "Alpha Defense", "DST", 100, 120.0, 20.0, "AAA"),
        _entry("k_one", "First Kicker", "K", 110, 140.0, 15.0, "BBB"),
        _entry("k_two", "Second Kicker", "K", 111, 130.0, 5.0, "CCC"),
    )


def _inputs(rows, *, starter_of=None, uplift_of=None, hc_reasons=None,
            qb_policy=vd.QB_POLICY_CAP, prior=None, unpriced=None,
            missing_clubs=(), board_rows=None):
    return vd.DurableInputs(
        availability=dict(rows),
        starter_of=dict(starter_of or {}),
        uplift_of=dict(uplift_of or {}),
        handcuff_reasons=dict(hc_reasons or {}),
        prior=prior or av.DEFAULT_DURABILITY,
        qb_policy=qb_policy,
        as_of="2026-08-30",
        season=2026,
        weeks=WEEKS,
        reasons=("test inputs",),
        priced=len(rows),
        from_player_history=0,
        board_rows=len(rows) if board_rows is None else board_rows,
        unpriced=dict(unpriced or {}),
        missing_clubs=tuple(missing_clubs),
        slate_clubs=32,
    )


def _priced(pid, pos, *, miss_rate, history=None, history_window=(2021, 2025),
            identified=True):
    """One availability row at ``miss_rate`` per game, byes already removed."""
    prior = av.DEFAULT_DURABILITY
    import dataclasses as _dc
    from types import MappingProxyType

    rates = dict(prior.season_miss_rate)
    rates[pos] = miss_rate
    opens = dict(prior.opening_absent)
    opens[pos] = 0.0
    tuned = _dc.replace(
        prior,
        season_miss_rate=MappingProxyType(rates),
        opening_absent=MappingProxyType(opens),
    )
    return av.player_availability(
        pid, pos, GAME_WEEKS, prior=tuned, history=history,
        history_window=history_window, identified=identified,
    )


def _history(*, games=60, missed=15, seasons=(2022, 2023, 2024, 2025),
             role_weeks=40, played=None, absent_seasons=()):
    """A real ``PlayerHistory``, so a row can come back on BASIS_PLAYER.

    Deliberately a career that starts in 2022: the shipped reason used to assert
    a "2021-2025" window for every player, which is false for exactly this shape.
    """
    return av.PlayerHistory(
        gsis_id="00-0000001", games=games, missed=missed, seasons=tuple(seasons),
        absent_seasons=tuple(absent_seasons), unknown_seasons=(),
        played=games - missed if played is None else played,
        role_weeks=role_weeks,
    )


class _Book:
    """The shape ``build_durable_inputs`` consumes, with nothing behind it.

    Carries the fields the disclosure reads (``season``, ``history_window``, the
    prior) so a fake book cannot pass while the real one's contract has moved.
    """

    prior = av.DEFAULT_DURABILITY
    as_of = "2026-08-30"
    season = 2026
    history_window = (2021, 2025)

    def __init__(self, slate=None):
        self.slate = {"AAA": GAME_WEEKS} if slate is None else dict(slate)

    def availability_for(self, key, position, team, weeks, *, gsis_id=None,
                         opening_absent=None):
        return _priced(key, position, miss_rate=0.2)


def _stub_survival(ctx, *, candidates, positions, rng):
    """A deterministic survival provider: everybody survives, no VONA anywhere.

    Keeps the ENGINE's score equal to ``vor * frac + need`` so a test can reason
    about what the wrapper added without the rollout in the way.
    """
    return SurvivalEstimate(
        survival={c.player_id: 1.0 for c in candidates},
        next_best_vor={p: 0.0 for p in positions},
    )


@pytest.fixture
def engine():
    return PickEngine(survival=_stub_survival)


def _ctx(board, *, own=(), taken=(), rnd=1, overall=1, slot=0):
    return PickContext.from_board(
        board,
        own_roster=[e for e in board if e.player_id in own],
        taken=taken,
        team_slot=slot,
        round=rnd,
        overall_pick=overall,
        rng=random.Random(7),
    )


# ================================================================ the policy


def test_the_qb_cap_borrows_a_measured_rate_and_never_invents_one():
    capped = vd.qb_policy_prior(policy=vd.QB_POLICY_CAP)
    raw = vd.qb_policy_prior(policy=vd.QB_POLICY_RAW)
    assert raw is av.DEFAULT_DURABILITY
    ref = max(
        av.DEFAULT_DURABILITY.season_miss_rate[p]
        for p in vd.UNCONTAMINATED_POSITIONS
    )
    assert capped.season_miss_rate["QB"] == pytest.approx(ref)
    # every OTHER position is untouched, to the bit
    for pos, rate in av.DEFAULT_DURABILITY.season_miss_rate.items():
        if pos != "QB":
            assert capped.season_miss_rate[pos] == rate
    # the multiplier's denominator is deliberately NOT capped, so a player's own
    # record keeps doing exactly the work it did before.
    assert dict(capped.prior_history_rate) == dict(av.DEFAULT_DURABILITY.prior_history_rate)


def test_the_cap_actually_binds_on_the_shipped_prior():
    """If the shipped QB rate ever falls below the reference this policy is a
    no-op, and a no-op policy that still prints a disclosure is a lie."""
    assert (
        av.DEFAULT_DURABILITY.season_miss_rate["QB"]
        > max(av.DEFAULT_DURABILITY.season_miss_rate[p] for p in vd.UNCONTAMINATED_POSITIONS)
    )
    capped = vd.qb_policy_prior(policy=vd.QB_POLICY_CAP)
    assert capped.season_miss_rate["QB"] < av.DEFAULT_DURABILITY.season_miss_rate["QB"]


def test_the_capped_prior_discloses_the_policy_in_its_own_label():
    """(mutation: dropping the label rewrite) — every reason line the prior
    renders must carry the policy, so a QB row cannot read as measured."""
    capped = vd.qb_policy_prior(policy=vd.QB_POLICY_CAP)
    assert "POLICY" in capped.label and vd.QB_POLICY_CAP in capped.label
    assert "POLICY" in capped.describe("QB")


def test_an_unknown_qb_policy_is_refused():
    with pytest.raises(vd.DurableVariantError):
        vd.qb_policy_prior(policy="whatever")


# ============================================================ the adjustment


def test_availability_reorders_two_backs_the_engine_calls_equal_enough():
    """THE POINT OF THE MODULE. ``rb_glass`` outprojects ``rb_iron`` by 10 VOR
    and the engine takes him; make him fragile enough and the durable variant
    takes the iron man instead.

    (mutation: ``b_availability=0`` — the assertion below pins that the flip is
    the availability term and not an accident of ordering.)"""
    b = (
        _entry("rb_glass", "Glass Back", "RB", 1, 260.0, 120.0, "AAA"),
        _entry("rb_iron", "Iron Back", "RB", 2, 250.0, 110.0, "BBB"),
    )
    eng = PickEngine(survival=_stub_survival)
    rows = {
        "rb_glass": _priced("rb_glass", "RB", miss_rate=0.35),
        "rb_iron": _priced("rb_iron", "RB", miss_rate=0.02),
    }
    inp = _inputs(rows)
    assert eng.recommend(_ctx(b), top=1)[0].player_id == "rb_glass"
    dur = vd.DurablePicker(engine=eng, inputs=inp)
    assert dur.pick(_ctx(b)) == "rb_iron"
    off = vd.DurablePicker(engine=eng, inputs=inp, b_availability=0.0)
    assert off.pick(_ctx(b)) == "rb_glass"


def test_the_adjusted_value_is_exactly_frac_times_vor_times_availability(board, engine):
    """The arithmetic a reader can check against the reasons."""
    rows = {"rb_glass": _priced("rb_glass", "RB", miss_rate=0.25)}
    dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    ctx = _ctx(board)
    adj = {a.player_id: a for a in dur.adjustments(ctx)}["rb_glass"]
    share = adj.fraction
    assert share is not None
    expected = -(1.0 - share) * 120.0 * adj.frac_lineup
    assert adj.availability == pytest.approx(expected)
    assert adj.delta == pytest.approx(adj.availability + adj.contingent)


def test_a_negative_vor_candidate_is_never_improved(board, engine):
    """(mutation: dropping the ``rec.vor > 0`` guard.) On the live board most of
    the late rounds carry NEGATIVE VOR; scaling those by availability would make
    a fragile player score BETTER, which is the one thing a discount must never
    do."""
    rows = {
        pid: _priced(pid, pos, miss_rate=0.35)
        for pid, pos in (("rb_backup", "RB"), ("rb_spare", "RB"))
    }
    dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    ctx = _ctx(board, own=("rb_glass", "rb_iron", "wr_one", "wr_two", "te_one"), rnd=6)
    for adj in dur.adjustments(ctx):
        assert adj.availability <= 0.0
        assert adj.delta <= adj.contingent + 1e-12


def test_an_unpriced_candidate_is_not_silently_treated_as_durable(board, engine):
    """A missing availability row must read as UNKNOWN, and a positive-value
    candidate priced at full durability must say so (Rule 6)."""
    dur = vd.DurablePicker(engine=engine, inputs=_inputs({}))
    ctx = _ctx(board)
    adj = {a.player_id: a for a in dur.adjustments(ctx)}
    top = dur.recommend(ctx, top=1)[0]
    assert adj[top.player_id].fraction is None
    assert adj[top.player_id].delta == 0.0
    assert any("as if he plays every week" in r for r in top.reasons)


def test_every_priced_recommendation_states_its_durability(board, engine):
    """(mutation: replacing the whole sentence with "Durability: this board
    applied an availability discount.") The prefix is not the content: a Rule-6
    reason has to carry the games, the share, the basis and what it COST him, or
    a novice cannot tell a 5-point haircut from a 50-point one."""
    rows = {e.player_id: _priced(e.player_id, e.position, miss_rate=0.2)
            for e in board if e.position != "DST"}
    dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    for rec in dur.recommend(_ctx(board), top=5):
        line = next((r for r in rec.reasons if r.startswith("Durability:")), None)
        assert line is not None, rec.player_id
        row = rows[rec.player_id]
        games = len(row.game_weeks)
        share = row.expected_games_played / games
        assert f"{row.expected_games_played:.1f} of his club's {games} games" in line
        assert f"({100 * share:.0f}%)" in line
        assert "position prior" in line or "HIS OWN record" in line
        if rec.vor > 0.0:
            # the decision, in the engine's own units — the one number the
            # availability row itself cannot know
            delta = {a.player_id: a for a in dur.adjustments(_ctx(board))}[
                rec.player_id
            ].availability
            assert f"{delta:+.0f} points against him" in line


def test_the_availability_discount_scales_with_the_lineup_reachability_fraction(
    board, engine
):
    """(mutation: dropping ``* frac`` from ``avail_delta``.) THE FACTOR THE WHOLE
    "cannot drift from the engine" claim rests on, and it was pinned by nothing:
    every non-zero availability adjustment in this file used to run at frac 1.0.

    A quarterback behind a rostered starter is worth 25% of his points to this
    roster (``engine._BENCH_VALUE_FRACTION``), so his durability discount must be
    25% of the discount he carries as a startable pick. Dropping the factor
    multiplies the penalty on that bench QB by four."""
    rows = {"qb_one": _priced("qb_one", "QB", miss_rate=0.25)}
    dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    startable = {a.player_id: a for a in dur.adjustments(_ctx(board))}["qb_one"]
    benched = {
        a.player_id: a
        for a in dur.adjustments(_ctx(board, own=("qb_two",), rnd=2, overall=12))
    }["qb_one"]
    assert startable.frac_lineup == 1.0
    assert benched.frac_lineup == 0.25          # the engine's own bench fraction
    assert startable.availability < 0.0
    assert benched.availability == pytest.approx(startable.availability * 0.25)
    share = startable.fraction
    assert benched.availability == pytest.approx(
        -(1.0 - share) * 80.0 * benched.frac_lineup
    )
    # and it is visible to the operator, not just to the arithmetic
    line = next(r for r in benched.reasons if r.startswith("Durability:"))
    assert "already cut to 25% as bench depth" in line


# ------------------------------------------------- Rule 6: the reasons carried


def test_a_priced_row_carries_availabilitys_own_row_specific_reasons_verbatim(
    board, engine
):
    """(mutation: dropping ``_carried`` from ``_availability_reasons``.) The
    handcuff arm has always carried marginal's hedges verbatim; the availability
    arm dropped every one of its source's — the shrink weight, the seasons, the
    clip — and substituted one sentence."""
    row = _priced("rb_glass", "RB", miss_rate=0.25, history=_history())
    assert row.basis == av.BASIS_PLAYER
    dur = vd.DurablePicker(engine=engine, inputs=_inputs({"rb_glass": row}))
    rec = {r.player_id: r for r in dur.recommend(_ctx(board), top=8)}["rb_glass"]
    own = next(r for r in row.reasons if r.startswith("Basis: HIS OWN RECORD"))
    assert own in rec.reasons                      # verbatim, not paraphrased
    assert "carries 50% of the weight" in own      # the sample size AND the shrink
    shape = next(r for r in row.reasons if r.startswith("Sample shape:"))
    assert shape in rec.reasons


def test_the_durability_sentence_never_asserts_a_window_it_did_not_read(
    board, engine
):
    """(mutation: restoring the hard-coded "from his own 2021-2025 record".) The
    literal was wrong for 38 of the 405 own-record rows on the live board and for
    every career that began after 2021."""
    row = _priced("rb_glass", "RB", miss_rate=0.25,
                  history=_history(seasons=(2022, 2023, 2024, 2025)))
    dur = vd.DurablePicker(engine=engine, inputs=_inputs({"rb_glass": row}))
    rec = {r.player_id: r for r in dur.recommend(_ctx(board), top=8)}["rb_glass"]
    line = next(r for r in rec.reasons if r.startswith("Durability:"))
    assert "2021-2025" not in line
    assert "15 of 60 club games missed" in line     # what the row actually carries
    # the seasons ARE disclosed — by the row, which knows them
    assert any("2022-2025" in r for r in rec.reasons)


def test_each_reason_for_using_the_position_prior_gets_its_own_sentence(
    board, engine
):
    """(mutation: collapsing the fallback clauses back to one string.) "He has no
    usable record of his own" is a false statement about the player in three of
    the five cases — and on the live board a top-50 TE was told it while his own
    row said he HAS a record and was never given the job."""
    cases = {
        av.NO_ADJUSTMENT_NO_ID: _priced("rb_glass", "RB", miss_rate=0.2,
                                        identified=False),
        av.NO_ADJUSTMENT_NO_RECORD: _priced("rb_glass", "RB", miss_rate=0.2),
        av.NO_ADJUSTMENT_ROLE: _priced("rb_glass", "RB", miss_rate=0.2,
                                       history=_history(role_weeks=3)),
    }
    lines = {}
    for code, row in cases.items():
        assert row.fallback_code == code, code
        dur = vd.DurablePicker(engine=engine, inputs=_inputs({"rb_glass": row}))
        rec = {r.player_id: r for r in dur.recommend(_ctx(board), top=8)}["rb_glass"]
        lines[code] = next(r for r in rec.reasons if r.startswith("Durability:"))
    assert len(set(lines.values())) == 3          # three causes, three sentences
    assert "NEVER LOOKED UP" in lines[av.NO_ADJUSTMENT_NO_ID]
    assert "crosswalk" in lines[av.NO_ADJUSTMENT_NO_ID]
    assert "found no season" in lines[av.NO_ADJUSTMENT_NO_RECORD]
    # the role case must NOT claim he has no record: he has one
    role = lines[av.NO_ADJUSTMENT_ROLE]
    assert "HAS a record" in role and "60 club games" in role
    assert "no usable record of his own" not in role


def test_a_clipped_multiplier_reaches_the_operator(board, engine):
    """20 rows on the live board ship a CLIPPED multiplier, three of them inside
    the ESPN top 200. The disclosure exists so the printed working reconciles
    with the shipped number; dropping it is how they silently disagree."""
    row = _priced("rb_glass", "RB", miss_rate=0.2,
                  history=_history(games=64, missed=40, role_weeks=40))
    assert row.multiplier_clipped
    dur = vd.DurablePicker(engine=engine, inputs=_inputs({"rb_glass": row}))
    rec = {r.player_id: r for r in dur.recommend(_ctx(board), top=8)}["rb_glass"]
    assert any(r.startswith("CLIPPED:") for r in rec.reasons)


def test_a_season_missed_in_full_is_surfaced_even_when_it_is_not_priced_in(
    board, engine
):
    """availability.py deliberately surfaces "NOT PRICED IN, but you should
    know" on a row whose record it declines to use. Swallowing that here would
    hide the strongest fact in the record behind an average-player price."""
    row = _priced(
        "rb_glass", "RB", miss_rate=0.2,
        history=_history(games=64, missed=17, seasons=(2022, 2023, 2024, 2025),
                         absent_seasons=(2023,), role_weeks=3),
    )
    assert row.basis == av.BASIS_POSITION and row.absent_seasons
    dur = vd.DurablePicker(engine=engine, inputs=_inputs({"rb_glass": row}))
    rec = {r.player_id: r for r in dur.recommend(_ctx(board), top=8)}["rb_glass"]
    assert any("NOT PRICED IN" in r for r in rec.reasons)


def test_the_population_level_notes_ship_once_and_not_on_every_row(monkeypatch):
    """The cohort, the method and the block structure are the same paragraph for
    every player at a position: they belong in the inputs' disclosure, and
    repeating them on each of five candidates on a 90-second clock buries the
    row-specific hedges that are the point."""
    _fake_book_setup(monkeypatch)
    b = (_entry("known", "Known", "RB", 1, 260.0, 120.0, "AAA"),)
    out = vd.build_durable_inputs(object(), as_of="2026-08-30", season=2026, board=b)
    prior = av.DEFAULT_DURABILITY
    for pos in prior.positions():
        assert prior.describe(pos) in out.reasons
        assert prior.method_note(pos) in out.reasons
    assert prior.describe("DST") in out.reasons
    assert prior.block_note() in out.reasons

    eng = PickEngine(survival=_stub_survival)
    row = _priced("known", "RB", miss_rate=0.2, history=_history())
    dur = vd.DurablePicker(engine=eng, inputs=_inputs({"known": row}))
    rec = dur.recommend(_ctx(b), top=1)[0]
    assert row.prior.block_note() not in rec.reasons
    assert row.prior.describe("RB") not in rec.reasons


def test_a_quarterback_row_always_discloses_the_policy(board, engine):
    rows = {"qb_one": _priced("qb_one", "QB", miss_rate=0.2)}
    for policy, note in (
        (vd.QB_POLICY_CAP, vd.QB_POLICY_NOTE),
        (vd.QB_POLICY_RAW, vd._RAW_QB_NOTE),
    ):
        dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows, qb_policy=policy))
        adj = {a.player_id: a for a in dur.adjustments(_ctx(board))}["qb_one"]
        assert note in adj.reasons, policy
    assert "POLICY, NOT A MEASUREMENT" in vd.QB_POLICY_NOTE


# ============================================================ contingent value


def _handcuff_inputs(miss_rate=0.3):
    rows = {
        "rb_glass": _priced("rb_glass", "RB", miss_rate=miss_rate),
        "rb_backup": _priced("rb_backup", "RB", miss_rate=0.1),
    }
    return _inputs(
        rows,
        starter_of={"rb_backup": "rb_glass"},
        uplift_of={"rb_backup": mg.DEFAULT_HANDCUFFS.uplift_for("RB")},
        hc_reasons={"rb_backup": ("the handcuff hedge, verbatim",)},
    )


def test_the_handcuff_bonus_fires_only_when_you_own_the_starter(board, engine):
    inp = _handcuff_inputs()
    dur = vd.DurablePicker(engine=engine, inputs=inp)
    # ``rb_backup`` only enters the engine's candidate set once the better backs
    # are gone — see test_the_wrapper_cannot_reach_a_handcuff_the_engine_never_offers.
    without = {
        a.player_id: a
        for a in dur.adjustments(
            _ctx(board, own=("rb_iron",), taken=("rb_glass", "rb_spare"), rnd=3)
        )
    }
    with_starter = {
        a.player_id: a
        for a in dur.adjustments(
            _ctx(board, own=("rb_glass", "rb_iron"), taken=("rb_spare",), rnd=3)
        )
    }
    assert without["rb_backup"].contingent == 0.0
    assert without["rb_backup"].starter_id is None
    assert with_starter["rb_backup"].contingent > 0.0
    assert with_starter["rb_backup"].starter_id == "rb_glass"


def test_the_handcuff_bonus_is_uplift_times_the_starters_expected_missed_games(board, engine):
    inp = _handcuff_inputs()
    dur = vd.DurablePicker(engine=engine, inputs=inp)
    adj = {a.player_id: a
           for a in dur.adjustments(
               _ctx(board, own=("rb_glass", "rb_iron"), taken=("rb_spare",), rnd=3))}
    row = adj["rb_backup"]
    starter = inp.availability["rb_glass"]
    backup = inp.availability["rb_backup"]
    share = backup.expected_games_played / len(backup.game_weeks)
    expected = mg.DEFAULT_HANDCUFFS.uplift_for("RB") * starter.expected_games_missed * share
    assert row.contingent == pytest.approx(expected)


def test_the_handcuff_uplift_is_the_core_hypothesis_and_not_a_local_number():
    """Rule 2 in spirit: the number must come from ``marginal.DEFAULT_HANDCUFFS``,
    with its own gate (WR measured at -0.14, D/ST and K non-linear)."""
    src = Path(vd.__file__).read_text()
    assert "DEFAULT_HANDCUFFS" in src
    for pos in ("WR", "DST", "K"):
        assert mg.DEFAULT_HANDCUFFS.uplift_for(pos) == 0.0


def test_the_handcuff_reasons_are_carried_verbatim(board, engine):
    inp = _handcuff_inputs()
    dur = vd.DurablePicker(engine=engine, inputs=inp)
    adj = {a.player_id: a
           for a in dur.adjustments(
               _ctx(board, own=("rb_glass", "rb_iron"), taken=("rb_spare",), rnd=3))}
    assert "the handcuff hedge, verbatim" in adj["rb_backup"].reasons
    assert any(r.startswith("HANDCUFF:") for r in adj["rb_backup"].reasons)


def test_the_wrapper_cannot_reach_a_handcuff_the_engine_never_offers(board, engine):
    """THE STRUCTURAL LIMIT OF WRAP-AND-RE-RANK, pinned so nobody reports the
    contingent term as inert without knowing why.

    The engine gathers the top ``candidate_width`` by ESPN rank plus the best by
    VOR at each allowed position. A handcuff sitting deep on the board is simply
    not in that set, so no adjustment this module computes can promote him. Making
    him reachable means widening the candidate set, which is ``engine.py``'s file."""
    inp = _handcuff_inputs()
    dur = vd.DurablePicker(engine=engine, inputs=inp)
    # Both better backs still available: rb_backup (ESPN rank 60) is not offered.
    offered = {a.player_id for a in dur.adjustments(_ctx(board, own=("rb_glass",), rnd=3))}
    assert "rb_backup" not in offered
    # Once they are gone he is the front of the RB board and the bonus can apply.
    reachable = {
        a.player_id: a
        for a in dur.adjustments(
            _ctx(board, own=("rb_glass",), taken=("rb_iron", "rb_spare"), rnd=3)
        )
    }
    assert reachable["rb_backup"].contingent > 0.0


def test_a_handcuff_bonus_can_change_the_pick(board, engine):
    """The term has to be able to MOVE a decision, or it is decoration."""
    b = (
        _entry("rb_glass", "Glass Back", "RB", 1, 260.0, 120.0, "AAA"),
        _entry("rb_backup", "Backup Back", "RB", 60, 90.0, 40.0, "AAA"),
        _entry("rb_spare", "Spare Back", "RB", 59, 92.0, 44.0, "CCC"),
    )
    rows = {
        "rb_glass": _priced("rb_glass", "RB", miss_rate=0.30),
        "rb_backup": _priced("rb_backup", "RB", miss_rate=0.10),
        "rb_spare": _priced("rb_spare", "RB", miss_rate=0.10),
    }
    inp = _inputs(
        rows,
        starter_of={"rb_backup": "rb_glass"},
        uplift_of={"rb_backup": 40.0},   # a deliberately loud uplift
    )
    def ctx():
        return PickContext.from_board(
            b, own_roster=[b[0]], team_slot=0, round=3, overall_pick=21,
            rng=random.Random(7),
        )

    eng_pick = engine.recommend(ctx(), top=1)[0].player_id
    dur = vd.DurablePicker(engine=engine, inputs=inp)
    assert eng_pick == "rb_spare"
    assert dur.pick(ctx()) == "rb_backup"


# =========================================================== the wrapper seam


def test_the_wrapper_consumes_the_rng_exactly_like_the_engine(board, engine):
    """Determinism: the cockpit replays a journal bit-for-bit, so the wrapper may
    not perturb the draft's random stream.

    (mutation: calling ``engine.recommend`` twice inside ``_scored``.) ``recommend``
    draws one rollout child from ``ctx.rng`` per call, so a second call is
    immediately visible in the stream's state."""
    eng = engine
    a = random.Random(4242)
    ctx_a = PickContext.from_board(board, rng=a)
    eng.pick(ctx_a)
    after_engine = a.getstate()

    b = random.Random(4242)
    ctx_b = PickContext.from_board(board, rng=b)
    vd.DurablePicker(engine=eng, inputs=_inputs({})).pick(ctx_b)
    assert b.getstate() == after_engine


def test_the_wrapper_is_deterministic_across_repeats(board, engine):
    rows = {e.player_id: _priced(e.player_id, e.position, miss_rate=0.18)
            for e in board if e.position != "DST"}
    dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    first = [r.player_id for r in dur.recommend(_ctx(board), top=5)]
    for _ in range(3):
        assert [r.player_id for r in dur.recommend(_ctx(board), top=5)] == first


def test_the_engine_reasons_survive_verbatim_and_are_only_extended(board, engine):
    """(mutation: replacing rather than extending ``rec.reasons``.) The 2.4 TUI
    renders these; losing the engine's own sentences would silently change what
    the operator is told on the clock."""
    rows = {"rb_glass": _priced("rb_glass", "RB", miss_rate=0.2)}
    ctx = _ctx(board)
    base = {r.player_id: r for r in engine.recommend(_ctx(board), top=12)}
    dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    for rec in dur.recommend(ctx, top=5):
        original = base[rec.player_id]
        assert rec.reasons[: len(original.reasons)] == original.reasons
        assert len(rec.reasons) >= len(original.reasons)
        assert rec.reasons  # Rule 6: never empty


def test_alternatives_are_rebuilt_from_the_new_order(board):
    """(mutation: passing ``rec.alternatives`` through unchanged.) An alternatives
    list that still describes the ENGINE's ranking is a lie the operator cannot
    detect from the screen."""
    b = (
        _entry("rb_glass", "Glass Back", "RB", 1, 260.0, 120.0, "AAA"),
        _entry("rb_iron", "Iron Back", "RB", 2, 250.0, 110.0, "BBB"),
    )
    eng = PickEngine(survival=_stub_survival)
    inp = _inputs({
        "rb_glass": _priced("rb_glass", "RB", miss_rate=0.35),
        "rb_iron": _priced("rb_iron", "RB", miss_rate=0.02),
    })
    dur = vd.DurablePicker(engine=eng, inputs=inp)
    recs = dur.recommend(_ctx(b), top=2)
    assert recs[0].player_id == "rb_iron"
    assert recs[0].alternatives[0][0] == "Glass Back"


def test_the_wrapper_never_surfaces_a_player_the_engine_would_not_offer(board, engine):
    """It re-ranks; it must not smuggle in an illegal or out-of-window pick."""
    rows = {e.player_id: _priced(e.player_id, e.position, miss_rate=0.2)
            for e in board if e.position != "DST"}
    dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    ctx = _ctx(board, rnd=1, overall=1)
    offered = {r.player_id for r in engine.recommend(_ctx(board), top=vd.DEFAULT_TOP_K)}
    for rec in dur.recommend(ctx, top=8):
        assert rec.player_id in offered


def test_top_k_covers_the_whole_engine_candidate_set(board, engine):
    """DEFAULT_TOP_K must not silently truncate the set it claims to re-rank."""
    ctx = _ctx(board)
    assert len(engine.recommend(ctx, top=1000)) <= vd.DEFAULT_TOP_K


# ================================================= composition / double count


def test_the_module_never_reads_a_dispersion_number():
    """Hazard (b). ``core/dispersion.py`` excludes availability from its band on
    purpose; this module must hold up the other end of that contract."""
    tree = ast.parse(Path(vd.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any("dispersion" in a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert "dispersion" not in (node.module or "")
    src = Path(vd.__file__).read_text()
    assert "POSITIONAL_DISPERSION_PRIOR" not in src
    assert "relative_dispersion" not in src


def test_the_composition_rule_ships_with_the_inputs():
    """An integrator stacking two variants must be able to READ the rule."""
    assert "dispersion" in vd.COMPOSITION_NOTE
    assert "multiply" in vd.COMPOSITION_NOTE


def test_the_engine_risk_term_rides_through_untouched(board, engine):
    """The adjustment is additive on top of ``pick_score``; nothing inside the
    engine's own score is rewritten."""
    rows = {"rb_glass": _priced("rb_glass", "RB", miss_rate=0.25)}
    dur = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    base = {r.player_id: r for r in engine.recommend(_ctx(board), top=12)}
    for rec in dur.recommend(_ctx(board), top=6):
        adj = {a.player_id: a for a in dur.adjustments(_ctx(board))}[rec.player_id]
        assert rec.pick_score == pytest.approx(base[rec.player_id].pick_score + adj.delta)
        assert rec.vor == base[rec.player_id].vor


# ======================================================= the evaluation seam


def _weekly(points, positions):
    return grader.WeeklyPointsMap(
        points,
        positions=positions,
        names={k: k for k in points},
        teams={k: "AAA" for k in points},
    )


def test_expected_weekly_map_scales_by_the_weekly_probability():
    rows = {"rb_glass": _priced("rb_glass", "RB", miss_rate=0.25)}
    inp = _inputs(rows)
    weekly = _weekly({"rb_glass": {w: 10.0 for w in GAME_WEEKS}}, {"rb_glass": "RB"})
    out = vd.expected_weekly_map(weekly, inp)
    row = rows["rb_glass"]
    for w in GAME_WEEKS:
        assert out["rb_glass"][w] == pytest.approx(10.0 * row.p_available(w))
    assert 7 not in out["rb_glass"]           # the bye is still a bye
    assert sum(out["rb_glass"].values()) < sum(weekly["rb_glass"].values())


def test_expected_weekly_map_leaves_an_unpriced_player_alone():
    inp = _inputs({})
    weekly = _weekly({"x": {1: 10.0}}, {"x": "RB"})
    assert vd.expected_weekly_map(weekly, inp)["x"] == {1: 10.0}


def test_sampled_seasons_are_a_property_of_the_player_not_of_the_caller():
    """The pairing guarantee: the same player has the same simulated season in
    every arm, so the objective adds no noise to a paired comparison.

    (mutation: re-keying the stream from the player id to his ENUMERATION INDEX
    in the map.) The probe player is deliberately NOT first in either map and
    sits at a DIFFERENT index in each — the earlier version of this test compared
    two maps in which the probe was index 0 in both, so a single shared stream
    handed him identical draws and the property went untested."""
    rows = {p: _priced(p, "RB", miss_rate=0.25) for p in ("a", "b", "c")}
    inp = _inputs(rows)
    pts = {p: {w: 10.0 for w in GAME_WEEKS} for p in ("a", "b", "c")}
    pos = dict.fromkeys(pts, "RB")
    w1 = _weekly({k: pts[k] for k in ("a", "b", "c")}, pos)          # probe at 1
    w2 = _weekly({k: pts[k] for k in ("c", "b")}, {"c": "RB", "b": "RB"})  # probe at 1...
    w3 = _weekly({k: pts[k] for k in ("b",)}, {"b": "RB"})           # probe at 0
    m1 = vd.sampled_weekly_maps(w1, inp, samples=4, seed=5)
    m2 = vd.sampled_weekly_maps(w2, inp, samples=4, seed=5)
    m3 = vd.sampled_weekly_maps(w3, inp, samples=4, seed=5)
    for x, y, z in zip(m1, m2, m3, strict=True):
        assert set(x["b"]) == set(y["b"]) == set(z["b"])
    # and at least one of those seasons actually has an absence in it, or the
    # comparison above is three copies of "he played every week".
    assert any(set(m["b"]) != set(GAME_WEEKS) for m in m1)
    # (mutation: one stream per SAMPLE instead of per player.) Two players must
    # not share a season: that would make every absence in the league perfectly
    # correlated, which is a different objective entirely.
    assert any(set(m["a"]) != set(m["b"]) for m in m1)
    again = vd.sampled_weekly_maps(w1, inp, samples=4, seed=5)
    for x, y in zip(m1, again, strict=True):
        assert dict(x) == dict(y)


def test_a_week_the_availability_row_does_not_cover_is_not_zeroed():
    """(mutation: dropping the ``if w in row.week_available`` guard.) An uncovered
    week is a week this model has nothing to say about — ``p_available`` returns
    0.0 for it, so multiplying blindly would silently delete real points, which
    is the same collapse-an-unknown-into-a-fact failure the repo pays for
    elsewhere."""
    rows = {"a": _priced("a", "RB", miss_rate=0.25)}
    inp = _inputs(rows)
    assert 7 not in rows["a"].week_available          # his club's bye
    weekly = _weekly({"a": {w: 10.0 for w in (5, 7, 18)}}, {"a": "RB"})
    out = vd.expected_weekly_map(weekly, inp)
    assert out["a"][7] == 10.0                        # the bye: untouched, not zeroed
    assert out["a"][18] == 10.0                       # outside the priced window
    assert out["a"][5] == pytest.approx(10.0 * rows["a"].p_available(5))
    assert out["a"][5] < 10.0                         # a covered week IS discounted


def test_sampled_seasons_produce_absence_BLOCKS_not_scattered_weeks():
    """The whole reason ``sample_available_weeks`` exists: an absence a bench
    absorbs and one it does not are different seasons."""
    rows = {"a": _priced("a", "RB", miss_rate=0.30)}
    inp = _inputs(rows)
    weekly = _weekly({"a": {w: 10.0 for w in GAME_WEEKS}}, {"a": "RB"})
    maps = vd.sampled_weekly_maps(weekly, inp, samples=120, seed=3)
    runs, missed_total = [], 0
    for m in maps:
        played = set(m["a"])
        run = 0
        for w in GAME_WEEKS:
            if w in played:
                if run:
                    runs.append(run)
                run = 0
            else:
                run += 1
                missed_total += 1
        if run:
            runs.append(run)
    assert missed_total > 0
    assert sum(runs) / len(runs) > 1.5    # blocks, not coin flips


def test_sampled_seasons_refuse_a_zero_sample_request():
    with pytest.raises(vd.DurableVariantError):
        vd.sampled_weekly_maps(_weekly({"a": {1: 1.0}}, {"a": "RB"}), _inputs({}),
                               samples=0, seed=1)


def test_a_points_map_without_positions_is_refused_not_guessed():
    with pytest.raises(vd.DurableVariantError):
        vd.expected_weekly_map({"a": {1: 1.0}}, _inputs({}))


def test_averaged_grade_fn_averages_and_refuses_an_empty_list():
    from ziggurat.draft import evaluate as ev

    with pytest.raises(vd.DurableVariantError):
        vd.averaged_grade_fn((), make_grade_fn=ev.make_grade_fn)


def test_every_sampled_grade_says_it_is_conditional_on_one_world_draw():
    """(mutation: dropping the stamped note.) A paired interval computed against
    a FIXED set of simulated worlds contains no world-draw uncertainty at all —
    and the published +0.209 headline was quoted with an interval that did not
    contain the same design's estimate under a different draw. The disclosure is
    stamped on the grade rather than left to the caller, because the caller who
    forgets it is the one who quotes the interval."""
    from ziggurat.draft import evaluate as ev

    smoke_board, weekly = ev.smoke_inputs()
    rows = {
        e.player_id: _priced(e.player_id, e.position, miss_rate=0.25)
        for e in smoke_board[:40] if e.position != "DST"
    }
    inp = _inputs(rows, board_rows=len(smoke_board))
    maps = vd.sampled_weekly_maps(weekly, inp, samples=2, seed=11)
    fn = vd.averaged_grade_fn(maps, make_grade_fn=ev.make_grade_fn)
    grade = fn(list(smoke_board[:16]), None)
    assert any("CONDITIONAL ON ONE DRAW" in r for r in grade.reasons)
    assert any("2 simulated season(s)" in r for r in grade.reasons)
    assert "seed" in vd.SAMPLED_OBJECTIVE_NOTE


# ================================================== the A/B preflight (audit)


def test_an_inert_variant_refuses_to_be_measured(board, engine):
    """A challenger that adjusts NOTHING comes out of the paired harness as
    ``mean_delta +0.0000, sd 0.0000, ties n/n`` — the exact shape of a genuine
    "no effect" finding. It happened to an auditor of this module, on a copy
    captured mid-edit, and nothing anywhere raised. This is the preflight."""
    rows = {"rb_glass": _priced("rb_glass", "RB", miss_rate=0.25)}
    live = vd.DurablePicker(engine=engine, inputs=_inputs(rows))
    assert vd.assert_adjustments_are_live(live, _ctx(board))

    dead = vd.DurablePicker(engine=engine, inputs=_inputs(rows),
                            b_availability=0.0, b_contingent=0.0)
    with pytest.raises(vd.DurableVariantError) as excinfo:
        vd.assert_adjustments_are_live(dead, _ctx(board))
    assert "INERT VARIANT" in str(excinfo.value)

    # an ablation arm is NOT inert as long as its own term still moves
    avail_only = vd.DurablePicker(engine=engine, inputs=_inputs(rows),
                                  b_contingent=0.0)
    assert vd.assert_adjustments_are_live(avail_only, _ctx(board))


def test_the_preflight_takes_a_SET_of_contexts_for_a_rarely_reachable_term(
    board, engine
):
    """The handcuff term is reachable on ~5% of picks (the wrapper cannot add to
    the engine's candidate set), so ONE arbitrary context proves nothing about
    it. A preflight that raised on the first inert context would refuse to
    measure a perfectly live arm — and one that passed on it would be the
    original bug wearing a guard's clothes."""
    inp = _handcuff_inputs()
    hc_only = vd.DurablePicker(engine=engine, inputs=inp, b_availability=0.0)
    unreachable = _ctx(board, own=("rb_glass",), rnd=3)         # backup not offered
    reachable = _ctx(board, own=("rb_glass",), taken=("rb_iron", "rb_spare"), rnd=3)
    with pytest.raises(vd.DurableVariantError):
        vd.assert_adjustments_are_live(hc_only, unreachable)
    assert vd.assert_adjustments_are_live(hc_only, [unreachable, reachable])
    with pytest.raises(vd.DurableVariantError) as excinfo:
        vd.assert_adjustments_are_live(
            vd.DurablePicker(engine=engine, inputs=inp, b_availability=0.0,
                             b_contingent=0.0),
            [unreachable, reachable],
        )
    assert "2 context(s)" in str(excinfo.value)


# ================================================================ Rule 1


def test_the_builder_takes_a_keyword_only_as_of_with_no_default():
    sig = inspect.signature(vd.build_durable_inputs)
    p = sig.parameters["as_of"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty
    assert sig.parameters["view"].default == "historical"


def test_the_builder_threads_as_of_and_view_into_both_reads(monkeypatch):
    """(mutation: hard-coding ``latest_truth`` or dropping ``as_of``.) The gate is
    enforced downstream, so what this layer must be pinned on is that it NEVER
    widens or forgets it."""
    seen = {}

    def fake_book(conn, *, as_of, season, prior, view, **kw):
        seen["book"] = (as_of, season, view)
        return _Book()

    def fake_links(conn, *, as_of, season, weeks, source, handcuffs, view):
        seen["links"] = (as_of, season, tuple(weeks), view)
        return []

    monkeypatch.setattr(vd.av, "load_durability_book", fake_book)
    monkeypatch.setattr(vd.mg, "handcuff_links", fake_links)
    monkeypatch.setattr(vd.base, "espn_by_gsis", lambda conn: {})
    b = (_entry("rb_glass", "Glass Back", "RB", 1, 260.0, 120.0, "AAA"),)
    out = vd.build_durable_inputs(object(), as_of="2024-09-01", season=2024, board=b)
    assert seen["book"] == ("2024-09-01", 2024, "historical")
    assert seen["links"][:2] == ("2024-09-01", 2024)
    assert seen["links"][3] == "historical"
    # weeks are passed EXPLICITLY: handcuff_links' own default resolves the live
    # in-season week and raises preseason, which is every moment this is used.
    assert seen["links"][2] == tuple(range(1, 18))
    assert out.priced == 1


def test_the_builder_refuses_an_empty_board():
    with pytest.raises(vd.DurableVariantError):
        vd.build_durable_inputs(object(), as_of="2026-08-30", season=2026, board=())


def test_the_builder_refuses_an_unknown_policy():
    b = (_entry("rb_glass", "Glass Back", "RB", 1, 260.0, 120.0, "AAA"),)
    with pytest.raises(vd.DurableVariantError):
        vd.build_durable_inputs(object(), as_of="2026-08-30", season=2026,
                                board=b, qb_policy="nope")


def _fake_book_setup(monkeypatch, *, slate=None):
    monkeypatch.setattr(vd.av, "load_durability_book",
                        lambda conn, **kw: _Book(slate=slate))
    monkeypatch.setattr(vd.mg, "handcuff_links", lambda conn, **kw: [])
    monkeypatch.setattr(vd.base, "espn_by_gsis", lambda conn: {})


def test_a_row_with_no_club_is_left_unpriced_rather_than_assumed_durable(monkeypatch):
    _fake_book_setup(monkeypatch)
    b = (
        _entry("known", "Known", "RB", 1, 260.0, 120.0, "AAA"),
        _entry("clubless", "Clubless", "RB", 2, 250.0, 110.0, None),
    )
    out = vd.build_durable_inputs(object(), as_of="2026-08-30", season=2026, board=b)
    assert out.fraction("known") is not None
    assert out.fraction("clubless") is None
    assert out.cause("clubless") == vd.UNPRICED_NO_CLUB
    assert out.cause("known") is None


# ------------------------------------------------ hazard 3: the slate floor


def test_a_club_missing_from_the_slate_is_a_refusal_not_a_silent_promotion(monkeypatch):
    """(mutation: dropping the SlateGap raise.) A four-club-short schedule pull
    loads clean past availability's own MIN_BOOK_CLUBS=28 floor, and every player
    from the missing clubs then keeps 100% of his value while everyone else is
    discounted — the model does not degrade toward the engine, it PROMOTES the
    players whose data was lost."""
    _fake_book_setup(monkeypatch)
    b = (
        _entry("known", "Known", "RB", 1, 260.0, 120.0, "AAA"),
        _entry("offslate", "Off Slate", "RB", 2, 250.0, 110.0, "ZZZ"),
    )
    with pytest.raises(vd.SlateGap) as excinfo:
        vd.build_durable_inputs(object(), as_of="2026-08-30", season=2026, board=b)
    assert "ZZZ" in str(excinfo.value)
    assert isinstance(excinfo.value, vd.DurableVariantError)


def test_the_slate_override_is_explicit_recorded_and_disclosed(monkeypatch):
    _fake_book_setup(monkeypatch)
    b = (
        _entry("known", "Known", "RB", 1, 260.0, 120.0, "AAA"),
        _entry("offslate", "Off Slate", "RB", 2, 250.0, 110.0, "ZZZ"),
    )
    out = vd.build_durable_inputs(object(), as_of="2026-08-30", season=2026,
                                  board=b, allow_missing_clubs=True)
    assert out.missing_clubs == ("ZZZ",)
    assert out.cause("offslate") == vd.UNPRICED_CLUB_OFF_SLATE
    assert out.fraction("offslate") is None
    assert any("ZZZ" in r and "SLATE GAP" in r for r in out.reasons)


def test_an_unpriced_row_names_the_RIGHT_cause(board, engine):
    """(mutation: reverting to the one-sentence "no club on the row".) The board
    row for an off-slate player CARRIES a club; telling the operator it does not
    points a diagnosis at the wrong field."""
    off = _inputs({}, unpriced={"rb_glass": vd.UNPRICED_CLUB_OFF_SLATE})
    dur = vd.DurablePicker(engine=engine, inputs=off)
    rec = {r.player_id: r for r in dur.recommend(_ctx(board), top=8)}["rb_glass"]
    text = " ".join(rec.reasons)
    assert "club is missing from the 2026 schedule" in text.lower() or (
        "CLUB is missing from the 2026 schedule" in text)
    assert "no club at all" not in text

    none_row = _inputs({}, unpriced={"rb_glass": vd.UNPRICED_NO_CLUB})
    dur2 = vd.DurablePicker(engine=engine, inputs=none_row)
    rec2 = {r.player_id: r for r in dur2.recommend(_ctx(board), top=8)}["rb_glass"]
    assert any("no club at all" in r for r in rec2.reasons)


# ============================================================ DB-guarded


pytestmark_db = pytest.mark.skipif(
    not LIVE_DB.exists(), reason="live db/ziggurat.sqlite not present"
)


@pytestmark_db
def test_an_as_of_before_the_data_existed_refuses_rather_than_prices_empty():
    """Rule 1, on the real database: the collapse floors inside
    ``load_durability_book`` are what stop a silent 1.00x board."""
    from ziggurat.draft import simulator

    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        board = simulator.load_board(conn, as_of="2026-08-30", season=2026)
        with pytest.raises(av.HistoryCollapse):
            vd.build_durable_inputs(conn, as_of="2015-01-01", season=2026, board=board)
    finally:
        conn.close()


def _top200_means(board, inp):
    top = sorted(board, key=lambda e: e.espn_overall_rank)[:200]
    by_pos: dict[str, list[float]] = {}
    for e in top:
        row = inp.availability.get(e.player_id)
        if row is not None:
            by_pos.setdefault(e.position, []).append(row.expected_games_played)
    return {p: sum(v) / len(v) for p, v in by_pos.items()}


@pytestmark_db
def test_the_live_board_prices_the_measured_availability_spread():
    """The headline measurement, re-derived rather than trusted: the top of the
    live board is nowhere near sixteen games for anybody but a defense.

    THIS PINS THE MODULE DOCSTRING'S TABLE, to +/-0.4. The band it replaced
    (10.5 < mean < 15.0) admitted anything, and the docstring drifted inside it:
    the table printed the RAW-policy QB number (11.87) while the shipped default
    is the CAPPED one (12.56), fifty lines above the paragraph that explains the
    cap. A table nothing measures is a claim, not a measurement."""
    from ziggurat.draft import simulator

    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        board = simulator.load_board(conn, as_of="2026-08-30", season=2026)
        inp = vd.build_durable_inputs(conn, as_of="2026-08-30", season=2026,
                                      board=board)
        raw = vd.build_durable_inputs(conn, as_of="2026-08-30", season=2026,
                                      board=board, qb_policy=vd.QB_POLICY_RAW)
    finally:
        conn.close()
    means = _top200_means(board, inp)
    shipped = {"DST": 16.00, "TE": 13.62, "K": 12.92, "WR": 12.65,
               "RB": 12.61, "QB": 12.56}
    assert means["DST"] == pytest.approx(16.0)
    for pos, expected in shipped.items():
        assert abs(means[pos] - expected) < 0.4, (pos, means[pos], expected)
    # the docstring's second row: RAW moves the QB cell and NOTHING else.
    raw_means = _top200_means(board, raw)
    assert abs(raw_means["QB"] - 11.87) < 0.4, raw_means["QB"]
    assert raw_means["QB"] < means["QB"] - 0.4
    for pos in ("DST", "TE", "K", "WR", "RB"):
        assert raw_means[pos] == pytest.approx(means[pos])
    assert inp.priced > 500 and len(inp.starter_of) > 20
    # hazard 3, on the real board: every club on it is in the schedule, so the
    # floor costs nothing here and fires only on a degraded pull.
    assert inp.missing_clubs == () and inp.slate_clubs == 32
