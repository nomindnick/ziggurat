-- db/migrations/005_league_state.sql
-- Item 3.1: league state — rosters, standings, matchups, transactions, FA pool.
--
-- THE CONSTRAINT THAT SHAPES THIS SCHEMA (recon 2026-07-24, probed live):
-- ESPN serves league state as a CURRENT SNAPSHOT ONLY. There is no historical
-- backfill of any kind — leagueHistory ignores scoringPeriodId on mRoster (all
-- weeks return the identical end-of-season roster), past-season box scores carry
-- no rosterForCurrentScoringPeriod, mTransactions2 has no transactions key, and
-- the activity feed 404s for a past season. So league history EXISTS ONLY
-- BECAUSE WE SNAPSHOT IT. Whatever a missed run fails to capture is gone
-- permanently; hence league_sync_runs (below) and `ziggurat league status`.
--
-- Stamping: league state is a live mutable snapshot, so
--   knowable_as_of = retrieved_as_of = the pull day
-- exactly as item 2.1 stamps espn_draft_ranks (design D8). The one exception is
-- league_transactions, which carries ESPN's own event timestamp and is stamped
-- knowable_as_of = date(processed_at or proposed_at).
--
-- Grain is a DAY: the last pull of a day replaces earlier pulls of that day
-- (delete-the-partition-then-insert, mirroring espn_ranks), which also makes a
-- re-run idempotent. Sub-day knowledge time is a deliberate cross-cutting
-- change to base.select_as_of, not a per-table hack (see that function's
-- GRANULARITY note and IMPLEMENTATION_PLAN 1.4 forward item 2).
--
-- PRIVACY (rule 5): league_teams.name / abbrev / primary_owner can identify
-- real colleagues. They live in this (gitignored) database and MUST NEVER be
-- copied into a committed fixture, test, or commit message.

