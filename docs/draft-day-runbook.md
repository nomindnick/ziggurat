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

**The one flag that IS a tool: `--legacy-engine`.** It restores the engine
exactly as it drafted the four rehearsals, and it is rung 0 of the fallback
ladder in §6 — the first thing to try if the cockpit looks wrong. It costs one
re-launch and nothing else.

---

## 1. What must already be true

All five verified on this box 2026-08-27. Re-check before Monday; each takes
seconds.

| Requirement | Verify | Status 08-27 |
|---|---|---|
| The desktop is the draft machine | you are sitting at it | settled 2026-08-10 |
| Tampermonkey in Chrome | `ls ~/.config/google-chrome/Default/Extensions/dhdgffkkebhmkfjojejmpbldmpobfkfo` | installed |
| Queue writer userscript **v1.9** | the cockpit page says so (below) | verify Monday |
| Sync userscript **v1.2** | the cockpit page says so (below) | verify Monday |
| ESPN cookies valid | `.venv/bin/ziggurat league status` says `ok` | ok |

**You no longer check these by eye.** Since queue writer v1.9 / sync v1.2 each
script names itself in every report and the cockpit diffs that against the
shipped file, so a stale install announces itself as an amber **STALE
USERSCRIPT** banner across the top of the cockpit page, naming both versions
and the fix. Green means the browser is running what this repo ships.

This matters because Tampermonkey runs a **snapshot**: editing the script here
changes what `/queue.user.js` serves and not one byte of what Chrome executes.
That drift is invisible from both sides, and it had already happened — on
2026-08-27 the installed writer was **v1.6 against a shipped v1.8**, i.e.
missing both the autopick-selector fix and the hidden-tab alarm, with nothing
but this table and an operator's memory standing between that and a live draft.

Two honest limits of the banner: the **writer** reports from the moment the
ESPN tab loads, so its version is confirmed before the clock starts; the
**sync** script only posts once it has picks, so it reads "not reported yet"
until the draft's first pick and is confirmed a few picks in. Neither script
having reported is shown as unknown, never as stale — a banner that cries wolf
during the pre-clock window is one you will have stopped reading by 19:00.

The versions in the table are checked against the shipped files by
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

The reverse direction needs checking too, and was (2026-08-29): a **resume
(§6) re-reads the board from the database** at the journal header's `as_of`,
so a timer that rewrote `espn_ranks` mid-draft would hand the resumed session a
different board than the one it started on. It cannot happen on Monday —
`espn_ranks` is in the `daily` group (07:20 +≤15 min), the `gameday` group is
weather only, and **no ingest timer fires between 18:00 and 23:00 at all**. The
board is therefore frozen across the whole draft window, restart included. The
one timer that does fire in that window is the 20-minute **alerts** tick, which
only reads and pushes news.

Which is worth knowing for a second reason: **your phone will get ordinary
injury-news alerts during the draft**, on the same ntfy topic as the cockpit's
own escalations. A push during the draft is not necessarily about the draft —
read the text before reacting to it.

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
`.venv/bin/pytest` — **2443 passed / 4 skipped as of 2026-08-31** (it was 1473 on
2026-08-27; two phases of measurement work, the item-3.11 integration and its
audit-fix round landed in between). It takes about 5½ minutes.

**Two failures here are EXPECTED on draft day and are not a reason to stop.**
Both are the same cause: the golden master is frozen at `as_of 2026-08-30`, and
the 07:20 ingest re-pulls `espn_ranks` / `projections` every morning. When the
board moves, `test_the_live_board_still_matches_the_frozen_board` and
`test_the_live_weekly_points_map_still_matches_the_frozen_one` fail *by design* —
they are drift detectors for the fixture, not correctness checks on the engine.
Anything ELSE failing is real. Do not re-bless a fixture on draft day: the frozen
board is what every measurement was made on, and the draft itself reads the live
board either way.

---

## 3. Launch, in this order

Order matters: the cockpit must be listening before the ESPN tab loads, because
the userscripts probe it at `document-idle`.

### 3.1 Start the cockpit

```bash
cd ~/Projects/ziggurat
.venv/bin/ziggurat draft-web --season 2026 --slot 9
```

