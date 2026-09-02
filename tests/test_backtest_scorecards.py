"""Item 4.1 — the GRADE phase (backtest/scorecards.py) and backtest/stats.py.

Every scorecard branch is exercised on a SYNTHETIC panel inserted straight
into the in-memory schema, so each test states the market movement it
expects and nothing else moves.  Decisions are built by hand: the grade
phase must never need the generator.
"""

from __future__ import annotations

import math

import pytest

from backtest import decisions as D
from backtest import scorecards as S
from backtest import stats

SEASON, WEEK = 2023, 6
AS_OF = "2023-10-17"            # Tuesday after week 6's Monday game
GRADE_AS_OF = "2024-02-28"
R0, R1, R2 = "2023-10-13", "2023-10-20", "2023-10-27"
BULK = "2026-08-30"
PARAMS = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(SEASON,))


# ------------------------------------------------------------------ helpers


def _page(db, *, week, scrape, ranks, size=40, ecr_type="wp", page="ppr-rb",
          position="RB", team_of=None, retrieved=BULK):
    """Insert one market page: ``ranks`` maps gsis -> page_rank; the other
    ranks up to ``size`` are filled with anonymous filler players so the page
    size (and thus the unranked entry rank) is exact.  ``team_of`` maps gsis
    -> team; fillers sit on team ``FIL``."""
    used = set(ranks.values())
    assert len(used) == len(ranks), "ranks must be distinct"
    assert all(1 <= r <= size for r in used)
    rows = {gsis: rank for gsis, rank in ranks.items()}
    filler = iter(f"filler-{i}" for i in range(size + 1))
    for rank in range(1, size + 1):
        if rank not in used:
            rows[next(filler)] = rank
    team_of = team_of or {}
    for gsis, rank in rows.items():
        team = team_of.get(gsis, "FIL")
        db.execute(
            "INSERT INTO fpecr_panel (fantasypros_id, ecr_type, fp_page, scrape_date, season, "
            "nfl_week, week_basis, player, position, team, gsis_id, page_rank, pos_rank, "
            "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"fp-{gsis}", ecr_type, page, scrape, SEASON, week, "inferred", gsis, position,
             team, gsis, rank, rank, retrieved, scrape),
        )
    db.commit()


def _stat(db, gsis, *, week=WEEK, position="RB", team="BUF", carries=5, targets=2,
          knowable="2023-10-15", retrieved="2026-07-25"):
    db.execute(
        "INSERT INTO weekly_stats (player_id, season, week, season_type, position, recent_team, "
        "carries, targets, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (gsis, SEASON, week, "REG", position, team, carries, targets, retrieved, knowable),
    )
    db.commit()


def _owned(db, *, week, sleeper_id, gsis, owned, knowable, retrieved="2026-09-01"):
    db.execute(
        "INSERT INTO sleeper_ownership (season, season_type, week, sleeper_id, gsis_id, position, "
        "team, owned_pct, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (SEASON, "regular", week, sleeper_id, gsis, "RB", "BUF", owned, retrieved, knowable),
    )
    db.commit()


def _decision(gsis, *, rank_in_board=1, r0_rank=None, page_size=40, team="BUF",
              position="RB", week=WEEK, as_of=AS_OF, strategy="signal_topk", r0_scrape=R0):
    return D.Decision(
        season=SEASON, week=week, as_of=as_of, strategy=strategy,
        rank_in_board=rank_in_board, board_rank=rank_in_board, gsis_id=gsis, espn_id=None,
        player=gsis, position=position, team=team, signal_kind="USAGE_BREAKOUT",
        magnitude=1.0, market_rank_r0=r0_rank, market_page_size_r0=page_size,
        r0_scrape_date=r0_scrape if page_size is not None else None,
        reasons=(f"reason for {gsis}",),
    )


def _crosswalk(db, gsis, *, sleeper_id):
    """A players row that says Sleeper CAN report this gsis (item STAT-3)."""
    db.execute(
        "INSERT INTO players (gsis_id, sleeper_id, name, position, retrieved_as_of, "
        "knowable_as_of) VALUES (?,?,?,?,?,?)",
        (gsis, sleeper_id, gsis, "RB", BULK, "2020-01-01"),
    )
    db.commit()


def _record(decisions, *, week=WEEK, as_of=AS_OF, status=D.WEEK_DECIDED, reason=None,
            strategy="signal_topk"):
    return D.WeekRecord(
        season=SEASON, week=week, as_of=as_of, strategy=strategy, k=3, status=status,
        reason=reason, decisions=tuple(decisions), generator_rows=10, usage_rows=8,
        pool_size=5, excluded_injury=1, excluded_qb1=1, excluded_ineligible=2,
        excluded_no_gsis=0, excluded_position=1, r0_scrape_date=R0, r0_pages=("ppr-rb",),
        log_lines=(("WARNING: crosswalk: espn_id # maps to multiple gsis (...); keeping first", 3),),
    )


def _card(db, records, **kw):
    kw.setdefault("strategy", "signal_topk")
    kw.setdefault("market", "wp")
    kw.setdefault("grade_as_of", GRADE_AS_OF)
    return S.build_scorecard(db, records, PARAMS, **kw)


def _pick(card, gsis):
    (p,) = [p for p in card.picks if p.gsis_id == gsis]
    return p


@pytest.fixture()
def panel(db):
    """The standard three-page world: A hits at r1, B first hits at r2, C is
    unranked at r0 and enters, N never moves, E's team is on bye at r1."""
    teams = {"A": "BUF", "B": "MIA", "C": "NYJ", "N": "NE", "E": "KC"}
    _page(db, week=WEEK, scrape=R0, ranks={"A": 30, "B": 30 - 1, "N": 20, "E": 35},
          team_of=teams)
    # r1: A moves 30 -> 19 (11 places: hit at H<=10), B 29 -> 27 (2: miss),
    #     C unranked (entry 41) -> 34 (7: hit at H<=7), N 20 -> 20, E absent and KC off page
    _page(db, week=WEEK + 1, scrape=R1, ranks={"A": 19, "B": 27, "C": 34, "N": 20},
          team_of=teams)
    # r2: B 29 -> 15 (first hit), E 35 -> 25 (hit), A 30 -> 22, C 41 -> 33, N 20 -> 21
    _page(db, week=WEEK + 2, scrape=R2, ranks={"A": 22, "B": 15, "C": 33, "N": 21, "E": 25},
          team_of=teams)
    return teams


