-- db/migrations/019_gsis_placeholder_rekey.sql
-- Item 3.18, the PAIRED REPAIR (2026-09-15): re-derive the gsis join key on rows
-- already stored under the accidental scan-order resolution, so the whole
-- database names a colliding player by ONE id.
--
-- WHY. nflverse mints a placeholder gsis (`LOV121782`) for a player it has not
-- yet reconciled, and the real one (`00-0041027`) arrives beside it. `players`
-- is keyed (gsis_id, retrieved_as_of), so such a player is TWO rows sharing one
-- espn_id and one sleeper_id. Every crosswalk that inverts that key therefore
-- meets a collision, and until today two of them resolved it by SQLite scan
-- order with no rule at all:
--
--   * base.gsis_by_espn        -> kept the placeholder for 264 of 266 espn ids
--   * projections._sleeper_to_gsis -> `setdefault`, the same accident, no log
--
-- while base.gsis_by_pfr (which feeds snap_counts) already applied the
-- deterministic `00-` preference. The result, measured on the live database
-- 2026-09-15 for season 2026:
--
--   | table                        | on the KEPT id        | on the DISCARDED id   |
--   |------------------------------|-----------------------|-----------------------|
--   | weekly_stats (wk1 REG)       | 0 rows /   0 ids      | 189 rows /  96 ids    |
--   | snap_counts                  | 0 rows /   0 ids      | 142 rows / 125 ids    |
--   | projections (sleeper)        | 106,848 rows / 112 ids|   6,030 rows /   9 ids|
--
-- i.e. the USAGE tables sat on the real id and the PRICING table sat on the
-- placeholder. Six 2026 rookies are on a league roster today; all six had zero
-- Week-1 stats under the id the crosswalk kept, and two of them are in the
-- top-30-per-position priced pool. Flipping either map ALONE moves the loss onto
-- the other side of the join — which is why the first half of item 3.18 measured
-- this and refused the one-line fix.
--
-- WHAT THIS MIGRATION REWRITES, AND WHY THAT IS NOT REWRITING A FACT.
-- `projections.gsis_id`, `league_player_state.gsis_id` and
-- `player_news_links.gsis_id` are DERIVED CROSSWALK COLUMNS, not upstream facts.
-- The fact each row carries is its own source key — `source_player_id` (Sleeper's
-- player_id) and `espn_player_id` / `espn_id` — and none of those is touched. The
-- gsis on the row is this system's own answer to "which nflverse player is that",
-- computed at ingest from `players`; item 3.1 already records
-- `league_player_state.gsis_id` as "explicitly derived and backfillable". This
-- migration recomputes that answer under the corrected rule. It adds no row,
-- removes no row, and changes no `retrieved_as_of` / `knowable_as_of`, so Rule 1
-- is untouched: nothing here alters WHEN anything was knowable, only WHICH
-- player it is about.
--
-- The alternative considered and rejected was a read-time ALIAS: teach the
-- pricing accessor to also accept the placeholder. That leaves the fact table
-- "honest" at the cost of a SECOND id space maintained by hand forever, threaded
-- through valuation, marginal, candidates, dispersion, waiver, espn_projections,
-- league/state and news — a permanent mechanism serving rows that stop being
-- produced the moment `_sleeper_to_gsis` prefers `00-`. One-off damage from a
-- one-off cause gets a one-off repair.
--
-- WHAT IS DELIBERATELY NOT REWRITTEN.
--   * `weekly_stats`, `snap_counts`, `ngs_*` — measured 0 placeholder ids; they
--     were always on the real id, which is exactly why the old rule lost them.
--   * `adp_rankings`, `fpecr_panel`, `fp_weekly_ecr` — keyed through
--     `base.ids_by_fantasypros`, which has ZERO collisions (0 of 4,781); between
--     them they hold 4 placeholder ids and none is an espn-collision loser.
--   * `depth_chart_slots` — 9 of its ids ARE collision losers, and it keeps them.
--     Its `gsis_id` is a column of the UPSTREAM nflverse depth-chart frame, not a
--     value this crosswalk derived; rewriting it would falsify what upstream
--     published. A consumer that needs to join it to the spine resolves through
--     `espn_id`, which the same table carries.
--
-- THE REWRITE RULE, stated so a test can check it exactly. A row is rewritten
-- only when BOTH of these hold:
--   1. the gsis currently stored on the row is one of ITS OWN source key's gsis
--      ids in the latest `players` snapshot — never some third id that happens
--      to look like a placeholder;
--   2. the preferred gsis for that key differs from the stored one.
-- The preferred gsis mirrors `base.preferred_gsis_by` exactly, in two passes:
--   pass 1 — `_gsis_preference` over the ids THIS column offers for the key:
--            `00-` first then lexicographic, written here as
--            `substr(MIN(CASE WHEN gsis_id LIKE '00-%' THEN '0' ELSE '1' END ||
--            gsis_id), 2)`, which is the same total order;
--   pass 2 — ONLY when pass 1 still returns a placeholder, one hop through the
--            player's ESPN identity: the smallest real `00-` gsis among the
--            latest rows sharing that placeholder's espn_id.
-- Pass 2 exists because a crosswalk reads only MAX(retrieved_as_of) per gsis, so
-- an id column upstream DROPS from a gsis's newest row vanishes from that gsis's
-- view. Measured live: Max Bredeson's newest `00-0041081` row carries
-- sleeper_id NULL while his placeholder `BRE060106` row still carries `13516`,
-- so the sleeper column sees one candidate and no collision at all — and his 954
-- projection rows would have stayed on the placeholder while `gsis_by_espn`
-- moved his roster row to the real id. Priced before the repair, unpriceable
-- after it. Pass 2 fires for 2 sleeper keys and 0 espn keys on the live data.
-- The placeholder-only GATE on pass 2 is load-bearing: four espn ids upstream
-- are shared by two genuinely DIFFERENT retired players with two real `00-` ids
-- (see `base.gsis_by_espn`), and an ungated hop would re-point one onto the
-- other.
-- Idempotent by construction: after it runs, condition 2 is false for every row,
-- so a second application is a no-op (pinned by test).
--
-- `gsis_id` is in NO primary key here (projections keys on
-- (source, source_player_id, season, week, retrieved_as_of);
-- league_player_state on (season, espn_player_id, retrieved_as_of);
-- player_news_links on (source, news_id, espn_id, retrieved_as_of)), so the
-- rewrite cannot collapse two rows into one. That is why this is an UPDATE and
-- not a table rebuild, and why it needs no `*_collapsed` meta alert.
--
-- The temp tables are dropped at the end: `store.apply_schema` hands the SAME
-- connection back to the caller, and a leftover TEMP table would shadow nothing
-- but would outlive the migration for the life of the process.
--
-- No BEGIN/COMMIT and no schema_version write here: store.apply_schema wraps this
-- script in its own transaction and stamps the version itself.

