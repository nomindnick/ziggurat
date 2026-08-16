"""Checkpoint 2 — the web draft cockpit: a local live-search view over the
headless session.

DELETABLE package (Rule 8). Rehearsal 2 (an ESPN mock lobby, 2026-07-24) showed
the scroll-on-enter REPL cannot keep up with BURST pick entry: seven CPU picks
landed in seconds and each one cost a full type→Enter→confirm round trip. This
module is the 2.4-anticipated "framework flip" — the same headless
:class:`~ziggurat.draft.session.DraftSession` (state, scoring, survival,
journaling), the same :class:`~ziggurat.draft.resolver.NameResolver` matching,
the same :class:`~ziggurat.draft.posture.PostureMonitor` cadence — rendered as a
single local web page with per-keystroke autocomplete (``resolver.suggest``)
instead of a blocking ``input()`` loop.

Like ``app.py``, this module holds NO draft state and does NO football math: it
translates HTTP requests into session/resolver/posture calls and serializes the
returned objects. ``PickRec.reasons`` render VERBATIM in the page (Rule 6).
Mirrored wiring contracts (kept in lockstep with ``app.py``):

  * after every confirmed pick/undo/edit the controller recomputes
    synchronously — the recommendation is already computed when the clock
    reaches the operator;
  * the posture monitor advances ONLY at the operator-turn cadence, and a fired
    tip is HELD until the operator accepts (clean reset) or dismisses
    (cooldown) it;
  * autodraft proposes a RIVAL's pick and REFUSES on the operator's own turn;
  * a pick is committed by explicit ``player_id`` (the operator clicked/chose a
    VISIBLE name — the confirm step the resolver's Enter-flow panel exists to
    provide happens on screen instead).

SECURITY/SCOPE: binds 127.0.0.1 ONLY — the board and journal are league-private
(Rule 5); this is a single-operator local cockpit, not a service. The tiny HTML
shell lives in the sibling ``webui.html`` (read once at startup).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.posture import PostureMonitor
from ziggurat.draft.resolver import NameResolver, normalize_query
from ziggurat.draft.session import DraftSession
from ziggurat.draft.sync import ParsedPick, parse_payload_pick, resolve_synced_pick

_UI_PATH = Path(__file__).with_name("webui.html")
_USERSCRIPT_PATH = Path(__file__).with_name("espn_sync.user.js")
_QUEUE_USERSCRIPT_PATH = Path(__file__).with_name("espn_queue.user.js")

# Sync bookkeeping caps: pending picks are bounded by draft size; conflict
# messages are display-only and bounded so a pathological feed can't grow RAM.
_MAX_CONFLICTS_SHOWN = 12

# The web pick flow commits an explicit player_id the operator SAW and chose, so
# suggestion count only needs to cover honest ambiguity, not typo rescue depth.
_SUGGEST_LIMIT_DEFAULT = 8
_SUGGEST_LIMIT_MAX = 25

# Desired-queue depth served to the ESPN queue writer (auto-entry spec §6:
# K ≈ 5-8). The floor is the writer's own escalation threshold (spec §7
# K_min = 3): serving fewer valid targets than the depth the writer is required
# to maintain would manufacture escalations, so a smaller ?k= is clamped UP.
_QUEUE_K_DEFAULT = 8
_QUEUE_K_MIN = 3
_QUEUE_K_MAX = 10
# Writer status reports kept for post-run analysis (~16 rounds of a fast mock
# produced 36 reports; 200 covers a slow human draft with margin, bounded RAM).
_QUEUE_REPORT_HISTORY = 200


class CockpitError(ValueError):
    """A user-correctable request error (rendered as a 400 with a message)."""


def _load_or_create_sync_token(directory: Path) -> str:
    """Per-installation shared secret for the userscript feed (0600, gitignored
    dir). Stable across cockpit restarts so the installed userscript keeps
    working; delete the file to rotate."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sync-token.txt"
    if path.exists():
        os.chmod(path, 0o600)  # re-harden a pre-existing file (audit)
        token = path.read_text(encoding="utf-8").strip()
        # Only accept the token_urlsafe alphabet: the value is substituted
        # into a JS string literal and an HTTP header (audit note 22).
        if token and re.fullmatch(r"[A-Za-z0-9_-]+", token):
            return token
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def _entry_json(e: BoardEntry) -> dict[str, Any]:
    return {
        "player_id": e.player_id,
        "name": e.name,
        "position": e.position,
        "team": e.team,
        "espn_rank": e.espn_overall_rank,
        "vor": round(float(e.vor), 1),
        "points": round(float(e.house_points), 1),
    }


