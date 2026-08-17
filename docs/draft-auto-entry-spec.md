# Draft-day auto-entry — build spec

**Status:** P0 gate PASSED 2026-08-15 (§4) — building. Approved 2026-08-13.
**Deadline:** draft is **Mon 2026-08-31 19:00 PT** (room opens 18:00). Draft
order is drawn 2026-08-24 12:00. 16 rounds, SNAKE, **90 s per pick**.
**Machine:** the desktop (`framework-desktop`) — see `runbook-strix-halo.md` §4.1.
**Rule 8:** everything here lives in `ziggurat/draft/` and is deleted after
draft day. Nothing outside may import it.

This document is the reference for building the thing. The feasibility
evidence behind it is in `IMPLEMENTATION_PLAN.md` (Checkpoint 2 notes,
2026-08-12) and the probe is `ziggurat/draft/espn_probe.user.js`.

---

## 1. Why this exists — the operator constraint

The draft starts at 19:00, which is the start of toddler bedtime. The operator
can be present for the first few rounds (bath is staffed by their wife, then
getting the kid dressed is a two-person job), is likely **unavailable from
roughly an hour in**, and has said they will simply ignore a push once they
are unavailable.

Two hard consequences, and both shape the design more than anything technical:

- **The system must be fully autonomous from roughly round 5 onward.**
- **A push is best-effort, never a control-flow dependency.** No design may
  require the operator to respond in order to stay correct.

## 2. Design: maintain the queue, never click Draft

**The script does not commit picks.** It keeps ESPN's **Pick Queue** populated
and correctly ordered with the engine's live recommendations, and lets the
operator's clock expire so **ESPN's own autopick** commits from that queue.

Rationale — every dangerous failure lives in the commit path (selection state,
mis-targeting, double-fire), and all of it disappears if we never click Draft.
The queue is a *visible, verifiable, reversible* data structure: you can look at
it and see exactly what will happen before it happens.

Failure modes degrade benignly:

| failure | outcome |
|---|---|
| script dies | last good queue stands; autopick uses it |
| resolution ambiguous | refuse that player, keep previous queue |
| ESPN rebuilds their bundle | falls back to today's manual plan; nothing lost |
| cockpit down | queue is stale but still Ziggurat-ordered |

**Expected value, from our own numbers** (2.2/2.3 tournaments): follow-VOR beat
follow-ESPN by ~+128; the full pick engine added +22…+53 *on top of* follow-VOR.
A live-resorted queue captures most of that gap, because the ordering is
recomputed by the real engine between picks — only the commit mechanism differs.
Active clicking buys the remainder at all of the risk. **Not worth it as v1.**

Active Draft-clicking is a **stretch goal**, only if the queue layer is
rehearsing flawlessly well before 08-31.

---

## 3. What is PROVEN (2026-08-12 probe, live practice drafts)

- **ESPN accepts untrusted synthetic clicks.** A bare `element.click()` —
  ladder level 1 of 4, no pointer synthesis, no React-fiber poking — drives the
  real controls. React 16 delegation, `onClick` at depth 0, no `isTrusted` check.
- **Queue add works**, confirmed off-turn twice with named targets
  (T. Higgins, J. Price entered the panel; nothing else populates *your* queue).
- **Card-bound commit works**, three exact-target picks (Flowers p8, Irving p13,
  Tuten p33), all at L1, clock at fire 00:16 / 00:19 / 00:22 — i.e. **not**
  expiry autodraft.
- **No REST write path exists.** Live picks ride a private WebSocket, so a
  userscript is the only option — but it is a sufficient one. No Playwright/CDP.

### Selectors (verified)

