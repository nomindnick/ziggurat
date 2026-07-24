"""Item 2.4 — the structured tiered name/alias resolver for TUI pick entry.

DELETABLE package (Rule 8): lives under ``ziggurat/draft/`` and nothing outside
that package imports it. Pure over the in-memory board — no DB handle, no I/O, no
terminal — so every unit test runs offline on a synthetic board (Rule 5).

WHAT THIS IS (recon ``intel/research/tui-2.4-recon.md`` §2 "Fuzzy pick entry"):
turn a typed fragment ("cmc", "niners", "mhj", "mahomes") into a resolved
:class:`BoardEntry` on the live draft board, erring toward a one-keystroke CONFIRM
panel rather than a silent wrong pick — because a wrong entry corrupts
``BoardState`` and every downstream recommendation (recon §6 top risk).

DESIGN — a stdlib-only structured tiered scorer (recon: at this board size the
accuracy win comes from STRUCTURE — initials / prefixes / DST nicknames — not from
a faster edit-distance kernel, so no ``rapidfuzz``). Each named candidate is scored
as the MAX over structural tiers; ``difflib.SequenceMatcher`` is only the
last-resort typo tier. Ties break by ``score − espn_overall_rank/1e7`` so the more
draftable player wins (implemented as a secondary sort on rank).

THE THREE VERIFIER-MANDATED MUSTs (recon §3 / §6):
  1. **Empty/whitespace guard** — ``resolve("")`` returns ``kind="empty"`` and
     NEVER a match (a bare ``startswith("")`` otherwise commits the #1 player).
  2. **Elite-safety candidate list** — the confirm top-3 can never exclude the
     best rank-weighted near-match across tiers, so an elite player is never
     silently dropped behind three deep same-surname collisions (the recon
     "james"/"saquan"/"dk" counterexamples).
  3. **Err toward confirm** — auto-accept ONLY on an exact/curated-alias hit or a
     single dominant top-tier candidate; ties/close calls surface a confirm panel.

Two curated maps live in this module: team-nickname -> abbr (for DST entry, seeded
from ``base.TEAM_ALIASES`` plus common nicknames) and a famous-initialism/nickname
alias map. Both grow from Checkpoint-2 rehearsal misses (recon §3 MUST 3).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from difflib import SequenceMatcher

from ziggurat.data.nfl import base
from ziggurat.draft.bots import BoardEntry

# --------------------------------------------------------------- tuning knobs
#
# Tier scores (recon §2): exact 1000 > core-join 980 > DST marker/bare 995/990 >
# initials 900 > first-initial+last 880 > last-name 820 > first-name/token 780 >
# prefixes 760/740/700 > subsequence 620 > difflib typo (<= 400*ratio).
_T_EXACT = 1000.0
_T_DST_MARKED = 995.0    # "niners defense" — a defense marker + team match
_T_DST_BARE = 990.0      # "niners" / "sf" — a bare team nickname/abbr => the DST
_T_CORE_JOIN = 980.0     # "patrickmahomes" typed without the space
_T_INITIALS = 900.0      # "pm" / "mhj" (suffix-inclusive)
_T_FI_LAST = 880.0       # "pmahomes" / "p mahomes"
_T_LAST = 820.0          # surname exact
_T_FIRST = 780.0         # first name / any single token exact
_T_LAST_PREFIX = 760.0
_T_CORE_PREFIX = 740.0
_T_FIRST_PREFIX = 700.0
_T_SUBSEQ = 620.0
_T_DIFFLIB_CAP = 400.0   # capped typo tier: _T_DIFFLIB_CAP * SequenceMatcher.ratio()

# Elite-safety floor (MUST 2). recon §3 mandates "the best rank-weighted fuzzy
# candidate ACROSS tiers" is never dropped from the confirm panel — so this floor
# must sit BELOW the typo/subsequence/prefix tiers, not at the first-name tier.
# A 780 floor (the old value) contradicted "across tiers": a plausible surname
# typo (difflib <=400), truncation (subsequence 620) or last-prefix (760) of a
# star dropped that star out of the panel entirely (refuter finding F1: "jacob" ->
# Josh Jacobs #21 at 760, "ribinson" -> Bijan #2 at 350, "landon" -> Drake London
# #13 at 333, "aefferson" -> Justin Jefferson #11 at 355, "henro" -> Derrick Henry
# #20 at 320 all absent). 300 rescues those while staying above _NONE_FLOOR noise;
# the rank-tiebreak still force-keeps only the single most-draftable near-match.
_ELITE_FLOOR = 300.0

# Below this a candidate is noise (a weak difflib echo) and is not offered at all;
# if every candidate is below it the query resolves to kind="none".
_NONE_FLOOR = 280.0      # difflib ratio ~0.72 => ~288, the weakest real typo hit

# AUTO gate (MUST 3, err toward confirm): auto-accept only a dominant top-tier hit.
_AUTO_TIER = _T_INITIALS         # initials / core-join / exact are "top tier"
_AUTO_GAP = 60.0                 # ... and only when it clears the runner-up by this
_DIFFLIB_MIN_RATIO = 0.72        # don't even compute a typo score below this

# F2 (alias-vs-elite guard): a curated-alias AUTO yields to a CONFIRM when some
# OTHER candidate that is genuinely elite (ESPN rank <= _ALIAS_RIVAL_RANK) also
# matches the RAW query at the initials tier (>= _ALIAS_RIVAL_TIER) or better —
# e.g. "aj" -> A.J. Brown must not silently bury Ashton Jeanty (#17). "cmc"/"mhj"
# keep auto-firing because no top-100 rival matches their key.
_ALIAS_RIVAL_RANK = 100
_ALIAS_RIVAL_TIER = _T_INITIALS

# F3/NEW-1 (DST-vs-player guard): a DST abbr/nickname/city AUTO (990/995) yields to
# a CONFIRM when a real player scores at first-name tier (>= _DST_COLLIDE_TIER) or
# better on the same query — "kc" (KC Concepcion), "dallas" (Dallas Goedert),
# "washington" (Parker Washington). One rule covers 2-char abbrs and full words.
_DST_COLLIDE_TIER = _T_FIRST

# F5 (unranked-scrub cap): an unranked deep entry (ESPN rank >= _UNRANKED_RANK) can
# never outrank a ranked player, so its score caps below the last-/first-name tiers
# — "james" must not show Jesse James #10607 above James Cook #15. Deep entries are
# NOT filtered from the index (a rival can draft a deep K late); they stay reachable
# whenever nothing ranked matches.
_UNRANKED_RANK = 10000
_UNRANKED_CAP = 700.0

_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

# D/ST positions (the board's canonical spellings) — used by the index and by the
# F3 DST-collision guard so the check lives in exactly one place.
_DST_POSITIONS = frozenset({"DST", "D/ST", "DEF"})


def _is_dst_position(pos: str) -> bool:
    return pos.upper() in _DST_POSITIONS


# ------------------------------------------------------------- curated maps
#
# Team nickname/city/abbr -> the board's TEAM_ALIASES-normalized abbr (Rams=LA,
# Chargers=LAC, Raiders=LV, Commanders=WAS, Jaguars=JAX — matching
# simulator.load_board so a DST entry's ``team`` lines up). Generous by design;
# grown from rehearsal (recon §3 MUST 3). Not real colleague names (Rule 5) — NFL
# team identities only.
_TEAM_NICKS: dict[str, tuple[str, ...]] = {
    "ARI": ("cardinals", "cards", "arizona", "arizona cardinals", "big red"),
    "ATL": ("falcons", "atlanta", "atlanta falcons", "dirty birds"),
    "BAL": ("ravens", "baltimore", "baltimore ravens"),
    "BUF": ("bills", "buffalo", "buffalo bills", "bills mafia", "mafia"),
    "CAR": ("panthers", "carolina", "carolina panthers"),
    "CHI": ("bears", "chicago", "chicago bears", "da bears"),
    "CIN": ("bengals", "cincinnati", "cincy", "cincinnati bengals"),
    "CLE": ("browns", "cleveland", "cleveland browns"),
    "DAL": ("cowboys", "dallas", "dallas cowboys", "boys", "americas team"),
    "DEN": ("broncos", "denver", "denver broncos"),
    "DET": ("lions", "detroit", "detroit lions"),
    "GB": ("packers", "green bay", "green bay packers", "pack", "cheeseheads"),
    "HOU": ("texans", "houston", "houston texans"),
    "IND": ("colts", "indianapolis", "indy", "indianapolis colts"),
    "JAX": ("jaguars", "jacksonville", "jags", "jacksonville jaguars"),
    "KC": ("chiefs", "kansas city", "kansas city chiefs"),
    "LA": ("rams", "los angeles rams", "la rams"),
    "LAC": ("chargers", "los angeles chargers", "la chargers", "bolts"),
    "LV": ("raiders", "las vegas", "las vegas raiders", "oakland", "raider nation"),
    "MIA": ("dolphins", "miami", "miami dolphins", "fins", "phins"),
    "MIN": ("vikings", "minnesota", "minnesota vikings", "vikes", "skol"),
    "NE": ("patriots", "new england", "new england patriots", "pats"),
    "NO": ("saints", "new orleans", "new orleans saints", "nola", "who dat"),
    "NYG": ("giants", "new york giants", "gmen", "g men", "big blue"),
    "NYJ": ("jets", "new york jets", "gang green"),
    "PHI": ("eagles", "philadelphia", "philadelphia eagles", "philly", "birds"),
    "PIT": ("steelers", "pittsburgh", "pittsburgh steelers", "steel curtain"),
    "SF": ("49ers", "niners", "san francisco", "san francisco 49ers", "forty niners"),
    "SEA": ("seahawks", "seattle", "seattle seahawks", "hawks", "legion of boom"),
    "TB": ("buccaneers", "bucs", "buccs", "tampa bay", "tampa", "tampa bay buccaneers"),
    "TEN": ("titans", "tennessee", "tennessee titans"),
    "WAS": ("commanders", "washington", "washington commanders", "skins",
            "commies", "football team", "wft"),
}


def _build_nickname_map() -> dict[str, str]:
    """normalized nickname (spaced AND joined) -> canonical board abbr.

    Seeds every abbr as its own key, folds every nickname above, and folds
    ``base.TEAM_ALIASES`` (LAR->LA, WSH->WAS, OAK->LV, SD->LAC, JAC->JAX, ...) so a
    stale/variant abbr resolves too (recon §2: seed from base.TEAM_ALIASES).
    """
    out: dict[str, str] = {}

    def add(key: str, abbr: str) -> None:
        norm = normalize_query(key)
        if norm:
            out.setdefault(norm, abbr)
            out.setdefault(norm.replace(" ", ""), abbr)

    for abbr, nicks in _TEAM_NICKS.items():
        add(abbr, abbr)                     # the abbr itself ("sf" -> SF)
        for nick in nicks:
            add(nick, abbr)
    for alias, canon in base.TEAM_ALIASES.items():
        # canon is the schedules abbr; keep only ones we actually carry as a team.
        add(alias, base.TEAM_ALIASES.get(canon, canon))
    return out


# Famous initialism / nickname aliases -> the NORMALIZED full name they expand to.
# Real NFL players (public figures, the system's whole domain) — never colleague
# names (Rule 5). The resolver scores the expansion against the board so a curated
# alias resolves exactly like typing the player's name. Computed initials already
# cover the plain cases (e.g. "mhj" for Marvin Harrison Jr via the suffix-inclusive
# initials tier); this map is for the NON-obvious nicknames. Grows from rehearsal.
_ALIASES: dict[str, str] = {
    "cmc": "christian mccaffrey",
    "cmac": "christian mccaffrey",
    "jt": "jonathan taylor",
    "dk": "dk metcalf",
    "mhj": "marvin harrison jr",
    "jj": "justin jefferson",
    "jjettas": "justin jefferson",
    "dhop": "deandre hopkins",
    "nuk": "deandre hopkins",
    "hollywood": "marquise brown",
    "saquan": "saquon barkley",
    "ceedee": "ceedee lamb",
    "cd": "ceedee lamb",
    "bijan": "bijan robinson",
    "jsn": "jaxon smith njigba",
    "arsb": "amon ra st brown",
    "str": "amon ra st brown",
    "nacua": "puka nacua",
    "chubb": "nick chubb",
    "etn": "travis etienne",
    "gibbs": "jahmyr gibbs",
    "pacheco": "isiah pacheco",
    "waddle": "jaylen waddle",
    "aj": "aj brown",
    "dj moore": "dj moore",
}

# Public, test-facing views of the curated maps (data tests read these directly
# rather than reaching for real names on a synthetic board — Rule 5).
NICKNAME_TO_ABBR: dict[str, str]  # filled after normalize_query is defined
FAMOUS_ALIASES: dict[str, str] = dict(_ALIASES)


# --------------------------------------------------------------- normalization


def normalize_query(raw: str) -> str:
    """Fold a raw name/query to the resolver's canonical form (recon §2).

    Strips accents, lowercases, drops apostrophes/periods (so ``A.J.`` -> ``aj``,
    ``Ja'Marr`` -> ``jamarr``), turns hyphens/slashes/other punctuation into
    spaces, and folds every defense marker (``D/ST``, ``def``, ``defense``,
    ``d s t``) to a single ``dst`` token so ``SF D/ST`` and ``niners defense`` line
    up. Returns "" for an empty/whitespace/punctuation-only input (the MUST-1 empty
    guard depends on this).
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKD", raw)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[.'`’‘]", "", s)          # drop apostrophes/periods (join initials)
    s = re.sub(r"[^a-z0-9]+", " ", s)                # every other separator -> space
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    # Fold defense markers to one 'dst' token (order matters: after punct->space,
    # so "d/st" is now "d st"). Bare "ds" is deliberately NOT folded — it collides
    # with real initials (e.g. Deebo Samuel).
    s = re.sub(r"\bd s t\b", "dst", s)
    s = re.sub(r"\bd st\b", "dst", s)
    s = re.sub(r"\b(def|defense|defence)\b", "dst", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


NICKNAME_TO_ABBR = _build_nickname_map()


# ------------------------------------------------------------------- index


@dataclass(frozen=True)
class _Indexed:
    """One board entry with its name decomposed for scoring (built once)."""

    entry: BoardEntry
    norm: str
    tokens: tuple[str, ...]
    token_set: frozenset[str]
    core_join: str
    first: str
    last: str
    initials_stripped: str
    initials_full: str
    fi_last: str
    is_dst: bool
    team: str | None


# --------------------------------------------------------------- resolution


@dataclass(frozen=True)
class Resolution:
    """The resolver's verdict for one query (recon §2 disambiguation UX).

    ``kind``:
      * ``"empty"``   — the query was empty/whitespace; NEVER a match (MUST 1).
      * ``"auto"``    — a confident single hit; ``candidates`` has exactly one.
      * ``"confirm"`` — plausible but not certain; ``candidates`` has up to 3, the
                        first being the rank-tiebroken best (a confirm, not a
                        re-search), always including the best near-match (MUST 2).
      * ``"none"``    — nothing matched; ``candidates`` is empty.
    """

    kind: str
    candidates: tuple[BoardEntry, ...]


class NameResolver:
    """Structured tiered fuzzy/alias resolver over an in-memory draft board.

    Built once per session over the whole board (recon: pay it once, ~1k named
    entries). Filters to ``name is not None`` (all None-named rows are deep scrubs
    with no draftable identity). ``resolve`` is pure and re-runs per Enter (not per
    keystroke), skipping already-drafted ``taken`` ids so gone players never match.
    """

    def __init__(self, board: Sequence[BoardEntry]) -> None:
        self._index: list[_Indexed] = []
        for e in board:
            if e.name is None:
                continue
            norm = normalize_query(e.name)
            if not norm:
                continue
            tokens = tuple(norm.split())
            core = tuple(t for t in tokens if t not in _SUFFIXES) or tokens
            is_dst = _is_dst_position(e.position)
            self._index.append(
                _Indexed(
                    entry=e,
                    norm=norm,
                    tokens=tokens,
                    token_set=frozenset(tokens),
                    core_join="".join(core),
                    first=core[0],
                    last=core[-1],
                    initials_stripped="".join(t[0] for t in core),
                    initials_full="".join(t[0] for t in tokens),
                    fi_last=(core[0][0] + core[-1]) if core[0] and core[-1] else "",
                    is_dst=is_dst,
                    team=(e.team.upper() if e.team else None),
                )
            )

    # -- public seam -------------------------------------------------------

    def resolve(
        self, query: str, *, taken: AbstractSet[str] = frozenset()
    ) -> Resolution:
        """Resolve ``query`` against the live board, skipping ``taken`` ids."""
        q = normalize_query(query)
        if not q:                                   # MUST 1: empty/whitespace guard
            return Resolution("empty", ())
        qt = q.split()

        alias_exp = _ALIASES.get(q)
        alias_qt = alias_exp.split() if alias_exp else None

        scored: list[tuple[float, BoardEntry]] = []
        struct_by_id: dict[str, float] = {}     # raw (pre-alias) score, per candidate
        alias_rivals: list[BoardEntry] = []     # F2: top-100 initials rivals to an alias
        dst_colliders: list[BoardEntry] = []    # F3/NEW-1: players colliding with a DST
        for ic in self._index:
            if ic.entry.player_id in taken:
                continue
            unranked = ic.entry.espn_overall_rank >= _UNRANKED_RANK
            s_struct = _score_dst(q, qt, ic) if ic.is_dst else _score_person(q, qt, ic)

            # F2: a genuinely elite (rank <= 100) player matching the RAW query at
            # the initials tier or better is a rival an alias AUTO must not bury.
            if (
                alias_exp is not None
                and not ic.is_dst
                and s_struct >= _ALIAS_RIVAL_TIER
                and ic.entry.espn_overall_rank <= _ALIAS_RIVAL_RANK
            ):
                alias_rivals.append(ic.entry)

            s = s_struct
            if alias_exp is not None:
                sa = (
                    _score_dst(alias_exp, alias_qt, ic)
                    if ic.is_dst
                    else _score_person(alias_exp, alias_qt, ic)
                )
                if sa >= _T_CORE_JOIN:              # a strong expansion hit is curated
                    sa = max(sa, _T_DST_BARE)       # promote into the auto-eligible band
                s = max(s, sa)

            # F5: an unranked deep scrub never outranks a ranked player — cap its
            # score below the name tiers (applied to the raw score too, so a scrub
            # never counts as a DST collider or as "the elite").
            if unranked:
                s = min(s, _UNRANKED_CAP)
                s_struct = min(s_struct, _UNRANKED_CAP)

            # F3/NEW-1: a real player scoring at first-name tier or better collides
            # with a DST abbr/nickname/city query and must not be silently buried.
            if not ic.is_dst and s_struct >= _DST_COLLIDE_TIER:
                dst_colliders.append(ic.entry)

            if s >= _NONE_FLOOR:
                scored.append((s, ic.entry))
                struct_by_id[ic.entry.player_id] = s_struct

        if not scored:
            return Resolution("none", ())

        scored.sort(key=lambda t: (-t[0], t[1].espn_overall_rank, t[1].player_id))
        best = scored[0][0]
        second = scored[1][0] if len(scored) > 1 else float("-inf")
        unique_top = sum(1 for s, _ in scored if s >= best - 1e-9) == 1

        auto = (
            (best >= _T_EXACT - 1e-9 and unique_top)          # unique exact
            or (best >= _T_DST_BARE - 1e-9 and unique_top)    # unique curated/DST
            or (best >= _AUTO_TIER and (best - second) >= _AUTO_GAP)  # dominant top-tier
        )
        if auto:
            top = scored[0][1]
            # F3/NEW-1: a DST-tier AUTO that collides with a real player -> CONFIRM,
            # the D/ST shown first and the colliding player(s) alongside it.
            if _is_dst_position(top.position) and dst_colliders:
                return Resolution("confirm", self._panel(top, dst_colliders))
            # F2: an alias-DRIVEN AUTO (the alias, not structure, put this player on
            # top) over a genuine top-100 initials rival -> CONFIRM, alias target
            # shown first and the rival(s) alongside it.
            if (
                alias_exp is not None
                and struct_by_id.get(top.player_id, 0.0) < best
            ):
                rivals = [e for e in alias_rivals if e.player_id != top.player_id]
                if rivals:
                    return Resolution("confirm", self._panel(top, rivals))
            return Resolution("auto", (top,))
        return Resolution("confirm", self._confirm(scored))

    def suggest(
        self, query: str, *, taken: AbstractSet[str] = frozenset(), limit: int = 8
    ) -> tuple[BoardEntry, ...]:
        """Ranked top-``limit`` candidates for a PARTIAL query (live autocomplete).

        The per-keystroke seam the Checkpoint-2 web cockpit renders while the
        operator types (rehearsal 2 evidence, 2026-07-24: burst pick entry needs
        matches visible BEFORE Enter). Same tier scorers and F5 unranked cap as
        ``resolve`` — one matching implementation — but no auto/confirm verdict:
        the operator's click/Enter on a VISIBLE name is the confirmation, so the
        panel semantics (MUST 2/3) don't apply. Empty queries suggest nothing
        (MUST 1). Ordering: score desc, then ESPN rank, then player_id — the
        same total order ``resolve`` uses, so the two surfaces never disagree
        about who the best match is."""
        q = normalize_query(query)
        if not q:
            return ()
        qt = q.split()
        alias_exp = _ALIASES.get(q)
        alias_qt = alias_exp.split() if alias_exp else None
        scored: list[tuple[float, BoardEntry]] = []
        for ic in self._index:
            if ic.entry.player_id in taken:
                continue
            s = _score_dst(q, qt, ic) if ic.is_dst else _score_person(q, qt, ic)
            if alias_exp is not None:
                sa = (
                    _score_dst(alias_exp, alias_qt, ic)
                    if ic.is_dst
                    else _score_person(alias_exp, alias_qt, ic)
                )
                s = max(s, sa)
            if ic.entry.espn_overall_rank >= _UNRANKED_RANK:
                s = min(s, _UNRANKED_CAP)
            if s >= _NONE_FLOOR:
                scored.append((s, ic.entry))
        scored.sort(key=lambda t: (-t[0], t[1].espn_overall_rank, t[1].player_id))
        top = scored[: max(1, limit)]
        # MUST-2 parity (audit 2026-07-24 finding 3): the best rank-weighted
        # near-match across tiers is never dropped off the visible list, so an
        # elite's typo/truncation can't hide behind a page of deeper matches.
        if len(scored) > len(top):
            elite: tuple[float, BoardEntry] | None = None
            for s, e in scored:
                if s < _ELITE_FLOOR:
                    continue
                if elite is None or (e.espn_overall_rank, e.player_id) < (
                    elite[1].espn_overall_rank, elite[1].player_id
                ):
                    elite = (s, e)
            if elite is not None and elite[1].player_id not in {
                e.player_id for _, e in top
            }:
                top = top[:-1] + [elite]
        return tuple(e for _, e in top)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _panel(
        first: BoardEntry, others: Sequence[BoardEntry]
    ) -> tuple[BoardEntry, ...]:
        """A confirm panel with ``first`` pinned as candidate 1 (Rule 6 legibility).

        Used by the F2 (alias-vs-rival) and F3 (DST-vs-player) demotions: the
        intended target the operator most likely meant leads, the colliding rival(s)
        follow ranked most-draftable first, capped at 3 with ``first`` never dropped.
        """
        ordered = sorted(others, key=lambda e: (e.espn_overall_rank, e.player_id))
        panel = [first]
        seen = {first.player_id}
        for e in ordered:
            if e.player_id not in seen:
                panel.append(e)
                seen.add(e.player_id)
        return tuple(panel[:3])

    @staticmethod
    def _confirm(scored: list[tuple[float, BoardEntry]]) -> tuple[BoardEntry, ...]:
        """Top-3 confirm list with the MUST-2 elite-safety guarantee.

        The best rank-weighted near-match (lowest ``espn_overall_rank`` among ANY
        candidate at/above ``_ELITE_FLOOR`` — spanning the typo/subsequence/prefix
        tiers, not just name-component hits) is force-kept, so an elite player is
        never buried behind three deep same-token collisions even when the query is
        a surname typo/truncation of that star (refuter F1).
        """
        top = scored[:3]
        if len(scored) > 3:
            elite: tuple[float, BoardEntry] | None = None
            for s, e in scored:
                if s < _ELITE_FLOOR:
                    continue
                if elite is None or (e.espn_overall_rank, e.player_id) < (
                    elite[1].espn_overall_rank,
                    elite[1].player_id,
                ):
                    elite = (s, e)
            if elite is not None and elite[1].player_id not in {
                e.player_id for _, e in top
            }:
                top = scored[:2] + [elite]           # keep top-2 by score + the elite
                top.sort(
                    key=lambda t: (-t[0], t[1].espn_overall_rank, t[1].player_id)
                )
        return tuple(e for _, e in top[:3])


# --------------------------------------------------------------- scorers


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _score_person(q: str, qt: list[str], ic: _Indexed) -> float:
    """Best structural-tier score of query ``q`` (tokens ``qt``) against a person.

    The MAX over the recon §2 tiers; ``difflib`` is only reached when no structural
    tier beats its cap, so a keystroke never pays an edit-distance kernel it can't
    use.
    """
    if q == ic.norm:
        return _T_EXACT
    qj = "".join(qt)
    best = 0.0

    if qj and qj == ic.core_join:
        best = _T_CORE_JOIN
    if best < _T_INITIALS and qj and qj in (ic.initials_stripped, ic.initials_full):
        best = _T_INITIALS
    if best < _T_FI_LAST and ic.fi_last:
        fi_last_hit = qj == ic.fi_last or (
            len(qt) >= 2 and qt[0] and ic.first and qt[0][0] == ic.first[0] and qt[-1] == ic.last
        )
        if fi_last_hit:
            best = _T_FI_LAST
    if best < _T_LAST and ic.last and qt and qt[-1] == ic.last:
        best = _T_LAST
    if best < _T_FIRST and qt:
        if ic.first and qt[0] == ic.first:
            best = _T_FIRST
        elif len(qt) == 1 and qt[0] in ic.token_set:
            best = _T_FIRST
    if best < _T_LAST_PREFIX and len(qj) >= 2 and ic.last.startswith(qj):
        best = _T_LAST_PREFIX
    if best < _T_CORE_PREFIX and len(qj) >= 3 and ic.core_join.startswith(qj):
        best = _T_CORE_PREFIX
    if best < _T_FIRST_PREFIX and len(qj) >= 2 and ic.first.startswith(qj):
        best = _T_FIRST_PREFIX
    if best < _T_SUBSEQ and len(qj) >= 3 and _is_subsequence(qj, ic.core_join):
        best = _T_SUBSEQ
    if best < _T_DIFFLIB_CAP and len(qj) >= 3:
        ratio = max(
            SequenceMatcher(None, qj, ic.core_join).ratio(),
            SequenceMatcher(None, q, ic.norm).ratio(),
            SequenceMatcher(None, qj, ic.last).ratio(),
        )
        if ratio >= _DIFFLIB_MIN_RATIO:
            best = max(best, _T_DIFFLIB_CAP * ratio)
    return best


def _score_dst(q: str, qt: list[str], ic: _Indexed) -> float:
    """Score a DST entry: exact full name, or a team-nickname/abbr match.

    Person tiers are deliberately NOT applied to a defense (its "surname" is the
    literal token ``dst``, which would otherwise match every defense). A bare team
    nickname/abbr resolves to the defense (990); an explicit defense marker bumps
    it (995).
    """
    if q == ic.norm:
        return _T_EXACT
    if not ic.team:
        return 0.0
    has_marker = "dst" in qt
    parts = [t for t in qt if t != "dst"]
    if not parts:
        return 0.0
    for key in (" ".join(parts), "".join(parts)):
        abbr = NICKNAME_TO_ABBR.get(key)
        if abbr and abbr == ic.team:
            return _T_DST_MARKED if has_marker else _T_DST_BARE
    return 0.0