# ------------------------------------------------------------ hit + lead


def test_hit_at_r1_is_lead_1_concurrent(db, panel):
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.gradeability == S.G_GRADEABLE
    assert (p.status_l1, p.rank_l1) == (S.S_HIT, 19)
    assert p.hit is True and p.lead == 1
    assert card.overall.lead1 == 1 and card.overall.lead2 == 0
    assert "CONCURRENT" in S.LEAD_LABELS[1]


def test_first_hit_at_r2_is_lead_2_a_genuine_lead(db, panel):
    card = _card(db, [_record([_decision("B", r0_rank=29)])])
    p = _pick(card, "B")
    assert p.status_l1 == S.S_MISS and p.status_l2 == S.S_HIT
    assert p.hit is True and p.lead == 2
    assert card.overall.lead1 == 0 and card.overall.lead2 == 1


def test_no_hit_is_a_graded_miss_not_an_ungradeable_pick(db, panel):
    card = _card(db, [_record([_decision("N", r0_rank=20)])])
    p = _pick(card, "N")
    assert p.gradeable and p.hit is False and p.lead is None
    assert card.overall.gradeable == 1
    assert card.overall.precision_at(1).mean == 0.0


def test_unranked_at_r0_enters_one_past_the_page_and_can_hit(db, panel):
    card = _card(db, [_record([_decision("C", r0_rank=None, page_size=40)])])
    p = _pick(card, "C")
    assert p.r0_rank is None and p.entry_rank == 41
    assert p.status_l1 == S.S_HIT and p.rank_l1 == 34 and p.lead == 1
    # ... and the threshold matters: 7 places is not 8
    strict = _card(db, [_record([_decision("C", r0_rank=None, page_size=40)])], places=8)
    assert _pick(strict, "C").status_l1 == S.S_MISS


def test_bye_at_r1_is_reported_not_counted_as_a_miss_on_the_weekly_market(db, panel):
    card = _card(db, [_record([_decision("E", r0_rank=35, team="KC")])])
    p = _pick(card, "E")
    assert p.status_l1 == S.S_BYE and p.status_l2 == S.S_HIT
    # a hit — but the T+2 page is the market's FIRST rankable scrape, so the
    # lead is unmeasurable: bye-deferred, never a one-week lead (STAT-1)
    assert p.hit is True and p.lead == S.LEAD_BYE_DEFERRED
    assert card.overall.bye_at_l1 == 1
    assert card.overall.lead2 == 0 and card.overall.lead_bye_deferred == 1
    assert card.overall.lead1 == 0
    text = S.render(card)
    assert "lead2=0 (one-week lead)  bye-deferred=1  bye at lead1=1" in text
    (row,) = [w for w in card.weeks if w.week == WEEK]
    assert (row.lead1, row.lead2, row.lead_bye_deferred) == (0, 0, 1)
    # the ROS market keeps bye teams, so an absence there is a plain miss
    ros = _card(db, [_record([_decision("E", r0_rank=35, team="KC")])], market="ros")
    assert _pick(ros, "E").gradeability == S.G_NO_REFERENCE  # no ros pages inserted at all


def test_lead_of_never_calls_a_bye_deferred_hit_a_one_week_lead():
    assert S.lead_of(S.S_BYE, S.S_HIT) == S.LEAD_BYE_DEFERRED
    assert S.lead_of(S.S_BYE, S.S_HIT) != 2
    assert S.lead_of(S.S_MISS, S.S_HIT) == 2
    assert S.lead_of(S.S_ABSENT, S.S_HIT) == 2
    assert S.lead_of(S.S_HIT, S.S_MISS) == 1
    assert S.lead_of(S.S_MISS, S.S_MISS) is None
    assert S.lead_of(S.S_BYE, S.S_MISS) is None
    assert "BYE-DEFERRED" in S.LEAD_LABELS[S.LEAD_BYE_DEFERRED]
    assert "never counted" in S.LEAD_LABELS[S.LEAD_BYE_DEFERRED]


def test_absent_from_a_page_whose_team_is_present_is_a_miss(db, panel):
    # a BUF player unranked at r1 while BUF is on the page: dropped, not bye
    _page(db, week=WEEK, scrape="2023-10-12", ranks={"Z": 38}, team_of={"Z": "BUF"},
          page="ppr-wr", position="WR")
    _page(db, week=WEEK + 1, scrape=R1, ranks={"other": 1}, team_of={"other": "BUF"},
          page="ppr-wr", position="WR")
    _page(db, week=WEEK + 2, scrape=R2, ranks={"other": 1}, team_of={"other": "BUF"},
          page="ppr-wr", position="WR")
    card = _card(db, [_record([_decision("Z", r0_rank=38, position="WR",
                                         r0_scrape="2023-10-12")])])
    p = _pick(card, "Z")
    assert p.status_l1 == S.S_ABSENT and p.status_l2 == S.S_ABSENT
    assert p.gradeable and p.hit is False


# -------------------------------------------------------- gradeability


def test_no_reference_page_makes_the_pick_ungradeable_but_counted(db):
    # no pages at all for this week
    card = _card(db, [_record([_decision("A", r0_rank=None, page_size=None)])])
    p = _pick(card, "A")
    assert p.gradeability == S.G_NO_REFERENCE and p.hit is None and p.lead is None
    s = card.overall
    assert s.decisions == 1 and s.gradeable == 0
    assert dict(s.ungradeable) == {S.G_NO_REFERENCE: 1}
    assert s.precision_at(1).n == 0
    assert "n/a (0 gradeable)" in S.render(card)


def test_missing_r2_page_is_truncated_and_still_graded_on_r1(db):
    _page(db, week=WEEK, scrape=R0, ranks={"A": 30})
    _page(db, week=WEEK + 1, scrape=R1, ranks={"A": 20})
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.gradeability == S.G_TRUNCATED_R2 and p.gradeable
    assert p.status_l2 == S.S_NO_PAGE and p.hit is True and p.lead == 1
    # RULE6-1: graded on ONE scrape is disclosed, not folded into 'gradeable'
    assert card.overall.truncated == ((S.G_TRUNCATED_R2, 1),)
    text = S.render(card)
    assert "truncated_r2=1" in text and "lead2 impossible" in text
    assert "graded on ONE scrape" in text


