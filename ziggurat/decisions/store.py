"""``decision_freezes`` — the decision-archive run log (item 4.2b, migration 018).

Operational DB metadata: NO as-of columns, never read through ``select_as_of``
(exactly like ``league_sync_runs`` / ``nfl_ingest_runs`` / ``push_runs``). The
one column that looks like an exception, ``plan_as_of``, is the run PARAMETER
the operator passed — the gate the plan ran at, stored so a capture can be found
by the decision it belongs to. Nothing gates on it. ``nfl_ingest_runs``'s
``retrieved_as_of`` is the same shape and carries the same disclosure.

The API is a copy of ``push/runs.py``'s, with its rules inherited verbatim:

  * SILENCE IS NOT SUCCESS: every attempt writes a row, failures included.
  * START-BEFORE-WORK: a ``running`` row lands before the writer touches the
    filesystem, so a killed process leaves a durable, reapable fact.
  * PUBLISH-THEN-RECORD: the row only becomes ``ok`` AFTER ``manifest.json``
    lands. A capture is complete exactly when its manifest exists.
  * ``reap_orphans``: a long-``running`` row is a dead process, not a slow one.

What is NOT inherited: ``push_runs``'s healthy-empty stance. An alert tick with
nothing to say is healthy; a Tuesday with no capture is a LOST TUESDAY, which is
why :func:`format_status` says so in as many words rather than printing an empty
list (item 3.7's lesson: "no push runs recorded yet" once read as healthy).
"""

import textwrap
from datetime import date, datetime, timedelta

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_ABANDONED = "abandoned"

#: A capture landed. ``partial`` IS a capture — the manifest exists and every
#: file in it verifies; what is missing is the candidate half, with its reason
#: recorded (pre-Week-1 the generator raises ``NoCompletedWeek``, and the
#: blocked-roster path never reaches the signal load at all).
CAPTURED_STATUSES = frozenset({STATUS_OK, STATUS_PARTIAL})

#: A ``running`` row older than this is presumed orphaned by a killed process.
#: A capture is ~0 s on top of a ~25 s waiver run, so 3600 s is ~140x the
#: slowest thing it can be waiting on — generous enough that a live run is never
#: falsely reaped.
ORPHAN_AFTER_SECONDS = 3600

TRIGGER_WAIVERS = "waivers"   # the always-on capture inside `ziggurat waivers`
TRIGGER_CLI = "cli"           # `ziggurat decisions freeze`
TRIGGER_TIMER = "timer"       # the Tuesday 18:30 PT unit


def start_capture(conn, *, capture_id, season, week, trigger, plan_as_of, started_at) -> int:
    """Write the ``running`` row BEFORE the writer touches the filesystem.

    This is start-before-work, not the 3.6 reserve-before-effect defect: the row
    suppresses nothing. A later capture is a new row with a new ``capture_id``,
    so a crashed or dry run can never consume a real one.
    """
    cur = conn.execute(
        "INSERT INTO decision_freezes (capture_id, season, week, trigger, plan_as_of, "
        "started_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(capture_id), season, week, str(trigger),
         None if plan_as_of is None else str(plan_as_of), started_at, STATUS_RUNNING),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_capture(
    conn,
    freeze_id,
    *,
    status,
    finished_at,
    week=None,
    artifact_dir=None,
    manifest_sha256=None,
    payload_digest=None,
    files=None,
    claims=None,
    grabs=None,
    streaming=None,
    evaluated=None,
    flagged=None,
    blocked=None,
    chain_gain=None,
    error=None,
) -> None:
    # `week` is COALESCEd, never overwritten with NULL: the row is started BEFORE
    # the plan runs (so a credential failure still leaves a fact — item 4.2b
    # audit, OPS-3), and the week only becomes knowable when the plan resolves it.
    conn.execute(
        "UPDATE decision_freezes SET status=?, finished_at=?, week=COALESCE(?, week), "
        "artifact_dir=?, manifest_sha256=?, payload_digest=?, files=?, claims=?, "
        "grabs=?, streaming=?, evaluated=?, flagged=?, blocked=?, chain_gain=?, "
        "error=? WHERE freeze_id=?",
        (status, finished_at, week, artifact_dir, manifest_sha256, payload_digest,
         files, claims, grabs, streaming, evaluated, flagged, blocked, chain_gain,
         error, freeze_id),
    )
    conn.commit()


def last_capture(conn, *, season=None, status=None):
    clauses, params = [], {}
    if season is not None:
        clauses.append("season = :season")
        params["season"] = int(season)
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM decision_freezes{where} ORDER BY freeze_id DESC LIMIT 1", params
    ).fetchone()


