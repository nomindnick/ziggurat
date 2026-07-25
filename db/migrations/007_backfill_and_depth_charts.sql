-- db/migrations/007_backfill_and_depth_charts.sql
-- Item 3.2c: the historical NFL backfill + `depth_charts` v2.
--
-- ONE FILE, DELIBERATELY. The two halves of 3.2c (backfill, depth charts v2)
-- were each designed with their own `007_*.sql`. Two files numbered 007 make
-- `store._migrations()` raise
--     RuntimeError: migration versions must be contiguous;
--                   found [2,3,4,5,6,7,7], expected [2,3,4,5,6,7,8]
-- and `apply_schema` is called UNCONDITIONALLY by `open_db`, which its own
-- docstring calls "the safe default for any command" -- so `draft-web`,
-- `draft-board`, `league sync`, `ingest run` and `marginal` would ALL die at
-- startup, three weeks before the draft. Anything 3.2c adds to the schema goes
-- in THIS file until it ships. `tests/test_migrations.py` asserts the shipped
-- migrations directory actually applies, so this class cannot recur silently.
--
-- No BEGIN/COMMIT and no `schema_version` write here: `store.apply_schema`
-- wraps this script in a transaction and stamps the version itself.
--
-- Contents, in order:
--   1. depth_chart_panels + depth_chart_slots  (the v2 dated-panel regime, 2025+)
--   2. depth_charts -> depth_charts_weekly     (the legacy weekly regime, 2021-2024)
--   3. snap_counts PK widening                 (F-E: a two-team week is not storable)
--
-- WHY schema.sql IS NOT TOUCHED: `db/schema.sql` is the FROZEN v1 bootstrap --
-- it stamps `schema_version = 1` itself and no table added by migrations 003
-- (team_defense, game_odds, game_weather, projections, adp_rankings), 004
-- (espn_draft_ranks), 005 (the five league_* tables) or 006 (nfl_ingest_runs)
-- was ever mirrored back into it. Mirroring the v2 tables there would duplicate
-- DDL that can drift; RENAMING `depth_charts` there would break migration 002,
-- which does `CREATE INDEX idx_depth_charts_lookup ON depth_charts` (measured:
-- "no such table: main.depth_charts"). So a fresh `ziggurat db init` bootstraps
-- the v1 shape and THIS migration converts it -- which is exactly the shape this
-- file is written against. `tests/test_migrations.py` pins the post-migration
-- shape of a fresh bootstrap, which is what "schema.sql and 007 agree" actually
-- means operationally.


-- =========================================================================
-- 1. depth_charts v2 -- the dated daily panel (2025 onwards)
-- =========================================================================
-- Upstream replaced the weekly depth chart with a DAILY PANEL keyed on a
-- publish timestamp `dt`. That is why `depth_charts` has been BLOCKED since
-- item 3.1b: the stored weekly table cannot hold it and `base.select_as_of`
-- cannot query it.
--
-- TWO REGIMES, TWO TABLES, PERMANENTLY. The obvious move -- route 2021-2024
-- through the new table too -- was measured and is a data-fabrication bug:
-- four of the five v2 key columns (pos_grp_id, pos_id, pos_rank, espn_id) do
-- not exist in the legacy frame at all, and the rescue (join gsis_id -> espn_id
-- through the crosswalk) resolves only ~82% of legacy rows, so ~18% land with
-- espn_id NULL -- which in this table MEANS "this slot was vacated". ~148k rows
-- would have stored "successfully" and read back as ZERO occupancy rows, with
-- one row in six a fabricated vacancy fact, biased toward OL/LS/practice squad
-- (i.e. invisible to any skill-position smoke check). Do not merge these tables.

-- ------------------------------------------------------------ depth_chart_panels
-- ONE ROW PER OBSERVED SNAPSHOT: the positive "a panel existed at this instant"
-- fact, mirroring item 3.1's positive `on_team_id IS NULL` drop fact. Three jobs:
--  (a) A QUIET DAY IS STILL WORK DONE. Measured: 8 of 2025's 221 panels and 20
--      of 2026's 127 carried ZERO slot changes. Without this row those pulls
--      write 0 rows, `refresh.py` reads 0 rows as STATUS_EMPTY -> a problem
--      status, a non-zero exit and a standing false alarm on ~16% of days.
--  (b) "AS OF WHEN" FOR THE READER (Rule 6). The slot log alone cannot tell
--      "no panel was published" from "a panel was published and nothing moved".
--  (c) THE INGEST WATERMARK. MAX(observed_at) per season is what stops the
--      daily pull from re-storing 348 snapshots every morning.
CREATE TABLE IF NOT EXISTS depth_chart_panels (
    season          INTEGER NOT NULL,  -- stamped from the FILE REQUESTED, never inferred from dt:
                                       -- the 2025 file runs to 2026-03-14, so inferring would
                                       -- misfile every season's Jan-Mar tail into the next one
    observed_at     TEXT    NOT NULL,  -- upstream `dt` VERBATIM, e.g. '2025-09-17T07:14:22Z'
    n_teams         INTEGER NOT NULL,  -- 32 in all 348 observed panels; < 32 is a partial scrape
    n_slots         INTEGER NOT NULL,  -- rows in that panel (2095..3264 measured)
    n_changes       INTEGER NOT NULL,  -- slot rows this panel contributed (0 is LEGAL -- see (a))
    retrieved_as_of TEXT    NOT NULL,
    knowable_as_of  TEXT    NOT NULL,  -- = observed_at[:10]
    PRIMARY KEY (season, observed_at, retrieved_as_of)
);

