"""Item 4.2 — the pre-registered search runner (breakout-backtest.md §11.5).

A Python API with a thin ``__main__`` (parse, call, print — Rule 3).  It calls
the 4.1 harness IN-PROCESS (``backtest.replay`` / ``backtest.scorecards``),
never the CLI, over the FROZEN grid in :mod:`backtest.tune_grid`.

Per setting, in this order and nothing else: assert every §7.4 frozen value
(:func:`assert_frozen` — a failure is a RUNNER BUG and aborts the run, it is
never a result) → ``freeze_status`` → (``load`` if it verifies, else
``replay`` + ``freeze``; a ``corrupt:`` status is REFUSED and reported, never
``--force``'d) → ONE ``build_scorecard`` per strategy with BOTH sensitivity
lists empty → the admissibility screen A1–A12 (§6) → the paired comparison
against the DEFAULT (§5.2 / §5.3) → the result file, written atomically and
only once everything above is complete (publish-then-record).

Three things this module deliberately cannot do:

* it has NO holdout flag — every season it touches is asserted to be a TRAIN
  season before a byte is read, and ``unlock_holdout`` is ``False`` at every
  harness call (the two holdout commands are ``backtest.replay`` invocations,
  §8.3, never this runner);
* it REFUSES the live database — ``--db`` is required and any path resolving
  to ``<repo>/db/ziggurat.sqlite`` is refused (the search runs on the §11.5
  snapshot), and it refuses a ``--cache-dir`` resolving to the canonical
  ``data/backtest/replay/`` (Appendix A: untouched by the search, it holds the
  ledger).  BOTH refusals live in ``SearchConfig.__post_init__``, the one
  choke point every entry point — CLI or Python API — passes through;
* it refuses the 81st setting (``MAX_SETTINGS``, §4.5).  The §2.8 postflight
  and the G6 re-grades are not settings and are not made here.

Every ``build_scorecard`` call appends one line to the grade log (§6.4), so a
re-grade at another H or owned-delta — which leaves no trace in any freeze —
is visible to an auditor.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import multiprocessing as mp
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from backtest import decisions as D
from backtest import replay as R
from backtest import scorecards as S
from backtest import stats
from backtest import tune_grid as G

REPO_ROOT = R.REPO_ROOT
#: The live database — the one path this runner refuses (§11.5).
LIVE_DB = R.DEFAULT_DB
#: ``SEARCH_CACHE_DIR`` (§11.5): separate from the canonical ``replay/``.
SEARCH_CACHE_DIR = REPO_ROOT / "data" / "backtest" / "replay-4.2"
#: The CLI's ``--cache-dir`` default.  A separate name from ``SEARCH_CACHE_DIR``
#: because the test suite redirects THIS one (``tests/conftest.py``) so that a
#: test omitting ``--cache-dir`` can never land in the real search cache, while
#: the frozen path stays readable for the README/Appendix A checks.
DEFAULT_CACHE_DIR = SEARCH_CACHE_DIR
#: The canonical replay cache (Appendix A: "untouched by the search and holds
#: the ledger") — the second path this runner refuses, for the same class of
#: operator mistake the live-DB fence covers.
CANONICAL_CACHE_DIR = REPO_ROOT / "data" / "backtest" / "replay"
RESULTS_DIR = "results"
SUMMARY_FILE = "summary.jsonl"
GRADE_LOG = "grade-log.jsonl"
ROUND2_FILE = "round2.json"
FAMILY_FILE = "max-null.json"
FINGERPRINT_FILE = "fingerprint.json"
#: One appended record per INVOCATION (the single-file block is overwritten by
#: the next launch, so a resume would otherwise erase the launch in which the
#: fingerprint moved — F7 says results are never pooled across a move).
FINGERPRINT_LOG = "fingerprints.jsonl"
RUN_LOCK_FILE = ".search.lock"
RESULT_VERSION = 1

#: Appendix A ``PANEL_FINGERPRINT_BEFORE`` — the frozen TRAIN panel digests,
#: "to be re-taken on the snapshot before cell 1 and **asserted equal**".  The
#: recipe is Appendix A's verbatim: sha256 over ``repr()`` of the SQL-ordered
#: list of tuples, first 16 hex.  A digest computed any other way could never
#: be compared to these, which is what :func:`assert_panel_fingerprint` exists
#: to make impossible.
PANEL_FINGERPRINT_BEFORE: dict[str, dict[str, Any]] = {
    "2021": {"rows": 71757, "sha256_16": "47c395436f2fd23e"},
    "2022": {"rows": 62948, "sha256_16": "5ada72013746e972"},
    "2023": {"rows": 67293, "sha256_16": "d8205abba3540d90"},
}

#: §5.2 labels — printed beside both intervals, never a gate.
PER_WEEK_INTERVAL_LABEL = (
    "paired per-week Student-t (miscalibrated under sparsity — printed for continuity "
    "with 4.1, never a gate; the permutation p is the test)"
)
BLOCK_INTERVAL_LABEL = (
    "season-block Student-t, n=3 (miscalibrated under sparsity — printed for "
    "continuity with 4.1, never a gate)"
)
#: §6 A9 strata over the r0 depth-band labels.
STRATA: dict[str, tuple[str, ...]] = {
    "<=48": ("1-36", "37-48"),
    "49-100": ("49-60", "61-80", "81-100"),
    ">100|unranked": ("101-150", ">150", S.DEPTH_UNRANKED),
}
STRATUM_SHIFT_PP = 5.0
POSITION_SHIFT_PP = 10.0
TRAIN_SPLIT = "TRAIN"
THIN_BAND_LINES = 10
#: The fingerprint tables (§2.6 part 2) — the generator's own inputs.
FINGERPRINT_TABLES = ("weekly_stats", "snap_counts", "ngs_receiving", "ngs_rushing",
                      "ngs_passing", "sleeper_ownership")


class RunnerBug(AssertionError):
    """A §7.4 frozen value moved.  The run ABORTS — this is never a result."""


class LiveDatabaseRefused(ValueError):
    """``--db`` resolved to the live ``db/ziggurat.sqlite``."""


class CanonicalCacheRefused(ValueError):
    """``--cache-dir`` resolved to the canonical ``data/backtest/replay/``."""


class StaleCacheRefused(ValueError):
    """A cached result was produced from a DIFFERENT database state (F7)."""


class PanelFingerprintMismatch(RunnerBug):
    """The snapshot's TRAIN panel digests differ from Appendix A's frozen values."""


class SearchLocked(RuntimeError):
    """Another invocation already holds this cache dir's run lock."""


class FreezeRefused(ValueError):
    """A freeze under the cache dir does not verify (``corrupt:``) — refused, never forced."""


class BudgetExceeded(RuntimeError):
    """The 81st setting (§4.5 ``MAX_SETTINGS``)."""


class InfeasibleSetting(ValueError):
    """A setting the screen could not even pair (e.g. no graded week)."""


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchConfig:
    """Everything a setting is evaluated with.  The frozen §7.4 values are
    FIELDS rather than constants so :func:`assert_frozen` can refuse a moved
    one (that is what ``test_the_runner_refuses_a_setting_that_moves_hit_places``
    proves); the defaults are the pre-registered values."""

    db: Path
    cache_dir: Path
    jobs: int = 8
    hit_places: int = G.HIT_PLACES
    owned_delta: float = G.OWNED_DELTA
    grade_as_of: str = G.GRADE_AS_OF
    market: str = G.MARKET
    permutation_b: int = stats.PERMUTATION_DRAWS
    permutation_seed: int = stats.PERMUTATION_SEED
    max_settings: int = G.MAX_SETTINGS

    def __post_init__(self) -> None:
        """The two isolation refusals live HERE, not in ``main()``.

        §11.5 makes the live-DB refusal a property of the RUNNER, and §11.5
        also frames this module as "a Python API with a thin ``__main__``" — so
        a refusal implemented only in ``main()`` is absent from the sanctioned
        entry point (item 4.2 audit: ``run_round1``, ``run_round2``,
        ``evaluate_cell`` and the pool initializer each opened ``cfg.db``
        unchecked).  Every entry point passes through this constructor, so both
        fences are now unavoidable by construction.  A caller that injects its
        own CONNECTION still bypasses the path check — that is a deliberate
        test seam, not a hole this can close."""
        object.__setattr__(self, "db", refuse_live_database(self.db))
        object.__setattr__(self, "cache_dir", refuse_canonical_cache_dir(self.cache_dir))

    @property
    def results_dir(self) -> Path:
        return Path(self.cache_dir) / RESULTS_DIR

    @property
    def grade_log(self) -> Path:
        return Path(self.cache_dir) / GRADE_LOG


def refuse_live_database(path: str | os.PathLike) -> Path:
    """The snapshot rule (§11.5): any path resolving to the live DB is refused."""
    p = Path(path)
    resolved = p.resolve()
    live = LIVE_DB.resolve()
    same = resolved == live
    if not same and p.exists() and live.exists():
        try:
            same = os.path.samefile(p, live)
        except OSError:
            same = False
    if same:
        raise LiveDatabaseRefused(
            f"{path} resolves to the live database {LIVE_DB}; the search runs on the "
            "snapshot (data/backtest/ziggurat-4.2-snapshot.sqlite, §11.5) — pass --db <snapshot>"
        )
    return p


def refuse_canonical_cache_dir(path: str | os.PathLike) -> Path:
    """The cache-dir half of the isolation rule (Appendix A): the canonical
    ``data/backtest/replay/`` "is untouched by the search and holds the
    ledger", so a mistyped or copy-pasted ``--cache-dir data/backtest/replay``
    is refused by name rather than quietly dropping ~44 freezes plus five
    operational files into the directory an auditor enumerates."""
    p = Path(path)
    resolved = p.resolve()
    canonical = CANONICAL_CACHE_DIR.resolve()
    same = resolved == canonical
    if not same and p.exists() and canonical.exists():
        try:
            same = os.path.samefile(p, canonical)
        except OSError:
            same = False
    if same:
        raise CanonicalCacheRefused(
            f"{path} resolves to the canonical replay cache {CANONICAL_CACHE_DIR}, which holds "
            f"the holdout ledger and is untouched by the search; pass --cache-dir "
            f"{DEFAULT_CACHE_DIR} (SEARCH_CACHE_DIR, Appendix A)"
        )
    return p


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class run_lock:  # noqa: N801 — a context manager, named for what it is
    """An advisory exclusive lock on one cache dir for the life of the process.

    ``flock`` is released by the kernel when the holder dies, so a killed
    search never leaves a lock a resume has to break.  Two overlapping
    invocations against one cache dir are what turns ``freeze_status`` →
    ``freeze`` into a check-then-act race (item 4.2 audit), and a resume is a
    first-class mode here, so the second invocation is refused BY NAME rather
    than left to collide."""

    def __init__(self, cfg: SearchConfig) -> None:
        self.path = Path(cfg.cache_dir) / RUN_LOCK_FILE
        self._fd: int | None = None

    def __enter__(self) -> run_lock:
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SearchLocked(
                f"another invocation holds {self.path}; two searches sharing one cache dir "
                "race on freeze_status -> freeze (wait for it, or use a separate --cache-dir)"
            ) from None
        os.truncate(fd, 0)
        os.write(fd, f"{os.getpid()} {_utc_now()}\n".encode())
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)          # closing releases the flock
            self._fd = None


# ---------------------------------------------------------------------------
# §7.4 — the frozen values, asserted per setting before any read
# ---------------------------------------------------------------------------


