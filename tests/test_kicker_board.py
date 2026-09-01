"""Corrected kicker board tests (item 3.10) — `ziggurat/core/kicker_board.py`.

Offline: every row is inserted directly, so nothing here touches the network and
no ESPN or Sleeper fixture is required. The ESPN ingest path has its own tests in
``tests/test_projection_ensemble.py``; this file is about what the board DOES
with stored rows, and it is built so that each test states the situation it is
about instead of hoping the live data contains one.

THE ONE THING EVERY TEST HERE GUARDS. The current board understates every kicker
because ``projections._KICKER_DIRECT_MAP`` maps a source key the Sleeper feed
does not serve (see ``projections.unbucketed_fg_makes``). The correction is
therefore only useful if it is (a) opt-in, (b) horizon-aligned, and (c) LOUD
when it cannot be applied — a corrected board that silently serves uncorrected
numbers is worse than no correction at all, because a caller then believes a
number nobody checked.
"""

import pytest

from ziggurat.core import kicker_board as kbm
from ziggurat.core import scoring, valuation
from ziggurat.data.nfl import base

SEASON = 2026
DAY = "2026-08-30"
WEEKS = range(1, 18)

#: ESPN columns a synthetic kicker row needs. Deliberately spelled out rather
#: than reflected off the table, so a migration that renames one fails here.
_ESPN_COLS = (
    "source", "espn_key", "espn_id", "gsis_id", "player", "position", "team",
    "season", "week", "projected_games", "espn_applied_total",
    "fg_made_0_39", "fg_made_40_49", "fg_made_50_59", "fg_made_60",
    "pat_made", "fg_missed", "retrieved_as_of", "knowable_as_of",
)


def _house_kicker(db, *, espn_id, gsis_id, sleeper_id, name, team,
                  fg_0_39, fg_40_49, pat, missed, bye=8, weeks=WEEKS,
                  retrieved=DAY, season=SEASON):
    """A Sleeper-shaped weekly kicker, priced the way the LIVE feed prices one.

    Note what is NOT here: ``fg_made_50_59``. That is the defect — the feed
    serves no 50+ bucket, so those makes are stored as nothing while
    ``fg_missed`` still charges every miss. The bye week is a row that IS
    PRESENT with a NULL opponent and NULL stats (the live shape, item 3.2).
    """
    db.execute(
        "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, retrieved_as_of, "
        "knowable_as_of) VALUES (?, ?, ?, ?, ?, ?)",
        (gsis_id, sleeper_id, espn_id, name, retrieved, retrieved),
    )
    for week in weeks:
        blank = week == bye
        cols = {
            "source": "sleeper_rotowire", "source_player_id": sleeper_id,
            "gsis_id": gsis_id, "season": season, "week": week,
            "season_type": "regular", "position": "K", "team": team,
            "opponent": None if blank else "OPP",
            "retrieved_as_of": retrieved, "knowable_as_of": retrieved,
        }
        if not blank:
            cols.update({"fg_made_0_39": fg_0_39, "fg_made_40_49": fg_40_49,
                         "pat_made": pat, "fg_missed": missed})
        db.execute(
            f"INSERT INTO projections ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            tuple(cols.values()),
        )
    db.commit()


