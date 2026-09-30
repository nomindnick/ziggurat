"""Item 4.7 — the projection-calibration measurement (``backtest/calibration.py``).

Two halves, tested separately, the way the module is built:

* the PURE statistics — OLS, the player-clustered bootstrap, the exact-rank
  percentile interval and the pre-registered decision rule — on synthetic
  numbers with a known answer;
* the OBSERVATION rules and their as-of discipline on a synthetic in-memory
  database plus a synthetic, sha256-manifested decision capture. Every player,
  team pairing and id here is invented (Rule 5).
"""

from __future__ import annotations

import hashlib
import json
import random

import pytest

from backtest import calibration as C
from ziggurat.core import scoring

SEASON = 2026
AS_OF = "2026-09-08"
GAMEDAY = "2026-09-13"
POINT = C.DecisionPoint(1, "20260908T183009-testcal1", AS_OF)


# ===========================================================================
#                              pure statistics
# ===========================================================================


def _obs(cluster, x, y, *, week=1, position="WR"):
    return C.Observation(week=week, position=position, cluster=str(cluster),
                         player=f"P{cluster}", projected=float(x), realised=float(y))


def _synthetic(slope, *, intercept=1.5, players=80, weeks=3, noise=2.0, seed=7,
               position="WR"):
    rng = random.Random(seed)
    out = []
    for p in range(players):
        for w in range(1, weeks + 1):
            x = rng.uniform(2.0, 20.0)
            out.append(_obs(p, x, intercept + slope * x + rng.gauss(0.0, noise),
                            week=w, position=position))
    return out


def test_ols_recovers_an_exact_line():
    xs = [1.0, 2.0, 3.0, 7.5]
    a, b = C.ols(xs, [4.0 + 0.5 * x for x in xs])
    assert a == pytest.approx(4.0)
    assert b == pytest.approx(0.5)


