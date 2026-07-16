# Ziggurat

AI-assisted decision system for a season-long fantasy football league.

## Overview

Ziggurat is a solo-operated intelligence and decision-support system for competing in a 10-team office fantasy football league (ESPN platform). It combines a structured data layer (league state, NFL statistics, projections, betting markets), an unstructured signal pipeline (podcast transcription and claim extraction), a valuation and simulation core, and a markdown-based memory/judgment layer — all orchestrated through Claude Code acting as the agentic harness. There is no custom application runtime: the repo *is* the system, and the operator interacts with it conversationally through Claude Code, which manipulates CLI tools, a SQLite database, and markdown intelligence files.

## Problem Statement

The operator has joined an office fantasy football league with near-zero football domain knowledge, competing against colleagues with years of fan intuition. The system's purpose is to substitute engineered intelligence for that missing intuition — and exceed it. Fantasy football is a favorable target: it is a weekly-cadence, information-rich decision game where most competitors rely on default platform rankings, casual attention, and recency bias. Systematic data ingestion, league-specific valuation, and disciplined process should produce durable edges.

A secondary purpose: the project is a serious testbed for an agentic-harness architecture pattern (Claude Code + CLI tools + SQLite facts + markdown memory) the operator uses in professional contexts, and for backtest-driven signal validation methodology.

## League Context (ground truth — all valuation must respect these settings)

- **Platform:** ESPN Fantasy Football. League name "Sac LS Berry Patch" (JV office league). Operator's team: "The Bitter Lesson."
- **Format:** 10 teams, head-to-head points, one opponent per week, ~14-week regular season (2026 NFL season).
- **Rosters:** 16 players — 9 starters (QB, 2 RB, 2 WR, TE, FLEX, D/ST, K), 7 bench, 1 IR.
- **Draft:** Snake, 60-second pick clock, not yet scheduled (expected mid-to-late August 2026).
- **Scoring:** Full PPR. Custom quirks: D/ST receives yards-allowed bracket scoring **in addition to** points-allowed brackets; kickers score by distance with **−1 per missed kick**. These quirks materially diverge from ESPN default rankings — re-pricing under house rules is a core edge.
- **Waivers:** No FAAB. Priority resets weekly to inverse standings. 1-day waiver period, then free agents are first-come-first-served. No acquisition limits.
- **Locking:** Players lock individually at their game's kickoff (late-swap and Sunday-morning inactive plays are available). Lineup protection off.
- **Playoffs:** 6 of 10 teams, weeks 15–17. Seeding tiebreaker: total points for.
- **Trades:** Deadline Dec 2, 2026; vetoable by 4 votes. League trade culture unknown.

## Goals & Success Criteria

1. **Win the league** (minimum bar: make the 6-team playoff field).
2. **Draft with no self-inflicted wounds:** a roster with sound floor in rounds 1–4, ceiling-seeking in late rounds, no positional structure errors. Bye-week structure is evaluated on EV by the Monte Carlo/mock engines rather than penalized by rule of thumb — concentrated byes (punting one week for full strength in the other thirteen) can be EV-positive, and with inverse-standings priority a punted week even buys waiver position.
3. **Beat the market on waivers:** measurable target — detect in-season breakout players ≥1 week before league-wide consensus (proxied by roster-percentage spikes / expert rank moves), at a precision usable within 1–2 claims per week.
4. **Validated signals, not vibes:** every signal family deployed in-season must first demonstrate lift in multi-season backtests with strict as-of time discipline and train/holdout separation.
5. **Sustainable cadence:** the weekly in-season operating loop (briefing, waivers, lineup, retro) costs the operator ≤ ~30 minutes/day of attention.
6. **Explainability:** every recommendation ships with reasons legible to a football novice. The operator cannot catch domain-absurd outputs by intuition, so the system must show its work.

## Target User

One operator (the builder), technically strong (self-taught Python/AI), football novice. All interaction flows through Claude Code sessions in the repo, plus scheduled headless runs. No other users; no UI polish requirements beyond the draft-day tool.

## Core Features

### 1. Data Layer
All external data flows through ingestion clients into SQLite (facts) and file caches (raw pulls). **Every read accessor accepts an `as_of` date and returns only data knowable at that moment.** This is non-negotiable and enables the entire backtest program on the same code paths as live operation.

