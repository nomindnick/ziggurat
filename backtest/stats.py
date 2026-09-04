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

Item 4.2 adds the PAIRED machinery of the frozen pre-registration
(``intel/research/breakout-backtest.md`` §5): :func:`paired_by_key` (two
per-week vectors keyed by ``(season, week)`` -> both intervals on their
COMMON keys, the dropped keys named), :func:`sign_flip_permutation` (the
primary test — exact under exchangeability of the week-level signs and immune
to the zero spike a single-knob step produces), :func:`max_null_step_down`
(the multiplicity bar: the null of ``max_g D*(g)`` over a family of settings
under ONE shared flip pattern) and :func:`mcnemar_exact` (the A10 sign fence).
The flip for a ``(season, week)`` key in draw ``b`` is a function of the seed
and the key ALONE (:func:`flip_pattern`), never of which other keys happen to
be in the vector — so a cell tested alone and the same cell inside the family
see identical null draws.  Still stdlib only; still no numpy.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "FlipPattern",
    "Interval",
    "Key",
    "MaxNull",
    "McNemar",
    "PERMUTATION_DRAWS",
    "PERMUTATION_SEED",
    "Paired",
    "Permutation",
    "flip_pattern",
    "max_null_step_down",
    "mcnemar_exact",
    "nearest_rank_percentile",
    "paired_by_key",
    "season_block_interval",
    "sign_flip_permutation",
    "t_interval",
    "wilson_interval",
]

#: A per-week vector's key: ``(season, week)``.
Key = tuple[int, int]

#: The sign-flip defaults of the frozen pre-registration (Appendix A):
#: ``PERMUTATION_SEED = 0``; ``B = 10,000``.
PERMUTATION_SEED = 0
PERMUTATION_DRAWS = 10_000


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


# ---------------------------------------------------------------------------
# item 4.2 — paired per-week comparison
# ---------------------------------------------------------------------------


def _key(k: object) -> Key:
    """Normalise a ``(season, week)`` key; anything else is refused loudly."""
    try:
        season, week = k  # type: ignore[misc]
    except (TypeError, ValueError):
        raise TypeError(f"per-week vectors are keyed by (season, week); got {k!r}") from None
    return int(season), int(week)


def _normalise(vec: Mapping[object, float], *, what: str) -> dict[Key, float]:
    out: dict[Key, float] = {}
    for k, v in vec.items():
        key = _key(k)
        if key in out:
            raise ValueError(f"{what}: key {key} given twice")
        out[key] = float(v)
    return out


@dataclass(frozen=True)
class Paired:
    """A paired comparison of two per-week vectors on their COMMON keys.

    ``mean`` is ``D`` — the equal-week-weight mean of ``a[w] - b[w]`` over the
    keys BOTH vectors hold.  ``per_key`` is the Student-t interval on those
    differences (``kind="paired per-week"``); ``block`` treats each SEASON's
    mean difference as one observation (``kind="season-block"``).  Both are
    printed, both are labelled, and the frozen pre-registration says neither
    is a gate: on a spike-and-slab sample they are calibrated in neither
    direction — the permutation test is (:func:`sign_flip_permutation`).

    Nothing is dropped silently: ``only_a`` / ``only_b`` name the keys that
    were in exactly one vector (each is reported even when empty), and
    ``per_season_n`` reports how many common keys each season contributed.
    ``equal_count_keys`` is True when every season contributed the SAME number
    of common keys — the balanced design on which the per-week and season-block
    centres coincide exactly (§5.2); when it is False the two centres are
    different numbers and the reader must be told which is which.

    One common key is a DEGENERATE comparison (both intervals infinite), never
    a point; no common key at all is an error that names both key sets.
    """

    mean: float
    per_key: Interval
    block: Interval
    n_common: int
    only_a: tuple[Key, ...]
    only_b: tuple[Key, ...]
    equal_count_keys: bool
    per_season: Mapping[int, float]
    per_season_n: Mapping[int, int]
    diffs: tuple[tuple[Key, float], ...]

    @property
    def diff_vector(self) -> dict[Key, float]:
        """The ``d_w`` vector as a mapping — the input to the permutation tests."""
        return dict(self.diffs)


