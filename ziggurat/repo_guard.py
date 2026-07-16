"""Public-repo boundary guard.

The repo is public (build-in-public commitment); these paths hold league-private
material and must never be committed (SPEC: Security & Privacy Considerations).

The same pattern list is enforced twice:
  * scripts/hooks/pre-commit   (install: git config core.hooksPath scripts/hooks)
  * tests/test_repo_boundary.py

Paths are repo-relative with forward slashes, as printed by `git diff --name-only`.
"""

import re

BOUNDARY_PATTERNS: tuple[str, ...] = (
    # Markdown memory: opponent profiles, decision journal, research notes.
    # Anchored — templates/intel/ (the committed starter skeleton) stays public.
    r"^intel/",
    # Raw pulls, audio, transcripts, caches. Anchored — ziggurat/data/ is the
    # (public) ingestion package.
    r"^data/",
    # SQLite databases and their journal artifacts, anywhere in the tree.
    r"\.sqlite3?(-(wal|shm|journal))?$",
    # Credential files (.env, .env.local, ...), anywhere in the tree.
    r"(^|/)\.env(\..*)?$",
)


def violations(paths: list[str]) -> list[str]:
    """Return the subset of `paths` that cross the public-repo boundary."""
    return [p for p in paths if any(re.search(pat, p) for pat in BOUNDARY_PATTERNS)]