-- ------------------------------------------------------------- depth_chart_slots
-- THE CHANGE LOG. One row = "at this instant, this slot's occupant became X".
-- A slot with no row at `dt` is UNCHANGED since its last row; `espn_id IS NULL`
-- is a TOMBSTONE ("this slot no longer exists") -- the same shape as
-- league_player_state's `on_team_id IS NULL`.
--
-- WHY A CHANGE LOG AND NOT THE PANEL: verbatim storage of 2025+2026 (923,162
-- source rows) measured 255.4 MB on a 43.4 MB database. The change encoding
-- measured 31,085 rows / 6.50 MB WITH indexes, and is provably lossless -- all
-- 348 published panels reconstruct row-for-row against raw upstream (221/221
-- for 2025, 127/127 for 2026, 0 mismatches). It also dissolves the IDP
-- question: all-position 6.50 MB vs skill-only ~2 MB, so STORE EVERYTHING
-- rather than foreclose future D/ST front-personnel work to save ~4 MB.
--
-- WHY THE TOMBSTONES ARE LOAD-BEARING, NOT TIDINESS: per-key resolution over a
-- FULL panel inflates a board 58% (a KC roster showing both a QB3 and a QB4
-- named Chris Oladokun); per-key resolution WITHOUT tombstones resurrects
-- ghosts (a phantom rank-4 carried forward seven weeks). Change-only +
-- tombstones + per-key resolution ordered on `observed_at` satisfies both --
-- validated at 24 as-of points across two seasons, 0 mismatches.
--
-- WHY THIS KEY. Measured on 554,215 rows: (dt, team, pos_grp_id, pos_id,
-- pos_rank) -> 554,215 distinct, 0 duplicates.
--  * pos_grp_id IS REQUIRED, NOT DECORATIVE. Nine pos_abb values (SLB, LDE,
--    RDE, NB, FS, SS, RCB, LCB, WLB) appear in BOTH base defenses;
--    (dt, team, pos_abb, pos_rank) is unique today only because a team carries
--    one base front per dt -- and 9 teams switched scheme during 2025. That is
--    a latent silent collapse waiting on one team running two fronts at once.
--  * espn_id IS NOT IN THE KEY; gsis_id IS NOWHERE NEAR IT. The key is the
--    SLOT, the occupant is the value -- that is what makes "slot vacated"
--    expressible at all. espn_id has 0 nulls and 0 empty strings in 923,162
--    rows; gsis_id's 1.01% nulls would poison a key, since SQLite treats PK
--    NULLs as DISTINCT (the hazard the legacy ingester papers over with ''
--    coalescing).
--  * observed_at IN THE KEY, not just knowable_as_of. Four days carry 2-3
--    panels (2025-08-09, 2025-08-11, 2026-03-22); day-granular resolution
--    cannot order them, MAX(observed_at) can.
--  * retrieved_as_of IN THE KEY keeps the repo convention (a correction is a
--    new version, never an overwrite) and is what makes `latest_truth` mean
--    anything here. NOTE FOR THE INGESTER: because retrieved_as_of is IN the
--    key, `INSERT OR IGNORE` would never fire -- use the ordinary base.upsert.
--
-- NOT AN INJURY / AVAILABILITY SIGNAL. Measured on 2025: a starter ruled Out is
-- NOT demoted (Hubbard, Harrison Jr., Stevenson all held pos_rank = 1 every
-- single day); of 15 rank-1 skill players with >=3 consecutive Out weeks, 1
-- (7%) was demoted within 14 days. Injuries = availability, depth chart = role
-- order; never conflate them. See IMPLEMENTATION_PLAN.md 3.2c / 3.3.
CREATE TABLE IF NOT EXISTS depth_chart_slots (
    season          INTEGER NOT NULL,
    team            TEXT    NOT NULL,  -- matches schedules abbrs exactly (0 mismatches, both seasons)
    pos_grp_id      TEXT    NOT NULL,  -- '15' 3-4 D | '16' 4-3 D | '18' ST | '21' 3WR 1TE
    pos_id          TEXT    NOT NULL,  -- strictly 1:1 with pos_abb (0 violations in 554,215 rows)
    pos_rank        INTEGER NOT NULL,  -- depth order WITHIN pos_abb; 0 nulls upstream
    observed_at     TEXT    NOT NULL,  -- the `dt` this value was first observed
    pos_abb         TEXT    NOT NULL,  -- denormalised: the hot filter (QB/RB/WR/TE)
    pos_grp         TEXT,              -- label for the id; payload
    pos_slot        INTEGER,           -- lineup slot; payload (a slot holds several ranks)
    espn_id         TEXT,              -- OCCUPANT. NULL == TOMBSTONE.
    gsis_id         TEXT,              -- 1.01% null (2025) -- payload ONLY, never a key member
    player_name     TEXT,              -- 445 null (2025); payload
    retrieved_as_of TEXT    NOT NULL,
    knowable_as_of  TEXT    NOT NULL,  -- = observed_at[:10]
    PRIMARY KEY (season, team, pos_grp_id, pos_id, pos_rank, observed_at, retrieved_as_of)
);

