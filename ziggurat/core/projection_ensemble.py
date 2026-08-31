"""Two-source projection ensemble + the DISAGREEMENT signal (item 3.9).

WHAT THIS IS FOR. Everything this system values — the VOR board, the draft
engine, the marginal/waiver board, streaming — rides on ONE projection feed
(Sleeper's ``sleeper_rotowire``, re-scored through ``core/scoring.py`` by
``valuation.weekly_lines``). One feed cannot distinguish "this player is good"
from "this feed likes this player". This module joins a genuinely independent
second opinion (``data/nfl/espn_projections.py``) to that spine and reports:

  * a BLENDED season estimate, and
  * a per-player DISAGREEMENT measure — which is the more valuable output. A
    player both sources love is a safer pick than a player only one source
    loves, and nothing in this system has ever been able to say that.

It re-prices nothing and it owns no scoring number (rule 2). The house side
comes from ``valuation.weekly_lines`` verbatim; the ESPN side from
``espn_projections.house_points``, which re-derives ESPN's projected STAT LINE
through the same ``core/scoring.py``. Both numbers are therefore HOUSE points
computed by the same engine, which is what makes them comparable at all — and
in particular what makes them comparable at K and D/ST, where ESPN's *default*
scoring differs from this league's most and a naive comparison of two foreign
point totals would be meaningless. See ``espn_projections`` for the verification
(the ESPN stat line re-scored through ``scoring.py`` reproduces ESPN's own
league-applied total to 1e-6 on 32/32 kickers and 32/32 defences).

------------------------------------------------------------------------------
THE TWO MEASUREMENTS THAT SHAPE THIS MODULE (live 2026 data, 2026-08-30)
------------------------------------------------------------------------------

**1. The two sources are on DIFFERENT HORIZONS, and ignoring it would have made
the disagreement signal an artefact.** The house season total sums weeks 1-17,
and every 2026 team's bye falls in weeks 5-14, so every house line covers
exactly **16 games** (measured: 525 of 531 priceable identities cover 16 of 16
playable weeks). ESPN projects the **17-game season** (stat id 210 = 17.0 for
514 of 524 projected players). Raw totals therefore make ESPN look ~3% HIGHER
than the house (median ratio 1.0304) while per game it is ~3% LOWER (0.9697) —
the same data, opposite conclusions, purely from 17/16.

So every comparison here happens **per game**, and the reported season numbers
are both re-expressed on ONE common horizon (:attr:`EnsembleReport.horizon_games`,
DERIVED from the data as the most common house game count, not hard-coded). The
normalization is stated in the reasons on every row, because a novice reading
"ESPN says 380, the house says 366" cannot otherwise know that 14 of those
points are a calendar difference.

**2. ESPN carries an AVAILABILITY opinion that the house feed does not have, and
it is kept SEPARATE from the points.** Ten of 524 projected players carry fewer
than a full season of games — a TE at 16, a RB at 11, a WR at 10, a QB at 4 —
i.e. ESPN pricing in a known absence. The Sleeper feed is a flat season rate
(item 3.2: median week-to-week CV ~1% for every skill position) and has no such
column. That is real information, and it is surfaced as its own field and its
own reason line (``espn_expected_games`` / ``ESPN_EXPECTS_ABSENCE``) rather than
multiplied into ``blended_points``. Folding an availability discount into a
number labelled "projection blend" is precisely the
probability-mass-split-wearing-a-mechanism's-label mistake the item-3.2 audit
charged for; the rate opinion and the availability opinion answer different
questions and a novice must be able to see which one moved.

------------------------------------------------------------------------------
HOW DISAGREEMENT IS MEASURED, AND WHY NOT JUST |a - b|
------------------------------------------------------------------------------
A raw points gap is not comparable across positions (a 20-point gap is nothing
on a 370-point QB and enormous on a 60-point defence) and it carries the
per-position systematic offset between the two sources, which is not a fact
about any individual player. So three quantities are reported, in increasing
order of usefulness:

  * ``delta_points`` — signed ESPN minus house, on the common horizon. The
    honest, legible number. Positive = ESPN is higher.
  * ``delta_relative`` — the same gap as a fraction of the two sources' average,
    with the average floored at :data:`MIN_RELATIVE_DENOMINATOR` so a rounding
    difference between two near-zero deep-bench projections cannot top a report
    sorted by relative gap. Scale-free, but still carries the position's
    systematic offset. When the floor BINDS the reason text says so and quotes
    the unfloored figure — the label and the number must agree.
  * ``delta_z`` — ``delta_points`` standardized WITHIN THE POSITION. This is the
    risk signal: it answers "is this an unusual disagreement for a player at
    this position", and standardizing within position removes the systematic
    per-source offset, which is a property of the two FEEDS and not of any
    player. That offset is not small and it is not uniform: measured live it is
    about -3% overall but **+21 points (~17%) at kicker**, because the house
    feed drops every 50-yard-plus field goal (see ``POSITION_CAVEATS``).

    The centre and scale are the MEDIAN and a MAD-derived spread, not the mean
    and standard deviation. Measured on the live QB cohort, two extreme rows
    dragged a mean-centred "typical" offset to -21.4 and INVERTED the ranking —
    a 40.6-point gap sorted below a 1.0-point agreement. See
    :func:`_position_moments`.

``agreement`` bands ``delta_z`` into AGREED / MILD / STRONG, or
:data:`NOT_COMPARABLE` when the gap could not be standardized at all — which is
NOT agreement, and used to be reported as it. **The band edges (1.0 and 2.0
standard deviations) are a stated CONVENTION, not a measured threshold** —
nothing has yet graded whether a 2-sigma disagreement predicts anything, which
is a Phase-4 backtest question. They are labelled as such on every row that
carries one, :data:`BAND_LABEL` is the one place to change them, and a test pins
them at their exact edges.

------------------------------------------------------------------------------
WHAT THIS MODULE DELIBERATELY DOES NOT DO
------------------------------------------------------------------------------
* It does not change any existing valuation. Nothing here is wired into
  ``build_valuation``, ``PickEngine``, ``marginal`` or the CLI; it is a new
  read-only surface a caller opts into.
* It does not claim either source is better. :data:`DEFAULT_HOUSE_WEIGHT` is
  0.5 — an EQUAL weight, labelled as a hypothesis with no evidence behind it,
  because none exists yet: neither feed has been graded against a realized
  season. Phase 4 grades that; until then an unequal weight would be a guess
  wearing a decimal point.
* It does not treat a missing second opinion as a zero. A player ESPN does not
  project (roughly half the 2026 pool is deep-bench and unprojected) is
  ``SINGLE_SOURCE_HOUSE`` with the house number passed through unchanged — the
  item-3.2 lesson that an absence of data is not an observation of zero.
* It does not claim to know WHY a second opinion is missing. "ESPN publishes no
  projection for him" and "this identity has no ESPN id, so nothing could be
  looked up" are different facts and are reported differently
  (:data:`NO_ESPN_JOIN_KEY`) — an absence is only a fact when you know it is one
  (item 3.2c). Where a missing crosswalk splits one real player across a
  house-only and an ESPN-only row, both are flagged
  :data:`POSSIBLE_DUPLICATE_IDENTITY` and NEITHER is merged: there is no id to
  merge on, and matching by name is the guess item 2.4's resolver refuses.

------------------------------------------------------------------------------
THE ONE THING TO KNOW BEFORE PASSING ``weeks``
------------------------------------------------------------------------------
Every number here is a per-game RATE expressed on a common horizon — NOT a
forecast of the requested weeks. That distinction is harmless over a full season
and dangerous in a narrow window, because the stored ESPN row is a WHOLE-SEASON
projection with no week-by-week shape at all: its rate projects just as happily
onto a window the player's team spends entirely on bye. Measured at
``weeks=[11]`` on the live board, 93 rows for the six week-11 bye teams carried a
positive blend and four of the top thirteen players were on bye.

So a narrowed window derives, from the house lines already loaded, which weeks
each TEAM actually plays; a player whose team plays none of them is
:data:`NO_GAME_IN_WINDOW` with BOTH sources withheld, and one whose team the
house feed does not carry at all is :data:`WINDOW_PLAYABILITY_UNKNOWN` rather
than quietly priced. The full-season default cannot reach either state and its
behaviour is unchanged.
"""

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ziggurat.core import scoring, valuation
from ziggurat.core.marginal import COVERAGE_FLOOR
from ziggurat.data.nfl import base, espn_projections

