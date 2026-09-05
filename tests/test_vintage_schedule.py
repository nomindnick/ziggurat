"""Item 4.2b (unit B6) — the Tue/Thu stat-vintage schedule.

The design is two dedicated systemd units that pull ``weekly_stats`` and
``snap_counts`` FORCED at 08:00 on Tuesday and Thursday, ahead of the 08:20
weekly group. Tuesday is the copy the waiver decision is actually made on;
Thursday is the clean copy, because NFL stat corrections land Monday-Wednesday.

The hazard this file exists for is not a bug in either unit — it is what
``--force`` does to the other one. ``--force`` bypasses the interval gate but
STILL writes an anchoring run-log row, so a Tuesday pull makes both sources read
``fresh`` (age 2 < interval 7) for the rest of the week: the 08:20 weekly group
skips them, the 7-day self-heal that group provides stops covering them, and
``ziggurat ingest status`` says ``fresh`` throughout. Installing the Tuesday unit
alone would therefore not ADD the decision-day vintage, it would SILENTLY TRADE
AWAY the post-correction one.

That cannot be seen by watching one Tuesday, so it is tested the way the recon
said to test it: simulate ``refresh.decide`` across a full week, every unit that
fires, in the order they fire.

OFFLINE by construction — no source is ever pulled here. A simulated tick calls
the real ``decide`` and, when it says "pull", writes the run-log row a successful
pull would have left. The cadence decision IS the thing under test.
"""

import re

from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.data.nfl import refresh
from ziggurat.paths import REPO_ROOT

runner = CliRunner()

SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"
INSTALLER = REPO_ROOT / "scripts" / "install-nfl-ingest.sh"

VINTAGE_UNITS = ("ziggurat-nfl-ingest-vintage-tue", "ziggurat-nfl-ingest-vintage-thu")
#: The two sources the pair captures twice a week.
VINTAGE_SOURCES = ("weekly_stats", "snap_counts")

# One in-season week, keyed off the schedule the helper below writes: week 1's
# gameday is 2026-09-10, so week 5 is 2026-10-08 and the Monday before it is
# 2026-10-05. Days are spelled out rather than computed so the fixture reads as a
# calendar, which is what the units are scheduled against.
WEEK = {
    "mon": "2026-10-05", "tue": "2026-10-06", "wed": "2026-10-07",
    "thu": "2026-10-08", "fri": "2026-10-09", "sat": "2026-10-10",
    "sun": "2026-10-11",
}
#: The previous week's Thursday vintage. Seeded so a simulated week starts in
#: STEADY STATE — on a virgin database every source is due and the first day of
#: any week pulls, which would prove nothing about the interval gate.
LAST_THURSDAY = "2026-10-01"


# --------------------------------------------------------------------- helpers


def _schedule_rows(db, season=2026, first="2026-09-10"):
    """Minimal REG schedule so ``season_phase`` resolves to inseason."""
    from datetime import date

    start = date.fromisoformat(first)
    db.executemany(
        "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, knowable_as_of, retrieved_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
        [(f"{season}_{w:02d}_AAA_BBB", season, w, "REG",
          date.fromordinal(start.toordinal() + (w - 1) * 7).isoformat(), "BBB", "AAA",
          f"{season}-08-01", f"{season}-08-01") for w in range(1, 19)],
    )
    db.commit()


def _land(db, source, day, season=2026):
    """The run-log row a SUCCESSFUL pull leaves behind — the anchor the interval
    gate reads. Written by the simulation whenever ``decide`` says "pull"."""
    run_id = refresh.start_run(db, batch_id=f"sim-{day}", source=source, season=season,
                               scope=None, retrieved_as_of=day,
                               started_at=f"{day}T08:00:00+00:00")
    refresh.finish_run(db, run_id, status=refresh.STATUS_OK,
                       finished_at=f"{day}T08:00:02+00:00", rows_written=19_000)


def _tick(db, day, *, force, season=2026):
    """One timer firing over the two vintage sources: decide, then land what it
    decided to pull. Returns {source: action}."""
    actions = {}
    for name in VINTAGE_SOURCES:
        decision = refresh.decide(db, refresh.SOURCES_BY_NAME[name], season=season,
                                  today=day, have_credentials=False, force=force)
        actions[name] = decision.action
        if decision.action == "pull":
            _land(db, name, day, season=season)
    return actions


def _landings(db, source, season=2026):
    return [r["retrieved_as_of"] for r in db.execute(
        "SELECT retrieved_as_of FROM nfl_ingest_runs WHERE source = ? AND season = ? "
        "AND status = ? ORDER BY retrieved_as_of", (source, season, refresh.STATUS_OK))]


