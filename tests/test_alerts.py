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
    # NOTE (item 3.16): the article type is load-bearing now. This test pins the
    # ACTION-ONLY rule (own-roster news is the speed layer -> phone), so it uses the
    # type that clears the 3.16 gate; the gate's own behaviour is pinned below.
    _player(push_db, 400, "00-0000400", "My Guy", "WR")
    _snap(push_db, "2026-09-10", 400, "00-0000400", "My Guy", "WR", "MIN", 1, "ACTIVE")
    push_db.commit()
    payload = {"articles": [{
        "id": 900, "type": "HeadlineNews", "headline": "My Guy expected to play",
        "description": "Full practice.", "published": "2026-09-10T12:00:00Z",
        "byline": "Beat Reporter", "links": {"web": {"href": "x"}},
        "categories": [{"type": "athlete", "athleteId": 400, "description": "My Guy"}],
    }]}
    news.pull_news(push_db, retrieved_as_of="2026-09-10", fetch=lambda limit: payload)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    news_events = [e for e in board.events if e.kind == "NEWS"]
    assert len(news_events) == 1 and news_events[0].is_own
    assert news_events[0].dedup_key == "news:espn:900"
    # news never outranks a real OUT
    assert news_events[0].severity < alerts._SEV_INJURY_OUT
    # own-roster news is the speed layer for "your starter went down" -> phone
    assert news_events[0].phone_worthy and news_events[0].phone_gate is None


def test_phone_policy_a_push_must_name_an_action(push_db):
    """Operator decision 2026-08-05: the phone lane carries only events that
    name an action. INJURY_OUT always qualifies; NEWS only for an OWN-roster
    player. Free-agent/context news is computed (briefing + alert log) but
    never phone_worthy — pre-draft, when the whole universe is a free agent,
    that rule is what keeps the phone silent."""
    _player(push_db, 100, "00-0000100", "Star Back", "RB")
    _snap(push_db, "2026-09-09", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "OUT")
    _player(push_db, 500, "00-0000500", "Camp Hero", "WR")
    _snap(push_db, "2026-09-10", 500, "00-0000500", "Camp Hero", "WR", "DEN", None, "ACTIVE")
    push_db.commit()
    payload = {"articles": [{
        "id": 901, "type": "Story", "headline": "Camp Hero turning heads at practice",
        "description": "Feature piece.", "published": "2026-09-10T12:00:00Z",
        "links": {"web": {"href": "x"}},
        "categories": [{"type": "athlete", "athleteId": 500, "description": "Camp Hero"}],
    }]}
    news.pull_news(push_db, retrieved_as_of="2026-09-10", fetch=lambda limit: payload)

    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    outs = [e for e in board.events if e.kind == "INJURY_OUT"]
    assert outs and all(e.phone_worthy for e in outs)
    fa_news = [e for e in board.events if e.kind == "NEWS" and not e.is_own]
    assert len(fa_news) == 1 and not fa_news[0].phone_worthy
    # ...and the RIGHT rule gets the blame: a not-owned item is withheld by the
    # older action-only rule, so the item-3.16 gate must not tag it (item 3.16).
    assert fa_news[0].phone_gate is None


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


# ============ item 3.16: the NEWS arm's content gate (a dated hypothesis) ============
#
# All player names below are synthetic or NFL players — never a league member (Rule 5).


def _own_news(conn, *, news_id, news_type, byline=None, espn_id=400,
              headline="Something happened", body="A sentence."):
    """Publish one wire article about a player on the operator's own roster."""
    payload = {"articles": [{
        "id": news_id, "type": news_type, "headline": headline,
        "description": body, "published": "2026-09-10T12:00:00Z",
        "byline": byline, "links": {"web": {"href": "x"}},
        "categories": [{"type": "athlete", "athleteId": espn_id, "description": "My Guy"}],
    }]}
    news.pull_news(conn, retrieved_as_of="2026-09-10", fetch=lambda limit: payload)


def _seed_own_player(conn, espn_id=400):
    gsis = f"00-000{espn_id:04d}"
    _player(conn, espn_id, gsis, "My Guy", "WR")
    _snap(conn, "2026-09-10", espn_id, gsis, "My Guy", "WR", "MIN", 1, "ACTIVE")
    conn.commit()


def _news(board):
    return [e for e in board.events if e.kind == "NEWS"]


def test_news_gate_pushes_headlinenews_and_withholds_bylineless_media(push_db):
    """The measured separation this item is built on: over the 13-day Week-1 window
    every decision-relevant own-roster push was a bylined `HeadlineNews` and every
    piece of fluff was a bylineless `Media` row."""
    _seed_own_player(push_db)
    _own_news(push_db, news_id=910, news_type="HeadlineNews", byline="Beat Reporter",
              headline="My Guy expected to start the opener")
    _own_news(push_db, news_id=911, news_type="Media", byline=None,
              headline="My Guy highlight")
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    by_key = {e.dedup_key: e for e in _news(board)}
    assert len(by_key) == 2  # BOTH are computed — the gate routes, it never drops

    headline = by_key["news:espn:910"]
    assert headline.is_own and headline.phone_worthy and headline.phone_gate is None
    assert headline.news_type == "HeadlineNews"

    media = by_key["news:espn:911"]
    assert media.is_own and not media.phone_worthy
    assert media.news_type == "Media"
    assert "news_type=Media" in media.phone_gate and "no byline" in media.phone_gate
    assert alerts.NEWS_GATE_REVIEW_DATE in media.phone_gate  # it is dated, on the row


