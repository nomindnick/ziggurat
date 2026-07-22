"""Snapshot-style tests for the item-2.4 draft-board renderers (``board_view.py``).

All offline, synthetic names only (Rule 5 — never real colleague or player
identities; "Alpha Runner" is the house pattern). Renderers are pure (data in ->
Rich renderable out), so we point a ``Console(file=StringIO())`` at each and assert
on the captured text. The session/resolver/posture modules are NOT imported here
(a parallel builder owns them) — ``render_roster`` / ``render_status`` are exercised
with duck-typed stubs.

Load-bearing assertions:
  * Rule 6: ``render_recommendation`` shows ``PickRec.reasons`` VERBATIM.
  * tiers show a value-cliff break and EXCLUDE taken players.
  * the ESPN view is ordered by the room's board rank and marks taken picks out.
  * the honesty status line never claims a phantom recalibration.
  * every renderer survives a too-narrow (width 60) terminal.
"""

from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.draft.board_view import (
    render_autodraft_suggestion,
    render_confirm,
    render_contingencies,
    render_espn_view,
    render_recommendation,
    render_roster,
    render_status,
    render_tiers,
)
from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.engine import PickRec
from ziggurat.draft.survival import LIVE_RECAL_MIN_PICKS

# ------------------------------------------------------------------ fixtures


def _entry(pid, name, pos, rank, vor, team, pts=None):
    return BoardEntry(pid, name, pos, rank, pts if pts is not None else vor + 100.0, vor, team)


def _board():
    """A small synthetic board with a deliberate RB value cliff (92/90/88 -> 60)."""
    return (
        _entry("QB-ALPHA", "Alpha Passer", "QB", 5, 40.0, "AAA"),
        _entry("QB-BETA", "Beta Passer", "QB", 30, 20.0, "BBB"),
        _entry("RB-HOTEL", "Hotel Back", "RB", 2, 95.0, "HHH"),   # taken in tests -> excluded
        _entry("RB-GAMMA", "Gamma Runner", "RB", 1, 92.0, "CCC"),
        _entry("RB-DELTA", "Delta Runner", "RB", 4, 90.0, "DDD"),
        _entry("RB-ECHO", "Echo Runner", "RB", 7, 88.0, "EEE"),
        _entry("RB-FOX", "Foxtrot Runner", "RB", 40, 60.0, "FFF"),
        _entry("WR-INDIA", "India Catcher", "WR", 3, 80.0, "III"),
        _entry("WR-JULIET", "Juliet Catcher", "WR", 9, 70.0, "JJJ"),
        _entry("TE-KILO", "Kilo End", "TE", 12, 45.0, "KKK"),
        _entry("DST-LIMA", "Lima Defense", "DST", 150, 12.0, "LLL"),
        _entry("K-MIKE", "Mike Kicker", "K", 160, 8.0, "MMM"),
    )


def _capture(renderable, width=100):
    console = Console(file=StringIO(), width=width, no_color=True, highlight=False)
    console.print(renderable)
    return console.file.getvalue()


def _norm(text):
    """Collapse all whitespace so a wrapped Rich line still matches verbatim."""
    return " ".join(text.split())


# --------------------------------------------------------- recommendation (Rule 6)


def _pick_rec(entry, reasons, alternatives=()):
    return PickRec(
        player=entry,
        pick_score=112.0,
        vor=entry.vor,
        survival_next=0.22,
        vona=24.0,
        need_note="fills your open RB2 starter slot",
        risk_note="steady, high-floor RB",
        divergence_note="",
        reasons=tuple(reasons),
        alternatives=tuple(alternatives),
    )


def test_render_recommendation_shows_reasons_verbatim():
    board = {e.player_id: e for e in _board()}
    reasons = (
        "He fills your open RB2 starter slot.",
        "He very likely will not last until your next pick.",
    )
    top = _pick_rec(board["RB-GAMMA"], reasons, alternatives=[
        ("Delta Runner", "you can likely wait — about 80% he is still there next pick"),
    ])
    alt = _pick_rec(board["RB-DELTA"], ("Solid depth at running back.",))
    out = _norm(_capture(render_recommendation((top, alt))))

    for reason in reasons:  # VERBATIM — no paraphrase (Rule 6)
        assert _norm(reason) in out
    assert "Gamma Runner" in out          # the picked player
    assert "Delta Runner" in out          # the alternative row
    assert "you can likely wait" in out   # the engine's verbatim why-not


def test_render_recommendation_handles_empty():
    out = _capture(render_recommendation(()))
    assert "No recommendation" in out  # graceful, never a crash


# --------------------------------------------------------------------- tiers


