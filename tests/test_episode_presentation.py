"""Item 4.2b, B3 — the NEW / REPEAT episode rule and the presentation strings.

Two things are pinned here and they are different in kind.

**The episode rule** is a LABELLED HYPOTHESIS with one free parameter that moves
the answer 50 points on the 2025 backfill (70.3% NEW at gap>1, 48.7% at gap>2,
20.6% at one-per-season). So the tests pin the RULE, not a number: a bye week is
not a gap; three evaluated weeks with no flag is; week 1 is its own regime; and
an archive that does not reach back far enough says so rather than claiming
novelty it never checked.

**The presentation strings** are the promise the whole item makes to a novice —
that this column is evidence and not an instruction. A promise with no test is a
sentence, so the header, the banner, the legend and (for the first time) the
briefing's LLM system prompt are all pinned, plus the one behavioural fact that
makes the promise TRUE: the claim chain is byte-identical with the column full
and with it empty.

Every player name below is invented (Rule 5).
"""

import re

import pytest

from ziggurat.core import candidates as C
from ziggurat.core import waiver
from ziggurat.core.waiver import USAGE_EVIDENCE_HEADER, build_waiver_plan
from ziggurat.paths import REPO_ROOT
from ziggurat.push.run import BRIEFING_SYSTEM

USAGE = C.SIGNAL_USAGE
INJURY = C.SIGNAL_INJURY


# --------------------------------------------------------------- rule fixtures


def _wf(week, *, evaluated=(), flagged=()):
    """One archived week, in episode-key space."""
    ev = frozenset(C.episode_key(k, p) for k, p in evaluated)
    fl = frozenset(C.episode_key(k, p) for k, p in flagged)
    return C.WeekFlags(week=week, evaluated=ev | fl, flagged=fl)


def _tag(player, *, week, history, kind=USAGE, gap_weeks=C.EPISODE_GAP_WEEKS):
    return C.episode_tag_for(kind, player, week=week, history=history,
                             gap_weeks=gap_weeks)


# ============================================================ the episode rule


def test_a_flag_in_the_previous_evaluated_week_is_a_repeat():
    history = [
        _wf(3, evaluated=[(USAGE, "hank")], flagged=[(USAGE, "hank")]),
        _wf(4, evaluated=[(USAGE, "hank")]),
    ]
    # wk3 flag, wk4 evaluated-not-flagged, now wk5: gap of 2 evaluated weeks —
    # still inside the window, so the episode continues.
    assert _tag("hank", week=5, history=history) == "REPEAT (also wk 3)"


def test_three_evaluated_weeks_without_a_flag_ends_the_episode():
    history = [
        _wf(2, evaluated=[(USAGE, "hank")], flagged=[(USAGE, "hank")]),
        _wf(3, evaluated=[(USAGE, "hank")]),
        _wf(4, evaluated=[(USAGE, "hank")]),
        _wf(5, evaluated=[(USAGE, "hank")]),
    ]
    # the last flag is 4 evaluated weeks back — past the gap, a new episode.
    assert _tag("hank", week=6, history=history) == C.EPISODE_NEW


def test_a_bye_week_does_not_end_an_episode():
    """The headline case. ``usage_deltas`` emits NO row for a bye or an inactive,
    and 12.0% of one-week gaps on the 2025 backfill are exactly that. A calendar
    rule would call this man NEW; he is the same story, one week later."""
    history = [
        _wf(4, evaluated=[(USAGE, "hank")], flagged=[(USAGE, "hank")]),
        _wf(5, evaluated=[(USAGE, "rival")]),   # hank on bye — not evaluated
        _wf(6, evaluated=[(USAGE, "rival")]),   # still out — not evaluated
        _wf(7, evaluated=[(USAGE, "hank")]),    # back, no flag
    ]
    assert _tag("hank", week=8, history=history) == "REPEAT (also wk 4)"
    # and the same history under a CALENDAR reading (every week counts) would
    # have ended it — which is the whole reason the rule is worded this way.
    calendar = [_wf(w.week, evaluated=[(USAGE, "hank")],
                    flagged=[(USAGE, "hank")] if C.episode_key(USAGE, "hank") in w.flagged
                    else []) for w in history]
    assert _tag("hank", week=8, history=calendar) == C.EPISODE_NEW


