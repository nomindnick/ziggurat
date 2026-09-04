"""Item 4.1 — the DECIDE phase (backtest/replay.py) and the freeze (backtest/decisions.py).

The generator is the PRODUCTION `core.candidates.build_candidates`, called at
the week's decision clock; these tests run it on the offline 2023 week-5/6
fixture, add a synthetic market page where a test needs one, and check the
one thing the harness exists to guarantee: what was decided at ``as_of(T)``
cannot change when the future is added to the database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backtest import decisions as D
from backtest import replay as R
from backtest import scorecards as S
from backtest import tune as T
from ziggurat.core import candidates as C
from ziggurat.data.nfl import base, injuries, players, schedules, snap_counts, weekly_stats
from ziggurat.data.store import apply_schema, connect

REPO_ROOT = Path(__file__).resolve().parents[1]
BULK = "2026-07-16"
SEASON, WEEK = 2023, 6
AS_OF = "2023-10-17"   # week 6's last gameday is Monday 2023-10-16
ALL = D.ReplayParams(strategies=D.STRATEGY_NAMES, k=3, seasons=(SEASON,), weeks=(WEEK,))


def _seed(db, nfl_fixture):
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"), retrieved_as_of=BULK)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of=BULK)
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of=BULK)


@pytest.fixture()
def seeded(db, nfl_fixture):
    _seed(db, nfl_fixture)
    return db


@pytest.fixture()
def file_db(tmp_path, nfl_fixture):
    path = tmp_path / "replay.sqlite"
    conn = connect(path)
    apply_schema(conn)
    _seed(conn, nfl_fixture)
    conn.close()
    return path


def _panel_row(db, *, gsis, week, scrape, rank, position="RB", team="FIL", season=SEASON,
               ecr_type="wp", page="ppr-rb", retrieved="2026-08-30"):
    db.execute(
        "INSERT INTO fpecr_panel (fantasypros_id, ecr_type, fp_page, scrape_date, season, "
        "nfl_week, week_basis, player, position, team, gsis_id, page_rank, pos_rank, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"fp-{gsis}-{rank}", ecr_type, page, scrape, season, week, "inferred", gsis, position,
         team, gsis, rank, rank, retrieved, scrape),
    )
    db.commit()


def _game(db, *, season, week, gameday, game_type="REG", retrieved="2026-08-01"):
    db.execute(
        "INSERT INTO schedules (game_id, season, week, game_type, gameday, home_team, "
        "away_team, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{season}_{week}_{gameday}_{game_type}", season, week, game_type, gameday,
         "BUF", "MIA", retrieved, retrieved),
    )
    db.commit()


# ------------------------------------------------------------ the clock


@pytest.mark.parametrize("last_gameday, expected", [
    ("2030-09-09", "2030-09-10"),   # Monday -> the next day
    ("2030-09-08", "2030-09-10"),   # Sunday -> two days on
    ("2030-09-07", "2030-09-10"),   # Saturday
    ("2030-09-05", "2030-09-10"),   # Thursday-only week
    ("2030-09-10", "2030-09-17"),   # a Tuesday game rolls to the FOLLOWING Tuesday
])
def test_decision_clock_is_the_first_tuesday_strictly_after_the_last_reg_game(
    db, last_gameday, expected,
):
    _game(db, season=2030, week=1, gameday="2030-09-04")   # a Wednesday opener
    _game(db, season=2030, week=1, gameday=last_gameday)
    assert R.week_as_of(db, 2030, 1) == expected


def test_decision_clock_ignores_non_reg_games_and_refuses_an_empty_week(db):
    _game(db, season=2030, week=1, gameday="2030-09-08")
    _game(db, season=2030, week=1, gameday="2030-09-20", game_type="POST")
    assert R.week_as_of(db, 2030, 1) == "2030-09-10"
    with pytest.raises(R.NoSchedule):
        R.week_as_of(db, 2030, 2)
    assert R.reg_weeks(db, 2030) == [1]


def test_decision_clock_on_the_fixture_is_the_tuesday_after_the_monday_game(seeded):
    assert R.week_as_of(seeded, SEASON, WEEK) == AS_OF
    assert R.calendar_as_of(SEASON) == "2024-02-28"
    assert R.reg_weeks(seeded, SEASON)[:3] == [1, 2, 3]


# ------------------------------------------------------------- params


def test_params_validate_and_hash_deterministically():
    a = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,))
    b = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,))
    assert a.params_hash == b.params_hash and len(a.cache_key()) == 12
    assert a.params_hash != D.ReplayParams(strategies=("signal_topk",), k=2, seasons=(2023,)).params_hash
    assert a.eligibility_map == D.ELIGIBILITY_HYPOTHESIS
    assert json.dumps(a.to_json(), sort_keys=True)  # JSON-serialisable
    with pytest.raises(ValueError, match="unknown strategy"):
        D.ReplayParams(strategies=("oracle",), k=3, seasons=(2023,))
    with pytest.raises(ValueError):
        D.ReplayParams(strategies=(), k=3, seasons=(2023,))
    with pytest.raises(ValueError):
        D.ReplayParams(strategies=("signal_topk",), k=3, seasons=())
    assert D.split_of(2023) == "TRAIN" and D.split_of(2024) == "HOLDOUT"


# --------------------------------------------------------- strategies


def _row(gsis, *, magnitude, position="RB", volume=None, board_rank=1):
    return R.PoolRow(
        gsis_id=gsis, espn_id=None, player=gsis, position=position, team="BUF",
        signal_kind=C.SIGNAL_USAGE, magnitude=magnitude, board_rank=board_rank,
        market_rank_r0=None, market_page_size_r0=None, r0_scrape_date=None,
        week_volume=volume, reasons=(f"reason {gsis}",),
    )


def test_strategies_pick_at_most_k_in_a_total_order():
    pool = [
        _row("c", magnitude=2.0, volume=9.0),
        _row("a", magnitude=2.0, volume=None),
        _row("b", magnitude=5.0, volume=1.0, position="WR"),
        _row("d", magnitude=1.0, volume=20.0),
    ]
    rng = R.week_rng(0, SEASON, WEEK)
    top = R.STRATEGIES["signal_topk"].pick(pool, k=3, rng=rng)
    assert [r.gsis_id for r in top] == ["b", "a", "c"]           # magnitude, then (pos, gsis)
    vol = R.STRATEGIES["volume_topk"].pick(pool, k=3, rng=rng)
    assert [r.gsis_id for r in vol] == ["d", "c", "b"]           # touches; no stat line ranks last
    assert len(R.STRATEGIES["signal_topk"].pick(pool, k=1, rng=rng)) == 1
    assert len(R.STRATEGIES["signal_topk"].pick(pool[:2], k=3, rng=rng)) == 2


def test_random_k_is_seeded_per_week_and_independent_of_hash_seed():
    pool = [_row(f"p{i}", magnitude=float(i)) for i in range(12)]
    first = R.STRATEGIES["random_k"].pick(pool, k=3, rng=R.week_rng(0, SEASON, WEEK))
    again = R.STRATEGIES["random_k"].pick(pool, k=3, rng=R.week_rng(0, SEASON, WEEK))
    assert [r.gsis_id for r in first] == [r.gsis_id for r in again]
    assert len(first) == 3 and len({r.gsis_id for r in first}) == 3
    other = R.STRATEGIES["random_k"].pick(pool, k=3, rng=R.week_rng(0, SEASON, WEEK + 1))
    assert [r.gsis_id for r in other] != [r.gsis_id for r in first]
    # a string seed hashes through sha512, never through str.__hash__
    code = ("import random; print(random.Random('0:2023:6').random())")
    outs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONHASHSEED": h}, check=True).stdout
        for h in ("1", "2")
    }
    assert len(outs) == 1


# ---------------------------------------------------------- the pool


def _board(rows, *, season=SEASON, week=WEEK):
    return C.CandidateBoard(rows=tuple(rows), week=week, freshness=(), notes=(),
                            as_of=AS_OF, season=season)


def _crow(gsis, *, kind=C.SIGNAL_USAGE, position="RB", magnitude=1.0):
    return C.CandidateRow(
        player_key=gsis or "espn:1", player=gsis or "nobody", position=position, team="BUF",
        gsis_id=gsis, espn_id=None, signal_kind=kind, magnitude=magnitude, week=WEEK,
        prior_week=WEEK - 1, hypothesis=kind == C.SIGNAL_QB1, reasons=("r",),
    )


def test_pool_excludes_vacancies_hypotheses_unjoinable_and_market_priced_rows(db):
    _panel_row(db, gsis="priced", week=WEEK, scrape="2023-10-13", rank=10)
    _panel_row(db, gsis="cheap", week=WEEK, scrape="2023-10-13", rank=30)
    # a page scraped AFTER the decision clock must not price anyone
    _panel_row(db, gsis="late", week=WEEK, scrape="2023-10-18", rank=1)
    board = _board([
        _crow("priced", magnitude=9.0),
        _crow("cheap", magnitude=8.0),
        _crow("late", magnitude=7.0),
        _crow("unranked", magnitude=6.0),
        _crow(None, magnitude=5.0),
        _crow("qb", kind=C.SIGNAL_QB1, position="QB"),
        _crow("hurt", kind=C.SIGNAL_INJURY),
        _crow("thrower", position="QB", magnitude=4.0),
    ])
    refs = S.ReferenceCache(db, as_of=AS_OF)
    build = R.build_pool(board, params=ALL, refs=refs, as_of=AS_OF, volumes={"cheap": 12.0})
    assert [r.gsis_id for r in build.pool] == ["cheap", "late", "unranked"]
    assert (build.excluded_injury, build.excluded_qb1, build.excluded_no_gsis,
            build.excluded_position, build.excluded_ineligible) == (1, 1, 1, 1, 1)
    assert build.generator_rows == 8 and build.usage_rows == 6
    assert build.r0_scrape_date == "2023-10-13" and build.r0_pages == ("ppr-rb",)
    by = {r.gsis_id: r for r in build.pool}
    assert (by["cheap"].market_rank_r0, by["cheap"].market_page_size_r0) == (30, 2)
    assert by["late"].market_rank_r0 is None      # the late scrape was invisible
    assert by["cheap"].week_volume == 12.0 and by["unranked"].week_volume is None
    # board_rank counts USAGE rows in generator order, so it survives the exclusions
    assert [r.board_rank for r in build.pool] == [2, 3, 4]


# ---------------------------------------------------- decide_week live


def test_decide_week_runs_the_production_generator_and_keeps_its_reasons_verbatim(seeded):
    recs = R.decide_week(seeded, season=SEASON, week=WEEK, params=ALL)
    assert [r.strategy for r in recs] == list(D.STRATEGY_NAMES)
    assert all(r.status == D.WEEK_DECIDED and r.as_of == AS_OF for r in recs)
    top = recs[0]
    assert 1 <= len(top.decisions) <= 3 and top.pool_size >= len(top.decisions)
    # the same board, read directly, carries the same rows and the same words
    board = base.latest_truth(C.build_candidates)(seeded, as_of=AS_OF, season=SEASON, week=WEEK)
    by_gsis = {r.gsis_id: r for r in board.rows if r.signal_kind == C.SIGNAL_USAGE}
    for d in top.decisions:
        assert d.reasons == tuple(by_gsis[d.gsis_id].reasons)
        assert d.signal_kind == C.SIGNAL_USAGE and d.position in ALL.positions
    mags = [d.magnitude for d in top.decisions]
    assert mags == sorted(mags, reverse=True)
    # random_k and volume_topk draw from the same pool: same counts, same clock
    assert {r.pool_size for r in recs} == {top.pool_size}
    assert all(len(r.decisions) == len(top.decisions) for r in recs)


def test_weeks_the_generator_cannot_serve_are_recorded_never_skipped(seeded, monkeypatch):
    # week 1: schedule exists, no box score -> no_box_score
    (rec,) = R.decide_week(seeded, season=SEASON, week=1,
                           params=D.ReplayParams(strategies=("signal_topk",), k=3,
                                                 seasons=(SEASON,)))
    assert rec.status == R.WEEK_NO_BOX_SCORE and rec.decisions == ()
    assert "no weekly_stats rows" in rec.reason and rec.as_of == "2023-09-12"
    # no schedule at all -> no_schedule, with the clock left blank
    (rec,) = R.decide_week(seeded, season=2019, week=3,
                           params=D.ReplayParams(strategies=("signal_topk",), k=3,
                                                 seasons=(2019,)))
    assert rec.status == D.WEEK_NO_SCHEDULE and rec.as_of == ""
    # the generator raising is a generator_failed week naming the exception
    def boom(conn, *, as_of, season, week, view="historical", **kw):
        raise C.NoCompletedWeek("synthetic failure")
    monkeypatch.setattr(C, "build_candidates", boom)
    recs = R.decide_week(seeded, season=SEASON, week=WEEK, params=ALL)
    assert [r.status for r in recs] == [D.WEEK_GENERATOR_FAILED] * 3
    assert recs[0].reason == "NoCompletedWeek: synthetic failure"
    # ... and the CLI refuses to call an all-empty season a result
    all_empty = R.replay(seeded, ALL)
    assert all(r.decisions == () for r in all_empty)


def test_empty_pool_is_its_own_status_with_the_exclusion_counts(seeded, monkeypatch):
    board = _board([_crow("hurt", kind=C.SIGNAL_INJURY), _crow(None)])
    monkeypatch.setattr(C, "build_candidates",
                        lambda conn, *, as_of, season, week, view="historical", **kw: board)
    recs = R.decide_week(seeded, season=SEASON, week=WEEK, params=ALL)
    assert recs[0].status == D.WEEK_EMPTY_POOL
    assert "injury=1" in recs[0].reason and "no_gsis=1" in recs[0].reason
    assert recs[0].generator_rows == 2 and recs[0].usage_rows == 1


def test_generator_log_lines_are_captured_and_normalised(seeded):
    recs = R.decide_week(seeded, season=SEASON, week=WEEK, params=ALL)
    for text, count in recs[0].log_lines:
        assert count >= 1 and not any(ch.isdigit() for ch in text.split(":", 1)[1])
    assert R.normalise_log_line("crosswalk: espn_id 12345 maps to multiple gsis (a, b); x") == \
        "crosswalk: espn_id # maps to multiple gsis (...); x"


# ------------------------------------------------------------ leakage


def test_frozen_decisions_do_not_move_when_the_future_lands(seeded):
    top_gsis = None
    before = R.replay(seeded, ALL)
    digest = D.records_digest(before)
    assert any(r.decisions for r in before)
    top_gsis = next(r for r in before if r.decisions).decisions[0].gsis_id
    # the future: next week's market page, next week's box score, a re-pull of
    # this week's page dated after the clock, and a next-week ownership row
    _panel_row(seeded, gsis=top_gsis, week=WEEK + 1, scrape="2023-10-20", rank=1)
    _panel_row(seeded, gsis=top_gsis, week=WEEK, scrape="2023-10-18", rank=1)
    seeded.execute(
        "INSERT INTO weekly_stats (player_id, season, week, season_type, position, recent_team, "
        "carries, targets, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (top_gsis, SEASON, WEEK + 1, "REG", "RB", "BUF", 30, 10, BULK, "2023-10-22"),
    )
    seeded.execute(
        "INSERT INTO sleeper_ownership (season, season_type, week, sleeper_id, gsis_id, position, "
        "team, owned_pct, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (SEASON, "regular", WEEK + 1, "s1", top_gsis, "RB", "BUF", 99.0, "2026-09-01",
         "2023-10-23"),
    )
    # ... and a week-T injury report for the top pick that became knowable the
    # day AFTER the clock (LEAK-2): the generator's injury arm would rewrite the
    # pick's own reasons if it could see it, so this is a real bite
    _injury(seeded, gsis=top_gsis, knowable="2023-10-18")
    seeded.commit()
    after = R.replay(seeded, ALL)
    assert D.records_digest(after) == digest
    assert [D.record_to_json(r) for r in after] == [D.record_to_json(r) for r in before]


def _injury(db, *, gsis, knowable, status="Out", retrieved="2026-07-17"):
    """A week-T nflverse injury row for ``gsis`` (retrieved a day after the
    fixture's bulk stamp so it wins the key under latest_truth)."""
    db.execute(
        "INSERT INTO injuries (gsis_id, season, week, team, position, full_name, report_status, "
        "report_primary_injury, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (gsis, SEASON, WEEK, "BUF", "RB", gsis, status, "Knee", retrieved, knowable),
    )
    db.commit()


def test_an_injury_report_knowable_before_the_clock_does_change_the_decisions(seeded):
    # the CONTROL for LEAK-2: the same row, knowable a day BEFORE the clock, is
    # visible to the injury arm and moves the freeze — so the test above is
    # not passing because the generator ignores the injuries table
    before = R.replay(seeded, ALL)
    top = next(r for r in before if r.decisions).decisions[0]
    _injury(seeded, gsis=top.gsis_id, knowable="2023-10-16")
    after = R.replay(seeded, ALL)
    assert D.records_digest(after) != D.records_digest(before)


def test_the_generator_is_called_at_the_week_clock_and_nowhere_else(seeded, monkeypatch):
    # LEAK-2 (spy): whatever as_of the generator receives IS the decision clock
    seen = []
    def spy(conn, *, as_of, season, week, view="historical", **kw):
        seen.append((as_of, season, week, view))
        return _board([_crow("someone", magnitude=3.0)])
    monkeypatch.setattr(C, "build_candidates", spy)
    R.decide_week(seeded, season=SEASON, week=WEEK, params=ALL)
    assert seen and {s[0] for s in seen} == {R.week_as_of(seeded, SEASON, WEEK)} == {AS_OF}
    assert all(s[1:] == (SEASON, WEEK, "latest_truth") for s in seen)


def test_a_page_knowable_before_the_clock_does_change_the_pool(seeded):
    # the control for the leakage test: a week-T page scraped BEFORE as_of is
    # visible and prices the top pick out of eligibility
    before = R.replay(seeded, ALL)
    top = next(r for r in before if r.decisions).decisions[0]
    _panel_row(seeded, gsis=top.gsis_id, week=WEEK, scrape="2023-10-13", rank=1,
               position=top.position, page=f"ppr-{top.position.lower()}")
    after = R.replay(seeded, ALL)
    assert D.records_digest(after) != D.records_digest(before)
    assert next(r for r in after if r.decisions).excluded_ineligible == 1
    assert top.gsis_id not in {d.gsis_id for r in after for d in r.decisions}


def test_grade_clock_must_be_after_the_decision_clock(seeded):
    recs = R.replay(seeded, ALL)
    with pytest.raises(S.GradeInputError):
        S.build_scorecard(seeded, recs, ALL, strategy="signal_topk", market="wp",
                          grade_as_of=AS_OF)


# ------------------------------------------------- generator seam (SEAM-1)


def _tightened(mult):
    """Every shipped floor scaled by ``mult`` — a strictly stricter generator.

    A SHARE floor is capped just below 1.0: at 2x the shipped
    ``emergence:offense_pct`` (0.55) would reach 1.1, which item 4.2's A1
    refuses at the parameters because a share can never clear it (the axis
    would be silently disabled, not tightened — breakout-backtest.md §3.6).
    """
    shipped = D.default_generator()

    def scaled(m, v):
        return min(v * mult, 0.95) if m in D.SHARE_FLOORS else v * mult

    return R.generator_setting(
        [(m, scaled(m, v)) for m, v in shipped if not m.startswith(D.EMERGENCE_PREFIX)],
        [(m[len(D.EMERGENCE_PREFIX):], scaled(m, v)) for m, v in shipped
         if m.startswith(D.EMERGENCE_PREFIX)],
    )


def test_the_default_cache_key_is_pinned_and_a_moved_floor_changes_it(seeded, tmp_path):
    # SEAM-1 (c): the shipped floors are hashed into the key, so an edit to
    # core/candidates.DEFAULT_BREAKOUT / EMERGENCE_FLOORS orphans every freeze
    default = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,))
    assert default.generator_is_default and default.cache_key() == "66c0e83d7da3"
    assert dict(default.generator)["targets"] == 4.0
    # SEAM-1 (a): one floor moved -> a different key -> an unreachable freeze
    other = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,),
                           generator=R.generator_setting([("targets", 5.0)], None))
    assert not other.generator_is_default and other.cache_key() == "a4ccd4c04a9d"
    assert other.cache_key() != default.cache_key()
    assert "targets>=5 (shipped 4)" in other.generator_label
    recs = R.replay(seeded, ALL)
    D.freeze(recs, ALL, cache_dir=tmp_path, written_at="now")
    moved = D.ReplayParams(strategies=D.STRATEGY_NAMES, k=3, seasons=(SEASON,), weeks=(WEEK,),
                           generator=R.generator_setting([("targets", 5.0)], None))
    with pytest.raises(FileNotFoundError):
        D.load(tmp_path, moved)
    with pytest.raises(ValueError, match="unknown generator floor"):
        D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,),
                       generator=(("touchdowns", 1.0),))


