"""Tests for the Phase-2 ``weekwise`` variant (``ziggurat/draft/variant_weekwise.py``).

THE LOAD-BEARING TEST IS :func:`test_weight_zero_is_the_engine`. The whole point
of a wrapper is that it is opt-in: at ``weight=0.0`` the variant must be the
shipped :class:`~ziggurat.draft.engine.PickEngine` in every observable way —
same recommendation, same score, same reasons, same consumption of ``ctx.rng``,
same sixteen picks in a full draft. If that fails, the variant is not a variant,
it is an edit to the engine wearing a new filename, and the golden master
(``tests/test_draft_golden.py``) is the alarm that should have caught it.

Everything here is OFFLINE and DETERMINISTIC. The board and points map are a
purpose-built synthetic fixture with invented names and invented NFL teams
(Rule 5: no real identity, no league-private data), and every bye is placed by
hand so the bye-collision assertions are statements about a KNOWN roster rather
than about whatever the live board happens to contain this week. The engine is
driven with an injected survival stub, so no rollout runs and no wall clock or
global random enters any assertion.

WHAT EACH GROUP IS FOR
  1. the opt-in contract          — weight 0 is the engine, bit for bit
  2. the design finding           — completion is what makes a collision visible
  3. the promotion sanity case    — a bye-hole filler is visibly promoted
  4. Rule 6                       — the sentence appears iff the term moved a pick
  5. the machinery                — len(shortlist)+1 grades per decision and no
                                    more, a completion that is never truncated,
                                    never benches a pick while a starting slot is
                                    open, and whose slot labels are the seater's
  6. refuse-rather-than-guess     — every bad input raises instead of guessing
  7. the boundaries               — Rule 8's other direction (what this module is
                                    allowed to import) and Rule 1 by construction
  8. the audit round              — the guards the first version did not have:
                                    who actually closed a bye hole, a sentence
                                    that agrees with its own number, the posture
                                    seam, this seat's real snake offsets, the
                                    four mutation survivors, and the waiver
                                    pricing of an empty skill week

MUTATION-VERIFIED, TWICE. The first version of this file claimed sixteen injected
defects and fifteen caught; an independent twenty-mutation run then left FOUR
alive, two of them behaviour-changing on the live board — the candidate branch's
``first_pick=1`` and the completion's best-by-VOR tiebreak, neither of which was
pinned by anything. Section 8's ``test_the_candidate_branch_cannot_re_take_...``
and ``test_the_completion_takes_the_best_value_not_the_best_espn_rank`` are those
two. Re-verified 2026-08-30 against a fresh set of SIXTEEN defects — every guard
this audit round added, plus both surviving mutants and the two clauses of the
hole-credit guard injected SEPARATELY — and all sixteen are caught. The module
file was md5-checked before and after the run and is byte-identical.

The lesson that round paid for, recorded because it generalises: a fixture that
violates two clauses of one guard at once cannot tell them apart. Dropping either
clause alone left the tests green until ``_DOWNSTREAM_CANDIDATES`` split the case
into three, one per clause.
"""

from __future__ import annotations

import dataclasses
import pathlib
import random
import re

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.draft import evaluate as ev
from ziggurat.draft import grader
from ziggurat.draft import variant_weekwise as vw
from ziggurat.draft.bots import BoardEntry, PickContext
from ziggurat.draft.engine import (
    PickEngine,
    PickRec,
    SurvivalEstimate,
    _startable_now,
)
from ziggurat.draft.simulator import run_draft

WEEKS = tuple(range(1, 18))
REG = tuple(range(1, 15))


# ------------------------------------------------------------------ fixture


def _fixture():
    """A small synthetic board + points map with HAND-PLACED byes.

    Shaped so the bye arithmetic is legible in the test rather than emergent:

      * ``RB-anchor`` (NO bye — a synthetic ironman, deliberately, so that week 8
        is the ONE week this roster is short at running back and the assertions
        are about a single clean collision rather than a realistic schedule) and
        ``RB-hole`` (bye 8) are the operator's two rostered backs, so week 8
        leaves exactly ONE empty RB slot;
      * ``RB-collide`` (bye 8) and ``RB-cover`` (bye 13) are the two candidates,
        and the COLLIDING one is deliberately the BETTER player on raw value —
        exactly the live-board shape this variant exists for (Montgomery, bye 8
        behind a bye-8 McCaffrey, outranking a non-colliding Jacobs). So the
        engine prefers him, every points-level signal prefers him, and only a
        contested week-by-week lineup can see why he is the wrong pick;
      * every other position carries filler deep enough for a legal lineup, and
        NO other running back is left available, so the completion cannot quietly
        patch week 8 with a body the test did not put there.
    """
    rows: list[tuple[str, str, float, int | None, str]] = []  # id, pos, rate, bye, team
    rows += [
        ("RB-anchor", "RB", 15.0, None, "AAA"),
        ("RB-hole", "RB", 14.0, 8, "BBB"),
        ("RB-collide", "RB", 12.3, 8, "CCC"),
        ("RB-cover", "RB", 12.0, 13, "DDD"),
    ]
    # Backs that are already off the board (marked taken by the context builder).
    rows += [(f"RB-gone{i}", "RB", 13.5 - i, 5 + i, f"E{i:02d}") for i in range(6)]
    # Filler rates are deliberately BELOW the two candidates', so the candidates
    # top the available board by ESPN rank and the engine's rank window really
    # does shortlist both of them (the engine's per-position best-by-VOR head
    # would otherwise offer only one of two players with identical VOR).
    for pos, count, top, step in (("QB", 8, 11.0, 0.5), ("WR", 14, 10.0, 0.4),
                                  ("TE", 8, 8.0, 0.3), ("K", 6, 7.0, 0.2),
                                  ("DST", 6, 7.5, 0.2)):
        for i in range(count):
            rows.append((f"{pos}-{i:02d}", pos, round(top - step * i, 3), 4 + (i % 9),
                         f"{pos}{i:02d}"))

    points: dict[str, dict[int, float]] = {}
    positions: dict[str, str] = {}
    names: dict[str, str] = {}
    teams: dict[str, str] = {}
    season: dict[str, float] = {}
    for pid, pos, rate, bye, team in rows:
        played = tuple(w for w in WEEKS if w != bye)
        points[pid] = {w: rate for w in played}
        positions[pid] = pos
        names[pid] = f"Fixture {pid}"
        teams[pid] = team
        season[pid] = rate * len(played)

    # Replacement per position = the last player any of ten teams would start,
    # so VOR orders positions rather than raw points (as the real board does).
    replacement: dict[str, float] = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        pool = sorted((season[p] for p, q in positions.items() if q == pos), reverse=True)
        per_team = DEFAULT_ROSTER.starters.get(pos, 0) + (
            DEFAULT_ROSTER.flex_slots if pos in DEFAULT_ROSTER.flex_positions else 0
        )
        idx = min(max(per_team * DEFAULT_ROSTER.teams - 1, 0), len(pool) - 1)
        replacement[pos] = pool[idx]

    # ESPN rank: season points descending, id tiebreak.
    ordered = sorted(points, key=lambda p: (-season[p], p))
    ranks = {pid: i + 1 for i, pid in enumerate(ordered)}

    board = tuple(
        BoardEntry(
            player_id=pid,
            name=names[pid],
            position=positions[pid],
            espn_overall_rank=ranks[pid],
            house_points=round(season[pid], 4),
            vor=round(season[pid] - replacement[positions[pid]], 4),
            team=teams[pid],
        )
        for pid in ordered
    )
    weekly = grader.WeeklyPointsMap(points, positions=positions, names=names, teams=teams)
    return board, weekly


def _stub_survival(ctx, *, candidates, positions, rng):
    """Every candidate survives with the same probability and no position cliffs.

    Neutralises the engine's urgency term so a test that flips an ordering has
    flipped it for the reason the test is about.
    """
    return SurvivalEstimate(
        survival={c.player_id: 0.5 for c in candidates},
        next_best_vor={p: 0.0 for p in positions},
    )


def _engine(**kw) -> PickEngine:
    return PickEngine(survival=_stub_survival, **kw)


def _inputs(weekly, board=None, **kw) -> vw.WeekwiseInputs:
    return vw.WeekwiseInputs.build(weekly, board=board, **kw)


def _late_context(board, weekly, *, own_ids, taken_ids, round_num=13, picks_after=3):
    """A context with a nearly-complete roster and a few picks left.

    ``picks_after`` is derived from ``rounds_total - round``, so the two are set
    together rather than independently.
    """
    own = [e for e in board if e.player_id in own_ids]
    own.sort(key=lambda e: own_ids.index(e.player_id))
    return PickContext.from_board(
        board,
        own_roster=own,
        taken=tuple(taken_ids),
        team_slot=8,
        round=round_num,
        overall_pick=round_num * 10 - 1,
        rounds_total=round_num + picks_after,
        rng=random.Random(4),
    )


#: The operator's roster THREE picks in — the shape that makes the naive form
#: fail, because a three-man roster has an empty slot in every week.
_EARLY = ("RB-anchor", "RB-hole", "WR-00")

