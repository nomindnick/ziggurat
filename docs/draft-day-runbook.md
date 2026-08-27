# Runbook: draft night, and every practice run before it

**One procedure, deliberately.** A practice run that differs from draft night
proves less than it appears to — the two silent failures below are both "I did
it slightly differently this time". Practice exactly as you will draft; the
handful of genuine differences are marked **[practice only]**.

Verified end to end on `framework-desktop`, 2026-08-27. Draft: **Monday
2026-08-31, 19:00 PT**, room opens 18:00, 90 s per pick, snake, 16 rounds.
**We draft 9th of 10.**

---

## 0. The card

For when you have done this before and want the sequence, not the reasons.

```bash
cd ~/Projects/ziggurat
.venv/bin/ziggurat league status          # cookies alive, today's snapshot landed
.venv/bin/ziggurat ingest status          # espn_ranks / projections say "fresh"
.venv/bin/ziggurat draft-web --season 2026 --slot 9
```

Then: open `http://127.0.0.1:8811/`, open the ESPN draft room in Chrome,
confirm **two green badges** (bottom-right of the ESPN tab), confirm ESPN's **Autopick toggle
is ON**. Rounds 1–3 in person, then it runs itself.

**Never pass `--pick-order`, `--port`, or `--journal`.** Section 3 explains why
each one silently breaks something.

---

## 1. What must already be true

All five verified on this box 2026-08-27. Re-check before Monday; each takes
seconds.

| Requirement | Verify | Status 08-27 |
|---|---|---|
| The desktop is the draft machine | you are sitting at it | settled 2026-08-10 |
| Tampermonkey in Chrome | `ls ~/.config/google-chrome/Default/Extensions/dhdgffkkebhmkfjojejmpbldmpobfkfo` | installed |
| Queue writer userscript **v1.7** | Tampermonkey icon → Dashboard; check the version column | **REINSTALL NEEDED** — the installed copy is v1.6 |
| Sync userscript **v1.1** | same dashboard | installed |
| ESPN cookies valid | `.venv/bin/ziggurat league status` says `ok` | ok |

If either version differs from the table, reinstall it (below) — the versions
here are checked against the shipped files by
`tests/test_draft_runbook.py::test_the_userscript_versions_match_the_shipped_files`,
so this table is wrong only if the install is stale, not if the doc is.

**The installed userscripts have `127.0.0.1:8811` and a specific token compiled
into them.** The token lives in `data/draft/sync-token.txt` (persisted since
2026-08-16, unchanged). This is why §3 forbids `--port` and `--journal`: change
either and every post from the scripts gets a 403 — the badges go red and you
are on manual entry, having changed nothing that looked dangerous.

If you ever do need to reinstall them, start the cockpit and open
`http://127.0.0.1:8811/sync.user.js` and `http://127.0.0.1:8811/queue.user.js`
— Tampermonkey offers to install each, with the live port and token baked in at
serve time.

**Leave the systemd timers running.** `ziggurat/draft/` never writes to the
database — it reads the board and appends to a local journal — so the cadence
and the draft cannot collide. The 23:15 league sync is how the completed draft
gets captured (§7).

---

## 2. Preflight — about 20 minutes out

```bash
.venv/bin/ziggurat league status
.venv/bin/ziggurat ingest status
```

What you need to see:

- `league status` → `last run ... [ok]`, and a `snapshot` date of today or
  yesterday. This is the cookie check that matters: if the cookies have
  expired, this is where you find out, and refreshing them takes minutes you
  will not have at 18:55.
- `ingest status` → **`espn_ranks`, `projections` and `adp_rankings` all
  `fresh`**. These three are the board. They refresh daily on this box, so the
  expected state is fresh with age `1d` or less. If `espn_ranks` is stale, the
  engine is drafting off an old room-consensus board and the divergence play
  degrades quietly — force it:

```bash
.venv/bin/ziggurat ingest run --source espn_ranks --source projections --source adp_rankings
```

**[practice only]** Once, before Monday, also confirm the suite is green:
`.venv/bin/pytest` — 1473 passed / 4 skipped as of 2026-08-27.

---

## 3. Launch, in this order

Order matters: the cockpit must be listening before the ESPN tab loads, because
the userscripts probe it at `document-idle`.

### 3.1 Start the cockpit

```bash
cd ~/Projects/ziggurat
.venv/bin/ziggurat draft-web --season 2026 --slot 9
```

It prints three lines. The first is the page; keep the terminal open — closing
it ends the session.

