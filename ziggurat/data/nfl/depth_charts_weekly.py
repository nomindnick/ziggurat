"""Depth charts, LEGACY weekly regime (2021-2024) — item 3.2c.

This is the shipped item-1.4 ingester, moved here unchanged in spirit and fixed
in two places. Upstream replaced this shape with a dated daily panel for 2025+;
that regime lives in ``depth_charts.py`` and in different tables.

**Two regimes, two tables, permanently.** Merging them was measured and is a
data-fabrication bug: four of the panel table's five key columns (``pos_grp_id``,
``pos_id``, ``pos_rank``, ``espn_id``) do not exist in this frame, and the panel's
occupant column doubles as its tombstone sentinel — so the obvious rescue (join
``gsis_id`` -> ``espn_id`` through the crosswalk, 82% resolvable) would have
landed ~148k rows of which one in six was a fabricated "this slot was vacated"
fact, biased toward OL/LS/practice squad, i.e. invisible to any skill-position
smoke check. ``depth_charts_weekly`` is never dropped; it is the only copy of the
weekly shape.

Forward-looking weekly data: a week's depth chart projects who lines up where,
published *before* the games are played, and it carries no publish timestamp of
its own. So ``knowable_as_of`` is anchored to the week's FIRST kickoff
(``base.week_first_gameday_map``) — leakage-safe (never earlier than the week's
own games) and requiring schedules to be ingested first. A row whose
(season, week) has no resolvable gameday is dropped (counted), not stored with a
NULL ``knowable_as_of``.

THE KEY IS WIDER THAN IT WAS, AND THAT IS THE POINT
---------------------------------------------------
The item-1.4 primary key omitted ``game_type`` and ``depth_team``, so
``INSERT OR REPLACE`` silently collapsed **835 / 947 / 899 / 933 rows per season**
(2021-2024) — of which ~700 a season differed ONLY in ``depth_team``, i.e. the
depth ORDER, i.e. the one column this table exists for — while the ingester
returned the full row count and ``note_drops`` reported 0. Migration 007 widens
the stored key; ``_KEY_COLS`` below matches it, and ``base.upsert`` is now given
the full PK so the returned count is distinct keys written rather than rows
offered. With the widened key the residual collapse is 145/171/182/207
byte-identical upstream duplicates per season and ZERO non-identical collisions
(verified on all four season files) — the ``duplicated`` channel, deliberately
kept off the drop ceiling.

NOT IN THE CADENCE REGISTRY. This regime is finished: 2024 was its last season,
so there is nothing to refresh daily. It is pulled once per season by the
historical backfill, through ``run_ingest`` so it still gets the run log, the
rollback fence and the drop ceiling. Consequence to know: ``ziggurat ingest
status`` will not list it — ``ingest coverage`` and the run log are where it is
visible.
"""

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

#: Last season served in the weekly regime. 2025+ is the dated panel — see
#: ``depth_charts.py``. Enforced, not documented: a 2025 pull through this module
#: would hand a panel frame to a weekly ingester.
WEEKLY_MAX_SEASON = 2024

# Depth-chart columns we persist (each maps 1:1 to the import_depth_charts frame;
# base stamps retrieved_as_of / knowable_as_of).
_COLUMNS = (
    "gsis_id", "season", "week", "game_type", "club_code", "position",
    "depth_position", "depth_team", "formation", "full_name",
)

#: Columns only the 2025+ PANEL frame carries. Their presence means the wrong
#: regime reached this ingester.
_PANEL_MARKER_COLUMNS = ("dt", "espn_id", "pos_rank", "pos_grp_id")

#: The natural key a snapshot resolves on = the stored PK minus ``retrieved_as_of``.
#: ``game_type`` and ``depth_team`` are in it because leaving them out silently
#: destroyed ~900 rows a season (see the module docstring). Every nullable member
#: is coalesced to '' at ingest so the key can actually match: SQLite treats PK
#: NULLs as DISTINCT (so they never dedupe) and ``select_as_of``'s ``t2.k = t.k``
#: self-join is never satisfied by NULL (so the row is invisible to every read).
_KEY_COLS = ("season", "week", "game_type", "club_code", "formation",
             "position", "depth_position", "depth_team", "gsis_id")

