"""Item 4.2b, Unit F — the same-week FantasyPros weekly ECR capture (migration 016).

Two things get more attention here than everything else, because they are the two
ways this source silently corrupts a later result:

* **THE WEEK LABEL.** The feed states no NFL week, and the rule already in the
  repo (``fpecr.infer_nfl_week``) answers **week 0** — "a preseason board" — for
  a capture the live page ranks as **week 1** (measured 2026-09-04). A weekly
  board has no week-0 edition, so filing one under week 0 would be a wrong answer
  wearing a number. ``test_the_shipped_rule_and_the_fpecr_rule_disagree_...``
  pins BOTH rules side by side, so an edit toward the archive's rule fails loudly
  rather than shifting every weekly comparison.

* **THE CAPTURE FLOOR.** This ingester has no delete path, and it still needs a
  floor: ``select_as_of`` resolves the newest ``retrieved_as_of`` per key, so a
  thin or emptied scrape shadows a good one merely by arriving later
  (``players.CrosswalkCollapse``). Four arms, four tests, each asserting that
  NOTHING was written.

The ingest fixture is the REAL upstream file, trimmed: 46 rows of the public
2026-09-04 board (six per league page, three per IDP page, plus the one row whose
``r2p_pts`` is blank) parsed through the module's own reader, so the dtypes
upstream actually serves — an int64 id, an all-NaN float column where a TEXT
column is declared — are exercised rather than assumed. Rule 5: NFL player names
only; nothing league-private, and nothing captured from FantasyPros is committed
beyond this public board slice.

NOTHING HERE TOUCHES THE NETWORK. ``fetch_fp_weekly`` and ``fetch_week_page`` are
seams; the FantasyPros page in particular is behind an opt-in that DEFAULTS OFF
and is never fetched by a test.
"""

import re
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from ziggurat.data.nfl import base, fp_weekly, fpecr, refresh

FIXTURE = Path(__file__).parent / "fixtures" / "nfl" / "fp_latest_weekly.csv"

#: The board's own scrape day, and the day these tests pull it.
SCRAPE = "2026-09-04"

#: A crosswalk for four of the fixture's players. Deliberately partial: the point
#: of the `note_incomplete` channel is that an unresolved id is KEPT.
_CROSSWALK = {
    "19196": ("00-0036442", "3915511"),   # Joe Burrow
    "17298": ("00-0034857", "3918298"),   # Josh Allen
    "16393": ("00-0033280", "3116385"),   # a WR
    "11192": ("00-0027944", "13982"),     # a TE
}


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def board():
    """The trimmed real board, read through the module's own parser."""
    return fp_weekly.read_fp_weekly(FIXTURE)


def _stub_players(db, mapping=None, *, retrieved="2026-08-01"):
    for fp, (gsis, espn) in (mapping or _CROSSWALK).items():
        db.execute(
            "INSERT INTO players (gsis_id, fantasypros_id, espn_id, retrieved_as_of, "
            "knowable_as_of) VALUES (?,?,?,?,?)",
            (gsis, fp, espn, retrieved, retrieved),
        )
    db.commit()


def _stub_schedule(db, season=2026, weeks=None, *, retrieved="2026-08-01"):
    """``weeks`` maps week -> (first gameday, last gameday); one game at each end.

    The 2026 default is the REAL calendar: week 1 opens on a WEDNESDAY
    (2026-09-09) and closes 09-14, which is what makes a 2026-09-04 capture a
    pre-opener one.
    """
    weeks = weeks or {
        1: ("2026-09-09", "2026-09-14"),
        2: ("2026-09-17", "2026-09-21"),
        3: ("2026-09-24", "2026-09-28"),
    }
    for week, (first, last) in weeks.items():
        for i, day in enumerate(dict.fromkeys((first, last))):
            db.execute(
                "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
                "home_team, away_team, retrieved_as_of, knowable_as_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{season}_{week}_{i}", season, week, "REG", day, "BUF", "MIA",
                 retrieved, retrieved),
            )
    db.commit()


def _ingest(db, frame, *, day=SCRAPE, page_week=None):
    with base.collect_drops() as tally:
        written = fp_weekly.ingest_fp_weekly(
            db, frame, retrieved_as_of=day, page_week=page_week
        )
    return written, tally


def _restamp(frame, scrape):
    out = frame.copy()
    out["scrape_date"] = scrape
    return out


# ------------------------------------------------------ the ingest, end to end


def test_the_real_board_lands_with_its_league_rows_and_drops_the_idp_pages(db, board):
    """The cached-fixture ingestion pattern, on the file upstream actually serves.

    Two claims at once: the league board is stored whole, and the IDP pages
    (db/dl/lb — 996 of 1,678 rows on the full scrape) are filtered BY DESIGN, not
    dropped. The distinction is not cosmetic: item 3.1b's first live run failed
    `adp_rankings` on a 35% "drop" that was almost entirely this same filter.
    """
    _stub_players(db)
    _stub_schedule(db)
    written, tally = _ingest(db, board)

    assert written == 36                      # six per league page, six pages
    assert tally["dropped"] == 0              # nothing was LOST
    assert tally["filtered"] == 10            # 3 db + 3 dl + 3 lb + the extra db row
    stored = db.execute(
        "SELECT page, COUNT(*) FROM fp_weekly_ecr GROUP BY page ORDER BY page"
    ).fetchall()
    assert [(r[0], r[1]) for r in stored] == [
        ("dst", 6), ("k", 6), ("ppr-rb", 6), ("ppr-te", 6), ("ppr-wr", 6), ("qb", 6),
    ]
    assert {r[0] for r in db.execute("SELECT DISTINCT position FROM fp_weekly_ecr")} <= (
        fp_weekly.LEAGUE_POSITIONS
    )


def test_the_market_columns_land_verbatim_and_the_stamps_are_the_scrape_day(db, board):
    """One row, checked end to end, because a colmap typo is invisible in a count."""
    _stub_players(db)
    _stub_schedule(db)
    _ingest(db, board)

    row = db.execute(
        "SELECT * FROM fp_weekly_ecr WHERE fantasypros_id = '19196'"
    ).fetchone()
    assert row["player"] == "Joe Burrow"
    assert row["page"] == "qb" and row["position"] == "QB" and row["team"] == "CIN"
    assert row["rank"] == 1
    assert row["ecr"] == pytest.approx(1.73)
    assert (row["sd"], row["best"], row["worst"]) == (0.91, 1, 4)
    assert row["pos_rank_label"] == "QB1"     # upstream's LABEL, not an integer
    assert row["start_sit_grade"] == "A+"
    assert row["r2p_pts"] == pytest.approx(21.4)
    assert row["player_opponent"] == "vs. TB" and row["player_opponent_id"] == "TB"
    assert row["player_owned_avg"] == pytest.approx(98.9)
    assert row["gsis_id"] == "00-0036442" and row["espn_id"] == "3915511"
    assert row["season"] == 2026
    # knowable = the board's own scrape day (what adp_rankings/fpecr already do);
    # retrieved = the pull day. Same day here, and they are different columns.
    assert row["knowable_as_of"] == SCRAPE and row["retrieved_as_of"] == SCRAPE


def test_an_all_nan_upstream_column_stores_as_NULL_not_as_a_float(db, board):
    """`note`/`tag`/`recommendation` are empty on every scrape seen so far, so
    pandas types them float64 and hands the ingester `nan` for a TEXT column.
    Storing that would put a float in a TEXT column and make `IS NULL` lie."""
    _stub_players(db)
    _stub_schedule(db)
    _ingest(db, board)

    for column in ("note", "tag", "recommendation", "player_ecr_delta"):
        nulls = db.execute(
            f"SELECT COUNT(*) FROM fp_weekly_ecr WHERE {column} IS NOT NULL"
        ).fetchone()[0]
        assert nulls == 0, column
    assert board["note"].notna().sum() == 0    # the premise, not an assumption


def test_a_dst_row_keeps_a_null_gsis_and_is_never_dropped(db, board):
    """The 003 contract: FantasyPros keys defenses by a TEAM id that is absent
    from the player crosswalk. Those rows are still real market facts."""
    _stub_players(db)
    _stub_schedule(db)
    _, tally = _ingest(db, board)

    dst = db.execute("SELECT * FROM fp_weekly_ecr WHERE page = 'dst'").fetchall()
    assert len(dst) == 6
    assert all(r["gsis_id"] is None for r in dst)
    assert all(r["team"] is not None for r in dst)
    # Reported as INCOMPLETE (kept, missing a field), never as a drop.
    assert tally["dropped"] == 0 and tally["incomplete"] > 0


def test_an_unresolved_crosswalk_id_is_kept_rather_than_dropped(db, board):
    """No crosswalk at all: every row still lands, every gsis_id is NULL."""
    _stub_schedule(db)                        # deliberately no _stub_players
    written, tally = _ingest(db, board)

    assert written == 36
    assert tally["dropped"] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM fp_weekly_ecr WHERE gsis_id IS NOT NULL"
    ).fetchone()[0] == 0


def test_a_missing_upstream_column_fails_loudly(db, board):
    """The item-1.4 contract: upstream schema drift must never store partial rows."""
    _stub_schedule(db)
    with pytest.raises(ValueError, match="ecr"):
        _ingest(db, board.drop(columns=["ecr"]))


def test_a_missing_r2p_pts_column_is_optional_and_stored_null(db, board):
    """2026-09-15: upstream dropped ``r2p_pts`` from ``fp_latest_weekly.csv`` on the
    first in-season Tuesday and the whole perishable week-2 board failed loudly
    (timer 07:22 PT). Nothing may read that column (RULE 2), so its absence is
    not drift: the capture lands and the column is NULL. Every OTHER column is
    still required (the test above)."""
    _stub_schedule(db)
    written, _ = _ingest(db, board.drop(columns=["r2p_pts"]))
    assert written == 36
    nulls = db.execute(
        "SELECT COUNT(*) FROM fp_weekly_ecr WHERE r2p_pts IS NULL").fetchone()[0]
    assert nulls == 36
    # And a board that still carries it stores it — the column was not dropped.
    assert "r2p_pts" in fp_weekly._COLMAP


