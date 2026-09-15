"""Live in-game scoreboard read (item 3.17, deliverable 1).

Fully offline: the ONE network seam (``source.fetch_live_scoreboard``) is either
patched or injected, and the payload fixture is SYNTHETIC — invented team names,
invented players (Rule 5: the real league's team names and managers are
colleagues and never enter a committed file; team_id numbers are not identities).

What these pin, and why each one is worth a test:

  * the live number comes off ``totalPointsLive``, which is the whole reason the
    command exists — ``totalPoints`` reads 0.0 all Sunday, so a parser that fell
    back to it would render a legitimate-looking 0-0;
  * BE/IR rows never enter the starters' sum;
  * game status is derived from the STORED kickoff against the decision clock,
    and a starter who has already scored is never reported 'yet to play';
  * a closed period says the STORED value is the one to cite;
  * missing schedule rows / missing live fields / a missing matchup degrade with
    a sentence, not a traceback or a plausible zero.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.data.store import apply_schema, connect
from ziggurat.league import live, source

runner = CliRunner()

ET = ZoneInfo("America/New_York")

# ESPN proTeamId -> abbr, for the three clubs the fixture uses (espn_api's map:
# 25=SF, 14=LAR which normalizes to the schedules abbr "LA", 6=DAL).
SF, LAR, DAL = 25, 14, 6


def _stats(period, *, actual=None, projected=None):
    rows = []
    if actual is not None:
        rows.append({"scoringPeriodId": period, "statSourceId": 0,
                     "statSplitTypeId": 1, "appliedTotal": actual})
    if projected is not None:
        rows.append({"scoringPeriodId": period, "statSourceId": 1,
                     "statSplitTypeId": 1, "appliedTotal": projected})
    return rows


def _entry(pid, name, slot, pro_team_id, *, applied=None, projected=None, period=2):
    return {
        "lineupSlotId": slot,
        "playerPoolEntry": {
            "appliedStatTotal": applied,
            "player": {
                "id": pid,
                "fullName": name,
                "proTeamId": pro_team_id,
                "stats": _stats(period, actual=applied, projected=projected),
            },
        },
    }


def _side(team_id, entries, *, live_points=None, total_points=0.0):
    side = {
        "teamId": team_id,
        "totalPoints": total_points,
        "rosterForCurrentScoringPeriod": {"entries": entries},
    }
    if live_points is not None:
        side["totalPointsLive"] = live_points
    return side


def live_payload(*, period=2, own_team=9, opp_team=4, winner="UNDECIDED",
                 own_live=22.1, opp_live=12.0, own_entries=None, opp_entries=None,
                 opponent=True, own_total=0.0):
    """A synthetic live payload shaped like ESPN's mMatchupScore+mBoxscore+mRoster."""
    own_entries = own_entries if own_entries is not None else [
        _entry(1001, "Synthetic Passer", 0, SF, applied=21.1, projected=18.4, period=period),
        _entry(1002, "Synthetic Runner", 2, DAL, applied=None, projected=15.6, period=period),
        _entry(1003, "Synthetic Defense", 16, LAR, applied=1.0, projected=7.0, period=period),
        _entry(1004, "Synthetic Bench Body", 20, SF, applied=49.3, projected=4.0, period=period),
    ]
    opp_entries = opp_entries if opp_entries is not None else [
        _entry(2001, "Rival Passer", 0, SF, applied=12.0, projected=17.0, period=period),
    ]
    matchup = {
        "matchupPeriodId": period,
        "winner": winner,
        "home": _side(own_team, own_entries, live_points=own_live, total_points=own_total),
    }
    if opponent:
        matchup["away"] = _side(opp_team, opp_entries, live_points=opp_live)
    return {
        "scoringPeriodId": period,
        "status": {"currentMatchupPeriod": period},
        # a second matchup nobody in this test is in, so team selection is real work
        "schedule": [
            {"matchupPeriodId": period, "winner": "UNDECIDED",
             "home": _side(1, []), "away": _side(2, [])},
            matchup,
        ],
    }