def build_params(cell: G.Cell, *, seasons: Sequence[int] = G.TRAIN_SEASONS) -> D.ReplayParams:
    """The ``ReplayParams`` of a cell.  ``seasons`` exists ONLY so a test can
    prove :func:`assert_frozen` refuses a non-TRAIN season; the runner never
    passes it."""
    return D.ReplayParams(
        strategies=G.STRATEGIES, k=G.K, seasons=tuple(int(s) for s in seasons),
        market=G.MARKET, seed=G.SEED, weeks=G.WEEKS, generator=cell.generator,
    )


def assert_frozen(params: D.ReplayParams, cell: G.Cell, cfg: SearchConfig) -> None:
    """The ONE list (§6, "Runner assertions, per setting").  Order matters:
    the HOLDOUT check is first so a held season is refused by name before any
    other mismatch could mask it."""
    held = D.holdout_seasons(params.seasons)
    if held:
        raise RunnerBug(
            f"{cell.cell_id}: seasons {params.seasons} include HOLDOUT {held}; this runner "
            "has no holdout flag and never reads 2024-25 (§8.3: the holdout episode is a "
            "backtest.replay command)"
        )
    checks = [
        ("params.seasons", params.seasons, tuple(G.TRAIN_SEASONS)),
        ("params.market", params.market, G.MARKET),
        ("params.strategies", params.strategies, tuple(G.STRATEGIES)),
        ("k", params.k, G.K),
        ("seed", params.seed, G.SEED),
        ("weeks", params.weeks, G.WEEKS),
        ("hit_places", int(cfg.hit_places), G.HIT_PLACES),
        ("owned_delta", float(cfg.owned_delta), float(G.OWNED_DELTA)),
        ("grade_as_of", cfg.grade_as_of, G.GRADE_AS_OF),
        ("market", cfg.market, G.MARKET),
        ("eligibility", params.eligibility_map, dict(D.ELIGIBILITY_HYPOTHESIS)),
    ]
    for name, got, want in checks:
        if got != want:
            raise RunnerBug(f"{cell.cell_id}: {name} = {got!r}, frozen value is {want!r} (§7.4)")
    # the resolved 12-floor map == the literal row, exact float equality of
    # the parsed literals, checked as TWO maps so an `emergence:` name sent
    # through the differenced map (or vice versa) is caught (§3.7 / §4.3)
    want_breakout = {m: float(v) for m, v in zip(G.DIFFERENCED_AXES, cell.differenced, strict=True)}
    want_emergence = {m: float(v) for m, v in zip(G.EMERGENCE_AXES, cell.emergence, strict=True)}
    if params.breakout_floors != want_breakout:
        raise RunnerBug(
            f"{cell.cell_id}: breakout_floors {params.breakout_floors} != literal {want_breakout}"
        )
    if params.emergence_floors != want_emergence:
        raise RunnerBug(
            f"{cell.cell_id}: emergence_floors {params.emergence_floors} != literal "
            f"{want_emergence}"
        )
    if set(params.breakout_floors) != set(G.DIFFERENCED_AXES):
        raise RunnerBug(f"{cell.cell_id}: differenced map holds {sorted(params.breakout_floors)}")


# ---------------------------------------------------------------------------
# §2.6 — the fingerprint block, taken on the snapshot
# ---------------------------------------------------------------------------


def fingerprint(conn: sqlite3.Connection, *, seasons: Sequence[int] = G.TRAIN_SEASONS) -> dict:
    """Part 1: sha256[:16] of the sorted ``fpecr_panel`` tuples per TRAIN
    season; part 2: ``retrieved_as_of`` partition row counts per season of
    the generator's inputs.  Read-only, raw SQL — no as-of view applies, the
    point is to see EVERY partition."""
    for s in seasons:
        if s in D.HOLDOUT_SEASONS:
            raise RunnerBug(f"fingerprint over HOLDOUT season {s} refused")
    panel: dict[str, dict] = {}
    for s in seasons:
        rows = conn.execute(
            "SELECT season, nfl_week, ecr_type, fp_page, scrape_date, page_rank, gsis_id "
            "FROM fpecr_panel WHERE season = ? "
            "ORDER BY season, nfl_week, ecr_type, fp_page, scrape_date, page_rank, gsis_id",
            (int(s),),
        ).fetchall()
        # Appendix A's recipe VERBATIM: sha256 over repr() of the SQL-ordered
        # list of TUPLES, first 16 hex.  It has to be this exact spelling —
        # Appendix A pins three digests "to be re-taken on the snapshot before
        # cell 1 and asserted equal", and any other serialisation makes that
        # assertion unmakeable (the pre-4.2-audit code hashed per-row JSON and
        # so disagreed with all three frozen values on an UNMOVED panel).
        # `conn.row_factory` may be sqlite3.Row, whose repr() carries an
        # address — materialise plain tuples first.
        materialised = [tuple(r) for r in rows]
        digest = hashlib.sha256(repr(materialised).encode()).hexdigest()[:16]
        panel[str(s)] = {"rows": len(rows), "sha256_16": digest}
    partitions: dict[str, dict[str, dict[str, int]]] = {}
    for table in FINGERPRINT_TABLES:
        per_season: dict[str, dict[str, int]] = {}
        for s in seasons:
            rows = conn.execute(
                f"SELECT retrieved_as_of, COUNT(*) FROM {table} WHERE season = ? "
                "GROUP BY retrieved_as_of ORDER BY retrieved_as_of",
                (int(s),),
            ).fetchall()
            per_season[str(s)] = {str(r[0]): int(r[1]) for r in rows}
        partitions[table] = per_season
    return {"taken_at": _utc_now(), "seasons": [int(s) for s in seasons],
            "fpecr_panel": panel, "partitions": partitions}


def assert_panel_fingerprint(block: Mapping,
                             expected: Mapping[str, Mapping] = PANEL_FINGERPRINT_BEFORE) -> None:
    """Appendix A's "asserted equal": the snapshot's TRAIN panel digests and row
    counts must equal the frozen ones.  A mismatch means a TRAIN page was
    re-versioned (F7) — the run ABORTS before cell 1 rather than searching over
    a panel the pre-registration never saw."""
    panel = block.get("fpecr_panel") or {}
    bad: list[str] = []
    for season, want in expected.items():
        got = panel.get(season) or {}
        if got.get("sha256_16") != want["sha256_16"] or got.get("rows") != want["rows"]:
            bad.append(f"{season}: got {got.get('rows')} rows {got.get('sha256_16')}, "
                       f"Appendix A froze {want['rows']} rows {want['sha256_16']}")
    if bad:
        raise PanelFingerprintMismatch(
            "the snapshot's fpecr panel does not match Appendix A's PANEL_FINGERPRINT_BEFORE — "
            "a TRAIN page has been re-versioned (F7: results are never pooled across "
            "fingerprints); " + "; ".join(bad)
        )


def fingerprint_drift(before: Mapping, after: Mapping) -> dict:
    """The F7 verdict as a TESTED function of the two blocks, so the rule that
    decides whether ~80 settings may be pooled is not a boolean computed inline
    in ``main()`` and read by nobody.  Names WHICH seasons / tables moved."""
    panel_moved = sorted(
        s for s in set((before.get("fpecr_panel") or {})) | set((after.get("fpecr_panel") or {}))
        if (before.get("fpecr_panel") or {}).get(s) != (after.get("fpecr_panel") or {}).get(s)
    )
    b_part, a_part = before.get("partitions") or {}, after.get("partitions") or {}
    tables_moved = sorted(t for t in set(b_part) | set(a_part) if b_part.get(t) != a_part.get(t))
    unchanged = not panel_moved and not tables_moved
    return {"unchanged": unchanged, "panel_seasons_moved": panel_moved,
            "tables_moved": tables_moved,
            "verdict": ("db fingerprint UNCHANGED across the search"
                        if unchanged else
                        "FINGERPRINT CHANGED — results must not be pooled across it (F7): "
                        f"panel seasons {panel_moved or 'none'}, tables "
                        f"{tables_moved or 'none'}; every earlier setting must be re-run on "
                        "the new state, or the search restarted")}


def fingerprint_agrees(a: Mapping | None, b: Mapping | None) -> bool:
    """Do two blocks describe the same database STATE?  ``taken_at`` is ignored
    (it is when the block was read, not what it read)."""
    if a is None or b is None:
        return True                     # nothing to compare is not a disagreement
    return (dict(a.get("fpecr_panel") or {}) == dict(b.get("fpecr_panel") or {})
            and dict(a.get("partitions") or {}) == dict(b.get("partitions") or {}))


# ---------------------------------------------------------------------------
# the grade log (§6.4) and atomic JSON
# ---------------------------------------------------------------------------


def log_grade(cfg: SearchConfig, *, cache_key: str, strategy: str, market: str) -> None:
    """One line per ``build_scorecard`` call (§6.4).  The writer itself lives in
    ``decisions.py`` so ``backtest.replay``'s CLI — which makes the §8.3 holdout
    grades and the §7.2 G6 re-grades, the very calls that can move ``H`` — logs
    through the SAME function."""
    D.append_grade_log(cfg.grade_log, {
        "cache_key": cache_key, "strategy": strategy, "market": market,
        "places": int(cfg.hit_places), "owned_delta": float(cfg.owned_delta),
        "grade_as_of": cfg.grade_as_of, "timestamp": _utc_now(),
    })


