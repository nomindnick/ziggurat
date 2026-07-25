-- db/migrations/006_nfl_ingest_runs.sql
-- Item 3.1b: the NFL source refresh cadence's run log.
--
-- WHY A SECOND RUN-LOG TABLE INSTEAD OF REUSING league_sync_runs:
-- league.state.last_run() filters on SEASON ONLY and has no source column, so
-- the first NFL row written there would silently become the answer to "when did
-- the league last sync" — turning the one honest signal about PERISHABLE league
-- history into a lie, with no test failing. Its count columns
-- (teams/matchups/reconcile_conflicts) are league-shaped besides.
--
-- GRAIN: ONE ROW PER SOURCE PER RUN, not one per batch. "Last successful pull
-- per source" is exactly what item 3.1b's done-when asks for and what 3.2's
-- staleness banner reads, and at this grain it is a single GROUP BY. A batch of
-- sources launched together shares a batch_id so one `ingest run` is still
-- reconstructable.
--
-- SILENCE IS NOT SUCCESS (the item-3.1 stance, carried forward): every attempt
-- writes a row, including failures, skips and blocked sources. A source that
-- quietly stopped being pulled must be VISIBLE — the November failure mode this
-- item exists to prevent is Week 10 priced off a July projection snapshot with a
-- perfectly valid knowable_as_of, where nothing complains because nothing is
-- wrong, merely stale.
--
-- HOW THIS DIFFERS FROM league_sync_runs, deliberately: nflverse serves whole
-- season files on demand, so a missed NFL run is STALENESS, not loss. There is
-- no MISSING DAYS gap report here and there must not be one — crying wolf about
-- recoverable gaps is how an operator learns to ignore the league-side report,
-- where the same words are literally true. Only the perishable sources
-- (projections, adp_rankings, espn_ranks, game_weather in forecast mode) can
-- lose anything by being missed, and refresh.py marks those individually.
--
-- Operational metadata, NOT a fact table: no as-of columns, never read through
-- select_as_of.
CREATE TABLE IF NOT EXISTS nfl_ingest_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT NOT NULL,        -- groups the sources of one `ziggurat ingest run`
    source           TEXT NOT NULL,        -- refresh.SOURCES name
    season           INTEGER,
    scope            TEXT,                 -- partition detail actually requested, e.g. 'weeks 1-18'
    retrieved_as_of  TEXT NOT NULL,        -- the day stamp this run wrote under
    started_at       TEXT NOT NULL,        -- ISO datetime (run log wall clock, never a knowledge time)
    finished_at      TEXT,
    -- running / abandoned / ok / partial / empty / failed / skipped / fresh /
    -- blocked / upstream_absent. The distinct not-ok outcomes are the point:
    -- 'skipped' (the season phase or a missing dependency says this source has
    -- nothing to say today), 'fresh' (a good pull is still inside the source's
    -- interval, so the timer may fire daily while upstream is hit weekly),
    -- 'upstream_absent' (nflverse has not published this season yet — expected
    -- every day until ~Sept 10, and if that logged as 'failed' the operator would
    -- be desensitized by the time real failures arrive), 'blocked' (a recorded
    -- upstream schema break), 'abandoned' (a 'running' row a later run found
    -- orphaned: the process died mid-pull, so every source after it never ran)
    -- and 'failed' (a genuine error, including a refused collapse and a pull that
    -- dropped most of its rows).
    status           TEXT NOT NULL,
    rows_written     INTEGER,
    rows_dropped     INTEGER,              -- from base.collect_drops; 0 written + >0 dropped = failed
    error            TEXT
);

-- "last successful pull per source" (the staleness read) and "the whole of one
-- batch" (the run report) are the only two access patterns.
-- SEASON is in the index because it is in the WHERE clause: "how stale is source
-- S for season X" must not be answered from another season's run (a Phase-4
-- backfill, or the pinned --season in the units after a season rollover).
CREATE INDEX IF NOT EXISTS idx_nfl_ingest_runs_source
    ON nfl_ingest_runs (source, season, status, run_id);
CREATE INDEX IF NOT EXISTS idx_nfl_ingest_runs_batch
    ON nfl_ingest_runs (batch_id, run_id);
