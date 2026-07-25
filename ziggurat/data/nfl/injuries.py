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

import logging

import pandas as pd

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

logger = logging.getLogger("ziggurat.data.nfl")

# Injury columns we persist (each maps 1:1 to the import_injuries frame).
_COLUMNS = (
    "gsis_id", "season", "week", "team", "position", "full_name",
    "report_status", "report_primary_injury", "report_secondary_injury",
    "practice_status", "practice_primary_injury", "practice_secondary_injury",
    "date_modified",
)

# Columns whose ABSENCE is a known upstream regime change rather than drift, so
# require_columns must not fail on them (item 3.1b, verified 2026-07-24):
# nflverse dropped ``date_modified`` from the 2025+ injury release — 2024 has it,
# 2025 does not. Losing it costs the report's own publish timestamp, so every
# 2025+ row falls back to the team-gameday anchor below. That is still
# leakage-safe (a report is final no later than kickoff) but MUCH coarser: a
# Wednesday practice report becomes knowable only on Sunday. Consumers that need
# mid-week injury news must use a live feed (ESPN league state carries
# injury_status 4x/day via item 3.1), not this table.
_OPTIONAL_COLUMNS = ("date_modified",)
_REQUIRED_COLUMNS = tuple(c for c in _COLUMNS if c not in _OPTIONAL_COLUMNS)

# The stored PRIMARY KEY — one row per player-week per pull. Passed to
# ``base.upsert`` so its return value counts DISTINCT keys, not rows offered.
_PK_COLS = ("gsis_id", "season", "week", "retrieved_as_of")

# --- game-status severity ----------------------------------------------------
# The stored PK is (gsis_id, season, week, retrieved_as_of): ONE row per player
# per week. The source is finer than that — it carries a row per report REVISION
# and, for a player who changes clubs mid-week, a row per club. So a collision is
# structural, and something has to decide which fact survives.
#
# Until item 3.2c that decision was "whichever row pandas iterated last", via
# INSERT OR REPLACE. Measured live 2026-07-25 on the real 2024 file:
#
#   Tyler Conklin  NYJ wk15  Out          (date_modified 2024-12-15 13:57:00)
#   Tyler Conklin  NYJ wk15  Questionable (date_modified 2024-12-14 20:55:19)
#     -> the table stored **Questionable**, with the EARLIER knowable_as_of.
#   Cade Stover    HOU wk15  Out / Questionable — same shape, same wrong answer.
#
# That is the single fact standing rule 6 leans on ("never recommend starting a
# player ruled OUT") being silently downgraded to a startable one. Volume is
# tiny (4 rows in 2024, 2 in 2022, 0 in 2021/2023/2025); the class is the worst
# one this repo has.
#
# ORDERING RULE (one sentence): among the rows for a (gsis_id, season, week),
# keep the newest ``date_modified``; a row with NO ``date_modified`` is always a
# candidate (we cannot prove it stale) and among the surviving candidates the
# MOST SEVERE ``report_status`` wins, with frame order breaking an exact tie.
# The "always a candidate" clause is what keeps the 2025+ regime — where
# nflverse ships no ``date_modified`` at all — resolving in the Rule-6-safe
# direction instead of by iteration luck.
#
# Values below are the measured domain of ``report_status`` across 2021-2025
# (live, 2026-07-25): Out / Doubtful / Questionable, plus 'Note' in 2024 only.
# 'Note' is NOT a game-status designation — all 6 of its 2024 rows are roster
# notes whose text says so verbatim ("...Fully expected to play. No game
# status.", "...has cleared concussion protocol and does not have a game
# status."), so it ranks with "no designation filed".
_STATUS_SEVERITY: dict[str, int] = {
    "out": 40,
    "doubtful": 30,
    "questionable": 20,
    "probable": 10,   # retired by the NFL in 2016; still present in older archives
    "note": 0,        # a roster note, explicitly "no game status" (measured 2024)
    "": 0,            # no designation filed
}

# An UNRECOGNIZED status must not sort as least-severe — that is exactly the
# shape of the defect above, and a new upstream designation is far likelier to
# mean "less available" (IR / PUP / suspended) than "more". It also must not
# outrank ``Out``, the one designation rule 6 names by name: demoting a known
# OUT in favour of a string we have never seen would be the same bug with the
# sign flipped. So: above every recognized non-Out value, below Out. Logged, so
# an upstream vocabulary change surfaces instead of quietly changing outcomes.
_UNKNOWN_SEVERITY = 35

_warned_statuses: set[str] = set()


def _severity(report_status) -> int:
    """Rank a ``report_status`` for the dedupe tiebreak (higher = keep)."""
    key = "" if report_status is None else str(report_status).strip().lower()
    if key in _STATUS_SEVERITY:
        return _STATUS_SEVERITY[key]
    if key not in _warned_statuses:
        _warned_statuses.add(key)
        logger.warning(
            "injuries: unrecognized report_status %r — ranked just below 'Out' for "
            "dedupe (never least-severe). Add it to _STATUS_SEVERITY once its "
            "meaning is confirmed.",
            report_status,
        )
    return _UNKNOWN_SEVERITY