def _espn_kicker(db, *, espn_id, gsis_id, name, team, fg_0_39, fg_40_49,
                 fg_50_59, pat, missed, games=17.0, retrieved=DAY,
                 knowable=None, season=SEASON, week=0):
    """A stored ESPN whole-season kicker projection.

    ``espn_applied_total`` is ESPN's own number and is NEVER a scoring input
    anywhere in this repo; it is written so a row looks like the real thing, and
    no assertion below reads it.
    """
    stats = {"fg_made_0_39": fg_0_39, "fg_made_40_49": fg_40_49,
             "fg_made_50_59": fg_50_59, "fg_made_60": 0.0,
             "pat_made": pat, "fg_missed": missed}
    total = scoring.score("K", stats)
    row = {
        "source": "espn", "espn_key": str(espn_id), "espn_id": str(espn_id),
        "gsis_id": gsis_id, "player": name, "position": "K", "team": team,
        "season": season, "week": week, "projected_games": games,
        "espn_applied_total": total, **stats,
        "retrieved_as_of": retrieved, "knowable_as_of": knowable or retrieved,
    }
    assert set(row) == set(_ESPN_COLS), "synthetic ESPN row shape drifted"
    db.execute(
        f"INSERT INTO espn_projections ({', '.join(row)}) "
        f"VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    db.commit()
    return total


#: The synthetic pool, ordered as the CURRENT (broken) board ranks it.
#:
#: ``long`` is the season 50+ makes ESPN publishes and the Sleeper feed drops.
#: The numbers are invented, but the SHAPE is the live one and it is the shape
#: that matters: the long-make share is NOT constant across kickers (live 2026:
#: 8.8% to 25.8% of a kicker's makes), so the correction REORDERS the board
#: rather than scaling it. Here "Alpha" is the live board's Aubrey — 5th on the
#: broken board, 1st once his long makes are priced.
#:
#: ``long=None`` means ESPN publishes no line for him at all: he stays
#: UNCORRECTED, and he is deliberately parked LAST so a test can put him inside
#: or outside the replacement-level window by changing the league size.
_POOL = (
    # name,   team,  fg_0_39, fg_40_49, pat,  missed, long
    ("K01 Kicker", "LAC", 1.30, 0.50, 3.00, 0.30, 4.0),
    ("K02 Kicker", "HOU", 1.25, 0.50, 2.95, 0.30, 4.2),
    ("K03 Kicker", "SEA", 1.20, 0.50, 2.90, 0.30, 4.4),
    ("K04 Kicker", "LA",  1.15, 0.50, 2.85, 0.30, 4.6),
    ("Alpha Kicker", "DAL", 1.10, 0.50, 2.80, 0.30, 12.0),   # the reorder case
    ("K06 Kicker", "BAL", 1.05, 0.50, 2.75, 0.30, 5.0),
    ("K07 Kicker", "DET", 1.00, 0.50, 2.70, 0.30, 5.2),
    ("K08 Kicker", "KC",  0.95, 0.50, 2.65, 0.30, 5.4),
    ("K09 Kicker", "SF",  0.90, 0.50, 2.60, 0.30, 5.6),
    ("K10 Kicker", "CHI", 0.85, 0.50, 2.55, 0.30, 5.8),
    ("K11 Kicker", "TB",  0.80, 0.50, 2.50, 0.30, 6.0),
    ("K12 Kicker", "CLE", 0.78, 0.50, 2.44, 0.30, 6.1),
    ("K13 Kicker", "NYJ", 0.76, 0.50, 2.42, 0.30, 6.2),
    ("Gamma Kicker", "NYG", 0.70, 0.45, 2.30, 0.30, None),   # no ESPN line
)


#: The reorder case's identifiers and ESPN season line, derived from _POOL so a
#: test never re-types a number the fixture already owns.
ALPHA_INDEX = next(i for i, row in enumerate(_POOL, start=1) if row[0] == "Alpha Kicker")
ALPHA_ESPN_ID = f"90{ALPHA_INDEX:02d}"
ALPHA_GSIS_ID = f"00-00000{ALPHA_INDEX:02d}"
ALPHA_ESPN_LINE = {
    "fg_made_0_39": _POOL[ALPHA_INDEX - 1][2] * 17,
    "fg_made_40_49": _POOL[ALPHA_INDEX - 1][3] * 17,
    "fg_made_50_59": _POOL[ALPHA_INDEX - 1][6],
    "fg_made_60": 0.0,
    "pat_made": _POOL[ALPHA_INDEX - 1][4] * 17,
    "fg_missed": _POOL[ALPHA_INDEX - 1][5] * 17,
}


@pytest.fixture()
def world(db):
    """Fourteen kickers priced the way the LIVE feed prices them, plus ESPN lines.

    Deep enough that the default 10-team roster's replacement window (K10-K12)
    lands inside the pool rather than on a thin-board clamp — that window is
    what sets every K VOR, and a fixture that never reaches it cannot test the
    thing that matters. The ONE uncorrected kicker is parked below it, so the
    default board is servable; a test that wants the mixture INSIDE the window
    (which is a refusal, not a warning) moves the window with a bigger league
    rather than by editing the pool.
    """
    for i, (name, team, lo, mid, pat, missed, long) in enumerate(_POOL, start=1):
        espn_id, gsis_id = f"90{i:02d}", f"00-00000{i:02d}"
        _house_kicker(db, espn_id=espn_id, gsis_id=gsis_id, sleeper_id=str(i),
                      name=name, team=team, fg_0_39=lo, fg_40_49=mid,
                      pat=pat, missed=missed)
        if long is None:
            continue
        # ESPN's season line: the same 17-game volume the feed forecasts, plus
        # the long makes the feed never bucketed.
        _espn_kicker(db, espn_id=espn_id, gsis_id=gsis_id, name=name, team=team,
                     fg_0_39=lo * 17, fg_40_49=mid * 17, fg_50_59=long,
                     pat=pat * 17, missed=missed * 17)
    return db


def _board(db, **kw):
    return kbm.build_kicker_board(db, as_of=DAY, season=SEASON, **kw)


def _line(board, name):
    return next(ln for ln in board.lines if ln.player == name)


# =========================================================================
# RULE 1 — the as-of gate
# =========================================================================


def test_nothing_is_visible_before_the_corrected_source_was_retrieved(db):
    """Leakage: the ESPN pull is stamped 2026-08-30, so a read one day earlier
    must not see it — and must REFUSE rather than quietly serve the broken
    numbers under a corrected label. The HOUSE side is stamped a week earlier,
    so the only thing gated out here is the correction."""
    _house_kicker(db, espn_id="9001", gsis_id="00-0000001", sleeper_id="1",
                  name="Alpha Kicker", team="DAL", fg_0_39=1.0, fg_40_49=0.5,
                  pat=3.0, missed=0.3, retrieved="2026-08-20")
    _espn_kicker(db, espn_id="9001", gsis_id="00-0000001", name="Alpha Kicker",
                 team="DAL", fg_0_39=17.0, fg_40_49=8.5, fg_50_59=8.0,
                 pat=51.0, missed=5.1)
    assert kbm.build_kicker_board(db, as_of=DAY, season=SEASON).corrected_count == 1
    with pytest.raises(kbm.KickerBoardUnavailable) as exc:
        kbm.build_kicker_board(db, as_of="2026-08-29", season=SEASON)
    assert "espn_projections" in str(exc.value)


def test_a_later_retrieval_is_invisible_at_an_earlier_as_of(world):
    """A SECOND pull, stamped later, must not reach back: at the original as_of
    the board must still price the original snapshot. Retrieval time is gated,
    not just knowledge time (the two-clause `historical` view)."""
    before = _line(_board(world), "Alpha Kicker").season_points
    _espn_kicker(world, espn_id=ALPHA_ESPN_ID, gsis_id=ALPHA_GSIS_ID,
                 name="Alpha Kicker", team="DAL",
                 fg_0_39=ALPHA_ESPN_LINE["fg_made_0_39"],
                 fg_40_49=ALPHA_ESPN_LINE["fg_made_40_49"], fg_50_59=99.0,
                 pat=ALPHA_ESPN_LINE["pat_made"],
                 missed=ALPHA_ESPN_LINE["fg_missed"], retrieved="2026-09-05")
    assert _line(_board(world), "Alpha Kicker").season_points == pytest.approx(before)
    later = kbm.build_kicker_board(world, as_of="2026-09-05", season=SEASON)
    assert _line(later, "Alpha Kicker").season_points > before


def test_latest_truth_is_an_explicit_opt_in(world):
    """The view threads through; it is never widened by this layer.

    The correction below was KNOWABLE on the as_of day but RETRIEVED after it —
    the exact shape ``historical`` gates on retrieval time for and the exact
    shape ``latest_truth`` exists to let through on purpose.
    """
    _espn_kicker(world, espn_id=ALPHA_ESPN_ID, gsis_id=ALPHA_GSIS_ID,
                 name="Alpha Kicker", team="DAL",
                 fg_0_39=ALPHA_ESPN_LINE["fg_made_0_39"],
                 fg_40_49=ALPHA_ESPN_LINE["fg_made_40_49"], fg_50_59=99.0,
                 pat=ALPHA_ESPN_LINE["pat_made"],
                 missed=ALPHA_ESPN_LINE["fg_missed"],
                 retrieved="2026-09-05", knowable=DAY)
    gated = _line(_board(world), "Alpha Kicker").season_points
    open_ = _line(_board(world, view="latest_truth"), "Alpha Kicker").season_points
    assert open_ > gated


def test_as_of_is_keyword_only_and_has_no_default():
    with pytest.raises(TypeError):
        kbm.build_kicker_board(None, season=SEASON)  # type: ignore[call-arg]


# =========================================================================
# REFUSE RATHER THAN GUESS
# =========================================================================


def test_an_empty_corrected_source_refuses_instead_of_serving_the_broken_board(db):
    _house_kicker(db, espn_id="9001", gsis_id="00-0000001", sleeper_id="1",
                  name="Alpha Kicker", team="DAL",
                  fg_0_39=1.0, fg_40_49=0.5, pat=3.0, missed=0.3)
    with pytest.raises(kbm.KickerBoardUnavailable):
        _board(db)


def test_an_empty_house_board_refuses(db):
    _espn_kicker(db, espn_id="9001", gsis_id="00-0000001", name="Alpha Kicker",
                 team="DAL", fg_0_39=17.0, fg_40_49=8.5, fg_50_59=8.0,
                 pat=51.0, missed=5.1)
    with pytest.raises(kbm.KickerBoardUnavailable):
        _board(db)


def test_an_unknown_source_raises(world):
    with pytest.raises(ValueError):
        _board(world, source="rotowire-but-fixed")


# =========================================================================
# THE CORRECTION ITSELF
# =========================================================================


def test_the_correction_adds_the_missing_long_makes_and_reorders_the_board(world):
    board = _board(world)
    alpha, k01 = _line(board, "Alpha Kicker"), _line(board, "K01 Kicker")

    # The defect's signature: on the CURRENT board four kickers outscore Alpha
    # (they kick more short field goals); once the 50+ makes are priced he is K1.
    assert k01.current_points > alpha.current_points
    current_order = sorted(board.lines, key=lambda ln: -ln.current_points)
    assert [ln.player for ln in current_order[:5]][-1] == "Alpha Kicker"
    assert board.lines[0].player == "Alpha Kicker"
    # Every priced kicker is understated by the current board — that is the defect.
    assert all(ln.delta > 0 for ln in board.lines if ln.corrected)


def test_the_corrected_total_is_the_espn_line_scaled_to_the_played_weeks(world):
    """The horizon alignment, stated as arithmetic rather than trusted.

    ESPN projects SEVENTEEN games. Weeks 1-17 contain only SIXTEEN games for a
    kicker with a bye. Substituting ESPN's total unscaled overstates by ~6%.
    """
    board = _board(world)
    alpha = _line(board, "Alpha Kicker")
    espn_total = scoring.score("K", ALPHA_ESPN_LINE)
    assert alpha.played_weeks == frozenset(set(WEEKS) - {8})
    assert alpha.projected_games == 17.0
    assert alpha.season_points == pytest.approx(espn_total * 16 / 17)
    # ...and NOT the unscaled total, which is what a naive substitution gives.
    assert alpha.season_points < espn_total


def test_a_kicker_with_no_corrected_row_is_labelled_not_silently_corrected(world):
    gamma = _line(_board(world), "Gamma Kicker")   # the one with no ESPN line
    assert gamma.basis == kbm.BASIS_UNCORRECTED
    assert not gamma.corrected
    assert gamma.season_points == gamma.current_points
    assert any("UNCORRECTED" in r for r in gamma.reasons)
    assert any("50+" in r for r in gamma.reasons)


def test_a_kicker_with_no_projected_games_is_left_uncorrected(db):
    """No horizon, no scaling — and the row says which of the four it was.

    ``allow_partial`` because a one-kicker board whose only kicker is
    uncorrected IS a mixed replacement window, which is a refusal by default.
    """
    _house_kicker(db, espn_id="9001", gsis_id="00-0000001", sleeper_id="1",
                  name="Alpha Kicker", team="DAL",
                  fg_0_39=1.0, fg_40_49=0.5, pat=3.0, missed=0.3)
    _espn_kicker(db, espn_id="9001", gsis_id="00-0000001", name="Alpha Kicker",
                 team="DAL", fg_0_39=17.0, fg_40_49=8.5, fg_50_59=8.0,
                 pat=51.0, missed=5.1, games=None)
    line = _line(_board(db, allow_partial=True), "Alpha Kicker")
    assert line.basis == kbm.BASIS_UNCORRECTED
    assert any("projected games" in r for r in line.reasons)


def test_the_weekly_split_sums_to_the_corrected_season_total_and_keeps_the_bye_absent(world):
    alpha = _line(_board(world), "Alpha Kicker")
    assert sum(alpha.weeks.values()) == pytest.approx(alpha.season_points)
    assert 8 not in alpha.weeks, "the bye must stay ABSENT, never a zero"
    assert set(alpha.weeks) == set(alpha.played_weeks)


def test_the_weekly_split_follows_the_feeds_own_week_shape(db):
    """A kicker the feed forecasts unevenly keeps that shape after correction —
    the correction re-levels a season, it does not flatten a week profile."""
    _house_kicker(db, espn_id="9001", gsis_id="00-0000001", sleeper_id="1",
                  name="Alpha Kicker", team="DAL",
                  fg_0_39=1.0, fg_40_49=0.5, pat=3.0, missed=0.3, weeks=[1, 2])
    db.execute("UPDATE projections SET pat_made = 9.0 WHERE week = 2")
    db.commit()
    _espn_kicker(db, espn_id="9001", gsis_id="00-0000001", name="Alpha Kicker",
                 team="DAL", fg_0_39=17.0, fg_40_49=8.5, fg_50_59=8.0,
                 pat=51.0, missed=5.1, games=2.0)
    line = _line(kbm.build_kicker_board(db, as_of=DAY, season=SEASON, weeks=[1, 2]),
                 "Alpha Kicker")
    w1, w2 = line.weeks[1], line.weeks[2]
    current = valuation.weekly_lines(db, as_of=DAY, season=SEASON, weeks=[1, 2])
    raw = next(v for v in current.values() if v.player == "Alpha Kicker")
    assert w2 / w1 == pytest.approx(raw.points[2] / raw.points[1])


def test_every_point_comes_from_scoring_py(world):
    """Rule 2, tested by moving a house rule rather than by grepping for digits:
    change what a PAT is worth and the corrected total must move with it."""
    baseline = _line(_board(world), "Alpha Kicker").season_points
    doubled = scoring.HOUSE_RULES.__class__(
        **{**scoring.HOUSE_RULES.__dict__,
           "points_per_pat_made": scoring.HOUSE_RULES.points_per_pat_made + 1.0}
    )
    moved = _line(_board(world, rules=doubled), "Alpha Kicker").season_points
    assert moved > baseline


def test_reasons_are_present_and_name_the_source(world):
    """Rule 6: the operator cannot smell an absurd kicker total, so every
    corrected row must say where its number came from."""
    for line in _board(world).lines:
        assert line.reasons and all(isinstance(r, str) and r for r in line.reasons)
    alpha = _line(_board(world), "Alpha Kicker")
    assert any("ESPN" in r for r in alpha.reasons)
    assert any("17 projected games" in r or "17 projected" in r for r in alpha.reasons)


def test_the_board_discloses_the_mixture_and_the_unmatched_rows(world):
    board = _board(world)
    assert board.corrected_count == len(_POOL) - 1 and board.uncorrected_count == 1
    assert any("uncorrected" in r for r in board.reasons)
    assert any("can never rise above" in r for r in board.reasons)


def test_an_espn_kicker_the_house_board_does_not_carry_is_reported_not_added(world):
    _espn_kicker(world, espn_id="9099", gsis_id="00-0000099", name="Delta Kicker",
                 team="GB", fg_0_39=22.0, fg_40_49=9.0, fg_50_59=9.0,
                 pat=55.0, missed=4.0)
    board = _board(world)
    assert "9099" in board.unmatched_espn
    assert all(ln.player != "Delta Kicker" for ln in board.lines)
    assert any("were not applied to any house row" in r for r in board.reasons)
    # ...and the report names WHO, and what the likely cause was. "GB" carries no
    # house kicker at all here, so the cause is neither guessed nor invented.
    assert any("Delta Kicker" in d for d in board.unmatched_detail)


def test_a_mixed_replacement_window_refuses_rather_than_warning(world):
    """Twelve teams puts the window on K12-K14, where the uncorrected kicker is.

    An uncorrected kicker there holds the replacement level DOWN (the correction
    only adds points), which inflates every K VOR — the number the K/DST
    divergence play is priced off. It used to be a ``reasons`` entry that no
    shipped surface prints: ``BoardEntry`` has no reasons field, no CLI renders
    ``KickerBoard``, and ``format_valuation`` never prints ``ValuationRow``
    reasons. An invisible warning is not a warning, so this refuses.
    """
    with pytest.raises(kbm.KickerBoardDegraded) as exc:
        _board(world, roster=valuation.RosterStructure(teams=12))
    assert "Gamma Kicker" in str(exc.value)
    assert "allow_partial" in str(exc.value)


def test_allow_partial_serves_the_mixed_window_with_the_caution_attached(world):
    """The escape hatch is real, and it is loud: the mixture is stated on the
    board AND carried onto every K row by the valuation seam."""
    board = _board(world, roster=valuation.RosterStructure(teams=12),
                   allow_partial=True)
    assert board.replacement_is_mixed is True
    assert any("replacement-level rank window" in r for r in board.reasons)
    assert any("allow_partial=True" in r for r in board.reasons)

    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON,
                                     roster=valuation.RosterStructure(teams=12))
    new = kbm.apply_to_valuation(rows, board,
                                 roster=valuation.RosterStructure(teams=12))
    alpha = next(r for r in new if r.player == "Alpha Kicker")
    assert any(r.startswith("CAUTION") and "replacement-level" in r
               for r in alpha.reasons)


