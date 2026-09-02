"""Tests for the realized-outcome draft backtest (backtest/draft_backtest.py).

The module's whole value is that its numbers can be WRONG in a direction that
embarrasses the project, so the tests here are aimed at the ways it could be
wrong in a direction that flatters it instead:

* the LOO value curve must not see the season it prices (leakage);
* the realized objective must be a real zero-sum accounting (ten teams sum to
  exactly 70), not a per-roster score that happens to look like wins;
* the deterministic seater must seat the SAME lineup the grader does, or the two
  reported objectives silently describe different rosters;
* a player who played zero games must survive onto the board at his August
  price, because pricing him at zero — or dropping him — is how a backtest
  accidentally drafts with hindsight.

A three-reviewer audit then found that four of the guards this file was supposed
to be enforcing could not fail, so those tests are rewritten rather than added
to, and each one now names the mutation that proves it bites:

* the COVERAGE gate checked a property `realized_weekly_map` satisfies by
  construction, so a total id-space divergence passed it;
* the KICKER position filter was monkeypatched away by the very test whose
  docstring claimed to check it, and the filter never ran on a cached read;
* the BOARD-RANK re-derivation test dropped the row at rank 2 and asserted the
  survivor carried rank 1, which it already did;
* `week1_first_gameday`, the single line that sets the entire leakage cutoff, had
  no test at all.

And one measurement defect that decided a published number is pinned here: the
waiver-tier stream depth must be what the ROOM ROSTERED, not `roster.teams`.

Everything here is synthetic and offline. Player names are invented (Rule 5).
"""

import random

import pytest

from backtest import draft_backtest as bt
from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.draft import grader
from ziggurat.draft.bots import FollowEspnRank
from ziggurat.draft.simulator import run_draft

WEEKS = tuple(range(1, 18))
REG = bt.REGULAR_SEASON_WEEKS


# ---------------------------------------------------------------- fixtures


def _synthetic_rows(*, qb=24, rb=60, wr=70, te=24, k=16, dst=16):
    """A draftable consensus board: rows ordered by a plausible ECR."""
    specs = [("QB", qb, 60.0), ("RB", rb, 1.0), ("WR", wr, 2.0),
             ("TE", te, 30.0), ("K", k, 200.0), ("DST", dst, 205.0)]
    entries = []
    for position, count, ecr0 in specs:
        for i in range(count):
            entries.append((position, i + 1, ecr0 + i * 1.7))
    entries.sort(key=lambda e: e[2])
    rows = []
    for rank, (position, pos_rank, ecr) in enumerate(entries, start=1):
        key = f"{position}{pos_rank:03d}"
        rows.append(bt.BoardRow(
            player_id=f"DST:{key}" if position == "DST" else key,
            outcome_key=key,
            name=f"Player {key}",
            position=position,
            team=f"T{pos_rank % 32:02d}",
            board_rank=rank,
            pos_rank=pos_rank,
            ecr=ecr,
            sd=2.0,
        ))
    return tuple(rows)


def _synthetic_outcomes(rows, *, seed=7, missing=()):
    """Realized weekly points, with a real bye and a few season-long absences.

    ``missing`` names outcome keys that recorded NO week at all — the consensus
    running back who tore an ACL in August. That row shape is the one the whole
    module exists to price, so every fixture carries some.
    """
    rng = random.Random(seed)
    base_by_pos = {"QB": 19.0, "RB": 13.0, "WR": 12.0, "TE": 8.0, "K": 8.0, "DST": 7.0}
    out = {}
    for row in rows:
        if row.outcome_key in missing:
            out[row.outcome_key] = {}
            continue
        bye = 6 + (row.pos_rank % 8)
        level = base_by_pos[row.position] * max(0.25, 1.6 - 0.05 * row.pos_rank)
        out[row.outcome_key] = {
            w: round(max(0.0, level + rng.uniform(-4.0, 4.0)), 3)
            for w in WEEKS if w != bye
        }
    return out


@pytest.fixture()
def world():
    rows = _synthetic_rows()
    missing = {"RB003", "WR005", "QB002"}
    outcomes = _synthetic_outcomes(rows, missing=missing)
    curve = bt.realized_value_curve({1: rows}, {1: outcomes}, training_seasons=[1])
    board = bt.board_entries(rows, curve)
    weekly = bt.realized_weekly_map(rows, outcomes)
    return rows, board, weekly, missing


# ---------------------------------------------------------------- the curve


def test_monotone_curve_is_non_increasing_and_stays_inside_the_pava_fit():
    values = [10.0, 12.0, 9.0, 9.5, 4.0, 4.0, 1.0]
    step = bt._isotonic_decreasing(values)
    smooth = bt._monotone_curve(values)

    assert all(a >= b - 1e-12 for a, b in zip(step, step[1:], strict=False))
    assert all(a >= b - 1e-12 for a, b in zip(smooth, smooth[1:], strict=False))
    # Interpolation moves values WITHIN the fit, never outside it.
    assert min(smooth) >= min(step) - 1e-9
    assert max(smooth) <= max(step) + 1e-9
    # PAVA pools [10, 12] to 11.0; the interpolation redistributes that block
    # around its own mean rather than moving it.
    assert step[0] == step[1] == pytest.approx(11.0)
    assert smooth[0] >= 11.0 >= smooth[1]


