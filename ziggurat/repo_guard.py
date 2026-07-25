"""Public-repo boundary guard.

The repo is public (build-in-public commitment); these paths hold league-private
material and must never be committed (SPEC: Security & Privacy Considerations).

The same pattern list is enforced twice:
  * scripts/hooks/pre-commit   (install: git config core.hooksPath scripts/hooks)
  * tests/test_repo_boundary.py

Paths are repo-relative with forward slashes, as printed by `git diff --name-only`.
"""

import re

#: WHY THE TRAILING-SUFFIX FORM (item 3.2c audit, found by the operator directly).
#: The first version of the two file patterns below was anchored with a bare `$`
#: after a fixed list of endings: `\.sqlite3?(-(wal|shm|journal))?$`. So a
#: database BACKUP — `db/ziggurat.sqlite.bak-v7`, the single most natural name to
#: give the copy you take before a migration — matched NEITHER this list NOR
#: .gitignore's `*.sqlite`, and a 43 MB file holding every opponent's roster and
#: every league member's team name sailed past two of the three enforcement
#: points Rule 5 names. The rule these patterns now encode is "a database file
#: with ANY trailing suffix", not "a database file with one of the four suffixes
#: someone thought of in 2026". The separator class (`[^A-Za-z0-9/]`) is what
#: keeps `foo.sqliteish.py` from matching while `.sqlite.bak-v7`, `.sqlite-wal`,
#: `.sqlite.gz`, `.sqlite~` and `.sqlite.1` all do, and `[^/]*$` keeps a match
#: inside one path segment.
_ANY_TRAILING_SUFFIX = r"($|[^A-Za-z0-9/][^/]*$)"

BOUNDARY_PATTERNS: tuple[str, ...] = (
    # Markdown memory: opponent profiles, decision journal, research notes.
    # Anchored — templates/intel/ (the committed starter skeleton) stays public.
    r"^intel/",
    # Raw pulls, audio, transcripts, caches. Anchored — ziggurat/data/ is the
    # (public) ingestion package.
    r"^data/",
    # SQLite databases, their journal artifacts, and any copy/backup/compressed
    # form of one, anywhere in the tree.
    r"\.sqlite3?" + _ANY_TRAILING_SUFFIX,
    # Credential files (.env, .env.local, .env-prod, .envrc, .env.local.bak, ...),
    # anywhere in the tree. Same end-anchor class as above: the ESPN SWID/ESPN_S2
    # cookies live in one of these, and a name the pattern has not enumerated is
    # not a reason to publish them.
    r"(^|/)\.env(rc)?" + _ANY_TRAILING_SUFFIX,
)


def violations(paths: list[str]) -> list[str]:
    """Return the subset of `paths` that cross the public-repo boundary."""
    return [p for p in paths if any(re.search(pat, p) for pat in BOUNDARY_PATTERNS)]