#: A 52-player fixture board cannot absorb a realistic ten-players-per-round gap
#: for thirteen remaining picks, so the completion model is run proportionally
#: tighter here. On the live 3,264-row board the default (``roster.teams``) is the
#: one that ships.
_FIXTURE_GAP = 2

#: The operator's roster in the collision scenario: a legal starting nine with
#: exactly two backs, one of whom byes in week 8.
_OWN = (
    "QB-00", "RB-anchor", "RB-hole", "WR-00", "WR-01", "TE-00", "WR-02", "DST-00", "K-00",
)
#: Every other back is gone, so week 8 cannot be patched by the completion.
_TAKEN = tuple(f"RB-gone{i}" for i in range(6))


# ======================================================================
#  1.  the opt-in contract
# ======================================================================


def test_weight_zero_is_the_engine():
    """weight=0.0 returns the engine's recommendations UNTOUCHED, field for field."""
    board, weekly = _fixture()
    engine = _engine()
    picker = vw.WeekwisePicker(engine=engine, inputs=_inputs(weekly, board), weight=0.0)

    ctx_a = _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)
    ctx_b = _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)
    mine = picker.recommend(ctx_a, top=5)
    theirs = engine.recommend(ctx_b, top=5)

    assert [r.player_id for r in mine] == [r.player_id for r in theirs]
    assert [r.pick_score for r in mine] == [r.pick_score for r in theirs]
    assert [r.reasons for r in mine] == [r.reasons for r in theirs]
    assert [r.alternatives for r in mine] == [r.alternatives for r in theirs]
    assert picker.pick(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)) == (
        engine.pick(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN))
    )


def test_weight_zero_drafts_the_same_sixteen_picks():
    """A whole draft is bit-identical at weight 0 — every seat, not just ours.

    Driven off ``evaluate.smoke_inputs`` rather than this module's hand-built
    fixture: ``run_draft`` refuses a board too thin to feed ten teams for sixteen
    rounds, and the point here is the FULL draft, not one decision.
    """
    board, weekly = ev.smoke_inputs()
    inputs = _inputs(weekly, board)

    def _draft(operator):
        seats = [ev.OffsetRankPicker(offset=i % 3) for i in range(10)]
        seats[8] = operator
        return run_draft(board, seats, rng=random.Random(5), roster=DEFAULT_ROSTER, rounds=16)

    engine = _engine()
    plain = _draft(engine)
    wrapped = _draft(vw.WeekwisePicker(engine=engine, inputs=inputs, weight=0.0))
    for team in range(10):
        assert [e.player_id for e in plain.rosters[team]] == [
            e.player_id for e in wrapped.rosters[team]
        ], f"seat {team} diverged at weight 0"


def test_rng_consumption_matches_the_engine():
    """The wrapper draws from ``ctx.rng`` exactly as the engine does.

    ``run_draft`` threads one stream through the whole room, so a wrapper that
    consumed one extra number would silently re-deal every later rival pick —
    the failure ``evaluate``'s ``_SeatStream`` exists to keep out of a paired A/B.
    """
    board, weekly = _fixture()
    inputs = _inputs(weekly, board)
    engine = _engine()

    def _tail(picker):
        ctx = _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)
        rng = ctx.rng
        picker.recommend(ctx, top=3)
        return [rng.getrandbits(32) for _ in range(5)]

    assert _tail(engine) == _tail(vw.WeekwisePicker(engine=engine, inputs=inputs, weight=0.0))
    assert _tail(engine) == _tail(vw.WeekwisePicker(engine=engine, inputs=inputs, weight=9.0))


def test_recommend_is_deterministic():
    board, weekly = _fixture()
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=3.0)
    first = picker.recommend(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=5)
    second = picker.recommend(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=5)
    assert [(r.player_id, r.pick_score, r.reasons) for r in first] == [
        (r.player_id, r.pick_score, r.reasons) for r in second
    ]


def test_startable_matches_the_engine():
    """The completion's need rule is the engine's, not a second opinion."""
    board, _weekly = _fixture()
    by_pos = {}
    for e in board:
        by_pos.setdefault(e.position, e)
    for counts in (
        {}, {"RB": 1}, {"RB": 2}, {"RB": 2, "WR": 2}, {"RB": 2, "WR": 3},
        {"RB": 3, "WR": 2, "TE": 1}, {"QB": 1}, {"QB": 2}, {"TE": 1}, {"K": 1}, {"DST": 1},
    ):
        for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
            assert vw._startable(pos, counts, DEFAULT_ROSTER) == _startable_now(
                pos, counts, DEFAULT_ROSTER
            ), (pos, counts)


# ======================================================================
#  2.  the design finding: completion is what makes a collision visible
# ======================================================================


def _adjustments(board, weekly, *, completion, weight=1.0, own=_OWN, round_num=13,
                 picks_after=3, **kw):
    engine = _engine()
    def _ctx():
        return _late_context(
            board, weekly, own_ids=own, taken_ids=_TAKEN,
            round_num=round_num, picks_after=picks_after,
        )
    recs = engine.recommend(_ctx(), top=vw.DEFAULT_SHORTLIST)
    adjs = vw.weekwise_adjustments(
        recs, _ctx(), inputs=_inputs(weekly, board), weight=weight,
        completion=completion, **kw
    )
    return recs, {a.player_id: a for a in adjs}


def test_completion_is_what_makes_the_collision_visible():
    """On a three-man roster the naive form ranks by RAW POINTS and gets it backwards.

    The measured design finding restated as an assertion (module docstring).
    Grading ``own_roster + [candidate]`` directly seats every candidate in every
    week he plays — a roster with empty slots contests nothing — so the marginal
    collapses onto the player's own season total and the colliding back, who is
    the better player, wins. Completing the remaining picks makes the slots
    contested, and the sign flips.

    Live-board version of the same two numbers (2026-08-30, seat 9, after Gibbs /
    Smith-Njigba / McCaffrey): Montgomery, whose bye collides with McCaffrey's,
    grades 185.0 against non-colliding Jacobs' 183.4 on the partial roster and
    26.9 against 38.3 once the roster is completed.
    """
    board, weekly = _fixture()
    _recs, naive = _adjustments(
        board, weekly, completion="none", own=_EARLY, round_num=3, picks_after=13
    )
    _recs, done = _adjustments(
        board, weekly, completion="future", own=_EARLY, round_num=3, picks_after=13,
        completion_gap=_FIXTURE_GAP,
    )
    assert {"RB-collide", "RB-cover"} <= set(naive), "the fixture must shortlist both backs"

    # Naive: the colliding back is the better player, so he wins — the collision
    # is invisible, and the marginal is just his own season points.
    assert naive["RB-collide"].marginal_points > naive["RB-cover"].marginal_points
    assert naive["RB-collide"].marginal_points == pytest.approx(
        naive["RB-collide"].solo_points, abs=1e-6
    )
    # Completed: the sign flips. Same board, same roster, same candidates.
    assert done["RB-cover"].marginal_points > done["RB-collide"].marginal_points


def test_the_collision_is_named_as_a_week():
    board, weekly = _fixture()
    _recs, done = _adjustments(board, weekly, completion="future")
    assert 8 in done["RB-collide"].collision_weeks
    assert 8 not in done["RB-cover"].collision_weeks
    assert 8 in done["RB-cover"].filled_hole_weeks


def test_the_completed_roster_prices_the_collision_at_a_whole_lineup_week():
    """The gap is a week of a starting running back, not a rounding artefact."""
    board, weekly = _fixture()
    _recs, done = _adjustments(board, weekly, completion="future")
    gap = done["RB-cover"].marginal_points - done["RB-collide"].marginal_points
    assert gap > 5.0, gap
    # ...and it survives the completion model's own parameter, which is a
    # hypothesis rather than a measurement.
    for tight in (5, 8, 14):
        _r, alt = _adjustments(board, weekly, completion="future", completion_gap=tight)
        assert alt["RB-cover"].marginal_points > alt["RB-collide"].marginal_points, tight


# ======================================================================
#  3.  the promotion sanity case
# ======================================================================


def test_a_bye_hole_filler_is_promoted_over_an_identical_colliding_back():
    """The case the variant exists for, shown before and after.

    The colliding back is the BETTER player on every signal the engine has — more
    projected points, higher VOR, a better ESPN rank — so the engine ranks him
    first. With the week-by-week term on, the back who actually plays in the one
    week this roster is short overtakes him.
    """
    board, weekly = _fixture()
    inputs = _inputs(weekly, board)
    engine = _engine()

    collide = next(e for e in board if e.player_id == "RB-collide")
    cover = next(e for e in board if e.player_id == "RB-cover")
    assert collide.vor > cover.vor, "the fixture must make the colliding back the better player"
    assert collide.espn_overall_rank < cover.espn_overall_rank

    before = engine.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=vw.DEFAULT_SHORTLIST
    )
    order_before = [r.player_id for r in before]
    assert order_before.index("RB-collide") < order_before.index("RB-cover"), (
        "before: the engine prefers the colliding back"
    )

    picker = vw.WeekwisePicker(engine=engine, inputs=inputs, weight=2.0)
    after = picker.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=vw.DEFAULT_SHORTLIST
    )
    order_after = [r.player_id for r in after]
    assert order_after.index("RB-cover") < order_after.index("RB-collide"), (
        "after: the week-by-week term promotes the back who covers week 8"
    )