#: The stored PRIMARY KEY. Derived from _KEY_COLS so the two cannot drift.
_PK_COLS = _KEY_COLS + ("retrieved_as_of",)

#: The nullable key members coalesced to '' — "not present in the depth-chart
#: source". ``club_code``, ``formation``, ``position``, ``season`` and ``week``
#: are NOT NULL in the table and a missing one is a broken row, not a blank.
_COALESCED_KEY_COLS = ("gsis_id", "game_type", "depth_position", "depth_team")


class PanelDepthChartFrame(ValueError):
    """A 2025+ dated-panel frame was handed to the weekly ingester."""


def _season_week(src):
    """(season, week) as plain ints for the week_first_gameday_map lookup, or
    None if either is missing/NaN (no gameday can be resolved -> drop the row)."""
    try:
        return (int(src.get("season")), int(src.get("week")))
    except (TypeError, ValueError):
        return None


def _require_weekly_frame(df) -> None:
    panel = [c for c in _PANEL_MARKER_COLUMNS if c in set(df.columns)]
    if panel:
        raise PanelDepthChartFrame(
            f"this is the 2025+ DATED PANEL depth-chart frame (carries {panel}) — "
            "it belongs to ziggurat.data.nfl.depth_charts and the "
            "depth_chart_slots / depth_chart_panels tables. It has no week and no "
            "season column, so every row would be dropped as unstampable."
        )
    base.require_columns(df, _COLUMNS, source="depth_charts_weekly")


def ingest_depth_charts_weekly(conn, df, *, retrieved_as_of: str) -> int:
    """Clean + upsert weekly depth charts, stamping ``knowable_as_of`` = the
    week's first gameday (schedules MUST already be ingested so
    ``week_first_gameday_map`` resolves). Rows whose (season, week) has no
    schedule -> no gameday are dropped rather than stored with a NULL
    ``knowable_as_of``. Returns DISTINCT KEYS written.

    Refuses a frame containing any season past ``WEEKLY_MAX_SEASON``: upstream
    changed regime, so a 2025 row reaching here means the caller asked the wrong
    module and the row would be stored under a schema that cannot express it.
    """
    _require_weekly_frame(df)
    seasons = {int(s) for s in df["season"].dropna().unique()}
    late = sorted(s for s in seasons if s > WEEKLY_MAX_SEASON)
    if late:
        raise PanelDepthChartFrame(
            f"seasons {late} are past the weekly regime (last weekly season is "
            f"{WEEKLY_MAX_SEASON}); 2025+ depth charts are the dated panel served "
            "by ziggurat.data.nfl.depth_charts"
        )

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
    base.note_drops("depth_charts_weekly", len(rows) - len(resolved), len(rows))
    for row in resolved:
        for column in _COALESCED_KEY_COLS:
            if row[column] is None:
                row[column] = ""
    return base.upsert(conn, "depth_charts_weekly", resolved, key_cols=_PK_COLS)


def pull_depth_charts_weekly(conn, years, *, retrieved_as_of: str) -> int:
    """Pull real weekly depth charts and store them. The
    ``nfl.import_depth_charts`` call is the seam cached-fixture tests patch."""
    seasons = [int(y) for y in years]
    late = sorted(s for s in seasons if s > WEEKLY_MAX_SEASON)
    if late:
        raise PanelDepthChartFrame(
            f"seasons {late} are past the weekly regime (last weekly season is "
            f"{WEEKLY_MAX_SEASON}); use ziggurat.data.nfl.depth_charts"
        )
    df = nfl.import_depth_charts(seasons)
    return ingest_depth_charts_weekly(conn, df, retrieved_as_of=retrieved_as_of)


def get_depth_chart_week(
    conn, *, as_of, season=None, week=None, team=None,
    view: base.AsOfView = "historical",
):
    """Weekly depth-chart rows knowable on or before ``as_of`` (keyword-only; no
    implicit now). ``team`` filters ``club_code``. Returns the latest
    ``retrieved_as_of`` snapshot per natural key among rows both knowable and
    retrieved by ``as_of``.

    2021-2024 only. For 2025+ use ``depth_charts.get_depth_chart``, which reads a
    different table with a different (dated, not weekly) grain.
    """
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
        conn, "depth_charts_weekly", as_of=as_of, key_cols=list(_KEY_COLS),
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
