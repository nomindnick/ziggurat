"""Interval statistics shared by the backtest harnesses (item 4.1).

ONE implementation of the two intervals every backtest report must print side
by side, so that no report can quietly show only the flattering one:

* the **pooled** interval — every draft / pick / week is treated as an
  observation.  Tight, and *pseudo-replicated*: the observations inside a
  season share the same market, the same injuries and the same scrape
  cadence, so they are not independent draws.
* the **season-block** interval — each SEASON is one observation, a Student-t
  on ``df = seasons - 1``.  Honest and wide: with five seasons the critical
  value is 2.776, not 1.96.

The Student-t machinery is stdlib only (scipy is not a dependency) and is a
verbatim copy of the pinned helpers in ``ziggurat/draft/evaluate.py``.  It is
copied rather than imported on purpose: Rule 8 quarantines ``ziggurat/draft``
and the permanent replay harness must not depend on it.
``tests/test_backtest_scorecards.py`` pins the two copies equal so they cannot
drift apart silently, and ``backtest/draft_backtest.py`` now computes its
season-block interval through this module.

The pooled *rate* interval (hits out of n) is Wilson's score interval — the
normal approximation is anti-conservative at the hit counts a k<=3 replay
produces.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "Interval",
    "season_block_interval",
    "t_interval",
    "wilson_interval",
]


# ---------------------------------------------------------------------------
# Student-t machinery (verbatim from ziggurat/draft/evaluate.py, see module doc)
# ---------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    tiny = 1e-300
    eps = 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lb + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_cdf(t: float, df: float) -> float:
    """P(T <= t) for Student's t with ``df`` degrees of freedom."""
    x = df / (df + t * t)
    tail = 0.5 * _betai(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def _t_ppf(p: float, df: float) -> float:
    """Inverse of :func:`_t_cdf` by bisection.

    stdlib only — scipy is not a dependency of this project and a normal
    quantile is anti-conservative at the n a smoke run uses (z=1.96 against
    t=2.26 at df=9 understates the interval by 15%).
    """
    lo, hi = -1.0e4, 1.0e4
    for _ in range(300):
        mid = (lo + hi) / 2.0
        if _t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """A point estimate with its two-sided interval.

    ``n`` is the number of observations the interval treats as independent —
    picks for a pooled interval, seasons for a block interval.  ``sd`` is the
    sample standard deviation of those observations (0.0 for a rate interval,
    where the dispersion is implied by the count).  ``kind`` is the label the
    report prints beside the number, so the reader always sees which unit of
    replication produced it.
    """

    mean: float
    lo: float
    hi: float
    sd: float
    n: int
    confidence: float
    kind: str

    @property
    def excludes_zero(self) -> bool:
        """True only for a REAL interval that sits wholly on one side of zero.

        A zero-dispersion sample (every observation identical) collapses to
        ``lo == hi == mean``; that point is not evidence that the effect is
        distinguishable from zero — it is n observations that happened to
        agree — so it never earns the star.  Neither does a degenerate
        (infinite) interval.
        """
        return self.lo < self.hi and (self.lo > 0.0 or self.hi < 0.0)

    @property
    def is_degenerate(self) -> bool:
        return math.isinf(self.lo) or math.isinf(self.hi)

    def format(self, *, digits: int = 3) -> str:
        if self.is_degenerate:
            return f"{self.mean:+.{digits}f} [n={self.n}: no interval]"
        return f"{self.mean:+.{digits}f} [{self.lo:+.{digits}f}, {self.hi:+.{digits}f}] n={self.n}"


def t_interval(
    values: Sequence[float], *, confidence: float = 0.95, kind: str = "pooled"
) -> Interval:
    """Student-t interval on the mean of ``values`` (df = n - 1).

    ``n < 2`` returns the mean with an infinite interval — one observation
    carries no dispersion, and printing a point as if it were certain would be
    the wrong kind of quiet.  A zero sample standard deviation returns the
    point itself: widening it would invent uncertainty the data did not show.
    """
    n = len(values)
    if n == 0:
        raise ValueError("t_interval: no observations")
    mean = statistics.fmean(values)
    if n < 2:
        return Interval(mean, float("-inf"), float("inf"), 0.0, n, confidence, kind)
    sd = statistics.stdev(values)
    if sd == 0.0:
        return Interval(mean, mean, mean, 0.0, n, confidence, kind)
    se = sd / math.sqrt(n)
    t = _t_ppf(0.5 + confidence / 2.0, n - 1)
    return Interval(mean, mean - t * se, mean + t * se, sd, n, confidence, kind)


def season_block_interval(
    per_season: Mapping[int, float], *, confidence: float = 0.95
) -> Interval:
    """Treat each SEASON as one observation: a t on ``df = seasons - 1``.

    ``per_season`` maps season -> that season's mean effect (a paired
    difference, a lift, ...).  The order of seasons does not affect the
    result; they are sorted for determinism of any downstream rendering.
    """
    if not per_season:
        raise ValueError("season_block_interval: no seasons")
    values = [per_season[s] for s in sorted(per_season)]
    return t_interval(values, confidence=confidence, kind="season-block")


def wilson_interval(hits: int, n: int, *, confidence: float = 0.95) -> Interval:
    """Wilson score interval for a rate ``hits / n``.

    ``n == 0`` returns a NaN rate with an infinite interval so a report can
    print "no observations" rather than a 0% that looks like a measurement.
    """
    if hits < 0 or n < 0 or hits > n:
        raise ValueError(f"wilson_interval: hits={hits} n={n}")
    if n == 0:
        return Interval(float("nan"), float("-inf"), float("inf"), 0.0, 0, confidence, "wilson")
    z = statistics.NormalDist().inv_cdf(0.5 + confidence / 2.0)
    p = hits / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return Interval(p, centre - half, centre + half, 0.0, n, confidence, "wilson")
