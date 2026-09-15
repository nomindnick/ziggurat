"""The lock fence, the LOCKS FIRST line and the counting staleness banner.

Items **3.13** (the seater must not move a player whose game is over) and **3.17
deliverables 2 and 3** (the lineup half), both added 2026-09-15 out of the edge
program's E01 probe and the Week-1 Monday retro.

WHY THIS IS A SEPARATE FILE. ``test_lineup_support.py`` pins item 3.5's DECISION
— posture, variance, the win-probability search. Everything here pins the
opposite thing: what the card may not TELL THE OPERATOR TO DO, and what it must
say out loud whether or not anything interesting happened. The live defect these
replace was not a wrong lineup, it was an unexecutable instruction printed in the
register of a recommendation (2026-09-13, through the shipped command: a rival's
WR with a finished Wednesday game and 5.6 PPR banked printed under "REMOVED …
cannot start week 1", with two bench receivers promoted into slots ESPN had
frozen). **Estimated points recovered: 0.00.** Rule 6 is the whole justification.

Rule 5: every player, team name and owner here is invented.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ziggurat.core import lineup_support
from ziggurat.core.lineup import FLEX_LABEL, LineupFill
from ziggurat.core.lineup_support import (
    LOCKED_CARRY_LABEL,
    StartabilityError,
    assert_no_illegal_starters,
    build_lineup,
    format_lineup_recommendation,
    order_slots_by_lock,
)

SEASON = 2026
PULL = "2026-09-15"
WEEK = 3
TEAM = 10
OPP_TEAM = 11
ET = ZoneInfo("America/New_York")

# CHI plays the midweek game; every other club plays the Sunday early wave.
MIDWEEK = ("2026-09-16", "20:15")
SUNDAY = ("2026-09-20", "13:00")
MONDAY = ("2026-09-21", "20:15")

# A decision clock AFTER the midweek kickoff and BEFORE the Sunday wave — the
# exact window the live defect fired in.
AFTER_MIDWEEK = datetime(2026, 9, 17, 9, 0, tzinfo=ET)
BEFORE_ANYTHING = datetime(2026, 9, 16, 9, 0, tzinfo=ET)


# --------------------------------------------------------------------- builders


def _sched(db, *, game_id, home, away, day, time_, week=WEEK, season=SEASON):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, gametime, "
        "home_team, away_team, retrieved_as_of, knowable_as_of) VALUES "
        "(?, ?, ?, 'REG', ?, ?, ?, ?, '2026-08-01', '2026-08-01')",
        (game_id, season, week, day, time_, home, away),
    )


def _matchup(db, *, home_team_id, away_team_id, week=WEEK, season=SEASON):
    db.execute(
        "INSERT INTO league_matchups (season, week, home_team_id, away_team_id, "
        "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?)",
        (season, week, home_team_id, away_team_id, PULL, PULL),
    )


def _slate(db, *, chi=MIDWEEK, tb=SUNDAY, extra=()):
    """The week's games. CHI is the midweek opener; everyone else plays Sunday."""
    _sched(db, game_id="G_CHI", home="CHI", away="ARI", day=chi[0], time_=chi[1])
    _sched(db, game_id="G_TB", home="TB", away="CAR", day=tb[0], time_=tb[1])
    for i, team in enumerate(("TEN", "BUF", "DAL", "GB", "SEA", "JAX", "MIA", "NO",
                              "DEN", "KC", "HOU", "IND")):
        _sched(db, game_id=f"G_{team}", home=team, away=f"X{i}",
               day=SUNDAY[0], time_=SUNDAY[1])
    for game_id, home, away, day, time_ in extra:
        _sched(db, game_id=game_id, home=home, away=away, day=day, time_=time_)
    db.commit()


# THE ROSTER, with the ESPN starting slots the live rows carry. ``Wednesday
# Wideout`` is the E01 shape: a starter whose game is the midweek opener, tagged
# INJURY_RESERVE afterwards. ``Bench Wideout`` is the man the pre-3.13 seater
# promoted into his frozen slot.
_LOCKED_WR = "Wednesday Wideout"
_PROMOTED_WR = "Bench Wideout"


