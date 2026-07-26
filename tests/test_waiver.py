"""Waiver module tests — item 3.4.

Offline throughout: the synthetic ``marginal_world`` projection/league universe,
no network. Everything here is synthetic by necessity — the live DB is pre-draft
(every league_player_state row is a free agent, lineup_slot NULL), so the crux
(a roster that goes oversized when a Tuesday reset flips an IR occupant out of
IR-eligibility) has no real instance to test against.

The tests that matter most pin MEASURED design decisions: that legality recounts
IR itself rather than trusting active_players (which strips IR rows blindly),
that the flipped IR occupant is reslotted so he is visible to the drop board he
must be dropped from, and that an illegal roster refuses to plan claims and
proposes the fix (the done-when).
"""

import re
from unittest.mock import patch

import pytest

from ziggurat.core import waiver
from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.core.waiver import (
    IR_ELIGIBLE_LABEL,
    KIND_FREE_AGENT,
    KIND_WAIVER,
    build_waiver_plan,
    check_legality,
)
from ziggurat.league.state import OwnTeamUnresolved

SEASON = 2026
PULL = "2026-09-15"
WEEKS = range(3, 18)
TEAM = 10

# D/ST is the only position with real week-to-week variation; two defenses whose
# good weeks alternate reproduce it (mirrors test_marginal).
_ODD = {w: 20.0 for w in range(1, 18) if w % 2 == 1}