def recent_captures(conn, *, season=None, week=None, limit=15):
    clauses, params = [], {"limit": int(limit)}
    if season is not None:
        clauses.append("season = :season")
        params["season"] = int(season)
    if week is not None:
        clauses.append("week = :week")
        params["week"] = int(week)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM decision_freezes{where} ORDER BY freeze_id DESC LIMIT :limit",
        params,
    ).fetchall()


def capture_status(conn, freeze_id):
    """The status of one run-log row, or ``None``. Used to keep a failure path
    from finishing a row twice (the inner writer may already have done it)."""
    row = conn.execute(
        "SELECT status FROM decision_freezes WHERE freeze_id = ?", (int(freeze_id),)
    ).fetchone()
    return None if row is None else row["status"]


def capture_by_id(conn, capture_id):
    return conn.execute(
        "SELECT * FROM decision_freezes WHERE capture_id = ?", (str(capture_id),)
    ).fetchone()


def reap_orphans(conn, *, now, older_than_seconds=ORPHAN_AFTER_SECONDS) -> int:
    """Mark long-``running`` rows ``abandoned`` — a dead process is a positive
    fact, not silence. Compares in Python (SQLite datetime math is brittle across
    ISO formats), exactly as ``push/runs.py`` does."""
    def _parse(s):
        try:
            return datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None

    now_dt = _parse(now)
    if now_dt is None:
        return 0
    rows = conn.execute(
        "SELECT freeze_id, started_at FROM decision_freezes WHERE status = ?",
        (STATUS_RUNNING,),
    ).fetchall()
    reaped = 0
    for row in rows:
        started = _parse(row["started_at"])
        if started is None:
            continue
        if (now_dt - started).total_seconds() > older_than_seconds:
            conn.execute(
                "UPDATE decision_freezes SET status=?, finished_at=?, error=? "
                "WHERE freeze_id=?",
                (STATUS_ABANDONED, now,
                 "reaped: capture row left 'running' (the process died mid-write; "
                 "the directory it names may hold partial files and has no manifest)",
                 row["freeze_id"]),
            )
            reaped += 1
    if reaped:
        conn.commit()
    return reaped


#: The empty-state text. Deliberately NOT the shape of `alerts status`'s
#: healthy-empty line: an alert tick with nothing new is the common good outcome,
#: whereas a Tuesday with no capture is a Tuesday that can never be
#: reconstructed. Item 3.7 already paid for that confusion once, when
#: "no push runs recorded yet" read as healthy on a box where the push layer had
#: never been installed.
EMPTY_STATUS = (
    "NO DECISION CAPTURES RECORDED YET — this is NOT healthy-empty.\n"
    "  Every `ziggurat waivers` run captures a freeze, so an empty log means either\n"
    "  the archive has never run on this box, or every attempt failed before it could\n"
    "  record a row. A Tuesday that is not captured cannot be reconstructed later:\n"
    "  league_player_state accumulates forward only (item 3.1) and six market sources\n"
    "  serve the current value only (item 3.1b, as amended by item 4.2b: ff_opportunity\n"
    "  and fp_weekly_ecr joined the four).\n"
    "  fix: run `ziggurat waivers` (or `ziggurat decisions freeze`), then re-check;\n"
    "       install the Tuesday timer with scripts/install-decisions.sh."
)


def normalize_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _capture_day(row) -> date | None:
    """The calendar day a capture STARTED, from its own timestamp."""
    stamp = row["started_at"]
    try:
        return datetime.fromisoformat(str(stamp)).date()
    except (TypeError, ValueError):
        return None


def missing_tuesdays(conn, *, season, today) -> tuple[list[date], date | None]:
    """(Tuesdays with no capture, the first captured day) for one season.

    THE WORD IS LITERAL HERE, which is why this report is allowed to borrow
    ``league status``'s register (item 4.2b audit, OPS-2). A Tuesday that was not
    captured cannot be reconstructed: ``league_player_state`` accumulates forward
    only and six market sources serve the current value only, so there is no pull
    that recovers the pool as it stood that night.

    Bounded BELOW by the first capture — this box's archive begins when it begins,
    and reporting every Tuesday back to March as missing would be the
    undifferentiated alarm item 3.1b refused to ship — and above by ``today``.
    """
    rows = conn.execute(
        "SELECT started_at, status FROM decision_freezes WHERE season = ?", (int(season),)
    ).fetchall()
    days = {d for d in (_capture_day(r) for r in rows if r["status"] in CAPTURED_STATUSES)
            if d is not None}
    if not days:
        return [], None
    first, last = min(days), normalize_day(today)
    if last is None or last < first:
        return [], first
    cursor = first + timedelta(days=(1 - first.weekday()) % 7)   # first Tuesday >= first
    missing = []
    while cursor <= last:
        if cursor not in days:
            missing.append(cursor)
        cursor += timedelta(days=7)
    return missing, first