def test_the_same_player_on_two_pages_is_two_rows_not_one(db, board):
    """`page` is IN THE PRIMARY KEY, which is the migration-011 lesson applied
    here: a dual-eligibility player is published on two positional pages of one
    series on one day, and those are two genuine market facts. Drop `page` from
    the key and INSERT OR REPLACE keeps whichever row the loader handed SQLite
    last, with nothing reporting the substitution."""
    _stub_schedule(db)
    dual = board[board["page"] == "ppr-rb"].head(1).copy()
    dual["page"] = "ppr-wr"
    dual["pos"] = "WR"
    written, tally = _ingest(db, pd.concat([board, dual], ignore_index=True))

    assert written == 37
    assert tally["collapsed"] == 0 and tally["duplicated"] == 0
    both = db.execute(
        "SELECT page FROM fp_weekly_ecr WHERE fantasypros_id = ? ORDER BY page",
        (str(dual.iloc[0]["fantasypros_id"]),),
    ).fetchall()
    assert [r[0] for r in both] == ["ppr-rb", "ppr-wr"]


def test_a_row_with_no_scrape_date_refuses_the_whole_capture(db, board):
    """`scrape_date` is upstream's own snapshot key and this table's knowledge
    time. A row without one cannot be stamped under Rule 1, and a board whose
    snapshot key has gone missing is schema drift, not a data quirk — so the run
    is refused rather than half-stored under a guessed date."""
    _stub_schedule(db)
    broken = board.copy()
    broken.loc[broken.index[0], "scrape_date"] = None
    with pytest.raises(fp_weekly.WeeklyEcrCollapse, match="no scrape_date"):
        _ingest(db, broken)
    assert db.execute("SELECT COUNT(*) FROM fp_weekly_ecr").fetchone()[0] == 0


def test_a_row_with_no_page_is_a_drop_not_a_by_design_filter(db, board):
    """`page` is a NOT NULL KEY column, so a row without one cannot be stored —
    that is a LOSS, and it must land on the channel `run_ingest`'s 20% ceiling
    reads rather than beside the IDP filter, which is excluded from it. Otherwise
    a systematic upstream change that emptied the column would look exactly like
    a normal day on which two thirds of the file is correctly filtered.

    NOT fatal, unlike a missing `scrape_date`: one malformed row must not cost a
    perishable board, while a missing snapshot key is schema drift."""
    _stub_schedule(db)
    broken = board.copy()
    broken.loc[broken.index[0], "page"] = None
    written, tally = _ingest(db, broken)

    assert written == 35
    assert tally["dropped"] == 1               # counted against the ceiling
    assert tally["filtered"] == 10             # the IDP rows, unchanged
    assert "no `page`" in "".join(tally["reasons"])


def test_a_board_with_no_league_pages_is_refused_rather_than_logged_ok(db, board):
    """"Wrote 0 rows" is never ok (item 3.1b). ~41% of this file is league
    positions, so an empty result after the filter is a schema change."""
    _stub_schedule(db)
    idp_only = board[board["page"].isin(["db", "dl", "lb"])]
    with pytest.raises(fp_weekly.WeeklyEcrCollapse, match="no rows survived"):
        _ingest(db, idp_only)


def test_a_rewritten_board_versions_rather_than_replaces(db, board):
    """`retrieved_as_of` is IN THE PRIMARY KEY, and that is the whole reason to
    capture a twice-daily source daily: the earlier vintage must still be on disk
    afterwards, and the reader must resolve to the newer one."""
    _stub_schedule(db)
    today = _restamp(board, "2026-09-17")
    _ingest(db, today, day="2026-09-17")
    revised = today.copy()
    revised["ecr"] = revised["ecr"] + 1.0
    _ingest(db, revised, day="2026-09-18")     # SAME scrape_date, later pull

    vintages = {r[0] for r in db.execute(
        "SELECT DISTINCT retrieved_as_of FROM fp_weekly_ecr")}
    assert vintages == {"2026-09-17", "2026-09-18"}
    assert db.execute("SELECT COUNT(*) FROM fp_weekly_ecr").fetchone()[0] == 72
    read = fp_weekly.get_fp_weekly_ecr(db, as_of="2026-09-18", page="qb")
    assert len(read) == 6
    assert {r["retrieved_as_of"] for r in read} == {"2026-09-18"}


def test_two_pulls_on_one_day_overwrite_because_the_stamp_is_day_granular(db, board):
    """RECORDED, not asserted away (recon hazard 11). `retrieved_as_of` is a DAY,
    so upstream's TWICE-DAILY rewrite cannot be captured twice in one day — the
    second pull wins. A future reader who expects intraday vintages finds the
    answer in a test rather than in a surprise."""
    _stub_schedule(db)
    today = _restamp(board, "2026-09-17")
    _ingest(db, today, day="2026-09-17")
    revised = today.copy()
    revised["ecr"] = revised["ecr"] + 1.0
    _ingest(db, revised, day="2026-09-17")     # the SECOND pull of one day

    assert db.execute(
        "SELECT COUNT(*) FROM fp_weekly_ecr").fetchone()[0] == 36
    burrow = db.execute(
        "SELECT ecr FROM fp_weekly_ecr WHERE fantasypros_id = '19196'").fetchone()[0]
    assert burrow == pytest.approx(2.73)      # the SECOND pull's value


# ------------------------------------------------------------- the week label


def test_a_pre_opener_capture_is_unknown_and_never_says_schedules(db, board):
    """THE UNIT'S HEADLINE REQUIREMENT. A board scraped before any REG game has
    been played cannot be labelled by the schedule: "the first week that has not
    finished" answers week 1 for every day back to March, so the schedule is not
    deciding anything, it is agreeing with the only week left.

    `week_basis` must therefore be 'unknown' with a NULL week, NOT 'schedules'
    and NOT week 0 — the answer `fpecr.infer_nfl_week` gives, which would file a
    real weekly board under a week that does not exist."""
    _stub_schedule(db)                        # week 1 opens 2026-09-09
    _ingest(db, board, day=SCRAPE)            # scraped 2026-09-04

    rows = db.execute(
        "SELECT DISTINCT nfl_week, week_basis FROM fp_weekly_ecr").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(None, "unknown")]


def test_the_shipped_rule_and_the_fpecr_rule_disagree_on_the_pre_opener_day(db):
    """The disagreement, pinned in one place so an edit toward the archive's rule
    fails loudly. Measured 2026-09-04: `fpecr.infer_nfl_week` answers week 0
    ("a preseason board") while the live FantasyPros page ranks week 1."""
    _stub_schedule(db)
    bounds = fp_weekly.week_bounds(db, 2026)

    assert fpecr.infer_nfl_week(SCRAPE, bounds) == (0, "schedules")
    week, basis, why = fp_weekly.infer_weekly_board_week(SCRAPE, bounds)
    assert (week, basis) == (None, "unknown")
    assert "no preseason edition" in why
    assert fp_weekly.WEEK_PAGE_ENV in why     # names the remedy on the day it helps


@pytest.mark.parametrize(
    ("scrape", "expected"),
    [
        ("2026-09-15", (2, "schedules")),     # the TUESDAY the archive is built around
        ("2026-09-09", (1, "schedules")),     # the opener itself
        ("2026-09-14", (1, "schedules")),     # week 1's last gameday — the soft day
        ("2026-09-17", (2, "schedules")),
        ("2026-09-28", (3, "schedules")),
        ("2026-09-29", (None, "unknown")),    # past the last stubbed REG gameday
        ("2026-09-08", (None, "unknown")),    # still pre-opener
    ],
)
def test_the_schedule_rule_labels_each_day_of_the_week_cycle(db, scrape, expected):
    """The rule is exact from the opener on, and the FIRST TUESDAY — the day the
    whole archive is built around — is unambiguous: week 1 is finished and week 2
    is the first unfinished one.

    2026-09-14 is the one SOFT day and is pinned deliberately rather than left to
    discovery: week 1's last gameday is its Monday night game, so this rule still
    answers 1 that day while FantasyPros may already have flipped its page to
    week 2 (recon UNKNOWN 5). `week_basis` records that the SCHEDULE decided it,
    which is what lets a later reader tell the two apart."""
    _stub_schedule(db)
    bounds = fp_weekly.week_bounds(db, 2026)
    week, basis, _ = fp_weekly.infer_weekly_board_week(scrape, bounds)
    assert (week, basis) == expected


def test_with_no_schedule_ingested_the_week_is_unknown_not_guessed(db, board):
    """A missing schedule costs the WEEK LABEL, not the rows — which is exactly
    why this source declares `needs_schedules=False` while six others declare
    True and would drop 100%."""
    written, _ = _ingest(db, board)           # no _stub_schedule
    assert written == 36
    rows = db.execute(
        "SELECT DISTINCT nfl_week, week_basis FROM fp_weekly_ecr").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(None, "unknown")]


def test_the_page_authority_wins_over_the_schedule_and_says_so(db, board):
    """Operator decision D2(b): when the FantasyPros page IS read, it is the
    authority — it states the week rather than inferring it. `week_basis` records
    which one answered, so nothing has to trust a bare number."""
    _stub_schedule(db)
    _ingest(db, board, day=SCRAPE, page_week=(1, "1757000000"))

    rows = db.execute(
        "SELECT DISTINCT nfl_week, week_basis FROM fp_weekly_ecr").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "fantasypros_page")]


# ------------------------------------------------- the opt-in page authority


def test_the_page_authority_is_off_by_default(db):
    """DEFAULT OFF, pending operator decision D2(b). An empty environment, an
    unset variable and an explicitly falsy one all mean OFF; nothing about this
    setting may depend on the repo `.env` happening to be absent."""
    assert fp_weekly.week_page_enabled({}) is False
    assert fp_weekly.week_page_enabled({"SOMETHING_ELSE": "1"}) is False
    for value in ("", "0", "false", "no", "off", "maybe"):
        assert fp_weekly.week_page_enabled(
            {fp_weekly.WEEK_PAGE_ENV: value}) is False, value


