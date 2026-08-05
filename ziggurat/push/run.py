"""Push-layer orchestration (item 3.6): the scheduled briefing and alert ticks.

This is the top layer that WIRES the pure-compute cores (core.briefing,
core.alerts) to the LLM router (the two-minute prose), the ntfy egress choke point
(push.outbound), the dedup ledger + run log (push.runs), and the intel/weekly/
files. It imports downward (core / llm / data / league); nothing in core imports
it, so the dependency graph stays acyclic and the cores stay testable offline.

Every network/subprocess touch (claude -p, ntfy POST, the news pull) is preceded
by a `running` run-log row and is individually bounded, and any failure is a loud,
recorded non-`ok` — never a silent swallow (the ingest run-log discipline). The
deterministic briefing/alerts are written to disk REGARDLESS of whether the LLM
prose or the ntfy push succeed, so a token hiccup or a dead topic never costs the
operator the underlying facts (R1's degrade-gracefully requirement).
"""

import json
import os
from pathlib import Path

from ziggurat.core import alerts as alerts_mod
from ziggurat.core import briefing as briefing_mod
from ziggurat.data.nfl import news as news_mod
from ziggurat.paths import INTEL_DIR
from ziggurat.push import outbound, runs

BRIEFINGS_DIR = INTEL_DIR / "weekly" / "briefings"
ALERTS_DIR = INTEL_DIR / "weekly" / "alerts"

PHONE_CHANNEL = "phone"
DEFAULT_ALERT_CAP = 4

BRIEFING_SYSTEM = (
    "You are Ziggurat's briefing summarizer for a fantasy-football novice. You are "
    "given a full, already-correct markdown briefing. Rewrite it as a two-minute "
    "read: lead with the single most urgent action (roster legality, then top "
    "waiver claim), then lineup flags, then signals/alerts. Keep every number and "
    "player name EXACTLY as given — invent nothing, drop nothing load-bearing. Plain "
    "prose and short bullets, no preamble."
)


def _week_tag(week) -> str:
    return f"w{int(week):02d}" if week is not None else "w00"


def _write_briefing_file(season, week, *, prose, full_md, as_of) -> str:
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = BRIEFINGS_DIR / f"{season}-{_week_tag(week)}-briefing.md"
    parts = []
    if prose:
        parts.append("<!-- LLM two-minute summary (morning_briefing task) -->\n" + prose.strip())
        parts.append("\n---\n")
    parts.append(full_md)
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return str(path)


def _append_alert_log(season, week, record) -> str:
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ALERTS_DIR / f"{season}-{_week_tag(week)}.jsonl"
    # Append-only, fsync-before-ack (the journal discipline; pattern learned from
    # the draft session journal — NOT imported, Rule 8).
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return str(path)


def run_briefing(
    conn,
    *,
    as_of,
    season,
    own_team_id,
    now,
    week=None,
    last_week=17,
    claim_budget=3,
    today=None,
    router=None,
    config=None,
    poster=outbound._urllib_poster,
    push=True,
) -> dict:
    """Compose the Wednesday briefing, write it to intel/weekly/, optionally
    summarize it via the router, and push an allowlist-safe teaser to the phone.
    Returns a summary dict; records exactly one push_runs row."""
    run_id = runs.start_run(conn, kind="brief", season=season,
                            scope=f"week {week}" if week is not None else "week ?",
                            started_at=now)
    runs.reap_orphans(conn, now=now)
    llm_backend = ntfy_status = error = artifact = None
    status = runs.STATUS_OK
    try:
        brief = briefing_mod.build_briefing(
            conn, as_of=as_of, season=season, own_team_id=own_team_id,
            week=week, last_week=last_week, claim_budget=claim_budget, today=today or as_of,
        )
        full_md = briefing_mod.format_briefing(brief)

        prose = None
        if router is not None:
            try:
                resp = router.complete("morning_briefing", full_md, system=BRIEFING_SYSTEM)
                prose, llm_backend = resp.text, resp.backend
            except Exception as exc:  # LLM prose is optional (BackendError et al.); degrade
                status = runs.STATUS_PARTIAL
                error = f"llm prose failed: {type(exc).__name__}: {exc}"

        artifact = _write_briefing_file(season, brief.week, prose=prose, full_md=full_md, as_of=as_of)

        teaser = brief.headline_summary
        res = outbound.publish(
            teaser, conn=conn, as_of=as_of, season=season, own_team_id=own_team_id,
            title="Ziggurat briefing", tags="clipboard",
            config=config, dry_run=not push, poster=poster,
        )
        ntfy_status = res.status
        if not res.ok:
            status = runs.STATUS_PARTIAL
    except Exception as exc:  # a failed briefing must be a recorded failure, not a traceback
        status = runs.STATUS_FAILED
        error = f"{type(exc).__name__}: {exc}"
    runs.finish_run(conn, run_id, status=status, finished_at=now, llm_backend=llm_backend,
                    llm_task="morning_briefing", ntfy_status=ntfy_status,
                    artifact_path=artifact, error=error)
    return {"run_id": run_id, "status": status, "artifact": artifact,
            "ntfy": ntfy_status, "error": error}


def _alert_priority_tags(event) -> tuple[str, str]:
    if event.kind == "INJURY_OUT":
        return ("high", "rotating_light")
    return ("default", "newspaper")