def test_week_one_gets_its_own_tag_and_never_new():
    """Week 1 is the all-emergence regime: 321 of 321 evaluated rows on the 2025
    backfill carried prior_week=None, 158 flagged, and the top of that board is
    established starters. Marking them NEW is a false claim of novelty on the
    exact day the archive begins."""
    assert _tag("hank", week=1, history=[]) == C.EPISODE_WEEK1
    # even with a full prior-season-shaped archive in hand
    assert _tag("hank", week=1, history=[_wf(1, flagged=[(USAGE, "hank")])]) == C.EPISODE_WEEK1
    assert C.EPISODE_WEEK1 != C.EPISODE_NEW
    assert C.episode_legend(1) == C.EPISODE_WEEK1_NOTE
    assert "first observation, not a role change" in C.EPISODE_WEEK1_NOTE


def test_no_archive_is_first_seen_never_new():
    """'NEW' is a comparison. With nothing to compare against, saying it is a
    claim we did not check — the badge names the absence instead."""
    assert _tag("hank", week=9, history=[]) == C.EPISODE_FIRST_SEEN
    assert "no archive yet" in C.EPISODE_FIRST_SEEN
    assert C.EPISODE_FIRST_SEEN != C.EPISODE_NEW


def test_an_archive_that_stops_short_bounds_its_own_new():
    """An absence is only a fact when you know it is one (the 3.2c tombstone
    lesson). A flag FOUND is positive evidence whatever the coverage; 'no flag'
    is only NEW if the weeks that could have carried one were archived."""
    # archive holds wk 6 only; we are at wk 7, so wk 1-5 are unknown
    history = [_wf(6, evaluated=[(USAGE, "hank")])]
    tag = _tag("hank", week=7, history=history)
    assert tag.startswith(C.EPISODE_NEW) and "no archive for 5 earlier wk(s)" in tag
    # but once the window CLOSES inside the weeks we do hold, nothing earlier can
    # change the answer, and the badge stops hedging.
    history = [_wf(5, evaluated=[(USAGE, "hank")]),
               _wf(6, evaluated=[(USAGE, "hank")])]
    assert _tag("hank", week=7, history=history) == C.EPISODE_NEW


def test_a_flag_outside_the_window_is_new_not_repeat():
    history = [
        _wf(1, evaluated=[(USAGE, "hank")], flagged=[(USAGE, "hank")]),
        _wf(2, evaluated=[(USAGE, "hank")]),
        _wf(3, evaluated=[(USAGE, "hank")]),
        _wf(4, evaluated=[(USAGE, "hank")]),
    ]
    assert _tag("hank", week=5, history=history) == C.EPISODE_NEW


def test_the_same_archive_is_recomputable_under_a_different_rule():
    """§2.3's requirement: the freeze stores the RAW flag history, never only the
    badge, so any other episode rule is recomputable without re-deciding
    anything. That is only true if the rule is a parameter — this is the test
    that would fail if someone stored the badge instead."""
    history = [
        _wf(3, evaluated=[(USAGE, "hank")], flagged=[(USAGE, "hank")]),
        _wf(4, evaluated=[(USAGE, "hank")]),
    ]
    assert _tag("hank", week=5, history=history, gap_weeks=2) == "REPEAT (also wk 3)"
    assert _tag("hank", week=5, history=history, gap_weeks=1) == C.EPISODE_NEW


def test_the_two_arms_keep_separate_episodes():
    """A vacancy and a usage jump are different signals; 'REPEAT' must mean THIS
    one fired again. The injury arm also has no per-player 'not evaluated'
    state — it scans a fixed universe — so every archived week counts for it."""
    history = [_wf(w, evaluated=[(USAGE, "hank")]) for w in (1, 2, 3, 4, 5)]
    history.append(_wf(6, evaluated=[(USAGE, "hank")], flagged=[(INJURY, "hank")]))
    assert _tag("hank", week=7, history=history, kind=INJURY) == "REPEAT (also wk 6)"
    # the usage arm looked at him every week and passed him over: his usage
    # episode is genuinely new, and the injury flag is not evidence about it.
    assert _tag("hank", week=7, history=history, kind=USAGE) == C.EPISODE_NEW


def test_an_injury_episode_expires_on_calendar_weeks():
    history = [
        _wf(1), _wf(2),
        _wf(3, flagged=[(INJURY, "hank")]),
        _wf(4), _wf(5), _wf(6),
    ]
    assert _tag("hank", week=7, history=history, kind=INJURY) == C.EPISODE_NEW
    assert _tag("hank", week=5, history=history, kind=INJURY) == "REPEAT (also wk 3)"