#: The house projection source this module treats as the incumbent spine.
HOUSE_SOURCE = "sleeper_rotowire"

#: Label for the second opinion, used in reason text.
ESPN_SOURCE = espn_projections.SOURCE

#: Weight on the HOUSE number in :func:`blend`. A LABELLED HYPOTHESIS, not a fit:
#: neither feed has ever been graded against a realized season, so an equal
#: weight is the only defensible starting point and anything else would be a
#: guess wearing a decimal point. Phase 4's backtest is what can move it.
DEFAULT_HOUSE_WEIGHT = 0.5

BLEND_LABEL = (
    "HYPOTHESIS (untested): the two sources are weighted {house:.0%} house / "
    "{espn:.0%} ESPN. No evidence supports any other split — neither feed has "
    "been graded against a realized season (Phase 4)."
)

#: Agreement bands on |delta_z|. A STATED CONVENTION, not a measured threshold —
#: nothing has graded whether a 2-sigma disagreement predicts anything.
BAND_EDGES: tuple[float, float] = (1.0, 2.0)
BAND_LABEL = (
    "CONVENTION (unvalidated): 'mild' is a gap of at least "
    f"{BAND_EDGES[0]:g} standard deviation for the position and 'strong' at least "
    f"{BAND_EDGES[1]:g}. Whether a strong disagreement predicts anything is an "
    "open Phase-4 question, not a measured fact."
)

# ---- agreement classes -------------------------------------------------------
AGREED = "AGREED"                              # both sources, gap within the band
MILD = "MILD"                                  # both sources, 1-2 sigma apart
STRONG = "STRONG"                              # both sources, >= 2 sigma apart
SINGLE_SOURCE_HOUSE = "SINGLE_SOURCE_HOUSE"    # ESPN publishes no projection
SINGLE_SOURCE_ESPN = "SINGLE_SOURCE_ESPN"      # the house feed does not cover him
UNPRICEABLE = "UNPRICEABLE"                    # neither side can be priced
#: Both sources priced him, but the gap could not be STANDARDIZED (fewer than
#: :data:`MIN_POSITION_N` players at the position, or no spread in their deltas),
#: so how unusual it is cannot be said.
#:
#: This class exists because the alternative was AFFIRMATIVELY WRONG. ``_band``
#: used to return AGREED whenever ``delta_z`` was None, so a pair of opinions
#: that had never been compared was labelled "these two sources agree" in the
#: AGREEMENT column a novice reads. Reproduced with 7 WRs: a 1,200-point gap
#: printed as AGREED. An absence of comparison is not an observation of
#: agreement — the same distinction items 3.2 and 3.2c each paid for.
NOT_COMPARABLE = "NOT_COMPARABLE"
AGREEMENT_CLASSES = frozenset(
    {AGREED, MILD, STRONG, NOT_COMPARABLE,
     SINGLE_SOURCE_HOUSE, SINGLE_SOURCE_ESPN, UNPRICEABLE}
)

# ---- which way the disagreement points --------------------------------------
LEAN_ESPN = "LEAN_ESPN"      # ESPN values the player more than the house feed
LEAN_HOUSE = "LEAN_HOUSE"    # the house feed values the player more
LEAN_NONE = "LEAN_NONE"      # inside the agreement band, or not comparable
LEANS = frozenset({LEAN_ESPN, LEAN_HOUSE, LEAN_NONE})

#: Below this many players at a position, the within-position mean and standard
#: deviation are noise and ``delta_z`` is withheld (None) rather than reported as
#: a number a novice would read as meaningful. K and D/ST run 32 each live, so
#: this only bites on a badly degraded pull.
MIN_POSITION_N = 8

#: Floor on the denominator of ``delta_relative``, in points. Two projections of
#: 0.3 and 0.9 differ by 200% and mean nothing; this keeps a deep-bench rounding
#: difference from topping a report sorted by relative disagreement.
MIN_RELATIVE_DENOMINATOR = 5.0

#: Positions whose cross-source comparison carries a KNOWN structural caveat,
#: disclosed on every row (rule 6) rather than left for the reader to infer.
#: Both sides are house-scored through ``core/scoring.py``, so these are
#: comparability notes, NOT the "two differently-scored numbers" problem.
POSITION_CAVEATS: dict[str, str] = {
    "K": (
        "CAVEAT (kicker) — MEASURED, not speculative: the house feed publishes NO "
        "50+ field-goal bucket at all — 0 of the 104,652 stored kicker projection "
        "rows carry `fg_made_50_59` or `fg_made_60`, while 22,840 carry "
        "`fg_missed` — so every 50-yard-plus make is dropped from the house "
        "number while the misses are still charged. COHORT, stated exactly: that "
        "is the WHOLE `projections` table, which holds SEASON 2026 ONLY from the "
        "single source `sleeper_rotowire` (free historical point-in-time "
        "projections proved infeasible — item 1.5), so this is one season deep "
        "and wide, NOT a multi-season corroboration. Live 2026 week 1: the feed's "
        "own fgm total is 2.11/game against 1.59/game of published buckets, i.e. "
        "~25% of made field goals, ~5 house points each. It is a defect in the "
        "house feed's mapping, not a difference of opinion — so the HOUSE number "
        "on this row is low by roughly 40 points across a season."
    ),
    "DST": (
        "CAVEAT (D/ST): both sides are house-scored, but by structurally different "
        "routes. ESPN publishes the expected NUMBER OF GAMES in each points- and "
        "yards-allowed band, which prices the non-linear brackets as an "
        "expectation; the Sleeper feed publishes a per-week scalar that is "
        "bracketed week by week. Both are legitimate; they are not the same "
        "estimator, so a D/ST gap is partly method, not only opinion."
    ),
}

#: Appended to :data:`POSITION_CAVEATS` only when the row HAS both opinions —
#: the half of the caveat that is a statement about the comparison rather than
#: about the house feed.
#:
#: WHY THE SPLIT (finding [23]): the caveat used to be emitted only when ESPN had
#: an opinion, which put the disclosure exactly where it was redundant (the
#: reader can see the gap) and withheld it where it is load-bearing. Measured on
#: the live 2026 build: 5 kickers are SINGLE_SOURCE_HOUSE — at house ranks 197,
#: 200, 239, 376 and 382, two of them inside the top-200 board — and 0 of them
#: carried the caveat. Those rows are priced ENTIRELY by the defective feed and
#: have no second number to notice the gap from, so they need it most.
POSITION_CAVEATS_BOTH_SOURCES: dict[str, str] = {
    "K": (
        "That defect is why ESPN is higher for EVERY kicker in this build. The "
        "z-score below removes the offset; the raw points gap does not."
    ),
}

#: Shortfall (in games) below the pull's own full-season games count at which
#: ESPN's projection is read as pricing in a known absence. Half a game, so any
#: real reduction trips it while floating-point noise does not.
ABSENCE_GAMES_TOLERANCE = 0.5
ESPN_EXPECTS_ABSENCE = "ESPN_EXPECTS_ABSENCE"

#: Consistency factor turning a median absolute deviation into a standard-deviation
#: -like scale for a normal sample (1 / Phi^-1(0.75)). Used by
#: :func:`_position_moments`; it is a STATISTICAL constant, not a football or
#: scoring number, so rule 2 is untouched.
_MAD_TO_SD = 1.4826

