"""Cached-fixture integration + leakage tests for injury-report ingestion.

The leakage test proves REPORT-DATE gating: an injury report is knowable the
moment it was filed (its ``date_modified``), not on gameday — so the boundary
that flips is the report's own timestamp, not the 10-11 game boundary.

WHAT THE FIXTURE CANNOT CATCH (item 3.1b's lesson): the fixture is a frozen 2023
weeks 5-6 slice and 2023 contains ZERO duplicate player-weeks (measured live,
2026-07-25, all five seasons: 2021 0, 2022 2, 2023 0, 2024 4, 2025 0). So no
fixture-driven test could ever have caught finding F-F, and none of these tests
prove anything about the live 2026 schema. The dedupe tests below are built from
the exact rows measured upstream and are stated as such; the upstream-shape
guarantee is `require_columns`' job, not theirs.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from ziggurat.data.nfl import base, injuries, schedules


def test_ingest_and_get(db, nfl_fixture):
    df = nfl_fixture("injuries")
    n = injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")
    assert n == len(df)  # every fixture row carries a date_modified -> none dropped

    # report_status / practice_status populated for a known player-week.
    src = df[(df.week == 6) & df.report_status.notna() & df.practice_status.notna()].iloc[0]
    rows = injuries.get_injuries(db, as_of="2023-11-01", season=2023, week=6, gsis_id=src.gsis_id)
    assert len(rows) == 1
    got = rows[0]
    assert got["report_status"] == src.report_status
    assert got["practice_status"] == src.practice_status
    assert got["full_name"] == src.full_name
    assert got["date_modified"] == base.iso_date(src.date_modified)  # stored day-granular


def test_report_date_gates_knowability(db, nfl_fixture):
    df = nfl_fixture("injuries")
    # Retrieval anchored well before any week-6 report so ONLY the report-date
    # (knowable_as_of) gate can flip in the window we probe.
    injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-01")

    # Pick the latest-modified week-6 report; its date_modified D is its knowledge time.
    wk6 = df[df.week == 6].sort_values("date_modified")
    row = wk6.iloc[-1]
    gsis = row.gsis_id
    D = base.iso_date(row.date_modified)
    day_before = (date.fromisoformat(D) - timedelta(days=1)).isoformat()

    # A day before the report was filed, it is not yet knowable...
    assert injuries.get_injuries(db, as_of=day_before, season=2023, week=6, gsis_id=gsis) == []
    # ...on the report date it appears.
    shown = injuries.get_injuries(db, as_of=D, season=2023, week=6, gsis_id=gsis)
    assert len(shown) == 1
    assert shown[0]["gsis_id"] == gsis
    assert shown[0]["knowable_as_of"] == D


def test_nothing_knowable_before_any_report_filed(db, nfl_fixture):
    injuries.ingest_injuries(db, nfl_fixture("injuries"), retrieved_as_of="2023-10-20")
    # Reports are filed in October; before then none are knowable (knowable_as_of
    # is the report date, and retrieved_as_of does not gate).
    assert injuries.get_injuries(db, as_of="2023-09-30", season=2023) == []


def test_missing_date_modified_falls_back_to_own_team_gameday_not_week_first(db, nfl_fixture):
    # The leak the audit caught: with no date_modified, the fallback must anchor
    # to the player's OWN team gameday, never the week's first kickoff (which is
    # earlier and would expose the report before it was filed for late-week teams).
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    gdm = base.game_date_map(db)
    week6 = {t: d for (s, w, t), d in gdm.items() if s == 2023 and w == 6}
    week_first = min(week6.values())

    inj = nfl_fixture("injuries")
    # A week-6 injured player whose team plays LATER than the week's first game.
    late = next(
        r for _, r in inj[inj.week == 6].iterrows()
        if week6.get(r["team"], week_first) > week_first
    )
    team_gameday = week6[late["team"]]

    df = inj.copy()
    mask = (df.week == 6) & (df.gsis_id == late["gsis_id"])
    df.loc[mask, "date_modified"] = None  # force the fallback path
    injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")

    rows = injuries.get_injuries(db, as_of="2023-11-01", season=2023, week=6, gsis_id=late["gsis_id"])
    assert rows
    assert rows[0]["knowable_as_of"] == team_gameday          # own team gameday
    assert rows[0]["knowable_as_of"] > week_first             # NOT the leaky week-first anchor
    # And it stays hidden the day before that team plays (no leak).
    before = (date.fromisoformat(team_gameday) - timedelta(days=1)).isoformat()
    assert injuries.get_injuries(db, as_of=before, season=2023, week=6, gsis_id=late["gsis_id"]) == []


def test_a_release_without_date_modified_still_ingests(db, nfl_fixture):
    """VERIFIED 2026-07-24 against live nflverse: the 2025+ injury release no
    longer carries ``date_modified`` at all (2024 has it, 2025 does not — the
    upstream feed died after 2024 and 2025 exists only as a post-season bulk
    backfill). require_columns therefore raised on EVERY pull, and the frozen
    2023 fixture hid it: the suite stayed green over a broken production path.

    The column is optional now; every row falls back to the team-gameday anchor,
    which is coarser but still leakage-safe.
    """
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("injuries").drop(columns=["date_modified"])
    n = injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")
    assert n > 0

    game_dates = base.game_date_map(db)
    rows = injuries.get_injuries(db, as_of="2023-11-01", season=2023, week=6)
    assert rows
    for row in rows:
        assert row["date_modified"] is None
        assert row["knowable_as_of"] == game_dates[(2023, 6, row["team"])]


def test_a_release_missing_a_genuinely_required_column_still_fails_loud(db, nfl_fixture):
    """Only date_modified is optional. Real drift must still be a red build."""
    df = nfl_fixture("injuries").drop(columns=["report_status"])
    with pytest.raises(ValueError, match="report_status"):
        injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")


# --- F-F: one row per player-week, and it must be the RIGHT one --------------
#
# The table's grain is (gsis_id, season, week, retrieved_as_of); the source's is
# finer (one row per report revision, and one per club for a player who moves
# mid-week). Something has to choose. Before item 3.2c the choice was "whichever
# row pandas listed last", and it measurably chose wrong on the one status class
# standing rule 6 is built around.

GSIS = "00-9999999"


def _reports(nfl_fixture, statuses, stamps, *, teams=None, week=6, season=2023):
    """Build a frame of same-player-week reports in the given FRAME ORDER.

    Column shape is borrowed from the real fixture row so dtypes and the full
    column set stay honest; only the fields under test are overridden.
    """
    template = nfl_fixture("injuries").iloc[0]
    rows = []
    for i, (status, stamp) in enumerate(zip(statuses, stamps, strict=True)):
        row = template.copy()
        row["gsis_id"] = GSIS
        row["full_name"] = "Fixture Player"
        row["season"] = season
        row["week"] = week
        row["report_status"] = status
        row["date_modified"] = pd.Timestamp(stamp, tz="UTC") if stamp else None
        if teams is not None:
            row["team"] = teams[i]
        rows.append(row)
    return pd.DataFrame(rows).reset_index(drop=True)


def _stored_status(db):
    # as_of == the pull date: the historical view gates retrieval time too, so a
    # read before the pull is (correctly) empty.
    rows = injuries.get_injuries(db, as_of="2026-07-25", season=2023, week=6, gsis_id=GSIS)
    assert len(rows) == 1, f"expected exactly one stored row, got {len(rows)}"
    return rows[0]


def test_a_newer_out_is_not_overwritten_by_an_older_questionable(db, nfl_fixture):
    """THE defect. Measured live 2026-07-25 on the real 2024 file:

        Tyler Conklin NYJ wk15  Out          date_modified 2024-12-15 13:57:00
        Tyler Conklin NYJ wk15  Questionable date_modified 2024-12-14 20:55:19

    Frame order lists Out FIRST, so ``INSERT OR REPLACE`` stored **Questionable**
    — with the earlier knowable_as_of. Rule 6 says the system must never
    recommend starting a player ruled OUT; this silently deleted that fact.
    """
    df = _reports(
        nfl_fixture,
        ["Out", "Questionable"],
        ["2024-12-15 13:57:00", "2024-12-14 20:55:19"],
    )
    assert injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25") == 1
    stored = _stored_status(db)
    assert stored["report_status"] == "Out"
    assert stored["date_modified"] == "2024-12-15"
    assert stored["knowable_as_of"] == "2024-12-15"  # the LATER report's own time


def test_the_severity_tiebreak_keeps_out_when_there_is_no_date_modified(db, nfl_fixture):
    """The 2025+ regime: nflverse ships no ``date_modified`` at all, so
    recency cannot decide and the ladder must. Out is listed first, so
    last-write-wins would again store Questionable."""
    df = _reports(nfl_fixture, ["Out", "Questionable"], [None, None])
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    assert injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25") == 1
    assert _stored_status(db)["report_status"] == "Out"


def test_the_severity_tiebreak_keeps_out_when_the_timestamps_are_equal(db, nfl_fixture):
    df = _reports(
        nfl_fixture, ["Out", "Questionable"],
        ["2023-10-13 12:00:00", "2023-10-13 12:00:00"],
    )
    assert injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25") == 1
    assert _stored_status(db)["report_status"] == "Out"


def test_an_undated_out_is_not_shadowed_by_a_dated_questionable(db, nfl_fixture):
    """A row with no ``date_modified`` stays in contention: absence of a
    timestamp is not evidence of staleness. Treating it as oldest is exactly how
    an undated OUT would lose to a dated QUESTIONABLE."""
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    # Out FIRST, so last-write-wins would store the dated Questionable.
    df = _reports(nfl_fixture, ["Out", "Questionable"], [None, "2023-10-13 12:00:00"])
    assert injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25") == 1
    assert _stored_status(db)["report_status"] == "Out"


def test_an_unrecognized_status_is_never_ranked_least_severe(db, nfl_fixture, caplog):
    """Do not let a new upstream vocabulary silently sort to the benign end —
    that is F-F with a different label. An unknown value outranks every
    recognized non-Out designation..."""
    injuries._warned_statuses.clear()
    with caplog.at_level("WARNING", logger="ziggurat.data.nfl"):
        df = _reports(nfl_fixture, ["Questionable", "Reserve/PUP"],
                      ["2023-10-13 12:00:00", "2023-10-13 12:00:00"])
        injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25")
    assert _stored_status(db)["report_status"] == "Reserve/PUP"
    assert any("unrecognized report_status" in r.getMessage() for r in caplog.records)


def test_an_unrecognized_status_does_not_outrank_an_explicit_out(db, nfl_fixture):
    """...but it must not demote ``Out`` either. Out is the designation rule 6
    names by name; losing it to a string we have never seen is the same bug with
    the sign flipped."""
    # Out FIRST, so last-write-wins would store the unknown value.
    df = _reports(nfl_fixture, ["Out", "Reserve/PUP"],
                  ["2023-10-13 12:00:00", "2023-10-13 12:00:00"])
    injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25")
    assert _stored_status(db)["report_status"] == "Out"


def test_note_ranks_as_no_designation_not_as_an_unknown(db, nfl_fixture):
    """'Note' is a measured real value (6 rows, 2024 only) whose own text says
    "No game status" — a roster note, not a designation. It must lose to any
    real designation, and must NOT trip the unknown-status warning."""
    assert injuries._severity("Note") == injuries._severity(None) == 0
    # Questionable FIRST, so last-write-wins would store the roster note.
    df = _reports(nfl_fixture, ["Questionable", "Note"],
                  ["2023-10-13 12:00:00", "2023-10-13 12:00:00"])
    injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25")
    assert _stored_status(db)["report_status"] == "Questionable"


def test_a_same_day_de_escalation_keeps_the_LATER_report(db, nfl_fixture):
    """Recency is compared at full timestamp precision even though the column is
    STORED day-granular. Cade Stover's real 2024 wk15 pair is same-day (03:34
    Questionable, 14:17 Out); truncating to the date first would have thrown that
    ordering away and fallen through to the severity ladder.

    The ladder is right for a TIE and blunt for a genuine de-escalation — "Out on
    Friday morning, upgraded to Questionable on Friday afternoon" — which no
    season 2021-2025 contains but nothing forbids. Here the later report is the
    LESS severe one, so this test fails if precision is dropped.
    """
    df = _reports(nfl_fixture, ["Out", "Questionable"],
                  ["2023-10-13 09:00:00", "2023-10-13 16:00:00"])
    injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25")
    stored = _stored_status(db)
    assert stored["report_status"] == "Questionable"
    assert stored["date_modified"] == "2023-10-13"   # still stored day-granular


def test_the_real_same_day_escalation_still_lands_on_out(db, nfl_fixture):
    """The measured direction of the same-day case (Cade Stover, HOU wk15 2024):
    03:34 Questionable then 14:17 Out. Later AND more severe — both rules agree."""
    df = _reports(nfl_fixture, ["Questionable", "Out"],
                  ["2024-12-15 03:34:33", "2024-12-15 14:17:06"])
    injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25")
    assert _stored_status(db)["report_status"] == "Out"


def test_an_unusable_timestamp_degrades_to_undated_instead_of_raising():
    """A comparison failure inside the dedupe would fail the WHOLE pull (a
    naive/aware mix, an unparseable cell). Such a value degrades to "undated",
    which the ordering rule already handles safely: the row stays a candidate and
    severity decides. Tested at the dedupe seam because ``base.iso_date`` would
    mangle a junk string into ``knowable_as_of`` long before it got here — that
    is a separate (pre-existing) concern in base, not this rule's.
    """
    assert injuries._recency("not a date") is None
    assert injuries._recency(None) is None
    assert injuries._recency(pd.NaT) is None
    # naive and aware in the same group must not raise
    assert injuries._recency(pd.Timestamp("2023-10-13 09:00:00")) is not None

    def row(status, stamp):
        return {"gsis_id": GSIS, "season": 2023, "week": 6,
                "report_status": status, "date_modified": stamp}

    kept, dropped = injuries._dedupe_player_weeks([
        row("Out", "not a date"),
        row("Questionable", pd.Timestamp("2023-10-13 09:00:00")),   # tz-NAIVE
    ])
    assert dropped == 1
    assert kept[0]["report_status"] == "Out"


def test_severity_ordering_is_explicit_and_total():
    sev = injuries._severity
    assert sev("Out") > sev("Doubtful") > sev("Questionable") > sev("Probable") > sev(None)
    assert sev("out") == sev("  Out ") == sev("OUT")          # case/space insensitive
    assert sev("Out") > sev("Reserve/PUP") > sev("Doubtful")  # unknown sits below Out


def test_a_mid_week_club_change_keeps_the_newer_club(db, nfl_fixture):
    """A SECOND collision cause, measured live and not a revision at all:

        00-0033280 Christian McCaffrey 2022 wk7 CAR  date_modified 2022-10-19
        00-0033280 Christian McCaffrey 2022 wk7 SF   date_modified 2022-10-22

    He was traded mid-week. ``team`` is not in the injuries PK (unlike
    snap_counts, which item 3.2c widened), so one club's row cannot be stored.
    The rule keeps the newer — the club he actually played for.
    """
    df = _reports(nfl_fixture, [None, None], ["2022-10-19 18:48:29", "2022-10-22 13:29:14"],
                  teams=["CAR", "SF"])
    assert injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25") == 1
    assert _stored_status(db)["team"] == "SF"
    # Same answer with the frame order reversed — the rule decides, not iteration
    # order. (Upstream happens to list CAR first; nothing guarantees it will.)
    db.execute("DELETE FROM injuries")
    reversed_frame = df.iloc[::-1].reset_index(drop=True)
    assert injuries.ingest_injuries(db, reversed_frame, retrieved_as_of="2026-07-25") == 1
    assert _stored_status(db)["team"] == "SF"


def test_the_dedupe_is_reported_never_silent(db, nfl_fixture):
    """Losing a fact must be observable. It goes in the plain (ceiling-counting)
    drop channel, NOT ``by_design`` — a superseded-revision population that
    exploded would be a real regrain, and ``filtered`` is excluded from
    refresh's ceiling."""
    df = _reports(nfl_fixture, ["Out", "Questionable"],
                  ["2024-12-15 13:57:00", "2024-12-14 20:55:19"])
    with base.collect_drops() as tally:
        written = injuries.ingest_injuries(db, df, retrieved_as_of="2026-07-25")
    assert written == 1
    assert tally["dropped"] == 1
    assert tally["filtered"] == 0
    assert tally["collapsed"] == 0  # resolved BEFORE the upsert, not by SQLite


