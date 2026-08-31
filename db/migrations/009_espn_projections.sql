-- db/migrations/009_espn_projections.sql
-- Item 3.9: ESPN projected stat lines — the SECOND projection source.
--
-- WHY. Every valuation in this system (VOR board, draft engine, marginal/waiver
-- board, streaming) prices through ONE feed: Sleeper's `sleeper_rotowire`
-- weekly projections. A single-source system cannot distinguish "this player is
-- good" from "this feed likes this player". This table lands an INDEPENDENT
-- second opinion so `core/projection_ensemble.py` can compute a blended
-- estimate AND — the more valuable output — a per-player DISAGREEMENT signal.
--
-- ONE table in ONE file (the 007/008 lesson): two files sharing a number make
-- `store._migrations()` raise "versions must be contiguous", and `apply_schema`
-- runs UNCONDITIONALLY inside `open_db`, so a collision kills every command that
-- opens the database — including `ziggurat league status`, the alarm for the one
-- dataset whose lost day is literally unrecoverable. No BEGIN/COMMIT and no
-- schema_version write here: `store.apply_schema` wraps this script in a
-- transaction and stamps version 9.
--
-- SOURCE. The same raw `kona_player_info` response `espn_source.fetch_player_
-- universe` already pulls for the draft board (item 2.1, migration 004). Each
-- player carries a `stats` array whose entries are keyed by
--     statSourceId    0 = actual, 1 = PROJECTED   (we store only 1)
--     statSplitTypeId 0 = whole season, 1 = one scoring period
--     seasonId        the season described        (we store only the requested one)
-- so ESPN publishes a projected STAT LINE, not merely a point total.
--
-- THIS IS A FACT TABLE, and the fact is a LIVE MUTABLE signal: ESPN re-publishes
-- its projections continuously and serves no history of them. So, exactly like
-- `espn_draft_ranks` (004), a pull is stamped knowable_as_of = retrieved_as_of =
-- the pull day, and `retrieved_as_of` is in the PRIMARY KEY so successive pulls
-- coexist as versions that `base.select_as_of` resolves per key. A backtest
-- reads through `base.latest_truth(get_espn_projections)`.
--
-- PERISHABLE, like `projections` / `adp_rankings` / `espn_ranks`: a missed pull
-- is a lost observation, not staleness. Nothing here can be re-derived later.
--
-- ------------------------------------------------------------------------
-- WHY THE COLUMNS LOOK LIKE THIS
-- ------------------------------------------------------------------------
-- 1. The stat columns carry `core/scoring.py`'s CANONICAL KEY NAMES, matching
--    the `projections` table (003) name-for-name, so both feeds speak one
--    vocabulary and a stored row scores directly through `scoring.score`. Rule
--    2 holds: no scoring number lives here or in the ingester.
--
-- 2. `espn_applied_total` is ESPN's OWN point total for the row. It is a
--    CROSS-CHECK ONLY and is never a scoring input — the same role and the same
--    justification as `projections.projected_points`. It earns its place because
--    this endpoint applies THIS LEAGUE's scoring settings (verified: re-scoring
--    the stat line through scoring.py reproduces it to 1e-6 on 32/32 kickers and
--    32/32 D/ST, and on 459/460 offensive players), which makes the residual a
--    far sharper schema-drift alarm than any column-presence check: renumber a
--    stat id upstream and the re-derivation stops matching, at ingest.
--
-- 3. `projected_games` (ESPN stat id 210) is ESPN's projected games played. On
--    the live 2026 pool 514 of 524 projected players carry 17.0 and ten carry
--    less — ESPN pricing in a KNOWN absence, a column the flat-rate Sleeper feed
--    does not have. It is deliberately NOT folded into points; the ensemble
--    surfaces it as a separate availability opinion.
--
-- 4. THE D/ST BAND COUNTS, and the two scalar columns that are ABSENT.
--    ESPN does not project a defence's season points-allowed as one number: it
--    projects the EXPECTED NUMBER OF GAMES in each bracket band (ids 89/90/91/92
--    and 121-125 for points allowed, 128-136 for yards allowed). That is the
--    correct shape for a NON-LINEAR bracket — the expectation of a bracketed
--    value rather than the bracket of an expected value — and it is why the
--    D/ST re-derivation matches ESPN exactly.
--
--    So this table has NO `points_allowed` and NO `yards_allowed` column, and
--    their absence is load-bearing. ESPN's D/ST season row does carry a season
--    TOTAL points allowed (~322 for a full season). Stored under that name, a
--    caller's naive `scoring.score(pos, dict(row))` would price 322 through the
--    "46+ points allowed" bracket ONCE — catastrophically wrong, and silent.
--    With the scalars absent, the same naive call merely OMITS the brackets
--    instead of inventing a number. Measured across the 32 live 2026 defences
--    that omission is worth -45.8 to +27.9 points (median -12.9) on totals of
--    45-131 — negative for a bad defence, positive for a good one, so it does
--    not scale the D/ST board, it REORDERS it.
--
--    PRICE A ROW FROM THIS TABLE WITH `espn_projections.house_points(row)`,
--    never with a bare `scoring.score`. The band columns are named `pa_games_*`
--    / `ya_games_*` precisely so they cannot be mistaken for scoring keys, and
--    `house_points` converts a band to points by ASKING `scoring.score_dst` what
--    both ENDS of the band score and refusing if they disagree — so a re-banding
--    on either side fails loud rather than mispricing silently.

