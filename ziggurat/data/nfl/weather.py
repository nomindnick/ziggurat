"""Per-game weather context ingestion (item 1.5).

Weather is a decision *context* input, never a scoring input (no ``scoring.py``
contact). Two forecast regimes, distinguished by ``forecast_source`` so a
consumer can never mistake an ERA5 reanalysis actual for a pre-game forecast,
and each keeps its own version timeline:

  * ``forecast`` (live Open-Meteo Forecast API): a forecast is known when issued,
    so ``knowable_as_of = retrieved_as_of = pull day``. Each scheduled pull is a
    new row.
  * ``archive_actual`` (Open-Meteo Archive / ERA5 reanalysis): the actual
    game-hour weather, ``knowable_as_of = gameday`` (from schedules),
    ``retrieved_as_of = bulk-load day``. Physically cannot leak into a pre-game
    read — an ``archive_actual`` stamped at Sunday is invisible at ``as_of`` =
    the Wednesday before under BOTH views; grading reads it via
    ``base.latest_truth(get_game_weather)`` at ``as_of >= gameday`` (which relaxes
    only the retrieval gate, never the knowledge gate).

The stadium reference table (``_STADIUM_COORDS``) is committed PUBLIC stadium
reference data — lat/long/tz/dome for every venue nflverse schedules use — NOT
harvested league/colleague data. The harvested weather DATA stays in gitignored
``data/``. A fixed-dome game gets ``weather_relevant=0`` and NO external fetch;
retractable roofs are treated as outdoor (``weather_relevant=1``) because the
open/closed call is a near-kickoff decision unknowable at forecast time
(conservative — the consumer should down-weight). A game whose ``stadium_id`` is
absent from ``_STADIUM_COORDS`` is dropped via ``base.note_drops`` (surfaced by
the reference-completeness test when nflverse adds a venue).
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ziggurat import net
from ziggurat.data.nfl import base

# ------------------------------------------------------------------ stadium ref
# stadium_id -> (latitude, longitude, IANA timezone, is_fixed_dome).
# is_fixed_dome is True ONLY for permanently-enclosed roofs (no external weather
# ever); retractable-roof venues are False (treated as outdoor for forecasts).
# Covers every distinct stadium_id in load_schedules for 2020-2025 (incl. the
# international venues LON/GER/MEX/FRA/SAO). Public reference data — rule 5.
_STADIUM_COORDS: dict[str, tuple[float, float, str, bool]] = {
    "ATL97": (33.7554, -84.4009, "America/New_York", False),        # Mercedes-Benz (retractable)
    "BAL00": (39.2780, -76.6227, "America/New_York", False),        # M&T Bank
    "BOS00": (42.0909, -71.2643, "America/New_York", False),        # Gillette
    "BUF00": (42.7738, -78.7870, "America/New_York", False),        # Highmark/New Era
    "CAR00": (35.2258, -80.8528, "America/New_York", False),        # Bank of America
    "CHI98": (41.8623, -87.6167, "America/Chicago", False),         # Soldier Field
    "CIN00": (39.0954, -84.5160, "America/New_York", False),        # Paycor/Paul Brown
    "CLE00": (41.5061, -81.6995, "America/New_York", False),        # Cleveland Browns Stadium
    "DAL00": (32.7473, -97.0945, "America/Chicago", False),         # AT&T (retractable)
    "DEN00": (39.7439, -105.0201, "America/Denver", False),         # Empower Field
    "DET00": (42.3400, -83.0456, "America/New_York", True),         # Ford Field (fixed dome)
    "FRA00": (50.0686, 8.6455, "Europe/Berlin", False),            # Deutsche Bank Park (Frankfurt)
    "GER00": (48.2188, 11.6247, "Europe/Berlin", False),           # Allianz Arena (Munich)
    "GNB00": (44.5013, -88.0622, "America/Chicago", False),         # Lambeau Field
    "HOU00": (29.6847, -95.4107, "America/Chicago", False),         # NRG (retractable)
    "IND00": (39.7601, -86.1639, "America/Indiana/Indianapolis", False),  # Lucas Oil (retractable)
    "JAX00": (30.3239, -81.6373, "America/New_York", False),        # EverBank/TIAA Bank
    "KAN00": (39.0489, -94.4839, "America/Chicago", False),         # Arrowhead
    "LAX01": (33.9535, -118.3392, "America/Los_Angeles", True),     # SoFi (fixed canopy roof)
    "LON00": (51.5560, -0.2795, "Europe/London", False),           # Wembley (London)
    "LON02": (51.6043, -0.0665, "Europe/London", False),           # Tottenham Hotspur (London)
    "MEX00": (19.3029, -99.1505, "America/Mexico_City", False),    # Estadio Azteca (Mexico City)
    "MIA00": (25.9580, -80.2389, "America/New_York", False),        # Hard Rock
    "MIN01": (44.9736, -93.2575, "America/Chicago", True),          # U.S. Bank (fixed dome)
    "NAS00": (36.1665, -86.7713, "America/Chicago", False),         # Nissan
    "NOR00": (29.9511, -90.0812, "America/Chicago", True),          # Superdome (fixed dome)
    "NYC01": (40.8135, -74.0745, "America/New_York", False),        # MetLife
    "PHI00": (39.9008, -75.1675, "America/New_York", False),        # Lincoln Financial
    "PHO00": (33.5276, -112.2626, "America/Phoenix", False),        # State Farm (retractable)
    "PIT00": (40.4468, -80.0158, "America/New_York", False),        # Acrisure/Heinz
    "SAO00": (-23.5453, -46.4742, "America/Sao_Paulo", False),      # Arena Corinthians (Sao Paulo)
    "SEA00": (47.5952, -122.3316, "America/Los_Angeles", False),    # Lumen Field
    "SFO01": (37.4030, -121.9700, "America/Los_Angeles", False),    # Levi's
    "TAM00": (27.9759, -82.5033, "America/New_York", False),        # Raymond James
    "VEG00": (36.0909, -115.1830, "America/Los_Angeles", True),     # Allegiant (fixed dome)
    "WAS00": (38.9077, -76.8645, "America/New_York", False),        # FedExField
}

# db_column -> its default; the ordered set of columns each row carries.
_WEATHER_COLUMNS = (
    "game_id", "season", "week", "home_team", "stadium_id", "kickoff_local",
    "forecast_source", "weather_relevant", "temp_f", "wind_mph", "precip_mm",
    "precip_prob", "retrieved_as_of", "knowable_as_of",
)

# mode -> the forecast_source label persisted on the row.
_MODE_SOURCE = {"forecast": "forecast", "archive": "archive_actual"}

# nflverse schedules ``gametime`` is uniformly Eastern Time (verified: even
# international kickoffs are stamped in ET, e.g. a 15:30-local London game shows
# gametime "09:30"). So Open-Meteo is requested in ET and the ET gametime hour
# indexes the returned (ET-labelled) hourly array — the weather VALUES are for
# the stadium's lat/long regardless of the timezone label, which only sets the
# time axis. Requesting the stadium-LOCAL tz here (an earlier bug) misaligned the
# selected hour for every non-Eastern venue (1–3h domestic, 5–9h international).
_SCHEDULE_TZ = "America/New_York"


def fetch_open_meteo(lat, lon, date, tz, *, mode) -> dict:
    """The ONE HTTP seam tests patch. Fetch a single day's hourly weather.

    ``mode="forecast"`` -> ``api.open-meteo.com/v1/forecast`` (adds
    ``precipitation_probability``); ``mode="archive"`` ->
    ``archive-api.open-meteo.com/v1/archive`` (ERA5 reanalysis actuals). Hourly
    params: ``temperature_2m,wind_speed_10m,precipitation`` in Fahrenheit / mph /
    mm. ``tz`` must be the timezone the caller's kickoff hour is expressed in
    (Ziggurat passes ``_SCHEDULE_TZ`` = ET, since nflverse gametime is ET) so the
    returned hourly time axis aligns to the kickoff hour indexed against it.
    Fails loudly on HTTP error (urllib raises); never returns a partial row.
    """
    if mode == "forecast":
        host = "https://api.open-meteo.com/v1/forecast"
        hourly = "temperature_2m,wind_speed_10m,precipitation,precipitation_probability"
    elif mode == "archive":
        host = "https://archive-api.open-meteo.com/v1/archive"
        hourly = "temperature_2m,wind_speed_10m,precipitation"
    else:
        raise ValueError(f"unknown fetch mode {mode!r} (expected 'forecast' or 'archive')")

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date,
        "end_date": date,
        "hourly": hourly,
        "timezone": tz,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "mm",
    }
    url = f"{host}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "ziggurat/1.5"})
    # Bounded (item 3.1b): one call PER OUTDOOR GAME, so an unbounded urlopen is
    # ~13 chances a week to park the scheduled run under Type=oneshot.
    with urllib.request.urlopen(req, timeout=net.HTTP_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _kickoff_hour(gametime) -> int:
    """Hour-of-day (local) for a schedules ``gametime`` (e.g. ``"13:00"``)."""
    if gametime:
        try:
            return int(str(gametime)[:2])
        except (TypeError, ValueError):
            pass
    return 13  # sensible default kickoff hour when gametime is missing


def _game_hour_index(hourly_time, gameday, gametime) -> int:
    """Index into the hourly arrays for the kickoff hour (local time)."""
    hour = _kickoff_hour(gametime)
    target = f"{gameday}T{hour:02d}"
    for i, t in enumerate(hourly_time):
        if str(t)[:13] == target:
            return i
    # Fallback: match by hour-of-day if the exact day/hour string differs.
    for i, t in enumerate(hourly_time):
        if len(str(t)) >= 13 and str(t)[11:13] == f"{hour:02d}":
            return i
    if not hourly_time:
        raise ValueError("open-meteo returned no hourly data")
    return min(hour, len(hourly_time) - 1)


def _local_kickoff(gameday, gametime, stadium_tz) -> str | None:
    """Kickoff as a stadium-local ISO datetime string (context/display only).

    ``gameday``/``gametime`` are ET (nflverse convention); convert to the
    stadium's local wall clock so the stored ``kickoff_local`` is honestly local.
    Falls back to the raw ET ``gameday``/``gametime`` on any parse/zone error —
    this field feeds no leakage or scoring logic.
    """
    if not gameday:
        return None
    if not gametime:
        return gameday
    raw = f"{gameday}T{gametime}"
    try:
        et = datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(_SCHEDULE_TZ))
        return et.astimezone(ZoneInfo(stadium_tz)).isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        return raw


def _at(seq, idx):
    """Cell ``idx`` of an hourly array, coerced to a clean scalar (or None)."""
    if seq is None:
        return None
    try:
        return base._clean(seq[idx])
    except (IndexError, KeyError, TypeError):
        return None


def _build_row(game, *, mode, retrieved_as_of):
    """Build one game_weather row dict, fetching weather for outdoor games.

    Returns ``None`` when the game must be dropped (unresolvable stadium, or an
    archive_actual with no gameday to stamp a knowledge time).
    """
    stadium_id = game.get("stadium_id")
    coords = _STADIUM_COORDS.get(stadium_id)
    if coords is None:
        return None  # unresolvable venue -> caller drops via note_drops
    lat, lon, tz, is_fixed_dome = coords

    source = _MODE_SOURCE[mode]
    gameday = base.iso_date(game.get("gameday"))
    gametime = game.get("gametime")
    kickoff_local = _local_kickoff(gameday, gametime, tz)

    if source == "forecast":
        # A forecast is knowable the day it is issued (our pull day).
        knowable = base.iso_date(retrieved_as_of)
    else:
        # ERA5 actuals are knowable no earlier than the game itself.
        knowable = gameday
        if knowable is None:
            return None  # cannot stamp a leakage-safe knowledge time -> drop

    row = {
        "game_id": game.get("game_id"),
        "season": game.get("season"),
        "week": game.get("week"),
        "home_team": game.get("home_team"),
        "stadium_id": stadium_id,
        "kickoff_local": kickoff_local,
        "forecast_source": source,
        "retrieved_as_of": base.iso_date(retrieved_as_of),
        "knowable_as_of": knowable,
        "temp_f": None,
        "wind_mph": None,
        "precip_mm": None,
        "precip_prob": None,
    }

    if is_fixed_dome:
        # Fixed dome: weather is irrelevant and no external fetch is attempted.
        row["weather_relevant"] = 0
        return row

    row["weather_relevant"] = 1
    # Fetch in ET (the gametime timezone) so the ET kickoff hour indexes the
    # returned hourly array correctly; the weather values are for lat/lon.
    payload = fetch_open_meteo(lat, lon, gameday, _SCHEDULE_TZ, mode=mode)
    hourly = payload.get("hourly", {}) if isinstance(payload, dict) else {}
    times = hourly.get("time", []) or []
    idx = _game_hour_index(times, gameday, gametime)
    row["temp_f"] = _at(hourly.get("temperature_2m"), idx)
    row["wind_mph"] = _at(hourly.get("wind_speed_10m"), idx)
    row["precip_mm"] = _at(hourly.get("precipitation"), idx)
    if source == "forecast":
        row["precip_prob"] = _at(hourly.get("precipitation_probability"), idx)
    return row


def ingest_game_weather(conn, games, *, retrieved_as_of: str, mode: str) -> int:
    """Store weather rows for an iterable of schedule ``games`` (dict-like).

    Each game needs ``game_id, season, week, home_team, stadium_id, gameday`` and
    optionally ``gametime``. ``mode`` selects the regime ('forecast' live vs
    'archive' historical actuals). Fixed-dome games are stored with no fetch;
    unresolvable-stadium / no-gameday games are dropped via ``base.note_drops``.
    """
    if mode not in _MODE_SOURCE:
        raise ValueError(f"unknown mode {mode!r} (expected 'forecast' or 'archive')")

    games = list(games)
    rows = []
    for game in games:
        row = _build_row(game, mode=mode, retrieved_as_of=retrieved_as_of)
        if row is not None:
            rows.append(row)
    base.note_drops("game_weather", len(games) - len(rows), len(games),
                    why="unresolvable stadium or missing gameday")
    return base.upsert(conn, "game_weather", rows)


def pull_game_weather(conn, season, week, *, retrieved_as_of: str, mode: str) -> int:
    """Read the ingested schedule for (season, week) and fetch/store its weather.

    Schedules must be ingested first (the game context + gameday come from the
    schedules table). Delegates to ``ingest_game_weather`` (which owns the
    ``fetch_open_meteo`` seam)."""
    games = [
        dict(r)
        for r in conn.execute(
            "SELECT game_id, season, week, home_team, stadium_id, gameday, gametime "
            "FROM schedules WHERE season = ? AND week = ?",
            (season, week),
        )
    ]
    return ingest_game_weather(conn, games, retrieved_as_of=retrieved_as_of, mode=mode)


def get_game_weather(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    game_id=None,
    source=None,
    view: base.AsOfView = "historical",
):
    """Weather rows knowable on/before ``as_of`` (keyword-only; no implicit now).

    Keyed by ``(game_id, forecast_source)`` so a ``forecast`` and an
    ``archive_actual`` for the same game keep independent version timelines and
    never cross-contaminate the ``MAX(retrieved_as_of)`` version pick. Backtest
    / grading reads go through ``base.latest_truth(get_game_weather)``.
    """
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if game_id is not None:
        clauses.append("t.game_id = :game_id")
        params["game_id"] = game_id
    if source is not None:
        clauses.append("t.forecast_source = :source")
        params["source"] = source
    return base.select_as_of(
        conn, "game_weather", as_of=as_of,
        key_cols=["game_id", "forecast_source"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