def test_the_page_authority_turns_on_only_for_an_explicit_truthy_value():
    for value in ("1", "true", "TRUE", "yes", "on", " on "):
        assert fp_weekly.week_page_enabled(
            {fp_weekly.WEEK_PAGE_ENV: value}) is True, value


def test_a_pull_with_the_authority_off_never_reaches_the_page(db, board, monkeypatch):
    """TEETH on the default. The capture must happen either way, and with the
    opt-in off the FantasyPros page must not be requested at all — the ToS
    argument for the default is about REQUESTS, not about the label."""
    _stub_schedule(db)
    monkeypatch.setattr(fp_weekly, "fetch_fp_weekly",
                        lambda **kw: FIXTURE.read_bytes())

    def _never(**kw):                          # pragma: no cover - must not run
        raise AssertionError("the FantasyPros page was fetched with the opt-in OFF")

    monkeypatch.setattr(fp_weekly, "fetch_week_page", _never)
    with base.collect_drops():
        written = fp_weekly.pull_fp_weekly(
            db, retrieved_as_of=SCRAPE, season=2026, environ={})
    assert written == 36
    assert db.execute(
        "SELECT DISTINCT week_basis FROM fp_weekly_ecr").fetchone()[0] == "unknown"


def test_a_pull_with_the_authority_on_labels_from_the_page(db, board, monkeypatch):
    _stub_schedule(db)
    monkeypatch.setattr(fp_weekly, "fetch_fp_weekly",
                        lambda **kw: FIXTURE.read_bytes())
    # `resolve_page_week` USED to bind `fetch_week_page` as a default argument at
    # def time, so this monkeypatch never reached it and the test made a LIVE
    # request to FantasyPros on every suite run — silently passing while the real
    # page happened to say week 1, and failing the morning it flipped to week 2
    # (2026-09-15). `pull_fp_weekly` now passes the fetcher explicitly, so the
    # patch below is what is actually read.
    monkeypatch.setattr(
        fp_weekly, "fetch_week_page",
        lambda **kw: 'x = 1; var ecrData = {"year": 2026, "week": 1, '
                     '"last_updated_ts": "1757000000"}; more();',
    )
    with base.collect_drops():
        fp_weekly.pull_fp_weekly(
            db, retrieved_as_of=SCRAPE, season=2026,
            environ={fp_weekly.WEEK_PAGE_ENV: "1"})

    rows = db.execute(
        "SELECT DISTINCT nfl_week, week_basis FROM fp_weekly_ecr").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "fantasypros_page")]


def test_a_page_from_the_wrong_season_is_refused_as_an_authority(db):
    """The one silent failure mode of an out-of-band label: a cached January page
    ranking week 18 of the PREVIOUS season. Refused, logged, and the schedule
    answers instead — the capture is never lost over an optional label."""
    page = ('var ecrData = {"year": 2025, "week": 18, "last_updated_ts": "1"};')
    assert fp_weekly.resolve_page_week(season=2026, fetcher=lambda: page) is None
    assert fp_weekly.resolve_page_week(season=2025, fetcher=lambda: page) == (
        18, "1")


@pytest.mark.parametrize("html", [
    "",                                        # no script block at all
    "var ecrData = {not json};",               # unparseable
    'var ecrData = {"year": 2026};',           # no week
    'var ecrData = {"year": 2026, "week": "wk1"};',   # week is not an integer
])
def test_an_unreadable_page_degrades_to_none_rather_than_raising(html):
    """An OPTIONAL label upgrade must never cost a perishable capture. A marketing
    team changing a script tag is not a reason to lose the day's board."""
    assert fp_weekly.parse_page_week(html) is None
    assert fp_weekly.resolve_page_week(season=2026, fetcher=lambda: html) is None


def test_a_page_fetch_that_raises_degrades_to_the_schedule(db):
    def _boom():
        raise TimeoutError("upstream is asleep")

    assert fp_weekly.resolve_page_week(season=2026, fetcher=_boom) is None


# ------------------------------------------------------------- the leakage pair


def test_the_historical_view_holds_a_board_until_its_own_scrape_day(db, board):
    """THE FACT-TIME GATE. `knowable_as_of` is the scrape date, so a read as-of
    the day before sees nothing and a read as-of the day itself sees the board."""
    _stub_schedule(db)
    _ingest(db, board, day="2026-09-15")

    assert fp_weekly.get_fp_weekly_ecr(db, as_of="2026-09-14") == []
    assert len(fp_weekly.get_fp_weekly_ecr(db, as_of="2026-09-15")) == 36


def test_a_bulk_loaded_past_board_is_invisible_under_the_default_view(db, board):
    """THE RETRIEVAL GATE, which is the half a caller forgets. A board LOADED
    today but stamped with a past scrape date is not something this system knew
    then — under `historical` it is silent, exactly as `fpecr_panel`'s bulk rows
    are, and a forgetful caller gets silence rather than a wrong answer."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, "2026-09-15"), day="2026-10-01")   # loaded later

    assert fp_weekly.get_fp_weekly_ecr(db, as_of="2026-09-15") == []
    assert fp_weekly.get_fp_weekly_ecr(db, as_of="2026-09-30") == []
    assert len(fp_weekly.get_fp_weekly_ecr(db, as_of="2026-10-01")) == 36


def test_latest_truth_reads_the_bulk_board_and_still_gates_fact_time(db, board):
    """The other half of the pair, and the half that must NOT be a free pass:
    `latest_truth` lifts the RETRIEVAL gate only. The same stored rows become
    readable at their own scrape day, and a day EARLIER than the scrape still
    sees nothing."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, "2026-09-15"), day="2026-10-01")
    truth = base.latest_truth(fp_weekly.get_fp_weekly_ecr)

    assert len(truth(db, as_of="2026-09-15")) == 36
    assert truth(db, as_of="2026-09-14") == []


def test_the_reader_resolves_to_the_newest_capture_of_one_scrape(db, board):
    """Two captures of the SAME scrape_date (a re-pull the next morning) are two
    versions of one fact, and a read must return one row per key — the newest."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, "2026-09-15"), day="2026-09-15")
    revised = _restamp(board, "2026-09-15")
    revised["ecr"] = revised["ecr"] + 5.0
    _ingest(db, revised, day="2026-09-16")

    rows = fp_weekly.get_fp_weekly_ecr(db, as_of="2026-09-16", page="qb")
    assert len(rows) == 6
    assert {r["retrieved_as_of"] for r in rows} == {"2026-09-16"}


def test_every_filter_narrows_without_bypassing_the_gate(db, board):
    _stub_schedule(db)
    _ingest(db, board, day=SCRAPE)

    assert len(fp_weekly.get_fp_weekly_ecr(db, as_of=SCRAPE, page="dst")) == 6
    assert len(fp_weekly.get_fp_weekly_ecr(db, as_of=SCRAPE, position="K")) == 6
    assert len(fp_weekly.get_fp_weekly_ecr(db, as_of=SCRAPE, season=2026)) == 36
    assert fp_weekly.get_fp_weekly_ecr(db, as_of=SCRAPE, season=2025) == []
    assert len(fp_weekly.get_fp_weekly_ecr(
        db, as_of=SCRAPE, scrape_date=SCRAPE)) == 36
    # nfl_week is NULL for this pre-opener board, and a NULL never equals a value.
    assert fp_weekly.get_fp_weekly_ecr(db, as_of=SCRAPE, nfl_week=1) == []


def test_the_accessor_refuses_an_unknown_view(db):
    with pytest.raises(ValueError, match="unknown as-of view"):
        fp_weekly.get_fp_weekly_ecr(db, as_of=SCRAPE, view="whatever")


# --------------------------------------------------------------- the floor
#
# THE UNIT OF REFUSAL IS THE PAGE, AND THE BASELINE IS THE SAME WEEK (item
# 3.14a). Every test below states which of those two it pins, because the live
# defect on 2026-09-15 was BOTH at once: a whole-board floor compared a week-2
# board against a week-1 capture and refused a complete `dst` page — the page
# item 3.14 had made `ziggurat stream`'s primary D/ST ranker that same morning.
#
# `WK2_A`/`WK2_B` are two days inside the stubbed week-2 window, so a capture on
# each is two captures of ONE week — the only comparison the fence makes.
WK2_A, WK2_B = "2026-09-15", "2026-09-16"
#: Inside week 1 of the same stubbed calendar (09-09..09-14).
WK1_DAY = "2026-09-14"

#: The live shapes measured on 2026-09-15, 07:30 PT — the defect's own numbers.
#: `dst` is COMPLETE (32 of 32 NFL teams) and `k` is whole while the skill pages
#: sit at roughly half the mature board.
LIVE_WK2_TUESDAY = {"dst": 32, "k": 33, "qb": 33,
                    "ppr-rb": 88, "ppr-te": 54, "ppr-wr": 116}
#: The capture stored the previous day (week 1, the mature board).
LIVE_WK1_MONDAY = {"dst": 32, "k": 36, "qb": 82,
                   "ppr-rb": 146, "ppr-te": 147, "ppr-wr": 240}
#: What each live page does against a SAME-WEEK baseline of `LIVE_WK1_MONDAY`.
LIVE_SURVIVORS = {"dst", "k"}                 # 32/32 and 33/36 clear 70%
LIVE_REFUSED = {"qb", "ppr-rb", "ppr-te", "ppr-wr"}


def _shaped(board, counts, scrape):
    """A board carrying exactly ``counts[page]`` rows per page, dated ``scrape``.

    Rows are cloned from the fixture's own per-page blocks, so every dtype
    upstream actually serves (an int64 id, all-NaN float columns where TEXT is
    declared) is still exercised; only the id and the scrape day are rewritten.
    """
    frames, next_id = [], 500000
    for page, n in counts.items():
        src = board[board["page"] == page]
        assert not src.empty, page
        reps = -(-n // len(src))
        block = pd.concat([src] * reps, ignore_index=True).head(n).copy()
        block["fantasypros_id"] = list(range(next_id, next_id + n))
        next_id += n
        frames.append(block)
    out = pd.concat(frames, ignore_index=True)
    out["scrape_date"] = scrape
    return out


def _pages_at(db, day):
    """``{page: rows}`` stored under one ``retrieved_as_of``."""
    return {str(r[0]): int(r[1]) for r in db.execute(
        "SELECT page, COUNT(*) FROM fp_weekly_ecr WHERE retrieved_as_of = ? GROUP BY page",
        (day,),
    )}


def test_a_capture_with_no_consensus_at_all_is_refused_on_the_first_pull(db, board):
    """THE ABSOLUTE ARM, checked even with nothing to compare against: rows
    present, `ecr` empty. This is the `players.CrosswalkCollapse` shape — the one
    a row COUNT cannot see — and it was measured live on `players` in item 3.1b:
    every id column emptied, every row still there, every crosswalk to zero, the
    run logged `ok`.

    Per page now, so the arm fires page by page; here every page is empty, so no
    page survives and the WHOLE capture fails rather than writing a healthy zero
    (item 3.1b — "wrote 0 rows" is never ok)."""
    _stub_schedule(db)
    emptied = board.copy()
    emptied["ecr"] = None
    with pytest.raises(fp_weekly.WeeklyEcrCollapse, match="NOT ONE") as exc:
        _ingest(db, emptied)
    assert "NO PAGE survived" in str(exc.value)
    assert db.execute("SELECT COUNT(*) FROM fp_weekly_ecr").fetchone()[0] == 0


def test_one_empty_page_is_refused_while_its_siblings_land(db, board):
    """THE ABSOLUTE ARM, PER PAGE. A page whose consensus is gone is refused on
    its own; the pages that still carry one are written, because they are
    published independently and there is no reason for them to fail together."""
    _stub_schedule(db)
    gutted = board.copy()
    gutted.loc[gutted["page"] == "ppr-wr", "ecr"] = None

    written, tally = _ingest(db, gutted, day=WK2_A)
    assert written == 30                             # 36 - the 6 ppr-wr rows
    assert set(_pages_at(db, WK2_A)) == fp_weekly.LEAGUE_PAGES - {"ppr-wr"}
    assert tally["refused"] == 6 and len(tally["refusals"]) == 1
    assert "'ppr-wr'" in tally["refusals"][0] and "NOT ONE" in tally["refusals"][0]


def test_a_truncated_same_week_re_scrape_is_refused_before_the_write(db, board):
    """THE COUNT ARM, and the case the fence exists for: a re-scrape of a week
    ALREADY STORED that came back truncated. Nothing is deleted — `select_as_of`
    resolves the newest retrieved version per key, so a thin capture of the same
    page and week shadows a good one merely by arriving later.

    Every page is cut to 1 of 6 (17%), so no page survives and the whole capture
    raises; the stored board must be untouched."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, WK2_A), day=WK2_A)
    thin = pd.concat([g.head(1) for _, g in board.groupby("page", sort=False)],
                     ignore_index=True)
    with pytest.raises(fp_weekly.WeeklyEcrCollapse, match="truncated re-scrape") as exc:
        _ingest(db, _restamp(thin, WK2_B), day=WK2_B)
    assert "NO PAGE survived" in str(exc.value)

    assert db.execute("SELECT COUNT(*) FROM fp_weekly_ecr").fetchone()[0] == 36
    assert {r[0] for r in db.execute(
        "SELECT DISTINCT retrieved_as_of FROM fp_weekly_ecr")} == {WK2_A}