def test_a_fully_corrected_window_is_not_flagged(world):
    """The SAME board at the real ten teams: the window is K10-K12, entirely
    corrected, and the caution must NOT fire. A warning that fires whatever the
    data says teaches the operator to ignore it."""
    board = _board(world)
    assert board.replacement_is_mixed is False
    assert not any("replacement-level rank window" in r for r in board.reasons)


# =========================================================================
# THE VALUATION SEAM
# =========================================================================


def test_apply_to_valuation_moves_the_kickers_and_nothing_else(world):
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    board = _board(world)
    new = kbm.apply_to_valuation(rows, board)

    before = {(r.position, r.player): r for r in rows}
    for r in new:
        old = before[(r.position, r.player)]
        if r.position == "K" and any(
            ln.player == r.player and ln.corrected for ln in board.lines
        ):
            assert r.proj_points > old.proj_points
        else:
            assert r.proj_points == pytest.approx(old.proj_points)


def test_apply_to_valuation_moves_the_replacement_level_and_the_vor(world):
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    new = kbm.apply_to_valuation(rows, _board(world))
    # Twelve kickers and ten teams: the K10-K12 window is real, not a clamp.
    old_k = [r for r in rows if r.position == "K"]
    new_k = [r for r in new if r.position == "K"]
    assert new_k[0].replacement_points > old_k[0].replacement_points
    # VOR is recomputed against the NEW baseline, never left on the old one.
    for r in new_k:
        assert r.vor == pytest.approx(r.proj_points - r.replacement_points)


