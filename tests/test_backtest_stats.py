"""Item 4.2 — the paired / permutation / max-null / McNemar machinery of
``backtest/stats.py`` (breakout-backtest.md §5.2, §5.3, §5.4, §11.2).

The 4.1 interval tests (``t_interval``, ``season_block_interval``,
``wilson_interval``) live in ``tests/test_backtest_scorecards.py``; this file
covers only what 4.2 added.  Every vector here is synthetic and keyed by
``(season, week)``; nothing reads a database.
"""

from __future__ import annotations

import math
import statistics

import pytest

from backtest import stats as ST

B = 400  # draws per test — small enough to be fast, large enough to resolve


def _vec(season: int, weeks, value):
    if callable(value):
        return {(season, w): float(value(w)) for w in weeks}
    return {(season, w): float(value) for w in weeks}


# ------------------------------------------------------------ paired_by_key


def test_paired_reports_the_keys_it_dropped():
    a = {**_vec(2021, range(1, 6), 1.0), (2022, 1): 5.0, (2022, 2): 5.0}
    b = {**_vec(2021, range(1, 6), 0.0), (2023, 7): 9.0}
    p = ST.paired_by_key(a, b)
    # the intersection decides the centre; the difference is reported, not hidden
    assert p.n_common == 5 and p.mean == 1.0
    assert p.only_a == ((2022, 1), (2022, 2))
    assert p.only_b == ((2023, 7),)
    assert [k for k, _ in p.diffs] == [(2021, w) for w in range(1, 6)]
    assert p.per_season_n == {2021: 5}
    # a balanced pair reports the empties too — a zero count is still a count
    q = ST.paired_by_key(_vec(2021, range(1, 4), 2.0), _vec(2021, range(1, 4), 1.0))
    assert q.only_a == () and q.only_b == ()
    # MUTANT killed: a silent intersection (only_* absent / always empty) fails
    # the two tuple assertions; a union with 0-fill would give n_common 8.


def test_paired_with_no_common_keys_raises_and_names_both_key_sets():
    a = {(2021, 1): 1.0, (2021, 2): 1.0}
    b = {(2022, 3): 1.0}
    with pytest.raises(ValueError) as exc:
        ST.paired_by_key(a, b)
    msg = str(exc.value)
    assert "(2021, 1)" in msg and "(2021, 2)" in msg and "(2022, 3)" in msg
    assert "no common" in msg


def test_paired_on_a_single_common_key_is_degenerate_not_a_point():
    a = {(2021, 1): 3.0, (2021, 2): 8.0}
    b = {(2021, 1): 1.0, (2022, 2): 8.0}
    p = ST.paired_by_key(a, b)
    assert p.n_common == 1 and p.mean == 2.0
    assert p.per_key.is_degenerate and math.isinf(p.per_key.lo) and math.isinf(p.per_key.hi)
    assert p.block.is_degenerate and p.block.n == 1
    assert not p.per_key.excludes_zero and not p.block.excludes_zero
    # MUTANT killed: returning lo == hi == mean for n=1 (a point) makes
    # is_degenerate False and the two isinf assertions fail.


def test_paired_centre_equals_the_unpaired_difference_when_counts_match():
    # §5.2 identity 1: when both vectors hold the same keys, the paired centre
    # IS mean(a) - mean(b) — pairing changes nothing at the centre, only the
    # dispersion.  Values chosen dyadic so the identity is exact, not approx.
    weeks = range(1, 16)
    a = _vec(2021, weeks, lambda w: 0.25 * w)
    b = _vec(2021, weeks, lambda w: 0.5 * (16 - w))
    p = ST.paired_by_key(a, b)
    assert p.only_a == () and p.only_b == ()
    assert p.mean == statistics.fmean(a.values()) - statistics.fmean(b.values())
    # ... and breaks the moment one side holds a key the other lacks
    a2 = {**a, (2021, 16): 100.0}
    p2 = ST.paired_by_key(a2, b)
    assert p2.mean == p.mean  # the extra key is dropped from the centre
    assert p2.only_a == ((2021, 16),)
    assert p2.mean != statistics.fmean(a2.values()) - statistics.fmean(b.values())


