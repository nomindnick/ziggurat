"""Marginal (roster-context) valuation tests — item 3.2.

Offline throughout: a synthetic projection universe and a synthetic league state
(``marginal_world``), no network. The numbers are chosen so the football claim
each test makes is checkable by hand.

The tests that matter most are the ones pinning MEASURED defects rather than
intended behaviour: a second defense as the top add on every roster, negative
scenario weights, an exact-tie cohort ordered by nothing, a full-season window
guessed when the current week is unknown, and an unprojected player floor-ranked
as the top drop. Each of those was observed against real data during the recon
and each has a test here.
"""

import re

import pytest

from ziggurat.core import marginal
from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.league.state import get_player_state

SEASON = 2026
PULL = "2026-09-15"
WEEKS = range(3, 18)

# D/ST is the ONLY position with real week-to-week projection variation
# (measured median CV 12.0%, versus ~1% for every skill position), and that
# single fact is what made a SECOND DEFENSE the best add on 15 of 16 drop
# candidates. Reproduced here: two defenses whose good weeks alternate.
_ODD_WEEKS_BIG = {w: 20.0 for w in range(1, 18) if w % 2 == 1}
_EVEN_WEEKS_BIG = {w: 20.0 for w in range(1, 18) if w % 2 == 0}

# A realistic 16-active + 1-IR roster (team 10) plus a free-agent pool. Every
# player is invented (Rule 5). ``pts`` is HOUSE points per playing week.
ROSTER_SPECS = [
    {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
    {"name": "Backup Passer", "pos": "QB", "team": "TEN", "pts": 8.0, "bye": 6, "on_team": 10},
    {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
    {"name": "Second Runner", "pos": "RB", "team": "ATL", "pts": 5.0, "bye": 11, "on_team": 10},
    {"name": "Third Runner", "pos": "RB", "team": "BUF", "pts": 12.0, "bye": 7, "on_team": 10},
    {"name": "Depth Runner", "pos": "RB", "team": "CHI", "pts": 3.0, "bye": 9, "on_team": 10},
    {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
    {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": 10},
    {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 11.0, "bye": 10, "on_team": 10},
    {"name": "Fourth Catcher", "pos": "WR", "team": "HOU", "pts": 4.0, "bye": 12, "on_team": 10},
    {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": 10},
    {"name": "Tight Two", "pos": "TE", "team": "JAX", "pts": 3.0, "bye": 5, "on_team": 10},
    {"name": "Kick Er", "pos": "K", "team": "KC", "pts": 8.0, "bye": 14, "on_team": 10},
    {"name": "Miami D/ST", "pos": "D/ST", "team": "MIA", "pts": 2.0, "bye": 5, "on_team": 10,
     "weeks": _ODD_WEEKS_BIG},
    {"name": "Hurt Guy", "pos": "WR", "team": "MIN", "pts": 14.0, "bye": 6, "on_team": 10,
     "slot": "IR", "injury": "INJURY_RESERVE"},
    {"name": "Ghost Player", "pos": "WR", "team": "NO", "pts": 0.0, "bye": 7, "on_team": 10},
]
POOL_SPECS = [
    {"name": "Free Passer", "pos": "QB", "team": "NE", "pts": 12.0, "bye": 9},
    {"name": "Free Runner", "pos": "RB", "team": "NYG", "pts": 14.0, "bye": 7},
    {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 16.0, "bye": 11},
    {"name": "Free Tight", "pos": "TE", "team": "LV", "pts": 9.0, "bye": 8},
    {"name": "Free Kicker", "pos": "K", "team": "LAC", "pts": 10.0, "bye": 6},
    {"name": "Seattle D/ST", "pos": "D/ST", "team": "SEA", "pts": 2.0, "bye": 8,
     "weeks": _EVEN_WEEKS_BIG},
    {"name": "Frisco D/ST", "pos": "D/ST", "team": "SF", "pts": 12.0, "bye": 9},
    {"name": "Waiver Wideout", "pos": "WR", "team": "PIT", "pts": 13.0, "bye": 10,
     "status": "WAIVERS"},
]
ALL_SPECS = ROSTER_SPECS + POOL_SPECS


def _board(db, roster, pool, **kwargs):
    kwargs.setdefault("weeks", WEEKS)
    kwargs.setdefault("pool_limit", None)
    kwargs.setdefault("swap_limit", None)
    return marginal.build_board(
        db, as_of=PULL, season=SEASON, roster=roster, pool=pool, **kwargs
    )


def _row(board, name):
    for r in board.rows:
        if r.player == name:
            return r
    raise AssertionError(f"{name} not on the board")


@pytest.fixture()
def world(db, marginal_world):
    roster, pool = marginal_world(ALL_SPECS, retrieved=PULL)
    return roster, pool


# --------------------------------------------------------- 1. Rule 1 (leakage)


def test_accessors_require_as_of():
    """Every new accessor is keyword-only ``as_of`` with no default (Rule 1)."""
    for fn in (marginal.build_board, marginal.build_marginal, marginal.build_swaps):
        with pytest.raises(TypeError):
            fn(None, season=SEASON, roster=[])
    for fn in (marginal.bye_map, marginal.resolve_weeks, marginal.live_status_from):
        with pytest.raises(TypeError):
            fn(None, season=SEASON)


def test_nothing_is_visible_before_the_snapshot_was_knowable(db, world):
    roster, pool = world
    before = marginal.build_board(
        db, as_of="2026-09-14", season=SEASON, weeks=WEEKS, pool_limit=None,
        roster=get_player_state(db, as_of="2026-09-14", season=SEASON, on_team_id=10),
        pool=get_player_state(db, as_of="2026-09-14", season=SEASON, free_agents_only=True),
    )
    assert before.rows == ()
    assert any("NO PROJECTIONS" in n for n in before.notes)

    after = marginal.build_board(
        db, as_of=PULL, season=SEASON, weeks=WEEKS, pool_limit=None,
        roster=get_player_state(db, as_of=PULL, season=SEASON, on_team_id=10),
        pool=get_player_state(db, as_of=PULL, season=SEASON, free_agents_only=True),
    )
    assert len(after.rows) == len(ROSTER_SPECS) - 1        # the IR occupant is out
    assert not any("NO PROJECTIONS" in n for n in after.notes)


def test_a_later_projection_vintage_is_invisible_at_the_earlier_as_of(db, world):
    roster, pool = world
    db.execute(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, opponent, rushing_yards, retrieved_as_of, "
        "knowable_as_of) VALUES ('sleeper_rotowire', 'S9', '00-100009', 2026, 3, "
        "'regular', 'WR', 'HOU', 'OPP', 9000.0, '2026-09-20', '2026-09-20')"
    )
    db.commit()
    early = _board(db, roster, pool)
    late = marginal.build_board(
        db, as_of="2026-09-20", season=SEASON, roster=roster, pool=pool,
        weeks=WEEKS, pool_limit=None,
    )
    assert _row(early, "Fourth Catcher").marginal_points < 100.0
    assert _row(late, "Fourth Catcher").marginal_points > 500.0


def test_bye_map_is_as_of_gated(db, world):
    assert marginal.bye_map(db, as_of="2026-09-14", season=SEASON).byes == {}
    assert marginal.bye_map(db, as_of=PULL, season=SEASON).byes["ATL"] == 11


# ------------------------------------------------- 2. the objective's guards


def test_a_second_defense_is_never_the_best_add(db, world):
    """THE measured defect. Uncapped, the whole-pool argmax free agent came back
    ``LA D/ST`` on 15 of 16 drop candidates against the live board, because D/ST
    is the ONLY position with real week-to-week projection variation (CV 12.0% vs
    ~1% for skill), so under a per-week seater the only remaining source of gain
    is carrying two defenses and starting the better one each week. That is only
    true if you never stream — assumption A1."""
    roster, pool = world
    board = _board(db, roster, pool)
    skill_rows = [r for r in board.ranked if r.position not in marginal.STREAMED_POSITIONS]
    assert skill_rows
    for r in skill_rows:
        assert "D/ST" not in (r.best_replacement or ""), r

    # ... and the guard is load-bearing: remove the caps and the defect returns.
    uncapped = _board(db, roster, pool, position_caps={})
    assert any("D/ST" in (r.best_replacement or "")
               for r in uncapped.ranked
               if r.position not in marginal.STREAMED_POSITIONS)


def test_the_only_kicker_and_defense_are_not_the_least_droppable_players(db, world):
    """Leave-one-out (``V(R) - V(R\\{p})``, no replacement) is NOT the objective.
    It charges you for an empty mandatory lineup slot that waivers refill for
    free, which scored a defense at 123.4 and a kicker at ~124 — making them the
    two least-droppable players on the roster and refusing to ever stream a D/ST.
    """
    roster, pool = world
    board = _board(db, roster, pool)
    least_droppable = [r.player for r in board.ranked[-3:]]
    assert "Kick Er" not in least_droppable
    assert "Miami D/ST" not in least_droppable
    assert _row(board, "Kick Er").marginal_points < _row(board, "Lead Runner").marginal_points
    assert _row(board, "Miami D/ST").marginal_points < _row(board, "Lead Runner").marginal_points


def test_streamed_positions_are_priced_on_this_week_only(db, world):
    """A 1-week window and a 15-week window must produce IDENTICAL D/ST and K
    rows — otherwise 3.2 prices a kicker over the rest of the season while 3.5
    tells you to replace him on Thursday, and nothing errors."""
    roster, pool = world
    long_board = _board(db, roster, pool, weeks=WEEKS)
    short_board = _board(db, roster, pool, weeks=range(3, 4))
    for name in ("Kick Er", "Miami D/ST"):
        long_row, short_row = _row(long_board, name), _row(short_board, name)
        assert long_row.marginal_points == pytest.approx(short_row.marginal_points)
        assert long_row.horizon_weeks == short_row.horizon_weeks == 1
    assert _row(long_board, "Lead Runner").horizon_weeks == len(list(WEEKS))


def test_a_better_kicker_is_worth_adding(db, world):
    """The ported draft-time constant would price this at exactly 0.0
    (``_BENCH_VALUE_FRACTION`` reads K/DST 0.0). The pool's kicker outprojects the
    incumbent, so the swap must be positive."""
    roster, pool = world
    board = _board(db, roster, pool)
    kicker_swaps = [s for s in board.swaps
                    if s.drop == "Kick Er" and s.add == "Free Kicker"]
    assert kicker_swaps and kicker_swaps[0].gain > 0.0
    assert kicker_swaps[0].horizon_weeks == 1


# -------------------------------------------------- 3. scenario weights (math)


def test_scenario_weights_are_a_proper_distribution():
    """The naive form ``w0 = 1 - sum(p)`` gives -0.290 on a real 17-man roster —
    a negative probability — and it gets worse in the playoff bucket."""
    model = marginal.DEFAULT_AVAILABILITY
    roster = ["QB"] * 2 + ["RB"] * 5 + ["WR"] * 6 + ["TE"] * 2 + ["K"] + ["DST"]
    for week in (3, 9, 16):
        probs = [(f"{pos}{i}", model.p_out(pos, week)) for i, pos in enumerate(roster)]
        naive = 1.0 - sum(p for _k, p in probs)
        w0, singles = marginal.scenario_weights(probs)
        assert all(w >= 0.0 for _k, w in singles)
        assert w0 >= 0.0
        assert w0 + sum(w for _k, w in singles) == pytest.approx(1.0)
        if week == 16:
            assert naive < 0.0                 # the case the naive form breaks on

    # A CERTAINTY is a gate, not a scenario: leaving p=1.0 in the w0 product drove
    # w0 to 0, zeroed every single, and the norm<=0 guard then asserted that NOBODY
    # is ever out — silently discarding every other player's injury scenario.
    w0, singles = marginal.scenario_weights([("a", 1.0), ("b", 0.1), ("c", 0.1)])
    assert [k for k, _w in singles] == ["b", "c"]
    assert w0 + sum(w for _k, w in singles) == pytest.approx(1.0)
    assert w0 < 1.0
    assert marginal.scenario_weights([("a", 1.0)]) == (1.0, ())


def test_playoff_weeks_carry_a_higher_miss_rate_than_september():
    model = marginal.DEFAULT_AVAILABILITY
    assert model.p_out("RB", 3) < model.p_out("RB", 9) < model.p_out("RB", 16)
    assert model.p_out("DST", 16) == 0.0       # a team defense always plays


def test_one_out_truncation_stays_inside_its_stated_tolerance(db, world):
    """The SEARCH estimator is an APPROXIMATION (measured +1.9% high against a
    full Bernoulli Monte Carlo). Full MC is the oracle, never the estimator: a real
    swap scan under MC is ~11 minutes."""
    roster, pool = world
    board = _board(db, roster, pool)
    keys = [r.player_key for r in board.rows]
    enumerated = board.model.value(keys)
    sampled = board.model.value_monte_carlo(keys, samples=400, seed=7)
    assert enumerated == pytest.approx(sampled, rel=0.03)
    assert enumerated >= sampled * 0.99         # biased HIGH, as measured


def test_the_REPORTED_marginal_is_bounded_in_POINTS_not_in_percent_of_V(db, world):
    """The tolerance that matters is on the DIFFERENCE, not on the level.

    A +1.9% bias on V(K) is tolerable; the same truncation is 2-3x on the bench
    rows the drop board exists to rank, because a bench body's whole contribution
    lives in the >=2-out scenarios one-out throws away. Measured on a live
    post-draft roster over weeks 8-17: a deep RB priced -1.28 at depth 1 against
    -3.25 under exact enumeration. So the bound is stated in POINTS, against an
    unbiased oracle, on every row — and it is the REPORTED number that is bounded.
    """
    roster, pool = world
    board = _board(db, roster, pool)
    model, keys = board.model, [r.player_key for r in board.ranked]
    oracle_base = model.value_monte_carlo(keys, samples=3000, seed=11)
    for row in board.ranked:
        if row.position in marginal.STREAMED_POSITIONS or row.best_replacement is None:
            continue
        after = [k for k in keys if k != row.player_key]
        add = [k for k, e in model.entries.items()
               if e.player == row.best_replacement and not e.on_roster]
        oracle = oracle_base - model.value_monte_carlo(
            after + add, samples=3000, seed=11)
        assert row.marginal_points == pytest.approx(oracle, abs=1.5), row.player

    # ... and the SEARCH estimator alone would not have cleared that bar, which is
    # why the reporting pass exists at all.
    shallow = _board(db, roster, pool, report_depth=1)

    def worst_error(bd):
        err = 0.0
        for row in bd.ranked:
            if row.position in marginal.STREAMED_POSITIONS or row.best_replacement is None:
                continue
            after = [k for k in keys if k != row.player_key]
            add = [k for k, e in model.entries.items()
                   if e.player == row.best_replacement and not e.on_roster]
            oracle = oracle_base - model.value_monte_carlo(
                after + add, samples=3000, seed=11)
            err = max(err, abs(row.marginal_points - oracle))
        return err

    assert worst_error(shallow) > worst_error(board)


# ---------------------------------------------------------- 4. ties & unvalued


def test_exact_ties_are_broken_by_the_stated_ladder_and_said_out_loud(db, marginal_world):
    """Any player who never reaches the lineup in any scenario contributes exactly
    0, so a whole cohort collapses onto ONE number decided by sort order. The
    ladder is contingent value -> rest-of-season value -> percent owned."""
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "Run Two", "pos": "RB", "team": "BUF", "pts": 17.0, "bye": 7, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 16.0, "bye": 8, "on_team": 10},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": 10},
        {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 14.0, "bye": 10, "on_team": 10},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 13.0, "bye": 13, "on_team": 10},
        # two identical dead-weight WRs: same points, same bye, differing only in
        # how widely they are owned.
        {"name": "Deadweight A", "pos": "WR", "team": "LV", "pts": 1.0, "bye": 12,
         "on_team": 10, "owned": 4.0},
        {"name": "Deadweight B", "pos": "WR", "team": "LV", "pts": 1.0, "bye": 12,
         "on_team": 10, "owned": 40.0},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 2.0, "bye": 11},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool)
    a, b = _row(board, "Deadweight A"), _row(board, "Deadweight B")
    assert a.marginal_points == pytest.approx(b.marginal_points, abs=marginal.TIE_BAND)
    order = [r.player for r in board.ranked]
    assert order.index("Deadweight A") < order.index("Deadweight B")   # less owned drops first
    assert a.tiebreak_rung is not None
    assert any("tied with" in r for r in a.reasons)
    # the rung NAMED must be one a neighbour actually differs on
    assert "owned" in a.tiebreak_rung

    # determinism: the same inputs order the same way every time
    for _ in range(3):
        again = _board(db, roster, pool)
        assert [r.player for r in again.ranked] == order

    # ...and the order must come from the LADDER, not from Python's stable sort
    # falling back on the order the specs happened to be listed in. Reversing the
    # two tied rows in the input must not reverse them in the output.
    db.execute("DELETE FROM projections")
    db.execute("DELETE FROM players")
    db.execute("DELETE FROM league_player_state")
    db.commit()
    flipped_specs = list(specs)
    i, j = 7, 8
    flipped_specs[i], flipped_specs[j] = flipped_specs[j], flipped_specs[i]
    roster2, pool2 = marginal_world(flipped_specs, retrieved=PULL)
    flipped = _board(db, roster2, pool2)
    order2 = [r.player for r in flipped.ranked]
    assert order2.index("Deadweight A") < order2.index("Deadweight B")


def test_a_tie_broken_by_nothing_at_all_says_so_rather_than_naming_a_rung(
    db, marginal_world
):
    """Three identical never-starting bench bodies tie on the objective AND on
    every rung of the ladder. The order is then alphabetical and means nothing —
    which the reason must say, because a reason the operator cannot reproduce is
    worse than no reason."""
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "Run Two", "pos": "RB", "team": "BUF", "pts": 17.0, "bye": 7, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 16.0, "bye": 8, "on_team": 10},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": 10},
        {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 14.0, "bye": 10, "on_team": 10},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 13.0, "bye": 13, "on_team": 10},
        {"name": "Dead Aaa", "pos": "WR", "team": "LV", "pts": 1.0, "bye": 12,
         "on_team": 10, "owned": 5.0},
        {"name": "Dead Bbb", "pos": "WR", "team": "LV", "pts": 1.0, "bye": 12,
         "on_team": 10, "owned": 5.0},
        {"name": "Dead Ccc", "pos": "WR", "team": "LV", "pts": 1.0, "bye": 12,
         "on_team": 10, "owned": 5.0},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 2.0, "bye": 11},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool)
    dead = [_row(board, n) for n in ("Dead Aaa", "Dead Bbb", "Dead Ccc")]
    assert {round(r.marginal_points, 9) for r in dead} == {round(dead[0].marginal_points, 9)}
    for r in dead:
        assert "alphabetical" in r.tiebreak_rung, r.tiebreak_rung
        assert any("means nothing" in x for x in r.reasons)


