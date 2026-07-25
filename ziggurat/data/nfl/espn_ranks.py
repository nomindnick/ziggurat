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


# Minimum fraction of mapped rows that must carry an editorial PPR rank. The
# live pool runs ~99.9% (a lone fringe rookie shipped only an ELIMINATION block,
# 2026-07-24); wholesale schema drift (ESPN renames the PPR key) drops coverage
# to ~0, so the gap between the two regimes is wide.
_MIN_EDITORIAL_COVERAGE = 0.5

# Minimum fraction of the STORED board an incoming snapshot must carry before it
# is allowed to replace it. Same value and same reasoning as league state's
# _MIN_SNAPSHOT_FRACTION (ziggurat/league/state.py): a refused pull is retried by
# the next run, a destroyed board is gone.
_MIN_BOARD_FRACTION = 0.75


class BoardCollapse(RuntimeError):
    """A degraded ESPN response would have shrunk or emptied the stored board.

    The NFL sibling of ``league.state.SnapshotCollapse``, and it exists for the
    same measured reason: ``ingest_espn_ranks`` is the ONLY delete-then-write
    path in ``ziggurat/data/nfl/``, and item 3.1b reproduced the item-3.1 bug
    here exactly — a 20-player degraded response replaced a stored 1,026-player
    same-day board (before=1026, after=20), and an EMPTY response wiped it to
    zero, because the editorial-coverage guard sat behind ``if rows:`` and the
    DELETE ran unconditionally afterwards.

    Why that is worse than it looks: the DELETE is scoped to (season, TODAY),
    and today's partition is the one `draft-board` / `draft-web` / `valuation
    --espn` read. After a wipe ``get_espn_draft_ranks`` silently falls back to
    the previous day's snapshot PER board_key, so the cockpit still renders — as
    a stale/mixed hybrid, with nothing reporting the substitution.
    """


def _partition_size(conn, *, season: int, day: str) -> int:
    """Row count of one stored (season, retrieved_as_of) partition."""
    return conn.execute(
        "SELECT COUNT(*) FROM espn_draft_ranks WHERE season = ? AND retrieved_as_of = ?",
        (season, day),
    ).fetchone()[0]


def _board_size(conn, *, season: int, stamp: str) -> int:
    """The yardstick the incoming snapshot must clear: the LARGER of

      * the partition this write will DELETE (``stamp``) — the rows actually at
        risk, and
      * the most recent stored partition — so the very first pull of a new day
        is still measured against yesterday's board.

    Taking the max of the two is not belt-and-braces, it is the fix for a
    measured hole (3.1b audit): the old version measured only MAX(retrieved_as_of)
    while the DELETE targets ``stamp``, so a back-stamped or ``--allow-backfill``
    write compared a 600-row pull against a 500-row CURRENT board (floor 375),
    sailed through, and wiped a 2,051-row historical partition.
    """
    latest = conn.execute(
        "SELECT MAX(retrieved_as_of) FROM espn_draft_ranks WHERE season = ?", (season,)
    ).fetchone()[0]
    sizes = [_partition_size(conn, season=season, day=stamp)]
    if latest is not None:
        sizes.append(_partition_size(conn, season=season, day=latest))
    return max(sizes)


def _check_board_size(conn, rows, *, season: int, stamp: str, allow_shrink: bool) -> int:
    """Refuse to replace a stored board with a materially smaller (or empty) one.
    Returns the yardstick used, so the post-write re-count can reuse it.

    Called BEFORE the DELETE, never after: a refused pull leaves the stored board
    untouched and the next run retries; a destroyed board three weeks before
    draft day is not retryable.

    Measures DISTINCT ``board_key``s, not ``len(rows)``: the stored side is
    post-dedup (INSERT OR REPLACE collapses onto the key) so comparing a raw list
    length against it compares two different quantities — a response carrying
    duplicate ids would clear the floor and then collapse the board to a handful
    of rows, with ``rows_written`` still reporting the full incoming count.
    """
    if not rows:
        # Unconditional, and NOT covered by allow_shrink: ESPN never legitimately
        # serves zero draftable players. This is the case the old `if rows:`
        # guard skipped entirely on its way to the DELETE.
        raise BoardCollapse(
            f"refusing to write an EMPTY ESPN board for season {season} at {stamp}: "
            "the pull mapped 0 players (expired cookies, a payload shape change, or a "
            "degraded response). The stored board is untouched; the next run will retry."
        )

    previous = _board_size(conn, season=season, stamp=stamp)
    if allow_shrink or not previous:
        return 0  # override, or the first board of the season: nothing to compare

    incoming = len({r["board_key"] for r in rows})
    floor = int(previous * _MIN_BOARD_FRACTION)
    if incoming < floor:
        raise BoardCollapse(
            f"refusing to replace the stored {stamp} ESPN board for season {season}: "
            f"incoming snapshot has {incoming} players vs {previous} stored "
            f"(floor {floor} = {_MIN_BOARD_FRACTION:.0%}). ESPN likely returned a "
            "degraded pool. The stored board is untouched; the next run will retry. "
            "Re-run with --allow-shrink only if the shrink is real."
        )
    return previous


