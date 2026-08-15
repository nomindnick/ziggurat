"""Tests for the headless draft-session controller (``ziggurat/draft/session.py``).

All offline on a synthetic board with SYNTHETIC names (Rule 5 — never a real
colleague/player name; the conftest ``make_draft_board`` factory names players
"QB0", "RB1", ...). Engine rollouts are kept tiny (``rollouts=8``) so the
16-operator-turn drive-throughs run in well under a second.

Covers the recon §4 drive-through plus the §3 MUSTs: full 160-pick state/log
consistency, mis-entry correction (undo + arbitrary edit) via replay, crash-and-
resume to bit-identical state, ctx-reseed determinism, a non-identity pick_order
keying snake/survival correctly, fsync/atomic-rewrite exercise, and the
recalibration honesty fields on a cold start.
"""

import json
import os
from pathlib import Path
from time import perf_counter

import pytest

import ziggurat.draft.session as session_mod
from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.draft.bots import BoardEntry, min_to_complete, position_counts
from ziggurat.draft.session import DraftSession, JournalExistsError, PickRecord
from ziggurat.draft.simulator import ROUNDS, snake_sequence
from ziggurat.draft.survival import LIVE_RECAL_MIN_PICKS

IDENTITY = list(range(10))


def _start(tmp_path, board, *, operator_slot=0, pick_order=None, rollouts=8, seed=42, name="s"):
    return DraftSession.start(
        board,
        operator_slot=operator_slot,
        pick_order=IDENTITY if pick_order is None else pick_order,
        season=2026,
        as_of="2026-07-22",
        journal_path=Path(tmp_path) / f"{name}.jsonl",
        session_seed=seed,
        rollouts=rollouts,
    )


def _next_pid(session, seat):
    """The player id to enter next: engine on operator turns, autodraft otherwise."""
    if session.is_operator_turn:
        return session.recommend(top=3)[0].player_id
    return session.suggest_autodraft(seat)


def _drive(session, *, until=None):
    """Drive the session to completion (or to ``until`` confirmed picks)."""
    while not session.complete:
        if until is not None and len(session.picks) >= until:
            return
        session.append_pick(_next_pid(session, session.current_seat))


def _all_rosters(session):
    rosters = dict(session.opponent_rosters)
    rosters[session.operator_slot] = session.own_roster
    return rosters


# ------------------------------------------------------------ full drive-through


def test_full_drive_through_keeps_state_and_log_consistent(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=4)
    seq = snake_sequence(IDENTITY, ROUNDS)

    step = 0
    while not sess.complete:
        overall = sess.overall_pick
        assert overall == len(sess.picks) + 1
        assert sess.current_seat == seq[overall - 1]
        seat = sess.current_seat
        before_taken = len(sess.taken)

        if sess.is_operator_turn:
            t0 = perf_counter()
            recs = sess.recommend(top=3)
            # recon §4 done-when (9): no single interaction exceeds the ~5 s budget.
            assert perf_counter() - t0 < 5.0
            assert recs and all(r.reasons for r in recs)  # Rule 6: never empty
            pid = recs[0].player_id
        else:
            pid = sess.suggest_autodraft(seat)
        assert pid not in sess.taken

        sess.append_pick(pid)
        step += 1
        assert len(sess.taken) == before_taken + 1
        assert sess.picks[-1] == PickRecord(overall, seat, pid, board_name(board, pid))
        # journalled durably before this point: line count keeps pace (header + picks)
        assert _line_count(sess.journal_path) == 1 + len(sess.picks)

    assert sess.complete
    assert step == 160
    assert len(sess.picks) == 160
    assert sess.current_seat is None and sess.is_operator_turn is False

    # every finished roster is a legal 16-round lineup (Rule 6 rails held throughout)
    for entries in _all_rosters(sess).values():
        assert len(entries) == ROUNDS
        assert min_to_complete(position_counts(entries), DEFAULT_ROSTER) == 0


def board_name(board, pid):
    return next(e.name for e in board if e.player_id == pid)


def _line_count(path):
    return len(Path(path).read_text(encoding="utf-8").splitlines())


# ------------------------------------------------------- recommend gating + reseed