def test_a_tightened_generator_setting_reaches_the_generator_and_shrinks_the_pool(seeded):
    # SEAM-1 (b): the floors in the params are the floors the generator ran
    # with — a stricter setting yields a smaller pool and different picks
    loose = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(SEASON,), weeks=(WEEK,))
    tight = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(SEASON,), weeks=(WEEK,),
                           generator=_tightened(2.0))
    (a,) = R.decide_week(seeded, season=SEASON, week=WEEK, params=loose)
    (b,) = R.decide_week(seeded, season=SEASON, week=WEEK, params=tight)
    assert a.status == b.status == D.WEEK_DECIDED
    assert 0 < b.pool_size < a.pool_size
    assert [d.gsis_id for d in a.decisions] != [d.gsis_id for d in b.decisions]
    thresholds, emergence = R.generator_thresholds(tight)
    assert thresholds.floors["carries"] == 12.0 and emergence["carries"] == 20.0
    assert thresholds.label == R.OVERRIDDEN_BREAKOUT_LABEL
    assert R.generator_thresholds(loose) == (C.DEFAULT_BREAKOUT, C.EMERGENCE_FLOORS)


def test_cli_floor_flags_land_in_the_params_and_refuse_bad_metrics(capsys):
    args = R.build_parser().parse_args(
        ["--breakout-floor", "carries=8", "--emergence-floor", "targets=7.5"])
    setting = dict(R.generator_setting(args.breakout_floor, args.emergence_floor))
    assert setting["carries"] == 8.0 and setting["emergence:targets"] == 7.5
    assert setting["targets"] == 4.0
    with pytest.raises(SystemExit):
        R.build_parser().parse_args(["--breakout-floor", "carries"])
    assert "METRIC=VALUE" in capsys.readouterr().err
    assert R.main(["--db", "/nonexistent.sqlite", "--breakout-floor", "touchdowns=1",
                   "--seasons", "2023"]) == 2
    assert "unknown generator floor" in capsys.readouterr().err


