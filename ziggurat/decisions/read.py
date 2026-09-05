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