def test_render_tiers_shows_cliff_and_excludes_taken():
    board = _board()
    out = _capture(render_tiers(board, taken={"RB-HOTEL"}))
    # A value cliff (88 -> 60) must be surfaced as a legible warning line.
    assert "startable" in out and "drop" in out
    # The still-available RBs appear...
    assert "Gamma Runner" in out and "Foxtrot Runner" in out
    # ...but a drafted player is dropped from the live board (marked out).
    assert "Hotel Back" not in out


def test_render_tiers_cliff_counts_available_only():
    board = _board()
    # With Hotel Back drafted, Gamma/Delta/Echo (92/90/88) form the top RB tier
    # before the 28-pt cliff to Foxtrot (60) — the count reflects AVAILABLE only.
    out = _capture(render_tiers(board, taken={"RB-HOTEL"}))
    assert "3 startable RBs left before a 28-point drop" in _norm(out)


# --------------------------------------------------------------- ESPN room view


def test_render_espn_view_orders_by_room_rank_and_marks_taken():
    board = _board()
    out = _capture(render_espn_view(board, taken={"RB-DELTA"}))
    # Ordered by ESPN rank: Gamma (#1) appears before India Catcher (#3).
    assert out.index("Gamma Runner") < out.index("India Catcher")
    # ESPN rank 3 (India) before rank 5 (Alpha Passer).
    assert out.index("India Catcher") < out.index("Alpha Passer")
    # The drafted player's row carries the taken marker.
    delta_line = next(ln for ln in out.splitlines() if "Delta Runner" in ln)
    assert "×" in delta_line


def test_render_espn_view_omits_unranked_fallback():
    board = (*_board(), _entry("RB-ZULU", "Zulu Scrub", "RB", 10_042, 1.0, "ZZZ"))
    out = _capture(render_espn_view(board, taken=set()))
    assert "Zulu Scrub" not in out  # not on the room's real board


# --------------------------------------------------------------------- roster


def test_render_roster_shows_filled_and_open_slots():
    board = {e.player_id: e for e in _board()}
    session = SimpleNamespace(
        own_roster=(board["QB-ALPHA"], board["RB-GAMMA"]),
        roster=DEFAULT_ROSTER,
    )
    out = _capture(render_roster(session))
    assert "Alpha Passer" in out          # QB slot filled
    assert "Gamma Runner" in out          # RB1 filled
    assert "Still need to fill" in out    # roster is incomplete
    assert "— open —" in out              # at least one open slot rendered


# ----------------------------------------------------------------- status honesty


def test_render_status_baseline_says_picks_needed():
    recal = SimpleNamespace(engaged=False, n_room_picks=5, reach_sigma=None)
    out = _norm(_capture(render_status(recal, advice=None)))
    assert "2025 baseline" in out
    assert f"need {LIVE_RECAL_MIN_PICKS - 5} more" in out
    assert "recalibrated to 0" not in out  # never a phantom-adaptation dishonesty


def test_render_status_engaged_reports_adaptation():
    recal = SimpleNamespace(engaged=True, n_room_picks=30, reach_sigma=12.3)
    out = _norm(_capture(render_status(recal, advice=None)))
    assert "adapted from 30" in out
    assert "reach spread about 12" in out


def test_render_status_shows_posture_advice_sentence():
    recal = SimpleNamespace(engaged=False, n_room_picks=0, reach_sigma=None)
    advice = SimpleNamespace(message="Consider taking a running back now — the position is about to thin out.")
    out = _norm(_capture(render_status(recal, advice)))
    assert "Consider taking a running back now" in out


# --------------------------------------------------------------- edge renderers


def test_confirm_and_autodraft_and_contingency_render():
    board = _board()
    assert "Gamma Runner" in _capture(render_confirm(board[3:6]))
    assert "Gamma Runner" in _capture(render_autodraft_suggestion(board[3], seat=4))
    # Empty contingencies degrade gracefully; a duck-typed branch renders too.
    assert "No branch plans" in _capture(render_contingencies(()))
    branch = SimpleNamespace(if_gone="Gamma Runner", then="Delta Runner")
    assert "Delta Runner" in _capture(render_contingencies((branch,)))


# ------------------------------------------------------------- width resilience


def test_all_views_survive_narrow_terminal():
    board = _board()
    session = SimpleNamespace(own_roster=(board[3], board[7]), roster=DEFAULT_ROSTER)
    recal = SimpleNamespace(engaged=True, n_room_picks=25, reach_sigma=16.0)
    top = _pick_rec(board[3], ("A short reason.",), alternatives=[("Delta Runner", "wait")])
    renderables = [
        render_recommendation((top, _pick_rec(board[4], ("Depth.",)))),
        render_tiers(board, taken={"RB-HOTEL"}),
        render_espn_view(board, taken={"RB-DELTA"}),
        render_roster(session),
        render_status(recal, SimpleNamespace(message="Tip.")),
        render_confirm(board[3:6]),
    ]
    for r in renderables:
        # Must not raise at a cramped 60-column width.
        assert _capture(r, width=60)
