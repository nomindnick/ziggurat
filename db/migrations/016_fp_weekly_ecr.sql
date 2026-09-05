-- db/migrations/016_fp_weekly_ecr.sql
-- Item 4.2b (Unit F): the SAME-WEEK FantasyPros weekly ECR board, captured daily.
--
-- THE QUESTION THIS ANSWERS. Item 4.2b's own recon question was "can same-week
-- `wp` ECR be captured on Mon/Tue/Wed from the free page?" — because every
-- market number this project holds for a live Tuesday is either a WEEKLY-FRIDAY
-- draft/ROS board (`adp_rankings`) or a BULK ARCHIVE that is 2021-2025 only
-- (`fpecr_panel`, whose 2026 `wp` row count is ZERO and whose next scheduled
-- pull is ~2026-09-30). Measured: DynastyProcess publishes
-- `files/fp_latest_weekly.csv` TWICE DAILY in-season ("Daily FP scrape"), it is
-- the same `wp` series items 4.1/4.2 grade against, and it carries the whole
-- weekly board — so the answer is yes, and this table is where it lands.
--
-- WHY IT IS NOT `fpecr_panel` ROWS, AND THIS IS THE LOAD-BEARING DECISION.
-- `fpecr_panel` is a BACKTEST INPUT with a verified immutability story (spike
-- 1.2 diffed a two-year-older copy of the parquet: zero in-place revisions
-- across 1,261,623 shared rows). This source is the opposite: a LIVE number that
-- moves during the day — 81 of 159 ppr-rb ids changed integer rank in the 5.7
-- hours between one scrape and the next, measured 2026-09-04. Put a moving live
-- number in the archive's key space and `base.select_as_of`'s per-key
-- MAX(retrieved_as_of) resolves the live row over the archived one, so every
-- backtest read silently starts answering with today's board. Two tables, two
-- immutability stories, no shadowing.
--
-- WHY IT IS NOT `adp_rankings` ROWS EITHER. That table's key has no page column
-- and no week column, and its whole point is the DRAFT/rest-of-season board.
-- A weekly positional board is a different fact about a different horizon.
--
-- THE STAMPS.
--   * knowable_as_of = the file's own `scrape_date`. FantasyPros publishes the
--     scrape that day, so stamping the scrape day is leakage-safe — exactly what
--     `adp_rankings` (003) and `fpecr_panel` (011) already do.
--   * retrieved_as_of = the pull day, never back-stamped, and it is IN THE
--     PRIMARY KEY so a second capture of the same scrape lands as a version
--     beside the first rather than on top of it. (Day-granular, so two pulls in
--     one day DO overwrite each other: the same trade every other table here
--     makes, and upstream's twice-daily rhythm means the second pull of a day is
--     the one that survives.)
--
-- THE WEEK LABEL IS THE SHARPEST TRAP IN THIS SOURCE, AND IT IS NOT `fpecr`'s.
-- `fpecr.infer_nfl_week` was written for FRIDAY archive scrapes and short-
-- circuits to week 0 ("a preseason board") for any scrape before week 1's first
-- kickoff. Measured 2026-09-04: it returns week 0 for that day's capture while
-- the live FantasyPros page ranks WEEK 1. Calling this table's rows week 0 would
-- file a real weekly board under a week that does not exist. So the week here is
-- derived independently and `week_basis` records WHICH AUTHORITY decided it:
--   'schedules'        — the earliest REG week whose LAST gameday is on or after
--                        the scrape (the rule is exact from the season opener on;
--                        its one known soft day is the Monday a week ends, when
--                        FantasyPros may already have flipped to N+1 — recon
--                        UNKNOWN 5, one in-season observation settles it);
--   'fantasypros_page' — the FantasyPros rankings page's own `ecrData.week`,
--                        which is the AUTHORITY when it is read at all. It is
--                        behind an opt-in that DEFAULTS OFF (operator decision
--                        D2(b) is pending), so today no row carries this;
--   'unknown'          — the schedule cannot label it and the page was not read:
--                        no REG schedule ingested, or a PRE-OPENER capture (no
--                        REG game has been played, and "the first week that has
--                        not finished" answers week 1 for every day back to
--                        March), or after the season's last REG gameday.
-- `nfl_week` is NULL exactly when `week_basis` is 'unknown'. A NULL week is an
-- honest "we do not know"; week 0 would be a wrong answer wearing a number.
--
-- RULE 2. `r2p_pts` and `start_sit_grade` are FANTASYPROS' OWN projected points
-- and their own start/sit letter grade, in THEIR scoring, not this league's.
-- They are stored because they are what the market was saying — never as a
-- points input. House points come from ziggurat/core/scoring.py and nowhere
-- else, and `tests/test_nfl_fp_weekly.py` asserts no module under
-- `ziggurat/core/` imports this source in Week 1 (capture only, no integration).
--
-- RULE 5. Nothing captured from FantasyPros is committed: this table lives in
-- the gitignored database, and the committed test fixture is a trimmed copy of
-- the public NFL board (player names only, no league-private data).
--
-- No BEGIN/COMMIT and no schema_version write here: `store.apply_schema` wraps
-- this script in a transaction and stamps version 16.

