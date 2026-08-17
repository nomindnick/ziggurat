// ==UserScript==
// @name         Ziggurat queue writer
// @namespace    ziggurat
// @version      1.5
// @description  Keep ESPN's Pick Queue equal to the cockpit's desired queue (GET /api/queue) so ESPN's own autopick commits Ziggurat's pick when the clock expires. Never clicks Draft. Auto-entry spec §6.
// @match        https://fantasy.espn.com/football/draft*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

// v1.1 — the 35-agent audit round. The v1.0 core defect was treating
// surname+initial as an IDENTITY: "Brian Thomas Jr." and "B. Robinson Jr."
// both reduced to (jr, b) and verified green with the wrong player at the
// queue head. Identity now strips suffixes, uses team/position evidence from
// the row text, and REFUSES on ambiguity (protected rows are never removed).
// Also from the audit: the skip ledger could empty the whole queue and report
// ok (all-skipped guard + depth floor + charge-once + systemic-failure
// detection); a HALTED writer went silent exactly when step 4 most needs the
// failure streak (halted writers keep reporting); the head-changed rebuild
// emptied the queue for the whole add phase (iterative fix loop, add-before-
// remove when the needed player is absent); and the Autopick toggle — the P2
// load-bearing unknown, CONFIRMED 2026-08-16 — is observed and reported
// every cycle.
//
// v1.4 — after the first live mock (spec §6c). The operator's turn now runs
// the SAME reconcile loop as off-turn: v1.3's head-fix-only policy left the
// observed empty-queue-at-expiry state unfixable, and attempting adds is
// structurally safe (Button--queue or nothing; on_clock refusals break the
// loop before any removal could strip an unfillable queue). DST searches use
// the nickname alone; the add wait polls instead of sleeping fixed.
//
// v1.5 — run 2 (which executed v1.3; spec §6c). The not_in_pool epidemic was
// the grid's POSITION FILTER: search results are scoped by it and ESPN
// drifts it with its own need suggestions. The writer now sets the filter to
// the target's position before every search and restores All Pos. after.

