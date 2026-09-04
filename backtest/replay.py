"""Week-by-week replay of the production candidate generator (item 4.1).

The DECIDE phase of the weekly replay harness.  For every season and every
REG week ``T`` that has a completed box score it:

1. sets the decision clock ``as_of(T)`` — the Tuesday after the week's last
   game, read from the schedules table (a Tuesday last gameday rolls to the
   FOLLOWING Tuesday);
2. asks the PRODUCTION generator what it would have said at that clock —
   ``base.latest_truth(build_candidates)(conn, as_of=as_of(T), season=S,
   week=T)``, the same function ``ziggurat candidates`` runs, bound to the
   bulk-history view (Rule 1's documented exception: every fact row in the
   database was retrieved in 2026, so the safe-default view reads empty at
   any past clock);
3. builds the POOL from the board — usage-arm rows with a gsis_id, in the
   replay's positions, that the week-T market page does not already rank
   inside the eligibility window — and hands it to each requested
   :class:`Strategy`, which returns at most ``k`` picks;
4. FREEZES the picks as :class:`backtest.decisions.Decision` rows (in memory,
   and as JSONL under a gitignored cache) before any grading read happens.

Nothing here reads a market page later than ``as_of(T)``; the grade phase
(:mod:`backtest.scorecards`) reads later pages at a separate, later clock.
Weeks the generator cannot serve are recorded as ungradeable
:class:`WeekRecord` rows with the reason — never silently skipped.

The three baseline strategies are deliberately dumb: the harness measures the
GENERATOR, and a strategy is only the rule that turns its board into ``k``
waiver claims.  ``signal_topk`` is the tool's own answer, ``random_k`` the
pick-level null, ``volume_topk`` the "just add the guy who touched the ball
most" heuristic a novice would use without the tool.

CLI (``python -m backtest.replay``) is Rule-3 shaped: parse, call, print.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from backtest import decisions as D
from backtest import scorecards as S
from ziggurat.core import candidates as C
from ziggurat.data.nfl import base
from ziggurat.data.nfl.schedules import get_schedule
from ziggurat.data.nfl.weekly_stats import get_weekly_stats
from ziggurat.paths import REPO_ROOT

__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_DB",
    "STRATEGIES",
    "NoSchedule",
    "PoolRow",
    "Strategy",
    "build_pool",
    "calendar_as_of",
    "decide_week",
    "generator_setting",
    "generator_thresholds",
    "main",
    "open_ro",
    "replay",
    "week_as_of",
]

DEFAULT_DB = REPO_ROOT / "db" / "ziggurat.sqlite"
#: Under top-level ``data/`` — gitignored (Rule 5): decisions carry player
#: names and the generator's reason text, never league-private data, but the
#: freeze is an artifact, not source.
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "backtest" / "replay"

WEEK_NO_BOX_SCORE = "no_box_score"


class NoSchedule(ValueError):
    """No REG games are stored for the (season, week) — no clock can be set."""


# ---------------------------------------------------------------------------
# database + clocks
# ---------------------------------------------------------------------------


def open_ro(path: str | os.PathLike) -> sqlite3.Connection:
    """A READ-ONLY connection — never ``store.open_db``, which applies
    migrations, and a replay must not be able to write the production file."""
    conn = sqlite3.connect(f"file:{os.fspath(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def calendar_as_of(season: int) -> str:
    """The clock a season's CALENDAR is read at: the schedule is a structural
    fact published before the season, and this is the same fact-time
    ``backtest/draft_backtest.py`` grades a season at."""
    return f"{season + 1}-02-28"


def week_as_of(conn: sqlite3.Connection, season: int, week: int) -> str:
    """The decision clock for week ``T``: the first Tuesday strictly after the
    week's last REG gameday.  A Monday game rolls to the next day; a Sunday
    to two days on; a Tuesday game to the FOLLOWING Tuesday."""
    rows = base.latest_truth(get_schedule)(
        conn, as_of=calendar_as_of(season), season=season, week=week
    )
    days = [r["gameday"] for r in rows if r["game_type"] == "REG" and r["gameday"]]
    if not days:
        raise NoSchedule(f"no REG games stored for season {season} week {week}")
    last = dt.date.fromisoformat(max(days))
    d = last + dt.timedelta(days=1)
    while d.weekday() != 1:
        d += dt.timedelta(days=1)
    return d.isoformat()


def reg_weeks(conn: sqlite3.Connection, season: int) -> list[int]:
    rows = base.latest_truth(get_schedule)(conn, as_of=calendar_as_of(season), season=season)
    return sorted({int(r["week"]) for r in rows if r["game_type"] == "REG"})


# ---------------------------------------------------------------------------
# pool + strategies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolRow:
    """One eligible usage-arm candidate, as the strategy sees it."""

    gsis_id: str
    espn_id: str | None
    player: str
    position: str
    team: str | None
    signal_kind: str
    magnitude: float
    board_rank: int
    market_rank_r0: int | None
    market_page_size_r0: int | None
    r0_scrape_date: str | None
    week_volume: float | None
    reasons: tuple[str, ...]


class Strategy(Protocol):
    name: str

    def pick(self, pool: Sequence[PoolRow], *, k: int, rng: random.Random) -> list[PoolRow]:
        """At most ``k`` rows of ``pool``, in claim order.  Pure."""


def _tiebreak(row: PoolRow) -> tuple:
    return (row.position, row.gsis_id)


class SignalTopK:
    """The tool's own answer: the top-k of the pool by signal magnitude."""

    name = "signal_topk"

    def pick(self, pool: Sequence[PoolRow], *, k: int, rng: random.Random) -> list[PoolRow]:
        return sorted(pool, key=lambda r: (-r.magnitude, *_tiebreak(r)))[:k]


