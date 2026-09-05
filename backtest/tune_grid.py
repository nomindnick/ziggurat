"""Item 4.2 — the PRE-REGISTERED tuning grid, as literal data.

Every cell below is transcribed from the frozen pre-registration
(`intel/research/breakout-backtest.md` §4.2 / §4.3 / §4.4 / §4.5) as the
LITERAL STRINGS written there, parsed with ``float(...)`` — never computed
from the shipped floors at import time.  The frozen document says why:

    "it constructs each map by parsing the literal strings written here
    (e.g. `float("0.075")`), never by multiplying at run time — so the cache
    key is a function of this text."

A test (`test_a_scale_cell_resolves_to_its_literal_map`) recomputes each scale
map from the shipped floors x c and asserts exact equality with the literal,
so a transcription slip is caught without the module ever doing the multiply.

Each :class:`Cell` resolves to the FULL 12-pair ``ReplayParams.generator``
tuple (:func:`Cell.generator`), in exactly the shape ``replay.generator_setting``
produces, so the DEFAULT cell hashes to the reference key ``28007210abc5``
under the three-strategy TRAIN shape (pinned by test).

Round-2 constants (§4.4 / §4.5) live here too so the runner carries no number
the pre-registration does not.
"""

from __future__ import annotations

from dataclasses import dataclass

from backtest import decisions as D

# ---------------------------------------------------------------------------
# axis names, in the declaration order of core/candidates.py (§4.4 step 2:
# "PERTURBATION-INDEPENDENT ... never a function of round-1 outcomes")
# ---------------------------------------------------------------------------

#: The eight differenced floors, `DEFAULT_BREAKOUT` declaration order.
DIFFERENCED_AXES: tuple[str, ...] = (
    "carries", "targets", "receptions", "target_share", "air_yards_share",
    "offense_pct", "rushing_yards", "receiving_yards",
)
#: The four role-emergence floors, `EMERGENCE_FLOORS` declaration order.
EMERGENCE_AXES: tuple[str, ...] = ("carries", "targets", "receptions", "offense_pct")

#: The shipped values as the frozen document writes them (§4.3): differenced
#: `6, 4, 3, 0.08, 0.10, 0.20, 25, 25`; emergence `10, 5, 4, 0.55`.  A test
#: asserts these equal `D.default_generator()` — the grid is anchored to the
#: shipped floors, not to a remembered copy of them.
SHIPPED_DIFFERENCED: tuple[str, ...] = ("6", "4", "3", "0.08", "0.10", "0.20", "25", "25")
SHIPPED_EMERGENCE: tuple[str, ...] = ("10", "5", "4", "0.55")

#: Cell ids of the fixed reference points.
DEFAULT_CELL_ID = "default"


# ---------------------------------------------------------------------------
# the cell
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """One pre-registered setting.

    ``axis`` is the round-1 family the cell belongs to (a differenced metric
    name, ``"global_scale"``, ``"emergence_scale"``, ``"flood"`` or
    ``"default"``); ``level`` is the literal level string from the document
    (a floor value for a single-axis cell, ``c`` for a scale cell, the cell's
    own label for a flood cell); ``eligible`` is the argmax-eligibility of
    §4.3 / D4 (flood cells and the default reference are NOT eligible; the
    default is the comparison base, never a candidate).

    ``differenced`` / ``emergence`` are the LITERAL 8- and 4-tuples of floor
    strings in declaration order — the row of the document's table.
    """

    cell_id: str
    axis: str
    level: str
    eligible: bool
    differenced: tuple[str, ...]
    emergence: tuple[str, ...]
    note: str = ""
    round: int = 1

    def __post_init__(self) -> None:
        if len(self.differenced) != len(DIFFERENCED_AXES):
            raise ValueError(f"{self.cell_id}: {len(self.differenced)} differenced floors")
        if len(self.emergence) != len(EMERGENCE_AXES):
            raise ValueError(f"{self.cell_id}: {len(self.emergence)} emergence floors")

    @property
    def floors(self) -> dict[str, float]:
        """The resolved 12-floor map, ``emergence:`` keys for the four
        role-emergence floors, values parsed from the literal strings."""
        out = {m: float(v) for m, v in zip(DIFFERENCED_AXES, self.differenced, strict=True)}
        out.update({D.EMERGENCE_PREFIX + m: float(v)
                    for m, v in zip(EMERGENCE_AXES, self.emergence, strict=True)})
        return out

    @property
    def generator(self) -> tuple[tuple[str, float], ...]:
        """The ``ReplayParams.generator`` value — sorted ``(name, floor)``
        pairs over all 12 floors, the same shape ``replay.generator_setting``
        builds (a default-valued floor is still a pair; the tuple is the whole
        map, so the default cell's key is the reference key)."""
        return tuple(sorted(self.floors.items()))

    @property
    def overrides(self) -> dict[str, float]:
        """Only the floors that differ from shipped — what a CLI invocation
        would pass as ``--breakout-floor`` / ``--emergence-floor``."""
        shipped = dict(D.default_generator())
        return {m: v for m, v in self.floors.items() if shipped[m] != v}

    @property
    def moved_axes(self) -> tuple[str, ...]:
        """The 12-floor names this cell moves from shipped (for LOO, §4.4 step 4)."""
        return tuple(m for m in self.generator_names() if m in self.overrides)

    @staticmethod
    def generator_names() -> tuple[str, ...]:
        return tuple(DIFFERENCED_AXES) + tuple(D.EMERGENCE_PREFIX + m for m in EMERGENCE_AXES)


