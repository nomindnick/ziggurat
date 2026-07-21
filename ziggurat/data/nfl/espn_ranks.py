"""ESPN default draft board mapper + ingest + as-of accessor (item 2.1).

The ESPN side of the "what the room can't see" report (design §4, D8/D9). The
live raw pull lives behind ``espn_source.fetch_player_universe`` (the one network
seam tests patch); this module is the PURE mapping + persistence + read layer.

Two ESPN signals are captured per player (design D9):
  * the EDITORIAL PPR board rank (``draftRanksByRankType["PPR"]["rank"]``,
    ``rankSourceId=0``) — ESPN's own default recommendation, the PRIMARY signal;
  * the native crowd ADP (``ownership.averageDraftPosition``) — a SECONDARY lens.
Both are OVERALL signals; a within-position rank is DERIVED here (mirroring
``adp_rankings._assign_pos_rank``) so the value view can diff positional ranks.

Position comes from ``defaultPositionId`` via ``DEFPOS`` (verified zero mismatches
across the 1025-player 2026 pool) — NOT espn_api's lineup-slot-keyed
``POSITION_MAP``. Team comes from espn_api ``PRO_TEAM_MAP`` then ``TEAM_ALIASES``
so DST/skill teams normalize to the schedules abbr the market side uses (WSH->WAS,
LAR->LA). DST rows carry synthetic NEGATIVE ESPN ids and are keyed downstream by
team, so their ``espn_id`` is stored NULL.
"""

from importlib import import_module

from ziggurat.data.nfl import base

# defaultPositionId -> canonical league position label. Verified against the live
# 2026 pool: zero mismatches across 1025 rows (design §4). D/ST uses the literal
# "D/ST" label, which scoring.DST_POSITIONS already accepts.
DEFPOS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
_DST_POSITION_ID = 16

# db_column tuple stored per row (uniform key set for base.upsert). board_key is
# the non-null temporal/identity key (str(espn_id) skill / team DST) — see the
# migration comment for why espn_id NULL for DST needs a separate key.
_ROW_COLUMNS = (
    "board_key", "espn_id", "player", "position", "team", "season",
    "overall_rank", "espn_pos_rank", "adp", "espn_adp_pos_rank",
)


def _pro_team_map():
    """Lazy import of espn_api's proTeamId->abbr table (mirrors the lazy client
    import in ``espn_source``; keeps ``espn_api`` off the offline import path)."""
    return import_module("espn_api.football.constant").PRO_TEAM_MAP


def _norm_team(abbr):
    """Normalize an ESPN abbr through TEAM_ALIASES; 'None'/'FA'/empty -> None."""
    if abbr in (None, "None", "FA", ""):
        return None
    return base.TEAM_ALIASES.get(abbr, abbr)


def _editorial_rank(raw) -> int | None:
    """Read the PRIMARY editorial PPR board rank, failing LOUD on schema drift.

    The board rank lives at ``draftRanksByRankType["PPR"]["rank"]``. Every player
    in the live pool carries it; if the ``draftRanksByRankType`` container is
    present but has lost its ``PPR`` block or that block has lost ``rank``, ESPN
    changed the payload shape and every downstream rank would silently corrupt —
    so we raise rather than coerce to None."""
    ranks = raw.get("draftRanksByRankType")
    if ranks is None:
        return None  # container absent entirely -> no editorial signal for this row
    if "PPR" not in ranks:
        raise ValueError(
            "ESPN payload schema drift: draftRanksByRankType present but missing "
            f"'PPR' block (keys={sorted(ranks)})"
        )
    ppr = ranks["PPR"] or {}
    if "rank" not in ppr:
        raise ValueError(
            "ESPN payload schema drift: draftRanksByRankType['PPR'] missing 'rank' "
            f"(keys={sorted(ppr)})"
        )
    return ppr["rank"]


def map_espn_player(raw) -> dict | None:
    """Map ONE raw ``p["player"]`` dict to a board row, or None for a non-league
    position (defaultPositionId outside DEFPOS).

    Emits ``{espn_id, player, position, team, overall_rank, adp}`` (the derived
    ``espn_pos_rank``/``espn_adp_pos_rank``/``season`` are added by the ingest).
    ``espn_id`` is ``str(id)`` for skill players and None for DST (synthetic
    negative id). Raises on ``draftRanksByRankType`` schema drift (see
    ``_editorial_rank``)."""
    pos_id = raw.get("defaultPositionId")
    position = DEFPOS.get(pos_id)
    if position is None:
        return None  # non-league position (IDP / FB / punter / ...) — skipped

    team = _norm_team(_pro_team_map().get(raw.get("proTeamId")))
    is_dst = pos_id == _DST_POSITION_ID
    espn_id = None if is_dst else str(raw["id"])

    ownership = raw.get("ownership") or {}
    return {
        "espn_id": espn_id,
        "player": raw.get("fullName"),
        "position": position,
        "team": team,
        "overall_rank": _editorial_rank(raw),
        "adp": ownership.get("averageDraftPosition"),
    }


