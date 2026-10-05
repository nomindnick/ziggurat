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


def _snap(conn, day, espn_id, gsis_id, name, pos, team, on_team, status, sp=1,
          roster_status=None):
    # ESPN's own acquisition token rides every row (item 3.16b reads it to say
    # FREE AGENT vs WAIVERS); the default mirrors what ESPN serves.
    if roster_status is None:
        roster_status = "ONTEAM" if on_team is not None else "FREEAGENT"
    conn.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, position, "
        "pro_team, on_team_id, roster_status, injury_status, scoring_period, percent_owned, "
        "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SEASON, str(espn_id), gsis_id, name, pos, team, on_team, roster_status, status, sp,
         50.0, day, day),
    )


def _pricer(gain, *, drop="Depth Runner", calls=None):
    """A stub for the item-3.16b pricing seam: every backup it is asked about is
    worth ``gain`` (None = no positive move). ``calls`` records what was priced."""
    def _price(rows, *, weeks, lines):
        if calls is not None:
            calls.append([str(r["espn_player_id"]) for r in rows])
        return {str(r["espn_player_id"]): alerts.HandcuffPrice(
            gain, drop if gain is not None else None, len(weeks)) for r in rows}
    return _price


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

    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(3.0))
    outs = [e for e in board.events if e.kind == "INJURY_OUT"]
    assert len(outs) == 1
    e = outs[0]
    assert e.handcuff_name == "The Backup" and e.handcuff_espn_id == "301"
    # item 3.16b: someone else's player pushes because the backup PRICES for us, and
    # the push says what he is worth, how he is acquired and what it costs.
    assert e.phone_worthy and e.phone_gate is None and e.handcuff_gain == 3.0
    assert "a FREE AGENT" in e.headline and "worth +3.0 house pts" in e.headline
    assert "dropping Depth Runner" in e.headline and "PROJECTED" in e.headline
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
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(3.0))
    outs = [e for e in board.events if e.kind == "INJURY_OUT"]
    assert len(outs) == 1 and outs[0].handcuff_name == "The Backup" and outs[0].phone_worthy


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


# ========= item 3.16b: the INJURY_OUT arm's handcuff gate (a dated hypothesis) =========
#
# Someone else's player reaches the phone only when his backup prices for YOUR roster;
# YOUR player is never gated. Every name below is synthetic (Rule 5).


def _seed_pair(conn, *, starter_team=5, starter_days=(("2026-09-09", "ACTIVE"),
                                                      ("2026-09-10", "OUT")),
               backup_on=None, backup_status="ACTIVE", backup_roster_status=None,
               backup_in_pool=True):
    _player(conn, 300, "00-0000300", "Bell Cow", "RB")
    _player(conn, 301, "00-0000301", "The Backup", "RB")
    for wk in (2, 3, 4):
        _proj(conn, "S300", "00-0000300", "RB", "SEA", wk, 90.0)
        _proj(conn, "S301", "00-0000301", "RB", "SEA", wk, 20.0)
    for day, status in starter_days:
        _snap(conn, day, 300, "00-0000300", "Bell Cow", "RB", "SEA", starter_team, status)
    if backup_in_pool:
        for day, _ in starter_days:
            _snap(conn, day, 301, "00-0000301", "The Backup", "RB", "SEA", backup_on,
                  backup_status, roster_status=backup_roster_status)
    conn.commit()


def _outs(board):
    return [e for e in board.events if e.kind == "INJURY_OUT"]


def test_non_roster_out_below_the_floor_is_held_with_its_price(push_db):
    _seed_pair(push_db)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(0.6))
    (e,) = _outs(board)
    assert not e.phone_worthy and e.handcuff_gain == 0.6
    assert "below the +1.0 floor" in e.phone_gate and "item 3.16b" in e.phone_gate
    # held, not hidden: the line still renders, with the reason, and the tick says so
    assert "below the +1.0 floor" in alerts.format_alert_line(e)
    assert any("held back by the item-3.16b handcuff gate" in n for n in board.notes)
    # a held line must not tell the novice to grab anyone
    assert "grab" not in e.headline.lower() and "worth" not in e.headline


