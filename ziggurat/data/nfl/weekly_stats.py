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

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

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

# The stored PRIMARY KEY, passed to ``base.upsert`` so its return value is the
# number of DISTINCT keys written rather than rows offered (item 3.2c, F-G).
# Measured 0 same-batch collisions on live 2021/2024/2025, so this changes no
# count today — it makes a future upstream regrain visible instead of silent.
_PK_COLS = ("player_id", "season", "week", "retrieved_as_of")


def ingest_weekly_stats(conn, df, *, retrieved_as_of: str) -> int:
    """Persist weekly stats, stamping knowable_as_of with the team gameday.

    Requires schedules already ingested so ``base.game_date_map`` resolves.
    Rows whose (season, week, recent_team) has no gameday are dropped (counted
    via the difference between the frame length and the return value).

    Two drop classes are reported through ONE ``note_drops`` call. That is not
    tidiness: ``base.collect_drops`` SUMS ``total`` across calls, so the two
    calls this used to make reported ``{'dropped': 22, 'total': 37916}`` for an
    18,969-row frame — a denominator larger than the number of rows that ever
    existed (item 3.2c, finding F-H). It was cosmetic only because
    ``refresh.run_ingest`` computes its own ``seen = written + dropped`` and
    never reads ``tally['total']``; it was still wrong in the module whose job
    is drop accounting.
    """
    base.require_columns(df, _COLUMNS, source="weekly_stats")
    # nflverse ships all-zero placeholder rows with a NULL player_id (measured
    # 2026-07-24: 22 of 19,421 rows in stats_player_week_2025, one per week).
    # player_id is the NOT NULL primary key here, so leaving them in made the
    # WHOLE pull raise IntegrityError mid-executemany — which, on a shared
    # connection, left a partial week-1-only table for the next source's commit
    # to persist (item 3.1b). Drop them the way every other unresolvable key is
    # dropped: counted, never silent.
    total = len(df)
    df = df.dropna(subset=["player_id"])
    null_ids = total - len(df)
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
    unresolved = len(rows) - len(resolved)

    why = []
    if null_ids:
        why.append(f"{null_ids} null player_id")
    if unresolved:
        why.append(f"{unresolved} unresolved knowledge time")
    base.note_drops(
        "weekly_stats", null_ids + unresolved, total,
        why="; ".join(why) or "unresolved knowledge time",
    )
    return base.upsert(conn, "weekly_stats", resolved, key_cols=_PK_COLS)


def pull_weekly_stats(conn, years, *, retrieved_as_of: str) -> int:
    """Pull weekly box scores for ``years``. The ``nfl.import_weekly_data`` call
    is the seam cached-fixture tests patch."""
    df = nfl.import_weekly_data(list(years))
    return ingest_weekly_stats(conn, df, retrieved_as_of=retrieved_as_of)


def get_weekly_stats(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    player_id=None,
    position=None,
    view: base.AsOfView = "historical",
):
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
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
