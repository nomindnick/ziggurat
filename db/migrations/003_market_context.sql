-- db/migrations/003_market_context.sql
-- Item 1.5: projections, ADP/market rankings, Vegas odds, weather, and the
-- D/ST team-defense stat grid. Odds + actual weather were DELIBERATELY excluded
-- from the schedules table (see db/schema.sql comment) and land here so a
-- pre-game read cannot see a value stamped at the preseason anchor.
-- Every table carries the two knowledge-time columns; PK includes
-- retrieved_as_of so revisions coexist.

-- ------------------------------------------------------------------ team_defense
-- One row = a defense's (season, week, team) fantasy-scoring line. Columns are
-- named to match scoring.py _DST_EVENT_WEIGHTS + bracket inputs so dict(row)
-- scores directly through score_dst. knowable_as_of = the team's own gameday.
CREATE TABLE IF NOT EXISTS team_defense (
    season            INTEGER NOT NULL,
    week              INTEGER NOT NULL,
    team              TEXT NOT NULL,      -- the DEFENSE (D/ST owner), schedules-style abbr
    season_type       TEXT,               -- REG | POST
    opponent_team     TEXT,
    game_id           TEXT,
    -- scoring-ready keys (consumed by score_dst / score("DST", row)):
    sacks             REAL,
    def_interceptions REAL,
    fumble_recoveries REAL,
    safeties          REAL,
    blocked_kicks     REAL,
    def_tds           REAL,
    points_allowed    REAL,               -- v1 = opponent final score (3.8 refines)
    yards_allowed     REAL,               -- opp net total yards
    -- audit / re-derivation provenance (ignored by scoring — unknown keys skipped):
    team_score        INTEGER,            -- points this team scored
    opp_score         INTEGER,            -- audit-only; == points_allowed in v1
    retrieved_as_of   TEXT NOT NULL,
    knowable_as_of    TEXT NOT NULL,
    PRIMARY KEY (season, week, team, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_team_defense_lookup
    ON team_defense (season, week, team, knowable_as_of, retrieved_as_of);
-- NOTE: one_point_safeties / two_point_returns intentionally OMITTED (no source
-- column; score_dst treats an absent LINEAR key as 0 — correct for these exotics).
-- points_allowed & yards_allowed are ALWAYS derived for a played game; the
-- ingester DROPS (never NULL-inserts) any row whose score/opponent join fails, so
-- a NULL bracket input can never silently suppress a bracket in score_dst.

-- --------------------------------------------------------------------- game_odds
-- Closing line per game (one value, no intraday history). knowable_as_of =
-- gameday (knowable no earlier than kickoff). Kept OUT of the schedules table.
CREATE TABLE IF NOT EXISTS game_odds (
    game_id          TEXT NOT NULL,       -- joins schedules.game_id
    season           INTEGER NOT NULL,
    week             INTEGER NOT NULL,
    home_team        TEXT,
    away_team        TEXT,
    spread_line      REAL,                -- home perspective: positive = home favored
    total_line       REAL,                -- game over/under total
    home_moneyline   INTEGER,
    away_moneyline   INTEGER,
    home_spread_odds INTEGER,
    away_spread_odds INTEGER,
    over_odds        INTEGER,
    under_odds       INTEGER,
    retrieved_as_of  TEXT NOT NULL,
    knowable_as_of   TEXT NOT NULL,       -- = gameday
    PRIMARY KEY (game_id, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_game_odds_lookup
    ON game_odds (season, week, knowable_as_of, retrieved_as_of);
-- Odds columns may be NULL for unplayed in-season games; the ingester KEEPS
-- null-odds rows (only null-gameday rows are dropped).

-- ------------------------------------------------------------------ game_weather
-- Per-game weather CONTEXT (not a scoring input). forecast_source labels every
-- row so a consumer can never mistake an ERA5 actual for a forecast, and each
-- (game_id, forecast_source) keeps its own version timeline.
CREATE TABLE IF NOT EXISTS game_weather (
    game_id          TEXT NOT NULL,       -- joins schedules.game_id
    season           INTEGER NOT NULL,
    week             INTEGER NOT NULL,
    home_team        TEXT,
    stadium_id       TEXT,                -- reference-table key -> lat/long/tz/dome
    kickoff_local    TEXT,                -- ISO datetime, stadium-local (context)
    forecast_source  TEXT NOT NULL,       -- 'forecast' (live: knowable=pull day)
                                          --  | 'archive_actual' (ERA5: knowable=gameday)
    weather_relevant INTEGER NOT NULL,    -- 0 = fixed dome; 1 = outdoor/retractable
    temp_f           REAL,                -- game-hour temperature (°F)
    wind_mph         REAL,                -- game-hour wind (mph)
    precip_mm        REAL,                -- game-hour precipitation (mm)
    precip_prob      REAL,                -- forecast only; NULL for archive_actual
    retrieved_as_of  TEXT NOT NULL,
    knowable_as_of   TEXT NOT NULL,
    PRIMARY KEY (game_id, forecast_source, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_game_weather_lookup
    ON game_weather (season, week, game_id, knowable_as_of, retrieved_as_of);
-- humidity dropped from v1 (relative_humidity_2m is not in the fetched hourly
-- params; add both together later if a consumer needs it).

-- ------------------------------------------------------------------- projections
-- Forward weekly stat line, stored under scoring.py canonical keys so a row
-- scores directly. Provider label 'sleeper_rotowire' (single provider = Rotowire,
-- NOT a consensus). Post-game actuals must NEVER land here.
CREATE TABLE IF NOT EXISTS projections (
    source            TEXT NOT NULL,       -- 'sleeper_rotowire'
    source_player_id  TEXT NOT NULL,       -- Sleeper player_id (or team abbr for DEF)
    gsis_id           TEXT,                -- via players.sleeper_id crosswalk; NULL for DEF/unresolved
    season            INTEGER NOT NULL,
    week              INTEGER NOT NULL,
    season_type       TEXT,                -- 'regular'
    position          TEXT,                -- QB/RB/WR/TE/K/DEF
    team              TEXT,
    opponent          TEXT,
    -- Offense (scoring.py canonical keys)
    passing_yards             REAL,
    passing_tds               REAL,
    interceptions             REAL,
    rushing_yards             REAL,
    rushing_tds               REAL,
    receptions                REAL,
    receiving_yards           REAL,
    receiving_tds             REAL,
    fumbles_lost              REAL,        -- pre-summed alias scoring.py accepts
    passing_2pt_conversions   REAL,
    rushing_2pt_conversions   REAL,
    receiving_2pt_conversions REAL,
    -- Kicker (scoring.py bucket-count keys)
    fg_made_0_39      REAL,
    fg_made_40_49     REAL,
    fg_made_50_59     REAL,                -- LOSSY: absorbs 60+ (source splits only at 50p)
    fg_made_60        REAL,                -- source cannot fill; NULL
    pat_made          REAL,
    fg_missed         REAL,                -- derived: fga - fgm (flat -1/miss)
    -- D/ST (scoring.py event keys + bracket inputs)
    sacks             REAL,
    def_interceptions REAL,
    fumble_recoveries REAL,
    safeties          REAL,
    blocked_kicks     REAL,
    def_tds           REAL,                -- def_td + st_td (do NOT add component TD fields)
    points_allowed    REAL,               -- bracket input; absent => NULL, never 0
    yards_allowed     REAL,               -- bracket input; absent => NULL, never 0
    projected_points  REAL,               -- source pts_ppr, cross-check ONLY (never a scoring input)
    retrieved_as_of   TEXT NOT NULL,
    knowable_as_of    TEXT NOT NULL,
    PRIMARY KEY (source, source_player_id, season, week, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_projections_lookup
    ON projections (season, week, position, knowable_as_of, retrieved_as_of);

-- ------------------------------------------------------------------ adp_rankings
-- Market consensus rankings (FantasyPros ECR via load_ff_rankings). ECR is the
-- market rank; true draft ADP (FFC/MFL) is a deferred optional enrichment.
-- DST carry a fantasypros id but NO gsis_id -> align by normalized team abbr.
CREATE TABLE IF NOT EXISTS adp_rankings (
    fantasypros_id   TEXT NOT NULL,        -- ff_rankings.id coerced to TEXT
    gsis_id          TEXT,                 -- via players crosswalk; NULL for DST / unresolved
    espn_id          TEXT,                 -- via players crosswalk; the ESPN-side join key
    player           TEXT,
    position         TEXT NOT NULL,        -- QB/RB/WR/TE/K/DST (IDP dropped at ingest)
    team             TEXT,                 -- normalized via TEAM_ALIASES; join key for DST
    ecr_type         TEXT NOT NULL,        -- 'ro' redraft-overall, 'rp' positional, ...
    ecr              REAL,                 -- expert consensus rank (lower = better)
    sd               REAL,                 -- consensus dispersion
    best             INTEGER,
    worst            INTEGER,
    pos_rank         INTEGER,              -- derived at ingest by ecr order over league positions
    player_owned_avg REAL,                 -- FantasyPros ownership (fully populated; usable)
    season           INTEGER,              -- from scrape_date year
    scrape_date      TEXT,                 -- FantasyPros scrape day; == knowable_as_of
    retrieved_as_of  TEXT NOT NULL,
    knowable_as_of   TEXT NOT NULL,        -- = scrape_date
    PRIMARY KEY (fantasypros_id, ecr_type, scrape_date, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_adp_rankings_lookup
    ON adp_rankings (season, position, ecr_type, knowable_as_of, retrieved_as_of);