def test_recommend_raises_off_the_operator_turn(tmp_path, make_draft_board):
    board = make_draft_board()
    # operator_slot 5 with identity order: overall 1 is on seat 0, not the operator.
    sess = _start(tmp_path, board, operator_slot=5)
    assert not sess.is_operator_turn
    with pytest.raises(RuntimeError, match="operator's turn"):
        sess.recommend()


def test_recommend_is_bit_identical_across_calls(tmp_path, make_draft_board):
    # Guards the recon §1 idempotence bug at a state where it actually BITES: a
    # NON-WHEEL operator turn (picks_until_next >= 1) where survival runs real
    # Monte-Carlo rollouts. (operator_slot=0's first two turns form a wheel where
    # survival short-circuits to 1.0 for every candidate — a held-ctx bug is
    # invisible there, so that placement cannot catch the regression; audit arch F1.)
    # A held ctx would mutate ctx.rng and flicker survival between calls; a fresh
    # state-seeded ctx per call is bit-identical INCLUDING the survival-derived reason
    # strings.
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=4)  # middle seat: opener is non-wheel
    while not sess.is_operator_turn:
        sess.append_pick(sess.suggest_autodraft(sess.current_seat))

    # confirm this turn is a real rollout state, not the wheel short-circuit
    assert not sess._is_snake_turn()
    nxt = sess.overall_pick + 1
    assert sess._sequence[nxt - 1] != sess.operator_slot  # picks_until_next >= 1

    a = tuple(sess.recommend(top=5))
    b = tuple(sess.recommend(top=5))
    # survival genuinely ran here (the wheel short-circuit would make every value 1.0),
    # so a held-ctx reuse WOULD flicker — this state can discriminate the bug.
    assert not all(r.survival_next == 1.0 for r in a)
    assert a == b
    assert [r.survival_next for r in a] == [r.survival_next for r in b]
    assert [r.reasons for r in a] == [r.reasons for r in b]  # survival-derived reasons stable


# ------------------------------------------------------------- mis-entry correction


def test_undo_last_rebuilds_prior_state(tmp_path, make_draft_board):
    board = make_draft_board()
    long = _start(tmp_path, board, operator_slot=3, name="long")
    _drive(long, until=20)
    short = _start(tmp_path, board, operator_slot=3, name="short")
    _drive(short, until=19)

    long.undo_last()
    assert long.picks == short.picks
    assert long.taken == short.taken
    assert long.own_roster == short.own_roster
    assert long.opponent_rosters == short.opponent_rosters
    assert long.overall_pick == short.overall_pick
    # the rewritten log matches too (header + 19 picks)
    assert _line_count(long.journal_path) == 1 + 19


def test_edit_pick_player_rewrites_via_replay(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=2)
    _drive(sess, until=20)

    target = next(p for p in sess.picks if p.overall == 5)
    old_pid = target.player_id
    replacement = next(e.player_id for e in board if e.player_id not in sess.taken)

    sess.edit_pick(5, player_id=replacement)

    edited = next(p for p in sess.picks if p.overall == 5)
    assert edited.player_id == replacement
    assert edited.seat == target.seat  # seat unchanged
    assert replacement in sess.taken
    assert old_pid not in sess.taken  # freed by the edit
    seat_ids = {e.player_id for e in _all_rosters(sess)[target.seat]}
    assert replacement in seat_ids and old_pid not in seat_ids
    assert len(sess.picks) == 20  # count unchanged; only overall 5 swapped


def test_edit_pick_no_longer_accepts_a_seat_argument(tmp_path, make_draft_board):
    # Audit state F2: snake geometry fully determines the drafting seat from the
    # overall, so a seat override could only silently desync _rosters from the snake
    # order. The parameter is REMOVED — passing seat= is a hard TypeError — and the
    # player edit keeps the snake-derived seat.
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=1)
    _drive(sess, until=20)

    with pytest.raises(TypeError):
        sess.edit_pick(3, seat=5)  # type: ignore[call-arg]

    target = next(p for p in sess.picks if p.overall == 3)
    snake_seat = sess._sequence[3 - 1]
    assert target.seat == snake_seat  # seat is the snake-derived seat, not editable
    repl = next(e.player_id for e in board if e.player_id not in sess.taken)
    sess.edit_pick(3, player_id=repl)

    edited = next(p for p in sess.picks if p.overall == 3)
    assert edited.player_id == repl
    assert edited.seat == snake_seat  # seat unchanged after a player edit
    assert repl in {e.player_id for e in _all_rosters(sess)[snake_seat]}
    assert target.player_id not in sess.taken  # old player freed


