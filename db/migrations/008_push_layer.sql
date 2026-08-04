-- db/migrations/008_push_layer.sql
-- Item 3.6: the push layer — a player-news wire, a briefing/alert run log, and
-- an alert dedup ledger.
--
-- FOUR tables in ONE file, deliberately (the 007 lesson): two files numbered 008
-- make store._migrations() raise "versions must be contiguous", and apply_schema
-- runs UNCONDITIONALLY inside open_db on every command, so a split would kill
-- every CLI at startup. No BEGIN/COMMIT and no schema_version write here —
-- store.apply_schema wraps this script in a transaction and stamps version 8.
--
-- TWO KINDS of table live here, and they are NOT the same:
--   * player_news / player_news_links are FACT tables (as-of columns, read
--     through base.select_as_of, leakage-tested). News is knowable at a real
--     publish instant; a July read must not see a September note.
--   * push_runs / alert_ledger are OPERATIONAL metadata (run-log wall-clock
--     times, dedup bookkeeping). They carry NO as-of columns and are NEVER read
--     through select_as_of — exactly like league_sync_runs (005) and
--     nfl_ingest_runs (006). Mixing the two is how "when did X last run" becomes
--     a knowledge-time lie.
--
-- ALL FOUR are APPEND / UPSERT-BY-KEY. None does a delete-then-rewrite of a
-- whole partition, so the destroy-the-day floor pattern (SnapshotCollapse /
-- BoardCollapse / CrosswalkCollapse) has nothing to guard here — there is no
-- "replace the whole day" operation to fence.


