# Implementation Plan: Ziggurat

> **Reference:** See [SPEC.md](./SPEC.md) for full project context, architecture decisions, and feature details. The SPEC is the "what and why"; this document is the "in what order and how we'll know."

## How to Use This Plan (read first)

This plan is deliberately looser than a conventional sprint plan, because this project has an unusually high ratio of discovery to construction: unofficial APIs, uncertain data archives, and signals whose value is unknown until tested. Pretending to know Sprint 7's deliverables in advance would be fiction with acceptance criteria. So:

- **Work items are goals, not prescriptions.** Each item states the goal, what "done" observably means, and open questions — not step-by-step tasks. Claude Code chooses the implementation path and records it.
- **Three item types.** **[Build]** items produce working, tested code. **[Spike]** items produce *answers* — a findings note in `intel/research/` is the deliverable, and "we can't get X, here's the fallback" is a successful spike. **[Checkpoint]** items are scheduled re-planning moments where spike results and lessons redirect the plan; editing this document at a checkpoint is expected behavior, not scope creep.
- **Updates are the memory.** Every item has an **Update** block. Fill it in when the item closes: what was done, what was learned, decisions made, anything the next item needs. These blocks are the cross-session continuity record and carry more weight than usual — treat them as first-class output.
- **Calendar anchors (2026):** today is mid-July. The draft is unscheduled but expected mid-to-late August — **Phases 0–2 must be done before it.** NFL Week 1 is ~September 10 — **Phase 3 must be done before it.** Phases 4–5 are rolling and in-season by design.
- **Standing rules from the SPEC apply to every item:** `as_of` on every data accessor; house rules only in `core/scoring.py`; no logic in the CLI layer; no LLM called except through the routing interface; explainable outputs; nothing league-private or colleague-identifying in committed files (public repo).

**Estimated effort:** the pre-draft critical path (Phases 0–2) is roughly 25–40 focused hours; Phase 3 another 15–25 before Week 1. Phases 4–5 are season-long by design. With Claude Code doing the construction, calendar risk lives in the spikes and checkpoints, not the typing.

---

## Phase 0: Foundations

**Goal:** A scaffolded, public-safe repo where every later convention already exists in miniature. At phase end, the skeleton runs, tests pass, and the three retrofit-hostile rules (`as_of`, single scoring module, model routing) are load-bearing from the first commit.

### 0.1 [Build] Repo scaffold & conventions
**Goal:** The monorepo layout from the SPEC exists: package skeleton (`data/ core/ league/ draft/ cli/`), `backtest/`, `intel/` tree with starter templates, `db/schema.sql` stub, `tests/`, `pyproject.toml`, CI-less but `pytest`-green. `.gitignore` enforces the public-repo boundary (`intel/`, `db/*.sqlite`, `data/`, `.env`) — verified by a test or pre-commit check that fails if boundary paths are staged.
**Done when:** fresh clone → install → `pytest` passes; a deliberate attempt to commit a file under `intel/` is blocked/flagged.
**Update:**
> **Done 2026-07-16.** Full layout landed (package + `backtest/` + `db/schema.sql` stub + `config/` + `tests/`); boring choices throughout: hatchling build, Typer CLI, pytest via `[dev]` extra. **Boundary enforcement is three-layered and shares one pattern list** (`ziggurat/repo_guard.py`): anchored `.gitignore` rules, a versioned pre-commit hook (`scripts/hooks/pre-commit`, activated per clone via `git config core.hooksPath scripts/hooks` — documented in README/CLAUDE.md), and `tests/test_repo_boundary.py`. Gotcha caught and tested: ignore patterns must be anchored (`/intel/`, `/data/`) or they swallow the public `ziggurat/data/` package and `templates/intel/`. Since `intel/` is gitignored, its starter templates live committed at `templates/intel/` and `ziggurat intel init` copies missing files into `intel/` (never overwrites operator notes) — fresh-clone bootstrap. Verified: fresh clone from GitHub → venv install → 39 tests green; forced staging of `intel/_canary.md` blocked by the hook at commit time.