def test_apply_to_valuation_reranks_so_the_corrected_leader_is_k1(world):
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    new = kbm.apply_to_valuation(rows, _board(world))
    assert next(r for r in new if r.pos_rank == 1 and r.position == "K").player \
        == "Alpha Kicker"
    assert next(r for r in rows if r.pos_rank == 1 and r.position == "K").player \
        == "K01 Kicker"
    # ranks stay a consistent 1..N ordering by VOR
    ordered = sorted(new, key=lambda r: r.overall_rank)
    assert [r.overall_rank for r in ordered] == list(range(1, len(ordered) + 1))
    assert all(a.vor >= b.vor for a, b in zip(ordered, ordered[1:], strict=False))


def test_apply_to_valuation_reasons_disclose_the_correction(world):
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    new = kbm.apply_to_valuation(rows, _board(world))
    alpha = next(r for r in new if r.player == "Alpha Kicker")
    gamma = next(r for r in new if r.player == "Gamma Kicker")
    assert any("ESPN" in r for r in alpha.reasons)
    assert any("UNCORRECTED" in r for r in gamma.reasons)


def test_apply_to_valuation_is_a_no_op_when_nothing_matched(world):
    """A board whose corrected rows join no valuation row must leave the input
    numerically alone — the failure mode to catch is a "correction" that quietly
    re-prices everything to itself."""
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    board = _board(world)
    empty = kbm.KickerBoard(
        season=SEASON, as_of=DAY, source=kbm.SOURCE_ESPN, lines=(),
        corrected_count=0, uncorrected_count=0, unmatched_espn=(),
        replacement_is_mixed=False, reasons=board.reasons,
    )
    new = kbm.apply_to_valuation(rows, empty)
    for a, b in zip(sorted(rows, key=lambda r: (r.position, str(r.player))),
                    sorted(new, key=lambda r: (r.position, str(r.player))),
                    strict=True):
        assert a.proj_points == pytest.approx(b.proj_points)
        assert a.vor == pytest.approx(b.vor)


