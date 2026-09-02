-- Migration 012: Sleeper research ownership panel (item 4.1, the second weekly
-- point-in-time market proxy beside 011's db_fpecr ECR panel).
--
-- CORRECTION TO 011's HEADER (which cannot be edited — applied migrations are
-- immutable, enforced by test_an_applied_migration_is_never_edited). The second
-- paragraph of 011_fpecr_panel.sql gives a false mechanism for why `fp_page` is
-- in that table's primary key. The key is RIGHT; the stated reason is wrong. The
-- corrected justification, verbatim from
-- intel/research/draft-backtest-2026-08-30.md §7.0:
--
--   `fp_page` is IN THE PRIMARY KEY because one `ecr_type` spans several ranking
--   PAGES and dual-eligible players appear on more than one of them on the same
--   day. Measured on the 2026-08-30 archive, after the position filter, over
--   `ro`/`rp`/`wp`: 215 same-key page collisions across 14 distinct player-name
--   strings (`ppr-rb-cheatsheets`+`ppr-wr-cheatsheets` 59, `ppr-rb`+`ppr-wr` 36,
--   `ros-ppr-rb`+`ros-ppr-wr` 36, `idp-cheatsheets`+`ppr-cheatsheets` 33,
--   `db-cheatsheets`+`ppr-wr-cheatsheets` 31, …). `adp_rankings`' key
--   (fantasypros_id, ecr_type, scrape_date, retrieved_as_of) folds each pair to
--   one row with `INSERT OR REPLACE` and nothing records the loss. NOTE: an
--   earlier version of this header claimed the collision was `ppr-cheatsheets`
--   (preseason) against `ros-ppr-overall` (rest-of-season) inside `ro`. Those two
--   pages share ZERO scrape dates (259 vs 101), so that collision cannot occur.
--
-- ---------------------------------------------------------------------------
--
-- sleeper_ownership: one row per (season, season_type, week, sleeper_id) per
-- pull. `owned_pct` / `started_pct` are percent-rostered / percent-started
-- across Sleeper's whole user base for that NFL week, served by
-- api.sleeper.com/players/nfl/research/{season_type}/{season}/{week} as a
-- FROZEN per-week snapshot (a retired player's 2021 week-6 number is a past-
-- season value, not his live ownership) and frozen again locally as raw JSON
-- under the gitignored data/backtest/sleeper-research/ tree.
--
-- Identity is `sleeper_id`, upstream's own key, NOT `gsis_id`: team-defense
-- rows (position DST, the team abbreviation as the key) and Sleeper ids the
-- players crosswalk cannot name (kept, NULL gsis_id, position 'UNK') have no
-- gsis to key on, and folding every unresolved player into one NULL row is
-- exactly the silent loss 011's correction above is about. `retrieved_as_of`
-- is in the key so a re-pull VERSIONS rather than replaces (`select_as_of`
-- resolves the newest version per key); the ingester floors a shrunken grid
-- before it can shadow a good one.
--
-- `knowable_as_of` is the week's LAST regular-season gameday from `schedules`
-- — a LABELLED HYPOTHESIS, because Sleeper does not document within-week
-- timing (intel/research/market-archives.md: resolution ~1 week); the later
-- stamp is the conservative one for a leakage gate. The whole grid carries one
-- pull day as `retrieved_as_of`, so it reads EMPTY under the default
-- `historical` view at any past `as_of` — read it through
-- `base.latest_truth(get_sleeper_ownership)`, like `fpecr_panel`.
--
-- No BEGIN/COMMIT and no schema_version write here: `store.apply_schema` wraps
-- this script in a transaction and stamps version 12.

CREATE TABLE IF NOT EXISTS sleeper_ownership (
    season          INTEGER NOT NULL,
    season_type     TEXT    NOT NULL,   -- upstream's path segment: regular | pre | post
    week            INTEGER NOT NULL,
    sleeper_id      TEXT    NOT NULL,   -- numeric player id, or the team abbreviation for DST
    gsis_id         TEXT,               -- NULL for DST rows and unresolved crosswalk ids
    position        TEXT    NOT NULL,   -- house spelling (K not PK, DST for teams, UNK if unresolved)
    team            TEXT,               -- DST rows only (base.TEAM_ALIASES: LAR -> LA); the payload carries no team for players
    owned_pct       REAL    NOT NULL,   -- percent rostered across Sleeper's user base; censored at ~1% (absent = at/below the floor)
    started_pct     REAL,               -- percent started; absent for a handful of keys per week
    retrieved_as_of TEXT    NOT NULL,   -- the pull day (one per grid pull)
    knowable_as_of  TEXT    NOT NULL,   -- the week's last REG gameday (hypothesis; see header)
    PRIMARY KEY (season, season_type, week, sleeper_id, retrieved_as_of)
);

-- The as-of resolution pattern: (partition, knowable_as_of, retrieved_as_of).
CREATE INDEX IF NOT EXISTS idx_sleeper_ownership_lookup
    ON sleeper_ownership (season, season_type, week, knowable_as_of, retrieved_as_of);

-- One player's ownership curve across a season (the delta helper's read shape).
CREATE INDEX IF NOT EXISTS idx_sleeper_ownership_player
    ON sleeper_ownership (gsis_id, season, week, knowable_as_of, retrieved_as_of);
