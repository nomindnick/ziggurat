"""Next Gen Stats ingestion (import_ngs_data) — item 1.4.

Three sibling tables (receiving / rushing / passing) sharing one shape: a
per-(player, week) line of tracking-derived metrics keyed on
``player_gsis_id, season, week``. They are post-game facts, so ``knowable_as_of``
is the day that player's team played — resolved through
``base.game_date_map`` on ``(season, week, team_abbr)``. A week-N line whose game
date can't be resolved (schedules not yet ingested) is dropped rather than
stored with a leaky/NULL knowledge time.

Real NGS frames carry ``week == 0`` season-aggregate rows; those have no single
gameday and are filtered out on ingest (the cached fixture is weeks 5-6 already).
They also number the Super Bowl **week 23** where ``schedules`` numbers it 22, so
a handful of rows a season legitimately never resolve a gameday; see
``_NGS_SUPER_BOWL_WEEK`` for why that is a message fix and not a remap.

The three tables differ only in their metric columns, so ingest/pull/get are one
shared internal helper each, wrapped by three thin public functions apiece.
"""

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

# Persisted columns per table (each maps 1:1 to the import_ngs_data frame by
# name). key + team_abbr are shared; the rest are the table's metrics.
_KEY_COLS = ("player_gsis_id", "season", "week", "team_abbr")

_COLUMNS = {
    "ngs_receiving": _KEY_COLS + (
        "avg_cushion", "avg_separation", "avg_intended_air_yards",
        "percent_share_of_intended_air_yards", "catch_percentage", "avg_yac",
        "avg_expected_yac", "avg_yac_above_expectation",
    ),
    "ngs_rushing": _KEY_COLS + (
        "efficiency", "percent_attempts_gte_eight_defenders", "avg_time_to_los",
        "expected_rush_yards", "rush_yards_over_expected",
        "rush_yards_over_expected_per_att", "rush_pct_over_expected",
    ),
    "ngs_passing": _KEY_COLS + (
        "avg_time_to_throw", "avg_intended_air_yards", "avg_air_yards_differential",
        "aggressiveness", "expected_completion_percentage",
        "completion_percentage_above_expectation",
    ),
}

# stat_type passed to nfl.import_ngs_data for each table.
_STAT_TYPE = {
    "ngs_receiving": "receiving",
    "ngs_rushing": "rushing",
    "ngs_passing": "passing",
}

_NATURAL_KEY = ["player_gsis_id", "season", "week"]

# The stored PRIMARY KEY (identical across the three sibling tables), passed to
# ``base.upsert`` so its return value is the number of DISTINCT keys written
# rather than rows offered (item 3.2c, F-G). Measured 0 same-batch collisions on
# live 2021/2024/2025 for all three tables.
_PK_COLS = ("player_gsis_id", "season", "week", "retrieved_as_of")

# NGS and schedules disagree about ONE week number, and only that one. NGS runs
# 1-18 REG then 19 WC, 20 DIV, 21 CONF, and numbers the Super Bowl **23** (it
# never emits a 22 at all); ``schedules`` numbers the Super Bowl **22**. So every
# NGS Super Bowl row fails ``game_date_map`` and is dropped — with the generic
# "unresolved knowledge time" message, which reads like a mystery when it is a
# fully explained structural mismatch.
#
# Measured live 2026-07-25, all three tables x 2021/2023/2024: the unresolved
# population is 100% week 23, and its ``team_abbr`` set is exactly that season's
# two Super Bowl participants every time (2021 CIN+LAR, 2023 KC+SF, 2024 KC+PHI)
# — 1 to 7 rows per table per season.
#
# The MESSAGE is the fix, deliberately not the data:
#   * we do NOT remap 23 -> 22. That would assert a week number upstream did not
#     give us, and bake an inference into a stored fact.
#   * the rows stay in the ``dropped`` (ceiling-counting) channel, NOT
#     ``by_design=True``. If this population ever explodes — a new postseason
#     round, a renumbering — it must still alarm. Labelling it "by design" is how
#     an alarm gets trained away.
_NGS_SUPER_BOWL_WEEK = 23


def _drop_reason(unresolved_weeks: list[int]) -> str:
    """Explain the unresolved-gameday population instead of just naming it."""
    sb = sum(1 for w in unresolved_weeks if w == _NGS_SUPER_BOWL_WEEK)
    other = len(unresolved_weeks) - sb
    if sb and not other:
        subject = f"all {sb} are" if sb > 1 else "the 1 dropped row is"
        return (
            f"{subject} NGS week {_NGS_SUPER_BOWL_WEEK} = the Super Bowl, which "
            "schedules numbers 22 — a known structural week-numbering mismatch, "
            "not an unexplained gap"
        )
    if sb:
        return (
            f"{sb} are NGS week {_NGS_SUPER_BOWL_WEEK} = the Super Bowl (schedules "
            f"numbers it 22 — known structural mismatch); {other} are NOT explained "
            "by that and need investigating"
        )
    return "unresolved knowledge time"


