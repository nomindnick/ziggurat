-- Access-path indexes for repeated as-of queries and future backtest replay.
-- Natural-key primary keys already support the correlated version lookup;
-- these indexes reduce the outer scan for the filters each accessor exposes.

CREATE INDEX IF NOT EXISTS idx_players_temporal
    ON players (knowable_as_of, retrieved_as_of);

CREATE INDEX IF NOT EXISTS idx_schedules_lookup
    ON schedules (season, week, knowable_as_of, retrieved_as_of);

CREATE INDEX IF NOT EXISTS idx_weekly_stats_lookup
    ON weekly_stats (season, week, position, knowable_as_of, retrieved_as_of);

CREATE INDEX IF NOT EXISTS idx_snap_counts_lookup
    ON snap_counts (season, week, knowable_as_of, retrieved_as_of);

CREATE INDEX IF NOT EXISTS idx_ngs_receiving_lookup
    ON ngs_receiving (season, week, knowable_as_of, retrieved_as_of);

CREATE INDEX IF NOT EXISTS idx_ngs_rushing_lookup
    ON ngs_rushing (season, week, knowable_as_of, retrieved_as_of);

CREATE INDEX IF NOT EXISTS idx_ngs_passing_lookup
    ON ngs_passing (season, week, knowable_as_of, retrieved_as_of);

CREATE INDEX IF NOT EXISTS idx_depth_charts_lookup
    ON depth_charts (season, week, club_code, knowable_as_of, retrieved_as_of);

CREATE INDEX IF NOT EXISTS idx_injuries_lookup
    ON injuries (season, week, gsis_id, knowable_as_of, retrieved_as_of);