class RandomK:
    """The pick-level null: k rows drawn uniformly from the same pool."""

    name = "random_k"

    def pick(self, pool: Sequence[PoolRow], *, k: int, rng: random.Random) -> list[PoolRow]:
        ordered = sorted(pool, key=_tiebreak)
        return rng.sample(ordered, min(k, len(ordered)))


class VolumeTopK:
    """The novice heuristic: the k pool rows with the most week-T touches
    (carries + targets), ignoring the signal.  Rows with no stat line rank last."""

    name = "volume_topk"

    def pick(self, pool: Sequence[PoolRow], *, k: int, rng: random.Random) -> list[PoolRow]:
        def key(r: PoolRow) -> tuple:
            vol = r.week_volume if r.week_volume is not None else float("-inf")
            return (-vol, *_tiebreak(r))
        return sorted(pool, key=key)[:k]


STRATEGIES: Mapping[str, Strategy] = {
    s.name: s for s in (SignalTopK(), RandomK(), VolumeTopK())
}
assert tuple(STRATEGIES) == D.STRATEGY_NAMES


def week_rng(seed: int, season: int, week: int) -> random.Random:
    """One stream per week, seeded by a STRING (hashed with sha512 by
    ``random.seed``, so it does not depend on ``PYTHONHASHSEED``)."""
    return random.Random(f"{seed}:{season}:{week}")


_NUMBERS = re.compile(r"\d+")
_PARENS = re.compile(r"\([^)]*\)")


def normalise_log_line(text: str) -> str:
    """Collapse ids and numbers so one warning template counts as one line
    (the generator emits ~70 distinct crosswalk warnings per week that differ
    only in the ids they name)."""
    return _NUMBERS.sub("#", _PARENS.sub("(...)", text)).strip()