@pytest.fixture()
def live_db():
    conn = connect(":memory:")
    apply_schema(conn)
    # schedules for the week the fixture scores: SF and LAR kicked off at 13:00 ET
    # on the Sunday, DAL at 20:20 (the SNF game the Week-1 journal waited on).
    games = [
        ("2026_02_SF_LA", "SF", "LA", "2026-09-20", "13:00"),
        ("2026_02_DAL_NYG", "DAL", "NYG", "2026-09-20", "20:20"),
    ]
    for game_id, away, home, gameday, gametime in games:
        conn.execute(
            "INSERT INTO schedules (game_id, season, week, game_type, gameday, weekday, "
            "gametime, away_team, home_team, retrieved_as_of, knowable_as_of) "
            "VALUES (?, 2026, 2, 'REG', ?, 'Sunday', ?, ?, ?, '2026-08-01', '2026-08-01')",
            (game_id, gameday, gametime, away, home),
        )
    for team_id, name, owner in ((9, "Synthetic Home Team", "{OWNER-9}"),
                                 (4, "Synthetic Away Team", "{OWNER-4}")):
        conn.execute(
            "INSERT INTO league_teams (season, team_id, name, abbrev, primary_owner, "
            "retrieved_as_of, knowable_as_of) VALUES (2026, ?, ?, 'SYN', ?, "
            "'2026-09-20', '2026-09-20')",
            (team_id, name, owner),
        )
    conn.commit()
    yield conn
    conn.close()


def _build(db, payload, *, now, as_of="2026-09-20", team=9, week=None):
    return live.build_live_matchup(db, payload, season=2026, own_team_id=team,
                                   as_of=as_of, now=now, week=week)


SUNDAY_AFTERNOON = datetime(2026, 9, 20, 16, 5, tzinfo=ET)   # 13:00 games running
SUNDAY_NIGHT = datetime(2026, 9, 20, 21, 0, tzinfo=ET)       # 13:00 done, SNF live


# --------------------------------------------------------------- the live number


def test_live_points_come_from_totalPointsLive_not_totalPoints(live_db):
    """The premise of the whole command: ESPN serves totalPoints = 0.0 until the
    period closes (Week-1 journal D6), so a parser that read it renders 0-0 all
    Sunday. The fixture's totalPoints is 0.0 on purpose."""
    m = _build(live_db, live_payload(), now=SUNDAY_AFTERNOON)
    assert m.own.live_points == 22.1
    assert m.own.final_points == 0.0
    assert m.own.points_source == "totalPointsLive"
    assert m.opponent.live_points == 12.0
    assert m.margin == pytest.approx(10.1)


def test_bench_rows_are_split_out_and_never_enter_the_starters_sum(live_db):
    m = _build(live_db, live_payload(), now=SUNDAY_AFTERNOON)
    assert [s.player for s in m.own.starters] == [
        "Synthetic Passer", "Synthetic Runner", "Synthetic Defense"]
    assert [b.player for b in m.own.bench] == ["Synthetic Bench Body"]
    # the bench body carries 49.3 applied points; the sum must ignore him
    assert m.own.starters_total == pytest.approx(22.1)


def test_a_disagreement_between_espn_total_and_the_decoded_starters_is_reported(live_db):
    """ESPN's live team total IS the sum of the starters' applied totals, so a
    mismatch means this module mis-read a lineup slot and is showing a starter
    set the operator's league does not have. It must say so, not pick a side."""
    clean = _build(live_db, live_payload(), now=SUNDAY_AFTERNOON)
    assert not any("DISAGREEMENT" in n for n in clean.notes)

    skewed = _build(live_db, live_payload(own_live=71.4), now=SUNDAY_AFTERNOON)
    note = next(n for n in skewed.notes if "DISAGREEMENT on your side" in n)
    assert "71.40" in note and "22.10" in note and "+49.30" in note
    assert "ESPN's total is the authority" in note


def test_projection_gap_is_realised_minus_espn_projection(live_db):
    m = _build(live_db, live_payload(), now=SUNDAY_AFTERNOON)
    by_name = {s.player: s for s in m.own.starters}
    assert by_name["Synthetic Passer"].projected == 18.4
    assert by_name["Synthetic Passer"].gap == pytest.approx(2.7)
    # no realised points yet -> the gap is absent, NOT a -15.6 "loss"
    assert by_name["Synthetic Runner"].points is None
    assert by_name["Synthetic Runner"].gap is None


def test_a_missing_projection_stays_none_rather_than_zero(live_db):
    entries = [_entry(3001, "Synthetic Unprojected", 0, SF, applied=6.0, projected=None)]
    m = _build(live_db, live_payload(own_entries=entries), now=SUNDAY_AFTERNOON)
    starter = m.own.starters[0]
    assert starter.projected is None and starter.gap is None
    assert "PROJ is ESPN's OWN" in "\n".join(m.notes)


# ------------------------------------------------------------------ game status


