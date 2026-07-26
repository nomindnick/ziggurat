"""League state mappers, ingest, and as-of accessors (item 3.1).

PURE layer: no network. ``ziggurat.league.source`` owns the HTTP seam;
``ziggurat.league.sync`` orchestrates. This module maps raw ESPN payloads to
rows, persists them, and reads them back under the as-of discipline (rule 1 —
every accessor is keyword-only ``as_of``, no implicit "now", every accessor has a
leakage test).

THE ONE THING TO UNDERSTAND HERE (see db/migrations/005_league_state.sql):
``league_player_state`` stores the WHOLE player universe every snapshot day, not
just rostered players. A drop must be a positive fact (a row with
``on_team_id`` NULL); otherwise the last "team 4 holds X" row stays the newest
row at or before every later ``as_of`` and ``who_held`` answers wrong forever.
The same property makes the free-agent pool a one-line filter on the same table.
"""

import json
import logging
import sqlite3
from datetime import date, datetime, timezone

from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import base
from ziggurat.data.nfl.espn_ranks import DEFPOS

logger = logging.getLogger("ziggurat.league")

# ESPN lineupSlotId -> slot label. The full standard football map (espn_api's
# POSITION_MAP), so an unfamiliar slot is genuinely unfamiliar rather than merely
# absent from a hand-written subset. This league uses {0,2,4,6,16,17,20,21,23}
# (QB/RB/WR/TE/D-ST/K/BE/IR/FLEX) — verified against the real 2025 rosters.
LINEUP_SLOTS: dict[int, str] = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE", 7: "OP",
    8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S", 14: "DB", 15: "DP",
    16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE", 21: "IR", 23: "FLEX",
    24: "ER", 25: "Rookie",
}

# ESPN activity messageTypeId -> action (espn_api's ACTIVITY_MAP, re-expressed
# with the waiver/FA distinction preserved — item 3.4 needs to tell a queued
# waiver claim from a first-come-first-served grab).
ACTIVITY_ACTIONS: dict[int, tuple[str, str]] = {
    178: ("ADD", "FREEAGENT"),
    180: ("ADD", "WAIVER"),
    179: ("DROP", "TEAM"),
    181: ("DROP", "TEAM"),
    239: ("DROP", "TEAM"),
    244: ("TRADE", "TEAM"),
}

_STARTING_SLOTS = frozenset({"QB", "RB", "WR", "TE", "FLEX", "D/ST", "K", "RB/WR", "WR/TE", "OP"})

_PLAYER_COLUMNS = (
    "season", "espn_player_id", "gsis_id", "player", "position", "pro_team",
    "on_team_id", "roster_status", "lineup_slot", "acquisition_type",
    "acquisition_date", "injury_status", "percent_owned", "percent_started",
    "percent_change", "scoring_period",
)
_TEAM_COLUMNS = (
    "season", "team_id", "abbrev", "name", "primary_owner", "division_id",
    "waiver_rank", "playoff_seed", "wins", "losses", "ties", "points_for",
    "points_against", "streak_length", "streak_type", "acquisitions", "drops",
    "trades", "moves_to_ir", "moves_to_active", "acquisition_budget_spent",
    "team_charges", "is_transaction_locked", "scoring_period",
)
_MATCHUP_COLUMNS = (
    "season", "week", "home_team_id", "away_team_id", "home_points", "away_points",
    "home_games_played", "away_games_played", "winner", "playoff_tier", "scoring_period",
)
_TRANSACTION_COLUMNS = (
    "season", "transaction_key", "week", "team_id", "espn_player_id", "action",
    "source", "status", "bid_amount", "proposed_at", "processed_at",
)
# The transaction fields whose change makes a stored event stale (write-on-change,
# §3.4 of the design): a claim really does mutate PENDING -> EXECUTED overnight.
_TRANSACTION_MUTABLE = ("action", "source", "status", "bid_amount", "proposed_at", "processed_at")

# Minimum fraction of SKILL players (non-D/ST) that must crosswalk to a gsis_id.
# The live pool runs high but never 100% (ESPN carries fringe/practice-squad
# players nflverse has not issued an id for). Wholesale drift — a players table
# that was never loaded, or an espn_id format change — drops coverage to ~0, so
# the gap between the two regimes is wide. Below this the run is downgraded to
# 'partial' — NOT failed: see _gsis_coverage.
_MIN_GSIS_COVERAGE = 0.5

# A replacement snapshot must be at least this fraction of the previous one,
# both in total universe size and in rostered count. Day-over-day churn in
# ESPN's universe is ~0-2%; a 25% collapse means a degraded response, not news.
#
# WHY THIS EXISTS (audit finding, reproduced): ingest replaces a day by deleting
# its partition and rewriting it. Without a floor, a single degraded pull — ESPN
# answering 200 with an empty `players` array on the 11:15 run — DELETES a
# complete 05:15 snapshot and writes nothing, so the newest surviving row for a
# player is the PREVIOUS day's, and a player dropped that morning reverts to his
# stale holder for the rest of the season. That is exactly the failure the
# whole-universe design exists to prevent, reintroduced through the replace.
# Refusing is always safe here: a refused day is retried three more times by the
# timer, while a destroyed day is gone forever.
_MIN_SNAPSHOT_FRACTION = 0.75


class SnapshotCollapse(RuntimeError):
    """A replacement snapshot is materially smaller than the one it would destroy.

    Raised BEFORE any DELETE, so the stored day survives untouched. The operator
    can override with ``allow_shrink=True`` (``--allow-shrink``) once they have
    confirmed the shrink is real (e.g. ESPN pruning the universe in the
    offseason) rather than a degraded response.
    """