```
Draft cockpit: http://127.0.0.1:8811/  (Ctrl-C to quit; journal: data/draft/session-YYYYMMDD-HHMMSS.jsonl)
ESPN sync userscript (install once in Tampermonkey): http://127.0.0.1:8811/sync.user.js
ESPN queue writer (install once in Tampermonkey): http://127.0.0.1:8811/queue.user.js
```

**The three flags not to pass, and what each one silently breaks:**

- **`--pick-order`** — do not pass it. It was long believed mandatory, and
  getting it wrong seats the engine in someone else's chair with no error
  raised. It is genuinely unnecessary: seat ids are arbitrary internal labels,
  synced picks arrive positionally, and identity order was proven (2026-08-27)
  to produce the identical 16 overall picks. `--slot 9` alone is correct and
  complete.
- **`--port`** — the installed userscripts are compiled for 8811.
- **`--journal`** — the sync token is read from the journal's own directory. A
  journal elsewhere means a different token and a 403 on every sync post.

Also **do not pass `--resume`** at the start of a session. Bare `--resume`
recovers the *newest* journal in `data/draft/`, which during practice week is a
practice draft. Resume is for recovering a crash mid-draft (§6), and then you
name the file explicitly.

**[practice only]** Add `--no-push` unless you are deliberately exercising the
push path (§8.2). Without it, practice refusals ring your phone.

### 3.2 Open the cockpit page

`http://127.0.0.1:8811/` in Chrome. You should see the board populated, an
empty pick log, and "Best available (your scoring)".

### 3.3 Open the ESPN draft room

- **Draft night:** your league's draft room, from the ESPN fantasy app or site.
- **[practice only]:** the mock lobby → **"League Specific Practice Draft"** —
  the real draft room with the real league's settings against Auto teams,
  repeatable on demand. This is the venue all four prior rehearsals used. It
  drafts at CPU speed (~2 s/pick), which is roughly 40× draft-night pace and
  therefore a stress test, not a simulation of the real tempo.

**Only one draft room tab at a time.** The cockpit binds to the first room it
hears from and refuses every other one, so a leftover practice tab cannot
contaminate a live session — the loser's badge says so out loud. But the
binding is claimed by whichever tab ticks first, so a stale tab can claim it.
This matters most **after a restart** (§6): if you resume a crashed session
with a practice room still open somewhere, that room can win the binding and
the real one gets refused. Close every other draft tab before Monday, and use
a fresh cockpit for each room.

### 3.4 Confirm the two badges

In the ESPN tab, two small monospace badges stack in the **bottom-right
corner**:

- `zig queue: ...` (upper) — the queue writer. Green `LIVE` means it is
  reconciling. Amber is a recoverable warning. **Red `HALTED`** means it has
  stopped and you are on manual entry until it recovers (§6). It also appends
  `· autopick ON`, or shouts `· AUTOPICK OFF!`, every cycle. This badge is
  clickable: click to pause, click again to resume.
- `zig sync: ...` (lower) — the pick mirror. Green good, red bad. Not
  clickable.

Green on both is the state you want before the clock starts. If a badge is
missing entirely, the userscript did not load: reload the ESPN tab, and check
Tampermonkey is enabled for the page.

Ignore the sync badge if it suggests opening the Pick History tab. That guidance
is stale: `.pick-history` reads fine regardless of which tab is active, proven
by a full 160-pick run in which the tab was never visited. **No tab discipline
is needed.**

### 3.5b The draft tab must stay VISIBLE and in front

**MEASURED 2026-08-27, and it silently cost a whole practice draft.** Chrome
throttles timers in a hidden tab. Both userscripts are timer-driven, so a
backgrounded draft tab does not fail loudly — it just slows to a crawl and then
stops. In that run `document.hidden` was `true` throughout: the queue writer
stalled after roughly pick 33 and never recovered, and from that point ESPN
autodrafted off ITS board instead of ours.

The result is the tell to memorise, because it is what a throttled writer looks
like from the outside: **D/ST went at 149 and the kicker at 152** — exactly the
room's own behaviour — instead of the engine's R9/R10 divergence play. The
board was never wrong; it just was not being written to ESPN any more.

So: the draft room must be the **active tab in a window that is not minimised
and not fully behind another window**. Do not park it on a second desktop and
do not tab away to another site in that window. Other windows in front of it
are what `document.hidden` reacts to.

The queue writer does say so — its report carries `(note: tab hidden — timers
throttled)` — but that text is inside the report, not on the badge, so nobody
reads it in time. If picks stop arriving in the cockpit, check this first.

### 3.5 Confirm ESPN's Autopick toggle is ON

