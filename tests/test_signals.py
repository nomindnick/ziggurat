"""Candidate generator & signals (item 3.3) — offline, fixture-backed.

Mirrors the two package exemplars: ``test_nfl_usage.py`` (hand-computed oracle +
leakage + explicit latest_truth binding, because the backfill DB reads EMPTY under
the default historical view) and ``test_marginal.py`` (synthetic world). The
usage/injury arms run on the captured 2023 wk5-6 nflverse slice; the QB1 arm runs
on a synthetic two-day panel; the live league-state injury arm is exercised
through synthetic snapshots in ``test_league_state.py``.
"""

import pandas as pd
import pytest

from ziggurat.core import candidates as C
from ziggurat.data.nfl import (
    base,
    depth_charts,
    injuries,
    players,
    schedules,
    snap_counts,
    weekly_stats,
)

# The bulk backfill is retrieved in the FUTURE relative to the 2023 season, so a
# 2023 read under the default historical view returns [] — only knowable_as_of can
# be doing any hiding, which is exactly the two-view seam we must pin.
_BULK_RETRIEVED = "2026-07-16"

# Verified oracle players from the fixture frame (see design note §validation):
_USAGE_JUMP_RB = "00-0035250"   # d_carries = +12 wk6 vs wk5 -> clears the +6 floor
_SANDERS = "00-0035243"         # Miles Sanders, CAR RB, Out wk6 (injury arm)


def _seed(db, nfl_fixture, *, retrieved=_BULK_RETRIEVED):
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"), retrieved_as_of=retrieved)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of=retrieved)
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of=retrieved)


def _read(db, **kw):
    """The 2023/2025 validation path: bind the WHOLE generator to latest_truth."""
    return base.latest_truth(C.build_candidates)(db, **kw)


# --------------------------------------------------------------- Rule 1 guards


def test_build_candidates_requires_keyword_as_of(db):
    with pytest.raises(TypeError):
        C.build_candidates(db, "2023-10-17", season=2023, week=6)  # as_of positional


def test_the_2025_validation_path_needs_latest_truth(db, nfl_fixture):
    # THE two-view seam: bulk history retrieved in the future reads EMPTY under
    # the default historical view (silent footgun), and non-empty under
    # latest_truth — the 0-vs-N fact pinned here.
    _seed(db, nfl_fixture)
    historical = C.build_candidates(db, as_of="2023-10-17", season=2023, week=6)
    assert historical.by_kind(C.SIGNAL_USAGE) == ()
    assert historical.by_kind(C.SIGNAL_INJURY) == ()
    # and the board says how to fix it rather than looking simply "empty"
    assert any("latest_truth" in n for n in historical.notes)

    truth = _read(db, as_of="2023-10-17", season=2023, week=6)
    assert truth.by_kind(C.SIGNAL_USAGE), "latest_truth should surface real candidates"


def _seed_production_stamped(db, nfl_fixture):
    """Seed EVERY source at the future bulk stamp, mirroring production: a
    backfilled schedule is retrieved_as_of in the future and hidden under
    historical for any past as_of (F5 — the test fixture previously seeded
    schedules/players at a PAST stamp, masking the no-week resolution defect)."""
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of=_BULK_RETRIEVED)
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of=_BULK_RETRIEVED)
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"), retrieved_as_of=_BULK_RETRIEVED)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of=_BULK_RETRIEVED)
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of=_BULK_RETRIEVED)


def test_no_week_historical_past_season_is_the_two_view_trap_not_pre_season(db, nfl_fixture):
    # F5 + F3: with schedules seeded at the FUTURE stamp (as in production), a
    # historical past-season read with week=None must NOT resolve a week; it must
    # raise a TWO-VIEW-labelled NoCompletedWeek naming latest_truth/--validate, NOT
    # a misleading 'pre-season' message.
    _seed_production_stamped(db, nfl_fixture)
    with pytest.raises(C.NoCompletedWeek) as exc:
        C.build_candidates(db, as_of="2023-10-17", season=2023)  # week=None, historical
    msg = str(exc.value)
    assert "latest_truth" in msg or "--validate" in msg
    assert "pre-season" not in msg, "must not misdirect a mid-season read as pre-season"
    # the same read via latest_truth resolves the week and returns rows
    truth = _read(db, as_of="2023-10-17", season=2023)  # week=None -> resolved
    assert truth.week == 6
    assert truth.by_kind(C.SIGNAL_USAGE)


