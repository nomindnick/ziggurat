"""Starting-lineup seater tests (item 3.2, core/lineup.py).

The load-bearing test here is ``test_greedy_matches_brute_force``: it re-runs, on
every suite run, the comparison the recon did once — greedy versus an exhaustive
enumeration of every legal flex assignment over random 16-man rosters. That is
what licenses the greedy solve, and it is only valid for THIS league's slot
structure (one flex, pooled over RB/WR/TE), so ``test_multi_flex_is_refused``
pins the boundary in code rather than in a comment.
"""

import itertools
import random

import pytest

from ziggurat.core import lineup
from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure

POSITIONS = ("QB", "RB", "WR", "TE", "DST", "K")


# ------------------------------------------------------------- the test oracle


def _slot_list(roster):
    slots: list[tuple[str, frozenset[str]]] = []
    for pos in ("QB", "RB", "WR", "TE", "DST", "K"):
        for i in range(roster.starters.get(pos, 0)):
            slots.append((f"{pos}{i}", frozenset({pos})))
    for i in range(roster.flex_slots):
        slots.append((f"FLEX{i}", frozenset(roster.flex_positions)))
    return slots


def brute_force_total(players, positions, points, roster=DEFAULT_ROSTER, available=None):
    """Exhaustive optimum: the best assignment of players to slots, over EVERY
    assignment, with any slot allowed to be left empty (an empty slot scores 0,
    which is why a negative-projection starter is never seated).

    Deliberately dumb and obviously correct — the oracle the fast greedy seater is
    checked against, not a second implementation of the same idea. Exhaustive
    search over (slot index, set of players already used), memoized so the sweep
    finishes; ``test_oracle_agrees_with_literal_enumeration`` checks it against a
    literal enumeration on small cases.
    """
    live = [
        p for p in players
        if (available is None or available.get(p, True)) and positions.get(p) is not None
    ]
    slots = _slot_list(roster)
    seen: dict[tuple[int, int], float] = {}

    def best(slot_idx: int, used: int) -> float:
        if slot_idx == len(slots):
            return 0.0
        memo = seen.get((slot_idx, used))
        if memo is not None:
            return memo
        _label, eligible = slots[slot_idx]
        value = best(slot_idx + 1, used)                    # leave this slot empty
        for i, player in enumerate(live):
            if used >> i & 1 or positions[player] not in eligible:
                continue
            value = max(
                value,
                points.get(player, 0.0) + best(slot_idx + 1, used | (1 << i)),
            )
        seen[(slot_idx, used)] = value
        return value

    return best(0, 0)


def literal_enumeration_total(players, positions, points, roster=DEFAULT_ROSTER):
    """The oracle's own oracle: every slot independently takes a player or None,
    rejecting any assignment that uses a player twice. Only tractable for a
    handful of players, which is exactly what it is used for."""
    slots = _slot_list(roster)
    best = 0.0
    for combo in itertools.product([None, *players], repeat=len(slots)):
        used = [p for p in combo if p is not None]
        if len(set(used)) != len(used):
            continue
        total = 0.0
        ok = True
        for (_label, eligible), player in zip(slots, combo, strict=True):
            if player is None:
                continue
            if positions[player] not in eligible:
                ok = False
                break
            total += points.get(player, 0.0)
        if ok:
            best = max(best, total)
    return best


# ------------------------------------------------------------------ the tests


def test_oracle_agrees_with_literal_enumeration():
    """Guard the guard: the memoized oracle == a literal every-slot-every-player
    enumeration on small rosters."""
    rng = random.Random(11)
    tiny = RosterStructure(
        starters={"QB": 1, "RB": 1, "WR": 1}, flex_slots=1,
        flex_positions=frozenset({"RB", "WR"}),
    )
    for _ in range(25):
        players = [f"p{i}" for i in range(4)]
        positions = {p: rng.choice(("QB", "RB", "WR")) for p in players}
        points = {p: round(rng.uniform(-5.0, 20.0), 2) for p in players}
        assert brute_force_total(players, positions, points, roster=tiny) == pytest.approx(
            literal_enumeration_total(players, positions, points, roster=tiny)
        )


def test_greedy_matches_brute_force_on_random_rosters():
    """Greedy == exhaustive optimum. 0 mismatches in the recon's 300 16-man
    rosters; this re-runs the comparison every suite run so a future edit cannot
    break the argument silently."""
    rng = random.Random(20260724)
    for _ in range(150):
        players, positions, points = [], {}, {}
        for i in range(rng.choice((9, 12, 16))):
            key = f"p{i}"
            players.append(key)
            positions[key] = rng.choice(POSITIONS)
            # Negative points are real (a D/ST can bracket below zero) and are the
            # case where "seat the best available" and "maximize" come apart.
            points[key] = round(rng.uniform(-4.0, 30.0), 2)
        available = {k: rng.random() > 0.2 for k in players}
        greedy = lineup.fill_lineup(
            players, positions, points, available=available
        ).total
        oracle = brute_force_total(
            players, positions, points, available=available
        )
        assert greedy == pytest.approx(oracle, abs=1e-9), (positions, points, available)