def _recency(value):
    """Full-precision UTC sort key for ``date_modified`` (None when unusable).

    Deliberately compared at TIMESTAMP precision, not at the day granularity the
    column is finally STORED at. The stored knowledge time is a day either way
    (``iso_date``), so nothing about leakage changes — but the choice of which
    revision survives does: a same-day pair (Cade Stover, 2024 wk15, 03:34
    Questionable then 14:17 Out) is genuinely ordered, and truncating first would
    have thrown that ordering away and fallen through to the severity ladder.
    That ladder is the right answer for a TIE; it is a blunt one for a real
    de-escalation ("Out on Friday morning, upgraded to Questionable on Friday
    afternoon"), which no season 2021-2025 contains but nothing forbids.

    Defensive because a comparison failure here would fail the whole pull: a
    naive/aware mix or an unparseable cell degrades to "undated", which the rule
    above already handles safely, rather than raising.
    """
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    except (TypeError, ValueError):
        return None


def _dedupe_player_weeks(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapse rows to one per (gsis_id, season, week). Returns (kept, dropped).

    Applies the ordering rule documented above. Input order is the frame order,
    which is the last tiebreak. ``rows`` must still carry the RAW
    ``date_modified`` (see ``_recency``); ``ingest_injuries`` truncates it to an
    ISO date only after this has run.
    """
    groups: dict[tuple, list[tuple[int, dict]]] = {}
    for i, row in enumerate(rows):
        groups.setdefault((row["gsis_id"], row["season"], row["week"]), []).append((i, row))

    kept: list[tuple[int, dict]] = []
    dropped = 0
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        at = {i: _recency(r["date_modified"]) for i, r in group}
        stamps = [t for t in at.values() if t is not None]
        if stamps:
            newest = max(stamps)
            # A dateless row stays in contention: absence of a timestamp is not
            # evidence of staleness, and treating it as oldest is how an
            # undated OUT would lose to a dated QUESTIONABLE.
            candidates = [(i, r) for i, r in group if at[i] is None or at[i] == newest]
        else:
            candidates = list(group)
        winner = max(candidates, key=lambda pair: (_severity(pair[1]["report_status"]), -pair[0]))
        kept.append(winner)
        dropped += len(group) - 1

    kept.sort(key=lambda pair: pair[0])  # restore frame order for a stable upsert
    return [row for _, row in kept], dropped


def ingest_injuries(conn, df, *, retrieved_as_of: str) -> int:
    """Stamp knowable_as_of from each report's own ``date_modified`` (fallback:
    the team's own gameday), normalize date_modified to its ISO date, dedupe to
    one row per (gsis_id, season, week), and upsert. Rows whose knowledge time
    can't be resolved are dropped, never inserted with a NULL ``knowable_as_of``.

    The dedupe is the rule-6 guard: see ``_dedupe_player_weeks``. Without it the
    table stored whichever revision the frame happened to list last, which was
    measured storing ``Questionable`` over a same-week ``Out``."""
    base.require_columns(df, _REQUIRED_COLUMNS, source="injuries")
    offered = len(df)
    df = df.dropna(subset=["gsis_id"])
    # gsis_id is the NOT NULL primary key, so a null-key row cannot be stored.
    # It was dropped here SILENTLY before item 3.2c. Measured 0 such rows in every
    # season 2021-2025, so counting them changes nothing today — which is the
    # point: an unreported drop path only becomes visible after it starts firing.
    null_keys = offered - len(df)
    if "date_modified" not in df.columns:
        # 2025+ release: no publish timestamp at all. Materialize the column as
        # NULL so the mapping stays uniform and EVERY row takes the team-gameday
        # fallback below, rather than the ingester exploding on a schema the
        # source now legitimately serves.
        df = df.assign(date_modified=None)
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

    stampable, unstampable = [], 0
    for row in rows:
        if row["knowable_as_of"] is None:
            unstampable += 1  # neither date_modified nor a team gameday -> skip, don't NULL
            continue
        stampable.append(row)

    # Dedupe BEFORE truncating date_modified: the winner is chosen at full
    # timestamp precision (see ``_recency``), the column is stored day-granular.
    kept, superseded = _dedupe_player_weeks(stampable)
    for row in kept:
        # Store date_modified day-granular (matches knowable_as_of; Timestamp isn't bindable).
        row["date_modified"] = base.iso_date(row["date_modified"])

    # ONE note_drops call over ONE denominator. Two calls with different totals
    # is finding F-H (fixed in weekly_stats.py in the same change): the tally
    # sums ``total`` across calls, so a second call inflates the denominator past
    # the number of rows that ever existed.
    #
    # ``superseded`` rows go in the plain (ceiling-counting) channel, NOT
    # ``by_design=True``. They are real information loss — a second, differing
    # fact about the same key that the table's grain cannot hold — and if the
    # population ever explodes (an upstream regrain, a botched retrieval stamp)
    # that must alarm rather than be filed as correct-by-design filtering. At the
    # measured volume (4 rows in the whole 2024 season, out of 6,215) it is
    # nowhere near refresh's 20% ceiling.
    why = []
    if null_keys:
        why.append(f"{null_keys} with a null gsis_id")
    if unstampable:
        why.append(f"{unstampable} with no date_modified and no team gameday")
    if superseded:
        why.append(
            f"{superseded} superseded by a newer/more-severe report for the same "
            "player-week (revision or mid-week club change)"
        )
    base.note_drops(
        "injuries", null_keys + unstampable + superseded, offered,
        why="; ".join(why) or "unresolved knowledge time",
    )
    # ``key_cols`` is the honest-count guard (F-G), and here it is also the PROOF
    # that the dedupe above is complete: if any (gsis_id, season, week) still
    # collided, ``note_collapsed`` would say so at WARNING and the returned count
    # would fall below len(kept). Measured 0 collapses on live 2021-2025.
    return base.upsert(conn, "injuries", kept, key_cols=_PK_COLS)


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
