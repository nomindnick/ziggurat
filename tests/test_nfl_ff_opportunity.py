"""Capture + leakage tests for the ffverse ff_opportunity panel (item 4.2b, 015).

THE FIXTURE IS REAL UPSTREAM DATA, and that is deliberate.
``tests/fixtures/nfl/ffopp_weekly.parquet`` is weeks 1-2 of the real
``ep_weekly_2025.parquet`` — 662 rows, ALL 159 upstream columns, NFL player names
only (Rule 5: no league member, no rival team) — and
``tests/fixtures/nfl/ffopp_schedules.parquet`` is the 32 matching ``schedules``
rows with their REAL gamedays. Two properties this source's design rests on are
only checkable against the real frame:

* the gameday stamp resolves for EVERY row (285/285 game_ids on the full season),
  and week 1 alone spans FOUR distinct gamedays — so a stamp taken from the
  file's publish timestamp instead of the row's own game would collapse four
  knowledge times into one;
* 25 of 159 columns reach SQLite. A hand-built 25-column fixture could not tell
  a lean projection from an ingester that stores whatever it is handed.

WHAT THE FIXTURE CANNOT CATCH (item 3.1b's lesson, restated because this source
is the one where it bites hardest): it is FROZEN. Upstream rewrites the live
season's asset several times a week, and no committed fixture will ever notice a
column being renamed, a model being bumped, or the release tag moving. That is
what ``_REQUIRED`` (fails loudly), ``ModelVersionChanged`` (refuses) and the
dated lossless mirror (keeps the bytes) are for.
"""

import re
import shutil
import sqlite3
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from ziggurat.data.nfl import base, ff_opportunity, refresh

FIXTURE_SEASON = 2025
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "nfl"


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def ffopp_frame(nfl_fixture):
    return nfl_fixture("ffopp_weekly")


@pytest.fixture()
def ffopp_db(db, nfl_fixture):
    """``db`` with the 32 real schedule rows the fixture's game_ids need."""
    sched = nfl_fixture("ffopp_schedules")
    db.executemany(
        "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, gameday, "
        "weekday, gametime, away_team, home_team, retrieved_as_of, knowable_as_of) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (r.game_id, int(r.season), int(r.week), r.game_type, r.gameday, r.weekday,
             r.gametime, r.away_team, r.home_team, "2025-08-01", "2025-08-01")
            for r in sched.itertuples()
        ],
    )
    db.commit()
    return db


def _ingest(conn, frame, *, day, **kw):
    kw.setdefault("model_version", ff_opportunity.EXPECTED_MODEL_VERSION)
    return ff_opportunity.ingest_ff_opportunity(
        conn, frame, retrieved_as_of=day, season=FIXTURE_SEASON, **kw
    )


def _gamedays(nfl_fixture) -> dict[str, str]:
    sched = nfl_fixture("ffopp_schedules")
    return dict(zip(sched["game_id"], sched["gameday"], strict=True))


# ------------------------------------------------------------------ shape


def test_the_fixture_is_the_real_upstream_shape(ffopp_frame):
    """A guard on the fixture itself: if someone regenerates it from a 25-column
    slice, the lean-projection test below silently stops testing anything."""
    assert len(ffopp_frame.columns) == 159
    assert set(ff_opportunity._REQUIRED) <= set(ffopp_frame.columns)
    assert len(ffopp_frame) == 662
    assert ffopp_frame["player_id"].isna().sum() == 41   # unattributed plays