def test_paired_block_uses_seasons_not_weeks():
    # three weeks in each of two seasons; season means 1 and 3.  Blocking on
    # SEASON gives n=2 with a real spread; blocking on week (the mutant) would
    # give three groups whose means are all 2 -> sd 0 -> lo == hi.
    a = {**_vec(2021, (1, 2, 3), 1.0), **_vec(2022, (1, 2, 3), 3.0)}
    b = {**_vec(2021, (1, 2, 3), 0.0), **_vec(2022, (1, 2, 3), 0.0)}
    p = ST.paired_by_key(a, b)
    assert p.per_season == {2021: 1.0, 2022: 3.0}
    assert p.per_season_n == {2021: 3, 2022: 3}
    assert p.block.n == 2 and p.block.kind == "season-block"
    assert p.block.mean == 2.0 and p.block.sd == pytest.approx(math.sqrt(2.0))
    assert p.block.lo < p.block.hi
    assert p.per_key.n == 6 and p.per_key.kind == "paired per-week"


def test_paired_per_week_and_block_centres_coincide_on_a_balanced_design():
    # §5.2 identity 2: equal per-season counts -> the equal-week-weight mean
    # and the mean of season means are the same number.  Unbalance one season
    # and they part company, and the flag says so.
    a = {**_vec(2021, range(1, 4), lambda w: w),          # mean 2
         **_vec(2022, range(1, 4), lambda w: 4 * w),      # mean 8
         **_vec(2023, range(1, 4), lambda w: 2 * w)}      # mean 4
    b = {k: 0.0 for k in a}
    p = ST.paired_by_key(a, b)
    assert p.equal_count_keys is True
    assert p.per_key.mean == p.block.mean == pytest.approx(14 / 3)
    a2 = {**a, (2023, 4): 100.0}
    b2 = {**b, (2023, 4): 0.0}
    q = ST.paired_by_key(a2, b2)
    assert q.equal_count_keys is False
    assert q.per_season_n == {2021: 3, 2022: 3, 2023: 4}
    assert q.per_key.mean != q.block.mean


def test_paired_refuses_a_key_that_is_not_season_week():
    with pytest.raises(TypeError, match=r"\(season, week\)"):
        ST.paired_by_key({"2021-1": 1.0}, {"2021-1": 0.0})


# --------------------------------------------------- sign_flip_permutation


def test_permutation_p_is_one_when_every_week_difference_is_zero():
    d = _vec(2021, range(1, 55), 0.0)
    r = ST.sign_flip_permutation(d, b=B, seed=0)
    assert r.mean == 0.0
    assert r.p == 1.0 and r.p_two == 1.0
    assert r.ge_count == B and r.abs_ge_count == B and r.b == B and r.n == 54
    # MUTANT killed: a strict '>' in the count gives ge_count 0 and p = 1/(B+1).


def test_permutation_one_sided_p_is_small_for_all_positive_and_near_one_for_its_negation():
    d = _vec(2021, range(1, 21), 1.0)
    pos = ST.sign_flip_permutation(d, b=B, seed=0)
    # only the all-plus draw ties the observed mean: P = 2^-20 per draw, so
    # with overwhelming probability ge_count == 0 and p == (0 + 1) / (B + 1)
    assert pos.ge_count == 0
    assert pos.p == (0 + 1) / (B + 1)
    neg = ST.sign_flip_permutation({k: -v for k, v in d.items()}, b=B, seed=0)
    assert neg.ge_count == B and neg.p == 1.0
    # the two-sided p is symmetric and equally small for both
    assert pos.p_two == neg.p_two == (0 + 1) / (B + 1)
    # MUTANT killed: dropping the plus-one correction gives p == 0.0 here.


