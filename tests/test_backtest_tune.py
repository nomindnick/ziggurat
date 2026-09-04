"""Item 4.2 — the pre-registered search runner (backtest/tune.py).

Every fixture here is SYNTHETIC: the offline 2023 week-5/6 nflverse slice from
``tests/fixtures/nfl`` plus a hand-built FantasyPros panel whose "players" are
gsis ids, never names.  Nothing opens the live database, nothing reads a
holdout season, nothing runs a non-default floor on real data.  The round-2
procedure is exercised against canned per-week vectors so its rules (keep on
permutation-cleared increments only, report a reverse-order disagreement,
stop a bisection within tolerance or say "unmatched", count every attempt)
are pinned independently of the harness.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import multiprocessing as mp
import os
import shutil
from pathlib import Path

import pytest

from backtest import decisions as D
from backtest import replay as R
from backtest import stats
from backtest import tune as T
from backtest import tune_grid as G
from ziggurat.data.nfl import injuries, players, schedules, snap_counts, weekly_stats
from ziggurat.data.store import apply_schema, connect

BULK = "2026-07-16"
PAGES = {"QB": "qb", "RB": "ppr-rb", "WR": "ppr-wr", "TE": "ppr-te"}
DEFAULT_KEY = "28007210abc5"


# ---------------------------------------------------------------------------
# fixtures — a file DB with a synthetic panel so the fixture's two decidable
# weeks (2023 wk5 / wk6) grade and carry a null
# ---------------------------------------------------------------------------


def _panel(conn, *, week, scrape, shift):
    rows = conn.execute(
        "SELECT DISTINCT player_id, position, recent_team FROM weekly_stats WHERE position IN "
        "('QB','RB','WR','TE') AND player_id IS NOT NULL AND season=2023 AND week=5 "
        "ORDER BY player_id").fetchall()
    by_pos: dict[str, list] = {}
    for gsis, pos, team in rows:
        by_pos.setdefault(pos, []).append((gsis, team))
    for pos, lst in by_pos.items():
        for i, (gsis, team) in enumerate(lst):
            rank = max(1, 25 + i + shift(gsis))          # every player beyond eligibility
            conn.execute(
                "INSERT INTO fpecr_panel (fantasypros_id, ecr_type, fp_page, scrape_date, "
                "season, nfl_week, week_basis, player, position, team, gsis_id, page_rank, "
                "pos_rank, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"fp-{gsis}", "wp", PAGES[pos], scrape, 2023, week, "inferred", gsis, pos,
                 team, gsis, rank, rank, "2026-08-30", scrape))


def _rise3(gsis):
    return -8 if sum(map(ord, gsis)) % 3 == 0 else 0


def _rise4(gsis):
    return -8 if sum(map(ord, gsis)) % 4 == 0 else 0


@pytest.fixture()
def search_db(tmp_path, nfl_fixture) -> Path:
    path = tmp_path / "search.sqlite"
    conn = connect(path)
    apply_schema(conn)
    players.ingest_players(conn, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(conn, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(conn, nfl_fixture("weekly_stats"), retrieved_as_of=BULK)
    snap_counts.ingest_snap_counts(conn, nfl_fixture("snap_counts"), retrieved_as_of=BULK)
    injuries.ingest_injuries(conn, nfl_fixture("injuries"), retrieved_as_of=BULK)
    _panel(conn, week=5, scrape="2023-10-06", shift=lambda g: 0)      # r0 for wk5
    _panel(conn, week=6, scrape="2023-10-13", shift=_rise3)           # r1 wk5 / r0 wk6
    _panel(conn, week=7, scrape="2023-10-20", shift=_rise3)           # r2 wk5 / r1 wk6
    _panel(conn, week=8, scrape="2023-10-27", shift=_rise4)           # r2 wk6
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def cfg(search_db, tmp_path) -> T.SearchConfig:
    # 200 permutation draws keep the tests fast; the frozen 10,000 is the default
    return T.SearchConfig(db=search_db, cache_dir=tmp_path / "cache", jobs=1, permutation_b=200)


@pytest.fixture()
def ro(search_db):
    conn = R.open_ro(search_db)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# the three refusals
# ---------------------------------------------------------------------------


def test_the_search_runner_has_no_holdout_flag(cfg):
    # Mutant killed: deleting the HOLDOUT block at the top of assert_frozen —
    # the generic seasons check would still refuse, but without naming the
    # held season, and this asserts the refusal is BY NAME and precedes any
    # read (the "connection" here is an object that cannot be read from).
    parser = T.build_parser()
    for action in parser._actions:
        for opt in action.option_strings:
            assert "holdout" not in opt.lower(), opt
    assert "unlock_holdout=True" not in Path(T.__file__).read_text(encoding="utf-8")
    with pytest.raises(T.RunnerBug, match="HOLDOUT") as exc:
        T.evaluate_cell(object(), G.DEFAULT_CELL, cfg, seasons=(2021, 2022, 2023, 2024))
    assert "2024" in str(exc.value)
    with pytest.raises(T.RunnerBug):
        T.evaluate_cell(object(), G.DEFAULT_CELL, cfg, seasons=(2021, 2022))   # not TRAIN
    assert not cfg.results_dir.exists()                       # nothing was written


def test_the_runner_refuses_the_live_database(tmp_path, capsys):
    # Mutant killed: comparing the path STRING to the live path — the
    # non-normalised spelling below (db/../db/ziggurat.sqlite) differs as text
    # and resolves to the live file.  A snapshot elsewhere is accepted.
    live = R.DEFAULT_DB
    assert live == T.LIVE_DB
    with pytest.raises(T.LiveDatabaseRefused):
        T.refuse_live_database(live)
    with pytest.raises(T.LiveDatabaseRefused):
        T.refuse_live_database(live.parent / ".." / live.parent.name / live.name)
    snapshot = tmp_path / "ziggurat-4.2-snapshot.sqlite"
    assert T.refuse_live_database(snapshot) == snapshot
    rc = T.main(["--db", str(live), "--dry-run", "--cache-dir", str(tmp_path / "c")])
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().err
    assert not (tmp_path / "c").exists()
    # the dry run on an acceptable path prints every cell's cache key and
    # opens nothing (the snapshot path does not even exist)
    rc = T.main(["--db", str(snapshot), "--dry-run", "--cache-dir", str(tmp_path / "c")])
    out = capsys.readouterr().out
    assert rc == 0 and DEFAULT_KEY in out and "44 round-1 cells" in out
    assert not (tmp_path / "c").exists()


def test_the_runner_refuses_a_setting_that_moves_hit_places(cfg, ro):
    # Mutant killed: grading at the literal 5 instead of asserting the
    # config — the run would silently proceed at H=5 while the result file
    # claimed hit_places=4.  Every frozen §7.4 value is refused BEFORE a grade
    # log line or a result file exists.
    moved = {
        "hit_places": T.SearchConfig(db=cfg.db, cache_dir=cfg.cache_dir, hit_places=4),
        "owned_delta": T.SearchConfig(db=cfg.db, cache_dir=cfg.cache_dir, owned_delta=9.0),
        "grade_as_of": T.SearchConfig(db=cfg.db, cache_dir=cfg.cache_dir, grade_as_of="2026-09-04"),
        "market": T.SearchConfig(db=cfg.db, cache_dir=cfg.cache_dir, market="ros"),
    }
    for name, bad in moved.items():
        with pytest.raises(T.RunnerBug, match=name):
            T.evaluate_cell(ro, G.DEFAULT_CELL, bad)
    assert not cfg.grade_log.exists() and not cfg.results_dir.exists()


# ---------------------------------------------------------------------------
# one setting end to end on the synthetic fixture
# ---------------------------------------------------------------------------


def test_a_result_file_is_written_atomically_and_reused_on_resume(cfg, ro, monkeypatch):
    # Mutant killed: dropping the load_result check at the top of
    # evaluate_cell — the second call would re-enter obtain_records, which
    # finds the freeze verified and reloads, so the spy on R.replay would
    # NOT fire ... hence the spy is on build_scorecard too: a resumed setting
    # neither decides nor grades.  Atomicity: the file arrives through
    # D._write_atomic and no temp file survives.
    writes: list[Path] = []
    real = T.D._write_atomic

    def spy(path, data):
        writes.append(Path(path))
        real(path, data)
    monkeypatch.setattr(T.D, "_write_atomic", spy)
    res = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    path = T.result_path(cfg, DEFAULT_KEY)
    assert res["cache_key"] == DEFAULT_KEY and res["phase"] == "decide+freeze"
    assert path in writes and path.exists()
    assert not [p for p in cfg.results_dir.iterdir() if p.suffix != ".json"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["sha256"] == T._self_digest(payload) == res["sha256"]
    assert len(cfg.grade_log.read_text().splitlines()) == 3      # one line per strategy
    # resume: neither replay nor build_scorecard runs again
    real_build, real_replay = T.S.build_scorecard, T.R.replay
    monkeypatch.setattr(T.R, "replay", lambda *a, **k: pytest.fail("replay ran on resume"))
    monkeypatch.setattr(T.S, "build_scorecard",
                        lambda *a, **k: pytest.fail("build_scorecard ran on resume"))
    again = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    assert again["sha256"] == res["sha256"] and again["L_w"] == res["L_w"]
    assert len(cfg.grade_log.read_text().splitlines()) == 3
    # a tampered result file is set aside, never reused — the setting is redone
    # (the freeze still verifies, so redoing it is a load + grade).  The READ is
    # non-destructive (load_result/verify_result rename nothing); the setting-aside
    # happens inside evaluate_cell, AFTER assert_frozen.
    monkeypatch.setattr(T.S, "build_scorecard", real_build)
    path.write_text(path.read_text().replace('"n_weeks": 2', '"n_weeks": 3'))
    assert T.load_result(cfg, DEFAULT_KEY) is None
    assert T.verify_result(cfg, DEFAULT_KEY) == (None, "digest")
    assert path.exists() and not [p.name for p in cfg.results_dir.iterdir()
                                 if ".corrupt-" in p.name]
    redone = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    assert [p.name for p in cfg.results_dir.iterdir() if ".corrupt-" in p.name]
    assert redone["phase"] == "load" and redone["L_w"] == res["L_w"]
    assert T.load_result(cfg, DEFAULT_KEY)["n_weeks"] == 2
    # round 1 (serial) over the default + one moved cell resumes the default
    # and rebuilds the summary from the files
    monkeypatch.setattr(T.R, "replay", real_replay)
    out = T.run_round1(cfg, cells=(G.DEFAULT_CELL, G.cell_by_id("carries=2")), conn=ro,
                       log=lambda s: None)
    rows = [json.loads(line) for line in (cfg.cache_dir / T.SUMMARY_FILE).read_text().splitlines()]
    assert [r["cell_id"] for r in rows] == ["default", "carries=2"]
    assert out["carries=2"]["reference_cache_key"] == DEFAULT_KEY
    assert rows[1]["per_season_D"] == {"2023": out["carries=2"]["D"]["per_season"][2023]}


def test_a_setting_that_empties_a_week_is_recorded_infeasible_not_averaged_in(cfg, ro, monkeypatch):
    # Mutant killed: imputing 0.0 for a week the setting did not contribute —
    # D would read (d_5 + 0) / 2 over n_common = 2 and the row could be
    # ranked.  Instead the week is ABSENT from d_w, named in only_default, D
    # is the mean over the one common week, and the HARD screen (A11 common
    # weeks) records the row as argmax-ineligible.  A setting with NO
    # contributing week at all is `infeasible`: no D, no permutation, no
    # d vector, never in a family.
    default = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    assert set(default["L_w"]) == {"2023,5", "2023,6"}
    absurd = dict(G.DEFAULT_CELL.floors)
    for k in absurd:
        absurd[k] = 0.99 if k.endswith(("share", "pct")) else 9999.0
    cell = G.combined_cell(absurd, cell_id="absurd", axis="test", level="9999", eligible=True)
    res = T.evaluate_cell(ro, cell, cfg, reference=default)
    assert set(res["L_w"]) == {"2023,5"}                 # the usage arm emptied week 6
    d = res["D"]
    assert set(d["d_w"]) == {"2023,5"} and d["n_common"] == 1
    assert d["only_default"] == [(2023, 6)] and d["only_g"] == []
    assert d["mean"] == pytest.approx(res["L_w"]["2023,5"] - default["L_w"]["2023,5"])
    assert d["mean"] != pytest.approx(d["d_w"]["2023,5"] / 2)     # not averaged over 2
    assert d["per_season_n"] == {2023: 1}
    assert "A11" in res["hard_failures"] and res["argmax_eligible"] is False
    assert res["infeasible"] is None
    # no contributing week at all → infeasible, recorded, not paired
    monkeypatch.setattr(T.S.MarketScorecard, "per_week_depth_lift", lambda self, split: {})
    shutil.rmtree(cfg.results_dir)
    empty = T.evaluate_cell(ro, G.cell_by_id("carries=2"), cfg, reference=default)
    assert empty["n_weeks"] == 0 and empty["L_w"] == {}
    assert empty["infeasible"] and "0 contributing weeks" in empty["infeasible"]
    assert empty["D"] is None and empty["argmax_eligible"] is False
    assert T.diff_vector(empty) == {} and T._argmax([empty]) is None
    assert "A5" in empty["hard_failures"]
    with pytest.raises(T.InfeasibleSetting):
        T.paired_lift({}, default["L_w"], cfg)


def test_null_is_invariant_to_the_generator_floors(cfg, ro):
    # The harness property the whole design leans on (§5.1): the null is the
    # eligibility-matched r0 universe, which no floor touches.  Mutant killed
    # (in the harness, not here): a null drawn from the generator's pool.
    default = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    moved = T.evaluate_cell(ro, G.cell_by_id("carries=2"), cfg, reference=default)
    scaled = T.evaluate_cell(ro, G.cell_by_id("global_scale=0.5"), cfg, reference=default)
    assert default["null_gradeable"] == 459
    for r in (moved, scaled):
        assert r["null_gradeable"] == default["null_gradeable"]
        assert r["null_hits"] == default["null_hits"]
        assert r["null_by_band"] == default["null_by_band"]
        assert r["strategies"]["random_k"]["null_gradeable"] == default["null_gradeable"]
    # ... while the picks did move (the setting is not the default)
    assert scaled["cache_key"] != default["cache_key"]
    assert scaled["D_hit"]["differing_gsis"] >= 1 or moved["D_hit"]["differing_gsis"] >= 1


def test_every_row_carries_the_per_season_paired_differences(cfg, ro):
    # Mutant killed: dropping `per_season` / `d_w` / `only_*` from
    # paired_lift — the summary row and the result file both carry them,
    # keyed by season, with the week-level d vector they are the means of.
    default = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    res = T.evaluate_cell(ro, G.cell_by_id("carries=2"), cfg, reference=default)
    d = res["D"]
    assert set(d["d_w"]) == {"2023,5", "2023,6"} == set(res["L_w"]) == set(default["L_w"])
    assert d["per_season"] == {2023: pytest.approx(sum(d["d_w"].values()) / 2)}
    assert d["per_season_n"] == {2023: 2} and d["n_common"] == 2
    assert d["only_g"] == [] and d["only_default"] == []
    assert d["per_week_interval"]["label"] == T.PER_WEEK_INTERVAL_LABEL
    assert d["block_interval"]["label"] == T.BLOCK_INTERVAL_LABEL
    assert d["permutation"]["b"] == 200 and d["permutation"]["seed"] == 0
    for k, v in d["d_w"].items():
        assert v == pytest.approx(res["L_w"][k] - default["L_w"][k])
    # the default pairs against itself: every difference is exactly zero
    assert set(default["D"]["d_w"].values()) == {0.0} and default["D"]["permutation"]["p"] == 1.0
    stored = json.loads(T.result_path(cfg, res["cache_key"]).read_text())
    assert stored["D"]["per_season"] == {"2023": d["per_season"][2023]}
    assert T.summary_row(stored)["per_season_D"] == {"2023": d["per_season"][2023]}


# ---------------------------------------------------------------------------
# round 2 — the procedure, exercised on canned per-week vectors.  No harness
# runs here: `evaluate` and `decide` are mocks that return the lift vector the
# default would give plus a per-axis delta, so what is pinned is the RULE
# (keep only permutation-cleared increments; report a reverse disagreement;
# stop a bisection within tolerance; count every attempt), not the data.
# ---------------------------------------------------------------------------

WEEKS = tuple((s, w) for s in (2021, 2022) for w in range(1, 15)) + tuple(
    (2023, w) for w in range(1, 13))                                  # 40 TRAIN weeks
DEFAULT_LW = {f"{s},{w}": 0.30 + 0.01 * ((i * 7) % 5) for i, (s, w) in enumerate(WEEKS)}


def _delta(moved: set[str], i: int) -> float:
    """The canned effect of a cell's moved axes at week index ``i``."""
    d = 0.0
    if "carries" in moved:
        d += 0.20                                     # cleared everywhere it is tried
    if "targets" in moved:
        # a real candidate on its own (+0.30 vs default) but pure noise on top of
        # carries (alternating sign, mean 0) -> its forward increment is not cleared
        d += (0.20 if i % 2 else -0.20) if "carries" in moved else 0.30
    if "receptions" in moved:
        d += 0.15 if "carries" in moved else 0.10   # cleared alone too, worth more on carries
    return d