def _ingest(conn, df, *, table: str, retrieved_as_of: str) -> int:
    """Filter to real weeks, stamp gameday knowledge time, drop unresolved rows.

    A row's ``knowable_as_of`` is the gameday of its ``(season, week, team_abbr)``
    game. Rows with ``week == 0`` (season aggregates) or an unresolvable game date
    are dropped (and counted) instead of persisted with a leaky knowledge time.
    """
    base.require_columns(df, _COLUMNS[table], source=table)
    df = df[df["week"] > 0]
    game_dates = base.game_date_map(conn)

    def knowable_as_of(src):
        return game_dates.get((int(src["season"]), int(src["week"]), src["team_abbr"]))

    rows = base.frame_to_rows(
        df,
        {c: c for c in _COLUMNS[table]},
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=knowable_as_of,
    )
    resolved, unresolved_weeks = [], []
    for r in rows:
        if r["knowable_as_of"] is None:
            unresolved_weeks.append(r["week"])
        else:
            resolved.append(r)
    base.note_drops(table, len(unresolved_weeks), len(rows), why=_drop_reason(unresolved_weeks))
    return base.upsert(conn, table, resolved, key_cols=_PK_COLS)


def _pull(conn, years, *, table: str, retrieved_as_of: str) -> int:
    """Pull one NGS stat type and store it. ``nfl.import_ngs_data`` is the seam
    cached-fixture tests patch."""
    df = nfl.import_ngs_data(_STAT_TYPE[table], list(years))
    return _ingest(conn, df, table=table, retrieved_as_of=retrieved_as_of)


def _get(
    conn,
    *,
    table: str,
    as_of,
    season=None,
    week=None,
    player_gsis_id=None,
    view: base.AsOfView = "historical",
):
    """NGS lines knowable on or before ``as_of`` (keyword-only; no implicit now)."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if player_gsis_id is not None:
        clauses.append("t.player_gsis_id = :player_gsis_id")
        params["player_gsis_id"] = player_gsis_id
    return base.select_as_of(
        conn, table, as_of=as_of, key_cols=_NATURAL_KEY,
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


# --- receiving ---------------------------------------------------------------
def ingest_ngs_receiving(conn, df, *, retrieved_as_of: str) -> int:
    return _ingest(conn, df, table="ngs_receiving", retrieved_as_of=retrieved_as_of)


def pull_ngs_receiving(conn, years, *, retrieved_as_of: str) -> int:
    return _pull(conn, years, table="ngs_receiving", retrieved_as_of=retrieved_as_of)


def get_ngs_receiving(
    conn, *, as_of, season=None, week=None, player_gsis_id=None,
    view: base.AsOfView = "historical",
):
    return _get(conn, table="ngs_receiving", as_of=as_of, season=season,
                week=week, player_gsis_id=player_gsis_id, view=view)


# --- rushing -----------------------------------------------------------------
def ingest_ngs_rushing(conn, df, *, retrieved_as_of: str) -> int:
    return _ingest(conn, df, table="ngs_rushing", retrieved_as_of=retrieved_as_of)


def pull_ngs_rushing(conn, years, *, retrieved_as_of: str) -> int:
    return _pull(conn, years, table="ngs_rushing", retrieved_as_of=retrieved_as_of)


def get_ngs_rushing(
    conn, *, as_of, season=None, week=None, player_gsis_id=None,
    view: base.AsOfView = "historical",
):
    return _get(conn, table="ngs_rushing", as_of=as_of, season=season,
                week=week, player_gsis_id=player_gsis_id, view=view)


# --- passing -----------------------------------------------------------------
def ingest_ngs_passing(conn, df, *, retrieved_as_of: str) -> int:
    return _ingest(conn, df, table="ngs_passing", retrieved_as_of=retrieved_as_of)


def pull_ngs_passing(conn, years, *, retrieved_as_of: str) -> int:
    return _pull(conn, years, table="ngs_passing", retrieved_as_of=retrieved_as_of)


def get_ngs_passing(
    conn, *, as_of, season=None, week=None, player_gsis_id=None,
    view: base.AsOfView = "historical",
):
    return _get(conn, table="ngs_passing", as_of=as_of, season=season,
                week=week, player_gsis_id=player_gsis_id, view=view)