def test_two_sided_p_counts_both_tails_of_the_flip_distribution():
    # ONE nonzero week: the flipped mean is +-v with equal probability, so the
    # one-sided count is the plus draws and the two-sided count is EVERY draw.
    d = {**_vec(2021, range(1, 11), 0.0), (2021, 11): 3.0}
    r = ST.sign_flip_permutation(d, b=B, seed=1)
    assert r.abs_ge_count == B and r.p_two == 1.0
    assert 0 < r.ge_count < B and r.p == (1 + r.ge_count) / (B + 1)
    assert r.p >= 0.05  # a single-week spike never reads significant
    # MUTANT killed: a two-sided p computed as 2 * one-sided (capped) would
    # read ~1.0 only by accident of the draw; abs_ge_count == B pins the
    # mechanism, not the value.


def test_a_single_week_spike_does_not_read_significant():
    d = {**_vec(2021, range(1, 54), 0.0), (2021, 54): 40.0}
    r = ST.sign_flip_permutation(d, b=2000, seed=0)
    assert r.p == pytest.approx(0.5, abs=0.05)


def test_permutation_is_deterministic_in_the_seed_and_reports_its_null_draws():
    d = _vec(2021, range(1, 11), lambda w: w - 5.5)
    r1 = ST.sign_flip_permutation(d, b=B, seed=7)
    r2 = ST.sign_flip_permutation(d, b=B, seed=7)
    r3 = ST.sign_flip_permutation(d, b=B, seed=8)
    assert r1 == r2 and r1.seed == 7
    assert r1.null_means != r3.null_means
    assert len(r1.null_means) == B
    # the observed mean is the mean, not the median or a sum
    assert r1.mean == statistics.fmean(d.values())


def test_permutation_refuses_an_empty_vector_and_a_pattern_missing_a_key():
    with pytest.raises(ValueError, match="empty"):
        ST.sign_flip_permutation({}, b=B)
    pat = ST.flip_pattern([(2021, 1), (2021, 2)], b=B, seed=0)
    with pytest.raises(KeyError, match=r"\(2021, 3\)"):
        ST.sign_flip_permutation({(2021, 1): 1.0, (2021, 3): 1.0}, pattern=pat)


# --------------------------------------------------------- flip_pattern


def test_flip_pattern_is_indexed_by_season_week_not_by_position():
    # the sign of a key in draw i depends on (seed, key) only — so two
    # patterns over DIFFERENT key sets agree on every key they share, and a
    # different seed disagrees somewhere
    small = ST.flip_pattern([(2021, 5), (2022, 5)], b=B, seed=0)
    big = ST.flip_pattern([(2021, w) for w in range(1, 19)] + [(2022, 5), (2023, 9)],
                          b=B, seed=0)
    assert small.signs((2021, 5)) == big.signs((2021, 5))
    assert small.signs((2022, 5)) == big.signs((2022, 5))
    other = ST.flip_pattern([(2021, 5)], b=B, seed=1)
    assert other.signs((2021, 5)) != small.signs((2021, 5))
    assert set(small.signs((2021, 5))) == {-1, 1}
    assert small.keys == ((2021, 5), (2022, 5)) and big.b == B
    # MUTANT killed: drawing signs from one stream in sorted-key order makes
    # (2022, 5)'s signs depend on how many keys precede it -> the equality
    # on (2022, 5) fails between `small` and `big`.


def test_flip_pattern_signs_are_balanced_and_independent_across_keys():
    pat = ST.flip_pattern([(2021, w) for w in range(1, 4)], b=4000, seed=0)
    for k in pat.keys:
        plus = sum(1 for s in pat.signs(k) if s == 1)
        assert 0.45 < plus / 4000 < 0.55
    s1, s2 = pat.signs((2021, 1)), pat.signs((2021, 2))
    agree = sum(1 for x, y in zip(s1, s2, strict=True) if x == y)
    assert 0.45 < agree / 4000 < 0.55
    with pytest.raises(IndexError):
        pat.sign((2021, 1), 4000)
    with pytest.raises(ValueError):
        ST.flip_pattern([], b=B)


# --------------------------------------------------- max_null_step_down