# =========================================================================
# THE GRADING SEAM
# =========================================================================


def test_apply_to_weekly_points_replaces_only_the_corrected_kickers(world):
    board = _board(world)
    points = valuation.weekly_points(world, as_of=DAY, season=SEASON)
    out = kbm.apply_to_weekly_points(points, board)
    for line in board.lines:
        if line.corrected:
            assert out[line.key] == pytest.approx(dict(line.weeks))
            assert sum(out[line.key].values()) > sum(points[line.key].values())
        else:
            assert out[line.key] == pytest.approx(dict(points[line.key]))


def test_apply_to_weekly_points_does_not_mutate_its_input(world):
    board = _board(world)
    points = valuation.weekly_points(world, as_of=DAY, season=SEASON)
    snapshot = {k: dict(v) for k, v in points.items()}
    kbm.apply_to_weekly_points(points, board)
    assert points == snapshot


def test_apply_to_weekly_points_keeps_the_bye_absent(world):
    board = _board(world)
    out = kbm.apply_to_weekly_points(
        valuation.weekly_points(world, as_of=DAY, season=SEASON), board)
    alpha = _line(board, "Alpha Kicker")
    assert 8 not in out[alpha.key]


# =========================================================================
# FORMATTING
# =========================================================================


def test_format_shows_both_numbers_side_by_side(world):
    text = kbm.format_kicker_board(_board(world), top=None)
    assert "Alpha Kicker" in text and "corrected" in text
    assert kbm.BASIS_ESPN_STAT_LINE in text and kbm.BASIS_UNCORRECTED in text
    # the delta column carries a sign so a reader sees the direction
    assert "+" in text


def test_team_aliases_are_normalized_on_the_house_side(db):
    """LAR/LA: the projection feed stores LAR and league state stores LA. The
    house spine normalizes, so the board must carry the normalized abbreviation
    rather than whichever one the feed happened to use."""
    _house_kicker(db, espn_id="9001", gsis_id="00-0000001", sleeper_id="1",
                  name="Alpha Kicker", team="LAR",
                  fg_0_39=1.0, fg_40_49=0.5, pat=3.0, missed=0.3)
    _espn_kicker(db, espn_id="9001", gsis_id="00-0000001", name="Alpha Kicker",
                 team="LA", fg_0_39=17.0, fg_40_49=8.5, fg_50_59=8.0,
                 pat=51.0, missed=5.1)
    line = _line(_board(db), "Alpha Kicker")
    assert line.team == base.TEAM_ALIASES.get("LAR", "LAR")


def test_a_thin_board_still_reports_the_mixture_at_its_clamped_baseline(db):
    """Fewer kickers than the window: `replacement_levels` clamps to the LAST
    one, so the mixture check must clamp to the same row. Without this a
    two-kicker board reports 'not mixed' while an uncorrected kicker is setting
    the baseline for every K VOR on it."""
    _house_kicker(db, espn_id="9001", gsis_id="00-0000001", sleeper_id="1",
                  name="Alpha Kicker", team="DAL", fg_0_39=1.0, fg_40_49=0.5,
                  pat=3.0, missed=0.3)
    _house_kicker(db, espn_id="9002", gsis_id="00-0000002", sleeper_id="2",
                  name="Gamma Kicker", team="NYG", fg_0_39=0.6, fg_40_49=0.3,
                  pat=2.0, missed=0.4)
    _espn_kicker(db, espn_id="9001", gsis_id="00-0000001", name="Alpha Kicker",
                 team="DAL", fg_0_39=17.0, fg_40_49=8.5, fg_50_59=8.0,
                 pat=51.0, missed=5.1)
    with pytest.raises(kbm.KickerBoardDegraded):
        _board(db)
    board = _board(db, allow_partial=True)
    assert len(board.lines) == 2 and board.uncorrected_count == 1
    assert board.replacement_is_mixed is True


def test_a_kicker_with_no_espn_id_is_left_alone_by_apply_to_valuation(world):
    """The uncrosswalked case (live 2026: 57 of 153 house kickers carry no
    espn_id). There is nothing to join on, so the row must come through
    untouched rather than being matched by name or by position rank."""
    world.execute("UPDATE players SET espn_id = NULL WHERE name = 'Alpha Kicker'")
    world.commit()
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    alpha_before = next(r for r in rows if r.player == "Alpha Kicker")
    assert alpha_before.espn_id is None
    new = kbm.apply_to_valuation(rows, _board(world))
    alpha_after = next(r for r in new if r.player == "Alpha Kicker")
    assert alpha_after.proj_points == pytest.approx(alpha_before.proj_points)


# =========================================================================
# THE HORIZON GUARD — never scale a part-season projection UP
# =========================================================================


def test_an_espn_row_projecting_fewer_games_than_the_window_is_never_scaled_up(world):
    """The critical one. ``corrected = espn_total * played / games`` is a RATE
    extrapolation, and ESPN publishes short ``projected_games`` counts in this
    very snapshot (2.0 / 4.0 / 6.0 / 10.0 / 11.0 / 15.0 / 16.0 at other
    positions on 2026-08-30). With ``games=6`` and a 16-week window the
    un-capped form multiplies a part-season projection by 2.67: reproduced on
    the live board, the 32nd-best kicker priced at 324.4 points, became K1 and
    overall #1 above every RB and WR, and was taken at pick 89 ahead of the
    D/ST — with the row still saying it had been "scaled from ESPN's 17-game
    season".
    """
    world.execute("UPDATE espn_projections SET projected_games = 6.0 "
                  "WHERE espn_key = ?", (ALPHA_ESPN_ID,))
    world.commit()
    board = _board(world, allow_partial=True)
    alpha = _line(board, "Alpha Kicker")

    espn_total = scoring.score("K", ALPHA_ESPN_LINE)
    assert alpha.basis == kbm.BASIS_UNCORRECTED
    assert alpha.season_points == pytest.approx(alpha.current_points)
    assert alpha.season_points < espn_total * 16 / 6      # the extrapolation
    assert any("refusing to scale" in r for r in alpha.reasons)
    assert any("only 6 games" in r for r in alpha.reasons)
    # ...and he is nowhere near the top of the board any more.
    assert board.lines[0].player != "Alpha Kicker"