def _lock_specs():
    return [
        {"name": "Pocket Passer", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6,
         "on_team": TEAM, "slot": "QB"},
        {"name": "Anchor Runner", "pos": "RB", "team": "BUF", "pts": 22.0, "bye": 7,
         "on_team": TEAM, "slot": "RB"},
        {"name": "Second Runner", "pos": "RB", "team": "DAL", "pts": 20.0, "bye": 8,
         "on_team": TEAM, "slot": "RB"},
        {"name": _LOCKED_WR, "pos": "WR", "team": "CHI", "pts": 16.9, "bye": 9,
         "on_team": TEAM, "slot": "WR", "injury": "INJURY_RESERVE"},
        {"name": "Second Wideout", "pos": "WR", "team": "GB", "pts": 19.0, "bye": 10,
         "on_team": TEAM, "slot": "WR"},
        {"name": "Flex Wideout", "pos": "WR", "team": "TB", "pts": 18.0, "bye": 11,
         "on_team": TEAM, "slot": "FLEX"},
        {"name": _PROMOTED_WR, "pos": "WR", "team": "SEA", "pts": 17.0, "bye": 12,
         "on_team": TEAM},
        {"name": "Starter Tight", "pos": "TE", "team": "JAX", "pts": 13.0, "bye": 5,
         "on_team": TEAM, "slot": "TE"},
        {"name": "Home D/ST", "pos": "D/ST", "team": "MIA", "pts": 6.0, "bye": 8,
         "on_team": TEAM, "slot": "D/ST"},
        {"name": "Steady Kicker", "pos": "K", "team": "NO", "pts": 8.0, "bye": 8,
         "on_team": TEAM, "slot": "K"},
        {"name": "Depth Runner", "pos": "RB", "team": "DEN", "pts": 3.0, "bye": 5,
         "on_team": TEAM},
    ]


def _healthy_specs():
    """The same roster with the midweek starter NOT injured — what the LOCKS FIRST
    line has to handle, since its whole point is that it prints when nothing is
    wrong. (An injured midweek man is benched before his kickoff, so he would not be
    the earliest-locking SEATED starter at all.)"""
    specs = _lock_specs()
    specs[3] = {k: v for k, v in specs[3].items() if k != "injury"}
    specs[3]["pts"] = 21.0      # healthy, and clearly good enough to be seated
    return specs


def _build(db, **kwargs):
    kwargs.setdefault("week", WEEK)
    kwargs.setdefault("opponent_total", 120.0)
    return build_lineup(db, as_of=PULL, season=SEASON, own_team_id=TEAM, **kwargs)


def _seats(rec):
    return {s.player for s in rec.starters}


def _slot_of(rec):
    return {s.player: s.slot for s in rec.starters}


# ====================== 3.13 (a) the done-when =================================


def test_a_locked_starter_stays_seated_and_no_bench_body_takes_his_slot(db, marginal_world):
    """THE DONE-WHEN (item 3.13 grading clause), and the assertion that FAILS against
    pre-3.13 code in three independent ways.

    ``Wednesday Wideout`` played the midweek opener and was placed on INJURY_RESERVE
    afterwards. Pre-3.13 he was ``hard_out`` -> ``available=False`` -> benched, and
    ``Bench Wideout`` was promoted into a slot ESPN froze at kickoff. Post-3.13 he is
    PINNED into the WR slot the roster row says ESPN has him in, the promotion cannot
    happen, his projection is in the total, and nothing raises."""
    marginal_world(_lock_specs(), retrieved=PULL)
    _slate(db)

    rec = _build(db, now=AFTER_MIDWEEK)

    assert _LOCKED_WR in _seats(rec)                    # (1) still seated
    assert _PROMOTED_WR not in _seats(rec)              # (2) no promotion into his slot
    assert _slot_of(rec)[_LOCKED_WR].startswith("WR")   # ESPN's own slot, not FLEX
    # (3) his points are IN the total (carried at projection — see the honest
    # tradeoff test below; this is executability, not accuracy)
    assert rec.own_projected_total == pytest.approx(
        sum(s.proj_points for s in rec.starters))
    assert any(s.player == _LOCKED_WR and s.proj_points == pytest.approx(16.9)
               for s in rec.starters)


