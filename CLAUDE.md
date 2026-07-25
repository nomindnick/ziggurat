# CLAUDE.md — the Ziggurat constitution

Ziggurat is an AI-assisted decision system for a season-long fantasy football
league (10-team ESPN office league, full PPR with custom D/ST and kicker
scoring). The repo **is** the system: Claude Code is the harness and reasoning
layer, the `ziggurat/` package provides deterministic tools, SQLite holds
facts, markdown under `intel/` holds judgment.

- **[SPEC.md](./SPEC.md)** — what & why (features, architecture, league ground truth)
- **[IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)** — in what order, with per-item Update blocks (the cross-session memory of what happened)
- **This file** — the standing rules and workflows that are followed, not improvised

## Current status

**Phase 1 (Ground Truth & Data Spine) — COMPLETE (Checkpoint 1 held
2026-07-20).** Phase 0 complete 2026-07-16. Both scheduled-first spikes closed
2026-07-16:
- **1.1 ESPN access** — `espn_api` authenticates (SWID/ESPN_S2 in local `.env`)
  and the full custom scoring (PPR, distance kicker incl. −1/miss, D/ST
  points-and-yards brackets) is machine-readable, so **no hand-transcription of
  scoring is needed** (`intel/research/espn-access.md`; numbers feed item 1.3).