def _assign_pos_ranks(rows) -> None:
    """Derive espn_pos_rank (by editorial ``overall_rank``) and espn_adp_pos_rank
    (by native ``adp``) IN PLACE, numbering 1..n within each position.

    Mirrors ``adp_rankings._assign_pos_rank``: lower overall_rank / lower adp = a
    better (smaller) positional rank; a None value sorts LAST so an unranked
    player never precedes a ranked one. Grouping is by (season, position)."""
    by_pos_rank: dict[tuple, list[dict]] = {}
    by_adp_rank: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["season"], row["position"])
        by_pos_rank.setdefault(key, []).append(row)
        by_adp_rank.setdefault(key, []).append(row)

    for group in by_pos_rank.values():
        group.sort(key=lambda r: (r["overall_rank"] is None,
                                  r["overall_rank"] if r["overall_rank"] is not None else 0))
        for i, row in enumerate(group, start=1):
            row["espn_pos_rank"] = i

    for group in by_adp_rank.values():
        group.sort(key=lambda r: (r["adp"] is None,
                                  r["adp"] if r["adp"] is not None else 0.0))
        for i, row in enumerate(group, start=1):
            row["espn_adp_pos_rank"] = i


def ingest_espn_ranks(conn, raw_players, *, retrieved_as_of: str, season: int) -> int:
    """Persist a full ESPN board snapshot. The board is a LIVE mutable signal, so
    knowable_as_of = retrieved_as_of = the pull day (design D8) — a backtest reads
    it via ``base.latest_truth(get_espn_draft_ranks)``.

    Non-league positions are filtered by ``map_espn_player`` (not counted as
    drops). The pull is the FULL universe, so a re-pull on the same retrieved day
    REPLACES that whole snapshot — the (season, retrieved_as_of) partition is
    cleared first. This also sidesteps SQLite's NULL-in-unique-index semantics:
    DST rows store espn_id NULL, so INSERT OR REPLACE alone would not dedup them.
    """
    stamp = base.iso_date(retrieved_as_of)

    rows: list[dict] = []
    for raw in raw_players:
        mapped = map_espn_player(raw)
        if mapped is None:
            continue
        mapped["season"] = season
        # board_key: str(espn_id) for skill (always present), team for DST
        # (espn_id NULL). Non-null by construction — the NOT NULL column enforces
        # it, so a would-be all-null row (e.g. a teamless DST) fails loud.
        mapped["board_key"] = mapped["espn_id"] or mapped["team"]
        rows.append(mapped)

    _assign_pos_ranks(rows)

    for row in rows:
        row["retrieved_as_of"] = stamp
        row["knowable_as_of"] = stamp
        # ensure a uniform key set for base.upsert (derived ranks always present)
        for col in _ROW_COLUMNS:
            row.setdefault(col, None)

    # Replace the whole (season, retrieved_as_of) snapshot for idempotency.
    conn.execute(
        "DELETE FROM espn_draft_ranks WHERE season = ? AND retrieved_as_of = ?",
        (season, stamp),
    )
    return base.upsert(conn, "espn_draft_ranks", rows)


def pull_espn_ranks(conn, *, league_id, season, espn_s2, swid, retrieved_as_of: str) -> int:
    """Live-pull the ESPN board and store it. ``espn_source.fetch_player_universe``
    is the network seam tests patch; no live call runs offline."""
    from ziggurat.data.nfl import espn_source

    players = espn_source.fetch_player_universe(
        league_id=league_id, season=season, espn_s2=espn_s2, swid=swid
    )
    return ingest_espn_ranks(conn, players, retrieved_as_of=retrieved_as_of, season=season)


def get_espn_draft_ranks(
    conn,
    *,
    as_of,
    season=None,
    position=None,
    view: base.AsOfView = "historical",
):
    """ESPN board rows knowable on or before ``as_of`` (keyword-only; no implicit
    now). Defaults to the safe ``historical`` view (gates both knowable_as_of and
    retrieved_as_of). Backtest reads go through
    ``base.latest_truth(get_espn_draft_ranks)``."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if position is not None:
        clauses.append("t.position = :position")
        params["position"] = position
    return base.select_as_of(
        conn, "espn_draft_ranks", as_of=as_of,
        key_cols=["season", "board_key"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
