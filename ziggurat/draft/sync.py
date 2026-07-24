"""ESPN draft-room DOM-sync: parse harvested picks and resolve them onto the board.

DELETABLE package (Rule 8). The 2026-07-24 spike proved the draft-room PAGE
renders picks live (the REST API does not): a userscript watching the Pick
History panel pushes each pick to the cockpit's ``/api/sync``. This module is
the PURE half of that pipeline — no HTTP, no DOM, no session: given the raw
strings the userscript harvested, produce either a confident board entry or an
explicit "needs the operator" verdict. Every unit test runs offline.

Trust model (Rule 6 — the operator can't smell a wrong pick, so the code must
refuse rather than guess): a synced pick is auto-committed ONLY when the
resolution is unambiguous — an exact ESPN player-id match from the anchor href,
a resolver AUTO verdict, or a CONFIRM verdict whose sole position-and-team-
consistent candidate is the top one. Anything else is BLOCKED and surfaced for
a one-click manual entry; sync then resumes on its own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from ziggurat.data.nfl import base
from ziggurat.draft.bots import BoardEntry
from ziggurat.draft.resolver import NICKNAME_TO_ABBR, NameResolver, normalize_query

# Every canonical team abbreviation plus every alias variant, so the team
# suffix is VALIDATED rather than regex-guessed — "SF D/STSF" must yield team
# SF, not a phantom "TSF" (the D/ST 'ST' letters collide with a bare
# trailing-capitals match for 2-letter teams).
_KNOWN_ABBRS = frozenset(NICKNAME_TO_ABBR.values()) | {
    a.upper() for a in base.TEAM_ALIASES
}

# Position tokens as ESPN renders them in the history cell, longest first so
# "D/ST" wins before a bare trailing letter could be misread.
_POS_TOKENS = ("D/ST", "DST", "QB", "RB", "WR", "TE", "K")
# Injury/status flags ESPN appends to the player name ("Cam SkatteboQ").
# Matched ONLY as a whole trailing token after team+pos are stripped, so name
# suffixes like "III" or initials like "DK" are never eaten.
_STATUS_FLAGS = ("IR", "SSPD", "NA", "Q", "O", "D", "P")

# ESPN player-page hrefs carry the player id: .../id/4362628/... — the same id
# the board's ``espn_id`` uses, giving an exact match that bypasses name fuzz.
_HREF_ID_RE = re.compile(r"/id/(\d+)(?:/|$)")


@dataclass(frozen=True)
class ParsedPick:
    """One harvested pick, decomposed. ``espn_id`` is None when no usable href
    came through; ``team``/``position`` are None when parsing could not split
    the concatenated cell text (resolution then leans on the name alone)."""

    overall: int
    name: str            # identity name (anchor text when present, else cell)
    cell_name: str       # name parsed from the FULL cell text ("" if unparsed)
    position: str | None
    team: str | None
    espn_id: str | None
    fantasy_team: str | None  # ESPN's drafting-team label (display only)


@dataclass(frozen=True)
class SyncResolution:
    """Verdict for one parsed pick. ``entry`` is set iff ``confident``;
    ``reason`` is a short honest sentence for the UI either way."""

    confident: bool
    entry: BoardEntry | None
    reason: str


def _norm_team(abbr: str | None) -> str | None:
    if not abbr:
        return None
    up = abbr.strip().upper()
    return base.TEAM_ALIASES.get(up, up) or None


def _canon_pos(pos: str | None) -> str | None:
    if not pos:
        return None
    up = pos.strip().upper()
    return "DST" if up in ("D/ST", "DST", "DEF") else up


def parse_history_cell(text: str) -> tuple[str, str | None, str | None]:
    """Split ESPN's concatenated player cell into (name, nfl_team, position).

    The Pick History cell renders as ``<name><status?><TEAM><POS>`` with no
    separators once textContent flattens it: "Ja'Marr ChaseCINWR",
    "Texans D/STHOUD/ST", "Brandon AubreyDALK", "Cam SkatteboQNYGRB".
    D/ST is special: the NAME itself ends in "D/ST" and the position token is
    also "D/ST", so the position is stripped FIRST, then the team, and a name
    that still ends with "D/ST" keeps it (that's the defense's actual name).
    Returns (text, None, None) unchanged when the pattern doesn't match —
    resolution then works from the raw string and the confidence gate decides.
    """
    raw = (text or "").strip()
    if not raw:
        return ("", None, None)

    pos = None
    rest = raw
    for tok in _POS_TOKENS:
        if rest.endswith(tok):
            # Guard the bare-"K" token: only accept when what remains still
            # ends in a KNOWN team abbreviation (a name ending in K alone,
            # with no team appended, must not lose its final letter).
            candidate = rest[: -len(tok)]
            if tok == "K" and not any(
                len(candidate) > k
                and candidate[-k:].isupper()
                and candidate[-k:] in _KNOWN_ABBRS
                for k in (3, 2)
            ):
                continue
            pos = tok
            rest = candidate
            break
    if pos is None:
        return (raw, None, None)

    team = None
    for k in (3, 2):  # longest-valid-abbr first ("SEA" before "EA")
        # The suffix must be UPPERCASE IN THE RAW TEXT: ESPN renders abbrs in
        # caps, and a lowercase name tail that happens to spell one ("Luther
        # Burden" -> "den" -> DEN) must never become a phantom team (audit).
        if len(rest) > k and rest[-k:].isupper() and rest[-k:] in _KNOWN_ABBRS:
            team = rest[-k:]
            rest = rest[:-k]
            break

    name = rest.strip()
    for flag in _STATUS_FLAGS:
        if name.endswith(flag) and len(name) > len(flag):
            trimmed = name[: -len(flag)].rstrip()
            # Only treat it as a status flag when it directly follows a
            # lowercase letter or period (end of a real name), so "DK" in
            # "DK Metcalf" or a "III" suffix is never clipped.
            if trimmed and (trimmed[-1].islower() or trimmed[-1] == "."):
                name = trimmed
            break
    return (name, _norm_team(team), pos)


def parse_payload_pick(p: dict) -> ParsedPick | None:
    """Validate/decompose one userscript payload item; None if malformed."""
    try:
        overall = int(p["overall"])
    except (KeyError, TypeError, ValueError):
        return None
    if overall < 1:
        return None
    raw_name = str(p.get("player") or "").strip()
    if not raw_name:
        return None
    cell_name, team, pos = parse_history_cell(raw_name)
    # The clean anchor-text name is the primary identity, but the CELL name is
    # kept alongside it: anchor and cell come from different DOM sources, so a
    # wrong first <a> in the cell (re-audit finding 2) shows up as a
    # name disagreement the commit gate can catch.
    clean = str(p.get("player_clean") or "").strip()
    name = clean or cell_name
    cell_name = cell_name if pos is not None else ""  # unparsed cell -> no claim
    espn_id = None
    href = str(p.get("href") or "")
    m = _HREF_ID_RE.search(href)
    if m:
        espn_id = m.group(1)
    fantasy_team = str(p.get("fantasy_team") or "").strip() or None
    return ParsedPick(
        overall=overall, name=name, cell_name=cell_name,
        position=_canon_pos(pos), team=team,
        espn_id=espn_id, fantasy_team=fantasy_team,
    )


def _consistent(entry: BoardEntry, pick: ParsedPick) -> bool:
    """Does a candidate agree with everything the harvest parsed? Missing
    parse fields are permissive (no evidence ≠ contradiction)."""
    if pick.position is not None and _canon_pos(entry.position) != pick.position:
        return False
    if pick.team is not None and entry.team is not None:
        if _norm_team(entry.team) != pick.team:
            return False
    return True


_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def _core_name(s: str | None) -> str:
    """Punctuation/accent/generational-suffix-blind canonical name form."""
    if not s:
        return ""
    return " ".join(t for t in normalize_query(s).split() if t not in _NAME_SUFFIXES)


def _same_name(a: str | None, b: str | None) -> bool:
    """The commit-gate name identity: 'Marvin Harrison Jr.' == 'Marvin
    Harrison', "Ja'Marr" == "JaMarr", but 'Dylan Stone' != 'Aaron Stone'.
    Nickname drift (ESPN 'Hollywood Brown' vs board 'Marquise Brown')
    deliberately FAILS — that pick blocks for a one-click confirm rather than
    risking a guess."""
    ca, cb = _core_name(a), _core_name(b)
    return ca != "" and ca == cb


def _entry_matches_pick(entry: BoardEntry, pick: ParsedPick) -> bool:
    """The ONE commit predicate (audit 2026-07-24: every weaker gate leaked).

    A pick may auto-commit only when the candidate is field-consistent AND
    identity-matched: same name (suffix-blind), or — for a D/ST, whose display
    name differs by design (ESPN 'Texans D/ST' vs board 'HOU D/ST') — same NFL
    team with both sides agreeing the pick is a defense."""
    if not _consistent(entry, pick):
        return False
    if (
        pick.position == "DST"
        and _canon_pos(entry.position) == "DST"
        and pick.team is not None
        and entry.team is not None
        and _norm_team(entry.team) == pick.team
    ):
        return True
    if not _same_name(entry.name, pick.name):
        return False
    # Re-audit finding 2: the anchor (identity name + href id) and the cell
    # text are different DOM sources. When the cell parsed cleanly, the entry
    # must agree with the CELL name too — a wrong first <a> then disagrees
    # and the pick blocks instead of committing the anchor's player.
    if pick.cell_name and not _same_name(entry.name, pick.cell_name):
        return False
    return True


def resolve_synced_pick(
    resolver: NameResolver,
    board: Sequence[BoardEntry],
    pick: ParsedPick,
    *,
    taken: AbstractSet[str] = frozenset(),
) -> SyncResolution:
    """Resolve one harvested pick to a board entry, or refuse with a reason.

    Every rung ends at the SAME gate — ``_entry_matches_pick`` (consistency +
    suffix-blind name identity, or DST team identity). The audit proved any
    weaker gate silently commits a wrong player (panel-truncated uniqueness,
    an unchecked stale href id, a team-less entry "agreeing" with anything):

      1. exact ``espn_id`` from the player link href — commit IF the gate holds
         (a matching id with contradicting name/pos/team is DOM drift, not
         truth — refuse);
      2. resolver AUTO/CONFIRM top candidate — commit IF the gate holds;
      3. anything else — BLOCKED (the UI prefills the search with the name).
    """
    if pick.espn_id is not None:
        entry = next(
            (e for e in board
             if e.player_id == pick.espn_id and e.player_id not in taken),
            None,
        )
        if entry is not None:
            if _entry_matches_pick(entry, pick):
                return SyncResolution(True, entry, f"exact ESPN id match ({pick.name})")
            return SyncResolution(
                False, None,
                f"ESPN id {pick.espn_id} maps to {entry.name}, but the page "
                f"shows '{pick.name}' — refusing to guess",
            )
        return SyncResolution(
            False, None,
            f"ESPN id {pick.espn_id} ({pick.name}) not available on the board",
        )

    res = resolver.resolve(pick.name, taken=taken)
    if res.kind in ("auto", "confirm") and res.candidates:
        top = res.candidates[0]
        if _entry_matches_pick(top, pick):
            # Re-audit finding 3: two draftable board entries can share a core
            # name (rookie/veteran clashes). Name identity cannot pick between
            # twins — if ANY other available entry also satisfies the gate,
            # refuse rather than guess (the module's stated rule).
            twins = sum(
                1 for e in board
                if e.player_id not in taken and _entry_matches_pick(e, pick)
            )
            if twins > 1:
                return SyncResolution(
                    False, None,
                    f"'{pick.name}' matches {twins} different players on the "
                    "board — pick the right one by hand",
                )
            return SyncResolution(True, top, f"name match ({pick.name})")
        return SyncResolution(
            False, None,
            f"'{pick.name}' has no exact board match (closest: {top.name})",
        )
    return SyncResolution(False, None, f"no board match for '{pick.name}'")