- **1.2 historical market archives** (the #1 backtest-feasibility risk) —
  **retired**: two independent, free, weekly point-in-time market proxies span
  2021-2025 (DynastyProcess `db_fpecr` weekly FantasyPros ECR + Sleeper
  `/research` ownership), so Phase 4 is scoped a **full backtest program**
  (`intel/research/market-archives.md`).

**1.3 house-rules scoring engine — done 2026-07-16.** `core/scoring.py` is
complete: full PPR (+2-pt, nflverse fumble components), distance kicker with
−1/miss (missed XP scores 0), and D/ST with BOTH points- and yards-allowed
brackets incl. the explicit implicit-zero bands — locked to the ESPN fixture.

**1.4 NFL data ingestion — done 2026-07-16; foundation stabilized 2026-07-20.**
nflverse sources (players, schedules, weekly stats, snaps, NGS, depth charts,
injuries) land in SQLite under `ziggurat/data/nfl/`. `base.select_as_of` now has
two explicit views: safe-default `historical` gates both knowledge and retrieval
time; `latest_truth` intentionally allows later corrections for final grading or
accepted immutable bulk history. The deprecated `nfl_data_py` dependency was
replaced by the maintained `nflreadpy` client behind a tested adapter, source
schema drift fails loudly, and ordered SQLite migrations/indexes are live.

**1.5 projections/ADP/odds/weather + D/ST team-defense ingestion — done
2026-07-20.** Five sources land behind migration `003_market_context.sql`
(`schema_version` 3), each with the item-1.4 as-of pattern + leakage/fixture
tests: **team_defense** (`load_team_stats` + schedules scores → a D/ST line that
prices directly through `score_dst`; ESPN charge semantics deferred to 3.8),
**game_odds** (closing lines, `knowable=gameday`), **game_weather** (Open-Meteo
forecast + ERA5 archive, two-regime `forecast_source`; context only), **projections**
(Sleeper `sleeper_rotowire`, current-season-forward + `latest_truth`-only bulk
backfill — free historical point-in-time stat-line projections proved infeasible,
operator-confirmed), **adp_rankings** (FantasyPros ECR). The **ESPN-vs-market
divergence report** (`core/divergence.py`, `ziggurat divergence` CLI) runs and
prints a readable table (done-when met). Built via three verified workflows
(recon → build → adversarial audit); the audit found no leakage bugs and 4
correctness findings, all fixed. Suite green (212 passed). Design +
deferrals in `IMPLEMENTATION_PLAN.md` 1.5 and `intel/research/ingestion-1.5-design.md`.

**Checkpoint 1 (data spine review) — held 2026-07-20.** Re-plan recorded in
IMPLEMENTATION_PLAN.md (Checkpoint 1 notes + inline amendments to 2.1/2.2/2.3 and
4.1/4.2). Headline decisions: no plan-structural surprises (both spikes de-risked
their unknowns); Phase 4 backtest scoped on the `db_fpecr` weekly-ECR panel +
Sleeper ownership deltas (the panel ingester is 4.1's first deliverable, read under
`latest_truth`); Phase 2 uses the exact decoded roster structure and can calibrate
the mock-sim opponent model on prior-season `leagueHistory` drafts; **nothing in
Phase 4 blocks draft day — Phase 2 is the sole draft-critical path and begins next.**

**Phase 2 — Valuation Core & Draft Weapon — IN PROGRESS.**
- **2.1 global valuation (VOR) — done 2026-07-20.** `core/valuation.py` re-scores
  the 1.5 weekly projections through `scoring.py` **per-week-then-sum** (non-linear
  D/ST brackets make sum-then-score wrong), computes replacement levels from the
  exact roster (empirical flex allocation, superflex-guarded, K/DST denoised), and
  ranks a global VOR board. The "what the room can't see" value view diffs that
  scarcity-priced board against a **live ESPN default board** (`espn_source.py` raw
  `kona_player_info` + `espn_ranks.py` + migration `004`, `schema_version` 4;
  as-of-gated, leakage-tested) — the house edge surfaces at the distance kicker and
  dual D/ST brackets (offense house scoring ≈ Sleeper PPR). Thin `ziggurat valuation
  [--espn]` CLI. Built via three verified workflows (recon → build → adversarial
  audit; leakage/scoring/VOR clean, 4 value-view findings fixed). Suite green (243).
  Design + deferrals in `IMPLEMENTATION_PLAN.md` 2.1 and `intel/research/valuation-2.1-design.md`.

- **2.2 mock draft simulator — done 2026-07-21.** Deletable `ziggurat/draft/`
  package: snake sim + calibrated opponent model + `ziggurat mock-draft` CLI.
  `leagueHistory` recon found exactly ONE prior draft (league founded 2025; 2 of
  10 seats fully autodrafted), so ESPN-rank+noise is the PRIMARY bot model,
  seeded with aggregate 2025 priors (reach σ=17.78, autodraft 20%, K/DST
  round-window R9+, position-run curve) fit against a real board-at-draft-time
  signal (2025 ESPN editorial board + draft-day db_fpecr ECR). The `Picker` seam
  is where 2.3 plugs in. Done-when met on real data (`db/ziggurat.sqlite` now
  actually populated, schema 4): 20,000 drafts (10 slots × 2 strategies × 1,000)
  in 87s; follow-VOR beats follow-ESPN in all 10 slots (~+128 — a
  house-projected-points gap, not a validated realized edge; Phase 4 grades
  that). Three verified workflows (recon → build → 5-skeptic audit); 7 minor
  findings, 5 fixed, 2 recorded. Suite green (292). Details:
  `IMPLEMENTATION_PLAN.md` 2.2 + gitignored `intel/research/mocksim-2.2-*.md`.

- **2.3 draft pick engine — done 2026-07-22.** `ziggurat/draft/engine.py` +
  `survival.py`: Fry–Ohlmann additive score (VOR + need + survival-timed VONA
  urgency + round-signed risk prior), survival via Monte-Carlo rollouts of the
  calibrated 2.2 room (cloned board state; analytic sigmoid fallback), one new
  trailing `PickContext.opponent_rosters` field, `recommend()` → novice-legible
  `PickRec` reasons (the 2.4 TUI contract), live-recalibration utility (priors
  seam on `PickEngine`; TUI wiring is 2.4's). Done-when met: 60/60 tournament
  cells positive (3 seeds × 10 slots × both baselines, all CI>0; +135…+197 vs
  follow-ESPN, +22…+53 vs follow-VOR — house-projected, self-graded; Phase 4
  grades realized). K/DST divergence play emerges from urgency (R9–10 vs the
  room's R15), no special case. 18-agent audit: margin structural (survives
  held-out seeds, non-winning weights, hostile rooms); 13 minor findings all
  fixed/recorded. Suite green (334). Details: `IMPLEMENTATION_PLAN.md` 2.3 +
  gitignored `intel/research/pick-engine-2.3-design.md` & `draft-strategy.md`.

- **2.4 draft board TUI — done 2026-07-22.** Draft-day cockpit in deletable
  `ziggurat/draft/`: `resolver.py` (stdlib tiered fuzzy entry, confirm-on-tie,
  elite-safety — silent wrong-pick autos measured 0), `session.py` (headless
  controller; fsync-before-ack JSONL journal, resume-by-replay bit-identical,
  fresh state-seeded ctx per compute, live-recalibration + honesty display,
  snake-turn contingencies), `posture.py` (hysteresis monitor — both 2.3
  deferrals landed), `board_view.py`/`app.py` (Rich scroll-on-enter REPL;
  verbatim `PickRec.reasons`), thin `ziggurat draft-board` CLI. Measured:
  recommend() ≤243 ms @ R=512 on the real board → synchronous recompute (no
  threads). Three verified workflows + fix round; audit (10 agents) found 1
  critical (journal clobber on relaunch-without---resume — fixed with O_EXCL +
  timestamped names + header-driven resume) and 5 majors, all fixed; recorded
  notes in `IMPLEMENTATION_PLAN.md` 2.4 + gitignored
  `intel/research/tui-2.4-recon.md`. Suite green (460). Rich REPL vs Textual
  and rapidfuzz deliberately deferred to Checkpoint-2 rehearsal evidence.

**Checkpoint 2 — rehearsal gate MET 2026-07-24; remainder is calendar-bound.**
The day's arc: rehearsals surfaced and fixed the engine's
lineup-reachability discount (bench QB/TE stacking — the 2.2 tournament
metric was blind to bench value) and the burst pick-entry problem (Rich
REPL → `ziggurat draft-web` live-search web cockpit → quick-pick strip).
ESPN REST live-sync proved impossible (views freeze during live drafts,
flush atomically at completion — the flush does enable post-draft
auto-import for Phase 3), but **DOM-sync shipped instead**: a Tampermonkey
userscript mirrors the draft room's Pick History into the cockpit through a
refuse-rather-than-guess resolution gate (audited by a 35-agent workflow +
skeptic re-audits; board now unions the full ESPN universe so every
draftable player is enterable). Two full-length practice-draft rehearsals
completed — the second flawless, hands-free. Remaining before draft day
(all calendar-bound): strategy-from-slot once ESPN schedules the draft;
near-day board refresh + room re-snapshot + a confidence run on the
draft-day machine. Details in IMPLEMENTATION_PLAN.md Checkpoint 2 notes.

**Phase 3 — In-Season Operations — IN PROGRESS** (hard deadline ~NFL Week 1,
~Sept 10; weeks 1–3 are the richest waiver season).

- **3.1 league state sync & cadence — built & tested 2026-07-24.** Permanent
  `ziggurat/league/` package (`source.py` network seam, `state.py` mappers +
  as-of accessors, `sync.py` orchestration + run log), migration `005`
  (`schema_version` 5), `ziggurat league {sync,status,roster,free-agents,holdings}`,
  systemd user timer + installer. **The recon finding that shaped it: ESPN serves
  league state as a CURRENT SNAPSHOT ONLY — no historical backfill exists**
  (leagueHistory ignores `scoringPeriodId` on rosters; past-season box scores
  carry no per-week roster; past-season transactions are empty/404). So league
  history accumulates ONLY forward and a missed run is unrecoverable — hence
  `league_sync_runs` + a gap report, and hence `league_player_state` snapshots
  the WHOLE universe daily (a drop must be a positive `on_team_id IS NULL` fact,
  which doubles as the FA pool). Validated live: 10 teams, 70 matchups, 1026
  players all correctly free agents pre-draft, 98.9% espn→gsis coverage, leakage
  clean. Two-round adversarial audit (27 agents): 24 findings, 12 confirmed, all
  fixed — headline catch was that a degraded pull *destroyed* the day it should
  have refreshed (empty ESPN response → partition deleted → dropped players
  reverted to stale holders, run still logged `ok`), now blocked by
  `SnapshotCollapse` floors checked before any delete; plus an unbounded hang
  that would have silently killed the cadence under `Type=oneshot`. Suite green
  (624). Timer installed and firing on this box. Remaining is calendar-bound:
  real-data confirmation of roster history needs the August draft, and the
  Strix Halo still needs the same installer run on it. Details:
  `IMPLEMENTATION_PLAN.md` 3.1 + gitignored `intel/research/league-sync-3.1-design.md`.

- **3.1b NFL data refresh cadence — built & tested 2026-07-24.** The 1.4/1.5
  `pull_*` ingesters existed and were tested but **nothing called them**, so 14
  tables sat empty; the failure mode is Rule-1-invisible (a November read priced
  off a July snapshot carries a perfectly valid `knowable_as_of` — not leaked,
  merely stale). Landed: `ziggurat/data/nfl/refresh.py` (a 15-entry `SourceSpec`
  registry + `run_ingest` + run log + staleness report — the `league/sync.py`
  analogue), migration `006` (`schema_version` 6, `nfl_ingest_runs`, one row per
  source per run), `ziggurat ingest {run,status,sources}` with `--dry-run`,
  `ziggurat/net.py` (shared `bounded_socket`), and three systemd unit pairs +
  `scripts/install-nfl-ingest.sh`. **The build's headline finding: 3 of the 14
  ingesters were ALREADY BROKEN against live upstream while the suite was green
  — the committed fixtures are frozen 2023 frames, so `require_columns` never
  fired.** `weekly_stats` (null `player_id`) and `injuries` (nflverse dropped
  `date_modified` in 2025+) are fixed; **`depth_charts` is BLOCKED and recorded**
  (upstream became a dated daily panel — a table + accessor rewrite, and 3.2 had
  already deferred its consumer). The item-3.1 lesson carried forward: the ONE
  delete-then-write path (`espn_ranks`) reproduced the destroy-the-day bug live
  (a 20-player response replaced a stored 1,026-player same-day board; an empty
  one wiped it), now fenced by `BoardCollapse` floors before the delete; all
  three unbounded network seams bounded; a failed source's partial rows rolled
  back so they cannot ride the next source's commit; and "wrote 0 rows" is never
  `ok`. Deliberately NOT copied from 3.1: the missing-days gap report — nflverse
  is re-pullable and crying wolf there would train the operator to ignore the
  league-side report where "unrecoverable" is literal.
  **A four-auditor round then found 28 real defects, all fixed (suite 732).** Two
  corrected the build's own claims: (a) "append-only tables need no floor" is
  false for EMPTIED VALUES — `select_as_of` resolves per key, so a `players` pull
  with null id columns SHADOWS the good crosswalk (measured live: every crosswalk
  → 0, run logged `ok`); now `players.CrosswalkCollapse`. (b) `bounded_socket()`
  never bounded ESPN — `requests` discards the process socket default, so item
  3.1's hang fix was ineffective too; now `net.bounded_espn()` (measured: hangs
  forever → raises in 3.0 s). Also fixed: `run_ingest`'s back-stamp fence was
  disabled by its own default and `valuation --espn --as-of <past>` destroyed a
  stored board; the staleness report ignored `season` and reported future runs as
  fresh; a 99.7%-dropped pull read `fresh`/`no failures`; `weather_weeks` dropped
  the week being played; `interval_days` was decorative (the weekly group now
  fires daily and the run log decides, so a failed Thursday retries Friday).
  Remaining is an operator step (populate, install) + calendar (six sources have
  no 2026 data upstream until ~Sept 10; nothing here has met a real game week).
  Details: `IMPLEMENTATION_PLAN.md` 3.1b.

- **3.2 marginal valuation — built & tested 2026-07-24.** Roster-context value:
  `core/lineup.py` (the permanent per-week starting-lineup seater, written FRESH
  — `ziggurat/draft/` is untouched and deletes its own copies; brute force ships
  as a test oracle) and `core/marginal.py` (`V(K) = Σ_w E_S[lineup(K,w,S)]`;
  `marginal(p|R) = V(R) − best legal free-agent replacement`), plus a thin
  `ziggurat marginal` CLI. **The recon finding that shaped it: the weekly
  projections are a flat season rate, not week-specific forecasts** (median CV
  ~1% for every skill position) — **D/ST alone varies (12%)**, so an uncapped
  best-available baseline makes a SECOND DEFENSE the top add on nearly any roster
  (measured 15 of 16). Hence `POSITION_CAPS` (DST/K hard 1) and hence K/DST
  priced on a **current-week horizon** (3.5 owns streaming; without this the two
  modules contradict each other with no error anywhere). Availability is a
  NORMALIZED Bernoulli distribution (the naive form gives `w0 = −0.290` on a
  17-man roster); handcuff coupling is gated to QB/RB/TE (measured WR uplift
  −0.14 — no WR handcuff effect exists) which is also what keeps the non-linear
  D/ST brackets Rule-2 safe. `weeks=None` RAISES rather than guessing a full
  season. Every prior ships as a **labeled hypothesis** with its source in the
  reason text, and a staleness banner reads 3.1b's `source_freshness()` — a July
  projection pricing a November decision is Rule-1-invisible.
  **A four-auditor round then found 1 critical, 6 major and 13 lesser defects,
  all fixed (suite 832).** The critical one is the shape of the whole item's
  danger: **the feed's bye row and its "no forecast" row are byte-identical**
  (team set, opponent NULL, every stat NULL), so a point-sum test could not tell
  "worth nothing" from "we do not know" — A.J. Brown, 99.3% owned, carries ONE
  real week and sixteen empty ones, cleared the gate, and topped the drop board
  with a confident "drop him and GAIN 24.4" and no disclosure at all. Coverage
  (`WeeklyLine.played_weeks`), not the sum, now decides priceability. The other
  majors: one-out truncation was bounded on the LEVEL `V(K)` (+1.9%) while the
  shipped quantity is a DIFFERENCE that was **2-3x off on bench rows** — the
  SEARCH stays at one-out for cost, everything REPORTED is re-priced at depth 3
  (Monte Carlo was rejected because sampling noise dissolves the exact-tie band
  the tiebreak ladder exists for); the decomposition was a probability-mass split
  wearing a mechanism's label (~55% of every row read as "injury insurance",
  including a D/ST that can never be unavailable); a season-ending
  `INJURY_RESERVE` was priced as ONE missed week with no reason naming the
  designation; the static-roster caveat printed the bias BACKWARDS; every
  availability reason quoted the handcuff study's pair count as its own `n`; the
  staleness banner warned off the NEWEST pull so one refreshed row silenced it;
  and in-season week resolution returned the week that had already finished on
  **Tuesday and Wednesday — the two waiver days the cadence is built around**.
  Details: `IMPLEMENTATION_PLAN.md` 3.2 + gitignored
  `intel/research/marginal-valuation-3.2-design.md`.

