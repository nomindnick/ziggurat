"""Week-by-week roster grader tests (draft/grader.py).

Three groups carry the weight, and they are the three ways this module can be
wrong without anything raising:

1. **The id space.** ``weekly_points_map`` must key on exactly the ids
   ``simulator.load_board`` puts on ``BoardEntry.player_id`` — including the
   ``<POS>:<overall rank>`` fallback that 37% of the live board lands on. A
   silent drift there grades every roster as an empty lineup and reports it as a
   season of holes. ``test_fallback_ids_match_build_valuations_own_rank`` checks
   the fallback against ``build_valuation``'s OWN ``overall_rank`` rather than
   against a re-derivation, so a drift in the mirror fails the test.

2. **Holes must not be carried.** A player who does not play must not be seated
   at zero; the slot behind him must be genuinely empty and reported. The
   load-bearing test is ``test_identical_season_totals_are_separated_by_byes``:
   two rosters with the SAME ``optimal_starting_points`` and different bye
   alignment must get different objectives. That is the entire reason this
   module exists, stated as an assertion.

3. **The probability model must be coherent.** Grading all ten teams of a league
   against each other must return sums that are true by construction:
   expected wins sum to half the games, playoff probabilities sum to the number
   of playoff berths, title probabilities sum to one. Those are not tautologies
   — they are checks on the normal approximation and the independence
   assumption, and they fail on a real modelling error.

Three more groups were added after an adversarial review found the module
returning confident numbers computed from nothing:

4. **The opponent model must REFUSE, not fabricate** (section 8). Every failure
   there had one shape — the model produced exactly ``teams - 1`` rivals of
   ``(0.0, 0.0)``, which is a NON-empty tuple, so the "no rivals" guard never
   fired and a certain win was reported with a straight face.

5. **The field's LEVEL, not just its shape** (section 9). Four one-line
   mutations to the deal each graded a real roster as a near-perfect season with
   every earlier test green; only a golden master of the dealt points catches
   them, and every test here is mutation-verified.

6. **The streamed slots** (section 10). A one-deep K/DST bye is a waiver add in
   this league, not an empty slot, and grading it as a hole made a SECOND kicker
   the best available add on a full roster — a pick a draft optimiser would
   really have spent.

Everything is synthetic (Rule 5): invented player ids, invented teams, no real
league identity anywhere. Where a docstring quotes a measurement it was taken on
the live board on 2026-08-30 and is stated so the next reader can re-run it.
"""

import math
import random
import time

import pytest

from ziggurat.core.lineup import LineupStructureError
from ziggurat.core.lineup_support import DEFAULT_VARIANCE, VarianceModel
from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure, build_valuation
from ziggurat.data.nfl import projections
from ziggurat.data.store import apply_schema, connect
from ziggurat.draft import grader
from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.simulator import load_board, optimal_starting_points

SEASON = 2026
WEEKS = tuple(range(1, 18))
AS_OF = "2026-08-01"

# The league under test, stated once (matches DEFAULT_ROSTER / the live league).
REG_WEEKS = tuple(range(1, 15))
PO_WEEKS = (15, 16, 17)


# ============================================================ in-memory helpers


def _entry(pid, pos, *, season_points=0.0, team=None, rank=1, name=None) -> BoardEntry:
    return BoardEntry(
        player_id=pid,
        name=name or pid,
        position=pos,
        espn_overall_rank=rank,
        house_points=season_points,
        vor=season_points,
        team=team,
    )


def _weeks_for(bye: int | None, weeks=WEEKS) -> tuple[int, ...]:
    return tuple(w for w in weeks if w != bye)


def _line(per_week: float, bye: int | None, weeks=WEEKS) -> dict[int, float]:
    """One player's map entry: points ONLY for weeks he plays (module convention)."""
    return {w: per_week for w in _weeks_for(bye, weeks)}


def _roster_from(spec) -> tuple[list[BoardEntry], dict[str, dict[int, float]], dict[str, str]]:
    """``spec`` is ``[(pid, pos, per_week_points, bye, team)]`` -> entries, map, positions."""
    entries, weekly, positions = [], {}, {}
    for i, (pid, pos, per_week, bye, team) in enumerate(spec):
        entries.append(
            _entry(pid, pos, season_points=per_week * len(_weeks_for(bye)), team=team, rank=i + 1)
        )
        weekly[pid] = _line(per_week, bye)
        positions[pid] = pos
    return entries, weekly, positions


#: A legal, unremarkable 16-man roster: 9 starters + 7 bench, byes spread out.
_BALANCED = [
    ("qb1", "QB", 18.0, 6, "AAA"),
    ("rb1", "RB", 15.0, 5, "BBB"),
    ("rb2", "RB", 13.0, 9, "CCC"),
    ("rb3", "RB", 9.0, 11, "DDD"),
    ("wr1", "WR", 16.0, 7, "EEE"),
    ("wr2", "WR", 14.0, 10, "FFF"),
    ("wr3", "WR", 11.0, 12, "GGG"),
    ("wr4", "WR", 8.0, 13, "HHH"),
    ("te1", "TE", 12.0, 8, "III"),
    ("te2", "TE", 6.0, 14, "JJJ"),
    ("dst1", "DST", 7.0, 4, "KKK"),
    ("k1", "K", 8.0, 3, "LLL"),
    ("qb2", "QB", 14.0, 2, "MMM"),
    ("rb4", "RB", 7.0, 6, "NNN"),
    ("wr5", "WR", 6.0, 9, "OOO"),
    ("te3", "TE", 5.0, 10, "PPP"),
]


def _with_a_board(weekly, positions, *, prefix="fld"):
    """Add the REST of a league's talent to a fixture's (weekly, positions).

    A positions map covering only the roster under test is not a board: every
    player in it is excluded from the field as "yours", the deal produces nine
    rivals scoring 0.0, and the grade comes back as a certain win.
    ``grade_roster`` refuses that outright now, so fixtures that want a dealt
    field have to supply somebody to deal. Filler players never bye (so they
    cannot move a bye-structure assertion) and sit below the roster under test.
    """
    weekly = dict(weekly)
    positions = dict(positions)
    depth = {"QB": 20, "RB": 40, "WR": 45, "TE": 20, "DST": 16, "K": 16}
    top = {"QB": 15.0, "RB": 12.0, "WR": 12.0, "TE": 9.0, "DST": 6.0, "K": 6.0}
    for pos, n in depth.items():
        for i in range(n):
            pid = f"{prefix}-{pos}{i:03d}"
            weekly[pid] = _line(max(1.0, top[pos] - 0.15 * i), None)
            positions[pid] = pos
    return weekly, positions


def _flat_opponent(entries, weekly, *, per_week_total, n=9, prefix="opp"):
    """``n`` rival rosters that each seat exactly ``per_week_total`` every week.

    Nine-man rosters of single-position players with no byes: deterministic and
    bye-free, so a test can move OUR roster and read the effect without the
    opponent moving underneath it. ``prefix`` keeps two different fields in the
    same ``weekly`` map from overwriting each other's points.
    """
    slots = [("QB", 1), ("RB", 2), ("WR", 2), ("TE", 2), ("DST", 1), ("K", 1)]
    per_slot = per_week_total / sum(c for _p, c in slots)
    rosters = {}
    for t in range(n):
        roster = []
        for pos, count in slots:
            for i in range(count):
                pid = f"{prefix}{t}-{pos}{i}"
                roster.append(_entry(pid, pos, season_points=per_slot * 17, team=f"O{t}"))
                weekly[pid] = _line(per_slot, None)
        rosters[t] = roster
    return rosters


# ============================================================ 1. the id space


def _proj_row(player_id, pos, team, week, opponent, stats):
    """A raw Sleeper-shaped projection row (what ``ingest_projections`` eats)."""
    return {
        "player_id": player_id,
        "season": str(SEASON),
        "week": week,
        "season_type": "regular",
        "team": team,
        "opponent": opponent,
        "player": {"position": pos, "team": team},
        "stats": stats,
    }


#: (sleeper_id, position, team, bye week, crosswalk) — the four id branches:
#: "espn" -> board id is the espn_id; "gsis" -> the gsis_id; None -> no crosswalk
#: at all, so the board falls back to "<POS>:<overall rank>"; DEF -> "DST:<team>".
_UNIVERSE = [
    ("s-qb-a", "QB", "AAA", 6, "espn"),
    ("s-qb-b", "QB", "BBB", 7, None),
    ("s-rb-a", "RB", "CCC", 5, "espn"),
    ("s-rb-b", "RB", "DDD", 8, "gsis"),
    ("s-rb-c", "RB", "EEE", 9, None),
    ("s-wr-a", "WR", "FFF", 10, "espn"),
    ("s-wr-b", "WR", "GGG", 11, None),
    ("s-te-a", "TE", "HHH", 12, "gsis"),
    ("s-te-b", "TE", "III", 13, None),
    ("s-k-a", "K", "JJJ", 4, None),
    ("s-k-b", "K", "KKK", 14, "espn"),
    ("AAA", "DEF", "AAA", 6, "def"),
    ("BBB", "DEF", "BBB", 7, "def"),
]


def _stats_for(pos, i):
    """Distinct, deterministic weekly stat lines so no two players tie on points."""
    if pos == "QB":
        return {"pass_yd": 260 + 7 * i, "pass_td": 2, "pass_int": 1}
    if pos in ("RB", "WR", "TE"):
        return {"rush_yd": 40 + 3 * i, "rec": 4 + i, "rec_yd": 55 + 5 * i, "rec_td": 1}
    if pos == "K":
        return {"fgm_40_49": 1, "xpm": 2 + i}
    return {"sack": 2, "int": 1, "pts_allow": 17, "yds_allow": 320 + 5 * i}