def test_unpriceable_players_are_reported_separately_not_ranked_as_top_drops(db, world):
    """An unprojected player contributes 0 in every scenario, so the objective
    ranks him at the tied floor — i.e. as your top drop — for a reason that has
    nothing to do with football. We say we cannot price him instead."""
    roster, pool = world
    board = _board(db, roster, pool)
    assert [r.player for r in board.unpriceable] == ["Ghost Player"]
    assert "Ghost Player" not in [r.player for r in board.ranked]
    assert board.ranked[0].player != "Ghost Player"
    text = marginal.format_marginal(board)
    assert "CANNOT VALUE" in text and "Ghost Player" in text


# ---------------------------------- 5. roster context changes the answer (done-when)


_NO_COUPLING = marginal.HandcuffModel(
    uplift={}, correlation={}, pairs_n={}, ci={}, workload_spread={},
    label="test: no coupling", source="test",
)

_CONTEXT_FILLER = [
    {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
    {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
    {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": 10},
    {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": 10},
    {"name": "Kick Er", "pos": "K", "team": "KC", "pts": 8.0, "bye": 14, "on_team": 10},
    {"name": "Miami D/ST", "pos": "D/ST", "team": "MIA", "pts": 7.0, "bye": 5, "on_team": 10},
]
_CONTEXT_STARTER = {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0,
                    "bye": 11, "on_team": 10}
_CONTEXT_HANDCUFF = {"name": "Second Runner", "pos": "RB", "team": "ATL", "pts": 5.0,
                     "bye": 11, "on_team": 10}
_CONTEXT_OTHER_RBS = [
    {"name": "Other Runner 1", "pos": "RB", "team": "BUF", "pts": 16.0, "bye": 7, "on_team": 10},
    {"name": "Other Runner 2", "pos": "RB", "team": "CHI", "pts": 15.0, "bye": 9, "on_team": 10},
    {"name": "Other Runner 3", "pos": "RB", "team": "CLE", "pts": 14.0, "bye": 10, "on_team": 10},
    {"name": "Other Runner 4", "pos": "RB", "team": "CIN", "pts": 13.0, "bye": 12, "on_team": 10},
    {"name": "Spare Catcher", "pos": "WR", "team": "SEA", "pts": 14.0, "bye": 13, "on_team": 10},
]
_CONTEXT_POOL = [{"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 6.0, "bye": 12}]


def test_the_same_handcuff_is_valuable_on_a_thin_roster_and_worthless_on_a_deep_one(
    db, marginal_world
):
    """THE DONE-WHEN, and the SPEC failure mode in one test.

    Hold the candidate fixed and change ONLY the roster around him. On median
    points alone this handcuff is 5.0/wk and gets dumped instantly ("median-only
    drops of lottery tickets"). The model must say he is worth holding on the thin
    roster AND that he is genuinely worthless on the deep one — the measured
    result was that coupling moved him by exactly 0.00 behind four better backs,
    because with the starter out he still never reaches the lineup.
    """
    all_specs = [_CONTEXT_STARTER, _CONTEXT_HANDCUFF, *_CONTEXT_OTHER_RBS,
                 *_CONTEXT_FILLER, *_CONTEXT_POOL]
    roster, pool = marginal_world(all_specs, retrieved=PULL)
    deep = [r for r in roster]
    thin = [r for r in roster if not r["player"].startswith(("Other Runner", "Spare Catcher"))]

    def coupling_delta(roster_rows):
        on = _row(_board(db, roster_rows, pool), "Second Runner").marginal_points
        off = _row(_board(db, roster_rows, pool, handcuffs=_NO_COUPLING),
                   "Second Runner").marginal_points
        return on - off

    thin_delta = coupling_delta(thin)
    deep_delta = coupling_delta(deep)
    assert thin_delta > 1.0, "on a thin roster the handcuff must be worth something"
    # The recon measured this at EXACTLY 0.00, but that was an artifact of the
    # one-out truncation: behind three better backs he reaches the lineup only in
    # the weeks where the starter AND two more are out at once, and the reporting
    # estimator now enumerates those. The football claim is unchanged and is the
    # one asserted here — the handcuff is worth an order of magnitude less on the
    # deep roster, and what is left is rounding on a 15-week board.
    assert 0.0 <= deep_delta < 0.5, (
        "behind three better backs he barely reaches the lineup even when the "
        "starter is out — the handcuff is all but worthless there"
    )
    assert deep_delta < thin_delta / 10.0

    # ... and the REASONS change with the context, which is what the operator
    # actually reads: the same player at the same projection starts most weeks on
    # the thin roster and never reaches the lineup on the deep one.
    thin_row = _row(_board(db, thin, pool), "Second Runner")
    deep_row = _row(_board(db, deep, pool), "Second Runner")
    assert thin_row.weeks_started > 0
    assert deep_row.weeks_started == 0
    assert any("never reaches your starting lineup" in r for r in deep_row.reasons)
    assert not any("never reaches your starting lineup" in r for r in thin_row.reasons)
    assert thin_row.contingent_component > deep_row.contingent_component


def test_the_handcuff_reason_names_the_starter_and_its_evidence(db, marginal_world):
    roster, pool = marginal_world(
        [_CONTEXT_STARTER, _CONTEXT_HANDCUFF, *_CONTEXT_FILLER, *_CONTEXT_POOL],
        retrieved=PULL,
    )
    row = _row(_board(db, roster, pool), "Second Runner")
    blob = " ".join(row.reasons)
    assert "insurance on Lead Runner" in blob
    assert "hypothesis" in blob and "n=103 pairs" in blob
    assert "misses TIME" in blob                    # an annuity, not a one-week lottery
    assert "committee" in blob                      # the 4x workload spread is stated


def test_a_backup_qb_is_a_worse_add_than_a_wr_who_covers_byes(db, marginal_world):
    """A second QB behind a healthy elite QB1 never reaches the lineup; a WR who
    starts on two of your bye weeks does."""
    specs = [
        {"name": "Elite Passer", "pos": "QB", "team": "TEN", "pts": 25.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 8, "on_team": 10},
        {"name": "Run Two", "pos": "RB", "team": "BUF", "pts": 16.0, "bye": 8, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 9, "on_team": 10},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": 10},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": 10},
        {"name": "Kick Er", "pos": "K", "team": "KC", "pts": 8.0, "bye": 14, "on_team": 10},
        {"name": "Miami D/ST", "pos": "D/ST", "team": "MIA", "pts": 7.0, "bye": 5, "on_team": 10},
        {"name": "Deadweight", "pos": "WR", "team": "LV", "pts": 1.0, "bye": 12, "on_team": 10},
        # the two adds under comparison
        {"name": "Spare Passer", "pos": "QB", "team": "NE", "pts": 14.0, "bye": 7},
        {"name": "Bye Coverer", "pos": "WR", "team": "NYJ", "pts": 12.0, "bye": 11},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool)
    best = {}
    for s in board.swaps:
        best[s.add] = max(best.get(s.add, 0.0), s.gain)
    assert best.get("Bye Coverer", 0.0) > best.get("Spare Passer", 0.0)


# -------------------------------------------------------------- 6. bye weeks


def test_bye_map_is_derived_by_complement_not_by_a_null_opponent_filter(db, marginal_world):
    """The naive ``opponent IS NULL`` filter reads as if it works. It does not:
    the placeholder identities carry a team and a NULL opponent in EVERY week, so
    every team ends up marked on bye every week (measured on the live board:
    18 of 18 distinct weeks, for all 32 teams)."""
    specs = ALL_SPECS + [
        # a placeholder identity: a team, no opponent, no stats, every week
        {"name": "Camp Body", "pos": "WR", "team": "ATL", "pts": 0.0, "bye": None},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    db.execute("UPDATE projections SET opponent = NULL WHERE source_player_id = ?",
               (f"S{len(specs) - 1}",))
    db.commit()

    byes = marginal.bye_map(db, as_of=PULL, season=SEASON)
    assert byes.byes["ATL"] == 11              # unchanged by the placeholder rows
    assert byes.byes["MIA"] == 5
    assert byes.source == "projections"
    # the property that makes the naive query wrong is really present
    rows = db.execute(
        "SELECT COUNT(DISTINCT week) FROM projections WHERE team='ATL' AND opponent IS NULL"
    ).fetchone()[0]
    assert rows > 1


def test_a_narrowed_week_span_reports_unknown_rather_than_no_bye(db, world):
    """Deriving over weeks 10-17 returns 16 teams on the live board, not 32 — an
    ``assert len(byes) == 32`` takes the module down on any narrowed pull. A team
    whose bye is outside the span must read UNKNOWN, never 'no bye'."""
    db.execute("DELETE FROM projections WHERE week < 10")
    db.commit()
    byes = marginal.bye_map(db, as_of=PULL, season=SEASON)
    assert byes.span[0] == 10
    assert "ATL" in byes.byes and byes.byes["ATL"] == 11       # bye inside the span
    assert "MIA" in byes.unknown and "MIA" not in byes.byes    # week-5 bye, outside
    assert byes.bye_of("MIA") is None


def test_a_span_too_sparse_to_tell_reports_unknown_rather_than_inventing_a_bye(
    db, world
):
    """0 missing weeks means "his bye is outside this span"; MORE THAN ONE missing
    means the span is too sparse to tell. Reporting the first missing week as a
    confident bye is the mistake — and with a partial source that branch is the
    one that fires."""
    db.execute("DELETE FROM projections WHERE team='ATL' AND week IN (3, 4, 5)")
    db.commit()
    byes = marginal.bye_map(db, as_of=PULL, season=SEASON)
    assert "ATL" in byes.unknown
    assert "ATL" not in byes.byes
    assert byes.byes["MIA"] == 5              # unaffected neighbours still resolve


def test_both_bye_row_shapes_resolve_to_an_unavailable_zero_point_week(db, world):
    """A skill player's bye row is PRESENT with NULL stats; a D/ST's bye row is
    ABSENT entirely. A detector keyed on either shape alone mislabels the other."""
    roster, pool = world
    board = _board(db, roster, pool, weeks=range(3, 18))
    model = board.model
    lead = _row(board, "Lead Runner").player_key
    miami = _row(board, "Miami D/ST").player_key
    assert model.available(lead, 10) and not model.available(lead, 11)
    assert model.points(lead, 11) == 0.0
    assert model.available(miami, 6) and not model.available(miami, 5)
    assert model.points(miami, 5) == 0.0


def test_a_missing_dst_week_is_never_scored_as_a_shutout(db, world):
    """Fabricating a ``points_allowed=0`` row for an absent D/ST week would award a
    phantom +5/+5 under scoring.py's present-vs-absent bracket rule. The bye week
    must be worth exactly 0."""
    roster, pool = world
    board = _board(db, roster, pool)
    miami = _row(board, "Miami D/ST").player_key
    assert board.model.points(miami, 5) == 0.0        # the ABSENT bye row
    assert board.model.points(miami, 6) > 0.0         # a week he really plays


def test_handcuff_coupling_contributes_nothing_in_a_shared_bye_week(db, marginal_world):
    """ATL's bye is week 11 and both the starter and his handcuff are ATL — the
    backup cannot cover a week he is also on bye for. The contingent term is
    summed only over weeks the backup is himself available."""
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "Second Runner", "pos": "RB", "team": "ATL", "pts": 5.0, "bye": 11, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": 10},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 6.0, "bye": 12},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    shared_bye_only = _board(db, roster, pool, weeks=range(11, 12))
    row = _row(shared_bye_only, "Second Runner")
    assert row.contingent_component == pytest.approx(0.0)
    assert row.marginal_points == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------ 7. availability gates


def test_coupling_is_never_applied_to_a_defense_or_a_kicker(db, world):
    """Rule-2 guard: offense scoring is linear so adding raw points to a
    projection is sound, but the D/ST brackets are NOT linear. A future author
    must not be able to widen the handcuff gate onto them."""
    assert marginal.DEFAULT_HANDCUFFS.uplift_for("DST") == 0.0
    assert marginal.DEFAULT_HANDCUFFS.uplift_for("K") == 0.0
    assert marginal.DEFAULT_HANDCUFFS.uplift_for("WR") == 0.0     # measured -0.14
    greedy = marginal.HandcuffModel(
        uplift={**marginal.DEFAULT_HANDCUFFS.uplift, "DST": 50.0, "K": 50.0, "WR": 50.0},
        correlation=marginal.DEFAULT_HANDCUFFS.correlation,
        pairs_n=marginal.DEFAULT_HANDCUFFS.pairs_n, ci=marginal.DEFAULT_HANDCUFFS.ci,
        workload_spread=marginal.DEFAULT_HANDCUFFS.workload_spread,
        label="hostile hypothesis", source="test",
    )
    for pos in ("DST", "K", "WR"):
        assert greedy.uplift_for(pos) == 0.0

    # ...and asserted THROUGH A BOARD, not just on the one-line method: a future
    # author could bypass ``uplift_for`` at any of its call sites and the
    # method-level assertion above would not notice.
    roster, pool = world
    plain = {r.player: r.marginal_points for r in _board(db, roster, pool).ranked}
    hostile = {r.player: r.marginal_points
               for r in _board(db, roster, pool, handcuffs=greedy).ranked}
    for name, value in plain.items():
        assert hostile[name] == pytest.approx(value, abs=1e-9), name


def test_a_player_ruled_out_cannot_be_a_start_this_week_add(db, marginal_world):
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Deadweight", "pos": "WR", "team": "LV", "pts": 1.0, "bye": 12, "on_team": 10},
        {"name": "Injured Star", "pos": "WR", "team": "NYJ", "pts": 22.0, "bye": 9,
         "injury": "OUT"},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    # schedules make week 1 knowable, so the ESPN status is a GAME designation
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES "
        "('g1', 2026, 1, 'REG', '2026-09-10', 'TEN', 'ATL', '2026-08-01', '2026-08-01')"
    )
    db.commit()
    board = _board(db, roster, pool)
    injured = [s for s in board.swaps if s.add == "Injured Star"]
    assert injured, "an OUT player can still be a worthwhile FUTURE add"
    for s in injured:
        assert s.add_startable_this_week is False
        assert any("cannot start this week" in r for r in s.reasons)
    assert board.model.available(
        [k for k, e in board.model.entries.items() if e.player == "Injured Star"][0],
        board.weeks[0],
    ) is False


def test_preseason_injury_tags_are_ignored_until_week_one(db, marginal_world):
    """Today ESPN reads OUT on a 71%-owned running back and on a starting WR —
    preseason it is a ROSTER TAG, not a game designation. Honoring it before Week
    1 zeroes out healthy studs."""
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Tagged Star", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11,
         "on_team": 10, "injury": "OUT"},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 6.0, "bye": 12},
    ]
    roster, pool = marginal_world(specs, retrieved="2026-07-24")
    preseason = marginal.build_board(
        db, as_of="2026-07-24", season=SEASON, roster=roster, pool=pool,
        weeks=range(1, 18), pool_limit=None,
    )
    key = _row(preseason, "Tagged Star").player_key
    assert preseason.model.available(key, 1) is True
    assert any("roster labels this early" in n for n in preseason.notes)

    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES "
        "('g1', 2026, 1, 'REG', '2026-09-10', 'TEN', 'ATL', '2026-08-01', '2026-08-01')"
    )
    db.commit()
    inseason = marginal.build_board(
        db, as_of="2026-09-15", season=SEASON, roster=roster, pool=pool,
        weeks=range(1, 18), pool_limit=None,
    )
    assert inseason.model.available(key, 1) is False


def test_ir_players_leave_the_lineup_and_the_active_slot_count(db, world):
    roster, pool = world
    board = _board(db, roster, pool)
    assert "Hurt Guy" not in [r.player for r in board.rows]
    assert any("IR-slotted" in n for n in board.notes)
    assert DEFAULT_ROSTER.active_slots == 16 and DEFAULT_ROSTER.ir_slots == 1


# --------------------------------------------------------- 8. week resolution


def test_weeks_none_raises_rather_than_pricing_a_whole_season(db, world):
    """``scoring_period`` is 0 on every live row and ``schedules`` is not knowable
    until Aug 1. Falling through to weeks 1-17 in November would price ten
    already-played weeks into every decision and look completely plausible."""
    with pytest.raises(marginal.WeekResolutionError) as exc:
        marginal.resolve_weeks(db, as_of=PULL, season=SEASON)
    assert "--from-week" in str(exc.value)
    with pytest.raises(marginal.WeekResolutionError):
        marginal.build_board(db, as_of=PULL, season=SEASON, roster=[])


def test_weeks_resolve_from_the_scoring_period_when_espn_reports_one(db, marginal_world):
    marginal_world(ROSTER_SPECS, retrieved=PULL, scoring_period=7)
    assert marginal.resolve_weeks(db, as_of=PULL, season=SEASON) == tuple(range(7, 18))


def test_weeks_resolve_from_schedules_when_the_scoring_period_is_still_zero(db, world):
    db.executemany(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES (?, 2026, ?, 'REG', ?, "
        "'TEN', 'ATL', '2026-08-01', '2026-08-01')",
        [(f"g{w}", w, f"2026-09-{2 + w * 7:02d}") for w in range(1, 3)],
    )
    db.commit()
    assert marginal.resolve_weeks(db, as_of="2026-09-18", season=SEASON)[0] == 2


def test_the_live_status_boundary_is_derived_when_schedules_are_readable(db, world):
    assert marginal.live_status_from(db, as_of=PULL, season=SEASON) == "2026-09-10"
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES "
        "('g1', 2026, 1, 'REG', '2026-09-04', 'TEN', 'ATL', '2026-08-01', '2026-08-01')"
    )
    db.commit()
    assert marginal.live_status_from(db, as_of=PULL, season=SEASON) == "2026-09-04"


# ----------------------------------------------------------- 9. Rule 6 sanity


def test_every_ranked_row_ships_reasons(db, world):
    roster, pool = world
    board = _board(db, roster, pool)
    for r in board.rows:
        assert r.reasons and all(isinstance(x, str) and x.strip() for x in r.reasons)
    for s in board.swaps:
        assert s.reasons and all(isinstance(x, str) and x.strip() for x in s.reasons)


def test_reasons_contain_no_jargon_the_operator_cannot_check(db, world):
    """The operator is a football novice and cannot smell an absurd output. A
    reason he cannot read is not a reason."""
    banned = ("vor", "vona", "sigma", "marginal_component", "argmax", "bernoulli",
              "monte carlo", "vbd")
    roster, pool = world
    board = _board(db, roster, pool)
    blob = " ".join(r for row in board.rows for r in row.reasons).lower()
    blob += " " + " ".join(r for s in board.swaps for r in s.reasons).lower()
    for word in banned:
        assert not re.search(rf"\b{re.escape(word)}\b", blob), word


def test_the_decomposition_sums_to_the_number_it_explains(db, world):
    """A single '+3.1 pts' is unfalsifiable to a novice; 'starts 0.0 + byes 2.4 +
    if-hurt 0.7' explains why — but only if it actually adds up."""
    roster, pool = world
    board = _board(db, roster, pool)
    for r in board.ranked:
        parts = r.lineup_component + r.bye_component + r.contingent_component
        assert parts == pytest.approx(r.marginal_points, abs=1e-9), r.player


def test_the_decomposition_is_a_MECHANISM_split_not_a_probability_slice(db, world):
    """The sum test above is a tautology — the row is BUILT from those three
    numbers, so it can never fail. These pin what the three MEAN, which is what
    the operator actually reads.

    The shipped split used to be probability mass: ``lineup`` was the
    nobody-hurt scenario TIMES its weight and ``contingent`` was everything else,
    so on a 16-man roster ~57% of every player's value landed in the column the
    reason string calls "somebody on your roster is hurt" — including for players
    with no linked backup at all, and including a D/ST, which the model states can
    never be unavailable.
    """
    roster, pool = world
    never_hurt = marginal.AvailabilityModel(
        base_rate={}, bucket_multiplier={"early": 1.0, "mid": 1.0, "playoff": 1.0},
        questionable_rate={}, hard_out_statuses=frozenset(),
        absence_curve=(1.0,), long_term_statuses=frozenset(),
        cohort="test", label="hypothesis: nobody ever misses a game", source="test",
    )
    healthy_world = _board(db, roster, pool, availability=never_hurt)
    for r in healthy_world.ranked:
        assert r.contingent_component == pytest.approx(0.0, abs=1e-9), r.player

    # A defense's replacement is another defense and the DST slot is independent of
    # every other slot, so nobody else's injury can move that row.
    board = _board(db, roster, pool)
    dst = _row(board, "Miami D/ST")
    assert dst.contingent_component == pytest.approx(0.0, abs=1e-9)

    # ``lineup_component`` is the REAL all-healthy starting-lineup delta, not that
    # delta scaled by the probability nobody is hurt.
    from ziggurat.core.lineup import fill_lineup
    model = board.model
    keys = [r.player_key for r in board.ranked]
    for row in board.ranked:
        if row.position in marginal.STREAMED_POSITIONS or row.best_replacement is None:
            continue
        add = [k for k, e in model.entries.items()
               if e.player == row.best_replacement and not e.on_roster]
        after = [k for k in keys if k != row.player_key] + add
        repl = model.entries[add[0]] if add else None
        want = 0.0
        for w in model.weeks:
            if model.entries[row.player_key].bye == w or (repl and repl.bye == w):
                continue
            a = [k for k in keys if model.available(k, w)]
            b = [k for k in after if model.available(k, w)]
            want += (fill_lineup(a, model.positions, {k: model.points(k, w) for k in a},
                                 roster=model.rs).total
                     - fill_lineup(b, model.positions, {k: model.points(k, w) for k in b},
                                   roster=model.rs).total)
        assert row.lineup_component == pytest.approx(want, abs=1e-6), row.player


def test_every_quoted_prior_carries_its_hypothesis_label(db, world):
    """Availability rates and handcuff uplifts were calibrated on nflverse
    history, not on 2026, and item 5.2's promotion ladder is still a placeholder —
    so nothing but this test stops a labeled hypothesis hardening into a rule."""
    roster, pool = world
    board = _board(db, roster, pool)
    for row in board.ranked:
        for reason in row.reasons:
            if "%/wk chance" in reason or "house pts more per week" in reason:
                assert "hypothesis" in reason, reason
    assert "hypothesis" in marginal.DEFAULT_AVAILABILITY.label
    assert "hypothesis" in marginal.DEFAULT_HANDCUFFS.label


def test_negative_value_is_kept_and_becomes_an_add_recommendation(db, world):
    """Marginal value going negative IS the actionable add signal; clamping it
    would throw the recommendation away."""
    roster, pool = world
    board = _board(db, roster, pool)
    negatives = [r for r in board.ranked if r.marginal_points < 0]
    assert negatives, "a 16-man roster with a live pool should have droppable players"
    for r in negatives:
        assert any(s.drop == r.player and s.gain > 0 for s in board.swaps)
        assert any("you would GAIN" in x for x in r.reasons)


def test_the_static_roster_caveat_and_staleness_banner_are_printed(db, world):
    roster, pool = world
    board = _board(db, roster, pool)
    text = marginal.format_marginal(board, reasons=True)
    assert "NO OTHER MOVES ALL SEASON" in text
    # The DIRECTION is the whole point of the sentence and it shipped backwards:
    # assumption A1 makes optionality slots too EXPENSIVE, never too cheap.
    assert "OVER-VALUES" in text
    assert "understates any slot" not in text
    assert "UPPER BOUND" in text
    assert "projections: pulled 2026-09-15" in text
    assert "league state: pulled 2026-09-15" in text


def test_stale_projections_raise_a_visible_warning(db, marginal_world):
    """The live failure mode: a July projection pricing a November decision is
    Rule-1 INVISIBLE — that snapshot really is the newest thing at or before the
    as-of date, so nothing errors and the number is simply wrong."""
    roster, pool = marginal_world(ROSTER_SPECS + POOL_SPECS, retrieved="2026-07-24")
    board = marginal.build_board(
        db, as_of="2026-11-10", season=SEASON, roster=roster, pool=pool,
        weeks=range(10, 18), pool_limit=None,
    )
    text = marginal.format_marginal(board)
    assert "WARNING" in text and "109 days old" in text


# ----------------------------------------------------------- 10. swap matrix


def test_the_swap_matrix_and_the_drop_board_agree(db, world):
    """They are the same scan on purpose: splitting the computation is how the add
    board and the drop board start disagreeing."""
    roster, pool = world
    board = _board(db, roster, pool)
    for row in board.ranked:
        if row.marginal_points >= 0 or row.best_replacement is None:
            continue
        match = [s for s in board.swaps
                 if s.drop == row.player and s.add == row.best_replacement]
        assert match, row.player
        assert match[0].gain == pytest.approx(-row.marginal_points, abs=1e-6)


def test_waiver_and_free_agent_adds_are_distinguishable(db, world):
    """A waiver claim is queued and processed overnight; a free agent is a click.
    Item 3.4 needs the difference and retrofitting it later is expensive."""
    roster, pool = world
    board = _board(db, roster, pool)
    statuses = {s.add_status for s in board.swaps}
    assert "WAIVERS" in statuses and "FREEAGENT" in statuses


# ------------------------------------------------------------ 11. performance


def test_a_single_roster_scan_stays_inside_its_budget(db, world):
    """The stated budget is 30 s for one roster against the real board. This
    fixture is ~100x smaller, so a 30 s assert here would pass through a 100x
    regression; the bar is scaled to the fixture instead. The real number is
    measured by hand and recorded in the plan (10.8 s over weeks 8-17, 16.9 s over
    1-17, on the live post-draft database)."""
    import time

    roster, pool = world
    start = time.monotonic()
    _board(db, roster, pool)
    assert time.monotonic() - start < 5.0


def test_a_permanently_empty_starting_slot_is_called_out_once_in_words(db, marginal_world):
    """With no D/ST on the roster the best add for EVERY drop candidate is a D/ST
    — correct, but fifteen rows all saying 'add Steelers D/ST' need explaining
    once, in words, or the board reads as if it were broken."""
    specs = [s for s in ALL_SPECS if s["pos"] != "D/ST"]
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool)
    assert any("EMPTY DST SLOT IN EVERY REMAINING WEEK" in n for n in board.notes)

    with_defense = [s for s in ALL_SPECS]
    assert any(s["pos"] == "D/ST" and s.get("on_team") for s in with_defense)


