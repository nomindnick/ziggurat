"""Leakage/revision + cached-fixture tests for game-odds ingestion (item 1.5).

Odds are stamped ``min(gameday, retrieved_as_of)`` (item 3.14 step 3): a line is
visible from the day a pull carried it, never earlier, and a pull made on or
after the game (a closing line) is invisible before kickoff day under EITHER
view — the crux this source must enforce.
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



# ------------------------------------------------ item 3.14 step 3: midweek lines
_SUN_GAME = "2026_04_MIA_MIN"


def _sun_row(**overrides):
    row = {"game_id": _SUN_GAME, "season": 2026, "week": 4, "home_team": "MIN",
           "away_team": "MIA", "gameday": "2026-10-04", "total_line": 44.0,
           "spread_line": 6.5}
    row.update(overrides)
    return row


def _pull(db, day, **overrides):
    game_odds.ingest_game_odds(db, _odds_frame([_sun_row(**overrides)]), retrieved_as_of=day)


def test_a_midweek_line_is_knowable_from_its_pull_day_and_never_before(db):
    """THE LEAKAGE TEST that closes item 4.6's retired done-when (b)."""
    _pull(db, "2026-09-29", total_line=44.0)      # Tuesday
    _pull(db, "2026-10-01", total_line=45.5)      # Thursday
    _pull(db, "2026-10-05", total_line=46.0)      # the day after: the CLOSING line

    stamps = {r[0]: r[1] for r in db.execute(
        "SELECT retrieved_as_of, knowable_as_of FROM game_odds")}
    assert stamps == {"2026-09-29": "2026-09-29", "2026-10-01": "2026-10-01",
                      "2026-10-05": "2026-10-04"}

    def seen(as_of, view="historical"):
        read = game_odds.get_game_odds
        if view == "latest_truth":
            read = base.latest_truth(read)
        rows = read(db, as_of=as_of, game_id=_SUN_GAME)
        return rows[0]["total_line"] if rows else None

    assert seen("2026-09-28") is None             # before any pull
    assert seen("2026-09-29") == 44.0             # Tuesday sees Tuesday's line
    assert seen("2026-09-30") == 44.0             # ...and Wednesday still does
    assert seen("2026-10-01") == 45.5
    assert seen("2026-10-04") == 45.5             # the closing pull is not yet retrieved
    assert seen("2026-10-05") == 46.0

    # latest_truth drops the retrieval gate, and STILL cannot see the future:
    # the Thursday line is not knowable on Tuesday, and the closing line is
    # stamped at kickoff day, so nothing before it can read it.
    assert seen("2026-09-29", "latest_truth") == 44.0
    assert seen("2026-10-03", "latest_truth") == 45.5
    assert seen("2026-10-04", "latest_truth") == 46.0


def test_a_backfill_pulled_after_the_season_keeps_the_gameday_stamp(db):
    """Every 2021–2025 row was bulk-pulled in 2026: min(gameday, pull) is the
    gameday, so the backtest's leakage story is exactly what it was."""
    game_odds.ingest_game_odds(db, _odds_frame([_kc_row()]), retrieved_as_of="2026-07-25")
    [row] = db.execute("SELECT knowable_as_of FROM game_odds").fetchall()
    assert row[0] == "2023-09-07"


def test_restamping_rows_stored_under_the_old_rule_is_exact_and_idempotent(db):
    """Rows the old rule stamped at the gameday although they were pulled days
    earlier get their pull day; a row pulled on or after the game, and every
    other season, is untouched; a second run changes nothing."""
    ins = ("INSERT INTO game_odds (game_id, season, week, home_team, away_team, "
           "total_line, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?)")
    db.execute(ins, (_SUN_GAME, 2026, 4, "MIN", "MIA", 44.0, "2026-09-29", "2026-10-04"))
    db.execute(ins, (_SUN_GAME, 2026, 4, "MIN", "MIA", 46.0, "2026-10-05", "2026-10-04"))
    db.execute(ins, (_KC_GAME, 2023, 1, "KC", "DET", 53.0, "2023-09-01", "2023-09-07"))
    db.commit()

    assert game_odds.restamp_stored_forward_lines(db, season=2026) == 1
    stamps = {(r[0], r[1]): r[2] for r in db.execute(
        "SELECT game_id, retrieved_as_of, knowable_as_of FROM game_odds")}
    assert stamps == {(_SUN_GAME, "2026-09-29"): "2026-09-29",
                      (_SUN_GAME, "2026-10-05"): "2026-10-04",
                      (_KC_GAME, "2023-09-01"): "2023-09-07"}
    assert game_odds.restamp_stored_forward_lines(db, season=2026) == 0


def test_a_played_game_keeps_the_gameday_stamp_even_when_back_stamped(db):
    """Defence in depth (review, 2026-09-30): `ingest run --allow-backfill
    --as-of <past>` writes a pull made TODAY under a past retrieved_as_of. A row
    whose game carries a result is a closing line, so it is stamped at kickoff
    day and a read before it cannot see the close."""
    played = _sun_row(gameday="2026-09-13", total_line=41.0, result=7, home_score=24,
                      away_score=17)
    game_odds.ingest_game_odds(db, _odds_frame([played]), retrieved_as_of="2026-09-05")
    [row] = db.execute("SELECT knowable_as_of FROM game_odds").fetchall()
    assert row[0] == "2026-09-13"
    assert game_odds.get_game_odds(db, as_of="2026-09-06", game_id=_SUN_GAME) == []
