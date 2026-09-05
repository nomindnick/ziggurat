"""Item 4.2b (B2) — the decision-freeze writer, its run log, and `ziggurat decisions`.

Offline throughout: the synthetic ``marginal_world`` projection/league universe
(Rule 5 — invented players, no league member or rival team ever enters a
committed file), a temp directory for every capture, and a stubbed git identity
so nothing here depends on the state of the working tree.

The tests that matter most pin the things a capture is WORTH nothing without:
that two runs over identical inputs write identical bytes, that a mutated byte is
REFUSED rather than repaired, that the run log never says ``ok`` over a directory
with no manifest, that two captures on one Tuesday are two directories, and that
a pre-Week-1 run — the state the first live Tuesday will NOT be in, and every
rehearsal before it IS — still captures the plan half and records the candidate
half as ABSENT with its reason instead of failing.
"""

import ast
import json
from pathlib import Path

import pytest

from ziggurat.core import waiver
from ziggurat.core.waiver import WaiverArtifacts, build_waiver_plan
from ziggurat.decisions import capture as C
from ziggurat.decisions import read as R
from ziggurat.decisions import store as S

SEASON = 2026
PULL = "2026-09-15"
WEEKS = range(3, 18)
TEAM = 10

#: a fixed identity so the manifest's varying parts are the test's choice, not
#: the clock's or the tree's.
GIT = {"commit": "0" * 40, "diff_sha256": "d" * 64, "diff_bytes": 12, "dirty": True,
       "error": None}

_ODD = {w: 20.0 for w in range(1, 18) if w % 2 == 1}


def _active_specs():
    """16 non-IR bodies on team 10 — a full, legal active roster."""
    return [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": TEAM},
        {"name": "Backup Passer", "pos": "QB", "team": "TEN", "pts": 8.0, "bye": 6, "on_team": TEAM},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": TEAM},
        {"name": "Second Runner", "pos": "RB", "team": "ATL", "pts": 5.0, "bye": 11, "on_team": TEAM},
        {"name": "Third Runner", "pos": "RB", "team": "BUF", "pts": 12.0, "bye": 7, "on_team": TEAM},
        {"name": "Depth Runner", "pos": "RB", "team": "CHI", "pts": 3.0, "bye": 9, "on_team": TEAM},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": TEAM},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": TEAM},
        {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 11.0, "bye": 10, "on_team": TEAM},
        {"name": "Fourth Catcher", "pos": "WR", "team": "HOU", "pts": 4.0, "bye": 12, "on_team": TEAM},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": TEAM},
        {"name": "Tight Two", "pos": "TE", "team": "JAX", "pts": 3.0, "bye": 5, "on_team": TEAM},
        {"name": "Kick Er", "pos": "K", "team": "KC", "pts": 8.0, "bye": 14, "on_team": TEAM},
        {"name": "Miami D/ST", "pos": "D/ST", "team": "MIA", "pts": 2.0, "bye": 5, "on_team": TEAM,
         "weeks": _ODD},
        {"name": "Fifth Catcher", "pos": "WR", "team": "NO", "pts": 6.0, "bye": 7, "on_team": TEAM},
        {"name": "Sixth Catcher", "pos": "WR", "team": "SEA", "pts": 7.0, "bye": 8, "on_team": TEAM},
    ]


_POOL_SPECS = [
    {"name": "Free Passer", "pos": "QB", "team": "NE", "pts": 12.0, "bye": 9},
    {"name": "Free Runner", "pos": "RB", "team": "NYG", "pts": 20.0, "bye": 7},
    {"name": "Waiver Catcher", "pos": "WR", "team": "NYJ", "pts": 19.0, "bye": 11,
     "status": "WAIVERS"},
    {"name": "Waiver Wideout", "pos": "WR", "team": "PIT", "pts": 16.0, "bye": 10,
     "status": "WAIVERS"},
    {"name": "Free Tight", "pos": "TE", "team": "LV", "pts": 9.0, "bye": 8},
]


def _ir_spec(injury):
    return {"name": "IR Guy", "pos": "WR", "team": "MIN", "pts": 14.0, "bye": 6,
            "on_team": TEAM, "slot": "IR", "injury": injury}


def _world(marginal_world, injury="OUT"):
    marginal_world(_active_specs() + [_ir_spec(injury)] + _POOL_SPECS, retrieved=PULL)


def _collect(db, **kwargs):
    """Run the real plan with a real collector — the only input a capture takes."""
    art = WaiverArtifacts()
    kwargs.setdefault("weeks", WEEKS)
    kwargs.setdefault("pool_limit", None)
    build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=TEAM,
                      collect=art, **kwargs)
    return art


def _freeze(db, art, root, **kwargs):
    kwargs.setdefault("git", GIT)
    return C.freeze_waiver_artifacts(db, art, trigger=S.TRIGGER_CLI,
                                     argv=["ziggurat", "waivers"], root=root, **kwargs)


def _manifest(result):
    return json.loads((Path(result.directory) / C.MANIFEST).read_text(encoding="utf-8"))


def _with_candidate_board(monkeypatch):
    """Stand a candidate board in front of the generator.

    The synthetic world holds no ``weekly_stats``, so the real generator raises
    ``NoCompletedWeek`` and every capture over it is (correctly) ``partial``. A
    test that wants the COMPLETE outcome has to supply the half that does not
    exist here — which is itself the item's shape: before Week 1 there is nothing
    to capture on that side, and this is the only way to rehearse the other case.
    """
    from ziggurat.core import candidates as CAND

    row = CAND.CandidateRow(
        player_key="00-000999", player="Studied Player", position="RB", team="ATL",
        gsis_id="00-000999", espn_id="2000", signal_kind=CAND.SIGNAL_USAGE,
        magnitude=1.5, week=2, prior_week=1, hypothesis=False,
        reasons=("carries up 5 on the week",),
    )
    board = CAND.CandidateBoard(rows=(row,), week=2, freshness=(), notes=(),
                                as_of=PULL, season=SEASON)
    monkeypatch.setattr(waiver, "build_candidates", lambda *a, **kw: board)
    return board


# ------------------------------------------------------------------ determinism


def test_two_captures_of_the_same_run_are_byte_identical(db, marginal_world, tmp_path):
    """The determinism digest. Two captures over the same artifacts must produce
    the same payload bytes — otherwise 'this archive is what the tool said' is
    unfalsifiable, and a later diff of two Tuesdays reports noise as change."""
    _world(marginal_world)
    art = _collect(db)
    a = _freeze(db, art, tmp_path / "a", capture_id="fixed-1")
    b = _freeze(db, art, tmp_path / "b", capture_id="fixed-2")

    assert a.payload_digest == b.payload_digest
    for name in C.PAYLOAD_FILES:
        assert (Path(a.directory) / name).read_bytes() == (Path(b.directory) / name).read_bytes(), name
    # ... and the digest is over CONTENT, not over the directory it landed in.
    assert _manifest(a)["files"] == _manifest(b)["files"]


def test_the_payload_digest_moves_when_a_payload_moves(db, marginal_world, tmp_path):
    """A digest that cannot notice a change is decoration."""
    _world(marginal_world)
    art = _collect(db)
    a = _freeze(db, art, tmp_path / "a", capture_id="fixed-1")
    files = dict(_manifest(a)["files"])
    files[C.PLAN_FILE] = {**files[C.PLAN_FILE], "sha256": "0" * 64}
    assert C.payload_digest(files) != a.payload_digest


# --------------------------------------------------------------- the manifest


