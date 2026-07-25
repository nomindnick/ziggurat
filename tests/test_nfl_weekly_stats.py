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

from ziggurat.data.nfl import schedules, weekly_stats

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
    from ziggurat.data.nfl import base

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
    from ziggurat.data.nfl import base

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
    from ziggurat.data.nfl import base

    _load_schedules(db, nfl_fixture)
    df = nfl_fixture("weekly_stats").copy().reset_index(drop=True)
    df.loc[0, "player_id"] = None
    df.loc[1, "recent_team"] = "ZZZ"         # no such team in schedules -> unstampable
    with base.collect_drops() as tally:
        written = weekly_stats.ingest_weekly_stats(db, df, retrieved_as_of="2023-10-20")

    assert tally["dropped"] == 2
    assert tally["total"] == len(df)
    assert written == len(df) - 2
