"""Injury report ingestion (import_injuries) — item 1.4.

The weekly injury report (practice participation + game-status designation) is
the most time-sensitive fact in the roster loop: an OUT/DOUBTFUL tag is exactly
the sanity check standing rule 6 relies on. Its leakage crux is subtle — an
injury report is knowable not on gameday but the moment the report was filed, so
``knowable_as_of`` is the report's OWN ``date_modified`` timestamp (a mid-week
Wednesday/Thursday practice report is public days before Sunday). When a row
carries no ``date_modified`` we fall back to that team's OWN gameday
(``game_date_map[(season, week, team)]``) — the report is finalized no later than
the team's kickoff, so this is the tightest leakage-safe upper bound. (The week's
FIRST gameday would be wrong: it is the earliest game of the week and, for a team
that plays later, would expose the report before it was filed.) The schedules
table must be ingested first for that fallback to resolve.

Anatomy repeats the players/schedules exemplars: ``ingest_injuries`` (frame ->
cleaned rows -> upsert), ``pull_injuries`` (wraps the one ``nfl.import_injuries``
seam tests patch), and a keyword-only ``as_of`` accessor. Columns map 1:1 to the
import_injuries frame EXCEPT ``date_modified``, which is stored as its ISO date
(knowledge time is day-granular here, and a tz-aware Timestamp is not storable
as-is) — the same normalization that derives ``knowable_as_of``.
"""

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

# Injury columns we persist (each maps 1:1 to the import_injuries frame).
_COLUMNS = (
    "gsis_id", "season", "week", "team", "position", "full_name",
    "report_status", "report_primary_injury", "report_secondary_injury",
    "practice_status", "practice_primary_injury", "practice_secondary_injury",
    "date_modified",
)


def ingest_injuries(conn, df, *, retrieved_as_of: str) -> int:
    """Stamp knowable_as_of from each report's own ``date_modified`` (fallback:
    the week's first gameday), normalize date_modified to its ISO date, and
    upsert. Rows whose knowledge time can't be resolved are dropped, never
    inserted with a NULL ``knowable_as_of``."""
    base.require_columns(df, _COLUMNS, source="injuries")
    df = df.dropna(subset=["gsis_id"])
    # Fallback knowledge time when a row has no date_modified: the report's own
    # TEAM gameday (needs schedules). NOT the week's first gameday, which would
    # leak the report for teams that play later in the week.
    game_dates = base.game_date_map(conn)

    def _knowable(src) -> str | None:
        stamped = base.iso_date(src.get("date_modified"))
        if stamped is not None:
            return stamped
        try:
            key = (int(src.get("season")), int(src.get("week")), src.get("team"))
        except (TypeError, ValueError):
            return None
        return game_dates.get(key)

    rows = base.frame_to_rows(
        df,
        {c: c for c in _COLUMNS},
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=_knowable,
    )

    kept, dropped = [], 0
    for row in rows:
        if row["knowable_as_of"] is None:
            dropped += 1  # neither date_modified nor a team gameday -> skip, don't NULL
            continue
        # Store date_modified day-granular (matches knowable_as_of; Timestamp isn't bindable).
        row["date_modified"] = base.iso_date(row["date_modified"])
        kept.append(row)

    base.note_drops("injuries", dropped, len(rows), why="no date_modified and no team gameday")
    return base.upsert(conn, "injuries", kept)


def pull_injuries(conn, years, *, retrieved_as_of: str) -> int:
    """Pull real injury reports and store them. ``nfl.import_injuries`` is the
    seam cached-fixture tests patch."""
    df = nfl.import_injuries(list(years))
    return ingest_injuries(conn, df, retrieved_as_of=retrieved_as_of)


def get_injuries(
    conn, *, as_of, season=None, week=None, gsis_id=None,
    view: base.AsOfView = "historical",
):
    """Injury reports knowable on or before ``as_of`` (latest snapshot per
    gsis/season/week). Keyword-only ``as_of`` — no implicit now."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if gsis_id is not None:
        clauses.append("t.gsis_id = :gsis_id")
        params["gsis_id"] = gsis_id
    return base.select_as_of(
        conn, "injuries", as_of=as_of,
        key_cols=["gsis_id", "season", "week"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
