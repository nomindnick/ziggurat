"""Corrected kicker board — the ESPN stat-line kicker source, OPT-IN (item 3.10).

WHY THIS MODULE EXISTS. The house board prices kickers through the Sleeper
``sleeper_rotowire`` feed, and that feed's made-FG buckets do not add up to its
own made-FG total. ``data/nfl/projections._KICKER_DIRECT_MAP`` maps a source key
``fgm_50p`` that the live feed does not serve, so every made field goal of 50+
yards scored ZERO while ``fg_missed`` (derived as ``fga - fgm``) still charged
-1 for every miss. See that module's ``unbucketed_fg_makes`` for the source-side
half of the story and the measurements behind it.

MEASURED CONSEQUENCE, live 2026 board at ``as_of`` 2026-08-30 (weeks 1-17):

  * every one of the 32 starting kickers is understated, by 25 to 43 house
    points on a ~120-point total;
  * the shortfall is NOT a constant fraction (8.8% to 25.8% of a kicker's made
    FGs), so it does not merely scale the K board — it REORDERS it. ESPN ranks
    Brandon Aubrey K1; the house board has him K5 behind Cameron Dicker.

That reordering is the reason this is a draft problem and not a rounding
problem: the engine takes a kicker around pick 92 as a deliberate divergence
play priced off exactly this board (CLAUDE.md item 2.3), and the board it is
diverging on has the wrong kicker at the top.

WHY ESPN AND NOT A SLEEPER REPAIR. Both exist and this repo ships both:

  * The SOURCE-SIDE repair is ``projections.map_sleeper_projection(...,
    derive_fg_50_plus=True)`` — the 50+ makes are recoverable from the feed's
    own arithmetic (``fgm`` minus the disjoint distance buckets). It is off by
    default because turning it on changes what a future ingest stores and
    therefore what ``build_valuation`` returns; that decision belongs to an
    integrator, not to an ingester's default.
  * The route THIS module serves is a MEASUREMENT rather than a model: ESPN
    publishes a projected kicker stat line, and item 3.9 verified that
    re-scoring it through ``core/scoring.py`` reproduces ESPN's own
    league-applied total to 1e-6 on 32 of 32 kickers — including the distance
    tiers and the -1/miss this league scores. So a correct, house-scored kicker
    board already exists in ``espn_projections`` and needs no derivation at all.

HOW MUCH THE TWO ROUTES ACTUALLY AGREE — measured on the live 2026 pool
(29 kickers both routes price), and stated at this length because an earlier
version of this docstring claimed more agreement than the numbers support:

  * they agree on K1 (Brandon Aubrey under both) and on the LEVEL of the
    correction: corrected season totals correlate r = 0.521, and the two routes'
    means differ by 2.7 house points (2.0%: ESPN 132.7, Sleeper-derived 135.3);
  * they do NOT agree on the per-player CORRECTION. The deltas each route adds
    correlate r = -0.454 — where ESPN adds most, the Sleeper residual adds
    least. Top-3 overlap is 1 of 3 (ESPN: Aubrey / Dicker / Mevis; Sleeper:
    Aubrey / Fairbairn / Little), and the Spearman between the two corrected
    orderings (0.862) is barely above the Spearman between the CURRENT board and
    the ESPN-corrected one (0.851);
  * so the small mean difference is a statement about LEVEL, not about ordering,
    and it is what a negative delta correlation produces on its own.

What survives that: both routes agree the current board understates every
kicker, and both put the same kicker first. The DECISION this board drives (take
that kicker) is therefore robust to which route you believe; the ORDERING below
about K3 is one feed's opinion and this module does not pretend otherwise.

THE HORIZON ALIGNMENT, which is load-bearing and easy to get wrong. ESPN's
season row is a whole-season projection carrying its own ``projected_games``
count (stat id 210; 17.0 for every 2026 kicker in the snapshot read on
2026-08-30 — but NOT a constant: the same snapshot publishes 2.0, 4.0, 6.0,
10.0, 11.0, 15.0 and 16.0 at other positions). The house board sums the weeks in
its own window — by default weeks 1-17, which contains only SIXTEEN games for a
kicker because his team has a bye inside it. Substituting ESPN's total unscaled
would overstate every kicker by about 6%. So the corrected total is

    espn_house_points * (weeks this kicker actually plays in the window)
                      / (games ESPN projected)

where "weeks he actually plays" is ``WeeklyLine.played_weeks`` — the coverage
measure item 3.2's audit installed, never the point sum (a bye row and a
"no forecast" row are byte-identical in this feed).

**That ratio is never allowed above 1.0.** When ESPN projects FEWER games than
the board's window covers, its total is a part-season number (an injury or
suspension view the rest of the board does not carry), and multiplying it up to
a full window invents a season. The row is left UNCORRECTED and says so. This is
not hypothetical: with a single ESPN row at ``projected_games = 6.0``, the
un-capped form priced the 32nd-best kicker at 324.4 points — K1, and overall #1
on the entire 3,229-row board above every RB and WR — and drove the operator to
take him at pick 89, ahead of the D/ST.

WHAT THIS MODULE DOES NOT DO.

  * It does not change any default. ``build_valuation`` and
    ``simulator.load_board`` are untouched; :func:`apply_to_valuation` is an
    explicit call an integrator makes, and until someone makes it the board is
    exactly what it was.
  * It holds NO scoring number (rule 2). Every point comes from
    ``espn_projections.house_points``, which asks ``core/scoring.py``.
  * It touches nothing under ``ziggurat/draft/`` and imports nothing from it
    (rule 8). The board-side splice is expressed against ``ValuationRow`` — a
    ``core`` type — and the grading-side splice is expressed against a plain
    mapping, so wiring it in crosses no import boundary.

HOW AN INTEGRATOR WIRES IT (both halves, both behind the same flag)::

    # ziggurat/draft/simulator.load_board, after build_valuation:
    kboard = build_kicker_board(conn, as_of=as_of, season=season, weeks=weeks)
    val_rows = apply_to_valuation(val_rows, kboard)

    # ziggurat/draft/grader.weekly_points_map, on the published points:
    points = apply_to_weekly_points(points, kboard)   # or on the returned map

:func:`apply_to_weekly_points` accepts BOTH key spaces — ``weekly_lines``' tuple
keys and ``load_board``'s player-id strings — detects which it was handed, and
RAISES :class:`KickerBoardMismatch` when it recognises neither. It used to match
tuples only, so the second line above substituted nothing, raised nothing, and
left the grading twin on the uncorrected board. That silent no-op FLIPS THE SIGN
of the evidence this correction is judged on. Measured on held-out seeds (75
paired PickEngine drafts, 25 at each of seats 1/5/9, rollouts 512, seed
20260831, live 2026 board): drafting off the corrected board is worth **+0.053
expected wins** (95% CI +0.039..+0.067) when the grading truth is corrected by
the INDEPENDENT Sleeper-residual route, and **-0.044** (95% CI -0.058..-0.029)
when the grading truth is the uncorrected board — which is exactly the
configuration the silent no-op produced. So the refusal is not tidiness; it is
the difference between reading this correction as a gain and rejecting it.

RULE 1. :func:`build_kicker_board` is the only DB read here; ``as_of`` is
keyword-only with no default and is threaded verbatim into both
``valuation.weekly_lines`` and ``espn_projections.get_espn_projections``, which
enforce the gate themselves. The default view is the safe ``historical`` one.
``tests/test_kicker_board.py`` carries the leakage test.

RULE 1'S OTHER HALF — STALENESS. ``espn_projections`` is NOT in
``data/nfl/refresh.py``'s ``SourceSpec`` registry and ESPN serves no projection
history, so the stored snapshot is whatever day someone last ran
``pull_espn_projections`` and NOTHING refreshes it. A board read in November off
an August pull is perfectly legal under the as-of gate. So every board carries
``source_retrieved_as_of`` and states it in its reasons and in
:func:`format_kicker_board`'s header, with an explicit CAUTION past
:data:`_STALE_SOURCE_DAYS`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from ziggurat.core import scoring, valuation
from ziggurat.core.valuation import DEFAULT_ROSTER, RosterStructure, ValuationRow
from ziggurat.data.nfl import base
from ziggurat.data.nfl import espn_projections as ep

#: The one corrected source implemented today. Named (rather than a bare bool)
#: because ``projections``'s own ``derive_fg_50_plus`` repair is a real second
#: route and a caller must be able to say WHICH correction it is reading.
SOURCE_ESPN = "espn"

#: Sources :func:`build_kicker_board` knows how to serve.
SOURCES = (SOURCE_ESPN,)

#: Per-kicker pricing basis, carried on every row so a mixed board says so.
BASIS_ESPN_STAT_LINE = "ESPN_STAT_LINE"      # corrected: re-scored ESPN projection
BASIS_UNCORRECTED = "UNCORRECTED"            # no ESPN row / no usable horizon

#: The canonical position this module prices. Deliberately a constant rather
#: than a literal sprinkled through the code: everything here is K-only by
#: design, and a reader should be able to see that in one place.
POSITION = "K"

#: Key spaces :func:`apply_to_weekly_points` can splice into. ``valuation`` is
#: ``weekly_lines``' own tuple key; ``board`` is the player-id STRING
#: ``draft/simulator.load_board`` (and therefore ``draft/grader``) uses, which
#: for a corrected kicker is always his ``espn_id``.
KEY_SPACE_VALUATION = "valuation"
KEY_SPACE_BOARD = "board"
KEY_SPACES = (KEY_SPACE_VALUATION, KEY_SPACE_BOARD)

#: How much of the house board's kicking CLUBS the corrected source must cover
#: before the board is servable without ``allow_partial=True``.
#:
#: ESPN publishes exactly one projected kicker per club: measured 2026-08-30,
#: 32 rows over 32 distinct clubs against a house K board covering the same 32.
#: A degraded pull is therefore visible as missing CLUBS, and this is sized as a
#: high-water mark rather than a median because the failure is all-or-nothing —
#: the reproduction that motivated it retained 4 of 32 rows (12.5%), reordered
#: the K board, and flipped the K/DST pick order with nothing raised anywhere.
#: 0.9 leaves room for ESPN genuinely not publishing a kicker for two or three
#: clubs without blocking the board.
_MIN_SOURCE_CLUB_FRACTION = 0.9

#: Days between the ESPN snapshot and the board's ``as_of`` past which the
#: staleness note is escalated to a CAUTION.
#:
#: A JUDGMENT CALL, not a measurement, and labelled as one wherever it prints:
#: no study in this repo says when an ESPN preseason projection goes stale. It
#: is set at a week because that is one full NFL news cycle. The board always
#: prints the actual retrieval DATE beside it, so an operator who disagrees with
#: the threshold can still see the fact.
_STALE_SOURCE_DAYS = 7


class KickerBoardUnavailable(RuntimeError):
    """The corrected source has nothing to serve at this ``as_of``/season.

    Raised rather than returning an empty board, and rather than silently
    falling back to the uncorrected numbers, because a caller that asked for the
    CORRECTED board and got the broken one without an error is precisely the
    Rule-1-invisible failure this module was written to remove. The usual cause
    is that ``espn_projections`` has never been pulled on this database (the
    table ships empty; see ``espn_projections.pull_espn_projections``), or that
    the migrations creating it have not been applied on this box.
    """


class KickerBoardDegraded(KickerBoardUnavailable):
    """The corrected source is present but too PARTIAL to price a board from.

    A subclass, so a caller that already handles "no corrected board" handles
    this too. Two conditions raise it, both measured rather than assumed:

      * the source covers fewer than :data:`_MIN_SOURCE_CLUB_FRACTION` of the
        house board's kicking clubs — a degraded pull;
      * an UNCORRECTED kicker sits inside the rank window that sets replacement
        level. The correction only adds points, so an uncorrected kicker there
        holds the baseline DOWN and inflates every K VOR on the board — which is
        exactly the number the K/DST divergence play is priced off.

    ``allow_partial=True`` serves the board anyway, with both conditions stated
    in ``KickerBoard.reasons`` and on the affected rows. That escape hatch
    exists because a partial board is sometimes the honest best available; the
    default is refusal because the disclosures cannot be seen from the draft
    cockpit (``BoardEntry`` carries no reasons field) and an unseen disclosure is
    not a disclosure.
    """


class KickerBoardMismatch(ValueError):
    """This board does not describe the thing it was handed.

    Raised by the two seams when the splice would be meaningless:

      * :func:`apply_to_weekly_points` recognised neither key space, or matched
        NONE of its corrected kickers — the silent no-op that used to leave the
        grading twin on the uncorrected board;
      * :func:`apply_to_valuation` was given rows from a different season, or
        from a different week window than the board was built over (a 17-week
        corrected total spliced onto a 14-week valuation overstates a kicker by
        about 60%, with the correction's own confident reasons attached).
    """


@dataclass(frozen=True)
class KickerLine:
    """One kicker, priced both ways, with the identity spine to join on.

    ``key`` is ``valuation.weekly_lines``' key — the SAME key space
    ``build_valuation`` groups on — so a caller can splice this row onto the
    house board without inventing an identity join of its own.

    ``season_points`` is the number to use; ``current_points`` is what the
    uncorrected Sleeper board says, kept beside it so every report can show the
    move rather than merely assert it. ``weeks`` is the corrected season total
    spread over the weeks the kicker actually plays, in the Sleeper feed's own
    weekly proportions — a WEEK ABSENT MEANS HE DOES NOT PLAY (the item-3.2
    convention, and the one ``draft/grader.py`` also relies on).

    ``source_retrieved_as_of`` is the day the ESPN row this line was priced from
    was pulled (``None`` on an uncorrected row). It is NOT the board's ``as_of``:
    nothing refreshes ``espn_projections``, so the two can be months apart with
    no gate violated.
    """

    key: tuple
    espn_key: str | None
    espn_id: str | None
    gsis_id: str | None
    team: str | None
    player: str | None
    season_points: float
    current_points: float
    weeks: Mapping[int, float]
    played_weeks: frozenset[int]
    projected_games: float | None
    basis: str
    reasons: tuple[str, ...]
    source_retrieved_as_of: str | None = None

    @property
    def delta(self) -> float:
        """Corrected minus current, in house points. Positive means the current
        board understates him — which, on the live 2026 board, is every kicker
        (checked per board rather than assumed; see ``KickerBoard.reasons``)."""
        return self.season_points - self.current_points

    @property
    def corrected(self) -> bool:
        return self.basis == BASIS_ESPN_STAT_LINE


@dataclass(frozen=True)
class KickerBoard:
    """Every kicker on the house board, priced through the corrected source.

    ``lines`` is ordered by corrected season points DESC. It contains BOTH
    corrected and uncorrected rows: ESPN publishes one kicker per club (32) and
    the house board carries every kicker the projection feed lists (153 live),
    so the tail cannot be corrected. ``replacement_is_mixed`` says whether that
    mixture reaches the rank window that sets replacement level — the one place
    where it stops being cosmetic, and by default a refusal (see
    :class:`KickerBoardDegraded`).

    ``weeks`` is the week window the board was priced over. It exists so
    :func:`apply_to_valuation` can refuse rows built over a DIFFERENT window
    instead of silently splicing a 17-week total onto a 14-week board. An empty
    tuple means "not recorded" (a hand-built board) and skips that check.

    ``unmatched_espn`` is ESPN kicker rows that joined no house row;
    ``unmatched_detail`` says who they are and — where it is knowable — why,
    because "ESPN publishes no line for him" and "our crosswalk has no espn_id
    for him" are opposite problems and the code used to report the first when it
    meant the second.
    """

    season: int
    as_of: str
    source: str
    lines: tuple[KickerLine, ...]
    corrected_count: int
    uncorrected_count: int
    unmatched_espn: tuple[str, ...]
    replacement_is_mixed: bool
    reasons: tuple[str, ...]
    weeks: tuple[int, ...] = ()
    source_retrieved_as_of: str | None = None
    unmatched_detail: tuple[str, ...] = ()
    partial_allowed: bool = False
    source_club_fraction: float | None = None

    def by_key(self) -> dict[tuple, KickerLine]:
        """Rows keyed by ``valuation.weekly_lines``' own grouping key."""
        return {line.key: line for line in self.lines}

    def corrected_by_espn_id(self) -> dict[str, KickerLine]:
        """CORRECTED rows only, keyed by ``espn_id``.

        Every corrected row has one by construction — matching ESPN's stat line
        is what makes a row corrected — so this join is exact and total, and it
        needs no mirror of ``weekly_lines``' key derivation (``ValuationRow``
        does not carry the ``source_player_id`` that key can fall back to, so a
        re-derived key would silently miss exactly the uncrosswalked rookies the
        fallback exists for).
        """
        return {str(line.espn_id): line for line in self.lines
                if line.corrected and line.espn_id is not None}

    def by_espn_id(self) -> dict[str, KickerLine]:
        """EVERY row that carries an ``espn_id``, corrected or not.

        The uncorrected half matters at the seams: without it
        :func:`apply_to_valuation` cannot tell an operator WHY a kicker was left
        alone, and used to print one fixed guess for four different causes.
        """
        return {str(line.espn_id): line for line in self.lines
                if line.espn_id is not None}


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _days_between(later, earlier) -> int | None:
    """Whole days from ``earlier`` to ``later``, or None if either is unusable."""
    a, b = base.iso_date(later), base.iso_date(earlier)
    if not a or not b:
        return None
    try:
        return (date.fromisoformat(a) - date.fromisoformat(b)).days
    except ValueError:  # pragma: no cover - defensive
        return None


def _espn_kicker_rows(conn, *, as_of, season, view) -> dict[str, object]:
    """``espn_key -> the stored ESPN whole-season kicker projection row``.

    ``week=SEASON_WEEK`` is ESPN's own scoring-period id for the season split
    (not an invented sentinel), and ``position`` is the value ``espn_ranks``
    canonicalises to, so this is the accessor's own vocabulary throughout.

    A MISSING TABLE is turned into the module's own guided refusal. Migrations
    ``009``/``010`` create ``espn_projections``; on a box that has not applied
    them the bare ``sqlite3.OperationalError`` would reach the operator at
    cockpit launch WITHOUT the one sentence that says what to run.
    """
    try:
        rows = ep.get_espn_projections(
            conn, as_of=as_of, season=season, week=ep.SEASON_WEEK,
            position=POSITION, view=view,
        )
    except sqlite3.OperationalError as exc:
        raise KickerBoardUnavailable(
            f"cannot read the corrected kicker source: {exc}. The "
            "espn_projections table is created by db/migrations/009 and 010 and "
            "populated by espn_projections.pull_espn_projections; a database "
            "that has neither cannot serve a corrected board. Refusing to serve "
            "the uncorrected board under a corrected label."
        ) from exc
    return {str(r["espn_key"]): r for r in rows}


def _week_shape(line: valuation.WeeklyLine) -> dict[int, float]:
    """Relative weekly weights for one kicker, over the weeks he PLAYS.

    ``played_weeks`` (the row carried an opponent), never the point sum: in this
    feed a bye row and a "we have no forecast for him" row are byte-identical,
    so a points-based split silently treats missing coverage as a bye. Weights
    come from the Sleeper weekly points where they are positive; where the feed
    gives a played week no points at all, the split falls back to uniform so a
    real game is never valued at exactly zero by accident.
    """
    played = sorted(line.played_weeks)
    if not played:
        return {}
    weights = {w: max(0.0, float(line.points.get(w, 0.0))) for w in played}
    if sum(weights.values()) <= 0.0:
        return {w: 1.0 for w in played}
    return weights


def build_kicker_board(
    conn,
    *,
    as_of,
    season,
    weeks: Iterable[int] | None = None,
    source: str = SOURCE_ESPN,
    projection_source: str = "sleeper_rotowire",
    rules: scoring.ScoringRules = scoring.HOUSE_RULES,
    roster: RosterStructure = DEFAULT_ROSTER,
    view: base.AsOfView = "historical",
    lines: Mapping[tuple, valuation.WeeklyLine] | None = None,
    allow_partial: bool = False,
) -> KickerBoard:
    """The corrected kicker board, read at ``as_of`` (keyword-only; no implicit now).

    Both reads are gated INSIDE their accessors and both receive ``as_of`` and
    ``view`` verbatim: the house spine through ``valuation.weekly_lines`` (so
    the identity keys are the ones ``build_valuation`` uses) and the correction
    through ``espn_projections.get_espn_projections``.

    ``lines`` lets a caller hand over a ``weekly_lines`` map it has ALREADY
    built, instead of paying for a second full pass over the projections table.
    Measured on the live 3,264-row board: ``load_board`` 3.75 s and a fresh
    ``build_kicker_board`` 3.77 s, so wiring the correction without this doubled
    the cockpit's cold start (~3.8 s to ~7.5 s at 18:45 on the runbook's launch
    clock). THE CALLER OWNS THE CONTRACT: the map must have been built at the
    same ``as_of``/``season``/``weeks``/``source``/``view``, because this
    function cannot check a gate it did not run. Everything else on this board —
    including the ESPN read — is still gated here.

    Raises :class:`KickerBoardUnavailable` when the corrected source serves
    nothing at this ``as_of``, and :class:`KickerBoardDegraded` when it serves
    too little to price a board from (``allow_partial=True`` overrides, loudly).
    Never a silent fallback to the broken numbers.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown kicker source {source!r}; known: {sorted(SOURCES)}")

    week_window = tuple(sorted(set(valuation.DEFAULT_WEEKS if weeks is None else weeks)))
    if lines is None:
        lines = valuation.weekly_lines(
            conn, as_of=as_of, season=season, weeks=week_window,
            source=projection_source, rules=rules, view=view,
        )
    else:
        # The one half of the caller's contract that IS checkable: a map built
        # over a wider window than the one claimed would price a 17-week kicker
        # onto a board that calls itself 14 weeks, and ``apply_to_valuation``
        # would then wave it through because the two week counts agree.
        stray = {w for v in lines.values() if v.position == POSITION
                 for w in v.points} - set(week_window)
        if stray:
            raise KickerBoardMismatch(
                f"the weekly_lines map handed in covers weeks {sorted(stray)} that are "
                f"outside weeks={week_window[0]}-{week_window[-1]}; it was built over a "
                "different window than this board claims. Build both with the same weeks="
            )

    house = {k: v for k, v in lines.items() if v.position == POSITION}
    if not house:
        raise KickerBoardUnavailable(
            f"no kicker rows on the house board at as_of={as_of} season={season} "
            f"(projection source {projection_source!r}); nothing to correct"
        )

    espn_rows = _espn_kicker_rows(conn, as_of=as_of, season=season, view=view)
    if not espn_rows:
        raise KickerBoardUnavailable(
            f"the corrected kicker source {source!r} has no rows at as_of={as_of} "
            f"season={season}. espn_projections ships EMPTY and is populated by "
            "espn_projections.pull_espn_projections (or ingest_espn_projections on "
            "an already-fetched kona_player_info response). Refusing to serve the "
            "uncorrected board under a corrected label."
        )

    # ---- club coverage: a degraded pull is missing CLUBS, and one club's
    # kicker is one row, so this is the honest denominator for "how much of the
    # source arrived". Computed against the house board's own kicking clubs
    # rather than a hardcoded 32.
    house_clubs = {v.team for v in house.values() if v.team}
    espn_clubs = {str(r["team"]).strip().upper() for r in espn_rows.values()
                  if r["team"] is not None}
    espn_clubs = {base.TEAM_ALIASES.get(t, t) for t in espn_clubs}
    club_fraction = (len(espn_clubs & house_clubs) / len(house_clubs)) if house_clubs else 0.0
    club_note = (
        f"the corrected source covers {len(espn_clubs & house_clubs)} of "
        f"{len(house_clubs)} kicking clubs on the house board "
        f"({100 * club_fraction:.0f}%; ESPN publishes one kicker per club)"
    )
    if club_fraction < _MIN_SOURCE_CLUB_FRACTION and not allow_partial:
        raise KickerBoardDegraded(
            f"refusing to price a kicker board from a PARTIAL source: {club_note}, "
            f"under the {100 * _MIN_SOURCE_CLUB_FRACTION:.0f}% floor. A partial "
            "correction re-orders the K board and moves the replacement level "
            "without saying so anywhere the draft cockpit can show it. Re-run "
            "espn_projections.pull_espn_projections, or pass allow_partial=True "
            "to accept the mixture with its disclosures."
        )

    matched_espn_keys: set[str] = set()
    out: list[KickerLine] = []
    for key, line in house.items():
        current = float(line.season_points)
        espn_key = str(line.espn_id) if line.espn_id is not None else None
        row = espn_rows.get(espn_key) if espn_key else None
        games = _as_float(row["projected_games"]) if row is not None else None
        played = len(line.played_weeks)

        # WHY a row is left uncorrected, distinguished rather than guessed. The
        # first two cases used to collapse into one string that named the wrong
        # cause: live 2026, three kickers were told "ESPN publishes no projected
        # stat line" while ESPN was holding their line and it was OUR crosswalk
        # that had no espn_id for them.
        why: str | None = None
        if espn_key is None:
            why = ("this house row carries no espn_id, so there is nothing to "
                   "join ESPN's stat line to (a players-crosswalk gap, NOT an "
                   "absent ESPN projection)")
        elif row is None:
            why = "ESPN publishes no projected stat line for this kicker"
        elif not games:
            why = "the ESPN row carries no projected games count"
        elif played == 0:
            why = "the projection feed forecasts no game for him in this week window"
        elif games < played - 1e-9:
            why = (
                f"ESPN projects only {games:.0f} games where this board's window "
                f"covers {played} — refusing to scale a part-season projection UP "
                "to a full one (that is an availability view, not a scoring fix)"
            )

        if why is not None:
            out.append(
                KickerLine(
                    key=key, espn_key=espn_key, espn_id=line.espn_id,
                    gsis_id=line.gsis_id, team=line.team, player=line.player,
                    season_points=current, current_points=current,
                    weeks=dict(line.points), played_weeks=line.played_weeks,
                    projected_games=games, basis=BASIS_UNCORRECTED,
                    reasons=(
                        f"UNCORRECTED {current:.1f} house pts — {why}",
                        "the 50+ made-FG shortfall is still in this number "
                        "(see core/kicker_board for what that costs)",
                    ),
                    source_retrieved_as_of=None,
                )
            )
            continue

        matched_espn_keys.add(espn_key)
        espn_total = ep.house_points(row, rules=rules)
        corrected = espn_total * played / games

        shape = _week_shape(line)
        total_weight = sum(shape.values())
        weekly = {w: corrected * wt / total_weight for w, wt in shape.items()}
        retrieved = row["retrieved_as_of"] if "retrieved_as_of" in row.keys() else None

        out.append(
            KickerLine(
                key=key, espn_key=espn_key, espn_id=line.espn_id,
                gsis_id=line.gsis_id, team=line.team, player=line.player,
                season_points=corrected, current_points=current,
                weeks=weekly, played_weeks=line.played_weeks,
                projected_games=games, basis=BASIS_ESPN_STAT_LINE,
                reasons=(
                    f"{corrected:.1f} house pts over {played} played wk "
                    f"({corrected - current:+.1f} vs the {current:.1f} the "
                    "current board shows)",
                    correction_label(games=games, played=played),
                    f"ESPN season line {espn_total:.1f} pts over "
                    f"{games:.0f} projected games"
                    + (f", pulled {retrieved}" if retrieved else ""),
                ),
                source_retrieved_as_of=str(retrieved) if retrieved else None,
            )
        )

    out.sort(key=lambda r: (-r.season_points, str(r.player or ""), str(r.key)))
    corrected_count = sum(1 for r in out if r.corrected)
    unmatched = tuple(sorted(set(espn_rows) - matched_espn_keys))

    # Does the mixture reach the rank window that sets replacement level? Below
    # that window it is cosmetic (an uncorrected kicker can only sink); inside
    # it, it holds the baseline down and inflates every K VOR.
    started = roster.teams * roster.starters.get(POSITION, 0)
    window = out[max(0, started - 1):started + 2]
    if not window:
        # THE THIN-BOARD CLAMP, mirrored from ``valuation.replacement_levels``:
        # when the board is shorter than the window, that function falls back to
        # ``pts_list[-1]``. This must fall back to the same row, or a short board
        # would report "not mixed" while the last kicker — uncorrected — was in
        # fact setting the baseline.
        window = out[-1:]
    mixed = any(not r.corrected for r in window)
    if mixed and not allow_partial:
        raise KickerBoardDegraded(
            f"refusing to price a kicker board whose replacement-level window "
            f"(around K{started + 1}) contains an UNCORRECTED kicker "
            f"({', '.join(str(r.player or r.key) for r in window if not r.corrected)}). "
            "The correction only adds points, so an uncorrected kicker there holds "
            "the baseline DOWN and inflates every K VOR on the board — the exact "
            "number the K/DST divergence play is priced off. Pass allow_partial="
            "True to accept it with the mixture disclosed on the board and on "
            "every K row."
        )

    # The direction claim, COMPUTED. "The correction only ever adds points" is
    # true of the 2026 board and is not a theorem: an ESPN row can price a
    # kicker below the Sleeper feed, and then an uncorrected kicker really can
    # outrank one he should sit below.
    lowered = [r for r in out if r.corrected and r.delta < 0]
    retrieved_days = sorted({r.source_retrieved_as_of for r in out
                             if r.source_retrieved_as_of}, reverse=True)
    newest = retrieved_days[0] if retrieved_days else None

    reasons = [
        f"{corrected_count} of {len(out)} kickers priced from {source} "
        f"({len(out) - corrected_count} left uncorrected and labelled)",
        club_note,
    ]
    if lowered:
        reasons.append(
            f"CAUTION: the correction LOWERS {len(lowered)} of {corrected_count} "
            f"priced kickers (worst {min(r.delta for r in lowered):+.1f} pts), so "
            "the usual one-way safety argument does not hold on this board — an "
            "uncorrected kicker can outrank a corrected one he should sit below"
        )
    else:
        reasons.append(
            "on this board the correction only ADDS points (checked, not assumed), "
            "so an uncorrected kicker can sink past a corrected one but can never "
            "rise above one he should sit below"
        )
    if newest:
        age = _days_between(as_of, newest)
        stale = (
            f"the ESPN snapshot was pulled {newest}"
            + (f", {age} days before this board's as_of {as_of}" if age is not None else "")
            + " — espn_projections is NOT in the scheduled ingest registry and ESPN "
              "serves no projection history, so nothing refreshes it"
        )
        if age is not None and age > _STALE_SOURCE_DAYS:
            stale = (f"CAUTION, STALE SOURCE: {stale}. (More than "
                     f"{_STALE_SOURCE_DAYS} days is a judgment call, not a measured "
                     "threshold; the pull date above is the fact.)")
        reasons.append(stale)
    if mixed:
        reasons.append(
            "CAUTION: an uncorrected kicker sits in the replacement-level rank "
            f"window (around K{started + 1}), so the mixture is priced into every "
            "K VOR on this board, not just into the tail"
        )
    if allow_partial:
        reasons.append(
            "allow_partial=True — the completeness floors that normally REFUSE a "
            "partial board were waived by the caller; read the two lines above "
            "as the reason they exist"
        )

    detail = _unmatched_detail(espn_rows, unmatched, house)
    if unmatched:
        reasons.append(
            f"{len(unmatched)} ESPN kicker rows were not applied to any house row "
            f"({'; '.join(detail[:4])}{' ...' if len(detail) > 4 else ''}). They are "
            "NOT added to the board, which stays the house spine — and where the "
            "cause is a missing espn_id the SAME kicker is already on this board, "
            "uncorrected, under a row with no id to join on"
        )

    return KickerBoard(
        season=int(season), as_of=str(as_of), source=source, lines=tuple(out),
        corrected_count=corrected_count,
        uncorrected_count=len(out) - corrected_count,
        unmatched_espn=unmatched, replacement_is_mixed=mixed,
        reasons=tuple(reasons), weeks=week_window,
        source_retrieved_as_of=newest, unmatched_detail=detail,
        partial_allowed=bool(allow_partial), source_club_fraction=club_fraction,
    )


def correction_label(*, games: float | None = None, played: int | None = None) -> str:
    """The Rule-6 label quoted on every corrected row.

    Takes the row's OWN horizon rather than quoting a constant. The previous
    version hard-coded "scaled from ESPN's 17-game season", which was false for
    any row ESPN projected differently — and false at exactly the point where
    the number needed the most scrutiny.
    """
    horizon = ""
    if games is not None and played is not None:
        horizon = (f", then scaled from ESPN's {games:.0f}-game projection to the "
                   f"{played} weeks he plays in this board's window")
    return (
        "ESPN projected stat line re-scored through the house rules "
        "(item 3.9 verified this reproduces ESPN's own league total on 32/32 "
        "kickers)" + horizon
    )


#: The generic form of :func:`correction_label`, kept as a module constant
#: because the original contract names one. Every ROW quotes the per-row form
#: above instead, which is the whole point of the change.
CORRECTION_LABEL = correction_label()

#: The Rule-6 caveat that must ride on a KICKER RECOMMENDATION when this board
#: could not be built (item 3.11 audit finding 5). The launch-time note in
#: ``simulator.load_draft_board`` is read three hours before the round-10 kicker
#: pick and is addressed to the operator's *setup*; this sentence is addressed to
#: the *recommendation*, which is where Rule 6 puts the burden — "every
#: recommendation ships with its reasons and data". It matters because the
#: shortfall is not a constant fraction (8.8%-25.8% measured across the 32
#: starters), so it REORDERS the K board rather than scaling it: the ordering
#: claim is the part that is unreliable, and the panel used to state it bare.
UNCORRECTED_KICKER_CAVEAT = (
    "CAVEAT — this K board is UNCORRECTED: the projections feed never publishes "
    "the 50+ made-FG bucket while still charging -1 a miss, so every kicker here "
    "is understated by roughly 25-43 points and by DIFFERENT amounts (8.8%-25.8%), "
    "which reorders them. Take a kicker here; treat WHICH kicker as a coin-flip "
    "among the top few. Nothing to do about it tonight (item 3.10 / runbook §9)."
)


def _unmatched_detail(espn_rows, unmatched, house) -> tuple[str, ...]:
    """One legible line per unapplied ESPN kicker, naming the likely cause.

    The cause is INFERRED and says so: an ESPN kicker whose club already carries
    a house kicker with no ``espn_id`` is almost certainly the same man on both
    sides of a crosswalk gap — but where a club has more than one such row (live
    2026: the Giants have two) this refuses to pick, because guessing which
    un-crosswalked kicker is which would be exactly the invented join this
    module does not do.
    """
    unjoined_by_club: dict[str, int] = {}
    for line in house.values():
        if line.espn_id is None and line.team:
            unjoined_by_club[line.team] = unjoined_by_club.get(line.team, 0) + 1
    out: list[str] = []
    for key in unmatched:
        row = espn_rows[key]
        team = str(row["team"]).strip().upper() if row["team"] is not None else None
        team = base.TEAM_ALIASES.get(team, team) if team else None
        n = unjoined_by_club.get(team or "", 0)
        if n == 1:
            cause = (f"the house board carries exactly one {team} kicker with no "
                     "espn_id — same kicker, crosswalk gap, left uncorrected")
        elif n > 1:
            cause = (f"the house board carries {n} {team} kickers with no espn_id, "
                     "so which one he is cannot be told apart — not guessed")
        else:
            cause = "no house kicker on his club is missing an espn_id"
        out.append(f"{row['player'] or key} ({team or '?'}): {cause}")
    return tuple(out)


def apply_to_valuation(
    rows: Sequence[ValuationRow],
    board: KickerBoard,
    *,
    roster: RosterStructure = DEFAULT_ROSTER,
    denoise_kdst: bool = True,
) -> list[ValuationRow]:
    """A ``build_valuation`` result with the kicker rows re-priced, re-ranked.

    THE SEAM a Phase-3 integrator wires in, and the reason it takes rows rather
    than a connection: it is a pure function of the board it is handed, so the
    caller keeps the single ``as_of`` it already read at and this cannot open a
    second, differently-gated read behind its back.

    That purity has a limit and the limit is checked here rather than assumed:
    rows carry their own ``season`` and their own ``weeks_counted``, so a board
    built over a DIFFERENT season or a different week window is a
    :class:`KickerBoardMismatch` rather than a 60% overstatement wearing the
    correction's confident reasons. (A board with no recorded ``weeks`` — one
    built by hand in a test — skips the window half of that check.)

    Everything the correction touches is recomputed rather than patched:
    replacement levels run through ``valuation.replacement_levels`` again on the
    new points (only K's list changed — K is not flex-eligible, so no other
    position's baseline can move), every row's VOR is recomputed against that
    dict, and the overall/positional ranks are rebuilt exactly the way
    ``build_valuation`` builds them.

    NOT re-keyed: identity fields are copied through untouched. The kicker rows
    are matched on ``espn_id``, which is what makes a row corrected in the first
    place, so this never invents a join.
    """
    _check_alignment(rows, board)

    by_espn_all = board.by_espn_id()
    corrected_lines: dict[int, KickerLine] = {}
    board_lines: dict[int, KickerLine] = {}
    for i, row in enumerate(rows):
        if row.position != POSITION or row.espn_id is None:
            continue
        line = by_espn_all.get(str(row.espn_id))
        if line is None:
            continue
        board_lines[i] = line
        if line.corrected:
            corrected_lines[i] = line
    corrected_points = {i: line.season_points for i, line in corrected_lines.items()}
    if board.corrected_count and not corrected_lines:
        raise KickerBoardMismatch(
            f"none of this board's {board.corrected_count} corrected kickers matched a "
            "valuation row (the join is on espn_id). Re-pricing nothing while returning "
            "a re-ranked board is the same silent no-op the grading seam used to have. "
            "Build the board and the valuation from the same connection, as_of, season "
            "and weeks."
        )

    by_pos: dict[str, list[float]] = {}
    for i, row in enumerate(rows):
        pts = corrected_points.get(i, row.proj_points)
        by_pos.setdefault(row.position, []).append(pts)
    for pts_list in by_pos.values():
        pts_list.sort(reverse=True)

    replacement, started = valuation.replacement_levels(
        by_pos, roster, denoise_kdst=denoise_kdst
    )

    # Board-level cautions ride on every K row, because ``KickerBoard.reasons``
    # is not reachable from anything the draft cockpit renders.
    board_cautions = tuple(r for r in board.reasons if r.startswith("CAUTION"))

    rebuilt: list[ValuationRow] = []
    for i, row in enumerate(rows):
        pts = corrected_points.get(i, row.proj_points)
        repl = replacement.get(row.position, 0.0)
        reasons = row.reasons
        if row.position == POSITION:
            baseline_rank = started.get(POSITION, 0) + 1
            line = board_lines.get(i)
            head = (
                f"{pts:.1f} house pts / {row.weeks_counted} wk",
                f"K replacement {repl:.1f} (K{baseline_rank})",
            )
            tail = line.reasons if line is not None else (
                "UNCORRECTED — this row does not appear on the corrected kicker "
                + ("board (it carries no espn_id to join on)" if row.espn_id is None
                   else f"board under espn_id {row.espn_id}")
                + "; the 50+ made-FG shortfall is still in this number",
            )
            reasons = (head + tail
                       + _carried_reasons(row.reasons, corrected=i in corrected_lines)
                       + board_cautions)
        rebuilt.append(
            replace(row, proj_points=pts, replacement_points=repl,
                    vor=pts - repl, reasons=tuple(reasons))
        )

    rebuilt.sort(key=lambda v: v.vor, reverse=True)
    pos_counter: dict[str, int] = {}
    ranked: list[ValuationRow] = []
    for i, v in enumerate(rebuilt, start=1):
        pos_counter[v.position] = pos_counter.get(v.position, 0) + 1
        ranked.append(replace(v, overall_rank=i, pos_rank=pos_counter[v.position]))
    return ranked


def _check_alignment(rows: Sequence[ValuationRow], board: KickerBoard) -> None:
    """Refuse rows the board does not describe (season, or week window)."""
    seasons = {r.season for r in rows}
    if seasons and board.season not in seasons:
        raise KickerBoardMismatch(
            f"this kicker board prices season {board.season} but the valuation "
            f"rows are season {sorted(seasons)}; splicing them would price one "
            "season's kickers onto another season's board"
        )
    if not board.weeks:
        return
    spans = {r.weeks_counted for r in rows if r.position == POSITION}
    if spans and len(board.weeks) not in spans:
        raise KickerBoardMismatch(
            f"this kicker board was priced over {len(board.weeks)} weeks "
            f"({board.weeks[0]}-{board.weeks[-1]}) but the valuation's kicker rows "
            f"count {sorted(spans)} weeks. A 17-week corrected total spliced onto "
            "a 14-week board overstates a kicker by about 60%, with the "
            "correction's own reasons attached. Rebuild both with the same weeks="
        )


#: A carried reason is dropped from a CORRECTED row when it quotes a points
#: figure, because the figure it quotes is the pre-correction one. Everything
#: else survives — notably ``build_valuation``'s "low-confidence order (small
#: season spread)", a Rule-6 disclosure that is still true after the correction
#: (``denoise_kdst`` is still in force and the K spread is still small) and that
#: this seam used to silently delete from exactly the rows it moved.
_POINTS_MARKER = " pts"


def _carried_reasons(reasons: tuple[str, ...], *, corrected: bool) -> tuple[str, ...]:
    """``build_valuation``'s own reasons, minus the two this seam recomputes."""
    rest = tuple(reasons[2:])   # [0] season points, [1] replacement — both rebuilt
    if not corrected:
        return rest
    return tuple(r for r in rest if _POINTS_MARKER not in r)


def apply_to_weekly_points(
    points_by_key: Mapping,
    board: KickerBoard,
    *,
    key_space: str = "auto",
):
    """A weekly-points map with the corrected kicker weeks spliced in.

    The grading-side twin of :func:`apply_to_valuation`: a decision board and the
    metric that grades it must price a kicker the same way, or the experiment
    measures the disagreement instead of the change. Uncorrected kickers keep
    their existing weeks untouched, and the input is never mutated.

    TWO KEY SPACES, because there are two callers and they do not agree:

      ``valuation``  ``valuation.weekly_points``' tuple key, e.g.
                     ``('SKILL', '00-0037692')`` — what ``KickerLine.key`` is.
      ``board``      ``draft/simulator.load_board``'s player-id STRING, which is
                     what ``draft/grader.weekly_points_map`` is keyed by and,
                     for a corrected kicker, is always his ``espn_id``.

    ``key_space="auto"`` (the default) reads the shape of the keys it was
    handed. THIS FUNCTION USED TO MATCH TUPLES ONLY, so the documented grader
    call substituted nothing, raised nothing, and produced a well-formed map
    with zero corrections in it — measured live: 0 of 29 kickers spliced, and
    the evidence the correction is judged on flips sign (+0.06 expected wins to
    -0.04) when the decision board is corrected and the grading truth is not. So
    matching NOTHING now raises :class:`KickerBoardMismatch`.

    RETURN TYPE. A plain ``dict`` in, a plain ``dict`` out. A mapping that
    carries ``positions``/``names``/``teams`` (``grader.WeeklyPointsMap``) is
    rebuilt as its OWN type with those attributes intact — losing them silently
    degrades the grader's field model to ``None`` — and if that rebuild is not
    possible this raises rather than handing back something thinner than what it
    was given. No import of ``ziggurat.draft`` is involved: this is a duck-typed
    protocol, so rule 8 holds.

    THE MISSING-WEEK CONVENTION SURVIVES: a corrected kicker's weeks are exactly
    the weeks he plays, so a week that was absent stays absent and a bye is still
    "he does not play", never "he scores zero".
    """
    if key_space not in (*KEY_SPACES, "auto"):
        raise ValueError(f"unknown key_space {key_space!r}; known: {sorted(KEY_SPACES)}")

    out = {k: dict(v) for k, v in points_by_key.items()}
    space = _detect_key_space(out) if key_space == "auto" else key_space

    corrected = [ln for ln in board.lines if ln.corrected]
    substituted = 0
    if space == KEY_SPACE_VALUATION:
        for line in corrected:
            if line.key in out:
                out[line.key] = dict(line.weeks)
                substituted += 1
    else:
        for line in corrected:
            pid = str(line.espn_id) if line.espn_id is not None else None
            if pid is not None and pid in out:
                out[pid] = dict(line.weeks)
                substituted += 1

    if corrected and out and not substituted:
        sample = next(iter(out))
        raise KickerBoardMismatch(
            f"none of this board's {len(corrected)} corrected kickers matched a key "
            f"in the map it was handed (read as the {space!r} key space; a key there "
            f"looks like {sample!r}, a board key looks like "
            f"{corrected[0].key!r} / espn_id {corrected[0].espn_id!r}). Splicing "
            "nothing while returning a well-formed map is how a corrected decision "
            "board ends up graded against uncorrected truth. Pass key_space= "
            "explicitly if the shape is right and the ids are not."
        )
    return _rebuild_like(points_by_key, out)


def _detect_key_space(points: Mapping) -> str:
    """Which key space a map is in, refusing to guess on a mixture."""
    kinds = {("tuple" if isinstance(k, tuple) else "str" if isinstance(k, str) else "?")
             for k in points}
    if kinds <= {"tuple"}:
        return KEY_SPACE_VALUATION
    if kinds <= {"str"}:
        return KEY_SPACE_BOARD
    raise KickerBoardMismatch(
        f"cannot tell which key space this map is in (key kinds: {sorted(kinds)}). "
        f"Pass key_space={KEY_SPACE_VALUATION!r} for valuation.weekly_points' tuple "
        f"keys or {KEY_SPACE_BOARD!r} for load_board's player-id strings."
    )


_METADATA_ATTRS = ("positions", "names", "teams")


def _rebuild_like(original: Mapping, points: dict):
    """``points`` in ``original``'s own container type, metadata intact."""
    if type(original) is dict or not all(hasattr(original, a) for a in _METADATA_ATTRS):
        return points
    kwargs = {a: getattr(original, a) for a in _METADATA_ATTRS}
    try:
        return type(original)(points, **kwargs)
    except TypeError as exc:   # pragma: no cover - defensive
        raise KickerBoardMismatch(
            f"cannot rebuild a {type(original).__name__} around the corrected points "
            f"({exc}); returning a bare dict would silently drop "
            f"{', '.join(_METADATA_ATTRS)} and degrade whatever grades with it. "
            "Hand this function a plain dict if that is what you want back."
        ) from exc


_COLUMNS = (
    ("K", 3), ("kicker", 22), ("tm", 4), ("current", 8), ("corrected", 10),
    ("delta", 7), ("basis", 16),
)


def format_kicker_board(board: KickerBoard, *, top: int | None = 12,
                        reasons: bool = True) -> str:
    """A readable side-by-side of the current and corrected kicker boards."""
    header = "  ".join(name.rjust(w) if i >= 3 else name.ljust(w)
                       for i, (name, w) in enumerate(_COLUMNS))
    span = (f", weeks {board.weeks[0]}-{board.weeks[-1]}" if board.weeks else "")
    pulled = (f", ESPN snapshot pulled {board.source_retrieved_as_of}"
              if board.source_retrieved_as_of else "")
    out = [f"corrected kicker board — {board.source}, season {board.season}, "
           f"as_of {board.as_of}{span}{pulled}", header, "-" * len(header)]
    rows = board.lines if top is None else board.lines[:top]
    for rank, line in enumerate(rows, start=1):
        cells = [
            str(rank).ljust(3),
            str(line.player or "?")[:22].ljust(22),
            str(line.team or "?")[:4].ljust(4),
            f"{line.current_points:.1f}".rjust(8),
            f"{line.season_points:.1f}".rjust(10),
            f"{line.delta:+.1f}".rjust(7),
            line.basis.rjust(16),
        ]
        out.append("  ".join(cells))
    if reasons:
        out.append("")
        out.extend(f"  - {r}" for r in board.reasons)
    return "\n".join(out)
