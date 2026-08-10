# Runbook: moving Ziggurat's base of operations to the Strix Halo

**Written 2026-07-25, before the draft.** The desktop becomes the season-long
host: it runs every timer and holds the authoritative database. The laptop keeps
a checkout for development and *possibly* for draft day, but stops running
anything on a schedule.

Read this once end to end before starting. The mechanical steps are easy; the
two things that can actually cost you something are in §1 and §2.

---

## 1. The database moves. It is never re-created.

`ziggurat db init` on the desktop would produce a perfectly working system and
silently discard everything that cannot be pulled again. Sorted by whether a
missed day is recoverable:

| data | recoverable if lost? | why |
|---|---|---|
| `league_player_state`, `league_teams`, `league_matchups`, `league_sync_runs` | **NO — never** | ESPN serves league state as a CURRENT SNAPSHOT ONLY. There is no historical backfill for rosters, lineups, or transactions. Whatever was not captured that day is gone permanently. |
| `espn_draft_ranks` | no | the draft board as it stood on a given day |
| `adp_rankings` | no | a daily FantasyPros scrape |
| `projections` | no | Sleeper serves the current projection only |
| `game_weather` (forecast rows) | no | a forecast is only a forecast before kickoff |
| everything nflverse (`weekly_stats`, `snap_counts`, `injuries`, `ngs_*`, `schedules`, `team_defense`, `game_odds`, `depth_charts`) | **yes, always** | whole-season files, re-downloaded in full every pull |

So: **copy `db/ziggurat.sqlite`. Do not run `db init` on the desktop.**

Copy it *before* running the backfill — move ~43 MB and let the desktop spend 40
seconds rebuilding the ~124 MB, rather than shipping 124 MB over the network.

> If you are reading this long after it was written and the copy is a season's
> worth of league history rather than two pre-draft days, this section is the
> whole runbook and the rest is detail.

---

## 2. Exactly ONE machine runs the timers

Two machines running `league-sync` is **not** a redundant copy. It is two
independently valid, divergent, partial histories with no merge path. The moment
either box is asleep at a scheduled firing, that database has a permanent hole
the other does not, and nothing in this repo can reconcile them — you end up with
two files and no way to say which one is the truth.

`Persistent=true` on the timers catches up a firing missed while the box was off,
but it does not rescue league state: ESPN only ever hands you *"now"*, so a
catch-up run gives you that moment's snapshot, not the one you missed.

A laptop is structurally the wrong host for a 4×/day timer against a source with
no backfill — it sleeps, it travels, it gets closed. Hence:

- **Desktop: authoritative. Runs all four timers.**
- **Laptop: timers UNINSTALLED**, not merely stopped, so a future you cannot
  re-enable them by accident.

**Do the laptop uninstall and the desktop install in the same sitting.** The
window in which both are firing is the window in which the two databases begin to
diverge.

---

## 3. Desktop setup

### 3.1 Checkout and environment

```bash
git clone git@github.com:nomindnick/ziggurat.git && cd ziggurat
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
git config core.hooksPath scripts/hooks      # per clone, REQUIRED (public-repo boundary hook)
```

`nflreadpy` is a declared dependency; there is no second install step.

### 3.2 Move the three gitignored things, out of band

Never through git. Use `scp`, a USB stick, whatever — but not a commit, not a
gist, not a paste into a chat window.

| what | size | notes |
|---|---|---|
| `.env` | tiny | `SWID`, `ESPN_S2`, `ESPN_LEAGUE_ID`. Keep `SWID`'s surrounding `{ }` braces; do **not** URL-decode `ESPN_S2`. |
| `intel/` | ~500 KB | the private judgment layer: research notes, opponents, heuristics. `ziggurat intel init` only recreates an empty skeleton — it will not bring your notes. |
| `db/ziggurat.sqlite` | ~43 MB | see §1. Copy it with nothing running against it. |

`data/` (~93 MB) is recon scratch and old backups. Leave it behind; nothing reads it.

### 3.3 Verify before you schedule anything

```bash
.venv/bin/pytest                 # expect 1219 passed, 2 skipped
.venv/bin/ziggurat smoke         # spine wiring sanity check
.venv/bin/ziggurat league status # your existing days, and MISSING DAYS: none
.venv/bin/ziggurat ingest status # per-source last pull + staleness
```

If `pytest` is red, stop and fix that first — do not install timers on a box
whose suite does not pass.

> **The desktop must be on this code before it opens that database.** The DB is
> at `schema_version 7`; pre-3.2c code refuses it with *"schema version 7 is
> newer than supported version 6"*. A fresh clone of `main` is fine.

### 3.4 Land the NFL history

```bash
.venv/bin/ziggurat ingest run --dry-run          # see the plan, touches no network
.venv/bin/ziggurat ingest backfill --first 2021 --last 2025
```

~40 seconds, 55 source-season pairs, database grows to ~124 MB. This is separate
from the scheduled command on purpose: the cadence is a *current-season*
refresher and will never pull history on its own.

