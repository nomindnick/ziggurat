"""End-to-end tests for the item-2.4 draft-board TUI edge (``ziggurat/draft/app.py``).

All offline, deterministic, SYNTHETIC names only (Rule 5 — "Alpha Runner",
"Filler RB0", never real colleague or player identities). The app is the only
module with terminal I/O, so we drive :func:`run_app` with scripted stdin
(``monkeypatch`` on ``builtins.input``) and a ``Console(file=StringIO())`` and
assert on the captured text, the in-memory ``DraftSession`` state, and the JSONL
journal on disk. No real DB, no network.

These tests close the resolve->enter seam that had zero coverage (audit arch F3)
and lock the app-loop fixes:
  * autodraft routes through the legality-aware ``suggest_autodraft`` (state F1)
    and refuses on the operator's own turn (state NEW-1);
  * a fired posture tip is HELD across picks until acknowledged, p accepts /
    x dismisses (ux NEW-1, ux F4);
  * the final pick renders the finish once with no "seat None" (ux F2);
  * "e <name>" is a name search, not the edit command (ux F3);
  * a stray digit at the main prompt re-prompts legibly (ux F5);
  * an already-drafted append and a journal clobber are guarded, not crashes
    (crash F4 + the pinned JournalExistsError / resume_warnings seam).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.draft.app import HELP, _enter_pick, launch, run_app
from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.posture import PostureMonitor, PostureProjection
from ziggurat.draft.resolver import NameResolver, Resolution
from ziggurat.draft.session import DraftSession

# ------------------------------------------------------------------ fixtures


def _board() -> tuple[BoardEntry, ...]:
    """A synthetic board deep enough for a full 10-team / 16-round draft (>=160
    players) plus five named "stars" the interaction tests query by name:

      * "Alpha Runner" / "Bravo Runner" — a shared surname (query "runner" ties
        -> the confirm panel);
      * "Zulu Unique" / "Yankee Solo" — unique full names (auto-commit);
      * "Echo Goedert" — a first-initial-E surname, so "e goedert" exercises the
        edit-vs-search grammar.
    """
    repl = {"QB": 240.0, "RB": 120.0, "WR": 120.0, "TE": 150.0, "DST": 95.0, "K": 100.0}
    entries: list[BoardEntry] = []
    stars = [
        ("RB-ALPHA", "Alpha Runner", "RB", 1, 300.0),
        ("RB-BRAVO", "Bravo Runner", "RB", 2, 297.0),
        ("WR-ZULU", "Zulu Unique", "WR", 3, 295.0),
        ("WR-YANKEE", "Yankee Solo", "WR", 4, 293.0),
        ("WR-ECHO", "Echo Goedert", "WR", 5, 290.0),
    ]
    for pid, name, pos, rank, pts in stars:
        entries.append(BoardEntry(pid, name, pos, rank, pts, pts - repl[pos], None))

    rank = 6
    specs = {"QB": (315.0, 6.0, 20), "RB": (285.0, 3.0, 60), "WR": (285.0, 2.5, 60),
             "TE": (228.0, 5.0, 20), "DST": (128.0, 3.0, 16), "K": (118.0, 2.0, 16)}
    for pos, (top, dec, cnt) in specs.items():
        for i in range(cnt):
            pts = max(1.0, top - dec * i)
            entries.append(
                BoardEntry(f"{pos}-F{i}", f"Filler {pos}{i}", pos, rank, pts, pts - repl[pos], None)
            )
            rank += 1
    return tuple(entries)


@pytest.fixture()
def board() -> tuple[BoardEntry, ...]:
    return _board()


def _session(
    board: tuple[BoardEntry, ...], journal: Path, *, operator_slot: int = 0, rollouts: int = 1
) -> DraftSession:
    return DraftSession.start(
        board,
        operator_slot=operator_slot,
        pick_order=list(range(DEFAULT_ROSTER.teams)),
        season=2026,
        as_of="2026-07-22",
        journal_path=journal,
        roster=DEFAULT_ROSTER,
        session_seed=42,
        rollouts=rollouts,
    )


def _quiet_monitor() -> PostureMonitor:
    """A monitor whose comparator never fires (posture is not under test here)."""
    return PostureMonitor(margin=8.0, consecutive=2, cooldown=3, evaluator=lambda s: None)


def _loud_eval(session: object) -> PostureProjection:
    """A comparator that always clears the margin with the same lean (so the tip
    fires at the operator's opening turn, injected to bypass the roster-drift gate)."""
    return PostureProjection(
        "balanced", "zero_rb", "WR", 50.0, 1000.0, 1050.0, getattr(session, "overall_pick", 1)
    )


def _run(session, resolver, monitor, inputs, monkeypatch, *, width: int = 100) -> str:
    """Drive ``run_app`` with a scripted stdin and capture its output."""
    it = iter(inputs)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration as exc:  # ran out of script -> behave like Ctrl-D
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", fake_input)
    buf = io.StringIO()
    console = Console(file=buf, width=width, no_color=True, highlight=False)
    run_app(session, resolver, console, monitor)
    return buf.getvalue()


# ----------------------------------------------------- resolve -> enter seam (arch F3)


def test_resolve_enter_seam_drive_through(board, tmp_path, monkeypatch):
    # One drive through the whole input grammar: confirm-panel pick, auto pick,
    # garbage re-prompt, "e <name>" search, undo, stray digit, quit.
    journal = tmp_path / "seam.jsonl"
    sess = _session(board, journal)
    inputs = [
        "runner", "1",       # tie -> confirm panel -> digit -> commit Alpha Runner
        "zulu unique",       # unique full name -> auto-commit Zulu Unique
        "zzz",               # garbage -> "No match", re-prompt, no pick
        "2",                 # stray digit at the main prompt -> legible re-prompt
        "e goedert", "1",    # "e <name>" is a search (NOT the editor) -> commit Echo
        "u",                 # undo the last pick (Echo)
        "q",
    ]
    out = _run(sess, NameResolver(board), _quiet_monitor(), inputs, monkeypatch)

    assert "Traceback" not in out
    committed = [p.player_id for p in sess.picks]
    assert committed == ["RB-ALPHA", "WR-ZULU"]        # Echo was entered then undone
    assert "Which player did you mean" in out          # the confirm panel appeared
    assert "No match for 'zzz'" in out                 # garbage re-prompted
    assert "Numbers 1-3 only choose" in out            # stray digit re-prompted legibly
    assert "Usage: e" not in out                       # "e goedert" was NOT read as an edit

    # the on-disk journal matches the in-memory committed picks (undo rewrote it)
    lines = journal.read_text(encoding="utf-8").splitlines()
    pick_ids = [json.loads(ln)["player_id"] for ln in lines if '"kind": "pick"' in ln]
    assert pick_ids == ["RB-ALPHA", "WR-ZULU"]


# ------------------------------------------------------------- "e <name>" vs "e <N>"


def test_e_prefix_searches_on_a_name_and_edits_on_a_number(board, tmp_path, monkeypatch):
    resolver = NameResolver(board)

    # (a) "e goedert" is a first-initial name search, not the edit command (ux F3).
    s1 = _session(board, tmp_path / "e1.jsonl")
    out1 = _run(s1, resolver, _quiet_monitor(), ["e goedert", "1", "q"], monkeypatch)
    assert "Usage: e" not in out1
    assert [p.player_id for p in s1.picks] == ["WR-ECHO"]

    # (b) "e <N>" still edits the earlier pick (seat is snake-derived, not asked for).
    s2 = _session(board, tmp_path / "e2.jsonl")
    out2 = _run(
        s2, resolver, _quiet_monitor(), ["zulu unique", "e 1", "alpha runner", "q"], monkeypatch
    )
    assert [p.player_id for p in s2.picks] == ["RB-ALPHA"]  # pick 1 rewritten Zulu -> Alpha
    assert "Pick 1 updated" in out2
    assert "New seat" not in out2                            # seat prompt is gone (contract)


# --------------------------------------------------------- stray digit at main prompt


def test_stray_digit_at_main_prompt_reprompts_legibly(board, tmp_path, monkeypatch):
    sess = _session(board, tmp_path / "d.jsonl")
    out = _run(sess, NameResolver(board), _quiet_monitor(), ["2", "99", "q"], monkeypatch)
    assert len(sess.picks) == 0
    assert "Traceback" not in out
    assert "Numbers 1-3 only choose" in out
    # and the help no longer misadvertises digits as a top-level command (ux F5)
    assert "1-3 pick from list" not in HELP


# ------------------------------------------------------------- autodraft wiring (state)


def test_autodraft_records_the_legality_aware_suggestion(board, tmp_path, monkeypatch):
    # operator_slot=9 so seat 0 (a RIVAL) is on the clock at overall 1: pressing 'a'
    # must record session.suggest_autodraft (the legality-aware AutodraftBot rule),
    # never the removed legality-blind espn_top_available (state F1 / ux F6).
    sess = _session(board, tmp_path / "ad.jsonl", operator_slot=9)
    seat = sess.current_seat
    assert not sess.is_operator_turn
    expected = sess.suggest_autodraft(seat)  # deterministic on the pre-drive state
    out = _run(sess, NameResolver(board), _quiet_monitor(), ["a", "", "q"], monkeypatch)
    assert "Traceback" not in out
    assert [p.player_id for p in sess.picks] == [expected]


def test_a_on_the_operators_own_turn_refuses(board, tmp_path, monkeypatch):
    # On the operator's own pick, 'a' would bury the engine's VOR/need rec behind a
    # pure-ESPN pick (state NEW-1) — it must refuse and record nothing.
    sess = _session(board, tmp_path / "aop.jsonl", operator_slot=0)
    assert sess.is_operator_turn
    out = _run(sess, NameResolver(board), _quiet_monitor(), ["a", "q"], monkeypatch)
    assert len(sess.picks) == 0
    assert "It's your pick" in out


# --------------------------------------------------------------- final-pick rendering


def test_final_pick_renders_the_finish_once_without_seat_none(board, tmp_path, monkeypatch):
    # Pre-fill every pick but the last directly, then enter the final pick through
    # the app: the finish must render exactly once, with no "seat None" header
    # (ux F2 — the pre-complete turn header used to leak current_seat==None).
    journal = tmp_path / "fin.jsonl"
    sess = _session(board, journal)
    max_pick = DEFAULT_ROSTER.teams * 16  # ROUNDS = 16 -> 160 picks
    for e in board[: max_pick - 1]:
        sess.append_pick(e.player_id)
    final = board[max_pick - 1]

    out = _run(sess, NameResolver(board), _quiet_monitor(), [final.name], monkeypatch)

    assert sess.complete
    assert "seat None" not in out
    assert out.count("Draft complete") == 1
    assert out.count("Your draft is saved to the journal.") == 1


# ------------------------------------------------------- posture banner persistence (ux)


def test_posture_advice_persists_across_picks_until_acknowledged(board, tmp_path, monkeypatch):
    # A fired tip must survive subsequent picks (operator's own AND a rival's) until
    # the operator acts — the old code overwrote `advice` with evaluate()'s
    # deliberately-None return on the very next pick, so it flashed once then
    # vanished, and the monitor latched active forever (ux NEW-1).
    mon = PostureMonitor(margin=8.0, consecutive=1, cooldown=3, evaluator=_loud_eval)
    sess = _session(board, tmp_path / "hold.jsonl", operator_slot=0)
    inputs = [
        "",              # re-render (tip already fired at the opening operator turn)
        "zulu unique",   # the operator's OWN pick — must not wipe the held tip
        "yankee solo",   # a rival pick — still must not wipe it
        "",              # re-render, tip still shown
        "x",             # dismiss -> clears the held tip
        "",              # re-render, now with NO banner
        "q",
    ]
    out = _run(sess, NameResolver(board), mon, inputs, monkeypatch)

    # rendered on many turns, not a single flash (the old bug rendered it once)
    assert out.count("Heads up:") >= 3
    assert "running back" in out.lower()               # the composed tip is real
    # after dismissal the banner is gone from the subsequent re-render
    assert "Heads up:" not in out.split("Tip dismissed.")[-1]


def test_p_accepts_and_x_dismisses_the_posture_tip(board, tmp_path, monkeypatch):
    # p -> accept (clean reset, no cooldown); x -> dismiss (starts the cooldown).
    # accept()/dismiss() are distinct; before the fix both keys called dismiss().
    mon_p = PostureMonitor(margin=8.0, consecutive=1, cooldown=3, evaluator=_loud_eval)
    sess_p = _session(board, tmp_path / "acc.jsonl", operator_slot=0)
    _run(sess_p, NameResolver(board), mon_p, ["p", "q"], monkeypatch)
    assert not mon_p.active
    assert mon_p.cooldown_remaining == 0               # accept: no cooldown

    mon_x = PostureMonitor(margin=8.0, consecutive=1, cooldown=3, evaluator=_loud_eval)
    sess_x = _session(board, tmp_path / "dis.jsonl", operator_slot=0)
    _run(sess_x, NameResolver(board), mon_x, ["x", "q"], monkeypatch)
    assert not mon_x.active
    assert mon_x.cooldown_remaining == 3               # dismiss: cooldown started


def test_posture_evaluated_on_operator_turns_not_after_every_rival_pick(board, tmp_path, monkeypatch):
    # Cadence fix (ux F1): the comparator runs when the clock reaches the operator,
    # not once per entered pick. With operator at seat 0 (overall 1), entering the
    # operator's own pick then several rival picks must NOT re-evaluate.
    calls = {"n": 0}

    def counting(session):
        calls["n"] += 1
        return None

    mon = PostureMonitor(margin=8.0, consecutive=2, cooldown=3, evaluator=counting)
    sess = _session(board, tmp_path / "cad.jsonl", operator_slot=0)
    # operator pick (seat 0), then three rival picks (seats 1-3), then quit
    inputs = ["zulu unique", "yankee solo", "alpha runner", "bravo runner", "q"]
    _run(sess, NameResolver(board), mon, inputs, monkeypatch)
    # exactly one evaluation: the opening operator turn (overall 1). None of the
    # four entered picks brings the clock back to the operator within the window.
    assert calls["n"] == 1


# ------------------------------------------------------- entry-path guards (crash F4)


def test_enter_pick_guards_a_rejected_append(board):
    # append_pick raising ValueError (already-drafted / off-board) must become a
    # one-line message + re-prompt, never a traceback that kills the loop.
    class _BoomSession:
        taken = frozenset()

        def append_pick(self, player_id):
            raise ValueError("player is already drafted")

    class _AutoResolver:
        def resolve(self, query, *, taken=frozenset()):
            return Resolution("auto", (board[0],))

    buf = io.StringIO()
    console = Console(file=buf, width=100, no_color=True, highlight=False)
    ok = _enter_pick(console, _BoomSession(), _AutoResolver(), "alpha runner")
    assert ok is False
    assert "Could not record that pick" in buf.getvalue()


# --------------------------------------------- launch: clobber refusal + resume warnings


def test_launch_refuses_to_clobber_an_existing_journal(board, tmp_path):
    # A fresh launch (no --resume) onto a journal that already holds picks must
    # surface the legible refusal and exit nonzero, leaving the picks intact
    # (the pinned JournalExistsError seam).
    journal = tmp_path / "live.jsonl"
    existing = _session(board, journal)
    existing.append_pick(board[0].player_id)

    buf = io.StringIO()
    console = Console(file=buf, width=100, no_color=True, highlight=False)
    with pytest.raises(SystemExit) as excinfo:
        launch(
            board,
            operator_slot=0,
            pick_order=None,
            season=2026,
            as_of="2026-07-22",
            journal_path=journal,
            resume=False,
            rollouts=1,
            console=console,
        )
    assert excinfo.value.code == 1
    out = buf.getvalue()
    assert "already exists" in out
    assert "--resume" in out
    # the original single pick survived (no clobber)
    assert journal.read_text(encoding="utf-8").count('"kind": "pick"') == 1


def test_launch_prints_resume_warnings(board, tmp_path, monkeypatch):
    # A torn final journal line is tolerated on resume and recorded as a human
    # sentence in session.resume_warnings; launch must print it (crash F2 seam).
    journal = tmp_path / "torn.jsonl"
    seed = _session(board, journal)
    seed.append_pick(board[0].player_id)
    seed.append_pick(board[1].player_id)
    with open(journal, "a", encoding="utf-8") as f:
        f.write('{"kind": "pick", "overall": 3, "seat": 2, "pl')  # truncated, no newline

    # run_app exits immediately (EOF at the first prompt) after the warning prints.
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": (_ for _ in ()).throw(EOFError()),
    )
    buf = io.StringIO()
    console = Console(file=buf, width=100, no_color=True, highlight=False)
    launch(
        board,
        operator_slot=0,
        pick_order=None,
        season=2026,
        as_of="2026-07-22",
        journal_path=journal,
        resume=True,
        rollouts=1,
        console=console,
    )
    out = buf.getvalue()
    assert "partially written" in out
