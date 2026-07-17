-- Ziggurat SQLite schema. Facts live here; judgment lives in intel/ (markdown).
--
-- Conventions (see CLAUDE.md and ziggurat/data/asof.py):
--   * Knowledge time: every fact table carries TWO ISO-8601 TEXT columns
--     (lexicographic order == chronological order):
--       - knowable_as_of : when the fact became TRUE/PUBLIC — the primary
--         leakage filter (a week-N game stat is knowable on that game's date; an
--         injury report on its date_modified; a schedule at preseason release).
--       - retrieved_as_of: when WE pulled it — provenance, and it selects the
--         latest VERSION of a key (corrections/re-pulls). It does NOT gate
--         visibility: history is bulk-pulled now but a backtest as-of 2023 must
--         still see 2023-knowable facts.
--     Accessors filter on `knowable_as_of <= :as_of` (the sole leakage gate) and
--     take the greatest `retrieved_as_of` among a key's knowable rows. See
--     ziggurat/data/nfl/base.py::select_as_of (and test_asof_pattern.py for the
--     single-column projection variant where retrieval *is* the knowledge time).
--   * Every read accessor filters on an `as_of` argument and ships a leakage test.
--   * The .sqlite file itself is gitignored; only this schema is public.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '1');

-- ===================================================================
-- NFL data ingestion (item 1.4). Source: nfl_data_py / nflverse.
-- ===================================================================

-- Player cross-ID crosswalk (nfl_data_py.import_ids / DynastyProcess).
-- STABLE IDENTITY MAPPING ONLY (gsis <-> pfr <-> espn <-> sleeper <-> ...);
-- time-varying attributes (team, depth, status) live in the per-week tables.
-- D/STs and some rookies are absent here — that gap is asserted by a test and
-- handled by team-abbr keying for defenses. gsis_id is the nflverse spine id.
CREATE TABLE IF NOT EXISTS players (
    gsis_id         TEXT NOT NULL,
    pfr_id          TEXT,
    espn_id         TEXT,
    sleeper_id      TEXT,
    yahoo_id        TEXT,
    mfl_id          TEXT,
    fantasypros_id  TEXT,
    sportradar_id   TEXT,
    name            TEXT,
    merge_name      TEXT,
    position        TEXT,
    birthdate       TEXT,
    retrieved_as_of TEXT NOT NULL,
    knowable_as_of  TEXT NOT NULL,
    PRIMARY KEY (gsis_id, retrieved_as_of)
);

-- Game schedule / structure (import_schedules). Structural fields (matchup,
-- venue, rest) are knowable at preseason release; scores/odds/actual weather are
-- deliberately NOT stored here (odds + weather are item 1.5, post-game actuals
-- would leak). This table is also the canonical (season,week,team)->gameday
-- source used to stamp knowable_as_of on the post-game tables below.
CREATE TABLE IF NOT EXISTS schedules (
    game_id         TEXT NOT NULL,
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    game_type       TEXT,
    gameday         TEXT,      -- ISO date the game is played
    weekday         TEXT,
    gametime        TEXT,
    away_team       TEXT,
    home_team       TEXT,
    location        TEXT,
    roof            TEXT,
    surface         TEXT,
    stadium_id      TEXT,
    stadium         TEXT,
    away_rest       INTEGER,
    home_rest       INTEGER,
    div_game        INTEGER,
    retrieved_as_of TEXT NOT NULL,
    knowable_as_of  TEXT NOT NULL,
    PRIMARY KEY (game_id, retrieved_as_of)
);

-- Weekly player box score + usage (import_weekly_data). knowable_as_of = the
-- player's team gameday for that season/week. Stat keys match scoring.py inputs
-- (nflverse naming) so a row scores directly, incl. the three fumble components
-- and the three 2-pt conversions.
CREATE TABLE IF NOT EXISTS weekly_stats (
    player_id                 TEXT NOT NULL,   -- gsis_id
    season                    INTEGER NOT NULL,
    week                      INTEGER NOT NULL,
    season_type               TEXT,
    position                  TEXT,
    recent_team               TEXT,
    opponent_team             TEXT,
    -- passing
    completions               REAL,
    attempts                  REAL,
    passing_yards             REAL,
    passing_tds               REAL,
    interceptions             REAL,
    sacks                     REAL,
    sack_fumbles_lost         REAL,
    passing_air_yards         REAL,
    passing_epa               REAL,
    passing_2pt_conversions   REAL,
    -- rushing
    carries                   REAL,
    rushing_yards             REAL,
    rushing_tds               REAL,
    rushing_fumbles_lost      REAL,
    rushing_epa               REAL,
    rushing_2pt_conversions   REAL,
    -- receiving
    receptions                REAL,
    targets                   REAL,
    receiving_yards           REAL,
    receiving_tds             REAL,
    receiving_fumbles_lost    REAL,
    receiving_air_yards       REAL,
    receiving_epa             REAL,
    receiving_2pt_conversions REAL,
    -- usage shares
    target_share              REAL,
    air_yards_share           REAL,
    wopr                      REAL,
    special_teams_tds         REAL,
    fantasy_points_ppr        REAL,   -- nflverse's own PPR (cross-check vs scoring.py)
    retrieved_as_of           TEXT NOT NULL,
    knowable_as_of            TEXT NOT NULL,
    PRIMARY KEY (player_id, season, week, retrieved_as_of)
);