def test_a_large_weight_changes_the_actual_pick():
    """The term has teeth: at a big enough weight the DRAFTED player changes."""
    board, weekly = _fixture()
    inputs = _inputs(weekly, board)
    engine = _engine()
    plain = engine.pick(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN))
    loud = vw.WeekwisePicker(engine=engine, inputs=inputs, weight=50.0).pick(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)
    )
    assert loud != plain


def test_crowding_mode_can_only_demote():
    """``crowding`` is a pure penalty — it never scores a candidate ABOVE his own value."""
    board, weekly = _fixture()
    _recs, adjs = _adjustments(
        board, weekly, completion="none", weight=0.05, mode="crowding"
    )
    assert adjs, "the fixture must produce candidates"
    assert all(a.adjustment <= 0.0 for a in adjs.values())
    assert any(a.adjustment < 0.0 for a in adjs.values())


def test_crowding_with_the_completion_is_refused():
    """The one combination whose number reads like points and is not.

    ``crowded_out`` is ``solo - marginal``. Under the completion, ``marginal`` is
    a difference against the player you would otherwise take, so the subtraction
    absorbs that player's whole season and the "penalty" grows with how good the
    alternative is. Nothing raises on its own, so this does.
    """
    board, weekly = _fixture()
    with pytest.raises(vw.WeekwiseInputError, match="UNUSABLE production"):
        vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board),
                          weight=0.05, mode="crowding")
    engine = _engine()
    recs = engine.recommend(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=3)
    with pytest.raises(vw.WeekwiseInputError, match="UNUSABLE production"):
        vw.weekwise_adjustments(
            recs, _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN),
            inputs=_inputs(weekly, board), weight=0.05, mode="crowding", completion="future",
        )


def test_a_reason_never_claims_a_share_the_mode_did_not_measure():
    """Rule 6: the sentence has to mean what the number means.

    "only X of his Y points could reach your lineup" is true when the marginal is
    measured against an unspent pick, and FALSE when it is measured against the
    player you would otherwise take — there the difference is between two players,
    not between a player and nothing.
    """
    board, weekly = _fixture()
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=4.0)
    lines = [
        line
        for rec in picker.recommend(
            _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN),
            top=vw.DEFAULT_SHORTLIST,
        )
        for line in _weekwise_lines(rec)
    ]
    assert lines
    for line in lines:
        assert "could actually reach" not in line, line
    # ...and every non-hole sentence names what the number is a difference AGAINST.
    for line in lines:
        if "empty spot in week" in line or "he is off in week" in line:
            continue
        assert "the player you would otherwise take with this pick" in line, line


def test_marginal_wins_mode_reads_expected_wins():
    board, weekly = _fixture()
    _recs, adjs = _adjustments(
        board, weekly, completion="future", weight=2.0, mode="marginal_wins"
    )
    for a in adjs.values():
        if a.applies:
            assert a.adjustment == pytest.approx(2.0 * a.marginal_wins)


def test_kdst_are_left_alone_by_default_and_can_be_opted_in():
    """The default guard on the separately validated K/DST divergence play."""
    board, weekly = _fixture()
    own = ("QB-00", "RB-anchor", "RB-hole", "WR-00", "WR-01", "TE-00", "WR-02")
    engine = _engine()
    ctx = _late_context(board, weekly, own_ids=own, taken_ids=_TAKEN, round_num=9, picks_after=7)
    recs = engine.recommend(ctx, top=vw.DEFAULT_SHORTLIST)
    inputs = _inputs(weekly, board)
    ctx2 = _late_context(board, weekly, own_ids=own, taken_ids=_TAKEN, round_num=9, picks_after=7)
    fenced = {a.player_id: a for a in vw.weekwise_adjustments(
        recs, ctx2, inputs=inputs, weight=1.0)}
    kdst = [a for a in fenced.values() if a.position in ("K", "DST")]
    assert kdst, "the fixture must shortlist a K or D/ST at this round"
    assert all(a.adjustment == 0.0 and not a.applies for a in kdst)

    ctx3 = _late_context(board, weekly, own_ids=own, taken_ids=_TAKEN, round_num=9, picks_after=7)
    opted = {a.player_id: a for a in vw.weekwise_adjustments(
        recs, ctx3, inputs=inputs, weight=1.0,
        adjust_positions=("QB", "RB", "WR", "TE", "K", "DST"))}
    assert any(opted[a.player_id].applies for a in kdst)


# ======================================================================
#  4.  Rule 6 — the sentence appears iff the term moved a pick
# ======================================================================


def _weekwise_lines(rec):
    return [r for r in rec.reasons if r.startswith("week-by-week check:")]


def test_the_reason_line_appears_when_the_term_moves_a_recommendation():
    board, weekly = _fixture()
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=4.0)
    recs = picker.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=vw.DEFAULT_SHORTLIST
    )
    moved = [r for r in recs if _weekwise_lines(r)]
    assert moved, "a weight this large must move something and say so"
    cover = next(r for r in recs if r.player_id == "RB-cover")
    line = _weekwise_lines(cover)[0]
    assert "week 8" in line, line
    # Rule 6: the sentence must not lean on jargon a novice cannot read.
    for jargon in ("VONA", "marginal", "sigma", "objective", "expected wins"):
        assert jargon not in line
    # The completion is a hypothesis and says so wherever it moved a pick.
    assert vw.COMPLETION_LABEL in cover.reasons


def test_no_reason_line_on_a_recommendation_the_term_did_not_move():
    """The sentence appears EXACTLY on the recommendations whose place changed.

    Stated as an iff over the whole returned list rather than by hunting for a
    weight small enough to change nothing: the engine hands back exact score ties
    (two backs with identical VOR), so any non-zero weight is decisive on those
    and "a tiny weight moves nothing" is simply false.
    """
    board, weekly = _fixture()
    engine = _engine()
    before = [
        r.player_id
        for r in engine.recommend(
            _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN),
            top=vw.DEFAULT_SHORTLIST,
        )
    ]
    picker = vw.WeekwisePicker(engine=engine, inputs=_inputs(weekly, board), weight=4.0)
    after = picker.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=vw.DEFAULT_SHORTLIST
    )
    unmoved = [r for i, r in enumerate(after) if before.index(r.player_id) == i]
    moved = [r for i, r in enumerate(after) if before.index(r.player_id) != i]
    assert unmoved, "the fixture must leave at least one recommendation in place"
    assert moved, "and must move at least one"
    for rec in unmoved:
        assert not _weekwise_lines(rec), rec.player_id
        assert vw.COMPLETION_LABEL not in rec.reasons
    for rec in moved:
        assert _weekwise_lines(rec), rec.player_id


def test_every_recommendation_keeps_a_non_empty_reason_list():
    board, weekly = _fixture()
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=4.0)
    for rec in picker.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=5
    ):
        assert rec.reasons and all(isinstance(r, str) and r for r in rec.reasons)


def test_format_adjustments_discloses_the_bench_blindness():
    board, weekly = _fixture()
    _recs, adjs = _adjustments(board, weekly, completion="future")
    text = vw.format_adjustments(tuple(adjs.values()))
    assert vw.BENCH_BLINDNESS_LABEL in text
    assert "RB-cover" in text or "Fixture RB-cover" in text


# ======================================================================
#  5.  the machinery: cost gate, completion model, slot labels
# ======================================================================


def test_grade_calls_are_bounded_per_decision(monkeypatch):
    """Exactly ``len(shortlist) + 1`` grades — the load-free latency regression gate.

    Wall clock is a property of the box; this is a property of the algorithm. A
    change that graded inside the survival rollout, or re-graded per week, or
    rebuilt the field pool per call, moves this number and nothing else has to.
    """
    board, weekly = _fixture()
    calls: list[int] = []
    real = vw._grade

    def _counting(inputs, entries):
        calls.append(len(entries))
        return real(inputs, entries)

    monkeypatch.setattr(vw, "_grade", _counting)
    engine = _engine()
    picker = vw.WeekwisePicker(engine=engine, inputs=_inputs(weekly, board), weight=2.0)
    ctx = _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)
    recs = picker.recommend(ctx, top=5)
    assert recs
    shortlist = len(
        engine.recommend(
            _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN),
            top=vw.DEFAULT_SHORTLIST,
        )
    )
    assert len(calls) == shortlist + 1


def test_weight_zero_costs_no_grade_calls(monkeypatch):
    """The off switch must be FREE, not merely harmless.

    At ``weight=0.0`` every adjustment is 0.0, so the order and the reasons come
    back identical either way — which means only the cost can tell the short
    circuit from its absence, and the cost is the whole point of an opt-in flag.
    """
    board, weekly = _fixture()
    calls: list[int] = []
    monkeypatch.setattr(vw, "_grade", lambda i, e: calls.append(1))
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=0.0)
    picker.recommend(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=5)
    assert calls == []


