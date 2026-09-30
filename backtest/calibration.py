"""Item 4.7 — projection calibration, the MEASUREMENT half.

Is the week-level SPREAD of our house projections too wide? Measured as the
slope ``b`` of realised house points on projected house points
(``realised = a + b * projected``), per position. ``b < 1`` means every quoted
DIFFERENCE between two players is too wide by about ``1/b``.

THIS MODULE IMPLEMENTS A FROZEN PRE-REGISTRATION. The block that begins
"Pre-registration (written 2026-09-30, committed BEFORE any regression is run"
in ``IMPLEMENTATION_PLAN.md`` item 4.7 was committed in 6a3c9f4 before any
regression was run, and every constant below is a literal transcription of it.
Nothing here tunes, searches or chooses: the decision points, the populations,
the positions, the interval, the floor and the decision rule are all fixed. A
reading the pre-registration leaves ambiguous is recorded in
:data:`INTERPRETATION_NOTES` and printed with the results — never silently.

It MEASURES ONLY. No shrinkage multiplier is built here or anywhere in the
product by this pass; a DEPLOY verdict is a recommendation for a later,
separately-reviewed change to the BOARD (never to ``scoring.py``, Rule 2).

THE TWO SIDES OF EVERY OBSERVATION
----------------------------------
* PROJECTED: ``valuation.weekly_lines(as_of=<capture as_of>, weeks=[w])`` in the
  default ``historical`` view, source ``sleeper_rotowire`` — i.e. house points
  priced through ``scoring.py`` exactly as the waiver tool priced them that
  Tuesday. The projections table is pulled once a day at ~07:25 PT and each
  capture ran at 18:30 PT, so the capture day's stored pull IS the pull the
  tool read (each capture's manifest records that vintage, and
  :func:`observations_for_week` checks it).
* REALISED: house points for week ``w`` through ``scoring.py`` — skill from
  ``weekly_stats`` (``score_offense``), D/ST from ``team_defense``
  (``score_dst``, both bracket systems), K from the kicking columns persisted by
  migration 013 (``weekly_stats.kicker_scoring_inputs`` -> ``score_kicker``) —
  all read under ``base.latest_truth`` at as_of 2026-09-30. No network: the
  draft backtest's ``kicking_frame`` fetches a parquet supplement over the
  network, and that is NOT used here; the persisted columns replace it.

``weekly_stats`` holds more than one retrieval vintage of most rows. Every read
here goes through the as-of accessors, which resolve the newest vintage PER KEY,
so no row is ever counted twice (a bare ``SELECT *`` would double count).

The statistics (:func:`ols`, :func:`cluster_bootstrap`, :func:`percentile_interval`,
:func:`decide`, :func:`fit_position`) are pure and take plain sequences; the
database and capture reads live in the orchestration half below them.
"""

from __future__ import annotations

import argparse
import math
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ziggurat.core import scoring
from ziggurat.core.valuation import canon_position, weekly_lines
from ziggurat.data.nfl import base
from ziggurat.data.nfl.schedules import get_schedule
from ziggurat.data.nfl.team_defense import get_team_defense
from ziggurat.data.nfl.weekly_stats import get_weekly_stats, kicker_scoring_inputs
from ziggurat.decisions import read as capture_read
from ziggurat.decisions.capture import DECISIONS_DIR, POOL_FILE, ROSTER_FILE
from ziggurat.league.state import get_player_state
from ziggurat.paths import DEFAULT_DB_PATH

# ===========================================================================
#                 the pre-registration, transcribed literally
# ===========================================================================

SEASON = 2026

#: Realised points are read under ``base.latest_truth`` at this as_of.
REALISED_AS_OF = "2026-09-30"

#: The projection source the waiver tool prices with (``weekly_lines``' default).
PROJECTION_SOURCE = "sleeper_rotowire"

#: PRIMARY population floor: a week-``w`` projection of at least this many house
#: points. Inclusive ("≥ 1.0").
PROJECTION_FLOOR = 1.0

#: The practical floor on ``b``. Below it a shrinkage factor would move a typical
#: quoted gain by more than 10%.
PRACTICAL_FLOOR = 0.90

#: 95% player-clustered percentile bootstrap, B = 2,000, fixed seed.
CONFIDENCE = Fraction(95, 100)
BOOTSTRAP_B = 2000
#: The fixed seed. Chosen once, before any regression ran (the date of the
#: pre-registration), and never varied. Each (population, position) cell draws
#: from its own SHA-512-seeded stream derived from it, so a cell's interval does
#: not depend on which other cells were fitted first.
BOOTSTRAP_SEED = 20260930

#: Fitted separately, in this order. Canonical names (``valuation`` canon).
POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DST")
#: Display labels (the league's own spelling of the defense slot).
POSITION_LABEL = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "DST": "D/ST"}

DEPLOY = "DEPLOY"
RETIRE = "RETIRE"
NARROW = "NARROW"


@dataclass(frozen=True)
class DecisionPoint:
    """One graded week and the Tuesday 18:30 PT TIMER capture it is read at."""

    week: int
    capture_id: str
    as_of: str


#: The three pre-registered decision points. The decision uses these only.
DECISION_POINTS: tuple[DecisionPoint, ...] = (
    DecisionPoint(1, "20260908T183009-d81bf0a2", "2026-09-08"),
    DecisionPoint(2, "20260915T183009-f6d82bf9", "2026-09-15"),
    DecisionPoint(3, "20260922T183009-527a11ed", "2026-09-22"),
)

#: Week 4 joins ONLY as a sensitivity and ONLY once its Monday game is final.
#: The pre-registration does not name its capture; this is the Tuesday 18:30 PT
#: TIMER capture of week 4 (see INTERPRETATION_NOTES, note 7).
WEEK4_POINT = DecisionPoint(4, "20260929T183029-c1a67b3d", "2026-09-29")