def _build_grader_db(db_path, *, retrieved=AS_OF, with_espn_ranks=False, bump=0.0):
    """Projections + a partial players crosswalk, so every board id branch fires."""
    conn = connect(db_path)
    apply_schema(conn)
    for sid, _pos, _team, _bye, kind in _UNIVERSE:
        if kind == "espn":
            conn.execute(
                "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, retrieved_as_of, "
                "knowable_as_of) VALUES (?, ?, ?, ?, ?, ?)",
                (f"g-{sid}", sid, f"e-{sid}", f"Player {sid}", "2026-07-01", "2026-07-01"),
            )
        elif kind == "gsis":
            conn.execute(
                "INSERT INTO players (gsis_id, sleeper_id, name, retrieved_as_of, "
                "knowable_as_of) VALUES (?, ?, ?, ?, ?)",
                (f"g-{sid}", sid, f"Player {sid}", "2026-07-01", "2026-07-01"),
            )
    conn.commit()

    rows = []
    for i, (sid, pos, team, bye, _kind) in enumerate(_UNIVERSE):
        for wk in WEEKS:
            if wk == bye:
                if pos == "DEF":
                    continue  # a D/ST bye row is ABSENT entirely (item 3.2 §2.5)
                rows.append(_proj_row(sid, pos, team, wk, None, {}))  # bye-shaped row
                continue
            stats = dict(_stats_for(pos, i))
            if bump and pos in ("RB", "WR"):
                stats["rec_yd"] = stats["rec_yd"] + bump
            rows.append(_proj_row(sid, pos, team, wk, "ZZZ", stats))
    projections.ingest_projections(conn, rows, retrieved_as_of=retrieved)

    if with_espn_ranks:
        import json
        from pathlib import Path

        from ziggurat.data.nfl.espn_ranks import ingest_espn_ranks

        fixture = Path(__file__).parent / "fixtures" / "espn" / "player_universe.json"
        ingest_espn_ranks(
            conn, json.loads(fixture.read_text()), retrieved_as_of=retrieved, season=SEASON
        )
    return conn


def test_key_space_matches_load_board_exactly(tmp_path):
    """THE test. Without an ESPN snapshot the board is exactly the priced board,
    so the two id sets must be EQUAL — not overlapping, not a superset."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    board = load_board(conn, as_of=AS_OF, season=SEASON)
    weekly = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    conn.close()

    assert board, "expected a non-empty board"
    assert {e.player_id for e in board} == set(weekly)

    # ...and the hard branches actually fired, or the equality proves nothing.
    ids = set(weekly)
    assert any(i.startswith("DST:") for i in ids), "no DST:<team> id exercised"
    assert any(
        i.split(":")[0] in ("QB", "RB", "WR", "TE", "K") and ":" in i for i in ids
    ), "no <POS>:<rank> fallback id exercised"
    assert any(i.startswith("e-") for i in ids), "no espn_id branch exercised"
    assert any(i.startswith("g-") for i in ids), "no gsis_id branch exercised"


def test_fallback_ids_match_build_valuations_own_rank(tmp_path):
    """The mirror is checked against ``build_valuation``'s AUTHORITATIVE rank.

    ``weekly_points_map`` reproduces the VOR ranking to rebuild the
    ``<POS>:<overall rank>`` fallback. If that reproduction drifts by even one
    place this fails — which is the point: comparing against a second copy of my
    own derivation would prove nothing.
    """
    conn = _build_grader_db(tmp_path / "g.sqlite")
    rows = build_valuation(conn, as_of=AS_OF, season=SEASON)
    weekly = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    conn.close()

    checked = 0
    for r in rows:
        if r.espn_id is not None or r.gsis_id is not None:
            continue
        expected = f"DST:{r.team}" if r.position == "DST" else f"{r.position}:{r.overall_rank}"
        assert expected in weekly, f"{expected} missing — the rank mirror has drifted"
        checked += 1
    assert checked >= 6, f"only {checked} fallback rows in the fixture; test is too weak"


def test_espn_union_entries_are_the_only_unmatched_ids(tmp_path):
    """``load_board`` unions the ESPN universe in as UNPRICED entries; those have
    no projection, so they are legitimately absent from the points map. Nothing
    else may be."""
    conn = _build_grader_db(tmp_path / "g.sqlite", with_espn_ranks=True)
    board = load_board(conn, as_of=AS_OF, season=SEASON)
    weekly = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    conn.close()

    matched, total, sample = grader.board_key_coverage(board, weekly)
    assert total > matched, "the ESPN fixture should add unpriced union entries"
    assert matched == len(weekly), f"a priced player fell out of the map: {sample}"
    unmatched = {e.player_id for e in board} - set(weekly)
    for e in board:
        if e.player_id in unmatched:
            assert e.house_points == 0.0, f"{e.name} is priced but unmatched"


def test_weekly_points_map_requires_explicit_as_of(tmp_path):
    conn = _build_grader_db(tmp_path / "g.sqlite")
    with pytest.raises(TypeError):
        grader.weekly_points_map(conn, season=SEASON)  # Rule 1: never an implicit now
    conn.close()


def test_weekly_points_map_leakage_by_retrieval(tmp_path):
    """Rule 1 on the module's one DB seam: nothing retrieved after ``as_of`` is
    visible, and a later re-pull never rewrites what an earlier as_of saw."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    assert grader.weekly_points_map(conn, as_of="2026-07-31", season=SEASON) == {}

    before = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    assert before
    pid = next(k for k in before if k.startswith("e-s-wr-a"))
    week1_before = before[pid][1]

    # A later re-pull revises the receiving line upward.
    conn2 = _build_grader_db(tmp_path / "g2.sqlite")
    conn2.close()
    rows = []
    for i, (sid, pos, team, bye, _kind) in enumerate(_UNIVERSE):
        for wk in WEEKS:
            if wk == bye:
                continue
            stats = dict(_stats_for(pos, i))
            if pos == "WR":
                stats["rec_yd"] = stats["rec_yd"] + 100
            rows.append(_proj_row(sid, pos, team, wk, "ZZZ", stats))
    projections.ingest_projections(conn, rows, retrieved_as_of="2026-08-15")

    old = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    new = grader.weekly_points_map(conn, as_of="2026-08-15", season=SEASON)
    conn.close()
    assert old[pid][1] == week1_before
    assert new[pid][1] > week1_before