def _rec_json(rec: Any) -> dict[str, Any]:
    return {
        **_entry_json(rec.player),
        "pick_score": round(float(rec.pick_score), 1),
        "survival_next": round(float(rec.survival_next), 2),
        "vona": round(float(rec.vona), 1),
        "reasons": list(rec.reasons),  # VERBATIM (Rule 6)
        "alternatives": [{"name": n, "why_not": w} for n, w in rec.alternatives],
    }


@dataclass
class WebCockpit:
    """The headless-session adapter the HTTP handler calls under one lock.

    Owns the derived caches (recommendations, recalibration line, held posture
    tip) exactly the way the REPL loop owns its locals; every public method is
    self-contained so the handler stays a dumb router.
    """

    session: DraftSession
    resolver: NameResolver = field(init=False)
    # Same tuning as app.launch — the two front-ends must nudge identically.
    posture: PostureMonitor = field(
        default_factory=lambda: PostureMonitor(margin=8.0, consecutive=2, cooldown=3)
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _recs: tuple[Any, ...] = field(default=(), init=False)
    _recal: Any = field(default=None, init=False)
    _advice: Any = field(default=None, init=False)
    # DOM-sync state (userscript feed): out-of-order picks wait in _sync_pending;
    # a pick the resolver refuses to auto-commit blocks the head until the
    # operator enters it manually (the userscript keeps retrying it, and the
    # verify path then dedupes it away — self-healing by design).
    _sync_pending: dict[int, ParsedPick] = field(default_factory=dict, init=False)
    _sync_blocked: tuple[ParsedPick, str] | None = field(default=None, init=False)
    # overall -> (display message, the ESPN-side ParsedPick) so the UI can
    # offer a one-click "use ESPN's pick" repair (rehearsal finding).
    _sync_conflicts: dict[int, tuple[str, ParsedPick]] = field(
        default_factory=dict, init=False
    )
    _sync_received: int = field(default=0, init=False)
    # Epoch: a fresh random id per cockpit RUN. The userscript clears its
    # sent-set whenever the epoch changes, so acceptance never has to be
    # durable — a crash/restart just triggers a full re-send that the verify
    # path dedupes (audit findings 7/17: the RAM-only pending dict plus a
    # stale in-page sent-set otherwise deadlocks sync under a green badge).
    sync_epoch: str = field(default="", init=False)
    # League binding: the first sync batch of an epoch claims the session for
    # its draft room; batches from a DIFFERENT room (e.g. a practice draft
    # left open in another tab) are rejected (audit finding 18).
    _sync_league: str | None = field(default=None, init=False)
    sync_token: str = field(default="", init=False)
    # player_id -> the display name ESPN's own draft-room DOM uses ("Texans
    # D/ST", "Hollywood Brown") — the vocabulary the queue writer must search
    # and verify with (spec §5c: an identifier the other side actually uses).
    # Built at the load_board DB seam (simulator.espn_display_names); empty
    # when the caller has no DB (tests), in which case rows serve null.
    espn_names: dict[str, str] = field(default_factory=dict)
    # Desired-queue cache (GET /api/queue): keyed on the EXACT pick sequence,
    # so an edit (same length, different player) can never serve stale — the
    # length-keyed shortcut is precisely the bug this key shape forbids. Always
    # computed at the K_MAX depth and sliced per request (`top` only slices the
    # engine's scored list), so a varying ?k= cannot bust the cache and force
    # rollouts under the lock (audit: cross-origin GETs are unauthenticated).
    # One rollout batch (~250 ms) per state change; polls between picks are free.
    _queue_cache: tuple[tuple[str, ...], tuple[Any, ...]] | None = field(
        default=None, init=False
    )
    # Queue-writer status reports (POST /api/queue/status): the LAST report plus
    # a consecutive-failure streak. Step 2 records and displays; the push
    # decision (spec §9 step 4) reads the streak — it is never made here.
    _queue_last_report: dict[str, Any] | None = field(default=None, init=False)
    _queue_bad_streak: int = field(default=0, init=False)
    _queue_reports_received: int = field(default=0, init=False)
    # Bounded history of writer reports (newest last). The first live test
    # lost its whole arc to Chrome's console retention — only the final report
    # survived server-side. GET /api/queue/reports serves this so a post-run
    # analysis never depends on the browser again.
    _queue_reports: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.resolver = NameResolver(self.session.board)
        self.sync_token = _load_or_create_sync_token(self.session.journal_path.parent)
        self.sync_epoch = secrets.token_hex(8)
        with self._lock:
            self._recompute()

    # -- derived-state upkeep (mirrors app._recompute) ----------------------

    def _recompute(self) -> None:
        """Refresh recs + recal after a state change; advance posture only at
        the operator-turn cadence; hold a fired tip until accept/dismiss."""
        try:
            self._recal = self.session.recalibration()
        except Exception:
            self._recal = None
        # recommend()/contingencies are operator-turn-only by session contract
        # (they raise off-turn); the panel is empty while a rival is on the clock,
        # exactly like the REPL's on-the-clock panel.
        if self.session.complete or not self.session.is_operator_turn:
            self._recs = ()
            return
        try:
            self._recs = tuple(self.session.recommend())
        except Exception:
            self._recs = ()
        try:
            fired = self.posture.evaluate(self.session)
        except Exception:
            fired = None
        if fired is not None:
            self._advice = fired

    # -- reads ---------------------------------------------------------------

    def board_json(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_entry_json(e) for e in self.session.board]

    def suggest_json(self, query: str, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), _SUGGEST_LIMIT_MAX))
        with self._lock:
            got = self.resolver.suggest(query, taken=self.session.taken, limit=limit)
            return [_entry_json(e) for e in got]

    def state_json(self) -> dict[str, Any]:
        with self._lock:
            s = self.session
            try:
                branches = s.contingencies()
            except Exception:
                branches = ()
            return {
                "overall_pick": s.overall_pick,
                "round": s.round,
                "rounds_total": s.rounds_total,
                "current_seat": s.current_seat,
                "operator_slot": s.operator_slot,
                "is_operator_turn": s.is_operator_turn,
                "complete": s.complete,
                "journal": str(s.journal_path),
                "taken": sorted(s.taken),
                "own_roster": [_entry_json(e) for e in s.own_roster],
                "picks": [
                    {"overall": p.overall, "seat": p.seat,
                     "player_id": p.player_id, "name": p.name}
                    for p in s.picks
                ],
                "recs": [] if s.complete else [_rec_json(r) for r in self._recs],
                "recal": self._recal.message if self._recal is not None else None,
                "posture": self._advice.message if self._advice is not None else None,
                "contingencies": [b.message for b in branches],
                "sync": self._sync_state(),
                "queue": {
                    "last_report": self._queue_last_report,
                    "bad_streak": self._queue_bad_streak,
                    "received": self._queue_reports_received,
                },
            }

    def queue_json(self, k: int) -> dict[str, Any]:
        """The desired ESPN Pick Queue (auto-entry spec §6): top-``k`` engine
        recommendations for the operator's NEXT pick, computable at any point
        in the draft.

        Writer-safety contract (the queue writer depends on this, spec §7):

        * ``desired`` is empty ONLY when ``queue_for_overall`` is null (the
          draft is complete, or every remaining pick is a rival's). An engine
          failure RAISES — the handler turns it into a 500 — so the writer can
          treat ``desired: []`` as "the queue no longer matters", never as
          "clear the queue" on a lie.
        * Ordering is deterministic per state (``session_seed ^ overall`` rng):
          the response only changes when a pick lands, so the writer's
          diff-then-rebuild loop cannot oscillate between polls.
        * On the operator's turn ``desired`` extends the exact list shown in
          the cockpit's recommendation panel — the committed pick equalling
          ``desired[0]`` at expiry is acceptance test §8.5.
        * ``k`` is a CAP, not a promise: depth is bounded by the engine's own
          candidate window (D1, ``candidate_width``), so ``desired`` may run
          shorter than ``k``. Deliberate — widening the window only for the
          queue could re-rank the head, and then ESPN's autopick would diverge
          from the on-clock panel. The queue uses the same engine or none.
        """
        k = max(_QUEUE_K_MIN, min(int(k), _QUEUE_K_MAX))
        with self._lock:
            s = self.session
            target = s.next_operator_overall()
            base = {
                "epoch": self.sync_epoch,
                "overall_pick": s.overall_pick,
                "complete": s.complete,
                "is_operator_turn": s.is_operator_turn,
                "queue_for_overall": target,
                "k": k,
            }
            if target is None:
                return {**base, "picks_until_operator": None,
                        "caveats": [], "desired": []}
            key = tuple(p.player_id for p in s.picks)
            if self._queue_cache is not None and self._queue_cache[0] == key:
                recs = self._queue_cache[1]
            else:
                recs = tuple(s.recommend_upcoming(top=_QUEUE_K_MAX))
                self._queue_cache = (key, recs)
            until = target - s.overall_pick
            # Rule 6 honesty: off-turn, each row's survival figure (and its
            # verbatim reason string) is the ON-CLOCK vantage at `target` — it
            # prices lasting BEYOND that pick, not lasting TO it. At a wheel
            # target it reads "100% — no rush" while a whole round of rival
            # picks intervenes; a response-level caveat corrects the reading
            # without rewriting the engine's verbatim reasons (audit finding).
            caveats = []
            if until > 0:
                caveats.append(
                    f"off-turn basis: rows are priced as if on the clock at pick "
                    f"{target}; each survival figure looks past that pick, not to "
                    f"it — {until} pick(s) intervene first and may take these players"
                )
            return {
                **base,
                "picks_until_operator": until,
                "caveats": caveats,
                "desired": [
                    {**_rec_json(r),
                     "espn_name": self.espn_names.get(r.player_id)}
                    for r in recs[:k]
                ],
            }

    def queue_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record the queue the writer ACTUALLY achieved (spec §6's report-back
        half). Step 2 logs and displays; the push decision on a bad streak is
        step 4's and is deliberately not made here.

        Bounded storage: the report is display/telemetry, not state — a
        pathological payload must not grow RAM (same discipline as
        ``_MAX_CONFLICTS_SHOWN``).
        """
        league = str(payload.get("league") or "").strip()
        achieved_raw = payload.get("achieved")
        if not isinstance(achieved_raw, list):
            raise CockpitError("body must carry an 'achieved' list")
        ok = payload.get("ok")
        # Strict bool (the `type(exp) is int` idiom): a JSON string "false" is
        # truthy, and a coerced True here silently RESETS the failure streak
        # step 4's push trigger reads — the one counter that must not lie.
        if type(ok) is not bool:
            raise CockpitError("'ok' must be a JSON boolean")
        overall = payload.get("overall")
        reason = str(payload.get("reason") or "")[:300]
        achieved = [str(a)[:80] for a in achieved_raw[:_QUEUE_K_MAX * 2]]
        # The writer observes ESPN's Autopick toggle every cycle — the P2
        # load-bearing unknown (does expiry draw from the queue?). Recorded so
        # rehearsals gather the evidence; "off" is the state that would defeat
        # the whole queue-first design.
        autopick_raw = payload.get("autopick")
        autopick = autopick_raw if autopick_raw in ("on", "off", "unknown") else None
        with self._lock:
            # VALIDATE against the first-room-wins binding — NEVER establish
            # it. The binding is claimed only by the pick feed (/api/sync),
            # which rides the load-bearing data: a picks-free telemetry POST
            # that claimed it could bind a fresh cockpit to "" before the
            # harvester's first batch and brick /api/sync for the whole
            # unattended remainder of the draft (audit, demonstrated live).
            # A pre-binding report is recorded unbound — log-only, harmless —
            # and the mismatch check engages the moment sync claims the room.
            if self._sync_league is not None and league != self._sync_league:
                raise CockpitError(
                    "this cockpit session is already synced to a different "
                    "ESPN draft room — close the other room's tab (or "
                    "restart the cockpit to re-bind)"
                )
            self._queue_reports_received += 1
            self._queue_bad_streak = 0 if ok else self._queue_bad_streak + 1
            self._queue_last_report = {
                "ok": ok,
                "achieved": achieved,
                "reason": reason,
                "reported_overall": overall if type(overall) is int else None,
                "session_overall": self.session.overall_pick,
                "autopick": autopick,
            }
            self._queue_reports.append(dict(self._queue_last_report,
                                            n=self._queue_reports_received))
            del self._queue_reports[:-_QUEUE_REPORT_HISTORY]
            return {
                "ok": True,
                "epoch": self.sync_epoch,
                "session_overall": self.session.overall_pick,
                "bad_streak": self._queue_bad_streak,
            }

    def queue_reports_json(self) -> dict[str, Any]:
        """The bounded writer-report history (GET /api/queue/reports)."""
        with self._lock:
            return {
                "received": self._queue_reports_received,
                "kept": len(self._queue_reports),
                "reports": list(self._queue_reports),
            }

    # -- mutations (each recomputes synchronously, like the REPL) ------------

    def pick(self, player_id: str, *, expected_overall: int | None = None) -> dict[str, Any]:
        with self._lock:
            if self.session.complete:
                raise CockpitError("the draft is complete — no more picks")
            # Dual-writer guard (audit finding 8): once the sync feed is live,
            # a manual commit rendered against a stale head must not land one
            # slot late. Only enforced while sync is active so pure-manual
            # burst entry keeps its speed.
            if (
                expected_overall is not None
                and self._sync_received > 0
                and expected_overall != self.session.overall_pick
            ):
                raise CockpitError(
                    f"the board moved — pick {expected_overall} was already "
                    f"entered (now on pick {self.session.overall_pick}); "
                    "re-check and click again"
                )
            entry = next(
                (e for e in self.session.board if e.player_id == player_id), None
            )
            if entry is None:
                raise CockpitError(f"unknown player_id {player_id!r}")
            if player_id in self.session.taken:
                raise CockpitError(f"{entry.name or player_id} is already drafted")
            # Captured BEFORE the append so the client's confirmation label names
            # the slot this pick actually landed on (audit finding 5: the page's
            # own state can be stale mid-burst; the server's cannot).
            overall, seat = self.session.overall_pick, self.session.current_seat
            self.session.append_pick(player_id)
            self._drain_sync()
            self._recompute()
            return {"ok": True, "picked": _entry_json(entry), "overall": overall, "seat": seat}

    def undo(self) -> dict[str, Any]:
        with self._lock:
            try:
                self.session.undo_last()
            except Exception as exc:
                raise CockpitError(str(exc)) from exc
            # Deliberately NO sync drain here: the operator undid for a reason;
            # re-applying the same stashed pick instantly would fight them. But
            # a blocked banner for a DIFFERENT head is a misdirection trap
            # (audit finding 6) — reconcile it away.
            self._sync_reconcile_blocked()
            self._recompute()
            return {"ok": True}

    def edit(self, overall: int, player_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                self.session.edit_pick(int(overall), player_id=player_id)
            except Exception as exc:
                raise CockpitError(str(exc)) from exc
            # The operator just fixed this slot — its sync conflict is resolved
            # (audit finding 12; a fresh mismatch would be re-raised by the
            # next verify pass anyway).
            self._sync_conflicts.pop(int(overall), None)
            self._drain_sync()
            self._recompute()
            return {"ok": True}

    def autodraft(self) -> dict[str, Any]:
        """PROPOSE the legality-aware bot pick for the rival seat on the clock.

        Never commits — the page prefills the proposal for a one-click confirm
        (the ``a``-then-confirm REPL contract). Refuses on the operator's turn
        so a pure-ESPN pick can't bury the engine's recommendation."""
        with self._lock:
            if self.session.complete:
                raise CockpitError("the draft is complete")
            if self.session.is_operator_turn:
                raise CockpitError(
                    "it's YOUR pick — use the recommendation panel; autodraft "
                    "only fills in a rival's pick"
                )
            seat = self.session.current_seat
            try:
                pid = self.session.suggest_autodraft(seat)
            except Exception as exc:
                raise CockpitError(str(exc)) from exc
            entry = next(
                (e for e in self.session.board if e.player_id == pid), None
            )
            if entry is None:
                raise CockpitError("autodraft could not find a legal pick")
            return {"seat": seat, "proposal": _entry_json(entry)}

    # -- DOM-sync feed (userscript) -----------------------------------------

    def _sync_verify_existing(self, pick: ParsedPick) -> None:
        """A pick we already hold: cross-check instead of re-entering. A
        mismatch is SURFACED, never auto-edited (Rule 6 — the operator decides;
        the ``e N`` / click-to-edit flow is one click away). The NAME is
        compared first: an espn_id can be wrong for the same DOM-drift reasons
        the resolution rung distrusts it (audit finding 2), so an id match
        alone must not silence a name contradiction."""
        held = self.session.picks[pick.overall - 1]
        names_agree = bool(
            held.name and normalize_query(held.name) == normalize_query(pick.name)
        )
        ids_agree = pick.espn_id is not None and held.player_id == pick.espn_id
        # Leniency (rehearsal 2 finding): the operator CONFIRMED the held pick,
        # so verify exists to catch a WRONG PLAYER, not a name variant. If the
        # held player is among the resolver's own candidates for ESPN's name
        # ("Kenny Gainwell" -> Kenneth Gainwell via the surname tier), the two
        # sides agree on identity — no phantom conflict.
        plausible = False
        if not names_agree and held.player_id not in (None, ""):
            try:
                res = self.resolver.resolve(pick.name)
                plausible = any(
                    c.player_id == held.player_id for c in res.candidates
                )
            except Exception:
                plausible = False
        if names_agree or plausible or (ids_agree and not pick.name):
            self._sync_conflicts.pop(pick.overall, None)
            return
        self._sync_conflicts[pick.overall] = (
            f"pick {pick.overall}: cockpit has {held.name or held.player_id}, "
            f"ESPN shows {pick.name}",
            pick,
        )
        while len(self._sync_conflicts) > _MAX_CONFLICTS_SHOWN:
            self._sync_conflicts.pop(next(iter(self._sync_conflicts)))

    def _sync_try_apply_head(self, pick: ParsedPick) -> bool:
        """Resolve-and-commit the pick that is exactly at the session head.
        True on commit; False leaves it as the blocked pick."""
        res = resolve_synced_pick(
            self.resolver, self.session.board, pick, taken=self.session.taken
        )
        if not res.confident or res.entry is None:
            self._sync_blocked = (pick, res.reason)
            return False
        self.session.append_pick(res.entry.player_id)
        self._sync_blocked = None
        return True

    def _sync_reconcile_blocked(self) -> None:
        """A blocked pick is only meaningful AT the current head. If the head
        moved in either direction (manual entry past it, or an UNDO below it —
        audit finding 6: the stale 'Find him' banner misdirected a wrong-slot
        commit), drop it; the userscript resends and the machine re-decides."""
        if self._sync_blocked is not None and (
            self._sync_blocked[0].overall != self.session.overall_pick
        ):
            self._sync_blocked = None

    def _drain_sync(self) -> None:
        """Apply every stashed pick that has become the session head. Called
        after any state change (sync or manual) so the two entry paths
        interleave freely. Stops at the first pick the resolver refuses.
        Stashed picks the head has already passed (the operator typed them)
        are routed through the verify path and pruned."""
        self._sync_reconcile_blocked()
        while not self.session.complete:
            head = self.session.overall_pick
            for stale in [o for o in self._sync_pending if o < head]:
                self._sync_verify_existing(self._sync_pending.pop(stale))
            pick = self._sync_pending.get(head)
            if pick is None:
                break
            if not self._sync_try_apply_head(pick):
                break
            del self._sync_pending[head]
        self._sync_reconcile_blocked()

    def sync_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ingest a userscript batch. Returns which overalls the cockpit now
        OWNS (``accepted`` — the script stops resending those for THIS epoch),
        the session head, the epoch, and the blocked pick if the operator is
        needed."""
        raw = payload.get("picks")
        if not isinstance(raw, list):
            raise CockpitError("body must carry a 'picks' list")
        # The room identity: absent/empty league is still an IDENTITY (a room
        # whose URL carries no leagueId), never a bypass — re-audit finding 1
        # live-proved that a league-less batch walked straight past the
        # binding into a bound session.
        league = str(payload.get("league") or "").strip()
        parsed = [pp for p in raw if isinstance(p, dict) and (pp := parse_payload_pick(p))]
        draft_size = self.session.roster.teams * self.session.rounds_total
        accepted: list[int] = []
        applied = 0
        with self._lock:
            # First-room-wins binding (audit finding 18): a practice/mock room
            # left open in another tab must not feed the live session. Note
            # the honest limit: after a cockpit restart, whichever open tab
            # ticks first claims the binding — the operator closes tabs they
            # don't mean to sync (the loser's badge says so out loud).
            if self._sync_league is None:
                self._sync_league = league
            elif league != self._sync_league:
                raise CockpitError(
                    "this cockpit session is already synced to a different "
                    "ESPN draft room — close the other room's tab (or "
                    "restart the cockpit to re-bind)"
                )
            self._sync_received += len(parsed)
            state_changed = False
            for pick in sorted(parsed, key=lambda p: p.overall):
                if pick.overall > draft_size:
                    continue  # not a real slot in this draft; never stash it
                head = self.session.overall_pick
                if pick.overall < head or self.session.complete:
                    self._sync_verify_existing(pick)
                    accepted.append(pick.overall)
                elif pick.overall == head:
                    if self._sync_try_apply_head(pick):
                        accepted.append(pick.overall)
                        applied += 1
                        state_changed = True
                        self._drain_sync()
                    # blocked: NOT accepted — the script retries until cleared
                else:
                    self._sync_pending[pick.overall] = pick
                    accepted.append(pick.overall)
            if state_changed:
                self._recompute()
            blocked = self._sync_blocked
            return {
                "accepted": accepted,
                "applied": applied,
                "epoch": self.sync_epoch,
                "session_overall": self.session.overall_pick,
                "blocked": (
                    {"overall": blocked[0].overall, "name": blocked[0].name,
                     "reason": blocked[1]}
                    if blocked is not None else None
                ),
                "conflicts": len(self._sync_conflicts),
            }

    def _sync_state(self) -> dict[str, Any]:
        blocked = self._sync_blocked
        return {
            "active": self._sync_received > 0,
            "epoch": self.sync_epoch,
            "league": self._sync_league,
            "pending": sorted(self._sync_pending),
            "blocked": (
                {"overall": blocked[0].overall, "name": blocked[0].name,
                 "reason": blocked[1]}
                if blocked is not None else None
            ),
            "conflicts": [
                {"overall": k, "message": self._sync_conflicts[k][0]}
                for k in sorted(self._sync_conflicts)
            ],
        }

    def sync_fix(self, overall: int) -> dict[str, Any]:
        """One-click conflict repair: replace the cockpit's pick at ``overall``
        with what ESPN recorded there, when that resolves confidently. The
        held (wrong) player is released from ``taken`` for the resolution so
        the swap is legal. Falls back to a clear error telling the operator to
        use the edit flow when confidence fails."""
        with self._lock:
            entry_conflict = self._sync_conflicts.get(int(overall))
            if entry_conflict is None:
                raise CockpitError(f"no sync conflict recorded for pick {overall}")
            _msg, pick = entry_conflict
            held = self.session.picks[int(overall) - 1]
            taken = set(self.session.taken) - {held.player_id}
            res = resolve_synced_pick(
                self.resolver, self.session.board, pick, taken=taken
            )
            if not res.confident or res.entry is None:
                raise CockpitError(
                    f"couldn't auto-fix pick {overall} ({res.reason}) — click "
                    "the pick in the log and search for the right player"
                )
            try:
                self.session.edit_pick(int(overall), player_id=res.entry.player_id)
            except Exception as exc:
                raise CockpitError(str(exc)) from exc
            self._sync_conflicts.pop(int(overall), None)
            self._drain_sync()
            self._recompute()
            return {"ok": True, "fixed": _entry_json(res.entry), "overall": int(overall)}

    def posture_action(self, action: str) -> dict[str, Any]:
        with self._lock:
            if action == "accept":
                self.posture.accept()
            elif action == "dismiss":
                self.posture.dismiss()
            else:
                raise CockpitError(f"unknown posture action {action!r}")
            self._advice = None
            return {"ok": True}


