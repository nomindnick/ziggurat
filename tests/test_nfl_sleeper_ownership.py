"""Ingestion, freeze, leakage and registry tests for Sleeper ownership (012).

Item 4.1's second weekly market proxy: what the room was DOING, week by week,
frozen per (season_type, season, week) as raw JSON and loaded under one pull
day. Everything here runs offline against a hand-made two-week grid in
``tests/fixtures/sleeper-research/`` — every id is invented, every number is
invented, and the network seam is patched at ``urllib.request.urlopen``.

Three things get more attention than the rest, because each is a way this
source silently corrupts a result rather than failing:

* THE FREEZE IS THE ARCHIVE. Upstream is undocumented and may vanish or drift,
  so the first pull of a week is the only observation of it and a later pull
  must never overwrite the file. ``test_a_frozen_week_is_never_overwritten``
  feeds a DIFFERENT payload on the second pull and proves the bytes on disk and
  the rows in the table both stayed with the first one.

* THE COLLAPSE FLOOR. ``retrieved_as_of`` is in the primary key so a re-pull
  versions rather than replaces — which means a half-served grid does not have
  to delete anything to hide the good rows (``select_as_of`` resolves the
  newest version). The floor is mutation-verified: the truncated re-pull is
  refused BEFORE the write and the row count is unchanged.

* AN ABSENCE IS NOT A ZERO. Upstream omits anything at or below ~1% owned, so
  a key missing from one week is "at or below the floor", never "0.0" and never
  "dropped". ``ownership_deltas`` imputes the floor and says so (``censored``).

* A WEEK IS FETCHED ONLY ONCE IT HAS SETTLED (item 4.1 audit, SLEEP-1 / OPS-1).
  The current-week bucket upstream aliases the live board, so "the last gameday
  has passed" proves nothing about Sleeper's own week pointer; ``SETTLE_DAYS``
  is the labelled hypothesis, ``--force`` re-fetches a frozen week live and
  REPORTS any divergence without ever rewriting the file, and ``200 null`` is an
  absence handled per week — never frozen, never a failure among stored weeks.

Rule 5: nothing below is a real player, team roster, or league fact.
"""

import io
import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.data.nfl import base, fpecr, refresh
from ziggurat.data.nfl import sleeper_ownership as so

FIXTURES = Path(__file__).parent / "fixtures" / "sleeper-research"
SEASON = 2023
PULL_DAY = "2026-09-01"

#: The invented ``players`` crosswalk the fixture grid resolves against.
#: (sleeper_id, gsis_id, position, retrieved_as_of). 900008 carries TWO rows —
#: a placeholder gsis first, the real one on a later pull — which is the shape
#: measured live (186 ids on 2026-09-01), so the newest-row rule is exercised.
_PLAYERS = [
    ("900001", "00-0090001", "QB", "2026-08-01"),
    ("900002", "00-0090002", "RB", "2026-08-01"),
    ("900003", "00-0090003", "PK", "2026-08-01"),   # nflverse spelling; stored as K
    ("900004", "00-0090004", "LB", "2026-08-01"),   # IDP: filtered by design
    ("900006", "00-0090006", "TE", "2026-08-01"),
    ("900007", "00-0090007", "XX", "2026-08-01"),   # gsis on file, no position
    ("900008", "PLC900008", "WR", "2026-07-01"),    # placeholder, older pull
    ("900008", "00-0090008", "WR", "2026-08-15"),   # real id, newer pull
    ("900009", "00-0090009", "WR", "2026-08-01"),
]


def _stub_players(db):
    for sid, gsis, pos, retrieved in _PLAYERS:
        db.execute(
            "INSERT INTO players (gsis_id, sleeper_id, position, retrieved_as_of, "
            "knowable_as_of) VALUES (?,?,?,?,?)",
            (gsis, sid, pos, retrieved, retrieved),
        )
    db.commit()


def _stub_schedule(db, weeks, *, season=SEASON):
    """``weeks`` maps week -> (first gameday, last gameday); Thu/Sun/Mon shape."""
    for week, (first, last) in weeks.items():
        for i, day in enumerate(sorted({first, last})):
            db.execute(
                "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
                "home_team, away_team, retrieved_as_of, knowable_as_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{season}_{week:02d}_{i}", season, week, "REG", day, "BBB", "AAA",
                 f"{season}-08-01", f"{season}-08-01"),
            )
    db.commit()


#: Week 6 finishes Monday 2023-10-16 and SETTLES (last gameday + SETTLE_DAYS=7,
#: strictly before the pull day) from 2023-10-24; week 7 finishes 10-23 and
#: settles from 10-31; week 8 finishes 10-30 (settles 11-07). All three are
#: settled by PULL_DAY. `_SETTLED_6` / `_SETTLED_7` are the first pull days on
#: which each is fetchable.
_WEEKS = {
    6: ("2023-10-12", "2023-10-16"),
    7: ("2023-10-19", "2023-10-23"),
    8: ("2023-10-26", "2023-10-30"),
}
_SETTLED_6 = "2023-10-24"
_SETTLED_7 = "2023-10-31"


def _fixture(week):
    with open(FIXTURES / f"regular-{SEASON}-wk{week:02d}.json", encoding="utf-8") as f:
        return json.load(f)


def _fetch_from_fixtures(calls=None):
    """A ``fetch`` that serves the committed grid and records what it was asked."""
    def fetch(season_type, season, week, *, sleep):
        if calls is not None:
            calls.append((season_type, season, week))
        try:
            return _fixture(week)
        except FileNotFoundError as exc:
            raise so.UpstreamAbsent(f"fixture week {week} absent") from exc
    return fetch


@pytest.fixture()
def grid_db(db):
    _stub_players(db)
    _stub_schedule(db, _WEEKS)
    return db


def _pull(db, cache_dir, *, weeks=(6, 7), retrieved_as_of=PULL_DAY, fetch=None,
          sleep=None, calls=None):
    sleeps = []
    n = so.pull_sleeper_ownership(
        db, SEASON, retrieved_as_of=retrieved_as_of, cache_dir=cache_dir, weeks=weeks,
        fetch=fetch or _fetch_from_fixtures(calls), sleep=sleep or sleeps.append,
    )
    return n, sleeps


def _rows(db, **kw):
    return base.latest_truth(so.get_sleeper_ownership)(db, as_of="2023-12-31", **kw)


def _count(db):
    return db.execute("SELECT COUNT(*) FROM sleeper_ownership").fetchone()[0]


# ---------------------------------------------------------------- the load


def test_the_grid_loads_with_the_house_positions_and_the_crosswalk(grid_db, tmp_path):
    """One pull, two weeks: every mapping rule asserted by name on the row."""
    n, _ = _pull(grid_db, tmp_path)
    rows = _rows(grid_db)
    # 10 keys wk6 - 1 IDP = 9; 11 keys wk7 - 1 IDP = 10.
    assert n == 19 and len(rows) == 19
    by = {(r["week"], r["sleeper_id"]): r for r in rows}

    assert by[(6, "900003")]["position"] == "K"            # PK -> K, the house spelling
    assert by[(6, "900003")]["gsis_id"] == "00-0090003"
    assert (6, "900004") not in by                          # LB filtered, by design
    assert by[(6, "900005")]["gsis_id"] is None             # unresolved: KEPT, not dropped
    assert by[(6, "900005")]["position"] == so.UNKNOWN_POSITION
    assert by[(6, "900006")]["started_pct"] is None         # optional `started` -> NULL
    assert by[(6, "900006")]["owned_pct"] == 7.0
    assert by[(6, "900007")]["gsis_id"] == "00-0090007"     # XX: gsis kept, position UNK
    assert by[(6, "900007")]["position"] == so.UNKNOWN_POSITION
    # Team keys: position DST, NULL gsis, team through base.TEAM_ALIASES.
    lar = by[(6, "LAR")]
    assert (lar["position"], lar["gsis_id"], lar["team"]) == ("DST", None, "LA")
    assert by[(6, "KC")]["team"] == "KC"
    assert by[(7, "900009")]["position"] == "WR"           # present only in week 7


