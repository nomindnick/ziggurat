"""Maintained nflverse client adapter.

The project previously imported the deprecated ``nfl_data_py`` package, whose
published dependency constraints conflict with supported pandas and NumPy.
This module preserves the narrow import seam used by the ingestion modules while
routing downloads through nflverse's maintained ``nflreadpy`` client.
"""

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