def test_a_bye_week_hole_is_not_reported_as_a_structural_one(db, world):
    """A slot empty in SOME weeks is a bye — which the bye component already
    prices. Only a slot empty in every week is a hole."""
    roster, pool = world
    board = _board(db, roster, pool)
    assert not any("EVERY REMAINING WEEK" in n for n in board.notes)


# ------------------------------------------------- 12. the audit's findings
#
# Every test below pins a defect an adversarial audit found in the built module,
# not an intended behaviour. Each one failed before the fix.


def test_a_partly_projected_player_is_unpriceable_not_near_worthless(db, marginal_world):
    """THE CRITICAL ONE. The feed's bye row and its "no forecast" row are
    byte-identical (team set, opponent NULL, every stat NULL), so a point sum
    cannot tell "worth nothing" from "we do not know".

    Measured on the live 2026 feed: A.J. Brown, 99.3% owned, carries a real week-1
    line and sixteen empty weeks. His sum was 14.1 — not 0.0 — so he cleared the
    unpriceable gate, priced at -24.4 over 17 weeks and topped the drop board with
    "never reaches your starting lineup ... drop him and add Jordan Love and you
    would GAIN 24.4", with nothing anywhere disclosing the missing weeks. The old
    gate was also a knife edge: narrow the window past his one good week and he was
    correctly flagged, so the behaviour flipped on the window.
    """
    specs = [s for s in ALL_SPECS if s["name"] != "Fourth Catcher"] + [
        {"name": "Half Known", "pos": "WR", "team": "SEA", "pts": 22.0, "bye": 12,
         "on_team": 10, "owned": 99.3, "forecast": {3}},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    for window in (range(3, 18), range(6, 18)):
        board = _board(db, roster, pool, weeks=window)
        assert "Half Known" in [r.player for r in board.unpriceable], window
        assert "Half Known" not in [r.player for r in board.ranked], window
        row = _row(board, "Half Known")
        assert row.unvalued
        text = marginal.format_marginal(board)
        assert "Half Known" in text and "CANNOT VALUE" in text
    # ...and the count is specific, not "no projection at all"
    board = _board(db, roster, pool, weeks=range(3, 18))
    assert _row(board, "Half Known").weeks_projected == 1
    assert any("only 1 of" in x for x in _row(board, "Half Known").reasons)


def test_a_partly_projected_free_agent_is_never_recommended_as_an_add(db, marginal_world):
    specs = ALL_SPECS + [
        {"name": "Half Free", "pos": "WR", "team": "SEA", "pts": 40.0, "bye": 12,
         "owned": 55.0, "forecast": {3}},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool)
    assert "Half Free" not in {s.add for s in board.swaps}
    assert "Half Free" not in {r.best_replacement for r in board.ranked}


def test_free_agents_that_cannot_be_priced_are_counted_out_loud(db, marginal_world):
    """Rule 6 applies to the ADD side too: the drop board names every roster player
    it cannot price, while the pool used to drop them silently — so "scanned 168 of
    359" read as the whole pool when the pool was 866 and 507 of them, including
    33%-owned names, had never been considered."""
    specs = ALL_SPECS + [
        {"name": "Dark Catcher", "pos": "WR", "team": "SEA", "pts": 0.0, "bye": 8,
         "owned": 33.0, "forecast": set()},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool)
    note = [n for n in board.notes if "no usable projection" in n]
    assert note, board.notes
    assert "Dark Catcher 33%" in note[0]
    assert "NOT" in note[0]
    assert "Dark Catcher" not in {s.add for s in board.swaps}


def test_an_injury_reserve_player_costs_more_than_one_week_and_says_so(db, marginal_world):
    """ESPN's strongest "he is done for a stretch" signal used to cost exactly ONE
    week: every later week fell back to the ~9%/wk base rate, so a player on
    injured reserve was modelled ~91% likely to play in week N+1, N+2, N+3 ... and
    no reason mentioned his designation at all."""
    base = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "Run Two", "pos": "RB", "team": "BUF", "pts": 16.0, "bye": 7, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": 10},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": 10},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 6.0, "bye": 12},
    ]
    values = {}
    for status in ("ACTIVE", "OUT", "INJURY_RESERVE"):
        db.execute("DELETE FROM projections")
        db.execute("DELETE FROM players")
        db.execute("DELETE FROM league_player_state")
        db.execute("DELETE FROM schedules")
        db.execute(
            "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
            "away_team, home_team, retrieved_as_of, knowable_as_of) VALUES "
            "('g1', 2026, 1, 'REG', '2026-09-10', 'TEN', 'ATL', '2026-08-01', '2026-08-01')"
        )
        db.commit()
        specs = list(base) + [
            {"name": "Hurt Runner", "pos": "RB", "team": "CHI", "pts": 19.0, "bye": 9,
             "on_team": 10, "injury": status},
        ]
        roster, pool = marginal_world(specs, retrieved=PULL)
        board = _board(db, roster, pool)
        row = _row(board, "Hurt Runner")
        values[status] = row.marginal_points
        if status != "ACTIVE":
            assert any(f"ESPN lists him {status}" in x for x in row.reasons), status
            assert any("ABSENCE EPISODE" in x for x in row.reasons), status
        else:
            assert not any("ESPN lists him" in x for x in row.reasons)
    # one missed week out of fifteen is ~7%; the measured return curve is ~40%
    assert values["OUT"] < values["ACTIVE"] * 0.85
    assert values["INJURY_RESERVE"] < values["ACTIVE"] * 0.85