| control | selector | notes |
|---|---|---|
| queue add (row) | `button.Button--queue` | present **only off-turn** |
| draft (row) | row action button reads `Draft` **on-turn** | third commit path, no modal |
| draft (card) | `button.Button--draft.PlayerCard__action-btn` | player-bound; the safe commit |
| draft (header) | `button.Button--alt.Button--draft` | **NEVER USE** — see §5 |
| queue panel | `.pick-queue` | `empty` class when no entries; `with-footer` when full |
| **queue rows** | `.pick-queue [class*="Table__TR"]` | **NOT** FixedDataTable; header + empty-state rows must be filtered |
| **queue remove** | `button.Button--dequeue` (text `Remove`) | **hover-gated** — retry the hover, see §4a |
| **undo (drafted)** | `button.Button--undo` | marks an ALREADY-DRAFTED player's row |
| player search | `input.form__control[placeholder="Player Name"]` | drive via the native value setter + `input` event |
| player rows | `[class*="fixedDataTableRowLayout_main"]` | virtualized; **nodes are recycled**, see §4a |
| pick history | `.pick-history` | existing sync script harvests this; readable *simultaneously* with the queue panel |

---

## 4. P0 — PASSED, 2026-08-15 (live practice drafts)

The gate is cleared. All three questions answered on real rooms.

- **Q1 — REMOVE WORKS.** `button.Button--dequeue` (text `Remove`), driven by a
  bare `element.click()` (L1). The named entry leaves; every other entry
  survives. Confirmed on two independent runs plus three more removals inside
  the rebuild test, which **cleared a 3-entry queue in 802 ms**.
- **Q2 — ADD APPENDS.** Five names entered in order landed `0/1, 1/2, 2/3,
  3/4, 4/5`. Add order *is* queue order, so **clear-and-refill sets any
  order**.
- **Q3 — REORDER WORKS**, via **HTML5 drag-and-drop**. Rows carry
  `draggable=true` and `onDragStart/onDragOver/onDragEnd` at depth 0; a
  synthesized `dragstart → dragenter → dragover → drop → dragend` with a real
  `DataTransfer` swapped the top two entries exactly as requested. Q2 makes
  this optional, but it is available if incremental edits beat full rebuilds.
- **P1 — SEARCH WORKS.** ESPN's own filter (`input.form__control`,
  placeholder `Player Name`) is drivable via the prototype's native value
  setter plus a bubbling `input` event, and an off-screen target renders
  within ~1.4 s. **The queue writer must clear the box afterwards** — a live
  filter starves every later lookup.

### 4a. Three hazards the probe measured, which the writer must handle

1. **Row nodes are recycled.** A lookup for `mullens` ended up holding a row
   that by then read *Amon-Ra St. Brown*. **Re-resolve and re-verify the row
   immediately before clicking**, and treat "queue grew, but not with our
   target" as a wrong-player event to be undone — never as a no-op.
2. **The Remove button is hover-gated and the synthetic hover is flaky.** One
   inspection found it on exactly one of three rows — the one under the
   operator's real cursor. Retry the hover (3 attempts, increasing settle)
   before concluding the control is absent.
3. **On your turn, the queue row's action button is `Draft`.** Anything that
   clicks in a queue row must blacklist `Button--draft`/`Button--undo`/
   `Button--queue` and skip disabled nodes. This is not hypothetical: it cost
   a real practice pick — see §5c.

### P2 — CONFIRMED 2026-08-16 (first live mock, room 1506119378)

**Clock expiry drafts from the queue.** The proof is statistical, from the
journal replay: the operator's expiry picks included players at **#12, #18,
#21, #25, #29 and #35 on ESPN's own best-available board** — depths ESPN's
native autopick never reaches — while every pick appears in the engine's
top-8 at its moment. The room's `Autopick` toggle was **ON** (verified in the
live DOM: `.autoPick-container input.form__control--toggle`, `checked: true`).
Per the operator's debrief, **all 16 picks were queue-fed: the queue was
sometimes empty when his turn STARTED but always refilled within seconds and
was never empty at expiry.** Pick 13 (ESPN's #1 but the engine's desired[4])
was initially misread as the empty-queue fallback and was actually a **stale
queue head** drafted from the queue — head-staleness under a fast room is a
real, measured fidelity drag. **Run 2 (2026-08-16, room 1908085605) then
witnessed the fallback itself**: with the queue starved empty by the
position-filter gate (§6d), expiry at picks 113/133/148 took ESPN's own
need-shaped best-available — the benign degradation §2 assumed, now
observed. Whether expiry-from-queue survives the toggle being OFF is still
unmeasured — the writer treats an `off` reading as an alarm (badge warning).