-- The latest snapshot per gsis — exactly what every crosswalk builder reads.
CREATE TEMP TABLE _m019_players AS
SELECT gsis_id, espn_id, sleeper_id
  FROM players p
 WHERE p.retrieved_as_of = (
           SELECT MAX(p2.retrieved_as_of) FROM players p2 WHERE p2.gsis_id = p.gsis_id
       );
CREATE INDEX _m019_players_sleeper ON _m019_players (sleeper_id, gsis_id);
CREATE INDEX _m019_players_espn    ON _m019_players (espn_id, gsis_id);
CREATE INDEX _m019_players_gsis    ON _m019_players (gsis_id, espn_id);

-- Pass 1, per key column: the `00-` preference over the ids that column offers.
CREATE TEMP TABLE _m019_sleeper_win0 AS
SELECT sleeper_id AS k,
       substr(MIN(CASE WHEN gsis_id LIKE '00-%' THEN '0' ELSE '1' END || gsis_id), 2)
           AS win0
  FROM _m019_players
 WHERE sleeper_id IS NOT NULL AND gsis_id IS NOT NULL
 GROUP BY sleeper_id;

CREATE TEMP TABLE _m019_espn_win0 AS
SELECT espn_id AS k,
       substr(MIN(CASE WHEN gsis_id LIKE '00-%' THEN '0' ELSE '1' END || gsis_id), 2)
           AS win0
  FROM _m019_players
 WHERE espn_id IS NOT NULL AND gsis_id IS NOT NULL
 GROUP BY espn_id;

