"""Vegas line ingestion (import_game_odds) — item 1.5; midweek stamp item 3.14 step 3.

The spread / total / moneyline per game, kept OUT of the schedules table so a
pre-game read can never see a value stamped at the preseason anchor. Odds ride
the ``load_schedules`` frame but come through their own ``import_game_odds``
seam (distinct from ``import_schedules``) so this source has an independent test
point.

WHAT A ROW IS. nflverse rewrites the season file as lines move, so a pull on day
R carries the line AS OF R for every game not yet played, and the CLOSING line
once the game has been played. ``_PK_COLS`` holds ``retrieved_as_of``, so every
pull is its own row and nothing is overwritten in this table.

``knowable_as_of = min(gameday, retrieved_as_of)`` (item 3.14 step 3,
2026-09-30). Until then it was the gameday alone, which was leakage-safe but
BLIND: a Tuesday or Wednesday read (the waiver and streaming days) could never
see a line that had been sitting in the table since Monday, so ``ziggurat
stream`` printed "Vegas lines not posted" all week over stored lines. The
per-row stamp is leakage-safe for the reason the plan recorded (item 4.6,
retired done-when (b)):
- a row pulled on R is only visible at ``as_of >= R``;
- the ``historical`` view also gates ``retrieved_as_of``;
- a row pulled on or after gameday keeps the gameday stamp, so its closing line
  is still invisible before kickoff day under ``latest_truth`` too.
A bulk backfill pulled after the season (every 2021–2025 row) is therefore
stamped exactly as before.

A PLAYED game (the row carries a result) is always stamped at its gameday, so a
back-stamped ``--allow-backfill`` run cannot publish a closing line early.

The midweek half is lost if not pulled: the closing line is re-pullable
forever, but the line a pull saw on a given Tuesday is replaced by the next
rewrite (measured: 18 forward lines vanished between the 09-09 and 09-13 pulls).
The registry does NOT mark this source ``perishable``. That flag is per SOURCE,
and ``ingest status`` would then call a re-pullable past season "UNOBTAINABLE"
and a played season's gap "gone" — both false (item 3.14 step 3 review).

Migration 003's column comment ``-- = gameday`` is stale since this change.
Applied migrations are never edited, so the correction lives here.

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


# The stored PRIMARY KEY, passed to ``base.upsert`` so its return value is the
# number of DISTINCT keys written rather than rows offered (item 3.2c, F-G).
# SWEPT 2026-07-25: the same instrumentation was applied to 6 of 14 call sites
# in 3.2c and skipped here, and the one skipped site that DID collide
# (adp_rankings) lost a real market fact a day for two days, silently, with an
# inflated count in the run log.
# One row per game_id; rides the same whole-season frame shape as schedules.
_PK_COLS = ('game_id', 'retrieved_as_of')


def _played(src) -> bool:
    """Does the source row carry a RESULT? (The ``load_schedules`` frame does:
    ``result`` / ``home_score`` / ``away_score``.) A frame without those columns
    reads as unplayed, and the pull date decides."""
    for column in ("result", "home_score", "away_score"):
        value = src.get(column)
        if value is not None and value == value:          # not None, not NaN
            return True
    return False


def ingest_game_odds(conn, df, *, retrieved_as_of: str) -> int:
    """Persist per-game lines, stamping ``knowable_as_of = min(gameday, retrieved_as_of)``.

    ``require_columns`` fails loudly on odds/identity schema drift. Rows with a
    null gameday (unstampable — typically a not-yet-scheduled future game) are
    dropped via ``note_drops``; rows whose *odds* are null are KEPT, because an
    unplayed in-season game legitimately has no line yet and the absence is a
    fact the consumer should see rather than a reason to drop the game.
    """
    base.require_columns(df, _REQUIRED, source="game_odds")

    pulled = base.iso_date(retrieved_as_of)

    def _knowable(src):
        gameday = base.iso_date(src.get("gameday"))
        if gameday is None:
            return None
        if pulled is None or _played(src):
            # A PLAYED game's row is a closing line, and a closing line is
            # knowable at kickoff day no matter what the pull date says. This
            # holds even under a back-stamped `--allow-backfill` run, whose
            # `retrieved_as_of` would otherwise publish the close early.
            return gameday
        return min(gameday, pulled)

    rows = base.frame_to_rows(
        df,
        _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=_knowable,
    )
    kept = [r for r in rows if r["knowable_as_of"] is not None]
    base.note_drops("game_odds", len(rows) - len(kept), len(rows), why="null gameday")
    return base.upsert(conn, "game_odds", kept, key_cols=_PK_COLS)


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
    """Lines knowable on or before ``as_of`` (keyword-only; no implicit now).

    WHAT YOU GET: per game, the NEWEST pull knowable at ``as_of`` — i.e. the line
    as of that pull (``retrieved_as_of`` is on the row), which is the closing
    line only if that pull came after the game. A Tuesday read sees Tuesday's
    pull (item 3.14 step 3). A game no pull on or before ``as_of`` carried a line
    for comes back with NULL odds or not at all.

    LEAKAGE: rows are stamped ``min(gameday, retrieved_as_of)`` (module
    docstring), so no read sees a line before the day it was pulled, and no read
    before kickoff day sees a pull made on or after it. Backtest/grading reads go
    through ``base.latest_truth(get_game_odds)``. For a 2021–2025 game that is the
    closing line, stamped at gameday, so a backtest on it is still an UPPER BOUND
    on what a Tuesday read could have seen (item 4.6).

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


def restamp_stored_forward_lines(conn, *, season: int) -> int:
    """Apply the item-3.14-step-3 stamp to rows stored BEFORE it existed.

    Rows pulled before their game were stamped at the gameday, i.e. invisible
    until kickoff day; the correct stamp is the pull day. ONE transaction,
    touches only ``knowable_as_of``, and only on rows where the pull day is
    EARLIER than the stored stamp. That is exactly the set the old rule
    over-delayed, and re-running it changes nothing (idempotent). In place for
    the same reason as ``fp_weekly.repair_stored_week_labels``: the stamp was a
    derivation, not an observation. Returns rows changed.
    """
    with conn:
        cursor = conn.execute(
            "UPDATE game_odds SET knowable_as_of = retrieved_as_of "
            "WHERE season = ? AND retrieved_as_of < knowable_as_of",
            (int(season),),
        )
    return cursor.rowcount