def test_status_is_derived_from_the_stored_kickoff_and_the_decision_clock(live_db):
    """13:00 ET games are live at 16:05; the 20:20 SNF game has not kicked off."""
    m = _build(live_db, live_payload(), now=SUNDAY_AFTERNOON)
    status = {s.player: s.status for s in m.own.starters}
    assert status["Synthetic Passer"] == live.STATUS_LIVE       # SF, 13:00
    assert status["Synthetic Defense"] == live.STATUS_LIVE      # LA, 13:00
    assert status["Synthetic Runner"] == live.STATUS_UPCOMING   # DAL, 20:20

    later = _build(live_db, live_payload(), now=SUNDAY_NIGHT)
    status = {s.player: s.status for s in later.own.starters}
    assert status["Synthetic Passer"] == live.STATUS_FINAL
    assert status["Synthetic Runner"] == live.STATUS_LIVE
    assert later.own.count(live.STATUS_FINAL) == 2


def test_a_starter_who_has_already_scored_is_never_reported_yet_to_play(live_db):
    """The stored kickoff can be wrong (a flexed game, a stale schedules pull).
    Saying 'yet to play' about a player ESPN has already given points to is the
    one output a novice WOULD smell, so applied points override the clock."""
    entries = [_entry(4001, "Synthetic Early Scorer", 0, DAL, applied=9.4, projected=12.0)]
    m = _build(live_db, live_payload(own_entries=entries), now=SUNDAY_AFTERNOON)
    assert m.own.starters[0].status == live.STATUS_LIVE


def test_a_not_yet_played_starter_shows_his_kickoff_time(live_db):
    """'when does he play' is the question a Sunday read is actually being
    asked; the Week-1 scratch answered it by printing raw kickoff strings."""
    m = _build(live_db, live_payload(), now=SUNDAY_AFTERNOON)
    runner_row = next(s for s in m.own.starters if s.player == "Synthetic Runner")
    assert runner_row.kickoff == "2026-09-20T20:20-04:00"
    assert "yet to play 20:20ET" in live.format_live_matchup(m)


def test_the_status_hypothesis_is_labelled_on_every_render(live_db):
    """Rule 6: the clock window is a guess and must say so wherever it is shown."""
    m = _build(live_db, live_payload(), now=SUNDAY_AFTERNOON)
    text = live.format_live_matchup(m)
    assert str(live.TYPICAL_GAME_MINUTES) in text
    assert "not from a live game clock" in text


def test_a_starter_whose_team_has_no_game_reads_as_a_bye_not_as_upcoming(live_db):
    entries = [_entry(5001, "Synthetic Bye Guy", 0, 12, applied=None, projected=11.0)]  # KC
    m = _build(live_db, live_payload(own_entries=entries), now=SUNDAY_AFTERNOON)
    assert m.own.starters[0].status == live.STATUS_BYE


# ------------------------------------------------------------------ degradation


def test_no_stored_schedule_says_so_instead_of_calling_everyone_a_bye(live_db):
    """An empty kickoff map means we do not KNOW, and 'no game' would be a
    confident wrong answer for every starter on the card."""
    live_db.execute("DELETE FROM schedules")
    live_db.commit()
    m = _build(live_db, live_payload(), now=SUNDAY_AFTERNOON)
    assert all(s.status == live.STATUS_UNKNOWN for s in m.own.starters)
    assert any("NO KICKOFF TIMES" in n for n in m.notes)
    assert "ziggurat ingest run --source schedules" in live.format_live_matchup(m)


def test_a_closed_period_points_at_the_stored_value(live_db):
    m = _build(live_db, live_payload(winner="HOME", own_live=None, own_total=105.96),
               now=SUNDAY_NIGHT)
    assert m.closed is True
    assert m.own.points_source == "totalPoints" and m.own.live_points == 105.96
    text = live.format_live_matchup(m)
    assert "FINAL" in text and "league_matchups" in text
    assert "no `totalPointsLive`" in text


def test_missing_live_and_final_fields_do_not_render_a_confident_zero(live_db):
    payload = live_payload()
    home = payload["schedule"][1]["home"]
    home.pop("totalPointsLive")
    home.pop("totalPoints")
    m = _build(live_db, payload, now=SUNDAY_AFTERNOON)
    assert m.own.live_points is None and m.own.points_source == "unavailable"
    assert any("NEITHER" in n for n in m.notes)


def test_an_empty_roster_side_says_espn_served_none_not_that_nobody_is_playing(live_db):
    m = _build(live_db, live_payload(own_entries=[], own_live=0.0), now=SUNDAY_AFTERNOON)
    assert any("NO roster rows for your side" in n for n in m.notes)
    live.format_live_matchup(m)  # must not raise on an empty table


