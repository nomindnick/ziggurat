"""The draft-day runbook is an interface, not prose (same discipline as item
3.7's cadence tests).

`docs/draft-day-runbook.md` is what a fresh session — or the operator at 18:45
on draft night — executes verbatim. Every `ziggurat ...` line it quotes must
resolve against the real CLI, and the facts it hard-codes (the userscript
versions to install, the routes that serve them, the flags it forbids) must
still describe the code. A renamed flag or a bumped userscript version rots
this file silently, and the failure lands during the one hour of the year that
cannot absorb it.

Deletable with `ziggurat/draft/` after draft day (Rule 8), like every other
`test_draft_*` module.
"""

import re

from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.paths import REPO_ROOT

runner = CliRunner()

_RUNBOOK = REPO_ROOT / "docs" / "draft-day-runbook.md"
_DRAFT_PKG = REPO_ROOT / "ziggurat" / "draft"

# Same shape as the cadence guard: `ziggurat <cmd> [<sub>] [--flag ...]*`.
_INVOCATION = re.compile(
    r"ziggurat ([a-z][a-z-]*)((?: [a-z][a-z-]*)?)((?:\s+--[a-z-]+)*)"
)


def _runbook() -> str:
    return _RUNBOOK.read_text(encoding="utf-8")


def _quoted_invocations():
    seen = set()
    for cmd, sub, flags in _INVOCATION.findall(_runbook()):
        args = [cmd] + ([sub.strip()] if sub.strip() else [])
        key = (tuple(args), tuple(flags.split()))
        if key not in seen:
            seen.add(key)
            yield args, flags.split()


def test_the_runbook_quotes_a_nonempty_command_set():
    """Guard the guard: a regex that matches nothing makes every assertion
    below vacuously pass."""
    invocations = list(_quoted_invocations())
    assert len(invocations) >= 4
    quoted = {tuple(args) for args, _ in invocations}
    for expected in [("draft-web",), ("league", "status"), ("ingest", "status")]:
        assert expected in quoted, f"runbook no longer quotes `ziggurat {' '.join(expected)}`"


def test_every_quoted_invocation_resolves_against_the_real_cli():
    for args, flags in _quoted_invocations():
        result = runner.invoke(app, args + ["--help"])
        assert result.exit_code == 0, (
            f"the runbook quotes `ziggurat {' '.join(args)}` "
            f"but the CLI refuses it:\n{result.output}"
        )
        for flag in flags:
            assert flag in result.output, (
                f"the runbook quotes `ziggurat {' '.join(args)} {flag}` "
                f"but {flag} is not in that command's --help"
            )


def test_the_forbidden_flags_still_exist():
    """§3 tells the operator NOT to pass four specific flags, each with a
    concrete consequence. If one is renamed the warning silently stops
    describing anything — worse than no warning, because it reads as covered."""
    result = runner.invoke(app, ["draft-web", "--help"])
    assert result.exit_code == 0
    body = _runbook()
    for flag in ("--pick-order", "--port", "--journal", "--resume"):
        assert flag in body, f"runbook no longer warns about {flag}"
        assert flag in result.output, (
            f"the runbook warns against `draft-web {flag}` but that flag no "
            "longer exists — the warning now describes nothing"
        )


def test_the_userscript_versions_match_the_shipped_files():
    """The runbook tells the operator which versions must be installed in
    Tampermonkey. Installed-vs-repo drift is a real draft-night failure (the
    scripts carry the port and token compiled in), so the numbers must track
    the files rather than the last time someone remembered to edit prose."""
    body = _runbook()
    for filename, label in (("espn_queue.user.js", "queue writer"),
                            ("espn_sync.user.js", "sync")):
        source = (_DRAFT_PKG / filename).read_text(encoding="utf-8")
        version = re.search(r"^// @version\s+(\S+)", source, re.M)
        assert version, f"{filename} has no @version header"
        assert f"v{version.group(1)}" in body, (
            f"{filename} is at v{version.group(1)} but the runbook does not "
            f"name that version for the {label} userscript"
        )


def test_the_runbook_names_the_routes_that_serve_the_userscripts():
    webapp = (_DRAFT_PKG / "webapp.py").read_text(encoding="utf-8")
    body = _runbook()
    for route in ("/sync.user.js", "/queue.user.js"):
        assert route in webapp, f"{route} is no longer served by webapp.py"
        assert route in body, f"runbook no longer names {route}"


def test_the_runbook_carries_the_draft_facts_a_fresh_session_needs():
    body = _runbook()
    # the seat, and the port the installed userscripts are compiled against
    assert "--slot 9" in body
    assert "8811" in body
    # the setting the whole queue-first design depends on, and whose it is
    assert "Autopick" in body
    # the token file that must survive between sessions
    assert "sync-token.txt" in body