def test_a_bye_week_is_absent_not_zero(tmp_path):
    """The convention the whole module rests on. A skill bye row is PRESENT with
    NULL stats (scoring 0.0) and a D/ST bye row is ABSENT — both must come out
    the same way: the week is not in the map."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    weekly = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    conn.close()

    qb = weekly["e-s-qb-a"]          # bye week 6, bye-shaped row present in the feed
    assert 6 not in qb, "a 0.0 bye row leaked into the map as a real week"
    assert set(qb) == set(_weeks_for(6))

    dst = weekly["DST:AAA"]          # bye week 6, no row at all in the feed
    assert 6 not in dst
    assert set(dst) == set(_weeks_for(6))


def test_weekly_points_map_carries_positions_for_the_field(tmp_path):
    conn = _build_grader_db(tmp_path / "g.sqlite")
    weekly = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    conn.close()
    assert isinstance(weekly, dict)
    assert weekly.positions["DST:AAA"] == "DST"
    assert weekly.positions["e-s-rb-a"] == "RB"
    assert weekly.scheduled_weeks("e-s-rb-a") == len(WEEKS) - 1
    with pytest.raises(TypeError):
        weekly.positions["x"] = "QB"  # read-only view, not a mutable back door


# ============================================ 2. holes are real and reportable


def test_a_bye_player_is_not_seated_and_the_slot_is_a_hole():
    """Two RBs behind two RB slots, both on bye in week 5, no third RB: the RB2
    slot is EMPTY, week 5 is a hole, and the week's mean drops by the RB's
    points — it does not quietly score as if the other eight carried it."""
    spec = [
        ("qb1", "QB", 18.0, None, "A"),
        ("rb1", "RB", 15.0, 5, "B"),
        ("rb2", "RB", 13.0, 5, "C"),
        ("wr1", "WR", 16.0, None, "D"),
        ("wr2", "WR", 14.0, None, "E"),
        ("wr3", "WR", 11.0, None, "F"),
        ("te1", "TE", 12.0, None, "G"),
        ("dst1", "DST", 7.0, None, "H"),
        ("k1", "K", 8.0, None, "I"),
    ]
    entries, weekly, positions = _roster_from(spec)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    grade = grader.grade_roster(entries, weekly, opponent_rosters=opponents)

    assert 5 in grade.hole_weeks
    empty = dict(grade.hole_detail)[5]
    assert "RB2" in empty, empty
    # week 4 seats RB1+RB2+WR-flex; week 5 loses both RBs and the flex re-fills
    # with wr3 only, so exactly rb1+rb2 worth of points is missing.
    assert grade.weekly_means[3] - grade.weekly_means[4] == pytest.approx(15.0 + 13.0)
    assert grade.win_probs[4] < grade.win_probs[3]


def test_holes_are_reported_in_playoff_weeks_too():
    spec = [(p, pos, pts, (16 if p == "dst1" else None), t) for p, pos, pts, _b, t in _BALANCED]
    entries, weekly, _pos = _roster_from(spec)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    grade = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    assert 16 in grade.hole_weeks
    assert 16 in grade.playoff_weeks


def test_identical_season_totals_are_separated_by_byes():
    """THE reason this module exists.

    Two rosters, one player different, chosen so the SEASON TOTALS are identical
    to the last decimal — so ``optimal_starting_points`` cannot tell them apart —
    but one RB byes alongside the other starting RB and the other does not. The
    week-by-week objective must prefer the roster without the collision.
    """
    base = [
        ("qb1", "QB", 18.0, None, "A"),
        ("rb1", "RB", 15.0, 5, "B"),       # the RB the swapped player collides with
        ("rb3", "RB", 9.0, None, "C"),
        ("wr1", "WR", 16.0, None, "D"),
        ("wr2", "WR", 14.0, None, "E"),
        ("wr3", "WR", 11.0, None, "F"),
        ("te1", "TE", 12.0, None, "G"),
        ("dst1", "DST", 7.0, None, "H"),
        ("k1", "K", 8.0, None, "I"),
    ]
    collide = [*base, ("rb2a", "RB", 13.0, 5, "J")]     # byes WITH rb1, in week 5
    spread = [*base, ("rb2b", "RB", 13.0, 13, "K")]     # byes alone, in week 13

    e_c, w_c, _ = _roster_from(collide)
    e_s, w_s, _ = _roster_from(spread)
    weekly = {**w_c, **w_s}

    # The season sums are identical: 16 played weeks x the same per-week points,
    # so the OLD metric cannot tell these two rosters apart at all.
    assert optimal_starting_points(e_c) == pytest.approx(optimal_starting_points(e_s))

    opponents = _flat_opponent(e_c, weekly, per_week_total=100.0)
    g_c = grader.grade_roster(e_c, weekly, opponent_rosters=opponents)
    g_s = grader.grade_roster(e_s, weekly, opponent_rosters=opponents)

    assert g_s.objective > g_c.objective, (
        "the season-total metric cannot see a bye collision; this one must"
    )
    assert g_c.hole_weeks == (5,)
    assert g_s.hole_weeks == ()
    assert any("BYE COLLISION week 5" in r for r in g_c.reasons)


def test_a_player_with_no_projection_is_unavailable_and_disclosed():
    entries, weekly, _pos = _roster_from(_BALANCED)
    weekly["k1"] = {}                       # on the board, no forecast at all
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    grade = grader.grade_roster(entries, weekly, opponent_rosters=opponents)

    assert set(grade.hole_weeks) >= set(REG_WEEKS) | set(PO_WEEKS)
    assert any("NO PROJECTION AT ALL" in r and "k1" in r for r in grade.reasons)


def test_thin_coverage_is_called_out_as_a_data_gap_not_a_verdict():
    """Item 3.2's A.J. Brown case: one real week and sixteen empty ones is a hole
    in the FEED, and a novice must not read it as 'this player is worthless'."""
    entries, weekly, _pos = _roster_from(_BALANCED)
    weekly["wr4"] = {1: 8.0}
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    grade = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    assert any("THIN COVERAGE" in r for r in grade.reasons)


def test_weekly_means_is_dense_from_week_one():
    entries, weekly, _pos = _roster_from(_BALANCED)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    grade = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    assert len(grade.weekly_means) == 17
    assert len(grade.win_probs) == 17
    # week 3 is the kicker's bye: index 2 is week 3, and it is lower than week 2.
    assert grade.weekly_means[2] < grade.weekly_means[1]


# ============================================================ 3. the win model


def test_expected_wins_is_seven_against_a_mirror_of_yourself():
    """The cleanest invariant available: identical rosters -> P(win)=0.5 every
    week -> exactly half the games. Catches any sign error or stray offset."""
    entries, weekly, _pos = _roster_from(_BALANCED)
    mirrors = {t: list(entries) for t in range(9)}
    grade = grader.grade_roster(entries, weekly, opponent_rosters=mirrors)
    assert grade.expected_wins == pytest.approx(len(REG_WEEKS) / 2.0)
    for wk in REG_WEEKS:
        assert grade.win_probs[wk - 1] == pytest.approx(0.5)


def test_a_stronger_opponent_lowers_every_week():
    entries, weekly, _pos = _roster_from(_BALANCED)
    weak = _flat_opponent(entries, weekly, per_week_total=80.0, prefix="weak")
    strong = _flat_opponent(entries, weekly, per_week_total=140.0, prefix="strong")
    g_weak = grader.grade_roster(entries, weekly, opponent_rosters=weak)
    g_strong = grader.grade_roster(entries, weekly, opponent_rosters=strong)
    assert g_weak.expected_wins > g_strong.expected_wins
    assert g_weak.playoff_prob > g_strong.playoff_prob


def test_more_variance_pulls_a_favorite_back_toward_a_coin_flip():
    """The posture asymmetry item 3.5 measured, re-checked here because the
    grader supplies its own variance sum. A big favorite prefers LOW variance;
    if the sigma model were ignored these two would be identical."""
    entries, weekly, _pos = _roster_from(_BALANCED)
    opponents = _flat_opponent(entries, weekly, per_week_total=90.0)
    wide = VarianceModel(
        coefficients=DEFAULT_VARIANCE.coefficients,
        r_squared=DEFAULT_VARIANCE.r_squared,
        k_flat_sigma=DEFAULT_VARIANCE.k_flat_sigma * 6,
        correlation_qb_passcatcher=DEFAULT_VARIANCE.correlation_qb_passcatcher,
        opp_flat_sigma=DEFAULT_VARIANCE.opp_flat_sigma,
        cohort="test", label="test", source="test",
    )
    tight = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    loose = grader.grade_roster(entries, weekly, opponent_rosters=opponents, variance=wide)
    assert tight.expected_wins > len(REG_WEEKS) / 2.0, "fixture must make us a favorite"
    assert loose.expected_wins < tight.expected_wins


def test_qb_and_his_own_pass_catcher_raise_the_lineup_variance():
    """The one non-zero cross-player correlation in the model. Stacking a QB with
    a WR on the SAME NFL team must matter; the same two players on different
    teams must not."""
    stacked = [
        ("qb1", "QB", 20.0, None, "SAME"),
        ("wr1", "WR", 18.0, None, "SAME"),
        ("wr2", "WR", 14.0, None, "B"),
        ("wr3", "WR", 12.0, None, "C"),
        ("rb1", "RB", 15.0, None, "D"),
        ("rb2", "RB", 13.0, None, "E"),
        ("te1", "TE", 11.0, None, "F"),
        ("dst1", "DST", 7.0, None, "G"),
        ("k1", "K", 8.0, None, "H"),
    ]
    split = [(p, pos, pts, b, ("OTHER" if p == "wr1" else t)) for p, pos, pts, b, t in stacked]
    e_st, w_st, _ = _roster_from(stacked)
    e_sp, w_sp, _ = _roster_from(split)
    weekly = {**w_st, **w_sp}
    opponents = _flat_opponent(e_st, weekly, per_week_total=90.0)
    g_st = grader.grade_roster(e_st, weekly, opponent_rosters=opponents)
    g_sp = grader.grade_roster(e_sp, weekly, opponent_rosters=opponents)

    assert g_st.weekly_means[0] == pytest.approx(g_sp.weekly_means[0]), "points must match"
    # Same mean, more variance from the stack -> a favorite wins slightly less often.
    assert g_st.expected_wins < g_sp.expected_wins


def test_grade_is_deterministic():
    entries, weekly, positions = _roster_from(_BALANCED)
    weekly, positions = _with_a_board(weekly, positions)
    a = grader.grade_roster(entries, weekly, positions=positions)
    b = grader.grade_roster(list(reversed(entries)), weekly, positions=positions)
    assert a.objective == b.objective
    assert a.weekly_means == b.weekly_means
    assert a.hole_detail == b.hole_detail


# ======================================================== 4. the opponent field


def _field_world(seed=0):
    """A league-sized synthetic board: enough depth at every position for nine
    rival lineups plus a roster under test, with byes spread across weeks."""
    rng = random.Random(seed)
    counts = {"QB": 28, "RB": 60, "WR": 70, "TE": 28, "DST": 24, "K": 24}
    tops = {"QB": 21.0, "RB": 17.0, "WR": 17.0, "TE": 13.0, "DST": 8.0, "K": 8.5}
    weekly, positions, entries = {}, {}, []
    rank = 0
    for pos, n in counts.items():
        for i in range(n):
            rank += 1
            pid = f"{pos}{i:03d}"
            per_week = max(1.0, tops[pos] - 0.22 * i)
            bye = 5 + (rank % 10)      # byes land in weeks 5..14, as 2026's do
            weekly[pid] = _line(per_week, bye)
            positions[pid] = pos
            entries.append(
                _entry(pid, pos, season_points=per_week * 16, team=f"T{rank % 32:02d}", rank=rank)
            )
    rng.shuffle(entries)
    entries.sort(key=lambda e: -e.house_points)
    return entries, weekly, positions


def _deal_league(entries, roster=DEFAULT_ROSTER, rounds=16):
    """Deal the board into ten legal-ish rosters, snake order, best available at
    the position each team still needs. Deterministic, no rng."""
    teams = roster.teams
    need = [dict(roster.starters) for _ in range(teams)]
    for n in need:
        n["RB"] = n.get("RB", 0) + 1     # the flex, taken as an extra RB/WR
    rosters = {t: [] for t in range(teams)}
    pool = list(entries)
    for r in range(rounds):
        order = range(teams) if r % 2 == 0 else range(teams - 1, -1, -1)
        for t in order:
            wanted = {p for p, c in need[t].items() if c > 0}
            pick = next((e for e in pool if not wanted or e.position in wanted), pool[0])
            pool.remove(pick)
            rosters[t].append(pick)
            if pick.position in need[t] and need[t][pick.position] > 0:
                need[t][pick.position] -= 1
    return rosters


def test_the_dealt_field_is_bunched_not_sorted():
    """A snake deal is what makes the synthetic field realistic. A naive
    rank-ordered deal would hand rival 0 every #1 and rival 8 every #9; that
    field's spread is several times too wide and would wreck the playoff odds."""
    _entries, weekly, positions = _field_world()
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 18))
    mus = [mu for mu, _var in grader.field_lineups(pool, 1)]
    assert len(mus) == DEFAULT_ROSTER.teams - 1
    assert all(m > 0 for m in mus)
    spread = (max(mus) - min(mus)) / (sum(mus) / len(mus))
    assert spread < 0.30, f"field spread {spread:.2f} — this is a sorted deal, not a snake"
    assert spread > 0.01, "a field with no spread at all cannot rank teams"


def test_the_field_never_starts_a_player_on_your_roster():
    _entries, weekly, positions = _field_world()
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 18))
    open_field = grader.field_lineups(pool, 1)
    # take the nine best players outright; the field must get measurably worse
    mine = frozenset(["QB000", "RB000", "RB001", "WR000", "WR001", "TE000", "DST000", "K000"])
    thinned = grader.field_lineups(pool, 1, exclude=mine)
    assert sum(m for m, _v in thinned) < sum(m for m, _v in open_field)
    # ...and every rival is still fielding a full-strength-ish lineup
    assert all(m > 0 for m, _v in thinned)


def test_a_bye_week_thins_the_field_too():
    """The rivals draw from the same board, so a heavy bye week must lower THEIR
    week as well — otherwise every bye would look like a pure own-goal."""
    _entries, weekly, positions = _field_world()
    # put every DST on bye in week 9
    for pid, pos in positions.items():
        if pos == "DST":
            weekly[pid] = _line(next(iter(weekly[pid].values())), 9)
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 18))
    normal = sum(m for m, _v in grader.field_lineups(pool, 8))
    thinned = sum(m for m, _v in grader.field_lineups(pool, 9))
    assert thinned < normal


