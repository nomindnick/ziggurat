"""Simulator tests: snake order, determinism, legality at scale, summaries,
and the DB-edge board loader (item 2.2). Synthetic boards only (Rule 5)."""

import json
import random
import time
from collections import Counter
from pathlib import Path

import pytest

from ziggurat.core.valuation import DEFAULT_ROSTER as ROSTER
from ziggurat.data.nfl import projections
from ziggurat.data.nfl.espn_ranks import get_espn_draft_ranks, ingest_espn_ranks
from ziggurat.data.store import apply_schema, connect
from ziggurat.draft.bots import (
    BoardEntry,
    FollowEspnRank,
    FollowVor,
    PickContext,
    RankNoiseBot,
    min_to_complete,
    position_counts,
)
from ziggurat.draft.priors import ROOM_PRIORS_2025
from ziggurat.draft.simulator import (
    ROUNDS,
    format_strategy_summary,
    load_board,
    run_draft,
    run_many,
    snake_sequence,
)

_VAL_FIXTURE = Path(__file__).parent / "fixtures" / "nfl" / "valuation_projections_sample.json"
_ESPN_FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "player_universe.json"


def _roster_is_legal(entries):
    """A finished roster covers all nine starters (min_to_complete == 0)."""
    return min_to_complete(position_counts(entries), ROSTER) == 0


# --------------------------------------------------------------- snake order


def test_snake_sequence_hand_computed():
    # 3 teams, 3 rounds: forward, reversed, forward.
    assert snake_sequence([0, 1, 2], 3) == [0, 1, 2, 2, 1, 0, 0, 1, 2]


def test_snake_sequence_honors_a_custom_pick_order():
    assert snake_sequence([2, 0, 1], 2) == [2, 0, 1, 1, 0, 2]


def test_run_draft_rejects_bad_pick_order(make_draft_board):
    board = make_draft_board()
    pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
    with pytest.raises(ValueError):
        run_draft(board, pickers, rng=random.Random(0), pick_order=[0, 0, 1, 2, 3, 4, 5, 6, 7, 8])


# ---------------------------------------------------------------- determinism


def test_run_draft_is_deterministic(make_draft_board):
    board = make_draft_board()

    def one():
        pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
        return run_draft(board, pickers, rng=random.Random(99)).pick_log

    assert one() == one()


def test_run_many_same_seed_same_summary(make_draft_board):
    board = make_draft_board()
    a = run_many(board, n=25, operator_slot=2, strategy=FollowVor(), seed=7)
    b = run_many(board, n=25, operator_slot=2, strategy=FollowVor(), seed=7)
    assert a == b


def test_run_many_autodraft_count_is_reproducible(make_draft_board):
    board = make_draft_board()
    a = run_many(board, n=20, operator_slot=0, strategy=FollowEspnRank(),
                 seed=3, autodraft_count=3)
    b = run_many(board, n=20, operator_slot=0, strategy=FollowEspnRank(),
                 seed=3, autodraft_count=3)
    assert a == b


# --------------------------------------------------------- legality at scale


def test_every_finished_roster_is_legal(make_draft_board):
    board = make_draft_board()
    pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
    result = run_draft(board, pickers, rng=random.Random(1))
    for team, entries in result.rosters.items():
        assert len(entries) == ROUNDS
        assert _roster_is_legal(entries), f"team {team} illegal: {position_counts(entries)}"


def test_kdst_scarce_board_distributes_one_each(make_draft_board):
    # Exactly 10 DST and 10 K for 10 teams: legality + the DST/K cap of 1 must
    # hand every team exactly one of each, with none starved.
    board = make_draft_board(dst=10, k=10)
    pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
    result = run_draft(board, pickers, rng=random.Random(2))
    for entries in result.rosters.values():
        c = Counter(e.position for e in entries)
        assert c["DST"] == 1 and c["K"] == 1
        assert _roster_is_legal(entries)


