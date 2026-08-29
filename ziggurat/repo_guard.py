"""Public-repo boundary guard.

The repo is public (build-in-public commitment); these paths hold league-private
material and must never be committed (SPEC: Security & Privacy Considerations).

The same pattern list is enforced twice:
  * scripts/hooks/pre-commit   (install: git config core.hooksPath scripts/hooks)
  * tests/test_repo_boundary.py

Paths are repo-relative with forward slashes, as printed by `git diff --name-only`.

Names are not the only signal: ``looks_like_sqlite`` (below) lets the hook catch
a database by its magic header no matter what it is called.
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


#: The first 16 bytes of every SQLite database file, by file format.
SQLITE_MAGIC = b"SQLite format 3\x00"


def looks_like_sqlite(head: bytes) -> bool:
    """Is this the start of a SQLite database file, whatever it is called?

    WHY A CONTENT CHECK EXISTS AT ALL (2026-08-29). Every pattern above keys on
    the NAME, so the guard's reach is exactly as wide as someone's imagination
    about naming — which is the same failure the trailing-suffix note above
    already paid for once. A database that lands in the tree under a name no
    pattern anticipated (a scratch copy, a typo'd path, a script that wrote its
    output to the string ``"None"``) is untracked, invisible to all three
    enforcement points, and one ``git add -A`` from being public. The magic
    header cannot be argued with: it is a database regardless of what it is
    called, and the file whose exposure Rule 5 exists to prevent is 600 MB of
    exactly this format.

    This complements the name patterns; it does not replace them. A `.env` has
    no signature to match, and `intel/` is about location, not content.
    """
    return head.startswith(SQLITE_MAGIC)