- **3.2c historical backfill & `depth_charts` v2 — built, audited & fixed
  2026-07-25.** Inserted as a prerequisite for 3.3 from a measurement, not a
  plan reading: **the database held only season 2026 and every stat table was
  empty**, because 3.1b built a *current-season refresher* that correctly
  phase-skips history and will never backfill it — so 3.3's done-when ("run
  against last season's data") had no data. `ziggurat ingest backfill` now lands
  2021–2025 (55 source-season pairs, 40.6 s, DB → 124.3 MB), and `depth_charts`
  is unblocked as a **change log + tombstones** (29,483 slots + 348 panels,
  6.98 MiB vs 255.4 MB verbatim; migration `007`, `schema_version` 7). All 10
  shipped-code defects recon surfaced are fixed. Suite 832 → **1219**.
  **The audit's headline is the tombstone rule: an absence is only a fact when
  you know it is one.** `_change_log` could not tell "these players were removed
  from the chart" from "upstream's scraper failed for this club today" — and
  upstream does the latter in **12 of 348 published panels** (ARI 2026-07-24:
  100 slots → 42 → back to 100). One collapse wrote 91 tombstones, logged `ok`,
  and made `qb1_change_candidates` announce a QB1 change that never happened.
  Raising was the wrong fix (the whole file is re-diffed every pull, so a bad
  past `dt` bricks the source forever — unlike `espn_ranks`, where the bad
  response is transient) and a club-count floor catches 0 of 12. Also fixed: a
  daily silent row loss on `adp_rankings` that put a hole in the WR board, and
  an unfenced `ingest run --season <past>` that wrote ~58k projection rows
  stamped `knowable_as_of = today` and logged `ok`. Details:
  `IMPLEMENTATION_PLAN.md` 3.2c + gitignored
  `intel/research/backfill-depthcharts-3.2c-design.md`.

