"""Frozen decision records for the weekly replay harness (item 4.1).

The replay has two phases that must never share a read:

1. **decide** (``backtest/replay.py``) — at ``as_of(T)`` the production
   candidate generator is asked what it would have said, a strategy turns
   the board into at most ``k`` :class:`Decision` rows, and the rows are
   FROZEN (in memory, and optionally as JSONL under a gitignored cache).
2. **grade** (``backtest/scorecards.py``) — at a strictly later
   ``grade_as_of`` the frozen rows are compared with what the market did next.

This module holds the records the two phases hand each other and the
freeze/load round trip, so that neither phase needs to import the other.
Everything here is plain data: no database, no scoring, no model.  The one
thing it reaches into ``ziggurat/`` for is the generator's SHIPPED floor
constants (lazily, in :func:`default_generator`), so that the replay's cache
key always hashes the values the production generator runs with — never a
sentinel that would keep an old freeze reachable after the floors moved.

It also holds the HOLDOUT lock: 2024-25 are the seasons every threshold is
meant to be *reported* on and never tuned on, so every entry point that would
decide on or grade them refuses unless the caller passes an explicit unlock,
and the CLI appends every unlocked run to a gitignored ledger AFTER the run
completed (publish-then-record).
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: The five seasons the weekly ECR panel spans continuously (spike 1.2).
SEASONS: tuple[int, ...] = (2021, 2022, 2023, 2024, 2025)
#: Thresholds may be tuned on these ...
TRAIN_SEASONS: tuple[int, ...] = (2021, 2022, 2023)
#: ... and only ever *reported* on these (backtest/README.md).
HOLDOUT_SEASONS: tuple[int, ...] = (2024, 2025)

#: The gitignored ledger every HOLDOUT unlock is appended to (under the replay
#: cache dir, ``data/backtest/replay/``): one JSON line per completed run.
UNLOCK_LEDGER = "holdout-unlocks.jsonl"


class HoldoutLocked(ValueError):
    """A HOLDOUT season was asked for without the explicit, logged unlock."""


def holdout_seasons(seasons: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted({int(s) for s in seasons if int(s) in HOLDOUT_SEASONS}))


def require_holdout_unlock(seasons: Iterable[int], *, unlock_holdout: bool) -> tuple[int, ...]:
    """Refuse a HOLDOUT season unless the caller unlocked it explicitly.

    Called at every entry point that would decide on or grade a season —
    BEFORE any database is opened — so the lock cannot be bypassed by going
    through a lower layer.  Returns the holdout seasons present (empty for a
    TRAIN-only run) so the caller knows whether the ledger applies.
    """
    held = holdout_seasons(seasons)
    if held and not unlock_holdout:
        raise HoldoutLocked(
            f"seasons {held} are HOLDOUT: 2024-25 is refused without an explicit, "
            f"logged unlock flag; tune on TRAIN_SEASONS {TRAIN_SEASONS}.  Pass "
            "--unlock-holdout (unlock_holdout=True) to read them — every unlocked run "
            f"is appended to the {UNLOCK_LEDGER} ledger after it completes."
        )
    return held


def record_holdout_unlock(
    cache_dir: str | os.PathLike,
    *,
    params: ReplayParams,
    seasons: Sequence[int],
    argv: Sequence[str],
    grade_as_of: str,
    written_at: str,
    phase: str,
) -> Path:
    """Append one line to the unlock ledger.  Publish-then-record: the caller
    invokes this only AFTER the unlocked decide/grade completed, so a run that
    failed (or a dry preview) never claims a read it did not make."""
    path = Path(cache_dir) / UNLOCK_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "written_at": written_at,
        "params_hash": params.params_hash,
        "cache_key": params.cache_key(),
        "holdout_seasons": [int(s) for s in seasons],
        "seasons": list(params.seasons),
        "strategies": list(params.strategies),
        "grade_as_of": grade_as_of,
        "phase": phase,
        "argv": [str(a) for a in argv],
    }
    line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return path

#: Positions the usage arm of the generator emits (core/candidates.py
#: ``USAGE_POSITIONS``) — the replay's default pool.
DEFAULT_POSITIONS: tuple[str, ...] = ("RB", "WR", "TE")

#: HYPOTHESIS (item 4.1 recon): a player the market already ranks inside this
#: window on the week-T weekly page is a starter having a big week, not a
#: waiver candidate, and is excluded from the pool.  Unranked players are
#: always eligible.  Nothing in Phase 3 measured these numbers; they are the
#: rough size of a 10-team league's starting pool (2 RB + flex, 2-3 WR + flex,
#: 1 TE, 1 QB) and 4.2 owns tuning them — on TRAIN seasons only.
ELIGIBILITY_HYPOTHESIS: Mapping[str, int] = {"RB": 24, "WR": 24, "TE": 12, "QB": 12}
ELIGIBILITY_LABEL = (
    "ELIGIBILITY (hypothesis, item 4.1 recon; untuned): a candidate is in the "
    "pool only if the week-T weekly market page ranks them OUTSIDE "
    "RB>24 / WR>24 / TE>12 / QB>12, or not at all.  The week-T page is its last "
    "scrape at or before as_of(T) — in practice the Friday OF week T, after "
    "Thursday's game and before Sunday's."
)

#: Strategies the replay knows.  Kept as data here so a frozen cache can be
#: validated without importing the decide phase.
STRATEGY_NAMES: tuple[str, ...] = ("signal_topk", "random_k", "volume_topk")

#: Prefix that marks a ROLE-EMERGENCE (absolute-usage) floor inside
#: :attr:`ReplayParams.generator`; bare metric names are the differenced floors.
EMERGENCE_PREFIX = "emergence:"

#: The floors that are SHARES of a team total (a fraction in [0, 1]); a floor
#: at or above 1.0 on any of them can never be cleared.  Item 4.2, A1.
SHARE_FLOORS: frozenset[str] = frozenset({
    "target_share", "air_yards_share", "offense_pct", EMERGENCE_PREFIX + "offense_pct",
})


def default_generator() -> tuple[tuple[str, float], ...]:
    """The generator's SHIPPED floors as sorted ``(metric, floor)`` pairs.

    Read from ``ziggurat.core.candidates`` at call time (a lazy import: this
    module stays free of the generator otherwise), so the replay's cache key
    hashes the VALUES the production generator runs with.  If someone edits
    ``DEFAULT_BREAKOUT`` the key changes and every old freeze becomes
    unreachable — which is the point: a freeze decided under other floors
    must never be graded as if it were this generator's.
    """
    from ziggurat.core import candidates as C

    pairs = [(str(m), float(v)) for m, v in C.DEFAULT_BREAKOUT.floors.items()]
    pairs += [(EMERGENCE_PREFIX + str(m), float(v)) for m, v in C.EMERGENCE_FLOORS.items()]
    return tuple(sorted(pairs))

#: Week statuses a :class:`WeekRecord` can carry.  Only ``decided`` weeks hold
#: decisions; every other status is an ungradeable week that the scorecard
#: reports by count and reason — never silently dropped.
WEEK_DECIDED = "decided"
WEEK_GENERATOR_FAILED = "generator_failed"
WEEK_NO_SCHEDULE = "no_schedule"
WEEK_EMPTY_POOL = "empty_pool"


def split_of(season: int) -> str:
    """``TRAIN`` / ``HOLDOUT`` / ``OTHER`` — printed beside every season."""
    if season in TRAIN_SEASONS:
        return "TRAIN"
    if season in HOLDOUT_SEASONS:
        return "HOLDOUT"
    return "OTHER"


@dataclass(frozen=True)
class ReplayParams:
    """Everything that determines a decision, hashed into the cache key.

    ``market`` names the market whose week-T page decides pool ELIGIBILITY
    (the weekly page ``wp`` by default: it is the plan-mandated primary and
    the one a 10-team league's waiver wire actually tracks).  Grading may run
    against any market; eligibility is fixed at decide time.

    ``generator`` is the usage arm's floor setting as sorted ``(metric, floor)``
    pairs — the differenced floors under their metric name, the role-emergence
    (absolute) floors under ``emergence:<metric>`` — defaulting to the shipped
    constants (:func:`default_generator`).  It is part of the hash on purpose:
    a freeze decided under one set of floors is a different experiment from one
    decided under another, and the values (never a "default" sentinel) are what
    make two freezes comparable.  The HOLDOUT unlock is deliberately NOT a
    field: it changes what a run may read, not what it decides.
    """

    strategies: tuple[str, ...]
    k: int
    seasons: tuple[int, ...]
    positions: tuple[str, ...] = DEFAULT_POSITIONS
    market: str = "wp"
    seed: int = 0
    eligibility: tuple[tuple[str, int], ...] = field(
        default_factory=lambda: tuple(sorted(ELIGIBILITY_HYPOTHESIS.items()))
    )
    weeks: tuple[int, ...] | None = None
    generator: tuple[tuple[str, float], ...] = field(default_factory=default_generator)

    def __post_init__(self) -> None:
        if not (1 <= self.k <= 3):
            raise ValueError(
                f"k={self.k}: precision@k is defined for k in 1..3 only "
                "(backtest/README.md: a 10-team league's waiver window is that narrow)"
            )
        if not self.strategies:
            raise ValueError("at least one strategy is required")
        unknown = [s for s in self.strategies if s not in STRATEGY_NAMES]
        if unknown:
            raise ValueError(f"unknown strategy {unknown}; known: {STRATEGY_NAMES}")
        if not self.seasons:
            raise ValueError("at least one season is required")
        if not self.positions:
            raise ValueError("at least one position is required")
        object.__setattr__(self, "strategies", tuple(self.strategies))
        object.__setattr__(self, "seasons", tuple(int(s) for s in self.seasons))
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "eligibility", tuple((p, int(t)) for p, t in self.eligibility))
        if self.weeks is not None:
            object.__setattr__(self, "weeks", tuple(int(w) for w in self.weeks))
        gen = tuple(sorted((str(m), float(v)) for m, v in self.generator))
        known = {m for m, _ in default_generator()}
        bad = sorted(m for m, _ in gen if m not in known)
        if bad:
            raise ValueError(
                f"unknown generator floor {bad}; the usage arm knows {sorted(known)} "
                f"(role-emergence floors carry the {EMERGENCE_PREFIX!r} prefix)"
            )
        if len({m for m, _ in gen}) != len(gen):
            raise ValueError("a generator floor may be given once only")
        if not gen:
            raise ValueError("the generator needs at least one floor")
        # Item 4.2, admissibility A1 (breakout-backtest.md §3.6 / §11.3): the
        # generator's `magnitude` is Σ delta/floor, so a floor of 0 divides by
        # zero (swallowed per week into `generator_failed`) and a NEGATIVE floor
        # admits collapsing usage and INVERTS that metric's ranking — both exit
        # 0 today.  A SHARE floor at or above 1.0 can never be cleared, which
        # silently disables its axis.  Refused HERE, at the parameters, because
        # this is the one place every run passes through before a cache key.
        degenerate = [(m, v) for m, v in gen if not (v > 0.0)]
        if degenerate:
            raise ValueError(
                f"generator floor(s) {degenerate} must be strictly > 0: the usage arm's "
                "magnitude is Σ delta/floor — a floor of 0 divides by zero and a negative "
                "floor inverts that metric's ranking (item 4.2 admissibility A1)"
            )
        shares = [(m, v) for m, v in gen if m in SHARE_FLOORS and v >= 1.0]
        if shares:
            raise ValueError(
                f"share floor(s) {shares} must be < 1.0: a share cannot clear 1.0, so "
                "the axis would be silently disabled rather than tightened (item 4.2 "
                "admissibility A1)"
            )
        object.__setattr__(self, "generator", gen)

    @property
    def eligibility_map(self) -> dict[str, int]:
        return dict(self.eligibility)

    @property
    def breakout_floors(self) -> dict[str, float]:
        """The differenced floors (metric -> floor) this replay runs with."""
        return {m: v for m, v in self.generator if not m.startswith(EMERGENCE_PREFIX)}

    @property
    def emergence_floors(self) -> dict[str, float]:
        """The role-emergence absolute floors (metric -> floor)."""
        return {m[len(EMERGENCE_PREFIX):]: v for m, v in self.generator
                if m.startswith(EMERGENCE_PREFIX)}

    @property
    def generator_is_default(self) -> bool:
        return self.generator == default_generator()

    @property
    def generator_label(self) -> str:
        """The GENERATOR line of the HYPOTHESES block: what floors decided the
        pool, and whether they are the shipped ones."""
        shipped = dict(default_generator())
        moved = [(m, v) for m, v in self.generator if shipped.get(m) != v]
        floors = ", ".join(f"{m}>={v:g}" for m, v in self.generator)
        if not moved:
            return (
                "GENERATOR (hypothesis; the SHIPPED core/candidates floors, untuned): "
                f"usage arm qualifies a row if ANY differenced delta clears its floor "
                f"[{floors}]; 'emergence:' floors are the absolute-usage role-emergence "
                "path for a player with no prior week.  Part of the cache key."
            )
        moved_txt = ", ".join(f"{m}>={v:g} (shipped {shipped[m]:g})" for m, v in moved)
        return (
            "GENERATOR (hypothesis; OVERRIDDEN by this replay run — NOT the shipped "
            f"core/candidates floors): {moved_txt}; the rest as shipped [{floors}].  A "
            "tuning run: report on TRAIN seasons only.  Part of the cache key."
        )

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @property
    def params_hash(self) -> str:
        """sha256 of the sorted-key JSON of the parameters."""
        blob = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def cache_key(self) -> str:
        return self.params_hash[:12]


@dataclass(frozen=True)
class Decision:
    """One frozen pick: what the strategy said at ``as_of``, and nothing later.

    ``reasons`` is the generator's reason text verbatim (Rule 6) — the grade
    is meaningless to a novice without the sentence the tool printed.
    ``market_rank_r0`` / ``market_page_size_r0`` are the pre-event reference
    the pick was ELIGIBLE against; the scorecard re-reads the same page at
    grade time and asserts they agree.
    """

    season: int
    week: int
    as_of: str
    strategy: str
    rank_in_board: int
    board_rank: int
    gsis_id: str
    espn_id: str | None
    player: str
    position: str
    team: str | None
    signal_kind: str
    magnitude: float
    market_rank_r0: int | None
    market_page_size_r0: int | None
    r0_scrape_date: str | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True)
class WeekRecord:
    """One (strategy, season, week) of the replay — decided or not.

    An undecided week keeps its ``status`` and ``reason`` so the scorecard can
    print "N weeks ungradeable: <reason>" instead of a total that quietly
    shrank.  The generator bookkeeping (pool size, exclusions, captured log
    lines) is the same for every strategy of a week; it is repeated per
    record so a single JSONL line is self-describing.
    """

    season: int
    week: int
    as_of: str
    strategy: str
    k: int
    status: str
    reason: str | None
    decisions: tuple[Decision, ...]
    generator_rows: int
    usage_rows: int
    pool_size: int
    excluded_injury: int
    excluded_qb1: int
    excluded_ineligible: int
    excluded_no_gsis: int
    excluded_position: int
    r0_scrape_date: str | None
    r0_pages: tuple[str, ...]
    log_lines: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "r0_pages", tuple(self.r0_pages))
        object.__setattr__(
            self, "log_lines", tuple((str(m), int(n)) for m, n in self.log_lines)
        )

    @property
    def decided(self) -> bool:
        return self.status == WEEK_DECIDED


# ---------------------------------------------------------------------------
# freeze / load
# ---------------------------------------------------------------------------

MANIFEST = "manifest.json"


def record_to_json(rec: WeekRecord) -> str:
    """One deterministic JSON line per record (sorted keys, no whitespace)."""
    return json.dumps(dataclasses.asdict(rec), sort_keys=True, separators=(",", ":"))


def record_from_json(line: str) -> WeekRecord:
    raw = json.loads(line)
    decisions = tuple(Decision(**d) for d in raw.pop("decisions"))
    raw["r0_pages"] = tuple(raw["r0_pages"])
    raw["log_lines"] = tuple(tuple(x) for x in raw["log_lines"])
    return WeekRecord(decisions=decisions, **raw)


def record_key(rec: WeekRecord) -> tuple[str, int, int]:
    """The canonical order of a set of week records: the order ``load`` returns
    them in, whatever order the decide phase produced them."""
    return (rec.strategy, rec.season, rec.week)


def records_digest(records: Iterable[WeekRecord]) -> str:
    """sha256 over the JSON lines in canonical order — the determinism tests
    compare this, and a freeze read back must digest equal to the run that
    wrote it."""
    h = hashlib.sha256()
    for rec in sorted(records, key=record_key):
        h.update(record_to_json(rec).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def file_name(strategy: str, k: int, season: int) -> str:
    return f"{strategy}-k{k}-{season}.jsonl"


def freeze_dir(cache_dir: str | os.PathLike, params: ReplayParams) -> Path:
    return Path(cache_dir) / params.cache_key()


_TMP_COUNTER = itertools.count()


def _write_atomic(path: Path, data: bytes) -> None:
    """tmp + fsync + rename.  The temp name carries the writing PROCESS and a
    per-process counter: two writers aiming at one path must not interleave
    into a single shared ``<name>.tmp`` fd (item 4.2 audit — a fixed temp name
    turns a lost race into a torn survivor, or into a ``FileNotFoundError``
    when the winner's rename removes the shared temp under the loser)."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{next(_TMP_COUNTER)}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_grade_log(path: str | os.PathLike, payload: Mapping[str, object]) -> Path:
    """Append ONE json line to a grade log (§6.4), fsync'd.

    Lives here rather than in the runner because BOTH callers need it and
    ``backtest.replay`` cannot import ``backtest.tune`` (the runner imports the
    harness).  Every ``build_scorecard`` call made anywhere in 4.2 — the
    search, the G6 re-grades, the holdout episode — appends one line, so an
    undisclosed H- or owned-delta sweep is visible to an auditor.  A grade log
    written by only ONE of the two entry points cannot see the sweep it exists
    to expose (item 4.2 audit)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return p


def freeze(
    records: Sequence[WeekRecord],
    params: ReplayParams,
    *,
    cache_dir: str | os.PathLike,
    force: bool = False,
    written_at: str,
    db_path: str | None = None,
) -> Path:
    """Write the frozen decisions under ``cache_dir/<hash>/``.

    One JSONL per (strategy, season) plus a manifest naming the parameters
    and each file's sha256.  Refuses to overwrite an existing freeze unless
    ``force`` — a cache that changes underneath a grade run is exactly the
    leak the two-phase design exists to prevent.  Files land via
    temp + ``os.replace`` so a killed run leaves no half-written line.
    """
    target = freeze_dir(cache_dir, params)
    manifest_path = target / MANIFEST
    if manifest_path.exists() and not force:
        raise FileExistsError(
            f"{target} already holds a freeze for these parameters; pass force=True "
            "(--force) to overwrite it"
        )
    target.mkdir(parents=True, exist_ok=True)
    by_file: dict[str, list[WeekRecord]] = {}
    for rec in records:
        by_file.setdefault(file_name(rec.strategy, rec.k, rec.season), []).append(rec)
    files: dict[str, dict] = {}
    for name in sorted(by_file):
        recs = sorted(by_file[name], key=lambda r: (r.season, r.week))
        blob = ("".join(record_to_json(r) + "\n" for r in recs)).encode("utf-8")
        _write_atomic(target / name, blob)
        files[name] = {
            "sha256": hashlib.sha256(blob).hexdigest(),
            "weeks": len(recs),
            "decisions": sum(len(r.decisions) for r in recs),
        }
    manifest = {
        "params": params.to_json(),
        "params_hash": params.params_hash,
        "written_at": written_at,
        "db_path": db_path,
        "files": files,
    }
    _write_atomic(
        manifest_path,
        json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8") + b"\n",
    )
    return target


FREEZE_MISSING = "missing"
FREEZE_OK = "ok"


def freeze_status(cache_dir: str | os.PathLike, params: ReplayParams) -> str:
    """What the cache holds for ``params`` WITHOUT reading any decision.

    ``FREEZE_MISSING`` (no manifest), ``FREEZE_OK`` (manifest hash and every
    file digest verify) or a ``"corrupt: <reason>"`` string.  The CLI asks this
    before the decide phase so that re-running the done-when command reuses a
    verifying freeze instead of dying on ``FileExistsError`` — and refuses, by
    name, one that does not verify (``--force`` re-decides over it).  Reads no
    season data, so it needs no holdout unlock.
    """
    target = freeze_dir(cache_dir, params)
    manifest_path = target / MANIFEST
    if not manifest_path.exists():
        return FREEZE_MISSING
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"corrupt: {manifest_path} unreadable ({exc})"
    if manifest.get("params_hash") != params.params_hash:
        return (
            f"corrupt: {manifest_path} params_hash {manifest.get('params_hash')!r} "
            f"!= requested {params.params_hash}"
        )
    for name in sorted(manifest.get("files", {})):
        path = target / name
        if not path.exists():
            return f"corrupt: {path} named by the manifest is missing"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != manifest["files"][name].get("sha256"):
            return f"corrupt: {path} sha256 {digest} != manifest's"
    return FREEZE_OK


def load(
    cache_dir: str | os.PathLike, params: ReplayParams, *, unlock_holdout: bool = False
) -> list[WeekRecord]:
    """Read a freeze back; refuses a manifest whose hash disagrees or a file
    whose bytes do not match the manifest's digest.

    A freeze holding HOLDOUT seasons is refused (:class:`HoldoutLocked`) before
    a byte of it is read unless ``unlock_holdout`` — reading frozen 2024-25
    decisions to GRADE them is exactly the tuning loop the lock exists to make
    deliberate.
    """
    require_holdout_unlock(params.seasons, unlock_holdout=unlock_holdout)
    target = freeze_dir(cache_dir, params)
    manifest_path = target / MANIFEST
    if not manifest_path.exists():
        raise FileNotFoundError(f"no freeze under {target} (manifest missing)")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("params_hash") != params.params_hash:
        raise ValueError(
            f"{manifest_path}: params_hash {manifest.get('params_hash')!r} does not "
            f"match the requested parameters ({params.params_hash})"
        )
    out: list[WeekRecord] = []
    for name in sorted(manifest["files"]):
        blob = (target / name).read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if digest != manifest["files"][name]["sha256"]:
            raise ValueError(f"{target / name}: sha256 {digest} != manifest's")
        for line in blob.decode("utf-8").splitlines():
            if line.strip():
                out.append(record_from_json(line))
    out.sort(key=record_key)
    return out
