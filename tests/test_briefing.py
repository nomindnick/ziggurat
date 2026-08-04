"""Item 3.6 — the briefing composer (core/briefing.py). Focus: the composition
and per-section degrade logic this module OWNS (the sub-builders have their own
tests); a missing section must SAY why (Rule 6), not vanish or crash the read."""

from ziggurat.core import briefing


def _seed_snapshot(conn, *, season=2026, day="2026-09-10"):
    conn.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, position, "
        "pro_team, on_team_id, injury_status, scoring_period, percent_owned, retrieved_as_of, "
        "knowable_as_of) VALUES (?, '999', '00-0000999', 'A Player', 'RB', 'ATL', 1, 'ACTIVE', "
        "0, 10.0, ?, ?)",
        (season, day, day),
    )
    conn.commit()


def test_unresolved_own_team_degrades_roster_sections_not_crash(push_db):
    _seed_snapshot(push_db)
    b = briefing.build_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=None,
                                today="2026-09-10")
    by_title = {s.title: s for s in b.sections}
    assert by_title["WAIVERS"].degraded and "own team unresolved" in by_title["WAIVERS"].body
    assert by_title["LINEUP"].degraded
    # SIGNALS degrades (no completed week) with a why; ALERTS still renders.
    assert by_title["SIGNALS"].degraded
    assert "ALERTS" in by_title


def test_preseason_week_note_and_signals_degrade(push_db):
    # No schedule/projections and scoring_period 0 -> week unresolved -> a note,
    # and SIGNALS says why rather than vanishing.
    _seed_snapshot(push_db)
    b = briefing.build_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=1,
                                today="2026-09-10")
    assert any("week window unresolved" in n for n in b.notes)
    by_title = {s.title: s for s in b.sections}
    assert by_title["SIGNALS"].degraded


def test_format_includes_banner_and_degraded_flags(push_db):
    _seed_snapshot(push_db)
    b = briefing.build_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=None,
                                today="2026-09-10")
    text = briefing.format_briefing(b)
    assert "# Ziggurat briefing" in text
    assert "## Data freshness" in text
    assert "(DEGRADED)" in text  # the degraded sections are flagged, not hidden
    assert "league state:" in text


def test_all_four_sections_present_even_when_degraded(push_db):
    _seed_snapshot(push_db)
    b = briefing.build_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=None,
                                today="2026-09-10")
    titles = [s.title for s in b.sections]
    assert titles == ["WAIVERS", "LINEUP", "SIGNALS", "ALERTS"]


def test_headline_summary_is_allowlist_safe_and_covers_both_branches():
    # audit D8: the teaser is the actual ntfy body; its two novice-critical branches
    # (ROSTER ILLEGAL, N claims) must be exercised AND must carry no names.
    from ziggurat.core.alerts import AlertEvent

    ev = AlertEvent(kind="INJURY_OUT", player="X", position="RB", team="ATL", gsis_id=None,
                    espn_id="1", on_team_id=1, is_own=True, headline="h", detail=(),
                    source="s", event_day="2026-09-10", severity=2.5, dedup_key="k")
    illegal = briefing.Briefing(season=2026, as_of="2026-09-10", week=3, team_id=1,
                                staleness=(), sections=(), alert_events=(ev,), notes=(),
                                n_claims=2, legal=False)
    s = illegal.headline_summary
    assert "ROSTER ILLEGAL" in s and "2 claim(s)" in s and "1 alert(s)" in s
    # allowlist-safe: no player/team names, only counts + status.
    assert "X" not in s and "ATL" not in s

    legal = briefing.Briefing(season=2026, as_of="2026-09-10", week=3, team_id=1,
                              staleness=(), sections=(), alert_events=(), notes=(),
                              n_claims=0, legal=True)
    assert "ROSTER ILLEGAL" not in legal.headline_summary


def test_staleness_banner_flags_missing_snapshot(push_db):
    # No league snapshot at all -> the banner says so (not silently fresh).
    b = briefing.build_briefing(push_db, as_of="2026-09-10", season=2026, own_team_id=None,
                                today="2026-09-10")
    assert any("NO snapshot" in s for s in b.staleness)