def _canned(cfg, cell, *, moved: set[str] | None = None, pool_median: float = 160.0,
            reference: dict | None = None, over: dict | None = None) -> dict:
    """A result in the runner's own shape for a cell whose L_w is the default
    vector plus `_delta`; written through write_result with a stub freeze
    manifest so load_result / load_results treat it as done."""
    moved = set(cell.moved_axes) if moved is None else moved
    L_w = {k: v + _delta(moved, i) for i, (k, v) in enumerate(DEFAULT_LW.items())}
    ref_L = (reference or {}).get("L_w") or DEFAULT_LW
    key = T.build_params(cell).cache_key()
    res = {
        "version": T.RESULT_VERSION, "cell_id": cell.cell_id, "cache_key": key,
        "axis": cell.axis, "level": cell.level, "round": cell.round, "eligible": cell.eligible,
        "argmax_eligible": bool(cell.eligible), "inert": False,
        "infeasible": None, "hard_failures": [], "flags": [],
        "floors": dict(cell.floors),
        "log_ratios": {k: __import__("math").log(v / G.DEFAULT_CELL.floors[k])
                       for k, v in cell.floors.items()},
        "L_w": L_w, "n_weeks": len(L_w), "graded": 3 * len(L_w),
        "D": T.paired_lift(L_w, ref_L, cfg), "D_hit": {"mean": 0.0, "differing_gsis": 9},
        "pool": {"median": pool_median},
        "picks_by_week": {k: [f"g{i}", f"g{i + 1}", f"g{i + 2}"] for i, k in enumerate(L_w)},
        "hit_places": cfg.hit_places, "owned_delta": cfg.owned_delta,
        "grade_as_of": cfg.grade_as_of, "market": cfg.market, "fingerprint": None,
    }
    res.update(over or {})
    T.write_result(cfg, res)
    man = Path(cfg.cache_dir) / key / D.MANIFEST
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text("{}", encoding="utf-8")
    return res


@pytest.fixture
def round1_done(tmp_path):
    cfg = T.SearchConfig(db=tmp_path / "never-opened.sqlite", cache_dir=tmp_path / "cache",
                         jobs=1, permutation_b=200)
    default = _canned(cfg, G.DEFAULT_CELL)
    for cid in ("carries=2", "targets=3", "receptions=2"):
        _canned(cfg, G.cell_by_id(cid), reference=default)
    # an axis with NO cleared level: alone it is noise, so it is skipped at step 1
    noise = G.cell_by_id("rushing_yards=12")
    _canned(cfg, noise, moved=set(), reference=default)
    return cfg, default


class _Mock:
    def __init__(self, cfg, default):
        self.cfg, self.default, self.calls, self.decides, self.keys = cfg, default, [], [], []

    def evaluate(self, cell, reference):
        self.calls.append(cell.cell_id)
        # faithful to evaluate_cell: a verifying result file for this cache key
        # is RETURNED, never recomputed or relabelled (two chain cells can
        # resolve to one 12-floor map, and then to one result file)
        key = T.build_params(cell).cache_key()
        existing = T.load_result(self.cfg, key)
        res = existing if existing is not None else _canned(self.cfg, cell, reference=reference)
        self.keys.append(res["cache_key"])
        return res

    def decide(self, cell):
        self.decides.append(cell.cell_id)
        return 200.0 / (cell.floors["carries"] / G.DEFAULT_CELL.floors["carries"])


