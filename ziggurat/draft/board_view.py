"""Item 2.4 — pure Rich renderables for the draft-board TUI.

DELETABLE package (Rule 8). This module is the render layer: **data in ->
Rich renderable out**. It constructs NO ``Console`` and prints NOTHING — the
edge module ``app.py`` owns all terminal I/O. Every function here is snapshot-
testable by pointing a ``Console(file=StringIO())`` at its output.

Rule 6 (explainability) is load-bearing here: :func:`render_recommendation`
renders ``PickRec.reasons`` **VERBATIM** — it never paraphrases the engine's
novice-legible sentences. The other views translate board numbers into a shape
a football novice can glance at (tiers with a value-cliff warning, the room's
ESPN board with a house-value delta, the operator's own lineup vs open needs,
and the honesty line about how much the room model has adapted).

Width discipline: nothing forces a fixed pixel/character width, so Rich re-flows
tables and panels to whatever the console reports — a 60-column terminal wraps
gracefully rather than crashing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from statistics import median
from typing import TYPE_CHECKING

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure
from ziggurat.draft.bots import BoardEntry, needed_positions, position_counts
from ziggurat.draft.survival import LIVE_RECAL_MIN_PICKS

if TYPE_CHECKING:  # pragma: no cover - typing only (never imported at runtime)
    from ziggurat.draft.engine import PickRec

# Positions in the order a novice reads a lineup. Mirrors bots.POSITIONS.
POSITION_ORDER = ("QB", "RB", "WR", "TE", "DST", "K")

# Players the ESPN board never ranks sit at rank >= this sentinel (shared with
# simulator._FALLBACK_BASE / survival._FALLBACK_RANK_BASE). Never phrase a board
# position from it, and drop them from the room's-screen view.
_UNRANKED_BASE = 10_000

# Tier-gap heuristic (recon: "a simple gap heuristic is fine"). A break starts a
# new tier when the VOR drop to the next player is at least this many points AND
# at least ``_TIER_GAP_FACTOR`` times the typical gap so far.
_TIER_MIN_GAP = 8.0
_TIER_GAP_FACTOR = 1.8

# How divergent the two boards must be before the room's-screen view flags it.
_DIVERGENCE_HINT = 15


# --------------------------------------------------------------------- labels


def player_label(entry: BoardEntry) -> str:
    """``"Alpha Runner — RB BUF (ESPN #4)"`` — a one-line novice-legible name."""
    name = entry.name or entry.player_id
    team = f" {entry.team}" if entry.team else ""
    rank = f" (ESPN #{entry.espn_overall_rank})" if entry.espn_overall_rank < _UNRANKED_BASE else ""
    return f"{name} — {entry.position}{team}{rank}"


def _display_name(entry: BoardEntry) -> str:
    return entry.name or entry.player_id


# ------------------------------------------------------------ recommendation


def render_recommendation(recs: Sequence["PickRec"]) -> RenderableType:
    """The on-the-clock panel: the top pick with its reasons rendered VERBATIM
    (Rule 6 — never reworded) plus a compact table of the next-best options."""
    if not recs:
        return Panel(
            Text(
                "No recommendation available — the board may be exhausted, or it is "
                "not your pick yet.",
                style="dim",
            ),
            title="Your pick",
            border_style="yellow",
        )

    top = recs[0]
    body: list[RenderableType] = [
        Text(player_label(top.player), style="bold green"),
        Text(
            f"value (VOR) {top.vor:.0f}   ·   pick score {top.pick_score:.0f}   ·   "
            f"stays to your next pick: {round(top.survival_next * 100)}%",
            style="dim",
        ),
        Text(""),
        Text("Why this pick:", style="bold"),
    ]
    # Reasons VERBATIM — one bullet per engine sentence, no rewording.
    for reason in top.reasons:
        body.append(Text(f"  • {reason}"))

    panel = Panel(
        Group(*body),
        title="Your pick — top recommendation",
        border_style="green",
        box=box.ROUNDED,
    )

    if len(recs) == 1:
        return panel

    why_not = {name: note for name, note in top.alternatives}
    table = Table(
        title="Other options",
        box=box.SIMPLE_HEAD,
        expand=False,
        pad_edge=False,
    )
    table.add_column("Player", overflow="fold")
    table.add_column("Pos")
    table.add_column("Team")
    table.add_column("VOR", justify="right")
    table.add_column("Stays%", justify="right")
    table.add_column("Why not now", overflow="fold")
    for alt in recs[1:]:
        note = why_not.get(alt.name or alt.player_id, "")
        table.add_row(
            _display_name(alt.player),
            alt.position,
            alt.player.team or "",
            f"{alt.vor:.0f}",
            f"{round(alt.survival_next * 100)}%",
            note,
        )
    return Group(panel, table)


# --------------------------------------------------------------------- tiers


def _tier_break_indices(vors: Sequence[float]) -> set[int]:
    """Indices where a new tier STARTS (a big VOR gap opened just above it)."""
    if len(vors) < 2:
        return set()
    gaps = [vors[i] - vors[i + 1] for i in range(len(vors) - 1)]
    positive = [g for g in gaps if g > 0]
    typical = median(positive) if positive else 0.0
    threshold = max(_TIER_MIN_GAP, _TIER_GAP_FACTOR * typical)
    return {i + 1 for i, g in enumerate(gaps) if g >= threshold}


def _cliff_line(pos: str, entries: Sequence[BoardEntry], breaks: set[int]) -> Text:
    """The value-cliff warning: ``"3 startable RBs left before a 24-point drop"``."""
    if not breaks:
        return Text(
            f"{pos}: no clear value cliff in the top {len(entries)} — this position is deep",
            style="green",
        )
    first = min(breaks)
    drop = entries[first - 1].vor - entries[first].vor
    plural = "s" if first != 1 else ""
    return Text(
        f"{pos}: {first} startable {pos}{plural} left before a {drop:.0f}-point drop",
        style="bold yellow",
    )


def render_tiers(
    board: Sequence[BoardEntry], taken: Iterable[str], *, per_pos: int = 8
) -> RenderableType:
    """Per-position VOR tiers over the STILL-AVAILABLE players, each with a
    value-cliff warning. Taken players are dropped from the live board (marked
    out by removal), so every count is "what is actually left"."""
    taken_set = set(taken)
    blocks: list[RenderableType] = []
    for pos in POSITION_ORDER:
        avail = sorted(
            (
                e
                for e in board
                if e.position == pos and e.name and e.player_id not in taken_set
            ),
            key=lambda e: -e.vor,
        )
        if not avail:
            blocks.append(Text(f"{pos}: none left on the board", style="dim"))
            continue
        top = avail[:per_pos]
        breaks = _tier_break_indices([e.vor for e in top])
        table = Table(box=box.SIMPLE, expand=False, pad_edge=False, show_header=True)
        table.add_column(pos, justify="right")
        table.add_column("Player", overflow="fold")
        table.add_column("Team")
        table.add_column("VOR", justify="right")
        for i, e in enumerate(top):
            if i in breaks:
                table.add_section()
            table.add_row(
                str(i + 1), _display_name(e), e.team or "", f"{e.vor:.0f}"
            )
        blocks.append(Group(_cliff_line(pos, top, breaks), table))
        blocks.append(Text(""))
    return Group(*blocks)


# ------------------------------------------------------- the room's ESPN board


def render_espn_view(
    board: Sequence[BoardEntry], taken: Iterable[str], *, top: int = 40
) -> RenderableType:
    """The room's own screen: players in ESPN-board order, drafted ones struck
    out, with a house-VOR-rank-vs-ESPN-rank delta so the operator can see where
    their scoring diverges from the room (the K/DST + house-bracket edge)."""
    taken_set = set(taken)
    named = [e for e in board if e.name]
    house_rank = {
        e.player_id: i + 1
        for i, e in enumerate(sorted(named, key=lambda e: -e.vor))
    }
    shown = sorted(
        (e for e in named if e.espn_overall_rank < _UNRANKED_BASE),
        key=lambda e: e.espn_overall_rank,
    )[:top]

    table = Table(
        title="The room's board (ESPN order) — × = already drafted",
        box=box.SIMPLE_HEAD,
        expand=False,
        pad_edge=False,
    )
    table.add_column("", justify="center")
    table.add_column("ESPN#", justify="right")
    table.add_column("Player", overflow="fold")
    table.add_column("Pos")
    table.add_column("Team")
    table.add_column("Your#", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("Read", overflow="fold")
    for e in shown:
        hr = house_rank.get(e.player_id)
        delta = e.espn_overall_rank - hr if hr is not None else None
        if delta is None:
            delta_txt, read = "", ""
        elif delta >= _DIVERGENCE_HINT:
            delta_txt, read = f"+{delta}", "the room sleeps on him"
        elif delta <= -_DIVERGENCE_HINT:
            delta_txt, read = str(delta), "the room over-drafts him"
        else:
            delta_txt, read = f"{delta:+d}", ""
        is_taken = e.player_id in taken_set
        table.add_row(
            "×" if is_taken else "",
            str(e.espn_overall_rank),
            _display_name(e),
            e.position,
            e.team or "",
            str(hr) if hr is not None else "",
            delta_txt,
            read,
            style="strike dim" if is_taken else None,
        )
    return table


# --------------------------------------------------------------- own roster


def _fill_lineup(
    own: Sequence[BoardEntry], roster: RosterStructure
) -> tuple[list[tuple[str, BoardEntry | None]], list[BoardEntry]]:
    """Greedily seat the drafted players into starter slots (best points first),
    then FLEX, then DST/K; leftovers are bench. Returns (slots, bench)."""
    by_pos: dict[str, list[BoardEntry]] = {}
    for e in own:
        by_pos.setdefault(e.position, []).append(e)
    for lst in by_pos.values():
        lst.sort(key=lambda e: -e.house_points)
    used: dict[str, int] = {p: 0 for p in by_pos}

    def take(pos: str | None) -> BoardEntry | None:
        if pos is None:
            return None
        lst = by_pos.get(pos, [])
        i = used.get(pos, 0)
        if i < len(lst):
            used[pos] = i + 1
            return lst[i]
        return None

    slots: list[tuple[str, BoardEntry | None]] = []
    for pos in ("QB", "RB", "WR", "TE"):
        req = roster.starters.get(pos, 0)
        for k in range(req):
            label = pos if req == 1 else f"{pos}{k + 1}"
            slots.append((label, take(pos)))
    for _ in range(roster.flex_slots):
        best_pts: float | None = None
        best_pos: str | None = None
        for pos in roster.flex_positions:
            lst = by_pos.get(pos, [])
            i = used.get(pos, 0)
            if i < len(lst) and (best_pts is None or lst[i].house_points > best_pts):
                best_pts, best_pos = lst[i].house_points, pos
        slots.append(("FLEX", take(best_pos)))
    for pos in ("DST", "K"):
        req = roster.starters.get(pos, 0)
        for k in range(req):
            label = pos if req == 1 else f"{pos}{k + 1}"
            slots.append((label, take(pos)))

    bench: list[BoardEntry] = []
    for pos, lst in by_pos.items():
        bench.extend(lst[used.get(pos, 0):])
    bench.sort(key=lambda e: -e.house_points)
    return slots, bench


def render_roster(session: object) -> RenderableType:
    """The operator's own team by starter slot vs open needs. Glanceable: filled
    starters, open holes called out, and a bench-depth tail. Duck-types
    ``session`` (``own_roster`` + optional ``roster``) — no import of session.py."""
    own = tuple(getattr(session, "own_roster", ()) or ())
    roster: RosterStructure = getattr(session, "roster", DEFAULT_ROSTER)
    slots, bench = _fill_lineup(own, roster)

    table = Table(
        title="Your team", box=box.SIMPLE_HEAD, expand=False, pad_edge=False
    )
    table.add_column("Slot")
    table.add_column("Player", overflow="fold")
    table.add_column("Pos")
    table.add_column("Team")
    table.add_column("Pts", justify="right")
    for label, entry in slots:
        if entry is None:
            table.add_row(label, "— open —", "", "", "", style="dim")
        else:
            table.add_row(
                label,
                _display_name(entry),
                entry.position,
                entry.team or "",
                f"{entry.house_points:.0f}",
            )

    open_needs = [label for label, entry in slots if entry is None]
    counts = position_counts(own)
    still = needed_positions(counts, roster)
    footer_lines: list[RenderableType] = []
    if open_needs:
        footer_lines.append(
            Text(
                "Still need to fill: " + ", ".join(open_needs),
                style="yellow",
            )
        )
        if still:
            footer_lines.append(
                Text(
                    "Positions that can still fill a starter: "
                    + ", ".join(sorted(still)),
                    style="dim",
                )
            )
    else:
        footer_lines.append(
            Text("Starting lineup is set — draft for depth and upside.", style="green")
        )
    footer_lines.append(Text(f"Bench depth: {len(bench)} player(s).", style="dim"))
    return Group(table, *footer_lines)


# ------------------------------------------------------------- status / honesty


def _recal_line(recal: object) -> str:
    """The Rule-6 honesty line about how far the room model has adapted.

    Never claims a phantom recalibration: below the pick threshold (or on a
    degenerate spread) it SAYS it is still on the 2025 baseline."""
    if recal is None:
        return "Room model: 2025 baseline."
    engaged = bool(getattr(recal, "engaged", False))
    n = int(getattr(recal, "n_room_picks", 0) or 0)
    if engaged:
        sigma = getattr(recal, "reach_sigma", None)
        tail = f", reach spread about {sigma:.0f} board slots" if sigma else ""
        return f"Room model: adapted from {n} live room pick(s){tail}."
    need = max(0, LIVE_RECAL_MIN_PICKS - n)
    if need > 0:
        return (
            f"Room model: 2025 baseline — need {need} more room pick(s) before it "
            f"adapts to this room."
        )
    return (
        "Room model: 2025 baseline — the room's picks so far do not yet give a "
        "stable read, so it holds the baseline."
    )


def render_status(recal: object, advice: object) -> RenderableType:
    """The honesty line about the room model, plus the current posture tip (if
    any) as a single novice-legible sentence. Duck-types both inputs."""
    lines: list[RenderableType] = [Text(_recal_line(recal), style="dim")]
    if advice is not None:
        message = getattr(advice, "message", None) or str(advice)
        lines.append(Text(f"Heads up: {message}", style="bold yellow"))
    return Group(*lines)


# --------------------------------------------------- small edge helpers (app.py)


def render_confirm(candidates: Sequence[BoardEntry]) -> RenderableType:
    """The disambiguation panel: number the near-matches so one keystroke picks."""
    table = Table(box=box.SIMPLE_HEAD, expand=False, pad_edge=False)
    table.add_column("#", justify="right")
    table.add_column("Player", overflow="fold")
    table.add_column("Pos")
    table.add_column("Team")
    table.add_column("ESPN#", justify="right")
    table.add_column("VOR", justify="right")
    for i, e in enumerate(candidates, start=1):
        rank = str(e.espn_overall_rank) if e.espn_overall_rank < _UNRANKED_BASE else "—"
        table.add_row(str(i), _display_name(e), e.position, e.team or "", rank, f"{e.vor:.0f}")
    return Panel(table, title="Which player did you mean?", border_style="cyan")


def render_autodraft_suggestion(entry: BoardEntry, seat: object) -> RenderableType:
    """One-line 'the room would likely take X' proposal the operator confirms."""
    return Panel(
        Text(
            f"Seat {seat} would most likely take (ESPN board): {player_label(entry)}",
            style="cyan",
        ),
        title="Autodraft suggestion",
        border_style="cyan",
    )


def render_contingencies(contingencies: Sequence[object]) -> RenderableType:
    """'If X is gone, then Y' branch plans for the operator's upcoming pick.

    Duck-typed: each branch's shape is owned by session.py, so this reads a few
    likely fields and falls back to ``str`` rather than assuming a schema."""
    if not contingencies:
        return Panel(
            Text(
                "No branch plans yet — you are not close enough to your pick, or the "
                "board is thin.",
                style="dim",
            ),
            title="If-this-then-that plans",
            border_style="blue",
        )
    lines: list[RenderableType] = []
    for c in contingencies:
        # session.py's Contingency carries a ready, novice-legible one-sentence
        # ``.message`` ("If you take X now, Y is the likely best value waiting …") —
        # prefer it verbatim (Rule 6). The duck-typed fallbacks below cover any other
        # branch shape without assuming session.py's schema.
        message = getattr(c, "message", None)
        if isinstance(message, str) and message:
            lines.append(Text(f"  • {message}"))
            continue
        trigger = (
            getattr(c, "if_gone", None)
            or getattr(c, "trigger", None)
            or getattr(c, "gone", None)
        )
        then = (
            getattr(c, "then", None)
            or getattr(c, "pick", None)
            or getattr(c, "recommendation", None)
        )
        if trigger is not None and then is not None:
            lines.append(Text(f"  • If {trigger} is gone → take {then}"))
        else:
            summary = getattr(c, "summary", None) or getattr(c, "label", None) or str(c)
            lines.append(Text(f"  • {summary}"))
    return Panel(Group(*lines), title="If-this-then-that plans", border_style="blue")