def test_missing_r1_page_is_truncated_the_other_way_and_disclosed(db):
    _page(db, week=WEEK, scrape=R0, ranks={"A": 30})
    _page(db, week=WEEK + 2, scrape=R2, ranks={"A": 20})
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.gradeability == S.G_TRUNCATED_R1 and p.gradeable
    assert p.status_l1 == S.S_NO_PAGE and p.hit is True
    assert card.overall.truncated == ((S.G_TRUNCATED_R1, 1),)
    text = S.render(card)
    assert "truncated_r1=1" in text and "lead1 impossible" in text
    assert "truncated_r2" not in text.split("HYPOTHESES")[0]


def test_no_rerank_pages_at_all_is_ungradeable(db):
    _page(db, week=WEEK, scrape=R0, ranks={"A": 30})
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    assert _pick(card, "A").gradeability == S.G_NO_RERANK
    assert card.overall.gradeable == 0


def test_an_ungradeable_week_is_reported_by_count_and_reason(db, panel):
    failed = _record([], week=WEEK + 1, as_of="2023-10-24", status=D.WEEK_GENERATOR_FAILED,
                     reason="NoCompletedWeek: synthetic")
    card = _card(db, [_record([_decision("A", r0_rank=30)]), failed])
    assert card.generator_failures == (("generator_failed: NoCompletedWeek: synthetic", 1),)
    s = card.overall
    assert s.weeks_total == 2 and s.weeks_decided == 1
    assert s.weeks_undecided == (("generator_failed: NoCompletedWeek: synthetic", 1),)
    text = S.render(card)
    assert "UNGRADEABLE: generator_failed: NoCompletedWeek: synthetic" in text
    assert "1 x generator_failed: NoCompletedWeek: synthetic" in text


# ------------------------------------------------------------ refusals


def test_k_above_three_is_refused_at_the_parameters_and_at_the_summary(db, panel):
    with pytest.raises(ValueError, match="1..3"):
        D.ReplayParams(strategies=("signal_topk",), k=4, seasons=(SEASON,))
    with pytest.raises(ValueError, match="1..3"):
        D.ReplayParams(strategies=("signal_topk",), k=0, seasons=(SEASON,))
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    with pytest.raises(S.GradeInputError, match="defined for k in"):
        card.overall.precision_at(4)
    # a k INSIDE the defined range but above the replay's own k is a different
    # refusal: the summary never computed it
    for k in (1, 2):
        params = D.ReplayParams(strategies=("signal_topk",), k=k, seasons=(SEASON,))
        small = S.build_scorecard(
            db, [_record([_decision("A", r0_rank=30)])], params,
            strategy="signal_topk", market="wp", grade_as_of=GRADE_AS_OF,
        )
        assert small.overall.precision_at(k).n == 1
        with pytest.raises(S.GradeInputError, match="not computed"):
            small.overall.precision_at(k + 1)


def test_grade_clock_at_or_before_a_decision_clock_is_refused(db, panel):
    recs = [_record([_decision("A", r0_rank=30)])]
    with pytest.raises(S.GradeInputError, match="strictly later"):
        _card(db, recs, grade_as_of=AS_OF)
    with pytest.raises(S.GradeInputError, match="strictly later"):
        _card(db, recs, grade_as_of="2023-10-01")
    assert _card(db, recs, grade_as_of="2023-10-18").overall.decisions == 1


def test_a_reference_that_moved_under_the_freeze_is_refused(db, panel):
    # the frozen decision claims rank 31 but the page says 30
    with pytest.raises(S.GradeInputError, match="changed under the freeze"):
        _card(db, [_record([_decision("A", r0_rank=31)])])
    # ... and a page-size disagreement is the same refusal (RIGOR-1: the
    # second half of the frozen tuple is checked too)
    with pytest.raises(S.GradeInputError, match="changed under the freeze"):
        _card(db, [_record([_decision("A", r0_rank=30, page_size=41)])])


def test_unknown_market_and_bad_threshold_are_refused(db, panel):
    recs = [_record([_decision("A", r0_rank=30)])]
    with pytest.raises(S.GradeInputError, match="unknown market"):
        _card(db, recs, market="dynasty")
    with pytest.raises(S.GradeInputError, match=">= 1"):
        _card(db, recs, places=0)
    with pytest.raises(S.GradeInputError, match="no frozen records"):
        _card(db, recs, strategy="random_k")


# --------------------------------------------------------- precision@k


def test_precision_at_k_truncates_one_frozen_set(db, panel):
    recs = [_record([
        _decision("A", rank_in_board=1, r0_rank=30),   # hit
        _decision("N", rank_in_board=2, r0_rank=20),   # miss
        _decision("B", rank_in_board=3, r0_rank=29),   # hit (lead 2)
    ])]
    s = _card(db, recs).overall
    assert s.precision == ((1, 1, 1), (2, 1, 2), (3, 2, 3))
    assert s.precision_at(1).mean == 1.0
    assert s.precision_at(2).mean == 0.5
    assert s.precision_at(3).mean == pytest.approx(2 / 3)
    assert s.precision_at(3).kind == "wilson"


# ------------------------------------------------------------ base rate