def sanitise(obj: Any) -> Any:
    """inf/nan → strings (§2.7); tuples → lists; mappings keyed by tuples →
    ``"a,b"`` strings; dataclass-free."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return "nan"
        if math.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        return obj
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, Mapping):
        return {(",".join(str(x) for x in k) if isinstance(k, tuple) else str(k)): sanitise(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [sanitise(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return sanitise({k: getattr(obj, k) for k in obj.__dataclass_fields__})
    return str(obj)


def write_json_atomic(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    D._write_atomic(path, json.dumps(sanitise(payload), sort_keys=True, indent=1).encode() + b"\n")
    return path


def _self_digest(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "sha256"}
    return hashlib.sha256(
        json.dumps(sanitise(body), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def result_path(cfg: SearchConfig, cache_key: str) -> Path:
    return cfg.results_dir / f"{cache_key}.json"


#: The verify reasons that mean the FILE is damaged (as against a file that is
#: fine but was written under different frozen values, which is a runner bug and
#: is refused by :func:`assert_frozen` before anything is set aside).
CORRUPT_REASONS = ("unreadable", "version", "cache_key", "digest")


def verify_result(cfg: SearchConfig, cache_key: str) -> tuple[dict | None, str | None]:
    """``(payload, reason)`` for one result file: ``(payload, None)`` when it
    verifies, ``(None, reason)`` otherwise.  PURE — it never renames, deletes or
    writes anything.  That separation is the fix for an audit finding: the
    read-shaped ``load_result`` used to rename a file aside as a side effect,
    and ``run_round1`` calls it for all 44 cells BEFORE ``assert_frozen`` runs,
    so one invocation with a moved frozen value displaced the whole accumulated
    result set before the abort that was supposed to precede any write."""
    path = result_path(cfg, cache_key)
    if not path.exists():
        return None, "absent"
    if not (Path(cfg.cache_dir) / cache_key / D.MANIFEST).exists():
        return None, "freeze-manifest-gone"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, AttributeError, OSError):
        return None, "unreadable"
    if not isinstance(payload, dict):
        return None, "unreadable"
    if payload.get("version") != RESULT_VERSION:
        return None, "version"
    if payload.get("cache_key") != cache_key:
        return None, "cache_key"
    if not (payload.get("hit_places") == int(cfg.hit_places)
            and payload.get("owned_delta") == float(cfg.owned_delta)
            and payload.get("grade_as_of") == cfg.grade_as_of
            and payload.get("market") == cfg.market):
        return None, "frozen-values"
    if payload.get("sha256") != _self_digest(payload):
        return None, "digest"
    return payload, None


def load_result(cfg: SearchConfig, cache_key: str) -> dict | None:
    """The verified result file for ``cache_key``, or ``None``.  NON-DESTRUCTIVE
    (see :func:`verify_result`); a damaged file is set aside by
    :func:`quarantine_result`, called only from the evaluate paths and only
    AFTER ``assert_frozen`` has passed."""
    return verify_result(cfg, cache_key)[0]


def quarantine_result(cfg: SearchConfig, cache_key: str, reason: str,
                      *, log: Callable[[str], None] | None = None) -> Path | None:
    """Rename a DAMAGED result file aside so the setting is recomputed.

    The aside name carries microseconds AND the pid and is claimed with
    ``O_EXCL``, so two corrupt payloads for one key — or two workers reaching
    the same shared DEFAULT — cannot collapse onto one filename and silently
    overwrite the evidence this rename exists to keep.  Losing the race is
    success: somebody else already set the file aside."""
    path = result_path(cfg, cache_key)
    for _ in range(64):
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S.%f")
        aside = path.with_suffix(f".corrupt-{stamp}-{os.getpid()}")
        try:
            fd = os.open(aside, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        try:
            os.replace(path, aside)
        except FileNotFoundError:
            os.unlink(aside)
            return None                 # another writer already set it aside
        if log is not None:
            log(f"result {cache_key} did not verify ({reason}) -> {aside.name}")
        return aside
    raise RunnerBug(f"could not claim an aside name for {path}")


def _interval(iv: stats.Interval | None) -> dict | None:
    if iv is None:
        return None
    return {"mean": iv.mean, "lo": iv.lo, "hi": iv.hi, "sd": iv.sd, "n": iv.n,
            "confidence": iv.confidence, "kind": iv.kind}


# ---------------------------------------------------------------------------
# the freeze → grade path of ONE setting
# ---------------------------------------------------------------------------


def obtain_records(conn: sqlite3.Connection, cfg: SearchConfig, params: D.ReplayParams,
                   *, progress: Callable[[str], None] | None = None) -> tuple[list, str]:
    """``freeze_status`` → ``load`` if it verifies, else ``replay`` + ``freeze``.
    A ``corrupt:`` status is REFUSED by name (never forced).  The one benign
    case is a killed freeze: JSONL without a manifest reads ``FREEZE_MISSING``
    and the setting simply re-decides over it."""
    status = D.freeze_status(cfg.cache_dir, params)
    if status == D.FREEZE_OK:
        return list(D.load(cfg.cache_dir, params, unlock_holdout=False)), "load"
    if status != D.FREEZE_MISSING:
        raise FreezeRefused(
            f"the freeze under {D.freeze_dir(cfg.cache_dir, params)} does not verify "
            f"({status}); this runner never --force's a freeze — move it aside and re-run"
        )
    records = R.replay(conn, params, progress=progress, unlock_holdout=False)
    try:
        D.freeze(records, params, cache_dir=cfg.cache_dir, force=False, written_at=_utc_now(),
                 db_path=os.path.abspath(cfg.db))
    except FileExistsError:
        # a concurrent writer got there between the status read and the freeze
        # (check-then-act).  If what it wrote verifies, LOAD it — this runner
        # never --force's — otherwise refuse by name rather than let the
        # operator see a bare traceback whose text suggests --force.
        again = D.freeze_status(cfg.cache_dir, params)
        if again == D.FREEZE_OK:
            return list(D.load(cfg.cache_dir, params, unlock_holdout=False)), "load"
        raise FreezeRefused(
            f"another writer froze {D.freeze_dir(cfg.cache_dir, params)} while this setting was "
            f"deciding and it does not verify ({again}); move it aside and re-run"
        ) from None
    return list(records), "decide+freeze"


def grade_all(conn: sqlite3.Connection, cfg: SearchConfig, params: D.ReplayParams,
              records: Sequence[D.WeekRecord]) -> dict[str, S.MarketScorecard]:
    """ONE ``build_scorecard`` per strategy, both sensitivity lists EMPTY,
    ``unlock_holdout=False``; every call logged (§6.4)."""
    cards: dict[str, S.MarketScorecard] = {}
    key = params.cache_key()
    for strategy in params.strategies:
        card = S.build_scorecard(
            conn, records, params, strategy=strategy, market=cfg.market,
            grade_as_of=cfg.grade_as_of, places=cfg.hit_places, owned_delta=cfg.owned_delta,
            places_sensitivity=(), owned_sensitivity=(), unlock_holdout=False,
        )
        log_grade(cfg, cache_key=key, strategy=strategy, market=cfg.market)
        cards[strategy] = card
    return cards


def pool_stats(records: Sequence[D.WeekRecord]) -> dict:
    pools = sorted(int(r.pool_size) for r in records
                   if r.strategy == R.PER_WEEK_STRATEGY and r.decided)
    if not pools:
        return {"n": 0, "min": None, "p10": None, "median": None, "max": None, "per_week": {}}
    return {
        "n": len(pools), "min": pools[0],
        "p10": stats.nearest_rank_percentile(pools, 0.10),
        "median": statistics.median(pools), "max": pools[-1],
        "per_week": {f"{r.season},{r.week}": int(r.pool_size) for r in records
                     if r.strategy == R.PER_WEEK_STRATEGY and r.decided},
    }


def _strata_mass(picks_by_band: Sequence[tuple[str, int, int]]) -> dict[str, float]:
    n_by = {b: n for b, _, n in picks_by_band}
    total = sum(n_by.values())
    out = {}
    for stratum, bands in STRATA.items():
        out[stratum] = (sum(n_by.get(b, 0) for b in bands) / total) if total else 0.0
    return out


def _strata_hits(picks_by_band: Sequence[tuple[str, int, int]]) -> dict[str, tuple[int, int]]:
    out = {}
    for stratum, bands in STRATA.items():
        h = sum(hh for b, hh, _ in picks_by_band if b in bands)
        n = sum(nn for b, _, nn in picks_by_band if b in bands)
        out[stratum] = (h, n)
    return out


def _standardised_hit_rate(g_bands, ref_bands) -> dict:
    """A9's re-read: the setting's per-stratum hit rate re-weighted by the
    DEFAULT's stratum weights, renormalised over the strata BOTH populate;
    the dropped weight mass is disclosed."""
    g_hits, ref_hits = _strata_hits(g_bands), _strata_hits(ref_bands)
    ref_mass = _strata_mass(ref_bands)
    both = [s for s in STRATA if g_hits[s][1] > 0 and ref_hits[s][1] > 0]
    weight = sum(ref_mass[s] for s in both)
    if not both or weight == 0:
        return {"rate": None, "strata": both, "dropped_mass": 1.0}
    rate = sum(ref_mass[s] * g_hits[s][0] / g_hits[s][1] for s in both) / weight
    return {"rate": rate, "strata": both, "dropped_mass": 1.0 - weight,
            "per_stratum": {s: {"hits": g_hits[s][0], "n": g_hits[s][1]} for s in STRATA}}


def _position_mix(picks: Sequence[S.PickGrade]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    total = len(picks)
    for pos in sorted({p.position for p in picks}):
        sub = [p for p in picks if p.position == pos]
        hits = sum(1 for p in sub if p.hit)
        out[pos] = {"n": len(sub), "share": len(sub) / total if total else 0.0,
                    "hits": hits, "hit_rate": hits / len(sub) if sub else None}
    return out


def slot_map(slots: Sequence[Sequence]) -> dict[tuple[int, int, int], tuple[str, bool]]:
    """``(season, week, rank_in_board) -> (gsis_id, hit)`` from a result's slot list."""
    return {(int(s), int(w), int(r)): (str(g), bool(h)) for s, w, r, g, h, *_ in slots}


def paired_hits(slots_g: Sequence, slots_ref: Sequence) -> dict:
    """§5.4 (c): ``D_hit`` on the INTERSECTION slots gradeable under both, with
    the McNemar discordant counts (``b`` = default-hit / cell-miss, ``c`` =
    default-miss / cell-hit), the dropped-slot counts and A8's differing count."""
    g, ref = slot_map(slots_g), slot_map(slots_ref)
    common = sorted(set(g) & set(ref))
    diffs = [float(g[k][1]) - float(ref[k][1]) for k in common]
    b = sum(1 for k in common if ref[k][1] and not g[k][1])
    c = sum(1 for k in common if g[k][1] and not ref[k][1])
    differing = sum(1 for k in common if g[k][0] != ref[k][0])
    mc = stats.mcnemar_exact(b, c, alpha=G.PERMUTATION_ALPHA)
    iv = stats.t_interval(diffs, kind="paired per-slot") if diffs else None
    return {
        "mean": (sum(diffs) / len(diffs)) if diffs else None,
        "interval": _interval(iv), "n_common": len(common),
        "dropped_g": len(set(g) - set(ref)), "dropped_default": len(set(ref) - set(g)),
        "b": mc.b, "c": mc.c, "p": mc.p, "p_two": mc.p_two,
        "significant_negative": mc.significant_negative, "differing_gsis": differing,
    }


def paired_lift(l_g: Mapping, l_ref: Mapping, cfg: SearchConfig) -> dict:
    """§5.2 / §5.3: ``d_w`` on the intersection, ``D``, both labelled
    intervals, the per-season means, and the sign-flip permutation."""
    vg = {_parse_key(k): float(v) for k, v in l_g.items()}
    vr = {_parse_key(k): float(v) for k, v in l_ref.items()}
    if not vg or not vr:
        raise InfeasibleSetting(
            f"no per-week depth-matched lift to pair: setting has {len(vg)} contributing "
            f"weeks, default has {len(vr)}"
        )
    if not set(vg) & set(vr):
        raise InfeasibleSetting("no common contributing week with the default")
    paired = stats.paired_by_key(vg, vr)
    perm = stats.sign_flip_permutation(paired.diff_vector, b=cfg.permutation_b,
                                       seed=cfg.permutation_seed)
    return {
        "mean": paired.mean,
        "per_week_interval": _interval(paired.per_key) | {"label": PER_WEEK_INTERVAL_LABEL},
        "block_interval": _interval(paired.block) | {"label": BLOCK_INTERVAL_LABEL},
        "n_common": paired.n_common, "only_g": list(paired.only_a),
        "only_default": list(paired.only_b), "equal_count_keys": paired.equal_count_keys,
        "per_season": dict(paired.per_season), "per_season_n": dict(paired.per_season_n),
        "d_w": {f"{k[0]},{k[1]}": v for k, v in paired.diffs},
        # C19 (external review, 2026-09-04): `m` is the count of NON-ZERO paired
        # weekly differences — the only weeks that carry sign information.  The
        # exact one-sided p cannot fall below 2**-m, so a cell at m <= 4 has
        # `min_attainable_p` >= 0.0625 > PERMUTATION_ALPHA and CANNOT clear the
        # gate whatever its effect.  Recorded beside `p` everywhere `p` is
        # reported so an arithmetically unsatisfiable comparison is visible on
        # the row rather than reconstructed later (14 of the 45 graded cells sat
        # at m <= 4, all three `receptions` levels among them).
        "permutation": {"p": perm.p, "p_two": perm.p_two, "n": perm.n, "b": perm.b,
                        "seed": perm.seed, "ge_count": perm.ge_count,
                        "abs_ge_count": perm.abs_ge_count,
                        "m": _m_informative(paired.diffs),
                        "min_attainable_p": 2.0 ** -_m_informative(paired.diffs)},
    }


