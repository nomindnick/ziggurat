"""Market consensus rankings ingestion (import_ff_rankings) — item 1.5.

FantasyPros Expert Consensus Rank (ECR) is the point-in-time *market* rank of a
player: the single free signal that spans the backtest window (spike 1.2). One
row = a player's rank for one ``ecr_type`` on one ``scrape_date``. The frame
carries several ranking flavors (``ro`` redraft-overall, ``rp`` redraft-
positional, dynasty/best-ball/superflex variants); every flavor is ingested and
the accessor filters by ``ecr_type`` (default ``ro``).

knowable_as_of is the FantasyPros ``scrape_date`` — a Friday scrape is knowable
before that week's games, so stamping the scrape day is leakage-safe.

Crosswalk: ECR is FantasyPros-keyed. ``base.ids_by_fantasypros`` resolves the
FantasyPros id to (gsis_id, espn_id) from the players table. DST rows carry a
FantasyPros *team* id that is absent from the player crosswalk, so they keep a
NULL gsis_id and are joined downstream by normalized team abbr (LAR->LA,
JAC->JAX via TEAM_ALIASES). ~6% of even the draftable range is unresolved for
skill players too; those rows are KEPT with a NULL gsis_id (never dropped) — the
count is surfaced via ``note_drops`` rather than silently assumed irrelevant.

IDP rows (DB/DL/LB) contaminate the overall ECR (a LB can rank top-5 overall) but
our league scores none of them, so they are dropped at ingest. ``pos_rank`` is
derived here by ECR order over the LEAGUE positions only, so a positional rank is
never polluted by an IDP sitting "ahead" of a startable player.
"""

from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

# Positions our league actually starts. Everything else (DB/DL/LB IDP) is dropped.
LEAGUE_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DST"})

# Source columns we require (fail loud if an upstream release drops one).
_REQUIRED = (
    "id", "player", "pos", "team", "ecr_type",
    "ecr", "sd", "best", "worst", "player_owned_avg", "scrape_date",
)

# db_column -> source_column for the straight-through fields (derived columns
# gsis_id/espn_id/season/pos_rank are added per-row after the crosswalk join).
_COLMAP = {
    "fantasypros_id": "id",
    "player": "player",
    "position": "pos",
    "team": "team",
    "ecr_type": "ecr_type",
    "ecr": "ecr",
    "sd": "sd",
    "best": "best",
    "worst": "worst",
    "player_owned_avg": "player_owned_avg",
    "scrape_date": "scrape_date",
}


def _coerce_fp_id(value):
    """FantasyPros id arrives as an int64; store the bare digit string so it
    matches players.fantasypros_id (players.py normalizes its side the same way)."""
    if value is None:
        return None
    if isinstance(value, float):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def _assign_pos_rank(rows) -> None:
    """Derive pos_rank in place: within each (ecr_type, scrape_date, position),
    order by ecr ascending (lower ECR = better) and number 1..n. IDP rows are
    already gone, so the positional rank spans league positions only."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["ecr_type"], row["scrape_date"], row["position"])
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        # None ECR sorts last so a ranked player always precedes an unranked one.
        group.sort(key=lambda r: (r["ecr"] is None, r["ecr"] if r["ecr"] is not None else 0.0))
        for i, row in enumerate(group, start=1):
            row["pos_rank"] = i


def ingest_adp_rankings(conn, df, *, retrieved_as_of: str) -> int:
    """Persist market ECR rankings, stamping knowable_as_of with the scrape date.

    Drops IDP rows (not startable in our league); resolves gsis_id/espn_id via the
    FantasyPros crosswalk (DST + unresolved keep NULL gsis_id); normalizes team via
    TEAM_ALIASES; derives pos_rank by ECR order over league positions only.
    """
    base.require_columns(df, _REQUIRED, source="adp_rankings")
    crosswalk = base.ids_by_fantasypros(conn)

    rows = base.frame_to_rows(
        df,
        _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=lambda src: base.iso_date(src.get("scrape_date")),
    )

    kept: list[dict] = []
    unresolved = 0
    for row in rows:
        if row["position"] not in LEAGUE_POSITIONS:
            continue  # IDP — dropped below (counted by kept-vs-total)
        row["fantasypros_id"] = _coerce_fp_id(row["fantasypros_id"])
        if row["team"] is not None:
            row["team"] = base.TEAM_ALIASES.get(row["team"], row["team"])
        gsis, espn = crosswalk.get(row["fantasypros_id"], (None, None))
        row["gsis_id"] = gsis
        row["espn_id"] = espn
        if gsis is None and row["position"] != "DST":
            unresolved += 1  # kept (NULL gsis_id), not dropped
        row["season"] = int(row["scrape_date"][:4]) if row["scrape_date"] else None
        kept.append(row)

    idp_dropped = len(rows) - len(kept)
    base.note_drops("adp_rankings", idp_dropped, len(rows), why="IDP position (not startable)")
    base.note_drops(
        "adp_rankings", unresolved, len(kept),
        why="unresolved FantasyPros crosswalk id (kept, NULL gsis_id)",
    )

    _assign_pos_rank(kept)
    return base.upsert(conn, "adp_rankings", kept)


def pull_adp_rankings(conn, *, retrieved_as_of: str) -> int:
    """Pull the current FantasyPros ECR scrape and store it. ``nfl.import_ff_rankings``
    is the seam cached-fixture tests patch. The endpoint carries a single current
    scrape_date; a weekly panel accumulates by pulling every week (dedup the in-
    flight edge-week scrape) or the Phase-4 db_fpecr backfill into this table."""
    df = nfl.import_ff_rankings()
    return ingest_adp_rankings(conn, df, retrieved_as_of=retrieved_as_of)


def get_adp_rankings(
    conn,
    *,
    as_of,
    season=None,
    position=None,
    ecr_type="ro",
    view: base.AsOfView = "historical",
):
    """Market ranking rows knowable on or before ``as_of`` (keyword-only; no
    implicit now). Defaults to the redraft-overall (``ro``) ECR; pass
    ``ecr_type=None`` to read every flavor. Backtest reads go through
    ``base.latest_truth(get_adp_rankings)``."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if position is not None:
        clauses.append("t.position = :position")
        params["position"] = position
    if ecr_type is not None:
        clauses.append("t.ecr_type = :ecr_type")
        params["ecr_type"] = ecr_type
    return base.select_as_of(
        conn, "adp_rankings", as_of=as_of,
        key_cols=["fantasypros_id", "ecr_type", "scrape_date"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
