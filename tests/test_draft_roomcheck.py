"""Tests for the survival-model room check (``ziggurat/draft/roomcheck.py``).

All offline and entirely synthetic (Rule 5): journals are generated here by
running the shipped simulator over the synthetic ``make_draft_board`` fixture,
so no real player, no real manager and no real league state enters a committed
file. The one DB test uses the in-memory ``db`` fixture.

The tests are aimed at the two classes of defect this module can actually have:

* a RECONSTRUCTION defect — the replayed board state is not the state the
  cockpit held — which produces confident, plausible, wrong calibration numbers
  with nothing raising. Both such bugs occurred during the build and both have a
  test here that fails if the fix is reverted;
* an ESTIMATOR defect — censoring dropped, the operator's own picks counted as
  room behaviour, a bias sign flipped — which silently changes what the module
  concludes.
"""

import dataclasses
import inspect
import json
import math
import random
from dataclasses import dataclass

import pytest

from ziggurat.core.valuation import RosterStructure
from ziggurat.draft import roomcheck
from ziggurat.draft.bots import (
    POSITIONS,
    AutodraftBot,
    BoardEntry,
    allowed_positions,
    position_counts,
)
from ziggurat.draft.priors import ROOM_PRIORS_2025
from ziggurat.draft.session import _board_hash
from ziggurat.draft.simulator import run_draft, snake_sequence
from ziggurat.draft.survival import DEFAULT_SURVIVAL_PARAMS, SurvivalParams

ROSTER = RosterStructure()
TEAMS = ROSTER.teams
ROUNDS = 16
AS_OF = "2026-08-01"


# ------------------------------------------------------------- journal factory


@dataclass
class _OffsetBot:
    """Takes the ``offset``-th best legal player by ESPN rank, every time.

    ``offset=0`` is exactly :class:`AutodraftBot`'s choice in the unconstrained
    case; a larger offset is a seat that consistently reaches past the board.
    """

    offset: int

    def pick(self, ctx):
        counts = position_counts(ctx.own_roster)
        allowed = allowed_positions(counts, ctx.picks_after, ctx.roster) or set(POSITIONS)
        window = ctx.state.window_by_rank(sorted(allowed), self.offset + 1)
        return window[min(self.offset, len(window) - 1)].player_id


@dataclass
class _GrabPositionAtRound:
    """Autodrafts, except it grabs the best available ``position`` in one round.

    Stands in for the operator seat: the engine's whole K/DST divergence play is
    to take a defense five rounds before the room, and several tests need a
    journal in which exactly that happened.
    """

    position: str
    round: int

    def pick(self, ctx):
        if ctx.round == self.round:
            entry = ctx.state.front_rank(self.position)
            if entry is not None:
                return entry.player_id
        return AutodraftBot().pick(ctx)


def _make_journal_file(tmp_path, board, pickers, *, operator_slot, name="draft.jsonl",
                       as_of=AS_OF, season=2026, seed=7):
    """Run a real snake draft and write it out as a cockpit journal."""
    result = run_draft(
        board,
        list(pickers),
        rng=random.Random(seed),
        roster=ROSTER,
        rounds=ROUNDS,
    )
    by_id = {e.player_id: e for e in board}
    header = {
        "kind": "header",
        "season": season,
        "as_of": as_of,
        "operator_slot": operator_slot,
        "pick_order": list(range(TEAMS)),
        "session_seed": 42,
        "rollouts": 128,
        "rounds": ROUNDS,
        "roster": {
            "teams": ROSTER.teams,
            "starters": dict(ROSTER.starters),
            "flex_slots": ROSTER.flex_slots,
            "flex_positions": sorted(ROSTER.flex_positions),
        },
        "board_count": len(board),
        "board_hash": _board_hash(board),
    }
    lines = [json.dumps(header, sort_keys=True)]
    for overall, seat, pid in result.pick_log:
        lines.append(
            json.dumps(
                {
                    "kind": "pick",
                    "overall": overall,
                    "seat": seat,
                    "player_id": pid,
                    "name": by_id[pid].name,
                }
            )
        )
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def board(make_draft_board):
    return make_draft_board()


@pytest.fixture()
def autodraft_journal(tmp_path, board):
    """A journal in which every seat drafts pure ESPN board order."""
    path = _make_journal_file(
        tmp_path, board, [AutodraftBot() for _ in range(TEAMS)], operator_slot=5
    )
    return roomcheck.load_journal(path)


# ------------------------------------------------------------ journal loading


def test_load_journal_round_trips_header_and_picks(autodraft_journal):
    journal = autodraft_journal
    assert journal.season == 2026
    assert journal.board_as_of == AS_OF
    assert journal.operator_slot == 5
    assert journal.teams == TEAMS
    assert journal.complete
    assert len(journal.picks) == ROUNDS * TEAMS
    assert [p.overall for p in journal.picks] == list(range(1, ROUNDS * TEAMS + 1))
    assert journal.operator_overall_picks() == tuple(
        o for o, seat in enumerate(snake_sequence(range(TEAMS), ROUNDS), start=1)
        if seat == 5
    )
    assert len(journal.room_picks()) == ROUNDS * (TEAMS - 1)
    assert all(p.seat != 5 for p in journal.room_picks())


def test_load_journal_rejects_a_seat_that_contradicts_the_snake(tmp_path, board):
    """A journalled seat that is not on the clock invalidates every replay below.

    The seat drives which roster a rival advances from and which picks count as
    'the room'. Silently trusting it would produce numbers that look fine.
    """
    path = _make_journal_file(tmp_path, board, [AutodraftBot()] * TEAMS, operator_slot=0)
    lines = path.read_text().splitlines()
    record = json.loads(lines[3])
    record["seat"] = (record["seat"] + 1) % TEAMS
    lines[3] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(roomcheck.JournalError, match="on the clock"):
        roomcheck.load_journal(path)


def test_load_journal_rejects_the_same_player_drafted_twice(tmp_path, board):
    path = _make_journal_file(tmp_path, board, [AutodraftBot()] * TEAMS, operator_slot=0)
    lines = path.read_text().splitlines()
    second = json.loads(lines[2])
    second["player_id"] = json.loads(lines[1])["player_id"]
    lines[2] = json.dumps(second)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(roomcheck.JournalError, match="same player twice"):
        roomcheck.load_journal(path)


