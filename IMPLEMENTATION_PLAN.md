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
> _[To be completed]_

---

## Phase 2: Valuation Core & Draft Weapon

**Goal:** Global valuation under house rules, and a draft-day system rehearsed to the point of boredom. **Hard deadline: the draft.** The draft tool is a deletable wrapper; everything else here is permanent.

### 2.1 [Build] Global valuation (VOR)
**Goal:** Re-score consensus projections through `scoring.py`; compute replacement levels from league size/roster structure; produce ranked global values with the house-rules delta vs. ESPN default rankings surfaced explicitly (the "what the room can't see" report).
**Done when:** valuation runs end-to-end from ingested data; spot-checks on known league quirks behave (e.g., pass-catching RBs and league-scored D/STs move the right direction vs. default ranks).
**Update:**
> _[To be completed]_

### 2.2 [Build] Mock draft simulator
**Goal:** Snake-draft sim with bot opponents drafting off ESPN default rank + noise (the room's actual behavior model), configurable to blend market ADP. This is both the strategy laboratory and the draft engine's test harness — build it *before* the engine it tests.
**Done when:** 1,000 mock drafts run headlessly from any slot and output roster + projected-points distributions per strategy.
**Update:**
> _[To be completed]_

### 2.3 [Build] Draft pick engine
**Goal:** Pick logic in the Fry–Ohlmann tradition (player value × board state × positional need), with survival probabilities keyed primarily on ESPN default rank, market/ESPN divergence exploitation, opponent-roster need modeling, and round-appropriate risk posture (floor early, ceiling late; bench picks as options). Validated by tournament runs in the 2.2 sim against naive strategies; distill the academic holdings into `intel/research/draft-strategy.md` as part of this item.
**Done when:** the engine beats ESPN-rank-following bots in sim by a stable margin across slots, and its recommendations come with legible reasons.
**Update:**
> _[To be completed]_

### 2.4 [Build] Draft board TUI
**Goal:** Terminal draft-day interface: fuzzy/alias pick entry (RapidFuzz-style; 'cmc' resolves instantly), continuous background recompute between picks, tier view, ESPN-rank view (the room's screen), contingency prompts at snake turns. Manual entry is the primary path per SPEC; if 1.1 found any live-sync affordance, it's a bonus assist only.
**Done when:** a full mock draft can be driven through the TUI without touching documentation, and no single interaction takes more than ~5 seconds.
**Update:**
> _[To be completed]_

### ✦ Checkpoint 2: Draft dress rehearsal (gate for draft day)
At least two full-speed rehearsals against the sim under a real 60-second clock — operator at the keyboard, tool recommending, picks entered by hand. Fix what breaks; rehearse again if the fixes were structural. Also: strategy selection from the actual draft slot once the league schedules the draft.
**Checkpoint notes:**
> _[To be completed]_

---

## Phase 3: In-Season Operations

**Goal:** The full weekly operating loop, live before NFL Week 1. **Hard deadline: ~Sept 10.** Weeks 1–3 are the richest waiver season; this phase cannot slip into them.

### 3.1 [Build] League state sync & cadence
**Goal:** Scheduled sync of rosters (all 10 teams), standings, matchups, transactions, free agents into temporal tables; runs on the Strix Halo cron.
**Done when:** the database answers "who held player X in week N" and "current FA pool" correctly after a scheduled run with no manual step.
**Update:**
> _[To be completed]_

### 3.2 [Build] Marginal valuation
**Goal:** Roster-context value per SPEC: starting-lineup improvement over remaining season, positional depth, bye coverage, playoff-week schedules, and **conditional-distribution bench valuation** (handcuff contingent value; no median-only drops of lottery tickets). Drop candidates ranked by marginal value.
**Done when:** for a synthetic roster, add/drop recommendations visibly change as roster context changes, with reasons.
**Update:**
> _[To be completed]_

### 3.3 [Build] Candidate generator & signals
**Goal:** High-recall breakout candidate scan from usage deltas and opportunity shocks (injury/depth-chart triggers), plus TD-regression flags. Output: ranked weekly candidate list with the signal evidence attached. (Precision re-ranking arrives in Phase 4 if the podcast arm earns deployment.)
**Done when:** run against last season's data as-of mid-season, the generator's candidate lists visibly contain the known breakouts of the following weeks (informal sanity check; rigorous measurement is Phase 4).
**Update:**
> _[To be completed]_

### 3.4 [Build] Waiver module
**Goal:** Claims-vs-FCFS logic (claims are queued and free — submit liberally), roster-legality precheck (IR eligibility after Tuesday status resets; forced-drop computation), drop recommendations from 3.2, all with reasons.
**Done when:** given a synthetic illegal-roster state, the module correctly refuses to plan claims until legality is restored and proposes the fix.
**Update:**
> _[To be completed]_

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
**Update:**
> _[To be completed]_

### 4.2 [Experiment] Breakout detection
**Goal:** Tune the 3.3 candidate generator's thresholds on train seasons; measure lead time and precision@k on holdout. Distinguish preseason breakouts (out of scope) from in-season opportunity shocks (the target). Fade detection as a secondary run if results warrant.
**Done when:** findings in `intel/research/breakout-backtest.md`: tuned thresholds, honest holdout numbers, deployed defaults updated.
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