def test_view_is_threaded_not_hardcoded(db, nfl_fixture):
    _seed(db, nfl_fixture)
    # A conflicting explicit view is rejected by the wrapper's guard — proof the
    # generator threads `view` down rather than hardcoding it.
    with pytest.raises(ValueError, match="conflicting view"):
        base.latest_truth(C.build_candidates)(
            db, as_of="2023-10-17", season=2023, week=6, view="historical")


# ---------------------------------------------------------------- usage oracle


def test_a_usage_breakout_surfaces_the_right_player(db, nfl_fixture):
    _seed(db, nfl_fixture)
    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    usage = board.by_kind(C.SIGNAL_USAGE)
    keys = {r.gsis_id for r in usage}
    assert _USAGE_JUMP_RB in keys, "the +12-carry RB must be flagged"

    # Hand-diff the oracle against the fixture frame.
    wk = nfl_fixture("weekly_stats")
    rb = wk[(wk.position == "RB") & (wk.week.isin([5, 6]))]
    c6 = rb[(rb.player_id == _USAGE_JUMP_RB) & (rb.week == 6)]["carries"].iloc[0]
    c5 = rb[(rb.player_id == _USAGE_JUMP_RB) & (rb.week == 5)]["carries"].iloc[0]
    assert float(c6) - float(c5) >= C.DEFAULT_BREAKOUT.floors["carries"]

    row = next(r for r in usage if r.gsis_id == _USAGE_JUMP_RB)
    assert row.player and row.player != _USAGE_JUMP_RB, "Rule 6: display name, not a gsis id"
    assert row.prior_week == 5
    assert not row.hypothesis
    assert any("carries" in reason for reason in row.reasons)
    # provenance travels with the row (Rule 6)
    assert any(C.DEFAULT_BREAKOUT.label in reason for reason in row.reasons)


def test_usage_arm_scans_only_skill_positions(db, nfl_fixture):
    _seed(db, nfl_fixture)
    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    positions = {r.position for r in board.by_kind(C.SIGNAL_USAGE)}
    assert positions <= set(C.USAGE_POSITIONS)
    assert "QB" not in positions and "K" not in positions and "DST" not in positions


def _append_debut_rb(wk, *, gsis, team, week=6, carries=22, targets=0, receptions=0):
    """A synthetic debut: a player with a target-week row but NO prior week, so
    usage_deltas gives prior_week=None + all d_*=None (the F1 role-emergence cohort)."""
    base_row = wk.iloc[0].copy()
    for col in wk.columns:
        if wk[col].dtype.kind in "fi":
            base_row[col] = 0
    base_row["player_id"] = gsis
    base_row["player_name"] = None
    base_row["position"] = "RB"
    base_row["position_group"] = "RB"
    base_row["recent_team"] = team
    base_row["season"] = 2023
    base_row["week"] = week
    base_row["season_type"] = "REG"
    base_row["carries"] = carries
    base_row["targets"] = targets
    base_row["receptions"] = receptions
    base_row["rushing_yards"] = carries * 4
    return pd.concat([wk, pd.DataFrame([base_row])], ignore_index=True)


def test_role_emergence_surfaces_a_prior_week_none_debut(db, nfl_fixture):
    # F1: a debut RB (22 carries, no prior week to difference) must surface as a
    # USAGE_BREAKOUT via the ABSOLUTE role-emergence floors, not be silently dropped.
    debut = "00-0099999"
    wk = _append_debut_rb(nfl_fixture("weekly_stats"), gsis=debut, team="CAR")
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, wk, retrieved_as_of=_BULK_RETRIEVED)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of=_BULK_RETRIEVED)
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of=_BULK_RETRIEVED)

    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    row = next((r for r in board.by_kind(C.SIGNAL_USAGE) if r.gsis_id == debut), None)
    assert row is not None, "the 22-carry debut must surface (F1), not be dropped"
    assert row.prior_week is None
    assert not row.hypothesis
    joined = " ".join(row.reasons)
    assert "role emergence" in joined and "no prior week to difference" in joined
    assert "labelled hypothesis" in joined  # Rule 6: the floor's status travels


