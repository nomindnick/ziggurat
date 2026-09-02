"""Weekly player box score + usage ingestion (import_weekly_data) — item 1.4.

The per-week statistical spine: one row per (player, season, week) carrying the
box-score and usage columns scoring.py consumes (nflverse naming, so a row
scores directly — for QB/RB/WR/TE through ``score_offense`` and, since
migration 013, for K through ``kicker_scoring_inputs`` + ``score_kicker``).
Keyed on gsis_id (the frame's ``player_id``), it joins to the crosswalk, snap
counts, and NGS.

knowable_as_of is a post-game fact: a player's week-N line becomes knowable on
the day their team played, so it is stamped with the team gameday from
``base.game_date_map`` (schedules must be ingested first). A row whose
(season, week, recent_team) can't be resolved to a gameday is dropped, never
inserted with a NULL knowledge time — dropping is the leakage-safe default.

KICKING (item 4.1 §7.1, migration 013). Item 1.4's column list carried no
kicking stat, so every stored kicker row was all-zero for every stat the house
pays him for and re-scoring the table graded EVERY kicker at 0.000 with nothing
raised (the draft backtest's finding, 2026-08-30). The eight nflverse distance
buckets now persist under upstream's own names. Rows retrieved before the
migration read NULL in all of them, and NULL means NOT CAPTURED — never zero: a
captured kicker row is never NULL in any of the eight (measured 569/569 on the
live 2023 frame), so ``kicker_scoring_inputs`` returns ``None`` for an
uncaptured row rather than a stat line that would score 0.
"""

import math

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

#: The nflverse kicking columns persisted since migration 013, under upstream's
#: names (``fg_made_60_`` has the trailing underscore upstream). Named once so
#: the ingester, the fixture builder and the tests agree on the set.
KICKING_COLUMNS: tuple[str, ...] = (
    "fg_made_0_19", "fg_made_20_29", "fg_made_30_39", "fg_made_40_49",
    "fg_made_50_59", "fg_made_60_", "pat_made", "fg_missed",
)

#: nflverse made-FG bucket columns -> ``core/scoring.py``'s bucket KEYS. The
#: house pays by distance and nflverse splits 0-39 into three sub-buckets that
#: all price identically, so they are summed onto the one key scoring.py has.
#: A key mapping, not a scoring number (Rule 2): the points per bucket live only
#: in ``ScoringRules``. Ported from ``backtest/draft_backtest.py``'s
#: ``_NFLVERSE_FG_BUCKETS`` so the two folds cannot disagree.
_FG_BUCKET_FOLD: dict[str, tuple[str, ...]] = {
    "fg_made_0_39": ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39"),
    "fg_made_40_49": ("fg_made_40_49",),
    "fg_made_50_59": ("fg_made_50_59",),
    "fg_made_60": ("fg_made_60_",),
}

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
    # kicking (item 4.1 §7.1; migration 013) — nflverse distance buckets
    *KICKING_COLUMNS,
)

# The stored PRIMARY KEY, passed to ``base.upsert`` so its return value is the
# number of DISTINCT keys written rather than rows offered (item 3.2c, F-G).
# Measured 0 same-batch collisions on live 2021/2024/2025, so this changes no
# count today — it makes a future upstream regrain visible instead of silent.
_PK_COLS = ("player_id", "season", "week", "retrieved_as_of")