def test_round2_forward_greedy_keeps_only_permutation_cleared_additions(round1_done):
    # Mutant killed: keeping every attempted addition (or keeping on D alone) —
    # targets' increment on top of carries has mean 0 and p ~ 0.5, so it must be
    # SKIPPED and its floor UNDONE before receptions is tried; a mutant that keeps
    # it leaves targets=3 in final_floors and reports two kept axes as three.
    cfg, default = round1_done
    m = _Mock(cfg, default)
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None)
    assert list(report["candidates"]) == ["carries", "targets", "receptions"]
    assert [s["axis"] for s in report["skipped_axes"]] == [
        "target_share", "air_yards_share", "offense_pct", "rushing_yards", "receiving_yards"]
    fwd = report["forward"]
    assert [a["axis"] for a in fwd["attempts"]] == ["carries", "targets", "receptions"]
    assert [a["axis"] for a in fwd["kept"]] == ["carries", "receptions"]
    assert [a["axis"] for a in fwd["skipped"]] == ["targets"]
    skipped = fwd["skipped"][0]
    assert skipped["increment_p"] >= G.PERMUTATION_ALPHA and skipped["reasons"] == [
        f"increment p={skipped['increment_p']} not < {G.PERMUTATION_ALPHA}"]
    assert all(a["increment_p"] < G.PERMUTATION_ALPHA for a in fwd["kept"])
    # the skipped addition is undone: targets back at shipped, carries + receptions moved
    ff = fwd["final_floors"]
    assert ff["targets"] == G.DEFAULT_CELL.floors["targets"]
    assert ff["carries"] == G.cell_by_id("carries=2").floors["carries"]
    assert ff["receptions"] == G.cell_by_id("receptions=2").floors["receptions"]
    # receptions was tried on top of the KEPT chain, not on top of the skipped one
    assert m.calls[2].startswith("fwd:3:receptions=2")
    assert fwd["attempts"][2]["floors"]["targets"] == G.DEFAULT_CELL.floors["targets"]
    assert fwd["final_D"] == pytest.approx(0.35) and fwd["final_cell_id"] == m.calls[2]
    # the winner is the forward chain end and clears G1; LOO restores each moved axis once
    assert report["winner"]["cell_id"] == fwd["final_cell_id"]
    assert report["winner"]["clears_G1"] is True and report["winner"]["is_default"] is False
    assert [a["axis"] for a in report["loo"]["attempts"]] == ["carries", "receptions"]
    assert report["loo"]["attempts"][1]["drop_from_winner"] == pytest.approx(0.15)
    # the family is label-based: round-1 eligible + forward attempts, never default / LOO / reverse
    members = set(report["family"]["members"])
    assert {"carries=2", "targets=3", "receptions=2", "rushing_yards=12"} <= members
    assert {a["cell_id"] for a in fwd["attempts"]} <= members
    assert not any(c.startswith(("loo:", "rev:", "control:", "bisect:")) for c in members)
    assert G.DEFAULT_CELL_ID not in members
    assert report["max_null"]["winner_D"] == pytest.approx(0.35)
    # round2.json and max-null.json are on disk and the summary carries round-2 rows
    on_disk = json.loads((cfg.cache_dir / T.ROUND2_FILE).read_text())
    assert on_disk["forward"]["kept"] == fwd["kept"]
    rows = [json.loads(line) for line in (cfg.cache_dir / T.SUMMARY_FILE).read_text().splitlines()]
    assert any(r["round"] == 2 and r["cell_id"] == fwd["final_cell_id"] for r in rows)


def test_reverse_order_disagreement_is_reported_not_resolved(round1_done):
    # Mutant killed: resolving the disagreement by taking the higher-D chain —
    # the reverse chain here ends HIGHER (receptions +0.10 then targets +0.30
    # = 0.40 vs forward's 0.35) and a mutant that prefers it, or that re-runs the
    # argmax over reverse results, moves the winner.  §4.4 step 6: the reverse
    # is a replicate, the disagreement is REPORTED, forward stays the winner.
    cfg, default = round1_done
    m = _Mock(cfg, default)
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None)
    rev = report["reverse"]
    assert rev["order"] == list(G.ROUND2_REVERSE_ORDER)
    assert rev["forward_kept_axes"] == ["carries", "receptions"]
    assert rev["reverse_kept_axes"] == ["receptions", "targets"]
    assert rev["disagrees"] is True and rev["forward_stays_winner"] is True
    assert rev["final_D"] == pytest.approx(0.40) and rev["final_D"] > report["forward"]["final_D"]
    assert report["winner"]["cell_id"] == report["forward"]["final_cell_id"]
    assert report["winner"]["D"] == pytest.approx(0.35)
    assert report["winner"]["floors"] == report["forward"]["final_floors"]
    # the reverse chain's cells were evaluated (recorded), never ranked: targets
    # on top of receptions clears (it did not on top of carries), and carries on
    # top of both is a negative increment -> skipped
    assert [a["axis"] for a in rev["attempts"]] == ["receptions", "targets", "carries"]
    assert [a["axis"] for a in rev["skipped"]] == ["carries"]
    assert rev["final_floors"] != report["forward"]["final_floors"]
    assert not any(c.startswith("rev:") for c in report["family"]["members"])
    # a control matched on the winner's median pool was graded and its churn reported
    ctl = report["control"]
    assert ctl["bisection"]["status"] == "matched" and ctl["grade"]["cell_id"].startswith("control:")
    assert abs(ctl["bisection"]["median"] - ctl["bisection"]["target"]) <= G.BISECTION_POOL_TOLERANCE
    assert ctl["churn"]["weeks"] == 40


def test_scale_only_bisection_stops_within_tolerance_or_reports_unmatched():
    # Mutant killed: a bisection that exceeds max_trials, or that reports the
    # nearest trial as "matched" when it is outside tolerance (the doc: "never
    # rounded up to a match"), or that takes a target beyond the endpoints as
    # something to search for.
    def pool(c):                                    # monotone non-increasing in c
        return 200.0 / c
    hit = T.bisect_scale(160.0, decide=pool)
    assert hit.status == "matched" and hit.c == 1.25 and hit.median == 160.0
    assert [c for c, _ in hit.trials] == [0.5, 2.0, 1.25] and len(hit.trials) == 3
    assert abs(hit.median - hit.target) <= hit.tolerance == G.BISECTION_POOL_TOLERANCE
    edge = T.bisect_scale(398.0, decide=pool)
    assert edge.status == "matched" and edge.c == 0.5 and len(edge.trials) == 1
    for target in (500.0, 50.0):                    # outside [pool(2.0), pool(0.5)] = [100, 400]
        miss = T.bisect_scale(target, decide=pool)
        assert miss.status == "unmatched" and miss.c is None and miss.median is None
        assert len(miss.trials) == 2
    # a step function has no c within tolerance of 250: six trials, then "exhausted"
    def step(c):
        return 400.0 if c < 1.0 else 100.0
    ex = T.bisect_scale(250.0, decide=step)
    assert ex.status == "exhausted" and ex.c is None and ex.median is None
    assert len(ex.trials) == G.BISECTION_DECIDE_TRIALS == 6
    assert all(abs(m - 250.0) > G.BISECTION_POOL_TOLERANCE for _, m in ex.trials)
    # whenever it says matched, the match is inside tolerance — over a sweep of targets
    for target in range(100, 401, 7):
        r = T.bisect_scale(float(target), decide=pool)
        assert len(r.trials) <= 6
        if r.status == "matched":
            assert abs(r.median - target) <= 5 and r.median == pool(r.c)
        else:
            assert r.status == "exhausted" and r.c is None


def test_skipped_attempts_are_counted(round1_done):
    # Mutant killed: a budget that counts only KEPT additions (or only forward
    # ones) — every evaluate call is an attempt, every attempt is in the report,
    # and the settings cap counts result files, not kept cells.
    cfg, default = round1_done
    m = _Mock(cfg, default)
    before = T.count_settings(cfg)
    keys_before = set(T.load_results(cfg))
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None)
    b = report["budget"]
    assert (b["forward_attempts"], b["forward_kept"], b["forward_skipped"]) == (3, 2, 1)
    assert (b["reverse_attempts"], b["reverse_skipped"]) == (3, 1)
    assert b["loo_attempts"] == 2 and b["bisection_trials"] == 3 and b["control_grades"] == 1
    assert b["round2_attempts_total"] == 3 + 2 + 3 + 1 + 3 == 12 <= G.ROUND2_MAX
    assert len(m.calls) == 3 + 2 + 1 + 3 and len(m.decides) == 3
    assert b["settings_before"] == before == 5
    assert b["new_settings"] == T.count_settings(cfg) - before
    # attempts are counted per evaluate call; SETTINGS per distinct cache key — a
    # LOO restore that lands on an already-graded map is an attempt, not a setting
    assert b["new_settings"] == len(set(m.keys) - keys_before) == 5 < len(m.calls) == 9
    assert b["forward_not_attempted"] == 0
    # the cap counts settings on disk: a config whose ceiling is already reached refuses
    tight = T.SearchConfig(db=cfg.db, cache_dir=cfg.cache_dir, jobs=1, permutation_b=200,
                           max_settings=T.count_settings(cfg))
    with pytest.raises(T.BudgetExceeded):
        T.evaluate_cell(None, G.cell_by_id("carries=5"), tight, reference=default)


# ---------------------------------------------------------------------------
# the §6 admissibility screen, row by row.  Pinned DIRECTLY over a synthetic
# result dict, because on any harness-backed fixture the TRAIN-scale rows
# (A2 54/162, A3 8853, A5 45, A11 40) can never pass, so no fixture-based test
# can observe a single row's verdict: neutering one leaves four others failing.
# ---------------------------------------------------------------------------


