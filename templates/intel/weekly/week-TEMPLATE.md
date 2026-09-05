# Week NN (season YYYY) — decision journal

<!--
Copy this file to intel/weekly/<season>-wkNN.md when the week's first decision
is made (e.g. intel/weekly/2026-wk03.md). One file per NFL week.

Two disciplines, from SPEC.md:
- Log a decision the DAY it is made, while the reasons are the real reasons.
- The Monday retro grades PROCESS, not outcome: was the call right given what
  was knowable at decision time? Correct-but-unlucky logs as variance, not
  error; wrong-but-lucky is still wrong.
-->

## Week facts
- Matchup: vs <opponent team> (favorite / underdog / close, per `ziggurat lineup`)
- Data health at decision time: <preflight flags: stale sources, missing sync days, or "clean">

## Decision log
<!-- One block per decision, numbered D1, D2, … Include decisions NOT to act
     when the tools recommended acting — those get retro-graded too. -->

### D1 — <short name> (<day, date>)
- **Decision:** <what was done, or deliberately not done>
- **Tool said:** `<command>` → <the printed reasons, quoted not paraphrased>
- **Alternatives considered:** <and why they lost>
- **What would change my mind:** <the observable that flips this decision>

## Submitted claims & departures (Tuesday)
<!-- Item 4.2b. The freeze records what the TOOL printed; only this block records
     what YOU DID, and the two are joined by capture_id. ESPN stamps a won claim
     and a grab you made yourself both as `ADD` on the same day, so without this
     block Wednesday cannot tell them apart. Fill it the night you submit —
     THIS BLOCK IS THE RECORD. (`ziggurat decisions record` lands with item
     4.2b B12 and does not exist yet; until it does, nothing reads this block
     but you.) -->

- **Tool run:** `<the exact command, including --as-of and --claim-budget>`
- **capture_id:** <from the freeze line the run printed>
- **Wall clock (PT):** <when the run happened>
- **Chain total the page printed:** <the "IF EVERY CLAIM AND GRAB LISTED WINS" line, verbatim>
- **Why the list ended, quoted:** <the tool's own stop sentence, verbatim — then mark it VERDICT (the next add is worth nothing or less) or BOOKKEEPING (pairs ran out / position limit / pricing ceiling)>
- **Submitted in the app at:** <HH:MM PT — the batch runs 00:01-01:13 PT, so this must be before ~23:59 PT>

<!-- ONE ROW PER PRINTED LINE, submitted or not. Both espn_ids because two
     players can share a display name; both gain columns because since 2026-09-02
     a bare `gain` is CONDITIONAL and `gain_alone` is what the line is worth if
     the ones above it lose. -->

| # | kind | add + POS | add espn_id | drop + POS | drop espn_id | gain | gain_alone | submitted? | departure reason |
|---|------|-----------|-------------|------------|--------------|------|------------|------------|------------------|
|   |      |           |             |            |              |      |            |            |                  |

- **Departures — printed but NOT submitted:** <which #, and why. "none" is a valid entry.>
- **Departures — submitted but NOT printed:** <what you added off your own read, and why. "none" is a valid entry.>
- **Departures — order changed:** <if you queued them in a different order than the printed #. "none" is a valid entry.>
- **Did USAGE / ROLE EVIDENCE change anything?** <yes/no — and if yes: which player, his NEW or REPEAT badge, and what it changed. A **no** week is still a data point: that column does not change the claim order, and this field is the only record of whether it changed YOURS.>
- **Projection-only chain:** <"identical to the page above", or the diff>
- **Outcome (fill in Wednesday):** <per #: WON / LOST / not processed — plus your new waiver priority>

## Sunday late swaps
<!-- GTD contingency branches executed: the trigger news, the clock time, the
     swap. "None" is a fine entry. -->

## Monday retro
<!-- One block per D-number. Grade the process first, then look at the outcome. -->

### D1
- **Process:** sound / flawed — <why, judged only on what was knowable then>
- **Outcome:** <what actually happened>
- **Verdict:** good call / variance (right call, bad bounce) / lucky (wrong call, good bounce) / error

### Week-level
- **What the process missed (and what catches it next time):**
- **Candidate heuristics observed** (write as observations in `intel/heuristics.md`; promotion rules land with item 5.2):
- **Cadence/tooling friction this week** (feeds Checkpoint 3 and the plan):
