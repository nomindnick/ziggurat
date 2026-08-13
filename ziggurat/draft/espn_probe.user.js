// ==UserScript==
// @name         Ziggurat draft-control probe
// @namespace    ziggurat
// @version      1.1
// @description  DIAGNOSTIC ONLY. Can page-side code drive ESPN's draft-room controls (Queue, Draft) — and can it EDIT the queue? Nothing runs on load; every test is operator-triggered from the badge.
// @match        https://fantasy.espn.com/football/draft*
// @match        https://fantasy.espn.com/football/mockdraft*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

// WHY THIS EXISTS (2026-08-10 probe, item: draft-day automation feasibility):
// Remote browser automation could not answer the question. Two harness artifacts
// confounded every measurement: the player grid is a virtualized FixedDataTable
// that recycles nodes between tool calls (element refs went stale every time),
// and screenshots come back at 1106x1112 while the viewport is 1498x1506, so
// coordinate clicks landed off-target. A synthetic click appeared to fail — but
// the TRUSTED control leg never validly executed, so "untrusted events are
// ignored" was never separated from "my clicks missed."
//
// A userscript has neither problem: it resolves the element and clicks it in the
// same tick, in page coordinates that are by definition correct. If a synthetic
// click works, this reports WHICH level of synthesis was needed — which is most
// of the design of the real feature. If none works, the pure-userscript
// automation path is closed and we stop paying for it.