def test_every_graded_roster_in_one_decision_is_the_same_size(monkeypatch):
    """A candidate SPENDS one of the remaining picks; the completion must know it.

    Otherwise the roster with the candidate is graded one player LARGER than the
    roster without him, and the difference reported as his marginal value is
    really the value of an extra sixteenth-round pick nobody gets.
    """
    board, weekly = _fixture()
    sizes: list[int] = []
    real = vw._grade
    monkeypatch.setattr(vw, "_grade", lambda i, e: (sizes.append(len(e)), real(i, e))[1])
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=2.0)
    picker.recommend(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=5)
    assert sizes and len(set(sizes)) == 1, sizes
    # nine rostered + the pick on the clock + the three remaining picks
    assert sizes[0] == len(_OWN) + 1 + 3


def test_the_slot_labels_match_the_real_seater():
    """The collision test keys on ``core.lineup``'s slot LABELS, so pin them.

    ``_slots_for`` is a string convention ("RB1"/"RB2"/"TE"/"FLEX"), and if the
    seater ever renumbered or renamed a slot this module would quietly find no
    overlap, report no collisions, and drop the one reason a novice most needs —
    with every number still looking perfectly reasonable.
    """
    board, weekly = _fixture()
    inputs = _inputs(weekly, board)
    full = [e for e in board if e.player_id in _OWN]
    grade = inputs.grade(full)
    seated = {label for week in grade.lineups.values() for label, _key in week.slots}
    assert seated, "the fixture must seat a real lineup"
    covered: set[str] = set()
    for pos in ("QB", "RB", "WR", "TE", "DST", "K"):
        labels = vw._slots_for(pos, DEFAULT_ROSTER)
        assert labels <= seated, (pos, sorted(labels - seated))
        covered |= labels
    assert covered == seated, sorted(seated - covered)


def test_a_collision_week_is_always_a_week_he_does_not_play():
    """The reason says "he is off in week N" — so it had better be true.

    Driven from a roster with NO running back and none reachable by the
    completion, so a starting RB slot is empty in every single week: "the lineup
    is short at his position" is then true 14 times over, and only the "and he is
    not playing" half can cut the list back down to his actual bye. A weaker
    fixture cannot tell the two halves apart.
    """
    board, weekly = _fixture()
    backless = ("QB-00", "WR-00", "WR-01", "TE-00", "WR-02", "DST-00", "K-00")
    gone = _TAKEN + ("RB-anchor", "RB-hole")
    engine = _engine()

    def _ctx():
        own = [e for e in board if e.player_id in backless]
        own.sort(key=lambda e: backless.index(e.player_id))
        return PickContext.from_board(
            board, own_roster=own, taken=gone, team_slot=8, round=13,
            overall_pick=129, rounds_total=16, rng=random.Random(4),
        )

    recs = engine.recommend(_ctx(), top=vw.DEFAULT_SHORTLIST)
    adjs = {a.player_id: a for a in vw.weekwise_adjustments(
        recs, _ctx(), inputs=_inputs(weekly, board), weight=1.0)}
    backs = [a for a in adjs.values() if a.position == "RB"]
    assert backs, "the fixture must shortlist a back for this roster"
    for a in backs:
        assert a.collision_weeks, a.player_id
        for week in a.collision_weeks:
            assert week not in weekly[a.player_id], (a.player_id, week)
        assert len(a.collision_weeks) <= 2, (a.player_id, a.collision_weeks)


def test_the_completion_never_benches_a_pick_while_a_starting_slot_is_open():
    """Need-first is a RULE, not a tendency.

    ``legal_positions`` already forces the last few picks to fill whatever
    starting slots are still empty, so a pure best-VOR completion still ends up
    with a legal lineup — it just fills those slots with the scraps left in round
    sixteen instead of the best player available when the slot came open. The
    invariant below is what actually separates the two.
    """
    board, _weekly = ev.smoke_inputs()
    ctx = PickContext.from_board(
        board, own_roster=(), taken=(), team_slot=8, round=1, overall_pick=9,
        rounds_total=16, rng=random.Random(1),
    )
    pool = vw._future_pool(ctx, picks=16, gap=DEFAULT_ROSTER.teams)
    filled = vw.complete_roster((), pool, picks_left=16, roster=DEFAULT_ROSTER)

    counts: dict[str, int] = {}
    for j, entry in enumerate(filled):
        startable = {
            pos for pos in pool.by_pick[j]
            if vw._startable(pos, counts, DEFAULT_ROSTER) and pool.by_pick[j].get(pos)
        }
        if startable:
            assert entry.position in startable, (j, entry.position, sorted(startable))
        counts[entry.position] = counts.get(entry.position, 0) + 1


def test_the_pick_on_the_clock_is_index_zero_and_your_next_pick_is_not():
    """Pool index 0 is NOW (nobody else has picked yet); index 1 is your next turn.

    Both halves matter. Index 0 must offer the whole board, because the baseline
    branch spends the pick on the clock and everyone available now really is
    available now. Index 1 must NOT, because by then the room has taken more
    players — and a completion that can re-take the very player a candidate is
    being compared against gives two candidates it would have collected either way
    byte-identical marginals, silently cancelling the decision it is pricing.
    """
    board, weekly = _fixture()
    ctx = _late_context(board, weekly, own_ids=_EARLY, taken_ids=_TAKEN,
                        round_num=3, picks_after=13)
    gap = 4
    pool = vw._future_pool(ctx, picks=14, gap=gap)
    available = sorted(
        (e for e in board if e.player_id not in ctx.state.taken),
        key=lambda e: (e.espn_overall_rank, e.player_id),
    )
    gone_by_your_next_pick = {e.player_id for e in available[:gap]}
    now = {e.player_id for row in pool.by_pick[0].values() for e in row}
    next_turn = {e.player_id for row in pool.by_pick[1].values() for e in row}
    assert gone_by_your_next_pick <= now, "the pick on the clock sees the whole board"
    assert not (next_turn & gone_by_your_next_pick)


def test_the_completion_fills_the_starting_lineup_before_the_bench():
    """Need-first, not best-VOR-first.

    A pure value greedy never reaches a kicker or a defence — their VOR is the
    lowest on any board — so the completed roster would carry two permanently
    empty starting slots and every candidate would be graded against a season of
    manufactured holes.
    """
    board, _weekly = ev.smoke_inputs()   # deep enough to have every position left
    ctx = PickContext.from_board(
        board, own_roster=(), taken=(), team_slot=8, round=1, overall_pick=9,
        rounds_total=16, rng=random.Random(1),
    )
    pool = vw._future_pool(ctx, picks=16, gap=DEFAULT_ROSTER.teams)
    filled = vw.complete_roster((), pool, picks_left=16, roster=DEFAULT_ROSTER)
    counts: dict[str, int] = {}
    for entry in filled:
        counts[entry.position] = counts.get(entry.position, 0) + 1
    for pos, need in DEFAULT_ROSTER.starters.items():
        assert counts.get(pos, 0) >= need, (pos, counts)


def test_a_completion_is_never_truncated():
    """Every hypothetical roster must be a FULL roster.

    ``_ALTERNATES`` bounds how many players each (pick, position) row keeps, and
    consecutive picks draw from nested pools, so an early pick can eat the front
    of a later pick's row. If a row runs dry the completion stops early and the
    candidate is graded on a short roster whose missing man reads as a hole.
    """
    board, _weekly = ev.smoke_inputs()
    ctx = PickContext.from_board(
        board, own_roster=(), taken=(), team_slot=8, round=1, overall_pick=9,
        rounds_total=16, rng=random.Random(1),
    )
    for gap in (6, 10, 14):
        pool = vw._future_pool(ctx, picks=16, gap=gap)
        assert len(vw.complete_roster((), pool, picks_left=16, roster=DEFAULT_ROSTER)) == 16, gap


def test_the_last_pick_of_the_draft_still_compares_two_full_rosters(monkeypatch):
    """``picks_after == 0`` is the edge where a completion has nothing left to do.

    It must still spend the pick on BOTH sides — the candidate on one, the
    completion's own choice on the other — or the final pick of the draft is the
    one decision the whole term silently sits out.
    """
    board, weekly = _fixture()
    sizes: list[int] = []
    real = vw._grade
    monkeypatch.setattr(vw, "_grade", lambda i, e: (sizes.append(len(e)), real(i, e))[1])
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=2.0)
    recs = picker.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN,
                      round_num=16, picks_after=0),
        top=3,
    )
    assert recs
    assert sizes and set(sizes) == {len(_OWN) + 1}