#: Every place the pre-registration left a choice open, and the reading taken.
#: Printed verbatim with the results. The most conservative reading was taken in
#: each case; none of them was chosen after seeing a result.
INTERPRETATION_NOTES: tuple[str, ...] = (
    "1. 'The ESPN universe (league_player_state at the capture's as_of)' is read "
    "through the production accessor get_player_state(as_of=<capture as_of>), "
    "historical view: the newest stored row per ESPN id. Snapshots are "
    "day-granular and each sync replaces its whole day, so this is the day's LAST "
    "sync (23:15 PT), not the 18:30 PT instant of the capture. Universe "
    "MEMBERSHIP (not rostered status) is all the primary population uses, so only "
    "a player added to ESPN's universe in that window could differ.",
    "2. The ESPN->projection join is the production one (core/marginal "
    "_entry_from_row): D/ST by TEAM_ALIASES-normalised pro_team, every other "
    "position by the snapshot row's own gsis_id. A universe row with no gsis_id, or "
    "whose key has no projection line, is UNJOINABLE and counted, never guessed.",
    "3. Positions are the ESPN (league) position, canonicalised by "
    "valuation.canon_position; a projection-side position that disagrees is "
    "counted. Rows at a non-league position are dropped and counted.",
    "4. 'Byes are excluded (no projection)' is enforced as: week w must be in the "
    "line's played_weeks (the projection row carried an OPPONENT). The feed's "
    "bye-shaped row and its no-forecast row are byte-identical, so both are "
    "excluded. This applies to BOTH populations.",
    "5. 'A projected player whose team played but who has no stat row scores 0.0': "
    "'his team' is the projection line's team; 'played' means that team appears "
    "in the week's REG schedule (read under latest_truth at the realised as_of). A "
    "stat row, if present, is scored whatever team it lists. A projected player "
    "with no stat row whose team is NOT on the schedule is excluded and counted. "
    "A D/ST whose team played but has no team_defense row is a join failure, not "
    "a zero, and REFUSES the run. A kicker whose stat row has NULL kicking columns "
    "(not captured) is excluded and counted, never scored 0.",
    "6. SECONDARY population = the capture's pool.jsonl rows with scanned == true "
    "(the free agents the tool's scan modelled) plus every roster.json row, read "
    "through the sha256-verified capture reader. The pre-registration applies the "
    ">= 1.0 floor to the PRIMARY only, so the secondary is fitted WITHOUT it "
    "(byes still excluded, note 4). A second secondary line WITH the floor is "
    "printed and labelled descriptive / not pre-registered.",
    "7. Week 4's capture is not named in the pre-registration. The week-4 "
    "sensitivity uses the Tuesday 18:30 PT TIMER capture of week 4 "
    f"({WEEK4_POINT.capture_id}, as_of {WEEK4_POINT.as_of}), and 'joins' is read "
    "as weeks 1-4 pooled. It is reported 'not yet' until every week-4 team has a "
    "team_defense row at the realised as_of it is read at.",
    "8. The 95% percentile interval uses nearest-rank order statistics with EXACT "
    "rational tails: the 50th and 1,950th smallest of the 2,000 slopes. (Float "
    "arithmetic puts the lower rank at 51: (1 - 0.95) / 2 * 2000 evaluates to "
    "50.00000000000004.)",
    "9. 'The 95% interval contains 1.0' is read as a CLOSED interval "
    "(lower <= 1.0 <= upper). DEPLOY's 'upper < 0.90' is strict, as written.",
    "10. The cluster ('player') is the ESPN player id; a D/ST's cluster is its "
    "ESPN team-defense id. A replicate whose resampled projections have zero "
    "variance has no slope; it would be dropped and COUNTED on the results line "
    "(the percentile then runs over the remaining slopes).",
    "11. The intercept a-hat is reported with its own percentile interval from the "
    "same bootstrap draws. Only b decides anything; a's interval is descriptive.",
    "12. 'No stat row' means no stat row for THIS PLAYER. nflverse's stat tables are "
    "keyed on the real '00-' gsis id; a projection line still keyed on a nflverse "
    "PLACEHOLDER id (the item-3.18 collision) finds no row under its own id even "
    "when the player played. So when the line's own gsis has no stat row, the "
    "ESPN id's preferred gsis (base.gsis_by_espn, 3.18's placeholder-to-real "
    "preference) is tried before the 0.0 rule applies. Every such resolution is "
    "listed by name in the coverage block. Found by auditing every did-not-play "
    "zero BEFORE any fit: one player, two player-weeks.",
    "13. The SECONDARY rows are joined to their projection line through the SAME "
    "ESPN-universe row as the primary (by ESPN id), not through the gsis written "
    "into the capture file. Migration 019 (item 3.18) re-keyed stored projection "
    "and league-state rows from nflverse placeholder ids to real ones AFTER the "
    "week-1 capture was written, so 16 week-1 quoted rookies carry a placeholder "
    "gsis in the capture that no stored line is keyed on any more. The re-key "
    "changed join keys, not projected points. A quoted row whose ESPN id is not in "
    "the universe snapshot falls back to the capture's own ids (counted).",
    "14. latest_truth serves the NEWEST retrieval of every fact knowable by the "
    "realised as_of, so the realised side is the database as it stood when the run "
    "was made, and the coverage block prints the retrieval days it resolved to. On "
    "2026-09-30 week 3's rows come from the Tuesday 2026-09-29 pull, which sits "
    "inside nflverse's Mon-Wed stat-correction window; weeks 1-2 resolve to later "
    "re-pulls. A re-run after the Thursday 2026-10-01 pull can move week-3 values "
    "by the size of any stat correction. The pre-registered as_of is kept as "
    "written; no later pull was waited for.",
)


class CalibrationInputError(ValueError):
    """The measurement was asked for something it cannot honestly compute."""


# ===========================================================================
#                      1.  pure statistics (no database)
# ===========================================================================