def test_one_truncated_page_is_refused_while_the_rest_of_the_week_lands(db, board):
    """THE COUNT ARM, PER PAGE. Only `ppr-wr` is truncated; the other five pages
    are written, and the stored `ppr-wr` rows keep their own capture day."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, WK2_A), day=WK2_A)
    thin = pd.concat(
        [g.head(1) if page == "ppr-wr" else g for page, g in board.groupby("page", sort=False)],
        ignore_index=True,
    )
    written, tally = _ingest(db, _restamp(thin, WK2_B), day=WK2_B)

    assert written == 30
    assert set(_pages_at(db, WK2_B)) == fp_weekly.LEAGUE_PAGES - {"ppr-wr"}
    assert _pages_at(db, WK2_A)["ppr-wr"] == 6
    assert tally["refused"] == 1
    assert "'ppr-wr'" in tally["refusals"][0]


def test_a_page_that_vanished_is_reported_even_when_the_total_holds(db, board):
    """THE VANISHED-PAGE ARM, and the reason it exists separately: a scrape that
    published only some of its pages is invisible in the TOTAL as soon as the
    surviving pages grew. Here `dst` disappears and `qb` doubles, so the row
    count is FINE and the board is broken.

    Per page, this is no longer a refusal of anything — there are no `dst` rows
    to refuse and the stored ones cannot be shadowed by rows that do not exist —
    so the surviving pages are WRITTEN and the absence is REPORTED, which is what
    makes the run `partial` and names `dst`."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, WK2_A), day=WK2_A)

    without_dst = board[board["page"] != "dst"].copy()
    padded = board[board["page"] == "qb"].copy()
    padded["fantasypros_id"] = padded["fantasypros_id"] + 900000
    partial = pd.concat([without_dst, padded], ignore_index=True)
    partial = _restamp(partial, WK2_B)
    assert len(partial[partial["page"].isin(fp_weekly.LEAGUE_PAGES)]) >= 36

    written, tally = _ingest(db, partial, day=WK2_B)
    assert written == 36                         # 5 pages, qb doubled
    assert "dst" not in _pages_at(db, WK2_B)
    assert _pages_at(db, WK2_A)["dst"] == 6      # the stored page is untouched
    assert tally["refused"] == 0                 # nothing to refuse...
    assert len(tally["refusals"]) == 1           # ...and still a fence firing
    assert "'dst'" in tally["refusals"][0] and "ABSENT" in tally["refusals"][0]

    # AND IT IS A TRANSITION, NOT A LATCH (the item-3.8A lesson). The next
    # capture's baseline is the one WITHOUT `dst`, so the same headline does not
    # re-print every day for the rest of the week — which is how the report that
    # matters gets skimmed.
    again = _restamp(partial, "2026-09-17")      # still week 2
    _, tally2 = _ingest(db, again, day="2026-09-17")
    assert tally2["refusals"] == []


def test_an_emptied_page_is_refused_although_its_row_count_is_perfect(db, board):
    """THE VALUED-SHARE ARM. Every key present, most of the consensus gone. The
    count arm passes (6 of 6 rows) and only a share check sees it."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, WK2_A), day=WK2_A)

    gutted = _restamp(board.copy(), WK2_B)
    qb = gutted.index[gutted["page"] == "qb"]
    gutted.loc[qb[1:], "ecr"] = None             # 1 of 6 qb rows keeps a value
    written, tally = _ingest(db, gutted, day=WK2_B)

    assert written == 30
    assert "qb" not in _pages_at(db, WK2_B)
    assert tally["refused"] == 6
    assert "which is exactly why" in tally["refusals"][0]
    assert db.execute(
        "SELECT COUNT(*) FROM fp_weekly_ecr WHERE page = 'qb' AND ecr IS NOT NULL"
    ).fetchone()[0] == 6


def test_a_complete_page_is_never_refused_because_a_sibling_is_thin(db, board):
    """THE HEADLINE PIN, on the live 2026-09-15 shape, SAME WEEK so the floor
    actually bites: `dst` is 32 of 32 NFL teams and lands, while `ppr-wr` at
    116 against 240 is refused. The pre-3.14a whole-board floor refused all six
    pages here — including the one `ziggurat stream` ranks D/ST with."""
    _stub_schedule(db)
    _ingest(db, _shaped(board, LIVE_WK1_MONDAY, WK2_A), day=WK2_A)

    written, tally = _ingest(db, _shaped(board, LIVE_WK2_TUESDAY, WK2_B), day=WK2_B)

    assert set(_pages_at(db, WK2_B)) == LIVE_SURVIVORS
    assert _pages_at(db, WK2_B) == {"dst": 32, "k": 33}
    assert written == 65
    assert tally["refused"] == sum(LIVE_WK2_TUESDAY[p] for p in LIVE_REFUSED) == 291
    assert {p for p in LIVE_REFUSED if f"'{p}'" in "; ".join(tally["refusals"])} == LIVE_REFUSED
    assert not [p for p in LIVE_SURVIVORS if f"'{p}'" in "; ".join(tally["refusals"])]
    # And the refused pages' stored rows are exactly as they were.
    assert _pages_at(db, WK2_A) == LIVE_WK1_MONDAY


def test_a_thin_board_for_a_NEW_week_is_never_refused(db, board):
    """THE CROSS-WEEK DECISION, pinned as behaviour rather than left in prose.

    This is the live 2026-09-15 pull exactly: a week-2 board against the week-1
    capture stored the day before. The key `select_as_of` resolves on is
    `(fantasypros_id, page, scrape_date)` and `nfl_week` is a function of
    `scrape_date`, so the two captures occupy DISJOINT key spaces — a week-2 row
    cannot shadow a week-1 row. Refusing here protects nothing and loses a
    perishable capture that exists nowhere else once upstream rewrites the file.
    Every page lands, including the four that a same-week baseline would refuse
    (see the test above, which uses the identical numbers)."""
    _stub_schedule(db)
    _ingest(db, _shaped(board, LIVE_WK1_MONDAY, WK1_DAY), day=WK1_DAY)
    assert {r[0] for r in db.execute(
        "SELECT DISTINCT nfl_week FROM fp_weekly_ecr")} == {1}

    written, tally = _ingest(db, _shaped(board, LIVE_WK2_TUESDAY, WK2_A), day=WK2_A)

    assert written == sum(LIVE_WK2_TUESDAY.values()) == 356
    assert _pages_at(db, WK2_A) == LIVE_WK2_TUESDAY
    assert tally["refused"] == 0 and tally["refusals"] == []
    assert _pages_at(db, WK1_DAY) == LIVE_WK1_MONDAY
    assert {r[0] for r in db.execute(
        "SELECT DISTINCT nfl_week FROM fp_weekly_ecr")} == {1, 2}


def test_the_baseline_is_one_upstream_FILE_not_a_day_of_forced_pulls(db, board):
    """Two forced pulls on ONE day can land two different `scrape_date`s under a
    single `retrieved_as_of` — the PK carries both, so neither replaces the other
    — and a Tuesday morning pull plus an afternoon one to catch upstream's
    fill-in is exactly the habit item 3.14a's own finding invites.

    Summing them would make the baseline 72 rows instead of 36 and refuse the
    next day's perfectly healthy 36-row board (36 < floor 50). The baseline is
    the newest `scrape_date` within the newest capture day: ONE file."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, WK2_A), day=WK2_A)
    _ingest(db, _restamp(board, WK2_B), day=WK2_A)      # same DAY, second file
    assert len(_pages_at(db, WK2_A)) == 6
    assert sum(_pages_at(db, WK2_A).values()) == 72

    written, tally = _ingest(db, _restamp(board, "2026-09-17"), day="2026-09-17")
    assert written == 36
    assert tally["refusals"] == []


