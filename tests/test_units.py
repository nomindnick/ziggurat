"""Item 4.7 parts 3 and 4 — the units every quoted number is in (the shared helpers).

The page-level pins sit beside each page's own fixtures (``test_waiver.py``,
``test_streaming.py``, ``test_lineup_support.py``, each under an "item 4.7"
banner). This file pins the arithmetic and vocabulary those pages share: that a
season total is divided down to a per-week figure BEFORE it meets a one-week swing,
that a real gain is never printed as '+0.0', that a missing swing is said rather
than invented, and that the module stays a leaf so every page can import it.
"""

import ast
from pathlib import Path

import pytest

from ziggurat.core import units
from ziggurat.core.units import PROJECTED, REALISED, UNITS_LEGEND, WeeklyScale

TEAM = WeeklyScale(sigma=23.5, basis="a test lineup",
                   provenance="[hypothesis: a test prior; item 0 (2026-01-01)]")


@pytest.mark.parametrize("x, out", [
    (1.94, "+1.9"), (-1.94, "-1.9"), (0.136, "+0.1"),
    (0.04, "+0.04"), (0.004, "+0.004"), (0.0, "+0.0"), (-0.0, "+0.0"),
])
def test_signed_never_prints_a_real_gain_as_zero(x, out):
    """A +0.5 chain over 14 weeks is +0.036/wk: at one decimal that reads '+0.0',
    i.e. "nothing", which is a different claim from "small"."""
    assert units.signed(x) == out


def test_a_season_total_is_divided_per_week_before_it_meets_a_week_swing():
    """The plan's own example. Setting a 14-week sum against a ONE-week swing would
    overstate the gain fourteenfold, so the per-week figure is the one compared."""
    s = units.season_gain_sentence(4.3, 14, TEAM)
    assert "+4.3 over 14 wks is about +0.3 pts/wk" in s
    assert "against a normal week-to-week swing in your team's score of about +/-23.5 pts" in s
    assert f"the {PROJECTED} +4.3" in s
    assert TEAM.basis in s and TEAM.provenance in s
    # the number set against the swing is the per-week one, stated before it
    assert s.index("pts/wk") < s.index("+/-23.5")


def test_a_one_week_window_reads_this_week_with_no_conversion():
    s = units.season_gain_sentence(1.9, 1, TEAM)
    assert "+1.9 pts this week" in s
    assert "pts/wk" not in s and "1 wks" not in s


def test_an_unmeasured_swing_is_said_not_invented():
    for s in (units.season_gain_sentence(1.9, 14, None),
              units.week_gain_sentence(0.4, None)):
        assert "could not be measured" in s
        assert "+/-" not in s


def test_the_lane_sentence_points_back_instead_of_repeating_the_bracket():
    cited = units.week_gain_sentence(0.4, TEAM)
    back = units.week_gain_sentence(0.4, TEAM, cite=False)
    assert TEAM.provenance in cited and TEAM.provenance not in back
    assert "quoted above" in back and "+/-23.5 pts" in back
    for s in (cited, back):
        assert "ONE week" in s and "+0.4 pts" in s and PROJECTED in s
        assert "pts/wk" not in s        # one-week gains need no conversion


def test_the_matchup_sentence_quotes_all_three_swings_and_the_prior():
    own = WeeklyScale(sigma=23.1, basis="b", provenance="[p]")
    s = units.matchup_sentence(own=own, opp_sigma=17.5, opp_basis="his lineup",
                               margin_sigma=29.0)
    assert "margin swings about +/-29.0 pts" in s
    assert "your lineup +/-23.1" in s and "your opponent's +/-17.5 from his lineup" in s
    assert "[p]" in s and PROJECTED in s
    alone = units.matchup_sentence(own=own, opp_sigma=None, opp_basis="",
                                   margin_sigma=None)
    assert "+/-23.1 pts" in alone and "no opponent" in alone
    assert "margin swings" not in alone


def test_the_house_sentence_names_the_column_kind_and_the_unit():
    s = units.house_scale_sentence("D/ST", WeeklyScale(6.2, "at 6.0", "[p]"))
    assert s.startswith(f"HOUSE = {PROJECTED} house pts this week")
    assert "one D/ST's real week typically lands within about +/-6.2 pts" in s
    assert "[p]" in s


def test_the_legend_names_both_kinds_and_glosses_the_swing_once():
    assert PROJECTED in UNITS_LEGEND and REALISED in UNITS_LEGEND
    assert "forecast, not a result" in UNITS_LEGEND
    assert "never add or compare the two" in UNITS_LEGEND
    # the swing is glossed in words, so the page sentences need no jargon
    assert "standard deviation" in UNITS_LEGEND and "two weeks in three" in UNITS_LEGEND
    assert "sigma" not in UNITS_LEGEND.lower()


def test_units_is_a_leaf_that_imports_nothing_from_ziggurat():
    """``lineup_support`` imports ``streaming``, so a shared helper that imported
    either would be an import cycle for the other. The three pages can only share
    one vocabulary if the module that holds it depends on nothing."""
    tree = ast.parse(Path(units.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("ziggurat"), node.module
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("ziggurat") for a in node.names)


def test_units_holds_no_hard_coded_swing():
    """Item 4.7: the plan's '23.5' is an EXAMPLE. Every swing printed comes from
    item 3.5's measured priors through ``lineup_support``; a literal here would be
    a number no prior stands behind."""
    src = Path(units.__file__).read_text()
    for literal in ("23.5", "23.53", "21.88", "26.29", "17.5"):
        assert literal not in src, literal
