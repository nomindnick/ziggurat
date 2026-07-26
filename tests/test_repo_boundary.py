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

#: Private paths whose suffix arrives AFTER the recognised extension. Held as
#: their own list because they are the shape every enforcement point missed, not
#: because any point misses them now: the matcher and hook were widened by the
#: 3.2c audit fix, `.gitignore` by the operator immediately after, so all THREE
#: agree and these are asserted in `PRIVATE_PATHS` too (below).
SUFFIXED_PRIVATE_PATHS = [
    "db/ziggurat.sqlite.bak-v7",       # the operator's actual pre-migration backup name
    "db/ziggurat.sqlite.gz",
    "db/ziggurat.sqlite.1",
    "db/ziggurat.sqlite3.old",
    ".env-prod",
    ".envrc",
]

PRIVATE_PATHS += SUFFIXED_PRIVATE_PATHS

PUBLIC_PATHS = [
    "ziggurat/data/__init__.py",  # the ingestion *package* is public
    "templates/intel/README.md",  # committed starter skeleton is public
    "db/schema.sql",  # schema is public; only the .sqlite is private
    "SPEC.md",
    "ziggurat/core/scoring.py",
    "docs/sqlite.md",  # writing ABOUT sqlite is public
    "ziggurat/sqlite_helpers.py",
    "foo.sqliteish.py",  # the separator class is what keeps this out
    "db/migrations/008_backfill_recovery.sql",
]


def test_matcher_flags_private_paths():
    assert violations(PRIVATE_PATHS) == PRIVATE_PATHS


def test_matcher_allows_public_paths():
    assert violations(PUBLIC_PATHS) == []


def test_a_database_backup_is_blocked(tmp_path):
    """THE OPERATOR'S OWN FINDING (item 3.2c audit). The sqlite pattern used to be
    `\\.sqlite3?(-(wal|shm|journal))?$` — anchored after four enumerated endings —
    so `db/ziggurat.sqlite.bak-v7`, the most natural name for the copy you take
    before a migration, matched NOTHING: not this matcher, not the hook that reads
    it, and not `.gitignore`'s `*.sqlite`. That file is 43 MB of league-private
    state (every opponent's roster, every league member's team name) in a PUBLIC
    repo, and Rule 5 names three enforcement points, two of which it walked past.

    Asserted through the HOOK as well as the matcher, because the hook is the
    gate that actually runs at commit time.

    CLOSED 2026-07-25: `.gitignore` was widened too, so all three points agree.
    But NOT with the `*.sqlite*` this docstring originally proposed — that
    over-matches in the other direction and silently ignores a source file named
    `foo.sqliteish.py` (it is in `PUBLIC_PATHS`, and it caught exactly that
    mistake being made). The pattern is a SEPARATOR CLASS: the suffix must begin
    with `.` or `-`. A boundary pattern can fail in both directions and only one
    of them is loud.
    """
    for path in SUFFIXED_PRIVATE_PATHS:
        assert violations([path]) == [path], f"boundary path not matched: {path}"
    blocked = subprocess.run(
        [sys.executable, str(HOOK), "--paths", *SUFFIXED_PRIVATE_PATHS],
        capture_output=True, text=True,
    )
    assert blocked.returncode == 1
    assert "COMMIT BLOCKED" in blocked.stderr
    for path in SUFFIXED_PRIVATE_PATHS:
        assert path in blocked.stderr


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
