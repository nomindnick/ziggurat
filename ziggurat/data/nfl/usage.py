"""Usage-trend accessor (item 1.4 done-when): week-over-week usage deltas.

"Usage deltas for all RBs as of 2023 week 6" — the query the plan names. Built
ON TOP of the as-of source accessors (get_weekly_stats / get_snap_counts), so
leakage safety is inherited, not re-implemented: a week-N delta only exists once
week N's games are knowable by ``as_of`` (you cannot difference a game that has
not been played). This is the raw-usage trend signal SPEC decision 6 calls the
edge — detecting role change before consensus moves.

Each player is differenced against their MOST RECENT PRIOR knowable week (their
last game before ``week``), not a hard-coded week-1 — so the bye / injury-return
/ promotion cohort (a player active this week but not last) is exactly the role
change we want to surface, carried with ``prior_week`` set and, when there is no
prior game, null deltas + ``prior_week=None`` (visible and flagged, never
silently dropped). Snap deltas are always present as keys, ``None`` when the
pfr->gsis crosswalk or a week's snap row is missing (so "unknown" is
distinguishable from a real 0.0 change).
"""

from ziggurat.data.nfl.snap_counts import get_snap_counts
from ziggurat.data.nfl.weekly_stats import get_weekly_stats

# Usage columns we difference week-over-week (from weekly_stats).
USAGE_METRICS = (
    "targets", "target_share", "air_yards_share", "wopr",
    "carries", "receptions", "rushing_yards", "receiving_yards",
)


def _f(value) -> float:
    return 0.0 if value is None else float(value)


def usage_deltas(conn, *, as_of, season: int, week: int, position: str | None = "RB") -> list[dict]:
    """Week-over-week usage deltas for `week` vs each player's most recent prior
    knowable week, as of `as_of`.

    Returns one dict per player with a `week` row (optionally filtered to
    `position`, default RB), carrying `prior_week` and d_<metric> for every
    USAGE_METRIC, plus d_offense_snaps / d_offense_pct (None when the snap
    crosswalk bridge doesn't resolve in both weeks). Deltas are None when the
    player has no prior knowable game. Empty when `week` is not yet knowable at
    `as_of` — the leakage property, inherited from the accessors.
    """
    # All knowable weeks this season (position-filtered), grouped per player.
    by_player: dict[str, dict[int, dict]] = {}
    for r in get_weekly_stats(conn, as_of=as_of, season=season, position=position):
        by_player.setdefault(r["player_id"], {})[r["week"]] = r

    snaps: dict[tuple[str, int], dict] = {}
    for s in get_snap_counts(conn, as_of=as_of, season=season):
        if s["gsis_id"] is not None:
            snaps[(s["gsis_id"], s["week"])] = s

    out: list[dict] = []
    for pid, weeks in by_player.items():
        cur = weeks.get(week)
        if cur is None:
            continue  # not active in the target week -> no delta to report
        prior_weeks = [w for w in weeks if w < week]
        prior_week = max(prior_weeks) if prior_weeks else None
        prev = weeks[prior_week] if prior_week is not None else None

        row = {
            "player_id": pid,
            "position": cur["position"],
            "team": cur["recent_team"],
            "season": season,
            "week": week,
            "prior_week": prior_week,
        }
        for m in USAGE_METRICS:
            row[f"d_{m}"] = None if prev is None else _f(cur[m]) - _f(prev[m])

        sc = snaps.get((pid, week))
        sp = snaps.get((pid, prior_week)) if prior_week is not None else None
        both = sc is not None and sp is not None
        row["d_offense_snaps"] = _f(sc["offense_snaps"]) - _f(sp["offense_snaps"]) if both else None
        row["d_offense_pct"] = _f(sc["offense_pct"]) - _f(sp["offense_pct"]) if both else None
        out.append(row)
    return out