-- One row per player per snapshot day, for the WHOLE universe (~1026/day).
--
-- Why the whole universe and not just rostered players: a DROP has to be
-- visible. select_as_of resolves the newest row per key at or before as_of, so
-- if only rostered players were written, the last "team 4 holds X" row would
-- stay newest forever after X was dropped and who_held would answer wrong for
-- the rest of the season. Writing every player every day makes a drop a real
-- row (on_team_id NULL) — which is simultaneously the free-agent pool
-- (WHERE on_team_id IS NULL). One table answers both halves of item 3.1.
--
-- percent_owned/started/change are ESPN's league-wide ownership series. They are
-- captured here because they arrive on the same HTTP response as the FA pool and
-- are point-in-time only (no history endpoint) — they are the live 2026 copy of
-- the market-consensus proxy SPEC goal 3 measures the edge against, and the
-- signal 3.3/4.2 must beat. Not capturing them would destroy them.
CREATE TABLE IF NOT EXISTS league_player_state (
    season            INTEGER NOT NULL,
    espn_player_id    TEXT NOT NULL,       -- ESPN player id as string (D/ST ids are negative)
    gsis_id           TEXT,                -- crosswalked at ingest; NULL for D/ST + unmatched
    player            TEXT,
    position          TEXT,                -- QB/RB/WR/TE/K/D/ST via espn_ranks.DEFPOS
    pro_team          TEXT,                -- normalized NFL abbr (base.TEAM_ALIASES)
    on_team_id        INTEGER,             -- league team holding the player; NULL = free agent
    roster_status     TEXT,                -- ESPN entry status: FREEAGENT / WAIVERS / ONTEAM
    lineup_slot       TEXT,                -- decoded slot when rostered (QB..FLEX/BE/IR)
    acquisition_type  TEXT,                -- DRAFT / ADD / TRADE when rostered
    acquisition_date  TEXT,                -- ISO date (from epoch ms) when present
    injury_status     TEXT,
    percent_owned     REAL,                -- league-wide ownership % (the consensus proxy)
    percent_started   REAL,
    percent_change    REAL,                -- ESPN's own w/w ownership delta
    scoring_period    INTEGER,             -- ESPN scoringPeriodId at snapshot time
    retrieved_as_of   TEXT NOT NULL,
    knowable_as_of    TEXT NOT NULL,       -- = retrieved_as_of (live mutable snapshot)
    PRIMARY KEY (season, espn_player_id, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_league_player_state_lookup
    ON league_player_state (season, espn_player_id, knowable_as_of, retrieved_as_of);
CREATE INDEX IF NOT EXISTS idx_league_player_state_holder
    ON league_player_state (season, on_team_id, knowable_as_of);

-- One row per team per snapshot: standings, waiver priority, transaction counters.
-- waiver_rank is the live inverse-standings priority item 3.4 plans claims against.
-- transaction counters are also how the 1.1 open question about acquisitionBudget=100
-- (inert default vs a real season-long cap) gets answered from observed behaviour.
CREATE TABLE IF NOT EXISTS league_teams (
    season             INTEGER NOT NULL,
    team_id            INTEGER NOT NULL,
    abbrev             TEXT,               -- LEAGUE-PRIVATE (rule 5) — never fixture this
    name               TEXT,               -- LEAGUE-PRIVATE (rule 5)
    primary_owner      TEXT,               -- LEAGUE-PRIVATE (rule 5) — ESPN owner GUID
    division_id        INTEGER,
    waiver_rank        INTEGER,            -- live waiver priority (1 = next claim wins)
    playoff_seed       INTEGER,
    wins               INTEGER,
    losses             INTEGER,
    ties               INTEGER,
    points_for         REAL,
    points_against     REAL,
    streak_length      INTEGER,
    streak_type        TEXT,
    acquisitions       INTEGER,
    drops              INTEGER,
    trades             INTEGER,
    moves_to_ir        INTEGER,
    moves_to_active    INTEGER,
    acquisition_budget_spent REAL,
    team_charges       REAL,
    is_transaction_locked INTEGER,
    scoring_period     INTEGER,
    retrieved_as_of    TEXT NOT NULL,
    knowable_as_of     TEXT NOT NULL,      -- = retrieved_as_of
    PRIMARY KEY (season, team_id, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_league_teams_lookup
    ON league_teams (season, team_id, knowable_as_of, retrieved_as_of);

-- One row per matchup per snapshot. The full regular-season schedule is knowable
-- pre-season (70 pairings exist today, pre-draft); scores fill in as weeks run.
-- Because an as-of read returns the newest snapshot <= as_of, a mid-season read
-- of a FUTURE week correctly sees the pairing with zero points — no leakage and
-- no special casing needed.
CREATE TABLE IF NOT EXISTS league_matchups (
    season            INTEGER NOT NULL,
    week              INTEGER NOT NULL,    -- matchupPeriodId
    home_team_id      INTEGER NOT NULL,
    away_team_id      INTEGER,             -- NULL only if ESPN ever emits a bye side
    home_points       REAL,
    away_points       REAL,
    home_games_played INTEGER,
    away_games_played INTEGER,
    winner            TEXT,                -- HOME / AWAY / TIE / UNDECIDED
    playoff_tier      TEXT,
    scoring_period    INTEGER,
    retrieved_as_of   TEXT NOT NULL,
    knowable_as_of    TEXT NOT NULL,       -- = retrieved_as_of
    PRIMARY KEY (season, week, home_team_id, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_league_matchups_lookup
    ON league_matchups (season, week, knowable_as_of, retrieved_as_of);

-- Best-effort event log. The ONLY intraday-accurate record in the system
-- (proposed_at/processed_at are full ISO timestamps), and the only table whose
-- knowable_as_of genuinely differs from retrieved_as_of.
--
-- May never populate: the 2025 feed is empty/404 and the 2026 feed currently
-- returns 200 with no transactions. Nothing depends on it — snapshot diffing of
-- league_player_state is the primary movement source; this only ADDS precision
-- (exact time, and waiver-vs-FCFS provenance) when ESPN cooperates.
--
-- WRITE-ON-CHANGE: a new version row is written only when the payload differs
-- from the newest stored version of that transaction_key. A claim is genuinely
-- mutable before processing (PENDING -> EXECUTED/FAILED in the overnight batch),
-- so first-seen-wins would freeze it as PENDING; versioning every pull would
-- rewrite the entire feed daily.
CREATE TABLE IF NOT EXISTS league_transactions (
    season            INTEGER NOT NULL,
    transaction_key   TEXT NOT NULL,       -- ESPN transaction/message id
    week              INTEGER,             -- scoringPeriodId
    team_id           INTEGER,
    espn_player_id    TEXT,
    action            TEXT,                -- ADD / DROP / LINEUP / TRADE / ...
    source            TEXT,                -- FREEAGENT / WAIVER / TEAM / ...
    status            TEXT,                -- PENDING / EXECUTED / FAILED / ...
    bid_amount        REAL,
    proposed_at       TEXT,                -- ISO datetime (intraday!)
    processed_at      TEXT,                -- ISO datetime (intraday!)
    retrieved_as_of   TEXT NOT NULL,
    knowable_as_of    TEXT NOT NULL,       -- date(processed_at or proposed_at)
    PRIMARY KEY (season, transaction_key, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_league_transactions_lookup
    ON league_transactions (season, transaction_key, knowable_as_of, retrieved_as_of);

-- Operational metadata, NOT a fact table: no as-of columns, never read through
-- select_as_of. It exists because league history is perishable — this is how a
-- missed or half-failed scheduled run becomes VISIBLE instead of silently
-- absent, and it is what `ziggurat league status` reports gaps from.
CREATE TABLE IF NOT EXISTS league_sync_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    season           INTEGER NOT NULL,
    retrieved_as_of  TEXT NOT NULL,        -- the day stamp this run wrote under
    started_at       TEXT NOT NULL,        -- ISO datetime
    finished_at      TEXT,
    status           TEXT NOT NULL,        -- ok / partial / failed
    teams            INTEGER,
    players          INTEGER,
    matchups         INTEGER,
    transactions     INTEGER,
    reconcile_conflicts INTEGER,           -- mRoster vs onTeamId disagreements
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_league_sync_runs_lookup
    ON league_sync_runs (season, retrieved_as_of, started_at);
