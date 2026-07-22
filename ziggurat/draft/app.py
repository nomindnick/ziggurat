"""Item 2.4 — the draft-board TUI edge: the ONLY module with terminal I/O.

DELETABLE package (Rule 8). This is a blocking ``input()`` read/render loop over
a **headless** :class:`~ziggurat.draft.session.DraftSession` (all state, scoring,
survival, journaling live there) and the pure renderers in ``board_view``. app.py
holds no draft state and does no football math — it translates keystrokes into
session/resolver/posture calls and prints the returned renderables (recon: the
headless controller owns everything; the render layer stays thin so a later
framework flip is contained).

Keyboard grammar (also shown on-screen as the help line):

  * type part of a name + Enter -> record the current pick (all 10 seats are
    entered so board state stays truthful). On a tie, a numbered confirm panel
    appears — press 1-3. A bare Enter just re-renders (never auto-commits).
  * u        undo the last pick
  * e <N>    edit an earlier pick N (new player and/or new seat)
  * t        tiers / value-cliff view          r   the room's ESPN board
  * m        my roster + open needs            c   if-this-then-that plans
  * a        autodraft suggestion for a RIVAL seat on the clock (you still
             confirm; on YOUR own pick, act on the recommendation panel instead)
  * p / x    accept / dismiss the posture tip
  * q        quit (the journal is saved; resume with --resume)

The number keys 1-3 only choose inside the confirm panel (after a name search) —
they are not a top-level command. After every confirmed pick the controller
recomputes synchronously (the next-turn recommendation is already on screen when
the clock reaches the operator) and re-reads the live room recalibration; the
posture tip is re-evaluated only when the clock reaches the operator (a snake-turn
checkpoint), and a fired tip is held on screen until the operator acts on it (p/x).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.draft.board_view import (
    player_label,
    render_autodraft_suggestion,
    render_confirm,
    render_contingencies,
    render_espn_view,
    render_recommendation,
    render_roster,
    render_status,
    render_tiers,
)
from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.posture import PostureMonitor
from ziggurat.draft.resolver import NameResolver
from ziggurat.draft.session import DraftSession, JournalExistsError

HELP = (
    "type a name+Enter to record a pick (numbers 1-3 choose in the confirm panel) · "
    "u undo · e N edit · t tiers · r room · m roster · c plans · a auto · "
    "p accept tip · x dismiss tip · q quit"
)


# ---------------------------------------------------------------- small helpers


def _board_of(session: object) -> Sequence[BoardEntry]:
    """The in-memory board the headless controller holds (for the tier/room views).

    The session builds a fresh PickContext over this board every recompute, so it
    necessarily owns it. Integrator note: if DraftSession exposes the board under a
    name other than ``board``, reconcile here — the views degrade to empty, never
    crash, if it is missing."""
    board = getattr(session, "board", None)
    return board if board is not None else ()


def _safe_recal(session: object) -> object | None:
    try:
        return session.recalibration()  # type: ignore[attr-defined]
    except Exception:
        return None


def _safe_recs(session: object) -> Sequence[object]:
    try:
        return session.recommend()  # type: ignore[attr-defined]
    except Exception:
        return ()


def _safe_posture(posture: object, session: object) -> object | None:
    """Evaluate the posture monitor, never letting a comparator error kill the loop."""
    try:
        return posture.evaluate(session)  # type: ignore[attr-defined]
    except Exception:
        return None


def _entry_by_id(session: object, player_id: str) -> BoardEntry | None:
    """The board entry for a ``player_id`` off the session's in-memory board."""
    for e in _board_of(session):
        if e.player_id == player_id:
            return e
    return None


def _recompute(
    session: object, posture: object, advice: object | None
) -> tuple[object | None, object | None]:
    """Refresh the room-recalibration line after a state change and, at the recon
    snake-turn cadence ONLY (when the clock has reached the operator), advance the
    posture monitor. A newly-fired tip replaces ``advice``; a quiet evaluation
    (``None``) leaves the last fired tip **held** — it is cleared solely by p/x, so
    a fired banner never flashes for a single render (recon §ux NEW-1) and the
    comparator does not run after every rival pick (recon §ux F1)."""
    recal = _safe_recal(session)
    if getattr(session, "is_operator_turn", False):
        fired = _safe_posture(posture, session)
        if fired is not None:
            advice = fired
    return recal, advice