def test_max_null_uses_one_shared_flip_pattern_across_settings():
    full = _vec(2021, range(1, 21), lambda w: (w % 3) - 1.0)
    # the subset is the LATER half so that its keys sit at different positions
    # in its own sorted order than in the union's — a position-indexed pattern
    # would agree on the first half by accident
    subset = {k: v for k, v in full.items() if k[1] > 10}
    family = {"a": full, "a_twin": dict(full), "sub": subset,
              "other": _vec(2021, range(1, 21), lambda w: 0.5 * ((w % 2) - 0.5))}
    mn = ST.max_null_step_down(family, b=B, seed=0)
    assert mn.b == B and mn.seed == 0 and len(mn.null_max) == B
    assert mn.keys == tuple(sorted(full))  # the UNION of every cell's keys
    # (1) two identical cells receive identical null draws
    pat = ST.flip_pattern(mn.keys, b=B, seed=0)
    draws_a = pat.flipped_means(full)
    assert pat.flipped_means(family["a_twin"]) == draws_a
    # (2) a cell whose keys are a SUBSET sees the same flips on the shared keys:
    # its flipped mean under draw i is the shared-key partial sum of a's
    draws_sub = pat.flipped_means(subset)
    for i in range(B):
        partial = sum(pat.sign(k, i) * v for k, v in subset.items()) / len(subset)
        assert draws_sub[i] == pytest.approx(partial, abs=1e-12)
        assert draws_a[i] == pytest.approx(
            sum(pat.sign(k, i) * v for k, v in full.items()) / len(full), abs=1e-12)
    # (3) the family maximum is the per-draw max of exactly those per-cell draws
    draws_other = pat.flipped_means(family["other"])
    for i in range(B):
        assert mn.null_max[i] == max(draws_a[i], draws_sub[i], draws_other[i])
    # (4) and the single-cell test on `sub` ALONE sees the same draws — the
    # pattern is indexed by (season, week), not by the family it sits in
    alone = ST.sign_flip_permutation(subset, b=B, seed=0)
    assert list(alone.null_means) == draws_sub
    # MUTANTS killed: (i) a fresh pattern per cell breaks (1) and (4);
    # (ii) a pattern drawn in key order over each cell's OWN keys breaks (2)
    # and (4) for the subset; (iii) a per-draw pattern re-seeded per cell
    # breaks (3).


def test_max_null_bar_is_at_least_every_single_cell_percentile():
    family = {
        "spike": {**_vec(2021, range(1, 46), 0.0), (2021, 45): 9.0},
        "flat": _vec(2021, range(1, 46), lambda w: 0.2 if w % 2 else -0.1),
        "wide": _vec(2022, range(1, 46), lambda w: (w % 7) - 3.0),
        "short": _vec(2023, range(1, 12), lambda w: w - 6.0),
    }
    mn = ST.max_null_step_down(family, b=B, seed=3)
    for cid, vec in family.items():
        single = ST.sign_flip_permutation(vec, b=B, seed=3)
        assert mn.bar >= ST.nearest_rank_percentile(single.null_means, 0.95)
        assert mn.observed[cid] == single.mean
        # the family-adjusted p can only be larger than the cell's own p
        assert mn.adjusted_p[cid] >= single.p
    assert mn.bar == ST.nearest_rank_percentile(mn.null_max, 0.95)
    assert mn.alpha == 0.05
    # MUTANT killed: a bar taken from the MEAN over cells (or from any one
    # cell) instead of the per-draw MAX violates the >= for the 'wide' cell.