def test_field_pool_is_deterministic_under_ties():
    """Two players with identical points must always deal in the same order, or
    a rollout's grades wobble for no reason."""
    weekly = {f"p{i}": _line(10.0, None) for i in range(20)}
    positions = {f"p{i}": "WR" for i in range(20)}
    a = grader.build_field_pool(weekly, positions, weeks=[1])
    b = grader.build_field_pool(dict(reversed(list(weekly.items()))), positions, weeks=[1])
    assert a.ladders[1]["WR"] == b.ladders[1]["WR"]


def test_grade_refuses_to_invent_an_opponent():
    entries, weekly, _positions = _roster_from(_BALANCED)
    plain = {k: dict(v) for k, v in weekly.items()}      # a bare dict: no positions
    with pytest.raises(grader.GradeInputError, match="Refusing to invent an opponent"):
        grader.grade_roster(entries, plain)


def test_the_field_is_built_once_and_reused(tmp_path, monkeypatch):
    """Performance is a correctness property here: this is called inside a
    Monte-Carlo rollout, and rebuilding the (roster-independent) field per call
    is a ~7x regression (5 ms vs 0.8 ms measured on the live board) that nothing
    else would catch."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    weekly = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    conn.close()
    entries = [
        _entry(pid, weekly.positions[pid], team=weekly.teams[pid])
        for pid in list(weekly)[:9]
    ]

    calls = []
    real = grader.build_field_pool
    monkeypatch.setattr(
        grader, "build_field_pool",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )
    for _ in range(5):
        grader.grade_roster(entries, weekly)
    assert len(calls) == 1, f"the field was rebuilt {len(calls)} times"


# ==================================================== 5. playoffs: coherence


def _league_grades(**kwargs):
    entries, weekly, positions = _field_world()
    rosters = _deal_league(entries)
    out = []
    for t in sorted(rosters):
        others = {k: v for k, v in rosters.items() if k != t}
        out.append(grader.grade_roster(rosters[t], weekly, opponent_rosters=others, **kwargs))
    return out


def test_expected_wins_across_the_league_sum_to_half_the_games():
    grades = _league_grades()
    total = sum(g.expected_wins for g in grades)
    assert total == pytest.approx(DEFAULT_ROSTER.teams * len(REG_WEEKS) / 2.0, abs=1e-6)


def test_playoff_probabilities_sum_to_the_number_of_berths():
    """Six of ten teams make the bracket, so the ten probabilities must sum to
    six. They are computed independently per team through a normal
    approximation, so this is a real check on that approximation — it is off by
    ~1% on the live board and must not drift far past that."""
    grades = _league_grades()
    total = sum(g.playoff_prob for g in grades)
    assert total == pytest.approx(grader.DEFAULT_PLAYOFF_TEAMS, abs=0.35), total


def test_title_probabilities_sum_to_one():
    grades = _league_grades()
    total = sum(g.title_prob for g in grades)
    assert total == pytest.approx(1.0, abs=0.15), total


def test_bye_seeds_match_the_bracket_shape():
    assert grader._bye_seeds(6, 3) == 2      # this league: 6 teams, 3 rounds of 8
    assert grader._bye_seeds(4, 2) == 0
    assert grader._bye_seeds(8, 3) == 0
    assert grader._bye_seeds(6, 0) == 0


def test_playoff_grid_is_converged(monkeypatch):
    """The integral over your own win total runs on a fixed 25-point grid. If 25
    is too coarse, the probabilities are wrong in a way no other test sees."""
    own = (8.5, 1.7)
    others = [(7.0 + 0.2 * i, 1.8) for i in range(9)]
    coarse = grader._seed_probabilities(own, others, playoff_teams=6, bye_seeds=2)
    monkeypatch.setattr(grader, "_WIN_TOTAL_GRID", 401)
    fine = grader._seed_probabilities(own, others, playoff_teams=6, bye_seeds=2)
    assert coarse[0] == pytest.approx(fine[0], abs=2e-3)
    assert coarse[1] == pytest.approx(fine[1], abs=2e-3)


def test_a_hole_costs_playoff_odds_not_just_points():
    entries, weekly, _pos = _roster_from(_BALANCED)
    healthy = _flat_opponent(entries, weekly, per_week_total=105.0)
    good = grader.grade_roster(entries, weekly, opponent_rosters=healthy)
    weekly = {**weekly, "dst1": {}, "k1": {}}
    holed = grader.grade_roster(entries, weekly, opponent_rosters=healthy)
    assert holed.playoff_prob < good.playoff_prob
    assert holed.title_prob < good.title_prob
    assert holed.objective < good.objective


def test_bye_seeds_are_credited_a_shorter_bracket():
    """A top-two seed plays two playoff games, not three. Zeroing the bye
    probability must therefore lower the title odds."""
    grades = _league_grades()
    best = max(grades, key=lambda g: g.expected_wins)
    assert best.playoff_bye_prob > 0.0
    assert best.playoff_bye_prob <= best.playoff_prob + 1e-9
    assert best.title_prob > 0.0


# ================================================= 6. contract & refusals


def test_the_public_contract_is_what_downstream_builds_against():
    """Pin the field names and the default objective. A downstream agent is
    writing against this list verbatim."""
    entries, weekly, positions = _roster_from(_BALANCED)
    weekly, positions = _with_a_board(weekly, positions)
    g = grader.grade_roster(entries, weekly, positions=positions)
    for name in (
        "objective", "expected_wins", "playoff_prob", "title_prob",
        "weekly_means", "hole_weeks", "reasons",
    ):
        assert hasattr(g, name), name
    assert isinstance(g.weekly_means, tuple)
    assert isinstance(g.hole_weeks, tuple)
    assert isinstance(g.reasons, tuple) and g.reasons
    assert g.objective == pytest.approx(g.expected_wins), (
        "the default objective IS expected wins; a weighted blend must be opt-in"
    )
    weighted = grader.grade_roster(
        entries, weekly, positions=positions,
        objective_playoff_weight=2.0, objective_title_weight=5.0,
    )
    assert weighted.objective == pytest.approx(
        weighted.expected_wins + 2.0 * weighted.playoff_prob + 5.0 * weighted.title_prob
    )


def test_overlapping_season_and_playoff_weeks_are_refused():
    entries, weekly, positions = _roster_from(_BALANCED)
    with pytest.raises(grader.GradeInputError, match="BOTH regular season and playoff"):
        grader.grade_roster(
            entries, weekly, positions=positions,
            regular_season_weeks=range(1, 16), playoff_weeks=(15, 16, 17),
        )


def test_an_empty_season_is_refused():
    entries, weekly, positions = _roster_from(_BALANCED)
    with pytest.raises(grader.GradeInputError, match="no season to grade"):
        grader.grade_roster(entries, weekly, positions=positions, regular_season_weeks=())


def test_an_opponent_mapping_the_wrong_size_is_refused():
    """A short mapping does not weaken the field, it SHRINKS THE LEAGUE: the
    playoff DP asks "do at most 5 rivals finish above me?", which is certain with
    five rivals, so it used to return a confident "playoff odds 100% (top 6 of
    10)" off three rosters. Measured on the live board before the fix: 5 rivals
    -> playoff 1.0000, 1 rival -> playoff 1.0000 and first-round bye 1.0000."""
    entries, weekly, _positions = _roster_from(_BALANCED)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    for n in (0, 1, 5, 8):
        short = {k: v for k, v in list(opponents.items())[:n]}
        with pytest.raises(grader.GradeInputError, match=f"holds {n} rivals"):
            grader.grade_roster(entries, weekly, opponent_rosters=short)
    # ...and the right size still grades.
    full = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    assert 0.0 < full.playoff_prob < 1.0


def test_a_roster_shape_greedy_cannot_solve_is_refused():
    """The seater is only proven optimal for ONE flex pooled over RB/WR/TE. The
    grader must inherit that refusal rather than quietly returning a suboptimal
    lineup 14 times over."""
    entries, weekly, positions = _roster_from(_BALANCED)
    two_flex = RosterStructure(flex_slots=2)
    with pytest.raises(LineupStructureError):
        grader.grade_roster(entries, weekly, positions=positions, roster=two_flex)


def test_collision_reasons_name_the_players_a_novice_must_act_on():
    """Rule 6: the operator cannot smell a wrong roster. Every hole must name the
    slot, the week, and who is out."""
    spec = [
        ("qb1", "QB", 18.0, None, "A"),
        ("rb1", "RB", 15.0, 7, "B"),
        ("rb2", "RB", 13.0, 7, "C"),
        ("wr1", "WR", 16.0, None, "D"),
        ("wr2", "WR", 14.0, None, "E"),
        ("wr3", "WR", 11.0, None, "F"),
        ("te1", "TE", 12.0, None, "G"),
        ("dst1", "DST", 7.0, None, "H"),
        ("k1", "K", 8.0, None, "I"),
    ]
    entries, weekly, _pos = _roster_from(spec)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    grade = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    hole_line = next(r for r in grade.reasons if r.startswith("week 7:"))
    assert "rb1" in hole_line and "rb2" in hole_line
    assert "RB2" in hole_line
    assert any("BYE COLLISION week 7" in r for r in grade.reasons)
    # every labelled prior travels into the reasons
    assert any(DEFAULT_VARIANCE.label in r for r in grade.reasons)
    assert any(grader.PLAYOFF_LABEL in r for r in grade.reasons)
    # Item 4.1 audit, KICK-2: the K sigma line says NOT YET FITTED (migration 013
    # landed the FG columns), never that weekly_stats lacks them.
    spread = next(r for r in grade.reasons if r.startswith("spread, the two pieces"))
    assert "not yet fitted" in spread and "migration 013" in spread
    assert "carries no field-goal" not in spread and "cannot be fitted" not in spread


def test_format_season_grade_is_readable_and_flags_holes():
    entries, weekly, _pos = _roster_from(_BALANCED)
    weekly = {**weekly, "k1": {}}
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    text = grader.format_season_grade(grader.grade_roster(
        entries, weekly, opponent_rosters=opponents))
    assert "expected wins" in text
    assert "HOLE" in text
    assert "wk  1" in text and "wk 17" in text


def test_duplicate_board_ids_raise_rather_than_merge(tmp_path, monkeypatch):
    """A merged key is invisible; a raise is not. Structurally unreachable today,
    so the guard is exercised by forcing the derivation to collide."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    monkeypatch.setattr(grader, "_board_player_id", lambda **kw: "SAME")
    with pytest.raises(grader.BoardKeyCollision):
        grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    conn.close()


