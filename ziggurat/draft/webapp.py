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

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.posture import PostureMonitor
from ziggurat.draft.resolver import NameResolver
from ziggurat.draft.session import DraftSession

_UI_PATH = Path(__file__).with_name("webui.html")

# The web pick flow commits an explicit player_id the operator SAW and chose, so
# suggestion count only needs to cover honest ambiguity, not typo rescue depth.
_SUGGEST_LIMIT_DEFAULT = 8
_SUGGEST_LIMIT_MAX = 25


class CockpitError(ValueError):
    """A user-correctable request error (rendered as a 400 with a message)."""


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

    def __post_init__(self) -> None:
        self.resolver = NameResolver(self.session.board)
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
            }

    # -- mutations (each recomputes synchronously, like the REPL) ------------

    def pick(self, player_id: str) -> dict[str, Any]:
        with self._lock:
            if self.session.complete:
                raise CockpitError("the draft is complete — no more picks")
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
            self._recompute()
            return {"ok": True, "picked": _entry_json(entry), "overall": overall, "seat": seat}

    def undo(self) -> dict[str, Any]:
        with self._lock:
            try:
                self.session.undo_last()
            except Exception as exc:
                raise CockpitError(str(exc)) from exc
            self._recompute()
            return {"ok": True}

    def edit(self, overall: int, player_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                self.session.edit_pick(int(overall), player_id=player_id)
            except Exception as exc:
                raise CockpitError(str(exc)) from exc
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
            elif url.path == "/api/state":
                self._run(cockpit.state_json)
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
            origin = self.headers.get("Origin")
            if origin is not None and urlparse(origin).hostname not in (
                "127.0.0.1", "localhost"
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
            if url.path == "/api/pick":
                self._run(lambda: cockpit.pick(str(payload.get("player_id", ""))))
            elif url.path == "/api/undo":
                self._run(cockpit.undo)
            elif url.path == "/api/edit":
                self._run(
                    lambda: cockpit.edit(
                        payload.get("overall", 0), str(payload.get("player_id", ""))
                    )
                )
            elif url.path == "/api/autodraft":
                self._run(cockpit.autodraft)
            elif url.path == "/api/posture":
                self._run(
                    lambda: cockpit.posture_action(str(payload.get("action", "")))
                )
            else:
                self._json({"error": "not found"}, code=404)

    return Handler


def serve(session: DraftSession, *, port: int = 8811) -> ThreadingHTTPServer:
    """Build the cockpit server on 127.0.0.1:``port`` (not started).

    The caller (CLI) calls ``serve_forever()``; tests start it on port 0 in a
    thread and shut it down. Loopback-only by construction (league-private data
    never listens on an external interface — Rule 5)."""
    cockpit = WebCockpit(session=session)
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

    server = serve(session, port=port)
    host, bound_port = server.server_address[:2]
    print(f"Draft cockpit: http://{host}:{bound_port}/  (Ctrl-C to quit; journal: {session.journal_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        print(f"Draft saved to the journal ({session.journal_path}). Resume any time with --resume.")