def _prompt(session: object) -> str:
    seat = getattr(session, "current_seat", "?")
    mark = " (YOU)" if getattr(session, "is_operator_turn", False) else ""
    pick = getattr(session, "overall_pick", "?")
    return f"pick {pick} · seat {seat}{mark} > "


def _turn_header(session: object) -> str:
    pick = getattr(session, "overall_pick", "?")
    seat = getattr(session, "current_seat", "?")
    mark = "  — YOUR PICK" if getattr(session, "is_operator_turn", False) else ""
    return f"Pick {pick} · seat {seat} on the clock{mark}"


def _input(console: Console, prompt: str) -> str | None:
    """A guarded ``input``: returns None on EOF/Ctrl-C (piped or interrupted)."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        console.print()
        return None


# ------------------------------------------------------------------ rendering


def _render_turn(console: Console, session: object, recal: object, advice: object) -> None:
    # A completed draft is rendered exactly once, by the loop's post-exit
    # _render_final — never here. Returning early avoids both the double finish
    # render and the bogus "seat None on the clock" header (current_seat is None
    # when complete) that the pre-complete turn header would otherwise leak to a
    # novice (recon §ux F2).
    if getattr(session, "complete", False):
        return
    console.rule(_turn_header(session))
    console.print(render_status(recal, advice))
    if getattr(session, "is_operator_turn", False):
        console.print(render_recommendation(_safe_recs(session)))
    else:
        seat = getattr(session, "current_seat", "?")
        console.print(
            Text(
                f"On the clock: seat {seat}. Enter their pick (type part of a name).",
                style="cyan",
            )
        )
    console.print(Text(HELP, style="dim"))


def _render_final(console: Console, session: object) -> None:
    console.rule("Draft complete")
    console.print(render_roster(session))
    console.print(Text("Your draft is saved to the journal.", style="green"))


# ------------------------------------------------------------------ actions


def _resolve_pick(
    console: Console, resolver: object, query: str, taken: object
) -> BoardEntry | None:
    """Resolve a name fragment to one board entry, showing the confirm panel on a
    tie. Returns None (with a message) on empty/no-match/cancel."""
    res = resolver.resolve(query, taken=taken)  # type: ignore[attr-defined]
    kind = getattr(res, "kind", "none")
    candidates = tuple(getattr(res, "candidates", ()))
    if kind == "empty":
        console.print(Text("Type at least part of a name.", style="dim"))
        return None
    if kind in ("none", "") or not candidates:
        console.print(Text(f"No match for '{query}'. Try more of the name.", style="red"))
        return None
    if kind == "auto":
        return candidates[0]
    console.print(render_confirm(candidates))
    choice = _input(console, "Pick number (1-3, blank to cancel): ")
    if choice is None or not choice.strip().isdigit():
        console.print(Text("Cancelled.", style="dim"))
        return None
    idx = int(choice.strip()) - 1
    if not 0 <= idx < len(candidates):
        console.print(Text("Cancelled.", style="dim"))
        return None
    return candidates[idx]


def _enter_pick(console: Console, session: object, resolver: object, query: str) -> bool:
    entry = _resolve_pick(console, resolver, query, getattr(session, "taken", frozenset()))
    if entry is None:
        return False
    try:
        session.append_pick(entry.player_id)  # type: ignore[attr-defined]
    except ValueError as exc:  # already-drafted / off-board -> one line, re-prompt
        console.print(Text(f"Could not record that pick ({exc}). Try another.", style="red"))
        return False
    console.print(Text(f"Recorded: {player_label(entry)}", style="green"))
    return True


def _undo(console: Console, session: object) -> None:
    try:
        session.undo_last()  # type: ignore[attr-defined]
        console.print(Text("Last pick undone.", style="green"))
    except Exception as exc:  # nothing to undo / replay error — never crash the loop
        console.print(Text(f"Nothing to undo ({exc}).", style="red"))


def _edit(console: Console, session: object, resolver: object, cmd: str) -> bool:
    """Correct an earlier pick's PLAYER. The seat is snake-derived and not editable
    (session.edit_pick dropped its seat parameter — audit state F2), so this only
    swaps the player at ``overall``."""
    parts = cmd.split()
    if len(parts) < 2 or not parts[1].isdigit():
        console.print(Text("Usage: e <pick number>", style="red"))
        return False
    overall = int(parts[1])
    q = _input(console, f"New player for pick {overall} (blank to keep): ")
    if q is None or not q.strip():
        console.print(Text("No change.", style="dim"))
        return False
    entry = _resolve_pick(console, resolver, q.strip(), getattr(session, "taken", frozenset()))
    if entry is None:
        return False
    try:
        session.edit_pick(overall, player_id=entry.player_id)  # type: ignore[attr-defined]
    except Exception as exc:
        console.print(Text(f"Could not edit pick {overall} ({exc}).", style="red"))
        return False
    console.print(Text(f"Pick {overall} updated.", style="green"))
    return True


def _autodraft_suggest(console: Console, session: object) -> bool:
    """Propose the legality-aware AutodraftBot pick for the RIVAL seat on the clock.

    Routes through ``session.suggest_autodraft`` (the AutodraftBot rule: best
    ESPN-ranked player that keeps the seat's lineup legal — so it proposes K/DST in
    the window rounds), NOT the legality-blind ESPN-top-available (recon §state F1 /
    §ux F6). The operator still confirms; a rival's pick is never self-entered."""
    seat = getattr(session, "current_seat", None)
    try:
        pid = session.suggest_autodraft(seat)  # type: ignore[attr-defined]
    except Exception as exc:
        console.print(Text(f"No autodraft suggestion available ({exc}).", style="red"))
        return False
    entry = _entry_by_id(session, pid)
    if entry is None:
        console.print(Text("No available player to suggest.", style="red"))
        return False
    console.print(render_autodraft_suggestion(entry, seat))
    ok = _input(console, "Enter to confirm, any key to cancel: ")
    if ok is None or ok.strip() != "":
        console.print(Text("Cancelled.", style="dim"))
        return False
    try:
        session.append_pick(pid)  # type: ignore[attr-defined]
    except ValueError as exc:
        console.print(Text(f"Could not record that pick ({exc}). Try another.", style="red"))
        return False
    console.print(Text(f"Recorded: {player_label(entry)}", style="green"))
    return True