---

## 5. The targeting trap (do not regress)

Committing is easy; **targeting is not**. The header Draft button is enabled on
your turn *regardless of what is selected*, and a plain row-cell click does
**not** move ESPN's selection. An early probe version therefore drafted the
default best-available three times running (Brown, McMillan, Loveland) while
reporting success.

Rules that follow, for any code that ever clicks Draft:

- Commit **only** through a player-bound control (the card button).
- **Never** fall back to the header button. The probe now aborts instead.
- **Verify after commit**: the newest pick-history row must name the intended
  player. Anything else is an incident.

## 5b. Measurement discipline (this cost three false CONFIRMEDs)

Three consecutive probe rounds reported success falsely. A pick landing under
your team after a click has two innocent explanations a pick-history detector
cannot distinguish from a real success: **the operator's own rescue click**
(each ladder level waits 2.5 s, so a hand anywhere in that window is credited
to whichever level was running) and **expiry autodraft**, which takes
best-available and so looks entirely plausible.

Two principles, both of which must survive into the build's tests:

1. **Prefer an experiment whose effect nothing else in the environment can
   produce.** The queue chain runs off-turn: no clock, no autodraft, and
   nothing else fills *your* queue.
2. **Witness the confounder directly.** Record the draft clock at fire time;
   autodraft fires only at 0:00.

The tell that was missed for three rounds: the "working" ladder level wandered
L1/L2/L3 across runs. **A real mechanism does not move.** Treat inconsistent
success levels as evidence of contamination, not flakiness.

## 5c. It happened again, and the lesson generalised (2026-08-14)

The P0 probe reported `REMOVE WORKS` for a click that had actually **drafted**
the player. On the operator's turn the queue row's action button is `Draft`;
the candidate ranker's "any small button" fallback matched it; and the
verdict's conjunction — *target left the queue AND the others survived* —
passed, because drafting a queued player also removes him from the queue. It
cost a practice pick.

The pre-click check for "was he already drafted?" ran, and passed. The
confounder that fired was **our own click, afterwards**.

> **The rule:** a confounder check must run *after* the action, not only
> before it. Checking the starting state proves the world was clean when you
> began; it says nothing about what you just did.

Two corollaries now in the probe, and both belong in the writer:

- **Verify with an identifier the other side actually uses.** Both post-click
  checks searched for the queue's abbreviated display name (`B. Sauls`), which
  can never match pick history or the grid (`Ben Sauls`) — so both returned
  clean *by construction*. They now match on surname; over-matching is the
  safe direction, since it can only make a check fire more often.
- **A destructive test must be structurally incapable of the destructive
  act.** The remove path now refuses to click a draft/undo/queue button at
  all, rather than relying on ranking to prefer the right one.

---

## 6. Architecture

Everything already exists except the queue writer.

```
ziggurat draft-web  (cockpit, 127.0.0.1, token-authed)
    │
    │  GET /api/state  → { is_operator_turn, overall_pick, taken[],
    │                      recs[ {player_id,name,position,team,
    │                             pick_score,reasons[]} ] }
    │  POST /api/sync  ← picks harvested from .pick-history   [EXISTS]
    │
    └── Tampermonkey userscript in the ESPN draft tab
          ├── harvest picks      → POST /api/sync             [EXISTS: espn_sync.user.js]
          └── QUEUE WRITER       ← GET /api/state             [BUILD THIS]
```

The queue writer is a **pure consumer of `/api/state`**. It holds no board
logic — all ranking stays in the engine, server-side, where it is tested.

### Reconciliation loop (the whole feature)

```
every N seconds, and after every pick observed in .pick-history:
  desired  = top K available recs from /api/state   (K ≈ 5–8)
  actual   = players currently in .pick-queue, in order
  if actual == desired: done
  else: rebuild — Remove all stale entries, then add `desired` in order
  verify: re-read .pick-queue and assert it matches `desired`
  if it does not match after one retry → REFUSE (§7)
```

