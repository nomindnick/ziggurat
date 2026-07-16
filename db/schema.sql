-- Ziggurat SQLite schema. Facts live here; judgment lives in intel/ (markdown).
--
-- Conventions (see CLAUDE.md and ziggurat/data/asof.py):
--   * Knowledge time: columns named retrieved_as_of / *_as_of record when a fact
--     became knowable to us, as ISO-8601 TEXT (lexicographic order == time order).
--   * Every read accessor filters on an `as_of` argument; each accessor ships with
--     a leakage test (pattern: tests/test_asof_pattern.py).
--   * The .sqlite file itself is gitignored; only this schema is public.
--
-- Real tables (players, weekly_stats, projections, injuries, rosters, claims,
-- decisions, ...) land in Phase 1 — see IMPLEMENTATION_PLAN.md and SPEC "Data Model".

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '0');
