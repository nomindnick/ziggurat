"""Leakage/revision + cached-fixture tests for game-odds ingestion (item 1.5).

Odds are stamped with the game's own gameday, so a closing line for a Sunday
game is invisible to a Saturday (D-1) read — the crux this source must enforce.
Fixtures are small, hand-built NFL-data snippets (public schedule/odds values,
no league-private data); the ``nfl.import_game_odds`` seam is never hit.
"""

import logging

import pandas as pd
import pytest

from ziggurat.data.nfl import base, game_odds

# One canonical closing-line row: DET @ KC, 2023 wk1, KC (home) favored by 4.
_KC_GAME = "2023_01_DET_KC"


def _odds_frame(rows):
    """Build a source-shaped odds frame from partial row dicts (missing odds
    default to NaN, missing gameday to None) — every required column present."""
    template = {
        "game_id": None, "season": 2023, "week": 1,
        "home_team": None, "away_team": None, "gameday": None,
        "spread_line": None, "total_line": None,
        "home_moneyline": None, "away_moneyline": None,
        "home_spread_odds": None, "away_spread_odds": None,
        "over_odds": None, "under_odds": None,
    }
    return pd.DataFrame([{**template, **r} for r in rows])


def _kc_row(**overrides):
    row = {
        "game_id": _KC_GAME, "home_team": "KC", "away_team": "DET",
        "gameday": "2023-09-07",
        "spread_line": 4.0, "total_line": 53.0,
        "home_moneyline": -198, "away_moneyline": 164,
        "home_spread_odds": -110, "away_spread_odds": -110,
        "over_odds": -110, "under_odds": -110,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- (1)
def test_leakage_gate_and_revision(db):
    # Closing line for a 2023-09-07 game, first pulled that same day.
    game_odds.ingest_game_odds(db, _odds_frame([_kc_row()]), retrieved_as_of="2023-09-07")

    # The day before kickoff the gameday-stamped line is (correctly) invisible.
    assert game_odds.get_game_odds(db, as_of="2023-09-06", game_id=_KC_GAME) == []

    on_day = game_odds.get_game_odds(db, as_of="2023-09-07", game_id=_KC_GAME)
    assert len(on_day) == 1
    assert on_day[0]["spread_line"] == 4.0
    assert on_day[0]["knowable_as_of"] == "2023-09-07"

    # A later-retrieved correction (spread re-marked to 6.0) coexists via the
    # retrieved_as_of PK component.
    game_odds.ingest_game_odds(
        db, _odds_frame([_kc_row(spread_line=6.0)]), retrieved_as_of="2023-09-10"
    )

    # historical reconstructs what we had retrieved by as_of=2023-09-07: the
    # 09-10 correction is not yet pulled, so the original 4.0 stands.
    hist = game_odds.get_game_odds(db, as_of="2023-09-07", game_id=_KC_GAME)
    assert len(hist) == 1 and hist[0]["spread_line"] == 4.0

    # latest_truth relaxes the retrieval gate: the newest correction surfaces
    # even at the earlier as_of (fact-time gate still honored — gameday reached).
    truth = base.latest_truth(game_odds.get_game_odds)(db, as_of="2023-09-07", game_id=_KC_GAME)
    assert len(truth) == 1 and truth[0]["spread_line"] == 6.0


# --------------------------------------------------------------------------- (2)
def test_cached_fixture_drops_null_gameday_keeps_null_odds(db, caplog):
    frame = _odds_frame([
        # KC home favored +4.0 — full odds, real gameday: KEPT, orientation verbatim.
        _kc_row(),
        # CIN @ CLE — away (CIN) favored, spread NEGATIVE: KEPT, sign preserved verbatim.
        _kc_row(game_id="2023_01_CIN_CLE", home_team="CLE", away_team="CIN",
                gameday="2023-09-10", spread_line=-1.0, total_line=46.5,
                home_moneyline=-108, away_moneyline=-112),
        # Unplayed in-season game: gameday present but odds all NULL: KEPT.
        {"game_id": "2023_18_TBD_TBD", "week": 18, "home_team": "TBD",
         "away_team": "TBD", "gameday": "2024-01-07"},
        # Not-yet-scheduled future game: NULL gameday (unstampable): DROPPED.
        {"game_id": "2099_01_FUT_URE", "week": 1, "home_team": "FUT",
         "away_team": "URE", "gameday": None, "spread_line": 2.5},
    ])

    with caplog.at_level(logging.WARNING, logger="ziggurat.data.nfl"):
        n = game_odds.ingest_game_odds(db, frame, retrieved_as_of="2024-01-10")

    # 4 rows in, the null-gameday row dropped -> 3 stored, and the drop is noted.
    assert n == 3
    assert any("game_odds" in rec.message and "dropped 1/4" in rec.message
               for rec in caplog.records), "note_drops must fire for the null-gameday row"

    stored = game_odds.get_game_odds(db, as_of="2024-01-10")
    by_id = {r["game_id"]: r for r in stored}
    assert set(by_id) == {_KC_GAME, "2023_01_CIN_CLE", "2023_18_TBD_TBD"}
    assert "2099_01_FUT_URE" not in by_id

    # Home-orientation preserved verbatim, both signs.
    assert by_id[_KC_GAME]["spread_line"] == 4.0          # home favored -> positive
    assert by_id["2023_01_CIN_CLE"]["spread_line"] == -1.0  # away favored -> negative
    assert by_id[_KC_GAME]["knowable_as_of"] == "2023-09-07"

    # Null-odds row KEPT with NULL line, and knowable_as_of == its gameday.
    unplayed = by_id["2023_18_TBD_TBD"]
    assert unplayed["spread_line"] is None
    assert unplayed["total_line"] is None
    assert unplayed["knowable_as_of"] == "2024-01-07"


def test_require_columns_drift_raises(db):
    frame = _odds_frame([_kc_row()]).drop(columns=["spread_line"])
    with pytest.raises(ValueError, match="spread_line"):
        game_odds.ingest_game_odds(db, frame, retrieved_as_of="2023-09-07")
