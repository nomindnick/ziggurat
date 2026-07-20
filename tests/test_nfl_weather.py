"""Cached-fixture + leakage tests for game_weather ingestion (item 1.5).

The ONE network seam is ``weather.fetch_open_meteo``; every test patches it, so
no HTTP call runs offline. Fixed-dome games must NOT fetch at all.
"""

import pytest

from ziggurat.data.nfl import base, schedules, weather


def _hourly_day(gameday, *, base_temp=50.0, with_prob=True):
    """A full-day hourly payload; hour i carries deterministic, distinct values
    so a test can prove the kickoff-hour index (not hour 0) was selected."""
    times = [f"{gameday}T{h:02d}:00" for h in range(24)]
    hourly = {
        "time": times,
        "temperature_2m": [base_temp + h for h in range(24)],
        "wind_speed_10m": [5.0 + h for h in range(24)],
        "precipitation": [round(0.1 * h, 2) for h in range(24)],
    }
    if with_prob:
        hourly["precipitation_probability"] = [float(h) for h in range(24)]
    return {"hourly": hourly}


class _Recorder:
    """A patchable ``fetch_open_meteo`` that records calls and returns a payload."""

    def __init__(self, payload_fn):
        self.calls = []
        self._payload_fn = payload_fn

    def __call__(self, lat, lon, date, tz, *, mode):
        self.calls.append({"lat": lat, "lon": lon, "date": date, "tz": tz, "mode": mode})
        return self._payload_fn(date, mode)


def _game(game_id, stadium_id, gameday, *, season=2023, week=3, home_team="GB", gametime="13:00"):
    return {
        "game_id": game_id, "season": season, "week": week, "home_team": home_team,
        "stadium_id": stadium_id, "gameday": gameday, "gametime": gametime,
    }


# --------------------------------------------------------------------- fixtures

@pytest.fixture()
def patch_fetch(monkeypatch):
    """Install a recording fetch; the test controls the payload per (date, mode)."""
    def install(payload_fn=lambda date, mode: _hourly_day(date, with_prob=(mode == "forecast"))):
        rec = _Recorder(payload_fn)
        monkeypatch.setattr(weather, "fetch_open_meteo", rec)
        return rec
    return install


# ---------------------------------------------------------------- cached-fixture

def test_outdoor_game_fetched_at_kickoff_hour(db, patch_fetch):
    rec = patch_fetch()
    # GNB00 (Lambeau) is outdoor; 1pm kickoff -> hour index 13.
    game = _game("2023_03_NO_GB", "GNB00", "2023-09-24", gametime="13:00")
    n = weather.ingest_game_weather(db, [game], retrieved_as_of="2023-09-20", mode="forecast")
    assert n == 1
    assert len(rec.calls) == 1  # outdoor -> exactly one fetch
    assert rec.calls[0]["mode"] == "forecast"

    row = weather.get_game_weather(db, as_of="2023-09-24", game_id="2023_03_NO_GB")[0]
    assert row["weather_relevant"] == 1
    assert row["forecast_source"] == "forecast"
    # Hour-13 values from _hourly_day, NOT hour 0.
    assert row["temp_f"] == 63.0
    assert row["wind_mph"] == 18.0
    assert row["precip_mm"] == 1.3
    assert row["precip_prob"] == 13.0


def test_fixed_dome_stored_without_fetch(db, patch_fetch):
    rec = patch_fetch()
    # DET00 (Ford Field) is a fixed dome: weather_relevant=0 and NO fetch.
    game = _game("2023_02_SEA_DET", "DET00", "2023-09-17", home_team="DET", week=2)
    n = weather.ingest_game_weather(db, [game], retrieved_as_of="2023-09-14", mode="forecast")
    assert n == 1
    assert rec.calls == []  # dome -> no external fetch attempted

    row = weather.get_game_weather(db, as_of="2023-09-17", game_id="2023_02_SEA_DET")[0]
    assert row["weather_relevant"] == 0
    assert row["temp_f"] is None
    assert row["wind_mph"] is None
    assert row["precip_mm"] is None