def test_the_rule_is_a_labelled_hypothesis_with_its_measurement():
    """Rule 6: the number that chose this gap, and the fact that nothing was
    graded to choose it, travel with the badge."""
    assert "hypothesis" in C.EPISODE_LABEL
    assert "49%" in C.EPISODE_LABEL and "2025" in C.EPISODE_LABEL
    assert "NOT tuned to outcomes" in C.EPISODE_LABEL
    assert C.EPISODE_LABEL in C.EPISODE_LEGEND


# ============================================ the badge on a real generator run


def _history_fn(weeks, *, calls=None):
    def history(*, season, before_week):
        if calls is not None:
            calls.append((season, before_week))
        return [w for w in weeks if w.week < before_week]
    return history


def test_build_candidates_badges_every_row_and_asks_the_archive_once(db, nfl_fixture):
    from ziggurat.data.nfl import base, players, schedules, snap_counts, weekly_stats
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"),
                                     retrieved_as_of="2026-07-16")
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"),
                                   retrieved_as_of="2026-07-16")

    calls: list = []
    # weeks 1-5 archived and holding no flag for anyone: full coverage, so the
    # NEW badge is a fact rather than a bound.
    archive = [_wf(w) for w in range(1, 6)]
    board = base.latest_truth(C.build_candidates)(
        db, as_of="2023-10-17", season=2023, week=6,
        history=_history_fn(archive, calls=calls))
    assert board.rows, "the fixture week must produce candidates"
    assert calls == [(2023, 6)], "the archive is read ONCE per scan, not per row"
    assert all(r.episode_tag == C.EPISODE_NEW for r in board.rows), \
        "an archive that covers every prior week and holds no flag means NEW"

    # ...and with no history at all, the honest badge, on every row.
    plain = base.latest_truth(C.build_candidates)(
        db, as_of="2023-10-17", season=2023, week=6)
    assert all(r.episode_tag == C.EPISODE_FIRST_SEEN for r in plain.rows)
    # the badge is the ONLY thing that moved: same players, same order, same
    # magnitudes, same reasons.
    assert [(r.player_key, r.signal_kind, r.magnitude, r.reasons) for r in board.rows] == \
           [(r.player_key, r.signal_kind, r.magnitude, r.reasons) for r in plain.rows]


def test_a_broken_archive_degrades_loudly_and_never_takes_the_board_down(db, nfl_fixture):
    from ziggurat.data.nfl import base, players, schedules, snap_counts, weekly_stats
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"),
                                     retrieved_as_of="2026-07-16")
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"),
                                   retrieved_as_of="2026-07-16")

    def broken(*, season, before_week):
        raise OSError("freeze directory is unreadable")

    board = base.latest_truth(C.build_candidates)(
        db, as_of="2023-10-17", season=2023, week=6, history=broken)
    assert board.rows, "the decision survives; only the badge is lost"
    assert all(r.episode_tag == C.EPISODE_FIRST_SEEN for r in board.rows)
    note = [n for n in board.notes if "FIRST SEEN badges UNAVAILABLE" in n]
    assert note, board.notes
    assert "OSError" in note[0]
    assert "not 'nothing has fired before'" in note[0]


def test_week_flags_builds_the_history_with_the_generators_own_keys():
    """The reader's constructor. A second identity rule outside this module is a
    rule that can silently diverge from the one that produced the flags."""
    row = C.CandidateRow(
        player_key="00-0001", player="Hank Invented", position="RB", team="KC",
        gsis_id="00-0001", espn_id="9", signal_kind=USAGE, magnitude=2.0,
        week=6, prior_week=5, hypothesis=False, reasons=("r",))
    board = C.CandidateBoard(rows=(row,), week=6, freshness=(), notes=(),
                             as_of="2023-10-17", season=2023)
    evaluated = C.EvaluatedRow(
        season=2023, week=6, prior_week=5, as_of="2023-10-17", view="historical",
        gsis_id="00-0002", espn_id=None, player="Ivy Invented", position="WR",
        team="KC", deltas={}, levels={}, snap_pct=None, snap_resolved=False,
        path=C.PATH_DIFFERENCED, floors_cleared={}, magnitude=0.0, flagged=False)
    wf = C.week_flags(board, [evaluated])
    assert wf.week == 6
    assert wf.flagged == frozenset({C.episode_key(USAGE, "00-0001")})
    # a flagged key is evaluated by construction, and the passed-over row joins it
    assert wf.evaluated == frozenset({C.episode_key(USAGE, "00-0001"),
                                      C.episode_key(USAGE, "00-0002")})
    # the false negative is a fact the rule can use next week — and with only wk 6
    # in hand the NEW is correctly BOUNDED, while the REPEAT is not (a flag found
    # is positive evidence whatever the coverage).
    assert _tag("00-0002", week=7, history=[wf]).startswith(C.EPISODE_NEW)
    assert "no archive for 5 earlier wk(s)" in _tag("00-0002", week=7, history=[wf])
    assert _tag("00-0001", week=7, history=[wf]) == "REPEAT (also wk 6)"