def test_correction_guards(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board)
    with pytest.raises(RuntimeError, match="no picks to undo"):
        sess.undo_last()
    _drive(sess, until=5)
    with pytest.raises(ValueError, match="no confirmed pick at overall"):
        sess.edit_pick(99, player_id="whatever")
    # editing an earlier pick to a player already drafted elsewhere is rejected
    already = sess.picks[0].player_id  # overall 1's player
    with pytest.raises(ValueError, match="already drafted"):
        sess.edit_pick(2, player_id=already)
    # entering an already-drafted player is rejected
    with pytest.raises(ValueError, match="already drafted"):
        sess.append_pick(already)


# ------------------------------------------------------------- crash and resume


def test_resume_reproduces_bit_identical_state_then_finishes(tmp_path, make_draft_board):
    board = make_draft_board()
    # straight-through reference draft (same seed/config as the crashed one)
    full = _start(tmp_path, board, operator_slot=6, seed=7, name="full")
    _drive(full)

    crashed = _start(tmp_path, board, operator_slot=6, seed=7, name="crash")
    _drive(crashed, until=37)

    resumed = DraftSession.resume(crashed.journal_path, board)
    # bit-identical to the crashed session at the crash point
    assert resumed.picks == crashed.picks
    assert resumed.taken == crashed.taken
    assert resumed.own_roster == crashed.own_roster
    assert resumed.opponent_rosters == crashed.opponent_rosters
    assert resumed.overall_pick == crashed.overall_pick
    rc, rr = crashed.recalibration(), resumed.recalibration()
    assert (rc.engaged, rc.n_room_picks, rc.reach_sigma) == (
        rr.engaged, rr.n_room_picks, rr.reach_sigma,
    )

    # finishing the resumed draft reproduces the reference draft exactly
    _drive(resumed)
    assert resumed.complete
    assert resumed.picks == full.picks


def test_resume_rejects_a_mismatched_board(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board)
    _drive(sess, until=6)
    other = make_draft_board(wr=40)  # different player-id set -> different provenance
    with pytest.raises(ValueError, match="does not match"):
        DraftSession.resume(sess.journal_path, other)


# ------------------------------------------------------- non-identity pick_order


def test_non_identity_pick_order_keys_snake_and_survival(tmp_path, make_draft_board):
    board = make_draft_board()
    order = [3, 7, 0, 9, 1, 5, 2, 8, 4, 6]  # non-identity permutation
    operator_slot = 9
    sess = _start(tmp_path, board, operator_slot=operator_slot, pick_order=order)
    assert sess.operator_dp == order.index(operator_slot)  # == 3

    seq = snake_sequence(order, ROUNDS)
    expected_operator_overalls = [i + 1 for i, s in enumerate(seq) if s == operator_slot]

    # drive, recording where the operator actually lands + that recs stay legal
    landed = []
    while not sess.complete:
        seat = sess.current_seat
        if sess.is_operator_turn:
            landed.append(sess.overall_pick)
            # the engine ctx keys opponents by DRAFT POSITION (survival's space),
            # never by seat id — verified below at the first operator turn.
            recs = sess.recommend(top=3)
            assert recs and all(r.reasons for r in recs)
            sess.append_pick(recs[0].player_id)
        else:
            sess.append_pick(sess.suggest_autodraft(seat))

    assert landed == expected_operator_overalls  # picks land at the right overalls

    for entries in _all_rosters(sess).values():
        assert min_to_complete(position_counts(entries), DEFAULT_ROSTER) == 0


def test_operator_context_keys_opponents_by_draft_position(tmp_path, make_draft_board):
    board = make_draft_board()
    order = [3, 7, 0, 9, 1, 5, 2, 8, 4, 6]
    sess = _start(tmp_path, board, operator_slot=9, pick_order=order)
    # advance to the operator's first turn
    while not sess.is_operator_turn:
        sess.append_pick(sess.suggest_autodraft(sess.current_seat))

    ctx = sess._operator_context(
        overall=sess.overall_pick, own_roster=sess.own_roster, taken=sess.taken
    )
    # keys are draft positions (0..9 minus the operator's), NOT seat ids
    assert set(ctx.opponent_rosters.keys()) == set(range(10)) - {sess.operator_dp}
    for dp, entries in ctx.opponent_rosters.items():
        assert entries == tuple(sess._rosters[order[dp]])