- **League sync (`espn_api`):** rosters (all 10 teams), standings, matchups, transactions, free-agent pool, draft results. Private-league auth via SWID/espn_s2 cookies.
- **NFL data (`nfl_data_py` / nflverse):** weekly stats, play-by-play-derived usage (snap share, target share, routes, red-zone touches), expected stats (xTD etc.), depth charts, official injury reports (practice participation Wed–Fri + game status — interpreted as trajectories against per-player rest-day baselines: a veteran's routine Wednesday DNP is noise, Friday designations are ground truth), schedules. Multi-season history for backtesting.
- **Consensus projections:** weekly full stat-line projections (e.g., FantasyPros consensus) as point estimates. Preseason ADP distributions for the draft.
- **Betting markets:** game totals, spreads, implied team totals.
- **Weather:** Open-Meteo (free) against static stadium coordinates and dome flags. House scoring makes this a first-class input, not a nicety: wind above ~15 mph materially suppresses FG accuracy (−1 per miss) and passing yardage (D/ST yards-allowed brackets).
- **News headline feed (speed lane):** an aggregated player-news wire (Rotoworld-style feeds) capturing beat-reporter reports within minutes, without Twitter/X API costs. Feeds the push layer; podcasts remain the depth/re-ranker lane.
- **Podcast pipeline:** RSS feed monitoring → audio download → local Whisper transcription → LLM claim extraction (see Feature 4). Historical episode archives (timestamped via RSS publish dates) enable backtesting.

### 2. House-Rules Scoring Engine
A single module (`core/scoring.py`) is the **only** place league scoring rules exist. Converts any stat line (actual or projected) into league points, including the D/ST yards-allowed brackets, distance-based kicker scoring with miss penalties, and full PPR. Consumed by valuation, draft, streaming, lineup, and backtest components. Golden-master tested against actual ESPN box scores once the season starts.

### 3. Valuation Core
Three distinct questions, three layers, each consuming the one below:

- **Global value:** Value Over Replacement (VOR) from re-scored consensus projections — points above a freely-available same-position player. Position replacement levels derived from league size and roster structure.
- **Marginal roster value:** a player's expected improvement to *this roster's* starting lineups over the remaining season — accounts for positional depth, bye coverage, handcuff/insurance correlation, and playoff-week (15–17) schedules. Drives add/drop and trade evaluation. Drop candidates ranked by lowest marginal (not global) value. Bench assets are valued on **conditional distributions, not medians**: a handcuff projecting 2.0 median points can carry large contingent value (P(starter unavailable) × conditional projection); median-only VOR systematically drops lottery tickets for low-ceiling mediocrities.
- **Candidate generation + signals:** a high-recall breakout candidate generator driven by raw usage leading-indicators (snap/target/route/red-zone share deltas, opportunity shocks from injuries and depth-chart changes) plus regression signals (TD over/under-performance vs. expected). Fade detection (efficiency decline under stable usage) as a secondary module.

### 4. Podcast Signal Arm (Re-Ranker)
Extracts **structured claims, not sentiment**, from fantasy/football podcasts: (player, claim type [role change / injury intel / scheme change / talent evaluation / matchup], direction, conviction, forward-looking flag, quote, timestamp, source). Aggregated into per-player weekly features. Architectural role: **precision re-ranker** over the valuation core's high-recall candidate set — its job is separating real role changes from usage blips at the top of the list. Includes per-source reliability scoring: because claims are timestamped and eventually resolve, each podcast/host is calibrated like a forecaster, and learned source weights feed the re-ranker. Transcription hardening: Whisper notoriously mangles NFL player names, so the pipeline biases decoding with a vocabulary prompt (within Whisper's prompt-token limit — the full active-player list does not fit) and runs a phonetic/fuzzy entity-resolution pass against the player table during claim extraction, where the extraction model maps mangled phonetics to canonical IDs from a candidate list. Deployment is conditional on demonstrated backtest lift (see Feature 9); a null result retires the arm.

### 5. League & Opponent Layer
- **State:** synced standings, all rosters, schedules, weekly matchups.
- **Behavioral profiles (markdown):** per-opponent tendencies observed over the season — lineup-setting diligence, bye-week negligence, points-chasing, roster hoarding. In a casual league, exploiting inattention is a first-class edge.
- **Rest-of-season Monte Carlo:** simulate remaining schedule (thousands of iterations) using team-strength projections → live playoff odds → **strategic posture** (must-win bubble mode: churn aggressively, maximize now; safe mode: stash breakouts, optimize for weeks 15–17). Posture is an input to waiver, lineup, and trade decisions.