**This is the single most load-bearing setting in the whole design, and it is
ESPN's, not ours.** Ziggurat never clicks Draft. It keeps ESPN's Pick Queue
equal to the engine's ranked list, and ESPN's own autopick commits from that
queue when your clock expires. Autopick off and the mechanism has nothing to
fire.

The queue writer reads the toggle every cycle and shouts `AUTOPICK OFF!` in the
badge if it is off. **Confirm it visually anyway before the first pick** — that
instruction earned its keep on 2026-08-27, when the writer reported
`autopick: off` while the DOM checkbox read `checked: true`. Its selector was
scoped to the queue panel and the control sits outside it, so it fell back to
reading whatever checkbox was nearest and reported that. Fixed in **v1.7**: it
now reads the named control and says `unknown` rather than inventing a state.

Three readings, three meanings: `ON` is what you want; `AUTOPICK OFF!` means go
look at the toggle; **`unknown` means the writer could not find the control at
all** — treat that as "check it yourself", not as a soft off.

---

## 4. The pre-clock checklist

Everything in one place. On draft night, be here by **18:45**.

- [ ] `league status` ok, `ingest status` shows the board fresh
- [ ] cockpit running, terminal open, page loads at `http://127.0.0.1:8811/`
- [ ] exactly one ESPN draft room tab open
- [ ] both badges present, queue badge green and `LIVE`
- [ ] the draft tab is the **active, visible, unobscured** tab (§3.5b)
- [ ] ESPN **Autopick toggle ON** — and see §3.5 on trusting that reading
- [ ] the queue writer has populated a non-empty ESPN Pick Queue
- [ ] cockpit shows a full board and your seat as **9**

---

## 5. During the draft