def test_the_locked_starter_is_flagged_locked_on_his_own_row(db, marginal_world):
    marginal_world(_lock_specs(), retrieved=PULL)
    _slate(db)
    rec = _build(db, now=AFTER_MIDWEEK)
    row = next(s for s in rec.starters if s.player == _LOCKED_WR)
    assert row.locked is True
    assert all(s.locked is False for s in rec.starters if s.player != _LOCKED_WR)
    blob = " ".join(row.reasons)
    assert "LOCKED" in blob and "kicked off" in blob


def test_before_his_kickoff_the_same_roster_behaves_exactly_as_it_did_pre_3_13(
        db, marginal_world):
    """THE OTHER HALF of the fence, and the reason the clock is the ``now`` decision
    clock rather than ``as_of``: with the same data read at a moment BEFORE the
    midweek kickoff, an INJURY_RESERVE tag is a live designation on a game that has
    not happened, so the man IS benched and the bench body IS promoted. A fix that
    pinned on ``as_of`` — or on the designation alone — would break this."""
    marginal_world(_lock_specs(), retrieved=PULL)
    _slate(db)
    rec = _build(db, now=BEFORE_ANYTHING)
    assert _LOCKED_WR not in _seats(rec)
    assert _PROMOTED_WR in _seats(rec)
    assert any(_LOCKED_WR in b and "HARD-OUT" in b for b in rec.sanity_blocks)
    assert rec.locked_notes == ()


# ====================== 3.13 (b) the taxonomy ==================================


def _taxonomy_specs():
    """One roster carrying all three not-seated cases at once, so their sentences
    can be compared side by side rather than one test at a time."""
    specs = _lock_specs()
    # HARD-OUT: ESPN rules him out and his game has NOT started (Sunday).
    specs[2] = {**specs[2], "injury": "OUT"}                       # Second Runner (DAL)
    # UNPRICEABLE: the feed forecast weeks 1-2 only, so week 3 is a blank row —
    # byte-identical to a bye row, which is exactly why it needs its own sentence.
    specs[6] = {**specs[6], "forecast": {1, 2}}                    # Bench Wideout (SEA)
    return specs


def test_the_three_not_seated_cases_get_three_distinct_sentences(db, marginal_world):
    """Item 3.13 design point 3. LOCKED / HARD-OUT / UNPRICEABLE must not read alike:
    one is 'nothing you can do', one is 'swap him', one is 'we do not know'. Pre-3.13
    all three printed under one REMOVED heading and the first was simply false."""
    marginal_world(_taxonomy_specs(), retrieved=PULL)
    _slate(db)
    rec = _build(db, now=AFTER_MIDWEEK)

    locked = " ".join(rec.locked_notes)
    blocks = list(rec.sanity_blocks)
    hard = next(b for b in blocks if "Second Runner" in b)
    unpriceable = next(b for b in blocks if _PROMOTED_WR in b)

    # each case is labelled, and the labels are different words
    assert "LOCKED" in locked and _LOCKED_WR in locked
    assert hard.startswith("HARD-OUT")
    assert unpriceable.startswith("UNPRICEABLE")
    assert len({"LOCKED", "HARD-OUT", "UNPRICEABLE"}) == 3

    # and they say DIFFERENT things about what the operator should do
    assert "NOTHING you can do" in locked
    assert "you can still fix" in hard and "swap him out" in hard
    assert "NOT a statement that he is out" in unpriceable and "verify" in unpriceable

    # the LOCKED man is NOT in the removal block — he is seated
    assert not any(_LOCKED_WR in b for b in blocks)
    assert _LOCKED_WR in _seats(rec)