def _row(**over) -> dict:
    """A result dict that PASSES every HARD row and raises no flag."""
    bands = [["1-36", 5, 10], ["49-60", 3, 10], [">150", 2, 10]]
    row = {
        "floors": dict(G.DEFAULT_CELL.floors),
        "weeks_decided": 54, "decisions": 162, "null_gradeable": 8853,
        "generator_failures": [], "pool": {"median": 77.5, "min": 57},
        "n_weeks": 45, "depth_unmatched": 0,
        "D_hit": {"differing_gsis": 9, "mean": 0.0, "b": 1, "c": 1, "p": 1.0,
                  "significant_negative": False},
        "D": {"n_common": 45}, "picks_by_band": bands,
        "position_mix": {"WR": {"share": 0.7, "hit_rate": 0.8},
                         "RB": {"share": 0.2, "hit_rate": 0.5},
                         "TE": {"share": 0.1, "hit_rate": 0.4}},
    }
    row.update(over)
    return row


def _verdicts(result, reference=None) -> tuple[list[str], list[str]]:
    adm = T.screen(result, reference if reference is not None else _row())
    hard = [k for k, v in adm.items() if v["kind"] == "hard" and not v["pass"]]
    flags = [k for k, v in adm.items() if v["kind"] == "flag" and not v["pass"]]
    return hard, flags


def test_every_hard_admissibility_row_refuses_its_own_violation():
    # Mutant killed, one per row: `_check("hard", True, ...)` on A1..A8, A10 or
    # A11 (the always-pass stub) — every case below then reports NO hard
    # failure.  Before this test the WHOLE screen was unpinned: ten of the
    # twelve rows could be disabled with the backtest suite green, including A7
    # (§6.1 "the item's central Goodhart fence") and A8 (§6.2 "without this
    # rule the tie-break becomes the real optimizer").
    assert _verdicts(_row()) == ([], [])
    cases = {
        # A1 — every floor > 0, every SHARE floor < 1
        "A1": [_row(floors={**G.DEFAULT_CELL.floors, "carries": 0.0}),
               _row(floors={**G.DEFAULT_CELL.floors, "carries": -1.0}),
               _row(floors={**G.DEFAULT_CELL.floors, "target_share": 1.0}),
               _row(floors={**G.DEFAULT_CELL.floors, "emergence:offense_pct": 1.5})],
        # A2 — the tripwire on the decide phase
        "A2": [_row(weeks_decided=53), _row(decisions=161)],
        # A3 — the null count is a function of `places` only, and no crash
        "A3": [_row(null_gradeable=8852), _row(generator_failures=[["2023", 5, "boom"]])],
        # A4 — pool floor: median >= 20 AND every week >= 3k = 9
        "A4": [_row(pool={"median": 19.0, "min": 57}),
               _row(pool={"median": 77.5, "min": 8})],
        # A5 — contributing weeks
        "A5": [_row(n_weeks=44)],
        # A6 — an unmatched depth band is never averaged in
        "A6": [_row(depth_unmatched=1)],
        # A7 — the pool CEILING, the recall-flood fence
        "A7": [_row(pool={"median": 156.0, "min": 57})],
        # A8 — the inert declaration, at its exact frozen boundary
        "A8": [_row(D_hit={**_row()["D_hit"], "differing_gsis": 4}),
               _row(D_hit={**_row()["D_hit"], "differing_gsis": 0})],
        # A10 — sign concordance: D_hit >= 0 and no significant negative McNemar
        "A10": [_row(D_hit={**_row()["D_hit"], "mean": -0.01}),
                _row(D_hit={**_row()["D_hit"], "significant_negative": True, "b": 9, "c": 0})],
        # A11 — common support with the default
        "A11": [_row(D={"n_common": 39})],
    }
    for row_id, rows in cases.items():
        for r in rows:
            hard, flags = _verdicts(r)
            assert hard == [row_id], (row_id, hard, flags)
    # A8 passes at exactly 5 differing slots and fails at 4 (the frozen number)
    assert _verdicts(_row(D_hit={**_row()["D_hit"], "differing_gsis": 5}))[0] == []
    assert _verdicts(_row(D_hit={**_row()["D_hit"], "differing_gsis": 4}))[0] == ["A8"]


def test_the_two_flag_rows_annotate_and_stay_ranked():
    # Mutant killed: making A9 / A12 HARD (the §6 preamble's rule is that a
    # FLAG "is annotated, its re-read is printed, and the cell STAYS ranked"),
    # or never firing them at all.
    ref = _row()
    shifted = _row(picks_by_band=[["1-36", 5, 30], ["49-60", 0, 0], [">150", 0, 0]])
    hard, flags = _verdicts(shifted, ref)
    assert hard == [] and flags == ["A9"]
    adm = T.screen(shifted, ref)
    assert adm["A9"]["value"]["band_standardised"] is not None
    assert any(abs(v) > T.STRATUM_SHIFT_PP for v in adm["A9"]["value"]["shift_pp"].values())
    pos = _row(position_mix={"WR": {"share": 0.2, "hit_rate": 0.8},
                             "RB": {"share": 0.7, "hit_rate": 0.5},
                             "TE": {"share": 0.1, "hit_rate": 0.4}})
    hard, flags = _verdicts(pos, ref)
    assert hard == [] and flags == ["A12"]
    assert T.screen(pos, ref)["A12"]["value"]["stratified"]["RB"]["default"] == 0.5


def test_the_reference_cell_is_not_screened_against_itself(cfg, ro):
    # Mutant killed: the unconditional `_check("hard", differing >= 5, ...)`.
    # The DEFAULT pairs against ITSELF, so `differing_gsis` is 0 by
    # construction; the pre-audit screen therefore wrote `hard_failures:
    # ["A8"]` and `inert: true` onto the baseline row — the row every other row
    # is measured against, the row §7.1(3) can name the winner, and the row
    # §7.2's G3 is read on.  The COUNT is still carried (§6 D7); only the
    # verdict says "not screened against itself".
    default = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    # (the 2-week fixture cannot satisfy the TRAIN-scale rows A2/A3/A5/A11 —
    # what matters here is that A8, the one REFERENCE-RELATIVE hard row, is not
    # among them on the baseline row)
    assert "A8" not in default["hard_failures"]
    assert default["inert"] is None                      # not applicable, not "true"
    a8 = default["admissibility"]["A8"]
    assert a8["pass"] is True and a8["value"]["self_comparison"] is True
    assert a8["value"]["differing_gsis"] == 0 == default["D_hit"]["differing_gsis"]
    assert T.summary_row(default)["inert"] is None
    # a MOVED cell is screened normally against it
    moved = T.evaluate_cell(ro, G.cell_by_id("global_scale=0.5"), cfg, reference=default)
    assert moved["admissibility"]["A8"]["value"] == moved["D_hit"]["differing_gsis"]
    assert moved["inert"] is (moved["D_hit"]["differing_gsis"] < 5)


def test_the_resolved_floor_map_is_asserted_as_two_separate_maps(cfg):
    # Mutant killed: `if False and params.breakout_floors != want_breakout`
    # (either half) — the §7.4 assertion the frozen list spells out as "checked
    # as the two maps separately (breakout_floors and emergence_floors)" was
    # pinned by nothing, so a refactor of Cell.generator / build_params /
    # ReplayParams could re-spell the prefix split silently.
    cell = G.cell_by_id("carries=2")
    params = T.build_params(cell)
    T.assert_frozen(params, cell, cfg)                    # the honest pair passes
    bad_diff = dataclasses.replace(cell, differenced=("7.0",) + cell.differenced[1:])
    with pytest.raises(T.RunnerBug, match="breakout_floors"):
        T.assert_frozen(params, bad_diff, cfg)
    bad_em = dataclasses.replace(cell, emergence=("11.0",) + cell.emergence[1:])
    with pytest.raises(T.RunnerBug, match="emergence_floors"):
        T.assert_frozen(params, bad_em, cfg)
    # the §3.7 footgun: the EMERGENCE values sent through the differenced map
    # (and vice versa).  Both maps hold known names and 12 distinct keys, so
    # only a two-map equality check can see it.
    swapped = dict(cell.floors)
    for m in G.EMERGENCE_AXES:
        swapped[m], swapped[D.EMERGENCE_PREFIX + m] = (
            swapped[D.EMERGENCE_PREFIX + m], swapped[m])
    crossed = dataclasses.replace(params, generator=tuple(sorted(swapped.items())))
    with pytest.raises(T.RunnerBug, match="breakout_floors"):
        T.assert_frozen(crossed, cell, cfg)



# ---------------------------------------------------------------------------
# the isolation fences: the live DB, the canonical cache dir, and the panel
# fingerprint that verifies the snapshot is the state the doc froze
# ---------------------------------------------------------------------------


def test_every_entry_point_refuses_the_live_database_and_the_canonical_cache(tmp_path):
    # Mutant killed: the pre-audit code, i.e. `refuse_live_database` called
    # ONLY from main().  Then SearchConfig(db=LIVE_DB) constructs, and
    # run_round1 / run_round2 / evaluate_cell / the pool initializer each open
    # the live file the 20-min timers write to — the F7 snapshot isolation was
    # absent from the sanctioned entry point (§11.5 calls this module "a Python
    # API with a thin __main__").
    with pytest.raises(T.LiveDatabaseRefused):
        T.SearchConfig(db=T.LIVE_DB, cache_dir=tmp_path / "c")
    with pytest.raises(T.LiveDatabaseRefused):
        T.SearchConfig(db=T.LIVE_DB.parent / ".." / T.LIVE_DB.parent.name / T.LIVE_DB.name,
                       cache_dir=tmp_path / "c")
    # ... and the cache-dir half: Appendix A says the canonical replay dir "is
    # untouched by the search and holds the ledger"
    with pytest.raises(T.CanonicalCacheRefused):
        T.SearchConfig(db=tmp_path / "snap.sqlite", cache_dir=T.CANONICAL_CACHE_DIR)
    with pytest.raises(T.CanonicalCacheRefused):
        T.SearchConfig(db=tmp_path / "snap.sqlite",
                       cache_dir=T.CANONICAL_CACHE_DIR.parent / "." / "replay")
    ok = T.SearchConfig(db=tmp_path / "snap.sqlite", cache_dir=tmp_path / "cache")
    assert ok.db == tmp_path / "snap.sqlite"
    assert T.refuse_canonical_cache_dir(T.SEARCH_CACHE_DIR) == T.SEARCH_CACHE_DIR


