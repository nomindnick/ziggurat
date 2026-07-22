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

Next: **2.4 draft board TUI** → Checkpoint 2 rehearsals. Calendar anchors:
draft expected mid-to-late August 2026, still unscheduled (monitor ESPN —
2 of 10 seats still invite-pending as of 2026-07-21; re-snapshot the room near
draft day); NFL Week 1 ~Sept 10 (Phase 3 must precede it).

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
  league/            league state, opponent layer, Monte Carlo (Phase 3+)
  draft/             DELETABLE draft tool (Phase 2, deleted after draft day)
  llm/               the LLM routing interface (router.py, backends.py)
  cli/               thin Typer commands — no logic
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
```

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
