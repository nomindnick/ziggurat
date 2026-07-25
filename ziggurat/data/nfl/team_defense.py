"""D/ST team-defense stat grid ingestion (import_team_stats) — item 1.5.

One row per (season, week, team) = a defense's fantasy-scoring line, named with
the scoring.py canonical keys so ``dict(row)`` prices directly through
``score_dst``. The line is DERIVED, not copied: nflverse ``load_team_stats``
carries a team's *own* offensive/defensive counters, so a defense's yards- and
points-allowed come from the OPPONENT's row in the same game (an opponent
self-join keyed on ``(game_id, opponent_team)``) and the game's final scores come
from the schedules frame.

knowable_as_of is a post-game fact — a defense's week-N line is knowable on the
day that team played — so it is stamped with the team gameday from
``base.game_date_map`` (schedules must be ingested first).

Join guard (leakage/soundness): ``points_allowed`` and ``yards_allowed`` are
BRACKET inputs, and ``score_dst`` *skips* an absent bracket key rather than
scoring it 0. A NULL bracket input would therefore silently understate a
defense with no loud failure. So any row whose opponent self-join OR schedules
score lookup fails to resolve is DROPPED via ``note_drops`` — never NULL-inserted.

The exact ESPN points/yards-allowed charge semantics (does a return TD the
opponent scored against *our* offense count against our D/ST? ESPN: no) is
deferred to item 3.8; v1 charges the opponent's full final score. The audit
columns ``team_score``/``opp_score`` are retained so 3.8 can refine without
re-ingesting. See intel/research/ingestion-1.5-design.md §5.
"""

import pandas as pd

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

# team_stats columns the derivation reads (fail loudly if a release drops one).
_TEAM_STATS_COLUMNS = (
    "season", "week", "team", "season_type", "game_id", "opponent_team",
    # T's own defensive/ST counters:
    "def_sacks", "def_interceptions", "fumble_recovery_opp", "def_safeties",
    "def_tds", "fumble_recovery_tds", "special_teams_tds",
    # kicking-unit blocks (sit on the KICKING team's row = the opponent of the
    # defense credited with the block):
    "fg_blocked", "pat_blocked", "pt_blocked",
    # offensive yards (read off the OPPONENT row for yards_allowed):
    "passing_yards", "rushing_yards", "sack_yards_lost",
)

# schedules columns the score map reads.
_SCHEDULES_COLUMNS = (
    "game_id", "home_team", "away_team", "home_score", "away_score",
)


def _num(value) -> float:
    """Coerce a team_stats cell to float, treating None/NaN as 0.0 (safe for the
    additive derivations; a played game never carries NaN in these counters)."""
    value = base._clean(value)
    if value is None:
        return 0.0
    return float(value)


# The stored PRIMARY KEY, passed to ``base.upsert`` so its return value is the
# number of DISTINCT keys written rather than rows offered (item 3.2c, F-G).
# SWEPT 2026-07-25: the same instrumentation was applied to 6 of 14 call sites
# in 3.2c and skipped here, and the one skipped site that DID collide
# (adp_rankings) lost a real market fact a day for two days, silently, with an
# inflated count in the run log.
# A DERIVED grid (one row per team-week): a collision here would mean the
# schedules join produced two games for one team-week, which is the shape that
# would silently halve a D/ST season.
_PK_COLS = ('season', 'week', 'team', 'retrieved_as_of')