def test_the_crosswalk_takes_the_newest_players_row_per_sleeper_id(grid_db, tmp_path):
    """A rookie's placeholder gsis is re-keyed to the real one on a later nflverse
    pull; the map must follow the newest row, by a TOTAL order, never by
    insertion or set order (item 3.11's PYTHONHASHSEED lesson)."""
    xw = so.sleeper_crosswalk(grid_db)
    assert xw["900008"] == ("00-0090008", "WR")
    _pull(grid_db, tmp_path)
    assert {r["gsis_id"] for r in _rows(grid_db) if r["sleeper_id"] == "900008"} == {
        "00-0090008"
    }


def test_knowable_as_of_is_the_weeks_last_reg_gameday(grid_db, tmp_path):
    """The labelled hypothesis, pinned: the stamp is the Monday the week ends on,
    read from `schedules`, never the pull day and never the first gameday."""
    _pull(grid_db, tmp_path)
    stamps = {r["week"]: r["knowable_as_of"] for r in _rows(grid_db)}
    assert stamps == {6: "2023-10-16", 7: "2023-10-23"}
    assert {r["retrieved_as_of"] for r in _rows(grid_db)} == {PULL_DAY}


def test_loading_the_same_frozen_week_twice_is_deterministic(grid_db, tmp_path):
    """Two loads from one frozen file must agree row for row (the freeze is the
    proof of what upstream served; the load must add nothing of its own)."""
    _pull(grid_db, tmp_path)
    first = [dict(r) for r in _rows(grid_db)]
    _pull(grid_db, tmp_path, retrieved_as_of="2026-09-02")
    second = [dict(r) for r in _rows(grid_db)]
    strip = lambda rs: sorted(  # noqa: E731
        ({k: v for k, v in r.items() if k != "retrieved_as_of"} for r in rs),
        key=lambda r: (r["week"], r["sleeper_id"]),
    )
    assert strip(first) == strip(second)
    assert {r["retrieved_as_of"] for r in second} == {"2026-09-02"}   # versioned, not replaced
    assert _count(grid_db) == 38


# ---------------------------------------------------------------- the freeze


def test_a_frozen_week_is_never_overwritten(grid_db, tmp_path):
    """The archive rule. The second pull is handed a DIFFERENT payload: neither
    the file nor the table may take it, and the network is not touched."""
    calls = []
    _pull(grid_db, tmp_path, weeks=(6,), calls=calls)
    path = Path(so.frozen_path(tmp_path, season_type="regular", season=SEASON, week=6))
    frozen_bytes = path.read_bytes()
    assert calls == [("regular", SEASON, 6)]

    def different(season_type, season, week, *, sleep):
        raise AssertionError("the network must not be touched for a frozen week")

    n, sleeps = _pull(grid_db, tmp_path, weeks=(6,), fetch=different,
                      retrieved_as_of="2026-09-02")
    assert path.read_bytes() == frozen_bytes
    assert n == 9 and sleeps == []
    assert {r["owned_pct"] for r in _rows(grid_db, week=6, gsis_id="00-0090001")} == {99.6}


def test_force_refetches_and_reports_divergence_without_overwriting(grid_db, tmp_path):
    """The ``--force`` instrument: every frozen week is re-fetched LIVE and
    compared. A divergence is REPORTED (a run note naming the week and the
    changed-key count); the frozen file is never rewritten and the rows still
    come from it. Identical copies say so. Live requests are spaced."""
    _pull(grid_db, tmp_path, weeks=(6, 7))
    frozen_bytes = {w: Path(so.frozen_path(tmp_path, season_type="regular", season=SEASON,
                                           week=w)).read_bytes() for w in (6, 7)}
    calls = []

    def edited(season_type, season, week, *, sleep):
        calls.append(week)
        payload = {k: dict(v) for k, v in _fixture(week).items()}
        if week == 6:
            payload["900001"]["owned"] = 42.0          # a silent upstream revision
        return payload

    sleeps = []
    with base.collect_drops() as tally:
        n = so.pull_sleeper_ownership(
            grid_db, SEASON, retrieved_as_of="2026-09-02", cache_dir=tmp_path,
            weeks=(6, 7), fetch=edited, sleep=sleeps.append, verify_frozen=True)
    assert n == 19 and calls == [6, 7]
    assert sleeps == [so.REQUEST_SPACING_S]                  # spaced, first one free
    notes = tally["notes"]
    assert len(notes) == 1 and notes[0].startswith("DIVERGENCE on week(s) [6]"), notes
    assert "week 6: 1 of 10 frozen keys CHANGED upstream, 0 added, 0 removed" in notes[0]
    assert "week 7: live copy identical (11 keys)" in notes[0]
    for w in (6, 7):
        path = Path(so.frozen_path(tmp_path, season_type="regular", season=SEASON, week=w))
        assert path.read_bytes() == frozen_bytes[w]      # never rewritten
    stored = {(r["week"], r["sleeper_id"]): r["owned_pct"]
              for r in _rows(grid_db) if r["retrieved_as_of"] == "2026-09-02"}
    assert stored[(6, "900001")] == 99.6                  # rows come from the FROZEN file

    # Identical live copies: verified, no DIVERGENCE word anywhere.
    with base.collect_drops() as tally:
        so.pull_sleeper_ownership(
            grid_db, SEASON, retrieved_as_of="2026-09-03", cache_dir=tmp_path,
            weeks=(6, 7), fetch=_fetch_from_fixtures(), verify_frozen=True)
    assert len(tally["notes"]) == 1 and "DIVERGENCE" not in tally["notes"][0]
    assert tally["notes"][0].startswith("--force verified the frozen weeks")
    assert "identical (10 keys)" in tally["notes"][0]

    # Without force: no live request at all for a frozen week.
    calls.clear()
    so.pull_sleeper_ownership(grid_db, SEASON, retrieved_as_of="2026-09-04",
                              cache_dir=tmp_path, weeks=(6, 7), fetch=edited)
    assert calls == []


def test_payload_divergence_counts_changed_added_removed():
    frozen = {"a": {"owned": 1.0, "started": 0.5}, "b": {"owned": 2.0}, "c": {"owned": 3.0}}
    live = {"a": {"owned": 1.0, "started": 0.5}, "b": {"owned": 2.5}, "d": {"owned": 9.0}}
    assert so.payload_divergence(frozen, live) == {"changed": 1, "added": 1, "removed": 1}
    assert so.payload_divergence(frozen, dict(frozen)) == {"changed": 0, "added": 0, "removed": 0}


