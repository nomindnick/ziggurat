// ==UserScript==
// @name         Ziggurat draft-control probe
// @namespace    ziggurat
// @version      1.0
// @description  DIAGNOSTIC ONLY. Answers one question: can page-side code drive ESPN's draft-room controls (Queue, Draft)? Nothing runs on load; every test is operator-triggered from the badge.
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

  function describe(el) {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      cls: (el.className || "").toString().slice(0, 90),
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

  function addBtn(label, color, fn) {
    const b = document.createElement("button");
    b.textContent = label;
    b.style.cssText =
      `font:11px monospace;padding:3px 7px;border-radius:4px;cursor:pointer;` +
      `background:#1c222b;color:${color};border:1px solid #3a4553`;
    b.addEventListener("click", fn);
    row.appendChild(b);
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