def test_missing_stadium_is_dropped(db, patch_fetch, caplog):
    rec = patch_fetch()
    good = _game("2023_03_NO_GB", "GNB00", "2023-09-24")
    bad = _game("2023_99_XX_YY", "ZZZ99", "2023-09-24")  # not in _STADIUM_COORDS
    import logging
    with caplog.at_level(logging.WARNING, logger="ziggurat.data.nfl"):
        n = weather.ingest_game_weather(db, [good, bad], retrieved_as_of="2023-09-20", mode="forecast")
    assert n == 1  # only the resolvable game stored
    assert any("game_weather" in r.message and "dropped" in r.message for r in caplog.records)
    # The bad game is absent; only one fetch (for the resolvable venue) happened.
    assert len(rec.calls) == 1
    assert weather.get_game_weather(db, as_of="2023-09-24", game_id="2023_99_XX_YY") == []


# ------------------------------------------------------------------- leakage (a)

def test_archive_actual_hidden_before_gameday_under_both_views(db, patch_fetch):
    """An archive_actual is knowable=gameday: invisible at the Wednesday before
    under BOTH views; latest_truth (which relaxes only the retrieval gate) still
    blocks it Wednesday and surfaces it at as_of >= gameday."""
    patch_fetch()
    # Use an outdoor game so an archive fetch actually runs (GNB00). Sunday game.
    game = _game("2023_03_NO_GB", "GNB00", "2023-09-24")
    # Bulk backfill: retrieved LATER than the game (as a real backfill would be).
    weather.ingest_game_weather(db, [game], retrieved_as_of="2026-01-01", mode="archive")

    read = weather.get_game_weather
    gid = "2023_03_NO_GB"
    # Wednesday before: hidden under historical AND latest_truth (knowledge gate).
    assert read(db, as_of="2023-09-20", game_id=gid) == []
    assert base.latest_truth(read)(db, as_of="2023-09-20", game_id=gid) == []

    # historical alone still can't see it even at gameday (retrieved 2026 > 2023).
    assert read(db, as_of="2023-09-24", game_id=gid) == []
    # The correct grading read: latest_truth at as_of >= gameday returns it.
    got = base.latest_truth(read)(db, as_of="2023-09-24", game_id=gid)
    assert len(got) == 1
    assert got[0]["forecast_source"] == "archive_actual"
    assert got[0]["knowable_as_of"] == "2023-09-24"


# ------------------------------------------------------------------- leakage (b)

def test_forecast_refresh_revision(db, patch_fetch):
    """A Wednesday forecast then a refreshed Saturday forecast for the same game:
    as_of=Wed sees only the Wednesday issue; as_of=Sat sees the refreshed one."""
    temps = {"2023-09-20": 60.0, "2023-09-23": 40.0}  # pull-day -> base temp

    def payload_fn(date, mode):
        # date here is the gameday passed to fetch; branch on the recorded pull.
        return _hourly_day(date, base_temp=payload_fn.base, with_prob=True)
    payload_fn.base = 60.0

    gid = "2023_03_NO_GB"
    game = _game(gid, "GNB00", "2023-09-24")

    payload_fn.base = temps["2023-09-20"]
    rec1 = _Recorder(payload_fn)
    from unittest import mock
    with mock.patch.object(weather, "fetch_open_meteo", rec1):
        weather.ingest_game_weather(db, [game], retrieved_as_of="2023-09-20", mode="forecast")

    payload_fn.base = temps["2023-09-23"]
    rec2 = _Recorder(payload_fn)
    with mock.patch.object(weather, "fetch_open_meteo", rec2):
        weather.ingest_game_weather(db, [game], retrieved_as_of="2023-09-23", mode="forecast")

    # Wednesday read: only the 2023-09-20 forecast exists/retrieved -> temp 73 (60+13).
    wed = weather.get_game_weather(db, as_of="2023-09-20", game_id=gid)
    assert len(wed) == 1
    assert wed[0]["temp_f"] == 73.0
    assert wed[0]["knowable_as_of"] == "2023-09-20"

    # Saturday read: the refreshed 2023-09-23 forecast wins (MAX retrieved) -> 53 (40+13).
    sat = weather.get_game_weather(db, as_of="2023-09-23", game_id=gid)
    assert len(sat) == 1
    assert sat[0]["temp_f"] == 53.0
    assert sat[0]["knowable_as_of"] == "2023-09-23"