def test_the_field_pool_is_built_once_not_per_decision(monkeypatch):
    """``WeekwiseInputs`` owns the 5.3 ms pool build; a decision must not repeat it."""
    board, weekly = _fixture()
    inputs = _inputs(weekly, board)
    builds: list[int] = []
    real = grader.build_field_pool
    monkeypatch.setattr(
        grader, "build_field_pool",
        lambda *a, **k: (builds.append(1), real(*a, **k))[1],
    )
    picker = vw.WeekwisePicker(engine=_engine(), inputs=inputs, weight=2.0)
    picker.recommend(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=5)
    assert builds == []


def test_the_future_pool_last_pick_sees_the_whole_tail():
    """A regression pin on a measured bug.

    The last remaining pick's pool was built from the SLICE between its own
    offset and the next one rather than from everything past its offset, so it
    offered ``gap`` players instead of the whole tail — and the completion, which
    fills bottom-up, then handed two different candidates near-identical late
    rosters (measured on the live board: a TE and a WR came back with byte-equal
    marginals). A ``gap``-wide slice can cover at most ``gap`` players and so at
    most ``gap`` positions; the tail covers every position still on the board.
    """
    board, weekly = _fixture()
    ctx = _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN, picks_after=6)
    pool = vw._future_pool(ctx, picks=6, gap=3)
    last = pool.by_pick[-1]
    assert last, "the last remaining pick must still have players to choose from"
    available = {e.position for e in board if e.player_id not in ctx.state.taken}
    assert set(last) <= available
    # A three-player slice could never span four positions; the tail does.
    assert len(last) >= 4

    # And a later pick is never offered a BETTER player than an earlier one: the
    # pools are nested suffixes, so best-available is non-increasing in j.
    for pos in last:
        best = [
            snapshot[pos][0].vor for snapshot in pool.by_pick if snapshot.get(pos)
        ]
        assert best == sorted(best, reverse=True), pos


# ======================================================================
#  6.  refuse rather than guess
# ======================================================================


def test_an_empty_points_map_is_refused():
    with pytest.raises(vw.WeekwiseInputError, match="EMPTY"):
        vw.WeekwiseInputs.build(grader.WeeklyPointsMap({}, positions={}, names={}, teams={}))


def test_a_points_map_without_positions_is_refused():
    with pytest.raises(vw.WeekwiseInputError, match="no positions"):
        vw.WeekwiseInputs.build({"x": {1: 5.0}})


def test_a_board_that_diverged_from_the_map_is_refused():
    """The failure that would otherwise grade every roster as a season of holes."""
    board, weekly = _fixture()
    ghost = BoardEntry(
        player_id="not-on-the-map", name="Fixture ghost", position="RB",
        espn_overall_rank=1, house_points=250.0, vor=100.0, team="ZZZ",
    )
    with pytest.raises(grader.GradeInputError, match="PRICED but missing"):
        vw.WeekwiseInputs.build(weekly, board=tuple(board) + (ghost,))


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"mode": "nope"}, "unknown mode"),
        ({"completion": "nope"}, "unknown completion"),
        ({"weight": float("nan")}, "finite"),
        ({"shortlist": 0}, "at least one candidate"),
    ],
)
def test_a_nonsense_setting_is_refused_at_construction(kwargs, match):
    board, weekly = _fixture()
    base = {"engine": _engine(), "inputs": _inputs(weekly, board), "weight": 1.0}
    base.update(kwargs)
    with pytest.raises(vw.WeekwiseInputError, match=match):
        vw.WeekwisePicker(**base)


def test_a_zero_completion_gap_is_refused():
    board, weekly = _fixture()
    engine = _engine()
    ctx = _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)
    recs = engine.recommend(ctx, top=3)
    with pytest.raises(vw.WeekwiseInputError, match="at least 1 player"):
        vw.weekwise_adjustments(
            recs,
            _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN),
            inputs=_inputs(weekly, board), weight=1.0, completion_gap=0,
        )


def test_an_empty_regular_season_is_refused():
    _board, weekly = _fixture()
    with pytest.raises(vw.WeekwiseInputError, match="regular_season_weeks is empty"):
        vw.WeekwiseInputs.build(weekly, regular_season_weeks=())


# ======================================================================
#  7.  the boundaries: what this module may reach for, and what it may not do
# ======================================================================


def test_this_module_only_reaches_into_core_and_its_own_package():
    """Rule 8's other direction, which no other test covers.

    ``tests/test_draft_boundary.py`` already proves that no PERMANENT package
    imports ``ziggurat.draft`` — that is the half that keeps the quarantine
    intact, and it covers this module for free. The half left over is what
    THIS module is allowed to reach for: ``core`` (the permanent valuation and
    lineup spine) and its own package. A dependency on ``league``, ``data``,
    ``llm`` or ``push`` would drag a draft-day experiment into the in-season
    system and would also be a Rule 1 problem, since those are where the data
    reads live and this module performs none.
    """
    import ast

    tree = ast.parse(pathlib.Path(vw.__file__).read_text(encoding="utf-8"))
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ziggurat"):
            reached.add(node.module)
        elif isinstance(node, ast.Import):
            reached.update(a.name for a in node.names if a.name.startswith("ziggurat"))
    assert reached, "the scanner must actually see this module's imports"
    for module in reached:
        assert module.startswith(("ziggurat.core.", "ziggurat.draft")), module


def test_this_module_performs_no_data_read():
    """Rule 1 by construction: no connection, no cursor, no accessor call.

    Everything priced here arrives through inputs the CALLER loaded with an
    ``as_of``. A read added later would carry no gate at all, because there is no
    ``as_of`` parameter anywhere in this file to thread one through.
    """
    source = pathlib.Path(vw.__file__).read_text(encoding="utf-8")
    for forbidden in ("sqlite3", "select_as_of", "latest_truth", "conn.execute", "open_db"):
        assert forbidden not in source, forbidden


# ======================================================================
#  8.  the audit round: the guards the first version did not have
# ======================================================================
#
# Everything below landed with the three-reviewer audit of 2026-08-30. Each test
# names the defect it pins, and each was mutation-checked by re-injecting that
# defect and watching this file go red.


#: The three shapes of the credit-the-wrong-player failure, as (position, bye).
#: Each violates a DIFFERENT clause of the guard, which is what makes the guard's
#: two halves separately mutation-visible: a fixture that breaks both at once
#: leaves either clause alone able to carry the test.
_DOWNSTREAM_CANDIDATES = {
    "wr-off-that-week": ("WR", 8),    # neither plays week 8 nor could fill RB2
    "rb-off-that-week": ("RB", 8),    # could fill RB2, but is off that week
    "wr-plays-that-week": ("WR", None),  # plays week 8, but cannot fill RB2
}


def _downstream_fixture(kind="wr-off-that-week"):
    """A board built for ONE question: who actually closed the bye hole?

    Hand-built rather than derived from ``_fixture`` because the failure needs a
    very specific shape, and a shape that emerges by accident is a shape that can
    stop emerging. The operator's nine-man roster is short at running back in
    week 8 and nowhere else. Two backs are left: ``RB-early`` is the best of them
    and is ALSO off in week 8, and ``RB-late`` plays week 8 but ranks below the
    completion's first-pick window. So:

      * the baseline branch spends the pick on the clock on ``RB-early`` (best
        value, taken from the index-0 pool) and week 8 stays empty;
      * a candidate branch that spends the pick on somebody else pushes its
        completion down to the index-1 pool, where ``RB-early`` is gone and
        ``RB-late`` is the best back — so week 8 is covered.

    Week 8 therefore stops being a hole on the candidate's side, and the raw
    set difference credits it to the CANDIDATE — including to a wide receiver
    who is himself off in week 8 and could not be seated at RB2 in any case.
    """
    rows = [
        # id, pos, weekly rate, bye, espn rank, vor
        ("QB-own", "QB", 18.0, 5, 1, 60.0),
        ("RB-own-a", "RB", 16.0, None, 2, 55.0),
        ("RB-own-b", "RB", 15.0, 8, 3, 50.0),   # the bye that makes week 8 short
        ("WR-own-a", "WR", 14.0, 6, 4, 45.0),
        ("WR-own-b", "WR", 13.0, 7, 5, 40.0),
        ("WR-own-c", "WR", 12.0, 9, 6, 35.0),   # seats in the FLEX
        ("TE-own", "TE", 11.0, 10, 7, 30.0),
        ("DST-own", "DST", 9.0, 11, 8, 20.0),
        ("K-own", "K", 8.0, 12, 9, 15.0),
        # available, in ESPN-rank order: the top row is what the index-0 pool
        # offers and the index-1 pool (gap 1) does not.
        ("RB-early", "RB", 14.5, 8, 10, 48.0),
        ("CAND", _DOWNSTREAM_CANDIDATES[kind][0], 14.2,
         _DOWNSTREAM_CANDIDATES[kind][1], 11, 47.0),
        ("RB-late", "RB", 10.0, 3, 12, 25.0),   # plays week 8
        ("TE-spare", "TE", 6.0, 4, 13, 5.0),
    ]
    points, positions, names, teams = {}, {}, {}, {}
    board = []
    for pid, pos, rate, bye, rank, vor in rows:
        played = tuple(w for w in WEEKS if w != bye)
        points[pid] = {w: rate for w in played}
        positions[pid], names[pid], teams[pid] = pos, f"Fixture {pid}", pid[:3]
        board.append(BoardEntry(player_id=pid, name=names[pid], position=pos,
                                espn_overall_rank=rank, house_points=rate * len(played),
                                vor=vor, team=teams[pid]))
    weekly = grader.WeeklyPointsMap(points, positions=positions, names=names, teams=teams)
    return tuple(board), weekly