# ---------------------------------------------------------------- contingencies


def test_contingencies_only_at_a_snake_turn(tmp_path, make_draft_board):
    board = make_draft_board()
    # corner seat (draft position 0) picks back-to-back at the R1/R2 turn.
    sess = _start(tmp_path, board, operator_slot=0)
    assert sess.is_operator_turn and not sess._is_snake_turn()  # R1 opener, not a wheel
    assert sess.contingencies() == ()

    # drive to the operator's R1 pick then to their R2 wheel pick
    _drive(sess, until=1)  # operator took R1
    while not sess.is_operator_turn:
        sess.append_pick(sess.suggest_autodraft(sess.current_seat))
    # now at overall 20 (R2 opener for the reversed corner) — the wheel back-to-back
    assert sess._is_snake_turn()
    conts = sess.contingencies()
    assert 1 <= len(conts) <= 3
    for c in conts:
        assert c.first.reasons and c.wheel.reasons
        assert c.first.player_id != c.wheel.player_id
        assert "take" in c.message.lower()


# ------------------------------------------------------- recalibration honesty


def test_recalibration_cold_start_is_honest(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board)
    status = sess.recalibration()
    assert status.engaged is False
    assert status.n_room_picks == 0
    assert status.min_room_picks == LIVE_RECAL_MIN_PICKS
    assert status.picks_needed == LIVE_RECAL_MIN_PICKS
    assert "2025 baseline" in status.message
    assert "more room picks needed" in status.message
    assert "recalibrated to 0" not in status.message  # never a dishonest zero

    # a handful of picks stays cold-start but the honesty count moves
    _drive(sess, until=8)
    later = sess.recalibration()
    assert later.engaged is False
    assert later.picks_needed <= LIVE_RECAL_MIN_PICKS
    assert "baseline" in later.message


# ------------------------------------------------- journal durability mechanics


def test_journal_fsync_and_atomic_rewrite_paths(tmp_path, make_draft_board, monkeypatch):
    board = make_draft_board()
    fsync_calls = {"n": 0}
    replace_calls = {"n": 0}
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        fsync_calls["n"] += 1
        return real_fsync(fd)

    def spy_replace(src, dst):
        replace_calls["n"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(session_mod.os, "fsync", spy_fsync)
    monkeypatch.setattr(session_mod.os, "replace", spy_replace)

    sess = _start(tmp_path, board)
    assert fsync_calls["n"] >= 1  # header fsync'd at start
    _drive(sess, until=3)
    after_appends = fsync_calls["n"]
    assert after_appends >= 4  # header + 3 appends, each fsync'd before ack
    assert replace_calls["n"] == 0  # appends never rewrite

    sess.undo_last()  # atomic rewrite: temp fsync + os.replace
    assert replace_calls["n"] == 1
    assert fsync_calls["n"] > after_appends
    # and the file on disk is a valid header + 2 picks after the rewrite
    lines = Path(sess.journal_path).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "header"
    assert len(lines) == 1 + 2


def test_journal_header_is_readable_and_carries_provenance(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=4, seed=99, rollouts=8)
    header = json.loads(Path(sess.journal_path).read_text(encoding="utf-8").splitlines()[0])
    assert header["kind"] == "header"
    assert header["operator_slot"] == 4
    assert header["session_seed"] == 99
    assert header["board_count"] == len(board)
    assert header["pick_order"] == IDENTITY
    assert header["roster"]["teams"] == DEFAULT_ROSTER.teams


# --------------------------------------------- start() crash-safety (F1 / F3 / F5)


def test_second_start_refuses_to_clobber_existing_journal(tmp_path, make_draft_board):
    # Crash F1 (CRITICAL): a second start() on the same path used to open("w") and
    # TRUNCATE the journal, destroying every confirmed pick — the exact "terminal
    # died, operator re-ran without --resume" footgun. Now it is exclusive-create.
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=0, name="dup")
    _drive(sess, until=6)
    before = sess.journal_path.read_text(encoding="utf-8")

    with pytest.raises(JournalExistsError) as ei:
        _start(tmp_path, board, operator_slot=0, name="dup")  # same journal path

    assert sess.journal_path.read_text(encoding="utf-8") == before  # picks survived
    msg = str(ei.value).lower()
    assert "--resume" in msg  # novice-legible recovery hint
    assert "already exists" in msg or "overwrite" in msg
    assert isinstance(ei.value, RuntimeError)  # contract: subclasses RuntimeError


