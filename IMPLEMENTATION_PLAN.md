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

**Goal:** Global valuation under house rules, and a draft-day system rehearsed to the point of boredom. **Hard deadline: the draft.** The draft tool is a quarantined wrapper (originally deletable — amended below); everything else here is permanent.

**Amendment 2026-08-31 (pre-draft, operator decision): the draft tool is RETAINED after draft day** for reuse next season — Rule 8 is now an import quarantine, not a deletion (canonical statement + consequences in CLAUDE.md Rule 8; SPEC §8 and design principle 12 amended the same day). Rationale, in brief: the quarantine — not the deletion — was what protected the permanent core; `draft/` imports *from* core, so keeping it in-tree with tests running means every later core refactor keeps it compiling for free, while delete-and-resurrect would mean reintegrating against a core that moved for a year; and Phase 4 already consumes it (`backtest/draft_backtest.py` grades draft decisions through the engine). The 3.2 pre-deletion checklist (in 3.2's audit-round Update, "Pre-deletion checklist") is mooted — nothing is deleted, and `draft/resolver.py` stays put; 3.3/3.6 shipped joining on ids and never needed it. Known follow-up for the post-draft consolidation pass, deliberately not done during the 08-31 freeze: code comments and test docstrings still describing the package as "deletable", and widening the Rule-8 boundary scanner's scope to match the amended rule. **Follow-up closed 2026-09-01:** the "deletable" wording is swept from every committed file (~45 sites across `ziggurat/draft/` module docstrings, `cli/main.py`, `core/dispersion.py`, tests, CLAUDE.md status entries, and this plan's historical Updates — the amendment notes themselves keep "originally deletable" as history); the operator's standing intent is recorded in `ziggurat/draft/__init__.py`'s header so no future session re-derives deletion from stale prose. The scanner widening is judged MOOT, not done: the amended rule's scope — nothing in `ziggurat/` outside `ziggurat/draft/` may import it, `backtest/` explicitly legal — is exactly the `ziggurat/`-only tree the scanner already walks, so there is nothing to widen.

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
> opponent model, entirely in the `ziggurat/draft/` package (Rule 8):
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
> **Done 2026-07-22.** Fry–Ohlmann pick engine in the `ziggurat/draft/`
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
> round; all agents Opus/xhigh. New modules, all in `ziggurat/draft/`
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
>
> **DRAFT SCHEDULED — 2026-08-10. The two deferred items now have hard dates,
> and a live probe of `draftSettings`/`mTeam` changed three assumptions.**
> Operator reports: draft **Mon 2026-08-31 19:00**, order drawn **from a hat
> Mon 2026-08-24 12:00**. Probed against ESPN rather than taken on
> transcription (`view=mSettings,mDraftDetail,mTeam`), which confirmed the dates
> (`date`/`availableDate` epochs → 19:00 / 18:00 PT) and surfaced:
>
> 1. **`timePerSelection` is 90 s, not 60.** Every rehearsal in this checkpoint
>    was run against a 60 s clock, and the quick-pick strip exists because
>    TRANSCRIPTION cost 5-8 s/pick at 60 s. At 90 s the margin is far wider —
>    this is slack, not a new risk, but it means the search fallback is more
>    viable mid-draft than the rehearsal notes above imply.
> 2. **`orderType` is `MANUAL` and `pickOrder` is readable live**, and
>    `state.resolve_own_team(swid=…)` already maps our cookie identity to a
>    team id. So slot discovery on 08-24 is a **read**, not an operator
>    transcription — verified end-to-end today against the placeholder array.
>    The array ESPN serves before the commissioner enters the hat draw is NOT
>    the real order; any pre-08-24 read is provisional by definition.
> 3. **2 of 10 seats still carry zero owners** (unchanged since 2026-07-21).
>    Unowned at draft time = autodraft, i.e. the 2.2 prior (2 of 10 autodrafted
>    in 2025) is holding. Note the asymmetry this creates: the survival model
>    treats every rival as ESPN-rank + noise at σ=17.78, but an autodraft seat
>    is ≈σ=0. Knowing WHICH slots autodraft would sharpen survival between our
>    turns — an available improvement, gated on the seats still being empty
>    near draft day (a seat can fill hours before the draft).
>
> Post-draft roster capture needs no new code: `league sync` snapshots the whole
> player universe with `on_team_id` 4×/day (05/11/17/23:15), so the 23:15 run on
> 08-31 captures the completed draft — and the 2026-07-24 live-sync spike showed
> ESPN flushes the draft atomically at completion, so that snapshot is complete
> rather than partial.
>
> **DRAFT MACHINE SETTLED — the DESKTOP, not the laptop (operator, 2026-08-10).**
> The laptop plan in `docs/runbook-strix-halo.md` §4 assumed an office draft; a
> Monday 19:00 draft is at home. This **removes** the whole copied-database path
> (and with it the divergence risk §2 warns about): the desktop already holds the
> authoritative db, runs the timers, refreshes the board daily via 3.1b, and has
> Chrome installed. `ziggurat/draft/` never writes to the db (§4), so drafting on
> the timer box is safe — the timers keep running throughout.
>
> Two concrete gaps found by inspection on 2026-08-10, both cheap, neither
> previously written down:
> 1. **Tampermonkey is NOT installed in Chrome on the desktop** (checked
>    `~/.config/google-chrome/*/Extensions`). Without it there is no DOM sync at
>    all and the draft falls back to manual quick-pick entry.
> 2. **`draft-web --pick-order` takes 0-BASED SEAT IDS; ESPN's `pickOrder`
>    serves 1-BASED TEAM IDS.** Getting that translation wrong on 08-24 seats the
>    engine in the wrong chair and silently corrupts every survival estimate —
>    no error is raised, the draft just quietly plays someone else's hand. This
>    is the single highest-consequence hand-transcription left in the system and
>    is exactly what a thin read-and-translate command should own.
>
> Remaining, with dates: (1) slot-conditional strategy — preparable for all 10
> slots BEFORE 08-24 since the draw is uniform, reducing 08-24 to a lookup;
> (2) desktop draft readiness — install Tampermonkey, one full dress rehearsal
> on this box; (3) near-day board refresh is now self-healing (3.1b pulls
> projections/adp/espn_ranks daily onto the very machine that will draft).
>
> **Draft-day auto-entry — FEASIBILITY PROBED 2026-08-12, NOT BUILT.** The
> operator asked whether the last mile could be automated (7pm on 08-31 is
> toddler bedtime). Probed with a throwaway diagnostic userscript,
> `ziggurat/draft/espn_probe.user.js` (Rule 8: lives in the quarantined package;
> nothing auto-runs, every test is operator-triggered from its own badge).
>
> **Finding: ESPN's draft room accepts UNTRUSTED synthetic clicks.** A bare
> `element.click()` (ladder level 1 of 4 — no pointer-sequence synthesis, no
> React-fiber poking) drives the real controls. React 16 delegation, `onClick`
> at depth 0, no `isTrusted` check. Confirmed controls:
> `BUTTON.Button--draft.PlayerCard__action-btn` (player-card commit, bound to
> ONE player by construction), `BUTTON.Button--queue` (row queue), and
> `BUTTON.Button--alt.Button--draft` (header commit). The row's action button
> is **Queue off-turn and Draft on-turn** — a third commit path needing no modal.
> So the automation path is a userscript; the CDP/Playwright fallback is NOT
> needed. Live picks ride a private WebSocket — no REST write path exists.
>
> **The methodological lesson is the durable part: three consecutive rounds of
> this probe produced CONFIRMED verdicts that were all false.** A pick landing
> under your team after a click has two innocent explanations that a pick-history
> detector cannot distinguish from success — the operator's own rescue click
> (they were manually clicking to avoid burning a practice pick, and each ladder
> level waits 2.5 s, so a hand anywhere in that window is attributed to whichever
> level was running), and **expiry autodraft**, which takes best-available and
> therefore looks exactly like a plausible pick. The tell was visible and missed:
> the "working" level wandered L1/L2/L3 across runs, and a real mechanism does
> not move. Two design changes made the measurement sound, and both generalize:
> (1) **prefer an experiment whose effect nothing else in the environment can
> produce** — the queue chain runs OFF-TURN, where there is no clock, no
> autodraft, and nothing else populates YOUR queue, so a named player appearing
> there is attributable to the click alone (confirmed twice: T. Higgins, J.
> Price); (2) **witness the confounder directly** — the commit ladder now records
> the draft clock at fire time, and autodraft fires only at 0:00, so the three
> exact-target commits (Flowers p8, Irving p13, Tuten p33, all L1, clock 00:16 /
> 00:19 / 00:22) exclude it. Earlier probe bugs of the same family, each fixed:
> an all-levels-SKIPPED run printed a confident negative verdict instead of
> INCONCLUSIVE; a selector matching bare `div`s returned 192 "Draft buttons" and
> put a wrapper ahead of the real one; and overlapping runs (no mutex) interleaved
> into an uninterpretable wrong-player commit.
>
> **The danger this exposed, if it is ever built:** committing is easy,
> TARGETING is not. The header Draft button is enabled on your turn regardless
> of what is selected, and a plain row-cell click does NOT move ESPN's
> selection — so an early version drafted the default best-available three times
> running (Brown, McMillan, Loveland) while reporting success. Only the
> player-card button is bound to a specific player. Any build must commit
> through a player-bound control, verify-after-commit, and refuse rather than
> guess.
>
> **Proposed architecture if the operator green-lights it (NOT yet decided):**
> keep the Pick Queue continuously populated with the cockpit's ranked board, so
> ESPN's own autopick — which auto-engages after repeated clock expiry — becomes
> a fallback that picks from ZIGGURAT's rankings instead of ESPN's. The
> userscript then makes the pick at the turn via the card path, and refuses on
> ambiguity. Worst case degrades to "autodrafted from our own board," which is
> strictly better than the current manual-entry plan. Remaining engineering (all
> tractable, none research): turn detection; **driving ESPN's search filter to
> bring a non-rendered player into the virtualized grid** (`findPlayerRow` only
> sees rendered rows); reuse of the existing DOM-sync resolver for name→row;
> verify-after-commit. Acceptance test would be a full 16-round hands-off
> practice draft PLUS a deliberate mid-draft kill to exercise the queue fallback.
> Standing risk: ESPN can rebuild that bundle at any time, so a rehearsal on
> 08-30 proves nothing about 08-31 — which is the strongest argument for the
> queue being the load-bearing safety layer rather than the script.
>
> **APPROVED TO BUILD 2026-08-13 — spec at `docs/draft-auto-entry-spec.md`.**
> Operator decisions recorded there: push-on-refusal is wanted but is
> **best-effort only** (they expect to be unavailable from roughly an hour into
> the draft and will ignore pushes then), so no design may depend on a response;
> and they can cover rounds 1–3 in person, so the autonomous window is roughly
> round 5 onward. **The operator identified the build blocker: queue EDIT is
> untested.** The probe proved *append* only — not *remove*, not *reorder* — and
> a queue that can only be appended to is useless by round 3 as the board
> re-ranks. That is P0; if `Remove` cannot be driven, the queue-first design is
> dead and the fallback is active card-path clicking with verify-after-commit.
>
> **DRAFT ORDER READ 2026-08-27 — slot 9 of 10, and the "highest-consequence
> hand-transcription" turned out not to be one.** `draftSettings.pickOrder` now
> serves the real post-hat-draw array (no longer the placeholder), and
> `resolve_own_team(SWID)` maps our cookie to the seat sitting **9th**, machine-
> confirmed rather than transcribed. Our 16 overall picks are 9, 12, 29, 32, 49,
> 52, 69, 72, 89, 92, 109, 112, 129, 132, 149, 152 (the 3-then-17 turn rhythm).
> The `--pick-order` translation this checkpoint flagged as the single most
> dangerous manual step (0-based seat ids vs ESPN's 1-based team ids) is
> **unnecessary**: seat ids are arbitrary internal labels, synced picks arrive
> positionally, and `snake_sequence` was verified to produce the IDENTICAL 16
> picks under identity order + `--slot 9` and under ESPN-team-id seats +
> `--slot 10`. Draft-night command is therefore `ziggurat draft-web --season
> 2026 --slot 9` with NO `--pick-order`, which removes the failure mode rather
> than managing it. Confidence run on the draft-day desktop the same day:
> cockpit launches, `operator_slot` 8, and driving it to overall 9 produces
> live recommendations with Rule-6 reasons. §10's runbook step should be
> amended accordingly.
>
> **UNPRICED BOARD ENTRIES OUTRANKED PRICED ONES — found and fixed 2026-08-27
> (`simulator.load_board`).** The Checkpoint-2 board-completeness fix unions the
> full ESPN universe in at `vor=0.0, house_points=0.0`, commented "never
> recommended (vor 0, points 0, deep rank)". That comment was true only while
> something priced was above zero. **Only ~90 of the live board's 3,263 entries
> carry POSITIVE vor** — replacement level sits at the last starter, so from
> roughly round 11 on, every priced player still available is NEGATIVE, and a
> zero outranks all of them. The path is the engine's "single best by VOR"
> candidate slot (`DEFAULT_CANDIDATE_WIDTH` takes top-C by ESPN rank *plus* the
> best by VOR), which an unpriced entry wins outright; `engine.py`'s
> `(c.vor * frac if c.vor > 0 else c.vor)` then passes the negative through
> undiscounted. **Measured from slot 9 on the real board: 46 of 60 picks in
> rounds 12-16 went to players with NO projection at all** — a fullback at ESPN
> #994, UDFAs at #1226/#1258/#1360 — while Chris Godwin (174 house pts), RJ
> Harvey (165) and Jalen Coker (189) sat there. Hands-off auto-entry would have
> drafted ~4 of our 16 picks as noise with nobody watching.
>
> **Fix:** union entries are floored strictly BELOW the worst priced entry
> (`min(priced vor) - 1.0`), so "never recommended" holds by construction rather
> than by the luck of a sign. Re-verified on the real board with the same 12
> seeds: **46/60 → 0/60**; rounds 12-16 now fill with Josh Downs, Kelce/Andrews,
> Goff, Coker, Harvey, Diggs, Juwan Johnson, Schultz. Starting-lineup metric is
> **bit-identical** (engine slot 9, n=40, seed 42: median 2187.5, mean 2184.9
> before and after) — which is the point: the 2.2/2.3 tournament scores bench
> picks zero and therefore could never have caught this, the same blind spot
> that hid rehearsal 1's QB-stacking defect. `follow-VOR` was hurt MORE than the
> engine and improves (mean 1982.1 → 2081.1), narrowing the slot-9 margin to
> **+104 vs follow-VOR / +153 vs follow-ESPN** (house-projected, self-graded).
> Regression test: `test_union_entries_are_floored_below_every_priced_player`
> asserts the invariant on the board, where the defect lived, not on the engine.
> **Residual, unchanged and still ungraded:** the tail now runs into the QB=3
> cap instead (QB 2.0 → 3.0 per draft in a 1-QB league), i.e. one bench slot is
> still misallocated — the documented bench-blind residual, now spent on a
> droppable backup QB rather than on a fullback. Phase 4 grades realized value.
>
> **FIRST LIVE PRACTICE DRAFT ON THE CURRENT BUILD — 2026-08-27, all 160
> picks, and it found what four rehearsals and three audits did not.** Run
> remotely from this session: Chrome driven via the extension, the operator not
> at the box, the room opened from their laptop and joined by URL. The build
> under test was the one no live draft had ever exercised (`webapp.py` changed
> 08-17, after the 08-16 graduation run) plus the same-day `load_board` VOR
> floor.
>
> **Verified:** slot 9 seating END TO END in a real ESPN room (our picks landed
> at overall 9, 12, 29, 32, 49, 52 … — the league-specific practice room honours
> the real `pickOrder`, so `--slot 9` with no `--pick-order` is now confirmed by
> observation, not just by geometry); sync harvest + back-fill (joined at pick
> 20, recovered every earlier pick, later caught up 63 → 125 in one go);
> queue-fed commits (`achieved: ['N. Collins', 'J. Allen']` matches exactly what
> we drafted at 29 and 32 — ESPN's autopick committing from Ziggurat's queue,
> and Josh Allen at 32 / Loveland at 49 are the same modal picks the slot-9
> profile predicted that morning); refuse-rather-than-guess (two blocks, zero
> wrong players committed); and the manual-entry fallback under the sync layer.
>
> **NOT a valid §8.2 acceptance pass** — joined mid-draft, and the tab was
> hidden throughout, so queue fidelity was never really measured. It was a far
> better bug-finding run than a clean one would have been.
>
> **Four defects, all fixed the same day:**
> 1. **(major) ESPN's injury designation broke the pick parser and DAMMED THE
>    FEED.** The Pick History cell renders `Kenneth Walker III` + `Q` + `KC` +
>    `RB` concatenated; the status-flag guard required the character before the
>    flag to be lowercase or a period (protecting "DK Metcalf"), but a
>    GENERATIONAL SUFFIX is uppercase — so the `Q` survived into the name
>    (`Kenneth Walker IIIQ`), disagreed with the clean anchor text, and the
>    commit gate refused. **The refusal was correct behaviour on a bad parse.**
>    Every blocked pick stops the whole feed until a human enters it by hand —
>    while the operator is meant to be hands-off from round 4. 2 of 160 picks
>    today; higher on 08-31 with designations settled. The test fixtures already
>    covered a suffix and a status flag **separately, never together**, which is
>    exactly why three adversarial audits missed it. Fixed in
>    `sync._ends_a_real_name`; both live cells are now verbatim regression
>    cases; mutation-checked (5 tests fail without the fix).
> 2. **(major) A hidden tab throttles both userscripts.** `document.hidden` was
>    true for the whole run; the queue writer stalled after ~pick 33 and ESPN
>    then autodrafted off ITS board. The signature is diagnostic and worth
>    memorising: **D/ST at 149 and K at 152** — the room's own behaviour —
>    instead of the engine's R9/R10 divergence play. Runbook §3.5b now makes the
>    foreground tab a checklist item.
> 3. **(major) `autopickState()` reported `off` while the toggle read
>    `checked: true`.** Its primary selector was scoped to the queue panel and
>    `.autoPick-container` sits outside it, so it fell through to reading
>    whatever checkbox was nearest and reported that control's state. A false
>    alarm in the exact direction the runbook tells the operator to act on, at
>    the exact moment they check. **Queue writer v1.7** reads the named control
>    and returns `unknown` rather than inventing a state — the recurring lesson
>    of this codebase, now in its fifth costume: an absence reported as a
>    measurement is the dangerous failure, not the loud one. Node-backed test
>    (`tests/test_draft_queue_userscript.py`) runs the real shipped function
>    against the live DOM shape; the pre-fix source fails it.
> 4. **(minor, Rule 6) The refusal message was unactionable.** It read
>    `'Kenneth Walker III' has no exact board match (closest: Kenneth Walker
>    III)` — two identical strings, because it printed the anchor name while the
>    failing check was against the cell name. It cost this session a wrong
>    diagnosis (a team mismatch that does not exist) and would have cost the
>    operator more at 19:30. Refusals now name the field that disagreed.
>
> **Operator action before the next practice run: reinstall the queue writer
> userscript** (v1.6 → v1.8) from `http://127.0.0.1:8811/queue.user.js`.
> Remaining gates unchanged: §8.3 (mid-draft kill) and the live half of §8.4
> (injected refusal → exactly one push) are still unrun, and a clean hands-off
> run on the fixed build with the tab in front is what §8.2 still needs. The
> 160-pick journal is kept at `data/draft/practice/session-20260827-091611.jsonl`.
>
> **THE HIDDEN-TAB FAILURE IS NOW LOUD — same day, second round.** Finding 2
> above was fixed at the source of the *silence*, not just documented: the
> queue writer (v1.8) reports `document.hidden` as a structured boolean each
> cycle, and the cockpit turns a sustained `true` into (a) a pulsing full-width
> **ESPN DRAFT TAB IS HIDDEN** banner on the cockpit page and (b) a fourth §7
> push lane (`hidden`: 6 consecutive reports mid-draft, budget 2,
> spacing-railed — the same audit-paid rails as deficit/stall/halt). A
> reason-text sniff covers v1.6/1.7 writers, so the alarm works before the
> pending reinstall; both the threshold and the sniff are mutation-verified. A
> second cockpit-page variant — **QUEUE WRITER SILENT** — fires when no report
> has arrived for 90 s (past Chrome's 60 s intensive-throttling cadence),
> which is the one signal that survives the writer dying entirely; /api/state
> now serves `report_age_s` and the page shows a writer status line (ok/
> degraded · autopick · age). All three banner states were verified live in
> the real page via injected reports (hidden → red banner; visible → clears,
> line green; 136 s quiet → SILENT). The Tampermonkey reinstall itself proved
> **not remotely automatable** (extension pages are walled off from the
> automation extension; no auto-update ever fired — the script ships no
> `@updateURL` and TM's storage shows v1.6 still installed), so it stays a
> one-click operator action at the box.
>
> **The hidden tab's ROOT CAUSE was then measured: the LOCKED DESKTOP.** The
> 08-27 run was driven remotely with the operator away — `loginctl` shows the
> graphical session `LockedHint=yes`, so Chrome sat behind the GNOME lock
> shield and `document.hidden` was true regardless of tab discipline; after 5
> minutes Chrome's intensive throttling cut the writer to ~1 cycle/min, which
> is exactly the pick-33 stall. Remote runs now launch Chrome with
> `--disable-background-timer-throttling --disable-backgrounding-occluded-windows
> --disable-renderer-backgrounding` (runbook §8.0) — verified to hold the ~5 s
> report cadence past the 5-minute cliff behind the locked screen — and use
> `--no-push` (the hidden banner/lane read true, truthfully, all run; a human
> watches /api/state instead). At the box the flags are unnecessary; §3.5b
> remains the primary discipline.
>
> **Strategy-from-slot delivered (the calendar-bound item):** 140 engine
> drafts at slot 9 on the live board → `intel/research/draft-strategy-slot9.md`
> (round-by-round modal picks, the R1 availability→choice ladder, roster-shape
> flags incl. the deliberate 51% early double-TE and the Chase-passed
> divergence, and the autodraft sensitivity re-measured on the live board:
> zero-auto moves R1 to RB 82% and RAISES our median 2181 → 2213 — a full room
> is good for us, and the engine needs no knob either way).
>
> **PRACTICE RUN 2 — same day, all 160 picks, hands-off, ZERO sync blocks.**
> Remote configuration (locked desktop + runbook §8.0 anti-throttle flags,
> `--no-push`, installed writer still v1.6 — the reason-text sniff carried the
> hidden detection). The room was all-bot (every seat on ESPN autopick,
> ~12-minute draft — a far harsher pace than the 90 s room). Result: **the
> full divergence play executed live through the queue-first pipeline for the
> first time** — LA D/ST at overall 89 and Dicker at 109 against a room
> taking K/DST in R13+ — and picks 12→152 tracked the engine's queue
> throughout (Loveland at 32 is an 18-spot reach over ESPN's board; nothing
> but the queue explains it). Journal 160/160; 191/191 writer reports kept,
> `hidden: true` in every one, cadence held past Chrome's 5-minute cliff; no
> halts. **Two facts this run bought:** (1) *ESPN's Autopick arms itself* —
> OFF until a seat's first clock expiry, ON thereafter; an UNARMED expiry
> commits from ESPN's OWN board and ignores the Pick Queue (pick 9: queue
> held the engine's list, ESPN took its #8 ASB), while armed autopick commits
> the queue head at turn start. Runbook §3.5 now instructs flipping Autopick
> ON in the lobby before pick 1 — that closes the P2 unknown. (2) *A practice
> draft is a NEW temporary league with its own leagueId* — the real league's
> draft URL never boots a practice room (runbook §8.0b); on draft night this
> doesn't apply. §8.2's at-box foreground confirmation and §8.1/§8.4 remain
> for the weekend.
>
> **PRACTICE RUN 3 — same day, a LIVE PUBLIC MOCK with ten humans.** Beginner
> 10-team H2H-Points PPR snake (ESPN standard settings, 16 rounds, 30 s
> clock), our seat 6, operator remote, same flags/--no-push configuration.
> All 160 picks synced, **zero blocks, zero conflicts, zero halts**; 16/16
> of our picks committed hands-free. Three results: **(1) The expiry
> question is closed.** With a verified-clean queue and Autopick read OFF in
> the DOM, our pick 6 expired and ESPN committed a player who was neither
> the queue head (available; untaken for 100 more picks) nor even ESPN's
> visible-rank best — pre-arm expiry runs ESPN's internal autopick logic,
> unpredictable from the screen. Lobby-ON is now measured twice, once
> clean. **(2) The armed regime commits the queue head at turn start** —
> both report-bracketed picks match exactly, and the divergence plays are
> only explicable as queue commits: Loveland 15 spots over board, and **the
> full K/DST play executed in a room of live humans able to snipe it**
> (D/ST at 86, K at 95). **(3) The live room-model recalibration adapted on
> real mixed human/autopick behavior for the first time** (reach ≈13 spots
> at pick 80, ≈21 by 126, vs the 17.78 prior — the mixed room sits where it
> should). Two observations recorded, not fixed: the writer's report stream
> was sparse this run (79 reports over ~55 min vs run 2's 191 over 12 —
> cadence question, benign here since every commit was correct; check the
> writer's early-return paths if it recurs) and the roster shape went
> maximally thin at RB (2 RBs total behind a CMC anchor — the flat-rate
> availability model prices bench RBs below WR darts; the "Weakness: RB"
> pattern, now twice observed, is a Phase-4 calibration question). Also
> operator-observed: ESPN kicks the draft-room session if the same account
> opens the room from a second device — runbook §3.5 now says only the
> desktop opens the room on draft night.
>
> **SAME-DAY FOLLOW-UP — the two draft-decision backtests RAN (early Phase-4
> pre-work, operator-requested), and both validated the engine unchanged.**
> Full numbers in gitignored `intel/research/draft-backtest-early-findings.md`.
> (1) D/ST: the realized top-3-vs-replacement prize is 2.5–3.2 pts/wk in
> every season 2021–25, August signals capture roughly half of it (ECR rho
> +0.30…+0.54) — and a VOR-shrinkage tweak was BUILT AND TESTED rather than
> argued about: shrinking D/ST VOR 50% on the live board changes neither the
> engine's points nor its R9 timing (75% shrink merely swaps the D/ST and K
> rounds; yardstick identical). The candidate tweak is a measured no-op, so
> nothing ships. (2) RB insurance: 2021–23 the waiver wire's 3rd-best
> sub-15%-owned RB out-scored the drafted rounds-10-14 bench RB every
> season — the thin-RB lean is realized-validated for an attentive operator;
> the handcuff-conditional form stays open as 4.x follow-up. Data finding
> for 4.1: the db_fpecr ownership series is UNUSABLE in-season for 2024–25,
> which upgrades the Sleeper /research ownership ingest from corroborator to
> REQUIRED for the holdout years.
>
> **FULL LEAGUE-SETTINGS RE-VERIFICATION (operator paste vs live mSettings
> pull vs code, 2026-08-27).** All 46 scoring items match exactly — the live
> D/ST values sit in `pointsOverrides["16"]` (slot-keyed), and merged they
> are byte-identical to the committed fixture the guard test locks to
> `scoring.py`. Roster slots/starters/bench/IR, draft (snake, 90 s, manual
> order, 2026-08-31 19:00 PDT, our seat = position 9 re-confirmed from the
> live `pickOrder`), trade deadline/veto, and schedule (14 matchups, 6
> playoff teams, Total-PF seeding) all match. THREE findings: (1) **CLAUDE.md
> stated the wrong waiver mechanism** — the league runs weekly
> reset-to-inverse-standings (`waiverOrderReset: true`) with processing ~3–4
> AM PT every day EXCEPT Tuesday, not rolling move-to-back with a single
> Wednesday batch; corrected in the cadence intro (the queue-liberally
> conclusion survives and strengthens — priority is use-it-or-lose-it weekly).
> (2) **Offensive return TDs were silently unpriced in realized scoring** —
> ESPN Misc KRTD/PRTD (6 pts) apply to the player, nflverse carries
> `special_teams_tds`, and the offense weight map never included it; mapped
> to `points_per_def_td` (same league value) with a regression test.
> Projection feeds don't forecast return TDs, so no board/valuation number
> moves. (3) Position MAXIMUMS (QB 4 / RB 8 / WR 8 / TE 3 / K 3 / DST 3) are
> ESPN-enforced but not encoded — recorded as accepted (sims never exceeded
> them; the queue mechanism degrades gracefully), alongside the standing
> note that 3.4's drop suggestions don't consult ESPN's undroppable list
> (ESPN blocks such a drop; the operator relays the refusal).
>
> **ROOM COMPOSITION CHANGED — all 10 seats are now owned** (the two ownerless
> seats attached ~2026-08-08 and ~2026-08-12, from `league_teams` history). The
> 2.2 prior `autodraft_fraction = 0.2` is a 2025 fit and no longer describes the
> room; owned is not the same as *present*, so it is not zeroed, but it is now
> an assumption rather than an observation. Sensitivity at slot 9 is real: with
> autodrafters forced to 0, round 1 goes RB **92%** (vs 66%) and elite RBs reach
> pick 9 more often.

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
> 2025 does not; **Corrected 2026-09-04 (C15):** the pre-2025 injury source DIED after the
> 2024 season and 2025 exists only as a one-shot post-season backfill from a replacement
> producer, published 2026-03-18 — 6,068 rows, `season_type` in and `date_modified` out —
> produced outside the scheduled pipeline, which has not run since 2025-08-07; no 2026 file
> exists as of 2026-09-04, so re-check after Week 1 before asserting anything about 2026); `pull_depth_charts` raised for every season (upstream replaced the weekly
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
>   after draft day)** — **[mooted 2026-08-31: the package is RETAINED (Rule 8 amendment;
>   see the Phase 2 goal note). 3.3/3.6 shipped joining on ids and never needed the
>   resolver; if a permanent module ever does, it is ported out, never imported]**: port `draft/resolver.py` (620 lines of fuzzy name resolution,
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
  2025's written 2026-02-10) ~~**by a model trained on the season it scores**~~. Stamping a 2021 week-5
  row `knowable_as_of = 2021-10-10` passes every leakage test while contaminating a Phase-4 backtest
  with the outcome distribution of the season it is grading. Its `model_version` pin pins a release
  *tag*, not a build, and both tags' assets straddle a model refresh on 2025-12-11. ~~There is no
  in-season file at all — that is structural, not calendar.~~ It lands in Phase 4 as a
  backtest-only, `latest_truth`-only source stamped from the asset's `updated_at`.
  **Corrected 2026-09-04 (C28):** `ff_opportunity` IS published in-season — upstream's GitHub
  Action rebuilds the current season's file after every game window (TNF / early / late / SNF-MNF,
  Sep-Feb; 91 successful runs 2025-09..2026-02) and the models are a documented, pinned 2006-2020
  fit, not a fit on the season they score. The 2026 asset 404s only because the season opens
  2026-09-09, so the real disqualifier is that the file is OVERWRITTEN IN PLACE with no
  point-in-time archive (the 2025 asset was rewritten twice in three days during the 2026-09-04
  review) — calendar and mutability, not structure.
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
**Corrected 2026-09-04 (C28):** the disqualifier is the missing point-in-time archive (the file is
rebuilt in place after every game window), not the absence of an in-season file — see the
correction under 3.2c; capturing `ep_weekly_2026.parquet` forward from Week 1 is item 4.2b's.

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
rows are gameday-stamped, 0-day lead — 5,783 of 5,783 REG rows fall on their own team's
gameday, against 12 of 5,952 in 2024) and is blind to IR/season-enders entirely
(measured: James Conner, Najee Harris = ~~0 rows~~ **one row each — Conner wk4, Harris
wk1, both with a NULL `report_status`, neither at or after the injury week; Corrected
2026-09-04 (C15)**). So the **live** in-season source
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

**Amendment — item 3.8a, 2026-09-02 (Rule 7): two statements above are now false and one
of those TODOs is CLOSED as not-to-be-done.** `eligibleSlots` is NOT authoritative for IR:
slot 21 (IR) is listed for **1,036 of 1,036** players alongside slot 20 (BE) — a positional
map, not a per-player gate — so "ingest `eligibleSlots` for machine-truth" is struck, not
deferred, and the field is deliberately not stored. The draft happened 2026-08-31. The
hypothesis has SPLIT: the DESIGNATION half is SETTLED (ESPN's own per-player `injured`
boolean, migration `014`, marks exactly `IR_ELIGIBLE_STATUSES` with 0 exceptions over
1,036 rows), so "UNVERIFIED"/"confirm in-app post-draft" left `IR_ELIGIBLE_LABEL`; the
IR-SLOT MECHANISM half keeps the word in `IR_FIX_MODEL_LABEL` — 0 of 10 rosters have ever
occupied the slot, i.e. unverified by measurement rather than by omission. DOUBTFUL/PUP
remain UNOBSERVED and are now WATCHED by `ziggurat league ir-check` (they are not settled);
within-week priority-reset is untouched. See §3.8's scope bullet 1 and its Update block.

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

**Addendum — item 3.4b, sequential (chain) claim pricing, 2026-09-02.** The first
in-season defect the operating cadence itself surfaced, and it was in the LIST, not
in any single number.

*The defect.* Every `SwapRow.gain` prices its move as if it were the ONLY one you
make, so `_select_claims` ranked them and printed the top k — quoting each claim
against a roster that stops existing the moment the claim above it wins. Tuesday
step 4 then said "queue every positive-marginal claim shown". **Measured
2026-09-01: three RB adds priced +5.55 / +5.25 / +2.21 each alone; all three won;
the JOINT gain was +1.22** (pairs +3.2…+3.9 — RB4 carries insurance value, RB5/RB6
essentially none). **On 2026-09-02 the same tool, on the new roster, recommended
the exact REVERSE of all three** (+2.65 / +1.68 / … each alone; reversing all three
= −1.22), off projections verified byte-identical across the two pulls. The tool was
walking a flat ridge and would have kept recommending one reversal per day, each
one individually defensible.

*The fix.* Price the season-long list SEQUENTIALLY. New seam on `MarginalBoard`
(`ziggurat/core/marginal.py`): `value_after(swaps, *, pure_adds)` = the roster with
each swap's drop removed and add inserted, valued at the board's own reporting depth
(`REPORT_DEPTH = 3`), memoised on the applied identities; plus `swap_keys` (the
model keys, index-aligned with `swaps` — `_reprice_swaps` now returns `(row, keys)`
pairs on BOTH branches, because it drops non-positive rows and re-sorts, so the keys
used to die inside `_SwapMatrix`), `roster_keys`, and `roster_position_counts`. The
memo lives on `_SwapMatrix` because `MarginalBoard` is frozen. `value_after` refuses
a duplicated key (`fill_lineup` does not dedupe — a repeated key seats the same
player twice, measured +29.99 pts on the live roster) and refuses a STREAMED row
outright (a one-week `model_now` price has no meaning over the season window, and
the board does not expose `model_now` to guess with).

`_select_claims` (`ziggurat/core/waiver.py`) became a lazy greedy (CELF) over the
seasonal rows: a heap keyed `(drop_unpriceable, −conditional gain, −standalone gain,
add, drop, index)`, pop, skip on a used add/drop identity or a `POSITION_CAPS`
breach across the chain, re-price if stale, STOP at the first fresh non-positive
gain. `ClaimRec.gain` is now the CONDITIONAL number and `ClaimRec.gain_alone` the
standalone one (printed beside it, because it is exactly what the claim is worth if
the lines above it lose to a rival with better priority); `chain_rank` is 1-based
across claims + grabs and 0 for the streaming lane. `WaiverPlan` gains `chain_gain`
(measured as `value_after(all) − base`, NEVER accumulated — the telescoping identity
`chain_gain == Σ conditional gains` is tested at 1e-9 against an independent
recomputation), `chain_rejected`, `chain_not_repriced` and `chain_stop`.
`claim_budget` became a TOTAL cap over WAIVER + FREE_AGENT + UNKNOWN (stricter than
the old per-bucket cap); the streamed K/DST lane keeps its own separate slice and is
otherwise byte-frozen.

*Two smaller defects fixed in passing.* An open-slot PURE ADD quoted the paired
swap's gain, which understates it (adding without dropping is never worse) — it is
now priced as `roster + add`, and the note says open-slots-first is an ASSUMPTION a
starter-upgrading swap can dominate. And `POSITION_CAPS` was only ever checked
per-swap against the BASE roster, so a chain could breach a cap that nothing
re-checked (`fill_lineup` would have silently seated a legal subset of an illegal
roster).

*Two disclosures shipped rather than hidden.* (1) The lazy re-evaluation is exact
under diminishing returns and a HEURISTIC otherwise — a complement (a starter and
his own handcuff) can be ranked lower than it deserves, never missed, because every
candidate stays on the heap and is re-priced before it can be accepted. One plan
note says so in plain words, and a constructed supermodular fixture pins that a
complement is accepted reporting the HIGHER number. (2) A chain can end because the
matrix ran out of legal drops rather than for any economic reason — measured live
2026-09-02, `board.swaps` held 179 rows over exactly THREE distinct drop identities
— so `chain_stop` distinguishes `nonpositive` / `budget` / `exhausted` /
`eval_budget` / `no_candidates` and the plan never reports bookkeeping as an
economic conclusion. A `CHAIN_EVAL_BUDGET = 200` ceiling on valuations degrades
LOUDLY (the chain stops with a note) rather than silently truncating; cost scales
with chain length × candidates re-evaluated, not with matrix size.

*Live acceptance, re-measured on the shipped command* (`ziggurat waivers --reasons
--claim-budget 10 --as-of 2026-09-02`): **23.9 s** against a 21.8 s pre-change
baseline (48 valuations, ~2 s), inside the item's ~35 s budget. Chain of TWO:
Josh Downs ← Keaton Mitchell **+3.3** (rank 1, conditional == standalone by
construction), Jordan Love ← Chris Rodriguez Jr. **+1.0 conditional / +2.2 alone**
(rank 2); joint **+4.3**. Refused: Jalen Coker ← Woody Marks **+1.7 alone / −5.6
after** (plus 2 more measured, 18 more disclosed as not re-priced). Streaming lane
unchanged (Rams → Chargers D/ST +1.1, five rows). The pre-change tool printed three
independent claims (+3.3 / +2.6 / +1.2) whose joint value is **−1.2**.

*Docs.* CLAUDE.md Tuesday step 4 no longer says "queue every positive-marginal claim
shown"; it says queue the chain in the NUMBERED order printed, that a SHORT list is
the answer rather than a truncation, that the refused list is a fallback only, and
that the joint total is what the plan is worth (it IS the sum of the printed
conditional gains — it is the "alone" numbers that do not add up). Step 2's
"the cap TRUNCATES each claim list" justification for `--claim-budget 10` was
reconciled in the same edit.

Twelve new waiver tests + six new marginal tests; NINE adversarial mutations were
run against a scratch copy of the module (conditional pricing reverted, telescoping
broken, laziness removed, pure-add re-quoted, streaming folded into the chain, the
double-seat guard removed, the unpriceable de-prioritisation removed, key alignment
scrambled, the memo removed) and each was caught by its intended assertion. Four
shipped mechanisms were NOT pinned by any of them and are covered by the audit round
below. No migration (`schema_version` stays 13) and no new dependency; the one CLI
edit is help text — `--claim-budget` described the cap as a per-list shortlist limit
("extra claims are free"), which this item made false, and now names the chain
ceiling. No logic in the CLI (Rule 3). Suite green
(**2,665 passed, 4 skipped**; +18). **Standing lesson: a list of individually-correct
recommendations is not a correct list. If the operator is told to act on all of
them, the tool owes them the JOINT number.**

**Addendum — item 3.4b audit-fix round, 2026-09-02 (same day).** A multi-lens
adversarial audit (sequential-pricing math; novice-facing display; cost/runtime/
determinism; rules/boundary/test-rigor; docs & cadence fidelity) returned **24
confirmed findings**, every one verified by three refute-first agents. All are
fixed. **No recommendation the tool makes moved**: the live command still returns
the same two claims, the same +4.3 joint and the same five streaming rows. The
REFUSED section still holds three rows, but two of them moved: the build had
refused Malik Willis and Tua Tagovailoa on VALUE (a QB add onto a 3-QB roster),
and the audit's new `chain_capped` bucket now names them for what they are —
blocked by `POSITION_CAPS`, not measured worthless — so Jordyn Tyson and AJ
Barner (two more swaps sharing the Woody Marks drop) took their places. Every
other fix is a disclosure the build swallowed, a search fence it lacked, or a
sentence that was false.

*The headline — the module deleted its own measurement.* In the rejection pass a
leftover re-priced at a POSITIVE conditional gain was `continue`d (waiver.py, "a
complement the lazy search under-ranked… Not shown as one"): absent from the
claims, from `chain_rejected` AND from `chain_not_repriced`, after the module had
just spent a 56 ms valuation establishing it was worth having. Meanwhile the plan
printed "a SHORT list is the answer here, not a truncation. Do not queue past the
end of it" and, unconditionally, "It cannot make one disappear." Both are false in
exactly the branch the code handled. Reachable two ways: the handcuff coupling in
`ScenarioModel.week_value` makes the objective genuinely supermodular for a
starter/backup pair, and the `drop_unpriceable` first rung of `_chain_key` breaks
the lazy upper bound even under strict submodularity (that route is also a
DISCLOSURE REGRESSION — pre-3.4b `bucket()` still SHOWED such a row, last). Fixed
with `WaiverPlan.chain_under_ranked` (rendered in the DEFAULT view), the two
sentences deleted/conditioned, and an ACCOUNTING INVARIANT: every leftover the
pass touches lands in exactly one of `chain_rejected` / `chain_measured_not_shown`
/ `chain_under_ranked` / `chain_capped` / `chain_not_repriced`, and the counts must
sum to the leftovers (asserted).

*The chain's ORDER was destroyed by its own renderer.* `_select_claims` builds one
ordered chain and then partitions it by KIND into `claims` / `fcfs_grabs`, which
render as two sections — so a chain that interleaves a free-agent grab prints
rank 1, 3, 2 while the notes say "in the order printed … Queue them in that order",
`_claim_line` says "if the claims above win", and a rank-3 reason says "the 2
claim(s) listed above this one" with one above it. `chain_rank` was rendered
NOWHERE, so the true order was unrecoverable, and CLAUDE.md's amended step 4 told
the operator to follow it. Reachability is the ordinary in-season case (live pool
2026-09-02: 872 FREEAGENT vs 4 WAIVERS). Every chained line now carries `#K`; the
notes say the numbers run across BOTH sections and to act in number order; the
reasons count chain positions rather than printed lines; and the grab-vs-claim
timing asymmetry (a grab is clicked now, claims clear overnight) is stated instead
of left implicit.

*The cost fence starved the half it was protecting.* Phase A (open slots) is
exhaustive per step — 82 distinct adds x ~68 ms on the live board — and shared one
counter with phase B, so at two open slots it burned 194 of 200 valuations and
`chain_stop` came back `eval_budget`; at three, phase B never ran at all and the
priced-refusal list was empty. Phase A now has a per-step top-K scan
(`PHASE_A_SCAN_TOP_K = 25`, disclosed with the skipped count) and its own share of
the ceiling (`PHASE_B_EVAL_RESERVE = 80` held back), and **a phase-A cost stop is
never terminal**. Its `g <= 0` shortcut also stopped settling the whole search
when a candidate was excluded from `reps` by the PURE-add cap test: `cap_ok(s,
pure=True)` counts the add without the offsetting drop, so a same-position swap at
a capped position is legal in phase B and invisible to phase A's "a pure add
dominates the same swap" argument. The constant's own comment is corrected too —
one depth-3 valuation is 56 ms as a swap and ~68 ms as a pure add, so the ceiling
is worth 11-15 s, not the ~2.6 s the live path spends, and it is APPROXIMATE (the
base/`prev`/`alone`/`chain_gain` bookkeeping calls are counted, not refused;
measured overshoot +2).

*Sentences that asserted what nobody measured.* `--claim-budget 0` returned
`no_candidates` and the plan then said "every add here would cost a drop worth
more than the add" without running a single valuation (now `STOP_BUDGET` plus a
note that says nothing about the pool; the streaming slice is clamped so a
negative budget cannot silently truncate). A streaming-only plan printed "these 0
add(s) are priced as a CHAIN, in the order printed … Queue them in that order" AND
suppressed the honest hold-your-roster note, because the guard tested all three
lanes (now the CHAIN is judged on its own, `_chain_notes` returns nothing at n=0,
and `STOP_NO_CANDIDATES` finally has a branch). The "not re-priced" note said
"only the top 3 are" even when the ceiling meant NONE were, and — measured live —
disclaimed as "unmeasured" 10 of 17 rows the chain had ALREADY measured, because
the display cap was tested before the freshness flag (now freshness first, free
numbers always classified, and the wording is driven by what was really priced).
The `STOP_EXHAUSTED` note blamed "every remaining pair reuses a player already
spent above" when the real cause was a `POSITION_CAPS` refusal (drain causes are
now counted separately), and a cap-blocked leftover was rendered with an economic
reason (now its own `chain_capped` bucket naming the cap as an item-3.2 modelling
guard).

*Novice-facing display.* A pure add's FIRST reason bullet was still the swap
matrix's own sentence — the paired swap's number plus a phantom drop, two bullets
above "no drop required" — and with several open slots every pure add named the
same drop; rebuilt from the reported gain, with every inherited sentence naming
the drop filtered out. A chained claim's first bullet likewise stated the
STANDALONE gain unqualified above the headline's conditional one; re-labelled in
place. Only `chain_rejected[0]` was ever rendered and `ChainRejection.reason` was
read by nothing — now every refusal renders, with its reason under `--reasons`,
under a REFUSED heading; refusals sharing one drop collapse into one statement
about that drop; the example was the only unlabelled `<-` on the page (the shipped
`morning_briefing` summarizer inverted it, naming the DROP as the add) and blamed
the ADD for a loss the shared drop caused. The DROP BOARD is priced pre-chain and
said so nowhere while contradicting the chain in the same view — it now discloses
the baseline, marks the drops the chain has spent, marks a drop the chain has
measured as refused, and says a named replacement can be had ONCE. The joint line
hard-coded "{n} wks" and printed "over 1 wks" in a one-week window (one
`_weeks_phrase` for the whole report). The waiver-priority sentence said "your
claims are ranked by projected gain" beside two non-comparable numbers.

*Rules & test rigor.* `position_counts` was `= None`-defaulted, and `dict(None or
{})` makes every `POSITION_CAPS` check `0 + 1 <= cap` — the cross-chain guard the
build ADDED was off unless the caller remembered it. Now required. Four shipped
mechanisms had **no test anywhere** and each survived deletion against the full
suite: `chain_not_repriced`, the cross-chain caps, `CHAIN_EVAL_BUDGET` and its
`STOP_EVAL_BUDGET` degrade path (the only assertion referencing it was
`calls <= CHAIN_EVAL_BUDGET`, which RAISING the constant satisfies), and
`value_after`'s canonical `sorted(keys)` (the determinism test ran both chains in
ONE process, where a set of the same strings iterates identically — the exact
hazard the code comment names). All four are pinned; the key-sort test is
mutation-verified (`sorted(keys)` -> `list(keys)` fails it and nothing else).

*Docs.* CLAUDE.md Tuesday step 4 said the joint total is "not the sum of the
individual lines" — the opposite of the item's own tested telescoping identity,
and of the same file 670 lines earlier; it also asserted the economic stop
unconditionally and told the operator to queue a refusal as a fallback with no
statement that ESPN grants every queued claim in one batch. Rewritten: numbered
order, the total IS the sum of the printed gains (the "alone" numbers are the ones
that do not add up), read the tool's stated stop reason rather than assume it, and
a refusal is a SUBSTITUTE never an addition. Step 2 regained the streaming-slice
sentence its rewrite deleted; Wednesday step 1 now states the briefing's fixed
`claim_budget=3`; the 3.4 status paragraph carries an amendment pointer (it still
said "re-pricing nothing"); and this plan's own "no CLI change" and "every one
mutation-verified" claims are corrected above.

*Four proposed fixes deliberately NOT taken, recorded so they are not re-derived.*
(1) **Reordering the chain so FCFS grabs are priced FIRST.** Two of the three
verifiers judged it harmful: constraining the greedy by ACQUISITION KIND changes
the selected SET, not just the attribution, and one measured that on the live board
it would surface a grab worth **−5.9 after the chain** as an act-now
recommendation. `value_after` is a set function, so the joint number is
order-invariant and nothing numeric is lost by leaving the order alone; the
wall-clock asymmetry (a grab is clicked now, claims clear overnight) is now STATED
instead. (2) **Headlining `gain_alone` for a grab at `chain_rank > 1`.** It would
break the property the whole item rests on — that the printed headline gains SUM to
`chain_gain` (tested at 1e-9, and now the thing CLAUDE.md tells the operator to
read aloud). The standalone number is on the same line either way. (3) **Demoting
`drop_unpriceable` below `−gain` in `_chain_key`** to restore the lazy-greedy upper
bound. Kept: it is a deliberate 3.4 decision with a recorded rationale (accepting an
unpriceable-drop row would price every conditional gain below it against a
fictional post-chain roster), and the disclosure hole it opened is what
`chain_under_ranked` now closes — such a row is re-priced in the rejection pass and
NAMED, with its upper-bound caveat, exactly as the pre-3.4b build showed it.
(4) **A CELF resume** (push the positive re-price back on the heap and continue).
It moves the stop semantics and the point at which `chain_gain` is measured, for a
branch that fires zero times on the live board; reporting the measurement is the
smaller change that removes the false sentence.

*Re-measured.* Live command unchanged in shape and result (see the numbers in the
run below). Eighteen new tests (2,683 passed, 4 skipped; +18 over the build). No migration
(`schema_version` stays 13), no new dependency.

*Gate + operator polish, same morning.* The workflow's gate failed on ONE
sentence — the claim above that the audit fixes left "the same three refusals"
(two had moved to the new position-cap bucket; corrected in place). Two
rendering changes made by hand after reading the live page: the ACTION now
prints first (WAIVER CLAIMS → FREE-AGENT GRABS → REFUSED → BLOCKED → STREAMING
→ DROP BOARD, pinned by test — the refusals are worded against "the moves
above", which is now literally where they are), and a position-2 line reads
"if #1 lands" rather than the range-of-one "if #1-#1 land" (`_above_phrase`,
pinned). Re-run live: 24.1 s, byte-identical across `PYTHONHASHSEED`, same
chain / joint / refusals / streaming rows. Suite **2,684 passed, 4 skipped**.
**Standing lesson: a search that
MEASURES something and then drops it is worse than one that never looked — it
turns the tool's own stop sentence into a lie, and that sentence is the part a
novice cannot check.**

### 3.5 [Build] Lineup support & streaming
**Goal:** Weekly starter recommendations with win-probability variance posture (opponent projected total → underdog/favorite mode), slot-lock optionality (Thursday players never in FLEX), time-contingent GTD handling, Sunday-morning inactives check; plus the D/ST + K streaming ranker using house scoring, opponent quality, Vegas totals, and weather. Hard-coded sanity checks (OUT/bye players never recommended) enforced in code with tests.
**Done when:** for a synthetic week, the lineup changes appropriately when the opponent's projection swings from −20 to +20, and the streaming ranker's weather sensitivity is demonstrable.
**Update:** _Built, audited & fixed 2026-07-26._ Two new **permanent** core modules,
pure composition over existing as-of-gated accessors (no migration; `schema_version`
stays 7; no new table): **`core/streaming.py`** (the D/ST + K streaming ranker,
`rank_streamers` + `format_stream_board`, thin `ziggurat stream` CLI) and
**`core/lineup_support.py`** (the weekly starter recommender — win-probability variance
posture + slot-lock + GTD + inactives, `build_lineup` + `format_lineup_recommendation`,
thin `ziggurat lineup` CLI). Import direction is the established `core→league→data`;
neither imports `draft/` (Rule 8).

**The shaping recon finding is the same flat-rate feed 3.2 found, and it forks the whole
item.** The 2026 projections are a flat SEASON RATE (median week-to-week CV ~1% for skill
positions; D/ST the only real mover at ~12%), which has two consequences: (1) a bare "rank
D/STs by projected house points" is a season-long defense ranking wearing a streaming
label — it names the same defense every week — so the streaming ranker's **opponent-quality
tilt is the load-bearing signal**; and (2) the feed carries no per-week dispersion, so the
win-probability model's **variance is measured off historical realised scoring** (nflverse
2021-2025 REG re-scored through `scoring.py`, per-(player|team)-season, ≥8 games, OLS of
weekly σ on weekly mean) and frozen as `DEFAULT_VARIANCE` — a labelled hypothesis with its
cohort + R² quoted in every reason (Rule 6), kept OUT of `scoring.py` (Rule 2: σ
parametrises a downstream win-prob model, it is not a scoring quantity). Fits: RB(2.25,0.395
R²0.70) WR(2.16,0.421 R²0.72) TE(1.43,0.503 R²0.77); QB/DST ~flat as expected; **K σ=3.5 a
PURE hypothesis** (`weekly_stats` carries no FG line — unmeasurable locally); **QB↔own
pass-catcher ρ=+0.35 unmeasured** (the strongest "correlated starts" underdog lever). All
Phase-4-tunable.

The decision: `P(win) = Φ((mu(L)−mu_opp)/√(var(L)+var_opp))` (Φ via stdlib `erf`), `var(L) =
Σσ² + 2Σρσσ` with ρ≠0 only for a QB + own-team pass-catcher. Seat the greedy E-points lineup;
inside a close band `c = max(5, 0.3·√var)` return it verbatim (points-for is the league
tiebreaker); outside it, hill-climb legal single swaps maximising the win-prob z-score,
capped at a 2.0-pt E(points) sacrifice. Underdog promotes ceilings/keeps stacks, favorite
promotes floors/breaks stacks — from `dz/dvar`'s sign, no special case. **Done-when 1 met**
on a synthetic near-tie flex contest (floor RB vs boom TE): `opponent_total=own−20` seats the
floor (FAVORITE), `own+20` seats the boom (UNDERDOG), and the **starter sets differ** (the
load-bearing assertion, not a label change). Slot-lock is a **points-neutral relabel** over
`fill_lineup` (latest-locking flex-eligible player → FLEX, total+starters byte-identical),
keyed generically on tz-aware ET kickoff (2026 opener is a *Wednesday*). GTD takes a
keyword-only `now` (decision clock, separate from the `as_of` data gate): `status_known_by =
kickoff−90min`, a later-locking alternative is a "safe wait" → a lock-time-ordered
contingency, not a point pick. `assert_no_illegal_starters` **hard-raises** on a seated
bye/live-OUT player (gated through `live_status_from` so preseason tags don't bench studs).

Streaming: `house_points` is **verbatim** from `weekly_lines` (the same `scoring.py` spine
marginal uses); `stream_score = house_points × Π(bounded labelled multipliers)`, disclosed as
"matchup-adjusted (HYPOTHESIS — not house scoring)". Opponent-quality is the primary tilt;
**Vegas is context-only + leakage-fenced** (`game_odds.knowable=gameday`, so a Tue/Wed waiver
read sees no line and discloses "line not yet posted" — never `latest_truth` in the live
path); weather is the **demonstrable done-when** (same K, calm vs windy → different rank), run
on injected synthetic rows since `game_weather` is empty live. Refuse-and-disclose (never
phantom-zero) for bye/OUT/unpriceable; `lineup_support` seats your rostered K/DST and surfaces
the ranker's pick as an optional upgrade note (never an implicit add/drop).

Three verified workflows (recon 7 agents → build+green-gate → 8-dimension adversarial audit
with per-finding refute-first verification, 16 agents). **The audit found the seam clean (no
leakage, no rules/boundary violations) and 8 real defects (2 major), all fixed; the two
highest-value fixes are mutation-verified.** Headline: **bye-week teams polluted the
opponent-quality reference** — a fully-bye team entered the reference at 0.0 and was not
filtered, inflating its pstdev ~3.5× (2026 wk5: 6.22→21.58) and collapsing the `tanh(z)` tilt
toward zero, **disabling the streaming module's primary signal exactly on the bye weeks when
streaming matters most**, plus printing a false "league average" the novice can't smell (fixed
by restricting the reference + printed average to teams playing this week). Also: the Vegas
home/away sign (correct, but a future inversion shipped green — now three tests, inversion
fails them); the GTD contingency wasn't `now`-gated (a live swap shown after a slot locked →
`window_closed`); an all-dome slate raised a false "DEGRADED weather" banner (split into
`weather_readable` = a row existed vs `weather_available` = an adjustment applied); and four
coverage gaps (correlation cross-term, μ-cap binding, GTD gamble branch, D/ST weather bump)
each now load-bearing-tested. Suite green (**1364 passed, 4 skipped**; +14). Deferrals
(archetype `k_p`, all adjustment magnitudes, close-band/cap widths, live inactives+weather,
playoff-week opponent) recorded in the design note. Details:
gitignored `intel/research/lineup-streaming-3.5-design.md`.

