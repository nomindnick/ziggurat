-- db/migrations/011_fpecr_panel.sql
-- The DynastyProcess `db_fpecr` weekly FantasyPros ECR panel — spike 1.2's
-- PRIMARY point-in-time market series, and the ONLY external ground truth this
-- project has for seasons it did not live through.
--
-- WHY A NEW TABLE INSTEAD OF `adp_rankings` (003). `adp_rankings` already holds
-- FantasyPros ECR and its own docstring invites "the Phase-4 db_fpecr backfill
-- into this table". Measured, that would SILENTLY DESTROY DATA: its primary key
-- is (fantasypros_id, ecr_type, scrape_date, retrieved_as_of), and the archive
-- ships several DIFFERENT ranking PAGES under one `ecr_type` on one
-- `scrape_date` — `ro` is both `ppr-cheatsheets` (the frozen PRESEASON draft
-- board) and `ros-ppr-overall` (the live REST-OF-SEASON board). Those are two
-- different market facts about the same player on the same day. On the shipped
-- archive 38 such key groups collide in `ro` alone; folded onto that key,
-- `INSERT OR REPLACE` keeps whichever the loader happened to hand SQLite last,
-- and a "preseason board" read afterwards can be a mid-season ROS ranking with
-- nothing reporting the substitution. That is the exact hazard
-- `intel/research/market-archives.md` flags for the Wayback source ("the ROS and
-- cheatsheet pages sit at week=0 frozen preseason values even in mid-season
-- captures — a real leakage hazard"). So `fp_page` is IN THE PRIMARY KEY here.
--
-- Two further reasons the panel is its own table rather than rows in
-- `adp_rankings`:
--   * `adp_rankings` is PERISHABLE and CURRENT-ONLY (FantasyPros serves today's
--     scrape; a missed day is a lost observation). This panel is BULK IMMUTABLE
--     HISTORY — one file, re-downloadable in full, empirically never revised in
--     place (spike 1.2 diffed a two-year-older copy of the parquet: zero
--     in-place revisions across 1,261,623 shared rows). Mixing a perishable
--     source and a replayable one in one table makes `ingest status`'s
--     recoverable/unrecoverable split unstatable for that table.
--   * this table carries an INFERRED `nfl_week`, which `adp_rankings` has no
--     column for and no business acquiring.
--
-- THE WEEK IS INFERRED, AND THAT IS THE SOURCE'S SHARPEST TRAP. The archive
-- carries `scrape_date` and NO NFL week; `market-archives.md` names a silent
-- off-by-one as the failure that "would corrupt every result". The inference is
-- therefore NOT the cadence-counting the note contemplated. It is a join to the
-- already-ingested `schedules` table: a scrape belongs to the earliest REG week
-- whose LAST gameday is on or after the scrape date. Keying on the last gameday
-- rather than the first is the whole fix — a Friday scrape sits AFTER that
-- week's Thursday-night opener, so "first week whose first gameday is still
-- ahead" returns week N+1 for exactly the Friday scrapes the archive is made of.
-- `week_basis` records HOW each row's week was decided, so "week 0 = preseason"
-- is never confused with "we could not tell" (a NULL under `no_schedule`).
--
-- knowable_as_of = scrape_date. FantasyPros publishes the scrape that day, so
-- stamping the scrape day is leakage-safe and is what `adp_rankings` already
-- does. retrieved_as_of = the day this system pulled the parquet; it is in the
-- PRIMARY KEY so a later re-pull coexists as a version rather than overwriting.
--
-- No BEGIN/COMMIT and no schema_version write here: `store.apply_schema` wraps
-- this script in a transaction and stamps version 11.

CREATE TABLE IF NOT EXISTS fpecr_panel (
    -- Identity. `fantasypros_id` is the bare digit string, normalized the same
    -- way `players.fantasypros_id` and `adp_rankings.fantasypros_id` are, so the
    -- crosswalk join is a plain equality. DST rows carry a FantasyPros TEAM id
    -- that is absent from the player crosswalk; they keep a NULL gsis_id and are
    -- joined downstream by normalized team abbr (the 003 contract).
    fantasypros_id  TEXT NOT NULL,

    -- WHICH ranking series this row belongs to. `ecr_type` is upstream's coarse
    -- label (ro = redraft overall, rp = redraft positional, wp = weekly
    -- positional, plus dynasty/best-ball/superflex variants this ingester
    -- filters out). `fp_page` is the SPECIFIC page, normalized from upstream's
    -- two spellings ('/nfl/rankings/ppr-cheatsheets.php' and bare
    -- 'ppr-cheatsheets') to the bare form. See the header: `fp_page` is in the
    -- key because one `ecr_type` spans several pages on one day.
    ecr_type        TEXT NOT NULL,
    fp_page         TEXT NOT NULL,

    scrape_date     TEXT NOT NULL,      -- upstream's own immutable snapshot key

    -- The NFL season this scrape describes, from `asof.nfl_season_of` (the
    -- league year turns over in mid-March, so a January scrape belongs to the
    -- PREVIOUS season — which is exactly when the fantasy playoffs are graded).
    season          INTEGER NOT NULL,

    -- The inferred NFL week (see header). 0 = the scrape predates week 1's first
    -- kickoff, i.e. a PRESEASON board. NULL = not derivable; `week_basis` says
    -- which kind of not-derivable.
    nfl_week        INTEGER,
    --   'schedules'   — joined to an ingested REG schedule (nfl_week is 0..N)
    --   'after_season'— later than that season's last REG gameday (NULL week)
    --   'no_schedule' — that season's schedules rows are not ingested (NULL week)
    week_basis      TEXT NOT NULL,

    player          TEXT,
    position        TEXT NOT NULL,      -- QB/RB/WR/TE/K/DST (IDP dropped at ingest)
    team            TEXT,               -- through base.TEAM_ALIASES; 'FA' kept verbatim

    -- Crosswalk, resolved at ingest from the players table. NULL is KEPT, never
    -- dropped (the 003 rule): an unresolved id is still a real market fact.
    gsis_id         TEXT,
    espn_id         TEXT,

    -- The consensus and its DISPERSION. `sd`/`best`/`worst` are why this source
    -- beats a bare ADP: they say how contested a player was, which is the input
    -- a risk term needs.
    ecr             REAL,
    sd              REAL,
    best            REAL,
    worst           REAL,
    player_owned_avg   REAL,
    player_owned_espn  REAL,

    -- DERIVED at ingest, over the LEAGUE positions only, so an IDP row can never
    -- sit "ahead" of a startable player in either number:
    --   page_rank — 1..n by ecr within (ecr_type, fp_page, scrape_date). On the
    --               overall cheatsheet this IS the draft board rank.
    --   pos_rank  — 1..n by ecr within that page AND position.
    -- Both are contiguous by construction (duplicates are folded before ranking)
    -- and are asserted so at ingest: a hole shifts every player below it by one.
    page_rank       INTEGER NOT NULL,
    pos_rank        INTEGER NOT NULL,

    retrieved_as_of TEXT NOT NULL,
    knowable_as_of  TEXT NOT NULL,      -- = scrape_date

    PRIMARY KEY (fantasypros_id, ecr_type, fp_page, scrape_date, retrieved_as_of)
);

-- The accessor's shape: pick a season + series, then resolve the per-key
-- MAX(retrieved_as_of) under the knowable/retrieved gates.
CREATE INDEX IF NOT EXISTS idx_fpecr_panel_lookup
    ON fpecr_panel (season, ecr_type, fp_page, scrape_date, knowable_as_of, retrieved_as_of);
-- The weekly-panel shape a Phase-4 lead-time study reads: one player's series.
CREATE INDEX IF NOT EXISTS idx_fpecr_panel_player
    ON fpecr_panel (gsis_id, season, nfl_week, scrape_date);
