"""Shared machinery for nflverse ingestion + as-of access (item 1.4).

Two knowledge-time columns on every fact table (see db/schema.sql):
  * ``knowable_as_of`` — when the fact became true/public (the primary leakage
    filter). Post-game facts (stats/snaps/ngs) are stamped with the team's
    gameday via ``game_date_map``; injuries use their own ``date_modified``;
    schedules use a preseason anchor.
  * ``retrieved_as_of`` — when we pulled it (provenance + revision key).

``select_as_of`` is the one query every accessor routes through. It supports
two deliberately distinct views:

* ``historical`` (the default) reconstructs what this system had retrieved by
  ``as_of``. Both timestamps gate the read.
* ``latest_truth`` ignores retrieval time and uses the newest correction for a
  fact that was already knowable by ``as_of``. This is useful for outcome
  grading and bulk-loaded immutable history, but must be requested explicitly.
"""

import functools
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

import pandas as pd

from ziggurat.data.asof import normalize_as_of

logger = logging.getLogger("ziggurat.data.nfl")

AsOfView = Literal["historical", "latest_truth"]
AS_OF_VIEWS = ("historical", "latest_truth")


def latest_truth(accessor: Callable) -> Callable:
    """Bind an as-of ``accessor`` to the immutable-bulk-history view.

    Backtests and outcome grading read history bulk-loaded *now*
    (``retrieved_as_of`` = today). The safe-default ``historical`` view also gates
    retrieval time, so for any past ``as_of`` it returns NOTHING — and returns it
    *silently*. That empty result is the footgun: it reads as "no data", not
    "wrong view". This wrapper makes ``latest_truth`` the read path a backtest
    can't forget:

        read_snaps = latest_truth(get_snap_counts)
        read_snaps(conn, as_of="2023-10-11", season=2023)   # bulk history, right view

    Fact-time protection is unchanged — ``latest_truth`` still hides facts not yet
    knowable by ``as_of``. A conflicting explicit ``view=`` is rejected rather than
    silently honored, so wrapping can only ever *add* the bulk-history semantics.
    """
    @functools.wraps(accessor)
    def read(*args, **kwargs):
        requested = kwargs.get("view", "latest_truth")
        if requested != "latest_truth":
            raise ValueError(
                f"latest_truth() reads the 'latest_truth' view; "
                f"got conflicting view={requested!r}"
            )
        kwargs["view"] = "latest_truth"
        return accessor(*args, **kwargs)

    return read


def require_columns(df: pd.DataFrame, required: Sequence[str], *, source: str) -> None:
    """Fail loudly when an upstream release changes the persisted schema."""
    missing = sorted(set(required) - set(df.columns))
    if missing:
        raise ValueError(f"{source}: source schema missing required columns {missing}")


def note_drops(source: str, dropped: int, total: int, *, why: str = "unresolved knowledge time") -> None:
    """Make a drop-on-ingest observable (never silent data loss). Ingesters call
    this when they skip rows they cannot stamp with a knowledge time."""
    if dropped:
        logger.warning("%s: dropped %d/%d rows (%s)", source, dropped, total, why)