#: This player's team plays NONE of the requested ``weeks`` — a bye that swallows
#: the whole window. See :func:`_team_weeks`.
NO_GAME_IN_WINDOW = "NO_GAME_IN_WINDOW"

#: The window is narrower than a full season and this identity's team could not be
#: found in the house feed at all, so whether he plays inside it is UNKNOWN.
WINDOW_PLAYABILITY_UNKNOWN = "WINDOW_PLAYABILITY_UNKNOWN"

#: This house line carries no ESPN player id, so it could not be LOOKED UP on the
#: ESPN side. Distinct from "ESPN has no projection for him" — see
#: :func:`_espn_key_for`.
NO_ESPN_JOIN_KEY = "NO_ESPN_JOIN_KEY"

#: A no-join-key house row and an ESPN-only row share a (position, team) and may
#: therefore be ONE real player split across two rows. Stated as a possibility,
#: never merged: there is no key to merge on, and guessing by name is the move
#: item 2.4's resolver refuses.
POSSIBLE_DUPLICATE_IDENTITY = "POSSIBLE_DUPLICATE_IDENTITY"


@dataclass(frozen=True)
class SourceOpinion:
    """One projection source's read on one player, on ITS OWN horizon.

    ``points`` is season house points as that source states them; ``games`` is
    how many games that number covers. ``per_game = points / games`` is the only
    quantity comparable across sources — see the module docstring's horizon
    measurement. ``points`` is None when the source cannot price the player at
    all, which is NOT the same as zero and never coerced to it.
    """

    source: str
    points: float | None
    games: float | None
    retrieved_as_of: frozenset[str]
    note: str | None = None

    @property
    def per_game(self) -> float | None:
        if self.points is None or not self.games:
            return None
        return self.points / self.games


@dataclass(frozen=True)
class EnsembleRow:
    """One player's two opinions, the blend, and the disagreement.

    Every ``*_points`` field is HOUSE points on the SAME horizon
    (``horizon_games``, reported on :class:`EnsembleReport`), so they can be
    compared and differenced directly. ``house.points`` / ``espn.points`` inside
    the :class:`SourceOpinion` objects are each source's own un-normalized
    number, kept so a reader can audit the normalization rather than trust it.
    """

    key: tuple
    player: str | None
    position: str                       # canonical QB/RB/WR/TE/DST/K
    team: str | None
    espn_id: str | None
    gsis_id: str | None

    house: SourceOpinion
    espn: SourceOpinion

    house_points: float | None          # house rate on the common horizon
    espn_points: float | None           # ESPN rate on the common horizon
    blended_points: float | None

    delta_points: float | None          # espn_points - house_points
    #: ``|delta| / max(mean(|house|, |espn|), MIN_RELATIVE_DENOMINATOR)``. When
    #: the floor binds this is NOT a percentage of their average, and the reason
    #: text says so and quotes the unfloored figure.
    delta_relative: float | None
    #: ``delta_points`` standardized within position on a MEDIAN centre and a
    #: MAD-derived scale (:func:`_position_moments`). ``None`` when the position
    #: has fewer than ``MIN_POSITION_N`` compared rows, or no spread.
    delta_z: float | None

    #: AGREED / MILD / STRONG / NOT_COMPARABLE / SINGLE_* / UNPRICEABLE
    agreement: str
    lean: str                           # LEAN_ESPN / LEAN_HOUSE / LEAN_NONE
    espn_expected_games: float | None   # ESPN's own games number (availability opinion)
    flags: tuple[str, ...]              # ESPN_EXPECTS_ABSENCE, ...
    house_rank: int | None              # 1-based by house_points desc (the incumbent board)
    reasons: tuple[str, ...]

    @property
    def both_sources(self) -> bool:
        return self.house_points is not None and self.espn_points is not None


@dataclass(frozen=True)
class EnsembleReport:
    """The build's rows plus the context needed to read a single row honestly."""

    rows: tuple[EnsembleRow, ...]
    season: int
    as_of: str
    weeks: tuple[int, ...]
    #: The common horizon every reported season number is expressed on, DERIVED
    #: from the house lines (the most common house game count), never assumed.
    horizon_games: float
    #: The pull's own full-season games count (the most common ESPN games
    #: number); the yardstick ``ESPN_EXPECTS_ABSENCE`` measures against.
    espn_full_season_games: float | None
    house_weight: float
    notes: tuple[str, ...]

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


# ------------------------------------------------------------------ small maths