class _LogCapture(logging.Handler):
    """Collects the generator's log lines (its crosswalk warnings arrive via
    ``logging``, not stderr) so the scorecard can summarise them with counts."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: Counter[str] = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        self.lines[f"{record.levelname}: {normalise_log_line(record.getMessage())}"] += 1


@contextlib.contextmanager
def capture_generator_output():
    """Yields a Counter of distinct log/stderr lines emitted inside the block."""
    handler = _LogCapture()
    logger = logging.getLogger("ziggurat")
    buf = io.StringIO()
    logger.addHandler(handler)
    try:
        with contextlib.redirect_stderr(buf):
            yield handler.lines
    finally:
        logger.removeHandler(handler)
        for line in buf.getvalue().splitlines():
            if line.strip():
                handler.lines[f"stderr: {normalise_log_line(line)}"] += 1


@dataclass(frozen=True)
class PoolBuild:
    pool: tuple[PoolRow, ...]
    generator_rows: int
    usage_rows: int
    excluded_injury: int
    excluded_qb1: int
    excluded_ineligible: int
    excluded_no_gsis: int
    excluded_position: int
    r0_scrape_date: str | None
    r0_pages: tuple[str, ...]


def build_pool(
    board: C.CandidateBoard,
    *,
    params: D.ReplayParams,
    refs: S.ReferenceCache,
    as_of: str,
    volumes: Mapping[str, float] | None,
) -> PoolBuild:
    """The eligible pool from a board.

    Injury-shock rows are VACANCIES (the player who went down), not adds, and
    QB1 rows are a labelled hypothesis about a different position — both are
    counted out, never priced.  A usage row without a gsis_id cannot be
    graded (the market panel is joined on gsis) and is counted out too.
    """
    market = S.MARKETS[params.market]
    elig = params.eligibility_map
    pool: list[PoolRow] = []
    excluded = Counter()
    usage = [r for r in board.rows if r.signal_kind == C.SIGNAL_USAGE]
    excluded["injury"] = sum(1 for r in board.rows if r.signal_kind == C.SIGNAL_INJURY)
    excluded["qb1"] = sum(1 for r in board.rows if r.signal_kind == C.SIGNAL_QB1)
    scrapes: set[str] = set()
    pages: set[str] = set()
    for board_rank, row in enumerate(usage, start=1):
        if row.gsis_id is None:
            excluded["no_gsis"] += 1
            continue
        if row.position not in params.positions:
            excluded["position"] += 1
            continue
        ref = refs.get(market, board.season, board.week, row.position, not_after=as_of)
        rank = size = scrape = None
        if ref is not None:
            scrapes.add(ref.scrape_date)
            pages.add(ref.fp_page)
            rank, size, scrape = ref.ranks.get(row.gsis_id), ref.page_size, ref.scrape_date
            if rank is not None and rank <= elig.get(row.position, 0):
                excluded["ineligible"] += 1
                continue
        pool.append(PoolRow(
            gsis_id=row.gsis_id, espn_id=row.espn_id, player=row.player,
            position=row.position, team=row.team, signal_kind=row.signal_kind,
            magnitude=float(row.magnitude), board_rank=board_rank,
            market_rank_r0=rank, market_page_size_r0=size, r0_scrape_date=scrape,
            week_volume=None if volumes is None else volumes.get(row.gsis_id),
            reasons=tuple(row.reasons),
        ))
    return PoolBuild(
        pool=tuple(pool), generator_rows=len(board.rows), usage_rows=len(usage),
        excluded_injury=excluded["injury"], excluded_qb1=excluded["qb1"],
        excluded_ineligible=excluded["ineligible"], excluded_no_gsis=excluded["no_gsis"],
        excluded_position=excluded["position"],
        r0_scrape_date=max(scrapes) if scrapes else None, r0_pages=tuple(sorted(pages)),
    )


def week_volumes(conn: sqlite3.Connection, *, season: int, week: int, as_of: str) -> dict[str, float]:
    """gsis_id -> carries + targets in week T, knowable at ``as_of``."""
    out: dict[str, float] = {}
    for r in base.latest_truth(get_weekly_stats)(conn, as_of=as_of, season=season, week=week):
        gsis = r["player_id"]
        if gsis is None:
            continue
        out[str(gsis)] = float((r["carries"] or 0) + (r["targets"] or 0))
    return out


def _undecided(
    params: D.ReplayParams, *, season: int, week: int, as_of: str, status: str, reason: str,
    build: PoolBuild | None = None, log_lines: Counter | None = None,
) -> list[D.WeekRecord]:
    b = build
    return [
        D.WeekRecord(
            season=season, week=week, as_of=as_of, strategy=name, k=params.k,
            status=status, reason=reason, decisions=(),
            generator_rows=b.generator_rows if b else 0, usage_rows=b.usage_rows if b else 0,
            pool_size=len(b.pool) if b else 0,
            excluded_injury=b.excluded_injury if b else 0,
            excluded_qb1=b.excluded_qb1 if b else 0,
            excluded_ineligible=b.excluded_ineligible if b else 0,
            excluded_no_gsis=b.excluded_no_gsis if b else 0,
            excluded_position=b.excluded_position if b else 0,
            r0_scrape_date=b.r0_scrape_date if b else None,
            r0_pages=b.r0_pages if b else (),
            log_lines=tuple(sorted((log_lines or Counter()).items())),
        )
        for name in params.strategies
    ]


#: The reason text a replay-overridden usage floor carries (Rule 6): a reader
#: of the frozen decisions must be able to see that the floors were NOT the
#: shipped hypothesis but a tuning setting UNDER EVALUATION.  It is stamped
#: into the frozen JSONL of every 4.2 cell, which is why the wording is pinned
#: by test (breakout-backtest.md §11.4 item 1): the 4.1 text called an
#: overridden setting "not tuned", which is the opposite of what it is.
OVERRIDDEN_BREAKOUT_LABEL = (
    "hypothesis: usage floors OVERRIDDEN by a replay run — an item-4.2 tuning setting "
    "under evaluation on TRAIN seasons, not the shipped core/candidates floors — a row "
    "qualifies if ANY differenced delta clears its floor"
)
OVERRIDDEN_BREAKOUT_SOURCE = "backtest/replay.py --breakout-floor (item 4.2 tuning run)"


def generator_thresholds(params: D.ReplayParams) -> tuple[C.BreakoutThresholds, Mapping[str, float]]:
    """The ``(thresholds, emergence_floors)`` pair ``build_candidates`` runs
    with under ``params.generator``.

    The shipped objects are handed over UNCHANGED when the setting is the
    default, so a default replay is row-for-row the production generator; an
    overridden setting builds a labelled :class:`BreakoutThresholds` whose
    reason text discloses the override.
    """
    if params.generator_is_default:
        return C.DEFAULT_BREAKOUT, C.EMERGENCE_FLOORS
    shipped = dict(C.DEFAULT_BREAKOUT.floors)
    floors = params.breakout_floors
    if floors == shipped:
        thresholds = C.DEFAULT_BREAKOUT
    else:
        thresholds = C.BreakoutThresholds(
            floors=MappingProxyType(dict(floors)),
            label=OVERRIDDEN_BREAKOUT_LABEL,
            source=OVERRIDDEN_BREAKOUT_SOURCE,
        )
    emergence = params.emergence_floors
    if emergence == dict(C.EMERGENCE_FLOORS):
        return thresholds, C.EMERGENCE_FLOORS
    return thresholds, MappingProxyType(dict(emergence))


def decide_week(
    conn: sqlite3.Connection,
    *,
    season: int,
    week: int,
    params: D.ReplayParams,
    unlock_holdout: bool = False,
) -> list[D.WeekRecord]:
    """One :class:`WeekRecord` per strategy for ``(season, week)``.

    Every read in here happens at ``as_of(T)``.  The generator is called
    exactly once and its board is shared by all strategies, so a run with
    three strategies costs one generator call per week, not three.  A HOLDOUT
    season is refused before the first read unless ``unlock_holdout``.
    """
    D.require_holdout_unlock((season,), unlock_holdout=unlock_holdout)
    try:
        as_of = week_as_of(conn, season, week)
    except NoSchedule as exc:
        return _undecided(
            params, season=season, week=week, as_of="", status=D.WEEK_NO_SCHEDULE,
            reason=str(exc),
        )
    volumes = week_volumes(conn, season=season, week=week, as_of=as_of)
    if not volumes:
        return _undecided(
            params, season=season, week=week, as_of=as_of, status=WEEK_NO_BOX_SCORE,
            reason=f"no weekly_stats rows for season {season} week {week} knowable at {as_of}",
        )
    thresholds, emergence_floors = generator_thresholds(params)
    with capture_generator_output() as log_lines:
        try:
            board = base.latest_truth(C.build_candidates)(
                conn, as_of=as_of, season=season, week=week,
                thresholds=thresholds, emergence_floors=emergence_floors,
            )
        except Exception as exc:  # recorded as an ungradeable week, never skipped
            return _undecided(
                params, season=season, week=week, as_of=as_of,
                status=D.WEEK_GENERATOR_FAILED, reason=f"{type(exc).__name__}: {exc}",
                log_lines=log_lines,
            )
    refs = S.ReferenceCache(conn, as_of=as_of)
    build = build_pool(
        board, params=params, refs=refs, as_of=as_of,
        volumes=volumes if "volume_topk" in params.strategies else None,
    )
    if not build.pool:
        return _undecided(
            params, season=season, week=week, as_of=as_of, status=D.WEEK_EMPTY_POOL,
            reason=(f"generator returned {build.usage_rows} usage rows, none eligible "
                    f"(injury={build.excluded_injury}, qb1={build.excluded_qb1}, "
                    f"ineligible={build.excluded_ineligible}, no_gsis={build.excluded_no_gsis}, "
                    f"position={build.excluded_position})"),
            build=build, log_lines=log_lines,
        )
    out: list[D.WeekRecord] = []
    for name in params.strategies:
        rng = week_rng(params.seed, season, week)
        picks = STRATEGIES[name].pick(build.pool, k=params.k, rng=rng)
        if len(picks) > params.k:
            raise RuntimeError(f"strategy {name} returned {len(picks)} > k={params.k}")
        decisions = tuple(
            D.Decision(
                season=season, week=week, as_of=as_of, strategy=name, rank_in_board=i,
                board_rank=p.board_rank, gsis_id=p.gsis_id, espn_id=p.espn_id,
                player=p.player, position=p.position, team=p.team,
                signal_kind=p.signal_kind, magnitude=p.magnitude,
                market_rank_r0=p.market_rank_r0, market_page_size_r0=p.market_page_size_r0,
                r0_scrape_date=p.r0_scrape_date, reasons=p.reasons,
            )
            for i, p in enumerate(picks, start=1)
        )
        out.append(D.WeekRecord(
            season=season, week=week, as_of=as_of, strategy=name, k=params.k,
            status=D.WEEK_DECIDED, reason=None, decisions=decisions,
            generator_rows=build.generator_rows, usage_rows=build.usage_rows,
            pool_size=len(build.pool), excluded_injury=build.excluded_injury,
            excluded_qb1=build.excluded_qb1, excluded_ineligible=build.excluded_ineligible,
            excluded_no_gsis=build.excluded_no_gsis, excluded_position=build.excluded_position,
            r0_scrape_date=build.r0_scrape_date, r0_pages=build.r0_pages,
            log_lines=tuple(sorted(log_lines.items())),
        ))
    return out


def replay(
    conn: sqlite3.Connection,
    params: D.ReplayParams,
    *,
    progress: Callable[[str], None] | None = None,
    unlock_holdout: bool = False,
) -> list[D.WeekRecord]:
    """The decide phase over every season and week in ``params``.

    Refuses (:class:`backtest.decisions.HoldoutLocked`) before the first read
    if any season is HOLDOUT and ``unlock_holdout`` is not set — the whole
    run, not the holdout weeks alone, so a TRAIN+HOLDOUT request never
    half-completes.
    """
    D.require_holdout_unlock(params.seasons, unlock_holdout=unlock_holdout)
    records: list[D.WeekRecord] = []
    for season in params.seasons:
        weeks = reg_weeks(conn, season)
        if params.weeks is not None:
            weeks = [w for w in weeks if w in params.weeks]
        if not weeks and params.weeks is not None:
            weeks = list(params.weeks)
        for week in weeks:
            t0 = time.perf_counter()
            recs = decide_week(
                conn, season=season, week=week, params=params, unlock_holdout=unlock_holdout,
            )
            records.extend(recs)
            if progress is not None:
                head = recs[0]
                n = ", ".join(f"{r.strategy}={len(r.decisions)}" for r in recs)
                progress(
                    f"{season} wk{week:02d} as_of={head.as_of or '-'} {head.status} "
                    f"pool={head.pool_size} usage={head.usage_rows} picks[{n}] "
                    f"{time.perf_counter() - t0:.1f}s"
                    + (f"  ({head.reason})" if head.reason else "")
                )
    return records


# ---------------------------------------------------------------------------
# CLI — parse, call, print
# ---------------------------------------------------------------------------


def _parse_seasons(text: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return tuple(sorted(set(out)))


def _parse_ints(text: str | None) -> tuple[int, ...] | None:
    if text is None:
        return None
    return tuple(int(x) for x in text.split(",") if x.strip())


def _parse_floats(text: str | None) -> tuple[float, ...] | None:
    if text is None:
        return None
    return tuple(float(x) for x in text.split(",") if x.strip())


def _parse_floor(text: str) -> tuple[str, float]:
    """``METRIC=VALUE`` for ``--breakout-floor`` / ``--emergence-floor``."""
    if "=" not in text:
        raise argparse.ArgumentTypeError(f"{text!r}: expected METRIC=VALUE, e.g. carries=8")
    metric, value = text.split("=", 1)
    metric = metric.strip()
    try:
        floor = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r}: {value!r} is not a number") from exc
    if not metric:
        raise argparse.ArgumentTypeError(f"{text!r}: empty metric name")
    return metric, floor


def generator_setting(
    breakout: Sequence[tuple[str, float]] | None,
    emergence: Sequence[tuple[str, float]] | None,
) -> tuple[tuple[str, float], ...]:
    """The shipped floors with the CLI's overrides applied — the
    ``ReplayParams.generator`` value.  Unknown metrics are refused by
    :class:`ReplayParams` itself, naming the known ones."""
    setting = dict(D.default_generator())
    for metric, floor in breakout or ():
        setting[metric] = floor
    for metric, floor in emergence or ():
        setting[D.EMERGENCE_PREFIX + metric] = floor
    return tuple(sorted(setting.items()))


#: Item 4.2 (breakout-backtest.md §11.4 item 2): the per-week vector file every
#: graded run leaves beside its freeze.  It is the `signal_topk` / `wp` card's
#: `per_week_depth_lift` of the run's split — TRAIN, or HOLDOUT on an unlocked
#: run — keyed "<season>,<week>" -> L(g, w).  The HOLDOUT episode's d_w vectors
#: are read from THIS file (§8.3), never from a second grade.
PER_WEEK_FILE = "per-week-lifts.json"
#: The §6.4 grade log's default basename (under ``--cache-dir``, or wherever
#: ``--grade-log`` points).  Item 4.2's SEARCH_CACHE_DIR carries the same name.
GRADE_LOG = "grade-log.jsonl"
PER_WEEK_STRATEGY = "signal_topk"
PER_WEEK_MARKET = "wp"


def per_week_lifts_payload(
    card: S.MarketScorecard, params: D.ReplayParams, *, split: str
) -> dict:
    """The JSON body of :data:`PER_WEEK_FILE` for ``card`` — pure, so a test
    can compare the file byte-for-byte against the card it was graded from."""
    vector = card.per_week_depth_lift(split)
    return {
        "cache_key": params.cache_key(),
        "params_hash": params.params_hash,
        "split": split,
        "strategy": card.strategy,
        "market": card.market,
        "hit_places": int(card.places),
        "owned_delta": float(card.owned_delta),
        "grade_as_of": card.grade_as_of,
        "seasons": list(params.seasons),
        "n_weeks": len(vector),
        "per_week_lift_depth": {
            f"{season},{week}": float(lift)
            for (season, week), lift in sorted(vector.items())
        },
    }


def write_per_week_lifts(
    freeze_dir: Path,
    cards: Sequence[S.MarketScorecard],
    params: D.ReplayParams,
    *,
    hit_places: int,
    owned_delta: float,
    grade_as_of: str,
) -> Path | None:
    """Write :data:`PER_WEEK_FILE` under ``freeze_dir`` for the ``signal_topk``
    / ``wp`` card of this run at the run's H / owned-delta, atomically (tmp +
    fsync + rename via :func:`D._write_atomic`).  Returns the path, or ``None``
    when no such card was graded (the caller prints why).  The split is
    HOLDOUT iff the run holds a holdout season, else TRAIN — never ALL."""
    split = "HOLDOUT" if D.holdout_seasons(params.seasons) else "TRAIN"
    for card in cards:
        if (card.strategy, card.market) != (PER_WEEK_STRATEGY, PER_WEEK_MARKET):
            continue
        if (int(card.places), float(card.owned_delta), card.grade_as_of) != (
            int(hit_places), float(owned_delta), grade_as_of
        ):
            continue
        payload = per_week_lifts_payload(card, params, split=split)
        freeze_dir.mkdir(parents=True, exist_ok=True)
        path = freeze_dir / PER_WEEK_FILE
        D._write_atomic(
            path, json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        )
        return path
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtest.replay",
        description="Weekly replay of the production candidate generator, graded "
                    "against the FantasyPros ECR panel (item 4.1).",
    )
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (opened READ-ONLY)")
    p.add_argument("--seasons", default=",".join(str(s) for s in D.TRAIN_SEASONS),
                   help="comma list and/or ranges, e.g. 2023 or 2021-2023,2025 "
                        f"(default: the TRAIN seasons {D.TRAIN_SEASONS}; the HOLDOUT "
                        f"seasons {D.HOLDOUT_SEASONS} need --unlock-holdout)")
    p.add_argument("--unlock-holdout", action="store_true",
                   help="allow the HOLDOUT seasons to be decided/graded; every such run "
                        f"is appended to the gitignored {D.UNLOCK_LEDGER} ledger AFTER "
                        "it completes (report, never tune, on holdout)")
    p.add_argument("--strategy", default="signal_topk",
                   help=f"comma list of {', '.join(D.STRATEGY_NAMES)}")
    p.add_argument("--k", type=int, default=3, help="picks per week (1..3)")
    p.add_argument("--breakout-floor", action="append", type=_parse_floor, default=None,
                   metavar="METRIC=VALUE",
                   help="override one differenced usage floor of the generator (repeatable; "
                        "part of the cache key — a tuning run, TRAIN seasons only)")
    p.add_argument("--emergence-floor", action="append", type=_parse_floor, default=None,
                   metavar="METRIC=VALUE",
                   help="override one role-emergence absolute floor (repeatable; part of "
                        "the cache key)")
    p.add_argument("--market", choices=("wp", "ros", "both"), default="both",
                   help="market to GRADE on (pool eligibility always uses the wp page)")
    p.add_argument("--grade-as-of", default=dt.date.today().isoformat(),
                   help="grade clock; must be later than every decision clock")
    p.add_argument("--hit-places", type=int, default=S.HIT_PLACES_DEFAULT,
                   help="HIT threshold in page places (hypothesis)")
    p.add_argument("--hit-places-sensitivity", default=",".join(map(str, S.HIT_PLACES_SENSITIVITY)))
    p.add_argument("--owned-delta", type=float, default=S.OWNED_DELTA_DEFAULT,
                   help="CORROBORATION threshold in Sleeper owned points (hypothesis)")
    p.add_argument("--owned-delta-sensitivity",
                   default=",".join(f"{x:g}" for x in S.OWNED_DELTA_SENSITIVITY))
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                   help="where frozen decisions land (gitignored data/ by default)")
    p.add_argument("--grade-log", default=None,
                   help=f"grade log to append one line per build_scorecard call to (§6.4; "
                        f"default <cache-dir>/{GRADE_LOG} — point it at the item-4.2 "
                        f"SEARCH_CACHE_DIR to keep one file for the whole item)")
    p.add_argument("--no-freeze", action="store_true", help="do not write the cache")
    p.add_argument("--force", action="store_true",
                   help="re-decide and overwrite an existing freeze (without it a verifying "
                        "freeze for the same parameters is REUSED and only graded)")
    p.add_argument("--grade-only", action="store_true",
                   help="skip the decide phase and grade the cached freeze")
    p.add_argument("--seed", type=int, default=0, help="random_k seed")
    p.add_argument("--weeks", default=None, help="restrict to these REG weeks (comma list)")
    p.add_argument("--reasons", action="store_true",
                   help="print every pick with the generator's reason text")
    return p


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, call, print.

    Order of the guards, and why: the HOLDOUT lock is checked BEFORE the
    database is opened (a refused run reads nothing); a verifying freeze for
    the same parameters is REUSED rather than re-decided (the done-when
    command is re-runnable — ``--force`` re-decides, a freeze that does not
    verify is refused by name); the unlock ledger line is appended only AFTER
    the grade completed (publish-then-record).
    """
    args = build_parser().parse_args(argv)
    t0 = time.perf_counter()
    try:
        params = D.ReplayParams(
            strategies=tuple(s.strip() for s in args.strategy.split(",") if s.strip()),
            k=args.k, seasons=_parse_seasons(args.seasons), seed=args.seed,
            weeks=_parse_ints(args.weeks),
            generator=generator_setting(args.breakout_floor, args.emergence_floor),
        )
    except ValueError as exc:
        print(f"EXIT 2: {exc}", file=sys.stderr)
        return 2
    markets = ("wp", "ros") if args.market == "both" else (args.market,)
    try:
        held = D.require_holdout_unlock(params.seasons, unlock_holdout=args.unlock_holdout)
    except D.HoldoutLocked as exc:
        print(f"EXIT 2: {exc}", file=sys.stderr)
        return 2
    conn = open_ro(args.db)
    print(f"replay: db={args.db} seasons={params.seasons} strategies={params.strategies} "
          f"k={params.k} eligibility_market={params.market} seed={params.seed} "
          f"params_hash={params.cache_key()}"
          + ("" if params.generator_is_default else "  generator=OVERRIDDEN"))
    if held:
        print(f"HOLDOUT unlocked for seasons {held}: this run is appended to "
              f"{os.path.join(args.cache_dir, D.UNLOCK_LEDGER)} once it completes "
              "(report on holdout; never tune on it)")
    target = D.freeze_dir(args.cache_dir, params)
    if args.grade_only:
        try:
            records = D.load(args.cache_dir, params, unlock_holdout=args.unlock_holdout)
        except FileNotFoundError:
            print(f"EXIT 2: --grade-only but no freeze under {target}; run without "
                  "--grade-only to decide first", file=sys.stderr)
            return 2
        except D.HoldoutLocked as exc:
            print(f"EXIT 2: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:  # a manifest / digest mismatch
            print(f"EXIT 2: the freeze under {target} does not verify ({exc}); pass "
                  "--force without --grade-only to re-decide over it", file=sys.stderr)
            return 2
        phase = "grade"
        print(f"loaded {len(records)} frozen week records from {target}")
    else:
        status = D.FREEZE_MISSING if args.no_freeze else D.freeze_status(args.cache_dir, params)
        if status == D.FREEZE_OK and not args.force:
            records = D.load(args.cache_dir, params, unlock_holdout=args.unlock_holdout)
            phase = "grade"
            print(f"reusing the freeze under {target}: {len(records)} frozen week records "
                  "verify against the manifest, so the decide phase is skipped; pass "
                  "--force to re-decide")
        elif status not in (D.FREEZE_OK, D.FREEZE_MISSING) and not args.force:
            print(f"EXIT 2: the freeze under {target} does not verify ({status}); "
                  "pass --force to re-decide over it, or move it aside", file=sys.stderr)
            return 2
        else:
            records = replay(
                conn, params, progress=lambda s: print("  " + s, file=sys.stderr),
                unlock_holdout=args.unlock_holdout,
            )
            phase = "decide+grade"
            decide_s = time.perf_counter() - t0
            print(f"decide phase: {len(records)} week records in {decide_s:.1f}s")
            if not args.no_freeze:
                try:
                    target = D.freeze(
                        records, params, cache_dir=args.cache_dir, force=args.force,
                        written_at=_utc_now(), db_path=os.path.abspath(args.db),
                    )
                except FileExistsError as exc:  # a freeze landed since the check above
                    print(f"EXIT 2: {exc}", file=sys.stderr)
                    return 2
                print(f"frozen under {target}")
    grade_log_path = Path(args.grade_log) if args.grade_log else (
        Path(args.cache_dir) / GRADE_LOG)
    cards: list[S.MarketScorecard] = []
    for strategy in params.strategies:
        for market in markets:
            try:
                card = S.build_scorecard(
                    conn, records, params, strategy=strategy, market=market,
                    grade_as_of=args.grade_as_of, places=args.hit_places,
                    owned_delta=args.owned_delta,
                    places_sensitivity=_parse_ints(args.hit_places_sensitivity) or (),
                    owned_sensitivity=_parse_floats(args.owned_delta_sensitivity) or (),
                    unlock_holdout=args.unlock_holdout,
                )
            except S.GradeInputError as exc:
                print(f"EXIT 2: grade refused ({strategy}, market={market}): {exc}",
                      file=sys.stderr)
                return 2
            cards.append(card)
            # The GRADE LOG (§6.4 / §11.5): "every build_scorecard call made
            # anywhere in 4.2 — the search, the G6 re-grades (§7.2), the holdout
            # episode — appends one line ... F8 is otherwise unverifiable after
            # the fact."  The §8.3 holdout commands and the §7.2 G6 re-grades are
            # THIS CLI, and `--hit-places` / `--owned-delta` / `--grade-as-of`
            # live in neither ReplayParams nor the manifest — so a log written
            # only by the search runner could not see the sweep it exists to
            # expose (the runner asserts H == 5 before it grades at all).
            D.append_grade_log(grade_log_path, {
                "cache_key": params.cache_key(), "strategy": strategy, "market": market,
                "places": int(args.hit_places), "owned_delta": float(args.owned_delta),
                "grade_as_of": args.grade_as_of, "timestamp": _utc_now(),
                "entry_point": "backtest.replay", "unlock_holdout": bool(args.unlock_holdout),
            })
            print()
            print(S.render(card, reasons=args.reasons))
    if len(params.strategies) > 1:
        print()
        print(S.render_comparison(cards))
    print()
    print(f"total runtime {time.perf_counter() - t0:.1f}s")
    # Item 4.2 (breakout-backtest.md §11.4 item 2): the per-week vector file,
    # written after EVERY build_scorecard and BEFORE the ledger row so the
    # holdout d_w vectors have exactly one sanctioned source (§8.3).
    if args.no_freeze:
        print(f"{PER_WEEK_FILE} not written: --no-freeze (nothing lands under {target})")
    else:
        vector_path = write_per_week_lifts(
            target, cards, params, hit_places=args.hit_places,
            owned_delta=args.owned_delta, grade_as_of=args.grade_as_of,
        )
        if vector_path is None:
            print(f"{PER_WEEK_FILE} not written: no {PER_WEEK_STRATEGY}/{PER_WEEK_MARKET} "
                  "card was graded in this run")
        else:
            print(f"per-week depth-matched lift vector written to {vector_path}")
    if held:
        # publish-then-record: the holdout read AND its grade have completed.
        ledger = D.record_holdout_unlock(
            args.cache_dir, params=params, seasons=held,
            argv=list(argv) if argv is not None else sys.argv[1:],
            grade_as_of=args.grade_as_of, written_at=_utc_now(), phase=phase,
        )
        print(f"HOLDOUT unlock recorded: {ledger}")
    empty = sorted({
        (r.strategy, r.season) for r in records
    } - {(r.strategy, r.season) for r in records if r.decisions})
    if empty:
        print(f"EXIT 2: zero decisions for {empty}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