def paired_by_key(
    a: Mapping[object, float],
    b: Mapping[object, float],
    *,
    confidence: float = 0.95,
) -> Paired:
    """Pair ``a`` against ``b`` on the intersection of their ``(season, week)`` keys.

    ``a`` is the setting under test and ``b`` the reference, so a positive
    ``mean`` means ``a`` is better.  The intersection is taken here and
    REPORTED here (``only_a`` / ``only_b``); no caller has to remember to.
    """
    va = _normalise(a, what="paired_by_key: a")
    vb = _normalise(b, what="paired_by_key: b")
    common = sorted(set(va) & set(vb))
    if not common:
        raise ValueError(
            "paired_by_key: no common (season, week) keys — "
            f"a has {sorted(va)}; b has {sorted(vb)}"
        )
    only_a = tuple(sorted(set(va) - set(vb)))
    only_b = tuple(sorted(set(vb) - set(va)))
    diffs = tuple((k, va[k] - vb[k]) for k in common)
    values = [d for _, d in diffs]
    per_key = t_interval(values, confidence=confidence, kind="paired per-week")
    by_season: dict[int, list[float]] = {}
    for (season, _), d in diffs:
        by_season.setdefault(season, []).append(d)
    per_season = {s: statistics.fmean(v) for s, v in sorted(by_season.items())}
    per_season_n = {s: len(v) for s, v in sorted(by_season.items())}
    block = season_block_interval(per_season, confidence=confidence)
    return Paired(
        mean=per_key.mean,
        per_key=per_key,
        block=block,
        n_common=len(common),
        only_a=only_a,
        only_b=only_b,
        equal_count_keys=len(set(per_season_n.values())) == 1,
        per_season=per_season,
        per_season_n=per_season_n,
        diffs=diffs,
    )


# ---------------------------------------------------------------------------
# item 4.2 — the sign-flip permutation test and the max-null bar
# ---------------------------------------------------------------------------


def _key_stream(seed: int, key: Key) -> random.Random:
    # A str seed is hashed with SHA-512 by ``random.seed`` (version 2), so the
    # stream is a pure function of (seed, season, week) — independent of
    # PYTHONHASHSEED and of which other keys share the pattern.
    return random.Random(f"ziggurat-4.2-flip:{int(seed)}:{key[0]}:{key[1]}")


@dataclass(frozen=True)
class FlipPattern:
    """``b`` sign-flip draws over a set of ``(season, week)`` keys.

    The sign of key ``w`` in draw ``i`` depends on ``seed`` and ``w`` ONLY, so
    two patterns built with the same seed agree on every key they share
    whatever else each contains.  That is the property the max-null family
    needs: one flip for week ``w`` in draw ``i``, applied to every setting
    whose paired set contains ``w``.
    """

    keys: tuple[Key, ...]
    b: int
    seed: int
    _bits: Mapping[Key, int]

    def sign(self, key: object, draw: int) -> int:
        """``+1`` or ``-1`` for ``key`` in draw ``draw`` (0-based)."""
        if not 0 <= draw < self.b:
            raise IndexError(f"draw {draw} outside 0..{self.b - 1}")
        return 1 if (self._bits[_key(key)] >> draw) & 1 else -1

    def signs(self, key: object) -> tuple[int, ...]:
        """All ``b`` signs of one key, in draw order."""
        return tuple(self.sign(key, i) for i in range(self.b))

    def flipped_means(self, d: Mapping[object, float]) -> list[float]:
        """The mean of ``d`` under each of the ``b`` draws (the null draws of
        ``mean(d)``).  Every key of ``d`` must be in the pattern; a key that
        is not would silently be an unflipped constant, so it is refused."""
        vec = _normalise(d, what="flipped_means")
        if not vec:
            raise ValueError("flipped_means: an empty vector has no mean")
        missing = sorted(set(vec) - set(self._bits))
        if missing:
            raise KeyError(f"flipped_means: keys not in the flip pattern: {missing}")
        n = len(vec)
        items = [(self._bits[k], v) for k, v in vec.items() if v != 0.0]
        zero_sum = 0.0
        out: list[float] = []
        for i in range(self.b):
            total = zero_sum
            for bits, v in items:
                total += v if (bits >> i) & 1 else -v
            out.append(total / n)
        return out


