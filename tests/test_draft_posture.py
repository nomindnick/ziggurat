"""Unit tests for the item-2.4 posture check + hysteresis (``ziggurat/draft/posture.py``).

Two layers, all offline and deterministic (Rule 5 — SYNTHETIC player names only):

* The MANDATED guard — the hysteresis state machine — is driven through every
  transition with a **scripted stub session** + an injected evaluator, so the
  machine is exercised without a live draft (below-margin never fires; above-margin
  must persist across the ``consecutive`` threshold; a changed leading alternative
  restarts the streak; dismiss -> cooldown suppression; acceptance resets; the
  message is a complete novice sentence with no placeholder text).
* The DEFAULT comparator (``project_postures``) — the short paired room
  continuation — is exercised on a synthetic board with a deliberate receiver cliff
  so posture drift produces a real, legible edge, plus its non-gating guards.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.engine import (
    NEED_SCHEDULE_ROBUST_RB,
    NEED_SCHEDULE_ZERO_RB,
    PickEngine,
)
from ziggurat.draft.posture import (
    PostureAdvice,
    PostureMonitor,
    PostureProjection,
    project_postures,
)

# ------------------------------------------------------ scripted-session harness
#
# The monitor's football projection is injected, so the state machine is driven by
# handing each ``evaluate`` call a stub session carrying the projection to return.


@dataclass
class _ScriptedSession:
    projection: PostureProjection | None


def _scripted(session: _ScriptedSession) -> PostureProjection | None:
    return session.projection


def _proj(edge, *, label="zero_rb", lean="WR", overall=25) -> PostureProjection:
    return PostureProjection(
        current_label="balanced",
        alternative_label=label,
        lean_position=lean,
        edge=float(edge),
        current_points=1000.0,
        alternative_points=1000.0 + float(edge),
        overall_pick=overall,
    )


def _sess(edge, **kw) -> _ScriptedSession:
    """A stub session that scripts one projection (edge=None -> no alternative)."""
    if edge is None:
        return _ScriptedSession(None)
    return _ScriptedSession(_proj(edge, **kw))


def _monitor(*, margin=10.0, consecutive=1, cooldown=0) -> PostureMonitor:
    return PostureMonitor(
        margin=margin, consecutive=consecutive, cooldown=cooldown, evaluator=_scripted
    )


# --------------------------------------------------------- hysteresis: below-margin


def test_below_or_at_margin_never_fires():
    mon = _monitor(margin=10.0, consecutive=1, cooldown=0)
    for edge in (0.0, 5.0, 9.9, 10.0):  # 10.0 == margin: strictly-greater required
        for _ in range(4):
            assert mon.evaluate(_sess(edge)) is None
    assert mon.streak == 0 and not mon.active


def test_none_projection_never_fires():
    mon = _monitor(margin=10.0, consecutive=1, cooldown=0)
    for _ in range(5):
        assert mon.evaluate(_sess(None)) is None
    assert mon.streak == 0


# ------------------------------------------------ hysteresis: persistence threshold


def test_edge_must_persist_across_the_consecutive_threshold():
    mon = _monitor(margin=10.0, consecutive=3, cooldown=0)
    assert mon.evaluate(_sess(20.0)) is None      # streak 1
    assert mon.streak == 1
    assert mon.evaluate(_sess(20.0)) is None      # streak 2
    advice = mon.evaluate(_sess(20.0))            # streak 3 -> fires
    assert isinstance(advice, PostureAdvice)
    assert advice.evaluations_held == 3
    assert advice.edge_points == 20.0


def test_a_dip_below_margin_resets_the_streak():
    mon = _monitor(margin=10.0, consecutive=3, cooldown=0)
    assert mon.evaluate(_sess(20.0)) is None      # streak 1
    assert mon.evaluate(_sess(20.0)) is None      # streak 2
    assert mon.evaluate(_sess(4.0)) is None       # dip -> reset
    assert mon.streak == 0
    assert mon.evaluate(_sess(20.0)) is None      # streak 1 again
    assert mon.evaluate(_sess(20.0)) is None      # streak 2
    assert mon.evaluate(_sess(20.0)) is not None  # streak 3 -> fires


def test_a_changed_leading_alternative_restarts_the_streak():
    # No flip-flopping: a different winning archetype must itself persist.
    mon = _monitor(margin=10.0, consecutive=2, cooldown=0)
    assert mon.evaluate(_sess(20.0, label="zero_rb")) is None      # zero_rb streak 1
    assert mon.evaluate(_sess(20.0, label="robust_rb", lean="RB")) is None  # switch -> streak 1
    assert mon.streak == 1
    advice = mon.evaluate(_sess(20.0, label="robust_rb", lean="RB"))  # streak 2 -> fires
    assert advice is not None
    assert advice.alternative_label == "robust_rb"


# ------------------------------------------------------ hysteresis: once-then-quiet


def test_fires_once_then_stays_quiet_until_the_operator_acts():
    # New contract (recon §ux NEW-1): once fired, the monitor holds quiet on EVERY
    # later evaluation — even if the edge later drops below margin — until the
    # operator acknowledges (accept/dismiss). The app keeps the fired tip on screen
    # across intervening picks; only p/x clears it, so a below-margin read can never
    # silently self-suppress an unacknowledged nudge.
    mon = _monitor(margin=10.0, consecutive=1, cooldown=0)
    assert mon.evaluate(_sess(20.0)) is not None  # fires
    assert mon.active
    assert mon.evaluate(_sess(20.0)) is None      # still above margin: no re-nag
    assert mon.evaluate(_sess(3.0)) is None        # below margin: does NOT clear it
    assert mon.active                              # still awaiting the operator
    mon.accept()                                   # the operator acknowledges
    assert not mon.active
    assert mon.evaluate(_sess(20.0)) is not None  # a fresh signal fires again


def test_active_monitor_does_not_re_run_the_comparator():
    # The efficiency half of recon §ux F1: while a tip is active and awaiting the
    # operator, the expensive comparator must not run again (it did, ~0.15s/call,
    # before the _active guard was moved ahead of the evaluator call).
    calls = {"n": 0}

    def counting(session):
        calls["n"] += 1
        return _proj(50.0)

    mon = PostureMonitor(margin=10.0, consecutive=1, cooldown=0, evaluator=counting)
    assert mon.evaluate(_sess(50.0)) is not None   # fires; comparator ran once
    assert calls["n"] == 1
    assert mon.evaluate(_sess(50.0)) is None        # active -> comparator skipped
    assert mon.evaluate(_sess(50.0)) is None
    assert calls["n"] == 1                          # never re-ran while active


# ---------------------------------------------------------- hysteresis: dismiss


def test_dismiss_starts_a_cooldown_that_suppresses_advice():
    mon = _monitor(margin=10.0, consecutive=1, cooldown=3)
    assert mon.evaluate(_sess(20.0)) is not None  # fires
    mon.dismiss()
    assert mon.cooldown_remaining == 3
    # three evaluations of guaranteed silence even though the edge stays high
    assert mon.evaluate(_sess(20.0)) is None
    assert mon.evaluate(_sess(20.0)) is None
    assert mon.evaluate(_sess(20.0)) is None
    assert mon.cooldown_remaining == 0
    # cooldown spent -> a persistent signal can fire again
    assert mon.evaluate(_sess(20.0)) is not None


# ---------------------------------------------------------- hysteresis: acceptance


def test_acceptance_resets_the_machine_without_a_cooldown():
    mon = _monitor(margin=10.0, consecutive=2, cooldown=5)
    assert mon.evaluate(_sess(20.0)) is None      # streak 1
    assert mon.evaluate(_sess(20.0)) is not None  # streak 2 -> fires
    mon.accept()
    assert mon.streak == 0 and not mon.active and mon.cooldown_remaining == 0
    # no cooldown swallow (unlike dismiss), but the streak must be rebuilt from 0
    assert mon.evaluate(_sess(20.0)) is None      # streak 1 (not immediate)
    assert mon.evaluate(_sess(20.0)) is not None  # streak 2 -> fires again


def test_reset_is_an_acceptance_alias():
    mon = _monitor(margin=10.0, consecutive=1, cooldown=4)
    assert mon.evaluate(_sess(20.0)) is not None
    mon.dismiss()
    assert mon.cooldown_remaining == 4
    mon.reset()
    assert mon.cooldown_remaining == 0 and mon.streak == 0


# --------------------------------------------------------- the message (Rule 6)

_JARGON = ("vona", "sigma", "vor", "need_schedule", "archetype", "posture",
           "rollout", "hysteresis", "survival")


def _assert_clean_sentence(message: str) -> None:
    assert message and message[0].isupper()          # opens like a sentence
    assert message.rstrip().endswith(".")            # closes like one
    assert message.count(".") == 1                   # exactly one sentence
    assert re.search(r"\d", message)                 # carries the projected number
    assert "season points" in message                # legible currency
    assert not re.search(r"[{}]", message)           # no unfilled template
    for token in ("None", "TODO", "PLACEHOLDER", "{", "}"):
        assert token not in message
    low = message.lower()
    for token in _JARGON:
        assert token not in low, f"jargon leaked: {token!r}"


@pytest.mark.parametrize(
    "label,lean",
    [("zero_rb", "WR"), ("robust_rb", "RB"), ("hero_rb", "RB"),
     ("balanced", "TE"), ("balanced", None)],
)
def test_advice_message_is_a_clean_novice_sentence(label, lean):
    mon = _monitor(margin=10.0, consecutive=1, cooldown=0)
    advice = mon.evaluate(_sess(23.0, label=label, lean=lean))
    assert isinstance(advice, PostureAdvice)
    _assert_clean_sentence(advice.message)
    # the supporting numbers ride along with the sentence
    assert advice.edge_points == 23.0
    assert advice.alternative_label == label


def test_constructor_rejects_nonsense_thresholds():
    with pytest.raises(ValueError):
        PostureMonitor(margin=10.0, consecutive=0, cooldown=0)
    with pytest.raises(ValueError):
        PostureMonitor(margin=10.0, consecutive=1, cooldown=-1)


# =============================================================================
# The DEFAULT comparator: the short paired room continuation (project_postures).
# =============================================================================


def _cliff_board() -> tuple[BoardEntry, ...]:
    """Synthetic board with a receiver CLIFF: 8 elite WRs whose VOR sits right at
    the top RBs', then a hard drop. Rivals drain the elite WRs, so deferring
    receivers (an RB-heavy lean) leaves the operator stuck below the cliff — the
    scarcity that makes posture drift cost real points. SYNTHETIC names (Rule 5)."""
    wr_pts = [295, 292, 289, 286, 283, 280, 277, 274] + [150 - 1.5 * i for i in range(70)]
    rb_pts = [300 - 3.0 * i for i in range(70)]
    qb_pts = [320 - 6.0 * i for i in range(24)]
    te_pts = [250 - 5.0 * i for i in range(24)]
    dst_pts = [130 - 3.0 * i for i in range(14)]
    k_pts = [120 - 2.0 * i for i in range(14)]
    pos_pts = {"QB": qb_pts, "RB": rb_pts, "WR": wr_pts, "TE": te_pts,
               "DST": dst_pts, "K": k_pts}
    repl = {"QB": 250.0, "RB": 118.0, "WR": 118.0, "TE": 150.0, "DST": 90.0, "K": 100.0}
    entries = [(pos, i, max(1.0, p)) for pos, pl in pos_pts.items()
               for i, p in enumerate(pl)]
    skill = sorted((e for e in entries if e[0] not in ("K", "DST")), key=lambda e: -e[2])
    rank_by = {e[:2]: r for r, e in enumerate(skill, 1)}
    deep = 200
    for e in entries:
        if e[0] in ("K", "DST"):
            deep += 1
            rank_by[e[:2]] = deep
    return tuple(
        BoardEntry(f"{pos}-{i}", f"{pos}-{i}", pos, rank_by[(pos, i)], pts,
                   pts - repl[pos], team=None)
        for pos, i, pts in entries
    )


@dataclass
class _LiveStubSession:
    """The duck-typed ``PostureSession`` surface the default comparator reads."""

    board: Sequence[BoardEntry]
    own_roster: Sequence[BoardEntry]
    opponent_rosters: Mapping[int, Sequence[BoardEntry]]
    taken: set
    operator_slot: int
    overall_pick: int
    engine: PickEngine
    roster: object = DEFAULT_ROSTER
    rounds_total: int = 16
    pick_order: Sequence[int] = field(default_factory=lambda: list(range(10)))
    session_seed: int = 42
    complete: bool = False


def _rb_heavy_session(schedule) -> _LiveStubSession:
    board = _cliff_board()
    by_id = {e.player_id: e for e in board}
    own = [by_id["RB-0"], by_id["RB-2"]]  # already leaning RB
    taken = {"RB-0", "RB-2", "WR-0", "WR-1", "RB-1", "QB-0"}
    return _LiveStubSession(
        board=board,
        own_roster=own,
        opponent_rosters={t: [] for t in range(1, 10)},
        taken=set(taken),
        operator_slot=0,
        overall_pick=21,  # the operator's round-3 turn
        engine=PickEngine(need_schedule=schedule),
    )


def test_project_postures_detects_receiver_scarcity_drift():
    # RB-heavy roster + a receiver cliff the room is draining: easing off RB
    # (zero_rb) projects a stronger final lineup than the current RB-hammering plan.
    sess = _rb_heavy_session(NEED_SCHEDULE_ROBUST_RB)
    proj = project_postures(sess, rollouts=6)
    assert proj is not None
    assert proj.alternative_label == "zero_rb"
    assert proj.lean_position == "WR"
    assert proj.edge > 20.0
    assert proj.alternative_points > proj.current_points


def test_project_postures_is_deterministic():
    a = project_postures(_rb_heavy_session(NEED_SCHEDULE_ROBUST_RB), rollouts=6)
    b = project_postures(_rb_heavy_session(NEED_SCHEDULE_ROBUST_RB), rollouts=6)
    assert a == b


def test_project_postures_silent_when_current_lean_is_already_best():
    # Already easing off RB into the scarce receivers: no archetype beats it.
    sess = _rb_heavy_session(NEED_SCHEDULE_ZERO_RB)
    proj = project_postures(sess, rollouts=6)
    assert proj is not None
    assert proj.alternative_label is None
    assert proj.edge <= 0.0


def test_default_monitor_fires_real_advice_with_a_clean_message():
    # End-to-end through the monitor's DEFAULT evaluator (the real continuation).
    mon = PostureMonitor(margin=20.0, consecutive=1, cooldown=2)
    advice = mon.evaluate(_rb_heavy_session(NEED_SCHEDULE_ROBUST_RB))
    assert isinstance(advice, PostureAdvice)
    assert advice.alternative_label == "zero_rb"
    _assert_clean_sentence(advice.message)
    assert "running back" in advice.message.lower()


# ---------------------------------------------------- comparator non-gating guards


def test_project_postures_returns_none_when_draft_complete():
    sess = _rb_heavy_session(NEED_SCHEDULE_ROBUST_RB)
    sess.complete = True
    assert project_postures(sess) is None


def test_project_postures_returns_none_with_too_little_roster():
    sess = _rb_heavy_session(NEED_SCHEDULE_ROBUST_RB)
    sess.own_roster = sess.own_roster[:1]  # one pick — nothing has drifted yet
    assert project_postures(sess) is None


def test_project_postures_returns_none_past_the_end_of_the_draft():
    sess = _rb_heavy_session(NEED_SCHEDULE_ROBUST_RB)
    sess.overall_pick = sess.rounds_total * 10 + 1
    assert project_postures(sess) is None


def test_project_postures_stays_quiet_when_a_seam_is_missing():
    # A session missing a required attribute must degrade to no-advice, not crash
    # (non-gating: posture advice simply stays silent until the seam is wired).
    class _PartialSession:
        complete = False
        own_roster = ()  # present, but no board / engine / geometry

    assert project_postures(_PartialSession()) is None