def test_a_locked_player_is_carried_at_his_projection_with_the_honest_tradeoff(
        db, marginal_world):
    """Item 3.13 design point 4. There is no live-score source, so he is carried at
    his projection — and the page must say so AND state that on the one measured
    instance the projection was FURTHER from the truth than deleting him. A fix that
    quietly improved the total while claiming accuracy would be the worse outcome."""
    marginal_world(_lock_specs(), retrieved=PULL)
    _slate(db)
    rec = _build(db, now=AFTER_MIDWEEK)
    blob = " ".join(rec.locked_notes)
    assert "carried at his projected 16.9" in blob
    assert LOCKED_CARRY_LABEL in rec.locked_notes
    assert "FURTHER from the realised points" in LOCKED_CARRY_LABEL
    assert "actually execute" in LOCKED_CARRY_LABEL
    assert "NOT about the total being right" in LOCKED_CARRY_LABEL
    # and it reaches the rendered page without --reasons
    assert "LOCKED" in format_lineup_recommendation(rec, reasons=False)


def test_a_locked_starter_with_no_projection_is_carried_at_zero_and_says_why(
        db, marginal_world):
    """The third live variant: the Week-1 card printed 'no projection at this as-of …
    cannot be seated (verify manually)' on the morning that player's game kicked off.
    A locked man with no forecast is carried at 0.0, but 0.0 is an ABSENCE OF DATA and
    the sentence has to say that, or a novice reads it as a measured zero."""
    specs = _lock_specs()
    specs[3] = {**specs[3], "forecast": {1, 2}}        # the CHI starter, no week-3 row
    marginal_world(specs, retrieved=PULL)
    _slate(db)
    rec = _build(db, now=AFTER_MIDWEEK)
    blob = " ".join(rec.locked_notes)
    assert _LOCKED_WR in blob
    assert "carried at 0.0" in blob
    assert "absence of data, not a zero" in blob


# ====================== 3.13 (c) the search fences =============================


def test_the_posture_search_never_swaps_a_pinned_slot(db, marginal_world):
    """``_steepest_ascent`` is the second way a locked man could be unseated. Driven
    to both postures with an extreme opponent total, the pinned WR stays put — and
    the search is genuinely running (it moves somebody), so this is not a vacuous
    assertion about a search that never fired."""
    specs = _lock_specs()
    # a genuine near-tie the posture search WILL act on: a floor RB (lower sigma)
    # 0.1 points behind the seated FLEX wideout, so a favorite trades the 0.1 for
    # the floor and an underdog does not. Without it this test would be vacuous.
    specs.append({"name": "Floor Runner", "pos": "RB", "team": "HOU", "pts": 17.9,
                  "bye": 6, "on_team": TEAM})
    marginal_world(specs, retrieved=PULL)
    _slate(db)
    neutral = _seats(_build(db, now=AFTER_MIDWEEK))
    moved = False
    for delta in (+60.0, -60.0):
        base = _build(db, now=AFTER_MIDWEEK).own_projected_total
        rec = _build(db, now=AFTER_MIDWEEK, opponent_total=base + delta)
        assert rec.posture in ("UNDERDOG", "FAVORITE")
        assert _LOCKED_WR in _seats(rec)
        assert _slot_of(rec)[_LOCKED_WR].startswith("WR")
        moved = moved or _seats(rec) != neutral
    assert moved, "the posture search never moved anyone — the fence is untested"