def test_base_rate_is_the_same_rule_over_the_matched_stat_line_universe(db, panel):
    for gsis, team in panel.items():
        _stat(db, gsis, team=team)
    _stat(db, "Q", position="QB", team="NE")             # wrong position: out
    _stat(db, "ghost", carries=0, targets=0)             # no touches: out
    _stat(db, "star", team="BUF")                         # in the universe ...
    # a different page, scraped the SAME day as r0 so the last-scrape dedupe cannot
    # hide it: only read_reference's fp_page filter keeps star's rank-3 row out
    # (RIGOR-3: the dup page must stay in the table for that filter to be under test)
    _page(db, week=WEEK, scrape=R0, ranks={"star": 3}, team_of={"star": "BUF"},
          page="ppr-rb-dup")
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    (n,) = [w for w in card.weeks if w.week == WEEK]
    s = card.overall
    # universe: A B C N E star = 6 RB lines with touches (QB Q and ghost excluded)
    assert s.null_universe == 6
    # 'star' is unranked at r0 (eligible), 'N' is rank 20 <= 24 (INELIGIBLE)
    assert s.null_eligible == 5
    # gradeable: A(hit L1) B(hit L2) C(hit L1) E(bye then hit L2) star(unranked, absent both:
    # BUF is on both pages -> miss)
    assert s.null_gradeable == 5 and s.null_hits == 4
    assert s.null_rate == pytest.approx(0.8)
    assert n.null_rate == pytest.approx(0.8)
    # the pick's lift is hit - base = 1 - 0.8
    assert s.lift_pooled is not None and s.lift_pooled.mean == pytest.approx(0.2)
    assert s.lift_pooled.kind == "pooled per-pick"
    assert s.lift_block is not None and s.lift_block.n == 1 and s.lift_block.is_degenerate
    # STAT-1: E's null hit is bye-deferred, not a one-week lead; B's is lead 2
    assert (s.null_lead1, s.null_lead2, s.null_lead_bye_deferred) == (2, 1, 1)
    text = S.render(card)
    assert "base rate (eligibility-matched null (week-T universe; NOT depth-matched" in text
    assert "+20.0pp" in text
    assert "null lead1=2 lead2=1 bye-deferred=1" in text
    # STAT-4: the two lift lines say what each centre is
    assert "per-pick vs OWN-WEEK null (pooled t, pseudo-replicated)" in text
    assert "season-block = season p@3 - season pooled null (t, df=seasons-1)" in text
    # a single season's block centre is exactly p@k - base
    assert s.lift_block.mean == pytest.approx(s.precision_at(3).mean - s.null_rate)


def test_a_week_with_no_stat_lines_has_no_base_rate_and_no_lift(db, panel):
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    assert card.overall.null_gradeable == 0 and card.overall.null_rate is None
    assert card.overall.lift_pooled is None and card.overall.lift_block is None
    assert "n/a" in S.render(card)


# -------------------------------------------------------- corroboration


def test_ownership_table_empty_for_the_season_degrades_to_a_printed_reason(db, panel):
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.corroboration == S.C_UNAVAILABLE and p.owned_delta is None
    reason = f"unavailable: sleeper_ownership missing or empty for {SEASON}"
    assert card.overall.corroboration_unavailable == (reason,)
    assert reason in S.render(card)


def test_ownership_delta_corroborates_at_the_threshold_and_not_below(db, panel):
    _owned(db, week=WEEK, sleeper_id="s-A", gsis="A", owned=5.0, knowable="2023-10-16")
    _owned(db, week=WEEK + 1, sleeper_id="s-A", gsis="A", owned=25.0, knowable="2023-10-23")
    _owned(db, week=WEEK, sleeper_id="s-B", gsis="B", owned=40.0, knowable="2023-10-16")
    _owned(db, week=WEEK + 1, sleeper_id="s-B", gsis="B", owned=44.0, knowable="2023-10-23")
    # C is below the censor floor at T (absent) and appears at T+1: imputed at 1.0
    _owned(db, week=WEEK + 1, sleeper_id="s-C", gsis="C", owned=9.0, knowable="2023-10-23")
    recs = [_record([
        _decision("A", rank_in_board=1, r0_rank=30),
        _decision("B", rank_in_board=2, r0_rank=29),
        _decision("C", rank_in_board=3, r0_rank=None),
        ])]
    card = _card(db, recs)
    assert _pick(card, "A").corroboration == S.C_YES
    assert _pick(card, "A").owned_delta == pytest.approx(20.0)
    assert _pick(card, "B").corroboration == S.C_NO
    assert _pick(card, "C").corroboration == S.C_NO       # 9 - 1 = 8 < 10
    assert _pick(card, "C").owned_delta == pytest.approx(8.0)
    s = card.overall
    assert (s.corroborated, s.corroboration_covered) == (1, 3)
    # sensitivity: at D=5 C corroborates too; at D=20 only A (>=)
    by_d = {(r.owned_delta, r.split): r for r in card.owned_sensitivity}
    assert by_d[(5.0, "ALL")].corroborated == 2
    assert by_d[(20.0, "ALL")].corroborated == 1
    # a player with no snapshot on either side is not covered, not a zero
    card2 = _card(db, [_record([_decision("N", r0_rank=20)])])
    assert _pick(card2, "N").corroboration == S.C_NOT_COVERED


def test_ownership_without_a_next_week_snapshot_is_not_covered(db, panel):
    _owned(db, week=WEEK, sleeper_id="s-A", gsis="A", owned=5.0, knowable="2023-10-16")
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    assert _pick(card, "A").corroboration == S.C_NOT_COVERED
    # ... even when the player is crosswalked: no T+1 grid means no delta
    _crosswalk(db, "A", sleeper_id="s-A")
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    assert _pick(card, "A").corroboration == S.C_NOT_COVERED


def _both_grids(db):
    # both snapshots exist (some OTHER player is in each), so absence from
    # the deltas is a statement about the player, not about the week
    _owned(db, week=WEEK, sleeper_id="s-X", gsis="X", owned=50.0, knowable="2023-10-16")
    _owned(db, week=WEEK + 1, sleeper_id="s-X", gsis="X", owned=55.0, knowable="2023-10-23")


def test_a_crosswalked_player_absent_from_both_grids_is_a_real_zero_delta(db, panel):
    # STAT-3 (a): Sleeper CAN report A (players.sleeper_id exists) and did not
    # move him above the 1% floor either week -> the crowd did not act: a
    # covered NOT_CORROBORATED with an imputed 0.0, disclosed as such
    _both_grids(db)
    _crosswalk(db, "A", sleeper_id="s-A")
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.corroboration == S.C_NO and p.owned_delta == 0.0 and p.owned_imputed
    assert (card.overall.corroborated, card.overall.corroboration_covered) == (0, 1)
    text = S.render(card, reasons=True)
    assert "(+0.0pt, below the 1% floor both weeks)" in text
    assert "(0/1 covered, graded picks)" in text