def test_max_null_clearing_is_strict_and_adjusted_p_counts_the_family_maximum():
    strong = _vec(2021, range(1, 31), 2.0)
    weak = {**_vec(2021, range(1, 31), 0.0), (2021, 30): 1.0}
    mn = ST.max_null_step_down({"strong": strong, "weak": weak}, b=B, seed=0)
    assert mn.observed["strong"] == 2.0 and mn.observed["weak"] == pytest.approx(1 / 30)
    assert "strong" in mn.clearing and "weak" not in mn.clearing
    for cid, obs in mn.observed.items():
        expected = (1 + sum(1 for t in mn.null_max if t >= obs)) / (B + 1)
        assert mn.adjusted_p[cid] == expected
    assert mn.adjusted_p["strong"] == 1 / (B + 1)
    # a cell sitting EXACTLY on the bar does not clear it (the frozen rule is
    # 'exceeds'); build one by a degenerate single-cell family of zeros
    zero = ST.max_null_step_down({"z": _vec(2021, range(1, 5), 0.0)}, b=B)
    assert zero.bar == 0.0 and zero.observed["z"] == 0.0 and zero.clearing == ()


def test_max_null_refuses_an_empty_family_or_an_empty_cell():
    with pytest.raises(ValueError, match="empty family"):
        ST.max_null_step_down({}, b=B)
    with pytest.raises(ValueError, match="empty vector"):
        ST.max_null_step_down({"ok": {(2021, 1): 1.0}, "bad": {}}, b=B)


def test_nearest_rank_percentile_is_the_ceil_q_n_th_smallest():
    values = list(range(100, 0, -1))  # 100..1, unsorted on purpose
    assert ST.nearest_rank_percentile(values, 0.95) == 95
    assert ST.nearest_rank_percentile(values, 1.0) == 100
    assert ST.nearest_rank_percentile([5.0], 0.95) == 5.0
    assert ST.nearest_rank_percentile(list(range(1, 10_001)), 0.95) == 9500
    with pytest.raises(ValueError):
        ST.nearest_rank_percentile([], 0.95)
    # MUTANT killed: math.ceil -> math.floor.  Every case above is an exact
    # multiple (q*n = 95 / 100 / 9500) or below 1 (rescued by max(1, ...)), so
    # the CEIL this test is named for was indistinguishable from floor — while
    # `tune.pool_stats` calls this at q=0.10 on 54 decided weeks (5.4), where
    # the two disagree and the frozen §6.1 pool p10 is the ceil value.
    assert ST.nearest_rank_percentile(list(range(1, 11)), 0.95) == 10      # ceil 9.5
    assert ST.nearest_rank_percentile(list(range(1, 11)), 0.15) == 2       # ceil 1.5
    assert ST.nearest_rank_percentile(list(range(1, 55)), 0.10) == 6       # ceil 5.4
    assert ST.nearest_rank_percentile(list(range(1, 11)), 0.05) == 1       # max(1, ceil 0.5)


# ------------------------------------------------------------ mcnemar_exact


def test_mcnemar_exact_is_the_one_sided_binomial_upper_tail():
    r = ST.mcnemar_exact(14, 11)
    expected = sum(math.comb(25, i) for i in range(14, 26)) / 2 ** 25
    assert r.p == pytest.approx(expected) and r.n == 25
    assert r.p == pytest.approx(0.345, abs=1e-3)
    assert not r.significant_negative
    assert r.p_two == pytest.approx(min(1.0, 2 * expected))
    # b == c: exactly half plus the tie mass -> p > 0.5
    assert ST.mcnemar_exact(5, 5).p > 0.5
    # a lopsided table IS significant, and only in the b > c direction
    neg = ST.mcnemar_exact(12, 2)
    assert neg.p < 0.05 and neg.significant_negative
    pos = ST.mcnemar_exact(2, 12)
    assert pos.p > 0.95 and not pos.significant_negative
    assert pos.p_two == pytest.approx(neg.p_two)
    # MUTANT killed: P(X > b) (strict) gives 0.212 at (14, 11), not 0.345.


def test_mcnemar_with_no_discordant_pairs_is_p_one():
    r = ST.mcnemar_exact(0, 0)
    assert r.p == 1.0 and r.p_two == 1.0 and r.n == 0
    assert not r.significant_negative
    assert ST.mcnemar_exact(0, 3).p == 1.0
    assert ST.mcnemar_exact(3, 0).p == pytest.approx(0.125)
    with pytest.raises(ValueError):
        ST.mcnemar_exact(-1, 2)