def test_steepest_ascent_refuses_a_swap_it_would_otherwise_take(db):
    """The fence at the unit, where it can be shown to BITE rather than merely not
    fire: one slot, one strictly-better bench body. Unpinned the search takes the
    swap; pinned it declines the same swap on the same numbers. A mutant that drops
    ``pinned_keys`` makes the two calls agree and fails here."""
    structure = lineup_support.RosterStructure(
        teams=10, starters={"RB": 1}, flex_slots=0, bench_slots=1, ir_slots=0)
    seats = {
        "held": _bare_seat("held", "RB", "CHI", 10.0),
        "spare": _bare_seat("spare", "RB", "BUF", 14.0),
    }
    fill = LineupFill(total=10.0, slots=(("RB", "held"),), bench=("spare",),
                      starters=frozenset({"held"}))
    kwargs = dict(mu_opp=200.0, var_opp=100.0, variance=lineup_support.DEFAULT_VARIANCE,
                  mu_cap=lineup_support._MU_SACRIFICE_CAP)

    assert lineup_support._steepest_ascent(fill, seats, structure, **kwargs) == {"RB": "spare"}
    assert lineup_support._steepest_ascent(
        fill, seats, structure, pinned_keys=frozenset({"held"}), **kwargs) == {"RB": "held"}


def _bare_seat(key, pos, team, pts, *, available=True):
    return lineup_support._Seat(
        key=key, player=key, position=pos, team=team, espn_id=None, gsis_id=None,
        points=pts, sigma=lineup_support.DEFAULT_VARIANCE.sigma(pos, pts),
        injury_status=None, lineup_slot=None, on_bye=False, has_proj=True,
        hard_out=False, available=available)


def test_a_locked_bench_player_can_never_be_promoted_into_the_lineup(db, marginal_world):
    """The mirror fence. ESPN froze the bench body's slot at HIS kickoff too, so a man
    whose game has started cannot be moved IN even when he out-projects a starter. He
    stays visible on the bench with the reason — silently dropping a 24-point name off
    the page is exactly what a novice cannot interrogate."""
    specs = _lock_specs()
    # a monster on the bench whose game is the midweek opener
    specs.append({"name": "Locked Bench Back", "pos": "RB", "team": "CHI", "pts": 24.0,
                  "bye": 5, "on_team": TEAM})
    marginal_world(specs, retrieved=PULL)
    _slate(db)

    rec = _build(db, now=AFTER_MIDWEEK)
    assert "Locked Bench Back" not in _seats(rec)
    row = next(b for b in rec.bench if b.player == "Locked Bench Back")
    assert row.locked is True
    assert "LOCKED on your bench" in row.reasons[0]
    assert any("LOCKED (bench)" in n and "Locked Bench Back" in n
               for n in rec.locked_notes)
    # ... and before his kickoff, that same player DOES start (the fence is the clock)
    early = _build(db, now=BEFORE_ANYTHING)
    assert "Locked Bench Back" in _seats(early)


def test_an_unpriceable_locked_bench_row_says_its_zero_is_an_absence_of_data(
        db, marginal_world):
    """Caught on the live card, not in review, and it is this item's own trap in a
    new place: LOCKED takes a man out of the UNPRICEABLE block, so a bench body with
    no forecast printed a bare ``0.0 pts`` with the disclosure gone. The bench row is
    the only surface left that can say it."""
    specs = _lock_specs()
    specs[6] = {**specs[6], "team": "CHI", "forecast": {1, 2}}   # locked AND unpriceable
    marginal_world(specs, retrieved=PULL)
    _slate(db)
    rec = _build(db, now=AFTER_MIDWEEK)
    row = next(b for b in rec.bench if b.player == _PROMOTED_WR)
    assert row.locked is True and row.proj_points == pytest.approx(0.0)
    assert "an absence of data, not a measured zero" in row.reasons[0]
    # and he is NOT double-reported in the removal block
    assert not any(_PROMOTED_WR in b for b in rec.sanity_blocks)
    # ... and it REACHES THE PAGE at every verbosity. The first draft of this fix
    # put the sentence in `BenchRec.reasons`, which the BENCH section rendered
    # nowhere — a disclosure that exists only in the dataclass is not a disclosure.
    plain = format_lineup_recommendation(rec, reasons=False)
    assert "NO FORECAST at this as-of, not a measured zero" in plain
    verbose = format_lineup_recommendation(rec, reasons=True)
    assert row.reasons[0] in verbose