# ==================================================== 7. cost (a real budget)


def test_one_grade_on_a_full_roster_is_fast_enough_for_a_rollout(tmp_path):
    """Measured 0.80 ms/call on the live 2026 board (3,229 priced players, a
    16-man roster, cached field). The bound here is deliberately loose enough not
    to flake on a busy box and tight enough to catch an algorithmic regression."""
    entries, weekly, positions = _field_world()
    roster = _deal_league(entries)[0]
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 18))
    grader.grade_roster(roster, weekly, field=pool)      # warm

    n = 40
    t0 = time.perf_counter()
    for _ in range(n):
        grader.grade_roster(roster, weekly, field=pool)
    per_call_ms = (time.perf_counter() - t0) / n * 1000
    assert per_call_ms < 25.0, f"{per_call_ms:.2f} ms/call — check for a rebuilt field"


def test_win_total_moments_are_the_poisson_binomial_ones():
    probs = [0.6, 0.5, 0.7, 0.4]
    mean, sd = grader._win_total_moments(probs)
    assert mean == pytest.approx(2.2)
    assert sd == pytest.approx(math.sqrt(sum(p * (1 - p) for p in probs)))
    # the floor keeps a degenerate season from collapsing the integral
    _m, sd0 = grader._win_total_moments([1.0] * 14)
    assert sd0 == grader._MIN_WIN_SIGMA


def test_a_mid_draft_roster_grades_honestly_instead_of_crashing():
    """The engine calls this on PARTIAL rosters inside a rollout. An empty roster
    is not an error state — it is a roster with nine holes, and it must read that
    way rather than raise or quietly score zero-as-average."""
    full, weekly, positions = _roster_from(_BALANCED)
    opponents = _flat_opponent(full, weekly, per_week_total=100.0)

    empty = grader.grade_roster([], weekly, opponent_rosters=opponents)
    # Not exactly zero: a Normal margin leaves a vanishing tail where the
    # opponent scores below nothing. ~6e-6 per week is the model being honest
    # about its own shape, not a bug.
    assert empty.expected_wins == pytest.approx(0.0, abs=1e-3)
    assert set(empty.hole_weeks) == set(REG_WEEKS) | set(PO_WEEKS)
    assert all(m == 0.0 for m in empty.weekly_means)

    partial = grader.grade_roster(full[:4], weekly, opponent_rosters=opponents)
    assert 0.0 < partial.expected_wins < grader.grade_roster(
        full, weekly, opponent_rosters=opponents).expected_wins
    assert partial.hole_weeks  # four players cannot fill nine slots


def test_a_negative_projection_is_explained_not_blamed_on_a_bye():
    """``fill_lineup`` leaves a slot empty rather than seat a below-zero D/ST (0
    beats a negative). The reason must say THAT, not imply the player is on bye —
    the operator's action is completely different."""
    spec = [
        ("qb1", "QB", 18.0, None, "A"),
        ("rb1", "RB", 15.0, None, "B"),
        ("rb2", "RB", 13.0, None, "C"),
        ("wr1", "WR", 16.0, None, "D"),
        ("wr2", "WR", 14.0, None, "E"),
        ("wr3", "WR", 11.0, None, "F"),
        ("te1", "TE", 12.0, None, "G"),
        ("dst1", "DST", 7.0, None, "H"),
        ("k1", "K", 8.0, None, "I"),
    ]
    entries, weekly, _pos = _roster_from(spec)
    weekly["dst1"] = {**weekly["dst1"], 6: -3.0}     # a real bracket blowup week
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    grade = grader.grade_roster(entries, weekly, opponent_rosters=opponents)

    assert grade.hole_weeks == (6,)
    line = next(r for r in grade.reasons if r.startswith("week 6:"))
    assert "BELOW zero" in line
    assert "out this week" not in line


def test_one_player_out_of_a_one_deep_position_is_not_called_a_collision():
    """A single kicker on his bye is a HOLE, not a collision. Reporting it twice
    under two headings trains the operator to skim past the heading that means
    'you drafted two players who disappear on the same day'."""
    spec = [
        ("qb1", "QB", 18.0, None, "A"),
        ("rb1", "RB", 15.0, None, "B"),
        ("rb2", "RB", 13.0, None, "C"),
        ("wr1", "WR", 16.0, None, "D"),
        ("wr2", "WR", 14.0, None, "E"),
        ("wr3", "WR", 11.0, None, "F"),
        ("te1", "TE", 12.0, None, "G"),
        ("dst1", "DST", 7.0, None, "H"),
        ("k1", "K", 8.0, 4, "I"),
    ]
    entries, weekly, _pos = _roster_from(spec)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    grade = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    assert grade.hole_weeks == (4,)
    assert any(r.startswith("week 4:") and "k1" in r for r in grade.reasons)
    assert not any("BYE COLLISION" in r for r in grade.reasons)


# ==================================================================
#   8. the opponent model must REFUSE rather than fabricate
#
# Every test in this section reproduces a measured failure in which the module
# returned a plausible number computed from nothing. They share one shape: the
# opponent model produced exactly ``teams - 1`` tuples of (0.0, 0.0), which is a
# NON-empty tuple, so the "no rivals" guard never fired and the grade came back
# as a certain win.
# ==================================================================


def _covered_world():
    """A field world plus a roster drawn from it (the normal, working case)."""
    entries, weekly, positions = _field_world()
    mine = _deal_league(entries)[0]
    return mine, weekly, positions


def test_a_field_pool_that_misses_the_playoff_weeks_is_refused():
    """THE documented fast path, and the measured disaster. Building the pool
    over the regular season (``weeks=range(1, 15)``) and grading with the default
    playoff weeks gave, on the live board: win probability 1.000 in weeks 15-17,
    opponent means 0.0, and title odds 0.696 -> 1.000, with the reasons happily
    printing 'your per-playoff-week win rate against that field is 100%'."""
    mine, weekly, positions = _covered_world()
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 15))
    with pytest.raises(grader.GradeInputError, match=r"weeks \[15, 16, 17\] are missing"):
        grader.grade_roster(mine, weekly, field=pool)

    # ...and the same pool grades fine when the season really is weeks 1-14.
    ok = grader.grade_roster(mine, weekly, field=pool,
                             regular_season_weeks=range(1, 15), playoff_weeks=())
    assert 0.0 < ok.expected_wins < 14.0


def test_a_field_pool_shorter_than_the_season_is_refused():
    """The narrower version: a pool over weeks 1-8 used to return
    ``expected_wins`` 12.79 of 14 with weeks 9-14 all won by certainty."""
    mine, weekly, positions = _covered_world()
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 9))
    with pytest.raises(grader.GradeInputError, match="the field pool covers weeks"):
        grader.grade_roster(mine, weekly, field=pool)


def test_a_field_pool_for_a_different_league_shape_is_refused():
    """A pool carries the roster structure that decided how many rivals it deals
    and how deep its ladders go. Measured on the live board: a 12-team pool used
    in a 10-team grade dealt eleven rivals and moved expected wins 11.86 -> 12.19
    while the reasons still said 'top 6 of 10'."""
    mine, weekly, positions = _covered_world()
    pool = grader.build_field_pool(
        weekly, positions, weeks=range(1, 18), roster=RosterStructure(teams=12)
    )
    with pytest.raises(grader.GradeInputError, match="12-team roster structure"):
        grader.grade_roster(mine, weekly, field=pool)


def test_a_field_pool_built_with_a_different_sigma_model_is_refused():
    """Rivals priced under one dispersion prior and you under another tilts every
    win probability, invisibly."""
    mine, weekly, positions = _covered_world()
    other = VarianceModel(
        coefficients=DEFAULT_VARIANCE.coefficients,
        r_squared=DEFAULT_VARIANCE.r_squared,
        k_flat_sigma=DEFAULT_VARIANCE.k_flat_sigma * 4,
        correlation_qb_passcatcher=DEFAULT_VARIANCE.correlation_qb_passcatcher,
        opp_flat_sigma=DEFAULT_VARIANCE.opp_flat_sigma,
        cohort="test", label="test-wide", source="test",
    )
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 18), variance=other)
    with pytest.raises(grader.GradeInputError, match="sigma model is not the one"):
        grader.grade_roster(mine, weekly, field=pool)
    # ...and it grades when both sides agree.
    assert grader.grade_roster(mine, weekly, field=pool, variance=other).expected_wins > 0


def test_a_positions_map_covering_only_your_own_roster_is_refused():
    """The other documented escape hatch. ``positions=`` restricted to the roster
    under test leaves the field with nobody to deal — every candidate is excluded
    as yours — which measured as expected_wins 14.000 of 14, playoff 1.000 and
    title 1.000 on the live board, with no reason line mentioning it."""
    mine, weekly, _positions = _covered_world()
    own_only = {e.player_id: e.position for e in mine}
    with pytest.raises(grader.GradeInputError, match="all score 0.0"):
        grader.grade_roster(mine, dict(weekly), positions=own_only)


def test_field_lineups_refuses_a_week_the_pool_never_covered():
    """The guard at its own level, so a caller using ``field_lineups`` directly
    (it is public) gets the refusal too."""
    _entries, weekly, positions = _field_world()
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 15))
    with pytest.raises(grader.GradeInputError, match="not in this field pool"):
        grader.field_lineups(pool, 15)
    assert len(grader.field_lineups(pool, 14)) == DEFAULT_ROSTER.teams - 1