def test_a_force_run_carries_the_divergence_note_into_the_run_log(grid_db, tmp_path, monkeypatch):
    """Through the registry: ``ingest run --force`` re-versions the stored weeks
    AND the divergence report lands in the run's free-text column, where
    ``ingest status`` prints it."""
    monkeypatch.setattr(refresh, "SLEEPER_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(so, "fetch_research", _fetch_from_fixtures())
    monkeypatch.setattr(so.time, "sleep", lambda s: None)
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                       retrieved_as_of=_SETTLED_7, today=_SETTLED_7)
    assert {r["week"] for r in _rows(grid_db)} == {6, 7}

    def edited(season_type, season, week, *, sleep):
        payload = {k: dict(v) for k, v in _fixture(week).items()}
        payload["900002"]["owned"] = 1.5
        return payload

    monkeypatch.setattr(so, "fetch_research", edited)
    runs = refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                              retrieved_as_of="2023-11-02", today="2023-11-02", force=True)
    run = runs[0]
    assert run["status"] == refresh.STATUS_OK, run
    assert "DIVERGENCE on week(s) [6, 7]" in run["reason"]
    error = grid_db.execute(
        "SELECT error FROM nfl_ingest_runs WHERE source = 'sleeper_ownership' "
        "ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    assert "DIVERGENCE" in error
    text = refresh.format_status(grid_db, season=SEASON, today="2023-11-02")
    assert "DIVERGENCE" in text
    # A frozen week is a fact: bytes unchanged, rows unchanged.
    assert {r["owned_pct"] for r in _rows(grid_db) if r["sleeper_id"] == "900002"} == {
        _fixture(6)["900002"]["owned"], _fixture(7)["900002"]["owned"]}


def test_the_freeze_is_written_atomically_and_only_after_validation(tmp_path):
    """A drifted response is never frozen: the file must not exist afterwards,
    and no ``.part`` may be left behind."""
    def drifted(season_type, season, week, *, sleep):
        payload = ["not", "a", "map"]
        so.validate_payload(payload, where="live")   # what fetch_research does
        return payload

    with pytest.raises(so.OwnershipSchemaDrift):
        so.freeze_week(tmp_path, season_type="regular", season=SEASON, week=6, fetch=drifted)
    assert list(tmp_path.iterdir()) == []


def test_live_fetches_are_spaced_and_cached_weeks_never_sleep(grid_db, tmp_path):
    """N live fetches -> N-1 sleeps of REQUEST_SPACING_S; a run served from the
    freeze sleeps zero times (the cadence pulls the same season daily)."""
    _, sleeps = _pull(grid_db, tmp_path, weeks=(6, 7))
    assert sleeps == [so.REQUEST_SPACING_S]
    _, sleeps = _pull(grid_db, tmp_path, weeks=(6, 7), retrieved_as_of="2026-09-02")
    assert sleeps == []


def test_a_mixed_run_sleeps_only_before_the_live_fetch(grid_db, tmp_path):
    """Week 6 frozen, week 7 live: one live fetch, so zero sleeps — the spacing
    is between live requests, not between weeks."""
    _pull(grid_db, tmp_path, weeks=(6,))
    _, sleeps = _pull(grid_db, tmp_path, weeks=(6, 7), retrieved_as_of="2026-09-02")
    assert sleeps == []


# ---------------------------------------------------------------- refusals


def test_pull_refuses_an_unsettled_week_and_never_fetches(grid_db, tmp_path):
    """A week whose last gameday has passed but whose SETTLE_DAYS have not is
    refused BEFORE any fetch — the old rule fetched it the next morning, when
    the current-week bucket upstream is still the live board."""
    calls = []
    # 10-28: week 6 settled (10-16 + 7 < 10-28); 7 (10-23) and 8 (10-30) not.
    with pytest.raises(ValueError, match=r"weeks \[7, 8\] .* have not settled") as info:
        _pull(grid_db, tmp_path, weeks=(6, 7, 8), retrieved_as_of="2023-10-28", calls=calls)
    assert f"SETTLE_DAYS={so.SETTLE_DAYS}" in str(info.value)
    assert calls == []
    # The Tuesday after week 6's Monday-night finish: nothing has settled yet.
    with pytest.raises(ValueError, match="no REG week settled") as info:
        _pull(grid_db, tmp_path, weeks=(6,), retrieved_as_of="2023-10-17", calls=calls)
    assert f"SETTLE_DAYS={so.SETTLE_DAYS}" in str(info.value)
    with pytest.raises(ValueError, match="no REG week"):
        _pull(grid_db, tmp_path, weeks=(6,), retrieved_as_of="2023-10-01", calls=calls)
    assert calls == [] and _count(grid_db) == 0 and list(tmp_path.iterdir()) == []


def test_completed_weeks_holds_a_week_until_it_settles(grid_db):
    """The settlement window, pinned day by day: strictly `last + SETTLE_DAYS <
    retrieved_as_of`, and the VALUE stays the last gameday (knowledge time does
    not move with retrieval time)."""
    assert so.SETTLE_DAYS == 7
    weeks = lambda day, **kw: so.completed_weeks(  # noqa: E731
        grid_db, SEASON, retrieved_as_of=day, **kw)
    assert weeks("2023-10-17") == {}                          # finished, not settled
    assert weeks("2023-10-23") == {}                          # exactly +7: still held
    assert weeks(_SETTLED_6) == {6: "2023-10-16"}             # +8: settled, old stamp
    assert weeks("2023-10-30") == {6: "2023-10-16"}
    assert weeks(_SETTLED_7) == {6: "2023-10-16", 7: "2023-10-23"}
    assert set(weeks(PULL_DAY)) == {6, 7, 8}
    # The lag is a parameter with a labelled default, not a hidden constant.
    assert weeks("2023-10-17", settle_days=0) == {6: "2023-10-16"}
    assert weeks("2023-10-31", settle_days=30) == {}


def test_a_week_is_fetched_only_after_the_settlement_window(grid_db, tmp_path):
    """Day D+7 refuses, day D+8 fetches exactly that week, and the stored
    knowable_as_of is still the last gameday."""
    calls = []
    with pytest.raises(ValueError):
        _pull(grid_db, tmp_path, weeks=(6,), retrieved_as_of="2023-10-23", calls=calls)
    assert calls == []
    n, _ = _pull(grid_db, tmp_path, weeks=(6,), retrieved_as_of=_SETTLED_6, calls=calls)
    assert n == 9 and calls == [("regular", SEASON, 6)]
    assert {r["knowable_as_of"] for r in _rows(grid_db)} == {"2023-10-16"}
    assert {r["retrieved_as_of"] for r in _rows(grid_db)} == {_SETTLED_6}


def test_only_the_regular_season_is_stamped(grid_db, tmp_path):
    with pytest.raises(ValueError, match="season_type='regular'"):
        so.pull_sleeper_ownership(grid_db, SEASON, retrieved_as_of=PULL_DAY,
                                  cache_dir=tmp_path, season_type="post")
    with pytest.raises(ValueError, match="season_type"):
        so.ingest_sleeper_ownership(grid_db, {6: _fixture(6)}, season=SEASON,
                                    season_type="playoffs", retrieved_as_of=PULL_DAY,
                                    knowable_by_week={6: "2023-10-16"})


def test_a_week_with_no_knowable_day_is_refused(grid_db):
    with pytest.raises(ValueError, match="no knowable_as_of"):
        so.ingest_sleeper_ownership(grid_db, {9: _fixture(6)}, season=SEASON,
                                    season_type="regular", retrieved_as_of=PULL_DAY,
                                    knowable_by_week={6: "2023-10-16"})
    assert _count(grid_db) == 0


def test_zero_surviving_rows_raises_and_writes_nothing(grid_db):
    """'wrote 0 rows' is never ok — and per WEEK: a week that is all IDP among
    full ones would otherwise read as 'nobody owned anyone'."""
    idp_only = {"900004": {"owned": 63.0, "started": 30.0}}
    with pytest.raises(so.OwnershipCollapse, match="none survived"):
        so.ingest_sleeper_ownership(grid_db, {6: _fixture(6), 7: idp_only}, season=SEASON,
                                    season_type="regular", retrieved_as_of=PULL_DAY,
                                    knowable_by_week={6: "2023-10-16", 7: "2023-10-23"})
    assert _count(grid_db) == 0


@pytest.mark.parametrize("payload", [
    ["a", "list"],
    {"900001": 99.6},
    {"900001": {"owned": "99.6"}},
    {"900001": {"owned": True}},
    {"900001": {"owned": 99.6, "started": "97"}},
    {"900001": {"rostered": 99.6}},
    {"900001": {"owned": 0.996, "started": 0.97}, "900002": {"owned": 0.5, "started": 0.1}},
    {"900001": {"owned": 9960.0, "started": 9700.0}},
    {"900001": {"owned": -5.0, "started": 1.0}},
    {},
])
def test_schema_drift_raises_before_any_write(grid_db, payload):
    with pytest.raises(so.OwnershipSchemaDrift):
        so.ingest_sleeper_ownership(grid_db, {6: payload}, season=SEASON,
                                    season_type="regular", retrieved_as_of=PULL_DAY,
                                    knowable_by_week={6: "2023-10-16"})
    assert _count(grid_db) == 0


def _wide_payload(n=100, bad=()):
    """``n`` unresolved ids at a spread of owned%, with the ids in ``bad`` served
    the way 2022 wk17 served id 4166 live: ``started`` present, no ``owned``."""
    payload = {str(700000 + i): {"owned": 1.0 + (i * 0.9) % 99.0, "started": (0.5 * i) % 100.0}
               for i in range(n)}
    for i in bad:
        payload[str(700000 + i)] = {"started": 0.1}
    return payload


def test_one_key_without_owned_is_an_anomaly_dropped_with_a_note(grid_db, tmp_path, caplog):
    """The shape measured live on 2022 wk17: ONE key of ~630 with no `owned`. A
    strict check refused the whole season for it. It is dropped — a missing
    value is not a zero and owned_pct is NOT NULL — the drop is logged at
    WARNING (not by design), the other 99 land, and the RAW payload, bad key
    included, is what gets frozen (the freeze is the archive)."""
    payload = _wide_payload(bad=(7,))
    assert so.validate_payload(payload, where="t") == frozenset({"700007"})

    def fetch(season_type, season, week, *, sleep):
        return payload

    with caplog.at_level("WARNING", logger="ziggurat.data.nfl"):
        n, _ = _pull(grid_db, tmp_path, weeks=(6,), fetch=fetch)
    assert n == 99
    assert {r["sleeper_id"] for r in _rows(grid_db)} == set(payload) - {"700007"}
    assert any("dropped 1/100 rows" in m and "no numeric `owned`" in m
               for m in caplog.messages), caplog.messages
    frozen = json.loads((tmp_path / "regular-2023-wk06.json").read_text())
    assert frozen["700007"] == {"started": 0.1}


def test_one_out_of_range_key_is_an_anomaly_dropped_with_a_note(grid_db, tmp_path, caplog):
    """A numeric ``owned`` outside 0..100 is not a percent. It is dropped — never
    clamped, never stored — with the first offender named in the unit's terms."""
    payload = _wide_payload()
    payload["700007"] = {"owned": 150.0, "started": 3.0}
    assert so.validate_payload(payload, where="t") == frozenset({"700007"})
    payload["700008"] = {"owned": 12.0, "started": -0.5}
    assert so.validate_payload(payload, where="t") == frozenset({"700007", "700008"})

    def fetch(season_type, season, week, *, sleep):
        return payload

    with caplog.at_level("WARNING", logger="ziggurat.data.nfl"):
        n, _ = _pull(grid_db, tmp_path, weeks=(6,), fetch=fetch)
    assert n == 98
    stored = {r["sleeper_id"]: r["owned_pct"] for r in _rows(grid_db)}
    assert "700007" not in stored and "700008" not in stored
    assert all(0.0 <= v <= 100.0 for v in stored.values())
    assert any("dropped 2/100 rows" in m and "0..100" in m for m in caplog.messages)


@pytest.mark.parametrize("shape, needle", [
    ("inflated", "outside 0..100 percent"),
    ("negative", "outside 0..100 percent"),
    ("rescaled", "a 0..1 rescale, not a percent"),
])
def test_a_rescaled_or_negative_unit_is_drift_not_a_bigger_number(grid_db, tmp_path, shape, needle):
    """A unit change on every key is drift, never a bigger (or smaller) number.
    Out of range (x100, negative) trips the anomaly ceiling naming the first
    offender. A 0..1 rescale sits INSIDE 0..100 — the range check cannot see
    it — so the floor guard is what refuses it: upstream omits keys at or below
    ~1%, so a published week cannot consist entirely of them. Nothing is
    clamped, rescaled or frozen in any case."""
    payload = _wide_payload()
    for value in payload.values():
        if shape == "rescaled":
            value["owned"] = value["owned"] / 100.0
            value["started"] = value["started"] / 100.0
        elif shape == "inflated":
            value["owned"] = value["owned"] * 100.0
        else:
            value["owned"] = -5.0
    with pytest.raises(so.OwnershipSchemaDrift, match=needle):
        _pull(grid_db, tmp_path, weeks=(6,), fetch=lambda *a, **k: payload)
    assert _count(grid_db) == 0 and list(tmp_path.iterdir()) == []
    # The real archive is nowhere near the guard: every frozen week tops out
    # above 97 owned, so a week whose top key is 2.0 is still published.
    fine = _wide_payload(n=3)
    fine["700002"]["owned"] = 2.0
    assert so.validate_payload(fine, where="t") == frozenset()


def test_every_key_missing_started_is_drift_not_optional(grid_db, tmp_path):
    """``started`` is optional on a trickle of keys, not on all of them: a
    renamed field takes every key with it, and the old rule would have stored
    a whole season of NULL started_pct without a word."""
    payload = {k: {"owned": v["owned"]} for k, v in _wide_payload().items()}
    with pytest.raises(so.OwnershipSchemaDrift, match=r"absent on 100 of 100 keys \(100%\)"):
        so.validate_payload(payload, where="t")
    with pytest.raises(so.OwnershipSchemaDrift, match="renamed field"):
        _pull(grid_db, tmp_path, weeks=(6,), fetch=lambda *a, **k: payload)
    assert _count(grid_db) == 0 and list(tmp_path.iterdir()) == []
    # Exactly half absent is still allowed; one more is drift.
    half = _wide_payload()
    for key in list(half)[:50]:
        del half[key]["started"]
    assert so.validate_payload(half, where="t") == frozenset()
    del half[list(half)[50]]["started"]
    with pytest.raises(so.OwnershipSchemaDrift, match="absent on 51 of 100"):
        so.validate_payload(half, where="t")


def test_a_trickle_of_missing_started_is_still_optional(grid_db, tmp_path):
    """The measured maximum: 2.2% of a 640-key week (2022 wk16, 14 of 638). Kept,
    NULL started_pct, no drift — and the note counts KEPT rows only."""
    payload = _wide_payload(n=640)
    for key in list(payload)[:14]:
        del payload[key]["started"]
    assert so.validate_payload(payload, where="t") == frozenset()
    n, _ = _pull(grid_db, tmp_path, weeks=(6,), fetch=lambda *a, **k: payload)
    assert n == 640
    assert sum(r["started_pct"] is None for r in _rows(grid_db)) == 14


def test_the_no_started_note_counts_kept_rows_only(grid_db, tmp_path, caplog):
    """A filtered IDP key without `started` is not a kept row: the note used to
    print 'kept 10/9' — a numerator larger than its denominator."""
    payload = dict(_fixture(6))
    payload["900004"] = {"owned": 63.0}              # the IDP key, no started
    payload["900006"] = {"owned": 7.0}               # a kept key, no started
    with caplog.at_level("INFO", logger="ziggurat.data.nfl"):
        n, _ = _pull(grid_db, tmp_path, weeks=(6,), fetch=lambda *a, **k: payload)
    assert n == 9
    notes = [m for m in caplog.messages if "no `started`" in m]
    assert notes and "kept 1/9" in notes[0], caplog.messages


def test_more_than_the_allowance_is_drift_not_anomaly(grid_db, tmp_path):
    """Three of a hundred (3% > 2%): the shape moved. Nothing frozen, nothing
    written, the message names the count and the first offender."""
    payload = _wide_payload(bad=(1, 2, 3))

    def fetch(season_type, season, week, *, sleep):
        so.validate_payload(payload, where="live")   # what fetch_research does
        return payload

    with pytest.raises(so.OwnershipSchemaDrift, match=r"3 of 100 keys .* '700001'"):
        _pull(grid_db, tmp_path, weeks=(6,), fetch=fetch)
    assert _count(grid_db) == 0 and list(tmp_path.iterdir()) == []
    # Exactly at the allowance (2 of 100) is still an anomaly.
    assert len(so.validate_payload(_wide_payload(bad=(1, 2)), where="t")) == 2


def test_an_empty_map_is_drift_and_never_frozen(grid_db, tmp_path):
    """The 3.2c brick shape, closed: a blank ``{}`` is not a published week and
    not a fact. It raises drift, nothing is frozen, and — the point — the NEXT
    pull makes a second network call instead of loading a frozen blank."""
    with pytest.raises(so.OwnershipSchemaDrift, match="empty object"):
        so.validate_payload({}, where="x")
    calls = []
    served = {"blank": True}

    def fetch(season_type, season, week, *, sleep):
        calls.append(week)
        return {} if served["blank"] else _fixture(week)

    with pytest.raises(so.OwnershipSchemaDrift, match="empty object"):
        _pull(grid_db, tmp_path, weeks=(7,), fetch=fetch)
    assert list(tmp_path.iterdir()) == [] and _count(grid_db) == 0
    served["blank"] = False
    n, _ = _pull(grid_db, tmp_path, weeks=(7,), fetch=fetch, retrieved_as_of="2026-09-02")
    assert n == 10 and calls == [7, 7]


def test_a_frozen_empty_file_names_its_path(grid_db, tmp_path):
    """A blank that DID reach disk (a hand edit, an older build) is refused by
    name — the message tells the operator which file to remove."""
    path = Path(so.frozen_path(tmp_path, season_type="regular", season=SEASON, week=6))
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(so.OwnershipSchemaDrift, match="empty object") as info:
        _pull(grid_db, tmp_path, weeks=(6,))
    assert str(path) in str(info.value)
    assert _count(grid_db) == 0


def test_the_registry_recovers_the_day_after_a_blank_response(grid_db, tmp_path, monkeypatch):
    """Through the registry: day 1 serves ``{}`` (a failed run, nothing frozen);
    day 2 serves the real weeks and lands them both. Before SLEEP-2 the blank
    was frozen and every later day loaded it from disk."""
    monkeypatch.setattr(refresh, "SLEEPER_RESEARCH_DIR", tmp_path)
    day = {"n": 1}

    def fetch(season_type, season, week, *, sleep):
        return {} if day["n"] == 1 else _fixture(week)

    monkeypatch.setattr(so, "fetch_research", fetch)
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    first = refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                               retrieved_as_of=_SETTLED_7, today=_SETTLED_7)
    assert first[0]["status"] == refresh.STATUS_FAILED and "empty object" in first[0]["reason"]
    assert list(tmp_path.iterdir()) == [] and _count(grid_db) == 0
    day["n"] = 2
    second = refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                                retrieved_as_of="2023-11-01", today="2023-11-01")
    assert second[0]["status"] == refresh.STATUS_OK, second
    assert {r["week"] for r in _rows(grid_db)} == {6, 7}


