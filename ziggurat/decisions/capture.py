"""The freeze writer (item 4.2b, B2): one Tuesday, written down on the day.

WHAT A CAPTURE IS. One directory under the gitignored
``data/decisions/<season>/wk<NN>/<capture_id>/`` holding the whole run that
produced a decision — the plan the operator read (every reason string verbatim,
both gain columns, the chain's own bookkeeping), the priced swap matrix, EVERY
evaluated candidate row (flagged or not, so a false negative is studyable), the
free-agent pool as priced, the roster and its legality verdict, and the market
provenance — plus a ``manifest.json`` naming every file's sha256. The manifest
is written LAST: a capture is complete exactly when its manifest exists.

WHAT IT IS NOT. It has no ``knowable_as_of`` and is never read back as a
decision input. A freeze is not a fact about the NFL; it is "what the tool SAID
at ``as_of`` T". It records the GATE it ran at (``as_of``), the wall clock it
ran at (``captured_at``, both Pacific and UTC), and — per source — the
``retrieved_as_of`` the view actually resolved, because a decision archive that
records only ``as_of`` records the gate and not the data (the item-3.1b lesson:
a July projection pull and a November one both carry a perfectly valid
``knowable_as_of``, and nothing in the output tells them apart).

APPEND-ONLY, ONE CAPTURE PER RUN. Two captures on one Tuesday are two facts —
the operator runs ``waivers`` more than once — so nothing here is
idempotent-overwrite and an existing directory is REFUSED, never reused.

PUBLISH-THEN-RECORD. The ``decision_freezes`` row becomes ``ok`` only after the
manifest lands. The ``running`` row that precedes the work is the run-log
discipline (a crashed capture must leave a legible fact, not silence) and is not
the item-3.6 defect: that was a DEDUP LEDGER reserved before its side effect,
which permanently suppressed the real push. Nothing here suppresses anything.

NOTHING ON THE ARCHIVE SIDE MAY TAKE THE TUESDAY PAGE DOWN.
:func:`capture_best_effort` never raises; a fault becomes a ``failed`` run-log
row and one printed line, and the operator still gets the plan.

The manifest / atomic-write / digest discipline is COPIED from
``backtest/decisions.py`` (``ziggurat/`` must not import ``backtest/`` — the
dependency runs the other way) and the run-log discipline from
``ziggurat/push/runs.py``, exactly as ``push/run.py`` already copies the draft
cockpit's fsync-before-ack journal rather than importing it (Rule 8).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import secrets
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ziggurat.decisions import store
from ziggurat.paths import REPO_ROOT

#: Where captures live. Under ``data/``, which is gitignored AND matched by
#: ``repo_guard.BOUNDARY_PATTERNS`` — a freeze holds the whole FA pool, rival
#: rosters and this league's private state, so it must never be reachable from a
#: committed path (Rule 5). Never ``templates/``, never ``tests/``.
DECISIONS_DIR = REPO_ROOT / "data" / "decisions"

#: The clock a capture id and the human-readable stamp are minted in. The
#: operator, the waiver batch (00:01-01:13 PT, item 3.8a) and the cadence all run
#: on Pacific; UTC is recorded beside it so nothing has to guess a zone later.
CAPTURE_TZ = ZoneInfo("America/Los_Angeles")

MANIFEST = "manifest.json"

PLAN_FILE = "plan.jsonl"
SWAPS_FILE = "swaps.jsonl"
CANDIDATES_FILE = "candidates.jsonl"
BOARD_FILE = "board.jsonl"
POOL_FILE = "pool.jsonl"
ROSTER_FILE = "roster.json"
MARKET_FILE = "market.json"

#: The order files are digested in for :func:`payload_digest` — sorted by name,
#: so a capture that gains a file later still digests deterministically.
PAYLOAD_FILES = (BOARD_FILE, CANDIDATES_FILE, MARKET_FILE, PLAN_FILE, POOL_FILE,
                 ROSTER_FILE, SWAPS_FILE)

#: The claim the manifest records, and the mechanism behind it. Stated as a
#: STRUCTURAL fact about the code path (pinned by
#: tests/test_decisions_capture.py::test_the_usage_evidence_column_is_order_inert),
#: never as something this run re-measured — see ``enriched_chain_equals_projection_only``.
CHAIN_INERT_MECHANISM = (
    "STRUCTURAL, not re-measured at capture time: the opportunity-signal notes "
    "(core/waiver._candidate_notes_by_espn) reach exactly one place — the "
    "`candidate_notes=` argument of `_claim_reasons`, called from `_swap_rec` on a "
    "row that has ALREADY been selected and ordered. They are read by no sort key, "
    "no gain, no `board.value_after` call and no step of the CELF chain in "
    "`_select_claims`. Deleting them would change reason TEXT and nothing else, so "
    "the chain the operator saw IS the projection-only chain. If the column is ever "
    "promoted to a tie-break, this field becomes False and the two chains become two "
    "different objects — which is what makes the promotion visible in the archive."
)

#: The pool disclosure this writer owes. ``build_board`` does not hand back its
#: scan's rejects, and ``_reprice_swaps`` drops non-positive rows before
#: ``board.swaps`` resolves, so a free agent who was scanned and priced at or
#: below zero leaves no trace. The capture reports what it can SEE and says the
#: rest is unknown; it must never re-scan to reconstruct it (two scans is two
#: answers).
POOL_UNKNOWN_NOTE = (
    "pool.jsonl: `scanned` is TRUE exactly when this free agent has an entry in the "
    "scan's own model (i.e. he was priceable AND survived the per-position pool_limit "
    "prune). A FALSE `scanned` is genuinely ambiguous between 'no usable projection at "
    "this as_of' and 'pruned by pool_limit', and this writer does NOT re-scan to tell "
    "them apart — a second scan is a second answer. `priced_in_matrix` is TRUE when he "
    "appears as an ADD in swaps.jsonl; note the matrix drops non-positive rows before "
    "it resolves, so FALSE there means 'not worth a positive gain against any legal "
    "drop', not 'never looked at'."
)


class CaptureCollision(FileExistsError):
    """A capture directory already exists. Captures are append-only and are never
    overwritten: two captures on one day are two facts."""


@dataclass(frozen=True)
class CaptureResult:
    """What one capture attempt did. ``status`` is a ``decisions.store`` status."""

    status: str
    capture_id: str
    directory: str | None
    season: int | None
    week: int | None
    manifest_sha256: str | None = None
    payload_digest: str | None = None
    files: int = 0
    error: str | None = None

    @property
    def captured(self) -> bool:
        return self.status in store.CAPTURED_STATUSES

    @property
    def line(self) -> str:
        """The ONE line a command prints under the page it just rendered. Rule 3:
        the CLI prints this string; it does not build it."""
        wk = "wk??" if self.week is None else f"wk{self.week:02d}"
        if self.status == store.STATUS_OK:
            return f"decision freeze [ok] {self.capture_id} ({wk}) -> {self.directory}"
        if self.status == store.STATUS_PARTIAL:
            return (f"decision freeze [partial] {self.capture_id} ({wk}) -> "
                    f"{self.directory}\n  incomplete: {self.error}")
        return (f"decision freeze [FAILED] {self.capture_id}: {self.error}\n"
                f"  the plan above is unaffected, but this Tuesday is NOT archived "
                f"— a Tuesday that is not captured cannot be reconstructed later.")


# --------------------------------------------------------------- serialisation


def _jsonable(value):
    """Recursively convert to JSON-safe types, REFUSING anything unrecognised.

    Refuse-rather-than-guess: a type this writer does not know would otherwise be
    silently stringified into the archive, and an archive that quietly reshapes a
    field is worse than one that fails loudly on the day someone can still fix it.
    ``dataclasses.asdict`` is deliberately not used — it deep-copies, and
    ``WaiverPlan.position_caps`` is a ``mappingproxy``, which cannot be copied.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                f"refusing to write a non-finite float ({value!r}) into a decision "
                "freeze: JSON has no representation for it and a reader would get "
                "either a crash or a silently wrong number"
            )
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    raise TypeError(
        f"decision freeze: no JSON rendering for {type(value).__name__} — add one "
        "deliberately rather than letting the archive stringify it"
    )


