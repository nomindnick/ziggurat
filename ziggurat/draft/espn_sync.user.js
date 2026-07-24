// ==UserScript==
// @name         Ziggurat draft sync
// @namespace    ziggurat
// @version      1.1
// @description  Mirror ESPN draft-room picks (Pick History panel) into the local Ziggurat cockpit. Read-only on the ESPN page; picks flow one way, to 127.0.0.1.
// @match        https://fantasy.espn.com/football/draft*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

(() => {
  "use strict";
  const COCKPIT = "http://127.0.0.1:{{PORT}}";
  const TOKEN = "{{TOKEN}}";
  const POLL_MS = 1200;
  // A pick the cockpit repeatedly refuses to accept OR park (e.g. it parses as
  // malformed server-side) is abandoned after this many sends and flagged —
  // an infinite silent retry under a green badge is a lie (audit finding 20).
  const MAX_SENDS_PER_PICK = 40;

  // The draft room this tab is showing — the cockpit binds to the FIRST room
  // that feeds it and rejects others (a practice tab left open must never
  // contaminate the live session — audit finding 18).
  const LEAGUE = new URLSearchParams(location.search).get("leagueId") || "";

  // ---- status badge (bottom-right) ------------------------------------
  const badge = document.createElement("div");
  badge.style.cssText =
    "position:fixed;bottom:8px;right:8px;z-index:99999;font:12px monospace;" +
    "padding:4px 10px;border-radius:6px;background:#12151a;color:#45c98b;" +
    "border:1px solid #313a48;pointer-events:none;opacity:.92";
  badge.textContent = "zig sync: starting";
  document.documentElement.appendChild(badge);
  const status = (msg, bad) => {
    badge.textContent = "zig sync: " + msg;
    badge.style.color = bad ? "#e06c6c" : "#45c98b";
  };

  // ---- state -----------------------------------------------------------
  // `sent` holds overalls the CURRENT cockpit epoch accepted. When the epoch
  // changes (cockpit restarted), everything is resent and the cockpit's
  // verify path dedupes — acceptance never needs to be durable (audit 7/17).
  let sent = new Set();
  let epoch = null;
  const sendCounts = new Map();
  const abandoned = new Set();
  let lastBlocked = null;
  let lastOk = null;
  let inFlight = false;

  // ---- harvest the Pick History panel ---------------------------------
  // Rows exist only while the Pick History tab is ACTIVE; on re-activation
  // ALL rows re-render, so dedupe-by-overall makes tab-flips lossless
  // (recon 2026-07-24). Cells are virtualized FixedDataTable divs.
  function harvest() {
    const panel = document.querySelector(".pick-history");
    if (!panel) return { visible: false, picks: [] };
    const picks = [];
    for (const row of panel.querySelectorAll('[class*="fixedDataTableRowLayout_main"]')) {
      const cells = [...row.querySelectorAll('[class*="fixedDataTableCellLayout_main"]')];
      if (cells.length < 3) continue;
      const numTxt = (cells[0].textContent || "").trim();
      if (!/^\d+$/.test(numTxt)) continue;
      const overall = Number(numTxt);
      if (sent.has(overall) || abandoned.has(overall)) continue;
      const playerCell = cells[1];
      const anchor = playerCell.querySelector("a");
      picks.push({
        overall,
        player: (playerCell.textContent || "").trim(),
        player_clean: anchor ? (anchor.textContent || "").trim() : "",
        href: anchor ? (anchor.getAttribute("href") || "") : "",
        fantasy_team: (cells[2].textContent || "").trim(),
      });
    }
    return { visible: true, picks };
  }

  function idleStatus(visible) {
    if (abandoned.size) {
      status(`pick(s) ${[...abandoned].join(",")} unreadable — enter manually`, true);
    } else if (lastBlocked !== null) {
      status(`pick ${lastBlocked} needs you in the cockpit`, true);
    } else if (lastOk !== null) {
      status(visible ? `ok · ${lastOk} picks` : `ok · ${lastOk} picks (history tab hidden)`);
    } else if (!visible) {
      status("open the Pick History tab", true);
    }
  }

  // ---- push to the cockpit --------------------------------------------
  function tick() {
    if (inFlight) return;
    const { visible, picks } = harvest();
    if (!picks.length) {
      idleStatus(visible);
      return;
    }
    inFlight = true;
    try {
      GM_xmlhttpRequest({
        method: "POST",
        url: COCKPIT + "/api/sync",
        headers: {
          "Content-Type": "application/json",
          "X-Zig-Sync-Token": TOKEN,
        },
        data: JSON.stringify({ league: LEAGUE, picks }),
        timeout: 8000,
        onload: (resp) => {
          inFlight = false;
          try {
            const j = JSON.parse(resp.responseText || "{}");
            if (resp.status !== 200) {
              status("cockpit says: " + (j.error || resp.status), true);
              return;
            }
            if (epoch !== null && j.epoch && j.epoch !== epoch) {
              // Cockpit restarted: forget everything and re-send from row 1.
              sent = new Set();
              sendCounts.clear();
              abandoned.clear();
            }
            epoch = j.epoch || epoch;
            const acc = new Set(j.accepted || []);
            for (const ov of acc) sent.add(ov);
            lastBlocked = j.blocked ? j.blocked.overall : null;
            // A pick the cockpit ANSWERED about but neither accepted nor
            // blocked (server-side malformed drop) burns a retry; abandon at
            // the cap so it can't loop forever under a green badge. Network
            // failures never count — the cockpit being down isn't the pick's
            // fault.
            for (const p of picks) {
              if (acc.has(p.overall) || p.overall === lastBlocked) {
                sendCounts.delete(p.overall);
                continue;
              }
              const n = (sendCounts.get(p.overall) || 0) + 1;
              if (n >= MAX_SENDS_PER_PICK) abandoned.add(p.overall);
              else sendCounts.set(p.overall, n);
            }
            lastOk = j.session_overall ? j.session_overall - 1 : lastOk;
            idleStatus(true);
          } catch (e) {
            status("bad cockpit response", true);
          }
        },
        onerror: () => { inFlight = false; status("cockpit unreachable", true); },
        ontimeout: () => { inFlight = false; status("cockpit timeout", true); },
      });
    } catch (e) {
      // A synchronous throw (manager quirk) must not wedge inFlight forever
      // under a stale green badge (audit note 21).
      inFlight = false;
      status("sync error: " + e, true);
    }
  }

  setInterval(tick, POLL_MS);
  status("watching");
})();