def format_status(conn, *, season=None, limit=10, today=None) -> str:
    """Last captures, newest first, then the VERDICT. Rule 3: the CLI prints this,
    it computes nothing.

    ``today`` is operational wall clock (the ``refresh.format_status`` shape),
    NOT an as-of gate — nothing here is a fact about the NFL. Without it the
    report can list rows and nothing else, which was the whole defect: a log with
    a capture from three Tuesdays ago read exactly like one captured tonight
    (item 4.2b audit, OPS-2).
    """
    rows = recent_captures(conn, season=season, limit=limit)
    if not rows:
        return EMPTY_STATUS
    out = ["last decision captures (newest first):"]
    for r in rows:
        wk = "wk??" if r["week"] is None else f"wk{int(r['week']):02d}"
        line = (f"  [{r['trigger']}] {r['started_at']} {r['season']} {wk} "
                f"-> {r['status']}  {r['capture_id']}")
        if r["claims"] is not None:
            line += (f"\n      claims={r['claims']} grabs={r['grabs']} "
                     f"streaming={r['streaming']}"
                     + ("" if r["chain_gain"] is None
                        else f" joint={r['chain_gain']:+.1f} house pts"))
            if r["blocked"]:
                line += "  BLOCKED (illegal roster: no claims planned)"
        if r["evaluated"] is not None:
            line += f"\n      candidates evaluated={r['evaluated']} flagged={r['flagged']}"
        if r["artifact_dir"]:
            line += f"\n      dir={r['artifact_dir']}"
        if r["error"]:
            # Never a bare mid-word truncation: the string being cut is the one
            # that tells a novice a `partial` capture is EXPECTED rather than
            # broken, and "The plan hal" reads as corruption (item 4.2b audit).
            line += "\n      NOTE=" + textwrap.shorten(
                " ".join(str(r["error"]).split()), width=200,
                placeholder=" … (full text: ziggurat decisions verify --capture "
                            f"{r['capture_id']})")
        out.append(line)
    latest = rows[0]
    if latest["status"] not in CAPTURED_STATUSES:
        out.append(
            f"  ^ the most recent attempt is '{latest['status']}', not a capture — "
            "that Tuesday is not archived."
        )
    out.extend(_verdict(conn, rows, season=season, today=today))
    out.append(
        "  verify a capture's bytes against its manifest: "
        "ziggurat decisions verify --capture <id>"
    )
    return "\n".join(out)


def _verdict(conn, rows, *, season, today) -> list[str]:
    """How OLD the archive is, and which Tuesdays are gone. The half the report
    was missing: it handled the empty case emphatically and then treated every
    non-empty log as healthy."""
    day = normalize_day(today)
    if day is None:
        return ["  AGE: not assessed (no `today` was passed to this report)."]
    out: list[str] = []
    newest = next((d for d in (_capture_day(r) for r in rows
                               if r["status"] in CAPTURED_STATUSES) if d is not None),
                  None)
    if newest is None:
        out.append(
            "  NO CAPTURE IN THE LAST "
            f"{len(rows)} ATTEMPT(S) — every row above is a failure, so nothing in "
            "this window is archived."
        )
    else:
        age = (day - newest).days
        verdict = "captured today" if age == 0 else f"{age} day(s) old"
        out.append(f"  LAST CAPTURE : {newest.isoformat()} ({verdict}).")
        if age > 7:
            out.append(
                "  ^ more than a week with no capture. A Tuesday with no capture is "
                "not a gap that can be filled later."
            )
    target = season if season is not None else rows[0]["season"]
    if target is None:
        return out
    missing, first = missing_tuesdays(conn, season=target, today=day)
    if first is None:
        return out
    if missing:
        out.append(
            f"  MISSING TUESDAYS : {len(missing)} — "
            + ", ".join(d.isoformat() for d in missing[:8])
            + ("" if len(missing) <= 8 else f", … (+{len(missing) - 8} more)")
        )
        out.append(
            "     (the archive on this box starts "
            f"{first.isoformat()}; those Tuesdays are UNRECOVERABLE — the pool as it "
            "stood that night cannot be re-pulled from anywhere.)"
        )
    else:
        out.append(
            f"  MISSING TUESDAYS : none since {first.isoformat()} "
            f"(season {target})."
        )
    return out
