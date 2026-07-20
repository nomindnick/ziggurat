"""Contract tests for the maintained nflreadpy adapter (no network)."""

import pandas as pd
import pytest

from ziggurat.data.nfl import source


class FakePolarsFrame:
    def __init__(self, label):
        self.label = label

    def to_pandas(self):
        return pd.DataFrame({"source": [self.label]})


class FakeClient:
    def __init__(self):
        self.calls = []

    def _load(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return FakePolarsFrame(name)

    def load_ff_playerids(self):
        return self._load("ids")

    def load_schedules(self, **kwargs):
        return self._load("schedules", **kwargs)

    def load_player_stats(self, **kwargs):
        return self._load("weekly", **kwargs)

    def load_snap_counts(self, **kwargs):
        return self._load("snaps", **kwargs)

    def load_nextgen_stats(self, **kwargs):
        return self._load("ngs", **kwargs)

    def load_depth_charts(self, **kwargs):
        return self._load("depth", **kwargs)

    def load_injuries(self, **kwargs):
        return self._load("injuries", **kwargs)


def test_adapter_maps_legacy_ingestion_seams_to_nflreadpy(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(source, "_client", lambda: client)

    assert source.import_ids().iloc[0]["source"] == "ids"
    assert source.import_schedules([2023]).iloc[0]["source"] == "schedules"
    assert source.import_weekly_data([2023]).iloc[0]["source"] == "weekly"
    assert source.import_snap_counts([2023]).iloc[0]["source"] == "snaps"
    assert source.import_ngs_data("receiving", [2023]).iloc[0]["source"] == "ngs"
    assert source.import_depth_charts([2023]).iloc[0]["source"] == "depth"
    assert source.import_injuries([2023]).iloc[0]["source"] == "injuries"

    assert ("weekly", {"seasons": [2023], "summary_level": "week"}) in client.calls
    assert ("ngs", {"seasons": [2023], "stat_type": "receiving"}) in client.calls


def test_weekly_adapter_normalizes_current_nflverse_names(monkeypatch):
    class WeeklyClient:
        def load_player_stats(self, **kwargs):
            return pd.DataFrame(
                {
                    "team": ["BUF"],
                    "passing_interceptions": [1],
                    "sacks_suffered": [2],
                }
            )

    monkeypatch.setattr(source, "_client", lambda: WeeklyClient())
    frame = source.import_weekly_data([2023])
    assert {"recent_team", "interceptions", "sacks"} <= set(frame.columns)


def test_adapter_requires_nonempty_integer_seasons():
    with pytest.raises(ValueError, match="at least one integer"):
        source.import_schedules([])
    with pytest.raises(ValueError, match="at least one integer"):
        source.import_schedules(["2023"])


def test_adapter_rejects_unknown_frame_type():
    with pytest.raises(TypeError, match="unsupported frame type"):
        source._to_pandas(object())