-- Snap counts (import_snap_counts). Keyed on PFR id; gsis_id is resolved at
-- ingest via the players crosswalk (NULL if unresolved) so it joins to
-- weekly_stats. knowable_as_of = team gameday.
CREATE TABLE IF NOT EXISTS snap_counts (
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
    PRIMARY KEY (pfr_player_id, season, week, retrieved_as_of)
);

-- Next Gen Stats — expected/advanced metrics (import_ngs_data). One table per
-- type; keyed on gsis id. knowable_as_of = team gameday.
CREATE TABLE IF NOT EXISTS ngs_receiving (
    player_gsis_id                       TEXT NOT NULL,
    season                               INTEGER NOT NULL,
    week                                 INTEGER NOT NULL,
    team_abbr                            TEXT,
    avg_cushion                          REAL,
    avg_separation                       REAL,
    avg_intended_air_yards               REAL,
    percent_share_of_intended_air_yards  REAL,
    catch_percentage                     REAL,
    avg_yac                              REAL,
    avg_expected_yac                     REAL,
    avg_yac_above_expectation            REAL,
    retrieved_as_of                      TEXT NOT NULL,
    knowable_as_of                       TEXT NOT NULL,
    PRIMARY KEY (player_gsis_id, season, week, retrieved_as_of)
);

CREATE TABLE IF NOT EXISTS ngs_rushing (
    player_gsis_id                    TEXT NOT NULL,
    season                            INTEGER NOT NULL,
    week                              INTEGER NOT NULL,
    team_abbr                         TEXT,
    efficiency                        REAL,
    percent_attempts_gte_eight_defenders REAL,
    avg_time_to_los                   REAL,
    expected_rush_yards               REAL,
    rush_yards_over_expected          REAL,
    rush_yards_over_expected_per_att  REAL,
    rush_pct_over_expected            REAL,
    retrieved_as_of                   TEXT NOT NULL,
    knowable_as_of                    TEXT NOT NULL,
    PRIMARY KEY (player_gsis_id, season, week, retrieved_as_of)
);

CREATE TABLE IF NOT EXISTS ngs_passing (
    player_gsis_id                          TEXT NOT NULL,
    season                                  INTEGER NOT NULL,
    week                                    INTEGER NOT NULL,
    team_abbr                               TEXT,
    avg_time_to_throw                       REAL,
    avg_intended_air_yards                  REAL,
    avg_air_yards_differential              REAL,
    aggressiveness                          REAL,
    expected_completion_percentage          REAL,
    completion_percentage_above_expectation REAL,
    retrieved_as_of                         TEXT NOT NULL,
    knowable_as_of                          TEXT NOT NULL,
    PRIMARY KEY (player_gsis_id, season, week, retrieved_as_of)
);

-- Depth charts (import_depth_charts). Forward-looking weekly data with no
-- publish timestamp: knowable_as_of = the week's first gameday (leakage-safe —
-- never earlier than the week's own games; refined post-Week-1 if needed).
CREATE TABLE IF NOT EXISTS depth_charts (
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
    PRIMARY KEY (season, week, club_code, formation, position, depth_position, gsis_id, retrieved_as_of)
);

-- Injury reports (import_injuries). date_modified IS the report's knowledge
-- time -> knowable_as_of. Practice-participation trajectory + game status.
CREATE TABLE IF NOT EXISTS injuries (
    gsis_id                   TEXT NOT NULL,
    season                    INTEGER NOT NULL,
    week                      INTEGER NOT NULL,
    team                      TEXT,
    position                  TEXT,
    full_name                 TEXT,
    report_status             TEXT,
    report_primary_injury     TEXT,
    report_secondary_injury   TEXT,
    practice_status           TEXT,
    practice_primary_injury   TEXT,
    practice_secondary_injury TEXT,
    date_modified             TEXT,
    retrieved_as_of           TEXT NOT NULL,
    knowable_as_of            TEXT NOT NULL,
    PRIMARY KEY (gsis_id, season, week, retrieved_as_of)
);