# ------------------------------------------- item 4.2: floor admissibility (A1)


def _params(**over):
    return D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,),
                          generator=R.generator_setting(
                              over.get("breakout"), over.get("emergence")))


@pytest.mark.parametrize("metric, value", [
    ("carries", 0.0), ("carries", -5.0), ("targets", 0.0), ("receiving_yards", -0.5),
    ("target_share", 0.0), ("target_share", -0.01), ("offense_pct", 0.0),
    ("carries", float("nan")),
])
def test_a_zero_or_negative_floor_is_refused_at_the_parameters(metric, value):
    # breakout-backtest.md §3.6: a floor of 0 is a ZeroDivisionError the
    # generator swallows per week into generator_failed (exit 0 today), and a
    # negative floor INVERTS that metric's ranking (also exit 0 today).  The
    # refusal names WHY, and sits at the parameters so no run reaches a key.
    with pytest.raises(ValueError) as exc:
        _params(breakout=[(metric, value)])
    msg = str(exc.value)
    assert metric in msg and "delta/floor" in msg
    assert "divides by zero" in msg and "inverts" in msg
    # the same value through the emergence map is refused too (the prefix is
    # the only thing that decides which map a name lands in — §3.7)
    with pytest.raises(ValueError, match="strictly > 0"):
        _params(emergence=[("carries", value)])
    # ... and via the CLI it is a sentence and exit 2, not a traceback
    assert R.main(["--db", "/nonexistent.sqlite", "--seasons", "2023",
                   "--breakout-floor", f"{metric}={value}"]) == 2