**Division of labour.** The tools recommend; ESPN commits. You do not enter
picks in the cockpit while sync is live — the cockpit says so itself ("Your
pick — make it in ESPN; the cockpit records it automatically") and stands the
quick-pick strip down, because two writers produce conflicts. When it is your
turn it also hands you **ESPN's own search text** for the recommended player
("Draft him *in ESPN* — search ..."), which is how the D/ST naming mismatch
gets solved at the table: our board says `HOU D/ST`, ESPN's search wants
`Texans`.

**Rounds 1–3, in person.** Watch that the pick ESPN commits matches the
cockpit's top recommendation. That match rate is the load-bearing claim of the
whole design; the graduation run hit 11/16 exact and 12/16 top-2 at 40× pace.

**From round 4 on it runs unattended.** Pushes are informational; they are
best-effort by design and nothing depends on you answering one. (The spec's
autonomous window is "roughly round 5 onward" — round 4 is the overlap, not a
gap in coverage.)

**Your picks are overall 9, 12, 29, 32, 49, 52, 69, 72, 89, 92, 109, 112, 129,
132, 149, 152.** Turning near the end of the snake means they arrive in pairs
three apart — 9 then 12, 29 then 32 — with sixteen picks of nothing in
between. That asymmetry drives the engine's reasoning and you will see it in
the reason text: at the FIRST pick of a pair only two rivals intervene, so
survival is high and it says things like "likely (96%) to still be there at
your next pick — no rush". At the SECOND pick you are about to wait sixteen,
so urgency spikes and the scarce position gets taken there. If a recommendation
ever looks like it is passing on the obvious best player at pick 9, this is
usually why — it expects to still have him at 12.

**Two things that will look wrong and are not:**

- **D/ST around pick 89 and a kicker around 92 or 109.** The room takes theirs
  around 141–152. This is the divergence play — our scoring values distance
  kickers and both D/ST brackets in ways ESPN's default board does not — and it
  is the single clearest edge the system has. Let it happen.
- **A third quarterback late.** In a 1-QB league that is one misallocated bench
  slot, a known and accepted residual (§9). It is a droppable backup, not a
  problem to solve at the table.

---

## 6. When something goes wrong

The ladder, worst-realistic-case first. Each rung degrades to the one below it
rather than to nothing — that is the design, and it is why the queue is the
safety layer and the script is not.

**The queue writer halts (red badge).** ESPN's queue still holds whatever was
last written, so autopick still drafts from our board. Recover by clicking the
badge to resume, or reload the ESPN tab. If it will not recover, you are on
manual entry: draft in ESPN yourself, off the cockpit's recommendation panel.

**Sync stops mirroring picks.** The cockpit's board state goes stale, so its
recommendations degrade. Reload the ESPN tab — re-activation re-renders all
rows and the harvester dedupes by pick number, so it back-fills everything it
missed.

**A pick refuses to commit — and this one is urgent.** By design the gate
refuses rather than guessing on an ambiguous name, and that is correct. But a
blocked pick **dams the entire feed behind it**: every later pick queues up
`pending` and the cockpit's board state freezes at the block, so its
recommendations go stale from that moment. Clear it promptly. Use the one-click
"Find him" assist, or enter the pick manually via search — the moment it lands,
the backlog drains in one go (measured 2026-08-27: 63 → 125 instantly).

The refusal message names the field that disagreed — position, team, or the
row's own two names. Read it; it tells you whether ESPN and the board disagree
about the player or the parse went wrong. If the cockpit and ESPN disagree
about a pick that DID commit, use "Use ESPN's pick" — ESPN is always ground
truth.

**The cockpit dies.** ESPN's queue survives it — autopick keeps drafting from
the last-written queue, which is exactly the fallback this design exists to
have. Restart and resume, naming the journal explicitly:

```bash
.venv/bin/ziggurat draft-web --resume --journal "$(ls -t data/draft/session-*.jsonl | head -1)"
```

Resume replays the journal and rebuilds state bit-identically. Naming the file
explicitly is the point — mid-draft the newest journal *is* tonight's, so bare
`--resume` would also work, but the habit is what keeps you from picking up a
practice session by reflex at the start of a night.

**Everything is dead.** Draft manually in ESPN. You have 90 seconds a pick,
which is a great deal more than it sounds like.

---

## 7. After

- **Do nothing to capture the roster.** The 23:15 league sync imports it — ESPN
  flushes its league views atomically at draft completion. That first
  post-draft snapshot is the most valuable league-state capture of the season.
- **Keep the journal.** `data/draft/session-*.jsonl` is the record of what the
  tool recommended versus what was actually drafted. Phase 4 grades decisions,
  not outcomes, and this is the input.
- The weekly cadence starts the **Tuesday after the draft** — see CLAUDE.md,
  "Weekly operating cadence".

---

## 8. Practice-run extras

### 8.1 The mid-draft kill test (spec §8.3, unrun as of 2026-08-27)

Around round 6, Ctrl-C the cockpit and leave it dead for several picks. What
must hold: **the last good queue still carries the remaining picks**, and no
wrong player is committed. Then resume (§6) and confirm the replayed state
matches ESPN.

### 8.2 The refusal test (spec §8.4, live half unrun as of 2026-08-27)

Feed a recommendation that cannot resolve to an ESPN row. Assert: it refuses
rather than guessing, the queue stays valid (the writer skips and takes the next
recommendation), and **exactly one** push fires — not zero, not a stream. Run
this one *without* `--no-push`.

### 8.3 Clean up afterwards

```bash
mkdir -p data/draft/practice && mv data/draft/session-*.jsonl data/draft/practice/
```

This keeps `data/draft/sync-token.txt` where it belongs (so the installed
userscripts keep working) while removing practice journals from bare-`--resume`
discovery, which only ever looks one directory deep.

---

## 9. Known, accepted, and not worth fixing at the table

- **A third QB in the last rounds.** One misallocated bench slot in a 1-QB
  league; the tail runs into the QB cap. The starting-lineup metric that grades
  the engine scores bench picks zero, so it cannot see this either way.
- **`autodraft_fraction = 0.2`** is a 2025 fit. All 10 seats are owned for
  2026, so the sim's ~2 random autodraft seats are now an assumption rather
  than an observation — the softest input to every survival estimate.
- **Every margin is house-projected and self-graded.** "+104 vs follow-VOR,
  +153 vs follow-ESPN at slot 9" means our own projections think so. Phase 4
  grades what actually happened.
- **ESPN can rebuild the draft-room bundle any day**, which would move the
  selectors §3 depends on. A clean rehearsal on Saturday proves nothing about
  Monday. This is not paranoia, it is the reason the fallback ladder in §6 has
  four rungs.

---

## 10. Quick reference

| | |
|---|---|
| Draft | Mon 2026-08-31, 19:00 PT (room opens 18:00) |
| Format | Snake, 16 rounds, 90 s/pick, 10 teams |
| Our slot | **9** |
| Our picks | 9, 12, 29, 32, 49, 52, 69, 72, 89, 92, 109, 112, 129, 132, 149, 152 |
| Command | `.venv/bin/ziggurat draft-web --season 2026 --slot 9` |
| Cockpit | `http://127.0.0.1:8811/` |
| Userscripts | `/sync.user.js` (v1.1), `/queue.user.js` (v1.6) |
| Journal | `data/draft/session-<timestamp>.jsonl` |
| Sync token | `data/draft/sync-token.txt` — do not delete |
| Practice venue | ESPN mock lobby → "League Specific Practice Draft" |
