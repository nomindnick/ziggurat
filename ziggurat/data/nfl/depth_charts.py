"""Depth chart ingestion (import_depth_charts) — item 1.4.

Forward-looking weekly data: a week's depth chart projects who lines up where,
published *before* the games are played, and it carries no publish timestamp of
its own. So ``knowable_as_of`` is anchored to the week's FIRST kickoff
(``base.week_first_gameday_map``) — leakage-safe (never earlier than the week's
own games) and requiring the schedules table to be ingested first. A row whose
(season, week) has no resolvable gameday is dropped (counted), not stored with a
NULL ``knowable_as_of``.

Anatomy copied from players.py: an ``ingest_*`` (frame -> cleaned rows ->
upsert), a ``pull_*`` (wraps the one ``nfl.import_depth_charts`` seam), and a
keyword-only ``as_of`` accessor routing through ``base.select_as_of``. Ships a
leakage test.
"""

import nfl_data_py as nfl

from ziggurat.data.nfl import base

# Depth-chart columns we persist (each maps 1:1 to the import_depth_charts frame;
# base stamps retrieved_as_of / knowable_as_of).
_COLUMNS = (
    "gsis_id", "season", "week", "game_type", "club_code", "position",
    "depth_position", "depth_team", "formation", "full_name",
)

# Natural key for the latest-snapshot-per-key read (matches the table PK minus
# retrieved_as_of).
_KEY_COLS = ("season", "week", "club_code", "formation", "position", "depth_position", "gsis_id")


def _season_week(src):
    """(season, week) as plain ints for the week_first_gameday_map lookup, or
    None if either is missing/NaN (no gameday can be resolved -> drop the row)."""
    try:
        return (int(src.get("season")), int(src.get("week")))
    except (TypeError, ValueError):
        return None


def ingest_depth_charts(conn, df, *, retrieved_as_of: str) -> int:
    """Clean + upsert depth charts, stamping ``knowable_as_of`` = the week's first
    gameday (schedules MUST already be ingested so ``week_first_gameday_map``
    resolves). Rows whose (season, week) has no schedule -> no gameday are
    dropped rather than stored with a NULL ``knowable_as_of``. Returns rows
    written."""
    week_first = base.week_first_gameday_map(conn)

    def knowable(src) -> str | None:
        key = _season_week(src)
        return week_first.get(key) if key is not None else None

    rows = base.frame_to_rows(
        df,
        {c: c for c in _COLUMNS},
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=knowable,
    )
    resolved = [r for r in rows if r["knowable_as_of"] is not None]
    base.note_drops("depth_charts", len(rows) - len(resolved), len(rows))
    # Coalesce the nullable PK members to '' so INSERT OR REPLACE can dedupe on
    # re-ingest (SQLite treats NULLs as distinct in the PK's UNIQUE index) and so
    # select_as_of's `t2.k = t.k` key match — which NULL never satisfies — can
    # still see these rows. '' means "not present in the depth-chart source".
    for r in resolved:
        if r["gsis_id"] is None:
            r["gsis_id"] = ""
        if r["depth_position"] is None:
            r["depth_position"] = ""
    return base.upsert(conn, "depth_charts", resolved)


def pull_depth_charts(conn, years, *, retrieved_as_of: str) -> int:
    """Pull real depth charts and store them. The ``nfl.import_depth_charts`` call
    is the seam cached-fixture tests patch."""
    df = nfl.import_depth_charts(list(years))
    return ingest_depth_charts(conn, df, retrieved_as_of=retrieved_as_of)


def get_depth_chart(conn, *, as_of, season=None, week=None, team=None):
    """Depth-chart rows knowable on or before ``as_of`` (keyword-only; no implicit
    now). ``team`` filters ``club_code``. Returns the latest ``retrieved_as_of``
    snapshot per natural key among rows both knowable and retrieved by ``as_of``."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if team is not None:
        clauses.append("t.club_code = :team")
        params["team"] = team
    return base.select_as_of(
        conn, "depth_charts", as_of=as_of, key_cols=list(_KEY_COLS),
        extra_where=" AND ".join(clauses), params=params,
    )