def flip_pattern(
    keys: Iterable[object], *, b: int = PERMUTATION_DRAWS, seed: int = PERMUTATION_SEED
) -> FlipPattern:
    """Build the shared flip pattern over ``keys`` (deduplicated, sorted)."""
    if b < 1:
        raise ValueError(f"flip_pattern: b={b} draws; need at least 1")
    ks = tuple(sorted({_key(k) for k in keys}))
    if not ks:
        raise ValueError("flip_pattern: no keys")
    bits = {k: _key_stream(seed, k).getrandbits(b) for k in ks}
    return FlipPattern(keys=ks, b=b, seed=int(seed), _bits=bits)


@dataclass(frozen=True)
class Permutation:
    """The sign-flip test on one ``d_w`` vector.

    ``p`` is ONE-SIDED for ``mean > 0``, plus-one corrected:
    ``(1 + #{i: D*_i >= D}) / (b + 1)`` — an all-zero vector gives exactly 1,
    and no p can be 0.  ``p_two`` counts ``|D*_i| >= |D|`` the same way.
    ``null_means`` are the ``b`` flipped means, in draw order, so a caller can
    read any quantile without re-drawing.
    """

    mean: float
    p: float
    p_two: float
    n: int
    b: int
    seed: int
    ge_count: int
    abs_ge_count: int
    null_means: tuple[float, ...]


def sign_flip_permutation(
    d: Mapping[object, float],
    *,
    b: int = PERMUTATION_DRAWS,
    seed: int = PERMUTATION_SEED,
    pattern: FlipPattern | None = None,
) -> Permutation:
    """The §5.3 primary test on a paired per-week difference vector.

    Under H0 each ``d_w`` carries either sign with equal probability.  Pass
    ``pattern`` to share draws with other vectors (the family bar does); the
    default builds one over ``d``'s own keys, which — because a key's flips are
    a function of ``(seed, key)`` alone — yields the same draws.
    """
    vec = _normalise(d, what="sign_flip_permutation")
    if not vec:
        raise ValueError("sign_flip_permutation: an empty vector has no mean")
    if pattern is None:
        pattern = flip_pattern(vec, b=b, seed=seed)
    observed = statistics.fmean(vec.values())
    nulls = pattern.flipped_means(vec)
    ge = sum(1 for m in nulls if m >= observed)
    abs_ge = sum(1 for m in nulls if abs(m) >= abs(observed))
    nb = pattern.b
    return Permutation(
        mean=observed,
        p=(1 + ge) / (nb + 1),
        p_two=(1 + abs_ge) / (nb + 1),
        n=len(vec),
        b=nb,
        seed=pattern.seed,
        ge_count=ge,
        abs_ge_count=abs_ge,
        null_means=tuple(nulls),
    )


