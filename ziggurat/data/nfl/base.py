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

``select_observed_as_of`` (item 3.2c) is its sibling for a CHANGE LOG — a table
whose rows carry their own observation instant (``observed_at``) and where a key
with no row at a later instant is unchanged rather than absent. It adds one
resolution stage that ``select_as_of`` structurally cannot express; both views
mean the same thing there. Nothing about ``select_as_of`` changed.
"""

import contextvars
import functools
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
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


# Per-run drop accounting (item 3.1b). ``note_drops`` used to log and nothing
# else, and NO logging is configured in this package — under systemd the message
# reached the journal through Python's lastResort handler as a bare line with no
# level and no logger name, indistinguishable from noise, and it was persisted
# nowhere. That mattered because a 100% drop is the exact signature of running a
# gameday-stamped source before ``schedules`` exists (measured: 19,421/19,421
# dropped, return value 0, no exception) — which is indistinguishable from
# "upstream legitimately had nothing" unless the count is recorded.
#
# A ContextVar, not a module global: the collector is scoped to one source's
# ingest inside the orchestrator and must not leak across sources or threads.
_drop_tally: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ziggurat_nfl_drop_tally", default=None
)


@contextmanager
def collect_drops():
    """Tally every ``note_drops`` call made inside the block.

    Yields the mutable ``{"dropped": int, "total": int}`` dict, which is filled
    in as the ingest runs. Best-effort by construction: it counts what ingesters
    report, and an ingester that never calls ``note_drops`` contributes nothing.
    """
    tally = {"dropped": 0, "total": 0, "filtered": 0, "incomplete": 0,
             "collapsed": 0, "duplicated": 0}
    token = _drop_tally.set(tally)
    try:
        yield tally
    finally:
        _drop_tally.reset(token)


def note_drops(
    source: str,
    dropped: int,
    total: int,
    *,
    why: str = "unresolved knowledge time",
    by_design: bool = False,
) -> None:
    """Make a drop-on-ingest observable (never silent data loss). Ingesters call
    this when they skip rows they cannot stamp with a knowledge time.

    ``by_design=True`` marks a filter the league's rules make CORRECT (e.g. the
    IDP rows FantasyPros ships, which are not startable here) rather than a
    failure to handle the data. Both are logged; only the latter counts against
    ``refresh``'s drop ceiling. The distinction is not cosmetic — the first live
    run of item 3.1b failed ``adp_rankings`` on a 35% "drop" that was almost
    entirely the intentional IDP filter, and a guard that cries wolf is how the
    one report that matters gets ignored (the same reasoning that kept the
    league sync's gap report out of this module).
    """
    tally = _drop_tally.get()
    if tally is not None:
        tally["filtered" if by_design else "dropped"] += dropped
        tally["total"] += total
    if dropped:
        logger.warning(
            "%s: %s %d/%d rows (%s)",
            source, "filtered" if by_design else "dropped", dropped, total, why,
        )


def note_incomplete(source: str, count: int, total: int, *, why: str) -> None:
    """Record rows that were KEPT but are missing an optional field.

    Distinct from ``note_drops`` because nothing was lost: the rows are in the
    table and readable. ``adp_rankings`` called ``note_drops`` for these — its
    own line comment said "kept (NULL gsis_id), not dropped" — which inflated
    the drop ratio with rows that had not been dropped at all.
    """
    tally = _drop_tally.get()
    if tally is not None:
        tally["incomplete"] += count
    if count:
        logger.warning("%s: kept %d/%d rows with a missing field (%s)", source, count, total, why)


def note_collapsed(source: str, collapsed: int, duplicated: int, total: int) -> None:
    """Record rows that never reached the table because another row in the SAME
    batch carried the same primary key (item 3.2c, F-G).

    A THIRD channel, deliberately not either of the two above:

    * NOT ``note_drops(by_design=False)`` — nothing was *dropped by the ingester*;
      the rows were handed to SQLite and ``INSERT OR REPLACE`` overwrote them. The
      distinction matters for diagnosis: a drop means "we could not stamp it", a
      collapse means "our key is wrong or upstream shipped a duplicate".
    * NOT ``note_drops(by_design=True)`` — ``by_design`` is defined one screen up
      as *a filter the league's rules make CORRECT*, and ``refresh.run_ingest``
      deliberately excludes ``filtered`` from its drop ceiling. Under that label a
      source that started colliding on 50% of its rows would write half the data
      and report ``ok``. **A primary-key collision is silent data loss.**

    Two counters, because the classes are genuinely different and only one is a
    defect (F-G's "separate by FULL-ROW EQUALITY, not by relabelling the class"):

    * ``collapsed`` — same key, DIFFERENT payload. One of the two facts is gone
      and nothing else records which. This is what must reach the drop ceiling.
      Measured warrant: the pre-3.2c ``depth_charts`` PK omitted ``game_type`` and
      ``depth_team``, so 835/947/899/933 rows per season (2021-2024) vanished —
      ~700 a season differing ONLY in ``depth_team``, i.e. the depth ORDER, i.e.
      the one column the table exists for — while the ingester returned the full
      count and ``note_drops`` reported 0.
    * ``duplicated`` — same key, byte-identical payload. Upstream shipped the row
      twice; storing it once loses nothing. Measured: 145/171/182/207 per season
      on the same four files once the key is widened. Counted so an EXPLOSION is
      still visible, but kept off the ceiling.

    Both are logged; the collapse at WARNING, the benign duplicate at INFO.
    """
    tally = _drop_tally.get()
    if tally is not None:
        tally["collapsed"] += collapsed
        tally["duplicated"] += duplicated
    if collapsed:
        logger.warning(
            "%s: COLLAPSED %d/%d rows — a same-batch primary-key collision with a "
            "DIFFERENT payload overwrote them (wrong key columns, or upstream "
            "restated a row); the lost facts are unrecoverable from this table",
            source, collapsed, total,
        )
    if duplicated:
        logger.info(
            "%s: %d/%d rows were byte-identical duplicates of another row in the "
            "same batch (stored once; nothing lost)", source, duplicated, total,
        )


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


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """The table's declared PRIMARY KEY columns, in declaration order.

    Positional indexing (not ``row["name"]``) so this works whether or not the
    caller's connection sets ``row_factory``. ``PRAGMA table_info`` returns
    ``(cid, name, type, notnull, dflt_value, pk)``; ``pk`` is the 1-based position
    within the primary key, 0 for a non-key column.
    """
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    keyed = sorted((r[5], r[1]) for r in info if r[5])
    return tuple(name for _, name in keyed)


def upsert(conn: sqlite3.Connection, table: str, rows: Sequence[Mapping], *,
           key_cols: Sequence[str] | None = None, commit: bool = True) -> int:
    """Idempotent INSERT OR REPLACE of uniform row dicts. Returns rows written —
    a count that is only honest when ``key_cols`` is given (see below).

    ``commit=False`` leaves the write inside the caller's transaction. A
    replace-the-partition ingester (DELETE then insert) needs this: with the
    default per-call commit, the DELETE and the insert are two transactions, so
    a failure between them leaves the partition deleted and unreplaced — for
    league state (item 3.1) that is unrecoverable data loss, since ESPN serves
    no history. Such callers wrap both statements in one ``with conn:`` block
    and pass ``commit=False``.

    ``key_cols`` — THE HONEST COUNT (item 3.2c, F-G). Without it this function
    returns ``len(rows)``, which is the number of rows OFFERED, and that is a lie
    whenever two of them share a primary key: ``INSERT OR REPLACE`` keeps the last
    one and the caller reports the full count. Three probes hit this on three
    different tables — measured, ``ingest_snap_counts`` returned 26,468 for a 2021
    pull the table received 26,467 of, and ``note_drops`` reported 0. Pass the
    table's FULL primary key and the return value becomes the number of DISTINCT
    keys written, with the collapse reported through ``note_collapsed``.

    Detection is PRE-INSERT, on purpose: the obvious post-hoc fix is verified not
    to work. ``conn.total_changes`` counts **3 for 3 rows that collapse to 2**,
    because REPLACE reports the replacing insert as a change (and the deletion of
    the row it displaced separately). SQLite cannot tell us afterwards what it
    silently overwrote; only the batch we handed it can.

    ``key_cols`` must equal the table's declared PRIMARY KEY exactly. A subset
    would count collisions SQLite does not make (phantom loss); a superset would
    miss collisions it does make (silent loss, wearing a clean count) — the very
    defect this parameter exists to end. A mismatch raises rather than returning a
    number nobody can interpret.

    Only collisions WITHIN this batch are counted. A row that replaces one already
    in the table is an ordinary re-ingest of the same key — nothing is lost, and
    the repo's convention puts ``retrieved_as_of`` in every key so a correction is
    a new version anyway.

    Counting follows the CHAIN, not the first row seen (corrected 2026-07-25).
    ``INSERT OR REPLACE`` overwrites whatever currently occupies the key, so the
    comparison that says "was a fact lost here?" is against the row the previous
    statement left there — not against the first row of the run. Comparing
    everything to the first row reported ``A, B, B`` as **two** collapses when
    exactly one fact (A) was lost and the second B was a byte-identical duplicate
    of the row already stored. It failed toward alarm and measured 0 in every real
    season, but a tally that over-counts is a tally an operator learns to discount,
    and this one gates ``run_ingest``'s 20% drop ceiling.

    A row with a NULL in ANY key column is exempt from collision accounting. A
    composite PRIMARY KEY on an ordinary rowid table is a UNIQUE index, and in a
    UNIQUE index every NULL is distinct — SQLite stores every such row and
    replaces nothing. Python tuple equality disagrees (``(None,) == (None,)``), so
    without this exemption a table that started serving NULLs in a key column
    would report a phantom collapse for every row after the first and could fail a
    healthy pull against the drop ceiling. Such rows are still counted as written
    (they are in the table) and are logged, because a NULL key column is almost
    always a real defect in its own right: ``select_as_of`` resolves per key and a
    NULL never equals itself in the correlated match, so the row is STORED AND
    UNREADABLE. Measured on the shipped DDL: ``game_type=NULL`` -> 2 stored,
    0 returned.

    Row order is untouched: every row is still handed to ``executemany`` in the
    order given, so a caller that deliberately orders its batch (``injuries``
    keeps the most recent report per player-week) keeps deciding which row wins.
    This parameter changes the COUNT and the REPORTING, never the stored data.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"

    written = len(rows)
    if key_cols is not None:
        key_cols = tuple(key_cols)
        declared = _primary_key_columns(conn, table)
        if set(key_cols) != set(declared):
            raise ValueError(
                f"{table}: key_cols {list(key_cols)} does not match the declared "
                f"primary key {list(declared)} — the collapse count would be "
                f"wrong in one direction or the other"
            )
        missing = sorted(set(key_cols) - set(cols))
        if missing:
            raise ValueError(
                f"{table}: key_cols {missing} absent from the rows being written"
            )
        occupant: dict[tuple, Mapping] = {}
        collapsed = duplicated = null_keyed = 0
        for row in rows:
            key = tuple(row[c] for c in key_cols)
            if any(v is None for v in key):
                # SQLite never collapses these (see the docstring); neither do we.
                null_keyed += 1
                continue
            prior = occupant.get(key)
            if prior is None:
                occupant[key] = row
            elif dict(prior) == dict(row):
                # Full-row equality, not a relabelled class. Safe as a plain ==
                # because every value has been through _clean: NaN is already
                # None, so the one scalar that is not equal to itself is gone.
                duplicated += 1
            else:
                collapsed += 1
                occupant[key] = row   # follow the chain: REPLACE moved the row
        written = len(occupant) + null_keyed
        if null_keyed:
            logger.warning(
                "%s: %d/%d rows carry a NULL in a PRIMARY KEY column (%s) — SQLite "
                "stores them but select_as_of cannot resolve a NULL key, so they are "
                "written and unreadable; they are exempt from collision accounting",
                table, null_keyed, len(rows), ", ".join(key_cols),
            )
        note_collapsed(table, collapsed, duplicated, len(rows))

    conn.executemany(sql, rows)
    if commit:
        conn.commit()
    return written


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


def _gsis_preference(gsis: str | None) -> tuple[int, str]:
    """Sort key for choosing among gsis ids that collide on one source id.

    A REAL gsis is ``00-00xxxxx``. nflverse also mints pseudo-ids (``ALT577722``,
    ``32004243-4152-...``) for players it has not yet reconciled, so ``00-`` first
    then lexicographic is a total order over any collision set.
    """
    gsis = gsis or ""
    return (0 if gsis.startswith("00-") else 1, gsis)


def gsis_by_pfr(conn: sqlite3.Connection) -> dict[str, str]:
    """pfr_id -> gsis_id from the latest players snapshot (crosswalk resolution
    for PFR-keyed sources like snap counts).

    A pfr_id should map to exactly one gsis_id. It does not always: 15 pfr ids in
    the live crosswalk map to two, a pseudo-id (``ALT577722``) and a real one
    (``00-0041453``) for the same player (item 3.2c, F-J). Every collision is
    logged — never silently last-write-wins.

    **Which one is kept is DETERMINISTIC (``00-`` preferred, then lexicographic),
    not verified correct.** Nobody has established that the ``00-`` id is the right
    one; recon recorded that as unmeasured (design note §5). What is fixed here is
    a worse property: the resolving SQL has no ``ORDER BY``, so the winner was
    SQLite's scan order, and ``snap_counts`` FREEZES the resolved gsis into the
    stored row at ingest — a flip between two runs would be permanently baked in
    and the table non-idempotent, with nothing anywhere recording that it moved.
    Measured 0 flips across one re-pull, i.e. luck rather than correctness. If the
    preference is later shown to pick the wrong id, that is a one-line change here
    plus a re-pull; a nondeterministic table is not repairable at all.
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
            keep = min(out[pfr], gsis, key=_gsis_preference)
            logger.warning(
                "crosswalk: pfr_id %s maps to multiple gsis (%s, %s); keeping %s "
                "(deterministic '00-' preference — see base._gsis_preference)",
                pfr, out[pfr], gsis, keep,
            )
            out[pfr] = keep
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


def select_observed_as_of(
    conn: sqlite3.Connection,
    table: str,
    *,
    as_of,
    key_cols: Sequence[str],
    observed_col: str = "observed_at",
    columns: str = "g.*",
    extra_where: str = "",
    params: Mapping | None = None,
    view: AsOfView = "historical",
) -> list[sqlite3.Row]:
    """``select_as_of`` for a table whose rows carry their OWN observation time.

    A CHANGE LOG, not a snapshot table (item 3.2c, the depth-chart panel). A row
    says "at this instant, this key's value became X"; a key with no row at a
    later instant is UNCHANGED, and a key whose value became "vacant" is recorded
    by a tombstone row (a positive fact — the same lesson item 3.1 paid for with
    ``on_team_id IS NULL``). Reading such a table needs THREE stages, in this
    order:

      1. gate:  ``knowable_as_of <= as_of``  (+ ``retrieved_as_of <= as_of``
                under ``historical``) — identical to ``select_as_of``.
      2. per ``key_cols``:  ``MAX(observed_col)``  — WHICH observation is current.
      3. per ``(key_cols, observed_col)``:  ``MAX(retrieved_as_of)`` — which
         VERSION of that observation (a later correction to the same instant).

    ``select_as_of`` is the degenerate case with no step 2, and step 2 is exactly
    what it cannot express: its correlated MAX is on ``retrieved_as_of`` ONLY, so
    a bulk backfill — one retrieval stamp for a whole season — makes every version
    of every key tie, and the read returns the ENTIRE HISTORY of each key as if it
    were all current. Measured on the real 2025 panel: **3,572 rows where 2,255
    was right**, a 58% inflated board showing one team both a QB3 and a QB4 who
    are the same player. Running the two resolutions in the other order does not
    help either, and dropping step 2 in favour of "just take the newest retrieval"
    resurrects ghosts — a phantom rank-4 carried forward seven weeks — because a
    slot that vacated has no newer row of its own.

    The caller supplies the tombstone predicate through ``extra_where`` (e.g.
    ``"g.espn_id IS NOT NULL"``). It CANNOT be applied inside stages 2/3: a
    tombstone must win the MAX so it can hide the row it retires, and only then be
    filtered from the output. Filtering first is precisely the ghost bug.

    COST, measured on this box against the shipped DDL with a 36,048-row season
    (larger than the real 2025 panel's 27,674): whole-league 91 ms, one team
    5 ms; both correlated subqueries resolve through the table's PK autoindex.
    A row-value variant (68 ms) and a ROW_NUMBER() window variant (63 ms) return
    byte-identical results and were rejected — the margin is not decision-relevant
    on the hot path (one team), and this shape is the one an auditor can read
    stage by stage. The design note's 20–44 ms is a different prototype's number.

    ``as_of`` is validated by ``normalize_as_of`` (no implicit "now"), and the
    day-granular contract in ``select_as_of``'s docstring applies unchanged —
    ``observed_col`` orders WITHIN a day (four measured days carry 2-3 panels) but
    does not make ``as_of`` intraday. ``extra_where`` is trusted, code-authored SQL
    plus bound ``params``; the outer row is aliased ``g``. Every ``key_cols``
    column must be NOT NULL in the table (a NULL never equals itself in the
    correlated joins, so a NULL-keyed row would resolve against nothing and leak
    its whole history) — the shipped ``depth_chart_slots`` declares them so.
    """
    if view not in AS_OF_VIEWS:
        raise ValueError(f"unknown as-of view {view!r} (known: {AS_OF_VIEWS})")
    if not key_cols:
        raise ValueError("select_observed_as_of requires at least one key column")

    cutoff = normalize_as_of(as_of).isoformat()
    obs_match = " AND ".join(f"o.{k} = g.{k}" for k in key_cols)
    ver_match = " AND ".join(f"v.{k} = g.{k}" for k in key_cols)
    where_extra = f" AND ({extra_where})" if extra_where else ""
    historical = view == "historical"
    gate_g = "AND g.retrieved_as_of <= :as_of" if historical else ""
    gate_o = "AND o.retrieved_as_of <= :as_of" if historical else ""
    gate_v = "AND v.retrieved_as_of <= :as_of" if historical else ""
    sql = f"""
        SELECT {columns}
        FROM {table} g
        WHERE g.knowable_as_of <= :as_of
          {gate_g}
          AND g.{observed_col} = (
              SELECT MAX(o.{observed_col}) FROM {table} o
              WHERE {obs_match}
                AND o.knowable_as_of <= :as_of
                {gate_o}
          )
          AND g.retrieved_as_of = (
              SELECT MAX(v.retrieved_as_of) FROM {table} v
              WHERE {ver_match}
                AND v.{observed_col} = g.{observed_col}
                AND v.knowable_as_of <= :as_of
                {gate_v}
          ){where_extra}
    """
    bound = {"as_of": cutoff, **(dict(params) if params else {})}
    return conn.execute(sql, bound).fetchall()
