"""Starting-lineup seater — the permanent, per-week lineup solve (item 3.2).

WHAT THIS IS. Given a set of players, their canonical positions, and THIS
WEEK'S points, seat the best legal starting lineup and report the total, the
slot assignment, and the bench. Everything in-season that asks "what does my
roster actually score" goes through here: marginal valuation (3.2), the waiver
module (3.4), lineup support (3.5).

WHY GREEDY IS CORRECT HERE, and exactly when it stops being correct.
Fill each dedicated slot with that position's highest-scoring available players,
then hand the single FLEX to the best remaining head across the flex positions.
Verified against brute-force enumeration of every legal flex assignment over 300
random 16-man rosters drawn from the live board at random weeks: **0 mismatches**
(item 3.2 recon, 2026-07-24; the brute-force oracle lives in tests/test_lineup.py
and re-runs that comparison every suite run).

That result holds BECAUSE this league has exactly ONE flex slot pooled over
{RB, WR, TE}. A second flex, a superflex (QB-eligible flex), or any slot whose
eligibility set overlaps another's in a second place breaks the exchange argument
and requires a real assignment solve. ``fill_lineup`` raises rather than silently
returning a suboptimal lineup if it is handed such a structure.

LINEAGE (Rule 8). The draft package carries two ancestors of this function —
``draft/board_view.py:_fill_lineup`` and ``draft/simulator.py:optimal_starting_points``
— and this module is written FRESH rather than by rewiring them: ``ziggurat/draft/``
passed a two-rehearsal gate, runs live on draft day with no rollback window, and
"behaviour-preserving" is still a change to code that must work that morning
(operator scope decision, 2026-07-24). The duplication deletes itself with the
package. Nothing here imports from ``ziggurat/draft/`` and nothing there imports
from here.

THE ONE STRUCTURAL DIFFERENCE from the draft ancestors, and it is the whole
point: **points arrive as a parameter, not off a ``house_points`` attribute.**
The draft seater could only ever ask "what does this roster score over a whole
season, with everyone healthy" — season-total, bye-blind, injury-blind. Seating
week by week with that week's own points is what makes bye coverage and
availability COMPUTED rather than approximated by a constant.

DELIBERATELY NOT PORTED: ``draft/engine.py``'s ``_BENCH_VALUE_FRACTION`` and
``_startable_now``. Those approximate, at draft time, exactly what this seater
computes exactly in-season. In-season they are actively wrong: the K/DST entries
are 0.0 ("a second is never worth a roster slot"), which would price every
kicker and defense streaming move at exactly zero marginal value while item 3.5
separately builds a D/ST + K streaming ranker — two modules, contradictory
advice, no error anywhere. Port the question, not the table.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure

# Slot display order. Dedicated skill slots first (that is how ESPN renders the
# roster), then FLEX, then the two streamed slots.
_DEDICATED_ORDER = ("QB", "RB", "WR", "TE")
_TAIL_ORDER = ("DST", "K")
FLEX_LABEL = "FLEX"


class LineupStructureError(ValueError):
    """The roster structure is one this greedy seater is not proven optimal for.

    Raised rather than silently returning a suboptimal lineup — a wrong lineup
    total is invisible to the operator (Rule 6), and every consumer of this
    module treats the number as exact.
    """


@dataclass(frozen=True)
class LineupFill:
    """One week's seated lineup.

    ``slots`` is ordered (slot_label, player_key-or-None); an empty required slot
    is a real, reportable state (bye + injury can empty one), and it contributes
    0 rather than raising. ``starters`` is the set of seated keys — consumers use
    it to answer "would this player have reached the lineup" without re-deriving.
    """

    total: float
    slots: tuple[tuple[str, str | None], ...]
    bench: tuple[str, ...]
    starters: frozenset[str]

    @property
    def empty_slots(self) -> tuple[str, ...]:
        return tuple(label for label, key in self.slots if key is None)


def check_structure(roster: RosterStructure) -> None:
    """Refuse a roster shape greedy is not proven optimal for (see module doc)."""
    if roster.flex_slots > 1:
        raise LineupStructureError(
            f"greedy seating is verified for a SINGLE flex slot; this structure has "
            f"{roster.flex_slots}. A multi-flex or superflex league needs a real "
            "assignment solve (see ziggurat/core/lineup.py module docstring)."
        )
    overlap = roster.flex_positions & {"QB", "DST", "K"}
    if overlap:
        raise LineupStructureError(
            f"greedy seating is verified for a flex pooled over RB/WR/TE; this "
            f"structure also pools {sorted(overlap)}."
        )


def fill_lineup(
    players: Iterable[str],
    positions: Mapping[str, str],
    points: Mapping[str, float],
    *,
    roster: RosterStructure = DEFAULT_ROSTER,
    available: Mapping[str, bool] | None = None,
) -> LineupFill:
    """Seat the best legal starting lineup for ONE week.

    ``players`` are opaque player keys; ``positions`` maps each to a canonical
    position (QB/RB/WR/TE/DST/K); ``points`` maps each to THIS WEEK'S house
    points (a missing key scores 0.0 — a player with no projection row for the
    week is worth nothing that week, which is exactly what a bye row is).
    ``available`` maps a key to False to bench him outright (bye, ruled OUT);
    a missing key means available.

    Unavailable players are not seated and are not counted as bench.
    """
    check_structure(roster)
    keys = list(players)

    by_pos: dict[str, list[str]] = {}
    for key in keys:
        if available is not None and not available.get(key, True):
            continue
        pos = positions.get(key)
        if pos is None:
            continue
        by_pos.setdefault(pos, []).append(key)
    # Deterministic order: points desc, then key — two identical projections must
    # never seat differently between runs (the tie band is real; see marginal.py).
    for lst in by_pos.values():
        lst.sort(key=lambda k: (-points.get(k, 0.0), k))

    used: dict[str, int] = dict.fromkeys(by_pos, 0)

    def peek(pos: str | None) -> str | None:
        """The best unseated player at ``pos``, or None — including None when his
        projection is NEGATIVE: an empty slot scores 0, and 0 beats a negative
        starter. (A D/ST really can bracket below zero.)"""
        if pos is None:
            return None
        lst = by_pos.get(pos, ())
        i = used.get(pos, 0)
        if i >= len(lst):
            return None
        return lst[i] if points.get(lst[i], 0.0) >= 0.0 else None

    slots: list[tuple[str, str | None]] = []
    total = 0.0

    def seat(label: str, pos: str | None) -> None:
        nonlocal total
        key = peek(pos)
        if key is not None:
            used[pos] = used.get(pos, 0) + 1
            total += points.get(key, 0.0)
        slots.append((label, key))

    for pos in _DEDICATED_ORDER:
        req = roster.starters.get(pos, 0)
        for i in range(req):
            seat(pos if req == 1 else f"{pos}{i + 1}", pos)

    for _ in range(roster.flex_slots):
        best_pos, best_pts = None, None
        for pos in roster.flex_positions:
            head = peek(pos)
            if head is None:
                continue
            pts = points.get(head, 0.0)
            if best_pts is None or pts > best_pts:
                best_pos, best_pts = pos, pts
        seat(FLEX_LABEL, best_pos)

    for pos in _TAIL_ORDER:
        req = roster.starters.get(pos, 0)
        for i in range(req):
            seat(pos if req == 1 else f"{pos}{i + 1}", pos)

    starters = frozenset(key for _label, key in slots if key is not None)
    bench: list[str] = []
    for pos, lst in by_pos.items():
        bench.extend(lst[used.get(pos, 0):])
    bench.sort(key=lambda k: (-points.get(k, 0.0), k))
    return LineupFill(
        total=total, slots=tuple(slots), bench=tuple(bench), starters=starters
    )


def lineup_total(
    players: Iterable[str],
    positions: Mapping[str, str],
    points: Mapping[str, float],
    *,
    roster: RosterStructure = DEFAULT_ROSTER,
    available: Mapping[str, bool] | None = None,
) -> float:
    """Just the seated total (the hot path in a swap scan)."""
    return fill_lineup(
        players, positions, points, roster=roster, available=available
    ).total


def format_lineup(fill: LineupFill, *, names: Mapping[str, str] | None = None) -> str:
    """One-week lineup card (display only)."""
    lines = []
    for label, key in fill.slots:
        who = "(empty)" if key is None else (names or {}).get(key, key)
        lines.append(f"  {label:<5} {who}")
    lines.append(f"  {'TOTAL':<5} {fill.total:.1f}")
    return "\n".join(lines)


def active_players(roster_rows: Sequence[Mapping]) -> list[Mapping]:
    """Drop IR-slotted rows: an IR player is neither startable nor occupying one
    of the 16 active slots (item 3.4's legality precheck depends on that count
    being right). Item 3.2 §7.7."""
    return [r for r in roster_rows if str(r.get("lineup_slot") or "").upper() != "IR"]