### 6. Lineup Decision Support
Weekly starter recommendations that maximize **win probability against the specific opponent**, not raw points: opponent projected total + own player projection distributions → variance posture (underdog: embrace variance and correlated starts; favorite: floors, avoid correlation). The points-for playoff tiebreaker keeps expected points as the default when margins are close. Slot assignment maximizes late-week optionality: earliest-kickoff starters (Thursday players especially) go in their dedicated position slots, never FLEX, preserving FLEX for the latest-locking player. Questionable/game-time-decision players get time-contingent EV treatment — waiting on a Monday-night Questionable versus locking a Sunday 1:00 alternative is a sequential decision under lock-time ordering, not a point comparison. Includes a Sunday-morning inactive-report check exploiting individual player locking.

### 7. Waiver / Free-Agent Module
Scans available players against roster marginal values; recommends claims and drops with reasoning. Speed-oriented **push layer**: a scheduled morning briefing (overnight injuries, candidates crossing thresholds, lineup flags — a two-minute read) and event-triggered alerts for time-critical moves (e.g., starter injured, handcuff sitting in free agency). Scheduled runs execute headlessly via `claude -p` against the repo. **Roster legality precheck:** ESPN blocks all transactions while a roster is illegal — notably when Tuesday's league-wide status reset flips an IR-slot occupant from Out back to Questionable — so the Tuesday workflow validates IR eligibility and computes any forced drop before generating claim plans. Timing: waiver *claims* are queued and need no speed, so submit them liberally; ESPN processes claims in overnight batches (~3:00–4:30 AM ET Wednesday), so the post-waiver scan is scheduled immediately after that window and the morning briefing surfaces first-come-first-served grabs at breakfast, not after the pool has been picked over. Priority-reset waiver rules imply claiming freely is always correct and speed on post-waiver free agents matters most.

