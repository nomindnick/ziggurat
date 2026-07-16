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

**Phase 1 (Ground Truth & Data Spine) — in progress.** Phase 0 complete
2026-07-16. Both scheduled-first spikes closed 2026-07-16:
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
brackets incl. the explicit implicit-zero bands — all values transcribed from
spike 1.1 and locked to the ESPN fixture. 112 tests green; triple-derived and
adversarially reviewed (see the 1.3 Update block).

Next: **1.4 NFL data ingestion**, then 1.5, then Checkpoint 1. Calendar anchors:
draft expected mid-to-late August 2026 (Phases 0–2 must precede it); NFL Week 1
~Sept 10 (Phase 3 must precede it).

Update this section whenever a phase or checkpoint closes.

## Standing rules (non-negotiable, from the SPEC)

1. **`as_of` on every data read.** Every read accessor takes a keyword-only
   `as_of` (no default, no implicit "now") and returns only what was knowable
   at that moment. Every accessor ships with a leakage test. Convention:
   `ziggurat/data/asof.py`; exemplar to copy: `tests/test_asof_pattern.py`.
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
db/schema.sql        public schema; the .sqlite file itself is gitignored
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