def test_the_manifest_records_the_gate_the_clock_the_code_and_the_vintages(
        db, marginal_world, tmp_path):
    _world(marginal_world)
    art = _collect(db)
    result = _freeze(db, art, tmp_path, capture_id="cap-1")
    m = _manifest(result)

    assert m["capture_id"] == "cap-1"
    assert m["as_of"] == PULL and m["season"] == SEASON
    assert m["week"] == 3 and "plan.weeks[0]" in m["week_basis"]
    assert set(m["captured_at"]) == {"pt", "utc"}
    assert m["argv"] == ["ziggurat", "waivers"]
    assert m["trigger"] == S.TRIGGER_CLI
    # git rev-parse HEAD is NOT a code identity here: the timers run the working
    # tree, so the diff digest and the dirty flag are load-bearing.
    assert m["code"]["commit"] == GIT["commit"]
    assert m["code"]["diff_sha256"] == GIT["diff_sha256"] and m["code"]["dirty"] is True
    assert isinstance(m["schema_version"], int)
    # Which PULL priced the page, per source — the item-3.1b lesson: `as_of` is
    # the gate, not the data.
    assert set(m["vintages"]) == set(waiver.ARCHIVE_VINTAGE_TABLES)
    assert m["vintages"]["projections"] == PULL
    assert m["crosswalk_vintage"] == PULL
    # NO knowable_as_of FIELD anywhere: a freeze is a record of a computation, so
    # it has no honest knowledge time to claim. (The manifest SAYS so in prose,
    # which is why this walks the keys rather than grepping the bytes.)
    def _keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from _keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _keys(v)

    assert "knowable_as_of" not in set(_keys(m))
    assert m["not_a_decision_input"].startswith("This capture has no knowable_as_of")


def test_the_manifest_states_the_chain_inertness_claim_with_its_mechanism(
        db, marginal_world, tmp_path):
    _world(marginal_world)
    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    m = _manifest(result)
    assert m["enriched_chain_equals_projection_only"] is True
    mech = m["enriched_chain_mechanism"]
    assert "STRUCTURAL" in mech and "_claim_reasons" in mech
    # The claim must say it was NOT re-measured on this run — the archive's own
    # honesty rule (an assertion the run did not make must not read like one).
    assert "not re-measured at capture time" in mech


def test_the_usage_evidence_column_is_order_inert(db, marginal_world, monkeypatch):
    """The manifest's claim, exercised: injecting an opportunity-signal note for
    every free agent changes reason TEXT and nothing else — not the chain order,
    not a gain, not `chain_rank`. The day that stops being true, the manifest's
    field has to become False."""
    _world(marginal_world)
    plain = build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=TEAM,
                              weeks=WEEKS, pool_limit=None)

    real = waiver._candidate_notes_by_espn

    def _noisy(conn, **kwargs):
        notes, err, board = real(conn, **kwargs)
        rows = conn.execute(
            "SELECT DISTINCT espn_player_id FROM league_player_state "
            "WHERE on_team_id IS NULL"
        ).fetchall()
        return ({str(r[0]): ["opportunity signal [USAGE_BREAKOUT]: invented"]
                 for r in rows}, err, board)

    monkeypatch.setattr(waiver, "_candidate_notes_by_espn", _noisy)
    noisy = build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=TEAM,
                              weeks=WEEKS, pool_limit=None)

    def _skeleton(plan):
        return [(r.chain_rank, r.add, r.add_espn_id, r.drop, r.drop_espn_id,
                 round(r.gain, 9), round(r.gain_alone, 9))
                for r in plan.claims + plan.fcfs_grabs + plan.streaming]

    assert _skeleton(noisy) == _skeleton(plain)
    assert noisy.chain_gain == plain.chain_gain
    # and the notes DID land somewhere, or this test proves nothing
    assert any("invented" in t for r in noisy.claims + noisy.fcfs_grabs
               for t in r.reasons)


# ------------------------------------------------------------ what is captured


def test_the_plan_file_holds_both_gains_both_ids_and_every_reason_verbatim(
        db, marginal_world, tmp_path):
    _world(marginal_world)
    art = _collect(db)
    result = _freeze(db, art, tmp_path, capture_id="cap-1")
    rows = R.read_records(result.directory, C.PLAN_FILE)
    header = rows[0]
    assert header["record"] == "plan"
    assert header["chain_stop"] and header["notes"]

    claims = [r for r in rows if r["record"] in ("claim", "grab")]
    assert claims, "the synthetic world must produce a chain, or this pins nothing"
    live = list(art.plan.claims) + list(art.plan.fcfs_grabs)
    for rec, rec_live in zip(sorted(claims, key=lambda r: r["chain_rank"]),
                             sorted(live, key=lambda c: c.chain_rank), strict=True):
        assert rec["chain_rank"] == rec_live.chain_rank
        assert rec["gain"] == rec_live.gain          # CONDITIONAL (item 3.4b)
        assert rec["gain_alone"] == rec_live.gain_alone   # and the standalone one
        assert rec["add_espn_id"] == rec_live.add_espn_id
        assert rec["drop_espn_id"] == rec_live.drop_espn_id
        assert rec["reasons"] == list(rec_live.reasons)   # verbatim (Rule 6)
    # the drop board and every disclosed chain bucket ride along too
    kinds = {r["record"] for r in rows}
    assert "drop_board" in kinds


def test_every_evaluated_candidate_row_lands_and_the_floors_travel_with_them(
        db, marginal_world, tmp_path, monkeypatch):
    """The candidate half: EVERY evaluated row, flagged or not (a false negative
    must be studyable), plus the floors that judged them (Rule 6)."""
    from ziggurat.core import candidates as CAND

    _world(marginal_world)
    art = _collect(db)

    # The synthetic world has no weekly_stats, so the arm evaluates nobody; stand
    # in a collector with rows so the WRITER's contract is what is under test.
    rows = [
        CAND.EvaluatedRow(
            season=SEASON, week=2, prior_week=1, as_of=PULL, view="historical",
            gsis_id=f"00-0000{i}", espn_id=str(9000 + i), player=f"Studied Player {i}",
            position="RB", team="ATL",
            deltas={"d_carries": 5.0 + i, "d_targets": None},
            levels={"carries": 12.0, "targets": 3.0},
            snap_pct=0.5, snap_resolved=True, path=CAND.PATH_DIFFERENCED,
            floors_cleared={"carries": 5.0 + i} if i == 0 else {},
            magnitude=1.5 if i == 0 else 0.0, flagged=(i == 0),
            reasons=("carries up 5 on the week",) if i == 0 else (),
        )
        for i in range(3)
    ]
    art.evaluated = CAND.EvaluatedRows(
        ran=True, rows=rows, season=SEASON, week=2, as_of=PULL, view="historical",
        positions=("RB", "WR", "TE"), floors=dict(CAND.DEFAULT_BREAKOUT.floors),
        floors_label=CAND.DEFAULT_BREAKOUT.label, floors_source="shipped",
        emergence_floors=dict(CAND.EMERGENCE_FLOORS), emergence_label="shipped",
    )
    result = _freeze(db, art, tmp_path, capture_id="cap-1")

    stored = R.read_records(result.directory, C.CANDIDATES_FILE)
    assert len(stored) == 3
    assert sum(1 for r in stored if r["flagged"]) == 1
    assert sum(1 for r in stored if not r["flagged"]) == 2, "false negatives must survive"
    # UNKNOWN stays UNKNOWN: a missing delta is never coerced to 0.0.
    assert stored[0]["d_targets"] is None
    # Rule 2: nothing in an evaluated row is a points column.
    assert not any("points" in k for r in stored for k in r)

    meta = _manifest(result)["candidates"]
    assert meta["rows"] == 3 and meta["flagged"] == 1 and meta["ran"] is True
    assert meta["floors"] == dict(CAND.DEFAULT_BREAKOUT.floors)
    assert meta["floors_source"] == "shipped" and meta["floors_label"]