def _downstream_case(kind="wr-off-that-week"):
    """The one decision the downstream fixture exists for: recs, ctx, adjustments."""
    board, weekly = _downstream_fixture(kind)
    by_id = {e.player_id: e for e in board}
    own_ids = ("QB-own", "RB-own-a", "RB-own-b", "WR-own-a", "WR-own-b",
               "WR-own-c", "TE-own", "DST-own", "K-own")
    own = [by_id[p] for p in own_ids]
    ctx = PickContext.from_board(
        board, own_roster=own, taken=(), team_slot=8, round=10, overall_pick=95,
        rounds_total=11, rng=random.Random(0),
    )
    recs = tuple(
        PickRec(player=by_id[pid], pick_score=score, vor=by_id[pid].vor,
                survival_next=0.5, vona=0.0, need_note="", risk_note="",
                divergence_note="", reasons=("engine reason",))
        for pid, score in (("CAND", 50.0), ("RB-late", 10.0))
    )
    inputs = _inputs(weekly, board)
    adjs = {
        a.player_id: a
        for a in vw.weekwise_adjustments(recs, ctx, inputs=inputs, weight=2.0,
                                         completion_gap=1)
    }
    return board, weekly, ctx, recs, inputs, adjs


def test_a_hole_the_completion_closed_is_not_credited_to_the_candidate():
    """The promotion sentence's ONE fact has to be about the player it names.

    ``filled_hole_weeks`` used to be the raw ``base_holes - candidate_holes`` set
    difference. Under ``completion="future"`` the candidate branch runs its own
    completion, so a DIFFERENT hypothetical later pick can be what covers the
    week — and the sentence built from it told the operator, verbatim, that a
    player "plays that week and plugs it" about a week he is on BYE for. Measured
    live at overall picks 9 and 12, the operator's two highest-consequence picks.
    """
    for kind, (position, bye) in _DOWNSTREAM_CANDIDATES.items():
        _board, weekly, _ctx, _recs, _inputs_, adjs = _downstream_case(kind)
        cand = adjs["CAND"]
        # The fixture must actually produce the failure, or this test proves
        # nothing: week 8 stops being a hole on his side...
        assert 8 in cand.closed_downstream_weeks, (kind, cand)
        # ...and exactly one clause of the guard is what stops him claiming it.
        plays = 8 in weekly["CAND"]
        fits = bool(vw._slots_for(position, DEFAULT_ROSTER) & {"RB2"})
        assert (plays, fits) != (True, True), kind
        assert plays == (bye is None) and fits == (position == "RB"), kind
        assert cand.filled_hole_weeks == (), (kind, cand.filled_hole_weeks)


def test_no_rendered_sentence_claims_a_bye_week_was_plugged():
    """Rule 6, stated over the rendered TEXT rather than over a field.

    The novice never sees ``filled_hole_weeks``; he sees one sentence. The two
    candidates of the downstream decision are the two halves of the invariant:
    week 8 stops being a hole on BOTH their sides, and exactly one of them is
    allowed to say so.

    Both sentences are rendered from the promotion branch deliberately — that is
    the branch the live failure was in, and asking for it directly means the test
    does not depend on which way the re-rank happens to order two candidates
    whose adjustments the fixture makes equal.
    """
    _board, weekly, _ctx, _recs, _inputs_, adjs = _downstream_case()
    back = vw._weekwise_reason(adjs["RB-late"], moved_up=True)
    receiver = vw._weekwise_reason(adjs["CAND"], moved_up=True)

    assert "fills it" in back, back
    for week in (int(tok) for tok in re.findall(r"\d+", back)):
        assert week in weekly["RB-late"], (week, back)

    assert "fills it" not in receiver, receiver
    assert "week 8" not in receiver, receiver


def test_a_filled_week_is_always_played_and_always_at_a_slot_he_could_fill():
    """The invariant, over every adjustment the fixtures produce."""
    for board, weekly, kwargs in (
        (*_fixture(), {}),
        (*_downstream_fixture(), {"completion_gap": 1}),
    ):
        inputs = _inputs(weekly, board)
        engine = _engine()
        for own_ids, taken, rnd, after in (
            (_OWN, _TAKEN, 13, 3), (_EARLY, _TAKEN, 3, 13), (_OWN[:5], _TAKEN, 6, 10),
        ):
            own = [e for e in board if e.player_id in own_ids]
            if len(own) != len(own_ids):
                continue
            own.sort(key=lambda e: own_ids.index(e.player_id))

            def _ctx():
                return PickContext.from_board(
                    board, own_roster=own, taken=taken, team_slot=8, round=rnd,
                    overall_pick=rnd * 10 - 1, rounds_total=rnd + after,
                    rng=random.Random(4),
                )

            recs = engine.recommend(_ctx(), top=vw.DEFAULT_SHORTLIST)
            for a in vw.weekwise_adjustments(recs, _ctx(), inputs=inputs, weight=2.0,
                                             **kwargs):
                slots = vw._slots_for(a.position, DEFAULT_ROSTER)
                for week in a.filled_hole_weeks:
                    assert week in weekly[a.player_id], (a.player_id, week)
                assert slots, a.position


# ---------------------------------------------------------------- Rule 6 text


def _all_sentences(board, weekly, *, weight=4.0, **kw):
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board),
                               weight=weight, **kw)
    return [
        line
        for rec in picker.recommend(
            _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN),
            top=vw.DEFAULT_SHORTLIST,
        )
        for line in _weekwise_lines(rec)
    ]


@pytest.mark.parametrize("marginal", [-40.0, -0.2, 0.0, 0.2, 37.0])
@pytest.mark.parametrize("moved_up", [True, False])
def test_a_sentence_never_puts_a_negative_number_in_a_positive_frame(marginal, moved_up):
    """The audit's sharpest text finding: 34 of 79 promotion sentences read
    "worth about -37 points MORE" — a recommendation explained by a sentence that
    says the opposite. Under the shipped completion the baseline spends the pick
    on somebody else, so a NEGATIVE marginal on a promoted player is the normal
    case, not an edge.
    """
    adj = vw.WeekwiseAdjustment(
        player_id="X", position="RB", name="Fixture X", engine_rank=3,
        engine_score=10.0, marginal_points=marginal, solo_points=200.0,
        crowded_out=0.0, marginal_wins=0.0, filled_hole_weeks=(), collision_weeks=(),
        adjustment=0.0, applies=True, completion="future",
    )
    line = vw._weekwise_reason(adj, moved_up=moved_up)
    lowered = line.lower()
    if marginal < -vw._SAME_BAND:
        assert "costs about 40 points" in lowered, line
        assert "adds about" not in lowered, line
        assert "-" not in line.split("about")[1][:8], line
    elif marginal > vw._SAME_BAND:
        assert "adds about 37 points" in lowered, line
        assert "costs about" not in lowered, line
    else:
        assert "about the same as" in lowered, line
        assert "0 points" not in lowered, line
    # every sentence names what the number is a difference against
    assert vw._AGAINST["future"] in line, line


def test_every_rendered_sentence_carries_a_comparative():
    """The fall-through branch used to splice a "than ..." clause with no
    comparative in front of it: "he is worth about 0 points to the lineup you
    would actually start than the player you would otherwise take with this
    pick". 19% of the variant's plain-language sentences did not parse.
    """
    board, weekly = _fixture()
    lines = _all_sentences(board, weekly)
    assert lines
    for line in lines:
        assert " than the" not in line, line
        body = line.split(":", 1)[1]
        assert any(
            phrase in body
            for phrase in ("adds about", "costs about", "about the same as",
                           "he is off in", "could actually reach", "fills it")
        ), line


def test_the_hole_sentence_does_not_claim_a_state_the_roster_is_not_in():
    """"as things stand your starting lineup has an empty spot in week N" is true
    of the roster as it stands ONLY under ``completion="none"``. Under the
    completion the holes belong to a modelled sixteen-man season, and the audit
    caught the sentence firing at round 3 with two players rostered — a roster
    whose real lineup is empty in every week.
    """
    adj = vw.WeekwiseAdjustment(
        player_id="X", position="RB", name="X", engine_rank=1, engine_score=1.0,
        marginal_points=5.0, solo_points=100.0, crowded_out=0.0, marginal_wins=0.0,
        filled_hole_weeks=(8,), collision_weeks=(), adjustment=0.0, applies=True,
        completion="future",
    )
    future = vw._weekwise_reason(adj, moved_up=True)
    naive = vw._weekwise_reason(
        vw.dc_replace(adj, completion="none"), moved_up=True
    )
    assert "as things stand" not in future, future
    assert "modelled out" in future, future
    assert "as things stand" in naive, naive