def _single(axis: str, level: str) -> Cell:
    """A §4.2 cell: the shipped map with exactly one differenced floor replaced."""
    i = DIFFERENCED_AXES.index(axis)
    diff = list(SHIPPED_DIFFERENCED)
    diff[i] = level
    return Cell(
        cell_id=f"{axis}={level}", axis=axis, level=level, eligible=True,
        differenced=tuple(diff), emergence=SHIPPED_EMERGENCE,
        note=f"§4.2 single-axis: {axis} {level} (shipped {SHIPPED_DIFFERENCED[i]})",
    )


# ---------------------------------------------------------------------------
# §4.2 round 1 — one axis at a time from the default (31 cells)
# ---------------------------------------------------------------------------

#: The level lists verbatim from the §4.2 table (the shipped value is not a cell).
SINGLE_AXIS_LEVELS: dict[str, tuple[str, ...]] = {
    "carries": ("1", "2", "5", "7"),
    "targets": ("2", "3", "5", "6"),
    "receptions": ("2", "4", "5"),
    "target_share": ("0.059", "0.100", "0.137", "0.163"),
    "air_yards_share": ("0.066", "0.136", "0.209", "0.257"),
    "offense_pct": ("0.12", "0.15", "0.30", "0.37"),
    "rushing_yards": ("5", "12", "19", "42"),
    "receiving_yards": ("19", "36", "53", "65"),
}

SINGLE_AXIS_CELLS: tuple[Cell, ...] = tuple(
    _single(axis, level)
    for axis in DIFFERENCED_AXES
    for level in SINGLE_AXIS_LEVELS[axis]
)


# ---------------------------------------------------------------------------
# §4.3 round 1 — the two SCALE axes, as literal 12-pair maps
# ---------------------------------------------------------------------------

#: GLOBAL differenced scale: all 8 differenced floors x c, emergence shipped.
#: Every row is the document's table row, verbatim.  5 cells, argmax-ELIGIBLE.
GLOBAL_SCALE_LITERALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("0.5",  ("3",   "2", "1.5",  "0.04", "0.05",  "0.10", "12.5",  "12.5")),
    ("0.75", ("4.5", "3", "2.25", "0.06", "0.075", "0.15", "18.75", "18.75")),
    ("1.25", ("7.5", "5", "3.75", "0.10", "0.125", "0.25", "31.25", "31.25")),
    ("1.5",  ("9",   "6", "4.5",  "0.12", "0.15",  "0.30", "37.5",  "37.5")),
    ("2.0",  ("12",  "8", "6",    "0.16", "0.20",  "0.40", "50",    "50")),
)

GLOBAL_SCALE_CELLS: tuple[Cell, ...] = tuple(
    Cell(
        cell_id=f"global_scale={c}", axis="global_scale", level=c, eligible=True,
        differenced=diff, emergence=SHIPPED_EMERGENCE,
        note=f"§4.3 GLOBAL differenced scale c={c}; emergence shipped",
    )
    for c, diff in GLOBAL_SCALE_LITERALS
)

#: EMERGENCE joint scale: all 4 emergence floors x c, differenced shipped.
#: c=2.0 is DROPPED (emergence:offense_pct would be 1.10 >= 1.0).  4 cells,
#: argmax-ELIGIBLE, all four pre-registered EXPECTED INERT (§3.4).
EMERGENCE_SCALE_LITERALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("0.5",  ("5",    "2.5",  "2", "0.275")),
    ("0.75", ("7.5",  "3.75", "3", "0.4125")),
    ("1.5",  ("15",   "7.5",  "6", "0.825")),
    ("1.75", ("17.5", "8.75", "7", "0.9625")),
)