def is_starting_slot(slot) -> bool:
    """True when a decoded lineup slot is a STARTING slot (not BE/IR).

    Bench and IR are the two slots that do not score, and every consumer
    (3.2 marginal valuation, 3.5 lineup support) needs that distinction; putting
    it here keeps the slot vocabulary in one module.
    """
    return slot in _STARTING_SLOTS


def _epoch_ms_to_iso(value, *, date_only: bool = False) -> str | None:
    """ESPN epoch-milliseconds -> ISO string in LOCAL time, or None.

    Timestamps are kept at FULL precision (offset-aware) for transactions — they
    are the only intraday-accurate record in the system (design §3.4).
    ``date_only`` truncates to the calendar day the as-of gate reads.

    LOCAL, not UTC, and that matters: every other date in this system is a local
    calendar day (the CLI stamps ``retrieved_as_of`` from ``date.today()``).
    Deriving the day in UTC put every evening event on the NEXT day west of
    Greenwich — so an 8pm waiver add was stamped knowable TOMORROW, invisible to
    a same-evening read and producing knowable_as_of > retrieved_as_of, which the
    as-of model treats as impossible (audit finding). ``astimezone()`` with no
    argument converts to the machine's local zone, which is by construction the
    same clock ``date.today()`` reads.
    """
    if value in (None, "", 0):
        return None
    try:
        moment = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return moment.date().isoformat() if date_only else moment.isoformat(timespec="seconds")


def _norm_team(abbr):
    """Normalize an ESPN pro-team abbr through TEAM_ALIASES; FA/None -> None."""
    if abbr in (None, "None", "FA", ""):
        return None
    return base.TEAM_ALIASES.get(abbr, abbr)


def _pro_team_map():
    """Lazy import of espn_api's proTeamId->abbr table (mirrors espn_ranks)."""
    from importlib import import_module

    return import_module("espn_api.football.constant").PRO_TEAM_MAP


def decode_slot(slot_id):
    """lineupSlotId -> label. An unknown id is stored as its raw string and
    logged, never silently dropped or coerced to bench (a mis-decoded slot would
    make a starter look benched to 3.5)."""
    if slot_id is None:
        return None
    label = LINEUP_SLOTS.get(slot_id)
    if label is None:
        logger.warning("league state: unknown lineupSlotId %r (stored raw)", slot_id)
        return str(slot_id)
    return label


# ----------------------------------------------------------------- mappers


def map_team(raw: dict, *, season: int, scoring_period=None) -> dict:
    """Map one raw ``teams[]`` entry to a ``league_teams`` row.

    ``name``/``abbrev``/``primary_owner`` are LEAGUE-PRIVATE (rule 5): they belong
    in the gitignored database and must never reach a committed fixture.
    """
    record = (raw.get("record") or {}).get("overall") or {}
    counter = raw.get("transactionCounter") or {}
    owners = raw.get("owners") or []
    return {
        "season": season,
        "team_id": raw.get("id"),
        "abbrev": raw.get("abbrev"),
        "name": raw.get("name"),
        "primary_owner": raw.get("primaryOwner") or (owners[0] if owners else None),
        "division_id": raw.get("divisionId"),
        "waiver_rank": raw.get("waiverRank"),
        "playoff_seed": raw.get("playoffSeed"),
        "wins": record.get("wins"),
        "losses": record.get("losses"),
        "ties": record.get("ties"),
        "points_for": record.get("pointsFor"),
        "points_against": record.get("pointsAgainst"),
        "streak_length": record.get("streakLength"),
        "streak_type": record.get("streakType"),
        "acquisitions": counter.get("acquisitions"),
        "drops": counter.get("drops"),
        "trades": counter.get("trades"),
        "moves_to_ir": counter.get("moveToIR"),
        "moves_to_active": counter.get("moveToActive"),
        "acquisition_budget_spent": counter.get("acquisitionBudgetSpent"),
        "team_charges": counter.get("teamCharges"),
        "is_transaction_locked": int(bool(raw.get("isTransactionLocked"))),
        "scoring_period": scoring_period,
    }


def map_matchup(raw: dict, *, season: int, scoring_period=None) -> dict | None:
    """Map one raw ``schedule[]`` entry to a ``league_matchups`` row, or None when
    it carries no home side (the row could not be keyed)."""
    home = raw.get("home") or {}
    away = raw.get("away") or {}
    if home.get("teamId") is None or raw.get("matchupPeriodId") is None:
        return None
    return {
        "season": season,
        "week": raw.get("matchupPeriodId"),
        "home_team_id": home.get("teamId"),
        "away_team_id": away.get("teamId"),
        "home_points": home.get("totalPoints"),
        "away_points": away.get("totalPoints"),
        "home_games_played": home.get("gamesPlayed"),
        "away_games_played": away.get("gamesPlayed"),
        "winner": raw.get("winner"),
        "playoff_tier": raw.get("playoffTierType"),
        "scoring_period": scoring_period,
    }


def roster_index(payload: dict) -> dict[str, dict]:
    """Build ``espn_player_id -> holding info`` from the AUTHORITATIVE ``mRoster``
    view of the league-state payload.

    This is one of the two independent answers to "who holds whom" (the other is
    the player pool's entry-level ``onTeamId``); ``ingest_player_state``
    cross-checks them and this one wins. Only this view carries lineup slot and
    acquisition provenance.
    """
    index: dict[str, dict] = {}
    for team in payload.get("teams") or []:
        team_id = team.get("id")
        for entry in ((team.get("roster") or {}).get("entries") or []):
            player_id = entry.get("playerId")
            if player_id is None:
                continue
            index[str(player_id)] = {
                "on_team_id": team_id,
                "lineup_slot": decode_slot(entry.get("lineupSlotId")),
                "acquisition_type": entry.get("acquisitionType"),
                "acquisition_date": _epoch_ms_to_iso(entry.get("acquisitionDate"), date_only=True),
            }
    return index