def test_the_curve_breaks_an_INTERIOR_plateau_but_not_a_leading_one():
    """What the interpolation does and — just as important — what it does not.

    An interior pooled block is straddled by two neighbouring block centres, so
    interpolation gives its members distinct values. A block at either END has no
    outward neighbour to interpolate toward, so its members stay tied: the fit
    genuinely has no information about their order.

    That residual tie is why ``board_entries`` adds ``_TIE_BREAK_EPS`` on top —
    within a leading plateau the ordering falls back to the consensus board rank,
    which is the only signal left, and the vor values become globally distinct so
    ``bots.best_by_vor`` cannot depend on set iteration order.
    """
    interior = bt._monotone_curve([10.0, 5.0, 7.0, 3.0])
    assert interior[1] > interior[2], "an interior plateau must be broken"

    leading = bt._monotone_curve([10.0, 12.0, 11.0, 9.0, 4.0])
    assert leading[0] == leading[1] == pytest.approx(11.0), "a leading tie survives"


def test_the_value_curve_never_sees_the_season_it_prices(world):
    """LEAKAGE. Rewriting a season's realized outcomes must not move the curve
    that season is drafted with — only the curves of the other seasons."""
    rows, _board, _weekly, _missing = world
    seasons = (2021, 2022, 2023)
    boards = {s: rows for s in seasons}
    outcomes = {s: _synthetic_outcomes(rows, seed=s) for s in seasons}

    def loo(season, oc):
        return bt.realized_value_curve(
            boards, oc, training_seasons=[s for s in seasons if s != season]
        )

    before_own = loo(2023, outcomes)
    before_other = loo(2021, outcomes)

    poisoned = dict(outcomes)
    poisoned[2023] = {key: {w: 999.0 for w in WEEKS} for key in outcomes[2023]}

    assert loo(2023, poisoned).by_position == before_own.by_position
    assert loo(2021, poisoned).by_position != before_other.by_position


# ---------------------------------------------------------------- the board


def test_board_vor_is_strictly_distinct_and_follows_consensus_within_a_position(world):
    _rows, board, _weekly, _missing = world
    assert len({e.vor for e in board}) == len(board)

    by_pos = {}
    for entry in board:
        by_pos.setdefault(entry.position, []).append(entry)
    for entries in by_pos.values():
        by_rank = sorted(entries, key=lambda e: e.espn_overall_rank)
        by_value = sorted(entries, key=lambda e: -e.vor)
        assert [e.player_id for e in by_rank] == [e.player_id for e in by_value]


def test_house_points_is_zero_for_every_entry(world):
    """Deliberate, and load-bearing: there is no projected season total for these
    seasons, and inventing one would have leaked the outcome through the grader's
    priced-entry gate — a player who missed the season is the ONLY kind of entry
    that gate would have tripped on."""
    _rows, board, weekly, _missing = world
    assert {e.house_points for e in board} == {0.0}
    grader.assert_board_coverage(board, weekly)   # trivially satisfied, by design


def test_a_player_who_never_played_stays_on_the_board_at_his_august_price(world):
    rows, board, weekly, missing = world
    for key in missing:
        assert key in weekly            # present in the id space...
        assert weekly[key] == {}        # ...and honestly empty
    priced = {e.player_id: e.vor for e in board}
    for row in rows:
        if row.outcome_key in missing:
            # He is on the board, and his value is the CONSENSUS value of his
            # rank — identical to a player at the same rank who stayed healthy.
            assert row.player_id in priced
    twins = [r for r in rows if r.position == "RB" and r.pos_rank in (3, 4)]
    a, b = (priced[t.player_id] for t in sorted(twins, key=lambda r: r.pos_rank))
    assert a > b and (a - b) < 25.0, "the injured RB3 is not discounted for it"


def test_coverage_gate_catches_a_missing_key(world):
    _rows, board, weekly, _missing = world
    bt.assert_backtest_coverage(board, weekly)
    drifted = dict(weekly)
    drifted.pop(board[0].player_id)
    with pytest.raises(bt.BacktestInputError, match="absent from the realized"):
        bt.assert_backtest_coverage(board, drifted)


def test_coverage_gate_catches_the_divergence_the_shipped_path_can_ACTUALLY_produce(world):
    """The failure the presence check could not see, reproduced end to end.

    ``realized_weekly_map`` writes ``{row.player_id: dict(outcomes.get(key, {}))}``
    — a KEY for every board row whether or not its outcome key resolved. So a
    total id-space divergence (every realized outcome re-keyed into a foreign
    namespace) produced a map with every key present and every value empty, the
    presence check passed, and all ten teams graded at objective exactly 7.0 with
    17 hole weeks. That is what this asserts is now refused.

    Mutation check: delete the ``min_priced_fraction`` branch and this passes.
    """
    rows, board, _weekly, _missing = world
    foreign = {f"FOREIGN::{k}": v for k, v in _synthetic_outcomes(rows).items()}
    diverged = bt.realized_weekly_map(rows, foreign)

    # The shape the old gate could not distinguish from "nobody played".
    assert all(pid in diverged for pid in (e.player_id for e in board))
    assert not any(diverged.get(e.player_id) for e in board)

    with pytest.raises(bt.BacktestInputError, match="carry ANY realized week"):
        bt.assert_backtest_coverage(board, diverged)


