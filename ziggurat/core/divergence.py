"""ESPN-vs-market divergence report — pure alignment logic (item 1.5, §6).

Where does the market rank a player *differently* from ESPN? A positive
divergence (market ranks a player higher than ESPN's own board) is a draft-time
buy signal; the inverse is a fade signal. This module is the pure alignment
layer: it takes the market side (FantasyPros ECR via ``get_adp_rankings``) and an
ESPN-side positional-rank snapshot and emits one comparison row per matched
player. The CLI only parses/calls/prints it (standing rule 3: no logic in cli/).

Join key (§6): ``espn_id`` for skill players (QB/RB/WR/TE/K), normalized team
abbr for DST (which carry no gsis/espn id). Team abbrs on both sides are pushed
through ``base.TEAM_ALIASES`` so a FantasyPros ``LAR``/``JAC`` lines up with a
schedules/ESPN ``LA``/``JAX``.

v1 ships the RAW positional-rank delta plus the market's own dispersion (``sd``,
``best``/``worst`` spread). Scarcity/VBD-weighted divergence — a 5-spot RB or TE
gap is worth more than a 5-spot WR gap — needs replacement-level valuation from
Phase 2.1, so ``position`` is surfaced for scarcity context only, never folded
into the delta yet.

The confidence gate uses the market's consensus dispersion: a rank delta no
larger than the market's ``sd`` for that player is reported as ``CONTESTED``
rather than a confident directional call; only a delta that clears the gate earns
``MARKET_HIGHER`` (market ranks the player better — lower rank number — than ESPN)
or ``ESPN_HIGHER``.

SCALE CAVEAT (v1, refined in Phase 2.1): ``sd``/``best``/``worst``/``spread`` come
from FantasyPros ``ecr_type='ro'`` and are measured on the OVERALL-rank scale,
while ``pos_rank_delta`` is a POSITIONAL-rank disagreement. The gate therefore
compares two different units — it is a coarse heuristic, NOT a calibrated
positional confidence, and its noise floor widens with draft depth (a deep
player's large overall ``sd`` swallows real positional deltas as CONTESTED). The
operator (a football novice — rule 6) must read ``CONTESTED``/directional as a
rough cue, not a precise call. A positional-scale gate (``ecr_type='rp'``
dispersion) and scarcity/VBD weighting land with replacement-level valuation in
Phase 2.1; the columns are labelled ``sd(ovr)``/``spread(ovr)`` to signal the scale.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ziggurat.core.scoring import DST_POSITIONS
from ziggurat.data.nfl.base import TEAM_ALIASES

# flag values
MARKET_HIGHER = "MARKET_HIGHER"  # market ranks the player better than ESPN
ESPN_HIGHER = "ESPN_HIGHER"      # ESPN ranks the player better than the market
CONTESTED = "CONTESTED"          # delta within the market's dispersion (noise)

FLAGS = frozenset({MARKET_HIGHER, ESPN_HIGHER, CONTESTED})


@dataclass(frozen=True)
class DivergenceRow:
    """One ESPN-vs-market comparison for a single player.

    ``pos_rank_delta = espn_pos_rank - market_pos_rank`` (rank numbers: lower is
    better). Positive => the market ranks the player better than ESPN does
    (MARKET_HIGHER once it clears the gate); negative => ESPN_HIGHER.
    """

    player: str | None
    position: str | None
    team: str | None
    espn_pos_rank: int | None
    market_pos_rank: int | None
    market_ecr: float | None
    pos_rank_delta: int | None
    market_sd: float | None
    best: int | None
    worst: int | None
    spread: int | None  # worst - best (overall-rank dispersion band)
    flag: str


def _norm_team(team) -> str | None:
    if team is None:
        return None
    team = str(team).strip().upper()
    return TEAM_ALIASES.get(team, team)


def _is_dst(position) -> bool:
    return bool(position) and str(position).strip().upper() in DST_POSITIONS


def _as_dict(row) -> dict:
    """Accept a sqlite3.Row / Mapping / plain dict uniformly."""
    if isinstance(row, Mapping):
        return dict(row)
    return dict(row)  # sqlite3.Row supports dict()


def _espn_rank(row: Mapping):
    """The ESPN positional rank, tolerating a few source spellings."""
    for key in ("espn_pos_rank", "posRank", "pos_rank", "positionalRanking"):
        if key in row and row[key] is not None:
            return row[key]
    return None


def _index_market(market_rows: Iterable) -> tuple[dict, dict]:
    """Build (by_espn_id, by_dst_team) lookup maps from market rows.

    Skill players key on their (string-coerced) espn_id; DST rows — which have no
    espn_id — key on their normalized team abbr. Later rows win on a key
    collision (the accessor already returns one version per player).
    """
    by_espn: dict[str, dict] = {}
    by_dst_team: dict[str, dict] = {}
    for raw in market_rows:
        m = _as_dict(raw)
        if _is_dst(m.get("position")):
            team = _norm_team(m.get("team"))
            if team is not None:
                by_dst_team[team] = m
        else:
            espn_id = m.get("espn_id")
            if espn_id is not None:
                by_espn[str(espn_id)] = m
    return by_espn, by_dst_team


def _classify(delta: int, market_sd, *, gate_multiplier: float) -> str:
    """Gate the delta against the market's consensus dispersion.

    A delta within ``gate_multiplier * market_sd`` of zero => CONTESTED. Missing
    sd falls back to a strict gate of 0 (any nonzero delta is directional). NOTE
    the scale caveat in the module docstring: ``market_sd`` is overall-rank
    dispersion while ``delta`` is a positional-rank gap — a coarse v1 heuristic,
    refined to a positional-scale gate in Phase 2.1.
    """
    threshold = (float(market_sd) * gate_multiplier) if market_sd is not None else 0.0
    if abs(delta) <= threshold:
        return CONTESTED
    return MARKET_HIGHER if delta > 0 else ESPN_HIGHER


def build_divergence(
    market_rows: Iterable,
    espn_rows: Iterable[Mapping],
    *,
    gate_multiplier: float = 1.0,
) -> list[DivergenceRow]:
    """Align the ESPN board against the market board (inner join on matched key).

    ``market_rows`` are ``get_adp_rankings`` rows (sqlite3.Row or dicts) carrying
    ``espn_id``/``team``/``position``/``pos_rank``/``ecr``/``sd``/``best``/``worst``.
    ``espn_rows`` are ESPN-side rank rows carrying ``position``, an ESPN
    positional rank (``espn_pos_rank``), and the join key — ``espn_id`` for skill
    players, ``team`` for DST — plus an optional ``player`` name.

    Only players present on BOTH boards produce a row (a divergence needs two
    ranks). Results are sorted by descending absolute delta (biggest
    disagreements first) so the report leads with its signal.
    """
    by_espn, by_dst_team = _index_market(market_rows)
    out: list[DivergenceRow] = []

    for raw in espn_rows:
        e = _as_dict(raw)
        position = e.get("position")
        if _is_dst(position):
            market = by_dst_team.get(_norm_team(e.get("team")))
        else:
            espn_id = e.get("espn_id")
            market = by_espn.get(str(espn_id)) if espn_id is not None else None
        if market is None:
            continue  # unmatched: no market rank to compare against

        espn_pos_rank = _espn_rank(e)
        market_pos_rank = market.get("pos_rank")
        if espn_pos_rank is None or market_pos_rank is None:
            continue  # cannot form a delta without both ranks

        espn_pos_rank = int(espn_pos_rank)
        market_pos_rank = int(market_pos_rank)
        delta = espn_pos_rank - market_pos_rank
        market_sd = market.get("sd")
        best = market.get("best")
        worst = market.get("worst")
        spread = (int(worst) - int(best)) if best is not None and worst is not None else None

        out.append(
            DivergenceRow(
                player=e.get("player") or market.get("player"),
                position=position or market.get("position"),
                team=_norm_team(e.get("team")) or _norm_team(market.get("team")),
                espn_pos_rank=espn_pos_rank,
                market_pos_rank=market_pos_rank,
                market_ecr=market.get("ecr"),
                pos_rank_delta=delta,
                market_sd=market_sd,
                best=int(best) if best is not None else None,
                worst=int(worst) if worst is not None else None,
                spread=spread,
                flag=_classify(delta, market_sd, gate_multiplier=gate_multiplier),
            )
        )

    out.sort(key=lambda r: abs(r.pos_rank_delta), reverse=True)
    return out


_COLUMNS = (
    ("player", "player", 22),
    ("position", "pos", 4),
    ("team", "team", 5),
    ("espn_pos_rank", "espn", 5),
    ("market_pos_rank", "mkt", 5),
    ("market_ecr", "ecr", 7),
    ("pos_rank_delta", "delta", 6),
    ("market_sd", "sd(ovr)", 8),
    ("best", "best", 5),
    ("worst", "worst", 6),
    ("spread", "spread(ovr)", 11),
    ("flag", "flag", 14),
)


def _fmt_cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def format_report(rows: Iterable[DivergenceRow]) -> str:
    """Render divergence rows as a fixed-width text table (display only)."""
    header = "  ".join(f"{label:<{width}}" for _, label, width in _COLUMNS)
    lines = [header, "  ".join("-" * width for _, _, width in _COLUMNS)]
    for row in rows:
        cells = []
        for attr, _, width in _COLUMNS:
            cells.append(f"{_fmt_cell(getattr(row, attr)):<{width}}")
        lines.append("  ".join(cells))
    return "\n".join(lines)