def test_drop_accounting_uses_one_denominator(db, nfl_fixture):
    """F-H's sibling: ``collect_drops`` SUMS ``total`` across calls, so two
    note_drops calls report a denominator larger than the frame."""
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("injuries")
    with base.collect_drops() as tally:
        injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")
    assert tally["total"] == len(df)


def test_a_null_gsis_row_is_counted_not_silently_swallowed(db, nfl_fixture):
    """``gsis_id`` is the NOT NULL primary key, so a null-key row cannot be
    stored — but until item 3.2c it vanished with no ``note_drops`` call at all.
    Measured 0 such rows in 2021-2025, which is exactly why an unreported path
    is dangerous: nothing would say when it started firing."""
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    df = nfl_fixture("injuries").copy().reset_index(drop=True)
    df.loc[0, "gsis_id"] = None
    with base.collect_drops() as tally:
        written = injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")
    assert written == len(df) - 1
    assert tally["dropped"] == 1
    assert tally["total"] == len(df)


def test_a_clean_frame_drops_nothing(db, nfl_fixture):
    """Regression guard: the dedupe must not start eating distinct player-weeks."""
    df = nfl_fixture("injuries")
    with base.collect_drops() as tally:
        written = injuries.ingest_injuries(db, df, retrieved_as_of="2023-10-20")
    assert written == len(df)
    assert tally["dropped"] == 0