def kicker_scoring_inputs(row) -> dict[str, int] | None:
    """Fold a stored weekly_stats row into the stat line ``scoring.score_kicker``
    consumes, or ``None`` when the kicking columns were NOT CAPTURED.

    ``row`` is anything indexable by column name (a ``sqlite3.Row`` from
    ``get_weekly_stats``, or a dict). Returns ``{"fg_made_0_39", "fg_made_40_49",
    "fg_made_50_59", "fg_made_60", "pat_made", "fg_missed"}`` — the caller passes
    it straight to ``score_kicker``; no scoring number is applied here (Rule 2).

    ``None`` (not a zeroed line) whenever ANY of the eight ``KICKING_COLUMNS``
    is NULL — or NaN, pandas' spelling of NULL when ``row`` came from
    ``DataFrame.to_dict("records")``. NULL means NOT CAPTURED: either the row
    sits in a partition retrieved before migration 013, or upstream served no
    value for the cell (``ingest_weekly_stats`` logs that as a WARNING and counts
    it under ``incomplete``, and a re-pull will NOT fill it). Pricing such a row
    at 0 is exactly the silent-zero kicker grade the migration exists to end. A
    consumer that wants a number for it must say "not captured"; for a pre-013
    partition a re-pull (``ziggurat ingest backfill --source weekly_stats
    --force``) is what supplies one.

    Position is the caller's concern — a captured non-K row folds to a
    legitimate zero line (that RB really made 0 field goals); a consumer merging
    kicker points into an outcomes map must filter ``position == K`` first (see
    ``draft_backtest._kicker_rows_only``), or the merge overwrites skill players'
    offensive points with 0.0. A guard here would refuse captured lines and hide
    that consumer bug behind a ``None``.

    Blocked field goals are NOT charged: nflverse counts ``fg_blocked``
    separately from ``fg_missed``, the house charges per missed FG (ESPN "FGM"),
    and ESPN's blocked-kick convention is not in the settings fixture — item
    3.8's post-Week-1 box-score validation confirms it. Missed PATs are not
    persisted because the house has no missed-PAT stat (``score_kicker``
    ignores ``pat_missed``).
    """
    values = {}
    for col in KICKING_COLUMNS:
        try:
            value = row[col]
        except (KeyError, IndexError):
            return None
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        values[col] = int(value)
    stats = {key: sum(values[c] for c in cols) for key, cols in _FG_BUCKET_FOLD.items()}
    stats["pat_made"] = values["pat_made"]
    stats["fg_missed"] = values["fg_missed"]
    return stats


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

    A kept row whose kicking cell is NaN upstream (stored NULL) is a THIRD
    thing, and goes through ``note_incomplete``, not ``note_drops`` (item 4.1
    audit, KICK-3): the row is written and its skill stats are fine, so it was
    never lost — routing it as a drop would feed refresh's drop ceiling and make
    ``seen = written + lost`` overcount. It still must not be silent: the NULL
    reads as NOT CAPTURED to ``kicker_scoring_inputs``, and a re-pull reproduces
    it, so the WARNING here is the only place the cause is ever named. Counted
    on every position (measured 0 NULL kicking cells across all 94,738 live rows,
    so any NaN is drift); narrow to K if upstream ever ships NaN for non-kickers
    routinely.
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
    _note_uncaptured_kicking(resolved)

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


def _note_uncaptured_kicking(rows) -> int:
    """Count KEPT rows with a NULL in any kicking column and record them as
    incomplete (never as drops — see ``ingest_weekly_stats``). Returns the count.
    ``base.frame_to_rows`` has already mapped NaN -> None, so None is the test."""
    incomplete = 0
    null_cols: set[str] = set()
    for row in rows:
        missing = [c for c in KICKING_COLUMNS if row[c] is None]
        if missing:
            incomplete += 1
            null_cols.update(missing)
    if incomplete:
        base.note_incomplete(
            "weekly_stats", incomplete, len(rows),
            why=(f"NaN in {', '.join(sorted(null_cols))} — stored NULL, reads as NOT "
                 "CAPTURED to kicker_scoring_inputs; a re-pull will not fill it"),
        )
    return incomplete


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
    through_week: int | None = None,
    view: base.AsOfView = "historical",
):
    """Weekly stat rows knowable on or before ``as_of`` (keyword-only; no implicit
    now). Latest snapshot per (player_id, season, week). ``through_week`` bounds
    a season-to-date read to ``week <= through_week`` (item 4.1 audit, COST-1)."""
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
    if through_week is not None:
        clauses.append("t.week <= :through_week")
        params["through_week"] = through_week
    return base.select_as_of(
        conn, "weekly_stats", as_of=as_of,
        key_cols=["player_id", "season", "week"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