@pytest.mark.parametrize("metric, value, emergence", [
    ("target_share", 1.0, False), ("target_share", 1.5, False),
    ("air_yards_share", 1.0, False), ("offense_pct", 1.0, False),
    ("offense_pct", 1.1, True),   # emergence:offense_pct
])
def test_a_share_floor_of_one_or_more_is_refused(metric, value, emergence):
    # a share cannot clear 1.0, so the axis would be silently DISABLED rather
    # than tightened (measured: emergence:offense_pct=1.1 changes 0 weeks)
    kw = {"emergence": [(metric, value)]} if emergence else {"breakout": [(metric, value)]}
    with pytest.raises(ValueError) as exc:
        _params(**kw)
    msg = str(exc.value)
    assert metric in msg and "< 1.0" in msg and "disabled" in msg
    # a count floor is NOT a share: 1.0 and far above are legal there
    ok = _params(breakout=[("carries", 1.0), ("targets", 250.0)])
    assert dict(ok.generator)["targets"] == 250.0
    # a share strictly below 1.0 is legal, however close
    close = {"emergence": [(metric, 0.999)]} if emergence else {"breakout": [(metric, 0.999)]}
    name = (D.EMERGENCE_PREFIX + metric) if emergence else metric
    assert dict(_params(**close).generator)[name] == 0.999
    assert D.SHARE_FLOORS == {"target_share", "air_yards_share", "offense_pct",
                              "emergence:offense_pct"}


def test_the_refusal_does_not_move_any_existing_cache_key():
    # the validation adds no field: the pinned keys of
    # test_the_default_cache_key_is_pinned_and_a_moved_floor_changes_it hold,
    # and a legal tightening still hashes as before
    default = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,))
    assert default.cache_key() == "66c0e83d7da3"
    other = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,),
                           generator=R.generator_setting([("targets", 5.0)], None))
    assert other.cache_key() == "a4ccd4c04a9d"


def test_the_overridden_floor_label_says_it_is_a_tuning_setting_under_evaluation():
    # breakout-backtest.md §11.4 item 1 (PF-15): the label is stamped into the
    # frozen JSONL of every 4.2 cell, and the 4.1 wording called an overridden
    # setting "not tuned" — the opposite of what it is
    label = R.OVERRIDDEN_BREAKOUT_LABEL
    assert "tuning" in label and "under evaluation" in label and "4.2" in label
    assert "not tuned" not in label.lower()
    assert "\n" not in label
    assert "hypothesis" in label  # Rule 6: still a labelled hypothesis
    thresholds, _ = R.generator_thresholds(_params(breakout=[("carries", 9.0)]))
    assert thresholds.label == label
    assert "4.2" in R.OVERRIDDEN_BREAKOUT_SOURCE


# -------------------------------------------------------- determinism


def test_replay_is_deterministic_in_process_and_across_hash_seeds(file_db):
    conn = R.open_ro(file_db)
    a = D.records_digest(R.replay(conn, ALL))
    b = D.records_digest(R.replay(conn, ALL))
    assert a == b
    code = (
        "import sys; from backtest import replay as R, decisions as D; "
        "conn = R.open_ro(sys.argv[1]); "
        "p = D.ReplayParams(strategies=D.STRATEGY_NAMES, k=3, seasons=(2023,), weeks=(6,)); "
        "print(D.records_digest(R.replay(conn, p)))"
    )
    outs = set()
    for h in ("0", "1", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", code, str(file_db)], capture_output=True, text=True,
            cwd=REPO_ROOT, env={**os.environ, "PYTHONHASHSEED": h}, check=True,
        )
        outs.add(proc.stdout.strip())
    assert outs == {a}


def test_open_ro_cannot_write(file_db):
    conn = R.open_ro(file_db)
    with pytest.raises(Exception, match="readonly|read-only"):
        conn.execute("DELETE FROM players")


# ------------------------------------------------------------- freeze