def _announce_quit(console: Console, session: object) -> None:
    journal = getattr(session, "journal_path", None)
    where = f" ({journal})" if journal else ""
    console.print(
        Text(
            f"Draft saved to the journal{where}. Resume any time with --resume.",
            style="green",
        )
    )


# --------------------------------------------------------------------- the loop


def run_app(
    session: DraftSession,
    resolver: NameResolver,
    console: Console,
    posture: PostureMonitor,
) -> None:
    """Drive the headless session with a blocking read/render loop until the draft
    is complete or the operator quits. The ONLY function with terminal I/O."""
    advice: object | None = None
    recal, advice = _recompute(session, posture, advice)
    _render_turn(console, session, recal, advice)

    while not getattr(session, "complete", False):
        raw = _input(console, _prompt(session))
        if raw is None:  # EOF / Ctrl-C -> save and leave
            _announce_quit(console, session)
            return
        cmd = raw.strip()
        low = cmd.lower()

        if low == "":
            recal = _safe_recal(session)
            _render_turn(console, session, recal, advice)
            continue
        if low == "q":
            _announce_quit(console, session)
            return
        if low == "u":
            _undo(console, session)
            recal, advice = _recompute(session, posture, advice)
            _render_turn(console, session, recal, advice)
            continue
        if low == "t":
            console.print(render_tiers(_board_of(session), getattr(session, "taken", frozenset())))
            continue
        if low == "r":
            console.print(render_espn_view(_board_of(session), getattr(session, "taken", frozenset())))
            continue
        if low == "m":
            console.print(render_roster(session))
            continue
        if low == "c":
            try:
                branches = session.contingencies()
            except Exception:
                branches = ()
            console.print(render_contingencies(branches))
            continue
        if low == "p":
            # Accept: the operator adopts the lean -> clean reset, no cooldown
            # (distinct from dismiss, which starts a suppression cooldown).
            if advice is not None:
                console.print(Text("Noted — keep it in mind as you draft.", style="green"))
                posture.accept()
                advice = None
            else:
                console.print(Text("No tip to accept right now.", style="dim"))
            continue
        if low == "x":
            posture.dismiss()
            advice = None
            console.print(Text("Tip dismissed.", style="dim"))
            continue
        if low == "a":
            # Autodraft is for filling in a RIVAL's pick. On the operator's own turn
            # it would bury the engine's VOR/need recommendation behind a pure-ESPN
            # pick (recon §state NEW-1) — refuse and point at the panel.
            if getattr(session, "is_operator_turn", False):
                console.print(
                    Text(
                        "It's your pick — use the recommendation panel above (or type a "
                        "name); autodraft is only for filling in a rival's pick.",
                        style="yellow",
                    )
                )
                continue
            if _autodraft_suggest(console, session):
                recal, advice = _recompute(session, posture, advice)
                _render_turn(console, session, recal, advice)
            continue
        if low.startswith("e "):
            # "e <N>" is the edit command; "e <name>" (e.g. "e goedert") is a
            # first-initial name search and must NOT be swallowed as an edit
            # (recon §ux F3). Treat it as an edit only when the argument is a number.
            arg = cmd[2:].strip()
            first_tok = arg.split()[0] if arg else ""
            if first_tok.isdigit():
                if _edit(console, session, resolver, cmd):
                    recal, advice = _recompute(session, posture, advice)
                    _render_turn(console, session, recal, advice)
                continue
            # else: fall through — the whole input is a name query for the resolver.
        if cmd.isdigit():
            # A digit at the MAIN prompt is not a command — numbers only choose
            # inside the confirm panel after a name search (recon §ux F5).
            console.print(
                Text(
                    "Numbers 1-3 only choose inside the confirm panel — type part of a "
                    "player's name to search for a pick.",
                    style="dim",
                )
            )
            continue

        # Anything else is a name fragment for the pick on the clock.
        if _enter_pick(console, session, resolver, cmd):
            recal, advice = _recompute(session, posture, advice)
            _render_turn(console, session, recal, advice)

    _render_final(console, session)