def test_thousand_drafts_from_every_slot_legal_and_fast(make_draft_board):
    board = make_draft_board()
    start = time.perf_counter()
    for slot in range(ROSTER.teams):  # 1..10 (0-based here)
        summary = run_many(board, n=1000, operator_slot=slot, strategy=FollowVor(),
                           priors=ROOM_PRIORS_2025, seed=slot)
        # every operator roster fills a legal starting lineup: DST/K always 1.
        assert summary.position_counts_mean["DST"] == pytest.approx(1.0)
        assert summary.position_counts_mean["K"] == pytest.approx(1.0)
        assert summary.operator_slot == slot + 1
    elapsed = time.perf_counter() - start
    assert elapsed < 60.0, f"10x1000 drafts took {elapsed:.1f}s (budget 60s)"


# ------------------------------------------------------------------ summaries


def test_summary_distribution_is_ordered_and_shaped(make_draft_board):
    board = make_draft_board()
    s = run_many(board, n=200, operator_slot=4, strategy=FollowVor(), seed=1)
    assert s.n == 200
    assert s.points_min <= s.points_p10 <= s.points_p50 <= s.points_p90 <= s.points_max
    assert s.points_min <= s.points_mean <= s.points_max
    # operator drafts 16 players across the tracked positions.
    assert sum(s.position_counts_mean.values()) == pytest.approx(ROUNDS)
    text = format_strategy_summary(s)
    assert "starting-lineup points" in text and "median" in text
    assert "draft slot 5" in text


def test_strategies_diverge_but_both_stay_legal(make_draft_board):
    # The two operator baselines draft off different signals (VOR desc vs ESPN
    # rank asc), so they produce different rosters — but both must be legal (a
    # full starting lineup, exactly one DST and one K). Note: naive best-available
    # VOR intentionally over-drafts the deepest position (that roster-need
    # weighting is the 2.3 engine's job), so it need NOT beat ESPN-follow on raw
    # starting-lineup points — only differ and stay legal.
    board = make_draft_board()
    vor = run_many(board, n=120, operator_slot=3, strategy=FollowVor(), seed=11)
    espn = run_many(board, n=120, operator_slot=3, strategy=FollowEspnRank(), seed=11)
    assert vor.position_counts_mean["DST"] == pytest.approx(1.0)
    assert vor.position_counts_mean["K"] == pytest.approx(1.0)
    assert espn.position_counts_mean["DST"] == pytest.approx(1.0)
    assert espn.position_counts_mean["K"] == pytest.approx(1.0)
    # different signals -> different roster shape or point profile
    assert vor.position_counts_mean != espn.position_counts_mean


# -------------------------------------------- opponent_rosters population (2.3)


class _OppRosterProbe:
    """Records that ``run_draft`` populates ``opponent_rosters`` correctly on every
    pick: the own seat is excluded, all rivals are present, and the drafted-so-far
    invariant holds (own + all rivals == overall_pick - 1). Delegates the actual
    pick to FollowEspnRank so the draft still completes."""

    def __init__(self):
        self.checks = 0
        self._delegate = FollowEspnRank()

    def pick(self, ctx: PickContext) -> str:
        teams = ctx.roster.teams
        assert ctx.team_slot not in ctx.opponent_rosters, "own seat must be excluded"
        assert set(ctx.opponent_rosters) == set(range(teams)) - {ctx.team_slot}
        opp_total = sum(len(r) for r in ctx.opponent_rosters.values())
        assert opp_total + len(ctx.own_roster) == ctx.overall_pick - 1
        # read-only view (same convention as own_roster)
        with pytest.raises(TypeError):
            ctx.opponent_rosters[ctx.team_slot] = ()
        self.checks += 1
        return self._delegate.pick(ctx)