# ---------------------------------------------------------------- the floor


def test_a_truncated_repull_is_refused_before_the_write(grid_db, tmp_path):
    """Mutation-verified floor. Week 6 stores 9 keys; a re-pull carrying 3 must
    be refused BEFORE the write (row count unchanged, no new version), because
    merely arriving later is enough to shadow the good rows."""
    _pull(grid_db, tmp_path, weeks=(6,))
    before = _count(grid_db)
    truncated = {k: v for k, v in list(_fixture(6).items())[:3]}

    def short(season_type, season, week, *, sleep):
        return truncated

    with pytest.raises(so.OwnershipCollapse, match="Refusing to write"):
        _pull(grid_db, tmp_path / "second-freeze", weeks=(6,), fetch=short,
              retrieved_as_of="2026-09-02")
    assert _count(grid_db) == before
    assert {r["retrieved_as_of"] for r in _rows(grid_db)} == {PULL_DAY}


def test_the_floor_is_per_week_not_only_per_season(grid_db, tmp_path):
    """A single half-served week is ~5% of a season's keys and would clear a
    season-level floor while still replacing a good week with a bad one."""
    _pull(grid_db, tmp_path, weeks=(6, 7))
    before = _count(grid_db)
    payloads = {6: _fixture(6), 7: {k: v for k, v in list(_fixture(7).items())[:2]}}
    with pytest.raises(so.OwnershipCollapse, match="week 7"):
        so.ingest_sleeper_ownership(grid_db, payloads, season=SEASON,
                                    season_type="regular", retrieved_as_of="2026-09-02",
                                    knowable_by_week={6: "2023-10-16", 7: "2023-10-23"})
    assert _count(grid_db) == before