def _m_informative(diffs) -> int:
    """``m``: how many paired weekly differences are non-zero (C19).

    A zero difference is an unflipped constant under the sign-flip null, so it
    carries no sign information; only these ``m`` weeks do.  The exact
    one-sided p therefore has an attainable floor of ``2**-m``.
    """
    return sum(1 for _k, v in diffs if float(v) != 0.0)


def _parse_key(k: object) -> tuple[int, int]:
    if isinstance(k, str):
        a, b = k.split(",")
        return int(a), int(b)
    a, b = k  # type: ignore[misc]
    return int(a), int(b)


def per_week_vector(result: Mapping, *, pooled: bool = False) -> dict[tuple[int, int], float]:
    """A result's ``L_w`` (or its season-pooled variant) as a ``(season, week)`` map."""
    src = result.get("L_w_pooled" if pooled else "L_w") or {}
    return {_parse_key(k): float(v) for k, v in src.items()}


def diff_vector(result: Mapping) -> dict[tuple[int, int], float]:
    """A result's ``d_w`` as a ``(season, week)`` map (empty when infeasible)."""
    d = (result.get("D") or {}).get("d_w") or {}
    return {_parse_key(k): float(v) for k, v in d.items()}


def _check(kind: str, ok: bool, value: Any) -> dict:
    return {"kind": kind, "pass": bool(ok), "value": value}


def screen(result: dict, reference: Mapping | None) -> dict:
    """§6 A1–A12 over an assembled result.  HARD → argmax-INELIGIBLE (recorded,
    not ranked); FLAG → annotated, stays ranked.  Returns the ``admissibility``
    map; the caller derives ``hard_failures`` / ``flags`` from it."""
    a: dict[str, dict] = {}
    floors = result["floors"]
    a["A1"] = _check("hard", all(v > 0 for v in floors.values())
                     and all(floors[k] < 1 for k in floors if k.endswith(("share", "pct"))),
                     floors)
    a["A2"] = _check("hard", result["weeks_decided"] == 54 and result["decisions"] == 162,
                     {"weeks_decided": result["weeks_decided"], "decisions": result["decisions"]})
    a["A3"] = _check("hard", result["null_gradeable"] == 8853
                     and not result["generator_failures"],
                     {"null_gradeable": result["null_gradeable"],
                      "generator_failures": len(result["generator_failures"])})
    pool = result["pool"]
    a["A4"] = _check("hard", pool["median"] is not None and pool["median"] >= 20
                     and pool["min"] >= 9, {"median": pool["median"], "min": pool["min"]})
    a["A5"] = _check("hard", result["n_weeks"] >= 45, result["n_weeks"])
    a["A6"] = _check("hard", result["depth_unmatched"] == 0, result["depth_unmatched"])
    a["A7"] = _check("hard", pool["median"] is not None and pool["median"] <= 155,
                     pool["median"])
    dh = result.get("D_hit")
    differing = dh["differing_gsis"] if dh else 0
    if reference is None:
        # the DEFAULT reference pairs against ITSELF, so `differing` is 0 by
        # construction.  §6.2 defines INERT relative to the default; applied to
        # the default it is vacuous, and recording it as a HARD failure makes
        # the baseline row — the row every other row is measured against, and
        # the row §7.1(3) may name the winner — read as inadmissible.  The
        # COUNT is still carried (§6 D7 wants all twelve values); only the
        # verdict says "not screened against itself".
        a["A8"] = _check("hard", True, {"differing_gsis": differing,
                                        "self_comparison": True,
                                        "note": "A8 is defined against the DEFAULT; the "
                                                "reference cell is not screened against itself"})
    else:
        a["A8"] = _check("hard", differing >= 5, differing)
    if reference is not None:
        mass_g, mass_r = (_strata_mass(result["picks_by_band"]),
                          _strata_mass(reference["picks_by_band"]))
        shift = {s: 100.0 * (mass_g[s] - mass_r[s]) for s in STRATA}
        flagged = any(abs(v) > STRATUM_SHIFT_PP for v in shift.values())
        a["A9"] = _check("flag", not flagged, {
            "shift_pp": shift,
            "band_standardised": (_standardised_hit_rate(result["picks_by_band"],
                                                         reference["picks_by_band"])
                                  if flagged else None)})
    else:
        a["A9"] = _check("flag", True, None)
    a["A10"] = _check("hard", dh is not None and dh["mean"] is not None and dh["mean"] >= 0
                      and not dh["significant_negative"],
                      None if dh is None else {"D_hit": dh["mean"], "b": dh["b"], "c": dh["c"],
                                               "p": dh["p"],
                                               "significant_negative": dh["significant_negative"]})
    dd = result.get("D")
    n_common = dd["n_common"] if dd else 0
    a["A11"] = _check("hard", n_common >= 40, n_common)
    if reference is not None:
        mix_g, mix_r = result["position_mix"], reference["position_mix"]
        shift_pos = {p: 100.0 * (mix_g.get(p, {}).get("share", 0.0)
                                 - mix_r.get(p, {}).get("share", 0.0))
                     for p in sorted(set(mix_g) | set(mix_r))}
        flagged = any(abs(v) > POSITION_SHIFT_PP for v in shift_pos.values())
        a["A12"] = _check("flag", not flagged, {
            "shift_pp": shift_pos,
            "stratified": ({p: {"setting": mix_g.get(p, {}).get("hit_rate"),
                                "default": mix_r.get(p, {}).get("hit_rate")}
                            for p in shift_pos} if flagged else None)})
    else:
        a["A12"] = _check("flag", True, None)
    return a


def _summarise_card(card: S.MarketScorecard) -> dict:
    s = card.split(TRAIN_SPLIT)
    return {
        "weeks_decided": s.weeks_decided, "decisions": s.decisions,
        "gradeable": s.gradeable, "ungradeable": s.ungradeable,
        "graded": s.lift_depth_pooled.n if s.lift_depth_pooled else 0,
        "precision": {f"p@{k}": {"hits": h, "n": n,
                                 "wilson": _interval(stats.wilson_interval(h, n)) if n else None}
                      for k, h, n in s.precision},
        "null_hits": s.null_hits, "null_gradeable": s.null_gradeable,
        "M": _interval(s.lift_depth_pooled),
        "M_block": _interval(s.lift_depth_block),
        "per_season_M": {str(k): v for k, v in s.per_season_lift_depth},
        "depth_fallbacks": s.depth_fallbacks, "depth_unmatched": s.depth_unmatched,
        "truncated": s.truncated, "picks_by_band": [list(t) for t in s.picks_by_band],
        "null_by_band": [list(t) for t in s.null_by_band],
    }


def _thin_band(card: S.MarketScorecard, graded: Sequence[S.PickGrade]) -> dict:
    nulls_map = {(n.season, n.week): n for n in card.nulls}
    totals = S.season_band_totals(nulls_map)
    thin = sorted((season, band, n) for (season, band), (_, n) in totals.items()
                  if n < THIN_BAND_LINES)
    thin_set = {(s, b) for s, b, _ in thin}
    on_thin = sum(1 for p in graded if (p.season, p.depth_band) in thin_set)
    out = {"lines_floor": THIN_BAND_LINES, "cells": thin, "picks_on_thin_lines": on_thin,
           "M_season_pooled": None, "L_w_pooled": None,
           "note": ("no (season, band) cell fell under the floor, so the season-pooled variant "
                    "of M and D does not apply to this setting (§6 thin-band sensitivity) — "
                    "the two None fields below are NOT-APPLICABLE, not NOT-COMPUTED")}
    if thin and graded:
        pooled = S.depth_matched_lifts(graded, {}, band_totals=totals)
        per_pick = list(pooled.per_pick)
        out["M_season_pooled"] = _interval(stats.t_interval(
            per_pick, kind="season-pooled band rate")) if len(per_pick) > 1 else None
        # per_week holds the per-PICK lifts of that week (a list); the vector the
        # paired comparison needs is one number per week — the same fmean
        # `_summarise` applies to the primary form.  Without it the pooled
        # variant of D raised an uncaught TypeError inside paired_lift and the
        # §6 disclosure could never be produced (item 4.2 audit).
        out["L_w_pooled"] = {f"{s},{w}": statistics.fmean(v)
                             for (s, w), v in sorted(pooled.per_week.items()) if v}
        out["note"] = (f"{len(thin)} (season, band) cell(s) hold < {THIN_BAND_LINES} null lines; "
                       "M and D are ALSO reported against the season-pooled band table")
    return out


def _band_table(picks_by_band, null_by_band) -> list[dict]:
    """One row per depth band (the two card tables are joined on the label;
    a band with no pick still prints its null line)."""
    picks = {b: (h, n) for b, h, n in picks_by_band}
    nulls = {b: (h, n) for b, h, n in null_by_band}
    return [{"band": b, "hits": picks.get(b, (0, 0))[0], "n": picks.get(b, (0, 0))[1],
             "null_hits": nulls.get(b, (0, 0))[0], "null_n": nulls.get(b, (0, 0))[1]}
            for b in list(S.DEPTH_BAND_LABELS) + [S.DEPTH_UNRANKED]
            if b in picks or b in nulls]


def assemble(cell: G.Cell, params: D.ReplayParams, cfg: SearchConfig, records: Sequence,
             cards: Mapping[str, S.MarketScorecard], *, reference: Mapping | None,
             phase: str, wall_seconds: float, fingerprint_block: Mapping | None) -> dict:
    """Every §11.5 field for one setting (pure over the cards)."""
    card = cards[R.PER_WEEK_STRATEGY]
    signal = _summarise_card(card)
    graded = [p for p in card.picks if p.gradeable]
    l_w = card.per_week_depth_lift(TRAIN_SPLIT)
    per_season_weeks: dict[int, int] = {}
    for (season, _w) in l_w:
        per_season_weeks[season] = per_season_weeks.get(season, 0) + 1
    shipped = G.DEFAULT_CELL.floors
    result: dict[str, Any] = {
        "version": RESULT_VERSION, "cell_id": cell.cell_id, "axis": cell.axis,
        "level": cell.level, "round": cell.round, "note": cell.note,
        "eligible": bool(cell.eligible), "cache_key": params.cache_key(),
        "params_hash": params.params_hash, "floors": dict(cell.floors),
        "overrides": dict(cell.overrides),
        "log_ratios": {k: math.log(v / shipped[k]) for k, v in cell.floors.items()
                       if v != shipped[k]},
        "phase": phase, "freeze_dir": str(D.freeze_dir(cfg.cache_dir, params)),
        "wall_seconds": wall_seconds, "hit_places": cfg.hit_places,
        "owned_delta": cfg.owned_delta, "market": cfg.market, "grade_as_of": cfg.grade_as_of,
        "seasons": list(params.seasons), **signal,
        "n_weeks": len(l_w), "per_season_weeks": per_season_weeks,
        "pool": pool_stats(records),
        "generator_failures": [list(f) for f in card.generator_failures],
        "L_w": {f"{s},{w}": v for (s, w), v in sorted(l_w.items())},
        "slots": [[p.season, p.week, p.rank_in_board, p.gsis_id, bool(p.hit), p.depth_band]
                  for p in graded],
        "picks_by_week": {f"{r.season},{r.week}": sorted(d.gsis_id for d in r.decisions)
                          for r in records if r.strategy == R.PER_WEEK_STRATEGY and r.decided},
        "position_mix": _position_mix(graded),
        "band_table": _band_table(signal["picks_by_band"], signal["null_by_band"]),
        "thin_band": _thin_band(card, graded),
        "strategies": {k: _summarise_card(v) for k, v in cards.items()
                       if k != R.PER_WEEK_STRATEGY},
        "hypotheses": list(card.hypotheses), "fingerprint": fingerprint_block,
        "reference_cache_key": None if reference is None else reference["cache_key"],
        "written_at": _utc_now(), "infeasible": None, "inert": None,
        "D": None, "D_hit": None, "M_minus_default": None, "D_pooled": None,
    }
    result["L_w_pooled"] = result["thin_band"].pop("L_w_pooled")
    for name in ("L_w", "L_w_pooled"):
        for key, value in (result[name] or {}).items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise RunnerBug(
                    f"{cell.cell_id}: {name}[{key!r}] is {type(value).__name__}, not a number — "
                    "a per-week vector must be ONE value per week before it is paired"
                )
    ref = reference if reference is not None else result
    try:
        result["D"] = paired_lift(result["L_w"], ref["L_w"], cfg)
        if result["M"] and ref.get("M"):
            result["M_minus_default"] = result["M"]["mean"] - ref["M"]["mean"]
        if result["L_w_pooled"] and ref.get("L_w_pooled"):
            result["D_pooled"] = paired_lift(result["L_w_pooled"], ref["L_w_pooled"], cfg)
    except InfeasibleSetting as exc:
        result["infeasible"] = str(exc)
    result["D_hit"] = paired_hits(result["slots"], ref["slots"])
    # INERT is a property of a cell versus the DEFAULT (§6.2).  For the default
    # itself the comparison is a tautology, so the verdict is None — "not
    # applicable" — rather than a `true` that reads as "the shipped floors are
    # inert".  (A10 / A11 are reference-relative too but pass trivially on the
    # self-comparison: D_hit mean 0.0, n_common = the default's own week count.)
    result["inert"] = (None if reference is None
                       else result["D_hit"]["differing_gsis"] < 5)
    adm = screen(result, reference)
    result["admissibility"] = adm
    result["hard_failures"] = [k for k, v in adm.items() if v["kind"] == "hard" and not v["pass"]]
    result["flags"] = [k for k, v in adm.items() if v["kind"] == "flag" and not v["pass"]]
    result["argmax_eligible"] = bool(cell.eligible and not result["hard_failures"]
                                     and result["infeasible"] is None)
    return result