Backfilled rows carry `retrieved_as_of = today` with their real historical
`knowable_as_of`, so **the default `historical` view returns nothing for a past
`as_of`** — that is Rule 1 working correctly, not a bug. Historical reads go
through `base.latest_truth(accessor)`.

### 3.5 Install the timers

```bash
loginctl enable-linger "$USER"     # FIRST — or every timer dies at logout
scripts/install-league-sync.sh
scripts/install-nfl-ingest.sh
systemctl --user list-timers 'ziggurat-*' --no-pager
```

What you should see:

| unit | schedule | what it protects |
|---|---|---|
| `ziggurat-league-sync` | 05:15, 11:15, 17:15, 23:15 | the unrecoverable one. 4×/day is resilience, not four distinct facts — you need one good run per day. |
| `ziggurat-nfl-ingest` | 07:20 daily | players, schedules, projections, ADP, board, in-season odds + injuries |
| `ziggurat-nfl-ingest-weekly` | 08:20 daily | fires daily, but each source's `interval_days` and the run log decide — so a failed Thursday retries Friday instead of costing an in-season week |
| `ziggurat-nfl-ingest-gameday` | 16:20 daily | weather forecasts inside the ~10-day horizon |

Force one of each immediately, so a failure surfaces now rather than at 05:15:

```bash
systemctl --user start ziggurat-league-sync.service
journalctl --user -u ziggurat-league-sync.service -n 50
.venv/bin/ziggurat league status
```

### 3.6 Decommission the laptop's timers

On the **laptop**:

```bash
scripts/install-league-sync.sh --uninstall
scripts/install-nfl-ingest.sh --uninstall
systemctl --user list-timers 'ziggurat-*' --no-pager   # expect nothing
```

Keep the laptop's `db/ziggurat.sqlite` as a frozen backup if you like, but from
this moment treat it as **dead** — it will drift and it is not the truth.
Renaming it (e.g. `ziggurat.pre-cutover.sqlite`) is a cheap way to stop yourself
trusting it later — any `.sqlite`, `.sqlite.<anything>` or `.sqlite-<anything>`
name stays gitignored and hook-blocked.

---

## 4. Draft day

**DECIDED 2026-08-10: the 2026 draft runs from the DESKTOP.** The draft is
Monday 2026-08-31 19:00 PT — at home, not the office, which is what the laptop
plan below was written for. Drafting on the desktop is the simpler path in every
respect and it is the one to follow; §4.2 is kept only as the fallback if you
ever do have to draft away from this box.

This is all cleaner than it looks, because **`ziggurat/draft/` never writes to
the database.** It contains no `INSERT`, `UPDATE`, `DELETE`, `upsert`, or
`commit()` — it reads the board and persists only to a local timestamped
`session-*.jsonl` journal, which is what `--resume` replays. So there is no
merge problem and nothing to copy back, and **drafting on the timer box is safe:
leave the timers running throughout.**

### 4.1 From the desktop (the 2026 plan)

No database copy, no second environment, no divergence risk — the board is
already refreshed daily here by the 3.1b timers. What actually needs doing:

- **Install Tampermonkey in Chrome** (as of 2026-08-10 it is NOT installed on
  this box — verified against `~/.config/google-chrome/*/Extensions`). Without
  it there is no DOM sync and every pick is manual entry.
- Start the cockpit, open `http://127.0.0.1:8811/sync.user.js` once to install
  the per-run script (the port and token are baked in at serve time).
- **Get the seat translation right — this is the one that silently ruins a
  draft.** ESPN's `draftSettings.pickOrder` is a list of **1-based team ids** in
  draft-position order; `draft-web --pick-order` wants **0-based seat ids**, and
  `--slot` is 1-based. Seating the engine in the wrong chair raises no error —
  it just plays someone else's hand, with every survival estimate wrong. Derive
  it, do not eyeball it.
- One full dress rehearsal on this box against sim rivals before the day.

### 4.2 From the laptop (fallback only)

**Before (a day or two out), on the desktop:**

```bash
.venv/bin/ziggurat ingest run --source espn_ranks --source projections --source adp_rankings
```

then copy `db/ziggurat.sqlite` to the laptop, so the laptop drafts off a current
board.

**On the laptop, before you leave:**

- confirm `.env` is present and the cookies are still good (`ziggurat league status`)
- install the Tampermonkey userscript in **the browser you will actually have
  open in that room**
- do a full dry run — `ziggurat draft-web` with sim rivals — on the actual
  machine you are bringing, on its real network

**During:** the laptop reads its own copy. **The desktop keeps its timers
running the entire time.**

**After:** nothing comes back from the laptop. ESPN flushes its league views
atomically when the draft completes, so the desktop's own `league sync` imports
the real rosters straight from ESPN within a few hours. That first post-draft
snapshot is the most valuable league-state capture of the season — which is the
strongest reason not to disturb the desktop's cadence on draft day.

Keep the journal file. It is the record of what the tool recommended versus what
you actually picked, and Phase 4 grades decisions rather than outcomes.

---

## 5. Ongoing, once the season starts