def test_an_empty_points_map_is_refused_on_both_paths():
    """Measured: a map built at an as_of before the first projection pull (or with
    a typo'd season/source) returned expected wins 7.0000, playoff 60%, title
    10.0% — the exact numbers a coin-flip league produces, from zero rows. The
    objective was then the CONSTANT 7.0 for every candidate roster."""
    entries, weekly, positions = _roster_from(_BALANCED)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    empty = grader.WeeklyPointsMap({}, positions={}, names={}, teams={})
    for kwargs in ({}, {"opponent_rosters": opponents}, {"positions": positions}):
        with pytest.raises(grader.GradeInputError, match="EMPTY"):
            grader.grade_roster(entries, empty, **kwargs)


def test_a_week_below_one_is_refused_rather_than_wrapped():
    """``weekly_means[wk - 1]`` is a dense index, so week 0 wrote means[-1] — the
    LAST week of the season — and the week-0 numbers vanished silently while the
    documented '[7] is always week 8' contract quietly stopped holding."""
    entries, weekly, positions = _roster_from(_BALANCED)
    weekly, positions = _with_a_board(weekly, positions)
    with pytest.raises(grader.GradeInputError, match="not fantasy weeks"):
        grader.grade_roster(entries, weekly, positions=positions,
                            regular_season_weeks=[0, 1, 2], playoff_weeks=())
    with pytest.raises(grader.GradeInputError, match="not fantasy weeks"):
        grader.grade_roster(entries, weekly, positions=positions,
                            regular_season_weeks=[1, 2], playoff_weeks=(-1,))
    ok = grader.grade_roster(entries, weekly, positions=positions,
                             regular_season_weeks=[1, 2], playoff_weeks=())
    assert len(ok.weekly_means) == 2


def test_playoff_teams_must_fit_the_league():
    entries, weekly, _positions = _roster_from(_BALANCED)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    for bad in (0, 11, 20):
        with pytest.raises(grader.GradeInputError, match="impossible in a 10-team"):
            grader.grade_roster(entries, weekly, opponent_rosters=opponents,
                                playoff_teams=bad)


# ==================================================================
#   9. the dealt field's LEVEL, not just its shape
#
# Four single-line mutations to the deal each graded a real roster as a
# near-perfect season while every earlier test stayed green: sorting the ladder
# ascending, trimming the wrong end, never dealing the FLEX, and dropping the
# rivals' variance. Only pinning the actual points catches those.
# ==================================================================


#: A one-week board with distinct, strictly descending points per position, so
#: every dealt seat total is arithmetic rather than an accident of ties.
_DEAL_COUNTS = {"QB": 14, "RB": 34, "WR": 34, "TE": 14, "DST": 14, "K": 14}
_DEAL_TOP = {"QB": 24.0, "RB": 20.0, "WR": 19.0, "TE": 14.0, "DST": 10.0, "K": 9.0}
_DEAL_STEP = {"QB": 0.7, "RB": 0.4, "WR": 0.35, "TE": 0.5, "DST": 0.3, "K": 0.25}


def _deal_world():
    weekly, positions = {}, {}
    for pos, n in _DEAL_COUNTS.items():
        for i in range(n):
            pid = f"{pos}{i:03d}"
            weekly[pid] = {1: round(_DEAL_TOP[pos] - _DEAL_STEP[pos] * i, 4)}
            positions[pid] = pos
    return weekly, positions


def test_the_dealt_field_scores_exactly_what_the_ladders_say():
    """GOLDEN MASTER of the nine rival lineups on a board with no ties.

    Recorded from the implementation and independently hand-derived below, so it
    pins the LEVEL of the modelled opponent — the quantity every win probability
    divides by and the one no other test touched.
    """
    weekly, positions = _deal_world()
    pool = grader.build_field_pool(weekly, positions, weeks=[1])
    seats = grader.field_lineups(pool, 1)

    mus = tuple(round(m, 4) for m, _v in seats)
    assert mus == (135.05, 133.2, 131.15, 129.35, 127.25, 125.5, 123.4, 121.6, 119.55)
    # the rivals carry real variance; dropping it (var += 0.0) reads as a field
    # of certainties and moved playoff odds 0.848 -> 0.962 on the live board.
    assert round(seats[0][1], 4) == 573.84
    assert all(v > 400.0 for _m, v in seats)


def test_the_dealt_field_matches_a_hand_derivation_slot_by_slot():
    """The same nine numbers, derived from the deal RULE instead of recorded.

    Seat 0 must get: the best QB, the best RB1 and the WORST RB2 (the snake turn
    inside the position), the same for WR, the best TE/D-ST/K, and the best FLEX
    leftover. Seat 8 must get the mirror. This is what 'alternating within a
    position' MEANS, and it fails under a straight deal, a reversed one, a
    missing FLEX round, or a mis-trimmed ladder.
    """
    weekly, positions = _deal_world()
    pool = grader.build_field_pool(weekly, positions, weeks=[1])
    seats = grader.field_lineups(pool, 1)
    pts = lambda pos, i: weekly[f"{pos}{i:03d}"][1]   # noqa: E731

    n = DEFAULT_ROSTER.teams - 1
    leftovers = sorted(
        [pts("RB", i) for i in range(2 * n, _DEAL_COUNTS["RB"])]
        + [pts("WR", i) for i in range(2 * n, _DEAL_COUNTS["WR"])]
        + [pts("TE", i) for i in range(n, _DEAL_COUNTS["TE"])],
        reverse=True,
    )
    seat0 = (pts("QB", 0) + pts("RB", 0) + pts("RB", 2 * n - 1)
             + pts("WR", 0) + pts("WR", 2 * n - 1)
             + pts("TE", 0) + pts("DST", 0) + pts("K", 0) + leftovers[0])
    seat8 = (pts("QB", n - 1) + pts("RB", n - 1) + pts("RB", n)
             + pts("WR", n - 1) + pts("WR", n)
             + pts("TE", n - 1) + pts("DST", n - 1) + pts("K", n - 1) + leftovers[n - 1])
    assert seats[0][0] == pytest.approx(seat0)
    assert seats[-1][0] == pytest.approx(seat8)
    # The FLEX is a real tenth starter, not decoration: without it every seat
    # loses its leftover and the whole field drops.
    assert seats[0][0] > seat0 - leftovers[0]


def test_the_ladder_keeps_the_BEST_players_not_the_worst():
    """``build_field_pool`` trims each ladder to the depth nine rivals plus one
    full roster could reach. Trimming the wrong END keeps the deepest bench in
    the league and throws the starters away — measured on the live board, the
    dealt field then scored 36.1 points a week and graded a real roster at 13.995
    expected wins of 14 while every other test stayed green.

    Deliberately built past the trim depth (D/ST and K reach ``1*9 + 16 + 2 =
    27``), because a fixture shallower than the trim never exercises it.
    """
    weekly, positions = {}, {}
    for pos, n in (("DST", 40), ("K", 40), ("QB", 40)):
        for i in range(n):
            pid = f"{pos}{i:03d}"
            weekly[pid] = {1: 30.0 - 0.5 * i}
            positions[pid] = pos
    pool = grader.build_field_pool(weekly, positions, weeks=[1])

    for pos in ("DST", "K", "QB"):
        ladder = pool.ladders[1][pos]
        depth = grader._pool_depth(pos, DEFAULT_ROSTER)
        assert len(ladder) == depth < 40, f"{pos} ladder was not trimmed"
        assert ladder[0][2] == f"{pos}000", f"{pos} ladder does not start at the best"
        assert ladder[-1][2] == f"{pos}{depth - 1:03d}", (
            f"{pos} ladder keeps the wrong end of the board"
        )
        assert [row[0] for row in ladder] == sorted(
            (row[0] for row in ladder), reverse=True
        )


def test_the_deal_alternates_inside_a_position_and_not_across_them():
    """The field's spread is what the playoff odds are most sensitive to, and the
    deal is a deliberate middle: alternating WITHIN a position (so RB1/RB2 offset
    each other) but restarting at each new position (so the one-deep slots stack
    on seat 0). Measured on the live 2026 board against the ten real rosters of a
    full engine-vs-bots draft, mean per-week standard deviation: real 7.47, this
    deal 6.72, alternation carried across positions 3.53, no alternation at all
    9.20. Both 'obvious fixes' are further from a real room than what ships.
    """
    weekly, positions = _deal_world()
    pool = grader.build_field_pool(weekly, positions, weeks=[1])
    mus = [m for m, _v in grader.field_lineups(pool, 1)]

    rb = {f"RB{i:03d}" for i in range(34)}
    ladder = [row for row in pool.ladders[1]["RB"]]
    n = DEFAULT_ROSTER.teams - 1
    # inside RB: seat 0 takes ladder[0] and ladder[2n-1]; seat 8 takes n-1 and n.
    assert ladder[0][2] in rb and ladder[2 * n - 1][2] in rb
    assert (ladder[0][0] + ladder[2 * n - 1][0]) == pytest.approx(
        ladder[n - 1][0] + ladder[n][0], abs=1e-9
    ), "an RB1+RB2 pair must be balanced by the turn"
    # across positions the alternation RESTARTS, so the field is ordered.
    assert mus == sorted(mus, reverse=True), (
        "the one-deep slots all deal forward; if this is no longer true the "
        "measured spread of the field has changed and the docstring is stale"
    )
    spread = (max(mus) - min(mus)) / (sum(mus) / len(mus))
    assert 0.05 < spread < 0.20, spread


def test_the_stream_tier_is_the_first_unrostered_player():
    """The waiver level is the ``teams``-th entry (0-indexed) of that week's
    ladder — the best K/DST that would still be free if all ten teams held one.
    Derived from the board, never a constant (Rule 2)."""
    weekly, positions = _deal_world()
    pool = grader.build_field_pool(weekly, positions, weeks=[1])
    assert pool.streams[1]["DST"][0] == pytest.approx(
        _DEAL_TOP["DST"] - _DEAL_STEP["DST"] * DEFAULT_ROSTER.teams
    )
    assert pool.streams[1]["K"][0] == pytest.approx(
        _DEAL_TOP["K"] - _DEAL_STEP["K"] * DEFAULT_ROSTER.teams
    )
    assert pool.streams[1]["DST"][2] == "DST010"


# ==================================================================
#   10. streamed slots: the two positions this league does not bench
# ==================================================================