def _clean(value):
    """Coerce a pandas/numpy cell to a SQLite-friendly Python scalar.

    NaN/NaT/None -> None (stored as SQL NULL); numpy scalars -> native Python.
    Storing NULL (not NaN) keeps downstream reads clean, and scoring._num already
    treats a missing/None value as zero.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):  # numpy scalar -> python
        return value.item()
    return value


def iso_date(value) -> str | None:
    """Normalize a date-ish cell to an ISO ``YYYY-MM-DD`` string (or None).

    Accepts pandas Timestamp / datetime / date / ISO-ish string. Datetimes are
    truncated to their date (knowledge time is day-granular, inclusive end-of-day).
    """
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10]  # "2023-10-05" or "2023-10-05 13:00:00" -> date part
    if hasattr(value, "date"):
        return value.date().isoformat()
    return str(value)[:10]


def frame_to_rows(
    df: pd.DataFrame,
    colmap: Mapping[str, str],
    *,
    retrieved_as_of: str,
    knowable_as_of: Callable[[pd.Series], str | None] | str,
) -> list[dict]:
    """Turn a source DataFrame into cleaned row dicts ready for ``upsert``.

    ``colmap`` maps db_column -> source_column. ``knowable_as_of`` is either a
    fixed ISO string or a callable taking the source row and returning one.
    """
    retrieved = iso_date(retrieved_as_of)
    rows: list[dict] = []
    for _, src in df.iterrows():
        row = {db: _clean(src.get(col)) for db, col in colmap.items()}
        row["retrieved_as_of"] = retrieved
        row["knowable_as_of"] = knowable_as_of(src) if callable(knowable_as_of) else knowable_as_of
        rows.append(row)
    return rows


def upsert(conn: sqlite3.Connection, table: str, rows: Sequence[Mapping], *,
           commit: bool = True) -> int:
    """Idempotent INSERT OR REPLACE of uniform row dicts. Returns rows written.

    ``commit=False`` leaves the write inside the caller's transaction. A
    replace-the-partition ingester (DELETE then insert) needs this: with the
    default per-call commit, the DELETE and the insert are two transactions, so
    a failure between them leaves the partition deleted and unreplaced — for
    league state (item 3.1) that is unrecoverable data loss, since ESPN serves
    no history. Such callers wrap both statements in one ``with conn:`` block
    and pass ``commit=False``.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, rows)
    if commit:
        conn.commit()
    return len(rows)


# Team-abbr variants across nflverse sources normalized to the schedules abbr.
# Notably NGS labels the Rams "LAR" while schedules use "LA"; without this every
# Rams NGS row would fail to resolve a gameday and be silently dropped. Extend as
# new mismatches surface (post-Week-1 validation will confirm coverage).
TEAM_ALIASES: dict[str, str] = {
    "LAR": "LA",   # Rams (NGS)
    "STL": "LA",   # Rams (relocation legacy)
    "OAK": "LV",   # Raiders (legacy)
    "SD": "LAC",   # Chargers (legacy)
    "WSH": "WAS",  # Washington (variant)
    "LVR": "LV",   # Raiders (variant)
    "JAC": "JAX",  # Jaguars (FantasyPros uses JAC; schedules use JAX)
}


def game_date_map(conn: sqlite3.Connection) -> dict[tuple[int, int, str], str]:
    """(season, week, team) -> gameday ISO string, from the schedules table.

    The knowledge-time source for post-game facts: a player's week-N line is
    knowable on the day that team played. Both home and away sides are indexed,
    and each canonical team is also indexed under its known source-abbr aliases
    (see TEAM_ALIASES) so e.g. an NGS "LAR" row resolves the schedules "LA" game.
    """
    out: dict[tuple[int, int, str], str] = {}
    alias_of: dict[str, list[str]] = {}
    for alias, canon in TEAM_ALIASES.items():
        alias_of.setdefault(canon, []).append(alias)
    for r in conn.execute(
        "SELECT season, week, home_team, away_team, gameday FROM schedules WHERE gameday IS NOT NULL"
    ):
        for team in (r["home_team"], r["away_team"]):
            if team is None:
                continue
            out[(r["season"], r["week"], team)] = r["gameday"]
            for alias in alias_of.get(team, ()):
                out.setdefault((r["season"], r["week"], alias), r["gameday"])
    return out


def week_first_gameday_map(conn: sqlite3.Connection) -> dict[tuple[int, int], str]:
    """(season, week) -> earliest gameday that week (knowledge time for
    forward-looking weekly data like depth charts: leakage-safe, never earlier
    than the week's own games)."""
    out: dict[tuple[int, int], str] = {}
    for r in conn.execute(
        "SELECT season, week, MIN(gameday) AS first FROM schedules "
        "WHERE gameday IS NOT NULL GROUP BY season, week"
    ):
        out[(r["season"], r["week"])] = r["first"]
    return out