def run_alert_tick(
    conn,
    *,
    as_of,
    season,
    own_team_id,
    now,
    week=None,
    last_week=17,
    cap=DEFAULT_ALERT_CAP,
    today=None,
    pull_news=True,
    news_limit=news_mod.DEFAULT_LIMIT,
    news_fetch=news_mod.fetch_espn_news,
    config=None,
    poster=outbound._urllib_poster,
    push=True,
) -> dict:
    """One alert tick: pull the news wire, compute alert-worthy events, dedup them
    against the ledger, and PUBLISH-THEN-RECORD the top `cap` new events to the
    phone (overflow collapses to one summary), appending the full set to the on-box
    log. Records exactly one push_runs row; STATUS_EMPTY when nothing is NEW.

    LEDGER DISCIPLINE (audit D4/D8): the ledger is written ONLY after a real send is
    confirmed — never on a dry run (`push=False` is side-effect-free, so a --no-push
    preview cannot poison the real cadence's dedup), never on an infra failure (so a
    transient ntfy outage retries next tick). A CONTENT block (the Rule-5 scrub or
    the size cap raising) is recorded as a reserved-not-pushed row so a genuinely
    unpublishable event does not retry forever."""
    run_id = runs.start_run(conn, kind="alert", season=season, scope="events", started_at=now)
    runs.reap_orphans(conn, now=now)
    status = runs.STATUS_OK
    error = None
    pushed = 0
    ntfy_status = None
    new_count = 0
    try:
        if pull_news:
            try:
                news_mod.pull_news(conn, retrieved_as_of=as_of, limit=news_limit, fetch=news_fetch)
            except Exception as exc:  # news is best-effort; the injury arm still works
                error = f"news pull failed: {type(exc).__name__}: {exc}"
                status = runs.STATUS_PARTIAL

        board = alerts_mod.build_alerts(
            conn, as_of=as_of, season=season, own_team_id=own_team_id,
            week=week, last_week=last_week, today=today or as_of,
        )

        # phone lane = phone_worthy ONLY (the push must name an action — operator
        # decision 2026-08-05; context events still land in the briefing + log
        # below), then dedup vs ledger (phone channel), then rank + cap. `new`
        # (NOT the raw candidate count) drives everything: injury_transitions
        # re-emits every historical crossing each tick, so a steady deduped tick
        # is honestly EMPTY.
        new = [e for e in board.events
               if e.phone_worthy
               and not runs.already_seen(conn, season=season, dedup_key=e.dedup_key, channel=PHONE_CHANNEL)]
        new.sort(key=lambda e: (-e.severity, e.event_day or "", e.player or ""))
        new_count = len(new)
        to_push, overflow = new[:cap], new[cap:]

        def _deliver(event, body, *, title, priority=None, tags=None):
            """Publish one item and, ONLY on a confirmed real send, record it in the
            ledger. Returns True if it counts as pushed."""
            nonlocal ntfy_status, status
            try:
                res = outbound.publish(
                    body, conn=conn, as_of=as_of, season=season, own_team_id=own_team_id,
                    title=title, priority=priority, tags=tags,
                    config=config, dry_run=not push, poster=poster,
                )
            except outbound.OutboundBoundaryError as exc:
                # A content defect (scrub/size): record it so it does not retry
                # forever, but as reserved-not-pushed, and never leak the reason.
                if event is not None and push:
                    runs.reserve(
                        conn, season=season, dedup_key=event.dedup_key, channel=PHONE_CHANNEL,
                        kind=event.kind, espn_player_id=event.espn_id, event_day=event.event_day,
                        first_seen_at=now, payload_summary=f"BLOCKED: {type(exc).__name__}",
                    )
                status = runs.STATUS_PARTIAL
                return False
            ntfy_status = res.status
            if res.ok and push:
                if event is not None:
                    runs.reserve(
                        conn, season=season, dedup_key=event.dedup_key, channel=PHONE_CHANNEL,
                        kind=event.kind, espn_player_id=event.espn_id, event_day=event.event_day,
                        first_seen_at=now, payload_summary=event.headline,
                    )
                    runs.mark_pushed(conn, season=season, dedup_key=event.dedup_key,
                                     channel=PHONE_CHANNEL, pushed_at=now)
                return True
            if res.ok and not push:
                return True  # dry run: delivered-as-preview, ledger untouched
            status = runs.STATUS_PARTIAL  # infra failure -> retry next tick, no reserve
            return False

        for event in to_push:
            priority, tags = _alert_priority_tags(event)
            if _deliver(event, event.headline, title="Ziggurat alert", priority=priority, tags=tags):
                pushed += 1

        # overflow: NOT reserved (they drain into individual pushes on later ticks
        # once the burst clears) — one summary keeps the busy-day phone from spamming.
        if overflow:
            summary = f"+{len(overflow)} more new event(s) this tick — see the briefing on the box"
            if _deliver(None, summary, title="Ziggurat alerts", tags="rotating_light"):
                pushed += 1

        # on-box record (append-only), even when nothing pushed.
        _append_alert_log(season, board.week, {
            "tick": now, "as_of": str(as_of), "candidates": len(board.events),
            "new": new_count, "pushed_new": len(to_push), "overflow": len(overflow),
            "events": [{"kind": e.kind, "player": e.player, "headline": e.headline,
                        "dedup_key": e.dedup_key, "severity": e.severity,
                        "phone_worthy": e.phone_worthy} for e in board.events],
            "notes": list(board.notes),
        })

        if status == runs.STATUS_OK and new_count == 0:
            status = runs.STATUS_EMPTY  # nothing NEW to push is the healthy common case
    except Exception as exc:
        status = runs.STATUS_FAILED
        error = f"{type(exc).__name__}: {exc}"
    runs.finish_run(conn, run_id, status=status, finished_at=now, events_found=new_count,
                    events_pushed=pushed, ntfy_status=ntfy_status, error=error)
    return {"run_id": run_id, "status": status, "found": new_count, "pushed": pushed, "error": error}
