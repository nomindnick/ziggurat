"""Maintained nflverse client adapter.

The project previously imported the deprecated ``nfl_data_py`` package, whose
published dependency constraints conflict with supported pandas and NumPy.
This module preserves the narrow import seam used by the ingestion modules while
routing downloads through nflverse's maintained ``nflreadpy`` client.
"""

import json
import urllib.parse
import urllib.request
from collections.abc import Iterable
from importlib import import_module

import pandas as pd


def _client():
    try:
        return import_module("nflreadpy")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "nflreadpy is required for live NFL pulls; install the project dependencies"
        ) from exc


def _to_pandas(frame) -> pd.DataFrame:
    if isinstance(frame, pd.DataFrame):
        return frame
    converter = getattr(frame, "to_pandas", None)
    if converter is None:
        raise TypeError(f"nflreadpy returned unsupported frame type {type(frame)!r}")
    return converter()


def _seasons(years: Iterable[int]) -> list[int]:
    seasons = list(years)
    if not seasons or any(not isinstance(year, int) for year in seasons):
        raise ValueError("years must contain at least one integer season")
    return seasons


def import_ids() -> pd.DataFrame:
    return _to_pandas(_client().load_ff_playerids())


def import_schedules(years: Iterable[int]) -> pd.DataFrame:
    seasons = _seasons(years)
    return _to_pandas(_client().load_schedules(seasons=seasons))


def import_weekly_data(years: Iterable[int]) -> pd.DataFrame:
    seasons = _seasons(years)
    frame = _to_pandas(_client().load_player_stats(seasons=seasons, summary_level="week"))
    # nflreadpy follows the current nflverse dictionary; the storage/scoring
    # contract retains the stable historical names used throughout Ziggurat.
    aliases = {
        "team": "recent_team",
        "passing_interceptions": "interceptions",
        "sacks_suffered": "sacks",
    }
    renames = {
        source: target
        for source, target in aliases.items()
        if source in frame.columns and target not in frame.columns
    }
    return frame.rename(columns=renames)


def import_snap_counts(years: Iterable[int]) -> pd.DataFrame:
    seasons = _seasons(years)
    return _to_pandas(_client().load_snap_counts(seasons=seasons))


def import_ngs_data(stat_type: str, years: Iterable[int]) -> pd.DataFrame:
    seasons = _seasons(years)
    return _to_pandas(_client().load_nextgen_stats(seasons=seasons, stat_type=stat_type))


def import_depth_charts(years: Iterable[int]) -> pd.DataFrame:
    seasons = _seasons(years)
    return _to_pandas(_client().load_depth_charts(seasons=seasons))


def import_injuries(years: Iterable[int]) -> pd.DataFrame:
    seasons = _seasons(years)
    return _to_pandas(_client().load_injuries(seasons=seasons))


def import_team_stats(years: Iterable[int]) -> pd.DataFrame:
    """Weekly team stat grid (item 1.5 team_defense). ``team`` is already the
    schedules-style abbr here (unlike ``import_weekly_data``), so no rename."""
    seasons = _seasons(years)
    return _to_pandas(_client().load_team_stats(seasons=seasons, summary_level="week"))


def import_game_odds(years: Iterable[int]) -> pd.DataFrame:
    """Vegas odds columns ride the schedules frame; this is a SEPARATE seam from
    ``import_schedules`` so item 1.5's game_odds tests patch their own point."""
    seasons = _seasons(years)
    return _to_pandas(_client().load_schedules(seasons=seasons))


def import_ff_rankings() -> pd.DataFrame:
    """FantasyPros ECR market rankings (item 1.5 adp_rankings). Current scrape
    only — a weekly panel comes from pulling every week or a Phase-4 backfill."""
    return _to_pandas(_client().load_ff_rankings())


# The Sleeper projections positions we request (scoring positions only; the feed
# also returns FB/CB/P rows that the mapper filters out).
_SLEEPER_PROJECTION_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


def import_sleeper_projections(season, week, season_type: str = "regular"):
    """The ONE projections network seam (item 1.5). Undocumented Sleeper endpoint
    ``GET https://api.sleeper.com/projections/nfl/{season}/{week}`` returning the
    parsed JSON list. Tests patch this function; no live call runs offline."""
    base_url = f"https://api.sleeper.com/projections/nfl/{season}/{week}"
    query = [("season_type", season_type)]
    query += [("position[]", pos) for pos in _SLEEPER_PROJECTION_POSITIONS]
    url = f"{base_url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "ziggurat/1.5"})
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (fixed https host)
        return json.loads(resp.read().decode("utf-8"))
