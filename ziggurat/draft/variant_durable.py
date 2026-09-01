"""The "durable" draft variant — the engine, re-ranked for AVAILABILITY.

Import-quarantined package (Rule 8): lives under ``ziggurat/draft/``, nothing outside the
package imports it, and it is deleted with the rest of the draft tool. It imports
``ziggurat.core.availability`` and ``ziggurat.core.marginal`` (draft -> core is the
allowed direction) and it does not edit, monkey-patch or subclass a single
production file: :class:`DurablePicker` WRAPS a
:class:`~ziggurat.draft.engine.PickEngine` and re-ranks what it hands back, so
the default draft path is byte-identical while this module is unused.

WHAT IT FIXES, and why it is not a tuning knob
----------------------------------------------
The engine prices every player as if he plays all sixteen games. He does not.
Measured by ``core/availability.py`` against 2021-2025 presence-vs-schedule (the
injury REPORT misses 68-88% of the weeks a starter actually missed, so the report
is not the source), the expected games played on the top-200 of this board,
**under this module's shipped default** (:data:`QB_POLICY_CAP` — see hazard 1;
the cap is the reason the QB cell is not the raw measurement), are::

    DST 16.00   TE 13.62   K 12.92   WR 12.65   RB 12.61   QB 12.56

Under :data:`QB_POLICY_RAW` every cell is identical except QB, which reads
**11.87**. Both were re-measured on the live 2026-08-30 board on 2026-08-30 and
are pinned to +/-0.4 by the DB-guarded
``test_the_live_board_prices_the_measured_availability_spread``, which also
asserts that the two policies differ at QB and NOWHERE ELSE — an earlier draft of
this table printed the RAW QB number above a paragraph describing the CAPPED one,
under a test whose only band was 10.5 < mean < 15.0, and nothing caught it.

That is a 21% haircut on a QB (26% raw) and 0% on a D/ST, applied to nothing
today. Two consequences, and the second is the one that costs a season:

  1.  a durable player and a fragile one at the same projection are priced
      identically, and
  2.  the BACKUP behind a starter you already own is worth more than his own
      projection says, because he is the one player on the board guaranteed to be
      startable exactly when your starter is not.

THE TWO TERMS, in the engine's own units (VOR points)
------------------------------------------------------
:meth:`DurablePicker.recommend` calls ``engine.recommend(ctx, top=K)`` and adds::

    availability = -(1 - a_p) * max(vor_p, 0) * frac_p
    contingent   = + a_p * uplift(pos) * E[games his starter misses]   (if you own the starter)

``a_p`` is his expected AVAILABLE fraction of his club's games
(``expected_games_played / len(game_weeks)``); ``frac_p`` is the engine's own
lineup-reachability fraction (:func:`~ziggurat.draft.engine._value_fraction`),
reused rather than re-derived so the adjusted value is exactly
``frac * vor * a`` and the two files cannot drift. Only POSITIVE ``vor`` is
discounted — the engine's own rule that a discount must never make a player score
BETTER, and on this board most of the late rounds carry negative VOR, where
scaling would do exactly that.

``uplift`` is ``marginal.DEFAULT_HANDCUFFS`` — the measured within-pair event
study (QB +8.59, RB +4.43, TE +1.66 house pts per week the starter is out; WR is
DELIBERATELY absent at a measured -0.14, and D/ST and K are gated out because the
D/ST brackets are non-linear and adding points to them outside ``scoring.py``
would break Rule 2). The starter -> backup pairing is ``marginal.handcuff_links``,
the same depth kernel item 3.6's alert path uses. NOTHING in this module invents a
prior that already exists somewhere else, and no scoring constant is introduced
(Rule 2): every number below is either a documented, cited prior imported from
``core/``, or a weight the A/B sweeps.

HAZARD 1 — THE QB RATE IS CONTAMINATED, AND THIS MODULE DOES NOT USE IT RAW
---------------------------------------------------------------------------
``availability.py`` measures GAMES NOT PLAYED FOR ANY REASON, which is the right
quantity for fantasy (a benched QB scores you nothing either) and the wrong one
for a *durability* prior: it cannot separate "hurt" from "lost the job", and the
result is a QB miss rate (26.71%) ABOVE the RB's (22.26%), which no injury
measurement supports and which would push every quarterback down this board for a
reason that does not apply to the entrenched QB1s at the top of it.

The shipped policy is :data:`QB_POLICY_CAP`: cap the QB *position level*
(``season_miss_rate`` and ``opening_absent``) at the highest UNCONTAMINATED
position's measured value — the RB's, from the same cohort and the same method —
and change nothing else. The player-specific multiplier is untouched, because it
is relative to ``prior_history_rate`` and therefore survives a level cap intact:
the relative ordering of quarterbacks by their own record is kept, only the
population level moves. :data:`QB_POLICY_RAW` restores the measured rate so the
choice is MEASURABLE rather than asserted (see the A/B), and every QB row this
module produces carries :data:`QB_POLICY_NOTE` in its reasons saying which policy
priced it (Rule 6). The cap is a POLICY, not a measurement, and it is labelled as
one everywhere it appears.

HAZARD 2 — DOUBLE COUNTING WITH ``core/dispersion.py``
------------------------------------------------------
``core/dispersion.py`` deliberately EXCLUDES availability from its floor/ceiling
band and says so, precisely so a consumer does not price the same risk twice.
This module is the other half of that split and holds up its end:

  * it never imports, reads or re-derives a dispersion number — the engine's own
    ``b_risk * risk_sign * dispersion`` term passes through this wrapper
    completely untouched, inside ``rec.pick_score``;
  * the two are ORTHOGONAL by construction: dispersion is the spread of his
    points GIVEN THAT HE PLAYS, availability is the probability that he plays.
    Composing them means multiplying a distribution by a probability of
    existing, not adding two overlapping penalties.

:data:`COMPOSITION_NOTE` states that contract in one paragraph and ships in
:attr:`DurableInputs.reasons`, so an integrator stacking this variant on a
dispersion variant reads the rule rather than reconstructing it.
:func:`test_the_module_never_reads_a_dispersion_number` pins the first bullet.

HAZARD 3 — A PARTIAL SLATE PROMOTES EXACTLY THE PLAYERS IT LOST
----------------------------------------------------------------
This is a discount, so an unpriced row is not a neutral row: it is a row that
keeps 100% of its value while its neighbours lose 20%. A schedule pull short by a
few clubs therefore does not degrade this model toward the engine, it
systematically PROMOTES every player from the missing clubs — which is strictly
worse than not running the model at all. ``availability.py``'s own book floor
(``MIN_BOOK_CLUBS`` = 28) tolerates four missing clubs by design, so it cannot be
the guard here. :func:`build_durable_inputs` adds its own, in the
``SnapshotCollapse`` / ``BoardCollapse`` / ``CrosswalkCollapse`` family this repo
already pays for: any club that appears on a BOARD ROW and is absent from the
book's slate raises :class:`SlateGap`, naming the clubs and the rows. Live on the
2026-08-30 board that count is zero, so the floor costs nothing and fires only on
the failure. ``allow_missing_clubs=True`` is the deliberate fixture-only override
and is disclosed in :attr:`DurableInputs.reasons` when used.

The row-level disclosure was wrong in the same place: an unpriced row said "no
club on the row" whatever the cause, including for a player whose row carried a
club perfectly well. :data:`UNPRICED_CAUSES` keys the sentence off the recorded
cause instead (``NO_CLUB`` / ``CLUB_NOT_IN_SCHEDULE`` / ``PRICING_REFUSED``).

RULE 6 — THE REASONS ARE CARRIED, NOT RE-WRITTEN
-------------------------------------------------
The handcuff arm has always carried ``marginal.handcuff_links``'s hedges
verbatim. The availability arm now does the same: every ROW-SPECIFIC line of
``PlayerAvailability.reasons`` rides through into the recommendation unchanged —
the shrink weight, the seasons his record actually covers, a season he missed in
full, a CLIPPED multiplier, the role-floor sentence — and this module adds one
summary sentence of its own naming the decision (the points it cost him). Only
the three POPULATION-level lines (``describe`` / ``method_note`` / ``block_note``
— identical for every player at a position) are lifted out and shipped once in
:attr:`DurableInputs.reasons` instead of repeated on every candidate.

That split is by IDENTITY, not by prefix matching: the three population lines are
recomputed from the row's own prior and removed by equality, so a re-worded
``availability.py`` cannot leak them or silently drop a row-specific hedge. The
summary sentence quotes only fields the row carries (``expected_games_played``,
``sample_games``, ``sample_missed``, ``fallback_code``) and names no window,
sample or shrinkage of its own — an earlier version asserted "from his own
2021-2025 record" for every priced row, which was false for 38 of the 405 rows
priced from a record and for every career that began after 2021, and it collapsed
five distinct ``NO_ADJUSTMENT_*`` causes into "he has no usable record of his
own", which was a false statement about the player in three of them.

WHAT THIS WRAPPER STRUCTURALLY CANNOT DO
-----------------------------------------
It re-ranks the engine's candidate set; it cannot ADD to it. The engine gathers
the top ``candidate_width`` by ESPN rank plus the best-by-VOR at each allowed
position, so a handcuff sitting at ESPN rank 300 is invisible to this wrapper
however valuable the contingent term says he is. That is a property of the
wrap-and-re-rank pattern, not a bug here, and the measured reachability is
reported rather than assumed (see ``contingent_reachability`` in the tests and
the findings note). Widening the candidate set is ``engine.py``'s file.

DETERMINISM. Nothing here draws randomness at all: the pick path is arithmetic
over pre-loaded tables plus one delegated ``engine.recommend`` call, so
``ctx.rng`` is consumed EXACTLY as the unwrapped engine consumes it and a journal
replays bit-for-bit. The one sampler in this module
(:func:`sampled_weekly_maps`, an evaluation-only helper) takes an explicit seed
and derives a per-player stream from a string, so it is stable across processes.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from types import MappingProxyType

from ziggurat.core import availability as av
from ziggurat.core import marginal as mg
from ziggurat.core.valuation import DEFAULT_WEEKS, RosterStructure
from ziggurat.data.nfl import base
from ziggurat.draft import grader
from ziggurat.draft.bots import BoardEntry, PickContext, position_counts
from ziggurat.draft.engine import PickEngine, PickRec, _value_fraction

__all__ = [
    "COMPOSITION_NOTE",
    "DEFAULT_B_AVAILABILITY",
    "DEFAULT_B_CONTINGENT",
    "DEFAULT_TOP_K",
    "DurableInputs",
    "DurablePicker",
    "DurableVariantError",
    "QB_POLICY_CAP",
    "QB_POLICY_NOTE",
    "QB_POLICY_RAW",
    "SAMPLED_OBJECTIVE_NOTE",
    "SlateGap",
    "UNPRICED_CAUSES",
    "UNPRICED_CLUB_OFF_SLATE",
    "UNPRICED_NO_CLUB",
    "UNPRICED_REFUSED",
    "assert_adjustments_are_live",
    "build_durable_inputs",
    "expected_weekly_map",
    "qb_policy_prior",
    "sampled_weekly_maps",
]


class DurableVariantError(ValueError):
    """The variant was asked for something it cannot answer honestly."""


class SlateGap(DurableVariantError):
    """A club on the board is missing from the season's schedule (hazard 3).

    Raised BEFORE anything is priced. A discount that silently skips a club does
    not fail safe — it promotes every player from that club to the top of the
    board — so this is a collapse floor rather than a warning.
    """


# --------------------------------------------------------------------- policy

#: Cap the QB position LEVEL at the highest uncontaminated position's measured
#: rate. The shipped default — see the module docstring, hazard 1.
QB_POLICY_CAP = "cap"

#: Use ``availability.DEFAULT_DURABILITY``'s measured QB rate unchanged. Kept so
#: the policy is a measured choice rather than an assertion.
QB_POLICY_RAW = "raw"

QB_POLICIES = (QB_POLICY_CAP, QB_POLICY_RAW)

#: Positions whose measured "did not play" rate is NOT materially confounded by
#: benching. A quarterback benched for performance reads identically to an injured
#: one in a presence-vs-schedule measurement; a running back does not lose 3.5
#: games to a coach's decision. These are the positions the cap's reference level
#: is drawn from — the cap never invents a number, it borrows a measured one.
UNCONTAMINATED_POSITIONS = ("RB", "WR", "TE", "K")

QB_POLICY_NOTE = (
    "QB DURABILITY IS A CAPPED POLICY, NOT A MEASUREMENT. The 2021-2025 "
    "presence-vs-schedule measurement puts quarterbacks at a HIGHER miss rate "
    "than running backs, which is a measurement artefact: 'did not play' cannot "
    "tell a hurt starter from a benched one, and quarterbacks are the position "
    "that gets benched. This board therefore caps the quarterback position level "
    "at the running back's measured rate and leaves each player's own record "
    "doing its usual work on top of it. Treat a QB's availability discount here "
    "as a floor on his risk, not as a finding about him."
)

#: Why one board row carries no availability discount. Stored per row (hazard 3):
#: the three are a data-quality hole, a schedule hole and a pricing refusal, and
#: one sentence for all three told a diagnosing operator to look at the wrong
#: field. ``UNPRICED_NO_CLUB`` is the ordinary case on this board — the ESPN
#: universe union carries ~2,200 clubless rows so every pick is enterable.
UNPRICED_NO_CLUB = "NO_CLUB"
UNPRICED_CLUB_OFF_SLATE = "CLUB_NOT_IN_SCHEDULE"
UNPRICED_REFUSED = "PRICING_REFUSED"

UNPRICED_CAUSES = {
    UNPRICED_NO_CLUB: (
        "This board row carries no club at all (it is one of the ESPN-universe "
        "entries the board unions in so every pick is enterable), so there is no "
        "schedule to price him against and he is counted as if he plays every "
        "week — the one optimistic assumption on this list."
    ),
    UNPRICED_CLUB_OFF_SLATE: (
        "His CLUB is missing from the {season} schedule this board loaded, so his "
        "availability could not be priced and he is counted as if he plays every "
        "week. That is a hole in our schedule data, NOT a finding that he is "
        "durable — and it makes him look better than the players around him, not "
        "worse."
    ),
    UNPRICED_REFUSED: (
        "This board refused to price his availability (the durability book could "
        "not answer for him), so he is counted as if he plays every week — the "
        "one optimistic assumption on this list."
    ),
}

SAMPLED_OBJECTIVE_NOTE = (
    "THIS GRADE IS CONDITIONAL ON ONE DRAW OF SIMULATED SEASONS. The objective "
    "averaged over the sampled worlds built by sampled_weekly_maps(seed=...): "
    "both arms of a paired comparison see the IDENTICAL worlds, so the paired "
    "confidence interval measures draft-to-draft noise and NOT the choice of "
    "world draw. Re-drawing the worlds moves the point estimate by more than the "
    "interval's own width at 8 samples. Record the seed, and report the spread "
    "across several draws alongside the interval or the interval is quoted too "
    "narrow."
)

COMPOSITION_NOTE = (
    "COMPOSING THIS WITH A FLOOR/CEILING (DISPERSION) VARIANT IS SAFE, AND HERE "
    "IS THE RULE. core/dispersion.py measures how much a player's points move "
    "GIVEN THAT HE PLAYS, and says in its own docstring that it excludes "
    "availability on purpose. This module supplies the other factor: the "
    "probability that he plays at all. They multiply, they do not overlap, and "
    "neither one may be applied twice. Concretely: this wrapper never reads a "
    "dispersion number, and the engine's own risk term (b_risk x risk_sign x "
    "dispersion) rides through it untouched inside pick_score. A variant that "
    "ALSO discounts value for injury risk must not be stacked on this one."
)


def qb_policy_prior(
    prior: av.DurabilityPrior = av.DEFAULT_DURABILITY, *, policy: str = QB_POLICY_CAP
) -> av.DurabilityPrior:
    """The durability prior under ``policy`` (module docstring, hazard 1).

    ``raw`` returns ``prior`` unchanged. ``cap`` returns a copy whose QB
    ``season_miss_rate`` and ``opening_absent`` are capped at the largest value
    among :data:`UNCONTAMINATED_POSITIONS` — a measured number from the same
    cohort, never an invented one — and whose ``label`` says so, so that every
    reason line the prior renders (``describe``) discloses the policy without the
    caller having to remember to.

    ``prior_history_rate`` is deliberately NOT capped: the player multiplier is
    ``shrunk_own_rate / prior_history_rate``, so capping the level alone keeps the
    relative ordering of quarterbacks by their own records exactly as measured
    while moving only the population level.
    """
    if policy not in QB_POLICIES:
        raise DurableVariantError(
            f"unknown QB policy {policy!r}; expected one of {QB_POLICIES}"
        )
    if policy == QB_POLICY_RAW:
        return prior
    if "QB" not in prior.season_miss_rate:
        return prior
    refs = [prior.season_miss_rate[p] for p in UNCONTAMINATED_POSITIONS
            if p in prior.season_miss_rate]
    if not refs:
        raise DurableVariantError(
            "the QB cap needs at least one uncontaminated position "
            f"({', '.join(UNCONTAMINATED_POSITIONS)}) in the prior; this prior "
            f"measures only {', '.join(prior.positions())}. Refusing to invent a "
            "reference level."
        )
    ceiling = max(refs)
    ceiling_pos = max(
        (p for p in UNCONTAMINATED_POSITIONS if p in prior.season_miss_rate),
        key=lambda p: prior.season_miss_rate[p],
    )
    if prior.season_miss_rate["QB"] <= ceiling:
        return prior
    open_refs = [prior.opening_absent[p] for p in UNCONTAMINATED_POSITIONS
                 if p in prior.opening_absent]
    open_ceiling = max(open_refs) if open_refs else prior.opening_absent.get("QB", 0.0)
    rates = dict(prior.season_miss_rate)
    rates["QB"] = ceiling
    opens = dict(prior.opening_absent)
    if "QB" in opens:
        opens["QB"] = min(opens["QB"], open_ceiling)
    return dc_replace(
        prior,
        season_miss_rate=MappingProxyType(rates),
        opening_absent=MappingProxyType(opens),
        label=(
            f"{prior.label}; variant_durable QB-contamination policy "
            f"'{QB_POLICY_CAP}': the QB per-game rate is capped at "
            f"{100 * ceiling:.2f}% (the {ceiling_pos} rate, the highest measured "
            f"among positions benching does not confound) and the QB opener rate "
            f"at {100 * open_ceiling:.2f}%. QB only, and a POLICY rather than a "
            f"measurement"
        ),
    )


# ------------------------------------------------------------------- weights

#: How many engine recommendations to re-rank. The engine's candidate set is the
#: top ``candidate_width`` (5) by ESPN rank UNIONED with the best-by-VOR at each
#: allowed position, so it is at most ~11 wide; 12 therefore re-ranks the WHOLE
#: candidate set in practice and never silently truncates it. MEASURED on the
#: live 2026 board over 8 full drafts at seat 9 (128 operator picks,
#: rollouts=512): the engine offered 7.65 candidates per pick, never more
#: than 11.
DEFAULT_TOP_K = 12

#: Multiplier on the availability discount. 1.0 means "count exactly the fraction
#: of the season he is expected to be there for" — the honest value, not a tuned
#: one. 0.0 turns the term off (the ablation arm of the A/B).
DEFAULT_B_AVAILABILITY = 1.0

#: Multiplier on the contingent (handcuff) bonus, same convention.
DEFAULT_B_CONTINGENT = 1.0


# -------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class DurableInputs:
    """Everything :class:`DurablePicker` needs, loaded ONCE off the database.

    Held rather than re-read because the load is seconds (five seasons of
    presence history plus the projection spine) and the pick path must stay
    inside the 243 ms recommend() budget. After construction this object is pure
    data and the picker touches no connection at all.

    ``availability`` and the handcuff maps are keyed in the BOARD's id space
    (``BoardEntry.player_id``), so the picker never has to translate.
    """

    #: board player_id -> his priced availability row.
    availability: Mapping[str, av.PlayerAvailability]
    #: backup board player_id -> the board player_id of the starter he backs up.
    starter_of: Mapping[str, str]
    #: backup board player_id -> the measured within-pair uplift, house pts/week.
    uplift_of: Mapping[str, float]
    #: backup board player_id -> the plain-language handcuff reasons, VERBATIM
    #: from ``marginal.handcuff_links`` (its hedges are the ones that apply).
    handcuff_reasons: Mapping[str, tuple[str, ...]]
    prior: av.DurabilityPrior
    qb_policy: str
    as_of: str
    season: int
    weeks: tuple[int, ...]
    #: Disclosure the operator/integrator reads before trusting any of it (Rule 6).
    reasons: tuple[str, ...]
    #: How many board rows were priced, and how many of those leaned on the
    #: player's own record rather than the position prior.
    priced: int = 0
    from_player_history: int = 0
    board_rows: int = 0
    #: board player_id -> an ``UNPRICED_*`` cause, for every row that carries NO
    #: availability discount. The row-level reason is keyed off this rather than
    #: guessed from the shape of the row (hazard 3).
    unpriced: Mapping[str, str] = MappingProxyType({})
    #: Clubs that appear on a board row and are absent from the book's slate.
    #: Non-empty ONLY when the caller passed ``allow_missing_clubs=True``;
    #: :class:`SlateGap` is raised otherwise.
    missing_clubs: tuple[str, ...] = ()
    #: How many clubs the book's slate carried (32 on a healthy 2026 pull).
    slate_clubs: int = 0

    def cause(self, player_id: str) -> str | None:
        """Why he carries no discount, as an ``UNPRICED_*`` code, or ``None``.

        ``None`` means he WAS priced. A caller must not read a missing key as
        "priced": ask :meth:`fraction` for that.
        """
        return self.unpriced.get(player_id)

    def fraction(self, player_id: str) -> float | None:
        """His expected AVAILABLE share of his club's games, or ``None``.

        ``None`` is "this board never priced him" and is deliberately not 1.0:
        a missing row must not read as a durable player.
        """
        row = self.availability.get(player_id)
        if row is None or not row.game_weeks:
            return None
        return row.expected_games_played / len(row.game_weeks)


def _board_gsis(board: Sequence[BoardEntry], gsis_by_espn: Mapping[str, str]) -> dict[str, str]:
    """board player_id -> gsis id, where one is resolvable.

    ``load_board`` keys on ``espn_id or gsis_id or <fallback>``, so a key is
    either an ESPN id (resolve through the crosswalk), already a gsis id (the
    nflverse ``00-00xxxxx`` form), or a synthetic fallback with no identity at
    all. The third case is left OUT of the map rather than guessed, which is what
    lets ``availability`` say "never looked up" instead of "no record".
    """
    out: dict[str, str] = {}
    for e in board:
        pid = e.player_id
        g = gsis_by_espn.get(pid)
        if g is None and pid.startswith("00-"):
            g = pid
        if g is not None:
            out[pid] = g
    return out


def build_durable_inputs(
    conn,
    *,
    as_of,
    season: int,
    board: Sequence[BoardEntry],
    weeks: Sequence[int] | None = None,
    source: str = "sleeper_rotowire",
    qb_policy: str = QB_POLICY_CAP,
    handcuffs: mg.HandcuffModel = mg.DEFAULT_HANDCUFFS,
    view: base.AsOfView = "historical",
    prior: av.DurabilityPrior = av.DEFAULT_DURABILITY,
    allow_missing_clubs: bool = False,
) -> DurableInputs:
    """Load the availability book and the handcuff links, keyed to ``board``.

    Rule 1: ``as_of`` is keyword-only with no default and is threaded, together
    with ``view``, into BOTH reads —
    :func:`~ziggurat.core.availability.load_durability_book` (whose collapse
    floors refuse an empty book rather than pricing the whole board at 1.00x) and
    :func:`~ziggurat.core.marginal.handcuff_links`. This layer never widens the
    gate and never substitutes ``latest_truth`` on its own account.

    ``weeks`` defaults to ``valuation.DEFAULT_WEEKS`` (1-17) and is passed to
    ``handcuff_links`` EXPLICITLY: its own default resolves the current in-season
    week and raises preseason, which is every moment this module is used.

    The identity crosswalk is ``base.espn_by_gsis`` — the same read
    ``valuation.py`` uses, inverted. It is crosswalk-at-now by design (see that
    function's docstring); a backtest at a past ``as_of`` gets today's identities.

    HAZARD 3's floor lives here: a club that appears on a board row and is absent
    from the book's slate raises :class:`SlateGap` before anything is priced,
    because skipping it silently PROMOTES that club's players (they keep 100% of
    their value while everyone else is discounted). ``allow_missing_clubs=True``
    is the deliberate override for a fixture or a hand-built book; it is recorded
    in :attr:`DurableInputs.missing_clubs` and disclosed in ``.reasons``.
    """
    if qb_policy not in QB_POLICIES:
        raise DurableVariantError(
            f"unknown QB policy {qb_policy!r}; expected one of {QB_POLICIES}"
        )
    if not board:
        raise DurableVariantError(
            "empty board — every candidate would be priced with no availability "
            "row and the whole variant would silently degrade to the unwrapped "
            "engine. Refusing."
        )
    wks = tuple(int(w) for w in (DEFAULT_WEEKS if weeks is None else weeks))
    effective_prior = qb_policy_prior(prior, policy=qb_policy)
    book = av.load_durability_book(
        conn, as_of=as_of, season=int(season), prior=effective_prior, view=view
    )
    gsis_by_espn: dict[str, str] = {}
    for gsis, espn in base.espn_by_gsis(conn).items():
        gsis_by_espn.setdefault(str(espn), gsis)
    board_gsis = _board_gsis(board, gsis_by_espn)

    # HAZARD 3, checked BEFORE a single row is priced: a club on the board that
    # the slate has never heard of is a schedule hole, and skipping it quietly
    # promotes every player who plays for it.
    board_clubs = {e.team for e in board if e.team is not None}
    missing_clubs = tuple(sorted(c for c in board_clubs if c not in book.slate))
    if missing_clubs and not allow_missing_clubs:
        affected = sum(1 for e in board if e.team in set(missing_clubs))
        raise SlateGap(
            f"{len(missing_clubs)} club(s) on this board are missing from the "
            f"{season} schedule the durability book loaded at as_of={book.as_of} "
            f"({len(book.slate)} clubs in the slate): "
            f"{', '.join(missing_clubs)}. That would leave {affected} board rows "
            f"with NO availability discount while every other row keeps one, "
            f"which does not degrade this model toward the engine — it PROMOTES "
            f"exactly the players whose data went missing. Re-pull the schedule "
            f"(`ziggurat ingest run --source schedules`), or pass "
            f"allow_missing_clubs=True if this is a deliberately partial fixture."
        )

    rows: dict[str, av.PlayerAvailability] = {}
    unpriced: dict[str, str] = {}
    own_record = 0
    for e in board:
        # The unpriced ESPN-universe union carries no club; it is also never
        # recommended (load_board floors it below every priced row), so there
        # is nothing to discount. Left OUT of the availability map so
        # `fraction()` returns None rather than a fabricated 1.0 — and the CAUSE
        # is recorded, because "no club on the row" and "his club is missing
        # from the schedule" are different facts and only one of them is about
        # him (hazard 3).
        if e.team is None:
            unpriced[e.player_id] = UNPRICED_NO_CLUB
            continue
        if e.team not in book.slate:
            unpriced[e.player_id] = UNPRICED_CLUB_OFF_SLATE
            continue
        try:
            row = book.availability_for(
                e.player_id, e.position, e.team, wks,
                gsis_id=board_gsis.get(e.player_id),
            )
        except av.DurabilityError:
            unpriced[e.player_id] = UNPRICED_REFUSED
            continue
        rows[e.player_id] = row
        if row.basis == av.BASIS_PLAYER:
            own_record += 1

    on_board = {e.player_id for e in board}
    starter_of: dict[str, str] = {}
    uplift_of: dict[str, float] = {}
    hc_reasons: dict[str, tuple[str, ...]] = {}
    links = mg.handcuff_links(
        conn, as_of=as_of, season=int(season), weeks=list(wks),
        source=source, handcuffs=handcuffs, view=view,
    )
    for link in links:
        if link.uplift <= 0.0:
            continue
        starter_key = link.starter_espn_id or link.starter_gsis_id
        backup_key = link.backup_espn_id or link.backup_gsis_id
        if starter_key is None or backup_key is None:
            continue
        if starter_key not in on_board or backup_key not in on_board:
            continue
        starter_of[str(backup_key)] = str(starter_key)
        uplift_of[str(backup_key)] = float(link.uplift)
        hc_reasons[str(backup_key)] = tuple(link.reasons)

    reasons = _inputs_reasons(
        book=book, priced=len(rows), own_record=own_record, board_rows=len(board),
        links=len(starter_of), qb_policy=qb_policy, handcuffs=handcuffs, weeks=wks,
        unpriced=unpriced, missing_clubs=missing_clubs,
    )
    return DurableInputs(
        availability=MappingProxyType(rows),
        starter_of=MappingProxyType(starter_of),
        uplift_of=MappingProxyType(uplift_of),
        handcuff_reasons=MappingProxyType(hc_reasons),
        prior=effective_prior,
        qb_policy=qb_policy,
        as_of=book.as_of,
        season=int(season),
        weeks=wks,
        reasons=reasons,
        priced=len(rows),
        from_player_history=own_record,
        board_rows=len(board),
        unpriced=MappingProxyType(unpriced),
        missing_clubs=missing_clubs,
        slate_clubs=len(book.slate),
    )


def _inputs_reasons(
    *, book, priced: int, own_record: int, board_rows: int, links: int,
    qb_policy: str, handcuffs: mg.HandcuffModel, weeks: Sequence[int],
    unpriced: Mapping[str, str], missing_clubs: Sequence[str],
) -> tuple[str, ...]:
    """What an operator must know before reading a single recommendation (Rule 6).

    This is where the POPULATION-level disclosure lives — one copy of each
    position's ``describe`` / ``method_note`` and of the block note, rather than
    the same paragraph repeated on every candidate. Everything ROW-specific rides
    on the row itself (see :func:`_availability_reasons`).
    """
    share = 100.0 * own_record / priced if priced else 0.0
    window = book.history_window
    span = f"{window[0]}-{window[1]}" if window else "the measured window"
    no_club = sum(1 for c in unpriced.values() if c == UNPRICED_NO_CLUB)
    off_slate = sum(1 for c in unpriced.values() if c == UNPRICED_CLUB_OFF_SLATE)
    refused = sum(1 for c in unpriced.values() if c == UNPRICED_REFUSED)
    out = [
        f"Availability priced for {priced} of {board_rows} board rows over weeks "
        f"{weeks[0]}-{weeks[-1]}; {own_record} of those ({share:.0f}%) lean on the "
        f"player's own record inside {span} and the rest fall back to the position "
        f"prior, which is a fact about our history coverage and not about them. "
        f"Each row says WHICH of those two priced it, and why, in its own words.",
        (
            f"The {len(unpriced)} rows with NO discount break down as: {no_club} "
            f"with no club at all (the ESPN-universe entries the board unions in "
            f"so every pick is enterable — never recommended), {off_slate} whose "
            f"club is missing from the {book.season} schedule, and {refused} the "
            f"durability book refused to price. An unpriced row keeps 100% of its "
            f"value while the rows around it lose theirs, so the second and third "
            f"of those are PROMOTIONS, not neutral gaps."
        ),
    ]
    if missing_clubs:
        out.append(
            f"SLATE GAP ACCEPTED BY THE CALLER (allow_missing_clubs=True): "
            f"{', '.join(missing_clubs)} appear on this board and not in the "
            f"{book.season} schedule, so every player from those clubs is priced "
            f"as perfectly durable. Do not read this board's ordering as a "
            f"durability finding about them."
        )
    for pos in book.prior.positions():
        out.append(book.prior.describe(pos))
        out.append(book.prior.method_note(pos))
    for pos in sorted(book.prior.never_absent):
        out.append(book.prior.describe(pos))
    out += [
        book.prior.block_note(),
        (
            f"Handcuff insurance is priced for {links} starter/backup pairs that "
            f"both sit on this board, from {handcuffs.label} ({handcuffs.source}). "
            f"Only QB, RB and TE carry an uplift: the measured WR value is -0.14 "
            f"pts/wk and D/ST and K are gated out because their scoring is "
            f"non-linear."
        ),
        (
            "The starter/backup PAIRING comes from this board's own projected "
            "depth order, so it inherits that order's mistakes; naming one "
            "handcuff is right about 54% of the time."
        ),
        COMPOSITION_NOTE,
    ]
    if qb_policy == QB_POLICY_CAP:
        out.append(QB_POLICY_NOTE)
    else:
        out.append(
            "QB policy 'raw': quarterbacks are priced at the MEASURED "
            "presence-vs-schedule rate, which is contaminated by benching and "
            "sits above the running back rate. This is the deliberately "
            "un-corrected arm."
        )
    return tuple(out)


# ------------------------------------------------------------- one adjustment


@dataclass(frozen=True)
class DurableAdjustment:
    """The delta this variant applied to one engine recommendation, decomposed.

    Every field is in the engine's own VOR-point units, and
    ``availability + contingent == delta`` exactly, so a reader can check the
    arithmetic against the reasons rather than trust it.
    """

    player_id: str
    position: str
    availability: float            # <= 0
    contingent: float              # >= 0
    delta: float
    fraction: float | None         # expected available share of his club's games
    frac_lineup: float             # the engine's lineup-reachability fraction
    starter_id: str | None         # the owned starter this backs up, if any
    reasons: tuple[str, ...]

    @property
    def priced(self) -> bool:
        return self.fraction is not None


# --------------------------------------------------------------- the picker


@dataclass(frozen=True)
class DurablePicker:
    """A :class:`~ziggurat.draft.bots.Picker` that re-ranks the engine for durability.

    ``pick(ctx)`` returns ``recommend(ctx, top=1)[0].player_id`` and consumes
    ``ctx.rng`` exactly as the wrapped engine does (this module draws no
    randomness of its own), so the cockpit's bit-for-bit journal replay survives.

    ``recommend`` returns :class:`~ziggurat.draft.engine.PickRec` objects — the
    2.4 TUI's render contract, unchanged — with ``pick_score`` moved by the
    adjustment, the reasons EXTENDED (never rewritten: the engine's own sentences
    survive verbatim), and ``alternatives`` rebuilt from the NEW order, because an
    alternatives list that still describes the engine's ranking is a lie the
    operator cannot detect.
    """

    engine: PickEngine
    inputs: DurableInputs
    top_k: int = DEFAULT_TOP_K
    b_availability: float = DEFAULT_B_AVAILABILITY
    b_contingent: float = DEFAULT_B_CONTINGENT

    # -- Picker seam -------------------------------------------------------

    def pick(self, ctx: PickContext) -> str:
        return self.recommend(ctx, top=1)[0].player_id

    def recommend(self, ctx: PickContext, *, top: int = 5) -> tuple[PickRec, ...]:
        scored = self._scored(ctx)
        n = max(1, top)
        chosen = scored[:n]
        out: list[PickRec] = []
        for i, (_score, rec, adj) in enumerate(chosen):
            alts = tuple(
                (alt.name or alt.player_id, _why_not(alt))
                for _s, alt, _a in scored[i + 1 : i + 4]
            )
            out.append(
                dc_replace(
                    rec,
                    pick_score=rec.pick_score + adj.delta,
                    reasons=rec.reasons + adj.reasons,
                    alternatives=alts,
                )
            )
        return tuple(out)

    # -- diagnostics -------------------------------------------------------

    def adjustments(self, ctx: PickContext) -> tuple[DurableAdjustment, ...]:
        """The per-candidate adjustment, in the re-ranked order. Test/audit surface.

        NOTE this runs a full ``engine.recommend`` and therefore consumes
        ``ctx.rng``; call it on a context you are not also picking from.
        """
        return tuple(adj for _score, _rec, adj in self._scored(ctx))

    # -- internals ---------------------------------------------------------

    def _scored(
        self, ctx: PickContext
    ) -> list[tuple[float, PickRec, DurableAdjustment]]:
        recs = self.engine.recommend(ctx, top=max(self.top_k, 1))
        counts = position_counts(ctx.own_roster)
        own_ids = frozenset(e.player_id for e in ctx.own_roster)
        rows: list[tuple[float, PickRec, DurableAdjustment]] = []
        for rec in recs:
            adj = self._adjust(rec, counts=counts, own_ids=own_ids, roster=ctx.roster)
            rows.append((rec.pick_score + adj.delta, rec, adj))
        # The engine's own total order, with the adjusted score in front of it:
        # higher score, then higher vor, then shallower ESPN rank, then id. No
        # wall clock, no dict order (D2).
        rows.sort(
            key=lambda t: (-t[0], -t[1].vor, t[1].player.espn_overall_rank, t[1].player_id)
        )
        return rows

    def _adjust(
        self,
        rec: PickRec,
        *,
        counts: Mapping[str, int],
        own_ids: frozenset[str],
        roster: RosterStructure,
    ) -> DurableAdjustment:
        pos = rec.position
        pid = rec.player_id
        frac = _value_fraction(pos, counts, roster)
        share = self.inputs.fraction(pid)
        row = self.inputs.availability.get(pid)
        reasons: list[str] = []

        avail_delta = 0.0
        if share is None:
            if rec.vor > 0.0:
                # Rule 6: an undiscounted positive-value player is a silent
                # assumption of perfect durability. Say it out loud, and say
                # WHICH of the three causes it was — an earlier version told a
                # player whose row carried a club that he had no club (hazard 3).
                reasons.append(_unpriced_reason(self.inputs, rec))
        else:
            if rec.vor > 0.0:
                avail_delta = -self.b_availability * (1.0 - share) * rec.vor * frac
            reasons.extend(_availability_reasons(rec, row, share, avail_delta, frac))
            if pos == "QB":
                reasons.append(QB_POLICY_NOTE if self.inputs.qb_policy == QB_POLICY_CAP
                               else _RAW_QB_NOTE)

        contingent = 0.0
        starter_id = self.inputs.starter_of.get(pid)
        if starter_id is not None and starter_id in own_ids:
            uplift = self.inputs.uplift_of.get(pid, 0.0)
            starter_row = self.inputs.availability.get(starter_id)
            if uplift <= 0.0 or starter_row is None:
                # He IS the named handcuff and we could not price the insurance.
                # Saying nothing here would present a priced-at-zero bonus as an
                # absence of one (Rule 6).
                reasons.append(
                    "He is the named backup to a player on your roster, but this "
                    "board could not price that insurance (no durability record "
                    "for the starter, or no measured uplift at this position), so "
                    "no credit was given for it."
                )
            else:
                missed = starter_row.expected_games_missed
                backup_share = share if share is not None else 1.0
                contingent = self.b_contingent * uplift * missed * backup_share
                if contingent > 0.0:
                    reasons.append(
                        f"HANDCUFF: he is the backup to a player already on your "
                        f"roster, who is expected to miss {missed:.1f} games this "
                        f"season. Weeks your starter is out are exactly the weeks "
                        f"this man plays, which is worth about "
                        f"{contingent:+.0f} points here."
                    )
                    reasons.extend(self.inputs.handcuff_reasons.get(pid, ()))
        else:
            starter_id = None

        delta = avail_delta + contingent
        return DurableAdjustment(
            player_id=pid,
            position=pos,
            availability=avail_delta,
            contingent=contingent,
            delta=delta,
            fraction=share,
            frac_lineup=frac,
            starter_id=starter_id,
            reasons=tuple(reasons),
        )


_RAW_QB_NOTE = (
    "This board is running the UN-corrected quarterback durability rate, which "
    "counts benchings as missed time and therefore overstates a starter's injury "
    "risk. It is the deliberately raw arm of the comparison."
)


def _unpriced_reason(inputs: DurableInputs, rec: PickRec) -> str:
    """Why this row carries NO discount, keyed off the recorded cause (hazard 3).

    The cause is what ``build_durable_inputs`` recorded while skipping him, not a
    guess reconstructed from the shape of the row. A hand-built ``DurableInputs``
    (a test, or an integrator's fixture) records nothing, so the fallback names
    the club when the row has one rather than asserting it does not.
    """
    cause = inputs.cause(rec.player_id)
    if cause is None:
        cause = UNPRICED_NO_CLUB if rec.player.team is None else UNPRICED_REFUSED
    text = UNPRICED_CAUSES.get(cause, UNPRICED_CAUSES[UNPRICED_REFUSED])
    return text.format(season=inputs.season)


#: The short, code-aware clause naming WHERE a row's number came from. The long
#: version rides on the row itself and is carried verbatim (see ``_carried``);
#: this is the half-line that goes inside the summary sentence. Keyed on
#: ``availability``'s own ``NO_ADJUSTMENT_*`` codes so a new code degrades to the
#: neutral wording instead of to a false statement about the player.
_BASIS_CLAUSES: Mapping[str, str] = MappingProxyType({
    av.NO_ADJUSTMENT_NO_ID: (
        "from the {pos} position prior — we never matched him to a player id, so "
        "his own record was NEVER LOOKED UP (a gap in our crosswalk, not a "
        "finding about him)"
    ),
    av.NO_ADJUSTMENT_NO_RECORD: (
        "from the {pos} position prior — we looked him up and found no season in "
        "the measured window in which he played or was proven to be on a roster"
    ),
    av.NO_ADJUSTMENT_THIN: (
        "from the {pos} position prior — his {games}-game record is shorter than "
        "the floor this adjustment needs"
    ),
    av.NO_ADJUSTMENT_ROLE: (
        "from the {pos} position prior — he HAS a record ({games} club games), "
        "but too few weeks as a starter for it to measure durability rather than "
        "his role (the next line gives the count)"
    ),
    av.NO_ADJUSTMENT_NO_MEAN: (
        "from the {pos} position prior — the prior itself carries no history mean "
        "for this position, so no personal adjustment can be formed"
    ),
})


def _basis_clause(rec: PickRec, row: av.PlayerAvailability) -> str:
    """Name the basis truthfully, in the row's own terms (Rule 6).

    NEVER a window literal and never a sample size this module re-derived: the
    quantities here (``sample_games``, ``sample_missed``, ``fallback_code``,
    ``basis``) are fields the row carries, and everything richer — the seasons,
    the shrink weight, the clip — is carried verbatim from the row's reasons.
    """
    pos = rec.position
    if row.basis == av.BASIS_ALWAYS:
        return (
            f"and a {pos} is a team unit rather than a player, so this board "
            f"never discounts it for availability"
        )
    if row.basis == av.BASIS_PLAYER:
        return (
            f"from HIS OWN record — {row.sample_missed} of {row.sample_games} "
            f"club games missed — shrunk toward the {pos} prior (the next line "
            f"gives the seasons and how much weight his record actually carries)"
        )
    clause = _BASIS_CLAUSES.get(row.fallback_code or "")
    if clause is None:
        return f"from the {pos} position prior"
    return clause.format(pos=pos, games=row.sample_games)


def _carried(row: av.PlayerAvailability) -> tuple[str, ...]:
    """The ROW-SPECIFIC half of ``row.reasons``, verbatim.

    The three POPULATION-level lines (the position prior's ``describe``, its
    ``method_note`` and the block note) are identical for every player at a
    position and ship ONCE in :attr:`DurableInputs.reasons`; everything else is
    about this man — the seasons his record covers, a season he missed in full, a
    gap that could not be priced, a CLIPPED multiplier, the role-floor sentence —
    and is carried through untouched.

    The split is by IDENTITY (recomputed from the row's own prior and removed by
    equality), never by matching a prefix, so a re-worded ``availability.py``
    cannot make this drop a hedge or duplicate a paragraph.
    """
    prior = row.prior
    general: set[str] = set()
    try:
        general = {
            prior.describe(row.position),
            prior.method_note(row.position),
            prior.block_note(),
        }
    except av.DurabilityError:
        # A prior that cannot describe this position is a disclosure problem, not
        # a reason to drop disclosure: carry everything.
        general = set()
    return tuple(r for r in row.reasons if r not in general)


def _availability_reasons(
    rec: PickRec, row: av.PlayerAvailability, share: float, delta: float, frac: float
) -> tuple[str, ...]:
    """The summary sentence a novice can act on, plus the row's own evidence.

    Sentence one is this module's: the games, the share, the basis and — the part
    the row itself cannot know — what the discount COST him in the engine's own
    units. Everything after it is ``availability.py`` speaking for itself.
    """
    played = row.expected_games_played
    games = len(row.game_weeks)
    basis = _basis_clause(rec, row)
    if rec.vor <= 0.0:
        head = (
            f"Durability: expects to play {played:.1f} of his club's {games} games "
            f"({100 * share:.0f}%), {basis}. His value here is already at or below "
            f"a replacement player's, so no further discount is applied."
        )
    else:
        tail = "" if frac >= 1.0 else (
            f" (his value was already cut to {100 * frac:.0f}% as bench depth, and "
            f"the durability discount applies to what is left)"
        )
        head = (
            f"Durability: expects to play {played:.1f} of his club's {games} games "
            f"({100 * share:.0f}%), {basis}, so this board counts "
            f"{100 * share:.0f}% of his value — {delta:+.0f} points against "
            f"him{tail}."
        )
    return (head,) + _carried(row)


def _why_not(rec: PickRec) -> str:
    """The engine's own alternatives phrasing, re-derived from the NEW order.

    Mirrors ``PickEngine._why_not`` exactly (its ``est.survival[pid]`` is this
    row's ``survival_next``), so re-ranking never changes the WORDS, only which
    players they are attached to.
    """
    s = rec.survival_next
    if s >= 0.75:
        return f"you can likely wait — about {round(s * 100)}% he's still there next pick"
    return "close in value, but a smaller edge here"


def assert_adjustments_are_live(
    picker: DurablePicker, contexts: PickContext | Sequence[PickContext]
) -> tuple[DurableAdjustment, ...]:
    """Refuse to measure a variant that is not doing anything. A/B PREFLIGHT.

    An inert challenger — a module captured mid-edit, a mis-built ``inputs``
    whose ids do not match the board, both weights zeroed by a copy-paste — comes
    out of the paired harness as ``mean_delta = +0.0000, sd 0.0000, ties n/n``,
    which is shaped exactly like a genuine "no effect" finding and is reported as
    one. That happened in this repo, to an auditor, on this module, while the
    tree was being edited under him.

    Pass the context(s) an A/B is about to draft through and the failure becomes
    a refusal instead of a number. A SEQUENCE is the honest call for the handcuff
    arm: its term is reachable on only ~5% of picks (see the module docstring's
    structural limit), so one arbitrary context proves nothing about it and this
    raises only when EVERY context given is inert.

    It runs a full ``engine.recommend`` per context and so CONSUMES each
    ``ctx.rng``: hand it contexts you are not also drafting from.

    Returns the adjustments from the first context that moved anything, so a
    caller can log what moved.
    """
    ctxs = [contexts] if isinstance(contexts, PickContext) else list(contexts)
    if not ctxs:
        raise DurableVariantError("no contexts to check")
    seen = 0
    for ctx in ctxs:
        adjs = picker.adjustments(ctx)
        seen += len(adjs)
        if any(a.delta != 0.0 for a in adjs):
            return adjs
    raise DurableVariantError(
        f"INERT VARIANT: every one of {seen} adjustments across {len(ctxs)} "
        f"context(s) is exactly 0.0 (b_availability={picker.b_availability}, "
        f"b_contingent={picker.b_contingent}, {picker.inputs.priced} of "
        f"{picker.inputs.board_rows} board rows priced). A paired A/B would "
        f"report this as mean_delta 0.000 with every pair an exact tie, which is "
        f"indistinguishable from a real 'no effect' result. Refusing to be "
        f"measured in this state."
    )


# ============================================================================
#            evaluation-only: an objective that can SEE availability
# ============================================================================
#
# ``grader.grade_roster`` has no availability model: every player is present for
# all sixteen games, so a bench player is worth exactly 0.000 unless he covers a
# bye. That is the objective this variant exists to disagree with, and grading the
# variant under it measures the variant's cost and NONE of its benefit. The two
# helpers below build weekly-points maps that price availability, so the same
# paired harness can be run under an objective that can see the thing. They are
# for the A/B; the picker never calls them.


def _weekly_like(
    weekly: Mapping[str, Mapping[int, float]], points: Mapping[str, Mapping[int, float]]
):
    """``points`` wearing ``weekly``'s metadata, so ``grade_roster`` still works."""
    positions = getattr(weekly, "positions", None)
    if positions is None:
        raise DurableVariantError(
            "the weekly points map carries no positions; pass the WeeklyPointsMap "
            "that grader.weekly_points_map() returns (grade_roster needs positions "
            "to model the field and to price the streamed K/DST slots)"
        )
    return grader.WeeklyPointsMap(
        points,
        positions=positions,
        names=getattr(weekly, "names", {}),
        teams=getattr(weekly, "teams", {}),
    )


def expected_weekly_map(
    weekly: Mapping[str, Mapping[int, float]], inputs: DurableInputs
):
    """``weekly`` with every week's points multiplied by P(he plays that week).

    THE CHEAP OBJECTIVE, and its limitation stated up front: it prices the LEVEL
    of availability (a fragile star is worth less) and NOT its structure. The
    lineup seater still sees a discounted-but-present player every week, so a
    bench player who covers a real absence is still worth ~0. Use
    :func:`sampled_weekly_maps` when the question is about depth.

    Byes are untouched: ``week_available`` has no key for a bye week and the
    points map has no entry either, so the missing-week convention survives.
    """
    out: dict[str, dict[int, float]] = {}
    for pid, wpts in weekly.items():
        row = inputs.availability.get(pid)
        if row is None:
            out[pid] = dict(wpts)
            continue
        # A week the availability row does not cover is a week this model has
        # nothing to say about — NOT a week he is out. Zeroing it would be the
        # same silent-collapse failure the repo pays for elsewhere.
        out[pid] = {
            w: float(p) * (row.p_available(w) if w in row.week_available else 1.0)
            for w, p in wpts.items()
        }
    return _weekly_like(weekly, out)


def sampled_weekly_maps(
    weekly: Mapping[str, Mapping[int, float]],
    inputs: DurableInputs,
    *,
    samples: int,
    seed: int,
) -> tuple:
    """``samples`` whole simulated seasons: the same board with injuries in it.

    Each map removes the weeks a player is absent for in that simulated season,
    drawn through ``availability.sample_available_weeks`` so absences come out as
    BLOCKS (the measured mean is 3.5 games) rather than as scattered independent
    weeks — which is the difference between an absence a bench absorbs and one it
    does not.

    THE DRAW IS A PROPERTY OF THE PLAYER, NOT OF THE DRAFT: the stream is seeded
    on ``(seed, sample, player_id)``, so the same player has the same season in
    every arm of a paired comparison and in every roster he is drafted onto. That
    is what keeps the comparison paired instead of adding sampling noise to it.
    Seeded from a string, so it is stable across processes. The key must contain
    the PLAYER ID and not his position in this mapping's iteration order, or a
    player's simulated season starts depending on who else is in the map.

    WHAT THAT PAIRING DOES NOT BUY, and it is the thing most easily misread:
    every number computed against a fixed set of maps is CONDITIONAL ON THIS
    ``seed``. The paired interval around a delta contains no world-draw
    uncertainty at all, and at ``samples=8`` the world draw moves the point
    estimate by more than that interval is wide. Record the seed with the result
    and report the spread across several draws — :data:`SAMPLED_OBJECTIVE_NOTE`
    is stamped into every grade :func:`averaged_grade_fn` returns for exactly
    this reason.

    A player with no availability row is left exactly as he is (see
    :meth:`DurableInputs.fraction`): unknown is not "durable", but it is also not
    something this function may invent an absence for.
    """
    if samples < 1:
        raise DurableVariantError("samples must be at least 1")
    out = []
    for s in range(int(samples)):
        points: dict[str, dict[int, float]] = {}
        for pid, wpts in weekly.items():
            row = inputs.availability.get(pid)
            if row is None:
                points[pid] = dict(wpts)
                continue
            rng = random.Random(f"ziggurat-durable:{seed}:{s}:{pid}")
            played = av.sample_available_weeks(row, rng)
            points[pid] = {w: float(p) for w, p in wpts.items() if played.get(w, True)}
        out.append(_weekly_like(weekly, points))
    return tuple(out)


def averaged_grade_fn(maps: Sequence, *, make_grade_fn, **grade_kwargs):
    """Average a ``GradeFn`` over several simulated seasons.

    ``make_grade_fn`` is ``evaluate.make_grade_fn`` (passed in rather than
    imported so this module does not depend on the harness); ``grade_kwargs`` go
    to each bound grade function unchanged. The returned callable has the
    ``GradeFn`` signature and returns the FIRST sample's :class:`SeasonGrade` with
    its scalar fields replaced by the mean across samples — so hole_weeks and the
    reasons still describe a concrete season while the numbers are expectations.

    Every returned grade carries :data:`SAMPLED_OBJECTIVE_NOTE` in its reasons:
    the numbers are conditional on ONE draw of worlds, and a paired interval
    computed against a fixed draw is narrower than the truth. That disclosure is
    stamped here rather than left to the caller because the caller who forgets it
    is the one who quotes the interval.
    """
    if not maps:
        raise DurableVariantError("no simulated seasons to average over")
    fns = [make_grade_fn(m, positions=getattr(m, "positions", None), **grade_kwargs)
           for m in maps]
    note = (
        f"{SAMPLED_OBJECTIVE_NOTE} This grade averages {len(maps)} simulated "
        f"season(s)."
    )

    def _grade(entries, opponents):
        grades = [fn(entries, opponents) for fn in fns]
        k = float(len(grades))
        return dc_replace(
            grades[0],
            objective=sum(g.objective for g in grades) / k,
            expected_wins=sum(g.expected_wins for g in grades) / k,
            playoff_prob=sum(g.playoff_prob for g in grades) / k,
            title_prob=sum(g.title_prob for g in grades) / k,
            reasons=tuple(grades[0].reasons) + (note,),
        )

    return _grade