def test_a_healthy_bye_week_board_passes_the_floor(db, board):
    """THE GUARD MUST NOT CRY WOLF, which is what set the number. FantasyPros
    ranks the players who PLAY, and 2026 runs up to six teams on bye, so a
    healthy board legitimately shrinks ~19% week to week. A floor tight enough to
    catch that would fire on an ordinary Sunday — and a guard that cries wolf is
    how the report that matters gets ignored.

    Both captures sit in ONE week, so the floor is genuinely exercised: under a
    cross-week pair there would be no baseline and the test would pass for a
    reason that has nothing to do with the number."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, WK2_A), day=WK2_A)

    bye = pd.concat(
        [g.head(5) for _, g in board.groupby("page", sort=False)],   # 5 of 6 = 83%
        ignore_index=True,
    )
    written, tally = _ingest(db, _restamp(bye, WK2_B), day=WK2_B)
    assert written == 30
    assert tally["refusals"] == []


def test_the_floor_is_scoped_per_season_so_a_new_season_is_not_a_collapse(db, board):
    """A season's FIRST capture has nothing to compare against and must not be
    measured against a previous season's full board — the `fpecr` scoping lesson,
    where an unscoped count told the operator to re-download an undamaged file
    with a remedy that could never clear."""
    _stub_schedule(db)
    _ingest(db, board, day="2026-09-15")
    written, _ = _ingest(db, _restamp(board.head(6), "2027-09-15"), day="2027-09-15")
    assert written == 6


# ---------------------------------------------------------------- the registry


def test_the_source_is_registered_perishable_and_ahead_of_the_archives(db):
    spec = refresh.SOURCES_BY_NAME["fp_weekly_ecr"]
    assert spec.group == refresh.GROUP_DAILY
    assert spec.perishable is True            # rewritten TWICE DAILY upstream
    assert spec.interval_days == 1
    assert spec.phases == frozenset({refresh.PHASE_INSEASON})
    assert spec.needs_credentials is False
    assert spec.replaces_partition is False
    # needs_schedules is FALSE on purpose: the scrape_date is the knowledge time,
    # so a missing schedule costs the WEEK LABEL and not the rows — the opposite
    # of the six sources that would drop 100%. Pinned because flipping it True
    # would make `decide()` SKIP the whole capture on a day the label is all that
    # is at stake, and this source is perishable.
    assert spec.needs_schedules is False
    names = [s.name for s in refresh.SOURCES if s.group == refresh.GROUP_DAILY]
    assert names.index("fp_weekly_ecr") < names.index("fpecr")


def test_a_past_season_is_refused_and_force_cannot_reach_it(db):
    """FORWARD-ONLY, through BOTH doors — and which door answers is recorded here
    rather than assumed, because for THIS source it is not the obvious one.

    The pull takes no season (the board's own scrape_date decides), so a
    five-season backfill would run five identical scrapes of TODAY's board and
    log them under five different seasons. `select_backfill_sources` refuses that
    with the recorded reason. On `ingest run --season <past>` the refusal comes
    EARLIER, from the phase gate: a completed season is never `inseason` at
    today's date, and the phase answer is the more specific one (exactly the
    espn_ranks case decide() already documents). So the BACKFILL_EXCLUDED arm of
    `decide` is unreachable for an inseason-only source — the entry still earns
    its place at the backfill door, and neither door lets `--force` through."""
    _stub_schedule(db, season=2023, weeks={1: ("2023-09-07", "2023-09-11")})
    _stub_schedule(db)
    assert "fp_weekly_ecr" in refresh.BACKFILL_EXCLUDED

    with pytest.raises(refresh.BackfillRefused) as exc:
        refresh.select_backfill_sources(names=["fp_weekly_ecr"])
    assert str(exc.value).endswith(refresh.BACKFILL_EXCLUDED["fp_weekly_ecr"])

    decision = refresh.decide(
        db, refresh.SOURCES_BY_NAME["fp_weekly_ecr"], season=2023,
        today="2026-09-15", have_credentials=True, force=True,
    )
    assert decision.action == refresh.STATUS_SKIPPED
    assert "offseason" in decision.reason


def test_the_current_season_still_pulls_in_season(db):
    _stub_schedule(db)
    decision = refresh.decide(
        db, refresh.SOURCES_BY_NAME["fp_weekly_ecr"], season=2026,
        today="2026-09-15", have_credentials=False,
    )
    assert decision.action == "pull", decision.reason
    assert "week 2" in decision.scope and "schedules" in decision.scope


def test_the_orchestrator_lands_the_board_and_the_idp_filter_is_not_a_drop(db, monkeypatch):
    """END TO END through the REAL `run_ingest`, offline — and the claim it pins
    is the item-3.1b lesson, not the happy path.

    ~59% of this file is IDP pages (996 of 1,678 on the live scrape; 10 of 46 in
    the trimmed fixture). `run_ingest`'s drop ceiling is 20%, so if that filter
    were counted as a DROP the source would be `failed` on every single healthy
    run — which is exactly what happened to `adp_rankings` on item 3.1b's first
    live run, on a 35% "drop" that was almost entirely this same filter. The
    filter must reach the log as `filtered`, and the run must be `ok`."""
    _stub_schedule(db)
    # This test pins the DEFAULT authority (the schedule). `run_ingest` passes
    # no `environ`, so the real `week_page_enabled` would load the repo `.env` —
    # where the operator turned ZIGGURAT_FP_WEEK_PAGE on (D2b, 2026-09-05) — and
    # the pull would then fetch the LIVE FantasyPros page from inside the suite
    # and label the board from it (measured: the suite went red on 2026-09-07
    # for exactly that). Pin the setting, and forbid the request outright.
    monkeypatch.setattr(fp_weekly, "week_page_enabled", lambda environ=None: False)

    def _never(**kw):                          # pragma: no cover - must not run
        raise AssertionError("the FantasyPros page was fetched from inside the suite")

    monkeypatch.setattr(fp_weekly, "fetch_week_page", _never)
    # Today's bytes, re-dated to today: the CSV goes through the SAME parse the
    # live pull uses, so the fetch/parse/ingest seam is exercised whole.
    today = fp_weekly.read_fp_weekly(FIXTURE)
    today["scrape_date"] = "2026-09-15"
    monkeypatch.setattr(fp_weekly, "fetch_fp_weekly",
                        lambda **kw: today.to_csv(index=False).encode("utf-8"))
    summaries = refresh.run_ingest(
        db, sources=(refresh.SOURCES_BY_NAME["fp_weekly_ecr"],), season=2026,
        retrieved_as_of="2026-09-15", today="2026-09-15",
    )
    assert [s["status"] for s in summaries] == [refresh.STATUS_OK]
    assert summaries[0]["rows"] == 36 and summaries[0]["dropped"] == 0
    assert not refresh.run_failed(summaries)

    logged = db.execute(
        "SELECT status, rows_written, error FROM nfl_ingest_runs "
        "WHERE source = 'fp_weekly_ecr' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert logged["status"] == refresh.STATUS_OK and logged["rows_written"] == 36
    # The run's own record says which authority labelled the week — the one
    # derived value a reader of the log would want, and the thing `week_basis`
    # exists to make checkable rather than remembered.
    assert "week label from 'schedules'" in (logged["error"] or "")
    assert db.execute(
        "SELECT DISTINCT nfl_week FROM fp_weekly_ecr").fetchone()[0] == 2


def _run_board(db, monkeypatch, frame, *, day):
    """One `run_ingest` of `frame` through the REAL orchestrator, offline."""
    monkeypatch.setattr(fp_weekly, "week_page_enabled", lambda environ=None: False)

    def _never(**kw):                          # pragma: no cover - must not run
        raise AssertionError("the FantasyPros page was fetched from inside the suite")

    monkeypatch.setattr(fp_weekly, "fetch_week_page", _never)
    monkeypatch.setattr(fp_weekly, "fetch_fp_weekly",
                        lambda **kw: frame.to_csv(index=False).encode("utf-8"))
    return refresh.run_ingest(
        db, sources=(refresh.SOURCES_BY_NAME["fp_weekly_ecr"],), season=2026,
        retrieved_as_of=day, today=day, force=True,
    )


def test_a_refused_page_makes_the_run_partial_and_never_trips_the_drop_ceiling(
    db, board, monkeypatch
):
    """THE STATUS PIN (item 3.14a), on the live 2026-09-15 shape under a SAME-WEEK
    baseline: 291 of 356 rows refused = 82%, which is four times `run_ingest`'s
    20% drop ceiling.

    A fence refusal must NOT reach that ceiling. The ceiling's own sentence
    diagnoses "an unresolvable key (new team abbr? missing crosswalk?)" — the
    opposite of a fence that understood the rows perfectly and judged them — and
    a working fence reported as `failed` is how the word stops meaning anything.
    The verdict is `partial`, `dropped` stays 0, and the reason NAMES the pages so
    the operator can tell which half of the board landed."""
    _stub_schedule(db)
    _ingest(db, _shaped(board, LIVE_WK1_MONDAY, WK2_A), day=WK2_A)

    summaries = _run_board(db, monkeypatch, _shaped(board, LIVE_WK2_TUESDAY, WK2_B),
                           day=WK2_B)
    assert [s["status"] for s in summaries] == [refresh.STATUS_PARTIAL]
    assert summaries[0]["rows"] == 65 and summaries[0]["dropped"] == 0
    assert not refresh.run_failed(summaries)

    logged = db.execute(
        "SELECT status, rows_written, rows_dropped, error FROM nfl_ingest_runs "
        "WHERE source = 'fp_weekly_ecr' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert logged["status"] == refresh.STATUS_PARTIAL
    assert logged["rows_written"] == 65 and logged["rows_dropped"] == 0
    assert "REFUSED 4 page(s)" in logged["error"]
    for page in LIVE_REFUSED:
        assert f"'{page}'" in logged["error"]

    # The run SUMMARY LINE names them too — that is what a systemd unit logs.
    line = refresh.format_run(summaries)
    assert "REFUSED" in line and "ppr-wr" in line
    # ...and so does `ingest status`, which is where the operator looks the next
    # morning. `partial` is not a PROBLEM status, so without its own label the
    # refusal would sit inside a report that calls the source fine.
    report = refresh.format_status(db, season=2026, today=WK2_B)
    assert "REFUSED" in report and "fp_weekly_ecr" in report


def test_a_capture_whose_every_page_is_refused_is_failed_not_a_silent_zero(
    db, board, monkeypatch
):
    """"Wrote 0 rows" is never `ok` (item 3.1b), and it is not `empty` either —
    `empty` means upstream returned nothing, which is a completely different
    investigation. A fence that refused everything is a FAILED run naming each
    page, and it must not anchor the interval gate."""
    _stub_schedule(db)
    _ingest(db, _restamp(board, WK2_A), day=WK2_A)
    thin = pd.concat([g.head(1) for _, g in board.groupby("page", sort=False)],
                     ignore_index=True)

    summaries = _run_board(db, monkeypatch, _restamp(thin, WK2_B), day=WK2_B)
    assert [s["status"] for s in summaries] == [refresh.STATUS_FAILED]
    assert refresh.run_failed(summaries)
    assert "NO PAGE survived" in summaries[0]["reason"]
    assert db.execute("SELECT COUNT(*) FROM fp_weekly_ecr").fetchone()[0] == 36
    assert refresh.STATUS_FAILED not in refresh._ANCHOR_STATUSES


def test_the_preseason_is_skipped_because_no_weekly_board_exists_yet(db):
    """INSEASON only. A weekly board is not published between seasons, and a
    source that records `upstream_absent` (or worse, stores a board with no week)
    for five preseason days is the wolf-cry this module is designed against."""
    _stub_schedule(db)
    decision = refresh.decide(
        db, refresh.SOURCES_BY_NAME["fp_weekly_ecr"], season=2026,
        today=SCRAPE, have_credentials=False,
    )
    assert decision.action == refresh.STATUS_SKIPPED
    assert "preseason" in decision.reason


def test_the_adp_rankings_note_no_longer_claims_a_missed_day_is_a_lost_scrape(db):
    """THE NOTE CORRECTION this unit also owed (recon §0.4). The registry note
    used to say FantasyPros serves today's scrape only; measured, the file it
    reads is rewritten FRIDAYS ONLY and consecutive daily pulls are identical on
    0 of 517 `ro` rows. The flag deliberately STAYS True — it reports on this
    TABLE, which nothing re-populates — and the note now says both halves."""
    note = refresh.SOURCES_BY_NAME["adp_rankings"].notes
    assert "Friday" in note and "0 of 517" in note
    assert "db_fpecr" in note                 # where the fact survives
    assert refresh.SOURCES_BY_NAME["adp_rankings"].perishable is True
    assert refresh.SOURCES_BY_NAME["adp_rankings"].interval_days == 1


# ------------------------------------------------------------ the scope fence


def _uses_fp_weekly(text: str) -> bool:
    """Does this module IMPORT or QUERY the weekly-ECR source?

    Deliberately narrower than "mentions": an import statement, an attribute
    access on the module, or the table by name. A fence that fired on a docstring
    is a fence somebody turns off within a week (the lesson B4 paid for on
    `core/candidates.py`, whose docstring legitimately names `ff_opportunity`)."""
    return bool(
        re.search(r"^\s*(?:from|import)\b[^\n]*\bfp_weekly\b", text, re.M)
        or re.search(r"\bfp_weekly\s*\.\s*\w", text)
        or re.search(r"\bfp_weekly_ecr\b", text)
    )


#: The ONE core module item 3.14 (2026-09-15) authorised to read this capture.
#: Week 1 is over; the fence is no longer "nobody", it is "this one, for this
#: page". An allowlist of one is still a fence — the failure it exists to catch
#: is the SECOND module reaching for the same too-useful number without a plan
#: item and a measurement behind it.
_WEEKLY_ECR_READERS = {"streaming.py"}


def test_only_the_authorised_core_module_reads_the_weekly_ecr_capture():
    """SCOPE FENCE, narrowed rather than deleted (item 4.2b -> item 3.14).

    4.2b's fence said NO core module may read this table, because the first live
    Tuesday was recording an undisturbed baseline of the shipped decision path and
    a same-week market rank is exactly the kind of number that looks too useful to
    leave alone. Week 1 is graded and item 3.14 spent a measurement to earn the
    read (Spearman +0.2701 vs the composite's +0.1300 on 2021-23), so
    `core/streaming.py` is allowed in — and nothing else is.

    Rule 2 still rides along and is checked separately below: `r2p_pts` and
    `start_sit_grade` are FantasyPros' own projection and letter grade, so a core
    module reaching for THOSE would be pricing a decision in another scoring
    system's currency, whatever the allowlist says."""
    core = Path(refresh.__file__).resolve().parents[2] / "core"
    offenders = [path.name for path in sorted(core.rglob("*.py"))
                 if _uses_fp_weekly(path.read_text(encoding="utf-8"))
                 and path.name not in _WEEKLY_ECR_READERS]
    assert offenders == [], (
        "only `core/streaming.py` may read the weekly-ECR capture (item 3.14); "
        f"these core modules also reach for it: {offenders}"
    )
    # The allowlist is not a rubber stamp: the authorised module must ACTUALLY be
    # the one reading it, or this test has quietly stopped fencing anything.
    assert (core / "streaming.py").exists()
    assert _uses_fp_weekly((core / "streaming.py").read_text(encoding="utf-8"))
    # Teeth, both directions: a prose mention must not trip it, an import or a
    # query must.
    assert not _uses_fp_weekly("the fp_weekly capture is deferred to 4.2c")
    assert not _uses_fp_weekly("# same-week ECR is captured, and nothing reads it")
    assert _uses_fp_weekly("from ziggurat.data.nfl import fp_weekly")
    assert _uses_fp_weekly("rows = conn.execute('SELECT * FROM fp_weekly_ecr')")


def test_the_authorised_reader_never_touches_fantasypros_own_points():
    """RULE 2, where the allowlist could have let it in. `r2p_pts` is
    FantasyPros' projected points and `start_sit_grade` their letter grade — both
    in FANTASYPROS' scoring, not the house's. `core/streaming.py` is allowed to
    read this table for an ORDERING (a rank is scoring-system-free); it is not
    allowed to read a number out of it and put it beside a house-scored one."""
    core = Path(refresh.__file__).resolve().parents[2] / "core"
    src = (core / "streaming.py").read_text(encoding="utf-8")
    for banned in ("r2p_pts", "start_sit_grade", "player_owned_avg"):
        assert banned not in src, (
            f"core/streaming.py reads {banned!r} out of the weekly board — that is "
            f"FantasyPros' own number in FantasyPros' scoring (Rule 2)."
        )
    # And it reads the D/ST page only: the rho and the paired margin were measured
    # on `dst`, and a ranker swapped in on another position's evidence is the move
    # Rule 6 exists to stop.
    assert 'MARKET_PAGE = "dst"' in src


def test_a_backfill_must_leave_the_capture_byte_identical(db, board):
    """The `adp_rankings` fence, applied to the other perishable season-agnostic
    capture. A backfill never pulls this source — which is exactly why a write
    here would be a defect — and the fingerprint is CONTENT, not cardinality: an
    INSERT OR REPLACE at the same (key, stamp) leaves a count identical while
    replacing every value, which is verbatim the 3.1b `players` failure."""
    _stub_schedule(db)
    _ingest(db, board, day=SCRAPE)
    before = refresh.protected_partitions(db, protect_season=2026)
    assert "fp_weekly_ecr" in before
    assert refresh.protected_partitions(db, protect_season=2026) == before

    db.execute("UPDATE fp_weekly_ecr SET ecr = ecr + 1 WHERE page = 'qb'")
    db.commit()
    after = refresh.protected_partitions(db, protect_season=2026)
    assert after["fp_weekly_ecr"] != before["fp_weekly_ecr"]


def test_the_capture_is_never_merged_into_the_backtest_panel(db, board):
    """`fpecr_panel` is a backtest input with a verified immutability story; this
    is a live number that moves during the day. Sharing a key space would let
    `select_as_of`'s per-key MAX answer every backtest read with today's board.
    Two tables, and this test is what keeps them two."""
    _stub_schedule(db)
    _ingest(db, board, day=SCRAPE)
    assert db.execute("SELECT COUNT(*) FROM fpecr_panel").fetchone()[0] == 0
    assert refresh._BACKFILL_TABLES.get("fpecr") == "fpecr_panel"
    assert "fp_weekly_ecr" not in refresh._BACKFILL_TABLES


# ------------------------------------------------------------------ network


def test_the_download_is_bounded_by_a_whole_transfer_budget():
    """`net.HTTP_TIMEOUT` bounds each socket READ, not the transfer: an upstream
    that trickles bytes just under the timeout would hold the daily unit for as
    long as it likes, and every perishable source behind this one in the registry
    would lose its observation for the day (item 4.1 audit, OPS-3)."""
    class _Trickle:
        def read(self, _n):
            return b"x" * 1024

    with pytest.raises(TimeoutError, match="budget"):
        fp_weekly._read_bounded(_Trickle(), url="u", budget_s=0.0,
                                max_bytes=1 << 30)


def test_the_download_is_bounded_by_a_size_cap():
    class _Firehose:
        def read(self, _n):
            return b"x" * 4096

    with pytest.raises(ValueError, match="not the shape"):
        fp_weekly._read_bounded(_Firehose(), url="u", budget_s=60.0, max_bytes=1024)


def test_the_reader_takes_bytes_and_paths_alike(board):
    """One parse seam, so the committed fixture is read the same way the live
    bytes are — a hand-built frame would not exercise the dtypes upstream serves."""
    from_bytes = fp_weekly.read_fp_weekly(FIXTURE.read_bytes())
    assert list(from_bytes.columns) == list(board.columns)
    assert len(from_bytes) == len(board)
    assert from_bytes["fantasypros_id"].dtype.kind == "i"   # an int64 id, coerced later


def test_the_fixture_is_the_public_nfl_board_only(board):
    """Rule 5, asserted rather than assumed: the committed fixture carries NFL
    player rows and nothing else — no league member, no rival team name, no id
    that belongs to this league."""
    assert set(board["scrape_date"].unique()) == {SCRAPE}
    assert set(board["page"].unique()) <= (
        fp_weekly.LEAGUE_PAGES | {"db", "dl", "lb"}
    )
    assert board["team"].str.len().max() <= 3
    text = FIXTURE.read_text(encoding="utf-8")
    assert "SWID" not in text and "espn.com" not in text


def test_the_declared_key_matches_the_migrated_schema(db):
    """The seam `base.upsert` enforces at ingest, asserted directly against the
    schema so a future migration that widens the PK fails here rather than at
    07:20 on a Tuesday."""
    info: list[sqlite3.Row] = db.execute(
        "PRAGMA table_info(fp_weekly_ecr)").fetchall()
    declared = tuple(name for _, name in sorted((r[5], r[1]) for r in info if r[5]))
    assert declared == fp_weekly._PK_COLS


# ------------------------------------------ the content authority (2026-09-30)
#
# THE DEFECT, three Tuesdays in four: DynastyProcess had not rewritten the CSV
# since Monday, the FantasyPros page had already flipped to the next week, and
# the pull filed Monday's board under the page's week. Measured on the live
# 09-29 capture: 100% of every page's opponents were week 3's, 0% week 4's.
# `stream` then printed a week-3 market rank beside a week-4 opponent, and the
# next day's genuine week-4 TE/WR pages were refused against that mislabelled
# baseline. Every test below builds a schedule in which weeks 3 and 4 pair the
# SAME sixteen teams differently, so only the opponents can tell them apart.

_TEAMS = ["BUF", "MIA", "NE", "NYJ", "BAL", "CIN", "CLE", "PIT",
          "HOU", "IND", "JAX", "TEN", "DEN", "KC", "LV", "LAC"]
#: week -> list of (away, home). Week 3 pairs neighbours; week 4 pairs across.
_MATCHUPS = {
    3: [(_TEAMS[i], _TEAMS[i + 1]) for i in range(0, 16, 2)],
    4: [(_TEAMS[i], _TEAMS[i + 2]) for i in (0, 1, 4, 5, 8, 9, 12, 13)],
}
_GAMEDAYS = {3: ("2026-09-24", "2026-09-28"), 4: ("2026-10-01", "2026-10-05")}
MON_WK3, TUE_WK4, WED_WK4 = "2026-09-28", "2026-09-29", "2026-09-30"


def _stub_matchups(db, matchups=None, *, retrieved="2026-08-01"):
    for week, games in (matchups or _MATCHUPS).items():
        first, last = _GAMEDAYS[week]
        for i, (away, home) in enumerate(games):
            db.execute(
                "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
                "home_team, away_team, retrieved_as_of, knowable_as_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"2026_{week:02d}_{away}_{home}", 2026, week, "REG",
                 first if i == 0 else last, home, away, retrieved, retrieved),
            )
    db.commit()