def test_freeze_and_load_round_trip_and_refuse_to_clobber(seeded, tmp_path):
    recs = R.replay(seeded, ALL)
    target = D.freeze(recs, ALL, cache_dir=tmp_path, written_at="2026-09-01T00:00:00+00:00",
                      db_path="/x/y.sqlite")
    assert target == D.freeze_dir(tmp_path, ALL)
    names = sorted(p.name for p in target.iterdir())
    assert names == sorted([D.file_name(s, 3, SEASON) for s in D.STRATEGY_NAMES] + ["manifest.json"])
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["params_hash"] == ALL.params_hash and manifest["db_path"] == "/x/y.sqlite"
    loaded = D.load(tmp_path, ALL)
    assert loaded == sorted(recs, key=D.record_key)
    assert D.records_digest(loaded) == D.records_digest(recs)
    with pytest.raises(FileExistsError):
        D.freeze(recs, ALL, cache_dir=tmp_path, written_at="later")
    D.freeze(recs, ALL, cache_dir=tmp_path, written_at="later", force=True)
    # a different parameter set is a different directory, and loading it is refused
    other = D.ReplayParams(strategies=D.STRATEGY_NAMES, k=2, seasons=(SEASON,), weeks=(WEEK,))
    with pytest.raises(FileNotFoundError):
        D.load(tmp_path, other)
    # a tampered file fails its checksum
    victim = target / D.file_name("signal_topk", 3, SEASON)
    old = victim.read_text()
    new = old.replace('"rank_in_board":1', '"rank_in_board":2', 1)
    assert new != old, "the tamper must change bytes, or the check below proves nothing"
    victim.write_text(new)
    with pytest.raises(ValueError, match="sha256|checksum|digest"):
        D.load(tmp_path, ALL)
    assert D.freeze_status(tmp_path, ALL).startswith("corrupt: ")


def test_record_json_round_trip_is_bytewise_stable(seeded):
    recs = R.replay(seeded, ALL)
    for rec in recs:
        line = D.record_to_json(rec)
        assert D.record_from_json(line) == rec
        assert D.record_to_json(D.record_from_json(line)) == line
        assert "elapsed" not in line and "written_at" not in line


# ---------------------------------------------------------------- CLI


def test_cli_prints_both_markets_and_exits_2_on_an_empty_season(file_db, tmp_path, capsys):
    argv = ["--db", str(file_db), "--seasons", "2023", "--weeks", "6",
            "--strategy", "signal_topk,random_k", "--k", "2",
            "--grade-as-of", "2024-02-28", "--cache-dir", str(tmp_path / "cache")]
    assert R.main(argv) == 0
    out = capsys.readouterr().out
    assert "market=wp" in out and "market=ros" in out
    assert "TRAIN 2021-23" in out and "COMPARISON" in out
    assert "frozen under" in out
    assert R.main(argv + ["--grade-only"]) == 0
    assert "loaded 2 frozen week records" in capsys.readouterr().out
    assert not (tmp_path / "cache" / D.UNLOCK_LEDGER).exists()
    # week 1 has a schedule and no box score: zero decisions is exit 2, not a scorecard of nothing
    assert R.main(["--db", str(file_db), "--seasons", "2023", "--weeks", "1", "--no-freeze",
                   "--grade-as-of", "2024-02-28", "--cache-dir", str(tmp_path / "cache")]) == 2
    err = capsys.readouterr().err
    assert "EXIT 2: zero decisions" in err


def test_a_test_that_omits_cache_dir_cannot_reach_the_canonical_caches(file_db, tmp_path,
                                                                        capsys):
    # Measured 2026-09-03: the invocation above, run WITHOUT --cache-dir, wrote
    # two fixture rows to data/backtest/replay/grade-log.jsonl — the item-4.2
    # audit trail — on every full-suite run (the grade log is appended before
    # the zero-decision exit, and the parser default is the canonical dir).
    # conftest.py now redirects both modules' DEFAULT_CACHE_DIR for every test;
    # this pins the redirect, and pins that the FROZEN names still point at the
    # real paths so the README / Appendix A checks read the truth.
    import backtest.tune as T
    assert R.DEFAULT_CACHE_DIR != REPO_ROOT / "data" / "backtest" / "replay"
    assert not str(R.DEFAULT_CACHE_DIR).startswith(str(REPO_ROOT))
    assert T.DEFAULT_CACHE_DIR != T.SEARCH_CACHE_DIR
    assert T.CANONICAL_CACHE_DIR == REPO_ROOT / "data" / "backtest" / "replay"
    assert T.SEARCH_CACHE_DIR == REPO_ROOT / "data" / "backtest" / "replay-4.2"
    assert R.build_parser().get_default("cache_dir") == str(R.DEFAULT_CACHE_DIR)
    assert T.build_parser().get_default("cache_dir") == str(T.DEFAULT_CACHE_DIR)
    canonical_log = T.CANONICAL_CACHE_DIR / R.GRADE_LOG
    before = canonical_log.read_bytes() if canonical_log.exists() else None
    assert R.main(["--db", str(file_db), "--seasons", "2023", "--weeks", "1", "--no-freeze",
                   "--grade-as-of", "2024-02-28"]) == 2
    capsys.readouterr()
    after = canonical_log.read_bytes() if canonical_log.exists() else None
    assert after == before
    rows = (R.DEFAULT_CACHE_DIR / R.GRADE_LOG).read_text().splitlines()
    assert len(rows) == 2 and {json.loads(r)["market"] for r in rows} == {"wp", "ros"}


def _argv(file_db, cache, **over):
    base = {"--db": str(file_db), "--seasons": "2023", "--weeks": "6",
            "--strategy": "signal_topk", "--k": "2", "--grade-as-of": "2024-02-28",
            "--cache-dir": str(cache)}
    base.update({("--" + k.replace("_", "-")): v for k, v in over.items()})
    return [x for k, v in base.items() for x in (k, v)]