**Two standing rules this item paid for, both about process rather than code:**
**(1) The systemd timers run `ziggurat` from the working tree, so uncommitted
code is the production cadence.** `db/ziggurat.sqlite` reached `schema_version 7`
because a timer applied a migration nobody had reviewed. **An applied migration
is never re-applied, so a correction NEVER edits an existing migration file** —
it ships as a new one, or the live database permanently describes a schema no
file holds while the whole suite agrees with the file. Enforced by
`test_an_applied_migration_is_never_edited`. **(2) Rule 5's three enforcement
points are only as wide as their patterns**: an end-anchored `*.sqlite` matched
neither `ziggurat.sqlite.bak-v7` nor `.sqlite.gz`, in `.gitignore` *and* in
`repo_guard.py`, so a database backup full of league-private data cleared two of
the three. Both widened; a boundary pattern is now assumed narrow until tested.

Calendar anchors: draft expected mid-to-late August 2026, still unscheduled
(monitor ESPN — 2 of 10 seats still invite-pending as of 2026-07-21).

Update this section whenever a phase or checkpoint closes.

## Standing rules (non-negotiable, from the SPEC)

1. **`as_of` on every data read.** Every read accessor takes a keyword-only
   `as_of` (no default, no implicit "now") and defaults to the `historical` view,
   which gates both `knowable_as_of` and `retrieved_as_of`. `latest_truth` is an
   explicit opt-in for corrected outcomes or deliberately accepted immutable
   bulk history; never use it for mutable decision inputs. Because a bulk-loaded
   backtest DB (all `retrieved_as_of` = today) reads *empty* under the default
   `historical` view, backtest/grading code reads through `base.latest_truth(accessor)`,
   which binds that view so it can't be silently forgotten. Every accessor ships
   with a leakage test. Convention: `ziggurat/data/asof.py` and
   `ziggurat/data/nfl/base.py`.
