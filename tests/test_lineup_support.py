"""Weekly starter-recommender tests — item 3.5 (Module B: core/lineup_support.py).

Offline throughout: the synthetic ``marginal_world`` projection/league universe
plus hand-inserted schedules / matchup rows. Everything is synthetic by necessity
and by Rule 5 (no real colleague/player names) — the live DB is pre-draft, so a
real opponent roster and real in-season injuries do not exist to test against.

The tests that matter most pin MEASURED / design decisions: that a favorite seats
FLOORS and an underdog seats CEILINGS off the SAME near-tie roster (the load-
bearing posture flip); that a close margin returns the greedy points lineup
verbatim; that a seated starter is NEVER on bye or ruled OUT (Rule 6 hard gate);
that the slot-lock relabel preserves who starts and the total exactly; and that a
game-time decision yields a lock-ordered contingency, not a naive point pick.
"""

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ziggurat.core import lineup_support
from ziggurat.core.lineup import FLEX_LABEL, LineupFill
from ziggurat.core.lineup_support import (
    DEFAULT_VARIANCE,
    StartabilityError,
    assert_no_illegal_starters,
    build_lineup,
    format_lineup_recommendation,
    order_slots_by_lock,
    resolve_opponent,
    win_probability,
)
from ziggurat.core.marginal import WeekResolutionError
from ziggurat.league.state import OwnTeamUnresolved

SEASON = 2026
PULL = "2026-09-15"
WEEK = 3
TEAM = 10
ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------- builders


def _sched(db, *, game_id, week, home, away, gameday, gametime="13:00",
           season=SEASON, knowable="2026-08-01", retrieved="2026-08-01"):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, gametime, "
        "home_team, away_team, retrieved_as_of, knowable_as_of) VALUES "
        "(?, ?, ?, 'REG', ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, gameday, gametime, home, away, retrieved, knowable),
    )


def _matchup(db, *, week, home_team_id, away_team_id, season=SEASON, retrieved=PULL):
    db.execute(
        "INSERT INTO league_matchups (season, week, home_team_id, away_team_id, "
        "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?)",
        (season, week, home_team_id, away_team_id, retrieved, retrieved),
    )


# A 16-man roster whose FLEX is a GENUINE near-tie: a boomier TE (mu 18.0, higher
# sigma via TE's steeper b) barely out-projects a floor RB (mu 17.9, lower sigma).
# Greedy seats the boom TE; a favorite should trade the 0.1 pts for the RB's floor.
_FLOOR_RB = "Floor Runner"
_BOOM_TE = "Boom Tighty"


def _contest_specs():
    return [
        {"name": "Pocket Passer", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": TEAM},
        {"name": "Backup Passer", "pos": "QB", "team": "SF", "pts": 8.0, "bye": 9, "on_team": TEAM},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 22.0, "bye": 11, "on_team": TEAM},
        {"name": "Second Runner", "pos": "RB", "team": "BUF", "pts": 20.0, "bye": 7, "on_team": TEAM},
        {"name": _FLOOR_RB, "pos": "RB", "team": "CHI", "pts": 17.9, "bye": 9, "on_team": TEAM},
        {"name": "Depth Runner", "pos": "RB", "team": "DEN", "pts": 3.0, "bye": 5, "on_team": TEAM},
        {"name": "First Wideout", "pos": "WR", "team": "DAL", "pts": 19.0, "bye": 8, "on_team": TEAM},
        {"name": "Second Wideout", "pos": "WR", "team": "GB", "pts": 16.0, "bye": 10, "on_team": TEAM},
        {"name": "Third Wideout", "pos": "WR", "team": "HOU", "pts": 10.0, "bye": 12, "on_team": TEAM},
        {"name": "Fourth Wideout", "pos": "WR", "team": "IND", "pts": 4.0, "bye": 13, "on_team": TEAM},
        {"name": "Starter Tight", "pos": "TE", "team": "JAX", "pts": 19.0, "bye": 5, "on_team": TEAM},
        {"name": _BOOM_TE, "pos": "TE", "team": "KC", "pts": 18.0, "bye": 14, "on_team": TEAM},
        {"name": "Home D/ST", "pos": "D/ST", "team": "MIA", "pts": 6.0, "bye": 8, "on_team": TEAM},
        {"name": "Steady Kicker", "pos": "K", "team": "NO", "pts": 8.0, "bye": 8, "on_team": TEAM},
        {"name": "Fifth Wideout", "pos": "WR", "team": "SEA", "pts": 7.0, "bye": 7, "on_team": TEAM},
        {"name": "Sixth Wideout", "pos": "WR", "team": "TB", "pts": 5.0, "bye": 12, "on_team": TEAM},
    ]