def test_the_pool_records_what_the_scan_did_and_says_what_it_cannot_know(
        db, marginal_world, tmp_path):
    _world(marginal_world)
    art = _collect(db)
    result = _freeze(db, art, tmp_path, capture_id="cap-1")
    pool = R.read_records(result.directory, C.POOL_FILE)

    assert len(pool) == len(_POOL_SPECS)
    assert all(r["espn_id"] for r in pool)
    assert any(r["scanned"] for r in pool)
    assert any(r["priced_in_matrix"] for r in pool), "some FA must beat some drop"
    # The two things this writer cannot know without a second scan, DISCLOSED
    # rather than guessed (a second scan is a second answer).
    note = _manifest(result)["disclosures"][0]
    assert "does NOT re-scan" in note and "pruned by pool_limit" in note


def test_the_roster_file_holds_the_raw_roster_and_the_legality_verdict(
        db, marginal_world, tmp_path):
    _world(marginal_world)
    art = _collect(db)
    result = _freeze(db, art, tmp_path, capture_id="cap-1")
    roster = R.read_records(result.directory, C.ROSTER_FILE)[0]

    assert len(roster["roster"]) == 17, "RAW: the IR occupant is a roster row too"
    assert roster["legality"]["legal"] is True
    assert roster["legality"]["ir_count"] == 1
    assert roster["team_id"] == TEAM and roster["view"] == "historical"


def test_the_market_file_carries_the_projection_input_hash(db, marginal_world, tmp_path):
    """The item's own words: 'the printed claim chain and its projection-input
    hash'. Two captures that priced the same numbers hash the same, whatever
    their as_of says."""
    _world(marginal_world)
    a = _freeze(db, _collect(db), tmp_path / "a", capture_id="cap-1")
    b = _freeze(db, _collect(db), tmp_path / "b", capture_id="cap-2")
    ma = R.read_records(a.directory, C.MARKET_FILE)[0]
    mb = R.read_records(b.directory, C.MARKET_FILE)[0]

    assert ma["projection_input"]["sha256"] == mb["projection_input"]["sha256"]
    assert ma["projection_input"]["entries"] > 16
    # The same-week market surfaces. NOT a disjunction any more (item 4.2b audit,
    # T5): `or ... == "absent"` was satisfied on the empty path, so a renamed
    # column would have been swallowed into `status: error` and the market half of
    # every capture would have gone quietly missing on the first live Tuesday.
    from ziggurat.data.nfl.fp_weekly import LEAGUE_PAGES

    weekly = ma["fp_weekly_ecr"]
    assert {p["page"] for p in weekly["pages"]} == set(LEAGUE_PAGES)
    assert weekly["board"] == []               # nothing scraped in a synthetic world
    assert ma["fpecr_panel_wp"]["rows"] == 0   # no wp rows in a synthetic world


def test_a_market_table_that_does_not_exist_is_absent_with_a_reason(
        db, marginal_world, tmp_path, monkeypatch):
    """An ABSENT field and an omitted one are different facts: "we did not capture
    it" and "the table did not exist on the day" must never look alike in an
    archive read months later."""
    _world(marginal_world)
    monkeypatch.setattr(C, "_table_exists", lambda conn, table: False)
    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    market = R.read_records(result.directory, C.MARKET_FILE)[0]
    assert market["fp_weekly_ecr"]["status"] == "absent"
    assert "B5" in market["fp_weekly_ecr"]["reason"]
    assert market["fpecr_panel_wp"]["status"] == "absent"


# ----------------------------------------------------- publish-then-record


def test_no_ok_row_lands_without_a_manifest(db, marginal_world, tmp_path, monkeypatch):
    """PUBLISH-THEN-RECORD. Kill the writer just before the manifest: the run log
    must hold a legible `failed` row and NOTHING that claims a capture."""
    _world(marginal_world)
    art = _collect(db)
    real_write = C._write_atomic

    def _die(path, data):
        if path.name == C.MANIFEST:
            raise OSError("disk full, right before the manifest")
        return real_write(path, data)

    monkeypatch.setattr(C, "_write_atomic", _die)
    with pytest.raises(OSError):
        _freeze(db, art, tmp_path, capture_id="cap-1")

    row = S.capture_by_id(db, "cap-1")
    assert row["status"] == S.STATUS_FAILED
    assert "disk full" in row["error"]
    assert row["manifest_sha256"] is None and row["payload_digest"] is None
    assert S.last_capture(db, status=S.STATUS_OK) is None
    # and the half-written directory is not a capture: verify refuses it.
    assert R.verify(row["artifact_dir"]).ok is False


def test_a_running_row_exists_before_the_work_and_becomes_ok_after(
        db, marginal_world, tmp_path, monkeypatch):
    """START-BEFORE-WORK is the run-log discipline, not the item-3.6 defect: the
    row suppresses nothing (a later capture is a new row with a new id), it only
    makes a killed process a positive fact instead of silence."""
    _world(marginal_world)
    _with_candidate_board(monkeypatch)          # so the capture can reach `ok`
    art = _collect(db)
    seen = {}
    real_write = C._write_capture

    def _peek(conn, *a, **kw):
        seen["row"] = dict(S.capture_by_id(conn, kw["capture_id"]))
        return real_write(conn, *a, **kw)

    monkeypatch.setattr(C, "_write_capture", _peek)
    result = _freeze(db, art, tmp_path, capture_id="cap-1")

    assert seen["row"]["status"] == S.STATUS_RUNNING
    row = S.capture_by_id(db, "cap-1")
    assert row["status"] == S.STATUS_OK == result.status
    assert row["manifest_sha256"] == result.manifest_sha256
    assert row["payload_digest"] == result.payload_digest
    assert row["claims"] == len(art.plan.claims)
    assert row["chain_gain"] == art.plan.chain_gain