def test_an_uncrosswalked_player_absent_from_both_grids_is_not_covered(db, panel):
    # STAT-3 (b): no players.sleeper_id -> Sleeper could never have reported
    # him; absence is unknown, not zero
    _both_grids(db)
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.corroboration == S.C_NOT_COVERED and p.owned_delta is None
    assert not p.owned_imputed
    assert card.overall.corroboration_covered == 0


def test_an_ungradeable_pick_is_outside_the_corroboration_denominator(db, panel):
    # STAT-3 (c): the pick rate is over GRADED picks, like the null's lines
    _both_grids(db)
    _crosswalk(db, "A", sleeper_id="s-A")
    _crosswalk(db, "G", sleeper_id="s-G")
    _owned(db, week=WEEK, sleeper_id="s-G", gsis="G", owned=5.0, knowable="2023-10-16")
    _owned(db, week=WEEK + 1, sleeper_id="s-G", gsis="G", owned=30.0, knowable="2023-10-23")
    # G has no page at all on the WR market -> no_reference, though the crowd
    # corroborated him loudly (+25)
    recs = [_record([
        _decision("A", rank_in_board=1, r0_rank=30),
        _decision("G", rank_in_board=2, r0_rank=None, page_size=None, position="WR"),
    ])]
    card = _card(db, recs)
    g = _pick(card, "G")
    assert g.gradeability == S.G_NO_REFERENCE and g.corroboration == S.C_YES
    s = card.overall
    assert s.decisions == 2 and s.gradeable == 1
    assert (s.corroborated, s.corroboration_covered) == (0, 1)
    assert "(0/1 covered, graded picks)" in S.render(card)


def test_null_coverage_moves_by_exactly_the_crosswalked_absent_lines(db, panel):
    # STAT-3 (d): the null's covered count includes a crosswalked line absent
    # from both grids (a real 0.0) and excludes an un-crosswalked one
    _both_grids(db)
    for gsis, team in panel.items():
        _stat(db, gsis, team=team)
    before = _card(db, [_record([_decision("A", r0_rank=30)])]).overall
    assert before.null_gradeable == 4 and before.null_corroboration_covered == 0
    _crosswalk(db, "A", sleeper_id="s-A")
    _crosswalk(db, "B", sleeper_id="s-B")
    after = _card(db, [_record([_decision("A", r0_rank=30)])]).overall
    assert after.null_gradeable == 4
    assert after.null_corroboration_covered == before.null_corroboration_covered + 2
    assert after.null_corroborated == 0
    assert "gradeable lines)" in S.render(_card(db, [_record([_decision("A", r0_rank=30)])]))


# ----------------------------------------------------------- splits


def test_holdout_seasons_are_labelled_and_banner_printed(db, panel):
    hold = 2024
    for week, scrape in ((6, "2024-10-11"), (7, "2024-10-18"), (8, "2024-10-25")):
        db.execute(
            "INSERT INTO fpecr_panel (fantasypros_id, ecr_type, fp_page, scrape_date, season, "
            "nfl_week, week_basis, player, position, team, gsis_id, page_rank, pos_rank, "
            "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("fp-H", "wp", "ppr-rb", scrape, hold, week, "inferred", "H", "RB", "BUF", "H",
             30 if week == 6 else 10, 1, BULK, scrape),
        )
    db.commit()
    params = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(SEASON, hold))
    hold_dec = D.Decision(
        season=hold, week=6, as_of="2024-10-15", strategy="signal_topk", rank_in_board=1,
        board_rank=1, gsis_id="H", espn_id=None, player="H", position="RB", team="BUF",
        signal_kind="USAGE_BREAKOUT", magnitude=1.0, market_rank_r0=30,
        market_page_size_r0=1, r0_scrape_date="2024-10-11", reasons=("r",),
    )
    hold_rec = D.WeekRecord(
        season=hold, week=6, as_of="2024-10-15", strategy="signal_topk", k=3,
        status=D.WEEK_DECIDED, reason=None, decisions=(hold_dec,), generator_rows=1,
        usage_rows=1, pool_size=1, excluded_injury=0, excluded_qb1=0, excluded_ineligible=0,
        excluded_no_gsis=0, excluded_position=0, r0_scrape_date="2024-10-11",
        r0_pages=("ppr-rb",), log_lines=(),
    )
    # HOLD-1: a holdout season is REFUSED before any read unless unlocked ...
    with pytest.raises(D.HoldoutLocked, match="HOLDOUT"):
        S.build_scorecard(
            db, [_record([_decision("A", r0_rank=30)]), hold_rec], params,
            strategy="signal_topk", market="wp", grade_as_of="2025-02-28",
        )
    # ... whether the season arrives in params or only in the records
    train_params = D.ReplayParams(strategies=("signal_topk",), k=3, seasons=(SEASON,))
    with pytest.raises(D.HoldoutLocked, match="2024"):
        S.build_scorecard(
            db, [_record([_decision("A", r0_rank=30)]), hold_rec], train_params,
            strategy="signal_topk", market="wp", grade_as_of="2025-02-28",
        )
    card = S.build_scorecard(
        db, [_record([_decision("A", r0_rank=30)]), hold_rec], params,
        strategy="signal_topk", market="wp", grade_as_of="2025-02-28",
        unlock_holdout=True,
    )
    splits = {s.split: s for s in card.splits}
    assert set(splits) == {"TRAIN", "HOLDOUT", "ALL"}
    assert splits["TRAIN"].seasons == (SEASON,) and splits["HOLDOUT"].seasons == (hold,)
    assert splits["ALL"].decisions == 2
    assert [s.split for s in card.seasons] == ["TRAIN", "HOLDOUT"]
    text = S.render(card)
    assert "holdout seasons present: do not tune thresholds on these" in text
    assert "HOLDOUT 2024-25" in text and "TRAIN 2021-23" in text


# ------------------------------------------------------ determinism