def count_settings(cfg: SearchConfig) -> int:
    """Distinct cache keys with a result file (full OR decide-only)."""
    if not cfg.results_dir.exists():
        return 0
    return len({p.stem for p in cfg.results_dir.glob("*.json")})


def _connection(conn_or_factory) -> sqlite3.Connection:
    """A connection, or a zero-arg factory returning one (``sqlite3.Connection``
    is itself callable, so the type is checked, not ``callable()``)."""
    if isinstance(conn_or_factory, sqlite3.Connection):
        return conn_or_factory
    return conn_or_factory()


def _resume(cfg: SearchConfig, key: str, fingerprint_block: Mapping | None,
            log: Callable[[str], None] | None = None) -> dict | None:
    """The resume read of ONE setting, with the two things a plain
    ``load_result`` cannot do:

    * a DAMAGED file is set aside here — after ``assert_frozen`` — so a moved
      frozen value aborts before any file is touched;
    * a result produced from a DIFFERENT database state is REFUSED by name.
      ``db_path`` is deliberately outside ``params_hash`` (§11.5), so without
      this check a cache dir populated from snapshot A silently supplies part
      of the grid when the search is resumed against snapshot B and the argmax
      is chosen across two databases (F7: never pool across fingerprints)."""
    payload, reason = verify_result(cfg, key)
    if payload is None:
        if reason in CORRUPT_REASONS:
            quarantine_result(cfg, key, reason, log=log)
        return None
    stored = payload.get("fingerprint")
    if not fingerprint_agrees(stored, fingerprint_block):
        raise StaleCacheRefused(
            f"the cached result {key} was produced from a DIFFERENT database state than this "
            "invocation reads (its stored §2.6 fingerprint block disagrees); F7 forbids pooling "
            "results across fingerprints — re-run the search on one snapshot, or move the "
            f"result files under {cfg.results_dir} aside"
        )
    return payload


def evaluate_cell(conn_or_factory, cell: G.Cell, cfg: SearchConfig, *,
                  reference: Mapping | None = None, seasons: Sequence[int] = G.TRAIN_SEASONS,
                  fingerprint_block: Mapping | None = None,
                  progress: Callable[[str], None] | None = None) -> dict:
    """One setting end to end (§6.4): assert every frozen value → resume from a
    verifying result file → ``freeze_status`` → load or replay+freeze → ONE
    ``build_scorecard`` per strategy → screen → pair against the DEFAULT →
    atomic result file.  ``reference`` is the default's result (None for the
    default itself, which pairs against itself: every ``d_w`` is 0)."""
    params = build_params(cell, seasons=seasons)
    assert_frozen(params, cell, cfg)
    key = params.cache_key()
    existing = _resume(cfg, key, fingerprint_block, progress)
    if existing is not None and not existing.get("decide_only"):
        return existing
    if reference is None and cell.cell_id != G.DEFAULT_CELL_ID:
        reference = _resume(cfg, build_params(G.DEFAULT_CELL, seasons=seasons).cache_key(),
                            fingerprint_block)
        if reference is None or reference.get("decide_only"):
            raise RunnerBug("the DEFAULT must be evaluated before any other setting")
    if existing is None and count_settings(cfg) >= cfg.max_settings:
        raise BudgetExceeded(
            f"{count_settings(cfg)} settings already evaluated; the frozen cap is "
            f"{cfg.max_settings} (§8.3) — refusing {cell.cell_id}")
    t0 = time.perf_counter()
    conn = _connection(conn_or_factory)
    records, phase = obtain_records(conn, cfg, params, progress=progress)
    cards = grade_all(conn, cfg, params, records)
    result = assemble(cell, params, cfg, records, cards, reference=reference, phase=phase,
                      wall_seconds=time.perf_counter() - t0, fingerprint_block=fingerprint_block)
    write_result(cfg, result)
    return result


def write_result(cfg: SearchConfig, result: dict) -> Path:
    body = sanitise({k: v for k, v in result.items() if k != "sha256"})
    body["sha256"] = _self_digest(body)
    result["sha256"] = body["sha256"]
    path = result_path(cfg, result["cache_key"])
    write_json_atomic(path, body)
    return path


def decide_only(conn_or_factory, cell: G.Cell, cfg: SearchConfig, *,
                seasons: Sequence[int] = G.TRAIN_SEASONS) -> dict:
    """A DECIDE-ONLY trial (§4.4 step 5): freeze (or load) and report the pool
    statistics; no grade, no scorecard, no d vector.  Counts against the cap."""
    params = build_params(cell, seasons=seasons)
    assert_frozen(params, cell, cfg)
    key = params.cache_key()
    existing = _resume(cfg, key, None)
    if existing is not None:
        return existing
    if count_settings(cfg) >= cfg.max_settings:
        raise BudgetExceeded(f"{cfg.max_settings} settings already spent — refusing {cell.cell_id}")
    conn = _connection(conn_or_factory)
    records, phase = obtain_records(conn, cfg, params)
    result = {
        "version": RESULT_VERSION, "decide_only": True, "cell_id": cell.cell_id,
        "axis": cell.axis, "level": cell.level, "round": cell.round, "note": cell.note,
        "eligible": False, "argmax_eligible": False, "cache_key": key,
        "params_hash": params.params_hash, "floors": dict(cell.floors), "phase": phase,
        "hit_places": cfg.hit_places, "owned_delta": cfg.owned_delta, "market": cfg.market,
        "grade_as_of": cfg.grade_as_of, "pool": pool_stats(records),
        "written_at": _utc_now(),
    }
    write_result(cfg, result)
    return result


# ---------------------------------------------------------------------------
# round 1 — the pre-registered grid, in parallel
# ---------------------------------------------------------------------------

_WORKER_CONN: sqlite3.Connection | None = None
_WORKER_FINGERPRINT: dict | None = None
_WORKER_INIT_ERROR: str | None = None


def _init_worker(db: str, fingerprint_block: dict | None) -> None:
    """A pool initializer must NEVER raise: CPython respawns workers
    indefinitely when it does, so ``imap_unordered`` neither returns nor
    raises and round 1 hangs forever (item 4.2 audit — the failure class items
    3.1/3.1b were fixed for).  The failure is stored and re-raised as a TASK
    error instead, which propagates to the parent at once."""
    global _WORKER_CONN, _WORKER_FINGERPRINT, _WORKER_INIT_ERROR
    _WORKER_FINGERPRINT = fingerprint_block
    try:
        _WORKER_CONN = R.open_ro(db)
        _WORKER_CONN.execute("SELECT 1").fetchone()
        _WORKER_INIT_ERROR = None
    except Exception as exc:                                # noqa: BLE001 — reported, not swallowed
        _WORKER_CONN = None
        _WORKER_INIT_ERROR = f"{type(exc).__name__}: {exc}"


def _worker_evaluate(args: tuple[str, SearchConfig]) -> tuple[str, str]:
    cell_id, cfg = args
    if _WORKER_CONN is None:
        raise RunnerBug(
            f"worker could not open {cfg.db} ({_WORKER_INIT_ERROR or 'no connection'}) — "
            "round 1 aborts rather than respawning workers forever")
    result = evaluate_cell(_WORKER_CONN, G.cell_by_id(cell_id), cfg,
                           fingerprint_block=_WORKER_FINGERPRINT)
    return cell_id, result["cache_key"]


def load_results(cfg: SearchConfig) -> dict[str, dict]:
    """Every verifying FULL result file, keyed by cache key."""
    out: dict[str, dict] = {}
    if cfg.results_dir.exists():
        for path in sorted(cfg.results_dir.glob("*.json")):
            r = load_result(cfg, path.stem)
            if r is not None and not r.get("decide_only"):
                out[r["cache_key"]] = r
    return out


def run_round1(cfg: SearchConfig, *, cells: Sequence[G.Cell] = G.ROUND1_CELLS,
               evaluate: Callable | None = None, conn: sqlite3.Connection | None = None,
               fingerprint_block: Mapping | None = None,
               log: Callable[[str], None] = print) -> dict[str, dict]:
    """The DEFAULT first (serial — everything pairs against it), then every
    other round-1 cell in a spawn pool of ``cfg.jobs`` workers (one read-only
    connection each).  Returns ``{cell_id: result}``; the summary is rebuilt
    from the result files afterwards by the ONE writer."""
    if cells[0].cell_id != G.DEFAULT_CELL_ID:
        raise RunnerBug("round 1 must evaluate the DEFAULT first")
    # §7.4 first, before ANY filesystem read or write: the pending scan below
    # touches every cell's result file, so a moved frozen value has to abort
    # before it — "a runner bug, not a result: the run aborts" (item 4.2 audit).
    for cell in cells:
        assert_frozen(build_params(cell), cell, cfg)
    pending = [c for c in cells if load_result(cfg, build_params(c).cache_key()) is None]
    if count_settings(cfg) + len(pending) > cfg.max_settings:
        raise BudgetExceeded(
            f"{count_settings(cfg)} settings done + {len(pending)} pending exceeds the "
            f"frozen cap {cfg.max_settings} (§8.3)")
    use_pool = evaluate is None and conn is None and cfg.jobs > 1 and len(cells) > 1
    if evaluate is None:
        if conn is None:
            conn = R.open_ro(cfg.db)
            conn.execute("SELECT 1").fetchone()   # openability proven in the PARENT

        def evaluate(cell: G.Cell, reference: Mapping | None) -> dict:  # noqa: E306
            return evaluate_cell(conn, cell, cfg, reference=reference,
                                 fingerprint_block=fingerprint_block)

    results: dict[str, dict] = {}
    default = evaluate(cells[0], None)
    results[default["cell_id"]] = default
    log(f"round 1: default {default['cache_key']} ({default['phase']}, "
        f"{default['wall_seconds']:.1f} s, {default['n_weeks']} weeks)")
    rest = list(cells[1:])
    if use_pool and rest:
        ctx = mp.get_context("spawn")
        with ctx.Pool(min(cfg.jobs, len(rest)), initializer=_init_worker,
                      initargs=(str(cfg.db), dict(fingerprint_block or {}) or None)) as pool:
            for cell_id, key in pool.imap_unordered(
                    _worker_evaluate, [(c.cell_id, cfg) for c in rest]):
                back = load_result(cfg, key)
                if back is None:
                    # the worker reported this key as evaluated and the parent
                    # cannot read it back: a data-loss event, not a cell to
                    # record as None and log as a success (item 4.2 audit — the
                    # cell then vanished from the summary, from round 2's
                    # candidate set and from the §5.3 max-null family, while
                    # the run finished reporting success).
                    raise RunnerBug(
                        f"{cell_id} was evaluated as {key} but its result file does not verify "
                        f"({verify_result(cfg, key)[1]}); refusing to record it as absent")
                results[cell_id] = back
                log(f"round 1: {cell_id} -> {key}")
    else:
        for cell in rest:
            r = evaluate(cell, default)
            results[cell.cell_id] = r
            log(f"round 1: {cell.cell_id} -> {r['cache_key']}")
    rebuild_summary(cfg)
    return results