def test_a_long_term_designation_says_the_return_curve_does_not_cover_it(db, marginal_world):
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Reserved Man", "pos": "RB", "team": "CHI", "pts": 19.0, "bye": 9,
         "on_team": 10, "injury": "INJURY_RESERVE"},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 6.0, "bye": 12},
    ]
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES "
        "('g1', 2026, 1, 'REG', '2026-09-10', 'TEN', 'ATL', '2026-08-01', '2026-08-01')"
    )
    db.commit()
    roster, pool = marginal_world(specs, retrieved=PULL)
    row = _row(_board(db, roster, pool), "Reserved Man")
    assert any("STRONGER signal" in x and "NOT fitted" in x for x in row.reasons)


def test_the_availability_prior_quotes_no_borrowed_sample_size(db, world):
    """Every non-streamed row printed "n=36" / "n=101" / "n=103" / "n=116" as the
    evidence behind the miss rate. Those four integers are the HANDCUFF event
    study's PAIR counts, lifted verbatim from a different measurement — the probe-3
    availability table has no sample size at all. A wrong n is worse than none,
    because it looks checkable and is not."""
    assert not hasattr(marginal.DEFAULT_AVAILABILITY, "n_by_position")
    roster, pool = world
    board = _board(db, roster, pool)
    for row in board.ranked:
        for reason in row.reasons:
            if "%/wk chance he sits" in reason:
                assert "n=" not in reason, reason
                assert "cohort:" in reason
                assert "hypothesis" in reason
    # the handcuff study keeps ITS n, because that one is real
    assert marginal.DEFAULT_HANDCUFFS.pairs_n["RB"] == 103


