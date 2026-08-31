-- db/migrations/010_espn_projections_source_key.sql
-- Item 3.9 correction: put `source` INTO the espn_projections PRIMARY KEY.
--
-- ------------------------------------------------------------------------
-- WHY THIS IS A SECOND FILE AND NOT AN EDIT TO 009
-- ------------------------------------------------------------------------
-- 009 shipped with PRIMARY KEY (season, week, espn_key, retrieved_as_of) while
-- its own comment on the `source` column promised that a second opinion "lands
-- additively, without a migration". That promise was false and MEASURED false:
-- two rows identical in those four columns but differing in `source` collapse to
-- ONE, the second silently REPLACING the first (offered 2, stored 1).
--
-- The fix was written as an edit to 009 — and then a systemd timer applied 009
-- to `db/ziggurat.sqlite` at 17:01 PDT on 2026-08-30, mid-change, exactly as
-- CLAUDE.md warns ("the systemd timers run `ziggurat` from the working tree, so
-- uncommitted code is the production cadence"). `apply_schema` compares NUMBERS
-- ONLY and never re-runs an applied migration, so from that moment the edit
-- could never reach the operator's database: a fresh build would have carried
-- the wide key, the live database the narrow one, with the whole test suite
-- agreeing with the file and none of it agreeing with the database. That is the
-- precise shape `test_an_applied_migration_is_never_edited` exists to prevent,
-- and its doctrine names the edited PRIMARY KEY as the worst case of all.
--
-- So 009 was restored byte-for-byte to what actually ran (verified against the
-- live `sqlite_master`), and the correction is here, where every database gets
-- it exactly once.
--
-- ------------------------------------------------------------------------
-- WHY A REBUILD IS SAFE HERE
-- ------------------------------------------------------------------------
-- SQLite cannot ALTER a PRIMARY KEY, so the table is rebuilt. Two facts make
-- that lossless rather than a 007-style collapse risk:
--
--   1. WIDENING a primary key can never merge rows. Adding a column to the key
--      can only ever split keys apart, never join them, so the copy below is
--      lossless BY CONSTRUCTION — there is no `INSERT OR REPLACE` here and no
--      `<nnn>_<table>_collapsed` meta row to write, because no collision is
--      reachable. (Contrast 007, which narrowed/reshaped and therefore had to.)
--   2. The table is EMPTY on the operator's database (0 rows measured at the
--      time of writing): 009 landed only minutes earlier and nothing has
--      ingested through it yet. The copy is still written to run correctly on a
--      populated database, because "it is empty today" is not a schema rule.
--
-- No BEGIN/COMMIT and no schema_version write: `store.apply_schema` wraps this
-- script in a transaction and stamps version 10.

CREATE TABLE espn_projections_v2 (
    -- Provenance, and now part of the identity. `projections` (migration 003)
    -- carries `source` in its own primary key for this exact reason; this table
    -- says it mirrors that one name-for-name, and now it does where it counts.
    source             TEXT NOT NULL,

    espn_key           TEXT NOT NULL,
    espn_id            TEXT,
    gsis_id            TEXT,
    player             TEXT,
    position           TEXT NOT NULL,
    team               TEXT,
    season             INTEGER NOT NULL,
    week               INTEGER NOT NULL,

    projected_games    REAL,
    espn_applied_total REAL,

    passing_yards             REAL,
    passing_tds               REAL,
    interceptions             REAL,
    rushing_yards             REAL,
    rushing_tds               REAL,
    receptions                REAL,
    receiving_yards           REAL,
    receiving_tds             REAL,
    fumbles_lost              REAL,
    passing_2pt_conversions   REAL,
    rushing_2pt_conversions   REAL,
    receiving_2pt_conversions REAL,
    special_teams_tds         REAL,

    fg_made_0_39              REAL,
    fg_made_40_49             REAL,
    fg_made_50_59             REAL,
    fg_made_60                REAL,
    pat_made                  REAL,
    fg_missed                 REAL,

    sacks                     REAL,
    def_interceptions         REAL,
    fumble_recoveries         REAL,
    safeties                  REAL,
    blocked_kicks             REAL,
    def_tds                   REAL,
    one_point_safeties        REAL,
    two_point_returns         REAL,

    pa_games_0                REAL,
    pa_games_1_6              REAL,
    pa_games_7_13             REAL,
    pa_games_14_17            REAL,
    pa_games_18_21            REAL,
    pa_games_22_27            REAL,
    pa_games_28_34            REAL,
    pa_games_35_45            REAL,
    pa_games_46_plus          REAL,
    ya_games_0_99             REAL,
    ya_games_100_199          REAL,
    ya_games_200_299          REAL,
    ya_games_300_349          REAL,
    ya_games_350_399          REAL,
    ya_games_400_449          REAL,
    ya_games_450_499          REAL,
    ya_games_500_549          REAL,
    ya_games_550_plus         REAL,

    retrieved_as_of    TEXT NOT NULL,
    knowable_as_of     TEXT NOT NULL,

    -- THE CORRECTION. `source` leads, matching `projections` (003).
    PRIMARY KEY (source, season, week, espn_key, retrieved_as_of)
);

-- Plain INSERT, never INSERT OR REPLACE: on a widened key a collision is
-- unreachable, so if one somehow occurred it MUST raise and roll the whole
-- migration back rather than silently drop a row.
INSERT INTO espn_projections_v2 SELECT * FROM espn_projections;

DROP TABLE espn_projections;
ALTER TABLE espn_projections_v2 RENAME TO espn_projections;

-- DROP TABLE took both indexes with it; recreate them against the new table.
-- The lookup index is unchanged from 009. The key index gains `source` so the
-- ensemble's join stays covered now that identity includes it.
CREATE INDEX IF NOT EXISTS idx_espn_projections_lookup
    ON espn_projections (season, week, position, knowable_as_of, retrieved_as_of);
CREATE INDEX IF NOT EXISTS idx_espn_projections_key
    ON espn_projections (espn_key, season, week, source, retrieved_as_of);
