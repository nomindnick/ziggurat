# backtest/ — experiments & replay harness

Phase 4. Imports `ziggurat/` directly and replays history through the
*production* code paths — that's the point of the repo-wide `as_of`
discipline. Nothing here is imported by `ziggurat/`; `backtest/` may import
`ziggurat/draft/` (Rule 8, amended 2026-08-31: it grades draft decisions).

Standing methodology for every experiment: strict as-of cuts on all inputs
(including podcast publish dates), train on 2021–23 / validate on 2024–25,
grade decisions not outcomes, precision@k for k ≤ 3 (the realistic weekly
claim budget).

## What is here

| module | what it does |
| --- | --- |
| `draft_backtest.py` | The draft backtest: runs the pick engine in the operator's seat against the calibrated room on each of the 2021–25 preseason consensus boards and grades every drafted roster on REALISED weekly house points (pooled AND season-block intervals, always both). It measures roster construction, not player evaluation — the module docstring says why. |
| `replay.py` | The weekly replay harness — the DECIDE phase. Runs the production candidate generator (`ziggurat.core.candidates.build_candidates`) at each week's decision clock (the first Tuesday strictly after the week's last REG game) and freezes what it picked. `python -m backtest.replay` is the CLI. |
| `decisions.py` | The freeze: `ReplayParams` (hashed into the cache key — including the generator's floors), `WeekRecord`/`Decision`, the JSONL + sha256 manifest writer/reader, the TRAIN/HOLDOUT split and the holdout lock. |
| `scorecards.py` | The GRADE phase, pure over a freeze: precision@k against the FantasyPros weekly (`wp`) and rest-of-season (`ros`) ECR pages, an eligibility-matched null (the whole week-T universe), depth-matched lifts by r0 band, Sleeper ownership corroboration, and the rendered scorecard whose HYPOTHESES block names every threshold and its source. |
| `stats.py` | Wilson, pooled-t and season-block intervals — one implementation, so no report can quietly print only the flattering one. An interval that collapses to a point never earns the `*`. |

Every read goes through the `ziggurat/data` as-of accessors bound to
`base.latest_truth` (the bulk-loaded history is invisible under the default
`historical` view). The database is opened **read-only**
(`file:...?mode=ro`); the harness never writes to it.

## Decide, then grade

Two phases, deliberately separated by a file on disk:

1. **decide** — for every (season, week) in the run, call the generator at
   `as_of(T)`, build the pool (usage-arm rows with a gsis id, skill positions,
   not already priced inside the eligibility window on the week-T `wp`
   page), and let each strategy pick `k`. The picks — reasons verbatim, the
   r0 market reference each pick was read against — are frozen under
   `data/backtest/replay/<params_hash[:12]>/` (gitignored) as one JSONL per
   strategy/season plus a `manifest.json` with a sha256 per file.
2. **grade** — read the freeze back (refused if a digest disagrees), and
   score it against the market at a strictly later `--grade-as-of`. A HIT is
   the player moving up the page by `--hit-places` by the second post-flag
   scrape; **lead 1 is the same scrape as the market's first re-rank
   (concurrent — it does NOT beat the market), lead 2 is one full scrape
   ahead (it does)**; a bye at lead 1 defers to lead 3 and is counted apart.
   A lead page scraped at or before the decision clock is `reference
   precedes` and ungradeable, never a hit or a miss.

What was decided at `as_of(T)` cannot change when the future lands in the
database — that is the guarantee `tests/test_backtest_replay.py` pins (a
next-week page, box score, ownership row and a post-clock injury report are
all added and the frozen digest must not move).

## TRAIN / HOLDOUT and the lock

`decisions.TRAIN_SEASONS = (2021, 2022, 2023)`; `HOLDOUT_SEASONS = (2024,
2025)`. The harness **refuses to decide on or grade a holdout season** —
at the CLI, in `replay()`, `decide_week()`, `decisions.load()` and
`build_scorecard()` alike — unless `--unlock-holdout` (`unlock_holdout=True`)
is passed explicitly. The refusal happens before the database is opened.
Every unlocked run is appended, **after** it completes (publish-then-record:
a failed run claims nothing), to the gitignored ledger
`data/backtest/replay/holdout-unlocks.jsonl` — timestamp, params hash,
seasons, phase and argv. Report on holdout; never tune on it. The
`--breakout-floor` / `--emergence-floor` tuning flags change the cache key
and are for TRAIN runs.

## The done-when command

```
python -m backtest.replay --seasons 2023 --strategy signal_topk --k 3
```

is re-runnable: it checks the freeze before deciding, reuses one whose files
verify against the manifest (and says so on stdout), and grades. `--force`
re-decides over an existing freeze; a freeze that does not verify is refused
by name, exit 2. `--grade-only` grades an existing freeze without touching
the generator. Other shapes:

```
python -m backtest.replay --seasons 2021-2023 --strategy signal_topk,random_k,volume_topk --k 3
python -m backtest.replay --seasons 2023 --strategy signal_topk --k 3 --grade-only --reasons
python -m backtest.replay --seasons 2021-2025 --strategy signal_topk,random_k,volume_topk --k 3 --unlock-holdout
python -m backtest.replay --seasons 2023 --strategy signal_topk --k 3 --breakout-floor carries=8 --emergence-floor targets=6
```

Exit codes: 0 graded; 2 refused (holdout locked, a freeze that does not
verify, a grade-input refusal, or zero decisions for a season) with the
reason on stderr — never a traceback.

## Reading a scorecard

Raw lifts (`pooled` / `block`) are against the eligibility-matched null: the
whole week-T universe, NOT depth-matched, so they are **not comparable across
strategies that pick at different r0 depths** — a strategy that picks unranked
players plays against a base rate that is far higher. The `DEPTH-MATCHED`
lines subtract each pick's own r0-band null rate and are the numbers to
compare strategies (and, in 4.2, to tune) on. `*` marks an interval that
excludes zero; per-pick intervals are pseudo-replicated (picks inside a season
share a market), the season-block interval is a t on `df = seasons - 1`.