def test_a_games_count_equal_to_the_window_is_still_corrected(world):
    """The boundary is ``games < played``, not ``games <= played``: ESPN saying
    16 games for a 16-game window is a horizon that lines up exactly."""
    world.execute("UPDATE espn_projections SET projected_games = 16.0 "
                  "WHERE espn_key = ?", (ALPHA_ESPN_ID,))
    world.commit()
    alpha = _line(_board(world), "Alpha Kicker")
    assert alpha.corrected
    assert alpha.season_points == pytest.approx(scoring.score("K", ALPHA_ESPN_LINE))


def test_the_correction_label_quotes_the_rows_own_horizon(world):
    """Rule 6 at the point the number is most likely to be wrong. The label used
    to hard-code "scaled from ESPN's 17-game season" whatever the row said."""
    world.execute("UPDATE espn_projections SET projected_games = 16.0 "
                  "WHERE espn_key = ?", (ALPHA_ESPN_ID,))
    world.commit()
    alpha = _line(_board(world), "Alpha Kicker")
    assert any("16-game projection" in r for r in alpha.reasons)
    assert not any("17-game" in r for r in alpha.reasons)
    # the module constant stays generic rather than quoting a horizon it cannot know
    assert "17" not in kbm.CORRECTION_LABEL


# =========================================================================
# COMPLETENESS FLOORS — a partial source is a refusal, not a footnote
# =========================================================================


def test_a_degraded_pull_refuses_instead_of_quietly_reordering_the_k_board(world):
    """Four of thirty-two ESPN rows is a degraded pull, and it re-orders the K
    board (measured live: Jason Myers to K1 above the best D/ST, Aubrey — K1
    under a complete correction — to K8) and flips the K/DST pick order. Nothing
    downstream can show the mixture, so the board refuses to exist."""
    world.execute("DELETE FROM espn_projections WHERE espn_key NOT IN ('9001', '9002')")
    world.commit()
    with pytest.raises(kbm.KickerBoardDegraded) as exc:
        _board(world)
    assert "kicking clubs" in str(exc.value)
    assert "pull_espn_projections" in str(exc.value)


def test_allow_partial_serves_the_degraded_pull_with_the_coverage_stated(world):
    world.execute("DELETE FROM espn_projections WHERE espn_key NOT IN ('9001', '9002')")
    world.commit()
    board = _board(world, allow_partial=True)
    assert board.corrected_count == 2
    assert board.source_club_fraction == pytest.approx(2 / 14)
    assert any("kicking clubs" in r for r in board.reasons)


def test_a_complete_source_is_not_called_partial(world):
    """The floor must not fire on the healthy case, or it teaches the operator
    to pass allow_partial reflexively."""
    board = _board(world)
    assert board.partial_allowed is False
    assert board.source_club_fraction == pytest.approx(13 / 14)
    assert not any("under the" in r for r in board.reasons)


def test_a_missing_espn_table_gives_the_guided_refusal_not_a_bare_sqlite_error(db):
    """Migrations 009/010 create ``espn_projections``. On a box that has not
    applied them the operator's only instruction — which pull to run — is in
    this module's message, and a bare OperationalError loses it."""
    _house_kicker(db, espn_id="9001", gsis_id="00-0000001", sleeper_id="1",
                  name="Alpha Kicker", team="DAL", fg_0_39=1.0, fg_40_49=0.5,
                  pat=3.0, missed=0.3)
    db.execute("DROP TABLE espn_projections")
    db.commit()
    with pytest.raises(kbm.KickerBoardUnavailable) as exc:
        _board(db)
    assert "pull_espn_projections" in str(exc.value)
    assert "009" in str(exc.value)


# =========================================================================
# THE GRADING SEAM'S KEY SPACE — the silent no-op
# =========================================================================


def _board_keyed_points(board, world):
    """A ``draft/grader.weekly_points_map``-shaped map: player-id STRINGS.

    Mirrors ``grader._board_player_id`` for the rows this module cares about —
    a corrected kicker always has an ``espn_id``, so his board key is it.
    """
    points = valuation.weekly_points(world, as_of=DAY, season=SEASON)
    by_key = board.by_key()
    return {str(by_key[k].espn_id or by_key[k].gsis_id): dict(v)
            for k, v in points.items() if k in by_key}


def test_apply_to_weekly_points_splices_a_board_keyed_map(world):
    """THE DEFECT THIS REPLACES: the shipped integration recipe hands this
    function ``grader.weekly_points_map``'s output, which is keyed by
    ``BoardEntry.player_id`` strings while ``KickerLine.key`` is a
    ``weekly_lines`` tuple. Measured live, that substituted 0 of 29 kickers,
    raised nothing, and left the grading twin on the uncorrected board — which
    flips the sign of the A/B the correction is judged on."""
    board = _board(world)
    points = _board_keyed_points(board, world)
    out = kbm.apply_to_weekly_points(points, board)
    moved = [pid for pid, weeks in out.items()
             if sum(weeks.values()) > sum(points[pid].values())]
    assert len(moved) == board.corrected_count
    alpha = _line(board, "Alpha Kicker")
    assert sum(out[str(alpha.espn_id)].values()) == pytest.approx(alpha.season_points)


def test_apply_to_weekly_points_refuses_a_map_it_matches_nothing_in(world):
    """Matching NOTHING is the failure the whole module exists to remove."""
    board = _board(world)
    alien = {f"nobody-{i}": {1: 1.0} for i in range(5)}
    with pytest.raises(kbm.KickerBoardMismatch) as exc:
        kbm.apply_to_weekly_points(alien, board)
    assert "corrected kickers matched" in str(exc.value)


def test_apply_to_weekly_points_refuses_a_mixed_key_space(world):
    board = _board(world)
    mixed = {("SKILL", "00-0000001"): {1: 1.0}, "9001": {1: 1.0}}
    with pytest.raises(kbm.KickerBoardMismatch):
        kbm.apply_to_weekly_points(mixed, board)