def test_a_narrowed_repull_is_measured_against_its_own_weeks(grid_db, tmp_path):
    """Like for like: a run scoped to week 6 is not compared to two stored weeks."""
    _pull(grid_db, tmp_path, weeks=(6, 7))
    n, _ = _pull(grid_db, tmp_path, weeks=(6,), retrieved_as_of="2026-09-02")
    assert n == 9


# ---------------------------------------------------------------- leakage


def test_the_default_historical_view_hides_bulk_loaded_history(grid_db, tmp_path):
    """The footgun ``base.latest_truth`` exists for. Every row carries the 2026
    pull day, so a 2023 read under the safe default returns NOTHING — silently,
    reading exactly like 'nobody owned anyone that year'."""
    _pull(grid_db, tmp_path)
    assert so.get_sleeper_ownership(grid_db, as_of="2023-10-20") == []
    assert len(so.get_sleeper_ownership(grid_db, as_of="2023-10-20", view="latest_truth")) == 9
    # And the plain view DOES serve it once the pull day itself has passed.
    assert len(so.get_sleeper_ownership(grid_db, as_of=PULL_DAY)) == 19


def test_the_knowable_gate_holds_a_week_until_its_last_gameday(grid_db, tmp_path):
    """Week 6 ends Monday 10-16: a Sunday read sees nothing of it, Monday sees
    all of it, and week 7 stays invisible until its own Monday."""
    _pull(grid_db, tmp_path)
    get = base.latest_truth(so.get_sleeper_ownership)
    assert get(grid_db, as_of="2023-10-15") == []
    assert {r["week"] for r in get(grid_db, as_of="2023-10-16")} == {6}
    assert {r["week"] for r in get(grid_db, as_of="2023-10-22")} == {6}
    assert {r["week"] for r in get(grid_db, as_of="2023-10-23")} == {6, 7}


def test_a_newer_version_is_resolved_per_key_and_the_older_stays_visible_before_it(
    grid_db, tmp_path,
):
    """retrieved_as_of is the version axis: the re-pull's row wins on a later
    read, and a read BETWEEN the two pull days still sees the first version."""
    _pull(grid_db, tmp_path, weeks=(6,))
    edited = dict(_fixture(6))
    edited["900001"] = {"owned": 50.0, "started": 10.0}
    _pull(grid_db, tmp_path / "v2", weeks=(6,), retrieved_as_of="2026-09-03",
          fetch=lambda *a, **k: edited)
    v1 = so.get_sleeper_ownership(grid_db, as_of="2026-09-02", gsis_id="00-0090001")
    v2 = so.get_sleeper_ownership(grid_db, as_of="2026-09-03", gsis_id="00-0090001")
    assert [r["owned_pct"] for r in v1] == [99.6]
    assert [r["owned_pct"] for r in v2] == [50.0]


