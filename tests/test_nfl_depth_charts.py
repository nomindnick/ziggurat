"""Depth charts v2 — the dated daily panel (2025+), item 3.2c.

THE HIGHEST-VALUE TEST HERE IS THE RECONSTRUCTION ORACLE
(``test_oracle_every_published_panel_reconstructs_exactly``). It replays the
fixture's ``dt`` through the ingester and then asserts, panel by panel, that the
accessor returns the published chart ROW FOR ROW. That single test fails on any
tombstone bug, any resolution-order bug and any watermark bug — which is what the
change-log encoding's whole claim rests on.

WHAT THESE FIXTURES CAN AND CANNOT CATCH (item 3.1b's lesson: three ingesters
were already broken against live upstream while the suite was green, because the
committed fixtures were frozen 2023 frames and ``require_columns`` never fired).

TWO fixtures, both REAL slices of the live files, each carrying phenomena that
upstream actually published:

* ``depth_chart_panel.parquet`` — 4,924 rows, 8 teams x the 9 consecutive ``dt``
  of 2025-09-13 .. 2025-09-21. Contains the Joe Burrow QB1 -> QB3 demotion at
  ``dt=2025-09-17T07:14:22Z``, 16 slot vacancies (the tombstone population), and
  342 dual-listed player-rows (the phantom-demotion trap the diff key exists
  for). Its 8 clubs are HEALTHY throughout — min per-club panel-to-panel ratio
  0.97 — which is why it cannot exercise the collapse floor and why the second
  fixture exists.
* ``depth_chart_collapse.parquet`` — 2,253 rows, 4 clubs x the 6 consecutive
  ``dt`` of 2026-07-20 .. 2026-07-25. Contains **two real partial scrapes**
  (IND 07-22, 99 slots -> 28; ARI 07-24, 100 -> 42 — the one that happened the
  day before this fixture was captured), one club that is healthy throughout
  (KC, the control), two REAL slot vacancies on healthy days (NE), and one REAL
  payload-only restatement (NE SLB4 espn_id 4613029: ``player_name`` NULL ->
  "Riley Wilson" at 07-24, occupant unchanged). Every panel in it carries all
  4 clubs, which is the point: a club-count floor sees nothing here.

CANNOT: both are frozen on 2026-07-25. They cannot prove nflverse still ships
``dt``, ``espn_id`` or ``pos_grp_id``, still numbers position groups 15/16/18/21,
or still publishes daily; and — specific to the collapse floor —
``PANEL_COLLAPSE_RATIO``'s 0.50 sits in a gap measured on the WHOLE 2025+2026
files (worst defective ratio 0.495, best legitimate 0.563), which is a property
of live upstream that no committed slice can re-derive. Four defences carry that
weight, in this order: (1) the cadence itself — this source pulls DAILY,
year-round, so live drift surfaces within a day (the 3.1b rot happened to sources
nothing ever called); (2) ``test_live_upstream_contract`` below, opt-in via
``ZIGGURAT_LIVE_TESTS=1``, which asserts the column set, the ``espn_id``
non-null invariant, slot-key uniqueness, 32 teams and the ``pos_grp_id`` domain
against the real file; (3) ``base.require_columns`` failing the run loudly rather
than storing partials; (4) re-running the module docstring's measurements when
the floor next looks wrong — they are recorded there with their dates for exactly
that reason.

Player names below are real NFL players — public data. Rule 5's red line is
league members, not the NFL.
"""

import os
from datetime import date

import pandas as pd
import pytest

from ziggurat.data.nfl import base, depth_charts

FIXTURE_DTS = [
    "2025-09-13T07:12:47Z", "2025-09-14T07:13:24Z", "2025-09-15T07:15:22Z",
    "2025-09-16T07:15:23Z", "2025-09-17T07:14:22Z", "2025-09-18T07:14:29Z",
    "2025-09-19T07:14:12Z", "2025-09-20T07:13:32Z", "2025-09-21T07:13:06Z",
]
BURROW = "3915511"
BROWNING = "3886812"
#: Greg Dortch, ARI — listed Special-Teams PR1, Special-Teams KR1 AND 3WR-1TE WR4
#: on the same panel. The measured shape behind the diff's composite key.
DORTCH = "4037235"
#: Grant Stuard, DET — listed Special-Teams KR1 AND a base-4-3 linebacker. Over
#: the fixture window he moves WLB2 -> SLB2 and does not move at KR. Keyed on the
#: player alone the diff reports him "SLB demoted 1 -> 2", where the 1 is his
#: KICK RETURN rank: a different listing entirely. See the diff-key test.
STUARD = "4240255"

#: The second fixture — six real 2026 panels carrying two real partial scrapes.
COLLAPSE_DTS = [
    "2026-07-20T09:54:02Z", "2026-07-21T09:26:01Z", "2026-07-22T09:23:35Z",
    "2026-07-23T09:22:29Z", "2026-07-24T09:16:54Z", "2026-07-25T08:57:25Z",
]
#: (club, the dt its chart collapsed, slots before -> slots in the bad panel).
COLLAPSES = [("IND", COLLAPSE_DTS[2], 99, 28), ("ARI", COLLAPSE_DTS[4], 100, 42)]
#: NE SLB4. A REAL payload-only restatement: the occupant never changes, the
#: name arrives on 07-24. Storage of that event is what C10 found untested.
RILEY_WILSON = "4613029"
SKILL = ["QB", "RB", "WR", "TE"]

PAYLOAD = ("pos_abb", "pos_grp", "pos_slot", "espn_id", "gsis_id", "player_name")


def _panel(nfl_fixture):
    return nfl_fixture("depth_chart_panel")


def _collapse(nfl_fixture):
    return nfl_fixture("depth_chart_collapse")


def _ingest_collapse(db, nfl_fixture, **kw):
    return depth_charts.ingest_depth_charts(
        db, _collapse(nfl_fixture), season=2026, retrieved_as_of="2026-07-20", **kw)