def test_only_the_lean_column_set_reaches_sqlite(ffopp_db, ffopp_frame):
    """159 columns in, 25 stored (+ provenance + stamps). The 134 that are dropped
    are not lost — pull_ff_opportunity keeps the whole parquet as a dated mirror —
    but they must not silently arrive in the table either, because the size
    argument for the lean set (963 B/row vs 196 B/row measured) is the reason the
    daily capture is affordable at all."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    stored = {r[1] for r in ffopp_db.execute("PRAGMA table_info(ffopp_weekly)")}
    assert stored == set(ff_opportunity._REQUIRED) | {
        "asset_updated_at", "source_timestamp", "model_version",
        "retrieved_as_of", "knowable_as_of",
    }
    # And nothing widened the table by accident: 25 + 3 provenance + 2 stamps.
    assert len(stored) == 30


def test_a_missing_upstream_column_fails_loudly(ffopp_db, ffopp_frame):
    """The item-1.4 contract. Upstream renaming `rec_touchdown_exp` must not
    quietly store a table of NULLs in the column the whole source exists for."""
    with pytest.raises(ValueError, match="rec_touchdown_exp"):
        _ingest(ffopp_db, ffopp_frame.drop(columns=["rec_touchdown_exp"]), day="2025-09-16")


# ------------------------------------------------------- knowable_as_of stamp


def test_every_row_is_stamped_with_its_own_gameday(ffopp_db, ffopp_frame, nfl_fixture):
    """THE stamp test, and the reason `needs_schedules=True`.

    Measured on the full 2025 file: 285/285 distinct game_ids and 6,054/6,054 rows
    resolve. Here: every stored row's knowable_as_of is ITS OWN game's gameday,
    nothing is dropped for want of one, and week 1 alone carries FOUR distinct
    knowledge times. Stamping the file's publish timestamp instead — one value for
    the whole season — would fold those four into one date in the future, and the
    season would read empty at every earlier as_of."""
    written = _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    gamedays = _gamedays(nfl_fixture)
    rows = ffopp_db.execute(
        "SELECT game_id, week, knowable_as_of FROM ffopp_weekly"
    ).fetchall()
    assert len(rows) == written == 662 - 41
    assert all(r["knowable_as_of"] == gamedays[r["game_id"]] for r in rows)
    week1 = {r["knowable_as_of"] for r in rows if r["week"] == 1}
    assert week1 == {"2025-09-04", "2025-09-05", "2025-09-07", "2025-09-08"}


def test_a_row_whose_game_cannot_be_dated_is_dropped_and_counted(ffopp_db, ffopp_frame):
    """Never stored with a guessed or NULL knowledge time (Rule 1), and never
    silently: the drop lands on the NOT-by-design channel, which is what
    run_ingest's drop ceiling reads."""
    orphan = ffopp_frame["game_id"].iloc[0]
    ffopp_db.execute("DELETE FROM schedules WHERE game_id = ?", (orphan,))
    ffopp_db.commit()
    # Only the ATTRIBUTED rows of that game reach the stamp: an unattributed play
    # is filtered one branch earlier, and counting it twice is exactly the
    # double-denominator mistake the ingester's own comment is about.
    orphaned = ffopp_frame[ffopp_frame["game_id"] == orphan]
    lost = int(orphaned["player_id"].notna().sum())
    assert lost < len(orphaned)          # the case is real, not hypothetical
    with base.collect_drops() as tally:
        written = _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    assert written == 662 - 41 - lost
    assert tally["dropped"] == lost
    assert "no gameday" in "".join(tally["reasons"])
    assert not ffopp_db.execute(
        "SELECT 1 FROM ffopp_weekly WHERE game_id = ?", (orphan,)
    ).fetchall()


def test_an_unattributed_play_is_a_by_design_filter_not_a_drop(ffopp_db, ffopp_frame):
    """423 of 6,054 rows in the real 2025 file carry a NULL player_id (and NULL
    name and position): unattributed plays, joinable to nothing. Counting them
    against the drop ceiling would fail a perfectly healthy pull at 7% — the
    adp_rankings/IDP lesson from item 3.1b's first live run."""
    with base.collect_drops() as tally:
        _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    assert tally["filtered"] == 41
    assert tally["dropped"] == 0


# ----------------------------------------------------------------- leakage


