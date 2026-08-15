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

### Still open (neither blocks the build)

**P2 — autopick semantics.** Confirm that clock expiry takes the **top queued
available** player. The queue panel carries an **`Autopick` toggle**; whether
expiry draws from the queue may depend on it. **This is the load-bearing
assumption of §2 and it is still an assumption** — witness it before the
hands-off rehearsal (§8.2), and treat §8.5 as the test that settles it.

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

- `ziggurat/draft/espn_queue.user.js` — the queue writer. Extends (or ships
  alongside) `espn_sync.user.js`. Same `{{PORT}}`/`{{TOKEN}}` templating.
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
`{league, overall, achieved: [names…], ok, reason?}` — **`ok` must be a JSON
boolean** (a stringly `"false"` is rejected 400; a coerced True would silently
reset the failure streak step 4 pushes on). The endpoint **validates the
first-room league binding but never claims it** — the binding belongs to the
pick feed (`/api/sync`) alone; a picks-free telemetry POST that claimed it
could bind a fresh cockpit to `""` before the harvester's first batch and brick
sync for the whole unattended remainder (audit, demonstrated live). Bounded
storage; response carries `bad_streak` (consecutive `ok: false` reports).
Surfaced in `/api/state` under `"queue"`.

---

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
3. Queue writer userscript: read → diff → rebuild → verify.
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
