# Ziggurat

AI-assisted decision system for a season-long fantasy football league — and a
serious testbed for an agentic-harness pattern: **Claude Code as the runtime**,
a Python package of deterministic CLI tools, SQLite for facts, and markdown for
judgment. There is no server or custom app; the repo is the system.

- **[SPEC.md](./SPEC.md)** — features, architecture, and league ground truth
- **[IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)** — phased plan and build log
- **[CLAUDE.md](./CLAUDE.md)** — standing rules, workflows, and current status

## Status

**Phase 1 (Ground Truth & Data Spine) is in progress.** Foundations, ESPN access,
historical-market feasibility, house-rules scoring, and core nflverse ingestion
are complete. Next is Phase 1.5: projections, ADP, odds, and weather ingestion.
The system is a tested data foundation, not yet a draft or in-season decision tool.

## Spine rules

1. **Explicit temporal reads.** Every accessor requires `as_of`. The default
   `historical` view reconstructs data retrieved by that date; explicit
   `latest_truth` reads use later corrections only for outcome grading or
   deliberately accepted immutable bulk history.
2. **One scoring module.** All league scoring rules live in
   `ziggurat/core/scoring.py`.
3. **One LLM entry point.** Programmatic model calls route through
   `ziggurat.llm.Router` by task tag and stakes tier.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
git config core.hooksPath scripts/hooks   # public-repo boundary guard
.venv/bin/ziggurat intel init             # recreate private intel/ skeleton
.venv/bin/ziggurat db init                # bootstrap/apply SQLite migrations
.venv/bin/pytest
.venv/bin/ruff check .
```

The declared install uses `nflreadpy`, nflverse's maintained Python client. No
manual `--no-deps` installation is required.

## Data discipline

NFL fact tables retain both `knowable_as_of` and `retrieved_as_of`. Historical
features use both timestamps; final corrected truth must be requested explicitly.
Database changes land as ordered files under `db/migrations/` and are applied by
`ziggurat db init` without recreating the local database.

## Public boundary

The code and documentation are public. `intel/`, top-level `data/`, SQLite files,
and credentials are private, gitignored, blocked by the versioned pre-commit hook,
and checked by the test suite and CI.