# ==================================================== the candidates page/block


def _row(kind, player, mag, *, tag="", hyp=False, reasons=("r1",)):
    return C.CandidateRow(
        player_key=player, player=player, position="RB", team="KC",
        gsis_id=player, espn_id=None, signal_kind=kind, magnitude=mag,
        week=9, prior_week=8, hypothesis=hyp, reasons=tuple(reasons),
        episode_tag=tag)


def _board(rows, *, week=9):
    return C.CandidateBoard(rows=tuple(rows), week=week, freshness=(), notes=(),
                            as_of="2025-11-04", season=2025)


def test_the_page_carries_the_banner_and_keeps_the_legend_it_already_had():
    text = C.format_candidates(_board([_row(USAGE, "Hank Invented", 4.0, tag="NEW")]))
    # the line that was already there, verbatim and still first
    legend = "  (SIGNAL = within-block ranking key, higher = stronger; NOT fantasy points)"
    assert legend in text
    lines = text.splitlines()
    i = lines.index(legend)
    # the two banner lines, immediately after it
    assert C.USAGE_EVIDENCE_BANNER in lines[i + 1]
    assert C.EPISODE_LEGEND in lines[i + 2]
    assert "does not change the claim order" in C.USAGE_EVIDENCE_BANNER
    assert "nothing on this page re-orders them" in C.USAGE_EVIDENCE_BANNER


def test_week_one_prints_the_week_one_sentence_instead_of_the_new_repeat_legend():
    text = C.format_candidates(
        _board([_row(USAGE, "Hank Invented", 4.0, tag=C.EPISODE_WEEK1)], week=1))
    assert C.EPISODE_WEEK1_NOTE in text
    assert C.EPISODE_LEGEND not in text     # every badge is the same; it would be noise


def test_the_first_seen_column_ships_and_the_old_headers_survive():
    text = C.format_candidates(_board([
        _row(USAGE, "Hank Invented", 4.0, tag="NEW"),
        _row(USAGE, "Ivy Invented", 2.0, tag="REPEAT (also wk 7)"),
    ]))
    assert "FIRST SEEN" in text
    assert "SIGNAL" in text and "SCORE" not in text     # the F11 pins, unmoved
    assert "NEW" in text and "REPEAT (also wk 7)" in text
    # the badge sits on the row, not in a footnote
    hank = [ln for ln in text.splitlines() if "Hank Invented" in ln][0]
    assert hank.rstrip().endswith("NEW")


def test_the_block_titles_are_extended_and_not_renamed():
    """Five existing string pins match on the leading literal. A rename for a
    wording change would have rotted all five."""
    text = C.format_candidates(_board([
        _row(USAGE, "Hank Invented", 4.0, tag="NEW"),
        _row(INJURY, "Ivy Invented", 2.0, tag="NEW"),
    ]))
    assert "USAGE BREAKOUTS" in text and "INJURY SHOCKS" in text
    assert "QB1-CHANGE HYPOTHESIS" in text
    assert "usage/role evidence" in text
    for title in C._KIND_TITLE.values():
        assert "usage/role evidence" in title


def test_a_hypothesis_row_keeps_its_tag_beside_the_badge():
    text = C.format_candidates(_board([
        _row(C.SIGNAL_QB1, "Quinn Invented", 1.0, tag="NEW", hyp=True)]))
    line = [ln for ln in text.splitlines() if "Quinn Invented" in ln][0]
    assert "NEW" in line and "[HYPOTHESIS]" in line


# ================================================== the waiver evidence bullets