def _seat_names(rec):
    return {s.player for s in rec.starters}


def _build(db, **kwargs):
    kwargs.setdefault("week", WEEK)
    return build_lineup(db, as_of=PULL, season=SEASON, own_team_id=TEAM, **kwargs)


# ============================ (a) the DONE-WHEN posture flip ==================


def test_favorite_seats_the_floor_and_underdog_seats_the_boom(db, marginal_world):
    """DONE-WHEN: from ONE near-tie roster, a FAVORITE (opp 20 below) seats the
    FLOOR player and an UNDERDOG (opp 20 above) seats the BOOM player, and the two
    starter sets DIFFER (the load-bearing assertion)."""
    marginal_world(_contest_specs(), retrieved=PULL)
    base = _build(db)                       # no opponent -> NEUTRAL, greedy total
    g = base.own_projected_total

    fav = _build(db, opponent_total=g - 20)
    und = _build(db, opponent_total=g + 20)

    assert fav.posture == "FAVORITE"
    assert und.posture == "UNDERDOG"

    fav_names, und_names = _seat_names(fav), _seat_names(und)
    assert _FLOOR_RB in fav_names            # favorite protects the lead with a floor
    assert _BOOM_TE in und_names             # underdog chases with the boom
    assert fav_names != und_names            # THE differ assertion


def test_greedy_seats_the_boom_by_points(db, marginal_world):
    """Sanity precondition for the flip: the boom TE out-projects the floor RB, so
    the points-greedy (NEUTRAL) lineup seats the boom and benches the floor."""
    marginal_world(_contest_specs(), retrieved=PULL)
    base = _build(db)
    assert base.posture == "NEUTRAL"
    assert _BOOM_TE in _seat_names(base)
    assert _FLOOR_RB not in _seat_names(base)


# ============================ (b) close-band NEUTRAL ==========================


def test_a_close_margin_returns_the_greedy_lineup_verbatim(db, marginal_world):
    """|margin| < close band -> NEUTRAL -> the greedy E-points lineup, unchanged."""
    marginal_world(_contest_specs(), retrieved=PULL)
    base = _build(db)
    g = base.own_projected_total
    neutral = _build(db, opponent_total=g)   # margin exactly 0
    assert neutral.posture == "NEUTRAL"
    assert _seat_names(neutral) == _seat_names(base)
    assert neutral.own_projected_total == pytest.approx(base.own_projected_total)


# ============================ (c) win-prob monotonicity =======================


def test_win_prob_is_one_half_at_margin_zero(db, marginal_world):
    marginal_world(_contest_specs(), retrieved=PULL)
    base = _build(db)
    even = _build(db, opponent_total=base.own_projected_total)
    assert even.win_prob == pytest.approx(0.5, abs=1e-9)


def test_win_probability_is_monotone_in_opponent_and_variance():
    """(c) strictly decreasing in opponent total; lower variance moves a FAVORITE
    up and an UNDERDOG down (the posture lever)."""
    # decreasing in opponent total
    assert win_probability(100, 90, 300, 300) > win_probability(100, 110, 300, 300)
    assert win_probability(100, 95, 300, 300) > win_probability(100, 105, 300, 300)
    # favorite (mu_own > mu_opp): less variance -> higher win prob
    assert win_probability(100, 90, 200, 300) > win_probability(100, 90, 500, 300)
    # underdog (mu_own < mu_opp): less variance -> LOWER win prob
    assert win_probability(90, 100, 200, 300) < win_probability(90, 100, 500, 300)


def test_build_lineup_win_prob_falls_as_the_opponent_rises(db, marginal_world):
    marginal_world(_contest_specs(), retrieved=PULL)
    base = _build(db)
    g = base.own_projected_total
    probs = [_build(db, opponent_total=g + d).win_prob for d in (-30, -10, 10, 30)]
    assert all(a > b for a, b in zip(probs, probs[1:])), probs


# ============================ (d) OUT / bye sanity ============================


def test_a_ruled_out_starter_is_never_seated_and_lands_in_sanity_blocks(db, marginal_world):
    specs = _contest_specs()
    specs[2] = {**specs[2], "injury": "OUT"}          # 'Lead Runner' ruled OUT
    marginal_world(specs, retrieved=PULL)
    rec = _build(db)
    assert "Lead Runner" not in _seat_names(rec)
    assert any("Lead Runner" in b and "OUT" in b for b in rec.sanity_blocks)


def test_an_on_bye_player_is_never_seated_and_lands_in_sanity_blocks(db, marginal_world):
    specs = _contest_specs()
    specs[6] = {**specs[6], "bye": WEEK}              # 'First Wideout' on bye this week
    marginal_world(specs, retrieved=PULL)
    rec = _build(db)
    assert "First Wideout" not in _seat_names(rec)
    assert any("First Wideout" in b and "BYE" in b for b in rec.sanity_blocks)


