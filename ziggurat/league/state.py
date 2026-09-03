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
from dataclasses import dataclass, replace
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
    # item 3.8a — ESPN's own machine truth (migration 014). Every one of these MUST
    # stay in this tuple: base.upsert takes its column list from rows[0] ONLY, and
    # ingest_player_state writes a HETEROGENEOUS batch (mapped pool rows plus
    # roster-only rows synthesized from a different literal). The
    # `setdefault(col, None)` loop over THIS tuple is the only thing that makes them
    # uniform, so a forgotten entry kills the whole day's snapshot, not one column.
    # Pinned by test_player_columns_match_the_table.
    "injured", "droppable", "entry_injury_status",
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
_SETTINGS_COLUMNS = (
    "season", "scoring_period", "acquisition_type", "is_using_acquisition_budget",
    "acquisition_budget", "acquisition_limit", "matchup_acquisition_limit",
    "waiver_hours", "waiver_process_days", "waiver_process_hour",
    "waiver_order_reset", "move_limit", "is_using_undroppable_list",
    "lineup_slot_counts", "position_limits", "trade_deadline",
    "trade_deadline_epoch_ms", "trade_veto_votes_required", "trade_revision_hours",
    "matchup_period_count", "playoff_team_count", "playoff_seeding_rule",
    "final_scoring_period", "waiver_last_execution", "waiver_process_status",
    "settings_json", "status_json",
)
# The settings columns stored as JSON text — decoded on the way back out so a
# caller never re-implements json.loads (and never forgets to).
_SETTINGS_JSON_COLUMNS = (
    "waiver_process_days", "lineup_slot_counts", "position_limits",
    "waiver_process_status", "settings_json", "status_json",
)

# ---------------------------------------------------------- IR ground truth (3.8a)
#
# The IR-slot designation set. It lives HERE, not in core/waiver.py where item 3.4
# put it, because it is an ESPN-FACING definition and ``league/`` cannot import
# ``core/`` — ``sync.format_status`` needs the standing check and would otherwise
# have no way to reach it. ``core.waiver`` re-exports the name.
#
# Deliberately {OUT, INJURY_RESERVE} and nothing else:
#   * NOT HARD_OUT_STATUSES (an availability boundary, a different concept);
#   * NOT marginal's hard-out set (it includes SUSPENSION/NOT_ACTIVE — not IR
#     designations);
#   * DOUBTFUL / PUP / NFI remain UNOBSERVED in this league and are treated as
#     INELIGIBLE. They are not settled — they are watched (see
#     OBSERVED_INJURY_STATUSES).
IR_ELIGIBLE_STATUSES = frozenset({"OUT", "INJURY_RESERVE"})

# Every injury designation this league had actually served as of the baseline day.
# Measured 2026-09-02 across the whole 1,036-player universe. ``""`` is ESPN
# serving no designation at all (the ten D/ST rows and 42 pool rows).
#
# WHY A BASELINE AND NOT JUST A CROSS-TAB: comparing ESPN's ``injured`` flag with
# IR_ELIGIBLE_STATUSES compares two encodings of ONE fact, and they agreed on all
# 1,036 rows with zero exceptions — so "divergences: none" prints every day and
# trains the operator to ignore the report (the same crying-wolf argument item
# 3.1b used to refuse a second gap report). The event this check actually exists
# for is a status that has NEVER been seen appearing — DOUBTFUL the first Friday
# of the season, PUP, NFI — because that is the case the shipped rule has never
# been tested on. THAT is what fires a note.
#
# THE BASELINE IS A FLOOR, NOT THE ANSWER (audit fix). It records what this league
# had served before ``injury_status`` capture was auditable; the set actually
# compared against is this floor UNIONED with every designation the league's own
# stored history holds on an EARLIER snapshot day. Without that union the watch
# LATCHES: the first DOUBTFUL would put the identical headline on `league status`
# and on every waiver plan for the rest of the season, which is the crying-wolf
# failure the paragraph above exists to prevent.
IR_RULE_BASELINE_DATE = "2026-09-02"
OBSERVED_INJURY_STATUSES = frozenset({
    "", "ACTIVE", "DAY_TO_DAY", "INJURY_RESERVE", "OUT", "QUESTIONABLE", "SUSPENSION",
})

# The day ESPN's own ``isUsingAcquisitionBudget`` was read and item 1.5's open
# question was closed. A HISTORICAL FACT that never moves. Deliberately NOT
# IR_RULE_BASELINE_DATE, which is the injury watch's floor and IS expected to be
# re-measured — the two share a date by coincidence, not by cause, and borrowing
# one for the other silently re-dates an unrelated observation (audit fix).
ACQUISITION_SETTLED_DATE = "2026-09-02"

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


def _flag(value) -> int | None:
    """ESPN boolean -> 1/0, or None when ESPN did not serve the key at all.

    The None is load-bearing (item 3.8a): ``int(bool(None))`` is 0, which would
    turn "we do not know whether he is injured" into "he is healthy" and "we do
    not know whether ESPN will let you drop him" into "drop away". Every consumer
    of ``injured``/``droppable`` branches on ``is None`` before it branches on the
    value.
    """
    return None if value is None else int(bool(value))


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
        # NOT A COUNT OF CLAIMS WON (measured 2026-09-02, item 3.8a): team 10 won
        # THREE waiver claims in that morning's batch and ESPN's counter still read
        # {acquisitions: 0, drops: 3}; a second team read {acquisitions: 0,
        # drops: 1, moveToActive: 5}. Whatever ESPN increments it on, it is not a
        # won claim in this league. Nothing in ziggurat/ reads it (grep verified) —
        # if you are about to be the first, measure it again first.
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
                # The ROSTER ENTRY's own injuryStatus — a DIFFERENT field from
                # player.injuryStatus, which map_player_entry reads (item 3.8a).
                # Stored raw and interpreted NOWHERE: it read NORMAL on all 160
                # rostered players on 2026-09-02, and it is the most plausible
                # ALTERNATIVE gate for ESPN's IR slot, which no roster in this
                # league has yet occupied. The first occupant settles both
                # candidates at once — see ir_rule_check.
                "entry_injury_status": entry.get("injuryStatus"),
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
        # ESPN's OWN flags (item 3.8a, migration 014). NULL means NOT CAPTURED,
        # never False: an absent key on a pre-014 snapshot, or on a rostered player
        # missing from the pool response, must not read as "healthy" / "droppable".
        "injured": _flag(player.get("injured")),
        "droppable": _flag(player.get("droppable")),
        "entry_injury_status": None,   # overlaid from roster_index when rostered
        "percent_owned": ownership.get("percentOwned"),
        "percent_started": ownership.get("percentStarted"),
        "percent_change": ownership.get("percentChange"),
        "scoring_period": scoring_period,
    }


