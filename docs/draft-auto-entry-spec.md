# Draft-day auto-entry — build spec

**Status:** approved to build, 2026-08-13. Not started.
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
| queue panel | `.pick-queue` | `empty` class + `Pick Queue (N)` text |
| player rows | `[class*="fixedDataTableRowLayout_main"]` | virtualized; also used by history/queue panels |
| pick history | `.pick-history` | existing sync script harvests this |

---

## 4. What is NOT proven — build blockers

**P0 — queue EDIT is completely untested, and the whole design rests on it.**
We proved *append*. We did not prove *remove* or *reorder*. A queue that can
only be appended to is useless by round 3, because the board re-ranks as other
teams draft. Flagged by the operator, and correctly.

Probe this **first**, before writing any feature code:

1. Does a synthetic click on the queue panel's **`Remove`** control work?
2. Does queue add always **append to the end**, or does ESPN insert by rank?
3. Is reordering possible at all — is it drag-only (HTML5 DnD or mouse-move
   based), or is there an ordering control?

**If Remove works, ordering is solved regardless**: reconciliation becomes
clear-and-refill in the desired order (~N clicks, cheap at 90 s/pick). Drag
reordering would be a nice-to-have, not a requirement. **If Remove does NOT
work, this entire design is dead** and we fall back to active Draft-clicking
(§9 kill criteria).

**P1 — virtualized-grid search.** `findPlayerRow` only sees *rendered* rows.
A recommended player outside the viewport has no DOM node. Need to drive
ESPN's own player-search filter to bring a target into the grid, then resolve.

**P2 — autopick semantics.** Confirm that clock expiry takes the **top queued
available** player, and characterise when ESPN flips the seat into persistent
"You're on Autopick" mode (observed to engage after repeated expiry). Confirm
a populated queue still governs once that mode is on.

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
- Cockpit endpoint `GET /api/queue` — returns the desired queue (top K
  available recs) so ordering policy lives server-side and is unit-testable.
- Cockpit endpoint `POST /api/queue/status` — the script reports the queue it
  actually achieved; the cockpit logs it and decides whether to push.
- Push on refusal via the **existing** `ziggurat/push/` egress choke point
  (Rule 5 outbound scrub applies; `draft/` → `push/` is a legal direction).

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

1. **P0 probe** (remove/reorder). ~1 session. **If Remove fails, stop** and
   re-plan around active Draft-clicking with verify-after-commit.
2. `GET /api/queue` + ordering policy, server-side, unit-tested.
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