def test_the_cli_refuses_the_canonical_cache_dir_by_name(tmp_path, capsys):
    rc = T.main(["--db", str(tmp_path / "snap.sqlite"), "--dry-run",
                 "--cache-dir", str(T.CANONICAL_CACHE_DIR)])
    err = capsys.readouterr().err
    assert rc == 2 and "REFUSED" in err and "ledger" in err
    assert not (T.CANONICAL_CACHE_DIR / T.RESULTS_DIR).exists()


def test_the_panel_fingerprint_uses_appendix_as_recipe_and_is_asserted_equal(tmp_path):
    # Mutant killed: the pre-audit recipe (per-row json.dumps + b"\n"), which
    # yielded a digest that could NEVER equal Appendix A's three frozen values —
    # so "to be re-taken on the snapshot before cell 1 and ASSERTED EQUAL" was
    # unmakeable, and the runner would have printed a fingerprint contradicting
    # the frozen document on an UNMOVED panel.
    path = tmp_path / "fp.sqlite"
    conn = connect(path)
    apply_schema(conn)
    rows = [(2021, w, "wp", "ppr-wr", "2021-10-0" + str(w), 10 + w, f"00-00{w:04d}")
            for w in (1, 2, 3)]
    for season, week, ecr, page, scrape, rank, gsis in rows:
        conn.execute(
            "INSERT INTO fpecr_panel (fantasypros_id, ecr_type, fp_page, scrape_date, season, "
            "nfl_week, week_basis, player, position, team, gsis_id, page_rank, pos_rank, "
            "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"fp-{gsis}", ecr, page, scrape, season, week, "inferred", gsis, "WR", "SEA",
             gsis, rank, rank, "2026-08-30", scrape))
    conn.commit()
    ro = R.open_ro(path)
    block = T.fingerprint(ro, seasons=(2021,))
    want = hashlib.sha256(repr([
        (season, week, ecr, page, scrape, rank, gsis)
        for season, week, ecr, page, scrape, rank, gsis in rows
    ]).encode()).hexdigest()[:16]
    assert block["fpecr_panel"]["2021"] == {"rows": 3, "sha256_16": want}
    # deterministic across calls, and the row_factory's Row objects never leak
    # their address into the digest
    assert T.fingerprint(ro, seasons=(2021,))["fpecr_panel"] == block["fpecr_panel"]
    # a HOLDOUT season is refused by name before a byte is read
    with pytest.raises(T.RunnerBug, match="HOLDOUT season 2024"):
        T.fingerprint(ro, seasons=(2021, 2024))
    # the Appendix A assertion: equal passes, one moved digest ABORTS by name
    frozen = {"2021": {"rows": 3, "sha256_16": want}}
    T.assert_panel_fingerprint(block, frozen)
    with pytest.raises(T.PanelFingerprintMismatch, match="re-versioned"):
        T.assert_panel_fingerprint(block, {"2021": {"rows": 3, "sha256_16": "0" * 16}})
    with pytest.raises(T.PanelFingerprintMismatch):
        T.assert_panel_fingerprint(block, {"2021": {"rows": 4, "sha256_16": want}})
    # the shipped constant is Appendix A's three TRAIN digests, verbatim
    assert set(T.PANEL_FINGERPRINT_BEFORE) == {"2021", "2022", "2023"}
    assert T.PANEL_FINGERPRINT_BEFORE["2021"] == {"rows": 71757,
                                                 "sha256_16": "47c395436f2fd23e"}
    assert T.PANEL_FINGERPRINT_BEFORE["2022"]["sha256_16"] == "5ada72013746e972"
    assert T.PANEL_FINGERPRINT_BEFORE["2023"]["sha256_16"] == "d8205abba3540d90"
    ro.close()
    conn.close()


def test_a_fingerprint_change_is_reported_and_exits_non_zero(cfg, monkeypatch, capsys):
    # Mutant killed: `{"unchanged": True}` written to a JSON file and read by
    # nobody — the pre-audit code computed the F7 verdict inline in main()'s
    # finally, printed nothing, kept exit 0 and OVERWROTE the file on the next
    # launch, so a resume erased the record of the launch in which the state
    # moved.  F7: "results are never pooled across fingerprints".
    a = {"fpecr_panel": {"2021": {"rows": 1, "sha256_16": "aa"}},
         "partitions": {"weekly_stats": {"2021": {"2026-07-25": 5}}}, "taken_at": "t0"}
    b = {"fpecr_panel": {"2021": {"rows": 1, "sha256_16": "aa"}},
         "partitions": {"weekly_stats": {"2021": {"2026-07-25": 5}}}, "taken_at": "t1"}
    assert T.fingerprint_drift(a, b)["unchanged"] is True         # taken_at is not the state
    moved = json.loads(json.dumps(b))
    moved["fpecr_panel"]["2021"]["sha256_16"] = "bb"
    moved["partitions"]["weekly_stats"]["2021"]["2026-09-30"] = 7
    drift = T.fingerprint_drift(a, moved)
    assert drift["unchanged"] is False
    assert drift["panel_seasons_moved"] == ["2021"] and drift["tables_moved"] == ["weekly_stats"]
    assert "must not be pooled" in drift["verdict"]
    assert T.fingerprint_agrees(a, b) and not T.fingerprint_agrees(a, moved)
    # end to end through main(): the verdict is PRINTED, the exit code is
    # non-zero, and each launch APPENDS its own record
    calls = {"n": 0}

    def fake_fingerprint(conn, **kw):
        calls["n"] += 1
        return a if calls["n"] == 1 else moved
    monkeypatch.setattr(T, "fingerprint", fake_fingerprint)
    monkeypatch.setattr(T, "assert_panel_fingerprint", lambda *a, **k: None)
    monkeypatch.setattr(T, "run_round1", lambda *a, **k: {})
    rc = T.main(["--db", str(cfg.db), "--cache-dir", str(cfg.cache_dir), "--round", "1"])
    captured = capsys.readouterr()
    assert rc == 4
    assert "FINGERPRINT CHANGED" in captured.err + captured.out
    body = json.loads((Path(cfg.cache_dir) / T.FINGERPRINT_FILE).read_text())
    assert body["unchanged"] is False and body["before"] and body["after"]
    log = [json.loads(x) for x in
           (Path(cfg.cache_dir) / T.FINGERPRINT_LOG).read_text().splitlines()]
    assert len(log) == 1 and log[0]["unchanged"] is False and log[0]["round"] == "1"
    # a second, CLEAN launch appends rather than erasing the launch that moved
    calls["n"] = 1
    monkeypatch.setattr(T, "fingerprint", lambda conn, **kw: moved)
    rc = T.main(["--db", str(cfg.db), "--cache-dir", str(cfg.cache_dir), "--round", "1"])
    log = [json.loads(x) for x in
           (Path(cfg.cache_dir) / T.FINGERPRINT_LOG).read_text().splitlines()]
    assert rc == 0 and len(log) == 2 and [r["unchanged"] for r in log] == [False, True]
    assert "UNCHANGED" in capsys.readouterr().out


def test_a_cached_result_from_another_database_state_is_refused(cfg, ro):
    # Mutant killed: resuming on `load_result` alone (the pre-audit path).
    # db_path is deliberately OUTSIDE params_hash (§11.5), so nothing in the
    # cache key can notice that a result file was produced from a different
    # snapshot — the argmax would then be chosen across two databases with no
    # refusal anywhere.
    fp_a = {"fpecr_panel": {"2021": {"rows": 1, "sha256_16": "aa"}}, "partitions": {}}
    fp_b = {"fpecr_panel": {"2021": {"rows": 1, "sha256_16": "bb"}}, "partitions": {}}
    first = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg, fingerprint_block=fp_a)
    assert first["fingerprint"] == fp_a
    again = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg, fingerprint_block=fp_a)
    assert again["sha256"] == first["sha256"]                    # same state: reused
    with pytest.raises(T.StaleCacheRefused, match="DIFFERENT database state"):
        T.evaluate_cell(ro, G.DEFAULT_CELL, cfg, fingerprint_block=fp_b)
    # a run that reads no fingerprint block at all cannot disagree with one
    assert T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)["sha256"] == first["sha256"]


def test_a_damaged_result_is_set_aside_under_a_unique_name_after_the_frozen_check(cfg, ro,
                                                                                 monkeypatch):
    # Two mutants killed: (1) `load_result` renaming the file aside as a READ
    # side effect — run_round1 calls it for every cell BEFORE assert_frozen, so
    # one invocation with a moved frozen value displaced the whole accumulated
    # result set before the abort that is supposed to precede any write; (2) a
    # SECOND-granular aside name, which collapses two asides for one key onto
    # one filename and silently overwrites the evidence the rename exists to
    # keep.
    default = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    path = T.result_path(cfg, default["cache_key"])
    path.write_text(path.read_text().replace('"phase": "decide+freeze"', '"phase": "tampered"'))
    assert T.verify_result(cfg, default["cache_key"]) == (None, "digest")
    assert T.load_result(cfg, default["cache_key"]) is None
    assert path.exists()                                   # the READ moved nothing
    # a moved frozen value aborts BEFORE the results directory is even scanned
    moved = T.SearchConfig(db=cfg.db, cache_dir=cfg.cache_dir, jobs=1, hit_places=4)
    reads: list[tuple] = []
    monkeypatch.setattr(T, "load_result", lambda *a, **k: reads.append(a) or None)
    with pytest.raises(T.RunnerBug, match="hit_places"):
        T.run_round1(moved, cells=(G.DEFAULT_CELL, G.cell_by_id("carries=2")), conn=ro,
                     log=lambda s: None)
    assert reads == []
    monkeypatch.undo()
    assert path.exists() and not list(cfg.results_dir.glob("*.corrupt-*"))
    # the quarantine itself: two asides for one key keep BOTH payloads
    first = T.quarantine_result(cfg, default["cache_key"], "digest")
    path.write_text('{"version": 1, "cache_key": "x"}')
    second = T.quarantine_result(cfg, default["cache_key"], "cache_key")
    assert first is not None and second is not None and first != second
    assert first.exists() and second.exists()
    assert str(os.getpid()) in first.name and "." in first.name.split("corrupt-")[1]
    assert json.loads(second.read_text())["cache_key"] == "x"
    # losing the race to another writer is SUCCESS, not an error
    assert T.quarantine_result(cfg, default["cache_key"], "absent") is None