def _strip_division_names(settings: dict) -> dict:
    """A shallow copy of ``settings`` with ``scheduleSettings.divisions[].name``
    removed (Rule 5).

    Division names are commissioner-authored free text and can encode a real
    colleague; nothing in scope reads them. Everything else in ``settings`` is
    league RULES plus the league name, which Rule 5 allows.
    """
    out = dict(settings)
    sched = out.get("scheduleSettings")
    if isinstance(sched, dict) and isinstance(sched.get("divisions"), list):
        sched = dict(sched)
        sched["divisions"] = [
            {k: v for k, v in (d or {}).items() if k != "name"} if isinstance(d, dict) else d
            for d in sched["divisions"]
        ]
        out["scheduleSettings"] = sched
    return out


def map_settings(payload: dict, *, season: int, scoring_period=None) -> dict | None:
    """Map the ``settings`` + ``status`` halves of a league-state payload to a
    ``league_settings`` row, or None when ESPN served no ``settings`` block
    (item 3.8a).

    THE LEAGUE'S OWN RULEBOOK, stored per snapshot. Not because it changes — 2025
    and 2026 serve byte-identical ``acquisitionSettings`` and ``positionLimits`` —
    but because a mid-season change (a commissioner switching FAAB on, a
    position limit tightened) is otherwise invisible to every module that prices
    a claim. The row is stamped like ``league_teams``:
    ``knowable_as_of == retrieved_as_of``, a live mutable snapshot.

    DECODING DISCIPLINE. ``lineupSlotCounts`` carries slot id 22 with count 0,
    which ``LINEUP_SLOTS`` does not map — zero-count slots are dropped BEFORE
    decoding so ``decode_slot`` does not log a false "unknown slot" warning every
    run. ``positionLimits`` carries 18 keys of which 12 are outside ``DEFPOS``,
    and id 0 carries a limit of **0**, not −1 — so "unmapped means unlimited" is a
    false shortcut and only the six DEFPOS ids are decoded. The raw map survives
    in ``settings_json`` either way. ESPN's −1 sentinel is preserved verbatim
    everywhere; interpreting it is the CONSUMER's job (see
    ``marginal.effective_position_caps``, where ``min(3, -1)`` would have refused
    every add at every position).

    ``waiver_process_hour`` is stored RAW: ESPN serves 11 and this league's
    batches run 00:01-01:13 Pacific, so the unit is unknown and is not guessed.
    """
    settings = payload.get("settings")
    if not isinstance(settings, dict) or not settings:
        return None
    acq = settings.get("acquisitionSettings") or {}
    roster = settings.get("rosterSettings") or {}
    trade = settings.get("tradeSettings") or {}
    sched = settings.get("scheduleSettings") or {}
    status = payload.get("status") or {}

    slot_counts = {}
    for raw_id, count in (roster.get("lineupSlotCounts") or {}).items():
        if not count:
            continue  # a zero-count slot is not part of this league's shape
        try:
            slot_counts[decode_slot(int(raw_id))] = int(count)
        except (TypeError, ValueError):
            continue

    limits = {}
    for raw_id, limit in (roster.get("positionLimits") or {}).items():
        try:
            label = DEFPOS.get(int(raw_id))
        except (TypeError, ValueError):
            continue
        if label is not None:
            limits[label] = limit

    deadline_ms = trade.get("deadlineDate")
    return {
        "season": season,
        "scoring_period": scoring_period,
        "acquisition_type": acq.get("acquisitionType"),
        "is_using_acquisition_budget": _flag(acq.get("isUsingAcquisitionBudget")),
        "acquisition_budget": acq.get("acquisitionBudget"),
        "acquisition_limit": acq.get("acquisitionLimit"),
        "matchup_acquisition_limit": acq.get("matchupAcquisitionLimit"),
        "waiver_hours": acq.get("waiverHours"),
        "waiver_process_days": json.dumps(acq.get("waiverProcessDays") or []),
        "waiver_process_hour": acq.get("waiverProcessHour"),
        "waiver_order_reset": _flag(acq.get("waiverOrderReset")),
        "move_limit": roster.get("moveLimit"),
        # NOT at the settings top level — ESPN nests it under rosterSettings
        # (measured 2026-09-02; the top-level read stored NULL and the page
        # printed "off" for a list that is ON and refuses two of our own drops).
        # The top-level fallback is kept in case ESPN ever moves it back.
        "is_using_undroppable_list": _flag(
            roster.get("isUsingUndroppableList",
                       settings.get("isUsingUndroppableList"))),
        "lineup_slot_counts": json.dumps(slot_counts, sort_keys=True),
        "position_limits": json.dumps(limits, sort_keys=True),
        "trade_deadline": _epoch_ms_to_iso(deadline_ms),
        "trade_deadline_epoch_ms": deadline_ms,
        "trade_veto_votes_required": trade.get("vetoVotesRequired"),
        "trade_revision_hours": trade.get("revisionHours"),
        "matchup_period_count": sched.get("matchupPeriodCount"),
        "playoff_team_count": sched.get("playoffTeamCount"),
        "playoff_seeding_rule": sched.get("playoffSeedingRule"),
        "final_scoring_period": status.get("finalScoringPeriod"),
        "waiver_last_execution": _epoch_ms_to_iso(status.get("waiverLastExecutionDate")),
        "waiver_process_status": json.dumps(status.get("waiverProcessStatus") or {},
                                            sort_keys=True),
        "settings_json": json.dumps(_strip_division_names(settings), sort_keys=True),
        # `status` ONLY. NEVER `members` or `teams` — those carry owner identity
        # (Rule 5) and live at the payload top level, outside this block.
        "status_json": json.dumps(status, sort_keys=True),
    }