def test_a_capture_is_invisible_under_the_default_view(ffopp_db, ffopp_frame):
    """The `fpecr`/`sleeper_ownership` pair, copied. Rows are stamped knowable =
    the game's own day but retrieved = the day the file was captured, which is
    always later. Under the safe-default `historical` view a read at the gameday
    itself therefore returns NOTHING — correctly, because this system did not
    have the file then. A caller that forgets `latest_truth` gets silence, not a
    wrong answer."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    assert ff_opportunity.get_ff_opportunity(ffopp_db, as_of="2025-09-08") == []
    assert ff_opportunity.get_ff_opportunity(ffopp_db, as_of="2025-09-15", week=1) == []


def test_latest_truth_reads_the_capture_and_still_gates_fact_time(ffopp_db, ffopp_frame):
    """The other half of the pair, and the half that must NOT be a free pass:
    `latest_truth` lifts the RETRIEVAL gate only. A read as-of the Thursday of
    week 1 sees the Thursday game and NOT the Sunday one, from the same stored
    capture."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    truth = base.latest_truth(ff_opportunity.get_ff_opportunity)

    assert truth(ffopp_db, as_of="2025-09-15", week=1)          # the capture is readable
    thursday = truth(ffopp_db, as_of="2025-09-04")
    assert thursday and {r["knowable_as_of"] for r in thursday} == {"2025-09-04"}
    assert {r["week"] for r in thursday} == {1}
    # Week 2 had not been played on week 1's Thursday, and nothing may show it.
    assert truth(ffopp_db, as_of="2025-09-04", week=2) == []
    assert truth(ffopp_db, as_of="2025-09-03") == []


def test_a_rewritten_capture_versions_rather_than_replaces(ffopp_db, ffopp_frame):
    """retrieved_as_of is IN THE PRIMARY KEY, which is the whole reason to capture
    a mutable source daily: the earlier vintage must still be on disk afterwards,
    and the reader must resolve to the newer one."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    revised = ffopp_frame.copy()
    revised["rec_touchdown_exp"] = revised["rec_touchdown_exp"] + 0.25
    _ingest(ffopp_db, revised, day="2025-09-17")

    vintages = {r[0] for r in ffopp_db.execute(
        "SELECT DISTINCT retrieved_as_of FROM ffopp_weekly")}
    assert vintages == {"2025-09-16", "2025-09-17"}

    truth = base.latest_truth(ff_opportunity.get_ff_opportunity)
    rows = truth(ffopp_db, as_of="2025-09-20", week=1)
    assert {r["retrieved_as_of"] for r in rows} == {"2025-09-17"}
    # …and the day-16 vintage is still readable as what we knew on day 16.
    day16 = ff_opportunity.get_ff_opportunity(ffopp_db, as_of="2025-09-16", week=1)
    assert day16 and {r["retrieved_as_of"] for r in day16} == {"2025-09-16"}


def test_two_pulls_on_one_day_overwrite_because_the_stamp_is_day_granular(
    ffopp_db, ffopp_frame
):
    """Recorded, not asserted away (recon hazard 11). The whole repo stamps
    day-granular, so a second capture on one day replaces the first rather than
    versioning beside it. That is the accepted trade — it is written down here so
    a future reader who expects intraday vintages finds the answer in a test
    instead of in a surprise."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    assert ffopp_db.execute("SELECT COUNT(*) FROM ffopp_weekly").fetchone()[0] == 662 - 41


# ------------------------------------------------------------ collapse floor


def test_a_truncated_capture_is_refused_before_the_write(ffopp_db, ffopp_frame):
    """APPEND-ONLY IS NOT A FLOOR (the players.CrosswalkCollapse lesson). Nothing
    has to be deleted for a thin capture to hide a good one: select_as_of resolves
    the newest retrieved version PER KEY, so merely arriving later is enough."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    thin = ffopp_frame.head(100)
    with pytest.raises(ff_opportunity.OpportunityCollapse, match="cumulative"):
        _ingest(ffopp_db, thin, day="2025-09-17")
    # BEFORE the write: the good vintage is untouched and no half-capture landed.
    assert {r[0] for r in ffopp_db.execute(
        "SELECT DISTINCT retrieved_as_of FROM ffopp_weekly")} == {"2025-09-16"}


def test_a_week_that_shrank_is_refused_even_when_the_total_holds(ffopp_db, ffopp_frame):
    """The per-week arm. A capture that loses week 1 but gains enough of week 2 to
    clear the total floor is still a partial file, and the week it lost is a week
    of a perishable source."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    lopsided = pd.concat([
        ffopp_frame[ffopp_frame["week"] == 1.0].head(20),
        ffopp_frame[ffopp_frame["week"] == 2.0],
        ffopp_frame[ffopp_frame["week"] == 2.0].assign(
            player_id=lambda d: d["player_id"] + "X"),
    ])
    with pytest.raises(ff_opportunity.OpportunityCollapse, match="week 1"):
        _ingest(ffopp_db, lopsided, day="2025-09-17")