def test_the_floor_is_inclusive_and_read_live(push_db, monkeypatch):
    _seed_pair(push_db)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(alerts.HANDCUFF_PHONE_MIN_GAIN))
    assert _outs(board)[0].phone_worthy
    monkeypatch.setattr(alerts, "HANDCUFF_PHONE_MIN_GAIN", 50.0)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(3.0))
    assert not _outs(board)[0].phone_worthy


def test_no_positive_move_is_held(push_db):
    _seed_pair(push_db)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(None))
    (e,) = _outs(board)
    assert not e.phone_worthy and e.handcuff_gain is None
    assert "no positive move" in e.phone_gate


def test_a_pricing_failure_holds_and_says_why(push_db):
    """The gate fails CLOSED and LOUD: a broken pricer never pushes someone else's
    player, never crashes the tick, and the reason reaches the log and the briefing."""
    _seed_pair(push_db)

    def _boom(rows, *, weeks, lines):
        raise RuntimeError("projections table locked")

    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_boom)
    (e,) = _outs(board)
    assert not e.phone_worthy and "pricing failed: RuntimeError" in e.phone_gate
    assert any("could not price" in n for n in board.notes)


def test_own_player_out_is_never_touched_by_the_handcuff_gate(push_db, monkeypatch):
    """THE pin (item 3.16b): YOUR player ruled out always pushes. An unreachable
    floor and a pricer that refuses to run must change nothing for him — and the
    pricer is never even asked, because YOUR player is not priced."""
    monkeypatch.setattr(alerts, "HANDCUFF_PHONE_MIN_GAIN", 1e9)
    _seed_pair(push_db, starter_team=1)
    calls = []

    def _never(rows, *, weeks, lines):
        calls.append(rows)
        raise AssertionError("YOUR player must not be priced")

    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_never)
    (e,) = _outs(board)
    assert e.is_own and e.phone_worthy and e.phone_gate is None
    assert "YOUR Bell Cow" in e.headline and "The Backup is a FREE AGENT" in e.headline
    assert calls == []


def test_a_crossing_he_has_come_back_from_is_not_reported(push_db):
    """The Penix 09-18 defect: a ruling ESPN had reversed was pushed as news because
    the replayed crossing was never checked against his CURRENT status."""
    _seed_pair(push_db, starter_days=(("2026-09-09", "ACTIVE"), ("2026-09-10", "OUT"),
                                      ("2026-09-12", "QUESTIONABLE")))
    calls = []
    board = alerts.build_alerts(push_db, as_of="2026-09-12", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(9.0, calls=calls))
    assert _outs(board) == []
    assert calls == []  # nothing stale is even priced
    assert any("no longer OUT or on IR" in n for n in board.notes)


def test_own_player_back_from_injury_is_not_reported_either(push_db):
    _seed_pair(push_db, starter_team=1,
               starter_days=(("2026-09-09", "ACTIVE"), ("2026-09-10", "OUT"),
                             ("2026-09-12", "ACTIVE")))
    board = alerts.build_alerts(push_db, as_of="2026-09-12", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(9.0))
    assert _outs(board) == []


@pytest.mark.parametrize("status", sorted(alerts.BACKUP_UNAVAILABLE_STATUSES))
def test_a_backup_who_cannot_play_is_never_offered(push_db, status):
    """Charbonnet (OUT) on 10-02 and Ferguson (IR) on 10-05 were both pushed as
    'grab him'."""
    _seed_pair(push_db, backup_status=status)
    calls = []
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(9.0, calls=calls))
    assert _outs(board) == [] and calls == []
    assert any("cannot play themselves" in n and "The Backup" in n for n in board.notes)


