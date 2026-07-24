"""League-state sync orchestration (item 3.1).

One run = one consistent snapshot: pull every ESPN view, map it, write every
table, reconcile the two independent roster views, and record the run.

Design stance, which follows directly from the recon (design §1): league history
is PERISHABLE — ESPN serves no historical league state, so a day this sync does
not capture is gone permanently. Two consequences are built in here:

* **The snapshot is protected from the optional parts.** The transaction/activity
  feed may not exist at all (it did not for 2025, and does not today); if mapping
  it explodes, the run still commits the snapshot and finishes ``partial`` with
  the error recorded, rather than losing the day.
* **Silence is not success.** Every run — including a failed one — writes a row
  to ``league_sync_runs``, so `ziggurat league status` can report a dead cron
  and the exact days that are missing.
"""

import logging
from datetime import datetime, timezone

from ziggurat.data.asof import normalize_as_of
from ziggurat.league import source, state

logger = logging.getLogger("ziggurat.league.sync")

# Recorded in league_sync_runs.error when a run was deliberately back-stamped, so
# format_status can flag that day as fabricated rather than counting it as
# genuine point-in-time coverage.
_BACKFILL_MARKER = "BACKFILLED:"


def _utc_now() -> str:
    """Wall-clock stamp for the run LOG only.

    Not a knowledge time and never used as one (rule 1 governs read accessors;
    ``retrieved_as_of`` is always passed in explicitly by the caller).
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_sync(
    conn,
    *,
    season: int,
    league_id: int,
    espn_s2,
    swid,
    retrieved_as_of,
    today=None,
    include_transactions: bool = True,
    allow_shrink: bool = False,
    allow_backfill: bool = False,
) -> dict:
    """Pull and persist one full league-state snapshot. Returns the run summary.

    Raises after recording the run when the SNAPSHOT itself fails (so a cron exits
    nonzero and the operator finds out); an optional-part failure downgrades the
    run to ``partial`` instead.

    ``retrieved_as_of`` is validated (not merely truncated) — an unparseable stamp
    used to write a whole day that no accessor could ever see, because the as-of
    gate compares date strings lexically and ``'2026-9-8' <= '2026-09-15'`` is
    False.

    BACK-STAMPING IS REFUSED by default. ESPN only ever serves CURRENT state, so
    writing it under a past ``retrieved_as_of`` does not recover that day — it
    fabricates it, stamping today's rosters and ownership percentages as though
    they were knowable then, and simultaneously erases the day from the gap report
    that exists to say history is missing. ``today`` is passed in explicitly (no
    implicit "now" in the package); ``allow_backfill`` overrides, and a forced run
    is MARKED so ``format_status`` can keep telling the truth about it.
    """
    stamp = normalize_as_of(retrieved_as_of).isoformat()
    if today is not None:
        today = normalize_as_of(today).isoformat()
        if stamp != today and not allow_backfill:
            raise ValueError(
                f"refusing to stamp a snapshot {stamp} on a run made {today}: ESPN serves "
                "only CURRENT league state, so a back-stamped run fabricates history "
                "rather than recovering it (and hides the gap). Pass --allow-backfill "
                "if you really mean to write today's state under that date."
            )
    backfilled = today is not None and stamp != today
    run_id = state.start_run(conn, season=season, retrieved_as_of=stamp, started_at=_utc_now())
    counts: dict[str, int] = {}
    warnings: list[str] = []

    try:
        payload = source.fetch_league_state(
            league_id=league_id, season=season, espn_s2=espn_s2, swid=swid
        )
        scoring_period = payload.get("scoringPeriodId")
        roster = state.roster_index(payload)

        pool = source.fetch_player_pool(
            league_id=league_id, season=season, espn_s2=espn_s2, swid=swid,
            scoring_period=scoring_period or 0,
        )

        counts.update(state.ingest_league_state(conn, payload, retrieved_as_of=stamp, season=season))
        player_counts = state.ingest_player_state(
            conn, pool, retrieved_as_of=stamp, season=season,
            roster=roster, scoring_period=scoring_period, allow_shrink=allow_shrink,
        )
        counts["players"] = player_counts["players"]
        counts["conflicts"] = player_counts["conflicts"]
        counts["rostered"] = len(roster)
        counts["scoring_period"] = scoring_period
        conn.commit()

        # The snapshot is safe. A collapsed crosswalk does NOT discard it (gsis_id
        # is derived and backfillable; the snapshot is not) — it downgrades the run.
        coverage = player_counts.get("gsis_coverage")
        if coverage is not None and coverage < state._MIN_GSIS_COVERAGE:
            warnings.append(
                f"espn->gsis crosswalk coverage {coverage:.0%} below "
                f"{state._MIN_GSIS_COVERAGE:.0%} — snapshot written, gsis_id needs backfill"
            )
        if backfilled:
            warnings.append(f"{_BACKFILL_MARKER} snapshot stamped {stamp} on a run made {today}")
    except Exception as exc:  # snapshot failed — the day is lost; make it loud
        state.finish_run(conn, run_id, status="failed", finished_at=_utc_now(),
                         counts=counts, error=f"{type(exc).__name__}: {exc}")
        raise

    # --- optional, best-effort: the event log. Never risks the snapshot. ---
    counts["transactions"] = 0
    if include_transactions:
        try:
            rows = []
            for raw in source.fetch_transactions(
                league_id=league_id, season=season, espn_s2=espn_s2, swid=swid
            ):
                rows.extend(state.map_transaction(raw, season=season))
            for topic in source.fetch_activity(
                league_id=league_id, season=season, espn_s2=espn_s2, swid=swid
            ):
                rows.extend(state.map_activity_topic(topic, season=season))
            counts["transactions"] = state.ingest_transactions(
                conn, rows, retrieved_as_of=stamp, season=season
            )
            conn.commit()
        except Exception as exc:
            warnings.append(f"transaction feed: {type(exc).__name__}: {exc}")
            logger.warning("league sync: transaction feed failed (%s) — snapshot kept", exc)

    status = "partial" if warnings else "ok"
    state.finish_run(conn, run_id, status=status, finished_at=_utc_now(),
                     counts=counts, error="; ".join(warnings) or None)
    counts["status"] = status
    counts["run_id"] = run_id
    counts["retrieved_as_of"] = stamp
    return counts


def format_run(summary: dict) -> str:
    """One-line human summary of a run (the cron's log line)."""
    return (
        f"[{summary.get('status')}] {summary.get('retrieved_as_of')} "
        f"sp={summary.get('scoring_period')} "
        f"teams={summary.get('teams')} players={summary.get('players')} "
        f"rostered={summary.get('rostered')} matchups={summary.get('matchups')} "
        f"transactions={summary.get('transactions')} conflicts={summary.get('conflicts')}"
    )


def format_status(conn, *, season: int, through) -> str:
    """The operational health report: last run, snapshot coverage, and the days
    that are permanently missing.

    ``through`` is passed in explicitly rather than defaulting to today — the
    caller states the horizon it is judging coverage against.
    """
    last_ok = state.last_run(conn, season=season, status="ok")
    last_any = state.last_run(conn, season=season, status=None)
    days = state.snapshot_days(conn, season=season)
    gaps = state.snapshot_gaps(conn, season=season, through=through)

    lines = [f"league sync status — season {season} "
             f"(through {normalize_as_of(through).isoformat()})"]
    if last_any is None:
        lines.append("  NEVER RUN — no league history is being captured.")
        return "\n".join(lines)

    lines.append(
        f"  last run     : {last_any['started_at']} [{last_any['status']}]"
        + (f" — {last_any['error']}" if last_any["error"] else "")
    )
    lines.append(
        f"  last success : {last_ok['started_at']} (snapshot {last_ok['retrieved_as_of']}, "
        f"{last_ok['players']} players, {last_ok['teams']} teams)"
        if last_ok is not None else "  last success : NONE"
    )
    lines.append(f"  snapshots    : {len(days)} days, {days[0] if days else '—'} → {days[-1] if days else '—'}")
    if gaps:
        shown = ", ".join(gaps[:10]) + (f", … (+{len(gaps) - 10} more)" if len(gaps) > 10 else "")
        lines.append(f"  MISSING DAYS : {len(gaps)} — {shown}")
        lines.append("  (ESPN serves no historical league state; missing days are unrecoverable.)")
    else:
        lines.append("  MISSING DAYS : none")

    faked = state.backfilled_days(conn, season=season, marker=_BACKFILL_MARKER)
    if faked:
        lines.append(f"  BACK-STAMPED : {len(faked)} — {', '.join(faked)}")
        lines.append("  (these days hold live state written under a past date; NOT point-in-time.)")
    return "\n".join(lines)
