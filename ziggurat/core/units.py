"""The units every quoted number is in — item 4.7, parts 3 and 4 (presentation only).

WHY THIS EXISTS. The pages the weekly cadence reads aloud (``waivers``, ``stream``,
``lineup``) print point totals with no sense of the noise around them, and they
print two different KINDS of number side by side without saying which is which.
The operator is a novice (Rule 6): he cannot tell a +1.9 that is a rounding error on
one week from a +1.9 that decides it, and the Monday retro was comparing projected
numbers with realised ones by hand, as if they were commensurable.

PART 3 — THE DISPERSION SENTENCE. Every gain read aloud prints its scale beside it.
The units are made COMMENSURABLE before they are compared: a 14-week total is
divided down to a per-week figure BEFORE it is set against a team-WEEK swing, and
the sentence says so. Setting a season sum against a one-week swing would overstate
the gain fourteenfold.

The swing is never a literal in this module. It is item 3.5's MEASURED per-player
weekly prior (``lineup_support.DEFAULT_VARIANCE``), composed over a seated lineup
with the SAME arithmetic the lineup card's win probability uses
(``lineup_support.lineup_sigma``). Every sentence quotes that prior's label and
source (Rule 6: a prior is a labelled hypothesis with its provenance).

PART 4 — PROJECTED / REALISED. A PROJECTED number is priced from the Sleeper
projection feed through ``scoring.py``: a forecast. A REALISED number was measured
on actual past scores (a backtest): a result. Each page prints ONE legend
(``UNITS_LEGEND``) that makes PROJECTED the default reading of a points number, and
every realised figure on a page carries the word REALISED beside it. The legend is
one constant so the three pages cannot paraphrase each other into disagreement (the
``streaming.DST_CARD_SENTENCES`` pattern).

WHAT THIS MODULE MAY NOT DO. It moves no recommendation, ordering, gain or chain
membership: it formats numbers the pages already hold. Rule 2 — nothing here is a
scoring value; a swing is a dispersion prior. Rule 3 — no CLI. It imports nothing
from ``ziggurat.core``, deliberately: ``lineup_support`` imports ``streaming``, so a
shared helper that imported either would be a cycle for the other.
"""

from dataclasses import dataclass

#: The two kinds of number (item 4.7 part 4). Spelled once so a test can find them.
PROJECTED = "PROJECTED"
REALISED = "REALISED"

#: The per-page legend (item 4.7 part 4), printed once on ``waivers``, ``stream``
#: and ``lineup``. It also glosses "+/-X pts" once per page, so the scale sentences
#: can say "+/-23.1 pts" instead of a word the waiver page bans as jargon ("sigma"
#: — ``tests/test_waiver.py``'s reasons ban).
UNITS_LEGEND = (
    f"UNITS: every pts number on this page is {PROJECTED} (Sleeper's projection feed "
    f"priced through the house scoring engine — a forecast, not a result) unless it "
    f"is marked {REALISED} (measured on actual past scores); never add or compare the "
    f"two as if they were the same kind of number. A '+/-X pts' swing is one standard "
    f"deviation: roughly two weeks in three land within X of the average."
)


@dataclass(frozen=True)
class WeeklyScale:
    """One week's typical swing in house points, with where it came from (Rule 6).

    ``sigma`` is ONE standard deviation of a single week's score. ``basis`` says in
    plain words what was measured ("your projected week-4 starting lineup ...");
    ``provenance`` quotes the prior's label and source verbatim.
    """

    sigma: float
    basis: str
    provenance: str


def signed(x: float) -> str:
    """A signed points figure that never prints a non-zero value as '+0.0'.

    One decimal, like every other number on these pages — widened only when one
    decimal would round a real, non-zero figure to zero, because '+0.0 pts/wk'
    reads as "nothing" and a 14-week gain divided down can land there.
    """
    for places in (1, 2, 3):
        if round(x, places) != 0.0:
            return f"{x:+.{places}f}"
    return "+0.0"           # genuinely zero (and never the '-0.0' a negative zero prints)


def swing(sigma: float) -> str:
    """The plain-words spelling of a one-standard-deviation swing."""
    return f"about +/-{sigma:.1f} pts"


def per_week(total: float, weeks: int) -> float:
    """A multi-week total expressed per week — the unit a team-WEEK swing is in."""
    return total / weeks if weeks > 1 else total


def _against_team(team: WeeklyScale | None, *, cite: bool) -> str:
    if team is None:
        return ("; your team's week-to-week swing could not be measured at this "
                "as-of (no starting lineup could be priced), so no scale is quoted")
    if not cite:
        return (f", against the same week-to-week swing in your team's score quoted "
                f"above ({swing(team.sigma)})")
    return (f", against a normal week-to-week swing in your team's score of "
            f"{swing(team.sigma)} ({team.basis}) {team.provenance}")


def season_gain_sentence(total: float, weeks: int, team: WeeklyScale | None) -> str:
    """The chain total's scale (item 4.7 part 3), per week before it is compared.

    ``+G over N wks is about +G/N pts/wk, against a normal week-to-week swing in
    your team's score of about +/-S pts`` — the season sum is divided down FIRST,
    because the swing it is set against is a one-week quantity.
    """
    if weeks <= 1:
        head = f"the {PROJECTED} {signed(total)} pts this week"
    else:
        head = (f"the {PROJECTED} {signed(total)} over {weeks} wks is about "
                f"{signed(per_week(total, weeks))} pts/wk")
    return f"SCALE: {head}{_against_team(team, cite=True)}"


def week_gain_sentence(largest: float, team: WeeklyScale | None, *, cite: bool = True) -> str:
    """The streaming lane's scale: its gains are ONE week each, so no conversion.

    ``cite=False`` points back at a provenance already printed on the page rather
    than repeating a long bracket two lines apart.
    """
    return (f"SCALE: every gain in this lane is a {PROJECTED} change to ONE week's "
            f"score (the largest here is {signed(largest)} pts)"
            f"{_against_team(team, cite=cite)}")


def house_scale_sentence(unit: str, scale: WeeklyScale) -> str:
    """The stream page's HOUSE column: what it is and how far one week wanders."""
    return (f"HOUSE = {PROJECTED} house pts this week; one {unit}'s real week typically "
            f"lands within {swing(scale.sigma)} of it ({scale.basis}) {scale.provenance}")


def matchup_sentence(
    *, own: WeeklyScale, opp_sigma: float | None, opp_basis: str, margin_sigma: float | None,
) -> str:
    """The lineup card's margin / win-prob scale (item 4.7 part 3).

    ``margin_sigma`` is the win probability's own denominator — the square root of
    both lineups' variances summed — so the sentence explains the number printed
    beside it rather than a different quantity. With no opponent readable there is
    no margin, and the sentence says only how far YOUR total wanders.
    """
    if opp_sigma is None or margin_sigma is None:
        return (f"SCALE: your {PROJECTED} total swings {swing(own.sigma)} from week to "
                f"week ({own.basis}); no opponent is readable this week, so there is no "
                f"margin to set it against {own.provenance}")
    return (f"SCALE: the {PROJECTED} margin swings {swing(margin_sigma)} from week to "
            f"week (your lineup +/-{own.sigma:.1f}, your opponent's +/-{opp_sigma:.1f} "
            f"from {opp_basis}); the win prob is the chance the real margin lands above "
            f"zero given that swing {own.provenance}")