def test_own_player_with_an_unavailable_backup_says_so(push_db):
    _seed_pair(push_db, starter_team=1, backup_status="OUT")
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(9.0))
    (e,) = _outs(board)
    assert e.phone_worthy and e.handcuff_name is None
    assert "handcuff" not in e.headline
    assert any("himself OUT" in d for d in e.detail)


def test_a_backup_on_waivers_is_labelled_a_claim(push_db):
    """Kamara and Gordon (09-28) were pushed as FREE AGENTS while ESPN had them on
    WAIVERS — not clickable that night."""
    _seed_pair(push_db, backup_roster_status="WAIVERS")
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(3.0))
    (e,) = _outs(board)
    assert "on WAIVERS" in e.headline and "FREE AGENT" not in e.headline
    assert any("a claim" in d and "overnight" in d for d in e.detail)


def test_a_backup_espn_does_not_list_is_not_offered(push_db):
    """`who_held` returns None for a player it has never seen, which the old code read
    as FREE AGENT (Ertz on a practice squad, 09-26)."""
    _seed_pair(push_db, backup_in_pool=False)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(9.0))
    assert _outs(board) == []


def test_someone_elses_old_ruling_is_not_news(push_db):
    _seed_pair(push_db, starter_days=(("2026-09-01", "ACTIVE"), ("2026-09-02", "OUT"),
                                      ("2026-09-10", "OUT")))
    calls = []
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(9.0, calls=calls))
    assert _outs(board) == [] and calls == []  # 8 days > the 7-day window
    assert any("more than 7 days ago" in n for n in board.notes)


def test_the_current_owner_decides_yours(push_db):
    """A player dropped since his ruling is no longer YOUR player: the crossing's
    holder is history, today's row is the fact (Achane, dropped 09-30)."""
    _seed_pair(push_db, starter_team=1,
               starter_days=(("2026-09-09", "ACTIVE"), ("2026-09-10", "OUT")))
    _snap(push_db, "2026-09-11", 300, "00-0000300", "Bell Cow", "RB", "SEA", None, "OUT",
          roster_status="WAIVERS")
    _snap(push_db, "2026-09-11", 301, "00-0000301", "The Backup", "RB", "SEA", None, "ACTIVE")
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-11", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(0.2))
    (e,) = _outs(board)
    assert not e.is_own and not e.phone_worthy and "YOUR" not in e.headline


def test_no_own_team_holds_rather_than_pushes(push_db):
    _seed_pair(push_db)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=None,
                                week=2, handcuff_pricer=_pricer(9.0))
    (e,) = _outs(board)
    assert not e.phone_worthy and "no own team" in e.phone_gate


def test_phone_lane_policy_states_the_injury_gate_and_dates_it():
    page = alerts.format_phone_lane_policy(today="2026-10-05")
    assert "YOUR player ruled out ALWAYS pushes" in page and "UNGATED" in page
    assert "SOMEONE ELSE'S player pushes only when" in page
    assert "+1.0 house pts" in page and "item 3.16b" in page
    assert "review 2026-11-05 (in 31 day(s))" in page
    assert "REVIEW DUE 2026-11-05 (TODAY)" in alerts.format_phone_lane_policy(today="2026-11-05")
    assert alerts.INJURY_GATE_LABEL in page


# ---------------------------------------------------- the real pricer, end to end