def test_apply_to_weekly_points_keeps_a_metadata_carrying_map_whole(world):
    """``grader.WeeklyPointsMap`` is a dict SUBCLASS carrying positions/names/
    teams, and ``grade_roster`` derives its whole modelled field from them.
    Returning a bare dict degrades that to None with nothing raised."""
    class _MetaMap(dict):
        def __init__(self, points, *, positions, names, teams):
            super().__init__(points)
            self.positions, self.names, self.teams = positions, names, teams

    board = _board(world)
    raw = _board_keyed_points(board, world)
    meta = _MetaMap(raw, positions={k: "K" for k in raw}, names={}, teams={})
    out = kbm.apply_to_weekly_points(meta, board)
    assert isinstance(out, _MetaMap)
    assert out.positions == meta.positions
    assert out is not meta and dict(meta) == raw   # input untouched


def test_apply_to_weekly_points_works_against_the_real_grader_map(world):
    """The seam the documented recipe actually names, exercised end to end.

    Guarded by ``importorskip`` (rule 8: this core module imports nothing from
    ``draft/`` — the test reaches ACROSS the quarantine, the code does not).
    """
    grader = pytest.importorskip("ziggurat.draft.grader")
    board = _board(world)
    weekly = grader.weekly_points_map(world, as_of=DAY, season=SEASON)
    out = kbm.apply_to_weekly_points(weekly, board)
    assert type(out) is type(weekly)
    assert out.positions == weekly.positions
    alpha = _line(board, "Alpha Kicker")
    assert sum(out[str(alpha.espn_id)].values()) == pytest.approx(alpha.season_points)
    assert sum(weekly[str(alpha.espn_id)].values()) < alpha.season_points


# =========================================================================
# THE VALUATION SEAM — alignment and reasons
# =========================================================================


def test_apply_to_valuation_refuses_rows_from_a_different_week_window(world):
    """A 17-week corrected total spliced onto a 14-week valuation overstates a
    kicker by ~60% and says nothing. The board records its own window so the
    seam can tell."""
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON,
                                     weeks=range(1, 15))
    with pytest.raises(kbm.KickerBoardMismatch) as exc:
        kbm.apply_to_valuation(rows, _board(world))
    assert "17 weeks" in str(exc.value) and "14" in str(exc.value)


def test_apply_to_valuation_accepts_rows_from_the_same_window(world):
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON,
                                     weeks=range(1, 15))
    board = _board(world, weeks=range(1, 15))
    new = kbm.apply_to_valuation(rows, board)
    assert next(r for r in new if r.position == "K" and r.pos_rank == 1).player \
        == "Alpha Kicker"


def test_apply_to_valuation_refuses_rows_from_another_season(world):
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    rows = [__import__("dataclasses").replace(r, season=2025) for r in rows]
    with pytest.raises(kbm.KickerBoardMismatch):
        kbm.apply_to_valuation(rows, _board(world))


def test_a_hand_built_board_without_a_window_skips_the_window_check(world):
    """``weeks=()`` means "not recorded" — a board assembled in a test or by a
    caller that never read a window. It must not become a hard failure."""
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    empty = kbm.KickerBoard(
        season=SEASON, as_of=DAY, source=kbm.SOURCE_ESPN, lines=(),
        corrected_count=0, uncorrected_count=0, unmatched_espn=(),
        replacement_is_mixed=False, reasons=())
    assert empty.weeks == ()
    kbm.apply_to_valuation(rows, empty)   # no raise


def test_apply_to_valuation_keeps_the_low_confidence_disclosure(world):
    """``build_valuation`` flags K and D/ST "low-confidence order (small season
    spread)". ``denoise_kdst`` is still in force after the correction and the K
    spread is still small, so the disclosure still applies — and this seam used
    to delete it from exactly the rows it moved."""
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    before = next(r for r in rows if r.player == "Alpha Kicker")
    assert any("low-confidence order" in r for r in before.reasons)
    after = next(r for r in kbm.apply_to_valuation(rows, _board(world))
                 if r.player == "Alpha Kicker")
    assert any("low-confidence order" in r for r in after.reasons)


def test_apply_to_valuation_drops_the_pre_correction_points_note(world):
    """The carried reasons must not quote the OLD total. ``build_valuation``'s
    K driver line is "kicking <n> pts (distance-tiered)", computed off the
    uncorrected feed; carrying it verbatim beside a corrected total puts two
    contradictory numbers on one row."""
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    before = next(r for r in rows if r.player == "Alpha Kicker")
    stale = next(r for r in before.reasons if r.startswith("kicking "))
    after = next(r for r in kbm.apply_to_valuation(rows, _board(world))
                 if r.player == "Alpha Kicker")
    assert stale not in after.reasons
    # ...while an UNCORRECTED row keeps everything, because nothing moved.
    gamma_before = next(r for r in rows if r.player == "Gamma Kicker")
    gamma_after = next(r for r in kbm.apply_to_valuation(rows, _board(world))
                       if r.player == "Gamma Kicker")
    assert all(r in gamma_after.reasons for r in gamma_before.reasons[2:])


def test_an_uncorrected_row_states_the_cause_the_board_actually_found(world):
    """Four different causes used to print one fixed sentence through this seam.
    A row ESPN never published and a row our crosswalk cannot join are opposite
    problems and the operator is the one who has to act on the difference."""
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    gamma = next(r for r in kbm.apply_to_valuation(rows, _board(world))
                 if r.player == "Gamma Kicker")
    assert any("ESPN publishes no projected stat line" in r for r in gamma.reasons)


# =========================================================================
# CAUSE ATTRIBUTION — an absent join is not an absent projection
# =========================================================================


def test_a_crosswalk_gap_is_not_reported_as_an_absent_espn_projection(world):
    """Live 2026: three kickers were told "ESPN publishes no projected stat line
    for this kicker" while the same call held their ESPN line — the join failed
    on a missing ``players.espn_id``. All three were already ON the board as
    nameless rows, which the board-level text also denied."""
    world.execute("UPDATE players SET espn_id = NULL WHERE name = 'Alpha Kicker'")
    world.commit()
    board = _board(world, allow_partial=True)
    alpha = _line(board, "Alpha Kicker")
    assert alpha.basis == kbm.BASIS_UNCORRECTED
    assert any("no espn_id" in r and "crosswalk gap" in r for r in alpha.reasons)
    assert not any("ESPN publishes no projected stat line" in r for r in alpha.reasons)
    # and the board-level note says the same kicker is already here, uncorrected
    assert ALPHA_ESPN_ID in board.unmatched_espn
    assert any("crosswalk gap" in d for d in board.unmatched_detail)


