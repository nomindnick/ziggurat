"""Read a decision capture back, and REFUSE one whose bytes moved (item 4.2b, B2).

An archive nobody re-reads for months is only worth what its integrity check is
worth. Every read here goes through the manifest: a file the manifest does not
name is reported, a file whose sha256 disagrees is a refusal, and a manifest
whose own digest disagrees with the run-log row is a refusal too — a mutation
that edited a payload file AND its manifest entry would otherwise verify clean.

Nothing in this module reads a capture as a DECISION INPUT. A freeze is a record
of a computation, not a fact about the NFL: it has no ``knowable_as_of``, it is
never passed to ``base.select_as_of``, and the analysis that reads it (the
five-way classification, the latency query — item 4.2b B12) does its joins in
Python against the normal as-of gated accessors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from ziggurat.decisions.capture import MANIFEST


class CaptureUnreadable(ValueError):
    """The capture cannot be trusted: a missing manifest, a missing file, or a
    digest that does not match. Never repaired, never worked around."""


@dataclass(frozen=True)
class VerifyReport:
    """Whether every byte of one capture still matches its manifest."""

    directory: str
    capture_id: str | None
    ok: bool
    files_checked: int
    problems: tuple[str, ...]
    manifest_sha256: str | None
    payload_digest: str | None
    status: str | None = None
    incomplete_reason: str | None = None

    def render(self) -> str:
        head = "VERIFIED" if self.ok else "REFUSED"
        lines = [f"{head}  {self.capture_id or '?'}  {self.directory}",
                 f"  files checked: {self.files_checked}"]
        if self.status:
            lines.append(f"  capture status: {self.status}")
        if self.incomplete_reason:
            lines.append(f"  incomplete: {self.incomplete_reason}")
        if self.ok:
            lines.append(f"  payload digest: {self.payload_digest}")
            lines.append("  every file's sha256 matches the manifest.")
        else:
            lines.append("  THIS CAPTURE IS NOT TRUSTWORTHY:")
            lines.extend(f"    - {p}" for p in self.problems)
        return "\n".join(lines)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(directory) -> dict:
    path = Path(directory) / MANIFEST
    if not path.exists():
        raise CaptureUnreadable(
            f"no manifest at {path}: an unmanifested directory is an INCOMPLETE "
            "capture (the manifest is written last, so its absence means the writer "
            "died mid-capture) and must not be read as a record of a decision"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaptureUnreadable(f"{path} is unreadable: {exc}") from exc


def verify(directory, *, expected_manifest_sha256: str | None = None) -> VerifyReport:
    """Re-hash every file the manifest names. ``ok`` is False on ANY mismatch.

    ``expected_manifest_sha256`` — the digest the ``decision_freezes`` row recorded
    when the capture landed — closes the one hole a files-vs-manifest check leaves
    open: an edit that changed a payload file AND its manifest entry.
    """
    directory = Path(directory)
    problems: list[str] = []
    manifest_path = directory / MANIFEST
    if not manifest_path.exists():
        return VerifyReport(
            directory=str(directory), capture_id=None, ok=False, files_checked=0,
            problems=(f"no {MANIFEST}: incomplete capture (the manifest is written "
                      f"last, so its absence means the writer died mid-capture)",),
            manifest_sha256=None, payload_digest=None,
        )
    blob = manifest_path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    try:
        manifest = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return VerifyReport(
            directory=str(directory), capture_id=None, ok=False, files_checked=0,
            problems=(f"{MANIFEST} is not readable JSON: {exc}",),
            manifest_sha256=digest, payload_digest=None,
        )
    if expected_manifest_sha256 and digest != expected_manifest_sha256:
        problems.append(
            f"{MANIFEST} sha256 {digest} != the {expected_manifest_sha256} recorded in "
            "decision_freezes when this capture landed — the manifest itself was edited"
        )
    files = manifest.get("files") or {}
    checked = 0
    for name in sorted(files):
        path = directory / name
        if not path.exists():
            problems.append(f"{name}: named by the manifest and missing from the directory")
            continue
        actual = _sha256_file(path)
        checked += 1
        if actual != files[name].get("sha256"):
            problems.append(
                f"{name}: sha256 {actual} != the manifest's {files[name].get('sha256')} "
                "— a byte of this capture moved after it was written"
            )
    named = set(files) | {MANIFEST}
    extra = sorted(p.name for p in directory.iterdir()
                   if p.is_file() and p.name not in named)
    if extra:
        problems.append(
            "files in the directory that the manifest does not name: "
            + ", ".join(extra)
            + " (a leftover temp file means a writer died mid-capture; anything else "
              "was added after the fact and is not part of the record)"
        )
    return VerifyReport(
        directory=str(directory), capture_id=manifest.get("capture_id"),
        ok=not problems, files_checked=checked, problems=tuple(problems),
        manifest_sha256=digest, payload_digest=manifest.get("payload_digest"),
        status=manifest.get("status"),
        incomplete_reason=manifest.get("incomplete_reason"),
    )


def read_records(directory, name) -> list[dict]:
    """One verified JSONL file, as a list of records.

    Refuses on a digest mismatch (:class:`CaptureUnreadable`) rather than handing
    back rows from a file that moved — a silently-repaired archive is worse than a
    missing one, because the number it hands you looks exactly as authoritative.
    """
    directory = Path(directory)
    manifest = load_manifest(directory)
    entry = (manifest.get("files") or {}).get(name)
    if entry is None:
        raise CaptureUnreadable(f"{name} is not named by {directory / MANIFEST}")
    path = directory / name
    if not path.exists():
        raise CaptureUnreadable(f"{path} is named by the manifest and missing")
    blob = path.read_bytes()
    actual = hashlib.sha256(blob).hexdigest()
    if actual != entry.get("sha256"):
        raise CaptureUnreadable(
            f"{path}: sha256 {actual} != the manifest's {entry.get('sha256')}"
        )
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]
    return [json.loads(blob.decode("utf-8"))]


class CaptureNotFound(LookupError):
    """No ``decision_freezes`` row answers this request. Carries the operator-facing
    sentence — the CLI prints it, it does not compose it (Rule 3)."""


class CaptureIncomplete(LookupError):
    """A row exists but named no directory, so there is nothing to verify."""


def resolve_capture_target(conn, *, capture_id=None) -> tuple[Path, str | None]:
    """(directory, the manifest sha256 recorded when it landed) for one capture.

    ``capture_id=None`` means the most recent capture recorded. Raises
    :class:`CaptureNotFound` / :class:`CaptureIncomplete` with the message the
    operator should read.

    THIS IS PACKAGE CODE ON PURPOSE (item 4.2b audit, R4). It was six branches in
    the ``decisions verify`` CLI body — a choice between two lookups, two failure
    sentences composed from row state, and a digest hand-over — while every
    sibling command in that file resolves its inputs through a helper and then
    makes ONE call. ``decisions record`` / ``classify`` / ``latency`` all need
    the same resolution, so leaving it there was leaving it to be copied.
    """
    from ziggurat.decisions import store

    row = (store.capture_by_id(conn, capture_id) if capture_id
           else store.last_capture(conn))
    if row is None:
        raise CaptureNotFound(
            "no capture recorded"
            + (f" for id {capture_id}" if capture_id else " yet")
            + " — run `ziggurat decisions status`."
        )
    if not row["artifact_dir"]:
        raise CaptureIncomplete(
            f"capture {row['capture_id']} is '{row['status']}' and named no "
            "directory — nothing landed to verify."
        )
    return Path(row["artifact_dir"]), row["manifest_sha256"]


def find_capture(root, *, capture_id) -> Path | None:
    """The directory of one capture id, searched under ``root``.

    A capture's path encodes ``<season>/wk<NN>/<capture_id>``, but the run log is
    the authority on where a capture landed — this is the fallback for a capture
    whose row is gone (or for a directory copied elsewhere).
    """
    root = Path(root)
    if not root.is_dir():
        return None
    for path in sorted(root.glob(f"*/*/{capture_id}")):
        if path.is_dir():
            return path
    return None


# ------------------------------------------------ the episode history (B3/B12)

def week_flags_history(root, *, season, before_week):
    """The archived weeks of one season, as :class:`ziggurat.core.candidates.WeekFlags`.

    This is what makes the NEW / REPEAT badge a comparison rather than a
    placeholder: ``build_candidates(history=...)`` calls it once, and without it
    every row on every surface reads ``FIRST SEEN (no archive yet)`` forever
    (item 4.2b audit, DC-2).

    THE WEEK IS THE CANDIDATE WEEK, NEVER THE DIRECTORY'S. A capture is filed
    under the week the board PRICED and its candidate rows describe the last week
    fully played — one lower on every in-season Tuesday (see
    ``capture.capture_dir``). So the week is read out of ``board.jsonl``'s own
    header, and a capture whose board half is absent contributes nothing.

    ONE CAPTURE PER ARCHIVED WEEK IS READ. Two captures on one Tuesday are two
    facts, but they describe the same board and the badge needs one — so the
    NEWEST readable capture in a week's directory wins and the rest are never
    opened. Directory names begin with a Pacific timestamp, so a plain reverse
    sort is chronological.

    AN UNREADABLE CAPTURE IS SKIPPED, NOT REPAIRED AND NOT FATAL. A digest
    mismatch, a missing file, a capture written before ``board.jsonl`` existed —
    each simply leaves that week un-archived, which
    ``candidates.episode_tag_for`` already reports as a BOUND ("NEW (no archive
    for N earlier wk(s))") rather than as a fact. Raising instead would take the
    badge down for the whole season because one directory moved.

    Rule 1 is not in play and that is deliberate: a capture has no
    ``knowable_as_of`` and is never read as a decision INPUT. What comes back is
    "which weeks did this tool flag this player", i.e. a fact about the tool's own
    past output — the callers still gate every NFL fact through the normal
    accessors.
    """
    from ziggurat.core import candidates as C

    root = Path(root) / str(int(season))
    if not root.is_dir():
        return []
    best: dict = {}
    for week_dir in sorted(root.glob("wk*")):
        if not week_dir.is_dir():
            continue
        for capture in sorted((d for d in week_dir.iterdir() if d.is_dir()),
                              reverse=True):
            flags = _week_flags_of(capture, C)
            if flags is None:
                continue
            if int(flags.week) >= int(before_week):
                break          # this capture's week is out of range; so are older
                               # captures of the SAME week — try the next week dir
            if flags.week not in best:
                best[flags.week] = flags
            break              # newest readable capture of this directory wins
    return [best[w] for w in sorted(best)]


def _week_flags_of(capture: Path, C):
    """One capture -> ``WeekFlags``, or ``None`` when it cannot be trusted.

    Builds the module's own objects and calls ``candidates.week_flags`` rather
    than re-deriving the episode key: a reader that invents its own key is a
    second identity rule that can silently diverge from the generator's.
    """
    try:
        board_records = read_records(capture, "board.jsonl")
    except (CaptureUnreadable, OSError, ValueError):
        return None
    header = next((r for r in board_records if r.get("record") == "board"), None)
    if header is None or header.get("week") is None:
        return None
    rows = tuple(
        C.CandidateRow(
            player_key=str(r.get("player_key") or ""),
            player=str(r.get("player") or ""),
            position=r.get("position"), team=r.get("team"),
            gsis_id=r.get("gsis_id"), espn_id=r.get("espn_id"),
            signal_kind=str(r.get("signal_kind") or ""),
            magnitude=float(r.get("magnitude") or 0.0),
            week=int(header["week"]), prior_week=r.get("prior_week"),
            hypothesis=bool(r.get("hypothesis")),
            reasons=tuple(r.get("reasons") or ()),
            episode_tag=str(r.get("episode_tag") or ""),
        )
        for r in board_records if r.get("record") == "flagged"
    )
    board = C.CandidateBoard(
        rows=rows, week=int(header["week"]),
        freshness=tuple(header.get("freshness") or ()),
        notes=tuple(header.get("notes") or ()),
        as_of=str(header.get("as_of") or ""), season=int(header.get("season") or 0),
    )
    evaluated: list = []
    try:
        for rec in read_records(capture, "candidates.jsonl"):
            evaluated.append(SimpleNamespace(gsis_id=rec.get("gsis_id"),
                                             team=rec.get("team")))
    except (CaptureUnreadable, OSError, ValueError):
        # A board with no evaluated table is the OLD-capture case week_flags
        # already documents: the rule still runs, it just cannot see the weeks a
        # player was looked at and passed over.
        evaluated = []
    return C.week_flags(board, evaluated)


def episode_history_provider(root=None):
    """The ``history=`` callable ``build_candidates`` accepts, bound to the
    archive on this box. Rule 3: the CLI passes this, it does not build one.

    A callable rather than a sequence, because ``ziggurat/core/`` must not import
    ``ziggurat/decisions/`` — the generator never learns where the archive is,
    and a caller with no archive passes nothing and gets the honest un-badged
    page (which now says so — item 4.2b audit, OPS-7).
    """
    from ziggurat.decisions.capture import DECISIONS_DIR

    def provider(*, season, before_week):
        base = DECISIONS_DIR if root is None else root
        return week_flags_history(base, season=season, before_week=before_week)

    return provider