def test_render_is_deterministic_and_carries_reasons_and_hypotheses(db, panel):
    recs = [_record([_decision("A", r0_rank=30), _decision("B", rank_in_board=2, r0_rank=29)])]
    a = S.render(_card(db, recs), reasons=True)
    b = S.render(_card(db, recs), reasons=True)
    assert a == b
    assert "reason for A" in a and "reason for B" in a
    assert "HIT (hypothesis" in a and "CORROBORATION (hypothesis" in a
    assert "ELIGIBILITY (hypothesis" in a
    assert "per-pick vs OWN-WEEK null (pooled t, pseudo-replicated)" in a
    assert "season-block = season p@3 - season pooled null (t, df=seasons-1)" in a
    assert "crosswalk: espn_id # maps to multiple gsis" in a  # log lines summarised
    assert "3 x WARNING" in a
    hyp = a.split("HYPOTHESES")[1]
    # LEAK-4: the scorecard says which page r0 is (the Friday OF week T)
    assert "after Thursday" in hyp
    # STAT-5: the HIT line carries the depth caveat
    hit_line = [ln for ln in hyp.splitlines() if ln.strip().startswith("- HIT (hypothesis")][0]
    assert "depth-matched lift" in hit_line and "weak evidence deeper" in hit_line
    # STAT-2 / STAT-4: DEPTH and the interval-centre disclosure are hypotheses
    assert "- DEPTH (hypothesis" in hyp
    assert "The two centres differ by construction" in hyp
    # SEAM-1: the generator setting is printed beside ELIGIBILITY
    assert "GENERATOR" in hyp and "Part of the cache key" in hyp
    # RULE6-2: the definition line sits BEFORE the first number, and says the
    # thing a novice must know about lead 1
    defn = a.index("HIT = the player moves >= 5 places up the wp page")
    assert defn < a.index("precision@")
    assert "does NOT beat the market" in a
    assert a.index("* = interval excludes zero") < a.index("PER WEEK")
    assert "r1 r2=post-flag scrape dates (- = no page)" in a
    assert "L1/L2=hits at lead 1/2" in a
    # STAT-2 (c): no bare 'matched null' anywhere
    for line in a.splitlines():
        if "matched null" in line:
            assert "eligibility-matched null" in line or "depth-matched" in line.lower(), line
    assert "DEPTH-MATCHED per-pick" in a and "DEPTH-MATCHED season-block" in a


def test_reference_dedupes_to_the_last_scrape_of_the_week(db):
    _page(db, week=WEEK, scrape="2023-10-10", ranks={"A": 5}, size=10)   # Tuesday
    _page(db, week=WEEK, scrape=R0, ranks={"A": 9}, size=12)              # Friday
    ref = S.read_reference(db, market=S.MARKETS["wp"], season=SEASON, week=WEEK,
                           position="RB", as_of=GRADE_AS_OF)
    assert ref is not None and ref.scrape_date == R0
    assert ref.ranks["A"] == 9 and ref.page_size == 12
    # not_after rebuilds what a decision clock saw
    early = S.read_reference(db, market=S.MARKETS["wp"], season=SEASON, week=WEEK,
                             position="RB", as_of=GRADE_AS_OF, not_after="2023-10-11")
    assert early is not None and early.scrape_date == "2023-10-10" and early.ranks["A"] == 5
    assert S.read_reference(db, market=S.MARKETS["wp"], season=SEASON, week=WEEK,
                            position="K", as_of=GRADE_AS_OF) is None


# ------------------------------------------- decision clock vs lead pages


def test_a_lead_page_scraped_at_or_before_the_decision_clock_is_ungradeable(db):
    # LEAK-3 (a): the T+1 page was scraped ON the decision clock -> it is not
    # a post-flag observation; the pick is refused as a grade, never a hit
    _page(db, week=WEEK, scrape=R0, ranks={"A": 30})
    _page(db, week=WEEK + 1, scrape=AS_OF, ranks={"A": 19})
    _page(db, week=WEEK + 2, scrape=R2, ranks={"A": 22})
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.status_l1 == S.S_PRECEDES and p.rank_l1 is None
    assert p.gradeability == S.G_REFERENCE_PRECEDES
    assert p.hit is None and p.lead is None and not p.gradeable
    s = card.overall
    assert s.gradeable == 0 and dict(s.ungradeable) == {S.G_REFERENCE_PRECEDES: 1}
    assert f"ungradeable picks: {S.G_REFERENCE_PRECEDES}=1" in S.render(card)


def test_the_null_excludes_the_same_preceding_lines_as_the_picks(db):
    # LEAK-3 (b): the matched null moves with the picks
    _page(db, week=WEEK, scrape=R0, ranks={"A": 30, "B": 31}, team_of={"A": "BUF", "B": "BUF"})
    _page(db, week=WEEK + 1, scrape=AS_OF, ranks={"A": 19, "B": 20},
          team_of={"A": "BUF", "B": "BUF"})
    _page(db, week=WEEK + 2, scrape=R2, ranks={"A": 22, "B": 23}, team_of={"A": "BUF", "B": "BUF"})
    _stat(db, "A")
    _stat(db, "B")
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    s = card.overall
    assert s.null_eligible == 2 and s.null_gradeable == 0 and s.null_rate is None
    (row,) = [w for w in card.weeks if w.week == WEEK]
    assert row.null_gradeable == 0
    assert s.lift_pooled is None


def test_a_lead_page_one_day_after_the_clock_still_grades_lead_1(db):
    # LEAK-3 (c): the control — strictly after the clock is a real observation
    _page(db, week=WEEK, scrape=R0, ranks={"A": 30})
    _page(db, week=WEEK + 1, scrape="2023-10-18", ranks={"A": 19})
    _page(db, week=WEEK + 2, scrape=R2, ranks={"A": 22})
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.gradeability == S.G_GRADEABLE and p.hit is True and p.lead == 1


def test_r0_is_read_gated_to_the_decision_clock_and_checked_against_the_freeze(db, panel):
    # LEAK-5 (a): the week-T page is re-scraped AFTER the clock at a different
    # rank; the grade must come from the scrape the decision saw, no raise
    _page(db, week=WEEK, scrape="2023-10-18", ranks={"A": 10, "B": 11, "N": 12, "E": 13},
          team_of=panel)
    card = _card(db, [_record([_decision("A", r0_rank=30)])])
    p = _pick(card, "A")
    assert p.r0_scrape == R0 and p.r0_rank == 30 and p.entry_rank == 30
    assert p.status_l1 == S.S_HIT and p.rank_l1 == 19
    # LEAK-5 (b): a frozen r0_scrape_date that disagrees with the gated read
    # is refused — the freeze and the panel no longer describe the same page
    with pytest.raises(S.GradeInputError, match="r0 reference changed under the freeze"):
        _card(db, [_record([_decision("A", r0_rank=30, r0_scrape="2023-10-12")])])