### 0.2 [Build] The three spine abstractions
**Goal:** (1) A data-access convention where every read accessor takes `as_of` — with a leakage test pattern established on a toy table. (2) `core/scoring.py` stub with the golden-master test harness shape (stat line in → points out). (3) The LLM routing interface: one entry point, task tags (stakes tier), backends as config (`claude -p` / Claude API / Ollama), with a no-op or echo backend for tests.
**Done when:** each abstraction has at least one passing test demonstrating the pattern a later module will copy.
**Update:**
> **Done 2026-07-16.** (1) **as-of:** convention documented in `ziggurat/data/asof.py` (keyword-only `as_of`, no default/no implicit "now"; ISO-8601 TEXT knowledge-time columns, inclusive end-of-day semantics; `normalize_as_of()` shared by all accessors); exemplar leakage test on a toy snapshot table in `tests/test_asof_pattern.py` using a correlated-subquery latest-snapshot-per-key query (deliberately avoids SQLite's bare-column-with-MAX quirk so the copied pattern is portable SQL). (2) **scoring:** `core/scoring.py` carries the golden-master harness (table-driven stat-line→points cases, nflverse stat-key naming, None/NaN-safe) with **all numeric weights loudly marked PLACEHOLDER pending 1.3**; D/ST and K scoring deliberately `raise NotImplementedError` until real league settings are transcribed — refusing beats guessing on the rules that are the core edge. (3) **LLM router:** `ziggurat/llm/` routes task tags → backends per `config/llm.toml` with stakes tiers (routine/standard/high_stakes); unregistered tags and config typos raise; echo backend live for tests, `claude_cli`/`anthropic_api`/`ollama` registered but NotImplemented until 3.6/4.5; backends injectable for the bake-off. `ziggurat smoke` exercises all three spines end to end.

### 0.3 [Build] CLAUDE.md constitution v0
**Goal:** First working version: repo map, the standing rules above, item-update discipline, sanity-check requirements (e.g., never recommend a player ruled OUT), and placeholders for the weekly cadence (filled in Phase 3) and heuristics promotion criteria (filled in Phase 5).
**Done when:** a fresh Claude Code session, given only the repo, correctly states the standing rules and current phase when asked.
**Update:**
> **Done 2026-07-16.** CLAUDE.md v0 written: status header (phase + calendar anchors, with an instruction to update it at every phase/checkpoint close), eight standing rules (the SPEC's five plus plan-update discipline, explainability-with-in-code-sanity-checks, and draft-package isolation), repo map, dev workflow (including the per-clone `core.hooksPath` step and the Phase-1 `.env` credential prep), and explicit placeholders for the weekly cadence (3.7) and heuristics promotion criteria (5.2). Verified per the done-when: a fresh-context agent given only the repo correctly recited the standing rules, current phase, and boundary paths.

---

## Phase 1: Ground Truth & Data Spine

**Goal:** All Tier-1 data flows in, time-aware, and the league's house rules are encoded and verified. This phase contains the project's two highest-risk unknowns — both are spikes, both are scheduled first.

### 1.1 [Spike] ESPN access — what can we actually read?
**Questions to answer:** Does `espn_api` authenticate against Sac LS Berry Patch (SWID/espn_s2)? What's readable: rosters, standings, transactions, free agents, **ESPN default rankings**, draft results? Are the custom scoring settings (D/ST yards-allowed brackets, kicker distance/miss rules) exposed programmatically, or must house rules be hand-transcribed from the settings page? What does the pre-draft league state even look like through the API? How fragile does the wrapper feel (what breaks, what needs caching)?
**Done when:** findings note in `intel/research/espn-access.md`, including a snapshot of pulled league settings and a decision: which fields sync automatically vs. get hand-maintained.
**Update:**
> **Done 2026-07-16.** `espn_api` (0.46.0, now a project dep) authenticates against the private league with the `.env` `SWID`/`espn_s2` cookies on the first try; full findings in `intel/research/espn-access.md` (gitignored — holds the league-specific snapshot). **Headline (de-risks 1.3): the full custom scoring is machine-readable — no scoring value needs hand-transcription.** Both `League.settings.scoring_format` (statId → human label already resolved) and the raw `mSettings` view return all 46 scoring items, including the two edge-defining house rules: distance-based kicker (FG 0–39=3 / 40–49=4 / 50–59=5 / 60+=6, PAT +1, **missed FG −1**) and D/ST **points-allowed** (0=+5 … 46+=−5) **and yards-allowed** (<100=+5 … 550+=−7) brackets, atop full PPR (1.0/rec, 0.04/pass yd, 0.1/rush+rec yd, 4/pass TD, 6/rush+rec TD). **Gotcha for 1.3:** ESPN lists only non-zero brackets, so the gaps are implicit zeros (18–27 pts→0, 300–349 yds→0) — reconstruct boundaries from label ranges, never assume contiguity. Captured as committed fixture `tests/fixtures/espn/scoring_format.json` + offline contract test `tests/test_espn_access.py` (the cached-fixture pattern 1.4/1.5 copy). **Pre-draft state:** API exposes settings + all 10 managers but empty rosters until the Aug draft; config confirms plan assumptions — SNAKE draft @ 60s/pick (date unset), TRADITIONAL free-claim waivers w/ 24h period (matches 3.4's model; verify `acquisitionBudget=100` semantics), trade veto = **4 votes** (confirms 5.3's constraint), 14-week reg season / 6 playoff teams, 9-starter lineup (QB/2RB/2WR/TE/FLEX/D-ST/K, 7 bench, 1 IR). **Fragility:** ESPN moved the reads host to `lm-api-reads.fantasy.espn.com` (old `fantasy.espn.com/apis/v3` returns empty); cookies are the sole auth and will need periodic refresh (3.1 must fail loudly on 401/403); historical seasons use the `leagueHistory` endpoint form. **Decision — auto-sync:** scoring, roster/lineup, draft/waiver/trade/schedule settings, the 10 managers, rosters, standings, matchups, transactions, free agents. **Hand-maintain:** only a one-time golden hand-check of `scoring.py` vs. the settings UI (the 1.3 anchor) + post-Week-1 box-score validation — no scoring values entered by hand.

### 1.2 [Spike] Historical market archives — the #1 risk
**Questions to answer:** Per SPEC Known Challenge 1 — what actually exists for point-in-time market expectations, 2021–2025? Test in order: historical DFS salaries (coverage, format, licensing), FantasyPros archives, Wayback snapshots, in-season ADP archives, historical roster-percentage data. What lead-time benchmark can we honestly construct from what's available?
**Done when:** findings note in `intel/research/market-archives.md` with a concrete recommendation: full backtest program / reduced program / usage-signals-only fallback — this scopes Phase 4 at Checkpoint 1.
**Update:**
> **Done 2026-07-16.** Recommendation: **FULL backtest program.** Run as a multi-agent research workflow (6 candidate sources × investigate → adversarial-verify → synthesize); full findings in `intel/research/market-archives.md` (gitignored). **Two independent, free, point-in-time WEEKLY market proxies span all of 2021-2025**, so the feasibility fear is retired:
> - **DynastyProcess `db_fpecr`** (primary = market *expectation*): one free ~38 MB parquet mirroring weekly FantasyPros Expert Consensus Rankings (355 Friday scrapes), append-only and **empirically immutable** — a 2-yr-old copy diffed byte-identical across 1.26M shared rows (zero in-place revisions). Weekly PPR ranks incl. K/DST with ecr/best/worst/sd dispersion.
> - **Sleeper `/research` ownership** (corroborating = market *attention*): free weekly percent-rostered + start-rate, frozen per-week snapshots (verified via a retired player's frozen past values); use week-over-week deltas.
> Both independently spot-checked from the main loop (Sleeper 2023 wk6 → 788 players; parquet → live 38 MB). **Honest benchmark:** a frozen local weekly market panel 2021-2025; lead metric = weeks between a Ziggurat usage/opportunity flag at T and the ECR re-rank at T+1/T+2, corroborated by ownership delta. Caveats: ECR is a *softer* bar than sharp Vegas money and weekly-resolution; kickers get no salary/props proxy (ECR only). **Rejected/limited:** Wayback ECR (bimodal — dense 2021-22, collapses to 0-2 snapshots/season 2023-25; superseded by db_fpecr); DFS salaries (rotoguru free but 2021-only, FantasyData paid + unverified immutability); ADP (draft-frozen, preseason baseline only); The Odds API player props (sharper but paid, 2023-05+ only — optional hard-tier cross-check). **Phase 4 scoping input for Checkpoint 1:** build against the db_fpecr weekly panel + Sleeper ownership deltas, pin our own db_fpecr mirror for provenance, scope lead-time claims to "lead over expert consensus," optionally layer Odds API props for 2023+. Harvested rankings/ownership stay under gitignored `data/` — never committed (rule 5). _(Workflow footnote: 2 of 13 agents hit mechanical faults — one verify + the synthesizer tripped on output-schema validation — but every source has a complete verified finding; db_fpecr's integrity was cross-verified by a second agent, and the two spanning sources were re-checked by hand.)_

### 1.3 [Build] House-rules scoring engine
**Goal:** `core/scoring.py` complete: full PPR, D/ST points-allowed **and** yards-allowed brackets, distance-based kicker scoring with −1/miss — transcribed from the league settings pulled in 1.1. Golden-master tests from hand-computed stat lines; a post-Week-1 validation task (compare against real ESPN box scores) gets a TODO anchor now.
**Done when:** golden tests pass, including deliberately nasty edge cases (D/ST safety + bracket combos, missed XP vs. missed FG).
**Update:**
> **Done 2026-07-16.** `core/scoring.py` now encodes the full house rules transcribed from the spike-1.1 ESPN settings — every value is real (no placeholders), living only in a frozen, swappable `ScoringRules`. **Offense:** full PPR incl. the three 2-pt conversions and the three nflverse fumble components (`sack_/rushing_/receiving_fumbles_lost`; `fumbles_lost` accepted as a pre-summed projection alias). **Kicker:** distance-tiered FG (0–39/40–49/50–59/60+ → 3/4/5/6) priced through one `_bracket_points` path shared by the count form and the raw-distance form, PAT +1, **−1 per missed FG**, and — the plan's named edge — a deliberately absent missed-PAT penalty so a **missed XP scores 0**. **D/ST:** BOTH bracket systems — points-allowed (0→+5 … 46+→−5) and yards-allowed (<100→+5 … 550+→−7) — with the **implicit-zero bands (18–27 pts, 300–349 yds) encoded explicitly** as contiguous partitions, plus events, the six +6 defensive/return TDs collapsed to `def_tds`, and the exotic 1-pt-safety / 2-pt-return. **Skip-absent semantics:** an absent/NaN/None `points_allowed`/`yards_allowed` is skipped (never coerced to 0), so a data gap can't award a phantom shutout; present-with-0 is a real shutout (+5). Dispatch also accepts Sleeper's `DEF` and `PK` labels.
>
> **Tests — 112 green.** A transcription-LOCK test asserts each of the 46 ESPN fixture values equals what the engine encodes (a new/changed statId fails CI); a boundary-lock test parses the ESPN labels and reproduces every PA/YA edge from ground truth; golden cases hit every bracket boundary, every kicker bucket, missed-XP-vs-FG, 2-pt, D/ST safety+bracket combos, exotics, and negatives; plus NaN/None-in-brackets, rules-swappability across all three families, and pandas-shaped inputs (scalar-NaN / string distance cells, non-float NaN carriers). `tests/test_cli.py` updated off the now-retired PLACEHOLDER honesty marker.
>
> **Verification (multi-agent, ultracode).** Three independent agent derivations confirmed every value/sign and both implicit-zero boundaries (zero discrepancies); 86 adversarially-generated + independently-recomputed golden cases all execute correctly against the engine; a 4-lens adversarial code review surfaced 15 verified findings, and the material ones were fixed here: nflverse `fumbles_lost` has no source column (the −2 would have silently evaporated), `_is_present` missed NaN in non-float carriers (→ phantom +5), `fg_made_distances` crashed on a scalar-NaN pandas cell, and the FG count keys were positionally coupled to the bracket table.
>
> **Deferred to the consumers that need them (noted, not built now):** a per-component score breakdown for the explainer (→ 2.1 / 3.x); an opt-in strict unknown-key validator + a published canonical key-set for the ingestion mapper (→ 1.4/1.5); a negative-bracket-input guard (→ ingestion validation). **Post-Week-1 TODO anchored** (item 3.8, and boxed in `scoring.py`): reconcile against real ESPN box scores and pin the two ingestion-layer definitional subtleties — exact `points_allowed`/`yards_allowed` derivation, and return-TD attribution (no double-count with individual returners).

### 1.4 [Build] NFL data ingestion
**Goal:** nflverse clients for weekly stats, usage (snap/target/route/red-zone shares), expected stats, depth charts, injuries (weekly archive plus live snapshot support; historical daily trajectories and rest baselines tracked below), and schedules — landed in SQLite with `as_of` semantics and multi-season history (≥2021). Player IDs use the nflverse/DynastyProcess crosswalk, with validation tests for rookies/D-ST gaps.
**Done when:** a query like "usage deltas for all RBs as of 2023 week 6" returns correct, leakage-tested results.
**Update:**
> **Done 2026-07-16.** nflverse ingestion landed under `ziggurat/data/nfl/` with strict as-of leakage discipline. **Schema** (`db/schema.sql`, v1): 10 tables — `players` (cross-ID crosswalk), `schedules`, `weekly_stats`, `snap_counts`, `ngs_receiving/rushing/passing`, `depth_charts`, `injuries` — each carrying two knowledge-time columns. **The leakage model is the load-bearing decision:** `knowable_as_of` records when a fact became public; `retrieved_as_of` records when this system obtained that version. Safe-default `historical` reads gate both. Explicit `latest_truth` reads gate fact time only and are reserved for corrected outcome grading or deliberately accepted immutable bulk history (`base.select_as_of`). Each source is a thin client wrapping one `source.import_*` seam + a keyword-only `as_of` accessor + a leakage test; the pfr↔gsis crosswalk stitches PFR-keyed snaps to gsis-keyed stats (99.7% bridge), numeric IDs are normalized for ESPN/Sleeper joins, and the D/ST + rookie crosswalk gaps are validated. **Done-when met:** `usage.usage_deltas` returns leakage-tested week-over-week RB usage deltas (differenced against each player's most-recent *prior knowable* week, so the bye / injury-return cohort is surfaced, not dropped) — as-of 2023 wk6 it flags Jonathan Taylor's post-holdout snap-share jump.
>
> **Foundation stabilization (2026-07-20):** deprecated `nfl_data_py` was replaced by declared dependency `nflreadpy` behind `ziggurat/data/nfl/source.py`; one normal editable install now provisions the data client. Adapter tests stay offline and ingesters reject missing upstream columns. SQLite now bootstraps v1 once, applies ordered migrations, and reached v2 with temporal query indexes.
>
> **Verification (multi-agent, ultracode):** a 5-way build fan-out produced the sources; a 4-lens adversarial leakage audit (every finding independently verified) surfaced 9 confirmed issues, all resolved here. The material ones: (a) **[HIGH]** a single temporal view could not distinguish strict historical reconstruction from corrected bulk truth — superseded by explicit `historical` and `latest_truth` modes; (b) **[HIGH]** injury rows lacking `date_modified` fell back to the week's *first* gameday, leaking late-week teams' reports — fixed to the player's own team gameday; (c) **[HIGH]** playoff schedule rows were stamped with the preseason anchor, leaking the bracket — fixed to gameday; (d) a silent **LA/LAR** team-abbr mismatch dropped ALL Rams NGS — fixed via `TEAM_ALIASES`; (e) depth-chart NULL-in-PK rows duplicated on re-ingest and were invisible to reads — fixed via sentinel coalesce; plus snap-delta None-vs-0 disambiguation, drop-path logging, and a pfr-collision guard. The offline cached-fixture, migration, adapter, and leakage suite is green.
>
> **Forward items recorded:** (1) **Historical injury trajectories.** The bulk archive supplies one final player-week row; Wednesday→Friday trajectory backtests require a daily archive, while live 2026 trajectories require repeated scheduled pulls. Per-player rest-day baselines remain a Phase-3 consumer. (2) **Intraday knowledge time (→ Phase 3 live loop).** The as-of gate is DAY-granular (inclusive end-of-day) — correct for the backtest, but it cannot express a Sunday-morning-before-kickoff moment. The live loop's per-player kickoff locking / inactive-report sequencing (SPEC) will need sub-day knowledge times (kickoff timestamps). (3) **D/ST team-defense stats gap (→ before the Phase-4 backtest).** `weekly_stats` is player-level only; team-defense weekly inputs for scoring D/STs (points/yards allowed, sacks, def TDs) are not in `import_weekly_data` and need a team-defense source (pbp aggregation or `import_team_desc`).

### 1.5 [Build] Projections, ADP, odds, weather ingestion
**Goal:** Current-season consensus projections (full stat lines), preseason ADP distributions (market source + ESPN default rankings side by side — the divergence table is a first-class artifact), Vegas totals/spreads, and the Open-Meteo weather client keyed to stadium coordinates/dome flags. News headline speed-lane ingestion can land here or in 3.6 — Claude Code's call.
**Done when:** each source lands in SQLite with `as_of`; the ESPN-vs-market divergence report runs and produces a readable table.
**Update:**
> **Done 2026-07-20.** Five sources land in SQLite under `ziggurat/data/nfl/` behind
> migration `003_market_context.sql` (five tables, each with the two knowledge-time
> columns + a temporal index; `schema_version` now 3), all following the item-1.4
> ingester pattern (thin `source.import_*` seam → `require_columns` fail-loud →
> knowledge-time stamp → `note_drops` (never NULL-insert) → `base.upsert`; keyword-only
> `as_of` accessor through `base.select_as_of`; leakage + cached-fixture tests each).
> **Done-when met:** the ESPN-vs-market **divergence report** (`core/divergence.py`, thin
> `ziggurat divergence` CLI) runs end-to-end against real FantasyPros data and prints a
> readable positional-rank divergence table. Design doc: `intel/research/ingestion-1.5-design.md`.
>
> **Sources:**
> - **team_defense** (`load_team_stats` weekly + schedules scores) — the D/ST ride-along.
>   One `(season, week, team)` row named with `scoring.py` D/ST keys so `dict(row)` prices
>   directly through `score_dst` (verified live: KC 2023-wk1 = 2.0, DET = 8.0). The line is
>   DERIVED: `fumble_recoveries = fumble_recovery_opp` (not `_own`); `def_tds = def_tds +
>   fumble_recovery_tds + special_teams_tds` (probed: `def_tds` excludes fumble-return TDs);
>   `blocked_kicks` from the OPPONENT's kicking row; `points_allowed` = opponent final score;
>   `yards_allowed` = opp `passing+rushing+sack_yards_lost`. `knowable_as_of` = the team's own
>   gameday. Any row whose opponent self-join or schedules score fails to resolve is DROPPED
>   (a NULL bracket input is silently skipped by `score_dst`).
> - **game_odds** (`load_schedules` odds columns → own `game_odds` table, own patch seam) —
>   closing spread/total/moneylines, `knowable_as_of = gameday` (leakage-safe; a same-day
>   pre-kickoff caller must pass `D-1`). Null-odds rows KEPT, null-gameday dropped. Kept OUT of
>   the structural `schedules` table by design.
> - **weather** (Open-Meteo forecast + ERA5 archive, stadium-keyed) — `game_weather`, decision
>   context only (no scoring contact). Two-regime `forecast_source`: `forecast` (knowable =
>   retrieved = pull day) vs `archive_actual` (knowable = gameday; grading reads via
>   `latest_truth`, which relaxes only the retrieval gate). Committed public `_STADIUM_COORDS`
>   reference (36 venues incl. international). Fixed domes never fetch.
> - **projections** (Sleeper `sleeper_rotowire`, undocumented endpoint) — full stat line under
>   `scoring.py` canonical keys so a row scores directly; `projected_points` stored as a
>   cross-check only. Strict `validate_projection_keys` imports the `scoring.py` key-sets (no
>   re-hardcoded values) — this is the unknown-key validator 1.3 deferred here.
> - **adp_rankings** (`load_ff_rankings` FantasyPros ECR) — market rankings, `knowable_as_of =
>   scrape_date`; IDP dropped, DST kept NULL-`gsis_id` and joined by normalized team abbr
>   (`JAC→JAX` alias added). New `base.ids_by_fantasypros` crosswalk helper.
>
> **PROJECTIONS SCOPE DECISION (operator-confirmed 2026-07-20).** Free, leakage-clean,
> point-in-time *historical* (2021-2025) stat-line projections do NOT exist: Sleeper's endpoint
> returns historical rows but its `last_modified` is null or a POST-game batch stamp, failing the
> spike-1.2 point-in-time bar that `db_fpecr` cleared. Decision: projections are **current-season-
> forward** (pull pre-game, stamp knowable=retrieved=pull day; feeds 2.1 + the live loop); a bulk
> historical backfill is permitted ONLY under the explicit `latest_truth` view (knowable =
> `week_first_gameday`), never presented as a reconstructed pre-game snapshot. This amends the
> plan's "consensus projections" wording — the source is a **single provider (Rotowire), not a
> consensus**. **Phase-4 backtest consequence:** the verified point-in-time market signal remains
> spike-1.2's `db_fpecr` weekly ECR (which lands in `adp_rankings`); a projection-driven backtest
> series is exploratory until Phase 4 verifies/reconstructs a trustworthy series.
>
> **Verification (multi-agent, ultracode).** Three sequential workflows: (1) read-only recon —
> five source-domain probes → adversarial verify → synthesis into the locked design doc; (2) build
> — shared-foundation barrier then five per-source modules + divergence in parallel; (3) adversarial
> leakage + correctness audit, one skeptic per module re-deriving against REAL nflverse data. The
> audit found **no leakage bugs** (the load-bearing property held across all six accessors) and 4
> confirmed correctness findings, all fixed here: **[MED]** weather selected the wrong game-hour for
> every non-Eastern venue (ET `gametime` indexed into a stadium-local hourly array) — fixed to fetch
> in ET (`_SCHEDULE_TZ`) with `kickoff_local` converted to true local via `zoneinfo`; **[LOW]** the
> stadium-completeness test only exercised the 2023 fixture — replaced with a frozen 36-venue set;
> **[LOW]** the projections kicker mapper silently dropped `fgm_0_19` (sub-20-yd makes scored 0 not
> +3) — folded into the 0–39 bucket with a regression test; **[LOW]** the divergence confidence gate
> compares a positional delta against overall-scale `sd` (units mismatch) — documented honestly and
> the columns labelled `sd(ovr)`/`spread(ovr)` (a positional-scale gate + VBD weighting land with
> valuation in 2.1; rule 6). Suite green: **212 passed** (from 167), `ziggurat smoke` + repo-boundary
> clean.
>
> **Forward items / deferrals.** (1) Exact ESPN D/ST `points_allowed`/`yards_allowed` charge
> semantics (opponent def/return TDs & safeties scored against our offense — ESPN does not count
> them; v1 over-charges) → **item 3.8** post-Week-1 box-score reconciliation; audit columns
> (`team_score`/`opp_score`) retained so it refines without re-ingesting. (2) Kicker 50–59 vs 60+
> cannot be split from Sleeper's `fgm_50p` (a 60+ FG scores +5 not +6, rare). (3) News headline
> speed-lane deferred to **3.6** (in-season concern). (4) Live ESPN-side rank snapshot for the
> divergence report → **item 3.1** (the report reads ESPN-side rows from JSON until then). (5) Odds
> API 5-min snapshots, `db_fpecr` weekly-panel backfill, and a trusted historical projection series
> → **Phase 4**. (6) Weather live forecast-pull cadence → the weekly loop (**3.7**); sub-day/intraday
> knowledge time remains the standing Phase-3 enhancement.

### ✦ Checkpoint 1: Data spine review
Re-plan with spike results in hand: scope the Phase 4 backtest program per 1.2's recommendation; adjust Phase 2 for anything 1.1 revealed (especially draft-results visibility and settings fidelity); record decisions in the Update blocks and amend this plan.
**Checkpoint notes:**
> **Held 2026-07-20. Phase 1 is closed (1.1–1.5 done).** The two scheduled-first
> spikes retired the project's two biggest unknowns — historical market archives
> (1.2 → `full_backtest`) and ESPN access/scoring fidelity (1.1 → full custom
> scoring machine-readable) — so **no plan-structural surprises**; the phases stand
> as written and this checkpoint is refinement, not redirection. Decisions:
>
> **A. State of the data spine.** In SQLite now (schema v3, strict as-of): players
> crosswalk, schedules, weekly stats, snaps, NGS, depth charts, injuries (1.4);
> team_defense, game_odds, game_weather, projections, adp_rankings (1.5); scoring.py
> locked to the ESPN fixture (1.3). ESPN access proven for settings + rankings +
> (in-season) rosters/matchups/transactions (1.1). **The one Phase-1-scoped-but-
> unbuilt piece is the historical market PANEL** — 1.5 deferred the db_fpecr weekly-
> ECR backfill and the Sleeper ownership series to Phase 4; that ingester is now the
> first deliverable of item 4.1 (below), not a gap in the live spine.
>
> **B. Phase 4 backtest program — scoped per 1.2 (`full_backtest`).** The benchmark
> is a **frozen local weekly market panel, 2021-2025**: (1) **PRIMARY expectation** =
> DynastyProcess `db_fpecr` weekly PPR ECR (`ecr_type='wp'`, carries `ecr/best/worst/sd`
> incl. K/DST), ingested under the **`latest_truth`** view (immutable accepted bulk
> history — empirically zero in-place revisions over a 2-yr diff), NFL week inferred
> from `scrape_date`, the in-progress edge week dropped, off-cadence scrapes deduped,
> **our own copy pinned/mirrored** for durable provenance; (2) **CORROBORATING
> attention** = Sleeper `/research` ownership **week-over-week deltas** (frozen weekly
> snapshots; use deltas, not absolute levels — population is Sleeper's base, not our
> 10-team ESPN room); (3) **optional HARD tier** = The Odds API player props for the
> **2023-05+** window only, reported separately so a "lead over ECR" is never
> overclaimed as a lead over sharp money. **Lead metric:** weeks from a Ziggurat
> usage/opportunity flag at T to the ECR re-rank at T+1/T+2, plus hit-rate;
> **precision@k, k≤3** (the real weekly claim budget). **Discipline:** train 2021-23 /
> holdout 2024-25, grade decisions not outcomes, all reads through `latest_truth`
> accessors. **Honest limits recorded:** ECR is usage-influenced (a softer bar than
> Vegas money); weekly (not intraday) resolution; **K/DST lead grading is weaker**
> (ECR-only, coarse dispersion) than skill positions. Team_defense (1.5) enables D/ST
> decision replay. **Historical stat-line projections are infeasible** (1.5 decision) —
> a projection-driven backtest series is exploratory only until Phase 4 verifies or
> reconstructs one; the verified point-in-time market signal is `db_fpecr`, not
> projections. Items 4.1/4.2 amended below.
>
> **C. Phase 2 adjustments — per 1.1.** (1) **Settings fidelity is excellent**, so
> **2.1 replacement levels use the exact decoded roster structure** (10 teams ×
> QB/2RB/2WR/TE/FLEX/D-ST/K, 7 bench, 1 IR; `TOTAL_POINTS_SCORED` seeding) with **no
> hand-maintenance**. (2) **ESPN's own PPR draft ranks / ADP are reachable via the
> same auth**, so **2.1's ESPN-vs-market divergence** (the 1.5 `core/divergence.py`
> report is the foundation) wires live ESPN ranks for draft day (the report currently
> reads the ESPN side from JSON; live ESPN rank pull lands with 2.1, ahead of the 3.1
> scheduled sync). (3) **Prior-season draft results + final rosters are available via
> the `leagueHistory` endpoint** — a concrete enrichment 1.1 unlocks: **calibrate the
> 2.2 mock-sim opponent model and 2.3 opponent-need modeling on the room's ACTUAL past
> behavior**, not solely the ESPN-rank+noise assumption. (4) **Draft date still unset**
> (SNAKE, 60s/pick) → that 60s clock is Checkpoint 2's rehearsal target; monitor ESPN
> for the schedule. Items 2.1/2.2/2.3 amended below.
>
> **D. Open confirmations carried forward (non-blocking).** `acquisitionBudget=100`
> semantics — season transaction-count cap vs inert default — verify against the ESPN
> UI (→ 3.4). Cookie (SWID/espn_s2) expiry must fail loud on 401/403 (→ 3.1). Trade
> deadline (epoch ≈ early-Dec 2026) localized precisely (→ schedule module). Post-Week-1
> box-score reconciliation of scoring.py D/ST charge semantics stays anchored at 3.8.
>
> **E. Housekeeping.** SPEC.md's tech-stack/data-model still name `nfl_data_py` /
> `import_ids()`; that dependency was replaced by `nflreadpy` in 1.4 (recorded in
> CLAUDE.md + item 1.4). The SPEC is the descriptive "what & why"; the operative
> record is this plan — noting the drift here rather than rewriting the SPEC.
>
> **F. Sequencing decision.** **Nothing in Phase 4 blocks draft day.** Phase 2
> (valuation core + draft weapon) is the sole draft-critical path, and with the draft
> expected mid-to-late Aug and unscheduled, the ~4–6-week window is the binding
> constraint — **Phase 2 begins next**; Phase 4 rolls alongside/after per its rolling
> design. Phase-1 exit criteria (all Tier-1 data time-aware in SQLite; house rules
> encoded and verified) are met.
>
> **G. Amendments applied at this checkpoint:** items 2.1, 2.2, 2.3, 4.1, 4.2 (inline
> **Checkpoint-1 amendment** notes below).

---

## Phase 2: Valuation Core & Draft Weapon

**Goal:** Global valuation under house rules, and a draft-day system rehearsed to the point of boredom. **Hard deadline: the draft.** The draft tool is a deletable wrapper; everything else here is permanent.

### 2.1 [Build] Global valuation (VOR)
**Goal:** Re-score consensus projections through `scoring.py`; compute replacement levels from league size/roster structure; produce ranked global values with the house-rules delta vs. ESPN default rankings surfaced explicitly (the "what the room can't see" report).
**Done when:** valuation runs end-to-end from ingested data; spot-checks on known league quirks behave (e.g., pass-catching RBs and league-scored D/STs move the right direction vs. default ranks).
**Checkpoint-1 amendment (2026-07-20):** replacement levels use the exact decoded roster structure from 1.1 (10×[QB/2RB/2WR/TE/FLEX/D-ST/K, 7 bench, 1 IR]) — no hand-maintenance. The "what the room can't see" report **builds on the shipped `core/divergence.py`** (item 1.5) — wire live ESPN PPR draft ranks/ADP (reachable via `espn_api`, same auth) as the ESPN side here, ahead of the 3.1 scheduled sync. Projections re-score through `scoring.py` via the 1.5 `projections` table + strict key validator.
**Update:**
> **Done 2026-07-20.** Global static VOR/VBD board + the "what the room can't
> see" value view. **Landed:** `core/valuation.py` (RosterStructure,
> ValuationRow, `build_valuation`, `replacement_levels`, `build_value_view`,
> formatters), the live ESPN board client (`data/nfl/espn_source.py` raw
> `kona_player_info` seam + `data/nfl/espn_ranks.py` mapper/ingest/accessor),
> migration `004_espn_ranks.sql` (`espn_draft_ranks`, `schema_version` 4),
> `base.espn_by_gsis` crosswalk, and a thin `ziggurat valuation [--espn]` CLI.
> Built via three verified workflows (recon → parallel build → adversarial
> audit); locked design in `intel/research/valuation-2.1-design.md`.
>
> **Key decisions:**
> - **Per-week-then-sum (D1) — the load-bearing correctness call.** Score EACH
>   weekly projection row through `scoring.py`, THEN sum; never sum a stat line
>   then score once. `score_dst`'s yards-allowed brackets are non-linear, so a
>   summed season of yards (~3500) buckets into the worst band every week (−7 vs
>   +2), erasing the whole house D/ST edge. Verified on real data + a regression
>   test that doubles as the proof.
> - **Source = the 1.5 weekly `projections` table summed over NFL weeks 1..17**
>   (fantasy window; wk18 rest week excluded — tunable via `weeks`). The Sleeper
>   *season* endpoint is spec'd as an OFFENSE-ONLY fallback (it silently drops
>   both D/ST brackets, every sub-40 FG make, and the miss penalty for K/DST) —
>   **not needed**: 2026 weekly offense+K+DST are fully populated wk1..17.
> - **Replacement = first-non-starter with EMPIRICAL flex allocation** (pool the
>   best RB/WR/TE leftovers beyond dedicated starters into the 10 flex slots),
>   superflex-guarded (no QB in flex; QB started stays 10), K/DST baselines
>   rank-window-denoised. All tunable via the frozen `RosterStructure` for 2.2/2.3
>   calibration. VOR = house season points − positional replacement.
> - **ESPN side wired live (Checkpoint-1 amendment).** Raw `kona_player_info`
>   request via `espn_api` (NOT `free_agents()`), editorial PPR board rank
>   (`draftRanksByRankType["PPR"]["rank"]`, `rankSourceId=0`) as the PRIMARY
>   `espn_pos_rank`, native `averageDraftPosition` stored as a secondary ADP lens;
>   persisted to `espn_draft_ranks`, as-of-gated + leakage-tested, stamped
>   knowable=retrieved=pull day (live board). Two real catches: the pool is **1025
>   players, not 1000** (the recon's own probe was truncating; default `limit`
>   raised to 2000 behind a fail-loud `len>=limit` guard, add pagination past
>   ~2000), and DST rows carry a NULL `espn_id` that breaks `select_as_of`'s
>   per-key equijoin → a non-null `board_key` (str(espn_id) skill / team DST) is
>   the temporal+PK key so no DST silently vanishes from an as-of read.
> - **The house edge is K and D/ST, not offense** (D10). In full PPR, house
>   offense scoring ≈ Sleeper PPR default (only QB shows a real offense delta), so
>   the value view is house VALUE (scarcity-priced VOR) vs ESPN positional RANK —
>   the distinctive divergence surfaces precisely at the distance kicker and the
>   dual D/ST brackets. `divergence.py` stays frozen (D11); its scarcity/VBD
>   refinement is superseded by the VOR-point value view.
>
> **Done-when met (real 2026 data, end to end):** a scratch DB of 7,973 players +
> 54,674 weekly projections + 1,025 ESPN board rows drives `build_valuation` →
> ranked board. Spot-checks behave: pass-catching RBs (Bijan/Gibbs/CMC) + Chase
> lead the full-PPR board; **QB1 Josh Allen is correctly muted to overall #19**
> despite 338 proj pts (deep QB replacement — VBD scarcity working); MIN/LA/SEA
> D/ST rise on strong yards-allowed brackets; the distance-K edge (McPherson)
> surfaces. ESPN↔house skill join is **95.6%** (949/993) — the unmatched 44 are
> long-tail/rookies with no Sleeper projection, no systematic id-space mismatch
> (design §4 residual risk closed).
>
> **Verification (multi-agent, ultracode).** Recon (4 probes → adversarial verify
> → synthesis) locked the design; a parallel build (ESPN data side ‖ valuation
> core, disjoint files) then an integration pass; a 5-skeptic adversarial audit
> re-derived every claim against the real DB. **Leakage, scoring-aggregation, and
> VOR/flex dimensions came back CLEAN**; all 4 confirmed findings were in the
> *value view* only (the primary board was clean), all fixed here: (1)+(2) a
> draftable filter (`min_vor`, default 0.0) — without it the house board (every
> projected player, incl. ~1,200 WRs tied at the replacement floor with a
> meaningless tiebreak rank) is 3–4× deeper than ESPN's, so a raw `|delta|` sort
> floated undraftable practice-squad players to the top and **buried the actual
> K/D-ST edge under 573 noise rows**; the filter makes the boards comparable and
> the report now leads with real targets (Tee Higgins, MIN D/ST, pass-catching
> RBs, McPherson). (3) a cross-position join guard (a player Sleeper calls TE but
> ESPN tags RB is skipped, not cross-pool subtracted). (4) report-specific flags
> `HOUSE_HIGHER`/`ESPN_HIGHER`/`ALIGNED` (a house-vs-ESPN report must never ship
> the word "MARKET" to a novice — rule 6). Suite green: **243 passed** (from 212),
> `ziggurat smoke` + repo-boundary clean; the committed ESPN fixture is public
> player-rank data only (no manager/roster identity — rule 5 verified by hand).
>
> **Operator-question resolutions (all tunable):** week window 1..17; first-
> non-starter baseline; K/DST denoise on; ESPN editorial board primary + ADP
> secondary; `week=0` season sentinel moot (weekly-sum path).
>
> **Forward items / deferrals.** (1) `ESPN_LEAGUE_ID` is not in `.env` —
> `ziggurat valuation --espn` needs `--league-id` or that env var (add it, or
> fold into the 3.7 cadence). (2) `base.espn_by_gsis` is crosswalk-at-now (reads
> `players` at MAX(retrieved) with no as-of gate) — fine for draft use; a past-
> as_of backtest would get today's identity map. (3) Preseason weekly coverage
> non-uniformity distorts a naive week-sum (a 14-wk player looks worse than a
> 17-wk one for non-football reasons) — `weeks_counted` is surfaced per row; a
> normalize-by-weeks option is a 2.2/2.3 calibration knob if the tail matters.
> (4) Positional-scale (`ecr_type='rp'`) gate refinement inside `divergence.py`
> deferred (superseded by the value view — D11). (5) 2.2 (mock sim) and 2.3
> (pick engine) consume this board; the `RosterStructure`/baseline knobs exist
> for their calibration.

### 2.2 [Build] Mock draft simulator
**Goal:** Snake-draft sim with bot opponents drafting off ESPN default rank + noise (the room's actual behavior model), configurable to blend market ADP. This is both the strategy laboratory and the draft engine's test harness — build it *before* the engine it tests.
**Done when:** 1,000 mock drafts run headlessly from any slot and output roster + projected-points distributions per strategy.
**Checkpoint-1 amendment (2026-07-20):** the bot opponent model can be **calibrated on the room's ACTUAL past behavior** — pull prior-season draft results via the ESPN `leagueHistory` endpoint (1.1) and fit reach/ADP-adherence tendencies, rather than assuming pure ESPN-rank+noise. Keep ESPN-rank+noise as the fallback when history is thin. Draft is SNAKE @ 60s/pick (1.1); date still unset — the 60s clock is the Checkpoint-2 rehearsal target.
**Update:**
> **Done 2026-07-21.** Snake mock-draft simulator with a 2025-calibrated
> opponent model, entirely in the deletable `ziggurat/draft/` package (Rule 8):
> `priors.py` (frozen `RoomPriors`, fitted 2025 values with per-number artifact
> citations), `bots.py` (`Picker` seam — the 2.3 engine's plug-in point;
> `RankNoiseBot` ESPN-rank+Gaussian-reach backbone honoring roster legality,
> positional need, position-run nudges and a K/DST round-window; `AutodraftBot`;
> `FollowVor`/`FollowEspnRank` operator baselines), `simulator.py` (snake loop,
> post-draft legality/cap assertions, `run_many` distributions, `load_board` as
> the ONLY DB seam — explicit keyword `as_of`, leakage-tested), `calibration.py`
> (pure re-fit from the raw 2025 artifacts), and a thin `ziggurat mock-draft`
> CLI. Import-time Rule-8 boundary test included. Built via the established
> three verified workflows (recon → parallel build → 5-skeptic adversarial
> audit with independent refuters), all agents Opus/xhigh.
>
> **Recon reality vs the amendment (full detail in gitignored
> `intel/research/mocksim-2.2-recon.md`):** exactly ONE prior draft exists —
> the league was founded 2025 (2015–2024 return 404). 2 of its 10 seats
> autodrafted 100% of picks, leaving 8 human seats / 125 human picks (6 of
> those drafters return in 2026). So per-manager calibration is off the table
> and **ESPN-rank+noise is the primary model, not a fallback**, seeded with
> AGGREGATE room priors fit from 2025: `reach_sigma=17.78` (human skill-only
> reach spread vs the IDP-filtered draft-day db_fpecr board; editorial-board
> fit also shipped), `autodraft_fraction=0.2`, `kdst_earliest_round=9`,
> 16-round position-run curve, empirical board-adherence Pearson 0.907
> (recorded, held neutral in the noise model to avoid double-counting).
> Surprises: a real board-at-draft-time signal exists in TWO forms (2025 ESPN
> editorial board joins 160/160 picks; db_fpecr scraped on the literal draft
> day), but ESPN's own historical ADP is degenerate (flat 170.0) and the
> fpecr redraft-overall board is IDP-contaminated (must filter+re-rank). No
> per-pick timestamps exist; per-manager `position_lean` ships gated OFF.
>
> **Done-when met on real data:** `db/ziggurat.sqlite` was populated for real
> this item (migrations→schema 4; 7,973 players / 57,892 projection rows /
> 1,025 ESPN board rows @ 2026-07-21) and 1,000-draft headless runs work from
> every slot: 10 slots × both strategies × 1,000 = 20,000 drafts in 87s with
> per-strategy mean/p10/p50/p90 + roster-shape output. Follow-VOR beats
> follow-ESPN in ALL 10 slots by ~+128 mean (seed-stable, pairing-unbiased) —
> correctly framed as a HOUSE-PROJECTED-points gap (the grader shares VOR's
> projection currency; on a non-divergent synthetic board naive VOR loses,
> so board divergence sets the sign). Realized-points validation is Phase 4.
>
> **Audit:** leakage, mechanics, calibration math, standing rules, and
> statistical validity all held under attack (every fitted number reproduced
> independently; +128 stable across seeds; FLEX optimizer brute-force-verified).
> 7 findings survived refutation, ALL minor; 5 fixed same-day (load_board
> leakage test; up-front board-supply validation + loud post-draft
> legality/cap failure with cap-aware fallback; CLI `--slot` bounds-check;
> clean CLI empty-board error; if/try-aware Rule-8 scanner exempting
> `TYPE_CHECKING`), 2 recorded as interpretive (reach-sigma board
> commensurability — measured impact ~5%, a 2.3 tuning seam; the +128 framing
> caveat above). Suite green: **292 passed**; repo-boundary + Rule-5 scans
> clean (no colleague names/GUIDs/abbrevs in committed files).
>
> **Forward items:** (1) 2.1 deferral closed — `ESPN_LEAGUE_ID` now in local
> `.env`. (2) 3 of 10 2026 seats are still unclaimed and the draft is
> unscheduled — re-snapshot `mTeam`/`mMembers` near draft day to freeze the
> bot roster (fold into 3.1/Checkpoint 2). (3) Reach-reference
> commensurability + `board_adherence` are explicit 2.3 tuning levers (both
> fits in the gitignored `priors_fit_2025.json`). (4) `position_lean` stays
> OFF until a second season of history exists (2027).

### 2.3 [Build] Draft pick engine
**Goal:** Pick logic in the Fry–Ohlmann tradition (player value × board state × positional need), with survival probabilities keyed primarily on ESPN default rank, market/ESPN divergence exploitation, opponent-roster need modeling, and round-appropriate risk posture (floor early, ceiling late; bench picks as options). Validated by tournament runs in the 2.2 sim against naive strategies; distill the academic holdings into `intel/research/draft-strategy.md` as part of this item.
**Done when:** the engine beats ESPN-rank-following bots in sim by a stable margin across slots, and its recommendations come with legible reasons.
**Checkpoint-1 amendment (2026-07-20):** survival probabilities key primarily on ESPN default rank (1.1 confirms it drives the room); opponent-roster-need modeling can **seed from prior-season `leagueHistory` rosters** where available. The market/ESPN divergence the engine exploits is exactly the 1.5 divergence signal (via 2.1).
**Update:**
> **Done 2026-07-22.** Fry–Ohlmann pick engine in the deletable `ziggurat/draft/`
> package: `engine.py` (`PickEngine`, a `Picker`; additive one-ply score
> `vor + b_need·need_fill + b_vona·urgency + b_risk·risk_sign(round)·dispersion`,
> with `urgency = max(0,VONA)·(1−S_next)` — survival-timed scarcity) and
> `survival.py` (Monte-Carlo rollouts of the calibrated 2.2 room over a CLONED
> board state; analytic sigmoid fallback re-fit on the real board:
> center ≈ 2.67 + 0.704·rank, R²=0.985; live-recalibration utility that refits
> reach spread from an observed pick log, threshold-gated). `PickContext` gained
> one trailing defaulted `opponent_rosters` field (2.2 pickers untouched).
> `recommend(ctx, top)` returns `PickRec`s with novice-legible reasons — the 2.4
> TUI contract. Weights (b_need=25, b_vona=2.0, b_risk=5.0, balanced schedule)
> selected by tournament sweep; archetype schedules (zero/hero/robust-RB) ship
> as data, none dominates balanced on our board (the board already prices
> scarcity). Deterministic bit-for-bit (D2); ~0.2s/decision at R=512 vs the 60s
> clock. Literature distilled to gitignored `intel/research/draft-strategy.md`
> (H1–H12, verifier-corrected citations); locked design + audit addenda in
> `intel/research/pick-engine-2.3-design.md`.
>
> **Done-when met (real 2026 board, self-graded house points — Phase 4 grades
> realized):** 60/60 cells positive — 3 seeds × 10 slots × paired n=120 vs BOTH
> baselines, every 95% CI > 0. Margin vs FollowEspnRank +135…+197 (the plan's
> literal bar), vs FollowVor +22…+53 (the honest bar). Min-over-slots: +135.4 /
> +22.1. The K/DST divergence play EMERGES from urgency (engine takes the
> divergent DST/K at R9–10, room waits to R15) — no special-case rule.
>
> **Audit (5 skeptics + refuters, 18 agents).** The margin survived every
> statistical attack: bit-identical reproduction, fresh seeds 7/11 all CI>0,
> unbiased pairing confirmed, harness bit-identical to `run_many`, and the
> selection-overfit probe showed even non-winning weight configs beat both
> baselines at held-out seeds (margin is structural, not tuned). Candidate-set
> truncation PROVEN safe (2,000 adversarial boards, 0 argmax mismatches).
> Robustness: margin stays CI>0 under hostile rooms (all-autodraft, reach
> halved/doubled, adherence off). 13 findings, all minor, all fixed 2026-07-22
> except recorded doc-notes: R/kappa/priors seam added to `PickEngine` (the
> "R=512 live" budget + live-recalibration priors are now actually reachable);
> wait-gate docs corrected (phrasing-only, score uses continuous urgency);
> Rule-6 reason fixes (unranked "~9955 spots later" suppressed, final-pick
> wording, no take-now/no-rush contradiction — regression-tested); CLI engine
> arm default n=100 + time notice (was a silent ~10-min hang); recalibrate
> degenerate-sigma honesty; dead `next_overall_pick` removed; harness pairing
> docstring + t-vs-z CI multiplier corrected (no verdict changed).
>
> **Deferrals:** posture_check (operator addendum #2) → 2.4 with the hysteresis
> requirement; live-recalibration WIRING into the TUI loop → 2.4 (the priors
> seam now exists); per-player dispersion upgrade gated on `adp_rankings`
> population (4.1 or a live pull); realized-points validation → Phase 4.
> Suite green: **334 passed** (from 292).

### 2.4 [Build] Draft board TUI
**Goal:** Terminal draft-day interface: fuzzy/alias pick entry (RapidFuzz-style; 'cmc' resolves instantly), continuous background recompute between picks, tier view, ESPN-rank view (the room's screen), contingency prompts at snake turns. Manual entry is the primary path per SPEC; if 1.1 found any live-sync affordance, it's a bonus assist only.
**Done when:** a full mock draft can be driven through the TUI without touching documentation, and no single interaction takes more than ~5 seconds.
**Update:**
> **Done 2026-07-22.** Standard three-workflow pattern (recon → 4-builder+
> integrator build → 5-skeptic × 5-refuter audit) plus a 3-fixer+verifier fix
> round; all agents Opus/xhigh. New modules, all in deletable `ziggurat/draft/`
> (Rule 8): `resolver.py` (stdlib tiered fuzzy name resolver + alias/DST maps,
> confirm-on-tie), `session.py` (headless `DraftSession` controller: snake
> bookkeeping, append-only fsync-before-ack JSONL journal + resume-by-replay,
> fresh state-seeded ctx per compute, live-recalibration wiring with honesty
> fields, snake-turn contingencies, legality-aware autodraft suggestion),
> `posture.py` (archetype comparison + hysteresis/cooldown monitor — the two
> carried 2.3 deferrals are LANDED), `board_view.py` (pure Rich renderables:
> rec panel with verbatim `PickRec.reasons`, tier/VOR cliffs, ESPN room view,
> roster/needs, honesty status), `app.py` (the only I/O module; scroll-on-enter
> Rich REPL), thin `ziggurat draft-board` CLI (lazy in-body imports).
> `BoardEntry.team` added (defaulted trailing field). `rich>=13` declared as a
> direct dep (recon said optional group; main-deps is safer for the shipped CLI
> and stays a one-line delete — recorded deviation).
>
> **Recon decisions (note: `intel/research/tui-2.4-recon.md`):** measured
> recommend() ≤243 ms @ R=512 on the real 3,218-entry board (~20× under the
> 5 s bar) → SYNCHRONOUS recompute after every entered pick (the background-
> thread design was refuted as over-engineered; multiprocessing dropped —
> engine dataclasses don't pickle). Verifiers overturned two probe claims:
> recommend() is NOT idempotent on a held ctx (mutates `ctx.rng` — now a hard
> rule + honest regression test), and fuzzy accuracy was downgraded 97.8%→~90%
> adversarial (drove the elite-safety/empty-guard/err-toward-confirm MUSTs).
> Rich-REPL-vs-Textual and rapidfuzz both deferred to Checkpoint-2 rehearsal
> evidence (headless controller contains a flip). League has no keepers.
>
> **Done-when met:** `tests/test_draft_app.py` + `test_draft_session.py` drive
> scripted full drafts through the real loop (resolve→confirm→enter, undo,
> edit, autodraft seat, crash-kill-resume bit-identical, non-identity
> `pick_order`, ctx-reseed determinism, <5 s latency guard); real-board
> verifier drive: recommend() 157 ms max @ R=512, wheel contingencies 3
> legible branches, zero tracebacks, resume bit-identical.
>
> **Audit (10 agents, every finding refuter-reproduced): 1 critical, 5 major,
> ~10 minor, 8 notes — all fixed 2026-07-22 except recorded notes.** Critical:
> same-path relaunch without `--resume` truncated the journal (silent total
> pick loss on the likeliest panic action) → O_EXCL + `JournalExistsError`,
> timestamped journal names, `--resume` discovers newest journal and loads the
> board at the JOURNALLED as_of (also fixes midnight rollover). Majors: torn
> tail bricked resume (now final-line-tolerant + warnings); first-launch
> missing-dir crash; app autodraft was legality-blind raw-ESPN (rewired to
> `suggest_autodraft`, blind path deleted, refuses on operator's own turn);
> resolver elite floor 780→300 (typo sweep residual 957→265, ALL residuals
> visible-confirm; silent wrong-pick autos = 0 across every adversarial
> sweep); posture banner flash-then-permanent-latch (now held until p/x accept/
> dismiss, snake-turn cadence, guard before compute); ctx-reseed test provably
> couldn't fail (wheel short-circuit) — rewritten and bug-injection-verified.
> Held clean under attack: verbatim reasons, empty-query guard, snake geometry
> under non-identity orders, Rule-8 deletability, fsync ordering. Recorded
> notes: over-cap rival entry is by design; kappa asymmetry cancels in paired
> posture comparison; wheel survival 1.0 is literally correct; `jt` alias
> stays auto (buried rival #104 outranked by target #7 — no invariant hit).
>
> **Deferrals → Checkpoint 2:** 60-s-clock rehearsals; TUI-shape + rapidfuzz
> revisits on rehearsal evidence; alias-map growth from real misses; posture
> margin/consecutive tuning; re-measure on the draft-day machine; near draft
> day re-snapshot the room + fix operator slot/`pick_order` + refresh board.
> Suite green: **460 passed** (from 334; 126 new). Ruff clean.

### ✦ Checkpoint 2: Draft dress rehearsal (gate for draft day)
At least two full-speed rehearsals against the sim under a real 60-second clock — operator at the keyboard, tool recommending, picks entered by hand. Fix what breaks; rehearse again if the fixes were structural. Also: strategy selection from the actual draft slot once the league schedules the draft.
**Checkpoint notes:**
> _In progress._ **Board refreshed 2026-07-24** (projections + ESPN board at
> `retrieved_as_of` 2026-07-24; the pull tripped the espn_ranks drift tripwire on
> ONE sparse row — ESPN ships the odd fringe player with only an ELIMINATION
> block — guard moved to snapshot-level coverage, commit c5b800c).
>
> **Rehearsal 1 held 2026-07-24** (slot 5, sim rivals via `a`, no clock — a
> mechanics blitz: 160 picks in ~7 min, zero undos/edits, resolver clean). It
> exposed a REAL engine defect (operator follows rec #1 blindly, so Rule 6
> carries everything): the additive score used FULL VOR for lineup-unreachable
> picks → QB2 in R7 and QB3 in R13 of a 1-QB league. The 2.3 tournament could
> not see it: its starting-lineup metric scores ALL bench picks zero. **Fix:**
> lineup-reachability fraction on positive score components (QB .25, RB/WR .60,
> TE .50, K/DST 0; 1.0 while startable incl. open flex), plus a plain-language
> injury-insurance reason. Replay: R7 flips to the open WR2 starter, Burrow
> demoted with the reason displayed. Post-fix tournament (10 slots, n=40 eng /
> 300 base, R=64): all slots positive, worst +28.1 vs FollowVor / +146.9 vs
> FollowESPN (means; house-projected, self-graded as before). Adversarial audit:
> CLEAN (determinism/replay, legality, K/DST play, TUI contract all verified;
> 9 findings, none a defect). Known residual: final-round picks still run into
> the QB=3/TE=3 caps (bench-blind metric can't grade the tail; Phase 4 grades
> realized). Rehearsal 1 does NOT count toward the two-rehearsal gate (no
> clock + structural fix) — next: two timed rehearsals on the fixed engine,
> one with a mid-draft kill + `--resume`; strategy-from-slot still blocked on
> ESPN scheduling the draft (2 seats invite-pending as of 2026-07-21).
>
> **Rehearsal 2 attempt (ESPN mock lobby, 2026-07-24) → the 2.4-deferred
> TUI-shape decision resolved: the Rich REPL loses to BURST entry** (7 CPU
> picks landed in seconds; each entry cost a type→Enter→confirm round trip —
> and 2 of our 10 real seats autodrafted in 2025, so instant consecutive picks
> are a draft-day certainty, not a mock artifact). **Built the web cockpit**
> (`draft/webapp.py` + `webui.html`, thin `ziggurat draft-web` CLI): a
> 127.0.0.1-only stdlib HTTP view over the SAME headless DraftSession
> (journal/resume/engine untouched), per-keystroke autocomplete via new
> `resolver.suggest()` (same tier scorers as `resolve` — punctuation-blind, any
> name chunk), commits by explicit clicked/highlighted player_id, autodraft
> propose-then-confirm, posture accept/dismiss and recompute cadence mirroring
> `app.py`. REPL kept as fallback. Verified live: suggest("jamarr") →
> Ja'Marr Chase w/o the apostrophe; 4-pick flow + operator rec + kill/`--resume`
> replay on the real board. rapidfuzz stays unnecessary (structure, not kernel
> speed, was the gap). Suite 485.
>
> **Rehearsal 3 attempts (ESPN mock lobby, 2026-07-24): typed live-search still
> loses the burst** — the operator was typing+Enter blind to keep up, which
> defeats the visible-confirm safety. Root cause named: TRANSCRIPTION (read a
> name on one screen, reproduce it on another) costs 5-8 s/pick regardless of
> search quality. Fix: turn transcription into VERIFICATION — a numbered
> quick-pick strip (top-6 available by ESPN rank; rooms mostly draft off the
> top of their board) commits a rival pick with one keypress (1-6) or click,
> and on the operator's turn keys 1-3 / one-click buttons draft the engine's
> recommendations directly. Search remains the fallback for reaches; edit-mode
> always uses search. Playwright-verified live: 4 rival picks in 4 keypresses,
> operator pick in 1. Perspective for the gate: the CPU lobby (7 picks in
> seconds) is beyond the real worst case (2/10 autodraft seats → bursts of
> ~2-3); rehearsals in the mock lobby remain the stress test.
>
> **ESPN live-sync spike (2026-07-24) — NEGATIVE, architecture settled.**
> Tested with a throwaway 4-team league + live autodraft: ESPN's REST views
> (`mDraftDetail`/`mRoster`/`mTeam`) stay placeholder/empty for the ENTIRE
> live draft and flush atomically at completion (all 64 picks in one poll,
> 5 ms spread; the draft room's realtime feed is a private websocket — not
> pursued: fragile, unverifiable, bad draft-day dependency). So manual entry
> via the quick-pick strip IS the draft-day plan. Salvage: completed drafts
> are auto-importable from the flush (Phase 3 roster init / rehearsal
> grading), and a scratch test league is the ideal rehearsal venue — real
> ESPN draft room, real 60 s clock, cockpit alongside. Full findings:
> `intel/research/espn-live-draft-sync-spike.md`.
>
> **DEFERRED (operator decision, 2026-07-24).** The league's real draft is
> still unscheduled and the operator is not maintaining a test league, so the
> checkpoint's remaining items move to a PRE-DRAFT-DAY gate rather than
> blocking now: (1) two clean full-speed rehearsals (public mock lobbies;
> focus = operator flow management, input via the quick-pick strip),
> (2) strategy-from-slot the moment ESPN schedules the draft, (3) near draft
> day: re-snapshot the room, refresh the board, re-verify on the draft-day
> machine. Follow-up before those rehearsals: DOM-scrape sync — **VALIDATED live
> 2026-07-24** in a league-specific ESPN practice draft (real draft room, the
> real league's settings, vs autos, repeatable on demand from the mock-draft
> lobby): a MutationObserver on the Pick History panel captured 16 consecutive
> autopicks at ~1.5 s cadence, zero misses, correct player + drafting team.
> Constraint: rows render only while the Pick History tab is active, but
> re-activation re-renders ALL rows, so a dedupe-by-pick-number harvester
> back-fills anything missed — flipping tabs pauses sync, never loses it.
> Selector spec + protocol in `intel/research/espn-live-draft-sync-spike.md`
> (addendum).
>
> **DOM-sync BUILT 2026-07-24** (`draft/sync.py` + `/api/sync` + Tampermonkey
> `espn_sync.user.js` served at `/sync.user.js` with the per-install token +
> port baked in). Trust model (Rule 6): a synced pick auto-commits ONLY
> through one gate — field consistency + suffix/punctuation-blind NAME
> identity (or DST-by-team), same-name twins refuse, wrong/stale anchor ids
> refuse; anything else BLOCKS with a one-click "Find him" assist and manual
> quick-pick entry always live underneath. Protocol: per-run sync epoch (the
> userscript resends everything after a cockpit restart; the verify path
> dedupes — acceptance never needs to be durable), first-room-wins league
> binding (a practice tab can't contaminate the live session; empty league is
> an identity, not a bypass), expected-overall dual-writer guard on manual
> picks while sync is active, conflicts surfaced (never auto-edited) and
> cleared on operator edit. Audited by a 35-agent find→verify workflow (30
> raised, 29 confirmed incl. 2 critical wrong-commit classes + restart
> deadlock + cross-room contamination) then a re-audit of the fixes
> (FIX-MINOR; 3 residuals found and fixed: empty-league bypass — live-proven,
> anchor-drift blinding the id gate, same-name-twin commits). All fixed with
> regression tests. Suite 537.
>
> **End-to-end dress rehearsal RUN 2026-07-24 (practice draft, sync live):
> 142 picks recorded hands-free in ~9 min, zero undos/edits; engine roster
> shape excellent (K R9/DST R10 divergence play, QB2 deferred to R14).**
> Three defects found and fixed: (1) BOARD GAP — 45 ESPN-draftable players
> (deep rookie Ks etc.) missing from the projections board made their picks
> UNENTERABLE, damming sync at pick 143 of 160 → `load_board` now unions the
> full ESPN universe as zero-VOR entries (0 missing on the real board);
> (2) dual-writer UX — with sync live the operator's own-turn commit buttons
> created cockpit-vs-ESPN conflicts → sync-aware UI ("draft him in ESPN —
> recorded automatically", quick strip stands down, search stays as guarded
> fallback) + one-click "Use ESPN's pick" conflict repair (`/api/sync/fix`);
> (3) D/ST display names unsearchable in ESPN ("HOU D/ST" vs "Texans") →
> ESPN-search hints on recommendations. Suite 539. Rehearsal 1 of 2 counts
> once these fixes see one clean re-run.
>
> **Re-run 2026-07-24 — ALL 160 PICKS recorded end-to-end; COUNTS as
> rehearsal 1 of 2.** One finding: ESPN display-name diminutives ("Kenny
> Gainwell" vs the board's nflverse "Kenneth Gainwell") blocked the commit
> (correct refuse), but after the operator's correct manual entry the sync
> retry raised a PHANTOM conflict on a right pick, and "Use ESPN's pick"
> couldn't auto-fix an unresolvable nickname. Fixed both layers: the commit
> gate's name identity now accepts diminutive first names (curated pairs +
> a y/ie-stripped >=3-char prefix rule; surnames still exact; twins still
> block; pure nicknames like "Hollywood" still refuse), and the verify path
> accepts a held pick when it appears among the resolver's candidates for
> ESPN's name (verify catches WRONG PLAYERS, not name variants). Suite 545.
>
> **Rehearsal 2 of 2 — 2026-07-24, FLAWLESS (operator's words). All 160
> picks, zero interventions, zero conflicts, sync hands-free throughout.
> THE TWO-REHEARSAL GATE IS MET.** Checkpoint 2's remaining items are
> purely calendar-bound: (1) strategy-from-slot the moment ESPN schedules
> the real draft, (2) near draft day: board refresh + room re-snapshot +
> one confidence run on the draft-day machine if it differs. Draft-day
> stack (engine + cockpit + DOM-sync) is validated under full-length live
> conditions three times in one day. Engine + tooling outcomes of this checkpoint (reachability
> discount, web cockpit, quick-pick strip) are already landed and audited.
> **Phase 3 begins now** — its ~Sept 10 hard deadline binds regardless of
> draft scheduling.

---

## Phase 3: In-Season Operations

**Goal:** The full weekly operating loop, live before NFL Week 1. **Hard deadline: ~Sept 10.** Weeks 1–3 are the richest waiver season; this phase cannot slip into them.

### 3.1 [Build] League state sync & cadence
**Goal:** Scheduled sync of rosters (all 10 teams), standings, matchups, transactions, free agents into temporal tables; runs on the Strix Halo cron.
**Done when:** the database answers "who held player X in week N" and "current FA pool" correctly after a scheduled run with no manual step.
**Update:**
> **Built and tested 2026-07-24; two confirmations are calendar-bound (below).**
> **Landed:** permanent `ziggurat/league/` package — `source.py` (the ONE network
> seam: `fetch_league_state` / `fetch_player_pool` / `fetch_transactions` /
> `fetch_activity`, reusing 2.1's request layer via the now-public
> `espn_source.league_client`), `state.py` (pure mappers + ingest + as-of
> accessors + formatters), `sync.py` (orchestration + run log + status report);
> migration `005_league_state.sql` (`league_player_state`, `league_teams`,
> `league_matchups`, `league_transactions`, `league_sync_runs`;
> **`schema_version` 5**); `base.gsis_by_espn` crosswalk; `asof.nfl_season_of`;
> a thin `ziggurat league {sync,status,roster,free-agents,holdings}` CLI; and a
> systemd user timer + installer (`scripts/systemd/`,
> `scripts/install-league-sync.sh`). Suite green (604). Design + raw probe
> evidence: `intel/research/league-sync-3.1-design.md`, `data/recon-3.1/`.
>
> **THE RECON FINDING THAT SHAPED EVERYTHING (probed live, four independent
> doors, all shut): ESPN serves league state as a CURRENT SNAPSHOT ONLY — there
> is no historical league-state backfill of any kind.**
> `leagueHistory?seasonId=2025&view=mRoster&scoringPeriodId=N` **ignores the
> scoring period** (weeks 1/4/9/14/17 all return the identical 163-player
> end-of-season roster, Jaccard 1.000); past-season box scores carry an EMPTY
> `rosterForCurrentScoringPeriod`; `mTransactions2` has no `transactions` key
> (confirming the 2.2 negative); and the activity feed **404s** for a past
> season. Consequences, which are now permanent facts of this system:
> 1. **League history is perishable and accumulates only forward.** A day the
>    sync does not capture is gone for everyone, forever. The cadence is not a
>    convenience — it is the only mechanism by which league history exists.
> 2. **Silence cannot look like success**, hence `league_sync_runs` +
>    `ziggurat league status`, which reports the exact unrecoverable missing days.
> 3. **Snapshot diffing is the primary movement source**; the transaction/activity
>    feed is a best-effort precision layer (exact timestamps, waiver-vs-FCFS
>    provenance) that may never populate — nothing depends on it.
>
> **Key decisions:**
> - **`league_player_state` stores the WHOLE universe every snapshot day
>   (~1026 rows/day), not just rostered players.** This is the load-bearing call:
>   `select_as_of` returns the newest row per key ≤ as_of, so if only rostered
>   players were written, the last "team 4 holds X" row would stay newest forever
>   after X was dropped and `who_held` would answer wrong for the rest of the
>   season. Writing everyone makes a drop a positive fact (`on_team_id` NULL) —
>   which is simultaneously the free-agent pool. One table answers both halves of
>   the done-when. Cost ≈ 190k rows/season; trivial.
> - **ESPN ownership percentages (`percentOwned`/`percentStarted`/`percentChange`)
>   are captured on the same pull.** They are SPEC goal 3's own consensus proxy
>   ("roster-percentage spikes"), they are point-in-time only (no history
>   endpoint — Phase 4 has to buy the historical version from Sleeper), and they
>   arrive in the same HTTP response as the FA pool. Not capturing them would
>   destroy the live 2026 copy of the series 3.3/4.2 exist to beat.
> - **Two independent roster views are reconciled, not silently merged.**
>   `mRoster` (authoritative, carries lineup slot + acquisition) wins over the
>   pool's entry-level `onTeamId`; every disagreement is counted into the run log
>   — a nonzero count means ESPN's views are mid-flush (the failure mode
>   Checkpoint 2 hit during live drafts). A rostered player missing from the pool
>   response is still written, so a hiccup never reads as a phantom drop.
> - **Day grain, deliberately.** Last pull of a day replaces earlier ones
>   (2.1's delete-partition-then-insert, so a re-run is idempotent). Sub-day
>   knowledge time stays a `base.select_as_of`-wide change (1.4 forward item 2):
>   two same-day rows would BOTH match `MAX(retrieved_as_of)` and every accessor
>   would silently return duplicates. The genuinely intraday question — who
>   grabbed whom, exactly when — rides on `league_transactions`' real ESPN
>   timestamps instead.
> - **`league_transactions` is write-on-change**, because a claim is genuinely
>   mutable before processing (PENDING → EXECUTED/FAILED in ESPN's overnight
>   batch): first-seen-wins would freeze it, per-pull versioning would rewrite the
>   feed daily. It is also the ONE table stamped `knowable_as_of` = the event's
>   own date rather than the pull day.
> - **Failure containment:** an optional-part failure downgrades the run to
>   `partial` and keeps the snapshot; a snapshot failure is recorded AND raised so
>   the timer exits nonzero. Truncated pools and auth rejections fail loud (a
>   silently truncated pool would write false free-agent history).
> - **`nfl_season_of`** replaces `date.today().year` defaults: a January run —
>   mid-fantasy-playoffs — would otherwise silently sync the wrong season.
>
> **Validated on real data (live pull, 2026-07-24):** 10 teams with live
> `waiverRank` 1–10 and full `transactionCounter`s, 70 matchups (the whole
> regular season is knowable pre-season; unplayed weeks correctly read 0–0 with
> no backwards leakage), 1026-player universe, **all 1026 free agents (correct
> pre-draft)**, espn→gsis coverage 983/994 skill players (98.9%), as-of leakage
> check clean (as_of = pull day − 1 → 0 rows).
>
> **Calendar-bound remainder (not code):** (1) **"who held X in week N" is proven
> on a synthetic add→drop→re-add timeline** (the exact stale-holder case the
> whole-universe design prevents) — real-data confirmation needs rosters, i.e.
> the August draft; (2) the timer must be installed on the machine that will
> actually run it (this dev box is a Ryzen 7 7840U laptop, not the Strix Halo)
> and one unattended run observed. Both land before Checkpoint 3.
>
> **Plan-level consequence recorded (affects Phases 4 & 5):** because ESPN keeps
> no league history, our own league's 2025 in-season decisions can NEVER be
> replayed — Phase 4 backtests stay on the public panel (`db_fpecr` + Sleeper
> ownership) as Checkpoint 1 scoped, and Phase 5 opponent behavioural profiles
> can only be built from 2026-forward snapshots. The 2025 season yields exactly
> one usable artifact (the draft + final standings/rosters), already harvested by 2.2.
>
> **Adversarial audit (2026-07-24, 27 agents over two rounds): 24 findings, 12
> confirmed after skeptic verification, 9 refuted, all confirmed ones fixed.**
> Suite 604 → 624. The audit's central catch was that the item's own load-bearing
> guarantee had a hole in it:
> - **CRITICAL-in-effect (reproduced, then re-verified fixed): a degraded pull
>   destroyed the day it was supposed to refresh.** Ingest replaces a day by
>   deleting its partition and rewriting it, with no floor on the replacement. So
>   when ESPN answered 200 with an empty `players` array on the 11:15 run, the
>   complete 05:15 snapshot was DELETED and nothing written — and because the
>   newest surviving row for each player was then the *previous* day's, a player
>   dropped that morning silently reverted to his stale holder for the rest of the
>   season. The exact failure the whole-universe design exists to prevent,
>   reintroduced through the replace, with the run still logged `ok`. Same shape
>   for a collapsed `mRoster` view (every rostered player rewritten as a free
>   agent). Fixed with `SnapshotCollapse` floors (`_MIN_SNAPSHOT_FRACTION`, on
>   both universe size and rostered count) checked BEFORE any delete, plus
>   `--allow-shrink` for a confirmed real shrink. Refusing is always right here:
>   a refused day is retried three more times by the timer; a destroyed day is gone.
> - **MAJOR: a hung pull would have silently killed the cadence.** `espn_api`
>   passes no timeout to `requests`, and under `Type=oneshot` systemd defaults
>   `TimeoutStartSec` to *infinity* — one black-holed connection would hold the
>   service Active forever and every later trigger would be skipped, with nothing
>   reporting it. Fixed at both levels (`TimeoutStartSec=600`; a scoped socket
>   timeout at the seam, which also covers the cron fallback, now `timeout 600`).
> - **Back-stamping refused.** `--as-of <past day>` wrote *today's* ESPN state
>   under that date — fabricating history rather than recovering it, and erasing
>   the day from the gap report that exists to say history is missing. Now refused
>   unless `--allow-backfill`, and a forced one is marked so `league status` still
>   reports it as `BACK-STAMPED … NOT point-in-time`.
> - **Inverted priority corrected:** a collapsed espn→gsis crosswalk used to
>   *discard the whole snapshot*. `gsis_id` is derived and backfillable at any
>   time; the ESPN snapshot is perishable. Never trade an unrecoverable asset to
>   protect a recoverable one — it now writes the day and downgrades the run to
>   `partial`.
> - Also fixed: writes now validate their stamp like reads do (`'2026-9-8'` wrote
>   a day no accessor could ever see, since the gate compares dates lexically);
>   DELETE+insert wrapped in one transaction (`base.upsert` gained `commit=False`);
>   event/acquisition days derived in LOCAL time, not UTC (evening events were
>   stamped a day late, producing `knowable_as_of > retrieved_as_of`);
>   reconciliation counts disagreements in BOTH directions (the pool flushing a
>   drop before `mRoster` — the direction that matters most — was silently
>   swallowed); the four `league` READ commands now migrate (`store.open_db`)
>   instead of tracebacking on any pre-005 database, which is exactly the sequence
>   CLAUDE.md tells the operator to run; `last_run` orders by the monotonic
>   `run_id`, not a second-resolution timestamp.
> - **Refuted and deliberately NOT changed** (recorded so they are not re-litigated):
>   a stale holder for a player vanishing from both roster and pool (triggers
>   contradicted by the real payload); teams/matchups surviving a failed run
>   (those two tables are re-served by ESPN every pull — not perishable);
>   duplicate rows across the two transaction feeds and the same-day transaction
>   PK collapse (no reader, no wrong result — but the timer's rationale comment
>   overclaimed and was corrected to say only CROSS-day transitions survive); the
>   244/TRADE branch (its acquiring-team semantics match every other row; dead
>   `elif` removed and the docstring corrected).
>
> **Deferred:** ESPN `acquisitionBudget=100` semantics (the 1.1 open question)
> now resolve themselves from observed in-season `transactionCounter`s;
> matchup-period ↔ NFL-week 1:1 assumed, verify at Checkpoint 3; the
> transaction/activity mappers follow espn_api's parsers and remain
> **unverified against a non-empty feed** until real transactions exist.

### 3.1b [Build] NFL data refresh cadence
**Inserted 2026-07-24 by the 3.2 recon workflow** (numbered `3.1b` rather than
renumbering 3.2–3.7, which are cross-referenced from CLAUDE.md and throughout this plan).

**Why this exists.** Recon for 3.2 probed the live DB and found **14 empty tables** —
`schedules`, `weekly_stats`, `injuries`, `depth_charts`, `snap_counts`, `team_defense`,
`game_odds`, `game_weather`, `adp_rankings`, `ngs_*`. The 1.4/1.5 **ingesters exist and
are tested**; what does not exist is any way to *run* them on a schedule:

- **No CLI command exists for any NFL ingestion.** `ziggurat/cli/main.py` exposes `db`,
  `intel`, `mock-draft`, `draft-board`, `draft-web`, `league` — and nothing else. The
  `pull_*` functions are reachable only from tests and ad-hoc Python.
- **`pull_projections` is called from nowhere in production code**
  (`ziggurat/data/nfl/projections.py:256`). The only systemd unit runs `ziggurat league sync`.

The failure mode is silent and Rule-1-invisible: in November, 3.2 would price Week 10 off
a July projection snapshot with a perfectly valid `knowable_as_of`. Nothing complains —
the data is not leaked, merely stale. This sits **upstream of 3.2, 3.3, and 3.5**: the
candidate generator reads `weekly_stats`/`snap_counts` for usage deltas, and the streaming
ranker reads `game_odds`/`game_weather`. All three would be built against empty tables.

**Goal:** A `ziggurat ingest` CLI over the existing 1.4/1.5 `pull_*` functions, wired into
the existing systemd cadence with per-source frequencies (projections weekly; injuries and
depth charts daily in-season; `weekly_stats`/`snap_counts`/`team_defense` after games
complete; `schedules`/`players` seasonally), a run log and gap report mirroring
`league_sync_runs`, and a one-time population of the empty tables. Carries forward the 3.1
lesson explicitly: **a degraded or empty upstream pull must never destroy good data** —
floors checked before any delete, and network calls bounded by timeouts so a hung pull
cannot silently kill the cadence under `Type=oneshot`.

**Done when:** `weekly_stats`, `injuries`, `depth_charts`, and `schedules` are populated
for the current season; a scheduled run refreshes them with no manual step; and a status
command reports per-source staleness (last successful pull per source) so 3.2's staleness
banner has a real source to read.

**Done-when amendment (2026-07-24, build step).** `depth_charts` is **struck from the
done-when and the cadence, and recorded as BLOCKED** — see the "what the build found"
section below. It is a table + accessor rewrite, not a column remap, and 3.2 already
deferred its only consumer. `weekly_stats` and `injuries` were also broken against live
upstream and were fixed here. Note that `injuries` and `weekly_stats` **cannot** be
populated for 2026 at all before ~Sept 10 (upstream 404 / client-side season guard), so
the population half of the done-when is calendar-bound for those two; `schedules` is
populatable today.

**Update — mechanism built & tested 2026-07-24; population is the operator's step.**
> **The build found three of the fourteen 1.4/1.5 ingesters ALREADY BROKEN against live
> upstream data while the suite was green** (624 passed). Independently re-verified before
> touching anything: `pull_injuries` raised `ValueError: missing required columns
> ['date_modified']` (nflverse dropped the column from the 2025+ release — 2024 has it,
> 2025 does not); `pull_depth_charts` raised for every season (upstream replaced the weekly
> table with a **daily snapshot panel** keyed on a `dt` timestamp, no `season`, no `week`,
> IDP rows included — 554,215 × 12 for 2025); `pull_weekly_stats` raised
> `IntegrityError: NOT NULL constraint failed: weekly_stats.player_id` (22 all-zero
> placeholder rows in `stats_player_week_2025`). **The fixtures under `tests/fixtures/nfl/`
> are frozen 2023 frames, which is the only reason `require_columns` — designed to fail
> loud — never fired.** "The ingesters exist and are tested" was false in practice.
>
> **Landed:**
> - **`ziggurat/data/nfl/refresh.py`** — the `league/sync.py` analogue: a frozen
>   `SourceSpec` registry (15 sources) carrying group, phases, interval, `perishable`,
>   `needs_schedules`, `needs_credentials`, `replaces_partition` and `blocked`; `decide` /
>   `plan_ingest` (pure, no network — `--dry-run` and the real run share it so they cannot
>   disagree); `run_ingest`; the run log; and `source_freshness` / `format_status`.
> - **Migration `006_nfl_ingest_runs.sql`** (`schema_version` 6), one row per source per
>   run. Deliberately NOT `league_sync_runs`: `state.last_run` filters on season only with
>   no source column, so the first NFL row there would silently become "when did the league
>   last sync" — a test guards that.
> - **`ziggurat ingest {run,status,sources}`** with `--group / --source / --season /
>   --as-of / --dry-run / --allow-shrink / --allow-backfill`. Exits nonzero on a failed
>   source, zero on merely-skipped.
> - **`ziggurat/net.py`** — `bounded_socket()` + `HTTP_TIMEOUT`, lifted out of
>   `league/source.py` so all four ESPN/HTTP seams share one mechanism.
> - **Three systemd unit pairs + `scripts/install-nfl-ingest.sh`** (daily 07:20, weekly
>   Thu 08:20, gameday 16:20; `TimeoutStartSec=1800` on each; installer checks
>   `loginctl Linger` and warns loudly rather than only printing a hint, which is what the
>   3.1 installer did and it was evidently not acted on — Linger is still `no` on this box).
>
> **Data-destruction floor (the item's most important property).** `ingest_espn_ranks` is
> the ONLY delete-then-write path in `ziggurat/data/nfl/`, and it reproduced the item-3.1
> destroy-the-day bug exactly: probed live, a 20-player degraded response replaced a stored
> 1,026-player **same-day** board, and an empty response wiped it to zero — the
> editorial-coverage guard sat behind `if rows:` while the `DELETE` ran unconditionally.
> Worse than it looks, because the DELETE is scoped to (season, TODAY) and today's
> partition is the one `draft-board`/`draft-web`/`valuation --espn` read; after a wipe
> `get_espn_draft_ranks` silently falls back to the previous day's snapshot **per
> board_key**, so the cockpit still renders as a stale/mixed hybrid with nothing reporting
> the substitution. Fixed with `BoardCollapse` + `_check_board_size` **before** the delete
> (floor 0.75 of the last stored snapshot, measured against a yardstick that deliberately
> includes today's own earlier run), an unconditional empty-board refusal not covered by
> `--allow-shrink`, and the whole replacement moved into one `with conn:` transaction.
> Nine tests, headlined by
> `test_degraded_same_day_pull_is_refused_and_the_stored_board_survives`.
>
> **The other three defect classes, all measured on this codebase, all designed out:**
> (a) **unbounded network hangs** — `source.import_sleeper_projections`,
> `weather.fetch_open_meteo` and `espn_source.fetch_player_universe` had NO timeout;
> the first two now pass `timeout=`, the third is wrapped in `bounded_socket()`, with
> applied-and-restored tests; (b) **a failed source's partial rows riding the next
> source's commit** — measured leaving `weekly_stats` permanently holding week 1 only
> (1,070 of 19,421 rows) with valid stamps, run log saying `failed`, table reading fresh;
> `run_ingest` now rolls back before the next source touches the connection; (c) **"wrote
> 0 rows" logged as success** — six ingesters drop 100% of their rows when `schedules` is
> empty (19,421/19,421) and raise nothing, so dependencies are checked in code
> (`skipped`, never a happy zero) and `base.note_drops` now feeds a `collect_drops()`
> tally the run log records, making `0 written + N dropped` a `failed`.
>
> **What was deliberately NOT copied from 3.1: the missing-days gap report.** ESPN serves
> no league history, so "unrecoverable" is literally true there. Every nflverse source is a
> whole-season file re-downloaded in full, so a missed NFL run is staleness. Reusing that
> alarm would train the operator to ignore the one report where the words are true — a test
> asserts the NFL status output contains neither "unrecoverable" nor "missing days". Only
> the four genuinely perishable sources (`projections`, `adp_rankings`, `espn_ranks`,
> `game_weather` forecast) get loss language, and only once they have actually expired.
>
> **`depth_charts` — BLOCKED, recorded, not faked.** The new upstream shape is a dated
> daily panel; the stored table cannot hold it, and `base.select_as_of` cannot express
> "newest `dt` per key at `as_of`" (it resolves MAX(`retrieved_as_of`) per key, and one
> pull carries 126 `dt` days under a single retrieval stamp), so the accessor needs a new
> query shape too. The registry carries the full reason, `ingest status` prints it, and
> a test asserts every source is either pullable or explicitly blocked. **Follow-on item:**
> rewrite the table to store one dated snapshot per (season, team, gsis_id, pos_abb) —
> arguably ingesting only the LATEST `dt` per pull, mirroring `league_player_state` — plus
> the matching accessor. Strictly better than the old shape when done (a real publish
> timestamp as knowledge time, and no `schedules` dependency).
>
> **Cadence, pinned to MEASURED upstream publish times, not to a wish.** daily (07:20):
> `players`, `schedules`, `projections`, `adp_rankings`, `espn_ranks` (preseason),
> `game_odds` + `injuries` (in-season). weekly (Thu 08:20): `weekly_stats`, `snap_counts`,
> `team_defense`, `ngs_*` — Thursday because NFL stat corrections land Mon–Wed, and the
> whole-season file self-heals every earlier week. gameday (16:20): `game_weather`
> forecast, restricted to weeks inside a 10-day horizon (Open-Meteo 400s beyond ~16 days —
> measured +16d OK, +20d 400). Season **phase** and the projection week range are derived
> from the `schedules` table, never from the wall clock and never from
> `nflreadpy.get_current_season()`, which returns **2025** until 2026-09-10 and would have
> quietly refreshed last season all summer.
>
> **Recorded, not fixed (out of scope, no consumer harmed):** `game_odds` is invisible to
> any pre-kickoff reader (`knowable_as_of` = gameday), so 3.5 needs a pre-game regime like
> weather's forecast/archive split before the closing line is usable; `adp_rankings`
> collapses one duplicate FantasyPros row per scrape and derives `season` from
> `scrape_date[:4]` rather than `asof.nfl_season_of`; the Sleeper projections payload
> carries `injury_status` / `news_updated` / `game_id` that the mapper discards (a cheap
> future injury fast lane now that nflverse injuries are a post-season artifact); nothing
> yet re-captures the stale `tests/fixtures/nfl/*.parquet` or adds an opt-in
> network-marked contract test, which is the only thing that would have caught these three
> breakages the day upstream shipped them.
>
> **Not done (calendar-bound / operator step):** the tables are NOT populated — that is
> deliberately a separate operator-run step (`ziggurat ingest run --dry-run` first, then
> without it). `weekly_stats`, `snap_counts`, `injuries`, `team_defense` and `ngs_*` cannot
> be populated for 2026 before ~Sept 10 regardless (upstream 404s / nflreadpy's client-side
> season guard); `run_ingest` records those as `upstream_absent`, not `failed`, so seven
> weeks of expected absence does not desensitize the operator. The timers are written but
> NOT installed on this box. Suite green (690, up from 624).
>
> ---
>
> **AUDIT ROUND (four adversarial auditors, 2026-07-24). 28 findings; ALL judged real and
> fixed, none refuted outright (two recommendations were partially declined — recorded
> below). Suite green: 732 passed, up from 690.** The build's own summary above overstated
> two things, and both corrections are now in the code rather than only here.
>
> **The two headline corrections — both reproduced by the auditors on a COPY of the live
> DB, and both re-verified as fixed the same way:**
>
> 1. **"Every other NFL table is append-only, so it needs no floor" was half true, and the
>    wrong half was load-bearing.** Append-only protects against a pull that is MISSING
>    ROWS. It does not protect against a pull whose VALUES arrived empty, because
>    `select_as_of` resolves the newest row PER KEY: a same-key row with null ids is not
>    absent, it WINS. Measured: `ingest_players` with the id columns served empty (a
>    column-present/values-null upstream regression, which `require_columns` cannot see)
>    took every crosswalk to zero — `espn_by_gsis` 7,897 → 0, `gsis_by_pfr` 7,784 → 0,
>    `ids_by_fantasypros` 4,709 → 0, sleeper→gsis 6,149 → 0 — with the good rows still
>    physically present underneath, the run logged `ok`, and no repair command in the
>    codebase. Fixed with `players.CrosswalkCollapse`: per-column non-null **coverage rate**
>    (not count — a truncated pull is genuinely harmless and was verified so) against the
>    last stored snapshot, floor 0.75, checked BEFORE the write, `allow_shrink` override.
>    Re-verified on the live copy: 7,897 → refused → 7,897.
> 2. **`bounded_socket()` never bounded the seam it was written for.**
>    `socket.setdefaulttimeout()` applies only to sockets created without an explicit
>    timeout, and `requests` always passes one — with no `timeout=` argument it hands
>    urllib3 `Timeout(connect=None, read=None)`, and urllib3 calls `sock.settimeout(None)`,
>    discarding the default. Reproduced against a local accept-and-never-reply server:
>    `with net.bounded_socket(3): requests.get(blackhole)` was still blocked when killed at
>    40 s. So item 3.1's league-sync hang fix was equally ineffective, and only
>    `TimeoutStartSec` was doing any work. Fixed with `net.bounded_espn()`, which swaps the
>    `requests` module object inside `espn_api.requests.espn_requests` for a shim that
>    injects `timeout=` (an explicit timeout still wins) and restores it on exit; both ESPN
>    seams (`data/nfl/espn_source.py`, `league/source.py`) use it. Measured after the fix:
>    `ReadTimeout` in 3.0 s. The guarding test is now real — it points the seam at a
>    blackhole socket and asserts it raises inside N seconds, instead of asserting the value
>    of a global that nothing reads.
>
> **Everything else that was fixed, grouped by what it broke:**
> - **Fences that did not fence.** `run_ingest(today=None)` defaulted `today` to the stamp,
>   making `stamp != today` trivially false — the documented "back-stamping is refused by
>   default" path did not refuse (8 of the item's own tests never passed `today`). `today`
>   is now required, and the check is hoisted into `refresh.resolve_stamp`, which BOTH the
>   dry run and the real run call, so `--dry-run --as-of <past>` reports the refusal instead
>   of printing a clean plan the real command then died on with a raw traceback (now `error:
>   …`, exit 2). `valuation --espn --as-of <past day>` bypassed the orchestrator's copy
>   entirely and DESTROYED that day's stored board (auditor case E, reproduced live:
>   2026-07-21 partition 1025 → overwritten by the 07-24 board, which
>   `get_espn_draft_ranks(as_of='2026-07-21')` then served as that day's ranks). The refusal
>   moved into `pull_espn_ranks` itself, so every caller inherits it, and the command now
>   goes through `espn_ranks.ensure_board`, which READS a stored past board rather than
>   refusing outright (a future `as_of` still pulls — it deletes nothing and under-claims
>   knowledge, so it cannot leak).
> - **The floor measured the wrong things.** `_board_size` used `MAX(retrieved_as_of)` while
>   the DELETE targets `stamp`, so a 600-row write cleared a floor computed from a 500-row
>   CURRENT board and wiped a 2,051-row historical partition; the yardstick is now the
>   larger of the two partitions. And it compared `len(rows)` (pre-dedup) against a stored
>   post-dedup count, so a key-collapsing response would clear the floor, collapse the board
>   onto a handful of keys, and still log `rows_written=1026`; it now compares distinct
>   `board_key`s, re-counts the stored partition INSIDE the transaction (rolling back if it
>   fell), and returns the STORED count.
> - **Statuses that reported success.** A 99.7%-dropped pull (67 written / 19,354 dropped,
>   measured against the real nflverse file) was `partial`, which was excluded from the
>   failure list, the exit code AND the staleness verdict — it read `fresh` and `no
>   failures`. Now a drop RATIO above 20% is `failed`. `_is_upstream_absent` was a bare
>   substring scan (`"404"`, `"no such file"`, `"not found"`, …) over any exception from any
>   source, so a `FileNotFoundError` from an unwritable cache — and, sharpest, espn_ranks'
>   OWN drift guard, whose message reads "only 404/2051 mapped rows…" — were downgraded to
>   an expected absence and exited 0. Now classified on exception TYPE plus an anchored
>   pattern (`ValueError` matching `^season must be between`; HTTP status 404 from
>   `.response.status_code`/`.code`; `ConnectionError` matching nflreadpy's wrapped
>   `404 Client Error`), and an absence is REFUSED for a source that already succeeded this
>   season (a 404 after a success is a renamed release, not an absence). `PROBLEM_STATUSES`
>   + `refresh.run_failed()` are now one definition the CLI calls (rule 3) — the exit code
>   and `format_run`'s PROBLEMS line had already drifted apart over `empty`, so an empty pull
>   of a PERISHABLE source was reported to systemd as success and never retried.
> - **The staleness report lied in three ways.** `last_run` ignored the `season` column
>   entirely, so one `ingest run --season 2025` backfill made 2026 read `fresh 0d` against a
>   table with zero 2026 rows (reproduced end-to-end through the CLI), and after the March
>   season rollover the units' pinned `--season` would report last season's pulls as this
>   season's for months. It also had no upper bound, so `status --through <past day>` was
>   answered from FUTURE runs with a negative age that pinned the verdict at `fresh` forever.
>   Both are now in the WHERE clause (and in the migration's index), with an assert that the
>   age is never negative. And `format_status` rendered no failure channel at all: a source
>   that succeeded once and then failed every run printed a bare `fresh`, and a run
>   SIGTERM'd by `TimeoutStartSec` left an orphaned `running` row nothing ever reported. The
>   report now carries a `last try` column, `LAST ATTEMPT FAILED` / `RUN NEVER FINISHED`
>   blocks with the recorded error, and `start_run` reaps an older `running` row as
>   `abandoned`.
> - **The gameday timer never refreshed the games being played.** `weather_weeks` selected
>   on the week's FIRST kickoff, so a week left the request set the moment its Thursday game
>   started; on the Saturday and Sunday of a game week the unit fetched only NEXT week, and
>   forecast mode is perishable, so the freshest forecast a Sunday lineup call could ever
>   read was three days old. Verified against the real 2025 schedule (week 5 absent on
>   10-03/04/05). Now selects on the week's LAST gameday, matching `current_week`.
>   `game_weather` also gained the `preseason` phase so week 1's run-up is captured at all —
>   the phase flips only ON week 1's Thursday.
> - **`interval_days` was decorative.** Nothing read it but the report, so the weekly group
>   ran on a fixed `OnCalendar=Thu` and an nflverse outage outlasting the unit's three
>   restarts cost a whole in-season week of `weekly_stats`/`snap_counts` — while `ingest
>   status` still said `fresh` (age 7 ≤ interval 7). `decide()` now consults the run log:
>   a source whose last SUCCESSFUL pull is inside its interval records `fresh` and is
>   skipped, the weekly timer fires DAILY, and a failed Thursday retries Friday while a
>   successful Thursday keeps the anchor. `--force` overrides. `schedules` moved to a 1-day
>   interval (flex scheduling moves kickoffs ~12 days out and six sources stamp off it).
> - **systemd.** `After=network-online.target` is a NO-OP in a user unit (verified:
>   `LoadState=not-found`), and with `Persistent=true` the catch-up fires exactly when the
>   network is most likely down; replaced in all four units — including item 3.1's, which
>   carried the same line — with a bounded `ExecStartPre` name-resolution wait that never
>   fails the unit. `StartLimitIntervalSec` (1800) was not larger than `TimeoutStartSec`
>   (1800), so the restart limiter could never engage against a HANG (a start killed at
>   t=1800 and retried at t=2100 has aged out of the window); now 10800 (7200 for the league
>   unit). **The league-sync unit therefore needs `scripts/install-league-sync.sh` re-run on
>   any box where it is already installed.**
> - **Smaller, but real:** `--source` silently overrode `--group` (now refused); a mid-loop
>   `game_weather` failure logged `rows_written=0` after earlier weeks had already committed
>   (now a `PartialPull` carrying the real count); `valuation --espn` had no `--allow-shrink`
>   and would die in a traceback on a legitimate `BoardCollapse` (both fixed); the CLI read
>   registry attributes to decide about credentials (now `refresh.needs_credentials`).
> - **Tests that did not test what they were named for.** The run-log durability test
>   re-read through the SAME in-memory connection, where an uncommitted INSERT is fully
>   visible — it passed with `start_run`'s `conn.commit()` deleted; it now uses a file-backed
>   DB and a SECOND connection. The CLI staleness test asserted only that source names were
>   printed; it now asserts the verdicts, the last-ok date, the row count, the failure block
>   and the cross-season case. The registry "inventory" tests were set-comprehension
>   restatements of the constants; a behavioural test now asserts no module in
>   `ziggurat/data/nfl/` outside `espn_ranks.py` contains a `DELETE FROM`.
>
> **Partially declined (recorded so they are not re-litigated):**
> - *"Drop `current_week`; it is dead code."* Kept. It is item 3.2's seam (3.2's recon
>   explicitly recorded that no current-week source existed and that a guess must raise) and
>   it is tested. The real hazard the finding identified — that it and its three siblings
>   read `schedules` with NO as-of gate and take `today` rather than `as_of` — is addressed
>   where the next reader will meet it: an `OPERATIONAL READ — no as-of gate; never call
>   this from a decision path` line in each of the four docstrings, not only in a section
>   comment fifty lines above.
> - *"Mark `injuries` `blocked` like `depth_charts`."* Declined: whether the 2026 feed
>   resumes cannot be known before September, and recording a block we are not sure of is a
>   different kind of lie. The underlying complaint — that six sources would sit in a
>   `NEVER PULLED` alarm for eighteen weeks and train the operator to ignore the report — is
>   fixed generally instead: a source that has never succeeded and whose last attempt was
>   `upstream_absent` now reports the distinct verdict `awaiting`, listed under
>   `NOT PUBLISHED UPSTREAM YET`, not under `NEVER PULLED`.
>
> **Still unverified / deferred after this round (honest list):**
> - **The tables are still NOT populated** and the timers are still NOT installed on this
>   box (`Linger=no`). Both remain the operator's step.
> - **Nothing here has met real games.** The gameday-weather window, the in-season phase
>   transitions, the weekly interval anchor and the `upstream_absent`→`ok` transition around
>   ~Sept 10 are all tested against synthetic schedules and reasoning about upstream, not
>   against a played week. First real proof is Week 1.
> - **The stale fixtures are still stale.** `tests/fixtures/nfl/*.parquet` are frozen 2023
>   frames — the sole reason `require_columns` never fired on the three broken ingesters.
>   The opt-in network-marked contract test that would catch the next upstream break the day
>   it ships is still not written.
> - **`depth_charts` remains BLOCKED** (table + accessor rewrite), and `players`' new
>   coverage floor is calibrated against ONE observed snapshot — if DynastyProcess
>   legitimately drops an id column, the first run refuses and the operator needs
>   `--allow-shrink`.
> - The 20% drop ceiling and the 0.75 coverage/board floors are judgement calls checked
>   against measured normal drop rates (0.1%–3.6%), not against an observed bad day.
>
> **FIRST LIVE RUN — 2026-07-24, operator-run, and it found three defects the whole
> four-auditor round had missed.** All three are one failure mode: *a guard that fires on
> healthy data*. That is the exact way this system gets its reports ignored, which is the
> reasoning that (correctly) kept the league sync's gap report out of this module — so it
> matters more than the cosmetic look of it. DB backed up to `data/backups/` first; suite
> 624 → **739**; the population itself succeeded (`players` 7,732, `schedules` 272,
> `projections` 57,910, `adp_rankings` 4,699, `espn_ranks` 1,026).
> - **`adp_rankings` failed at 35% on a completely healthy pull.** Its "drops" were
>   1,692 IDP rows — which this league cannot start, so filtering them is CORRECT — plus
>   861 rows the ingester's own line comment described as *"kept (NULL gsis_id), not
>   dropped"* and then reported to `note_drops` anyway. Fixed: `note_drops(..., by_design=
>   True)` tallies a rules-driven filter separately and only unintentional drops reach the
>   ceiling; new `base.note_incomplete()` records kept-but-missing-a-field rows without
>   touching the ratio. Re-run on identical data: `ok`.
> - **The failure message printed two different denominators in one sentence.** It read
>   `dropped 2553/11090 (35%)` — but 2553/11090 is 23%; the ratio actually tested is
>   `dropped/(written+dropped)`. `tally['total']` is a SUM across `note_drops` calls, so for
>   any ingester reporting twice it exceeds the rows that ever existed. Now one denominator.
> - **`game_weather` reported a standing `LAST ATTEMPT FAILED` for a correct no-op.** Outside
>   the ~10-day forecast wall there is no week to fetch, and `weather_weeks`' own docstring
>   already called that *"a legitimate 'nothing to do' rather than a failure"* — but the run
>   path had no way to say so, returned `STATUS_EMPTY`, and `run_failed()` counts empty as a
>   problem (rightly, for a perishable source). It would have alarmed daily from July to
>   September. Fixed with a `SourceSpec.applicable` predicate evaluated inside `decide()` —
>   so `--dry-run` reports it too, per that function's stated purity contract — and
>   `source_freshness` consults the same predicate, so the verdict reads `n/a` rather than
>   `never`. This is the phase gate's own principle at one notch finer granularity.
>
> **Timers installed on this box 2026-07-24** (daily 07:20, weekly 08:20, gameday 16:20,
> deliberately non-overlapping on the DB), and `install-league-sync.sh` re-run to pick up
> the corrected `ExecStartPre`/`StartLimitIntervalSec`. All four fire. **`Linger` is still
> OFF** — until `loginctl enable-linger` is run, every timer dies at logout, and a missed
> daily run costs a `projections`/`adp_rankings` snapshot that cannot be re-pulled.
> Post-run status: 5 fresh, 9 `n/a`, 1 `blocked` — exactly one alarm, and it is a true one.

### 3.2 [Build] Marginal valuation
**Goal:** Roster-context value per SPEC: starting-lineup improvement over remaining season, positional depth, bye coverage, playoff-week schedules, and **conditional-distribution bench valuation** (handcuff contingent value; no median-only drops of lottery tickets). Drop candidates ranked by marginal value.
**Done when:** for a synthetic roster, add/drop recommendations visibly change as roster context changes, with reasons.
**Recon complete 2026-07-24** (11-agent workflow; full note in gitignored
`intel/research/marginal-valuation-3.2-design.md`). Findings that reshape the item:
- **Weekly projections are a flat season rate, not week-specific forecasts.** Measured
  median week-to-week CV excluding byes: WR 0.98%, TE 1.06%, QB 1.10%, RB 1.57% — versus
  **DEF 6.6%** (12.0% on projected points). Skill-position projections carry no opponent
  signal; the only real movement is the bye-week zero. **Consequence:** an uncapped
  "best available free agent" baseline makes a *second defense* the top add on nearly any
  roster (15 of 16 best-replacements returned `LA D/ST`). Requires position caps and a
  written-down static-roster assumption (the objective silently assumed no streaming).
- Availability weights must be a normalized distribution (the naive form gave `w0 = −0.290`
  on a 17-man roster); the objective produces exact ties that need a stated tiebreak ladder;
  and **no "current week" source exists in the DB** (`scoring_period = 0` on every row), so
  `weeks=None` must raise rather than guess.
- Greedy lineup solve is optimal here (**0 mismatches in 300** random rosters); a capped
  swap scan runs in **0.61 s**, Monte Carlo as the estimator would be ~11 min (rejected as
  estimator, kept as test oracle). Bye derivation gives 32 teams over weeks 1–17 but **16**
  over weeks 10–17 — an `assert == 32` would crash.
- Availability rates and handcuff uplifts (**WR uplift is −0.14 — no WR handcuff effect
  exists**) were calibrated against nflverse history pulled over the network, not from this
  DB; they ship as **labeled hypotheses** with source and `n` in every reason string. Note
  that item 5.2's promotion ladder is still a placeholder, so nothing yet stops a labeled
  hypothesis from hardening into a "rule" by default.
- **Scope decisions (operator, 2026-07-24):** do **not** rewire `draft/simulator.py` to
  import a new `core/lineup.py` — that package passed a two-rehearsal gate and runs live in
  ~3 weeks with no rollback window; write the seater fresh and let the duplication delete
  itself with the package (Rule 8). Depth-chart ingester deferred (v1 handcuff link rides on
  projection ordering). `format_swaps` / `--swaps` CLI move to 3.4, but the swap **matrix**
  stays in 3.2 (same scan as the drop board; splitting the computation is how the add and
  drop boards start disagreeing).
**Update:**
> **Built & tested 2026-07-24** (design implemented as written; suite 739 → **794**).
> Two new permanent modules plus one thin CLI command:
> - **`ziggurat/core/lineup.py`** — the per-week starting-lineup seater, written FRESH
>   (Rule 8 / the operator's scope decision; `ziggurat/draft/` is untouched, and its two
>   ancestors are named in the module docstring). The structural difference that makes it
>   in-season rather than draft-time: **points arrive as a parameter**, not off a
>   `house_points` attribute, so bye coverage and availability are COMPUTED per week rather
>   than approximated by a constant. `draft/engine.py`'s `_BENCH_VALUE_FRACTION` and
>   `_startable_now` are deliberately NOT ported (their K/DST entries are 0.0, which would
>   price every streaming move at exactly zero marginal value while 3.5 builds a streaming
>   ranker — two modules, contradictory advice, no error anywhere). The brute-force optimum
>   ships as a TEST ORACLE (`tests/test_lineup.py`), re-running the recon's greedy-vs-exhaustive
>   comparison on 150 random rosters every suite run, and greedy REFUSES a multi-flex or
>   superflex structure rather than silently returning a suboptimal total.
> - **`ziggurat/core/marginal.py`** — the objective exactly as designed: `V(K) = Σ_w E_S[
>   lineup(K,w,S)]`, `marginal(p|R) = V(R) − max_{f∈F∪{∅}, caps} V((R\{p})∪{f})`, with
>   normalized independent-Bernoulli one-out weights, `POSITION_CAPS` (DST/K hard 1),
>   `STREAMED_POSITIONS` on a current-week horizon, the QB/RB/TE-only handcuff coupling,
>   structural bye coverage, a separately-reported weeks-15-17 subtotal, the tiebreak ladder,
>   and raise-don't-guess week resolution. Both assumptions (A1 static roster, A2
>   projections-are-conditional-on-playing) are written into the module docstring and A1 is
>   printed above every board.
> - **`ziggurat/core/valuation.py` extended** — `weekly_lines()` / `weekly_points()` (the ONE
>   identity spine, so the season board and the marginal board cannot drift apart),
>   `canon_position` promoted public, `RosterStructure` gains `bench_slots=7` / `ir_slots=1`
>   + `active_slots`. `build_valuation` now consumes the shared spine; verified
>   behaviour-preserving on the live DB (3,219 groups, season total 50203.9682 identical) and
>   by its existing 12 tests. **`ziggurat/league/state.py`** gains `resolve_own_team()` so no
>   module hard-codes the operator's team id.
> - **`ziggurat marginal`** (thin, Rule 3) — `--as-of --season --team --from-week --last-week
>   --top --reasons --pool-limit --source --path`. No `--swaps` (3.4's).
>
> **Verified by running, not inferred:** on a scratch copy of the live DB with a simulated
> post-draft league (10 × 16, one IR occupant), the full CLI path runs in **7.4 s** wall
> clock (budget 30 s) over weeks 4-17 with 32 teams' byes mapped; the drop board reads −1.6
> to +small with the D/ST correctly the most droppable row and NO D/ST stacking anywhere.
> Week resolution raises today (`scoring_period` is 0 on every row and `schedules` is not
> knowable until Aug 1) and resolves correctly from `schedules` at a September `as_of`; the
> staleness banner fires (58 days) on a July snapshot pricing a September decision.
>
> **Two defects the build found and fixed that the design did not anticipate:**
> 1. **Greedy seated NEGATIVE-projection players.** An empty slot scores 0, so a
>    below-zero D/ST bracket is worth benching — greedy-take-the-best is only optimal
>    against the exhaustive optimum once that case is handled. Caught by the oracle sweep,
>    which is precisely why the oracle is a test and not a comment.
> 2. **A permanently empty starting slot dominated every row** (measured on the live sim:
>    with no D/ST rostered, the best add for all 15 drop candidates was a D/ST, at +76 each
>    — arithmetically right, and unreadable). Now stated once, in words, as a board note.
>    Also fixed: a streamed row read "starts in 13 of your 1 remaining weeks" because the
>    starts profile came from the full-window model.
>
> **Deviations from the design, with reasons:**
> - `fill_lineup` takes an explicit `positions` mapping; §7.3's signature could not seat
>   without it.
> - `weekly_lines()` (a richer sibling) carries the per-week points AND the identity/driver
>   totals, so `build_valuation` and `marginal` share ONE pass over 57k rows; the design's
>   stated `weekly_points()` ships as a thin projection of it.
> - `build_board()` is the single public entry point that returns rows + swaps + byes +
>   model + banner together; `build_marginal` / `build_swaps` are the design's stated
>   wrappers over it (calling them separately would scan twice).
> - `pool_limit` (default 30/position, plus the best few of every bye week) — NOT in the
>   design, needed because the pre-draft pool is the whole 1,026-player universe. Within a
>   position a higher projection dominates a lower one whenever they share a bye, so the
>   bye-week carve-out is what keeps the pruning safe; `--pool-limit 0` disables it.
> - A minimal `format_swaps` is kept as a debug renderer (12 lines, docstring says 3.4 owns
>   real swap presentation). The design cut it; the reason it cut it was scope, and the risk
>   it named was the *computation* splitting, which has not happened.
> - Availability bucket multipliers are a SCALAR per bucket (early 0.70 / mid 1.00 / playoff
>   1.45) derived by ratio transfer within one measured cohort, rather than per-position
>   playoff cells — mixing probe-2 and probe-3 cohorts in one table would have quoted a
>   precision neither supports. Derivation is written next to the constant.
> - Handcuff coupling fires only when BOTH the starter and his backup are on the roster
>   being valued (the scenario set is your own roster, per §1.3's weight arithmetic). A
>   rostered backup whose starter is on another team therefore gets no contingent credit —
>   said out loud in that row's reasons rather than silently priced. **Deferred: extending
>   the scenario set to linked starters on other rosters** (it is real lottery-ticket value;
>   it also changes the weights and is unmeasured).
>
> **Plan amendments recorded here (Rule 7):**
> - **Playoff-week MATCHUP STRENGTH for skill players is deferred out of Phase 3's valuation
>   layer** to 3.3/3.5-after-ingestion or Phase 4. Measured median playoff tilt +0.00%
>   (p05 −1.00%, p95 +0.93%, max |tilt| 1.4%), against skill-position week-to-week CV of
>   0.84–1.57% — the signal is indistinguishable from rounding, and a confident,
>   novice-facing "favourable weeks 15-17 schedule" sentence built on rounding error is the
>   single most dangerous thing this item could have shipped. The weeks-15-17 SUBTOTAL is
>   reported; `playoff_weight=1.0` is the seam for real playoff odds from 5.1.
> - **Pre-deletion checklist for `ziggurat/draft/` (do this BEFORE the package is deleted
>   after draft day):** port `draft/resolver.py` (620 lines of fuzzy name resolution,
>   curated alias + diminutive maps, measured zero silent wrong autos) into the permanent
>   tree. 3.2 does not need it — it joins on `gsis_id`/`espn_player_id` — but 3.3 and 3.6
>   will, its only non-draft dependency is `base.TEAM_ALIASES`, and the curated maps cannot
>   be re-derived. `tests/test_draft_boundary.py` now also fails a LAZY import of
>   `ziggurat.draft` from `core/`, `league/`, `data/` or `llm/` (the CLI keeps its exemption),
>   which the import-time scanner allowed.
> - The `depth_charts` ingester rewrite (v1 handcuff link rides on projection ordering) and
>   `weekly_stats`-fitted availability rates stay deferred; both are recorded in the design
>   note's §11.1 with their landing items.
>
> **What is NOT validated:** every availability rate and handcuff uplift is a LABELED
> HYPOTHESIS fitted on nflverse 2021-2025 and quoted with its source in the reason
> strings — none is fitted to 2026, and item 5.2's promotion ladder is still a placeholder,
> so nothing but `test_every_quoted_prior_carries_its_hypothesis_label` stops one hardening
> into a rule. The done-when is met on a synthetic roster; the real roster arrives with the
> August draft and needs no code change (only `on_team_id` / `lineup_slot` /
> `acquisition_*` differ). Design + the full "refuted / deliberately not changed" table:
> gitignored `intel/research/marginal-valuation-3.2-design.md`.
>
> ---
>
> **AUDIT & FIX ROUND — 2026-07-24 (four adversarial auditors, 30 findings).** Suite
> 794 → **832**. Everything below is a defect the build shipped, not a design change;
> each fix carries a test that failed before it. The suite now runs in **2m31s**
> against 72 s at 796 tests — the depth-3 reporting pass is ~10x depth 1, and
> `tests/test_marginal.py` alone accounts for 83 s of it.
>
> **1 CRITICAL — a partly-projected player priced as near-worthless and topped the
> board.** The unpriceable gate tested the POINT SUM, and the feed's bye row is
> byte-identical to its "no forecast" row (team set, `opponent` NULL, every stat NULL).
> Measured on the live feed: **A.J. Brown, 99.31% owned, carries a real week-1 line and
> sixteen empty weeks**; his sum was 14.13, not 0.0, so he cleared the gate, priced at
> −24.4 over 17 weeks and read `never reaches your starting lineup ... drop him and add
> Jordan Love and you would GAIN 24.4`, with nothing anywhere disclosing the gap. The gate
> was also a knife edge — narrow the window past his one good week and he WAS flagged, so
> the behaviour flipped on the window. Fix: `WeeklyLine` now carries `played_weeks` (weeks
> whose row carried an OPPONENT — the only signal that separates "no forecast" from "bye"),
> and coverage against `COVERAGE_FLOOR = 0.75` decides priceability. Census over the live
> universe, weeks 1-17: **525 identities cover 16 of 16 playable weeks, 4 cover 15, exactly
> 2 cover ONE** — the floor sits clear of both clusters. Verified on the live DB: he now
> lands in `CANNOT VALUE — only 1 of 16 weeks forecast` at BOTH windows.
>
> **6 MAJOR, all fixed:**
> - **The truncation error was bounded on the wrong quantity.** The design measured
>   one-out truncation at +1.4% on the LEVEL `V(K)` and the suite asserted `rel=0.03` on
>   `V(K)` — but the shipped number is a DIFFERENCE whose bench-depth content lives almost
>   entirely in the ≥2-out mass the truncation discards. Reproduced on a live post-draft
>   roster over weeks 8-17 against an exact 2^15 enumerator: `V(R)` +1.9%, but a deep RB
>   priced **−1.28 truncated against −3.25 exact**. Fix: **two estimators, deliberately.**
>   The SEARCH (roster × pool, ~2,900 `V()` calls) stays at one-out — that cost argument is
>   the design's and it stands. Everything the board REPORTS (16 rows, the decomposition,
>   and the retained swap matrix) is re-priced at `REPORT_DEPTH = 3`: measured −1.28 → −2.97
>   against −3.25 exact, `V(R)` bias 1.9% → 0.37%. It must be a DETERMINISTIC estimator:
>   common-random-number Monte Carlo is unbiased and costs the same, but sampling noise
>   **destroys the exact-tie band the §1.5 ladder exists to resolve** (caught by the tie test
>   going red). `value_monte_carlo` was rewritten with common random numbers and kept as the
>   oracle. New test bounds the REPORTED marginal against it **in points**, per row, and
>   asserts the search estimator alone would not clear that bar.
> - **The decomposition was a probability-mass split wearing a mechanism's label.**
>   `lineup_component` was `w0 × base` and `contingent_component` absorbed the rest, so
>   ~55-60% of EVERY row landed in the column the reason calls "weeks somebody on your
>   roster is hurt" — including for players with no linked backup, and including a D/ST,
>   which the model states can never be unavailable. Measured on the live board: Chris Olave
>   reported `lineup +5.89` against a true all-healthy delta of **+16.37**, and Dak Prescott
>   reported `lineup −6.42` against **+20.19**. Fix: `lineup_component` is now the real
>   all-healthy lineup delta and `contingent_component` is the residual — the value that
>   exists ONLY because somebody might be hurt. Pinned by meaning, not by sum: a zero-rate
>   availability model must drive every `contingent_component` to 0.0, a D/ST-for-D/ST swap
>   must be 0.0, and `lineup_component` must equal a hand-computed `fill_lineup` delta. (The
>   old `test_the_decomposition_sums_to_the_number_it_explains` asserted `a+b+c == a+b+c`
>   and could never fail.)
> - **A season-ending injury was priced as ONE missed week.** `injury_status` was applied
>   only at `current_week`; every later week fell back to the position base rate, so a player
>   ESPN reports `INJURY_RESERVE` was modelled ~91% likely to play in each remaining week and
>   **no reason mentioned his designation at all**. Measured: ACTIVE +59.11 / OUT +52.21 /
>   INJURY_RESERVE +52.21, indistinguishable, one week each. Fix: `absence_curve` propagates
>   the design's own §5.5 measurement forward (P(back) 28.8% at W+1, 46.8% W+2, 54.5% W+3,
>   plateau ~62%, ~38% never return), floored at the base rate; same roster now reads
>   ACTIVE +59.11 / OUT +30.69. A reason naming the designation and the assumption is
>   MANDATORY, and `INJURY_RESERVE`/`SUSPENSION` carry a second line saying the curve was
>   fitted on Out weeks and does not really cover them.
> - **The static-roster caveat printed above every board stated the bias BACKWARDS.** The
>   note said A1 "understates any slot you would simply stream" while the module docstring
>   and design §1.1 both say **over-valued**. It is the one operator-facing sentence that
>   exists to protect a novice from the A1 artifact, and it pointed at exactly the adds the
>   board is already too optimistic about. Now: "OVER-VALUES ... treat a positive number on a
>   bench body as an UPPER BOUND", with "a backup quarterback" added to the examples (the
>   case that dominates a one-QB roster), and the direction word asserted.
> - **Every availability reason quoted a sample size lifted from a different study.**
>   `n_by_position = {QB 101, RB 103, WR 36, TE 116}` is verbatim the HANDCUFF event study's
>   pair counts (design §5.1); the probe-3 availability table has no `n` at all. A wrong `n`
>   is worse than none — it looks checkable and is not, and "WR miss rate, n=36" is absurd on
>   its face. Deleted; the rate now ships its COHORT description instead, with a test that
>   the string carries no `n=` and that the handcuff model keeps its own (real) one.
> - **The staleness banner warned off the NEWEST pull, so one refreshed row silenced it.**
>   `ingest_projections` ends in an upsert — it never replaces the partition — so a player
>   who falls out of the feed keeps his last-known rows forever and `select_as_of` keeps
>   serving them. Reproduced: re-stamp every projection to November except one rostered WR
>   and the 109-day warning disappears. Fix: the warning gap is computed from the OLDEST
>   vintage on the board, and any entry whose own vintage lags the board's newest by more
>   than `STALE_BANNER_DAYS` gets a mandatory `STALE:` reason naming the date.
> - **In-season week resolution returned the week that had already finished, on the two
>   waiver days the cadence is built around.** Step 3 picked `max(week whose FIRST gameday
>   <= as_of)`. Real 2026 boundaries: week 1 is 09-09..09-14, week 2 starts 09-17 — so
>   Tuesday 09-15 and Wednesday 09-16 both resolved to **weeks 1-17**, pricing a played week
>   into every board and handing D/ST and K (current-week horizon, and the ONE position with
>   real week-to-week variation) last week's matchup. Fix: resolve to the first week whose
>   LAST gameday is on or after `as_of`; pinned at the Tuesday/Wednesday boundary.
>
> **11 MINOR / 2 NOTE, all fixed:** the tie reason named a rung the neighbours did not
> differ on (the real break was alphabetical — now the ladder only claims a rung somebody
> differs on, and an all-square tie says "alphabetical and means nothing"); the availability
> prior quoted only the FIRST week's bucket (now the range actually used, `6-13%/wk (6% in
> weeks 3-6, 9% in 7-14, 13% in 15-17)`); the drop board sorted a ONE-WEEK D/ST number
> against a fourteen-week one under "lowest is most droppable" (streamed slots now print in
> their own labelled block, plus a `per wk` column); one unpriceable roster body silently
> suppressed the structural-hole note (`fill_lineup` seats a 0-point player into an empty
> slot — the check now runs over the priceable roster only); unpriceable roster players
> could never be the DROP side of a swap, so 3.4 would have planned around the obviously
> correct drop (they now emit swap rows flagged `drop_unpriceable`, with the gain labelled an
> upper bound); the handcuff "X is NOT on your roster" disclaimer was gated on pool
> membership rather than roster membership, so it vanished in exactly the case where the
> starter has just been dropped by another manager; `never reaches your starting lineup — you
> have 0 better DSTs ahead of him` blamed competition when the cause was a bye; 507 of 866
> free agents were dropped from the add board with no disclosure while the pruning note read
> "168 of 359" (now counted and named, most-owned first); `bye_component` silently absorbed
> the REPLACEMENT's bye (now named in its own reason); `--last-week` was a silent no-op
> without `--from-week` (threaded through `build_board` into `resolve_weeks`);
> `--pool-limit` defaulted to `None`, making `DEFAULT_POOL_LIMIT` dead code and `0` a no-op
> (now defaults to 30, `0` means the whole pool); `scenario_weights` silently returned
> "everybody healthy" if handed any `p >= 1.0`, discarding every other player's scenario;
> `playoff_subtotal` accumulated unweighted while the total it is a share of was weighted,
> so the sentence "weeks 15-17 account for +X" would have gone false the moment the
> advertised `playoff_weight` seam was used.
>
> **Test-coverage findings (mutation-tested by the auditors) — all closed.** Deleting the
> tiebreak ladder, throwing away the entire free-agent pool, deleting `_prune_pool`'s
> bye-week carve-out, inverting the swap sort, hard-zeroing `playoff_subtotal`, ignoring
> `playoff_weight`, relabelling the decomposition columns, dropping `TEAM_ALIASES` from the
> roster seam, and reporting the oldest pull instead of the newest all left the suite GREEN.
> Added: order-independent tie ordering; a direct `_prune_pool` test plus a
> pruned-vs-whole-pool board equality; swap ordering and `swap_limit` truncation; the
> playoff subtotal and a `playoff_weight=2.0` run that must NOT move a no-playoff-weeks
> window; the Rule-2 DST/K coupling guard asserted **through a board** with a hostile
> `HandcuffModel` (the old test restated a one-line method); the `schedules` bye path, which
> becomes production from 2026-08-01 and had **zero** coverage, plus the design's D3
> cross-check (both sources, 32 teams, identical) and the `>1 missing week` branch; an
> LAR/LA D/ST join; a two-vintage banner; `base.latest_truth(build_marginal)`; roster-context
> cases 14(c) and 14(d); and Rule-1 leakage tests for the three accessors that shipped
> without one — `resolve_own_team`, `weekly_lines`, `weekly_points`. The performance test's
> 30 s assert on a fixture ~100x smaller than the real board was scaled to 5 s (the real
> number is measured by hand: **10.8 s** over weeks 8-17 and **16.9 s** over 1-17 on a
> live-DB post-draft simulation, against the 30 s budget).
>
> **One design change the fix round made on its own: the swap matrix is now LAZY.**
> Re-pricing every (add, drop) pair at depth 3 costs more than the whole rest of the
> scan, and `ziggurat marginal` never prints it — the matrix is 3.4's input.
> `MarginalBoard.swaps` therefore resolves on first access, from the same scan state
> at the same depth, so the two boards still cannot drift onto different estimators.
> Measured on the live-DB post-draft simulation: `ziggurat marginal` **4.3 s** over
> weeks 8-17 and **6.4 s** over 1-17 (was 10.8 / 16.9 with the matrix eager), and
> `build_swaps` — which does materialise it — 10.1 s for 112 swaps. Budget 30 s.
>
> **`bye_map` hardened while fixing the coverage finding:** `schedules` was preferred
> whenever it returned ANY row, so a half-ingested table left every team "unknown" while a
> complete, correct projections-derived map sat unused in the same database. It is now
> preferred only when COMPLETE; otherwise both are derived and the one resolving more teams
> wins, with a loud note. On a healthy full-span table the second derivation never runs.
>
> **Refuted and deliberately NOT changed** (recorded so they are not re-litigated):
> - **"the truncation reorders the top of the drop board"** — the specific reorder the
>   auditor showed was an `Eagles D/ST` (ONE-week horizon) against a WR (fourteen-week
>   horizon), i.e. the incommensurable-units finding, not truncation. Re-run under exact
>   enumeration on a live post-draft roster, the ordering of the comparable rows was
>   **unchanged at every rank**. The magnitude finding is real and fixed; the ranking claim
>   was misattributed.
> - **"`lineup_component` reports exactly `w0` of the real effect"** — the measured ratios
>   are 0.36-0.47 while `w0` is 0.18, because the before- and after-rosters have different
>   `w0`. The substance (a probability slice, not a mechanism) is right and is fixed; the
>   arithmetic characterisation is not, and the fix is not "divide by `w0`".
> - **"extend the enumeration to two-out for the drop-board pass only"** — half-adopted and
>   deliberately overshot. Depth 2 recovers only ~55% of the gap; depth 3 recovers ~85% and
>   still fits the budget. Rejected outright: running the SEARCH at any depth above 1
>   (measured ~9x, which blows the 30 s budget), and using Monte Carlo anywhere the output is
>   sorted (it dissolves the tie band).
> - **"stop presenting the marginal as a magnitude the operator can act on"** — rejected.
>   Removing the number removes the recommendation; the number was made accurate instead.
> - **`test_the_same_handcuff_is_valuable_on_a_thin_roster_and_worthless_on_a_deep_one`'s
>   `== 0.00` assertion was relaxed to `< 0.5` and `< thin/10`.** The recon's exact 0.00 was
>   itself an artifact of one-out truncation: behind three better backs the handcuff DOES
>   reach the lineup when the starter and two others are out at once, which depth 3
>   enumerates. Measured 0.16 over 15 weeks. The football claim — worth an order of magnitude
>   less on the deep roster — is unchanged and is what is now asserted.
>
> **Deferred (unchanged or newly recorded):** `INJURY_RESERVE` gets the Out-fitted return
> curve because no IR-specific curve was ever measured — the row says so out loud rather
> than inventing one; the depth-chart v2 handcuff link and `weekly_stats`-fitted availability
> rates stay deferred with their landing items; extending the scenario set to linked starters
> on OTHER rosters is still open.
>
> **Still unverified until real games are played:** every availability and uplift constant,
> including the new `absence_curve`; the `schedules` bye path and the live-status boundary
> now have tests but have never run on a real in-season day; `roster_status` FREEAGENT vs
> WAIVERS is still all-FREEAGENT pre-draft; and the whole board has only ever been priced off
> a PRESEASON projection snapshot — 3.1b's refresh cadence is what makes the in-season
> numbers real, and the staleness banner is the only thing standing between a July snapshot
> and a November decision.

### 3.2c [Build] Historical NFL backfill & `depth_charts` v2
**Inserted 2026-07-25** (operator decision) as a prerequisite for 3.3, from three facts measured
against the live DB rather than assumed from the plan:
- **The database holds only season 2026, and every stat table is empty.** `weekly_stats`,
  `snap_counts`, `ngs_passing/rushing/receiving`, `injuries`, `team_defense`, `game_odds`,
  `game_weather` and `depth_charts` are all **0 rows**; `schedules` is 272 rows (2026 only) and
  `projections` 115,802 (2026 only). This is not a cadence bug — `nfl_ingest_runs` shows those
  sources correctly `skipped` with *"nothing to pull in the preseason phase"*. 3.1b built a
  **current-season refresher**, and item 1.4's "multi-season history (≥2021)" was satisfied by
  the ingester code, never by a populated database. Nothing will backfill history on its own.
- **A backfill is not one command.** `ziggurat ingest run --season 2025 --dry-run` reports every
  phase-gated source as *"season 2025 phase unknown — schedules not ingested yet"*, so it is a
  two-pass, per-season loop. The underlying `pull_*` functions already take a `years` **list**
  (`pull_weekly_stats(conn, years, *, retrieved_as_of)`); only the registry's one-season-at-a-time
  wiring does not use it.
- **`depth_charts` is still BLOCKED** (recorded in 3.1b: upstream became a dated daily panel that
  the stored table cannot hold and `base.select_as_of` cannot query). *"Opportunity shocks
  (injury/**depth-chart** triggers)"* is half of 3.3's stated goal, and 3.2 already deferred its
  own depth-chart consumer once. The deferral comes due here.

**Goal:** Land 2021–2025 nflverse history (the season set is a recon decision, floored by what
3.3 needs to be gradeable and reaching toward Phase 4's 2021–23 train / 2024–25 validate split),
and rewrite `depth_charts` to the dated-panel shape with the accessor it needs — one dated
snapshot per key, a real publish timestamp as knowledge time, and a diff accessor that 3.3's
depth-chart trigger can query.
**Done when:** the stat tables answer a 2025 mid-season read with real rows; `usage_deltas` runs
unchanged at that as-of and reproduces a known usage step-up; `depth_charts` ingests through the
cadence and its accessor returns both "the chart at as-of X" and "what changed between X and Y";
and a re-run of the full suite plus the 2026 board proves the backfill did not degrade the
draft-critical 2026 data.

**Why it is its own item and not folded into 3.3's recon:** `depth_charts` v2 is a migration
(`007`, `schema_version` 7) plus a table rewrite plus a new accessor query shape plus leakage
tests — that is an item. And 3.1b's headline finding was that **3 of 14 ingesters were already
broken against live upstream while the suite was green**, because the committed fixtures are
frozen 2023 frames; a 5-season × 8-source backfill is the shakeout that finds the rest of that
class, and those failures want to be isolated from 3.3's signal logic so each is diagnosable.

**The standing hazard for this item:** the draft is ~3 weeks out and the 2026 partition
(`players` crosswalk, `projections`, the `espn_ranks` board, league state) is what the draft
weapon runs on. Both 3.1 and 3.1b shipped a *"a degraded pull destroys the day"* defect that only
an audit caught. Every delete-then-write path and collapse floor is in scope for this item's
audit, and no backfill may damage the 2026 data.

**Recon complete 2026-07-25** (11-agent workflow: 7 probes → 2 designers → adversarial reviewer →
note; full note in gitignored `intel/research/backfill-depthcharts-3.2c-design.md`, 1,160 lines).
Findings that reshape the item:
- **The depth-chart panel does not detect injuries — see the amendment to 3.3 below.** This is the
  item's most valuable finding and it is a negative one.
- **`ff_opportunity` (expected TDs) is a leak wearing a valid timestamp, and is moved to Phase 4.**
  TD regression genuinely has no source in any ingested table (`weekly_stats` has no `*_exp` and no
  red-zone column; NGS has expected *yards* and *completions*, never expected *TDs*), so the design
  proposed adding ffverse `ff_opportunity`. Measured via the GitHub release API: it is a **model
  output published months after each season ends** (`ep_weekly_2021.parquet` written 2023-01-05;
  2025's written 2026-02-10) **by a model trained on the season it scores**. Stamping a 2021 week-5
  row `knowable_as_of = 2021-10-10` passes every leakage test while contaminating a Phase-4 backtest
  with the outcome distribution of the season it is grading. Its `model_version` pin pins a release
  *tag*, not a build, and both tags' assets straddle a model refresh on 2025-12-11. There is no
  in-season file at all — that is structural, not calendar. It lands in Phase 4 as a
  backtest-only, `latest_truth`-only source stamped from the asset's `updated_at`.
- **Store the panel as a change log + tombstones, not verbatim.** Verbatim 2025+2026 (923,162 source
  rows) measured **255.4 MB** on a 43.4 MB database. A row only when a slot's occupant changes, plus
  a tombstone when a slot vacates, measured **31,085 rows / 6.50 MB** with indexes — and it is
  provably lossless: every published panel reconstructed row-for-row against raw upstream,
  **221/221 for 2025 and 127/127 for 2026, 0 mismatches**. This also dissolves the IDP question
  (all-position 6.50 MB vs skill-only ~2 MB — **store everything**, since filtering forecloses
  future D/ST front-personnel work for ~4 MB). The tombstones are load-bearing, not tidiness: two
  probes recommended incompatible accessors because per-key resolution over a full panel inflates a
  board 58% (a KC roster showing both a QB3 and a QB4 named Chris Oladokun) while per-key resolution
  *without* tombstones resurrects ghosts (a phantom rank-4 carried forward seven weeks). Change-only
  + tombstones + per-key resolution ordered on `observed_at` satisfies both — validated at 24 as-of
  points across two seasons, 0 mismatches. **This is item 3.1's `on_team_id IS NULL` lesson again:
  a drop must be a positive fact.**
- **The two halves of the design collided on migration `007`, and it would have bricked every
  command.** Both designers independently wrote a `db/migrations/007_*.sql`; `store.py:58` enforces
  contiguous numbering and `apply_schema` is called unconditionally by `open_db`, which its own
  docstring calls "the safe default for any command" — so `draft-web`, `draft-board`, `league sync`,
  `ingest run` and `marginal` all die at startup with a traceback, three weeks before the draft.
  Caught by the adversarial stage against the real runner, not by inspection. One migration file
  ships (`007_backfill_and_depth_charts.sql`, `schema_version` → 7) plus a test that the shipped
  migrations directory actually applies.
- **Routing 2021–2024 legacy depth charts into the v2 panel table would have stored ~148k rows that
  read back as ZERO with the run log saying `ok`** — the legacy frame carries none of the new
  table's key columns, and the proposed crosswalk rescue would have fabricated "slot vacated" facts
  on 18% of rows. Two tables, permanently: the panel and the legacy weekly shape.
- **The blast radius is genuinely narrow.** Independently re-verified twice: nothing outside the
  owning modules selects from any table the backfill writes; `ziggurat/draft/*` touches only
  `espn_draft_ranks` and `base.TEAM_ALIASES`; `core/valuation.py` and `core/marginal.py` read only
  `{base, projections, schedules}`, all `WHERE season = ?`. **Nothing currently works only because a
  table is empty.** The residual risk is concentrated in the backfill's own correctness and in the
  run-log/concurrency seams — `start_run`'s orphan reap is not season-scoped, and `store.connect`
  sets no `busy_timeout` (verified), so a multi-minute backfill can collide with the league sync,
  whose lost day is the one that is literally unrecoverable.
- **The two-view trap is real and silent, measured on seven accessors.** Backfilled history under
  the default `historical` view returns 0 rows where `latest_truth` returns real data —
  `weekly_stats` 2023 (0 vs 6,002), `snap_counts` 2024 (0 vs 11,589), `injuries` 2023 (0 vs 2,430),
  `usage_deltas` 2025 wk9 (0 vs 83). This is `select_as_of` working as designed, and it must **not**
  be "solved" by back-stamping (`resolve_stamp` already refuses that; it would manufacture a leak).
  The failure mode is an empty result that reads as *"3.3 is broken"* rather than *"wrong view"* —
  hence a parameterized contract test across every backfilled accessor, the highest-value test here.
- Recon also surfaced **10 real defects in shipped 1.4/1.5/3.1b code**, two of which change
  live-cadence behaviour and one of which is a Rule-6 input. See §2.7 of the note.

**Scope decisions (operator, 2026-07-25):** all 10 shipped-code fixes plus the `store.connect`
`busy_timeout` land **inside 3.2c** — they are all in files this item already opens, and splitting
them means two migrations and two audit rounds three weeks before the draft. Two are worth naming
because they change behaviour beyond this item: **F-C** makes `decide()` anchor on `partial` as
well as `ok`, because `weekly_stats` drops the same 22 null-`player_id` rows every season and the
three `ngs_*` drop the week-23 Super Bowl rows, so those four sources **never anchor** and the
daily-firing weekly unit re-downloads four whole-season parquets every day in-season; the trade is
that a `partial` pull now anchors the interval instead of self-healing tomorrow, which is right for
a source that is `partial` by construction every run. **F-F** is the Rule-6 one: `injuries`
last-write-wins currently keeps the STALE status (source carries `Out` at 13:57 and `Questionable`
at 20:55 the previous day for the same player-week; the table stores **Questionable**) — 3 rows
across 5 seasons, and the worst possible class of wrong. `--with-weather` ships as a flag but is
not run (~18 min, 12× everything else, and 3.3 reads none of it).
**Update:**
> **Built, audited and fixed 2026-07-25** (recon → build → 15-agent adversarial audit → fix round;
> suite **832 → 1219**, ruff clean). The backfill runs 2021–2025 across 55 (source, season) pairs in
> **40.6 s**, reproducing every expected row count at **0.000% deviation** (`weekly_stats` 94,735,
> `snap_counts` 132,616, `injuries` 29,148, `schedules` 1,424, `team_defense` 2,848, NGS
> 2,832/2,853/6,707), taking a populated DB from 43.4 → **124.3 MB**. `depth_charts` is unblocked:
> the dated panel is stored as a **change log + tombstones** — **29,483 slot rows + 348 panel rows,
> 6.98 MiB**, against **255.4 MB** for the verbatim panel — and a second-oracle test reconstructs
> the published files exactly. Migration `007`, `schema_version` 7. All 10 shipped-code fixes landed.
>
> **The audit found 11 confirmed major/critical defects. Two are the item's real lessons.**
>
> **C1 (critical) — a tombstone is an ASSERTION derived from an ABSENCE, so anything that can make
> a row absent for a reason other than a real vacancy fabricates a fact.** `_change_log` could not
> tell *"these players were removed from the chart"* from *"upstream's scraper failed for this club
> today"*, and upstream does the latter often: **12 club-panels across the 348 published in
> 2025+2026 carry a partial chart**, most recently **ARI 2026-07-24 (100 slots → 42, zero skill
> players, back to 100 the next day)**. The LAC 2025-12-18 collapse alone wrote **91 tombstones**
> with the run log reading `ok` and `lost=0`, after which `qb1_change_candidates` announced
> *"Justin Herbert is now listed QB1 for LAC (previous=None)"* — this project's signature failure
> class, in the one module whose encoding turns absence into an assertion. **Two obvious fixes were
> tested and are both wrong:** raising re-raises forever (the whole file is re-diffed every pull, so
> a bad past `dt` bricks the source permanently — the asymmetry with `espn_ranks`/league-state,
> where the bad response is transient, is the whole point), and an `n_teams` floor catches **0 of
> 12** (all 348 panels carry 32 teams). Shipped: per-club suppression below
> `PANEL_COLLAPSE_RATIO = 0.50`, the panel flagged `degraded`, and a novice-legible caveat. The
> threshold was re-measured independently on both real files — worst defective ratio **0.4949**,
> lowest legitimate shrink **0.5634** — a clean gap, though only **1.0 pp** of headroom above the
> worst observed defect.
>
> **C2 (major) — the item's own fix, applied to 6 of 14 call sites, and skipped on the one source
> that is perishable, daily and draft-critical.** `adp_rankings.py:128` called `base.upsert` with no
> `key_cols`, so it silently lost a row per pull while the run log claimed 4,699 — confirmed on the
> live DB: **table held 4,698 both days, and `rp`/WR had a hole exactly at rank 64**, because
> FantasyPros ships Travis Hunter twice under one `fantasypros_id`. Every WR below him read one rank
> better than the truth, and `core/divergence.py:172` turns `pos_rank` into the delta that report
> leads with. It survived the build because the guard had no teeth: **7 of 9 `key_cols=` deletions
> passed the whole suite**. Now 15 sites instrumented, **15/15 mutants killed**.
>
> **Also fixed:** an interrupted backfill left an orphan `running` row that refused every subsequent
> `ingest backfill` with no shipped way to clear it (`ziggurat ingest reap` now exists, and `ingest
> status` names it); `ingest run --season <past>` had no fence and wrote **~58k fabricated projection
> rows stamped `knowable_as_of = today`, logged `ok`** — a manufactured leak every leakage test
> passes, the same class that disqualified `ff_opportunity` at recon; and three tests that could not
> fail (C10's payload mutant and C11's listing key both passed the entire suite).
>
> **The process finding, which is not about this code at all: the installed systemd timers run
> `ziggurat` FROM THE WORKING TREE, so uncommitted mid-build code is the production cadence.**
> `db/ziggurat.sqlite` reached `schema_version 7` because a timer applied a migration nobody had
> reviewed or committed. It was benign — no drift, and 007 was never edited afterward (verified
> byte-identical against a pre-round copy) — but only by luck: an applied migration is never
> re-applied, so **one edit to 007 would have left the live database permanently describing a schema
> no file holds, with the whole suite agreeing with the file.** Hence the standing rule that
> corrections ship as a new migration, now enforced by
> `test_an_applied_migration_is_never_edited` rather than by memory.
>
> **A Rule 5 near-miss, hit directly:** `repo_guard.py:23` anchored its pattern as
> `\.sqlite3?(-(wal|shm|journal))?$`, so a backup named `ziggurat.sqlite.bak-v7` matched **neither**
> `.gitignore`'s `*.sqlite` **nor** the pre-commit hook — a 43 MB file of league-private data past
> two of the three enforcement points Rule 5 names. Pattern widened, `.gitignore` widened, case added
> to `tests/test_repo_boundary.py`.
>
> **Verified, not inferred:** the 2026 draft-critical partitions are hash-identical across a full
> migrate + 2021–2025 backfill (`espn_draft_ranks` 3,077, `projections` 173,712, `players` 15,705,
> `adp_rankings` 9,396, the league tables, and all four crosswalks); `draft-board` renders its Pick-1
> recommendation and `draft-web` serves HTTP 200 against a fully backfilled database; and the exact
> Sunday-07:28 unit command was simulated against a copy of the live DB — it writes 6,647 slots
> (714 tombstones) + 127 panels, emits **11 `PARTIAL SCRAPE` warnings**, and **ARI 07-24 and IND
> 07-22 emit zero tombstones**.
>
> **Recorded, NOT fixed** (each is real, none is reachable by a shipped consumer): **C3** — a single
> unresolvable row still advances the panel watermark (inspection only; its trigger is 0 rows in
> 923,162 live). **M3** — a remedy string that names an impossible `--force`. **M2a** — `ingest
> status` reports `depth_charts` as `n/a` on a day it landed 6,774 rows. **M11** — `store.py:128`'s
> `"00%collapsed"` goes blind at migration 010. Three `base.upsert` sites in `ziggurat/league/state.py`
> are still uninstrumented, including `:525`, the unrecoverable dataset's delete-then-write path —
> confirmed no divergence today (1,026 reported = 1,026 stored on all runs), so the gap is
> unverifiability, not loss; it is deliberately out of scope three weeks before the draft.
>
> **Still unverified until a real in-season week:** 11 of the 12 measured panel collapses are
> offseason/preseason, so the 0.50 threshold has never met live roster churn; nothing here has met a
> real game week; and six sources have no 2026 data upstream until ~Sept 10.
>
> **Remaining operator steps:** run the backfill against the live database (it has only ever run on
> scratch copies — the live DB still holds 2026 only), and install the cadence on the Strix Halo,
> which must take this code first since pre-3.2c code now refuses a `schema_version 7` database.

### 3.3 [Build] Candidate generator & signals
**Goal:** High-recall breakout candidate scan from usage deltas and opportunity shocks (injury/depth-chart triggers), plus TD-regression flags. Output: ranked weekly candidate list with the signal evidence attached. (Precision re-ranking arrives in Phase 4 if the podcast arm earns deployment.)
**Blocked on 3.2c** (recorded 2026-07-25): the done-when below reads "last season's data" and
there is none in the database — see 3.2c for the measurements. Its depth-chart trigger is also
blocked on the `depth_charts` rewrite. **And it inherits a two-view seam:** backfilled history
lands with `retrieved_as_of` = the day of the pull, so a 2025 mid-season read under the default
`historical` view returns **empty** — correctly, per Rule 1. 3.3's live in-season path reads
`historical`; its 2025 validation path must bind `base.latest_truth`. Get that wrong and the
generator returns either a silently empty candidate list or a leaked one, and both look plausible.

**Amendment 2026-07-25 — two of this item's three signal arms are not what the goal above assumes.**
Both findings come from 3.2c's recon, measured on the real 2025 season; details in gitignored
`intel/research/backfill-depthcharts-3.2c-design.md` §F3/§F6.

**(a) The depth chart does NOT detect injuries. "Injuries = availability. Depth chart = role
order" — two mechanisms, never to be conflated.** Three probes measured this independently and
none of it is ambiguous. Chuba Hubbard (out wk 5–6), Marvin Harrison Jr. (11–12) and Rhamondre
Stevenson (9, 11) **all stayed `pos_rank = 1` every single day they were ruled Out**, and their
beneficiaries never moved. Systematically: of 15 rank-1 skill players with ≥3 consecutive `Out`
weeks, **1 (7%)** was demoted within 14 days; over any first-`Out` week (n=75), **19%**, median lag
6 days. The chart does not even track who plays — on the 497 team-week-positions where the real
snap leader changed, the pre-week chart already pointed at the new leader **35.0%** of the time,
and pre-week rank-1 led the position in snaps only **55.0%** of the time at WR (QB 88.4%, RB 77.1%,
TE 67.8%, n=2,161). The proposed noise filter does not rescue it: "persists ≥2 consecutive `dt`"
suppresses **2 of 117** rank-1 skill changes, and 9% revert to the prior occupant within 7 days
regardless. Firing anyway would produce **6.5 rank-1 + 15.3 rank-2 alerts per week** league-wide,
48 of the 117 rank-1 changes being TE. **A "starter falls off the depth chart" trigger fires on
essentially none of 2025's real shocks.** What the panel *is* good for is naming the **beneficiary**
of a shock something else detected, and only at QB: conditioned on the starter's absence, panel
rank-2 led the position 92% at QB vs 73% for the usage-only baseline 3.3 already has (n=49) — but
a wash at RB (75%/75%) and **worse than nothing at WR (49%/52%)**. Even that QB cell is conditioned
on *absence*, not on a rank-1 *change*, so it is not the trigger's own precision, which was never
measured. `QB1_CHANGE` therefore ships as a **labelled hypothesis with its source in the reason
text** (3.2's convention), not as a validated trigger. Injury detection comes from the `injuries`
feed, whose real waiver-day lead time is the operative number: **85–88% of rows land at exactly −2
days; only 3.6–9.1% are knowable ≥3 days before the game** (measured through the real accessor at a
real waiver-day as-of).

**(b) TD regression has no source, before or after 3.2c.** No ingested table carries expected TDs,
and the obvious candidate is disqualified — see 3.2c's `ff_opportunity` finding. This arm of the
done-when cannot be built in-season; it moves to Phase 4 alongside the backtest-only source.

**Consequence for this item's scope:** what 3.2c actually unblocks is the **usage-delta** arm
(`usage_deltas` already exists and was validated in 1.4) plus **injury-triggered** opportunity
shocks. The depth-chart arm survives only as a QB-beneficiary hypothesis, and the TD-regression arm
leaves for Phase 4. Rescope the goal accordingly when this item opens rather than pretending the
original three arms are all live.
**Done when:** run against last season's data as-of mid-season, the generator's candidate lists visibly contain the known breakouts of the following weeks (informal sanity check; rigorous measurement is Phase 4).
**Update:** _Built, audited & fixed 2026-07-26._ Permanent `core/candidates.py`
(Rule 8 — never in `draft/`): frozen `CandidateRow`/`CandidateBoard`, thin
`ziggurat candidates` CLI. Built to the rescope above, not the original goal —
three labelled signal blocks rendered separately (high-recall, not merged
precision): **usage-delta breakouts** (`usage_deltas`, full metric set, gsis→name
join, skill positions only), **injury-triggered opportunity shocks**, and
**`QB1_CHANGE`** as a labelled hypothesis (folds `qb1_change_candidates` reasons
verbatim, `hypothesis=True`, no RB/WR/TE rank-change trigger enforced by test).
No scoring/points (Rule 2). **TD-regression not built — no in-season source,
deferred to Phase 4** (recorded per the amendment). Thresholds ship as labelled
`MappingProxyType` hypotheses; precision tuning is 4.2's.

**Operator decision (2026-07-26): the injury arm ships BOTH sources now.** The
nflverse `get_injuries` feed grades the done-when on 2021–2024 lead time but is
**backtest-only for 2025+** (nflverse dropped `date_modified`, so 100% of 2025
rows are gameday-stamped, 0-day lead) and is blind to IR/season-enders entirely
(measured: James Conner, Najee Harris = 0 rows). So the **live** in-season source
is a NEW `state.injury_transitions()` in `ziggurat/league/state.py` — a pure
read-time diff of consecutive `league_player_state` snapshots for availability
crossings (ACTIVE/QUESTIONABLE/None ↔ OUT/INJURY_RESERVE), as-of-gated, no
migration. It is **synthetic/smoke-tested only until real games produce
transitions** (3 pre-season snapshots exist, all free agents) — stated in the
docstring. The two-view seam is threaded throughout: live path reads `historical`,
the 2025 validation path binds `base.latest_truth` (a `--validate` CLI flag
exposes it), the silent-empty-vs-populated fact pinned in tests.

**Done-when met on the live 2025 backfill under `latest_truth`:** the §7.3 five
verified targets all surface — Rico Dowdle (wk5), Sean Tucker (wk8), TreVeyon
Henderson (wk9), Kyle Monangai (wk9), Michael Wilson (wk11, the snap-blind case
caught via air-yards-share + targets) — and the Jahmyr Gibbs negative control does
not dominate.

Three verified workflows (recon → build+green-gate → 7-dimension adversarial audit
with per-finding refute-first verification). **The audit confirmed the seam is
clean** (no leakage, no repo-boundary or rules violations) and found **17 real
defects (2 major, 14 minor, 1 plausible), all fixed.** The headline major is that
**an absence of difference is not an absence of signal**: the usage arm silently
dropped the `prior_week=None` cohort — a rookie who *debuts* for 22 carries after
the starter is ruled Out has all-`None` deltas (nothing to difference against), so
`qualifies()` returned `{}` and the single highest-value waiver breakout of the
week was invisible in **both** arms (the injury arm builds its beneficiary index
from the same dropped usage rows), with no note — exactly the cohort the recon had
named as "the rows to surface." Fixed with an absolute-usage "role emergence" path
(raw target-week usage vs. provisional labelled-hypothesis floors, fed into the
beneficiary index; verified a trickle of ~1/week on 2025, not a flood). Other
fixes: hedged the unhedged "opportunity opened by X" causal claim (fired on
established WR1s); dropped WOPR from magnitude+display (it is
`1.5·target_share + 0.7·air_yards_share` — triple-counted *and* bare jargon);
renamed the `SCORE` column to `SIGNAL` (a novice reads SCORE as points); made the
past-season CLI actionable (`--validate`) and its errors honest ("bind
latest_truth", not "pre-season"); `strip()`ed whitespace-encoded positions that
defeated the None-keep guard; unified the dual-source injury dedupe across a gsis
crosswalk gap; value-aware (`percent_owned`) tiebreak so `--top` stops hiding star
shocks under bench streamers; and hardened four vacuous/dead-code tests (the
forbidden-trigger guard was a tautology; the QB1 snap-to-panel branch was dead).
Suite green (**1255 passed, 4 skipped**; +14). Details:
gitignored `intel/research/candidates-3.3-design.md`.

### 3.4 [Build] Waiver module
**Goal:** Claims-vs-FCFS logic (claims are queued and free — submit liberally), roster-legality precheck (IR eligibility after Tuesday status resets; forced-drop computation), drop recommendations from 3.2, all with reasons.
**Done when:** given a synthetic illegal-roster state, the module correctly refuses to plan claims until legality is restored and proposes the fix.
**Update:** _Built, audited & fixed 2026-07-26._ New permanent `core/waiver.py`
(`core→league` is the established acyclic import direction — `league` never imports
`core`; Rule 8: never touches `draft/`). Flat `ziggurat waivers` CLI. 3.4 is **pure
composition with one new piece**: it calls `marginal.build_board()` **once** and reads
`board.ranked` (drop board) + `board.swaps` (add/drop pairs, already gain>0, already
carrying `add_status` WAIVERS/FREEAGENT), joins `candidates.build_candidates` on
`espn_id` for add-opportunity context, and pulls the roster + FA pool + `waiver_rank` +
`is_transaction_locked` from `league/state`. It re-prices nothing. Frozen dataclasses
`LegalityVerdict`/`DropRec`/`ClaimRec`/`WaiverPlan`; pure `format_waiver_plan`; no
scoring (Rule 2). No migration (`schema_version` stays 7).

**The one new piece is the roster-legality precheck** — the done-when's crux. It recounts
IR itself (`active_players()`/`build_board` strip *all* IR rows unconditionally, so a
naive count misses the exact 17>16 oversize), reslots an ineligible IR occupant IR→BE in
a copy before pricing the forced drop, and runs independently of `build_board` (which
raises `WeekResolutionError` at `scoring_period==0`) so the refuse-and-propose path never
depends on pricing succeeding. Waiver-vs-FCFS keys only on `roster_status`; claims are
gain-ordered with a distinct drop each and bounded to a k≤3 shortlist; streamed D/ST/K
are segregated as "this week only — 3.5's lane". Done-when met on synthetic state
(pre-draft DB has no rostered players): 16 active + 1 IR-slot occupant flipped
OUT→QUESTIONABLE → blocked, empty claims, the ineligible occupant named, a forced-drop
fix — flip to OUT → legal + claims.

**IR eligibility and the whole IR-legality FIX MODEL ship as labelled hypotheses**
(`IR_ELIGIBLE_STATUSES = {OUT, INJURY_RESERVE}`, `IR_FIX_MODEL_LABEL`): ESPN's
authoritative `eligibleSlots` is not ingested and no draft has happened, so the block
condition, the sub-16 non-block, and the move-vs-drop preference all rest on ESPN
mechanics to **confirm in-app post-draft** — disclosed on every plan, same discipline as
scoring §3.8. Open TODOs recorded: ingest `eligibleSlots` for machine-truth; DOUBTFUL/PUP;
within-week priority-reset behaviour.

Three verified workflows (recon → build+green-gate → 7-dimension adversarial audit with
per-finding refute-first verification, 29 agents). **The audit found the seam clean (no
leakage, no rules/boundary violations) and 18 real defects (6 major), all fixed. The
headline: the legality *fix* was non-restorative and non-terminating** — an ineligible IR
occupant was double-counted as an independent violation, so following the plan's own
"drop this player" instruction never reached legality (17/16 → 16/16 → 15/16 … all still
"illegal") while the true fix (move the reset player out of the IR slot) was never stated;
and a costless IR-move fix (seat another IR-eligible body into the vacated slot) was never
offered. Fixed by redefining legality as `active>16 OR ir>1` (restorative, terminating —
proven by a re-run test) with a preference-ordered fix (zero-drop IR-move primary, drop
secondary) and sub-16 rosters never told to drop. Other fixes: the UNVERIFIED IR
disclosure was hidden behind `--reasons` on a destructive drop (now unconditional);
duplicate display-names mis-joined a claim to the wrong `espn_id` that 3.6 would act on
(now `SwapRow` carries `add_espn_id`/`drop_espn_id`, joined on identity); streamed 1-week
D/ST/K were ranked against season-long claims by raw gain and evicted them under budget
(now segregated); unpriceable drops rendered as confident top claims (now flagged in the
default view + de-prioritised); `own_team_id=None` read the whole universe as your roster
→ "drop 5 players" (now refused); a shared `classify_acquisition` so the drop board and
claims can't contradict; plus test-rigor gaps (a real view-threading leakage test, the
candidate join exercised non-empty, the legal-path render constrained, the budget/fallback
branches covered). Suite green (**1305 passed, 4 skipped**; +27). Details:
gitignored `intel/research/waiver-3.4-design.md`.

### 3.5 [Build] Lineup support & streaming
**Goal:** Weekly starter recommendations with win-probability variance posture (opponent projected total → underdog/favorite mode), slot-lock optionality (Thursday players never in FLEX), time-contingent GTD handling, Sunday-morning inactives check; plus the D/ST + K streaming ranker using house scoring, opponent quality, Vegas totals, and weather. Hard-coded sanity checks (OUT/bye players never recommended) enforced in code with tests.
**Done when:** for a synthetic week, the lineup changes appropriately when the opponent's projection swings from −20 to +20, and the streaming ranker's weather sensitivity is demonstrable.
**Update:**
> _[To be completed]_

### 3.6 [Build] Push layer
**Goal:** Post-waiver-window morning scan + briefing (scheduled just after ESPN's overnight processing, surfacing FCFS grabs at breakfast), event-triggered alerts from the news speed lane (starter down → handcuff available), all headless via the routing interface on the Strix Halo.
**Done when:** a real scheduled run produces a briefing the operator can read in two minutes, and a simulated injury event produces an alert.
**Update:**
> _[To be completed]_

### 3.7 [Build] CLAUDE.md operating cadence v1
**Goal:** Encode the weekly rhythm: Tuesday legality + claims, Wednesday post-waiver scan, Thu–Sat monitoring, Sunday inactives + final lineup, Monday journal. Journal and decision-log templates in `intel/weekly/`.
**Done when:** a fresh Claude Code session can execute "run the Tuesday workflow" end-to-end from CLAUDE.md alone.
**Update:**
> _[To be completed]_

### ✦ Checkpoint 3: Week 1 live shakedown
Operate the full loop through NFL Week 1 for real. Journal every friction, wrong output, and manual workaround; validate `scoring.py` against actual ESPN box scores (the anchored TODO from 1.3); fix and amend the plan.
**Checkpoint notes:**
> _[To be completed]_

---

## Phase 4: Backtest & Signal Program (rolling; scoped by Checkpoint 1)

**Goal:** Measure the signals before trusting them. Runs in parallel with Phases 2–3 wherever hours allow — nothing here blocks draft day or Week 1, but signal deployments in-season are gated on results here. Standing methodology for every experiment: strict `as_of` cuts, train on 2021–23 / validate on 2024–25, grade decisions not outcomes.

### 4.1 [Build] Backtest harness & decision grading
**Goal:** Replay engine over the historical spine: step week-by-week through past seasons, exercising production code paths; scorecards for lead-time-vs-market (using the 1.2 proxy) and precision@k (k ≤ 3, the realistic claim budget).
**Done when:** a trivial baseline strategy replays through 2023 producing graded weekly decisions.
**Checkpoint-1 amendment (2026-07-20):** the **first deliverable is the historical market-panel ingester** deferred from 1.5 — DynastyProcess `db_fpecr` weekly PPR ECR (`ecr_type='wp'`, with `ecr/best/worst/sd`) into a new panel table read under **`latest_truth`** (immutable accepted bulk history), NFL week inferred from `scrape_date`, edge week dropped, off-cadence scrapes deduped, **our copy pinned/mirrored**; plus the Sleeper `/research` weekly ownership series (frozen snapshots; use w/w deltas). Scorecards: **lead-time-vs-market** (weeks from a Ziggurat flag at T to the ECR re-rank at T+1/T+2, + hit-rate) and **precision@k, k≤3**. The replay steps week-by-week exercising production code paths, all reads through `latest_truth` accessors (a bulk DB reads empty under the default `historical` view — by design).
**Update:**
> _[To be completed]_

### 4.2 [Experiment] Breakout detection
**Goal:** Tune the 3.3 candidate generator's thresholds on train seasons; measure lead time and precision@k on holdout. Distinguish preseason breakouts (out of scope) from in-season opportunity shocks (the target). Fade detection as a secondary run if results warrant.
**Done when:** findings in `intel/research/breakout-backtest.md`: tuned thresholds, honest holdout numbers, deployed defaults updated.
**Checkpoint-1 amendment (2026-07-20):** tune on the `db_fpecr` lead metric, corroborated by Sleeper ownership deltas; train 2021-23 / hold out 2024-25. **State honestly** that the ECR bar is softer than sharp money and that **K/DST lead grading is weaker** (ECR-only, coarse dispersion). Optional hard-tier cross-check: The Odds API player props for the **2023-05+** window only, reported separately. A projection-driven variant is exploratory (no trustworthy historical stat-line projections — 1.5 decision).
**Update:**
> _[To be completed]_

### 4.3 [Build] Podcast pipeline
**Goal:** RSS archive harvest for a chosen pod slate (must have existed 2021–2025 and still publish), local Whisper with vocabulary biasing + phonetic entity resolution against the player table, claim extraction to the SPEC schema via the routing interface, claim-resolution logic (did the claimed thing happen?).
**Done when:** one full historical season of a single podcast is transcribed, extracted, entity-resolved, and resolution-graded end-to-end.
**Update:**
> _[To be completed]_

### 4.4 [Experiment] Podcast ablation & source calibration
**Goal:** The deploy/retire decision: candidate re-ranking with vs. without podcast features on holdout; per-source (and per-claim-type) reliability calibration learned on train seasons only. A null result retires the arm cleanly — that outcome is a success, not a failure.
**Done when:** findings in `intel/research/podcast-ablation.md` with an explicit deploy / retire / narrow-deploy (e.g., injury-intel claims only) decision.
**Update:**
> _[To be completed]_

### 4.5 [Experiment] Local-model bake-off
**Goal:** Pre-qualify the Ollama fallback before pricing changes force it: 2–3 local candidates vs. Claude on the 4.3 labeled extraction set; measure extraction agreement **and** downstream re-ranker lift preservation (the test that matters). Update routing config with qualified assignments per task tier.
**Done when:** findings in `intel/research/model-bakeoff.md`; routing config carries a validated local assignment for every routine task tag.
**Update:**
> _[To be completed]_

### ✦ Checkpoint 4: Signal deployment decisions
Deploy, narrow, or retire each signal arm per 4.2/4.4 results; fold tuned defaults into the live modules; record what the backtest priors now are (these become the learning loop's anchor in 5.2).
**Checkpoint notes:**
> _[To be completed]_

---

## Phase 5: Season Systems & Maturation (rolling, in-season)

**Goal:** The strategic layer and the self-improvement loop — shipped opportunistically across the season, sequenced by standings context and interest.

### 5.1 [Build] Playoff Monte Carlo & posture
**Goal:** Rest-of-season simulation → live playoff odds → strategic posture (bubble/safe) consumed by waiver, lineup, and trade logic; bye-week and punt-week EV evaluated here rather than by rule of thumb.
**Done when:** posture output demonstrably changes waiver aggressiveness recommendations across synthetic standings.
**Update:**
> _[To be completed]_

### 5.2 [Build] Learning loop
**Goal:** Monday retro workflow: grade the week's decisions on process (correct-but-unlucky = variance, not error); observations → hypotheses → rules promotion ladder in `intel/heuristics.md` with explicit criteria in CLAUDE.md; backtest priors as the anchor (strong, repeated evidence required to override); scheduled memory compaction (~every 4 weeks, journals → `intel/rest_of_season_priors.md`).
**Done when:** two consecutive real retros run from CLAUDE.md alone, and at least one hypothesis exists that is deliberately *not* yet a rule.
**Update:**
> _[To be completed]_

### 5.3 [Build] Trade finder & red-team report
**Goal:** All-roster marginal valuation scan → legibly-fair mutually-beneficial proposals (4-vote veto survivability is a design constraint) + pitch drafting; deployment gated on observed league trade culture. Red-team report: concentration, correlation, bye pileups, injury-fragility on the operator's own roster.
**Done when:** trade finder produces ranked proposals with both-sides reasoning against real league rosters; red-team report runs as part of the weekly cadence.
**Update:**
> _[To be completed]_

### 5.4 [Opportunistic] Deferred & fun
Fade-detection deployment, rookie ramp / injury-return signal families, the league newsletter (social layer), full-season 2025 replay tournament, the public build-in-public writeup. No deadlines; pull from this list when the mood strikes.
**Update:**
> _[To be completed]_

---

## Implementation Notes

### Dependency chain (why this order)
`scoring.py` before valuation (everything prices through it) → ingestion before valuation (data to price) → valuation before draft and waivers (both consume it) → mock sim before draft engine (the harness precedes the thing it tests) → league sync before marginal valuation (roster context) → backtest harness before signal experiments → experiments before signal deployment → backtest priors before the learning loop. Spikes 1.1/1.2 precede everything they inform, which is most of the plan.

### Testing strategy
Per SPEC "Notes for Claude Code": golden-master tests for scoring; `as_of` leakage tests for every accessor; the mock sim as the draft engine's harness; unit tests for pure logic; thin cached-fixture integration tests for ingestion; hard-coded sanity checks (OUT/bye) tested explicitly. Tests land *with* each item, not in a cleanup phase — there is no cleanup phase; the season is the cleanup phase.

### Definition of done (adapted)
An item is complete when: (1) the "Done when" condition observably holds; (2) tests for the item pass; (3) the Update block is filled in — including, for spikes, the findings note in `intel/research/`; (4) nothing league-private entered a committed file.

### Re-planning
Checkpoints are the scheduled moments, but any item may amend the plan when reality disagrees with it. The rule is only: amendments get written down (in the item's Update and, if structural, in the plan body), so the plan on disk is always the real plan.