def test_rerunning_the_same_command_reuses_a_verifying_freeze(file_db, tmp_path, monkeypatch,
                                                              capsys):
    # RERUN-1 / RULE3-1: the done-when command is re-runnable — a freeze for
    # the same params is reused, said so, and graded; --force re-decides; a
    # freeze that does not verify is refused by name, never a traceback
    cache = tmp_path / "cache"
    argv = _argv(file_db, cache)
    calls = []
    real = R.replay
    monkeypatch.setattr(R, "replay", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    assert R.main(argv) == 0
    assert "frozen under" in capsys.readouterr().out and calls == [1]
    assert R.main(argv) == 0
    out = capsys.readouterr().out
    assert "reusing the freeze under" in out and "pass --force to re-decide" in out
    assert "frozen under" not in out and "precision@2" in out and calls == [1]
    assert R.main(argv + ["--grade-only"]) == 0 and calls == [1]
    assert "loaded 1 frozen week records" in capsys.readouterr().out
    assert R.main(argv + ["--force"]) == 0 and calls == [1, 1]
    assert "frozen under" in capsys.readouterr().out
    # a freeze that no longer verifies: refused with the flag named, exit 2
    params = D.ReplayParams(strategies=("signal_topk",), k=2, seasons=(2023,), weeks=(6,))
    victim = D.freeze_dir(cache, params) / D.file_name("signal_topk", 2, 2023)
    victim.write_text(victim.read_text().replace('"rank_in_board":1', '"rank_in_board":2', 1))
    assert R.main(argv) == 2 and calls == [1, 1]
    err = capsys.readouterr().err
    assert "does not verify" in err and "--force" in err and "sha256" in err
    assert R.main(argv + ["--grade-only"]) == 2
    assert "does not verify" in capsys.readouterr().err
    assert R.main(argv + ["--force"]) == 0 and calls == [1, 1, 1]
    assert R.main(argv) == 0 and calls == [1, 1, 1]
    # --grade-only with nothing frozen is a sentence, not a FileNotFoundError
    assert R.main(_argv(file_db, tmp_path / "empty") + ["--grade-only"]) == 2
    assert "no freeze under" in capsys.readouterr().err


# ------------------------------------------------------ holdout lock


def _holdout_record(season=2024, week=6):
    d = D.Decision(
        season=season, week=week, as_of="2024-10-15", strategy="signal_topk", rank_in_board=1,
        board_rank=1, gsis_id="00-0000001", espn_id=None, player="Someone", position="RB",
        team="BUF", signal_kind=C.SIGNAL_USAGE, magnitude=1.0, market_rank_r0=None,
        market_page_size_r0=None, r0_scrape_date=None, reasons=("r",),
    )
    return D.WeekRecord(
        season=season, week=week, as_of="2024-10-15", strategy="signal_topk", k=2,
        status=D.WEEK_DECIDED, reason=None, decisions=(d,), generator_rows=1, usage_rows=1,
        pool_size=1, excluded_injury=0, excluded_qb1=0, excluded_ineligible=0,
        excluded_no_gsis=0, excluded_position=0, r0_scrape_date=None, r0_pages=(),
        log_lines=(),
    )


def test_holdout_seasons_are_refused_below_the_cli_too(seeded, tmp_path):
    train = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,), weeks=(WEEK,))
    held = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023, 2025), weeks=(WEEK,))
    assert D.require_holdout_unlock(train.seasons, unlock_holdout=False) == ()
    assert D.require_holdout_unlock(held.seasons, unlock_holdout=True) == (2025,)
    with pytest.raises(D.HoldoutLocked, match=r"2025.*HOLDOUT"):
        D.require_holdout_unlock(held.seasons, unlock_holdout=False)
    with pytest.raises(D.HoldoutLocked):
        R.replay(seeded, held)
    with pytest.raises(D.HoldoutLocked):
        R.decide_week(seeded, season=2025, week=WEEK, params=held)
    with pytest.raises(D.HoldoutLocked):
        D.load(tmp_path, held)          # refused before the (absent) freeze is looked for
    with pytest.raises(D.HoldoutLocked):
        S.build_scorecard(seeded, [_holdout_record(2025)], held, strategy="signal_topk",
                          market="wp", grade_as_of="2026-02-28")
    # a TRAIN params set carrying a HOLDOUT record is refused on the record
    with pytest.raises(D.HoldoutLocked):
        S.build_scorecard(seeded, [_holdout_record(2025)], train, strategy="signal_topk",
                          market="wp", grade_as_of="2026-02-28")
    assert not (tmp_path / D.UNLOCK_LEDGER).exists()


def test_cli_holdout_lock_refuses_without_the_flag_and_ledgers_only_a_completed_run(
    file_db, tmp_path, monkeypatch, capsys,
):
    cache = tmp_path / "cache"
    ledger = cache / D.UNLOCK_LEDGER
    argv = _argv(file_db, cache, seasons="2024", grade_as_of="2025-02-28")
    opened = []
    real_open = R.open_ro
    monkeypatch.setattr(R, "open_ro", lambda path: (opened.append(path), real_open(path))[1])
    # refused BEFORE the database is opened, for decide and for --grade-only alike
    assert R.main(argv) == 2
    assert "HOLDOUT" in capsys.readouterr().err and opened == []
    assert R.main(argv + ["--grade-only"]) == 2
    assert "HOLDOUT" in capsys.readouterr().err and opened == []
    assert not ledger.exists()
    # the default season list is TRAIN, so a bare run never asks for holdout
    assert R.build_parser().parse_args([]).seasons == "2021,2022,2023"
    assert R._parse_seasons("2021-2023,2025") == (2021, 2022, 2023, 2025)
    # unlocked: the decide phase runs (synthetic 2024 records — the fixture has
    # no 2024 data), the grade completes, and ONLY THEN is the ledger written
    monkeypatch.setattr(R, "replay", lambda conn, params, **kw: [_holdout_record()])
    with monkeypatch.context() as failing:
        def boom(*a, **k):
            raise S.GradeInputError("synthetic grade failure")
        failing.setattr(S, "build_scorecard", boom)
        assert R.main(argv + ["--unlock-holdout"]) == 2
        assert "synthetic grade failure" in capsys.readouterr().err
    assert not ledger.exists(), "a run that did not complete must not claim an unlock"
    # the failed run had already frozen its decisions (freeze precedes grade),
    # so the completing run reuses them: phase 'grade', still a logged unlock
    assert R.main(argv + ["--unlock-holdout"]) == 0
    out = capsys.readouterr().out
    assert "HOLDOUT unlocked for seasons (2024,)" in out and "HOLDOUT unlock recorded" in out
    assert "reusing the freeze under" in out and "HOLDOUT 2024-25" in out
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["holdout_seasons"] == [2024]
    params = D.ReplayParams(strategies=("signal_topk",), k=2, seasons=(2024,), weeks=(6,))
    assert rows[0]["params_hash"] == params.params_hash and rows[0]["phase"] == "grade"
    assert rows[0]["argv"] == argv + ["--unlock-holdout"] and rows[0]["written_at"]
    # a forced re-decide and a grade-only re-read are each a further unlock
    assert R.main(argv + ["--unlock-holdout", "--force"]) == 0
    assert R.main(argv + ["--unlock-holdout", "--grade-only"]) == 0
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["phase"] for r in rows] == ["grade", "decide+grade", "grade"]
    assert len({r["params_hash"] for r in rows}) == 1
    # ... while the frozen holdout decisions stay locked to a flag-less reader
    assert R.main(argv + ["--grade-only"]) == 2 and len(ledger.read_text().splitlines()) == 3


def test_a_train_run_never_touches_the_unlock_ledger(file_db, tmp_path, capsys):
    cache = tmp_path / "cache"
    assert R.main(_argv(file_db, cache)) == 0
    assert R.main(_argv(file_db, cache) + ["--grade-only"]) == 0
    out = capsys.readouterr().out
    assert "HOLDOUT unlock" not in out and not (cache / D.UNLOCK_LEDGER).exists()


# -------------------------------------------- item 4.2: per-week vector file


def _train_params():
    return D.ReplayParams(strategies=("signal_topk",), k=2, seasons=(2023,), weeks=(6,))


