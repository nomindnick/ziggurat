"""Item 4.2b (audit T1) — the decision-archive timer's own unit files.

MUTATION-MISSED BEFORE THIS FILE EXISTED. The Tuesday 18:30 freeze is D1(c): the
half of the archive that captures a Tuesday the operator never opened, i.e. the
unrecoverable case the whole item exists for. Its ExecStart was pinned by
nothing — rewriting it to two flags that do not exist (`--claim-budgets`,
`--triggered`) left the whole suite green, because `tests/test_vintage_schedule.py`
reads only the two vintage units and `tests/test_operating_cadence.py` resolves
only the invocations CLAUDE.md quotes, and CLAUDE.md never quotes
`ziggurat decisions freeze`.

The units run `ziggurat` FROM THE WORKING TREE, so a renamed flag is not a test
failure, it is a silent 18:30 failure every Tuesday for the rest of the season.
Re-derive the command against the real CLI instead of trusting the file.

OFFLINE: nothing here starts a unit, installs anything or opens a database.
"""

import re

from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.paths import REPO_ROOT

runner = CliRunner()

SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"
INSTALLER = REPO_ROOT / "scripts" / "install-decisions.sh"
UNIT = "ziggurat-decisions"


def _unit(name: str) -> str:
    return (SYSTEMD_DIR / name).read_text(encoding="utf-8")


def _exec_start(service: str) -> list[str]:
    """The ExecStart command line, as CLI argv (the @REPO@ binary path dropped)."""
    line = next(ln for ln in _unit(service).splitlines() if ln.startswith("ExecStart="))
    return line.split("=", 1)[1].split()[1:]


def test_the_decision_unit_ships_and_the_installer_names_it():
    assert (SYSTEMD_DIR / f"{UNIT}.service").is_file()
    assert (SYSTEMD_DIR / f"{UNIT}.timer").is_file()
    assert UNIT in INSTALLER.read_text(encoding="utf-8"), \
        f"{UNIT} is not in the installer's UNITS list"


def test_the_decision_unit_runs_a_command_the_real_cli_accepts():
    """The pin the vintage pair already had, applied to the unit that captures
    the unrecoverable thing."""
    help_text = runner.invoke(app, ["decisions", "freeze", "--help"])
    assert help_text.exit_code == 0

    argv = _exec_start(f"{UNIT}.service")
    assert argv[:2] == ["decisions", "freeze"], argv
    for flag in [a for a in argv if a.startswith("--")]:
        assert flag in help_text.output, f"{UNIT}: {flag} is not a real flag"
    # The two the design decided, by value and not only by name.
    assert "--claim-budget" in argv and argv[argv.index("--claim-budget") + 1] == "10", (
        "the archive should hold the DEEP list the Tuesday cadence tells the "
        "operator to run, not the quick-scan default of 3"
    )
    assert argv[argv.index("--trigger") + 1] == "timer", (
        "an unattended capture must be distinguishable from the run the operator "
        "watched — see the three-value trigger vocabulary"
    )
    from ziggurat.decisions import store

    assert argv[argv.index("--trigger") + 1] == store.TRIGGER_TIMER


def test_the_decision_timer_fires_on_tuesday_evening_after_the_league_sync():
    """18:30 is not decoration: it is AFTER the 17:15 PT league sync (which lands
    today's rosters and today's pool) and BEFORE the operator's 19:00-21:00
    unavailable window. The unit's own comment says the ordering is the whole
    point of the hour, which is why there is no RandomizedDelaySec to blur it."""
    timer = _unit(f"{UNIT}.timer")
    assert re.search(r"^OnCalendar=Tue \*-\*-\* 18:30:00$", timer, re.M), timer
    assert not re.search(r"^RandomizedDelaySec=", timer, re.M), (
        "a randomised delay could push this before the 17:15 sync's data lands or "
        "into the operator's evening"
    )
    assert re.search(r"^Persistent=true$", timer, re.M)


def test_the_unit_does_not_claim_a_visibility_it_cannot_deliver():
    """The service file asserts a failed Tuesday is visible three ways — journal,
    a `failed` row, and `decisions status`. That is now true of EVERY failure
    path, including the most likely one (expired ESPN cookies), because the
    run-log row is written before the plan and the credential failure is recorded
    by `capture.record_prerun_failure` (item 4.2b audit, OPS-3). Pin both halves
    so the sentence and the mechanism cannot drift apart."""
    from ziggurat.decisions import capture

    service = _unit(f"{UNIT}.service")
    assert "failed` row in decision_freezes" in service
    assert callable(capture.record_prerun_failure)
    source = (REPO_ROOT / "ziggurat" / "cli" / "main.py").read_text(encoding="utf-8")
    assert "record_prerun_failure" in source, (
        "the CLI resolves ESPN credentials BEFORE run_freeze can record anything; "
        "without this call a credential failure leaves the journal as its only trace"
    )


def test_the_runbook_installs_the_freeze_timer_on_a_new_host():
    """A host cutover that follows the runbook must not silently drop D1(c)
    (item 4.2b audit, DC-10/OPS-12)."""
    runbook = (REPO_ROOT / "docs" / "runbook-strix-halo.md").read_text(encoding="utf-8")
    assert "scripts/install-decisions.sh" in runbook
    assert "scripts/install-decisions.sh --uninstall" in runbook
    assert UNIT in runbook, "the timer table does not list the decision freeze"


def test_the_installer_help_stops_before_the_shell():
    """`--help` prints a header block, not a shell directive (item 4.2b audit,
    R6/OPS-13). The sibling installer stops one line short of `set -euo` for the
    same reason."""
    text = INSTALLER.read_text(encoding="utf-8")
    lines = text.splitlines()
    match = re.search(r"sed -n '2,(\d+)p'", text)
    assert match, "the --help slice is gone"
    last = int(match.group(1))
    assert lines[last - 1].startswith("#"), (
        f"--help prints line {last}, which is not a comment: {lines[last - 1]!r}"
    )
    assert lines[last].startswith("set -euo"), (
        "the slice should end exactly where the header does"
    )