-- Pass 2: the ESPN-identity hop, taken ONLY when pass 1 left a placeholder.
CREATE TEMP TABLE _m019_sleeper_fix AS
SELECT d.k AS sleeper_id,
       CASE WHEN d.win0 LIKE '00-%' THEN d.win0
            ELSE COALESCE(
                (SELECT MIN(q.gsis_id) FROM _m019_players q
                  WHERE q.gsis_id LIKE '00-%'
                    AND q.espn_id = (SELECT w.espn_id FROM _m019_players w
                                      WHERE w.gsis_id = d.win0)),
                d.win0)
       END AS keep_gsis
  FROM _m019_sleeper_win0 d;
CREATE UNIQUE INDEX _m019_sleeper_fix_key ON _m019_sleeper_fix (sleeper_id);

CREATE TEMP TABLE _m019_espn_fix AS
SELECT d.k AS espn_id,
       CASE WHEN d.win0 LIKE '00-%' THEN d.win0
            ELSE COALESCE(
                (SELECT MIN(q.gsis_id) FROM _m019_players q
                  WHERE q.gsis_id LIKE '00-%'
                    AND q.espn_id = (SELECT w.espn_id FROM _m019_players w
                                      WHERE w.gsis_id = d.win0)),
                d.win0)
       END AS keep_gsis
  FROM _m019_espn_win0 d;
CREATE UNIQUE INDEX _m019_espn_fix_key ON _m019_espn_fix (espn_id);

-- 1. projections — the PRICING key. `source` is pinned because
--    `source_player_id` only means "Sleeper player_id" for this source.
UPDATE projections
   SET gsis_id = (SELECT f.keep_gsis FROM _m019_sleeper_fix f
                   WHERE f.sleeper_id = projections.source_player_id)
 WHERE source = 'sleeper_rotowire'
   AND gsis_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM _m019_sleeper_fix f
                WHERE f.sleeper_id = projections.source_player_id
                  AND f.keep_gsis <> projections.gsis_id)
   AND EXISTS (SELECT 1 FROM _m019_players p
                WHERE p.sleeper_id = projections.source_player_id
                  AND p.gsis_id    = projections.gsis_id);

-- 2. league_player_state — the ROSTER key marginal/waiver join the board on.
UPDATE league_player_state
   SET gsis_id = (SELECT f.keep_gsis FROM _m019_espn_fix f
                   WHERE f.espn_id = league_player_state.espn_player_id)
 WHERE gsis_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM _m019_espn_fix f
                WHERE f.espn_id = league_player_state.espn_player_id
                  AND f.keep_gsis <> league_player_state.gsis_id)
   AND EXISTS (SELECT 1 FROM _m019_players p
                WHERE p.espn_id = league_player_state.espn_player_id
                  AND p.gsis_id = league_player_state.gsis_id);

-- 3. player_news_links — the item-3.6 alert join (news.py resolves it through
--    base.gsis_by_espn, so it moved with that map).
UPDATE player_news_links
   SET gsis_id = (SELECT f.keep_gsis FROM _m019_espn_fix f
                   WHERE f.espn_id = player_news_links.espn_id)
 WHERE gsis_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM _m019_espn_fix f
                WHERE f.espn_id = player_news_links.espn_id
                  AND f.keep_gsis <> player_news_links.gsis_id)
   AND EXISTS (SELECT 1 FROM _m019_players p
                WHERE p.espn_id = player_news_links.espn_id
                  AND p.gsis_id = player_news_links.gsis_id);

DROP TABLE _m019_sleeper_fix;
DROP TABLE _m019_espn_fix;
DROP TABLE _m019_sleeper_win0;
DROP TABLE _m019_espn_win0;
DROP TABLE _m019_players;