def test_an_emptied_capture_is_refused_although_the_row_count_is_perfect(
    ffopp_db, ffopp_frame
):
    """THE shape a row count cannot see, and the one this project has already been
    bitten by once: every key present, every value gone. `players` measured it
    live — a pull with null id columns SHADOWED the good crosswalk, every lookup
    resolved to 0, and the run logged `ok`."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    # PARTIALLY emptied on purpose: an all-NULL capture is caught by the absolute
    # rule below, so only a capture that still has SOME values exercises the arm
    # that compares shares — and a source that quietly stops populating most of a
    # column is the likelier upstream accident than one that stops entirely.
    emptied = ffopp_frame.copy()
    emptied.loc[emptied.index[: int(len(emptied) * 0.9)], "total_fantasy_points_exp"] = float("nan")
    with pytest.raises(ff_opportunity.OpportunityCollapse, match="row COUNT is fine"):
        _ingest(ffopp_db, emptied, day="2025-09-17")
    assert {r[0] for r in ffopp_db.execute(
        "SELECT DISTINCT retrieved_as_of FROM ffopp_weekly")} == {"2025-09-16"}


def test_an_all_empty_first_capture_is_refused_with_nothing_to_compare_against(
    ffopp_db, ffopp_frame
):
    """The absolute case. On a fresh database there is no stored capture to
    measure against, so a relative floor alone would let the worst possible
    capture through as the baseline every later one is judged by."""
    emptied = ffopp_frame.copy()
    emptied["total_fantasy_points_exp"] = float("nan")
    with pytest.raises(ff_opportunity.OpportunityCollapse, match="NOT ONE of them"):
        _ingest(ffopp_db, emptied, day="2025-09-16")


def test_a_healthy_growing_capture_passes_the_floor(ffopp_db, ffopp_frame):
    """The floor must not cry wolf on the normal case — a capture that GAINS the
    week just played. A guard that fires on healthy data is how the report that
    matters gets ignored."""
    week1 = ffopp_frame[ffopp_frame["week"] == 1.0]
    _ingest(ffopp_db, week1, day="2025-09-09")
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16")
    weeks = {r[0] for r in ffopp_db.execute(
        "SELECT DISTINCT week FROM ffopp_weekly WHERE retrieved_as_of = '2025-09-16'")}
    assert weeks == {1, 2}


def test_a_capture_of_the_wrong_season_is_refused_whole(ffopp_db, ffopp_frame):
    """The asset is per-season and its NAME is the only thing that says which, so
    a frame carrying another season means the wrong file was read. Filing it under
    the season the caller believed they were pulling is the manufactured-history
    shape BACKFILL_EXCLUDED exists to prevent."""
    with pytest.raises(ff_opportunity.OpportunityCollapse, match="carries season"):
        ff_opportunity.ingest_ff_opportunity(
            ffopp_db, ffopp_frame, retrieved_as_of="2026-09-16", season=2026,
            model_version=ff_opportunity.EXPECTED_MODEL_VERSION,
        )
    assert ffopp_db.execute("SELECT COUNT(*) FROM ffopp_weekly").fetchone()[0] == 0


# --------------------------------------------------------- model provenance


def test_a_model_version_bump_refuses_the_capture(ffopp_db, ffopp_frame):
    """Every `_exp` column is a MODEL OUTPUT, and a bumped model can change what
    those numbers mean without changing one column name. The amendment's own
    requirement: a ffverse model bump must not silently redefine a column
    mid-season."""
    with pytest.raises(ff_opportunity.ModelVersionChanged, match="pinned to"):
        _ingest(ffopp_db, ffopp_frame, day="2025-09-16", model_version="v2.0.0")
    assert ffopp_db.execute("SELECT COUNT(*) FROM ffopp_weekly").fetchone()[0] == 0


def test_a_bump_is_caught_against_the_stored_history_too(
    ffopp_db, ffopp_frame, monkeypatch
):
    """The second comparison, and the one that survives a code edit: even if the
    pin in this module is moved to the new version, a table already holding rows
    from the old model refuses to mix them."""
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16", model_version="v1.0.0")
    monkeypatch.setattr(ff_opportunity, "EXPECTED_MODEL_VERSION", "v2.0.0")
    with pytest.raises(ff_opportunity.ModelVersionChanged, match="was built by"):
        _ingest(ffopp_db, ffopp_frame, day="2025-09-17", model_version="v2.0.0")


def test_an_unknown_model_version_is_recorded_not_refused(ffopp_db, ffopp_frame):
    """An unknown version is not a changed one. Losing a perishable capture
    because upstream stopped shipping version.txt would be the guard costing more
    than the thing it guards against — so it is stored NULL and noted on the run."""
    with base.collect_drops() as tally:
        _ingest(ffopp_db, ffopp_frame, day="2025-09-16", model_version=None)
    assert any("version.txt is absent" in note for note in tally["notes"])
    assert ffopp_db.execute(
        "SELECT COUNT(*) FROM ffopp_weekly WHERE model_version IS NULL"
    ).fetchone()[0] == 662 - 41


def test_provenance_is_stored_on_every_row(ffopp_db, ffopp_frame):
    _ingest(ffopp_db, ffopp_frame, day="2025-09-16",
            asset_updated_at="2026-09-04T09:57:25Z", source_timestamp="2026-09-04 09:57:10")
    row = ffopp_db.execute(
        "SELECT asset_updated_at, source_timestamp, model_version FROM ffopp_weekly LIMIT 1"
    ).fetchone()
    assert row["asset_updated_at"] == "2026-09-04T09:57:25Z"
    assert row["source_timestamp"] == "2026-09-04 09:57:10"
    assert row["model_version"] == ff_opportunity.EXPECTED_MODEL_VERSION


# --------------------------------------------------------------- the fetcher


def _release(assets):
    return {"assets": [
        {"name": name, "browser_download_url": f"https://example.invalid/{name}",
         "updated_at": updated, "size": 10}
        for name, updated in assets
    ]}


def test_a_season_upstream_has_not_published_reads_as_a_404(ffopp_db):
    """Shaped so `refresh._is_upstream_absent` recognises it. Pre-registered
    Week-1 behaviour: the 2026 file does not exist until games are played, and
    that must be `upstream_absent` — which is NOT an anchor, so the source retries
    tomorrow — rather than a failure the operator learns to ignore."""
    release = _release([("ep_weekly_2025.parquet", "2026-09-04T09:57:25Z")])
    with pytest.raises(urllib.error.HTTPError) as exc:
        ff_opportunity.release_asset(release, ff_opportunity.asset_name(2026))
    assert exc.value.code == 404
    assert refresh._is_upstream_absent(exc.value)


def test_the_orchestrator_records_upstream_absent_not_failed(db):
    """Through the REAL orchestrator, because the claim is about what the run log
    and `ingest status` say, not about the exception."""
    _in_season_schedule(db)
    release = _release([("ep_weekly_2025.parquet", "2026-09-04T09:57:25Z")])
    with patch.object(ff_opportunity, "fetch_release", return_value=release):
        out = refresh.run_ingest(
            db, sources=(refresh.SOURCES_BY_NAME["ff_opportunity"],), season=2026,
            retrieved_as_of="2026-09-10", today="2026-09-10",
        )
    assert [s["status"] for s in out] == [refresh.STATUS_ABSENT]
    assert "no asset named" in out[0]["reason"]


def test_a_404_after_a_successful_capture_is_a_failure_not_an_absence(db):
    """`run_ingest`'s own rule, exercised on this source because it is the one it
    matters for: once the season's file has landed, a 404 means the release was
    renamed or withdrawn — a real break wearing the expected costume."""
    _in_season_schedule(db)
    refresh.start_run(db, source="ff_opportunity", season=2026, started_at="x",
                      retrieved_as_of="2026-09-14", scope="s", batch_id="b")
    db.execute("UPDATE nfl_ingest_runs SET status = 'ok', rows_written = 300")
    db.commit()
    release = _release([("ep_weekly_2025.parquet", "2026-09-04T09:57:25Z")])
    with patch.object(ff_opportunity, "fetch_release", return_value=release):
        out = refresh.run_ingest(
            db, sources=(refresh.SOURCES_BY_NAME["ff_opportunity"],), season=2026,
            retrieved_as_of="2026-09-15", today="2026-09-15",
        )
    assert [s["status"] for s in out] == [refresh.STATUS_FAILED]
    assert "renamed" in out[0]["reason"]


def test_the_whole_pull_lands_rows_provenance_and_a_mirror(ffopp_db, tmp_path):
    """END TO END, offline: release document -> mirror on disk -> lean rows ->
    provenance on every row -> a run note naming the rewrite clock.

    The mirror half is the part that is easy to leave untested and expensive to
    get wrong: it is the ONLY copy of the 134 columns the table does not store,
    for a vintage that cannot be re-fetched."""
    source = _FIXTURE_DIR / "ffopp_weekly.parquet"
    mirror = tmp_path / "ep_weekly_2025-2025-09-16.parquet"
    release = _release([
        ("ep_weekly_2025.parquet", "2025-09-16T09:57:25Z"),
        ("timestamp.txt", "2025-09-16T09:57:25Z"),
        ("version.txt", "2025-09-16T09:57:25Z"),
    ])
    texts = {"version.txt": ff_opportunity.EXPECTED_MODEL_VERSION,
             "timestamp.txt": "2025-09-16 02:57:10"}

    def _fetch(asset, dest, **kw):
        shutil.copyfile(source, dest)
        return str(dest)

    with base.collect_drops() as tally, \
            patch.object(ff_opportunity, "fetch_release", return_value=release), \
            patch.object(ff_opportunity, "fetch_asset", _fetch), \
            patch.object(ff_opportunity, "fetch_text_asset",
                         lambda rel, name: texts[name]):
        written = ff_opportunity.pull_ff_opportunity(
            ffopp_db, retrieved_as_of="2025-09-16", season=2025, path=mirror,
        )

    assert written == 662 - 41
    assert mirror.exists() and mirror.stat().st_size == source.stat().st_size
    row = ffopp_db.execute(
        "SELECT asset_updated_at, source_timestamp, model_version FROM ffopp_weekly LIMIT 1"
    ).fetchone()
    assert row["asset_updated_at"] == "2025-09-16T09:57:25Z"
    assert row["source_timestamp"] == "2025-09-16 02:57:10"
    assert row["model_version"] == ff_opportunity.EXPECTED_MODEL_VERSION
    assert any("updated_at=2025-09-16T09:57:25Z" in note for note in tally["notes"])


def test_a_second_pull_the_same_day_reuses_the_mirror_unless_forced(ffopp_db, tmp_path):
    """The mirror is dated, so a re-run inside one day reads the file it already
    has — and `--force` (refresh=True) re-downloads, because for a source that is
    REWRITTEN upstream several times a week that is the only thing --force can
    sensibly mean."""
    source = _FIXTURE_DIR / "ffopp_weekly.parquet"
    mirror = tmp_path / "ep_weekly_2025-2025-09-16.parquet"
    shutil.copyfile(source, mirror)
    release = _release([("ep_weekly_2025.parquet", "2025-09-16T09:57:25Z")])
    calls = []

    def _fetch(asset, dest, **kw):
        calls.append(str(dest))
        shutil.copyfile(source, dest)
        return str(dest)

    with patch.object(ff_opportunity, "fetch_release", return_value=release), \
            patch.object(ff_opportunity, "fetch_asset", _fetch), \
            patch.object(ff_opportunity, "fetch_text_asset", lambda rel, name: None):
        ff_opportunity.pull_ff_opportunity(
            ffopp_db, retrieved_as_of="2025-09-16", season=2025, path=mirror)
        assert calls == []
        ff_opportunity.pull_ff_opportunity(
            ffopp_db, retrieved_as_of="2025-09-16", season=2025, path=mirror, refresh=True)
        assert calls == [str(mirror)]


def test_a_missing_metadata_asset_is_none_rather_than_an_error():
    """Provenance going missing must never be the reason a perishable capture is
    lost. `fetch_text_asset` answers None; `_check_model_version` then records
    rather than refuses."""
    release = _release([("ep_weekly_2025.parquet", "2026-09-04T09:57:25Z")])
    assert ff_opportunity.fetch_text_asset(release, "version.txt") is None


def test_the_metadata_reads_are_bounded_in_time_and_size():
    """Not only the parquet. The release document is a 330 KB JSON read on EVERY
    pull, and `net.HTTP_TIMEOUT` bounds each socket READ rather than the transfer
    — so an API that trickles just under the timeout would hold the daily unit and
    the sources behind this one in the registry would never run."""
    class _Trickle:
        def read(self, _n):
            return b"x" * 1024

    with pytest.raises(TimeoutError, match="budget"):
        ff_opportunity._read_bounded(_Trickle(), url="u", budget_s=0.0, max_bytes=1 << 30)
    with pytest.raises(ValueError, match="not the shape"):
        ff_opportunity._read_bounded(_Trickle(), url="u", budget_s=60.0, max_bytes=2048)


def test_an_asset_with_no_download_url_is_refused_rather_than_requested():
    """Upstream schema drift, not a missing file: a 404 would say "retry
    tomorrow", which is the wrong answer to "the release changed shape"."""
    release = {"assets": [{"name": "ep_weekly_2025.parquet", "updated_at": "x"}]}
    asset = ff_opportunity.release_asset(release, "ep_weekly_2025.parquet")
    with pytest.raises(ValueError, match="no download"):
        ff_opportunity._open(asset.url)