def map_player_entry(entry: dict, *, season: int, scoring_period=None) -> dict | None:
    """Map one raw player-pool ENTRY to a ``league_player_state`` row, or None for
    a non-league position (IDP/punter/coach — ``defaultPositionId`` outside DEFPOS).

    Holding fields come from the entry's own ``onTeamId``; ``ingest_player_state``
    overlays the authoritative roster index on top. ``gsis_id`` is filled at
    ingest (it needs a database).
    """
    player = entry.get("player") or {}
    position = DEFPOS.get(player.get("defaultPositionId"))
    if position is None:
        return None

    player_id = player.get("id", entry.get("id"))
    if player_id is None:
        return None

    ownership = player.get("ownership") or {}
    on_team = entry.get("onTeamId")
    return {
        "season": season,
        "espn_player_id": str(player_id),
        "gsis_id": None,
        "player": player.get("fullName"),
        "position": position,
        "pro_team": _norm_team(_pro_team_map().get(player.get("proTeamId"))),
        # ESPN's free-agent sentinel is 0; store NULL so "unrostered" is a single
        # representation everywhere (the schema, the FA filter, and who_held).
        "on_team_id": on_team if on_team not in (None, 0, -1) else None,
        "roster_status": entry.get("status"),
        "lineup_slot": None,
        "acquisition_type": None,
        "acquisition_date": None,
        "injury_status": player.get("injuryStatus"),
        "percent_owned": ownership.get("percentOwned"),
        "percent_started": ownership.get("percentStarted"),
        "percent_change": ownership.get("percentChange"),
        "scoring_period": scoring_period,
    }


def map_transaction(raw: dict, *, season: int) -> list[dict]:
    """Map one raw ESPN transaction to one row PER ITEM (a transaction can add and
    drop several players at once).

    UNVERIFIED SHAPE: this league has never served a non-empty transaction feed
    (2025's is absent, 2026's is empty pre-draft), so the field names follow
    espn_api's own ``Transaction`` parser — derived from real payloads elsewhere —
    and every read is defensive. Nothing depends on this table (design §1.3);
    it adds timestamp precision when ESPN cooperates.
    """
    items = raw.get("items") or []
    txn_id = raw.get("id") or f"{raw.get('teamId')}-{raw.get('proposedDate')}"
    proposed = _epoch_ms_to_iso(raw.get("proposedDate"))
    processed = _epoch_ms_to_iso(raw.get("processDate"))
    rows = []
    for index, item in enumerate(items):
        player_id = item.get("playerId")
        rows.append({
            "season": season,
            "transaction_key": f"{txn_id}:{index}:{player_id}",
            "week": raw.get("scoringPeriodId"),
            "team_id": item.get("toTeamId") or raw.get("teamId"),
            "espn_player_id": None if player_id is None else str(player_id),
            "action": item.get("type"),
            "source": raw.get("type"),
            "status": raw.get("status"),
            "bid_amount": raw.get("bidAmount"),
            "proposed_at": proposed,
            "processed_at": processed,
        })
    return rows


def map_activity_topic(topic: dict, *, season: int) -> list[dict]:
    """Map one raw communication topic to transaction rows (one per message).

    Same UNVERIFIED-shape caveat as ``map_transaction``; field names follow
    espn_api's ``Activity`` parser. Message id 239 names its team in ``for``;
    every other type (including 244/TRADE) uses ``to`` — see the loop comment.
    """
    topic_id = topic.get("id") or topic.get("date")
    rows = []
    for index, msg in enumerate(topic.get("messages") or []):
        msg_type = msg.get("messageTypeId")
        action, source = ACTIVITY_ACTIONS.get(msg_type, ("UNKNOWN", None))
        # 239 names its team in 'for'; every other type (including 244/TRADE)
        # names it in 'to'. For a trade that is the ACQUIRING side, which matches
        # the semantics of every other row here ("the team that ended up with
        # this player"); espn_api additionally emits the sending side from
        # 'from', which we deliberately do not, since a trade shows up as two
        # messages (one per player) and the acquiring side is the holding fact.
        team_id = msg.get("for") if msg_type == 239 else msg.get("to")
        player_id = msg.get("targetId")
        stamp = _epoch_ms_to_iso(msg.get("date") or topic.get("date"))
        rows.append({
            "season": season,
            "transaction_key": f"act:{topic_id}:{index}:{player_id}",
            "week": None,
            "team_id": team_id,
            "espn_player_id": None if player_id is None else str(player_id),
            "action": action,
            "source": source,
            "status": "EXECUTED",
            # msg['from'] carries the winning bid on a WAIVER ADDED message.
            "bid_amount": msg.get("from") if msg_type == 180 else None,
            "proposed_at": stamp,
            "processed_at": stamp,
        })
    return rows


# ------------------------------------------------------------------ ingest