def test_news_gate_withholds_story_too(push_db):
    """`Story` is bylined and still context — the plan's stated COST (it drops
    Story/Media rows with watch value), not an oversight."""
    _seed_own_player(push_db)
    _own_news(push_db, news_id=912, news_type="Story", byline="Feature Writer")
    e = _news(alerts.build_alerts(
        push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2))[0]
    assert not e.phone_worthy and "news_type=Story" in e.phone_gate
    # the byline is EVIDENCE for the review, never a second condition
    assert "byline 'Feature Writer'" in e.phone_gate


def test_news_gate_is_an_allowlist_so_an_unknown_type_is_context(push_db):
    """Safe-by-default, the same discipline `phone_worthy` uses for a new KIND: an
    unrecognised (or absent) type does not reach the phone until it opts in."""
    _seed_own_player(push_db)
    _own_news(push_db, news_id=913, news_type="SomeNewEspnType", byline="X")
    _own_news(push_db, news_id=914, news_type=None)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    gates = {e.dedup_key: e.phone_gate for e in _news(board)}
    assert "news_type=SomeNewEspnType" in gates["news:espn:913"]
    assert "news_type=(untyped)" in gates["news:espn:914"]
    assert all(g for g in gates.values())


def test_injury_out_is_never_touched_by_the_news_gate(push_db, monkeypatch):
    """THE pin (item 3.16 done-when): `INJURY_OUT` is UNGATED and always pushes.

    Emptying the allowlist gates every article there is; if the gate were ever
    applied to the injury arm — the arm the whole phone lane exists for — this
    would go silent. A test that only checked the shipped allowlist could not tell
    an ungated arm from one that merely happens to be allowed."""
    monkeypatch.setattr(alerts, "NEWS_PHONE_TYPES", frozenset())
    _player(push_db, 100, "00-0000100", "Star Back", "RB")
    _snap(push_db, "2026-09-09", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "ACTIVE")
    _snap(push_db, "2026-09-10", 100, "00-0000100", "Star Back", "RB", "ATL", 1, "OUT")
    _seed_own_player(push_db)
    _own_news(push_db, news_id=915, news_type="HeadlineNews", byline="Beat Reporter")

    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    outs = [e for e in board.events if e.kind == "INJURY_OUT"]
    assert len(outs) == 1
    assert outs[0].phone_worthy and outs[0].phone_gate is None and outs[0].news_type is None
    # ...while the news arm, with nothing allowed, went quiet.
    assert _news(board) and not any(e.phone_worthy for e in _news(board))


def test_a_withheld_item_is_disclosed_and_still_delivered(push_db):
    """The item's whole claim is 'withheld from the PUSH, not from you'. A silent
    drop would make that claim unfalsifiable, so the board carries the count + the
    type breakdown + the dated label, and the rendered line carries the reason."""
    _seed_own_player(push_db)
    _own_news(push_db, news_id=916, news_type="Media")
    _own_news(push_db, news_id=917, news_type="Media")
    _own_news(push_db, news_id=918, news_type="Story", byline="Feature Writer")
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)

    note = next(n for n in board.notes if "item-3.16" in n)
    assert "3 own-roster news item(s) held back" in note
    assert "2x Media" in note and "1x Story" in note
    assert "HYPOTHESIS, not a rule" in note and alerts.NEWS_GATE_REVIEW_DATE in note
    # all three are still ON the board (the briefing renders every event)...
    assert len(_news(board)) == 3
    # ...and the rendered line says it was routed, rather than arriving unmarked.
    line = alerts.format_alert_line(_news(board)[0])
    assert "held back from the phone" in line


def test_no_gate_note_when_nothing_was_withheld(push_db):
    """A 20-minute tick that withheld nothing must not print the disclosure — a
    banner on every tick is how the one that matters gets ignored."""
    _seed_own_player(push_db)
    _own_news(push_db, news_id=919, news_type="HeadlineNews", byline="Beat Reporter")
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1, week=2)
    assert not any("item-3.16" in n for n in board.notes)


def test_news_phone_gate_reads_only_the_type():
    """`byline` is recorded, never conditioned on — a bylined Media row is still
    withheld and a bylineless HeadlineNews still pushes. That asymmetry is what
    makes the review's question ('did we withhold a BYLINED Media row?') answerable
    instead of circular."""
    assert alerts.news_phone_gate({"news_type": "HeadlineNews", "byline": None}) is None
    assert alerts.news_phone_gate({"news_type": "Media", "byline": "Someone"}) is not None
    assert alerts.news_phone_gate({"news_type": " HeadlineNews "}) is None  # whitespace-safe


def test_phone_lane_policy_states_the_rules_and_dates_the_hypothesis():
    page = alerts.format_phone_lane_policy(today="2026-10-01")
    assert "INJURY_OUT" in page and "ALWAYS pushes" in page and "UNGATED" in page
    # the page names the LIVE allowlist, so widening the gate cannot leave the
    # operator reading a rule the code stopped following
    assert f"news_type in {sorted(alerts.NEWS_PHONE_TYPES)}" in page
    assert "HeadlineNews" in page
    assert "HYPOTHESIS, not a rule" in page
    assert "review 2026-10-15 (in 14 day(s))" in page
    # the flip margin is PRINTED and wired to nothing (item 3.16 did not adopt E18's
    # WATCH gate) — the disclosure must say so in the same breath as the number.
    assert "0.018-0.053 house pts/week" in page and "WIRED TO NO GATE" in page


def test_phone_lane_policy_announces_the_review_when_it_comes_due():
    """A review date nobody is reminded of is a review date that does not exist."""
    assert "REVIEW DUE 2026-10-15 (TODAY)" in alerts.format_phone_lane_policy(today="2026-10-15")
    overdue = alerts.format_phone_lane_policy(today="2026-11-01")
    assert "REVIEW DUE 2026-10-15 (OVERDUE by 17 day(s))" in overdue
    assert "review 2026-10-15 (in" in alerts.format_phone_lane_policy(today="2026-09-15")