_FULL_ROSTER = [  # 16 active + 1 IR, team 10 (the live shape)
    {"name": "Quarter Back", "pos": "QB", "team": "TEN", "pts": 20.0, "bye": 6, "on_team": 10},
    {"name": "Backup Passer", "pos": "QB", "team": "TEN", "pts": 8.0, "bye": 6, "on_team": 10},
    {"name": "Lead Runner", "pos": "RB", "team": "ATL", "pts": 18.0, "bye": 11, "on_team": 10},
    {"name": "Second Runner", "pos": "RB", "team": "BUF", "pts": 12.0, "bye": 7, "on_team": 10},
    {"name": "Third Runner", "pos": "RB", "team": "CLE", "pts": 7.0, "bye": 10, "on_team": 10},
    {"name": "Depth Runner", "pos": "RB", "team": "CHI", "pts": 2.0, "bye": 9, "on_team": 10},
    {"name": "First Catcher", "pos": "WR", "team": "DAL", "pts": 17.0, "bye": 8, "on_team": 10},
    {"name": "Second Catcher", "pos": "WR", "team": "DEN", "pts": 15.0, "bye": 9, "on_team": 10},
    {"name": "Third Catcher", "pos": "WR", "team": "GB", "pts": 11.0, "bye": 10, "on_team": 10},
    {"name": "Fourth Catcher", "pos": "WR", "team": "HOU", "pts": 6.0, "bye": 12, "on_team": 10},
    {"name": "Fifth Catcher", "pos": "WR", "team": "ARI", "pts": 5.0, "bye": 8, "on_team": 10},
    {"name": "Tight One", "pos": "TE", "team": "IND", "pts": 10.0, "bye": 13, "on_team": 10},
    {"name": "Tight Two", "pos": "TE", "team": "JAX", "pts": 3.0, "bye": 5, "on_team": 10},
    {"name": "Kick Er", "pos": "K", "team": "KC", "pts": 8.0, "bye": 14, "on_team": 10},
    {"name": "Miami D/ST", "pos": "D/ST", "team": "MIA", "pts": 6.0, "bye": 5, "on_team": 10},
    {"name": "Spare Catcher", "pos": "WR", "team": "CAR", "pts": 4.0, "bye": 11, "on_team": 10},
    {"name": "Hurt Guy", "pos": "WR", "team": "MIN", "pts": 14.0, "bye": 6, "on_team": 10,
     "slot": "IR", "injury": "INJURY_RESERVE"},
]
_FREE_RUNNERS = [
    {"name": "Big Free Runner", "pos": "RB", "team": "NYG", "pts": 16.0, "bye": 7},
    {"name": "Tiny Free Runner", "pos": "RB", "team": "NYJ", "pts": 0.5, "bye": 12},
]


@pytest.mark.parametrize("roster_specs, open_slot", [
    (_FULL_ROSTER, False),
    ([r for r in _FULL_ROSTER if r["name"] not in ("Spare Catcher", "Fifth Catcher")], True),
], ids=["full-roster-swap", "open-slot-pure-add"])
def test_price_handcuffs_reads_the_waiver_board(db, marginal_world, roster_specs, open_slot):
    """The production pricer on a synthetic roster: a strong free runner is worth
    points to team 10, a weak one is not — and the number IS the board's own: the
    best season-long swap on a full roster, the pure add into an open slot."""
    from ziggurat.core import marginal

    roster, pool = marginal_world(roster_specs + _FREE_RUNNERS, retrieved="2026-09-15")
    weeks = list(range(3, 18))
    prices = alerts.price_handcuffs(db, as_of="2026-09-15", season=SEASON, own_team_id=10,
                                    backup_rows=pool, weeks=weeks)
    big = prices[str(pool[0]["espn_player_id"])]
    tiny = prices[str(pool[1]["espn_player_id"])]
    assert big.gain is not None and big.gain > alerts.HANDCUFF_PHONE_MIN_GAIN
    assert tiny.gain is None and tiny.why
    board = marginal.build_board(db, as_of="2026-09-15", season=SEASON, roster=roster,
                                 pool=pool, weeks=weeks)
    rows = [s for s in board.swaps if s.add == "Big Free Runner" and s.horizon_weeks > 1]
    best = max(rows, key=lambda s: s.gain)
    if open_slot:
        pure = board.value_after(pure_adds=(best,)) - board.value_after()
        assert big.drop is None and big.gain == pytest.approx(pure, abs=1e-9)
        assert pure > best.gain  # an open slot is worth more than any swap
    else:
        assert big.drop == best.drop
        assert big.gain == pytest.approx(best.gain, abs=1e-9)