# -------------------------------------------------------- depth matching


def _two_band_world(db):
    """One week where the shallow band's null is 0% and the deep (unranked)
    band's is 100%: S1/S2 ranked 30/31 never move, U1/U2 unranked both enter.
    PA (ranked 32) and PB (unranked) are picks, both hit, both outside the
    null (no stat line)."""
    teams = {g: "BUF" for g in ("S1", "S2", "U1", "U2", "PA", "PB")}
    _page(db, week=WEEK, scrape=R0, ranks={"S1": 30, "S2": 31, "PA": 32}, team_of=teams)
    r1 = {"S1": 30, "S2": 31, "U1": 20, "U2": 21, "PA": 10, "PB": 15}
    _page(db, week=WEEK + 1, scrape=R1, ranks=r1, team_of=teams)
    _page(db, week=WEEK + 2, scrape=R2, ranks=r1, team_of=teams)
    for g in ("S1", "S2", "U1", "U2"):
        _stat(db, g)


def test_depth_matched_lift_separates_a_shallow_pick_from_a_deep_one(db):
    # STAT-2 (a): the load-bearing case.  Against ONE pooled null (50%) both
    # strategies read +0.5 — the raw lift cannot tell them apart — while the
    # depth-matched lift gives A (a shallow hit where the null is 0%) +1.0
    # and B (a deep hit where every deep line hits anyway) 0.0.
    _two_band_world(db)
    recs = [
        _record([_decision("PA", r0_rank=32)]),
        _record([_decision("PB", r0_rank=None, page_size=40, strategy="volume_topk")],
                strategy="volume_topk"),
    ]
    a = _card(db, recs).overall
    b = _card(db, recs, strategy="volume_topk").overall
    assert a.null_rate == pytest.approx(0.5) and b.null_rate == pytest.approx(0.5)
    assert a.lift_pooled.mean == pytest.approx(0.5)
    assert b.lift_pooled.mean == pytest.approx(0.5)
    assert a.lift_depth_pooled.mean == pytest.approx(1.0)
    assert b.lift_depth_pooled.mean == pytest.approx(0.0)
    assert a.lift_depth_block.mean == pytest.approx(1.0)
    assert b.lift_depth_block.mean == pytest.approx(0.0)
    assert a.lift_depth_pooled.kind == "pooled per-pick depth-matched"
    assert a.depth_fallbacks == 0 and a.depth_unmatched == 0
    assert a.null_by_band == (("1-36", 0, 2), ("unranked", 2, 2))
    assert a.picks_by_band == (("1-36", 1, 1),)
    assert b.picks_by_band == (("unranked", 1, 1),)
    # STAT-2 (d): derived from frozen r0 ranks, so identical across builds
    again = _card(db, recs).overall
    assert (again.lift_depth_pooled, again.lift_depth_block, again.null_by_band) == (
        a.lift_depth_pooled, a.lift_depth_block, a.null_by_band)
    # rendered beside — never instead of — the raw lift, with the band table
    text = S.render(_card(db, recs))
    assert "+50.0pp" in text and "DEPTH-MATCHED per-pick" in text
    assert "null hit rate by r0 depth band" in text
    assert "r0    1-36: null     0/2     =   0.0%  picks 1/1 = 100.0%" in text
    assert "r0 unranked: null     2/2     = 100.0%" in text
    assert "; 0 fallbacks" in text
    comp = S.render_comparison([_card(db, recs), _card(db, recs, strategy="volume_topk")])
    assert "NOT comparable across strategies that pick at different r0 depths" in comp
    assert "depth-pooled=+100.0pp" in comp and "depth-pooled=+0.0pp" in comp
    assert "picks by band, volume_topk: unranked=1/1" in comp


def _two_week_world(db):
    """The two-band week 6 (same pages as ``_two_band_world``) plus a week 7
    whose null has NO shallow line: PC (ranked 33 on week 7's page, band 1-36)
    must fall back to the season's band rate and PD (ranked 38, band 37-48, no
    null line in that band all season) is unmatched.  Week 7's null is one
    unranked line (V1) that misses.  PC/PD ride the SAME scrapes as the week-6
    world — a later re-scrape of the week-7 page would become week 6's r1
    page and hide PA."""
    teams = {g: "BUF" for g in ("S1", "S2", "U1", "U2", "PA", "PB", "PC", "PD", "V1")}
    _page(db, week=WEEK, scrape=R0, ranks={"S1": 30, "S2": 31, "PA": 32}, team_of=teams)
    r1 = {"S1": 30, "S2": 31, "U1": 20, "U2": 21, "PA": 10, "PB": 15, "PC": 33, "PD": 38}
    _page(db, week=WEEK + 1, scrape=R1, ranks=r1, team_of=teams)
    _page(db, week=WEEK + 2, scrape=R2, ranks={**r1, "PC": 5, "PD": 8}, team_of=teams)
    for g in ("S1", "S2", "U1", "U2"):
        _stat(db, g)
    _stat(db, "V1", week=WEEK + 1, knowable="2023-10-22")


def _week7_records():
    """Week 6 picks PA; week 7 (decided 2023-10-24 off the R1 scrape) picks PC
    then PD.  Shared by the STAT-2 (b) and STAT-4 tests."""
    return [
        _record([_decision("PA", r0_rank=32)]),
        _record([
            _decision("PC", r0_rank=33, week=WEEK + 1, as_of="2023-10-24", r0_scrape=R1),
            _decision("PD", r0_rank=38, rank_in_board=2, week=WEEK + 1, as_of="2023-10-24",
                      r0_scrape=R1),
        ], week=WEEK + 1, as_of="2023-10-24"),
    ]


