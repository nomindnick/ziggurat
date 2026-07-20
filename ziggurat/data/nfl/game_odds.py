"""Vegas closing-line ingestion (import_game_odds) — item 1.5.

The closing spread / total / moneyline per game, kept OUT of the schedules table
so a pre-game read can never see a value stamped at the preseason anchor. Odds
ride the ``load_schedules`` frame but come through their own ``import_game_odds``
seam (distinct from ``import_schedules``) so this source has an independent test
point.

``knowable_as_of`` is stamped with the game's own gameday (read directly off the
same frame — no ``game_date_map`` join needed). This is conservatively
leakage-safe: a gameday stamp can only ever be *too late*, never too early. The
practical consequence is documented on ``get_game_odds`` — a closing line
stamped at gameday is (correctly) invisible to a same-day pre-kickoff caller
reading at ``as_of = D-1``.

Null odds are KEPT (an unplayed in-season game legitimately carries no line
yet); only a null *gameday* — which leaves the row unstampable — is dropped.
``spread_line`` is stored home-oriented verbatim: positive = home favored.
No scoring.py contact; odds are decision inputs (item 3.5), not a stat line.
"""

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

# The odds values themselves — required to be present as columns (fail loud on
# upstream schema drift), though individual cells may be NULL for unplayed games.
_ODDS_COLUMNS = (
    "spread_line",       # home perspective: positive = home favored (stored verbatim)
    "total_line",        # game over/under total
    "home_moneyline",
    "away_moneyline",
    "home_spread_odds",
    "away_spread_odds",
    "over_odds",
    "under_odds",
)

# Identity columns we also read: game_id/season/week/home/away are persisted;
# gameday is read only to stamp knowable_as_of (it lives in the schedules table).
_ID_COLUMNS = ("game_id", "season", "week", "home_team", "away_team")
_REQUIRED = _ID_COLUMNS + ("gameday",) + _ODDS_COLUMNS

# db_column -> source_column (1:1). gameday is intentionally NOT stored here.
_COLMAP = {c: c for c in _ID_COLUMNS + _ODDS_COLUMNS}


def ingest_game_odds(conn, df, *, retrieved_as_of: str) -> int:
    """Persist per-game closing lines, stamping knowable_as_of with the gameday.

    ``require_columns`` fails loudly on odds/identity schema drift. Rows with a
    null gameday (unstampable — typically a not-yet-scheduled future game) are
    dropped via ``note_drops``; rows whose *odds* are null are KEPT, because an
    unplayed in-season game legitimately has no line yet and the absence is a
    fact the consumer should see rather than a reason to drop the game.
    """
    base.require_columns(df, _REQUIRED, source="game_odds")

    def _knowable(src):
        return base.iso_date(src.get("gameday"))

    rows = base.frame_to_rows(
        df,
        _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=_knowable,
    )
    kept = [r for r in rows if r["knowable_as_of"] is not None]
    base.note_drops("game_odds", len(rows) - len(kept), len(rows), why="null gameday")
    return base.upsert(conn, "game_odds", kept)


def pull_game_odds(conn, years, *, retrieved_as_of: str) -> int:
    """Pull real odds and store them. The ``nfl.import_game_odds`` call is the
    seam cached-fixture tests patch (distinct from ``import_schedules``)."""
    df = nfl.import_game_odds(list(years))
    return ingest_game_odds(conn, df, retrieved_as_of=retrieved_as_of)


def get_game_odds(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    game_id=None,
    view: base.AsOfView = "historical",
):
    """Closing lines knowable on or before ``as_of`` (keyword-only; no implicit now).

    LEAKAGE / GRANULARITY: every row is stamped ``knowable_as_of = gameday``, so
    this source cannot supply a *pre-kickoff* line to a same-day live caller — a
    closing line stamped at gameday D is (correctly) invisible at ``as_of = D-1``.
    That is by design: nflverse carries only the single closing value, not an
    intraday line history, so any earlier read would be a leak. Backtest/grading
    reads go through ``base.latest_truth(get_game_odds)``.

    ``spread_line`` is home-oriented (positive = home favored), stored verbatim.
    """
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if game_id is not None:
        clauses.append("t.game_id = :game_id")
        params["game_id"] = game_id
    return base.select_as_of(
        conn, "game_odds", as_of=as_of, key_cols=["game_id"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