def test_ols_refuses_a_slope_it_cannot_estimate():
    with pytest.raises(ValueError):
        C.ols([3.0, 3.0, 3.0], [1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        C.ols([3.0], [1.0])


def test_a_known_slope_is_recovered_with_an_interval_that_covers_it():
    fit = C.fit_position(_synthetic(0.80), position="WR", label="t", b=2000)
    assert fit.b == pytest.approx(0.80, abs=0.05)
    lo, hi = fit.b_interval
    assert lo < 0.80 < hi
    assert fit.n_obs == 240 and fit.n_players == 80
    # the interval is tight enough here to sit wholly below the floor
    assert hi < C.PRACTICAL_FLOOR and fit.decision == C.DEPLOY

    calibrated = C.fit_position(_synthetic(1.00), position="WR", label="t", b=2000)
    assert calibrated.b == pytest.approx(1.00, abs=0.05)
    assert calibrated.b_interval[0] <= 1.0 <= calibrated.b_interval[1]
    assert calibrated.decision == C.RETIRE


def test_the_bootstrap_is_deterministic_under_its_seed():
    obs = _synthetic(0.9, players=30)
    one = C.cluster_bootstrap(obs, b=400, seed=11, label="cell")
    two = C.cluster_bootstrap(obs, b=400, seed=11, label="cell")
    assert one == two
    assert C.cluster_bootstrap(obs, b=400, seed=12, label="cell").slopes != one.slopes
    # each cell draws its own stream: a different label is a different stream
    assert C.cluster_bootstrap(obs, b=400, seed=11, label="other").slopes != one.slopes
    # and the draws do not depend on the order the observations arrive in
    shuffled = list(obs)
    random.Random(3).shuffle(shuffled)
    assert C.cluster_bootstrap(shuffled, b=400, seed=11, label="cell") == one


def test_a_replicate_carries_every_week_of_each_drawn_player_together():
    """The sufficient-statistics replicate equals an explicit OLS over the drawn
    players' concatenated weeks — a player drawn twice contributes all his weeks
    twice, and no player ever contributes a subset of his weeks."""
    obs = _synthetic(0.7, players=9, weeks=3)
    ids, sums, mx, my = C._cluster_sums(obs)
    by_cluster = {cid: [o for o in obs if o.cluster == cid] for cid in ids}
    rng = random.Random(5)
    for _ in range(25):
        drawn = C.draw_clusters(len(ids), rng)
        rows = [o for i in drawn for o in by_cluster[ids[i]]]
        a_exp, b_exp = C.ols([o.projected for o in rows], [o.realised for o in rows])
        a_got, b_got = C.replicate_fit(sums, drawn, mx, my)
        assert b_got == pytest.approx(b_exp, rel=1e-9, abs=1e-9)
        assert a_got == pytest.approx(a_exp, rel=1e-9, abs=1e-9)


def test_the_bootstrap_resamples_players_not_player_weeks():
    """Three identical weeks per player carry ONE player's worth of information.
    Resampling players sees that; resampling rows (every row its own cluster)
    does not and reports an interval about sqrt(3) too narrow."""
    rng = random.Random(21)
    clustered, iid = [], []
    for p in range(14):
        x = rng.uniform(2.0, 20.0)
        y = 1.0 + 0.9 * x + rng.gauss(0.0, 4.0)
        for w in (1, 2, 3):
            clustered.append(_obs(p, x, y, week=w))
            iid.append(_obs(f"{p}-{w}", x, y, week=w))
    def width(obs):
        lo, hi = C.percentile_interval(C.cluster_bootstrap(obs, b=2000, seed=1, label="w").slopes)
        return hi - lo

    assert C.cluster_bootstrap(clustered, b=10, seed=1, label="w").clusters == 14
    assert width(clustered) > 1.4 * width(iid)


def test_the_percentile_interval_uses_exact_ranks_not_float_ranks():
    """(1 - 0.95) / 2 * 2000 is 50.00000000000004 in floats, whose ceiling is 51."""
    values = list(range(1, 2001))
    random.Random(9).shuffle(values)
    assert C.percentile_interval(values) == (50, 1950)


# --------------------------------------------------------------- decision rule


@pytest.mark.parametrize("b_hat, lo, hi, expected", [
    (0.70, 0.60, 0.85, C.DEPLOY),     # the whole interval sits below the floor
    (0.75, 0.60, 0.8999, C.DEPLOY),
    (0.75, 0.60, 0.90, C.NARROW),     # "upper < 0.90" is strict
    (0.95, 0.85, 1.05, C.RETIRE),     # contains 1.0 and b-hat >= floor
    (0.90, 0.80, 1.00, C.RETIRE),     # closed interval; b-hat AT the floor
    (1.00, 1.00, 1.20, C.RETIRE),
    (0.95, 0.91, 0.99, C.NARROW),     # precise, but excludes 1.0 from below
    (0.85, 0.60, 1.10, C.NARROW),     # contains 1.0 but b-hat under the floor
    (1.30, 1.10, 1.50, C.NARROW),     # significantly ABOVE 1: reported, not acted on
])
def test_the_pre_registered_decision_rule(b_hat, lo, hi, expected):
    assert C.decide(b_hat, lo, hi) == expected


def test_a_slope_significantly_above_one_is_narrow_end_to_end():
    fit = C.fit_position(_synthetic(1.4, noise=1.0, position="RB"), position="RB",
                         label="t", b=1000)
    assert fit.b_interval[0] > 1.0
    assert fit.decision == C.NARROW


# ===========================================================================
#                   the realised rule (pure, over plain inputs)
# ===========================================================================


def _week(stat_rows=None, dst_rows=None, teams=("BUF", "MIA")):
    return C.RealisedWeek(1, dict(stat_rows or {}), dict(dst_rows or {}), frozenset(teams))


def test_a_player_whose_team_played_with_no_stat_row_scores_zero():
    got = C.realised_points("WR", gsis_id="00-1", team="BUF", week=_week())
    assert got == C.Realised(0.0, did_not_play=True)


def test_a_player_whose_team_did_not_play_is_excluded_not_zeroed():
    assert C.realised_points("WR", gsis_id="00-1", team="NYJ", week=_week()) is None


def test_a_stat_row_is_scored_through_scoring_py():
    row = {"receptions": 5.0, "receiving_yards": 62.0, "receiving_tds": 1.0}
    got = C.realised_points("WR", gsis_id="00-1", team="BUF",
                            week=_week(stat_rows={"00-1": row}))
    assert got == C.Realised(scoring.score_offense(row))
    assert got.points == pytest.approx(5 + 6.2 + 6)


def test_the_crosswalk_is_consulted_only_when_the_line_id_has_no_row():
    real = {"rushing_yards": 40.0}
    week = _week(stat_rows={"00-0099999": real})
    got = C.realised_points("RB", gsis_id="PLA123", team="BUF", week=week,
                            crosswalk_gsis_id="00-0099999")
    assert got == C.Realised(pytest.approx(4.0), via_crosswalk=True)
    # the line's own row, when present, always wins
    own = _week(stat_rows={"PLA123": {"rushing_yards": 10.0}, "00-0099999": real})
    assert C.realised_points("RB", gsis_id="PLA123", team="BUF", week=own,
                             crosswalk_gsis_id="00-0099999").points == pytest.approx(1.0)


def test_a_kicker_with_uncaptured_kicking_columns_refuses_a_zero():
    row = {c: None for c in ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39",
                             "fg_made_40_49", "fg_made_50_59", "fg_made_60_",
                             "pat_made", "fg_missed")}
    with pytest.raises(C.NotCaptured):
        C.realised_points("K", gsis_id="00-9", team="BUF", week=_week(stat_rows={"00-9": row}))


def test_a_defense_whose_team_played_with_no_row_refuses_the_run():
    with pytest.raises(C.CalibrationInputError):
        C.realised_points("DST", gsis_id=None, team="BUF", week=_week())


# ===========================================================================
#               the orchestration on a synthetic database + capture
# ===========================================================================


def _schedule(db, week=1, games=(("BUF", "MIA"), ("KC", "DEN"))):
    for home, away in games:
        db.execute(
            "INSERT INTO schedules (game_id, season, week, game_type, gameday, home_team, "
            "away_team, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{SEASON}_{week:02d}_{away}_{home}", SEASON, week, "REG", GAMEDAY, home, away,
             "2026-09-01", "2026-08-01"),
        )