CREATE INDEX IF NOT EXISTS idx_depth_chart_slots_lookup
    ON depth_chart_slots (season, team, knowable_as_of, observed_at, retrieved_as_of);
CREATE INDEX IF NOT EXISTS idx_depth_chart_slots_player
    ON depth_chart_slots (season, espn_id, knowable_as_of);


-- =========================================================================
-- 2. depth_charts -> depth_charts_weekly, with the PK fixed
-- =========================================================================
-- The 2021-2024 archive. Renamed so the name says which regime it holds, and
-- NEVER dropped -- it is the only copy of the weekly shape.
--
-- WHY THE PK CHANGES. The stored PK omits `game_type` and `depth_team`, so
-- INSERT OR REPLACE silently collapsed 835 / 947 / 899 / 933 rows per season
-- (2021 / 2022 / 2023 / 2024) -- of which ~700 a season differ ONLY in
-- `depth_team`, i.e. the depth ORDER, i.e. the one column this table exists
-- for -- while the ingester returned the full row count and `note_drops`
-- reported 0. With game_type and depth_team in the key the residual collapse is
-- 145 / 171 / 182 / 207 BYTE-IDENTICAL upstream duplicates and ZERO
-- non-identical collisions (verified on all four season files).
--
-- SQLite cannot ALTER a PRIMARY KEY, so this is the standard safe rebuild
-- (create _new, INSERT ... SELECT, DROP, RENAME). The live table is 0 rows on
-- this box, but the rebuild is written to be correct on a populated one.
--
-- COALESCE(x, '') ON THE NULLABLE KEY MEMBERS, deliberately: a NULL PK member
-- is DISTINCT from every other NULL in SQLite's unique index (so it cannot
-- dedupe) AND `base.select_as_of`'s key self-join `t2.k = t.k` is never
-- satisfied by NULL (so the row is invisible to every read). '' means "not
-- present in the depth-chart source" -- the convention the shipped ingester
-- already applies to gsis_id and depth_position at ingest time.

-- Plain CREATE (no IF NOT EXISTS) on the scratch table: a pre-existing
-- `*_new` is an anomaly that must raise, not silently merge. apply_schema
-- wraps this script in a transaction and rolls back on any error, so a raise
-- here leaves the database exactly as it was.
CREATE TABLE depth_charts_weekly_new (
    gsis_id         TEXT,
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    game_type       TEXT,
    club_code       TEXT NOT NULL,
    position        TEXT NOT NULL,
    depth_position  TEXT,
    depth_team      TEXT,
    formation       TEXT NOT NULL,
    full_name       TEXT,
    retrieved_as_of TEXT NOT NULL,
    knowable_as_of  TEXT NOT NULL,
    PRIMARY KEY (season, week, game_type, club_code, formation, position,
                 depth_position, depth_team, gsis_id, retrieved_as_of)
);

-- OR REPLACE, not a bare INSERT: this migration runs inside `open_db` on EVERY
-- command, so it must not be able to raise on an operator's populated database
-- three weeks before the draft. The new key is a strict SUPERSET of the old
-- one, so no pair of rows that were distinct before can collide now -- the only
-- reachable collapse is a NULL/'' merge introduced by the COALESCE above. It is
-- recorded as a positive fact below rather than swallowed.
INSERT OR REPLACE INTO depth_charts_weekly_new
    (gsis_id, season, week, game_type, club_code, position,
     depth_position, depth_team, formation, full_name,
     retrieved_as_of, knowable_as_of)
