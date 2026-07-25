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

from ziggurat.data.nfl import base
from ziggurat.data.nfl.snap_counts import get_snap_counts
from ziggurat.data.nfl.weekly_stats import get_weekly_stats

# Usage columns we difference week-over-week (from weekly_stats).
USAGE_METRICS = (
    "targets", "target_share", "air_yards_share", "wopr",
    "carries", "receptions", "rushing_yards", "receiving_yards",
)


def _f(value) -> float:
    return 0.0 if value is None else float(value)


def _combine_clubs(rows: list) -> dict:
    """Fold one player's snap lines for ONE week into a single usage line.

    A player who changes clubs mid-week has a snap line at EACH club, and since
    item 3.2c's migration 007 both are stored and both are returned (before it,
    the second silently REPLACED the first on the primary key — that is the
    finding, F-E). ``get_snap_counts``' docstring says so explicitly: "callers
    that need a single line per player-week must decide whether to sum them or
    pick the club they care about; they must not assume uniqueness". This is
    that decision, made once, here.

    Measured population on the 2021-2025 backfill: **one** player-week
    (a cornerback with 0.0 offensive snaps at both clubs), so nothing in the
    stored data moves. It is written anyway because the alternative — indexing
    into a dict and keeping whichever row SQL happened to yield last — makes a
    mid-season trade, which is exactly the role change this module exists to
    detect, resolve by scan order.

    ``offense_snaps`` sums: it is a COUNT, and his week's offensive workload is
    the two clubs' snaps added. ``offense_pct`` cannot be summed (two shares of
    two different play counts), so it is re-derived from the counts:
    ``plays_i = snaps_i / pct_i`` recovers each club's offensive play total, and
    the combined share is ``sum(snaps) / sum(plays)``. Where a club's share
    cannot be inverted (``pct == 0`` with ``snaps > 0``; zero such rows in
    132,616 real ones) the combined share is ``None``, which the caller reports
    as unknown rather than as a real 0.0.
    """
    if len(rows) == 1:
        return rows[0]
    snaps = sum(_f(r["offense_snaps"]) for r in rows)
    plays = 0.0
    for r in rows:
        club_snaps, club_pct = _f(r["offense_snaps"]), _f(r["offense_pct"])
        if club_snaps == 0:
            continue
        if club_pct <= 0:
            return {"offense_snaps": snaps, "offense_pct": None}
        plays += club_snaps / club_pct
    return {"offense_snaps": snaps,
            "offense_pct": (snaps / plays) if plays else 0.0}


def usage_deltas(
    conn,
    *,
    as_of,
    season: int,
    week: int,
    position: str | None = "RB",
    view: base.AsOfView = "historical",
) -> list[dict]:
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
    for r in get_weekly_stats(
        conn, as_of=as_of, season=season, position=position, view=view
    ):
        by_player.setdefault(r["player_id"], {})[r["week"]] = r

    # A (gsis_id, week) can carry MORE THAN ONE snap line since migration 007
    # (one per club, for a player who moved mid-week), so collect and fold —
    # never index, which would keep whichever row SQL yielded last.
    by_week: dict[tuple[str, int], list] = {}
    for s in get_snap_counts(conn, as_of=as_of, season=season, view=view):
        if s["gsis_id"] is not None:
            by_week.setdefault((s["gsis_id"], s["week"]), []).append(s)
    snaps = {k: _combine_clubs(v) for k, v in by_week.items()}

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
        # ``None`` means UNKNOWN here, and _f() would turn an unknown share into
        # a real 0.0 — the same "two facts, one encoding" class item 3.2 paid
        # for. Zero such rows in the 132,616 real ones; only _combine_clubs can
        # produce one today.
        pct_known = both and sc["offense_pct"] is not None and sp["offense_pct"] is not None
        row["d_offense_pct"] = (_f(sc["offense_pct"]) - _f(sp["offense_pct"])
                                if pct_known else None)
        out.append(row)
    return out