def test_a_negative_projection_is_benched_rather_than_started():
    """An empty slot scores 0; a negative starter scores less than nothing."""
    fill = lineup.fill_lineup(
        ["d1"], {"d1": "DST"}, {"d1": -3.0}
    )
    assert dict(fill.slots)["DST"] is None
    assert fill.total == 0.0


def test_flex_takes_the_best_remaining_head():
    positions = {"rb1": "RB", "rb2": "RB", "rb3": "RB", "wr1": "WR", "wr2": "WR",
                 "wr3": "WR", "te1": "TE", "qb1": "QB", "d1": "DST", "k1": "K"}
    points = {"rb1": 20, "rb2": 18, "rb3": 5, "wr1": 19, "wr2": 17, "wr3": 12,
              "te1": 8, "qb1": 25, "d1": 7, "k1": 6}
    fill = lineup.fill_lineup(positions, positions, points)
    seated = dict(fill.slots)
    assert seated["FLEX"] == "wr3"           # 12 > rb3's 5 and > te1 (already seated)
    assert fill.total == pytest.approx(20 + 18 + 19 + 17 + 12 + 8 + 25 + 7 + 6)


def test_unavailable_players_are_neither_seated_nor_benched():
    positions = {"rb1": "RB", "rb2": "RB", "rb3": "RB"}
    points = {"rb1": 20.0, "rb2": 10.0, "rb3": 4.0}
    fill = lineup.fill_lineup(
        positions, positions, points, available={"rb1": False}
    )
    seated = dict(fill.slots)
    assert seated["RB1"] == "rb2" and seated["RB2"] == "rb3"
    assert "rb1" not in fill.bench and "rb1" not in fill.starters
    assert fill.total == pytest.approx(14.0)


def test_missing_week_scores_zero_not_an_error():
    """A player with no projection row for the week (a bye row full of NULLs, or a
    D/ST bye row that does not exist at all) is worth 0.0 that week."""
    positions = {"qb1": "QB"}
    fill = lineup.fill_lineup(["qb1"], positions, {})
    assert fill.total == 0.0
    assert dict(fill.slots)["QB"] == "qb1"


def test_empty_required_slot_is_reported_not_raised():
    fill = lineup.fill_lineup(["qb1"], {"qb1": "QB"}, {"qb1": 20.0})
    assert "K" in fill.empty_slots and "DST" in fill.empty_slots
    assert fill.total == pytest.approx(20.0)


def test_ties_seat_deterministically():
    positions = {"a": "WR", "b": "WR", "c": "WR"}
    points = dict.fromkeys(positions, 10.0)
    first = lineup.fill_lineup(positions, positions, points).slots
    for _ in range(5):
        assert lineup.fill_lineup(positions, positions, points).slots == first


def test_multi_flex_and_superflex_are_refused_not_silently_approximated():
    """Greedy optimality is proven for ONE flex over RB/WR/TE. Anything else must
    fail loudly — a silently suboptimal lineup total is invisible (Rule 6)."""
    with pytest.raises(lineup.LineupStructureError):
        lineup.fill_lineup([], {}, {}, roster=RosterStructure(flex_slots=2))
    with pytest.raises(lineup.LineupStructureError):
        lineup.fill_lineup(
            [], {}, {},
            roster=RosterStructure(flex_positions=frozenset({"QB", "RB", "WR", "TE"})),
        )


def test_ir_players_are_dropped_from_the_active_roster():
    rows = [
        {"player": "starter", "lineup_slot": "RB"},
        {"player": "benched", "lineup_slot": "BE"},
        {"player": "hurt", "lineup_slot": "IR"},
    ]
    kept = lineup.active_players(rows)
    assert [r["player"] for r in kept] == ["starter", "benched"]


def test_roster_structure_knows_its_slot_counts():
    assert DEFAULT_ROSTER.starting_slots == 9
    assert DEFAULT_ROSTER.active_slots == 16
    assert DEFAULT_ROSTER.ir_slots == 1


def test_format_lineup_names_every_slot():
    fill = lineup.fill_lineup(["qb1"], {"qb1": "QB"}, {"qb1": 12.0})
    text = lineup.format_lineup(fill, names={"qb1": "Some Quarterback"})
    assert "Some Quarterback" in text and "TOTAL" in text and "(empty)" in text