def test_only_a_players_latest_ruling_is_listed(push_db):
    """ESPN re-designates a long-term injury weekly, so one player accumulates several
    ruled-out crossings; only the latest can be current (Reed was listed twice)."""
    _player(push_db, 100, "00-0000100", "Star Back", "RB")
    for day, status in (("2026-09-09", "ACTIVE"), ("2026-09-10", "OUT"),
                        ("2026-09-15", "QUESTIONABLE"), ("2026-09-18", "OUT")):
        _snap(push_db, day, 100, "00-0000100", "Star Back", "RB", "ATL", 1, status)
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-18", season=SEASON, own_team_id=1,
                                week=3, handcuff_pricer=_pricer(None))
    (e,) = _outs(board)
    assert e.dedup_key == "inj:100:2026-09-18:ruled_out"


# ------------------------------------------- item 3.16b review fixes (2026-10-05)


def _best_expected(board, add_name):
    """The pricer's contract, restated: season-long for every row — a streamed-drop
    row is re-priced with the board's own `season_long_delta`."""
    from ziggurat.core import marginal

    vals = [(board.season_long_delta(s) if s.drop_position in marginal.STREAMED_POSITIONS
             else s.gain, s) for s in board.swaps if s.add == add_name]
    return max(vals, key=lambda t: t[0])


def test_price_handcuffs_prices_the_final_week_too(db, marginal_world):
    """Review finding 2: in the last week of the window every row has horizon 1, and
    the old `horizon_weeks <= 1` filter priced every backup at None ("no positive
    move") — a false reason on the page."""
    from ziggurat.core import marginal

    roster, pool = marginal_world(_FULL_ROSTER + _FREE_RUNNERS, retrieved="2026-09-15")
    prices = alerts.price_handcuffs(db, as_of="2026-09-15", season=SEASON, own_team_id=10,
                                    backup_rows=pool, weeks=[17])
    big = prices[str(pool[0]["espn_player_id"])]
    board = marginal.build_board(db, as_of="2026-09-15", season=SEASON, roster=roster,
                                 pool=pool, weeks=[17])
    expected, _row = _best_expected(board, "Big Free Runner")
    assert big.gain is not None and big.gain == pytest.approx(expected, abs=1e-9)
    assert big.weeks == 1


def test_a_streamed_drop_is_priced_over_the_season(db, marginal_world):
    """Review finding 2: "drop your second D/ST" is a real (often the cheapest) drop,
    but its matrix `gain` is a ONE-WEEK number; the pricer re-prices it season-long."""
    from ziggurat.core import marginal

    specs = [s for s in _FULL_ROSTER if s["name"] != "Spare Catcher"] + [
        {"name": "Second D/ST", "pos": "D/ST", "team": "SEA", "pts": 1.0, "bye": 8,
         "on_team": 10}]
    roster, pool = marginal_world(specs + _FREE_RUNNERS, retrieved="2026-09-15")
    weeks = list(range(3, 18))
    prices = alerts.price_handcuffs(db, as_of="2026-09-15", season=SEASON, own_team_id=10,
                                    backup_rows=pool, weeks=weeks)
    board = marginal.build_board(db, as_of="2026-09-15", season=SEASON, roster=roster,
                                 pool=pool, weeks=weeks)
    streamed = [s for s in board.swaps if s.add == "Big Free Runner"
                and s.drop_position in marginal.STREAMED_POSITIONS]
    assert streamed, "fixture must offer a streamed-drop row for the backup"
    expected, row = _best_expected(board, "Big Free Runner")
    big = prices[str(pool[0]["espn_player_id"])]
    assert big.gain == pytest.approx(expected, abs=1e-9) and big.drop == row.drop


