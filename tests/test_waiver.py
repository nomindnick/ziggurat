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
from dataclasses import replace
from unittest.mock import patch

import pytest

from ziggurat.core import candidates as C
from ziggurat.core import waiver
from ziggurat.core.marginal import SwapRow
from ziggurat.core.valuation import DEFAULT_ROSTER
from ziggurat.core.waiver import (
    IR_ELIGIBLE_LABEL,
    KIND_FREE_AGENT,
    KIND_WAIVER,
    USAGE_EVIDENCE_HEADER,
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
    """Item 3.8a SPLIT this label, and the split is the point.

    The DESIGNATION half is settled: ESPN's own ``injured`` flag marks exactly
    OUT/INJURY_RESERVE, measured 2026-09-02 with zero exceptions. The IR-SLOT
    MECHANISM half is not — no roster in this league has ever used the slot — so
    "UNVERIFIED" must survive on that half and ONLY that half. The old assertion
    pinned "UNVERIFIED ... post-draft" on the settled sentence; a rewrite that
    dropped the word entirely would have passed a laxer test.
    """
    rows = [_row(eid=str(i)) for i in range(16)] + [_row(slot="IR", injury="OUT", eid="ir")]
    v = check_legality(rows)
    assert any(IR_ELIGIBLE_LABEL in r for r in v.reasons)
    # the SETTLED half no longer calls itself unverified, and no longer defers to
    # a post-draft app check that has now happened
    assert "UNVERIFIED" not in IR_ELIGIBLE_LABEL
    assert "post-draft" not in IR_ELIGIBLE_LABEL
    assert "measured 2026-09-02" in IR_ELIGIBLE_LABEL
    # the MECHANISM half still is unverified, and says what settles it
    assert "UNVERIFIED" in waiver.IR_FIX_MODEL_LABEL
    assert "IR slot" in waiver.IR_FIX_MODEL_LABEL
    # and the verdict discloses HOW this occupant was decided (Rule 6)
    assert any("injury tag" in r for r in v.ir_flag_notes)


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
    # it is disclosed as a labelled hypothesis — and since item 3.8a the label is
    # narrowed to the IR-SLOT MECHANICS, which is the half still unverified
    assert any("UNVERIFIED" in line for line in plan.ir_move_fix)
    assert any("no roster in this league has ever used the IR slot" in line
               for line in plan.ir_move_fix)
    # the DESTINATION's "(IR-eligible)" ships its per-player evidence — which field
    # decided it (ESPN's own flag, or the injury-tag proxy when the flag was not
    # captured), the same line the occupant rows carry (item 3.8a audit)
    evidence = [line for line in plan.ir_move_fix if line.startswith("Lead Runner:")]
    assert len(evidence) == 1, plan.ir_move_fix
    assert "`injured` flag" in evidence[0] and "ELIGIBLE" in evidence[0]
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
    """F2: the IR disclosure renders even with reasons=False (the default
    `ziggurat waivers` view), under a destructive forced drop.

    Item 3.8a split it: the still-UNVERIFIED half is the IR SLOT MECHANISM, and a
    per-occupant line now says WHICH signal decided his eligibility."""
    _world(marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "UNVERIFIED" in text
    assert "what ESPN's IR SLOT itself accepts" in text
    # the flag was not captured for this synthetic row, so the PROXY path is
    # disclosed per player rather than as a blanket sentence
    assert "flag was NOT captured for this player" in text


def test_the_ir_unverified_disclosure_shows_on_the_legal_path(db, marginal_world):
    """F2: a legal roster whose IR occupant is legitimately OUT still discloses
    that the legality verdict rests on an UNVERIFIED IR-slot mechanism (default
    view) — narrowed by item 3.8a, but still on the default page."""
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "UNVERIFIED" in text
    assert "what ESPN's IR SLOT itself accepts" in text


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
    # item 4.2b, B3: the degrade note used to read as if the CLAIMS had lost
    # something load-bearing. They never used this column; say so in the same
    # breath as the alarm, or a novice re-reads a correct chain as damaged.
    assert any("claim ORDER is unaffected (it never used this column)" in n
               for n in plan.notes)


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
    """F15: a legal-path claim line reads '#K add <name> (<pos>)  <-  drop <name> ...
    +N.N pts / <horizon>' in that order.

    The '#K' is item 3.4b's audit fix: the two sections are split by ACTION (queue
    overnight vs click now), so the chain runs across them and the PRINTED order is
    not the chain order whenever a grab interleaves. Without the number on the line
    the operator cannot recover it."""
    _world(marginal_world, injury="OUT")
    plan = _plan(db)
    rec = next(iter(list(plan.claims) + list(plan.fcfs_grabs)), None)
    assert rec is not None and rec.drop is not None
    line = waiver._claim_line(rec)
    assert line.strip().startswith(f"#{rec.chain_rank} add {rec.add} ({rec.add_position})")
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
    # item 4.2b, B3: the evidence rows now sit under ONE header that states what
    # they are not, and each carries its NEW/REPEAT badge. With no decision
    # archive in this fixture the honest badge is FIRST SEEN, never "NEW".
    reasons = match[0].reasons
    assert USAGE_EVIDENCE_HEADER in reasons
    assert reasons.count(USAGE_EVIDENCE_HEADER) == 1, "the header lands once per claim"
    evidence = [r for r in reasons if r.startswith("  [USAGE_BREAKOUT] ")]
    assert evidence, f"no usage-evidence row in {reasons}"
    assert all(C.EPISODE_FIRST_SEEN in r for r in evidence)
    # and it is the LAST block of the claim's reasons — appended after the chain
    # was selected, which is what makes the header true.
    assert reasons.index(USAGE_EVIDENCE_HEADER) > 0
    assert reasons[-1] == evidence[-1]



# ==================== Cluster F — item 3.4b: claims are a CHAIN ===============
#
# The defect these pin: every SwapRow.gain prices its move as if it were the ONLY
# one you make, so the pre-3.4b "rank them, print the top k" quoted each claim
# against a roster that stops existing the moment the claim above it wins.
# Measured live 2026-09-01: three adds worth +5.55 / +5.25 / +2.21 each ALONE were
# worth +1.22 together, and the next morning the same module recommended reversing
# all three off unchanged projections. Every player below is invented (Rule 5).
#
# The fixture reproduces the live SHAPE, not the live numbers: a deep, cheap bench
# (six running backs, four of them buried) whose bodies are the cheapest drops, and
# a pool of wide receivers who each look like a large upgrade ALONE because they
# would seat the single FLEX slot — which only one of them can ever do.


_CHAIN_ROSTER = [
    {"name": "Alpha Thrower", "pos": "QB", "team": "TEN", "pts": 22.0, "bye": 6,
     "on_team": TEAM},
    {"name": "Bravo Thrower", "pos": "QB", "team": "NO", "pts": 12.0, "bye": 8,
     "on_team": TEAM},
    # Two starting RBs and FOUR buried ones: the buried bodies are the cheapest
    # drops on the board, which is what lets a chain eat into real depth.
    {"name": "Charlie Rusher", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11,
     "on_team": TEAM},
    {"name": "Delta Rusher", "pos": "RB", "team": "BUF", "pts": 16.0, "bye": 7,
     "on_team": TEAM},
    {"name": "Echo Rusher", "pos": "RB", "team": "CHI", "pts": 10.0, "bye": 9,
     "on_team": TEAM},
    {"name": "Foxtrot Rusher", "pos": "RB", "team": "GB", "pts": 9.0, "bye": 10,
     "on_team": TEAM},
    {"name": "Golf Rusher", "pos": "RB", "team": "SF", "pts": 8.0, "bye": 12,
     "on_team": TEAM},
    {"name": "Hotel Rusher", "pos": "RB", "team": "SEA", "pts": 7.0, "bye": 5,
     "on_team": TEAM},
    {"name": "India Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8,
     "on_team": TEAM},
    {"name": "Juliett Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9,
     "on_team": TEAM},
    {"name": "Kilo Catcher", "pos": "WR", "team": "HOU", "pts": 14.0, "bye": 12,
     "on_team": TEAM},
    {"name": "Papa Catcher", "pos": "WR", "team": "LV", "pts": 13.0, "bye": 7,
     "on_team": TEAM},
    {"name": "Lima Endzone", "pos": "TE", "team": "IND", "pts": 11.0, "bye": 13,
     "on_team": TEAM},
    {"name": "Mike Endzone", "pos": "TE", "team": "MIN", "pts": 10.0, "bye": 6,
     "on_team": TEAM},
    {"name": "November Boot", "pos": "K", "team": "KC", "pts": 8.0, "bye": 14,
     "on_team": TEAM},
    {"name": "Oscar D/ST", "pos": "D/ST", "team": "MIA", "pts": 7.0, "bye": 5,
     "on_team": TEAM, "weeks": _ODD},
]

# Three receivers, each positive ALONE. Only one FLEX slot exists, so the second is
# worth far less once the first has won and the third is worth LESS THAN NOTHING.
_CHAIN_POOL = [
    {"name": "Quebec Catcher", "pos": "WR", "team": "NYJ", "pts": 20.0, "bye": 11,
     "status": "WAIVERS"},
    {"name": "Romeo Catcher", "pos": "WR", "team": "PIT", "pts": 19.0, "bye": 10,
     "status": "WAIVERS"},
    {"name": "Sierra Catcher", "pos": "WR", "team": "NYG", "pts": 14.0, "bye": 12,
     "status": "WAIVERS"},
]


def _chain_world(marginal_world, roster=None, pool=None):
    marginal_world(list(_CHAIN_ROSTER if roster is None else roster)
                   + list(_CHAIN_POOL if pool is None else pool), retrieved=PULL)


def _chain_plan(db, **kw):
    kw.setdefault("claim_budget", 10)
    return _plan(db, **kw)


def _all_recs(plan):
    return list(plan.claims) + list(plan.fcfs_grabs)


def _chain_board(db, roster_specs=None):
    """The raw board behind the chain fixture — the pre-chain standalone numbers."""
    from ziggurat.core.marginal import build_board
    roster = [dict(r) for r in waiver.league_state.get_player_state(
        db, as_of=PULL, season=SEASON, on_team_id=TEAM)]
    pool = [dict(r) for r in waiver.league_state.get_free_agents(
        db, as_of=PULL, season=SEASON)]
    return build_board(db, as_of=PULL, season=SEASON, roster=roster, pool=pool,
                       weeks=WEEKS, pool_limit=None)


def test_the_claim_list_is_priced_as_a_chain_not_as_independent_moves(db, marginal_world):
    """D1 REGRESSION. The printed list no longer over-promises. The joint gain is
    strictly LESS than the sum of the standalone gains the module used to print,
    a later claim is worth measurably less in the chain than alone, and an add
    that is positive ALONE is REFUSED with a measured negative 'after' number —
    the flat-ridge reversal that had the tool recommending the opposite of
    yesterday's advice off unchanged projections."""
    _chain_world(marginal_world)
    plan = _chain_plan(db)
    recs = _all_recs(plan)
    assert len(recs) == 2, [(r.add, r.drop) for r in recs]

    # ranks are 1..n across claims + grabs, no gaps, no duplicates
    assert sorted(r.chain_rank for r in recs) == [1, 2]

    # what the OLD list implicitly promised, vs what the chain actually pays
    promised = sum(r.gain_alone for r in recs)
    assert plan.chain_gain < promised - 5.0, (plan.chain_gain, promised)
    later = next(r for r in recs if r.chain_rank == 2)
    assert later.gain < later.gain_alone - 5.0, (later.gain, later.gain_alone)

    # the refusal: positive alone, measured <= 0 once the chain above has won
    assert plan.chain_rejected, "a positive-alone add must be refused here"
    worst = plan.chain_rejected[0]
    assert worst.add == "Sierra Catcher"
    assert worst.gain_alone > 1.0 > 0.0 > worst.gain_after
    assert plan.chain_stop == waiver.STOP_NONPOSITIVE
    # and the plan SAYS a short list is the answer, not a truncation
    assert any("SHORT list is the answer" in n for n in plan.notes)


def test_the_joint_gain_equals_the_sum_of_the_conditional_gains(db, marginal_world):
    """The telescoping identity is the whole design: ``chain_gain`` is measured as
    value_after(everything) − value_after(nothing) and each printed gain is
    value_after(k) − value_after(k−1), so the printed numbers must ADD UP to the
    joint total. Accumulating (``base += g``) is the easy way to make this pass
    while the numbers drift, so it is asserted at 1e-9."""
    _chain_world(marginal_world)
    plan = _chain_plan(db)
    recs = _all_recs(plan)
    assert recs
    assert plan.chain_gain == pytest.approx(sum(r.gain for r in recs), abs=1e-9)

    # rank 1's conditional gain IS its standalone gain, and both equal the board's
    # own re-priced SwapRow gain — the two estimators cannot have drifted apart.
    first = next(r for r in recs if r.chain_rank == 1)
    assert first.gain == pytest.approx(first.gain_alone, abs=1e-9)
    board = _chain_board(db)
    row = next(s for s in board.swaps if s.add == first.add and s.drop == first.drop)
    assert first.gain == pytest.approx(row.gain, abs=1e-6)
    assert board.value_after([row]) - board.value_after() == pytest.approx(
        row.gain, abs=1e-6)

    # INDEPENDENT recomputation of the joint number from the board itself. Without
    # this, a chain that accumulated (base += g) instead of re-measuring would pass
    # the identity above trivially — both sides would be the same running sum.
    rows = [next(s for s in board.swaps if s.add == r.add and s.drop == r.drop)
            for r in recs]
    assert plan.chain_gain == pytest.approx(
        board.value_after(rows) - board.value_after(), abs=1e-6)


def test_the_chain_stops_at_the_first_non_positive_gain(db, marginal_world):
    """With budget to spare the chain stops on ECONOMICS and says so."""
    _chain_world(marginal_world)
    plan = _chain_plan(db, claim_budget=10)
    assert plan.chain_stop == waiver.STOP_NONPOSITIVE
    assert len(_all_recs(plan)) == 2 < 10          # stopped on its own, not on the cap


def test_claim_budget_caps_the_whole_chain_and_says_which(db, marginal_world):
    """The other stop: a tight budget truncates, and the note says THAT rather than
    'the next claim is worth nothing'. ``claim_budget`` is now a TOTAL cap over
    claims + grabs (it used to cap each bucket separately)."""
    _chain_world(marginal_world)
    plan = _chain_plan(db, claim_budget=1)
    assert len(_all_recs(plan)) == 1
    assert len(plan.claims) + len(plan.fcfs_grabs) <= 1
    assert plan.chain_stop == waiver.STOP_BUDGET
    assert any("--claim-budget" in n and "NOT because" in n for n in plan.notes)


class _FakeBoard:
    """A board whose ``value_after`` is SUPERMODULAR: B is worth more once A has
    won. Real lineup objectives are essentially never like this, which is exactly
    why the complement case needs a constructed one rather than a fixture."""

    # The real board exposes the caps its matrix was filtered with (item 3.8a) and
    # ``_select_claims`` reads them from there rather than from the module constant,
    # so the double has to carry them too. ``league_limits = None`` is the THIRD
    # state — no settings row was readable — and is deliberately not the same as an
    # empty mapping (audit fix): the page must not claim a fence it never read.
    position_caps = waiver.POSITION_CAPS
    league_limits = None

    def __init__(self, values):
        self.values = values
        self.calls = 0

    def value_after(self, swaps=(), *, pure_adds=()):
        self.calls += 1
        return self.values[frozenset(s.add for s in tuple(swaps) + tuple(pure_adds))]


def _fake_swap(add, gain, drop, *, add_id, drop_id):
    return SwapRow(
        add=add, drop=drop, gain=gain, add_position="WR", drop_position="RB",
        add_status="WAIVERS", add_startable_this_week=True, horizon_weeks=15,
        reasons=(f"add {add}, drop {drop}",), add_espn_id=add_id, drop_espn_id=drop_id,
    )


def test_a_complementary_add_is_accepted_and_reports_the_HIGHER_number(db):
    """Lazy re-evaluation is exact under diminishing returns and a HEURISTIC
    otherwise. A complement — an add worth MORE once another has won — must still
    be accepted, and must report the conditional (higher) number, not the stale
    standalone one it was seeded with. Nothing may raise.

    A is worth 10 alone, B is worth 4 alone, and together they are worth 20 — so
    B's true conditional gain is 10, two and a half times its seed."""
    a = _fake_swap("A Catcher", 10.0, "Y Rusher", add_id="a", drop_id="y")
    b = _fake_swap("B Catcher", 4.0, "Z Rusher", add_id="b", drop_id="z")
    board = _FakeBoard({
        frozenset(): 0.0,
        frozenset({"A Catcher"}): 10.0,
        frozenset({"B Catcher"}): 4.0,
        frozenset({"A Catcher", "B Catcher"}): 20.0,
    })
    chain = waiver._select_claims(
        [a, b], board=board, claim_budget=10, waiver_rank=None, team_count=None,
        open_slots=0, candidate_notes={}, dup_names=set(),
        # A fake board has no roster, so caps-off is a DELIBERATE choice here, not
        # a forgotten argument: `position_counts` is required precisely so that a
        # caller cannot disable the cross-chain POSITION_CAPS guard by omission.
        position_counts={},
    )
    recs = list(chain.claims) + list(chain.grabs)
    assert [(r.chain_rank, r.add) for r in recs] == [(1, "A Catcher"), (2, "B Catcher")]
    second = recs[1]
    assert second.gain == pytest.approx(10.0) and second.gain_alone == pytest.approx(4.0)
    assert second.gain > second.gain_alone
    assert chain.chain_gain == pytest.approx(20.0)
    assert chain.chain_gain == pytest.approx(sum(r.gain for r in recs))
    # the reason states BOTH numbers, so the operator can see the assumption
    blob = " ".join(second.reasons)
    assert "+10.0" in blob and "+4.0" in blob


def test_distinct_add_and_drop_identities_survive_the_chain(db, marginal_world):
    """Item 3.4's own pairing invariants are not collateral damage: identity is
    still the ESPN id, and no player is spent twice across the whole chain."""
    _chain_world(marginal_world)
    plan = _chain_plan(db)
    recs = _all_recs(plan)
    assert len(recs) >= 2
    adds = [r.add_espn_id for r in recs]
    drops = [r.drop for r in recs if r.drop is not None]
    assert all(a is not None for a in adds)
    assert len(set(adds)) == len(adds)
    assert len(set(drops)) == len(drops)


def test_an_open_slot_is_priced_as_a_pure_add_not_as_the_paired_swap(db, marginal_world):
    """Item 3.4b fixes a number that was quietly wrong: a pure add used to QUOTE the
    gain of the swap it was found through, which understates it (adding without
    dropping is never worse than swapping). It is now priced as roster + add."""
    short = [s for s in _CHAIN_ROSTER if s["name"] != "Hotel Rusher"]     # 15 active
    _chain_world(marginal_world, roster=short)
    plan = _chain_plan(db)
    assert plan.legality.active_count == 15
    top = _all_recs(plan)[0]
    assert top.chain_rank == 1 and top.drop is None and top.drop_position is None
    assert any("pure ADD" in r for r in top.reasons)

    board = _chain_board(db)
    paired = max(s.gain for s in board.swaps if s.add == top.add)
    assert top.gain > paired, (top.gain, paired)     # strictly better, not "at least"
    assert any("that ordering is an assumption" in n for n in plan.notes)

    # AND no phantom drop anywhere in its reasons (item 3.4b audit). The swap
    # matrix's own lead sentence names the drop the pure add was FOUND through and
    # quotes that swap's number — a third figure, and an instruction to drop a
    # player the row's own last bullet says needs no drop. Every rec whose drop is
    # None must be free of it, and its lead sentence must quote the reported gain.
    roster_names = {s["name"] for s in short}
    for rec in _all_recs(plan):
        if rec.drop is not None:
            continue
        for r in rec.reasons:
            for nm in roster_names:
                assert nm not in r, (rec.add, nm, r)
        assert f"{rec.gain:+.1f} house pts" in rec.reasons[0], rec.reasons[0]


def test_the_streaming_lane_is_untouched_by_the_chain(db, marginal_world):
    """FREEZE. The streamed D/ST lane sits OUTSIDE the chain entirely: same rows,
    same one-week horizon, ``chain_rank`` 0, ``gain_alone == gain``, and a rendered
    line byte-identical to the pre-3.4b one (no 'if the claims above win' suffix).
    ``value_after`` cannot price a one-week move against a season-long roster and
    raises rather than try, so this is a correctness boundary, not cosmetics."""
    _stream_world(marginal_world)
    plan = _plan(db, claim_budget=10)
    assert plan.streaming
    for rec in plan.streaming:
        assert rec.chain_rank == 0
        assert rec.gain_alone == rec.gain
        assert rec.horizon == 1
        line = waiver._claim_line(rec)
        assert "if the claims above win" not in line
        assert f"{rec.gain:+.1f} pts / this week" in line
    # ... and the board itself refuses to price one season-long
    board = _chain_board(db)
    streamed = [s for s in board.swaps if waiver._is_streamed(s)]
    assert streamed
    with pytest.raises(ValueError, match="season-long only"):
        board.value_after([streamed[0]])


def test_the_chain_search_is_lazy_not_a_full_re_evaluation(db, marginal_world):
    """COST FENCE. The chain must not decay into 'just re-price everything each
    step': that is len(candidates) x chain_len valuations, measured at 15.2 s on the
    live board against 2.6 s lazily — enough on its own to bust the item's runtime
    budget. Counted on the BOARD rather than on ScenarioModel, because resolving
    ``board.swaps`` itself runs 2 + N valuations BEFORE selection starts, so a
    global counter would measure re-pricing and call it selection."""
    _chain_world(marginal_world)
    board = _chain_board(db)
    swaps = board.swaps                      # resolve BEFORE counting

    class Counting:
        def __init__(self, inner):
            self._inner, self.calls = inner, 0
            # forward the real board's enforced caps (item 3.8a)
            self.position_caps = inner.position_caps

        def value_after(self, sw=(), *, pure_adds=()):
            self.calls += 1
            return self._inner.value_after(sw, pure_adds=pure_adds)

    counter = Counting(board)
    chain = waiver._select_claims(
        swaps, board=counter, claim_budget=10, waiver_rank=None, team_count=None,
        open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts=board.roster_position_counts,
    )
    seasonal = [s for s in swaps if not waiver._is_streamed(s)]
    chain_len = len(chain.claims) + len(chain.grabs)
    assert chain_len >= 2 and len(seasonal) > chain_len
    assert counter.calls < len(seasonal) * chain_len, (counter.calls, len(seasonal))
    # sharper: a NON-lazy greedy re-prices every candidate on its FIRST step alone,
    # so it cannot come in under len(seasonal) calls. The lazy one does.
    assert counter.calls < len(seasonal), (counter.calls, len(seasonal))
    assert counter.calls <= waiver.CHAIN_EVAL_BUDGET


def test_the_chain_is_deterministic_across_runs(db, marginal_world):
    """Same inputs -> the same chain, in the same order, to the last bit. The
    valuation is order-sensitive at ~1e-13 (float summation order), which is enough
    to flip an exact tie, so the roster keys are canonically sorted before pricing
    and the tie ladder is a total order."""
    _chain_world(marginal_world)
    a = _chain_plan(db)
    b = _chain_plan(db)

    def shape(p):
        return [(r.chain_rank, r.add, r.drop, r.gain, r.gain_alone) for r in _all_recs(p)]

    assert shape(a) == shape(b)
    assert a.chain_gain == b.chain_gain
    assert a.chain_stop == b.chain_stop
    assert [(c.add, c.drop, c.gain_alone, c.gain_after) for c in a.chain_rejected] == \
           [(c.add, c.drop, c.gain_alone, c.gain_after) for c in b.chain_rejected]


def test_the_default_view_shows_the_joint_total_and_both_numbers(db, marginal_world):
    """RULE 6. The operator reads the DEFAULT view (and so does the Wednesday
    briefing, which renders exactly this with ``reasons=False``). The joint total,
    the priced-in-order instruction and the refused reversals all have to be there:
    a novice who sees two claims each worth '+60' will queue both."""
    _chain_world(marginal_world)
    plan = _chain_plan(db)
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "IF EVERY CLAIM AND GRAB LISTED WINS:" in text
    assert f"{plan.chain_gain:+.1f} pts over" in text
    assert "priced in NUMBER order" in text
    # both numbers on a chained line, and the rank that makes the order recoverable
    later = next(r for r in _all_recs(plan) if r.chain_rank > 1)
    above = "#1 lands" if later.chain_rank == 2 else f"#1-#{later.chain_rank - 1} land"
    assert f"if {above} ({later.gain_alone:+.1f} alone)" in text
    assert "#1-#1" not in text          # a range of one is not a spelling
    # The ACTION prints before every refusal: the page is opened to learn what to
    # queue, and the refusals are worded against "the moves above".
    assert text.index("WAIVER CLAIMS") < text.index("FREE-AGENT GRABS") \
        < text.index("positive ALONE, measured at <= 0") < text.index("DROP BOARD")
    # rank 1 carries its number too, and no conditional suffix (nothing is above it)
    first = next(r for r in _all_recs(plan) if r.chain_rank == 1)
    assert f"#1 add {first.add}" in text
    assert f"{first.gain:+.1f} pts / {first.horizon} wks\n" in text + "\n"
    # the refused reversals, in the DEFAULT view — EVERY one of them, in the same
    # labelled 'add X (POS) <- drop Y (POS)' vocabulary the claim lines use. The
    # shipped one-example bare-arrow form was measurably inverted by the briefing
    # summarizer, and rows 2 and 3 were counted but never named at any verbosity.
    assert "positive ALONE, measured at <= 0" in text
    for r in plan.chain_rejected:
        assert f"add {r.add} ({r.add_position})" in text
        assert f"{r.gain_after:+.1f} after" in text
        if r.drop:
            assert f"drop {r.drop} ({r.drop_position or '-'})" in text
    # and the ESPN batch semantics that make "fallback" executable
    assert "INSTEAD OF" in text
    # no raw float noise anywhere the operator reads
    assert "e-09" not in text and "e-06" not in text


def test_the_chained_suffix_names_a_single_move_or_a_range():
    """RULE 6. Position 2 assumes ONE move landed, so it says so — ``#1-#1`` is a
    range of one and reads as a typo. From position 3 the range is real."""
    assert waiver._above_phrase(2) == "#1 lands"
    assert waiver._above_phrase(3) == "#1-#2 land"
    assert waiver._above_phrase(5) == "#1-#4 land"


def test_the_chain_reads_nothing_new_from_the_database(db, marginal_world):
    """LEAKAGE / Rule 1. The chain re-prices through ScenarioModel, which is pure
    arithmetic over entries the board already gated at ``as_of`` — so selection must
    issue ZERO further queries. Scoped to ``_select_claims`` deliberately:
    ``build_waiver_plan`` itself reads (the opportunity-signal join), so a fence
    around the whole plan would be flaky by design rather than load-bearing."""
    _chain_world(marginal_world)
    board = _chain_board(db)
    swaps = board.swaps

    seen: list[str] = []
    db.set_trace_callback(seen.append)
    try:
        chain = waiver._select_claims(
            swaps, board=board, claim_budget=10, waiver_rank=None, team_count=None,
            open_slots=0, candidate_notes={}, dup_names=set(),
            position_counts=board.roster_position_counts,
        )
    finally:
        db.set_trace_callback(None)
    assert chain.claims or chain.grabs
    assert seen == [], seen


# --------------------------------------------------------------------------
# item 3.4b audit — the mechanisms the first build shipped without a pin
# --------------------------------------------------------------------------


class _FuncBoard:
    """A board whose value is an explicit FUNCTION of the applied add set — for
    cases where enumerating every subset by hand would be noise rather than the
    thing under test."""

    # See _FakeBoard: _select_claims reads the enforced caps off the board (3.8a).
    position_caps = waiver.POSITION_CAPS

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def value_after(self, swaps=(), *, pure_adds=()):
        self.calls += 1
        return self.fn(frozenset(s.add for s in tuple(swaps) + tuple(pure_adds)))


def _fs(add, gain, drop, *, add_id, drop_id, add_pos="WR", drop_pos="RB",
        status="WAIVERS", unpriceable=False):
    """``_fake_swap`` with the positions and the drop-priceability exposed."""
    return SwapRow(
        add=add, drop=drop, gain=gain, add_position=add_pos, drop_position=drop_pos,
        add_status=status, add_startable_this_week=True, horizon_weeks=15,
        reasons=(f"add {add} ({add_pos}), drop {drop} ({drop_pos}): "
                 f"{gain:+.1f} house pts over 15 weeks",),
        add_espn_id=add_id, drop_espn_id=drop_id, drop_unpriceable=unpriceable,
    )


def test_a_leftover_measured_POSITIVE_after_the_chain_is_reported_not_discarded():
    """The lazy greedy is a heuristic for COMPLEMENTS, and the shipped build turned
    that caveat into a silent deletion: a leftover the rejection pass re-priced at
    a POSITIVE conditional gain was ``continue``d — absent from the claims, from
    ``chain_rejected`` AND from ``chain_not_repriced`` — while the plan printed
    'a SHORT list is the answer here, not a truncation' and 'It cannot make one
    disappear'. Both sentences were false about a number the module was holding.

    A/B/D/E over a supermodular table: the chain takes A then E and stops on D's
    fresh -0.5, leaving C measured at +6.0 against the finished chain."""
    a = _fs("Alpha Catcher", 10.0, "Sierra Rusher", add_id="a", drop_id="s")
    b = _fs("Bravo Catcher", 5.0, "Tango Rusher", add_id="b", drop_id="t")
    c = _fs("Charlie Catcher", 4.0, "Uniform Rusher", add_id="c", drop_id="u")
    e = _fs("Echo Catcher", 3.0, "Victor Rusher", add_id="e", drop_id="v")
    V = {
        frozenset(): 0.0,
        frozenset({"Alpha Catcher"}): 10.0,
        frozenset({"Bravo Catcher"}): 5.0,
        frozenset({"Charlie Catcher"}): 4.0,
        frozenset({"Echo Catcher"}): 3.0,
        frozenset({"Alpha Catcher", "Bravo Catcher"}): 9.5,
        frozenset({"Alpha Catcher", "Charlie Catcher"}): 9.0,
        frozenset({"Alpha Catcher", "Echo Catcher"}): 12.0,
        frozenset({"Alpha Catcher", "Echo Catcher", "Bravo Catcher"}): 11.5,
        frozenset({"Alpha Catcher", "Echo Catcher", "Charlie Catcher"}): 18.0,
    }
    chain = waiver._select_claims(
        [a, b, c, e], board=_FakeBoard(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts={},
    )
    assert chain.chain_stop == waiver.STOP_NONPOSITIVE
    assert [r.add for r in chain.claims] == ["Alpha Catcher", "Echo Catcher"]

    # Charlie is measured POSITIVE after the chain and must be REPORTED, not binned.
    assert [r.add for r in chain.chain_under_ranked] == ["Charlie Catcher"]
    assert chain.chain_under_ranked[0].gain_after == pytest.approx(6.0)
    assert chain.chain_under_ranked[0].gain_alone == pytest.approx(4.0)

    # ... and the notes must stop saying the two things it falsifies.
    notes = waiver._chain_notes(chain, claim_budget=10, weeks=15)
    joined = " ".join(notes)
    assert "It cannot make one disappear" not in joined
    assert "not a truncation" not in joined
    assert "still positive against the finished list" in joined


def test_an_under_ranked_positive_row_is_RENDERED_in_the_default_view(db, marginal_world):
    """...and it has to reach the page, not just the dataclass. The DEFAULT view is
    what the operator and the Wednesday briefing read."""
    _chain_world(marginal_world)
    plan = _chain_plan(db)
    row = waiver.ChainRejection(
        add="Victor Catcher", add_position="WR", drop="Whiskey Rusher",
        drop_position="RB", gain_alone=4.0, gain_after=6.0,
        reason="Victor Catcher measures +6.0 AFTER the moves above have won.",
    )
    seeded = replace(plan, chain_under_ranked=(row,))
    text = waiver.format_waiver_plan(seeded, reasons=False)
    assert "POSITIVE AFTER THE CHAIN" in text
    assert "add Victor Catcher (WR)  <-  drop Whiskey Rusher (RB)" in text
    assert "+4.0 alone / +6.0 after" in text
    # the reason itself is a --reasons detail, not default-view noise
    assert row.reason not in text
    assert row.reason in waiver.format_waiver_plan(seeded, reasons=True)


def test_every_leftover_lands_in_exactly_one_disclosed_bucket():
    """ACCOUNTING. Nothing the rejection pass touches may leave it unreported: the
    four buckets plus the unmeasured count must add up to the leftovers, or a
    future ``continue`` can silently eat a candidate again."""
    a = _fs("Alpha Catcher", 10.0, "Sierra Rusher", add_id="a", drop_id="s")
    rest = [
        _fs(f"Rest{i} Catcher", 9.0 - i, f"Drop{i} Rusher", add_id=f"r{i}",
            drop_id=f"d{i}")
        for i in range(6)
    ]
    V = {frozenset(): 0.0}
    V[frozenset({"Alpha Catcher"})] = 10.0
    for r in rest:
        V[frozenset({r.add})] = r.gain
        V[frozenset({"Alpha Catcher", r.add})] = 10.0 - 0.5   # every pair is worse
    chain = waiver._select_claims(
        [a, *rest], board=_FakeBoard(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts={},
    )
    assert len(chain.claims) == 1
    leftovers = 6
    accounted = (
        len(chain.chain_rejected) + chain.chain_measured_not_shown
        + len(chain.chain_under_ranked) + len(chain.chain_capped)
        + chain.chain_not_repriced
    )
    assert accounted == leftovers, (
        chain.chain_rejected, chain.chain_measured_not_shown,
        chain.chain_under_ranked, chain.chain_capped, chain.chain_not_repriced)
    # and the display cap is a DISPLAY cap, not a measurement claim
    assert len(chain.chain_rejected) == waiver.CHAIN_REJECTED_PRICED
    assert chain.chain_measured_not_shown + chain.chain_not_repriced == 3


def test_position_caps_are_re_checked_ACROSS_the_chain():
    """``POSITION_CAPS`` used to be checked per-swap against the BASE roster only,
    so two individually-legal adds could jointly breach a cap. The board is
    strictly ADDITIVE here, so economics cannot be what stops the second add —
    only the cap can. Deleting the ``cap_ok`` guard makes this test fail."""
    caps = waiver.POSITION_CAPS["QB"]
    one = _fs("One Thrower", 10.0, "Sierra Rusher", add_id="1", drop_id="s",
              add_pos="QB")
    two = _fs("Two Thrower", 9.0, "Tango Rusher", add_id="2", drop_id="t",
              add_pos="QB")
    V = {
        frozenset(): 0.0,
        frozenset({"One Thrower"}): 10.0,
        frozenset({"Two Thrower"}): 9.0,
        frozenset({"One Thrower", "Two Thrower"}): 19.0,      # additive
    }
    at_cap_minus_one = {"QB": caps - 1, "RB": 5}
    chain = waiver._select_claims(
        [one, two], board=_FakeBoard(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts=at_cap_minus_one,
    )
    assert [r.add for r in chain.claims] == ["One Thrower"], "the cap is not enforced"

    # ... and the SAME board with headroom takes both, so the cap is what did it.
    loose = waiver._select_claims(
        [one, two], board=_FakeBoard(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts={"QB": 0, "RB": 5},
    )
    assert [r.add for r in loose.claims] == ["One Thrower", "Two Thrower"]


def test_a_cap_blocked_leftover_is_labelled_a_cap_not_an_economic_refusal():
    """A cap refusal must not be dressed as a valuation. The shipped build gave a
    cap-blocked row the 'winning him too would undo part of them' sentence — which
    blames the chain for a roster limit — and, if it re-priced POSITIVE, dropped it
    entirely. It also drove the exhaustion note to assert the wrong cause."""
    caps = waiver.POSITION_CAPS["QB"]
    one = _fs("One Thrower", 10.0, "Sierra Rusher", add_id="1", drop_id="s",
              add_pos="QB")
    two = _fs("Two Thrower", 9.0, "Tango Rusher", add_id="2", drop_id="t",
              add_pos="QB")
    V = {
        frozenset(): 0.0,
        frozenset({"One Thrower"}): 10.0,
        frozenset({"Two Thrower"}): 9.0,
        frozenset({"One Thrower", "Two Thrower"}): 19.0,
    }
    chain = waiver._select_claims(
        [one, two], board=_FakeBoard(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts={"QB": caps - 1, "RB": 5},
    )
    assert [r.add for r in chain.chain_capped] == ["Two Thrower"]
    assert not chain.chain_rejected and not chain.chain_under_ranked
    reason = chain.chain_capped[0].reason
    # Item 3.8a: the reason names WHICH of the two fences bound. This board carries
    # no league limit, so it must say the module guard — and it must NOT go on
    # claiming "not a league rule" unconditionally now that a league rule exists.
    assert f"the binding limit is {caps}" in reason
    # This board double carries NO league limits (`league_limits is None`), which
    # is a THIRD state, not the module-guard state: the page must say the league's
    # own limit was not captured rather than assert a fence nobody read (audit fix).
    assert "item 3.2's POSITION_CAPS" in reason
    assert "NOT CAPTURED" in reason
    assert "undo part of them" not in reason

    # the exhaustion note names the CAP, not a spent player
    assert chain.chain_stop == waiver.STOP_EXHAUSTED
    notes = " ".join(waiver._chain_notes(chain, claim_budget=10, weeks=15))
    assert "reuses a player already spent above" not in notes
    assert "over the binding limit for its position" in notes
    # ...and with no league limits read, it says ONE fence applied, not two.
    assert "Only ONE fence applied here" in notes
    assert "TWO fences apply" not in notes


def test_a_cap_excluded_pure_add_does_not_let_phase_a_settle_the_whole_search():
    """Phase A's shortcut rests on 'a pure add is never worse than the same add as
    a swap' — true only for adds phase A could SEE. ``cap_ok(s, pure=True)`` counts
    the add WITHOUT the offsetting drop, so a same-position swap at a capped
    position is legal in phase B and invisible in phase A. The shipped build let a
    worthless pure add settle the entire search and skip that swap."""
    caps = waiver.POSITION_CAPS["TE"]
    upgrade = _fs("Great Endzone", 9.0, "Weak Endzone", add_id="g", drop_id="w",
                  add_pos="TE", drop_pos="TE")
    dud = _fs("Dud Catcher", 0.5, "Sierra Rusher", add_id="d", drop_id="s")
    V = {
        frozenset(): 0.0,
        frozenset({"Great Endzone"}): 9.0,
        frozenset({"Dud Catcher"}): 0.0,          # worth NOTHING as a pure add
        frozenset({"Great Endzone", "Dud Catcher"}): 9.0,
    }
    chain = waiver._select_claims(
        [upgrade, dud], board=_FakeBoard(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=1, candidate_notes={}, dup_names=set(),
        position_counts={"TE": caps, "RB": 5, "WR": 4},
    )
    assert [r.add for r in chain.claims] == ["Great Endzone"], (
        "phase A settled on a pure-add argument that never covered the capped swap")


def test_a_phase_a_cost_stop_never_kills_the_swap_lane(monkeypatch):
    """Phase A is exhaustive per open slot; phase B is the cheap lazy half where
    the swaps live. A COST exhaustion in phase A is not evidence that no swap pays,
    so it must not set a terminal stop — measured on the live board, two open slots
    burned 194 of 200 valuations in phase A and phase B never ran."""
    monkeypatch.setattr(waiver, "CHAIN_EVAL_BUDGET", 6)
    monkeypatch.setattr(waiver, "PHASE_B_EVAL_RESERVE", 4)
    rows = [
        _fs(f"Pure{i} Catcher", 5.0 - i * 0.1, f"Drop{i} Rusher", add_id=f"p{i}",
            drop_id=f"q{i}")
        for i in range(6)
    ]
    weight = {r.add: r.gain for r in rows}
    # Strictly diminishing: only the two best adds ever pay, so the board is
    # submodular and nothing here depends on a contrived complement.
    board = _FuncBoard(lambda ks: sum(sorted((weight[k] for k in ks),
                                             reverse=True)[:2]))
    chain = waiver._select_claims(
        rows, board=board, claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=2, candidate_notes={}, dup_names=set(),
        position_counts={},
    )
    assert chain.phase_a_truncated
    assert chain.chain_stop != waiver.STOP_NONPOSITIVE
    # phase B still ran on its reserve and took something
    assert chain.claims, "a phase-A cost stop starved the swap lane"
    notes = " ".join(waiver._chain_notes(chain, claim_budget=10, weeks=15))
    assert "cost limit on the open-slot half only" in notes


def test_the_evaluation_ceiling_stops_the_chain_LOUDLY(monkeypatch):
    """The ceiling exists so a post-Week-1 board cannot silently blow the runtime
    budget. Nothing pinned its degrade path: the only assertion referencing it was
    ``calls <= CHAIN_EVAL_BUDGET``, which RAISING the constant satisfies."""
    monkeypatch.setattr(waiver, "CHAIN_EVAL_BUDGET", 4)
    rows = [
        _fs(f"Cand{i} Catcher", 5.0 - i * 0.1, f"Drop{i} Rusher", add_id=f"c{i}",
            drop_id=f"e{i}")
        for i in range(8)
    ]
    V = {frozenset(): 0.0}
    for r in rows:
        V[frozenset({r.add})] = r.gain
        for r2 in rows:
            if r2 is not r:
                V[frozenset({r.add, r2.add})] = max(r.gain, r2.gain) + 0.05
    chain = waiver._select_claims(
        rows, board=_FakeBoard(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts={},
    )
    assert chain.chain_stop == waiver.STOP_EVAL_BUDGET
    notes = " ".join(waiver._chain_notes(chain, claim_budget=10, weeks=15))
    assert "cost limit, not a verdict" in notes
    # and it must never be reported as an economic conclusion
    assert "not a truncation" not in notes


def test_the_not_repriced_note_never_claims_a_pricing_that_did_not_happen(monkeypatch):
    """The shipped note said '(only the top 3 are, to keep this report inside a few
    seconds)' unconditionally — including when the evaluation ceiling meant ZERO
    were re-priced and ``chain_rejected`` was empty. It told the operator three
    refusals were measured when the tool measured none."""
    chain = waiver._ChainResult(
        claims=(), grabs=(), streaming=(), chain_gain=0.0, chain_rejected=(),
        chain_not_repriced=79, chain_stop=waiver.STOP_EVAL_BUDGET, evaluations=203,
    )
    notes = " ".join(waiver._chain_notes(chain, claim_budget=10, weeks=15))
    assert "NONE of them were" in notes
    assert f"only the top {waiver.CHAIN_REJECTED_PRICED} are" not in notes


def test_a_leftover_the_chain_ALREADY_measured_is_not_reported_as_unmeasured():
    """FRESHNESS BEFORE THE DISPLAY CAP. The shipped pass counted every row past the
    cap into ``chain_not_repriced`` — 'treat them as unmeasured, not as rejected' —
    without first checking ``at[i] == len(accepted)``, the module's own 'this row
    already carries a post-chain number' flag. Measured live, 10 of 17 such rows
    were fresh and strongly negative: the sentence was false for the majority of
    the rows it covered, at zero cost to fix."""
    a = _fs("Alpha Catcher", 10.0, "Sierra Rusher", add_id="a", drop_id="s")
    rest = [
        _fs(f"Rest{i} Catcher", 9.0 - i, f"Drop{i} Rusher", add_id=f"r{i}",
            drop_id=f"d{i}")
        for i in range(6)
    ]
    V = {frozenset(): 0.0, frozenset({"Alpha Catcher"}): 10.0}
    for r in rest:
        V[frozenset({r.add})] = r.gain
        V[frozenset({"Alpha Catcher", r.add})] = 9.5          # every pair is worse
    chain = waiver._select_claims(
        [a, *rest], board=_FakeBoard(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts={},
    )
    # the chain re-priced several leftovers on its way to stopping; those numbers
    # are in hand, so they must be classified as REFUSALS, not disclaimed
    measured = len(chain.chain_rejected) + chain.chain_measured_not_shown
    assert measured >= 4, (measured, chain.chain_not_repriced)
    notes = " ".join(waiver._chain_notes(chain, claim_budget=10, weeks=15))
    if chain.chain_measured_not_shown:
        assert "also MEASURED at" in notes


def test_claim_budget_zero_does_not_claim_the_pool_is_worthless(db, marginal_world):
    """'You asked me not to look' is not 'there were no candidates'. At budget 0 the
    shipped plan asserted 'every add here would cost a drop worth more than the
    add' — a specific, checkable claim, false, and made without running a single
    valuation."""
    _chain_world(marginal_world)
    plan = _plan(db, claim_budget=0)
    assert not plan.claims and not plan.fcfs_grabs
    assert plan.chain_stop == waiver.STOP_BUDGET
    joined = " ".join(plan.notes)
    assert "no add in the free-agent pool improves this roster" not in joined
    assert "--claim-budget is 0" in joined and "says nothing about the free-agent pool" in joined
    # ... while a positive add demonstrably exists
    deep = _plan(db, claim_budget=10)
    assert deep.claims or deep.fcfs_grabs


def test_a_streaming_only_plan_does_not_print_chain_instructions(db, marginal_world):
    """A live streaming lane with an empty season-long chain routed the plan into
    the chain notes, which printed 'these 0 add(s) are priced as a CHAIN, in the
    order printed ... Queue them in that order' over an empty list — AND suppressed
    the one sentence that is the answer on such a day."""
    _stream_world(marginal_world)
    plan = _plan(db, claim_budget=10)
    assert plan.streaming and not plan.claims and not plan.fcfs_grabs
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "priced as a CHAIN" not in text
    assert "Queue them in that order" not in text
    assert "no SEASON-LONG add in the free-agent pool improves this roster" in text


def test_the_streaming_slice_is_disclosed_when_it_hides_a_positive_stream(db, marginal_world):
    """``claim_budget`` slices the STREAMING lane too, and the pre-3.4b sentence
    that covered that ('the cap TRUNCATES each claim list') was replaced by one
    that is true of claims+grabs and false of streaming. A truncated list printed
    under a note reading 'a SHORT list is the answer, not a truncation'."""
    specs = _active_specs()
    specs[13] = {"name": "Weak DST", "pos": "D/ST", "team": "MIA", "pts": 1.0,
                 "bye": 5, "on_team": TEAM,
                 "weeks": {w: 1.0 for w in range(3, 18)}}
    specs += [
        {"name": "Streamer DST", "pos": "D/ST", "team": "PIT", "pts": 1.0, "bye": 10,
         "status": "WAIVERS", "weeks": {3: 80.0}},
        {"name": "Backup DST", "pos": "D/ST", "team": "CLE", "pts": 1.0, "bye": 11,
         "status": "WAIVERS", "weeks": {3: 40.0}},
    ]
    marginal_world(specs, retrieved=PULL)

    deep = _plan(db, claim_budget=10)
    assert len(deep.streaming) == 2, [r.add for r in deep.streaming]
    shallow = _plan(db, claim_budget=1)
    assert len(shallow.streaming) == 1
    joined = " ".join(shallow.notes)
    assert "one-week STREAM(s) are positive but not shown" in joined
    # ... and the deep run, which hides nothing, must not claim it did
    assert "not shown" not in " ".join(deep.notes)


def test_the_joint_line_reads_this_week_in_a_one_week_window(db, marginal_world):
    """ONE rendering of a horizon per report. The joint line hard-coded '{n} wks',
    so a one-week window — reachable on a plain in-season run in week 17 — printed
    'over 1 wks' above claim lines saying '/ this week'."""
    _chain_world(marginal_world)
    plan = _chain_plan(db)
    one = replace(plan, weeks=(17,))
    text = waiver.format_waiver_plan(one, reasons=False)
    assert "1 wks" not in text and "1 wk(s)" not in text
    assert "pts over this week" in text


def test_the_drop_board_says_it_is_priced_BEFORE_the_chain(db, marginal_world):
    """The drop board comes from the single pre-chain ``build_board`` scan while the
    claims above it are priced sequentially on top of it — two lanes, two rosters,
    in one view. Unlabelled it reads as a menu that can be combined with the chain,
    and it can point at exactly the drop the chain has just refused."""
    _chain_world(marginal_world)
    plan = _chain_plan(db)
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "these numbers assume you make NONE of the" in text
    assert "can only be added ONCE" in text
    spent = {r.drop for r in _all_recs(plan) if r.drop}
    for name in spent:
        assert f"{name} (" in text
    assert "already spent" in text


def test_the_chain_notes_are_absent_when_there_is_no_chain():
    """``_chain_notes`` must not narrate an ordering that does not exist. Guarded
    at the function too, not only at the call site, because it is reachable from
    every stop reason with an empty chain."""
    for stop in (waiver.STOP_NO_CANDIDATES, waiver.STOP_NONPOSITIVE,
                 waiver.STOP_EXHAUSTED, waiver.STOP_BUDGET):
        chain = waiver._ChainResult(
            claims=(), grabs=(), streaming=(), chain_gain=0.0, chain_rejected=(),
            chain_not_repriced=0, chain_stop=stop, evaluations=0,
        )
        notes = " ".join(waiver._chain_notes(chain, claim_budget=3, weeks=15))
        assert "priced as a CHAIN" not in notes, stop
        assert "Queue them in that order" not in notes, stop
        assert "how the next claim is chosen" not in notes, stop


def test_a_position_counts_argument_is_required_not_defaulted():
    """A safety check whose default value is 'off' is the shape the 3.1/3.1b audits
    kept finding: every ``POSITION_CAPS`` entry is >= 1, so an empty mapping makes
    ``0 + 1 <= cap`` true for everything and the cross-chain guard evaporates. The
    caller must disable it on purpose, never by omission."""
    import inspect
    sig = inspect.signature(waiver._select_claims)
    p = sig.parameters["position_counts"]
    assert p.default is inspect.Parameter.empty
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


# ============================================ item 3.8a — ESPN's own flags in the plan


def _row38(slot=None, injury="ACTIVE", pos="WR", eid="1", name="P", **extra):
    return {"lineup_slot": slot, "injury_status": injury, "position": pos,
            "espn_player_id": eid, "player": name, **extra}


def test_espn_own_flag_decides_ir_eligibility_and_the_reason_names_it():
    """ESPN's own `injured` boolean is the same field the app reads, and when we
    have it, it wins (item 3.8a).

    CATCHES a rewrite that stores the flag and keeps inferring from the tag —
    which is what `eligibleSlots` got wrong before it: store the field, assume it
    means something, never actually read it.
    """
    rows = [_row38(eid=str(i)) for i in range(16)] + [
        _row38(slot="IR", injury="INJURY_RESERVE", eid="ir", name="IR Guy", injured=1)]
    v = check_legality(rows)
    assert v.legal is True and v.ir_ineligible == ()
    assert any("ESPN's own `injured` flag reads TRUE" in n for n in v.ir_flag_notes)
    # the PROXY disclosure must NOT fire — the flag was captured for him
    assert not any("was NOT captured" in n for n in v.ir_flag_notes)


def test_the_espn_flag_overrides_a_stale_looking_tag_and_makes_the_roster_illegal():
    """The Tuesday-reset crux, decided by ESPN's own flag: an IR occupant whose
    flag reads FALSE is forced onto the active roster even if his tag still looks
    plausible. 17 active bodies on a 16-slot roster blocks EVERY transaction."""
    rows = [_row38(eid=str(i)) for i in range(16)] + [
        _row38(slot="IR", injury="QUESTIONABLE", eid="ir", name="IR Guy", injured=0)]
    v = check_legality(rows)
    assert v.legal is False
    assert v.active_count == 17
    assert [o.player for o in v.ir_ineligible] == ["IR Guy"]
    assert any("`injured` flag reads FALSE" in n for n in v.ir_flag_notes)


def test_an_uncaptured_flag_falls_back_to_the_proxy_with_a_PER_PLAYER_note():
    """NULL is NOT captured — and that happens on a POST-014 snapshot too, when a
    rostered player is missing from ESPN's pool response (item 3.8a).

    CATCHES a blanket "pre-migration" note: two IR occupants can differ, and the
    disclosure has to be about the PLAYER, not about the migration era.
    """
    rows = [_row38(eid=str(i)) for i in range(15)] + [
        _row38(slot="IR", injury="OUT", eid="a", name="Flagged Guy", injured=1),
        _row38(slot="IR", injury="OUT", eid="b", name="Proxy Guy"),
    ]
    v = check_legality(rows)
    notes = " | ".join(v.ir_flag_notes)
    assert "Proxy Guy: ESPN's `injured` flag was NOT captured" in notes
    assert "Flagged Guy: ESPN's own `injured` flag reads TRUE" in notes
    assert "Flagged Guy: ESPN's `injured` flag was NOT captured" not in notes


def test_an_undroppable_player_is_tagged_on_the_DEFAULT_waivers_page(db, marginal_world):
    """A3: the tag must render WITHOUT --reasons.

    CATCHES putting it in ``reasons``: those render only under --reasons, so on
    the page the operator actually reads, the board would propose a drop ESPN
    refuses with no mark at all.
    """
    specs = _active_specs() + [_ir_spec("OUT")] + _POOL_SPECS
    for s in specs:
        if s["name"] == "Depth Runner":
            s["droppable"] = 0
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    row = next(d for d in plan.drop_board if d.player == "Depth Runner")
    assert row.undroppable is True
    text = waiver.format_waiver_plan(plan, reasons=False)
    assert "Depth Runner" in text
    assert "[UNDROPPABLE" in text
    # and no recommendation anywhere names him as the drop
    for rec in list(plan.claims) + list(plan.fcfs_grabs) + list(plan.streaming):
        assert rec.drop != "Depth Runner"


def test_the_forced_drop_skips_an_undroppable_player(db, marginal_world):
    """THE SECOND DROP PATH (item 3.8a). The illegal-roster fix picks a BOARD row,
    not a swap, so the matrix fence does not cover it — and this is the one
    instruction the operator cannot work around: obeying a drop ESPN refuses
    leaves the roster illegal and every claim blocked.
    """
    specs = _active_specs() + [_ir_spec("QUESTIONABLE")] + _POOL_SPECS
    marginal_world(specs, retrieved=PULL)
    baseline = _plan(db)
    assert baseline.blocked is True and baseline.forced_drop is not None
    named = baseline.forced_drop.player

    # now make exactly that player undroppable and rebuild the world
    db.execute("DELETE FROM league_player_state")
    db.execute("DELETE FROM projections")
    db.execute("DELETE FROM players")
    db.commit()
    specs2 = [dict(s) for s in _active_specs()] + [_ir_spec("QUESTIONABLE")] + _POOL_SPECS
    for s in specs2:
        if s["name"] == named:
            s["droppable"] = 0
    marginal_world(specs2, retrieved=PULL)
    plan = _plan(db)
    assert plan.blocked is True
    assert plan.forced_drop is not None
    assert plan.forced_drop.player != named, "ESPN refuses this drop; naming it is useless"
    assert any("UNDROPPABLE" in n and named in n for n in plan.notes)
    # he is still ON the board, tagged
    assert any(d.player == named and d.undroppable for d in plan.drop_board)
    # ...and the note REACHES the page. The blocked branch used to return before
    # the notes loop, so every disclosure routed there was unreachable text.
    text = waiver.format_waiver_plan(plan)
    assert any(n in text for n in plan.notes), "a blocked page must render its notes"


def test_a_league_position_limit_tighter_than_the_module_guard_binds_the_chain(
        db, marginal_world):
    """A4 (mandatory): today POSITION_CAPS <= the league's limit at every position,
    so this path can ONLY be proved synthetically — and an unexercised fence is
    not a fence. The refusal must name the LEAGUE rule, not the module guard.
    """
    _world(marginal_world, injury="OUT")
    db.execute(
        "INSERT INTO league_settings (season, acquisition_type, lineup_slot_counts, "
        "position_limits, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?)",
        (SEASON, "WAIVERS_TRADITIONAL", '{"QB": 1}',
         # the roster already holds 5 WRs; a league cap of 5 refuses every WR add
         '{"QB": 3, "RB": 8, "WR": 5, "TE": 3, "K": 3, "D/ST": 3}', PULL, PULL),
    )
    db.commit()
    plan = _plan(db, claim_budget=10)
    assert plan.position_caps["WR"] == 5 < waiver.POSITION_CAPS["WR"]
    assert all(r.add_position != "WR" for r in list(plan.claims) + list(plan.fcfs_grabs))
    text = waiver.format_waiver_plan(plan, reasons=True)
    assert "your LEAGUE's own roster limit" in text or \
        any("tighter than this board's guard" in n for n in plan.notes)


def test_the_ir_rule_note_is_silent_when_clean_and_speaks_on_news(db, marginal_world):
    """The operator-attention contract: an interrupt must name an action or stay
    silent. CATCHES an unconditional 'IR RULE CHECK: clean' note on every plan,
    which is how the operator learns to skip the one report that matters."""
    _world(marginal_world, injury="OUT")
    # every row carries a captured flag that agrees with the rule -> clean
    db.execute(
        "UPDATE league_player_state SET injured = "
        "CASE WHEN injury_status IN ('OUT','INJURY_RESERVE') THEN 1 ELSE 0 END")
    db.commit()
    plan = _plan(db)
    assert plan.ir_rule is not None and plan.ir_rule.has_news is False
    assert not any("IR RULE CHECK" in n for n in plan.notes)

    # a designation this league has never served appears -> exactly one note
    db.execute("UPDATE league_player_state SET injury_status = 'DOUBTFUL', injured = 0 "
               "WHERE player = 'Fifth Catcher'")
    db.commit()
    plan2 = _plan(db)
    assert plan2.ir_rule.has_news is True
    assert sum("IR RULE CHECK" in n for n in plan2.notes) == 1
    assert any("DOUBTFUL" in n for n in plan2.notes)


# ==================================== item 3.8a audit fixes (waiver / legality)


def test_the_espn_flag_BEATS_the_tag_when_the_two_DISAGREE():
    """The ONLY rows that can prove flag-first are the ones where the tag says the
    OPPOSITE. Both shipped tests used agreeing pairs — (injured=1,
    INJURY_RESERVE) and (injured=0, QUESTIONABLE) — so a mutant that ignores
    ``injured`` entirely, i.e. exactly the pre-3.8a code, passed the FULL suite
    (measured: 2,702 passed, identical to control). The item's headline behaviour
    change was pinned by nothing.

    CATCHES: reverting ``_ir_status`` to the designation proxy.
    """
    # flag TRUE over an IR-INELIGIBLE tag: the proxy would call him ineligible and
    # make this roster illegal at 17 active. ESPN's own flag says he may sit on IR.
    rows = [_row38(eid=str(i)) for i in range(16)] + [
        _row38(slot="IR", injury="QUESTIONABLE", eid="ir", name="IR Guy", injured=1)]
    v = check_legality(rows)
    assert v.legal is True and v.active_count == 16 and v.ir_ineligible == ()
    assert "reads TRUE, so we treat him as IR-ELIGIBLE" in " | ".join(v.ir_flag_notes)

    # flag FALSE over an IR-ELIGIBLE tag: the proxy would pass an ILLEGAL roster
    # as legal — every transaction blocked in the app with no explanation here.
    rows = [_row38(eid=str(i)) for i in range(16)] + [
        _row38(slot="IR", injury="OUT", eid="ir", name="IR Guy", injured=0)]
    v = check_legality(rows)
    assert v.legal is False and v.active_count == 17
    assert [o.player for o in v.ir_ineligible] == ["IR Guy"]


def test_a_flagged_player_with_a_blank_tag_is_eligible_not_unknown():
    """The third discriminating row: no tag at all. The proxy returns UNKNOWN and
    owes a "could not verify" advisory; the flag answers outright, so none is due.
    """
    rows = [_row38(eid=str(i)) for i in range(16)] + [
        _row38(slot="IR", injury=None, eid="ir", name="IR Guy", injured=1)]
    v = check_legality(rows)
    assert v.legal is True and v.ir_unverified == ()


def test_the_violation_and_the_required_move_name_the_signal_that_DECIDED():
    """``_ir_status`` is flag-first, so on the only rows where the flag changes an
    answer the injury TAG is not the evidence — and both operator-facing sentences
    still quoted it. The page then said "ESPN lists him OUT, not IR-eligible"
    directly above a label stating that OUT is exactly the IR-ELIGIBLE
    designation, with nothing anywhere saying the two ESPN fields DISAGREE.

    CATCHES: rebuilding either sentence from ``o.injury_status`` alone.
    """
    rows = [_row38(eid=str(i)) for i in range(16)] + [
        _row38(slot="IR", injury="OUT", eid="ir", name="IR Guy", injured=0)]
    v = check_legality(rows)
    blob = " | ".join(v.violations + v.ir_advisories)
    assert "`injured` flag reads FALSE" in blob
    assert "DISAGREE" in blob
    assert "ESPN lists him OUT, not IR-eligible" not in blob

    # the PROXY path keeps the tag wording — there the tag really did decide
    proxy = [_row38(eid=str(i)) for i in range(16)] + [
        _row38(slot="IR", injury="QUESTIONABLE", eid="ir", name="IR Guy")]
    pblob = " | ".join(check_legality(proxy).ir_advisories)
    assert "ESPN lists him QUESTIONABLE" in pblob and "PROXY" in pblob


def test_a_blocked_page_renders_every_note_it_holds(db, marginal_world):
    """The blocked branch of ``format_waiver_plan`` returned BEFORE the notes
    loop, so every disclosure routed into ``plan.notes`` was unreachable text on
    the one page where ESPN is blocking all transactions — the IR RULE CHECK
    headline, the undroppable-skip note, item 3.4's alternative-fix option, and
    the board's own "no projections are knowable" caveat.

    CATCHES: re-introducing an early return above the loop.
    """
    _world(marginal_world, injury="QUESTIONABLE")
    plan = _plan(db)
    assert plan.blocked is True and plan.notes
    text = waiver.format_waiver_plan(plan)
    for note in plan.notes:
        assert note in text, "a blocked plan must render every note it holds"


def test_an_all_undroppable_roster_is_refused_WITH_a_reason(db, marginal_world):
    """The new no-fix outcome the item itself introduced: when every priceable row
    is undroppable, ``forced_drop`` is None, and if no IR-eligible body exists the
    IR move is empty too. The page printed the ILLEGAL banner, the violation and
    "No claims are planned until the roster is legal." — an alarm with no fix and
    no reason. Refuse-and-propose became refuse-and-say-nothing.

    CATCHES: a page that names neither a fix nor the reason it cannot.
    """
    specs = [dict(s) for s in _active_specs()] + [_ir_spec("QUESTIONABLE")] + _POOL_SPECS
    for s in specs:
        s["droppable"] = 0
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    assert plan.blocked is True
    assert plan.forced_drop is None
    text = waiver.format_waiver_plan(plan)
    assert "NO FIX THIS TOOL CAN NAME" in text
    assert "UNDROPPABLE" in text.upper()
    # ...and it no longer points at a section this page does not have
    assert "CANNOT VALUE" not in text


def test_no_blocked_note_tells_you_to_drop_an_undroppable_player(db, marginal_world):
    """THE FIFTH DROP PATH (item 3.8a audit). ``marginal.py``'s own enumeration
    closed with "A fifth path is a fifth fence" — and there was one, unfenced: the
    blocked page's "you may instead DROP {occupant} himself" note names an
    IR-ineligible occupant in PROSE, so neither the matrix fence nor the
    forced-drop fence covers it. ESPN's undroppable list is composed of elite
    players and the Tuesday crux is a player hurt enough to occupy IR, so obeying
    it means the app refuses, the roster stays illegal, and every claim stays
    blocked.

    The assertion is an INVARIANT over the whole notes list, not a string match on
    one sentence, so a sixth path added later is caught too.
    """
    specs = [dict(s) for s in _active_specs()] + [_ir_spec("QUESTIONABLE")] + _POOL_SPECS
    for s in specs:
        if s["name"] == "IR Guy":
            s["droppable"] = 0
    marginal_world(specs, retrieved=PULL)
    plan = _plan(db)
    assert plan.blocked is True
    fenced = {d.player for d in plan.drop_board if d.undroppable}
    assert "IR Guy" in fenced
    for note in plan.notes:
        if "DROP" in note.upper() and "UNDROPPABLE" not in note.upper():
            assert not any(name in note for name in fenced), \
                f"a note names a drop ESPN refuses: {note!r}"
    assert any("UNDROPPABLE list, so dropping HIM is not an option" in n
               for n in plan.notes)

    # CONTROL: a droppable occupant still gets the option — the fence must not
    # over-block, and NULL ("not captured") is never a refusal.
    db.execute("DELETE FROM league_player_state")
    db.execute("DELETE FROM projections")
    db.execute("DELETE FROM players")
    db.commit()
    marginal_world([dict(s) for s in _active_specs()] + [_ir_spec("QUESTIONABLE")]
                   + _POOL_SPECS, retrieved=PULL)
    plan2 = _plan(db)
    assert any("you may instead DROP IR Guy himself" in n for n in plan2.notes)


def test_a_cap_blocked_leftover_names_the_LEAGUE_limit_when_that_fence_bound():
    """The LEAGUE branch of ``describe_cap`` never reached rendered text in any
    test: its only coverage was a pure unit call, and the one test that exercised
    the wiring ended in an ``or`` whose right disjunct is satisfied by the
    ``effective_position_caps`` NOTE — which the classifier does not produce — so
    replacing the interpolation with the module-guard literal left the suite green.

    Asserts on the ``ChainRejection`` object rather than the page, so no other
    sentence can satisfy it by accident.
    """
    class _LeagueCapped(_FakeBoard):
        position_caps = dict(waiver.POSITION_CAPS, QB=2)
        league_limits = {"QB": 2}

    one = _fs("One Thrower", 10.0, "Sierra Rusher", add_id="1", drop_id="s", add_pos="QB")
    two = _fs("Two Thrower", 9.0, "Tango Rusher", add_id="2", drop_id="t", add_pos="QB")
    V = {frozenset(): 0.0, frozenset({"One Thrower"}): 10.0,
         frozenset({"Two Thrower"}): 9.0,
         frozenset({"One Thrower", "Two Thrower"}): 19.0}
    chain = waiver._select_claims(
        [one, two], board=_LeagueCapped(V), claim_budget=10, waiver_rank=None,
        team_count=None, open_slots=0, candidate_notes={}, dup_names=set(),
        position_counts={"QB": 1, "RB": 5},
    )
    assert [r.add for r in chain.chain_capped] == ["Two Thrower"]
    reason = chain.chain_capped[0].reason
    assert "the binding limit is 2" in reason
    assert "your LEAGUE's own roster limit (ESPN positionLimits)" in reason
    assert "modelling guard" not in reason
    # ...and the page says TWO fences applied, because they really were both read
    notes = " ".join(waiver._chain_notes(chain, claim_budget=10, weeks=15))
    assert "TWO fences apply" in notes
    assert "Only ONE fence applied" not in notes


def test_faab_being_on_changes_what_a_claim_line_says_it_costs(db, marginal_world):
    """"it is free and non-FAAB" was a hard-coded literal in every WAIVERS claim
    reason — a league rule asserted from a string, in a recommendation. If a
    commissioner flips FAAB on, `settings_verdicts` holds the correct verdict in a
    command the weekly cadence never runs, while the claim line keeps telling the
    operator submitting costs nothing.

    CATCHES: an unconditional cost sentence.
    """
    swap = _fs("Free Catcher", 5.0, "Fourth Catcher", add_id="9", drop_id="4",
               add_pos="WR", status="WAIVERS")
    free = waiver._claim_reasons(
        swap, kind=waiver.KIND_WAIVER, waiver_rank=None, team_count=None,
        is_pure_add=False, candidate_notes=(), faab=0)
    assert any("free and non-FAAB" in r for r in free)

    paid = waiver._claim_reasons(
        swap, kind=waiver.KIND_WAIVER, waiver_rank=None, team_count=None,
        is_pure_add=False, candidate_notes=(), faab=1)
    assert any("FAAB IS ON" in r and "COSTS BID DOLLARS" in r for r in paid)
    assert not any("free and non-FAAB" in r for r in paid)

    unknown = waiver._claim_reasons(
        swap, kind=waiver.KIND_WAIVER, waiver_rank=None, team_count=None,
        is_pure_add=False, candidate_notes=(), faab=None)
    assert any("NOT CAPTURED" in r and "UNKNOWN" in r for r in unknown)


def test_a_changed_league_roster_shape_is_disclosed_rather_than_priced_through():
    """Migration 014's stated purpose is that a mid-season settings change stops
    being invisible to every module that prices a claim. ``positionLimits`` was
    duly wired; ``lineupSlotCounts`` was stored, printed, and consumed by nothing,
    so ``check_legality`` keeps blocking (or permitting) on 16 active / 1 IR while
    `ziggurat league settings` prints the new shape — two commands, one database,
    silently contradicting each other on the check that decides whether ESPN will
    process ANY transaction.

    Reconciled by DISCLOSURE, not by deriving the structure: ``RosterStructure``
    also drives replacement levels and the weekly seater.
    """
    from ziggurat.core.valuation import DEFAULT_ROSTER
    same = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1,
            "BE": 7, "IR": 1}
    assert waiver._roster_shape_mismatch({"lineup_slot_counts": same},
                                         DEFAULT_ROSTER) is None
    grown = {**same, "BE": 8, "IR": 2}
    note = waiver._roster_shape_mismatch({"lineup_slot_counts": grown}, DEFAULT_ROSTER)
    assert note and "17 active + 2 IR" in note and "16 active + 1 IR" in note
    # nothing to say when the settings row was never captured
    assert waiver._roster_shape_mismatch(None, DEFAULT_ROSTER) is None
