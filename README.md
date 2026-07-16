# Ziggurat

AI-assisted decision system for a season-long fantasy football league — and a
serious testbed for an agentic-harness pattern: **Claude Code as the runtime**,
a Python package of deterministic CLI tools, SQLite for facts, markdown for
judgment. There is no server and no app; the repo is the system.

The operator is a football novice competing in a 10-team office league against
years of fan intuition. The bet: systematic data ingestion, league-specific
valuation under custom house scoring, validated leading-indicator signals, and
disciplined process beat casual expertise.

- **[SPEC.md](./SPEC.md)** — full specification: features, architecture, design decisions
- **[IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)** — phased plan with per-item Update blocks (the build log)
- **[CLAUDE.md](./CLAUDE.md)** — the constitution: standing rules, workflows, current status

## Status

**Phase 0 (Foundations) complete** — scaffold, the three spine abstractions
(as-of data discipline, single scoring module, LLM routing interface), and the
constitution. Next: Phase 1 spikes (ESPN API access, historical market archives).

## The three spine rules

Everything else hangs off three retrofit-hostile conventions, load-bearing
since the first commit:

1. **`as_of` on every data read** — accessors return only what was knowable at
   that moment, so backtests replay history through production code paths
   without leakage.
2. **One scoring module** — the league's custom rules (D/ST yards-allowed
   brackets, kicker distance scoring with miss penalties, full PPR) exist in
   exactly one place; re-pricing under house rules is the core edge.
3. **One LLM entry point** — model calls are routed by task tag + stakes tier
   through config, so backends (Claude headless / API / local Ollama) swap
   without code changes.

## Public by design, private by design

The code, structure, and documentation are public (build-in-public). The
intelligence is not: `intel/` (opponent notes, decision journal, research
findings), `data/` (raw pulls), the SQLite file, and credentials are gitignored
and additionally guarded by a pre-commit hook plus boundary tests. Publishing
reveals the method — the private intel layer and execution are the moat.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
git config core.hooksPath scripts/hooks   # boundary guard, required per clone
.venv/bin/ziggurat intel init             # recreate the private intel/ skeleton
.venv/bin/pytest
```