def test_accessor_requires_an_explicit_as_of(grid_db):
    with pytest.raises(TypeError):
        so.get_sleeper_ownership(grid_db)            # Rule 1: no implicit now
    with pytest.raises(TypeError):
        so.get_sleeper_ownership(grid_db, as_of=None)


def test_latest_truth_refuses_a_conflicting_view(grid_db):
    with pytest.raises(ValueError, match="conflicting view"):
        base.latest_truth(so.get_sleeper_ownership)(grid_db, as_of="2023-10-20",
                                                    view="historical")


def test_filters_match_the_stored_house_spelling(grid_db, tmp_path):
    _pull(grid_db, tmp_path)
    assert {r["sleeper_id"] for r in _rows(grid_db, position="K")} == {"900003"}
    assert _rows(grid_db, position="PK") == []
    assert {r["team"] for r in _rows(grid_db, position="DST", week=6)} == {"LA", "KC"}
    assert _rows(grid_db, season_type="post") == []


# ---------------------------------------------------------------- deltas


def test_deltas_impute_the_censoring_floor_and_say_so():
    prev = [{"sleeper_id": "a", "gsis_id": "g-a", "position": "RB", "team": None,
             "owned_pct": 88.0},
            {"sleeper_id": "gone", "gsis_id": "g-gone", "position": "WR", "team": None,
             "owned_pct": 5.0}]
    cur = [{"sleeper_id": "a", "gsis_id": "g-a", "position": "RB", "team": None,
            "owned_pct": 60.0},
           {"sleeper_id": "new", "gsis_id": None, "position": so.UNKNOWN_POSITION,
            "team": None, "owned_pct": 35.0}]
    out = so.ownership_deltas(prev, cur)
    by = {r["sleeper_id"]: r for r in out}
    assert by["a"]["delta"] == pytest.approx(-28.0) and by["a"]["censored"] is False
    # Absent before: at or below the floor, never 0.0, never dropped.
    assert by["new"]["prev_owned"] == so.CENSOR_FLOOR_PCT
    assert by["new"]["delta"] == pytest.approx(34.0) and by["new"]["censored"] is True
    assert by["new"]["gsis_id"] is None                      # unresolved rides along
    # Absent after: the same rule on the other side.
    assert by["gone"]["cur_owned"] == so.CENSOR_FLOOR_PCT
    assert by["gone"]["delta"] == pytest.approx(-4.0) and by["gone"]["censored"] is True
    assert [r["sleeper_id"] for r in out] == ["new", "gone", "a"]   # largest rise first


def test_deltas_on_the_fixture_grid_end_to_end(grid_db, tmp_path):
    _pull(grid_db, tmp_path)
    out = so.ownership_deltas(_rows(grid_db, week=6), _rows(grid_db, week=7))
    by = {r["sleeper_id"]: r for r in out}
    assert by["900009"]["censored"] is True and by["900009"]["delta"] == pytest.approx(34.0)
    assert by["900002"]["delta"] == pytest.approx(-28.0)
    assert by["LAR"]["team"] == "LA"


# ---------------------------------------------------------------- network seam


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "boom", hdrs=None, fp=None)


def test_the_network_seam_is_bounded_retries_transients_and_not_404(monkeypatch):
    from ziggurat import net

    seen = {"timeouts": [], "urls": []}
    answers = [_http_error("u", 503), urllib.error.URLError("reset"),
               _Resp(json.dumps({"900001": {"owned": 9.0, "started": 1.0}}).encode())]

    def fake_urlopen(request, timeout=None):
        seen["timeouts"].append(timeout)
        seen["urls"].append(request.full_url)
        nxt = answers.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(so.urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    payload = so.fetch_research("regular", 2023, 6, sleep=sleeps.append)
    assert payload == {"900001": {"owned": 9.0, "started": 1.0}}
    assert sleeps == list(so.RETRY_BACKOFF_S)
    assert set(seen["timeouts"]) == {net.HTTP_TIMEOUT}
    assert seen["urls"][0] == so.SLEEPER_RESEARCH_URL.format(
        season_type="regular", season=2023, week=6)

    # A 404 is an absence (defensive: the MEASURED absence signal is 200/null,
    # below): raised at once, never retried, and classified by the registry the
    # way a raw HTTPError 404 is.
    answers[:] = [_http_error("u", 404)]
    sleeps.clear()
    with pytest.raises(so.UpstreamAbsent) as info:
        so.fetch_research("regular", 2023, 6, sleep=sleeps.append)
    assert sleeps == []
    assert refresh._is_upstream_absent(info.value)

    # Retries are BOUNDED: three transients in a row surface the last one.
    answers[:] = [_http_error("u", 502), _http_error("u", 502), _http_error("u", 502)]
    sleeps.clear()
    with pytest.raises(urllib.error.HTTPError):
        so.fetch_research("regular", 2023, 6, sleep=sleeps.append)
    assert sleeps == list(so.RETRY_BACKOFF_S)


def test_a_null_body_is_the_measured_absence_signal(monkeypatch):
    """Measured 2026-09-01: an unpublished week answers HTTP 200 with body
    ``null`` — never a 404. It is an absence: UpstreamAbsent, no retry, and
    the registry classifies it as upstream_absent."""
    monkeypatch.setattr(so.urllib.request, "urlopen", lambda r, timeout=None: _Resp(b"null"))
    sleeps = []
    with pytest.raises(so.UpstreamAbsent, match="200/null") as info:
        so.fetch_research("regular", 2026, 3, sleep=sleeps.append)
    assert sleeps == [] and "not yet published" in str(info.value)
    assert refresh._is_upstream_absent(info.value)


def test_a_null_week_is_never_frozen_and_is_re_requested(grid_db, tmp_path):
    """Per week: ``null`` freezes nothing and stores nothing, so the next run
    asks again — a frozen null would have been a permanent hole."""
    calls = []
    served = {"null": True}

    def fetch(season_type, season, week, *, sleep):
        calls.append(week)
        return None if served["null"] else _fixture(week)

    with pytest.raises(so.UpstreamAbsent, match="every requested REG week") as info:
        _pull(grid_db, tmp_path, weeks=(7,), fetch=fetch)
    assert "nothing frozen, nothing written" in str(info.value)
    assert list(tmp_path.iterdir()) == [] and _count(grid_db) == 0
    served["null"] = False
    n, _ = _pull(grid_db, tmp_path, weeks=(7,), fetch=fetch)
    assert n == 10 and calls == [7, 7]


def test_a_partly_published_run_stores_what_exists_and_notes_the_rest(grid_db, tmp_path, monkeypatch):
    """Week 6 published, week 7 still ``null``: week 6 lands, the run is ok, the
    run log's free text names week 7 as not yet published, and the registry
    lists week 7 as NEW on the next day rather than as stored."""
    monkeypatch.setattr(refresh, "SLEEPER_RESEARCH_DIR", tmp_path)

    def fetch(season_type, season, week, *, sleep):
        return None if week == 7 else _fixture(week)

    monkeypatch.setattr(so, "fetch_research", fetch)
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    runs = refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                              retrieved_as_of=_SETTLED_7, today=_SETTLED_7)
    run = runs[0]
    assert run["status"] == refresh.STATUS_OK, run
    assert run["rows"] == 9 and {r["week"] for r in _rows(grid_db)} == {6}
    assert "weeks [7]" in run["reason"] and "not yet published" in run["reason"]
    assert "200/null" in run["reason"]
    assert not (tmp_path / so.frozen_path(tmp_path, season_type="regular",
                                          season=SEASON, week=7)).exists()
    assert refresh.sleeper_new_weeks(grid_db, season=SEASON, retrieved_as_of="2023-11-01") == [7]
    # The run log itself carries the note (the only free-text column).
    stored = grid_db.execute(
        "SELECT status, error FROM nfl_ingest_runs WHERE source = 'sleeper_ownership'"
    ).fetchall()
    assert len(stored) == 1 and stored[0][0] == refresh.STATUS_OK
    assert "not yet published" in (stored[0][1] or "")