def test_start_creates_missing_parent_directory(tmp_path, make_draft_board):
    # Crash F3 (MAJOR): data/draft/ is gitignored and absent on a fresh clone, so
    # the first real launch used to FileNotFoundError before the draft began.
    board = make_draft_board()
    journal_path = Path(tmp_path) / "data" / "draft" / "session-x.jsonl"
    assert not journal_path.parent.exists()

    sess = DraftSession.start(
        board, operator_slot=0, pick_order=IDENTITY, season=2026, as_of="2026-07-22",
        journal_path=journal_path, rollouts=8,
    )

    assert journal_path.exists()  # start() mkdir'd the parent and wrote the header
    header = json.loads(journal_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["kind"] == "header"
    assert sess.overall_pick == 1


def test_start_and_rewrite_fsync_the_containing_directory(
    tmp_path, make_draft_board, monkeypatch
):
    # Crash F5 (NOTE): the file create and the os.replace rename are not power-durable
    # without a directory fsync. start() and the atomic rewrite both do one now.
    board = make_draft_board()
    calls = []
    real = session_mod._fsync_dir

    def spy(p):
        calls.append(Path(p))
        return real(p)

    monkeypatch.setattr(session_mod, "_fsync_dir", spy)

    sess = _start(tmp_path, board)
    assert sess.journal_path in calls  # start fsync'd the journal's directory
    n_after_start = len(calls)

    _drive(sess, until=3)
    sess.undo_last()  # atomic-rewrite path
    assert len(calls) > n_after_start  # rewrite fsync'd the directory after os.replace


# ------------------------------------------ resume torn-tail tolerance (F2 / F6)


def test_resume_tolerates_a_torn_final_line(tmp_path, make_draft_board):
    # Crash F2 (MAJOR): a partial/torn FINAL line (power loss mid-write) used to brick
    # resume of the entire durable prefix. Now the torn tail is dropped, the clean
    # prefix recovered, and a human sentence recorded in resume_warnings.
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=0)
    _drive(sess, until=12)
    n = len(sess.picks)
    # emulate a partial append: a truncated JSON line with no trailing newline
    with open(sess.journal_path, "a", encoding="utf-8") as f:
        f.write('{"kind": "pick", "overall": 13, "seat": 2, "player_')

    resumed = DraftSession.resume(sess.journal_path, board)

    assert len(resumed.picks) == n  # 12 clean picks recovered
    assert resumed.picks == sess.picks
    assert resumed.taken == sess.taken
    assert resumed.resume_warnings  # a human sentence was recorded
    assert any("partial" in w.lower() for w in resumed.resume_warnings)


def test_resume_rejects_corruption_that_is_not_the_final_line(tmp_path, make_draft_board):
    # Only the FINAL line may be dropped; corruption anywhere earlier stays LOUD
    # (dropping it would silently lose durable confirmed picks).
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=0)
    _drive(sess, until=8)
    lines = sess.journal_path.read_text(encoding="utf-8").splitlines()
    lines[3] = '{"kind": "pick", "overall": 3, torn'  # corrupt a MIDDLE pick line
    sess.journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="corrupt"):
        DraftSession.resume(sess.journal_path, board)


def test_start_records_no_resume_warnings(tmp_path, make_draft_board):
    # A fresh session always exposes an (empty) resume_warnings list so the app can
    # read it unconditionally.
    board = make_draft_board()
    sess = _start(tmp_path, board)
    assert sess.resume_warnings == []


def test_resume_rejects_duplicate_player_ids(tmp_path, make_draft_board):
    # Crash F6: a tampered/corrupt journal with the SAME player drafted twice used to
    # resume silently (taken < picks, a player on two rosters). Now replay revalidates.
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=0)
    _drive(sess, until=6)
    lines = sess.journal_path.read_text(encoding="utf-8").splitlines()
    p1 = json.loads(lines[1])
    p3 = json.loads(lines[3])
    p3["player_id"] = p1["player_id"]  # overall 3 now duplicates overall 1's player
    lines[3] = json.dumps(p3)
    sess.journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="more than once"):
        DraftSession.resume(sess.journal_path, board)


