"""Item 3.12's amendment (2026-11-06) — the pre-lock WAVES timer.

The base league-sync timer runs 4x/day plus two Sunday-morning pre-lock runs.
Its 17:15 run carries a 300 s random delay, so it lands at or after a Thursday
or Monday 17:15 PT kickoff: the inactives for those waves were never synced
before the lock. The fix is a SECOND timer on the same service.

What this file pins is the shape of that fix, because the dangerous version of
it looks almost identical: a drop-in on the BASE timer needs an `OnCalendar=`
reset line, and one empty `OnCalendar=` silently deletes the base schedule that
protects league history ESPN cannot give back (item 3.1). So:

* the waves timer exists, triggers the base SERVICE, and never resets a schedule;
* the base timer's own schedule is byte-for-byte what it was;
* no drop-in directory for the base timer ships;
* the installer installs, dry-runs and uninstalls both timers;
* every dated wave run lands after its wave's inactives (~90 min before
  kickoff) and at least 60 min before the kickoff it serves.

OFFLINE: reads the unit files only.
"""

import re
from datetime import datetime, timedelta

from ziggurat.paths import REPO_ROOT

SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"
INSTALLER = REPO_ROOT / "scripts" / "install-league-sync.sh"
WAVES = SYSTEMD_DIR / "ziggurat-league-sync-waves.timer"
BASE = SYSTEMD_DIR / "ziggurat-league-sync.timer"

#: The base timer's schedule, which this item must not move.
BASE_SCHEDULE = [
    "*-*-* 05,11,17,23:15:00",
    "Sun *-*-* 09:15:00",
    "Sun *-*-* 12:15:00",
]

#: Non-Sunday-morning kickoffs (Pacific) the DATED entries serve, from the 2026
#: `schedules` table read 2026-11-06 (it stores Eastern; minus 3 h here).
DATED_KICKOFFS_PT = {
    "2026-11-25 15:45:00": "2026-11-25 17:00",  # wk12 Wed night
    "2026-11-26 08:45:00": "2026-11-26 10:00",  # Thanksgiving early
    "2026-11-26 12:15:00": "2026-11-26 13:30",  # Thanksgiving late afternoon
    "2026-12-19 12:45:00": "2026-12-19 14:00",  # wk15 Saturday afternoon
    "2026-12-19 16:05:00": "2026-12-19 17:20",  # wk15 Saturday night
    "2026-12-25 08:45:00": "2026-12-25 10:00",  # Christmas early
    "2026-12-25 12:15:00": "2026-12-25 13:30",  # Christmas afternoon
    "2026-12-25 16:00:00": "2026-12-25 17:15",  # Christmas night
    "2027-01-03 16:05:00": "2027-01-03 17:20",  # wk17 SNF
    "2027-01-04 16:00:00": "2027-01-04 17:15",  # wk17 MNF
}


def _oncalendar(path):
    return [
        line.split("=", 1)[1].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("OnCalendar")
    ]


def test_the_waves_timer_triggers_the_base_service_and_resets_nothing():
    text = WAVES.read_text(encoding="utf-8")
    assert re.search(r"^Unit=ziggurat-league-sync\.service$", text, re.M)
    # an empty OnCalendar= is a RESET; in a second timer it is harmless, but it
    # is the exact line that would wipe the base schedule if this file were ever
    # turned into a drop-in, so it is refused outright
    assert all(_oncalendar(WAVES)), "empty OnCalendar= reset line in the waves timer"
    assert len(_oncalendar(WAVES)) >= 4


def test_the_base_schedule_is_untouched_and_no_drop_in_ships():
    assert _oncalendar(BASE) == BASE_SCHEDULE
    assert not (SYSTEMD_DIR / "ziggurat-league-sync.timer.d").exists()


def test_the_regular_entries_cover_thursday_sunday_and_monday_nights():
    cal = _oncalendar(WAVES)
    assert "Thu,Mon 2026-11,12-* 16:00:00" in cal
    assert "Sun 2026-11,12-* 16:05:00" in cal


def test_every_dated_wave_lands_after_inactives_and_an_hour_before_kickoff():
    cal = set(_oncalendar(WAVES))
    fmt = "%Y-%m-%d %H:%M"
    for entry, kickoff in DATED_KICKOFFS_PT.items():
        assert entry in cal, f"missing dated wave {entry}"
        run = datetime.strptime(entry[:16], fmt)
        ko = datetime.strptime(kickoff, fmt)
        inactives = ko - timedelta(minutes=90)
        # RandomizedDelaySec=120 can push a run up to 2 min later
        assert run >= inactives, f"{entry} precedes the inactives for {kickoff}"
        assert ko - (run + timedelta(minutes=2)) >= timedelta(minutes=60), entry


def test_the_waves_timer_is_not_persistent_and_keeps_a_small_spread():
    text = WAVES.read_text(encoding="utf-8")
    assert re.search(r"^Persistent=false$", text, re.M)
    delay = int(re.search(r"^RandomizedDelaySec=(\d+)$", text, re.M).group(1))
    assert delay <= 300


def test_the_installer_installs_dry_runs_and_uninstalls_both_timers():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "render ziggurat-league-sync-waves.timer >" in text
    assert "enable --now ziggurat-league-sync-waves.timer" in text
    assert "disable --now ziggurat-league-sync-waves.timer" in text
    assert re.search(r"rm -f [^\n]*ziggurat-league-sync-waves\.timer", text)
    dry = text.split("if [[ $DRY_RUN -eq 1 ]]; then", 1)[1].split("fi", 1)[0]
    assert "render ziggurat-league-sync-waves.timer" in dry