# ---------------------------------------------------------------------------
# the per-setting sequence: the grade log's own fields, ONE grade per strategy
# with both sensitivity lists empty, the freeze refusals, the thin-band
# disclosure, and the parallel path that runs 43 of the 44 round-1 cells
# ---------------------------------------------------------------------------


def test_the_grade_log_carries_the_fields_it_exists_to_expose(cfg, ro):
    # Mutant killed: dropping `places` / `owned_delta` / `grade_as_of` from the
    # payload.  The pre-audit suite asserted only the LINE COUNT, so the log
    # could be emptied of exactly the values §6.4 says make "an undisclosed H-
    # or owned-delta shopping pass visible to an auditor".
    T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    lines = [json.loads(x) for x in cfg.grade_log.read_text().splitlines()]
    assert [r["strategy"] for r in lines] == list(G.STRATEGIES)
    for row in lines:
        assert row["cache_key"] == DEFAULT_KEY and row["market"] == cfg.market == "wp"
        assert row["places"] == cfg.hit_places == G.HIT_PLACES
        assert row["owned_delta"] == cfg.owned_delta == float(G.OWNED_DELTA)
        assert row["grade_as_of"] == cfg.grade_as_of == G.GRADE_AS_OF
        assert row["timestamp"].endswith("+00:00")
        assert set(row) == {"cache_key", "strategy", "market", "places", "owned_delta",
                            "grade_as_of", "timestamp"}


def test_each_setting_is_graded_once_per_strategy_with_empty_sensitivity_lists(cfg, ro,
                                                                              monkeypatch):
    # Mutant killed: passing S.HIT_PLACES_SENSITIVITY / OWNED_DELTA_SENSITIVITY
    # through (the harness default).  §11.5: "build_scorecard ONCE per strategy
    # with both sensitivity lists empty" — the extra H passes are ~3.5 s each and
    # are exactly the F8 traceability problem the grade log exists for.
    seen = []
    real = T.S.build_scorecard

    def spy(*a, **k):
        seen.append(k)
        return real(*a, **k)
    monkeypatch.setattr(T.S, "build_scorecard", spy)
    res = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    assert len(seen) == len(G.STRATEGIES) == 3
    assert [k["strategy"] for k in seen] == list(G.STRATEGIES)
    for k in seen:
        assert k["places_sensitivity"] == () and k["owned_sensitivity"] == ()
        assert k["unlock_holdout"] is False and k["places"] == 5
    assert len(cfg.grade_log.read_text().splitlines()) == 3
    assert res["hit_places"] == 5


def test_a_freeze_that_does_not_verify_is_refused_and_a_concurrent_one_is_loaded(cfg, ro,
                                                                                monkeypatch):
    # Two mutants killed: (1) `if False: raise FreezeRefused` — §11.5's "a
    # corrupt: status is REFUSED and reported, never --force'd" was pinned by
    # nothing; (2) letting D.freeze's FileExistsError escape, which reaches the
    # operator as a bare traceback whose text suggests --force, on a freeze
    # another worker wrote between the status read and the write.
    default = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    key = default["cache_key"]
    freeze = Path(cfg.cache_dir) / key
    T.result_path(cfg, key).unlink()                     # force the freeze path
    jsonl = sorted(freeze.glob("*.jsonl"))[0]
    jsonl.write_text(jsonl.read_text() + "\n")           # digest no longer matches
    assert D.freeze_status(cfg.cache_dir, T.build_params(G.DEFAULT_CELL)).startswith("corrupt")
    with pytest.raises(T.FreezeRefused, match=r"never --force's a freeze"):
        T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    # a CONCURRENT writer: the status read says missing, the freeze exists by the
    # time we write it.  If what landed verifies, LOAD it.
    shutil.rmtree(freeze)
    calls = {"n": 0}
    real_status = D.freeze_status

    def flaky(cache_dir, params):
        calls["n"] += 1
        return D.FREEZE_MISSING if calls["n"] == 1 else real_status(cache_dir, params)

    T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)             # writes the freeze again
    T.result_path(cfg, key).unlink()
    monkeypatch.setattr(T.D, "freeze_status", flaky)
    resumed = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    assert resumed["phase"] == "load" and resumed["L_w"] == default["L_w"]


def test_the_thin_band_variant_pairs_a_vector_of_numbers(cfg, ro, monkeypatch):
    # Mutant killed: `out["L_w_pooled"] = {key: v ...}` over DepthLifts.per_week
    # WITHOUT statistics.fmean — per_week holds each week's per-PICK list, so
    # paired_lift's float(v) raised an uncaught TypeError and the §6 thin-band
    # disclosure could never be produced at all.
    monkeypatch.setattr(T, "THIN_BAND_LINES", 10_000)     # every band cell is "thin"
    default = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    tb = default["thin_band"]
    assert tb["cells"] and tb["picks_on_thin_lines"] > 0 and str(T.THIN_BAND_LINES) in tb["note"]
    assert default["L_w_pooled"] and set(default["L_w_pooled"]) <= set(default["L_w"])
    assert all(isinstance(v, float) for v in default["L_w_pooled"].values())
    assert default["D_pooled"]["n_common"] == len(default["L_w_pooled"])
    assert set(default["D_pooled"]["d_w"].values()) == {0.0}     # the default vs itself
    moved = T.evaluate_cell(ro, G.cell_by_id("global_scale=0.5"), cfg, reference=default)
    assert moved["D_pooled"] is not None and moved["D_pooled"]["mean"] is not None
    # with no thin cell the two fields are NOT-APPLICABLE, and the row says so
    monkeypatch.setattr(T, "THIN_BAND_LINES", 10)
    shutil.rmtree(cfg.results_dir)
    plain = T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)
    assert plain["thin_band"]["cells"] == [] and plain["L_w_pooled"] is None
    assert "NOT-APPLICABLE" in plain["thin_band"]["note"] and plain["D_pooled"] is None


def test_a_per_week_vector_that_is_not_one_number_per_week_is_refused(cfg, ro, monkeypatch):
    # the same defect, fenced at the seam rather than left to paired_lift's
    # float() (mutant: returning the raw per-pick lists from per_week_depth_lift)
    monkeypatch.setattr(T.S.MarketScorecard, "per_week_depth_lift",
                        lambda self, split: {(2023, 5): [0.1, 0.2]})
    with pytest.raises(T.RunnerBug, match="not a number"):
        T.evaluate_cell(ro, G.DEFAULT_CELL, cfg)


def test_round1_runs_the_other_cells_in_a_spawn_pool(cfg, monkeypatch):
    # Mutant killed: dropping `initializer=_init_worker` (every worker then has
    # no connection), and switching the context to "fork" (sqlite3 connections
    # are not fork-safe — §11.5 mandates spawn).  The pool path runs 43 of the
    # 44 round-1 cells and NO test exercised it: every shipped test either
    # passed conn= or jobs=1, both of which force the serial branch.
    jobbed = T.SearchConfig(db=cfg.db, cache_dir=cfg.cache_dir, jobs=3, permutation_b=200)
    seen: list[str] = []
    real_ctx = mp.get_context

    def spy(name=None):
        seen.append(name)
        return real_ctx(name)
    monkeypatch.setattr(T.mp, "get_context", spy)
    cells = (G.DEFAULT_CELL, G.cell_by_id("carries=2"), G.cell_by_id("targets=3"),
             G.cell_by_id("receptions=2"))
    out = T.run_round1(jobbed, cells=cells, log=lambda s: None)
    assert seen == ["spawn"]
    assert set(out) == {c.cell_id for c in cells}
    assert all(r is not None for r in out.values())
    default_key = T.build_params(G.DEFAULT_CELL).cache_key()
    for cid, r in out.items():
        if cid != G.DEFAULT_CELL_ID:
            assert r["reference_cache_key"] == default_key       # resolved from DISK in the worker
    assert len(jobbed.grade_log.read_text().splitlines()) == 3 * len(cells)
    rows = [json.loads(x) for x in
            (Path(jobbed.cache_dir) / T.SUMMARY_FILE).read_text().splitlines()]
    assert {r["cell_id"] for r in rows} == {c.cell_id for c in cells}


def test_a_worker_that_cannot_open_the_database_aborts_instead_of_hanging(tmp_path):
    # Mutant killed: `_init_worker` letting R.open_ro raise.  CPython respawns
    # workers indefinitely when the INITIALIZER raises, so imap_unordered
    # neither returns nor raises and round 1 hangs forever with no result —
    # the silent-unbounded-hang class items 3.1 / 3.1b were fixed for.
    cfg = T.SearchConfig(db=tmp_path / "gone.sqlite", cache_dir=tmp_path / "c", jobs=2)
    T._init_worker(str(cfg.db), None)                    # must NOT raise
    assert T._WORKER_CONN is None and T._WORKER_INIT_ERROR
    with pytest.raises(T.RunnerBug, match="could not open"):
        T._worker_evaluate(("carries=2", cfg))
    T._init_worker(str(cfg.db), None)                    # leave no live connection behind


def test_a_pool_result_the_parent_cannot_read_back_is_a_data_loss_event(cfg, monkeypatch):
    # Mutant killed: `results[cell_id] = load_result(cfg, key)` with no guard —
    # a cell the worker reported as evaluated was recorded as None, LOGGED as a
    # success, and then vanished from the summary, from round 2's candidate set
    # and from the §5.3 max-null family while the run reported success.
    jobbed = T.SearchConfig(db=cfg.db, cache_dir=cfg.cache_dir, jobs=2, permutation_b=200)

    class _FakePool:
        def __init__(self, n, initializer=None, initargs=()):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def imap_unordered(self, fn, items):
            return iter([("carries=2", "deadbeefcafe")])

    class _FakeCtx:
        Pool = _FakePool
    monkeypatch.setattr(T.mp, "get_context", lambda name: _FakeCtx())
    with pytest.raises(T.RunnerBug, match="does not verify"):
        T.run_round1(jobbed, cells=(G.DEFAULT_CELL, G.cell_by_id("carries=2")),
                     log=lambda s: None)