def test_role_emergence_never_treats_none_usage_as_zero():
    # F1 / Rule 2: a None raw value is UNKNOWN and clears no floor; only real
    # values that meet the absolute floor count.
    assert C._emergence_hits({"carries": None, "targets": None, "receptions": None}, None) == {}
    assert C._emergence_hits({"carries": 10, "targets": None, "receptions": None}, None) == {"carries": 10.0}
    assert C._emergence_hits({"carries": 3, "targets": 3, "receptions": 3}, 0.40) == {}


def test_role_emergence_only_fires_above_the_absolute_floors(db, nfl_fixture):
    # F1 guard: a debut BELOW every absolute floor (3 carries, 2 targets, 1 catch,
    # no snap share) must NOT surface — the floors gate the cohort so it stays a
    # trickle mid-season, not a flood. (The 2-week fixture cannot measure the
    # mid-season cohort size; that is verified on the live 2025 db in the report.)
    weak = "00-0099998"
    wk = _append_debut_rb(nfl_fixture("weekly_stats"), gsis=weak, team="CAR",
                          carries=3, targets=2, receptions=1)
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, wk, retrieved_as_of=_BULK_RETRIEVED)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of=_BULK_RETRIEVED)
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of=_BULK_RETRIEVED)
    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    assert weak not in {r.gsis_id for r in board.by_kind(C.SIGNAL_USAGE)}


def test_committee_beneficiary_links_a_same_slot_debut_to_the_vacancy(db, nfl_fixture):
    # F2 + F1: Miles Sanders (CAR RB) is Out wk6; a debut CAR RB with 22 carries
    # must (a) be named as a possible beneficiary on Sanders' injury row with the
    # committee hedge, and (b) receive the hedged vacancy annotation on his own
    # usage row in place.
    debut = "00-0099999"
    wk = _append_debut_rb(nfl_fixture("weekly_stats"), gsis=debut, team="CAR")
    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, wk, retrieved_as_of=_BULK_RETRIEVED)
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of=_BULK_RETRIEVED)
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of=_BULK_RETRIEVED)

    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    sanders = next(r for r in board.by_kind(C.SIGNAL_INJURY) if r.gsis_id == _SANDERS)
    sj = " ".join(sanders.reasons)
    assert "possible beneficiaries" in sj and "Committee — not naming one" in sj
    assert debut in sj, "the debut beneficiary must be named on the vacancy row"

    debut_row = next(r for r in board.by_kind(C.SIGNAL_USAGE) if r.gsis_id == debut)
    dj = " ".join(debut_row.reasons)
    assert "vacancy opened this week" in dj and "may or may not explain this usage" in dj
    assert "opportunity opened by" not in dj, "the causal (unhedged) wording is gone (F7)"


def test_committee_hedge_absent_when_no_same_slot_uptick(db, nfl_fixture):
    # F2: a vacancy with NO same-team/same-position usage uptick emits the
    # 'no same team+position usage uptick is visible yet' branch.
    _seed(db, nfl_fixture)
    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    lonely = [r for r in board.by_kind(C.SIGNAL_INJURY)
              if any("no same team+position usage uptick" in x for x in r.reasons)]
    assert lonely, "at least one vacancy has no visible same-slot beneficiary"


def test_none_delta_is_unknown_not_a_breakout(db, nfl_fixture):
    # A player with no prior knowable game carries null deltas; qualifies() must
    # treat None as UNKNOWN, never a real >= floor value.
    _seed(db, nfl_fixture)
    assert C.DEFAULT_BREAKOUT.qualifies({f"d_{m}": None for m in C.DEFAULT_BREAKOUT.floors}) == {}