def test_run_draft_populates_opponent_rosters(make_draft_board):
    board = make_draft_board()
    probe = _OppRosterProbe()
    pickers = [probe] + [RankNoiseBot() for _ in range(ROSTER.teams - 1)]
    run_draft(board, pickers, rng=random.Random(5))
    assert probe.checks == ROUNDS  # the probe seat was on the clock every round


# --------------------------------------------------------------- DB edge loader


def _build_board_db(db_path):
    """Projections + players (so build_valuation works) + an ESPN board snapshot."""
    conn = connect(db_path)
    apply_schema(conn)
    for gsis, sleeper, espn_id, name in [
        ("00-QB", "100", "3918298", "Test QB"),   # Josh Allen espn id -> board rank 36
        ("00-R1", "201", "e201", "Test RB1"),
        ("00-WR", "301", "e301", "Test WR"),
    ]:
        conn.execute(
            "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, retrieved_as_of, knowable_as_of) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (gsis, sleeper, espn_id, name, "2026-07-01", "2026-07-01"),
        )
    conn.commit()
    projections.ingest_projections(conn, json.loads(_VAL_FIXTURE.read_text()),
                                   retrieved_as_of="2026-08-01")
    ingest_espn_ranks(conn, json.loads(_ESPN_FIXTURE.read_text()),
                      retrieved_as_of="2026-08-01", season=2026)
    return conn


def test_load_board_joins_espn_rank(tmp_path):
    conn = _build_board_db(tmp_path / "board.sqlite")
    board = load_board(conn, as_of="2026-08-01", season=2026)
    conn.close()
    assert board, "expected a non-empty board"
    by_name = {e.name: e for e in board}
    assert "Test QB" in by_name
    # Josh Allen's editorial PPR rank in the fixture is 36 -> joined onto the QB.
    assert by_name["Test QB"].espn_overall_rank == 36
    # every entry is a canonical league position with finite numbers.
    for e in board:
        assert e.position in ("QB", "RB", "WR", "TE", "DST", "K")
        assert e.espn_overall_rank >= 1


def test_load_board_requires_explicit_as_of(tmp_path):
    conn = _build_board_db(tmp_path / "board.sqlite")
    with pytest.raises(TypeError):
        load_board(conn, season=2026)  # no as_of -> library never assumes "now"
    conn.close()


def test_load_board_leakage_by_retrieval(tmp_path):
    """Rule 1 on the sim's one DB seam: nothing retrieved after ``as_of`` is
    visible, and a later correction never rewrites what an earlier as_of saw
    (mirrors test_espn_ranks.test_leakage_by_retrieval)."""
    conn = _build_board_db(tmp_path / "board.sqlite")  # everything retrieved 2026-08-01
    assert load_board(conn, as_of="2026-07-31", season=2026) == ()
    board = load_board(conn, as_of="2026-08-01", season=2026)
    assert board
    assert {e.name: e.espn_overall_rank for e in board}["Test QB"] == 36

    # A later re-pull corrects the QB's editorial rank; the old as_of still
    # serves the rank as it was known then.
    raw = json.loads(_ESPN_FIXTURE.read_text())
    for pl in raw:
        if pl["id"] == 3918298:
            pl["draftRanksByRankType"]["PPR"]["rank"] = 99
    ingest_espn_ranks(conn, raw, retrieved_as_of="2026-08-15", season=2026)
    old = {e.name: e.espn_overall_rank for e in load_board(conn, as_of="2026-08-01", season=2026)}
    new = {e.name: e.espn_overall_rank for e in load_board(conn, as_of="2026-08-15", season=2026)}
    conn.close()
    assert old["Test QB"] == 36
    assert new["Test QB"] == 99


# ----------------------------------------------------------- thin-board failure


def test_short_board_rejected_up_front(make_draft_board):
    pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
    with pytest.raises(ValueError, match="needs at least 160"):
        run_draft(make_draft_board(qb=10, rb=40, wr=40, te=10, dst=10, k=10)[:150],
                  pickers, rng=random.Random(0))
    with pytest.raises(ValueError, match="short on DST"):
        run_draft(make_draft_board(dst=5), pickers, rng=random.Random(0))