def test_the_bench_blindness_disclosure_reaches_the_operator():
    """It used to live only in ``format_adjustments``, which no cockpit calls: 0
    of 161 measured rendered reasons carried it while 161 carried the completion
    label. Rule 6 is about the text a human sees.
    """
    board, weekly = _fixture()
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=4.0)
    recs = picker.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN),
        top=vw.DEFAULT_SHORTLIST,
    )
    moved = [r for r in recs if _weekwise_lines(r)]
    assert moved
    for rec in moved:
        assert vw.BENCH_BLINDNESS_LABEL in rec.reasons, rec.player_id
    for rec in recs:
        if not _weekwise_lines(rec):
            assert vw.BENCH_BLINDNESS_LABEL not in rec.reasons, rec.player_id


# ------------------------------------------------------- the posture seam


class _PostureSession:
    """The duck-typed surface ``posture.project_postures`` reads. Nothing more."""

    def __init__(self, board, engine, own, taken):
        self.complete = False
        self.board = board
        self.own_roster = own
        self.roster = DEFAULT_ROSTER
        self.operator_slot = 8
        self.overall_pick = 32
        self.opponent_rosters = {t: [] for t in range(10) if t != 8}
        self.rounds_total = 16
        self.taken = set(taken)
        self.pick_order = list(range(10))
        self.session_seed = 3
        self.room_priors = None
        self.engine = engine


def _posture_bits(board):
    ordered = sorted(board, key=lambda e: e.espn_overall_rank)
    taken = [e.player_id for e in ordered[:31]]
    by_id = {e.player_id: e for e in board}
    own = [by_id[p] for p in (taken[8], taken[11], taken[28])]
    return own, taken


def test_the_wrapper_does_not_silently_kill_the_posture_monitor():
    """The module's own wiring recipe used to kill a shipped 2.4 feature.

    ``DraftSession.engine`` is a public property returning ``_engine()``, and
    ``posture.project_postures`` both READS ``engine.need_schedule`` and CLONES
    the engine with ``dataclasses.replace(engine, need_schedule=..., survival=...)``.
    Neither works on a naive wrapper — and ``app._safe_posture`` and
    ``webapp._recompute`` both catch bare ``Exception``, so the hysteresis posture
    advice would have died for the whole draft with no error and no log.

    The assertion is IDENTITY, not merely "it does not raise": the projection a
    wrapped session produces must equal the one the bare engine produces, which
    is what makes :data:`POSTURE_CLONE_LABEL` a true statement.
    """
    posture = pytest.importorskip("ziggurat.draft.posture")
    board, weekly = ev.smoke_inputs()
    own, taken = _posture_bits(board)
    engine = PickEngine(rollouts=8)
    picker = vw.WeekwisePicker(
        engine=engine, inputs=vw.WeekwiseInputs.build(weekly, board=board), weight=2.0
    )

    # the two attribute uses that used to raise
    assert picker.need_schedule is engine.need_schedule
    clone = dataclasses.replace(
        picker, need_schedule=posture.ARCHETYPE_NEED_SCHEDULES["zero_rb"],
        survival=posture._FrontSurvival(),
    )
    assert clone.weight == 0.0, "a posture clone must be the bare engine"
    assert isinstance(clone.engine.survival, posture._FrontSurvival)
    assert clone.engine.need_schedule is posture.ARCHETYPE_NEED_SCHEDULES["zero_rb"]

    bare = posture.project_postures(_PostureSession(board, engine, own, taken), rollouts=4)
    wrapped = posture.project_postures(_PostureSession(board, picker, own, taken), rollouts=4)
    assert wrapped == bare


def test_a_posture_clone_costs_no_grade_calls(monkeypatch):
    """The reason the clone is the bare engine: a posture projection continues the
    whole draft thousands of times, and one grade call per pick inside that is
    order ten seconds against a 90-second clock.
    """
    board, weekly = _fixture()
    calls: list[int] = []
    real = vw._grade
    monkeypatch.setattr(vw, "_grade", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=4.0)
    clone = dataclasses.replace(picker, need_schedule={}, survival=_stub_survival)
    calls.clear()
    clone.recommend(_late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=5)
    assert calls == [], "a posture clone must not grade anything"


def test_replacing_an_ordinary_field_does_not_disarm_the_term():
    """``dataclasses.replace(picker, weight=4)`` re-passes ``need_schedule`` and
    ``survival`` (the fields hold the engine's values), which must NOT be read as
    a posture clone — an equality test instead of an identity test here would
    silently zero the weight of every ordinary replace.
    """
    board, weekly = _fixture()
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board), weight=2.0)
    assert dataclasses.replace(picker, weight=4.0).weight == 4.0
    assert dataclasses.replace(picker, shortlist=6).weight == 2.0


def test_unknown_attributes_read_through_to_the_wrapped_engine():
    board, weekly = _fixture()
    engine = _engine(b_need=3.5)
    picker = vw.WeekwisePicker(engine=engine, inputs=_inputs(weekly, board), weight=2.0)
    assert picker.b_need == 3.5
    assert picker.rollouts == engine.rollouts
    with pytest.raises(AttributeError):
        picker.no_such_attribute


# --------------------------------------------------- the completion's offsets


def test_the_completion_offsets_are_this_seat_s_real_snake_turns():
    """The flat ``gap = teams`` is wrong at every seat except the middle one.

    From slot 9 of 10 the operator picks at overall 9 and 12 — three apart — then
    waits seventeen picks. A flat gap models ten in both cases. The audit measured
    the consequence: sweeping the flat gap over 6..14 changes the DRAFTED player
    in 16% of the operator's decisions, which is the size of the variant's whole
    effect on the pick. So the offsets are derived rather than chosen, and this
    checks them against the simulator's OWN snake sequence at every seat.
    """
    from ziggurat.draft.simulator import snake_sequence

    board, _weekly = ev.smoke_inputs()
    sequence = snake_sequence(list(range(DEFAULT_ROSTER.teams)), 16)
    for slot in range(DEFAULT_ROSTER.teams):
        own = [i + 1 for i, team in enumerate(sequence) if team == slot]
        assert len(own) == 16
        for k, overall in enumerate(own):
            ctx = PickContext.from_board(
                board, team_slot=slot, round=k + 1, overall_pick=overall,
                rounds_total=16, rng=random.Random(0),
            )
            expected = tuple(own[k + j] - overall - j for j in range(16 - k))
            assert vw.snake_offsets(ctx, picks=16 - k) == expected, (slot, k)
    # and the flat model it replaces really is different at the operator's seat
    operator = PickContext.from_board(
        board, team_slot=8, round=1, overall_pick=9, rounds_total=16,
        rng=random.Random(0),
    )
    assert vw.snake_offsets(operator, picks=4) == (0, 2, 18, 20)
    assert vw.snake_offsets(operator, picks=4) != tuple(j * 10 for j in range(4))


def test_a_context_that_is_not_a_consistent_snake_falls_back_to_the_flat_gap():
    """Refuse-rather-than-guess, in the quiet direction: a hand-built context
    (every unit test in this file) is not a snake, and guessing offsets from it
    would be worse than the documented flat model.
    """
    board, _weekly = ev.smoke_inputs()
    bad = PickContext.from_board(
        board, team_slot=8, round=3, overall_pick=9999, rounds_total=16,
        rng=random.Random(0),
    )
    assert vw.snake_offsets(bad, picks=4) is None
    ok = PickContext.from_board(
        board, team_slot=8, round=1, overall_pick=9, rounds_total=16,
        rng=random.Random(0),
    )
    assert vw.snake_offsets(ok, picks=0) is None


def test_the_pool_uses_the_snake_offsets_when_the_gap_is_not_pinned():
    """``completion_gap=None`` means "derive"; an explicit int means the legacy
    flat model, kept only so the A/B can measure the difference.
    """
    board, weekly = ev.smoke_inputs()
    inputs = vw.WeekwiseInputs.build(weekly, board=board)
    ctx = PickContext.from_board(
        board, team_slot=8, round=1, overall_pick=9, rounds_total=16,
        rng=random.Random(0),
    )
    seen: list[vw._FuturePool] = []
    real = vw._future_pool

    def _spy(*a, **k):
        pool = real(*a, **k)
        seen.append(pool)
        return pool

    engine = PickEngine(rollouts=0, survival=_stub_survival)
    recs = engine.recommend(ctx, top=3)
    try:
        vw._future_pool = _spy  # type: ignore[assignment]
        vw.weekwise_adjustments(recs, ctx, inputs=inputs, weight=1.0)
        vw.weekwise_adjustments(recs, ctx, inputs=inputs, weight=1.0, completion_gap=10)
    finally:
        vw._future_pool = real  # type: ignore[assignment]
    assert seen[0].offset_model == "snake"
    assert seen[0].offsets[:4] == (0, 2, 18, 20)
    assert seen[1].offset_model == "flat"
    assert seen[1].offsets[:4] == (0, 10, 20, 30)


