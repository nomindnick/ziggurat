"""Player cross-ID crosswalk (import_ids) — item 1.4, and the EXEMPLAR every
other nflverse source copies.

The crosswalk is the backbone: it stitches gsis-keyed sources (weekly stats,
NGS, depth charts, injuries) to pfr-keyed snap counts, to the ESPN league
(espn_id) and the Phase-4 Sleeper proxy (sleeper_id). It stores STABLE identity
only; time-varying team/status live in the per-week tables. D/STs and some
rookies are absent here by construction — validated below, and defenses are
keyed by team abbr elsewhere.

Anatomy each source repeats: a `ingest_*` (frame -> cleaned rows -> upsert), a
`pull_*` (wraps one nfl.import_* seam), and keyword-only `as_of` accessors that
route through base.select_as_of. Ships with a leakage test.
"""

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

_COLMAP = {
    "gsis_id": "gsis_id",
    "pfr_id": "pfr_id",
    "espn_id": "espn_id",
    "sleeper_id": "sleeper_id",
    "yahoo_id": "yahoo_id",
    "mfl_id": "mfl_id",
    "fantasypros_id": "fantasypros_id",
    "sportradar_id": "sportradar_id",
    "name": "name",
    "merge_name": "merge_name",
    "position": "position",
    "birthdate": "birthdate",
}

# ID columns that arrive as floats (e.g. espn_id 4837248.0) and must be
# normalized to plain digit strings so they join to ESPN/Sleeper/Yahoo ids.
_NUMERIC_ID_COLS = ("espn_id", "sleeper_id", "yahoo_id", "mfl_id", "fantasypros_id")


# The id columns with production consumers. base.espn_by_gsis / gsis_by_pfr /
# ids_by_fantasypros and projections._sleeper_to_gsis all resolve MAX(retrieved_as_of)
# PER gsis_id, so today's row SHADOWS yesterday's for that player even when it is
# emptier — a column-present/values-null upstream regression takes every crosswalk
# to zero while the good rows sit unreachable underneath. Measured on a copy of
# the live DB (3.1b audit): espn_by_gsis 7897 -> 0, gsis_by_pfr 7784 -> 0,
# ids_by_fantasypros 4709 -> 0, sleeper->gsis 6149 -> 0, run logged `ok`.
_GUARDED_ID_COLS = ("espn_id", "pfr_id", "fantasypros_id", "sleeper_id")

# Fraction of the previous snapshot's per-column coverage an incoming pull must
# keep. Same value and same reasoning as espn_ranks._MIN_BOARD_FRACTION and
# league.state._MIN_SNAPSHOT_FRACTION: a refused pull is retried by the next run.
_MIN_ID_COVERAGE_FRACTION = 0.75


class CrosswalkCollapse(RuntimeError):
    """A degraded players pull would have hidden the good crosswalk underneath it.

    The append-only tables were argued to "need no floor" because a thin pull
    leaves untouched keys resolving to their older rows. That is true for MISSING
    ROWS and false for EMPTIED VALUES: the as-of resolution is per key, so a row
    that arrives with null ids is not absent, it wins.
    """


def _id_coverage(rows) -> dict:
    """Non-null count per guarded id column across a list of row dicts."""
    return {col: sum(1 for r in rows if r.get(col) not in (None, "")) for col in _GUARDED_ID_COLS}


def _stored_id_coverage(conn) -> tuple[dict, int]:
    """Non-null count per guarded id column in the most recent stored snapshot."""
    day = conn.execute("SELECT MAX(retrieved_as_of) FROM players").fetchone()[0]
    if day is None:
        return {}, 0
    cols = ", ".join(f"SUM({c} IS NOT NULL AND {c} != '')" for c in _GUARDED_ID_COLS)
    row = conn.execute(
        f"SELECT COUNT(*), {cols} FROM players WHERE retrieved_as_of = ?", (day,)
    ).fetchone()
    return {col: row[i + 1] or 0 for i, col in enumerate(_GUARDED_ID_COLS)}, row[0]


def _check_id_coverage(conn, rows, *, allow_shrink: bool) -> None:
    """Refuse a pull whose id columns collapsed relative to the last snapshot.

    Checked BEFORE the write, like every other floor in this codebase. There is
    no repair command for a bad players partition, and the crosswalk is the join
    key of the ESPN value view, ADP, snap counts and projections — three weeks
    before draft day the cost of a refusal (retry tomorrow) is far below the cost
    of a silent zero.
    """
    if allow_shrink or not rows:
        return
    stored, stored_rows = _stored_id_coverage(conn)
    if not stored_rows:
        return  # first pull: nothing to compare
    incoming = _id_coverage(rows)
    for col in _GUARDED_ID_COLS:
        # A RATE, not a count: a legitimately smaller pull keeps its coverage rate
        # (and is harmless anyway — untouched keys keep resolving to their older
        # rows, verified), while a values-emptied pull takes the rate to zero.
        was = stored[col] / stored_rows
        now = incoming[col] / len(rows)
        if was and now < was * _MIN_ID_COVERAGE_FRACTION:
            raise CrosswalkCollapse(
                f"refusing the players pull: {col} is populated on {incoming[col]} of "
                f"{len(rows)} incoming rows ({now:.0%}) vs {stored[col]} of {stored_rows} "
                f"stored ({was:.0%}; floor {_MIN_ID_COVERAGE_FRACTION:.0%} of that). "
                "Upstream likely "
                "served the column empty; writing it would SHADOW the good crosswalk, "
                "because the as-of read resolves the newest row per gsis_id. The stored "
                "crosswalk is untouched; the next run will retry. Use allow_shrink only "
                "if the drop is real."
            )


def _norm_id(value):
    """Normalize a numeric-looking id to a bare digit string ('4837248.0'->'4837248')."""
    if value is None:
        return None
    if isinstance(value, float):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def ingest_players(conn, df, *, retrieved_as_of: str, allow_shrink: bool = False) -> int:
    base.require_columns(df, list(_COLMAP.values()), source="players")
    df = df.dropna(subset=["gsis_id"]).drop_duplicates(subset=["gsis_id"])
    rows = base.frame_to_rows(
        df, _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=base.iso_date(retrieved_as_of),  # known when pulled
    )
    for row in rows:
        for col in _NUMERIC_ID_COLS:
            row[col] = _norm_id(row[col])
    _check_id_coverage(conn, rows, allow_shrink=allow_shrink)
    return base.upsert(conn, "players", rows)


def pull_players(conn, *, retrieved_as_of: str, allow_shrink: bool = False) -> int:
    """Pull the DynastyProcess/nflverse id crosswalk. ``nfl.import_ids`` is the
    seam cached-fixture tests patch."""
    df = nfl.import_ids()
    return ingest_players(conn, df, retrieved_as_of=retrieved_as_of, allow_shrink=allow_shrink)


def get_players(conn, *, as_of, view: base.AsOfView = "historical"):
    """Crosswalk rows knowable on or before ``as_of`` (latest snapshot per gsis)."""
    return base.select_as_of(conn, "players", as_of=as_of, key_cols=["gsis_id"], view=view)


def id_crosswalk(
    conn,
    *,
    as_of,
    id_from: str = "gsis_id",
    id_to: str = "espn_id",
    view: base.AsOfView = "historical",
) -> dict:
    """Map one id space to another as of ``as_of`` (e.g. espn_id -> gsis_id)."""
    rows = get_players(conn, as_of=as_of, view=view)
    return {r[id_from]: r[id_to] for r in rows if r[id_from] is not None and r[id_to] is not None}