def test_a_week_override_discloses_that_the_roster_rows_are_the_current_period(live_db):
    """--week moves the SCORE to another matchup period but ESPN keeps serving
    the CURRENT period's roster, so the table and the total would describe two
    different weeks with nothing saying so."""
    payload = live_payload(period=2)
    payload["schedule"].append({
        "matchupPeriodId": 1, "winner": "HOME",
        "home": _fixture_side(9), "away": _fixture_side(4),
    })
    m = _build(live_db, payload, now=SUNDAY_AFTERNOON, week=1)
    assert m.week == 1 and m.scoring_period == 2
    assert any("pass --scoring-period 1" in n for n in m.notes)


def _fixture_side(team_id):
    return {"teamId": team_id, "totalPoints": 100.0,
            "rosterForCurrentScoringPeriod": {"entries": []}}


def test_a_team_with_no_matchup_raises_a_legible_error(live_db):
    with pytest.raises(live.LiveMatchupUnavailable) as exc:
        _build(live_db, live_payload(), now=SUNDAY_AFTERNOON, team=7)
    assert "no matchup for team 7" in str(exc.value)


def test_a_half_matchup_renders_your_side_and_says_the_opponent_is_missing(live_db):
    m = _build(live_db, live_payload(opponent=False), now=SUNDAY_AFTERNOON)
    assert m.opponent is None and m.margin is None
    assert any("no opponent side" in n for n in m.notes)
    live.format_live_matchup(m)  # must not raise


# ------------------------------------------------------------------ resolution


def test_the_matchup_period_comes_from_espn_unless_overridden(live_db):
    payload = live_payload(period=2)
    assert live.resolve_week(payload) == 2
    assert live.resolve_week(payload, week=5) == 5
    payload.pop("status")
    assert live.resolve_week(payload) == 2  # falls back to scoringPeriodId
    payload.pop("scoringPeriodId")
    assert live.resolve_week(payload) is None


def test_own_team_is_resolved_from_the_swid_never_hard_coded(live_db):
    """Same resolution `league sync` uses — the team number is a fact about the
    synced league, not a constant, and the SWID is matched, never echoed."""
    seen = {}

    def _fetch(**kwargs):
        seen.update(kwargs)
        return live_payload()

    m = live.read_live_matchup(
        live_db, season=2026, league_id=42, espn_s2="s2", swid="{owner-9}",
        as_of="2026-09-20", now=SUNDAY_AFTERNOON, fetch=_fetch,
    )
    assert m.own.team_id == 9 and m.own.team_name == "Synthetic Home Team"
    assert seen["scoring_period"] is None  # ESPN picks the current period
    assert "{owner-9}" not in live.format_live_matchup(m)


def test_team_override_skips_swid_resolution(live_db):
    m = live.read_live_matchup(
        live_db, season=2026, league_id=42, espn_s2="s2", swid="{nobody}",
        as_of="2026-09-20", now=SUNDAY_AFTERNOON, team_id=4,
        fetch=lambda **kw: live_payload(),
    )
    assert m.own.team_id == 4 and m.opponent.team_id == 9


def test_now_et_reads_a_naive_stamp_as_eastern(live_db):
    assert live.now_et("2026-09-20T16:05").utcoffset().total_seconds() == -4 * 3600
    assert live.now_et("2026-09-20T13:05-07:00").hour == 16  # converted to ET


# ------------------------------------------------------------------ network seam


def test_fetch_live_scoreboard_refuses_an_empty_schedule():
    """A blank scoreboard on a Sunday afternoon is indistinguishable from 'your
    starters have not played yet' — refuse rather than render it."""
    with patch.object(source, "_client", return_value=object()), \
         patch.object(source, "_request", return_value={"schedule": []}):
        with pytest.raises(RuntimeError, match="no schedule"):
            source.fetch_live_scoreboard(league_id=42, season=2026, espn_s2="s2", swid="{x}")


def test_fetch_live_scoreboard_asks_for_the_box_score_view_and_omits_the_period():
    calls = {}

    def _request(league, *, params, headers=None, extend=""):
        calls["params"] = params
        return {"schedule": [{"matchupPeriodId": 1}]}

    with patch.object(source, "_client", return_value=object()), \
         patch.object(source, "_request", _request):
        source.fetch_live_scoreboard(league_id=42, season=2026, espn_s2="s2", swid="{x}")
        # mBoxscore is what carries totalPointsLive + rosterForCurrentScoringPeriod
        assert "mBoxscore" in calls["params"]["view"]
        assert "scoringPeriodId" not in calls["params"]

        source.fetch_live_scoreboard(league_id=42, season=2026, espn_s2="s2", swid="{x}",
                                     scoring_period=3)
        assert calls["params"]["scoringPeriodId"] == 3