def _verdict(db, source, today, season=2026):
    rows = refresh.source_freshness(db, season=season, today=today)
    return next(r for r in rows if r["source"] == source)["verdict"]


# ------------------------------------------------------- the premises it rests on


def test_the_two_vintage_sources_are_weekly_and_seven_day_intervalled():
    """Everything below is about the gap between a 7-day interval and a 2-day
    cadence. If either source is ever re-grouped or re-intervalled, the pair's
    whole reason for existing changes and this fails first."""
    for name in VINTAGE_SOURCES:
        spec = refresh.SOURCES_BY_NAME[name]
        assert spec.group == refresh.GROUP_WEEKLY, name
        assert spec.interval_days == 7, name
        assert not spec.perishable, name       # re-pullable files; the OBSERVATION is not


# ---------------------------------------------------------- the week simulation


def test_a_full_week_lands_both_the_tuesday_and_the_thursday_vintage(db):
    """The done-when, simulated across every firing in one week.

    Each day the 08:20 weekly group fires (unforced, as it really does — it is a
    daily timer with an interval gate). On Tuesday and Thursday the vintage units
    fire first, at 08:00, forced.
    """
    _schedule_rows(db)
    for source in VINTAGE_SOURCES:
        _land(db, source, LAST_THURSDAY)           # steady state, not a virgin DB
    weekly_group = {}
    for day_name, day in WEEK.items():
        if day_name in ("tue", "thu"):
            forced = _tick(db, day, force=True)
            assert set(forced.values()) == {"pull"}, (day_name, forced)
        weekly_group[day_name] = _tick(db, day, force=False)

    for source in VINTAGE_SOURCES:
        assert _landings(db, source) == [LAST_THURSDAY, WEEK["tue"], WEEK["thu"]], source

    # The 08:20 group never pulled these two on any day of the week: on Monday
    # because the previous week's Thursday anchored it, and afterwards because the
    # vintage units did. That is the ordering working, not a hole.
    assert all(action == refresh.STATUS_FRESH
               for actions in weekly_group.values() for action in actions.values()), weekly_group


def test_the_forced_tuesday_pull_disables_the_weekly_groups_self_heal(db):
    """THE HAZARD, stated as a test rather than discovered in a retro.

    ``--force`` still writes an ANCHORING row, so from Tuesday onward the weekly
    group reads both sources as fresh — including on THURSDAY, the day it used to
    take the clean post-correction copy. Without the Thursday unit the pair is not
    two vintages, it is the early one INSTEAD of the good one.
    """
    _schedule_rows(db)
    _tick(db, WEEK["tue"], force=True)

    thursday = _tick(db, WEEK["thu"], force=False)      # the weekly group, unforced
    assert set(thursday.values()) == {refresh.STATUS_FRESH}, thursday
    for source in VINTAGE_SOURCES:
        assert _landings(db, source) == [WEEK["tue"]], source
        # ...and the interval gate says so in its own words
        decision = refresh.decide(db, refresh.SOURCES_BY_NAME[source], season=2026,
                                  today=WEEK["thu"], have_credentials=False)
        assert "refreshes every 7d" in decision.reason

    # The forced Thursday unit is the only thing that gets the clean copy back.
    assert set(_tick(db, WEEK["thu"], force=True).values()) == {"pull"}
    for source in VINTAGE_SOURCES:
        assert _landings(db, source) == [WEEK["tue"], WEEK["thu"]], source


def test_ingest_status_cannot_see_a_missing_vintage(db):
    """Why the units' own run-log rows are the health signal.

    ``source_freshness`` reads the same anchor the interval gate does, so after
    ANY landing this week both sources read fresh — whether the pair is complete
    or half of it silently failed. A green `ziggurat ingest status` is therefore
    not evidence that Thursday's vintage exists, and nothing that consumes the
    pair may treat it as such.
    """
    _schedule_rows(db)
    _tick(db, WEEK["tue"], force=True)                  # Tuesday lands, Thursday never runs
    for source in VINTAGE_SOURCES:
        assert _verdict(db, source, WEEK["fri"]) == refresh.VERDICT_FRESH
        assert _landings(db, source) == [WEEK["tue"]]    # the run log tells the truth