# ------------------------------------------------------------------ HTTP layer


def _make_handler(cockpit: WebCockpit) -> type[BaseHTTPRequestHandler]:
    ui_html = _UI_PATH.read_text(encoding="utf-8")
    userscript_tpl = _USERSCRIPT_PATH.read_text(encoding="utf-8")
    queue_userscript_tpl = _QUEUE_USERSCRIPT_PATH.read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        # A local single-operator cockpit: keep the terminal quiet during bursts.
        def log_message(self, *_args: Any) -> None:  # pragma: no cover - cosmetic
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(
                code, json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _run(self, fn: Any) -> None:
            try:
                self._json(fn())
            except CockpitError as exc:
                self._json({"error": str(exc)}, code=400)
            except Exception as exc:  # surface, never hide (Rule 6 spirit)
                self._json({"error": f"internal error: {exc}"}, code=500)

        def do_GET(self) -> None:  # noqa: N802 (http.server contract)
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, ui_html.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/sync.user.js":
                # Serves the Tampermonkey script with this installation's token
                # and the live port baked in. Loopback-only server: any local
                # process could read this — same trust boundary as the journal
                # on disk (single-operator machine).
                script = (
                    userscript_tpl
                    .replace("{{TOKEN}}", cockpit.sync_token)
                    .replace("{{PORT}}", str(self.server.server_address[1]))
                )
                self._send(200, script.encode("utf-8"), "text/javascript; charset=utf-8")
            elif url.path == "/queue.user.js":
                # The queue-writer userscript (auto-entry spec §6), same token
                # + port substitution and the same trust boundary as sync.
                script = (
                    queue_userscript_tpl
                    .replace("{{TOKEN}}", cockpit.sync_token)
                    .replace("{{PORT}}", str(self.server.server_address[1]))
                )
                self._send(200, script.encode("utf-8"), "text/javascript; charset=utf-8")
            elif url.path == "/api/state":
                self._run(cockpit.state_json)
            elif url.path == "/api/queue":
                qs = parse_qs(url.query)
                raw_k = (qs.get("k") or [str(_QUEUE_K_DEFAULT)])[0]
                try:
                    k = int(raw_k)
                except ValueError:
                    k = _QUEUE_K_DEFAULT
                self._run(lambda: cockpit.queue_json(k))
            elif url.path == "/api/queue/reports":
                self._run(cockpit.queue_reports_json)
            elif url.path == "/api/board":
                self._run(cockpit.board_json)
            elif url.path == "/api/suggest":
                qs = parse_qs(url.query)
                q = (qs.get("q") or [""])[0]
                limit = (qs.get("limit") or [str(_SUGGEST_LIMIT_DEFAULT)])[0]
                try:
                    lim = int(limit)
                except ValueError:
                    lim = _SUGGEST_LIMIT_DEFAULT
                self._run(lambda: cockpit.suggest_json(q, lim))
            else:
                self._json({"error": "not found"}, code=404)

        def do_POST(self) -> None:  # noqa: N802 (http.server contract)
            # CSRF guard (audit 2026-07-24 finding 1): a hostile page in the
            # operator's browser can fire cross-origin "simple" POSTs (text/plain,
            # no preflight) at 127.0.0.1 and the WRITE executes even though the
            # response is unreadable — /api/undo needs no body, so a loop of it
            # would silently roll back live draft picks. Requiring exact
            # application/json makes every cross-origin write need a CORS
            # preflight; with no do_OPTIONS handler the preflight fails and the
            # browser never sends the write. The foreign-Origin reject is belt
            # and braces on top.
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._json({"error": "Content-Type must be application/json"}, code=403)
                return
            # The sync feed authenticates by TOKEN, not by origin: some
            # userscript managers forward the ESPN page's Origin, which the
            # CSRF guard would reject (audit finding 14) — but a page that
            # KNOWS the token isn't a confused deputy, so a valid token
            # bypasses the Origin check. Compare as bytes so a hostile
            # non-ASCII header can't raise out of compare_digest (note 27).
            sent_token = (self.headers.get("X-Zig-Sync-Token") or "").encode(
                "utf-8", "surrogateescape"
            )
            has_valid_token = hmac.compare_digest(
                sent_token, cockpit.sync_token.encode("utf-8")
            )
            origin = self.headers.get("Origin")
            if (
                not has_valid_token
                and origin is not None
                and urlparse(origin).hostname not in ("127.0.0.1", "localhost")
            ):
                # Includes Origin: null (sandboxed iframes) — hostname parses None.
                self._json({"error": "cross-origin request rejected"}, code=403)
                return
            url = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "malformed JSON body"}, code=400)
                return
            if url.path == "/api/sync":
                # The token (checked above, constant-time) is what stops any
                # other local page/process from injecting picks.
                if not has_valid_token:
                    self._json({"error": "bad sync token"}, code=403)
                    return
                self._run(lambda: cockpit.sync_apply(payload))
            elif url.path == "/api/queue/status":
                # Same trust boundary as /api/sync: this arrives from the
                # userscript, and the token is what authenticates it.
                if not has_valid_token:
                    self._json({"error": "bad sync token"}, code=403)
                    return
                self._run(lambda: cockpit.queue_status(payload))
            elif url.path == "/api/pick":
                exp = payload.get("expected_overall")
                self._run(lambda: cockpit.pick(
                    str(payload.get("player_id", "")),
                    # type() not isinstance(): a JSON true must not become 1
                    expected_overall=exp if type(exp) is int else None,
                ))
            elif url.path == "/api/undo":
                self._run(cockpit.undo)
            elif url.path == "/api/edit":
                self._run(
                    lambda: cockpit.edit(
                        payload.get("overall", 0), str(payload.get("player_id", ""))
                    )
                )
            elif url.path == "/api/sync/fix":
                ov = payload.get("overall")
                self._run(lambda: cockpit.sync_fix(
                    ov if type(ov) is int else 0
                ))
            elif url.path == "/api/autodraft":
                self._run(cockpit.autodraft)
            elif url.path == "/api/posture":
                self._run(
                    lambda: cockpit.posture_action(str(payload.get("action", "")))
                )
            else:
                self._json({"error": "not found"}, code=404)

    return Handler