def test_a_capture_never_takes_the_page_down(db, marginal_world, tmp_path, monkeypatch):
    """`capture_best_effort` is what `ziggurat waivers` wires: nothing on the
    archive side may cost the operator the plan."""
    _world(marginal_world)
    art = _collect(db)
    monkeypatch.setattr(C, "_write_capture",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    result = C.capture_best_effort(db, art, trigger=S.TRIGGER_WAIVERS,
                                   root=tmp_path, capture_id="cap-1", git=GIT)
    assert result.status == S.STATUS_FAILED and result.captured is False
    assert "boom" in result.error
    assert "NOT archived" in result.line
    assert S.capture_by_id(db, "cap-1")["status"] == S.STATUS_FAILED


def test_an_orphaned_running_row_is_reaped_not_left_silent(db):
    S.start_capture(db, capture_id="dead", season=SEASON, week=3, trigger="timer",
                    plan_as_of=PULL, started_at="2026-09-15T18:30:00")
    assert S.reap_orphans(db, now="2026-09-15T20:30:00") == 1
    assert S.capture_by_id(db, "dead")["status"] == S.STATUS_ABANDONED


# ------------------------------------------------------------- no collisions


def test_two_captures_on_one_day_are_two_directories(db, marginal_world, tmp_path):
    """The operator runs `waivers` more than once on a Tuesday. Two captures are
    two FACTS — never an idempotent overwrite, so the first one's bytes survive."""
    _world(marginal_world)
    art = _collect(db)
    a = _freeze(db, art, tmp_path, capture_id="cap-1")
    before = (Path(a.directory) / C.PLAN_FILE).read_bytes()
    b = _freeze(db, art, tmp_path, capture_id="cap-2")

    assert a.directory != b.directory
    assert Path(a.directory).is_dir() and Path(b.directory).is_dir()
    assert (Path(a.directory) / C.PLAN_FILE).read_bytes() == before
    assert R.verify(a.directory).ok and R.verify(b.directory).ok
    assert len(S.recent_captures(db)) == 2


def test_an_existing_capture_directory_is_refused_never_reused(
        db, marginal_world, tmp_path):
    _world(marginal_world)
    art = _collect(db)
    _freeze(db, art, tmp_path, capture_id="cap-1")
    with pytest.raises(C.CaptureCollision):
        _freeze(db, art, tmp_path, capture_id="cap-1")
    # the refused attempt is a recorded FAILURE, not silence... and it did not
    # touch the capture that was already there.
    assert R.verify(C.capture_dir(tmp_path, season=SEASON, week=3,
                                  capture_id="cap-1")).ok


def test_minted_ids_do_not_collide_within_one_second():
    from datetime import datetime
    now = datetime(2026, 9, 15, 18, 30, 0)
    ids = {C.new_capture_id(now) for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("20260915T183000-") for i in ids)


# ------------------------------------------------------------------- verify


def test_a_mutated_byte_is_refused(db, marginal_world, tmp_path):
    _world(marginal_world)
    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    assert R.verify(result.directory).ok

    target = Path(result.directory) / C.PLAN_FILE
    blob = bytearray(target.read_bytes())
    blob[0] = blob[0] ^ 0x20            # one byte, one bit-flip
    target.write_bytes(bytes(blob))

    report = R.verify(result.directory)
    assert report.ok is False
    assert any(C.PLAN_FILE in p and "sha256" in p for p in report.problems)
    assert "NOT TRUSTWORTHY" in report.render()
    with pytest.raises(R.CaptureUnreadable):
        R.read_records(result.directory, C.PLAN_FILE)


def test_an_edited_manifest_is_caught_by_the_run_log_digest(
        db, marginal_world, tmp_path):
    """The hole a files-vs-manifest check leaves open: an edit that changes a
    payload file AND its manifest entry. The digest recorded when the capture
    landed is what closes it."""
    _world(marginal_world)
    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    directory = Path(result.directory)

    blob = bytearray((directory / C.PLAN_FILE).read_bytes())
    blob[0] = blob[0] ^ 0x20
    (directory / C.PLAN_FILE).write_bytes(bytes(blob))
    manifest = json.loads((directory / C.MANIFEST).read_text(encoding="utf-8"))
    import hashlib
    manifest["files"][C.PLAN_FILE]["sha256"] = hashlib.sha256(bytes(blob)).hexdigest()
    (directory / C.MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assert R.verify(directory).ok is True, "files-vs-manifest alone cannot see this"
    row = S.capture_by_id(db, "cap-1")
    report = R.verify(directory, expected_manifest_sha256=row["manifest_sha256"])
    assert report.ok is False
    assert any("manifest itself was edited" in p for p in report.problems)


def test_an_unmanifested_directory_is_not_a_capture(tmp_path):
    (tmp_path / "half").mkdir()
    (tmp_path / "half" / C.PLAN_FILE).write_text("{}\n", encoding="utf-8")
    report = R.verify(tmp_path / "half")
    assert report.ok is False and "incomplete capture" in report.problems[0]
    with pytest.raises(R.CaptureUnreadable):
        R.load_manifest(tmp_path / "half")


def test_a_file_the_manifest_does_not_name_is_reported(db, marginal_world, tmp_path):
    _world(marginal_world)
    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    (Path(result.directory) / "notes.txt").write_text("added later", encoding="utf-8")
    report = R.verify(result.directory)
    assert report.ok is False
    assert any("notes.txt" in p for p in report.problems)


# ------------------------------------------------ the pre-Week-1 / degrade path


def test_a_capture_with_no_candidate_board_is_partial_with_its_reason_not_a_failure(
        db, marginal_world, tmp_path, monkeypatch):
    """The state every rehearsal before 2026-09-15 is in: `build_candidates`
    raises NoCompletedWeek until a REG week is fully played. The plan half of
    that Tuesday is still the only record of it there will ever be."""
    from ziggurat.core.candidates import NoCompletedWeek

    _world(marginal_world)
    monkeypatch.setattr(
        waiver, "build_candidates",
        lambda *a, **kw: (_ for _ in ()).throw(NoCompletedWeek("no REG week is fully played")),
    )
    art = _collect(db)
    result = _freeze(db, art, tmp_path, capture_id="cap-1")

    assert result.status == S.STATUS_PARTIAL and result.captured is True
    assert "no completed week" in result.error.lower()
    meta = _manifest(result)["candidates"]
    assert meta["present"] is False and meta["ran"] is False and meta["rows"] == 0
    assert "NoCompletedWeek" in meta["absent_reason"]
    # the PLAN half is complete and verifiable
    assert R.verify(result.directory).ok
    assert R.read_records(result.directory, C.PLAN_FILE)
    assert S.capture_by_id(db, "cap-1")["status"] == S.STATUS_PARTIAL


def test_a_degraded_signal_load_is_recorded_as_a_degrade_not_as_an_empty_board(
        db, marginal_world, tmp_path, monkeypatch):
    _world(marginal_world)
    monkeypatch.setattr(
        waiver, "build_candidates",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("the wire is down")),
    )
    art = _collect(db)
    result = _freeze(db, art, tmp_path, capture_id="cap-1")
    meta = _manifest(result)["candidates"]
    assert result.status == S.STATUS_PARTIAL
    assert "degraded" in meta["absent_reason"] and "the wire is down" in meta["absent_reason"]


def test_a_blocked_roster_captures_the_refusal_and_never_prices_the_matrix(
        db, marginal_world, tmp_path):
    """An illegal roster refuses to plan claims (item 3.4's done-when). The
    capture records the refusal — and must NOT resolve the lazy swap matrix to do
    it: an archive of a refusal that costs more than the refusal is a tax on the
    one page the operator needs fastest."""
    _world(marginal_world, injury="QUESTIONABLE")   # the IR occupant resets out
    art = _collect(db)
    assert art.plan.blocked is True
    result = _freeze(db, art, tmp_path, capture_id="cap-1")

    assert art.board is not None and art.board._swaps._resolved is None
    assert result.status == S.STATUS_PARTIAL
    assert "blocked" in _manifest(result)["candidates"]["absent_reason"].lower()
    header = R.read_records(result.directory, C.PLAN_FILE)[0]
    assert header["blocked"] is True
    assert R.read_records(result.directory, C.SWAPS_FILE) == []
    assert S.capture_by_id(db, "cap-1")["blocked"] == 1


def test_an_empty_collector_is_refused_rather_than_archived(db, tmp_path):
    with pytest.raises(ValueError, match="nothing to freeze"):
        C.freeze_waiver_artifacts(db, WaiverArtifacts(), trigger="cli", root=tmp_path)


# ------------------------------------------------------------------ the status


def test_no_captures_recorded_is_not_healthy_empty(db):
    """Item 3.7 already paid for this once: `no push runs recorded yet` read as
    healthy on a box where the push layer had never been installed."""
    text = S.format_status(db)
    assert "NOT healthy-empty" in text
    assert "cannot be reconstructed" in text
    assert "install-decisions.sh" in text


def test_the_status_names_the_capture_the_week_and_the_chain(db, marginal_world, tmp_path):
    _world(marginal_world)
    art = _collect(db)
    _freeze(db, art, tmp_path, capture_id="cap-1")
    text = S.format_status(db)
    assert "cap-1" in text and "wk03" in text
    assert f"claims={len(art.plan.claims)}" in text
    assert "decisions verify" in text


def test_the_status_says_so_when_the_latest_attempt_is_not_a_capture(db):
    S.start_capture(db, capture_id="dead", season=SEASON, week=3, trigger="timer",
                    plan_as_of=PULL, started_at="2026-09-15T18:30:00")
    S.finish_capture(db, S.last_capture(db)["freeze_id"], status=S.STATUS_FAILED,
                     finished_at="2026-09-15T18:30:01", error="disk full")
    text = S.format_status(db)
    assert "is 'failed', not a capture" in text
    assert "that Tuesday is not archived" in text


# --------------------------------------------------------- boundaries & rules


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_the_archive_imports_neither_draft_nor_backtest():
    """Rule 8, and the one-way backtest arrow. The manifest / atomic-write /
    digest pattern is COPIED from backtest/decisions.py and the run-log pattern
    from push/runs.py — copied, never imported, exactly as push/run.py copies the
    draft cockpit's fsync journal. A STATIC scan, so a lazy in-body import (the
    shape Rule 8 has to catch) cannot slip past."""
    pkg = Path(__file__).resolve().parents[1] / "ziggurat" / "decisions"
    files = sorted(pkg.glob("*.py"))
    assert files, "the decisions package must exist"
    for path in files:
        for name in _imports(path):
            assert not name.startswith("ziggurat.draft"), f"{path.name} imports {name}"
            assert not name.startswith("backtest"), f"{path.name} imports {name}"
    # and the copy SAYS it is a copy (the disclosure `push/run.py` already makes)
    text = (pkg / "capture.py").read_text(encoding="utf-8")
    assert "COPIED from backtest/decisions.py" in text


def test_the_run_log_carries_no_knowledge_time_columns(db):
    """Operational metadata: no as-of columns, never read through select_as_of
    (the league_sync_runs / nfl_ingest_runs / push_runs precedent). `plan_as_of`
    is the run PARAMETER — the gate the plan ran at — and nothing gates on it."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(decision_freezes)")}
    assert "knowable_as_of" not in cols and "retrieved_as_of" not in cols
    assert "plan_as_of" in cols

    pkg = Path(__file__).resolve().parents[1] / "ziggurat" / "decisions"
    for path in pkg.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # a CALL, not the word: the docstrings name the rule they are obeying.
        assert "select_as_of(" not in text, f"{path.name} calls select_as_of"
    assert "never read through ``select_as_of``" in (pkg / "store.py").read_text(
        encoding="utf-8")


def test_captures_land_where_the_public_repo_boundary_can_see_them():
    """Rule 5. A freeze holds the whole free-agent pool, rival rosters and this
    league's private state; it must live somewhere BOTH .gitignore and
    repo_guard already refuse — never under templates/, tests/ or a fixture."""
    from ziggurat.repo_guard import violations

    # The SHIPPED location, read from the source rather than from the module
    # attribute (the suite redirects that attribute so no test can write into the
    # operator's real archive — see the conftest fixture).
    source = Path(C.__file__).read_text(encoding="utf-8")
    assert 'DECISIONS_DIR = REPO_ROOT / "data" / "decisions"' in source
    assert violations(["data/decisions/2026/wk03/cap-1/plan.jsonl"])
    assert violations(["data/decisions/2026/wk03/cap-1/manifest.json"])


def test_the_writer_refuses_a_value_it_has_no_rendering_for():
    """Refuse-rather-than-guess: an unknown type must not be silently
    stringified into an archive nobody re-reads for months."""
    class Odd:
        pass

    with pytest.raises(TypeError, match="no JSON rendering"):
        C._dumps({"x": Odd()})
    with pytest.raises(ValueError, match="non-finite"):
        C._dumps({"x": float("nan")})


def test_git_identity_records_the_working_tree_not_just_the_commit():
    """`git rev-parse HEAD` is not a code identity here: the timers run the
    WORKING TREE, so an uncommitted edit is the production cadence."""
    info = C.git_identity()
    assert set(info) >= {"commit", "diff_sha256", "dirty", "error"}
    if info["error"] is None:
        assert info["commit"] and len(info["commit"]) == 40
        assert isinstance(info["dirty"], bool)
        if info["dirty"]:
            assert info["diff_sha256"] and len(info["diff_sha256"]) == 64


# ------------------------------------------------------------ the CLI surface


def _file_db(db, tmp_path, name="cli.sqlite"):
    """The in-memory synthetic world, copied to a file the CLI can open by path."""
    from ziggurat.data.store import connect

    path = tmp_path / name
    dest = connect(path)
    db.backup(dest)
    dest.commit()
    dest.close()
    return path


def test_waivers_archives_every_run_and_says_where(db, marginal_world, tmp_path,
                                                   monkeypatch):
    """The always-on capture (operator decision D1): the archived page is the one
    that actually produced the decision, and it costs ~0 s because the plan is
    already in memory when the writer fires."""
    from typer.testing import CliRunner

    from ziggurat.cli.main import app
    from ziggurat.data.store import connect

    _world(marginal_world)
    path = _file_db(db, tmp_path)
    monkeypatch.setattr(C, "DECISIONS_DIR", tmp_path / "archive")

    result = CliRunner().invoke(app, ["waivers", "--path", str(path), "--as-of", PULL,
                                      "--season", str(SEASON), "--team", str(TEAM),
                                      "--from-week", "3"])
    assert result.exit_code == 0, result.output
    assert "WAIVER CLAIMS" in result.output          # the page is unchanged...
    assert "decision freeze [" in result.output      # ... and one line says where

    captured = sorted((tmp_path / "archive").glob("*/*/*/manifest.json"))
    assert len(captured) == 1
    assert R.verify(captured[0].parent).ok

    conn = connect(path)
    row = S.last_capture(conn)
    assert row["trigger"] == S.TRIGGER_WAIVERS
    assert row["status"] in S.CAPTURED_STATUSES
    conn.close()


def test_no_freeze_skips_the_archive_entirely(db, marginal_world, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from ziggurat.cli.main import app
    from ziggurat.data.store import connect

    _world(marginal_world)
    path = _file_db(db, tmp_path)
    monkeypatch.setattr(C, "DECISIONS_DIR", tmp_path / "archive")

    result = CliRunner().invoke(app, ["waivers", "--path", str(path), "--as-of", PULL,
                                      "--season", str(SEASON), "--team", str(TEAM),
                                      "--from-week", "3", "--no-freeze"])
    assert result.exit_code == 0, result.output
    assert "decision freeze" not in result.output
    assert not (tmp_path / "archive").exists()
    conn = connect(path)
    assert S.last_capture(conn) is None
    conn.close()


def test_decisions_status_and_verify_run_end_to_end(db, marginal_world, tmp_path,
                                                    monkeypatch):
    from typer.testing import CliRunner

    from ziggurat.cli.main import app

    _world(marginal_world)
    path = _file_db(db, tmp_path)
    monkeypatch.setattr(C, "DECISIONS_DIR", tmp_path / "archive")
    runner = CliRunner()

    empty = runner.invoke(app, ["decisions", "status", "--path", str(path)])
    assert empty.exit_code == 0
    assert "NOT healthy-empty" in empty.output

    made = runner.invoke(app, ["decisions", "freeze", "--path", str(path),
                               "--as-of", PULL, "--season", str(SEASON),
                               "--team", str(TEAM), "--from-week", "3"])
    assert made.exit_code == 0, made.output
    assert "decision freeze [" in made.output

    listed = runner.invoke(app, ["decisions", "status", "--path", str(path)])
    assert "NOT healthy-empty" not in listed.output
    assert "wk03" in listed.output

    ok = runner.invoke(app, ["decisions", "verify", "--path", str(path)])
    assert ok.exit_code == 0 and "VERIFIED" in ok.output

    # a directory can be verified WITHOUT the run log — a capture copied to
    # another box is still checkable against its own manifest.
    directory = next((tmp_path / "archive").glob("*/*/*/manifest.json")).parent
    by_dir = runner.invoke(app, ["decisions", "verify", "--dir", str(directory)])
    assert by_dir.exit_code == 0 and "VERIFIED" in by_dir.output

    missing = runner.invoke(app, ["decisions", "verify", "--path", str(path),
                                  "--capture", "never-happened"])
    assert missing.exit_code == 1 and "no capture recorded" in missing.output

    # one byte moves -> the command REFUSES, and exits non-zero
    manifest = next((tmp_path / "archive").glob("*/*/*/manifest.json"))
    target = manifest.parent / C.PLAN_FILE
    blob = bytearray(target.read_bytes())
    blob[0] = blob[0] ^ 0x20
    target.write_bytes(bytes(blob))
    bad = runner.invoke(app, ["decisions", "verify", "--path", str(path)])
    assert bad.exit_code == 1 and "REFUSED" in bad.output


def test_the_suite_redirects_captures_away_from_the_operators_archive():
    """The conftest autouse redirect, pinned. Without it `ziggurat waivers` in any
    test drops a synthetic Tuesday into the real archive, where item 4.2b's
    classification would later read it as a decision that was actually made."""
    from ziggurat.paths import REPO_ROOT

    assert C.DECISIONS_DIR != REPO_ROOT / "data" / "decisions"


def test_a_broken_provenance_probe_never_costs_the_tuesday(db, marginal_world,
                                                           tmp_path, monkeypatch):
    """market.json is metadata ABOUT the run, not the run. A probe that breaks —
    a market table a later item renames a column on, a reader reaching into board
    internals that moved — is recorded as an error INSIDE the file, and the
    capture still lands."""
    _world(marginal_world)
    art = _collect(db)
    monkeypatch.setattr(
        C, "_fp_weekly_probe",
        lambda conn, as_of, view: (_ for _ in ()).throw(
        RuntimeError("no such column: page")))
    result = _freeze(db, art, tmp_path, capture_id="cap-1")

    assert result.captured is True
    market = R.read_records(result.directory, C.MARKET_FILE)[0]
    assert market["fp_weekly_ecr"]["status"] == "error"
    assert "no such column" in market["fp_weekly_ecr"]["reason"]
    # ... and the rest of the file is intact
    assert market["projection_input"]["sha256"]
    assert R.verify(result.directory).ok


# ================================================ item 4.2b audit-fix round

# The fixes below each replace a mechanism that shipped with no test, or a
# sentence the module asserted without measuring. Every one names its finding.


def _fp_weekly_row(fp_id, page, scrape, retrieved, rank):
    return (str(fp_id), page, scrape, 2026, 2, "schedules", f"Player {fp_id}", "RB",
            "ATL", None, None, rank, float(rank), 1.0, 1, 9, "RB1", 50.0, 0.0,
            "@BUF", None, "A", 12.0, None, None, None, retrieved, scrape)


def _seed_fp_weekly(db, rows):
    db.executemany(
        "INSERT OR REPLACE INTO fp_weekly_ecr VALUES ("
        + ",".join("?" * 28) + ")", rows)
    db.commit()


def _seed_fpecr(db, rows):
    db.executemany(
        "INSERT OR REPLACE INTO fpecr_panel VALUES (" + ",".join("?" * 22) + ")", rows)
    db.commit()


def _fpecr_row(fp_id, scrape, retrieved, season=2026):
    return (str(fp_id), "wp", "ppr-rb", scrape, season, 2, "schedules",
            f"Player {fp_id}", "RB", "ATL", None, None, 5.0, 1.0, 1, 9, 50.0, 40.0,
            1, 1, retrieved, scrape)


def test_the_market_probes_count_only_what_the_run_could_read(db, marginal_world,
                                                              tmp_path):
    """R1. `market.json` prints `view` beside these numbers, so they must be the
    numbers THAT view can serve.

    Two defects in one shape: no retrieval gate (so a past `--as-of` reported rows
    the run provably could not read — measured on the live database at 3,516
    fpecr rows against zero servable), and no per-key resolution (so a board
    pulled on three days counted three times). Both are fixed by going through
    the source's own accessor; this seeds exactly those two situations."""
    _world(marginal_world)
    # ONE five-row board, captured on three days. Under `historical` at PULL the
    # newest visible vintage is the one that resolves — never the sum.
    _seed_fp_weekly(db, [
        _fp_weekly_row(i, "ppr-rb", "2026-09-14", retrieved, i)
        for retrieved in ("2026-09-14", "2026-09-15", "2026-09-16")
        for i in range(1, 6)
    ])
    # ... plus a second page, and a row RETRIEVED AFTER the capture's as_of.
    _seed_fp_weekly(db, [_fp_weekly_row(20 + i, "ppr-wr", "2026-09-14",
                                        "2026-09-15", i) for i in range(1, 4)])
    _seed_fp_weekly(db, [_fp_weekly_row(90, "ppr-te", "2026-09-16", "2026-09-16", 1)])

    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    market = R.read_records(result.directory, C.MARKET_FILE)[0]
    weekly = market["fp_weekly_ecr"]
    by_page = {p["page"]: p for p in weekly["pages"]}

    assert by_page["ppr-rb"]["rows"] == 5, "three vintages of one board are one board"
    assert by_page["ppr-wr"]["rows"] == 3
    # knowable_as_of = scrape_date 2026-09-16 is after PULL, and so is its
    # retrieved_as_of: neither gate may let it through.
    assert by_page["ppr-te"]["rows"] == 0
    assert weekly["view"] == "historical"
    # DC-3: the ROWS themselves, not only their cardinality.
    assert len(weekly["board"]) == 8
    assert {b["page"] for b in weekly["board"]} == {"ppr-rb", "ppr-wr"}
    assert all({"rank", "ecr", "sd", "fantasypros_id", "week_basis"} <= set(b)
               for b in weekly["board"])
    for page, entry in by_page.items():
        assert entry["rows"] == sum(1 for b in weekly["board"] if b["page"] == page)


def test_the_fpecr_probe_honours_the_view_and_resolves_per_key(db, marginal_world,
                                                               tmp_path):
    """R1, the panel half. The bulk-history footgun in one test: rows retrieved
    AFTER the capture's as_of are invisible under `historical`, and re-mirroring
    the same scrape does not double the count."""
    _world(marginal_world)
    _seed_fpecr(db, [_fpecr_row(i, "2026-09-13", retrieved)
                     for retrieved in ("2026-09-13", "2026-09-14")
                     for i in range(1, 5)])
    _seed_fpecr(db, [_fpecr_row(50, "2026-09-13", "2026-09-20")])   # retrieved later

    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    panel = R.read_records(result.directory, C.MARKET_FILE)[0]["fpecr_panel_wp"]
    assert panel["rows"] == 4, "two mirrors of one scrape are one scrape"
    assert panel["first_scrape"] == panel["last_scrape"] == "2026-09-13"
    assert panel["view"] == "historical"


def test_the_board_itself_is_archived_not_only_its_count(db, marginal_world,
                                                         tmp_path, monkeypatch):
    """DC-1. Without `board.jsonl` the INJURY_SHOCK and QB1_CHANGE arms leave NO
    trace in a freeze: `candidates.jsonl` is the usage arm's evaluated rows only,
    and the board survived as an integer. Their reason text and their
    `player_key` — the episode key — exist nowhere else once the process exits."""
    from ziggurat.core import candidates as CAND

    _world(marginal_world)
    injury = CAND.CandidateRow(
        player_key="espn:4242", player="Hurt Starter", position="WR", team="MIN",
        gsis_id=None, espn_id="4242", signal_kind=CAND.SIGNAL_INJURY,
        magnitude=3.0, week=2, prior_week=None, hypothesis=False,
        reasons=("ruled OUT — the snaps have to go somewhere",),
        episode_tag="REPEAT (also wk 1)",
    )
    qb1 = CAND.CandidateRow(
        player_key="00-000777", player="New Starter", position="QB", team="CHI",
        gsis_id="00-000777", espn_id="777", signal_kind=CAND.SIGNAL_QB1,
        magnitude=1.0, week=2, prior_week=1, hypothesis=True,
        reasons=("depth chart QB1 changed",), episode_tag="NEW",
    )
    board = CAND.CandidateBoard(rows=(injury, qb1), week=2, freshness=("a note",),
                                notes=("badges unavailable",), as_of=PULL,
                                season=SEASON, badged=False)
    monkeypatch.setattr(waiver, "build_candidates", lambda *a, **kw: board)

    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    rows = R.read_records(result.directory, C.BOARD_FILE)
    header = rows[0]
    flagged = [r for r in rows if r["record"] == "flagged"]

    assert header["record"] == "board" and header["week"] == 2
    assert header["notes"] == ["badges unavailable"]
    assert len(flagged) == _manifest(result)["candidates"]["board_rows"] == 2
    shock = next(r for r in flagged if r["signal_kind"] == CAND.SIGNAL_INJURY)
    assert shock["player_key"] == "espn:4242"
    assert shock["reasons"] == list(injury.reasons)      # verbatim (Rule 6)
    assert shock["episode_tag"] == "REPEAT (also wk 1)"
    assert next(r for r in flagged
                if r["signal_kind"] == CAND.SIGNAL_QB1)["hypothesis"] is True
    assert C.BOARD_FILE in _manifest(result)["files"]


def test_the_manifest_names_both_weeks_because_they_differ(db, marginal_world,
                                                           tmp_path, monkeypatch):
    """DC-7. A capture is filed under the week the board PRICED; its candidate
    rows describe the last week fully PLAYED. On an in-season Tuesday those
    differ by one, so a reader joining on the directory name is off by one."""
    _with_candidate_board(monkeypatch)
    _world(marginal_world)
    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    m = _manifest(result)
    assert m["week"] == 3                     # the priced week (the directory)
    assert m["candidate_week"] == 2           # the week the rows describe
    assert "week - 1" in m["candidate_week_basis"]
    assert "THE WEEK THE BOARD PRICED" in C.capture_dir.__doc__


def test_the_trigger_vocabulary_is_written_down_where_it_is_written(db):
    """DC-11. A third value (`waivers`) was invented and is what the DOMINANT
    path writes; §2.1 named two. Useful, but it has to be recorded, or a reader
    filtering on the decided vocabulary sees almost nothing."""
    assert {S.TRIGGER_WAIVERS, S.TRIGGER_CLI, S.TRIGGER_TIMER} == {
        "waivers", "cli", "timer"}
    doc = C._write_capture.__doc__
    assert "THREE-VALUE" in doc
    for value in ("waivers", "cli", "timer"):
        assert f"``{value}``" in doc or value in doc


def test_the_capture_id_tail_is_wide_enough_for_the_claim_it_makes():
    """T4. The docstring says the random tail is what makes two captures in the
    same SECOND two directories. At three bytes that is 0.12% wrong over 200
    draws — measured at 0.13% over 20,000 repetitions, which made the 200-draw
    pin flaky at ~1 run in 800."""
    import re as _re

    ids = {C.new_capture_id(__import__("datetime").datetime(2026, 9, 15, 18, 30))
           for _ in range(200)}
    assert len(ids) == 200
    tail = next(iter(ids)).split("-")[1]
    assert _re.fullmatch(r"[0-9a-f]{8}", tail), tail


def test_two_runs_not_only_two_writes_agree(db, marginal_world, tmp_path):
    """T6. The determinism pin above freezes ONE `WaiverArtifacts` twice, so it
    covers the WRITER. Its stated motivation — 'a later diff of two Tuesdays
    reports noise as change' — depends on the RUN being deterministic too."""
    _world(marginal_world)
    digests = [_freeze(db, _collect(db), tmp_path / f"run{i}",
                       capture_id=f"run{i}").payload_digest for i in range(2)]
    assert digests[0] == digests[1]


def test_a_populated_candidates_file_is_byte_identical_across_captures(
        db, marginal_world, tmp_path):
    """T3. The byte-identity pin ran over an EMPTY collector — candidates.jsonl
    digested to sha256 of zero bytes — so the largest payload, the only one
    carrying floats and a 30-field row, was the one file it did not cover."""
    from ziggurat.core import candidates as CAND

    _world(marginal_world)
    art = _collect(db)
    rows = [
        CAND.EvaluatedRow(
            season=SEASON, week=2, prior_week=1, as_of=PULL, view="historical",
            gsis_id=f"00-0000{i}", espn_id=str(9000 + i), player=f"Studied {i}",
            position="RB", team="ATL",
            deltas={"d_carries": 5.0 + i / 3.0, "d_targets": None},
            levels={"carries": 12.0, "targets": 3.0},
            snap_pct=1.0 / 3.0, snap_resolved=True, path=CAND.PATH_DIFFERENCED,
            floors_cleared={"carries": 5.0 + i / 3.0} if i == 0 else {},
            magnitude=1.5 if i == 0 else 0.0, flagged=(i == 0),
            reasons=("carries up",) if i == 0 else (),
            id_alternates=("00-0009999",) if i == 1 else (),
        )
        for i in range(3)
    ]
    art.evaluated = CAND.EvaluatedRows(
        ran=True, rows=rows, season=SEASON, week=2, as_of=PULL, view="historical",
        positions=("RB",), floors=dict(CAND.DEFAULT_BREAKOUT.floors),
        floors_label=CAND.DEFAULT_BREAKOUT.label, floors_source="shipped",
        emergence_floors=dict(CAND.EMERGENCE_FLOORS), emergence_label="shipped",
    )
    a = _freeze(db, art, tmp_path / "a", capture_id="cap-a")
    b = _freeze(db, art, tmp_path / "b", capture_id="cap-b")
    blob = (Path(a.directory) / C.CANDIDATES_FILE).read_bytes()
    assert blob, "this pin is vacuous over an empty collector — that was the defect"
    assert blob == (Path(b.directory) / C.CANDIDATES_FILE).read_bytes()
    # DC-4: the collision field travels with the row.
    stored = R.read_records(a.directory, C.CANDIDATES_FILE)
    assert [r["id_alternates"] for r in stored] == [[], ["00-0009999"], []]


# ----------------------------------------------- the run log tells the truth


def test_a_failure_before_the_plan_still_leaves_a_row(db, marginal_world,
                                                      monkeypatch, tmp_path):
    """OPS-3. Every pre-plan failure used to exit before `start_capture`, so a
    Tuesday that failed showed NOTHING in `decisions status` while the unit file
    asserted a failure is visible three ways. Expired ESPN cookies are the most
    likely such failure and are a documented recurring event."""
    _world(marginal_world)

    def _boom(*a, **kw):
        raise RuntimeError("ESPN said no")

    monkeypatch.setattr(waiver, "build_waiver_plan", _boom)
    with pytest.raises(RuntimeError):
        C.run_freeze(db, as_of=PULL, season=SEASON, own_team_id=TEAM,
                     trigger=S.TRIGGER_TIMER, weeks=WEEKS, pool_limit=None,
                     root=tmp_path)
    row = S.last_capture(db)
    assert row["status"] == S.STATUS_FAILED
    assert "ESPN said no" in row["error"]
    assert "nothing to archive" in row["error"]
    assert S.format_status(db, today="2026-09-15").count("failed") >= 1


def test_a_credential_failure_is_recorded_not_only_journalled(db):
    """OPS-3, the other half: the CLI resolves credentials BEFORE run_freeze can
    record anything, so the package owns the row."""
    cid = C.record_prerun_failure(
        db, season=SEASON, trigger=S.TRIGGER_TIMER, plan_as_of=PULL,
        error="the capture never started: RuntimeError: missing ESPN pull credentials")
    row = S.capture_by_id(db, cid)
    assert row["status"] == S.STATUS_FAILED and row["week"] is None
    assert "missing ESPN pull credentials" in row["error"]


def test_status_says_how_old_the_archive_is_and_which_tuesdays_are_gone(db,
                                                                        marginal_world,
                                                                        tmp_path):
    """OPS-2. The report handled the EMPTY case emphatically and then treated
    every non-empty log as healthy — it could not tell 'captured tonight' from
    'last captured three Tuesdays ago', which is the failure the item exists to
    prevent. The word UNRECOVERABLE is literal here, unlike `ingest status`."""
    _world(marginal_world)
    _freeze(db, _collect(db), tmp_path, capture_id="cap-1",
            captured_at=__import__("datetime").datetime(2026, 9, 15, 18, 30,
                                                        tzinfo=C.CAPTURE_TZ))
    fresh = S.format_status(db, today="2026-09-15")
    assert "LAST CAPTURE : 2026-09-15 (captured today)" in fresh
    assert "MISSING TUESDAYS : none since 2026-09-15" in fresh

    stale = S.format_status(db, today="2026-10-06")
    assert "21 day(s) old" in stale
    assert "more than a week with no capture" in stale
    # 09-22, 09-29 and 10-06 are Tuesdays with no capture; 09-15 has one.
    assert "MISSING TUESDAYS : 3" in stale
    assert "2026-09-22" in stale and "2026-10-06" in stale
    assert "UNRECOVERABLE" in stale
    assert "2026-09-15" not in stale.split("MISSING TUESDAYS")[1].split("\n")[0]


def test_status_never_truncates_a_sentence_mid_word(db, marginal_world, tmp_path):
    """OPS-9. `NOTE=... The plan hal` reads as corruption to a novice — and the
    string being cut is the one that says a `partial` capture is EXPECTED."""
    _world(marginal_world)
    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    assert result.status == S.STATUS_PARTIAL
    text = S.format_status(db, today="2026-09-15")
    note = next(ln for ln in text.splitlines() if "NOTE=" in ln)
    assert note.rstrip().endswith("verify --capture cap-1)")
    assert "…" in note


def test_status_labels_the_joint_number(db, marginal_world, tmp_path):
    """OPS-10, Rule 6: a bare signed decimal beside three counts reads as a count
    or a score. Every other surface names the unit."""
    _world(marginal_world)
    _freeze(db, _collect(db), tmp_path, capture_id="cap-1")
    text = S.format_status(db, today="2026-09-15")
    assert "house pts" in text


def test_the_empty_status_counts_the_perishable_sources_correctly():
    """OPS-8. The same wave that corrected CLAUDE.md to SIX left two operator
    strings saying four — and this one is the first thing a fresh box prints."""
    assert "six market sources" in S.EMPTY_STATUS
    service = (Path(C.__file__).resolve().parents[2] / "scripts" / "systemd"
               / "ziggurat-decisions.service").read_text(encoding="utf-8")
    assert "six market sources" in service
    assert "four market sources" not in service + S.EMPTY_STATUS


def test_resolving_a_capture_target_is_package_code(db, marginal_world, tmp_path):
    """R4, Rule 3. The choice between two lookups, two failure sentences composed
    from row state and the digest hand-over were a 20-line CLI body; `decisions
    record` / `classify` / `latency` all need the same resolution."""
    with pytest.raises(R.CaptureNotFound) as exc:
        R.resolve_capture_target(db)
    assert "no capture recorded yet" in str(exc.value)
    with pytest.raises(R.CaptureNotFound) as exc:
        R.resolve_capture_target(db, capture_id="nope")
    assert "for id nope" in str(exc.value)

    freeze_id = S.start_capture(db, capture_id="cap-x", season=SEASON, week=3,
                                trigger=S.TRIGGER_CLI, plan_as_of=PULL,
                                started_at="2026-09-15T18:30:00")
    S.finish_capture(db, freeze_id, status=S.STATUS_FAILED,
                     finished_at="2026-09-15T18:30:01")
    with pytest.raises(R.CaptureIncomplete) as exc:
        R.resolve_capture_target(db, capture_id="cap-x")
    assert "nothing landed to verify" in str(exc.value)

    _world(marginal_world)
    result = _freeze(db, _collect(db), tmp_path, capture_id="cap-y")
    target, expected = R.resolve_capture_target(db, capture_id="cap-y")
    assert str(target) == result.directory
    assert expected == result.manifest_sha256
    assert R.verify(target, expected_manifest_sha256=expected).ok


# ------------------------------------------------- the episode history reader


def test_the_archive_feeds_the_next_weeks_badge(db, marginal_world, tmp_path,
                                                monkeypatch):
    """DC-2. The NEW/REPEAT rule shipped INERT: no caller passed `history=` and
    no adapter existed, so from week 2 onward every badge on every surface read
    'FIRST SEEN (no archive yet)' forever — the sentence the module itself
    defines as an absence of comparison, never a claim of novelty."""
    from ziggurat.core import candidates as CAND

    _world(marginal_world)
    root = tmp_path / "archive"

    def _board(week, keys):
        rows = tuple(
            CAND.CandidateRow(player_key=k, player=f"P {k}", position="RB",
                              team="ATL", gsis_id=k, espn_id=None,
                              signal_kind=CAND.SIGNAL_USAGE, magnitude=1.0,
                              week=week, prior_week=week - 1, hypothesis=False,
                              reasons=("up",))
            for k in keys
        )
        return CAND.CandidateBoard(rows=rows, week=week, freshness=(), notes=(),
                                   as_of=PULL, season=SEASON)

    # Two archived Tuesdays, each filed under the week ITS plan priced (so the
    # directories differ exactly as they do in production): 00-0001 fires in both,
    # 00-0002 only in week 1.
    for i, (week, keys) in enumerate(((1, ("00-0001", "00-0002")),
                                      (2, ("00-0001",))), start=1):
        frozen = _board(week, keys)
        monkeypatch.setattr(waiver, "build_candidates",
                            lambda *a, **kw: frozen)   # noqa: B023 — rebound each loop
        art = _collect(db, weeks=range(week + 1, 18))
        C.freeze_waiver_artifacts(db, art, trigger=S.TRIGGER_CLI, root=root,
                                  capture_id=f"cap-{i}", git=GIT)

    history = R.episode_history_provider(root)
    archived = history(season=SEASON, before_week=3)
    assert [w.week for w in archived] == [1, 2]
    assert CAND.episode_key(CAND.SIGNAL_USAGE, "00-0001") in archived[1].flagged

    assert CAND.episode_tag_for(CAND.SIGNAL_USAGE, "00-0001", week=3,
                                history=archived) == "REPEAT (also wk 2)"
    # 00-0002 last fired in week 1, two evaluated weeks back — inside the window.
    assert CAND.episode_tag_for(CAND.SIGNAL_USAGE, "00-0002", week=3,
                                history=archived) == "REPEAT (also wk 1)"
    # and a name the archive has never seen is NEW, not FIRST SEEN
    assert CAND.episode_tag_for(CAND.SIGNAL_USAGE, "00-9999", week=3,
                                history=archived) == CAND.EPISODE_NEW
    # the reader is bounded by `before_week`
    assert [w.week for w in history(season=SEASON, before_week=2)] == [1]


def test_an_unreadable_capture_leaves_a_hole_not_an_exception(db, marginal_world,
                                                              tmp_path, monkeypatch):
    """DC-2. A digest mismatch must cost that WEEK its comparison, never the badge
    for the whole season — and `episode_tag_for` already reports a hole as a
    BOUND ('NEW (no archive for N earlier wk(s))') rather than as a fact."""
    from ziggurat.core import candidates as CAND

    _world(marginal_world)
    root = tmp_path / "archive"
    _with_candidate_board(monkeypatch)
    result = C.freeze_waiver_artifacts(db, _collect(db), trigger=S.TRIGGER_CLI,
                                       root=root, capture_id="cap-1", git=GIT)
    board_file = Path(result.directory) / C.BOARD_FILE
    board_file.write_bytes(board_file.read_bytes() + b'{"record": "tampered"}\n')

    history = R.episode_history_provider(root)
    assert history(season=SEASON, before_week=9) == []
    tag = CAND.episode_tag_for(CAND.SIGNAL_USAGE, "00-000999", week=9, history=[])
    assert tag == CAND.EPISODE_FIRST_SEEN