def test_main_writes_the_per_week_vector_beside_the_freeze(file_db, tmp_path, capsys):
    # §11.4 item 2: a TRAIN-only graded run leaves per-week-lifts.json in the
    # freeze dir, split TRAIN, keyed "<season>,<week>", carrying H / owned-delta /
    # grade_as_of / cache_key / strategy / market.  Mutant tried: dropping the
    # write_per_week_lifts call from main() — this test fails on the missing file.
    cache = tmp_path / "cache"
    assert R.main(_argv(file_db, cache)) == 0
    out = capsys.readouterr().out
    path = D.freeze_dir(cache, _train_params()) / R.PER_WEEK_FILE
    assert path.exists() and str(path) in out
    assert not path.with_name(path.name + ".tmp").exists()
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["split"] == "TRAIN" and body["strategy"] == "signal_topk"
    assert body["market"] == "wp" and body["hit_places"] == S.HIT_PLACES_DEFAULT
    assert body["owned_delta"] == S.OWNED_DELTA_DEFAULT and body["grade_as_of"] == "2024-02-28"
    assert body["cache_key"] == _train_params().cache_key()
    assert set(body["per_week_lift_depth"]) <= {"2023,6"}
    assert body["n_weeks"] == len(body["per_week_lift_depth"])
    for key in body["per_week_lift_depth"]:
        season, week = key.split(",")
        assert (int(season), int(week)) == (2023, 6)
    # the extra file does not break the freeze's own verification, and a
    # --grade-only re-read rewrites it (every graded run writes it)
    assert D.freeze_status(cache, _train_params()) == D.FREEZE_OK
    path.unlink()
    assert R.main(_argv(file_db, cache) + ["--grade-only"]) == 0
    assert path.exists()
    # --no-freeze writes no cache, so no vector file either — said so
    assert R.main(_argv(file_db, tmp_path / "nofreeze") + ["--no-freeze"]) == 0
    assert f"{R.PER_WEEK_FILE} not written: --no-freeze" in capsys.readouterr().out
    # --no-freeze writes no FREEZE; the one thing under the cache dir is the §6.4
    # grade log, which every build_scorecard call owes an auditor (§11.5 F8)
    assert [p.name for p in (tmp_path / "nofreeze").iterdir()] == [R.GRADE_LOG]


def test_per_week_vector_file_matches_the_card_it_was_graded_from(file_db, tmp_path,
                                                                  monkeypatch):
    # the file's mapping is the card's own per_week_depth_lift("TRAIN") — no
    # arithmetic of the CLI's.  Mutant tried: writing the ALL split's vector
    # (identical on TRAIN-only) is caught by the split label; writing
    # `lift_depth_pooled.mean` per week instead of per_week_depth_lift is caught
    # by the equality against the captured card.
    cache = tmp_path / "cache"
    cards = []
    real = S.build_scorecard
    monkeypatch.setattr(S, "build_scorecard",
                        lambda *a, **k: (lambda c: (cards.append(c), c)[1])(real(*a, **k)))
    assert R.main(_argv(file_db, cache, market="wp")) == 0
    (card,) = cards
    body = json.loads((D.freeze_dir(cache, _train_params()) / R.PER_WEEK_FILE).read_text())
    expected = {f"{s},{w}": v for (s, w), v in card.per_week_depth_lift("TRAIN").items()}
    assert body["per_week_lift_depth"] == expected
    assert body == R.per_week_lifts_payload(card, _train_params(), split="TRAIN")
    with pytest.raises(S.GradeInputError, match="HOLDOUT"):
        card.split("HOLDOUT")


def test_the_vector_file_is_written_before_the_ledger_row(file_db, tmp_path, monkeypatch,
                                                          capsys):
    # publish-then-record: on an unlocked run the vector file exists when the
    # ledger row is appended, so file and row come from ONE run.  Mutant tried:
    # moving the write below `record_holdout_unlock` — the spy sees no file.
    cache = tmp_path / "cache"
    argv = _argv(file_db, cache, seasons="2024", grade_as_of="2025-02-28")
    monkeypatch.setattr(R, "replay", lambda conn, params, **kw: [_holdout_record()])
    seen = []
    real_record = D.record_holdout_unlock
    vector = D.freeze_dir(cache, D.ReplayParams(strategies=("signal_topk",), k=2,
                                                seasons=(2024,), weeks=(6,))) / R.PER_WEEK_FILE
    def spy(*a, **k):
        seen.append(vector.exists())
        return real_record(*a, **k)
    monkeypatch.setattr(D, "record_holdout_unlock", spy)
    assert R.main(argv + ["--unlock-holdout"]) == 0
    assert seen == [True]
    body = json.loads(vector.read_text())
    assert body["split"] == "HOLDOUT" and body["seasons"] == [2024]
    assert "HOLDOUT unlock recorded" in capsys.readouterr().out


def test_per_week_file_is_written_on_an_unlocked_run_without_a_second_load(
    file_db, tmp_path, monkeypatch,
):
    # the file is built from the cards main() already holds: one load, one
    # grade per card, no second read of the holdout freeze.  Mutant tried:
    # re-loading the freeze (D.load) or re-grading inside write_per_week_lifts
    # — the counters below move.
    cache = tmp_path / "cache"
    argv = _argv(file_db, cache, seasons="2024", grade_as_of="2025-02-28", market="wp")
    monkeypatch.setattr(R, "replay", lambda conn, params, **kw: [_holdout_record()])
    assert R.main(argv + ["--unlock-holdout"]) == 0     # decide + freeze + grade
    loads, grades = [], []
    real_load, real_grade = D.load, S.build_scorecard
    monkeypatch.setattr(D, "load", lambda *a, **k: (loads.append(1), real_load(*a, **k))[1])
    monkeypatch.setattr(S, "build_scorecard",
                        lambda *a, **k: (grades.append(1), real_grade(*a, **k))[1])
    assert R.main(argv + ["--unlock-holdout", "--grade-only"]) == 0
    assert loads == [1] and grades == [1]
    params = D.ReplayParams(strategies=("signal_topk",), k=2, seasons=(2024,), weeks=(6,))
    body = json.loads((D.freeze_dir(cache, params) / R.PER_WEEK_FILE).read_text())
    assert body["split"] == "HOLDOUT" and body["cache_key"] == params.cache_key()


# ------------------------------------------------------------- README


METHODOLOGY = (
    "Standing methodology for every experiment: strict as-of cuts on all inputs\n"
    "(including podcast publish dates), train on 2021–23 / validate on 2024–25,\n"
    "grade decisions not outcomes, precision@k for k ≤ 3 (the realistic weekly\n"
    "claim budget)."
)