# ----------------------------------------------------------------- forecast vs archive keys

def test_forecast_and_archive_keep_independent_timelines(db, patch_fetch):
    """A forecast and an archive_actual for the same game are distinct keys and
    do not cross-contaminate the version pick."""
    patch_fetch()
    gid = "2023_03_NO_GB"
    game = _game(gid, "GNB00", "2023-09-24")
    weather.ingest_game_weather(db, [game], retrieved_as_of="2023-09-20", mode="forecast")
    weather.ingest_game_weather(db, [game], retrieved_as_of="2026-01-01", mode="archive")

    # At gameday, historical sees the forecast (retrieved 09-20 <= gameday) but
    # not the archive_actual (retrieved 2026 > gameday).
    rows = weather.get_game_weather(db, as_of="2023-09-24", game_id=gid)
    assert {r["forecast_source"] for r in rows} == {"forecast"}
    # Filtering by source works.
    fc = weather.get_game_weather(db, as_of="2023-09-24", game_id=gid, source="forecast")
    assert len(fc) == 1


# --------------------------------------------------------------- reference completeness

def test_every_schedule_stadium_resolves(db, nfl_fixture):
    """Every distinct stadium_id in the loaded schedules must resolve in the
    committed reference table (fails loud when nflverse adds a venue)."""
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    stadium_ids = [
        r["stadium_id"]
        for r in db.execute("SELECT DISTINCT stadium_id FROM schedules WHERE stadium_id IS NOT NULL")
    ]
    assert stadium_ids  # fixture actually has stadiums
    missing = sorted(s for s in stadium_ids if s not in weather._STADIUM_COORDS)
    assert missing == [], f"stadium_ids absent from _STADIUM_COORDS: {missing}"


# The full set of distinct stadium_ids across load_schedules 2020-2025 (36),
# enumerated at build time. Frozen here so dropping/renaming ANY venue — including
# the non-2023 international ones (GER00/MEX00/SAO00) the single-season schedules
# fixture never exercises — fails loud, not just the ones present in a fixture.
_EXPECTED_STADIUM_IDS = frozenset({
    "ATL97", "BAL00", "BOS00", "BUF00", "CAR00", "CHI98", "CIN00", "CLE00",
    "DAL00", "DEN00", "DET00", "FRA00", "GER00", "GNB00", "HOU00", "IND00",
    "JAX00", "KAN00", "LAX01", "LON00", "LON02", "MEX00", "MIA00", "MIN01",
    "NAS00", "NOR00", "NYC01", "PHI00", "PHO00", "PIT00", "SAO00", "SEA00",
    "SFO01", "TAM00", "VEG00", "WAS00",
})


def test_stadium_reference_covers_all_2020_2025_venues():
    """The committed reference table must carry exactly the known 2020-2025 venue
    set (guards the international venues absent from the single-season fixture)."""
    assert set(weather._STADIUM_COORDS) == _EXPECTED_STADIUM_IDS


def test_pull_reads_schedules_and_stores(db, nfl_fixture, patch_fetch):
    """pull_game_weather reads the ingested schedule and stores weather per game."""
    rec = patch_fetch()
    schedules.ingest_schedules(db, nfl_fixture("schedules"), retrieved_as_of="2023-08-01")
    n = weather.pull_game_weather(db, 2023, 1, retrieved_as_of="2023-09-05", mode="forecast")
    assert n > 0
    rows = weather.get_game_weather(db, as_of="2023-09-10", season=2023, week=1)
    assert len(rows) == n
    # Domes among week 1 (MIN01, NOR00, LAX01) stored weather_relevant=0, no fetch.
    dome_rows = [r for r in rows if r["weather_relevant"] == 0]
    assert dome_rows, "week 1 includes fixed-dome venues"
