"""Week-by-week roster grader and win-probability objective (Phase-2 keystone).

Import-quarantined package (Rule 8). Nothing outside ``ziggurat/draft/`` imports this;
it imports ``ziggurat/core/`` freely (the dependency runs one way only).

WHAT THIS IS, AND WHAT IT REPLACES
----------------------------------
``simulator.optimal_starting_points`` seats ONE lineup off ``BoardEntry.
house_points`` — a SEASON SUM — and grades that. Every quantity that decides a
fantasy season is invisible to it:

  * WHICH WEEK a player byes, and whether two of your starters bye together;
  * that a bench player ever enters a lineup when the man in front of him byes
    (a season sum counts your 3rd QB's full season at zero relevance and at zero
    cost). This module prices THAT. It still does not price injury cover — see
    "what this objective still cannot see" below, and read it before optimising;
  * that the season is fourteen SEPARATE head-to-head weeks, so 30 surplus
    points in week 3 do not pay for a hole in week 8;
  * that six of ten teams make a playoff bracket played in weeks 15-17.

Measured symptom on the live 2026 board, re-verified 2026-08-30 (``PickEngine``
at seat 9 of 10 against nine ``RankNoiseBot``s, seed 7): a 16-man roster with
THREE QBs and THREE TEs — 37.5% of the picks spent on two one-starter slots —
and only three RBs behind two RB slots plus a FLEX. The season-total metric
scored that roster 2223.7 and had no way to say anything was wrong.

AND IT IS NOT MERELY BLIND, IT IS BACKWARDS. At seed 2 the same seat draws the
same 3-QB/3-TE/3-RB shape plus a week-8 bye collision — McCaffrey and Montgomery
out together behind two RB slots — season total 2233.0. Swap Montgomery for any
comparable RB who byes in a different week and the two metrics DISAGREE IN SIGN
(live board, graded against that draft's nine real rivals):

    Montgomery 227.5 (bye 8)  ->  Breece Hall     226.8 (bye 13)
                                      season total -0.7,  expected wins +0.160
    Montgomery 227.5 (bye 8)  ->  Josh Jacobs     226.3 (bye 11)
                                      season total -1.2,  expected wins +0.146
    Montgomery 227.5 (bye 8)  ->  Javonte W.      224.6 (bye 14)
                                      season total -2.9,  expected wins +0.139
    ...and the control, a swap that KEEPS the collision:
    Montgomery 227.5 (bye 8)  ->  Cam Skattebo    216.5 (bye 8)
                                      season total -11.0, expected wins -0.084

The season total ranks Montgomery ahead of all three; the week that decides the
season says giving up a point of season total to un-stack a bye is worth about a
sixth of a win. The hole in week 8 closes in exactly the three cases where the
objective flips, and stays open in the control.

This module grades a roster the way the league actually settles: seat the lineup
SEPARATELY IN EVERY WEEK with that week's own points and that week's own
availability, turn each week's seated mean into a win probability against a
modelled opponent, and sum. A player on bye is NOT SEATED (not "seated for zero")
— so when the cupboard behind him is bare the slot is genuinely EMPTY and the
week is reported as a HOLE, instead of quietly scoring as "the other eight
carried it".

THE OBJECTIVE
-------------
``SeasonGrade.objective`` is ``expected_wins`` — the sum over the regular-season
weeks of P(win that week). It is the right thing to maximise for three reasons:
it is smooth and strictly monotone in every week's mean (so a search can climb
it), it prices a hole exactly as hard as the win it costs (no invented penalty
term), and it needs no weight nobody has measured. ``playoff_prob`` and
``title_prob`` are reported as its CONSEQUENCES under a labelled field model,
deliberately NOT blended into ``objective`` — the blending weight would be an
invention. A caller who wants one passes ``objective_playoff_weight`` /
``objective_title_weight`` (both default 0.0, so the default objective is exactly
``expected_wins``).

THE WIN MODEL
-------------
``P = Phi((mu - mu_opp) / sqrt(var + var_opp))`` — item 3.5's own formula,
through item 3.5's own ``lineup_support.win_probability``. Sigma comes from
``lineup_support.DEFAULT_VARIANCE``, the OLS fit of realised weekly house-point
standard deviation on mean (2021-2025 REG, >=8 games, re-scored through
``scoring.py``). It is a LABELLED HYPOTHESIS and is quoted as one in the reasons
(Rule 6). Its shape fits here unchanged; the one thing it does not carry is a
between-TEAM strength spread, which this module supplies from the board (see
``FieldPool``).

WHY BYES SHOW UP AT ALL: THE COVERAGE TRAP
------------------------------------------
Item 3.2 paid for this in blood and it is the single easiest way to get this
module wrong. The projection feed's BYE row and its "we have no forecast for this
player" row are BYTE-IDENTICAL (team set, opponent NULL, every stat NULL, scoring
0.0), so points cannot distinguish them. The only real signal is the OPPONENT
column, carried by ``valuation.WeeklyLine.played_weeks``. So
:func:`weekly_points_map` publishes points ONLY for weeks the feed actually
forecast a game, and "week absent from the dict" means, uniformly for skill and
D/ST alike, **this player does not play that week**. That single convention is
what makes bye coverage computed rather than approximated, and it is why the map
must be built here rather than by summing ``BoardEntry.house_points``.

It also means COVERAGE is readable from the map: a healthy 2026 player carries 16
of 17 weeks (one bye). A player carrying one or two is a coverage GAP, not a
sixteen-week bye, and :func:`grade_roster` says so in its reasons rather than
grading him as worthless.

WHAT THIS OBJECTIVE STILL CANNOT SEE (read this before optimising against it)
----------------------------------------------------------------------------
**There is no injury or availability model here.** A player is available in
every week the feed forecast a game for him, full stop. The consequence is
exact, not vague, and it was measured on the live board on 2026-08-30 (a real
16-man roster graded against its nine real rivals): with the starting lineup
already filled, adding ANY bench player moves ``expected_wins`` by
**+0.000000** — a 262.5-point backup QB, a 154.2-point TE, a 145.9-point WR and
a 115.6-point RB all price identically to each other and to an empty seat. The
only bench value this model can price is BYE COVERAGE. (On the SYNTHETIC-field
path a bench add does move the number a little, but not because the bench is
worth something: the player you take is a player the modelled rivals no longer
get.)

That has one sharp edge, and it is fenced rather than left live. Because a
one-deep K/DST slot is guaranteed to go empty on that player's bye, a SECOND
kicker or a SECOND defense was, before the fence, the highest-scoring available
add on a full roster (measured over 30 engine-vs-bots cells on the live board:
+0.053 expected wins for a second K and +0.040 for a second D/ST, against
+0.017 for the best free-agent RB and NEGATIVE for every other skill add, with
the two of them the top-ranked add in 22 of the 30) — the exact pathology
``core/marginal.py`` hard-caps with ``POSITION_CAPS`` (DST/K = 1). It is wrong
because in this league you STREAM those slots: 32 defenses and 32 kickers, ten
teams, waivers processing six mornings a week. So :data:`STREAM_LABEL` prices an
empty K/DST slot at the best waiver-tier replacement that week (see
:func:`stream_levels`) instead of at zero, symmetrically for you and for every
rival, AND caps the seatable count at one (:func:`capped_out`) — the credit
alone still left a backup defense worth +0.005 and top-ranked in 21 of 30 cells,
because D/ST is the one position whose week-to-week points really move. With
both halves a second K and a second D/ST price at EXACTLY the same +0.000 as a
second QB or TE, and the only positive add left is one that fills a real hole.

So: **maximise this objective for the STARTING NINE and for bye structure; do
not read it as a ranking of late-round bench picks.** Wiring a real availability
model (``ziggurat/core/availability.py``) is the change that would make bench
depth priceable, and until it lands every grade says so in its reasons.

STANDING RULES
--------------
Rule 1 — :func:`weekly_points_map` takes a keyword-only ``as_of`` with no default
and threads ``as_of``/``view`` straight into ``valuation.weekly_lines``; it never
widens the gate. Ships a leakage test. Rule 2 — no scoring constant lives here;
every point is a house point priced by ``scoring.py`` upstream, and sigma is a
dispersion prior, never a scoring number. Rule 6 — every number a human sees
ships with plain-language reasons, and every prior is labelled with its source
and cohort. Rule 8 — import-quarantined; nothing outside ``draft/`` imports it.

DETERMINISM. There is no randomness in this module at all: no sampling, no
wall clock, no dict-order dependence (every ordering is an explicit sort key).
The playoff model is a deterministic numerical integral, not a simulation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from types import MappingProxyType

from ziggurat.core.lineup import LineupFill, fill_lineup
from ziggurat.core.lineup_support import (
    DEFAULT_VARIANCE,
    VarianceModel,
    win_probability,
)
from ziggurat.core.valuation import (
    DEFAULT_ROSTER,
    DEFAULT_WEEKS,
    RosterStructure,
    WeeklyLine,
    replacement_levels,
    weekly_lines,
)
from ziggurat.data.nfl import base
from ziggurat.draft.bots import BoardEntry

# ---------------------------------------------------------------- league shape

#: The league's fantasy regular season: 14 head-to-head weeks (live ESPN
#: ``scheduleSettings``, re-read 2026-08-30). Every 2026 NFL bye lands inside it.
DEFAULT_REGULAR_SEASON_WEEKS = range(1, 15)

#: The playoff bracket: three one-week rounds. No NFL team byes in 15-17.
DEFAULT_PLAYOFF_WEEKS = (15, 16, 17)

#: Six of ten teams qualify (``scheduleSettings.playoffTeamCount``).
DEFAULT_PLAYOFF_TEAMS = 6

# ------------------------------------------------------------------- labels
#
# Rule 6: every modelling choice that is not a measurement gets a label that
# travels into the reason text, so a novice operator reads "this is a guess with
# a stated provenance" rather than a bare number.

FIELD_LABEL = (
    "hypothesis: dealt field — your nine opponents are modelled by dealing the "
    "board's best available players into nine rosters, position by position and "
    "week by week (each position dealt from the top, alternating direction between "
    "its own rounds, so the rival who takes the best RB1 takes the worst RB2; the "
    "FLEX is dealt last from the leftovers). Derived from the board itself, no "
    "invented points constant. MEASURED BIAS (live 2026 board, 2026-08-30, against "
    "the ten real rosters of a full engine-vs-bots draft): the dealt field scores "
    "125.8 in week 1 where the real ten average 120.7, and 121.6 in the heaviest "
    "bye week (11) where they average 114.5 — every modelled rival always starts "
    "his best available player and never carries a bye hole. So every win "
    "probability on this path is CONSERVATIVE by a stated amount: grading all ten "
    "rosters of one real draft this way gave 63.20 expected wins where the "
    "arithmetic truth is 70.0 and 4.73 playoff berths where the truth is 6.0, i.e. "
    "0.68 wins and 13.5 playoff points per team too low (the same ten graded "
    "against their REAL rivals give 70.000 and 6.08). It is a LEVEL bias, not a "
    "ranking one — 44 of the 45 team pairs ordered identically either way and the "
    "one flip was a 0.003-win tie — so trust the comparison and discount the "
    "headline percentage"
)

STREAM_LABEL = (
    "hypothesis: K/DST waiver stream — an empty kicker or defense slot is priced "
    "at the best replacement that would still be unrostered in a 10-team league "
    "that week (the 11th-best at that position among players actually playing), "
    "not at zero, because those two slots are STREAMED weekly in this league (32 "
    "kickers and 32 defenses, ten rosters, waivers processing six mornings a week; "
    "core/streaming.py owns the in-season version and core/marginal.py caps K/DST "
    "at one for the same reason). Applied symmetrically to you and to every "
    "modelled rival. UNVERIFIED against a real in-season week; without it this "
    "objective ranks a SECOND defense above every skill-position add"
)

BENCH_LIMITATION_LABEL = (
    "LIMITATION: no injury or availability model — a bench player who is not "
    "covering a bye is worth EXACTLY 0.000 expected wins here (measured on the "
    "live 2026 board: a 262-pt backup QB, a 154-pt WR and a 115-pt RB all priced "
    "identically at +0.0000). Maximise this objective for the starting nine and "
    "for bye structure; do NOT read it as a ranking of late-round bench depth"
)

PLAYOFF_LABEL = (
    "approximation: playoff odds from a normal approximation to each team's "
    "win total (Poisson-binomial -> Normal), integrated over your own win total "
    "on a fixed grid. Treats the nine rival win totals as independent given "
    "yours and ignores the head-to-head schedule (nobody has one yet) and "
    "points-for tiebreaks"
)

TITLE_LABEL = (
    "approximation: title odds = P(reach the bracket) x P(win each playoff week) "
    "against the strongest half of the field. Credits the top-two seeds their "
    "first-round bye; assumes playoff weeks are independent of seeding"
)

# ------------------------------------------------------------------ tunables

#: Grid resolution for the playoff integral over your own win total. 25 points
#: across +/-4 sigma; measured error vs a 401-point grid is < 1e-4 in
#: probability, at ~1/16th the cost (``test_playoff_grid_is_converged``).
_WIN_TOTAL_GRID = 25
_WIN_TOTAL_SPAN = 4.0

#: The two slots this league streams week to week. See :data:`STREAM_LABEL` and
#: :func:`stream_levels`. Deliberately the same pair ``core/marginal.py`` caps at
#: one and ``core/streaming.py`` ranks: three modules must not disagree about
#: which positions are replaceable from the waiver wire.
STREAMED_POSITIONS = ("DST", "K")

#: A win total's sigma floor. A team whose weekly win probabilities are all ~0 or
#: all ~1 has ~zero win-total variance; the normal approximation then becomes a
#: step function and the integral degenerates. Half a win is well below any real
#: season's spread (a 14-game Bernoulli field has sigma ~1.9).
_MIN_WIN_SIGMA = 0.5


class BoardKeyCollision(RuntimeError):
    """Two board players resolved to the same ``player_id``.

    Structurally impossible on today's derivation (espn_id and gsis_id are
    per-player keys, ``DST:<team>`` is per-team, ``<POS>:<rank>`` is per-rank), so
    if it fires the id derivation has drifted away from ``simulator.load_board``
    and EVERY grade keyed on the colliding id is silently wrong. Raised rather
    than merged — a merged key is invisible (Rule 6).
    """


class GradeInputError(ValueError):
    """The grader was asked for something it cannot honestly compute.

    Refuse-rather-than-guess, the same discipline ``marginal.resolve_weeks`` and
    ``waiver`` use: a fabricated opponent or a silently empty week reads exactly
    like a real answer to a novice.
    """


# ========================================================================
#                       1.  the weekly points map
# ========================================================================


class WeeklyPointsMap(dict):
    """``player_id -> {week: house points}``, plus the board metadata beside it.

    IT IS A PLAIN ``dict[str, dict[int, float]]`` and satisfies that contract
    exactly — ``m[pid][week]`` is this week's house points, iteration yields
    player ids, ``len`` is the player count. Three read-only attributes ride
    alongside because :func:`grade_roster` needs positions to model the field and
    ``dict`` has nowhere else to put them:

      ``positions``  player_id -> canonical QB/RB/WR/TE/DST/K
      ``names``      player_id -> display name (may be None)
      ``teams``      player_id -> normalized NFL abbreviation (may be None)

    A caller who builds a plain ``dict`` instead loses only the automatic field
    derivation, and :func:`grade_roster` then asks for ``positions=`` explicitly
    rather than guessing (it raises if it has neither).

    THE MISSING-WEEK CONVENTION, restated because everything depends on it: a
    week that is ABSENT from a player's inner dict means HE DOES NOT PLAY THAT
    WEEK. It is never "zero points" and never "look it up somewhere else". See
    the module docstring's coverage-trap section.
    """

    __slots__ = ("positions", "names", "teams", "_field_cache")

    def __init__(self, points, *, positions, names, teams):
        super().__init__(points)
        self.positions = MappingProxyType(dict(positions))
        self.names = MappingProxyType(dict(names))
        self.teams = MappingProxyType(dict(teams))
        self._field_cache: dict[tuple, FieldPool] = {}

    def scheduled_weeks(self, player_id: str) -> int:
        """How many weeks the feed actually forecasts a game for this player.

        16 of a 17-week span is a healthy player with one bye. A small number is
        a COVERAGE GAP in the feed, not a sixteen-week bye (item 3.2's A.J. Brown
        case), and grading must say which it is looking at.
        """
        return len(self.get(player_id, ()))


def _board_player_id(
    *, position: str, espn_id, gsis_id, team: str | None, overall_rank: int
) -> str:
    """The id ``simulator.load_board`` gives this player. Mirrored DELIBERATELY.

    A silent divergence here is the single most dangerous failure this module
    has: every roster would grade to an empty lineup, every week would look like
    a hole, and nothing would raise. ``test_key_space_matches_load_board_exactly`` pins
    the mirror against the real ``load_board`` on a built board, and
    :func:`board_key_coverage` is the runtime version of the same check.
    """
    if position == "DST":
        return str(espn_id or gsis_id or f"DST:{team}")
    return str(espn_id or gsis_id or f"{position}:{overall_rank}")


def weekly_points_map(
    conn,
    *,
    as_of,
    season,
    source: str = "sleeper_rotowire",
    weeks: Iterable[int] | None = None,
    roster: RosterStructure = DEFAULT_ROSTER,
    view: base.AsOfView = "historical",
    denoise_kdst: bool = True,
    # --- additive, all defaulted ---
    rank_weeks: Iterable[int] | None = None,
    board: Sequence[BoardEntry] | None = None,
    lines: Mapping[tuple, WeeklyLine] | None = None,
) -> WeeklyPointsMap:
    """Per-week house points for every board player, keyed like ``load_board``.

    Rule 1: ``as_of`` is keyword-only with no default and is threaded, with
    ``view``, straight into ``valuation.weekly_lines`` — which is where the gate
    is enforced. This layer never widens it.

    ``weeks=None`` means ``valuation.DEFAULT_WEEKS`` (NFL weeks 1-17), which is
    what ``load_board`` uses and what the fantasy season needs: 14 regular-season
    weeks plus the three playoff weeks. (Item 3.2's in-season modules RAISE on
    ``weeks=None`` because there "the season" is ambiguous; at draft time the
    full season is exactly the question, so the default stands.)

    THE POINTS ARE RESTRICTED TO SCHEDULED WEEKS. Only weeks whose projection row
    carried an OPPONENT are published. That is the one signal that separates a
    bye from a missing forecast (module docstring), and it makes "absent" mean
    "not playing" uniformly across skill players (bye row present, all-NULL) and
    D/ST (bye row absent entirely).

    THE IDS, AND WHY ``weeks`` DOES NOT TOUCH THEM. ``load_board`` derives
    ``BoardEntry.player_id`` from the VOR board: ``espn_id or gsis_id``, falling
    back to ``DST:<team>`` or ``<POS>:<overall rank>``. Thirty-seven percent of
    the live 2026 board (1,215 of 3,264 entries) lands on that rank fallback, so
    the rank cannot be skipped — this function reproduces ``build_valuation``'s
    ranking purely to reconstruct it.

    That reproduction depends on the WEEK SPAN the ranking was computed over, and
    the natural call ``weekly_points_map(..., weeks=range(1, 15))`` ("grade the
    regular season") used to re-rank over 14 weeks while the board had been
    ranked over 17 — measured on the live board: 167 board entries silently fell
    out of the map, four of them priced (a kicker worth 107.1 house points), each
    then graded as "no projection at all" in every week. So the two spans are now
    SEPARATE parameters: ``weeks`` chooses which weeks are PUBLISHED, while
    ``rank_weeks`` (default ``valuation.DEFAULT_WEEKS``, exactly what
    ``load_board`` passes) chooses the span the id space is derived on. Change
    ``rank_weeks`` only to mirror a board built with a non-default ``weeks=``.

    ``roster`` and ``denoise_kdst`` are the OTHER two inputs to that ranking, and
    ``load_board`` always leaves them at ``build_valuation``'s defaults, so a
    non-default value here is provably a key-space divergence (measured:
    ``RosterStructure(teams=12)`` drops 831 of 3,264 board entries, 8 of them
    priced). They are therefore accepted only at their defaults, and refused
    loudly otherwise — ``grade_roster``'s own ``roster=`` is the parameter a
    caller grading a differently-shaped league actually wants.

    ``lines``, when given, is a :func:`~ziggurat.core.valuation.weekly_lines` map
    the caller has ALREADY built, so the draft-night launch pays for ONE pass over
    the projections table instead of three (item 3.11 audit finding 1; the pass is
    3.55 s on the live 2026 board). Same contract as ``build_valuation``'s and
    ``build_kicker_board``'s: the caller owns as_of/season/source/view, the week
    span is checked here.

    ``board``, when given, is checked: every priced entry on it must be in the
    map or this RAISES. That is :func:`board_key_coverage` wired up as a gate
    instead of left to a caller who will not run it.

    AN EMPTY RESULT IS RETURNED, NOT RAISED. "Nothing is knowable at this as_of"
    is a true answer and the leakage tests read it directly. The refusal lives
    one level up, in :func:`grade_roster`, because that is where an empty map
    stops being a fact and becomes a fabricated 7.00 expected wins.
    """
    if roster != DEFAULT_ROSTER or not denoise_kdst:
        raise GradeInputError(
            "weekly_points_map's roster= and denoise_kdst= exist only to mirror "
            "build_valuation's VOR ranking, which is what load_board keys "
            "BoardEntry.player_id on — and load_board always calls build_valuation "
            "at its DEFAULTS. A non-default value here silently re-partitions the "
            "'<POS>:<rank>' id space away from the board (measured on the live 2026 "
            "board: teams=12 drops 831 of 3,264 entries, denoise_kdst=False drops "
            "68), and every dropped player then grades as unavailable in all 17 "
            "weeks. If you are grading a differently-shaped league, pass roster= to "
            "grade_roster instead; this map must stay on the board's id space."
        )
    rank_week_set = frozenset(int(w) for w in (DEFAULT_WEEKS if rank_weeks is None else rank_weeks))
    publish_set = frozenset(int(w) for w in (DEFAULT_WEEKS if weeks is None else weeks))
    if not rank_week_set:
        raise GradeInputError("rank_weeks is empty — there is no span to rank the board on")

    # ONE fetch over the union; the ranking then re-sums over rank_weeks only, so
    # it reproduces build_valuation(weeks=rank_weeks) exactly while `weeks`
    # controls publication alone. (get_projections does not filter by week, so the
    # wider span costs nothing.)
    span = sorted(rank_week_set | publish_set)
    if lines is None:
        lines = weekly_lines(
            conn, as_of=as_of, season=season, weeks=span,
            source=source, view=view,
        )
    else:
        # THE CALLER OWNS as_of/season/source/view (a gate this layer did not run
        # cannot be re-checked here) — the same contract, word for word, that
        # build_valuation's and build_kicker_board's `lines=` state. The one half
        # that IS checkable is a map built over a WIDER window: the ranking below
        # re-sums over rank_weeks, so stray weeks would rank the board on points
        # that are not on it. (A NARROWER map is indistinguishable from a feed
        # that simply has fewer weeks, so it is not checkable here — it surfaces
        # as the assert_board_coverage divergence at the bottom of this function.)
        stray = {w for ln in lines.values() for w in ln.points} - set(span)
        if stray:
            raise GradeInputError(
                "the weekly_lines map handed to weekly_points_map covers weeks "
                f"{sorted(stray)} outside weeks={span[0]}-{span[-1]}; it was built "
                "over a different window than this map claims. The board's id space "
                "is derived from this ranking, so build both over the same span."
            )
    if not lines:
        return WeeklyPointsMap({}, positions={}, names={}, teams={})

    # --- reproduce build_valuation's VOR ranking, for the id fallback ONLY.
    # Identical inputs, identical helper, identical (stable) sort as
    # valuation.build_valuation, over the identical line ordering -> identical
    # overall_rank. Nothing else in this module reads vor.
    #
    # A line with no row inside rank_weeks would not EXIST under
    # build_valuation(weeks=rank_weeks), so it must not take a rank here either;
    # dropping it preserves the relative order of the rest, and the sort is
    # stable, so the ranks are identical.
    ranked = [ln for ln in lines.values() if any(w in rank_week_set for w in ln.points)]
    rank_points = {
        ln.key: sum(p for w, p in ln.points.items() if w in rank_week_set) for ln in ranked
    }
    by_pos: dict[str, list[float]] = {}
    for line in ranked:
        by_pos.setdefault(line.position, []).append(rank_points[line.key])
    for pts_list in by_pos.values():
        pts_list.sort(reverse=True)
    replacement, _started = replacement_levels(by_pos, roster, denoise_kdst=denoise_kdst)

    ordered = sorted(
        ranked,
        key=lambda ln: rank_points[ln.key] - replacement.get(ln.position, 0.0),
        reverse=True,
    )

    points: dict[str, dict[int, float]] = {}
    positions: dict[str, str] = {}
    names: dict[str, str | None] = {}
    teams: dict[str, str | None] = {}
    for rank, line in enumerate(ordered, start=1):
        team = (
            base.TEAM_ALIASES.get(str(line.team).upper(), str(line.team).upper())
            if line.team
            else None
        )
        pid = _board_player_id(
            position=line.position,
            espn_id=line.espn_id,
            gsis_id=line.gsis_id,
            team=team,
            overall_rank=rank,
        )
        if pid in points:
            raise BoardKeyCollision(
                f"player_id {pid!r} resolved twice ({names.get(pid)!r} and "
                f"{line.player!r}) — the id derivation has drifted from "
                "simulator.load_board and every grade keyed on it would be wrong"
            )
        # Only the weeks the feed forecast a real game, and only the PUBLISHED
        # span. See the module docstring.
        points[pid] = {
            wk: pts for wk, pts in sorted(line.points.items())
            if wk in line.played_weeks and wk in publish_set
        }
        positions[pid] = line.position
        names[pid] = line.player
        teams[pid] = team

    out = WeeklyPointsMap(points, positions=positions, names=names, teams=teams)
    if board is not None:
        assert_board_coverage(board, out)
    return out


def assert_board_coverage(
    board: Sequence[BoardEntry], weekly: Mapping[str, Mapping[int, float]]
) -> None:
    """Raise unless every PRICED board entry has a row in the points map.

    ``load_board`` unions the rest of the ESPN universe onto the board as UNPRICED
    entries (``house_points == 0.0``) that no projection exists for; those are
    legitimately absent — 2,693 of the live board's 3,229 mapped players carry no
    scheduled week at all. Anything the valuation actually priced must be present
    AND carry at least one week, because neither failure raises on its own: both
    grade as a player who never plays, under a reason that blames the FEED for a
    gap this module created.
    """
    missing = [
        e for e in board
        if float(e.house_points) > 0.0 and not weekly.get(e.player_id)
    ]
    if not missing:
        return
    worst = sorted(missing, key=lambda e: -float(e.house_points))[:5]
    shown = ", ".join(
        f"{e.player_id} ({e.position}, {e.house_points:.1f} pts)" for e in worst
    )
    raise GradeInputError(
        f"{len(missing)} of {len(board)} board entries are PRICED but missing from "
        f"the weekly points map — worst: {shown}. The id spaces have diverged: "
        "weekly_points_map's rank_weeks/source/season/as_of must match the ones "
        "load_board was built with. Refusing to grade rosters against a map that "
        "would report these players as having no projection at all."
    )


def board_key_coverage(
    board: Sequence[BoardEntry], weekly: Mapping[str, Mapping[int, float]]
) -> tuple[int, int, tuple[str, ...]]:
    """``(matched, total, sample of unmatched ids)`` for a board against a map.

    The runtime form of the key-space check. On the live 2026 board the expected
    shape is: every entry that ``build_valuation`` priced is present; the ~35
    ESPN-universe-only union entries (no projection at all) are not. A match rate
    that collapses means the derivations have drifted.
    """
    unmatched = [e.player_id for e in board if e.player_id not in weekly]
    return len(board) - len(unmatched), len(board), tuple(sorted(unmatched)[:10])


# ========================================================================
#                       2.  the opponent field
# ========================================================================


@dataclass(frozen=True)
class FieldPool:
    """Per-week, per-position ladders of the board's best available players.

    THE EXPENSIVE, ROSTER-INDEPENDENT HALF of the opponent model, built once and
    reused across every ``grade_roster`` call in a Monte-Carlo rollout. Each
    ladder entry is ``(points, sigma_squared, player_id)`` sorted by points
    descending (ties broken by id, so the deal is deterministic). Only the top
    ``depth`` at each position are kept — nine rival teams cannot reach past that
    even if the whole roster under test sits at one position.

    Dealing (the cheap half) is :func:`field_lineups`.
    """

    weeks: tuple[int, ...]
    ladders: Mapping[int, Mapping[str, tuple[tuple[float, float, str], ...]]]
    roster: RosterStructure
    label: str = FIELD_LABEL
    # --- additive, all defaulted ---
    #: The sigma model the ladders' variances were computed with. Carried so
    #: ``grade_roster`` can refuse a pool built under a different one instead of
    #: silently pricing this roster's variance against that pool's (None = a
    #: hand-built pool that cannot be checked).
    variance: VarianceModel | None = None
    #: ``week -> position -> (points, sigma^2, player_id)`` for the streamed
    #: slots. See :func:`stream_levels` and :data:`STREAM_LABEL`.
    streams: Mapping[int, Mapping[str, tuple[float, float, str]]] = dc_field(
        default_factory=dict
    )

    @property
    def opponents(self) -> int:
        return self.roster.teams - 1


def _pool_depth(pos: str, roster: RosterStructure) -> int:
    """How deep a position ladder must go for nine rivals plus one full roster.

    Worst case the roster under test holds ``active_slots`` players at this one
    position, all of whom must be skipped before the rivals start dealing.
    """
    n_opp = max(1, roster.teams - 1)
    per_team = roster.starters.get(pos, 0) + (
        roster.flex_slots if pos in roster.flex_positions else 0
    )
    return per_team * n_opp + roster.active_slots + 2


def build_field_pool(
    weekly: Mapping[str, Mapping[int, float]],
    positions: Mapping[str, str],
    *,
    weeks: Sequence[int],
    roster: RosterStructure = DEFAULT_ROSTER,
    variance: VarianceModel | None = None,
) -> FieldPool:
    """Rank the board by week-and-position, with each player's sigma precomputed.

    Cost is O(board x weeks x log) — 5 ms on the live 2026 board — and it does
    NOT depend on the roster being graded, which is exactly why it is a separate
    object: build it once outside a rollout loop and hand it to
    :func:`grade_roster` as ``field=``. (The map returned by
    :func:`weekly_points_map` memoises it for you, so the naive call is fast too.)
    """
    var = DEFAULT_VARIANCE if variance is None else variance
    weeks = tuple(sorted(set(int(w) for w in weeks)))
    depths = {pos: _pool_depth(pos, roster) for pos in
              set(roster.starters) | set(roster.flex_positions)}

    ladders: dict[int, dict[str, tuple[tuple[float, float, str], ...]]] = {}
    for wk in weeks:
        by_pos: dict[str, list[tuple[float, float, str]]] = {}
        for pid, pos in positions.items():
            wpts = weekly.get(pid)
            if wpts is None or wk not in wpts:
                continue  # not playing that week — never enters a rival lineup
            pts = float(wpts[wk])
            sigma = var.sigma(pos, pts)
            by_pos.setdefault(pos, []).append((pts, sigma * sigma, pid))
        trimmed: dict[str, tuple[tuple[float, float, str], ...]] = {}
        for pos, rows in by_pos.items():
            rows.sort(key=lambda r: (-r[0], r[2]))
            trimmed[pos] = tuple(rows[: depths.get(pos, len(rows))])
        ladders[wk] = trimmed
    return FieldPool(
        weeks=weeks,
        ladders=ladders,
        roster=roster,
        variance=var,
        streams=stream_levels(ladders, roster=roster),
    )


def stream_levels_from_board(
    weekly: Mapping[str, Mapping[int, float]],
    positions: Mapping[str, str],
    *,
    weeks: Sequence[int],
    roster: RosterStructure = DEFAULT_ROSTER,
    variance: VarianceModel | None = None,
) -> dict[int, dict[str, tuple[float, float, str]]]:
    """:func:`stream_levels` without building a whole :class:`FieldPool`.

    The opponent-rosters path needs the streamed levels but not the dealt field,
    and a full pool costs 5.3 ms on the live board against ~1 ms for the two
    ladders this actually reads — which matters because that path is called
    inside a rollout.
    """
    var = DEFAULT_VARIANCE if variance is None else variance
    wanted = set(STREAMED_POSITIONS)
    ladders: dict[int, dict[str, tuple[tuple[float, float, str], ...]]] = {}
    for wk in sorted(set(int(w) for w in weeks)):
        by_pos: dict[str, list[tuple[float, float, str]]] = {}
        for pid, pos in positions.items():
            if pos not in wanted:
                continue
            wpts = weekly.get(pid)
            if wpts is None or wk not in wpts:
                continue
            pts = float(wpts[wk])
            sigma = var.sigma(pos, pts)
            by_pos.setdefault(pos, []).append((pts, sigma * sigma, pid))
        for rows in by_pos.values():
            rows.sort(key=lambda r: (-r[0], r[2]))
        ladders[wk] = {pos: tuple(rows) for pos, rows in by_pos.items()}
    return stream_levels(ladders, roster=roster)


def stream_levels(
    ladders: Mapping[int, Mapping[str, tuple[tuple[float, float, str], ...]]],
    *,
    roster: RosterStructure = DEFAULT_ROSTER,
) -> dict[int, dict[str, tuple[float, float, str]]]:
    """The waiver-tier K and D/ST for each week: ``week -> pos -> (pts, var, id)``.

    THE RULE, and why this exists at all. An empty starting slot normally costs
    exactly what it is worth: nothing. That is right for a skill slot and WRONG
    for the two one-deep slots this league streams — if your kicker byes in week
    7 you do not field eight players, you add a kicker on Tuesday, for free, from
    a pool of twenty-two unrostered ones. Pricing that slot at zero made a SECOND
    kicker the highest-value add on a full roster (see the module docstring);
    pricing it at replacement makes it worth ~0, which is what it is.

    The level is the ``roster.teams``-th entry of that week's ladder, 0-indexed —
    i.e. the best player at that position who would still be unrostered if every
    one of the ten teams held exactly one. That is ``replacement_levels``'
    reasoning applied to one week, and it comes from the board rather than from a
    constant (Rule 2). A ladder shorter than that (a heavy bye week at a thin
    position) falls back to its last entry, and an empty one yields nothing at
    all — a hole that really is a hole.
    """
    out: dict[int, dict[str, tuple[float, float, str]]] = {}
    for wk, ladder in ladders.items():
        wk_out: dict[str, tuple[float, float, str]] = {}
        for pos in STREAMED_POSITIONS:
            rows = ladder.get(pos, ())
            if not rows:
                continue
            row = rows[min(roster.teams, len(rows) - 1)]
            if row[0] <= 0.0:
                continue  # an empty slot scores 0 and 0 beats a negative D/ST
            wk_out[pos] = row
        if wk_out:
            out[wk] = wk_out
    return out


def field_lineups(
    pool: FieldPool, week: int, *, exclude: frozenset[str] = frozenset()
) -> tuple[tuple[float, float], ...]:
    """Deal the week's board into the nine rival lineups: ``((mu, var), ...)``.

    THE DEAL, stated exactly, because the docstring used to overstate it. Each
    position is dealt from the top of its own ladder, alternating direction
    BETWEEN THAT POSITION'S OWN ROUNDS: the rival who takes the best RB1 takes
    the worst RB2. The alternation does NOT carry across positions, so at the
    four one-deep slots (QB, TE, D/ST, K) seat 0 takes the best of each and the
    field is ordered seat 0 -> seat 8 (measured on the live 2026 board, week 1:
    136.7 down to 118.6). The FLEX is dealt last from whatever RB/WR/TE remain,
    pooled, re-sorted and dealt the same way.

    WHY NOT A TRUE CONTINUOUS SNAKE (measured 2026-08-30, live board, against the
    ten real rosters of a full engine-vs-bots draft; population sd of the nine or
    ten weekly lineup means, averaged over weeks 1/5/8/9/11/13/14):

        real drafted rosters                          7.47
        this deal (alternating within a position)     6.72
        alternation carried ACROSS positions          3.53   <- far too uniform
        no alternation at all (straight rank order)   9.20   <- too polarised

    So the shipped deal is the closest of the three to a real room, and the
    "obvious fix" of carrying the snake across positions would halve the field's
    spread and make every playoff probability worse. It is kept, and named
    honestly, rather than corrected into a better-sounding model.

    ``exclude`` is the roster under test: those players genuinely are not
    available to the rivals, and skipping them is cheap because the ladders are
    pre-sorted (this is why the expensive half is cached separately).

    Rivals with a short pool (a position the board cannot fill nine deep in a
    heavy bye week) simply get fewer starters — a real hole in a real rival's
    week, never a fabricated one. A pool that does not COVER the week, or that
    has nothing left to deal, raises: nine rivals scoring 0.0 is not a weak
    field, it is a missing one, and it reads as a 100% win.
    """
    roster = pool.roster
    n = pool.opponents
    if week not in set(pool.weeks):
        raise GradeInputError(
            f"week {week} is not in this field pool (it covers weeks "
            f"{list(pool.weeks)}). Dealing it would hand every rival 0.0 points "
            "and report a certain win. Build the pool over the weeks you intend "
            "to grade — including the playoff weeks."
        )
    ladders = pool.ladders.get(week, {})

    heads: dict[str, list[tuple[float, float, str]]] = {}
    for pos, ladder in ladders.items():
        heads[pos] = [row for row in ladder if row[2] not in exclude]

    mu = [0.0] * n
    var = [0.0] * n
    cursor: dict[str, int] = dict.fromkeys(heads, 0)

    def deal(pos: str, rounds: int) -> None:
        rows = heads.get(pos)
        if not rows:
            return
        i = cursor.get(pos, 0)
        for r in range(rounds):
            seats = range(n) if r % 2 == 0 else range(n - 1, -1, -1)
            for seat in seats:
                if i >= len(rows):
                    cursor[pos] = i
                    return
                pts, sig2, _pid = rows[i]
                i += 1
                if pts <= 0.0:
                    continue  # an empty slot scores 0 and 0 beats a negative D/ST
                mu[seat] += pts
                var[seat] += sig2
        cursor[pos] = i

    for pos in ("QB", "RB", "WR", "TE", "DST", "K"):
        req = roster.starters.get(pos, 0)
        if req:
            deal(pos, req)

    # FLEX: the best remaining RB/WR/TE, pooled across positions, snaked once.
    if roster.flex_slots:
        leftovers: list[tuple[float, float, str]] = []
        for pos in roster.flex_positions:
            rows = heads.get(pos, ())
            leftovers.extend(rows[cursor.get(pos, 0):])
        leftovers.sort(key=lambda r: (-r[0], r[2]))
        i = 0
        for r in range(roster.flex_slots):
            seats = range(n) if r % 2 == 0 else range(n - 1, -1, -1)
            for seat in seats:
                if i >= len(leftovers):
                    break
                pts, sig2, _pid = leftovers[i]
                i += 1
                if pts <= 0.0:
                    continue
                mu[seat] += pts
                var[seat] += sig2

    if not any(m > 0.0 for m in mu):
        raise GradeInputError(
            f"week {week}: the field pool dealt {n} rivals who all score 0.0 — "
            "there is nobody left to deal (the positions map covers only the "
            "roster under test, or the whole board is out this week). That is a "
            "MISSING opponent, not a weak one, and it grades as a certain win."
        )
    return tuple(zip(mu, var, strict=True))


# ========================================================================
#                       3.  grading one roster
# ========================================================================


@dataclass(frozen=True)
class SeasonGrade:
    """One roster, graded week by week. Every field is a number a human reads.

    ``objective``       the number to MAXIMISE (``expected_wins`` by default)
    ``expected_wins``   sum over the regular-season weeks of P(win that week)
    ``playoff_prob``    P(finish top ``playoff_teams`` of ``teams``)
    ``title_prob``      P(playoffs) x P(win the bracket)
    ``weekly_means``    DENSE from week 1: ``weekly_means[7]`` is always week 8.
                        Weeks outside the graded span read 0.0.
    ``hole_weeks``      weeks whose seated lineup left a REQUIRED slot empty
    ``reasons``         plain language, Rule 6

    The extra fields below the contract carry the detail the reasons summarise;
    they all default, so the seven-field contract above is what a caller must
    know.
    """

    objective: float
    expected_wins: float
    playoff_prob: float
    title_prob: float
    weekly_means: tuple[float, ...]
    hole_weeks: tuple[int, ...]
    reasons: tuple[str, ...]

    # --- additive detail (all defaulted; the contract above is unchanged) ---
    win_probs: tuple[float, ...] = ()          # dense from week 1, like weekly_means
    hole_detail: tuple[tuple[int, tuple[str, ...]], ...] = ()   # (week, empty slots)
    opponent_means: tuple[float, ...] = ()     # dense from week 1
    playoff_bye_prob: float = 0.0              # P(top-two seed => a first-round bye)
    regular_season_weeks: tuple[int, ...] = ()
    playoff_weeks: tuple[int, ...] = ()
    lineups: Mapping[int, LineupFill] = dc_field(default_factory=dict)
    #: ``(week, ((slot, points), ...))`` — K/DST slots priced at the waiver-tier
    #: replacement instead of left empty (:data:`STREAM_LABEL`). These weeks are
    #: deliberately NOT in ``hole_weeks``: a streamed slot is a Tuesday add, not
    #: a hole. ``lineups[week]`` still shows the raw seated lineup, so
    #: ``lineups[week].total`` is BELOW ``weekly_means[week - 1]`` by exactly the
    #: streamed points.
    streamed_slots: tuple[tuple[int, tuple[tuple[str, float], ...]], ...] = ()
    #: "on" | "off" (caller passed ``stream_kdst=False``) | "unavailable" (no
    #: positions map, so no waiver tier could be priced from the board).
    stream_state: str = "off"


def _seat_week(
    entries: Sequence[BoardEntry],
    weekly: Mapping[str, Mapping[int, float]],
    week: int,
    roster: RosterStructure,
    variance: VarianceModel,
    streams: Mapping[int, Mapping[str, tuple[float, float, str]]] | None = None,
    capped: frozenset[str] = frozenset(),
) -> tuple[LineupFill, float, float, dict[str, float], tuple[tuple[str, float], ...]]:
    """Seat ONE week and price it: ``(fill, mu, var, points, streamed)``.

    Availability, not points, decides who is seatable: a player whose inner dict
    has no entry for this week DOES NOT PLAY (bye, or no forecast at all) and is
    withheld from the seater, so the slot behind him is genuinely empty rather
    than filled at zero. That is the whole difference from the season-total
    metric.

    ``streams`` (see :func:`stream_levels`) is the ONE exception, and it applies
    to the two slots this league streams: an empty K or D/ST slot is credited the
    week's waiver-tier replacement instead of zero, and comes back in
    ``streamed`` as ``(slot label, points)`` so the reasons can say so. The same
    mapping is passed when rivals are seated, so the credit is symmetric and the
    league sums are undisturbed. ``capped`` is the other half of that model (see
    :func:`capped_out`): a SECOND kicker or defense is not seatable at all, so
    holding one is worth exactly nothing rather than a little — which is the
    whole point, since "a little" is enough to win a round-16 pick.

    Variance is item 3.5's formula verbatim — ``Sum sigma_i^2 + 2 rho Sum_(i<j)
    sigma_i sigma_j`` with rho non-zero only for a QB and a pass-catcher on his
    own NFL team. It is recomputed here from the PUBLIC ``VarianceModel`` API
    rather than imported, because ``lineup_support``'s version is private and
    typed to that module's own private seat record.
    """
    points: dict[str, float] = {}
    available: dict[str, bool] = {}
    positions: dict[str, str] = {}
    for e in entries:
        wpts = weekly.get(e.player_id)
        plays = wpts is not None and week in wpts
        points[e.player_id] = float(wpts[week]) if plays else 0.0
        available[e.player_id] = plays and e.player_id not in capped
        positions[e.player_id] = e.position

    fill = fill_lineup(
        [e.player_id for e in entries], positions, points,
        roster=roster, available=available,
    )

    team_of = {e.player_id: e.team for e in entries}
    sigmas = {k: variance.sigma(positions[k], points[k]) for k in fill.starters}
    var = sum(s * s for s in sigmas.values())
    rho = variance.correlation_qb_passcatcher
    if rho:
        for qb in (k for k in fill.starters if positions[k] == "QB"):
            qteam = team_of.get(qb)
            if qteam is None:
                continue
            for k in fill.starters:
                if k != qb and positions[k] in ("WR", "TE") and team_of.get(k) == qteam:
                    var += 2.0 * rho * sigmas[qb] * sigmas[k]

    mu = fill.total
    streamed: list[tuple[str, float]] = []
    if streams:
        week_streams = streams.get(week, {})
        for label in fill.empty_slots:
            # Slot labels are the position for a one-deep slot ("DST") and
            # position+index when a league starts more than one ("DST1"); strip
            # the index so this does not silently stop streaming under a
            # non-default RosterStructure.
            level = week_streams.get(label.rstrip("0123456789"))
            if level is None:
                continue
            pts, sig2, _pid = level
            mu += pts
            var += sig2
            streamed.append((label, pts))
    return fill, mu, var, points, tuple(streamed)


def capped_out(
    entries: Sequence[BoardEntry],
    weekly: Mapping[str, Mapping[int, float]],
    weeks: Sequence[int],
    *,
    roster: RosterStructure = DEFAULT_ROSTER,
) -> frozenset[str]:
    """The K/D/ST beyond the first that this roster may not seat (``POSITION_CAPS``).

    The second half of the streamed-slot model, and the half that decides whether
    a draft optimiser wastes a pick. With an empty K/DST slot priced at the
    waiver tier, a backup defense is still worth a sliver — it can be started in
    the primary's bye week and D/ST is the one position whose week-to-week points
    really move (CV ~12% against ~1% for skill). Measured on the live board over
    30 engine-vs-bots cells, that sliver was +0.005 expected wins, which is
    nothing to a human and still the ARGMAX for a search: the second D/ST was the
    best available add in 21 of 30 cells with every skill add priced at exactly
    +0.000.

    So the same cap ``core/marginal.py`` applies in-season (``POSITION_CAPS``,
    DST/K hard 1, for exactly this reason) applies here: you may seat as many
    kickers and defenses as the league STARTS (one of each here); the bye week is
    covered from waivers like everybody else's. Ranked by points over the graded
    weeks, ties broken by id — deterministic.
    """
    week_set = set(int(w) for w in weeks)
    out: set[str] = set()
    for pos in STREAMED_POSITIONS:
        keep = max(1, roster.starters.get(pos, 1))
        held = [e for e in entries if e.position == pos]
        if len(held) <= keep:
            continue
        held.sort(
            key=lambda e: (
                -sum(p for w, p in weekly.get(e.player_id, {}).items() if w in week_set),
                e.player_id,
            )
        )
        out.update(e.player_id for e in held[keep:])
    return frozenset(out)


def _opponent_week(
    week: int,
    *,
    opponent_rosters: Mapping[int, Sequence[BoardEntry]] | None,
    weekly: Mapping[str, Mapping[int, float]],
    pool: FieldPool | None,
    exclude: frozenset[str],
    roster: RosterStructure,
    variance: VarianceModel,
    streams: Mapping[int, Mapping[str, tuple[float, float, str]]] | None = None,
    capped: Mapping[int, frozenset[str]] | None = None,
) -> tuple[tuple[float, float], ...]:
    """The rivals' ``(mu, var)`` for one week — real rosters if we have them."""
    if opponent_rosters is not None:
        out = []
        for slot, entries in sorted(opponent_rosters.items()):
            _fill, mu, var, _pts, _streamed = _seat_week(
                entries, weekly, week, roster, variance, streams,
                (capped or {}).get(slot, frozenset()),
            )
            out.append((mu, var))
        return tuple(out)
    if pool is None:                       # unreachable via grade_roster
        raise GradeInputError(
            "no opponent rosters and no field pool — nothing to play against"
        )
    return field_lineups(pool, week, exclude=exclude)


def _win_total_moments(probs: Sequence[float]) -> tuple[float, float]:
    """Mean and sigma of a win total from per-week win probabilities.

    Poisson-binomial: mean ``Sum p``, variance ``Sum p(1-p)``. These two moments
    are EXACT; only the Normal shape fitted to them (in
    :func:`_seed_probabilities`) is an approximation, and it is the one
    ``PLAYOFF_LABEL`` names.
    """
    mean = math.fsum(probs)
    var = math.fsum(p * (1.0 - p) for p in probs)
    return mean, max(math.sqrt(max(var, 0.0)), _MIN_WIN_SIGMA)


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """``Phi((x - mu) / sigma)`` — routed through item 3.5's own win-probability
    so this module never carries a second normal CDF."""
    return win_probability(x, mu, 0.0, sigma * sigma)


def _seed_probabilities(
    own: tuple[float, float],
    others: Sequence[tuple[float, float]],
    *,
    playoff_teams: int,
    bye_seeds: int,
) -> tuple[float, float]:
    """``(P(make the bracket), P(top-``bye_seeds`` seed))``.

    You qualify iff at most ``playoff_teams - 1`` rivals finish above you. Given
    YOUR win total ``w`` the rivals' totals are treated as independent, so the
    count of rivals above you is Poisson-binomial in ``q_j(w)`` and a small DP
    gives its whole distribution exactly. Integrating that over your own win
    total on a fixed grid is the outer step. Deterministic; no sampling.
    """
    mu_own, sd_own = own
    lo, hi = mu_own - _WIN_TOTAL_SPAN * sd_own, mu_own + _WIN_TOTAL_SPAN * sd_own
    step = (hi - lo) / (_WIN_TOTAL_GRID - 1)

    make = 0.0
    bye = 0.0
    weight_total = 0.0
    for i in range(_WIN_TOTAL_GRID):
        w = lo + i * step
        z = (w - mu_own) / sd_own
        weight = math.exp(-0.5 * z * z)
        if weight <= 0.0:
            continue
        # distribution of "how many rivals finish above w"
        dist = [1.0]
        for mu_j, sd_j in others:
            q = 1.0 - _normal_cdf(w, mu_j, sd_j)
            nxt = [0.0] * (len(dist) + 1)
            for k, pk in enumerate(dist):
                if pk == 0.0:
                    continue
                nxt[k] += pk * (1.0 - q)
                nxt[k + 1] += pk * q
            dist = nxt
        make += weight * math.fsum(dist[: max(0, playoff_teams)])
        bye += weight * math.fsum(dist[: max(0, bye_seeds)])
        weight_total += weight
    if weight_total == 0.0:
        return 0.0, 0.0
    return make / weight_total, bye / weight_total


def _bye_seeds(playoff_teams: int, rounds: int) -> int:
    """First-round byes implied by a bracket: ``2**rounds - playoff_teams``.

    Six teams over three one-week rounds -> a bracket of eight -> two byes, which
    is exactly this league's shape.
    """
    if rounds <= 0:
        return 0
    return max(0, min(playoff_teams, 2 ** rounds - playoff_teams))


def _check_pool(
    pool: FieldPool,
    *,
    all_weeks: Sequence[int],
    roster: RosterStructure,
    variance: VarianceModel,
) -> None:
    """Reconcile a caller-supplied :class:`FieldPool` with what is being graded.

    THE FAILURE THIS EXISTS FOR. ``field_lineups`` returns exactly
    ``roster.teams - 1`` tuples whatever happens, so a pool that does not cover a
    week hands back nine ``(0.0, 0.0)`` rivals — a NON-EMPTY tuple, which sails
    straight past ``grade_roster``'s "no rivals" guard and reads as a certain
    win. Measured on the live board with the documented fast path
    (``build_field_pool(..., weeks=range(1, 15))`` + the default playoff weeks):
    win probability 1.000 in weeks 15, 16 and 17, opponent means 0.0, and title
    odds inflated 0.696 -> 1.000 with no warning anywhere. A pool built on a
    different ``RosterStructure`` is the same class of error one level up: it
    deals a different number of rivals from a differently-trimmed ladder
    (measured: expected wins 11.86 -> 12.19 in a 10-team league).
    """
    missing = sorted(set(all_weeks) - set(pool.weeks))
    if missing:
        raise GradeInputError(
            f"the field pool covers weeks {list(pool.weeks)} but this grade needs "
            f"{list(all_weeks)} — weeks {missing} are missing. Every rival would "
            "score 0.0 in them and you would 'win' each with certainty. Build the "
            "pool over regular_season_weeks + playoff_weeks."
        )
    if pool.roster != roster:
        raise GradeInputError(
            f"the field pool was built for a {pool.roster.teams}-team roster "
            f"structure and this grade is for a {roster.teams}-team one. The pool "
            "decides how many rivals are dealt and how deep each ladder goes, so "
            "the two must be the same structure."
        )
    if pool.variance is not None and pool.variance != variance:
        raise GradeInputError(
            "the field pool's sigma model is not the one this grade is using "
            f"({pool.variance.label!r} vs {variance.label!r}). Every rival's "
            "variance would come from one model and yours from another, which "
            "silently tilts every win probability."
        )


def grade_roster(
    entries: Sequence[BoardEntry],
    weekly: Mapping[str, Mapping[int, float]],
    *,
    roster: RosterStructure = DEFAULT_ROSTER,
    opponent_rosters: Mapping[int, Sequence[BoardEntry]] | None = None,
    variance: VarianceModel | None = None,
    regular_season_weeks: Iterable[int] = DEFAULT_REGULAR_SEASON_WEEKS,
    playoff_weeks: Iterable[int] = DEFAULT_PLAYOFF_WEEKS,
    playoff_teams: int = DEFAULT_PLAYOFF_TEAMS,
    # --- additive, all defaulted; the contract above is unchanged ---
    positions: Mapping[str, str] | None = None,
    field: FieldPool | None = None,
    objective_playoff_weight: float = 0.0,
    objective_title_weight: float = 0.0,
    stream_kdst: bool = True,
) -> SeasonGrade:
    """Grade a roster week by week and return the win-probability objective.

    ``entries`` is the drafted roster (``BoardEntry``, so positions and NFL teams
    come with it); ``weekly`` is :func:`weekly_points_map`'s output or any
    ``{player_id: {week: points}}`` on the SAME id space.

    OPPONENTS. ``opponent_rosters`` (the ``PickContext.opponent_rosters`` shape,
    ``team_slot -> roster``) is used when given: every rival is seated week by
    week exactly like you are, and each week's win probability is averaged over
    the rivals — the expectation over a uniformly random opponent, which is the
    right quantity while the head-to-head schedule is unknown. With
    ``opponent_rosters=None`` the rivals are the labelled snake-dealt field
    (:data:`FIELD_LABEL`) derived from the board itself. Positions for that come
    from ``weekly.positions`` when ``weekly`` is a :class:`WeeklyPointsMap`, from
    ``positions=`` otherwise, and if neither is available this RAISES rather than
    invent an opponent. ``opponent_rosters`` wins over ``field`` if both are given
    — real rivals beat a model of them.

    STREAMED SLOTS. ``stream_kdst`` (default True) prices an empty K or D/ST slot
    at that week's waiver-tier replacement rather than at zero — see
    :data:`STREAM_LABEL` and :func:`stream_levels`. It needs a positions map (a
    :class:`WeeklyPointsMap`, ``positions=``, or a ``field=`` pool); without one
    the grade says out loud that those slots were priced STRICTLY, because with
    the credit off a second kicker is the best add on any full roster.

    MID-DRAFT ROSTERS grade honestly rather than specially: an incomplete roster
    has empty slots, which are real holes, which cost real win probability. A
    rival roster that is still filling up is likewise weak, and the reasons say
    so — do not read a mid-draft grade as a season forecast.

    COST, re-measured on the live 2026 board 2026-08-30 (3,229 priced players,
    weeks 1-17, 200 calls): **0.83 ms** per call against a cached synthetic field,
    **2.9 ms** against nine real opponent rosters (which must be re-seated every
    week). The one-off ``FieldPool`` build is **5.3 ms**, so rebuilding it per
    call is a ~6x regression — build it once outside a rollout loop, or pass the
    :class:`WeeklyPointsMap` that :func:`weekly_points_map` returns, which
    memoises it for you.
    """
    var_model = DEFAULT_VARIANCE if variance is None else variance
    reg_weeks = tuple(sorted({int(w) for w in regular_season_weeks}))
    po_weeks = tuple(sorted({int(w) for w in playoff_weeks}))
    if not reg_weeks:
        raise GradeInputError(
            "regular_season_weeks is empty — there is no season to grade. Pass the "
            "league's H2H weeks (this league: weeks 1-14)."
        )
    overlap = set(reg_weeks) & set(po_weeks)
    if overlap:
        raise GradeInputError(
            f"weeks {sorted(overlap)} are listed as BOTH regular season and playoff "
            "weeks; a week is one or the other"
        )
    bad_weeks = sorted(w for w in reg_weeks + po_weeks if w < 1)
    if bad_weeks:
        # means[wk - 1] is a DENSE index: week 0 would write means[-1], silently
        # overwriting the last week of the season instead of failing.
        raise GradeInputError(
            f"weeks {bad_weeks} are not fantasy weeks — every week must be >= 1, "
            "because weekly_means/win_probs/opponent_means are DENSE from week 1 "
            "(index 7 is always week 8)."
        )
    all_weeks = tuple(sorted(set(reg_weeks) | set(po_weeks)))
    if not weekly:
        raise GradeInputError(
            "the weekly points map is EMPTY — there is nothing to grade. Every "
            "roster would seat an empty lineup, every rival would score 0.0, and "
            "the grade would come back as a plausible-looking 7.00 expected wins "
            "computed from zero rows. Check the as_of (projections are not knowable "
            "before the first pull), the season and the source."
        )
    if not 1 <= playoff_teams <= roster.teams:
        raise GradeInputError(
            f"playoff_teams={playoff_teams} is impossible in a {roster.teams}-team "
            "league; pass the league's real bracket size (this league: 6 of 10)."
        )

    # --- resolve the opponent model -------------------------------------
    # Precedence: opponent_rosters > field > a field dealt from positions.
    pool = field
    own_ids = frozenset(e.player_id for e in entries)
    pos_map = positions
    if pos_map is None:
        pos_map = getattr(weekly, "positions", None)
    # id()-keyed, which is only safe while the referenced objects are alive — so
    # each cache VALUE pins them. Without that, a temporary RosterStructure could
    # be collected and a new one reuse its id, serving a silently wrong field
    # (the same class of bug as a stale crosswalk).
    cache = getattr(weekly, "_field_cache", None)
    cache_key = (all_weeks, id(pos_map), id(roster), id(var_model))
    if pool is None and opponent_rosters is None:
        if pos_map is None:
            raise GradeInputError(
                "no opponent model available: pass opponent_rosters=, or field=, "
                "or positions= (player_id -> QB/RB/WR/TE/DST/K), or hand this "
                "function the WeeklyPointsMap that weekly_points_map() returns — "
                "it carries positions and caches the field for you. Refusing to "
                "invent an opponent."
            )
        hit = cache.get(cache_key) if cache is not None else None
        if hit is not None:
            pool = hit[-1]
        else:
            pool = build_field_pool(
                weekly, pos_map, weeks=all_weeks, roster=roster, variance=var_model
            )
            if cache is not None:
                cache[cache_key] = (pos_map, roster, var_model, pool)

    if pool is not None:
        _check_pool(pool, all_weeks=all_weeks, roster=roster, variance=var_model)
    if opponent_rosters is not None:
        expected = roster.teams - 1
        if len(opponent_rosters) != expected:
            raise GradeInputError(
                f"opponent_rosters holds {len(opponent_rosters)} rivals but a "
                f"{roster.teams}-team league has {expected}. The playoff model asks "
                f"'do at most {playoff_teams - 1} rivals finish above me?', which is "
                "CERTAIN with too few of them — a short mapping silently returned "
                f"'playoff odds 100% (top {playoff_teams} of {roster.teams})'. Pass "
                "every rival, or pass roster=RosterStructure(teams=N) if the league "
                "really is that size."
            )

    # The streamed-slot levels, and the disclosure when they are unavailable.
    # The cap and the credit are ONE model and travel together: capping a backup
    # D/ST without crediting the stream would price a K/DST bye as a dead week.
    streams = None
    if stream_kdst:
        if pool is not None:
            streams = pool.streams
        elif pos_map is not None:
            # No dealt field is needed here (real rivals), and building one for
            # its stream table alone costs 6x what the two ladders cost.
            hit = cache.get(("streams",) + cache_key) if cache is not None else None
            if hit is not None:
                streams = hit[-1]
            else:
                streams = stream_levels_from_board(
                    weekly, pos_map, weeks=all_weeks, roster=roster, variance=var_model
                )
                if cache is not None:
                    cache[("streams",) + cache_key] = (
                        pos_map, roster, var_model, streams
                    )
    stream_state = (
        "on" if streams else
        ("off" if not stream_kdst else "unavailable")
    )
    own_capped = (
        capped_out(entries, weekly, all_weeks, roster=roster) if streams else frozenset()
    )
    rival_capped = (
        {
            slot: capped_out(r, weekly, all_weeks, roster=roster)
            for slot, r in opponent_rosters.items()
        }
        if (streams and opponent_rosters is not None) else None
    )

    # --- week by week ----------------------------------------------------
    span = max(all_weeks)
    means = [0.0] * span
    opp_means = [0.0] * span
    wprobs = [0.0] * span
    lineups: dict[int, LineupFill] = {}
    holes: list[tuple[int, tuple[str, ...]]] = []
    streams_used: list[tuple[int, tuple[tuple[str, float], ...]]] = []
    reg_probs: list[float] = []
    rival_week_probs: list[list[float]] | None = None  # per rival, per REG week
    own_by_week: dict[int, tuple[float, float]] = {}
    rivals_by_week: dict[int, tuple[tuple[float, float], ...]] = {}

    for wk in all_weeks:
        fill, mu, var, _pts, streamed = _seat_week(
            entries, weekly, wk, roster, var_model, streams, own_capped
        )
        rivals = _opponent_week(
            wk,
            opponent_rosters=opponent_rosters,
            weekly=weekly,
            pool=pool,
            exclude=own_ids,
            roster=roster,
            variance=var_model,
            streams=streams,
            capped=rival_capped,
        )
        if not rivals:
            raise GradeInputError(
                f"week {wk}: the opponent model produced no rivals — cannot form a "
                "win probability. Check opponent_rosters or the board's depth."
            )
        probs = [win_probability(mu, o_mu, var, o_var) for o_mu, o_var in rivals]
        p_win = math.fsum(probs) / len(probs)

        lineups[wk] = fill
        own_by_week[wk] = (mu, var)
        rivals_by_week[wk] = rivals
        means[wk - 1] = mu
        opp_means[wk - 1] = math.fsum(m for m, _v in rivals) / len(rivals)
        wprobs[wk - 1] = p_win
        # A slot filled by the waiver stream is NOT a hole — it is a Tuesday
        # transaction. Everything else still is.
        streamed_labels = {label for label, _pts in streamed}
        if streamed:
            streams_used.append((wk, streamed))
        empty = tuple(s for s in fill.empty_slots if s not in streamed_labels)
        if empty:
            holes.append((wk, empty))
        if wk in reg_weeks:
            reg_probs.append(p_win)
            # Each rival's own week, for the playoff field: P(rival beats the
            # AVERAGE of the other nine teams, ours included). One Phi per rival
            # per week, not n^2 — an approximation, stated in PLAYOFF_LABEL.
            if rival_week_probs is None:
                rival_week_probs = [[] for _ in rivals]
            if len(rivals) != len(rival_week_probs):
                raise GradeInputError(
                    f"week {wk}: the opponent model produced {len(rivals)} rivals "
                    f"but {len(rival_week_probs)} in an earlier week — the field "
                    "must be the same size every week"
                )
            tot_mu = math.fsum(m for m, _v in rivals) + mu
            tot_var = math.fsum(v for _m, v in rivals) + var
            for j, (o_mu, o_var) in enumerate(rivals):
                bar_mu = (tot_mu - o_mu) / len(rivals)
                bar_var = (tot_var - o_var) / len(rivals)
                rival_week_probs[j].append(
                    win_probability(o_mu, bar_mu, o_var, bar_var)
                )

    expected_wins = math.fsum(reg_probs)

    # --- playoffs & title -------------------------------------------------
    own_moments = _win_total_moments(reg_probs)
    rival_moments = [_win_total_moments(ps) for ps in (rival_week_probs or []) if ps]
    if rival_moments:
        playoff_prob, bye_prob = _seed_probabilities(
            own_moments, rival_moments,
            playoff_teams=playoff_teams, bye_seeds=_bye_seeds(playoff_teams, len(po_weeks)),
        )
    else:
        playoff_prob, bye_prob = 0.0, 0.0

    rounds = len(po_weeks)
    if rounds:
        # The bracket is the strongest half of the field, so a playoff opponent
        # is drawn from the top of it, not its middle.
        p_playoff_week = _playoff_week_probability(
            po_weeks, own_by_week, rivals_by_week, playoff_teams=playoff_teams
        )
        # A top-`bye_seeds` seed needs one fewer win. bye_prob <= playoff_prob by
        # construction (a stricter DP prefix), so the second term is never negative.
        title_prob = (
            bye_prob * p_playoff_week ** max(0, rounds - 1)
            + max(0.0, playoff_prob - bye_prob) * p_playoff_week ** rounds
        )
    else:
        p_playoff_week = 0.0
        title_prob = 0.0

    objective = (
        expected_wins
        + objective_playoff_weight * playoff_prob
        + objective_title_weight * title_prob
    )

    reasons = _grade_reasons(
        entries=entries,
        weekly=weekly,
        reg_weeks=reg_weeks,
        po_weeks=po_weeks,
        means=means,
        wprobs=wprobs,
        opp_means=opp_means,
        holes=holes,
        expected_wins=expected_wins,
        playoff_prob=playoff_prob,
        bye_prob=bye_prob,
        title_prob=title_prob,
        p_playoff_week=p_playoff_week,
        playoff_teams=playoff_teams,
        roster=roster,
        variance=var_model,
        synthetic_field=opponent_rosters is None,
        opponent_rosters=opponent_rosters,
        objective=objective,
        objective_playoff_weight=objective_playoff_weight,
        objective_title_weight=objective_title_weight,
        streams_used=streams_used,
        stream_state=stream_state,
        capped=own_capped,
    )

    return SeasonGrade(
        objective=objective,
        expected_wins=expected_wins,
        playoff_prob=playoff_prob,
        title_prob=title_prob,
        weekly_means=tuple(means),
        hole_weeks=tuple(wk for wk, _slots in holes),
        reasons=reasons,
        win_probs=tuple(wprobs),
        hole_detail=tuple(holes),
        opponent_means=tuple(opp_means),
        playoff_bye_prob=bye_prob,
        regular_season_weeks=reg_weeks,
        playoff_weeks=po_weeks,
        lineups=MappingProxyType(lineups),
        streamed_slots=tuple(streams_used),
        stream_state=stream_state,
    )


def _playoff_week_probability(
    po_weeks: Sequence[int],
    own_by_week: Mapping[int, tuple[float, float]],
    rivals_by_week: Mapping[int, Sequence[tuple[float, float]]],
    *,
    playoff_teams: int,
) -> float:
    """Your mean P(win) in a playoff week against the BRACKET-strength field.

    The bracket holds the strongest ``playoff_teams`` of ten, so the rival you
    meet there is drawn from the top of the field, not its middle. Approximated
    by averaging your win probability against the strongest
    ``playoff_teams - 1`` rivals in each playoff week — the same rivals, already
    seated in the main pass, just ranked and truncated.
    """
    keep = max(1, playoff_teams - 1)
    probs: list[float] = []
    for wk in po_weeks:
        own = own_by_week.get(wk)
        rivals = rivals_by_week.get(wk)
        if own is None or not rivals:
            continue
        mu, var = own
        strongest = sorted(rivals, key=lambda r: -r[0])[:keep]
        probs.extend(win_probability(mu, o_mu, var, o_var) for o_mu, o_var in strongest)
    if not probs:
        return 0.0
    return math.fsum(probs) / len(probs)


# ========================================================================
#                       4.  reasons and display (Rule 6)
# ========================================================================


def _name_of(entry: BoardEntry) -> str:
    return entry.name or entry.player_id


def _grade_reasons(
    *,
    entries,
    weekly,
    reg_weeks,
    po_weeks,
    means,
    wprobs,
    opp_means,
    holes,
    expected_wins,
    playoff_prob,
    bye_prob,
    title_prob,
    p_playoff_week,
    playoff_teams,
    roster,
    variance,
    synthetic_field,
    opponent_rosters,
    objective,
    objective_playoff_weight,
    objective_title_weight,
    streams_used,
    stream_state,
    capped,
) -> tuple[str, ...]:
    """Plain-language reasons a football novice can act on (Rule 6).

    Order matters: the headline number, then anything WRONG with the roster
    (holes, bye collisions, players with no data), then the labelled modelling
    assumptions. A reader who stops after three lines still gets the problems.
    """
    out: list[str] = []
    span = f"weeks {reg_weeks[0]}-{reg_weeks[-1]}"
    out.append(
        f"expected wins {expected_wins:.2f} of {len(reg_weeks)} ({span}) — the sum of "
        f"your chance of winning each week, not a points total"
    )
    if objective_playoff_weight or objective_title_weight:
        out.append(
            f"objective {objective:.3f} = expected wins "
            f"{expected_wins:+.2f}, playoff odds x{objective_playoff_weight:g}, "
            f"title odds x{objective_title_weight:g} (caller-supplied weights)"
        )
    else:
        out.append("objective = expected wins (no playoff/title weighting applied)")
    out.append(
        f"playoff odds {playoff_prob * 100:.0f}% (top {playoff_teams} of "
        f"{roster.teams}); first-round bye {bye_prob * 100:.0f}%; "
        f"title {title_prob * 100:.1f}%"
    )

    # --- what is WRONG with this roster ---------------------------------
    if holes:
        for wk, slots in holes[:6]:
            who = _bye_culprits(entries, weekly, wk, slots)
            detail = f" — {who}" if who else ""
            out.append(
                f"week {wk}: NO ONE to start at {', '.join(slots)}; you score "
                f"{means[wk - 1]:.1f} and win {wprobs[wk - 1] * 100:.0f}% of the "
                f"time that week{detail}"
            )
        if len(holes) > 6:
            out.append(f"...and {len(holes) - 6} more weeks with an empty starting slot")
    else:
        out.append("every required starting slot is filled in every graded week")

    for line in _collision_reasons(entries, weekly, reg_weeks, roster):
        out.append(line)

    # --- the streamed slots (a filled slot that is NOT a hole) ------------
    if streams_used:
        shown = "; ".join(
            f"wk {wk} " + ", ".join(f"{slot} +{pts:.1f}" for slot, pts in slots)
            for wk, slots in streams_used[:6]
        )
        more = f" (+{len(streams_used) - 6} more weeks)" if len(streams_used) > 6 else ""
        out.append(
            f"STREAMED (not counted as holes): {shown}{more} — you would add a "
            "free-agent kicker/defense that week. " + STREAM_LABEL
        )
    if capped:
        named = ", ".join(
            sorted(_name_of(e) for e in entries if e.player_id in capped)
        )
        out.append(
            f"NOT COUNTED AT ALL: {named} — you may start one kicker and one "
            "defense, and this grade streams the bye week from waivers rather than "
            "from your bench, so a SECOND one is worth exactly nothing here (the "
            "same cap core/marginal.py applies in-season). That pick bought no "
            "expected wins"
        )
    if stream_state == "unavailable":
        out.append(
            "K/DST bye weeks are graded STRICTLY as empty slots: no positions map "
            "was available to price a waiver replacement from the board (pass the "
            "WeeklyPointsMap, positions=, or field=). That OVERSTATES the cost of a "
            "K/DST bye and makes a second kicker or defense look like the best "
            "available add — see " + STREAM_LABEL
        )
    elif stream_state == "off":
        out.append(
            "K/DST bye weeks are graded STRICTLY as empty slots (stream_kdst=False). "
            "That overstates the cost of a K/DST bye; see " + STREAM_LABEL
        )

    worst = min(reg_weeks, key=lambda w: wprobs[w - 1])
    best = max(reg_weeks, key=lambda w: wprobs[w - 1])
    out.append(
        f"weakest week {worst} ({means[worst - 1]:.1f} pts, win {wprobs[worst - 1] * 100:.0f}%); "
        f"strongest week {best} ({means[best - 1]:.1f} pts, win {wprobs[best - 1] * 100:.0f}%)"
    )

    # --- coverage: is a zero a bye, or a hole in the feed? ---------------
    span_len = len(reg_weeks) + len(po_weeks)
    no_data = [e for e in entries if not weekly.get(e.player_id)]
    thin = [
        e for e in entries
        if weekly.get(e.player_id) and len(weekly[e.player_id]) < span_len - 2
    ]
    if no_data:
        out.append(
            "NO PROJECTION AT ALL for "
            + ", ".join(f"{_name_of(e)} ({e.position})" for e in no_data[:5])
            + " — graded as unavailable every week, which is a data gap, not a "
              "verdict on the player"
        )
    if thin:
        out.append(
            "THIN COVERAGE (the feed forecasts only a handful of weeks) for "
            + ", ".join(
                f"{_name_of(e)} {len(weekly[e.player_id])}/{span_len} wk" for e in thin[:5]
            )
            + " — a missing week is not the same as a bye; treat these grades as "
              "uncertain, not low"
        )

    # --- labelled model assumptions --------------------------------------
    if synthetic_field:
        out.append(f"opponents: {FIELD_LABEL}")
        out.append(
            f"opponents, what that costs THIS number: the dealt field is stronger "
            f"than a real room, so read the {playoff_prob * 100:.0f}% playoff line as "
            "a FLOOR, not an estimate — measured on ten real drafted rosters, the "
            "same rosters graded +0.68 expected wins and +13.5 playoff points "
            "against their REAL rivals. Pass opponent_rosters= (mid-draft: "
            "PickContext.opponent_rosters) whenever you have them and this goes away"
        )
    else:
        n_opp = len(opponent_rosters or {})
        short = sum(
            1 for r in (opponent_rosters or {}).values() if len(r) < roster.starting_slots
        )
        note = (
            f"; {short} of them cannot field a full lineup yet, so this grade "
            "flatters you" if short else ""
        )
        out.append(
            f"opponents: your {n_opp} real rivals, each seated week by week the same "
            f"way you are; each week's number is the average across them{note}"
        )
    out.append(f"spread: {variance.label}; {variance.cohort}")
    # Two sub-priors ride INSIDE that fit and are not part of it. Both change the
    # answer (the stack test moves expected wins), so both are named here rather
    # than left under a cohort string that describes only the measured OLS part.
    out.append(
        f"spread, the two pieces that are NOT measured: kicker sigma is a flat "
        f"{variance.k_flat_sigma:.1f} house points (a hypothesis not yet fitted — "
        "weekly_stats gained field-goal make/distance/miss columns in migration 013, "
        "item 4.1, and a measured K sigma is a recorded follow-up); and a QB "
        "starting alongside a WR/TE from his OWN NFL team is "
        f"given correlation rho=+{variance.correlation_qb_passcatcher:.2f}, which "
        "RAISES that lineup's variance — an unmeasured 'correlated starts' "
        f"hypothesis, not part of the fit above ({variance.source})"
    )
    out.append(BENCH_LIMITATION_LABEL)
    out.append(f"playoffs: {PLAYOFF_LABEL}")
    out.append(
        f"title: {TITLE_LABEL} (your per-playoff-week win rate against that field "
        f"is {p_playoff_week * 100:.0f}%)"
    )
    return tuple(out)


def _bye_culprits(entries, weekly, week, slots) -> str:
    """Say WHY the slot went empty — and the two reasons are different.

    Either the players who could fill it are not playing that week (bye, or no
    forecast), or they are playing and project BELOW ZERO, in which case the
    seater deliberately leaves the slot empty because 0 beats a negative starter.
    A novice reading "NO ONE to start at DST" needs to know which.
    """
    wanted = {s.rstrip("0123456789") for s in slots}
    if "FLEX" in slots:
        wanted |= {"RB", "WR", "TE"}
    out_this_week, negative = [], []
    for e in entries:
        if e.position not in wanted:
            continue
        wpts = weekly.get(e.player_id, {})
        if week not in wpts:
            out_this_week.append(_name_of(e))
        elif wpts[week] < 0.0:
            negative.append(_name_of(e))
    parts = []
    if out_this_week:
        parts.append("out this week: " + ", ".join(sorted(out_this_week)[:4]))
    if negative:
        parts.append(
            "projected BELOW zero (an empty slot scores more): "
            + ", ".join(sorted(negative)[:4])
        )
    return "; ".join(parts)


def _collision_reasons(entries, weekly, reg_weeks, roster) -> list[str]:
    """Weeks where a position loses MORE starters than it can afford at once.

    This is the failure the season-total metric structurally cannot see: two
    starting RBs on the same bye is invisible to a season sum and decisive in
    week 8. Reported even when the FLEX papers over it, because "papered over by
    your WR4" is exactly how a roster ends up losing that week.
    """
    out: list[str] = []
    for pos, req in sorted(roster.starters.items()):
        if req < 1:
            continue
        held = [e for e in entries if e.position == pos]
        if not held:
            continue
        hit: list[tuple[int, list[str]]] = []
        for wk in reg_weeks:
            missing = [e for e in held if wk not in weekly.get(e.player_id, {})]
            # TWO OR MORE out at once is a collision. One player out of a
            # one-deep position is just a bye, and the hole line above already
            # says so — repeating it here as a "collision" trains the operator
            # to skim past the line that matters.
            if len(missing) >= 2 and len(held) - len(missing) < req:
                hit.append((wk, [_name_of(e) for e in missing]))
        for wk, who in hit[:3]:
            plural = "s" if len(held) != 1 else ""
            out.append(
                f"BYE COLLISION week {wk}: you hold {len(held)} {pos}{plural} and "
                f"start {req}, but {len(who)} are out at once "
                f"({', '.join(sorted(who)[:4])})"
            )
    return out


def format_season_grade(grade: SeasonGrade, *, reasons: bool = True) -> str:
    """Render a grade for a human (display only — Rule 3 keeps this out of the CLI
    layer's logic, and Rule 6 keeps the numbers explained)."""
    reg = grade.regular_season_weeks or tuple(range(1, len(grade.weekly_means) + 1))
    lines = [
        f"Objective (higher is better): {grade.objective:.3f}",
        f"  expected wins   : {grade.expected_wins:.2f} of {len(reg)}",
        f"  playoff odds    : {grade.playoff_prob * 100:.1f}%  "
        f"(first-round bye {grade.playoff_bye_prob * 100:.1f}%)",
        f"  title odds      : {grade.title_prob * 100:.1f}%",
        "",
        "Week-by-week projected points (and your chance of winning that week):",
    ]
    for wk in range(1, len(grade.weekly_means) + 1):
        mu = grade.weekly_means[wk - 1]
        p = grade.win_probs[wk - 1] if grade.win_probs else 0.0
        opp = grade.opponent_means[wk - 1] if grade.opponent_means else 0.0
        tag = "  <-- HOLE" if wk in grade.hole_weeks else ""
        kind = "PO" if wk in grade.playoff_weeks else "  "
        lines.append(
            f"  wk {wk:>2} {kind}  you {mu:6.1f}   opp {opp:6.1f}   win {p * 100:5.1f}%{tag}"
        )
    if grade.hole_weeks:
        lines.append("")
        lines.append("Weeks with an EMPTY required starting slot: " + ", ".join(
            f"{wk} ({'/'.join(slots)})" for wk, slots in grade.hole_detail
        ))
    if grade.streamed_slots:
        lines.append("")
        lines.append(
            "Weeks a K/DST slot is filled from WAIVERS (a Tuesday add, not a hole): "
            + ", ".join(
                f"wk {wk} ({'/'.join(f'{s} +{p:.1f}' for s, p in slots)})"
                for wk, slots in grade.streamed_slots
            )
        )
    if reasons:
        lines.append("")
        lines.append("Why:")
        lines.extend(f"  - {r}" for r in grade.reasons)
    return "\n".join(lines)