def test_the_mirror_path_is_dated_and_lives_under_the_gitignored_data_tree():
    """A bare `ep_weekly_2026.parquet` would silently become a DIFFERENT file on
    the next download, and the vintage the database describes would exist nowhere.
    Rule 5: the mirror is bulk data under the gitignored top-level data/ tree."""
    path = Path(refresh.ffopp_mirror_path(2026, "2026-09-15"))
    assert path.name == "ep_weekly_2026-2026-09-15.parquet"
    assert path.parent.name == "ffopp" and path.parent.parent.name == "data"
    from ziggurat import repo_guard
    relative = str(path.relative_to(refresh.REPO_ROOT))
    assert repo_guard.violations([relative]) == [relative]


def test_the_download_is_bounded_by_a_whole_transfer_budget(tmp_path):
    """`net.HTTP_TIMEOUT` bounds each socket READ, not the transfer: an upstream
    that trickles bytes just under the timeout would hold the daily unit for as
    long as it likes. Also proves the `.part` is removed rather than left under a
    name that says what it nearly was."""
    class _Trickle:
        def read(self, _n):
            return b"x" * 1024
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    asset = ff_opportunity.ReleaseAsset("ep.parquet", "https://example.invalid/ep", None, 1)
    dest = tmp_path / "ep_weekly_2026-2026-09-15.parquet"
    with patch.object(ff_opportunity, "_open", lambda *a, **k: _Trickle()):
        with pytest.raises(TimeoutError, match="budget"):
            ff_opportunity.fetch_asset(asset, dest, budget_s=0.0)
    assert not dest.exists()
    assert list(tmp_path.glob("*.part")) == []