2. **House scoring rules live ONLY in `ziggurat/core/scoring.py`.** No other
   module hard-codes a scoring value. As of item 1.3 the numbers are the real
   league settings (transcribed from spike 1.1, locked to the ESPN fixture) and
   live only in the frozen `ScoringRules`; offense, D/ST (both bracket systems),
   and kicker are all implemented. Post-Week-1 box-score validation is the one
   open confirmation (anchored TODO in the module + item 3.8).
3. **No logic in the CLI layer** (`ziggurat/cli/`). Commands parse, call, print.
4. **Every LLM call goes through the router** (`ziggurat/llm/`, config in
   `config/llm.toml`). No component imports a model SDK or shells out to a
   model directly. Tasks carry a stakes tier (routine / standard / high_stakes).
5. **Public-repo boundary.** This repo is public. `intel/`, top-level `data/`,
   `*.sqlite*`, and `.env*` are never committed — enforced by `.gitignore`,
   the pre-commit hook, and `tests/test_repo_boundary.py`. The league name and
   the operator's own team name (as stated in SPEC.md) are acceptable in
   committed files — operator's decision, 2026-07-16. The hard red line for
   committed files, fixtures, and commit messages is **real names of the other
   league members (colleagues)**: never commit those; keep colleague names,
   other managers' rosters, and league-private data in local `intel/` only.
   Opponents' team names can encode real identities in an office league — use
   judgment; the hook can't catch prose, so you must.