def test_assert_no_illegal_starters_hard_raises_on_a_forced_bye_or_out():
    fill = LineupFill(total=0.0, slots=(("RB1", "x"),), bench=(), starters=frozenset({"x"}))
    with pytest.raises(StartabilityError):
        assert_no_illegal_starters(fill, byes={"x"}, statuses={}, week=WEEK, live_status=True)
    with pytest.raises(StartabilityError):
        assert_no_illegal_starters(fill, byes=set(), statuses={"x": "OUT"}, week=WEEK,
                                   live_status=True)
    # not live -> an OUT tag is a preseason roster label, not a game designation
    assert_no_illegal_starters(fill, byes=set(), statuses={"x": "OUT"}, week=WEEK,
                               live_status=False)


# ============================ (e) slot-lock ==================================


def _slot_lock_specs():
    return [
        {"name": "Pocket Passer", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": TEAM},
        {"name": "Sunday Runner A", "pos": "RB", "team": "BUF", "pts": 22.0, "bye": 7, "on_team": TEAM},
        {"name": "Sunday Runner B", "pos": "RB", "team": "DAL", "pts": 21.0, "bye": 8, "on_team": TEAM},
        {"name": "Thursday Runner", "pos": "RB", "team": "KC", "pts": 20.0, "bye": 9, "on_team": TEAM},
        {"name": "Wideout One", "pos": "WR", "team": "GB", "pts": 15.0, "bye": 10, "on_team": TEAM},
        {"name": "Wideout Two", "pos": "WR", "team": "SEA", "pts": 14.0, "bye": 11, "on_team": TEAM},
        {"name": "Tight One", "pos": "TE", "team": "JAX", "pts": 13.0, "bye": 12, "on_team": TEAM},
        {"name": "Home D/ST", "pos": "D/ST", "team": "MIA", "pts": 6.0, "bye": 8, "on_team": TEAM},
        {"name": "Steady Kicker", "pos": "K", "team": "NO", "pts": 8.0, "bye": 8, "on_team": TEAM},
    ]


def test_the_earliest_locking_flex_starter_is_never_in_flex(db, marginal_world):
    """(e) three seated RBs: the Thursday (earliest) one is relabeled OUT of FLEX
    into a dedicated RB slot, so the latest-locking RB holds the interchangeable
    FLEX and late optionality is preserved. 2026 opener is a Wednesday — keyed on
    the datetime, not a hardcoded weekday."""
    marginal_world(_slot_lock_specs(), retrieved=PULL)
    _sched(db, game_id="G_BUF", week=WEEK, home="BUF", away="X1", gameday="2026-09-20", gametime="13:00")
    _sched(db, game_id="G_DAL", week=WEEK, home="DAL", away="X2", gameday="2026-09-20", gametime="16:25")
    _sched(db, game_id="G_KC", week=WEEK, home="KC", away="X3", gameday="2026-09-17", gametime="20:15")
    db.commit()

    rec = _build(db)
    slot_of = {s.player: s.slot for s in rec.starters}
    assert "Thursday Runner" in slot_of                # it is seated
    assert slot_of["Thursday Runner"] != FLEX_LABEL     # but never in FLEX


def test_order_slots_by_lock_preserves_total_and_starters():
    """(e) the relabel is points-neutral: identical total AND identical seated set,
    only the slot labels move."""
    slots = (("RB1", "a"), ("RB2", "b"), (FLEX_LABEL, "c"), ("WR1", "w"))
    fill = LineupFill(total=42.0, slots=slots, bench=(), starters=frozenset({"a", "b", "c", "w"}))
    positions = {"a": "RB", "b": "RB", "c": "RB", "w": "WR"}
    locks = {
        "a": datetime(2026, 9, 17, 20, 15, tzinfo=ET),   # Thursday (earliest)
        "b": datetime(2026, 9, 20, 13, 0, tzinfo=ET),
        "c": datetime(2026, 9, 20, 16, 25, tzinfo=ET),   # latest
        "w": datetime(2026, 9, 20, 13, 0, tzinfo=ET),
    }
    relabeled, note = order_slots_by_lock(fill, positions, locks)
    assert note is None
    assert relabeled.total == fill.total
    assert relabeled.starters == fill.starters
    flex_key = next(k for lbl, k in relabeled.slots if lbl == FLEX_LABEL)
    assert flex_key == "c"                              # latest-locking RB holds FLEX
    assert "a" != flex_key                              # the Thursday RB is off FLEX


def test_order_slots_by_lock_degrades_to_a_note_when_no_locks_known():
    slots = (("RB1", "a"), ("RB2", "b"), (FLEX_LABEL, "c"))
    fill = LineupFill(total=10.0, slots=slots, bench=(), starters=frozenset({"a", "b", "c"}))
    positions = {"a": "RB", "b": "RB", "c": "RB"}
    relabeled, note = order_slots_by_lock(fill, positions, {})
    assert note is not None and "optionality" in note
    assert relabeled.total == fill.total and relabeled.starters == fill.starters


# ============================ (f) GTD contingency ============================


def _gtd_specs():
    specs = [
        {"name": "Pocket Passer", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": TEAM},
        {"name": "GTD Runner", "pos": "RB", "team": "CHI", "pts": 18.0, "bye": 9, "on_team": TEAM,
         "injury": "QUESTIONABLE"},                       # Sunday-early, game-time decision
        {"name": "Anchor Runner", "pos": "RB", "team": "BUF", "pts": 20.0, "bye": 7, "on_team": TEAM},
        {"name": "Late Backup", "pos": "RB", "team": "KC", "pts": 9.0, "bye": 8, "on_team": TEAM},
        {"name": "Wideout One", "pos": "WR", "team": "GB", "pts": 15.0, "bye": 10, "on_team": TEAM},
        {"name": "Wideout Two", "pos": "WR", "team": "SEA", "pts": 14.0, "bye": 11, "on_team": TEAM},
        {"name": "Wideout Three", "pos": "WR", "team": "TB", "pts": 12.0, "bye": 6, "on_team": TEAM},
        {"name": "Tight One", "pos": "TE", "team": "JAX", "pts": 13.0, "bye": 12, "on_team": TEAM},
        {"name": "Home D/ST", "pos": "D/ST", "team": "MIA", "pts": 6.0, "bye": 8, "on_team": TEAM},
        {"name": "Steady Kicker", "pos": "K", "team": "NO", "pts": 8.0, "bye": 8, "on_team": TEAM},
    ]
    return specs


def test_a_gtd_starter_gets_a_lock_ordered_safe_wait_contingency(db, marginal_world):
    """(f) a Questionable starter whose game locks EARLY plus a bench alternative
    whose game locks LATE (after the inactive report) yields a SAFE-WAIT contingency
    — start him, swap only if ruled out — not a pre-emptive point pick."""
    marginal_world(_gtd_specs(), retrieved=PULL)
    # GTD Runner (CHI) plays Sunday 1pm; Late Backup (KC) plays Monday night.
    _sched(db, game_id="G_CHI", week=WEEK, home="CHI", away="X1", gameday="2026-09-20", gametime="13:00")
    _sched(db, game_id="G_BUF", week=WEEK, home="BUF", away="X2", gameday="2026-09-20", gametime="13:00")
    _sched(db, game_id="G_KC", week=WEEK, home="KC", away="X3", gameday="2026-09-21", gametime="20:15")
    db.commit()

    now = datetime(2026, 9, 20, 9, 0, tzinfo=ET)         # Sunday morning, before kickoffs
    rec = _build(db, now=now)
    assert "GTD Runner" in _seat_names(rec)              # still started, NOT benched
    cont = next((c for c in rec.contingencies if c.player == "GTD Runner"), None)
    assert cont is not None
    assert cont.safe_wait is True
    assert cont.alternative == "Late Backup"
    assert cont.status_known_by is not None
    # the alternative's kickoff is strictly after the GTD man's status_known_by
    assert cont.alternative_kickoff > cont.status_known_by
    assert any("swap" in r.lower() for r in cont.reasons)
    # it is on the inactives watch (unresolved, not yet locked)
    assert any(w.player == "GTD Runner" for w in rec.watch_list)


def test_a_gtd_contingency_is_a_closed_window_once_the_game_has_locked(db, marginal_world):
    """CODE FIX 2: once the decision clock is PAST the GTD man's kickoff the slot is
    locked — the watch correctly drops him AND the contingency must no longer show a
    live actionable 'start X / swap to Y' plan; it is marked window-closed (historical)."""
    marginal_world(_gtd_specs(), retrieved=PULL)
    _sched(db, game_id="G_CHI", week=WEEK, home="CHI", away="X1", gameday="2026-09-20", gametime="13:00")
    _sched(db, game_id="G_BUF", week=WEEK, home="BUF", away="X2", gameday="2026-09-20", gametime="13:00")
    _sched(db, game_id="G_KC", week=WEEK, home="KC", away="X3", gameday="2026-09-21", gametime="20:15")
    db.commit()

    now = datetime(2026, 9, 20, 14, 30, tzinfo=ET)       # AFTER GTD Runner's 1pm kickoff
    rec = _build(db, now=now)
    # the watch drops him (his game has locked) ...
    assert not any(w.player == "GTD Runner" for w in rec.watch_list)
    # ... and the contingency is present but marked closed, not a live actionable swap.
    cont = next((c for c in rec.contingencies if c.player == "GTD Runner"), None)
    assert cont is not None
    assert cont.window_closed is True
    assert not (cont.safe_wait and not cont.window_closed)
    blob = " ".join(cont.reasons).lower()
    assert "closed" in blob and "no longer actionable" in blob
    assert "swap to" not in blob                          # not a live actionable instruction
    # the renderer tags it as locked, not "safe wait"
    text = format_lineup_recommendation(rec, reasons=True)
    assert "window closed" in text


@pytest.mark.parametrize("posture,delta,wording", [
    ("UNDERDOG", +30.0, "ceiling"),
    ("FAVORITE", -30.0, "floor"),
    ("NEUTRAL", 0.0, "coin flip"),
])
def test_a_gtd_gamble_branch_has_no_safe_wait_and_posture_conditional_wording(
    db, marginal_world, posture, delta, wording
):
    """TEST FIX 7: when every same-slot bench alternative locks at/before the GTD
    man's status is known there is NO safe wait — the contingency is a GAMBLE
    (safe_wait=False, alternative=None) whose reason wording is posture-conditional
    (ceiling / floor / coin-flip)."""
    marginal_world(_gtd_specs(), retrieved=PULL)
    # GTD Runner (CHI) locks LAST (Monday night); every other game is Sunday early,
    # so no bench alternative locks after his inactive-report deadline -> no safe wait.
    _sched(db, game_id="G_CHI", week=WEEK, home="CHI", away="X1", gameday="2026-09-21", gametime="20:15")
    _sched(db, game_id="G_BUF", week=WEEK, home="BUF", away="X2", gameday="2026-09-20", gametime="13:00")
    _sched(db, game_id="G_KC", week=WEEK, home="KC", away="X3", gameday="2026-09-20", gametime="13:00")
    db.commit()

    base = _build(db, opponent_total=None, week=WEEK)     # NEUTRAL greedy total
    g = base.own_projected_total
    now = datetime(2026, 9, 20, 9, 0, tzinfo=ET)          # before every kickoff
    rec = _build(db, opponent_total=g + delta, now=now)
    assert rec.posture == posture
    cont = next(c for c in rec.contingencies if c.player == "GTD Runner")
    assert cont.safe_wait is False
    assert cont.alternative is None
    assert cont.window_closed is False                    # his Monday game has not locked
    blob = " ".join(cont.reasons).lower()
    assert wording in blob


# ============================ variance model internals =======================


def _seat(key, pos, team, pts, *, available=True):
    """A priced _Seat with the model sigma (as _price_roster would produce)."""
    return lineup_support._Seat(
        key=key, player=key, position=pos, team=team, espn_id=None, gsis_id=None,
        points=pts, sigma=DEFAULT_VARIANCE.sigma(pos, pts), injury_status=None,
        lineup_slot=None, on_bye=False, has_proj=True, hard_out=False, available=available)


def test_qb_passcatcher_stack_adds_the_correlation_cross_term(db):
    """TEST FIX 5: a QB and a pass-catcher on his OWN NFL team add the
    2*rho*sigma_qb*sigma_pc cross term to the lineup variance (the strongest
    underdog lever). Moving that pass-catcher to a DIFFERENT team removes exactly
    that term; zeroing / mis-scaling / mis-positioning it must fail this test."""
    qb = _seat("qb", "QB", "KC", 20.0)
    wr_same = _seat("wr", "WR", "KC", 15.0)        # same NFL team as the QB -> stack
    wr_diff = _seat("wr", "WR", "SF", 15.0)        # different team -> no stack

    _, var_stack = lineup_support._lineup_stats(["qb", "wr"], {"qb": qb, "wr": wr_same},
                                                DEFAULT_VARIANCE)
    _, var_nostack = lineup_support._lineup_stats(["qb", "wr"], {"qb": qb, "wr": wr_diff},
                                                  DEFAULT_VARIANCE)
    rho = DEFAULT_VARIANCE.correlation_qb_passcatcher
    expected = 2.0 * rho * qb.sigma * wr_same.sigma
    assert var_stack - var_nostack == pytest.approx(expected)
    assert expected > 0.0                          # the term is a real, positive lever

    # and an UNDERDOG (trailing) is better off with the stack's higher variance
    mu = qb.points + wr_same.points
    wp_stack = win_probability(mu, mu + 15.0, var_stack, 300.0)
    wp_nostack = win_probability(mu, mu + 15.0, var_nostack, 300.0)
    assert wp_stack > wp_nostack


def test_mu_sacrifice_cap_binds_and_releases_at_its_threshold(db):
    """TEST FIX 6: a favorite's variance-reducing swap that is genuinely z-improving
    is REFUSED when it would sacrifice more than the cap, and taken when it would
    not — pinning both sides of _MU_SACRIFICE_CAP. Breaking a QB stack (the big
    variance lever) is the swap; the stacked WR out-projects the solo bench WR."""
    def seats_for(solo_pts):
        s = {
            "qb":  _seat("qb",  "QB",  "KC",  20.0),
            "rb1": _seat("rb1", "RB",  "ATL", 24.0),
            "rb2": _seat("rb2", "RB",  "BUF", 22.0),
            "rb3": _seat("rb3", "RB",  "CHI", 21.0),   # -> FLEX
            "wr1": _seat("wr1", "WR",  "DAL", 19.0),
            "stack": _seat("stack", "WR", "KC", 16.0), # stacked with the QB
            "solo": _seat("solo", "WR", "SF", solo_pts),   # bench alternative
            "te1": _seat("te1", "TE", "JAX", 15.0),
            "dst": _seat("dst", "DST", "MIA", 6.0),
            "k":   _seat("k",   "K",   "NO",  8.0),
        }
        return s

    from ziggurat.core.lineup_support import (
        _MU_SACRIFICE_CAP, _greedy_fill, _steepest_ascent)
    from ziggurat.core.valuation import DEFAULT_ROSTER

    # A large favorite so the (decoupled) stack-break variance drop is z-improving
    # even at a >2-pt sacrifice; the ONLY thing that changes the outcome is the cap.
    def seated_wr(solo_pts, cap):
        s = seats_for(solo_pts)
        greedy = _greedy_fill(s, DEFAULT_ROSTER)
        assert "stack" in greedy.starters and "solo" not in greedy.starters
        mu_g, _ = lineup_support._lineup_stats(greedy.starters, s, DEFAULT_VARIANCE)
        slotmap = _steepest_ascent(greedy, s, DEFAULT_ROSTER, mu_opp=mu_g - 100.0,
                                   var_opp=50.0, variance=DEFAULT_VARIANCE, mu_cap=cap)
        return set(slotmap.values())

    # cost 1.5 (<= cap 2.0): the swap is ALLOWED -> solo promoted, stack benched.
    assert "solo" in seated_wr(14.5, _MU_SACRIFICE_CAP)
    assert "stack" not in seated_wr(14.5, _MU_SACRIFICE_CAP)
    # cost 2.5 (> cap 2.0): the SAME z-improving swap is REFUSED -> stack STAYS.
    assert "stack" in seated_wr(13.5, _MU_SACRIFICE_CAP)
    assert "solo" not in seated_wr(13.5, _MU_SACRIFICE_CAP)
    # proof the cap is what blocked it: relax the cap and the 2.5 swap is taken.
    assert "solo" in seated_wr(13.5, 3.0)


# ============================ (g) opponent auto-compute ======================


def _opp_specs():
    """Own roster on TEAM plus an opponent roster on team 5 (weaker), so the
    auto-computed opponent total is well-defined and the margin is positive."""
    own = _contest_specs()
    opp = [
        {"name": "Opp QB", "pos": "QB", "team": "MIN", "pts": 12.0, "bye": 6, "on_team": 5},
        {"name": "Opp RB1", "pos": "RB", "team": "LV", "pts": 10.0, "bye": 7, "on_team": 5},
        {"name": "Opp RB2", "pos": "RB", "team": "LAC", "pts": 8.0, "bye": 8, "on_team": 5},
        {"name": "Opp WR1", "pos": "WR", "team": "CIN", "pts": 9.0, "bye": 9, "on_team": 5},
        {"name": "Opp WR2", "pos": "WR", "team": "CLE", "pts": 7.0, "bye": 10, "on_team": 5},
        {"name": "Opp TE", "pos": "TE", "team": "PIT", "pts": 6.0, "bye": 11, "on_team": 5},
        {"name": "Opp DST", "pos": "D/ST", "team": "BAL", "pts": 5.0, "bye": 12, "on_team": 5},
        {"name": "Opp K", "pos": "K", "team": "WAS", "pts": 7.0, "bye": 13, "on_team": 5},
    ]
    return own + opp


def test_opponent_total_auto_computes_from_the_opponent_roster(db, marginal_world):
    """(g) with opponent_total=None the module resolves the week's matchup, reads
    the opponent roster, and seats his all-healthy best lineup for the total."""
    marginal_world(_opp_specs(), retrieved=PULL)
    _matchup(db, week=WEEK, home_team_id=TEAM, away_team_id=5)
    db.commit()

    rec = _build(db, opponent_total=None)
    assert rec.opponent_total is not None
    assert rec.opponent_total > 0
    # own roster is stronger -> favorite, and the note discloses the all-healthy basis
    assert rec.posture == "FAVORITE"
    assert any("all-healthy" in n for n in rec.notes)
    # the number equals the opponent's own greedy best-lineup total
    opp = lineup_support._opponent_lineup(
        db, as_of=PULL, season=SEASON, week=WEEK, opp_team_id=5, source="sleeper_rotowire",
        byes=lineup_support.bye_map(db, as_of=PULL, season=SEASON, source="sleeper_rotowire"),
        variance=DEFAULT_VARIANCE, structure=lineup_support.DEFAULT_ROSTER, view="historical")
    assert rec.opponent_total == pytest.approx(opp[0])


def test_a_playoff_week_with_no_matchup_falls_back_to_neutral(db, marginal_world):
    """(g) fantasy playoff weeks 15-17 carry no matchup rows -> NEUTRAL with a
    reason, never a fabricated opponent."""
    marginal_world(_opp_specs(), retrieved=PULL)
    _matchup(db, week=WEEK, home_team_id=TEAM, away_team_id=5)   # week 3 only
    db.commit()
    rec = _build(db, week=15, opponent_total=None)
    assert rec.posture == "NEUTRAL"
    assert rec.opponent_total is None
    assert any("no opponent matchup" in n for n in rec.notes)


# ============================ (h) Rule 1: as_of + view =======================


def test_build_lineup_requires_as_of(db):
    with pytest.raises(TypeError):
        build_lineup(db, season=SEASON, own_team_id=TEAM, week=WEEK)


def test_resolve_opponent_requires_as_of(db):
    with pytest.raises(TypeError):
        resolve_opponent(db, season=SEASON, week=WEEK, own_team_id=TEAM)


def test_resolve_opponent_threads_the_view_and_is_leakage_safe(db, marginal_world):
    """(h) RULE 1: a matchup row retrieved AFTER the as-of (knowable before it) is
    hidden by the default historical view and shown by latest_truth — resolve_opponent
    threads ``view`` into the gated get_matchups accessor."""
    marginal_world(_contest_specs(), retrieved=PULL)
    _matchup(db, week=WEEK, home_team_id=TEAM, away_team_id=5, retrieved="2026-09-16")
    db.execute("UPDATE league_matchups SET knowable_as_of='2026-09-10' "
               "WHERE season=2026 AND week=3 AND home_team_id=10")
    db.commit()
    assert resolve_opponent(db, as_of=PULL, season=SEASON, week=WEEK, own_team_id=TEAM) is None
    assert resolve_opponent(db, as_of=PULL, season=SEASON, week=WEEK, own_team_id=TEAM,
                            view="latest_truth") == 5


def test_own_team_id_none_is_refused(db, marginal_world):
    marginal_world(_contest_specs(), retrieved=PULL)
    with pytest.raises(OwnTeamUnresolved):
        build_lineup(db, as_of=PULL, season=SEASON, own_team_id=None, week=WEEK)


def test_view_threading_hides_a_late_retrieved_roster_row_under_historical(db, marginal_world):
    """(h) a roster row retrieved AFTER the as-of but knowable before it (a later
    correction) is hidden by the historical view and shown by latest_truth —
    proving view threads into get_player_state AND weekly_lines."""
    marginal_world(_contest_specs(), retrieved=PULL)
    db.execute(
        "INSERT INTO players (gsis_id, espn_id, name, retrieved_as_of, knowable_as_of) "
        "VALUES ('00-777777', '7777', 'Late Wideout', '2026-09-16', '2026-09-10')"
    )
    db.execute(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, opponent, rushing_yards, retrieved_as_of, knowable_as_of) "
        "VALUES ('sleeper_rotowire', 'S777', '00-777777', 2026, 3, 'regular', 'WR', 'ARI', "
        "'OPP', 300.0, '2026-09-16', '2026-09-10')"
    )
    db.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, "
        "position, pro_team, on_team_id, roster_status, lineup_slot, injury_status, "
        "percent_owned, percent_started, percent_change, scoring_period, "
        "retrieved_as_of, knowable_as_of) VALUES "
        "(2026, '7777', '00-777777', 'Late Wideout', 'WR', 'ARI', 10, 'ONTEAM', 'BE', "
        "'ACTIVE', 50.0, 0.0, 0.0, 0, '2026-09-16', '2026-09-10')"
    )
    db.commit()

    hist = build_lineup(db, as_of=PULL, season=SEASON, own_team_id=TEAM, week=WEEK,
                        view="historical")
    truth = build_lineup(db, as_of=PULL, season=SEASON, own_team_id=TEAM, week=WEEK,
                         view="latest_truth")
    hist_all = _seat_names(hist) | {b.player for b in hist.bench}
    truth_all = _seat_names(truth) | {b.player for b in truth.bench}
    assert "Late Wideout" not in hist_all          # correction hidden (retrieved 09-16 > 09-15)
    assert "Late Wideout" in truth_all             # latest_truth surfaces it (30 pts -> seated)