-- =========================================================================
-- 1. player_news — the ESPN news wire (item 3.6 R3), one row per ARTICLE.
-- =========================================================================
-- Primary source: ESPN's public news API (site.api.espn.com/.../nfl/news). Each
-- article carries a stable integer id, an ISO-UTC `published` instant (the honest
-- knowable_as_of basis — publish time, never gameday), and a categories[] array
-- whose athlete entries carry athleteId == players.espn_id (a DIRECT join, zero
-- fuzzy matching — see player_news_links). The RotoWire RSS fallback (source
-- 'rotowire') is a labelled deferral; the `source` column is here so it can land
-- additively without a migration.
--
-- WHY published_at IS SEPARATE FROM knowable_as_of. select_as_of gates on a
-- DAY-granular knowable_as_of. News is a SPEED lane and intraday: a Sunday-2pm
-- note must not inform a Sunday-1pm lineup. So we keep the FULL-precision UTC
-- instant in published_at for intraday/backtest leakage checks (accessor exposes
-- a `published_before=` cutoff) AND the day-granular knowable_as_of for the
-- standard live as-of path. Both are leakage-tested.
--
-- retrieved_as_of is in the PK so a re-pull that carries a correction (ESPN edits
-- an article; lastModified moves) stores as a new version the select_as_of
-- "latest retrieved per key" resolution picks up — the adp_rankings convention.
CREATE TABLE IF NOT EXISTS player_news (
    source          TEXT NOT NULL,        -- 'espn' | 'rotowire' (provenance)
    news_id         TEXT NOT NULL,        -- ESPN article id (as TEXT) | RSS <guid>
    news_type       TEXT,                 -- ESPN 'type' (HeadlineNews/Story/Media) | 'note'
    headline        TEXT NOT NULL,
    body            TEXT,                  -- description / blurb (LOCAL-ONLY for copyrighted feeds)
    byline          TEXT,                  -- reporter attribution (NULL ok)
    url             TEXT,
    published_at    TEXT NOT NULL,         -- FULL UTC ISO-8601 instant (the real event time)
    knowable_as_of  TEXT NOT NULL,         -- iso_date(published_at) — the standard day gate
    retrieved_as_of TEXT NOT NULL,         -- pull day (provenance + revision key)
    PRIMARY KEY (source, news_id, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_player_news_knowable
    ON player_news (knowable_as_of, retrieved_as_of);

-- =========================================================================
-- 1b. player_news_links — the article -> N-athlete fan-out.
-- =========================================================================
-- One article can name several players; keeping the athletes in a child table
-- (rather than duplicating the blurb N times) keeps the fan-out queryable and the
-- body stored once. A link row exists only for an article's athlete categories,
-- so espn_id (the ESPN athleteId) is always present; gsis_id is resolved via the
-- players crosswalk and may be NULL (KEEP the row — an unresolved id is a fact,
-- not a reason to drop the linkage; the adp_rankings/depth-charts convention).
CREATE TABLE IF NOT EXISTS player_news_links (
    source          TEXT NOT NULL,
    news_id         TEXT NOT NULL,
    espn_id         TEXT NOT NULL,         -- ESPN athleteId == players.espn_id (direct join)
    gsis_id         TEXT,                  -- via crosswalk; NULL when unresolved (KEPT)
    player_name     TEXT,                  -- name as reported (display + resolution audit)
    team            TEXT,                  -- normalized abbr via base.TEAM_ALIASES (NULL ok)
    published_at    TEXT NOT NULL,         -- denormalized from the article (the join-time gate)
    knowable_as_of  TEXT NOT NULL,         -- denormalized: iso_date(published_at)
    retrieved_as_of TEXT NOT NULL,
    PRIMARY KEY (source, news_id, espn_id, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_player_news_links_player
    ON player_news_links (espn_id, knowable_as_of, retrieved_as_of);


-- =========================================================================
-- 2. push_runs — the briefing/alert run log (item 3.6 R5).
-- =========================================================================
-- WHY A THIRD RUN-LOG TABLE, not a reuse of league_sync_runs or nfl_ingest_runs:
-- the same argument 006 makes for itself applies again. Those tables answer
-- "when did the league / an NFL source last run" with their own count columns and
-- filters; a push row written there would corrupt that answer. push_runs is the
-- honest "when did the briefing / alert cadence last run, and did it push".
--
-- ONE table for BOTH kinds via a `kind` column (the way nfl_ingest_runs uses one
-- `source` column for 14+ pulls). SILENCE IS NOT SUCCESS: every tick writes a
-- row, including the empty ones. STATUS_EMPTY ("ran, nothing new, pushed nothing")
-- is a HEALTHY outcome for the alert kind and is the overwhelmingly common one
-- every 20 minutes — it must NOT look like a failure or trip Restart=on-failure
-- (the exact distinction 006/refresh.py already draws with PROBLEM_STATUSES).
--
-- START-BEFORE-NETWORK: a `running` row is written BEFORE the claude -p subprocess
-- or the ntfy POST (the two calls that can hang under Type=oneshot), so a crash
-- leaves a durable reapable row, not silence — the nfl_ingest_runs start/finish/
-- reap-orphan discipline.
CREATE TABLE IF NOT EXISTS push_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,          -- 'brief' | 'alert'
    season        INTEGER,
    scope         TEXT,                   -- 'week 5' (brief) | 'events since run 41' (alert)
    started_at    TEXT NOT NULL,          -- ISO datetime (run-log wall clock, never a knowledge time)
    finished_at   TEXT,
    -- running / abandoned / ok / partial / empty / failed / skipped.
    -- 'empty' is HEALTHY (alert tick with nothing new). 'partial' = ran but a
    -- section/push degraded. 'skipped' = nothing to do (e.g. pre-draft, no roster).
    -- 'abandoned' = a 'running' row a later run found orphaned (process died mid-run).
    status        TEXT NOT NULL,
    events_found  INTEGER,                -- alert kind; NULL for brief
    events_pushed INTEGER,                -- alert kind; NULL for brief
    llm_backend   TEXT,                   -- which Router backend actually served the prose
    llm_task      TEXT,                   -- the config/llm.toml task tag used
    ntfy_status   TEXT,                   -- http status | 'skipped' | 'error: ...'
    artifact_path TEXT,                   -- brief kind: the intel/weekly/ file written
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_runs_kind
    ON push_runs (kind, season, status, run_id);


-- =========================================================================
-- 3. alert_ledger — the dedup ledger (item 3.6 R5/R6).
-- =========================================================================
-- REQUIRED for idempotency: state.injury_transitions() re-emits EVERY historical
-- crossing on every call, and the alert tick runs many times a day, so without a
-- ledger the same "starter X ruled OUT" would push on every tick forever. A row
-- here means "this event has been handled on this channel"; the tick drops any
-- candidate whose (season, dedup_key, channel) is already present.
--
-- DEDUP KEY (built by the alert builder), keyed on DIRECTION not to_status:
--   injury: 'inj:<espn_player_id>:<became_knowable>:<direction>'
--   news:   'news:<source>:<news_id>'
-- Keying injuries on `direction` (ruled_out/cleared) rather than to_status means a
-- same-day OUT->INJURY_RESERVE escalation (which re-diffs to another 'ruled_out'
-- with a different to_status) does NOT double-push; to_status rides as payload.
--
-- CHANNEL-SCOPED: the full event set always lands in the gitignored intel/weekly/
-- briefing (channel 'briefing'); the phone lane ('phone') is rate-capped. The
-- same event can be briefing-recorded this tick and phone-pushed a later tick
-- (if it re-ranks past the cap) without re-writing the briefing.
--
-- RESERVE-THEN-PUSH: the tick INSERTs the ledger row (pushed_at NULL = reserved)
-- and confirms the insert won the race BEFORE the ntfy POST, then stamps pushed_at
-- after a durable push. Consequence, chosen deliberately: a crash between reserve
-- and push DROPS that one alert rather than risk a DUPLICATE push on the retry —
-- a missed push is recoverable (the fact is still in the next briefing / a manual
-- `ziggurat waivers`), a spam push is not undoable and erodes the one channel this
-- item exists to build trust in.
--
-- Operational metadata: no as-of columns, never read through select_as_of. Append
-- only; never deleted.
CREATE TABLE IF NOT EXISTS alert_ledger (
    alert_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    season          INTEGER,
    dedup_key       TEXT NOT NULL,        -- the event identity (see above)
    channel         TEXT NOT NULL,        -- 'phone' | 'briefing'
    kind            TEXT NOT NULL,        -- 'injury_out' | 'injury_back' | 'news'
    espn_player_id  TEXT,                 -- nullable; for joining/debugging only
    event_day       TEXT,                 -- became_knowable (injury) | publish date (news)
    first_seen_at   TEXT NOT NULL,        -- wall clock the tick first reserved this event
    pushed_at       TEXT,                 -- NULL = reserved/suppressed; set after a durable push
    payload_summary TEXT,                 -- short text of what was (or would have been) pushed
    UNIQUE (season, dedup_key, channel)
);
CREATE INDEX IF NOT EXISTS idx_alert_ledger_lookup
    ON alert_ledger (season, channel, dedup_key);