def test_the_header_lands_once_per_claim_above_its_evidence_rows(db, nfl_fixture,
                                                                 monkeypatch):
    """The shape of the block, without needing a full waiver world: one header,
    one legend, then the rows."""
    board = _board([
        _row(USAGE, "Hank Invented", 4.0, tag="NEW"),
        _row(INJURY, "Hank Invented", 2.0, tag="REPEAT (also wk 8)"),
    ])
    board = C.CandidateBoard(
        rows=tuple(C.CandidateRow(**{**r.__dict__, "espn_id": "4242"})
                   for r in board.rows),
        week=9, freshness=(), notes=(), as_of="2025-11-04", season=2025)
    monkeypatch.setattr(waiver, "build_candidates", lambda *a, **k: board)
    notes, err, out = waiver._candidate_notes_by_espn(
        db, as_of="2025-11-04", season=2025, view="historical", today=None)
    assert err is None and out is board
    block = notes["4242"]
    assert block[0] == USAGE_EVIDENCE_HEADER
    assert block[1] == C.EPISODE_LEGEND
    assert block[2] == "  [USAGE_BREAKOUT] NEW: r1"
    assert block[3] == "  [INJURY_SHOCK] REPEAT (also wk 8): r1"
    assert len(block) == 4, "the header is not repeated per row"


def test_the_header_never_promises_the_market(db):
    """The one sentence this relabel exists to forbid, in either dress."""
    banned = ("the market will agree", "the market usually follows",
              "the market will follow", "by Friday")
    for text in (USAGE_EVIDENCE_HEADER, C.USAGE_EVIDENCE_BANNER, C.EPISODE_LEGEND,
                 C.EPISODE_LABEL, C.EPISODE_WEEK1_NOTE, BRIEFING_SYSTEM):
        low = text.lower()
        for phrase in banned:
            assert phrase not in low, f"{phrase!r} is back in: {text[:60]}"
    assert "not a forecast, not a probability, not a tie-break" in USAGE_EVIDENCE_HEADER
    assert "does not change the claim order" in USAGE_EVIDENCE_HEADER


def test_the_week_one_block_explains_the_week_one_badge(db, monkeypatch):
    board = C.CandidateBoard(
        rows=(C.CandidateRow(
            player_key="k", player="Hank Invented", position="RB", team="KC",
            gsis_id="k", espn_id="4242", signal_kind=USAGE, magnitude=3.0,
            week=1, prior_week=None, hypothesis=False, reasons=("r1",),
            episode_tag=C.EPISODE_WEEK1),),
        week=1, freshness=(), notes=(), as_of="2026-09-15", season=2026)
    monkeypatch.setattr(waiver, "build_candidates", lambda *a, **k: board)
    notes, _, _ = waiver._candidate_notes_by_espn(
        db, as_of="2026-09-15", season=2026, view="historical", today=None)
    assert notes["4242"][1] == C.EPISODE_WEEK1_NOTE


# ======================================================== the order-inertness pin


def test_the_evidence_column_cannot_reorder_the_chain(db, marginal_world, nfl_fixture):
    """The header says the claim order does not use this column. Prove it: run the
    SAME world twice, once with a full opportunity-signal map and once with none,
    and require every economic output to be identical — the claims, their order,
    their gains, the joint total, the refusals and the stop reason. Only the
    reason TEXT may differ.

    This is stronger than checking that ``_select_claims`` is not handed the
    notes (it is handed them — it renders them into the reasons of rows it has
    already chosen), and it is the thing that would actually break if someone
    ever promoted the column to a tie-break.
    """
    from ziggurat.data.nfl import players, schedules, snap_counts, weekly_stats
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"),
                                     retrieved_as_of="2023-10-10")
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"),
                                   retrieved_as_of="2023-10-10")
    rb_espn = db.execute(
        "SELECT espn_id FROM players WHERE gsis_id='00-0035250'").fetchone()["espn_id"]

    from tests.test_waiver import _POOL_SPECS, TEAM, _active_specs
    specs = [{**s, "on_team": TEAM} for s in _active_specs()] + list(_POOL_SPECS)
    specs.append({"name": "Breakout FA", "pos": "RB", "team": "BUF", "pts": 30.0, "bye": 13})
    marginal_world(specs, season=2023, retrieved="2023-10-10")
    db.execute("UPDATE league_player_state SET espn_player_id=? WHERE player='Breakout FA'",
               (rb_espn,))
    db.commit()

    def _run():
        return build_waiver_plan(db, as_of="2023-10-17", season=2023, own_team_id=TEAM,
                                 weeks=range(7, 18), pool_limit=None, claim_budget=10,
                                 view="latest_truth")

    with_notes = _run()
    with pytest.MonkeyPatch.context() as mp:
        # the column is empty — nothing else about the run changes
        mp.setattr(waiver, "_candidate_notes_by_espn",
                   lambda *a, **k: ({}, None, None))
        without = _run()

    def _economics(plan):
        return (
            tuple((c.chain_rank, c.add, c.add_espn_id, c.drop, c.drop_espn_id,
                   round(c.gain, 9), round(c.gain_alone, 9), c.kind)
                  for c in list(plan.claims) + list(plan.fcfs_grabs) + list(plan.streaming)),
            round(plan.chain_gain, 9),
            tuple((r.add, r.drop) for r in plan.chain_rejected),
            plan.chain_stop,
        )

    assert _economics(with_notes) == _economics(without)
    # ...and the test is not vacuous: the column really was populated.
    populated = [c for c in with_notes.claims + with_notes.fcfs_grabs
                 if any(r == USAGE_EVIDENCE_HEADER for r in c.reasons)]
    assert populated, "no claim carried the evidence block — the pin would be empty"