# ============================ Rule 2: pricing / Rule 6: reasons ==============


def test_starter_proj_points_come_from_weekly_lines(db, marginal_world):
    """RULE 2: a starter's proj_points is the weekly_lines (scoring.py) number, not
    a re-computed one — the shared spine, so it can never disagree with marginal."""
    marginal_world(_contest_specs(), retrieved=PULL)
    lines = lineup_support.weekly_lines(db, as_of=PULL, season=SEASON, weeks=[WEEK])
    rec = _build(db)
    passer = next(s for s in rec.starters if s.player == "Pocket Passer")
    line = next(l for l in lines.values() if l.player == "Pocket Passer")
    assert passer.proj_points == pytest.approx(line.points[WEEK])


def test_lineup_support_hardcodes_no_scoring_constant():
    """RULE 2 (grep-proof): the module never scores a stat line itself — pricing is
    delegated to weekly_lines, and no scoring weight/bracket/scorer is referenced."""
    src = Path(lineup_support.__file__).read_text()
    assert "weekly_lines" in src
    for banned in ("points_per", "_brackets", "score_offense(", "score_kicker(",
                   "score_dst(", "scoring.score("):
        assert banned not in src, banned


def test_variance_priors_quote_their_labelled_hypothesis(db, marginal_world):
    """RULE 6: every sigma prior is disclosed as a measured 2021-2025 hypothesis in
    the starter reasons (a novice must be able to see the assumption)."""
    marginal_world(_contest_specs(), retrieved=PULL)
    rec = _build(db)
    blob = " ".join(r for s in rec.starters for r in s.reasons)
    assert "hypothesis" in blob and "measured 2021-2025" in blob