EMERGENCE_SCALE_CELLS: tuple[Cell, ...] = tuple(
    Cell(
        cell_id=f"emergence_scale={c}", axis="emergence_scale", level=c, eligible=True,
        differenced=SHIPPED_DIFFERENCED, emergence=emg,
        note=f"§4.3 EMERGENCE joint scale c={c}; differenced shipped; expected INERT (§3.4)",
    )
    for c, emg in EMERGENCE_SCALE_LITERALS
)

#: RECALL-FLOOD diagnostic cells — argmax-INELIGIBLE whatever they score (D3/D4).
#: FLOOD-1 is the recon LOOSE probe verbatim; FLOOD-2/3 are global scale 0.25 /
#: 0.35 with emergence shipped.  The literal scale constant is carried so the
#: literal-map test can recompute FLOOD-2/3 like any other scale cell.
FLOOD_LITERALS: tuple[tuple[str, str | None, tuple[str, ...], tuple[str, ...]], ...] = (
    ("FLOOD-1", None,   ("1",   "1",   "1",    "0.01",  "0.01",  "0.02", "1",    "1"),
     ("1", "1", "1", "0.05")),
    ("FLOOD-2", "0.25", ("1.5", "1",   "0.75", "0.02",  "0.025", "0.05", "6.25", "6.25"),
     SHIPPED_EMERGENCE),
    ("FLOOD-3", "0.35", ("2.1", "1.4", "1.05", "0.028", "0.035", "0.07", "8.75", "8.75"),
     SHIPPED_EMERGENCE),
)

FLOOD_CELLS: tuple[Cell, ...] = tuple(
    Cell(
        cell_id=label, axis="flood", level=(c if c is not None else label), eligible=False,
        differenced=diff, emergence=emg,
        note=("§4.3 RECALL-FLOOD diagnostic (argmax-INELIGIBLE): "
              + ("the recon LOOSE probe verbatim" if c is None
                 else f"global scale {c}, emergence shipped")),
    )
    for label, c, diff, emg in FLOOD_LITERALS
)

#: The DEFAULT reference cell: the shipped map, the comparison base of every
#: `D(g)`; NOT an argmax candidate (it is what "no change" means).
DEFAULT_CELL = Cell(
    cell_id=DEFAULT_CELL_ID, axis="default", level="shipped", eligible=False,
    differenced=SHIPPED_DIFFERENCED, emergence=SHIPPED_EMERGENCE,
    note="the shipped core/candidates floors — the reference every D(g) is paired against",
)

#: Round 1 in evaluation order: the default first (every other cell pairs
#: against it), then the 31 single-axis, 5 global-scale, 4 emergence-scale and
#: 3 flood cells = 44 runs (§4.3).
ROUND1_CELLS: tuple[Cell, ...] = (
    (DEFAULT_CELL,) + SINGLE_AXIS_CELLS + GLOBAL_SCALE_CELLS + EMERGENCE_SCALE_CELLS
    + FLOOD_CELLS
)

#: Literal scale maps by (family, c) for the recompute test and the bisection.
SCALE_MAPS: dict[tuple[str, str], Cell] = {
    **{("global_scale", cell.level): cell for cell in GLOBAL_SCALE_CELLS},
    **{("emergence_scale", cell.level): cell for cell in EMERGENCE_SCALE_CELLS},
    **{("global_scale", cell.level): cell for cell in FLOOD_CELLS if cell.level != cell.cell_id},
}


def cell_by_id(cell_id: str) -> Cell:
    for cell in ROUND1_CELLS:
        if cell.cell_id == cell_id:
            return cell
    raise KeyError(f"no pre-registered cell {cell_id!r}")


def scaled_cell(c: float, *, cell_id: str, eligible: bool = False, note: str = "",
                round_: int = 2) -> Cell:
    """A round-2 GLOBAL-scale cell at an arbitrary ``c`` (§4.4 step 5, the
    scale-only bisection) — the ONE place a map is computed rather than
    transcribed, because the bisection's constants are not pre-registerable.
    Values are formatted with ``repr`` so the literal round-trips exactly."""
    shipped = [float(v) for v in SHIPPED_DIFFERENCED]
    diff = tuple(repr(float(v) * float(c)) for v in shipped)
    return Cell(
        cell_id=cell_id, axis="global_scale", level=repr(float(c)), eligible=eligible,
        differenced=diff, emergence=SHIPPED_EMERGENCE, note=note, round=round_,
    )