def test_a_failed_tuesday_leaves_thursday_to_land_on_its_own(db):
    """The other direction: a vintage unit that FAILS writes no anchor, so nothing
    downstream is poisoned — the Thursday unit still pulls (it is forced anyway)
    and the week ends with one vintage rather than none."""
    _schedule_rows(db)
    run_id = refresh.start_run(db, batch_id="sim", source="weekly_stats", season=2026,
                               scope=None, retrieved_as_of=WEEK["tue"],
                               started_at=f"{WEEK['tue']}T08:00:00+00:00")
    refresh.finish_run(db, run_id, status=refresh.STATUS_FAILED,
                       finished_at=f"{WEEK['tue']}T08:00:02+00:00", error="nflverse 500")

    assert _tick(db, WEEK["thu"], force=True)["weekly_stats"] == "pull"
    assert _landings(db, "weekly_stats") == [WEEK["thu"]]


# --------------------------------------------------------------- the unit files


def _unit(name: str) -> str:
    return (SYSTEMD_DIR / name).read_text(encoding="utf-8")


def _exec_start(service: str) -> list[str]:
    """The ExecStart command line, as CLI argv (the @REPO@ binary path dropped)."""
    line = next(ln for ln in _unit(service).splitlines() if ln.startswith("ExecStart="))
    return line.split("=", 1)[1].split()[1:]


def test_both_vintage_units_ship_and_are_installed_together():
    """The pair is ONE mechanism: the Tuesday unit alone trades the clean copy for
    the early one. If a future edit installs half of it, this fails."""
    installer = INSTALLER.read_text(encoding="utf-8")
    for unit in VINTAGE_UNITS:
        assert (SYSTEMD_DIR / f"{unit}.service").is_file(), unit
        assert (SYSTEMD_DIR / f"{unit}.timer").is_file(), unit
        assert unit in installer, f"{unit} is not in the installer's UNITS list"


def test_the_vintage_units_run_a_command_the_real_cli_accepts():
    """The units run the working tree, so a renamed flag is a daily failure at
    08:00 with nothing in the suite complaining. Re-derive the command instead."""
    help_text = runner.invoke(app, ["ingest", "run", "--help"])
    assert help_text.exit_code == 0
    for unit in VINTAGE_UNITS:
        argv = _exec_start(f"{unit}.service")
        assert argv[:2] == ["ingest", "run"], argv
        for flag in [a for a in argv if a.startswith("--")]:
            assert flag in help_text.output, f"{unit}: {flag} is not a real flag"
        assert "--force" in argv, f"{unit} must force — the interval gate skips it otherwise"
        sources = [argv[i + 1] for i, a in enumerate(argv) if a == "--source"]
        assert sources == list(VINTAGE_SOURCES), (unit, sources)
        for name in sources:
            assert name in refresh.SOURCES_BY_NAME, f"{unit} names an unknown source: {name}"
        # --group and --source are mutually exclusive, which is why this cannot be
        # folded into the weekly unit.
        assert "--group" not in argv


def test_the_vintage_timers_fire_on_their_day_ahead_of_the_weekly_group():
    """08:00 Tue / 08:00 Thu, and no RandomizedDelaySec: the siblings jitter by up
    to 30 minutes to spread load, and a jitter here could land AFTER the 08:20
    group, at which point the group takes the vintage and the forced pull is a
    same-day duplicate. The ordering is the whole mechanism."""
    days = {"ziggurat-nfl-ingest-vintage-tue": "Tue",
            "ziggurat-nfl-ingest-vintage-thu": "Thu"}
    for unit, day in days.items():
        timer = _unit(f"{unit}.timer")
        assert re.search(rf"^OnCalendar={day} \*-\*-\* 08:00:00$", timer, re.M), unit
        # the DIRECTIVE, not the comment that explains why it is absent
        assert not re.search(r"^RandomizedDelaySec=", timer, re.M), unit
        assert re.search(r"^Unit=" + unit + r"\.service$", timer, re.M), unit
    weekly = _unit("ziggurat-nfl-ingest-weekly.timer")
    assert "OnCalendar=*-*-* 08:20:00" in weekly           # the group they run ahead of
    assert re.search(r"^RandomizedDelaySec=", weekly, re.M)  # which DOES jitter, unlike these


def test_the_units_state_the_consequence_they_create():
    """Rule 6/7 applied to a cadence file: the pair disables the weekly group's
    7-day self-heal for these two sources, and the health signal moves to the run
    log. An operator reading the unit must find that written down, because
    `ingest status` will never tell them (see the simulation above)."""
    for unit in VINTAGE_UNITS:
        text = _unit(f"{unit}.service").lower()
        assert "--force" in text and "anchor" in text
        assert "self-heal" in text, unit
        assert "nfl_ingest_runs" in text, unit