def test_every_week_null_is_upstream_absent_not_a_failure(grid_db, tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "SLEEPER_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(so, "fetch_research", lambda *a, **k: None)
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    runs = refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                              retrieved_as_of=_SETTLED_6, today=_SETTLED_6)
    assert runs[0]["status"] == refresh.STATUS_ABSENT, runs
    assert _count(grid_db) == 0 and list(tmp_path.iterdir()) == []


def test_a_client_error_that_is_ours_is_not_retried(monkeypatch):
    def bad_request(request, timeout=None):
        raise _http_error("u", 400)

    monkeypatch.setattr(so.urllib.request, "urlopen", bad_request)
    sleeps = []
    with pytest.raises(urllib.error.HTTPError):
        so.fetch_research("regular", 2023, 6, sleep=sleeps.append)
    assert sleeps == []


def test_fetch_refuses_an_unknown_season_type():
    with pytest.raises(ValueError, match="season_type"):
        so.fetch_research("spring", 2023, 6)


# ---------------------------------------------------------------- registry


def test_both_market_archives_are_registered_and_backfillable():
    for name, table in (("sleeper_ownership", "sleeper_ownership"), ("fpecr", "fpecr_panel")):
        spec = refresh.SOURCES_BY_NAME[name]
        assert name in refresh.BACKFILL_SOURCES
        assert refresh._BACKFILL_TABLES[name] == table
        assert refresh.backfill_spec(name) is spec
        assert spec.needs_schedules and not spec.perishable
        assert not spec.needs_credentials and not spec.replaces_partition
        assert "latest_truth" in spec.notes                     # Rule 7: the read rule is stated
    order = [s.name for s in refresh.SOURCES]
    assert order.index("schedules") < order.index("fpecr") < order.index("sleeper_ownership")
    assert refresh.SOURCES_BY_NAME["sleeper_ownership"].applicable is not None
    assert refresh.SOURCES_BY_NAME["fpecr"].interval_days >= 7    # a monthly-ish mirror
    sleeper = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    assert sleeper.interval_days == 1 and sleeper.freshness_days == 7      # retry daily, judge weekly
    assert "Backfillable 2021-2025" in sleeper.notes


def test_the_registry_wording_states_the_hypothesis_not_a_false_measurement():
    """SLEEP-1: the notes used to promise the week was frozen once its last
    gameday had passed — settlement was never measured. The registry now names
    SETTLE_DAYS as a hypothesis and 200/null as the absence signal."""
    notes = refresh.SOURCES_BY_NAME["sleeper_ownership"].notes
    assert "SETTLE_DAYS" in notes and "unmeasured" in notes and "200/null" in notes
    assert "only frozen once the week's last gameday has passed" not in notes
    doc = so.__doc__
    assert "SETTLE_DAYS" in doc and "immutable upstream by construction" not in doc
    assert "never frozen half-formed" not in doc
    assert so.KNOWABLE_BASIS and "SETTLE_DAYS" in so.KNOWABLE_BASIS


def test_freshness_days_is_never_shorter_than_the_retry_interval():
    """`freshness_days` widens the staleness judgement; it can never tighten it
    below the retry interval, or a source would read EXPIRED before its own
    next scheduled attempt."""
    for spec in refresh.SOURCES:
        assert spec.freshness_days is None or spec.freshness_days >= spec.interval_days, spec.name


def test_a_daily_retry_with_weekly_freshness_does_not_read_expired_midweek(grid_db, tmp_path,
                                                                            monkeypatch):
    """SLEEP-7: a source pulled Tuesday and correctly skipping Wednesday through
    Monday must NOT read EXPIRED on Thursday. Freshness is judged on the 7-day
    window; the 1-day interval only decides when to retry."""
    monkeypatch.setattr(refresh, "SLEEPER_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(so, "fetch_research", _fetch_from_fixtures())
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    runs = refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                              retrieved_as_of=_SETTLED_6, today=_SETTLED_6)
    assert runs[0]["status"] == refresh.STATUS_OK
    # Wed-Mon the applicable gate hides the age (n/a: week 7 is settling); the
    # next Tuesday the gate opens on week 7 at age 7 — FRESH, not expired; a
    # missed Tuesday reads stale the next day, expired only after ~3 weeks.
    for day, expect in (("2023-10-25", "n/a"), ("2023-10-26", "n/a"),
                        ("2023-10-30", "n/a"), (_SETTLED_7, "fresh"),
                        ("2023-11-01", "stale"), ("2023-11-14", "stale"),
                        ("2023-11-15", "expired")):
        rows = {r["source"]: r for r in refresh.source_freshness(grid_db, season=SEASON, today=day)}
        row = rows["sleeper_ownership"]
        assert row["freshness_days"] == 7 and row["interval_days"] == 1
        assert row["verdict"] == expect, (day, row)
        text = refresh.format_status(grid_db, season=SEASON, today=day)
        if expect != "expired":
            assert "EXPIRED" not in text, (day, text)
    text = refresh.format_status(grid_db, season=SEASON, today="2023-11-01")
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("sleeper_ownership"))
    assert "(cadence 7d, retry 1d)" in line and "(interval 1d)" not in line, line


def test_the_run_log_names_the_anomaly_not_unstampable(grid_db, tmp_path, monkeypatch):
    """OPS-5: a dropped key is dropped for a stated reason. The run log's loss
    detail used to say 'unstampable' for every drop — the schedules diagnosis —
    even when the row had a perfectly good stamp and a bad `owned`."""
    monkeypatch.setattr(refresh, "SLEEPER_RESEARCH_DIR", tmp_path)
    payload = _wide_payload(bad=(7,))               # one key with no owned: an anomaly
    monkeypatch.setattr(so, "fetch_research", lambda *a, **k: payload)
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    runs = refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                              retrieved_as_of=_SETTLED_6, today=_SETTLED_6)
    run = runs[0]
    assert run["status"] == refresh.STATUS_PARTIAL and run["rows"] == 99, run
    assert "1 dropped: no numeric `owned` in 0..100" in run["reason"], run["reason"]
    assert "unstampable" not in run["reason"]


def test_the_archives_live_under_the_gitignored_data_tree():
    """Rule 5: harvested ownership/rankings never enter the repo."""
    from ziggurat.paths import REPO_ROOT
    assert refresh.SLEEPER_RESEARCH_DIR == REPO_ROOT / "data" / "backtest" / "sleeper-research"
    assert Path(refresh.fpecr_mirror_path("2026-09-01")) == (
        REPO_ROOT / "data" / "backtest" / "db_fpecr-2026-09-01.parquet")
    gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert "/data/" in gitignore


def _ctx(db, *, retrieved_as_of, force=False):
    return refresh.IngestContext(conn=db, season=SEASON, retrieved_as_of=retrieved_as_of,
                                 today=retrieved_as_of, force=force)


