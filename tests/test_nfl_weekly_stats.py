"""Cached-fixture integration + leakage tests for weekly-stats ingestion.

Weekly lines are post-game facts stamped with the team's gameday, so schedules
must be ingested first (base.game_date_map resolves (season, week, recent_team)).
Fixture is 2023 weeks 5 (2023-10-05..09) and 6 (2023-10-12..16).

Leakage note: the frozen base.select_as_of gates on BOTH knowable_as_of <= as_of
AND retrieved_as_of <= as_of (a fact is unreadable before it was pulled). To
isolate and prove the game-date knowable gate — the crux of this source — the
leakage test pulls on 2023-10-11 (retrieved_as_of <= as_of) so the gameday gate
is the binding constraint: week 5 (played) is readable, week 6 (not yet) is not.
"""

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

from ziggurat.core import scoring
from ziggurat.data.nfl import base, schedules, weekly_stats
from ziggurat.data.nfl.weekly_stats import KICKING_COLUMNS, kicker_scoring_inputs

# Breece Hall (NYJ RB), week 5 2023: 3 receptions, 177 rushing yards.
_HALL = "00-0038120"


def _load_schedules(db, nfl_fixture):
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")


def test_ingest_and_get_rb_week5(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats")
    n = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")
    assert n == len(df) > 0  # every fixture team resolves to a gameday; none dropped

    rows = weekly_stats.get_weekly_stats(
        db, as_of="2023-10-20", season=2023, week=5, player_id=_HALL
    )
    assert len(rows) == 1
    row = rows[0]
    # A known RB's week-5 receptions / rushing yards are present and numeric.
    assert isinstance(row["receptions"], (int, float))
    assert isinstance(row["rushing_yards"], (int, float))
    assert row["receptions"] == 3
    assert row["rushing_yards"] == 177.0
    # knowable_as_of is that team's week-5 gameday, not the pull date.
    assert row["knowable_as_of"].startswith("2023-10-0")


def test_position_filter(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    weekly_stats.ingest_weekly_stats(
        db, nfl_fixture("weekly_stats"), retrieved_as_of="2023-10-20"
    )
    rbs = weekly_stats.get_weekly_stats(db, as_of="2023-10-20", week=5, position="RB")
    assert rbs and all(r["position"] == "RB" for r in rbs)


def test_weekly_stats_leakage_by_gameday(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    # Pull on 2023-10-11 so retrieved_as_of <= as_of and the gameday gate binds.
    weekly_stats.ingest_weekly_stats(
        db, nfl_fixture("weekly_stats"), retrieved_as_of="2023-10-11"
    )

    # 2023-10-11 is after every week-5 game (10-05..09) but before every week-6
    # game (10-12..16): week 5 is knowable, week 6 must be hidden.
    seen = weekly_stats.get_weekly_stats(db, as_of="2023-10-11", season=2023)
    weeks = {r["week"] for r in seen}
    assert weeks == {5}, "week 5 knowable, week 6 must be gated out"

    # By 2023-10-20 both weeks are knowable (and retrieved).
    later = {r["week"] for r in weekly_stats.get_weekly_stats(db, as_of="2023-10-20", season=2023)}
    assert later == {5, 6}


def test_unresolvable_rows_dropped_without_schedules(db, nfl_fixture):
    # With no schedules loaded, no (season, week, recent_team) resolves to a
    # gameday, so every row is dropped (counted out) rather than stored with a
    # NULL/leaky knowable_as_of.
    df = nfl_fixture("weekly_stats")
    n = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")
    assert n == 0
    assert weekly_stats.get_weekly_stats(db, as_of="2023-10-20", season=2023) == []


def test_null_player_id_rows_are_dropped_not_fatal(db, nfl_fixture):
    """nflverse ships all-zero placeholder rows with a NULL player_id — measured
    2026-07-24: 22 of 19,421 rows in stats_player_week_2025, one per week.

    player_id is this table's NOT NULL primary key, so leaving them in made the
    WHOLE pull raise IntegrityError mid-executemany. That is worse than it looks:
    on a shared connection the partially-inserted rows stayed in the open
    transaction and the NEXT ingester's commit persisted them, leaving a
    permanently truncated week-1-only table with valid stamps on every row.
    """
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats").copy().reset_index(drop=True)
    good = len(df)
    df.loc[0, "player_id"] = None

    n = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")
    assert n == good - 1, "the null-key row is dropped; every other row still lands"
    assert db.execute("SELECT COUNT(*) c FROM weekly_stats WHERE player_id IS NULL") \
             .fetchone()["c"] == 0


def test_the_null_player_id_drop_is_counted_not_silent(db, nfl_fixture):
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats").copy().reset_index(drop=True)
    df.loc[0, "player_id"] = None
    df.loc[1, "player_id"] = None
    with base.collect_drops() as tally:
        weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")
    assert tally["dropped"] >= 2


# --- F-H: the drop denominator ----------------------------------------------


def test_drop_accounting_uses_one_denominator(db, nfl_fixture):
    """F-H. ``base.collect_drops`` SUMS ``total`` over every ``note_drops``
    call, so the two calls this ingester used to make reported a denominator
    roughly DOUBLE the frame — measured ``{'dropped': 22, 'total': 37916}`` for
    an 18,969-row frame.

    Confirmed cosmetic, and the test says why so nobody re-inflates the claim:
    ``refresh.run_ingest`` computes its own ``seen = written + dropped`` and
    never reads ``tally['total']``, so the ceiling was never affected. It was
    still wrong in the module whose job is drop accounting.
    """
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats").copy().reset_index(drop=True)
    df.loc[0, "player_id"] = None            # one drop in the FIRST class...
    with base.collect_drops() as tally:
        written = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")

    assert tally["total"] == len(df), "the denominator is the frame, counted once"
    assert tally["dropped"] == 1
    assert written + tally["dropped"] == len(df)  # nothing unaccounted for


def test_both_drop_classes_are_counted_against_the_same_frame(db, nfl_fixture):
    """Both classes at once — a null player_id AND an unstampable team — still
    sum to one dropped count over one denominator, and the log names each class
    with its own count rather than collapsing them into one number."""
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats").copy().reset_index(drop=True)
    df.loc[0, "player_id"] = None
    df.loc[1, "recent_team"] = "ZZZ"         # no such team in schedules -> unstampable
    with base.collect_drops() as tally:
        written = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")

    assert tally["dropped"] == 2
    assert tally["total"] == len(df)
    assert written == len(df) - 2


# --- item 4.1 §7.1: the kicking columns (migration 013) ----------------------
#
# Item 1.4's column list carried no kicking stat, so every stored kicker row was
# all-zero for every stat the house pays him for and re-scoring the table graded
# EVERY kicker at 0.000 with nothing raised (the draft backtest's 2026-08-30
# finding). The tests below pin the three things that make that failure
# impossible to repeat silently: the columns round-trip, a NOT-CAPTURED row
# reads as None (never a zeroed stat line), and a frame without the columns is
# refused before any write.

# Nick Folk (TEN K), week 5 2023: FGs of 20-29, 30-39, 50-59 made, 1 PAT, 0 misses.
_FOLK = "00-0025565"


def _load_kickers(db, nfl_fixture, *, retrieved_as_of="2023-10-20"):
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats_kickers")
    n = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of=retrieved_as_of)
    assert n == len(df) == 58, "every kicker row resolves to a gameday; none dropped"
    return df


def test_kicking_columns_round_trip(db, nfl_fixture):
    """Every one of the eight nflverse buckets lands as a non-NULL int, and the
    fold onto scoring.py's keys is a stat line ``score_kicker`` prices."""
    df = _load_kickers(db, nfl_fixture)
    rows = base.latest_truth(weekly_stats.get_weekly_stats)(
        db, as_of="2023-10-20", season=2023, week=5, position="K")
    assert len(rows) == int((df["week"] == 5).sum()) == 28
    for row in rows:
        for col in KICKING_COLUMNS:
            assert isinstance(row[col], int), (row["player_id"], col, row[col])
        assert kicker_scoring_inputs(row) is not None

    folk = next(r for r in rows if r["player_id"] == _FOLK)
    stats = kicker_scoring_inputs(folk)
    assert stats == {
        "fg_made_0_39": 2, "fg_made_40_49": 0, "fg_made_50_59": 1, "fg_made_60": 0,
        "pat_made": 1, "fg_missed": 0,
    }
    # The fold prices identically to the raw-distance form scoring.py documents
    # as equivalent — so no house number is asserted here, only that the two
    # inputs the scorer accepts agree on this line.
    by_distance = scoring.score_kicker(
        {"fg_made_distances": [25, 35, 55], "pat_made": 1, "fg_missed": 0})
    assert scoring.score_kicker(stats) == by_distance > 0


def test_the_fold_sums_the_three_sub_forty_buckets_onto_one_key():
    row = {"fg_made_0_19": 1, "fg_made_20_29": 2, "fg_made_30_39": 3, "fg_made_40_49": 4,
           "fg_made_50_59": 5, "fg_made_60_": 6, "pat_made": 7, "fg_missed": 8}
    assert kicker_scoring_inputs(row) == {
        "fg_made_0_39": 6, "fg_made_40_49": 4, "fg_made_50_59": 5, "fg_made_60": 6,
        "pat_made": 7, "fg_missed": 8,
    }


def test_uncaptured_kicking_reads_as_none_not_zero(db, nfl_fixture):
    """A row retrieved before migration 013 — the 2026-07-25-shaped row, every
    kicking column NULL — is NOT a kicker who scored nothing; it is a kicker
    nobody captured. ``kicker_scoring_inputs`` must say so with ``None`` rather
    than hand ``score_kicker`` a line that prices at 0.0 (the §7.1 silent-zero
    failure, restated as the guard against it)."""
    _load_schedules(db, nfl_fixture)
    db.execute(
        "INSERT INTO weekly_stats (player_id, season, week, season_type, position, "
        "recent_team, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?)",
        (_FOLK, 2023, 5, "REG", "K", "TEN", "2026-07-25", "2023-10-08"))
    db.commit()
    rows = base.latest_truth(weekly_stats.get_weekly_stats)(
        db, as_of="2023-10-20", season=2023, week=5, player_id=_FOLK)
    assert len(rows) == 1
    assert all(rows[0][col] is None for col in KICKING_COLUMNS)
    assert kicker_scoring_inputs(rows[0]) is None

    # One NULL among eight is still "not captured": a partial line is not a line.
    partial = {c: 1 for c in KICKING_COLUMNS}
    partial["fg_missed"] = None
    assert kicker_scoring_inputs(partial) is None
    # And a row that never had the columns at all (a pre-013 shape read by
    # name) is None too, not a KeyError the caller has to know to catch.
    assert kicker_scoring_inputs({"player_id": _FOLK}) is None

    # Item 4.1 audit, KICK-5: NaN is pandas' NULL. A dict row from
    # ``DataFrame.to_dict("records")`` with one NaN cell is "not captured" too —
    # None, not the ``int(nan)`` ValueError the docstring never promised.
    nan_dict = {c: 1 for c in KICKING_COLUMNS}
    nan_dict["fg_made_40_49"] = float("nan")
    assert kicker_scoring_inputs(nan_dict) is None
    frame = pd.DataFrame([{c: 1 for c in KICKING_COLUMNS}, {c: 1 for c in KICKING_COLUMNS}])
    frame.loc[0, "pat_made"] = math.nan
    records = frame.to_dict("records")
    assert math.isnan(records[0]["pat_made"])
    assert kicker_scoring_inputs(records[0]) is None
    assert kicker_scoring_inputs(records[1]) is not None
    # A CAPTURED all-zero row is a zero line, never None: zero is a fact
    # (that player really made 0 kicks); position is the merging caller's
    # concern, not this fold's.
    assert kicker_scoring_inputs({c: 0 for c in KICKING_COLUMNS}) == {
        "fg_made_0_39": 0, "fg_made_40_49": 0, "fg_made_50_59": 0, "fg_made_60": 0,
        "pat_made": 0, "fg_missed": 0,
    }


def test_a_nan_kicking_cell_is_counted_incomplete_and_warned_not_silent(
        db, nfl_fixture, caplog):
    """Item 4.1 audit, KICK-3. A kicking cell upstream serves as NaN lands NULL
    (``base._clean``) and reads NOT CAPTURED — the fail-safe direction — but the
    cause used to be unlogged, and the docstring blamed a pre-013 partition that
    a re-pull would fix, when a re-pull reproduces the same NULL. The row is
    KEPT (its skill stats are fine), so it goes through ``note_incomplete``,
    never ``note_drops``: 58 written, 1 incomplete, 0 dropped, a WARNING naming
    the column."""
    import logging

    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats_kickers").copy().reset_index(drop=True)
    folk_wk5 = df.index[(df["player_id"] == _FOLK) & (df["week"] == 5)][0]
    df["fg_missed"] = df["fg_missed"].astype(float)
    df.loc[folk_wk5, "fg_missed"] = math.nan

    with caplog.at_level(logging.WARNING, logger="ziggurat.data.nfl.base"):
        with base.collect_drops() as tally:
            n = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2026-09-01")
    assert n == 58
    assert tally["incomplete"] == 1 and tally["dropped"] == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and r.getMessage().startswith("weekly_stats:")]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "kept 1/58" in text and "fg_missed" in text
    assert "NOT CAPTURED" in text and "re-pull will not fill it" in text

    stored = db.execute(
        "SELECT fg_missed, pat_made FROM weekly_stats WHERE player_id = ? AND week = 5",
        (_FOLK,)).fetchone()
    assert stored["fg_missed"] is None and stored["pat_made"] == 1
    rows = base.latest_truth(weekly_stats.get_weekly_stats)(
        db, as_of="2024-02-28", season=2023, week=5, player_id=_FOLK)
    assert len(rows) == 1 and kicker_scoring_inputs(rows[0]) is None
    # every OTHER row is still a captured line
    others = base.latest_truth(weekly_stats.get_weekly_stats)(
        db, as_of="2024-02-28", season=2023, position="K")
    assert sum(kicker_scoring_inputs(r) is None for r in others) == 1


def test_a_clean_frame_records_nothing_incomplete(db, nfl_fixture):
    """The counter is silent on the fixture as shipped (0 NaN), so the WARNING
    above is a signal, not the baseline noise the operator learns to ignore."""
    _load_schedules(db, nfl_fixture)
    with base.collect_drops() as tally:
        weekly_stats.ingest_weekly_stats(
            db, nfl_fixture("weekly_stats_kickers"), retrieved_as_of="2026-09-01")
    assert tally["incomplete"] == 0 and tally["dropped"] == 0


def test_a_re_pull_versions_the_uncaptured_row_rather_than_editing_it(db, nfl_fixture):
    """The migration does not rewrite the 2026-07-25 partition. A re-pull lands a
    SECOND version under a newer ``retrieved_as_of``; ``select_as_of`` resolves
    the newest, so the captured line wins — while a ``historical`` read at an
    as_of before the re-pull day still sees the uncaptured row (Rule 1: what was
    known then, not what is known now)."""
    _load_schedules(db, nfl_fixture)
    db.execute(
        "INSERT INTO weekly_stats (player_id, season, week, season_type, position, "
        "recent_team, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?)",
        (_FOLK, 2023, 5, "REG", "K", "TEN", "2026-07-25", "2023-10-08"))
    db.commit()
    df = nfl_fixture("weekly_stats_kickers")
    weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2026-09-01")

    stored = db.execute(
        "SELECT retrieved_as_of, fg_made_20_29 FROM weekly_stats WHERE player_id = ? "
        "AND week = 5 ORDER BY retrieved_as_of", (_FOLK,)).fetchall()
    assert [tuple(r) for r in stored] == [("2026-07-25", None), ("2026-09-01", 1)]

    newest = base.latest_truth(weekly_stats.get_weekly_stats)(
        db, as_of="2024-02-28", season=2023, week=5, player_id=_FOLK)
    assert kicker_scoring_inputs(newest[0]) is not None
    then = weekly_stats.get_weekly_stats(
        db, as_of="2026-08-01", season=2023, week=5, player_id=_FOLK)
    assert len(then) == 1 and kicker_scoring_inputs(then[0]) is None


@pytest.mark.parametrize("column", KICKING_COLUMNS)
def test_require_columns_fails_loudly_without_kicking(db, nfl_fixture, column):
    """Mutation-proof for the 3.1b frozen-fixture lesson: a frame missing any
    one kicking column is refused BEFORE any write, naming the column — never
    stored as a kicking-blind row that reads as zero downstream."""
    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats_kickers").drop(columns=[column])
    with pytest.raises(ValueError, match=column):
        weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")
    assert db.execute("SELECT COUNT(*) c FROM weekly_stats").fetchone()["c"] == 0


def test_the_frozen_skill_fixture_carries_zero_kicking_not_null(nfl_fixture):
    """The QB/RB/WR/TE fixture gained the eight columns as int32 ZEROS (it has no
    kicker rows, so zero is the truth upstream serves for a non-kicker) rather
    than being regenerated from live nflverse — a regeneration moves wopr /
    air_yards_share on ~180 rows and relabels FB rows, and the signals tests
    assert on those values."""
    df = nfl_fixture("weekly_stats")
    assert len(df) == 575 and (df["position"] == "K").sum() == 0
    assert (df[list(KICKING_COLUMNS)] == 0).all().all()
    assert df[list(KICKING_COLUMNS)].isna().sum().sum() == 0


# --- cross-check against the draft backtest's parquet supplement --------------


def _supplement_points(frame, season, tmp_path):
    """``backtest.draft_backtest._kicker_points`` over ``frame`` written as the
    supplement's cache file — the number the 2026-08-30 draft backtest graded
    the K slot with. Imported lazily: ``backtest/`` pulls ``ziggurat.draft`` in,
    which a data-layer test module has no other reason to load."""
    from backtest import draft_backtest as bt

    cache = tmp_path / "kicking"
    cache.mkdir()
    frame.to_parquet(cache / f"kicking-{season}.parquet", index=False)
    return bt._kicker_points(season, cache_dir=str(cache))


def test_db_kicker_points_equal_the_backtest_supplement_on_the_fixture(
        db, nfl_fixture, tmp_path):
    """The DB path (migration 013 columns -> ``kicker_scoring_inputs`` ->
    ``score_kicker``) and the backtest's parquet supplement (``_kicker_points``)
    must agree kicker-for-kicker, week-for-week, or retiring the supplement
    would move a published number. Exact equality on all 58 rows."""
    df = _load_kickers(db, nfl_fixture)
    supplement = _supplement_points(df, 2023, tmp_path)

    compared = 0
    for week in (5, 6):
        rows = base.latest_truth(weekly_stats.get_weekly_stats)(
            db, as_of="2024-02-28", season=2023, week=week, position="K")
        for row in rows:
            stats = kicker_scoring_inputs(row)
            assert stats is not None
            assert scoring.score_kicker(stats) == supplement[row["player_id"]][week]
            compared += 1
    assert compared == 58


_LIVE_DB = Path(__file__).resolve().parents[1] / "db" / "ziggurat.sqlite"
_LIVE_KICKING_2023 = Path(__file__).resolve().parents[1] / "data" / "backtest" / "kicking-2023.parquet"


def _refuse_partial_capture(lines) -> None:
    """The live cross-check's gate (item 4.1 audit, KICK-4). ``lines`` is
    ``[(player_id, week, kicker_scoring_inputs(row) | None), ...]``.

    EVERY line None (or no lines) is the documented pre-backfill state — skip,
    with the remedy. SOME None is a partial capture: a defect (KICK-3's NaN
    cell is what produces it), so it FAILS naming the count and the first row,
    instead of hiding behind the same skip and the same remedy that would not
    change it."""
    missing = [(pid, week) for pid, week, stats in lines if stats is None]
    if not lines or len(missing) == len(lines):
        pytest.skip("weekly_stats 2023 kicking columns not captured yet "
                    "(run: ziggurat ingest backfill --source weekly_stats --force)")
    if missing:
        pytest.fail(
            f"{len(missing)} of {len(lines)} 2023 K lines have NULL kicking columns "
            f"under latest_truth (first: {missing[0]}): partial capture is a defect, "
            "not the pre-backfill state")


@pytest.mark.skipif(
    not (_LIVE_DB.exists() and _LIVE_KICKING_2023.exists()),
    reason="needs the operator's live db/ziggurat.sqlite and data/backtest/kicking-2023.parquet",
)
def test_live_db_kicker_points_equal_the_backtest_supplement_2023():
    """The same agreement on the OPERATOR'S database, for every 2023 REG week:
    what the re-backfilled ``weekly_stats`` prices a kicker at through the
    house rules equals what ``data/backtest/kicking-2023.parquet`` graded him
    at. Read-only, and skipped (never failed) until the re-backfill that
    populates the columns has run — a NULL row is 'not captured', which is the
    documented state, not a mismatch."""
    import sqlite3

    from backtest import draft_backtest as bt

    conn = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = base.latest_truth(weekly_stats.get_weekly_stats)(
            conn, as_of="2024-02-28", season=2023, position="K")
    finally:
        conn.close()
    lines = [(r["player_id"], r["week"], kicker_scoring_inputs(r))
             for r in rows if r["season_type"] == "REG"]
    _refuse_partial_capture(lines)

    supplement = bt._kicker_points(2023, cache_dir=str(_LIVE_KICKING_2023.parent))
    week1 = [pid for pid, week, _ in lines if week == 1]
    assert len(week1) >= 3, "week 1 must carry at least three kickers to compare"
    mismatches = [
        (pid, week, scoring.score_kicker(stats), supplement.get(pid, {}).get(week))
        for pid, week, stats in lines
        if scoring.score_kicker(stats) != supplement.get(pid, {}).get(week)
    ]
    assert mismatches == []
    assert len(lines) == sum(len(weeks) for weeks in supplement.values())


def _file_db_with_kickers(tmp_path, nfl_fixture):
    """A FILE-backed full-schema DB (the live test opens by path, read-only)
    carrying the kicker fixture relabelled to weeks 1-2 — the live test insists
    on week 1 — plus the supplement parquet written from the same frame."""
    from ziggurat.data.store import apply_schema, connect

    path = tmp_path / "ziggurat.sqlite"
    conn = connect(path)
    apply_schema(conn)
    try:
        _load_schedules(conn, nfl_fixture)
        df = nfl_fixture("weekly_stats_kickers").copy().reset_index(drop=True)
        df["week"] = df["week"].map({5: 1, 6: 2})
        n = weekly_stats.ingest_weekly_stats(conn, df, retrieved_as_of="2026-09-01")
        assert n == 58
        conn.commit()
    finally:
        conn.close()
    df.to_parquet(tmp_path / "kicking-2023.parquet", index=False)
    return path


def test_the_live_cross_check_skips_only_when_nothing_is_captured(
        tmp_path, nfl_fixture, monkeypatch):
    """Item 4.1 audit, KICK-4: drive the live cross-check itself against a
    fixture DB through its module-level paths. All captured -> passes; ONE row
    NULL -> fails naming the count and the row (a partial capture used to read
    as 'not captured yet' and skip); every row NULL -> the documented skip."""
    import sqlite3

    path = _file_db_with_kickers(tmp_path, nfl_fixture)
    here = sys.modules[__name__]
    monkeypatch.setattr(here, "_LIVE_DB", path)
    monkeypatch.setattr(here, "_LIVE_KICKING_2023", tmp_path / "kicking-2023.parquet")

    # all captured: the cross-check runs to completion on the 58 lines
    test_live_db_kicker_points_equal_the_backtest_supplement_2023()

    # mixed: one NULL cell -> a failure that names the row, never a skip
    conn = sqlite3.connect(path)
    conn.execute("UPDATE weekly_stats SET fg_missed = NULL WHERE player_id = ? AND week = 1",
                 (_FOLK,))
    conn.commit()
    conn.close()
    with pytest.raises(pytest.fail.Exception) as failed:
        test_live_db_kicker_points_equal_the_backtest_supplement_2023()
    msg = str(failed.value)
    assert "1 of 58 2023 K lines have NULL kicking columns" in msg
    assert f"(first: ('{_FOLK}', 1))" in msg and "partial capture is a defect" in msg
    assert "not captured yet" not in msg

    # every row NULL: the pre-backfill state, skipped with the remedy
    conn = sqlite3.connect(path)
    conn.execute("UPDATE weekly_stats SET " + ", ".join(f"{c} = NULL" for c in KICKING_COLUMNS))
    conn.commit()
    conn.close()
    with pytest.raises(pytest.skip.Exception, match="not captured yet"):
        test_live_db_kicker_points_equal_the_backtest_supplement_2023()


def test_the_partial_capture_gate_on_bare_lines():
    """The predicate alone, so its three branches are pinned without a DB."""
    stats = {"fg_made_0_39": 1, "fg_made_40_49": 0, "fg_made_50_59": 0, "fg_made_60": 0,
             "pat_made": 2, "fg_missed": 0}
    _refuse_partial_capture([("a", 1, stats), ("b", 1, stats)])  # all captured: returns
    with pytest.raises(pytest.skip.Exception):
        _refuse_partial_capture([])
    with pytest.raises(pytest.skip.Exception):
        _refuse_partial_capture([("a", 1, None), ("b", 2, None)])
    with pytest.raises(pytest.fail.Exception, match=r"1 of 3 .*\(first: \('b', 2\)\)"):
        _refuse_partial_capture([("a", 1, stats), ("b", 2, None), ("c", 3, stats)])