It prints one line about the kicker board and one naming the engine, then three
lines about the cockpit. The cockpit URL is the page; keep the terminal open —
closing it ends the session.

```
KICKER BOARD: uncorrected (every kicker understated ~25-43 pts, ...). Nothing to do about it tonight; ...
ENGINE: default (composed) — the week-by-week re-rank is on at every pick, ...
Draft cockpit: http://127.0.0.1:8811/  (Ctrl-C to quit; journal: data/draft/session-YYYYMMDD-HHMMSS.jsonl)
ESPN sync userscript (install once in Tampermonkey): http://127.0.0.1:8811/sync.user.js
ESPN queue writer (install once in Tampermonkey): http://127.0.0.1:8811/queue.user.js
```

**It takes about 4 seconds and then it is up** (measured 2026-08-31 on this box:
3.8 s to the first line, 4.0 s to serving — and the same on `--legacy-engine`,
which is the point: the default engine no longer costs anything at launch). If
the terminal is still silent after ten, something is wrong; do not Ctrl-C before
then.

**Both lines also appear ON THE COCKPIT PAGE**, under the header, so "which
engine is on the clock?" is answerable at 18:45 from the browser rather than
from terminal scrollback. `ENGINE: default (composed)` is what you want tonight;
`ENGINE: --legacy-engine` means you passed the escape hatch.

**The kicker line is the item-3.10 disclosure** — the K board is known to be
misordered because the projections feed drops every 50+ made field goal. It is
NOT actionable tonight (the correction's source table has never been pulled on
this box, and pulling it hours before the draft would move the board out from
under the frozen golden master) and it affects WHICH kicker goes at round 10,
not whether to take one. You no longer have to remember it: **the same caveat is
attached to the kicker recommendation itself**, so it is on the panel at ~21:00
when the pick is actually made. See §9.

**One more line can appear, and it is the one to read carefully:** a sentence
beginning `ENGINE: week-by-week re-rank UNAVAILABLE`. That means an improvement
could not be built and the session fell back to the engine that drafted four
rehearsals — deliberately, rather than refusing to start. Nothing to do; draft
normally, and note it in the journal afterwards.

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
are what `document.hidden` reacts to — and so is the **GNOME lock screen**:
the 2026-08-27 run's `document.hidden: true` was measured to be the LOCKED
DESKTOP (`loginctl … LockedHint=yes`) during a remotely-driven run, not a
tabbing mistake. A remote/unattended run therefore launches Chrome with
throttling disabled (see §8.0); at the box, an unlocked screen with the tab
in front needs no flags.

Since v1.8 this failure is LOUD instead of silent: the writer reports
`document.hidden` as a structured flag every cycle, and the cockpit turns a
sustained `true` into a pulsing red **ESPN DRAFT TAB IS HIDDEN** banner across
the top of the page plus one phone push (budget: two per draft). A second
variant — **QUEUE WRITER SILENT** — fires on the cockpit page when no report
has arrived for 90 s, which is what a closed tab or a dead Chrome looks like
(a merely hidden tab still reports, throttled). The banner is the alarm, not
the fix: the rule above still stands, and if picks stop arriving in the
cockpit, check the tab first.

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

**How the toggle actually arms (operator knowledge + measured live,
2026-08-27 run 2):** ESPN starts every seat with Autopick OFF; it flips ON
the first time that seat's clock expires, and then stays ON until manually
disabled. The two regimes commit differently, and the difference is the whole
design: **armed autopick commits the QUEUE head at turn start; an unarmed
expiry commits from ESPN'S OWN BOARD and ignores the queue entirely** (run 2,
pick 9: queue held the engine's list, expiry took ESPN's #8 Amon-Ra St. Brown
— a board pick, not a queue pick; every armed pick from 12 on tracked the
queue, incl. an 18-spot Loveland reach and D/ST/K ~40 spots early). Run 3
(a live public mock, 2026-08-27) removed the last asterisk: with a
**verified-clean queue** and Autopick confirmed OFF in the DOM, expiry
committed a player who was neither the queue head (available, untaken for
another 100 picks) **nor ESPN's visible-rank best** — the pre-arm pick comes
from ESPN's internal autopick logic and is not predictable from anything on
the screen. So the §4 checklist item is not a formality: **flip Autopick ON
manually in the lobby, before pick 1** — otherwise round 1, the most
valuable pick of the draft, is decided by a black box.