def test_thin_board_fails_loud_never_silently_illegal(make_draft_board):
    """The audit's repro: 160 players with >= 10 of each position, but flex/bench
    drain the 10-deep TE pool. The guarantee is not that every draft completes —
    it is that a stranded draft raises instead of silently scoring an illegal or
    cap-busting roster."""
    board = make_draft_board(qb=10, rb=55, wr=55, te=10, dst=10, k=20)
    for seed in range(8):
        pickers = [RankNoiseBot() for _ in range(ROSTER.teams)]
        try:
            result = run_draft(board, pickers, rng=random.Random(seed))
        except RuntimeError:
            continue  # loud failure is the acceptable outcome
        for entries in result.rosters.values():
            counts = position_counts(entries)
            assert min_to_complete(counts, ROSTER) == 0
            assert counts.get("K", 0) <= 1
            assert counts.get("DST", 0) <= 1


def test_load_board_unions_the_full_espn_universe(tmp_path):
    # Dress-rehearsal finding (2026-07-24): ESPN can draft players the
    # projections board has never heard of (deep rookie kickers), and an
    # off-board pick cannot be ENTERED — damming the sync feed. Every
    # ESPN-universe skill player must be on the board, zero-valued when the
    # house has no projection for him.
    conn = _build_board_db(tmp_path / "board.sqlite")
    board = load_board(conn, as_of="2026-08-01", season=2026)
    espn = get_espn_draft_ranks(conn, as_of="2026-08-01", season=2026)
    conn.close()

    ids = {e.player_id for e in board}
    for r in espn:
        if r["espn_id"] is not None:
            assert str(r["espn_id"]) in ids, f"{r['player']} missing from board"
    # union entries carry zero house value and never a fabricated projection
    extras = [e for e in board if e.house_points == 0.0]
    assert extras, "expected at least one ESPN-only union entry in the fixture"
    for e in extras:
        assert e.position in ("QB", "RB", "WR", "TE", "DST", "K")
        assert e.espn_overall_rank >= 1


def test_union_entries_are_floored_below_every_priced_player(tmp_path):
    # An ABSENT projection is not a measurement of replacement level, and the
    # difference is not cosmetic. Union entries used to land at ``vor 0.0``,
    # which outranks every priced player whose vor is NEGATIVE — and past
    # roughly round 11 that is all of them, because replacement level sits at
    # the last starter (only ~90 of the live board's 3,263 entries are
    # positive). Measured on the real board 2026-08-27, from slot 9: 46 of 60
    # picks in rounds 12-16 went to players with NO projection at all, over
    # Chris Godwin (174 house pts) and RJ Harvey (165). The path is the
    # engine's "single best by VOR" candidate slot, which a zero wins outright,
    # so the invariant belongs here on the board rather than on the engine.
    conn = _build_board_db(tmp_path / "board.sqlite")
    board = load_board(conn, as_of="2026-08-01", season=2026)
    conn.close()

    priced = [e for e in board if e.house_points]
    unpriced = [e for e in board if not e.house_points]
    assert priced and unpriced, "fixture must carry both priced and union entries"
    assert max(e.vor for e in unpriced) < min(e.vor for e in priced), (
        "an unpriced union entry must never outrank a priced player by VOR"
    )