6. **Explainability.** The operator is a football novice and cannot smell
   absurd outputs: every recommendation ships with its reasons and data.
   Sanity checks (e.g., never recommend starting a player ruled OUT or on bye)
   are enforced in code with tests, not left to judgment.
7. **Plan-update discipline.** Closing a plan item fills in its **Update**
   block in IMPLEMENTATION_PLAN.md. Spikes close by writing a findings note in
   `intel/research/`. Any amendment to the plan gets written down — the plan
   on disk is always the real plan.
8. **Draft package is deletable.** Nothing outside `ziggurat/draft/` may
   import from it; it gets deleted after draft day.

## Repo map

```
ziggurat/            the Python package (deterministic tools)
  data/              ingestion clients + as-of data access (asof.py, store.py)
  core/              scoring.py (single source of truth), valuation, signals
  league/            league state sync (source/state/sync), opponent layer, Monte Carlo
  draft/             DELETABLE draft tool (Phase 2, deleted after draft day)
  llm/               the LLM routing interface (router.py, backends.py)
  cli/               thin Typer commands — no logic
  net.py             shared network bounding (bounded_socket / HTTP_TIMEOUT)
  repo_guard.py      public-repo boundary patterns (shared by hook + tests)
  scaffold.py        recreates gitignored intel/ skeleton from templates/
backtest/            Phase 4 experiments; imports ziggurat/ directly
db/schema.sql        initial public schema; ordered upgrades in db/migrations/
config/llm.toml      task-tag -> backend/tier routing table
templates/intel/     committed starter skeleton for the private intel/ tree
intel/               (gitignored) opponents/, weekly/, research/, heuristics.md
data/                (gitignored) raw pulls, audio, transcripts, caches
scripts/hooks/       versioned git hooks (pre-commit boundary guard)
tests/               pytest suite; patterns established here get copied
```