def ingest_team_defense(conn, team_stats_df, schedules_df, *, retrieved_as_of: str) -> int:
    """Derive + persist the per-defense fantasy line. Returns rows written.

    Requires schedules already ingested (``base.game_date_map`` resolves the
    team gameday). Rows whose opponent self-join, schedules score lookup, or
    gameday cannot resolve are dropped (counted via the length/return gap).
    """
    base.require_columns(team_stats_df, _TEAM_STATS_COLUMNS, source="team_defense")
    base.require_columns(schedules_df, _SCHEDULES_COLUMNS, source="team_defense/schedules")

    # (game_id, team) -> team_stats row, for the opponent self-join. Exactly one
    # row per pair (a team plays a game once); a duplicate is a source fault.
    opp_index: dict[tuple, pd.Series] = {}
    for _, r in team_stats_df.iterrows():
        key = (r["game_id"], r["team"])
        if key in opp_index:
            raise ValueError(f"team_defense: duplicate team_stats row for {key}")
        opp_index[key] = r

    # game_id -> schedules row (final scores + home/away identity).
    score_map: dict = {}
    for _, r in schedules_df.iterrows():
        score_map[r["game_id"]] = r

    gdm = base.game_date_map(conn)
    retrieved = base.iso_date(retrieved_as_of)

    rows: list[dict] = []
    total = len(team_stats_df)
    dropped = 0
    for _, t in team_stats_df.iterrows():
        game_id = t["game_id"]
        team = t["team"]
        opp = t["opponent_team"]
        season = int(t["season"])
        week = int(t["week"])

        opp_row = opp_index.get((game_id, opp))
        sched = score_map.get(game_id)
        knowable = gdm.get((season, week, team))
        if opp_row is None or sched is None or knowable is None:
            dropped += 1
            continue

        # Opponent's final score is points_allowed; identify which side opp is.
        if opp == sched["home_team"]:
            opp_score = base._clean(sched["home_score"])
            team_score = base._clean(sched["away_score"])
        elif opp == sched["away_team"]:
            opp_score = base._clean(sched["away_score"])
            team_score = base._clean(sched["home_score"])
        else:
            dropped += 1  # opponent not on this game's schedule row
            continue
        if opp_score is None:
            dropped += 1  # unplayed / missing final score -> no bracket input
            continue

        rows.append({
            "season": season,
            "week": week,
            "team": team,
            "season_type": base._clean(t["season_type"]),
            "opponent_team": opp,
            "game_id": game_id,
            # events (T's own row):
            "sacks": _num(t["def_sacks"]),
            "def_interceptions": _num(t["def_interceptions"]),
            "fumble_recoveries": _num(t["fumble_recovery_opp"]),  # NOT _own
            "safeties": _num(t["def_safeties"]),
            # kick/punt-return blocks live on the OPPONENT's kicking-unit row:
            "blocked_kicks": (
                _num(opp_row["fg_blocked"])
                + _num(opp_row["pat_blocked"])
                + _num(opp_row["pt_blocked"])
            ),
            # def_tds EXCLUDES fumble-return TDs; special_teams_tds = kick/punt
            # return TDs scored BY T. do NOT add pt_return_tds (allowed on the
            # punting unit) or the def_pr_td/def_fum_td components (double count).
            "def_tds": (
                _num(t["def_tds"])
                + _num(t["fumble_recovery_tds"])
                + _num(t["special_teams_tds"])
            ),
            "points_allowed": float(opp_score),
            # sack_yards_lost is already NEGATIVE -> net total yards:
            "yards_allowed": (
                _num(opp_row["passing_yards"])
                + _num(opp_row["rushing_yards"])
                + _num(opp_row["sack_yards_lost"])
            ),
            "team_score": team_score,
            "opp_score": opp_score,
            "retrieved_as_of": retrieved,
            "knowable_as_of": knowable,
        })

    base.note_drops("team_defense", dropped, total, why="unresolved opponent/score/gameday")
    return base.upsert(conn, "team_defense", rows, key_cols=_PK_COLS)


def pull_team_defense(conn, years, *, retrieved_as_of: str) -> int:
    """Pull team stats AND schedules for ``years`` and derive the D/ST grid. The
    ``nfl.import_team_stats`` / ``nfl.import_schedules`` calls are the seams
    cached-fixture tests patch (double-pull of schedules is acceptable)."""
    years = list(years)
    team_stats = nfl.import_team_stats(years)
    sched = nfl.import_schedules(years)
    return ingest_team_defense(conn, team_stats, sched, retrieved_as_of=retrieved_as_of)


def get_team_defense(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    team=None,
    view: base.AsOfView = "historical",
):
    """D/ST lines knowable on or before ``as_of`` (keyword-only; no implicit now).
    Latest snapshot per (season, week, team). Backtest/grading reads go through
    ``base.latest_truth(get_team_defense)``."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if team is not None:
        clauses.append("t.team = :team")
        params["team"] = team
    return base.select_as_of(
        conn, "team_defense", as_of=as_of,
        key_cols=["season", "week", "team"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