# ------------------------------------------------------------ registry seam


def test_the_source_is_registered_perishable_and_forward_only():
    """Three properties that are one decision. Perishable because the asset is
    REWRITTEN in place; forward-only because BACKFILL_EXCLUDED stays and decide()
    reads it for any past season; needs_schedules because the stamp is a join."""
    spec = refresh.SOURCES_BY_NAME["ff_opportunity"]
    assert spec.perishable is True
    assert spec.needs_schedules is True
    assert spec.group == refresh.GROUP_DAILY
    assert spec.interval_days == 1
    assert "ff_opportunity" in refresh.BACKFILL_EXCLUDED
    assert "ff_opportunity" not in refresh.BACKFILL_SOURCES


def test_a_past_season_is_refused_and_force_cannot_reach_it(db):
    """The forward-only guarantee, on this source specifically. The recorded
    hazard is measured and not hypothetical: `ingest run --source projections
    --season 2023` wrote 57,910 rows of TODAY's board into the 2023 partition and
    logged `ok`."""
    _in_season_schedule(db, season=2025, first="2025-09-04")
    _in_season_schedule(db, season=2026)
    spec = refresh.SOURCES_BY_NAME["ff_opportunity"]
    decision = refresh.decide(db, spec, season=2025, today="2026-09-15",
                              have_credentials=True, force=True)
    assert decision.action == refresh.STATUS_BLOCKED
    assert refresh.BACKFILL_EXCLUDED["ff_opportunity"] in decision.reason