(() => {
  "use strict";
  const COCKPIT = "http://127.0.0.1:{{PORT}}";
  const TOKEN = "{{TOKEN}}";
  const VERSION = "1.5";

  const TICK_MS = 1500;         // watch cadence (history signature + due polls)
  const POLL_MS = 5000;         // /api/queue refresh even when nothing observed
  const MAX_OPS_PER_CYCLE = 30; // fence: worst legal rebuild ≈ K removes + 2K adds
  const MAX_FAILS_PER_HEAD = 2; // per-player add refusals at one head (spec §7)
  const K_MIN = 3;              // §7 depth floor: below this, ok is never true

  const LEAGUE = new URLSearchParams(location.search).get("leagueId") || "";

  // ---- mode / kill-switch ---------------------------------------------
  // LIVE — reconciling. PAUSED — operator clicked the badge; observe+report
  // only. HALTED — the §5c tripwire fired; reload-only, but the writer KEEPS
  // REPORTING (audit: a silent halt froze the server's failure streak at 1 in
  // exactly the state step 4 must escalate on).
  let mode = "LIVE";
  let haltReason = "";

  // ---- tiny utils ------------------------------------------------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // v1.2: the paste-back trail. The badge is one line; a live test needs the
  // decisions on record — filter the DevTools console on [zig-queue] and copy.
  function qlog() {
    try {
      console.log.apply(console, ["[zig-queue]"].concat([].slice.call(arguments)));
    } catch (e) { /* console withheld is not a failure */ }
  }

  // `el.className` is an SVGAnimatedString on SVG nodes — read the attribute.
  function clsOf(el) {
    if (!el || !el.getAttribute) return "";
    const attr = el.getAttribute("class");
    if (attr) return attr;
    const c = el.className;
    if (typeof c === "string") return c;
    return (c && c.baseVal) || "";
  }

  function isDisabled(el) {
    return el.disabled === true || el.getAttribute("aria-disabled") === "true";
  }

  function centerOpts(el) {
    const r = el.getBoundingClientRect();
    return {
      bubbles: true, cancelable: true, composed: true, view: window,
      button: 0, buttons: 1,
      clientX: Math.round(r.left + r.width / 2),
      clientY: Math.round(r.top + r.height / 2),
    };
  }

  // A control that only exists on hover has NO DOM node until React sees a
  // mouseover (probe §4a hazard 2).
  function hoverRow(row) {
    const o = centerOpts(row);
    const seq = [
      ["pointerover", PointerEvent, true],
      ["pointerenter", PointerEvent, false],
      ["mouseover", MouseEvent, true],
      ["mouseenter", MouseEvent, false],
      ["mousemove", MouseEvent, true],
    ];
    for (const [type, Ctor, bubbles] of seq) {
      try { row.dispatchEvent(new Ctor(type, Object.assign({}, o, { bubbles }))); }
      catch (e) { /* older ctor missing is not a failure */ }
    }
  }

  // React tracks the previous value on the node — go through the native setter.
  function setNativeValue(el, value) {
    const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function searchInputs() {
    const hits = [];
    for (const el of document.querySelectorAll('input[type="text"], input[type="search"], input:not([type])')) {
      const r = el.getBoundingClientRect();
      if (r.width < 40 || r.height < 8) continue;
      hits.push(el);
    }
    const hint = (e) =>
      /player|search|name/i.test(
        (e.placeholder || "") + " " + clsOf(e) + " " + (e.getAttribute("aria-label") || "")
      ) ? 0 : 1;
    hits.sort((a, b) => hint(a) - hint(b));
    return hits;
  }

  // ---- the position filter (v1.5 — run 2's decisive finding) -------------
  // The grid's search results are SCOPED BY THE POSITION FILTER dropdown, and
  // the filter drifts with ESPN's own need suggestions: run 2 (2026-08-16)
  // watched DST adds fail not_in_pool for six straight rounds and then land
  // the moment the filter reached D/ST — while QBs simultaneously STARTED
  // failing. The writer must own the filter, deterministically, per add.
  // Verified live: <select class="dropdown__select"> with options
  // All Pos.=-1, QB=0, RB=2, WR=4, TE=6, FLEX=23, D/ST=16, K=17.
  const POSITION_FILTER_VALUES = { QB: "0", RB: "2", WR: "4", TE: "6", DST: "16", K: "17" };
  function positionFilterSelect() {
    for (const s of document.querySelectorAll("select")) {
      const texts = [...s.options].map((o) => (o.textContent || "").trim());
      if (texts.includes("QB") && (texts.includes("D/ST") || texts.includes("DST"))) return s;
    }
    return null;
  }
  async function setPositionFilter(pos) {
    const sel = positionFilterSelect();
    if (!sel) return false;
    const want = POSITION_FILTER_VALUES[String(pos || "").toUpperCase()] || "-1";
    if (sel.value === want) return true;
    setNativeValue(sel, want); // native setter + change event, like the search box
    await sleep(300);
    return true;
  }

  // ---- queue panel readers (proven 2026-08-13..15) ---------------------
  function queuePanel() { return document.querySelector(".pick-queue"); }

  const QUEUE_ROW_SELECTORS = [
    '[class*="fixedDataTableRowLayout_main"]',
    "li",
    '[class*="Table__TR"]',
    "tr",
  ];
  // An EMPTY queue still renders two rows: the column header ("RankPLAYER")
  // and the empty-state row ("No players in queue").
  function isNoiseRow(r) {
    if (/header/i.test(clsOf(r))) return true;
    if (r.closest('[class*="headerLayout"], thead, [class*="THEAD"]')) return true;
    if (r.querySelector("th")) return true;
    const t = (r.textContent || "").replace(/\s+/g, " ").trim();
    if (t.length <= 3) return true;
    if (/^no players? in queue/i.test(t)) return true;
    if (/^rank\s*player$/i.test(t)) return true;
    return false;
  }

  function queueEntries() {
    const p = queuePanel();
    if (!p) return { mode: "NO_PANEL", rows: [], empty: false };
    // ESPN's own `empty` class on the panel is the authoritative signal.
    const flaggedEmpty = /(^|\s)empty(\s|$)/.test(clsOf(p));
    for (const sel of QUEUE_ROW_SELECTORS) {
      const all = [...p.querySelectorAll(sel)];
      if (!all.length) continue;
      const rows = all.filter((r) => !isNoiseRow(r));
      if (flaggedEmpty) return { mode: sel, rows: [], empty: true };
      if (rows.length) return { mode: sel, rows, empty: false };
    }
    return { mode: "NO_ROWS", rows: [], empty: flaggedEmpty };
  }

  // The anchor carries the CLEAN player name; the full row text additionally
  // carries team/position — the disambiguation evidence identity needs.
  function entryName(row) {
    const a = row.querySelector("a");
    const t = a ? (a.textContent || "").trim() : "";
    if (t) return t;
    return (row.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40);
  }
  function rowText(row) {
    return (row.textContent || "").replace(/\s+/g, " ").trim();
  }
  function queueNames() { return queueEntries().rows.map(entryName); }

  // ---- identity (§5c — and the v1.0 audit's core finding) ---------------
  // The queue panel ABBREVIATES ("B. Sauls"); the grid/history/espn_name do
  // not ("Ben Sauls"). Identity = suffix-stripped surname + first initial,
  // PLUS team/position evidence from the row text when present, and REFUSAL
  // on ambiguity. surname alone was proven non-unique three ways: suffixes
  // ("Brian Thomas Jr." → "jr"), same-initial namesakes (Jameson/Javonte
  // Williams → "J. Williams"), and DSTs (every one → "dst").
  const SUFFIXES = { jr: 1, sr: 1, ii: 1, iii: 1, iv: 1, v: 1 };
  // Static NFL team codes as ESPN renders them (not league data, Rule 5 safe).
  const TEAM_CODES = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WSH", "WAS",
  ];
  const POS_TOKENS = ["QB", "RB", "WR", "TE"]; // multi-letter only; K collides with text

  function nameTokens(display) {
    return String(display || "").replace(/\./g, " ").replace(/\s+/g, " ").trim()
      .split(" ")
      .map((t) => t.replace(/[^A-Za-z'-]/g, ""))
      .filter(Boolean);
  }
  function surnameOf(display) {
    const toks = nameTokens(display);
    while (toks.length > 1 && SUFFIXES[toks[toks.length - 1].toLowerCase()]) toks.pop();
    return (toks[toks.length - 1] || "").toLowerCase();
  }
  function initialOf(display) {
    const toks = nameTokens(display);
    return ((toks[0] || "").slice(0, 1) || "").toLowerCase();
  }
  function isDstText(s) { return /d\/?st\b/i.test(String(s || "")); }
  function dstNickname(display) {
    // Letters only: a queue row's leading rank digit must not become a
    // phantom nickname ("9D/ST" is unreadable, not the "9" defense).
    return String(display || "").replace(/d\/?st\b.*/i, "")
      .replace(/[^A-Za-z' -]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
  }
  // The name this writer searches and verifies with: ESPN's own display text.
  function espnKey(d) { return d.espn_name || d.name || ""; }

  // Team codes present in a row's text (uppercased). Codes can occur inside
  // names ("LARSON" contains LAR), so evidence is only trusted when exactly
  // one code is visible or the expected code is among them.
  function teamCodesIn(text) {
    const up = String(text || "").toUpperCase();
    return TEAM_CODES.filter((c) => up.includes(c));
  }

  // Weak identity: suffix-stripped surname + compatible initial (or DST
  // nickname). Necessary, never sufficient on its own.
  function weakNameMatch(want, got) {
    if (!want || !got) return false;
    if (isDstText(want) || isDstText(got)) {
      if (!isDstText(want) || !isDstText(got)) return false;
      const nick = dstNickname(want);
      const gotNick = dstNickname(got);
      // Either side may be unreadable (abbreviated); require overlap when
      // both are readable, refuse (false) only on a clear mismatch — the
      // pairing layer protects unreadable DST rows from removal.
      if (nick && gotNick) return gotNick.includes(nick) || nick.includes(gotNick);
      return true; // both DST, at least one nickname unreadable: not a NON-match
    }
    if (surnameOf(want) !== surnameOf(got)) return false;
    const wi = initialOf(want), gi = initialOf(got);
    return !wi || !gi || wi === gi;
  }

  // Full match of a desired entry against a queue ROW: weak name match, then
  // team evidence from the row text. Returns "yes" | "no" | "unsure".
  //   confirmed  — row text shows the expected team code
  //   contradicted — row text shows exactly ONE code and it is a different one
  function rowMatch(d, name, text) {
    const want = espnKey(d);
    if (isDstText(want) || isDstText(text)) {
      if (!isDstText(want) || !isDstText(text)) return "no";
      const nick = dstNickname(want);
      const gotNick = dstNickname(name) || dstNickname(text);
      if (nick && gotNick) {
        return gotNick.includes(nick) || nick.includes(gotNick) ? "yes" : "no";
      }
      return "unsure"; // a DST row we cannot read the identity of
    }
    if (!weakNameMatch(want, name)) return "no";
    const codes = teamCodesIn(text);
    const dTeam = String(d.team || "").toUpperCase();
    if (dTeam && codes.includes(dTeam)) return "yes";
    if (dTeam && codes.length === 1 && codes[0] !== dTeam) return "no";
    // No usable team evidence: fall back to position evidence if present.
    const up = String(text || "").toUpperCase();
    const dPos = String(d.position || "").toUpperCase();
    if (POS_TOKENS.includes(dPos)) {
      const others = POS_TOKENS.filter((p) => p !== dPos && up.includes(p));
      if (up.includes(dPos) && !others.length) return "yes";
      if (!up.includes(dPos) && others.length === 1) return "no";
    }
    return "unsure";
  }

  // One line per queue row: its raw text and how the pairing judged it.
  // Deduped by signature so a stable queue does not spam the console. This is
  // ALSO the §6b DOM-assumption evidence (team codes? DST row shape?) — the
  // whole point of the paste-back trail.
  let lastPairLogSig = "";
  function logPairing(desired, pairing) {
    const lines = pairing.names.map((n, i) => {
      const v = pairing.rowPair[i];
      const verdict =
        v === "stale" ? "STALE (removable)"
        : v === "protected" ? "PROTECTED (ambiguous — never removed)"
        : "= desired[" + v + "] " + espnKey(desired[v]);
      return `  row[${i}] "${pairing.texts[i].slice(0, 48)}" -> ${verdict}`;
    });
    const sig = lines.join("|") + "#" + desired.map((d) => d.player_id).join(",");
    if (sig === lastPairLogSig) return;
    lastPairLogSig = sig;
    qlog(`queue pairing (${pairing.names.length} row(s) vs ${desired.length} desired):`);
    for (const l of lines) qlog(l);
  }

  // Pair queue rows to desired entries. Refuse-not-guess (§7): a row is
  //   paired     — exactly one desired entry says "yes"/"unsure" and no other
  //                candidate survives
  //   stale      — no desired entry matches at all → removable
  //   protected  — matches ambiguously (or is an unreadable DST while a DST
  //                is desired) → NEVER removed, reported instead
  function pairRows(desired, rows) {
    const names = rows.map(entryName);
    const texts = rows.map(rowText);
    const rowPair = new Array(rows.length).fill(null);   // desired index | "stale" | "protected"
    const usedDesired = new Map();                        // desired idx -> row idx
    for (let i = 0; i < rows.length; i++) {
      const yes = [], unsure = [];
      for (let j = 0; j < desired.length; j++) {
        const v = rowMatch(desired[j], names[i], texts[i]);
        if (v === "yes") yes.push(j);
        else if (v === "unsure") unsure.push(j);
      }
      const cands = yes.length ? yes : unsure;
      if (!cands.length) { rowPair[i] = "stale"; continue; }
      if (cands.length > 1) { rowPair[i] = "protected"; continue; }
      const j = cands[0];
      if (usedDesired.has(j)) {
        // two rows claim the same desired entry — refuse both
        rowPair[usedDesired.get(j)] = "protected";
        rowPair[i] = "protected";
        continue;
      }
      usedDesired.set(j, i);
      rowPair[i] = j;
    }
    return { rowPair, usedDesired, names, texts };
  }

  // ---- pick-history reader ----------------------------------------------
  function historyPanel() { return document.querySelector(".pick-history"); }
  function historyNames() {
    const out = new Set();
    const p = historyPanel();
    if (!p) return out;
    for (const row of p.querySelectorAll('[class*="fixedDataTableRowLayout_main"]')) {
      const cells = [...row.querySelectorAll('[class*="fixedDataTableCellLayout_main"]')];
      if (cells.length < 2) continue;
      const t = (cells[1].textContent || "").replace(/\s+/g, " ").trim();
      if (t) out.add(t);
    }
    return out;
  }
  function historyReadable() {
    const p = historyPanel();
    return !!p && (p.textContent || "").length > 20;
  }
  // Virtualization means the ROW COUNT saturates once the viewport fills —
  // a count-only trigger goes permanently dead (audit). Sign the content.
  function historySig() {
    const p = historyPanel();
    if (!p) return "none";
    const t = p.textContent || "";
    return t.length + ":" + t.slice(0, 60) + ":" + t.slice(-60);
  }

  // ---- available-player grid --------------------------------------------
  function findPlayerRow(fragment) {
    const frag = String(fragment || "").trim().toLowerCase();
    if (!frag) return null;
    // Rank rows by what they OFFER: an undo button means ALREADY DRAFTED.
    let best = null;
    let bestScore = 99;
    for (const r of document.querySelectorAll('[class*="fixedDataTableRowLayout_main"]')) {
      if (r.closest(".pick-history") || r.closest(".pick-queue")) continue;
      if (!(r.textContent || "").toLowerCase().includes(frag)) continue;
      const cls = [...r.querySelectorAll("button,[role=button]")].map(clsOf).join(" ").toLowerCase();
      let score = 3;
      if (/button--queue/.test(cls)) score = 0;       // available, off-turn
      else if (/button--draft/.test(cls)) score = 1;  // available, on the clock
      else if (/button--undo/.test(cls)) score = 4;   // already drafted
      else if (cls) score = 2;
      if (score < bestScore) { best = r; bestScore = score; }
      if (bestScore === 0) break;
    }
    return best;
  }

  // ---- removal: STRICT Button--dequeue only (§4a hazard 3, §5c) ---------
  function dequeueButtonIn(row) {
    const b = row.querySelector("button.Button--dequeue");
    if (b && !isDisabled(b)) return b;
    return null;
  }

  async function resolveDequeue(getRow, tries) {
    for (let i = 0; i < (tries || 3); i++) {
      const row = getRow();
      if (!row) return null;
      hoverRow(row);
      await sleep(150 + i * 200);
      const again = getRow();
      if (!again) return null;
      const ctl = dequeueButtonIn(again);
      if (ctl) return ctl;
    }
    return null;
  }

  // Remove the queue entry displaying `rowName`. Mode-gated: after a HALT or
  // PAUSE nothing may click (audit: the wrong-player undo loop kept clicking
  // after the tripwire fired). Returns "gone" | "refused" | "stuck".
  async function removeQueueEntryByName(rowName) {
    if (mode !== "LIVE") return "refused";
    const key = String(rowName || "").toLowerCase();
    const locate = () => {
      const q = queueEntries();
      const i = q.rows.findIndex((r) => entryName(r).toLowerCase() === key);
      return i >= 0 ? q.rows[i] : null;
    };
    if (!locate()) return "gone";
    const tripwireArmed = historyReadable();
    const historyBefore = tripwireArmed ? historyNames() : new Set();
    const ctl = await resolveDequeue(locate);
    if (!ctl) return "refused";
    try { ctl.click(); } catch (e) { return "refused"; } // L1, the proven level
    const deadline = Date.now() + 2500;
    let gone = false;
    while (Date.now() < deadline) {
      await sleep(100);
      if (!locate()) { gone = true; break; }
    }
    // §5c: the confounder check runs AFTER the action. A NEW history row for
    // this player right after our click means the "removal" may have been a
    // draft — HALT on evidence. Identity uses the FULL matcher, not the bare
    // surname (audit: "jr"/"dst" degeneracies made false halts likely, and a
    // false halt freezes the writer for the whole unattended remainder).
    if (tripwireArmed) {
      await sleep(400);
      for (const h of historyNames()) {
        if (!historyBefore.has(h) && weakNameMatch(rowName, h)) {
          mode = "HALTED";
          haltReason = `a pick naming "${h}" landed right after our remove of "${rowName}" — possible mis-click`;
          qlog("!!! HALT: " + haltReason);
          return "stuck";
        }
      }
    }
    return gone ? "gone" : "stuck";
  }

  // ---- add (probe-proven mechanics; landed check hardened) ---------------
  async function addOne(d) {
    if (mode !== "LIVE") return { ok: false, reason: "not_live" };
    const displayName = espnKey(d);
    const key = String(displayName || "").trim().toLowerCase();
    if (!key) return { ok: false, reason: "no_name" };

    let row = findPlayerRow(displayName);
    let searchBox = null;
    let filterTouched = false;
    if (!row) {
      // The grid renders only what is on screen — drive ESPN's own filter.
      // The position dropdown FIRST (v1.5: search is scoped by it and ESPN
      // drifts it with its own suggestions), then the name search. DSTs
      // search by nickname alone ("Chargers" — the full display text returns
      // nothing); verification still requires the full text on the row.
      filterTouched = await setPositionFilter(d.position);
      const searchText = isDstText(displayName) ? dstNickname(displayName) : displayName;
      searchBox = searchInputs()[0] || null;
      if (searchBox) {
        setNativeValue(searchBox, searchText);
        // Poll instead of a fixed wait (v1.4): at the mock's 30 s pick pace
        // the fill rate was losing to the drain rate.
        const deadline = Date.now() + 1600;
        while (Date.now() < deadline && !row) {
          await sleep(150);
          row = findPlayerRow(displayName);
        }
      }
    }
    const clearSearch = async () => {
      // A live filter (text OR position) starves every later lookup.
      if (searchBox) setNativeValue(searchBox, "");
      if (filterTouched) await setPositionFilter("ALL");
      if (searchBox || filterTouched) await sleep(250);
    };
    if (!row) {
      await clearSearch();
      return { ok: false, reason: "not_in_pool" };
    }

    hoverRow(row);
    await sleep(200);

    // The grid RECYCLES row nodes — re-resolve and re-verify at the last
    // moment (§4a hazard 1, measured).
    row = findPlayerRow(displayName);
    if (!row || !(row.textContent || "").toLowerCase().includes(key)) {
      await clearSearch();
      return { ok: false, reason: "recycled" };
    }

    const btns = [...row.querySelectorAll("button, [role=button]")];
    const label = (b) => (b.textContent || "").trim().toLowerCase();
    // ONLY Button--queue (or a button literally reading "queue"). On the
    // clock the row's action button is DRAFT — never click it (§4a hazard 3).
    const qb =
      row.querySelector("button.Button--queue") || btns.find((b) => label(b) === "queue");
    if (!qb || isDisabled(qb)) {
      const drafted = btns.some((b) => label(b) === "undo" || /button--undo/i.test(clsOf(b)));
      const onClock = btns.some((b) => label(b) === "draft");
      await clearSearch();
      return { ok: false, reason: drafted ? "drafted" : onClock ? "on_clock" : "no_control" };
    }

    const beforeNames = queueNames();
    try { qb.click(); } catch (e) { await clearSearch(); return { ok: false, reason: "click_failed" }; }

    // Landed = a NEW row that matches THIS player. The v1.0 check (surname
    // substring of the whole panel text) read an existing same-surname row as
    // success (audit) — a failed add then never retried and never reported.
    const counts = new Map();
    for (const n of beforeNames) counts.set(n, (counts.get(n) || 0) + 1);
    const newRows = () => {
      const seen = new Map(counts);
      const out = [];
      for (const n of queueNames()) {
        const c = seen.get(n) || 0;
        if (c > 0) seen.set(n, c - 1);
        else out.push(n);
      }
      return out;
    };
    const deadline = Date.now() + 3000;
    let landedRow = null;
    while (Date.now() < deadline) {
      await sleep(100);
      const fresh = newRows();
      if (fresh.length) {
        landedRow = fresh.find((n) => weakNameMatch(displayName, n)) || null;
        if (landedRow || fresh.length) break;
      }
    }
    await clearSearch(); // a live filter starves every later lookup
    const fresh = newRows();
    if (!landedRow) landedRow = fresh.find((n) => weakNameMatch(displayName, n)) || null;
    const wrong = fresh.filter((n) => !weakNameMatch(displayName, n));
    if (wrong.length) {
      // "Somebody else arrived" is a side effect to UNDO, never a no-op.
      for (const w of wrong) await removeQueueEntryByName(w);
      if (!landedRow) return { ok: false, reason: "wrong_player" };
    }
    if (!landedRow) return { ok: false, reason: "no_effect" };
    return { ok: true };
  }

  // ---- Autopick toggle observation (P2 — the load-bearing unknown) -------
  // Whether expiry actually draws from the queue may depend on this toggle.
  // The writer cannot SET it safely (unmeasured control); it observes and
  // reports, so every practice run gathers the P2 evidence.
  function autopickState() {
    const p = queuePanel();
    if (!p) return null;
    // Verified live 2026-08-16 (mock 1506119378):
    //   <div class="autoPick-container">… <div class="… autoPick-toggle …">
    //     <label class="control …"><input type="checkbox"
    //       class="form__control form__control--toggle"> …
    // The v1.2 bug: a comma-selector matched the WRAPPER div (class contains
    // "toggle") before the input inside it, and a div has no .checked —
    // every read came back "unknown". Read state candidates in preference
    // order and take the first that actually carries a state.
    const read = (t) => {
      if (!t) return null;
      if (t.checked === true) return "on";
      if (t.checked === false) return "off";
      const aria = t.getAttribute("aria-checked");
      if (aria === "true") return "on";
      if (aria === "false") return "off";
      return null;
    };
    const known = read(p.querySelector('.autoPick-container input[type="checkbox"]'));
    if (known) return known;
    for (const t of p.querySelectorAll('input[type="checkbox"], [role="switch"]')) {
      const v = read(t);
      if (v) return v;
    }
    return "unknown";
  }

  // ---- cockpit API ------------------------------------------------------
  function api(method, path, body) {
    return new Promise((resolve) => {
      try {
        GM_xmlhttpRequest({
          method,
          url: COCKPIT + path,
          headers: Object.assign(
            { "X-Zig-Sync-Token": TOKEN },
            body ? { "Content-Type": "application/json" } : {}
          ),
          data: body ? JSON.stringify(body) : undefined,
          timeout: 8000,
          onload: (resp) => {
            let j = null;
            try { j = JSON.parse(resp.responseText || "null"); } catch (e) { /* null */ }
            resolve({ status: resp.status, json: j });
          },
          onerror: () => resolve({ status: 0, json: null }),
          ontimeout: () => resolve({ status: 0, json: null }),
        });
      } catch (e) {
        resolve({ status: 0, json: null });
      }
    });
  }

  // ---- status badge -----------------------------------------------------
  const badge = document.createElement("div");
  badge.style.cssText =
    "position:fixed;bottom:34px;right:8px;z-index:99999;font:12px monospace;" +
    "padding:4px 10px;border-radius:6px;background:#12151a;color:#45c98b;" +
    "border:1px solid #313a48;cursor:pointer;opacity:.92";
  badge.title = `Ziggurat queue writer v${VERSION} — click to pause/resume`;
  badge.textContent = "zig queue: starting";
  document.documentElement.appendChild(badge);
  badge.addEventListener("click", () => {
    if (mode === "HALTED") return; // reload to clear a halt
    mode = mode === "LIVE" ? "PAUSED" : "LIVE";
    status(mode === "PAUSED" ? "paused by you — click to resume" : "resuming");
  });
  function status(msg, bad) {
    badge.textContent = "zig queue: " + msg;
    badge.style.color = mode === "HALTED" ? "#e06c6c" : bad ? "#e0b06c" : "#45c98b";
  }

  // ---- failure ledger (spec §7: refusal is per-player) -------------------
  // Charges accrue once per player per CYCLE (the v1.0 retry pass double-
  // charged, reaching the skip cap in a single cycle), and only for evidence
  // ABOUT THE PLAYER: when every add in a pass fails the same way, that is
  // evidence about the environment, and nobody is charged (audit: a moved
  // search box skipped the entire desired list and wiped the queue).
  let failHead = null;
  const failCounts = new Map();
  function chargeFailures(addResults, headOverall, chargedThisCycle) {
    const fails = addResults.filter((r) => !r.ok);
    if (!fails.length) return;
    const systemic =
      fails.length >= 2 &&
      !addResults.some((r) => r.ok) &&
      fails.every((r) => r.reason === fails[0].reason);
    if (systemic) return;
    if (failHead !== headOverall) { failHead = headOverall; failCounts.clear(); }
    for (const r of fails) {
      if (r.reason === "on_clock" || r.reason === "not_live") continue; // turn/mode state, not the player
      if (chargedThisCycle.has(r.pid)) continue;
      chargedThisCycle.add(r.pid);
      failCounts.set(r.pid, (failCounts.get(r.pid) || 0) + 1);
    }
  }
  function isSkipped(pid, headOverall) {
    return failHead === headOverall && (failCounts.get(pid) || 0) >= MAX_FAILS_PER_HEAD;
  }

  async function reportStatus(overall, achieved, ok, reason) {
    await api("POST", "/api/queue/status", {
      league: LEAGUE,
      overall: overall,
      achieved: achieved,
      ok: ok,
      reason: reason || "",
      autopick: autopickState(),
    });
  }

  // ---- the reconciliation core (spec §6) --------------------------------
  // Iterative fix loop, converging position by position from the TOP of the
  // queue (the position autopick reads first). Each iteration re-reads the
  // panel and makes exactly one repair:
  //   * the row at the first wrong position is removed — UNLESS the player
  //     that belongs there is absent from the queue entirely, in which case
  //     he is ADDED first (append), so the queue is never emptied while a
  //     wanted player can still be fetched (§6 never-empty, audit-fixed);
  //   * a queue that is a correct proper prefix gets the next desired append.
  // Protected (ambiguous) rows are never removed; convergence stops there
  // and the cycle reports not-ok with the ambiguity named (refuse-not-guess).
  async function reconcileOnce(payload, chargedThisCycle) {
    const desiredAll = payload.desired || [];
    const overall = payload.overall_pick;

    let q = queueEntries();
    if (q.mode === "NO_PANEL") {
      return { ok: false, reason: "queue panel not found (Pick Queue tab closed?)", achieved: [] };
    }

    // Server contract (§6a): desired:[] means exactly "no operator pick
    // remains" — INERT. Never clear ESPN's queue on an empty list.
    if (!desiredAll.length) {
      return { ok: true, reason: "inert: no operator pick remains", achieved: queueNames() };
    }

    const problems = [];
    if (!historyReadable()) {
      // §5c disclosure, not a failure: with history unreadable the removal
      // tripwire is blind. Say so rather than silently skipping the check.
      problems.push("(note: pick-history unreadable — removal tripwire blind)");
    }

    // v1.4: the operator's turn runs the SAME loop as off-turn. v1.3's
    // head-fix-only policy assumed adds are impossible on-turn (probe:
    // Button--queue "present only off-turn") — but the first live mock was
    // observed refilling DURING the operator's turn, and the empty-queue-at-
    // expiry state it guarded against is the worst outcome available (ESPN's
    // pick, not the engine's). Attempting adds is structurally safe: addOne
    // clicks Button--queue or nothing, so if ESPN truly offers no Queue
    // button on-turn every attempt returns on_clock (uncharged, logged —
    // turning the open question into data) and the loop breaks BEFORE any
    // removal can strip a queue it cannot refill (add-before-remove order).

    // The working list: desired minus players skipped after repeated
    // per-player failures — but a skipped player ALREADY QUEUED stays (the
    // v1.0 filter-first design removed rows the operator had added by hand).
    let working = desiredAll.slice();
    let ops = 0;
    const addResults = [];

    while (ops < MAX_OPS_PER_CYCLE) {
      if (mode !== "LIVE") break;
      q = queueEntries();
      if (q.mode === "NO_PANEL") { problems.push("queue panel disappeared mid-cycle"); break; }
      const pairing = pairRows(working, q.rows);
      logPairing(working, pairing);

      // longest correct prefix: row i is pairing to working[i]
      let ptr = 0;
      while (ptr < q.rows.length && pairing.rowPair[ptr] === ptr) ptr++;

      const converged = ptr === working.length && q.rows.length === working.length;
      if (converged) break;

      if (ptr < q.rows.length) {
        // Row at the first wrong position. If it is protected, we must not
        // remove it — refuse and report.
        const blocker = pairing.rowPair[ptr];
        if (blocker === "protected") {
          problems.push(`ambiguous queue row "${pairing.names[ptr]}" — refusing to touch it`);
          break;
        }
        // If the player who BELONGS at ptr is absent from the queue, fetch
        // him first (append) so the queue never drains to zero avoidably.
        const needed = working[ptr];
        if (needed && !pairing.usedDesired.has(ptr)) {
          if (isSkipped(needed.player_id, overall)) {
            working.splice(ptr, 1);
            continue;
          }
          ops++;
          const res = await addOne(needed);
          qlog(`add-first "${espnKey(needed)}" (belongs at ${ptr}) -> ${res.ok ? "landed" : res.reason}`);
          addResults.push({ pid: needed.player_id, ok: res.ok, reason: res.reason });
          if (!res.ok) {
            problems.push(`add "${espnKey(needed)}": ${res.reason}`);
            if (res.reason === "on_clock" || res.reason === "not_live") break;
            working.splice(ptr, 1); // §7: skip, take the next recommendation
          }
          continue;
        }
        ops++;
        const r = await removeQueueEntryByName(pairing.names[ptr]);
        qlog(`remove "${pairing.names[ptr]}" (blocking position ${ptr}) -> ${r}`);
        if (mode === "HALTED") { problems.push(haltReason); break; }
        if (r === "refused") { problems.push(`no Remove control for "${pairing.names[ptr]}"`); break; }
        if (r === "stuck") { problems.push(`"${pairing.names[ptr]}" would not leave the queue`); break; }
        continue;
      }

      // Queue is a correct proper prefix — append the next desired player.
      const d = working[ptr];
      if (!d) break;
      if (isSkipped(d.player_id, overall)) {
        working.splice(ptr, 1);
        continue;
      }
      ops++;
      const res = await addOne(d);
      qlog(`append "${espnKey(d)}" at ${ptr} -> ${res.ok ? "landed" : res.reason}`);
      addResults.push({ pid: d.player_id, ok: res.ok, reason: res.reason });
      if (!res.ok) {
        problems.push(`add "${espnKey(d)}": ${res.reason}`);
        if (res.reason === "on_clock" || res.reason === "not_live") break;
        working.splice(ptr, 1);
      }
    }
    if (ops >= MAX_OPS_PER_CYCLE) problems.push("op cap hit");
    chargeFailures(addResults, overall, chargedThisCycle);

    // Verify by RE-READING the panel. ok requires convergence on the working
    // list AND the §7 depth floor — an emptied/thin queue can never be "ok"
    // just because everything we still ATTEMPTED matched (the audit's
    // all-skipped wipe reported ok:true and reset the escalation streak).
    q = queueEntries();
    const achieved = queueNames();
    const pairing = pairRows(working, q.rows);
    let matched = 0;
    while (matched < q.rows.length && pairing.rowPair[matched] === matched) matched++;
    const skipped = desiredAll.length - working.length;
    const converged = matched === working.length && q.rows.length === working.length;
    const deepEnough = achieved.length >= Math.min(K_MIN, desiredAll.length);
    const realProblems = problems.filter((p) => !p.startsWith("(note:"));
    const okNow = converged && deepEnough && realProblems.length === 0;
    let reason = problems.join("; ");
    if (skipped) reason = (reason ? reason + "; " : "") + `${skipped} desired player(s) skipped/failed this cycle`;
    if (!okNow && !reason) reason = `queue is [${achieved.join(", ")}], wanted ${working.length} row(s)`;
    return { ok: okNow, reason: reason, achieved: achieved };
  }

  // ---- the outer loop ---------------------------------------------------
  let busy = false;
  let lastFetchAt = 0;
  let lastHistorySig = "";
  let lastCycleLogSig = "";

  async function cycle(force) {
    if (busy) return;
    const sig = historySig();
    const due = Date.now() - lastFetchAt >= POLL_MS;
    if (!force && !due && sig === lastHistorySig) return;
    busy = true;
    try {
      lastHistorySig = sig;
      let got = await api("GET", "/api/queue", null);
      lastFetchAt = Date.now();
      if (got.status !== 200 || !got.json) {
        // A non-200 is a NO-OP by contract (§6a) — the last good queue stands.
        status(got.status === 0 ? "cockpit unreachable" : `cockpit says ${got.status}`, true);
        return;
      }
      let payload = got.json;
      const cycleSig = payload.overall_pick + "#" +
        (payload.desired || []).map((d) => d.player_id).join(",");
      if (cycleSig !== lastCycleLogSig) {
        lastCycleLogSig = cycleSig;
        qlog(
          `overall ${payload.overall_pick}, queue for ${payload.queue_for_overall}` +
          ` (${payload.picks_until_operator} away)` +
          (payload.is_operator_turn ? " — YOUR PICK" : "") +
          ` · autopick ${autopickState()} · desired: ` +
          (payload.desired || []).map((d) => espnKey(d)).join(", ")
        );
      }
      if (payload.complete) {
        status("draft complete");
        return;
      }
      // A HALTED writer keeps REPORTING (the streak must keep growing so the
      // step-4 push can fire); a PAUSED one reports its pause.
      if (mode === "HALTED") {
        await reportStatus(payload.overall_pick, queueNames(), false,
          "writer HALTED: " + haltReason + " (reload the page to clear)");
        status("HALTED — " + haltReason.slice(0, 50) + " (reload to clear)");
        return;
      }
      if (mode === "PAUSED") {
        status("paused by you — click to resume");
        return;
      }

      const chargedThisCycle = new Set();
      let result = await reconcileOnce(payload, chargedThisCycle);
      if (!result.ok && mode === "LIVE") {
        // Spec §6: one retry against a FRESH read (a pick may have landed
        // mid-rebuild), then refuse and report. The retry's payload REPLACES
        // the first (audit: the report carried the stale overall).
        const again = await api("GET", "/api/queue", null);
        lastFetchAt = Date.now();
        if (again.status === 200 && again.json && !again.json.complete) {
          payload = again.json;
          result = await reconcileOnce(payload, chargedThisCycle);
        }
      }

      let reason = result.reason;
      if (document.hidden) {
        reason = (reason ? reason + "; " : "") + "(note: tab hidden — timers throttled)";
      }
      if (!result.ok || reason) {
        qlog(`verify ${result.ok ? "ok" : "NOT ok"} · queue [${result.achieved.join(", ")}]` +
          (reason ? ` · ${reason}` : ""));
      }
      await reportStatus(payload.overall_pick, result.achieved, result.ok, reason);
      const ap = autopickState();
      const apTxt = ap === "on" ? " · autopick ON" : ap === "off" ? " · AUTOPICK OFF!" : "";
      if (mode === "HALTED") {
        status("HALTED — " + haltReason.slice(0, 50) + " (reload to clear)");
      } else if (result.ok) {
        status(`ok · ${result.achieved.length} queued` +
          (payload.is_operator_turn ? " · YOUR PICK" : "") + apTxt, ap === "off");
      } else {
        status(`degraded: ${String(result.reason).slice(0, 60)}`, true);
      }
    } finally {
      busy = false;
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) cycle(true);
  });
  setInterval(() => { cycle(false); }, TICK_MS);
  cycle(true);
  status("watching");
})();