def _mode(values: Sequence[float]) -> float | None:
    """The most common value, ties broken by the LARGER value.

    Deterministic by construction (rule 9): never depends on dict or input
    order. Used to derive the common horizon rather than hard-coding 16/17.
    """
    if not values:
        return None
    counts: dict[float, int] = {}
    for v in values:
        counts[float(v)] = counts.get(float(v), 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation, or None when it is undefined (n < 2 or no spread)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def blend(house: float | None, espn: float | None, *, house_weight: float) -> float | None:
    """Weighted blend of the two opinions, with a documented single-source rule.

    When only ONE source can price the player the blend is that source's number
    unchanged — never that number scaled by its weight, and never zero. A
    consumer using ``blended_points`` as a drop-in for the house number must not
    silently lose every deep-bench player ESPN does not project (about half the
    live pool).
    """
    if house is None and espn is None:
        return None
    if espn is None:
        return house
    if house is None:
        return espn
    return house_weight * house + (1.0 - house_weight) * espn


# ------------------------------------------------------------------------ build


def _house_opinions(conn, *, as_of, season, weeks, source, rules, view):
    """The incumbent side: ``valuation.weekly_lines`` restricted to ``weeks``.

    COVERAGE, not the point sum, decides whether the house can price a player —
    the item-3.2 rule, reusing its measured ``COVERAGE_FLOOR`` rather than
    inventing a second one. The feed's bye row and its "no forecast" row are
    byte-identical, so a player with one real week and sixteen empty ones sums
    to a real-looking number; pricing that against ESPN would manufacture a
    spectacular false disagreement on exactly the players the report ranks first.
    """
    lines = valuation.weekly_lines(
        conn, as_of=as_of, season=season, weeks=weeks, source=source,
        rules=rules, view=view,
    )
    week_set = set(weeks)
    out = {}
    for key, line in lines.items():
        covered = sorted(set(line.played_weeks) & week_set)
        points = sum(line.points.get(w, 0.0) for w in week_set)
        note = None
        if not covered:
            games, points_or_none = None, None
            note = (
                "the house feed publishes no forecast week for this player in "
                "this window (not a projection of zero)"
            )
        elif len(covered) < COVERAGE_FLOOR * len(week_set - {_bye_week(line, week_set)}):
            games, points_or_none = None, None
            note = (
                f"the house feed covers only {len(covered)} of {len(week_set)} weeks "
                f"(below the {COVERAGE_FLOOR:.0%} coverage floor): its bye row and its "
                "'no forecast' row are identical, so this total cannot be trusted"
            )
        else:
            games, points_or_none = float(len(covered)), points
        out[key] = (line, SourceOpinion(
            source=source, points=points_or_none, games=games,
            retrieved_as_of=line.retrieved_as_of, note=note,
        ))
    return out


def _bye_week(line, week_set):
    """The single week in ``week_set`` this line has no game for, or None.

    Derived from the line itself (``played_weeks``) rather than from a schedule
    read, so this helper stays pure and cheap. With exactly one missing week the
    coverage denominator is the playable weeks, matching ``marginal``'s rule;
    with more than one missing week the player fails the floor anyway.
    """
    missing = sorted(week_set - set(line.played_weeks))
    return missing[0] if len(missing) == 1 else None


def _espn_opinions(conn, *, as_of, season, view, rules):
    """The second opinion: stored ESPN season projections, keyed by ``espn_key``.

    Priced through ``espn_projections.house_points`` — NEVER a bare
    ``scoring.score``, which silently omits a defence's bracket contribution
    (worth -45.8 to +27.9 points across the live 32).
    """
    rows = espn_projections.get_espn_projections(
        conn, as_of=as_of, season=season, week=espn_projections.SEASON_WEEK, view=view,
    )
    out: dict[str, dict] = {}
    for row in rows:
        games = row["projected_games"]
        note = None
        points = espn_projections.house_points(row, rules=rules)
        if not games:
            points, note = None, (
                "ESPN published a projection but no games count, so it cannot be "
                "put on a comparable horizon"
            )
        out[str(row["espn_key"])] = {
            "opinion": SourceOpinion(
                source=ESPN_SOURCE, points=points,
                games=float(games) if games else None,
                retrieved_as_of=frozenset({row["retrieved_as_of"]}), note=note,
            ),
            "player": row["player"],
            "position": valuation.canon_position(row["position"]),
            "team": row["team"],
            "espn_id": row["espn_id"],
            "gsis_id": row["gsis_id"],
        }
    return out


def _team_weeks(house) -> dict[str, frozenset[int]]:
    """``team -> the weeks the house feed shows that team playing``.

    Derived from the house lines ALREADY LOADED — no second read, no new accessor
    and therefore no new leakage surface: it inherits ``as_of`` and ``view`` from
    the read that produced them. A week counts as played for a team if ANY of its
    players carries an opponent that week, which is robust to an individual
    player's missing coverage (the item-3.2 trap: one player's bye row and his
    "no forecast" row are byte-identical, but a whole team's are not).

    VERIFIED against the independently measured 2026 bye calendar: on the live
    board this resolves all 32 teams and names exactly ATL/CLE/GB/LA/NE/SEA as
    having no week-11 game — the six-team week-11 bye, with no false positives.
    """
    out: dict[str, set[int]] = {}
    for line, _op in house.values():
        if line.team:
            out.setdefault(line.team, set()).update(line.played_weeks)
    return {team: frozenset(weeks) for team, weeks in out.items()}


def _espn_key_for(line) -> str | None:
    """The join key from a house line: team abbr for a D/ST, ESPN id otherwise.

    This is migration 004's D/ST contract, unchanged: ESPN gives team defences
    synthetic NEGATIVE ids, so they are keyed by team on both sides. Verified
    live: the 32 house D/ST teams and the 32 ESPN D/ST teams match exactly, with
    no team on one side only.

    ``None`` means THERE IS NOTHING TO LOOK HIM UP BY — a house line with no
    ``espn_id`` (9 priced rows on the live 2026 board, keyed ``('SPID', ...)``,
    at house ranks 197-533). That is a missing crosswalk, NOT evidence about what
    ESPN publishes, and the two must not be reported with the same sentence:
    see :data:`NO_ESPN_JOIN_KEY`.
    """
    if line.position == "DST":
        return line.team
    return line.espn_id


def build_ensemble(
    conn,
    *,
    as_of,
    season: int,
    weeks: Iterable[int] | None = None,
    house_source: str = HOUSE_SOURCE,
    house_weight: float = DEFAULT_HOUSE_WEIGHT,
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    view: base.AsOfView = "historical",
) -> EnsembleReport:
    """Join the two projection sources and measure their disagreement.

    ``as_of`` is keyword-only with no default (rule 1) and THREADS STRAIGHT into
    both reads — ``valuation.weekly_lines`` and
    ``espn_projections.get_espn_projections`` — as does ``view``. This layer
    never widens the gate: a caller that wants bulk history binds
    ``base.latest_truth`` around this function, which forces ``view`` on both
    sides at once rather than letting one side leak.

    Returns an :class:`EnsembleReport` sorted by ``blended_points`` descending
    (ties broken by key, so the order is deterministic). Selection cohorts for
    :func:`source_correlation` use ``house_rank`` — a rank on the INCUMBENT
    board — so a "top 200" slice is not selected by the very quantity being
    evaluated.
    """
    if not 0.0 <= house_weight <= 1.0:
        raise ValueError(f"house_weight must be in [0, 1]; got {house_weight!r}")

    week_tuple = tuple(sorted(set(valuation.DEFAULT_WEEKS if weeks is None else weeks)))
    if not week_tuple:
        raise ValueError("build_ensemble needs at least one week")

    house = _house_opinions(
        conn, as_of=as_of, season=season, weeks=week_tuple,
        source=house_source, rules=rules, view=view,
    )
    espn = _espn_opinions(conn, as_of=as_of, season=season, view=view, rules=rules)

    # --- the common horizon, DERIVED (see the module docstring) --------------
    house_horizon = _mode([op.games for _line, op in house.values() if op.games])
    horizon = house_horizon
    espn_full = _mode([e["opinion"].games for e in espn.values() if e["opinion"].games])
    if horizon is None:
        # No priceable house line at all (e.g. a pre-ingest database). Fall back
        # to ESPN's own horizon so the ESPN-only rows are still expressed
        # honestly, and say so.
        horizon = espn_full

    # --- assemble one record per identity, from BOTH sides -------------------
    records: list[dict] = []
    matched_keys: set[str] = set()
    unjoinable = 0
    for key, (line, opinion) in house.items():
        ekey = _espn_key_for(line)
        entry = espn.get(ekey) if ekey else None
        if entry is not None:
            matched_keys.add(ekey)
        if ekey is None:
            # An absence is only a fact when you know it is one (item 3.2c). We
            # have established that this identity cannot be LOOKED UP, not that
            # ESPN is silent about the player.
            #
            # Counted and flagged only where the HOUSE can price him. Most of the
            # 3,241 identities on a live build are deep-bench rows the house feed
            # has no forecast for either (2,638 UNPRICEABLE); flagging their
            # missing crosswalk too would put the marker on 1,273 rows and bury
            # the 9 that actually carry a number a reader could misread.
            if opinion.points is not None:
                unjoinable += 1
            espn_op = SourceOpinion(
                source=ESPN_SOURCE, points=None, games=None,
                retrieved_as_of=frozenset(),
                note=(
                    "this house identity carries no ESPN player id, so it could "
                    "not be looked up on the ESPN board at all. That is a MISSING "
                    "CROSSWALK, not a statement that ESPN has no projection for "
                    "him — and the same real player may also appear on this "
                    "report as a separate ESPN-only row"
                ),
            )
        elif entry is None:
            espn_op = SourceOpinion(
                source=ESPN_SOURCE, points=None, games=None,
                retrieved_as_of=frozenset(),
                note=("ESPN was looked up by this player's ESPN id and publishes "
                      "no season projection for him"),
            )
        else:
            espn_op = entry["opinion"]
        records.append({
            "key": key,
            "player": line.player or (entry or {}).get("player"),
            "position": line.position,
            "team": line.team or (entry or {}).get("team"),
            "espn_id": line.espn_id,
            "gsis_id": line.gsis_id,
            "house": opinion,
            "espn": espn_op,
            "no_join_key": ekey is None and opinion.points is not None,
        })
    # ESPN-only identities: a real second-source gain (a player the incumbent
    # board cannot see at all), so they are surfaced rather than dropped.
    for ekey, entry in sorted(espn.items()):
        if ekey in matched_keys or entry["position"] is None:
            continue
        records.append({
            "key": ("ESPN", ekey),
            "player": entry["player"],
            "position": entry["position"],
            "team": entry["team"],
            "espn_id": entry["espn_id"],
            "gsis_id": entry["gsis_id"],
            "house": SourceOpinion(
                source=house_source, points=None, games=None,
                retrieved_as_of=frozenset(),
                note="the house feed has no projection row for this player",
            ),
            "espn": entry["opinion"],
            "espn_only": True,
        })

    # --- POSSIBLE duplicate identities, stated but never merged --------------
    # A no-join-key house row and an ESPN-only row that share a (position, team)
    # may be one real player split across two rows. Measured live: the priceable
    # kicker board carries 37 rows for 32 teams (GB 2, NYG 3, WAS 2, NO 2), and
    # the GB pair is provably one roster slot. They are FLAGGED, not merged —
    # there is no key to merge on and matching by name is what item 2.4's
    # resolver refuses to do.
    orphan_slots = {(r["position"], r["team"]) for r in records if r.get("no_join_key")}
    espn_slots = {(r["position"], r["team"]) for r in records
                  if r.get("espn_only") and r["espn"].points is not None}
    duplicate_slots = {s for s in orphan_slots & espn_slots if s[1] is not None}
    for rec in records:
        rec["possible_duplicate"] = (
            (rec.get("no_join_key") or rec.get("espn_only"))
            and (rec["position"], rec["team"]) in duplicate_slots
        )

    # --- does this player's team play AT ALL inside the requested window? ----
    #
    # THE DEFECT THIS CLOSES. The stored ESPN row is a WHOLE-SEASON projection,
    # so its per-game rate is happily projectable onto any window — including a
    # window the player's team spends entirely on bye. The house side correctly
    # refuses (no covered week), but the ESPN rate then filled the hole and the
    # row came out as a confident positive number labelled SINGLE_SOURCE_ESPN.
    # Measured on the live board at `weeks=[11]`: 93 rows for the six week-11 bye
    # teams carried a positive blend, and FOUR of the top thirteen players on the
    # board were on bye — Puka Nacua 20.96 at #4, Bijan Robinson 20.75 at #5 —
    # each with `flags=()` and a reason reading "not a projection of zero".
    # `weeks` is a documented public parameter, so a streaming/weekly caller
    # reached that with no guard and nothing to smell (rule 6).
    #
    # Only a NARROWED window can do this, so the full-season default skips the
    # derivation entirely and its behaviour is untouched.
    week_set = set(week_tuple)
    narrowed = week_set != set(valuation.DEFAULT_WEEKS)
    team_weeks = _team_weeks(house) if narrowed else {}
    no_game_rows = unknown_window_rows = 0
    if narrowed:
        for rec in records:
            playable = team_weeks.get(rec["team"])
            if playable is None:
                if rec["espn"].points is not None and rec["house"].points is None:
                    # We cannot show he plays and the incumbent feed cannot price
                    # him: exactly the shape that produced the bye rows above.
                    rec["window_unknown"] = True
                    unknown_window_rows += 1
                continue
            if playable & week_set:
                continue
            rec["no_game_in_window"] = True
            no_game_rows += 1
            rec["espn"] = SourceOpinion(
                source=ESPN_SOURCE, points=None, games=None,
                retrieved_as_of=rec["espn"].retrieved_as_of,
                note=(
                    f"ESPN's stored projection is a WHOLE-SEASON line, and the "
                    f"house feed shows NO GAME for this player's team "
                    f"({rec['team']}) in any of weeks {week_tuple[0]}-"
                    f"{week_tuple[-1]} — normally a bye, though strictly what is "
                    "OBSERVED is that the feed carries no opponent for the team "
                    "here. Either way a season rate says nothing about a week "
                    "that is not played, so it is NOT scaled into this window"
                ),
            )

    # --- put both sources on the common horizon ------------------------------
    for rec in records:
        rec["house_points"] = _on_horizon(rec["house"], horizon)
        rec["espn_points"] = _on_horizon(rec["espn"], horizon)
        h, e = rec["house_points"], rec["espn_points"]
        rec["delta_points"] = None if (h is None or e is None) else e - h
        rec["blended_points"] = blend(h, e, house_weight=house_weight)

    # --- within-position standardization (removes the systematic offset) -----
    moments = _position_moments(records)

    # --- the incumbent board's rank, used for unbiased cohort selection ------
    ranked = sorted(
        (r for r in records if r["house_points"] is not None),
        key=lambda r: (-r["house_points"], str(r["key"])),
    )
    for i, rec in enumerate(ranked, start=1):
        rec["house_rank"] = i

    rows = tuple(sorted(
        (_finish(rec, moments=moments, horizon=horizon, espn_full=espn_full,
                 house_weight=house_weight, house_source=house_source)
         for rec in records),
        key=lambda r: (-(r.blended_points if r.blended_points is not None else -1e18),
                       str(r.key)),
    ))

    notes = [
        BLEND_LABEL.format(house=house_weight, espn=1.0 - house_weight),
        BAND_LABEL,
    ]
    if horizon:
        # DERIVED, and the illustration must be derived too: this note used to
        # assert "the house (16 games, weeks 1-17) and ESPN (17 games) ... ~6%"
        # verbatim regardless of the window, so the module's own 14-week test
        # world printed "a COMMON horizon of 14 games" and then a false sentence
        # about 16 and 17 — in the one note whose job is to stop a novice
        # misreading the numbers.
        house_from = "the house lines" if house_horizon is not None else "ESPN's own"
        detail = (
            f"Both sides are per-game rates multiplied by {horizon:g}. The horizon "
            f"is DERIVED from {house_from} games count, never assumed."
        )
        if house_horizon and espn_full and abs(espn_full - house_horizon) > 1e-9:
            gap = abs(espn_full - house_horizon) / house_horizon
            detail += (
                f" It matters: the house covers {house_horizon:g} games here and "
                f"ESPN projects {espn_full:g}, so raw season totals would differ by "
                f"~{gap:.0%} for calendar reasons alone."
            )
        if house_horizon is None:
            detail += (
                " NOTE THE SOURCE: no house line could be priced at all, so the "
                "horizon came from ESPN ALONE and there is no incumbent number on "
                "this report to compare against."
            )
        notes.insert(0, (
            f"Season points are expressed on a COMMON horizon of {horizon:g} games "
            f"for both sources; each source's own games count is on the row. {detail}"
        ))
    else:
        notes.insert(0, (
            "NO comparison horizon could be derived: neither source can price a "
            "single player at this as_of, so every row is UNPRICEABLE. This is the "
            "correct reading of an empty or pre-ingest database, not a failure."
        ))
    unpriceable = sum(1 for r in rows if r.agreement == UNPRICEABLE)
    if unpriceable:
        notes.append(
            f"{unpriceable} of {len(rows)} rows are UNPRICEABLE — identities the "
            "house feed carries with no forecast week at all in this window. They "
            "are kept rather than dropped (an absence is a fact worth seeing) and "
            "sort last; filter on `agreement` or slice by `house_rank`."
        )
    if espn_full is None:
        notes.append(
            "NO ESPN projections are stored at this as_of — every row is "
            "SINGLE_SOURCE_HOUSE and there is no second opinion to disagree with."
        )
    if narrowed:
        notes.append(
            f"NARROWED WINDOW (weeks {week_tuple[0]}-{week_tuple[-1]}, "
            f"{len(week_tuple)} of {len(valuation.DEFAULT_WEEKS)}): every number here "
            "is still a per-game RATE expressed on the common horizon, NOT a forecast "
            "of what these specific weeks will produce. ESPN's stored line is a "
            "whole-season projection and has no week-by-week shape at all."
        )
    if no_game_rows:
        notes.append(
            f"{no_game_rows} rows are flagged {NO_GAME_IN_WINDOW}: the house feed "
            "shows their team with no opponent in ANY requested week (normally a bye). "
            "Both sources are withheld for them rather than filled in from the ESPN "
            "season rate. Team playability is derived from the house lines already "
            "loaded — no second read — by unioning every player on a team, so one "
            "player's missing coverage cannot strand his whole team."
        )
    if unknown_window_rows:
        notes.append(
            f"{unknown_window_rows} rows are flagged {WINDOW_PLAYABILITY_UNKNOWN}: the "
            "house feed carries no line for their team, so whether they play inside "
            "this window could not be checked. Their ESPN number is a season rate."
        )
    if unjoinable:
        notes.append(
            f"{unjoinable} house identities carry NO ESPN JOIN KEY (no espn_id), so "
            "they could not be looked up on the ESPN side AT ALL — that is a missing "
            "crosswalk, not a finding about ESPN. Where the same (position, team) also "
            f"appears as an ESPN-only row, BOTH are flagged {POSSIBLE_DUPLICATE_IDENTITY}: "
            "one real player can appear twice, so a per-position count on this report "
            "can exceed the real number of players (measured live: 37 priceable kicker "
            "rows for 32 teams)."
        )
    absent = sum(1 for r in rows if ESPN_EXPECTS_ABSENCE in r.flags)
    if absent:
        notes.append(
            f"{absent} rows are flagged {ESPN_EXPECTS_ABSENCE}: ESPN projects them for "
            "FEWER than a full season, and because every number here is a per-game rate "
            "on a common horizon, their ESPN and BLEND columns are that partial "
            "projection EXTRAPOLATED to the full horizon (a 2-game projection becomes "
            f"a {horizon:g}-game one). The availability opinion is deliberately kept out of "
            "the points — read the FLAGS column, and the row's own reasons, before "
            "treating those two numbers as a season forecast."
        )
    return EnsembleReport(
        rows=rows, season=season, as_of=str(as_of), weeks=week_tuple,
        horizon_games=float(horizon) if horizon else 0.0,
        espn_full_season_games=espn_full, house_weight=house_weight,
        notes=tuple(notes),
    )


def _on_horizon(opinion: SourceOpinion, horizon: float | None) -> float | None:
    """A source's season points re-expressed on the common horizon."""
    rate = opinion.per_game
    if rate is None or not horizon:
        return None
    return rate * horizon


def _position_moments(records) -> dict[str, tuple[float, float, int]]:
    """Per-position ``(centre, scale, n)`` of ``delta_points`` — MEDIAN and a
    MAD-derived scale, deliberately NOT mean and standard deviation.

    Standardizing within position is what turns a raw points gap into a risk
    signal: it removes the systematic per-source offset (a property of the two
    feeds, not of any player) and it puts a 20-point gap on a 370-point QB and a
    20-point gap on a 60-point defence on one scale.

    WHY ROBUST STATISTICS, measured. The first version centred on the mean and
    scaled by the population standard deviation — both computed from the SAME
    sample that contains the extreme rows they are supposed to make extreme. On
    the live 2026 QB cohort (n=32) two rows at -148.4 and -116.5 dragged the
    "typical" offset to **-21.4** with sd 33.1, against a median of **-15.2** and
    a MAD-derived scale of **21.3**. The ranking then INVERTED: a 40.6-point gap
    (Brissett, z -0.58) sorted BELOW a 1.0-point agreement (z +0.68) in a report
    headed "the largest disagreements within each position", and the same numbers
    leaked onto the headline table where the per-row reason tells the novice "the
    standardized figure is the one to trust". A single outlier must not be able
    to redefine what typical means for the position it is an outlier in.

    The median and the MAD both have a 50% breakdown point, so the two extreme
    QBs move the centre by a rank rather than by their magnitude. ``_MAD_TO_SD``
    rescales the MAD so the numbers stay readable as "standard deviations" and
    :data:`BAND_EDGES` keeps its meaning on a clean sample.

    Falls back to ``pstdev`` ONLY when the MAD is exactly zero (more than half
    the position shares one delta, which no live cohort does); a position with no
    spread at all is dropped, exactly as before, so ``delta_z`` is withheld rather
    than divided by zero.
    """
    by_pos: dict[str, list[float]] = {}
    for rec in records:
        if rec["delta_points"] is not None:
            by_pos.setdefault(rec["position"], []).append(rec["delta_points"])
    moments = {}
    for pos, deltas in by_pos.items():
        if len(deltas) < MIN_POSITION_N:
            continue
        centre = statistics.median(deltas)
        mad = statistics.median([abs(d - centre) for d in deltas])
        scale = mad * _MAD_TO_SD
        if scale <= 0.0:
            # Degenerate MAD (>= half the cohort on one value). Fall back to the
            # non-robust scale rather than withhold z from a position that does
            # have spread; the centre stays the median either way.
            scale = statistics.pstdev(deltas)
        if scale <= 0.0:
            continue
        moments[pos] = (centre, scale, len(deltas))
    return moments


def _finish(rec, *, moments, horizon, espn_full, house_weight, house_source) -> EnsembleRow:
    """Turn an assembled record into a finished, fully-explained row."""
    pos = rec["position"]
    h, e, delta = rec["house_points"], rec["espn_points"], rec["delta_points"]

    relative = z = None
    if delta is not None:
        denom = max((abs(h) + abs(e)) / 2.0, MIN_RELATIVE_DENOMINATOR)
        relative = abs(delta) / denom
        moment = moments.get(pos)
        if moment is not None:
            mean, sd, _n = moment
            z = (delta - mean) / sd

    if h is None and e is None:
        agreement, lean = UNPRICEABLE, LEAN_NONE
    elif e is None:
        agreement, lean = SINGLE_SOURCE_HOUSE, LEAN_NONE
    elif h is None:
        agreement, lean = SINGLE_SOURCE_ESPN, LEAN_NONE
    else:
        agreement = _band(z)
        if agreement in (AGREED, NOT_COMPARABLE):
            lean = LEAN_NONE
        else:
            # Direction must come from the SAME quantity the BAND came from,
            # or the two contradict each other. Measured case that forced this:
            # every kicker's raw delta is positive (the house feed drops all 50+
            # field goals), so a kicker only +8.7 above the house sits a full
            # standard deviation BELOW the position's typical +21 gap. Reading
            # the sign off `delta` there would print "ESPN likes him more" on a
            # row flagged unusual precisely because ESPN likes him LESS than it
            # likes his peers. A novice cannot smell that (rule 6).
            lean = LEAN_ESPN if z > 0 else LEAN_HOUSE  # z is never None here

    flags: list[str] = []
    espn_games = rec["espn"].games
    if (espn_games is not None and espn_full is not None
            and espn_full - espn_games > ABSENCE_GAMES_TOLERANCE):
        flags.append(ESPN_EXPECTS_ABSENCE)
    if rec.get("no_game_in_window"):
        flags.append(NO_GAME_IN_WINDOW)
    if rec.get("window_unknown"):
        flags.append(WINDOW_PLAYABILITY_UNKNOWN)
    if rec.get("no_join_key"):
        flags.append(NO_ESPN_JOIN_KEY)
    if rec.get("possible_duplicate"):
        flags.append(POSSIBLE_DUPLICATE_IDENTITY)

    return EnsembleRow(
        key=rec["key"], player=rec["player"], position=pos, team=rec["team"],
        espn_id=rec["espn_id"], gsis_id=rec["gsis_id"],
        house=rec["house"], espn=rec["espn"],
        house_points=h, espn_points=e, blended_points=rec["blended_points"],
        delta_points=delta, delta_relative=relative, delta_z=z,
        agreement=agreement, lean=lean, espn_expected_games=espn_games,
        flags=tuple(flags), house_rank=rec.get("house_rank"),
        reasons=_reasons(rec, agreement=agreement, lean=lean, z=z, relative=relative,
                         horizon=horizon, espn_full=espn_full, flags=flags,
                         house_weight=house_weight, house_source=house_source,
                         moments=moments),
    )


def _band(z: float | None) -> str:
    """Classify a standardized gap. ``None`` is NOT agreement — see
    :data:`NOT_COMPARABLE`."""
    if z is None:
        return NOT_COMPARABLE  # never compared; saying "AGREED" would be a claim
    mild, strong = BAND_EDGES
    if abs(z) >= strong:
        return STRONG
    if abs(z) >= mild:
        return MILD
    return AGREED


def _fmt_stamps(stamps: frozenset[str]) -> str:
    return ", ".join(sorted(s for s in stamps if s)) or "unknown"


def _reasons(rec, *, agreement, lean, z, relative, horizon, espn_full, flags,
             house_weight, house_source, moments) -> tuple[str, ...]:
    """Plain-language reasons (rule 6). The operator is a football novice and
    cannot smell an absurd output, so every number a human sees is accompanied
    by what it is, where it came from, and what would make it wrong."""
    out: list[str] = []
    pos = rec["position"]
    house_op, espn_op = rec["house"], rec["espn"]
    h, e, delta = rec["house_points"], rec["espn_points"], rec["delta_points"]

    if h is not None:
        out.append(
            f"House feed ({house_source}): {house_op.points:.1f} pts over "
            f"{house_op.games:g} games = {house_op.per_game:.2f}/game -> {h:.1f} on the "
            f"{horizon:g}-game comparison horizon. Pulled {_fmt_stamps(house_op.retrieved_as_of)}."
        )
    elif house_op.note:
        out.append(f"House feed ({house_source}): NO USABLE NUMBER — {house_op.note}.")

    if e is not None:
        out.append(
            f"ESPN: {espn_op.points:.1f} pts over {espn_op.games:g} games = "
            f"{espn_op.per_game:.2f}/game -> {e:.1f} on the {horizon:g}-game horizon. "
            f"Re-derived from ESPN's projected stat line through the house scoring "
            f"rules, not copied from ESPN's own total. Pulled "
            f"{_fmt_stamps(espn_op.retrieved_as_of)}."
        )
    elif espn_op.note:
        out.append(f"ESPN: NO USABLE NUMBER — {espn_op.note}.")

    if agreement in (SINGLE_SOURCE_HOUSE, SINGLE_SOURCE_ESPN):
        if NO_ESPN_JOIN_KEY in flags:
            out.append(
                "ONE SOURCE ONLY — and NOT because ESPN is silent: this identity "
                "has no ESPN id to look him up by, so no second opinion could be "
                "FETCHED. The number is passed through unchanged and carries "
                "single-source risk, but nothing here has established what ESPN "
                "thinks of this player."
            )
        else:
            out.append(
                "ONE SOURCE ONLY: there is no second opinion here, so this number "
                "carries exactly the single-source risk this module exists to "
                "measure. It is passed through unchanged — an absence of data is "
                "not an observation of zero."
            )
    elif agreement == UNPRICEABLE:
        out.append("NEITHER source can price this player; nothing here is usable.")
    else:
        direction = "ESPN is higher" if delta > 0 else "the house feed is higher"
        average = (abs(h) + abs(e)) / 2.0
        if average < MIN_RELATIVE_DENOMINATOR:
            # The floor binds, so `relative` is NOT "% of their average" and must
            # not be labelled as one. Measured: 21 live rows print a floored
            # figure; Charlie Jones showed 18% against an actual 33%.
            # `average` can be exactly 0 (both sources price him at nothing), and
            # the unfloored ratio is then undefined rather than large.
            unfloored = (f"the unfloored figure would be {abs(delta) / average:.0%}"
                         if average > 0.0 else
                         "there is no unfloored figure: both sources price him at "
                         "0.0, so the ratio is undefined rather than large")
            out.append(
                f"Disagreement: {delta:+.1f} pts ({direction}). Shown as "
                f"{relative:.0%} of a {MIN_RELATIVE_DENOMINATOR:g}-point FLOOR, not "
                f"of these two projections' actual average of {average:.1f} pts — "
                f"{unfloored}. The floor exists so a rounding difference between "
                "two near-zero deep-bench projections cannot top a report sorted "
                "by relative gap."
            )
        else:
            out.append(
                f"Disagreement: {delta:+.1f} pts ({direction}), {relative:.0%} of the "
                f"two sources' average of {average:.1f} pts."
            )
        if z is None:
            out.append(
                f"NOT COMPARABLE: fewer than {MIN_POSITION_N} {pos} players have both "
                "opinions in this build (or they show no spread at all), so this gap "
                "could not be standardized and 'how unusual' cannot be said. That is "
                "why the agreement column reads NOT_COMPARABLE rather than AGREED — "
                "the two numbers above have not been compared, which is not the same "
                "as their agreeing."
            )
        else:
            centre, scale, n = moments[pos]
            out.append(
                f"Across all {n} {pos}s here the two sources TYPICALLY differ by "
                f"{centre:+.1f} pts (median; robust spread {scale:.1f}) — that offset "
                "is a property of the two FEEDS, not of this player, so it is "
                "subtracted before judging how unusual this gap is. Median and MAD "
                "rather than mean and standard deviation, so that one extreme row "
                "cannot redefine what is typical for the position it is extreme in."
            )
            direction_z = "ESPN unusually HIGH" if z > 0 else "ESPN unusually LOW"
            out.append(
                f"After removing it, this gap is {abs(z):.1f} standard deviations "
                f"from typical ({direction_z} for a {pos}), which reads as "
                f"{agreement.lower()} disagreement. Measured in this build, not a "
                f"stored prior. {BAND_LABEL}"
            )
            if delta * z < 0:
                out.append(
                    f"READ THIS CAREFULLY: the raw gap says ESPN is "
                    f"{'higher' if delta > 0 else 'lower'}, but relative to how "
                    f"the two feeds normally differ at {pos} it is the other way "
                    "round. The standardized figure is the one to trust."
                )
        if agreement == STRONG:
            out.append(
                "RISK NOTE: a player only ONE source is enthusiastic about is a "
                "riskier pick than one both agree on, at the same projected "
                "points. This says nothing about who is right."
            )

    if POSSIBLE_DUPLICATE_IDENTITY in flags:
        out.append(
            f"POSSIBLE DUPLICATE: another row on this report is the same position "
            f"({pos}) on the same team ({rec['team']}) and comes from the other "
            "source, so the two MAY be one real player split in two by the missing "
            "crosswalk. They are not merged, because there is no id to merge on and "
            "matching by name is exactly the guess this system refuses to make. "
            "Treat the pair as one roster slot until the crosswalk is fixed."
        )

    if WINDOW_PLAYABILITY_UNKNOWN in flags:
        out.append(
            "WINDOW UNVERIFIED: this is a narrowed week window, and the house feed "
            "carries no line for this player's team at all, so whether he even "
            "plays inside the window is UNKNOWN. The ESPN number below is a "
            "whole-season rate; if the team is on bye here it is an answer to a "
            "different question."
        )

    if NO_GAME_IN_WINDOW in flags:
        out.append(
            "NO GAME IN THIS WINDOW: the house feed shows this player's team with no "
            "opponent in ANY of the requested weeks — a bye that swallows the window. "
            "Neither source is priced here, and the ESPN season rate is deliberately "
            "NOT scaled into it: a rate per game says nothing about a game that is "
            "not played. DO NOT read the blank as a low projection; read it as 'he is "
            "not playing'. (The observation is about the FEED: if the feed were "
            "missing this team's whole week it would look the same, which is why "
            "nothing here is priced rather than priced at zero.)"
        )

    if ESPN_EXPECTS_ABSENCE in flags:
        missing = espn_full - espn_op.games
        out.append(
            f"AVAILABILITY (a separate opinion, deliberately NOT folded into the "
            f"points): ESPN projects {espn_op.games:g} games where its full season "
            f"is {espn_full:g} — it expects this player to miss about "
            f"{missing:.0f}. The house feed is a flat season rate and has no "
            "availability column at all, so it cannot agree or disagree."
        )

    # The caveat is a fact about the FEED, so it is emitted whenever the position
    # has one — including on rows priced by the house feed alone, where the reader
    # has no second number to notice the defect from (see
    # POSITION_CAVEATS_BOTH_SOURCES for why this used to be backwards).
    caveat = POSITION_CAVEATS.get(pos)
    if caveat and (h is not None or e is not None):
        both = POSITION_CAVEATS_BOTH_SOURCES.get(pos)
        if both and h is not None and e is not None:
            out.append(f"{caveat} {both}")
        else:
            out.append(caveat)

    if h is not None and e is not None:
        out.append(
            BLEND_LABEL.format(house=house_weight, espn=1.0 - house_weight)
        )
    return tuple(out)


# ----------------------------------------------------------------- correlation


@dataclass(frozen=True)
class CorrelationReport:
    """How much the two sources agree overall, in aggregate rather than per row."""

    cohort: str
    n: int
    correlation: float | None
    #: MEDIAN of espn / house on the common horizon. Direction matters and is
    #: pinned by a test: > 1.0 means ESPN is systematically HIGHER than the house.
    median_ratio: float | None
    #: ``position -> (n, correlation)``. The correlation is ``None`` when
    #: ``n < MIN_POSITION_N`` — withheld, not undefined.
    by_position: Mapping[str, tuple[int, float | None]]


def source_correlation(report: EnsembleReport, *, top: int | None = None) -> CorrelationReport:
    """Pearson correlation between the two sources over a cohort.

    ``top`` selects by ``house_rank`` — a rank on the INCUMBENT board — so the
    cohort is not chosen by the blended number being evaluated. ``top=200``
    answers "over the 200 players the current engine ranks highest, how much do
    the two sources agree".
    """
    rows = [r for r in report.rows if r.both_sources]
    if top is not None:
        rows = [r for r in rows if r.house_rank is not None and r.house_rank <= top]
    rows.sort(key=lambda r: (r.house_rank or 0, str(r.key)))

    h = [r.house_points for r in rows]
    e = [r.espn_points for r in rows]
    ratios = [b / a for a, b in zip(h, e, strict=True) if a > MIN_RELATIVE_DENOMINATOR]

    by_pos: dict[str, tuple[int, float | None]] = {}
    grouped: dict[str, list[EnsembleRow]] = {}
    for r in rows:
        grouped.setdefault(r.position, []).append(r)
    for pos in sorted(grouped):
        group = grouped[pos]
        # WITHHELD below MIN_POSITION_N, for the same reason and by the same
        # constant that withholds `delta_z`: a Pearson r on a handful of rows is
        # noise a novice reads as a finding. Measured live at top=200, the DST
        # cohort is 5 players and printed r=0.2978 ("the two sources barely agree
        # on defences") while the full 32-defence figure is r=0.8271.
        by_pos[pos] = (
            len(group),
            pearson([r.house_points for r in group], [r.espn_points for r in group])
            if len(group) >= MIN_POSITION_N else None,
        )
    return CorrelationReport(
        cohort="all" if top is None else f"top {top} by house rank",
        n=len(rows),
        correlation=pearson(h, e),
        median_ratio=statistics.median(ratios) if ratios else None,
        by_position=by_pos,
    )


def top_disagreements(
    report: EnsembleReport,
    *,
    top: int | None = 200,
    per_position: int = 15,
) -> dict[str, list[EnsembleRow]]:
    """The largest disagreements within each position, most unusual first.

    Ranked by ``|delta_z|`` (how unusual the gap is FOR THAT POSITION), not by
    raw points — a raw-points ranking would list only QBs, because a QB's whole
    scale is larger. ``top`` restricts the cohort by ``house_rank`` first, so
    this reports on players the engine would actually consider drafting rather
    than on deep-bench noise.
    """
    grouped: dict[str, list[EnsembleRow]] = {}
    for row in report.rows:
        if not row.both_sources or row.delta_z is None:
            continue
        if top is not None and (row.house_rank is None or row.house_rank > top):
            continue
        grouped.setdefault(row.position, []).append(row)
    return {
        pos: sorted(rows, key=lambda r: (-abs(r.delta_z), str(r.key)))[:per_position]
        for pos, rows in sorted(grouped.items())
    }


# --------------------------------------------------------------------- display


#: The headline table's columns.
#:
#: FLAGS is not decoration. Without it the module's claim that ESPN's availability
#: opinion "is surfaced as its own field and its own reason line" was untrue of the
#: only table it ships: a player ESPN projects for 2 games printed his rate
#: EXTRAPOLATED to the full horizon (8x) in the ESPN and BLEND columns, sorted high
#: on the board, with nothing on the row to smell (rule 6). Live 2026 carries ten
#: such players under a full season. The same column now also carries the
#: window/bye and missing-crosswalk flags.
_COLUMNS = (
    ("PLAYER", 24), ("POS", 4), ("TM", 4), ("HOUSE", 8), ("ESPN", 8),
    ("BLEND", 8), ("DELTA", 8), ("Z", 6), ("AGREEMENT", 16), ("FLAGS", 34),
)

#: Decimal places on the displayed ``delta_z``.
#:
#: TWO, not one, and it is a correctness fix rather than a taste one. The bands
#: turn at exactly 1.0 and 2.0, so rounding z to one place puts rows on either
#: side of an edge under one printed number: measured on the live board there are
#: FOUR such collisions, including two WRs both printing z -1.0 where one reads
#: AGREED and the other MILD. A novice cannot do anything with that except
#: distrust the table (rule 6). Two places separate every live pair.
_Z_DECIMALS = 2


def _cell(value, width: int, *, decimals: int = 1) -> str:
    if value is None:
        text = "-"
    elif isinstance(value, float):
        text = f"{value:.{decimals}f}"
    else:
        text = str(value)
    return text[:width].ljust(width)


def format_ensemble(report: EnsembleReport, *, top: int | None = None) -> str:
    """A readable table plus the notes a row cannot be read honestly without.

    Display only — no logic lives here beyond formatting (rule 3 keeps the CLI
    thin, and this is the function a CLI command would call).
    """
    rows = list(report.rows)[: top if top is not None else len(report.rows)]
    header = "  ".join(name.ljust(w) for name, w in _COLUMNS)
    lines = [
        f"projection ensemble — season {report.season}, as of {report.as_of}, "
        f"weeks {report.weeks[0]}-{report.weeks[-1]}",
        header, "-" * len(header),
    ]
    for r in rows:
        lines.append("  ".join([
            _cell(r.player, 24), _cell(r.position, 4), _cell(r.team, 4),
            _cell(r.house_points, 8), _cell(r.espn_points, 8),
            _cell(r.blended_points, 8), _cell(r.delta_points, 8),
            _cell(r.delta_z, 6, decimals=_Z_DECIMALS),
            _cell(r.agreement, 16),
            _cell(",".join(r.flags) if r.flags else None, 34),
        ]))
    lines.append("")
    lines.extend(f"NOTE: {n}" for n in report.notes)
    return "\n".join(lines)


def format_disagreements(report: EnsembleReport, *, top: int | None = 200,
                         per_position: int = 15) -> str:
    """The per-position disagreement report, for eyeballing whether it is sane."""
    grouped = top_disagreements(report, top=top, per_position=per_position)
    corr = source_correlation(report, top=top)
    out = [
        f"source disagreement — season {report.season}, as of {report.as_of}",
        f"cohort: {corr.cohort} ({corr.n} players with both opinions); "
        f"correlation r={corr.correlation:.4f}" if corr.correlation is not None
        else f"cohort: {corr.cohort} ({corr.n} players); correlation undefined",
    ]
    if corr.median_ratio is not None:
        out.append(
            f"median ESPN/house ratio on the {report.horizon_games:g}-game horizon: "
            f"{corr.median_ratio:.4f} (1.0 = the two sources agree on average)"
        )
    for pos, rows in grouped.items():
        n, r = corr.by_position.get(pos, (0, None))
        if r is not None:
            rtxt = f"r={r:.4f}"
        elif n < MIN_POSITION_N:
            rtxt = f"r WITHHELD — only {n} players in this cohort, below the {MIN_POSITION_N} needed to mean anything"
        else:
            rtxt = "r undefined"
        out.append(f"\n{pos}  (n={n}, {rtxt})")
        for row in rows:
            out.append(
                f"  {(row.player or '?'):24s} house {row.house_points:7.1f}  "
                f"espn {row.espn_points:7.1f}  delta {row.delta_points:+7.1f}  "
                f"z {row.delta_z:+{_Z_DECIMALS + 4}.{_Z_DECIMALS}f}  {row.agreement}"
                + ("  [" + ",".join(row.flags) + "]" if row.flags else "")
            )
    out.append("")
    out.extend(f"NOTE: {n}" for n in report.notes)
    return "\n".join(out)