def _opponent(team, week):
    for away, home in _MATCHUPS[week]:
        if team == away:
            return home
        if team == home:
            return away
    raise KeyError(team)


def _board_for(board, week, scrape, counts=None, *, first_id=700000):
    """A board whose every row faces its WEEK-``week`` opponent, dated ``scrape``."""
    out = _shaped(board, counts or {p: 16 for p in sorted(fp_weekly.LEAGUE_PAGES)}, scrape)
    out["fantasypros_id"] = list(range(first_id, first_id + len(out)))
    teams = [_TEAMS[i % 16] for i in range(len(out))]
    out["team"] = teams
    out["player_opponent_id"] = [_opponent(t, week) for t in teams]
    return out


def _labels(db, day=None):
    sql = "SELECT DISTINCT page, nfl_week, week_basis FROM fp_weekly_ecr"
    rows = db.execute(sql + (" WHERE retrieved_as_of = ?" if day else ""),
                      (day,) if day else ()).fetchall()
    return {(r[0], r[1], r[2]) for r in rows}


def test_the_rows_own_opponents_decide_the_week_and_beat_the_page(db, board):
    """THE DEFECT ITSELF. The page says week 4; the file is Monday's week-3 board.
    Every page must be filed as week 3, and the run log must say it overrode."""
    _stub_matchups(db)
    _written, tally = _ingest(db, _board_for(board, 3, MON_WK3), day=TUE_WK4,
                              page_week=(4, "1790701285"))

    assert _labels(db) == {(p, 3, fp_weekly.CONTENT_BASIS) for p in fp_weekly.LEAGUE_PAGES}
    text = " | ".join(tally["notes"])
    assert "ranks week 3" in text
    assert "OVERRODE the FantasyPros page's week 4" in text
    assert "OLDER than the page" in text
    # The override LEADS the note, so `ingest status`'s 220-char cut keeps it.
    [note] = [n for n in tally["notes"] if "OVERRODE" in n]
    assert note.index("OVERRODE") < 60
    # A provisional basis that decided nothing is not narrated as if it had.
    assert not any("week label from 'fantasypros_page'" in n for n in tally["notes"])


