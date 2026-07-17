"""Weekly player box score + usage ingestion (import_weekly_data) — item 1.4.

The per-week statistical spine: one row per (player, season, week) carrying the
box-score and usage columns scoring.py consumes (nflverse naming, so a row
scores directly). Keyed on gsis_id (the frame's ``player_id``), it joins to the
crosswalk, snap counts, and NGS.

knowable_as_of is a post-game fact: a player's week-N line becomes knowable on
the day their team played, so it is stamped with the team gameday from
``base.game_date_map`` (schedules must be ingested first). A row whose
(season, week, recent_team) can't be resolved to a gameday is dropped, never
inserted with a NULL knowledge time — dropping is the leakage-safe default.
"""

import nfl_data_py as nfl

from ziggurat.data.nfl import base

# Columns we persist; each maps 1:1 to the import_weekly_data frame by name.
# (player_id is the gsis id.) Excludes base's retrieved_as_of/knowable_as_of.
_COLUMNS = (
    "player_id", "season", "week", "season_type", "position",
    "recent_team", "opponent_team",
    # passing
    "completions", "attempts", "passing_yards", "passing_tds", "interceptions",
    "sacks", "sack_fumbles_lost", "passing_air_yards", "passing_epa",
    "passing_2pt_conversions",
    # rushing
    "carries", "rushing_yards", "rushing_tds", "rushing_fumbles_lost",
    "rushing_epa", "rushing_2pt_conversions",
    # receiving
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "receiving_fumbles_lost", "receiving_air_yards", "receiving_epa",
    "receiving_2pt_conversions",
    # usage shares
    "target_share", "air_yards_share", "wopr", "special_teams_tds",
    "fantasy_points_ppr",
)


def ingest_weekly_stats(conn, df, *, retrieved_as_of: str) -> int:
    """Persist weekly stats, stamping knowable_as_of with the team gameday.

    Requires schedules already ingested so ``base.game_date_map`` resolves.
    Rows whose (season, week, recent_team) has no gameday are dropped (counted
    via the difference between the frame length and the return value).
    """
    gdm = base.game_date_map(conn)

    def _knowable(r):
        return gdm.get((int(r["season"]), int(r["week"]), r["recent_team"]))

    rows = base.frame_to_rows(
        df,
        {c: c for c in _COLUMNS},
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=_knowable,
    )
    resolved = [row for row in rows if row["knowable_as_of"] is not None]
    base.note_drops("weekly_stats", len(rows) - len(resolved), len(rows))
    return base.upsert(conn, "weekly_stats", resolved)


def pull_weekly_stats(conn, years, *, retrieved_as_of: str) -> int:
    """Pull weekly box scores for ``years``. The ``nfl.import_weekly_data`` call
    is the seam cached-fixture tests patch."""
    df = nfl.import_weekly_data(list(years))
    return ingest_weekly_stats(conn, df, retrieved_as_of=retrieved_as_of)


def get_weekly_stats(conn, *, as_of, season=None, week=None, player_id=None, position=None):
    """Weekly stat rows knowable on or before ``as_of`` (keyword-only; no implicit
    now). Latest snapshot per (player_id, season, week)."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if player_id is not None:
        clauses.append("t.player_id = :player_id")
        params["player_id"] = player_id
    if position is not None:
        clauses.append("t.position = :position")
        params["position"] = position
    return base.select_as_of(
        conn, "weekly_stats", as_of=as_of,
        key_cols=["player_id", "season", "week"],
        extra_where=" AND ".join(clauses), params=params,
    )