def summary_row(r: Mapping) -> dict:
    d = r.get("D") or {}
    perm = d.get("permutation") or {}
    dh = r.get("D_hit") or {}
    return {
        "cell_id": r["cell_id"], "cache_key": r["cache_key"], "axis": r["axis"],
        "level": r["level"], "round": r["round"], "eligible": r["eligible"],
        "argmax_eligible": r.get("argmax_eligible", False),
        "decide_only": bool(r.get("decide_only", False)),
        "hard_failures": r.get("hard_failures", []), "flags": r.get("flags", []),
        "infeasible": r.get("infeasible"), "inert": r.get("inert"),
        "M": (r.get("M") or {}).get("mean"), "M_minus_default": r.get("M_minus_default"),
        "D": d.get("mean"), "p": perm.get("p"), "m": perm.get("m"),
        "min_attainable_p": perm.get("min_attainable_p"),
        "n_common": d.get("n_common"),
        "per_season_D": d.get("per_season"), "D_hit": dh.get("mean"),
        "n_weeks": r.get("n_weeks"), "graded": r.get("graded"),
        "pool_median": (r.get("pool") or {}).get("median"),
        "wall_seconds": r.get("wall_seconds"), "phase": r.get("phase"),
    }


def rebuild_summary(cfg: SearchConfig) -> Path:
    """``summary.jsonl`` is REBUILT from the result files by one writer (never
    appended by workers), so a killed run can never leave a torn line."""
    rows = []
    if cfg.results_dir.exists():
        for path in sorted(cfg.results_dir.glob("*.json")):
            r = load_result(cfg, path.stem)
            if r is not None:
                rows.append(summary_row(r))
    rows.sort(key=lambda x: (x["round"], not x["cell_id"] == G.DEFAULT_CELL_ID, x["cell_id"]))
    path = Path(cfg.cache_dir) / SUMMARY_FILE
    D._write_atomic(path, "".join(json.dumps(sanitise(x), sort_keys=True) + "\n"
                                  for x in rows).encode())
    return path


# ---------------------------------------------------------------------------
# round 2 — §4.4, in that order, every attempt counted
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BisectionResult:
    c: float | None
    median: float | None
    status: str                       # matched | unmatched | exhausted
    trials: tuple[tuple[float, float], ...]
    target: float
    tolerance: float


def bisect_scale(target: float, *, decide: Callable[[float], float],
                 lo: float = G.BISECTION_RANGE[0], hi: float = G.BISECTION_RANGE[1],
                 tolerance: float = G.BISECTION_POOL_TOLERANCE,
                 max_trials: int = G.BISECTION_DECIDE_TRIALS) -> BisectionResult:
    """§4.4 step 5: find the global scale ``c`` whose DECIDE-ONLY median pool
    lands within ``tolerance`` rows of ``target``.  The endpoints are tried
    first (a target outside ``[pool(hi), pool(lo)]`` is ``unmatched``, since
    the pool is monotone non-increasing in ``c``); at most ``max_trials``
    decide calls in total; running out is ``exhausted``, reported with the
    nearest trial, never rounded up to a match."""
    trials: list[tuple[float, float]] = []

    def probe(c: float) -> float:
        m = float(decide(c))
        trials.append((c, m))
        return m

    def done(c, m, status):
        return BisectionResult(c=c, median=m, status=status, trials=tuple(trials),
                               target=target, tolerance=tolerance)

    m_lo = probe(lo)
    if abs(m_lo - target) <= tolerance:
        return done(lo, m_lo, "matched")
    if len(trials) >= max_trials:
        return done(None, None, "exhausted")
    m_hi = probe(hi)
    if abs(m_hi - target) <= tolerance:
        return done(hi, m_hi, "matched")
    if target > m_lo or target < m_hi:
        return done(None, None, "unmatched")
    a, b = lo, hi
    while len(trials) < max_trials:
        mid = round((a + b) / 2, 4)
        m = probe(mid)
        if abs(m - target) <= tolerance:
            return done(mid, m, "matched")
        if m > target:
            a = mid          # pool too big → floors too low → larger c
        else:
            b = mid
    return done(None, None, "exhausted")


def _d(r: Mapping) -> float | None:
    return (r.get("D") or {}).get("mean")


def _p(r: Mapping) -> float | None:
    return ((r.get("D") or {}).get("permutation") or {}).get("p")


def _m(r: Mapping) -> int | None:
    """The cell's informative-week count (C19) — always reported beside ``_p``."""
    return ((r.get("D") or {}).get("permutation") or {}).get("m")


def _min_p(r: Mapping) -> float | None:
    """``2**-m``: the smallest one-sided p this cell could ever return (C19)."""
    m = _m(r)
    return None if m is None else 2.0 ** -m


def _log_ratio_norm(r: Mapping) -> float:
    return sum(abs(v) for v in (r.get("log_ratios") or {}).values())


def _argmax(rows: Sequence[Mapping]) -> Mapping | None:
    """Highest ``D``; ties within ``TIE_BAND`` → the smaller total |log ratio|."""
    ranked = [r for r in rows if _d(r) is not None]
    if not ranked:
        return None
    best = max(_d(r) for r in ranked)
    tied = [r for r in ranked if _d(r) >= best - G.TIE_BAND]
    return min(tied, key=lambda r: (_log_ratio_norm(r), r["cell_id"]))


def _relabel_ineligible(cfg: SearchConfig, result: dict, reason: str) -> None:
    """Mark an evaluated round-2 row argmax-INELIGIBLE, in memory AND on disk.

    Used for a SKIPPED forward attempt: the cell is built eligible (it is a
    forward nested combination) and only the keep decision, taken after it is
    evaluated, makes it a diagnostic.

    The caller only reaches this for a row written FOR that attempt: two chain
    cells can resolve to ONE 12-floor map, and a chain attempt whose map equals
    a round-1 single-axis cell's resumes that cell's row — which is the same
    SETTING and is round-1 eligible, so relabelling it would strip an eligible
    round-1 cell out of the argmax and the max-null family."""
    stored = load_result(cfg, result.get("cache_key") or "")
    result["eligible"] = False
    result["argmax_eligible"] = False
    result["argmax_ineligible_reason"] = reason
    if stored is not None and stored.get("cell_id") == result.get("cell_id"):
        write_result(cfg, result)


def _greedy_chain(order: Sequence[str], candidates: Mapping[str, Mapping], default: Mapping,
                  evaluate: Callable, cfg: SearchConfig, *, prefix: str, budget: int) -> dict:
    """The forward (or reverse) greedy of §4.4 step 3 / step 6.  Every
    evaluated attempt is recorded — kept or skipped — and every attempt
    the budget prevented is listed as ``not_attempted``."""
    floors = dict(default["floors"])
    prev: Mapping = default
    prev_cleared_g1 = False       # the default's D is 0 by construction
    attempts: list[dict] = []
    kept: list[dict] = []
    evaluated: list[Mapping] = []
    not_attempted: list[str] = []
    for axis in order:
        cand = candidates.get(axis)
        if cand is None:
            continue
        if len(attempts) >= budget:
            not_attempted.append(cand["cell_id"])
            continue
        floors[axis] = cand["floors"][axis]
        # §7.1 / §4.4 step 6: only the FORWARD nested combinations are
        # argmax-eligible.  The reverse replicate is argmax-INELIGIBLE by label
        # ("its disagreement is never resolved by picking whichever scored
        # higher"), so its rows must not be written carrying an eligible LABEL —
        # that label is the pre-registered enforcement mechanism ("the runner
        # enforces eligibility by cell label, never by score") and it is what
        # summary.jsonl, a resume and §7.3's LOSO read.
        cell = G.combined_cell(dict(floors), cell_id=f"{prefix}:{len(attempts) + 1}:{cand['cell_id']}",
                               axis=prefix, level=cand["level"], eligible=(prefix == "fwd"),
                               note=f"{prefix} greedy over {cand['cell_id']}")
        res = evaluate(cell, default)
        evaluated.append(res)
        try:
            inc = paired_lift(res["L_w"], prev["L_w"], cfg) if res.get("L_w") else None
        except InfeasibleSetting as exc:
            inc = {"infeasible": str(exc)}
        inc_p = (inc or {}).get("permutation", {}).get("p")
        inc_m = (inc or {}).get("permutation", {}).get("m")
        inc_d = (inc or {}).get("mean")
        d_now = _d(res)
        clears_g1 = d_now is not None and d_now >= G.PRACTICAL_FLOOR
        # §4.4 step 3 is "KEEP it iff BOTH (a) ... AND (b) ...", and it is those
        # two conditions ONLY.  A third rejection reason (the HARD screen) was a
        # silent strengthening of a frozen selection rule, so it is gone: the
        # screen is enforced where §7.1 says it is — at the argmax, which filters
        # `argmax_eligible` — and a screen-failing chain member is RECORDED here
        # (`hard_failures` / `infeasible` on the attempt row) rather than
        # changing the nesting nothing pre-registered it to change.
        reasons = []
        if inc_p is None or not inc_p < G.PERMUTATION_ALPHA:
            # C19: m rides with p.  When 2**-m >= alpha the comparison was
            # arithmetically unsatisfiable and the reason has to say so — the
            # cell did not fail on direction, it could not have passed.
            floor = None if inc_m is None else 2.0 ** -inc_m
            unsat = ("" if floor is None or floor < G.PERMUTATION_ALPHA else
                     f" (UNSATISFIABLE: m={inc_m} informative weeks, so the exact "
                     f"one-sided p cannot fall below 2**-m = {floor:g})")
            reasons.append(f"increment p={inc_p} (m={inc_m}) not < "
                           f"{G.PERMUTATION_ALPHA}{unsat}")
        if prev_cleared_g1 and not clears_g1:
            reasons.append(f"predecessor cleared G1 (+{G.PRACTICAL_FLOOR}) and this does not")
        keep = not reasons
        attempts.append({
            "cell_id": res["cell_id"], "cache_key": res["cache_key"], "axis": axis,
            "level": cand["level"], "kept": keep, "increment_D": inc_d,
            "increment_p": inc_p, "increment_m": inc_m,
            "increment_min_attainable_p": (None if inc_m is None else 2.0 ** -inc_m),
            "D_vs_default": d_now, "p_vs_default": _p(res),
            "m_vs_default": _m(res), "min_attainable_p_vs_default": _min_p(res),
            "clears_G1": clears_g1, "reasons": reasons, "floors": dict(floors),
            "hard_failures": list(res.get("hard_failures") or []),
            "infeasible": res.get("infeasible"),
            "argmax_eligible": bool(res.get("argmax_eligible")),
        })
        if not keep and prefix == "fwd" and res["cell_id"] == cell.cell_id:
            # a SKIPPED forward attempt is argmax-INELIGIBLE (§7.1) — the label
            # on disk has to say so, or an argmax taken over rows LABELLED
            # eligible could crown a rejected attempt
            _relabel_ineligible(cfg, res, f"skipped by §4.4 step 3: {'; '.join(reasons)}")
        if keep:
            kept.append(res)
            prev = res
            prev_cleared_g1 = clears_g1
        else:
            floors[axis] = prev["floors"][axis]        # a skipped addition is undone
    return {"order": list(order), "attempts": attempts,
            "kept": [a for a in attempts if a["kept"]],
            "skipped": [a for a in attempts if not a["kept"]],
            "not_attempted": not_attempted, "final_floors": dict(floors),
            "final_cache_key": prev["cache_key"], "final_cell_id": prev["cell_id"],
            "final_D": _d(prev), "kept_results": kept, "evaluated": evaluated}