def test_a_re_pull_of_the_same_file_never_relabels_it(db, board):
    """The second half of the defect: the label was decided per PULL, so a later
    retrieval of the SAME scrape_date carrying a flipped page rewrote the week
    that every later read resolved to. Content is the same on every pull."""
    _stub_matchups(db)
    monday = _board_for(board, 3, MON_WK3)
    _ingest(db, monday, day=MON_WK3, page_week=(3, "a"))
    _ingest(db, monday, day=TUE_WK4, page_week=(4, "b"))

    rows = fp_weekly.get_fp_weekly_ecr(db, as_of=TUE_WK4, season=2026)
    assert rows and {r["nfl_week"] for r in rows} == {3}
    assert fp_weekly.get_fp_weekly_ecr(db, as_of=TUE_WK4, season=2026, nfl_week=4) == []


def test_a_genuine_new_week_is_not_refused_against_last_weeks_file(db, board):
    """The 2026-09-30 refusal, reproduced and closed. Tuesday stored Monday's
    week-3 file (page said 4); Wednesday's genuine week-4 file arrives with thin
    TE/WR pages. With the old label the TE/WR pages were compared against the
    mislabelled 'week 4' baseline and refused; now there is no week-4 baseline."""
    _stub_matchups(db)
    full = {p: 48 for p in sorted(fp_weekly.LEAGUE_PAGES)}
    _ingest(db, _board_for(board, 3, MON_WK3, full), day=TUE_WK4, page_week=(4, "b"))

    thin = dict(full, **{"ppr-te": 16, "ppr-wr": 16})
    written, tally = _ingest(db, _board_for(board, 4, WED_WK4, thin, first_id=800000),
                             day=WED_WK4, page_week=(4, "c"))
    assert tally["refused"] == 0
    assert set(_pages_at(db, WED_WK4)) == fp_weekly.LEAGUE_PAGES
    assert _labels(db, WED_WK4) == {(p, 4, fp_weekly.CONTENT_BASIS)
                                    for p in fp_weekly.LEAGUE_PAGES}
    assert written == sum(thin.values())


def test_the_same_week_floor_still_bites_once_the_label_is_right(db, board):
    """The fence is not loosened, only pointed at the right week: a truncated
    re-scrape of the SAME week is still refused."""
    _stub_matchups(db)
    full = {p: 48 for p in sorted(fp_weekly.LEAGUE_PAGES)}
    _ingest(db, _board_for(board, 4, TUE_WK4, full), day=TUE_WK4)
    thin = dict(full, **{"ppr-te": 16})
    _written, tally = _ingest(db, _board_for(board, 4, WED_WK4, thin, first_id=900000),
                              day=WED_WK4)
    assert tally["refused"] == 16
    assert "'ppr-te' (week 4)" in tally["refusals"][0]


def test_a_thin_page_borrows_its_files_verdict(db, board):
    """A page with too few rows to decide (a one-team page on a bye-heavy week)
    takes the week its own FILE decided: the pages of one file are one scrape."""
    _stub_matchups(db)
    counts = {p: 16 for p in sorted(fp_weekly.LEAGUE_PAGES)}
    counts["k"] = 3
    _ingest(db, _board_for(board, 3, MON_WK3, counts), day=TUE_WK4, page_week=(4, "b"))
    assert ("k", 3, fp_weekly.CONTENT_BASIS) in _labels(db)