def test_coverage_gate_catches_a_PARTIAL_divergence_that_only_hits_defenses(world):
    """The aggregate floor rides straight through this one, which is why the
    position rule exists: a relocated-team abbreviation grades every D/ST at zero
    all season and biases the K/DST result with nothing reporting it, while 500
    healthy skill rows keep the overall fraction at 94%.

    Mutation check: delete the ``must_play`` branch and this passes.
    """
    rows, board, _weekly, _missing = world
    outcomes = _synthetic_outcomes(rows)
    for row in rows:
        if row.position == "DST":
            outcomes.pop(row.outcome_key, None)
    partial = bt.realized_weekly_map(rows, outcomes)

    priced = sum(1 for e in board if partial.get(e.player_id))
    assert priced / len(board) > bt._MIN_PRICED_FRACTION, (
        "the aggregate floor must NOT be what catches this, or the test proves nothing"
    )
    with pytest.raises(bt.BacktestInputError, match="no realized week at all"):
        bt.assert_backtest_coverage(board, partial)


# ---------------------------------------------------------------- seating


def test_stream_levels_match_the_grader_at_the_shipped_kdst_setting(world):
    _rows, _board, weekly, _missing = world
    mine = bt._stream_levels(
        weekly, weekly.positions, weeks=WEEKS, roster=DEFAULT_ROSTER,
        stream_positions=bt.STREAM_KDST,
    )
    theirs = grader.stream_levels_from_board(
        weekly, weekly.positions, weeks=WEEKS, roster=DEFAULT_ROSTER
    )
    assert set(mine) == set(theirs)
    for week, levels in theirs.items():
        assert set(mine[week]) == set(levels)
        for position, (points, _var, pid) in levels.items():
            assert mine[week][position][0] == pytest.approx(points)
            assert mine[week][position][2] == pid


def test_the_waiver_tier_depth_is_what_the_ROOM_ROSTERED_not_one_per_team(world):
    """THE defect that decided this study's most-quoted number.

    ``grader``'s rule credits an empty slot at index ``roster.teams`` = 10, on
    the stated reasoning "the best player who would still be unrostered if all
    ten teams held exactly one". That is true at K and D/ST, where position caps
    force exactly one per team (measured league-wide: 10.00 and 10.00, no
    variation). It is false at QB and TE, where the room rosters ~20 and ~16 —
    so the credit was 5-10 ranks too shallow, and it is paid to whichever arm
    leaves a starting slot EMPTY, which is the baseline.

    Mutation check: make ``_stream_levels`` ignore ``rostered`` and this fails.
    """
    _rows, _board, weekly, _missing = world
    shallow = bt._stream_levels(
        weekly, weekly.positions, weeks=WEEKS, roster=DEFAULT_ROSTER,
        stream_positions=bt.STREAM_WITH_QB_TE,
    )
    # A room that rosters 20 QB, 16 TE, 10 K and 10 DST, exactly as measured.
    rostered = set()
    for position, want in (("QB", 20), ("TE", 16), ("K", 10), ("DST", 10)):
        got = sorted(p for p, v in weekly.positions.items() if v == position)
        rostered.update(got[:want])
    measured = bt._stream_levels(
        weekly, weekly.positions, weeks=WEEKS, roster=DEFAULT_ROSTER,
        stream_positions=bt.STREAM_WITH_QB_TE, rostered=frozenset(rostered),
    )

    # K and D/ST are UNCHANGED — the cap really does make ten the right depth.
    for week, levels in shallow.items():
        for position in ("K", "DST"):
            if position in levels:
                assert measured[week][position] == levels[position]
    # QB and TE are strictly cheaper at the real depth, in every week.
    for position in ("QB", "TE"):
        deep = [measured[w][position][0] for w in shallow if position in shallow[w]]
        thin = [shallow[w][position][0] for w in shallow if position in shallow[w]]
        assert deep and len(deep) == len(thin)
        assert all(d < t for d, t in zip(deep, thin, strict=True)), (
            f"{position}: crediting an empty slot at the 11th-best when the room "
            "rosters 16-20 hands out a starter for free"
        )