def test_an_open_slot_pure_add_respects_the_position_cap(db, marginal_world):
    """Review finding 1: with an open slot and the TE cap already full, a free TE
    priced +24.4 "into your open roster slot" — a move ESPN refuses. The pricer must
    fall back to the best legal SWAP (here: dropping a TE)."""
    from ziggurat.core import marginal

    specs = [s for s in _FULL_ROSTER if s["name"] not in ("Spare Catcher", "Fifth Catcher")]
    specs += [{"name": "Tight Three", "pos": "TE", "team": "LAC", "pts": 2.0, "bye": 12,
               "on_team": 10}]
    free_te = [{"name": "Great Free Tight", "pos": "TE", "team": "SF", "pts": 14.0, "bye": 9}]
    roster, pool = marginal_world(specs + free_te, retrieved="2026-09-15")
    weeks = list(range(3, 18))
    board = marginal.build_board(db, as_of="2026-09-15", season=SEASON, roster=roster,
                                 pool=pool, weeks=weeks)
    assert board.roster_position_counts["TE"] == board.position_caps["TE"] == 3
    price = alerts.price_handcuffs(db, as_of="2026-09-15", season=SEASON, own_team_id=10,
                                   backup_rows=pool, weeks=weeks)[str(pool[0]["espn_player_id"])]
    expected, row = _best_expected(board, "Great Free Tight")
    assert price.drop is not None and price.drop == row.drop
    assert price.gain == pytest.approx(expected, abs=1e-9)


def test_a_player_acquired_after_his_ruling_is_not_news(push_db):
    """Review finding 3: "the current holder decides YOUR" turned an IR stash you
    claimed into a push — an 18-day-old ruling as news, the defect this item fixes."""
    _seed_pair(push_db, starter_team=5,
               starter_days=(("2026-09-01", "ACTIVE"), ("2026-09-02", "INJURY_RESERVE")))
    _snap(push_db, "2026-09-10", 300, "00-0000300", "Bell Cow", "RB", "SEA", 1,
          "INJURY_RESERVE")
    push_db.commit()
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(9.0))
    (e,) = _outs(board)
    assert e.is_own and not e.phone_worthy
    assert "before he joined your roster" in e.phone_gate
    assert any("ruled out before he joined your roster" in n for n in board.notes)


def test_the_seven_day_window_is_inclusive(push_db):
    _seed_pair(push_db, starter_days=(("2026-09-02", "ACTIVE"), ("2026-09-03", "OUT"),
                                      ("2026-09-10", "OUT")))
    calls = []
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(3.0, calls=calls))
    (e,) = _outs(board)           # exactly 7 days old: still news, still priced
    assert e.phone_worthy and calls


def test_a_questionable_backup_is_flagged_on_the_row(push_db):
    _seed_pair(push_db, backup_status="QUESTIONABLE")
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(3.0))
    (e,) = _outs(board)
    assert e.phone_worthy and any("is QUESTIONABLE himself" in d for d in e.detail)


def test_a_held_row_carries_no_acquire_imperative(push_db):
    """Review finding 5: "add him now" directly above "held back ... below the floor"."""
    _seed_pair(push_db)
    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_pricer(0.2))
    (e,) = _outs(board)
    text = alerts.format_alert_line(e)
    assert not e.phone_worthy and "add him now" not in text and "first come" not in text


def test_a_pricing_failure_is_on_the_board_not_only_in_the_notes(push_db):
    _seed_pair(push_db)

    def _boom(rows, *, weeks, lines):
        raise RuntimeError("locked")

    board = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                                week=2, handcuff_pricer=_boom)
    assert board.price_error and "RuntimeError" in board.price_error
    ok = alerts.build_alerts(push_db, as_of="2026-09-10", season=SEASON, own_team_id=1,
                             week=2, handcuff_pricer=_pricer(0.2))
    assert ok.price_error is None   # a low price is a verdict, not an error