def test_the_availability_prior_quotes_the_whole_window_not_its_first_week(db, world):
    """The rate climbs through the season: on a weeks-3-17 board the model prices
    weeks 15-17 at 1.45x what the first week's bucket says. Quoting only the first
    week hands the operator the most optimistic number in the window."""
    roster, pool = world
    board = _board(db, roster, pool)
    rb = _row(board, "Third Runner")
    line = [x for x in rb.reasons if "%/wk chance he sits" in x][0]
    assert "6-13%/wk" in line, line
    assert "weeks 15-17" in line
    single = marginal.DEFAULT_AVAILABILITY.describe("RB", [9])
    assert single.split(" — ")[0] == "assumed 9%/wk chance he sits"


def test_the_week_window_never_prices_a_week_that_has_already_finished(db, world):
    """Resolution step 3 used to pick "the last week whose FIRST game kicked off",
    which on Tuesday and Wednesday — CLAUDE.md's waiver days — returns the week
    that ended the night before. That prices a played week into every board and,
    worse, hands D/ST and K (valued on a current-week horizon) last week's
    matchup."""
    db.executemany(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES (?, 2026, ?, 'REG', ?, "
        "'TEN', 'ATL', '2026-08-01', '2026-08-01')",
        [("w1a", 1, "2026-09-09"), ("w1b", 1, "2026-09-14"),
         ("w2a", 2, "2026-09-17"), ("w2b", 2, "2026-09-21"),
         ("w3a", 3, "2026-09-24"), ("w3b", 3, "2026-09-28")],
    )
    db.commit()
    resolve = marginal.resolve_weeks
    assert resolve(db, as_of="2026-09-09", season=SEASON)[0] == 1   # in week 1
    assert resolve(db, as_of="2026-09-14", season=SEASON)[0] == 1   # MNF of week 1
    assert resolve(db, as_of="2026-09-15", season=SEASON)[0] == 2   # Tuesday: waivers
    assert resolve(db, as_of="2026-09-16", season=SEASON)[0] == 2   # Wednesday
    assert resolve(db, as_of="2026-09-17", season=SEASON)[0] == 2
    assert resolve(db, as_of="2026-09-22", season=SEASON)[0] == 3


def test_last_week_applies_when_the_window_is_derived_too(db, world):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES "
        "('g1', 2026, 1, 'REG', '2026-09-20', 'TEN', 'ATL', '2026-08-01', '2026-08-01')"
    )
    db.commit()
    roster, pool = world
    board = marginal.build_board(
        db, as_of=PULL, season=SEASON, roster=roster, pool=pool, last_week=14,
        pool_limit=None,
    )
    assert board.weeks[-1] == 14