def test_the_scope_line_says_whether_the_day_costs_a_download(db, tmp_path):
    ctx = refresh.IngestContext(conn=db, season=2026, retrieved_as_of="2026-09-15",
                                today="2026-09-15")
    assert "fresh" in refresh._scope_ff_opportunity(ctx)


# ------------------------------------------------------------- the scope fence


def _uses_ff_opportunity(text: str) -> bool:
    """Does this module IMPORT or QUERY the expected-points source?

    Deliberately narrower than "mentions": an import statement, an attribute
    access on the module, or the table by name. See the fence test below.
    """
    return bool(
        re.search(r"^\s*(?:from|import)\b[^\n]*\bff_opportunity\b", text, re.M)
        or re.search(r"\bff_opportunity\s*\.\s*\w", text)
        or re.search(r"\bffopp_weekly\b", text)
    )


def test_no_core_module_imports_ff_opportunity():
    """WEEK-1 SCOPE FENCE (item 4.2b amendment: "capture only, no integration").

    The risk this test exists for is named in the recon: an expected-TD column
    looks too useful to leave alone, and 4.2c has not defined the instrument that
    would price it. The first live Tuesday is also trying to record an UNDISTURBED
    baseline of the shipped decision path, which a new signal would destroy.

    Rule 2 rides along: `total_fantasy_points*` here is ffverse's own full-PPR
    scoring, so a core module reaching for it would be pricing a decision in
    another league's currency."""
    core = Path(refresh.__file__).resolve().parents[2] / "core"
    offenders = [path.name for path in sorted(core.rglob("*.py"))
                 if _uses_ff_opportunity(path.read_text(encoding="utf-8"))]
    assert offenders == [], (
        "item 4.2b is CAPTURE ONLY for Week 1; these core modules reach for the "
        f"expected-points source: {offenders}"
    )
    # A PROSE MENTION IS NOT A USE, and the difference is not academic:
    # core/candidates.py already names this source in its docstring (it records
    # why TD-regression was deferred). A fence that fired on that would be turned
    # off within a week. These two lines prove the fence still has teeth.
    assert not _uses_ff_opportunity("the ff_opportunity feed is deferred to 4.2c")
    assert _uses_ff_opportunity("from ziggurat.data.nfl import ff_opportunity")
    assert _uses_ff_opportunity("rows = conn.execute('SELECT * FROM ffopp_weekly')")


# ------------------------------------------------------------------ helpers


def _in_season_schedule(conn: sqlite3.Connection, season=2026, first="2026-09-09"):
    """A REG schedule so `season_phase` reads in-season and the dependency holds."""
    from datetime import date
    start = date.fromisoformat(first)
    rows = [
        (f"{season}_{week:02d}_AAA_BBB", season, week, "REG",
         date.fromordinal(start.toordinal() + (week - 1) * 7).isoformat(),
         "BBB", "AAA", f"{season}-08-01", f"{season}-08-01")
        for week in range(1, 19)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, knowable_as_of, retrieved_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