def test_blind_add_never_credits_the_week_winner_and_needs_a_free_agent_pool(world):
    """``blind_add`` removes the hindsight from the CHOICE.

    The waiver-tier rule credits the player who turned out to score most that
    week among the unrostered. A real Tuesday add is made on consensus rank. This
    pins that the blind rule picks by rank, that its pick is a free agent, and
    that asking for it without a free-agent pool RAISES rather than guessing.
    """
    _rows, _board, weekly, _missing = world
    ranks = {}
    per = {}
    for pid, pos in weekly.positions.items():
        per[pos] = per.get(pos, 0) + 1
        ranks[pid] = per[pos]
    # A twelve-deep QB pool leaves a real free-agent field to choose wrongly from.
    rostered = frozenset(sorted(p for p, v in weekly.positions.items() if v == "QB")[:12])

    blind = bt._stream_levels(
        weekly, weekly.positions, weeks=WEEKS, roster=DEFAULT_ROSTER,
        stream_positions=("QB",), rostered=rostered, pos_ranks=ranks, mode="blind_add",
    )
    picked = {levels["QB"][2] for levels in blind.values()}
    assert picked and picked.isdisjoint(rostered), "blind_add must add a FREE AGENT"
    assert len(picked) <= 2, "the blind rule takes the same top-ranked free agent each week"

    # THE POINT: it is not the week's best free agent. A rule with hindsight in
    # the choice would be, every week; this one leaves points on the table, which
    # is what a blind Tuesday add really does.
    best_free = {
        week: max(
            weekly[pid][week]
            for pid, pos in weekly.positions.items()
            if pos == "QB" and pid not in rostered and week in weekly.get(pid, {})
        )
        for week in blind
    }
    assert all(blind[w]["QB"][0] <= best_free[w] + 1e-9 for w in blind)
    assert sum(blind[w]["QB"][0] for w in blind) < sum(best_free.values()) - 1e-9, (
        "blind_add matched the week's best free agent EVERY week — that is "
        "hindsight wearing a blind label"
    )
    # (How far behind depends on how noisy the pool is. This fixture's noise is
    # small by construction, so the top-ranked free agent is often also the
    # week's best; the separation on real data is measured in the research note.)

    with pytest.raises(bt.BacktestInputError, match="free-agent pool"):
        bt._stream_levels(
            weekly, weekly.positions, weeks=WEEKS, roster=DEFAULT_ROSTER,
            stream_positions=("QB",), mode="blind_add",
        )


def test_extending_the_stream_to_qb_and_te_only_adds_those_positions(world):
    _rows, _board, weekly, _missing = world
    narrow = bt._stream_levels(weekly, weekly.positions, weeks=WEEKS,
                              roster=DEFAULT_ROSTER, stream_positions=bt.STREAM_KDST)
    wide = bt._stream_levels(weekly, weekly.positions, weeks=WEEKS,
                            roster=DEFAULT_ROSTER,
                            stream_positions=bt.STREAM_WITH_QB_TE)
    for week, levels in narrow.items():
        for position, value in levels.items():
            assert wide[week][position] == value
    assert any("QB" in levels for levels in wide.values())
    assert not any("QB" in levels for levels in narrow.values())


def _one_draft(board, seed=3):
    return run_draft(board, [FollowEspnRank() for _ in range(10)], rng=random.Random(seed))


def test_seat_totals_mirror_the_graders_own_weekly_means(world):
    """THE MIRROR. ``_seat_totals`` reproduces ``grader._seat_week``'s mu, and if
    it ever drifts the realized objective and the modelled one describe different
    lineups while both look plausible."""
    _rows, board, weekly, _missing = world
    result = _one_draft(board)
    streams = bt._stream_levels(weekly, weekly.positions, weeks=WEEKS,
                               roster=DEFAULT_ROSTER, stream_positions=bt.STREAM_KDST)
    for team in (0, 4, 9):
        entries = list(result.rosters[team])
        rivals = {t: result.rosters[t] for t in result.rosters if t != team}
        totals, _holes = bt._seat_totals(
            entries, weekly, tuple(REG), DEFAULT_ROSTER, streams
        )
        graded = grader.grade_roster(
            entries, weekly, opponent_rosters=rivals,
            regular_season_weeks=REG, playoff_weeks=(),
        )
        for week in REG:
            assert totals[week] == pytest.approx(graded.weekly_means[week - 1], abs=1e-9)


def test_a_player_with_no_week_is_not_seated_at_zero(world):
    """The difference between this grading and a season-total comparison: an
    absent week means the slot is EMPTY, not filled by a player scoring nothing."""
    _rows, board, weekly, _missing = world
    by_id = {e.player_id: e for e in board}
    qb = next(e for e in board if e.position == "QB")
    entries = [qb]
    week = next(w for w in REG if w in weekly[qb.player_id])
    totals, _ = bt._seat_totals(entries, weekly, (week,), DEFAULT_ROSTER, None)
    assert totals[week] == pytest.approx(weekly[qb.player_id][week])

    absent = next(e for pid, e in by_id.items() if not weekly[pid])
    totals, holes = bt._seat_totals([absent], weekly, (week,), DEFAULT_ROSTER, None)
    assert totals[week] == 0.0 and holes == (week,)


# ---------------------------------------------------------------- the objective


def test_realized_wins_over_ten_teams_sum_to_exactly_seventy(world):
    """A zero-sum accounting, not a per-roster score dressed as wins. Ten teams,
    fourteen weeks, every week worth exactly one win somewhere."""
    _rows, board, weekly, _missing = world
    result = _one_draft(board)
    grade = bt.realized_grade_fn(weekly)
    total = 0.0
    for team in sorted(result.rosters):
        rivals = {t: result.rosters[t] for t in result.rosters if t != team}
        total += grade(list(result.rosters[team]), rivals).objective
    assert total == pytest.approx(len(REG) * DEFAULT_ROSTER.teams / 2.0, abs=1e-9)