def test_fetch_live_scoreboard_unwraps_the_leagueHistory_array_form():
    with patch.object(source, "_client", return_value=object()), \
         patch.object(source, "_request", return_value=[{"schedule": [{"matchupPeriodId": 1}]}]):
        payload = source.fetch_live_scoreboard(league_id=42, season=2026, espn_s2="s2", swid="{x}")
    assert payload["schedule"][0]["matchupPeriodId"] == 1


# ------------------------------------------------------------------ CLI (rule 3)


def test_cli_live_prints_the_card_and_hides_the_bench_by_default(tmp_path, live_db):
    db_path = tmp_path / "live.sqlite"
    conn = connect(db_path)
    apply_schema(conn)
    for row in live_db.execute("SELECT * FROM schedules"):
        conn.execute("INSERT INTO schedules VALUES (" + ",".join("?" * len(row)) + ")", tuple(row))
    for row in live_db.execute("SELECT * FROM league_teams"):
        conn.execute("INSERT INTO league_teams VALUES (" + ",".join("?" * len(row)) + ")", tuple(row))
    conn.commit()
    conn.close()

    with patch("ziggurat.cli.main.load_espn_credentials",
               return_value={"league_id": 42, "espn_s2": "s2", "swid": "{OWNER-9}"}), \
         patch.object(source, "fetch_live_scoreboard", return_value=live_payload()):
        result = runner.invoke(app, ["league", "live", "--path", str(db_path),
                                     "--season", "2026", "--as-of", "2026-09-20",
                                     "--now", "2026-09-20T16:05"])
    assert result.exit_code == 0, result.output
    assert "Synthetic Passer" in result.output
    assert "Synthetic Bench Body" not in result.output
    assert "bench/IR row(s) hidden" in result.output

    with patch("ziggurat.cli.main.load_espn_credentials",
               return_value={"league_id": 42, "espn_s2": "s2", "swid": "{OWNER-9}"}), \
         patch.object(source, "fetch_live_scoreboard", return_value=live_payload()):
        result = runner.invoke(app, ["league", "live", "--path", str(db_path),
                                     "--season", "2026", "--as-of", "2026-09-20",
                                     "--now", "2026-09-20T16:05", "--bench"])
    assert "Synthetic Bench Body" in result.output


def test_cli_live_exits_nonzero_with_a_sentence_when_the_team_has_no_matchup(tmp_path):
    db_path = tmp_path / "empty.sqlite"
    conn = connect(db_path)
    apply_schema(conn)
    conn.close()
    with patch("ziggurat.cli.main.load_espn_credentials",
               return_value={"league_id": 42, "espn_s2": "s2", "swid": "{OWNER-9}"}), \
         patch.object(source, "fetch_live_scoreboard", return_value=live_payload()):
        result = runner.invoke(app, ["league", "live", "--path", str(db_path),
                                     "--season", "2026", "--team", "7",
                                     "--now", "2026-09-20T16:05"])
    assert result.exit_code == 1
    assert "no matchup for team 7" in result.output
    assert "Traceback" not in result.output


def test_the_live_module_holds_the_logic_and_the_cli_only_parses(tmp_path):
    """Rule 3. The command body must not grow a branch — every decision the card
    makes lives in ziggurat/league/live.py."""
    src = Path(__file__).resolve().parents[1] / "ziggurat" / "cli" / "main.py"
    body = src.read_text(encoding="utf-8").split('@league_app.command("live")')[1]
    body = body.split('@league_app.command("holdings")')[0]
    assert " if " not in body and "\n    for " not in body


def test_nothing_in_the_live_path_writes_to_the_database(live_db):
    """READ-ONLY is the command's contract (no migration, no snapshot, no run
    row) — a Sunday tool that mutated the perishable snapshot would be a much
    worse bug than the gap it fixes."""
    before = {
        t[0]: live_db.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]  # noqa: S608
        for t in live_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    live.read_live_matchup(
        live_db, season=2026, league_id=42, espn_s2="s2", swid="{OWNER-9}",
        as_of="2026-09-20", now=SUNDAY_AFTERNOON, fetch=lambda **kw: live_payload(),
    )
    after = {
        t[0]: live_db.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]  # noqa: S608
        for t in live_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert before == after
