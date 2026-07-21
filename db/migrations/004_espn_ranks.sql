-- db/migrations/004_espn_ranks.sql
-- Item 2.1: the ESPN default draft board (editorial PPR ranks + native ADP).
-- One row = one player's snapshot of what the room SEES when it opens ESPN.
-- The board is LIVE/MUTABLE (ESPN re-ranks daily up to the draft), so a pull is
-- stamped knowable_as_of = retrieved_as_of = the pull day; a backtest reads it
-- through base.latest_truth(get_espn_draft_ranks). PK includes retrieved_as_of so
-- successive pulls of the same board coexist as versions.
--
-- TWO distinct ESPN signals are captured (design D9):
--   * overall_rank / espn_pos_rank  <- draftRanksByRankType["PPR"]["rank"]
--       (rankSourceId=0): ESPN's own EDITORIAL board rank, the default
--       recommendation the room sees. PRIMARY signal.
--   * adp / espn_adp_pos_rank        <- ownership.averageDraftPosition:
--       the crowdsourced ADP of what ESPN drafters actually DO. SECONDARY lens.
-- Both positional ranks are DERIVED at ingest by ordering within position (skill
-- keyed by espn_id; DST keyed by team abbr — DST rows carry synthetic negative
-- ESPN ids and leave espn_id NULL, joined downstream by team).

CREATE TABLE IF NOT EXISTS espn_draft_ranks (
    -- board_key is the non-null temporal/identity key: str(espn_id) for skill,
    -- team abbr for DST. It exists because espn_id is NULL for DST (design
    -- contract: DST join by team), and select_as_of's per-key MAX(retrieved)
    -- subquery equijoins on the key columns — a NULL espn_id key would never
    -- self-match, silently dropping every DST row from an as-of read. board_key
    -- carries the stored espn_id column unchanged for skill and never collides
    -- (numeric id string vs 2-3 letter abbr). (Deviation from the design's bare
    -- column list, documented; keeps espn_id NULL for DST as specified.)
    board_key         TEXT NOT NULL,
    espn_id           TEXT,                -- str(player id) for skill; NULL for DST (synthetic negative id)
    player            TEXT,                -- display name (fullName)
    position          TEXT NOT NULL,       -- QB/RB/WR/TE/K/D/ST via DEFPOS (defaultPositionId)
    team              TEXT,                -- PRO_TEAM_MAP then TEAM_ALIASES; the DST join key
    season            INTEGER NOT NULL,
    overall_rank      INTEGER,             -- editorial PPR board rank (across all players)
    espn_pos_rank     INTEGER,             -- derived: within-position rank by editorial rank (PRIMARY)
    adp               REAL,                -- native ESPN averageDraftPosition (crowd ADP)
    espn_adp_pos_rank INTEGER,             -- derived: within-position rank by ADP (SECONDARY, D9)
    retrieved_as_of   TEXT NOT NULL,
    knowable_as_of    TEXT NOT NULL,       -- = retrieved_as_of (live mutable board)
    PRIMARY KEY (season, board_key, retrieved_as_of)
);
CREATE INDEX IF NOT EXISTS idx_espn_draft_ranks_lookup
    ON espn_draft_ranks (season, position, knowable_as_of, retrieved_as_of);