**One session per room.** ESPN kicks the draft-room session when the same
account opens the room from another device (operator-observed 2026-08-27).
On draft night the desktop's tab is the one wired to the cockpit: nobody
opens the draft room from a phone or laptop while it runs.

---

## 4. The pre-clock checklist

Everything in one place. On draft night, be here by **18:45**.

- [ ] `league status` ok, `ingest status` shows the board fresh
- [ ] cockpit running, terminal open, page loads at `http://127.0.0.1:8811/`
- [ ] exactly one ESPN draft room tab open
- [ ] both badges present, queue badge green and `LIVE`
- [ ] **no amber STALE USERSCRIPT banner** on the cockpit page (§1)
- [ ] the draft tab is the **active, visible, unobscured** tab (§3.5b)
- [ ] ESPN **Autopick toggle ON** — and see §3.5 on trusting that reading
- [ ] the queue writer has populated a non-empty ESPN Pick Queue
- [ ] cockpit shows a full board and your seat as **9**
- [ ] you know that `--legacy-engine` is the one-flag undo (§6 rung 0), and that
      it is a **re-launch decision, not a mid-draft one**

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
  is the single clearest edge the system has. Let it happen. It is **unchanged**
  by the 2026-08-31 engine change: both engines take the D/ST at 89 and the
  kicker at 92 on the frozen board, and a test asserts exactly that by name
  (`test_the_kdst_divergence_play_survives_the_rerank`), because a re-rank
  quietly eating this edge would otherwise be buried among 160 other numbers.
- **A third quarterback late.** In a 1-QB league that is one misallocated bench
  slot, a known and accepted residual (§9). It is a droppable backup, not a
  problem to solve at the table.

**At the FIRST pick of each pair (9, 29, 49, 69, 89, 109, 129, 149) the panel
names a second player.** It reads "The pairing this score assumes: him now, then
X at #12 — X is what the room most often leaves (…%)". Read it as what it says:
the doubled score is the value of holding a PAIR, and X is the pairing that
arithmetic assumed. **It is not a promise about your next pick.** Three picks
later the tool re-decides from scratch against the roster you then have, and it
lands on somebody other than X about a third of the time *even with X still
available* — measured over 10 simulated drafts. The panel says this itself, in
the bullet right underneath. That is normal, and it is not the cockpit
contradicting itself.

