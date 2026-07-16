"""Cached-fixture contract test for ESPN league-settings access (spike 1.1).

`espn_api` authenticates against the private league and returns the full custom
scoring. This test guards the captured scoring snapshot *offline* — no network,
no cookies — so that item 1.3 transcribes `core/scoring.py` against a stable
fixture, and it establishes the cached-fixture pattern the 1.4/1.5 ingestion
clients copy. The live pull, decision table, and league-specific snapshot live
in `intel/research/espn-access.md` (gitignored). Regenerate the fixture by
re-running the spike probe; if ESPN changes its scoring schema, this test flags
it before it can silently corrupt scoring.

Scoring values are committable by constitution rule 2 (they live in
`core/scoring.py`); nothing colleague-identifying is present in `mSettings`.
"""

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "espn" / "scoring_format.json"


@pytest.fixture(scope="module")
def by_abbr():
    items = json.loads(FIXTURE.read_text())
    return {it["abbr"]: it for it in items}


def test_espn_api_dependency_importable():
    # Guards the dependency wired into pyproject in 1.1.
    from espn_api.football import League  # noqa: F401


def test_fixture_is_well_formed():
    items = json.loads(FIXTURE.read_text())
    assert len(items) == 46
    for it in items:
        assert {"id", "abbr", "label", "points"} <= set(it)
        assert isinstance(it["points"], (int, float))


def test_full_ppr(by_abbr):
    # Confirms the PLACEHOLDER 1.0/rec in scoring.py is league ground truth.
    assert by_abbr["REC"]["points"] == 1.0
    assert by_abbr["PY"]["points"] == pytest.approx(0.04)  # 1 pt / 25 pass yds
    assert by_abbr["RY"]["points"] == pytest.approx(0.1)
    assert by_abbr["REY"]["points"] == pytest.approx(0.1)
    assert by_abbr["PTD"]["points"] == 4.0
    assert by_abbr["INTT"]["points"] == -2.0
    assert by_abbr["FUML"]["points"] == -2.0


def test_distance_based_kicker(by_abbr):
    # House rule: FG value rises with distance, and each miss is a penalty.
    assert by_abbr["FG0"]["points"] == 3.0   # 0-39
    assert by_abbr["FG40"]["points"] == 4.0  # 40-49
    assert by_abbr["FG50"]["points"] == 5.0  # 50-59
    assert by_abbr["FG60"]["points"] == 6.0  # 60+
    assert by_abbr["PAT"]["points"] == 1.0
    assert by_abbr["FGM"]["points"] == -1.0  # each FG missed


def test_dst_points_allowed_brackets(by_abbr):
    assert by_abbr["PA0"]["points"] == 5.0
    assert by_abbr["PA46"]["points"] == -5.0
    # PA<digits> are the D/ST brackets; "PAT" (kicker PAT) also starts with "PA",
    # so filter on the trailing digits. ESPN lists only non-zero brackets, so
    # 18-27 pts is an implicit zero (the 1.3 gotcha).
    assert {a for a in by_abbr if a.startswith("PA") and a[2:].isdigit()} == {
        "PA0", "PA1", "PA7", "PA14", "PA28", "PA35", "PA46"
    }


def test_dst_yards_allowed_brackets(by_abbr):
    # The distinctive house rule most default leagues lack.
    assert by_abbr["YA100"]["points"] == 5.0   # < 100 total yds
    assert by_abbr["YA550"]["points"] == -7.0  # 550+ total yds
    # 300-349 yds is an implicit zero (the gap between YA299 and YA399).
    assert {a for a in by_abbr if a.startswith("YA")} == {
        "YA100", "YA199", "YA299", "YA399", "YA449", "YA499", "YA549", "YA550"
    }
