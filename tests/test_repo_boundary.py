"""Public-repo boundary enforcement (item 0.1).

Three layers, all tested here:
  1. ziggurat.repo_guard.violations — the shared pattern matcher
  2. scripts/hooks/pre-commit — the hook wrapping it
  3. .gitignore — must cover the same paths (and NOT over-match the public
     ziggurat/data/ package or templates/intel/)
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ziggurat.repo_guard import violations

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "hooks" / "pre-commit"

PRIVATE_PATHS = [
    "intel/opponents/some-team.md",
    "intel/heuristics.md",
    "data/raw/espn_pull.json",
    "db/ziggurat.sqlite",
    "db/ziggurat.sqlite-wal",
    "cache/anything.sqlite3",
    ".env",
    ".env.local",
    "config/.env",
]

PUBLIC_PATHS = [
    "ziggurat/data/__init__.py",  # the ingestion *package* is public
    "templates/intel/README.md",  # committed starter skeleton is public
    "db/schema.sql",  # schema is public; only the .sqlite is private
    "SPEC.md",
    "ziggurat/core/scoring.py",
]


def test_matcher_flags_private_paths():
    assert violations(PRIVATE_PATHS) == PRIVATE_PATHS


def test_matcher_allows_public_paths():
    assert violations(PUBLIC_PATHS) == []


def test_hook_script_blocks_and_allows():
    blocked = subprocess.run(
        [sys.executable, str(HOOK), "--paths", "intel/notes.md"], capture_output=True, text=True
    )
    assert blocked.returncode == 1
    assert "COMMIT BLOCKED" in blocked.stderr
    allowed = subprocess.run(
        [sys.executable, str(HOOK), "--paths", "ziggurat/core/scoring.py"],
        capture_output=True,
        text=True,
    )
    assert allowed.returncode == 0


# ── git-dependent checks (skip on tarball downloads) ─────────────────────────

needs_git = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(),
    reason="not a git checkout",
)


def _check_ignore(path: str) -> bool:
    return (
        subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=REPO, capture_output=True
        ).returncode
        == 0
    )


@needs_git
def test_gitignore_covers_boundary_paths():
    for p in PRIVATE_PATHS:
        assert _check_ignore(p), f"boundary path not gitignored: {p}"


@needs_git
def test_gitignore_does_not_overmatch_public_paths():
    # Guards the anchoring: a bare `data/` pattern would swallow ziggurat/data/.
    for p in PUBLIC_PATHS:
        assert not _check_ignore(p), f"public path wrongly gitignored: {p}"


@needs_git
def test_nothing_tracked_crosses_the_boundary():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    tracked = [p for p in out.split("\0") if p]
    assert violations(tracked) == []