# -------------------------------------------------------------------- leakage


def test_usage_arm_leaks_nothing_before_the_week_is_played(db, nfl_fixture):
    _seed(db, nfl_fixture)
    # 2023-10-11 is after week 5 but before every week-6 game — a week-6 usage
    # delta cannot exist yet even though all rows were bulk-pulled in 2026.
    early = _read(db, as_of="2023-10-11", season=2023, week=6)
    assert early.by_kind(C.SIGNAL_USAGE) == ()
    # and once the week is played, it appears (the symptom is knowability)
    assert _read(db, as_of="2023-10-20", season=2023, week=6).by_kind(C.SIGNAL_USAGE)


def test_whole_board_is_empty_before_any_week6_signal_is_knowable(db, nfl_fixture):
    # Before the first week-6 injury report is modified (10-11) AND before any
    # week-6 game, nothing is knowable — the full board is empty. (Note the
    # injury arm DOES have real lead time in 2023: date_modified makes week-6
    # reports knowable from 10-11, unlike the 2025 gameday-only feed — that is the
    # accessor working, disclosed in every shock's reason.)
    _seed(db, nfl_fixture)
    board = _read(db, as_of="2023-10-10", season=2023, week=6)
    assert board.rows == ()


# --------------------------------------------------------------- injury arm (A)


def test_injury_arm_nflverse_source_surfaces_an_out_starter(db, nfl_fixture):
    _seed(db, nfl_fixture)
    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    shocks = board.by_kind(C.SIGNAL_INJURY)
    sanders = next((r for r in shocks if r.gsis_id == _SANDERS), None)
    assert sanders is not None, "Miles Sanders (Out wk6) must be an injury shock"
    joined = " ".join(sanders.reasons)
    assert "nflverse feed" in joined
    # Rule 6: every shock discloses lead-time honesty and IR-invisibility
    assert "lead-time reality" in joined
    assert "INVISIBLE" in joined


def test_injury_arm_drops_un_rosterable_idp_and_ol(db, nfl_fixture):
    # Rule 6: the injury feed carries the WHOLE NFL roster (CB/LB/S/OL). A
    # 10-team offense-skill league can roster none of them, so they must never
    # appear as opportunity shocks (measured: 53 of 81 wk9 shocks were IDP/OL).
    _seed(db, nfl_fixture)
    inj = nfl_fixture("injuries")
    out = inj[inj.report_status.str.lower().isin(["out", "doubtful"])]
    idp = set(out[~out.position.isin(C.FANTASY_INJURY_POSITIONS)]["gsis_id"])
    assert idp, "fixture must contain IDP/OL Out rows for this test to mean anything"
    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    shock_keys = {r.gsis_id for r in board.by_kind(C.SIGNAL_INJURY)}
    assert not (idp & shock_keys), "IDP/OL injuries must be filtered from the shock list"
    positions = {r.position for r in board.by_kind(C.SIGNAL_INJURY)}
    assert positions <= C.FANTASY_INJURY_POSITIONS | {None}


def test_questionable_is_not_a_shock(db, nfl_fixture):
    # Questionable is a weekly game designation, not a vacancy — excluded.
    _seed(db, nfl_fixture)
    board = _read(db, as_of="2023-10-17", season=2023, week=6)
    inj = nfl_fixture("injuries")
    q_only = set(inj[(inj.week == 6) & (inj.report_status == "Questionable")]["gsis_id"])
    out_wk6 = set(inj[(inj.week == 6) & (inj.report_status == "Out")]["gsis_id"])
    pure_q = q_only - out_wk6
    shock_keys = {r.gsis_id for r in board.by_kind(C.SIGNAL_INJURY)}
    assert not (pure_q & shock_keys), "a purely-Questionable player must not be a shock"


# --------------------------------------------------------------- injury arm (B)
# The LIVE league-state source (the ONLY one that sees IR) is exercised here with
# synthetic snapshots; its detector unit tests live in test_league_state.py.