def _dumps(obj) -> str:
    """One canonical JSON rendering: sorted keys, no whitespace, no NaN.

    Sorted keys are what makes a capture byte-deterministic for fixed inputs (the
    determinism digest), and ``allow_nan=False`` is what keeps invalid JSON out of
    an archive nobody will re-read for months.
    """
    return json.dumps(_jsonable(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _jsonl(records: Sequence) -> bytes:
    return "".join(_dumps(r) + "\n" for r in records).encode("utf-8")


_TMP_COUNTER = itertools.count()


def _write_atomic(path: Path, data: bytes) -> None:
    """tmp + fsync + ``os.replace``. The temp name carries the writing process and
    a per-process counter so two writers aiming at one path cannot interleave into
    a shared temp fd (the item-4.2 audit's finding on ``backtest/decisions.py``)."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{next(_TMP_COUNTER)}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def payload_digest(files: Mapping[str, Mapping]) -> str:
    """The determinism digest: sha256 over ``name\\0sha256\\n`` for every payload
    file in name order. Two runs over identical inputs produce the same value; the
    manifest's own varying fields (capture id, wall clock, argv) are deliberately
    outside it, so determinism is testable without freezing the clock."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(files[name]["sha256"].encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


# ------------------------------------------------------------------- provenance


def git_identity(root: Path = REPO_ROOT, *, timeout: float = 15.0) -> dict:
    """``commit`` + ``sha256(git diff HEAD)`` + ``dirty``.

    ``git rev-parse HEAD`` alone is NOT a code identity in this repo: the systemd
    timers run the WORKING TREE, so uncommitted code is the production cadence
    (the standing rule item 3.5 paid for). A capture that recorded only the commit
    would name a version of the tool that never ran. Failures degrade to a
    recorded reason — a missing git is not a reason to lose the Tuesday.
    """
    def _run(args):
        return subprocess.run(  # noqa: S603 — fixed argv, no shell, bounded
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    try:
        head = _run(["rev-parse", "HEAD"])
        if head.returncode != 0:
            return {"commit": None, "diff_sha256": None, "dirty": None,
                    "error": f"git rev-parse HEAD failed: {head.stderr.strip()[:200]}"}
        diff = _run(["diff", "HEAD"])
        if diff.returncode != 0:
            return {"commit": head.stdout.strip(), "diff_sha256": None, "dirty": None,
                    "error": f"git diff HEAD failed: {diff.stderr.strip()[:200]}"}
        blob = diff.stdout.encode("utf-8")
        return {
            "commit": head.stdout.strip(),
            "diff_sha256": _sha256(blob),
            "diff_bytes": len(blob),
            "dirty": bool(blob),
            "error": None,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"commit": None, "diff_sha256": None, "dirty": None,
                "error": f"{type(exc).__name__}: {exc}"}


def schema_version(conn) -> int | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    return None if row is None else int(row[0])


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def new_capture_id(now: datetime) -> str:
    """``<YYYYMMDD>T<HHMMSS>-<6 hex>``, minted in Pacific.

    The random tail is what makes two captures in the same SECOND two directories
    rather than a collision, and the timestamp head is what makes a directory
    listing sort into the order the operator ran things.

    FOUR bytes, not three (item 4.2b audit, T4). At three the birthday
    probability over 200 same-second draws is 0.12% — measured at 0.13% over
    20,000 repetitions — which is a real collision rate wearing the word
    "collision-free", and it made the suite's own 200-draw pin flaky at about 1
    run in 800. One extra character takes it to 0.00046%.
    """
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"


def capture_dir(root, *, season, week, capture_id) -> Path:
    """``<root>/<season>/wk<NN>/<capture_id>``.

    THE WEEK IN THE PATH IS THE WEEK THE BOARD PRICED, and on every in-season
    Tuesday that is one MORE than the week the candidate rows describe: the plan
    prices the week ahead, while ``build_candidates`` resolves the last week
    fully played and knowable (Tue 2026-09-15 → a ``wk02`` directory holding
    week-1 candidate rows). NEVER join the candidate half to the directory name
    or to ``decision_freezes.week``; the manifest records ``candidate_week``
    beside ``week`` for exactly that join (item 4.2b audit, DC-7).
    """
    wk = "wk00" if week is None else f"wk{int(week):02d}"
    return Path(root) / str(season if season is not None else "unknown") / wk / str(capture_id)


# ------------------------------------------------------------ payload assembly


def _plan_records(plan) -> list[dict]:
    """``plan.jsonl``: a header line plus one line per printed/held row.

    JSONL rather than one blob because the rows are what a later reader joins on
    (a claim to a transaction, a refusal to the row that replaced it), and every
    line is self-describing via ``record``.
    """
    header = {
        "record": "plan",
        "as_of": plan.as_of,
        "season": plan.season,
        "team_id": plan.team_id,
        "weeks": list(plan.weeks),
        "blocked": plan.blocked,
        "waiver_priority": plan.waiver_priority,
        "team_count": plan.team_count,
        "transaction_locked": plan.transaction_locked,
        "chain_gain": plan.chain_gain,
        "chain_measured_not_shown": plan.chain_measured_not_shown,
        "chain_not_repriced": plan.chain_not_repriced,
        "chain_stop": plan.chain_stop,
        "position_caps": dict(plan.position_caps),
        "league_limits": None if plan.league_limits is None else dict(plan.league_limits),
        "freshness": list(plan.freshness),
        "notes": list(plan.notes),
        "ir_move_fix": list(plan.ir_move_fix),
        "forced_drop": _jsonable(plan.forced_drop),
    }
    out = [header]
    for kind, rows in (("claim", plan.claims), ("grab", plan.fcfs_grabs),
                       ("streaming", plan.streaming)):
        for row in rows:
            rec = _jsonable(row)
            rec["record"] = kind
            out.append(rec)
    for row in plan.drop_board:
        rec = _jsonable(row)
        rec["record"] = "drop_board"
        out.append(rec)
    for kind, rows in (("chain_rejected", plan.chain_rejected),
                       ("chain_under_ranked", plan.chain_under_ranked),
                       ("chain_capped", plan.chain_capped)):
        for row in rows:
            rec = _jsonable(row)
            rec["record"] = kind
            out.append(rec)
    return out


def _swap_records(collect) -> list[dict]:
    """``swaps.jsonl``: the priced matrix AS SCANNED, in the order it resolved.

    The model keys ride along when the board can still hand them over: they are
    what makes a row re-priceable against a roster other than today's, and they
    die inside ``_SwapMatrix`` otherwise. Never touched on the blocked path — the
    matrix is lazy and costs more than the rest of the scan, and a refusal prices
    no claims (see ``WaiverArtifacts``).
    """
    swaps = list(collect.swaps)
    if not swaps:
        return []
    keys: list = [None] * len(swaps)
    board = collect.board
    if board is not None:
        try:
            resolved = board.swap_keys
            if len(resolved) == len(swaps):
                keys = list(resolved)
        except Exception:  # noqa: BLE001 — provenance is optional; the rows are not
            keys = [None] * len(swaps)
    out = []
    for idx, (row, key) in enumerate(zip(swaps, keys, strict=True)):
        rec = _jsonable(row)
        rec["record"] = "swap"
        rec["matrix_index"] = idx
        rec["drop_key"] = None if key is None else key[0]
        rec["add_key"] = None if key is None else key[1]
        out.append(rec)
    return out


def _candidate_records(collect) -> list[dict]:
    """``candidates.jsonl``: EVERY evaluated RB/WR/TE player-week, flagged or not.

    Sorted into a canonical order (position, gsis, espn, player) so two captures
    of the same board are byte-identical; the arm's own emission order carries no
    meaning. ``as_record()`` is the module's own flat rendering — this writer adds
    no field and re-derives no floor (a second flag rule is two rules that can
    silently diverge).
    """
    evaluated = collect.evaluated
    if evaluated is None or not evaluated.rows:
        return []
    rows = sorted(
        (r.as_record() for r in evaluated.rows),
        key=lambda r: (str(r.get("position") or ""), str(r.get("gsis_id") or ""),
                       str(r.get("espn_id") or ""), str(r.get("player") or "")),
    )
    return rows


def _board_records(collect) -> list[dict]:
    """``board.jsonl``: the FLAGGED board itself — every arm, not just usage.

    ``candidates.jsonl`` holds ``_usage_arm``'s evaluated rows, so without this
    file the INJURY_SHOCK and QB1_CHANGE arms leave no trace in a freeze at all:
    their reason text and their ``player_key`` (the episode key — item 4.2b §2.3
    keys the injury arm on it, and it re-fires while a designation persists)
    exist nowhere else once the process exits, and every Tuesday not captured is
    unrecoverable.

    It is also what makes the archive re-readable by its own rule:
    ``candidates.week_flags(board, evaluated_rows)`` needs a board, and the
    episode history (B12) is built from this file joined to
    ``candidates.jsonl``. A header line carries the board's own ``notes`` and
    ``freshness`` — the degrade sentences that explain a badge or a stale read —
    so a later reader is never left with rows whose caveats were dropped.

    Sorted canonically (kind, player_key, player) so two captures of one board
    are byte-identical; the arms' emission order is a ranking WITHIN a kind and
    is preserved as ``rank_in_kind``.
    """
    board = collect.candidates
    if board is None:
        return []
    out: list[dict] = [{
        "record": "board",
        "season": board.season,
        "week": board.week,
        "as_of": board.as_of,
        "rows": len(board.rows),
        "notes": list(board.notes),
        "freshness": list(board.freshness),
    }]
    seen: dict[str, int] = {}
    rows = []
    for row in board.rows:
        kind = str(row.signal_kind)
        seen[kind] = seen.get(kind, 0) + 1
        rows.append({
            "record": "flagged",
            "player_key": row.player_key,
            "signal_kind": kind,
            "rank_in_kind": seen[kind],
            "gsis_id": row.gsis_id,
            "espn_id": row.espn_id,
            "player": row.player,
            "position": row.position,
            "team": row.team,
            "magnitude": row.magnitude,
            "week": row.week,
            "prior_week": row.prior_week,
            "hypothesis": row.hypothesis,
            "episode_tag": row.episode_tag,
            "reasons": list(row.reasons),
        })
    rows.sort(key=lambda r: (str(r["signal_kind"]), str(r["player_key"]),
                             str(r["player"] or "")))
    out.extend(rows)
    return out


def _candidates_meta(collect) -> dict:
    """The candidate half's own provenance — or WHY it is absent.

    Four distinguishable outcomes, and the archive must never flatten them:
      * the board was built (``present``);
      * the roster was ILLEGAL, so the plan refused before the signal load ever
        ran (``absent: blocked``) — the collector was never even constructed;
      * no REG week is fully played and knowable at this ``as_of``
        (``absent: no_completed_week``) — expected until the season's first full
        week, and the reason the first live Tuesday is also this code's first
        end-to-end test;
      * the signal load FAILED (``absent: degraded``) — a real degrade, with the
        plan's own note quoted.
    """
    evaluated, board, err = collect.evaluated, collect.candidates, collect.candidate_error
    meta: dict = {
        "present": board is not None,
        "ran": bool(evaluated is not None and evaluated.ran),
        "rows": None if evaluated is None else len(evaluated.rows),
        "flagged": None if evaluated is None else len(evaluated.flagged),
    }
    if evaluated is not None:
        meta.update({
            "season": evaluated.season,
            "week": evaluated.week,
            "as_of": evaluated.as_of,
            "view": evaluated.view,
            "positions": list(evaluated.positions),
            # Rule 6: the floors a row was JUDGED against travel with the rows, so
            # an archived board can never be read against the wrong hypothesis —
            # and a tuned run records its own label, not the shipped one.
            "floors": None if evaluated.floors is None else dict(evaluated.floors),
            "floors_label": evaluated.floors_label,
            "floors_source": evaluated.floors_source,
            "emergence_floors": (None if evaluated.emergence_floors is None
                                 else dict(evaluated.emergence_floors)),
            "emergence_label": evaluated.emergence_label,
        })
    if board is not None:
        meta["board_rows"] = len(board.rows)
        # The BOARD's own week is the candidate week too, and it is the one that
        # survives when the evaluated collector did not run (an older generator, a
        # board built outside the usage arm). `week` here is the week the
        # CANDIDATE half describes — never the week the plan priced (DC-7).
        if meta.get("week") is None:
            meta["week"] = int(board.week)
        meta["absent_reason"] = None
        return meta
    if evaluated is None:
        meta["absent_reason"] = (
            "ABSENT (blocked): the roster was illegal at this as_of, so the plan "
            "refused before the opportunity-signal load — no candidate board is "
            "built on that path, and no row was evaluated."
        )
    elif err:
        meta["absent_reason"] = f"ABSENT (degraded): {err}"
    else:
        meta["absent_reason"] = (
            "ABSENT (no completed week): the generator raised NoCompletedWeek — no "
            "REG week is fully played and knowable at this as_of, which is the "
            "expected state before the season's first full week. The plan half of "
            "this capture is complete; the candidate half does not exist to capture."
        )
    return meta


def _pool_records(collect) -> list[dict]:
    """``pool.jsonl``: the free-agent pool AS PRICED.

    Every row the plan considered a free agent, plus what the scan actually did
    with him. See :data:`POOL_UNKNOWN_NOTE` for the two things this cannot know
    without re-scanning, and does not pretend to.
    """
    pool = list(collect.pool_rows)
    if not pool:
        return []
    entries: dict[str, object] = {}
    board = collect.board
    if board is not None:
        for entry in board.model.entries.values():
            if getattr(entry, "on_roster", False):
                continue
            if entry.espn_id is not None:
                entries[str(entry.espn_id)] = entry
    added: dict[str, list[float]] = {}
    for swap in collect.swaps:
        if swap.add_espn_id is not None:
            added.setdefault(str(swap.add_espn_id), []).append(swap.gain)

    out = []
    for row in pool:
        eid = row.get("espn_player_id")
        key = None if eid is None else str(eid)
        entry = entries.get(key or "")
        gains = added.get(key or "", [])
        rec = {
            "record": "pool",
            "espn_id": key,
            "gsis_id": row.get("gsis_id"),
            "player": row.get("player"),
            "position": row.get("position"),
            "pro_team": row.get("pro_team"),
            "roster_status": row.get("roster_status"),
            "percent_owned": row.get("percent_owned"),
            "percent_change": row.get("percent_change"),
            "injury_status": row.get("injury_status"),
            "injured": row.get("injured"),
            "droppable": row.get("droppable"),
            "scanned": entry is not None,
            "priced_in_matrix": bool(gains),
            "best_matrix_gain": max(gains) if gains else None,
            "matrix_rows": len(gains),
        }
        if entry is not None:
            rec.update({
                "bye": entry.bye,
                "weeks_projected": entry.weeks_projected,
                "weeks_projectable": entry.weeks_projectable,
                "no_projection_at_all": entry.no_projection_at_all,
                "projection_vintage": entry.pulled_as_of,
                "stale_projection": entry.stale_projection,
            })
        out.append(rec)
    out.sort(key=lambda r: (str(r.get("position") or ""), str(r.get("espn_id") or ""),
                            str(r.get("player") or "")))
    return out


def _roster_payload(collect) -> dict:
    """``roster.json``: the roster as read (RAW, IR rows included), the legality
    verdict that gated the whole page, and the IR ground-truth report."""
    plan = collect.plan
    return {
        "as_of": collect.as_of,
        "season": collect.season,
        "team_id": collect.team_id,
        "view": collect.view,
        "open_slots": collect.open_slots,
        "roster": [_jsonable(dict(r)) for r in collect.roster_rows],
        "legality": _jsonable(None if plan is None else plan.legality),
        "ir_rule": _jsonable(None if plan is None else plan.ir_rule),
        "league_settings": _jsonable(collect.league_settings),
        "waiver_priority": None if plan is None else plan.waiver_priority,
        "team_count": None if plan is None else plan.team_count,
    }


def _projection_input(collect) -> dict:
    """The projection-input hash the item's goal asks for, beside the chain.

    sha256 over the per-player, per-week points the scan actually priced with
    (``ScenarioModel``'s own entries, rounded to 6 dp), in canonical key order.
    That is the input that decides every gain on the page: two captures with the
    same hash priced the same numbers, whatever their ``as_of`` says. Rule 2 is
    untouched — nothing here computes a scoring value, it hashes ones already
    computed by ``core/scoring.py`` through the valuation spine.
    """
    board = collect.board
    if board is None:
        return {"sha256": None, "entries": 0, "weeks": [],
                "note": "no board was built at this as_of (blocked roster, or the "
                        "week window did not resolve)"}
    entries = board.model.entries
    payload = {
        key: {str(w): round(float(p), 6) for w, p in sorted(entry.points.items())}
        for key, entry in sorted(entries.items())
    }
    return {
        "sha256": _sha256(_dumps(payload).encode("utf-8")),
        "entries": len(entries),
        "weeks": list(board.weeks),
        "note": "sha256 over {model key: {week: house points}} as the scan priced it, "
                "6 dp, canonical JSON",
    }


def _probe(label, fn):
    """Run one PROVENANCE probe, degrading a fault to a recorded error.

    market.json is metadata ABOUT the run, not the run: a probe that breaks
    (a renamed column in a market table a later item ships, a reader reaching
    into board internals that moved) must not cost the Tuesday it was describing.
    The payload files proper get no such wrapper — if the plan or the pool cannot
    be written, the capture really did fail and must say so.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — a provenance probe never fails a capture
        return {"status": "error",
                "reason": f"{label} probe failed: {type(exc).__name__}: {exc}"}


def _market_payload(conn, collect) -> dict:
    """``market.json``: which pull priced this page, and what same-week market
    existed at the time.

    ``fp_weekly_ecr`` (item 4.2b B5) is recorded as ABSENT-with-a-reason when the
    table does not exist yet, rather than omitted: an absent field reads as "not
    captured", and "the table did not exist on the day" is a different fact.
    """
    season, as_of = collect.season, collect.as_of
    market: dict = {
        "as_of": as_of,
        "view": collect.view,
        "source": collect.source,
        "vintages": dict(collect.vintages),
        "crosswalk_vintage": collect.crosswalk_vintage,
        "projection_input": _probe("projection_input",
                                   lambda: _projection_input(collect)),
    }
    market["fpecr_panel_wp"] = _probe(
        "fpecr_panel", lambda: _fpecr_probe(conn, season, as_of, collect.view))
    market["fp_weekly_ecr"] = _probe(
        "fp_weekly_ecr", lambda: _fp_weekly_probe(conn, as_of, collect.view))
    return market


#: Why both probes below go through the module's own as-of accessor instead of
#: their own SQL (item 4.2b audit, R1). ``market.json`` prints ``"view"`` beside
#: these numbers, so they must be the numbers THAT view can serve. A hand-written
#: ``knowable_as_of <= :as_of`` is neither half of that:
#:
#:   * it has NO RETRIEVAL GATE, so under the safe-default ``historical`` view it
#:     reports rows the run itself provably could not read. Measured on the live
#:     database: 3,516 ``fpecr_panel`` wp rows for season 2023 at as_of
#:     2023-11-01 against ZERO the view can serve (that panel is bulk history —
#:     every row was retrieved in 2026);
#:   * it does NOT RESOLVE PER KEY, so once a perishable table holds more than
#:     one vintage of the same board the count multiplies by the number of
#:     captures (measured: a 5-row board pulled on three days counted as 15).
#:
#: The accessors do both, and doing it here a second time would be a second rule
#: that can silently diverge from the one the tool reads with. The
#: ``select_as_of`` ban in tests/test_decisions_capture.py is about this package
#: never gating a DECISION INPUT ITSELF; these are exactly the fact reads it
#: exists to protect, and they reach the gate the only safe way — through the
#: source module that owns it.
MARKET_PROBE_GATE = (
    "counted through the source's own as-of accessor at this capture's `as_of` "
    "and `view` — so the number is what the run itself could have READ, per-key "
    "resolved (one row per identity, never one per vintage)."
)


def _fpecr_probe(conn, season, as_of, view) -> dict:
    from ziggurat.data.nfl import fpecr

    if not _table_exists(conn, "fpecr_panel"):
        return {"status": "absent", "reason": "no fpecr_panel table at this schema_version"}
    rows = fpecr.get_fpecr(conn, as_of=as_of, season=season, ecr_type="wp", view=view)
    scrapes = sorted({r["scrape_date"] for r in rows if r["scrape_date"]})
    return {
        "view": view,
        "rows": len(rows),
        "first_scrape": scrapes[0] if scrapes else None,
        "last_scrape": scrapes[-1] if scrapes else None,
        "scrape_days": len(scrapes),
        "gate": MARKET_PROBE_GATE,
        "note": "the weekly-positional ECR series the 4.1/4.2 harness grades on. "
                "0 rows for the LIVE season is expected until the panel's next "
                "scheduled pull; 0 rows for a PAST season under `historical` is "
                "expected too and is not an absence of market — that panel is bulk "
                "history, and the backtest reads it through base.latest_truth.",
    }


def _fp_weekly_probe(conn, as_of, view) -> dict:
    """The same-week weekly-ECR board itself, not only its cardinality.

    §2.1 decides market.json holds "the day's fp_weekly_ecr rows for the six
    league pages". THE DAY'S: for each page this emits the rows of the NEWEST
    scrape this view can serve, and reports the whole visible history only as a
    count beside it. Anything else grows without bound (every page keeps every
    scrape day in its key), and the older boards are still re-readable from
    SQLite — ``retrieved_as_of`` is in this table's primary key, so a vintage is
    never overwritten by a later one.
    """
    from ziggurat.data.nfl import fp_weekly

    if not _table_exists(conn, "fp_weekly_ecr"):
        return {
            "status": "absent",
            "reason": "no fp_weekly_ecr table at this schema_version — the same-week "
                      "ECR capture (item 4.2b, B5) had not landed when this freeze "
                      "was written. The same-week market this capture DOES hold is "
                      "the Sleeper projections vintage above.",
        }
    pages, board = [], []
    for page in sorted(fp_weekly.LEAGUE_PAGES):
        served = fp_weekly.get_fp_weekly_ecr(conn, as_of=as_of, page=page, view=view)
        scrapes = sorted({r["scrape_date"] for r in served if r["scrape_date"]})
        last = scrapes[-1] if scrapes else None
        day = [r for r in served if r["scrape_date"] == last] if last else []
        pages.append({
            "page": page,
            "rows": len(day),               # the day's board — what `board` holds
            "rows_all_scrapes": len(served),
            "first_scrape": scrapes[0] if scrapes else None,
            "last_scrape": last,
            "scrape_days": len(scrapes),
        })
        for r in day:
            board.append({
                "page": page,
                "fantasypros_id": r["fantasypros_id"],
                "gsis_id": r["gsis_id"],
                "espn_id": r["espn_id"],
                "player": r["player"],
                "team": r["team"],
                "rank": r["rank"],
                "ecr": r["ecr"],
                "sd": r["sd"],
                "best": r["best"],
                "worst": r["worst"],
                "player_ecr_delta": r["player_ecr_delta"],
                "nfl_week": r["nfl_week"],
                "week_basis": r["week_basis"],
                "scrape_date": r["scrape_date"],
            })
    # Canonical order so two captures of one board are byte-identical; the
    # accessor's own row order carries no meaning.
    def _order(row):
        rank = row["rank"]
        return (str(row["page"]), rank is None, float(rank) if rank is not None else 0.0,
                str(row["fantasypros_id"] or ""), str(row["player"] or ""))

    board.sort(key=_order)
    return {
        "view": view,
        "pages": pages,
        "board": board,
        "gate": MARKET_PROBE_GATE,
        "board_basis": "for each of the six league pages, the rows of the NEWEST "
                       "scrape this view can serve at `as_of`. `pages[].rows` is "
                       "that board's size and equals this page's share of `board`; "
                       "`rows_all_scrapes` counts every scrape day still visible.",
    }


# ------------------------------------------------------------------- the writer


def freeze_waiver_artifacts(
    conn,
    collect,
    *,
    trigger: str,
    argv: Sequence[str] = (),
    root=None,
    capture_id: str | None = None,
    captured_at: datetime | None = None,
    git=None,
    record: bool = True,
    freeze_id: int | None = None,
) -> CaptureResult:
    """Write ONE capture from a filled :class:`~ziggurat.core.waiver.WaiverArtifacts`.

    Raises on a genuine write failure — :func:`capture_best_effort` is the wrapper
    every command uses, so a fault costs a run-log row and one printed line, never
    the operator's page.

    ``capture_id`` / ``captured_at`` / ``git`` are injectable so a test can fix the
    varying parts and compare bytes; every production caller leaves them alone.
    ``root`` defaults to :data:`DECISIONS_DIR` AT CALL TIME (not as a bound default)
    so a redirect is possible from one place — the suite redirects it so no test
    can drop a synthetic capture into the operator's real archive.
    """
    if collect is None or collect.plan is None:
        raise ValueError(
            "nothing to freeze: build_waiver_plan(collect=...) must have filled the "
            "artifacts before a capture (an empty collector would archive a Tuesday "
            "that never ran)"
        )
    plan = collect.plan
    now = captured_at or datetime.now(CAPTURE_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CAPTURE_TZ)
    cid = capture_id or new_capture_id(now)
    season = plan.season
    week = plan.weeks[0] if plan.weeks else None
    target = capture_dir(DECISIONS_DIR if root is None else root,
                         season=season, week=week, capture_id=cid)

    # Identity is checked BEFORE the run-log row, not after: a duplicate
    # ``capture_id`` is a caller error (production mints a fresh one per run), and
    # letting it reach the UNIQUE constraint would surface as an IntegrityError
    # naming a column instead of naming the capture it would have overwritten.
    if target.exists():
        raise CaptureCollision(
            f"{target} already exists — captures are append-only and are never "
            "overwritten (two captures on one Tuesday are two facts)"
        )
    if record and freeze_id is None and store.capture_by_id(conn, cid) is not None:
        raise CaptureCollision(
            f"capture_id {cid!r} is already recorded in decision_freezes — a capture "
            "id names one run and is never reused"
        )

    if record and freeze_id is None:
        # START-BEFORE-WORK: a killed process must leave a legible row, not silence.
        freeze_id = store.start_capture(
            conn, capture_id=cid, season=season, week=week, trigger=trigger,
            plan_as_of=plan.as_of, started_at=now.isoformat(timespec="seconds"),
        )
        store.reap_orphans(conn, now=now.isoformat(timespec="seconds"))
    try:
        result = _write_capture(
            conn, collect, target=target, capture_id=cid, now=now,
            trigger=trigger, argv=argv, week=week, git=git,
        )
    except BaseException as exc:
        if record and freeze_id is not None:
            store.finish_capture(
                conn, freeze_id, status=store.STATUS_FAILED, week=week,
                finished_at=datetime.now(CAPTURE_TZ).isoformat(timespec="seconds"),
                artifact_dir=str(target), error=f"{type(exc).__name__}: {exc}",
            )
        raise
    if record and freeze_id is not None:
        evaluated = collect.evaluated
        # PUBLISH-THEN-RECORD: only now, with manifest.json on disk.
        store.finish_capture(
            conn, freeze_id, status=result.status, week=week,
            finished_at=datetime.now(CAPTURE_TZ).isoformat(timespec="seconds"),
            artifact_dir=result.directory,
            manifest_sha256=result.manifest_sha256,
            payload_digest=result.payload_digest,
            files=result.files,
            claims=len(plan.claims), grabs=len(plan.fcfs_grabs),
            streaming=len(plan.streaming),
            evaluated=None if evaluated is None else len(evaluated.rows),
            flagged=None if evaluated is None else len(evaluated.flagged),
            blocked=int(bool(plan.blocked)), chain_gain=plan.chain_gain,
            error=result.error,
        )
    return result


def _write_capture(conn, collect, *, target: Path, capture_id, now, trigger, argv,
                   week, git) -> CaptureResult:
    """Write the payload files, then the manifest LAST. Never called directly.

    ``trigger`` IS A THREE-VALUE VOCABULARY, and §2.1 named two (item 4.2b audit,
    DC-11): ``waivers`` | ``cli`` | ``timer``. The third one is the NEW one, not a
    rename — ``waivers`` is the always-on capture inside ``ziggurat waivers``
    (decision D1(a), and the DOMINANT path), ``cli`` is an explicit
    ``ziggurat decisions freeze``, and ``timer`` is the Tuesday 18:30 unit. The
    decided two-way split was "was this the run that produced the decision, or the
    unattended one?"; splitting the attended half in two is what lets a later
    reader tell the page the operator watched from the one he asked for by hand.
    A reader filtering on ``{"cli", "timer"}`` alone sees almost nothing.
    """
    plan = collect.plan
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise CaptureCollision(
            f"{target} already exists — captures are append-only and are never "
            "overwritten (two captures on one Tuesday are two facts)"
        ) from exc

    payloads: dict[str, tuple[bytes, int | None]] = {}
    for name, records in (
        (PLAN_FILE, _plan_records(plan)),
        (SWAPS_FILE, _swap_records(collect)),
        (CANDIDATES_FILE, _candidate_records(collect)),
        (BOARD_FILE, _board_records(collect)),
        (POOL_FILE, _pool_records(collect)),
    ):
        payloads[name] = (_jsonl(records), len(records))
    for name, obj in (
        (ROSTER_FILE, _roster_payload(collect)),
        (MARKET_FILE, _market_payload(conn, collect)),
    ):
        payloads[name] = (_dumps(obj).encode("utf-8") + b"\n", None)
    files: dict[str, dict] = {}
    for name in sorted(payloads):
        blob, rows = payloads[name]
        _write_atomic(target / name, blob)
        files[name] = {"sha256": _sha256(blob), "bytes": len(blob), "rows": rows}

    candidates = _candidates_meta(collect)
    status = store.STATUS_OK if candidates["present"] else store.STATUS_PARTIAL
    error = None if candidates["present"] else candidates["absent_reason"]
    digest = payload_digest(files)

    manifest = {
        "capture_id": capture_id,
        "season": plan.season,
        "week": week,
        # The OTHER week this capture holds, and the reason both are named. See
        # capture_dir's docstring: on an in-season Tuesday these differ by one,
        # and a reader that joins the candidate rows on `week` (or on the
        # directory name) is off by one against every row in candidates.jsonl.
        "candidate_week": candidates.get("week"),
        "candidate_week_basis": (
            "the last REG week fully played AND knowable at this as_of, which is "
            "what build_candidates resolves — NOT `week` above, which is the first "
            "week the board PRICED. On an in-season Tuesday candidate_week == "
            "week - 1. None when the candidate half is absent."
        ),
        "week_basis": (
            "the first week the board priced (plan.weeks[0])" if week is not None
            else "UNRESOLVED — this plan priced no week window (a blocked roster "
                 "refuses before the board is built), so the capture is filed under wk00"
        ),
        "as_of": plan.as_of,
        "captured_at": {
            "pt": now.isoformat(timespec="seconds"),
            "utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        },
        "trigger": trigger,
        "argv": [str(a) for a in argv],
        "code": git if git is not None else git_identity(),
        "schema_version": schema_version(conn),
        "run": {
            "view": collect.view,
            "team_id": collect.team_id,
            "claim_budget": collect.claim_budget,
            "pool_limit": collect.pool_limit,
            "source": collect.source,
            "weeks_requested": list(collect.weeks_requested),
            "weeks_priced": list(plan.weeks),
            "today": collect.today,
        },
        "vintages": dict(collect.vintages),
        "crosswalk_vintage": collect.crosswalk_vintage,
        "counts": {
            "claims": len(plan.claims),
            "grabs": len(plan.fcfs_grabs),
            "streaming": len(plan.streaming),
            "drop_board": len(plan.drop_board),
            "chain_rejected": len(plan.chain_rejected),
            "chain_under_ranked": len(plan.chain_under_ranked),
            "chain_capped": len(plan.chain_capped),
            "chain_measured_not_shown": plan.chain_measured_not_shown,
            "chain_not_repriced": plan.chain_not_repriced,
            "chain_gain": plan.chain_gain,
            "chain_stop": plan.chain_stop,
            "swaps": len(collect.swaps),
            "pool": len(collect.pool_rows),
            "roster": len(collect.roster_rows),
            "blocked": bool(plan.blocked),
            "candidate_notes_attached": len(collect.candidate_notes),
        },
        "candidates": candidates,
        "enriched_chain_equals_projection_only": True,
        "enriched_chain_mechanism": CHAIN_INERT_MECHANISM,
        "disclosures": [POOL_UNKNOWN_NOTE],
        "files": files,
        "payload_digest": digest,
        "status": status,
        "incomplete_reason": error,
        "writer": (
            "ziggurat.decisions.capture (item 4.2b). Manifest/atomic-write pattern "
            "COPIED from backtest/decisions.py and the run-log pattern from "
            "ziggurat/push/runs.py — copied, never imported (ziggurat/ must not "
            "import backtest/, and Rule 8 forbids ziggurat/draft/)."
        ),
        "not_a_decision_input": (
            "This capture has no knowable_as_of and is never read back as an input to "
            "a later decision. It records the GATE the tool ran at (as_of), the wall "
            "clock it ran at, and per source the retrieved_as_of the view resolved."
        ),
    }
    blob = json.dumps(_jsonable(manifest), sort_keys=True, indent=2,
                      ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    # LAST, and atomically: a capture is complete exactly when its manifest exists.
    _write_atomic(target / MANIFEST, blob)
    return CaptureResult(
        status=status, capture_id=capture_id, directory=str(target),
        season=plan.season, week=week, manifest_sha256=_sha256(blob),
        payload_digest=digest, files=len(files), error=error,
    )


def capture_best_effort(conn, collect, *, trigger, argv=(), root=None,
                        **kwargs) -> CaptureResult:
    """:func:`freeze_waiver_artifacts` that NEVER raises.

    Every command wires the capture through this: nothing on the archive side may
    take the Tuesday page down. A fault is a ``failed`` run-log row (written by
    the inner call) plus a ``CaptureResult`` whose ``line`` says the Tuesday is not
    archived — loud, and not fatal.
    """
    try:
        return freeze_waiver_artifacts(conn, collect, trigger=trigger, argv=argv,
                                       root=root, **kwargs)
    except BaseException as exc:  # noqa: BLE001 — the page must survive anything here
        freeze_id = kwargs.get("freeze_id")
        if freeze_id is not None:
            # The pre-started row belongs to THIS run; a fault before
            # freeze_waiver_artifacts could record one (a bad `root`, a collision)
            # would otherwise leave it 'running' until the reaper.
            try:
                if store.capture_status(conn, freeze_id) == store.STATUS_RUNNING:
                    store.finish_capture(
                        conn, freeze_id, status=store.STATUS_FAILED,
                        finished_at=datetime.now(CAPTURE_TZ).isoformat(timespec="seconds"),
                        error=f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 — nothing here may raise
                pass
        return CaptureResult(
            status=store.STATUS_FAILED,
            capture_id=str(kwargs.get("capture_id") or "?"),
            directory=None,
            season=getattr(getattr(collect, "plan", None), "season", None),
            week=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def record_prerun_failure(conn, *, season, trigger, plan_as_of, error,
                          now=None) -> str:
    """Write ONE ``failed`` run-log row for a Tuesday that never reached the plan.

    The last hole in "a failure is visible three ways" (item 4.2b audit, OPS-3):
    the caller resolves ESPN credentials and the own-team id BEFORE
    :func:`run_freeze` can record anything, and expired cookies are a documented
    recurring event. Without this, the single most likely Tuesday failure leaves
    the journal as its only trace and ``ziggurat decisions status`` shows nothing
    at all for that day.

    Returns the capture id it minted. Never raises: a run-log write must not turn
    one failure into two.
    """
    stamp = now or datetime.now(CAPTURE_TZ)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CAPTURE_TZ)
    cid = new_capture_id(stamp)
    iso = stamp.isoformat(timespec="seconds")
    try:
        freeze_id = store.start_capture(
            conn, capture_id=cid, season=season, week=None, trigger=trigger,
            plan_as_of=plan_as_of, started_at=iso,
        )
        store.finish_capture(conn, freeze_id, status=store.STATUS_FAILED,
                             finished_at=iso, error=str(error))
    except Exception:  # noqa: BLE001 — the failure being reported is the point
        pass
    return cid


#: ``pool_limit=UNSET`` means "whatever ``build_waiver_plan`` ships as its
#: default". ``None`` already MEANS the whole pool there, so it cannot double as
#: "unspecified" — a caller who omitted the argument would silently get the
#: 876-row pre-draft universe instead of the top 30 per position.
UNSET = object()


def run_freeze(conn, *, as_of, season, own_team_id, trigger=store.TRIGGER_CLI,
               argv=(), weeks=None, last_week=17, pool_limit=UNSET,
               source="sleeper_rotowire", history=None, record=True,
               capture_id=None, captured_at=None,
               claim_budget=3, today=None, root=None, **kwargs):
    """Run the Tuesday plan and capture it — the ``ziggurat decisions freeze`` body.

    Returns ``(plan, CaptureResult)``. The plan is built with a collector attached
    and the capture is best-effort, so a plan that builds is always returned even
    if the archive write fails. A plan that does NOT build raises: there is nothing
    to archive, and the caller reports it.

    ``history`` is the NEW/REPEAT badge's comparison set (``read.episode_history_provider``),
    passed in rather than built here so this module keeps its one-way import edge
    with ``read`` — and so a timer run and a ``waivers`` run badge identically.

    The CANDIDATE half is allowed to be absent — before Week 1 the generator raises
    ``NoCompletedWeek`` and ``build_waiver_plan`` already degrades silently — and
    the capture records that as ``partial`` with the reason, rather than failing.
    """
    from ziggurat.core.marginal import DEFAULT_POOL_LIMIT
    from ziggurat.core.waiver import WaiverArtifacts, build_waiver_plan

    now = captured_at or datetime.now(CAPTURE_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CAPTURE_TZ)
    cid = capture_id or new_capture_id(now)

    # START BEFORE THE PLAN, not before the WRITE (item 4.2b audit, OPS-3). Every
    # pre-plan failure — expired ESPN cookies (a documented recurring event), an
    # unresolvable week, an unresolvable own team, anything raised inside
    # build_waiver_plan — used to exit before any row existed, so the timer's own
    # claim that "a failure is visible three ways" was true of the journal only:
    # `decisions status` showed NOTHING AT ALL for a Tuesday that failed. It
    # suppresses nothing (a later capture is a new row with a new capture_id) and
    # `reap_orphans` already covers the window this widens.
    freeze_id = None
    if record:
        freeze_id = store.start_capture(
            conn, capture_id=cid, season=season, week=None, trigger=trigger,
            plan_as_of=as_of, started_at=now.isoformat(timespec="seconds"),
        )
        store.reap_orphans(conn, now=now.isoformat(timespec="seconds"))

    collect = WaiverArtifacts()
    try:
        plan = build_waiver_plan(
            conn, as_of=as_of, season=season, own_team_id=own_team_id, weeks=weeks,
            last_week=last_week,
            pool_limit=DEFAULT_POOL_LIMIT if pool_limit is UNSET else pool_limit,
            source=source, claim_budget=claim_budget, today=today, collect=collect,
            history=history,
        )
    except BaseException as exc:
        if freeze_id is not None:
            store.finish_capture(
                conn, freeze_id, status=store.STATUS_FAILED,
                finished_at=datetime.now(CAPTURE_TZ).isoformat(timespec="seconds"),
                error=f"the plan did not build, so there was nothing to archive: "
                      f"{type(exc).__name__}: {exc}",
            )
        raise
    result = capture_best_effort(conn, collect, trigger=trigger, argv=argv, root=root,
                                 capture_id=cid, captured_at=now, record=record,
                                 freeze_id=freeze_id, **kwargs)
    return plan, result