## Dev workflow

```bash
# fresh clone, once:
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
# nflreadpy is a declared dependency; no manual second install step.
git config core.hooksPath scripts/hooks   # per clone, required
.venv/bin/ziggurat intel init             # recreate private intel/ skeleton
.venv/bin/ziggurat db init                # create db/ziggurat.sqlite

# routinely:
.venv/bin/pytest                          # must stay green
.venv/bin/ziggurat smoke                  # spine wiring sanity check

# in-season, on the machine that runs the cadence, once each:
scripts/install-league-sync.sh            # item 3.1 — systemd user timer, 4x/day
scripts/install-nfl-ingest.sh             # item 3.1b — daily / weekly / gameday timers
# re-run install-league-sync.sh on any box where it is ALREADY installed: the 3.1b
# audit corrected that unit's no-op After=network-online.target and its restart limiter.
loginctl enable-linger "$USER"            # or every timer dies at logout
.venv/bin/ziggurat league status          # last run + UNRECOVERABLE missing days
.venv/bin/ziggurat ingest status          # per-source last successful pull + staleness

# before any first/manual NFL pull, see the plan without touching the network:
.venv/bin/ziggurat ingest run --dry-run
.venv/bin/ziggurat ingest sources         # the registry: cadence, phases, flags

# item 3.2c — history is a SEPARATE command from the cadence, by design:
.venv/bin/ziggurat ingest backfill --first 2021 --last 2025   # ~40 s, DB -> ~124 MB
.venv/bin/ziggurat ingest reap --dry-run  # clear an orphan run left by a killed backfill
```