def test_every_replay_invocation_in_the_readme_parses_and_the_methodology_is_verbatim():
    # DOC-1: the README is an interface — a renamed flag rots it loudly here
    text = (REPO_ROOT / "backtest" / "README.md").read_text(encoding="utf-8")
    assert METHODOLOGY in text
    commands = [
        line.strip() for line in text.splitlines()
        if line.strip().startswith("python -m backtest.replay")
    ]
    assert len(commands) >= 5, commands
    parser = R.build_parser()
    done_when = "python -m backtest.replay --seasons 2023 --strategy signal_topk --k 3"
    assert done_when in commands
    for cmd in commands:
        argv = cmd.split()[3:]
        args = parser.parse_args(argv)            # SystemExit here = a stale README
        params = D.ReplayParams(
            strategies=tuple(args.strategy.split(",")), k=args.k,
            seasons=R._parse_seasons(args.seasons),
            generator=R.generator_setting(args.breakout_floor, args.emergence_floor),
        )
        held = D.holdout_seasons(params.seasons)
        assert bool(held) == args.unlock_holdout, cmd   # holdout shapes carry the flag
    args = parser.parse_args(done_when.split()[3:])
    assert not args.unlock_holdout and not args.force and not args.grade_only
    assert D.ReplayParams(strategies=("signal_topk",), k=3,
                          seasons=R._parse_seasons(args.seasons)).cache_key() == "66c0e83d7da3"
    # the module table is an interface too: a module with a CLI that the README
    # does not name is a module whose documented shape lives only in a
    # gitignored note (item 4.2 audit — tune.py / tune_grid.py were missing)
    for name in ("draft_backtest.py", "replay.py", "decisions.py", "scorecards.py", "stats.py",
                 "tune.py", "tune_grid.py"):
        assert f"`{name}`" in text, name
    tune_commands = [line.strip() for line in text.splitlines()
                     if line.strip().startswith("python -m backtest.tune")]
    assert len(tune_commands) >= 2, tune_commands
    tune_parser = T.build_parser()
    for cmd in tune_commands:
        targs = tune_parser.parse_args(cmd.split()[3:])   # SystemExit = a stale README
        assert targs.db and "ziggurat.sqlite" not in Path(targs.db).name
    assert R.GRADE_LOG in text and T.FINGERPRINT_LOG in text and T.RESULTS_DIR in text
    assert str(T.SEARCH_CACHE_DIR.relative_to(REPO_ROOT)) in text
    assert "holdout-unlocks.jsonl" in text and "data/backtest/replay/<params_hash[:12]>/" in text
    assert "does NOT beat the market" in text


# ------------------------------------------------------------ live DB


@pytest.mark.skipif(not R.DEFAULT_DB.exists(), reason="live db/ziggurat.sqlite not present")
def test_live_db_smoke_one_week():
    conn = R.open_ro(R.DEFAULT_DB)
    params = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(2023,), weeks=(6,))
    recs = R.replay(conn, params)
    (rec,) = recs
    assert rec.status == D.WEEK_DECIDED and rec.as_of == AS_OF and len(rec.decisions) == 3
    assert rec.r0_scrape_date is not None and rec.excluded_ineligible >= 0
    card = S.build_scorecard(conn, recs, params, strategy="signal_topk", market="wp",
                             grade_as_of="2024-02-28")
    assert card.overall.decisions == 3 and card.overall.null_universe > 100
    text = S.render(card)
    assert "TRAIN 2021-23" in text and "precision@3" in text


# ------------------------------ item 4.2: the grade log, and atomic writes


def test_every_cli_grade_appends_one_line_to_the_grade_log(file_db, tmp_path):
    # Mutant killed: logging grades ONLY from the search runner (backtest.tune),
    # the pre-audit state.  §6.4 / §11.5: "every build_scorecard call made
    # anywhere in 4.2 — the search, the G6 re-grades (§7.2), the holdout
    # episode — appends one line ... F8 is otherwise unverifiable after the
    # fact."  Both the §8.3 holdout commands and the §7.2 G6 re-grades at
    # --hit-places 3 / 8 are THIS CLI, and hit_places / owned_delta /
    # grade_as_of live in neither ReplayParams nor the manifest — so a log
    # written only by the runner (which asserts H == 5 before it grades at all)
    # could not see the sweep it exists to expose.
    cache = tmp_path / "cache"
    assert R.main(_argv(file_db, cache, market="wp")) == 0
    log = cache / R.GRADE_LOG
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["places"] == S.HIT_PLACES_DEFAULT and rows[0]["market"] == "wp"
    assert rows[0]["strategy"] == "signal_topk" and rows[0]["grade_as_of"] == "2024-02-28"
    assert rows[0]["entry_point"] == "backtest.replay"
    assert rows[0]["unlock_holdout"] is False
    assert rows[0]["cache_key"] == D.ReplayParams(
        strategies=("signal_topk",), k=2, seasons=(2023,), weeks=(6,)).cache_key()
    # a G6-shaped re-grade at another H is APPENDED, with the H it used — this
    # is the only artefact in the item that records it
    assert R.main(_argv(file_db, cache, market="wp")
                  + ["--grade-only", "--hit-places", "3"]) == 0
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r["places"] for r in rows] == [S.HIT_PLACES_DEFAULT, 3]
    assert rows[0]["cache_key"] == rows[1]["cache_key"]        # H is not in the key
    # --grade-log points the line somewhere else (e.g. the 4.2 SEARCH_CACHE_DIR,
    # so the whole item keeps ONE file)
    elsewhere = tmp_path / "search-cache" / "grade-log.jsonl"
    assert R.main(_argv(file_db, cache, market="wp")
                  + ["--grade-only", "--grade-log", str(elsewhere)]) == 0
    assert len(elsewhere.read_text().splitlines()) == 1
    assert len(log.read_text().splitlines()) == 2              # unchanged
    # two markets x two strategies is four calls and four lines
    assert R.main(_argv(file_db, cache, strategy="signal_topk,random_k", k="2",
                        market="both")) == 0
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(rows) == 6 and {r["market"] for r in rows[2:]} == {"wp", "ros"}


def test_the_per_week_vector_is_written_through_the_atomic_writer(file_db, tmp_path,
                                                                 monkeypatch):
    # Mutant killed: `path.write_bytes(blob)` in write_per_week_lifts.  The
    # docstring promises "atomically (tmp + fsync + rename via D._write_atomic)"
    # and the only assertion was that no `.tmp` survives — which a plain write
    # satisfies trivially, because it never creates one.  This file is the ONE
    # sanctioned source of the holdout d_w vectors (§8.3), and every graded run
    # REWRITES it in place, so a torn write destroys the previous valid file.
    writes: list[Path] = []
    real = D._write_atomic

    def spy(path, data):
        writes.append(Path(path))
        real(path, data)
    monkeypatch.setattr(D, "_write_atomic", spy)
    cache = tmp_path / "cache"
    assert R.main(_argv(file_db, cache)) == 0
    path = D.freeze_dir(cache, D.ReplayParams(strategies=("signal_topk",), k=2,
                                              seasons=(2023,), weeks=(6,))) / R.PER_WEEK_FILE
    assert path in writes and path.exists()
    assert not list(path.parent.glob("*.tmp*"))


def test_the_atomic_writer_never_shares_a_temp_name(tmp_path, monkeypatch):
    # Mutant killed: `tmp = path.with_name(path.name + ".tmp")`, a FIXED temp
    # name.  Two writers aiming at one path then interleave into a single shared
    # fd — the survivor can be a mixture neither of them wrote — and the loser's
    # os.replace hits a path the winner has already renamed away.
    target = tmp_path / "x.json"
    seen: list[str] = []
    real_open = open

    def spy(path, *a, **k):
        if str(path).startswith(str(target)) and str(path) != str(target):
            seen.append(Path(path).name)
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    D._write_atomic(target, b"one")
    D._write_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert len(seen) == 2 and len(set(seen)) == 2
    assert all(str(os.getpid()) in name for name in seen)
    assert not list(tmp_path.glob("*.tmp*"))