def test_one_frozen_player_among_fresh_ones_still_raises_the_stale_warning(
    db, marginal_world
):
    """The banner measured the gap off the NEWEST pull, so a single refreshed row
    silenced it for the whole board. The ingester UPSERTS — it never replaces the
    partition — so a player who falls out of the feed keeps his last-known line
    forever and ``select_as_of`` keeps serving it."""
    specs = [dict(s) for s in ALL_SPECS]
    for s in specs:
        s["retrieved"] = "2026-11-09"
    specs[6]["retrieved"] = "2026-07-24"            # First Catcher froze in July
    roster, pool = marginal_world(specs, retrieved="2026-11-09")
    board = marginal.build_board(
        db, as_of="2026-11-10", season=SEASON, roster=roster, pool=pool,
        weeks=range(10, 18), pool_limit=None,
    )
    text = marginal.format_marginal(board)
    assert "WARNING" in text and "oldest pull 2026-07-24" in text
    assert any("STALE" in x and "2026-07-24" in x
               for x in _row(board, "First Catcher").reasons)


def test_an_unpriceable_roster_player_still_appears_as_a_drop_in_the_swap_matrix(
    db, world
):
    """The obviously-correct drop side on a real roster is the body the board
    itself says it cannot price — and he was structurally unreachable, so item 3.4
    would have planned a claim that drops a genuine contributor instead."""
    roster, pool = world
    board = _board(db, roster, pool)
    ghost = [s for s in board.swaps if s.drop == "Ghost Player"]
    assert ghost, "the unpriceable roster player must be reachable as a drop"
    assert all(s.drop_unpriceable for s in ghost)
    assert any("UPPER BOUND" in r for r in ghost[0].reasons)
    assert all(not s.drop_unpriceable for s in board.swaps if s.drop != "Ghost Player")