def test_assert_no_illegal_starters_exempts_a_locked_seat(db):
    """A designation that lands AFTER a man played is a status change, not an illegal
    start. Without the exemption the Rule-6 hard gate would REFUSE TO PRINT A CARD over
    a slot the operator cannot change — the gate turning into the outage."""
    fill = LineupFill(total=0.0, slots=(("WR1", "x"),), bench=(),
                      starters=frozenset({"x"}))
    with pytest.raises(StartabilityError):
        assert_no_illegal_starters(fill, byes=set(), statuses={"x": "INJURY_RESERVE"},
                                   week=WEEK, live_status=True)
    # locked -> exempt, both for the OUT check and the bye check
    assert_no_illegal_starters(fill, byes=set(), statuses={"x": "INJURY_RESERVE"},
                               week=WEEK, live_status=True, locked={"x"})
    assert_no_illegal_starters(fill, byes={"x"}, statuses={}, week=WEEK,
                               live_status=True, locked={"x"})


def test_order_slots_by_lock_never_relabels_a_pinned_player(db):
    """The points-neutral FLEX relabel must not move a LOCKED man's label either: ESPN
    shows him in one slot and the card must agree with the app the operator is reading."""
    slots = (("RB1", "a"), ("RB2", "b"), (FLEX_LABEL, "c"))
    fill = LineupFill(total=10.0, slots=slots, bench=(), starters=frozenset({"a", "b", "c"}))
    positions = {"a": "RB", "b": "RB", "c": "RB"}
    locks = {
        "a": datetime(2026, 9, 16, 20, 15, tzinfo=ET),   # earliest — and locked
        "b": datetime(2026, 9, 20, 13, 0, tzinfo=ET),
        "c": datetime(2026, 9, 20, 16, 25, tzinfo=ET),
    }
    relabeled, _note = order_slots_by_lock(fill, positions, locks, pinned={"a"})
    assert dict((lbl, k) for lbl, k in relabeled.slots)["RB1"] == "a"
    assert relabeled.starters == fill.starters


# ====================== 3.13 (d) the opponent side =============================


def _opp_specs():
    """The rival roster from the live instance: a WR who played the midweek opener,
    plus a bench receiver our optimiser would rather seat."""
    return [
        {"name": "Rival Passer", "pos": "QB", "team": "KC", "pts": 21.0, "bye": 6,
         "on_team": OPP_TEAM, "slot": "QB"},
        {"name": "Rival Runner", "pos": "RB", "team": "HOU", "pts": 18.0, "bye": 7,
         "on_team": OPP_TEAM, "slot": "RB"},
        {"name": "Rival Runner Two", "pos": "RB", "team": "IND", "pts": 15.0, "bye": 8,
         "on_team": OPP_TEAM, "slot": "RB"},
        {"name": "Rival Locked Wideout", "pos": "WR", "team": "CHI", "pts": 5.0,
         "bye": 9, "on_team": OPP_TEAM, "slot": "WR"},
        {"name": "Rival Wideout", "pos": "WR", "team": "GB", "pts": 14.0, "bye": 10,
         "on_team": OPP_TEAM, "slot": "WR"},
        {"name": "Rival Bench Wideout", "pos": "WR", "team": "SEA", "pts": 13.0,
         "bye": 11, "on_team": OPP_TEAM},
        {"name": "Rival Tight", "pos": "TE", "team": "JAX", "pts": 9.0, "bye": 5,
         "on_team": OPP_TEAM, "slot": "TE"},
        {"name": "Rival Flex", "pos": "WR", "team": "TB", "pts": 12.0, "bye": 12,
         "on_team": OPP_TEAM, "slot": "FLEX"},
        {"name": "Rival D/ST", "pos": "D/ST", "team": "DEN", "pts": 5.0, "bye": 8,
         "on_team": OPP_TEAM, "slot": "D/ST"},
        {"name": "Rival Kicker", "pos": "K", "team": "NO", "pts": 7.0, "bye": 8,
         "on_team": OPP_TEAM, "slot": "K"},
    ]


