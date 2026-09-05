-- db/migrations/015_ff_opportunity.sql
-- Item 4.2b (S10 capture): ffverse `ff_opportunity` expected-points weekly panel.
--
-- WHAT THIS SOURCE IS. `ffopportunity` publishes, per player-week, what a
-- player's OPPORTUNITY was worth — expected receptions/yards/touchdowns/points
-- fitted from play context — beside what he actually did, and the difference
-- between them. It is the only in-season TD-regression input this project has;
-- item 4.1 recorded it as "not built" and item 4.2 named it as the thing a
-- future search would need a NEW source for.
--
-- WHY IT IS CAPTURED FORWARD-ONLY, AND WHY THAT NEEDED A MIGRATION AT ALL.
-- `refresh.BACKFILL_EXCLUDED['ff_opportunity']` records the measurement: the
-- release tag is `latest-data` and every season's file is REWRITTEN IN PLACE on
-- a game-window cron (ep_weekly_2025.parquet was rewritten 2026-09-01 and again
-- 2026-09-04). So this is a MUTABLE CURRENT-VALUE source in the same class as
-- `espn_ranks` — what it said last Tuesday is gone unless we stored it — and a
-- past-season pull would file today's numbers under a completed season. The
-- BACKFILL_EXCLUDED entry stays and `decide()` refuses any past season, not
-- overridable by --force. This table therefore only ever accumulates FORWARD.
--
-- THE STAMPS, and the one that is easy to get wrong.
--   * retrieved_as_of = the capture day, AND IT IS IN THE PRIMARY KEY. A rewrite
--     of the same player-week therefore lands as a NEW VERSION beside the old
--     one rather than replacing it; the whole reason to capture a mutable source
--     daily is to be able to diff the versions afterwards. (Day-granular: two
--     pulls on one day do overwrite each other, which is the same trade every
--     other source here makes.)
--   * knowable_as_of = THE ROW'S OWN GAMEDAY, joined from `schedules` on
--     `game_id` (measured 2026-09-04: 285/285 distinct game_ids and 6,054/6,054
--     rows of ep_weekly_2025 resolve). NOT the file's publish timestamp: that is
--     ONE value for the whole season, so every row of 2026 — week 1 included —
--     would carry the newest date and the season would read EMPTY at any earlier
--     as_of, which is the manufactured-leak shape Rule 1 exists to prevent. A row
--     whose game_id cannot be resolved to a gameday is DROPPED and counted, never
--     stored with a guessed or NULL knowledge time.
--
-- READ IT THROUGH `base.latest_truth`. Like fpecr/sleeper_ownership this is a
-- capture archive: the rows for a completed week carry the pull day, so a
-- `historical` read at that week's own as_of correctly returns nothing.
--
-- THE COLUMN SET IS LEAN ON PURPOSE, AND THE DROPPED COLUMNS ARE NOT LOST.
-- Upstream ships 159 columns; 25 are stored (identity + raw usage + the `_exp`
-- expected values + one `_diff` + the three team denominators a share is
-- computed against). Measured on the real 2025 file: 963 B/row across all 159
-- columns against 196 B/row on these 25, i.e. ~346 MB vs ~70 MB for a season
-- captured daily. Because a perishable vintage CANNOT be re-fetched, a column
-- dropped today is unrecoverable — so `ff_opportunity.pull_ff_opportunity`
-- keeps the WHOLE downloaded parquet, unmodified, as a dated lossless mirror
-- under the gitignored `data/ffopp/` (1.1 MB per capture). The column choice is
-- reversible for 1.1 MB a day; the capture is not reversible at all.
--
-- RULE 2 — READ THIS BEFORE USING `total_fantasy_points*`. Those two columns are
-- **ffverse's own full-PPR scoring**, not this league's house scoring (measured:
-- 1.0/reception on 2,711 pure-receiving rows, 0.1/yd, 6/TD — no distance kicker,
-- no dual D/ST brackets, and this league's D/ST and K do not appear here at all).
-- They are stored because the DIFF (actual minus expected) is the regression
-- signal and both halves must be in the same currency for it to mean anything.
-- Nothing may price a decision off them: house points come from
-- `ziggurat/core/scoring.py` and nowhere else. A Week-1 scope fence backs this
-- up — `tests/test_nfl_ff_opportunity.py` asserts that NO module under
-- `ziggurat/core/` imports this source at all.
--
-- COVERAGE IS OPPORTUNITY-GATED: only players with at least one attempt appear
-- (~300 rows per REG week), so an ABSENT row means "no opportunity", never a
-- zero. Rows with a NULL player_id (423 of 6,054 in 2025) are unattributed
-- plays and are filtered at ingest — they can never be joined to anything.
--
-- No BEGIN/COMMIT and no schema_version write here: `store.apply_schema` wraps
-- this script in a transaction and stamps version 15.

CREATE TABLE IF NOT EXISTS ffopp_weekly (
    -- Identity. `player_id` is upstream's own column name and holds a GSIS id
    -- (measured: 5,631/5,631 match 00-00\d{5}, and 100% of the 5,373 REG
    -- (player_id, week) keys join weekly_stats 2025 REG). It is NOT renamed to
    -- gsis_id: the lossless mirror beside this table carries the upstream name,
    -- and a reader comparing the two should not have to hold a rename in their
    -- head. (season, week, player_id) is unique in the shipped file.
    player_id            TEXT NOT NULL,
    season               INTEGER NOT NULL,
    week                 INTEGER NOT NULL,
    -- The game this row's opportunity happened in, and the JOIN KEY that stamps
    -- knowable_as_of. Kept so the stamp is re-derivable from the stored row.
    game_id              TEXT NOT NULL,
    posteam              TEXT,          -- upstream's abbrs match schedules exactly
    full_name            TEXT,
    position             TEXT,          -- upstream's own label; NOT filtered here

    -- RAW USAGE. Verified equal to the columns item 3.3's usage arm already
    -- reads (ffopp rec_attempt == weekly_stats.targets exactly, rush_attempt ==
    -- carries exactly, rec_air_yards/rec_air_yards_team == air_yards_share
    -- exactly), so this source CANNOT improve that arm — it is stored as the
    -- denominator context for the expected values below, not as a second
    -- opinion about usage.
    pass_attempt         REAL,
    rec_attempt          REAL,
    rush_attempt         REAL,
    rec_air_yards        REAL,

    -- THE EXPECTED VALUES — the entire value-add of this source. Each is what a
    -- player's opportunity was worth under ffverse's PINNED 2006-2020 model fit
    -- (`model_version` below), not a per-season refit.
    receptions_exp           REAL,
    rec_yards_gained_exp     REAL,
    rush_yards_gained_exp    REAL,
    rec_touchdown_exp        REAL,
    rush_touchdown_exp       REAL,
    pass_touchdown_exp       REAL,
    total_touchdown_exp      REAL,
    total_yards_gained_exp   REAL,
    -- FULL-PPR ffverse points, NOT house points. See the Rule 2 note above.
    total_fantasy_points_exp REAL,
    total_fantasy_points     REAL,
    total_fantasy_points_diff REAL,     -- actual - expected, both in ffverse PPR

    -- Team denominators, so a share is computable from the stored row alone
    -- rather than by re-deriving a team total from a partial capture.
    rec_attempt_team     REAL,
    rush_attempt_team    REAL,
    pass_attempt_team    REAL,

    -- PROVENANCE OF THE CAPTURE ITSELF. `asset_updated_at` is the GitHub release
    -- asset's own updated_at (the rewrite clock — this is how a revision is
    -- recognised as one); `source_timestamp` is the release's timestamp.txt (the
    -- publisher's own build clock); `model_version` is version.txt. The ingester
    -- REFUSES to write when model_version differs from the pin in
    -- ziggurat/data/nfl/ff_opportunity.py, so a future ffverse model bump cannot
    -- silently redefine every `_exp` column mid-season.
    asset_updated_at     TEXT,
    source_timestamp     TEXT,
    model_version        TEXT,

    retrieved_as_of      TEXT NOT NULL,   -- capture day; IN THE KEY (see header)
    knowable_as_of       TEXT NOT NULL,   -- = this row's game's gameday

    PRIMARY KEY (player_id, season, week, retrieved_as_of)
);

-- The accessor's shape: season/week slice, then the per-key MAX(retrieved_as_of)
-- resolution under the knowable/retrieved gates.
CREATE INDEX IF NOT EXISTS idx_ffopp_weekly_lookup
    ON ffopp_weekly (season, week, knowable_as_of, retrieved_as_of);
-- The capture-floor and vintage-diff shape: "what did the capture of day D hold".
CREATE INDEX IF NOT EXISTS idx_ffopp_weekly_capture
    ON ffopp_weekly (season, retrieved_as_of, week);