def gsis_by_pfr(conn: sqlite3.Connection) -> dict[str, str]:
    """pfr_id -> gsis_id from the latest players snapshot (crosswalk resolution
    for PFR-keyed sources like snap counts).

    A pfr_id should map to exactly one gsis_id; if the crosswalk ever carries a
    collision it is logged (not silently last-write-wins) and the first mapping
    is kept, so a mis-join surfaces instead of hiding.
    """
    out: dict[str, str] = {}
    for r in conn.execute(
        """
        SELECT pfr_id, gsis_id FROM players p
        WHERE pfr_id IS NOT NULL AND retrieved_as_of = (
            SELECT MAX(retrieved_as_of) FROM players p2 WHERE p2.gsis_id = p.gsis_id
        )
        """
    ):
        pfr, gsis = r["pfr_id"], r["gsis_id"]
        if pfr in out and out[pfr] != gsis:
            logger.warning("crosswalk: pfr_id %s maps to multiple gsis (%s, %s); keeping first",
                           pfr, out[pfr], gsis)
            continue
        out[pfr] = gsis
    return out


def ids_by_fantasypros(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    """fantasypros_id -> (gsis_id, espn_id) from the latest players snapshot.

    The crosswalk resolution for FantasyPros-keyed sources (ECR rankings). Reads
    the newest retrieved row per gsis_id (mirrors ``gsis_by_pfr``); a fantasypros
    id should map to exactly one player, so a collision is logged (not silently
    last-write-wins) and the first mapping kept. players.py already normalizes
    espn_id/fantasypros_id to bare digit strings, so no per-row coercion here.
    """
    out: dict[str, tuple[str | None, str | None]] = {}
    for r in conn.execute(
        """
        SELECT fantasypros_id, gsis_id, espn_id FROM players p
        WHERE fantasypros_id IS NOT NULL AND retrieved_as_of = (
            SELECT MAX(retrieved_as_of) FROM players p2 WHERE p2.gsis_id = p.gsis_id
        )
        """
    ):
        fp, gsis, espn = r["fantasypros_id"], r["gsis_id"], r["espn_id"]
        if fp in out and out[fp] != (gsis, espn):
            logger.warning(
                "crosswalk: fantasypros_id %s maps to multiple players (%s, %s); keeping first",
                fp, out[fp], (gsis, espn),
            )
            continue
        out[fp] = (gsis, espn)
    return out


def espn_by_gsis(conn: sqlite3.Connection) -> dict[str, str]:
    """gsis_id -> espn_id from the latest players snapshot (crosswalk resolution
    for the ESPN-side join key used by valuation / the value view).

    Mirrors ``ids_by_fantasypros`` / ``gsis_by_pfr``: reads the newest retrieved
    row per gsis_id, logs (never silently last-write-wins) a collision, and keeps
    the first mapping. players.py already normalizes espn_id to a bare digit
    string, so no per-row coercion here.

    Crosswalk-at-now: like the sibling crosswalks this reads ``players`` at
    ``MAX(retrieved_as_of)`` with NO as-of gate — fine for immutable gsis<->espn
    identity and current draft use; if valuation is ever run at a past as_of for
    backtest, the id mapping is today's, not as-of.
    """
    out: dict[str, str] = {}
    for r in conn.execute(
        """
        SELECT gsis_id, espn_id FROM players p
        WHERE espn_id IS NOT NULL AND retrieved_as_of = (
            SELECT MAX(retrieved_as_of) FROM players p2 WHERE p2.gsis_id = p.gsis_id
        )
        """
    ):
        gsis, espn = r["gsis_id"], r["espn_id"]
        if gsis in out and out[gsis] != espn:
            logger.warning("crosswalk: gsis_id %s maps to multiple espn (%s, %s); keeping first",
                           gsis, out[gsis], espn)
            continue
        out[gsis] = espn
    return out


def gsis_by_espn(conn: sqlite3.Connection) -> dict[str, str]:
    """espn_id -> gsis_id from the latest players snapshot — the reverse of
    ``espn_by_gsis``, used by league state (item 3.1) whose rows arrive keyed by
    ESPN player id and must join the nflverse spine.

    Same contract as its siblings: newest retrieved row per gsis_id, a collision
    is logged (never silently last-write-wins) and the first mapping kept, and the
    read is crosswalk-at-now (no as-of gate) because gsis<->espn identity is
    immutable. D/ST never appears here — ESPN gives team defenses synthetic
    negative ids and nflverse has no gsis for them; they join by team abbr.
    """
    out: dict[str, str] = {}
    for r in conn.execute(
        """
        SELECT espn_id, gsis_id FROM players p
        WHERE espn_id IS NOT NULL AND retrieved_as_of = (
            SELECT MAX(retrieved_as_of) FROM players p2 WHERE p2.gsis_id = p.gsis_id
        )
        """
    ):
        espn, gsis = r["espn_id"], r["gsis_id"]
        if espn in out and out[espn] != gsis:
            logger.warning("crosswalk: espn_id %s maps to multiple gsis (%s, %s); keeping first",
                           espn, out[espn], gsis)
            continue
        out[espn] = gsis
    return out


def select_as_of(
    conn: sqlite3.Connection,
    table: str,
    *,
    as_of,
    key_cols: Sequence[str],
    columns: str = "*",
    extra_where: str = "",
    params: Mapping | None = None,
    view: AsOfView = "historical",
) -> list[sqlite3.Row]:
    """The temporal read every accessor uses.

    ``historical`` is safe-by-default: a row is visible only when both the fact
    and this system's copy of that version existed by ``as_of``. ``latest_truth``
    keeps the fact-time gate but intentionally selects the newest retrieved
    correction even when that version arrived later. The latter is appropriate
    for final outcome grading and explicitly accepted bulk-history use; it is
    not a reconstruction of the information set available at the time.

    ``as_of`` is validated by ``normalize_as_of`` (no implicit "now").
    ``extra_where`` is appended with AND (trusted, code-authored SQL + bound
    ``params`` only).

    GRANULARITY: knowledge time is a DAY, inclusive end-of-day. ``as_of=D`` sees
    every fact knowable on D — including that day's FINAL box scores. This is
    correct for the day-granular backtest, but it cannot express an intraday
    moment (e.g. Sunday morning before the 1pm games). The live weekly loop's
    intraday sequencing (per-player kickoff locking, Sunday-AM inactive checks —
    SPEC) needs sub-day knowledge times (kickoff timestamps); that is a Phase-3
    enhancement, tracked in IMPLEMENTATION_PLAN 1.4. Until then, a same-day
    pre-kickoff caller must pass ``D-1``.
    """
    if view not in AS_OF_VIEWS:
        raise ValueError(f"unknown as-of view {view!r} (known: {AS_OF_VIEWS})")

    cutoff = normalize_as_of(as_of).isoformat()
    key_match = " AND ".join(f"t2.{k} = t.{k}" for k in key_cols)
    where_extra = f" AND ({extra_where})" if extra_where else ""
    retrieval_gate = "AND t2.retrieved_as_of <= :as_of" if view == "historical" else ""
    sql = f"""
        SELECT {columns}
        FROM {table} t
        WHERE t.knowable_as_of <= :as_of
          AND t.retrieved_as_of = (
              SELECT MAX(t2.retrieved_as_of) FROM {table} t2
              WHERE {key_match}
                AND t2.knowable_as_of <= :as_of
                {retrieval_gate}
          ){where_extra}
    """
    bound = {"as_of": cutoff, **(dict(params) if params else {})}
    return conn.execute(sql, bound).fetchall()