def test_the_summary_is_rebuilt_even_when_the_round_aborts(cfg, monkeypatch, capsys):
    # Mutant killed: rebuild_summary called only as the LAST statement of
    # run_round1 / run_round2 — any abort (the setting cap, a refused freeze, a
    # Ctrl-C) then left summary.jsonl absent or describing an earlier, smaller
    # run beside a truer results/ directory, which is the one artefact a human
    # reads.  It is also IDEMPOTENT: a second rebuild neither duplicates nor
    # drops a row.
    monkeypatch.setattr(T, "assert_panel_fingerprint", lambda *a, **k: None)

    def half_way(config, **kw):
        _canned(config, G.DEFAULT_CELL)
        raise T.BudgetExceeded("the frozen cap is 80 (§8.3)")
    monkeypatch.setattr(T, "run_round1", half_way)
    rc = T.main(["--db", str(cfg.db), "--cache-dir", str(cfg.cache_dir), "--round", "1"])
    assert rc == 3 and "ABORTED: BudgetExceeded" in capsys.readouterr().err
    summary = Path(cfg.cache_dir) / T.SUMMARY_FILE
    rows = [json.loads(x) for x in summary.read_text().splitlines()]
    assert [r["cell_id"] for r in rows] == [G.DEFAULT_CELL_ID]
    before = summary.read_text()
    T.rebuild_summary(cfg)
    assert summary.read_text() == before                  # one writer, full rebuild


def test_no_scratch_test_modules_are_left_in_the_tests_directory():
    # Mutant killed: any tests/test_zz*.py scratch probe (several appeared and
    # vanished during the 4.2 audit).  pytest COLLECTS them, so a suite count
    # taken while one exists is inflated and gets attributed to this item.
    tests_dir = Path(__file__).resolve().parent
    assert sorted(p.name for p in tests_dir.glob("test_zz*.py")) == []
    assert sorted(p.name for p in tests_dir.glob("*scratch*")) == []


# ---------------------------------------------------------------------------
# round 2 — the four selection rules the frozen text spells out and the
# shipped code did not enforce: the LABEL-based max-null family, ties -> the
# DEFAULT, argmax-eligibility by LABEL for the reverse replicate and skipped
# attempts, and the two-condition keep rule
# ---------------------------------------------------------------------------


def test_the_max_null_family_is_label_based_not_screen_filtered(round1_done):
    # Mutant killed: building the family from `argmax_eligible` (label AND
    # screen AND feasible), the pre-audit rule.  §5.3: "the max-null family is
    # label-based: every argmax-eligible-LABEL cell evaluated, INCLUDING those
    # that fail the HARD screen or are inert (their d_w vectors enter as
    # computed; this can only raise the bar)".  T*_b is a pointwise MAX, so
    # dropping members can only LOWER the 95th-percentile bar — the one
    # direction §5.3 says cannot happen, on the item's only multiplicity
    # control and the gate that licenses the single holdout unlock.
    cfg, default = round1_done
    big = {k: v + 0.5 for k, v in DEFAULT_LW.items()}
    _canned(cfg, G.cell_by_id("targets=5"), moved=set(), reference=default,
            over={"L_w": big, "D": T.paired_lift(big, DEFAULT_LW, cfg),
                  "hard_failures": ["A10"], "argmax_eligible": False})
    _canned(cfg, G.cell_by_id("receptions=4"), moved=set(), reference=default,
            over={"inert": True, "argmax_eligible": False})
    m = _Mock(cfg, default)
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None)
    members = set(report["family"]["members"])
    assert {"targets=5", "receptions=4"} <= members          # screen-failing AND inert
    assert report["family"]["label_based_size"] == len(members)
    # §7.3 wants the two sizes quoted side by side, and they are DIFFERENT sets
    assert report["family"]["argmax_screen_passing_size"] == report["winner"]["ranked"]
    assert report["family"]["label_based_size"] > report["family"]["argmax_screen_passing_size"]
    assert G.DEFAULT_CELL_ID not in members                  # the baseline is never in it
    # the bar can only RISE when a member is added
    rows = {r["cell_id"]: r for r in T.load_results(cfg).values()}
    full = {cid: T.diff_vector(rows[cid]) for cid in members if cid in rows}
    sub = {cid: v for cid, v in full.items() if cid not in ("targets=5", "receptions=4")}
    bar_full = stats.max_null_step_down(full, b=cfg.permutation_b, seed=0).bar
    bar_sub = stats.max_null_step_down(sub, b=cfg.permutation_b, seed=0).bar
    assert bar_full >= bar_sub
    assert report["max_null"]["bar"] == pytest.approx(bar_full)
    # ... and the screen-failing cell has the HIGHEST D of all and is still not
    # the winner: "the runner enforces eligibility by cell label, never by score"
    assert rows["targets=5"]["D"]["mean"] == pytest.approx(0.5)
    assert report["winner"]["cell_id"] != "targets=5"
    assert report["winner"]["D"] == pytest.approx(0.35)


def test_a_flat_search_reports_the_default_as_the_winner(tmp_path):
    # Mutant killed: `winner = _argmax(ranked) or default`, the pre-audit line.
    # §7.1 rule 2: "If the default is in [the tie set] (D_max < 0.005), the
    # default wins", and rule 3: "the incumbent is the default, and it wins by
    # default".  D(default) is 0 by construction and the default carries
    # eligible=False, so it can NEVER be in `ranked` — the rule could not fire
    # at all, and a flat (or negative) search was reported as a tuned winner
    # while steps 4-5 spent settings on it.
    cfg = T.SearchConfig(db=tmp_path / "never-opened.sqlite", cache_dir=tmp_path / "cache",
                         jobs=1, permutation_b=200)
    default = _canned(cfg, G.DEFAULT_CELL)
    for cid in ("carries=2", "targets=3"):
        _canned(cfg, G.cell_by_id(cid), moved=set(), reference=default)   # no effect at all
    ranked = [r for r in T.load_results(cfg).values()
              if r["round"] == 1 and r.get("argmax_eligible")]
    assert T._argmax(ranked)["cell_id"] != G.DEFAULT_CELL_ID      # what the mutant would pick
    m = _Mock(cfg, default)
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None)
    w = report["winner"]
    assert w["is_default"] is True and w["cell_id"] == G.DEFAULT_CELL_ID
    assert w["tie_to_default"] is True and w["observed_best_D"] == pytest.approx(0.0)
    assert w["tie_band"] == G.TIE_BAND == 0.005 and "§7.1 rule 2/3" in w["tie_reason"]
    assert w["clears_G1"] is False
    # G4 is a computed verdict: a per-season mean of exactly 0.0 is not > 0
    assert w["clears_G4"] is False and set(w["per_season_D"]) == {"2021", "2022", "2023"}
    # steps 4-5 are skipped and SAY they were (§4.4 step 4), and G5 is vacuous
    assert report["loo"]["skipped"] and report["loo"]["clears_G5"] is True
    assert report["loo"]["G5_vacuous"] is True and "VACUOUSLY" in report["loo"]["G5_note"]
    assert report["control"]["skipped"] and report["budget"]["loo_attempts"] == 0
    assert report["budget"]["control_grades"] == 0 and m.decides == []


def test_the_reverse_replicate_and_skipped_attempts_are_labelled_ineligible(round1_done):
    # Mutant killed: `eligible=True` for every chain cell (the pre-audit line),
    # which wrote an argmax-ELIGIBLE label onto 8 reverse cells and every
    # skipped forward attempt.  §4.4 step 6 makes the reverse replicate
    # argmax-ineligible ("never resolved by picking whichever scored higher")
    # and §7.1 makes a skipped attempt argmax-INELIGIBLE — and the label is
    # what summary.jsonl, a resume and §7.3's LOSO read.  In this fixture the
    # reverse chain ends HIGHER than the forward one, so a downstream argmax
    # over rows LABELLED eligible would crown a reverse cell.
    cfg, default = round1_done
    m = _Mock(cfg, default)
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None)
    stored = {r["cell_id"]: r for r in T.load_results(cfg).values()}
    rev_rows = {cid: r for cid, r in stored.items() if cid.startswith("rev:")}
    assert rev_rows and all(r["eligible"] is False for r in rev_rows.values())
    assert all(r["argmax_eligible"] is False for r in rev_rows.values())
    assert report["reverse"]["final_D"] > report["forward"]["final_D"]
    kept_ids = {a["cell_id"] for a in report["forward"]["kept"]}
    skipped_ids = {a["cell_id"] for a in report["forward"]["skipped"]}
    assert kept_ids and skipped_ids
    for cid in kept_ids:
        assert stored[cid]["eligible"] is True             # the KEPT chain is eligible
    for cid in skipped_ids:
        assert stored[cid]["eligible"] is False
        assert stored[cid]["argmax_eligible"] is False
        assert "skipped by §4.4 step 3" in stored[cid]["argmax_ineligible_reason"]
    # and no ineligible row can reach the summary claiming otherwise
    rows = [json.loads(x) for x in
            (Path(cfg.cache_dir) / T.SUMMARY_FILE).read_text().splitlines()]
    for row in rows:
        if row["cell_id"].startswith("rev:") or row["cell_id"] in skipped_ids:
            assert row["eligible"] is False and row["argmax_eligible"] is False


def test_the_greedy_keeps_on_the_two_pre_registered_conditions_only(round1_done):
    # Mutant killed: the third rejection reason the shipped code added — "if not
    # res.get('argmax_eligible'): reasons.append('HARD screen failed ...')".
    # §4.4 step 3 is "KEEP it iff BOTH (a) the paired sign-flip test ... AND
    # (b) D(C_i) still clears the practical floor if D(C_{i-1}) did" and nothing
    # else, so a HARD-screen failure must not silently change the nesting; the
    # screen is enforced at the argmax (§7.1), and the state is RECORDED here.
    cfg, default = round1_done

    class _ScreenFail(_Mock):
        def evaluate(self, cell, reference):
            self.calls.append(cell.cell_id)
            over = ({"hard_failures": ["A10"], "argmax_eligible": False}
                    if cell.cell_id.startswith("fwd:1:") else None)
            res = _canned(self.cfg, cell, reference=reference, over=over)
            self.keys.append(res["cache_key"])
            return res
    m = _ScreenFail(cfg, default)
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None)
    first = report["forward"]["attempts"][0]
    assert first["cell_id"].startswith("fwd:1:") and first["kept"] is True
    assert first["reasons"] == []                          # (a) and (b) both cleared
    assert first["hard_failures"] == ["A10"] and first["argmax_eligible"] is False
    assert [a["axis"] for a in report["forward"]["kept"]] == ["carries", "receptions"]
    # the screen still decides RANKING: the screen-failing member is not ranked
    assert report["winner"]["cell_id"] != first["cell_id"]