def test_a_page_whose_opponents_are_mixed_keeps_the_pull_time_label_and_says_so(db, board):
    """Enough rows to decide, but half week 3 and half week 4: NOT decisive, and
    NOT papered over with the file's answer. The pull-time label stands, and the
    run log names the page the content could not decide."""
    _stub_matchups(db)
    frame = _board_for(board, 3, MON_WK3)
    mixed = frame["page"] == "qb"
    qb_teams = frame.loc[mixed, "team"].tolist()
    frame.loc[mixed, "player_opponent_id"] = [
        _opponent(t, 4 if i % 2 else 3) for i, t in enumerate(qb_teams)]
    _written, tally = _ingest(db, frame, day=TUE_WK4, page_week=(4, "b"))

    assert ("qb", 4, "fantasypros_page") in _labels(db)
    assert ("dst", 3, fp_weekly.CONTENT_BASIS) in _labels(db)
    assert any("did NOT decide" in n and "qb" in n for n in tally["notes"])


def test_infer_week_ignores_byes_and_unknown_codes_and_normalises_aliases(db):
    _stub_matchups(db)
    pairs = fp_weekly.schedule_pairs(db, 2026)
    rows = [{"team": t, "player_opponent_id": _opponent(t, 3)} for t in _TEAMS]
    rows += [{"team": "BUF", "player_opponent_id": "BYE"},
             {"team": "XXX", "player_opponent_id": "BUF"},
             {"team": "BUF", "player_opponent_id": None}]
    verdict = fp_weekly.infer_week_from_opponents(rows, pairs)
    assert verdict["week"] == 3
    assert (verdict["matched"], verdict["considered"]) == (16, 16)
    assert verdict["runner_up_matched"] == 0


def test_a_schedule_with_no_matching_week_decides_nothing(db, board):
    """No ingested schedule at all: the content authority is silent and the
    existing rule answers, exactly as before."""
    written, _ = _ingest(db, board)
    assert written == 36
    assert {(w, b) for _p, w, b in _labels(db)} == {(None, "unknown")}


def test_the_audit_names_stored_mislabels_and_the_repair_touches_only_labels(db, board):
    """The stored damage from before this fix: rows written with the page's week.
    Simulated by writing them directly, as the pre-fix code did."""
    _stub_matchups(db)
    _ingest(db, _board_for(board, 3, MON_WK3), day=TUE_WK4, page_week=(4, "b"))
    db.execute("UPDATE fp_weekly_ecr SET nfl_week = 4, week_basis = 'fantasypros_page'")
    db.commit()
    before = db.execute(
        "SELECT fantasypros_id, page, ecr, rank FROM fp_weekly_ecr ORDER BY 1, 2").fetchall()

    found = fp_weekly.audit_stored_week_labels(db, season=2026)
    assert {m["page"] for m in found} == fp_weekly.LEAGUE_PAGES
    assert {(tuple(m["stored_week"]), m["content_week"]) for m in found} == {((4,), 3)}
    report = fp_weekly.format_label_audit(found, season=2026)
    assert "Nothing changed" in report and "Back up" in report

    changed = fp_weekly.repair_stored_week_labels(db, found)
    assert changed == 16 * len(fp_weekly.LEAGUE_PAGES)
    assert _labels(db) == {(p, 3, fp_weekly.CONTENT_BASIS) for p in fp_weekly.LEAGUE_PAGES}
    assert db.execute(
        "SELECT fantasypros_id, page, ecr, rank FROM fp_weekly_ecr ORDER BY 1, 2"
    ).fetchall() == before
    assert fp_weekly.audit_stored_week_labels(db, season=2026) == []


def test_the_audit_never_reports_a_capture_its_content_cannot_decide(db, board):
    """The audit names what the rows PROVE is wrong, not what they fail to confirm."""
    _ingest(db, board)                       # no schedule: nothing is decisive
    assert fp_weekly.audit_stored_week_labels(db, season=2026) == []


def test_the_cli_audit_is_read_only_by_default(tmp_path, board):
    from typer.testing import CliRunner

    from ziggurat.cli.main import app
    from ziggurat.data.store import apply_schema, connect

    path = tmp_path / "z.sqlite"
    conn = connect(str(path))
    apply_schema(conn)
    _stub_matchups(conn)
    _ingest(conn, _board_for(board, 3, MON_WK3), day=TUE_WK4)
    conn.execute("UPDATE fp_weekly_ecr SET nfl_week = 4, week_basis = 'fantasypros_page'")
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "fp-weekly-labels", "--season", "2026",
                                 "--path", str(path)])
    assert result.exit_code == 0, result.output
    assert "Nothing changed" in result.output
    conn = connect(str(path))
    assert {r[0] for r in conn.execute("SELECT DISTINCT nfl_week FROM fp_weekly_ecr")} == {4}
    conn.close()

    result = runner.invoke(app, ["ingest", "fp-weekly-labels", "--season", "2026",
                                 "--repair", "--path", str(path)])
    assert result.exit_code == 0, result.output
    assert "REPAIRED" in result.output
    conn = connect(str(path))
    assert {r[0] for r in conn.execute("SELECT DISTINCT nfl_week FROM fp_weekly_ecr")} == {3}
    conn.close()


def test_the_content_pass_runs_BEFORE_the_fence(db, board):
    """The ordering, pinned (item 3.14b review). A MONDAY pull, page authority
    off, so the schedule clock says week 3 — but upstream has already published
    week 4's board, with TE/WR still thin. Fence-first would compare those pages
    against the stored week-3 file and refuse them; content-first files them as
    week 4, where there is nothing to shadow."""
    _stub_matchups(db)
    full = {p: 48 for p in sorted(fp_weekly.LEAGUE_PAGES)}
    _ingest(db, _board_for(board, 3, "2026-09-24", full), day="2026-09-24")

    thin = dict(full, **{"ppr-te": 16, "ppr-wr": 16})
    written, tally = _ingest(db, _board_for(board, 4, MON_WK3, thin, first_id=800000),
                             day=MON_WK3)
    assert tally["refused"] == 0
    assert written == sum(thin.values())
    assert _labels(db, MON_WK3) == {(p, 4, fp_weekly.CONTENT_BASIS)
                                    for p in fp_weekly.LEAGUE_PAGES}
    text = " | ".join(tally["notes"])
    assert "schedule-clock week 3" in text
    assert "OLDER" not in text                 # the cause is stated per basis


def test_an_unlabelled_capture_is_not_said_to_be_older_than_a_page(db, board):
    """No page was read and the schedule could not label it: the override note
    must say exactly that, not the stock 'older than the page' sentence."""
    _stub_matchups(db)
    _written, tally = _ingest(db, _board_for(board, 3, "2026-09-20"), day="2026-09-20")
    assert _labels(db) == {(p, 3, fp_weekly.CONTENT_BASIS) for p in fp_weekly.LEAGUE_PAGES}
    text = " | ".join(tally["notes"])
    assert "an unlabelled week" in text and "than the page" not in text


def test_a_thin_page_that_CONTRADICTS_its_file_does_not_borrow(db, board):
    """A three-row page whose every row faces a WEEK-4 opponent, inside a week-3
    file: too thin to decide on its own, and its rows say the file's week is
    wrong for it — so it keeps its pull-time label rather than being stamped 3."""
    _stub_matchups(db)
    counts = {p: 16 for p in sorted(fp_weekly.LEAGUE_PAGES)}
    counts["k"] = 3
    frame = _board_for(board, 3, MON_WK3, counts)
    k_rows = frame["page"] == "k"
    frame.loc[k_rows, "player_opponent_id"] = [
        _opponent(t, 4) for t in frame.loc[k_rows, "team"]]
    _written, tally = _ingest(db, frame, day=TUE_WK4, page_week=(4, "b"))

    assert ("k", 4, "fantasypros_page") in _labels(db)
    assert ("dst", 3, fp_weekly.CONTENT_BASIS) in _labels(db)
    assert any("did NOT decide" in n and "k (" in n for n in tally["notes"])


def test_the_runner_up_rule_refuses_a_week_the_rows_cannot_tell_apart():
    """Two weeks with the SAME games (a rematch-heavy late season, taken to the
    limit): every row matches both, so nothing is decisive."""
    games = frozenset({("BUF", "MIA"), ("MIA", "BUF"), ("NE", "NYJ"), ("NYJ", "NE")})
    pairs = {3: games, 5: games}
    rows = [{"team": t, "player_opponent_id": o} for t, o in sorted(games)] * 3
    verdict = fp_weekly.infer_week_from_opponents(rows, pairs)
    assert verdict["week"] is None
    assert verdict["matched"] == verdict["runner_up_matched"] == 12


def test_team_aliases_are_normalised_on_both_sides():
    """FantasyPros writes JAC and LAR; nflverse writes JAX and LA."""
    pairs = {3: frozenset({("JAX", "LA"), ("LA", "JAX")})}
    rows = [{"team": "JAC", "player_opponent_id": "LAR"},
            {"team": "LAR", "player_opponent_id": "JAC"}] * 4
    verdict = fp_weekly.infer_week_from_opponents(rows, pairs)
    assert (verdict["week"], verdict["matched"], verdict["considered"]) == (3, 8, 8)


def test_the_label_reads_the_seasons_NEWEST_schedule_snapshot(db, board):
    """An older schedule snapshot that paired week 3 differently must not vote:
    nflverse re-issues the whole season file, so its newest snapshot is the
    calendar, and a moved game gets a new game_id rather than a new row."""
    stale = {3: _MATCHUPS[4], 4: _MATCHUPS[3]}          # swapped, and older
    _stub_matchups(db, stale, retrieved="2026-07-01")
    _stub_matchups(db, retrieved="2026-08-01")
    assert fp_weekly.schedule_pairs(db, 2026)[3] == frozenset(
        p for away, home in _MATCHUPS[3] for p in ((away, home), (home, away)))
    _ingest(db, _board_for(board, 3, MON_WK3), day=TUE_WK4, page_week=(4, "b"))
    assert {w for _p, w, _b in _labels(db)} == {3}


def test_a_file_the_rows_cannot_check_says_so(db, board):
    """No schedule ingested: the content authority is blind, and the run log
    says the relabel guarantee lapsed for those pages rather than going quiet."""
    _written, tally = _ingest(db, board)
    assert any("could not check the week label" in n for n in tally["notes"])
