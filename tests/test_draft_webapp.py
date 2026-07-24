"""Endpoint tests for the Checkpoint-2 web draft cockpit (``ziggurat/draft/webapp.py``).

All offline: the synthetic conftest board, a tmp journal, tiny engine rollouts
(``rollouts=8``), and a real ``ThreadingHTTPServer`` on an EPHEMERAL loopback
port driven with ``urllib`` — the whole HTTP surface is exercised end-to-end
without touching the network beyond 127.0.0.1. Mirrored REPL contracts pinned
here: explicit-id pick commit, taken/unknown rejection, autodraft refusing the
operator's turn, posture accept/dismiss clearing the held tip, undo/edit
recompute, and verbatim reasons in the state payload (Rule 6).
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from ziggurat.draft.session import DraftSession
from ziggurat.draft.webapp import serve

# ------------------------------------------------------------------ harness


@pytest.fixture()
def cockpit(tmp_path, make_draft_board):
    """(base_url, session) against a live ephemeral-port server; auto-shutdown."""
    board = make_draft_board()
    session = DraftSession.start(
        board,
        operator_slot=0,
        pick_order=list(range(10)),
        season=2026,
        as_of="2026-07-24",
        journal_path=tmp_path / "web.jsonl",
        rollouts=8,
    )
    server = serve(session, port=0)  # ephemeral port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", session
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _post_err(base, path, payload):
    """POST expecting a non-2xx; returns (status, body)."""
    try:
        _post(base, path, payload)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    raise AssertionError("expected an HTTP error")


# ------------------------------------------------------------------ tests


def test_page_and_board_and_state_serve(cockpit):
    base, session = cockpit
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        assert r.status == 200
        assert b"Ziggurat Draft Cockpit" in r.read()

    board = _get(base, "/api/board")
    assert len(board) == len(session.board)
    assert {"player_id", "name", "position", "espn_rank", "vor"} <= set(board[0])

    state = _get(base, "/api/state")
    assert state["overall_pick"] == 1
    assert state["is_operator_turn"] is True  # operator_slot 0 opens the draft
    assert state["complete"] is False
    # Rule 6: the recommendation carries non-empty VERBATIM reasons.
    assert state["recs"] and state["recs"][0]["reasons"]


def test_suggest_endpoint_matches_and_excludes_taken(cockpit):
    base, _session = cockpit
    got = _get(base, "/api/suggest?q=qb0")
    assert got and got[0]["player_id"] == "QB-0"
    # empty query suggests nothing (MUST 1 parity)
    assert _get(base, "/api/suggest?q=") == []

    _post(base, "/api/pick", {"player_id": "QB-0"})
    after = _get(base, "/api/suggest?q=qb0")
    assert all(e["player_id"] != "QB-0" for e in after)


def test_pick_advances_state_and_journals(cockpit):
    base, session = cockpit
    out = _post(base, "/api/pick", {"player_id": "RB-0"})
    assert out["ok"] and out["picked"]["player_id"] == "RB-0"
    state = _get(base, "/api/state")
    assert state["overall_pick"] == 2
    assert state["picks"][-1]["player_id"] == "RB-0"
    assert "RB-0" in state["taken"]
    # journalled through the same fsync path the session owns
    text = session.journal_path.read_text()
    assert '"RB-0"' in text


def test_pick_rejects_unknown_and_already_taken(cockpit):
    base, _session = cockpit
    code, body = _post_err(base, "/api/pick", {"player_id": "NOPE"})
    assert code == 400 and "unknown" in body["error"]
    _post(base, "/api/pick", {"player_id": "RB-0"})
    code, body = _post_err(base, "/api/pick", {"player_id": "RB-0"})
    assert code == 400 and "already drafted" in body["error"]


def test_undo_and_edit_roundtrip(cockpit):
    base, _session = cockpit
    _post(base, "/api/pick", {"player_id": "RB-0"})
    _post(base, "/api/undo", {})
    assert _get(base, "/api/state")["overall_pick"] == 1

    _post(base, "/api/pick", {"player_id": "RB-1"})
    _post(base, "/api/edit", {"overall": 1, "player_id": "RB-2"})
    state = _get(base, "/api/state")
    assert state["picks"][0]["player_id"] == "RB-2"
    assert "RB-1" not in state["taken"]


def test_undo_on_empty_draft_is_a_400_not_a_500(cockpit):
    base, _session = cockpit
    code, body = _post_err(base, "/api/undo", {})
    assert code == 400 and body["error"]


def test_autodraft_refuses_operator_turn_then_proposes_for_rival(cockpit):
    base, _session = cockpit
    # pick 1 is the operator's (slot 0) — autodraft must refuse (REPL contract).
    code, body = _post_err(base, "/api/autodraft", {})
    assert code == 400 and "YOUR pick" in body["error"]

    _post(base, "/api/pick", {"player_id": "RB-0"})  # now seat 1 is on the clock
    out = _post(base, "/api/autodraft", {})
    assert out["seat"] == 1
    pid = out["proposal"]["player_id"]
    assert pid != "RB-0"
    # proposing did NOT commit — the pick count is unchanged until confirmed
    assert _get(base, "/api/state")["overall_pick"] == 2
    _post(base, "/api/pick", {"player_id": pid})
    assert _get(base, "/api/state")["overall_pick"] == 3


def test_posture_accept_and_dismiss_clear_the_tip(cockpit):
    base, _session = cockpit
    for action in ("accept", "dismiss"):
        out = _post(base, "/api/posture", {"action": action})
        assert out["ok"]
        assert _get(base, "/api/state")["posture"] is None
    code, body = _post_err(base, "/api/posture", {"action": "bogus"})
    assert code == 400


def test_malformed_body_is_a_400(cockpit):
    base, _session = cockpit
    req = urllib.request.Request(
        base + "/api/pick", data=b"not json",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 400


def test_unknown_routes_404(cockpit):
    base, _session = cockpit
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(base + "/api/nope", timeout=10)
    assert ei.value.code == 404


def test_cross_origin_simple_post_is_rejected_and_does_not_execute(cockpit):
    # Audit finding 1: a text/plain POST is a CORS "simple request" (no
    # preflight) — a hostile page could fire /api/undo blind. The guard must
    # 403 it BEFORE any state change.
    base, _session = cockpit
    _post(base, "/api/pick", {"player_id": "RB-0"})

    req = urllib.request.Request(
        base + "/api/undo", data=b"{}",
        headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 403
    # the write did NOT execute
    assert _get(base, "/api/state")["overall_pick"] == 2

    # foreign Origin is rejected even WITH the right content type…
    for origin in ("https://evil.example", "null"):
        req = urllib.request.Request(
            base + "/api/undo", data=b"{}",
            headers={"Content-Type": "application/json", "Origin": origin},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 403, origin
    # …while the page's own loopback origin passes.
    req = urllib.request.Request(
        base + "/api/undo", data=b"{}",
        headers={"Content-Type": "application/json", "Origin": base},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200
    assert _get(base, "/api/state")["overall_pick"] == 1


def test_concurrent_picks_serialize_under_the_lock(cockpit):
    # Audit finding 2: drive the server from many threads at once. Every pick
    # must land exactly once, on strictly-advancing overall slots, with no
    # torn state — remove WebCockpit._lock and this becomes flaky/corrupt.
    base, _session = cockpit
    players = [f"RB-{i}" for i in range(6)] + [f"WR-{i}" for i in range(6)]
    barrier = threading.Barrier(len(players))
    results = []

    def hit(pid):
        barrier.wait()
        try:
            results.append(("ok", _post(base, "/api/pick", {"player_id": pid})))
        except urllib.error.HTTPError as exc:
            results.append(("err", exc.code))

    threads = [threading.Thread(target=hit, args=(p,)) for p in players]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    oks = [r for kind, r in results if kind == "ok"]
    assert len(oks) == len(players)          # every distinct player committed
    overalls = sorted(r["overall"] for r in oks)
    assert overalls == list(range(1, len(players) + 1))  # no slot torn or reused
    state = _get(base, "/api/state")
    assert state["overall_pick"] == len(players) + 1
    assert set(p for p in state["taken"]) == set(players)


# --------------------------------------------------------- DOM-sync endpoint


def _sync_post(base, session, picks, token=None):
    if token is None:
        token = (session.journal_path.parent / "sync-token.txt").read_text().strip()
    req = urllib.request.Request(
        base + "/api/sync", data=json.dumps({"picks": picks}).encode(),
        headers={"Content-Type": "application/json", "X-Zig-Sync-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_sync_requires_the_token(cockpit):
    base, session = cockpit
    with pytest.raises(urllib.error.HTTPError) as ei:
        _sync_post(base, session, [], token="wrong-token")
    assert ei.value.code == 403
    # and the userscript endpoint serves the real token + live port
    with urllib.request.urlopen(base + "/sync.user.js", timeout=10) as r:
        script = r.read().decode()
    real = (session.journal_path.parent / "sync-token.txt").read_text().strip()
    assert real in script and str(base.rsplit(":", 1)[1]) in script
    assert "{{TOKEN}}" not in script and "{{PORT}}" not in script


def test_sync_applies_picks_in_order(cockpit):
    base, session = cockpit
    out = _sync_post(base, session, [
        {"overall": 1, "player": "RB0"},
        {"overall": 2, "player": "WR0"},
    ])
    assert out["applied"] == 2 and out["session_overall"] == 3
    assert out["blocked"] is None
    assert sorted(out["accepted"]) == [1, 2]
    state = _get(base, "/api/state")
    assert [p["player_id"] for p in state["picks"]] == ["RB-0", "WR-0"]
    assert state["sync"]["active"] is True


def test_sync_stashes_out_of_order_then_drains(cockpit):
    base, session = cockpit
    out = _sync_post(base, session, [{"overall": 3, "player": "WR1"}])
    assert out["applied"] == 0 and 3 in out["accepted"]
    assert _get(base, "/api/state")["sync"]["pending"] == [3]

    out = _sync_post(base, session, [
        {"overall": 2, "player": "RB1"},
        {"overall": 1, "player": "RB0"},
    ])
    # 1 and 2 apply in order, then the stashed 3 drains automatically.
    assert out["session_overall"] == 4
    state = _get(base, "/api/state")
    assert [p["player_id"] for p in state["picks"]] == ["RB-0", "RB-1", "WR-1"]
    assert state["sync"]["pending"] == []


def test_sync_blocks_on_unresolvable_name_and_manual_entry_unblocks(cockpit):
    base, session = cockpit
    out = _sync_post(base, session, [{"overall": 1, "player": "Zzyzx Nobody"}])
    assert out["applied"] == 0
    assert out["blocked"]["overall"] == 1
    assert 1 not in out["accepted"]           # the script keeps retrying it
    sync = _get(base, "/api/state")["sync"]
    assert sync["blocked"]["name"] == "Zzyzx Nobody"

    # Operator enters the pick manually -> blocked clears on the next batch,
    # and the retried pick dedupes through the verify path.
    _post(base, "/api/pick", {"player_id": "RB-0"})
    out = _sync_post(base, session, [{"overall": 1, "player": "Zzyzx Nobody"}])
    assert 1 in out["accepted"] and out["blocked"] is None
    # verify path saw a name mismatch and surfaced it as a conflict
    assert _get(base, "/api/state")["sync"]["conflicts"]


def test_sync_repost_is_idempotent_and_verifies(cockpit):
    base, session = cockpit
    _sync_post(base, session, [{"overall": 1, "player": "RB0"}])
    out = _sync_post(base, session, [{"overall": 1, "player": "RB0"}])
    assert out["applied"] == 0 and out["session_overall"] == 2
    assert 1 in out["accepted"]
    assert _get(base, "/api/state")["sync"]["conflicts"] == []


def test_sync_and_manual_quick_picks_interleave(cockpit):
    base, session = cockpit
    _sync_post(base, session, [{"overall": 2, "player": "WR0"}])   # stashed
    _post(base, "/api/pick", {"player_id": "RB-0"})                # manual pick 1
    # the stashed pick 2 drained on the manual state change
    state = _get(base, "/api/state")
    assert state["overall_pick"] == 3
    assert [p["player_id"] for p in state["picks"]] == ["RB-0", "WR-0"]


def test_sync_malformed_items_are_ignored_not_fatal(cockpit):
    base, session = cockpit
    out = _sync_post(base, session, [
        {"overall": "x", "player": "RB0"}, {"nonsense": True},
        {"overall": 1, "player": "RB0"},
    ])
    assert out["applied"] == 1 and out["session_overall"] == 2

    token = (session.journal_path.parent / "sync-token.txt").read_text().strip()
    req = urllib.request.Request(
        base + "/api/sync", data=json.dumps({"picks": "not-a-list"}).encode(),
        headers={"Content-Type": "application/json", "X-Zig-Sync-Token": token},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 400


def test_sync_response_carries_a_stable_epoch(cockpit):
    base, session = cockpit
    a = _sync_post(base, session, [{"overall": 1, "player": "RB0"}])
    b = _sync_post(base, session, [{"overall": 2, "player": "WR0"}])
    assert a["epoch"] and a["epoch"] == b["epoch"]
    assert _get(base, "/api/state")["sync"]["epoch"] == a["epoch"]


def _sync_post_league(base, session, picks, league):
    token = (session.journal_path.parent / "sync-token.txt").read_text().strip()
    req = urllib.request.Request(
        base + "/api/sync",
        data=json.dumps({"league": league, "picks": picks}).encode(),
        headers={"Content-Type": "application/json", "X-Zig-Sync-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_sync_binds_to_the_first_league_and_rejects_others(cockpit):
    # Audit finding 18: a practice-draft tab left open must not feed the
    # live session. First room wins; a different room 400s.
    base, session = cockpit
    _sync_post_league(base, session, [{"overall": 1, "player": "RB0"}], "111")
    with pytest.raises(urllib.error.HTTPError) as ei:
        _sync_post_league(base, session, [{"overall": 2, "player": "WR0"}], "222")
    assert ei.value.code == 400
    assert "different ESPN draft room" in json.loads(ei.value.read())["error"]
    assert _get(base, "/api/state")["overall_pick"] == 2  # pick 2 never landed
    # the bound room keeps flowing
    out = _sync_post_league(base, session, [{"overall": 2, "player": "WR0"}], "111")
    assert out["applied"] == 1


def test_manual_pick_with_stale_expected_overall_rejected_while_sync_active(cockpit):
    # Audit finding 8: the dual-writer race — a confirm rendered against head
    # N arriving after sync applied ESPN's real pick N must NOT land at N+1.
    base, session = cockpit
    _sync_post(base, session, [{"overall": 1, "player": "RB0"}])  # sync active, head=2
    token_stale = json.dumps({"player_id": "WR-0", "expected_overall": 1}).encode()
    req = urllib.request.Request(
        base + "/api/pick", data=token_stale,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 400
    assert "board moved" in json.loads(ei.value.read())["error"]
    # with the CURRENT head it commits fine
    out = _post(base, "/api/pick", {"player_id": "WR-0", "expected_overall": 2})
    assert out["ok"]


def test_manual_pick_guard_inactive_without_sync(cockpit):
    # Pure-manual burst entry keeps its speed: no sync feed -> no guard.
    base, _session = cockpit
    out = _post(base, "/api/pick", {"player_id": "RB-0", "expected_overall": 99})
    assert out["ok"]


def test_undo_clears_a_stale_blocked_banner(cockpit):
    # Audit finding 6 (critical): blocked at head N + undo -> banner must NOT
    # keep pointing the operator at pick N while the head is N-1.
    base, session = cockpit
    _sync_post(base, session, [{"overall": 1, "player": "RB0"}])
    _sync_post(base, session, [{"overall": 2, "player": "Zzyzx Nobody"}])
    assert _get(base, "/api/state")["sync"]["blocked"]["overall"] == 2
    _post(base, "/api/undo", {})   # head back to 1
    sync = _get(base, "/api/state")["sync"]
    assert sync["blocked"] is None


def test_conflict_clears_when_the_operator_edits_the_slot(cockpit):
    # Audit finding 12: fixing the flagged pick must clear its conflict.
    base, session = cockpit
    _post(base, "/api/pick", {"player_id": "RB-0"})
    _sync_post(base, session, [{"overall": 1, "player": "WR0"}])  # mismatch
    assert _get(base, "/api/state")["sync"]["conflicts"]
    _post(base, "/api/edit", {"overall": 1, "player_id": "WR-0"})
    assert _get(base, "/api/state")["sync"]["conflicts"] == []


def test_sync_ignores_overalls_beyond_the_draft(cockpit):
    base, session = cockpit
    out = _sync_post(base, session, [{"overall": 9999, "player": "RB0"}])
    assert 9999 not in out["accepted"]
    assert _get(base, "/api/state")["sync"]["pending"] == []


def test_sync_league_binding_cannot_be_bypassed_by_empty_league(cockpit):
    # Re-audit finding 1 (live-proven): after binding, a batch with NO league
    # (or league:"") must be rejected, not waved through.
    base, session = cockpit
    _sync_post_league(base, session, [{"overall": 1, "player": "RB0"}], "111")
    for bad_payload in (
        {"picks": [{"overall": 2, "player": "WR0"}]},                  # absent
        {"league": "", "picks": [{"overall": 2, "player": "WR0"}]},    # empty
    ):
        token = (session.journal_path.parent / "sync-token.txt").read_text().strip()
        req = urllib.request.Request(
            base + "/api/sync", data=json.dumps(bad_payload).encode(),
            headers={"Content-Type": "application/json", "X-Zig-Sync-Token": token},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 400
    assert _get(base, "/api/state")["overall_pick"] == 2  # nothing leaked in