def test_the_opponent_lineup_applies_the_identical_pinning(db, marginal_world):
    """Item 3.13 design point 5. The opponent's total is priced through the SAME seater,
    so without the pin our optimiser re-seats HIS already-played WR out of the lineup
    and replaces him with a bench body — mis-stating the number the whole posture
    decision is made against. Measured on the live instance at up to 11.3 house points
    and 13.6 pp of win probability."""
    marginal_world(_lock_specs() + _opp_specs(), retrieved=PULL)
    _slate(db)
    _matchup(db, home_team_id=TEAM, away_team_id=OPP_TEAM)
    db.commit()

    after = build_lineup(db, as_of=PULL, season=SEASON, own_team_id=TEAM, week=WEEK,
                         now=AFTER_MIDWEEK)
    before = build_lineup(db, as_of=PULL, season=SEASON, own_team_id=TEAM, week=WEEK,
                          now=BEFORE_ANYTHING)

    # Before his kickoff the seater is free and seats his best three WRs
    # (14 + 13 + 12 in WR1/WR2/FLEX) -> 114.0. After it, ESPN has frozen the 5.0 man
    # in a WR slot, so the 12.0 FLEX body is the one that falls out: 114 - 12 + 5.
    assert before.opponent_total == pytest.approx(114.0)
    assert after.opponent_total == pytest.approx(107.0)
    # and the difference is not cosmetic: it moves the number the posture is read off
    assert after.win_prob > before.win_prob


# ====================== 3.17 d2 — the LOCKS FIRST line =========================


def test_locks_first_names_the_earliest_locking_starters_and_prints_unconditionally(
        db, marginal_world):
    """Item 3.17 deliverable 2. Week 1's lock check looked at ONE game and two starters
    played on the Thursday; it was caught ~11 h before the lock by luck of reading
    order, not by process. The line is now the first thing on the page whether or not
    anyone is Questionable — the pre-3.17 card printed a lock time ONLY inside a GTD
    contingency."""
    marginal_world(_healthy_specs(), retrieved=PULL)
    _slate(db)
    rec = _build(db, now=BEFORE_ANYTHING)

    assert rec.locks_first, "the LOCKS FIRST line is unconditional"
    head = rec.locks_first[0]
    assert head.startswith("LOCKS FIRST:")
    assert _LOCKED_WR in head                      # the midweek starter
    assert "2026-09-16T20:15" in head              # his ET kickoff
    assert "ET" in head
    # nobody is Questionable on this roster, so pre-3.17 nothing would have printed
    assert rec.contingencies == ()
    # and it renders at the TOP of the card, above the posture line
    text = format_lineup_recommendation(rec, reasons=False)
    lines = text.splitlines()
    assert lines[1].startswith("LOCKS FIRST:")
    assert lines.index(head) < next(i for i, ln in enumerate(lines) if "win prob" in ln)


def test_locks_first_names_every_starter_sharing_the_earliest_kickoff(db, marginal_world):
    """Week 1's actual shape: TWO starters in the same early game. Naming only one is
    how the second gets missed."""
    specs = _healthy_specs()
    specs[7] = {**specs[7], "team": "CHI"}          # Starter Tight also plays midweek
    marginal_world(specs, retrieved=PULL)
    _slate(db)
    rec = _build(db, now=BEFORE_ANYTHING)
    head = rec.locks_first[0]
    assert _LOCKED_WR in head and "Starter Tight" in head


def test_locks_first_reports_a_fully_locked_lineup_as_a_record_not_a_decision(
        db, marginal_world):
    marginal_world(_lock_specs(), retrieved=PULL)
    _slate(db, chi=MIDWEEK, tb=SUNDAY)
    rec = _build(db, now=datetime(2026, 9, 22, 9, 0, tzinfo=ET))   # after everything
    head = rec.locks_first[0]
    assert "already kicked off" in head
    assert "a record, not a decision" in head