def test_the_realized_objective_refuses_the_synthetic_field(world):
    _rows, board, weekly, _missing = world
    result = _one_draft(board)
    grade = bt.realized_grade_fn(weekly)
    with pytest.raises(bt.BacktestInputError, match="real rivals"):
        grade(list(result.rosters[0]), None)


def test_the_grade_carries_both_objectives_from_one_draft(world):
    _rows, board, weekly, _missing = world
    result = _one_draft(board)
    grade = bt.realized_grade_fn(weekly)
    rivals = {t: result.rosters[t] for t in result.rosters if t != 0}
    out = grade(list(result.rosters[0]), rivals)
    assert 0.0 <= out.objective <= len(REG)
    assert 0.0 <= out.expected_wins <= len(REG)
    # Distinct measurements, not one number under two names.
    assert out.objective != out.expected_wins
    assert out.title_prob == 0.0
    assert any("NOT COMPUTED" in r for r in out.reasons)


def test_beating_every_rival_every_week_is_a_perfect_record(world):
    """A directional check the sum-to-70 identity cannot make: the objective must
    actually reward outscoring people."""
    _rows, board, weekly, _missing = world
    result = _one_draft(board)
    juiced = dict(weekly)
    mine = {e.player_id for e in result.rosters[0]}
    for pid in mine:
        if weekly[pid]:
            juiced[pid] = {w: 1000.0 for w in weekly[pid]}
    juiced_map = grader.WeeklyPointsMap(
        juiced, positions=weekly.positions, names=weekly.names, teams=weekly.teams
    )
    grade = bt.realized_grade_fn(juiced_map)
    rivals = {t: result.rosters[t] for t in result.rosters if t != 0}
    out = grade(list(result.rosters[0]), rivals)
    assert out.objective == pytest.approx(float(len(REG)))
    assert out.playoff_prob == 1.0


# ---------------------------------------------------------------- kickers


def test_kicker_points_use_the_house_distance_rules(monkeypatch):
    """The K slot is priced from a supplement because ``weekly_stats`` carries no
    kicking columns at all. Check the mapping onto ``scoring.py``'s buckets, and
    that a non-kicker row can never enter (it would OVERWRITE that player's
    offensive points with a kicker score of 0.0 — measured: every skill value
    curve came back identically zero)."""
    import pandas as pd

    frame = pd.DataFrame([
        {"player_id": "K1", "season": 2023, "week": 1, "season_type": "REG",
         "position": "K", "fg_made_0_19": 1, "fg_made_20_29": 0, "fg_made_30_39": 1,
         "fg_made_40_49": 1, "fg_made_50_59": 1, "fg_made_60_": 0,
         "pat_made": 3, "fg_missed": 1},
        {"player_id": "K1", "season": 2023, "week": 2, "season_type": "POST",
         "position": "K", "fg_made_0_19": 5, "fg_made_20_29": 0, "fg_made_30_39": 0,
         "fg_made_40_49": 0, "fg_made_50_59": 0, "fg_made_60_": 0,
         "pat_made": 0, "fg_missed": 0},
    ])
    monkeypatch.setattr(bt, "kicking_frame", lambda season, **kw: frame)
    points = bt._kicker_points(2023)
    # 2 x 3 (0-39) + 4 (40-49) + 5 (50-59) + 3 x 1 (PAT) - 1 (miss) = 17.0
    assert points == {"K1": {1: pytest.approx(17.0)}}, "POST weeks must not count"


def test_the_kicker_filter_runs_on_the_CACHED_read_not_only_on_the_write(tmp_path):
    """The filter used to run only on the branch that WRITES the parquet, so on
    every run after the first the frame was whatever the file held, unchecked.

    Demonstrated live before the fix: a cache file holding one RB row made
    ``_kicker_points`` return ``{'00-RB1': {1: 0.0}, ...}``, and
    ``season_outcomes``' ``out.update(_kicker_points(...))`` then REPLACED that
    running back's real offensive week with 0.0 — the exact failure the module
    documents as having made every QB/RB/WR/TE value curve come back identically
    zero with nothing raised. The old test monkeypatched ``kicking_frame`` away
    and therefore never exercised the filter at all: deleting the filter left it
    green.

    Mutation check: drop the ``position == 'K'`` filter from ``_kicker_rows_only``
    and the first assertion fails.
    """
    import pandas as pd

    columns = list(bt._KICKING_COLUMNS)
    def _row(pid, position, made_30_39):
        base = dict.fromkeys(columns, 0)
        base.update(player_id=pid, season=2023, week=1, season_type="REG",
                    position=position, fg_made_30_39=made_30_39)
        return base

    cache = tmp_path / "cache"
    cache.mkdir()
    pd.DataFrame([_row("00-RB1", "RB", 0), _row("00-K1", "K", 2)],
                 columns=columns).to_parquet(cache / "kicking-2023.parquet", index=False)

    points = bt._kicker_points(2023, cache_dir=str(cache))
    assert "00-RB1" not in points, (
        "a non-kicker row survived the cached read; season_outcomes would then "
        "overwrite that player's real offensive week with a kicker score of 0.0"
    )
    assert points == {"00-K1": {1: pytest.approx(6.0)}}

    # A cache that has lost a required column is refused, not silently priced.
    pd.DataFrame([_row("00-K1", "K", 2)], columns=columns).drop(
        columns=["fg_missed"]
    ).to_parquet(cache / "kicking-2024.parquet", index=False)
    with pytest.raises(bt.BacktestInputError, match="missing kicking columns"):
        bt._kicker_points(2024, cache_dir=str(cache))