# --------------------------------------------------------------- CLI entry point


def launch(
    board: Sequence[BoardEntry],
    *,
    operator_slot: int,
    pick_order: Sequence[int] | None,
    season: int,
    as_of: str,
    journal_path: Path,
    resume: bool = False,
    rollouts: int = 512,
    seed: int = 42,
    roster: RosterStructure = DEFAULT_ROSTER,
    console: Console | None = None,
) -> None:
    """Wire the headless controller, resolver, posture monitor, and console, then
    run the loop. The thin ``draft-board`` CLI command calls this after loading the
    board (Rule 3 keeps the command itself logic-free)."""
    console = console or Console()
    order = list(pick_order) if pick_order is not None else list(range(roster.teams))
    try:
        if resume:
            session = DraftSession.resume(journal_path, board)
        else:
            session = DraftSession.start(
                board,
                operator_slot=operator_slot,
                pick_order=order,
                season=season,
                as_of=as_of,
                journal_path=journal_path,
                roster=roster,
                session_seed=seed,
                rollouts=rollouts,
            )
    except JournalExistsError as exc:
        # The refuse-to-clobber footgun guard (session.start opens the journal
        # O_EXCL). Surface the novice-legible message + the recovery hint and exit
        # nonzero rather than proceeding on a half-built session (pinned seam).
        console.print(Text(str(exc), style="red"))
        console.print(
            Text(
                "To continue that draft, re-run with --resume; to start a brand-new "
                "one, pass a different --journal path.",
                style="yellow",
            )
        )
        raise SystemExit(1) from exc

    # A tolerated torn-tail recovery records human sentences the operator must see
    # (recon §crash F2) — print each before the board comes up.
    for line in getattr(session, "resume_warnings", ()) or ():
        console.print(Text(line, style="yellow"))

    resolver = NameResolver(board)
    posture = PostureMonitor(margin=8.0, consecutive=2, cooldown=3)
    run_app(session, resolver, console, posture)