**And one thing that is genuinely new this year.** From 2026-08-31 the
recommendation reasons can include a sentence about *weeks* — something like "he
plays in the weeks your other backs are off". That is the week-by-week term
explaining itself, and it always arrives with its own limitation attached ("this
week-by-week score has NO injury model: a bench player is worth nothing in it
unless he covers a bye"). Read both halves. It appears only on a pick whose place
in the order that term actually changed.

---

## 6. When something goes wrong

The ladder. **Rung 0 is a flag; every rung below it is a failure mode.** Each of
those degrades to the one below rather than to nothing — that is the design, and
it is why the queue is the safety layer and the script is not.

### Rung 0 — the recommendations look wrong: `--legacy-engine`

```bash
.venv/bin/ziggurat draft-web --season 2026 --slot 9 --legacy-engine
```

**Try this first, before diagnosing anything.** As of 2026-08-31 the default
engine adds two re-ranks to the shipped one, over separate picks (item 3.11):
at the FIRST pick of each of your pairs (overall 9, 29, 49, 69, 89, 109, 129,
149) it asks which *pair* of players you end up holding rather than who is best
right now; everywhere else it asks what each candidate does to a *seated lineup
in every week of the season* — the term that can see a bye collision. Both are
measured better and they are what should run. But they are also the newest thing
in the system on the newest day, and `--legacy-engine` is the engine that drafted
four rehearsals, proven bit-for-bit against its own frozen golden master
(`tests/test_draft_golden.py`) rather than merely believed to be equivalent.

What you would notice: on the frozen board the default takes a different player
at **7 of the 16 picks** (overall 32, 49, 52, 129, 132, 149, 152). On the
frozen board the ROSTER SHAPE is identical either way — both engines finish
3 QB / 3 RB / 5 WR / 3 TE / 1 D/ST / 1 K — so this is not "more running backs";
it is different *names* in those seven slots, chosen so that fewer weeks of the
season have an unfillable starting slot (measured 0.47 → 0.27 holes a season;
re-measured 0.45 → 0.29 over 300 fresh simulated rooms on 2026-08-31, where the
shape came out identical in two thirds of rooms and differed by about one RB/WR
in the rest — so shape is not a reliable tell for which engine is running; the
cockpit page names it, §3.1). It does **not** move the D/ST or the kicker
(§5 above). One number will look wrong and is not: at the
eight pair picks the displayed **pick score is a TWO-PICK TOTAL**, so it reads
about double what the same player would score on the legacy engine. The reasons
say so in the panel, right under the number.

**You should not need this flag for a mere wobble.** If the week-by-week or pair
re-rank ever throws mid-draft, the cockpit does not go blank: it serves that one
recommendation from the shipped engine instead and says so in an amber banner
naming the cause. Take the pick and carry on. Rung 0 is for recommendations that
look *absurd*, not for a banner.

If a recommendation looks absurd rather than merely surprising, take the two
seconds and re-launch on the legacy engine; you give up about 0.06 expected wins
a season and nothing else.

**If you are already mid-draft, you cannot switch.** The journal records which
engine made the picks and a resume on the other one REFUSES, by design: the picks
already made would stand while every remaining pick was decided differently, with
nothing to show for it. Finish on the engine you started on. The refusal message
names the flag to add or drop, so you cannot get this wrong by accident — but
that only helps at a restart, not at pick 40.

### The failure ladder proper

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

### 8.0 Remote / unattended runs: disable Chrome's timer throttling

A practice run driven remotely (operator away, desktop screen locked) runs
Chrome behind the lock shield, so `document.hidden` is true for the whole
draft and Chrome's *intensive throttling* cuts the userscripts' timers to
once per minute after 5 minutes — the exact failure that cost the 2026-08-27
run its second half. For any run where a human is not sitting at the box with
the tab in front, launch Chrome as:

```bash
google-chrome --disable-background-timer-throttling   --disable-backgrounding-occluded-windows --disable-renderer-backgrounding
```

With those flags the writer keeps its ~5 s cadence even hidden (verified
2026-08-27: steady full-rate reports well past the 5-minute cliff behind a
locked screen). Two consequences to expect during such a run: the cockpit's
red **TAB IS HIDDEN** banner shows throughout (it is truthful — the tab *is*
hidden; the flags merely make that harmless), and the `hidden` push lane will
page accordingly — so remote practice runs use `--no-push` and a human watches
`/api/state` instead. On draft night, at the box, the flags are unnecessary
but harmless; the §3.5b rule (tab visible and in front) remains the primary
discipline either way.

### 8.0b A practice draft is a NEW temporary league — get the URL from the launcher

"League Specific Practice Draft" creates a throwaway league with its **own
leagueId**; the real league's draft-room URL never boots a practice room (it
loads only the auth responder and sits blank — measured 2026-08-27, run 2's
first ten minutes). So a pre-parked tab on the real URL is useless for
practice: launch the practice draft first, copy the room URL **from the
launching device** (it looks like the real one but with the temporary
leagueId), and only then point the desktop tab at it. The cockpit needs no
restart — it binds to whatever league the sync feed claims first. On draft
night this problem does not exist: the real room IS the known URL.

Public mock-lobby drafts (run 3) are friendlier than the private practice
flow: joining puts you in a **waiting room**
(`/football/waitingroom?leagueId=<temp>`) that shows the room's settings,
the full draft order (your slot included) well before start, and an **Enter
The Draft** button when the room opens — so the cockpit can be started with
the right `--slot` minutes early and the same tab clicks through when the
button appears. Beginner 10-team H2H-Points PPR snake mocks matched the
league's roster shape (16 rounds, standard lineup) but ran a 30 s pick
clock.