def nearest_rank_percentile(values: Sequence[float], q: float) -> float:
    """The nearest-rank ``q``-th percentile: the ``ceil(q * n)``-th smallest
    value (``q = 0.95`` of 10,000 values is the 9,500th smallest)."""
    if not values:
        raise ValueError("nearest_rank_percentile: no values")
    if not 0.0 < q <= 1.0:
        raise ValueError(f"nearest_rank_percentile: q={q} outside (0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class MaxNull:
    """The max-statistic bar over a family of settings (§5.3, gate G2).

    ``bar`` is the nearest-rank ``1 - alpha`` percentile of ``T*_i = max_g
    D*_i(g)``; a cell clears it iff its observed mean EXCEEDS the bar
    (``clearing``).  ``adjusted_p[g]`` is ``(1 + #{i: T*_i >= D(g)}) / (b + 1)``
    — the same plus-one form as the single-cell p, against the family maximum.
    The procedure is single-step: the caller tests only its winner against
    ``bar``; the per-cell values are disclosure.  ``keys`` is the UNION of
    every cell's keys — the pattern the draws were indexed on.
    """

    bar: float
    alpha: float
    b: int
    seed: int
    keys: tuple[Key, ...]
    observed: Mapping[str, float]
    adjusted_p: Mapping[str, float]
    clearing: tuple[str, ...]
    null_max: tuple[float, ...]


def max_null_step_down(
    vectors: Mapping[str, Mapping[object, float]],
    *,
    b: int = PERMUTATION_DRAWS,
    seed: int = PERMUTATION_SEED,
    alpha: float = 0.05,
) -> MaxNull:
    """The null of ``max_g D*(g)`` over ``vectors`` under ONE shared flip pattern.

    The pattern is built once over the UNION of every cell's keys; draw ``i``
    flips week ``w`` the same way in every cell whose vector holds ``w``.
    That preserves the between-setting correlation a Bonferroni count throws
    away.  Every cell passed is in the family — the caller decides the family
    (label-based, §5.3), this function never drops a member.
    """
    if not vectors:
        raise ValueError("max_null_step_down: an empty family has no maximum")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"max_null_step_down: alpha={alpha} outside (0, 1)")
    cells = {str(cid): _normalise(v, what=f"max_null_step_down: cell {cid!r}")
             for cid, v in vectors.items()}
    for cid, vec in cells.items():
        if not vec:
            raise ValueError(f"max_null_step_down: cell {cid!r} has an empty vector")
    union = {k for vec in cells.values() for k in vec}
    pattern = flip_pattern(union, b=b, seed=seed)
    observed = {cid: statistics.fmean(vec.values()) for cid, vec in cells.items()}
    per_cell = {cid: pattern.flipped_means(vec) for cid, vec in cells.items()}
    null_max = [max(per_cell[cid][i] for cid in cells) for i in range(pattern.b)]
    bar = nearest_rank_percentile(null_max, 1.0 - alpha)
    adjusted = {
        cid: (1 + sum(1 for t in null_max if t >= obs)) / (pattern.b + 1)
        for cid, obs in observed.items()
    }
    clearing = tuple(cid for cid, obs in observed.items() if obs > bar)
    return MaxNull(
        bar=bar,
        alpha=alpha,
        b=pattern.b,
        seed=pattern.seed,
        keys=pattern.keys,
        observed=observed,
        adjusted_p=adjusted,
        clearing=clearing,
        null_max=tuple(null_max),
    )


# ---------------------------------------------------------------------------
# item 4.2 — the McNemar sign fence (A10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McNemar:
    """Exact binomial McNemar on the discordant counts.

    ``b`` = reference-hit / cell-miss, ``c`` = reference-miss / cell-hit.
    ``p`` is ONE-SIDED, ``P(Bin(b + c, 1/2) >= b)`` — small when ``b`` is
    improbably large, i.e. when the cell LOSES hits the reference had.
    ``p_two`` is ``min(1, 2 * P(Bin >= max(b, c)))``.  ``b + c == 0`` gives
    ``p == p_two == 1``: no discordant slot is no evidence either way.
    ``significant_negative`` is A10's clause: ``b > c`` and ``p < alpha``.
    """

    b: int
    c: int
    n: int
    p: float
    p_two: float
    alpha: float
    significant_negative: bool


def _binom_upper_tail(n: int, k: int) -> float:
    """``P(Bin(n, 1/2) >= k)`` exactly (integer arithmetic, then one division)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (1 << n)


def mcnemar_exact(b: int, c: int, *, alpha: float = 0.05) -> McNemar:
    """The exact one-sided binomial test that ``b > c`` on the discordant pairs."""
    b, c = int(b), int(c)
    if b < 0 or c < 0:
        raise ValueError(f"mcnemar_exact: counts must be non-negative; got b={b} c={c}")
    n = b + c
    p = _binom_upper_tail(n, b)
    p_two = min(1.0, 2.0 * _binom_upper_tail(n, max(b, c)))
    return McNemar(
        b=b, c=c, n=n, p=p, p_two=p_two, alpha=alpha,
        significant_negative=(b > c and p < alpha),
    )
