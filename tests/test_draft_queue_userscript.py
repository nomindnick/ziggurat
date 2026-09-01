"""Behavioural tests for the queue-writer userscript's DOM readers.

The userscript is shipped JavaScript with no test harness, and its readings are
CONSUMED AS FACTS by the operator at 18:45 on draft night ("confirm Autopick is
ON"). This runs the real function source out of the shipped file under node,
against a DOM shaped like the live one, so a misreading fails here instead of
on the clock.

Part of the import-quarantined ``ziggurat/draft/`` surface (Rule 8; retained across seasons).
"""

import shutil
import subprocess

import pytest

from ziggurat.paths import REPO_ROOT

USERSCRIPT = REPO_ROOT / "ziggurat" / "draft" / "espn_queue.user.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not available to run the userscript"
)


HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/function autopickState\(\)\s*\{[\s\S]*?\n  \}/);
if (!m) { console.error('EXTRACT_FAILED'); process.exit(2); }

// The LIVE shape, measured 2026-08-27: `.autoPick-container` is a sibling
// control OUTSIDE the queue panel, and the panel holds an unrelated toggle
// that happens to be unchecked.
const autopickInput  = { checked: true,  getAttribute: () => null };
const unrelatedInput = { checked: false, getAttribute: () => null };
const panel = {
  querySelector: () => null,
  querySelectorAll: (sel) => sel.includes('autoPick') ? [] : [unrelatedInput],
};
const queuePanel = () => panel;

let document = {
  querySelector: (sel) =>
    sel === '.autoPick-container input[type="checkbox"]' ? autopickInput : null,
  querySelectorAll: () => [],
};
console.log(eval('(' + m[0] + ')')());

document = { querySelector: () => null, querySelectorAll: () => [] };
console.log(eval('(' + m[0] + ')')());
"""


def _run(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(USERSCRIPT)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout.split()


def test_autopick_reads_the_named_control_not_whatever_checkbox_is_nearest(tmp_path):
    """MEASURED live 2026-08-27: the writer reported ``autopick: off`` while the
    real toggle read ``checked: true``.

    The primary selector was scoped to the queue PANEL, but the control lives
    outside it — so the lookup missed and the fallback read the first checkbox
    it could find in the panel, reporting an unrelated control's state as the
    autopick state. It is a false alarm in the exact direction the runbook
    tells the operator to act on, arriving at the exact moment they check.
    """
    on, _absent = _run(tmp_path)
    assert on == "on", (
        "autopickState() must read the NAMED .autoPick-container control even "
        "when it sits outside the queue panel"
    )


def test_autopick_says_unknown_rather_than_guessing(tmp_path):
    """An unlabelled checkbox is not evidence about autopick. With the named
    control absent the reader must say so — the recurring lesson of this
    codebase is that an absence reported as a measurement is the dangerous
    failure, not the loud one."""
    _on, absent = _run(tmp_path)
    assert absent == "unknown"