# ============================================== the briefing prompt's first pin


def test_the_briefing_prompt_forbids_promoting_a_signal_row():
    """``BRIEFING_SYSTEM`` is the highest-leverage string in the push layer and
    had ZERO tests. It instructs the model to 'lead with the single most urgent
    action', which without this clause is an open invitation to turn a SIGNALS
    row into one."""
    assert "SIGNALS" in BRIEFING_SYSTEM
    assert "USAGE / ROLE EVIDENCE" in BRIEFING_SYSTEM
    assert "context, not a recommendation" in BRIEFING_SYSTEM
    assert "never turn a SIGNALS row into an action" in BRIEFING_SYSTEM
    assert "never re-order or re-weight the waiver claim chain" in BRIEFING_SYSTEM
    assert "claim order comes from the WAIVERS section only" in BRIEFING_SYSTEM
    # the instruction it exists to fence is still there (this is an addition, not
    # a rewrite — the prose the operator reads every Wednesday did not change)
    assert "lead with the single most urgent action" in BRIEFING_SYSTEM
    assert "invent nothing, drop nothing load-bearing" in BRIEFING_SYSTEM


def test_the_briefing_signals_block_is_the_same_renderer_as_the_cli_page():
    """One edit had to reach both surfaces. It does because the briefing renders
    ``format_candidates`` — pinned here so a future split does not quietly leave
    the phone briefing with the old wording."""
    src = (REPO_ROOT / "ziggurat" / "core" / "briefing.py").read_text()
    assert re.search(r"format_candidates\(board, top=8\)", src)


# ========================================================= the journal template


def test_the_journal_block_records_what_the_operator_actually_did():
    """Item 4.2b §2.4(i). The freeze records what the TOOL printed; only this
    block records what was submitted, and ESPN stamps a won claim and a
    self-made grab identically."""
    body = (REPO_ROOT / "templates" / "intel" / "weekly" / "week-TEMPLATE.md").read_text()
    assert "## Submitted claims & departures (Tuesday)" in body
    # both ids, because two players share a display name (the 3.4 audit's mis-join)
    assert "add espn_id" in body and "drop espn_id" in body
    # both gain columns, because a bare `gain` means different things either side
    # of 2026-09-02
    assert "| gain | gain_alone |" in body
    assert "capture_id" in body
    assert "Submitted in the app at" in body
    for kind in ("printed but NOT submitted", "submitted but NOT printed",
                 "order changed"):
        assert kind in body, f"the journal must name the {kind!r} departure"
    assert "Did USAGE / ROLE EVIDENCE change anything?" in body
    # the stop sentence is QUOTED, not summarised, and classified
    assert "Why the list ended, quoted" in body
    assert "VERDICT" in body and "BOOKKEEPING" in body
    # and the block sits between the decision log and the Sunday swaps
    assert body.index("## Decision log") < body.index("## Submitted claims") \
        < body.index("## Sunday late swaps")


def test_the_journal_block_quotes_only_sentences_the_tools_still_print():
    """The doc-to-code link the cadence tests already enforce for CLAUDE.md,
    applied to the template: a quoted output string that no module prints any
    more is a rot the whole suite would otherwise miss."""
    body = (REPO_ROOT / "templates" / "intel" / "weekly" / "week-TEMPLATE.md").read_text()
    waiver_src = re.sub(r'"\s*\n\s*f?"', "",
                        (REPO_ROOT / "ziggurat" / "core" / "waiver.py").read_text())
    for fragment in ("IF EVERY CLAIM AND GRAB LISTED WINS",):
        assert fragment in body and fragment in waiver_src
    assert "USAGE / ROLE EVIDENCE" in body
    assert "USAGE / ROLE EVIDENCE" in waiver_src