def _snap_entry(pid, *, pos="RB", injury="ACTIVE", team_id=None):
    return {
        "id": int(pid),
        "onTeamId": team_id or 0,
        "status": "ONTEAM" if team_id else "FREEAGENT",
        "player": {
            "id": int(pid), "fullName": f"Player {pid}",
            "defaultPositionId": {"QB": 1, "RB": 2, "WR": 3, "TE": 4}[pos],
            "proTeamId": 1, "injuryStatus": injury,
            "ownership": {"percentOwned": 50.0, "percentStarted": 40.0, "percentChange": 0.0},
        },
    }


def test_injury_dedupe_collapses_a_gsis_gap_across_both_sources(db):
    # F16: the same player Out in BOTH sources — gsis present in the nflverse feed
    # but NULL in league state (crosswalk gap) — must collapse to ONE row keyed by
    # the crosswalk-at-now identity, carrying both sources' evidence, not two halves.
    gsis, espn = "00-0088888", "888801"
    # players crosswalk bridges espn -> gsis (crosswalk-at-now)
    db.execute("INSERT INTO players (gsis_id, espn_id, name, position, retrieved_as_of, "
               "knowable_as_of) VALUES (?,?,?,?,?,?)",
               (gsis, espn, "Dupe Star", "WR", "2099-01-01", "2099-01-01"))
    db.execute(
        "INSERT INTO injuries (gsis_id,season,week,team,position,full_name,report_status,"
        "report_primary_injury,date_modified,retrieved_as_of,knowable_as_of) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (gsis, 2099, 5, "KC", "WR", "Dupe Star", "Out", "Hamstring",
         "2099-10-30", "2099-10-30", "2099-10-30"))
    for day, st in (("2099-10-28", "ACTIVE"), ("2099-10-31", "OUT")):
        db.execute(
            "INSERT INTO league_player_state (season,espn_player_id,gsis_id,player,position,"
            "pro_team,on_team_id,injury_status,percent_owned,scoring_period,retrieved_as_of,"
            "knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (2099, espn, None, "Dupe Star", "WR", "KC", 3, st, 50.0, 5, day, day))
    db.commit()
    rows = C._injury_arm(db, as_of="2099-11-01", season=2099, week=5,
                         view="historical", usage_rows=[])
    dupes = [r for r in rows if r.player == "Dupe Star"]
    assert len(dupes) == 1, "the gsis-gap duplicate must collapse to ONE row"
    joined = " ".join(dupes[0].reasons)
    assert "nflverse feed" in joined and "ESPN league state" in joined, "both evidences kept"


def test_injury_block_orders_out_shocks_by_percent_owned(db):
    # F17: all Out shocks tie at severity 2.0, so the block must rank by rostered
    # value (percent_owned DESC) with a stable name tiebreak, so --top keeps the
    # star above the fold and the order is reproducible across a re-ingest.
    for pid, name, pct in (("00-0077701", "Star WR", 95.0),
                           ("00-0077702", "Scrub WR", 2.0),
                           ("00-0077703", "Mid WR", 40.0)):
        db.execute("INSERT INTO injuries (gsis_id,season,week,team,position,full_name,"
                   "report_status,date_modified,retrieved_as_of,knowable_as_of) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (pid, 2099, 5, "KC", "WR", name, "Out", "2099-10-30",
                    "2099-10-30", "2099-10-30"))
        db.execute("INSERT INTO league_player_state (season,espn_player_id,gsis_id,player,"
                   "position,pro_team,on_team_id,injury_status,percent_owned,scoring_period,"
                   "retrieved_as_of,knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (2099, f"e{pid}", pid, name, "WR", "KC", 3, "ACTIVE", pct, 5,
                    "2099-10-28", "2099-10-28"))
    db.commit()
    rows = C._injury_arm(db, as_of="2099-11-01", season=2099, week=5,
                         view="historical", usage_rows=[])
    order = [r.player for r in rows]
    assert order == ["Star WR", "Mid WR", "Scrub WR"], order