CREATE TABLE IF NOT EXISTS fp_weekly_ecr (
    -- Identity. `fantasypros_id` is the bare digit string, normalized the same
    -- way `players.fantasypros_id`, `adp_rankings.fantasypros_id` and
    -- `fpecr_panel.fantasypros_id` are, so the crosswalk join is a plain
    -- equality. DST rows carry a FantasyPros TEAM id absent from the player
    -- crosswalk; they keep a NULL gsis_id and are joined downstream by
    -- normalized team abbr (the 003 contract).
    fantasypros_id  TEXT NOT NULL,

    -- WHICH weekly board this row came off: 'qb', 'ppr-rb', 'ppr-wr', 'ppr-te',
    -- 'k', 'dst' (the IDP pages 'db'/'dl'/'lb' are dropped at ingest — 996 of
    -- 1,678 rows on the 2026-09-04 scrape). IN THE PRIMARY KEY because a
    -- dual-eligibility player is published on two positional pages of one
    -- series on one day, which is two genuine market facts — the 215 measured
    -- collisions that put `fp_page` in migration 011's key.
    page            TEXT NOT NULL,

    scrape_date     TEXT NOT NULL,      -- upstream's own snapshot key

    -- The NFL season this scrape describes, from `asof.nfl_season_of` (the
    -- league year turns over in mid-March, so a January scrape belongs to the
    -- PREVIOUS season — which is when the fantasy playoffs are graded).
    season          INTEGER NOT NULL,

    -- The week this board RANKS. NULL exactly when week_basis is 'unknown'.
    -- See the header: this is NOT `fpecr.infer_nfl_week`.
    nfl_week        INTEGER,
    week_basis      TEXT NOT NULL,      -- 'schedules' | 'fantasypros_page' | 'unknown'

    player          TEXT,
    position        TEXT NOT NULL,      -- QB/RB/WR/TE/K/DST (IDP dropped at ingest)
    team            TEXT,               -- through base.TEAM_ALIASES

    -- Crosswalk, resolved at ingest from the players table. NULL is KEPT, never
    -- dropped (the 003 rule): an unresolved id is still a real market fact.
    gsis_id         TEXT,
    espn_id         TEXT,

    -- THE MARKET FACTS. `rank` is upstream's own integer rank WITHIN this page
    -- (1..n) — stored verbatim, not re-derived, because there is no IDP
    -- contamination to correct for here: one page carries exactly one position
    -- (measured, all nine pages). `ecr` is the consensus; `sd`/`best`/`worst`
    -- are its DISPERSION, which is what says how contested the ranking was.
    rank            INTEGER,
    ecr             REAL,
    sd              REAL,
    best            REAL,
    worst           REAL,

    -- Upstream's own positional LABEL, e.g. 'QB1', 'DST3'. Deliberately NOT
    -- named `pos_rank`: `adp_rankings.pos_rank` and `fpecr_panel.pos_rank` are
    -- DERIVED INTEGERS, and a same-named column of a different type in a sibling
    -- table is a join that silently never matches. The integer is `rank`.
    pos_rank_label  TEXT,

    player_owned_avg    REAL,           -- market ownership % across host sites
    -- The week-over-week ECR MOVEMENT, which is the column a same-week capture
    -- exists for. Entirely NULL on the 2026-09-04 pre-opener scrape (measured 0
    -- of 1,678); expected to populate once weeks are being ranked against a
    -- previous week.
    player_ecr_delta    REAL,
    player_opponent     TEXT,           -- as published: 'vs. TB' / 'at HOU'
    player_opponent_id  TEXT,           -- the bare abbr, the joinable form

    -- RULE 2 (see header): FantasyPros' own numbers in FantasyPros' scoring.
    start_sit_grade TEXT,               -- their letter grade, 'A+' .. 'F'
    r2p_pts         REAL,               -- their projected points — NOT house points

    -- Three columns upstream ships and has never populated in anything this
    -- project has seen (0 of 1,678 on 2026-09-04). Stored because they cost
    -- nothing and a perishable vintage cannot be re-fetched once they do.
    note            TEXT,
    tag             TEXT,
    recommendation  TEXT,

    retrieved_as_of TEXT NOT NULL,      -- pull day; IN THE KEY (see header)
    knowable_as_of  TEXT NOT NULL,      -- = scrape_date

    PRIMARY KEY (fantasypros_id, page, scrape_date, retrieved_as_of)
);

-- The accessor's shape: pick a season/week/page slice, then resolve the per-key
-- MAX(retrieved_as_of) under the knowable/retrieved gates.
CREATE INDEX IF NOT EXISTS idx_fp_weekly_ecr_lookup
    ON fp_weekly_ecr (season, nfl_week, page, scrape_date, knowable_as_of, retrieved_as_of);
-- The lead-time shape a later study reads: one player's weekly series.
CREATE INDEX IF NOT EXISTS idx_fp_weekly_ecr_player
    ON fp_weekly_ecr (gsis_id, season, nfl_week, scrape_date);
-- The collapse floor's shape: "what did the capture of day D hold, per page".
CREATE INDEX IF NOT EXISTS idx_fp_weekly_ecr_capture
    ON fp_weekly_ecr (season, retrieved_as_of, page);