@dataclass(frozen=True)
class Observation:
    """One player-week: what the tool projected and what the week returned."""

    week: int
    position: str             # canonical QB/RB/WR/TE/K/DST
    cluster: str              # the bootstrap's resampling unit: the player
    player: str
    projected: float
    realised: float
    did_not_play: bool = False  # realised is the 0.0 the did-not-play rule assigns


def ols(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """Ordinary least squares ``y = a + b x``. Returns ``(a, b)``.

    Raises :class:`ValueError` when ``x`` has no variance (the slope is undefined,
    and a number here would be invented)."""
    n = len(xs)
    if n != len(ys):
        raise ValueError(f"ols: {n} x values against {len(ys)} y values")
    if n < 2:
        raise ValueError(f"ols: {n} observation(s); a slope needs two")
    mx = math.fsum(xs) / n
    my = math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    if sxx <= 0.0:
        raise ValueError("ols: x has zero variance, the slope is undefined")
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    b = sxy / sxx
    return my - b * mx, b


@dataclass(frozen=True)
class _ClusterSums:
    """Sufficient statistics of one cluster, on data centred at the full-sample
    means (centring leaves the slope unchanged and keeps the sums well
    conditioned)."""

    n: int
    sx: float
    sy: float
    sxx: float
    sxy: float


def _cluster_sums(obs: Sequence[Observation]) -> tuple[list[str], list[_ClusterSums], float, float]:
    xs = [o.projected for o in obs]
    ys = [o.realised for o in obs]
    mx = math.fsum(xs) / len(xs)
    my = math.fsum(ys) / len(ys)
    grouped: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        grouped[o.cluster].append(o)
    ids = sorted(grouped)  # a fixed order, independent of input order
    sums = []
    for cid in ids:
        rows = grouped[cid]
        cx = [o.projected - mx for o in rows]
        cy = [o.realised - my for o in rows]
        sums.append(_ClusterSums(
            n=len(rows), sx=math.fsum(cx), sy=math.fsum(cy),
            sxx=math.fsum(x * x for x in cx),
            sxy=math.fsum(x * y for x, y in zip(cx, cy, strict=True)),
        ))
    return ids, sums, mx, my


def draw_clusters(n_clusters: int, rng: random.Random) -> list[int]:
    """One bootstrap draw: ``n_clusters`` cluster indices, with replacement."""
    return [rng.randrange(n_clusters) for _ in range(n_clusters)]


def replicate_fit(sums: Sequence[_ClusterSums], drawn: Sequence[int],
                  mx: float, my: float) -> tuple[float, float] | None:
    """``(a, b)`` of the OLS fit to the union of the drawn clusters, EVERY week of
    each drawn cluster included (a cluster drawn twice contributes all its weeks
    twice). ``None`` when the replicate's projections have no variance."""
    n = sx = sy = sxx = sxy = 0.0
    for i in drawn:
        c = sums[i]
        n += c.n
        sx += c.sx
        sy += c.sy
        sxx += c.sxx
        sxy += c.sxy
    denom = n * sxx - sx * sx
    if n < 2 or denom <= 1e-12 * max(1.0, n * sxx):
        return None
    b = (n * sxy - sx * sy) / denom
    # intercept in the ORIGINAL (uncentred) units: y - my = a' + b (x - mx)
    a_centred = (sy - b * sx) / n
    return my + a_centred - b * mx, b


@dataclass(frozen=True)
class BootstrapDraws:
    slopes: tuple[float, ...]
    intercepts: tuple[float, ...]
    degenerate: int           # replicates with no slope (dropped, counted)
    clusters: int


def bootstrap_rng(seed: int, label: str) -> random.Random:
    """The stream for one fitted cell. A ``str`` seed is hashed with SHA-512 by
    ``random.seed`` (version 2), so it does not depend on ``PYTHONHASHSEED``."""
    return random.Random(f"ziggurat-4.7-calibration:{int(seed)}:{label}")


def cluster_bootstrap(obs: Sequence[Observation], *, b: int = BOOTSTRAP_B,
                      seed: int = BOOTSTRAP_SEED, label: str) -> BootstrapDraws:
    """Player-clustered bootstrap of the OLS fit: resample PLAYERS with
    replacement, carrying all their weeks together, ``b`` times."""
    if not obs:
        raise ValueError("cluster_bootstrap: no observations")
    ids, sums, mx, my = _cluster_sums(obs)
    rng = bootstrap_rng(seed, label)
    slopes, intercepts, degenerate = [], [], 0
    for _ in range(b):
        fit = replicate_fit(sums, draw_clusters(len(ids), rng), mx, my)
        if fit is None:
            degenerate += 1
            continue
        intercepts.append(fit[0])
        slopes.append(fit[1])
    return BootstrapDraws(tuple(slopes), tuple(intercepts), degenerate, len(ids))


def percentile_interval(values: Sequence[float],
                        confidence: Fraction = CONFIDENCE) -> tuple[float, float]:
    """The equal-tailed percentile interval by nearest-rank order statistics,
    with the tails computed in EXACT rational arithmetic (a float tail puts the
    lower rank of 2,000 at 51, not 50)."""
    if not values:
        raise ValueError("percentile_interval: no values")
    confidence = Fraction(confidence)
    if not 0 < confidence < 1:
        raise ValueError(f"percentile_interval: confidence {confidence} outside (0, 1)")
    tail = (1 - confidence) / 2
    ordered = sorted(values)
    n = len(ordered)
    lo_rank = max(1, math.ceil(tail * n))
    hi_rank = max(1, math.ceil((1 - tail) * n))
    return ordered[lo_rank - 1], ordered[hi_rank - 1]


def decide(b_hat: float, lower: float, upper: float, *,
           floor: float = PRACTICAL_FLOOR) -> str:
    """The pre-registered decision rule, per position.

    * DEPLOY: the 95% upper bound of ``b`` is < ``floor`` (0.90).
    * RETIRE: the 95% interval contains 1.0 AND ``b_hat >= floor``.
    * NARROW: every other outcome — including a slope significantly ABOVE 1,
      which is reported, not acted on.
    """
    if upper < floor:
        return DEPLOY
    if lower <= 1.0 <= upper and b_hat >= floor:
        return RETIRE
    return NARROW


@dataclass(frozen=True)
class Fit:
    position: str
    n_obs: int
    n_players: int
    a: float
    b: float
    a_interval: tuple[float, float]
    b_interval: tuple[float, float]
    decision: str
    degenerate: int
    mean_projected: float
    mean_realised: float
    did_not_play: int


def fit_position(obs: Sequence[Observation], *, position: str, label: str,
                 b: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED,
                 confidence: Fraction = CONFIDENCE,
                 floor: float = PRACTICAL_FLOOR) -> Fit:
    """Fit one position's pooled observations and apply the decision rule."""
    rows = [o for o in obs if o.position == position]
    if len(rows) < 2:
        raise ValueError(f"fit_position: {len(rows)} observation(s) at {position}")
    a_hat, b_hat = ols([o.projected for o in rows], [o.realised for o in rows])
    draws = cluster_bootstrap(rows, b=b, seed=seed, label=f"{label}:{position}")
    b_lo, b_hi = percentile_interval(draws.slopes, confidence)
    a_lo, a_hi = percentile_interval(draws.intercepts, confidence)
    return Fit(
        position=position, n_obs=len(rows), n_players=draws.clusters,
        a=a_hat, b=b_hat, a_interval=(a_lo, a_hi), b_interval=(b_lo, b_hi),
        decision=decide(b_hat, b_lo, b_hi, floor=floor),
        degenerate=draws.degenerate,
        mean_projected=math.fsum(o.projected for o in rows) / len(rows),
        mean_realised=math.fsum(o.realised for o in rows) / len(rows),
        did_not_play=sum(1 for o in rows if o.did_not_play),
    )


def fit_all(obs: Sequence[Observation], *, label: str, b: int = BOOTSTRAP_B,
            seed: int = BOOTSTRAP_SEED,
            positions: Sequence[str] = POSITIONS) -> dict[str, Fit | None]:
    """Every position's fit; ``None`` for a position with fewer than two rows."""
    out: dict[str, Fit | None] = {}
    for pos in positions:
        try:
            out[pos] = fit_position(obs, position=pos, label=label, b=b, seed=seed)
        except ValueError:
            out[pos] = None
    return out


# ===========================================================================
#            2.  the observation rules (pure over plain inputs)
# ===========================================================================


def norm_team(raw) -> str | None:
    """TEAM_ALIASES-normalised team abbreviation; ``None``/empty/``FA`` -> None."""
    if raw is None:
        return None
    token = str(raw).strip().upper()
    if not token or token in ("NONE", "FA"):
        return None
    return base.TEAM_ALIASES.get(token, token)


def projection_key(position: str, *, gsis_id, pro_team) -> tuple | None:
    """The ``weekly_lines`` key a league-side row joins on (core/marginal's rule):
    D/ST by normalised team, everything else by gsis id; ``None`` = unjoinable."""
    if position == "DST":
        team = norm_team(pro_team)
        return ("DST", team) if team else None
    return ("SKILL", str(gsis_id)) if gsis_id else None


@dataclass(frozen=True)
class RealisedWeek:
    """Everything the grade of one week needs, already resolved per key."""

    week: int
    stat_rows: Mapping[str, Mapping]    # gsis -> newest weekly_stats row (REG)
    dst_rows: Mapping[str, Mapping]     # normalised team -> newest team_defense row
    teams_played: frozenset[str]        # normalised teams on the week's REG schedule

    def vintages(self) -> str:
        """The retrieval days the resolved rows came from (reproducibility: under
        ``latest_truth`` a later correction pull replaces them — note 14)."""
        def fmt(rows):
            c = Counter(str(r["retrieved_as_of"]) for r in rows.values())
            return ", ".join(f"{d} x{n}" for d, n in sorted(c.items())) or "none"
        return f"weekly_stats {fmt(self.stat_rows)}; team_defense {fmt(self.dst_rows)}"


class NotCaptured(Exception):
    """A kicker stat row whose kicking columns are NULL: no honest number."""


@dataclass(frozen=True)
class Realised:
    points: float
    did_not_play: bool = False   # the 0.0 the did-not-play rule assigns
    via_crosswalk: bool = False  # the stat row was found under the ESPN-side gsis


def realised_points(position: str, *, gsis_id, team, week: RealisedWeek,
                    crosswalk_gsis_id=None) -> Realised | None:
    """The realised house points of one projected player-week, or ``None`` when
    the rule EXCLUDES him (his team did not play and he has no stat row).

    ``crosswalk_gsis_id`` is the ESPN id's preferred gsis (``base.gsis_by_espn``,
    item 3.18's placeholder-to-real preference). It is consulted ONLY when the
    projection line's own gsis has no stat row: nflverse's stat tables are keyed
    on the real ``00-`` id, and a line still keyed on a placeholder would
    otherwise score a player who PLAYED as 0.0 — the opposite of the rule's
    premise (INTERPRETATION_NOTES, note 12).

    Raises :class:`NotCaptured` for a kicker row with NULL kicking columns and
    :class:`CalibrationInputError` for a D/ST whose team played with no row.
    Every point comes from ``core/scoring.py`` (Rule 2).
    """
    if position == "DST":
        row = week.dst_rows.get(team)
        if row is not None:
            return Realised(scoring.score_dst(dict(row)))
        if team in week.teams_played:
            raise CalibrationInputError(
                f"week {week.week}: the {team} D/ST's team played but has no "
                "team_defense row — a franchise defense always has one, so this is a "
                "join failure, not a zero. Refusing."
            )
        return None
    row = week.stat_rows.get(str(gsis_id)) if gsis_id else None
    via = False
    if row is None and crosswalk_gsis_id and str(crosswalk_gsis_id) != str(gsis_id):
        row = week.stat_rows.get(str(crosswalk_gsis_id))
        via = row is not None
    if row is not None:
        if position == "K":
            line = kicker_scoring_inputs(row)
            if line is None:
                raise NotCaptured(str(gsis_id))
            return Realised(scoring.score_kicker(line), via_crosswalk=via)
        return Realised(scoring.score_offense(dict(row)), via_crosswalk=via)
    if team is not None and team in week.teams_played:
        # he did not play, and that is what starting him returned
        return Realised(0.0, did_not_play=True)
    return None


# ===========================================================================
#               3.  orchestration (database + capture reads)
# ===========================================================================


def open_ro(path) -> sqlite3.Connection:
    """A READ-ONLY connection — never ``store.open_db``, which migrates."""
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def realised_week(conn, week: int, *, as_of: str = REALISED_AS_OF,
                  season: int = SEASON) -> RealisedWeek:
    """Week ``week``'s realised inputs, every read under ``base.latest_truth``."""
    stat_rows: dict[str, Mapping] = {}
    for r in base.latest_truth(get_weekly_stats)(conn, as_of=as_of, season=season, week=week):
        if r["season_type"] != "REG" or r["player_id"] is None:
            continue
        pid = str(r["player_id"])
        if pid in stat_rows:  # select_as_of resolves one row per key; belt and braces
            raise CalibrationInputError(f"week {week}: two weekly_stats rows for {pid}")
        stat_rows[pid] = r
    dst_rows: dict[str, Mapping] = {}
    for r in base.latest_truth(get_team_defense)(conn, as_of=as_of, season=season, week=week):
        if r["season_type"] != "REG":
            continue
        team = norm_team(r["team"])
        if team in dst_rows:
            raise CalibrationInputError(f"week {week}: two team_defense rows for {team}")
        dst_rows[team] = r
    teams: set[str] = set()
    for g in base.latest_truth(get_schedule)(conn, as_of=as_of, season=season, week=week):
        if g["game_type"] != "REG":
            continue
        teams.update(t for t in (norm_team(g["home_team"]), norm_team(g["away_team"])) if t)
    return RealisedWeek(week, stat_rows, dst_rows, frozenset(teams))


def week_is_final(week: RealisedWeek) -> bool:
    """Every team on the week's REG schedule has a team_defense row (the D/ST
    line is built from the final game result, so its presence is the finish)."""
    return bool(week.teams_played) and week.teams_played <= set(week.dst_rows)


@dataclass
class WeekCoverage:
    """The join and coverage counts of one decision point (printed in full)."""

    week: int
    capture_id: str
    as_of: str
    projection_vintage: str | None = None
    realised_vintages: str = ""
    universe_rows: int = 0
    universe_snapshot_rows: int = 0
    non_league_position: int = 0
    unjoinable_no_gsis: int = 0
    unjoinable_no_gsis_by_position: Counter = field(default_factory=Counter)
    unjoinable_no_line: int = 0
    unjoinable_no_line_by_position: Counter = field(default_factory=Counter)
    lines_without_gsis_above_floor: int = 0     # SPID-keyed projection lines
    realised_via_crosswalk: list = field(default_factory=list)
    duplicate_key: int = 0
    no_forecast_this_week: int = 0      # bye-shaped / no-forecast row (note 4)
    below_floor: int = 0
    team_did_not_play: int = 0
    kicker_not_captured: int = 0
    position_disagrees: int = 0
    stale_projection_rows: int = 0
    lines_above_floor_outside_universe: int = 0
    primary: int = 0
    primary_did_not_play: int = 0
    primary_by_position: Counter = field(default_factory=Counter)
    secondary_rows: int = 0
    secondary_unjoinable: int = 0
    secondary_no_forecast: int = 0
    secondary_excluded: int = 0         # team not on schedule / kicker not captured
    secondary_via_crosswalk: list = field(default_factory=list)
    secondary_joined_on_capture_ids: int = 0   # quoted row absent from the universe
    secondary: int = 0
    secondary_by_position: Counter = field(default_factory=Counter)


def _capture_dir(root, point: DecisionPoint) -> Path:
    path = capture_read.find_capture(root, capture_id=point.capture_id)
    if path is None:
        raise CalibrationInputError(
            f"capture {point.capture_id} (week {point.week}) not found under {root}"
        )
    return path


def load_capture(root, point: DecisionPoint) -> tuple[dict, list[dict], list[dict]]:
    """``(manifest, pool rows, roster rows)`` of one decision point, verified.

    Refuses a capture whose bytes moved (the sha256-checked reader), whose
    as_of differs from the pre-registered one, which was not a TIMER capture,
    or which priced a different first week."""
    path = _capture_dir(root, point)
    report = capture_read.verify(path)
    if not report.ok:
        raise CalibrationInputError(report.render())
    manifest = capture_read.load_manifest(path)
    if manifest.get("as_of") != point.as_of:
        raise CalibrationInputError(
            f"{point.capture_id}: manifest as_of {manifest.get('as_of')} is not the "
            f"pre-registered {point.as_of}"
        )
    if manifest.get("trigger") != "timer":
        raise CalibrationInputError(
            f"{point.capture_id}: trigger {manifest.get('trigger')!r}; the "
            "pre-registration names the Tuesday 18:30 PT TIMER capture"
        )
    if manifest.get("week") != point.week:
        raise CalibrationInputError(
            f"{point.capture_id}: priced week {manifest.get('week')}, expected {point.week}"
        )
    pool = capture_read.read_records(path, POOL_FILE)
    roster_doc = capture_read.read_records(path, ROSTER_FILE)[0]
    return manifest, pool, list(roster_doc.get("roster") or [])


def _observation(pos, *, cluster, player, line, point, realised, cov,
                 crosswalk: Mapping[str, str]) -> Observation | None:
    """Apply the realised rule to one joined row, counting every exclusion."""
    try:
        got = realised_points(pos, gsis_id=line.gsis_id, team=line.team, week=realised,
                              crosswalk_gsis_id=crosswalk.get(cluster))
    except NotCaptured:
        cov.kicker_not_captured += 1
        return None
    if got is None:
        cov.team_did_not_play += 1
        return None
    if got.via_crosswalk:
        cov.realised_via_crosswalk.append(f"{player} ({line.gsis_id} -> {crosswalk.get(cluster)})")
    return Observation(
        week=point.week, position=pos, cluster=cluster, player=player,
        projected=float(line.points[point.week]), realised=float(got.points),
        did_not_play=got.did_not_play,
    )


def observations_for_week(conn, point: DecisionPoint, *, captures_root,
                          realised: RealisedWeek, floor: float = PROJECTION_FLOOR,
                          crosswalk: Mapping[str, str] | None = None,
                          ) -> tuple[list[Observation], list[Observation], WeekCoverage]:
    """``(primary, secondary, coverage)`` for one decision point.

    Both populations join a player to his projection line through the SAME
    ESPN-universe row (note 13), so a player quoted by the tool and present in
    the universe carries the identical projected number in both.
    """
    w = point.week
    cov = WeekCoverage(week=w, capture_id=point.capture_id, as_of=point.as_of)
    cov.realised_vintages = realised.vintages()
    manifest, pool, roster = load_capture(captures_root, point)
    cov.projection_vintage = (manifest.get("vintages") or {}).get("projections")

    lines = weekly_lines(conn, as_of=point.as_of, season=SEASON, weeks=[w],
                         source=PROJECTION_SOURCE)
    # espn id -> preferred (real) gsis: the repo's own crosswalk, consulted ONLY
    # when a line's gsis has no stat row (note 12). Identity is immutable, so the
    # crosswalk-at-now read is the documented contract of this map.
    if crosswalk is None:
        crosswalk = base.gsis_by_espn(conn)
    newest = max((d for ln in lines.values() for d in ln.retrieved_as_of), default=None)
    if cov.projection_vintage is not None and newest != cov.projection_vintage:
        raise CalibrationInputError(
            f"{point.capture_id}: the capture priced off the {cov.projection_vintage} "
            f"projection pull, but re-reading at as_of {point.as_of} resolves to {newest}; "
            "the database no longer reproduces the tool's Tuesday read. Refusing."
        )
    if cov.projection_vintage is not None:
        cov.stale_projection_rows = sum(
            1 for ln in lines.values()
            if w in ln.points and cov.projection_vintage not in ln.retrieved_as_of
        )

    def forecast(line) -> bool:
        return line is not None and w in line.points and w in line.played_weeks

    # ---------------------------------------------------------------- PRIMARY
    universe = get_player_state(conn, as_of=point.as_of, season=SEASON)
    cov.universe_rows = len(universe)
    snapshot_day = max((r["retrieved_as_of"] for r in universe), default=None)
    cov.universe_snapshot_rows = sum(1 for r in universe if r["retrieved_as_of"] == snapshot_day)
    primary: list[Observation] = []
    joined_keys: dict[tuple, str] = {}
    key_by_espn: dict[str, tuple[str, tuple | None]] = {}
    for r in sorted(universe, key=lambda r: str(r["espn_player_id"])):
        pos = canon_position(r["position"])
        if pos is None:
            cov.non_league_position += 1
            continue
        key = projection_key(pos, gsis_id=r["gsis_id"], pro_team=r["pro_team"])
        key_by_espn[str(r["espn_player_id"])] = (pos, key)
        if key is None:
            cov.unjoinable_no_gsis += 1
            cov.unjoinable_no_gsis_by_position[pos] += 1
            continue
        line = lines.get(key)
        if line is None:
            cov.unjoinable_no_line += 1
            cov.unjoinable_no_line_by_position[pos] += 1
            continue
        if key in joined_keys:
            cov.duplicate_key += 1   # two ESPN rows on one projection line: keep the first
            continue
        joined_keys[key] = str(r["espn_player_id"])
        if not forecast(line):
            cov.no_forecast_this_week += 1
            continue
        if line.points[w] < floor:
            cov.below_floor += 1
            continue
        if line.position != pos:
            cov.position_disagrees += 1
        obs = _observation(pos, cluster=str(r["espn_player_id"]),
                           player=str(r["player"] or line.player or key),
                           line=line, point=point, realised=realised, cov=cov,
                           crosswalk=crosswalk)
        if obs is not None:
            primary.append(obs)
    cov.lines_above_floor_outside_universe = sum(
        1 for k, ln in lines.items()
        if k not in joined_keys and forecast(ln) and ln.points[w] >= floor
    )
    cov.lines_without_gsis_above_floor = sum(
        1 for k, ln in lines.items()
        if k[0] == "SPID" and forecast(ln) and ln.points[w] >= floor
    )
    cov.primary = len(primary)
    cov.primary_did_not_play = sum(1 for o in primary if o.did_not_play)
    cov.primary_by_position = Counter(o.position for o in primary)

    # -------------------------------------------------------------- SECONDARY
    quoted = [
        (str(r.get("espn_id")), r.get("position"), r.get("gsis_id"), r.get("pro_team"),
         r.get("player"))
        for r in pool if r.get("scanned")
    ] + [
        (str(r.get("espn_player_id")), r.get("position"), r.get("gsis_id"),
         r.get("pro_team"), r.get("player"))
        for r in roster
    ]
    cov.secondary_rows = len(quoted)
    secondary: list[Observation] = []
    silent = WeekCoverage(week=w, capture_id=point.capture_id, as_of=point.as_of)
    for espn_id, raw_pos, gsis, team, name in sorted(quoted, key=lambda q: q[0]):
        if espn_id in key_by_espn:
            pos, key = key_by_espn[espn_id]
        else:
            # not in the universe snapshot: fall back to the capture's own ids
            cov.secondary_joined_on_capture_ids += 1
            pos = canon_position(raw_pos)
            key = projection_key(pos, gsis_id=gsis, pro_team=team) if pos else None
        line = lines.get(key) if key is not None else None
        if line is None:
            cov.secondary_unjoinable += 1
            continue
        if not forecast(line):
            cov.secondary_no_forecast += 1
            continue
        obs = _observation(pos, cluster=espn_id, player=str(name or line.player or key),
                           line=line, point=point, realised=realised, cov=silent,
                           crosswalk=crosswalk)
        if obs is not None:
            secondary.append(obs)
    cov.secondary_excluded = silent.team_did_not_play + silent.kicker_not_captured
    cov.secondary_via_crosswalk = list(silent.realised_via_crosswalk)
    cov.secondary = len(secondary)
    cov.secondary_by_position = Counter(o.position for o in secondary)
    return primary, secondary, cov


@dataclass(frozen=True)
class Cell:
    """One fitted population: a label, its fits, and whether it decides."""

    label: str
    description: str
    decides: bool
    fits: Mapping[str, Fit | None]
    n_weeks: tuple[int, ...]


@dataclass(frozen=True)
class CalibrationReport:
    primary: Cell
    sensitivities: tuple[Cell, ...]
    week4_status: str
    coverage: tuple[WeekCoverage, ...]


def run_calibration(conn, *, captures_root, realised_as_of: str = REALISED_AS_OF,
                    week4_realised_as_of: str | None = None,
                    b: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED,
                    points: Sequence[DecisionPoint] = DECISION_POINTS,
                    week4: DecisionPoint | None = WEEK4_POINT) -> CalibrationReport:
    """The whole pre-registered measurement: the PRIMARY decision plus every
    pre-registered sensitivity. Pure reads; writes nothing anywhere."""
    primary_obs: list[Observation] = []
    secondary_obs: list[Observation] = []
    coverage: list[WeekCoverage] = []
    crosswalk = base.gsis_by_espn(conn)
    for point in points:
        realised = realised_week(conn, point.week, as_of=realised_as_of)
        if not week_is_final(realised):
            raise CalibrationInputError(
                f"week {point.week} is not final at the realised as_of {realised_as_of}: "
                f"{len(realised.teams_played - set(realised.dst_rows))} scheduled team(s) "
                "have no team_defense row. The pre-registered weeks must be final."
            )
        p, s, cov = observations_for_week(conn, point, captures_root=captures_root,
                                          realised=realised, crosswalk=crosswalk)
        primary_obs += p
        secondary_obs += s
        coverage.append(cov)

    weeks = tuple(pt.week for pt in points)
    primary = Cell("PRIMARY", "ESPN universe, week-w projection >= 1.0, weeks "
                   + "-".join(str(w) for w in (weeks[0], weeks[-1])) + " pooled",
                   True, fit_all(primary_obs, label="primary", b=b, seed=seed), weeks)

    sens: list[Cell] = []
    late = tuple(w for w in weeks if w >= 2)
    sens.append(Cell(
        "WEEKS 2-3", "primary population, weeks 2-3 only (Week-1 scoring is "
        "systematically suppressed — literature hint 8)", False,
        fit_all([o for o in primary_obs if o.week >= 2], label="weeks23", b=b, seed=seed),
        late,
    ))
    sens.append(Cell(
        "SECONDARY", "the capture's scanned free-agent pool + our roster (the rows "
        "the tool quoted), no projection floor", False,
        fit_all(secondary_obs, label="secondary", b=b, seed=seed), weeks,
    ))
    sens.append(Cell(
        "SECONDARY >= 1.0", "DESCRIPTIVE, NOT PRE-REGISTERED: the secondary "
        "population with the primary's >= 1.0 floor applied", False,
        fit_all([o for o in secondary_obs if o.projected >= PROJECTION_FLOOR],
                label="secondary-floor", b=b, seed=seed), weeks,
    ))

    week4_status = "not yet: no week-4 decision point configured"
    if week4 is not None:
        at = week4_realised_as_of or realised_as_of
        realised4 = realised_week(conn, week4.week, as_of=at)
        if not week_is_final(realised4):
            missing = len(realised4.teams_played - set(realised4.dst_rows))
            week4_status = (
                f"not yet: week {week4.week} is not final at realised as_of {at} "
                f"({missing} of {len(realised4.teams_played)} scheduled teams have no "
                "team_defense row). Re-run with --week4-realised-as-of after its "
                "Monday game."
            )
        else:
            p4, _s4, cov4 = observations_for_week(conn, week4, captures_root=captures_root,
                                                  realised=realised4, crosswalk=crosswalk)
            coverage.append(cov4)
            sens.append(Cell(
                "WEEKS 1-4", f"primary population with week 4 joined (week 4 realised "
                f"read at {at})", False,
                fit_all(primary_obs + p4, label="weeks14", b=b, seed=seed),
                weeks + (week4.week,),
            ))
            week4_status = f"final at {at}: reported as the WEEKS 1-4 sensitivity"
    return CalibrationReport(primary, tuple(sens), week4_status, tuple(coverage))


# ===========================================================================
#                              4.  rendering
# ===========================================================================


def _fmt_interval(iv: tuple[float, float], digits: int = 3) -> str:
    return f"[{iv[0]:+.{digits}f}, {iv[1]:+.{digits}f}]"


def format_cell(cell: Cell) -> str:
    head = "decision" if cell.decides else "rule reads (NOT decided on)"
    out = [f"{cell.label} — {cell.description}",
           f"  {'pos':<5}{'n obs':>6}{'players':>8}{'b-hat':>8}  {'95% CI of b':<18}"
           f"{'a-hat':>7}  {'95% CI of a':<18}{'mean proj':>10}{'mean real':>10}"
           f"{'DNP 0.0':>8}  {head}"]
    for pos in POSITIONS:
        fit = cell.fits.get(pos)
        label = POSITION_LABEL[pos]
        if fit is None:
            out.append(f"  {label:<5}  fewer than two observations — not fitted")
            continue
        note = f" ({fit.degenerate} degenerate replicate(s) dropped)" if fit.degenerate else ""
        out.append(
            f"  {label:<5}{fit.n_obs:>6}{fit.n_players:>8}{fit.b:>8.3f}  "
            f"{_fmt_interval(fit.b_interval):<18}{fit.a:>7.2f}  "
            f"{_fmt_interval(fit.a_interval, 2):<18}{fit.mean_projected:>10.2f}"
            f"{fit.mean_realised:>10.2f}{fit.did_not_play:>8}  {fit.decision}{note}"
        )
    return "\n".join(out)


def _by_pos(counter: Counter) -> str:
    parts = [f"{POSITION_LABEL[p]} {counter[p]}" for p in POSITIONS if counter.get(p)]
    return ", ".join(parts) if parts else "none"


def format_coverage(cov: WeekCoverage) -> str:
    pos = ", ".join(f"{POSITION_LABEL[p]} {cov.primary_by_position.get(p, 0)}" for p in POSITIONS)
    spos = ", ".join(f"{POSITION_LABEL[p]} {cov.secondary_by_position.get(p, 0)}" for p in POSITIONS)
    crossed = cov.realised_via_crosswalk + [f"{x} [secondary]" for x in cov.secondary_via_crosswalk]
    return "\n".join([
        f"week {cov.week}  capture {cov.capture_id}  as_of {cov.as_of}  "
        f"projection vintage {cov.projection_vintage}",
        f"  realised rows resolved from retrieval day(s): {cov.realised_vintages}",
        f"  ESPN universe rows {cov.universe_rows} (of them on the newest snapshot day: "
        f"{cov.universe_snapshot_rows}); non-league position {cov.non_league_position}",
        f"  UNJOINABLE: no gsis id {cov.unjoinable_no_gsis} ({_by_pos(cov.unjoinable_no_gsis_by_position)}); "
        f"no projection line {cov.unjoinable_no_line} "
        f"({_by_pos(cov.unjoinable_no_line_by_position)}); second ESPN row on one line "
        f"{cov.duplicate_key}",
        f"  projection lines >= {PROJECTION_FLOOR} keyed on no gsis at all (Sleeper id only, "
        f"unjoinable to ESPN by construction): {cov.lines_without_gsis_above_floor}",
        f"  realised stat row found via the espn->gsis crosswalk (note 12): "
        f"{'; '.join(crossed) if crossed else 'none'}",
        f"  joined but excluded: no week-{cov.week} forecast (bye / no-forecast) "
        f"{cov.no_forecast_this_week}; projection < {PROJECTION_FLOOR} {cov.below_floor}; "
        f"team not on the schedule, no stat row {cov.team_did_not_play}; "
        f"kicker not captured {cov.kicker_not_captured}",
        f"  projection lines >= {PROJECTION_FLOOR} with no ESPN universe row: "
        f"{cov.lines_above_floor_outside_universe}; lines read from an older vintage "
        f"than the capture's: {cov.stale_projection_rows}; projection position != "
        f"ESPN position (in primary): {cov.position_disagrees}",
        f"  PRIMARY {cov.primary} player-weeks ({pos}); did-not-play -> 0.0: "
        f"{cov.primary_did_not_play}",
        f"  SECONDARY {cov.secondary} of {cov.secondary_rows} quoted rows ({spos}); "
        f"unjoinable {cov.secondary_unjoinable}; joined on the capture's own ids "
        f"(absent from the universe snapshot) {cov.secondary_joined_on_capture_ids}; "
        f"no week-{cov.week} forecast "
        f"{cov.secondary_no_forecast}; team not on schedule / kicker not captured "
        f"{cov.secondary_excluded}",
    ])


def format_report(report: CalibrationReport, *, b: int = BOOTSTRAP_B,
                  seed: int = BOOTSTRAP_SEED, realised_as_of: str = REALISED_AS_OF) -> str:
    lines = [
        "ITEM 4.7 — PROJECTION CALIBRATION (the pre-registered measurement; frozen in 6a3c9f4)",
        f"realised = a + b * projected, OLS per position; 95% player-clustered percentile "
        f"bootstrap, B = {b}, seed {seed}; practical floor b = {PRACTICAL_FLOOR:.2f}",
        f"projected: weekly_lines at each capture's as_of (historical, {PROJECTION_SOURCE}); "
        f"realised: scoring.py over weekly_stats / team_defense / persisted kicking "
        f"columns under latest_truth at {realised_as_of}",
        "PROJECTED and REALISED are both house points; only b decides anything.",
        "",
        format_cell(report.primary),
        "",
        "SENSITIVITIES (reported, never decided on):",
    ]
    for cell in report.sensitivities:
        lines += ["", format_cell(cell)]
    lines += ["", f"WEEK 4: {report.week4_status}", "", "JOIN / COVERAGE:"]
    for cov in report.coverage:
        lines.append(format_coverage(cov))
    lines += ["", "INTERPRETATION NOTES (readings of the pre-registration, fixed before any fit):"]
    lines += [f"  {n}" for n in INTERPRETATION_NOTES]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.calibration",
        description="Item 4.7: the pre-registered projection-calibration measurement.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH),
                        help="SQLite database (opened READ-ONLY)")
    parser.add_argument("--captures", default=str(DECISIONS_DIR),
                        help="decision-capture root (data/decisions)")
    parser.add_argument("--week4-realised-as-of", default=None,
                        help="realised as_of for the week-4 sensitivity only (default: "
                             f"the pre-registered {REALISED_AS_OF})")
    args = parser.parse_args(argv)
    conn = open_ro(args.db)
    try:
        report = run_calibration(conn, captures_root=args.captures,
                                 week4_realised_as_of=args.week4_realised_as_of,
                                 points=DECISION_POINTS, week4=WEEK4_POINT)
    except CalibrationInputError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