### 3.6 [Build] Push layer
**Goal:** Post-waiver-window morning scan + briefing (scheduled just after ESPN's overnight processing, surfacing FCFS grabs at breakfast), event-triggered alerts from the news speed lane (starter down → handcuff available), all headless via the routing interface on the Strix Halo.
**Done when:** a real scheduled run produces a briefing the operator can read in two minutes, and a simulated injury event produces an alert.
**Update:**
> **Built, audited & fixed 2026-07-30. The FIRST live LLM backend** — everything
> through 3.5 was deterministic; 3.6 implements `claude_cli` (headless `claude -p`
> on the Max subscription) as the one sanctioned model shell-out (Rule 4).
> Operator decisions (locked at start): delivery = full briefing to gitignored
> `intel/weekly/briefings/` + a short teaser curl'd to a private **ntfy.sh** topic
> (public-by-obscurity — **the outbound scrub is the real guarantee**; self-host/
> reserved is a `.env`-only upgrade); news lane = build a real wire NOW; process =
> full recon→build→audit.
>
> **What shipped.** Two scheduled deliverables + plumbing. `ziggurat brief run`
> (Wed 06:00 PT timer) composes waiver(3.4)+lineup(3.5)+candidates(3.3)+the alert
> feed into a structured `Briefing` (`core/briefing.py`, pure), writes the full
> markdown to disk, asks the router (`morning_briefing`→claude_cli→sonnet) for the
> two-minute prose, and pushes an **allowlist-safe teaser** (counts+legality+week,
> NO names). `ziggurat alerts run` (every 20 min 06:00–23:00 PT) pulls the ESPN
> news wire, computes alert-worthy events (`core/alerts.py`, pure: a starter down →
> his handcuff on waivers; news on an owned/rosterable player), dedups, and pushes
> the top few. New: `llm/backends.py` `ClaudeCLIBackend`+`BackendError`;
> `config/llm.toml` two tasks; **migration `008_push_layer.sql`** (schema 8 — four
> append-only tables: `player_news`+`player_news_links` fact/as-of, `push_runs`+
> `alert_ledger` operational/no-as-of); `data/nfl/news.py` (ESPN wire, primary —
> `athleteId==players.espn_id` direct join, zero fuzzy; RotoWire fallback deferred);
> `marginal.handcuff_links()` (reuse — a shared `_rank_depth_chart` kernel + the
> existing `DEFAULT_HANDCUFFS` labelled hypothesis, Rule 2); `ziggurat/push/`
> package (the Rule-5 outbound scrub + the single ntfy choke point via stdlib
> `urllib`, the run-log/dedup helpers, the `run_briefing`/`run_alert_tick`
> orchestration); thin `brief`/`alerts` CLI; `scripts/install-push.sh` + two
> systemd unit pairs. **Migration 008 was iterated against a scratch DB copy and
> only moved into `db/migrations/` once final** — the desktop's live timers apply
> any file there within hours (the standing rule).
>
> **Done-when MET on REAL data, not just simulated:** the whole pipeline produced a
> two-minute briefing and surfaced real preseason injury→handcuff alerts (Kittle
> OUT → handcuff Tonges FA; Kraft OUT → Musgrave FA) end-to-end, and the
> `claude_cli` prose summary is genuinely good (leads with the urgent action,
> preserves every number/name). `injury_transitions` remains **synthetic/
> smoke-tested only until Week 1** produces a real transition (the 3.3 caveat).
>
> **Adversarial audit: 36 agents, 8 dimensions, refute-first per finding. Seam
> clean** (no real leak, no rules/boundary violation that ships private data).
> **19 CONFIRMED + 5 PLAUSIBLE defects, all fixed; 4 refuted. Headline: dry-run
> ledger poisoning** — a `--no-push` preview reserved the dedup ledger before the
> (dry) publish, so the next REAL cadence tick treated every previewed event as
> already-seen and never pushed it; rewritten to **publish-then-record** (ledger
> written only after a confirmed real send — never on a dry run, never on an infra
> failure; a content/scrub block is recorded reserved-not-pushed so it doesn't
> retry forever). Also fixed: the Rule-5 scrub over-blocked on substring ("Rivals"
> blocked "Arrivals") → word-boundary, missed the title/tags headers → scrubs the
> combination, failed OPEN on an empty denylist → fail CLOSED on a real send; the
> news `knowable_as_of` was a UTC-date truncation that withheld evening-Pacific
> notes until local midnight → bucketed in `America/Los_Angeles` to match the live
> gate; a news correction that DROPPED an athlete left a ghost link → links now
> follow the article's resolved version; an injury vacancy was wrongly
> bye-suppressed (an OUT player is hurt, not on bye) → gate removed; an owned K/DST
> OUT fired a high-priority alert outranking a real handcuff → own edge-guard is
> None-position only; own-player OUT printed a raw `INJURY_RESERVE` enum → plain
> language + next-step; `claude_cli` crashed on non-object JSON and its env scrub
> missed `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL`/`CLAUDE_CONFIG_DIR` (all
> reroute off the subscription) → both fixed; `STATUS_EMPTY` keyed on the
> ever-growing candidate count → keys on new-count; briefing per-section isolation
> was asymmetric → broad per-section fallback + guarded staleness. Suite green
> (**1435 passed, 4 skipped**; +71). Details:
> gitignored `intel/research/push-layer-3.6-design.md`.
>
> **Remaining is operator + calendar.** OPERATOR: set `NTFY_TOPIC` (+ optional
> `NTFY_SERVER`/`NTFY_TOKEN`) in `.env`, install the ntfy phone app, then run
> `scripts/install-push.sh` on the desktop and `loginctl enable-linger`. CALENDAR:
> the injury-alert arm and the news wire's real value need live games (~Sept 10);
> `injury_transitions` has produced nothing real yet. DEFERRED (labelled): the
> RotoWire fallback, the `news_summarization` LLM step (task tag reserved, tick
> does no LLM work yet), and the ensure-fresh inline `run_sync` self-heal (v1
> discloses staleness via the banner instead).
>
> **Amendment 2026-08-05 — the phone lane is action-only.** First live evening:
> 46 pushes, all `kind=NEWS` (camp roundups, a columnist's sleeper, a contract
> extension) — partly ledger catch-up on the backlog, but structurally the
> "news on an owned/rosterable player" gate matched the ENTIRE NFL pre-draft
> (everyone is a free agent), and even in-season it is a news stream. Operator
> decision: a push must NAME AN ACTION — the operator is running an experiment,
> not following football. Implemented as `AlertEvent.phone_worthy`
> (safe-by-default False): `INJURY_OUT` always (every surviving variant already
> names an action), NEWS only for an own-roster player (the 20-min news tick is
> the speed layer for "your starter went down"; league sync is 4x/day).
> FA/context news is still computed — it feeds the briefing and the append-only
> alert log — but never the phone; `found` now counts the phone lane only, so a
> context-only tick is honestly `empty`. Measured live: found=166 → 0, no
> ledger surgery. Also that evening: ESPN's edge began 403'ing the news wire's
> identifying User-Agent (fingerprint-consistency rule; browser UAs blocked
> too) — fixed by sending urllib's default UA. Suite 1435 → 1437.

### 3.7 [Build] CLAUDE.md operating cadence v1
**Goal:** Encode the weekly rhythm: Tuesday legality + claims, Wednesday post-waiver scan, Thu–Sat monitoring, Sunday inactives + final lineup, Monday journal. Journal and decision-log templates in `intel/weekly/`.
**Done when:** a fresh Claude Code session can execute "run the Tuesday workflow" end-to-end from CLAUDE.md alone.
**Update:**
> **Built 2026-08-04.** CLAUDE.md's "Weekly operating cadence" placeholder is
> replaced with the day-keyed loop (shared preflight → Tuesday legality+claims,
> Wednesday post-waiver scan, Thu–Sat monitoring, Sunday inactives+final lineup,
> Monday process-not-outcome retro, ~4-weekly compaction into
> `intel/rest_of_season_priors.md`). Design choices: every step quotes the exact
> `.venv/bin/ziggurat` invocation (the done-when makes the section an interface,
> not prose); the division of labor is explicit (tools recommend, ONLY the
> operator acts in the ESPN app); the two ESPN facts that shape the rhythm
> (overnight claim batches ~3:00–4:30 AM ET Wed; priority reset ⇒ claim
> liberally Tuesday, speed matters Wednesday) are stated inline with the steps
> they justify, per SPEC §waivers. One journal template ships instead of two —
> `templates/intel/weekly/week-TEMPLATE.md` holds the decision log AND the
> Monday retro, because SPEC specifies one file per NFL week and a decision and
> its grade belong on the same page; `ziggurat intel init` scaffolds it (live
> tree confirmed). `tests/test_operating_cadence.py` is the doc-rot guard: it
> re-parses every `ziggurat` invocation + flag the cadence section quotes and
> resolves them against the real Typer app (a renamed command or dropped flag
> now fails the suite), plus a guard-the-guard floor so an empty parse can't
> vacuously pass. **Done-when status: executable pre-draft, verified live only
> partially** — all quoted commands run today and disclose empty-roster reality
> honestly (`waivers`: "no roster rows at this as-of"; `lineup`: greedy
> no-opponent card), but a true end-to-end Tuesday (legality trap, real claims,
> journal) needs a drafted roster; final verification is a fresh-session run
> the first in-season Tuesday, under Checkpoint 3. Suite green (1435 passed,
> 4 skipped; +4).
>
> **Fresh-session run, same day (2026-08-04):** an Opus subagent given the bare
> operator instruction "Run the Tuesday workflow" executed it end-to-end from
> CLAUDE.md alone — preflight, legality precheck (legal, pre-draft empty),
> recommendation (queue nothing), journal (`intel/weekly/2026-wk00.md`) — and
> returned six friction findings, all doc-fixed the same day. The two that
> would bite on a real in-season Tuesday: **(b)** step 4 ("queue every
> positive-marginal claim") was unexecutable with step 2's command — the
> default `--claim-budget 3` TRUNCATES each claim list and nothing prints past
> it (Tuesday now quotes `--claim-budget 10` plus an explicit
> re-run-deeper-if-the-last-claim-is-positive rule); **(c)** the preflight's
> "'empty' is healthy" made a push layer that has NEVER RUN read as healthy —
> the actual string `no push runs recorded yet` means not-installed/no
> `NTFY_TOPIC`, and tomorrow's Wednesday step 1 has no briefing until it is
> (the preflight now branches to `scripts/install-push.sh`). The cheap fixes:
> **(a)** `candidates` exits pre-season with "no REG week is fully played" while
> the doc claimed all commands run (now the stated exception, step 3 skipped
> pre-Week-1); **(d)** the journal naming had no preseason slot (now
> `<season>-wk00.md`); **(e)** a zero-action Tuesday's journaling was ambiguous
> between two defensible behaviors (now explicit: the decision not to act is
> journaled and retro-graded); **(f)** run timestamps print UTC, so "did
> today's sync land" is now keyed to the `snapshot <date>` line. In-season
> fresh-session re-verification stays under Checkpoint 3.

### 3.8 [Build] Ground-truth reconciliation — retire the UNVERIFIED hypotheses
**Goal:** Close every open confirmation that could only ever be settled by real ESPN data, now that a drafted roster (2026-08-31) and played weeks (Week 1 completes 2026-09-14) finally produce it. Six items across the build accrued here by explicit deferral, each shipped as a *labelled hypothesis* rather than a guess — this is where they become machine-checked truth or get corrected.

**Section added 2026-08-10.** Seven references to "item 3.8" existed across CLAUDE.md and this plan (from 1.3, 1.5, 3.4 and `core/scoring.py`'s boxed TODO) with **no such section** — Phase 3 stopped at 3.7. The work was real and anchored in code; only the plan entry was missing, so it existed solely as a one-line mention inside Checkpoint 3. Rule 7: the plan on disk is the real plan.

**Scope — wave A, needs only the completed draft (from 2026-09-01):**
1. **IR legality model (3.4).** `IR_ELIGIBLE_STATUSES` and `IR_FIX_MODEL_LABEL` are disclosed UNVERIFIED on every waiver plan because ESPN's `eligibleSlots` is not ingested and no roster existed to test against. Ingest `eligibleSlots` for machine truth, confirm which statuses ESPN actually accepts into the IR slot, and settle DOUBTFUL/PUP handling. Removing an UNVERIFIED banner is the deliverable, not just a passing test.
   > **AMENDED INLINE 2026-09-02 (wave A build, Rule 7): the premise of this bullet is FALSE.** Measured live against the post-draft league: `eligibleSlots` carries **no IR information at all** — slot 21 (IR) is listed for **1,036 of 1,036** players in the universe, alongside slot 20 (BE). It is a POSITIONAL map, not a per-player IR gate, and it is deliberately NOT ingested. The machine truth is ESPN's own per-player `injured` boolean (migration `014`). "Confirm which statuses ESPN actually accepts into the IR slot" also could not be done: **0 of 10 rosters have ever occupied the IR slot**, so that half stays UNVERIFIED by measurement rather than by omission. See the Update block.
2. **`acquisitionBudget = 100` semantics (1.5 open confirmation D).** Season transaction-count cap or inert default? Verify against the live ESPN UI with a real roster. It gates whether the waiver module should be counting spend at all.
3. **Trade deadline** (epoch ≈ early-Dec 2026) localized precisely from league settings.

**Scope — wave B, needs a played + finalized week (from 2026-09-15):**
4. **`scoring.py` box-score reconciliation** — the anchored TODO from 1.3, boxed in the module. Reconcile the engine against actual ESPN weekly D/ST and kicker totals for every team in the league, not just ours (10 teams × ~16 starters is the real sample; one roster is too thin to catch a bracket-edge bug).
5. **D/ST charge semantics (1.5 deferral).** Pin the exact `points_allowed` / `yards_allowed` derivation. Known v1 defect: it **over-charges** — opponent defensive/return TDs scored against *our* offense are counted as points we allowed, and ESPN does not count them. The `team_score`/`opp_score` audit columns were retained in `team_defense` precisely so this refines **without re-ingesting**.
6. **Return-TD attribution.** ESPN credits kick/punt-return TDs to the D/ST (`def_tds`); confirm the feed agrees and that individual returners are not *also* credited (double count).

**Explicitly NOT in scope:** the kicker 50–59 vs 60+ split, which cannot be recovered from Sleeper's `fgm_50p` at all (a 60+ FG scores +5, not +6). That is a source limitation, not an open question — it stays a known, bounded, rare error.

**Done when:** every UNVERIFIED / hypothesis banner listed above is either retired against ESPN ground truth or re-stated with measured error bars; `scoring.py`'s boxed TODO is deleted (or narrowed to what genuinely cannot be settled); and a reconciliation note lands in `intel/research/` recording per-position agreement between Ziggurat's re-scored week and ESPN's published box score, with any residual disagreement explained rather than averaged away.

**Rule note:** any scoring correction lands in `core/scoring.py` **only** (Rule 2), and any derivation fix lands at the ingestion layer where the 1.3 TODO says it belongs — `score_dst` brackets an already-derived value and must not learn to derive one.

**Update:**
> **Wave A done 2026-09-02. Wave B remains open (needs a finalized week, from
> 2026-09-15).** Migration `014_league_ground_truth.sql` → `schema_version` 14:
> three nullable columns on `league_player_state` (`injured`, `droppable`,
> `entry_injury_status`) and a new `league_settings` table, one row per
> `(season, retrieved_as_of)` stamped like `league_teams`. New permanent surface:
> `state.map_settings` / `validate_settings_row`, `state.get_league_settings` /
> `league_position_limits` / `injured_flag_crosstab` / `latest_snapshot_day` /
> `ir_rule_check` (+ `IRRuleReport`, `format_ir_rule_report`, `format_settings`,
> `settings_verdicts`), `marginal.effective_position_caps` / `describe_cap` /
> `MarginalBoard.position_caps` / `MarginalRow.undroppable`, `waiver.DropRec.undroppable`
> / `LegalityVerdict.ir_flag_notes` / `WaiverPlan.position_caps` / `.ir_rule`, and
> `ziggurat league {settings,ir-check}`. Suite **2,714 passed, 4 skipped** (+30).
>
> **The premise correction is the headline, and it is annotated inline on scope
> bullet 1 above.** `eligibleSlots` is not an IR signal: slot 21 is listed for
> 1,036 of 1,036 players, alongside slot 20 (BE). It is a positional map. Storing
> it would have repeated the exact mistake this item exists to correct — store a
> field, assume it means something, never test it — so it is not stored, and the
> module prose that named it as "ESPN's authoritative eligibility" is gone.
>
> **Scope item 1 — IR legality. SPLIT, because only one half is settleable.**
> *Settled:* ESPN serves a per-player `injured` boolean, now ingested, and it
> marks **exactly** `{OUT, INJURY_RESERVE}` — 0 exceptions over the whole
> 1,036-player universe (ACTIVE/false 808, QUESTIONABLE/false 121,
> INJURY_RESERVE/true 54, none/false 42, OUT/true 9, DAY_TO_DAY/false 1,
> SUSPENSION/false 1). `_ir_status` now reads the flag FIRST and falls back to the
> designation proxy per player, with a per-player disclosure; "UNVERIFIED" and
> "confirm in the ESPN app post-draft" left `IR_ELIGIBLE_LABEL`.
> *NOT settled, and it keeps the word:* what ESPN's IR SLOT accepts, and when ESPN
> blocks a transaction. **0 of 10 rosters in this league have ever used the IR
> slot**; every roster entry reads `injuryStatus: NORMAL` and every team reads
> `isTransactionLocked: false`. `injured` agreeing with `injury_status` is TWO
> ENCODINGS OF ONE FACT, not a mechanism — retiring the banner on that basis would
> have dropped the label from a claim exactly as unverified as the day it was
> written. `IR_FIX_MODEL_LABEL` is narrowed to the (a)/(b)/(c) mechanics and names
> the two things that settle it: the first IR occupant this league produces, or a
> 30-second app check (drag a QUESTIONABLE player onto IR, read the refusal).
> **First observation, 2026-09-03 (operator, on the ESPN WEBSITE — there is no
> app on this side):** the roster page exposes moves only through a per-player
> MOVE button under ACTIONS that lists the destinations ESPN will accept, and on
> the operator's 16/16 roster (0/1 IR; ten ACTIVE, five QUESTIONABLE, one
> DAY_TO_DAY, no OUT/INJURY_RESERVE player) IR was offered to NOBODY — only
> starter↔bench swaps. So the "refusal" the label asked for is an ABSENT option,
> not a message, and **the negative half of (b) is observed: an ineligible body
> cannot be put on IR — ESPN enforces the eligibility gate on the way in.** NOT
> observed, and the label says so: the positive half (an OUT/IR player being
> OFFERED the slot — the roster carried none to try; the league's one rostered
> OUT player sits on another team's bench, which says nothing about what that
> manager was offered), and (a)/(c), which need a real occupant who heals. The
> label, the plan-renderer disclosure and the `ir-check` ask were re-worded to
> the website's affordance ("open his MOVE menu the first time an OUT/IR player
> is on your roster"), the word UNVERIFIED stays, and the `ir-check` pin
> (`test_ir_check_names_the_app_check_that_settles_the_item_today`) now asserts
> the observation is printed and the app-drag wording is gone.
> *DOUBTFUL / PUP / NFI remain **UNOBSERVED** and remain treated as INELIGIBLE.*
> They are not settled; they are WATCHED. `ir_rule_check` carries an
> unobserved-status baseline (the seven designations this league had served by
> 2026-09-02) and reports any new one with the `injured` value ESPN gave it and
> how the rule treats it — that is the event the check exists for, because
> "divergences: none" would otherwise print every single day and train the
> operator to skip the report (the same crying-wolf argument 3.1b used to refuse a
> second gap report). `league status` prints the IR line ONLY on news.
>
> **Scope item 2 — `acquisitionBudget = 100`. CLOSED: INERT.**
> `isUsingAcquisitionBudget: false`, `acquisitionLimit: -1`,
> `matchupAcquisitionLimit: -1.0`, `rosterSettings.moveLimit: -1`, `financeSettings`
> all 0.0 — identical in the 2025 `leagueHistory`. The waiver module owes NO spend
> or count accounting. **Counter finding, recorded on the mapper at `map_team` and
> in the migration header so nothing ever reads it wrong:
> `transactionCounter.acquisitions` did NOT increment for three won waiver
> claims** (team 10: 0 acquisitions / 3 drops; a second team: 0 / 1 drop / 5
> moveToActive), so `league_teams.acquisitions` is not a count of claims won.
> Nothing in `ziggurat/` reads it today (grep verified).
>
> **Scope item 3 — trade deadline. CLOSED.** `deadlineDate: 1796230800000` →
> **2026-12-02 09:00 PST** (17:00 UTC), stored at full precision with ESPN's raw
> epoch beside it. The NFL week it precedes is a **JOIN against `schedules`**, not
> arithmetic: the first REG gameday strictly after it is 2026-12-03 → **week 13**.
> Arithmetic would have been wrong by a day — 2026's Week 1 opens on a WEDNESDAY
> (09-09), not a Thursday.
>
> **Two findings outside the stated scope, kept because both prevent a
> recommendation the operator cannot follow.** (a) **ESPN's undroppable list is ON
> in this league** (`isUsingUndroppableList: true`) and 19 of 1,036 players are
> `droppable: false`, **two of them on the operator's own roster**. The app refuses
> those drops. The fence sits at the TWO `swaps.append` sites inside the scan's
> drop loop — not at the loop head, which would have deleted the drop-board row
> the operator still needs — plus a second fence on the illegal-roster forced
> drop, which picks a BOARD row rather than a swap and is the one instruction that
> cannot be worked around (obeying a refused drop leaves the roster illegal and
> every claim blocked). The board still prices him, tagged `[UNDROPPABLE …]` on
> the DEFAULT page, as a field rather than a `reasons` string. (b) **The league's
> own `positionLimits` are ingested**, so the reason text that said "not a league
> rule" now has a league rule to reckon with. Composition happens at ONE seam
> (`effective_position_caps`, resolved inside `build_board`, exposed as
> `board.position_caps`, read by all three cap check points). A naive `min()` was a
> bug twice over: ESPN's −1 "unlimited" sentinel becomes a cap of −1 and refuses
> every add at that position, and the label spaces do not meet ("D/ST" vs "DST"),
> which drops the defense limit silently. Today `POSITION_CAPS` ≤ the league limit
> everywhere (QB 3≤4, RB 8≤8, WR 8≤8, TE 3≤3, K 1≤3, DST 1≤3), so **there is no
> live behaviour change** and the binding path is proved synthetically — an
> unexercised fence is not a fence.
>
> **One correction to this file's own cadence section.** The waiver batch runs
> **00:01–01:13 PACIFIC**, measured from ESPN's own `waiverProcessStatus` map
> (n=29: 28 batches in 2025 + 2026-09-02 at 00:06:12 PDT). CLAUDE.md's "~3–4 AM
> PT" was the EASTERN clock. The operator-facing consequence is now stated in
> Tuesday step 4: submit before **~23:59 PT Tuesday**. `waiverProcessHour: 11` is
> stored RAW — no batch has ever run at that hour in any zone we can name, so the
> unit is unknown and is not guessed.
>
> **Degradation.** A missing or half-decoded settings block never costs the
> snapshot (teams, matchups and players still land) but never logs `ok` either:
> `validate_settings_row` refuses the row and the run degrades to `partial`. A
> DEGRADED row is the real hazard — it looks healthy and silently changes
> decisions through the cap fence and the FAAB verdict.
>
> **Not built, recorded:** `pendingTransactionIds` (present on all 160 entries,
> all empty — a possible future "which claims are queued" read);
> `lineupLocked`/`rosterLocked`/`tradeLocked`; storing `eligibleSlots` (M1);
> anything in wave B. Measured tables and the re-runnable probe:
> gitignored `intel/research/ground-truth-3.8a.md`.
>
> **Standing lesson: two encodings of one field agreeing is a tautology, not a
> confirmed mechanism** — and a standing check that can only ever report the case
> it always passes will be ignored by the time the case it was built for arrives.
> Watch the unobserved, not the observed.
>
> ---
>
> **Audit-fix round — done 2026-09-02, same day.** A multi-lens adversarial audit
> (50 raw findings across 5 lenses, deduped to 30 distinct defects, each verified
> by three refute-first agents) returned **30 confirmed, 0 refuted — 14 major, 16
> minor** on the shipped wave A. All are fixed; **no recommendation, number or
> ordering moves** — every fix is a disclosure the build swallowed, a fence it
> lacked, or a sentence it asserted without measuring. No schema change: migration
> `014` is applied and frozen and nothing here needed a `015`. Two bookkeeping
> notes on the round itself: the fix pass's narrative said "26 confirmed", a
> number that matched neither the dedupe nor the verify stage (both 30) — the
> ledger here follows the stages, not the narrative; and the fix pass closed 29 of
> the 30 and never touched the last (minor: the zero-drop IR-move fix labelled its
> DESTINATION player "(IR-eligible)" with no per-player evidence, while flag-first
> lets that player carry a visible ACTIVE tag), which was fixed by hand afterwards
> — `_zero_drop_reslot` returns each moved player's `_ir_reason` line and the fix
> splices them before `IR_FIX_MODEL_LABEL` — and pinned in
> `tests/test_waiver.py::test_a_zero_drop_ir_move_is_the_primary_fix_when_an_eligible_body_exists`.
>
> **The headline is that the fence's own enumeration was wrong about itself.**
> `marginal.py` listed the four places a drop can be named and closed "A fifth
> path is a fifth fence" — and there was a fifth, unfenced: the blocked page's
> "you may instead DROP {occupant} himself" note names an IR-ineligible occupant
> in PROSE, so neither the matrix fence nor the forced-drop fence covers it. ESPN's
> undroppable list is composed of elite players and the Tuesday crux is a player
> hurt enough to occupy IR, so obeying it means the app refuses and the roster
> stays illegal. Two RENDERING surfaces were wrong the same way: `ziggurat
> marginal` — a shipped operator command whose header is literally "drop board —
> LOWEST value is the most droppable" — rendered NO undroppable tag at any
> verbosity while the same board tagged two rows on `ziggurat waivers`, and told
> the operator to "drop him and add X and you would GAIN N". Both renderers now
> build from one `UNDROPPABLE_TAG` stem, a fenced row's reason is a valuation
> rather than an instruction, and the docstring enumerates six paths and two
> renderers.
>
> **Second: the blocked page discarded its own disclosures.** `format_waiver_plan`
> returned before the `plan.notes` loop, so on the one page where ESPN is blocking
> every transaction NO note rendered — the IR RULE CHECK headline, the
> undroppable-skip note, item 3.4's alternative-fix option, the board's "no
> projections are knowable" caveat. Worse, this item introduced a new terminal
> outcome (`forced_drop is None` when every priceable row is undroppable) whose
> ONLY explanation lived in that dead channel: the page printed an alarm, no fix
> and no reason. Now the notes render, a `NO FIX THIS TOOL CAN NAME` block carries
> the reason, the blocked path also keeps `board.notes` / `position_caps` /
> `league_limits` (it kept none of them), and the dangling "(see CANNOT VALUE)"
> pointer — a section only `format_marginal` has — is gone.
>
> **Third: cap attribution was INFERRED from `cap < POSITION_CAPS[pos]` instead of
> carried, and that is wrong in both directions on the real board.** This league
> allows QB 4 / RB 8 / WR 8 / TE 3 / K 3 / D/ST 3, so at RB, WR and TE the two
> fences TIE and a refusal printed "the binding limit is 3. That is a modelling
> guard …, not a league rule" — which a novice reads as "the app will let me do
> this anyway"; and with NO settings row read at all (every stored day before
> 2026-09-02, the A/B copy, any backtest DB) the header still asserted "TWO fences
> apply … ESPN positionLimits". `MarginalBoard.league_limits` now carries the
> league map (None = not read), `describe_cap` has four answers including
> BOTH-AGREE-AT-N and NOT-CAPTURED, `_caps_phrase` names each entry's source, and
> the header says how many fences really applied. `league settings` no longer
> asserts "this board's own caps are tighter" — a comparison it never performed and
> structurally cannot — and its "-1 = unlimited" legend prints only when a -1 is
> actually on the page.
>
> **Fourth: the flag-first rewrite — the item's headline behaviour change — was
> pinned by NOTHING.** Both tests used rows where the flag and the tag agree, so a
> mutant that ignores `injured` entirely (the pre-3.8a code) passed the full suite,
> 2,702 passed, identical to control. Three discriminating rows are now pinned. The
> same divergence case exposed a self-contradicting page: the violation and the
> REQUIRED ROSTER MOVE quoted the injury TAG as the evidence for a verdict the FLAG
> made, so the page said "ESPN lists him OUT, not IR-eligible" above a label saying
> OUT is exactly the IR-eligible designation. Both sentences are now built from the
> deciding signal and say plainly when the two ESPN fields disagree.
>
> **Fifth: the watch LATCHED.** `OBSERVED_INJURY_STATUSES` is a frozen constant, so
> the first DOUBTFUL (expected 2026-09-11) would have put the identical headline on
> `league status` and on every waiver plan for the rest of the season — the exact
> crying-wolf failure the watch was written to avoid, with the genuine events then
> arriving inside a sentence the operator had been trained to skip. "Never seen
> before" is now derived from this league's OWN earlier snapshots, with the
> constant as the pre-014 floor. Alongside it: `has_news`'s coverage clause is
> scoped to rows that can decide something for THIS reader (a hole on a rival's
> bench, and every permanently-unrepairable pre-baseline snapshot, went silent);
> the first IR occupant is reported as a TRANSITION rather than a standing state;
> and divergences are aggregated by DESIGNATION rather than per crosstab row (the
> third grouping dimension made one diverging tag report as several with each n
> undercounted).
>
> **Also fixed, each with a load-bearing test:** `ziggurat league ir-check` printed
> "divergences: none — the flag marks exactly INJURY_RESERVE/OUT" on snapshots
> where ZERO comparisons were possible, and suppressed its coverage line entirely
> when `rostered_n == 0`, so a silent section doubled as a clean bill of health; the
> `injured`-coverage disclosure named only the IR consequence and not the DROP one,
> though both flags are NULL together, and pointed at `ir-check` (which re-reports
> the same percentage) rather than at the PULL that repairs it; the report never
> named the ~30-second app check that is the only way to settle the IR-slot
> question today; a settings row with NOT-CAPTURED acquisition fields passed
> validation and printed a definitive "FAAB is off, claims are free, no caps"
> verdict, plus the literal string "acquisition budget None is INERT"; the FAAB
> verdict reached no daily surface at all and the claim reason asserted "free and
> non-FAAB" from a hard-coded literal — both now read the flag, and `league status`
> and every waiver plan speak when it is ON or uncaptured; the trade-deadline
> sentence was backwards ("the last NFL week it precedes is week 13" when 13 is the
> FIRST week after it); the `waivers last` line gave a deadline that is wrong on
> Monday nights (this league runs no Tuesday batch) and told the operator to run a
> sync for a field a sync will not produce; the forced-drop note named EVERY
> undroppable roster player as "skipped", including ones that were never candidates,
> and was ungrammatical for the live two-player case; the FAAB settled-date borrowed
> `IR_RULE_BASELINE_DATE`, so bumping the injury baseline silently re-dated an
> unrelated fact (now `ACQUISITION_SETTLED_DATE`); the league's own roster SHAPE was
> stored, printed and consumed by nothing, so `check_legality` and `league settings`
> could contradict each other silently (reconciled by DISCLOSURE, not by deriving
> `RosterStructure` — that also drives replacement levels and the weekly seater);
> and the cross-tab's third dimension plus the LEAGUE branch of `describe_cap` were
> both shipped with no test that could see them (the one wiring test's assertion was
> `or`-joined with a condition that is always true).
>
> **Docs (Rule 7):** CLAUDE.md's Tuesday step 4 quoted verbatim a chain stop
> sentence this item had DELETED, and `tests/test_operating_cadence.py` re-derives
> only quoted `ziggurat` invocations — never quoted OUTPUT — so it rotted silently
> with the suite green; the quote is corrected and a new pin now checks every stop
> sentence the cadence quotes against `waiver.py`. Two numbers were wrong in the
> ledger every later item is graded against: the schema transition (`8→14`, carried
> over from the 3.6 bullet — it was 13→14) and the suite count (2,712 vs the real
> 2,714). CLAUDE.md's new Tuesday/Wednesday text claimed a 05:15 snapshot persists;
> `league_player_state` is partitioned by DAY and every later sync destroys it, so
> the comparison is Tuesday's snapshot against today's and must be done BEFORE any
> Wednesday grab. §3.4's Update block still asserted the premise M1 disproved and
> carried an `eligibleSlots` TODO this item closed as not-to-be-done; it now carries
> the amendment.
>
> **Nothing was deliberately left unfixed.** Suite green (**2,743 passed, 4
> skipped**; +29 over wave A, every one of them a pin for a defect above; four of
> the highest-value pins were mutation-verified — reverting flag-first
> ``_ir_status``, the observed-set union, the divergence aggregation and the
> ``format_marginal`` tag each turns one red). Migrations `001`-`013` are
> byte-identical to HEAD and `014`'s pinned sha256 is unchanged; no timer was
> stopped, because no migration was placed. The A/B (`--path
> db/ziggurat.sqlite.bak-v13`, 24.1 s) and the live run (24.0 s, against a 24.36 s
> baseline) both reproduce the wave-A chain EXACTLY: #1 Downs <- Mitchell +3.3,
> #2 Love <- Rodriguez Jr. +1.0 (+2.2 alone), joint **+4.3**, 5 streaming rows, 3
> refused, 3 capped. The A/B's diff is label text only, and it is now CORRECT
> about that copy: no settings row, so the page says "Only ONE fence applied here"
> rather than crediting ESPN's positionLimits, and the coverage note names both
> flags and the pull that repairs them. `db/ziggurat.sqlite.bak-v13.pristine`
> re-verified at `schema_version` 13, read-only.
>
> **Standing lesson this round paid for: an enumeration of the places a rule must
> be enforced is itself a claim, and it needs a test.** `marginal.py` listed four
> drop paths and asserted the list complete; there were five, and two RENDERERS of
> the one scan disagreed about the same row. The same shape produced the flag-first
> mutant that no test could see: a mechanism is not pinned by a test that cannot
> distinguish it from the mechanism it replaced.

### 3.11 [Build] Draft-engine integration — the composed engine ships as the default

**Goal:** Take the improvements two phases of measurement earned and make them
what `ziggurat draft-web --season 2026 --slot 9` — the unchanged runbook command
— actually runs on draft night, with a one-flag escape hatch that restores the
engine four rehearsals were run on.

**Section added 2026-08-31, the morning of the draft (code freeze 12:00 PT).**
Phase 1 built seven measurement modules; Phase 2 built an evaluation harness and
seven candidate variants and made each prove itself on HELD-OUT seeds against a
week-by-week expected-wins objective. Three earned their place, three were killed
by their own authors. This item is the integration, and it is the last change
before the draft.

**What shipped, and the composition:**

1. **The week-by-week re-rank** (`variant_weekwise`, blend weight 2). The shipped
   engine's score is additive over one player at a time, so nothing in it can see
   that two of your starting backs are off in the same week. This re-ranks the
   engine's own shortlist by what each candidate does to a SEATED LINEUP in every
   week of the season, over a completed hypothetical roster.
2. **The pair re-rank** (`variant_wheel`). At seat 9 of 10 the sixteen picks
   arrive in eight tight pairs three overalls apart. At the FIRST of each pair the
   right question is "which PAIR of players do I end up holding", which a one-ply
   score cannot ask.
3. **The `bots.py` determinism fix** (see below).
4. **The kicker correction is wired but INERT — see "What did not ship".**

**The composition is DISJOINT DOMAINS, and that is the whole design decision.**
Both re-ranks operate on the same candidate list, so the order matters. The pair
term engages only where the operator's next pick is at most `max_pair_gap` rival
picks away — at this seat, exactly overalls 9, 29, 49, 69, 89, 109, 129, 149 —
and delegates verbatim everywhere else. So `WheelPicker(WeekwisePicker(PickEngine))`
gives each term eight picks of its own and neither ever re-ranks the other's
output. That is the only arrangement in which each runs in exactly the regime it
was MEASURED in: `pair_analysis` reconstructs the engine's own score rather than
calling `recommend`, so the pair term never sees a week-by-week-adjusted number,
and the week-by-week term never sees a two-pick total — a quantity on roughly
double the scale, which would have silently halved its blend weight. The other
nesting was rejected for that scale mismatch, not on taste.

**MEASURED ON THE COMPOSITION AS SHIPPED, not on either part** (frozen 2026-08-30
board, seat 9, `rollouts=128`, the calibrated 2.2 room, `grader.grade_roster`
expected wins, paired on identical rooms, `evaluate.paired_compare`):

| contrast | n | mean | 95% CI |
|---|---|---|---|
| weekwise vs the shipped engine, 4 held-out seeds | 1,000 | +0.0431 | every seed's interval excludes zero (+0.0396 / +0.0417 / +0.0522 / +0.0387) |
| + the pair term on top of weekwise, 8 held-out seeds | 2,000 | **+0.0182** | **[+0.0119, +0.0245]**, positive in all eight |

The first row REPLICATES phase 2's +0.0444 claim on seeds neither phase tuned on
and, importantly, through the ACTUAL shipped wiring (`WeekwisePicker` inside
`DraftSession._engine()`) rather than through the variant's own harness. The
second is this item's own measurement and did not exist before: the pair term's
increment on top of weekwise is the same size as its solo held-out estimate at
this seat (+0.0153), i.e. the two are additive here — turning weekwise off at the
eight pair picks costs less than the pair term gains there.

**What did not ship, and why — the kicker correction.** `core/kicker_board.py`
fixes a CONFIRMED data bug (`projections._KICKER_DIRECT_MAP` reads an `fgm_50p`
key Sleeper has never shipped, so every 50+ made field goal scores zero while
every miss still charges −1; all 32 starting kickers understated 25–43 points,
and by a non-constant fraction, so the K board is REORDERED rather than scaled).
It is measured at +0.053 [+0.039, +0.067]. It is wired at the board seam
(`simulator.load_board(kicker_board=...)`, turned on by `load_draft_board`) and
it is a **no-op tonight**, because:

* `espn_projections` — its source — holds **0 rows**. It has never been pulled on
  this box and there is no CLI command or ingest-registry entry that pulls it.
* Filling it needs a live ESPN pull, and `pull_espn_projections` REFUSES to
  back-stamp (correctly). So its rows can only be stamped *today*, which is
  invisible at the golden master's frozen `as_of` of 2026-08-30 — the board that
  decided the picks could not be a board any test had ever seen.
* The alternative source-side route (`derive_fg_50_plus`) is equally unavailable:
  the stored `projections` rows carry the distance buckets but not the source's
  `fgm` total, so the residual is not recomputable from what is in the database.

Shipping a data correction whose data does not exist, hours before a live draft,
by moving the board out from under its own golden master, is the trade this
project spent two phases learning not to make. The seam and its tests ship; the
correction reports OFF in a line the cockpit prints at launch (Rule 6 — the
operator is told the K board he drafts off is misordered, and told it is not
actionable tonight). Post-draft work: a `SourceSpec` entry, a pull, a re-blessed
golden.

**The determinism hazard, fixed.** `BoardState.best_by_vor` iterated `allowed`, a
SET of position strings, keeping the first strictly-greater entry — so an exact
cross-position VOR tie was resolved by **PYTHONHASHSEED**. Verified: a set of six
position strings iterates in a different order under every seed. The ties are not
theoretical — `load_board` floors every UNPRICED ESPN-universe row at ONE
identical `vor`, and the old form gave three different answers on a three-way
tie. **The COUNT recorded here was wrong and the audit corrected it** (2026-08-31):
that tier is **35** rows on the live board, not 1,215 (1,215 is the
`<POS>:<rank>` ID-fallback count, a different quantity), and those 35 sit at ESPN
rank 410 and worse — out of reach in a 160-pick draft. So the fix buys nothing on
tonight's board; it is kept because a total order costs nothing and the board is
re-pulled daily. `best_by_rank` and `window_by_rank` carried the identical hazard
(the latter inside the engine's own candidate gather, where a rank tie at the
truncation line decided which candidates got scored at all). All three now use the
engine's own tie-break ladder as a TOTAL order. Not tripped by the live board —
but the cockpit promises a bit-identical journal replay, and a replay after a
crash runs in a NEW process, i.e. under a new hash seed.

**Done when — all met 2026-08-31:**
* `ziggurat draft-web --season 2026 --slot 9` runs the composed engine, unchanged
  command, verified live end-to-end (journal header `engine_profile: composed`;
  the pick-9 queue head carries the pair reasoning).
* `--legacy-engine` restores the pre-3.11 engine exactly, verified live
  (`engine_profile: legacy`, the single-pick reasoning, and its golden fixture
  did not move by one byte).
* **Latency:** worst-case `recommend()` at `rollouts=512` on the real 3,264-row
  board, measured paired and interleaved so both arms share the box: legacy
  **212.4 ms**, composed **223.4 ms** — an overhead of **+11.1 ms** against a
  243 ms gate (and matching `variant_weekwise`'s own +13.5 ms measurement). The
  pair term is free here: its picks are the ones with only two intervening rival
  picks, structurally the cheapest survival batch of the draft.
* **Determinism:** the full 16-pick composed drive on the real board is
  bit-identical (a) twice in one process, (b) across `PYTHONHASHSEED` 0/1/7/12345
  and `random`, and (c) after a crash + journal replay at pick 100 — including
  the next recommendation off the replayed state.
* **Cost gate:** the composed engine consumes EXACTLY legacy's search — 16
  rollout calls, 69,632 simulated opponent picks — with exactly 8 of the 16 on
  the pair path.
* **The K/DST divergence play is untouched:** D/ST at overall 89 and the kicker
  at 92 under both engines, asserted by name.
* Suite green: **2,422 passed, 4 skipped** (from 2,386).

**Update:**
> **Done 2026-08-31 (before the 12:00 PT freeze).** Two improvements shipped as
> the default, one deliberately did not, and the golden master now freezes BOTH
> engines.
>
> **The re-bless was deliberate and it separates three causes.** `tests/test_draft_golden.py`
> previously held one engine fixture; it now holds two plus the objective input:
> `board-2026-08-30.json` (unchanged), `weekly-points-2026-08-30.json` (new — the
> `grader.weekly_points_map` the composed engine grades with, frozen so the
> DEFAULT path is testable with **no database**; without it the engine that runs
> tonight would have been the only one the module could not check on a fresh
> clone), `engine-golden-2026-08-30.json` (LEGACY, unchanged behaviour — it is the
> escape hatch's proof) and `engine-composed-2026-08-30.json` (new, the default).
> `--bless-weekly` was added so an objective move and an engine move stay
> attributable to different causes, the same discipline the board already had.
>
> **What moved, exactly:** the composed engine takes a different player at **7 of
> the operator's 16 picks** (overalls 32, 49, 52, 129, 132, 149, 152), pinned as an
> EQUALITY rather than a bound — "at most N" would stay green while the change
> quietly stopped doing anything, which is the failure mode a variant this small is
> most exposed to. It does not move overall 9, 12, 29, 69, 72, 89, 92, 109 or 112.
> One number in the composed fixture looks wrong and is not: at the eight pair
> picks `pick_score` is a TWO-PICK TOTAL, so overall 9 reads 333.49 against
> legacy's 199.24 for the same player — the reasons say so in words on every such
> recommendation, which is the only reason it may reach a novice at all.
>
> **Three defects this integration surfaced and fixed, all of them silent:**
> (a) the golden's cost instrument wrapped only `survival.rollout_survival`, so
> the composed drive read 8 calls / 61,440 simulated picks against legacy's 16 /
> 69,632 and looked like a search that had SHRUNK BY HALF — when the other half
> had merely moved to `rollout_pair_batch`, which the instrument could not see. A
> cost gate that reads a relocation as an improvement is worse than none; it now
> counts both, and pins that exactly 8 of 16 decisions take the pair path (0 and
> 16 are both "one of the two shipped improvements is dead code" with every
> behavioural assertion still green).
> (b) `posture.project_postures` reads `session.engine.need_schedule` and clones
> with `dataclasses.replace`, and BOTH cockpits catch a bare `Exception` around
> it — so returning a wrapper from that property would have killed the 2.4
> hysteresis monitor for the whole draft with no error, no log and no symptom
> (confirmed: `WheelPicker` raises `AttributeError` there). Resolved by splitting
> the two surfaces: `session.engine` is the BARE engine the posture comparator
> continues with, `session._engine()` is the composed decision path. That is not a
> dodge — it is the same cost decision both variants already make for themselves
> (`variant_weekwise.POSTURE_CLONE_LABEL`), reached without new wrapper code.
> (c) the resume profile-mismatch refusal reached the operator as a **25-frame
> traceback with the one useful line at the bottom** — during a crash recovery,
> the worst moment of the night for that. Now `EngineProfileMismatch` (a
> `ValueError` subclass, so existing catches still work), caught by name in both
> `app.launch` and `webapp.launch` and printed as one sentence naming the flag to
> add or drop. Verified live: exit 1, no traceback.
>
> **The resume refusal itself is new and load-bearing.** The board hash already
> catches a resume against a differently-loaded board, but nothing in a journal's
> picks would catch resuming a composed session on the legacy engine or the
> reverse: the picks already made would stand while every remaining pick was
> decided by a different engine. The header now records `engine_profile` and a
> mismatch raises. A pre-3.11 journal carries no such field and resumes as
> `legacy`, which is what wrote it.
>
> **What is deliberately still true and unflattering.** Every margin above is
> against the calibrated 2.2 MODEL of the room, on our own projections, graded by
> our own week-by-week objective. The one external validation this project has
> (2021–2025 FantasyPros ECR boards, graded on realized weekly house points) could
> NOT demonstrate that the engine beats drafting the preseason consensus straight
> down — mean +0.066 wins, and not one of 16 cells excluding zero. That result can
> only speak to roster CONSTRUCTION (no free historical point-in-time projection
> exists, so every strategy shares the within-position ordering), and it is the
> right amount of humility to carry into tonight. It is now recorded in the runbook
> §9 rather than only in a research note.
>
> **Deferred to post-draft, recorded rather than done:** the kicker correction's
> source pull (a `SourceSpec` + CLI command + re-blessed golden); the
> `weekly_stats._COLUMNS` gap that grades every kicker 0.000 in every week
> (realized-outcome analysis only, needs a migration + backfill); and the
> `durable` availability variant, which measures +0.17 under an
> availability-aware objective nobody has validated and −0.039 under the shipping
> one.

### ✦ Checkpoint 3: Week 1 live shakedown
Operate the full loop through NFL Week 1 for real. Journal every friction, wrong output, and manual workaround; validate `scoring.py` against actual ESPN box scores (the anchored TODO from 1.3, now scoped as **item 3.8**); fix and amend the plan.
**Checkpoint notes:**
> _[To be completed]_

---

### 3.11a [Fix] Audit round on the composed engine — five majors, before the freeze

**Status: DONE 2026-08-31, before the 12:00 code freeze.** Six adversarial
auditors examined the shipped integration and returned five confirmed majors.
All five are fixed. **No recommendation the engine makes moved**: both goldens'
players, pick scores and drafted rosters are byte-identical, and only reason TEXT
changed (43 rows over two deliberate `--bless-engine` runs). The legacy golden
did not move at all.

1. **The launch read the projections table THREE times.** `build_kicker_board`,
   `build_valuation` (via `load_board`) and `grader.weekly_points_map` each made
   their own `valuation.weekly_lines` pass — 3.55 s apiece on the live board — so
   the composed cockpit printed nothing for 11.8 s idle / 23.6 s loaded, on every
   launch AND every crash-resume, against a runbook that documents "Resume in
   4 s" for the moment §6 calls the dangerous one. `build_valuation` and
   `weekly_points_map` gained the `lines=` hand-over `build_kicker_board` already
   had; `load_draft_board` builds ONE map and passes it to all three. **Measured
   after: cold start 3.85 s, serving 3.87 s, a 100-pick resume 3.83 s — and
   identical to `--legacy-engine`, which is the honest bar.** The board and the
   points map are byte-identical to the frozen fixtures either way.
   The same change fixed `--weeks`, which crashed the default engine because
   `rank_weeks` was not forwarded and the two id spaces were derived over
   different spans.
2. **"Degrade LOUDLY, never crash" was implemented for the kicker half only.** A
   `GradeInputError` from the objective, or a `WeekwiseInputError` from
   `WeekwiseInputs.build`, propagated as a bare traceback that killed the launch
   and named no fallback — unlike `EngineProfileMismatch`, the other launch-time
   refusal, which prints one sentence naming the flag. Both halves now fall back
   to the pre-3.11 engine with a note that names the cause; `DraftSession` records
   `engine_profile: legacy` honestly, so a later resume continues on the engine
   that made the picks.
3. **An on-clock composed fault degraded to a SILENTLY empty panel** — and, once
   the queue cache went cold, an `/api/queue` 500 that stops the writer
   reconciling and hands the pick to ESPN's own board, the failure that cost the
   2026-08-27 practice draft its second half. `DraftSession._recommend_at` now
   serves that one recommendation from the shipped 2.3 engine on a FRESH context
   (proven equal to `--legacy-engine`'s answer for the same state) and records a
   legible fault line the cockpit renders in an amber banner. If BOTH engines
   fail, `/api/state` carries an `engine_error` naming the cause and the restart
   command, instead of a blank panel with no key containing "err".
4. **The wheel promised a partner its own next pick contradicted.** Measured over
   10 rooms at seat 9: of 80 first-of-pair sentences, 32 were broken — 26 of them
   with the named partner STILL on the board, at a median quoted share of 100%.
   The figures describe the ROOM (right ~94% of the time); the sentence read as
   describing the TOOL. Re-phrased as "the pairing this score assumes", with the
   measured break rate stated on the panel. The imperative "Take him now …" is
   gone and a runbook test keeps it gone.
5. **The kicker recommendation asserted "the best your scoring sees" with no
   provenance.** Across 23 rooms, 0 of 23 K panels contained "uncorrected",
   "understated", "misordered" or "50+"; the only disclosure was a terminal line
   printed three hours earlier that the runbook tells the operator to forget.
   `DraftSession.rec_caveats` (position → extra reason lines, registered by the
   launcher from `DraftInputs.kicker_corrected`) now attaches the item-3.10
   caveat to every K row, so it reaches the panel, the `/api/queue` rows and the
   journal. Verified on the golden drive: the K rows at overall 89 and 92 both
   carry it, and the roster is unchanged.

**Minors fixed alongside:** the default engine now announces itself and the
cockpit PAGE renders `engine_profile` plus the launch notes (previously only
`--legacy-engine` printed a line, and only to the terminal); a resume at a
different `weekwise_weight` refuses like a profile mismatch; `1e-09` is gone from
a novice-facing sentence; the bye-hole sentence names the BYE that reconciles it
with the need note two bullets above; `--legacy-engine`'s help text names the
pair re-rank it actually gives up; the recorded justification for the determinism
fix is corrected (35 floor-tier rows, not 1,215); the launch banner now FLUSHES
before `serve_forever` blocks (redirected stdout is block-buffered, so a
remote/unattended run per runbook §8.0 saw the notes and then silence,
indistinguishable from a hang); and the operator's real ESPN league id, hard-coded
at `tests/test_espn_ranks.py:226` since commit `dd0fcb9`, is replaced by the
placeholder every sibling test uses, with a shape-based guard in
`tests/test_repo_boundary.py` so it cannot come back (Rule 5).

**Re-measured after every change.** Latency, paired and interleaved, best-of-5,
worst pick at R=512 on the real 3,264-row board: legacy 216.5/216.8/216.9/218.4
ms, composed 226.1/227.2/229.4/229.6 ms — the +11 ms is weekwise's, unchanged by
this round, and inside the 243 ms observation with ~14 ms of room. Determinism
three ways: same seed twice in one process, five `PYTHONHASHSEED` values in
separate processes, and crash + journal replay at pick 100 — one digest
(`eb91e29f99a79c2e`) throughout, `engine_profile` preserved, next recommendation
identical and idempotent. Suite **2,443 passed / 4 skipped**.

**NOT fixed, deliberately, and recorded instead** (neither is draft-critical, and
neither is worth a change eight hours before a live draft): the literal Rule-8
violation in `backtest/draft_backtest.py`, which imports `ziggurat.draft` at
module import time while `tests/test_draft_boundary.py` rglobs only `ziggurat/`
and structurally cannot see it; and the same suite's `_PERMANENT_PACKAGES`
allowlist omitting `push`, which CLAUDE.md's repo map lists as permanent.

## Phase 4: Backtest & Signal Program (rolling; scoped by Checkpoint 1)

**Goal:** Measure the signals before trusting them. Runs in parallel with Phases 2–3 wherever hours allow — nothing here blocks draft day or Week 1, but signal deployments in-season are gated on results here. Standing methodology for every experiment: strict `as_of` cuts, train on 2021–23 / validate on 2024–25, grade decisions not outcomes.

**Re-sequencing amendment (2026-09-01, operator decision, made while 4.1 was
building):** the phase has two kinds of item and the plan's order conflated
them. **4.1 → 4.2 is the critical path** — 4.1 is the instrument and 4.2 is the
one experiment that changes a number the season is decided by (which of the
≤3 weekly claims to spend priority on, and how early). **4.3–4.5 are deferred
behind more critical work** — not struck, not tied to any date, simply not
next: 4.3/4.4 are ONE experiment (does podcast intel add lift *on top of* the
stat generator?) whose build cost is the largest in the phase and whose
injury/news half is already served at zero cost by the 3.6 ESPN wire + 20-min
alert tick; 4.5 is a cost hedge whose trigger is external ("before pricing
changes force it") and whose labelled set does not exist without 4.3. The
"more critical work" they yield to is **5.1 (playoff posture) and 5.2 (the
learning loop)**, which touch every in-season week and are pulled ahead of
them — see the Phase 5 header. Checkpoint 4 still owns the deploy/retire
decision for every arm; it simply no longer waits on the podcast arm to
convene. Two honest framings recorded with the decision: (a) the largest
lever on the season — the draft — has already been pulled, so 4.2 tunes the
largest lever that *remains*, and in a 10-team league that edge is real but
bounded (replacement level is high; every obvious breakout is claimed by
someone; the edge is choosing the right claim and being first on Wednesday —
exactly precision@k and lead-time); (b) the other half of in-season success
is not making errors, and that is the shipped cadence + sanity checks + 5.2,
not Phase 4. Week 1 is 2026-09-09: 4.1 closes today, 4.2 runs this week, and
nothing else in this phase lands before real games — which is fine, because
the cadence runs Week 1, not Phase 4.

### 4.0 [Fix] Draft-week loose ends — two live-fire defects (added 2026-09-01)
**Origin:** both surfaced during/around the 2026-08-31 live draft; full incident
notes in gitignored `intel/weekly/2026-wk00.md` ("Draft night" sections). Added
as a preliminary Phase-4 item on operator instruction so a fresh session picks
them up from the plan alone. (Other recorded post-draft follow-ups live where
they were logged: the kicker-board `espn_projections` source spec + re-blessed
golden in item 3.11's text, and the `no_control`-vs-`no_effect` add-failure
diagnostic split in runbook §9 + wk00 — they are NOT part of this item.)

**Fix A — sync gate chokes on injury-suffixed names (recurrence certain).**
At live pick 72 the DOM-sync resolution gate refused ESPN's Pick History row
for Josh Jacobs: the row's link text said `Josh Jacobs` but the cell text
parsed as `Josh JacobsDTD` — ESPN renders the injury designation glued to the
name, so the gate's two-name self-check disagreed and it refused (correctly;
the operator/monitor cleared it via "Find him" in ~2 min, feed dammed
meanwhile). Fix: strip trailing injury-designation suffixes (DTD, Q, O, IR,
SSPD, PUP…) from the harvested cell text before the name comparison — at
whichever layer parses the cell (check the sync userscript's harvester vs the
server-side gate in `ziggurat/draft/`); add a regression test with the literal
`Josh JacobsDTD` form. **If the fix touches a userscript: bump its version and
note the Tampermonkey reinstall step** — the installed copy is a snapshot
(runbook §1), and `tests/test_draft_runbook.py` pins the quoted versions.
No in-season consumer (draft/ is draft-only) — the deadline is "before next
draft", but fix it while the incident is fresh.

**Fix B — `game_weather` has NEVER successfully pulled on this box.**
`ziggurat ingest status`: `NEVER PULLED … PartialPull: game_weather failed on
week 1 after storing 0 rows … URLError: <urlopen error _ssl.c:993: The
handshake operation timed out>` (every attempt, e.g. 2026-08-31T23:39Z). It is
context-only but PERISHABLE in forecast mode — each missed day is a lost
observation once games near. Hypothesis to check first: the 3.1b
`net.py` socket bound (3.0 s) may be too tight for Open-Meteo's TLS handshake
from this host; also check IPv6 vs IPv4. **Deadline: before the Wed 2026-09-09
opener** (Week 1 weather context feeds `stream`/`lineup` disclosure).

**Done when:** (A) the suffixed-name form resolves in tests and the gate's
self-check passes on a `NameDTD` fixture; (B) `ziggurat ingest run --source
game_weather` stores real rows and `ingest status` shows it fresh. Suite green.
**Update:**
> **Both fixed 2026-09-01. Suite 2,443 → 2,448 passed, 4 skipped. No
> userscript was touched (no version bump / reinstall needed): both defects
> were server-side.**
>
> **Fix A — one line, but the root cause was ORDER, not just a missing token.**
> `_STATUS_FLAGS` in `ziggurat/draft/sync.py` lacked `DTD`/`PUP`, and the strip
> loop stops at the first flag the text ends with — so even with `DTD` merely
> appended, bare `"D"` would still have matched `"Josh JacobsDTD"` first,
> failed the `_ends_a_real_name` guard on `"…DT"`, and kept the whole suffix.
> The tuple is now longest-first: `("SSPD", "DTD", "PUP", "IR", "NA", "Q",
> "O", "D", "P")`. Regression tests: the literal live form
> `"Josh JacobsDTDGBRB"` (plus suffix+DTD and PUP combos) in the
> `parse_history_cell` table, and an end-to-end replay of live pick 72 on the
> **espn_id rung** — the rung that actually refused (the href was present; the
> cell-vs-anchor self-check is what disagreed) — asserting
> `pick.cell_name == pick.name` and a confident commit.
>
> **Fix B — the plan's hypothesis was wrong twice, and the real defect was
> ours, not the network's.** The `net.py` bound is 60 s (not 3.0 s), and
> Open-Meteo has no AAAA record, so IPv6 was not in play. What the run log
> actually showed (it is richer than `ingest status`): the handshake-timeout
> runs ALTERNATED with runs that reached the API and still `failed` — "wrote
> 16 rows but lost 8/24 (33% — over the 20% ceiling)". Two real defects:
>
> 1. **`pull_game_weather` read schedules with NO snapshot dedup.** The
>    schedules table stores one full snapshot per pull day (as-of design), so
>    the raw `WHERE season=? AND week=?` returned one row per game PER DAY —
>    624 rows for the 16 games of 2026 week 1 by Sept 1, growing daily — and
>    `_build_row` fetches Open-Meteo once per ROW: a burst of ~430 fresh TLS
>    connections where ≤16 would do. Five-minute runs, intermittent handshake
>    timeouts (burst throttling is the likely mechanism; unprovable, but the
>    exposure is gone), and inflated loss denominators. The read now resolves
>    the LATEST snapshot per `game_id` (documented as an operational read —
>    `get_schedule`'s knowable gate would silently exclude playoff games
>    before their bracket is knowable). One fetch per outdoor game, pinned by
>    test. The archive backfill path routes through the same function.
> 2. **The 8 "unstampable" rows were ONE missing venue counted 8 times:**
>    `MEL00` — the Melbourne Cricket Ground, which 2026 schedules introduced
>    on 2026-08-24 for the week-1 SF@LA international game. Not in
>    `_STADIUM_COORDS` (its completeness tests only cover 2020-2025 venues),
>    so the game was dropped on every pull, once per accumulated snapshot day
>    — which is what pushed the loss ratio over the ceiling and failed even
>    the runs that reached the API. Added (`-37.8200, 144.9834,
>    Australia/Melbourne`, open-air); the frozen `_EXPECTED_STADIUM_IDS` test
>    updated; and the drop note now NAMES the missing stadium_id(s) instead
>    of the generic "unresolvable stadium" (the generic message is why this
>    sat undiagnosed for a week).
>
> Live done-when met on this box: `ziggurat ingest run --source game_weather`
> → `ok`, 16 rows in 8.5 s (previously 2–5 min then `failed`); `ingest
> status` reads `fresh`. The Melbourne row is honest: kickoff_local
> `2026-09-11T10:35+10:00`, 64.9 °F, 14.4 mph. Standing lesson: **a run log
> that records loss RATIOS can turn one missing reference row into a nightly
> hard failure when the denominator is silently multiplied — dedup the read,
> and make drop messages name the key they dropped.**

### 4.1 [Build] Backtest harness & decision grading
**Pre-work note (2026-08-27):** three ad-hoc analyses already ran against a
scratch download of the db_fpecr panel (gitignored `data/backtest/`), before
this item's build — see Checkpoint 2's 08-27 follow-up entry and gitignored
`intel/research/draft-backtest-early-findings.md`. Carry two findings into
this build: `dp` preseason pages are clean 2021–2025 for RB/K/DST, and the
panel's in-season ownership (`wp`/`player_owned_espn`) dies after 2023 — the
Sleeper `/research` ownership series is REQUIRED for the 2024–25 holdout.
**Goal:** Replay engine over the historical spine: step week-by-week through past seasons, exercising production code paths; scorecards for lead-time-vs-market (using the 1.2 proxy) and precision@k (k ≤ 3, the realistic claim budget).
**Done when:** a trivial baseline strategy replays through 2023 producing graded weekly decisions.
**Checkpoint-1 amendment (2026-07-20):** the **first deliverable is the historical market-panel ingester** deferred from 1.5 — DynastyProcess `db_fpecr` weekly PPR ECR (`ecr_type='wp'`, with `ecr/best/worst/sd`) into a new panel table read under **`latest_truth`** (immutable accepted bulk history), NFL week inferred from `scrape_date`, edge week dropped, off-cadence scrapes deduped, **our copy pinned/mirrored**; plus the Sleeper `/research` weekly ownership series (frozen snapshots; use w/w deltas). Scorecards: **lead-time-vs-market** (weeks from a Ziggurat flag at T to the ECR re-rank at T+1/T+2, + hit-rate) and **precision@k, k≤3**. The replay steps week-by-week exercising production code paths, all reads through `latest_truth` accessors (a bulk DB reads empty under the default `historical` view — by design).
**Update:**
> **Built, audited & fixed 2026-09-01 (three builders in parallel → 7-lens
> adversarial audit with refute-first verification, 32 agents → two fixers →
> gate). Done-when met and RE-RUNNABLE; five
> seasons replayed under the holdout lock. Suite 2,448 → 2,647 passed, 4
> skipped. Nothing committed yet (this record ships with the build).**
>
> **What was built.** A weekly replay harness under `backtest/` (namespace
> package; imports `ziggurat/` directly, never `draft/` — the t-machinery is
> COPIED from `draft/evaluate.py` and a test pins the two copies equal):
> `stats.py` (ONE implementation of the pooled per-pick t-interval, the
> season-block t-interval df = seasons − 1, and Wilson; `draft_backtest.py`
> now delegates here, its printed numbers unchanged), `decisions.py` (the
> records the two phases hand each other + byte-deterministic JSONL freeze
> with a sha256 manifest + the HOLDOUT lock), `replay.py` (DECIDE: one call to
> PRODUCTION `core.candidates.build_candidates` per week through
> `base.latest_truth` at `as_of(T)` = the first Tuesday STRICTLY after the
> week's last REG gameday, shared by every strategy **[Corrected 2026-09-04
> (C16): in TRAIN exactly ONE week decides 7 days late — 2021 wk15's last REG
> game was a Tuesday, so its clock is 2021-12-28, after every week-16 game and
> sharing week 16's clock; 48 weeks sit at 1 day and 5 at 2, and the grader
> already excludes those 3 picks as `G_REFERENCE_PRECEDES`.]** **[Corrected
> 2026-09-04 (C17): `knowable_as_of` on `weekly_stats`/`snap_counts` is the team
> GAMEDAY, so the replay is a game-date cut over the FINALISED season files —
> 87.8% of TRAIN week-T lines sit inside nflverse's Mon–Wed correction window,
> and the DB already holds two vintages differing on 55 of 94,734 keys, 0 of
> them in TRAIN.]**; `python -m
> backtest.replay`, Rule-3 shaped) and `scorecards.py` (GRADE: pure over the
> frozen decisions at a strictly-later `grade_as_of`, never imports the
> generator). Three strategies over the generator's pool: `signal_topk`,
> `random_k` (seeded by a STRING per week, `PYTHONHASHSEED`-independent) and
> `volume_topk` (most week-T carries+targets — the novice heuristic). Two
> markets from the `db_fpecr` panel (migration 011, which had already landed
> with 3.11 — this item added its `fpecr` registry entry, `interval_days=28`):
> `wp` (weekly positional PPR ECR, drops bye teams) and `ros`. Grading reads
> r0 = the week-T page (scraped the FRIDAY of week T, after Thursday's game —
> so eligibility is decided at `not_after=as_of(T)` and pinned as a
> hypothesis), r1 = T+1, r2 = T+2. Two ingestion deliverables beside it:
> **Sleeper `/research` ownership** (`data/nfl/sleeper_ownership.py`,
> migration `012`, schema 12; 36,855 rows 2021–25, 18/18 weeks every season,
> crosswalk 99.65–100%, raw JSON frozen under gitignored
> `data/backtest/sleeper-research/`; one 2022 wk17 IDP-key anomaly → a 2%
> allowance and that run logged `partial`) and **`weekly_stats` kicking
> columns** (migration `013`, schema 13: 8 nflverse FG/XP columns +
> `kicker_scoring_inputs()` folding onto `score_kicker`'s keys; re-backfill
> 2021–25 = 94,738 rows in 30.65 s; 543 REG-2023 K lines vs the parquet
> supplement, 0 mismatches). Every threshold in the scorecard is a labelled
> hypothesis printed with the card: ELIGIBILITY (RB/WR > 24, TE/QB > 12 on
> r0), HIT = up ≥ 5 places (sensitivities 3/8/10), CORROBORATION = Sleeper
> owned-delta ≥ 10 pts (5/20; 1% censor floor), DEPTH_BANDS
> 36/48/60/80/100/150/>150/unranked, and the generator's own floors
> (`DEFAULT_BREAKOUT` + `EMERGENCE_FLOORS`, in the cache key).
>
> **Done-when, verbatim command, post-fix:** `python -m backtest.replay
> --seasons 2023 --strategy signal_topk --k 3` — fresh **11.2 s** (decide 9.0 s
> for 18 weeks), the SAME command again **2.2 s** ("reusing the freeze under
> …/66c0e83d7da3 … pass --force to re-decide"), JSONL sha256
> `209f8ddf…065ad`. wp: 54 decisions, 45 gradeable (no_reference 6 — the panel
> has no wk1/wk18 page; no_rerank 3; truncated_r2 3), p@1 73.3% [48.0, 89.1]
> n=15, p@3 **73.3% [59.0, 84.0]** n=45 vs base 48.2% (1418/2942): pooled
> +26.2pp [+13.0, +39.4] \*, depth-matched +34.4pp [+21.6, +47.2] \*;
> corroboration 31.1% (14/45) vs 4.4%. ros: p@3 53.3% [39.1, 67.1] vs 26.5%,
> +26.6pp [+11.9, +41.3] \*.
>
> **Five seasons** (`--seasons 2021-2025 --strategy
> signal_topk,random_k,volume_topk --k 3 --unlock-holdout`): fresh **74.5 s**
> (decide 45.7 s for 270 week records; the first draft took 315.9 s), reuse
> 28.7 s; 90/90 weeks decided, cache key `ce3e8d005ec5`. `signal_topk` on wp,
> ALL n=219: p@1 87.7% [78.2, 93.4], p@2 84.2%, p@3 **82.6% [77.1, 87.1]** vs
> base 49.5% (7045/14226) — pooled **+34.0pp [+29.0, +39.0]** \*, season-block
> +33.1pp [+26.7, +39.5] n=5 \*, per-season +35.7/+37.1/+25.1/+30.6/+36.9;
> depth-matched +36.8pp [+31.7, +41.8] \*. **TRAIN 2021–23** p@3 83.0% [75.7,
> 88.4] n=135, +33.6 / block +32.6 [+16.4, +48.9] n=3 \*; **HOLDOUT 2024–25**
> p@3 82.1% [72.6, 88.9] n=84, +34.6 [+26.4, +42.9] \* / block +33.7 [−6.4,
> +73.9] n=2 (~~two seasons cannot exclude zero~~ — printed, not hidden).
> **Corrected 2026-09-04 (C13):** this PAIR does not exclude zero; an n=2 block
> can and does — 4 of the 12 n=2 holdout season-block intervals in this record
> exclude zero, because at df=1 the interval excludes zero whenever the two
> seasons' gap is under 15.7% of their mean. Hits by
> lead: **158 concurrent (lead 1), 12 ~~genuine one-week leads~~ (lead 2), 11
> bye-deferred (unmeasurable), 16 bye at lead 1, 15 truncated (r2 page absent).**
> **Corrected 2026-09-04 (C3/C30):** the two categories are renamed the
> **first-snapshot crossing** (r1) and the **second-snapshot-only crossing**
> (r2) and the word "genuine" is struck everywhere it qualified them — 52 of 54
> TRAIN weeks hold no market observation at all between the week's last game and
> the Tuesday flag, and on the one Tuesday-vintage page that survives (2021 wk4)
> 91 of 150 crossings had ALREADY happened by the flag, so r2 is an upper bound
> on a one-week lead, never a measurement of one.
> HIT sensitivity H=3/5/8/10: p@3 87.7/82.6/75.8/68.9% vs base
> 57.2/49.5/39.7/34.0 — the lift is flat, the levels are not. Corroboration
> 30.1% (66/219) vs 6.0% of null lines. ros (the honest bar, base 30.2%):
> p@3 65.3% [58.8, 71.3], +35.0 [+28.8, +41.3] / block +35.2 [+25.1, +45.3] \*.
> Baselines, wp ALL: `random_k` 58.0% (+9.3 [+3.0, +15.7] / block +8.5 [+5.2,
> +11.8] \* — the POOL's enrichment, i.e. the generator's recall value);
> `volume_topk` 71.2% (+22.6 / block +21.7 [+13.2, +30.2] \*). Ordering signal
> 82.6 > volume 71.2 > random 58.0 > null 49.5 holds on both splits.
>
> **Headline lesson, in two halves. (1) The instrument grades AGREEMENT with
> the market, not a lead over it.** 158 of 181 wp hits are lead 1 — the
> market's first scrape after the event, three days after the Tuesday flag —
> and only 12 (6.6%) are ~~genuine one-week leads~~ second-snapshot-only
> crossings (C3/C30); the first draft's "23 leads"
> was 12 + 11 bye-deferred picks whose lead is unmeasurable (STAT-1). A 49.5%
> base rate says "up 5 places" on a weekly page is easy. On the ONE number the
> panel has that would mean "beat the market" — the raw lead-2 rate — the
> tool shows no lift at all (LEAD-1, measured pre-fix: signal 10.4%, random
> 10.4%, null 11.9%, volume 14.0%; post-split, signal's genuine lead-2 is
> 12/219 = 5.5% against a 9.8% null — a RAW rate that is structurally
> depressed because a lead-1 hit cannot also be a lead-2 hit, which is why the
> CONDITIONAL rate is the open 4.2 item). Every point of the +34pp headline is
> a lead-1 phenomenon. Nothing here says the operator could have claimed
> before the Wednesday batch; it says the generator's Tuesday flag and the
> market's Friday move point the same way ~83% of the time, ~34pp more often
> than chance — which is the plan's own definition of the metric (T+1 IS a
> lead in the Checkpoint-1 wording; the verifier refuted the plan-fidelity
> half of LEAD-1 and confirmed the 4.2-readiness half). **(2) The audit's biggest
> confirmed defect: the null was matched on ELIGIBILITY, not DEPTH (STAT-2),
> and it had already produced a false conclusion.** The hit rule's null
> rises monotonically with r0 depth (1–36: 31.1% … 101–150: 69.9%, unranked
> 70.4%), `volume_topk` picks 146 of 219 inside r0 ≤ 36 (median 33) while
> signal's median is 50, so against ONE pooled null the raw lift REWARDED
> picking deeper. The first draft's "+11pp over the touches heuristic" and
> "on HOLDOUT the gap widens" are STRUCK: depth-matched, signal +36.8 vs
> volume +35.3 ALL, +37.5 vs +32.7 HOLDOUT — indistinguishable at n=84. Every
> card now prints both; the comparison table says the raw lifts are not
> comparable across strategies; **any 4.2 read is on the depth-matched
> column** (a setting that merely pushes the pool toward unranked players
> "improves" the raw one).
>
> **Audit** (7 lenses — leakage/timing, scorecard statistics, Sleeper
> ingester + registry, kicking columns, rules/boundary/test rigor, timer
> safety, plan fidelity + 4.2 readiness — whose finding prefixes are LEAK,
> STAT, SLEEP, KICK, RULES, RIGOR, RULE6, RULE3, OPS, HOLD, SEAM, COST, LEAD,
> PLAN, RERUN, DOC, OPEN; 21 refute-first verifiers, one per major and one
> per batch of five minors): 46 deduped findings,
> **43 confirmed**, 3 refuted (SLEEP-5, RIGOR-5, OPS-2), all confirmed fixed
> except two recorded below; every fix carries a test that fails on the
> mutation the verifier named (six mutants re-run and killed on the final
> tree). The majors: **LEAK-1/HOLD-1** — the amendment's holdout lock did not
> exist; the first five-season run (`110ba96ae988`) read 2024–25 with no flag
> and is recorded as the one pre-ledger holdout read. Now
> `require_holdout_unlock()` at the CLI, `replay()`, `decide_week()`, `load()`
> and `build_scorecard()`; a flagless `--seasons 2024` exits 2 in 0.2 s BEFORE
> the DB opens, naming `TRAIN_SEASONS`; `--unlock-holdout` writes
> `data/backtest/replay/holdout-unlocks.jsonl` publish-then-record (4 rows
> today). **LEAK-2** — the seam's leakage test was vacuous (passed with
> `as_of='2030-01-01'` at the generator call); now a real spy that fails
> with LEAK-5 (`not_after=d.as_of` dropped from the r0 read). **LEAK-3** —
> 2021 wk15 (COVID Tuesday game) decided on the wk16 clock, so its r1 page
> PRECEDED the decision and was graded a hit; now `G_REFERENCE_PRECEDES`,
> ungradeable — 3 picks per strategy per market out (n 222 → 219), the only
> place a headline moved (82.4 → 82.6%). **LEAK-4** — the r0 page is scraped
> the Friday OF week T (304 of 316 pages +1 day, 4 Saturdays), pinned in the
> ELIGIBILITY wording. **RULES-2/SEAM-1** — no threshold injection existed;
> `build_candidates(thresholds=, emergence_floors=)` now, `ReplayParams.generator`
> carries the floors into the cache key (`--breakout-floor` /
> `--emergence-floor METRIC=VALUE`; an unknown name is refused; a non-default
> floor carries `OVERRIDDEN_BREAKOUT_LABEL` in every reason). Two first-draft
> freezes were orphaned by the new key and proven decision-identical.
> **OPS-1/SLEEP-1** — Sleeper's current-week bucket ALIASES THE LIVE BOARD
> (`regular/2026/1`, `/0` and `pre/2026/1..4` returned one body, md5
> `f29e2d2a…`), so an in-season Tuesday pull would have frozen a moving
> number as a point-in-time fact; now `SETTLE_DAYS = 7` (labelled hypothesis,
> "settling" in the registry) with the settling measurement scheduled for
> 2026-09-15 → 09-22. **COST-1** — in item-3.3 PRODUCTION code, not the
> harness: `usage_deltas` read the whole season-to-date `snap_counts` with no
> week bound or player restriction, once per position (23,854 rows at 2023
> wk17, 5,803 of them RB/WR/TE), and the as-of planner is O(week), so a season
> replay was O(W²). Bounded to `week ≤ target` and the `weekly_stats` gsis ids
> (a gsis join, never `snap_counts.position` — a PFR label: 63 RB→FB, 16
> TE→QB, 9 RB→LB, 2 WR→QB in 2023 would have silently become `None`), read
> ONCE in `_usage_arm` and handed over (`stats=`/`snaps=`, the 3.11a `lines=`
> pattern); written atomically because the alert/briefing timers import
> these. `build_candidates` 2023 wk1/5/10/17 **5.74/5.87/5.90/5.90 s →
> 0.23/0.34/0.49/0.73 s**, board digest byte-identical, freeze sha-identical.
> **LEAD-1** (split verdict) — the lead-time metric shipped only as raw
> counts: no per-strategy rate, no interval, no null, no row in the
> comparison block, so the objective 4.2's amendment names ("precision@≤3
> AND lead-time") did not exist as a number; the verifier also found
> `LEAD_LABELS[2]` calling a bye-at-r1 row "a genuine one-week lead" (fixed by
> the STAT-1 split; the label's remaining "genuine one-week lead" wording is
> itself renamed to "second-snapshot-only crossing" — **Corrected 2026-09-04
> (C3/C30)**, above). The hits-by-lead line now prints on every card; **the
> conditional bye-corrected lead-2 RATE as one per-strategy number with its
> null and interval is still open — it is 4.2's, and it is the number 4.2's
> objective needs.** Also: STAT-3 /
> RULE6-2 corroboration re-based over GRADED picks with a crosswalked
> three-way rule (an absence from BOTH Sleeper grids is a real 0.0 delta,
> not imputed); RULE6-1 a wk16 pick graded on one scrape says so ("lead 2
> impossible"); RULE3-1/RERUN-1 the done-when was not re-runnable
> (`FileExistsError` on the freeze dir) — a verifying freeze is reused, a
> non-verifying one refused by name, `--force` re-decides, `--grade-only` on
> an empty cache exits 2 with a sentence; STAT-6 a point interval never earns
> the `*`; PLAN-1 `backtest/README.md` rewritten and every `python -m
> backtest.replay` line in it is parsed by a test. **One defect the fixer
> found outside the audit:** `_grade_market` passed the `MarketSpec` object
> where `grade_one` compared `market.name`, so the "r0 changed under the
> freeze" refusal could never fire. Numbers moved from the first draft ONLY
> via LEAK-3 and STAT-1 (23 "lead 2" rows split into 12 + 11 with sums
> unchanged); everything else is additive columns.
>
> **§7 follow-ups (the plan's 08-27 pre-work list): closed — kicking columns
> persisted (migration 013), the frozen skill fixture extended
> (575,53)→(575,61) plus a 58-row kicker fixture, the `fpecr` registry entry,
> `pull_adp_rankings` documented as NOT a db_fpecr backfill (215 `fp_page`
> collisions). Open, recorded:** retirement of `draft_backtest.kicking_frame`
> / `_NFLVERSE_FG_BUCKETS` / `_kicker_points` in favour of the persisted
> columns (OPEN-1; the parquet supplement still agrees 543/543, so it is
> redundancy, not disagreement); item 3.8's blocked-FG convention; the 4,788
> per-season crosswalk warnings the generator logs (3.3's crosswalk, counted
> on every card, not investigated).
>
> **Deliberately NOT built, and why:** `ff_opportunity` / expected-points
> features (→ 4.2 as its TD-regression source; the harness grades the shipped
> generator, new signals are the generator's business); The Odds API hard-tier
> cross-check (paid; `game_odds` closing lines are context, not a market
> graded here); an sd-units HIT variant (the places rule with sensitivities
> 3/5/8/10 ships instead and the lift is flat across them — a 4.2 option if a
> page's spread ever makes a place count misleading); K/DST grading (the pool
> is the usage arm; the panel has no K/DST `wp` page; QB is wired, untested
> live); a per-position card (n≈45/season is too thin for intervals); a
> layered "feature freeze" that would let a 4.2 setting re-threshold at grade
> time (the freeze carries magnitude + rank + reason text, not per-metric
> deltas — every setting is a NEW decide run, ~27 s on TRAIN); a
> `/v1/state/nfl` belt on the Sleeper pull; and **a replay of the waiver
> claim ORDER** — `core/waiver.py` → `marginal.build_board` is
> projection-priced and 2021–25 has no point-in-time projections (item 1.5),
> so the harness replays ONE production path, `build_candidates`, and grades
> the CONTEXT column of a waiver plan, not the plan's order. Any migration
> beyond 012/013: none — the harness opens the live DB `mode=ro` (a test
> proves `open_ro` cannot write).
>
> **The two 4.2 seams, as they now stand.** (a) The **holdout lock is built**
> (above) — 4.2 tunes on `TRAIN_SEASONS` and reads holdout ONCE with the flag,
> and the ledger shows how many times it did. (b) **Threshold injection is
> built and 4.2's first step is to confirm it**: the knobs are the eight
> `DEFAULT_BREAKOUT` usage-delta floors (carries 6, targets 4, receptions 3,
> target_share 0.08, air_yards_share 0.10, offense_pct 0.20, rushing/receiving
> yards 25) and the four `EMERGENCE_FLOORS` role-emergence floors (carries 10,
> targets 5, receptions 4, offense_pct 0.55) — twelve numbers, all injectable
> (this paragraph originally said "seven" and "eleven"; the enumeration was
> always eight long — corrected under item 4.2, which counted at the source);
> the "beneficiary index" is a same-team-same-position usage-uptick
> MECHANISM, not a floor, and is not a knob today. **Corrected search
> arithmetic:** TRAIN is **54 weeks (3 × 18), not ~85**; per setting ~0.5
> s/week ≈ **27 s post-COST-1** (was ~170 s), so a ~100-setting coordinate
> search is under an hour. The pre-registration rule stands as the amendment
> wrote it, with one addition from this item: the grid AND the metric it is
> read on (depth-matched lift, HIT = 5 places) are written down before the
> first run, because the hit threshold is a hypothesis the optimizer would
> otherwise choose.
>
> **Operational watch item:** the `fpecr` registry entry makes its FIRST pull
> from the Wed 2026-09-02 07:22 PDT timer (~38 MB `db_fpecr-2026-09-01.parquet`
> mirror); until then `ingest status` reads `NEVER PULLED : fpecr`, which is
> correct, not a failure. The Sleeper settling measurement is 09-15 → 09-22
> (§4 item 12 of the design note). Two orphaned first-draft freezes
> (`7eb1e0057dc2`, `110ba96ae988`) remain on disk under gitignored
> `data/backtest/replay/`, identical decision-for-decision — deletable.
>
> Files: `backtest/{stats,decisions,replay,scorecards}.py` (new),
> `backtest/README.md`, `backtest/draft_backtest.py`, `ziggurat/core/candidates.py`
> (injection seam), `ziggurat/data/nfl/{usage,snap_counts,weekly_stats,sleeper_ownership,fpecr,refresh,adp_rankings}.py`,
> migrations `012`/`013`, `tests/test_backtest_{replay,scorecards}.py`,
> `tests/test_nfl_sleeper_ownership.py`. Design, verbatim cards and the audit
> in gitignored `intel/research/backtest-harness-4.1-design.md`. **Standing
> lesson: a null that is not matched on the thing the strategies vary is a
> thumb on the scale for whichever strategy varies it most — and the
> instrument that seemed to show one strategy beating another was measuring
> where each one liked to pick.**

### 4.2 [Experiment] Breakout detection
**Goal:** Tune the 3.3 candidate generator's thresholds on train seasons; measure lead time and precision@k on holdout. Distinguish preseason breakouts (out of scope) from in-season opportunity shocks (the target). Fade detection as a secondary run if results warrant.
**Done when:** findings in `intel/research/breakout-backtest.md`: tuned thresholds, honest holdout numbers, deployed defaults updated.
**Checkpoint-1 amendment (2026-07-20):** tune on the `db_fpecr` lead metric, corroborated by Sleeper ownership deltas; train 2021-23 / hold out 2024-25. **State honestly** that the ECR bar is softer than sharp money and that **K/DST lead grading is weaker** (ECR-only, coarse dispersion). Optional hard-tier cross-check: The Odds API player props for the **2023-05+** window only, reported separately. A projection-driven variant is exploratory (no trustworthy historical stat-line projections — 1.5 decision).
**Amendment (2026-09-01, with the Phase-4 re-sequencing):** this is an
**optimization loop, not model fitting**, and it must be run as the former.
What it tunes is the handful of labelled floors inside the 3.3 generator
(usage-delta thresholds, role-emergence floors, the beneficiary index) — a
coordinate/grid search over ~6–10 knobs against precision@≤3 and lead-time
on the TRAIN seasons, then ONE read of holdout. Run it as a goal-driven
workflow with a **pre-registered parameter grid** written down before the
first evaluation, because an autonomous "tune until ideal" loop fools itself
two ways that are both live here: it leaks holdout into the search, and it
**Goodharts the hit definition** (the hit threshold is itself a labelled
hypothesis in the 4.1 scorecard, so an unsupervised optimizer will find the
setting that makes the *scorer* happy). Two seams 4.1 must provide, and 4.2's
first step is to confirm they exist: a **holdout lock** in the harness
(2024–25 is refused without an explicit, logged unlock flag) and **threshold
injection** into `build_candidates` (the floors are module constants today;
the search cannot re-import the module per setting). Every setting re-runs the
generator over ~85 train weeks, so search cost = settings × per-week generator
runtime — 4.1's recon measured that runtime and it decides whether this is an
afternoon or an overnight. "Ideal" is bounded from above by the base rate the
harness prints and by the soft ECR bar; a tuned setting that beats the null
by less than its season-block interval is reported as no evidence, not as a
small win. **Corrected 2026-09-04 (C26):** this rule was superseded by 4.2's
own G1-G6 gates, and its premise — that the season-block interval is the
conservative one — is false: the block interval is NARROWER than the pooled
interval in 6 of 45 graded cells and on both load-bearing numbers (TRAIN depth
8.29 vs 12.80pp; the winner 7.83 vs 14.92pp, excluding zero where the pooled
interval spans it). Pooled and block are different estimands, not a
liberal/conservative pair, and `stats.py` labels both "miscalibrated under
sparsity — never a gate". Deployed defaults change only at Checkpoint 4, with the holdout
numbers beside them.
**Handover from 4.1 (2026-09-01):** both seams exist — the holdout lock
(`backtest/decisions.py::require_holdout_unlock`, ledger
`data/backtest/replay/holdout-unlocks.jsonl`) and threshold injection
(`build_candidates(thresholds=, emergence_floors=)`, surfaced as
`--breakout-floor` / `--emergence-floor METRIC=VALUE`, floors in the freeze's
cache key) — confirm them first, then pre-register. The knobs are the eight
`DEFAULT_BREAKOUT` floors + four `EMERGENCE_FLOORS` = twelve (the handover
said "seven"; the beneficiary index is a mechanism, not a floor). TRAIN is
**54 weeks**, ~27 s per setting (post
COST-1), so the search is an afternoon. **Read every setting on the
DEPTH-MATCHED lift** — the raw lift rewards picking deeper (4.1 STAT-2). Two
4.1 deferrals are this item's: **`ff_opportunity` is 4.2's TD-regression
source** (backtest-only, `latest_truth`-only, stamped from the nflverse
asset's `updated_at` — see item 3.2c's "leak wearing a valid timestamp"
finding above: ~~there is NO in-season file, so it can only ever grade, never
run live~~; it is not ingested today. **Corrected 2026-09-04 (C28):** the file
IS published in-season and CAN run live; what it cannot do is be replayed,
because each season's asset is overwritten in place with no point-in-time
archive) and the conditional bye-corrected lead-2 RATE as one per-strategy
number with its null (4.1 LEAD-1, partially closed).
**Update:**
> **Done 2026-09-03 — verdict, in the pre-registered words: "no setting
> earned a holdout read."** Run as three Opus workflows (build `wf_76dc343d-e54`
> → the search itself, orchestrator-run → analysis `wf_047bdba1-947`, 19
> agents, 4 headline claims each surviving 3/3 refute-first verifiers, 0
> protocol violations). The deployed floors are byte-unchanged; the holdout
> ledger is **4 rows before and after**; `TUNED` in the pre-registration is
> blank. Findings, all tables and every gate number in gitignored
> `intel/research/breakout-backtest.md` (frozen prefix 2,333 lines, sha256
> `41fad648c1de27e3…`, never edited; results as dated amendments F4-1…F4-3 at
> the end) with the analysis artefacts beside the data under
> `data/backtest/replay-4.2/analysis/`.
>
> **What was pre-registered, then run.** A coordinate search over the **twelve**
> labelled floors (8 `DEFAULT_BREAKOUT` + 4 `EMERGENCE_FLOORS` — the plan said
> eleven; counted at the source and corrected inline above) on a 40-cell
> label-based family (5 levels × 8 axes, with 4 `emergence_scale` and 5
> `global_scale` cells, 3 recall floods, a scale-only control, LOO and a
> forward/reverse round 2), read on `D(g) = M(g) − M(default)` where `M` is the
> depth-matched lift of `signal_topk` p@3 at HIT = 5 places, paired per week
> over the 45 common weeks of TRAIN 2021–23, on a frozen DB snapshot
> (`data/backtest/ziggurat-4.2-snapshot.sqlite`; panel fingerprint and preflight/
> postflight digest `0a98eff6…` equal on both sides, three ways). Six gates
> stood between any winner and the ONE ledgered holdout read: G1 `D ≥ +5.4pp`
> (**Corrected 2026-09-04 (C22):** that constant is the rounded half-width of
> the argmax-INELIGIBLE FLOOD-1 probe's own paired interval — 2.0154 × one
> cell's SE — not a power calculation and not a decision-utility bar; the
> measured family max-null bar sits 1.8pp above it, and 15.3% of sign-flip
> replicates clear it on noise alone);
> **G2 a max-null step-down over the whole family (B=10,000)**
> (**Corrected 2026-09-04 (C18):** the symbol keeps its frozen pre-registration
> name; the procedure is SINGLE-STEP — only the winner is ever tested, against
> one bar — and the bar is exact only under exchangeability of the per-week
> signs); G3 the HARD
> admissibility screen; G4 3-of-3 per-season sign; G5 leave-one-out; G6 sign at
> HIT = 3 and 8. `backtest/tune.py` refuses the live DB and the canonical cache
> dir by name, has no holdout flag at all, and raises on a holdout-season
> fingerprint. Build: 6-lens audit, 34 confirmed defects fixed (the max-null
> family had been SCREEN-filtered where §5.3 says LABEL-based; the fingerprint
> recipe did not match Appendix A; a fixture test had been appending rows to the
> canonical grade log on every suite run — `tests/conftest.py` now redirects
> both modules' cache default for every test, pinned).
>
> **The result.** 47 of the 80-setting cap, ≈7 min elapsed in an 8-way pool
> (34.4 s per fresh cell; the pre-registered 31.26 s was serial). Winner
> `carries=1` (shipped 6): **D = +0.05408, clearing G1 by 8e-05 — 1/95th of one
> hit's quantum** (**Corrected 2026-09-04 (C6):** 1/93.03 — see F4-4(b) line
> 2476) **— and FAILING G2**: the family-wise null-max bar is 0.07206
> (adjusted p 0.153, `clearing: []`; 15.3% of ~~pure-noise~~ replicates exceed
> G1's floor on their own — **Corrected 2026-09-04 (C23): 15.3% ± 0.36pp (MC
> SE at B=10,000) of SIGN-FLIP replicates produce a FAMILY MAXIMUM above the
> floor; the per-CELL exceedance is 0.66% on average and 7.3% for the winner's
> own cell, a 23× difference**). G3 passes with the A9 (depth) and A12 (position-mix)
> flags raised; G4 passes; G5 vacuous (single axis); G6 passes as a sign gate
> only (H=3 +2.1pp, H=8 +5.2pp with 2023 NEGATIVE and p > 0.05 at every H).
> Round 2 was empty — 0 axes survived the single-cell p < 0.05 entry rule, 8
> skipped. **Corrected 2026-09-04 (C19/C24):** on 14 of the 45 graded cells
> that rule was arithmetically unopenable — with `m` non-zero paired weekly
> differences the exact one-sided p cannot fall below 2^-m, so m ≤ 4 floors it
> at 0.0625 (all three `receptions` levels sit there) — and NO multi-knob
> combination cell was ever built or graded (`round2.json`: `candidates {}`,
> `forward_attempts 0`, 0 cells setting two axes independently), so two-knob
> nesting is UNTESTED, not tested and failed. FLOOD-1 has the largest D on the page (+6.24pp, pool 187) and is
> argmax-ineligible by construction — **§13.1 predicted every clause of this
> outcome before the first cell ran.** The 40-cell D histogram: median −0.3pp,
> 27 of 40 negative, 2 reach G1, 0 reach G2. §7.3 LOSO: three folds, three
> different winners (`gs=2.0` / `rushing_yards=5` / `carries=1`), LOSO mean
> +1.2pp vs TRAIN +5.4pp — a **+4.2pp winner's curse, all of it selection
> switching** (forcing every fold to `carries=1` reproduces TRAIN exactly).
> **Corrected 2026-09-04 (C21):** that parenthetical is an arithmetic IDENTITY,
> not a measurement — the three folds are 15 weeks each, so a forced-LOSO mean
> IS the pooled `D` (exact `Fraction` equality; max gap 6.9e-18 across all 40
> cells) — and the +4.2pp curse itself carries a 95% CI of [−0.007, +0.091],
> i.e. it does not exclude zero. The runner-up is 1.26 tie-bands away and would win
> the closest-to-shipped tie-break.
>
> **What the +5.4pp is made of** (the decomposition the pre-registration
> demanded): `D = D_hit − Δbar` splits **54.8% more hits / 45.2% lower null bar**
> — near half is the winner picking shallower, not better. `rho(D, pool)` over
> the grid is −0.10; the ~~pure pool lever~~ **uniform-scale family** is −0.83
> (n=6): "recall, not thresholds" is NOT the read, and neither is its opposite.
> **Corrected 2026-09-04 (C7):** those six cells are not a pure pool lever —
> 23-26% of the control's shared rows are not a 1/c rescale at all, and
> `global_scale=2.0`, which is strictly TIGHTER, still changes 40 of 54 top-3
> sets — so the family varies WHICH rows enter as well as how many. At the pick level
> the winner swaps 68 RBs in for 61 WRs + 7 TEs out (winner-only hit rate 81.4%
> vs default-only 75.7%, backup/committee backs), with a Simpson's reversal
> under position standardisation. **Corrected 2026-09-04 (C8):** it is a mix
> change AND a heterogeneous within-position quality change, not a pure mix
> shift — a pure mix change makes BOTH standardised differences identically
> 0.000000, while the measured pair is −0.0416 / +0.0580, with RB −11.3pp and
> WR +10.3pp within position. The strata are thin (Fisher RB p 0.29, WR 0.15,
> TE 1.00), so neither standardised number is "the" answer. And the §9 secondaries say what "better"
> means here: **~~genuine one-week leads~~ second-snapshot-only crossings FALL
> 10 → 7 (wp) and 20 → 17 (ros)**, the entire +4 hit gain is concurrent or
> bye-deferred, the conditional lead-2 denominator collapses 29 → 19, and
> Sleeper corroboration at Δ20 rises 1.74× — `carries=1` agrees with the market
> sooner and ~~beats it less~~. **Corrected 2026-09-04 (C9/C25):** "beats it
> less" is struck — the conditional RATE RISES in both markets (34.5 → 36.8% wp,
> 28.6 → 31.5% ros) and no test distinguishes any of it from noise (slot-paired
> exact McNemar p 0.581 / 0.664; Fisher on the counts 0.618 / 0.724), while the
> denominator falls for a post-treatment reason (−4 extra first-snapshot hits,
> −6 extra bye-at-r1) rather than an economic one. **Also struck: "loses the
> default's only interval that excluded zero"** — the default has three, and the
> winner KEEPS the `ros` conditional depth interval at a higher +18.4pp
> [+6.4, +30.4]. The §9.8 pool-invariant
> read cannot rank settings (its correlation with pool size is carried entirely
> by the random arm getting worse).
>
> **Disclosures owed regardless of outcome, now on record (F4-3):** the
> twelve floors have exactly four consumers (`ziggurat candidates`, waiver
> CONTEXT bullets, the briefing SIGNALS block, the backtest); the phone teaser
> and the 20-min alert tick are NOT consumers; only the usage arm is graded,
> and a floor change alters injury-arm reason text the grade never sees; a
> deploy ~~orphans 50 of 51 freeze keys incl. both 4.1 holdout freezes (whose
> re-creation is a ledger event) and fails six pinned literals in four tests~~;
> **Corrected 2026-09-04 (C14): nothing is orphaned.** Cache keys are hashed
> from parameter VALUES, so a simulated adoption of `carries=1` moves 0 of the
> 44 round-1 grid keys, 44/44 still verify `ok`, the paired reference
> `28007210abc5` still loads its 162 week records, and one flag
> (`--breakout-floor carries=6`) re-addresses both item-4.1 reference keys
> exactly; what actually moves is FIVE key-literal sites (two distinct keys)
> plus ~14 shipped-anchor assertions;
> the null universe is read at the grade clock and the pool at the decision
> clock (PF-21, identical for every setting). Harness defects found by the
> analysis, recorded not fixed: `sign_flip_permutation` mis-resolves exact
> ties (exact-mean observed vs float-summed null — the control's `p_two` logs
> 1e-04 where the exact value is 0.0625; no gate or ranking touched a tied
> pattern); `band_table` duplicates its `unranked` row; decide-only trials lack
> `wall_seconds`; G6 cards' internal counts are not persisted.
>
> **Not done, deliberately:** no holdout read (unearned); no second search under
> a different metric, α, family or weighting from this data (that is the
> post-hoc search the freeze exists to prevent); `ff_opportunity`
> (TD-regression source) stays DEFERRED with its recon artefact in the note's
> §12; K/DST grading and the Odds-API cross-check remain 4.1's deferrals. **If
> Phase 4 revisits the floors it does so under a NEW pre-registration and,
> preferably, a new source** — `ff_opportunity`, or 2026's own weeks once they
> exist. Deployed defaults are unchanged at Checkpoint 4 by this item's own rule.
>
> Files: `backtest/{tune,tune_grid}.py` (new), `backtest/{stats,decisions,
> replay,scorecards}.py`, `backtest/README.md`, `tests/conftest.py`,
> `tests/test_backtest_{stats,tune,tune_grid}.py` (new),
> `tests/test_backtest_{replay,scorecards}.py`. `ziggurat/core/candidates.py`
> untouched. Suite green (**2,854 passed, 4 skipped**; +111 over the 3.8A
> baseline). **Standing lesson: a search that pre-registers its own null
> distribution finds out what its instrument can see — here, nothing smaller
> than ~7pp on 45 weeks** [**Corrected 2026-09-04 (C20):** that ~7pp is the
> SELECTION bar (the family max-null p95), not an 80%-power MDE. At
> sd(d_w) = 0.2483 and n = 45 the SE is 3.70pp, so 80% power needs +9.2pp
> against a single-cell bar or +10.3pp against the family bar, and detecting a
> true +5.4pp at 80% needs ≈131 paired weeks — n = 100 delivers ~70%] **— and a
> winner that clears the practical floor by
> 8e-05 while sitting 1.8pp under the noise maximum is the noise maximum
> wearing a label.** The item's value is the number it did NOT change.

### 4.2a [Triage] External review of 4.1/4.2 — corrections, suggestions, decisions (added 2026-09-04)
**Goal:** Two independent external reviews (GPT and GPT Pro, both 2026-09-04,
both report-based — neither ran the code or opened an artefact) of the 4.1/4.2
program answered the brief `intel/research/breakout-review-brief-2026-09-04.md`.
Work through them without re-deriving anything: verify every claimed
correction against the artefacts (two refute-first verifiers per claim, an
artefact lens and an independent-recomputation lens, a third on
disagreement), bin every suggestion by what it NEEDS (presentation / new
instrument / new generator / start-recording-in-2026 / strategy rework),
decide PURSUE / FOLD / DEFER / REJECT with the reason written down, and hand
the whole record back out for a second round. The working ledger is
`intel/research/external-review-tracker-2026-09.md` (gitignored, written
scrubbed so it can be handed over unedited); this item OWNS that file. It is
a triage item: anything pursued becomes its own plan item (4.2b, 4.2c, later
4.2d) and this item never carries a build.
**Done when:** every correction row in the tracker has a cited verdict; every
suggestion has a decision and, if pursued, a plan item; the brief carries a
dated errata section (never silently edited); the lead-category rename
("first-snapshot crossing" / "second-snapshot-only crossing") and the holdout
relabel ("previously inspected external evaluation set") are applied in cards,
notes, CLAUDE.md and this plan; and the handoff package (tracker §6) has gone
back to the reviewers.
**Standing constraints (from the tracker header):** floors unchanged; 2024–25
locked AND relabelled — the shipped floors were chosen against five
hand-verified 2025 breakouts, so those seasons were never pristine; no second
search on 2021–23 under the `wp`-hit metric; nothing rebuilds the generator
during Week 1.
**Update:**
> **Interim, 2026-09-04 — corrections verified.** Both reviews are on disk
> (`intel/research/GPT-*.md`); the tracker is written and binned; 29 claimed
> corrections (C1, C3–C30; C2 resolved by inspection) went through 65 Opus
> agents — two refute-first lenses each (artefact / recomputation), a tiebreak
> on the six splits, one synthesis: **17 CONFIRMED · 11 PARTLY · 1 REFUTED
> (C29, the ESPN-transactions claim — the brief stands) · 0 JUDGMENT.** Record:
> `intel/research/external-review-verdicts-2026-09-04.md` (+ `.json`). The
> brief carries an appended dated ERRATA section (body untouched); the plan
> §4.1/§4.2 Update blocks, the CLAUDE.md bullets and the code-facing text
> (card labels, docstrings, `tune.py` printing m beside p) are applied as one
> small gated build. Headline corrections: 2024–25 is a *previously inspected
> external evaluation set* (all twelve floors carry 2025 provenance; the
> unlock ledger evidences completed `backtest/` reads only). **Corrected
> 2026-09-04 (C1):** the lock exists SOLELY in `backtest/` — `ziggurat
> candidates --season 2025 --validate` is an ungated 2025 read path — and the
> ledger is written AFTER a run completes, so a spent-but-unlogged read is
> possible; at least three further holdout reads are named in the artefacts
> (the pre-lock `110ba96ae988` freeze, item 3.3's 2025 five-target + control
> validation, and 4.1's COST-1 2025 re-check); the lead labels
> become first-snapshot / second-snapshot-only crossings (52 of 54 TRAIN weeks
> hold no market observation at the flag); 80 % power needs +9.2–10.3pp or
> ≈131 weeks (the "≈100" was the 70 % figure); the round-2 rule was
> arithmetically unopenable on 14 of 45 cells (m ≤ 4 ⇒ p ≥ 2^-m); the tie bug
> moves 10 cells (largest 0.0141, none across α) and the control's true
> two-sided p is 0.0625, not 1e-4; `weekly_stats` already holds two vintages
> differing on 55 keys; `ff_opportunity` IS published in-season (game-day
> cron, pinned 2006–20 models) and becomes a Week-1 CAPTURE in 4.2b; nothing
> is orphaned by a default change (value-hashed keys). Holdout: ledger 4 → 4;
> one disclosed near-miss (two verifiers globbed holdout freeze MANIFESTS —
> parameters and digests only, no outcomes). Remaining for this item: the
> errata build, then the handoff package after the first live weeks.
> **Errata build landed the same evening** (6 agents: build ×2 → audit ×2 →
> fix → gate): 57 entries applied, 9 audit findings fixed (1 major — the
> tune summary rows lacked m beside p), **suite 2,856 passed / 4 skipped**
> (+2 pins), frozen pre-registration prefix byte-unchanged with F4-4 appended,
> `repo_guard` clean; uncommitted pending the operator. Remaining for this
> item: the handoff package only.

### 4.2b [Build] Decision-time archive & usage-evidence presentation (added 2026-09-04; deadline Tue 2026-09-15)
**Goal:** The reviewers' unanimous #1, and the one item with a hard date: the
richest waiver weeks are the first three and a missed Tuesday cannot be
reconstructed. ONE per-Tuesday FREEZE that ties together what already
accumulates (daily whole-universe `league_player_state` = the FA pool as a
positive fact; `league_transactions` with ESPN timestamps — 32 rows by
2026-09-04, so "who claimed whom, when" is already recorded forward; daily
Sleeper projections = a same-week forecast at several days; weekly `fpecr`
Friday pages; the journal) with what does not yet exist: the candidate board
with features for EVERY evaluated row (false negatives must be studyable), the
printed claim chain and its projection-input hash, the projection-only chain
beside the page the operator saw, the operator's submitted claims plus any
departure and its stated reason, and a Tuesday pull of `weekly_stats` kept
beside Thursday's for the same week — the data-vintage diff: the historical
replay is a game-date cut over FINALISED values (`weekly_stats.knowable_as_of`
is the gameday; nflverse corrections land Mon–Wed), and only a forward
measurement says whether that ever moves a pick. Episode-aware NEW / REPEAT
marking per flagged player. Recon question: can same-week `wp` ECR be captured
on Mon/Tue/Wed from the free page — if not, Sleeper projections are the
same-week forecast we have. Presentation: the candidates column on `waivers`
and the briefing SIGNALS block are relabelled "USAGE / ROLE EVIDENCE — does
not change the claim order" (never "the market will agree by Friday"), and the
column is never auto-promoted to a tie-break before a shadow comparison.
Also carries the two integrity fixes both reviewers rank ahead of any new
experiment: the `stats.sign_flip_permutation` tie bug (exact enumeration for
small m, identical observed/null statistic computation, regression fixtures
for m = 4, m = 5, tied statistics, all-zero and repeated-zero weeks), after
which the FROZEN 4.2 analysis is re-run with corrected arithmetic — an
implementation correction, not a new search; both outputs preserved — and a
crosswalk-coverage measurement weighted by decision impact (unresolved joins
among eligible / selected / near-cutoff players; pick changes under corrected
joins). And one query the frozen data enables: the acquisition-latency
hypothesis — the batch runs 00:01–01:13 PT and the briefing lands 06:00; are
valuable free agents taken inside that window? A positive answer is a CADENCE
change, not a threshold change.
**Amendment (2026-09-04, after the corrections verification):** three
additions from the verdicts. (a) **S10 capture** — `ff_opportunity` publishes
the in-progress season's `ep_weekly_<season>.parquet` after every game window
(C28: 91 successful game-day runs 2025-09..2026-02; the 2025 file was rewritten
2026-09-01 AND 2026-09-04) and the models are a pinned 2006–2020 fit, so it is a
MUTABLE current-value source whose observations are lost if not captured — the
same class as `espn_ranks`. Pull it on the ingest cadence from Week 1 with its
`updated_at` recorded, PERISHABLE, `latest_truth`-only for any backtest; no
integration and no R port (that stays deferred). (b) **S2 fixtures** — the tie
bug moves 10 of 45 cells (C5) and can breach the exact 2^-m floor downward
(C19, ~10 % of all-favourable m = 4 vectors): fixtures must include the
control's own 5-informative-week all-negative vector, an all-favourable m = 4
vector, and an exactness assertion against full enumeration. (c) **S26
watch** — re-check for an `injuries_2026` upstream file after Week 1 (none
exists at 2026-09-04; the 2025 file was a one-shot post-season backfill).
**Done when:** the first live Tuesday (2026-09-15) is captured end-to-end and
reconstructible; completeness targets are stated IN the item before that
Tuesday (every submitted claim; ≥ 95 % of evaluated candidates); the
presentation change is live and pinned by test; the tie bug is fixed with
exact-case fixtures and the re-run 4.2 gate table is recorded (verdict expected
unchanged — a changed verdict is a finding, not a promotion); the crosswalk
measurement is written down with its decision-impact number. After three live
weeks the archive must let every flagged player be classified as one of
{useful & acquired, already priced in, already rostered, claim lost, surfaced
too late} — if flagged players are routinely gone before the system can act,
the operational-lead hypothesis is refuted whatever the Friday rank precision
says.
**Update:**
> **Interim, 2026-09-04 — recon done, design proposed.** Nine read-only
> scouts + synthesis wrote `intel/research/decision-archive-4.2b-recon.md`:
> the freeze is a sha256-manifested JSONL payload under gitignored
> `data/decisions/<season>/wk<NN>/<capture_id>/` plus ONE run-log table
> (`decision_freezes`, migration 018), fired always-on inside every `waivers`
> run AND by a Tue 18:30 PT timer, through passive collector seams on
> `build_waiver_plan` / `build_candidates` (no import cycle; Rule 3 kept);
> the enriched and projection-only chains are IDENTICAL by construction
> today and the freeze asserts it; every evaluated candidate row (30 fields)
> is emitted from the table `_usage_arm` already holds; NEW/REPEAT is an
> episode rule (gap > 2 EVALUATED weeks, labelled hypothesis) with a Week-1
> tag because Week 1 is the all-emergence regime; `ff_opportunity` is
> fetched by URL into a lean 25-column table (015) plus a lossless parquet
> mirror; same-week ECR is DynastyProcess `fp_latest_weekly` daily (016) with
> one FantasyPros page per Tuesday as the week-label authority; Tue/Thu
> vintages are two forced systemd units at 08:00; `acquisition_at`
> (millisecond) + `related_transaction_id` land in 017. **Schedule: every
> core/registry unit must land by Mon 2026-09-08 (Week-1 rule + 09-15
> deadline).** Two operational findings: the Week-1 stat-anchor trap (Tue
> 09-15 could price an opener-only vintage with `ingest status` reading
> `fresh`) — B6 fixes it; and the 4.1 design note's Sleeper settling
> measurement targets a URL that 404s. Ten operator decisions are listed in
> the brief; the build proceeds on the recommendations in an isolated
> worktree against a scratch DB copy, and NOTHING touches the production
> tree, its DB or the timers until the operator approves the merge.
> **Wave 1 BUILT and gated, 2026-09-05** — in the isolated worktree
> (`wt/4.2b-decision-archive`, base `a37ab3d`), 13 Opus agents: six unit
> builders in three lanes, one integration agent, four refute-first
> auditors (rules/boundaries, design conformance, tests/determinism with 19
> mutation experiments, operations/Rule 6 run live against the scratch DB),
> one fixer, one gate. **8 commits, 48 files, +11,416 / −86; full suite
> 3,106 passed / 5 skipped** (base 2,856 / 4; the extra skip is an
> environment gate, none of the 4.2b tests skip); ruff clean on every touched
> file; `repo_guard` clean over 48 paths; production tree, DB and timers
> untouched (proven by the gate). What landed: `ziggurat/decisions/`
> ({capture,store,read}.py, 1,949 lines) + `ziggurat decisions
> {freeze,status,verify}` + always-on capture inside `ziggurat waivers`
> (+0.15 s on 24.3 s, ~450 KB/capture) + the Tue 18:30 unit and
> `scripts/install-decisions.sh` (NOT installed); collector seams
> `WaiverArtifacts` / `EvaluatedRows` (35 fields per evaluated row, pinned by
> IDENTITY against the shipped board on 2025 wk1/5/9/18 = 158/108/91/134;
> collector=None byte-identical, measured on the real 37,071-byte page) and a
> new Rule-1 accessor `base.resolved_vintage`; the episode rule
> (`episode_tag_for`: NEW / REPEAT / FIRST SEEN / WEEK 1, gap > 2 EVALUATED
> weeks, labelled hypothesis with its 2025 provenance) + the seven
> presentation strings + the journal block in `templates/`; `ff_opportunity`
> (`ffopp_weekly`, migration 015, gameday-stamped 100 %, parquet mirror,
> `OpportunityCollapse`, `model_version` change raises, `mirror_only()` for
> 2021–25 as an operator step); `fp_weekly_ecr` (migration 016, DynastyProcess
> twice-daily file, `week_basis` provenance, the FantasyPros page opt-in via
> `ZIGGURAT_FP_WEEK_PAGE=1`, default OFF); the Tue/Thu vintage units
> (`ziggurat-nfl-ingest-vintage-{tue,thu}`, decide()-across-a-week simulated
> both ways) + `acquisition_at` (ms) and `related_transaction_id` (migration
> 017); `decision_freezes` run log (018). Migrations 015–018 pinned; schema
> 14 → 18 on the scratch copy. Audit: **37 findings (1 critical, 9 major),
> 36 fixed, 1 refuted** — the critical was two temporary source mutations a
> concurrent lens had not restored (restored, tree clean); the fixer's
> record follows. **NOT merged**: awaiting the operator's D1/D2/D4 answers
> and the merge itself. Open, carried: `decision_freezes.candidate_week`
> needs migration 019 (Wave 2); `core/briefing.py` history wiring goes
> through `push/run.py` (this session); the LIVE journal template
> (gitignored, never overwritten by `scaffold`) must be hand-synced; the
> `mirror_only` step for 2021–25; the two installers; D2(b)'s env flag.
> **The intermediate commits are not individually green by construction**
> (the migration pins land at the integration commit); HEAD is what was
> measured.
>
> Audit-fix round, 2026-09-05 (commit `0c222a5`). 36 of 37 findings applied,
> one refused; no recommendation, gain or ordering moves. **The headline is
> that the freeze archived the usage arm and nothing else** — the
> INJURY_SHOCK and QB1_CHANGE arms survived only as the integer
> `board_rows`, so their reason text and their `player_key` (the episode key
> the whole NEW/REPEAT rule is built on) died with the process on a Tuesday
> that cannot be re-taken. `board.jsonl` now holds them, and with it the
> reader that makes the badge a comparison: `read.week_flags_history` walks
> the archive, verifies each manifest and rebuilds `WeekFlags` through
> `candidates.week_flags` itself, and the `waivers` / `candidates` /
> `decisions freeze` CLI bodies pass it as `history=`. The badge had shipped
> INERT and SILENT: `history is None` was the one branch of three that
> produced no note, and it was the branch production took, under a legend
> explaining NEW and REPEAT above rows that all read FIRST SEEN.
> Second: the two market probes reported a market the run could not read.
> They gated `knowable_as_of` only — no retrieval gate, no per-key
> resolution — beside a printed `"view"`. Measured on the live database:
> 3,516 `fpecr_panel` wp rows for 2023 at as_of 2023-11-01 against ZERO the
> historical view can serve, and a five-row board pulled on three days
> counted as fifteen. Both now go through the source's own as-of accessor,
> and `market.json` records the day's fp_weekly BOARD rather than its
> cardinality, which is what the design decided. Third: a failed Tuesday
> left no fact — every pre-plan failure (expired ESPN cookies above all)
> exited before `start_capture`, so `decisions status` showed nothing at all
> while the unit file asserted a failure is visible three ways. The run-log
> row is now written before the plan, and `capture.record_prerun_failure`
> covers the credential path the CLI resolves earlier still.
> Recorded deviations from the design brief: (a) it said the run log carries
> no as-of columns; `plan_as_of` is the run PARAMETER — the gate the plan ran
> at — kept so `decisions status` can say which gate a capture ran at, and
> asserted never to reach `select_as_of`. (b) `trigger` is a THREE-value
> vocabulary, `waivers` | `cli` | `timer`; the brief named two — `waivers` is
> the always-on capture inside `ziggurat waivers`, the dominant path.
> (c) `crosswalk_vintage` stays an UNGATED `MAX(retrieved_as_of)` and that is
> correct: the crosswalk accessors read `players` at-now with no as-of gate
> by design, so a gated number would record a vintage the run did NOT use —
> the audit finding asking to gate it was examined and refuted. (d)
> `ff_opportunity.mirror_only(season, path)` fetches a past season's asset
> and ingests nothing, so 4.2c gets `ep_weekly_2021..2025` without anyone
> relaxing `BACKFILL_EXCLUDED`; no CLI or registry entry by design.
> **Standing lesson this round paid for: a boundary guard that inspects the
> wrong attribute is indistinguishable from one that works.**
> `test_nothing_in_the_package_imports_backtest` read `getattr(n, "module",
> "")` on every node — and an `ast.Import` has no `.module`, so a plain
> `import backtest.decisions` evaluated to `""` and the guard only ever
> caught the `from backtest ... import ...` shape, while a correct scanner
> sat ten lines above it in the same file. Four mechanisms in this wave had
> no test that could distinguish them from their absence; three are now
> mutation-verified (the freeze timer's ExecStart, that import guard, and
> the chain's order-inertness — stated structurally, because a TIE-BREAK
> promotion fires only on exactly equal gains and passed both behavioural
> pins).
> **Merged and installed 2026-09-05 (operator: yes to all four decisions).**
> Fast-forward of `wt/4.2b-decision-archive` onto main at `ed8abc8` after
> the merge gate (3,108 passed / 5 skipped in the worktree); main's 15
> uncommitted errata copies were byte-identical to the branch's first commit
> and were discarded, not lost. Migrations 015–018 applied on the live DB by
> the first command after the merge (`meta.schema_version` 14 → 18; backup
> `db/ziggurat.sqlite.bak-v14-pre-4.2b` taken first). Installed:
> `scripts/install-nfl-ingest.sh` (now five units; the Tue/Thu vintage pair
> first fires Tue 09-08 08:00 / Thu 09-10 08:00) and
> `scripts/install-decisions.sh` (Tue 18:30; first fires 09-08). D2b:
> `ZIGGURAT_FP_WEEK_PAGE=1` in `.env`. The gitignored journal template was
> overwritten from `templates/` (verified identical to the OLD committed copy
> first). `ff_opportunity.mirror_only` run for 2021–2025 into `data/ffopp/`
> (~1.1 MB each; 2025's asset is frozen upstream). Production smoke: `decisions
> status` refused to call an empty log healthy; `decisions freeze` produced
> the first real capture in 25 s (`partial`, candidate half ABSENT with the
> NoCompletedWeek reason — correct before Week 1); `decisions verify` matched
> every file's sha256; `brief run --no-push --no-llm` composed. Worktree
> removed; the branch ref is kept (merged). Not pushed.
> **Remaining for this item:** the first live Tuesday (2026-09-15) captured
> end-to-end with the candidate half present; the three-week five-way
> classification; Wave 2.

### 4.2c [Experiment] Realised-points instrument — pre-registered (added 2026-09-04; opens 2026-09-15)
**Goal:** Replace market-rank movement as the PRIMARY objective with what the
waiver decision cashes in: realised house-scoring points. Per the reviewers'
shared spec: four CALENDAR weeks after the flag (byes and confirmed
non-participation score zero; missing data does not; horizon-truncated
decisions excluded by a rule fixed beforehand; negatives kept — no oracle
option to keep the good weeks), house scoring via `core/scoring.py` (Rule 2,
touchdowns retained), against a frozen benchmark basket B(p,T) chosen with
Tuesday-available information only. Two definitions are on the table — top-3
by week-T carries+targets at the position within the eligible universe (R1);
top-3 highest-ranked eligible players on the reference page (R2) — and the
pre-registration picks one as primary and the other as a secondary WITHOUT
looking at results. Three strategies on a COMMON eligible universe, not only
inside the signal's own gate: the shipped signal, volume, and market-only
(best available by r0) — the last answers "does Tuesday usage add information
beyond the observed Friday rank?", which is the question the ECR panel CAN
answer (it cannot measure a lead at weekly resolution). Role persistence over
the next two team games is a secondary. The four-week window is justified
against the pricer's rest-of-season horizon, not assumed. Method requirements
written in BEFORE the first grade: a studentised paired statistic with
dependence-aware block resampling and block-length sensitivity (weeks are not
independent — repeated players, overlapping windows); a practical floor stated
in house points from decision utility, not from an interval width
(**Amended 2026-09-04 (C22):** name the UNIT explicitly — extra correct claims
per season, or house points — pin it BEFORE any probe, and print the
detectable-effect bar beside it as a SEPARATE row; 4.2's whole +5.41pp
translates to 4 net extra correct picks in 135 slots, ~1.3 per season, 45% of
which is a lower null bar rather than extra hits); the
informative-week count m and its 2^-m p-floor printed per cell (4.2's round-2
rule could not open at p < 0.05 on any cell with m ≤ 4, whatever the
direction); a selection-aware family rule if any search is run. TRAIN 2021–23
only — grading the SHIPPED default under a new objective is a measurement, not
a search. 2024–25 stay locked.
**Done when:** the pre-registration is frozen and sha256-pinned before the
first grade; scorecards for the three strategies on TRAIN exist under the new
objective; the ECR-hit instrument is retained as a SECONDARY with the lead
categories renamed; the challenger seam for 4.2d (the explainable role-state
challenger — admission / role estimation / presentation separated, trailing
three-game baseline, an active-game record that distinguishes zero involvement
from inactive from bye from missing) is defined so it can be shadow-run on
2026 without a second search.
**Update:**
> _[To be completed]_

### 4.3 [Build] Podcast pipeline
**Goal:** RSS archive harvest for a chosen pod slate (must have existed 2021–2025 and still publish), local Whisper with vocabulary biasing + phonetic entity resolution against the player table, claim extraction to the SPEC schema via the routing interface, claim-resolution logic (did the claimed thing happen?).
**Done when:** one full historical season of a single podcast is transcribed, extracted, entity-resolved, and resolution-graded end-to-end.
**Deferral (2026-09-01, Phase-4 re-sequencing):** deferred behind 4.2, 5.1
and 5.2 — see the Phase 4 header. Not struck and not dated: the item stays
as written, and its case is strongest if 4.2's holdout precision disappoints
(the stat generator alone is not enough) — the 4.1 lead-time scorecard is
exactly how a podcast arm would prove it sees role changes before usage does.
**Update:**
> _[To be completed]_

### 4.4 [Experiment] Podcast ablation & source calibration
**Goal:** The deploy/retire decision: candidate re-ranking with vs. without podcast features on holdout; per-source (and per-claim-type) reliability calibration learned on train seasons only. A null result retires the arm cleanly — that outcome is a success, not a failure.
**Done when:** findings in `intel/research/podcast-ablation.md` with an explicit deploy / retire / narrow-deploy (e.g., injury-intel claims only) decision.
**Deferral (2026-09-01):** deferred with 4.3 — it is the same experiment's
grading half and has no input without it.
**Update:**
> _[To be completed]_

### 4.5 [Experiment] Local-model bake-off
**Goal:** Pre-qualify the Ollama fallback before pricing changes force it: 2–3 local candidates vs. Claude on the 4.3 labeled extraction set; measure extraction agreement **and** downstream re-ranker lift preservation (the test that matters). Update routing config with qualified assignments per task tier.
**Done when:** findings in `intel/research/model-bakeoff.md`; routing config carries a validated local assignment for every routine task tag.
**Deferral (2026-09-01):** deferred behind more critical work. This is a
cost hedge, not a signal: the only live LLM workload today is the Wednesday
briefing prose (3.6, `claude_cli` on the subscription), and the labelled
extraction set it would bake off against exists only if 4.3 is built. Its
trigger stays what the goal says — pricing changes forcing it — or 4.3
landing, whichever comes first.
**Update:**
> _[To be completed]_

### ✦ Checkpoint 4: Signal deployment decisions
Deploy, narrow, or retire each signal arm per 4.2/4.4 results; fold tuned defaults into the live modules; record what the backtest priors now are (these become the learning loop's anchor in 5.2).
**Checkpoint notes:**
> _[To be completed]_

---

## Phase 5: Season Systems & Maturation (rolling, in-season)

**Goal:** The strategic layer and the self-improvement loop — shipped opportunistically across the season, sequenced by standings context and interest.

**Pull-forward amendment (2026-09-01, with the Phase-4 re-sequencing):**
**5.1 and 5.2 now come before 4.3–4.5**, i.e. immediately after 4.2. They are
the two items that touch every in-season week — 5.2 in particular is the
human half of the learning loop (the Monday retro and the heuristics
promotion ladder, still a CLAUDE.md placeholder), and the Phase-4 backtest
priors it anchors on exist once 4.2 closes. 5.3 keeps its opportunistic
place; 5.4 is unchanged.

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