def _proj(db, spid, gsis, pos, team, *, week=1, opponent="OPP", retrieved=AS_OF,
          knowable=None, **stats):
    cols = {"source": "sleeper_rotowire", "source_player_id": spid, "gsis_id": gsis,
            "season": SEASON, "week": week, "season_type": "regular", "position": pos,
            "team": team, "opponent": opponent, "retrieved_as_of": retrieved,
            "knowable_as_of": knowable or retrieved, **stats}
    db.execute(f"INSERT INTO projections ({', '.join(cols)}) VALUES "
               f"({', '.join('?' * len(cols))})", tuple(cols.values()))


def _espn(db, espn_id, gsis, pos, team, name, *, on_team=None, retrieved=AS_OF):
    row = {"season": SEASON, "espn_player_id": espn_id, "gsis_id": gsis, "player": name,
           "position": pos, "pro_team": team, "on_team_id": on_team,
           "roster_status": "ONTEAM" if on_team else "FREEAGENT",
           "retrieved_as_of": retrieved, "knowable_as_of": retrieved}
    db.execute(f"INSERT INTO league_player_state ({', '.join(row)}) VALUES "
               f"({', '.join('?' * len(row))})", tuple(row.values()))


def _stat(db, gsis, pos, team, *, week=1, retrieved="2026-09-29", knowable=GAMEDAY, **stats):
    cols = {"player_id": gsis, "season": SEASON, "week": week, "season_type": "REG",
            "position": pos, "recent_team": team, "retrieved_as_of": retrieved,
            "knowable_as_of": knowable, **stats}
    db.execute(f"INSERT INTO weekly_stats ({', '.join(cols)}) VALUES "
               f"({', '.join('?' * len(cols))})", tuple(cols.values()))


def _dst(db, team, *, week=1, retrieved="2026-09-29", **stats):
    cols = {"season": SEASON, "week": week, "team": team, "season_type": "REG",
            "retrieved_as_of": retrieved, "knowable_as_of": GAMEDAY, **stats}
    db.execute(f"INSERT INTO team_defense ({', '.join(cols)}) VALUES "
               f"({', '.join('?' * len(cols))})", tuple(cols.values()))