SELECT COALESCE(gsis_id, ''), season, week, COALESCE(game_type, ''), club_code, position,
       COALESCE(depth_position, ''), COALESCE(depth_team, ''), formation, full_name,
       retrieved_as_of, knowable_as_of
  FROM depth_charts;

-- SILENCE IS NOT SUCCESS. If the rebuild lost a row, say so in a durable place
-- rather than let a migration be the quiet one. The row only exists when there
-- IS a discrepancy, so its presence is the alarm.
INSERT OR REPLACE INTO meta (key, value)
SELECT '007_depth_charts_weekly_collapsed',
       CAST((SELECT COUNT(*) FROM depth_charts)
            - (SELECT COUNT(*) FROM depth_charts_weekly_new) AS TEXT)
 WHERE (SELECT COUNT(*) FROM depth_charts)
       <> (SELECT COUNT(*) FROM depth_charts_weekly_new);

DROP TABLE depth_charts;   -- takes idx_depth_charts_lookup (migration 002) with it
ALTER TABLE depth_charts_weekly_new RENAME TO depth_charts_weekly;

CREATE INDEX IF NOT EXISTS idx_depth_charts_weekly_lookup
    ON depth_charts_weekly (season, week, club_code, knowable_as_of, retrieved_as_of);


-- =========================================================================
-- 3. snap_counts -- widen the PK to hold a two-team week (finding F-E)
-- =========================================================================
-- MEASURED, twice and independently: pfr_player_id `DaviJa06` (Jalen Davis),
-- 2021 week 12, played 10 defensive snaps for MIA *and* 23 for CIN. Under the
-- PK (pfr_player_id, season, week, retrieved_as_of) those are the same key, so
-- INSERT OR REPLACE keeps one: the ingester returned 26,468 rows, the table
-- received 26,467, and `note_drops` reported 0. Silent loss of a real fact.
--
-- Zero fantasy impact in 2021-2025 (the collisions are defensive backs), but a
-- mid-season traded skill player hits it, and the failure mode is a row that
-- simply is not there.
--
-- `team` is safe in the key: `ingest_snap_counts` resolves knowable_as_of via
-- game_date_map[(season, week, team)], so a row with no team can never resolve
-- a gameday and is dropped before storage -- no NULL-team row can exist. The
-- COALESCE is belt-and-braces for a database written by some other path.
-- HANDOFF: `get_snap_counts`'s `key_cols` must widen to match, or the accessor
-- will resolve two teams' rows against each other.
CREATE TABLE snap_counts_new (
    pfr_player_id   TEXT NOT NULL,
    gsis_id         TEXT,            -- resolved via players crosswalk
    player          TEXT,
    position        TEXT,
    team            TEXT,
    opponent        TEXT,
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    game_id         TEXT,
    offense_snaps   REAL,
    offense_pct     REAL,
    defense_snaps   REAL,
    defense_pct     REAL,
    st_snaps        REAL,
    st_pct          REAL,
    retrieved_as_of TEXT NOT NULL,
    knowable_as_of  TEXT NOT NULL,
    PRIMARY KEY (pfr_player_id, season, week, team, retrieved_as_of)
);

INSERT OR REPLACE INTO snap_counts_new
    (pfr_player_id, gsis_id, player, position, team, opponent, season, week,
     game_id, offense_snaps, offense_pct, defense_snaps, defense_pct,
     st_snaps, st_pct, retrieved_as_of, knowable_as_of)
SELECT pfr_player_id, gsis_id, player, position, COALESCE(team, ''), opponent, season, week,
       game_id, offense_snaps, offense_pct, defense_snaps, defense_pct,
       st_snaps, st_pct, retrieved_as_of, knowable_as_of
  FROM snap_counts;

INSERT OR REPLACE INTO meta (key, value)
SELECT '007_snap_counts_collapsed',
       CAST((SELECT COUNT(*) FROM snap_counts)
            - (SELECT COUNT(*) FROM snap_counts_new) AS TEXT)
 WHERE (SELECT COUNT(*) FROM snap_counts)
       <> (SELECT COUNT(*) FROM snap_counts_new);

DROP TABLE snap_counts;    -- takes idx_snap_counts_lookup (migration 002) with it
ALTER TABLE snap_counts_new RENAME TO snap_counts;

CREATE INDEX IF NOT EXISTS idx_snap_counts_lookup
    ON snap_counts (season, week, knowable_as_of, retrieved_as_of);