**Never leave the queue empty mid-rebuild if avoidable** — sequence removals
and additions so a valid queue exists at all times, because the clock can
expire at any moment.

### Components to write

- ~~`ziggurat/draft/espn_queue.user.js` — the queue writer~~ — **BUILT
  2026-08-15** (v1.1 after a 35-agent audit; §6b below). Served at
  `/queue.user.js`, same `{{PORT}}`/`{{TOKEN}}` templating as sync.
  **Code-complete but live-unrehearsed** — §8.2/§8.3 are what ship it.
- ~~Cockpit endpoint `GET /api/queue`~~ — **BUILT 2026-08-15** (§6a below).
- ~~Cockpit endpoint `POST /api/queue/status`~~ — **BUILT 2026-08-15**,
  log-only: records the report + a consecutive-failure streak; the
  push-on-streak decision is step 4's, deliberately not made yet.
- Push on refusal via the **existing** `ziggurat/push/` egress choke point
  (Rule 5 outbound scrub applies; `draft/` → `push/` is a legal direction).

### 6a. The served contract (step 2, built 2026-08-15)

`GET /api/queue?k=N` (loopback, unauthenticated read, like `/api/state`):

```json
{ "epoch": "…", "overall_pick": 12, "complete": false,
  "is_operator_turn": false, "queue_for_overall": 17,
  "picks_until_operator": 5, "k": 8, "caveats": ["…"],
  "desired": [ { "player_id": …, "name": …, "espn_name": …, "position": …,
                 "team": …, "pick_score": …, "reasons": […], … } ] }
```

Rules the writer may rely on, each pinned by a test:

- **`desired: []` means exactly "no operator pick remains"** (`queue_for_overall`
  null: draft complete, or every remaining pick is a rival's). An engine failure
  is a **500**, never an empty list — so the writer must treat `[]` as *inert*
  and a non-200 as a no-op; neither is ever "clear ESPN's queue".
- **Deterministic per state**: the response changes only when a pick lands
  (exact pick-sequence cache; an *edit* of a past pick recomputes). The writer's
  diff-then-rebuild loop cannot oscillate between polls.
- **On the operator's turn `desired` extends the cockpit's recommendation panel
  exactly** — same ctx, same seeded rng, bit-identical (the server half of
  acceptance test §8.5).
- **`k` is clamped to [3, 10] and is a CAP, not a promise**: depth is bounded by
  the engine's own candidate window (`candidate_width`, default 5 ⇒ typical
  depth 6–9). Deliberate — widening the window only for the queue could re-rank
  the head and make ESPN's autopick diverge from the on-clock panel.
- Off-turn, `desired` prices the operator's **next** pick (`queue_for_overall`)
  with today's taken set — the board is slightly richer than it will be by then;
  each refresh after an observed pick converges it, and depth-K absorbs snipes.
- **`espn_name` is the writer's search/verify vocabulary** — ESPN's OWN display
  text ("Texans D/ST", "Hollywood Brown"), joined from the stored ESPN board at
  the `load_board` seam (`simulator.espn_display_names`; skill by espn_id, DST
  by team). Nullable → fall back to `name`. This is §5c applied to the payload:
  match with an identifier the other side actually uses, or the DST divergence
  play silently never executes under green telemetry (audit, demonstrated).
- **`caveats` corrects the off-turn survival reading**: each row's survival
  figure (and its verbatim reason) is the ON-CLOCK vantage at
  `queue_for_overall` — it prices lasting *beyond* that pick, not *to* it, and
  at a wheel target it reads "100% — no rush" while a whole round intervenes.
  The reasons stay verbatim (Rule 6); the response says so at the top level.

`POST /api/queue/status` (token-authed like `/api/sync`): body
`{league, overall, achieved: [names…], ok, reason?, autopick?}` — **`ok` must
be a JSON boolean** (a stringly `"false"` is rejected 400; a coerced True would
silently reset the failure streak step 4 pushes on). `autopick` is the writer's
per-cycle observation of ESPN's Autopick toggle (`"on"/"off"/"unknown"`; junk
records null) — the P2 evidence gatherer. The endpoint **validates the
first-room league binding but never claims it** — the binding belongs to the
pick feed (`/api/sync`) alone; a picks-free telemetry POST that claimed it
could bind a fresh cockpit to `""` before the harvester's first batch and brick
sync for the whole unattended remainder (audit, demonstrated live). Bounded
storage; response carries `bad_streak` (consecutive `ok: false` reports).
Surfaced in `/api/state` under `"queue"`.

