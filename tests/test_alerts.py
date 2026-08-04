"""Item 3.6 — the event->alert pipeline (core/alerts.py, pure compute).

Injury transitions and news become novice-legible, correctly-gated alert events;
the handcuff-grab arm reuses marginal (QB/RB/TE only, backup must be a FA, bye
suppression); dedup keys are stable; leakage is gated."""

import pytest

from ziggurat.core import alerts
from ziggurat.data.nfl import news

SEASON = 2026


def _player(conn, espn_id, gsis_id, name, position):
    conn.execute(
        "INSERT INTO players (gsis_id, espn_id, name, position, retrieved_as_of, knowable_as_of) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (gsis_id, str(espn_id), name, position, "2026-09-01", "2026-09-01"),
    )


def _snap(conn, day, espn_id, gsis_id, name, pos, team, on_team, status, sp=1):
    conn.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, position, "
        "pro_team, on_team_id, injury_status, scoring_period, percent_owned, retrieved_as_of, "
        "knowable_as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SEASON, str(espn_id), gsis_id, name, pos, team, on_team, status, sp, 50.0, day, day),
    )


def _proj(conn, spid, gsis, pos, team, week, rush_yds, *, day="2026-09-01"):
    conn.execute(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, season_type, "
        "position, team, opponent, rushing_yards, projected_points, retrieved_as_of, knowable_as_of) "
        "VALUES ('sleeper_rotowire', ?, ?, ?, ?, 'regular', ?, ?, 'OPP', ?, ?, ?, ?)",
        (spid, gsis, SEASON, week, pos, team, rush_yds, rush_yds / 10.0, day, day),
    )


def test_own_player_ruled_out_alerts(push_db):
    _player(push_db, 100, "00-0000100", "Star Back", "RB")
    _snap(push_db, "2026-09-09", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "OUT")
    push_db.commit()

    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    outs = [e for e in board.events if e.kind == "INJURY_OUT"]
    assert len(outs) == 1
    e = outs[0]
    assert e.is_own and "YOUR Star Back" in e.headline and "OUT" in e.headline
    assert e.dedup_key == "inj:100:2026-09-10:ruled_out"


def test_not_owned_out_without_available_handcuff_is_not_pushed(push_db):
    # A random OUT with no rosterable FA handcuff is not a phone alert.
    _player(push_db, 200, "00-0000200", "Someone Else", "RB")
    _snap(push_db, "2026-09-09", 200, "00-0000200", "Someone Else", "RB", "CHI", 5, "ACTIVE")
    _snap(push_db, "2026-09-10", 200, "00-0000200", "Someone Else", "RB", "CHI", 5, "OUT")
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    assert [e for e in board.events if e.kind == "INJURY_OUT"] == []


def test_handcuff_available_arm(push_db):
    # Starter (owned by another team) and his FA backup, same team+position.
    _player(push_db, 300, "00-0000300", "Bell Cow", "RB")
    _player(push_db, 301, "00-0000301", "The Backup", "RB")
    # projections make Bell Cow the starter (higher), Backup rank 2.
    for wk in (2, 3, 4):
        _proj(push_db, "S300", "00-0000300", "RB", "SEA", wk, 90.0)
        _proj(push_db, "S301", "00-0000301", "RB", "SEA", wk, 20.0)
    # snapshots: starter goes OUT (held by team 5); backup is a FREE AGENT (on_team NULL).
    _snap(push_db, "2026-09-09", 300, "00-0000300", "Bell Cow", "RB", "SEA", 5, "ACTIVE")
    _snap(push_db, "2026-09-10", 300, "00-0000300", "Bell Cow", "RB", "SEA", 5, "OUT")
    _snap(push_db, "2026-09-09", 301, "00-0000301", "The Backup", "RB", "SEA", None, "ACTIVE")
    _snap(push_db, "2026-09-10", 301, "00-0000301", "The Backup", "RB", "SEA", None, "ACTIVE")
    push_db.commit()

    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    outs = [e for e in board.events if e.kind == "INJURY_OUT"]
    assert len(outs) == 1
    e = outs[0]
    assert e.handcuff_name == "The Backup" and e.handcuff_espn_id == "301"
    assert "FREE AGENT" in e.headline and "grab him" in e.headline
    assert any("house pts/wk" in d for d in e.detail)  # the labelled uplift hypothesis


def test_handcuff_not_offered_when_backup_is_rostered(push_db):
    _player(push_db, 300, "00-0000300", "Bell Cow", "RB")
    _player(push_db, 301, "00-0000301", "The Backup", "RB")
    for wk in (2, 3, 4):
        _proj(push_db, "S300", "00-0000300", "RB", "SEA", wk, 90.0)
        _proj(push_db, "S301", "00-0000301", "RB", "SEA", wk, 20.0)
    _snap(push_db, "2026-09-09", 300, "00-0000300", "Bell Cow", "RB", "SEA", 5, "ACTIVE")
    _snap(push_db, "2026-09-10", 300, "00-0000300", "Bell Cow", "RB", "SEA", 5, "OUT")
    # backup rostered by team 7 -> NOT a free agent -> no grab alert.
    _snap(push_db, "2026-09-09", 301, "00-0000301", "The Backup", "RB", "SEA", 7, "ACTIVE")
    _snap(push_db, "2026-09-10", 301, "00-0000301", "The Backup", "RB", "SEA", 7, "ACTIVE")
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    assert [e for e in board.events if e.kind == "INJURY_OUT"] == []