def test_the_applicable_gate_names_why_there_is_nothing_to_pull(grid_db, tmp_path):
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    # Before any week has finished: a reason, not a failure.
    why = spec.applicable(_ctx(grid_db, retrieved_as_of="2023-10-14"))
    assert why and "no REG week" in why and "not a failure" in why
    # Week 6 finished but still settling (OPS-1): a correct skip that NAMES the
    # week, the day it becomes fetchable, and the hypothesis — never "dead".
    why = spec.applicable(_ctx(grid_db, retrieved_as_of="2023-10-17"))
    assert why and "not a failure" in why and "still settling" in why
    assert "REG week 6" in why and f"fetched from {_SETTLED_6}" in why
    assert f"SETTLE_DAYS={so.SETTLE_DAYS}" in why and "unmeasured" in why
    assert refresh.sleeper_new_weeks(grid_db, season=SEASON, retrieved_as_of="2023-10-17") == []
    # Settled: pull.
    assert spec.applicable(_ctx(grid_db, retrieved_as_of=_SETTLED_6)) is None
    assert refresh.sleeper_new_weeks(grid_db, season=SEASON, retrieved_as_of=_SETTLED_6) == [6]
    # Once stored, the same day has nothing NEW — unless --force, which the
    # reason describes as the re-fetch-and-compare instrument it is.
    _pull(grid_db, tmp_path, weeks=(6,), retrieved_as_of=_SETTLED_6)
    why = spec.applicable(_ctx(grid_db, retrieved_as_of=_SETTLED_6))
    assert why and "already stored" in why and "--force" in why
    assert "re-fetches each frozen week live and reports any divergence" in why
    # With week 7 finished but settling, the same reason also names it.
    why = spec.applicable(_ctx(grid_db, retrieved_as_of="2023-10-25"))
    assert why and "already stored" in why and "REG week 7" in why and "still settling" in why
    assert spec.applicable(_ctx(grid_db, retrieved_as_of=_SETTLED_6, force=True)) is None
    assert refresh.sleeper_new_weeks(grid_db, season=SEASON, retrieved_as_of=_SETTLED_6,
                                     force=True) == [6]
    # A week later, week 7 has settled and the gate opens on its own.
    assert refresh.sleeper_new_weeks(grid_db, season=SEASON, retrieved_as_of=_SETTLED_7) == [7]
    assert spec.applicable(_ctx(grid_db, retrieved_as_of=_SETTLED_7)) is None


def test_the_daily_skip_reason_reaches_the_run_log(grid_db, tmp_path, monkeypatch):
    """The Tuesday-through-Monday skip is what the operator reads in
    ``ingest status``: it must carry the settling week and its fetch day, or
    seven consecutive skips read as a dead source."""
    monkeypatch.setattr(refresh, "SLEEPER_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(so, "fetch_research", _fetch_from_fixtures())
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    runs = refresh.run_ingest(grid_db, sources=[spec], season=SEASON,
                              retrieved_as_of="2023-10-18", today="2023-10-18")
    run = runs[0]
    assert run["status"] == refresh.STATUS_SKIPPED, run
    assert "still settling" in run["reason"] and _SETTLED_6 in run["reason"]
    assert _count(grid_db) == 0 and list(tmp_path.iterdir()) == []


def test_force_reaches_the_applicable_predicate_through_the_context(grid_db):
    """`decide()` checks `applicable` BEFORE the interval gate, the only gate
    force used to bypass — so force must ride the context or the daily re-pull
    of a stored season could never be asked for."""
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    seen = {}
    spec = replace(spec, applicable=lambda ctx: seen.setdefault("force", ctx.force) and None)
    refresh.decide(grid_db, spec, season=SEASON, today="2023-10-17",
                   have_credentials=False, force=True)
    assert seen["force"] is True


def test_the_pull_wrapper_loads_only_the_new_weeks(grid_db, tmp_path, monkeypatch):
    """The registry's pull loads sleeper_new_weeks(force) — never a week already
    stored on a plain run, every completed week under force."""
    monkeypatch.setattr(refresh, "SLEEPER_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(so, "fetch_research", _fetch_from_fixtures())
    monkeypatch.setattr(so.time, "sleep", lambda s: None)
    _pull(grid_db, tmp_path, weeks=(6,), retrieved_as_of=_SETTLED_6)
    spec = refresh.SOURCES_BY_NAME["sleeper_ownership"]
    assert "new REG weeks 7-7 (1)" == spec.scope(_ctx(grid_db, retrieved_as_of=_SETTLED_7))
    n = spec.pull(_ctx(grid_db, retrieved_as_of=_SETTLED_7))
    assert n == 10
    assert {r["week"] for r in _rows(grid_db)} == {6, 7}
    assert spec.scope(_ctx(grid_db, retrieved_as_of=_SETTLED_7)) == "no new completed week"
    forced = _ctx(grid_db, retrieved_as_of=_SETTLED_7, force=True)
    assert spec.scope(forced) == "REG weeks 6-7 (2): 2 stored re-versioned, 0 new"


def test_the_status_table_does_not_overflow_on_the_long_names(grid_db):
    """`sleeper_ownership` is 17 characters; the literal width of 14 that every
    report used put its verdict column three characters right of the others."""
    assert refresh.NAME_WIDTH >= max(len(s.name) for s in refresh.SOURCES)
    text = refresh.format_status(grid_db, season=2026, today="2026-09-01")
    body = [ln for ln in text.splitlines() if ln.startswith("  ") and len(ln) > 30
            and ln.split()[0] in refresh.SOURCES_BY_NAME]
    offsets = {ln.find(ln.split()[1], 2 + len(ln.split()[0])) for ln in body}
    assert len(body) >= 2 and len(offsets) == 1, offsets


def test_ingest_sources_lists_both_archives_via_the_cli():
    out = CliRunner().invoke(app, ["ingest", "sources"])
    assert out.exit_code == 0, out.output
    assert "sleeper_ownership" in out.output and "fpecr" in out.output


def test_the_fpecr_wrapper_pulls_only_the_context_season(monkeypatch, tmp_path):
    seen = {}

    def fake_pull(conn, *, retrieved_as_of, path, seasons=None, ecr_types=None, refresh=False):
        seen.update(retrieved_as_of=retrieved_as_of, path=path, seasons=seasons,
                    refresh=refresh)
        return 7

    monkeypatch.setattr(fpecr, "pull_fpecr", fake_pull)
    monkeypatch.setattr(refresh, "MARKET_ARCHIVE_DIR", tmp_path)
    ctx = refresh.IngestContext(conn=None, season=2024, retrieved_as_of="2026-09-01",
                                today="2026-09-01")
    assert refresh.SOURCES_BY_NAME["fpecr"].pull(ctx) == 7
    assert seen["seasons"] == [2024] and seen["refresh"] is False
    assert seen["path"] == str(tmp_path / "db_fpecr-2026-09-01.parquet")
    assert "fresh (~38 MB download)" in refresh.SOURCES_BY_NAME["fpecr"].scope(ctx)
    (tmp_path / "db_fpecr-2026-09-01.parquet").write_bytes(b"x")
    assert "existing mirror" in refresh.SOURCES_BY_NAME["fpecr"].scope(ctx)


def test_no_delete_and_no_scoring_numbers_in_the_module():
    src = Path(so.__file__).read_text(encoding="utf-8")
    assert "DELETE FROM" not in src
    assert "core.scoring" not in src and "from ziggurat.core" not in src   # Rule 2 boundary
    assert "ziggurat.draft" not in src                                       # Rule 8


def test_the_fixture_grid_is_invented(grid_db):
    """Rule 5 guard on the committed fixture: every player key is in the 9xxxxx
    block the test reserves, and no key resolves against nflverse's real id space."""
    for week in (6, 7):
        for key in _fixture(week):
            assert key.startswith("9000") or key in ("LAR", "KC"), key
    assert all(g.startswith(("00-009", "PLC")) for _, g, _, _ in _PLAYERS)