def ingest_league_state(conn, payload: dict, *, retrieved_as_of, season: int) -> dict:
    """Persist the teams + matchups halves of one league-state snapshot.

    Idempotent by the item-2.1 pattern: the ``(season, retrieved_as_of)``
    partition is deleted and rewritten, so re-running a day (or a cron firing
    twice) replaces rather than duplicates.
    """
    stamp = normalize_as_of(retrieved_as_of).isoformat()
    scoring_period = payload.get("scoringPeriodId")

    teams = [
        map_team(raw, season=season, scoring_period=scoring_period)
        for raw in (payload.get("teams") or [])
    ]
    teams = [t for t in teams if t["team_id"] is not None]
    if not teams:
        # Never delete a stored day for a payload that cannot replace it.
        raise SnapshotCollapse(
            "league-state payload mapped to ZERO teams — refusing to replace the "
            f"stored {stamp} snapshot with an empty one"
        )

    raw_matchups = payload.get("schedule") or []
    matchups = [map_matchup(raw, season=season, scoring_period=scoring_period) for raw in raw_matchups]
    kept = [m for m in matchups if m is not None]
    base.note_drops("league_matchups", len(raw_matchups) - len(kept), len(raw_matchups),
                    why="no home teamId / matchupPeriodId")

    # ONE transaction for the whole delete-then-rewrite. Two transactions (the
    # default per-call commit in base.upsert) leave a window where the day is
    # deleted and not yet replaced; a crash there loses it permanently.
    with conn:
        for rows, table, columns in (
            (teams, "league_teams", _TEAM_COLUMNS),
            (kept, "league_matchups", _MATCHUP_COLUMNS),
        ):
            if not rows:
                continue  # nothing to write -> nothing to destroy
            conn.execute(f"DELETE FROM {table} WHERE season = ? AND retrieved_as_of = ?",
                         (season, stamp))
            for row in rows:
                row["retrieved_as_of"] = stamp
                row["knowable_as_of"] = stamp
                for col in columns:
                    row.setdefault(col, None)
            base.upsert(conn, table, rows, commit=False)

    return {"teams": len(teams), "matchups": len(kept)}


def ingest_player_state(
    conn,
    entries,
    *,
    retrieved_as_of,
    season: int,
    roster: dict[str, dict] | None = None,
    scoring_period=None,
    allow_shrink: bool = False,
) -> dict:
    """Persist one full-universe player-state snapshot.

    ``roster`` is the authoritative ``roster_index`` from ``mRoster``. It is
    overlaid on the pool's own ``onTeamId``, and every DISAGREEMENT is counted
    and logged rather than silently resolved: a nonzero count means ESPN's views
    were mid-flush (the exact failure mode Checkpoint 2 hit during live drafts),
    which the run log surfaces instead of burying.

    A player who is on a roster but absent from the pool response is still
    written — losing a rostered player from the snapshot would make him look
    dropped, which is unrecoverable history.

    Refuses (``SnapshotCollapse``, BEFORE any delete) when the incoming snapshot
    is materially smaller than the one it would replace — see
    ``_MIN_SNAPSHOT_FRACTION``. ``allow_shrink`` overrides that once the operator
    has confirmed the shrink is real.
    """
    stamp = normalize_as_of(retrieved_as_of).isoformat()
    roster = dict(roster or {})
    crosswalk = base.gsis_by_espn(conn)

    rows: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    conflicts = 0
    for entry in entries:
        row = map_player_entry(entry, season=season, scoring_period=scoring_period)
        if row is None:
            skipped += 1
            continue
        key = row["espn_player_id"]
        if key in seen:  # ESPN has been observed to repeat entries across pages
            continue
        seen.add(key)

        held = roster.get(key)
        if held is not None:
            # Count a disagreement in BOTH directions. The pool saying "free
            # agent" while mRoster still shows a holder is the direction that
            # matters most — it is what a half-flushed DROP looks like — and
            # gating on `is not None` silently swallowed exactly that case.
            # An entry with no onTeamId field at all asserts nothing, so it is
            # not a disagreement.
            if "onTeamId" in entry and row["on_team_id"] != held["on_team_id"]:
                conflicts += 1
            row.update(held)
        elif row["on_team_id"] is not None:
            # The pool says rostered, the authoritative roster view does not.
            conflicts += 1
            row["on_team_id"] = None
            row["roster_status"] = row["roster_status"] or "FREEAGENT"
        row["gsis_id"] = crosswalk.get(key)
        rows.append(row)

    # Rostered players missing from the pool response: synthesize from the roster
    # view so the holding is never lost.
    for key, held in roster.items():
        if key in seen:
            continue
        conflicts += 1
        rows.append({
            "season": season, "espn_player_id": key, "gsis_id": crosswalk.get(key),
            "player": None, "position": None, "pro_team": None,
            "roster_status": "ONTEAM", "injury_status": None,
            "percent_owned": None, "percent_started": None, "percent_change": None,
            "scoring_period": scoring_period, **held,
        })

    if conflicts:
        logger.warning(
            "league state: %d roster/pool disagreements at %s (mRoster wins; "
            "ESPN views may be mid-flush)", conflicts, stamp,
        )

    # Guard BEFORE the delete: a refused day is retried by the next timer run,
    # a destroyed day is gone forever.
    _check_snapshot_size(conn, rows, roster, season=season, stamp=stamp, allow_shrink=allow_shrink)
    coverage = _gsis_coverage(rows)

    with conn:  # one transaction: the day is replaced atomically or not at all
        conn.execute(
            "DELETE FROM league_player_state WHERE season = ? AND retrieved_as_of = ?",
            (season, stamp),
        )
        for row in rows:
            row["retrieved_as_of"] = stamp
            row["knowable_as_of"] = stamp
            for col in _PLAYER_COLUMNS:
                row.setdefault(col, None)
        written = base.upsert(conn, "league_player_state", rows, commit=False)
    logger.info("league_player_state: wrote %d rows at %s (%d non-league positions skipped)",
                written, stamp, skipped)
    return {"players": written, "conflicts": conflicts, "skipped": skipped,
            "gsis_coverage": coverage}


def _snapshot_sizes(conn, *, season: int) -> tuple[int, int]:
    """(universe rows, rostered rows) of the most recent stored snapshot day.

    The yardstick the collapse guard measures a replacement against. Includes
    today's own snapshot when one exists — a re-run must not shrink what an
    earlier run of the SAME day already captured.
    """
    day = conn.execute(
        "SELECT MAX(retrieved_as_of) FROM league_player_state WHERE season = ?", (season,)
    ).fetchone()[0]
    if day is None:
        return (0, 0)
    row = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(on_team_id) AS held FROM league_player_state "
        "WHERE season = ? AND retrieved_as_of = ?",
        (season, day),
    ).fetchone()
    return (row["n"], row["held"])