### 8. Draft Tool (deletable by design)
A standalone directory that imports the permanent valuation core and is deleted after draft day. Components:
- **Pick engine:** dynamic-programming pick logic in the Fry–Lundberg–Ohlmann tradition — pick value as a function of player value, remaining board, and positional need — extended with **survival probabilities** (odds each player remains at the next snake pick, from ADP distributions) and opponent-need modeling (nine rosters' hunger sharpens board-decay predictions; Gibson–Ohlmann–Fry sequential-competition framing). Survival probabilities key primarily on **ESPN's default rankings**: a room of casuals drafts off the list on their screens, and ESPN's list lags true market ADP — so market/ESPN divergences are directly exploitable (a player at market ADP 30 but ESPN rank 70 can safely be taken around pick 55).
- **Live board:** terminal-first (rich CLI/TUI), optimized for fast pick entry under a 60-second clock. The tool recomputes continuously between the operator's picks; the operator's turn is confirmation, not deliberation. Pre-computed tiers and contingency plans cover the snake turns where windows are tightest. Pick entry uses fuzzy/alias matching (RapidFuzz-style: 'cmc' or a mangled partial resolves instantly to the intended player) — sixty seconds leaves no room for spelling Chigoziem Okonkwo under pressure — and the board shows ESPN-rank ordering alongside value, since that list predicts the room's behavior.
- **Mock-draft simulator:** ADP-plus-noise bot opponents for strategy rehearsal from the actual draft slot; doubles as the pick engine's test harness and the apparatus for validating practitioner strategies (Zero-RB etc.) under house scoring.
- **Fallback:** if live ESPN draft-room sync proves infeasible, manual fast entry is the primary interface. Design for this from the start.

### 9. Backtest & Experiment Harness
Imports production code directly and replays history through it. Priority experiments:
- **Breakout detection:** across multiple seasons (e.g., train 2021–23, hold out 2024–25), measure the candidate pipeline's *lead time vs. the market* (roster % / rank-move proxies) and precision@k for small k (1–3 claims/week is the realistic budget).
- **Podcast ablation:** candidate rankings with vs. without podcast features; per-source reliability calibration on train seasons, validated on holdout. Decides whether the podcast arm deploys.
- **Fade detection:** same methodology, expected weaker signal, secondary priority.
- **Draft strategy validation:** mock-sim tournaments comparing roster-construction strategies under house rules.
- Evaluation philosophy throughout: **grade decisions, not championships** — week-level, slot-level gradable calls, hundreds of measurements per season.

### 10. Memory & Learning Layer
- `intel/` markdown tree: opponent profiles, player watch notes, weekly decision journal, distilled research holdings (academic papers reduced to operative claims + parameters), backtest findings.
- **Monday retro:** grades the prior week's decisions on *process, not outcomes* (was the call right given what was knowable at decision time; correct-but-unlucky logs as variance, not error).
- **Heuristics promotion ladder** (`intel/heuristics.md`): observation → hypothesis (tracked, not applied) → rule (applied, with cited supporting evidence). Promotion criteria written explicitly in CLAUDE.md. Backtest findings are strong priors; in-season evidence must be repeated and strong to override them (season = fine-tuning at a low learning rate). This guards against overlearning from single-week flukes.

### 11. Trade Finder (build-ready, deploy per league culture)
Computes marginal values for **every** roster, not just the operator's; surfaces trades that are mutually beneficial by each side's own lights but asymmetric in the operator's favor. Sell-high timing from fade/regression signals. Drafts natural-language trade pitches. Deployment gated on observed league trade activity/receptivity — and tempered by the 4-of-10 veto threshold: office leagues veto trades that *look* lopsided regardless of true mutual benefit, so proposals must be legible as fair, not merely be fair.

### 12. Streaming Module
Weekly ranked lists of available D/ST (and K) under house scoring, driven by opponent-offense quality and Vegas totals. The custom D/ST yards-allowed brackets make league-specific re-pricing here a cheap, repeatable edge invisible to default-rankings users. Supports claiming next week's obvious stream a week early.

### 13. Roster Red-Team Report
Standing self-audit: projection concentration risk, correlation exposure, bye-week pileups, injury-fragility clustering. Cheap; catches failure modes optimism hides.

## Technical Architecture

### System Overview
Claude Code is the harness and reasoning layer. The Python package provides deterministic tools; the CLI exposes them; SQLite holds facts; markdown holds judgment; CLAUDE.md holds the constitution (workflows, cadences, promotion criteria). Scheduled intelligence runs headlessly (`claude -p` + cron) against the same repo. No server, no deployment, no custom agent runtime.

### Repository Layout
```
ziggurat/
├── CLAUDE.md                # constitution: workflows, cadences, heuristic-promotion rules
├── pyproject.toml           # one Python package
├── ziggurat/
│   ├── data/                # ingestion clients (espn, nfl, projections, odds, podcasts)
│   ├── core/                # scoring.py (single source of truth), valuation, marginal, signals
│   ├── league/              # state, monte-carlo simulate
│   ├── draft/               # deletable: engine, board, mock  (imports core/)
│   └── cli/                 # thin commands — no logic lives here
├── backtest/                # experiments; imports ziggurat/ directly
├── db/                      # schema.sql in git; .sqlite file gitignored
├── intel/                   # markdown memory: opponents/, weekly/, heuristics.md, research/
├── tests/
└── data/                    # gitignored: raw pulls, audio, transcripts, parquet caches
```

### Technology Stack
- **Language:** Python 3.11+
- **Database:** SQLite (facts, temporal tables with as-of semantics)
- **Key libraries:** `espn_api` (league), `nfl_data_py` (NFL data), `pandas`, Typer or Click (CLI), `pytest`; local Whisper for transcription (operator has capable local inference hardware); `rich`/Textual for the draft board TUI
- **LLM usage (tiered, swappable):** Claude Code (Max subscription) for interactive reasoning and the weekly decision loop. Scheduled/batch work (headless briefings, podcast claim extraction, news summarization) currently runs via headless `claude -p` on the subscription — but Anthropic has signaled `-p` may move to API pricing, and over a 16-week season that change should be assumed. Therefore every programmatic LLM call goes through a thin internal routing interface with swappable backends: local models via Ollama for routine extraction/summarization, with metered Claude API tolerated only for designated high-stakes tasks. No component calls a model directly.
- **Infrastructure:** local-only; the operator's always-on Strix Halo desktop is the system's home — cron scheduling plus Ollama inference on the same box

### Data Model (entity level)
- **players** (identity, position, NFL team; cross-source IDs via the maintained nflverse/DynastyProcess crosswalks — `nfl_data_py`'s `import_ids()` — rather than a hand-built mapping, with thin validation tests since rookies and D/STs have gaps)
- **weekly_stats / usage** (per player-week: box stats + snap/target/route/red-zone shares + expected stats)
- **projections** (per player-week per source, with `retrieved_as_of`)
- **games / schedules / odds** (per NFL game: teams, kickoff, total, spread)
- **injuries** (per player-week: practice participation trajectory, game status, with report dates)
- **league_teams / rosters** (temporal: who held whom, when), **transactions**, **matchups / standings** (per week)
- **claims** (podcast: player, source, episode, timestamp, type, direction, conviction, forward-looking, resolution)
- **decisions** (graded decision log: decision, alternatives, reasoning ref, process grade, outcome, variance/error classification)

### Key Design Decisions
1. **Claude Code as harness, no custom runtime:** eliminates harness engineering; frontier reasoning on a flat subscription; matches the operator's proven working pattern.
2. **Monorepo:** components share the valuation core and the backtest must exercise production code; the repo is Claude Code's operational surface and splitting it fragments the memory system. Isolation comes from module boundaries and tests, not git boundaries.
3. **Single scoring engine:** house-rule quirks are the core edge; they must never drift between components.
4. **`as_of` discipline from day one:** every accessor is time-aware, making backtests leakage-resistant and free on the live code path. Retrofitting this is prohibitively painful.
5. **SQLite for facts, markdown for judgment:** schema'd, queryable data vs. narrative context Claude Code reads/writes. Mirrors the operator's existing systems.
6. **Consume consensus projections; build leading indicators:** beating aggregated professional point estimates is a losing game; the edge is (a) re-pricing under house rules and (b) raw-usage trend detection that precedes consensus movement.
7. **Candidate generator (recall) + re-ranker (precision):** matches the decision budget — 1–2 waiver moves/week means precision@3 is what matters.
8. **Claims, not sentiment,** from podcasts; marginal-information test (does the claim contain anything not already in the usage data?).
9. **Win probability over raw points** in lineup logic, expressed as variance posture; points-for tiebreaker keeps expected points the tiebreaking default.
10. **Grade process, not outcomes;** promotion ladder + backtest priors guard against overlearning single-week noise.
11. **Human-in-the-loop execution:** the system recommends; the operator executes all roster transactions in the ESPN app. No automated writes to the ESPN account (reliability, ToS, and blast-radius reasons).
12. **Draft tool is a deletable wrapper** over the permanent valuation engine; intelligence is pre-computed because a 60-second clock forbids in-loop deliberation.
13. **Model-tier routing with local optionality from day one:** LLM calls are abstracted behind a single interface, tagged by task stakes; backends (Claude Code headless / Claude API / Ollama) are configuration, not code. Cheap now, painful to retrofit — the same logic as the `as_of` rule. An early bake-off pre-qualifies local models (the podcast backtest's resolved claims double as a labeled eval set for extraction quality), so the swap is validated before a pricing change forces it.

## Constraints & Considerations

### Known Challenges (ranked by risk)
1. **Historical market/forecast archives** (biggest backtest feasibility risk): historical weekly consensus projections, roster percentages, and expert ranks may be partially inaccessible. Requires an early feasibility spike. Leading fallback: **historical DFS salaries** (DraftKings/FanDuel — archived weekly and strictly timestamped) as the market-expectation proxy: salary levels proxy weekly expectations, and week-over-week salary moves proxy when the market caught up to a player. Caveats: salaries are set by operators rather than a clearing market and are compressed at the top. Secondary candidates: FantasyPros historical rank archives, Wayback snapshots, in-season ADP.
2. **`espn_api` fragility:** unofficial, cookie-authenticated, breaks when ESPN changes endpoints. Wrap it behind an internal interface; cache aggressively; assume mid-season repair work.
3. **Live draft sync:** the ESPN draft room is WebSocket-driven and `espn_api` live sync is expected to lag or fail. Manual fast entry (with fuzzy matching) is the designed primary path, not a fallback afterthought.
4. **Player ID reconciliation** across nflverse/ESPN/projection sources — tedious, error-prone, foundational. Build the mapping table early and test it.
5. **Podcast signal may be derivative** (adds nothing after conditioning on usage features). Acceptable outcome; the ablation decides, and the arm retires cleanly.
6. **Small breakout samples** (~10–20/season at relevant positions): multi-season data mandatory; threshold tuning must respect holdout discipline or it memorizes lucky seasons.
7. **Leakage discipline generally:** timestamp cuts on every backtest input, including podcast publish dates and source-weight training.

### Out of Scope
- Daily fantasy sports, betting, or any real-money application
- Multi-league support or productization; any other user
- Building projections from raw stats (consensus is consumed, not replicated)
- Automated transaction execution against the ESPN account
- Web/mobile frontends (terminal + markdown + briefings only; draft board TUI is the ceiling)
- Full-season multi-agent simulation (2025 replay with competing strategies) — deferred; the harness enables it later if time permits

### Security & Privacy Considerations
- **The repo is public** (operator's build-in-public commitment). Code, structure, and documentation are published; **the `intel/` tree, `db/` contents, and `data/` are gitignored** — opponent behavioral profiles, the decision journal, and any league-specific notes never leave the local machine. Committed files, tests, fixtures, and commit messages must contain no colleague names or personal details.
- ESPN credentials (SWID/espn_s2) in a gitignored `.env`, never committed.
- Colleague behavioral profiles, even locally, are lighthearted competitive notes about fantasy play, not personal dossiers; contents stay strictly game-related.
- Competitive OPSEC: publishing reveals the method, not the intelligence — the private intel/data layer and execution are the moat. If league-mates find the repo, that's a feature of building in public, not a leak.

### Future Considerations
- Full-season 2025 replay tournament (strategy × model ablations)
- Weekly league newsletter / power-rankings generator (social layer)
- Additional signal families: rookie ramp curves, injury-return trajectories
- Post-season writeup: the project doubles as a demonstrable agentic-systems case study

## Timeline & Sequencing Tiers
- **Tier 1 — Draft day (expected mid-to-late Aug 2026):** ingestion (ESPN, NFL, projections, ADP), scoring engine, valuation core (global VOR), draft tool + mock sim. The as-of foundation ships here.
- **Tier 2 — NFL Week 1 (~Sept 10, 2026):** league sync cadence, marginal valuation, candidate/waiver pipeline, streaming module + weather feed, push layer, lineup support. Weeks 1–3 are the richest waiver season; this tier cannot slip.
- **Tier 3 — In-season (rolling):** podcast arm (pending backtest lift), local-model bake-off (Ollama candidates vs. Claude on extraction/summarization, front-loaded alongside the podcast backtest since it shares the labeled data), trade finder, playoff Monte Carlo, learning loop maturation, red-team report, deferred experiments.
- Backtest work is timeless but front-loads wherever it gates a live signal (tune before deploying).

---

## Notes for Claude Code

- **CLAUDE.md is the constitution.** Weekly cadences (waiver day, Sunday inactives check, Monday retro), heuristic promotion criteria, and data-refresh workflows live there and are followed, not improvised.
- **Explainability is a requirement, not a nicety.** The operator is a football novice and cannot smell absurd outputs. Every recommendation includes the reasons and the data behind it. Sanity checks (e.g., never recommend starting a player ruled OUT) are enforced in code, not left to judgment.
- **Testing expectations:** `core/scoring.py` gets golden-master tests (known stat lines → asserted points, later validated against real ESPN box scores). Data accessors get as-of leakage tests (querying as-of week N must never surface week N+1 facts). The mock-draft sim is the draft engine's test harness. Pure-logic modules get unit tests; ingestion gets thin integration tests with cached fixtures.
- **Build order follows the dependency chain:** scoring → ingestion → valuation → everything else. Each layer lands with tests before the next begins.
- **Memory compaction is a scheduled workflow, not an emergency measure.** The `intel/` tree outgrows useful context size by mid-season; CLAUDE.md codifies periodic synthesis (every ~4 weeks, distill weekly journals into `intel/rest_of_season_priors.md`) and keeps raw archives out of the default reading path for routine tasks.
- **Style:** plain, readable Python; minimal dependencies; small modules with clean interfaces; no logic in the CLI layer. Prefer boring technology.
- **Expect discovery.** Unofficial APIs, archive availability, and signal quality will force plan adjustments. Record findings and adaptations in sprint updates and `intel/research/` so decisions persist across sessions.
