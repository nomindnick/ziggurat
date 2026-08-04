"""push_runs (the briefing/alert run log) + alert_ledger (the dedup ledger).

Operational DB metadata — NO as-of columns, never read through select_as_of
(exactly like league_sync_runs / nfl_ingest_runs). This is the ``push`` layer's
I/O sibling to those cadences' run logs, with the same discipline:

  * SILENCE IS NOT SUCCESS: every tick writes a row, empty ones included.
  * START-BEFORE-NETWORK: a `running` row is written BEFORE the claude -p call or
    the ntfy POST, so a crash mid-run leaves a durable, reapable row.
  * STATUS_EMPTY is HEALTHY for the alert kind (the common every-20-min outcome).
  * RESERVE-THEN-PUSH dedup: reserve the ledger row (pushed_at NULL) and confirm
    the insert won BEFORE the ntfy POST; a crash between reserve and push drops
    that one alert rather than risking a duplicate (a missed push is recoverable
    via the next briefing; a spam push is not, and erodes the channel's trust).
"""

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_ABANDONED = "abandoned"

#: Not-a-problem outcomes (the alert tick with nothing new is the common case and
#: must not look like a failure or trip Restart=on-failure).
HEALTHY_STATUSES = frozenset({STATUS_OK, STATUS_EMPTY, STATUS_SKIPPED})

#: A `running` row older than this (by started_at vs now) is presumed orphaned by
#: a killed process and reaped to `abandoned`. Generous vs the units'
#: TimeoutStartSec so a slow-but-alive run is never falsely reaped.
ORPHAN_AFTER_SECONDS = 3600


# ------------------------------------------------------------------ push_runs


def start_run(conn, *, kind, season, scope, started_at) -> int:
    cur = conn.execute(
        "INSERT INTO push_runs (kind, season, scope, started_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, season, scope, started_at, STATUS_RUNNING),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn,
    run_id,
    *,
    status,
    finished_at,
    events_found=None,
    events_pushed=None,
    llm_backend=None,
    llm_task=None,
    ntfy_status=None,
    artifact_path=None,
    error=None,
) -> None:
    conn.execute(
        "UPDATE push_runs SET status=?, finished_at=?, events_found=?, events_pushed=?, "
        "llm_backend=?, llm_task=?, ntfy_status=?, artifact_path=?, error=? WHERE run_id=?",
        (status, finished_at, events_found, events_pushed, llm_backend, llm_task,
         ntfy_status, artifact_path, error, run_id),
    )
    conn.commit()


def last_run(conn, *, kind, season=None, status=None):
    clauses = ["kind = :kind"]
    params = {"kind": kind}
    if season is not None:
        clauses.append("season = :season")
        params["season"] = season
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    return conn.execute(
        f"SELECT * FROM push_runs WHERE {' AND '.join(clauses)} ORDER BY run_id DESC LIMIT 1",
        params,
    ).fetchone()


def reap_orphans(conn, *, now, older_than_seconds=ORPHAN_AFTER_SECONDS) -> int:
    """Mark long-`running` rows (a process that died mid-run) as `abandoned`, so a
    crash is a positive fact, not silence. Compares started_at to `now` in Python
    (SQLite datetime math is brittle across ISO formats)."""
    from datetime import datetime

    def _parse(s):
        try:
            return datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None

    now_dt = _parse(now)
    if now_dt is None:
        return 0
    rows = conn.execute(
        "SELECT run_id, started_at FROM push_runs WHERE status = ?", (STATUS_RUNNING,)
    ).fetchall()
    reaped = 0
    for row in rows:
        started = _parse(row["started_at"])
        if started is None:
            continue
        if (now_dt - started).total_seconds() > older_than_seconds:
            conn.execute(
                "UPDATE push_runs SET status=?, finished_at=?, error=? WHERE run_id=?",
                (STATUS_ABANDONED, now, "reaped: run row left 'running' (process died)", row["run_id"]),
            )
            reaped += 1
    if reaped:
        conn.commit()
    return reaped


def format_status(conn, *, kind=None) -> str:
    clause = "" if kind is None else " WHERE kind = :kind"
    params = {} if kind is None else {"kind": kind}
    rows = conn.execute(
        f"SELECT kind, season, status, started_at, finished_at, events_found, events_pushed, "
        f"ntfy_status, artifact_path, error FROM push_runs{clause} "
        f"ORDER BY run_id DESC LIMIT 15",
        params,
    ).fetchall()
    if not rows:
        return "no push runs recorded yet."
    out = ["last push runs (newest first):"]
    for r in rows:
        line = f"  [{r['kind']}] {r['started_at']} -> {r['status']}"
        if r["events_found"] is not None:
            line += f"  found={r['events_found']} pushed={r['events_pushed']}"
        if r["ntfy_status"]:
            line += f"  ntfy={r['ntfy_status']}"
        if r["artifact_path"]:
            line += f"  file={r['artifact_path']}"
        if r["error"]:
            line += f"  ERROR={r['error'][:120]}"
        out.append(line)
    return "\n".join(out)


# ------------------------------------------------------------------ alert_ledger


def _require_season(season) -> None:
    """The dedup ledger's UNIQUE(season, dedup_key, channel) relies on a non-NULL
    season: SQLite treats NULLs as DISTINCT in a UNIQUE index, so a NULL season
    would silently disable dedup (INSERT OR IGNORE never ignores) and re-push the
    same alert every tick. season is always resolved upstream (`_season()` never
    returns None); this makes the invariant explicit rather than trusting it
    (audit D4/D7). The ledger's one job is to never double-push."""
    if season is None:
        raise ValueError("alert_ledger requires a non-NULL season (dedup depends on it)")


def already_seen(conn, *, season, dedup_key, channel) -> bool:
    _require_season(season)
    return conn.execute(
        "SELECT 1 FROM alert_ledger WHERE season = ? AND dedup_key = ? AND channel = ?",
        (season, dedup_key, channel),
    ).fetchone() is not None


def reserve(conn, *, season, dedup_key, channel, kind, espn_player_id, event_day,
            first_seen_at, payload_summary) -> bool:
    _require_season(season)
    """Reserve this event on this channel (pushed_at NULL). Returns True iff THIS
    call inserted the row (won the race); False if it was already reserved. The
    caller pushes only on True, then calls mark_pushed after a durable send."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO alert_ledger (season, dedup_key, channel, kind, "
        "espn_player_id, event_day, first_seen_at, payload_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (season, dedup_key, channel, kind, espn_player_id, event_day, first_seen_at, payload_summary),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_pushed(conn, *, season, dedup_key, channel, pushed_at) -> None:
    conn.execute(
        "UPDATE alert_ledger SET pushed_at = ? WHERE season IS ? AND dedup_key = ? AND channel = ?",
        (pushed_at, season, dedup_key, channel),
    )
    conn.commit()
