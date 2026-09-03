-- db/migrations/014_league_ground_truth.sql
-- Item 3.8 wave A: ESPN's own roster-mechanics ground truth, per snapshot.
--
-- WHY. Item 3.4 shipped roster legality on two LABELLED HYPOTHESES because no
-- roster existed to test against, and item 1.5 left acquisitionBudget=100
-- open ("season cap or inert default?"). The draft happened 2026-08-31 and
-- three waiver claims cleared on 2026-09-02, so ESPN finally answers. This
-- migration stores the ANSWERS AS FACTS, per snapshot, rather than letting them
-- live in a prose note that nothing re-checks.
--
-- WHAT THE PLAN'S PREMISE GOT WRONG (measured 2026-09-02, live, n=1,036).
-- IMPLEMENTATION_PLAN §3.8 wave A said "ingest eligibleSlots for machine truth".
-- eligibleSlots carries NO IR information: slot 21 (IR) is listed for 1,036 of
-- 1,036 players in the universe, alongside slot 20 (BE) — it is a POSITIONAL
-- map, not a per-player IR gate. It is therefore deliberately NOT stored: a
-- column that means nothing is worse than an absent one, because the next reader
-- assumes it means something (that is the whole shape of the mistake this item
-- exists to correct).
--
-- WHAT IS STORED INSTEAD, and what each column MEANS:
--
-- 1. league_player_state.injured — ESPN's OWN per-player boolean. Measured
--    2026-09-02 over the whole universe: injured == true on EXACTLY the
--    {OUT, INJURY_RESERVE} designations, 0 exceptions (ACTIVE/false 808,
--    QUESTIONABLE/false 121, INJURY_RESERVE/true 54, None/false 42, OUT/true 9,
--    DAY_TO_DAY/false 1, SUSPENSION/false 1). That is TWO ENCODINGS OF ONE FACT
--    agreeing — it is NOT evidence about what ESPN's IR SLOT gates on, which
--    remains unobserved (0 of 10 rosters have used the IR slot). The comparison
--    is re-run on every snapshot by `ziggurat league ir-check`.
-- 2. league_player_state.droppable — ESPN's undroppable list is ON in this
--    league (isUsingUndroppableList: true) and 19 of 1,036 players read
--    droppable:false, two of them on the operator's own roster. ESPN REFUSES a
--    drop of those in the app, so a recommendation naming one is an instruction
--    the operator cannot follow. core/marginal.py fences the swap matrix on it.
-- 3. league_player_state.entry_injury_status — the ROSTER ENTRY's own
--    injuryStatus, a DIFFERENT field from player.injuryStatus (observed NORMAL
--    on all 160 rostered players). Stored raw, interpreted nowhere: it is the
--    single most plausible ALTERNATIVE gate for the IR slot, and the first IR
--    occupant this league ever produces settles both candidates at once.
-- 4. league_settings — one row per (season, retrieved_as_of), the league's own
--    rulebook as ESPN serves it. The point is not that it changes; it is that a
--    mid-season change (a commissioner switching FAAB on) would otherwise be
--    invisible. 2025's and 2026's acquisitionSettings and positionLimits are
--    byte-identical, so this is cheap-and-currently-inert, not speculative.
--
-- NULL MEANS NOT CAPTURED — NEVER FALSE, NEVER ZERO. Every league_player_state
-- row retrieved before this migration (41 snapshot days, 2026-07-24..2026-09-02)
-- predates the three columns and reads NULL in all three. A consumer that reads
-- `injured` with `or 0` turns "we do not know" into "he is healthy" — so
-- core/waiver.py's _ir_status branches on `is None` and falls back to the
-- designation PROXY with a per-player note. NULL also happens on a POST-014
-- snapshot: a rostered player absent from the pool response is synthesized from
-- the roster view alone (state.ingest_player_state) and carries no pool fields,
-- which is why the IR report measures per-row COVERAGE rather than a boolean
-- about the migration.
--
-- THE COUNTER FINDING, recorded here so nothing ever reads that column wrong:
-- league_teams.acquisitions IS NOT A COUNT OF CLAIMS WON. Team 10 won three
-- waiver claims in the 2026-09-02 batch and its transactionCounter read
-- {acquisitions: 0, drops: 3}; a second team read {acquisitions: 0, drops: 1,
-- moveToActive: 5}. Nothing in ziggurat/ reads it today (grep verified) and the
-- same sentence is on the mapper at league/state.py's map_team.
--
-- WAIVER TIMING, measured (M6): this league's batches run 00:01-01:13 PACIFIC
-- (n=29: 28 in the 2025 waiverProcessStatus map + the 2026-09-02 batch at
-- 00:06:12 PDT), i.e. the "3-4 AM" figure in circulation was the EASTERN one.
-- waiver_process_hour is stored RAW because its unit is unknown: ESPN serves 11
-- and no batch has ever run at 11 in any zone we can name. Claiming a unit we
-- have not measured is what this item exists to stop.
--
-- PRIVACY (rule 5). settings_json holds the raw settings object, which contains
-- the league NAME (allowed) and no owner identity; scheduleSettings.divisions[].name
-- is commissioner-authored free text that can encode a colleague and is STRIPPED
-- before storage. status_json is the `status` object only — never `members`,
-- never `teams`. Both live in this gitignored database and must never be copied
-- into a committed fixture; every settings fixture in tests/ is SYNTHETIC.
--
-- ADD COLUMN is metadata-only in SQLite (rehearsed on a copy of the 813 MB live
-- database); the columns append after knowable_as_of, and every accessor reads
-- by name (select_as_of projects "*" into sqlite3.Row), so no positional
-- consumer exists.
--
-- No BEGIN/COMMIT and no schema_version write here: store.apply_schema wraps
-- this script in a transaction and stamps version 14.