def combined_cell(floors: dict[str, float], *, cell_id: str, axis: str, level: str,
                  eligible: bool, note: str = "") -> Cell:
    """A round-2 cell from a resolved 12-floor map (forward/reverse greedy,
    LOO): the map's values re-rendered with ``repr`` so the literal parses
    back to the same float."""
    diff = tuple(repr(float(floors[m])) for m in DIFFERENCED_AXES)
    emg = tuple(repr(float(floors[D.EMERGENCE_PREFIX + m])) for m in EMERGENCE_AXES)
    return Cell(cell_id=cell_id, axis=axis, level=level, eligible=eligible,
                differenced=diff, emergence=emg, note=note, round=2)


# ---------------------------------------------------------------------------
# §4.4 / §4.5 round-2 rules — constants only; the procedure is backtest/tune.py
# ---------------------------------------------------------------------------

#: Round 2 nests ONLY the 8 named differenced axes, in declaration order;
#: the scale axes are excluded from nesting (§4.4 preamble).
ROUND2_AXIS_ORDER: tuple[str, ...] = DIFFERENCED_AXES
#: The reverse-order replicate (§4.4 step 6): `receiving_yards` first.
ROUND2_REVERSE_ORDER: tuple[str, ...] = tuple(reversed(DIFFERENCED_AXES))

#: Per-step evaluation budgets (§4.4 steps 3-6; "a skipped attempt was still
#: evaluated and counts").
FORWARD_BUDGET = 8
REVERSE_BUDGET = 8
LOO_BUDGET = 8
BISECTION_DECIDE_TRIALS = 6
CONTROL_GRADES = 1
ROUND2_MAX = FORWARD_BUDGET + REVERSE_BUDGET + LOO_BUDGET + BISECTION_DECIDE_TRIALS + CONTROL_GRADES
assert ROUND2_MAX == 31

#: §4.5: HARD CAP — the runner refuses the 81st setting.
MAX_SETTINGS = 80
#: §4.4 step 5: the scale-only control searches c in [0.5, 2.0] for a median
#: per-week pool within +-5 rows of the winner's; outside → "unmatched".
BISECTION_RANGE: tuple[float, float] = (0.5, 2.0)
BISECTION_POOL_TOLERANCE = 5
#: §4.4 step 1 / §7.1: a D(g) tie within this resolves to the smaller |log ratio|.
TIE_BAND = 0.005
#: §4.4 steps 1 and 3: the single-cell and increment permutation bar (unadjusted).
PERMUTATION_ALPHA = 0.05
#: §7.1 G1 — the practical floor D(g) must clear (pp of depth-matched lift).
#: PROVENANCE (external review C22, 2026-09-04): this is the rounded half-width
#: of the ARGMAX-INELIGIBLE FLOOD-1 cell's own paired interval — 5.369pp =
#: 2.0154 x that ONE cell's SE, i.e. ~2 standard errors of a single cell,
#: rounded up to 0.054. It is NOT a power calculation and NOT a
#: decision-utility bar (the 2,333-line pre-registration contains no
#: decision-utility language), and it sits BELOW the measured family max-null
#: bar of 0.0721: 15.3% of pure-noise replicates clear it. Read it as "bigger
#: than one cell's noise", never as "big enough to matter".
PRACTICAL_FLOOR = 0.054

#: The frozen §7.4 values every setting asserts before it reads a byte.
TRAIN_SEASONS = D.TRAIN_SEASONS
STRATEGIES: tuple[str, ...] = ("signal_topk", "random_k", "volume_topk")
K = 3
SEED = 0
WEEKS = None
HIT_PLACES = 5
OWNED_DELTA = 10.0
GRADE_AS_OF = "2026-09-03"
MARKET = "wp"


def counts() -> dict[str, int]:
    """The pre-registered counts (§4.3): 31 / 5 / 4 / 3 + 1 default = 44."""
    return {
        "single_axis": len(SINGLE_AXIS_CELLS),
        "global_scale": len(GLOBAL_SCALE_CELLS),
        "emergence_scale": len(EMERGENCE_SCALE_CELLS),
        "flood": len(FLOOD_CELLS),
        "default": 1,
        "round1": len(ROUND1_CELLS),
        "eligible": sum(1 for c in ROUND1_CELLS if c.eligible),
    }