def test_injury_arm_league_state_source_sees_a_ruled_out_transition(db):
    from ziggurat.league import state
    # two consecutive snapshots: player 5001 goes ACTIVE -> INJURY_RESERVE.
    state.ingest_player_state(db, [_snap_entry(5001, injury="ACTIVE")],
                              retrieved_as_of="2026-09-10", season=2026, allow_shrink=True)
    state.ingest_player_state(db, [_snap_entry(5001, injury="INJURY_RESERVE")],
                              retrieved_as_of="2026-09-11", season=2026, allow_shrink=True)
    rows = C._injury_arm(db, as_of="2026-09-11", season=2026, week=1,
                         view="historical", usage_rows=[])
    hit = next((r for r in rows if r.espn_id == "5001"), None)
    assert hit is not None, "the ACTIVE->IR transition must surface as a shock"
    joined = " ".join(hit.reasons)
    assert "ESPN league state" in joined
    assert "INJURY_RESERVE" in joined


# ---------------------------------------------------------------- QB1 hypothesis


def _qb_slot(dt, team, name, espn, gsis, pos_id, pos_abb, rank):
    return dict(dt=dt, team=team, player_name=name, espn_id=espn, gsis_id=gsis,
                pos_grp_id="16", pos_grp="Offense", pos_id=pos_id, pos_name=pos_abb,
                pos_abb=pos_abb, pos_slot=rank, pos_rank=rank)


def _qb_panel(dt, qb1, rb1):
    return pd.DataFrame([
        _qb_slot(dt, "ZZZ", qb1[0], qb1[1], qb1[2], "1", "QB", 1),
        _qb_slot(dt, "ZZZ", "Backup QB", "9002", "00-0090002", "1", "QB", 2),
        _qb_slot(dt, "ZZZ", rb1[0], rb1[1], rb1[2], "2", "RB", 1),
        _qb_slot(dt, "ZZZ", "Backup RB", "9004", "00-0090004", "2", "RB", 2),
    ])


def _seed_qb1_change(db):
    """Team ZZZ: QB1 flips Alpha->Beta AND RB1 flips (to test the forbidden guard).
    Each panel is retrieved on its own day so the historical view can see the
    baseline at ``since`` (the two-view seam, again)."""
    d1, d2 = "2025-09-13T07:12:47Z", "2025-09-20T07:13:32Z"
    depth_charts.ingest_depth_charts(
        db, _qb_panel(d1, ("Alpha QB", "9001", "00-0090001"),
                      ("Alpha RB", "9005", "00-0090005")),
        season=2025, retrieved_as_of="2025-09-13")
    depth_charts.ingest_depth_charts(
        db, _qb_panel(d2, ("Beta QB", "9003", "00-0090003"),
                      ("Beta RB", "9006", "00-0090006")),
        season=2025, retrieved_as_of="2025-09-20")
    return "2025-09-13", "2025-09-20"


def test_qb1_change_ships_as_a_labelled_hypothesis(db):
    since, as_of = _seed_qb1_change(db)
    board = C.build_candidates(db, as_of=as_of, season=2025, week=2, since=since)
    qb1 = board.by_kind(C.SIGNAL_QB1)
    assert len(qb1) == 1
    row = qb1[0]
    assert row.hypothesis is True
    assert row.position == "QB"
    assert row.player == "Beta QB"
    joined = " ".join(row.reasons).lower()
    # the folded caveats must survive verbatim (Rule 6)
    assert "precision" in joined  # "...precision was never measured"
    assert "listed qb1" in joined


def test_no_rb_wr_te_rank_change_is_ever_a_trigger(db):
    # The forbidden guard (F12 — non-tautological): the RB1 ("Beta RB") ALSO flipped
    # in the same panel, so the real invariant is on IDENTITY, not the hardcoded
    # position label. Every QB1 row must BE the QB, and there must be EXACTLY ONE —
    # so if a skill-position rank-change ever leaked in, the len==1 assertion FAILS.
    since, as_of = _seed_qb1_change(db)
    board = C.build_candidates(db, as_of=as_of, season=2025, week=2, since=since)
    qb1 = board.by_kind(C.SIGNAL_QB1)
    assert len(qb1) == 1, "only the QB1 flip may surface — an RB1 flip must not leak"
    # the ONE row is the QB (Beta QB), never the RB1-flip bait (Beta RB / gsis 90006)
    assert qb1[0].player == "Beta QB"
    assert qb1[0].gsis_id == "00-0090003"
    assert qb1[0].gsis_id != "00-0090006", "the RB1 flip must never appear as a QB1 row"
    for row in board.rows:
        if row.position in ("RB", "WR", "TE"):
            assert not row.hypothesis