def validate_settings_row(row: dict | None) -> str | None:
    """None when the settings row is fit to store, else the warning that must
    degrade the run to ``partial`` (item 3.8a).

    A MISSING settings block loses nothing perishable — the day's delete only runs
    when there is a row to write. A PARTIALLY DECODED one is the real hazard:
    it looks healthy, it is stored as fact, and it silently changes decisions
    through the league position-limit fence and the FAAB verdict. So the row is
    refused rather than half-written, and silence is never success
    (``sync.run_sync`` turns any warning into a ``partial`` run).
    """
    if row is None:
        return (
            "ESPN served no `settings` block — no league_settings row was written "
            "for this snapshot (teams, matchups and players were). `ziggurat league "
            "settings` will read the previous snapshot instead."
        )
    if not json.loads(row["lineup_slot_counts"] or "{}"):
        return "league settings decoded to ZERO lineup slots — row refused, not stored"
    if row["acquisition_type"] is None:
        return "league settings carry no acquisitionType — row refused, not stored"
    limits = json.loads(row["position_limits"] or "{}")
    missing = sorted(set(DEFPOS.values()) - set(limits))
    if missing:
        return (
            f"league settings decoded position limits for only {len(limits)} of "
            f"{len(DEFPOS)} positions (missing {', '.join(missing)}) — row refused, "
            f"not stored"
        )
    return None


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
    """Persist the teams + matchups + settings halves of one league-state snapshot.

    Idempotent by the item-2.1 pattern: the ``(season, retrieved_as_of)``
    partition is deleted and rewritten, so re-running a day (or a cron firing
    twice) replaces rather than duplicates.

    The settings row (item 3.8a) rides the SAME transaction as teams and matchups.
    It is written only when it VALIDATES: a missing or half-decoded settings block
    returns ``settings_warning`` and writes nothing there, while teams and matchups
    still land — the snapshot is perishable and must not be lost to a rulebook
    read. ``sync.run_sync`` turns that warning into a ``partial`` run.
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

    settings_row = map_settings(payload, season=season, scoring_period=scoring_period)
    settings_warning = validate_settings_row(settings_row)
    settings_rows = [] if settings_warning else [settings_row]

    # ONE transaction for the whole delete-then-rewrite. Two transactions (the
    # default per-call commit in base.upsert) leave a window where the day is
    # deleted and not yet replaced; a crash there loses it permanently.
    with conn:
        for rows, table, columns in (
            (teams, "league_teams", _TEAM_COLUMNS),
            (kept, "league_matchups", _MATCHUP_COLUMNS),
            (settings_rows, "league_settings", _SETTINGS_COLUMNS),
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

    if settings_warning:
        logger.warning("league settings: %s", settings_warning)
    return {
        "teams": len(teams),
        "matchups": len(kept),
        "settings": len(settings_rows),
        "settings_warning": settings_warning,
    }


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


def get_league_settings(conn, *, as_of, season, view: base.AsOfView = "historical"):
    """The league's own rulebook as of a date, or None before the first capture
    (item 3.8a).

    Resolved through ``base.select_as_of`` on ``key_cols=["season"]`` like every
    other accessor — never a hand-rolled ``MAX(retrieved_as_of)``, and never with
    ``retrieved_as_of`` in the key columns (that returns every snapshot ever
    taken instead of the newest one at or before ``as_of``).

    JSON columns come back DECODED, so no caller re-implements ``json.loads`` —
    or forgets to and compares a Python dict against a string.
    """
    rows = base.select_as_of(
        conn, "league_settings", as_of=as_of, key_cols=["season"],
        extra_where="t.season = :season", params={"season": season}, view=view,
    )
    if not rows:
        return None
    row = dict(rows[0])
    for col in _SETTINGS_JSON_COLUMNS:
        raw = row.get(col)
        if isinstance(raw, str):
            try:
                row[col] = json.loads(raw)
            except ValueError:
                logger.warning("league settings: %s did not decode as JSON", col)
                row[col] = None
    return row


def league_position_limits(conn, *, as_of, season, view: base.AsOfView = "historical"):
    """``{position label: limit}`` from the league's own ``positionLimits``, or
    None (item 3.8a).

    Returns the DEFPOS labels ("D/ST", not "DST") — canonicalising to the
    valuation layer's vocabulary happens on the CORE side, because ``league/``
    cannot import ``core/``. See ``marginal.effective_position_caps``, which is
    the ONE place a league limit is turned into a cap.

    Returns None (never a PARTIAL dict) when there is no settings row or the
    decoded map does not cover all six league positions: a partial map read as
    "no limit at the missing positions" is indistinguishable from a real absence,
    and the consumer must be able to tell.

    ESPN's sentinels are preserved VERBATIM. −1 is "unlimited" and 0 is a real
    stored value (id 0 carries 0), so this function does not interpret them —
    ``effective_position_caps`` does, once.
    """
    settings = get_league_settings(conn, as_of=as_of, season=season, view=view)
    if settings is None:
        return None
    limits = settings.get("position_limits")
    if not isinstance(limits, dict):
        return None
    if set(limits) < set(DEFPOS.values()):
        return None
    return {str(k): int(v) for k, v in limits.items() if v is not None}


def latest_snapshot_day(conn, *, as_of, season, view: base.AsOfView = "historical"):
    """The newest ``league_player_state`` snapshot day knowable at ``as_of``.

    The gate is hand-rolled rather than delegated because this is an aggregate
    over the whole table, not a per-key resolve — the same shape
    ``injury_transitions`` uses, and the same two gates: ``knowable_as_of`` under
    every view, plus ``retrieved_as_of`` under ``historical``.
    """
    cutoff = normalize_as_of(as_of).isoformat()
    gate = "knowable_as_of <= :as_of"
    if view == "historical":
        gate += " AND retrieved_as_of <= :as_of"
    return conn.execute(
        f"SELECT MAX(retrieved_as_of) FROM league_player_state "
        f"WHERE season = :season AND {gate}",
        {"season": season, "as_of": cutoff},
    ).fetchone()[0]


def _snapshot_gate(as_of, view: base.AsOfView) -> tuple[str, str]:
    """The two as-of gates every hand-rolled aggregate here must carry.

    ``knowable_as_of`` under every view, plus ``retrieved_as_of`` under
    ``historical``. Factored out so a new aggregate cannot invent its own gate.
    """
    gate = "knowable_as_of <= :as_of"
    if view == "historical":
        gate += " AND retrieved_as_of <= :as_of"
    return gate, normalize_as_of(as_of).isoformat()


def previous_snapshot_day(conn, *, as_of, season, day,
                          view: base.AsOfView = "historical"):
    """The newest snapshot day STRICTLY BEFORE ``day``, or None.

    What makes "first ever seen" a transition rather than a standing state: an IR
    occupant who was already there yesterday is not news, and a report that says
    he is every day is a report the operator stops reading.
    """
    if day is None:
        return None
    gate, cutoff = _snapshot_gate(as_of, view)
    return conn.execute(
        f"SELECT MAX(retrieved_as_of) FROM league_player_state "
        f"WHERE season = :season AND retrieved_as_of < :day AND {gate}",
        {"season": season, "as_of": cutoff, "day": day},
    ).fetchone()[0]


def designations_seen_before(conn, *, as_of, season, day,
                             view: base.AsOfView = "historical") -> set[str]:
    """Every injury designation this league served on a snapshot BEFORE ``day``.

    Normalised exactly as ``ir_rule_check`` normalises the current snapshot's
    tokens (strip + upper, NULL -> ""), or a whitespace/case difference would make
    an already-seen designation read as new forever.

    Hand-rolled rather than ``select_as_of`` on purpose: ``select_as_of`` resolves
    the newest row PER KEY, which is precisely the history this needs to keep.
    """
    if day is None:
        return set()
    gate, cutoff = _snapshot_gate(as_of, view)
    rows = conn.execute(
        f"SELECT DISTINCT UPPER(TRIM(COALESCE(injury_status, ''))) AS tok "
        f"FROM league_player_state "
        f"WHERE season = :season AND retrieved_as_of < :day AND {gate}",
        {"season": season, "as_of": cutoff, "day": day},
    ).fetchall()
    return {str(r["tok"]) for r in rows}


def ir_occupant_ids_on(conn, *, as_of, season, day,
                       view: base.AsOfView = "historical") -> set[str]:
    """The ESPN ids sitting in an IR lineup slot on ONE named snapshot day.

    Day-scoped and raw, not ``get_player_state``: that accessor resolves the
    newest row per player at ``as_of``, which is the wrong question when the point
    is what a PARTICULAR earlier day held.
    """
    if day is None:
        return set()
    gate, cutoff = _snapshot_gate(as_of, view)
    rows = conn.execute(
        f"SELECT espn_player_id FROM league_player_state "
        f"WHERE season = :season AND retrieved_as_of = :day AND {gate} "
        f"AND UPPER(TRIM(COALESCE(lineup_slot, ''))) = 'IR'",
        {"season": season, "as_of": cutoff, "day": day},
    ).fetchall()
    return {str(r["espn_player_id"]) for r in rows}


def injured_flag_crosstab(conn, *, as_of, season, rostered_only: bool = False,
                          view: base.AsOfView = "historical") -> list[dict]:
    """``injury_status x injured x entry_injury_status`` counts over the newest
    snapshot at or before ``as_of`` (item 3.8a).

    PURE DATA — it counts, it does not judge. ``league/`` cannot import ``core/``,
    so the rule comparison lives one level up in ``ir_rule_check``.

    ``rostered_only`` matters and the caller must state it: the UNIVERSE cross-tab
    is n≈1,036 with 63 injured rows, while the ROSTERED one is n=160 with exactly
    ONE (every INJURY_RESERVE player in this league is unrostered). A cross-tab
    that did not say which population it ran over would read as near-vacuous in
    one case and strong in the other, off the same call.
    """
    day = latest_snapshot_day(conn, as_of=as_of, season=season, view=view)
    if day is None:
        return []
    clause = " AND on_team_id IS NOT NULL" if rostered_only else ""
    rows = conn.execute(
        f"SELECT injury_status, injured, entry_injury_status, COUNT(*) AS n "
        f"FROM league_player_state "
        f"WHERE season = :season AND retrieved_as_of = :day{clause} "
        f"GROUP BY injury_status, injured, entry_injury_status "
        f"ORDER BY n DESC, injury_status",
        {"season": season, "day": day},
    ).fetchall()
    return [dict(r) for r in rows]


@dataclass(frozen=True)
class IRRuleReport:
    """What ESPN's own flags say about this system's IR rule, on ONE snapshot
    (item 3.8a).

    THREE SURFACES, because only one of them can ever fire on its own:

    1. ``universe`` / ``rostered`` — the ``injury_status x injured`` cross-tabs,
       each with its own n. Cheap, and the thing a reader wants to see once.
       On its own it is NEAR-VACUOUS: ``injured`` and ``injury_status`` are two
       encodings of ONE fact and have agreed on every row ever measured, so
       ``divergences`` printing "none" every day would train the operator to skip
       the report.
    2. ``new_statuses`` — the WATCH, and the surface this report exists for. Any
       designation outside ``OBSERVED_INJURY_STATUSES`` (DOUBTFUL, PUP, NFI...)
       appearing for the first time, with the ``injured`` value ESPN gave it and
       how the shipped rule treats it. Those three are the ones the rule has
       never been tested on.
    3. ``ir_occupants`` — every IR-slot occupant LEAGUE-WIDE, with the roster
       entry's own ``injuryStatus`` and the holding team's transaction lock. Zero
       have ever been observed (0 of 10 rosters, 2026-09-02), which is exactly why
       the IR-SLOT mechanism is still UNVERIFIED. The first occupant whose
       ``injured`` flag is 0 is the event that settles it.

    ``coverage`` is the fraction of ROSTERED rows carrying a non-NULL ``injured``.
    It is a per-row fraction, not a boolean about migration 014: a rostered player
    absent from ESPN's pool response is synthesized from the roster view alone and
    carries NULL flags on a post-014 snapshot too.

    ``has_news`` is the whole point of the operator-attention contract: the report
    is rendered on demand, but ``league status`` and ``ziggurat waivers`` say
    NOTHING about it unless something here actually changed.
    """

    season: int
    as_of: str
    snapshot_date: str | None
    universe: tuple[dict, ...]
    universe_n: int
    rostered: tuple[dict, ...]
    rostered_n: int
    divergences: tuple[dict, ...]
    new_statuses: tuple[dict, ...]
    ir_occupants: tuple[dict, ...]
    coverage: float | None
    has_news: bool
    # How many UNIVERSE rows actually carried a non-NULL ``injured`` — i.e. how
    # many comparisons the divergence check could make. Zero is NOT "no
    # divergence" (audit fix): on any pre-014 snapshot no comparison is possible
    # at all, and reporting that as agreement is a positive claim about a flag
    # nobody captured.
    compared_n: int = 0
    # Same fraction as ``coverage`` for ESPN's ``droppable`` flag. Both keys come
    # from the same pool entry so they are NULL together in every case the live
    # system produces, but a NULL there silently disables the UNDROPPABLE fence
    # and the operator is owed BOTH consequences of the one gap (audit fix).
    droppable_coverage: float | None = None
    # Coverage restricted to the reader's OWN roster, when a caller names one. A
    # flag hole on another manager's bench cannot change anything on the
    # operator's waiver page and must not interrupt him about it.
    own_coverage: float | None = None
    own_team_id: int | None = None
    # IR-slot occupants present on THIS snapshot and absent from the previous
    # one — the transition, not the standing state. A permanently-on line about a
    # player who has sat on IR for six weeks names no action.
    new_occupants: tuple[dict, ...] = ()
    # True when this snapshot predates ESPN-flag capture. Such a gap is
    # PERMANENT (ESPN serves no historical league state), so it names no remedy
    # and is never news.
    pre_baseline: bool = False

    @property
    def headline(self) -> str:
        """The ONE line ``league status`` / a waiver plan prints when there IS news.

        Each bit carries the remedy that actually fixes IT: a coverage gap is
        repaired by a fresh PULL, not by re-reading the same report.
        """
        bits: list[str] = []
        remedies: list[str] = []

        def remedy(cmd: str) -> None:
            if cmd not in remedies:
                remedies.append(cmd)

        if self.snapshot_date is None:
            return "IR RULE CHECK: no league snapshot at this as-of — nothing to check."
        if self.divergences:
            bits.append(
                f"{len(self.divergences)} injury designation(s) where ESPN's own "
                f"`injured` flag DISAGREES with this system's IR-eligible set "
                f"({', '.join(sorted(d['injury_status'] or '(none)' for d in self.divergences))})"
            )
            remedy("`ziggurat league ir-check`")
        if self.new_statuses:
            bits.append(
                f"{len(self.new_statuses)} injury designation(s) never seen in this "
                f"league before ("
                + ", ".join(
                    f"{d['injury_status'] or '(none)'} x{d['n']}, ESPN injured="
                    f"{'yes' if d['injured'] else 'no' if d['injured'] is not None else 'not captured'}"
                    f" -> we treat him as "
                    f"{'IR-ELIGIBLE' if d['rule_eligible'] else 'IR-INELIGIBLE'}"
                    for d in self.new_statuses
                )
                + ")"
            )
            remedy("`ziggurat league ir-check`")
        flagged = [o for o in self.ir_occupants if o.get("injured") == 0]
        if flagged:
            bits.append(
                f"{len(flagged)} IR-slot occupant(s) whose ESPN `injured` flag is "
                f"FALSE — the event that settles the IR-slot mechanism "
                f"({', '.join(str(o['player']) for o in flagged)})"
            )
            remedy("`ziggurat league ir-check`")
        unflagged_new = [o for o in self.new_occupants if o.get("injured") != 0]
        if unflagged_new:
            bits.append(
                f"{len(unflagged_new)} IR-slot occupant(s) appeared for the first time "
                f"({', '.join(str(o['player'] or o['espn_player_id']) for o in unflagged_new)}) "
                f"— this league had never used the slot, so what ESPN accepted there is "
                f"the evidence the IR-slot mechanism has been waiting for"
            )
            remedy("`ziggurat league ir-check`")
        gap = self._coverage_gap()
        if gap is not None:
            pct, scope = gap
            bits.append(
                f"ESPN's `injured` and `droppable` flags are missing on {pct:.0f}% of "
                f"{scope} in the {self.snapshot_date} snapshot — IR eligibility falls "
                f"back to the injury-tag proxy for those rows AND the UNDROPPABLE check "
                f"did not run for them, so verify any drop in the app"
            )
            remedy("`ziggurat league sync` (a fresh pull is what fills the flags), then "
                   "`ziggurat league ir-check`")
        if not bits:
            if self.ir_occupants:
                return (f"IR RULE CHECK: no divergence; {len(self.ir_occupants)} IR-slot "
                        f"occupant(s) present, ESPN's flags agree.")
            return "IR RULE CHECK: clean."
        return "IR RULE CHECK: " + "; ".join(bits) + " — run " + "; ".join(remedies) + "."

    def _coverage_gap(self) -> tuple[float, str] | None:
        """``(missing %, population)`` when a flag gap is NEWS, else None.

        A gap is news only when it is BOTH repairable and capable of changing an
        answer the reader will act on: a pre-baseline snapshot can never be
        re-flagged (ESPN serves no historical league state), and a hole confined
        to another manager's bench decides nothing on the operator's own page.
        """
        if self.pre_baseline:
            return None
        # An IR-slot occupant with no captured flag is the one row where the gap
        # DECIDES something — his eligibility, and therefore roster legality — so
        # it speaks whoever holds him.
        blind = [o for o in self.ir_occupants if o.get("injured") is None]
        if blind:
            return (100.0 * len(blind) / len(self.ir_occupants), "IR-slot occupants")
        if self.own_team_id is not None:
            if self.own_coverage is not None and self.own_coverage < 1.0:
                return ((1 - self.own_coverage) * 100, "YOUR rostered players")
            return None
        if self.coverage is not None and self.coverage < 1.0:
            return ((1 - self.coverage) * 100, "rostered players league-wide")
        return None


def ir_rule_check(conn, *, as_of, season, own_team_id: int | None = None,
                  view: base.AsOfView = "historical") -> IRRuleReport:
    """Re-run item 3.8a's IR ground-truth comparison against one snapshot.

    Rule 1: ``as_of`` keyword-only, ``view`` threaded, ``historical`` default —
    it reads ``league_player_state`` and ``league_teams`` through the accessors
    above and invents no gate of its own.

    WHAT IT DOES AND DOES NOT SETTLE. It settles that ESPN's own ``injured``
    boolean still marks exactly the ``IR_ELIGIBLE_STATUSES`` designations. It
    does NOT settle what ESPN's IR SLOT accepts — no roster in this league has
    used it — which is why ``ir_occupants`` is reported with an explicit zero
    rather than as silence, and why ``core.waiver.IR_FIX_MODEL_LABEL`` still
    carries the word UNVERIFIED.

    ``own_team_id`` narrows the COVERAGE half of ``has_news`` to the reader's own
    roster. A flag hole on a rival's bench is real league-health information (and
    ``league status``, which names no team, still reports it), but it can decide
    nothing on the operator's waiver page and must not interrupt him there.
    """
    day = latest_snapshot_day(conn, as_of=as_of, season=season, view=view)
    universe = injured_flag_crosstab(conn, as_of=as_of, season=season, view=view)
    rostered = injured_flag_crosstab(conn, as_of=as_of, season=season,
                                     rostered_only=True, view=view)

    def token(status) -> str:
        return str(status or "").strip().upper()

    # "Never seen before" is a fact about THIS LEAGUE'S OWN HISTORY, not about a
    # source constant. The constant is only the floor for days that predate the
    # capture; without the union a designation stays "new" on every snapshot it
    # appears in, forever (audit fix).
    observed = OBSERVED_INJURY_STATUSES | designations_seen_before(
        conn, as_of=as_of, season=season, day=day, view=view)

    # Keyed by DESIGNATION, exactly like the watch below it. The cross-tab groups
    # by (tag, flag, roster-entry status), so one diverging designation split
    # across two entry statuses used to report as TWO designations with each n
    # undercounted (audit fix).
    divergences: dict[str, dict] = {}
    new_statuses: dict[str, dict] = {}
    for row in universe:
        tok = token(row["injury_status"])
        rule_eligible = tok in IR_ELIGIBLE_STATUSES
        if row["injured"] is not None and bool(row["injured"]) != rule_eligible:
            seen = divergences.setdefault(tok, {
                "injury_status": row["injury_status"],
                "injured": row["injured"],
                "n": 0,
                "rule_eligible": rule_eligible,
            })
            seen["n"] += row["n"]
        if tok not in observed:
            seen = new_statuses.setdefault(tok, {
                "injury_status": row["injury_status"],
                "injured": row["injured"],
                "n": 0,
                "rule_eligible": rule_eligible,
            })
            # A designation split across rows can carry DIFFERENT flags; asserting
            # one of them over the summed n would be false for the rest.
            if seen["injured"] != row["injured"]:
                seen["injured"] = None
            seen["n"] += row["n"]

    rostered_n = sum(r["n"] for r in rostered)
    captured = sum(r["n"] for r in rostered if r["injured"] is not None)
    coverage = (captured / rostered_n) if rostered_n else None
    compared_n = sum(r["n"] for r in universe if r["injured"] is not None)

    # ONE universe read, shared by the coverage fractions and the occupant scan —
    # this runs on every waiver plan and every `league status`, so a second full
    # resolve would be a cost the report cannot justify.
    all_rows = [dict(r) for r in get_player_state(
        conn, as_of=as_of, season=season, view=view)]
    roster_rows = [r for r in all_rows if r["on_team_id"] is not None]
    drop_captured = sum(1 for r in roster_rows if r.get("droppable") is not None)
    droppable_coverage = (drop_captured / len(roster_rows)) if roster_rows else None
    own_rows = [r for r in roster_rows if r["on_team_id"] == own_team_id] \
        if own_team_id is not None else []
    own_coverage = (
        sum(1 for r in own_rows if r.get("injured") is not None) / len(own_rows)
    ) if own_rows else None

    locked = {
        t["team_id"]: t["is_transaction_locked"]
        for t in get_team_state(conn, as_of=as_of, season=season, view=view)
    }
    occupants = [
        {
            "team_id": r["on_team_id"],
            "player": r["player"],
            "position": r["position"],
            "espn_player_id": r["espn_player_id"],
            "injury_status": r["injury_status"],
            "injured": r["injured"],
            "entry_injury_status": r["entry_injury_status"],
            "lineup_slot": r["lineup_slot"],
            "team_transaction_locked": locked.get(r["on_team_id"]),
        }
        for r in all_rows
        if str(r["lineup_slot"] or "").strip().upper() == "IR"
    ]
    occupants.sort(key=lambda o: (o["team_id"] or 0, str(o["player"] or "")))

    # A TRANSITION, not a state: an occupant already present on the previous
    # snapshot is not news today.
    prior_day = previous_snapshot_day(conn, as_of=as_of, season=season, day=day, view=view)
    prior_ids = ir_occupant_ids_on(conn, as_of=as_of, season=season, day=prior_day, view=view)
    # With NO previous snapshot there is no basis for "first seen" at all, and an
    # absence is only a fact when you know it is one — so nothing is reported as a
    # transition rather than every occupant being called new on a one-day database.
    new_occupants = () if prior_day is None else tuple(
        o for o in occupants if str(o["espn_player_id"]) not in prior_ids
    )

    pre_baseline = bool(day is not None and str(day) < IR_RULE_BASELINE_DATE)
    report = IRRuleReport(
        season=int(season),
        as_of=normalize_as_of(as_of).isoformat(),
        snapshot_date=day,
        universe=tuple(universe),
        universe_n=sum(r["n"] for r in universe),
        rostered=tuple(rostered),
        rostered_n=rostered_n,
        divergences=tuple(divergences.values()),
        new_statuses=tuple(new_statuses.values()),
        ir_occupants=tuple(occupants),
        coverage=coverage,
        has_news=False,
        compared_n=compared_n,
        droppable_coverage=droppable_coverage,
        own_coverage=own_coverage,
        own_team_id=own_team_id,
        new_occupants=new_occupants,
        pre_baseline=pre_baseline,
    )
    has_news = bool(
        divergences
        or new_statuses
        or any(o["injured"] == 0 for o in occupants)
        or new_occupants
        # The coverage half is gated on being repairable AND capable of deciding
        # something for THIS reader — see IRRuleReport._coverage_gap.
        or report._coverage_gap() is not None
    )
    return replace(report, has_news=has_news)


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


def format_ir_rule_report(report: IRRuleReport) -> str:
    """Render the IR ground-truth report (item 3.8a) — evidence, not a verdict."""
    out = [
        f"IR rule check — season {report.season}, as of {report.as_of}"
        + (f" (snapshot {report.snapshot_date})" if report.snapshot_date else "")
    ]
    if report.snapshot_date is None:
        out.append("  no league_player_state snapshot at this as_of — nothing to check.")
        return "\n".join(out)

    def crosstab(rows, label, n) -> list[str]:
        lines = [f"  {label} (n={n}):",
                 f"    {'INJURY TAG':<18} {'ESPN injured':<13} {'ROSTER ENTRY':<14} "
                 f"{'N':>5}  our rule"]
        for r in rows:
            tok = str(r["injury_status"] or "").strip().upper()
            flag = ("yes" if r["injured"] else "no") if r["injured"] is not None \
                else "NOT CAPTURED"
            lines.append(
                f"    {(r['injury_status'] or '(none)'):<18} {flag:<13} "
                f"{(r['entry_injury_status'] or '-'):<14} {r['n']:>5}  "
                f"{'IR-ELIGIBLE' if tok in IR_ELIGIBLE_STATUSES else 'IR-ineligible'}"
            )
        return lines

    out.extend(crosstab(report.universe, "whole player universe", report.universe_n))
    out.append("")
    out.extend(crosstab(report.rostered, "rostered players only", report.rostered_n))
    out.append("")

    if report.divergences:
        out.append("  DIVERGENCES — ESPN's own flag disagrees with our IR-eligible set:")
        for d in report.divergences:
            out.append(
                f"    {d['injury_status'] or '(none)'}: ESPN injured="
                f"{'yes' if d['injured'] else 'no'}, we treat him as "
                f"{'IR-ELIGIBLE' if d['rule_eligible'] else 'IR-ineligible'} "
                f"({d['n']} player(s))"
            )
    elif report.compared_n == 0:
        # An absence of difference is not an absence of signal. On a snapshot
        # where the flag was never captured, NOTHING was compared — reporting
        # that as agreement is a positive claim about a column that is all NULL.
        out.append(
            f"  divergences: NOT MEASURED — ESPN's `injured` flag is absent on ALL "
            f"{report.universe_n} row(s) of this snapshot"
            + (f" (it was first captured {IR_RULE_BASELINE_DATE}, and ESPN serves no "
               f"historical league state, so this day can never be re-flagged)"
               if report.pre_baseline else "")
            + ". No comparison was possible here; the injury-tag PROXY decided every "
              "IR call on this day."
        )
    else:
        out.append(
            f"  divergences: none — compared on {report.compared_n} of "
            f"{report.universe_n} row(s) (the ones that carry the flag), and on every "
            f"one ESPN's `injured` flag marks exactly "
            f"{'/'.join(sorted(IR_ELIGIBLE_STATUSES))}. NOTE this is two encodings "
            f"of ONE fact agreeing; it is not evidence about what ESPN's IR SLOT "
            f"accepts."
        )

    if report.new_statuses:
        out.append(
            f"  NEW DESIGNATIONS never seen in this league before "
            f"{IR_RULE_BASELINE_DATE} — the case the shipped rule has NOT been tested on:"
        )
        for d in report.new_statuses:
            flag = ("yes" if d["injured"] else "no") if d["injured"] is not None \
                else "NOT CAPTURED"
            out.append(
                f"    {d['injury_status'] or '(none)'} x{d['n']}: ESPN injured={flag}"
                f" -> we treat him as "
                f"{'IR-ELIGIBLE' if d['rule_eligible'] else 'IR-ineligible'}"
            )
    else:
        out.append(
            f"  new designations: none — every injury tag on this snapshot is already "
            f"in the set this league has served before (the {IR_RULE_BASELINE_DATE} "
            f"baseline, plus everything its own earlier snapshots hold). DOUBTFUL / "
            f"PUP / NFI remain UNOBSERVED and are treated as IR-INELIGIBLE; this watch "
            f"is what settles them, and it speaks ONCE, on the day one first appears."
        )

    if report.ir_occupants:
        out.append(f"  IR-slot occupants league-wide ({len(report.ir_occupants)}):")
        for o in report.ir_occupants:
            flag = ("yes" if o["injured"] else "no") if o["injured"] is not None \
                else "NOT CAPTURED"
            out.append(
                f"    team {o['team_id']}  {o['player'] or o['espn_player_id']} "
                f"({o['position'] or '?'})  tag={o['injury_status'] or '(none)'}  "
                f"ESPN injured={flag}  roster-entry status="
                f"{o['entry_injury_status'] or '-'}  team locked="
                f"{'yes' if o['team_transaction_locked'] else 'no'}"
            )
    else:
        out.append(
            "  IR-slot occupants league-wide: 0 — no roster here has ever used the "
            "slot, so what ESPN does with an occupant is UNTESTED in this league."
        )
        # Naming the action is the whole point of a report built to settle a
        # question. Waiting for an occupant is not the only way, and it is the
        # slow one (audit fix). The negative half was observed 2026-09-03 on the
        # ESPN WEBSITE (the operator has no app): a player's MOVE button lists
        # only the moves ESPN accepts, and it offered IR to nobody on a roster
        # with no OUT/IR player. The ask below is what is STILL open.
        out.append(
            "    observed 2026-09-03 (ESPN website): a player's MOVE button lists only "
            "the moves ESPN accepts, and it offered IR to NOBODY on a roster with no "
            "OUT/INJURY_RESERVE player — an ineligible body cannot be put on IR. "
            "Still open, and you do NOT have to wait for an occupant: the first time "
            "an OUT/INJURY_RESERVE player is on YOUR roster, open his MOVE menu and see "
            "whether IR is offered (~30 s). Whether ESPN blocks transactions on an "
            "oversized roster still needs a real occupant who heals. Record the answer "
            "in IMPLEMENTATION_PLAN.md §3.8."
        )

    # ALWAYS printed, including the pre-draft n=0 case: a suppressed coverage line
    # doubles as a clean bill of health for a measurement nobody made.
    if report.coverage is None:
        out.append(
            "  ESPN flag coverage on rostered players: n/a — no rostered players at "
            "this as-of (pre-draft, or no snapshot rows for a roster)"
        )
    else:
        drop_pct = ("n/a" if report.droppable_coverage is None
                    else f"{report.droppable_coverage:.0%}")
        out.append(
            f"  ESPN flag coverage on rostered players: injured "
            f"{report.coverage:.0%}, droppable {drop_pct} "
            f"({report.rostered_n} rostered)"
        )
        if report.coverage < 1.0 or (report.droppable_coverage or 1.0) < 1.0:
            out.append(
                "    the uncovered rows fall back to the injury-tag PROXY for IR "
                "eligibility, and the UNDROPPABLE check did not run for them at all — "
                "verify any drop in the app"
                + (" (this snapshot predates flag capture, so it can never be "
                   "re-flagged)" if report.pre_baseline else
                   " (re-run `ziggurat league sync`)")
            )
    return "\n".join(out)


def settings_verdicts(settings, *, conn=None, as_of=None, view: base.AsOfView = "historical"):
    """The DECISIONS the settings row supports, as sentences (item 3.8a, Rule 3).

    Logic, not printing — which is why it lives here and not in the CLI:

    * the FAAB verdict is a rule over three fields, and getting it wrong in either
      direction changes what the waiver module owes (a budget it must track vs a
      number it must ignore);
    * "the deadline is the Wednesday before Week 13" is a JOIN against the
      ``schedules`` table, not arithmetic on the date — 2026's Week 1 opens on a
      WEDNESDAY, so counting sevens from a Thursday is wrong by a day.

    ``conn``/``as_of`` are optional so a caller with no database (a test, a
    formatter over a dict) still gets the FAAB half.
    """
    if not settings:
        return []
    out: list[str] = []
    verdict = faab_verdict(settings)
    out.append(verdict if verdict is not None else _inert_budget_sentence(settings))
    season_limit = settings.get("acquisition_limit")
    weekly_limit = settings.get("matchup_acquisition_limit")
    move_limit = settings.get("move_limit")
    caps: list[str] = []
    unlimited: list[str] = []
    uncaptured: list[str] = []
    for name, value, phrase in (
        ("acquisitionLimit", season_limit, "season acquisitions capped at {}"),
        ("matchupAcquisitionLimit", weekly_limit, "per-matchup acquisitions capped at {}"),
        ("moveLimit", move_limit, "lineup moves capped at {}"),
    ):
        if value is None:
            uncaptured.append(name)
        elif value >= 0:
            caps.append(phrase.format(f"{value:g}"))
        else:
            unlimited.append(name)
    if caps:
        out.append("TRANSACTION CAPS: " + "; ".join(caps))
    # NULL is NOT CAPTURED, and it must never be reported as ESPN's -1 sentinel —
    # that would turn "we did not read it" into "no cap binds" (audit fix).
    if uncaptured and not caps:
        out.append(
            f"NO transaction cap could be read: {', '.join(uncaptured)} "
            f"{'was' if len(uncaptured) == 1 else 'were'} NOT CAPTURED in this "
            f"snapshot"
            + (f" ({', '.join(unlimited)} read as ESPN's -1 'unlimited' sentinel)"
               if unlimited else "")
            + " — do NOT read this as 'no cap binds'."
        )
    elif uncaptured:
        out[-1] += (
            f"; {', '.join(uncaptured)} NOT CAPTURED in this snapshot — no claim is "
            f"made about {'it' if len(uncaptured) == 1 else 'them'}"
        )
    elif not caps:
        out.append(
            "no season or weekly acquisition cap, and no lineup-move cap "
            "(acquisitionLimit / matchupAcquisitionLimit / moveLimit are all ESPN's -1 "
            "'unlimited' sentinel) — the waiver module owes NO spend or count accounting."
        )
    deadline = settings.get("trade_deadline")
    if deadline and conn is not None and as_of is not None:
        week = _week_of_first_gameday_after(
            conn, deadline, season=settings.get("season"), as_of=as_of, view=view,
        )
        # The QUANTIFIER matters and used to be inverted: the derived number is
        # the FIRST week starting after the deadline, and a deadline precedes
        # every week after it, so "the LAST week it precedes" named the wrong end
        # of the season (audit fix). State the consequence instead of the label.
        out.append(
            f"trade deadline {deadline} (local) — that is the last moment a trade can "
            f"be made all season. The FIRST NFL games after it are week {week}'s, so a "
            f"trade agreed before then can still change your week-{week} lineup; "
            f"nothing after."
            if week is not None else
            f"trade deadline {deadline} (local) — no REG gameday after it is knowable "
            f"at this as_of, so the first week after it could not be derived"
        )
    elif deadline:
        out.append(f"trade deadline {deadline} (local time).")
    return out


def faab_verdict(settings) -> str | None:
    """The ONE sentence about whether a claim costs money, or None when it is not
    worth interrupting anyone with (item 3.8a, audit fix).

    Returns a sentence whenever FAAB is ON or the flag was NOT CAPTURED — both of
    which change what the waiver module owes and neither of which anything else in
    the system reads. Returns None when ESPN says the budget is inert, which is
    the measured state of this league and is not news on a daily surface;
    ``settings_verdicts`` prints that case itself.
    """
    if not settings:
        return None
    using_budget = settings.get("is_using_acquisition_budget")
    budget = settings.get("acquisition_budget")
    if using_budget is None:
        return (
            "FAAB status NOT CAPTURED in this snapshot — ESPN served no "
            "`isUsingAcquisitionBudget`, so whether a claim costs bid dollars is "
            "UNKNOWN here. It read FALSE on "
            f"{ACQUISITION_SETTLED_DATE}, but that is a past snapshot, not this one: "
            "check the app before you trust the claim list."
        )
    if using_budget:
        return (
            f"FAAB IS ON: acquisition budget {budget} is a REAL spend cap "
            f"(isUsingAcquisitionBudget=true). Every claim costs bid dollars — the "
            f"waiver module does NOT track a budget and this needs re-reading before "
            f"you trust its claim list."
        )
    return None


def _inert_budget_sentence(settings) -> str:
    return (
        f"acquisition budget {settings.get('acquisition_budget')} is INERT: "
        f"isUsingAcquisitionBudget=false, so claims are free and non-FAAB. This is the "
        f"item-1.5 open question, closed by observation on {ACQUISITION_SETTLED_DATE}."
    )


def _week_of_first_gameday_after(conn, deadline_iso, *, season, as_of,
                                 view: base.AsOfView = "historical"):
    """The REG week of the first NFL gameday strictly after ``deadline_iso``.

    A JOIN, deliberately, not week arithmetic: 2026's Week 1 opens on a
    WEDNESDAY, so "the Thursday N weeks after the opener" is off by a day and the
    plan text that assumed it was wrong.
    """
    from ziggurat.data.nfl.schedules import get_schedule

    day = str(deadline_iso)[:10]
    rows = [
        r for r in get_schedule(conn, as_of=as_of, season=season, view=view)
        if r["game_type"] == "REG" and str(r["gameday"] or "") > day
    ]
    if not rows:
        return None
    return min(rows, key=lambda r: (str(r["gameday"]), r["week"]))["week"]


def format_settings(settings, *, verdicts=(), limits_note: str = "") -> str:
    """Render the decoded league settings (item 3.8a).

    DECODED FIELDS ONLY — never ``settings_json`` / ``status_json``. Those hold
    the raw ESPN blobs for a later reader (wave B diffs ``scoringSettings``
    against the item-1.1 fixture out of exactly this column) and printing them
    would put commissioner free text on a terminal Rule 5 has no way to police.
    """
    if not settings:
        return ("(no league_settings row at this as_of — run `ziggurat league sync`; "
                "settings capture started 2026-09-02, item 3.8a)")
    slots = settings.get("lineup_slot_counts") or {}
    limits = settings.get("position_limits") or {}
    days = settings.get("waiver_process_days") or []
    out = [
        f"league settings — season {settings.get('season')} "
        f"(snapshot {settings.get('retrieved_as_of')})",
        "",
        "  ACQUISITIONS",
    ]
    out.extend(f"    {line}" for line in verdicts)
    out.append(f"    acquisition type      : {settings.get('acquisition_type')}")
    out.append(f"    waiver process days   : {', '.join(days) or '(none)'}")
    out.append(f"    waiver claim window   : {settings.get('waiver_hours')} hours")
    out.append(
        f"    waiver order          : "
        f"{'resets weekly to inverse standings' if settings.get('waiver_order_reset') else 'rolling (move-to-back)'}"
    )
    out.append(
        f"    waiver processHour    : {settings.get('waiver_process_hour')} "
        f"(ESPN's raw value — UNIT UNKNOWN, and no batch has ever run at that hour "
        f"in any zone we can name; trust the observed time below, not this)"
    )
    out.append(
        f"    waivers last executed : "
        f"{settings.get('waiver_last_execution') or 'not captured'} (local) — this "
        f"league's batches run 00:01-01:13 PACIFIC (n=29 measured), so a claim must "
        f"be queued before ~23:59 PT to make that night's batch"
    )
    # NULL is NOT CAPTURED, and it must not print as "off" — that is the same
    # false-negative the `injured`/`droppable` NULL convention exists to stop.
    undroppable = settings.get("is_using_undroppable_list")
    out.append(
        f"    undroppable list      : "
        + ("ON — ESPN refuses a drop of a listed player (see the UNDROPPABLE tags "
           "on `ziggurat waivers`)" if undroppable
           else "off" if undroppable is not None
           else "NOT CAPTURED in this snapshot")
    )
    out.append("")
    out.append("  ROSTER")
    out.append(f"    lineup slots          : "
               + ", ".join(f"{k} {v}" for k, v in sorted(slots.items())))
    # NO UNMEASURED COMPARATIVE HERE (audit fix). This function cannot compare the
    # league's limits against the board's guard — ``league/`` may not import
    # ``core/`` — so it must not assert which is tighter. The caller passes the
    # measured sentence in (``marginal.describe_league_limits``), and the -1
    # legend prints only when a -1 is actually on the page: the renderer
    # substitutes the WORD "unlimited", so the sentinel is otherwise invisible.
    unlimited = any(v is not None and v < 0 for v in limits.values())
    out.append(
        "    league position limits: "
        + ", ".join(f"{k} {'unlimited' if v is not None and v < 0 else v}"
                    for k, v in sorted(limits.items()))
        + ("  (a position shown as 'unlimited' is ESPN's -1 sentinel)" if unlimited else "")
        + (f"  ({limits_note})" if limits_note else "")
    )
    out.append("")
    out.append("  SCHEDULE & TRADES")
    out.append(f"    matchup periods       : {settings.get('matchup_period_count')}")
    out.append(f"    playoff teams / seed  : {settings.get('playoff_team_count')} / "
               f"{settings.get('playoff_seeding_rule')}")
    out.append(f"    final scoring period  : {settings.get('final_scoring_period')}")
    out.append(f"    trade deadline        : {settings.get('trade_deadline')} (local)")
    out.append(f"    trade veto votes      : {settings.get('trade_veto_votes_required')} "
               f"(revision window {settings.get('trade_revision_hours')}h)")
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