def test_espn_display_names_serves_the_other_sides_vocabulary(tmp_path):
    # Auto-entry audit (major): the queue writer searches ESPN's DOM by ESPN's
    # OWN display text — "Josh Allen" may be the house name while ESPN renders
    # a variant, and DST is nflverse "HOU D/ST" vs ESPN "Texans D/ST". The map
    # must serve ESPN's text keyed by the BOARD's player_id (skill by espn_id,
    # DST by team), same joins and as-of threading as load_board itself.
    from ziggurat.draft.simulator import espn_display_names

    conn = _build_board_db(tmp_path / "names.sqlite")
    board = load_board(conn, as_of="2026-08-01", season=2026)
    names = espn_display_names(conn, board, as_of="2026-08-01", season=2026)

    # Skill join by espn_id: the players-table name is "Test QB", but the map
    # serves what ESPN's board says for espn id 3918298.
    assert names["3918298"] == "Josh Allen"
    # DST join by team: hand the function a board entry for a team the ESPN
    # fixture carries (HOU) and it must serve ESPN's nickname form.
    dst_entry = BoardEntry("DST:HOU", "HOU D/ST", "DST", 236, 0.0, 0.0, "HOU")
    dst_names = espn_display_names(
        conn, [dst_entry], as_of="2026-08-01", season=2026
    )
    assert dst_names["DST:HOU"] == "Texans D/ST"
    # Rule 1: the map is as-of-gated exactly like the board.
    assert espn_display_names(conn, board, as_of="2026-07-31", season=2026) == {}
    conn.close()


# ------------------------------------------------- the draft-night input bundle
#
# Item 3.11. ``load_draft_board`` is the one read the cockpit makes: the board,
# the week-by-week objective the composed engine re-ranks with, and the kicker
# correction — all at ONE ``as_of``, because a decision board and the objective
# that re-ranks it must agree about who a player is.


def test_load_draft_board_reads_the_board_and_the_objective_at_one_as_of(tmp_path):
    from ziggurat.draft.simulator import load_draft_board

    conn = _build_board_db(tmp_path / "bundle.sqlite")
    inputs = load_draft_board(conn, as_of="2026-08-01", season=2026)
    conn.close()
    assert inputs.board
    assert inputs.weekly is not None
    # The two id spaces are the SAME one: a diverged map would grade every
    # candidate as a season of holes and read exactly like a real answer.
    from ziggurat.draft.grader import assert_board_coverage

    assert_board_coverage(inputs.board, inputs.weekly)


def test_load_draft_board_is_the_legacy_cockpit_when_asked(tmp_path):
    """``--legacy-engine``: no objective, no correction, and it SAYS so.

    An escape hatch that silently differs from the engine it claims to restore
    is worse than none, so the note is asserted along with the absence.
    """
    from ziggurat.draft.simulator import load_draft_board

    conn = _build_board_db(tmp_path / "legacy.sqlite")
    inputs = load_draft_board(conn, as_of="2026-08-01", season=2026, legacy=True)
    plain = load_board(conn, as_of="2026-08-01", season=2026)
    conn.close()
    assert inputs.weekly is None and inputs.kicker_board is None
    assert inputs.board == plain, "the legacy board must be the untouched load_board"
    assert any("--legacy-engine" in n for n in inputs.notes)


def test_load_draft_board_degrades_LOUDLY_when_the_kicker_source_is_absent(tmp_path):
    """The live 2026-08-31 situation, pinned.

    ``espn_projections`` ships empty and has never been pulled on the draft box,
    so the correction cannot be served. At 18:45 the cost of refusing to start is
    total and the cost of drafting on the engine that ran four rehearsals is
    zero — so this degrades rather than raises. But it must degrade in WORDS the
    operator reads, because the K board he then drafts off is known to be
    misordered (item 3.10), and a silent degrade is the Rule-1-invisible failure
    this repo keeps finding.
    """
    from ziggurat.draft.simulator import load_draft_board

    conn = _build_board_db(tmp_path / "nokicker.sqlite")
    inputs = load_draft_board(conn, as_of="2026-08-01", season=2026)
    conn.close()
    assert inputs.kicker_board is None
    assert inputs.board, "an absent correction must never cost us the board"
    assert inputs.weekly is not None, "nor the objective"
    note = " ".join(inputs.notes)
    assert "KICKER BOARD: uncorrected" in note
    assert "espn_projections" in note, "the note must name what is missing"