def test_a_pick_whose_band_the_week_never_populated_falls_back_and_is_counted(db):
    # STAT-2 (b)
    _two_week_world(db)
    card = _card(db, _week7_records())
    s = card.overall
    assert s.gradeable == 3 and all(p.hit for p in card.picks)
    assert s.depth_fallbacks == 1 and s.depth_unmatched == 1
    # PA: 1 - 0 (own week); PC: 1 - 0 (season 1-36 rate, fallback); PD skipped
    assert s.lift_depth_pooled.n == 2 and s.lift_depth_pooled.mean == pytest.approx(1.0)
    text = S.render(card)
    assert "1 pick(s) fell back to the season band rate" in text
    assert "1 pick(s) unmatched (no null line in band all season)" in text
    assert "r0   37-48: null     -/-     =   n/a  picks 1/1 (no null line in band)" in text


def test_per_pick_and_season_block_centres_differ_by_construction(db):
    # STAT-4: week 6 null 50% (4 lines), week 7 null 0% (1 line); three hits
    _two_week_world(db)
    card = _card(db, _week7_records())
    s = card.overall
    # per-pick weights weeks by the tool's picks: (0.5 + 1.0 + 1.0) / 3
    assert s.lift_pooled.mean == pytest.approx(2.5 / 3)
    # season-block weights weeks by null lines: p@3 (3/3) - pooled null (2/5)
    assert s.lift_block.mean == pytest.approx(1.0 - 0.4)
    assert s.lift_pooled.mean != s.lift_block.mean
    text = S.render(card)
    assert "The two centres differ by construction" in text
    assert "per-pick weights weeks by the tool's picks, season-block weights weeks by null" in text


# --------------------------------------------------------------- stats


def test_season_block_interval_reproduces_the_draft_backtest_6c_example():
    # intel/research/draft-backtest-2026-08-30.md §6c: five per-season deltas,
    # mean -0.3064, sd 0.709, t(4) = 2.776 -> [-1.186, +0.573]
    per_season = {2021: -0.587, 2022: 0.046, 2023: 0.484, 2024: -1.373, 2025: -0.102}
    iv = stats.season_block_interval(per_season)
    assert iv.n == 5 and iv.kind == "season-block"
    assert iv.mean == pytest.approx(-0.3064, abs=1e-4)
    assert iv.sd == pytest.approx(0.709, abs=2e-3)
    assert iv.lo == pytest.approx(-1.186, abs=2e-3)
    assert iv.hi == pytest.approx(0.573, abs=2e-3)
    assert not iv.excludes_zero


def test_stats_t_interval_matches_the_quarantined_draft_copy_bit_for_bit():
    # stats.py COPIES evaluate.py's stdlib t machinery rather than importing it
    # (Rule 8); this pins the two copies equal so they cannot drift apart.
    from ziggurat.draft import evaluate as ev

    for values in ([0.1, -0.2, 0.3, 0.05, -0.1], [1.0, 1.0, 1.0], [2.0, -1.0],
                   [0.5] + [0.0] * 30):
        mean, lo, hi, sd = ev._t_interval(values, 0.95)
        iv = stats.t_interval(values)
        assert (iv.mean, iv.lo, iv.hi, iv.sd) == (mean, lo, hi, sd)
    for p, df in ((0.975, 4), (0.995, 1), (0.9, 30)):
        assert stats._t_ppf(p, df) == ev._t_ppf(p, df)


def test_t_interval_edges():
    one = stats.t_interval([0.3])
    assert one.is_degenerate and one.n == 1 and one.mean == 0.3
    flat = stats.t_interval([0.2, 0.2, 0.2])
    assert flat.sd == 0.0 and flat.lo == pytest.approx(0.2) and flat.hi == pytest.approx(0.2)
    # STAT-6: a collapsed interval is n identical observations, not evidence
    assert not flat.excludes_zero
    with pytest.raises(ValueError):
        stats.t_interval([])
    with pytest.raises(ValueError):
        stats.season_block_interval({})


def test_a_zero_dispersion_interval_never_prints_a_star():
    iv = stats.season_block_interval({2024: 0.3, 2025: 0.3})
    assert iv.lo == iv.hi == pytest.approx(0.3) and not iv.excludes_zero
    assert " *" not in S._lift(iv)
    real = stats.season_block_interval({2024: 0.3, 2025: 0.31, 2023: 0.29})
    assert real.excludes_zero and S._lift(real).endswith(" *")
    assert not stats.t_interval([0.3]).excludes_zero  # degenerate: no star either


def test_wilson_interval_known_values():
    iv = stats.wilson_interval(5, 10)
    assert iv.mean == 0.5 and iv.n == 10
    assert iv.lo == pytest.approx(0.2366, abs=1e-3)
    assert iv.hi == pytest.approx(0.7634, abs=1e-3)
    empty = stats.wilson_interval(0, 0)
    assert math.isnan(empty.mean) and empty.is_degenerate
    zero = stats.wilson_interval(0, 8)
    assert zero.mean == 0.0 and zero.lo == 0.0 and 0.0 < zero.hi < 0.4
    with pytest.raises(ValueError):
        stats.wilson_interval(3, 2)


def test_draft_backtest_season_block_goes_through_stats():
    # the ONE implementation: draft_backtest's BlockResult is stats' interval
    import backtest.draft_backtest as bt
    from ziggurat.draft.evaluate import DraftOutcome

    per = {2021: -0.587, 2022: 0.046, 2023: 0.484, 2024: -1.373, 2025: -0.102}

    def outcome(objective):
        return DraftOutcome(
            slot=bt.OPERATOR_SLOT, draft_seed=0, objective=objective, expected_wins=objective,
            playoff_prob=0.0, title_prob=0.0, rank=1, holes=0, hole_weeks=(), shape={},
            field_objective=None,
        )

    results = [
        bt.SeasonResult(season=s, scrape_date="x", board_size=1,
                        outcomes={"A": tuple(outcome(d) for _ in range(3)),
                                  "B": tuple(outcome(0.0) for _ in range(3))})
        for s, d in per.items()
    ]
    block = bt.season_block_interval(results, challenger="A", baseline="B")
    iv = stats.season_block_interval(per)
    assert (block.mean, block.ci_low, block.ci_high, block.sd, block.n_blocks) == (
        iv.mean, iv.lo, iv.hi, iv.sd, iv.n)