def test_the_winner_carries_computed_G4_and_G5_verdicts(round1_done, monkeypatch):
    # Mutant killed: reporting only clears_G1 / clears_G2 (the pre-audit
    # winner block).  §7.2 requires all six gates before an unlock, and G4
    # ("the per-season mean of d_w is > 0 in each of 2021, 2022, 2023") and G5
    # ("no single-floor removal from the WINNER takes D below the practical
    # floor") are pure functions of fields already on the rows.
    cfg, default = round1_done
    m = _Mock(cfg, default)
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None)
    w = report["winner"]
    assert w["clears_G1"] is True and w["clears_G4"] is True
    assert set(w["per_season_D"]) == {"2021", "2022", "2023"}
    assert all(v > 0 for v in w["per_season_D"].values())
    loo = report["loo"]
    assert [a["axis"] for a in loo["attempts"]] == ["carries", "receptions"]
    assert loo["clears_G5"] is True and loo["G5_vacuous"] is False
    assert all(a["D"] >= G.PRACTICAL_FLOOR for a in loo["attempts"])
    assert "no single-floor removal" in loo["G5_note"]
    on_disk = json.loads((cfg.cache_dir / T.ROUND2_FILE).read_text())
    assert on_disk["winner"]["clears_G4"] is True and on_disk["loo"]["clears_G5"] is True
    # ... and G5 FAILS when an ablation drops below the floor (mutant killed:
    # `clears_G5 = True`): at a floor of +0.15 the carries restore, worth +0.10,
    # takes the winner below it
    monkeypatch.setattr(G, "PRACTICAL_FLOOR", 0.15)
    again = T.run_round2(cfg, evaluate=_Mock(cfg, default).evaluate,
                         decide=_Mock(cfg, default).decide, log=lambda s: None)
    assert again["winner"]["clears_G1"] is True
    assert again["loo"]["clears_G5"] is False and again["loo"]["G5_vacuous"] is False
    assert "below +0.15" in again["loo"]["G5_note"]


def test_argmax_ties_within_the_band_go_to_the_closest_to_the_shipped_floors():
    # Mutant killed: `min(tied, key=lambda r: r["cell_id"])` — the §7.1 rule 2
    # tie-break ("the member closest to the shipped floors in relative L1
    # distance") was implemented and pinned by nothing, and it decides which
    # floors get called "the tuned setting".
    far = {"cell_id": "aaa_far", "D": {"mean": 0.100}, "log_ratios": {"carries": 0.80}}
    near = {"cell_id": "zzz_near", "D": {"mean": 0.0965}, "log_ratios": {"carries": 0.10}}
    assert T._argmax([far, near])["cell_id"] == "zzz_near"      # 0.0035 < TIE_BAND
    outside = {"cell_id": "zzz_near", "D": {"mean": 0.090}, "log_ratios": {"carries": 0.10}}
    assert T._argmax([far, outside])["cell_id"] == "aaa_far"    # 0.010 > TIE_BAND
    assert T._argmax([]) is None and T._argmax([{"cell_id": "x", "D": None}]) is None


def test_pool_stats_reports_the_nearest_rank_p10_of_the_decided_weeks(cfg, ro):
    # Mutant killed: `"p10": statistics.median(pools)` (or a floor-rank
    # percentile).  The pool distribution min/p10/median/max is a §6.4 mandatory
    # disclosure and A4/A7 are read beside it, and `pool_stats` was referenced
    # by no test at all.
    recs = [D.WeekRecord(season=2023, week=w, as_of="2023-10-17", strategy=R.PER_WEEK_STRATEGY,
                         k=3, status=D.WEEK_DECIDED, reason=None, decisions=(),
                         generator_rows=0, usage_rows=0, pool_size=p, excluded_injury=0,
                         excluded_qb1=0, excluded_ineligible=0, excluded_no_gsis=0,
                         excluded_position=0, r0_scrape_date="2023-10-13", r0_pages=(), log_lines=())
            for w, p in enumerate([60, 57, 59, 61, 66, 67, 90, 100, 120, 168], start=1)]
    st = T.pool_stats(recs)
    assert st["n"] == 10 and st["min"] == 57 and st["max"] == 168
    assert st["p10"] == 57                       # ceil(0.10 * 10) = 1st smallest
    assert st["median"] == pytest.approx(66.5)
    assert st["per_week"]["2023,5"] == 66
    assert T.pool_stats([])["p10"] is None
    # an UNDECIDED week takes its pool out of the distribution
    undecided = dataclasses.replace(recs[0], status=D.WEEK_EMPTY_POOL, decisions=())
    assert T.pool_stats([undecided] + recs[1:])["n"] == 9


def test_main_runs_round_one_end_to_end_and_records_the_fingerprint_block(cfg, monkeypatch,
                                                                        capsys):
    # Mutant killed: any break in main()'s NON-dry-run path, which no test
    # reached (both shipped main() tests passed --dry-run, which returns before
    # the database is opened) — the fingerprint block, the panel assertion, the
    # summary line and the exit code were all unexercised.
    # the Appendix A assertion is wired into main(): this synthetic panel is not
    # the frozen TRAIN panel, so the run ABORTS before cell 1
    rc = T.main(["--db", str(cfg.db), "--cache-dir", str(cfg.cache_dir), "--round", "1"])
    err = capsys.readouterr().err
    assert rc == 3 and "PanelFingerprintMismatch" in err and "re-versioned" in err
    assert not cfg.results_dir.exists()
    monkeypatch.setattr(T, "assert_panel_fingerprint", lambda *a, **k: None)
    real = T.run_round1
    monkeypatch.setattr(T, "run_round1", lambda config, **kw: real(
        config, cells=(G.DEFAULT_CELL, G.cell_by_id("carries=2")), log=lambda s: None, **kw))
    rc = T.main(["--db", str(cfg.db), "--cache-dir", str(cfg.cache_dir), "--round", "1",
                 "--jobs", "1"])
    out = capsys.readouterr().out
    assert rc == 0 and "settings evaluated: 2 / 80" in out
    assert "db fingerprint UNCHANGED across the search" in out
    body = json.loads((Path(cfg.cache_dir) / T.FINGERPRINT_FILE).read_text())
    assert body["unchanged"] is True and body["before"]["seasons"] == [2021, 2022, 2023]
    assert set(body["before"]["partitions"]) == set(T.FINGERPRINT_TABLES)
    assert body["before"]["fpecr_panel"]["2023"]["rows"] > 0
    rows = [json.loads(x) for x in
            (Path(cfg.cache_dir) / T.SUMMARY_FILE).read_text().splitlines()]
    assert [r["cell_id"] for r in rows] == [G.DEFAULT_CELL_ID, "carries=2"]
    assert len(cfg.grade_log.read_text().splitlines()) == 6      # 2 settings x 3 strategies
    # every stored row carries the block it was READ with (§11.5), and a second
    # invocation is refused while the first holds the lock
    stored = T.load_result(cfg, DEFAULT_KEY)
    assert stored["fingerprint"]["fpecr_panel"] == body["before"]["fpecr_panel"]
    with T.run_lock(cfg):
        with pytest.raises(T.SearchLocked):
            with T.run_lock(cfg):
                pass
        rc = T.main(["--db", str(cfg.db), "--cache-dir", str(cfg.cache_dir), "--round", "1"])
        assert rc == 2 and "another invocation holds" in capsys.readouterr().err
    with T.run_lock(cfg):                        # released on exit, so this works
        pass


def test_an_inert_level_is_never_a_round_two_candidate(cfg):
    # Mutant killed: dropping `not r.get("inert")` from pick_candidates (or
    # making A8 a FLAG).  §6.2: "an inert setting is neither ranked nor reported
    # as an improvement ... without this rule the tie-break becomes the real
    # optimizer", and the four emergence-scale cells are pre-registered EXPECTED
    # INERT, so the rule is guaranteed to fire during the search.
    def row(cell_id, axis, d, *, inert=False, eligible=True, p=0.01):
        return {"cell_id": cell_id, "axis": axis, "round": 1, "eligible": eligible,
                "argmax_eligible": eligible, "inert": inert, "level": cell_id.split("=")[1],
                "floors": dict(G.DEFAULT_CELL.floors), "log_ratios": {"carries": 0.1},
                "D": {"mean": d, "permutation": {"p": p}}}
    rows = {"a": row("carries=2", "carries", 0.40, inert=True),      # highest D, INERT
            "b": row("carries=5", "carries", 0.20),
            "c": row("targets=3", "targets", 0.30, inert=True)}      # the only targets level
    cands, skipped = T.pick_candidates(rows)
    assert cands["carries"]["cell_id"] == "carries=5"
    assert "targets" not in cands
    assert {s["axis"] for s in skipped} == set(G.ROUND2_AXIS_ORDER) - {"carries"}
    # ... and neither does a level whose permutation p does not clear
    rows["b"] = row("carries=5", "carries", 0.20, p=0.20)
    cands, skipped = T.pick_candidates(rows)
    assert "carries" not in cands and len(skipped) == len(G.ROUND2_AXIS_ORDER)


def test_round_two_stamps_the_fingerprint_it_read_not_the_default_rows_copy(round1_done):
    # Mutant killed: `fingerprint_block=default.get("fingerprint")` (the
    # pre-audit closure).  §11.5 says every row carries the block "as read on
    # the snapshot"; taking the stored DEFAULT row's copy means a round-2
    # invocation after a re-snapshot records a fingerprint it never read, so a
    # post-hoc audit of the rows can pass FALSELY.
    cfg, default = round1_done
    assert default["fingerprint"] is None                  # what the mutant would copy
    block = {"fpecr_panel": {"2021": {"rows": 1, "sha256_16": "aa"}}, "partitions": {},
             "taken_at": "now"}
    m = _Mock(cfg, default)
    report = T.run_round2(cfg, evaluate=m.evaluate, decide=m.decide, log=lambda s: None,
                          fingerprint_block=block)
    assert report["fingerprint"] == block
    on_disk = json.loads((cfg.cache_dir / T.ROUND2_FILE).read_text())
    assert on_disk["fingerprint"]["fpecr_panel"]["2021"]["sha256_16"] == "aa"
