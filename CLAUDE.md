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

- **2.2 mock draft simulator — done 2026-07-21.** Rule-8-quarantined `ziggurat/draft/`
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

- **2.4 draft board TUI — done 2026-07-22.** Draft-day cockpit in Rule-8-quarantined
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
  — `ziggurat/draft/` is untouched and keeps its own quarantined copies; brute force ships
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

- **3.3 candidate generator & signals — built, audited & fixed 2026-07-26.**
  Permanent `core/candidates.py` (Rule 8): three labelled signal blocks —
  **usage-delta breakouts** (`usage_deltas`, full metric set, skill positions
  only), **injury-triggered opportunity shocks**, and **`QB1_CHANGE`** as a
  labelled hypothesis (folds `qb1_change_candidates` reasons verbatim; no
  RB/WR/TE rank-change trigger, enforced by test) — rendered separately (high
  recall, precision tuning is 4.2's), thin `ziggurat candidates` CLI, no
  scoring/points (Rule 2). Built to the 3.2c rescope, not the original goal:
  depth-chart demotion is measured-dead so it survives only as the QB hypothesis,
  and **TD-regression is not built (no in-season source; deferred to Phase 4).**
  **The build's shaping fact: the live injury signal cannot come from nflverse.**
  `get_injuries` is backtest-only for 2025+ (nflverse dropped `date_modified`, so
  every 2025 row is gameday-stamped with 0-day lead) and is blind to IR/season
  enders entirely (Conner, Harris = 0 rows) — those shocks ride the usage arm
  alone. So per operator decision the **injury arm ships BOTH sources**: a NEW
  read-time `state.injury_transitions()` (diffs consecutive `league_player_state`
  snapshots for availability crossings, no migration) is the live path,
  `get_injuries` the historical/backtest path; the live helper is
  **synthetic/smoke-tested only until real games produce transitions.** The
  two-view seam is threaded throughout (live reads `historical`, the 2025
  validation binds `base.latest_truth`, exposed as `ziggurat candidates
  --validate`). Done-when met on the live 2025 backfill: the §7.3 five verified
  targets all surface (Dowdle wk5, Tucker wk8, Henderson wk9, Monangai wk9,
  Wilson wk11 — the snap-blind case caught via air-yards/targets) and the Gibbs
  control does not dominate. Three verified workflows (recon → build+green-gate →
  7-dimension adversarial audit with per-finding refute-first verification, 26
  agents). **The audit found the seam clean (no leakage, no rules/boundary
  violations) and 17 real defects (2 major), all fixed. Headline: an absence of
  difference is not an absence of signal** — the usage arm silently dropped the
  `prior_week=None` cohort, so a rookie who *debuts* for 22 carries after the
  starter is ruled Out (all-`None` deltas — nothing to difference) was invisible
  in BOTH arms with no note, exactly the cohort recon named as the one to
  surface; fixed with an absolute-usage "role emergence" path (labelled floors,
  fed into the beneficiary index, a ~1/week trickle on 2025). Also fixed: an
  unhedged "opportunity opened by X" causal claim that fired on established WR1s;
  WOPR triple-counted in the magnitude AND printed as bare jargon (dropped from
  both — it is `1.5·target_share + 0.7·air_yards_share`); a `SCORE` column a
  novice reads as points (→ `SIGNAL`); a past-season CLI whose only guidance
  named a Python API it didn't expose; a whitespace-encoded position that
  defeated the None-keep guard; a dual-source dedupe that double-emitted across a
  gsis gap; a `--top` that hid star shocks under bench streamers (now
  `percent_owned`-tiebroken); and four vacuous/dead-code tests. Suite green
  (**1255 passed**; +14). Details: `IMPLEMENTATION_PLAN.md` 3.3 + gitignored
  `intel/research/candidates-3.3-design.md`.

- **3.4 waiver module — built, audited & fixed 2026-07-26.** New permanent
  `core/waiver.py` (`core→league` acyclic direction; Rule 8 never touches
  `draft/`), flat `ziggurat waivers` CLI. **Pure composition with one new piece**:
  it calls `marginal.build_board()` ONCE (drop board + add/drop swap matrix, adds
  already scoped to the FA pool and carrying WAIVERS-vs-FREEAGENT status), joins
  `candidates.build_candidates` on `espn_id` for opportunity context, and reads the
  roster + FA pool + `waiver_rank` from `league/state` — re-pricing nothing. No
  scoring (Rule 2), no migration (`schema_version` 7). The **one new piece is the
  roster-legality precheck**: it recounts IR itself (the shared seater strips all IR
  rows, hiding the exact 17>16 oversize), reslots an ineligible IR occupant IR→BE
  before pricing the forced drop, and runs independently of `build_board` (which
  raises at `scoring_period==0`) so refuse-and-propose never depends on pricing.
  Done-when met on synthetic state (pre-draft DB has no rostered players): 16 active
  + 1 IR occupant flipped OUT→QUESTIONABLE → blocked, empty claims, occupant named,
  a restorative fix; flip to OUT → legal + claims. **IR eligibility AND the whole
  IR-legality fix model ship as labelled hypotheses** (`IR_ELIGIBLE_STATUSES`,
  `IR_FIX_MODEL_LABEL`) — ESPN's `eligibleSlots` is not ingested and no draft has
  happened, so they are disclosed as UNVERIFIED on every plan, to confirm in-app
  post-draft (same discipline as scoring §3.8). Three verified workflows (recon →
  build+green-gate → 7-dimension adversarial audit, 29 agents). **The audit found
  the seam clean (no leakage, no rules/boundary violations) and 18 real defects (6
  major), all fixed. Headline: the legality FIX was non-restorative and
  non-terminating** — an ineligible IR occupant was double-counted as an independent
  violation, so obeying the plan's own "drop this player" never reached legality
  (17/16 → 16/16 → 15/16 … all still "illegal") while the true fix (move the reset
  player off IR) was never stated, and a costless IR-move fix was never offered.
  Fixed by redefining legality as `active>16 OR ir>1` (restorative + terminating,
  proven by a re-run test) with a preference-ordered fix (zero-drop IR-move primary,
  drop secondary) and sub-16 rosters never told to drop. Also fixed: the UNVERIFIED
  IR disclosure was hidden behind `--reasons` on a destructive drop (now
  unconditional); duplicate display-names mis-joined a claim to the wrong `espn_id`
  3.6 would act on (now `SwapRow` carries the ids, joined on identity); streamed
  1-week D/ST/K evicted season-long claims under budget (now segregated); unpriceable
  drops shown as confident top claims (now flagged + de-prioritised); `own_team_id=
  None` read the whole universe as your roster (now refused); a shared
  `classify_acquisition` so the drop board and claims can't contradict; plus
  test-rigor gaps (real view-threading leakage test, the candidate join exercised
  non-empty, the legal render constrained). Suite green (**1305 passed**; +27).
  Details: `IMPLEMENTATION_PLAN.md` 3.4 + gitignored
  `intel/research/waiver-3.4-design.md`.

- **3.5 lineup support & streaming — built, audited & fixed 2026-07-26.** Two new
  **permanent** core modules, pure composition over existing as-of accessors (no
  migration, `schema_version` stays 7): `core/streaming.py` (D/ST + K streaming ranker,
  `ziggurat stream`) and `core/lineup_support.py` (weekly starter recommender — win-prob
  variance posture + slot-lock + GTD + inactives, `ziggurat lineup`). `core→league→data`;
  never `draft/` (Rule 8). **The shaping fact is 3.2's flat-rate feed, and it forks the
  item:** projections are a flat SEASON RATE (skill CV ~1%, D/ST ~12%), so (1) a bare
  points-ranked D/ST is a season-long ranking wearing a streaming label — hence the
  ranker's **opponent-quality tilt is the load-bearing signal**; and (2) the feed carries
  no per-week dispersion, so the win-prob model's **variance is MEASURED off historical
  realised scoring** (nflverse 2021-2025 REG re-scored through `scoring.py`, per-unit ≥8
  games, OLS σ-on-mean) and frozen as `DEFAULT_VARIANCE` — a labelled hypothesis with its
  cohort/R² quoted in every reason (Rule 6), kept OUT of `scoring.py` (Rule 2: σ is a
  dispersion prior, not a scoring number). K σ is a PURE hypothesis (no FG line in
  `weekly_stats`); QB↔own-pass-catcher ρ=+0.35 is unmeasured (the strongest underdog
  "correlated starts" lever). The decision is `P(win)=Φ((mu−mu_opp)/√(var+var_opp))` with a
  close-band that returns the greedy points lineup verbatim (points-for tiebreaker) and,
  outside it, a hill-climb over legal single swaps capped at a 2-pt E(points) sacrifice;
  underdog promotes ceilings, favorite floors — from `dz/dvar`'s sign. **Done-when 1 met:**
  opponent swing −20→+20 flips the seated STARTER SET (floor↔boom), not just a label.
  Slot-lock is a **points-neutral relabel** over `fill_lineup` (latest-locking → FLEX,
  keyed generically on ET kickoff — 2026 opener is a *Wednesday*); GTD takes a keyword-only
  `now` decision clock (separate from the `as_of` data gate) and emits a lock-time-ordered
  contingency, not a point pick; `assert_no_illegal_starters` hard-raises on a seated
  bye/live-OUT player. Streaming prices `house_points` VERBATIM through `weekly_lines`
  (the same spine marginal uses); `stream_score` is a separately-**labelled non-house**
  multiplier; **Vegas is context-only + leakage-fenced** (`game_odds.knowable=gameday`, so
  a Tue/Wed read discloses "line not yet posted" — never `latest_truth` live); weather is
  the demonstrable done-when on injected synthetic rows (`game_weather` empty live). Three
  verified workflows (recon 7 → build+green-gate → 8-dimension refute-first audit, 16
  agents). **The audit found the seam clean (no leakage/rules violations) and 8 real
  defects (2 major), all fixed, the two highest-value mutation-verified. Headline: bye-week
  teams polluted the opponent-quality reference** — a fully-bye team entered at 0.0 and was
  not filtered, inflating pstdev ~3.5× (2026 wk5: 6.22→21.58) and collapsing the tilt
  toward zero, **disabling the streaming module's PRIMARY signal exactly on the bye weeks
  when streaming matters most**, plus printing a false "league average" (fixed: reference +
  average restricted to teams playing this week). Also fixed: the Vegas home/away sign had
  no correctness test (a future inversion shipped green — now mutation-caught); the GTD
  contingency wasn't `now`-gated (a live swap shown after a slot locked → `window_closed`);
  an all-dome slate raised a false "DEGRADED weather" banner (`weather_readable` split from
  `weather_available`); and four coverage gaps each now load-bearing-tested. Suite green
  (**1364 passed, 4 skipped**; +14). Details: `IMPLEMENTATION_PLAN.md` 3.5 + gitignored
  `intel/research/lineup-streaming-3.5-design.md`.

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

- **3.6 push layer — built, audited & fixed 2026-07-30. The FIRST live LLM
  backend.** Everything through 3.5 was deterministic; 3.6 implements `claude_cli`
  (headless `claude -p` on the Max subscription) as the one sanctioned model
  shell-out (Rule 4). Two scheduled deliverables + plumbing: `ziggurat brief run`
  (Wed 06:00 PT) composes waiver+lineup+signals+alerts into a two-minute briefing
  (`core/briefing.py`, pure), writes the full markdown to gitignored
  `intel/weekly/briefings/`, asks the router (`morning_briefing`→claude_cli→sonnet)
  for the prose, and pushes an **allowlist-safe teaser** (counts+legality+week, NO
  names) to a private **ntfy.sh** topic; `ziggurat alerts run` (every 20 min) pulls
  the **ESPN news wire** (`data/nfl/news.py`, `athleteId==players.espn_id` direct
  join), computes alert-worthy events (`core/alerts.py`, pure), dedups, and pushes
  the top few — where the phone lane is **action-only** (operator decision
  2026-08-05, `AlertEvent.phone_worthy`): `INJURY_OUT` always (seat someone /
  grab the handcuff), news ONLY about an own-roster player (the 20-min speed
  layer vs the 4x/day sync); free-agent/context news is computed for the
  briefing + alert log but never pushed — the operator is running an
  experiment, not following football, and pre-draft "rosterable" matched the
  entire NFL (46 news pushes in one evening, measured). New permanent `ziggurat/push/` package is the **egress choke point**:
  the Rule-5 **outbound scrub** (a data-driven denylist of this league's other-team
  names, checked before any send — the guarantee that makes a public-by-obscurity
  topic safe), the run-log/dedup helpers, and the orchestration. `marginal.handcuff_links()`
  reuses the existing depth kernel + `DEFAULT_HANDCUFFS` hypothesis (Rule 2, not
  reinvented). Migration `008` (`schema_version` 8: `player_news`/`player_news_links`
  fact tables + `push_runs`/`alert_ledger` operational tables) was iterated on a
  scratch DB and only moved into `db/migrations/` once final. **Done-when met on
  REAL preseason data** (Kittle OUT → handcuff Tonges FA surfaced through the whole
  pipeline; the claude_cli prose is genuinely good), though `injury_transitions` is
  synthetic-tested only until Week 1. **36-agent adversarial audit: seam clean; 19
  confirmed + 5 plausible defects, all fixed** — headline was **dry-run ledger
  poisoning** (a `--no-push` preview reserved the dedup ledger and permanently
  suppressed the real push; rewritten to publish-then-record). Also fixed: the
  scrub over-blocked on substring (→ word-boundary) and missed the title/tags
  headers; news `knowable_as_of` was UTC-truncated (→ bucketed in the box's Pacific
  calendar); an injury vacancy was wrongly bye-suppressed; the env scrub missed
  `ANTHROPIC_AUTH_TOKEN`/`_BASE_URL`/`CLAUDE_CONFIG_DIR`. Suite green (**1435
  passed**). Details: `IMPLEMENTATION_PLAN.md` 3.6 + gitignored
  `intel/research/push-layer-3.6-design.md`. **Standing lesson this item paid for:
  a dedup/idempotency ledger must be written ONLY after a real side effect
  (publish-then-record) — reserving before the effect means a preview/dry-run, or a
  transient failure, silently consumes the event.** Operator step complete
  2026-08-04: `NTFY_TOPIC` set, phone subscribed, timers installed on the
  desktop; first live ticks pushed `ntfy=200` end-to-end, and the install
  surfaced + fixed an ESPN news-wire 403 (edge now rejects UAs that don't match
  the client fingerprint; urllib's default UA passes). Remaining: real games
  (~Sept 10).

- **3.7 operating cadence v1 — built 2026-08-04.** The "Weekly operating
  cadence" section below is live: day-keyed workflows (Tue legality+claims, Wed
  post-waiver scan, Thu–Sat monitoring, Sun inactives+final lineup, Mon
  process-not-outcome retro, ~4-weekly compaction), a shared preflight, and an
  explicit division of labor (tools recommend; only the operator acts in the
  ESPN app). One journal template per NFL week
  (`templates/intel/weekly/week-TEMPLATE.md`, scaffolded by `intel init`) holds
  the decision log AND the Monday retro. The cadence section is treated as an
  interface, not prose: `tests/test_operating_cadence.py` resolves every quoted
  `ziggurat` invocation + flag against the real CLI, so a renamed command rots
  loudly. Done-when exercised same day by a fresh Opus subagent running the
  bare instruction "Run the Tuesday workflow": executed end-to-end pre-draft,
  six friction findings, all doc-fixed same day — the two that would have bitten
  in-season were the truncating default `--claim-budget` (step 4 was
  unexecutable as quoted) and a never-installed push layer reading as healthy
  (`no push runs recorded yet` ≠ healthy-empty). In-season re-verification
  under Checkpoint 3. Suite green (**1435 passed, 4 skipped**).

- **3.11 draft-engine integration — done 2026-08-31, the morning of the draft.**
  Two phases of measurement (7 measurement modules, an evaluation harness, 7
  candidate variants proved on HELD-OUT seeds) landed in the shipped path.
  **`ziggurat draft-web --season 2026 --slot 9` — the unchanged runbook command —
  now runs the COMPOSED engine, and `--legacy-engine` restores the pre-3.11
  engine exactly.** Two re-ranks of the shipped 2.3 engine, composed over
  **disjoint decision domains**: the pair term (`variant_wheel`) owns the 8
  first-of-pair picks (overalls 9, 29, 49, 69, 89, 109, 129, 149 — where only two
  rival picks separate the operator's turns), the week-by-week term
  (`variant_weekwise`, weight 2) owns the other 8. That nesting is the design
  decision: it is the only arrangement where each term runs in exactly the regime
  it was measured in, because `pair_analysis` reconstructs the engine's score
  rather than calling `recommend` (so neither ever re-ranks the other's output,
  and the week-by-week weight is never silently halved against a two-pick total).
  Wired at ONE seam — `DraftSession._engine()` — with the week-by-week points map
  read beside the board at one `as_of` (`simulator.load_draft_board`).
  **Measured on the composition as shipped**, not on either part: weekwise vs the
  engine replicates at **+0.043** through the actual wiring on 4 fresh held-out
  seeds (1,000 paired drafts, all four intervals excluding zero), and the pair
  term's increment ON TOP of it is **+0.0182 [+0.0119, +0.0245]** over 8 held-out
  seeds (2,000 paired drafts, positive in all eight). **Latency +11.1 ms** (legacy
  212.4 → composed 223.4 ms worst case at R=512 on the real board, paired and
  interleaved; 243 ms gate). **Determinism verified three ways**: same seed twice,
  across five `PYTHONHASHSEED` values, and crash + journal replay at pick 100 —
  all bit-identical. **The K/DST divergence play is untouched** (D/ST 89, K 92
  under both engines, asserted by name). Suite green (**2,443 passed, 4 skipped** after the 3.11a audit-fix round; 2,424 at integration).
  **The kicker correction (`core/kicker_board.py`, measured +0.053) is wired but
  did NOT ship**: its source table `espn_projections` holds 0 rows, has no CLI or
  registry entry, and `pull_espn_projections` correctly refuses to back-stamp — so
  its rows could only be stamped today, invisible at the golden's frozen `as_of`,
  i.e. the board deciding the picks could not be one any test had seen. The seam
  and its tests ship; the cockpit PRINTS that the K board is uncorrected and that
  it is not actionable tonight. **`bots.py` determinism hazard fixed**:
  `best_by_vor` / `best_by_rank` / `window_by_rank` resolved exact cross-position
  ties by SET iteration order, i.e. by `PYTHONHASHSEED` (measured: three different
  answers on a three-way tie). All three now carry the engine's own tie-break
  ladder as a total order — though the recorded justification was WRONG about the
  live board and is corrected here: `load_board` floors **35** unpriced rows at
  one identical `vor`, not 1,215 (that is the `<POS>:<rank>` ID-fallback count, a
  different quantity), and those 35 sit at ESPN rank 410+, i.e. out of reach in a
  160-pick draft. The fix buys nothing on tonight's board; it is kept because a
  total order costs nothing and the replay promise runs in a fresh process.
  Details: `IMPLEMENTATION_PLAN.md` 3.11.

  **Two disclosures the ship summary led without, both from the variant's own
  module docstring:** the week-by-week term does NOT fix the 3-QB/3-TE
  concentration (it prices a bench QB3 at zero for the same reason the 2.2 metric
  did), and with holes priced at a waiver-corrected rate the variant **stops
  removing them** and no longer clears its own +10-RB mechanism control. The
  +0.043 is measured against the objective as shipped, which likes removed holes.

- **3.11a audit-fix round — done 2026-08-31, before the 12:00 freeze.** Six
  adversarial auditors returned five confirmed majors on the integrated engine;
  all five are fixed and re-measured, with no change to any recommendation the
  engine makes (both goldens' players, scores and rosters are byte-identical;
  only reason TEXT moved, 43 rows across two deliberate re-blesses).
  **The headline is a cost bug the integration created and nothing measured:**
  `load_draft_board` read the projections table THREE times — `build_kicker_board`,
  `build_valuation` (via `load_board`) and `grader.weekly_points_map` each made
  their own `weekly_lines` pass at 3.55 s — so the composed cockpit printed
  nothing for 11.8 s idle / 23.6 s loaded on every launch AND every crash-resume,
  against a runbook that documents "Resume in 4 s" for the moment it calls the
  dangerous one. All three now share ONE read (`lines=` hand-over on
  `build_valuation` and `weekly_points_map`, which `build_kicker_board` already
  had): **cold start 3.8 s / serving 4.0 s, identical to `--legacy-engine`, and a
  100-pick resume 3.8 s** — measured on the real command. The same change fixed
  the `--weeks` crash (`rank_weeks` was not forwarded, so the board's and the
  map's id spaces were derived over different spans). Also fixed: **the
  "degrade LOUDLY, never crash" promise was implemented for the kicker half
  only** — a `GradeInputError` from the objective, or a `WeekwiseInputError` from
  `WeekwiseInputs.build`, killed the launch with a bare traceback naming no
  fallback; both now fall back to the pre-3.11 engine with a sentence that names
  the cause. **An on-clock composed fault degraded to a SILENTLY empty panel**
  (and, once the queue cache went cold, an `/api/queue` 500 that stops the writer
  reconciling — the failure that cost the 2026-08-27 practice draft its second
  half); `DraftSession._recommend_at` now serves that one recommendation from the
  shipped engine on a FRESH context (proven equal to `--legacy-engine`'s answer)
  and records a legible fault line the cockpit renders. **The wheel's first-of-pair
  sentence promised a partner the tool's own next pick contradicted a third of the
  time with that partner still on the board** (measured: 32 of 80 promises broken,
  26 of them that way, median quoted share 100%) — re-phrased as the assumption
  behind the number, with the break rate stated on the panel. **The kicker
  recommendation asserted "the best your scoring sees" with no disclosure that the
  K board is known-misordered**; `DraftSession.rec_caveats` now attaches the
  item-3.10 caveat to every K row, so it reaches the panel, the queue rows and the
  journal. Minors fixed alongside: the default engine now announces itself (the
  cockpit PAGE renders `engine_profile` and the launch notes, not just the
  terminal); a resume at a different `weekwise_weight` refuses like a profile
  mismatch; `1e-09` is gone from a novice-facing sentence; the hole sentence names
  the BYE that reconciles it with the need note two bullets above; and
  `--legacy-engine`'s help text names the pair re-rank it actually gives up.
  Latency re-measured paired and interleaved: **legacy 216.5/216.8 ms, composed
  226.1/227.2 ms worst case at R=512** (gate 243 ms). Determinism re-verified
  three ways including five `PYTHONHASHSEED` values in separate processes —
  identical digest. **NOT fixed, deliberately, and recorded instead:** the literal
  Rule-8 violation in `backtest/draft_backtest.py` (the boundary scanner rglobs
  only `ziggurat/`, so it cannot see it) and the operator's ESPN league id
  hard-coded at `tests/test_espn_ranks.py:226` since commit `dd0fcb9` — both real,
  neither draft-critical, and both post-draft work rather than a test-file edit
  eight hours before the draft.

**The golden master now freezes BOTH engines** (`tests/test_draft_golden.py`), and
that is the point: `--legacy-engine` is not a code path that resembles the engine
four rehearsals ran on, it is that engine, proven against a fixture that did not
move. The composed engine takes a different player at 7 of the 16 picks (overalls
32, 49, 52, 129, 132, 149, 152), pinned as an EQUALITY — "at most N" would stay
green while a small variant quietly stopped doing anything. A third fixture
freezes the objective input (`weekly-points-2026-08-30.json`) so the DEFAULT path
is testable with no database; without it the engine that drafts tonight would have
been the only one the module could not check on a fresh clone.

**Standing lesson this item paid for: a cost gate only sees the function it
wraps.** The golden's rollout counter wrapped `survival.rollout_survival` alone,
so the composed drive read 8 calls / 61,440 simulated picks against legacy's 16 /
69,632 — a search that appeared to have SHRUNK BY HALF, when half of it had simply
moved to `rollout_pair_batch`. A cost instrument that reads a relocation as an
improvement is worse than none.

Calendar anchors (draft SCHEDULED 2026-08-10; all confirmed against ESPN's own
`draftSettings`, not operator transcription):
- **2026-08-24, 12:00** — draft order drawn from a hat, then entered in ESPN by
  the commissioner. `draftSettings.orderType` is `MANUAL` and `pickOrder` is
  readable live, so the slot is a **read**, not a transcription. Until it is
  entered, the array ESPN serves is a placeholder — treat any pre-08-24 read as
  provisional.
- **2026-08-31, 19:00 PT** — draft (`draftSettings.date`); room opens 18:00
  (`availableDate`). **SNAKE, 16 rounds, `timePerSelection` = 90 s** — half again
  the 60 s every Checkpoint-2 rehearsal assumed.
- **2026-09-09 (a Wednesday)** — NFL Week 1 opener, confirmed against the
  ingested `schedules` table; Week 1 runs through 09-14. This is the Phase 3
  hard deadline and the Checkpoint 3 trigger.

Room composition, re-verified live 2026-08-29: **all 10 seats are owned**
(10/10 in `mTeam`; the two ownerless seats attached ~2026-08-08 and ~2026-08-12,
read from `league_teams` history, not reported by ESPN). So the 2.2 opponent
model's `autodraft_fraction = 0.2` — a 2025 fit, 2 of 10 seats autodrafted, both
of them ownerless — is now an **assumption, not an observation**.

**Measured 2026-08-29 and CLOSED: the prior stays at 0.2 unchanged.** The note
that stood here conflated two different quantities, and only one of them is
tunable:

- the room's TRUE autodraft count changes *the board we face* (top-pick share at
  9 moves 41% → 60% between 0 and 3 autodrafters). Unknowable until Monday and
  not a parameter — nothing to set.
- the ENGINE'S BELIEF about that count is the only tunable, and it is
  **decision-irrelevant**: paired on identical room draws (N=80, R=256, live
  board, slot 9), believing anything in 0.0–0.3 recommends the *same player* in
  77–80 of 80 draws at pick 9 and 79–80 of 80 at pick 12, whatever the room
  actually does. Only an implausible 0.4 moves picks, and every pick it moves
  goes to a lower-VOR player — 0.2 sits on the safe side.

Two structural facts that made this worth measuring rather than assuming, and
which remain true: live recalibration re-fits **only `reach_sigma`**, never this
prior, and it does not engage until 20 room picks (`LIVE_RECAL_MIN_PICKS`) — so
picks 9 and 12 are decided *entirely* on the cold-start 2025 priors. That is
fine, because the belief does not matter; it would not be fine if it did.
Numbers and caveats: gitignored `intel/research/autodraft-prior-2026-08-29.md`.

**Draft slot: 9 of 10** (re-read live 2026-08-29 from `draftSettings.pickOrder`
+ `resolve_own_team(SWID)` — the post-hat-draw order, re-confirmed two days out
along with the date, the 90 s clock, SNAKE/MANUAL, and 10/10 owned seats).
Overall picks 9, 12, 29, 32, 49, 52, 69, 72, 89, 92, 109, 112, 129, 132, 149,
152. Draft-night command is `ziggurat draft-web --season 2026
--slot 9` — **no `--pick-order`**: seat ids are arbitrary internal labels and
synced picks arrive positionally, so identity order is provably equivalent (the
16 overall picks are identical either way). That removes what the plan called
the highest-consequence hand-transcription in the system rather than managing
it.

**The executable procedure for draft night — and for every practice run before
it — is [`docs/draft-day-runbook.md`](./docs/draft-day-runbook.md)**: setup,
preflight, launch order, the three flags never to pass, the ONE flag that is a
tool (`--legacy-engine`, rung 0 of the ladder — the first thing to try if the
cockpit looks wrong, and a RE-LAUNCH decision, never a mid-draft one: a resume
across engines is refused by design), the badge and Autopick checks, and the
fallback ladder. **Both remaining acceptance tests
were run 2026-08-29 and PASSED** (§8.3 mid-draft kill; §8.4 refusal, whose push
half a practice draft structurally cannot reach and which was therefore driven
directly — the first live firing of the draft cockpit's ntfy path). The kill
test's finding is the one to carry: rivals ate the top FOUR queue rows in the
four picks between the kill and our turn, so **queue DEPTH is the safety
margin**, and off-turn depth sat below the K_MIN=3 floor 22% of a practice
draft. Draft-night cadence should make that far healthier (~70x more refill
opportunity at 90 s/pick) but that is arithmetic, not measurement. It is treated as an interface,
not prose — `tests/test_draft_runbook.py` re-derives every command it quotes
against the real CLI and checks the userscript versions it names against the
shipped files, so a renamed flag or a bumped script rots it loudly instead of
at 18:45 on draft night.

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
8. **Draft package is quarantined — and retained.** Nothing in `ziggurat/`
   outside `ziggurat/draft/` may import from it, lazily or otherwise
   (enforced by `tests/test_draft_boundary.py`; the CLI's lazy in-body
   imports keep their exemption). **Amended 2026-08-31 (pre-draft, operator
   decision): the package is KEPT after draft day for reuse next season** —
   the original rule's deletion clause is retired; the import quarantine was
   always the load-bearing half (it is what let draft/ ship fast without
   polluting the permanent core, and it stays). Two consequences, recorded
   so they are not re-derived: (a) `backtest/` importing `draft/` is legal —
   it grades draft decisions; the violation recorded in 3.11a for
   `backtest/draft_backtest.py` dissolves under this amendment (the sin was
   depending on code slated for deletion, and nothing is). (b) Retention is
   NOT next-August readiness: the userscripts pin ESPN's 2026 draft-room
   DOM, the opponent priors are 2025-room fits, and the goldens freeze the
   2026-08-30 board — next season's draft prep starts with recalibration
   and DOM re-verification, never with "the suite is green." If a permanent
   module ever needs something living in `draft/` (e.g. `resolver.py`'s
   curated alias maps), it is PORTED out, never imported.

## Repo map

```
ziggurat/            the Python package (deterministic tools)
  data/              ingestion clients + as-of data access (asof.py, store.py)
  core/              scoring.py (single source of truth), valuation, signals
  league/            league state sync (source/state/sync), opponent layer, Monte Carlo
  draft/             draft tool (Phase 2) — retained across seasons, import-quarantined (Rule 8, amended 2026-08-31)
  push/              scheduled briefing/alert delivery (item 3.6): outbound.py is
                     the ntfy egress choke point + Rule-5 scrub; runs.py = run-log +
                     dedup ledger; run.py orchestrates (imports core, never vice versa)
  llm/               the LLM routing interface (router.py, backends.py — claude_cli live)
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
scripts/install-push.sh                   # item 3.6 — Wed briefing + 20-min alert tick
# re-run install-league-sync.sh on any box where it is ALREADY installed: the 3.1b
# audit corrected that unit's no-op After=network-online.target and its restart limiter.
# install-push.sh needs NTFY_TOPIC in .env (a high-entropy string = the topic password);
# the outbound scrub is what keeps a public topic safe. Test first without pushing:
#   ziggurat brief run --no-push --no-llm   ;   ziggurat alerts run --no-push
loginctl enable-linger "$USER"            # or every timer dies at logout
.venv/bin/ziggurat league status          # last run + UNRECOVERABLE missing days
.venv/bin/ziggurat ingest status          # per-source last successful pull + staleness
.venv/bin/ziggurat brief status           # item 3.6 — last briefing runs
.venv/bin/ziggurat alerts status          # item 3.6 — last alert ticks ('empty' is healthy)

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

The in-season loop, keyed by day. "Run the Tuesday workflow" means: do the
steps under **Tuesday**, in order, and journal the result — each day below is
executable by a fresh session from this file alone. Two ESPN facts shape the
rhythm (re-verified against the live `mSettings` pull 2026-08-27): waivers
process ~3–4 AM PT **every morning except Tuesday** (`waiverProcessDays`; the
Wednesday run is the week's big batch because the weekend's locked players
clear then), and the waiver order **resets every week to inverse standings**
(`waiverOrderReset` — NOT rolling move-to-back), so priority is
use-it-or-lose-it within the week and **Tuesday claims are free — queue
liberally**; what clears waivers is first-come-first-served, so **Wednesday
morning is when speed matters**.

**Division of labor:** the tools recommend and explain; only the operator acts
in the ESPN app (claims, adds, lineup changes, IR moves). Every workflow ends
by writing what was decided and why to the week's journal.

**Journal discipline:** one file per NFL week at `intel/weekly/<season>-wkNN.md`,
created from `intel/weekly/week-TEMPLATE.md` when the week's first decision is
made (preseason dry-runs, when no NFL week exists yet, journal to
`<season>-wk00.md`). Log decisions the day they happen — **a workflow that
decided to do nothing is still journaled as that decision**, and gets
retro-graded like any other. Monday grades on **process, not outcome**
(correct-but-unlucky is variance, not error — and wrong-but-lucky is still
wrong).

**Every workflow starts with the same preflight:**

```bash
.venv/bin/ziggurat league status   # last sync + UNRECOVERABLE missing days
.venv/bin/ziggurat ingest status   # per-source staleness (a stale read is Rule-1-invisible)
.venv/bin/ziggurat alerts status   # the 20-min tick is alive ('empty' is healthy)
```

Missing league days go in the journal (that history is gone — item 3.1). A
source that `ingest status` calls stale and that feeds today's decision gets
disclosed alongside the recommendation, not silently priced through.

Two output notes: (1) `'empty' is healthy` refers to empty alert TICKS — the
distinct string `no push runs recorded yet` means the push layer has never run
on this box (not installed, or `NTFY_TOPIC` unset; see `scripts/install-push.sh`
under Dev workflow). Until that is fixed, Wednesday step 1 has no briefing to
read — journal that, don't treat the silence as healthy. (2) Run timestamps
print in UTC; judge "did today's sync land" by the `snapshot <date>` line, not
the run timestamp — UTC rolls past midnight hours before a Pacific evening does.

### Tuesday — roster legality + waiver claims
1. Preflight. If today's league sync hasn't landed, run
   `.venv/bin/ziggurat league sync` — Tuesday reads today's rosters.
2. `.venv/bin/ziggurat waivers --reasons --claim-budget 10` — the deeper
   budget is deliberate: the cap TRUNCATES each claim list and nothing prints
   past it, so Tuesday (when claims are free) never runs at the quick-scan
   default of 3. The roster-legality precheck runs FIRST and is the point:
   ESPN blocks ALL transactions while a roster is illegal, and Tuesday's
   league-wide status reset is exactly when an IR-slot occupant flips
   Out → Questionable and breaks legality. On a refusal: relay the proposed
   fix (usually a costless IR move) to the operator, re-sync after they apply
   it, re-run.
3. Cross-reference `.venv/bin/ziggurat candidates --reasons` for the breakout
   context behind each add. Where the two disagree, surface the disagreement —
   never smooth it over.
4. Recommend the final claim list with drops. Claims are free: queue every
   positive-marginal claim shown — and if the LAST claim listed is still
   clearly positive, re-run with a deeper `--claim-budget` (the list may be
   truncated, not exhausted). The operator submits in the app before the
   overnight batch.
5. Journal each claim: add, drop, the tool's stated reasons verbatim, and what
   would make it wrong. A no-claim Tuesday is journaled as the decision not to
   claim.

### Wednesday — post-waiver scan
1. The 06:00 PT briefing (timer) is on the phone; the full text is in
   `intel/weekly/briefings/`. Read it.
2. `.venv/bin/ziggurat league sync`, then compare the roster against Tuesday's
   journal: which claims won, which lost.
3. `.venv/bin/ziggurat waivers --reasons` again — the pool has re-formed, and a
   FREEAGENT-status add is first-come: flag anything worth grabbing NOW.
4. First lineup pass: `.venv/bin/ziggurat lineup --reasons`. Note the GTD /
   Questionable starters to track through the practice week.
5. Journal claim outcomes, grabs made or passed on, and the provisional lineup.

### Thursday–Saturday — monitoring
- The 20-minute alert timer covers breaking news. When a push fires, evaluate
  with `.venv/bin/ziggurat waivers` / `lineup` — never react on the headline.
- Injury reports: Wed–Fri practice participation is trajectory; **Friday's
  designation is ground truth.** Re-run `.venv/bin/ziggurat lineup --reasons`
  after Friday's reports.
- Before the week's FIRST kickoff (usually Thursday night — but check the
  schedule; the 2026 opener is a Wednesday), settle any starter playing in it.
  Slot-lock discipline is in the tool: earliest kickoffs in dedicated slots,
  FLEX preserved for the latest-locking player.
- Streaming week for D/ST or K: `.venv/bin/ziggurat stream --reasons`.
- Journal anything acted on; an empty day is fine.

### Sunday — inactives + final lineup
1. Inactives surface ~90 minutes before each kickoff wave. Run
   `.venv/bin/ziggurat lineup --reasons --now "<current ET ISO datetime>"` —
   the real clock matters: the GTD contingency ladder only offers swaps whose
   window is still open (`window_closed` means that door shut).
2. Walk the ladder for each Questionable starter in lock-time order; execute
   the branch matching the news.
3. A hard refusal from the seated-lineup legality check (bye/OUT starter) on a
   Sunday morning means something upstream was missed all week — journal it as
   an incident, don't just fix the slot.
4. Journal the final lineup and any late swaps with their triggers.

### Monday — retro & journal close
1. `.venv/bin/ziggurat league sync` — capture the completed week.
2. Retro every logged decision (template's Monday section): process first,
   outcome second, verdict one of good call / variance / lucky / error.
3. Anything observed that smells like a repeatable lesson gets written to
   `intel/heuristics.md` as an **observation** (promotion rules land with 5.2).
4. Skim `.venv/bin/ziggurat brief status` and `.venv/bin/ziggurat alerts status`
   for the week's push-layer health; a missed briefing is a process finding.

### Every ~4 weeks — memory compaction
Distill the accumulated journals into `intel/rest_of_season_priors.md`
(scheduled synthesis, not an emergency measure): standing lessons, opponent
tendencies, calibration notes. Raw journals stay archived out of the default
reading path.

**Before Week 1** (pre-draft/preseason): `waivers`, `lineup`, `marginal`, and
`stream` run but disclose empty-roster reality honestly (`waivers` prints "no
roster rows at this as-of", `lineup` seats a greedy empty-context lineup) —
correct, not broken. `candidates` is the exception: it EXITS with "no REG week
is fully played and knowable" until a real week completes, so Tuesday step 3
and any other `candidates` step is skipped before Week 1. The cadence starts
for real the Tuesday after the draft.

## Heuristics promotion criteria

> **PLACEHOLDER — lands with item 5.2.** Ladder: observation → hypothesis
> (tracked, not applied) → rule (applied, evidence cited), in
> `intel/heuristics.md`. Backtest findings are strong priors; in-season
> evidence must be repeated and strong to override them.
