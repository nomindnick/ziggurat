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

## OPEN TRIGGERS (read after the preflight, before any decision tool)
<!-- Mid-season review 2026-11-05 (CLAUDE.md preflight). One row per PASS (we
     declined a #1-#2 chain line), OVERRIDE (we went against a tool's top line),
     HOLD-RULE (a written rule with its trigger) and DROP (an injured, suspended
     or exempt player we released in the last 14 days, with a known return).
     The premise is something CHECKABLE (player, field, threshold). Each session
     writes "unchanged" or names the changed fact; only a named changed fact
     re-opens a decision. DROP rows: Saturday-morning news read with
     intel/scripts/drop_news.py, and again in the Sunday pre-lock pass. Carry
     open rows forward into next week's file; close a row by naming why. -->

| # | kind | player / rule | premise (checkable) | trigger → action | opened | last read | status |
|---|------|---------------|---------------------|------------------|--------|-----------|--------|
|   |      |               |                     |                  |        |           |        |

## Decision log
<!-- One block per decision, numbered D1, D2, … Include decisions NOT to act
     when the tools recommended acting — those get retro-graded too. -->

### D1 — <short name> (<day, date>)
- **Decision:** <what was done, or deliberately not done>
- **Tool said:** `<command>` → <the printed reasons, quoted not paraphrased>
- **Alternatives considered:** <and why they lost>
- **What would change my mind:** <the observable that flips this decision>
- **Override?** <no — or: the tool line overridden, the TRIGGER that would prove the override wrong, and the number it would need to be right. An override with no trigger is a finding whatever the outcome.>
- **Deadline:** <the ESPN clock this decision depends on: his kickoff (IR entry) / ~23:59 PT Tue (claims) / Wed morning (grabs); moved earlier if it falls 19:00-21:00 PT>

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

| # | kind | add + POS | add espn_id | drop + POS | drop espn_id | gain | gain_alone | card-start? | submitted? | departure reason |
|---|------|-----------|-------------|------------|--------------|------|------------|-------------|------------|------------------|
|   |      |           |             |            |              |      |            |             |            |                  |

<!-- card-start? (journal-only, a TRACKED hypothesis that decides nothing until
     graded; intel/heuristics.md H-g): for a small line, would the add change our
     CARD lineup in a named week within 2 weeks? "yes wkNN" / "no". -->

- **Departures — printed but NOT submitted:** <which #, and why. "none" is a valid entry.>
- **Departures — submitted but NOT printed:** <what you added off your own read, and why. "none" is a valid entry.>
- **Departures — order changed:** <if you queued them in a different order than the printed #. "none" is a valid entry.>
- **Did USAGE / ROLE EVIDENCE change anything?** <yes/no — and if yes: which player, his NEW or REPEAT badge, and what it changed. A **no** week is still a data point: that column does not change the claim order, and this field is the only record of whether it changed YOURS.>
- **Projection-only chain:** <"identical to the page above", or the diff>
- **Outcome (fill in Wednesday):** <per #: WON / LOST / SUB-FAILED (by design: a same-drop substitute that fails because the line above it won) / not processed — plus your new waiver priority>

## Sunday late swaps
<!-- GTD contingency branches executed: the trigger news, the clock time, the
     swap. "None" is a fine entry. -->

## Monday retro
<!-- One block per D-number. Grade the process first, then look at the outcome. -->

### D1
- **Process:** sound / flawed — <why, judged only on what was knowable then>
- **Outcome:** <what actually happened — label every number PROJECTED or REALISED; they are not commensurable>
- **Verdict:** good call / variance (right call, bad bounce) / lucky (wrong call, good bounce) / error
- **Bust case (required for "good call"):** <the plausible outcome under which this same call would have lost. No bust case → grade it "lucky", not "good call".>
- **Override-with-trigger:** <n/a — or the override's trigger, and whether it fired>
- **Result attribution (only if the result is within 1 SD = 24.22 of the card):** <symmetric: say what luck did FOR us as readily as against us>

### Week-level
- **Unverified claims this week:** <count, and each statement of fact made without quoting a table or the tool's own text — [ ] checked>
- **Rulers broken:** <n / none — a counterfactual ruler applied inconsistently (bye-filled baseline, claim-time board, house card for our lineups, near ties as coin flips)>
- **What the process missed (and what catches it next time):**
- **Candidate heuristics observed** (write as observations in `intel/heuristics.md`; promotion rules land with item 5.2):
- **Cadence/tooling friction this week** (feeds Checkpoint 3 and the plan):