### 6b. The queue writer (step 3, built 2026-08-15; v1.1 after audit)

`espn_queue.user.js` runs the §6 loop as an **iterative fix loop**, converging
position by position from the TOP of the queue (the position autopick reads
first): the row at the first wrong position is removed — unless the player who
belongs there is absent entirely, in which case he is ADDED first — so the
queue never drains to zero while a wanted player can still be fetched. One
retry against a fresh `/api/queue` read, then refuse and report (§6).

**Identity is the load-bearing piece, and v1.0 got it wrong** (35-agent audit:
6 criticals, all fixed and node-verified against the audit's own repros):

- suffix-stripped surname + first initial is NECESSARY, never sufficient —
  "Brian Thomas Jr." vs "B. Robinson Jr." both reduced to *(jr, b)* and
  verified green with the wrong player at the queue head;
- **team/position evidence from the row text confirms or contradicts** (the
  queue abbreviates names but renders team+pos; Jameson vs Javonte Williams
  both render "J. Williams" and only the team code separates them);
- **ambiguity → the row is PROTECTED**: never removed, never claimed, reported
  as not-ok (§7 refuse-not-guess). An unreadable D/ST row while any D/ST is
  desired is protected, not stale.

Other audit-paid rules now in the writer:

- **The skip ledger only charges evidence about the player** — one failure per
  player per cycle, nothing on `on_clock`, and a pass where every add fails
  identically charges nobody (that is evidence about the environment). ok
  additionally requires the §7 depth floor (`achieved ≥ min(3, |desired|)`),
  so an emptied queue can never report ok (v1.0's all-skipped state wiped the
  queue and reset the escalation streak).
- **On the operator's own turn: head-fix only.** Adds are impossible on-turn
  (`Button--queue` is off-turn only), so a rebuild could only strip the queue
  while the clock runs. If `desired[0]` is queued below the top, rows above
  him are removed; if he is absent, the writer touches nothing and reports.
- **A HALTED writer keeps reporting** (`ok:false, "writer HALTED …"` every
  cycle) so the server streak keeps growing for step 4 — v1.0 went silent and
  froze the streak at 1 in exactly the state that most needs a push. The §5c
  tripwire matches on full identity, not bare surname (bare surnames made
  false halts LIKELY: every "Jr." collided, every D/ST collided).
- Landing verification = a NEW queue row that matches the added player —
  panel-text substring accepted an existing same-surname row as success.

**Rehearsal must verify two DOM assumptions the audit flagged as unmeasured**
(both fail SAFE — protected rows + degraded reports — but noisy): (1) queue
rows actually render team codes next to the abbreviated name (the namesake
disambiguation depends on it); (2) what a D/ST row looks like in the queue.
Also watch the badge's `autopick ON/OFF` readout — that observation is P2.

### 6c. First live test — 2026-08-16, full 160-pick mock (writer v1.2)

The mock ran END TO END fully hands-off (operator confirmed: no interaction
at all): 36 status reports, sync bound and conflict-free, correct inert
shutdown after the operator's last pick, a legal 16-round roster, and **every
one of the 16 picks committed from the queue at expiry** — the queue was
sometimes empty at a turn's start but never at its end. P2 confirmed (§4).
Three defects found, fixed in v1.3:

1. **DST adds failed all game** (`not_in_pool`): ESPN's player search returns
   nothing for the full display text ("Chargers D/ST"), so the engine's
   **D/ST-early play was silently defeated for six straight turns** (88–133 —
   expiry took the first addable player each time; ESPN itself filled PHI
   D/ST at R16). v1.3 searches the nickname alone ("Chargers") and still
   verifies the full text on the row — **works-on-live is unverified until
   the next mock; if nickname search also fails, the fallback is driving the
   grid's D/ST position tab.**
2. **The Autopick reader always said `unknown`**: the comma-selector matched
   the wrapper div (class contains "toggle") before the input inside it.
   Fixed with the live-verified selector.
3. **The console trail did not survive the session** (Chrome retention kept
   ~12 lines); only the LAST report survived server-side. The cockpit now
   keeps a bounded report history at `GET /api/queue/reports`, so post-run
   analysis never depends on the browser again.

**Open observation, not yet a defect — and sharpened by the debrief:** expiry
picks tracked desired[1]–[3] more often than desired[0] mid-draft, and pick
13 shows the mechanism at its worst: the queue head was the engine's
desired[4] (a stale head — built from a lagged desired list and/or after
transient add failures spliced past the top entries), and that stale head IS
what got drafted. Confounders: the DST hole (desired[0] literally unaddable
for six turns), mock CPU pace (~2–4 s/pick vs ~2 s per add — the real
draft's 90 s human pace is far kinder), and sync lag feeding the writer a
desired list a pick or two behind the room. Re-measure on the next mock with
the report history; §8.5's five-pick fidelity test is the gate.

**Operator debrief (2026-08-16, clarified) + the v1.4 changes it forced:**
the run was FULLY hands-off — no manual queue/draft/interaction at all —
making it a §8.2-shaped complete draft at the mock's 30 s clock (draft night
is 90 s; the mock is the harsher timing environment). The queue was
**sometimes empty when the operator's turn STARTED, refilled within a few
seconds, and was never empty at expiry** — every pick was queue-fed. Two
readings: the refills almost certainly ran while sync lag still had the
cockpit believing it was off-turn, which means `Button--queue` was PRESENT
during the operator's actual turn — disputing the probe's "off-turn only"
finding — and v1.3's head-fix-only policy (which refused all on-turn adds)
was both built on a doubtful premise and unable to fix the worst reachable
state. v1.4 therefore runs the SAME loop on-turn (attempting adds is
structurally safe: worst case is an `on_clock` refusal that finally measures
the question), polls the search wait instead of sleeping a fixed 1.4 s, and
trims settles — roughly halving refill latency. Also confirmed by the
operator: **ESPN auto-removes a queued player the moment any team drafts
him** (previously assumed). Team codes in queue rows remain UNCONFIRMED
(low-confidence "not seen" — next run's pairing log settles it; absence
degrades namesakes to protected rows, safe but noisy).

---

### 6d. Second live test — 2026-08-16, room 1908085605 (executed v1.3)

An install lag made this a clean second v1.3 measurement (the v1.4 fixes
landed on disk mid-draft; the Tampermonkey copy was older — **protocol rule:
restart the cockpit, reinstall the script, VERIFY the badge version before
the room opens**; the cockpit reads the userscript file at startup). Full
160-pick run, 46 reports (33 not-ok — the history endpoint earned its keep on
its first outing).

**The decisive finding: the grid's search is SCOPED BY THE POSITION FILTER
dropdown, and ESPN drifts that filter with its own need suggestions.** The
`not_in_pool` epidemic was positional, not lexical: DST adds failed for six
straight rounds then LANDED the moment the filter reached D/ST — while QBs
simultaneously began failing; Jared Goff went `not_in_pool → no_control →
landed` in 20 s as the filter moved. Verified live post-draft:
`<select class="dropdown__select">` with `All Pos.=-1, QB=0, RB=2, WR=4,
TE=6, FLEX=23, D/ST=16, K=17`. **v1.5 owns the filter**: set to the target's
position (native setter + change) before every search, restore All Pos.
after.

Also settled by this run:

- **Team codes in queue rows: CONFIRMED** ("53L. Burden IIICHIWRRemove") —
  §6b's open DOM assumption #1 closes. DST queue rows matched by nickname
  (adds verified landed) — assumption #2 effectively closes too.
- **The identity layer ran flawlessly live**: every pairing correct including
  suffixed names ("L. Burden III"), zero protected-row refusals, zero
  wrong-player adds, zero halts.
- **Queue-head-equals-drafted traced directly for picks 68, 73, 88, 93, 108,
  128 and 153**; ESPN fallback (starved queue) at 113, 133, 148. One
  anomaly on file: at 33, reports showed 2 queued rows shortly before a
  non-queued player was drafted — both rows were likely sniped mid-turn (one
  demonstrably was); watch for recurrence.
- The hover-gated Remove flaked once ("T. Henderson" → refused) and
  recovered on the next cycle — the retry design absorbing §4a hazard 2.
- **Run 2 was also fully hands-off — and the operator never visited the Pick
  History tab.** Sync still harvested all 160 picks and the writer's §5c
  tripwire stayed armed (no "tripwire blind" notes in any report):
  `.pick-history` is readable regardless of the active tab, resolving the
  in-repo contradiction the audit flagged. **No tab discipline is needed on
  draft night**, and the sync badge's "open the Pick History tab" guidance is
  stale for this room shape.

### 6e. Third live test — 2026-08-16, room 1639896930 (v1.5, badge-verified)

160 picks in under six minutes (~2.2 s/pick — 40x draft-night pace), run per
the hands-off protocol. Fidelity: 6/16 exact desired[0], 11/16 within the
top-2, at a pace the real draft will never approach. Two discoveries and one
root-caused defect:

**Autopilot is the real commit model.** The room's Autopick toggle starts
OFF; the operator's first expiry (pick 8 burned its full 30 s clock) flips
the seat to autopilot, and every later operator turn committed INSTANTLY at
turn arrival (state-watcher timing: no pause at any later operator pick).
Consequences: (1) after the first expiry there is NO on-turn window — the
between-picks queue maintenance is the entire game, which is exactly what
the writer does; (2) **draft-night runbook item: flip Autopick ON in the
queue panel at ~18:45** for full autonomy from pick 1 (otherwise round 1
burns the full 90 s and flips it anyway). The v1.3 reader-bug fix proved
itself: reports recorded the off→on transition cleanly.

**On-turn adds are REFUTED, definitively.** v1.5's unified loop attempted
them; ESPN returned `on_clock` five consecutive cycles (Button--queue is
genuinely absent on the operator's turn — §3 stands). Run 1's mid-turn
refills were sync-lag off-turn cycles. The unified loop stays (it is the
correct posture under autopilot's instant commits, and the attempts cost
nothing).

**The divergence play EXECUTED**: Cameron Dicker (K) drafted at overall 88 —
ESPN-available **#95**, a depth no fallback logic reaches — plus deep-board
queue commits at 68 (#20), 73 (#33), 113 (#25), 148 (#51). v1.5's position
filter held mid-game depth at 5–8 (run 2 starved to 1–2).

**The defect (fixed in v1.6, node-verified on the real row text): the writer
removed its own correct DST adds.** `landed → STALE → removed → re-added`
oscillating for two minutes: the queue row's concatenated text
("169Texans D/STHOUD/STRemove") failed the word-boundary-anchored is-DST
test ("D/ST" is always followed by a letter there), fell out of the DST
matching branch, and paired as stale — precisely the audit's predicted
failure shape, resurfacing through a regex. `isDstText`/`dstNickname` are
now slash-mandatory with no boundary ("d/st" cannot occur in a person's
name; "Goldstein" stays safe). v1.6 also retries not_in_pool once under
All Pos. — ESPN files Kyle Juszczyk under FB, invisible under the RB filter
the house position implies.

## 7. Refusal + push contract

**Refuse rather than guess** — never queue a player we are not confident maps
to the right ESPN row. Refusal is per-player: skip and take the next
recommendation; only escalate if the queue cannot be kept valid.

**Safety depth:** maintain at least **K_min = 3** valid queued players. Falling
below that is the escalation trigger, not a single failed resolution.

**Push** (operator attention contract — must name an action):

- Trigger: queue depth < K_min, or verification failed twice, or the sync feed
  has gone silent while a pick is pending.
- Content: the action and the deadline. e.g.
  `"Queue empty — your pick in ~60s. Open ESPN and pick manually."`
- Rate-limit hard; the operator has said they will ignore pushes once they are
  unavailable, so **a push must never be load-bearing**.
- Publish-then-record on the dedup ledger (the 3.6 standing lesson: reserving
  before the side effect means a preview silently consumes the event).

---

## 8. Acceptance tests

Nothing ships without these, on **live practice drafts**:

1. **P0 probe passes** — remove and reorder demonstrated (§4).
2. **Full 16-round hands-off practice draft**, operator does not touch the
   mouse. Every pick comes from the queue; final roster is legal and sane.
3. **Deliberate mid-draft kill** — kill the cockpit at ~round 6. The last good
   queue must still carry the remaining picks; no wrong-player commits.
4. **Resolution failure injected** — feed a rec that cannot be resolved; assert
   refuse-not-guess, assert the queue stays valid, assert exactly one push.
5. **Queue-order fidelity** — assert the committed pick equals `desired[0]` at
   expiry, across at least 5 picks. This is the load-bearing claim of §2.
6. Existing suite stays green; the boundary guard still passes.

---

## 9. Build order, and kill criteria

1. ~~**P0 probe** (remove/reorder)~~ — **DONE 2026-08-15, passed** (§4).
2. ~~`GET /api/queue` + ordering policy, server-side, unit-tested~~ — **DONE
   2026-08-15** (§6a; 20-agent adversarial audit: 11 confirmed findings — 3
   major — all fixed same day; suite 1457).
3. ~~Queue writer userscript: read → diff → rebuild → verify~~ —
   **CODE-COMPLETE 2026-08-15** (§6b; 35-agent audit: 28 confirmed findings —
   6 critical — all fixed; identity fixes node-verified against the audit's
   repros; suite 1458). **Unrehearsed against live ESPN** — steps 5's
   rehearsals are the acceptance gate, and §6b names the two DOM assumptions
   they must verify first.
4. Refusal + push path.
5. Rehearsals (§8.2, §8.3).
6. *Stretch, only if 1–5 are clean well before 08-31:* active card-path
   commit, replacing expiry with a deliberate click.

**Kill criteria — revert to the manual quick-pick plan if any of these hold on
2026-08-29:** the full-length hands-off rehearsal has not passed twice; queue
verification is not reliably achieving the desired order; or ESPN ships a
draft-room change that moves the selectors in §3.

**Standing risk:** ESPN can rebuild that bundle at any time. A clean rehearsal
on 08-30 proves nothing about 08-31 — which is precisely why the queue is the
load-bearing safety layer and the script is not.

---

## 10. Draft-night runbook (draft here, refine after rehearsals)

- **18:00** — room opens. Start `ziggurat draft-web` with the correct
  `--slot` / `--pick-order`. **Seat translation is the highest-consequence
  hand-transcription in the system**: ESPN's `pickOrder` is 1-based *team ids*;
  `--pick-order` takes 0-based *seat ids*. Get it wrong and the engine silently
  plays someone else's hand. Verify against ESPN's own displayed draft slot.
- **18:45** — confirm the userscripts are live in the draft tab (badge visible),
  the cockpit shows a full board, and the queue writer has populated a queue.
- **19:00–~19:30** — operator present, rounds 1–3. Watch that the committed
  pick matches `desired[0]` every time.
- **~19:30 onward** — unattended. Pushes are informational only.
- **23:15** — the scheduled league sync captures the completed draft (ESPN
  flushes atomically at completion; no new code needed).

---

## Appendix — using the probe

`ziggurat/draft/espn_probe.user.js`, installed via Tampermonkey (already
installed in Chrome on the desktop as of 2026-08-11). **Nothing auto-runs**;
it `@match`es the real draft room too, so every test is triggered from its own
badge. Buttons: `inspect DRAFT control`, `queue (in gesture)`,
`queue (no gesture, 6s)`, `QUEUE chain (named)`, `arm CHAIN test` (two-click,
commits a pick — practice drafts only), `copy log`.

Run tests **off-turn and hands-off** wherever possible, and when testing a
commit, **let it fail** rather than rescuing it with a manual click — a wasted
practice pick is worth far more than a contaminated result.