def test_rewrite_journal_unlinks_tmp_on_failure(tmp_path, make_draft_board, monkeypatch):
    # Crash F7: a failed rewrite must not orphan a partial <journal>.tmp, and must
    # leave the original journal intact (os.replace never ran).
    board = make_draft_board()
    sess = _start(tmp_path, board)
    _drive(sess, until=4)
    tmp = sess.journal_path.with_name(sess.journal_path.name + ".tmp")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(session_mod.os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        sess.undo_last()

    assert not tmp.exists()  # no orphaned .tmp left behind
    lines = Path(sess.journal_path).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "header"
    assert len(lines) == 1 + 4  # original journal untouched (undo did not commit)
    assert len(sess.picks) == 4  # in-memory state unchanged too


# ----------------------------------- journal discovery + header-driven resume (NEW-1)


def test_find_latest_journal_picks_newest_by_name(tmp_path):
    d = Path(tmp_path) / "draft"
    d.mkdir()
    assert session_mod.find_latest_journal(d) is None  # none yet
    (d / "session-20260825-090000.jsonl").write_text("{}\n")
    (d / "session-20260825-101500.jsonl").write_text("{}\n")
    (d / "session-20260825-094500.jsonl").write_text("{}\n")
    (d / "not-a-session.txt").write_text("x")  # ignored by the glob
    latest = session_mod.find_latest_journal(d)
    assert latest is not None and latest.name == "session-20260825-101500.jsonl"


def test_find_latest_journal_missing_dir_is_none(tmp_path):
    assert session_mod.find_latest_journal(Path(tmp_path) / "does-not-exist") is None


def test_read_journal_header_round_trips_provenance(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=4, seed=99)
    header = session_mod.read_journal_header(sess.journal_path)
    assert header["kind"] == "header"
    assert header["as_of"] == "2026-07-22"
    assert header["season"] == 2026
    assert header["operator_slot"] == 4


def test_read_journal_header_is_loud_on_missing_empty_and_corrupt(tmp_path):
    with pytest.raises(ValueError):
        session_mod.read_journal_header(Path(tmp_path) / "gone.jsonl")

    empty = Path(tmp_path) / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        session_mod.read_journal_header(empty)

    not_header = Path(tmp_path) / "nh.jsonl"
    not_header.write_text('{"kind": "pick"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        session_mod.read_journal_header(not_header)

    corrupt = Path(tmp_path) / "corrupt.jsonl"
    corrupt.write_text("{not valid json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        session_mod.read_journal_header(corrupt)


def test_resume_after_date_change_recovers_via_discovery(tmp_path, make_draft_board):
    # Crash NEW-1 / arch NEW-1: the OLD default name was deterministic-per-DAY, so a
    # crash/resume across midnight targeted a different (nonexistent) file and orphaned
    # the picks. The new flow keys on the newest TIMESTAMPED journal by name and reloads
    # at the journalled as_of (NOT today), so a date rollover recovers cleanly.
    board = make_draft_board()
    draft_dir = Path(tmp_path) / "draft"
    jp = draft_dir / "session-20260825-231800.jsonl"  # started "yesterday"
    sess = DraftSession.start(
        board, operator_slot=0, pick_order=IDENTITY, season=2026, as_of="2026-08-25",
        journal_path=jp, rollouts=8,
    )
    _drive(sess, until=20)

    # after midnight, `--resume` with no --journal/--as-of: discover newest + read header
    found = session_mod.find_latest_journal(draft_dir)
    assert found == jp
    header = session_mod.read_journal_header(found)
    assert header["as_of"] == "2026-08-25"  # the ORIGINAL snapshot, not today
    assert header["season"] == 2026

    resumed = DraftSession.resume(found, board)
    assert len(resumed.picks) == 20
    assert resumed.picks == sess.picks


# ---------------------------------------------- board provenance backstop (arch F2)


def test_resume_rejects_a_board_whose_ranks_drifted(tmp_path, make_draft_board):
    # Arch F2: _board_hash covered only the id set, so a same-membership board whose
    # ESPN ranks drifted (a mid-session re-pull at a different as_of) resumed silently
    # onto a re-priced board. The hash now covers (player_id, espn_overall_rank) pairs.
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=0)
    _drive(sess, until=6)

    drifted = tuple(
        BoardEntry(
            e.player_id, e.name, e.position,
            e.espn_overall_rank + 5,  # same membership, drifted ranks
            e.house_points, e.vor, e.team,
        )
        for e in board
    )
    with pytest.raises(ValueError, match="does not match"):
        DraftSession.resume(sess.journal_path, drifted)


# ------------------------------------------- queue-writer seam (auto-entry spec §6)


def test_next_operator_overall_on_and_off_turn(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=0)
    # On the clock: the next operator pick IS the current one.
    assert sess.next_operator_overall() == 1
    sess.append_pick(sess.recommend(top=1)[0].player_id)
    # Off-turn, identity order, slot 0: round 2 runs reversed, so dp0 picks
    # last of it — overall 20.
    assert not sess.is_operator_turn
    assert sess.next_operator_overall() == 20

    late = _start(tmp_path, board, operator_slot=4, name="late")
    assert late.next_operator_overall() == 5  # dp4's opener, computed off-turn


def test_next_operator_overall_follows_a_non_identity_pick_order(tmp_path, make_draft_board):
    # pick_order maps draft position -> seat; the scan must follow the REAL
    # sequence, not assume identity. Seat 0 drafts 4th here (dp 3), and round 2
    # reversed puts dp3 at overall 17 — both from snake geometry, not _sequence.
    board = make_draft_board()
    order = [5, 3, 8, 0, 9, 1, 7, 2, 6, 4]
    sess = _start(tmp_path, board, operator_slot=0, pick_order=order, name="perm")
    assert sess.operator_dp == 3
    assert sess.next_operator_overall() == 4
    _drive(sess, until=4)  # through the operator's opener
    assert sess.next_operator_overall() == 17


def test_next_operator_overall_none_when_operator_is_done(tmp_path, make_draft_board):
    # Identity order, slot 1: dp1's LAST pick is overall 159 (round 16 reversed),
    # so at overall 160 the draft is NOT complete but no operator pick remains —
    # the one state where "nothing to queue" must be explicit, never an empty
    # list a writer could mistake for "clear the queue".
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=1, name="done")
    while len(sess.picks) < 159:
        sess.append_pick(sess.suggest_autodraft(sess.current_seat))
    assert not sess.complete and sess.overall_pick == 160
    assert sess.next_operator_overall() is None
    with pytest.raises(RuntimeError, match="nothing to queue"):
        sess.recommend_upcoming()
    sess.append_pick(sess.suggest_autodraft(sess.current_seat))
    assert sess.complete
    assert sess.next_operator_overall() is None


def test_recommend_upcoming_equals_recommend_on_the_operator_turn(tmp_path, make_draft_board):
    # The §8.5 determinism guarantee: on the clock, the queue head IS the
    # on-clock recommendation — same ctx, same session_seed ^ overall rng,
    # bit-identical output INCLUDING survival-derived reason strings. Placed at
    # a non-wheel turn where survival runs real rollouts (a wheel short-circuit
    # would make the comparison vacuous — see the bit-identical test above).
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=4)
    while not sess.is_operator_turn:
        sess.append_pick(sess.suggest_autodraft(sess.current_seat))
    assert not sess._is_snake_turn()

    a = tuple(sess.recommend(top=5))
    b = tuple(sess.recommend_upcoming(top=5))
    assert not all(r.survival_next == 1.0 for r in a)  # rollouts genuinely ran
    assert a == b


def test_recommend_upcoming_works_off_turn_and_excludes_taken(tmp_path, make_draft_board):
    board = make_draft_board()
    sess = _start(tmp_path, board, operator_slot=0)
    sess.append_pick(sess.recommend(top=1)[0].player_id)
    for _ in range(3):  # rivals draft; their picks must vanish from the queue
        sess.append_pick(sess.suggest_autodraft(sess.current_seat))
    assert not sess.is_operator_turn
    with pytest.raises(RuntimeError, match="operator's turn"):
        sess.recommend()  # the on-clock surface still refuses off-turn

    recs = sess.recommend_upcoming(top=8)
    assert 0 < len(recs) <= 8
    assert all(r.player_id not in sess.taken for r in recs)
    assert all(r.reasons for r in recs)  # Rule 6: never an unexplained row