def _stream_world():
    """A roster whose kicker byes in week 4 and whose defense byes in week 6,
    with a full board behind it so a waiver tier exists."""
    spec = [
        ("qb1", "QB", 18.0, None, "A"),
        ("rb1", "RB", 15.0, None, "B"),
        ("rb2", "RB", 13.0, None, "C"),
        ("wr1", "WR", 16.0, None, "D"),
        ("wr2", "WR", 14.0, None, "E"),
        ("wr3", "WR", 11.0, None, "F"),
        ("te1", "TE", 12.0, None, "G"),
        ("dst1", "DST", 9.0, 6, "H"),
        ("k1", "K", 8.5, 4, "I"),
    ]
    entries, weekly, positions = _roster_from(spec)
    weekly, positions = _with_a_board(weekly, positions)
    return entries, weekly, positions


def test_a_kicker_bye_is_streamed_not_scored_as_a_hole():
    """A one-deep K/DST bye is a Tuesday waiver add in this league, not an empty
    slot: 32 kickers, ten rosters. Grading it as a hole is what made a SECOND
    kicker the best available add on a full roster."""
    entries, weekly, positions = _stream_world()
    g = grader.grade_roster(entries, weekly, positions=positions)

    assert g.hole_weeks == (), g.hole_detail
    assert dict(g.streamed_slots)[4] and dict(g.streamed_slots)[6]
    assert [s for s, _p in dict(g.streamed_slots)[4]] == ["K"]
    assert [s for s, _p in dict(g.streamed_slots)[6]] == ["DST"]
    # the credited points are the waiver tier, not the player's own points
    streamed_k = dict(g.streamed_slots)[4][0][1]
    assert 0.0 < streamed_k < 8.5
    # ...and week 4 still scores BELOW a full week (a stream is worse than your K)
    assert g.weekly_means[3] < g.weekly_means[0]
    assert any("STREAMED" in r and "wk 4" in r for r in g.reasons)


def test_strict_mode_restores_the_hole_and_says_so():
    entries, weekly, positions = _stream_world()
    strict = grader.grade_roster(entries, weekly, positions=positions, stream_kdst=False)
    assert set(strict.hole_weeks) == {4, 6}
    assert strict.stream_state == "off"
    assert any("graded STRICTLY" in r for r in strict.reasons)
    # and strict really is worse: an empty slot scores nothing
    streamed = grader.grade_roster(entries, weekly, positions=positions)
    assert streamed.expected_wins > strict.expected_wins


def test_without_a_positions_map_the_strictness_is_disclosed_not_hidden():
    """No board -> no waiver tier can be priced. That is allowed (the roster is
    still gradable against real rivals) but it must not be silent, because it is
    the state in which a second defense looks like a good pick."""
    entries, weekly, _positions = _roster_from(_BALANCED)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    g = grader.grade_roster(entries, dict(weekly), opponent_rosters=opponents)
    assert g.stream_state == "unavailable"
    assert any("no positions map was available" in r for r in g.reasons)


def test_a_second_defense_and_a_second_kicker_are_worth_exactly_nothing():
    """THE pathology this fence exists for, as an assertion.

    Measured over 30 engine-vs-bots cells on the live 2026 board BEFORE the fence:
    a second D/ST was worth +0.040 expected wins and a second kicker +0.053,
    against +0.017 for the best free-agent RB and negative for every other skill
    add — the two of them top-ranked in 22 of the 30 cells. A draft optimiser
    maximising this objective would have spent real picks on them.

    Graded against FIXED rivals, because on the dealt-field path adding a player
    also removes him from the pool the rivals draw from, which is a second (real)
    effect that would hide this one.
    """
    entries, weekly, positions = _stream_world()
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    kw = {"opponent_rosters": opponents, "positions": positions}
    base = grader.grade_roster(entries, weekly, **kw)

    for pos, pid in (("DST", "fld-DST000"), ("K", "fld-K000")):
        doubled = [*entries, _entry(pid, pos, season_points=100.0, team="ZZZ")]
        g = grader.grade_roster(doubled, weekly, **kw)
        assert g.expected_wins == pytest.approx(base.expected_wins, abs=1e-12), (
            f"a second {pos} bought expected wins; the POSITION_CAPS fence is off"
        )
        assert any("NOT COUNTED AT ALL" in r and pid in r for r in g.reasons)

    # ...and the FIRST one is worth plenty (the cap is not a blanket zero).
    without = [e for e in entries if e.position != "DST"]
    assert grader.grade_roster(without, weekly, **kw).expected_wins < base.expected_wins


def test_the_cap_keeps_the_better_defense_not_the_first_one_listed():
    """Which of the two is benched is decided by the POINTS MAP over the graded
    weeks — the pricing spine — not by roster order or by a possibly-stale
    ``BoardEntry.house_points``."""
    entries, weekly, positions = _stream_world()
    weekly = dict(weekly)
    weekly["fld-DST000"] = _line(20.0, None)      # far better than dst1's 9.0/wk
    strong = _entry("fld-DST000", "DST", season_points=1.0, team="ZZZ")
    doubled = [*entries, strong]
    assert grader.capped_out(doubled, weekly, range(1, 18)) == frozenset({"dst1"})
    assert grader.capped_out(entries, weekly, range(1, 18)) == frozenset()

    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    kw = {"opponent_rosters": opponents, "positions": positions}
    assert grader.grade_roster(doubled, weekly, **kw).expected_wins > \
        grader.grade_roster(entries, weekly, **kw).expected_wins


def test_streaming_is_symmetric_so_the_league_still_sums_to_half_the_games():
    """The credit and the cap are applied to rivals exactly as to you. If they
    were applied only to the roster under test, ten teams' expected wins would
    stop summing to 70 — every team would be credited a stream nobody else got.

    ``positions=`` is what turns streaming ON here; without it the whole league
    would grade in the STRICT mode and this test would pass while proving
    nothing.
    """
    entries, weekly, positions = _field_world()
    rosters = _deal_league(entries)
    grades = []
    for t in sorted(rosters):
        others = {k: v for k, v in rosters.items() if k != t}
        grades.append(grader.grade_roster(
            rosters[t], weekly, opponent_rosters=others, positions=positions
        ))
    assert all(g.stream_state == "on" for g in grades), "streaming was not exercised"
    assert any(g.streamed_slots for g in grades), "no K/DST bye in the fixture"
    assert sum(g.expected_wins for g in grades) == pytest.approx(
        DEFAULT_ROSTER.teams * len(REG_WEEKS) / 2.0, abs=1e-6
    )
    # ...and the cap really did bench somebody's second defense somewhere.
    assert any(
        grader.capped_out(r, weekly, range(1, 18)) for r in rosters.values()
    ) or all(
        sum(1 for e in r if e.position == "DST") <= 1 for r in rosters.values()
    )


# ==================================================================
#   11. the id space under this module's OWN parameters
# ==================================================================


def test_publishing_fewer_weeks_does_not_move_the_id_space(tmp_path):
    """``weeks=range(1, 15)`` is the natural 'grade the regular season' call and
    it used to re-rank the board over 14 weeks while ``load_board`` had ranked it
    over 17 — measured on the live board: 167 entries silently fell out of the
    map, four of them priced (a kicker worth 107.1 house points), each then
    reported as 'NO PROJECTION AT ALL'. The publish span and the id-space span
    are now separate parameters."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    board = load_board(conn, as_of=AS_OF, season=SEASON)
    full = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    reg = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON, weeks=range(1, 15))
    conn.close()

    assert set(reg) == set(full) == {e.player_id for e in board}
    assert max(max(v) for v in reg.values() if v) == 14, "weeks= must still restrict"
    assert max(max(v) for v in full.values() if v) == 17
    # a rank-fallback id (the fragile branch) survived the narrower publish span
    assert any(":" in pid and not pid.startswith("DST:") for pid in reg)


def test_a_ranking_span_that_does_not_match_the_board_is_caught_by_the_board_gate(tmp_path):
    """``rank_weeks`` still CAN diverge — a caller may legitimately mirror a board
    built over a different span — so passing the board turns the silent drift into
    a refusal."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    board = load_board(conn, as_of=AS_OF, season=SEASON)
    grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON, board=board)  # matching: fine
    with pytest.raises(grader.GradeInputError, match="PRICED but missing"):
        # week 6 is a D/ST bye: that team has NO row, so it does not exist under
        # this ranking span at all and every rank behind it shifts.
        grader.weekly_points_map(
            conn, as_of=AS_OF, season=SEASON, rank_weeks=[6], board=board
        )
    conn.close()