### 8.1 The mid-draft kill test (spec §8.3) — RUN 2026-08-29, PASSED

`SIGKILL` at overall 45; ESPN drafted picks 49 and 52 from the last-written
queue with no cockpit alive. Resume in 4 s, 0 conflicts.

**That 4 s was re-measured on 2026-08-31 and still holds on the default engine**
— 3.8 s to the first line, 4.0 s to serving, on a real 100-pick journal. It
briefly did not: the composed launch read the projections table three separate
times (once for the kicker attempt, once for the board, once for the objective)
and a resume took 11 s idle and 22 s under load. All three now share one read.

**What it taught, and the reason depth matters:** rivals took the top FOUR rows
of our queue in the four picks between the kill and our turn. ESPN reached row 4
for pick 49 and row 5 for 52. **A queue sitting at the `K_MIN=3` floor would
have been exhausted and pick 49 would have fallen through to ESPN's own board.**

Measured the same day over a full draft: off-turn queue depth is median 4, but
**22% of the time below the floor and 8% empty**, worst continuous stretch 19 s.
On-turn thinness is excluded — the writer structurally cannot add during your
own clock, and autopick reads the queue at turn start, so it is harmless.

That 22% was at CPU practice pace, where the writer reported once per ~4 picks.
**Re-measured the same evening against a 30 s human clock and the concern
largely dissolves**: 1 report per 0.89 picks, median depth **7 of a max 8**,
below the floor only **4%** of the time and empty 3%. `no_control` add failures
fell from 14 to 3 over the same comparison. Monday's 90 s clock is 3x slower
again, so expect healthier still. The CPU-pace numbers were a pace artifact, as
the cadence arithmetic predicted — this is now measured, not inferred.

Still true, and the reason to keep the finding: if you ever restart the cockpit
mid-draft, glance at the writer line before walking away. Restarting into a thin
queue is the one combination these findings say is dangerous.

### 8.2 The refusal test (spec §8.4) — RUN 2026-08-29, PASSED in two halves

Refusal: six players poisoned at the `espn_names` seam. The writer searched,
got `not_in_pool`, and skipped — **no poisoned name ever reached the queue and
nothing similar was substituted**. Queue stayed usable throughout.

Push: **a practice draft cannot test this.** The deficit push needs 6
consecutive thin reports while your pick is within 10; at CPU pace only 2-3 land
in that window before your turn resets the streak. Zero pushes in a practice run
is uninformative. Driven directly instead: fired on exactly the 6th report,
`ntfy=200`, and 8 further reports added none — the one-per-draft budget holds.
First time the draft cockpit's ntfy path has fired live.

Harnesses for both are in gitignored `intel/research/` (see
`acceptance-tests-2026-08-29.md`).

### 8.2b The notification sidecar (prototype — use it on draft night)

`intel/research/draft_sidecar.py` (gitignored, persists across sessions). Runs
BESIDE the cockpit, changes no shipped code, and publishes through the same
`make_draft_pusher` egress so the Rule-5 scrub still applies:

```bash
.venv/bin/python intel/research/draft_sidecar.py --slot 9 --heartbeat-min 10
```

It exists because the shipped push lane is tuned for a season of waiver quiet,
which is the wrong setting for one evening when the operator is putting a child
to bed and wants to know the thing has not silently died. It sends **each of
your picks as it lands** (16, naturally bounded, and the signal the operator
actually wants), a **heartbeat every N minutes** naming facts rather than "OK",
and rate-limited alarms for: pick feed blocked, writer silent, tab hidden,
autopick not ON, thin queue near your pick, cockpit unresponsive.

Validated end to end on the 2026-08-29 dress rehearsal (public mock, 30 s clock,
operator away): 18 pushes, all `ntfy=200`, correct throughout. Fold the useful
parts into the cockpit's own push lane after the draft — the shipped budgets
(`_PUSH_DEFICIT_MAX=1` etc.) remain unchanged and untuned for draft night.

### 8.3 Clean up afterwards

