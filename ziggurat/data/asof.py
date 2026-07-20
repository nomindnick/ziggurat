"""The as-of convention — Ziggurat's leakage discipline.

Every read accessor in this codebase follows three rules (SPEC key decision 4;
retrofitting this is prohibitively painful, so it is load-bearing from day one):

1. **Keyword-only `as_of`, no default.** Signature shape:
       def get_projections(conn, *, as_of, ...) -> ...
   Callers must state the knowledge instant explicitly; there is no "now".

2. **Knowledge and retrieval time are distinct.** NFL fact tables carry
   `knowable_as_of` (when the source fact was public) and `retrieved_as_of` (when
   this system obtained that version). Historical reads gate both. Explicit
   `latest_truth` reads gate knowledge time only so final corrected outcomes can
   grade decisions without pretending the correction was available at the time.
   Semantics are inclusive end-of-day until Phase 3 adds intraday timestamps.

3. **Every accessor ships with a leakage test.** Insert facts on both sides of a
   knowledge-time boundary, query as-of the boundary, assert the later facts are
   invisible. The exemplar to copy is tests/test_asof_pattern.py.

This makes backtests leakage-resistant on the *live* code path — the backtest
harness replays history through the same accessors production uses.
"""

from datetime import date, datetime


def normalize_as_of(as_of: date | datetime | str) -> date:
    """Validate and normalize an `as_of` argument to a date.

    Accepts a date, a datetime (truncated to its date), or an ISO-8601 string.
    Anything else — including None — raises: accessors have no implicit "now".
    """
    # datetime first: datetime is a subclass of date.
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    if isinstance(as_of, str):
        return date.fromisoformat(as_of)
    raise TypeError(
        f"every read accessor requires as_of (date, datetime, or ISO string); got {as_of!r}"
    )