def pick_candidates(results: Mapping[str, Mapping]) -> tuple[dict[str, Mapping], list[dict]]:
    """§4.4 step 1: per single-axis, the argmax-eligible, permutation-cleared
    level with the highest D (ties → smaller |log ratio|)."""
    cands: dict[str, Mapping] = {}
    skipped: list[dict] = []
    for axis in G.ROUND2_AXIS_ORDER:
        rows = [r for r in results.values() if r["axis"] == axis and r["round"] == 1
                and r.get("argmax_eligible") and not r.get("inert")
                and _p(r) is not None and _p(r) < G.PERMUTATION_ALPHA]
        best = _argmax(rows)
        if best is None:
            # C19: report each level's m and 2**-m, so an axis that was
            # arithmetically unopenable (every level at m <= 4) is legible as
            # that, not as an axis that failed on evidence.
            levels = [r for r in results.values()
                      if r["axis"] == axis and r["round"] == 1 and not r.get("inert")]
            per_level = {r["cell_id"]: {"p": _p(r), "m": _m(r),
                                        "min_attainable_p": _min_p(r),
                                        "argmax_eligible": bool(r.get("argmax_eligible"))}
                         for r in levels}
            blocked = [c for c, v in per_level.items()
                       if v["min_attainable_p"] is not None
                       and v["min_attainable_p"] >= G.PERMUTATION_ALPHA]
            reason = "no argmax-eligible level with p < 0.05"
            if levels and len(blocked) == len(levels):
                reason += (f" — and NO level could have one: all {len(levels)} sit at "
                           f"m <= {max(v['m'] for v in per_level.values())} informative "
                           f"weeks, where the exact floor 2**-m >= {G.PERMUTATION_ALPHA}")
            skipped.append({"axis": axis, "reason": reason, "levels": per_level,
                            "unsatisfiable_levels": blocked})
        else:
            cands[axis] = best
    return cands, skipped


def churn(a: Mapping, b: Mapping) -> dict:
    """Fraction of common decided weeks whose top-k gsis SET differs."""
    pa, pb = a.get("picks_by_week") or {}, b.get("picks_by_week") or {}
    common = sorted(set(pa) & set(pb))
    differ = sum(1 for w in common if sorted(pa[w]) != sorted(pb[w]))
    return {"weeks": len(common), "differing": differ,
            "fraction": differ / len(common) if common else None}


def run_round2(cfg: SearchConfig, *, evaluate: Callable | None = None,
               decide: Callable[[G.Cell], float] | None = None,
               conn: sqlite3.Connection | None = None,
               fingerprint_block: Mapping | None = None,
               log: Callable[[str], None] = print) -> dict:
    """§4.4 exactly: candidates → forward greedy (≤8) → argmax → LOO on the
    winner (≤8) → scale-only bisection (≤6 decide trials, one control grade)
    → reverse replicate (≤8, disagreement REPORTED — forward stays winner) →
    the max-null family → ``round2.json`` + ``max-null.json``."""
    assert_frozen(build_params(G.DEFAULT_CELL), G.DEFAULT_CELL, cfg)   # before any read
    results = load_results(cfg)
    by_id = {r["cell_id"]: r for r in results.values()}
    default = by_id.get(G.DEFAULT_CELL_ID)
    if default is None:
        raise RunnerBug("round 2 needs round 1's DEFAULT result on disk")
    # §11.5: every row carries the fingerprint block "as read on the snapshot".
    # Round 2 stamps the block THIS invocation read; falling back to the stored
    # default row's copy would record a fingerprint round 2 never read, so a
    # post-hoc audit of the rows could pass FALSELY after a re-snapshot.
    fp_block = fingerprint_block if fingerprint_block is not None else default.get("fingerprint")
    if evaluate is None:
        conn = conn or R.open_ro(cfg.db)

        def evaluate(cell: G.Cell, reference: Mapping) -> dict:  # noqa: E306
            return evaluate_cell(conn, cell, cfg, reference=reference,
                                 fingerprint_block=fp_block)
    if decide is None:
        conn = conn or R.open_ro(cfg.db)

        def decide(cell: G.Cell) -> float:  # noqa: E306
            return decide_only(conn, cell, cfg)["pool"]["median"]

    before = count_settings(cfg)
    report: dict[str, Any] = {"started_at": _utc_now(), "settings_before": before,
                              "fingerprint": sanitise(fp_block)}
    # step 1 — candidates
    cands, skipped_axes = pick_candidates(results)
    report["candidates"] = {a: {"cell_id": r["cell_id"], "D": _d(r), "p": _p(r),
                                "m": _m(r), "min_attainable_p": _min_p(r)}
                            for a, r in cands.items()}
    report["skipped_axes"] = skipped_axes
    log(f"round 2: {len(cands)} candidate axes, {len(skipped_axes)} skipped")
    # step 3 — forward greedy
    fwd = _greedy_chain(G.ROUND2_AXIS_ORDER, cands, default, evaluate, cfg,
                        prefix="fwd", budget=G.FORWARD_BUDGET)
    kept_results = fwd.pop("kept_results")
    fwd_evaluated = fwd.pop("evaluated")
    report["forward"] = fwd
    # argmax over eligible round-1 cells + the kept chain, then §7.1 rule 2
    ranked = [r for r in results.values() if r["round"] == 1 and r.get("argmax_eligible")]
    ranked += [r for r in kept_results if r.get("argmax_eligible")]
    scores = [_d(r) for r in ranked if _d(r) is not None]
    best = max(scores) if scores else None
    # §7.1 rule 2: "The tie set is every eligible screen-passing setting with
    # D(g) >= D_max - 0.005.  If the default is in it (D_max < 0.005), the
    # default wins."  D(default) is 0 by construction, so the default IS in the
    # tie set whenever D_max < TIE_BAND — including every case where nothing
    # beat the shipped floors at all.  It can never be in `ranked` (it carries
    # eligible=False by pre-registration), so the rule has to be applied here
    # or it can never fire, and a flat or negative search would be reported as
    # a tuned winner (item 4.2 audit).  Rule 3: "the incumbent is the default,
    # and it wins by default."
    tie_to_default = best is None or best < G.TIE_BAND
    winner = default if tie_to_default else (_argmax(ranked) or default)
    per_season_w = ((winner.get("D") or {}).get("per_season") or {})
    g4_seasons = {str(s): v for s, v in per_season_w.items()}
    report["winner"] = {"cell_id": winner["cell_id"], "cache_key": winner["cache_key"],
                        "D": _d(winner), "p": _p(winner), "m": _m(winner),
                        "min_attainable_p": _min_p(winner), "floors": winner["floors"],
                        "is_default": winner["cell_id"] == G.DEFAULT_CELL_ID,
                        "clears_G1": (_d(winner) or 0.0) >= G.PRACTICAL_FLOOR,
                        # G4 (§7.2): the per-season mean of d_w is > 0 in each of
                        # 2021, 2022 and 2023 — a computed verdict, not left to a
                        # reader of three numbers
                        "clears_G4": (len(per_season_w) == len(G.TRAIN_SEASONS)
                                      and all(v > 0 for v in per_season_w.values())),
                        "per_season_D": g4_seasons,
                        "tie_to_default": tie_to_default,
                        "observed_best_D": best,
                        "tie_band": G.TIE_BAND,
                        "ranked": len(ranked),
                        "argmax_screen_passing": len(ranked)}
    if tie_to_default:
        report["winner"]["tie_reason"] = (
            f"no eligible screen-passing setting reached D >= {G.TIE_BAND} "
            f"(best {best}); the default is in the tie set and WINS (§7.1 rule 2/3)")
    log(f"round 2: winner {winner['cell_id']} D={_d(winner)}"
        f"{' (ties -> the default, §7.1 rule 2)' if tie_to_default else ''}")
    # step 4 — leave-one-out on the winner
    loo: dict[str, Any] = {"attempts": [], "skipped": None, "not_attempted": []}
    moved = [k for k, v in winner["floors"].items() if v != G.DEFAULT_CELL.floors[k]]
    if winner["cell_id"] == G.DEFAULT_CELL_ID or not moved:
        loo["skipped"] = "the argmax is the default: nothing to leave out"
    else:
        for i, axis in enumerate(moved):
            if len(loo["attempts"]) >= G.LOO_BUDGET:
                loo["not_attempted"].append(axis)
                continue
            floors = dict(winner["floors"])
            floors[axis] = G.DEFAULT_CELL.floors[axis]
            cell = G.combined_cell(floors, cell_id=f"loo:{i + 1}:{axis}", axis="loo",
                                   level=f"restore {axis}", eligible=False,
                                   note=f"leave-one-out of {winner['cell_id']}")
            res = evaluate(cell, default)
            loo["attempts"].append({
                "axis": axis, "cell_id": res["cell_id"], "cache_key": res["cache_key"],
                "D": _d(res), "p": _p(res), "m": _m(res), "min_attainable_p": _min_p(res),
                "drop_from_winner": (None if _d(res) is None or _d(winner) is None
                                     else _d(winner) - _d(res)),
                "hard_failures": res.get("hard_failures"), "infeasible": res.get("infeasible"),
            })
    # G5 (§7.2) as a COMPUTED verdict.  "If the winner is a single-axis round-1
    # cell, its one ablation IS the default and G5 is satisfied vacuously —
    # stated as such in the findings note": that ablation resolves to the
    # DEFAULT's own cache key, so its D is 0.0 and a reader applying G5's
    # sentence literally would score the winner as G5-FAILED.  Say it instead.
    default_key = build_params(G.DEFAULT_CELL).cache_key()
    vacuous = [a for a in loo["attempts"] if a["cache_key"] == default_key]
    if loo["skipped"] or (vacuous and len(loo["attempts"]) == 1):
        loo["clears_G5"] = True
        loo["G5_vacuous"] = True
        loo["G5_note"] = (
            "the winner's only single-floor removal IS the default (§4.4 step 4), so G5 is "
            "satisfied VACUOUSLY (§7.2) — the 0.0 on that row is the default paired against "
            "itself, not a collapse of the winner's gain")
    else:
        below = [a for a in loo["attempts"]
                 if a["cache_key"] != default_key
                 and (a["D"] is None or a["D"] < G.PRACTICAL_FLOOR)]
        loo["clears_G5"] = not below and not loo["not_attempted"]
        loo["G5_vacuous"] = bool(vacuous)
        loo["G5_note"] = (
            "no single-floor removal takes D below the practical floor"
            if loo["clears_G5"] else
            f"{len(below)} ablation(s) below +{G.PRACTICAL_FLOOR}"
            + (f"; {len(loo['not_attempted'])} not attempted (budget)"
               if loo["not_attempted"] else "")
            + (f"; {len(vacuous)} ablation(s) ARE the default (vacuous, §7.2)"
               if vacuous else ""))
    report["loo"] = loo
    # step 5 — scale-only control matched on median pool (decide-only bisection)
    control: dict[str, Any] = {"bisection": None, "grade": None, "churn": None, "skipped": None}
    target = (winner.get("pool") or {}).get("median")
    if winner["cell_id"] == G.DEFAULT_CELL_ID:
        control["skipped"] = "the argmax is the default: the scale-only control IS the default"
    elif target is None:
        control["skipped"] = "the winner has no median pool"
    else:
        trials_seen: dict[float, str] = {}

        def decide_c(c: float) -> float:
            cell = G.scaled_cell(c, cell_id=f"bisect:global_scale={c!r}",
                                 note="decide-only bisection trial")
            trials_seen[c] = cell.cell_id
            return decide(cell)

        bis = bisect_scale(float(target), decide=decide_c)
        control["bisection"] = {"status": bis.status, "c": bis.c, "median": bis.median,
                                "target": bis.target, "tolerance": bis.tolerance,
                                "trials": [list(t) for t in bis.trials]}
        if bis.status == "matched":
            cell = G.scaled_cell(bis.c, cell_id=f"control:global_scale={bis.c!r}",
                                 note="scale-only control matched on median pool")
            res = evaluate(cell, default)
            control["grade"] = {"cell_id": res["cell_id"], "cache_key": res["cache_key"],
                                "D": _d(res), "p": _p(res), "m": _m(res),
                                "min_attainable_p": _min_p(res),
                                "pool_median": (res.get("pool") or {}).get("median"),
                                "hard_failures": res.get("hard_failures")}
            control["churn"] = churn(winner, res)
            control["winner_minus_control_D"] = (
                None if _d(res) is None or _d(winner) is None else _d(winner) - _d(res))
            log(f"round 2: control c={bis.c} D={_d(res)} churn={control['churn']}")
        else:
            log(f"round 2: scale-only control {bis.status} — reported, no control grade")
    report["control"] = control
    # step 6 — reverse replicate
    rev = _greedy_chain(G.ROUND2_REVERSE_ORDER, cands, default, evaluate, cfg,
                        prefix="rev", budget=G.REVERSE_BUDGET)
    rev.pop("kept_results")
    rev.pop("evaluated")
    fwd_axes = [a["axis"] for a in fwd["kept"]]
    rev_axes = [a["axis"] for a in rev["kept"]]
    rev["disagrees"] = sorted(fwd_axes) != sorted(rev_axes) or rev["final_floors"] != fwd["final_floors"]
    rev["forward_stays_winner"] = True
    rev["forward_kept_axes"], rev["reverse_kept_axes"] = fwd_axes, rev_axes
    report["reverse"] = rev
    # The family (§5.3) is LABEL-based, and the label is `eligible` ALONE:
    # "every argmax-eligible-LABEL cell evaluated, INCLUDING those that fail
    # the HARD screen or are inert (their d_w vectors enter as computed; this
    # can only raise the bar)".  Filtering on `argmax_eligible` (label AND
    # screen AND feasible) DROPS members, and because T*_b is a pointwise max
    # that can only LOWER the 95th-percentile bar — the one direction §5.3 says
    # cannot happen, on the item's only multiplicity control (item 4.2 audit).
    # Round-2 forward attempts enter whether kept or skipped, by the same rule.
    family: dict[str, dict] = {}
    for r in results.values():
        if r["round"] == 1 and r.get("eligible") and diff_vector(r):
            family[r["cell_id"]] = diff_vector(r)
    for a in fwd["attempts"]:
        r = _find_result(a["cache_key"], fwd_evaluated, cfg)
        if r is not None and diff_vector(r):
            family[a["cell_id"]] = diff_vector(r)
    # §7.3 wants TWO numbers quoted: the size of the label-based max-null family
    # the bar was computed over, BESIDE the size of the argmax-eligible
    # screen-passing set the argmax ran over.  They are different sets.
    report["family"] = {"members": sorted(family), "size": len(family),
                        "label_based_size": len(family),
                        "argmax_screen_passing_size": len(ranked),
                        "rule": "argmax-ELIGIBLE-LABEL round-1 cells (screen failures and inert "
                                "cells INCLUDED, §5.3) + every forward attempt, kept or skipped; "
                                "never default / flood / LOO / reverse / control"}
    if family:
        mn = stats.max_null_step_down(family, b=cfg.permutation_b, seed=cfg.permutation_seed)
        fam = {"bar": mn.bar, "alpha": mn.alpha, "b": mn.b, "seed": mn.seed,
               "n_keys": len(mn.keys), "observed": dict(mn.observed),
               "adjusted_p": dict(mn.adjusted_p), "clearing": list(mn.clearing),
               "winner_clears_G2": winner["cell_id"] in mn.clearing,
               "winner_D": _d(winner)}
    else:
        fam = {"bar": None, "empty": True, "winner_clears_G2": False, "winner_D": _d(winner)}
    write_json_atomic(Path(cfg.cache_dir) / FAMILY_FILE, sanitise(fam))
    report["max_null"] = fam
    after = count_settings(cfg)
    report["budget"] = {
        "forward_attempts": len(fwd["attempts"]), "forward_kept": len(fwd["kept"]),
        "forward_skipped": len(fwd["skipped"]), "forward_not_attempted": len(fwd["not_attempted"]),
        "loo_attempts": len(loo["attempts"]),
        "bisection_trials": len((control["bisection"] or {}).get("trials", [])),
        "control_grades": 1 if control["grade"] else 0,
        "reverse_attempts": len(rev["attempts"]), "reverse_skipped": len(rev["skipped"]),
        "round2_attempts_total": (len(fwd["attempts"]) + len(loo["attempts"])
                                  + len((control["bisection"] or {}).get("trials", []))
                                  + (1 if control["grade"] else 0) + len(rev["attempts"])),
        "round2_cap": G.ROUND2_MAX, "settings_before": before, "settings_after": after,
        "new_settings": after - before, "max_settings": cfg.max_settings,
    }
    report["finished_at"] = _utc_now()
    write_json_atomic(Path(cfg.cache_dir) / ROUND2_FILE, sanitise(report))
    rebuild_summary(cfg)
    return report


