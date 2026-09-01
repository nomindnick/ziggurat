"""The draft-day runbook is an interface, not prose (same discipline as item
3.7's cadence tests).

`docs/draft-day-runbook.md` is what a fresh session — or the operator at 18:45
on draft night — executes verbatim. Every `ziggurat ...` line it quotes must
resolve against the real CLI, and the facts it hard-codes (the userscript
versions to install, the routes that serve them, the flags it forbids) must
still describe the code. A renamed flag or a bumped userscript version rots
this file silently, and the failure lands during the one hour of the year that
cannot absorb it.

Part of the import-quarantined `ziggurat/draft/` surface (Rule 8; retained across seasons), like every other
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


def test_the_escape_hatch_the_runbook_promises_actually_exists():
    """§6 rung 0 tells the operator that one flag restores the pre-3.11 engine.

    The generic invocation scan cannot see it: the runbook quotes it AFTER
    positional-looking arguments (`--season 2026 --slot 9 --legacy-engine`) and
    the shared regex stops at the first value. So the promise gets its own check
    — a fallback the doc names and the CLI does not have is the worst kind of
    stale instruction, because it reads as covered.
    """
    body = _runbook()
    assert "--legacy-engine" in body, "the runbook no longer offers the escape hatch"
    for command in ("draft-web", "draft-board"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--legacy-engine" in result.output, (
            f"the runbook offers `{command} --legacy-engine` as rung 0 of the "
            "fallback ladder, but the flag does not exist"
        )


def test_the_runbook_says_the_escape_hatch_is_a_relaunch_not_a_mid_draft_switch():
    """The one way rung 0 can hurt rather than help.

    ``DraftSession.resume`` REFUSES a journal whose picks were made by the other
    engine — correctly, because the alternative is half a draft decided one way
    and half the other. The doc must say so, or the operator learns it at pick 40
    from an error message.
    """
    import ziggurat.draft.session as session_mod

    body = _runbook()
    assert "mid-draft" in body and "--legacy-engine" in body
    # The refusal the doc describes is real, and it names the fix.
    src = (REPO_ROOT / "ziggurat" / "draft" / "session.py").read_text(encoding="utf-8")
    assert "add --legacy-engine" in src and "drop --legacy-engine" in src
    assert session_mod.ENGINE_LEGACY != session_mod.ENGINE_COMPOSED


def test_the_runbook_discloses_the_uncorrected_kicker_board():
    """Rule 6: the launch banner tells the operator his K board is misordered,
    and §9 tells him why it was not fixed. If the banner text changes, the doc
    that quotes it must too — this pins the phrase they share."""
    body = _runbook()
    simulator = (_DRAFT_PKG / "simulator.py").read_text(encoding="utf-8")
    assert "KICKER BOARD: uncorrected" in simulator
    assert "KICKER BOARD: uncorrected" in body, (
        "the runbook quotes the launch banner; that text has moved"
    )
    assert "espn_projections" in body, "§9 must name the source that is missing"


# --------------------------------------- item 3.11 audit fixes, as doc claims


def test_the_runbook_quotes_the_engine_line_the_launcher_actually_prints():
    """§3.1 tells the operator to expect an ENGINE line and to read it on the page.

    Before the audit fix the DEFAULT engine printed nothing at all — only
    ``--legacy-engine`` announced itself — so the operator could not confirm at
    18:45 which engine was about to draft for him. The doc now promises a line;
    this pins the promise to the code that makes it.
    """
    body = _runbook()
    simulator = (_DRAFT_PKG / "simulator.py").read_text(encoding="utf-8")
    assert "ENGINE: default (composed)" in simulator, (
        "the default engine no longer announces itself in the launch notes"
    )
    assert "ENGINE: default (composed)" in body
    # ...and the page renders the notes, not just the terminal.
    webapp = (_DRAFT_PKG / "webapp.py").read_text(encoding="utf-8")
    webui = (_DRAFT_PKG / "webui.html").read_text(encoding="utf-8")
    assert '"engine_profile"' in webapp and '"notes"' in webapp
    assert "engine_profile" in webui and "notes" in webui


def test_the_runbook_does_not_promise_a_roster_shape_the_goldens_contradict():
    """§6 rung 0 told the operator to expect "one more running back, one fewer
    wide receiver" from the default engine. On the frozen board that is FALSE —
    both goldens finish 3 QB / 3 RB / 5 WR / 3 TE / 1 D/ST / 1 K, and the
    difference is WHICH players fill seven of the sixteen slots.

    A rung-0 section that mis-describes what rung 0 undoes is worse than one that
    says nothing: the operator checks the claim, sees it fail, and distrusts the
    ladder at the moment he needs it.
    """
    import json

    body = _runbook()
    fixtures = REPO_ROOT / "tests" / "fixtures" / "draft"
    legacy = json.loads((fixtures / "engine-golden-2026-08-30.json").read_text())
    composed = json.loads((fixtures / "engine-composed-2026-08-30.json").read_text())
    assert legacy["roster_shape"] == composed["roster_shape"], (
        "the shapes have diverged — §6's roster claim must be rewritten to match"
    )
    flat = " ".join(body.split())   # the doc hard-wraps; claims span line breaks
    assert "one more running back, one fewer wide receiver" not in flat
    assert "ROSTER SHAPE is identical either way" in flat
    shape = " / ".join(
        f"{n} {pos}" for pos, n in
        [("QB", 3), ("RB", 3), ("WR", 5), ("TE", 3), ("D/ST", 1), ("K", 1)]
    )
    assert shape in flat, f"§6 must quote the shape the goldens actually produce: {shape}"


def test_the_runbook_tells_the_operator_the_pair_line_is_not_a_promise():
    """§5 must prepare the operator for the thing he will see in rounds 1-3.

    The first-of-pair panel names a partner with a high confidence figure, and
    the tool's own next recommendation lands elsewhere about a third of the time
    with that partner still on the board. The panel says so itself; the runbook
    has to say so too, because rounds 1-3 are exactly where the doc tells him to
    watch the tool in person and calibrate his trust in it.
    """
    body = _runbook()
    wheel = (_DRAFT_PKG / "variant_wheel.py").read_text(encoding="utf-8")
    flat = " ".join(body.split())
    assert "The pairing this score assumes" in wheel
    assert "The pairing this score assumes" in flat
    assert "not a promise about your next pick" in flat.lower()
    # The imperative form must not come back — checked on the CODE, with comment
    # lines stripped (the fix's own rationale quotes the old sentence verbatim).
    code = "\n".join(
        ln for ln in wheel.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "Take him now and" not in code, (
        "the imperative form is what read as a commitment; it must not come back"
    )