def test_season_outcomes_lets_the_kicking_supplement_win(monkeypatch, db):
    """A kicker also has a (kicking-blind, all-zero) row in ``weekly_stats``. The
    merge order must let the supplement replace it, not the other way round."""
    monkeypatch.setattr(bt, "_offense_points", lambda conn, season: {"K1": {1: 0.0}})
    monkeypatch.setattr(bt, "_dst_points", lambda conn, season: {})
    monkeypatch.setattr(bt, "_kicker_points", lambda season, **kw: {"K1": {1: 11.0}})
    assert bt.season_outcomes(db, 2023) == {"K1": {1: 11.0}}


# ---------------------------------------------------------------- board reads


def _panel_row(db, *, fp_id, player, pos, team, ecr, page, scrape, season,
               page_rank, pos_rank, retrieved="2026-08-30", gsis=None):
    db.execute(
        "INSERT INTO fpecr_panel (fantasypros_id, ecr_type, fp_page, scrape_date, "
        "season, nfl_week, week_basis, player, position, team, gsis_id, espn_id, "
        "ecr, sd, best, worst, player_owned_avg, player_owned_espn, page_rank, "
        "pos_rank, retrieved_as_of, knowable_as_of) "
        "VALUES (?,'ro',?,?,?,0,'schedules',?,?,?,?,NULL,?,1.0,1,2,90.0,90.0,?,?,?,?)",
        (fp_id, page, scrape, season, player, pos, team, gsis, ecr, page_rank,
         pos_rank, retrieved, scrape),
    )


def test_the_preseason_board_read_cannot_reach_an_in_season_scrape(db):
    """Two gates on purpose: ``as_of`` is what the system could know, ``before``
    is where the football calendar sits. Either alone excludes the mid-season
    board; both make it structural."""
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, retrieved_as_of, knowable_as_of) "
        "VALUES ('g1',2023,1,'REG','2023-09-07','BUF','MIA','2026-08-30','2026-08-30')"
    )
    for scrape, ecr in (("2023-09-01", 2.0), ("2023-10-06", 9.0)):
        _panel_row(db, fp_id="1", player="Player A", pos="WR", team="CIN", ecr=ecr,
                   page="ppr-cheatsheets", scrape=scrape, season=2023,
                   page_rank=1, pos_rank=1, gsis="00-0000001")
    db.commit()

    scrape, rows, coverage = bt.preseason_board_rows(
        db, season=2023, week1_date="2023-09-07"
    )
    assert scrape == "2023-09-01"
    assert [r.ecr for r in rows] == [2.0]
    assert coverage["unresolved_dropped"] == 0


def test_an_unresolvable_row_is_dropped_and_counted_not_graded_at_zero(db):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, retrieved_as_of, knowable_as_of) "
        "VALUES ('g1',2023,1,'REG','2023-09-07','BUF','MIA','2026-08-30','2026-08-30')"
    )
    _panel_row(db, fp_id="1", player="Known", pos="WR", team="CIN", ecr=2.0,
               page="ppr-cheatsheets", scrape="2023-09-01", season=2023,
               page_rank=1, pos_rank=1, gsis="00-0000001")
    _panel_row(db, fp_id="2", player="Unknown", pos="WR", team="CIN", ecr=3.0,
               page="ppr-cheatsheets", scrape="2023-09-01", season=2023,
               page_rank=2, pos_rank=2, gsis=None)
    db.commit()

    _scrape, rows, coverage = bt.preseason_board_rows(
        db, season=2023, week1_date="2023-09-07"
    )
    assert [r.name for r in rows] == ["Known"]
    assert coverage == {"kept": 1, "unresolved_dropped": 1}


def test_both_ranks_are_re_derived_over_the_survivors(db):
    """THE TEST THAT USED TO PROVE NOTHING. Its predecessor dropped the row at
    page_rank 2 and asserted ``rows[0].board_rank == 1`` — which the surviving row
    already carried from the panel, so replacing the re-derivation with ``pass``
    left it green. Dropping the row at rank 1 is what makes the assertion bite.

    ``pos_rank`` matters for a different reason and was not re-derived at all: it
    is the key into the positional value curve, and a hole there put a hard 0.0
    into the raw curve at a rank no training board reached. Measured gaps on the
    shipped boards included RB 81/122/130 and TE 88 (2021), WR 181 (2023) and
    five K ranks.

    Mutation check: replace either re-derivation with the panel's own value and
    one of the two assertions below fails.
    """
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, retrieved_as_of, knowable_as_of) "
        "VALUES ('g1',2023,1,'REG','2023-09-07','BUF','MIA','2026-08-30','2026-08-30')"
    )
    # Page ranks 1..5; ranks 1 (a WR) and 3 (an RB) do not resolve.
    spec = [
        ("1", "Gone WR", "WR", 1, 1, None),
        ("2", "Kept WR", "WR", 2, 2, "00-0000002"),
        ("3", "Gone RB", "RB", 3, 1, None),
        ("4", "Kept RB", "RB", 4, 2, "00-0000004"),
        ("5", "Kept WR2", "WR", 5, 3, "00-0000005"),
    ]
    for fp_id, name, pos, page_rank, pos_rank, gsis in spec:
        _panel_row(db, fp_id=fp_id, player=name, pos=pos, team="CIN",
                   ecr=float(page_rank), page="ppr-cheatsheets", scrape="2023-09-01",
                   season=2023, page_rank=page_rank, pos_rank=pos_rank, gsis=gsis)
    db.commit()

    _scrape, rows, coverage = bt.preseason_board_rows(
        db, season=2023, week1_date="2023-09-07"
    )
    assert coverage == {"kept": 3, "unresolved_dropped": 2}
    # Contiguous 1..3 in consensus order, NOT the panel's 2/4/5.
    assert [r.board_rank for r in rows] == [1, 2, 3]
    # Contiguous within each position, NOT the panel's WR 2/3 and RB 2.
    assert [(r.position, r.pos_rank) for r in rows] == [
        ("WR", 1), ("RB", 1), ("WR", 2)
    ]