CREATE TABLE IF NOT EXISTS espn_projections (
    -- Provenance. Constant 'espn' today; the column exists so a third opinion
    -- (or a per-source ESPN variant) lands additively, without a migration.
    source             TEXT NOT NULL,

    -- espn_key is the non-null temporal/identity key: str(player id) for skill,
    -- team abbr for D/ST. It exists for the reason migration 004 spells out at
    -- length: ESPN gives team defences SYNTHETIC NEGATIVE ids, so espn_id is
    -- stored NULL for them (the 004 contract: D/ST joins by team), and
    -- select_as_of's per-key MAX(retrieved) subquery equijoins on the key
    -- columns — a NULL key never self-matches and would silently drop every
    -- D/ST row from an as-of read. Numeric id strings and 2-3 letter abbrs
    -- cannot collide.
    espn_key           TEXT NOT NULL,
    espn_id            TEXT,               -- str(player id); NULL for D/ST
    gsis_id            TEXT,               -- via base.gsis_by_espn; NULL kept, never dropped
    player             TEXT,               -- display name (fullName)
    position           TEXT NOT NULL,      -- QB/RB/WR/TE/K/D/ST via espn_ranks.DEFPOS
    team               TEXT,               -- PRO_TEAM_MAP then TEAM_ALIASES; the D/ST join key
    season             INTEGER NOT NULL,

    -- 0 = the WHOLE-SEASON projection (ESPN's own scoringPeriodId for the season
    -- split, not an invented sentinel); >= 1 = that scoring period's projection.
    -- The endpoint serves the season total plus only the CURRENT scoring period,
    -- so this is not a full 17-week panel — and unlike the Sleeper feed (a flat
    -- season rate, item 3.2) the weekly entry IS opponent-aware.
    week               INTEGER NOT NULL,

    projected_games    REAL,               -- ESPN stat 210; NOT a scoring input (see note 3)
    espn_applied_total REAL,               -- ESPN's own total; CROSS-CHECK ONLY (see note 2)

    -- ---- canonical core/scoring.py keys (offense) ----------------------
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
    -- The five non-scrimmage TD ids (blocked-kick 93, kickoff 101, punt 102,
    -- interception 103, fumble 104 return TDs), summed. Each is 6.0 in the
    -- league's ESPN scoring fixture, the value scoring.py's special_teams_tds
    -- rides. 98 of 460 live offensive projections carry one.
    special_teams_tds         REAL,

    -- ---- canonical core/scoring.py keys (kicker) -----------------------
    -- Bucketed made-FG counts, 1:1 with scoring.py's own bucket keys. ESPN id
    -- 74 ("50+") is deliberately unused: it is 50-59 and 60+ ADDED TOGETHER,
    -- and the house pays 5 for the first and 6 for the second.
    fg_made_0_39              REAL,
    fg_made_40_49             REAL,
    fg_made_50_59             REAL,
    fg_made_60                REAL,
    pat_made                  REAL,
    fg_missed                 REAL,

    -- ---- canonical core/scoring.py keys (D/ST events) ------------------
    sacks                     REAL,
    def_interceptions         REAL,
    fumble_recoveries         REAL,
    safeties                  REAL,
    blocked_kicks             REAL,
    def_tds                   REAL,        -- the same five return-TD ids, summed
    one_point_safeties        REAL,
    two_point_returns         REAL,

    -- ---- D/ST bracket BAND COUNTS — NOT scoring keys (see note 4) ------
    -- Expected number of GAMES landing in each band. Band bounds are ESPN's
    -- own band definitions; the band -> points conversion is done at read time
    -- by asking core/scoring.py, never by a number stored or written here.
    pa_games_0                REAL,        -- id 89   0 points allowed
    pa_games_1_6              REAL,        -- id 90
    pa_games_7_13             REAL,        -- id 91
    pa_games_14_17            REAL,        -- id 92
    pa_games_18_21            REAL,        -- id 121  (house pays 0 here)
    pa_games_22_27            REAL,        -- id 122  (house pays 0 here)
    pa_games_28_34            REAL,        -- id 123
    pa_games_35_45            REAL,        -- id 124
    pa_games_46_plus          REAL,        -- id 125
    ya_games_0_99             REAL,        -- id 128  < 100 total yards allowed
    ya_games_100_199          REAL,        -- id 129
    ya_games_200_299          REAL,        -- id 130
    ya_games_300_349          REAL,        -- id 131  (house pays 0 here)
    ya_games_350_399          REAL,        -- id 132
    ya_games_400_449          REAL,        -- id 133
    ya_games_450_499          REAL,        -- id 134
    ya_games_500_549          REAL,        -- id 135
    ya_games_550_plus         REAL,        -- id 136

    retrieved_as_of    TEXT NOT NULL,
    knowable_as_of     TEXT NOT NULL,      -- = retrieved_as_of (live mutable source)

    PRIMARY KEY (season, week, espn_key, retrieved_as_of)
);

-- Covers the accessor's shape: filter by season/week (+ position), then resolve
-- the per-key MAX(retrieved_as_of) under the knowable/retrieved gates.
CREATE INDEX IF NOT EXISTS idx_espn_projections_lookup
    ON espn_projections (season, week, position, knowable_as_of, retrieved_as_of);
-- The ensemble joins the house spine to this table by espn_key.
CREATE INDEX IF NOT EXISTS idx_espn_projections_key
    ON espn_projections (espn_key, season, week, retrieved_as_of);