def test_an_unpriceable_roster_player_does_not_silence_the_structural_hole_note(
    db, marginal_world
):
    """``fill_lineup`` seats a 0-point player into any otherwise-empty slot, so one
    unprojected body plugged the hole for the purposes of the check and deleted the
    note — the note being the whole Rule-6 mitigation for the hole."""
    base = [s for s in ALL_SPECS if s["pos"] != "D/ST"]
    with_ghost = base + [
        {"name": "Dark Defense", "pos": "D/ST", "team": "SEA", "pts": 0.0, "bye": 8,
         "on_team": 10, "forecast": set()},
    ]
    roster, pool = marginal_world(with_ghost, retrieved=PULL)
    board = _board(db, roster, pool)
    assert "Dark Defense" in [r.player for r in board.unpriceable]
    assert any("EMPTY DST SLOT IN EVERY REMAINING WEEK" in n for n in board.notes)


def test_the_handcuff_disclaimer_fires_when_the_starter_is_a_free_agent(
    db, marginal_world
):
    """The disclaimer was gated on whether the starter appeared anywhere in the
    scanned ENTRIES, which include the free-agent pool — so in the one situation
    where the operator is most likely to be holding a handcuff (another manager
    just dropped the injured starter) the full insurance case printed with no
    caveat, while the model gave the row no contingent credit at all."""
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Second Runner", "pos": "RB", "team": "ATL", "pts": 5.0, "bye": 11,
         "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": 10},
        # the linked STARTER is a free agent, not on the roster being valued
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 6.0, "bye": 12},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    row = _row(_board(db, roster, pool), "Second Runner")
    assert any("insurance on Lead Runner" in x for x in row.reasons)
    assert any("NOT on your roster" in x for x in row.reasons)


def test_a_player_on_bye_says_so_instead_of_blaming_competition(db, marginal_world):
    """"never reaches your starting lineup — you have 0 better DSTs ahead of him"
    is both self-contradictory and false: the real cause is his bye, and for a
    streamed slot the horizon is that single week."""
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Miami D/ST", "pos": "D/ST", "team": "MIA", "pts": 9.0, "bye": 5, "on_team": 10},
        {"name": "Seattle D/ST", "pos": "D/ST", "team": "SEA", "pts": 8.0, "bye": 8},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 6.0, "bye": 12},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool, weeks=range(5, 18))   # week 5 IS the MIA bye
    row = _row(board, "Miami D/ST")
    assert row.horizon_weeks == 1
    assert any("bye in week 5" in x for x in row.reasons)
    assert not any("better DST" in x for x in row.reasons)


def test_the_replacement_s_own_bye_is_named_when_it_drives_the_bye_component(
    db, marginal_world
):
    """A third of a top row's value was labelled "bye weeks" with nothing naming
    whose bye it was — the dropped player's own bye fell outside the window and the
    whole term was the REPLACEMENT's."""
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": 10},
        {"name": "Bye Free Catcher", "pos": "WR", "team": "NYJ", "pts": 9.0, "bye": 12},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    row = _row(_board(db, roster, pool), "First Catcher")
    assert row.best_replacement == "Bye Free Catcher"
    assert any("your best replacement's" in x and "week-12 bye" in x for x in row.reasons)


def test_streamed_rows_print_in_their_own_block_not_sorted_against_season_rows(
    db, world
):
    """A K/D-ST row is a ONE-WEEK number and every other row is a whole-season one.
    Sorting them into a single column under "lowest is most droppable" asks the
    operator to compare -1.6 over one week against -0.5 over fourteen."""
    roster, pool = world
    board = _board(db, roster, pool)
    text = marginal.format_marginal(board)
    assert "streamed slots" in text
    assert text.index("drop board") < text.index("streamed slots")
    head, tail = text.split("streamed slots", 1)
    assert "Miami D/ST" in tail and "Miami D/ST" not in head
    assert "Kick Er" in tail and "Kick Er" not in head
    assert "per wk" in text
    row = _row(board, "Miami D/ST")
    assert row.per_week == pytest.approx(row.marginal_points / row.horizon_weeks)


def test_pruning_keeps_the_candidate_that_wins_only_on_bye_timing(db, marginal_world):
    """``pool_limit`` is a cost control the design never sanctioned, and its whole
    safety argument is the bye-week carve-out: within a position a higher
    projection dominates a lower one whenever they share a bye, so the ONLY way a
    low-projected free agent can win is on bye TIMING. Throwing the entire pool
    away, or deleting that carve-out, used to leave the suite green."""
    fillers = [
        {"name": f"Filler {i:02d}", "pos": "WR", "team": "PIT", "pts": 9.0 + i * 0.1,
         "bye": 9}
        for i in range(40)
    ]
    specs = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 9, "on_team": 10},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 16.0, "bye": 9, "on_team": 10},
        {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 15.0, "bye": 9, "on_team": 10},
        {"name": "Deadweight", "pos": "WR", "team": "LV", "pts": 1.0, "bye": 12, "on_team": 10},
        # the low-projected name whose ONLY edge is that his bye is elsewhere
        {"name": "Bye Cover", "pos": "WR", "team": "NYJ", "pts": 8.0, "bye": 13},
        *fillers,
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    limited = _board(db, roster, pool, pool_limit=5)
    whole = _board(db, roster, pool, pool_limit=None)

    # the carve-out survives the cut ...
    entries = {e.player: e for e in whole.model.entries.values() if not e.on_roster}
    kept = marginal._prune_pool(list(entries.values()), 5)
    assert "Bye Cover" in {e.player for e in kept}, "pruned away the bye-timing winner"
    assert len(kept) < len(entries)
    assert marginal._prune_pool(list(entries.values()), None) == list(entries.values())

    # ... and a pruned board reaches the SAME conclusion as the whole-pool board
    for row in whole.ranked:
        assert _row(limited, row.player).best_replacement == row.best_replacement, row.player
        assert _row(limited, row.player).marginal_points == pytest.approx(
            row.marginal_points, abs=1e-9), row.player
    assert any("free-agent pool scanned" in n for n in limited.notes)