def test_load_journal_rejects_a_file_with_no_header(tmp_path):
    path = tmp_path / "headerless.jsonl"
    path.write_text(
        json.dumps({"kind": "pick", "overall": 1, "seat": 0, "player_id": "x"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(roomcheck.JournalError, match="not a session header"):
        roomcheck.load_journal(path)


def test_load_journals_keeps_only_completed_drafts(tmp_path, board):
    """The practice directory is mostly abandoned launches; a fragment is not a room."""
    full = _make_journal_file(
        tmp_path, board, [AutodraftBot()] * TEAMS, operator_slot=0, name="full.jsonl"
    )
    # header-only launch
    (tmp_path / "empty.jsonl").write_text(
        full.read_text().splitlines()[0] + "\n", encoding="utf-8"
    )
    # a 20-pick fragment
    (tmp_path / "partial.jsonl").write_text(
        "\n".join(full.read_text().splitlines()[:21]) + "\n", encoding="utf-8"
    )
    (tmp_path / "garbage.jsonl").write_text("not json at all\n", encoding="utf-8")

    kept = roomcheck.load_journals(tmp_path)
    assert [j.name for j in kept] == ["full.jsonl"]
    # header-only, 20-pick and complete all parse; only the garbage file is dropped
    assert {j.name for j in roomcheck.load_journals(tmp_path, complete_only=False)} == {
        "full.jsonl",
        "empty.jsonl",
        "partial.jsonl",
    }


# --------------------------------------------------- decision reconstruction


def test_every_window_state_holds_exactly_the_picks_before_it(autodraft_journal, board):
    """Each window's board must be the board as it stood on that clock.

    This is the regression test for BOTH reconstruction bugs found during the
    build. If the prefix before our first turn is not replayed, window 0 is
    short by ``decision_pick - 1`` players (and the top of the board never
    disappears). If every window shares ONE live BoardState, the returned tuple
    hands the caller a state holding the entire finished draft, every probe then
    reads undrafted players and the observed survival rate comes out at 1.000.
    """
    windows = roomcheck.operator_windows(autodraft_journal, board)
    assert windows, "a 16-round journal must yield windows"
    for window in windows:
        assert len(window.state.taken) == window.decision_pick - 1
    # ... and the identities are right, not merely the count.
    by_overall = autodraft_journal.by_overall
    first = windows[0]
    assert first.state.taken == {
        by_overall[o].player_id for o in range(1, first.decision_pick)
    }
    assert first.decision_pick == autodraft_journal.operator_slot + 1
    assert len(first.state.taken) == autodraft_journal.operator_slot


def test_windows_cover_every_operator_pick_but_the_last(autodraft_journal, board):
    windows = roomcheck.operator_windows(autodraft_journal, board)
    op_picks = autodraft_journal.operator_overall_picks()
    assert [w.decision_pick for w in windows] == list(op_picks[:-1])
    assert [w.next_pick for w in windows] == list(op_picks[1:])
    assert all(w.gap == w.next_pick - w.decision_pick - 1 for w in windows)


def test_taken_in_window_is_exactly_the_intervening_room_picks(autodraft_journal, board):
    by_overall = autodraft_journal.by_overall
    for window in roomcheck.operator_windows(autodraft_journal, board):
        expected = {
            by_overall[o].player_id
            for o in range(window.decision_pick + 1, window.next_pick)
        }
        assert window.taken_in_window == expected
        # neither endpoint belongs to the window
        assert window.operator_took not in window.taken_in_window
        assert by_overall[window.next_pick].player_id not in window.taken_in_window


def test_opponent_rosters_exclude_us_and_match_the_journal(autodraft_journal, board):
    journal = autodraft_journal
    by_overall = journal.by_overall
    window = roomcheck.operator_windows(journal, board)[6]
    assert journal.operator_slot not in window.opponent_rosters
    for seat, entries in window.opponent_rosters.items():
        expected = [
            by_overall[o].player_id
            for o in range(1, window.decision_pick)
            if by_overall[o].seat == seat
        ]
        assert [e.player_id for e in entries] == expected
    assert [e.player_id for e in window.own_roster] == [
        by_overall[o].player_id
        for o in range(1, window.decision_pick)
        if by_overall[o].seat == journal.operator_slot
    ]


def test_probe_never_scores_the_player_we_took(autodraft_journal, board):
    """Our own pick's survival is counterfactual and is never observed.

    Leaving him in would score him as 'survived' in every window (the room never
    took him — we did), and he is by construction the single most contested
    player on the board.
    """
    board_ids = {e.player_id for e in board}
    for window in roomcheck.operator_windows(autodraft_journal, board):
        candidates, engine_ids = roomcheck.probe_candidates(window)
        ids = {c.player_id for c in candidates}
        assert window.operator_took not in ids
        assert window.operator_took not in engine_ids
        assert engine_ids <= ids
        assert ids <= board_ids                      # every probe row is a board row
        assert ids.isdisjoint(window.state.taken)    # and none of them is already gone


def test_a_board_missing_a_drafted_player_is_refused(autodraft_journal, board):
    """Replaying onto a board that lacks a drafted player must crash, not skip him.

    Skipping would leave him "available" for the rest of the replay and score him
    as having survived every window — the same failure shape as the two
    reconstruction bugs above, arriving through a different door.
    """
    without_the_first_pick = tuple(board[1:])
    with pytest.raises(roomcheck.BoardMismatch, match="not on the rebuilt board"):
        roomcheck.operator_windows(autodraft_journal, without_the_first_pick)


# ---------------------------------------------------------------- calibration


def _points(pairs, *, rounds=None):
    """``[(predicted, survived), ...]`` -> SurvivalPoints (fields that matter only)."""
    return [
        roomcheck.SurvivalPoint(
            journal="j",
            route="test",
            round=1 if rounds is None else rounds[i],
            decision_pick=1,
            next_pick=2,
            player_id=f"p{i}",
            name=None,
            position="RB",
            espn_overall_rank=i + 1,
            predicted=p,
            survived=s,
            in_engine_candidates=True,
        )
        for i, (p, s) in enumerate(pairs)
    ]


def test_calibration_on_a_perfectly_calibrated_cohort():
    points = _points([(0.3, True)] * 30 + [(0.3, False)] * 70)
    cal = roomcheck.calibrate(points, label="perfect")
    assert cal.n == 100
    assert cal.mean_predicted == pytest.approx(0.3)
    assert cal.observed_rate == pytest.approx(0.3)
    assert cal.bias == pytest.approx(0.0)
    assert cal.brier == pytest.approx(0.3 * 0.7)
    assert cal.verdict == "well calibrated"


def test_bias_sign_says_optimistic_when_the_model_over_promises():
    """Sign convention is load-bearing: it names the engine's failure mode.

    Positive bias = the model promised more survival than the room delivered =
    the engine waits on players that are gone. A flipped sign would print the
    opposite instruction to a novice operator.
    """
    optimistic = roomcheck.calibrate(
        _points([(1.0, True)] * 50 + [(1.0, False)] * 50), label="over"
    )
    assert optimistic.bias == pytest.approx(0.5)
    assert "OPTIMISTIC" in optimistic.verdict

    pessimistic = roomcheck.calibrate(
        _points([(0.0, True)] * 50 + [(0.0, False)] * 50), label="under"
    )
    assert pessimistic.bias == pytest.approx(-0.5)
    assert "PESSIMISTIC" in pessimistic.verdict


def test_reliability_bins_keep_every_point_including_a_flat_certainty():
    """A prediction of exactly 1.0 must land in the top bin, not vanish.

    The rollout returns exactly 1.0 for a large share of candidates (nobody
    touched them in any rollout), so a half-open top bin ending at 1.0 would
    silently drop the biggest and most over-confident cohort in the sample.
    """
    points = _points([(1.0, True)] * 20 + [(0.42, False)] * 10 + [(0.0, False)] * 5)
    cal = roomcheck.calibrate(points, label="bins")
    assert sum(b.n for b in cal.bins) == cal.n == 35
    top = [b for b in cal.bins if b.low >= 0.99]
    assert top and top[0].n == 20


def test_calibrate_refuses_an_empty_cohort():
    with pytest.raises(ValueError, match="no survival points"):
        roomcheck.calibrate([], label="nothing")


def test_calibrate_by_partitions_the_sample():
    points = _points([(0.5, True), (0.5, False), (0.5, True), (0.5, False)],
                     rounds=[1, 2, 1, 2])
    grouped = roomcheck.calibrate_by(points, "round")
    assert set(grouped) == {1, 2}
    assert sum(c.n for c in grouped.values()) == len(points)
    assert all(c.n == 2 for c in grouped.values())


# --------------------------------------------------------------------- re-fit


def _logistic_draw(rng, center, width):
    """One draw from a logistic(center, width) via inverse CDF."""
    u = rng.random()
    return center + width * math.log(u / (1.0 - u))


def _generated_observations(*, intercept, slope, width, n_ranks, seed, last_pick=160):
    rng = random.Random(seed)
    rows = []
    for rank in range(1, n_ranks + 1):
        pick = _logistic_draw(rng, intercept + slope * rank, width)
        if pick > last_pick or pick < 1:
            rows.append(roomcheck.SurvivalObservation("RB", rank, last_pick, True))
        else:
            rows.append(roomcheck.SurvivalObservation("RB", rank, int(round(pick)), False))
    return rows


def _kdst_observations(*, center, width, seed, position, n, last_pick=160):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        pick = _logistic_draw(rng, center, width)
        if pick > last_pick or pick < 1:
            rows.append(roomcheck.SurvivalObservation(position, 200 + i, last_pick, True))
        else:
            rows.append(
                roomcheck.SurvivalObservation(position, 200 + i, int(round(pick)), False)
            )
    return rows


def test_fit_recovers_the_parameters_that_generated_the_data():
    """The MLE is the whole re-fit deliverable; it must actually be an MLE.

    Data is drawn FROM the shipped survival form (logistic location-scale, right
    censored at the last pick), so a correct fit returns the generating
    constants. A curve-fit that ignored the model, or an optimizer that never
    moved off its start, fails this.
    """
    rows = _generated_observations(
        intercept=4.0, slope=0.85, width=5.0, n_ranks=1200, seed=11
    )
    rows += _kdst_observations(center=150.0, width=9.0, seed=12, position="K", n=400)
    rows += _kdst_observations(center=145.0, width=11.0, seed=13, position="DST", n=400)

    fit = roomcheck.fit_survival_params(rows)
    assert fit.params.skill_center_intercept == pytest.approx(4.0, abs=2.0)
    assert fit.params.skill_center_slope == pytest.approx(0.85, abs=0.03)
    assert fit.params.skill_width == pytest.approx(5.0, abs=0.6)
    assert fit.params.k_center == pytest.approx(150.0, abs=4.0)
    assert fit.params.k_width == pytest.approx(9.0, abs=2.0)
    assert fit.params.dst_center == pytest.approx(145.0, abs=4.0)
    assert fit.params.dst_width == pytest.approx(11.0, abs=2.5)
    # and it beat the shipped constants on the data that generated it
    assert fit.skill.nll < fit.skill.nll_baseline


def test_fit_honours_right_censoring():
    """A censored row says 'not yet by then', never 'taken then'.

    Every observation here is censored at pick 60. Treating them as events
    places the center AT 60; honouring the censoring must push it well past 60,
    because all the data says is that nobody had been taken yet.
    """
    censored = [roomcheck.SurvivalObservation("DST", 200 + i, 60, True) for i in range(60)]
    censored += [roomcheck.SurvivalObservation("DST", 300, 140, False)]
    censored += _generated_observations(
        intercept=4.0, slope=0.85, width=5.0, n_ranks=300, seed=3
    )
    censored += _kdst_observations(center=150.0, width=9.0, seed=4, position="K", n=200)

    fit = roomcheck.fit_survival_params(censored)
    assert fit.params.dst_center > 90.0

    events = [
        roomcheck.SurvivalObservation(o.position, o.espn_overall_rank, o.pick, False)
        if o.position == "DST" and o.pick == 60
        else o
        for o in censored
    ]
    as_events = roomcheck.fit_survival_params(events)
    assert as_events.params.dst_center == pytest.approx(60.0, abs=6.0)
    assert as_events.params.dst_center < fit.params.dst_center - 20.0


def test_fit_is_deterministic():
    rows = _generated_observations(
        intercept=4.0, slope=0.85, width=5.0, n_ranks=400, seed=5
    )
    rows += _kdst_observations(center=150.0, width=9.0, seed=6, position="K", n=200)
    rows += _kdst_observations(center=145.0, width=11.0, seed=7, position="DST", n=200)
    assert roomcheck.fit_survival_params(rows) == roomcheck.fit_survival_params(rows)


def test_fit_refuses_a_group_with_no_room_events():
    rows = _generated_observations(
        intercept=4.0, slope=0.85, width=5.0, n_ranks=200, seed=8
    )
    rows += _kdst_observations(center=150.0, width=9.0, seed=9, position="K", n=100)
    rows += [roomcheck.SurvivalObservation("DST", 200 + i, 160, True) for i in range(30)]
    with pytest.raises(ValueError, match="no uncensored room picks"):
        roomcheck.fit_survival_params(rows)


def test_survival_observations_censor_our_own_picks(tmp_path, board):
    """Our defense at round 5 is OUR behaviour, never evidence about the room.

    If the operator's pick were recorded as a room event, the re-fit would learn
    'this room takes defenses around pick 45' from the engine's own divergence
    play — the exact circularity this whole module exists to break.
    """
    pickers = [AutodraftBot() for _ in range(TEAMS)]
    pickers[3] = _GrabPositionAtRound("DST", 5)
    path = _make_journal_file(tmp_path, board, pickers, operator_slot=3)
    journal = roomcheck.load_journal(path)

    ours = [p for p in journal.picks if p.seat == 3]
    our_dst = next(
        p for p in ours if next(e for e in board if e.player_id == p.player_id).position == "DST"
    )
    assert our_dst.overall < 60  # the scripted early grab really happened

    observations = {
        (o.position, o.espn_overall_rank): o
        for o in roomcheck.survival_observations(journal, board)
    }
    entry = next(e for e in board if e.player_id == our_dst.player_id)
    mine = observations[(entry.position, entry.espn_overall_rank)]
    assert mine.pick == our_dst.overall
    assert mine.censored is True

    room_dst = [
        p
        for p in journal.picks
        if p.seat != 3
        and next(e for e in board if e.player_id == p.player_id).position == "DST"
    ]
    assert room_dst, "the autodraft room must complete its defenses"
    sample = room_dst[0]
    sample_entry = next(e for e in board if e.player_id == sample.player_id)
    assert (
        observations[(sample_entry.position, sample_entry.espn_overall_rank)].censored
        is False
    )


def test_survival_observations_drop_board_unranked_players():
    fake = (
        BoardEntry("a", "a", "RB", 5, 100.0, 10.0),
        BoardEntry("b", "b", "WR", roomcheck.FALLBACK_RANK_BASE + 3, 10.0, -5.0),
    )
    journal = roomcheck.DraftJournal(
        path="x.jsonl",
        season=2026,
        board_as_of=AS_OF,
        operator_slot=0,
        pick_order=(0,),
        rounds=1,
        roster=RosterStructure(teams=1),
        board_count=2,
        board_hash="h",
        picks=(),
    )
    observed = roomcheck.survival_observations(journal, fake)
    assert [o.espn_overall_rank for o in observed] == [5]


# ------------------------------------------------------------- the K/DST question


def _kdst_world(tmp_path, board, *, n=3):
    """Journals where WE grab a defense in round 5 and the room drafts on rank."""
    journals = {}
    boards = {}
    for i in range(n):
        pickers = [AutodraftBot() for _ in range(TEAMS)]
        pickers[2] = _GrabPositionAtRound("DST", 5)
        path = _make_journal_file(
            tmp_path, board, pickers, operator_slot=2, name=f"j{i}.jsonl", seed=100 + i
        )
        journal = roomcheck.load_journal(path)
        journals[journal.name] = journal
        boards[journal.name] = board
    return list(journals.values()), boards


def test_kdst_timing_never_counts_our_own_early_defense(tmp_path, board):
    journals, boards = _kdst_world(tmp_path, board)
    timing = roomcheck.kdst_timing(journals, boards, position="DST")

    assert timing.operator_picks and max(timing.operator_picks) < 60
    # not one of our picks leaked into the room sample
    assert set(timing.operator_picks).isdisjoint(timing.all_room_picks)
    assert timing.earliest > max(timing.operator_picks)
    assert timing.median_first > max(timing.operator_picks)
    assert timing.n_room_picks == len(timing.all_room_picks)
    assert timing.n_rival_seats == len(journals) * (TEAMS - 1)
    assert timing.picks_before[90] == sum(1 for p in timing.all_room_picks if p < 90)


def test_kdst_player_rows_carry_adp_and_the_signed_error(tmp_path, board):
    """``adp_error`` is the headline of §3: positive means ADP is EARLY.

    A sign flip here would invert the module's whole conclusion about ESPN ADP.
    """
    journals, boards = _kdst_world(tmp_path, board, n=2)
    kickers = {e.player_id for e in board if e.position == "K"}
    adp = roomcheck.AdpTable(
        by_espn_id={pid: 100.0 for pid in kickers}, by_dst_team={}
    )
    timing = roomcheck.kdst_timing(
        journals, boards, position="K", adp_by_as_of={AS_OF: adp}
    )
    assert timing.players
    for row in timing.players:
        assert row.espn_adp == 100.0
        if row.median_room_pick is not None:
            assert row.adp_error == pytest.approx(row.median_room_pick - 100.0)
    # the autodraft room takes kickers very late, so ADP at 100 reads as EARLY
    priced = [r for r in timing.players if r.adp_error is not None]
    assert priced and all(r.adp_error > 0 for r in priced)


def test_kdst_player_rows_report_no_adp_honestly(tmp_path, board):
    """A missing ADP is None, never a zero that would silently enter a mean."""
    journals, boards = _kdst_world(tmp_path, board, n=2)
    timing = roomcheck.kdst_timing(
        journals,
        boards,
        position="DST",
        adp_by_as_of={AS_OF: roomcheck.AdpTable(by_espn_id={}, by_dst_team={})},
    )
    assert timing.players
    assert all(row.espn_adp is None and row.adp_error is None for row in timing.players)


def test_adp_rescale_recovers_a_known_rescale(tmp_path, board):
    """The slope is the whole test of the 'ADP is just a bigger league' claim.

    Construct rooms whose picks ARE a clean affine function of ADP and check the
    fit returns that function; then the measured collapse at K/DST in the real
    journals is a property of the data, not of this estimator.
    """
    journals, boards = _kdst_world(tmp_path, board, n=6)
    picks_by_player = {}
    for journal in journals:
        for pick in journal.picks:
            if pick.seat != journal.operator_slot:
                picks_by_player.setdefault(pick.player_id, []).append(pick.overall)
    by_id = {e.player_id: e for e in board}
    skill = [
        pid
        for pid, overalls in picks_by_player.items()
        if by_id[pid].position in roomcheck.SKILL_POSITIONS and len(overalls) >= 6
    ]
    assert len(skill) >= 20
    # invert the relation we want to recover: adp = (median_pick - 5) / 0.8
    adp = roomcheck.AdpTable(
        by_espn_id={
            pid: (sorted(picks_by_player[pid])[len(picks_by_player[pid]) // 2] - 5.0) / 0.8
            for pid in skill
        },
        by_dst_team={},
    )
    fit = roomcheck.adp_rescale(
        journals,
        boards,
        {AS_OF: adp},
        positions=roomcheck.SKILL_POSITIONS,
        group="skill",
    )
    assert fit is not None
    assert fit.slope == pytest.approx(0.8, abs=0.02)
    assert fit.intercept == pytest.approx(5.0, abs=1.5)
    assert fit.r_squared > 0.99


def test_adp_rescale_returns_none_rather_than_a_fit_on_nothing(tmp_path, board):
    journals, boards = _kdst_world(tmp_path, board, n=2)
    empty = roomcheck.AdpTable(by_espn_id={}, by_dst_team={})
    assert (
        roomcheck.adp_rescale(
            journals, boards, {AS_OF: empty}, positions=("RB",), group="RB"
        )
        is None
    )


def test_adp_at_the_ceiling_is_treated_as_undrafted_not_as_a_position():
    entry = BoardEntry("x", "x", "RB", 40, 100.0, 5.0)
    table = roomcheck.AdpTable(by_espn_id={"x": 170.0}, by_dst_team={})
    assert table.get(entry) == 170.0
    assert table.censored(entry) is True
    assert roomcheck.AdpTable(by_espn_id={"x": 88.0}, by_dst_team={}).censored(entry) is False


# ------------------------------------------------------------------ room fit


def test_a_pure_board_room_reads_as_fully_autodraft_like(tmp_path, board):
    """An all-AutodraftBot room is the positive control for the seat measure."""
    path = _make_journal_file(tmp_path, board, [AutodraftBot()] * TEAMS, operator_slot=4)
    journal = roomcheck.load_journal(path)
    fit = roomcheck.room_fit(journal, board)

    assert {s.seat for s in fit.seats} == set(range(TEAMS)) - {4}
    assert all(s.autodraft_match == 1.0 for s in fit.seats)
    assert all(s.median_slots_past_best == 0 for s in fit.seats)
    assert fit.autodraft_like == TEAMS - 1
    # every seat finishes with exactly one K and one D/ST (caps + legality), and
    # the shipped reach estimator excludes both, so the reach sample is
    # 9 rival seats x 14 non-K/DST picks.
    assert all(set(s.kdst_picks) == {"K", "DST"} for s in fit.seats)
    assert fit.n_room_picks == (TEAMS - 1) * (ROUNDS - 2)


def test_a_reaching_room_reads_as_not_autodraft_like(tmp_path, board):
    """The negative control: seats that consistently reach must not read as bots."""
    pickers = [_OffsetBot(6) for _ in range(TEAMS)]
    pickers[0] = AutodraftBot()
    path = _make_journal_file(tmp_path, board, pickers, operator_slot=0)
    journal = roomcheck.load_journal(path)
    fit = roomcheck.room_fit(journal, board)

    assert fit.autodraft_like == 0
    assert all(s.autodraft_match < 0.5 for s in fit.seats)
    assert all(s.median_slots_past_best > 0 for s in fit.seats)
    assert fit.reach_sigma is not None and fit.reach_sigma > 0


def test_room_fit_never_measures_the_operator_seat(tmp_path, board):
    """Our seat is the engine under test; counting it measures the model twice."""
    pickers = [AutodraftBot() for _ in range(TEAMS)]
    pickers[7] = _OffsetBot(15)  # a wildly non-board operator
    path = _make_journal_file(tmp_path, board, pickers, operator_slot=7)
    journal = roomcheck.load_journal(path)
    fit = roomcheck.room_fit(journal, board)

    assert 7 not in {s.seat for s in fit.seats}
    assert all(s.autodraft_match == 1.0 for s in fit.seats)


# ---------------------------------------------------------- the DB seam (Rule 1)


def test_board_loader_takes_a_keyword_only_as_of_with_no_default():
    parameter = inspect.signature(roomcheck.load_journal_board).parameters["as_of"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    adp_parameter = inspect.signature(roomcheck.load_espn_adp).parameters["as_of"]
    assert adp_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert adp_parameter.default is inspect.Parameter.empty


def test_board_loader_forwards_as_of_and_refuses_a_different_board(
    monkeypatch, autodraft_journal, board
):
    """A board loaded at the wrong as_of must crash, not re-price history quietly."""
    seen = {}

    def fake_load_board(conn, *, as_of, season, **kwargs):
        seen["as_of"] = as_of
        seen["season"] = season
        return tuple(board) if as_of == AS_OF else tuple(board[:-2])

    monkeypatch.setattr("ziggurat.draft.simulator.load_board", fake_load_board)
    loaded = roomcheck.load_journal_board(None, autodraft_journal, as_of=AS_OF)
    assert seen == {"as_of": AS_OF, "season": 2026}
    assert len(loaded) == len(board)

    with pytest.raises(roomcheck.BoardMismatch, match="board_as_of"):
        roomcheck.load_journal_board(None, autodraft_journal, as_of="2026-08-29")


def test_espn_adp_read_does_not_see_a_later_pull(db):
    """Leakage test for the module's only new read (Rule 1).

    THE BACK-STAMPED ROW IS THE POINT. Rows whose ``retrieved_as_of`` equals
    their ``knowable_as_of`` only exercise the KNOWLEDGE-time gate, which
    ``base.latest_truth`` enforces too — so a regression of this accessor to
    ``latest_truth`` (the exact Rule-1 mistake) would pass such a test. The
    third row below is stamped knowable 2026-08-16 but RETRIEVED 2026-08-29:
    only the ``historical`` view hides it from an 2026-08-16 read, so this test
    now distinguishes the safe default view from an explicit opt-in.
    """
    rows = [
        ("111", "111", "Synthetic RB", "RB", "KC", 2026, 10, 1, 40.0, 1,
         "2026-08-16", "2026-08-16"),
        ("111", "111", "Synthetic RB", "RB", "KC", 2026, 10, 1, 12.0, 1,
         "2026-08-29", "2026-08-29"),
        ("SEA", None, "Seahawks D/ST", "D/ST", "SEA", 2026, 240, 3, 111.0, 3,
         "2026-08-16", "2026-08-16"),
        # back-stamped: pulled on the 29th, claiming to have been knowable on
        # the 16th. A read at the 16th must not see it.
        ("222", "222", "Backstamped WR", "WR", "SF", 2026, 44, 4, 55.0, 4,
         "2026-08-29", "2026-08-16"),
    ]
    db.executemany(
        "INSERT INTO espn_draft_ranks (board_key, espn_id, player, position, team, "
        "season, overall_rank, espn_pos_rank, adp, espn_adp_pos_rank, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()

    early = roomcheck.load_espn_adp(db, as_of="2026-08-16", season=2026)
    assert early.by_espn_id["111"] == 40.0
    assert early.by_dst_team["SEA"] == 111.0

    assert "222" not in early.by_espn_id, (
        "a row RETRIEVED after this as_of must be invisible: load_espn_adp "
        "must keep the historical view, not opt into latest_truth"
    )

    late = roomcheck.load_espn_adp(db, as_of="2026-08-29", season=2026)
    assert late.by_espn_id["111"] == 12.0
    assert late.by_espn_id["222"] == 55.0  # visible once its pull has happened

    entry = BoardEntry("111", "Synthetic RB", "RB", 10, 100.0, 5.0, team="KC")
    dst = BoardEntry("-16", "Seahawks D/ST", "DST", 240, 90.0, 20.0, team="SEA")
    assert early.get(entry) == 40.0
    assert early.get(dst) == 111.0


# ------------------------------------------------------------------ end to end


def test_analytic_points_are_pure_and_respond_to_the_parameters(autodraft_journal, board):
    shipped = roomcheck.analytic_points(autodraft_journal, board)
    refit = roomcheck.analytic_points(
        autodraft_journal, board, params=roomcheck.REFIT_PRACTICE_2026
    )
    assert len(shipped) == len(refit) > 0
    assert shipped == roomcheck.analytic_points(autodraft_journal, board)
    # the refit centers sit LATER, so it predicts more survival on the same rows
    assert roomcheck.calibrate(refit, label="r").mean_predicted > roomcheck.calibrate(
        shipped, label="s"
    ).mean_predicted


def test_rollout_points_are_deterministic_for_a_fixed_seed(autodraft_journal, board):
    def run(seed):
        return roomcheck.rollout_points(
            autodraft_journal, board, rng=random.Random(seed), rollouts=16
        )

    assert run(4) == run(4)
    assert run(4) != run(5)


def test_rollout_points_report_a_real_outcome_mix(autodraft_journal, board):
    """A guard against reconstruction bugs that make every outcome identical.

    Both build-time reconstruction defects showed up here first: one drove the
    observed survival rate to 1.000, the other to a near-constant. A real draft
    has contested and uncontested candidates in every window.
    """
    points = roomcheck.rollout_points(
        autodraft_journal, board, rng=random.Random(2), rollouts=32
    )
    cal = roomcheck.calibrate(points, label="mix")
    assert 0.2 < cal.observed_rate < 0.95
    assert 0.2 < cal.mean_predicted < 0.95
    assert any(not p.survived for p in points)
    assert any(p.survived for p in points)


def test_cross_validation_scores_a_held_out_room(tmp_path, board):
    journals = []
    boards = {}
    for i in range(3):
        pickers = [_OffsetBot(i % 3) for _ in range(TEAMS)]
        path = _make_journal_file(
            tmp_path, board, pickers, operator_slot=1, name=f"cv{i}.jsonl", seed=200 + i
        )
        journal = roomcheck.load_journal(path)
        journals.append(journal)
        boards[journal.name] = board
    cv = roomcheck.cross_validate_refit(journals, boards)
    assert [f.journal for f in cv.folds] == [j.name for j in journals]
    assert 0 <= cv.folds_improved <= len(cv.folds)
    for fold in cv.folds:
        assert fold.params != DEFAULT_SURVIVAL_PARAMS
        assert fold.shipped.n == fold.refit.n > 0


def test_simulated_room_reports_the_same_statistics_as_a_real_one(board):
    sim = roomcheck.simulate_room(
        board, rng=random.Random(3), drafts=8, priors=ROOM_PRIORS_2025, roster=ROSTER
    )
    assert sim.drafts == 8
    assert len(sim.first_dst) == len(sim.first_k) == 8
    assert sim.median_first_dst <= sim.median_dst
    assert sim.median_first_k <= sim.median_k
    assert sim.realized_reach_sigma > 0
    # deterministic for a fixed seed
    again = roomcheck.simulate_room(
        board, rng=random.Random(3), drafts=8, priors=ROOM_PRIORS_2025, roster=ROSTER
    )
    assert sim == again


def test_render_report_survives_a_partial_report(board):
    """Every section must render alone; the renderer is how the finding is read.

    A section that reads another section's local state crashes the whole report
    the first time a caller runs one measurement without the other.
    """
    sim = roomcheck.simulate_room(board, rng=random.Random(1), drafts=4, roster=ROSTER)
    lines = roomcheck.render_report(roomcheck.RoomcheckReport(simulated=sim))
    text = "\n".join(lines)
    assert "generative check" in text
    assert "no real rooms" in text
    assert roomcheck.render_report(roomcheck.RoomcheckReport())  # empty is still a report


def test_render_report_names_every_number_it_prints(autodraft_journal, board):
    points = roomcheck.analytic_points(autodraft_journal, board)
    report = roomcheck.RoomcheckReport(
        journals=(autodraft_journal,),
        rollout=roomcheck.calibrate(points, label="rollout"),
    )
    lines = roomcheck.render_report(report)
    text = "\n".join(lines)
    assert "CALIBRATION" in text
    assert "predicted" in text and "observed" in text and "Brier" in text
    assert str(len(autodraft_journal.picks)) in text


def test_engine_candidate_set_matches_the_engines_own_construction():
    """The `in_engine_candidates` flag claims to reproduce PickEngine's D1 set.

    Two numbers make that claim true; both live in engine.py and are restated
    here, so this test is what keeps the restatement honest.
    """
    from ziggurat.draft import engine as engine_module
    from ziggurat.draft.priors import DEFAULT_KDST_EARLIEST_ROUND

    assert roomcheck.DEFAULT_CANDIDATE_WIDTH == engine_module.DEFAULT_CANDIDATE_WIDTH
    signature = inspect.signature(roomcheck.probe_candidates)
    assert signature.parameters["candidate_width"].default == (
        engine_module.DEFAULT_CANDIDATE_WIDTH
    )
    assert signature.parameters["kdst_earliest_round"].default == (
        DEFAULT_KDST_EARLIEST_ROUND
    )
    assert engine_module.PickEngine().kdst_earliest_round == DEFAULT_KDST_EARLIEST_ROUND


def test_the_engine_set_defers_kdst_exactly_as_the_engine_does(autodraft_journal, board):
    """Before round 9 a kicker is scored by the probe but is NOT an engine candidate."""
    early = [
        w for w in roomcheck.operator_windows(autodraft_journal, board) if w.round < 5
    ]
    assert early
    saw_kdst_in_probe = False
    for window in early:
        candidates, engine_ids = roomcheck.probe_candidates(window)
        by_id = {c.player_id: c for c in candidates}
        kdst = {pid for pid, c in by_id.items() if c.position in ("K", "DST")}
        saw_kdst_in_probe = saw_kdst_in_probe or bool(kdst)
        assert kdst.isdisjoint(engine_ids)
    assert saw_kdst_in_probe, "the probe must still price K/DST before the window"

    # and the points functions must inherit that window, not quietly re-open it
    early_rounds = {w.round for w in early}
    for scored in (
        roomcheck.analytic_points(autodraft_journal, board),
        roomcheck.rollout_points(
            autodraft_journal, board, rng=random.Random(1), rollouts=8
        ),
    ):
        flagged = [
            p
            for p in scored
            if p.round in early_rounds and p.position in ("K", "DST")
        ]
        assert flagged, "K/DST are still scored in early rounds"
        assert not any(p.in_engine_candidates for p in flagged)


def test_published_alternatives_are_labelled_not_defaults():
    """Rule 6 + the standing 'do not change shipped behaviour' constraint.

    The refit exists to be A/B'd, and the only thing standing between it and a
    silent behaviour change is that it is a separate, labelled constant.
    """
    assert roomcheck.REFIT_PRACTICE_2026 != DEFAULT_SURVIVAL_PARAMS
    assert isinstance(roomcheck.REFIT_PRACTICE_2026, SurvivalParams)
    assert DEFAULT_SURVIVAL_PARAMS == SurvivalParams()  # untouched by this module
    assert roomcheck.MEASURED_ROOM_2026.reach_sigma != ROOM_PRIORS_2025.reach_sigma
    assert roomcheck.MEASURED_ROOM_2026.autodraft_fraction == (
        ROOM_PRIORS_2025.autodraft_fraction
    )
    for label in (
        roomcheck.REFIT_PRACTICE_2026_LABEL,
        roomcheck.REACH_SIGMA_IS_AN_INPUT_NOT_AN_OUTPUT,
        roomcheck.KDST_SHAPE_IS_MISSPECIFIED,
    ):
        assert any(word in label for word in ("HYPOTHESIS", "TRAP", "FINDING"))
        assert "2026" in label  # every published constant cites its cohort


# =====================================================================
# The mutation-survivors: behaviours the first version of this file left
# entirely unguarded. Each test below was written against a specific mutation
# of roomcheck.py that previously left the whole suite green.
# =====================================================================


class _RecordingSurvival:
    """A survival provider that records the exact candidate set the engine built.

    ``PickEngine._ask_survival`` hands its provider the D1 candidate list, so
    injecting this is the only way to see the real gather without copying it.
    """

    def __init__(self):
        self.seen: set[str] | None = None

    def __call__(self, ctx, *, candidates, positions, rng):
        from ziggurat.draft.engine import SurvivalEstimate

        self.seen = {c.player_id for c in candidates}
        return SurvivalEstimate(
            survival={c.player_id: 1.0 for c in candidates},
            next_best_vor={p: 0.0 for p in positions},
        )


def test_engine_candidate_flag_reproduces_a_real_pickengine_gather(
    autodraft_journal, board
):
    """``in_engine_candidates`` must BE PickEngine's D1 set, not resemble it.

    The flag defines the decision-relevant cohort every headline in this module
    is filtered to, and it is a RESTATEMENT of engine.py's candidate gather.
    Pinning the two integer constants (as this file used to) does not test the
    restatement: dropping the best-by-VOR-per-allowed-position half — measured
    load-bearing in 14 of 15 windows below — left the whole suite green while
    the engine cohort silently lost a third of its rows. So this drives a REAL
    :class:`PickEngine` over the same windows and compares the sets.
    """
    from ziggurat.draft.engine import PickEngine

    recorder = _RecordingSurvival()
    engine = PickEngine(survival=recorder)
    windows = roomcheck.operator_windows(autodraft_journal, board)
    assert windows

    rank_only_would_differ = 0
    for window in windows:
        engine.recommend(window.context(rng=random.Random(11)))
        assert recorder.seen is not None
        _probe, engine_ids = roomcheck.probe_candidates(window)
        assert engine_ids == recorder.seen - {window.operator_took}, (
            f"round {window.round}: the reconstructed engine candidate set is "
            "not the set PickEngine actually gathered"
        )
        # ... and record that the reconstruction is doing real work here: a
        # rank-window-only set would be a different (smaller) answer.
        counts = position_counts(window.own_roster)
        allowed = allowed_positions(
            counts,
            window.rounds_total - window.round,
            window.roster,
            round_num=window.round,
            kdst_earliest_round=9,
        ) or set(POSITIONS)
        rank_only = {
            e.player_id
            for e in window.state.window_by_rank(
                sorted(allowed), roomcheck.DEFAULT_CANDIDATE_WIDTH
            )
        }
        if rank_only != recorder.seen:
            rank_only_would_differ += 1
    assert rank_only_would_differ >= len(windows) - 2, (
        "this test only has power while the best-by-VOR half of the gather "
        "changes the set; if that stops being true, the mutation it exists to "
        "catch would pass"
    )


def test_survived_is_the_journals_own_outcome(autodraft_journal, board):
    """The ground-truth label behind EVERY number this module publishes.

    Inverting ``survived`` (one character) reverses the module's whole headline
    — the analytic route reads "nearly calibrated" and the rollout reads "badly
    optimistic" — and used to leave all 44 tests green. This checks the flag
    against the journal's own pick records, not against the derived
    ``taken_in_window`` the flag is computed from.
    """
    journal = autodraft_journal
    taken_at = {p.player_id: p.overall for p in journal.picks}
    points = roomcheck.analytic_points(journal, board)
    assert points

    saw_survivor = saw_casualty = False
    for pt in points:
        overall = taken_at.get(pt.player_id)
        gone_in_window = (
            overall is not None and pt.decision_pick < overall < pt.next_pick
        )
        assert pt.survived is (not gone_in_window), (
            f"{pt.player_id} was drafted at {overall}; window "
            f"{pt.decision_pick}->{pt.next_pick} says survived={pt.survived}"
        )
        saw_survivor = saw_survivor or pt.survived
        saw_casualty = saw_casualty or not pt.survived
    assert saw_survivor and saw_casualty, "a real draft has both outcomes"


def test_cross_validation_actually_holds_the_journal_out(tmp_path, board):
    """Leave-one-out is the ONLY evidence that the re-fit is not a memory.

    Training on all eleven journals (including the held-out one) satisfied
    every assertion the previous version of this test made. Here each fold's
    parameters must equal a fit on the OTHER journals and must differ from the
    fit on all of them — which an in-sample fold cannot do.
    """
    journals = []
    boards = {}
    for i in range(3):
        pickers = [_OffsetBot(i % 3) for _ in range(TEAMS)]
        path = _make_journal_file(
            tmp_path, board, pickers, operator_slot=1, name=f"cv{i}.jsonl", seed=300 + i
        )
        journal = roomcheck.load_journal(path)
        journals.append(journal)
        boards[journal.name] = board

    def fit_on(subset):
        rows = [
            o for j in subset for o in roomcheck.survival_observations(j, boards[j.name])
        ]
        return roomcheck.fit_survival_params(rows).params

    in_sample = fit_on(journals)
    cv = roomcheck.cross_validate_refit(journals, boards)
    assert [f.journal for f in cv.folds] == [j.name for j in journals]
    for fold, held in zip(cv.folds, journals, strict=True):
        honest = fit_on([j for j in journals if j.name != held.name])
        assert fold.params == honest
        assert fold.params != in_sample, (
            "a fold whose parameters equal the all-journals fit has trained on "
            "the room it is scoring"
        )
        assert fold.shipped.n == fold.refit.n > 0


def test_adp_rescale_never_counts_the_operator_seat(tmp_path, board):
    """Our own K/D-ST divergence play must not enter the ADP-vs-room regression.

    The engine grabs a kicker five rounds before the room does. If that pick
    counted as room behaviour, the module's whole "is ESPN ADP just a bigger
    league?" argument would be regressing our own strategy against ADP. The
    discriminator here is the operator LABEL, not the data: the same journals
    are re-read with the grabbing seat declared part of the room, and the fitted
    location must move.
    """
    journals = []
    boards = {}
    for i in range(6):
        pickers = [AutodraftBot() for _ in range(TEAMS)]
        pickers[2] = _GrabPositionAtRound("K", 3)
        path = _make_journal_file(
            tmp_path, board, pickers, operator_slot=2, name=f"k{i}.jsonl", seed=400 + i
        )
        journal = roomcheck.load_journal(path)
        journals.append(journal)
        boards[journal.name] = board

    kickers = [e for e in board if e.position == "K"]
    adp = roomcheck.AdpTable(
        by_espn_id={e.player_id: 40.0 + 2.0 * i for i, e in enumerate(kickers)},
        by_dst_team={},
    )
    fit = roomcheck.adp_rescale(
        journals, boards, {AS_OF: adp}, positions=("K",), group="K"
    )
    assert fit is not None
    assert fit.mean_room_pick > 100, "the room takes kickers late; we do not"

    leaked = [
        dataclasses.replace(j, operator_slot=(j.operator_slot + 1) % TEAMS)
        for j in journals
    ]
    leaked_fit = roomcheck.adp_rescale(
        leaked, boards, {AS_OF: adp}, positions=("K",), group="K"
    )
    assert leaked_fit is not None
    assert leaked_fit.mean_room_pick < fit.mean_room_pick - 5, (
        "declaring the early-grabbing seat part of the room must drag the fit "
        "earlier; if it does not, the operator exclusion is not doing anything"
    )


def test_each_journal_is_priced_with_its_own_days_adp(tmp_path, board):
    """ADP moves between drafts; pricing an old draft with today's market is a lie.

    Two journals, two ``board_as_of`` days, two very different markets. A player
    the room took in both must report the MEDIAN of the two days' values — if
    ``_adp_for`` fell back to "whichever table came first", he would report one
    of them.
    """
    journals = []
    boards = {}
    for i, as_of in enumerate(("2026-08-01", "2026-08-15")):
        path = _make_journal_file(
            tmp_path,
            board,
            [AutodraftBot() for _ in range(TEAMS)],
            operator_slot=0,
            name=f"day{i}.jsonl",
            as_of=as_of,
            seed=500 + i,
        )
        journal = roomcheck.load_journal(path)
        journals.append(journal)
        boards[journal.name] = board

    kickers = {e.player_id for e in board if e.position == "K"}
    tables = {
        "2026-08-01": roomcheck.AdpTable(
            by_espn_id={pid: 50.0 for pid in kickers}, by_dst_team={}
        ),
        "2026-08-15": roomcheck.AdpTable(
            by_espn_id={pid: 150.0 for pid in kickers}, by_dst_team={}
        ),
    }
    timing = roomcheck.kdst_timing(
        journals, boards, position="K", adp_by_as_of=tables
    )
    both = [r for r in timing.players if len(r.room_picks) >= 2]
    assert both, "the autodraft room takes the same kickers in both drafts"
    assert all(r.espn_adp == pytest.approx(100.0) for r in both)


def test_survival_observations_censor_at_this_drafts_last_pick(autodraft_journal, board):
    """The right-censoring horizon IS this draft's length, not a constant.

    Every never-drafted ranked player is censored at the last pick of the snake.
    Stretching that horizon (to 300, say) silently moves every fitted K/DST
    center, because those centers are mostly explained by the never-drafted
    majority.
    """
    journal = autodraft_journal
    last = journal.rounds * journal.teams
    drafted = {p.player_id for p in journal.picks}
    ranked_undrafted = {
        e.player_id
        for e in board
        if e.espn_overall_rank < roomcheck.FALLBACK_RANK_BASE
        and e.player_id not in drafted
    }
    assert ranked_undrafted, "a 160-pick draft leaves ranked players on the board"

    observations = roomcheck.survival_observations(journal, board)
    horizon = [o for o in observations if o.censored and o.pick == last]
    assert len(horizon) == len(ranked_undrafted)
    assert all(o.pick <= last for o in observations)


def test_rollout_points_use_the_rollout_count_they_are_given(autodraft_journal, board):
    """A hard-coded rollout count would still look deterministic and seed-sensitive.

    Survival is ``survivors / rollouts``, so the achievable values pin R: at
    R=7 every prediction is a multiple of 1/7 and at least one is not a
    multiple of 1/4.
    """
    points = roomcheck.rollout_points(
        autodraft_journal, board, rng=random.Random(3), rollouts=7
    )
    values = {p.predicted for p in points}
    assert any(0.0 < v < 1.0 for v in values), "some candidate must be contested"
    assert all(abs(v * 7 - round(v * 7)) < 1e-9 for v in values)
    assert any(abs(v * 4 - round(v * 4)) > 1e-9 for v in values), (
        "these predictions are sevenths, so a fixed R of 1, 2 or 4 is excluded"
    )


def test_window_context_threads_the_opponent_rosters(autodraft_journal, board):
    """``rollout_survival`` runs NEED-BLIND without them, and says so in its docs.

    An empty mapping is not an error anywhere: the rollout just stops honoring
    each rival's saturation and legality, and every survival number shifts.
    """
    window = roomcheck.operator_windows(autodraft_journal, board)[8]
    ctx = window.context(rng=random.Random(0))
    assert set(ctx.opponent_rosters) == set(window.opponent_rosters)
    assert ctx.opponent_rosters
    for seat, entries in window.opponent_rosters.items():
        assert [e.player_id for e in ctx.opponent_rosters[seat]] == [
            e.player_id for e in entries
        ]
    assert [e.player_id for e in ctx.own_roster] == [
        e.player_id for e in window.own_roster
    ]
    assert ctx.team_slot == window.operator_slot
    assert ctx.overall_pick == window.decision_pick
    assert ctx.round == window.round
    assert ctx.rounds_total == window.rounds_total


def test_the_wide_probe_is_wider_than_the_engine_set(autodraft_journal, board):
    """Without the wide probe every by-position and by-round n changes silently."""
    window = roomcheck.operator_windows(autodraft_journal, board)[4]
    narrow, engine_ids = roomcheck.probe_candidates(window, probe_width=5)
    wide, wide_engine = roomcheck.probe_candidates(window, probe_width=40)
    assert wide_engine == engine_ids  # the engine set does not depend on the probe
    assert len(wide) > len(narrow) > len(engine_ids)
    assert len(wide) >= 40 - 1  # minus our own pick, which is never scored


def test_rollout_defaults_are_the_live_cockpits_settings():
    """kappa and live recalibration are what make the "as the cockpit ran" claim true."""
    from ziggurat.draft import survival as survival_module

    signature = inspect.signature(roomcheck.rollout_points)
    assert signature.parameters["kappa"].default == survival_module.DEFAULT_KAPPA
    assert signature.parameters["live_recalibration"].default is True
    assert signature.parameters["priors"].default is ROOM_PRIORS_2025
    assert signature.parameters["rollouts"].default == (
        roomcheck.DEFAULT_MEASUREMENT_ROLLOUTS
    )


def test_live_recalibration_reproduces_what_the_session_would_have_held(
    autodraft_journal, board
):
    """The cockpit re-fits the room from the live pick log; so must the measurement.

    ``session._compute_recal`` is a pure function of (picks so far, board,
    operator slot). This asserts the window carries exactly that prefix and
    returns exactly that recalibration — and that a late window is ENGAGED, so
    the cold-start priors are demonstrably not what the cockpit was using.
    """
    from ziggurat.draft.survival import recalibrate_from_pick_log

    journal = autodraft_journal
    by_overall = journal.by_overall
    windows = roomcheck.operator_windows(journal, board)

    first, late = windows[0], windows[-1]
    assert len(first.prior_picks) == first.decision_pick - 1
    assert [pid for _o, _s, pid in late.prior_picks] == [
        by_overall[o].player_id for o in range(1, late.decision_pick)
    ]

    expected = recalibrate_from_pick_log(
        list(late.prior_picks), board, operator_slot=journal.operator_slot
    )
    assert late.live_recalibration(board) == expected
    assert expected.engaged, "a round-15 decision has seen far more than 20 room picks"
    assert expected.priors.reach_sigma != ROOM_PRIORS_2025.reach_sigma
    assert not first.live_recalibration(board).engaged  # cold start, as in the cockpit


def test_rollout_scores_the_live_priors_not_the_cold_start_ones(
    autodraft_journal, board
):
    """The measurement must change when the room model it uses changes."""
    live = roomcheck.rollout_points(
        autodraft_journal, board, rng=random.Random(5), rollouts=32
    )
    cold = roomcheck.rollout_points(
        autodraft_journal,
        board,
        rng=random.Random(5),
        rollouts=32,
        live_recalibration=False,
    )
    assert [p.player_id for p in live] == [p.player_id for p in cold]
    assert [p.predicted for p in live] != [p.predicted for p in cold]
    assert {p.route for p in live} == {"rollout"}
    assert {p.route for p in cold} == {"rollout/cold"}


def test_conditional_route_answers_the_conditional_question(autodraft_journal, board):
    """``S(next)/S(decision-1)`` >= ``S(next)``, always, and strictly for real rows.

    The unconditional form is what the engine consumes and is scored by default;
    this pins the alternative so the reported decomposition of the -0.370 bias
    into "shape" and "constants" cannot silently become two copies of the same
    number.
    """
    from ziggurat.draft.survival import analytic_survival

    unconditional = roomcheck.analytic_points(autodraft_journal, board)
    conditional = roomcheck.analytic_points(autodraft_journal, board, conditional=True)
    assert len(unconditional) == len(conditional) > 0
    assert {p.route for p in conditional} == {"analytic/conditional"}

    strictly_higher = 0
    for a, b in zip(unconditional, conditional, strict=True):
        assert (a.player_id, a.decision_pick) == (b.player_id, b.decision_pick)
        assert b.predicted >= a.predicted - 1e-12
        strictly_higher += b.predicted > a.predicted + 1e-9
        s_now = analytic_survival(
            a.espn_overall_rank, a.position, a.decision_pick - 1
        )
        if s_now > 1e-12:
            assert b.predicted == pytest.approx(min(1.0, a.predicted / s_now))
    assert strictly_higher > 0, "conditioning must matter on a real board"


def test_analytic_prediction_is_about_the_next_pick(autodraft_journal, board):
    """WHICH pick the survival question is about is the question itself.

    Scoring ``S(decision_pick)`` instead of ``S(next_pick)`` asks "is he there
    now?" — a question whose answer is always yes, by construction — and left
    the whole suite green.
    """
    from ziggurat.draft.survival import analytic_survival

    points = roomcheck.analytic_points(autodraft_journal, board)
    assert points
    differed = 0
    for pt in points:
        assert pt.predicted == pytest.approx(
            analytic_survival(pt.espn_overall_rank, pt.position, pt.next_pick)
        )
        differed += pt.predicted != pytest.approx(
            analytic_survival(pt.espn_overall_rank, pt.position, pt.decision_pick)
        )
    assert differed > len(points) // 4, (
        "the two questions must differ on enough rows for this test to bite"
    )


def test_kdst_timing_counts_picks_and_seats_separately(tmp_path, board):
    """"10/100 rival seats" was ten PICKS by 99 managers — a Rule-6 defect.

    One rival seat holding two defenses is enough to make picks and seats
    disagree, which is exactly what the real journals contain (100 D/ST picks,
    99 rival seats, one seat with two). The simulator refuses to produce that
    roster (it enforces the D/ST cap), so the journal is edited the way the real
    one arrived: one seat's last pick is a second defense.
    """
    path = _make_journal_file(
        tmp_path, board, [AutodraftBot() for _ in range(TEAMS)], operator_slot=0,
        seed=606,
    )
    lines = path.read_text().splitlines()
    drafted = {json.loads(ln)["player_id"] for ln in lines[1:]}
    spare = next(
        e for e in board if e.position == "DST" and e.player_id not in drafted
    )
    position_of = {e.player_id: e.position for e in board}
    for i in range(len(lines) - 1, 0, -1):
        record = json.loads(lines[i])
        if record["seat"] == 4 and position_of[record["player_id"]] != "DST":
            record["player_id"] = spare.player_id
            record["name"] = spare.name
            lines[i] = json.dumps(record)
            break
    else:  # pragma: no cover - the fixture room always leaves one
        raise AssertionError("seat 4 has no non-D/ST pick to convert")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    journal = roomcheck.load_journal(path)
    timing = roomcheck.kdst_timing([journal], {journal.name: board}, position="DST")

    assert timing.n_room_picks == len(timing.all_room_picks)
    assert timing.n_room_picks == timing.n_seats + 1, "seat 4 holds two defenses"
    assert timing.n_rival_seats == TEAMS - 1
    assert timing.n_seats <= timing.n_rival_seats
    late = max(timing.all_room_picks) + 1
    wide = roomcheck.kdst_timing(
        [journal], {journal.name: board}, position="DST", cuts=(late,)
    )
    assert wide.picks_before[late] == timing.n_room_picks
    assert wide.seats_before[late] == timing.n_seats
    assert wide.seats_before[late] < wide.picks_before[late]


# ------------------------------------------------------- the null control


def _rooms(tmp_path, board, factory, *, n, seed0, name, operator_slot=3):
    journals, boards = [], {}
    for i in range(n):
        path = _make_journal_file(
            tmp_path,
            board,
            factory(),
            operator_slot=operator_slot,
            name=f"{name}{i}.jsonl",
            seed=seed0 + i,
        )
        journal = roomcheck.load_journal(path)
        journals.append(journal)
        boards[journal.name] = board
    return journals, boards


def test_null_control_calls_a_same_process_room_a_model_finding(tmp_path, board):
    """Rooms drawn from the model under test must NOT read as a discovery.

    This is the control that the first version of this module never ran, and
    the reason its headline was mis-attributed: scored against all-bot rooms the
    shipped constants fail almost exactly as badly as they do against humans,
    and a fit that never saw a real draft recovers nearly all of the "real-room"
    re-fit's gain.
    """
    journals, boards = _rooms(
        tmp_path,
        board,
        lambda: [AutodraftBot() for _ in range(TEAMS)],
        n=3,
        seed0=700,
        name="same",
    )
    control = roomcheck.null_control(journals, boards, rng=random.Random(1), drafts=3)
    assert control.drafts == 3
    assert control.share_of_refit_gain_available_offline > 0.6
    assert "OUR OWN MODEL" in control.verdict
    assert control.control_shipped.n > 0
    assert control.real_shipped.brier > control.real_refit.brier


def test_null_control_does_not_explain_away_a_genuinely_different_room(
    tmp_path, board
):
    """...and a room the model does NOT generate must survive the control.

    The control is only worth anything if it can come back negative. Here the
    "real" rooms reach fourteen board slots past the best available every pick —
    behaviour no RankNoiseBot room reproduces — and a control-only fit does not
    recover the gain.
    """
    journals, boards = _rooms(
        tmp_path,
        board,
        lambda: [_OffsetBot(14) for _ in range(TEAMS)],
        n=3,
        seed0=800,
        name="odd",
    )
    control = roomcheck.null_control(journals, boards, rng=random.Random(1), drafts=3)
    assert control.share_of_refit_gain_available_offline < 0.5
    assert "REAL-ROOM" in control.verdict


def test_simulated_control_journals_obey_the_real_journal_invariants(board):
    """The control is only comparable because it goes through the SAME code."""
    controls = roomcheck.simulate_journals(
        board,
        rng=random.Random(2),
        drafts=2,
        roster=ROSTER,
        rounds=ROUNDS,
        operator_slot=4,
        board_as_of=AS_OF,
    )
    assert len(controls) == 2
    for journal in controls:
        assert journal.complete
        assert len(journal.picks) == ROUNDS * TEAMS
        assert journal.operator_slot == 4
        assert all(p.seat != 4 for p in journal.room_picks())
        assert roomcheck.operator_windows(journal, board)
        assert roomcheck.survival_observations(journal, board)
    assert controls[0].picks != controls[1].picks  # independent draws


# -------------------------------------------------------- the prior A/B


def test_prior_variant_names_every_knob_it_moved():
    """The regression this dataclass exists for: a row that moved TWO knobs.

    An A/B row labelled "autodraft_fraction=0.0" whose numbers only reproduced
    with a second change (reach_sigma) read as evidence for moving a shipped
    prior. The diff is derived from the variant now, so a two-knob row cannot be
    described as a one-knob row.
    """
    one = roomcheck.PriorVariant(
        "one knob",
        priors=dataclasses.replace(ROOM_PRIORS_2025, autodraft_fraction=0.0),
    )
    assert one.changes() == ("autodraft_fraction 0.2 -> 0.0",)

    two = roomcheck.PriorVariant(
        "looks like one knob",
        priors=dataclasses.replace(
            roomcheck.MEASURED_ROOM_2026, autodraft_fraction=0.0
        ),
    )
    assert len(two.changes()) == 2
    assert any("reach_sigma" in c for c in two.changes())
    assert any("autodraft_fraction" in c for c in two.changes())

    assert roomcheck.PriorVariant("reference").changes() == ()
    assert roomcheck.PriorVariant("kappa", kappa=1.0).changes() == ("kappa 1.3 -> 1.0",)
    assert roomcheck.PriorVariant("cold", live_recalibration=False).changes() == (
        "live recalibration on -> off",
    )


def test_compare_priors_scores_every_variant_on_the_same_rows(
    autodraft_journal, board
):
    """Unpaired rows at this rollout count would be reporting seeds, not knobs."""
    journals = [autodraft_journal]
    boards = {autodraft_journal.name: board}
    variants = (
        roomcheck.PriorVariant("shipped"),
        roomcheck.PriorVariant("cold", live_recalibration=False),
    )
    rows = roomcheck.compare_priors(
        journals, boards, rng=random.Random(9), variants=variants, rollouts=8
    )
    assert [r.variant.label for r in rows] == ["shipped", "cold"]
    assert rows[0].calibration.n == rows[1].calibration.n > 0
    assert rows[0].calibration.observed_rate == rows[1].calibration.observed_rate
    again = roomcheck.compare_priors(
        journals, boards, rng=random.Random(9), variants=variants, rollouts=8
    )
    assert [r.calibration.brier for r in again] == [
        r.calibration.brier for r in rows
    ]  # deterministic for a fixed seed


# ------------------------------------------------------------ ADP census


def test_adp_census_measures_the_plateau_band_not_an_exact_value():
    """The published census was wrong by ~4x; it is computed now.

    ESPN parks the mostly-undrafted around 170 with jitter, and values run PAST
    it — so an exact-equality count misses most of them, which is precisely how
    the remembered figure (631 at exactly 170.0) both failed to reproduce and
    understated the censoring.
    """
    table = roomcheck.AdpTable(
        by_espn_id={
            "a": 12.0,
            "b": 169.6,
            "c": 170.0,
            "d": 170.0,
            "e": 171.4,
        },
        by_dst_team={"SEA": 88.0},
    )
    census = table.census()
    assert census.n == 6
    assert census.at_plateau == 4  # 169.6, 170.0, 170.0, 171.4
    assert census.exactly_at_ceiling == 2
    assert census.maximum == 171.4
    assert census.plateau_share == pytest.approx(4 / 6)
    assert roomcheck.AdpTable(by_espn_id={}, by_dst_team={}).census().n == 0


def test_adp_coverage_reports_only_board_ranked_players(board):
    priced = {e.player_id for e in board if e.espn_overall_rank <= 20}
    table = roomcheck.AdpTable(
        by_espn_id={pid: 30.0 for pid in priced}, by_dst_team={}
    )
    ranked = [e for e in board if e.espn_overall_rank < roomcheck.FALLBACK_RANK_BASE]
    assert table.coverage(board) == pytest.approx(len(priced) / len(ranked))
    assert roomcheck.AdpTable(by_espn_id={}, by_dst_team={}).coverage(board) == 0.0


def test_adp_rescale_reports_the_slope_a_compressed_room_could_express():
    """A slope compared against 0.833 that could never reach 0.833 is not evidence."""
    compressed = roomcheck.AdpRescale(
        group="K",
        n=10,
        intercept=135.0,
        slope=0.14,
        r_squared=0.73,
        mean_adp=139.8,
        mean_room_pick=154.7,
        adp_span=80.5,
        room_pick_span=10.0,
    )
    assert compressed.max_expressible_slope == pytest.approx(10.0 / 80.5, abs=1e-6)
    assert compressed.max_expressible_slope < 0.833
    text = "\n".join(
        roomcheck.render_report(
            roomcheck.RoomcheckReport(skill_rescale=compressed)
        )
    )
    assert "RANGE RESTRICTION" in text
    assert "0.833" in text

    roomy = roomcheck.AdpRescale(
        group="skill",
        n=133,
        intercept=1.5,
        slope=0.868,
        r_squared=0.97,
        mean_adp=78.4,
        mean_room_pick=69.5,
        adp_span=159.5,
        room_pick_span=138.0,
    )
    assert roomy.max_expressible_slope > 0.833
    roomy_text = "\n".join(
        roomcheck.render_report(roomcheck.RoomcheckReport(skill_rescale=roomy))
    )
    assert "RANGE RESTRICTION" not in roomy_text


# ---------------------------------------------------- the rendered artifact


def test_render_report_keeps_the_autodraft_qualifier(tmp_path, board):
    """Rule 6: the qualifier lives in the OUTPUT, not in a docstring.

    "no rival seat drafts like a pure-board autodrafter" reads to a novice as
    "no seat was on autopilot"; the measurement only supports "not like OUR
    AutodraftBot". The rendered line is the artifact an operator reads ~28 hours
    before a draft, so the qualifier and the fact that the question is already
    closed both belong in it.
    """
    path = _make_journal_file(tmp_path, board, [_OffsetBot(6)] * TEAMS, operator_slot=0)
    journal = roomcheck.load_journal(path)
    report = roomcheck.RoomcheckReport(rooms=(roomcheck.room_fit(journal, board),))
    text = "\n".join(roomcheck.render_report(report))
    assert "AS WE MODEL ONE" in text
    assert "decision-irrelevant" in text
    assert "not a reason to move the prior" in text.lower()


def test_render_report_says_when_the_prior_ab_did_not_run(autodraft_journal, board):
    points = roomcheck.analytic_points(autodraft_journal, board)
    report = roomcheck.RoomcheckReport(
        journals=(autodraft_journal,),
        rollout=roomcheck.calibrate(points, label="rollout"),
    )
    text = "\n".join(roomcheck.render_report(report))
    assert "NOT RUN" in text
    assert "--priors-ab" in text


# ------------------------------------------------- end to end, no live DB


def _fake_espn_rows(board):
    rows = []
    for entry in board:
        rows.append(
            {
                "espn_id": None if entry.position == "DST" else entry.player_id,
                "position": "D/ST" if entry.position == "DST" else entry.position,
                "team": entry.team or "KC",
                "adp": min(170.0, float(entry.espn_overall_rank)),
            }
        )
    return rows


def test_run_roomcheck_end_to_end_on_synthetic_journals(monkeypatch, tmp_path, board):
    """The whole runner, including the board cache and every new section.

    No live database: ``load_board`` and ``get_espn_draft_ranks`` are the only
    two DB seams and both are stubbed here, which also lets the test assert the
    per-(season, as_of) board cache is real — three journals over two days must
    load two boards, not three — while every journal is still verified against
    its own ``board_hash``.
    """
    import ziggurat.data.nfl.espn_ranks as espn_ranks_module
    import ziggurat.draft.simulator as simulator_module

    calls = []

    def fake_load_board(conn, *, as_of, season, **kwargs):
        calls.append((season, as_of))
        return tuple(board)

    monkeypatch.setattr(simulator_module, "load_board", fake_load_board)
    monkeypatch.setattr(
        espn_ranks_module,
        "get_espn_draft_ranks",
        lambda conn, *, as_of, season: _fake_espn_rows(board),
    )

    for i, as_of in enumerate(("2026-08-01", "2026-08-01", "2026-08-15")):
        _make_journal_file(
            tmp_path,
            board,
            [AutodraftBot() for _ in range(TEAMS)],
            operator_slot=2,
            name=f"e2e{i}.jsonl",
            as_of=as_of,
            seed=900 + i,
        )

    report = roomcheck.run_roomcheck(
        None,
        tmp_path,
        rollouts=8,
        sim_drafts=2,
        control_drafts=2,
        prior_variants=(roomcheck.PriorVariant("shipped"),),
        noise_seeds=(4242,),
    )

    assert len(report.journals) == 3
    assert sorted(calls) == [(2026, "2026-08-01"), (2026, "2026-08-15")], (
        "boards must be cached per (season, as_of), not reloaded per journal"
    )
    assert report.rollout is not None and report.rollout_cold is not None
    assert report.rollout.n == report.rollout_cold.n
    assert report.analytic_conditional is not None
    assert report.control is not None and report.control.drafts == 2
    assert set(report.adp_census) == {"2026-08-01", "2026-08-15"}
    assert set(report.adp_coverage) == {"2026-08-01", "2026-08-15"}
    ranked = [e for e in board if e.espn_overall_rank < roomcheck.FALLBACK_RANK_BASE]
    # the synthetic board carries no team abbreviations, so its D/ST rows cannot
    # join ESPN's team-keyed ADP — which is exactly what coverage is for.
    joinable = [e for e in ranked if e.position != "DST"]
    assert report.adp_coverage["2026-08-01"] == pytest.approx(
        len(joinable) / len(ranked)
    )
    assert len(report.prior_ab) == 1
    assert len(report.noise_probe) == 1
    assert report.recalibration_engaged[1] > 0
    assert report.recalibration_engaged[0] > 0

    text = "\n".join(roomcheck.render_report(report))
    for expected in (
        "NULL CONTROL",
        "rollout, LIVE priors",
        "rollout, COLD-START only",
        "analytic, CONDITIONED",
        "undrafted plateau",
        "noise probe",
        "room-prior A/B",
        "rival SEATS whose first",
    ):
        assert expected in text, expected


def test_run_roomcheck_refuses_a_journal_whose_board_hash_disagrees(
    monkeypatch, tmp_path, board
):
    """The board cache must never substitute one draft's board for another's."""
    import ziggurat.data.nfl.espn_ranks as espn_ranks_module
    import ziggurat.draft.simulator as simulator_module

    monkeypatch.setattr(
        simulator_module,
        "load_board",
        lambda conn, *, as_of, season, **kwargs: tuple(board),
    )
    monkeypatch.setattr(
        espn_ranks_module,
        "get_espn_draft_ranks",
        lambda conn, *, as_of, season: _fake_espn_rows(board),
    )
    _make_journal_file(
        tmp_path,
        board,
        [AutodraftBot() for _ in range(TEAMS)],
        operator_slot=2,
        name="good.jsonl",
        seed=950,
    )
    path = _make_journal_file(
        tmp_path,
        board,
        [AutodraftBot() for _ in range(TEAMS)],
        operator_slot=2,
        name="tampered.jsonl",
        seed=951,
    )
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header["board_hash"] = "0" * 16
    lines[0] = json.dumps(header)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(roomcheck.BoardMismatch, match="board_as_of"):
        roomcheck.run_roomcheck(None, tmp_path, rollouts=4, sim_drafts=1, control_drafts=1)


def test_reach_sigma_transfer_recomputes_the_input_output_curve(board):
    """The trap constant's curve must be re-derivable, not just quotable.

    Its first version quoted a 20-draft median to two decimals; those digits
    moved by up to 0.4 between seeds. This pins the two properties the claim
    actually rests on — realized rises with input, and realized is NOT input
    (the room's other machinery compresses it) — and that the curve is
    deterministic for a fixed seed. The floor's actual VALUE (~12.3 on the real
    2026 board) is a property of that board's depth, not of this fixture, so it
    is not asserted here.
    """
    curve = roomcheck.reach_sigma_transfer(
        board,
        rng=random.Random(6),
        inputs=(4.0, 30.0),
        drafts=3,
        roster=ROSTER,
        rounds=ROUNDS,
    )
    assert [inp for inp, _out in curve] == [4.0, 30.0]
    low, high = (out for _inp, out in curve)
    assert high > low, "a wider input must realize a wider spread"
    assert high < 30.0, "the room's machinery compresses the input sigma"
    assert abs(low - 4.0) > 0.2, "realized is not a read-back of the input"
    assert curve == roomcheck.reach_sigma_transfer(
        board, rng=random.Random(6), inputs=(4.0, 30.0), drafts=3,
        roster=ROSTER, rounds=ROUNDS,
    )