def test_news_event_for_owned_player(push_db):
    _player(push_db, 400, "00-0000400", "My Guy", "WR")
    _snap(push_db, "2026-09-10", 400, "00-0000400", "My Guy", "WR", "MIN", 1, "ACTIVE")
    push_db.commit()
    payload = {"articles": [{
        "id": 900, "type": "Story", "headline": "My Guy expected to play",
        "description": "Full practice.", "published": "2026-09-10T12:00:00Z",
        "links": {"web": {"href": "x"}},
        "categories": [{"type": "athlete", "athleteId": 400, "description": "My Guy"}],
    }]}
    news.pull_news(push_db, retrieved_as_of="2026-09-10", fetch=lambda limit: payload)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    news_events = [e for e in board.events if e.kind == "NEWS"]
    assert len(news_events) == 1 and news_events[0].is_own
    assert news_events[0].dedup_key == "news:espn:900"
    # news never outranks a real OUT
    assert news_events[0].severity < alerts._SEV_INJURY_OUT


def test_own_kicker_out_does_not_fire_a_high_priority_injury_alert(push_db):
    # audit D6: an owned K/DST ruled OUT must NOT fire a high-severity injury alert
    # that would outrank a real handcuff. The is_own edge-guard is None-position only.
    _player(push_db, 500, "00-0000500", "My Kicker", "K")
    _snap(push_db, "2026-09-09", 500, "00-0000500", "My Kicker", "K", "ATL", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 500, "00-0000500", "My Kicker", "K", "ATL", 1, "OUT")
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    assert [e for e in board.events if e.kind == "INJURY_OUT"] == []


def test_own_player_out_has_plain_language_and_next_step(push_db):
    # audit D6: no raw ESPN enum; a novice-legible status + a next-step (Rule 6).
    _player(push_db, 100, "00-0000100", "Star Back", "RB")
    _snap(push_db, "2026-09-09", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "INJURY_RESERVE")
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    e = [e for e in board.events if e.kind == "INJURY_OUT"][0]
    assert "INJURED RESERVE" in e.headline and "INJURY_RESERVE" not in e.headline
    assert any("waiver claims" in d for d in e.detail)  # a next-step for the novice


def test_injured_starter_on_bye_still_gets_handcuff_alert(push_db):
    # audit D5/D6: an injury vacancy is NOT a bye. A starter ruled OUT whose NFL team
    # is on bye this week must STILL surface his FA handcuff (season-long insurance),
    # not be silently bye-suppressed.
    _player(push_db, 300, "00-0000300", "Bell Cow", "RB")
    _player(push_db, 301, "00-0000301", "The Backup", "RB")
    # SEA byes in the resolved week (no projection row for week 2 -> a bye-shaped gap),
    # but we still have weeks 3-4 so handcuff_links prices the pair.
    for wk in (3, 4):
        _proj(push_db, "S300", "00-0000300", "RB", "SEA", wk, 90.0)
        _proj(push_db, "S301", "00-0000301", "RB", "SEA", wk, 20.0)
    _snap(push_db, "2026-09-09", 300, "00-0000300", "Bell Cow", "RB", "SEA", 5, "ACTIVE")
    _snap(push_db, "2026-09-10", 300, "00-0000300", "Bell Cow", "RB", "SEA", 5, "OUT")
    _snap(push_db, "2026-09-09", 301, "00-0000301", "The Backup", "RB", "SEA", None, "ACTIVE")
    _snap(push_db, "2026-09-10", 301, "00-0000301", "The Backup", "RB", "SEA", None, "ACTIVE")
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    outs = [e for e in board.events if e.kind == "INJURY_OUT"]
    assert len(outs) == 1 and outs[0].handcuff_name == "The Backup"


def test_leakage_transition_not_visible_before_it_is_knowable(push_db):
    _player(push_db, 100, "00-0000100", "Star Back", "RB")
    _snap(push_db, "2026-09-09", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "OUT")
    push_db.commit()
    # as_of the day BEFORE the OUT snapshot: no transition yet.
    board = alerts.build_alerts(push_db, as_of="2026-09-09", season=SEASON, own_team_id=1, week=2)
    assert [e for e in board.events if e.kind == "INJURY_OUT"] == []


def test_cleared_transition_is_not_a_phone_event(push_db):
    _player(push_db, 100, "00-0000100", "Star Back", "RB")
    _snap(push_db, "2026-09-09", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "OUT")
    _snap(push_db, "2026-09-10", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "ACTIVE")
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    assert board.events == ()  # 'cleared' is briefing context, not a push


def test_preseason_degrades_with_a_note_not_a_crash(push_db):
    # scoring_period=0 (pre-draft) AND no schedule -> resolve_weeks raises;
    # build_alerts must degrade (own-player-down still works) and disclose the
    # missing enrichment rather than crash the tick.
    _player(push_db, 100, "00-0000100", "Star Back", "RB")
    _snap(push_db, "2026-09-09", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "ACTIVE", sp=0)
    _snap(push_db, "2026-09-10", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "OUT", sp=0)
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1)  # week=None
    assert any("handcuff pricing unavailable" in n for n in board.notes)
    assert len([e for e in board.events if e.is_own]) == 1  # own-down still fires