```bash
mkdir -p data/draft/practice && mv data/draft/session-*.jsonl data/draft/practice/
```

This keeps `data/draft/sync-token.txt` where it belongs (so the installed
userscripts keep working) while removing practice journals from bare-`--resume`
discovery, which only ever looks one directory deep.

---

## 9. Known, accepted, and not worth fixing at the table

- **The kicker board is UNCORRECTED, and the cockpit says so at launch.** The
  Sleeper projections feed publishes made-FG distance buckets that do not add up
  to its own made-FG total: it never serves the 50+ bucket, so every long field
  goal scores zero while every miss still charges −1. Measured on the live board:
  all 32 starting kickers understated by 25–43 points on a ~120-point season, and
  the shortfall is **not** a constant fraction (8.8%–25.8%), so it REORDERS the K
  board rather than scaling it. `ziggurat/core/kicker_board.py` is the fix and it
  is wired in — but its source table (`espn_projections`) has never been pulled
  on this box, so the correction reports OFF. **The caveat rides the kicker
  recommendation itself**, not just the launch banner: the panel at round 10 says
  in so many words that this K board is uncorrected, that the shortfall reorders
  it rather than scaling it, and that WHICH kicker is therefore close to a
  coin-flip among the top few. **Deliberately not fixed tonight**:
  filling it needs a live ESPN pull whose rows can only be stamped *today*, which
  is invisible at the golden master's frozen `as_of` — i.e. the board that
  decided the picks could not be the board any test had ever seen. Post-draft
  work, with a source spec and a re-blessed golden. The consequence is confined
  to WHICH kicker goes at round 10.
- **A third QB in the last rounds.** One misallocated bench slot in a 1-QB
  league; the tail runs into the QB cap. The starting-lineup metric that grades
  the engine scores bench picks zero, so it cannot see this either way. The
  week-by-week engine does not fix it and was never expected to: it prices a
  bench QB3 at zero for the same reason.
- **Every margin the default engine claims is against a MODEL of the room.** The
  +0.04 expected wins is measured against the calibrated 2.2 simulation of nine
  rivals, on our own projections, graded by our own week-by-week objective. The
  one external validation this project has (2021–2025 FantasyPros ECR boards,
  graded on realized weekly points) could **not** demonstrate that the engine
  beats drafting the preseason consensus straight down — mean +0.066 wins and not
  one of 16 cells excluding zero. That result is about roster CONSTRUCTION only
  (no historical point-in-time projections exist, so every strategy shares the
  within-position ordering), and it is the right amount of humility to hold about
  tonight.
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
- **`no_control` add failures** — the writer finds a player's row but it offers
  no queue button. Second-commonest cause of a thinning queue (14 of 51 add
  failures in the 2026-08-29 run), clustered by cycle and weighted late. The
  attractive explanation — ESPN's position maximums — was tested and is WRONG
  (we held 4 RBs against a max of 8 at most of the failures). Accepted for
  Monday because the degradation is the designed one: the add is charged, the
  player is skipped after two failures, the next recommendation is taken, and it
  never produces a wrong pick. The real defect is diagnostic — the label
  collapses "no queue button exists" with "the button is disabled", which have
  different causes. Split it and re-measure after the draft, not 48 hours
  before it.

---

## 10. Quick reference

| | |
|---|---|
| Draft | Mon 2026-08-31, 19:00 PT (room opens 18:00) |
| Format | Snake, 16 rounds, 90 s/pick, 10 teams |
| Our slot | **9** |
| Our picks | 9, 12, 29, 32, 49, 52, 69, 72, 89, 92, 109, 112, 129, 132, 149, 152 |
| Command | `.venv/bin/ziggurat draft-web --season 2026 --slot 9` |
| If it looks wrong | same command `--legacy-engine` (§6 rung 0) |
| Cockpit | `http://127.0.0.1:8811/` |
| Userscripts | `/sync.user.js` (v1.2), `/queue.user.js` (v1.9) |
| Journal | `data/draft/session-<timestamp>.jsonl` |
| Sync token | `data/draft/sync-token.txt` — do not delete |
| Practice venue | ESPN mock lobby → "League Specific Practice Draft" |
