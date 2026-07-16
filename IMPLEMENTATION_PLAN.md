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
> _[To be completed]_

### 1.3 [Build] House-rules scoring engine
**Goal:** `core/scoring.py` complete: full PPR, D/ST points-allowed **and** yards-allowed brackets, distance-based kicker scoring with −1/miss — transcribed from the league settings pulled in 1.1. Golden-master tests from hand-computed stat lines; a post-Week-1 validation task (compare against real ESPN box scores) gets a TODO anchor now.
**Done when:** golden tests pass, including deliberately nasty edge cases (D/ST safety + bracket combos, missed XP vs. missed FG).
**Update:**
> _[To be completed]_

### 1.4 [Build] NFL data ingestion
**Goal:** `nfl_data_py` clients for weekly stats, usage (snap/target/route/red-zone shares), expected stats, depth charts, injuries (with practice-trajectory and per-player rest-day-baseline interpretation), schedules — landed in SQLite with `as_of` semantics and multi-season history (≥2021). Player IDs via the nflverse/DynastyProcess crosswalk (`import_ids()`), with validation tests for rookies/D-ST gaps.
**Done when:** a query like "usage deltas for all RBs as of 2023 week 6" returns correct, leakage-tested results.
**Update:**
> _[To be completed]_

### 1.5 [Build] Projections, ADP, odds, weather ingestion
**Goal:** Current-season consensus projections (full stat lines), preseason ADP distributions (market source + ESPN default rankings side by side — the divergence table is a first-class artifact), Vegas totals/spreads, and the Open-Meteo weather client keyed to stadium coordinates/dome flags. News headline speed-lane ingestion can land here or in 3.6 — Claude Code's call.
**Done when:** each source lands in SQLite with `as_of`; the ESPN-vs-market divergence report runs and produces a readable table.
**Update:**
> _[To be completed]_

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