**League history is perishable (item 3.1).** ESPN serves league state as a
current snapshot only — there is no historical backfill for rosters, lineups, or
transactions. Whatever the scheduled sync does not capture is gone permanently,
so `ziggurat league status` reporting missing days is a real (unfixable) data
loss, not a cosmetic warning. Check it whenever cookies are refreshed or the
sync machine changes.

**NFL sources are MOSTLY replayable — know which ones are not (item 3.1b).**
Every nflverse source (`schedules`, `weekly_stats`, `snap_counts`, `ngs_*`,
`injuries`, `team_defense`, `game_odds`) is a whole-season file re-downloaded in
full, so a missed `ziggurat ingest` run is **staleness, not loss** — re-pullable
any time. Exactly four sources serve the CURRENT value only and lose an
observation permanently when missed: **`projections`** (Sleeper), **`adp_rankings`**
(FantasyPros scrape), **`espn_ranks`** (the draft board), and **`game_weather` in
forecast mode**. `ziggurat ingest status` says which is which, and deliberately
never uses the league sync's "unrecoverable / missing days" language — an
undifferentiated alarm is how the one report where those words are literal gets
ignored. Freshness is read from `nfl_ingest_runs`, never from
`MAX(retrieved_as_of)` on a fact table (that lies three measured ways).

**The timers fire more often than the sources need.** Each source carries an
`interval_days`, and `ziggurat ingest run` SKIPS one whose last successful pull is
still inside it (status `fresh`) — so the weekly unit can fire daily while
nflverse is hit once a week, and a failed Thursday retries on Friday instead of
costing a whole in-season week. `--force` pulls anyway. A past `--as-of` is
refused by default on every path (back-stamping writes today's data under a past
`retrieved_as_of`, which the default `historical` view then serves as if it had
been knowable then); `--allow-backfill` is the deliberate override.

Tests land **with** each item, not in a cleanup phase. Test conventions:
golden-master cases for scoring, leakage tests for accessors, the mock-draft
sim as the draft engine's harness, unit tests for pure logic, thin
cached-fixture integration tests for ingestion.

**NFL source client (item 1.4).** `nfl_data_py` is deprecated upstream and its
metadata conflicts with modern pandas/NumPy. Ziggurat uses the maintained
`nflreadpy` package through `ziggurat/data/nfl/source.py`, which converts Polars
frames to pandas at one seam. Cached-fixture and adapter-contract tests remain
offline; ingesters validate required columns so upstream schema changes fail
loudly rather than storing partial rows.

Phase 1 prep: ESPN private-league auth needs `SWID` and `ESPN_S2` cookie
values in a local `.env` (gitignored; never committed, never echoed into
committed files or logs).

## Weekly operating cadence

> **PLACEHOLDER — lands with item 3.7.** Will encode: Tuesday roster-legality
> check + waiver claims, Wednesday post-waiver scan, Thu–Sat monitoring,
> Sunday inactives + final lineup, Monday retro & journal.

## Heuristics promotion criteria

> **PLACEHOLDER — lands with item 5.2.** Ladder: observation → hypothesis
> (tracked, not applied) → rule (applied, evidence cited), in
> `intel/heuristics.md`. Backtest findings are strong priors; in-season
> evidence must be repeated and strong to override them.
