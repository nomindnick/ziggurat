"""Item 3.7 — the weekly operating cadence in CLAUDE.md is executable doc.

The done-when is "a fresh session can run the Tuesday workflow from CLAUDE.md
alone", which makes the cadence section a load-bearing interface, not prose: a
renamed command or dropped flag rots it silently. These tests re-derive every
`ziggurat ...` invocation the section quotes and check it against the real CLI.
"""

import re
from pathlib import Path

from typer.testing import CliRunner

from ziggurat.cli.main import app
from ziggurat.paths import REPO_ROOT

runner = CliRunner()

_CLAUDE_MD = Path(__file__).parent.parent / "CLAUDE.md"

# `ziggurat <cmd> [<sub>] [--flag ...]*` — sub only matches a bare word (never
# a --flag), flags only while directly chained to this invocation.
_INVOCATION = re.compile(
    r"ziggurat ([a-z][a-z-]*)((?: [a-z][a-z-]*)?)((?:\s+--[a-z-]+)*)"
)


def _cadence_section() -> str:
    text = _CLAUDE_MD.read_text()
    start = text.index("## Weekly operating cadence")
    tail = text[start + 1 :]
    end = tail.index("\n## ")  # next top-level section
    return text[start : start + 1 + end]


def _quoted_invocations():
    """(args, flags) per `ziggurat ...` mention in the cadence section."""
    seen = set()
    for cmd, sub, flags in _INVOCATION.findall(_cadence_section()):
        args = [cmd] + ([sub.strip()] if sub.strip() else [])
        key = (tuple(args), tuple(flags.split()))
        if key not in seen:
            seen.add(key)
            yield args, flags.split()


def test_the_placeholder_is_gone():
    section = _cadence_section()
    assert "PLACEHOLDER" not in section
    # the named workflows a fresh session will be asked for by day
    for day in ("Tuesday", "Wednesday", "Sunday", "Monday"):
        assert day in section


def test_the_cadence_quotes_a_nonempty_command_set():
    """Guard the guard: if the regex ever matches nothing, every downstream
    assertion would vacuously pass."""
    invocations = list(_quoted_invocations())
    assert len(invocations) >= 8
    quoted = {tuple(args) for args, _ in invocations}
    # the load-bearing daily commands must stay quoted somewhere in the section
    for expected in [("waivers",), ("lineup",), ("candidates",), ("stream",),
                     ("league", "sync"), ("league", "status"),
                     ("ingest", "status"), ("alerts", "status")]:
        assert expected in quoted, f"cadence no longer quotes `ziggurat {' '.join(expected)}`"


def test_every_quoted_invocation_resolves_against_the_real_cli():
    for args, flags in _quoted_invocations():
        result = runner.invoke(app, args + ["--help"])
        assert result.exit_code == 0, (
            f"CLAUDE.md cadence quotes `ziggurat {' '.join(args)}` "
            f"but the CLI refuses it:\n{result.output}"
        )
        for flag in flags:
            assert flag in result.output, (
                f"CLAUDE.md cadence quotes `ziggurat {' '.join(args)} {flag}` "
                f"but {flag} is not in that command's --help"
            )


def test_the_week_journal_template_ships_and_scaffolds():
    """The Tuesday workflow journals into a copy of week-TEMPLATE.md; the
    template must exist in the committed tree and carry the retro discipline."""
    template = REPO_ROOT / "templates" / "intel" / "weekly" / "week-TEMPLATE.md"
    assert template.is_file()
    body = template.read_text()
    assert "Decision log" in body and "Monday retro" in body
    # the one line the whole retro hinges on
    assert "PROCESS, not outcome" in body or "process, not outcome" in body.lower()
