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

import nfl_data_py as nfl

from ziggurat.data.nfl import base

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


def ingest_players(conn, df, *, retrieved_as_of: str) -> int:
    df = df.dropna(subset=["gsis_id"]).drop_duplicates(subset=["gsis_id"])
    rows = base.frame_to_rows(
        df, _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=base.iso_date(retrieved_as_of),  # known when pulled
    )
    for row in rows:
        for col in _NUMERIC_ID_COLS:
            row[col] = _norm_id(row[col])
    return base.upsert(conn, "players", rows)


def pull_players(conn, *, retrieved_as_of: str) -> int:
    """Pull the DynastyProcess/nflverse id crosswalk. ``nfl.import_ids`` is the
    seam cached-fixture tests patch."""
    df = nfl.import_ids()
    return ingest_players(conn, df, retrieved_as_of=retrieved_as_of)


def get_players(conn, *, as_of):
    """Crosswalk rows knowable on or before ``as_of`` (latest snapshot per gsis)."""
    return base.select_as_of(conn, "players", as_of=as_of, key_cols=["gsis_id"])


def id_crosswalk(conn, *, as_of, id_from: str = "gsis_id", id_to: str = "espn_id") -> dict:
    """Map one id space to another as of ``as_of`` (e.g. espn_id -> gsis_id)."""
    rows = get_players(conn, as_of=as_of)
    return {r[id_from]: r[id_to] for r in rows if r[id_from] is not None and r[id_to] is not None}