def _editorial_rank(raw) -> int | None:
    """Read the PRIMARY editorial PPR board rank, or None when this ROW carries
    no readable PPR signal (container absent, ``PPR`` block missing, or ``rank``
    missing). Individual sparse rows are real — ESPN ships the odd fringe player
    with only an ELIMINATION rank — so the loud wholesale-drift guard lives at
    the snapshot level in ``ingest_espn_ranks``, not here."""
    ranks = raw.get("draftRanksByRankType")
    if ranks is None:
        return None
    ppr = ranks.get("PPR") or {}
    return ppr.get("rank")


def map_espn_player(raw) -> dict | None:
    """Map ONE raw ``p["player"]`` dict to a board row, or None for a non-league
    position (defaultPositionId outside DEFPOS).

    Emits ``{espn_id, player, position, team, overall_rank, adp}`` (the derived
    ``espn_pos_rank``/``espn_adp_pos_rank``/``season`` are added by the ingest).
    ``espn_id`` is ``str(id)`` for skill players and None for DST (synthetic
    negative id). A row with no readable PPR rank maps with ``overall_rank``
    None; wholesale drift is caught in ``ingest_espn_ranks``."""
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


def ingest_espn_ranks(conn, raw_players, *, retrieved_as_of: str, season: int,
                      allow_shrink: bool = False) -> int:
    """Persist a full ESPN board snapshot. The board is a LIVE mutable signal, so
    knowable_as_of = retrieved_as_of = the pull day (design D8) — a backtest reads
    it via ``base.latest_truth(get_espn_draft_ranks)``.

    Non-league positions are filtered by ``map_espn_player`` (not counted as
    drops). The pull is the FULL universe, so a re-pull on the same retrieved day
    REPLACES that whole snapshot — the (season, retrieved_as_of) partition is
    cleared first. This also sidesteps SQLite's NULL-in-unique-index semantics:
    DST rows store espn_id NULL, so INSERT OR REPLACE alone would not dedup them.

    That replacement is what makes this the one destructive write in the NFL data
    layer, so it is fenced (item 3.1b): ``_check_board_size`` runs BEFORE the
    DELETE, the whole replacement is ONE transaction, and ``allow_shrink`` is the
    operator's explicit override for a shrink that is real.
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

    # SIZE FLOOR FIRST, before anything is deleted (item 3.1b). This also covers
    # the empty case, which the old `if rows:` drift guard skipped on its way to
    # an unconditional DELETE.
    previous = _check_board_size(conn, rows, season=season, stamp=stamp,
                                 allow_shrink=allow_shrink)

    # Wholesale-drift guard: individual rows may lack a PPR rank (fringe players
    # with only an ELIMINATION block), but if MOST of the snapshot has none, ESPN
    # changed the payload shape and every downstream rank would silently corrupt.
    covered = sum(1 for r in rows if r["overall_rank"] is not None)
    if covered / len(rows) < _MIN_EDITORIAL_COVERAGE:
        raise ValueError(
            "ESPN payload schema drift: only "
            f"{covered}/{len(rows)} mapped rows carry a PPR editorial rank "
            f"(min coverage {_MIN_EDITORIAL_COVERAGE:.0%})"
        )

    _assign_pos_ranks(rows)

    for row in rows:
        row["retrieved_as_of"] = stamp
        row["knowable_as_of"] = stamp
        # ensure a uniform key set for base.upsert (derived ranks always present)
        for col in _ROW_COLUMNS:
            row.setdefault(col, None)

    # ONE transaction: the day's board is replaced atomically or not at all. With
    # the per-call commit the DELETE and the insert were two transactions, so a
    # crash between them left the partition deleted and unreplaced.
    with conn:
        conn.execute(
            "DELETE FROM espn_draft_ranks WHERE season = ? AND retrieved_as_of = ?",
            (season, stamp),
        )
        base.upsert(conn, "espn_draft_ranks", rows, commit=False)
        # Report (and check) what is actually STORED, not what was handed in.
        # base.upsert returns len(rows), which over-reports whenever rows collapse
        # onto a board_key — so a key-collapsing response could wipe the board and
        # still log rows_written=1026. Raising inside `with conn:` rolls the whole
        # replacement back, leaving the previous board intact.
        written = _partition_size(conn, season=season, day=stamp)
        if previous and written < int(previous * _MIN_BOARD_FRACTION):
            raise BoardCollapse(
                f"refusing the {stamp} ESPN board for season {season}: {len(rows)} rows "
                f"collapsed onto {written} distinct board keys vs {previous} stored "
                f"(floor {int(previous * _MIN_BOARD_FRACTION)}). The stored board is "
                "restored by this rollback; the next run will retry."
            )
    return written


def pull_espn_ranks(conn, *, league_id, season, espn_s2, swid, retrieved_as_of: str,
                    today, allow_shrink: bool = False, allow_backfill: bool = False) -> int:
    """Live-pull the ESPN board and store it. ``espn_source.fetch_player_universe``
    is the network seam tests patch; no live call runs offline.

    ``today`` is required and BACK-stamping is REFUSED (3.1b audit finding). This
    function fetches LIVE data, so stamping it with a PAST ``retrieved_as_of``
    does two irreversible things at once: it DELETEs that past day's stored board
    (perishable — ESPN serves no history), and it manufactures a retrieval-time
    leak, because ``get_espn_draft_ranks(as_of=<that past day>)`` then serves
    today's board under the safe ``historical`` view as if it had been knowable
    then. The guard lives HERE rather than in the orchestrator so every caller
    inherits it — ``valuation --espn --as-of <past day>`` bypassed the
    orchestrator's copy entirely and destroyed the stored board.

    A FUTURE stamp is allowed: it deletes nothing (that partition cannot exist
    yet) and it under-claims knowledge rather than over-claiming it, so it cannot
    leak. That is the ``--as-of <a day the projections become knowable>`` case.
    """
    from ziggurat.data.nfl import espn_source

    stamp = base.iso_date(retrieved_as_of)
    day = base.iso_date(today)
    if stamp < day and not allow_backfill:
        raise ValueError(
            f"refusing to store a LIVE ESPN board pulled {day} under retrieved_as_of "
            f"{stamp}: it would DELETE the stored {stamp} board (which ESPN cannot "
            "re-serve) and would make today's ranks readable at a past as_of under the "
            "historical view. Read the stored board instead, or pass allow_backfill."
        )

    players = espn_source.fetch_player_universe(
        league_id=league_id, season=season, espn_s2=espn_s2, swid=swid
    )
    return ingest_espn_ranks(conn, players, retrieved_as_of=stamp, season=season,
                             allow_shrink=allow_shrink)


def ensure_board(conn, *, league_id, season, espn_s2, swid, as_of, today,
                 allow_shrink: bool = False) -> str:
    """Make the board for ``as_of`` available to a read, and say what it did.

    The policy the ``valuation --espn`` command needs, kept out of the CLI (rule
    3): refresh from ESPN unless ``as_of`` is in the PAST, in which case read
    whatever was stored on that day. A live pull back-stamped to a past day is
    refused by ``pull_espn_ranks``, and silently doing it was destroying a
    perishable board — but a *read* of a past board is a perfectly good request,
    so answer it instead of failing.
    """
    stamp = base.iso_date(as_of)
    day = base.iso_date(today)
    if stamp < day:
        stored = _partition_size(conn, season=season, day=stamp)
        return (f"espn board: read the stored {stamp} snapshot ({stored} rows); no live "
                f"pull, because a board pulled {day} is not what ESPN showed on {stamp}.")
    written = pull_espn_ranks(conn, league_id=league_id, season=season, espn_s2=espn_s2,
                              swid=swid, retrieved_as_of=stamp, today=day,
                              allow_shrink=allow_shrink)
    return f"espn board: refreshed {stamp} snapshot ({written} rows)."


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