def test_a_value_curve_with_a_rank_hole_is_refused(world):
    """A missing rank used to enter the curve as a hard 0.0 — not a small number
    for a season total — which the smoother then spread over its neighbours."""
    rows, _board, _weekly, _missing = world
    outcomes = _synthetic_outcomes(rows)
    holed = tuple(r for r in rows if not (r.position == "RB" and r.pos_rank == 4))
    with pytest.raises(bt.BacktestInputError, match="no sample at rank"):
        bt.realized_value_curve({1: holed}, {1: outcomes}, training_seasons=[1])


def test_week1_first_gameday_is_week_ONE(db):
    """The single line that sets the entire leakage cutoff, and it had no test:
    pointing its SQL at week 3 left the suite green while every "preseason" board
    silently became the last scrape before week 3 — two weeks of injury and
    performance news inside the board every strategy drafts from.

    Mutation check: change ``AND week = 1`` to ``AND week = 3`` and this fails.
    """
    for game, week, day in (("g1", 1, "2023-09-10"), ("g0", 1, "2023-09-07"),
                            ("g3", 3, "2023-09-21"), ("g2", 2, "2023-09-14")):
        db.execute(
            "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
            "home_team, away_team, retrieved_as_of, knowable_as_of) "
            "VALUES (?,2023,?,'REG',?,'BUF','MIA','2026-08-30','2026-08-30')",
            (game, week, day),
        )
    # A preseason game LATER than the week-1 opener must not win either.
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, retrieved_as_of, knowable_as_of) "
        "VALUES ('p1',2023,1,'PRE','2023-08-12','BUF','MIA','2026-08-30','2026-08-30')"
    )
    db.commit()
    assert bt.week1_first_gameday(db, 2023) == "2023-09-07"

    with pytest.raises(bt.BacktestInputError, match="no ingested REG week-1"):
        bt.week1_first_gameday(db, 2024)


def test_missing_preseason_board_refuses_rather_than_returning_empty(db):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, retrieved_as_of, knowable_as_of) "
        "VALUES ('g1',2023,1,'REG','2023-09-07','BUF','MIA','2026-08-30','2026-08-30')"
    )
    db.commit()
    with pytest.raises(bt.BacktestInputError, match="no preseason ECR board"):
        bt.preseason_board_rows(db, season=2023, week1_date="2023-09-07")


def test_season_end_as_of_is_after_the_super_bowl_and_before_the_league_year():
    """The grading read still has a FACT-TIME gate; ``_season_end`` must sit past
    the last game and inside the same NFL season."""
    from ziggurat.data.asof import nfl_season_of

    for season in bt.SEASONS:
        stamp = bt._season_end(season)
        assert nfl_season_of(stamp) == season
        assert stamp > f"{season + 1}-02-01"


def test_realized_reads_use_latest_truth_not_the_default_view(db):
    """The bulk-history footgun: these tables are loaded now, so a default
    ``historical`` read of a past season returns nothing, silently."""
    db.execute(
        "INSERT INTO weekly_stats (player_id, season, week, season_type, position, "
        "recent_team, rushing_yards, retrieved_as_of, knowable_as_of) "
        "VALUES ('00-1',2023,1,'REG','RB','BUF',100.0,'2026-08-30','2023-09-10')"
    )
    db.commit()
    from ziggurat.data.nfl.weekly_stats import get_weekly_stats

    assert get_weekly_stats(db, as_of="2024-02-28", season=2023) == []
    assert bt._offense_points(db, 2023) == {"00-1": {1: pytest.approx(10.0)}}


def test_a_tied_week_is_half_a_win_not_a_win(world):
    """The synthetic fixture's floats never tie, so the sum-to-70 identity alone
    cannot see a tie rule that rounds in one direction. Force one."""
    _rows, board, weekly, _missing = world
    result = _one_draft(board)
    # Every player available every week at the same score: ten identical
    # nine-man lineups, so every week is a ten-way tie.
    flat = grader.WeeklyPointsMap(
        {pid: {w: 1.0 for w in WEEKS} for pid in weekly},
        positions=weekly.positions, names=weekly.names, teams=weekly.teams,
    )
    grade = bt.realized_grade_fn(flat, stream_positions=())
    rivals = {t: result.rosters[t] for t in result.rosters if t != 0}
    out = grade(list(result.rosters[0]), rivals)
    # Ten identical-scoring rosters: every week is a ten-way tie, worth half.
    assert out.objective == pytest.approx(len(REG) / 2.0)