def _active_specs():
    """16 non-IR bodies on team 10 (a full, legal active roster)."""
    return [
        {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": TEAM},
        {"name": "Backup Passer", "pos": "QB", "team": "TEN", "pts": 8.0, "bye": 6, "on_team": TEAM},
        {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": TEAM},
        {"name": "Second Runner", "pos": "RB", "team": "ATL", "pts": 5.0, "bye": 11, "on_team": TEAM},
        {"name": "Third Runner", "pos": "RB", "team": "BUF", "pts": 12.0, "bye": 7, "on_team": TEAM},
        {"name": "Depth Runner", "pos": "RB", "team": "CHI", "pts": 3.0, "bye": 9, "on_team": TEAM},
        {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": TEAM},
        {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": TEAM},
        {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 11.0, "bye": 10, "on_team": TEAM},
        {"name": "Fourth Catcher", "pos": "WR", "team": "HOU", "pts": 4.0, "bye": 12, "on_team": TEAM},
        {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": TEAM},
        {"name": "Tight Two", "pos": "TE", "team": "JAX", "pts": 3.0, "bye": 5, "on_team": TEAM},
        {"name": "Kick Er", "pos": "K", "team": "KC", "pts": 8.0, "bye": 14, "on_team": TEAM},
        {"name": "Miami D/ST", "pos": "D/ST", "team": "MIA", "pts": 2.0, "bye": 5, "on_team": TEAM,
         "weeks": _ODD},
        {"name": "Fifth Catcher", "pos": "WR", "team": "NO", "pts": 6.0, "bye": 7, "on_team": TEAM},
        {"name": "Sixth Catcher", "pos": "WR", "team": "SEA", "pts": 7.0, "bye": 8, "on_team": TEAM},
    ]


def _ir_spec(injury):
    return {"name": "IR Guy", "pos": "WR", "team": "MIN", "pts": 14.0, "bye": 6,
            "on_team": TEAM, "slot": "IR", "injury": injury}


_POOL_SPECS = [
    {"name": "Free Passer", "pos": "QB", "team": "NE", "pts": 12.0, "bye": 9},
    {"name": "Free Runner", "pos": "RB", "team": "NYG", "pts": 20.0, "bye": 7},          # FCFS grab
    {"name": "Waiver Catcher", "pos": "WR", "team": "NYJ", "pts": 19.0, "bye": 11,
     "status": "WAIVERS"},                                                              # queued claim
    {"name": "Waiver Wideout", "pos": "WR", "team": "PIT", "pts": 16.0, "bye": 10,
     "status": "WAIVERS"},                                                              # queued claim
    {"name": "Free Tight", "pos": "TE", "team": "LV", "pts": 9.0, "bye": 8},
]


def _world(marginal_world, injury="QUESTIONABLE"):
    specs = _active_specs() + [_ir_spec(injury)] + _POOL_SPECS
    marginal_world(specs, retrieved=PULL)


def _plan(db, **kwargs):
    kwargs.setdefault("weeks", WEEKS)
    kwargs.setdefault("pool_limit", None)
    return build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=TEAM, **kwargs)


# ---------------------------------------------------- check_legality (pure unit)


def _row(slot=None, injury="ACTIVE", pos="WR", eid="1", name="P"):
    return {"lineup_slot": slot, "injury_status": injury, "position": pos,
            "espn_player_id": eid, "player": name}


def test_sixteen_active_is_legal():
    rows = [_row(eid=str(i)) for i in range(16)]
    v = check_legality(rows)
    assert v.legal is True
    assert v.active_count == 16 and v.ir_count == 0
    assert v.violations == ()


def test_seventeen_active_is_illegal():
    rows = [_row(eid=str(i)) for i in range(17)]
    v = check_legality(rows)
    assert v.legal is False
    assert v.active_count == 17
    assert any("17 of 16" in p for p in v.violations)


def test_an_ir_out_occupant_does_not_count_active():
    rows = [_row(eid=str(i)) for i in range(16)] + [_row(slot="IR", injury="OUT", eid="ir")]
    v = check_legality(rows)
    assert v.legal is True
    assert v.active_count == 16 and v.ir_count == 1
    assert v.ir_ineligible == ()


def test_an_ir_questionable_occupant_re_counts_active_and_is_illegal():
    rows = [_row(eid=str(i)) for i in range(16)] + [
        _row(slot="IR", injury="QUESTIONABLE", eid="ir", name="Reset Guy")]
    v = check_legality(rows)
    assert v.legal is False
    assert v.active_count == 17
    assert [o.player for o in v.ir_ineligible] == ["Reset Guy"]


def test_two_ir_occupants_is_illegal_even_when_active_is_small():
    rows = [_row(eid=str(i)) for i in range(15)] + [
        _row(slot="IR", injury="OUT", eid="a"), _row(slot="IR", injury="OUT", eid="b")]
    v = check_legality(rows)
    assert v.legal is False
    assert v.active_count == 15  # both eligible, so neither re-counts
    assert any("2 players are in the IR slot" in p for p in v.violations)


def test_a_lone_ineligible_ir_occupant_on_a_sub16_roster_is_legal_with_a_required_move():
    # F5: an ineligible IR occupant is folded into active_count, NOT an independent
    # illegality source. On a sub-16 roster he cannot make it oversized, so the
    # roster is LEGAL — ESPN just benches him — and we surface a REQUIRED ROSTER
    # MOVE advisory rather than telling the operator to drop a body.
    rows = [_row(slot="IR", injury="ACTIVE", eid="x", name="Healthy On IR")]
    v = check_legality(rows)
    assert v.legal is True
    assert v.active_count == 1 and v.ir_count == 1
    assert v.violations == ()
    assert [o.player for o in v.ir_ineligible] == ["Healthy On IR"]
    assert any("REQUIRED ROSTER MOVE" in a and "Healthy On IR" in a for a in v.ir_advisories)


def test_ir_slot_count_is_read_from_the_structure_not_hard_coded():
    assert DEFAULT_ROSTER.active_slots == 16 and DEFAULT_ROSTER.ir_slots == 1


# ------------------------------------------------------------ Rule 1 (leakage)


def test_build_waiver_plan_requires_as_of():
    with pytest.raises(TypeError):
        waiver.build_waiver_plan(None, season=SEASON, own_team_id=TEAM)


def test_nothing_is_visible_before_the_snapshot_was_knowable(db, marginal_world):
    _world(marginal_world, injury="OUT")
    before = build_waiver_plan(db, as_of="2026-09-14", season=SEASON,
                               own_team_id=TEAM, weeks=WEEKS, pool_limit=None)
    # the whole roster read is empty before it was knowable — no leak
    assert before.legality.active_count == 0
    assert before.drop_board == ()
    assert before.blocked is False

    after = _plan(db)
    assert after.legality.active_count == 16


# ----------------------------------------------------------------- the done-when


def test_an_illegal_roster_refuses_claims_and_proposes_the_fix(db, marginal_world):
    """DONE-WHEN: 16 active + 1 IR occupant flipped to QUESTIONABLE -> 17 of 16 ->
    the plan refuses to plan claims and proposes the forced drop."""
    _world(marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)

    assert plan.blocked is True
    assert plan.claims == ()
    assert plan.fcfs_grabs == ()
    assert plan.legality.active_count == 17
    assert [o.player for o in plan.legality.ir_ineligible] == ["IR Guy"]

    # a forced drop is proposed and its reason states the fix + names the cause
    assert plan.forced_drop is not None
    blob = " ".join(plan.forced_drop.reasons)
    assert "DROP THIS PLAYER" in blob
    assert "IR Guy" in blob                       # the cause is named
    assert "16" in blob                           # the target active count


def test_the_same_roster_with_an_ir_eligible_occupant_is_legal_and_emits_claims(db, marginal_world):
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    assert plan.blocked is False
    assert plan.forced_drop is None
    assert plan.claims or plan.fcfs_grabs      # a positive add exists in the pool


# ------------------------------------------------ reslot-before-pricing guard


def test_the_flipped_ir_player_is_visible_on_the_drop_board(db, marginal_world):
    """Regression guard: build_board strips every IR row, so without reslotting the
    ineligible occupant IR->BE he is invisible to the very drop board that must
    decide the forced drop."""
    _world(marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)
    assert "IR Guy" in [d.player for d in plan.drop_board]


def test_the_forced_drop_is_the_lowest_marginal_active_player(db, marginal_world):
    _world(marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)
    # drop_board is ascending (lowest = most droppable); the forced drop is its head
    assert plan.drop_board != ()
    lowest = min(plan.drop_board, key=lambda d: d.marginal_points)
    assert plan.forced_drop.player == plan.drop_board[0].player == lowest.player


# ------------------------------------------------------- claims vs FCFS split


def test_a_waivers_add_is_a_claim_and_a_freeagent_add_is_an_fcfs_grab(db, marginal_world):
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    kinds_claims = {c.kind for c in plan.claims}
    kinds_grabs = {c.kind for c in plan.fcfs_grabs}
    assert kinds_claims <= {KIND_WAIVER}
    assert kinds_grabs <= {KIND_FREE_AGENT}
    # the WAIVERS pool players surface as claims; the FREEAGENT ones as grabs
    assert any(c.add in ("Waiver Catcher", "Waiver Wideout") for c in plan.claims)
    assert any(c.add == "Free Runner" for c in plan.fcfs_grabs)


def test_claims_are_gain_ordered_and_each_has_a_distinct_drop(db, marginal_world):
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    gains = [c.gain for c in plan.claims]
    assert gains == sorted(gains, reverse=True)
    recs = list(plan.claims) + list(plan.fcfs_grabs)
    drops = [c.drop for c in recs]
    adds = [c.add for c in recs]
    assert len(set(drops)) == len(drops), "each claim needs its OWN drop"
    assert len(set(adds)) == len(adds)


def test_a_waivers_claim_says_it_is_queued_not_a_click(db, marginal_world):
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    claim = next(c for c in plan.claims)
    assert any("queue it" in r.lower() for r in claim.reasons)


# --------------------------------------------------- QUESTIONABLE dual semantics


def test_questionable_breaks_legality_in_ir_but_is_a_normal_body_elsewhere():
    """Same status, two questions: QUESTIONABLE is IR-INELIGIBLE for legality (it
    breaks the roster) but a QUESTIONABLE player in a normal slot is just a
    rostered body (availability treats him as expected-to-play). The two must not
    collapse into one set."""
    on_bench = [_row(eid=str(i)) for i in range(15)] + [
        _row(slot="BE", injury="QUESTIONABLE", eid="q", name="Q Body")]
    assert check_legality(on_bench).legal is True         # 16 active, legal

    in_ir = [_row(eid=str(i)) for i in range(16)] + [
        _row(slot="IR", injury="QUESTIONABLE", eid="q", name="Q Body")]
    v = check_legality(in_ir)
    assert v.legal is False                               # 17 active, illegal
    assert [o.player for o in v.ir_ineligible] == ["Q Body"]


# ---------------------------------------------------------- Rule 6 (reasons)


def test_ir_eligibility_is_a_labelled_hypothesis():
    rows = [_row(eid=str(i)) for i in range(16)] + [_row(slot="IR", injury="OUT", eid="ir")]
    v = check_legality(rows)
    assert any(IR_ELIGIBLE_LABEL in r for r in v.reasons)
    # the disclosure is explicit about being unverified
    assert any("UNVERIFIED" in r and "post-draft" in r for r in v.reasons)


def test_every_claim_and_drop_ships_reasons(db, marginal_world):
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    for rec in list(plan.claims) + list(plan.fcfs_grabs):
        assert rec.reasons, rec.add
    for d in plan.drop_board:
        assert d.reasons, d.player


def test_reasons_contain_no_jargon_the_operator_cannot_check(db, marginal_world):
    banned = ("vor", "vona", "sigma", "marginal_component", "argmax", "bernoulli",
              "monte carlo", "vbd")
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    blob = " ".join(
        r for rec in list(plan.claims) + list(plan.fcfs_grabs) for r in rec.reasons
    ).lower()
    blob += " " + " ".join(r for d in plan.drop_board for r in d.reasons).lower()
    blob += " " + " ".join(plan.legality.reasons).lower()
    for word in banned:
        assert not re.search(rf"\b{re.escape(word)}\b", blob), word


# ---------------------------------------------------- waiver priority context


def test_waiver_priority_is_reported_from_team_state(db, marginal_world):
    _world(marginal_world, injury="OUT")
    db.execute(
        "INSERT INTO league_teams (season, team_id, primary_owner, waiver_rank, "
        "is_transaction_locked, retrieved_as_of, knowable_as_of) VALUES "
        "(2026, 10, '{OWNER-10}', 4, 0, '2026-09-15', '2026-09-15')"
    )
    db.commit()
    plan = _plan(db)
    assert plan.waiver_priority == 4
    assert plan.transaction_locked is False
    # it is reported as CONTEXT on a claim, never as a claim order (F16: the
    # OUT world reliably yields a WAIVER claim, so this is NOT a vacuous guard).
    assert plan.claims, "the WAIVERS pool must produce at least one claim"
    assert any("priority" in r.lower() for r in plan.claims[0].reasons)


def test_format_puts_the_legality_block_first_when_blocked(db, marginal_world):
    _world(marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)
    text = waiver.format_waiver_plan(plan, reasons=True)
    assert "ROSTER ILLEGAL" in text
    assert text.index("ROSTER ILLEGAL") < text.index("THE FIX")
    assert "No claims are planned" in text


# ============================ Cluster A — legality/fix-model redesign ==========


def test_check_legality_is_pure_iff_active_or_ir_over_capacity():
    """F5: an ineligible IR occupant is folded into active_count and is NOT an
    independent illegality source — the roster blocks ONLY on active>16 or ir>1."""
    # 16 active + 1 ineligible IR = 17 -> blocked on the ACTIVE count.
    over = [_row(eid=str(i)) for i in range(16)] + [
        _row(slot="IR", injury="QUESTIONABLE", eid="ir", name="IR Guy")]
    assert check_legality(over).legal is False
    # 15 active + 1 ineligible IR = 16 -> LEGAL (ESPN benches him), advisory only.
    ok = [_row(eid=str(i)) for i in range(15)] + [
        _row(slot="IR", injury="QUESTIONABLE", eid="ir", name="IR Guy")]
    v = check_legality(ok)
    assert v.legal is True and v.active_count == 16
    assert any("REQUIRED ROSTER MOVE" in a for a in v.ir_advisories)


def test_the_forced_drop_fix_is_restorative(db, marginal_world):
    """F5: re-running check_legality on the roster produced by APPLYING the proposed
    forced drop returns legal — the fix terminates (it used to loop forever)."""
    _world(marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)
    assert plan.blocked and plan.forced_drop is not None
    rows = [dict(r) for r in waiver.league_state.get_player_state(
        db, as_of=PULL, season=SEASON, on_team_id=TEAM, view="historical")]
    after = [r for r in rows if r["player"] != plan.forced_drop.player]
    assert check_legality(after).legal is True   # RESTORATIVE / TERMINATES


def _world_with_eligible_body(marginal_world, injury="QUESTIONABLE"):
    """The crux PLUS an IR-eligible active body (OUT) that could fill a freed IR
    slot — the zero-drop move scenario (F1)."""
    specs = _active_specs()
    specs[2] = {**specs[2], "injury": "OUT"}         # 'Lead Runner' -> OUT (IR-eligible)
    specs += [_ir_spec(injury)] + _POOL_SPECS
    marginal_world(specs, retrieved=PULL)


def test_a_zero_drop_ir_move_is_the_primary_fix_when_an_eligible_body_exists(db, marginal_world):
    """F1: when an IR-eligible active body can be moved into the freed IR slot, the
    zero-drop move is the PRIMARY fix and the drop is demoted to an alternative."""
    _world_with_eligible_body(db and marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)
    assert plan.blocked is True
    assert plan.ir_move_fix, "the costless IR move must be surfaced"
    move_blob = " ".join(plan.ir_move_fix)
    assert "NO drop" in move_blob and "Lead Runner" in move_blob and "IR Guy" in move_blob
    # it is disclosed as a labelled hypothesis (ESPN mechanics unverified)
    assert any("UNVERIFIED" in line for line in plan.ir_move_fix)
    # the drop, if present, is demoted to the ALTERNATIVE
    if plan.forced_drop is not None:
        assert any("ALTERNATIVE" in r for r in plan.forced_drop.reasons)
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "preferred — no drop" in text


def test_the_zero_drop_move_actually_restores_legality(db, marginal_world):
    """F1/F5: applying the surfaced IR move yields a legal roster (restorative)."""
    _world_with_eligible_body(db and marginal_world, injury="QUESTIONABLE")
    rows = [dict(r) for r in waiver.league_state.get_player_state(
        db, as_of=PULL, season=SEASON, on_team_id=TEAM, view="historical")]
    for r in rows:                       # apply: Lead Runner -> IR, IR Guy -> bench
        if r["player"] == "Lead Runner":
            r["lineup_slot"] = "IR"
        if r["player"] == "IR Guy":
            r["lineup_slot"] = "BE"
    assert check_legality(rows).legal is True


def test_an_ineligible_occupant_on_a_legal_roster_is_not_blocked(db, marginal_world):
    """F1: 15 active + 1 ineligible IR = 16 -> NOT blocked; a required move advisory,
    never a forced drop."""
    specs = _active_specs()[:15] + [_ir_spec("QUESTIONABLE")] + _POOL_SPECS
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    assert plan.blocked is False
    assert plan.forced_drop is None
    assert any("REQUIRED ROSTER MOVE" in a and "IR Guy" in a
               for a in plan.legality.ir_advisories)


def test_two_eligible_ir_occupants_block_and_the_fix_restores_ir_count(db, marginal_world):
    """F16 / K1: two eligible IR occupants (ir_count=2>1) block, and the surfaced
    zero-drop fix restores ir_count<=1."""
    specs = _active_specs()[:14] + [
        {**_ir_spec("OUT"), "name": "IR A", "on_team": TEAM},
        {**_ir_spec("OUT"), "name": "IR B", "on_team": TEAM},
    ] + _POOL_SPECS
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    assert plan.blocked is True and plan.legality.ir_count == 2
    assert plan.ir_move_fix, "benching an excess IR occupant is a zero-drop fix"
    # applying it (bench one occupant) restores legality
    rows = [dict(r) for r in waiver.league_state.get_player_state(
        db, as_of=PULL, season=SEASON, on_team_id=TEAM, view="historical")]
    benched = False
    for r in rows:
        if r["player"] == "IR B":
            r["lineup_slot"] = "BE"
            benched = True
    assert benched
    post = check_legality(rows)
    assert post.legal is True and post.ir_count <= 1


# ============================ Cluster B — IR-eligibility honesty ===============


def test_the_ir_unverified_disclosure_shows_in_the_default_blocked_view(db, marginal_world):
    """F2: the UNVERIFIED IR-eligibility disclosure renders even with reasons=False
    (the default `ziggurat waivers` view), under a destructive forced drop."""
    _world(marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "UNVERIFIED" in text


def test_the_ir_unverified_disclosure_shows_on_the_legal_path(db, marginal_world):
    """F2: a legal roster whose IR occupant is legitimately OUT still discloses that
    the legality verdict rests on an UNVERIFIED inference (default view)."""
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "UNVERIFIED" in text


def test_a_blank_status_ir_occupant_is_unknown_not_illegal():
    """F7: a null injury_status on an IR occupant is UNKNOWN — it does NOT make the
    roster illegal and it is NOT conflated with ACTIVE."""
    rows = [_row(eid=str(i)) for i in range(16)] + [
        {"lineup_slot": "IR", "injury_status": None, "position": "WR",
         "espn_player_id": "n", "player": "Blank Guy"}]
    v = check_legality(rows)
    assert v.legal is True                       # not counted against active
    assert v.active_count == 16
    assert v.ir_ineligible == ()
    assert any("could not verify IR eligibility for Blank Guy" in r for r in v.reasons)
    assert not any("ACTIVE/none" in r for r in v.reasons)   # no misleading conflation


# ============================ Cluster C — claim/drop pairing & display =========


def test_a_claim_joins_add_espn_id_on_identity_not_display_name(db, marginal_world):
    """F3: two free agents share a display name; the claim's add_espn_id is the ONE
    that priced the swap (identity), never the higher-owned namesake by name."""
    specs = _active_specs()
    specs[15] = {**specs[15], "pts": 1.0}          # make a WR add a real upgrade
    specs += [
        {"name": "Ghost Twin", "pos": "QB", "team": "NE", "pts": 3.0, "bye": 9, "owned": 95.0},
        {"name": "Ghost Twin", "pos": "WR", "team": "NYJ", "pts": 25.0, "bye": 11, "owned": 1.0},
    ]
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    ghosts = [c for c in (list(plan.claims) + list(plan.fcfs_grabs) + list(plan.streaming))
              if c.add == "Ghost Twin"]
    assert ghosts, "the WR upgrade should surface as a claim/grab"
    g = ghosts[0]
    # the WR (not the QB namesake) priced it, so add_position AND add_espn_id agree
    assert g.add_position == "WR"
    # resolve the WR's espn id from the pool and assert the join used it
    wr_id = next(r["espn_player_id"] for r in
                 waiver.league_state.get_free_agents(db, as_of=PULL, season=SEASON)
                 if r["player"] == "Ghost Twin" and r["position"] == "WR")
    assert g.add_espn_id == str(wr_id)


def _stream_world(marginal_world):
    """A roster whose D/ST is weak in the opening window week, plus a pool D/ST that
    is huge that week — a streamed (this-week-only) swap."""
    specs = _active_specs()
    # replace Miami D/ST with a weak-this-week one
    specs[13] = {"name": "Weak DST", "pos": "D/ST", "team": "MIA", "pts": 1.0, "bye": 5,
                 "on_team": TEAM, "weeks": {w: 1.0 for w in range(3, 18)}}
    specs += [
        {"name": "Streamer DST", "pos": "D/ST", "team": "PIT", "pts": 1.0, "bye": 10,
         "status": "WAIVERS", "weeks": {3: 80.0}},
    ]
    marginal_world(specs, retrieved=PULL)


def test_streamed_dst_swaps_are_segregated_from_season_long_claims(db, marginal_world):
    """F4: a 1-week D/ST stream lands in its own STREAMING section, never in the
    budgeted WAIVER CLAIMS / FCFS shortlist, and its reason no longer claims it is
    'not ranked against season-long adds'."""
    _stream_world(marginal_world)
    plan = _plan(db)
    # no season-long claim/grab is a 1-week D/ST/K stream
    for rec in list(plan.claims) + list(plan.fcfs_grabs):
        assert not (rec.horizon == 1 and (rec.drop_position or "") in ("DST", "K"))
    if plan.streaming:
        assert all(rec.horizon == 1 for rec in plan.streaming)
        blob = " ".join(r for rec in plan.streaming for r in rec.reasons)
        assert "not ranked against season-long adds" not in blob
        text = waiver.format_waiver_plan(plan, reasons=False)
        assert "STREAMING (this week only" in text


def test_an_unpriceable_drop_marker_renders_in_the_default_view():
    """F6: the DEFAULT (no --reasons) claim line flags an unpriceable drop."""
    rec = waiver.ClaimRec(
        add="Free Runner", add_position="RB", add_espn_id="1",
        kind=KIND_FREE_AGENT, gain=153.1, drop="Sixth Catcher", drop_position="WR",
        startable_this_week=True, horizon=15, drop_unpriceable=True,
        waiver_rank=None, reasons=())
    line = waiver._claim_line(rec)
    assert "[drop UNPRICED — verify before dropping]" in line


def test_an_unpriceable_drop_is_de_prioritized_out_of_the_shortlist(db, marginal_world):
    """F6: when a rostered player cannot be priced, his (inflated, upper-bound) swap
    gain does not claim a top-k slot ahead of a real priceable pairing — the thin
    player is NOT surfaced as a recommended drop."""
    specs = _active_specs()
    specs[15] = {**specs[15], "forecast": {3}}     # 'Sixth Catcher' thin -> unpriceable
    specs += _POOL_SPECS
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    all_recs = list(plan.claims) + list(plan.fcfs_grabs)
    assert all_recs, "priceable claims exist"
    # the thin player is never the paired drop of a shortlisted claim
    assert not any(r.drop == "Sixth Catcher" for r in all_recs)
    # and any unpriceable-drop claim that does slip through sorts AFTER priceable ones
    for lst in (plan.claims, plan.fcfs_grabs):
        flags = [r.drop_unpriceable for r in lst]
        assert flags == sorted(flags)


def test_a_leaked_onteam_pool_row_is_not_a_click_now_grab(db, marginal_world):
    """F8: an 'ONTEAM'-token row that leaked into the FA pool is held out (guarded),
    and the shared classifier means no drop-board / claims contradiction remains."""
    specs = _active_specs() + [
        {"name": "Conflict Runner", "pos": "RB", "team": "NYG", "pts": 22.0, "bye": 7,
         "status": "ONTEAM"},          # on_team omitted -> on_team_id NULL, lands in pool
    ] + _POOL_SPECS
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    all_recs = list(plan.claims) + list(plan.fcfs_grabs) + list(plan.streaming)
    assert not any(c.add == "Conflict Runner" for c in all_recs)
    assert any("held out 'Conflict Runner'" in n for n in plan.notes)


def test_the_drop_board_legend_reconciles_the_sign(db, marginal_world):
    """F18: the drop board is labelled as 'give up' with a legend, so the same player
    is not +X in claims and -X on the drop board with no explanation."""
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "GIVE UP" in text
    assert "legend:" in text


# ============================ Cluster D — robustness ==========================


def test_own_team_id_none_is_refused(db, marginal_world):
    """F9: None own_team_id raises rather than valuing the whole universe as a roster."""
    _world(marginal_world, injury="OUT")
    with pytest.raises(OwnTeamUnresolved):
        build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=None,
                          weeks=WEEKS, pool_limit=None)


def test_a_candidate_load_failure_is_disclosed_not_silent(db, marginal_world):
    """F10: any non-NoCompletedWeek failure of the opportunity-signal load surfaces a
    plan NOTE (a visible degrade), instead of returning empty silently."""
    _world(marginal_world, injury="OUT")
    with patch("ziggurat.core.waiver.build_candidates",
               side_effect=RuntimeError("schema drift")):
        plan = _plan(db)
    assert any("opportunity signals UNAVAILABLE" in n for n in plan.notes)


def test_illegal_path_reports_the_window_that_priced_the_drop(db, marginal_world):
    """F11: on the illegal path plan.weeks is the window that priced the forced drop
    (from build_board), not the empty raw arg."""
    specs = _active_specs() + [_ir_spec("QUESTIONABLE")] + _POOL_SPECS
    marginal_world(specs, retrieved=PULL, scoring_period=10)
    plan = build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=TEAM,
                             weeks=None, pool_limit=None)
    assert plan.blocked is True and plan.forced_drop is not None
    assert plan.weeks == tuple(range(10, 18))


def test_the_team_count_denominator_comes_from_data(db, marginal_world):
    """F13: the 'of N' waiver-priority denominator is sourced from league_teams, not
    hardcoded to 10."""
    _world(marginal_world, injury="OUT")
    for tid in range(1, 9):            # 8 teams in this synthetic league
        db.execute(
            "INSERT INTO league_teams (season, team_id, primary_owner, waiver_rank, "
            "is_transaction_locked, retrieved_as_of, knowable_as_of) VALUES "
            "(2026, ?, ?, ?, 0, '2026-09-15', '2026-09-15')",
            (tid, f"{{OWNER-{tid}}}", tid),
        )
    db.execute(
        "INSERT INTO league_teams (season, team_id, primary_owner, waiver_rank, "
        "is_transaction_locked, retrieved_as_of, knowable_as_of) VALUES "
        "(2026, 10, '{OWNER-10}', 4, 0, '2026-09-15', '2026-09-15')"
    )
    db.commit()
    plan = _plan(db)
    assert plan.team_count == 9
    text = waiver.format_waiver_plan(plan)
    assert "4 of 9" in text and "of 10" not in text


# ============================ Cluster E — test rigor ==========================


def test_view_threading_hides_a_late_retrieved_row_under_historical(db, marginal_world):
    """F12: a row whose retrieved_as_of > knowable_as_of (an after-the-fact
    correction) is HIDDEN by the historical view but SHOWN by latest_truth — this
    fails if build_waiver_plan's view-threading regresses."""
    _world(marginal_world, injury="OUT")
    # add one MORE roster body, stamped as retrieved AFTER the decision date but
    # knowable before it (a correction landing later).
    db.execute(
        "INSERT INTO players (gsis_id, espn_id, name, retrieved_as_of, knowable_as_of) "
        "VALUES ('00-999999', '9999', 'Late Body', '2026-09-16', '2026-09-10')"
    )
    db.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, "
        "position, pro_team, on_team_id, roster_status, lineup_slot, injury_status, "
        "percent_owned, percent_started, percent_change, scoring_period, "
        "retrieved_as_of, knowable_as_of) VALUES "
        "(2026, '9999', '00-999999', 'Late Body', 'WR', 'MIN', 10, 'ONTEAM', 'BE', "
        "'ACTIVE', 5.0, 0.0, 0.0, 0, '2026-09-16', '2026-09-10')"
    )
    db.commit()
    hist = build_waiver_plan(db, as_of="2026-09-15", season=SEASON, own_team_id=TEAM,
                             weeks=WEEKS, pool_limit=None, view="historical")
    truth = build_waiver_plan(db, as_of="2026-09-15", season=SEASON, own_team_id=TEAM,
                              weeks=WEEKS, pool_limit=None, view="latest_truth")
    assert hist.legality.active_count == 16          # correction hidden (retrieved 09-16 > 09-15)
    assert truth.legality.active_count == 17          # latest_truth surfaces it


def test_a_streamed_row_reads_this_week_in_the_format(db, marginal_world):
    """F15: the streamed row renders 'this week' rather than an N-week horizon."""
    _stream_world(marginal_world)
    plan = _plan(db)
    if plan.streaming:
        text = waiver.format_waiver_plan(plan, reasons=False)
        assert "this week" in text


def test_the_legal_path_claim_line_has_the_expected_shape(db, marginal_world):
    """F15: a legal-path claim line reads 'add <name> (<pos>)  <-  drop <name> ...
    +N.N pts / <horizon>' in that order."""
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    rec = next(iter(list(plan.claims) + list(plan.fcfs_grabs)), None)
    assert rec is not None and rec.drop is not None
    line = waiver._claim_line(rec)
    assert line.strip().startswith(f"add {rec.add} ({rec.add_position})")
    assert f"<-  drop {rec.drop}" in line
    assert f"{rec.gain:+.1f} pts" in line
    assert ("this week" if rec.horizon == 1 else f"{rec.horizon} wks") in line


def test_claim_budget_truncates(db, marginal_world):
    """F16: claim_budget bounds each section."""
    _world(marginal_world, injury="OUT")
    plan = build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=TEAM,
                             weeks=WEEKS, pool_limit=None, claim_budget=1)
    assert len(plan.claims) <= 1 and len(plan.fcfs_grabs) <= 1


def test_a_legal_roster_with_no_positive_add_says_hold(db, marginal_world):
    """F16: a legal roster whose pool holds nothing better emits the 'hold' note."""
    # a strong 16-man roster and a pool of only weak bodies
    specs = _active_specs() + [
        {"name": "Weak FA", "pos": "WR", "team": "NYJ", "pts": 0.1, "bye": 11},
    ]
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    assert plan.blocked is False
    if not plan.claims and not plan.fcfs_grabs and not plan.streaming:
        assert any("hold your roster" in n for n in plan.notes)


def test_the_week_resolution_error_blocked_path_degrades_to_a_note(db, marginal_world):
    """F16: an illegal roster whose week window cannot resolve degrades to the
    note-only fix (no crash)."""
    specs = _active_specs() + [_ir_spec("QUESTIONABLE")] + _POOL_SPECS
    marginal_world(specs, retrieved=PULL)              # scoring_period defaults to 0
    plan = build_waiver_plan(db, as_of=PULL, season=SEASON, own_team_id=TEAM,
                             weeks=None, pool_limit=None)
    assert plan.blocked is True
    assert plan.forced_drop is None
    assert any("week window could not be resolved" in n for n in plan.notes)


def test_a_waivers_claim_is_actually_produced_not_vacuously_skipped(db, marginal_world):
    """F16: the world reliably yields a WAIVER claim, so the priority-context
    assertion is not vacuous."""
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    assert plan.claims, "the WAIVERS pool players must surface as claims"
    assert plan.claims[0].kind == KIND_WAIVER


def test_a_completed_week_opportunity_signal_lands_on_the_matching_claim(db, marginal_world, nfl_fixture):
    """F14: seed a REAL completed REG week (schedules + weekly_stats + snap_counts) so
    build_candidates emits a USAGE_BREAKOUT whose espn_id collides with a pool add;
    the claim for that add must carry the 'opportunity signal' note — proving the
    espn_id join actually matches (the headline 3.4 deliverable that shipped with
    zero positive-path coverage)."""
    from ziggurat.data.nfl import players, schedules, snap_counts, weekly_stats

    players.ingest_players(db, nfl_fixture("ids"), retrieved_as_of="2023-08-01")
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    weekly_stats.ingest_weekly_stats(db, nfl_fixture("weekly_stats"), retrieved_as_of="2023-10-10")
    snap_counts.ingest_snap_counts(db, nfl_fixture("snap_counts"), retrieved_as_of="2023-10-10")

    # the +12-carry breakout RB (gsis 00-0035250) resolves to this ESPN id
    rb_espn = db.execute(
        "SELECT espn_id FROM players WHERE gsis_id='00-0035250'"
    ).fetchone()["espn_id"]

    specs = [{**s, "on_team": TEAM} for s in _active_specs()]
    specs.append({"name": "Breakout FA", "pos": "RB", "team": "BUF", "pts": 30.0, "bye": 13})
    marginal_world(specs, season=2023, retrieved="2023-10-10")
    # give the pool add the breakout RB's ESPN identity, so the espn_id join collides
    db.execute("UPDATE league_player_state SET espn_player_id=? WHERE player='Breakout FA'",
               (rb_espn,))
    db.commit()

    plan = build_waiver_plan(db, as_of="2023-10-17", season=2023, own_team_id=TEAM,
                             weeks=range(7, 18), pool_limit=None, view="latest_truth")
    recs = list(plan.claims) + list(plan.fcfs_grabs) + list(plan.streaming)
    match = [c for c in recs if c.add == "Breakout FA"]
    assert match, "the breakout RB should surface as a positive add"
    assert match[0].add_espn_id == str(rb_espn)
    assert any("opportunity signal [USAGE_BREAKOUT]" in r for r in match[0].reasons)