def _check_snapshot_size(conn, rows, roster, *, season: int, stamp: str, allow_shrink: bool) -> None:
    """Refuse to replace a stored snapshot with a materially smaller one.

    Two independent collapses are caught, because ESPN can degrade either view
    on its own: the player POOL coming back empty/short, and the mRoster view
    coming back empty (which would rewrite every rostered player as a free
    agent). Both were reproduced during the item-3.1 audit; both looked like a
    successful run.
    """
    if allow_shrink:
        logger.warning("league state: --allow-shrink set, skipping the collapse guard at %s", stamp)
        return

    previous_total, previous_held = _snapshot_sizes(conn, season=season)
    if not previous_total:
        return  # first snapshot of the season: nothing to lose, nothing to compare

    floor = int(previous_total * _MIN_SNAPSHOT_FRACTION)
    if len(rows) < floor:
        raise SnapshotCollapse(
            f"refusing to replace the stored {stamp} snapshot: incoming universe has "
            f"{len(rows)} players vs {previous_total} stored "
            f"(floor {floor} = {_MIN_SNAPSHOT_FRACTION:.0%}). ESPN likely returned a "
            "degraded pool. The stored day is untouched; the next run will retry. "
            "Re-run with --allow-shrink only if the shrink is real."
        )

    held_floor = int(previous_held * _MIN_SNAPSHOT_FRACTION)
    if previous_held and len(roster) < held_floor:
        raise SnapshotCollapse(
            f"refusing to replace the stored {stamp} snapshot: mRoster reports "
            f"{len(roster)} rostered players vs {previous_held} stored "
            f"(floor {held_floor}). Writing this would mark the league as mass free "
            "agency. The stored day is untouched; the next run will retry. "
            "Re-run with --allow-shrink only if the drop is real."
        )


def _gsis_coverage(rows) -> float | None:
    """Fraction of skill players that resolved to a gsis_id (None when N/A).

    Logs LOUDLY below ``_MIN_GSIS_COVERAGE`` — an unloaded ``players`` table or an
    id-format change takes coverage to ~0 and severs league state from the NFL
    spine 3.2 values it through — but deliberately does NOT raise.

    It used to raise, which inverted this system's own priority: ``gsis_id`` is a
    DERIVED column (``base.gsis_by_espn`` is crosswalk-at-now over immutable
    identity, so it can be recomputed and backfilled from ``players`` any time),
    while the ESPN league state being rejected is PERISHABLE and gone forever
    (audit finding). Never trade an unrecoverable asset to protect a recoverable
    one: the snapshot is written with gsis_id NULL and the run is downgraded to
    'partial' so the operator sees it.
    """
    skill = [r for r in rows if r.get("position") not in (None, "D/ST")]
    if not skill:
        return None
    coverage = sum(1 for r in skill if r.get("gsis_id")) / len(skill)
    if coverage < _MIN_GSIS_COVERAGE:
        logger.error(
            "espn->gsis crosswalk collapsed: only %.0f%% of %d skill players resolved "
            "(min %.0f%%). Snapshot IS being written (it is perishable); gsis_id is "
            "derived and can be backfilled. Is the players table loaded and current?",
            coverage * 100, len(skill), _MIN_GSIS_COVERAGE * 100,
        )
    return coverage


def ingest_transactions(conn, rows, *, retrieved_as_of, season: int) -> int:
    """Persist transaction rows WRITE-ON-CHANGE (design §3.4).

    A new version is written only when the mutable payload differs from the newest
    stored version of that key — a waiver claim genuinely mutates
    PENDING -> EXECUTED/FAILED overnight, so first-seen-wins would freeze it,
    while versioning every pull would rewrite the whole feed daily.

    ``knowable_as_of`` is the EVENT's own date (processed, else proposed, else the
    pull day) — the one table here whose knowledge time is not the pull day.
    """
    stamp = normalize_as_of(retrieved_as_of).isoformat()
    written = 0
    for row in rows:
        key = row.get("transaction_key")
        if not key:
            continue
        latest = conn.execute(
            """
            SELECT * FROM league_transactions
            WHERE season = ? AND transaction_key = ?
            ORDER BY retrieved_as_of DESC LIMIT 1
            """,
            (season, key),
        ).fetchone()
        if latest is not None and all(
            latest[field] == row.get(field) for field in _TRANSACTION_MUTABLE
        ):
            continue  # unchanged — no new version
        event_day = (
            base.iso_date(row.get("processed_at"))
            or base.iso_date(row.get("proposed_at"))
            or stamp
        )
        payload = {col: row.get(col) for col in _TRANSACTION_COLUMNS}
        payload["retrieved_as_of"] = stamp
        payload["knowable_as_of"] = event_day
        base.upsert(conn, "league_transactions", [payload])
        written += 1
    return written


# --------------------------------------------------------------- accessors


