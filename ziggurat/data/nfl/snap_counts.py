"""Snap counts (import_snap_counts) — item 1.4.

Per-player, per-game participation (offense/defense/special-teams snaps + share).
Two wrinkles this source carries that the crosswalk exemplar does not:

  * It is keyed on the **PFR** player id, not gsis. We resolve ``gsis_id`` at
    ingest via the players crosswalk (``base.gsis_by_pfr``) so a snap line joins
    to gsis-keyed weekly stats/NGS. Unresolved -> NULL gsis_id (rookies/DSTs the
    crosswalk lacks); the row still lands, just without the bridge. Players must
    therefore be ingested before snap counts.
  * It is a POST-GAME fact: ``knowable_as_of`` is the team's gameday for that
    (season, week), read from ``base.game_date_map`` (schedules must be ingested
    first). A row whose gameday can't be resolved is DROPPED, not stored with a
    NULL knowable_as_of (that would be an un-gateable leak).

Anatomy copied from players.py: ingest_* (frame -> cleaned rows -> upsert),
pull_* (wraps the one nfl.import_snap_counts seam tests patch), keyword-only
``as_of`` accessor through base.select_as_of. Ships a leakage test.
"""

import nfl_data_py as nfl

from ziggurat.data.nfl import base

# db_column -> source_column. 1:1 by name for every persisted column EXCEPT
# gsis_id, which is absent from the frame and resolved from the crosswalk below.
_COLMAP = {
    "pfr_player_id": "pfr_player_id",
    "player": "player",
    "position": "position",
    "team": "team",
    "opponent": "opponent",
    "season": "season",
    "week": "week",
    "game_id": "game_id",
    "offense_snaps": "offense_snaps",
    "offense_pct": "offense_pct",
    "defense_snaps": "defense_snaps",
    "defense_pct": "defense_pct",
    "st_snaps": "st_snaps",
    "st_pct": "st_pct",
}


def ingest_snap_counts(conn, df, *, retrieved_as_of: str) -> int:
    """Clean the snap-count frame and upsert it.

    ``knowable_as_of`` = the team's gameday for (season, week) from schedules;
    rows with no resolvable gameday are dropped (counted, not stored NULL).
    ``gsis_id`` is resolved from the players crosswalk (NULL if absent).
    Returns the number of rows written.
    """
    game_dates = base.game_date_map(conn)   # (season, week, team) -> gameday
    gsis_map = base.gsis_by_pfr(conn)       # pfr_id -> gsis_id

    rows = base.frame_to_rows(
        df,
        _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=lambda r: game_dates.get((r["season"], r["week"], r["team"])),
    )

    kept, dropped = [], 0
    for row in rows:
        if row["knowable_as_of"] is None:
            dropped += 1  # unresolvable gameday -> drop rather than leak a NULL gate
            continue
        row["gsis_id"] = gsis_map.get(row["pfr_player_id"])  # NULL if uncrosswalked
        kept.append(row)

    base.note_drops("snap_counts", dropped, len(rows))
    return base.upsert(conn, "snap_counts", kept)


def pull_snap_counts(conn, years, *, retrieved_as_of: str) -> int:
    """Pull real snap counts and store them. ``nfl.import_snap_counts`` is the
    single seam cached-fixture tests patch."""
    df = nfl.import_snap_counts(list(years))
    return ingest_snap_counts(conn, df, retrieved_as_of=retrieved_as_of)


def get_snap_counts(conn, *, as_of, season=None, week=None, pfr_player_id=None):
    """Snap-count rows knowable on or before ``as_of`` (keyword-only; no implicit
    now). Latest ``retrieved_as_of`` snapshot per (pfr_player_id, season, week)."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if pfr_player_id is not None:
        clauses.append("t.pfr_player_id = :pfr_player_id")
        params["pfr_player_id"] = pfr_player_id
    return base.select_as_of(
        conn, "snap_counts", as_of=as_of,
        key_cols=["pfr_player_id", "season", "week"],
        extra_where=" AND ".join(clauses), params=params,
    )