def test_load_board_splices_a_kicker_board_when_it_is_given_one(tmp_path, monkeypatch):
    """The seam itself: a board handed in is applied to the valuation rows.

    ``apply_to_valuation``'s own behaviour (re-pricing, re-ranking, moving
    replacement level) is covered exhaustively in ``tests/test_kicker_board.py``;
    what is unproven anywhere else is that ``load_board`` actually CALLS it, with
    the rows, before the ESPN join. A no-op splice is the exact failure the
    kicker module documents flipping the sign of its own evidence.
    """
    import ziggurat.core.kicker_board as kbm

    seen = {}

    def spy(rows, board, **kw):
        seen["rows"] = len(rows)
        seen["board"] = board
        return list(rows)

    monkeypatch.setattr(kbm, "apply_to_valuation", spy)
    conn = _build_board_db(tmp_path / "splice.sqlite")
    sentinel = object()
    load_board(conn, as_of="2026-08-01", season=2026, kicker_board=sentinel)
    conn.close()
    assert seen["board"] is sentinel
    assert seen["rows"] > 0


def test_load_board_without_a_kicker_board_is_the_pre_3_11_board(tmp_path, monkeypatch):
    """The default is unchanged, and provably so: the correction is never even
    imported. ``load_board`` is also the harness's board loader, and an
    experiment must be able to load the uncorrected board on purpose."""
    import ziggurat.core.kicker_board as kbm

    def explode(*a, **k):  # pragma: no cover - the assertion is that this never runs
        raise AssertionError("load_board applied a kicker correction it was not given")

    monkeypatch.setattr(kbm, "apply_to_valuation", explode)
    conn = _build_board_db(tmp_path / "plain.sqlite")
    assert load_board(conn, as_of="2026-08-01", season=2026)
    conn.close()


# =====================================================================
#  item 3.11 audit fixes: the launch cost, and the OTHER half of the
#  "degrade, don't crash" promise
# =====================================================================


def test_load_draft_board_reads_the_projections_table_exactly_once(tmp_path, monkeypatch):
    """The composed launch must not pay for THREE full passes (audit finding 1).

    ``build_kicker_board``, ``build_valuation`` (via ``load_board``) and
    ``grader.weekly_points_map`` each used to call ``valuation.weekly_lines``
    themselves. Measured on the live 3,264-row board that pass is 3.55 s, so the
    composed cockpit printed nothing for ~11.5 s on an idle box and ~23.6 s under
    load — on every launch AND on every crash-resume, which is the moment
    ``docs/draft-day-runbook.md`` §6 calls the dangerous one. Counted rather than
    timed, because a wall clock on CI measures the box.
    """
    import ziggurat.core.valuation as val
    from ziggurat.draft.simulator import load_draft_board

    calls = []
    real = val.weekly_lines

    def counting(*a, **kw):
        calls.append(kw.get("weeks"))
        return real(*a, **kw)

    monkeypatch.setattr(val, "weekly_lines", counting)
    conn = _build_board_db(tmp_path / "once.sqlite")
    try:
        inputs = load_draft_board(conn, as_of="2026-08-01", season=2026)
    finally:
        conn.close()
    assert inputs.board and inputs.weekly is not None
    assert len(calls) == 1, (
        f"load_draft_board made {len(calls)} passes over the projections table; "
        "the whole launch shares ONE (see the `lines=` hand-over)"
    )


