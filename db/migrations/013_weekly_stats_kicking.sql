-- Migration 013: kicking columns on weekly_stats (item 4.1 §7.1 follow-up).
--
-- WHY. Item 1.4 chose a column list for weekly_stats that carried no kicking
-- stat at all, so every stored kicker row was all-zero for every stat the house
-- pays him for — and nothing raised, because the columns were not missing, they
-- were never asked for. Re-scoring the table graded EVERY kicker at 0.000 in
-- EVERY week, silently (the draft backtest's finding, 2026-08-30; it shipped a
-- parquet supplement, backtest/draft_backtest.py kicking_frame, to grade the K
-- slot at all). In a league whose distinctive rule is a distance-tiered kicker
-- with a -1 miss penalty, and whose draft engine's signature move is an early
-- kicker as a divergence play, the K slot must be priceable from the spine.
--
-- WHAT. The eight nflverse distance buckets the house scoring consumes, exactly
-- as upstream names them (stats_player_week; every one int32, 0 on non-kicker
-- rows, never NaN — measured 569/569 non-null on 2023 K rows). The fold onto
-- core/scoring.py's bucket keys (0-19 + 20-29 + 30-39 -> fg_made_0_39, the rest
-- 1:1) lives in weekly_stats.kicker_scoring_inputs; no scoring NUMBER is here or
-- there (Rule 2). fg_blocked and pat_missed are deliberately NOT persisted: the
-- house has no missed-PAT stat and ESPN's blocked-FG convention is not in the
-- settings fixture (item 3.8 confirms post-Week-1); a later migration adds them
-- if 3.8 finds a charge.
--
-- NULL MEANS NOT CAPTURED — NEVER ZERO. Every row retrieved before this
-- migration (the 2021-2025 backfill partitions stamped 2026-07-25) predates the
-- columns and reads NULL in all eight; a captured kicker row is never NULL in
-- any of them. A consumer that sums these with `or 0` re-creates the silent-zero
-- grade this migration exists to end, so consumers must refuse or flag a NULL
-- row (weekly_stats.kicker_scoring_inputs returns None for one) rather than
-- score it. The old partitions are not rewritten: a re-pull versions the row
-- under a new retrieved_as_of (INSERT OR REPLACE keyed on retrieved_as_of), the
-- newest version wins under select_as_of, and a `historical` read at an as_of
-- before the re-pull day correctly still sees the uncaptured row.
--
-- ADD COLUMN is metadata-only in SQLite (rehearsed on a copy of the 771 MB live
-- database: 2 ms); the columns append after knowable_as_of, and no positional
-- SELECT * consumer exists (accessors return sqlite3.Row by name).
--
-- No BEGIN/COMMIT and no schema_version write here: `store.apply_schema` wraps
-- this script in a transaction and stamps version 13.

ALTER TABLE weekly_stats ADD COLUMN fg_made_0_19  INTEGER;
ALTER TABLE weekly_stats ADD COLUMN fg_made_20_29 INTEGER;
ALTER TABLE weekly_stats ADD COLUMN fg_made_30_39 INTEGER;
ALTER TABLE weekly_stats ADD COLUMN fg_made_40_49 INTEGER;
ALTER TABLE weekly_stats ADD COLUMN fg_made_50_59 INTEGER;
ALTER TABLE weekly_stats ADD COLUMN fg_made_60_   INTEGER;
ALTER TABLE weekly_stats ADD COLUMN pat_made      INTEGER;
ALTER TABLE weekly_stats ADD COLUMN fg_missed     INTEGER;