def test_the_swap_matrix_is_ordered_and_truncated_from_the_top(db, world):
    """The matrix is what item 3.4 plans claims from: invert the sort and it is
    handed the 200 WORST positive-gain swaps, every one with a plausible reason."""
    roster, pool = world
    full = _board(db, roster, pool)
    gains = [s.gain for s in full.swaps]
    assert gains == sorted(gains, reverse=True)
    assert all(g > 0 for g in gains)
    small = _board(db, roster, pool, swap_limit=3)
    assert len(small.swaps) <= 3
    assert [s.gain for s in small.swaps] == gains[:len(small.swaps)]


def test_the_playoff_subtotal_and_the_playoff_weight_seam_both_do_something(db, world):
    """The subtotal is printed on every row and quoted in a reason; the weight is
    the documented seam for real playoff odds. Neither was asserted, so hard-zeroing
    the subtotal or ignoring the weight left the suite green."""
    roster, pool = world
    board = _board(db, roster, pool)
    top = max(board.ranked, key=lambda r: r.marginal_points)
    assert top.playoff_subtotal != 0.0
    assert abs(top.playoff_subtotal) <= abs(top.marginal_points) + 1e-9

    weighted = _board(db, roster, pool, playoff_weight=2.0)
    assert _row(weighted, top.player).marginal_points != pytest.approx(
        top.marginal_points, abs=1e-6)
    # ...and the subtotal is weighted like the total it is a share of
    assert _row(weighted, top.player).playoff_subtotal == pytest.approx(
        2.0 * top.playoff_subtotal, rel=0.05)

    # a window with no playoff weeks in it must be untouched by the seam
    early = _board(db, roster, pool, weeks=range(3, 6))
    early_w = _board(db, roster, pool, weeks=range(3, 6), playoff_weight=2.0)
    for r in early.ranked:
        assert _row(early_w, r.player).marginal_points == pytest.approx(
            r.marginal_points, abs=1e-9)


def _full_schedule(db, byes):
    """A COMPLETE regular-season schedule: every team plays every week except one.

    Anything less is the half-ingested case, which is its own test — and the
    difference matters, because the module only trusts ``schedules`` when the map
    it produces is complete.
    """
    teams = sorted(byes)
    rows = []
    for week in range(1, 18):
        playing = [t for t in teams if byes[t] != week]
        if len(playing) % 2:
            playing.append(playing[0])       # odd week: the fixture is not the NFL
        for i in range(0, len(playing) - 1, 2):
            rows.append((f"g{week}-{i}", week, f"2026-09-{(week % 28) + 1:02d}",
                         playing[i], playing[i + 1]))
    db.executemany(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES (?, 2026, ?, 'REG', ?, ?, ?, "
        "'2026-08-01', '2026-08-01')",
        rows,
    )
    db.commit()


def test_the_bye_map_prefers_schedules_and_the_two_sources_agree(db, world):
    """From 2026-08-01 the schedules table is knowable and becomes the bye source,
    so the projections-derived path — the only one any test exercised — stops
    running in production. Design D3 asked for a cross-check; this is it."""
    from_projections = marginal.bye_map(db, as_of=PULL, season=SEASON)
    assert from_projections.source == "projections"

    _full_schedule(db, {s["team"]: s["bye"] for s in ALL_SPECS if s.get("bye")})
    from_schedules = marginal.bye_map(db, as_of=PULL, season=SEASON)
    assert from_schedules.source == "schedules"
    assert not from_schedules.unknown
    assert not from_schedules.note
    assert dict(from_schedules.byes) == dict(from_projections.byes)


def test_a_half_ingested_schedules_table_does_not_discard_a_correct_bye_map(db, world):
    """``schedules`` was preferred whenever it returned ANY row, with no coverage
    floor: an interrupted ingest left every team's bye "unknown" while a complete,
    correct projections-derived map sat unused in the same database."""
    db.executemany(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, away_team, "
        "home_team, retrieved_as_of, knowable_as_of) VALUES (?, 2026, ?, 'REG', "
        "'2026-09-10', 'ATL', 'DAL', '2026-08-01', '2026-08-01')",
        [(f"g{w}", w) for w in range(1, 4)],
    )
    db.commit()
    byes = marginal.bye_map(db, as_of=PULL, season=SEASON)
    assert byes.source == "projections"
    assert byes.byes["ATL"] == 11 and byes.byes["MIA"] == 5
    assert byes.note and "half-ingested" in byes.note

    roster, pool = world
    board = _board(db, roster, pool)
    assert any("half-ingested" in n for n in board.notes)


def test_a_second_tight_end_lowers_the_backup_tight_end_s_value(db, marginal_world):
    """Design test 14(c): hold the candidate fixed, change only the roster around
    him. Adding a better TE pushes the incumbent backup out of the lineup."""
    base = [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 16.0, "bye": 9,
         "on_team": 10},
        {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 12.0, "bye": 10,
         "on_team": 10},
        {"name": "Tight Two", "pos": "TE", "team": "JAX", "pts": 9.0, "bye": 5, "on_team": 10},
        {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 4.0, "bye": 12},
    ]
    alone_roster, alone_pool = marginal_world(base, retrieved=PULL)
    alone = _row(_board(db, alone_roster, alone_pool), "Tight Two").marginal_points

    db.execute("DELETE FROM projections")
    db.execute("DELETE FROM players")
    db.execute("DELETE FROM league_player_state")
    db.commit()
    crowded_specs = base + [
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 14.0, "bye": 13, "on_team": 10},
    ]
    c_roster, c_pool = marginal_world(crowded_specs, retrieved=PULL)
    crowded = _row(_board(db, c_roster, c_pool), "Tight Two").marginal_points
    assert crowded < alone


def test_a_bye_collision_raises_the_value_of_the_backup_who_covers_it(
    db, marginal_world
):
    """Design test 14(d): the SAME backup is worth more when his starters' byes
    collide with each other and not with his."""
    def value(other_bye):
        db.execute("DELETE FROM projections")
        db.execute("DELETE FROM players")
        db.execute("DELETE FROM league_player_state")
        db.commit()
        specs = [
            {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6,
             "on_team": 10},
            {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11,
             "on_team": 10},
            {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 11,
             "on_team": 10},
            {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 16.0,
             "bye": other_bye, "on_team": 10},
            {"name": "Cover Man", "pos": "WR", "team": "GB", "pts": 8.0, "bye": 4,
             "on_team": 10},
            {"name": "Free Catcher", "pos": "WR", "team": "NYJ", "pts": 3.0, "bye": 12},
        ]
        roster, pool = marginal_world(specs, retrieved=PULL)
        return _row(_board(db, roster, pool), "Cover Man").marginal_points

    collided = value(11)      # three starters all off in week 11
    spread = value(7)         # the same three byes spread out
    assert collided > spread


def test_a_defense_joins_on_a_normalized_team_abbreviation(db, marginal_world):
    """League state normalizes LAR->LA while the projection feed stores LAR
    verbatim; joining raw loses the Rams (31 of 32 measured). The normalization is
    redundant today because ``league/state.py`` normalizes at sync — which is
    exactly the kind of invisible coupling worth pinning."""
    specs = [s for s in ALL_SPECS if s["name"] != "Miami D/ST"] + [
        {"name": "Rams D/ST", "pos": "D/ST", "team": "LA", "pts": 7.0, "bye": 5,
         "on_team": 10, "proj_team": "LAR"},
    ]
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool)
    row = _row(board, "Rams D/ST")
    assert not row.unvalued
    assert row.team == "LA"
    assert board.model.points(row.player_key, 6) == pytest.approx(7.0)


def test_the_banner_names_the_newest_pull_and_mentions_the_oldest(db, marginal_world):
    specs = [dict(s) for s in ALL_SPECS]
    specs[0]["retrieved"] = "2026-09-12"
    roster, pool = marginal_world(specs, retrieved=PULL)
    board = _board(db, roster, pool)
    banner = " ".join(board.freshness)
    assert "projections: pulled 2026-09-15" in banner
    assert "oldest row in this board: 2026-09-12" in banner


def test_latest_truth_still_gates_fact_time_on_the_new_entry_point(db, world):
    """The Rule-1 wrapper backtests and grading must use — never exercised against
    ``build_marginal``. ``latest_truth`` deliberately relaxes RETRIEVAL time; it
    must not relax KNOWABLE time."""
    from ziggurat.data.nfl import base

    roster, pool = world
    rows = base.latest_truth(marginal.build_marginal)(
        db, as_of=PULL, season=SEASON, roster=roster, pool=pool, weeks=WEEKS,
        pool_limit=None,
    )
    assert rows
    early = base.latest_truth(marginal.build_marginal)(
        db, as_of="2026-09-14", season=SEASON, roster=roster, pool=pool, weeks=WEEKS,
        pool_limit=None,
    )
    assert all(r.unvalued for r in early), "nothing was knowable the day before"
