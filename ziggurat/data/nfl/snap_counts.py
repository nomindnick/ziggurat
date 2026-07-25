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
  * A (player, season, week) is NOT unique: a mid-week move gives one player a
    snap line for each club that week. ``team`` is therefore part of both the
    stored PK (migration 007) and ``_KEY_COLS`` below — see the note there.

Anatomy copied from players.py: ingest_* (frame -> cleaned rows -> upsert),
pull_* (wraps the one nfl.import_snap_counts seam tests patch), keyword-only
``as_of`` accessor through base.select_as_of. Ships a leakage test.
"""

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

# The natural key a snapshot resolves on. ``team`` is in it because a player can
# appear TWICE in one (season, week): a mid-week trade or waiver claim gives him
# a snap line for each club. Measured live 2026-07-25 on the real 2021 file:
#
#   DaviJa06 / Jalen Davis, 2021 week 12 -> MIA 10 defensive snaps (2021_12_CAR_MIA)
#                                        -> CIN 23 defensive snaps (2021_12_PIT_CIN)
#
# Before item 3.2c the stored PK was (pfr_player_id, season, week, retrieved_as_of),
# so the second row REPLACED the first: the ingester returned 26,468 and the table
# held 26,467, with ``note_drops`` reporting 0 — silent loss. Migration 007 widened
# the PK to include ``team``; this list must match it, or ``select_as_of``'s
# correlated MAX() would resolve the two clubs' rows against each other and hide
# whichever carries the older ``retrieved_as_of``.
#
# A NULL ``team`` cannot occur: ``ingest_snap_counts`` resolves knowable_as_of via
# ``game_date_map[(season, week, team)]``, so a team-less row never resolves a
# gameday and is dropped before storage. (That matters because ``t2.team = t.team``
# is NULL-blind — a NULL-team row written by some other path would read as absent.)
_KEY_COLS = ["pfr_player_id", "season", "week", "team"]

# The stored PRIMARY KEY = the natural key plus the revision column. Passed to
# ``base.upsert`` so the count it returns is distinct keys written, not rows
# offered; derived from _KEY_COLS so the two can never drift apart.
_PK_COLS = tuple(_KEY_COLS) + ("retrieved_as_of",)

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
    base.require_columns(df, list(_COLMAP.values()), source="snap_counts")
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
    # key_cols = the table's FULL declared PK, so the return value is the number
    # of DISTINCT keys written rather than the number of rows offered. This is the
    # call that measured F-E in the first place: 26,468 offered, 26,467 stored,
    # 0 reported. After migration 007 it is 0 collapsed on every live season
    # measured (2021/2024/2025) — and it stays measured, instead of assumed.
    return base.upsert(conn, "snap_counts", kept, key_cols=_PK_COLS)


def pull_snap_counts(conn, years, *, retrieved_as_of: str) -> int:
    """Pull real snap counts and store them. ``nfl.import_snap_counts`` is the
    single seam cached-fixture tests patch."""
    df = nfl.import_snap_counts(list(years))
    return ingest_snap_counts(conn, df, retrieved_as_of=retrieved_as_of)


def get_snap_counts(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    pfr_player_id=None,
    team=None,
    view: base.AsOfView = "historical",
):
    """Snap-count rows knowable on or before ``as_of`` (keyword-only; no implicit
    now). Latest ``retrieved_as_of`` snapshot per
    (pfr_player_id, season, week, team).

    A player traded mid-week legitimately returns TWO rows for one (player, week)
    — one per club. Callers that need a single line per player-week must decide
    whether to sum them or pick the club they care about (``team=``); they must
    not assume uniqueness.
    """
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
    if team is not None:
        clauses.append("t.team = :team")
        params["team"] = team
    return base.select_as_of(
        conn, "snap_counts", as_of=as_of,
        key_cols=_KEY_COLS,
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