(() => {
  "use strict";

  // NOTHING auto-runs. This script @matches the REAL draft room too (same URL
  // shape), and a probe that clicked things on load would fire on 2026-08-31.
  // Every test below requires an operator click on this script's own badge.

  const log = [];
  let out = null; // assigned when the badge is built; emit() tolerates null
  const emit = (...parts) => {
    const line = parts
      .map((p) => (typeof p === "string" ? p : JSON.stringify(p)))
      .join(" ");
    log.push(line);
    console.log("[ZIG_PROBE]", line); // readable via read_console_messages
    if (out) out.textContent = log.slice(-40).join("\n");
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---- observation helpers --------------------------------------------

  // The queue may round-trip through ESPN's websocket before it renders, so
  // never judge an attempt on one immediate read. Signature over several
  // independent indicators, so the verdict doesn't hinge on guessing which
  // one ESPN actually mutates.
  function queueSig() {
    const p = document.querySelector(".pick-queue");
    if (!p) return "NO_PANEL";
    const rows = p.querySelectorAll('[class*="fixedDataTableRowLayout_main"]').length;
    return JSON.stringify([
      p.className.includes("empty"),
      rows,
      (p.textContent || "").trim().slice(0, 200),
    ]);
  }

  // Autodraft fires ONLY at 0:00. Recording the clock at fire time is what
  // separates "our click committed" from "the clock expired and ESPN picked"
  // — indistinguishable by pick history alone (measured 2026-08-12).
  function clockText() {
    for (const el of document.querySelectorAll('[class*="lock"],[class*="imer"]')) {
      const t = (el.textContent || "").trim();
      if (/^\d{1,2}:\d{2}$/.test(t)) return t;
    }
    const m = (document.body.innerText || "").match(/\b(\d{1,2}:\d{2})\b/);
    return m ? m[1] : null;
  }

  async function waitForChange(sigFn, before, ms) {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      await sleep(100);
      if (sigFn() !== before) return true;
    }
    return false;
  }

  // ---- React introspection --------------------------------------------

  function reactKeys(el) {
    return Object.keys(el).filter((k) => k.startsWith("__react"));
  }

  // React attaches props to the DOM node under a randomized key. The handler
  // is not always on the element that LOOKS clickable, so walk up a few levels.
  function findOnClick(el) {
    let node = el;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      for (const k of Object.keys(node)) {
        if (k.startsWith("__reactProps$") || k.startsWith("__reactEventHandlers$")) {
          const props = node[k];
          if (props && typeof props.onClick === "function") {
            return { handler: props.onClick, node, depth, key: k };
          }
        }
      }
    }
    return null;
  }

  // `el.className` is an SVGAnimatedString on SVG nodes, so the usual
  // `(el.className || "").toString()` yields "[object SVGAnimatedString]" —
  // and a remove control rendered as a bare <svg> icon is exactly the case
  // this probe has to be able to see.
  function clsOf(el) {
    if (!el || !el.getAttribute) return "";
    const attr = el.getAttribute("class");
    if (attr) return attr;
    const c = el.className;
    if (typeof c === "string") return c;
    return (c && c.baseVal) || "";
  }

  function describe(el) {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      cls: clsOf(el).slice(0, 90),
      role: el.getAttribute("role") || null,
      disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true",
      txt: (el.textContent || "").trim().slice(0, 40),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      visible: r.width > 0 && r.height > 0,
      reactKeys: reactKeys(el).map((k) => k.split("$")[0]),
      onClickFoundAtDepth: (findOnClick(el) || {}).depth ?? null,
    };
  }

  // ---- the escalation ladder ------------------------------------------
  // Ordered cheapest-to-most-invasive. The level that works IS the finding:
  // L1/L2 mean a 10-line feature; L4 means we're reaching into React internals
  // and the whole thing is one ESPN bundle rebuild away from breaking.

  function centerOpts(el) {
    const r = el.getBoundingClientRect();
    return {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      button: 0,
      buttons: 1,
      clientX: Math.round(r.left + r.width / 2),
      clientY: Math.round(r.top + r.height / 2),
    };
  }

  const LADDER = [
    {
      name: "L1 element.click()",
      fire: (el) => el.click(),
    },
    {
      name: "L2 dispatch MouseEvent(click)",
      fire: (el) => el.dispatchEvent(new MouseEvent("click", centerOpts(el))),
    },
    {
      name: "L3 full pointer+mouse sequence",
      fire: (el) => {
        const o = centerOpts(el);
        const up = Object.assign({}, o, { buttons: 0 });
        el.dispatchEvent(new PointerEvent("pointerover", o));
        el.dispatchEvent(new PointerEvent("pointerdown", o));
        el.dispatchEvent(new MouseEvent("mousedown", o));
        try { el.focus(); } catch (e) { /* non-focusable is fine */ }
        el.dispatchEvent(new PointerEvent("pointerup", up));
        el.dispatchEvent(new MouseEvent("mouseup", up));
        el.dispatchEvent(new MouseEvent("click", Object.assign({}, up, { detail: 1 })));
      },
    },
    {
      name: "L4 React onClick direct",
      fire: (el) => {
        const found = findOnClick(el);
        if (!found) throw new Error("no React onClick within 6 ancestors");
        found.handler({
          type: "click",
          target: el,
          currentTarget: found.node,
          nativeEvent: new MouseEvent("click", centerOpts(el)),
          bubbles: true,
          cancelable: true,
          defaultPrevented: false,
          isTrusted: false,
          preventDefault() {},
          stopPropagation() {},
          persist() {},
        });
      },
    },
  ];

  async function runLadder(label, getEl, sigFn) {
    emit(`--- ${label} ---`);
    const ua = navigator.userActivation;
    emit("userActivation at start:", {
      isActive: ua ? ua.isActive : "unsupported",
      hasBeenActive: ua ? ua.hasBeenActive : "unsupported",
    });

    // A level that never FIRED is not evidence about anything. Counted so the
    // verdict can distinguish "tried and failed" from "never had a target" —
    // measured 2026-08-12: run from the lobby, all four levels skipped for a
    // missing target and the probe still printed a confident negative verdict.
    let fired = 0;

    for (const level of LADDER) {
      // Re-resolve every time: the grid virtualizes and recycles nodes, so a
      // reference captured one level ago may be detached by now.
      const el = getEl();
      if (!el) { emit(level.name, "SKIP — target not found"); continue; }
      if (!document.contains(el)) { emit(level.name, "SKIP — target detached"); continue; }

      const before = sigFn();
      let threw = null;
      fired++;
      const clockAtFire = clockText();
      try { level.fire(el); } catch (e) { threw = String(e); }
      if (threw) { emit(level.name, "THREW", threw); continue; }

      const changed = await waitForChange(sigFn, before, 2500);
      emit(level.name, changed ? "*** WORKED ***" : "no effect (2.5s)",
           `[clock at fire ${clockAtFire || "?"} -> now ${clockText() || "?"}]`);
      if (changed) {
        emit("VERDICT: page-side code CAN drive this control, at", level.name);
        return level.name;
      }
    }
    if (fired === 0) {
      emit("VERDICT: INCONCLUSIVE — no level fired; the target was never found.");
      emit("  (are you inside a live draft room? the lobby has no draft controls)");
      return "NO_TARGET";
    }
    emit(`VERDICT: no synthesis level moved this control (${fired} of ${LADDER.length} fired).`);
    return null;
  }

  // ---- targets ---------------------------------------------------------

  function firstQueueButton() {
    const byClass = document.querySelector("button.Button--queue");
    if (byClass) return byClass;
    return [...document.querySelectorAll("button")].find(
      (b) => (b.textContent || "").trim().toLowerCase() === "queue"
    ) || null;
  }

  // The DRAFT control is NOT a <button> by text match (measured: 0 hits while
  // DRAFT was visibly rendered), so cast wider and report what it actually is.
  // STRICT: real controls only. Matching bare `div`s containing the text
  // "Draft" returned 192 hits (measured 2026-08-12) and put a
  // `Wrapper Card__Content` ancestor at index 0, so every commit went through
  // whatever ancestor handler the click happened to bubble into — which is
  // why the working level wandered L1/L2/L3 instead of being deterministic.
  // The real control is BUTTON.Button--draft.PlayerCard__action-btn, onClick
  // at depth 0.
  function draftButtonsIn(scope) {
    const hits = [];
    for (const el of scope.querySelectorAll("button, [role=button]")) {
      const t = (el.textContent || "").trim().toLowerCase();
      const cls = (el.className || "").toString().toLowerCase();
      if (t === "draft" || t === "draft player" || cls.includes("button--draft")) hits.push(el);
    }
    // Prefer the player-card action button over any other real button.
    hits.sort((a, b) => {
      const score = (e) =>
        (e.className || "").toString().includes("PlayerCard__action-btn") ? 0 : 1;
      return score(a) - score(b);
    });
    return hits;
  }
  function draftCandidates() {
    return draftButtonsIn(document);
  }

  function isDisabled(el) {
    return (
      el.disabled === true ||
      el.getAttribute("aria-disabled") === "true" ||
      (el.className || "").toString().toLowerCase().includes("disabled")
    );
  }

  // Clicking a player's NAME LINK opens a player card with its own Draft
  // button; clicking the ROW arms the header Draft button. Two different
  // commit paths, and the probe must not confuse one for the other
  // (measured 2026-08-12: the chain opened the card, then hunted for the
  // header button and stalled).
  function openModal() {
    const sel = '[role=dialog], [class*="modal"], [class*="Modal"], [class*="player-card"]';
    for (const m of document.querySelectorAll(sel)) {
      const r = m.getBoundingClientRect();
      if (r.width > 150 && r.height > 150) return m;
    }
    return null;
  }
  function headerDraftButton() {
    const m = openModal();
    return draftButtonsIn(document).find((b) => !m || !m.contains(b)) || null;
  }

  // A pick-history row count is NOT a specific detector: in a practice room of
  // instant-picking bots it increments on its own within the observation
  // window, so it reports "worked" for a click that did nothing (measured
  // 2026-08-12). The specific fact is WHICH player the newest pick names.
  function lastPick() {
    const p = document.querySelector(".pick-history");
    if (!p) return null;
    let best = null;
    for (const r of p.querySelectorAll('[class*="fixedDataTableRowLayout_main"]')) {
      const c = [...r.querySelectorAll('[class*="fixedDataTableCellLayout_main"]')]
        .map((x) => (x.textContent || "").trim());
      if (c.length < 3 || !/^\d+$/.test(c[0])) continue;
      const n = Number(c[0]);
      if (!best || n > best.overall) best = { overall: n, player: c[1], team: c[2] };
    }
    return best;
  }

  // Rows in the AVAILABLE-players grid only — the history and queue panels
  // use the same FixedDataTable classes and would otherwise match.
  function findPlayerRow(fragment) {
    const frag = fragment.trim().toLowerCase();
    if (!frag) return null;
    for (const r of document.querySelectorAll('[class*="fixedDataTableRowLayout_main"]')) {
      if (r.closest(".pick-history") || r.closest(".pick-queue")) continue;
      if ((r.textContent || "").toLowerCase().includes(frag)) return r;
    }
    return null;
  }

  // ---- badge UI --------------------------------------------------------
  // Buttons, not console calls, and deliberately NO alert/confirm/prompt:
  // a modal dialog blocks the page's event loop and wedges the automation
  // channel entirely.

  const box = document.createElement("div");
  box.style.cssText =
    "position:fixed;bottom:8px;right:8px;z-index:2147483647;font:11px monospace;" +
    "background:#12151a;color:#d8dee9;border:1px solid #313a48;border-radius:6px;" +
    "padding:8px;max-width:460px;opacity:.96";

  const title = document.createElement("div");
  title.textContent = "ZIG PROBE — diagnostic, nothing auto-runs";
  title.style.cssText = "color:#45c98b;margin-bottom:6px;font-weight:bold";
  box.appendChild(title);

  const row = document.createElement("div");
  row.style.cssText = "display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px";
  box.appendChild(row);

  out = document.createElement("pre");
  out.style.cssText =
    "margin:0;max-height:260px;overflow:auto;white-space:pre-wrap;" +
    "font:10px monospace;color:#9aa7b8";
  box.appendChild(out);

  function addBtn(label, color, fn, parent) {
    const b = document.createElement("button");
    b.textContent = label;
    b.style.cssText =
      `font:11px monospace;padding:3px 7px;border-radius:4px;cursor:pointer;` +
      `background:#1c222b;color:${color};border:1px solid #3a4553`;
    b.addEventListener("click", fn);
    (parent || row).appendChild(b);
    return b;
  }

  // Queue, inside a real user gesture: the ladder's first level fires
  // synchronously in this handler, so navigator.userActivation is live.
  addBtn("queue (in gesture)", "#45c98b", async () => {
    if (busy) { emit("REFUSED — a run is already in flight"); return; }
    busy = true;
    try { await runLadder("QUEUE, in user gesture", firstQueueButton, queueSig); }
    finally { busy = false; }
  });

  // Queue, with no user activation: waits out the ~5s activation window first.
  // If ESPN gates on activation rather than on isTrusted, only this one fails —
  // and that distinction decides whether an unattended script can work at all.
  addBtn("queue (no gesture, 6s)", "#e0c46c", async () => {
    if (busy) { emit("REFUSED — a run is already in flight"); return; }
    busy = true;
    try {
      emit("waiting 6s for user activation to expire...");
      await sleep(6000);
      await runLadder("QUEUE, no user gesture", firstQueueButton, queueSig);
    } finally { busy = false; }
  });

  addBtn("inspect DRAFT control", "#7aa2f7", () => {
    const hits = draftCandidates();
    emit(`DRAFT candidates found: ${hits.length}`);
    hits.slice(0, 8).forEach((el, i) => emit(`  [${i}]`, describe(el)));
    const q = firstQueueButton();
    emit("queue button:", q ? describe(q) : "NOT FOUND");
    emit("queue panel sig:", queueSig().slice(0, 160));
  });

  // The whole automation chain end to end: name a player, select them with a
  // synthetic click, commit with a synthetic click, and verify the NEWEST PICK
  // NAMES THAT PLAYER. That last assertion is what makes this a measurement
  // rather than a coincidence — a row-count detector cannot tell our commit
  // from the bot picking two seats over.
  // One run at a time. Overlapping ladders interleaved on 2026-08-12 and
  // produced an uninterpretable wrong-player commit; in a real system two
  // live ladders is a double-draft.
  let busy = false;

  async function fullChain(fragment) {
    if (busy) { emit("REFUSED — a run is already in flight"); return; }
    busy = true;
    try {
      await fullChainInner(fragment);
    } finally {
      busy = false;
    }
  }

  async function fullChainInner(fragment) {
    emit(`--- FULL CHAIN: "${fragment}" ---`);
    emit("last pick before:", lastPick() || "none");

    const prow = findPlayerRow(fragment);
    if (!prow) { emit(`ABORT — no available-player row matching "${fragment}"`); return; }
    emit("target row:", (prow.textContent || "").trim().slice(0, 60));

    const before = lastPick();
    const frag = fragment.trim().toLowerCase();

    // ROW-SELECT IS NOT A SELECTION. Measured 2026-08-12: clicking a plain
    // cell left ESPN's selection untouched, but the header Draft button is
    // enabled on your turn REGARDLESS of what is selected — so committing
    // through it drafted the default best-available three times running
    // (Brown, McMillan, Loveland) while the probe claimed the target failed.
    // The card button is bound to ONE player by construction, so it is the
    // only path that cannot silently draft a stranger. Card first, always.
    let getBtn = null;
    (prow.querySelector("a") || prow).click();
    await sleep(900);
    const modal = openModal();
    emit("player card modal:", modal ? (modal.className || "").toString().slice(0, 70) : "none");
    const mb = modal ? draftButtonsIn(modal)[0] : null;
    emit("card DRAFT button:", mb ? describe(mb) : "none");
    if (mb) {
      emit("ARMED via the player card — bound to this player");
      getBtn = () => {
        const m = openModal();
        return m ? draftButtonsIn(m)[0] || null : null;
      };
    }

    if (!getBtn) {
      emit("ABORT — no Draft button inside the player card.");
      emit("  NOT falling back to the header button: on your turn it is always");
      emit("  enabled and would commit whoever ESPN has selected, not the target.");
      return;
    }

    // Step 2: commit.
    const sig = () => {
      const lp = lastPick();
      return lp ? `${lp.overall}|${lp.player}` : "none";
    };
    const level = await runLadder("DRAFT COMMIT", getBtn, sig);

    await sleep(800);
    const after = lastPick();
    emit("last pick after:", after || "none");

    // Three outcomes, and conflating the last two is how a wrong-player commit
    // gets read as a harmless no-op.
    const hit = after && (after.player || "").toLowerCase().includes(frag);
    const advanced = after && before && after.overall > before.overall;
    const turnOver = !headerDraftButton();
    if (hit) {
      emit(`*** CONFIRMED: pick ${after.overall} is our target (${after.player}) — synthetic click, at ${level}`);
    } else if (advanced && turnOver) {
      emit(`*** WRONG PLAYER COMMITTED: pick ${after.overall} = "${after.player}" (${after.team}).`);
      emit(`    The click DID draft — the selection never moved to "${fragment}".`);
    } else {
      emit(`no commit attributable to us: newest "${after ? after.player : "n/a"}" (${after ? after.team : "n/a"}), still our turn: ${!turnOver}`);
    }
  }

  // THE CLEAN EXPERIMENT. Runs OFF-TURN: no clock, no autodraft, reversible,
  // and nothing else in the room puts a player in YOUR queue. So a named
  // player appearing there after a synthetic click is attributable to that
  // click and to nothing else — unlike a pick, which the operator's own hand
  // or an expiring clock can produce identically (measured 2026-08-12).
  // Run it with hands off the mouse.
  async function queueChain(fragment) {
    if (busy) { emit("REFUSED — a run is already in flight"); return; }
    busy = true;
    try {
      const frag = fragment.trim().toLowerCase();
      if (!frag) { emit("type a player surname first"); return; }
      emit(`--- QUEUE CHAIN: "${fragment}" — HANDS OFF NOW ---`);
      const panel = () => document.querySelector(".pick-queue");
      const panelText = () => {
        const p = panel();
        return p ? (p.textContent || "").toLowerCase() : "";
      };
      if (!panel()) { emit("ABORT — no .pick-queue panel (is the draft live?)"); return; }
      if (panelText().includes(frag)) { emit("ABORT — target is already in the queue"); return; }
      emit("queue before:", (panel().textContent || "").trim().slice(0, 120));

      for (const level of LADDER) {
        // Re-resolve every level: the grid virtualizes and recycles nodes.
        const prow = findPlayerRow(fragment);
        if (!prow) { emit(level.name, "SKIP — target row not rendered"); continue; }
        const qb =
          prow.querySelector("button.Button--queue") ||
          [...prow.querySelectorAll("button")].find(
            (b) => (b.textContent || "").trim().toLowerCase() === "queue"
          );
        if (!qb) { emit(level.name, "SKIP — no Queue button in that row"); continue; }

        const before = panelText();
        let threw = null;
        try { level.fire(qb); } catch (e) { threw = String(e); }
        if (threw) { emit(level.name, "THREW", threw); continue; }
        await waitForChange(panelText, before, 2500);
        if (panelText().includes(frag)) {
          emit(`*** CONFIRMED: "${fragment}" entered the queue via ${level.name}`);
          emit("    an untrusted click drove a real ESPN draft-room control.");
          emit("queue after:", (panel().textContent || "").trim().slice(0, 120));
          return;
        }
        emit(level.name, "target not in queue after 2.5s");
      }
      emit(`VERDICT: no synthesis level put "${fragment}" in the queue.`);
    } finally { busy = false; }
  }

  // ======================================================================
  // P0 — QUEUE EDIT (spec §4). The queue-first design rests entirely on
  // this. The 2026-08-12 probe proved APPEND and nothing else, and a queue
  // that can only be appended to is useless by round 3 because the board
  // re-ranks every time another team picks. Three questions:
  //
  //   Q1  can a synthetic click REMOVE a queued player?      <- load-bearing
  //   Q2  does add APPEND, or does ESPN insert by its own rank?
  //   Q3  is reorder possible at all (HTML5 DnD / mouse-drag / a control)?
  //
  // If Q1 is yes, Q3 stops mattering: clear-and-refill produces ANY order,
  // and N clicks is nothing against a 90 s pick clock. If Q1 is no, the
  // design is dead and we fall back to active card-path clicking (spec §9).
  // ======================================================================

  function queuePanel() { return document.querySelector(".pick-queue"); }

  // We do not get to ASSUME the queue's row markup. Report which selector
  // actually matched, so a future ESPN rebuild reads as a changed mode in
  // the log instead of a silent zero that looks like an empty queue.
  const QUEUE_ROW_SELECTORS = [
    '[class*="fixedDataTableRowLayout_main"]',
    "li",
    '[class*="Table__TR"]',
    "tr",
  ];
  function queueEntries() {
    const p = queuePanel();
    if (!p) return { mode: "NO_PANEL", rows: [] };
    for (const sel of QUEUE_ROW_SELECTORS) {
      const rows = [...p.querySelectorAll(sel)].filter((r) => {
        if (/header/i.test(clsOf(r))) return false;
        if (r.closest('[class*="headerLayout"]')) return false;
        return (r.textContent || "").trim().length > 3;
      });
      if (rows.length) return { mode: sel, rows };
    }
    return { mode: "NO_ROWS", rows: [] };
  }

  // The anchor carries a CLEAN player name; the row's full text carries
  // rank/position/team noise that will never match pick-history text — and
  // matching pick history is how the confounder below gets witnessed.
  function entryName(row) {
    const a = row.querySelector("a");
    const t = a ? (a.textContent || "").trim() : "";
    if (t) return t;
    return (row.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40);
  }
  function queueNames() { return queueEntries().rows.map(entryName); }

  // THE CONFOUNDER for every test below: ESPN drops a queued player from
  // YOUR queue the moment ANY team drafts him. Same observable, different
  // cause — precisely the class of mistake that cost three false CONFIRMEDs
  // on 2026-08-12. Pick History and Pick Queue are tabs in the same panel,
  // so this check is frequently UNAVAILABLE; say so out loud rather than
  // report a clean result from a measurement that never ran.
  function historyProbe() {
    const p = document.querySelector(".pick-history");
    const txt = p ? (p.textContent || "").toLowerCase() : "";
    return { available: !!p && txt.length > 20, txt };
  }

  // A control that only exists on hover has NO DOM node until React sees a
  // mouseover, so query-then-click finds nothing and reports a confident
  // false negative. Synthesize the hover, then re-query.
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
      catch (e) { /* older event ctor missing is not a finding */ }
    }
  }

  function reactPropsOf(el) {
    for (const k of Object.keys(el)) {
      if (k.startsWith("__reactProps$") || k.startsWith("__reactEventHandlers$")) return el[k];
    }
    return null;
  }

  // Which handlers React has bound tells us the reorder MECHANISM before we
  // try to drive it: onDragStart => HTML5 DnD; onMouseDown with no drag
  // handlers => a mouse-move drag library; neither => not reorderable here.
  function handlerNames(el, depth) {
    const names = new Set();
    let node = el;
    for (let d = 0; node && d <= (depth == null ? 4 : depth); d++, node = node.parentElement) {
      const p = reactPropsOf(node);
      if (!p) continue;
      for (const k of Object.keys(p)) {
        if (/^on[A-Z]/.test(k) && typeof p[k] === "function") names.add(k + "@" + d);
      }
    }
    return [...names];
  }

  // Which node is the remove control is not knowable in advance, so rank
  // every plausible candidate and let the test try them in order.
  const REMOVE_HINT = /(remove|delete|dequeue|trash|cancel|close|minus)/i;
  function removeCandidatesIn(row) {
    const hits = [];
    for (const el of row.querySelectorAll("*")) {
      const cls = clsOf(el);
      const label = [
        el.getAttribute("aria-label"),
        el.getAttribute("title"),
        el.getAttribute("data-testid"),
      ].filter(Boolean).join(" ");
      const txt = (el.textContent || "").trim();
      const clickable = el.tagName === "BUTTON" || el.getAttribute("role") === "button";
      let score = null;
      if (REMOVE_HINT.test(cls) || REMOVE_HINT.test(label)) score = clickable ? 0 : 1;
      else if (/^(×|✕|✖|x|−|-)$/i.test(txt)) score = clickable ? 0 : 2;
      else if (clickable && txt.length <= 14) score = 3;
      if (score === null) continue;
      hits.push({ el, score, cls: cls.slice(0, 60), label, txt: txt.slice(0, 20), tag: el.tagName });
    }
    hits.sort((a, b) => a.score - b.score);
    return hits;
  }

  // Set by test C when a candidate is PROVEN to remove. Test E then reuses
  // that exact signature instead of blind-clicking its own best guess — two
  // tests silently driving two different controls is how a rebuild result
  // ends up meaning nothing.
  let confirmedRemoveSig = null;

  function pickRemoveControl(row) {
    const list = removeCandidatesIn(row);
    if (confirmedRemoveSig) {
      const m = list.find(
        (c) =>
          c.cls === confirmedRemoveSig.cls &&
          c.txt === confirmedRemoveSig.txt &&
          c.tag === confirmedRemoveSig.tag
      );
      if (m) return m;
    }
    return list[0] || null;
  }

  // A candidate that turns out to be the player-card opener leaves a modal
  // covering the panel, which would fail every LATER candidate for a reason
  // that has nothing to do with removal.
  function dismissModal() {
    const m = openModal();
    if (!m) return false;
    const x = [...m.querySelectorAll("button,[role=button]")].find((b) =>
      /close|dismiss|×|✕/i.test(
        (b.getAttribute("aria-label") || "") + " " + clsOf(b) + " " + (b.textContent || "").trim()
      )
    );
    if (x) { try { x.click(); } catch (e) { /* fall through to Escape */ } }
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, which: 27, bubbles: true })
    );
    return true;
  }

  // Ladder variant that judges on an EXPLICIT predicate rather than "the
  // signature changed" — a change detector cannot tell our removal from a
  // rival drafting the same player out of our queue.
  async function runLadderUntil(label, getEl, verify, waitMs) {
    emit(`--- ${label} ---`);
    let fired = 0;
    for (const level of LADDER) {
      const el = getEl();
      if (!el) { emit(level.name, "SKIP — target not found"); continue; }
      if (!document.contains(el)) { emit(level.name, "SKIP — target detached"); continue; }
      fired++;
      let threw = null;
      try { level.fire(el); } catch (e) { threw = String(e); }
      if (threw) { emit(level.name, "THREW", threw); continue; }
      const deadline = Date.now() + (waitMs || 2500);
      while (Date.now() < deadline) {
        await sleep(100);
        const note = verify();
        if (note) { emit(level.name, "*** WORKED ***", note); return { level: level.name, note, fired }; }
      }
      emit(level.name, `no effect (${waitMs || 2500}ms)`);
    }
    if (!fired) emit("INCONCLUSIVE — no level fired; the target was never found.");
    return { level: null, note: null, fired };
  }

  // ---- shared add step (L1 only; L1 is the proven level) ---------------
  async function addOne(name) {
    const before = queueNames();
    const row = findPlayerRow(name);
    if (!row) {
      emit(`  "${name}" SKIP — no rendered available-player row (P1: the grid is virtualized)`);
      return null;
    }
    const qb =
      row.querySelector("button.Button--queue") ||
      [...row.querySelectorAll("button")].find(
        (b) => (b.textContent || "").trim().toLowerCase() === "queue"
      );
    if (!qb) {
      emit(`  "${name}" SKIP — no Queue button in that row (on the clock? it becomes Draft)`);
      return null;
    }
    qb.click();
    let after = before;
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      await sleep(100);
      after = queueNames();
      if (after.length > before.length) break;
    }
    if (after.length <= before.length) { emit(`  "${name}" — queue did not grow in 3s`); return null; }
    const index = after.findIndex((x) => x.toLowerCase().includes(name.toLowerCase()));
    emit(`  "${name}" landed at index ${index} of ${after.length}`, after);
    return { name, index, of: after.length };
  }

  // ---- test A: read the panel, click nothing ---------------------------
  async function inspectQueue() {
    emit("--- INSPECT QUEUE PANEL (read-only) ---");
    const p = queuePanel();
    if (!p) { emit("no .pick-queue in the DOM — open the Pick Queue tab in the draft room"); return; }
    emit("panel class:", clsOf(p).slice(0, 90), "| empty-flag:", clsOf(p).includes("empty"));
    const { mode, rows } = queueEntries();
    emit("row selector that matched:", mode, "| rows:", String(rows.length));
    emit("queue order:", queueNames());
    emit("elements with draggable=true:", String(p.querySelectorAll('[draggable="true"]').length));
    for (const [i, r] of rows.slice(0, 3).entries()) {
      emit(`  row[${i}] "${entryName(r)}"`);
      emit("    row handlers:", handlerNames(r, 2));
      const beforeHover = removeCandidatesIn(r).length;
      hoverRow(r);
      await sleep(250);
      const cands = removeCandidatesIn(r);
      emit(`    candidate controls: ${beforeHover} before hover, ${cands.length} after hover`);
      cands.slice(0, 5).forEach((c, j) =>
        emit(`      cand[${j}] score=${c.score}`, {
          tag: c.tag, cls: c.cls, label: c.label, txt: c.txt, handlers: handlerNames(c.el, 2),
        })
      );
    }
    const hp = historyProbe();
    emit("pick-history readable from this tab (confounder check):", String(hp.available));
  }

  // ---- test B: Q2, append or rank-insert? ------------------------------
  async function queueAddOrdered(csv) {
    const names = String(csv || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (names.length < 2) { emit('P0-B: type at least two surnames, comma-separated'); return; }
    emit(`--- P0-B QUEUE ADD, ORDERED: ${names.join(" -> ")} ---`);
    emit("Q2: does add APPEND, or does ESPN insert by its own rank?");
    if (!queuePanel()) { emit("ABORT — no .pick-queue panel"); return; }
    const landed = [];
    for (const n of names) {
      const r = await addOne(n);
      if (r) landed.push(r);
    }
    if (!landed.length) { emit("VERDICT Q2: INCONCLUSIVE — nothing was added."); return; }
    const appended = landed.every((l) => l.index === l.of - 1);
    emit("landings:", landed.map((l) => `${l.name}@${l.index}/${l.of}`));
    emit(
      appended
        ? "VERDICT Q2: APPEND — add order IS queue order, so clear-and-refill controls ordering."
        : "VERDICT Q2: NOT a plain append — ESPN placed an entry by its own rule. Ordering needs reorder."
    );
    emit("final queue:", queueNames());
  }

  // ---- test C: Q1, the load-bearing one --------------------------------
  async function queueRemove(fragment) {
    emit("--- P0-C QUEUE REMOVE (Q1 — the load-bearing test) ---");
    const { rows } = queueEntries();
    if (!rows.length) { emit("ABORT — queue is empty; run the ADD test first"); return; }
    const names = rows.map(entryName);
    // The P0 input is a comma-separated list shared with the add/rebuild
    // tests; removal takes the FIRST name, or row 0 when the box is empty.
    const frag = String(fragment || "").split(",")[0].trim().toLowerCase();
    let idx = 0;
    if (frag) {
      idx = names.findIndex((n) => n.toLowerCase().includes(frag));
      if (idx < 0) { emit(`ABORT — "${fragment}" is not in the queue:`, names); return; }
    }
    const target = names[idx];
    const others = names.filter((_, i) => i !== idx);
    emit("queue before:", names);
    emit(`target: index ${idx} "${target}" — these must SURVIVE:`, others);

    const hp0 = historyProbe();
    if (hp0.available && hp0.txt.includes(target.toLowerCase())) {
      emit("ABORT — target already appears in pick history; he was drafted, not removable.");
      return;
    }
    if (!hp0.available) {
      emit("NOTE: pick-history is not readable from this tab, so the");
      emit("  drafted-by-a-rival confounder cannot be witnessed directly.");
      emit("  Prefer a target nobody in the room would take.");
    }

    // The verdict is a CONJUNCTION: the target left AND everything else
    // stayed. A rival's pick removes exactly one name too — but it does not
    // arrive within 2 s of our click on that specific row.
    const verify = () => {
      const now = queueNames();
      if (now.some((n) => n.toLowerCase() === target.toLowerCase())) return null;
      const kept = others.every((o) => now.some((n) => n.toLowerCase() === o.toLowerCase()));
      return `queue is now ${JSON.stringify(now)} | other entries intact: ${kept}`;
    };

    const rowAt = () => queueEntries().rows[idx] || null;
    const r0 = rowAt();
    if (r0) { hoverRow(r0); await sleep(250); }
    const cands = r0 ? removeCandidatesIn(r0) : [];
    emit(`remove candidates in that row: ${cands.length}`);
    cands.slice(0, 6).forEach((c, j) =>
      emit(`  cand[${j}] score=${c.score}`, { tag: c.tag, cls: c.cls, label: c.label, txt: c.txt })
    );
    if (!cands.length) {
      emit("VERDICT Q1: no candidate control in that row. Run 'inspect QUEUE' and send the markup.");
      return;
    }

    for (let j = 0; j < Math.min(cands.length, 4); j++) {
      const sig = { cls: cands[j].cls, txt: cands[j].txt, tag: cands[j].tag };
      // Re-resolve per level BY SIGNATURE: the panel re-renders and the
      // captured node goes stale (this is what broke remote automation).
      const getEl = () => {
        const r = rowAt();
        if (!r) return null;
        hoverRow(r);
        const list = removeCandidatesIn(r);
        const match =
          list.find((c) => c.cls === sig.cls && c.txt === sig.txt && c.tag === sig.tag) || list[j];
        return match ? match.el : null;
      };
      const res = await runLadderUntil(
        `remove cand[${j}] ${sig.tag}.${sig.cls.slice(0, 30)}`, getEl, verify, 2000
      );
      if (res.level) {
        confirmedRemoveSig = sig;
        emit(`*** VERDICT Q1: REMOVE WORKS — "${target}" left the queue via ${res.level}, cand[${j}].`);
        emit("    selector hint:", sig);
        emit("    => clear-and-refill is viable; the queue-first design lives.");
        return;
      }
      if (dismissModal()) { emit(`  (cand[${j}] opened a modal — dismissed before the next candidate)`); await sleep(400); }
    }
    emit("VERDICT Q1: no candidate removed the target.");
    emit("  Queue-first is in doubt — see spec §9 kill criteria (fall back to card-path clicking).");
  }

  // ---- test D: Q3, reorder ---------------------------------------------
  async function queueReorder() {
    emit("--- P0-D QUEUE REORDER (Q3) ---");
    const { rows } = queueEntries();
    if (rows.length < 2) { emit("ABORT — need at least 2 queued players; run ADD first"); return; }
    const before = rows.map(entryName);
    emit("queue before:", before);
    emit("target order (swap the top two):", [before[1], before[0], ...before.slice(2)]);

    const src = () => queueEntries().rows[1] || null;
    const dst = () => queueEntries().rows[0] || null;
    const s0 = src();
    if (!s0) { emit("ABORT — row[1] vanished between reads"); return; }
    emit("row[1] handlers:", handlerNames(s0, 3));
    emit("row[1] draggable attr:", String(s0.getAttribute("draggable")),
         "| draggable descendants:", String(s0.querySelectorAll('[draggable="true"]').length));

    // Same-set-different-order is the assertion. A drop or an add is not a
    // reorder, and ESPN removing a drafted player would otherwise read as one.
    const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));
    const verify = () => {
      const now = queueNames();
      if (!sameSet(now, before)) return null;
      if (now.join("|") === before.join("|")) return null;
      return `order is now ${JSON.stringify(now)}`;
    };

    const at = (x, y, buttons) => ({
      bubbles: true, cancelable: true, composed: true, view: window,
      button: 0, buttons, clientX: Math.round(x), clientY: Math.round(y),
    });

    async function html5Drag() {
      const s = src(), d = dst();
      if (!s || !d) return false;
      let dt;
      try { dt = new DataTransfer(); } catch (e) { emit("  DataTransfer unavailable:", String(e)); return false; }
      const so = centerOpts(s), dof = centerOpts(d);
      const fire = (type, el, o) =>
        el.dispatchEvent(new DragEvent(type, Object.assign({ dataTransfer: dt }, o)));
      fire("dragstart", s, so);
      await sleep(120);
      fire("dragenter", d, dof);
      fire("dragover", d, dof);
      await sleep(120);
      fire("drop", d, dof);
      fire("dragend", s, so);
      return true;
    }

    // Drag libraries listen on document/window, not on the row, so the moves
    // go to the document even though the press and release go to the rows.
    async function mouseDrag() {
      const s = src(), d = dst();
      if (!s || !d) return false;
      const a = centerOpts(s), b = centerOpts(d);
      s.dispatchEvent(new PointerEvent("pointerdown", at(a.clientX, a.clientY, 1)));
      s.dispatchEvent(new MouseEvent("mousedown", at(a.clientX, a.clientY, 1)));
      for (let i = 1; i <= 10; i++) {
        const x = a.clientX + ((b.clientX - a.clientX) * i) / 10;
        const y = a.clientY + ((b.clientY - a.clientY) * i) / 10;
        document.dispatchEvent(new PointerEvent("pointermove", at(x, y, 1)));
        document.dispatchEvent(new MouseEvent("mousemove", at(x, y, 1)));
        await sleep(40);
      }
      document.dispatchEvent(new PointerEvent("pointerup", at(b.clientX, b.clientY, 0)));
      document.dispatchEvent(new MouseEvent("mouseup", at(b.clientX, b.clientY, 0)));
      return true;
    }

    for (const [name, fn] of [["HTML5 drag-and-drop", html5Drag], ["mouse-move drag", mouseDrag]]) {
      emit(`trying ${name}...`);
      if (!(await fn())) { emit(`  ${name}: could not fire`); continue; }
      const deadline = Date.now() + 2500;
      let note = null;
      while (Date.now() < deadline) { await sleep(100); note = verify(); if (note) break; }
      if (note) { emit(`*** VERDICT Q3: REORDER WORKS via ${name} —`, note); return; }
      emit(`  ${name}: order unchanged`);
    }
    emit("VERDICT Q3: no reorder mechanism responded.");
    emit("  Only fatal if Q1 ALSO failed — clear-and-refill needs no reorder at all.");
  }

  // ---- test E: the actual production operation -------------------------
  async function queueRebuild(csv) {
    const want = String(csv || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (!want.length) { emit("P0-E: type the desired queue, comma-separated"); return; }
    emit(`--- P0-E QUEUE REBUILD (the production operation): ${want.join(" -> ")} ---`);
    const t0 = Date.now();
    let clicks = 0;

    for (let guard = 0; guard < 20; guard++) {
      const rows = queueEntries().rows;
      if (!rows.length) break;
      const n = rows.length;
      hoverRow(rows[0]);
      await sleep(150);
      const ctl = pickRemoveControl(queueEntries().rows[0] || rows[0]);
      if (!ctl) { emit("STOP — no remove control on row 0"); break; }
      if (guard === 0) emit("  removing via:", { tag: ctl.tag, cls: ctl.cls, txt: ctl.txt, proven: !!confirmedRemoveSig });
      try { ctl.el.click(); } catch (e) { emit("STOP — remove click threw:", String(e)); break; }
      clicks++;
      const deadline = Date.now() + 2500;
      while (Date.now() < deadline) { await sleep(100); if (queueEntries().rows.length < n) break; }
      if (queueEntries().rows.length >= n) { emit("STOP — removal did not take"); break; }
    }
    emit(`after clear: ${JSON.stringify(queueNames())} (${clicks} clicks, ${Date.now() - t0}ms)`);

    for (const n of want) { if (await addOne(n)) clicks++; }

    const got = queueNames();
    const ok =
      got.length === want.length &&
      want.every((w, i) => (got[i] || "").toLowerCase().includes(w.toLowerCase()));
    emit("final queue:", got);
    emit(
      ok
        ? `*** REBUILD OK — desired order achieved: ${clicks} clicks, ${Date.now() - t0}ms (budget: 90s/pick)`
        : `*** REBUILD MISMATCH — wanted ${JSON.stringify(want)}, got ${JSON.stringify(got)}`
    );
  }

  // ---- test F: P1, drive the search box (virtualized-grid access) ------
  // Cheap to answer while we are already in a live room, and the queue
  // writer cannot queue a player whose row was never rendered.
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

  // React tracks the previous value on the node, so a plain `el.value = x`
  // is swallowed as a no-op change. Go through the prototype's native setter.
  function setNativeValue(el, value) {
    const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function searchProbe(name) {
    const q = String(name || "").split(",")[0].trim();
    if (!q) { emit("P1: type a surname first"); return; }
    emit(`--- P1 SEARCH: drive the player filter to "${q}" ---`);
    const ins = searchInputs();
    emit(`visible text inputs: ${ins.length}`);
    ins.slice(0, 4).forEach((el, i) =>
      emit(`  [${i}]`, { ph: el.placeholder || null, cls: clsOf(el).slice(0, 50), aria: el.getAttribute("aria-label") })
    );
    if (!ins.length) { emit("P1 FAILED — no search input found"); return; }
    const el = ins[0];
    emit("target before search:", findPlayerRow(q) ? "already rendered" : "not rendered");
    setNativeValue(el, q);
    await sleep(1500);
    const row = findPlayerRow(q);
    emit(
      row
        ? `*** P1 OK — "${q}" rendered after driving the search box: ${(row.textContent || "").trim().slice(0, 60)}`
        : `P1 FAILED — "${q}" still not in the grid after 1.5s`
    );
    // A left-over filter would starve every later test of rows and read as a
    // failure that has nothing to do with what was being tested.
    setNativeValue(el, "");
    emit("(search box cleared)");
  }

  // Every P0 test shares the one-run-at-a-time mutex: two live ladders on
  // the same panel interleave into an uninterpretable result.
  function guarded(fn) {
    return async (...args) => {
      if (busy) { emit("REFUSED — a run is already in flight"); return; }
      busy = true;
      try { await fn(...args); } catch (e) { emit("TEST THREW:", String(e)); }
      finally { busy = false; }
    };
  }

  const nameInput = document.createElement("input");
  nameInput.placeholder = "player surname";
  nameInput.style.cssText =
    "font:11px monospace;padding:3px 5px;border-radius:4px;width:120px;" +
    "background:#0d1116;color:#d8dee9;border:1px solid #3a4553";
  row.appendChild(nameInput);

  // No arming: queueing is reversible and costs nothing.
  addBtn("QUEUE chain (named)", "#45c98b", () => queueChain(nameInput.value));

  // Two-click arm. This COMMITS A PICK, so it must be impossible to fire by
  // accident on 2026-08-31. Practice drafts only (operator authorization);
  // the league id is printed so the room is confirmed before the second click.
  let armed = false;
  let armTimer = null;
  const draftBtn = addBtn("arm CHAIN test", "#e06c6c", () => {
    if (!armed) {
      const frag = nameInput.value.trim();
      if (!frag) { emit("type a player surname first — nothing armed"); return; }
      armed = true;
      draftBtn.textContent = `CONFIRM — drafts "${frag}" (10s)`;
      draftBtn.style.background = "#3a1c1c";
      const league = new URLSearchParams(location.search).get("leagueId") || "?";
      emit(`ARMED. leagueId=${league} — confirm this is a PRACTICE draft.`);
      armTimer = setTimeout(() => {
        armed = false;
        draftBtn.textContent = "arm CHAIN test";
        draftBtn.style.background = "#1c222b";
        emit("disarmed (timeout)");
      }, 10000);
      return;
    }
    clearTimeout(armTimer);
    armed = false;
    draftBtn.textContent = "arm CHAIN test";
    draftBtn.style.background = "#1c222b";
    fullChain(nameInput.value);
  });

  // ---- P0 row: queue editing. Its own row, deliberately away from the
  // pick-COMMITTING button above — nothing here can spend a pick.
  const p0label = document.createElement("div");
  p0label.textContent = "P0 queue-edit — run OFF-TURN, hands off the mouse:";
  p0label.style.cssText = "color:#e0c46c;margin:8px 0 4px;border-top:1px solid #2a323d;padding-top:6px";
  box.insertBefore(p0label, out);

  const row2 = document.createElement("div");
  row2.style.cssText = "display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px";
  box.insertBefore(row2, out);

  const listInput = document.createElement("input");
  listInput.placeholder = "surnames, comma-sep";
  listInput.style.cssText =
    "font:11px monospace;padding:3px 5px;border-radius:4px;width:170px;" +
    "background:#0d1116;color:#d8dee9;border:1px solid #3a4553";
  row2.appendChild(listInput);

  addBtn("inspect QUEUE", "#7aa2f7", guarded(() => inspectQueue()), row2);
  addBtn("B: add ordered", "#45c98b", guarded(() => queueAddOrdered(listInput.value)), row2);
  addBtn("C: REMOVE", "#e0c46c", guarded(() => queueRemove(listInput.value)), row2);
  addBtn("D: reorder", "#e0c46c", guarded(() => queueReorder()), row2);
  addBtn("E: rebuild", "#45c98b", guarded(() => queueRebuild(listInput.value)), row2);
  addBtn("P1: search", "#7aa2f7", guarded(() => searchProbe(listInput.value)), row2);

  addBtn("copy log", "#9aa7b8", () => {
    navigator.clipboard.writeText(log.join("\n")).then(
      () => emit("(log copied)"),
      () => emit("(clipboard blocked — select the text instead)")
    );
  });

  document.documentElement.appendChild(box);
  emit("loaded. Nothing has run. Click a test above.");
  emit("page:", { url: location.pathname, league: new URLSearchParams(location.search).get("leagueId") || "?" });
})();