def get_team_state(conn, *, as_of, season, team_id=None, view: base.AsOfView = "historical"):
    """League teams (standings, waiver rank, transaction counters) as of a date."""
    clauses, params = [], {"season": season}
    clauses.append("t.season = :season")
    if team_id is not None:
        clauses.append("t.team_id = :team_id")
        params["team_id"] = team_id
    return base.select_as_of(
        conn, "league_teams", as_of=as_of, key_cols=["season", "team_id"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


class OwnTeamUnresolved(RuntimeError):
    """The operator's own league team could not be identified from the SWID."""


def resolve_own_team(conn, *, as_of, season, swid, view: base.AsOfView = "historical") -> int:
    """The operator's own ``team_id``, matched on ``league_teams.primary_owner``.

    Package-layer glue so the CLI stays thin (Rule 3) and so no module hard-codes
    a team number: the id is a fact about the synced league, not a constant. The
    SWID is a private credential — it is matched, never echoed into output or logs
    (Rule 5). Raises rather than defaulting, because silently valuing SOMEONE
    ELSE'S roster is a wrong answer the operator cannot smell (Rule 6).
    """
    token = str(swid or "").strip().upper()
    rows = get_team_state(conn, as_of=as_of, season=season, view=view)
    matches = [r["team_id"] for r in rows
               if str(r["primary_owner"] or "").strip().upper() == token]
    if len(matches) == 1:
        return int(matches[0])
    if not rows:
        raise OwnTeamUnresolved(
            f"no league_teams rows at as_of={as_of}: run `ziggurat league sync` first, "
            "or pass --team explicitly."
        )
    if not matches:
        raise OwnTeamUnresolved(
            "the SWID in .env does not match any team owner in this league snapshot; "
            "pass --team explicitly."
        )
    raise OwnTeamUnresolved(
        f"the SWID in .env matches {len(matches)} teams; pass --team explicitly."
    )


def get_player_state(
    conn,
    *,
    as_of,
    season,
    espn_player_id=None,
    on_team_id=None,
    position=None,
    free_agents_only: bool = False,
    view: base.AsOfView = "historical",
):
    """Player league-state rows as of a date — the roster AND the free-agent pool.

    ``free_agents_only`` filters to unrostered players; ``on_team_id`` filters to
    one team's roster. Both read the same snapshot table, which is the point of
    storing the whole universe (design §3.1).
    """
    clauses, params = ["t.season = :season"], {"season": season}
    if espn_player_id is not None:
        clauses.append("t.espn_player_id = :pid")
        params["pid"] = str(espn_player_id)
    if on_team_id is not None:
        clauses.append("t.on_team_id = :team")
        params["team"] = on_team_id
    if position is not None:
        clauses.append("t.position = :position")
        params["position"] = position
    if free_agents_only:
        clauses.append("t.on_team_id IS NULL")
    return base.select_as_of(
        conn, "league_player_state", as_of=as_of, key_cols=["season", "espn_player_id"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


def get_free_agents(conn, *, as_of, season, position=None, view: base.AsOfView = "historical"):
    """The free-agent pool as of a date, richest-owned first (ESPN's own
    ``percentOwned`` — the consensus proxy the waiver module is trying to beat)."""
    rows = get_player_state(
        conn, as_of=as_of, season=season, position=position,
        free_agents_only=True, view=view,
    )
    return sorted(rows, key=lambda r: (r["percent_owned"] is None, -(r["percent_owned"] or 0.0)))


def who_held(conn, *, as_of, season, espn_player_id, view: base.AsOfView = "historical"):
    """The league team id holding this player as of a date, or None if he was a
    free agent (or not yet observed) then.

    Answers item 3.1's done-when directly. Correctness depends on the
    whole-universe snapshot: a dropped player has a real ``on_team_id`` NULL row,
    so this returns None rather than the stale pre-drop holder.
    """
    rows = get_player_state(
        conn, as_of=as_of, season=season, espn_player_id=espn_player_id, view=view,
    )
    return rows[0]["on_team_id"] if rows else None


def holder_timeline(conn, *, season, espn_player_id, since=None, until=None) -> list[dict]:
    """Collapse the snapshot series into ``{from, to, team_id}`` holding segments.

    Deliberately NOT an as-of accessor: it reports the OBSERVED history of a
    single player across snapshots (what the sync recorded and when), which is
    what "who held X during week N" and later opponent-behaviour profiling
    actually want. It reads raw rows in retrieval order and never reconstructs a
    past information set, so there is no knowledge-time gate to forget.
    """
    clauses, params = ["season = :season", "espn_player_id = :pid"], {
        "season": season, "pid": str(espn_player_id),
    }
    if since is not None:
        clauses.append("retrieved_as_of >= :since")
        params["since"] = normalize_as_of(since).isoformat()
    if until is not None:
        clauses.append("retrieved_as_of <= :until")
        params["until"] = normalize_as_of(until).isoformat()
    rows = conn.execute(
        f"SELECT retrieved_as_of, on_team_id FROM league_player_state "
        f"WHERE {' AND '.join(clauses)} ORDER BY retrieved_as_of",
        params,
    ).fetchall()

    segments: list[dict] = []
    for row in rows:
        day, team = row["retrieved_as_of"], row["on_team_id"]
        if segments and segments[-1]["team_id"] == team:
            segments[-1]["to"] = day
            segments[-1]["snapshots"] += 1
        else:
            segments.append({"from": day, "to": day, "team_id": team, "snapshots": 1})
    return segments


# Availability boundary for the injury-transition detector. A player is
# "unavailable" for the coming week when ESPN tags him OUT or INJURY_RESERVE;
# ACTIVE / QUESTIONABLE / None are all "expected to play" (QUESTIONABLE is a
# weekly game designation that resolves week to week and is not a shock on its
# own — see marginal.AvailabilityModel). The set is the same one
# marginal.DEFAULT_AVAILABILITY.hard_out_statuses uses, kept local so a caller
# does not have to import the valuation layer to read league state.
HARD_OUT_STATUSES = frozenset({"OUT", "INJURY_RESERVE"})


def injury_transitions(conn, *, as_of, season, view: base.AsOfView = "historical") -> list[dict]:
    """Injury-status transitions across consecutive daily snapshots, up to ``as_of``.

    The **LIVE in-season** injury-shock source for item 3.3's candidate generator
    (the historical/backtest source is nflverse ``injuries.get_injuries``, which
    for 2025+ has no mid-week lead time — see IMPLEMENTATION_PLAN.md 3.3). This
    diffs each player's ``injury_status`` between consecutive
    ``league_player_state`` snapshot days and emits a transition when he crosses
    the availability boundary:

      * ``ruled_out``  — ACTIVE/QUESTIONABLE/None -> OUT/INJURY_RESERVE
        (the opportunity shock: his role just vacated);
      * ``cleared``    — OUT/INJURY_RESERVE -> ACTIVE/QUESTIONABLE/None
        (he is back; the vacancy closed).

    A move WITHIN a class (ACTIVE -> QUESTIONABLE, OUT -> INJURY_RESERVE) is not a
    boundary crossing and is not reported — it does not change availability.

    Each transition carries ``espn_player_id``, ``gsis_id``, ``player``,
    ``position``, ``pro_team``, ``on_team_id`` (the roster holding him at the
    to-snapshot), ``from_status``, ``to_status``, ``direction`` (ruled_out /
    cleared), and ``became_knowable`` (the snapshot day the new status first
    appeared — the day it became actionable).

    Leakage-safe (Rule 1): only snapshots knowable at ``as_of`` are scanned
    (``knowable_as_of`` gate under every view; ``retrieved_as_of`` gate too under
    ``historical``), so a transition can never surface before the day it was
    observed. Modeled on ``who_held`` / ``holder_timeline``; ``view`` is threaded
    so the same code serves the live ``historical`` path and any latest-truth
    replay. Returned ascending by ``(became_knowable, player)``.

    HONESTY: as of 2026-07-26 only three PRE-SEASON snapshots exist
    (2026-07-24/25/26), every player a free agent with ``injury_status`` unchanged,
    so this helper produces NOTHING against real data yet. It is UNIT-TESTED on
    synthetic snapshots and can only be smoke-tested live until the season starts.
    That is expected and fine — it is wired now so the waiver cadence has it on
    day one.
    """
    cutoff = normalize_as_of(as_of).isoformat()
    # The knowledge gate. league_player_state stamps knowable_as_of ==
    # retrieved_as_of (a live mutable snapshot), so both gates coincide here; we
    # write both to match select_as_of's semantics under each view rather than
    # relying on that coincidence.
    gate = "knowable_as_of <= :as_of"
    if view == "historical":
        gate += " AND retrieved_as_of <= :as_of"
    rows = conn.execute(
        f"SELECT retrieved_as_of, espn_player_id, gsis_id, player, position, "
        f"       pro_team, on_team_id, injury_status "
        f"FROM league_player_state "
        f"WHERE season = :season AND {gate} "
        f"ORDER BY espn_player_id, retrieved_as_of",
        {"season": season, "as_of": cutoff},
    ).fetchall()

    def _class(status):
        return "OUT" if (status or "").strip().upper() in HARD_OUT_STATUSES else "IN"

    out: list[dict] = []
    prev_by_player: dict[str, sqlite3.Row] = {}
    for row in rows:
        pid = row["espn_player_id"]
        prev = prev_by_player.get(pid)
        prev_by_player[pid] = row
        if prev is None:
            continue
        before, after = _class(prev["injury_status"]), _class(row["injury_status"])
        if before == after:
            continue  # no availability-boundary crossing
        out.append({
            "season": season,
            "espn_player_id": pid,
            "gsis_id": row["gsis_id"],
            "player": row["player"],
            "position": row["position"],
            "pro_team": row["pro_team"],
            "on_team_id": row["on_team_id"],
            "from_status": prev["injury_status"],
            "to_status": row["injury_status"],
            "direction": "ruled_out" if after == "OUT" else "cleared",
            "became_knowable": row["retrieved_as_of"],
        })
    out.sort(key=lambda t: (t["became_knowable"], t["player"] or "", t["espn_player_id"]))
    return out


def get_matchups(conn, *, as_of, season, week=None, view: base.AsOfView = "historical"):
    """League matchups as of a date. Pairings are knowable pre-season; a read
    before a week is played correctly returns that week with zero points."""
    clauses, params = ["t.season = :season"], {"season": season}
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    return base.select_as_of(
        conn, "league_matchups", as_of=as_of, key_cols=["season", "week", "home_team_id"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


def get_transactions(conn, *, as_of, season, team_id=None, week=None,
                     view: base.AsOfView = "historical"):
    """Transaction events knowable as of a date (gated on the EVENT's own date,
    not the pull day — see ``ingest_transactions``)."""
    clauses, params = ["t.season = :season"], {"season": season}
    if team_id is not None:
        clauses.append("t.team_id = :team")
        params["team"] = team_id
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    return base.select_as_of(
        conn, "league_transactions", as_of=as_of, key_cols=["season", "transaction_key"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


# ------------------------------------------------------------- run logging
#
# Operational metadata, NOT facts about the world: no as_of, never through
# select_as_of. This exists because league history is perishable — a cron that
# quietly stopped firing must be VISIBLE, since the days it missed can never be
# recovered from ESPN (design §1).


def start_run(conn, *, season: int, retrieved_as_of, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO league_sync_runs (season, retrieved_as_of, started_at, status) "
        "VALUES (?, ?, ?, 'running')",
        (season, normalize_as_of(retrieved_as_of).isoformat(), started_at),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, *, status: str, finished_at: str, counts=None, error=None) -> None:
    counts = counts or {}
    conn.execute(
        """
        UPDATE league_sync_runs
        SET status = ?, finished_at = ?, teams = ?, players = ?, matchups = ?,
            transactions = ?, reconcile_conflicts = ?, error = ?
        WHERE run_id = ?
        """,
        (
            status, finished_at, counts.get("teams"), counts.get("players"),
            counts.get("matchups"), counts.get("transactions"), counts.get("conflicts"),
            error, run_id,
        ),
    )
    conn.commit()


def last_run(conn, *, season: int, status: str | None = "ok"):
    """The most recent run row (by default the most recent SUCCESSFUL one).

    Ordered by the monotonic ``run_id``, NOT ``started_at``: run timestamps are
    second-resolution, and two runs inside one second (a retry, or a manual run
    racing the timer) made "the last run" ambiguous — so a failure could be
    reported as the earlier success.
    """
    if status is None:
        return conn.execute(
            "SELECT * FROM league_sync_runs WHERE season = ? ORDER BY run_id DESC LIMIT 1",
            (season,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM league_sync_runs WHERE season = ? AND status = ? "
        "ORDER BY run_id DESC LIMIT 1",
        (season, status),
    ).fetchone()


def snapshot_days(conn, *, season: int) -> list[str]:
    """Every day that actually has a stored player-state snapshot, ascending."""
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT retrieved_as_of FROM league_player_state "
            "WHERE season = ? ORDER BY retrieved_as_of",
            (season,),
        )
    ]


def backfilled_days(conn, *, season: int, marker: str) -> list[str]:
    """Days whose snapshot was deliberately back-stamped rather than captured live.

    Such a day LOOKS like coverage to ``snapshot_gaps`` (it has rows), but its
    contents are today's ESPN state wearing a past date. The status report has to
    keep saying so, or the one honest signal about missing history quietly turns
    into a lie.
    """
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT retrieved_as_of FROM league_sync_runs "
            "WHERE season = ? AND error LIKE ? ORDER BY retrieved_as_of",
            (season, f"%{marker}%"),
        )
    ]


def snapshot_gaps(conn, *, season: int, through) -> list[str]:
    """Days between the first snapshot and ``through`` with NO snapshot at all.

    The honest report of what league history is permanently missing. ``through``
    is passed in (never an implicit "now") so the caller states the horizon.
    """
    days = snapshot_days(conn, season=season)
    if not days:
        return []
    start = date.fromisoformat(days[0])
    end = normalize_as_of(through)
    have = set(days)
    missing, cursor = [], start
    while cursor <= end:
        if cursor.isoformat() not in have:
            missing.append(cursor.isoformat())
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return missing


def sqlite_json(value) -> str:
    """Small helper for CLI/debug dumps of a row (kept here so the CLI stays thin)."""
    if isinstance(value, sqlite3.Row):
        value = dict(value)
    return json.dumps(value, indent=2, sort_keys=True, default=str)


# -------------------------------------------------------------- formatters
#
# Display lives in the package, not the CLI (rule 3: commands parse, call, print).
# The operator is a football novice (rule 6), so these print the evidence — slot,
# ownership, injury — not just names.

_SLOT_ORDER = ("QB", "RB", "WR", "TE", "FLEX", "D/ST", "K", "BE", "IR")


def format_roster(rows) -> str:
    """One team's roster, starters first, then bench, then IR."""
    if not rows:
        return "(no roster rows at this as_of — has the draft happened, and has a sync run?)"

    def sort_key(row):
        slot = row["lineup_slot"]
        rank = _SLOT_ORDER.index(slot) if slot in _SLOT_ORDER else len(_SLOT_ORDER)
        return (rank, row["position"] or "", -(row["percent_owned"] or 0.0))

    out = [f"{'SLOT':<5} {'POS':<5} {'PLAYER':<24} {'NFL':<4} {'%OWN':>6}  {'ACQ':<6} INJ"]
    for row in sorted(rows, key=sort_key):
        own = "" if row["percent_owned"] is None else f"{row['percent_owned']:.1f}"
        inj = row["injury_status"] or ""
        out.append(
            f"{(row['lineup_slot'] or '?'):<5} {(row['position'] or ''):<5} "
            f"{(row['player'] or '?')[:24]:<24} {(row['pro_team'] or ''):<4} {own:>6}  "
            f"{(row['acquisition_type'] or ''):<6} {inj}"
        )
    return "\n".join(out)


def format_free_agents(rows, *, limit: int = 40) -> str:
    """The free-agent pool, most-owned first (the consensus-ranked shelf)."""
    if not rows:
        return "(no free agents at this as_of — has a sync run?)"
    out = [f"{'POS':<5} {'PLAYER':<24} {'NFL':<4} {'%OWN':>6} {'%CHG':>7} {'STATUS':<10} INJ"]
    for row in list(rows)[:limit]:
        own = "" if row["percent_owned"] is None else f"{row['percent_owned']:.1f}"
        chg = "" if row["percent_change"] is None else f"{row['percent_change']:+.2f}"
        out.append(
            f"{(row['position'] or ''):<5} {(row['player'] or '?')[:24]:<24} "
            f"{(row['pro_team'] or ''):<4} {own:>6} {chg:>7} "
            f"{(row['roster_status'] or ''):<10} {row['injury_status'] or ''}"
        )
    if len(rows) > limit:
        out.append(f"… {len(rows) - limit} more")
    return "\n".join(out)


def format_timeline(segments, *, player_label: str = "") -> str:
    """Observed holding segments — the readable answer to 'who held X, when'."""
    if not segments:
        return f"(no observed snapshots for {player_label or 'this player'})"
    out = [f"holding timeline{f' — {player_label}' if player_label else ''}:"]
    for seg in segments:
        holder = f"team {seg['team_id']}" if seg["team_id"] is not None else "FREE AGENT"
        span = seg["from"] if seg["from"] == seg["to"] else f"{seg['from']} → {seg['to']}"
        out.append(f"  {span:<26} {holder}  ({seg['snapshots']} snapshot(s))")
    return "\n".join(out)