def test_two_uncrosswalked_kickers_on_one_club_are_not_guessed_between(world):
    """Refuse rather than guess: the live board carries TWO Giants kickers with
    no espn_id, so which of them ESPN's Giants row is cannot be told."""
    world.execute("UPDATE players SET espn_id = NULL WHERE name IN "
                  "('Alpha Kicker', 'K01 Kicker')")
    world.execute("UPDATE projections SET team = 'DAL' WHERE source_player_id = '1'")
    world.commit()
    board = _board(world, allow_partial=True)
    detail = " ".join(board.unmatched_detail)
    assert "cannot be told apart" in detail and "not guessed" in detail


# =========================================================================
# STALENESS AND DIRECTION — disclosures that are computed, not asserted
# =========================================================================


def test_the_board_names_the_day_the_espn_snapshot_was_pulled(world):
    board = _board(world)
    assert board.source_retrieved_as_of == DAY
    assert any(f"pulled {DAY}" in r for r in board.reasons)
    assert f"pulled {DAY}" in kbm.format_kicker_board(board)
    assert all(ln.source_retrieved_as_of == DAY for ln in board.lines if ln.corrected)


def test_an_old_espn_snapshot_reads_as_a_caution_not_as_a_fresh_board(world):
    """``espn_projections`` is not in the ingest registry and ESPN serves no
    projection history, so nothing ever refreshes it. A November board off an
    August pull is legal under the as-of gate and must not render identically to
    one built the day of the pull."""
    board = kbm.build_kicker_board(world, as_of="2026-11-01", season=SEASON)
    assert board.source_retrieved_as_of == DAY
    stale = [r for r in board.reasons if "STALE SOURCE" in r]
    assert stale and "63 days" in stale[0]
    assert "judgment call" in stale[0]


def test_the_one_way_safety_argument_is_computed_not_asserted(world):
    """"The correction only ADDS points" is true of the 2026 board and is not a
    theorem. An ESPN line BELOW the house line breaks it, and then an
    uncorrected kicker really can outrank a corrected one."""
    assert any("only ADDS points" in r for r in _board(world).reasons)
    world.execute("UPDATE espn_projections SET fg_made_0_39 = 1.0, fg_made_40_49 = 0.0, "
                  "fg_made_50_59 = 0.0, pat_made = 1.0 WHERE espn_key = ?",
                  (ALPHA_ESPN_ID,))
    world.commit()
    board = _board(world, allow_partial=True)
    assert not any("only ADDS points" in r for r in board.reasons)
    assert any(r.startswith("CAUTION") and "LOWERS" in r for r in board.reasons)


# =========================================================================
# COST — the seam that lets a caller not pay twice
# =========================================================================


def test_a_precomputed_weekly_lines_map_gives_the_identical_board(world):
    """Live measurement: ``load_board`` 3.75 s and a fresh ``build_kicker_board``
    3.77 s over the same 2.2M-row projections table, so wiring the correction
    naively DOUBLED the cockpit's cold start. Handing over the pass the caller
    already made must produce exactly the same board."""
    lines = valuation.weekly_lines(world, as_of=DAY, season=SEASON, weeks=WEEKS)
    a, b = _board(world), _board(world, lines=lines)
    assert [(ln.player, ln.season_points, ln.basis) for ln in a.lines] == \
           [(ln.player, ln.season_points, ln.basis) for ln in b.lines]
    assert a.reasons == b.reasons and a.weeks == b.weeks


def test_a_precomputed_map_is_actually_used_rather_than_re_read(world):
    """Teeth for the test above: equality alone cannot tell "reused" from
    "silently re-read". Hand over a map with one kicker withheld and the board
    must be short by exactly that kicker."""
    lines = valuation.weekly_lines(world, as_of=DAY, season=SEASON, weeks=WEEKS)
    dropped = next(k for k, v in lines.items() if v.player == "K13 Kicker")
    trimmed = {k: v for k, v in lines.items() if k != dropped}
    board = kbm.build_kicker_board(world, as_of=DAY, season=SEASON, weeks=WEEKS,
                                   lines=trimmed)
    assert len(board.lines) == len(_board(world).lines) - 1
    assert all(ln.player != "K13 Kicker" for ln in board.lines)


def test_apply_to_valuation_refuses_when_a_populated_board_matches_no_row(world):
    """The valuation seam's own version of the grading seam's silent no-op: a
    board whose corrected kickers join NOTHING must say so rather than hand back
    a confidently re-ranked board with no correction in it. (An EMPTY board is
    different and stays a no-op — there was nothing to apply.)"""
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    stripped = [__import__("dataclasses").replace(r, espn_id=None) for r in rows]
    with pytest.raises(kbm.KickerBoardMismatch) as exc:
        kbm.apply_to_valuation(stripped, _board(world))
    assert "espn_id" in str(exc.value)


def test_an_uncorrected_row_missing_from_the_board_states_only_what_is_known(world):
    """The fallback text must not assert a cause it did not check — that is the
    exact defect the per-row causes above fix."""
    rows = valuation.build_valuation(world, as_of=DAY, season=SEASON)
    board = _board(world)
    thin = kbm.KickerBoard(
        season=SEASON, as_of=DAY, source=kbm.SOURCE_ESPN,
        lines=tuple(ln for ln in board.lines if ln.player == "Alpha Kicker"),
        corrected_count=1, uncorrected_count=0, unmatched_espn=(),
        replacement_is_mixed=False, reasons=(), weeks=tuple(WEEKS))
    out = kbm.apply_to_valuation(rows, thin)
    missing = next(r for r in out if r.player == "K01 Kicker")
    assert any("does not appear on the corrected kicker board" in r
               for r in missing.reasons)
    assert not any("no espn_id to join on" in r for r in missing.reasons)


def test_a_precomputed_map_from_a_different_window_is_refused(world):
    """The caller owns the ``lines=`` contract, but the half that IS checkable
    gets checked: a 17-week map handed in under ``weeks=1-14`` would price
    17-week kickers onto a board that calls itself 14 weeks, and the valuation
    seam's window guard would then wave it through because the counts agree."""
    wide = valuation.weekly_lines(world, as_of=DAY, season=SEASON, weeks=WEEKS)
    with pytest.raises(kbm.KickerBoardMismatch) as exc:
        kbm.build_kicker_board(world, as_of=DAY, season=SEASON,
                               weeks=range(1, 15), lines=wide)
    assert "outside weeks" in str(exc.value)