def test_locks_first_degrades_loudly_when_no_kickoff_is_known(db, marginal_world):
    """No schedule rows at all: the line still prints and tells the operator to check
    the app himself, rather than silently omitting the deadline."""
    marginal_world(_lock_specs(), retrieved=PULL)
    rec = _build(db, now=BEFORE_ANYTHING)
    head = rec.locks_first[0]
    assert "no kickoff time is known" in head
    assert "ESPN app" in head


def test_locks_first_counts_the_starters_that_have_already_locked(db, marginal_world):
    marginal_world(_lock_specs(), retrieved=PULL)
    _slate(db)
    rec = _build(db, now=AFTER_MIDWEEK)
    blob = " ".join(rec.locks_first)
    assert "1 seated starter already locked" in blob
    assert "LOCKS FIRST:" in blob and "2026-09-20T13:00" in blob


# ====================== 3.17 d3 — the counting staleness banner ================


def _warn(rec):
    return next((f for f in rec.freshness if "WARNING" in f), None)


def test_the_projection_age_banner_counts_and_clears_the_card_when_no_starter_is_stale(
        db, marginal_world):
    """Item 3.17 deliverable 3. It fired on ONE orphan row of 3,229 and read as a
    blanket 'do not trust this card'. It must now name the COUNT and say whether a
    SEATED player is among them — a stale bench body is not a reason to distrust the
    card."""
    specs = _lock_specs()
    specs[10] = {**specs[10], "retrieved": "2026-08-01"}     # Depth Runner: bench, stale
    marginal_world(specs, retrieved=PULL)
    _slate(db)
    rec = _build(db, now=BEFORE_ANYTHING)

    warn = _warn(rec)
    assert warn is not None
    assert "1 of 11 projection rows" in warn                  # the COUNT, not "some"
    assert "NO seated starter is priced off one" in warn
    assert "not a reason to distrust the lineup" in warn
    assert "Depth Runner" not in _seats(rec)


def test_the_projection_age_banner_escalates_when_a_seated_starter_is_stale(
        db, marginal_world):
    """The other half, and the only version of this warning that should change what the
    operator does."""
    specs = _lock_specs()
    specs[0] = {**specs[0], "retrieved": "2026-08-01"}        # Pocket Passer: the QB
    marginal_world(specs, retrieved=PULL)
    _slate(db)
    rec = _build(db, now=BEFORE_ANYTHING)

    warn = _warn(rec)
    assert warn is not None
    assert "1 of 11 projection rows" in warn
    assert "1 SEATED starter is priced off one" in warn
    assert "before trusting this card" in warn
    assert "Pocket Passer" in _seats(rec)


def test_a_fresh_feed_raises_no_projection_age_banner_at_all(db, marginal_world):
    marginal_world(_lock_specs(), retrieved=PULL)
    _slate(db)
    rec = _build(db, now=BEFORE_ANYTHING)
    assert _warn(rec) is None


def test_the_stale_row_scan_reads_the_newest_pull_per_key_not_the_oldest_overall(db):
    """The pre-3.17 banner took ``min`` over every pull date in the whole feed, so one
    orphan row condemned 3,228 fresh ones AND a key that had been REFRESHED still
    counted as stale. Per key, newest-first, is the honest unit."""
    class _Line:
        def __init__(self, dates):
            self.retrieved_as_of = frozenset(dates)

    lines = {
        ("SKILL", "a"): _Line({"2026-08-01", "2026-09-14"}),   # refreshed -> fresh
        ("SKILL", "b"): _Line({"2026-08-01"}),                 # never refreshed -> stale
        ("SKILL", "c"): _Line({"2026-09-14"}),
    }
    stale = lineup_support._stale_projection_rows(lines, as_of=PULL)
    assert stale == [("SKILL", "b")]