ALTER TABLE league_player_state ADD COLUMN injured              INTEGER;
ALTER TABLE league_player_state ADD COLUMN droppable            INTEGER;
ALTER TABLE league_player_state ADD COLUMN entry_injury_status  TEXT;

-- One row per (season, snapshot day): the league rulebook as ESPN served it.
-- Stamped like league_teams — knowable_as_of = retrieved_as_of = the pull day,
-- because this is a live mutable snapshot with no history endpoint behind it.
CREATE TABLE IF NOT EXISTS league_settings (
    season                     INTEGER NOT NULL,
    scoring_period             INTEGER,
    -- acquisition / waiver rules. acquisition_limit and matchup_acquisition_limit
    -- are ESPN's -1 sentinel for "no limit"; is_using_acquisition_budget FALSE is
    -- what makes acquisition_budget=100 INERT (the item-1.5 open question).
    acquisition_type           TEXT,
    is_using_acquisition_budget INTEGER,
    acquisition_budget         REAL,
    acquisition_limit          INTEGER,
    matchup_acquisition_limit  REAL,
    waiver_hours               INTEGER,
    waiver_process_days        TEXT,     -- JSON array of day names
    waiver_process_hour        INTEGER,  -- RAW: unit unknown, see the header
    waiver_order_reset         INTEGER,
    move_limit                 INTEGER,  -- rosterSettings.moveLimit (-1 = none)
    is_using_undroppable_list  INTEGER,
    -- roster shape. lineup_slot_counts is keyed by DECODED slot label
    -- (state.LINEUP_SLOTS); position_limits by DECODED position label
    -- (espn_ranks.DEFPOS), with -1/0 preserved verbatim, never normalised away.
    lineup_slot_counts         TEXT,     -- JSON {slot label: count}
    position_limits            TEXT,     -- JSON {position label: limit}
    -- trades
    trade_deadline             TEXT,     -- LOCAL ISO, full precision
    trade_deadline_epoch_ms    INTEGER,  -- ESPN's own value, unconverted
    trade_veto_votes_required  INTEGER,
    trade_revision_hours       INTEGER,
    -- schedule / status
    matchup_period_count       INTEGER,
    playoff_team_count         INTEGER,
    playoff_seeding_rule       TEXT,
    final_scoring_period       INTEGER,
    waiver_last_execution      TEXT,     -- LOCAL ISO of status.waiverLastExecutionDate
    waiver_process_status      TEXT,     -- JSON {batch timestamp: claims processed}
    settings_json              TEXT,     -- raw settings, divisions[].name stripped
    status_json                TEXT,     -- raw status only (never members/teams)
    retrieved_as_of            TEXT NOT NULL,
    knowable_as_of             TEXT NOT NULL,   -- = retrieved_as_of
    PRIMARY KEY (season, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_league_settings_lookup
    ON league_settings (season, knowable_as_of, retrieved_as_of);