def _nan_to_none(value):
    """Upstream serves a missing string as NaN; the table stores NULL."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    return value


def _published(df, dt):
    """{slot key: payload} for one published panel, straight from the frame."""
    rows = df[df.dt == dt]
    return {
        (r.team, r.pos_grp_id, r.pos_id, int(r.pos_rank)): (
            _nan_to_none(r.pos_abb), _nan_to_none(r.pos_grp), int(r.pos_slot),
            _nan_to_none(r.espn_id), _nan_to_none(r.gsis_id), _nan_to_none(r.player_name),
        )
        for r in rows.itertuples(index=False)
    }


def _stored(rows):
    return {
        (r["team"], r["pos_grp_id"], r["pos_id"], r["pos_rank"]):
            tuple(_nan_to_none(r[c]) for c in PAYLOAD)
        for r in rows
    }


def _ingest(db, df, *, retrieved_as_of="2025-09-13", season=2025, since=None):
    return depth_charts.ingest_depth_charts(
        db, df, season=season, retrieved_as_of=retrieved_as_of, since=since
    )


# ===========================================================================
# the oracle
# ===========================================================================


def test_oracle_every_completely_published_panel_reconstructs_exactly(db, nfl_fixture):
    """Change log + tombstones is LOSSLESS: every COMPLETE panel comes back
    row-for-row.

    Reproduces, at fixture scale, the measurement the whole encoding rests on.
    RE-SCOPED with the collapse floor: against the live files this assertion is
    **338/348** panels, 0 mismatches — not the 348/348 it was before, because the
    10 partial-scrape panel-days are deliberately not reproduced (a club's
    absence from a broken scrape is not a vacancy). The old number was lossless
    about a lie. The deviation on those 10 days was measured column by column and
    is one-directional: 807 extra carried-forward rows, 0 missing rows, 0
    differing payloads — see the degraded-panel oracle below, which asserts
    exactly that shape.

    All 9 panels of THIS fixture are complete (min per-club ratio 0.97), so 9/9
    here.
    """
    df = _panel(nfl_fixture)
    _ingest(db, df)

    for dt in FIXTURE_DTS:
        rows = depth_charts.get_depth_chart(db, as_of=dt[:10], season=2025)
        assert _stored(rows) == _published(df, dt), f"panel {dt} did not reconstruct"


def test_oracle_a_degraded_panel_deviates_only_by_carrying_a_club_forward(db, nfl_fixture):
    """The other half of the re-scoped oracle: what the 10 exceptions look like.

    An unbounded "these panels are allowed to differ" escape hatch would let any
    encoding bug hide behind the floor. So the deviation is pinned in BOTH
    directions, on the two real partial scrapes: on a degraded panel-day the read
    may return rows the panel did not publish (that club's previous listing,
    carried forward) and NOTHING ELSE — no row published in the panel may be
    missing, and no payload may differ. That is the measured live shape too
    (807 extra / 0 missing / 0 differing across the 10 days).
    """
    df = _collapse(nfl_fixture)
    _ingest_collapse(db, nfl_fixture)

    for dt in COLLAPSE_DTS:
        published = _published(df, dt)
        stored = _stored(depth_charts.get_depth_chart(db, as_of=dt[:10], season=2026))
        degraded = {team for team, bad_dt, _, _ in COLLAPSES if bad_dt == dt}
        assert not set(published) - set(stored), f"panel {dt} lost a published slot"
        assert all(stored[k] == v for k, v in published.items()), \
            f"panel {dt} altered a published payload"
        extra = set(stored) - set(published)
        assert {k[0] for k in extra} == degraded, \
            f"panel {dt}: carried-forward clubs {sorted({k[0] for k in extra})} != {degraded}"
        if not degraded:
            assert stored == published


def test_the_change_log_is_far_smaller_than_the_panel_it_encodes(db, nfl_fixture):
    """The reason for the encoding, asserted rather than asserted-in-prose."""
    df = _panel(nfl_fixture)
    _ingest(db, df)
    stored = db.execute("SELECT COUNT(*) AS n FROM depth_chart_slots").fetchone()["n"]
    assert stored == 651                      # 635 changes + 16 tombstones
    assert stored < len(df) / 7               # 4,924 published rows
    assert db.execute(
        "SELECT COUNT(*) AS n FROM depth_chart_slots WHERE espn_id IS NULL"
    ).fetchone()["n"] == 16


# ===========================================================================
# tombstones — the load-bearing part
# ===========================================================================


def test_a_vacated_slot_is_tombstoned_and_stops_being_returned(db, nfl_fixture):
    """Both directions: gone after the vacancy, present before it.

    This is the assertion that fails if tombstone emission is dropped — the
    phantom-QB4 bug, where per-key resolution carries a retired occupant forward
    for weeks because a vacated slot has no newer row of its own.
    """
    df = _panel(nfl_fixture)
    _ingest(db, df)

    vacated = []
    for earlier, later in zip(FIXTURE_DTS, FIXTURE_DTS[1:], strict=False):
        gone = set(_published(df, earlier)) - set(_published(df, later))
        vacated.extend((later, key) for key in gone)
    assert len(vacated) == 16, "fixture must contain the measured 16 vacancies"

    for dt, key in vacated:
        team, grp, pos, rank = key
        before = _stored(depth_charts.get_depth_chart(
            db, as_of=FIXTURE_DTS[FIXTURE_DTS.index(dt) - 1][:10], season=2025, team=team))
        after = _stored(depth_charts.get_depth_chart(db, as_of=dt[:10], season=2025, team=team))
        assert key in before, f"{key} should be occupied before {dt}"
        assert key not in after, f"{key} was vacated at {dt} and must not be returned"


def test_deleting_the_tombstones_resurrects_the_ghost(db, nfl_fixture):
    """The tombstones are LOAD-BEARING, not tidiness — measured by removing them.

    Without this row the accessor is not what protects us; nothing is. Same
    lesson item 3.1 paid for with ``on_team_id IS NULL``.
    """
    df = _panel(nfl_fixture)
    _ingest(db, df)
    last = FIXTURE_DTS[-1][:10]
    honest = len(depth_charts.get_depth_chart(db, as_of=last, season=2025))

    db.execute("DELETE FROM depth_chart_slots WHERE espn_id IS NULL")
    db.commit()
    haunted = depth_charts.get_depth_chart(db, as_of=last, season=2025)
    # 9, not 16: seven of the sixteen vacated slots were re-occupied later in the
    # window, so a real event is their newest row either way. The other nine have
    # nothing after the tombstone — those are the ghosts, and on the real 2025
    # panel the same mechanism carried a phantom rank-4 forward for seven weeks.
    assert len(haunted) == honest + 9
    assert _stored(haunted) != _published(df, FIXTURE_DTS[-1])


# ===========================================================================
# the collapse floor — a partial scrape is not a vacancy (the C1 class)
# ===========================================================================


def test_a_partially_scraped_club_is_never_reported_as_vacated(db, nfl_fixture):
    """THE test this floor exists for, on two real partial scrapes.

    Without the floor this fixture writes 129 tombstones — 71 for IND on 07-22
    and 58 for ARI on 07-24 — the run log says ``ok``, and both clubs read ZERO
    skill-position players for a day before "returning" the next morning. On the
    live files it is 12 club-panels in 348, the worst of them turning the LAC
    2025-12-18 scrape into 91 fabricated "slot vacated" facts.

    Both directions, because a floor that suppresses everything is not a fix: the
    two REAL vacancies this fixture carries (NE, on healthy days) must still be
    tombstoned.
    """
    _ingest_collapse(db, nfl_fixture)

    for team, bad_dt, _before, _after in COLLAPSES:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM depth_chart_slots "
            "WHERE espn_id IS NULL AND observed_at = ? AND team = ?", (bad_dt, team)
        ).fetchone()["n"] == 0, f"{team} was tombstoned by a partial scrape at {bad_dt}"
        # ...and the club reads normally straight through the bad day.
        skill = [len(depth_charts.get_depth_chart(db, as_of=dt[:10], season=2026,
                                                  team=team, positions=SKILL))
                 for dt in COLLAPSE_DTS[1:]]
        assert len(set(skill)) == 1 and skill[0] > 0, f"{team} skill count moved: {skill}"

    # The healthy control club is untouched by any of this.
    kc = [len(depth_charts.get_depth_chart(db, as_of=dt[:10], season=2026, team="KC"))
          for dt in COLLAPSE_DTS]
    assert len(set(kc)) == 1

    # REAL vacancies still land. Suppressing these would be the ghost bug.
    real = db.execute(
        "SELECT observed_at, team FROM depth_chart_slots WHERE espn_id IS NULL"
    ).fetchall()
    assert [(r["observed_at"], r["team"]) for r in real] == [
        (COLLAPSE_DTS[3], "NE"), (COLLAPSE_DTS[5], "NE")]


def test_the_collapse_is_invisible_to_a_club_count_floor(db, nfl_fixture):
    """Why the floor is per club and not ``n_teams`` — the cheap fix that fails.

    Measured on the live files: all 348 panels carry 32 clubs, so a club-count
    floor catches 0 of the 12 real collapses. This fixture reproduces that shape
    in miniature: every panel publishes all 4 clubs, including the two that are
    broken. A future edit that replaces the ratio with a club-count check has to
    make this test lie first.
    """
    df = _collapse(nfl_fixture)
    for dt in COLLAPSE_DTS:
        assert df[df.dt == dt].team.nunique() == 4, f"{dt} is missing a club entirely"
    for team, bad_dt, before, after in COLLAPSES:
        assert len(df[(df.dt == bad_dt) & (df.team == team)]) == after
        assert after / before < depth_charts.PANEL_COLLAPSE_RATIO


def test_a_partial_panel_is_flagged_and_carries_a_novice_legible_caveat(db, nfl_fixture):
    """Rule 6. After suppression the chart reads perfectly normally — that IS the
    repair — so without a surface nothing would tell the operator that some
    clubs' listings on this date were never confirmed by it.

    ``n_teams`` is the count of clubs the panel is AUTHORITATIVE for, which is
    what migration 007's own column comment already declares ("< 32 is a partial
    scrape") and what nothing read until now.
    """
    _ingest_collapse(db, nfl_fixture)
    panels = {r["observed_at"]: r["n_teams"] for r in db.execute(
        "SELECT observed_at, n_teams FROM depth_chart_panels")}
    assert panels == {COLLAPSE_DTS[0]: 4, COLLAPSE_DTS[1]: 4, COLLAPSE_DTS[2]: 3,
                      COLLAPSE_DTS[3]: 4, COLLAPSE_DTS[4]: 3, COLLAPSE_DTS[5]: 4}

    for dt in COLLAPSE_DTS:
        caveat = depth_charts.panel_completeness_caveat(db, as_of=dt[:10], season=2026)
        degraded = any(bad_dt == dt for _, bad_dt, _, _ in COLLAPSES)
        if not degraded:
            assert caveat is None, f"{dt} is complete and must print no caveat"
            continue
        assert caveat is not None and "PARTIAL" in caveat
        assert "3 of 4 clubs" in caveat          # data-derived, never a hard-coded 32
        assert "CARRIED FORWARD" in caveat
        assert dt in caveat
        # ...and the panel row itself is still served, so a reader gets both.
        assert depth_charts.get_depth_chart_observed(
            db, as_of=dt[:10], season=2026)["observed_at"] == dt


def test_the_completeness_caveat_is_as_of_gated_like_every_other_read(db, nfl_fixture):
    """Rule 1 on the new accessor. A backfilled panel (``retrieved_as_of`` today,
    facts months old) is invisible under ``historical`` and visible under
    ``latest_truth`` — and ``latest_truth`` still gates the FACT time, so the
    caveat cannot be reported before the partial panel was published."""
    df = _collapse(nfl_fixture)
    depth_charts.ingest_depth_charts(db, df, season=2026, retrieved_as_of="2026-08-01")
    bad = COLLAPSE_DTS[4][:10]
    assert depth_charts.panel_completeness_caveat(db, as_of=bad, season=2026) is None
    truth = base.latest_truth(depth_charts.panel_completeness_caveat)
    assert "PARTIAL" in truth(db, as_of=bad, season=2026)
    assert truth(db, as_of="2026-07-19", season=2026) is None

    # ...and the "how many clubs is a full panel" baseline is retrieval-gated
    # TOO, which is the half a single-stamp fixture cannot show. Here the first
    # three panels were retrieved on 07-22 knowing only three clubs; the
    # four-club file arrived later. Read as of 07-22, "a full panel" must mean
    # the 3 clubs that were knowable THEN — reading 4 is later knowledge.
    from ziggurat.data.store import apply_schema, connect
    mixed = connect(":memory:")
    apply_schema(mixed)
    early = df[df.dt.isin(COLLAPSE_DTS[:3]) & (df.team != "NE")]
    depth_charts.ingest_depth_charts(mixed, early, season=2026,
                                     retrieved_as_of="2026-07-22")
    depth_charts.ingest_depth_charts(mixed, df, season=2026, retrieved_as_of="2026-07-26")
    caveat = depth_charts.panel_completeness_caveat(mixed, as_of="2026-07-22", season=2026)
    assert caveat is not None and "2 of 3 clubs" in caveat, caveat
    # Ungate the baseline and this reads "2 of 4" — the 4 is knowledge that did
    # not exist on 07-22. `latest_truth` is where "4" is the right answer, and
    # there the panel row itself is the later 4-club version, so it reads 3 of 4.
    assert "3 of 4 clubs" in base.latest_truth(depth_charts.panel_completeness_caveat)(
        mixed, as_of="2026-07-22", season=2026)
    mixed.close()


def test_a_partial_scrape_does_not_fabricate_a_qb1_change(db, nfl_fixture):
    """The downstream shape of the same fabrication, and the reason it is a
    CRITICAL rather than a storage nit.

    Unfixed, ARI's QB1 slot is tombstoned on 07-24, so the 07-24 -> 07-25 window
    reads as a brand-new QB1 with no predecessor and
    ``qb1_change_candidates`` announces "Jacoby Brissett is now listed QB1 for
    ARI" with ``previous_player_name=None``: a confident, well-formed, novice-
    legible, entirely fabricated fact — this project's signature failure class
    (Rule 6). ARI's QB room did not change once in these six days.
    """
    _ingest_collapse(db, nfl_fixture)
    for since, as_of in zip(COLLAPSE_DTS, COLLAPSE_DTS[1:], strict=False):
        assert depth_charts.qb1_change_candidates(
            db, since=since[:10], as_of=as_of[:10], season=2026) == [], \
            f"a QB1 change was reported across {since} -> {as_of}"
    # ...and the diff does not report the club's whole chart as removed/added.
    for team, bad_dt, _, _ in COLLAPSES:
        i = COLLAPSE_DTS.index(bad_dt)
        moves = depth_charts.depth_chart_diff(
            db, since=COLLAPSE_DTS[i - 1][:10], as_of=bad_dt[:10], season=2026,
            team=team, positions=SKILL)
        assert moves == [], f"{team} skill listings moved on its partial-scrape day"


def test_a_window_with_no_stored_baseline_refuses_instead_of_inventing_a_change(db, nfl_fixture):
    """"Nothing was listed" and "we never saw that day" are the SAME read: zero rows.

    This repo has paid for that shape twice. Item 3.2: the projection feed's bye
    row and its "no forecast" row are byte-identical, so a point-sum could not
    tell "worth nothing" from "we do not know", and a 99.3%-owned player topped
    the drop board with a confident number and no disclosure anywhere. Item 3.1:
    a dropped player and a stale holder, same fix — the absence had to become a
    positive fact before anything could reason about it.

    Here an empty before-state makes EVERY listing read as ``added`` and EVERY
    club as a brand-new QB1. Measured on the real post-backfill database under
    the DEFAULT view: 32 clubs, every one ``previous_player_name=None``, and
    3,176 diff rows all ``added``. ``depth_chart_panels`` is the table that can
    tell the two apart, so both accessors ask it before answering.

    They RAISE rather than return ``[]``: an empty list reads as "nothing
    changed", which is the same lie pointing the other way. Raising is safe HERE
    because this is a read path — unlike the ingest path, where refusing a
    partial panel would re-refuse on every pull and brick the source for good.
    """
    _ingest_collapse(db, nfl_fixture)
    unseen, latest = "2026-01-01", COLLAPSE_DTS[-1][:10]

    # The premise: the two states really are indistinguishable to a naive reader.
    assert depth_charts.get_depth_chart(db, as_of=unseen, season=2026) == []
    assert depth_charts.get_depth_chart_observed(db, as_of=unseen, season=2026) is None
    assert len(depth_charts.get_depth_chart(db, as_of=latest, season=2026)) > 0

    for name, call in (
        ("depth_chart_diff",
         lambda: depth_charts.depth_chart_diff(db, since=unseen, as_of=latest, season=2026)),
        ("qb1_change_candidates",
         lambda: depth_charts.qb1_change_candidates(db, since=unseen, as_of=latest, season=2026)),
    ):
        with pytest.raises(depth_charts.NoBaselinePanel, match="no baseline to compare"):
            call()
        assert name in str(pytest.raises(depth_charts.NoBaselinePanel, call).value), \
            "the error must name the accessor that refused (Rule 6)"


def test_the_baseline_guard_is_silent_on_a_day_the_archive_does_hold(db, nfl_fixture):
    """The other side of the guard: it must cost the ordinary path nothing.

    A guard that fires on legitimate windows would be worse than the fabrication
    it prevents, because 3.3 would learn to route around it.
    """
    _ingest_collapse(db, nfl_fixture)
    first, latest = COLLAPSE_DTS[0][:10], COLLAPSE_DTS[-1][:10]
    assert isinstance(
        depth_charts.depth_chart_diff(db, since=first, as_of=latest, season=2026), list)
    assert isinstance(
        depth_charts.qb1_change_candidates(db, since=first, as_of=latest, season=2026), list)
    # ...and a same-day window, where before and after are the one panel.
    assert depth_charts.depth_chart_diff(db, since=latest, as_of=latest, season=2026) == []


def test_qb1_change_candidates_refuses_a_backwards_window(db, nfl_fixture):
    """``depth_chart_diff`` already refused this; its sibling silently returned 0.

    Same argument pair, same meaning, two different behaviours — and the silent
    one answers "no QB1 changed", which a novice reads as reassurance.
    """
    _ingest_collapse(db, nfl_fixture)
    with pytest.raises(ValueError, match="must be on or before"):
        depth_charts.qb1_change_candidates(
            db, since=COLLAPSE_DTS[-1][:10], as_of=COLLAPSE_DTS[0][:10], season=2026)


def test_a_legitimate_shrinkage_above_the_floor_still_tombstones(db, nfl_fixture):
    """The other side of the threshold — the direction that makes it a threshold.

    The floor sits in a measured gap: across 2025+2026 the 12 defective ratios
    top out at **0.495**, the lowest legitimate ratio anywhere is **0.563** (LV
    2026-04-19, 71 -> 40) and the annual roster-cutdown day bottoms out at
    **0.656** (JAX 2025-08-27). A club that legitimately sheds a third of its
    chart is still a club whose vacancies are real, so it must still tombstone.
    """
    df = _collapse(nfl_fixture).copy().reset_index(drop=True)
    bad = COLLAPSE_DTS[3]
    victim = df.index[(df.dt == bad) & (df.team == "KC")]
    cut = victim[:int(len(victim) * 0.40)]          # 98 -> 59 slots, ratio 0.602
    trimmed = df.drop(index=cut)
    assert 0.55 < (len(victim) - len(cut)) / len(victim) < 0.65

    depth_charts.ingest_depth_charts(db, trimmed, season=2026,
                                     retrieved_as_of="2026-07-20")
    assert db.execute(
        "SELECT COUNT(*) AS n FROM depth_chart_slots "
        "WHERE espn_id IS NULL AND observed_at = ? AND team = 'KC'", (bad,)
    ).fetchone()["n"] == len(cut)
    assert db.execute(
        "SELECT n_teams AS n FROM depth_chart_panels WHERE observed_at = ?", (bad,)
    ).fetchone()["n"] == 4, "a legitimate shrinkage does not cost the club its vote"


def test_the_partial_scrape_is_reported_once_and_not_re_reported_for_ever(
        db, nfl_fixture, caplog):
    """``note_incomplete`` on the pull that STORES the panel — and silence after.

    The whole file is re-diffed on every pull, so a report keyed on the file
    rather than on what is being written would re-warn about all 12 historical
    collapses every single morning. A guard that fires identically for ever is
    the wolf-cry this module's other guards (``_check_restatement``) are
    explicitly written to avoid, and M3 is the finding that it is not
    theoretical.
    """
    with base.collect_drops() as tally:
        _ingest_collapse(db, nfl_fixture)
    assert tally["incomplete"] == 71 + 58        # the tombstones REFUSED
    assert tally["dropped"] == 0                 # nothing was dropped; this is not a loss
    assert "PARTIAL SCRAPE: IND" in caplog.text and "PARTIAL SCRAPE: ARI" in caplog.text

    caplog.clear()
    with base.collect_drops() as again:
        written = _ingest_collapse(
            db, nfl_fixture, since=depth_charts.latest_observed_at(db, season=2026))
    assert written == 0
    assert again["incomplete"] == 0, "a settled collapse must not re-warn every day"
    assert "PARTIAL SCRAPE" not in caplog.text


def test_incremental_and_whole_file_ingest_agree_across_a_partial_scrape(db, nfl_fixture):
    """The floor must not fork the daily path from the backfill path.

    Suppression is a function of the WHOLE file, exactly like the rest of the
    diff, so replaying one ``dt`` at a time must reach a byte-identical table —
    including on the two days a club collapsed and the day it came back.
    """
    df = _collapse(nfl_fixture)
    _ingest_collapse(db, nfl_fixture)
    full = _dump(db)

    from ziggurat.data.store import apply_schema, connect
    inc = connect(":memory:")
    apply_schema(inc)
    for i in range(len(COLLAPSE_DTS)):
        depth_charts.ingest_depth_charts(
            inc, df[df.dt.isin(COLLAPSE_DTS[:i + 1])], season=2026,
            retrieved_as_of="2026-07-20",
            since=depth_charts.latest_observed_at(inc, season=2026))
    assert _dump(inc) == full
    inc.close()


def test_a_real_vacancy_hidden_by_a_partial_scrape_is_still_recorded_afterwards(
        db, nfl_fixture):
    """Suppressing the tombstone must also preserve the STATE it came from.

    Half a fix is available here and it is silent: skip the tombstone but let the
    club's slots go to "vacant" in the diff's running state anyway. Every
    assertion above still passes — the bad day reads correctly, because a
    suppressed tombstone is simply not on disk — and the damage only appears when
    a vacancy that was real all along becomes visible on the recovery day: the
    slot's state is already ``None``, "a vacancy is recorded once" skips it, and
    the retired occupant is carried forward FOR EVER. That is the phantom-rank-4
    ghost the tombstones exist to prevent, re-entering through the fix for C1.
    """
    df = _collapse(nfl_fixture).copy().reset_index(drop=True)
    bad, recovery = COLLAPSE_DTS[4], COLLAPSE_DTS[5]
    gone = df.index[(df.dt == recovery) & (df.team == "ARI") & (df.pos_abb == "WR")
                    & (df.pos_rank == 5)]
    assert len(gone) == 1, "fixture must carry ARI WR5 on the recovery panel"
    slot = df.loc[gone[0]]
    assert not len(df[(df.dt == bad) & (df.team == "ARI") & (df.pos_abb == "WR")]), \
        "the partial panel published no WR at all — that is what makes this the case"

    depth_charts.ingest_depth_charts(db, df.drop(index=gone), season=2026,
                                     retrieved_as_of="2026-07-20")
    assert db.execute(
        "SELECT COUNT(*) AS n FROM depth_chart_slots WHERE espn_id IS NULL "
        "AND team = 'ARI' AND observed_at = ?", (recovery,)).fetchone()["n"] == 1
    live = depth_charts.get_depth_chart(db, as_of=recovery[:10], season=2026, team="ARI")
    assert slot.espn_id not in {r["espn_id"] for r in live if r["pos_abb"] == "WR"}
    # ...and he WAS there right through the partial-scrape day.
    day_before = depth_charts.get_depth_chart(db, as_of=bad[:10], season=2026, team="ARI")
    assert slot.espn_id in {r["espn_id"] for r in day_before}


def test_a_second_bad_day_in_a_row_is_measured_against_the_last_GOOD_panel(db, nfl_fixture):
    """The baseline is the last panel the diff TRUSTED, not simply the previous
    one — otherwise the second bad day in a row compares 42 against 42, reads
    healthy, and tombstones the club after all. Unobserved upstream (all 12
    collapses recovered the next day), which is exactly why it is pinned: nobody
    would notice it breaking.
    """
    df = _collapse(nfl_fixture).copy()
    bad = COLLAPSE_DTS[4]                       # ARI, 100 -> 42
    encore = df[(df.dt == bad)].assign(dt="2026-07-24T19:01:03Z")
    depth_charts.ingest_depth_charts(db, pd.concat([df[df.dt <= bad], encore]),
                                     season=2026, retrieved_as_of="2026-07-20")

    assert db.execute(
        "SELECT COUNT(*) AS n FROM depth_chart_slots WHERE espn_id IS NULL AND team = 'ARI'"
    ).fetchone()["n"] == 0
    assert len(depth_charts.get_depth_chart(
        db, as_of="2026-07-24", season=2026, team="ARI", positions=SKILL)) == 29
    assert [r["n_teams"] for r in db.execute(
        "SELECT n_teams FROM depth_chart_panels ORDER BY observed_at")] == [4, 4, 3, 4, 3, 3]


def test_a_club_that_vanishes_entirely_is_suppressed_not_tombstoned(db, nfl_fixture):
    """The escalation the audit measured synthetically: one club missing from the
    panel altogether. Its ratio is 0, so the same floor catches it — where an
    ``n_teams`` check would have been the only thing that could, and does not
    exist. Unfixed this writes ~100 tombstones and reports ``ok``.
    """
    df = _collapse(nfl_fixture)
    bad = COLLAPSE_DTS[3]
    thinned = df[~((df.dt == bad) & (df.team == "KC"))]
    depth_charts.ingest_depth_charts(db, thinned, season=2026, retrieved_as_of="2026-07-20")

    assert db.execute(
        "SELECT COUNT(*) AS n FROM depth_chart_slots "
        "WHERE espn_id IS NULL AND observed_at = ? AND team = 'KC'", (bad,)
    ).fetchone()["n"] == 0
    assert len(depth_charts.get_depth_chart(db, as_of=bad[:10], season=2026, team="KC")) == 98
    row = db.execute("SELECT n_teams AS n, n_slots AS s FROM depth_chart_panels "
                     "WHERE observed_at = ?", (bad,)).fetchone()
    assert (row["n"], row["s"]) == (3, 398 - 98)


# ===========================================================================
# resolution: which observation, and which version of it
# ===========================================================================


def test_exactly_one_row_per_slot_and_the_count_matches_the_panel(db, nfl_fixture):
    """The assertion a ``select_as_of``-shaped implementation fails.

    ``select_as_of``'s correlated MAX is on ``retrieved_as_of`` only, so under one
    bulk retrieval stamp every version of every key ties and the read returns the
    whole history — measured on the real 2025 panel as 3,572 rows where 2,255 was
    right, a 58% inflated board showing one team both a QB3 and a QB4 who are the
    same player.

    NOTE, and it corrects the design note's §3.8 wording: the discriminating
    assertion is ONE ROW PER SLOT, not one ``observed_at`` per team. In a change
    log a stable slot keeps the instant its value was FIRST observed, so a
    correct read of a 9-day window legitimately mixes nine different
    ``observed_at`` — that is the compression working, not a bug.
    """
    df = _panel(nfl_fixture)
    _ingest(db, df)
    dt = FIXTURE_DTS[4]
    rows = depth_charts.get_depth_chart(db, as_of=dt[:10], season=2025)
    assert len(rows) == len(df[df.dt == dt])
    keys = [(r["team"], r["pos_grp_id"], r["pos_id"], r["pos_rank"]) for r in rows]
    assert len(set(keys)) == len(keys), "a slot resolved to more than one row"
    assert len({r["observed_at"] for r in rows}) > 1, (
        "a stable slot must keep its FIRST-observed instant; if every row carried "
        "the panel's dt the storage would be verbatim, not a change log"
    )
    assert max(r["observed_at"] for r in rows) == dt


def test_the_later_panel_wins_when_a_day_carries_two(db, nfl_fixture):
    """Four measured days carry 2-3 panels (2025-08-09, 2025-08-11, 2026-03-22).

    ``as_of`` is day-granular by ``select_as_of``'s documented contract, so
    ``observed_at`` is what orders them — which is why it is in the key.
    """
    df = _panel(nfl_fixture)
    early, late = FIXTURE_DTS[0], FIXTURE_DTS[1]
    same_day = pd.concat([
        df[df.dt == early],
        df[df.dt == late].assign(dt=early[:10] + "T19:01:03Z"),
    ])
    _ingest(db, same_day)
    rows = depth_charts.get_depth_chart(db, as_of=early[:10], season=2025)
    assert _stored(rows) == _published(df, late)
    assert max(r["observed_at"] for r in rows) == early[:10] + "T19:01:03Z"
    observed = depth_charts.get_depth_chart_observed(db, as_of=early[:10], season=2025)
    assert observed["observed_at"] == early[:10] + "T19:01:03Z"


def test_a_later_retrieval_of_the_same_observation_wins(db, nfl_fixture):
    """Stage 3: same instant, corrected later. The stored row is a new VERSION,
    never an overwrite — which is what makes ``latest_truth`` mean anything."""
    df = _panel(nfl_fixture)
    first = df[df.dt == FIXTURE_DTS[0]]
    _ingest(db, first, retrieved_as_of="2025-09-13")
    corrected = first.copy()
    corrected.loc[corrected.index, "player_name"] = corrected["player_name"] + " (corrected)"
    _ingest(db, corrected, retrieved_as_of="2025-09-14")

    rows = depth_charts.get_depth_chart(db, as_of="2025-09-14", season=2025)
    assert all(r["player_name"].endswith("(corrected)") for r in rows)
    # ...and the original version is still on disk, under its own stamp.
    stamps = {r["retrieved_as_of"] for r in
              db.execute("SELECT retrieved_as_of FROM depth_chart_slots")}
    assert stamps == {"2025-09-13", "2025-09-14"}


def test_a_payload_only_restatement_is_a_change_and_lands(db, nfl_fixture):
    """C10: the change log compares the whole PAYLOAD, not just the occupant.

    A real, unglamorous event: NE's SLB4 slot keeps the same player all week and
    upstream simply learns his name — ``player_name`` NULL -> "Riley Wilson" at
    2026-07-24, everything else identical. The live files carry 48 (2025) + 61
    (2026) of these, almost all name restatements (``Kam Curl`` -> ``Kamren
    Curl``).

    A ``_change_log`` that compared only ``espn_id`` passes every other test in
    this file and the whole suite: the fabricated-vacancy tests still hold, the
    counts still add up, and the only symptom is a correction that NEVER LANDS —
    the player stays nameless (or misspelled, or carrying a stale ``gsis_id``,
    which is what a downstream join keys on) for the rest of the season. Measured
    against an independent oracle, that mutant reconstructs 32 of 348 panels.
    """
    df = _collapse(nfl_fixture)
    before = df[(df.dt == COLLAPSE_DTS[3]) & (df.espn_id == RILEY_WILSON)]
    after = df[(df.dt == COLLAPSE_DTS[4]) & (df.espn_id == RILEY_WILSON)]
    assert len(before) == len(after) == 1
    assert pd.isna(before.iloc[0].player_name) and after.iloc[0].player_name == "Riley Wilson"
    assert (before.iloc[0].pos_abb, int(before.iloc[0].pos_rank)) == \
           (after.iloc[0].pos_abb, int(after.iloc[0].pos_rank)), "occupant and slot unchanged"

    _ingest_collapse(db, nfl_fixture)
    named = [r for r in depth_charts.get_depth_chart(db, as_of="2026-07-24", season=2026,
                                                     team="NE") if r["espn_id"] == RILEY_WILSON]
    assert [(r["player_name"], r["observed_at"]) for r in named] == \
           [("Riley Wilson", COLLAPSE_DTS[4])]
    # ...and it was genuinely a restatement: nameless the day before.
    nameless = [r for r in depth_charts.get_depth_chart(db, as_of="2026-07-23", season=2026,
                                                        team="NE") if r["espn_id"] == RILEY_WILSON]
    assert [r["player_name"] for r in nameless] == [None]
    # ...counted as a change by the panel row, not slipped in silently.
    assert db.execute("SELECT n_changes AS n FROM depth_chart_panels WHERE observed_at = ?",
                      (COLLAPSE_DTS[4],)).fetchone()["n"] >= 1


# ===========================================================================
# leakage (T1) and the backfill contract (T2)
# ===========================================================================


def test_leakage_hides_a_panel_that_had_not_been_published(db, nfl_fixture):
    """Retrieval is BEFORE the leakage as_of for every row, so only
    ``knowable_as_of`` can do the hiding — the crux, isolated."""
    df = _panel(nfl_fixture)
    _ingest(db, df, retrieved_as_of="2025-09-13")

    rows = depth_charts.get_depth_chart(db, as_of="2025-09-15", season=2025)
    assert _stored(rows) == _published(df, FIXTURE_DTS[2])
    assert all(r["knowable_as_of"] <= "2025-09-15" for r in rows)
    assert db.execute(
        "SELECT COUNT(*) AS n FROM depth_chart_slots WHERE knowable_as_of > '2025-09-15'"
    ).fetchone()["n"] > 0, "later panels must be PRESENT, just hidden"

    # Same rows, a later as_of: the future arrives, proving knowable_as_of — not
    # a missing row — did the gating.
    later = depth_charts.get_depth_chart(db, as_of="2025-09-21", season=2025)
    assert _stored(later) == _published(df, FIXTURE_DTS[-1])


def test_backfilled_history_is_invisible_under_historical_and_visible_under_latest_truth(
        db, nfl_fixture):
    """T2, the single highest-value test class in item 3.2c.

    A bulk backfill stamps ``retrieved_as_of = today`` on rows whose facts are
    months old. Under the default ``historical`` view those rows read EMPTY —
    correctly, and silently. The failure mode is an empty result that reads as
    "3.3 is broken" rather than "wrong view", so it is pinned as a contract.
    """
    df = _panel(nfl_fixture)
    _ingest(db, df, retrieved_as_of="2026-07-25")
    past = FIXTURE_DTS[4][:10]

    assert depth_charts.get_depth_chart(db, as_of=past, season=2025) == []
    truth = base.latest_truth(depth_charts.get_depth_chart)
    assert len(truth(db, as_of=past, season=2025)) == len(df[df.dt == FIXTURE_DTS[4]])
    # ...and latest_truth still gates the FACT time.
    assert truth(db, as_of="2025-09-12", season=2025) == []


def test_the_view_argument_is_validated_on_every_public_read(db, nfl_fixture):
    _ingest(db, _panel(nfl_fixture))
    truth = base.latest_truth(depth_charts.get_depth_chart)
    with pytest.raises(ValueError):
        truth(db, as_of="2025-09-15", season=2025, view="historical")
    for reader in (depth_charts.get_depth_chart, depth_charts.get_depth_chart_observed,
                   depth_charts.resolve_panel_season,
                   depth_charts.panel_completeness_caveat):
        with pytest.raises(ValueError):
            reader(db, as_of="2025-09-15", view="nonsense")


def test_every_accessor_requires_as_of(db, nfl_fixture):
    _ingest(db, _panel(nfl_fixture))
    with pytest.raises(TypeError):
        depth_charts.get_depth_chart(db, as_of=None, season=2025)
    with pytest.raises(TypeError):
        depth_charts.get_depth_chart(db, season=2025)


# ===========================================================================
# the panel table: a quiet day is still work done
# ===========================================================================


def test_a_panel_row_lands_for_every_observation_including_quiet_ones(db, nfl_fixture):
    """Measured: 8 of 2025's 221 panels and 20 of 2026's 127 carried ZERO slot
    changes. Without this row those pulls write 0 rows, ``refresh`` reads 0 rows
    as ``empty``, and a healthy source stands in a false alarm ~16% of days."""
    df = _panel(nfl_fixture)
    quiet = df[df.dt == FIXTURE_DTS[-1]].assign(dt="2025-09-22T07:13:00Z")
    written = _ingest(db, pd.concat([df, quiet]))

    panels = db.execute(
        "SELECT observed_at, n_teams, n_slots, n_changes FROM depth_chart_panels "
        "ORDER BY observed_at").fetchall()
    assert len(panels) == 10
    assert panels[-1]["n_changes"] == 0, "a re-published identical panel changes nothing"
    assert panels[-1]["n_slots"] == len(quiet)
    assert panels[-1]["n_teams"] == 8
    assert written == 651 + 10, "written counts BOTH tables"


def test_get_depth_chart_observed_reports_when_the_chart_was_published(db, nfl_fixture):
    """Rule 6: a recommendation says "chart observed <when>", never implies live."""
    _ingest(db, _panel(nfl_fixture))
    row = depth_charts.get_depth_chart_observed(db, as_of="2025-09-17", season=2025)
    assert row["observed_at"] == FIXTURE_DTS[4]
    assert row["n_teams"] == 8
    assert depth_charts.get_depth_chart_observed(db, as_of="2025-09-01", season=2025) is None


# ===========================================================================
# the watermark: the daily path must not re-store the season every morning
# ===========================================================================


def test_incremental_ingest_reaches_the_same_state_as_one_full_ingest(db, nfl_fixture):
    """One code path, two callers. Diffing the WHOLE file and filtering by the
    watermark is provably identical to the backfill's output — which is what
    stops the daily and backfill paths being two implementations that must
    agree."""
    df = _panel(nfl_fixture)
    _ingest(db, df)
    full = _dump(db)

    from ziggurat.data.store import apply_schema, connect
    inc = connect(":memory:")
    apply_schema(inc)
    depth_charts.ingest_depth_charts(inc, df[df.dt.isin(FIXTURE_DTS[:3])],
                                     season=2025, retrieved_as_of="2025-09-13")
    volumes = []
    for i in range(3, len(FIXTURE_DTS)):
        watermark = depth_charts.latest_observed_at(inc, season=2025)
        assert watermark == FIXTURE_DTS[i - 1]
        volumes.append(depth_charts.ingest_depth_charts(
            inc, df[df.dt.isin(FIXTURE_DTS[:i + 1])], season=2025,
            retrieved_as_of="2025-09-13", since=watermark))
    assert _dump(inc) == full
    # ...and each day wrote a handful of rows, not the whole season.
    assert max(volumes) < 40, volumes
    inc.close()


def test_a_same_day_re_ingest_writes_nothing_and_changes_nothing(db, nfl_fixture):
    """Idempotence. The case §7.3.4 calls out explicitly: a re-run must not turn
    a healthy source into a standing alarm or a duplicated table."""
    df = _panel(nfl_fixture)
    _ingest(db, df)
    before = _dump(db)
    written = _ingest(db, df, since=depth_charts.latest_observed_at(db, season=2025))
    assert written == 0
    assert _dump(db) == before


def test_the_watermark_comes_from_the_panel_table_not_the_slot_table(db, nfl_fixture):
    """A quiet panel contributes no slot row, so the slot table's MAX would
    rewind to the last day something moved and the ingester would re-diff every
    panel since."""
    df = _panel(nfl_fixture)
    quiet = df[df.dt == FIXTURE_DTS[-1]].assign(dt="2025-09-22T07:13:00Z")
    _ingest(db, pd.concat([df, quiet]))
    assert depth_charts.latest_observed_at(db, season=2025) == "2025-09-22T07:13:00Z"
    assert db.execute(
        "SELECT MAX(observed_at) AS m FROM depth_chart_slots").fetchone()["m"] == FIXTURE_DTS[-1]


def test_a_restated_past_panel_is_reported_not_swallowed(db, nfl_fixture, caplog):
    """Never observed upstream; "we would notice" is cheaper than "we assume"."""
    df = _panel(nfl_fixture)
    _ingest(db, df)
    watermark = depth_charts.latest_observed_at(db, season=2025)

    restated = df.copy()
    victim = restated.index[0]
    restated.loc[victim, "espn_id"] = "9999999"
    with base.collect_drops() as tally:
        _ingest(db, restated, since=watermark)
    assert tally["incomplete"] > 0
    assert "restated a past dt" in caplog.text


def _dump(conn):
    out = {}
    for table in ("depth_chart_slots", "depth_chart_panels"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        out[table] = sorted(tuple(r) for r in conn.execute(
            f"SELECT {', '.join(cols)} FROM {table}"))
    return out


# ===========================================================================
# ingest-time refusals
# ===========================================================================


def test_the_legacy_weekly_frame_is_refused_with_a_pointer(db, nfl_fixture):
    """F2: routing 2021-2024 here would have stored ~148k rows that read back as
    ZERO, because four of five key columns are absent and the occupant column is
    the tombstone sentinel."""
    with pytest.raises(depth_charts.LegacyDepthChartFrame) as exc:
        _ingest(db, nfl_fixture("depth_charts"))
    assert "depth_charts_weekly" in str(exc.value)


def test_a_pre_panel_season_is_refused(db, nfl_fixture):
    with pytest.raises(depth_charts.LegacyDepthChartFrame):
        _ingest(db, _panel(nfl_fixture), season=2024)


def test_a_missing_column_fails_loudly(db, nfl_fixture):
    df = _panel(nfl_fixture).drop(columns=["pos_grp_id"])
    with pytest.raises(ValueError) as exc:
        _ingest(db, df)
    assert "pos_grp_id" in str(exc.value)


def test_a_null_occupant_is_dropped_and_never_fabricates_a_vacancy(db, nfl_fixture):
    """``espn_id IS NULL`` MEANS tombstone, so a null occupant cannot be stored.

    Measured 0 nulls in 923,162 rows — this is a guard, not a populated path. The
    subtle half is that the slot must NOT be treated as vacant either: a row we
    cannot read means "unknown", and emitting a tombstone for a slot the panel
    actually published is a fabricated fact.
    """
    df = _panel(nfl_fixture).copy().reset_index(drop=True)
    target = df[(df.dt == FIXTURE_DTS[3]) & (df.espn_id == BURROW)].index[0]
    df.loc[target, "espn_id"] = None

    with base.collect_drops() as tally:
        _ingest(db, df)
    assert tally["dropped"] == 1

    slot = df.loc[target]
    key = (slot.team, slot.pos_grp_id, slot.pos_id, int(slot.pos_rank))
    tombstones = db.execute(
        "SELECT * FROM depth_chart_slots WHERE espn_id IS NULL AND observed_at = ?",
        (FIXTURE_DTS[3],)).fetchall()
    assert key not in {(r["team"], r["pos_grp_id"], r["pos_id"], r["pos_rank"])
                       for r in tombstones}
    # The previous observation carries forward: unknown, not vacated.
    rows = _stored(depth_charts.get_depth_chart(db, as_of=FIXTURE_DTS[3][:10], season=2025))
    assert rows[key][3] == BURROW


@pytest.mark.parametrize("bad_dt", [
    "2025-09-13T07:12:47+00:00",   # same instant, different width -> sorts wrong
    "2025-09-13 07:12:47",         # no T, no Z
    "2025-09-13",                  # day only
])
def test_a_reshaped_dt_fails_the_run_rather_than_corrupting_the_gate(db, nfl_fixture, bad_dt):
    """Two invariants ride on ``dt``'s exact shape and neither is obvious.

    ``observed_at`` is ordered by STRING comparison (in the key, in the
    accessor's MAX, in the watermark), which equals time order only while every
    value is fixed-width UTC; and ``knowable_as_of`` is ``dt[:10]``, which
    silently becomes garbage under any other shape. An upstream format change is
    exactly the drift 3.1b's frozen fixtures hid for a year, so it raises.
    """
    df = _panel(nfl_fixture).copy()
    df.loc[df.dt == FIXTURE_DTS[0], "dt"] = bad_dt
    with pytest.raises(depth_charts.PanelTimestampFormat):
        _ingest(db, df)


def test_two_rows_claiming_one_slot_raise(db, nfl_fixture):
    """Measured 0 collisions in 554,215 rows. If it ever happens the change log
    cannot say who the occupant is, so the run fails and retries tomorrow — the
    file carries its whole history, so refusing costs nothing."""
    df = _panel(nfl_fixture)
    dupe = df[df.dt == FIXTURE_DTS[0]].head(1)
    with pytest.raises(depth_charts.PanelKeyCollision):
        _ingest(db, pd.concat([df, dupe]))


def test_an_unknown_position_group_is_reported_and_stored_anyway(db, nfl_fixture):
    """The group is IN the key, so the rows are well-formed. Failing a whole
    daily run on an upstream taxonomy addition would be crying wolf."""
    df = _panel(nfl_fixture).copy().reset_index(drop=True)
    df.loc[df.index[:3], "pos_grp_id"] = "99"
    with base.collect_drops() as tally:
        _ingest(db, df)
    assert tally["incomplete"] == 3
    assert tally["dropped"] == 0
    assert db.execute(
        "SELECT COUNT(*) AS n FROM depth_chart_slots WHERE pos_grp_id = '99'"
    ).fetchone()["n"] == 3


# ===========================================================================
# season resolution
# ===========================================================================


def test_season_is_stamped_from_the_file_never_inferred_from_dt(db, nfl_fixture):
    """The 2025 file runs to 2026-03-14 and the 2026 file opens 2026-03-22.
    Inferring the season from ``dt`` would misfile every season's Jan-Mar tail."""
    df = _panel(nfl_fixture)
    march = df[df.dt == FIXTURE_DTS[0]].assign(dt="2026-03-14T07:32:09Z")
    _ingest(db, march, season=2025, retrieved_as_of="2026-03-14")
    assert {r["season"] for r in db.execute("SELECT season FROM depth_chart_slots")} == {2025}


def test_season_auto_resolves_to_one_file_never_a_union(db, nfl_fixture):
    """Measured: at ``as_of=2026-07-25`` an unfiltered read returns 5,489 rows —
    3,176 real 2026 slots plus 2,313 stale 2025 slots — because slots retired at
    the season boundary carry no cross-file tombstone."""
    df = _panel(nfl_fixture)
    _ingest(db, df, retrieved_as_of="2025-09-13")
    next_year = df[df.dt == FIXTURE_DTS[0]].assign(dt="2026-03-22T06:38:42Z")
    _ingest(db, next_year, season=2026, retrieved_as_of="2026-03-22")

    assert depth_charts.resolve_panel_season(db, as_of="2026-07-25") == 2026
    rows = depth_charts.get_depth_chart(db, as_of="2026-07-25")
    assert {r["season"] for r in rows} == {2026}
    # ...and in the March window the answer is the PREVIOUS file's final chart.
    assert depth_charts.resolve_panel_season(db, as_of="2026-03-18") == 2025
    assert depth_charts.get_depth_chart(db, as_of="2000-01-01") == []


@pytest.mark.parametrize(("today", "expected"), [
    ("2026-03-01", 2025),   # ziggurat flips to 2026; nflreadpy has not
    ("2026-03-14", 2025),   # the 2025 file's last measured observation
    ("2026-03-15", 2026),   # nflreadpy's get_current_season(roster=True) flips
    ("2026-03-22", 2026),   # the 2026 file's first measured observation
    ("2026-07-25", 2026),
    ("2026-02-28", 2025),   # before ziggurat's own flip: nothing to reconcile
])
def test_the_march_resolver_bridges_the_two_libraries_flip_dates(today, expected):
    season = depth_charts.nfl_season_of(date.fromisoformat(today))
    assert depth_charts.resolve_season(season=season, today=today) == expected


def test_an_explicitly_requested_past_season_is_never_redirected():
    """The backfill's path. Only the CURRENT season is ambiguous in March."""
    assert depth_charts.resolve_season(season=2025, today="2027-03-05") == 2025


def test_applicable_skips_only_when_todays_panel_is_already_held(db, nfl_fixture):
    _ingest(db, _panel(nfl_fixture))
    assert depth_charts.nothing_new_to_pull(db, season=2025, today="2025-09-21") is not None
    assert depth_charts.nothing_new_to_pull(db, season=2025, today="2025-09-22") is None
    assert depth_charts.nothing_new_to_pull(db, season=2026, today="2025-09-21") is None


# ===========================================================================
# the diff, and the item-3.3 trigger contract
# ===========================================================================


def test_diff_reports_the_burrow_demotion_and_the_promotion_behind_it(db, nfl_fixture):
    _ingest(db, _panel(nfl_fixture))
    moves = depth_charts.depth_chart_diff(
        db, since="2025-09-16", as_of="2025-09-17", season=2025, team="CIN", positions=["QB"])
    by_player = {m["espn_id"]: m for m in moves}
    assert by_player[BURROW]["verdict"] == depth_charts.VERDICT_DEMOTED
    assert (by_player[BURROW]["rank_before"], by_player[BURROW]["rank_after"]) == (1, 3)
    assert by_player[BROWNING]["verdict"] == depth_charts.VERDICT_PROMOTED
    assert (by_player[BROWNING]["rank_before"], by_player[BROWNING]["rank_after"]) == (2, 1)


def test_a_dual_listed_player_is_diffed_per_LISTING_not_per_player(db, nfl_fixture):
    """M1, corrected: ``espn_id -> (pos_abb, pos_rank)`` IS NOT A FUNCTION.

    Measured on the 2025 panel: 48,764 of 495,581 (dt, team, espn_id) triples
    carry more than one row, ALL differing in ``pos_abb``. Keyed on the player
    alone, a dict keeps whichever row iterates last, and the diff reports a move
    between two listings that are not the same listing.

    THIS TEST WAS REWRITTEN BECAUSE IT COULD NOT FAIL (audit finding C11).
    Reverting ``_LISTING_KEY`` to ``("espn_id",)`` left 405 passed / 1 skipped
    across every file that touches the diff: the one-day Dortch window below is
    TRUE but not discriminating, because an unmoved player produces no row under
    either key. What discriminates is a window in which a dual-listed player
    moves at ONE of his listings — measured on this fixture as 88 net moves
    under the shipped key against 72 under ``("espn_id",)``, with 6 fabricated.

    Grant Stuard is the legible one. He is DET's KR1 (special teams) all week and
    moves WLB2 -> SLB2 in the base-4-3 front. Keyed on the player, the diff says
    "SLB demoted 1 -> 2" — and the ``1`` is his KICK RETURN rank, read off a
    listing he never left.
    """
    df = _panel(nfl_fixture)
    _ingest(db, df)

    listings = df[(df.dt == FIXTURE_DTS[0]) & (df.espn_id == DORTCH)]
    assert len(listings) == 3, "fixture must keep the measured dual-listing case"
    assert len({(r.pos_grp_id, r.pos_abb) for r in listings.itertuples()}) == 3
    one_day = depth_charts.depth_chart_diff(
        db, since=FIXTURE_DTS[0][:10], as_of=FIXTURE_DTS[1][:10], season=2025)
    assert not [m for m in one_day if m["espn_id"] == DORTCH], \
        "an unmoved dual-listed player must produce no diff row at all"

    moves = depth_charts.depth_chart_diff(
        db, since=FIXTURE_DTS[0][:10], as_of=FIXTURE_DTS[-1][:10], season=2025)
    assert len(moves) == 88, "the whole-window net move set, keyed per LISTING"
    stuard = sorted((m["pos_abb"], m["verdict"], m["rank_before"], m["rank_after"])
                    for m in moves if m["espn_id"] == STUARD)
    assert stuard == [("SLB", depth_charts.VERDICT_ADDED, None, 2),
                      ("WLB", depth_charts.VERDICT_REMOVED, 2, None)], \
        "a move between two DIFFERENT listings is not a rank change within one"
    assert not [m for m in moves if m["espn_id"] == STUARD
                and m["verdict"] in (depth_charts.VERDICT_PROMOTED,
                                     depth_charts.VERDICT_DEMOTED)]
    # He never left KR1 — the rank the defective key reads as his "before".
    kr = [r for r in depth_charts.get_depth_chart(db, as_of=FIXTURE_DTS[-1][:10], season=2025,
                                                  team="DET")
          if r["espn_id"] == STUARD and r["pos_abb"] == "KR"]
    assert [r["pos_rank"] for r in kr] == [1]


def test_diff_reports_a_club_change_as_removed_plus_added_not_a_rank_move(db, nfl_fixture):
    """Comparing "WR2 in Cincinnati" with "WR4 in Kansas City" and printing
    "demoted" is exactly the well-formed nonsense Rule 6 exists to prevent."""
    df = _panel(nfl_fixture).copy().reset_index(drop=True)
    later = df.index[(df.dt == FIXTURE_DTS[1]) & (df.espn_id == BROWNING)]
    df.loc[later, "team"] = "KC"
    df.loc[later, "pos_rank"] = 4
    _ingest(db, df)

    moves = depth_charts.depth_chart_diff(
        db, since=FIXTURE_DTS[0][:10], as_of=FIXTURE_DTS[1][:10], season=2025)
    mine = sorted((m["verdict"], m["team"]) for m in moves if m["espn_id"] == BROWNING)
    assert mine == [(depth_charts.VERDICT_ADDED, "KC"),
                    (depth_charts.VERDICT_REMOVED, "CIN")]


def test_diff_refuses_a_window_that_runs_backwards(db, nfl_fixture):
    _ingest(db, _panel(nfl_fixture))
    with pytest.raises(ValueError):
        depth_charts.depth_chart_diff(db, since="2025-09-20", as_of="2025-09-14", season=2025)


def test_qb1_change_ships_as_a_labelled_hypothesis_with_the_right_n(db, nfl_fixture):
    """F3 is the most valuable finding in the item and this is where it lands.

    The panel is NOT the shock trigger. ``QB1_CHANGE`` ships as a hypothesis with
    its source in the reason text (3.2's convention), and — because 3.2's own
    audit caught reasons quoting the wrong study's n — the reasons must say that
    the supporting 92% is the n=49 BENEFICIARY number conditioned on the
    starter's ABSENCE, not this trigger's own precision, which nobody measured.
    """
    _ingest(db, _panel(nfl_fixture))
    events = depth_charts.qb1_change_candidates(
        db, since="2025-09-16", as_of="2025-09-17", season=2025)
    assert len(events) == 1
    event = events[0]
    assert (event["team"], event["espn_id"], event["previous_espn_id"]) == ("CIN", BROWNING, BURROW)
    assert event["observed_at"] == FIXTURE_DTS[4]

    blob = " ".join(event["reasons"])
    assert "HYPOTHESIS" in blob
    assert "22 rank-1 QB changes" in blob            # this trigger's population
    assert "PRECISION HAS NEVER BEEN MEASURED" in blob
    assert "n=49" in blob and "ABSENCE" in blob      # the OTHER study, named as such
    assert "not an injury" in blob                   # never an availability claim

    quiet = depth_charts.qb1_change_candidates(
        db, since="2025-09-18", as_of="2025-09-21", season=2025)
    assert quiet == [], "a stable QB1 must not fire"


def test_explain_listing_separates_when_the_chart_was_published_from_since_when(
        db, nfl_fixture):
    """Two dates, and conflating them is the Rule-6 hazard.

    Burrow's QB3 listing was first observed on 09-17 and the chart read here was
    published on 09-18. Printing the row's own instant as "chart observed" would
    tell a novice a live chart is a day stale; printing only the chart date would
    hide that the demotion is not new.
    """
    _ingest(db, _panel(nfl_fixture))
    text = depth_charts.explain_listing(db, as_of="2025-09-18", espn_id=BURROW, season=2025)
    assert "Joe Burrow" in text and "QB3" in text
    assert f"published {FIXTURE_DTS[5]}" in text
    assert f"unchanged since {FIXTURE_DTS[4]}" in text
    assert "not an availability signal" in text
    assert depth_charts.explain_listing(db, as_of="2025-09-18", espn_id="0", season=2025) is None


def test_the_module_does_not_offer_an_injury_trigger(db, nfl_fixture):
    """F3, asserted structurally. Measured on 2025: three starters ruled Out held
    ``pos_rank = 1`` every single day; of 15 rank-1 skill players with >=3
    consecutive Out weeks, 1 (7%) was demoted within 14 days. A future edit that
    adds a "starter fell off the chart" trigger has to delete this test first."""
    exported = {n for n in dir(depth_charts) if not n.startswith("_")}
    assert not {n for n in exported if "injur" in n.lower() or "available" in n.lower()}
    assert "NOT AN INJURY" in depth_charts.__doc__.upper()


# ===========================================================================
# the live contract test — opt-in, the anti-3.1b defence a fixture cannot be
# ===========================================================================


@pytest.mark.skipif(not os.environ.get("ZIGGURAT_LIVE_TESTS"),
                    reason="set ZIGGURAT_LIVE_TESTS=1 to hit live nflverse")
def test_live_upstream_contract():
    """The one test a frozen fixture cannot be. Run before a release or when the
    cadence reports something odd:  ``ZIGGURAT_LIVE_TESTS=1 pytest -k live``."""
    from ziggurat.data.nfl import source as nfl

    df = nfl.import_depth_charts([2026])
    base.require_columns(df, depth_charts._PANEL_COLUMNS, source="depth_charts")
    newest = df[df.dt == df.dt.max()]
    assert newest["espn_id"].notna().all(), "espn_id IS NULL is the tombstone sentinel"
    assert (newest["espn_id"] != "").all()
    key = ["team", "pos_grp_id", "pos_id", "pos_rank"]
    assert not newest.duplicated(subset=key).any(), "slot key must be unique in a panel"
    assert newest["team"].nunique() == 32
    assert set(df["pos_grp_id"].unique()) <= depth_charts.KNOWN_POS_GRP_IDS


@pytest.mark.skipif(not os.environ.get("ZIGGURAT_LIVE_TESTS"),
                    reason="set ZIGGURAT_LIVE_TESTS=1 to hit live nflverse")
def test_live_collapse_floor_still_separates():
    """The one property of ``PANEL_COLLAPSE_RATIO`` no committed fixture can hold.

    0.50 is not a taste, it is the middle of a measured gap — and a gap is a
    property of live upstream, so it has to be re-measured against live upstream.
    On 2026-07-25 the whole 2025+2026 files gave: worst partial scrape 0.495 (SEA
    2026-05-24, 99 -> 49), best legitimate shrinkage 0.563 (LV 2026-04-19,
    71 -> 40), nothing in between, 12 club-panels below the floor and 0 in the
    gap. If this fails, do not move the constant on a hunch: print the sorted
    ratios (the module docstring lists all 12 with their dates) and decide with
    the new distribution in front of you.
    """
    from ziggurat.data.nfl import source as nfl

    ratios, below = [], []
    for season in (2025, 2026):
        df = nfl.import_depth_charts([season])
        trusted = {}
        for dt in sorted(df.dt.unique()):
            here = df[df.dt == dt].groupby("team").size().to_dict()
            degraded = set()
            for team, before in trusted.items():
                ratio = here.get(team, 0) / before
                ratios.append(ratio)
                if ratio < depth_charts.PANEL_COLLAPSE_RATIO:
                    degraded.add(team)
                    below.append((round(ratio, 3), dt, team, before, here.get(team, 0)))
            for team, count in here.items():
                if team not in degraded:
                    trusted[team] = count

    worst_bad = max(r for r, *_ in below)
    best_good = min(r for r in ratios if r >= depth_charts.PANEL_COLLAPSE_RATIO)
    assert below, "no partial scrape in either file — has upstream changed?"
    assert worst_bad < depth_charts.PANEL_COLLAPSE_RATIO <= best_good
    assert best_good - worst_bad > 0.05, (
        f"the gap has closed: worst partial {worst_bad}, best legitimate "
        f"{best_good}. Re-measure before touching the constant. Below floor: {below}")
