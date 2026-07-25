"""Global static draft valuation — VOR / VBD core + value view (item 2.1).

Re-scores the weekly Sleeper projections through ``scoring.py`` (per-week-then-
sum — design D1), computes per-position replacement levels from the exact
10-team / 9-starter roster (empirical flex allocation — D4), and produces a
ranked global VALUE board (VOR). The value view (``build_value_view``) diffs the
scarcity-priced house board against a live ESPN default-board snapshot — "what
the room can't see" (D11).

Standing rules honored here:
  * Rule 1 (as-of): ``build_valuation`` only THREADS ``as_of``/``view`` into
    ``get_projections`` (keyword-only, ``historical`` default, leakage-tested);
    the aggregation layer cannot widen the gate.
  * Rule 2 (scoring): NO scoring constant lives here. Every point is priced
    through ``scoring.score`` and positions are canonicalized via scoring's own
    frozensets. Reasons that quote a per-unit weight read it off ``ScoringRules``
    (still the single source), never a re-hardcoded literal.
  * Rule 6 (explainability): every ``ValuationRow`` ships a legible ``reasons``
    tuple (season pts / weeks, replacement rank+points, dominant scoring driver;
    K/DST flagged low-confidence order).

The dynamic pick engine (board state, survival, opponent need) is item 2.3.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ziggurat.core import divergence, scoring
from ziggurat.data.nfl import base, projections

# Canonical league positions. DEF/D/ST -> DST, PK -> K, resolved via scoring's
# own frozensets (rule 2 — no re-hardcoded position set beyond what scoring
# exposes). K and DST stay on the board (D5), flagged low-confidence order.
CANONICAL_POSITIONS = ("QB", "RB", "WR", "TE", "DST", "K")
_LOW_CONFIDENCE_POSITIONS = frozenset({"K", "DST"})

_DEFAULT_STARTERS = MappingProxyType({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1})

# Default regular-season week window: NFL weeks 1..17. Week 18 (the rest week) is
# excluded — this league's fantasy regular season is 14 weeks + playoffs, and
# week-18 starter production is noise for a draft board. Tunable via ``weeks``.
DEFAULT_WEEKS = range(1, 18)


@dataclass(frozen=True)
class RosterStructure:
    """The exact league roster shape (locked spike 1.1), tunable for calibration.

    ``starters`` are DEDICATED (non-flex) slots per team; ``flex_slots`` are the
    additional RB/WR/TE slots pooled empirically across ``flex_positions``.

    ``bench_slots`` / ``ir_slots`` were added by item 3.2 (§7.7): the real roster
    is **16 active slots + 1 IR slot**, not "≤16", and ESPN blocks every
    transaction while a roster is illegal — so the waiver module (3.4) needs the
    active-slot count and the IR allowance to be stated somewhere. Both are
    additive with defaults, so no 2.1/2.2/2.3 behaviour changes.
    """

    teams: int = 10
    starters: Mapping[str, int] = _DEFAULT_STARTERS
    flex_slots: int = 1
    flex_positions: frozenset[str] = frozenset({"RB", "WR", "TE"})
    bench_slots: int = 7
    ir_slots: int = 1

    @property
    def starting_slots(self) -> int:
        """Slots that score in a given week (9 here: 7 dedicated + 1 flex + ...)."""
        return sum(self.starters.values()) + self.flex_slots

    @property
    def active_slots(self) -> int:
        """Roster slots that count against legality (starters + flex + bench).

        IR is deliberately NOT counted — an IR occupant is off the active roster.
        """
        return self.starting_slots + self.bench_slots


DEFAULT_ROSTER = RosterStructure()


@dataclass(frozen=True)
class WeeklyLine:
    """One player's per-week house points plus the identity spine (item 3.2).

    ``points`` is week -> house points for the weeks that HAVE a projection row.
    A missing week means "no row" — which for a skill player's bye is a row full
    of NULLs scoring 0.0, and for a D/ST bye is no row at all (item 3.2 §2.5).
    Consumers must treat "missing" as 0.0 points and derive bye/availability
    SEPARATELY, never from the points value.

    ``played_weeks`` is the weeks whose row carried an OPPONENT — i.e. the weeks
    the feed actually forecast a game for this player. It is NOT the same as
    ``points.keys()`` and the difference is load-bearing: the feed publishes
    bye-SHAPED rows (team set, opponent NULL, every stat NULL, scoring 0.0) both
    for a real bye AND for a player it simply has no forecast for. A player with
    more than one such week in a span has MISSING COVERAGE, not a bye — measured
    on the live 2026 feed, A.J. Brown (99.3% owned) carries a real week-1 line and
    16 bye-shaped weeks, which scored him as near-worthless. Consumers must check
    coverage against ``played_weeks``, never against the point sum.

    ``retrieved_as_of`` carries the snapshot day(s) the points came from — the
    staleness banner's input (a July projection pricing a November decision is
    Rule-1-invisible: that snapshot really is the newest thing <= as_of).
    """

    key: tuple
    position: str                       # canonical QB/RB/WR/TE/DST/K
    gsis_id: str | None
    source_player_id: str | None
    espn_id: str | None
    team: str | None
    player: str | None
    points: Mapping[int, float]
    totals: Mapping[str, float]         # reason-string drivers (receptions, ...)
    retrieved_as_of: frozenset[str]
    played_weeks: frozenset[int] = frozenset()

    @property
    def season_points(self) -> float:
        return sum(self.points.values())


@dataclass(frozen=True)
class ValuationRow:
    gsis_id: str | None
    espn_id: str | None          # resolved via base.espn_by_gsis (skill); None for DST
    team: str | None
    player: str | None
    position: str                # CANONICAL QB/RB/WR/TE/DST/K
    season: int
    weeks_counted: int
    proj_points: float           # house season total (Σ per-week score) — D1
    replacement_points: float
    vor: float
    pos_rank: int                # 1-based within position by vor desc
    overall_rank: int            # 1-based across all by vor desc
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ValueDivergenceRow:
    """One house-VALUE-vs-ESPN-board comparison (the value view).

    ``pos_rank_delta = espn_pos_rank - house_pos_rank`` (rank numbers: lower is
    better). Positive => the house values the player MORE than ESPN's default
    board (flag ``MARKET_HIGHER``, reusing divergence's flag where "market" is
    the house VOR board); negative => ``ESPN_HIGHER``.
    """

    player: str | None
    position: str
    team: str | None
    espn_id: str | None
    vor: float
    house_pos_rank: int
    house_overall_rank: int
    espn_pos_rank: int | None            # editorial board (primary signal)
    espn_adp_pos_rank: int | None        # native ADP (secondary, D9)
    pos_rank_delta: int | None           # espn_pos_rank - house_pos_rank
    flag: str
    reasons: tuple[str, ...]


# --------------------------------------------------------------- position canon


def canon_position(pos) -> str | None:
    """Canonical QB/RB/WR/TE/DST/K, or None for a non-league position.

    Uses scoring.py's frozensets (rule 2): DEF/D/ST -> DST, PK -> K.
    """
    if pos is None:
        return None
    p = str(pos).strip().upper()
    if p in scoring.OFFENSE_POSITIONS:
        return p
    if p in scoring.DST_POSITIONS:
        return "DST"
    if p in scoring.KICKER_POSITIONS:
        return "K"
    return None


# Item 3.2 promoted this to the public surface (core/marginal.py and the CLI both
# canonicalize positions). The private name stays as an alias so no 2.x caller
# breaks.
_canon_position = canon_position


def _num(value) -> float:
    """Local, scoring-free numeric coercion for reason breakdowns (None -> 0)."""
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v != v else v  # NaN -> 0


# ---------------------------------------------------------------- replacement


def replacement_levels(
    by_pos: Mapping[str, list[float]],
    roster: RosterStructure,
    *,
    denoise_kdst: bool = True,
) -> tuple[dict[str, float], dict[str, int]]:
    """Per-position replacement points + started counts (design §3).

    ``by_pos`` maps a canonical position to its season points DESC. Flex is
    allocated EMPIRICALLY: pool every flex-eligible player ranked BEYOND its
    dedicated starters, take the best ``teams * flex_slots``, credit each back to
    its position. Replacement = FIRST NON-STARTER (index = started count), with a
    thin-board clamp. ``denoise_kdst`` averages a small rank window around the
    baseline for K and D/ST (D5) — their season spread is tiny vs weekly variance.

    Superflex guard: ``flex_positions`` excludes QB, so no QB enters the pool and
    ``started['QB']`` stays ``teams * starters['QB']``.
    """
    started = {pos: roster.teams * roster.starters[pos] for pos in roster.starters}

    # Empirical flex allocation over leftovers (ranked beyond dedicated starters).
    flex_total = roster.teams * roster.flex_slots
    pool: list[tuple[float, str]] = []
    for pos in roster.flex_positions:
        for pts in by_pos.get(pos, [])[started.get(pos, 0):]:
            pool.append((pts, pos))
    pool.sort(reverse=True)  # best leftovers first
    for _pts, pos in pool[:flex_total]:
        started[pos] += 1

    replacement: dict[str, float] = {}
    for pos, pts_list in by_pos.items():
        idx = started.get(pos, 0)  # 0-based -> first non-starter
        if not pts_list:
            replacement[pos] = 0.0
        elif denoise_kdst and pos in _LOW_CONFIDENCE_POSITIONS:
            lo = max(0, idx - 1)
            window = pts_list[lo:idx + 2]
            replacement[pos] = sum(window) / len(window) if window else pts_list[-1]
        elif idx < len(pts_list):
            replacement[pos] = pts_list[idx]
        else:
            replacement[pos] = pts_list[-1]  # thin-board clamp
    return replacement, started


# ------------------------------------------------------------------- valuation


def _resolve_names(conn) -> dict[str, str]:
    """gsis_id -> player name from the latest players snapshot (best-effort).

    Mirrors the crosswalk read pattern; used only to make ``reasons``/rows
    legible (rule 6). An empty players table simply yields no names.
    """
    out: dict[str, str] = {}
    for r in conn.execute(
        """
        SELECT gsis_id, name FROM players p
        WHERE name IS NOT NULL AND retrieved_as_of = (
            SELECT MAX(retrieved_as_of) FROM players p2 WHERE p2.gsis_id = p.gsis_id
        )
        """
    ):
        out.setdefault(r["gsis_id"], r["name"])
    return out


def _driver_reason(canon: str, totals: dict, rules: scoring.ScoringRules) -> str:
    """The dominant scoring-driver line (rule 6). Reads per-unit weights off
    ``rules`` (the single scoring source), never a re-hardcoded literal."""
    if canon in scoring.OFFENSE_POSITIONS:
        rec = totals.get("receptions", 0.0)
        rec_pts = rec * rules.points_per_reception
        return f"PPR rec +{rec_pts:.0f} pts ({rec:.0f} catches)"
    if canon == "DST":
        return f"D/ST allowed-brackets net {totals.get('bracket_pts', 0.0):+.0f} pts"
    if canon == "K":
        return f"kicking {totals.get('kick_pts', 0.0):.0f} pts (distance-tiered)"
    return ""


def weekly_lines(
    conn,
    *,
    as_of,
    season,
    weeks: Iterable[int] | None = None,
    source: str = "sleeper_rotowire",
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    view: base.AsOfView = "historical",
) -> dict[tuple, WeeklyLine]:
    """The ONE identity spine: player key -> per-week house points + identity.

    Extracted by item 3.2 so the season board (``build_valuation``) and the
    in-season marginal board (``core/marginal.py``) cannot drift onto two
    different spines — a player present on one board and absent from the other is
    an invisible failure.

    Rule 1: ``as_of``/``view`` are keyword-only and THREAD STRAIGHT INTO
    ``get_projections``; this layer never widens the gate. Rule 2: every point is
    priced through ``scoring.score`` on that week's OWN row (per-week-then-sum —
    the non-linear D/ST brackets make sum-then-score wrong).

    Grouping: D/ST by ``TEAM_ALIASES``-normalized team (league state normalizes
    LAR->LA while the projection feed stores LAR verbatim; joining raw loses the
    Rams), skill by ``gsis_id``, falling back to ``source_player_id`` so two
    uncrosswalked rookies never merge.
    """
    week_set = set(DEFAULT_WEEKS if weeks is None else weeks)
    rows = projections.get_projections(
        conn, as_of=as_of, season=season, source=source, view=view
    )
    names = _resolve_names(conn)
    espn_ids = base.espn_by_gsis(conn)

    acc: dict[tuple, dict] = {}
    for r in rows:
        st = r["season_type"]
        if st is None or str(st).strip().lower() != "regular":
            continue
        week = r["week"]
        if week not in week_set:
            continue
        raw_pos = r["position"]
        canon = canon_position(raw_pos)
        if canon is None:
            continue

        team = base.TEAM_ALIASES.get(
            str(r["team"]).strip().upper(), str(r["team"]).strip().upper()
        ) if r["team"] is not None else None

        if canon == "DST":
            key = ("DST", team)
        elif r["gsis_id"] is not None:
            key = ("SKILL", r["gsis_id"])
        else:
            key = ("SPID", r["source_player_id"])

        a = acc.get(key)
        if a is None:
            a = {
                "canon": canon,
                "gsis_id": r["gsis_id"],
                "source_player_id": r["source_player_id"],
                "team": team,
                "points": {},
                "retrieved": set(),
                "played": set(),
                "totals": {"receptions": 0.0, "bracket_pts": 0.0, "kick_pts": 0.0},
            }
            acc[key] = a

        stat = dict(r)
        pts = scoring.score(raw_pos, stat, rules)
        a["points"][week] = a["points"].get(week, 0.0) + pts
        a["retrieved"].add(r["retrieved_as_of"])
        opp = r["opponent"]
        if opp is not None and str(opp).strip():
            a["played"].add(week)
        if canon in scoring.OFFENSE_POSITIONS:
            a["totals"]["receptions"] += _num(r["receptions"])
        elif canon == "DST":
            # bracket contribution = full - events (public API only, no constants).
            no_brackets = dict(stat)
            no_brackets["points_allowed"] = None
            no_brackets["yards_allowed"] = None
            a["totals"]["bracket_pts"] += pts - scoring.score(raw_pos, no_brackets, rules)
        elif canon == "K":
            a["totals"]["kick_pts"] += pts
        if a["team"] is None and team is not None:
            a["team"] = team

    out: dict[tuple, WeeklyLine] = {}
    for key, a in acc.items():
        gsis = a["gsis_id"]
        canon = a["canon"]
        if canon == "DST":
            player = f"{a['team']} D/ST" if a["team"] else "D/ST"
        else:
            player = names.get(gsis)
        out[key] = WeeklyLine(
            key=key,
            position=canon,
            gsis_id=gsis,
            source_player_id=a["source_player_id"],
            espn_id=None if canon == "DST" else espn_ids.get(gsis),
            team=a["team"],
            player=player,
            points=a["points"],
            totals=a["totals"],
            retrieved_as_of=frozenset(x for x in a["retrieved"] if x is not None),
            played_weeks=frozenset(a["played"]),
        )
    return out


def weekly_points(
    conn,
    *,
    as_of,
    season,
    weeks: Iterable[int] | None = None,
    source: str = "sleeper_rotowire",
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    view: base.AsOfView = "historical",
) -> dict[tuple, dict[int, float]]:
    """``{player_key: {week: house points}}`` — the design's stated §7.4 surface,
    a thin projection of :func:`weekly_lines` for callers that need only points."""
    return {
        key: dict(line.points)
        for key, line in weekly_lines(
            conn, as_of=as_of, season=season, weeks=weeks, source=source,
            rules=rules, view=view,
        ).items()
    }


def build_valuation(
    conn,
    *,
    as_of,
    season,
    weeks: Iterable[int] | None = None,
    source: str = "sleeper_rotowire",
    roster: RosterStructure = DEFAULT_ROSTER,
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    view: base.AsOfView = "historical",
    denoise_kdst: bool = True,
) -> list[ValuationRow]:
    """Global static VOR board, read at ``as_of`` (keyword-only; no implicit now).

    Reads ``get_projections(conn, as_of=as_of, season=season, source=source,
    view=view)`` (the as-of gate is enforced INSIDE the accessor), keeps
    ``season_type == 'regular'`` rows whose ``week`` is in ``weeks`` (default
    NFL weeks 1..17), scores EACH weekly row through ``scoring.score`` then SUMS
    (D1 — never sum stat lines then score once; the non-linear D/ST brackets make
    that wrong), groups skill by gsis_id (source_player_id fallback when the
    crosswalk is unresolved, so distinct rookies/DEF never merge) and DST by
    normalized team, then prices VOR off ``replacement_levels``.
    """
    lines = weekly_lines(
        conn, as_of=as_of, season=season, weeks=weeks, source=source,
        rules=rules, view=view,
    )
    if not lines:
        return []

    # by_pos: canonical position -> season points DESC (for replacement levels).
    by_pos: dict[str, list[float]] = {}
    for line in lines.values():
        by_pos.setdefault(line.position, []).append(line.season_points)
    for pts_list in by_pos.values():
        pts_list.sort(reverse=True)

    replacement, started = replacement_levels(by_pos, roster, denoise_kdst=denoise_kdst)

    rows_out: list[ValuationRow] = []
    for line in lines.values():
        canon = line.position
        proj = line.season_points
        repl = replacement.get(canon, 0.0)
        # rows-present, NOT games played (item 3.2 §2.5: a bye row is present with
        # NULL stats for skill players and absent entirely for D/ST). Never a
        # denominator.
        weeks_counted = len(line.points)

        baseline_rank = started.get(canon, 0) + 1  # first non-starter is (started+1)-th
        reasons = [
            f"{proj:.1f} house pts / {weeks_counted} wk",
            f"{canon} replacement {repl:.1f} ({canon}{baseline_rank})",
        ]
        driver = _driver_reason(canon, line.totals, rules)
        if driver:
            reasons.append(driver)
        if canon in _LOW_CONFIDENCE_POSITIONS:
            reasons.append("low-confidence order (small season spread)")

        rows_out.append(
            ValuationRow(
                gsis_id=line.gsis_id,
                espn_id=line.espn_id,
                team=line.team,
                player=line.player,
                position=canon,
                season=int(season),
                weeks_counted=weeks_counted,
                proj_points=proj,
                replacement_points=repl,
                vor=proj - repl,
                pos_rank=0,       # filled below
                overall_rank=0,   # filled below
                reasons=tuple(reasons),
            )
        )

    # Ranks by VOR desc: overall across all, pos_rank within position.
    rows_out.sort(key=lambda v: v.vor, reverse=True)
    pos_counter: dict[str, int] = {}
    ranked: list[ValuationRow] = []
    for i, v in enumerate(rows_out, start=1):
        pos_counter[v.position] = pos_counter.get(v.position, 0) + 1
        ranked.append(
            ValuationRow(
                **{**v.__dict__, "overall_rank": i, "pos_rank": pos_counter[v.position]}
            )
        )
    return ranked


# -------------------------------------------------------------------- value view

# Value-view flags. This report has NO market side (that is divergence.py's job);
# it diffs the HOUSE VOR board against ESPN's default board. So it ships its own
# labels rather than reusing divergence's MARKET_HIGHER/ESPN_HIGHER — a novice
# operator must never read "MARKET" in a house-vs-ESPN report (rule 6).
HOUSE_HIGHER = "HOUSE_HIGHER"  # house VOR values the player more than ESPN's board
ESPN_HIGHER = "ESPN_HIGHER"    # ESPN's board ranks the player better than the house
ALIGNED = "ALIGNED"            # same positional rank on both boards

VALUE_FLAGS = frozenset({HOUSE_HIGHER, ESPN_HIGHER, ALIGNED})


def build_value_view(
    valuation_rows: Iterable[ValuationRow],
    espn_rows: Iterable,
    *,
    use_signal: str = "editorial",
    min_vor: float | None = 0.0,
) -> list[ValueDivergenceRow]:
    """Diff the scarcity-priced house VOR board against the ESPN default board.

    ``espn_rows`` are PLAIN dicts / sqlite3.Row from the ESPN-side snapshot (the
    seam with the ESPN ingester), each carrying ``espn_id``, ``position``,
    ``team``, ``espn_pos_rank`` (editorial board — primary), ``espn_adp_pos_rank``
    (native ADP — secondary), ``overall_rank``, ``adp``. The join reuses
    divergence's private helpers (read-only): skill by ``espn_id``, DST by
    ``TEAM_ALIASES``-normalized team. ``use_signal='adp'`` diffs against ESPN's
    crowdsourced ADP positional rank instead of the editorial board.

    ``pos_rank_delta = espn_pos_rank - house_pos_rank`` (+ve => house values the
    player MORE than the ESPN board). Sorted by ``|pos_rank_delta|`` desc so the
    biggest "room can't see it" gaps lead.

    DRAFTABLE FILTER (``min_vor``, default 0.0): only house rows with ``vor >
    min_vor`` enter the report. Without it the house board (every projected
    player — e.g. ~1200 WRs tied at the replacement floor with proj≈0 and a
    meaningless tiebreak ``pos_rank``) is 3–4× DEEPER than ESPN's, so a raw
    ``|delta|`` sort floats undraftable practice-squad players to the top and
    buries the actual actionable edge (the K/D-ST house divergence, pass-catching
    RB gaps). Requiring the player to beat replacement makes the two boards
    comparable and keeps the report to real draft targets (rule 6). Pass
    ``min_vor=None`` to disable (e.g. to inspect fades below replacement).

    POSITION GUARD: a skill player is compared only when the house (Sleeper) and
    ESPN agree on canonical position; otherwise ``espn_pos_rank`` would be a rank
    from ESPN's OTHER position pool and the delta a meaningless cross-pool
    subtraction (with a factually wrong reason string). Such rows are skipped.
    """
    by_espn, by_dst_team = divergence._index_market(espn_rows)

    out: list[ValueDivergenceRow] = []
    for v in valuation_rows:
        if min_vor is not None and v.vor <= min_vor:
            continue  # below replacement — not a draft target; keeps noise out
        if v.position == "DST":
            e = by_dst_team.get(divergence._norm_team(v.team))
        else:
            e = by_espn.get(str(v.espn_id)) if v.espn_id is not None else None
            # Only compare when both boards agree on the player's position;
            # a Sleeper/ESPN position disagreement (fringe/gadget players) would
            # otherwise subtract ranks from two different position pools.
            if e is not None and canon_position(e.get("position")) != v.position:
                continue
        if e is None:
            continue  # unmatched: no ESPN board rank to compare against

        espn_adp_pos_rank = e.get("espn_adp_pos_rank")
        if use_signal == "adp":
            espn_pos_rank = espn_adp_pos_rank
        else:
            espn_pos_rank = divergence._espn_rank(e)  # editorial board rank

        if espn_pos_rank is None:
            continue  # cannot form a delta without the chosen ESPN rank
        espn_pos_rank = int(espn_pos_rank)
        delta = espn_pos_rank - v.pos_rank
        flag = HOUSE_HIGHER if delta > 0 else ESPN_HIGHER if delta < 0 else ALIGNED

        reasons = [
            f"house VOR {v.vor:.1f} ({v.position}{v.pos_rank})",
            f"ESPN {'ADP' if use_signal == 'adp' else 'board'} {v.position}{espn_pos_rank}",
            f"delta {delta:+d} => "
            + ("house values more" if delta > 0 else "ESPN values more" if delta < 0 else "aligned"),
        ]

        out.append(
            ValueDivergenceRow(
                player=v.player,
                position=v.position,
                team=v.team,
                espn_id=v.espn_id,
                vor=v.vor,
                house_pos_rank=v.pos_rank,
                house_overall_rank=v.overall_rank,
                espn_pos_rank=espn_pos_rank,
                espn_adp_pos_rank=int(espn_adp_pos_rank) if espn_adp_pos_rank is not None else None,
                pos_rank_delta=delta,
                flag=flag,
                reasons=tuple(reasons),
            )
        )

    out.sort(key=lambda r: abs(r.pos_rank_delta), reverse=True)
    return out


# ---------------------------------------------------------------------- display

_VAL_COLUMNS = (
    ("overall_rank", "#", 4),
    ("player", "player", 22),
    ("position", "pos", 4),
    ("pos_rank", "prnk", 5),
    ("team", "team", 5),
    ("proj_points", "proj", 8),
    ("replacement_points", "repl", 8),
    ("vor", "vor", 8),
)


def _fmt_cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def format_valuation(rows: Iterable[ValuationRow], *, top: int | None = None) -> str:
    """Render valuation rows as a fixed-width text table (display only)."""
    rows = list(rows)
    if top is not None:
        rows = rows[:top]
    header = "  ".join(f"{label:<{width}}" for _, label, width in _VAL_COLUMNS)
    lines = [header, "  ".join("-" * width for _, _, width in _VAL_COLUMNS)]
    for row in rows:
        cells = [f"{_fmt_cell(getattr(row, attr)):<{width}}" for attr, _, width in _VAL_COLUMNS]
        lines.append("  ".join(cells))
    return "\n".join(lines)


_VALUE_COLUMNS = (
    ("player", "player", 22),
    ("position", "pos", 4),
    ("team", "team", 5),
    ("vor", "vor", 8),
    ("house_pos_rank", "house", 6),
    ("espn_pos_rank", "espn", 5),
    ("pos_rank_delta", "delta", 6),
    ("flag", "flag", 14),
)


def format_value_view(rows: Iterable[ValueDivergenceRow], *, top: int | None = None) -> str:
    """Render value-view rows as a fixed-width text table (display only)."""
    rows = list(rows)
    if top is not None:
        rows = rows[:top]
    header = "  ".join(f"{label:<{width}}" for _, label, width in _VALUE_COLUMNS)
    lines = [header, "  ".join("-" * width for _, _, width in _VALUE_COLUMNS)]
    for row in rows:
        cells = [f"{_fmt_cell(getattr(row, attr)):<{width}}" for attr, _, width in _VALUE_COLUMNS]
        lines.append("  ".join(cells))
    return "\n".join(lines)