def test_qb1_arm_snaps_a_since_before_the_first_panel_forward(db):
    # F13: a `since` that PRECEDES the season's first observed panel must be
    # SNAPPED FORWARD onto that panel (never crash, never silently skip) so a real
    # QB1 change whose baseline is the first chart still surfaces. Panels here are
    # 2025-09-13 / 09-20; since=2025-09-01 precedes both.
    _seed_qb1_change(db)
    board = C.build_candidates(db, as_of="2025-09-20", season=2025, week=2,
                               since="2025-09-01")  # no panel that early -> snap fwd
    qb1 = board.by_kind(C.SIGNAL_QB1)
    assert len(qb1) == 1, "the QB1 change must surface via the snapped-forward baseline"
    assert qb1[0].player == "Beta QB"
    # and because the arm ANSWERED, no could-not-answer note is emitted (F6)
    assert not any("could not" in n or "produced nothing" in n for n in board.notes)


def test_qb1_arm_notes_when_it_cannot_answer_under_the_view(db):
    # F6: when the QB1 arm is skipped because NO baseline panel is visible under the
    # view (backfilled panels retrieved in the FUTURE are hidden under historical),
    # the board must SAY so — an unexplained empty block cannot be told apart from
    # "no QB1 changes". latest_truth would see the same panels.
    d1, d2 = "2025-09-13T07:12:47Z", "2025-09-20T07:13:32Z"
    depth_charts.ingest_depth_charts(
        db, _qb_panel(d1, ("Alpha QB", "9001", "00-0090001"),
                      ("Alpha RB", "9005", "00-0090005")),
        season=2025, retrieved_as_of="2026-07-25")  # backfilled: retrieved in the future
    depth_charts.ingest_depth_charts(
        db, _qb_panel(d2, ("Beta QB", "9003", "00-0090003"),
                      ("Beta RB", "9006", "00-0090006")),
        season=2025, retrieved_as_of="2026-07-25")
    board = C.build_candidates(db, as_of="2025-09-20", season=2025, week=2,
                               since="2025-09-13", view="historical")
    assert board.by_kind(C.SIGNAL_QB1) == ()
    assert any("QB1 arm" in n and ("latest_truth" in n or "--validate" in n)
               for n in board.notes), "the skip must be explained, not silently empty"
    # and latest_truth resolves the same baseline and surfaces the change
    truth = _read(db, as_of="2025-09-20", season=2025, week=2, since="2025-09-13")
    assert {r.player for r in truth.by_kind(C.SIGNAL_QB1)} == {"Beta QB"}


def test_qb1_arm_before_the_panel_regime_explains_the_skip(db):
    # F6: season < 2025 (pre-panel) with a since given must explain the empty block.
    since, as_of = _seed_qb1_change(db)
    board = C.build_candidates(db, as_of=as_of, season=2024, week=2, since=since)
    assert board.by_kind(C.SIGNAL_QB1) == ()
    assert any("before the depth-chart panel regime" in n for n in board.notes)


def test_qb1_arm_is_disabled_before_the_panel_regime(db):
    since, as_of = _seed_qb1_change(db)
    board = C.build_candidates(db, as_of=as_of, season=2024, week=2, since=since)
    assert board.by_kind(C.SIGNAL_QB1) == ()


# ---------------------------------------------------------------- freshness


