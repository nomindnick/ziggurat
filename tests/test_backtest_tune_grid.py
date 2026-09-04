"""Item 4.2 — the pre-registered grid (backtest/tune_grid.py) is literal data.

Every number here is transcribed from the FROZEN pre-registration
(`intel/research/breakout-backtest.md` §4.2 / §4.3); these tests check the
transcription against the shipped floors and pin the counts and the reference
cache key, so a slip in the literal text — or a moved shipped floor — rots the
grid loudly before a single cell is spent.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from backtest import decisions as D
from backtest import replay as R
from backtest import tune_grid as G


def test_the_shipped_row_is_the_shipped_generator():
    # the grid is anchored to core/candidates.py, not to a remembered copy:
    # the literal shipped row must parse to exactly D.default_generator()
    assert G.DEFAULT_CELL.generator == D.default_generator()
    assert G.DEFAULT_CELL.overrides == {} and G.DEFAULT_CELL.moved_axes == ()
    assert set(G.Cell.generator_names()) == {m for m, _ in D.default_generator()}
    assert len(G.Cell.generator_names()) == 12


def _exact_product(shipped: str, c: str) -> Decimal:
    return Decimal(shipped) * Decimal(c)


@pytest.mark.parametrize("family, c", sorted(G.SCALE_MAPS))
def test_a_scale_cell_resolves_to_its_literal_map(family, c):
    # §4.3: "the runner asserts, per setting, that its resolved 12-floor map
    # equals the literal row by exact float equality of the parsed literals",
    # and a test "recomputes each scale map from the shipped floors x c and
    # asserts exact equality with the literal".  The recompute is done in
    # EXACT decimal arithmetic on the literal strings (the document's own
    # "no rounding, anywhere"): binary floats cannot represent 0.55 x 0.75
    # exactly, so a float multiply is NOT equal to float("0.4125") and the
    # literal is what the cache key hashes.  Mutant tried: changing one digit
    # of a literal (0.4125 -> 0.4126) fails the decimal equality; a float
    # multiply in place of the literal is caught by test_no_scale_map_is_computed.
    cell = G.SCALE_MAPS[(family, c)]
    scaled = G.DIFFERENCED_AXES if family == "global_scale" else G.EMERGENCE_AXES
    shipped_by_axis = dict(zip(G.DIFFERENCED_AXES, G.SHIPPED_DIFFERENCED, strict=True))
    shipped_emg = dict(zip(G.EMERGENCE_AXES, G.SHIPPED_EMERGENCE, strict=True))
    for axis, literal in zip(G.DIFFERENCED_AXES, cell.differenced, strict=True):
        expect = (_exact_product(shipped_by_axis[axis], c) if family == "global_scale"
                  else Decimal(shipped_by_axis[axis]))
        assert Decimal(literal) == expect, (cell.cell_id, axis, literal, expect)
        assert float(literal) == float(expect)          # the parsed literal is the nearest float
    for axis, literal in zip(G.EMERGENCE_AXES, cell.emergence, strict=True):
        expect = (_exact_product(shipped_emg[axis], c) if family == "emergence_scale"
                  else Decimal(shipped_emg[axis]))
        assert Decimal(literal) == expect, (cell.cell_id, axis, literal, expect)
        assert float(literal) == float(expect)
    # the resolved map is the parsed literal, floor for floor
    assert cell.floors == {m: float(v) for m, v in zip(G.DIFFERENCED_AXES, cell.differenced, strict=True)} | {
        D.EMERGENCE_PREFIX + m: float(v) for m, v in zip(G.EMERGENCE_AXES, cell.emergence, strict=True)
    }
    # and it is a legal ReplayParams generator (A1: every floor > 0, shares < 1)
    D.ReplayParams(strategies=G.STRATEGIES, k=3, seasons=G.TRAIN_SEASONS,
                   generator=cell.generator)
    del scaled


def test_no_scale_map_is_computed_at_import():
    # the literal 0.4125 is NOT 0.55 * 0.75 in binary; if the module had
    # multiplied, this row would carry the float product and the cache key
    # would be a function of the arithmetic, not of the frozen text
    cell = G.SCALE_MAPS[("emergence_scale", "0.75")]
    assert cell.floors["emergence:offense_pct"] == float("0.4125")
    assert 0.55 * 0.75 != float("0.4125")           # the reason the rule exists
    assert cell.floors["emergence:offense_pct"] != 0.55 * 0.75
    assert G.SCALE_MAPS[("global_scale", "0.35")].floors["carries"] == float("2.1") != 6 * 0.35


def test_grid_counts_are_the_preregistered_ones():
    # §4.3: 31 + 5 + 4 = 40 eligible, plus the DEFAULT reference, plus 3
    # flood diagnostics = 44 runs.  Mutant tried: dropping one single-axis
    # level, or making a flood cell eligible.
    assert G.counts() == {
        "single_axis": 31, "global_scale": 5, "emergence_scale": 4, "flood": 3,
        "default": 1, "round1": 44, "eligible": 40,
    }
    assert {axis: len(levels) for axis, levels in G.SINGLE_AXIS_LEVELS.items()} == {
        "carries": 4, "targets": 4, "receptions": 3, "target_share": 4,
        "air_yards_share": 4, "offense_pct": 4, "rushing_yards": 4, "receiving_yards": 4,
    }
    assert all(not c.eligible for c in G.FLOOD_CELLS) and not G.DEFAULT_CELL.eligible
    assert all(c.eligible for c in G.SINGLE_AXIS_CELLS + G.GLOBAL_SCALE_CELLS
               + G.EMERGENCE_SCALE_CELLS)
    # every cell has a distinct id AND a distinct resolved map (the runner
    # dedupes by cache key; the grid must not hand it a duplicate)
    ids = [c.cell_id for c in G.ROUND1_CELLS]
    assert len(set(ids)) == len(ids)
    keys = {c.generator for c in G.ROUND1_CELLS}
    assert len(keys) == len(G.ROUND1_CELLS)
    # every single-axis cell moves exactly ONE differenced floor, none of the emergence
    for cell in G.SINGLE_AXIS_CELLS:
        assert list(cell.overrides) == [cell.axis] and cell.emergence == G.SHIPPED_EMERGENCE
    for cell in G.GLOBAL_SCALE_CELLS:
        assert set(cell.overrides) == set(G.DIFFERENCED_AXES)
    for cell in G.EMERGENCE_SCALE_CELLS:
        assert set(cell.overrides) == {D.EMERGENCE_PREFIX + m for m in G.EMERGENCE_AXES}
    # round-2 constants as pre-registered (§4.4 / §4.5)
    assert G.ROUND2_AXIS_ORDER == ("carries", "targets", "receptions", "target_share",
                                   "air_yards_share", "offense_pct", "rushing_yards",
                                   "receiving_yards")
    assert G.ROUND2_REVERSE_ORDER[0] == "receiving_yards"
    assert (G.FORWARD_BUDGET, G.REVERSE_BUDGET, G.LOO_BUDGET, G.BISECTION_DECIDE_TRIALS,
            G.CONTROL_GRADES) == (8, 8, 8, 6, 1)
    assert G.ROUND2_MAX == 31 and G.MAX_SETTINGS == 80
    assert G.BISECTION_RANGE == (0.5, 2.0) and G.BISECTION_POOL_TOLERANCE == 5


def test_the_default_cell_hashes_to_the_reference_key():
    # the reference TRAIN freeze 28007210abc5 (three strategies in this
    # order, k=3, 2021-2023, seed 0, weeks None) must be what the DEFAULT
    # cell resolves to, or the whole search pairs against a key no freeze
    # holds.  Mutant tried: listing only the overridden floors in the tuple
    # (an empty tuple is refused by ReplayParams); reordering the strategies
    # gives a different key.
    params = D.ReplayParams(
        strategies=("signal_topk", "random_k", "volume_topk"), k=3,
        seasons=(2021, 2022, 2023), seed=0, weeks=None,
        generator=G.DEFAULT_CELL.generator,
    )
    assert params.cache_key() == "28007210abc5"
    assert params.generator_is_default
    # the same shape the CLI builds from no override flags
    assert G.DEFAULT_CELL.generator == R.generator_setting(None, None)
    # a cell's generator is the full 12-pair map in generator_setting's shape
    cell = G.cell_by_id("carries=5")
    assert cell.generator == R.generator_setting([("carries", 5.0)], None)
    assert D.ReplayParams(strategies=G.STRATEGIES, k=3, seasons=G.TRAIN_SEASONS,
                          generator=cell.generator).cache_key() != "28007210abc5"


def test_round2_cell_builders_round_trip_their_floors():
    # combined_cell / scaled_cell render floats with repr, so the literal
    # parses back to the same float and the resolved-map assertion holds
    floors = dict(G.DEFAULT_CELL.floors)
    floors["carries"] = 5.0
    floors["emergence:offense_pct"] = 0.4125
    cell = G.combined_cell(floors, cell_id="fwd:carries", axis="forward", level="carries=5",
                           eligible=True)
    assert cell.floors == floors and cell.round == 2 and cell.moved_axes == (
        "carries", "emergence:offense_pct")
    s = G.scaled_cell(1.0625, cell_id="bisect:1", note="x")
    assert s.floors["carries"] == 6 * 1.0625 and s.eligible is False
    assert all(math.isfinite(v) for v in s.floors.values())
    with pytest.raises(KeyError):
        G.cell_by_id("carries=6")        # the shipped value is not a cell
