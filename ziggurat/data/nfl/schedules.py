"""Schedule ingestion (import_schedules) — item 1.4.

Two roles: (1) an as-of accessor for game structure (matchup, venue, rest), and
(2) the canonical (season, week, team) -> gameday source that stamps
``knowable_as_of`` on every post-game table. Odds and actual weather are
deliberately excluded here (odds + weather are item 1.5; storing post-game
actuals would leak). Structural facts are knowable at preseason release.
"""

import nfl_data_py as nfl

from ziggurat.data.nfl import base

# Schedule columns we persist (each maps 1:1 to the import_schedules frame).
_COLUMNS = (
    "game_id", "season", "week", "game_type", "gameday", "weekday", "gametime",
    "away_team", "home_team", "location", "roof", "surface", "stadium_id",
    "stadium", "away_rest", "home_rest", "div_game",
)


def _preseason_anchor(season) -> str:
    """Structural schedule facts are knowable at release; anchor to Aug 1 of the
    season (after the spring release, before Week 1)."""
    return f"{int(season)}-08-01"


def _knowable(src) -> str | None:
    """Regular-season matchups are knowable at the preseason release; the PLAYOFF
    bracket (which teams play whom, encoded in home/away) is NOT known until the
    regular season ends, so a preseason anchor there would leak the bracket into
    preseason/in-season reads. Playoff (non-REG) rows are knowable no later than
    their own kickoff -> stamp them with their gameday."""
    if src.get("game_type") == "REG":
        return _preseason_anchor(src["season"])
    return base.iso_date(src.get("gameday"))


def ingest_schedules(conn, df, *, retrieved_as_of: str) -> int:
    rows = base.frame_to_rows(
        df,
        {c: c for c in _COLUMNS},
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=_knowable,
    )
    kept = [r for r in rows if r["knowable_as_of"] is not None]
    base.note_drops("schedules", len(rows) - len(kept), len(rows), why="no game_type/gameday")
    return base.upsert(conn, "schedules", kept)


def pull_schedules(conn, years, *, retrieved_as_of: str) -> int:
    """Pull real schedules and store them. The ``nfl.import_schedules`` call is
    the seam cached-fixture tests patch."""
    df = nfl.import_schedules(list(years))
    return ingest_schedules(conn, df, retrieved_as_of=retrieved_as_of)


def get_schedule(conn, *, as_of, season=None, week=None):
    """Games knowable on or before ``as_of`` (keyword-only; no implicit now)."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    return base.select_as_of(
        conn, "schedules", as_of=as_of, key_cols=["game_id"],
        extra_where=" AND ".join(clauses), params=params,
    )