def test_reasons_contain_no_unexplained_jargon(db, marginal_world):
    banned = ("vor", "vona", "argmax", "bernoulli", "monte carlo", "vbd", "z-score")
    marginal_world(_contest_specs(), retrieved=PULL)
    rec = _build(db, opponent_total=100.0)
    blob = " ".join(r for s in rec.starters for r in s.reasons).lower()
    for word in banned:
        assert not re.search(rf"\b{re.escape(word)}\b", blob), word


# ============================ (i) formatting =================================


def test_format_lineup_recommendation_smoke(db, marginal_world):
    """(i) the renderer prints posture, win prob, starters and their reasons."""
    marginal_world(_contest_specs(), retrieved=PULL)
    rec = _build(db, opponent_total=120.0)
    text = format_lineup_recommendation(rec, reasons=True)
    assert "lineup — season 2026" in text
    assert rec.posture in text
    assert "win prob" in text
    assert "Pocket Passer" in text


def test_format_renders_the_sanity_block_section(db, marginal_world):
    """(i) a removed OUT/bye player is surfaced in a REMOVED section (Rule 6)."""
    specs = _contest_specs()
    specs[2] = {**specs[2], "injury": "OUT"}
    marginal_world(specs, retrieved=PULL)
    rec = _build(db)
    text = format_lineup_recommendation(rec, reasons=False)
    assert "REMOVED" in text
    assert "Lead Runner" in text


# ============================ week resolution ================================


def test_week_none_resolves_the_current_week_not_a_finished_one(db, marginal_world):
    """week=None resolves via marginal.resolve_weeks — the first NOT-yet-finished
    week, never a played one (waiver Tue/Wed safety)."""
    marginal_world(_contest_specs(), retrieved=PULL)
    _sched(db, game_id="G_W1", week=1, home="TEN", away="ATL", gameday="2026-09-10")
    _sched(db, game_id="G_W2", week=2, home="TEN", away="ATL", gameday="2026-09-14")
    _sched(db, game_id="G_W3", week=3, home="TEN", away="ATL", gameday="2026-09-21")
    db.commit()
    rec = build_lineup(db, as_of=PULL, season=SEASON, own_team_id=TEAM, week=None,
                       opponent_total=120.0)
    assert rec.week == 3


def test_week_resolution_raises_when_it_cannot_be_derived(db, marginal_world):
    """week=None with no scoring period and no schedule RAISES rather than guessing
    a full season (Rule 6)."""
    marginal_world(_contest_specs(), retrieved=PULL)
    with pytest.raises(WeekResolutionError):
        build_lineup(db, as_of="2026-06-01", season=SEASON, own_team_id=TEAM, week=None)