def test_the_ranking_roster_and_denoise_flags_are_refused_off_default(tmp_path):
    """They exist only to mirror ``build_valuation``, which ``load_board`` always
    calls at its defaults, so a non-default value is provably a key-space
    divergence (measured on the live board: teams=12 drops 831 of 3,264 board
    entries, 8 of them priced; denoise_kdst=False drops 68)."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    with pytest.raises(grader.GradeInputError, match="must stay on the board's id space"):
        grader.weekly_points_map(
            conn, as_of=AS_OF, season=SEASON, roster=RosterStructure(teams=12)
        )
    with pytest.raises(grader.GradeInputError, match="must stay on the board's id space"):
        grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON, denoise_kdst=False)
    conn.close()


def test_board_coverage_gate_names_the_priced_players_it_lost():
    board = [
        _entry("kept", "WR", season_points=200.0),
        _entry("lost", "K", season_points=107.1),
        _entry("unpriced-union-entry", "TE", season_points=0.0),
    ]
    grader.assert_board_coverage(board, {"kept": {1: 10.0}, "lost": {1: 5.0}})
    with pytest.raises(grader.GradeInputError, match=r"lost \(K, 107.1 pts\)"):
        grader.assert_board_coverage(board, {"kept": {1: 10.0}})
    # an id that is PRESENT but carries no week at all is the same failure with
    # a different shape: he grades as a player who never plays.
    with pytest.raises(grader.GradeInputError, match=r"lost \(K, 107.1 pts\)"):
        grader.assert_board_coverage(board, {"kept": {1: 10.0}, "lost": {}})
    # ...while an UNPRICED union entry with no weeks is exactly what is expected.
    grader.assert_board_coverage(
        board, {"kept": {1: 10.0}, "lost": {1: 5.0}, "unpriced-union-entry": {}}
    )


def test_weekly_points_map_reads_the_historical_view_not_latest_truth(tmp_path):
    """Rule 1, the half the other leakage test cannot see.

    A bulk-history projection carries ``knowable_as_of`` = the week's first
    gameday but ``retrieved_as_of`` = the day it was pulled, so the RETRIEVAL
    gate is the binding one — and a forward-mode fixture (knowable == retrieved)
    can never tell the two views apart. Hard-coding ``view='latest_truth'`` inside
    ``weekly_points_map`` passed every other test in this file.
    """
    conn = _build_grader_db(tmp_path / "g.sqlite", retrieved="2026-08-20")
    conn.execute("UPDATE projections SET knowable_as_of = '2026-07-01'")
    conn.commit()

    # as_of sits AFTER the fact was knowable and BEFORE we retrieved it.
    assert grader.weekly_points_map(conn, as_of="2026-08-01", season=SEASON) == {}
    late = grader.weekly_points_map(
        conn, as_of="2026-08-01", season=SEASON, view="latest_truth"
    )
    assert late, "latest_truth must see the bulk-history rows the historical view hides"
    assert grader.weekly_points_map(conn, as_of="2026-08-20", season=SEASON)
    conn.close()


# ==================================================================
#   12. public behaviours that had no test at all
# ==================================================================


def test_opponent_means_are_the_rivals_score_not_your_own():
    """``opponent_means`` is rendered as the 'opp 118.3' column a human reads
    against 'you 125.8'. Nothing pinned it, so filling it with your OWN mean
    passed the whole suite."""
    entries, weekly, _positions = _roster_from(_BALANCED)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    g = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    for wk in REG_WEEKS:
        assert g.opponent_means[wk - 1] == pytest.approx(100.0)
        assert g.opponent_means[wk - 1] != pytest.approx(g.weekly_means[wk - 1])
    assert "opp  100.0" in grader.format_season_grade(g)


def test_an_explicit_positions_argument_beats_the_maps_own():
    """Documented precedence: ``positions=`` is the explicit escape hatch and
    wins over ``weekly.positions``. Untested, so inverting it passed."""
    entries, weekly, positions = _roster_from(_BALANCED)
    weekly, positions = _with_a_board(weekly, positions)
    rich = grader.WeeklyPointsMap(
        weekly,
        positions={pid: "K" for pid in positions},   # a deliberately wrong map
        names={pid: pid for pid in positions},
        teams={pid: None for pid in positions},
    )
    good = grader.grade_roster(entries, rich, positions=positions)
    bad = grader.grade_roster(entries, rich)      # falls back to the wrong map
    assert good.expected_wins != pytest.approx(bad.expected_wins)
    # the wrong map cannot field anything but kickers, so the field collapses
    assert good.opponent_means[0] > bad.opponent_means[0]


def test_real_rivals_beat_a_field_pool_when_both_are_given():
    """The documented rule, and the obvious mid-draft call: a rollout holding a
    cached FieldPool for speed plus the real ``PickContext.opponent_rosters``."""
    entries, weekly, positions = _roster_from(_BALANCED)
    weekly, positions = _with_a_board(weekly, positions)
    opponents = _flat_opponent(entries, weekly, per_week_total=60.0)   # very weak
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 18))

    both = grader.grade_roster(entries, weekly, opponent_rosters=opponents, field=pool)
    real_only = grader.grade_roster(
        entries, weekly, opponent_rosters=opponents, positions=positions
    )
    field_only = grader.grade_roster(entries, weekly, field=pool)

    assert both.expected_wins == pytest.approx(real_only.expected_wins)
    assert both.expected_wins != pytest.approx(field_only.expected_wins)
    assert any("your 9 real rivals" in r for r in both.reasons)


def test_the_unmeasured_priors_are_named_in_the_text_a_human_reads():
    """Rule 6. The kicker sigma is a pure hypothesis and the QB/pass-catcher
    correlation is unmeasured; both change the answer, and both used to ride
    under one reason line whose cohort string describes only the MEASURED fit."""
    entries, weekly, _positions = _roster_from(_BALANCED)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    text = " ".join(grader.grade_roster(entries, weekly, opponent_rosters=opponents).reasons)
    assert "kicker sigma" in text and "3.5" in text
    assert "correlation rho=+0.35" in text
    assert "NOT measured" in text or "not part of the fit" in text
    assert DEFAULT_VARIANCE.source in text


def test_the_bench_and_field_limitations_are_disclosed_on_every_grade():
    entries, weekly, positions = _roster_from(_BALANCED)
    weekly, positions = _with_a_board(weekly, positions)
    synthetic = grader.grade_roster(entries, weekly, positions=positions)
    assert any(grader.BENCH_LIMITATION_LABEL in r for r in synthetic.reasons)
    assert any("read the" in r and "FLOOR" in r for r in synthetic.reasons)

    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    real = grader.grade_roster(entries, weekly, opponent_rosters=opponents)
    assert any(grader.BENCH_LIMITATION_LABEL in r for r in real.reasons)
    assert not any("FLOOR" in r for r in real.reasons), (
        "the synthetic-field caveat must not be printed when real rivals were used"
    )


def test_a_bench_skill_player_is_worth_exactly_zero_and_the_reasons_say_so():
    """The limitation, measured as an assertion rather than asserted as prose.

    Each added player is a hair BELOW the man in front of him — a backup QB
    scoring 17.9 behind a starter on 18.0, a bench RB/WR/TE one tenth of a point
    below the FLEX incumbent. In a real season those are the players who win you
    weeks 6 through 17, and every one of them is worth exactly +0.000 here,
    because nothing in this model can make the man in front of them disappear.
    That is also why the K/DST cap matters: against 'exactly zero', the sliver a
    backup defense earns from a bye week wins a round-16 pick outright.
    """
    entries, weekly, positions = _stream_world()
    weekly = dict(weekly)
    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    kw = {"opponent_rosters": opponents, "positions": positions}
    base = grader.grade_roster(entries, weekly, **kw)

    for pos, pts in (("QB", 17.9), ("RB", 10.9), ("WR", 10.9), ("TE", 10.9)):
        pid = f"bench-{pos}"
        weekly[pid] = _line(pts, None)
        positions[pid] = pos
        deeper = [*entries, _entry(pid, pos, season_points=pts * 17, team="ZZZ")]
        g = grader.grade_roster(deeper, weekly, **kw)
        assert g.expected_wins == pytest.approx(base.expected_wins, abs=1e-12), pos
        assert all(pid not in fill.starters for fill in g.lineups.values())
    assert any(grader.BENCH_LIMITATION_LABEL in r for r in base.reasons)

    # ...and on the DEALT-field path it is not quite zero, because the player you
    # add is a player the modelled rivals no longer get. That is a real effect,
    # and it is why the measurement above fixes the opponent.
    dealt_base = grader.grade_roster(entries, weekly, positions=positions)
    dealt = grader.grade_roster(
        [*entries, _entry("bench-QB", "QB", season_points=300.0, team="ZZZ")],
        weekly, positions=positions,
    )
    assert dealt.expected_wins > dealt_base.expected_wins


def test_the_stream_table_is_the_same_whether_or_not_a_field_was_dealt():
    """The opponent-rosters path needs the waiver tier but not the dealt field,
    so it builds the two ladders directly (1.25 ms on the live board against 5.34
    ms for a whole pool, and this runs inside a rollout). The two routes must
    agree exactly, or a roster would grade differently depending on which
    opponent model it was handed."""
    entries, weekly, positions = _stream_world()
    pool = grader.build_field_pool(weekly, positions, weeks=range(1, 18))
    cheap = grader.stream_levels_from_board(weekly, positions, weeks=range(1, 18))
    assert cheap == dict(pool.streams)
    assert cheap[4]["K"] and cheap[4]["DST"]

    opponents = _flat_opponent(entries, weekly, per_week_total=100.0)
    from_pool = grader.grade_roster(
        entries, weekly, opponent_rosters=opponents, field=pool
    )
    from_board = grader.grade_roster(
        entries, weekly, opponent_rosters=opponents, positions=positions
    )
    assert from_pool.expected_wins == pytest.approx(from_board.expected_wins)
    assert from_pool.streamed_slots == from_board.streamed_slots


def test_the_stream_table_is_built_once_per_map_too(tmp_path, monkeypatch):
    """Same rollout budget argument as the field pool: 1.25 ms per call would be
    half the cost of a real-rivals grade."""
    conn = _build_grader_db(tmp_path / "g.sqlite")
    weekly = grader.weekly_points_map(conn, as_of=AS_OF, season=SEASON)
    conn.close()
    mine = [
        _entry(pid, weekly.positions[pid], team=weekly.teams[pid])
        for pid in list(weekly)[:6]
    ]
    rivals = {t: mine for t in range(DEFAULT_ROSTER.teams - 1)}

    calls = []
    real = grader.stream_levels_from_board
    monkeypatch.setattr(
        grader, "stream_levels_from_board",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )
    for _ in range(4):
        grader.grade_roster(mine, weekly, opponent_rosters=rivals)
    assert len(calls) == 1, f"the stream table was rebuilt {len(calls)} times"


def test_the_streamed_slot_model_follows_a_non_default_roster_shape():
    """A league that starts TWO defenses labels its slots 'DST1'/'DST2', and a
    stream keyed on the bare position would silently stop working there while the
    cap kept benching the second defense — the worst of both halves. Both halves
    read the roster instead."""
    entries, weekly, positions = _stream_world()
    two_dst = RosterStructure(starters={**DEFAULT_ROSTER.starters, "DST": 2})
    second = _entry("fld-DST000", "DST", season_points=100.0, team="ZZZ")
    doubled = [*entries, second]

    assert grader.capped_out(doubled, weekly, range(1, 18)) == frozenset({"fld-DST000"})
    assert grader.capped_out(doubled, weekly, range(1, 18), roster=two_dst) == frozenset()

    g = grader.grade_roster(doubled, weekly, positions=positions, roster=two_dst)
    # week 6 is dst1's bye: the DST2 slot is filled by the second defense and the
    # slot the bye emptied is STREAMED, not left as a hole.
    assert 6 not in g.hole_weeks, g.hole_detail
    assert [s for s, _p in dict(g.streamed_slots)[6]] == ["DST2"]