def _capture(root, point, *, pool=(), roster=(), trigger="timer"):
    """A minimal capture the verified reader accepts: every file named by the
    manifest with its sha256, and nothing else in the directory."""
    path = root / str(SEASON) / f"wk{point.week:02d}" / point.capture_id
    path.mkdir(parents=True)
    payload = {
        "pool.jsonl": "".join(json.dumps(r, sort_keys=True) + "\n" for r in pool).encode(),
        "roster.json": json.dumps({"team_id": 10, "roster": list(roster)}).encode(),
    }
    files = {}
    for name, blob in payload.items():
        (path / name).write_bytes(blob)
        files[name] = {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
    manifest = {"capture_id": point.capture_id, "as_of": point.as_of, "week": point.week,
                "trigger": trigger, "files": files,
                "vintages": {"projections": point.as_of}}
    (path / "manifest.json").write_text(json.dumps(manifest))
    return root


@pytest.fixture()
def world(db, tmp_path):
    """One graded week: four skill players, a kicker and two defenses."""
    _schedule(db)
    # A: projected exactly 1.0 (one reception) — ON the floor, so IN
    _proj(db, "S1", "00-0000001", "WR", "BUF", receptions=1.0)
    _espn(db, "101", "00-0000001", "WR", "BUF", "Floor Catcher")
    _stat(db, "00-0000001", "WR", "BUF", receptions=3.0, receiving_yards=20.0)
    # B: projected 0.99 (9.9 yards) — under the floor, so OUT of the primary
    _proj(db, "S2", "00-0000002", "WR", "MIA", receiving_yards=9.9)
    _espn(db, "102", "00-0000002", "WR", "MIA", "Under Floor")
    # C: projected 12.0, no stat row, team played -> did not play, 0.0
    _proj(db, "S3", "00-0000003", "RB", "KC", rushing_yards=120.0)
    _espn(db, "103", "00-0000003", "RB", "KC", "Sat Out", on_team=10)
    # D: a bye-shaped row (no opponent) -> excluded as no forecast
    _proj(db, "S4", "00-0000004", "TE", "NYJ", opponent=None)
    _espn(db, "104", "00-0000004", "TE", "NYJ", "On Bye")
    # K: a captured kicking line
    _proj(db, "S5", "00-0000005", "K", "DEN", pat_made=3.0, fg_made_0_39=1.0)
    _espn(db, "105", "00-0000005", "K", "DEN", "Kick Er")
    _stat(db, "00-0000005", "K", "DEN", fg_made_0_19=0, fg_made_20_29=1, fg_made_30_39=0,
          fg_made_40_49=1, fg_made_50_59=0, fg_made_60_=0, pat_made=2, fg_missed=1)
    # two defenses, priced on events only
    for i, team in enumerate(("BUF", "MIA")):
        _proj(db, team, None, "DEF", team, sacks=3.0)
        _espn(db, str(-16001 - i), None, "D/ST", team, f"{team} D/ST")
        _dst(db, team, sacks=2.0, def_interceptions=1.0, points_allowed=17.0,
             yards_allowed=320.0)
    for team in ("KC", "DEN"):
        _dst(db, team, sacks=1.0, points_allowed=24.0, yards_allowed=350.0)
    db.commit()
    root = _capture(tmp_path / "decisions", POINT,
                    pool=[{"espn_id": "101", "position": "WR", "gsis_id": "00-0000001",
                           "pro_team": "BUF", "player": "Floor Catcher", "scanned": True},
                          {"espn_id": "102", "position": "WR", "gsis_id": "00-0000002",
                           "pro_team": "MIA", "player": "Under Floor", "scanned": True},
                          {"espn_id": "105", "position": "K", "gsis_id": "00-0000005",
                           "pro_team": "DEN", "player": "Kick Er", "scanned": False}],
                    roster=[{"espn_player_id": "103", "position": "RB",
                             "gsis_id": "00-0000003", "pro_team": "KC", "player": "Sat Out"}])
    return db, root


def _run_week(db, root, point=POINT):
    realised = C.realised_week(db, point.week)
    return C.observations_for_week(db, point, captures_root=root, realised=realised)


def test_the_primary_population_is_projection_at_least_one(world):
    db, root = world
    primary, _secondary, cov = _run_week(db, root)
    names = {o.player for o in primary}
    assert "Floor Catcher" in names            # exactly 1.0 is IN (">= 1.0")
    assert "Under Floor" not in names          # 0.99 is OUT
    assert "On Bye" not in names
    assert cov.below_floor == 1
    assert cov.no_forecast_this_week == 1
    by = {o.player: o for o in primary}
    assert by["Floor Catcher"].projected == pytest.approx(1.0)
    assert by["Floor Catcher"].realised == pytest.approx(3.0 + 2.0)


def test_the_did_not_play_rule_on_real_rows(world):
    db, root = world
    primary, _secondary, cov = _run_week(db, root)
    sat = next(o for o in primary if o.player == "Sat Out")
    assert sat.projected == pytest.approx(12.0)
    assert sat.realised == 0.0 and sat.did_not_play
    assert cov.primary_did_not_play == 1


def test_kickers_and_defenses_are_scored_through_their_own_paths(world):
    db, root = world
    primary, _secondary, _cov = _run_week(db, root)
    by = {o.player: o for o in primary}
    kick = scoring.score_kicker({"fg_made_0_39": 1, "fg_made_40_49": 1, "fg_made_50_59": 0,
                                 "fg_made_60": 0, "pat_made": 2, "fg_missed": 1})
    assert by["Kick Er"].realised == pytest.approx(kick)
    assert by["Kick Er"].position == "K"
    dst = scoring.score_dst({"sacks": 2.0, "def_interceptions": 1.0,
                             "points_allowed": 17.0, "yards_allowed": 320.0})
    assert by["BUF D/ST"].realised == pytest.approx(dst)
    assert by["BUF D/ST"].position == "DST"


def test_the_secondary_is_the_scanned_pool_plus_the_roster_with_no_floor(world):
    db, root = world
    _primary, secondary, cov = _run_week(db, root)
    names = sorted(o.player for o in secondary)
    # scanned pool rows (the 0.99 one included: no floor) + the roster; the
    # UNscanned kicker is not a row the tool quoted
    assert names == ["Floor Catcher", "Sat Out", "Under Floor"]
    assert cov.secondary_rows == 3


def test_a_projection_retrieved_after_the_capture_is_never_used(world):
    """Leakage: the projected side is the tool's Tuesday read. A later pull of
    the same player-week (retrieved the day after the capture) must not move it —
    even one stamped knowable ON the capture day, which only the RETRIEVAL gate of
    the ``historical`` view can hide (``latest_truth`` would serve it)."""
    db, root = world
    _proj(db, "S1", "00-0000001", "WR", "BUF", retrieved="2026-09-09", knowable=AS_OF,
          receptions=9.0)
    db.commit()
    primary, _s, _cov = _run_week(db, root)
    by = {o.player: o for o in primary}
    assert by["Floor Catcher"].projected == pytest.approx(1.0)
    # ... and the SAME read one day later does see it, so the gate is what hid it
    later = C.DecisionPoint(1, POINT.capture_id, "2026-09-09")
    lines = C.weekly_lines(db, as_of=later.as_of, season=SEASON, weeks=[1])
    assert lines[("SKILL", "00-0000001")].points[1] == pytest.approx(9.0)


def test_the_realised_side_is_latest_truth_with_the_fact_time_gate(world):
    db, _root = world
    # a stat correction retrieved AFTER the fact is the grade (latest_truth) ...
    _stat(db, "00-0000001", "WR", "BUF", retrieved="2026-09-30", receptions=4.0,
          receiving_yards=20.0)
    # ... but a fact not knowable by the realised as_of is invisible
    _stat(db, "00-0000009", "WR", "BUF", knowable="2026-10-01", receptions=7.0)
    db.commit()
    week = C.realised_week(db, 1)
    assert week.stat_rows["00-0000001"]["receptions"] == 4.0
    assert "00-0000009" not in week.stat_rows
    # one row per player even with two vintages stored (no double count)
    assert list(week.stat_rows).count("00-0000001") == 1


def test_a_placeholder_line_finds_the_real_stat_row_through_the_crosswalk(db, tmp_path):
    _schedule(db)
    _proj(db, "S7", "PLA123", "RB", "BUF", rushing_yards=50.0)
    _espn(db, "107", "PLA123", "RB", "BUF", "Rookie Back")
    for gsis in ("PLA123", "00-0099999"):
        db.execute("INSERT INTO players (gsis_id, espn_id, name, retrieved_as_of, "
                   "knowable_as_of) VALUES (?, '107', 'Rookie Back', ?, ?)",
                   (gsis, AS_OF, AS_OF))
    _stat(db, "00-0099999", "RB", "BUF", rushing_yards=70.0)
    db.commit()
    root = _capture(tmp_path / "d", POINT)
    primary, _s, cov = _run_week(db, root)
    back = next(o for o in primary if o.player == "Rookie Back")
    assert back.realised == pytest.approx(7.0) and not back.did_not_play
    assert cov.realised_via_crosswalk == ["Rookie Back (PLA123 -> 00-0099999)"]


def test_the_secondary_joins_through_the_universe_not_the_capture_gsis(db, tmp_path):
    """Migration 019 re-keyed stored rows after the week-1 capture: the capture
    still carries the old placeholder, the stored rows the new id."""
    _schedule(db)
    _proj(db, "S8", "00-0000008", "WR", "MIA", receptions=4.0)
    _espn(db, "108", "00-0000008", "WR", "MIA", "Rekeyed Rookie")
    db.commit()
    root = _capture(tmp_path / "d", POINT,
                    pool=[{"espn_id": "108", "position": "WR", "gsis_id": "OLD108",
                           "pro_team": "MIA", "player": "Rekeyed Rookie", "scanned": True}])
    _p, secondary, cov = _run_week(db, root)
    assert [o.player for o in secondary] == ["Rekeyed Rookie"]
    assert cov.secondary_unjoinable == 0


def test_a_capture_whose_bytes_moved_is_refused(world):
    db, root = world
    pool = next(root.rglob("pool.jsonl"))
    pool.write_bytes(pool.read_bytes() + b"\n")
    with pytest.raises(C.CalibrationInputError):
        _run_week(db, root)


def test_a_non_timer_capture_is_refused(db, tmp_path):
    root = _capture(tmp_path / "d", POINT, trigger="manual")
    with pytest.raises(C.CalibrationInputError):
        C.load_capture(root, POINT)


def test_the_whole_run_refuses_a_week_that_is_not_final(world):
    db, root = world
    db.execute("DELETE FROM team_defense WHERE team = 'KC'")
    db.commit()
    with pytest.raises(C.CalibrationInputError):
        C.run_calibration(db, captures_root=root, points=[POINT], week4=None, b=50)


def test_the_whole_run_reports_every_cell_and_week_four_not_yet(world):
    db, root = world
    week4 = C.DecisionPoint(4, "20260929T183029-testcal4", "2026-09-29")
    report = C.run_calibration(db, captures_root=root, points=[POINT], week4=week4, b=50)
    assert report.primary.decides
    assert not any(cell.decides for cell in report.sensitivities)
    assert [c.label for c in report.sensitivities] == [
        "WEEKS 2-3", "SECONDARY", "SECONDARY >= 1.0"]
    assert report.week4_status.startswith("not yet")
    text = C.format_report(report, b=50)
    assert "INTERPRETATION NOTES" in text and "not yet" in text
    # the kicker and the two defenses are a single observation each or two: a
    # position with fewer than two rows is reported as not fitted, never guessed
    assert report.primary.fits["QB"] is None


def test_the_cli_runs_read_only_end_to_end_and_the_readme_names_it(world, tmp_path, capsys,
                                                                   monkeypatch):
    """``python -m backtest.calibration`` opens a FILE database read-only, prints
    the report and exits 0; a missing pre-registered capture is a REFUSAL (exit 2),
    never a partial report; and the backtest README's module table names it (a
    module with a CLI the README does not name is documented only in a gitignored
    note)."""
    import sqlite3

    from ziggurat.paths import REPO_ROOT

    db, root = world
    path = tmp_path / "world.sqlite"
    target = sqlite3.connect(path)
    db.backup(target)
    target.close()
    before = path.read_bytes()
    # the real pre-registered captures are not in this synthetic archive
    assert C.main(["--db", str(path), "--captures", str(root)]) == 2
    assert "REFUSED" in capsys.readouterr().err
    monkeypatch.setattr(C, "DECISION_POINTS", (POINT,))
    monkeypatch.setattr(C, "WEEK4_POINT", None)
    assert C.main(["--db", str(path), "--captures", str(root)]) == 0
    out = capsys.readouterr().out
    assert "PRIMARY" in out and "INTERPRETATION NOTES" in out
    assert path.read_bytes() == before              # read-only: not a byte moved
    readme = (REPO_ROOT / "backtest" / "README.md").read_text(encoding="utf-8")
    assert "`calibration.py`" in readme and "python -m backtest.calibration" in readme