def _find_result(cache_key: str, extra: Sequence[Mapping], cfg: SearchConfig) -> Mapping | None:
    for r in extra:
        if r["cache_key"] == cache_key:
            return r
    return load_result(cfg, cache_key)


# ---------------------------------------------------------------------------
# CLI — parse, call, print (Rule 3).  No holdout option exists here.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtest.tune",
        description="Item 4.2 pre-registered search runner (TRAIN 2021-23 only; no holdout "
                    "flag exists — the holdout episode is a backtest.replay command).")
    p.add_argument("--db", required=True,
                   help="the §11.5 SNAPSHOT database; the live db/ziggurat.sqlite is refused")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                   help=f"SEARCH_CACHE_DIR (default {DEFAULT_CACHE_DIR}); the canonical "
                        f"{CANONICAL_CACHE_DIR}, which holds the holdout ledger, is refused")
    p.add_argument("--jobs", type=int, default=8, help="round-1 worker processes (default 8)")
    p.add_argument("--round", choices=("1", "2", "all"), default="all")
    p.add_argument("--dry-run", action="store_true",
                   help="print the cells and their cache keys without opening the database")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = SearchConfig(db=refuse_live_database(args.db),
                           cache_dir=refuse_canonical_cache_dir(args.cache_dir), jobs=args.jobs)
        db = cfg.db
    except (LiveDatabaseRefused, CanonicalCacheRefused) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        for cell in G.ROUND1_CELLS:
            params = build_params(cell)
            assert_frozen(params, cell, cfg)
            print(f"{cell.cell_id:28s} {params.cache_key()}  eligible={cell.eligible}")
        print(f"{len(G.ROUND1_CELLS)} round-1 cells; cap {cfg.max_settings}; cache {cfg.cache_dir}")
        return 0
    if not db.exists():
        print(f"REFUSED: {db} does not exist", file=sys.stderr)
        return 2
    try:
        conn = R.open_ro(db)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as exc:
        print(f"REFUSED: {db} is not a readable database ({exc})", file=sys.stderr)
        return 2
    fp_before = fingerprint(conn)
    try:
        # Appendix A: the panel digests are "to be re-taken on the snapshot
        # before cell 1 and ASSERTED EQUAL" — before a byte is spent.
        assert_panel_fingerprint(fp_before)
    except PanelFingerprintMismatch as exc:
        print(f"ABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    rc = 0
    try:
        with run_lock(cfg):
            write_json_atomic(Path(cfg.cache_dir) / FINGERPRINT_FILE, {"before": fp_before})
            try:
                if args.round in ("1", "all"):
                    run_round1(cfg, fingerprint_block=fp_before)
                if args.round in ("2", "all"):
                    report = run_round2(cfg, conn=conn, fingerprint_block=fp_before)
                    w = report["winner"]
                    print(f"winner: {w['cell_id']} D={w['D']} clears_G1={w['clears_G1']} "
                          f"clears_G2={report['max_null']['winner_clears_G2']} "
                          f"clears_G4={w['clears_G4']} clears_G5={report['loo'].get('clears_G5')} "
                          f"(bar {report['max_null'].get('bar')}; max-null family "
                          f"{report['family']['label_based_size']} label-based vs "
                          f"{report['family']['argmax_screen_passing_size']} ranked)")
                    if w["tie_to_default"]:
                        print(f"ties -> the default: {w['tie_reason']}")
                    print(f"reverse disagrees: {report['reverse']['disagrees']}; "
                          f"control: {report['control'].get('skipped') or report['control']['bisection']['status']}")
                    print(f"budget: {report['budget']}")
            except (BudgetExceeded, FreezeRefused, RunnerBug, StaleCacheRefused, OSError) as exc:
                print(f"ABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
                rc = 3
            finally:
                # F7's verdict is a DECISION RULE ("results are never pooled
                # across fingerprints"), so it is printed, exit-coded and
                # APPENDED — the single-file block is overwritten by the next
                # launch, which would erase the very launch that moved.  And the
                # summary is rebuilt here so an abort never leaves a stale
                # summary beside a truer results/ directory.
                fp_after = fingerprint(conn)
                drift = fingerprint_drift(fp_before, fp_after)
                write_json_atomic(Path(cfg.cache_dir) / FINGERPRINT_FILE,
                                  {"before": fp_before, "after": fp_after, **drift})
                D.append_grade_log(Path(cfg.cache_dir) / FINGERPRINT_LOG,
                                   {"before": sanitise(fp_before), "after": sanitise(fp_after),
                                    **{k: sanitise(v) for k, v in drift.items()},
                                    "argv": list(argv) if argv is not None else sys.argv[1:],
                                    "round": args.round, "pid": os.getpid()})
                print(drift["verdict"], file=sys.stderr if not drift["unchanged"] else sys.stdout)
                if not drift["unchanged"]:
                    rc = rc or 4              # an abort's own code is not masked
                try:
                    rebuild_summary(cfg)
                except Exception as exc:                    # noqa: BLE001 — never mask the abort
                    print(f"summary rebuild failed: {exc}", file=sys.stderr)
    except SearchLocked as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"settings evaluated: {count_settings(cfg)} / {cfg.max_settings}; "
          f"summary {Path(cfg.cache_dir) / SUMMARY_FILE}")
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