def test_freshness_banner_stays_quiet_on_fresh_archived_and_na(db, monkeypatch):
    from ziggurat.data.nfl import refresh

    def fake(conn, *, season, today):
        return [
            {"source": "weekly_stats", "verdict": refresh.VERDICT_FRESH,
             "perishable": False, "age_days": 1},
            {"source": "injuries", "verdict": "stale",
             "perishable": False, "age_days": 40},
            {"source": "adp_rankings", "verdict": "stale",  # not in the watched set
             "perishable": True, "age_days": 40},
            {"source": "depth_charts", "verdict": refresh.VERDICT_ARCHIVED,
             "perishable": False, "age_days": 400},
        ]

    monkeypatch.setattr(refresh, "source_freshness", fake)
    lines = C._freshness_lines(db, season=2025, as_of="2025-11-04", today="2026-07-26")
    text = "\n".join(lines)
    assert "injuries: stale" in text            # non-quiet + watched -> shown
    assert "weekly_stats" not in text           # FRESH is quiet
    assert "depth_charts" not in text           # ARCHIVED is quiet
    assert "adp_rankings" not in text           # not a source this module reads


def test_freshness_is_silent_without_a_today(db):
    assert C._freshness_lines(db, season=2025, as_of="2025-11-04", today=None) == []


# ---------------------------------------------------------------- format (F14)


def _row(kind, player, mag, *, pos="RB", hyp=False, reasons=("r1", "r2")):
    return C.CandidateRow(
        player_key=player, player=player, position=pos, team="KC",
        gsis_id=player, espn_id=None, signal_kind=kind, magnitude=mag,
        week=9, prior_week=8, hypothesis=hyp, reasons=tuple(reasons))


def test_format_candidates_orders_and_truncates_and_tags():
    # A CandidateBoard whose USAGE block is already ranked descending (build_candidates'
    # job); format must PRESERVE that order (never reverse/reshuffle) and slice top-N.
    board = C.CandidateBoard(
        rows=(
            _row(C.SIGNAL_USAGE, "Strong", 9.0),
            _row(C.SIGNAL_USAGE, "Mid", 5.0),
            _row(C.SIGNAL_USAGE, "Weak", 1.0),
            _row(C.SIGNAL_QB1, "Backup QB", 1.0, pos="QB", hyp=True,
                 reasons=("listed QB1; precision never measured",)),
        ),
        week=9, freshness=(), notes=(), as_of="2025-11-04", season=2025)

    # descending magnitude order is preserved within the USAGE block
    full = C.format_candidates(board)
    usage_block = full.split("QB1-CHANGE")[0]
    assert usage_block.index("Strong") < usage_block.index("Mid") < usage_block.index("Weak")

    # top=1 shows one row + a '... N more' counter
    top1 = C.format_candidates(board, top=1)
    assert "Strong" in top1 and "Mid" not in top1
    assert "... 2 more" in top1

    # hypothesis rows render the [HYPOTHESIS] tag; the SIGNAL legend is present (F11)
    assert "[HYPOTHESIS]" in full
    assert "SIGNAL" in full and "NOT fantasy points" in full
    assert "SCORE" not in full  # the misleading header is gone (F11)

    # reasons=True emits each reason line
    with_reasons = C.format_candidates(board, reasons=True)
    assert "- r1" in with_reasons and "- r2" in with_reasons


# ---------------------------------------------- partial week / pre-season (F15)


def test_partial_week_note_when_as_of_precedes_the_last_gameday(db, nfl_fixture):
    # F15: a mid-week as_of (before week 6's last gameday) must carry a PARTIAL WEEK
    # note — the usage arm is reading an incomplete slice.
    _seed(db, nfl_fixture)
    board = _read(db, as_of="2023-10-13", season=2023, week=6)  # mid-week-6
    assert any("PARTIAL WEEK" in n for n in board.notes)


def test_pre_season_no_week_raises_no_completed_week(db, nfl_fixture):
    # F15: a genuine pre-season as_of with week=None (no REG week played) raises
    # NoCompletedWeek — never guesses a not-yet-played week.
    _seed(db, nfl_fixture)
    with pytest.raises(C.NoCompletedWeek):
        _read(db, as_of="2023-08-15", season=2023)  # week=None, before any game