def test_prepare_gives_each_season_a_curve_trained_on_the_others(monkeypatch, db):
    """The leave-one-out WIRING, separately from the estimator. A run narrowed to
    one season must still train on the full pool minus that season — otherwise a
    spot check silently measures a different board than the full run."""
    seen: dict[int, tuple[int, ...]] = {}
    monkeypatch.setattr(bt, "season_outcomes", lambda conn, season: {})
    monkeypatch.setattr(bt, "week1_first_gameday", lambda conn, season: f"{season}-09-07")
    monkeypatch.setattr(
        bt, "preseason_board_rows",
        lambda conn, *, season, week1_date: (f"{season}-09-01", (), {"kept": 0}),
    )

    order: list[int] = []

    def _curve(boards, outcomes, *, training_seasons, **kwargs):
        order.append(len(order))
        seen[order[-1]] = tuple(sorted(training_seasons))
        return bt.ValueCurve(by_position={}, source="stub",
                             training_seasons=tuple(sorted(training_seasons)))

    monkeypatch.setattr(bt, "realized_value_curve", _curve)
    inputs = bt.prepare(db, seasons=(2023,))
    assert set(seen.values()) == {(2021, 2022, 2024, 2025)}
    assert inputs[2023].curve.training_seasons == (2021, 2022, 2024, 2025)


# ---------------------------------------------------------------- statistics


def test_the_season_block_interval_is_a_t_on_FIVE_numbers_not_five_hundred(world):
    """PSEUDO-REPLICATION, made visible instead of disclosed.

    All ``n`` drafts of one season share ONE realized-outcome draw, so the pooled
    interval's ``n`` is drafts, not seasons. This pins the block reading: the mean
    of the five season means with a t on df = 4 (NOT 2, which is what ``pair``'s
    docstring used to claim). Hand-built so the arithmetic is checkable by eye.
    """
    import math

    per_season = {
        2021: -0.587, 2022: +0.046, 2023: +0.484, 2024: -1.373, 2025: -0.102,
    }
    results = []
    for season, delta in per_season.items():
        results.append(bt.SeasonResult(
            season=season, scrape_date="x", board_size=1,
            outcomes={
                "A": tuple(_outcome(objective=delta) for _ in range(100)),
                "B": tuple(_outcome(objective=0.0) for _ in range(100)),
            },
        ))
    block = bt.season_block_interval(results, challenger="A", baseline="B")

    values = list(per_season.values())
    mean = sum(values) / 5
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / 4)
    assert block.n_blocks == 5
    assert block.mean == pytest.approx(mean)
    assert block.mean == pytest.approx(-0.3064, abs=5e-4)
    # t(4) at 97.5% is 2.776; the pooled interval on the same numbers excluded zero.
    half = 2.776 * sd / math.sqrt(5)
    assert block.ci_low == pytest.approx(mean - half, abs=2e-3)
    assert block.ci_high == pytest.approx(mean + half, abs=2e-3)
    assert not block.excludes_zero, (
        "the study's most-quoted negative does not survive a season-block reading"
    )
    assert bt.pair(results, challenger="A", baseline="B").excludes_zero, (
        "...while the pooled reading of the identical numbers does — which is the "
        "whole point of printing both"
    )


def test_identical_per_season_deltas_collapse_the_block_interval_and_never_exclude_zero(world):
    """STAT-6 (draft-backtest analogue): five seasons that all read +0.5 give a
    zero-dispersion block whose interval collapses to the point [+0.5, +0.5].
    That is five agreeing draws, not evidence against zero, so it must print
    ``includes zero`` — the same rule as ``backtest.stats.Interval``."""
    results = [
        bt.SeasonResult(
            season=season, scrape_date="x", board_size=1,
            outcomes={
                "A": tuple(_outcome(objective=0.5) for _ in range(10)),
                "B": tuple(_outcome(objective=0.0) for _ in range(10)),
            },
        )
        for season in (2021, 2022, 2023, 2024, 2025)
    ]
    block = bt.season_block_interval(results, challenger="A", baseline="B")
    assert block.n_blocks == 5 and block.mean == pytest.approx(0.5)
    assert block.ci_low == block.ci_high == pytest.approx(0.5) and block.sd == 0.0
    assert not block.excludes_zero
    assert "includes zero" in bt.format_block(block)
    assert "EXCLUDES ZERO" not in bt.format_block(block)


def _outcome(*, objective: float):
    from ziggurat.draft.evaluate import DraftOutcome

    return DraftOutcome(
        slot=bt.OPERATOR_SLOT, draft_seed=0, objective=objective,
        expected_wins=objective, playoff_prob=0.0, title_prob=0.0, rank=1,
        holes=0, hole_weeks=(), shape={}, field_objective=None,
    )

