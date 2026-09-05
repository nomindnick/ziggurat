-- db/migrations/018_decision_freezes.sql
-- Item 4.2b (B2): the decision-freeze RUN LOG.
--
-- WHY THIS TABLE EXISTS AND THE PAYLOAD DOES NOT LIVE IN IT.
-- `ziggurat waivers` produces stdout and nothing else: the swap matrix, the FA
-- pool as priced, the candidate board, the chain's own bookkeeping and every
-- reason string are discarded at process exit. A missed Tuesday is
-- UNRECOVERABLE (league_player_state accumulates forward only, item 3.1; four
-- market sources serve the current value only, item 3.1b), so the Tuesday that
-- produced a decision has to be written down on the day.
--
-- The payload is written OUTSIDE the database, as sha256-manifested JSONL under
-- the gitignored `data/decisions/<season>/wk<NN>/<capture_id>/`, for three
-- reasons recorded so they are not re-litigated:
--   1. A freeze is neither a fact about the NFL nor pure operational metadata —
--      it is "what the tool SAID at as_of T". As a fact table it would owe a
--      `knowable_as_of` that has no honest value, and every future accessor
--      would have to remember not to serve a Tuesday page as a Wednesday input.
--      Outside the DB the question does not arise (which is why the 4.1 backtest
--      freezes live outside it too).
--   2. The database is ~840 MB growing ~13.5 MB/day, almost all of it the daily
--      projections re-version. ~450 KB/week of payload belongs where it costs
--      nothing to back up.
--   3. A table has no manifest concept. "Was this Tuesday captured COMPLETELY?"
--      is a digest check against a manifest, not a COUNT query.
--
-- WHAT THIS TABLE IS FOR, then: making the capture VISIBLE to the surfaces the
-- operator already reads. JSONL alone means "did Tuesday get captured?" is
-- answered by listing directories, and SILENCE IS NOT SUCCESS is the one rule
-- this system has paid for three times (items 3.1, 3.1b, 3.6).
--
-- OPERATIONAL METADATA — NO as-of COLUMNS, NEVER READ THROUGH select_as_of.
-- Same class as league_sync_runs (005), nfl_ingest_runs (006) and push_runs
-- (008). One column looks like an exception and is not: `plan_as_of` is the run
-- PARAMETER the operator passed (`--as-of`), i.e. the gate the plan ran at,
-- stored so a capture can be found by the decision it belongs to. It is not a
-- knowledge-time stamp on a fact, nothing gates on it, and nothing in
-- ziggurat/decisions/ passes this table to base.select_as_of — pinned by
-- tests/test_decisions_capture.py. nfl_ingest_runs.retrieved_as_of is the same
-- shape and the same disclosure.
--
-- PUBLISH-THEN-RECORD, and the one thing it does NOT mean here. The `ok` row is
-- written only AFTER manifest.json has landed: a capture is complete exactly
-- when its manifest exists, so a row claiming `ok` over a half-written directory
-- would be a lie that `verify` could not repair. But a `running` row IS written
-- before the work starts, because a crashed capture must leave a legible fact
-- rather than silence — that is the run-log discipline (start-before-work), and
-- it is NOT the 3.6 defect. The 3.6 defect was reserving a DEDUP LEDGER row
-- before the side effect, which permanently SUPPRESSED the real push. Nothing
-- here suppresses anything: a `running`/`failed` row blocks no later capture,
-- and a later capture is a new row with a new capture_id.
--
-- APPEND-ONLY, ONE ROW PER RUN. Captures are never idempotent-overwrite: two
-- captures on one Tuesday are two facts (the operator runs `waivers` more than
-- once), and item 4.2b's record command names which one was acted on. There is
-- no delete-then-rewrite path here, so the SnapshotCollapse / BoardCollapse /
-- CrosswalkCollapse floor pattern has nothing to guard.
--
-- No BEGIN/COMMIT and no schema_version write here: store.apply_schema wraps
-- this script in a transaction and stamps the version itself.

CREATE TABLE IF NOT EXISTS decision_freezes (
    freeze_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The capture directory's own name, minted before any work starts, so a
    -- crashed run's row still names where it was writing. UNIQUE: two captures
    -- must never share an identity, and the writer refuses an existing directory
    -- rather than overwriting one.
    capture_id      TEXT NOT NULL UNIQUE,
    season          INTEGER,
    -- The decision week (the first week the plan priced). NULL when the window
    -- never resolved — a blocked roster refuses before the board is built.
    week            INTEGER,
    -- 'waivers' (the always-on capture inside every `ziggurat waivers` run),
    -- 'cli' (`ziggurat decisions freeze`), 'timer' (the Tuesday unit).
    trigger         TEXT NOT NULL,
    -- The RUN PARAMETER --as-of. See the header: not a knowledge-time column.
    plan_as_of      TEXT,
    started_at      TEXT NOT NULL,      -- run-log wall clock, never a knowledge time
    finished_at     TEXT,
    -- running / ok / partial / failed / abandoned.
    --   'ok'        every file including the candidate board landed.
    --   'partial'   the manifest landed but the CANDIDATE half is absent, with
    --               its reason recorded. EXPECTED before Week 1 (the generator
    --               raises NoCompletedWeek until a REG week is fully played) and
    --               on the blocked-roster path. A capture, not a failure — but
    --               deliberately not 'ok', because the freeze is incomplete and
    --               `decisions status` has to say so.
    --   'failed'    the manifest did NOT land; the directory may hold partial
    --               files and must not be trusted.
    --   'abandoned' a 'running' row a later run found orphaned (process died).
    status          TEXT NOT NULL,
    artifact_dir    TEXT,               -- the capture directory (gitignored data/)
    manifest_sha256 TEXT,               -- the manifest's OWN digest: `verify` re-checks it
    payload_digest  TEXT,               -- sha256 over the payload files' digests (determinism)
    files           INTEGER,            -- payload files written (manifest excluded)
    claims          INTEGER,            -- plan.claims
    grabs           INTEGER,            -- plan.fcfs_grabs
    streaming       INTEGER,            -- plan.streaming (outside the chain)
    evaluated       INTEGER,            -- candidate rows captured; NULL = the arm never ran
    flagged         INTEGER,            -- of those, the ones that became CandidateRows
    blocked         INTEGER,            -- 1 = the roster was illegal and claims were refused
    chain_gain      REAL,               -- the joint "if every claim and grab wins" number
    error           TEXT                -- the failure, or the ABSENT reason on 'partial'
);

-- The two access patterns: "the last N captures" (status) and "this week's
-- captures" (verify / record / classify, all keyed on season+week).
CREATE INDEX IF NOT EXISTS idx_decision_freezes_recent
    ON decision_freezes (season, week, freeze_id);
CREATE INDEX IF NOT EXISTS idx_decision_freezes_status
    ON decision_freezes (status, freeze_id);