```bash
.venv/bin/ziggurat league status   # last run + UNRECOVERABLE missing days
.venv/bin/ziggurat ingest status   # per-source last successful pull + staleness
```

Check both whenever you refresh ESPN cookies, after any reboot or OS upgrade, and
any time the machine has been off for more than a day.

**The two reports deliberately use different language.** `league status` says
"unrecoverable" and "missing days" because there those words are literally true.
`ingest status` never uses them — nflverse is re-pullable, and crying wolf there
would train you to ignore the one report where the alarm is real. Only the four
genuinely perishable NFL sources (`projections`, `adp_rankings`, `espn_ranks`,
`game_weather` in forecast mode) get loss language, and only once they have
actually expired.

### When ESPN cookies expire

They will. Symptoms: `league sync` failing, or an ESPN-backed pull going
`skipped` for want of credentials. Refresh `SWID` and `ESPN_S2` from a logged-in
browser session (DevTools → Application → Cookies → `https://fantasy.espn.com`),
update `.env` **on the desktop**, then:

```bash
systemctl --user start ziggurat-league-sync.service
.venv/bin/ziggurat league status     # confirm the gap is only the days you lost
```

### The push layer — briefing + alerts to your phone (item 3.6)

One-time setup on the desktop:

```bash
# 1. Pick a HIGH-ENTROPY ntfy topic (the topic name IS the password on ntfy.sh).
#    Put it in .env (never committed, never logged — treat it like SWID):
#      NTFY_TOPIC=zig-<32 random chars>
#      NTFY_SERVER=https://ntfy.sh        # optional; this is the default
#      NTFY_TOKEN=tk_...                  # optional; only for a reserved/self-hosted topic
# 2. Install the ntfy app on your phone and SUBSCRIBE to that topic string.
# 3. Install the timers (Wed 06:00 briefing + a 20-min alert tick, both Pacific):
scripts/install-push.sh
loginctl enable-linger "$USER"           # or the timers die at logout

# Test without pushing / without spending an LLM call:
.venv/bin/ziggurat brief run  --no-push --no-llm
.venv/bin/ziggurat alerts run --no-push
# Then a real one:
systemctl --user start ziggurat-brief.service
.venv/bin/ziggurat brief status          # last briefing runs (silence is not success)
.venv/bin/ziggurat alerts status         # last alert ticks ('empty' is HEALTHY)
```

The **outbound scrub** is what makes the cheap public-topic config safe: a colleague's
team name can never cross to ntfy even on a world-readable topic (the teaser is built
from counts + public player names + your own team only, and a data-driven denylist
refuses anything else). If you want real transport privacy too, the upgrade is
`.env`-only, no code change: **self-host ntfy on the desktop behind Tailscale** (deny-all
+ token, `NTFY_SERVER` = the tailnet URL) or an **ntfy.sh Pro reserved topic**.

The briefing writes the full markdown to gitignored `intel/weekly/briefings/` regardless
of whether the LLM prose or the ntfy push succeed — a token hiccup or a dead topic never
costs you the underlying facts. `injury_transitions` (the live injury→handcuff arm)
produces nothing until real games start; the news wire and the briefing are useful before
then, the alert arm proves out in Week 1.

---

## 6. Things that will bite

- **Editing an applied migration.** The timers run `ziggurat` from the working
  tree, so uncommitted code is the production cadence. An applied migration is
  never re-applied — editing one leaves the live database permanently describing
  a schema no file holds, while the whole test suite agrees with the file.
  Corrections always ship as a **new** migration.
  `test_an_applied_migration_is_never_edited` enforces this, but only if you run
  the suite.
- **`git pull` on the desktop mid-season** brings new migrations, which the next
  timer firing applies to the live database automatically. That is usually what
  you want — but pull deliberately, not incidentally, and run `pytest` after.
- **Forgetting `loginctl enable-linger`.** Everything works until you log out,
  then silently stops. `ziggurat league status` is how you find out.
- **Two boxes syncing.** See §2. The failure is silent and there is no repair.
- **A backup named `foo.sqlite.bak`.** Covered — `.gitignore`, `repo_guard.py`
  and the pre-commit hook all match on a separator class now (`.sqlite`,
  `.sqlite.*`, `.sqlite-*`). But the boundary is only ever as wide as its
  patterns, and it can fail in *both* directions: the first attempt at this fix
  used a bare `*.sqlite*`, which would have silently ignored a source file named
  `foo.sqliteish.py`. Do not invent a new extension for a file full of
  league-private data, and do not widen a boundary pattern without running
  `pytest tests/test_repo_boundary.py`.
- **The clocks.** `OnCalendar` is local time. If the two boxes are in different
  timezones the schedules above mean different things.

---

## 7. Uninstall / rollback

```bash
scripts/install-league-sync.sh --uninstall
scripts/install-nfl-ingest.sh --uninstall
```

Both are user-level systemd units — no root, no system-wide side effects. If the
desktop has no user systemd at all, the tail of each installer prints a cron
equivalent that keeps the `timeout` wrapper (cron has no `TimeoutStartSec`, and a
hung pull would otherwise never end).