def test_load_draft_board_degrades_LOUDLY_when_the_OBJECTIVE_cannot_be_built(
    tmp_path, monkeypatch
):
    """Audit finding 2: the docstring's promise, applied to the week-by-week half.

    ``load_draft_board`` promises an unavailable improvement 'degrades to the
    legacy path — but LOUDLY, in notes' because 'a crash would be worse than the
    thing it is protecting against'. That was implemented for the kicker board
    only: a ``GradeInputError`` out of ``weekly_points_map`` propagated through
    ``cli._resolve_draft_launch`` as a bare traceback that killed the launch at
    18:45 and named no fallback — while ``EngineProfileMismatch``, the OTHER
    launch-time refusal, gets one clean sentence naming the flag.
    """
    from ziggurat.draft import grader
    from ziggurat.draft.simulator import load_draft_board

    def boom(*a, **kw):
        raise grader.GradeInputError("injected id-space divergence")

    monkeypatch.setattr(grader, "weekly_points_map", boom)
    conn = _build_board_db(tmp_path / "nograde.sqlite")
    try:
        inputs = load_draft_board(conn, as_of="2026-08-01", season=2026)
    finally:
        conn.close()
    assert inputs.board, "a failed objective must never cost us the board"
    assert inputs.weekly is None, "and must leave the session on the legacy engine"
    note = " ".join(inputs.notes)
    assert "week-by-week re-rank UNAVAILABLE" in note
    assert "injected id-space divergence" in note, "the note must name the cause"


def test_a_non_default_week_window_does_not_crash_the_default_engine(tmp_path):
    """``--weeks`` is a documented flag that is NOT on the runbook's forbidden list.

    It used to hard-crash the default engine at launch: ``load_draft_board``
    forwarded ``weeks`` to ``weekly_points_map(weeks=)`` but not to
    ``rank_weeks=``, so the board's ``<POS>:<rank>`` id space was derived over
    weeks 1-17 while the map's was derived over the requested span — and
    ``assert_board_coverage`` (correctly) refused. Now both halves are built over
    the SAME span, which is also what makes the single ``weekly_lines`` pass sound.
    """
    from ziggurat.draft.simulator import load_draft_board

    conn = _build_board_db(tmp_path / "weeks.sqlite")
    try:
        inputs = load_draft_board(
            conn, as_of="2026-08-01", season=2026, weeks=range(1, 15)
        )
    finally:
        conn.close()
    assert inputs.board
    assert inputs.weekly is not None, (
        "a narrower --weeks window must still produce an objective, not a "
        "traceback (or a silent fall back to legacy)"
    )
    assert not any("UNAVAILABLE" in n for n in inputs.notes)


def test_the_launch_notes_say_which_engine_is_on_the_clock(tmp_path):
    """The DEFAULT engine never announced itself; only --legacy-engine printed a line.

    The operator cannot confirm at 18:45 which engine is about to draft for him if
    the only positive signal is the one he gets by passing the escape-hatch flag
    (audit minor). Both paths now say so, in the notes both front-ends print and
    the web cockpit renders on the page.
    """
    from ziggurat.draft.simulator import load_draft_board

    conn = _build_board_db(tmp_path / "engnote.sqlite")
    try:
        default = load_draft_board(conn, as_of="2026-08-01", season=2026)
        legacy = load_draft_board(conn, as_of="2026-08-01", season=2026, legacy=True)
    finally:
        conn.close()
    assert any(n.startswith("ENGINE: default") for n in default.notes)
    assert any("--legacy-engine" in n for n in legacy.notes)


def test_draft_inputs_report_whether_the_kicker_board_was_corrected(tmp_path):
    """``kicker_corrected`` is what puts the item-3.10 caveat on the K PANEL.

    Rule 6 puts the burden on the recommendation, not on a terminal line printed
    three hours before the round-10 kicker pick (audit finding 5). The launcher
    reads this flag to register ``DraftSession.rec_caveats['K']``.
    """
    from ziggurat.draft.simulator import load_draft_board

    conn = _build_board_db(tmp_path / "kflag.sqlite")
    try:
        inputs = load_draft_board(conn, as_of="2026-08-01", season=2026)
    finally:
        conn.close()
    assert inputs.kicker_board is None
    assert inputs.kicker_corrected is False