def serve(
    session: DraftSession,
    *,
    port: int = 8811,
    espn_names: dict[str, str] | None = None,
) -> ThreadingHTTPServer:
    """Build the cockpit server on 127.0.0.1:``port`` (not started).

    The caller (CLI) calls ``serve_forever()``; tests start it on port 0 in a
    thread and shut it down. Loopback-only by construction (league-private data
    never listens on an external interface — Rule 5). ``espn_names`` is the
    ``simulator.espn_display_names`` mapping for the queue writer; None (tests,
    no DB) serves null per row."""
    cockpit = WebCockpit(session=session, espn_names=dict(espn_names or {}))
    return ThreadingHTTPServer(("127.0.0.1", port), _make_handler(cockpit))


def launch(
    board: Any,
    *,
    operator_slot: int,
    pick_order: Any,
    season: int,
    as_of: str,
    journal_path: Path,
    resume: bool = False,
    rollouts: int = 512,
    seed: int = 42,
    roster: Any = None,
    port: int = 8811,
    espn_names: dict[str, str] | None = None,
) -> None:
    """Build the session (start or resume — the exact ``app.launch`` semantics,
    including the O_EXCL no-clobber guard) and serve until Ctrl-C. The thin
    ``draft-web`` CLI calls this after loading the board (Rule 3)."""
    from ziggurat.core.valuation import DEFAULT_ROSTER
    from ziggurat.draft.session import JournalExistsError

    roster = roster if roster is not None else DEFAULT_ROSTER
    order = list(pick_order) if pick_order is not None else list(range(roster.teams))
    try:
        if resume:
            session = DraftSession.resume(journal_path, board)
        else:
            session = DraftSession.start(
                board,
                operator_slot=operator_slot,
                pick_order=order,
                season=season,
                as_of=as_of,
                journal_path=journal_path,
                roster=roster,
                session_seed=seed,
                rollouts=rollouts,
            )
    except JournalExistsError as exc:
        print(str(exc))
        print(
            "To continue that draft, re-run with --resume; to start a brand-new "
            "one, pass a different --journal path."
        )
        raise SystemExit(1) from exc

    for line in getattr(session, "resume_warnings", ()) or ():
        print(line)

    server = serve(session, port=port, espn_names=espn_names)
    host, bound_port = server.server_address[:2]
    print(f"Draft cockpit: http://{host}:{bound_port}/  (Ctrl-C to quit; journal: {session.journal_path})")
    print(f"ESPN sync userscript (install once in Tampermonkey): http://{host}:{bound_port}/sync.user.js")
    print(f"ESPN queue writer (install once in Tampermonkey): http://{host}:{bound_port}/queue.user.js")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        print(f"Draft saved to the journal ({session.journal_path}). Resume any time with --resume.")