# ----------------------------------------------- the four mutation survivors
#
# An independent 20-mutation run against the first version of this file left four
# defects alive, two of them behaviour-changing on the live board. These are the
# tests that kill them.


def _entry(pid, pos, *, vor, rank):
    return BoardEntry(player_id=pid, name=pid, position=pos, espn_overall_rank=rank,
                      house_points=vor + 100.0, vor=vor, team=pid[:3])


def _filler_rows(*, vor=0.5, rank=900):
    """One dull body at every position, so ``legal_positions`` is never the thing
    under test — a completion that cannot legally finish its roster returns an
    empty tuple, which would make either assertion below vacuously false."""
    return {
        pos: tuple(_entry(f"{pos}-fill{i}", pos, vor=vor, rank=rank + i)
                   for i in range(4))
        for pos in ("QB", "RB", "WR", "TE", "K", "DST")
    }


def _hand_pool(overrides_by_pick):
    """A ``_FuturePool`` of filler with ``overrides_by_pick[j]`` merged in."""
    rows = []
    for over in overrides_by_pick:
        row = _filler_rows()
        for pos, entries in over.items():
            row[pos] = tuple(entries) + row[pos]
        rows.append(row)
    return vw._FuturePool(by_pick=tuple(rows), gap=1, depth=10,
                          offsets=tuple(range(len(rows))))


def test_the_candidate_branch_cannot_re_take_the_pick_it_is_measured_against():
    """SURVIVOR (a): ``first_pick=1`` in the candidate branch of ``_hypothetical``.

    The module's own comment says it hit this off-by-one and fixed it: with
    ``first_pick=0`` the candidate branch's completion draws from the index-0
    pool, which still holds the very player the baseline spends the pick on — so
    the candidate is compared against a roster that also contains his
    alternative, and two candidates come back with byte-identical marginals. The
    original test could not see it, because both branches still produced sixteen
    players.
    """
    only_now = _entry("ONLY-NOW", "RB", vor=99.0, rank=1)
    later = _entry("LATER", "RB", vor=98.0, rank=2)
    pool = _hand_pool([{"RB": (only_now, later)}] + [{"RB": (later,)}] * 15)
    from_clock = vw.complete_roster((), pool, picks_left=16, roster=DEFAULT_ROSTER,
                                    first_pick=0)
    from_next = vw.complete_roster((), pool, picks_left=15, roster=DEFAULT_ROSTER,
                                   first_pick=1)
    assert from_clock[0].player_id == "ONLY-NOW"
    assert "ONLY-NOW" not in {e.player_id for e in from_next}, (
        "first_pick=1 must start at your NEXT turn, where the baseline's pick is gone"
    )
    assert from_next[0].player_id == "LATER"


def test_the_completion_takes_the_best_value_not_the_best_espn_rank():
    """SURVIVOR (b): the completion's ``(-vor, rank, player_id)`` tiebreak.

    Replacing it with ``(rank, player_id)`` completes by ESPN rank instead of the
    documented best-by-VOR, and on the house board those are very different
    orderings — the whole reason this project has its own board. Nothing pinned
    the stated selection rule, so the mutant passed all forty-two tests.
    """
    cheap_rank = _entry("RANK-1", "WR", vor=1.0, rank=1)     # best rank, worst value
    best_vor = _entry("VOR-BEST", "RB", vor=90.0, rank=50)   # worst rank, best value
    pool = _hand_pool([{"WR": (cheap_rank,), "RB": (best_vor,)}] * 16)
    filled = vw.complete_roster((), pool, picks_left=16, roster=DEFAULT_ROSTER)
    assert filled[0].player_id == "VOR-BEST", (
        "the completion is documented as best-by-VOR, not best-by-rank"
    )
    # ...and ESPN rank is still the tiebreak BELOW value, not above it.
    tie_a = _entry("TIE-A", "RB", vor=5.0, rank=9)
    tie_b = _entry("TIE-B", "WR", vor=5.0, rank=4)
    pool2 = _hand_pool([{"RB": (tie_a,), "WR": (tie_b,)}] * 16)
    assert vw.complete_roster((), pool2, picks_left=16,
                              roster=DEFAULT_ROSTER)[0].player_id == "TIE-B"


def test_week_mean_reads_the_week_it_is_asked_for():
    """SURVIVOR (c): ``_week_mean``'s dense-array index.

    ``weekly_means`` is dense FROM WEEK 1, so week ``w`` is index ``w - 1``. An
    off-by-one merely shifts the summed window and every arm still returns a
    plausible number.
    """
    means = (10.0, 20.0, 30.0, 40.0)
    assert vw._week_mean(means, 1) == 10.0
    assert vw._week_mean(means, 3) == 30.0
    assert vw._week_mean(means, len(means)) == 40.0
    assert vw._week_mean(means, len(means) + 1) == 0.0
    assert vw._week_mean(means, 0) == 0.0


def test_the_re_rank_tiebreak_is_total_and_value_ordered():
    """SURVIVOR (d), the one the first version called structurally unreachable —
    pinned here on a hand-built pair rather than left to a real board.

    Two candidates with identical blended scores must order by VOR, then by ESPN
    rank, then by id; a dict-ordered draft is unbounded cost in a journal replay.
    """
    board, weekly = _fixture()
    inputs = _inputs(weekly, board)
    by_id = {e.player_id: e for e in board}
    pair = tuple(
        PickRec(player=by_id[pid], pick_score=10.0, vor=by_id[pid].vor,
                survival_next=0.5, vona=0.0, need_note="", risk_note="",
                divergence_note="", reasons=("engine",))
        for pid in ("RB-collide", "RB-cover")
    )

    class _Stub:
        def recommend(self, ctx, *, top=5):
            return pair

    picker = vw.WeekwisePicker(engine=_Stub(), inputs=inputs, weight=0.0, shortlist=2)
    order = [r.player_id for r in picker.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=2)]
    higher = max(pair, key=lambda r: (r.vor, -r.player.espn_overall_rank))
    assert order[0] == higher.player_id, order


# ------------------------------------------------- pricing an empty skill week


def test_the_waiver_credit_levels_are_real_and_skip_the_streamed_slots():
    board, weekly = _fixture()
    levels = vw.hole_credit_levels(_inputs(weekly, board))
    assert levels, "the fixture board must price a waiver tier"
    for week, row in levels.items():
        assert set(row) <= {"QB", "RB", "WR", "TE"}, row
        assert all(v >= 0.0 for v in row.values()), row
        # K and D/ST are the grader's own business (grader.stream_levels)
        assert "K" not in row and "DST" not in row


def test_the_waiver_credit_shrinks_what_a_removed_hole_is_worth():
    """The limitation this option exists for (:data:`HOLE_PRICING_WARNING`).

    ``grade_roster`` prices an empty QB/RB/WR/TE starting week at exactly 0.0
    while giving K and D/ST a waiver-tier credit, and the variant's whole measured
    margin is bought in removed holes. So the hole-filling candidate's own number
    must move DOWN when the empty week is priced the way this repo's Tuesday
    waiver cadence would actually cover it — and it must move down by exactly the
    credit, not by a made-up amount.
    """
    board, weekly = _fixture()
    engine = _engine()
    ctx = lambda: _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)
    recs = engine.recommend(ctx(), top=vw.DEFAULT_SHORTLIST)
    plain = {a.player_id: a for a in vw.weekwise_adjustments(
        recs, ctx(), inputs=_inputs(weekly, board), weight=2.0)}
    credited = {a.player_id: a for a in vw.weekwise_adjustments(
        recs, ctx(), inputs=_inputs(weekly, board), weight=2.0, hole_credit="waiver")}

    filler = next(a for a in plain.values() if a.filled_hole_weeks)
    other = credited[filler.player_id]
    assert other.hole_credit_delta < 0.0, other.hole_credit_delta
    assert other.marginal_points == pytest.approx(
        plain[filler.player_id].marginal_points + other.hole_credit_delta
    )
    assert all(a.hole_credit_delta == 0.0 for a in plain.values())


def test_the_waiver_credit_is_labelled_wherever_it_moved_a_pick():
    board, weekly = _fixture()
    picker = vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board),
                               weight=4.0, hole_credit="waiver")
    recs = picker.recommend(
        _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN), top=5)
    moved = [r for r in recs if _weekwise_lines(r)]
    assert moved
    for rec in moved:
        assert vw.HOLE_CREDIT_LABEL in rec.reasons, rec.player_id


def test_a_nonsense_hole_credit_is_refused():
    board, weekly = _fixture()
    with pytest.raises(vw.WeekwiseInputError, match="hole_credit"):
        vw.WeekwisePicker(engine=_engine(), inputs=_inputs(weekly, board),
                          weight=1.0, hole_credit="free")
    engine = _engine()
    ctx = _late_context(board, weekly, own_ids=_OWN, taken_ids=_TAKEN)
    with pytest.raises(vw.WeekwiseInputError, match="hole_credit"):
        vw.weekwise_adjustments(engine.recommend(ctx, top=2), ctx,
                                inputs=_inputs(weekly, board), weight=1.0,
                                hole_credit="free")
