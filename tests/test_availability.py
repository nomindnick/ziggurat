"""Tests for the availability / durability model (ziggurat/core/availability.py).

Three things these tests are built to catch, because they are the three ways this
module could ship wrong and look fine:

1. **A silently durable unknown.** If a rookie, or anyone with no measured
   history, ever prices as MORE available than a proven-durable veteran, the
   model systematically favours the players it knows least about. Several tests
   below exist only to pin that ordering and the disclosure that goes with it.
2. **Independent weekly coin flips.** The marginals of an independent-Bernoulli
   model and of this chain can be made identical while the SEASONS they generate
   are completely different — one six-week absence versus six scattered weeks.
   ``test_absences_arrive_in_blocks_not_scattered_singletons`` fails if anyone
   replaces the chain with flips, even after re-matching the marginals.
3. **A bulk-history read through the wrong as-of view.** Every 2021-2025 row in
   the real database was retrieved in one 2026 backfill, so the safe-default
   ``historical`` view returns NOTHING for a past ``as_of`` — silently. A
   durability model reading that empty result prices every player as never having
   missed a game. The two-view tests pin both halves.
"""

import dataclasses
import inspect
import random
import statistics

import pytest

from ziggurat.core import availability as av

P = av.DEFAULT_DURABILITY
FULL = tuple(range(1, 18))  # 17 fantasy weeks; a club plays 16 of them


# --------------------------------------------------------------- seeding helper


def _seed_world(
    db,
    *,
    seasons,
    weeks,
    teams=("AAA", "BBB"),
    retrieved="2026-07-25",
    appearances=None,
    role_snaps=None,
    injuries=(),
    schedule=None,
    retrieved_schedules=None,
    retrieved_weekly=None,
    retrieved_snaps=None,
    retrieved_injuries=None,
):
    """Plant a tiny, fully-known league.

    ``appearances`` maps (gsis, season) -> {week: (team, position)}; every planted
    appearance writes BOTH a weekly_stats row and a snap_counts row unless the
    position is prefixed ``snap:`` (snaps only, no stat line) or ``stat:`` (stat
    line only). ``role_snaps`` overrides a player's offensive snaps so the top-k
    role gate can be steered. ``injuries`` is (gsis, season, week, status[, team,
    position]).

    ``schedule`` is an explicit [(season, week, home, away)] list, for the fixtures
    that need two clubs with DIFFERENT slates — a helper that gives every club the
    same weeks cannot tell a union from an intersection, which is how the traded
    player's denominator went untested.

    The four ``retrieved_*`` overrides exist so each underlying accessor can be
    gated INDEPENDENTLY. With one shared stamp, hardcoding ``latest_truth`` on any
    single source still leaves the whole file green: every other gate returns
    empty and collapses the composite result to {} either way.
    """
    appearances = appearances or {}
    role_snaps = role_snaps or {}
    sched_stamp = retrieved_schedules or retrieved
    weekly_stamp = retrieved_weekly or retrieved
    snaps_stamp = retrieved_snaps or retrieved
    inj_stamp = retrieved_injuries or retrieved
    home, away = teams[0], teams[1]
    games = schedule or [
        (season, week, home, away) for season in seasons for week in weeks
    ]
    for season, week, home_team, away_team in games:
        gameday = f"{season}-09-{week:02d}"
        db.execute(
            "INSERT INTO schedules (game_id, season, week, game_type, gameday,"
            " away_team, home_team, retrieved_as_of, knowable_as_of)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            # Production stamps a REG schedule row knowable at the PRESEASON
            # anchor (schedules.py), not at kickoff — the whole slate is
            # visible all season. Mirroring that here is what makes the
            # played_only tests mean anything.
            (f"{season}_{week:02d}_{away_team}_{home_team}", season, week, "REG",
             gameday, away_team, home_team, sched_stamp, f"{season}-08-01"),
        )
    for (gsis, season), plan in appearances.items():
        for week, spec in plan.items():
            team, position = spec
            mode, _, position = position.rpartition(":")
            gameday = f"{season}-09-{week:02d}"
            if mode != "snap":
                db.execute(
                    "INSERT INTO weekly_stats (player_id, season, week, season_type,"
                    " position, recent_team, retrieved_as_of, knowable_as_of)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (gsis, season, week, "REG", position, team, weekly_stamp, gameday),
                )
            if mode != "stat":
                db.execute(
                    "INSERT INTO snap_counts (pfr_player_id, gsis_id, position, team,"
                    " season, week, offense_snaps, defense_snaps, st_snaps,"
                    " retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"pfr-{gsis}", gsis, position, team, season, week,
                     role_snaps.get((gsis, season, week), 50.0), 0.0, 0.0,
                     snaps_stamp, gameday),
                )
    for row in injuries:
        gsis, season, week, status = row[:4]
        team = row[4] if len(row) > 4 else None
        position = row[5] if len(row) > 5 else None
        db.execute(
            "INSERT INTO injuries (gsis_id, season, week, team, position,"
            " report_status, retrieved_as_of, knowable_as_of)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (gsis, season, week, team, position, status, inj_stamp,
             f"{season}-09-{week:02d}"),
        )
    db.commit()


def _plan(team, position, weeks):
    return {w: (team, position) for w in weeks}


# ------------------------------------------------------------ chain mathematics


def test_pmf_is_a_distribution_whose_mean_is_the_chain_expectation():
    for position in sorted(P.season_miss_rate):
        opening = P.opening_absent[position]
        onset = av.solve_onset(16, P.season_miss_rate[position] * 16, opening, P)
        pmf = av.games_played_pmf(16, onset, opening, P)
        assert len(pmf) == 17
        assert all(x >= 0.0 for x in pmf)
        assert sum(pmf) == pytest.approx(1.0, abs=1e-12)
        mean_played = sum(i * x for i, x in enumerate(pmf))
        expected = 16 - av.expected_missed(16, onset, opening, P)
        assert mean_played == pytest.approx(expected, abs=1e-9)


def test_solve_onset_hits_any_reachable_target():
    for n in (8, 16, 17, 20):
        for position in sorted(P.season_miss_rate):
            target = P.season_miss_rate[position] * n
            opening = P.opening_absent[position]
            onset = av.solve_onset(n, target, opening, P)
            assert av.expected_missed(n, onset, opening, P) == pytest.approx(
                target, abs=1e-6
            ), (position, n)


def test_solve_onset_refuses_to_fake_an_unreachable_target():
    """Below the opening-absence floor there is no hazard that helps.

    A one-game window already misses ``opening_absent`` games no matter what the
    onset is; driving the hazard to some boundary to "hit" the number would be a
    lie. The honest answer is no onset, and ``player_availability`` then reports
    the ACHIEVED rate and says why it differs.
    """
    # Below the floor: he is already out, so no hazard can miss FEWER games.
    assert av.solve_onset(3, P.season_miss_rate["RB"] * 3, 1.0, P) == 0.0
    # Above the ceiling: over one game the onset has no leverage at all, so the
    # solver saturates rather than pretending. It must stay a probability.
    saturated = av.solve_onset(1, 0.9, 0.12, P)
    assert 0.0 <= saturated <= 1.0
    assert av.expected_missed(1, saturated, 0.12, P) == pytest.approx(0.12)
    # And the row built from it reports what it ACHIEVED, with the reason why.
    opening = P.opening_absent["K"]
    row = av.player_availability("k", "K", (1,))
    assert row.miss_rate == pytest.approx(opening)
    assert row.target_miss_rate == pytest.approx(P.season_miss_rate["K"])
    assert "window this short" in " ".join(row.reasons)


def test_the_onset_hazard_does_not_depend_on_the_window_the_caller_asked_about():
    """A per-game injury hazard is a property of the player, not of the question.

    Solving the onset against the caller's window would make the same player's
    weekly risk change with the length of the window — and, worse, would let an
    ``opening_absent=1.0`` override drive the onset to zero to keep the season
    total on target, so a player known to be hurt came back MORE durable for the
    rest of the year than an average one.
    """
    short = av.player_availability("a", "RB", FULL[:6])
    long = av.player_availability("a", "RB", FULL[:16])
    hurt = av.player_availability("a", "RB", FULL[:16], opening_absent=1.0)
    healthy = av.player_availability("a", "RB", FULL[:16], opening_absent=0.0)
    assert short.onset == pytest.approx(long.onset)
    assert hurt.onset == pytest.approx(long.onset)
    assert hurt.expected_games_missed > long.expected_games_missed > \
        healthy.expected_games_missed
    assert hurt.expected_games_missed > 7.5   # measured opener cohort: 8.46 of 17


def test_steady_state_is_the_right_opening_for_a_mid_season_window():
    steady = av.steady_state_absent("RB")
    # The measured in-season prevalence plateau is ~21-25% absent.
    assert 0.20 < steady < 0.28
    assert steady > P.opening_absent["RB"]
    playoffs = av.player_availability(
        "p", "RB", (15, 16, 17), opening_absent=steady
    )
    assert playoffs.miss_rate == pytest.approx(steady, abs=0.02)
    # The default (season-opener) starting condition would understate it.
    naive = av.player_availability("p", "RB", (15, 16, 17))
    assert naive.miss_rate < playoffs.miss_rate


def test_solve_onset_never_returns_a_negative_or_super_unit_hazard():
    assert av.solve_onset(16, 0.0, 0.0, P) == 0.0
    assert 0.0 <= av.solve_onset(16, 15.9, 0.0, P) <= 1.0
    assert av.solve_onset(16, -1.0, 0.0, P) == 0.0


def test_a_zero_onset_zero_opening_player_plays_every_game():
    path = av.availability_path(16, 0.0, 0.0, P)
    assert path == [1.0] * 16
    pmf = av.games_played_pmf(16, 0.0, 0.0, P)
    assert pmf[16] == pytest.approx(1.0)


def test_availability_path_falls_monotonically_from_the_opener():
    # Prevalence must accumulate: the onset hazard is roughly flat but absences
    # pile up, which is the measured shape (12.7% absent in week 1 -> ~21% by
    # week 10). A path that rises would mean players are healing faster than
    # they break, which the measured hazards do not permit.
    path = av.availability_path(16, 0.07, 0.13, P)
    assert all(b <= a + 1e-12 for a, b in zip(path, path[1:], strict=False))
    assert path[0] == pytest.approx(0.87)


def test_sampler_marginals_and_pmf_match_the_exact_forward_pass():
    row = av.player_availability("mc", "RB", FULL[:16])
    rng = random.Random(20260830)
    trials = 20_000
    played_counts = [0] * 17
    week_hits = dict.fromkeys(row.game_weeks, 0)
    for _ in range(trials):
        draw = av.sample_available_weeks(row, rng)
        assert set(draw) == set(row.game_weeks)
        played = 0
        for week, ok in draw.items():
            if ok:
                week_hits[week] += 1
                played += 1
        played_counts[played] += 1
    for week in row.game_weeks:
        assert week_hits[week] / trials == pytest.approx(
            row.week_available[week], abs=0.02
        ), f"week {week} marginal disagrees with the exact forward pass"
    for g, exact in enumerate(row.games_played_pmf):
        assert played_counts[g] / trials == pytest.approx(exact, abs=0.02)
    mean_played = sum(g * c for g, c in enumerate(played_counts)) / trials
    assert mean_played == pytest.approx(row.expected_games_played, abs=0.12)


def test_absences_arrive_in_blocks_not_scattered_singletons():
    """The load-bearing difference from an independent-Bernoulli model.

    Both models can be made to agree on every weekly marginal; they disagree
    completely on the SHAPE of a season. This compares the chain's sampled
    absence blocks against a Bernoulli sampler matched to the very same weekly
    marginals. If anyone replaces the chain with per-week flips, the chain's mean
    block collapses toward the Bernoulli's and this fails.
    """
    row = av.player_availability("blocks", "RB", FULL[:16])
    rng = random.Random(7)

    def blocks(draw_fn, trials=4000):
        lengths = []
        for _ in range(trials):
            run = 0
            for ok in draw_fn():
                if ok:
                    if run:
                        lengths.append(run)
                    run = 0
                else:
                    run += 1
            if run:
                lengths.append(run)
        return lengths

    chain = blocks(lambda: list(av.sample_available_weeks(row, rng).values()))
    bern = blocks(
        lambda: [rng.random() < row.week_available[w] for w in row.game_weeks]
    )
    assert statistics.mean(chain) > 1.9 * statistics.mean(bern), (
        f"chain blocks {statistics.mean(chain):.2f} vs bernoulli "
        f"{statistics.mean(bern):.2f} — absence is not being modelled as episodes"
    )
    # ...while both agree on total games missed, which is exactly why the
    # marginals alone cannot catch the substitution.
    chain_missed = sum(chain) / 4000
    bern_missed = sum(bern) / 4000
    assert chain_missed == pytest.approx(bern_missed, abs=0.35)


def test_sampler_is_deterministic_and_never_touches_the_global_rng():
    row = av.player_availability("det", "WR", FULL[:16])
    a = av.sample_available_weeks(row, random.Random(11))
    b = av.sample_available_weeks(row, random.Random(11))
    assert a == b
    random.seed(999)
    before = random.random()
    av.sample_available_weeks(row, random.Random(11))
    random.seed(999)
    assert random.random() == before


# ------------------------------------------------------------- the shipped prior


def test_shipped_prior_reproduces_its_own_measured_miss_rates():
    for position, rate in P.season_miss_rate.items():
        row = av.player_availability("x", position, FULL[:P.horizon_games])
        assert row.expected_games_missed == pytest.approx(
            rate * P.horizon_games, abs=1e-6
        ), f"{position}: the chain does not reproduce its own measured rate"


def test_measured_position_ordering_survives():
    # QB is the most contaminated (a benched starter reads as absent) and TE/K
    # the least affected; this ordering is the measurement's headline and a
    # transcription error in the table would invert it.
    r = P.season_miss_rate
    assert r["QB"] > r["RB"] > r["WR"] > r["K"] >= r["TE"]
    assert P.miss_rate("DST") == 0.0


def test_return_hazard_decays_with_the_length_of_the_absence():
    hazard = P.return_hazard
    assert all(b < a for a, b in zip(hazard, hazard[1:], strict=False))
    assert hazard[0] > 0.3 and hazard[-1] < 0.15


def test_the_opening_rung_is_the_one_nearest_the_measured_opener_return():
    """``opening_duration`` is not free: it must be the rung closest to the
    opener cohort's OWN measured P(back for game 2).

    Pinned against ``opening_return`` rather than against a transcribed literal,
    so a re-measurement that moves both stays consistent and a re-measurement
    that moves only one fails here.
    """
    gaps = [abs(h - P.opening_return) for h in P.return_hazard]
    assert gaps.index(min(gaps)) == P.opening_duration - 1
    # ...and it is a real choice, not a tie: the next rung is materially further.
    ordered = sorted(gaps)
    assert ordered[1] > ordered[0] * 1.5


def test_an_unmeasured_position_is_refused_not_defaulted():
    with pytest.raises(av.DurabilityError, match="no durability prior"):
        P.miss_rate("LB")
    with pytest.raises(av.DurabilityError, match="not a league position"):
        av.build_availability([("k", "LB", FULL[:16])])


def test_opening_absence_is_priced_as_a_worse_animal_than_an_in_season_tweak():
    """A player who misses the opener misses ~8 of 16, not ~3."""
    row = av.player_availability("opener", "RB", FULL[:16], opening_absent=1.0)
    assert row.expected_games_missed > 7.5
    healthy = av.player_availability("healthy", "RB", FULL[:16], opening_absent=0.0)
    assert healthy.expected_games_missed < row.expected_games_missed - 4.0


# -------------------------------------------- the unknown-player asymmetry (task)


def test_a_player_with_no_history_is_never_the_most_durable():
    weeks = FULL[:16]
    unknown = av.player_availability("rookie", "RB", weeks)
    durable = av.player_availability(
        "iron", "RB", weeks,
        history=av.PlayerHistory("iron", games=85, missed=0, seasons=(2021, 2025)),
    )
    fragile = av.player_availability(
        "glass", "RB", weeks,
        history=av.PlayerHistory("glass", games=68, missed=34, seasons=(2022, 2025)),
    )
    assert durable.expected_games_played > unknown.expected_games_played
    assert unknown.expected_games_played > fragile.expected_games_played
    assert unknown.basis == av.BASIS_POSITION
    assert unknown.multiplier == 1.0
    # ...and he is priced at EXACTLY the position rate, not better than it.
    assert unknown.miss_rate == pytest.approx(P.season_miss_rate["RB"])


def test_a_sample_below_the_floor_falls_back_and_says_which_floor():
    tiny = av.PlayerHistory("kid", games=5, missed=0, seasons=(2025,))
    row = av.player_availability("kid", "WR", FULL[:16], history=tiny)
    assert row.basis == av.BASIS_POSITION
    assert row.multiplier == 1.0
    assert row.miss_rate == pytest.approx(P.season_miss_rate["WR"])
    blob = " ".join(row.reasons)
    assert "5 prior club games" in blob
    assert f"{P.min_prior_games}-game floor" in blob


def test_the_unknown_players_reasons_refuse_to_read_as_a_clean_bill_of_health():
    row = av.player_availability("rookie", "TE", FULL[:16])
    blob = " ".join(row.reasons)
    assert "POSITION PRIOR ONLY" in blob
    assert "AVERAGE TE" in blob
    assert "never evidence of durability" in blob
    # It must not claim a sample it does not have.
    assert "HIS OWN RECORD" not in blob


def test_every_priceable_row_discloses_the_injury_report_gap():
    for position in ("QB", "RB", "WR", "TE", "K"):
        blob = " ".join(av.player_availability("p", position, FULL[:16]).reasons)
        assert "not from the injury report" in blob
        assert "GAMES NOT PLAYED for any reason" in blob
        assert "hypothesis" in blob and "cohort" in blob
        assert "BLOCKS" in blob


# ------------------------------------------------------------------- shrinkage


def test_the_multiplier_is_relative_to_the_cohorts_own_prior_record():
    """The fix for a measured bias, pinned.

    The cohort's prior-history miss rate sits well below its outcome rate (it is
    selected on having started 8+ games the year before). A player whose record
    IS the cohort's prior-history mean must therefore land on exactly the
    position prior — 1.00x — not below it. Reverting to a shrunk ABSOLUTE rate
    fails here.
    """
    for position, mu in P.prior_history_rate.items():
        games = 1000
        history = av.PlayerHistory("avg", games=games, missed=round(mu * games),
                                   seasons=(2021, 2025))
        mult, basis = av.durability_multiplier(position, history, prior=P)
        assert basis == av.BASIS_PLAYER
        assert mult == pytest.approx(1.0, abs=0.01), position
        assert mu < P.season_miss_rate[position], (
            f"{position}: the prior-history rate must sit BELOW the outcome rate "
            "— that gap is the whole reason the adjustment is a multiplier"
        )
        # What the discarded ABSOLUTE-rate form would have produced for the very
        # same player: a large, uniform optimism. If anyone reverts to it, the
        # assertion above fires; this pins how far wrong it was.
        shrunk = (history.missed + P.shrink_k * mu) / (history.games + P.shrink_k)
        assert shrunk / P.season_miss_rate[position] < 0.8, position


def test_a_worse_record_always_raises_the_multiplier():
    prev = None
    for missed in range(0, 41, 5):
        mult, _ = av.durability_multiplier(
            "RB", av.PlayerHistory("p", games=68, missed=missed, seasons=(2022,)),
            prior=P,
        )
        if prev is not None:
            assert mult > prev
        prev = mult
    # ...and then saturates at the clip rather than running off the evidence.
    worse = av.durability_multiplier(
        "RB", av.PlayerHistory("p", games=68, missed=68, seasons=(2022,)), prior=P)[0]
    assert worse == P.multiplier_clip[1]


def test_more_evidence_at_the_same_rate_moves_the_estimate_further():
    short = av.durability_multiplier(
        "WR", av.PlayerHistory("s", games=17, missed=0, seasons=(2025,)), prior=P)[0]
    long = av.durability_multiplier(
        "WR", av.PlayerHistory("l", games=85, missed=0, seasons=(2021,)), prior=P)[0]
    assert long < short < 1.0
    # ...and one season of a perfect record must NOT be worth a clean bill of
    # health: at k=60 it can move the estimate by at most ~22%.
    assert short > 0.7


def test_the_multiplier_is_clipped_at_the_measured_extremes():
    lo, hi = P.multiplier_clip
    worst = av.durability_multiplier(
        "RB", av.PlayerHistory("w", games=85, missed=85, seasons=(2021,)), prior=P)[0]
    best = av.durability_multiplier(
        "RB", av.PlayerHistory("b", games=400, missed=0, seasons=(2021,)), prior=P)[0]
    assert worst == hi
    assert best == lo
    assert lo < 1.0 < hi


def test_a_team_defense_is_never_discounted_for_availability():
    row = av.player_availability("SF DST", "DST", FULL[:16])
    assert row.basis == av.BASIS_ALWAYS
    assert row.expected_games_played == 16.0
    assert all(v == 1.0 for v in row.week_available.values())
    assert row.games_played_pmf[16] == 1.0
    assert "every week its club plays" in " ".join(row.reasons)
    # A history must not be able to move it — a defense cannot be injured.
    with_hist = av.player_availability(
        "SF DST", "DST", FULL[:16],
        history=av.PlayerHistory("x", games=85, missed=40, seasons=(2021,)),
    )
    assert with_hist.expected_games_played == 16.0


# --------------------------------------------------------------- weeks and byes


def test_game_weeks_must_be_strictly_increasing_and_non_empty():
    with pytest.raises(av.DurabilityError, match="no game weeks"):
        av.player_availability("p", "RB", [])
    with pytest.raises(av.DurabilityError, match="strictly increasing"):
        av.player_availability("p", "RB", [3, 1, 2])
    with pytest.raises(av.DurabilityError, match="strictly increasing"):
        av.player_availability("p", "RB", [1, 1, 2])


def test_a_bye_week_is_absent_from_the_slate_and_never_available():
    weeks = tuple(w for w in FULL if w != 9)
    row = av.player_availability("bye", "WR", weeks)
    assert 9 not in row.week_available
    assert row.p_available(9) == 0.0
    assert row.p_available(1) > 0.0
    assert len(row.games_played_pmf) == len(weeks) + 1


def test_p_plays_at_least_is_a_survival_function():
    row = av.player_availability("s", "RB", FULL[:16])
    assert row.p_plays_at_least(0) == pytest.approx(1.0, abs=1e-9)
    assert row.p_plays_at_least(17) == 0.0
    vals = [row.p_plays_at_least(g) for g in range(0, 17)]
    assert all(b <= a + 1e-12 for a, b in zip(vals, vals[1:], strict=False))


# ------------------------------------------------------------------ the DB layer


#: Every function in this module that reads the database. No waivers: Rule 1 says
#: keyword-only ``as_of`` with no default, and one exemption is how a rule dies.
DB_ACCESSORS = (
    "team_game_weeks", "player_seasons", "player_histories", "season_panel",
    "measure_durability", "load_player_histories", "load_durability_book",
    "fit_player_adjustment", "_roster_evidence",
)


def test_as_of_is_keyword_only_with_no_default_on_every_accessor():
    for name in DB_ACCESSORS:
        sig = inspect.signature(getattr(av, name))
        as_of = sig.parameters["as_of"]
        assert as_of.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert as_of.default is inspect.Parameter.empty, name


def test_no_db_reader_in_the_module_is_missing_from_that_list():
    """The list above is only a rule if nothing can be added outside it.

    Any module-level function taking a ``conn`` first argument is a database
    reader and must be enumerated, so a new accessor cannot slip in with a
    defaulted ``as_of``.
    """
    found = set()
    for name, fn in vars(av).items():
        if not inspect.isfunction(fn) or fn.__module__ != av.__name__:
            continue
        params = list(inspect.signature(fn).parameters)
        if params[:1] == ["conn"]:
            found.add(name)
    assert found == set(DB_ACCESSORS), f"unlisted DB readers: {found ^ set(DB_ACCESSORS)}"


def test_view_defaults_to_historical_on_every_db_accessor():
    for name in DB_ACCESSORS:
        params = inspect.signature(getattr(av, name)).parameters
        if "view" not in params:      # load_player_histories BINDS latest_truth
            assert name == "load_player_histories", name
            continue
        assert params["view"].default == "historical", name


def test_a_missed_game_is_measured_from_presence_not_the_injury_report(db):
    # He played weeks 1 and 3, missed week 2, and the injury report says NOTHING
    # about it — which is the case for ~66% of real missed weeks.
    _seed_world(
        db, seasons=(2024,), weeks=(1, 2, 3),
        appearances={("g1", 2024): _plan("AAA", "RB", (1, 3))},
    )
    seasons = av.player_seasons(db, as_of="2026-08-01", season=2024)
    ps = seasons["g1"]
    assert ps.game_weeks == (1, 2, 3)
    assert ps.played_weeks == (1, 3)
    assert ps.missed == 1
    assert ps.position == "RB"


def test_a_snap_row_with_no_stat_line_still_counts_as_playing(db):
    # A receiver who ran routes and drew no targets has snaps and no stat row.
    # Counting him absent would manufacture missed games out of a quiet game.
    _seed_world(
        db, seasons=(2024,), weeks=(1, 2),
        appearances={("g2", 2024): {1: ("AAA", "stat:WR"), 2: ("AAA", "snap:WR")}},
    )
    ps = av.player_seasons(db, as_of="2026-08-01", season=2024)["g2"]
    assert ps.played_weeks == (1, 2)
    assert ps.missed == 0


def test_a_zero_snap_row_is_not_an_appearance(db):
    _seed_world(
        db, seasons=(2024,), weeks=(1, 2),
        appearances={("g3", 2024): {1: ("AAA", "RB"), 2: ("AAA", "snap:RB")}},
        role_snaps={("g3", 2024, 2): 0.0},
    )
    ps = av.player_seasons(db, as_of="2026-08-01", season=2024)["g3"]
    # Week 2's snap row carries no snaps at all: he dressed for nothing.
    assert ps.played_weeks == (1,)
    assert ps.missed == 1


def test_role_weeks_rank_within_the_club_and_position(db):
    # Two RBs on one club; ROLE_DEPTH["RB"] is 2, so a third would be excluded.
    _seed_world(
        db, seasons=(2024,), weeks=(1, 2),
        appearances={
            ("rb1", 2024): _plan("AAA", "RB", (1, 2)),
            ("rb2", 2024): _plan("AAA", "RB", (1, 2)),
            ("rb3", 2024): _plan("AAA", "RB", (1, 2)),
        },
        role_snaps={("rb1", 2024, 1): 60.0, ("rb1", 2024, 2): 60.0,
                    ("rb2", 2024, 1): 30.0, ("rb2", 2024, 2): 30.0,
                    ("rb3", 2024, 1): 5.0, ("rb3", 2024, 2): 5.0},
    )
    seasons = av.player_seasons(db, as_of="2026-08-01", season=2024)
    assert seasons["rb1"].role_weeks == 2
    assert seasons["rb2"].role_weeks == 2
    assert seasons["rb3"].role_weeks == 0


def test_the_cohort_gate_reads_only_the_previous_season(db):
    # `starter` holds the role in 2023 and so is a 2024 cohort row. `late` holds
    # it only in 2024, which cannot make him a 2024 cohort row — that gate would
    # be selecting on the outcome it is about to measure.
    weeks = tuple(range(1, 10))
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks,
        appearances={
            ("starter", 2023): _plan("AAA", "RB", weeks),
            ("starter", 2024): _plan("AAA", "RB", weeks[:5]),
            ("late", 2024): _plan("BBB", "RB", weeks),
        },
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2023, last_season=2024)
    cohort = av.cohort_seasons(panel, min_role_weeks=8)
    assert [ps.gsis_id for ps in cohort] == ["starter"]
    assert cohort[0].season == 2024
    assert cohort[0].missed == 4


def test_measure_durability_recovers_a_planted_miss_rate(db):
    weeks = tuple(range(1, 11))
    # Two RBs per club: ROLE_DEPTH["RB"] is 2, so a third on the same club would
    # never hold the role and never enter the cohort.
    clubs = {0: "AAA", 1: "AAA", 2: "BBB", 3: "BBB"}
    plans = {(f"s{i}", 2023): _plan(clubs[i], "RB", weeks) for i in range(4)}
    # In 2024, two play everything and two miss half.
    plans[("s0", 2024)] = _plan("AAA", "RB", weeks)
    plans[("s2", 2024)] = _plan("BBB", "RB", weeks)
    plans[("s1", 2024)] = _plan("AAA", "RB", weeks[:5])
    plans[("s3", 2024)] = _plan("BBB", "RB", weeks[:5])
    _seed_world(db, seasons=(2023, 2024), weeks=weeks, appearances=plans)
    m = av.measure_durability(
        db, as_of="2026-08-01", first_season=2023, last_season=2024,
        horizon_games=10, min_role_weeks=8,
    )
    assert m.cohort_n["RB"] == 4
    assert m.season_miss_rate["RB"] == pytest.approx(0.25)   # 10 of 40
    assert m.mean_games_missed["RB"] == pytest.approx(2.5)
    assert m.prior_history_rate["RB"] == pytest.approx(0.0)  # all four were perfect
    assert m.opening_absent["RB"] == pytest.approx(0.0)


def test_measure_durability_reports_the_injury_report_gap_it_finds(db):
    weeks = tuple(range(1, 11))
    plans = {
        ("a", 2023): _plan("AAA", "RB", weeks),
        ("b", 2023): _plan("BBB", "RB", weeks),
        ("a", 2024): _plan("AAA", "RB", weeks[:8]),   # misses weeks 9, 10
        ("b", 2024): _plan("BBB", "RB", weeks[:8]),   # misses weeks 9, 10
    }
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks, appearances=plans,
        # Only ONE of the four missed weeks carries a designation, exactly the
        # shape of the real gap.
        injuries=[("a", 2024, 9, "Out")],
    )
    m = av.measure_durability(
        db, as_of="2026-08-01", first_season=2023, last_season=2024,
        horizon_games=10, min_role_weeks=8,
    )
    assert m.presence_mean_missed["RB"] == pytest.approx(2.0)
    assert m.injury_only_mean_missed["RB"] == pytest.approx(0.5)
    assert m.injury_report_coverage["RB"] == pytest.approx(0.25)


def test_measurement_freezes_into_a_usable_prior(db):
    weeks = tuple(range(1, 11))
    plans = {}
    for i in range(6):
        club = "AAA" if i % 2 == 0 else "BBB"   # ROLE_DEPTH["WR"] is 3 per club
        plans[(f"p{i}", 2023)] = _plan(club, "WR", weeks)
        plans[(f"p{i}", 2024)] = _plan(club, "WR", weeks if i < 3 else weeks[:6])
    _seed_world(db, seasons=(2023, 2024), weeks=weeks, appearances=plans)
    m = av.measure_durability(
        db, as_of="2026-08-01", first_season=2023, last_season=2024,
        horizon_games=10, min_role_weeks=8,
    )
    prior = m.as_prior(label="hypothesis: synthetic", cohort="cohort: synthetic",
                       source="test")
    row = av.player_availability("p", "WR", tuple(range(1, 11)), prior=prior)
    assert row.expected_games_missed == pytest.approx(
        prior.season_miss_rate["WR"] * 10, abs=1e-6
    )


# ------------------------------------------------------------ Rule 1 / leakage


def test_historical_view_hides_bulk_backfilled_history_and_latest_truth_reveals_it(db):
    """The footgun ``load_player_histories`` exists to close.

    Every 2021-2025 row in the real database was retrieved in one 2026 backfill.
    Under the safe-default ``historical`` view a 2024 read returns NOTHING — and
    a durability model reading that empty result would price every player as
    never having missed a game, silently.
    """
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2024,), weeks=weeks, retrieved="2026-07-25",
        appearances={("g", 2024): _plan("AAA", "RB", (1, 3))},
    )
    hidden = av.player_histories(
        db, as_of="2024-12-31", first_season=2024, last_season=2024
    )
    assert hidden == {}, "historical must NOT serve a future-retrieved backfill"
    seen = av.load_player_histories(
        db, as_of="2024-12-31", first_season=2024, last_season=2024
    )
    assert seen["g"].games == 3
    assert seen["g"].missed == 1
    # And the pricing consequence: reading the hidden view is not "he is durable",
    # it is "we know nothing", which must show as POSITION_PRIOR.
    blind = av.player_availability("g", "RB", FULL[:16], history=hidden.get("g"))
    assert blind.basis == av.BASIS_POSITION


def test_latest_truth_still_refuses_a_game_that_has_not_been_played(db):
    """``latest_truth`` relaxes RETRIEVAL time, never FACT time."""
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2024,), weeks=weeks, retrieved="2026-07-25",
        appearances={("g", 2024): _plan("AAA", "RB", weeks)},
    )
    # Week 3's gameday is 2024-09-03; a read on 09-02 must not see it.
    mid = av.load_player_histories(
        db, as_of="2024-09-02", first_season=2024, last_season=2024
    )
    assert mid["g"].games == 2 and mid["g"].missed == 0
    full = av.load_player_histories(
        db, as_of="2024-12-31", first_season=2024, last_season=2024
    )
    assert full["g"].games == 3


def test_the_schedule_gate_moves_with_as_of_so_a_future_week_is_not_a_missed_game(db):
    """The nastiest possible leakage bug here would be an INVERTED one: if the
    slate were read at a later ``as_of`` than the appearances, every not-yet-played
    week would count as a missed game and mid-season durability would collapse."""
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2024,), weeks=weeks, retrieved="2026-07-25",
        appearances={("g", 2024): _plan("AAA", "RB", weeks)},
    )
    early = av.base.latest_truth(av.player_seasons)(
        db, as_of="2024-09-01", season=2024
    )["g"]
    assert early.game_weeks == (1,)
    assert early.missed == 0


def test_player_histories_refuses_an_inverted_season_range(db):
    with pytest.raises(av.DurabilityError, match="precedes"):
        av.player_histories(db, as_of="2026-08-01", first_season=2025, last_season=2021)


# ------------------------------------------------------------------- the book


def _book(db):
    weeks = tuple(range(1, 5))
    _seed_world(
        db, seasons=(2024, 2026), weeks=weeks, retrieved="2026-07-25",
        appearances={("g", 2024): _plan("AAA", "RB", (1, 2, 3))},
    )
    # AAA plays every week; BBB is the away side of the same games, so both have
    # the full slate. Give AAA a bye by deleting one game and re-adding it as a
    # different pairing would over-complicate the fixture; instead assert on the
    # slate the schedule actually declares.
    return av.load_durability_book(
        db, as_of="2026-08-01", season=2026,
        first_history_season=2024, last_history_season=2024,
        # A two-club fixture is deliberately below the collapse floors; they are
        # exercised for real in the collapse tests below.
        min_histories=0, min_clubs=0,
    )


def test_the_book_prices_a_player_off_his_clubs_real_slate(db):
    book = _book(db)
    assert book.slate["AAA"] == (1, 2, 3, 4)
    row = book.availability_for("g", "RB", "AAA", range(1, 6), gsis_id="g")
    assert row.game_weeks == (1, 2, 3, 4)
    assert row.basis == av.BASIS_POSITION  # 4 prior games, below the 8-game floor
    assert row.sample_games == 4
    assert row.sample_missed == 1


def test_the_book_refuses_a_player_with_no_club_rather_than_assuming_he_plays(db):
    book = _book(db)
    with pytest.raises(av.DurabilityError, match="cannot tell his bye week"):
        book.availability_for("g", "RB", None, range(1, 6))
    with pytest.raises(av.DurabilityError, match="no 2026 schedule"):  # noqa: E501
        book.availability_for("g", "RB", "ZZZ", range(1, 6))


def test_the_book_trims_the_slate_to_the_requested_window(db):
    book = _book(db)
    row = book.availability_for("g", "RB", "AAA", (2, 3), gsis_id="g")
    assert row.game_weeks == (2, 3)


def test_build_availability_maps_board_keys_to_gsis_history(db):
    history = {"gsis-1": av.PlayerHistory("gsis-1", games=85, missed=0,
                                          seasons=(2021, 2025))}
    rows = av.build_availability(
        [("espn:1", "RB", FULL[:16]), ("espn:2", "RB", FULL[:16])],
        histories=history, history_keys={"espn:1": "gsis-1"},
    )
    assert rows["espn:1"].basis == av.BASIS_PLAYER
    assert rows["espn:2"].basis == av.BASIS_POSITION
    assert rows["espn:1"].expected_games_played > rows["espn:2"].expected_games_played


def test_build_availability_canonicalizes_the_position(db):
    rows = av.build_availability([("dst", "D/ST", FULL[:16]), ("k", "PK", FULL[:16])])
    assert rows["dst"].position == "DST"
    assert rows["k"].position == "K"


# ------------------------------------------------------------ adjustment fitting


def test_fit_player_adjustment_recovers_a_planted_signal(db):
    """Plant two durability classes and check the fit sees them.

    Half the players are perfect in every season; half miss half of every season.
    A working fit must (a) find a positive year-over-year correlation and (b) beat
    the position-only baseline.
    """
    weeks = tuple(range(1, 11))
    plans = {}
    for i in range(6):
        fragile = i % 2 == 1
        club = "AAA" if i < 3 else "BBB"   # ROLE_DEPTH["WR"] is 3 per club
        for season in (2022, 2023, 2024):
            played = weeks[:5] if fragile else weeks
            plans[(f"p{i}", season)] = _plan(club, "WR", played)
    _seed_world(db, seasons=(2022, 2023, 2024), weeks=weeks, appearances=plans)
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2022, last_season=2024)
    fit = av.fit_player_adjustment_from_panel(
        panel, horizon_games=10, min_role_weeks=5
    )
    assert fit.n > 0
    assert fit.yoy_correlation > 0.9
    assert fit.best_mse < fit.position_only_mse
    assert fit.mse_skill_vs_position > 0.5
    assert fit.mean_predicted == pytest.approx(fit.mean_actual, abs=0.02)


def test_the_fit_splits_its_db_and_pure_entry_points_so_as_of_needs_no_default():
    """Rule 1 has no waivers, so neither does this module.

    ``fit_player_adjustment`` used to be one function serving both the read-the-DB
    and reuse-a-panel calls, which forced ``as_of=None`` and made it the ONLY
    accessor in the package whose ``as_of`` carried a default. A rule with one
    waiver in it is a rule people learn to waive.
    """
    sig = inspect.signature(av.fit_player_adjustment)
    as_of = sig.parameters["as_of"]
    assert as_of.kind is inspect.Parameter.KEYWORD_ONLY
    assert as_of.default is inspect.Parameter.empty
    for name in ("first_season", "last_season"):
        assert sig.parameters[name].default is inspect.Parameter.empty
    # The pure entry point reads nothing, so it must not pretend to take an
    # as-of at all — an optional one is exactly the shape that came back wrong.
    assert "as_of" not in inspect.signature(av.fit_player_adjustment_from_panel).parameters
    with pytest.raises(av.DurabilityError, match="non-empty panel"):
        av.fit_player_adjustment_from_panel({})


def test_the_played_only_gate_is_what_stops_a_future_week_becoming_a_missed_game(db):
    """Mutation guard for the nastiest defect this module can have.

    A REG schedule row is knowable from the preseason anchor, so at ANY in-season
    ``as_of`` the full slate is readable. If ``player_seasons`` used that slate as
    its denominator, every not-yet-played week would count as a game the player
    missed. This pins both halves: the forward slate really is fully visible, and
    the played-only slate really is not.
    """
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2024,), weeks=weeks, retrieved="2026-07-25",
        appearances={("g", 2024): _plan("AAA", "RB", weeks)},
    )
    # latest_truth throughout: this is bulk-backfilled history, which the
    # safe-default view correctly hides at a 2024 as_of.
    slate = av.base.latest_truth(av.team_game_weeks)
    forward = slate(db, as_of="2024-09-01", season=2024)
    played = slate(db, as_of="2024-09-01", season=2024, played_only=True)
    assert forward["AAA"] == (1, 2, 3), "the forward slate must show the whole season"
    assert played["AAA"] == (1,), "the played slate must stop at as_of"
    ps = av.base.latest_truth(av.player_seasons)(
        db, as_of="2024-09-01", season=2024
    )["g"]
    assert ps.missed == 0, (
        "a week that has not been played is not a week he missed"
    )


def test_role_ranking_breaks_snap_ties_deterministically(db):
    # Three RBs on one club with IDENTICAL snaps: ROLE_DEPTH picks two, and which
    # two must not depend on the order sqlite happened to return the rows in.
    _seed_world(
        db, seasons=(2024,), weeks=(1, 2),
        appearances={
            ("z_rb", 2024): _plan("AAA", "RB", (1, 2)),
            ("a_rb", 2024): _plan("AAA", "RB", (1, 2)),
            ("m_rb", 2024): _plan("AAA", "RB", (1, 2)),
        },
    )
    first = av.player_seasons(db, as_of="2026-08-01", season=2024)
    second = av.player_seasons(db, as_of="2026-08-01", season=2024)
    assert {k: v.role_weeks for k, v in first.items()} == \
        {k: v.role_weeks for k, v in second.items()}
    assert sorted(k for k, v in first.items() if v.role_weeks == 2) == ["a_rb", "m_rb"]


def test_a_traded_player_is_priced_against_the_union_of_his_clubs_weeks(db):
    """The two clubs must have DIFFERENT slates or this test proves nothing.

    Given both clubs the same weeks (the shape this fixture used to have), the
    union, the intersection and "first club only" are the same set, and a rewrite
    of the union rule ships green. On the live 2021-2025 panel 55 of 1,017 cohort
    rows have more than one club and 49 of those carry an 18-week union, so the
    rule is load-bearing: the intersection would erase real games and the
    first-club-only reading would erase the rest of his season.

    AAA plays weeks 1-3 (bye in 4); BBB plays 2-4 (bye in 1). He appeared in week
    1 for AAA and week 4 for BBB. Union = (1,2,3,4), 4 games, 2 missed;
    intersection = (2,3); AAA only = (1,2,3); BBB only = (2,3,4). All four are
    distinguishable.
    """
    _seed_world(
        db, seasons=(2024,), weeks=(),
        schedule=[(2024, 1, "AAA", "CCC"), (2024, 2, "AAA", "BBB"),
                  (2024, 3, "AAA", "BBB"), (2024, 4, "BBB", "DDD")],
        appearances={("t", 2024): {1: ("AAA", "WR"), 4: ("BBB", "WR")}},
    )
    ps = av.player_seasons(db, as_of="2026-08-01", season=2024)["t"]
    assert ps.teams == ("AAA", "BBB")
    assert ps.game_weeks == (1, 2, 3, 4), "the UNION of both clubs' weeks"
    assert ps.played_weeks == (1, 4)
    assert ps.missed == 2


def test_a_player_whose_club_has_no_schedule_is_dropped_not_credited(db):
    # Appearances stamped to a club with no games at this as_of cannot produce a
    # denominator; silently crediting him with a perfect record would be the
    # unknown-reads-as-durable failure in a different costume.
    _seed_world(db, seasons=(2024,), weeks=(1, 2), appearances={})
    db.execute(
        "INSERT INTO weekly_stats (player_id, season, week, season_type, position,"
        " recent_team, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?)",
        ("ghost", 2024, 1, "REG", "RB", "ZZZ", "2026-07-25", "2024-09-01"),
    )
    db.commit()
    assert "ghost" not in av.player_seasons(db, as_of="2026-08-01", season=2024)


def test_the_prior_is_frozen_and_cannot_be_mutated_by_a_consumer():
    # A grader holding the book must not be able to edit the shipped hypothesis
    # out from under everyone else in the process.
    with pytest.raises(TypeError):
        P.season_miss_rate["RB"] = 0.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        P.shrink_k = 1.0
    row = av.player_availability("f", "RB", FULL[:16])
    with pytest.raises(TypeError):
        row.week_available[1] = 0.0


def test_the_sampler_walks_the_same_chain_as_the_row_it_was_given(db):
    """A row built from a re-measured prior must be SAMPLED from that prior.

    Reaching for the module default inside the sampler would produce draws that
    silently disagree with the marginals printed beside them — the kind of defect
    that only shows up as a grader whose Monte Carlo and whose reported
    expectations never quite line up.
    """
    weeks = tuple(range(1, 11))
    plans = {}
    for i in range(6):
        club = "AAA" if i % 2 == 0 else "BBB"
        plans[(f"p{i}", 2023)] = _plan(club, "WR", weeks)
        plans[(f"p{i}", 2024)] = _plan(club, "WR", weeks[:2])   # brutally fragile
    _seed_world(db, seasons=(2023, 2024), weeks=weeks, appearances=plans)
    m = av.measure_durability(
        db, as_of="2026-08-01", first_season=2023, last_season=2024,
        horizon_games=10, min_role_weeks=8,
    )
    harsh = m.as_prior(label="hypothesis: synthetic", cohort="cohort: synthetic",
                       source="test")
    assert harsh.season_miss_rate["WR"] > P.season_miss_rate["WR"] * 2
    row = av.player_availability("p", "WR", weeks, prior=harsh)
    assert row.prior is harsh
    rng = random.Random(3)
    trials = 4000
    played = sum(
        sum(av.sample_available_weeks(row, rng).values()) for _ in range(trials)
    ) / trials
    assert played == pytest.approx(row.expected_games_played, abs=0.25)
    # ...and materially fewer games than the shipped prior would have drawn.
    default_row = av.player_availability("p", "WR", weeks)
    assert played < default_row.expected_games_played - 1.5


def test_the_onset_memo_cannot_serve_one_priors_answer_to_another():
    """The cache key is the CHAIN, not the prior object.

    Keying on ``id(prior)`` would be reused after a garbage collection and could
    hand a re-measured prior the shipped prior's onset — a wrong answer that
    would be bit-stable and therefore invisible.
    """
    slow = dataclasses.replace(P, return_hazard=(0.05,) * len(P.return_hazard))
    a = av.solve_onset(16, 3.0, 0.13, P)
    b = av.solve_onset(16, 3.0, 0.13, slow)
    assert a != b, "a different return hazard must give a different onset"
    # A slower return means fewer onsets are needed to miss the same games.
    assert b < a
    assert av.expected_missed(16, b, 0.13, slow) == pytest.approx(3.0, abs=1e-6)
    assert av.expected_missed(16, a, 0.13, P) == pytest.approx(3.0, abs=1e-6)


# ==================================================================== the season
# ==================================== a player missed IN FULL (the headline bug)


def test_a_season_missed_in_full_is_counted_not_deleted(db):
    """The defect that inverted the sign of the strongest signal in the model.

    Keying the record on "seasons he appeared in" drops a wholly-missed season
    from BOTH halves of the fraction, so the most fragile players read as the most
    durable. Measured on the live board before the fix: a kicker with a lost 2025
    came out 0 of 67 missed, 0.47x, FIFTH most durable of 250 priced rows.

    Here: he plays all of 2023, misses all of 2024 (the injury report proves the
    roster spot), plays all of 2025. The appearances-only reading calls him
    perfect; the correct one calls him a third absent.
    """
    weeks = tuple(range(1, 18))
    plans = {("k1", 2023): _plan("AAA", "K", weeks),
             ("k1", 2025): _plan("AAA", "K", weeks)}
    _seed_world(
        db, seasons=(2023, 2024, 2025), weeks=weeks, appearances=plans,
        injuries=[("k1", 2024, 5, "Out", "AAA", "K")],
    )
    seasons = av.player_seasons(db, as_of="2026-08-01", season=2024)
    assert "k1" in seasons, "a season he missed in full must still be a row"
    lost = seasons["k1"]
    assert lost.roster_basis == av.SEASON_ROSTER_EVIDENCE
    assert lost.played_weeks == ()
    assert lost.missed == 17 and lost.games == 17
    assert lost.role_weeks == 0

    full = av.player_histories(db, as_of="2026-08-01",
                               first_season=2023, last_season=2025)["k1"]
    assert (full.games, full.missed) == (51, 17)
    assert full.seasons == (2023, 2024, 2025)
    assert full.absent_seasons == (2024,)

    # ...and the appearances-only reading is the bug, reproduced on demand.
    blind_panel = {
        s: av.player_seasons(db, as_of="2026-08-01", season=s, roster_evidence=False)
        for s in (2023, 2024, 2025)
    }
    # Bracketing would ALSO catch this one, so turn both rules off to see the
    # original defect: 51 club games, nothing missed, a perfect record.
    blind = av.histories_from_panel(blind_panel)["k1"]
    assert (blind.games, blind.missed) == (34, 0)

    # The pricing consequence, which is the whole point: counting the lost season
    # makes him WORSE than an average kicker; deleting it made him better.
    good = av.durability_multiplier("K", full)[0]
    bad = av.durability_multiplier("K", blind)[0]
    assert good > 1.0 > bad, (good, bad)


def test_a_season_lost_with_no_injury_row_is_caught_by_the_bracket(db):
    """The rule that exists because the evidence goes missing where it hurts.

    This module's own headline finding is that a player on IR drops off the injury
    report entirely — the receiver who lost 2022 to an ACL and 2023 to an Achilles
    has NO injury row in either season. Bracketing catches him: he appeared before
    and after, and played nothing in between.
    """
    weeks = tuple(range(1, 18))
    _seed_world(
        db, seasons=(2021, 2022, 2023, 2024), weeks=weeks,
        appearances={("w1", 2021): _plan("AAA", "WR", weeks),
                     ("w1", 2024): _plan("AAA", "WR", weeks)},
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2021, last_season=2024)
    for season in (2022, 2023):
        row = panel[season]["w1"]
        assert row.roster_basis == av.SEASON_BRACKETED
        assert row.played_weeks == () and row.games == 17
        assert row.position == "WR", "the absent row must carry his real position"
        assert row.teams == ("AAA",)
    history = av.histories_from_panel(panel)["w1"]
    assert (history.games, history.missed) == (68, 34)
    assert history.absent_seasons == (2022, 2023)
    # Without the rule he reads as a 34-of-34 iron man, i.e. MORE durable than
    # average, which is exactly backwards.
    assert av.durability_multiplier("WR", history)[0] > 1.5


def test_a_season_before_his_first_appearance_is_never_bracketed(db):
    """A rookie must not be charged for the years he was in college.

    The bracket rule is the one that could reach back, so it is the one pinned:
    outside the span of his appearances, "on a roster and never played" and "not
    in the NFL" are the same empty row.
    """
    weeks = tuple(range(1, 18))
    _seed_world(
        db, seasons=(2021, 2022, 2023), weeks=weeks,
        appearances={("rook", 2023): _plan("AAA", "RB", weeks),
                     ("vet", 2021): _plan("AAA", "RB", weeks),
                     ("vet", 2023): _plan("AAA", "RB", weeks)},
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2021, last_season=2023)
    assert "rook" not in panel[2021] and "rook" not in panel[2022]
    assert panel[2022]["vet"].roster_basis == av.SEASON_BRACKETED
    rook = av.histories_from_panel(panel)["rook"]
    assert rook.seasons == (2023,) and rook.absent_seasons == ()


def test_a_season_after_his_last_appearance_needs_evidence_not_a_guess(db):
    """Retirement is not a missed season; a proven roster spot is.

    Both players stop appearing after 2023. One has a 2024 injury row naming his
    club — he was there and did not play. The other has nothing, and is not
    charged for a season he may simply not have been in the league for.
    """
    weeks = tuple(range(1, 18))
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks,
        appearances={("stayed", 2023): _plan("AAA", "RB", weeks),
                     ("retired", 2023): _plan("AAA", "RB", weeks)},
        injuries=[("stayed", 2024, 3, "Out", "AAA", "RB")],
        role_snaps={("retired", 2023, w): 10.0 for w in weeks},
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2023, last_season=2024)
    assert panel[2024]["stayed"].roster_basis == av.SEASON_ROSTER_EVIDENCE
    assert "retired" not in panel[2024]
    hist = av.histories_from_panel(panel)
    assert hist["stayed"].absent_seasons == (2024,)
    assert hist["retired"].absent_seasons == ()


def test_roster_evidence_ignores_the_designation_and_needs_a_club(db):
    """It asks "was he in the league", not "was he hurt" — but it needs a club.

    A row with no team gives no denominator, and an absence with no denominator
    is not a fact (the tombstone rule this repo already pays for elsewhere).
    """
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks,
        appearances={("a", 2023): _plan("AAA", "RB", weeks),
                     ("b", 2023): _plan("AAA", "RB", weeks)},
        injuries=[("a", 2024, 1, "Questionable", "AAA", "RB"),   # not "Out"
                  ("b", 2024, 1, "Out", None, "RB")],            # no club
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2023, last_season=2024)
    assert panel[2024]["a"].roster_basis == av.SEASON_ROSTER_EVIDENCE
    assert "b" not in panel[2024]


def test_an_absent_season_reaches_the_cohort_and_moves_the_measured_rate(db):
    """The same deletion also biased every POSITION rate downward.

    A cohort row is a season-Y outcome for a player who started in Y-1. If a
    wholly-missed Y is not a row, the worst outcomes are missing from the
    measurement — measured on the live panel, RB's rate moves 0.2099 -> 0.2226 on
    four such rows alone.
    """
    weeks = tuple(range(1, 18))
    plans = {}
    for i in range(3):                      # ROLE_DEPTH["TE"] is 1, so one per club
        for season in (2023,):
            plans[(f"te{i}", season)] = _plan(f"C{i}", "TE", weeks)
    plans[("te0", 2024)] = _plan("C0", "TE", weeks)
    plans[("te1", 2024)] = _plan("C1", "TE", weeks)
    schedule = [(s, w, f"C{i}", "ZZZ") for s in (2023, 2024)
                for w in weeks for i in range(3)]
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks, schedule=schedule, appearances=plans,
        injuries=[("te2", 2024, 4, "Out", "C2", "TE")],
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2023, last_season=2024)
    cohort = av.cohort_seasons(panel)
    assert {ps.gsis_id for ps in cohort} == {"te0", "te1", "te2"}
    lost = next(ps for ps in cohort if ps.gsis_id == "te2")
    assert lost.horizon(16) == (16, 16)
    m = av.measure_durability(db, as_of="2026-08-01", first_season=2023,
                              last_season=2024, panel=panel)
    # Two perfect seasons and one wholly missed: 16 of 48 club games.
    assert m.season_miss_rate["TE"] == pytest.approx(16 / 48)
    assert m.cohort_n["TE"] == 3


def test_the_reasons_name_a_season_missed_in_full(db):
    weeks = tuple(range(1, 18))
    _seed_world(
        db, seasons=(2023, 2024, 2025), weeks=weeks,
        appearances={("w", 2023): _plan("AAA", "WR", weeks),
                     ("w", 2025): _plan("AAA", "WR", weeks)},
    )
    history = av.player_histories(db, as_of="2026-08-01",
                                  first_season=2023, last_season=2025)["w"]
    row = av.player_availability("w", "WR", FULL[:16], history=history,
                                 history_window=(2023, 2025))
    blob = " ".join(row.reasons)
    assert "missed in full (2024)" in blob
    assert "an appearances-only record would have deleted them" in blob
    assert row.absent_seasons == (2024,)


def test_the_season_span_shows_a_gap_instead_of_papering_over_it():
    """min-max was a lie with arithmetic attached.

    A (2021, 2023, 2024, 2025) record printed "across 2021-2025" — five seasons,
    ~85 club games — next to a 68-game denominator, and the missing years were
    missing precisely because he was hurt for all of them.
    """
    assert av._season_span((2021, 2023, 2024, 2025)) == "2021, 2023-2025"
    assert av._season_span((2021, 2022, 2023)) == "2021-2023"
    assert av._season_span((2024,)) == "2024"
    assert av._season_span(()) == "no measured season"
    history = av.PlayerHistory("g", games=68, missed=22,
                               seasons=(2021, 2023, 2024, 2025), role_weeks=60)
    reasons = av.player_availability("g", "WR", FULL[:16], history=history).reasons
    record = next(r for r in reasons if r.startswith("Basis: HIS OWN RECORD"))
    assert "2021, 2023-2025" in record
    assert "2021-2025" not in record, "a contiguous span would hide the gap"


def test_a_gap_nothing_can_price_is_reported_rather_than_counted_as_healthy(db):
    """The one direction this module must never fail silently in.

    He appeared in 2021 and is provably on a roster in 2023, so 2022 is a hole
    between two seasons we KNOW he was in the league — but nothing appeared in it
    and nothing proves the roster spot, so it is neither charged nor swallowed.
    """
    weeks = tuple(range(1, 18))
    _seed_world(
        db, seasons=(2021, 2022, 2023), weeks=weeks,
        appearances={("g", 2021): _plan("AAA", "WR", weeks)},
        injuries=[("g", 2023, 2, "Out", "AAA", "WR")],
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2021, last_season=2023)
    assert "g" not in panel[2022]
    history = av.histories_from_panel(panel, window=(2021, 2023))["g"]
    assert history.seasons == (2021, 2023)
    assert history.absent_seasons == (2023,)
    assert history.unknown_seasons == (2022,)
    row = av.player_availability("g", "WR", FULL[:16], history=history,
                                 history_window=(2021, 2023))
    blob = " ".join(row.reasons)
    assert "NOT in this record: 2022" in blob
    assert "cannot be better" in blob


# ============================================================ collapse + the view


def test_an_empty_history_read_is_a_collapse_not_a_board_of_average_players(db):
    """A book with no histories prices the whole board at 1.00x and says nothing.

    Same shape as SnapshotCollapse / BoardCollapse / CrosswalkCollapse: the failure
    is invisible precisely because every row still looks like a confident answer.
    """
    _seed_world(db, seasons=(2026,), weeks=(1, 2, 3), retrieved="2026-07-25")
    with pytest.raises(av.HistoryCollapse, match="history collapsed"):
        av.load_durability_book(
            db, as_of="2026-08-01", season=2026,
            first_history_season=2026, last_history_season=2026,
            min_clubs=0,
        )


def test_a_collapsed_slate_blames_the_view_and_not_the_schedule(db):
    """Finding the SLATE empty at a past as-of is a view bug, not a data gap.

    Every schedules row in the real database was retrieved in 2026, so the
    safe-default ``historical`` view gates the whole 2024 slate out and every
    ``availability_for`` call then fails on a message about the schedule.
    """
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2024,), weeks=weeks, retrieved="2026-07-25",
        appearances={(f"p{i}", 2024): _plan("AAA", "RB", weeks) for i in range(5)},
    )
    with pytest.raises(av.HistoryCollapse, match="slate collapsed") as caught:
        av.load_durability_book(
            db, as_of="2024-12-31", season=2024,
            first_history_season=2024, last_history_season=2024,
            min_histories=0, min_clubs=2,
        )
    assert "latest_truth" in str(caught.value)
    # ...and the view parameter is the fix, not a hand-built DurabilityBook.
    book = av.load_durability_book(
        db, as_of="2024-12-31", season=2024, view="latest_truth",
        first_history_season=2024, last_history_season=2024,
        min_histories=0, min_clubs=2,
    )
    assert book.slate["AAA"] == weeks
    assert book.view == "latest_truth"


def test_the_fallback_reason_names_the_window_that_was_actually_read(db):
    """A hardcoded "(2021-2025)" in the reason text outlives the caller's window.

    It read as a measured fact about the player while naming a window the book
    never opened.
    """
    _seed_world(
        db, seasons=(2022, 2026), weeks=(1, 2, 3),
        appearances={(f"p{i}", 2022): _plan("AAA", "RB", (1, 2, 3)) for i in range(3)},
    )
    book = av.load_durability_book(
        db, as_of="2026-08-01", season=2026,
        first_history_season=2022, last_history_season=2022,
        min_histories=0, min_clubs=0,
    )
    row = book.availability_for("ghost", "RB", "AAA", (1, 2, 3), gsis_id="nobody")
    basis = next(r for r in row.reasons if r.startswith("Basis: POSITION PRIOR"))
    assert "(2022-2022)" in basis
    assert "2021-2025" not in basis, "the reason must not name a window it never read"


def test_an_unlooked_up_player_is_not_told_he_has_no_record(db):
    """history=None has two causes and they are not the same statement.

    "We searched the record and he is not in it" is a fact about the player;
    "we never resolved him to a player id" is a fact about our crosswalk (42 of
    1,029 rows on the live 2026 board). Reporting the second as the first is
    exactly the class of statement Rule 6 exists to prevent.
    """
    _seed_world(db, seasons=(2026,), weeks=(1, 2, 3))
    book = av.load_durability_book(
        db, as_of="2026-08-01", season=2026,
        first_history_season=2026, last_history_season=2026,
        min_histories=0, min_clubs=0,
    )
    unknown_id = book.availability_for("x", "RB", "AAA", (1, 2, 3))
    looked_up = book.availability_for("y", "RB", "AAA", (1, 2, 3), gsis_id="nope")
    assert unknown_id.fallback_code == av.NO_ADJUSTMENT_NO_ID
    assert looked_up.fallback_code == av.NO_ADJUSTMENT_NO_RECORD
    assert "NEVER LOOKED UP" in " ".join(unknown_id.reasons)
    assert "no season" in " ".join(looked_up.reasons)
    assert "NEVER LOOKED UP" not in " ".join(looked_up.reasons)
    # The PRICE is identical — only the claim differs.
    assert unknown_id.expected_games_played == looked_up.expected_games_played
    # build_availability makes the same distinction off history_keys.
    rows = av.build_availability(
        [("known", "RB", FULL[:16]), ("unmapped", "RB", FULL[:16])],
        histories={}, history_keys={"known": "gsis-1"},
    )
    assert rows["known"].fallback_code == av.NO_ADJUSTMENT_NO_RECORD
    assert rows["unmapped"].fallback_code == av.NO_ADJUSTMENT_NO_ID


# ================================================== reasons that must reconcile


def test_a_clip_that_binds_says_so_because_the_printed_numbers_stop_adding_up():
    """The reader recomputes 3.7x from the quoted record, k and weight; the row
    shipped 2.5x. A stated reason that contradicts the shipped number is a Rule 6
    defect even when the number itself is the safe one."""
    brittle = av.PlayerHistory("b", games=68, missed=64, seasons=(2022, 2025),
                               role_weeks=60)
    row = av.player_availability("b", "RB", FULL[:16], history=brittle)
    assert row.multiplier == P.multiplier_clip[1]
    assert row.raw_multiplier > row.multiplier
    assert row.multiplier_clipped
    blob = " ".join(row.reasons)
    assert "CLIPPED" in blob
    assert f"{row.raw_multiplier:.2f}x" in blob
    # ...and a row the clip did NOT touch must not carry the disclosure.
    ordinary = av.player_availability(
        "o", "RB", FULL[:16],
        history=av.PlayerHistory("o", games=68, missed=14, seasons=(2022, 2025),
                                 role_weeks=60),
    )
    assert not ordinary.multiplier_clipped
    assert "CLIPPED" not in " ".join(ordinary.reasons)


def test_the_starting_role_floor_fires_where_the_game_floor_structurally_cannot():
    """``min_prior_games`` cannot reach a completed-season record.

    One season is already 17 club games, so on a full-history read the game floor
    never fires and the guard it was presented as never existed. What the record
    of a player who never held the job measures is his ROLE: measured on the live
    2026 board, ten one-season players priced 0.8-3.8 expected games BELOW a
    rookie with no record at all.
    """
    backup = av.PlayerHistory("b", games=17, missed=16, seasons=(2025,),
                              played=1, role_weeks=1)
    starter = av.PlayerHistory("s", games=17, missed=16, seasons=(2025,),
                               played=1, role_weeks=10)
    assert backup.games >= P.min_prior_games, "the game floor cannot see this"
    assert av.durability_multiplier("RB", backup) == (1.0, av.BASIS_POSITION)
    assert av.durability_multiplier("RB", starter)[1] == av.BASIS_PLAYER
    row = av.player_availability("b", "RB", FULL[:16], history=backup)
    assert row.fallback_code == av.NO_ADJUSTMENT_ROLE
    rookie = av.player_availability("r", "RB", FULL[:16])
    assert row.expected_games_played == pytest.approx(rookie.expected_games_played)
    blob = " ".join(row.reasons)
    assert "starting-role floor" in blob
    assert "measures his ROLE" in blob


def test_a_hand_built_history_with_no_role_measurement_is_not_silently_gated():
    """``role_weeks=None`` means "not measured", and must not read as zero."""
    unmeasured = av.PlayerHistory("h", games=68, missed=20, seasons=(2022, 2025))
    assert unmeasured.role_weeks is None
    assert av.durability_multiplier("WR", unmeasured)[1] == av.BASIS_PLAYER
    assert "Sample shape" not in " ".join(
        av.player_availability("h", "WR", FULL[:16], history=unmeasured).reasons
    )


def test_a_withheld_adjustment_still_names_a_season_he_missed_in_full():
    """A guard must not swallow the one fact the operator most needs.

    The row is priced as an average player because his record measures his role —
    but he lost a whole season, and "priced as an average TE" with nothing else
    said would read as a clean bill of health.
    """
    history = av.PlayerHistory("j", games=34, missed=31, seasons=(2024, 2025),
                               absent_seasons=(2025,), played=3, role_weeks=3)
    row = av.player_availability("j", "RB", FULL[:16], history=history)
    assert row.basis == av.BASIS_POSITION
    blob = " ".join(row.reasons)
    assert "NOT PRICED IN" in blob
    assert "missed 2025 in full" in blob
    assert "NOT because the record is clean" in blob


def test_a_prior_with_no_position_mean_does_not_claim_the_sample_was_too_small():
    """Five facts used to share one sentence, and this one was a falsehood.

    With ``prior_history_rate['RB'] = 0.0`` an 85-game record was told it was
    "below the 8-game floor". 85 is not below 8. And the gap is in the PRIOR (this
    module's own re-measurement produces a 0.0 whenever a cohort missed nothing),
    not in the player.
    """
    holed = dataclasses.replace(
        P, prior_history_rate={**P.prior_history_rate, "RB": 0.0}
    )
    history = av.PlayerHistory("p", games=85, missed=40, seasons=(2021, 2025),
                               role_weeks=80)
    row = av.player_availability("p", "RB", FULL[:16], history=history, prior=holed)
    assert row.basis == av.BASIS_POSITION
    assert row.fallback_code == av.NO_ADJUSTMENT_NO_MEAN
    blob = " ".join(row.reasons)
    assert "8-game floor" not in blob
    assert "NO measured prior-history mean" in blob
    assert "The gap is in the prior, not in him." in blob


def test_an_overridden_starting_condition_says_so():
    """The Rule-6 line for the override the caveats say a caller MUST use for a
    player known to be starting the season on IR. Untested, it silently vanished:
    the row reported 7.8 of 16 games missed with nothing saying why."""
    overridden = av.player_availability("hurt", "RB", FULL[:16], opening_absent=1.0)
    assert "Starting condition OVERRIDDEN" in " ".join(overridden.reasons)
    assert overridden.expected_games_missed > 7.5
    # An override that lands on the prior's own value is not an override.
    same = av.player_availability(
        "same", "RB", FULL[:16], opening_absent=P.opening_absent["RB"]
    )
    assert "Starting condition OVERRIDDEN" not in " ".join(same.reasons)


def test_every_position_prior_discloses_that_it_is_a_lower_bound():
    """56 cohort-eligible seasons vanish from the record entirely, so every rate
    is a floor. Showing the operator the floor alone is the failure mode this
    module exists to avoid, so the band is printed on every priceable row."""
    for position in ("QB", "RB", "WR", "TE", "K"):
        assert P.season_miss_rate_upper[position] > P.season_miss_rate[position]
        blob = " ".join(av.player_availability("p", position, FULL[:16]).reasons)
        assert "LOWER bound" in blob
        assert str(P.unprovable_cohort_seasons) in blob
        assert f"{100 * P.season_miss_rate_upper[position]:.1f}%" in blob


# ============================================ per-accessor view threading (Rule 1)


def _one_gated_source(db, **stamps):
    """Seed a world where exactly ONE table is future-retrieved."""
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2024,), weeks=weeks, retrieved="2024-01-01",
        appearances={("g", 2024): _plan("AAA", "RB", (1, 3))},
        injuries=[("g", 2024, 2, "Out", "AAA", "RB")],
        **stamps,
    )


def test_the_view_reaches_the_schedule_read_not_just_the_signature(db):
    """``inspect.signature(fn).parameters['view'].default`` proves nothing about
    where the argument goes. Accepting ``view`` and ignoring it leaves the
    composite leakage tests green, because the appearances are hidden by the same
    view and the result collapses to {} either way."""
    _one_gated_source(db, retrieved_schedules="2026-07-25")
    assert av.team_game_weeks(db, as_of="2024-12-31", season=2024) == {}
    seen = av.team_game_weeks(db, as_of="2024-12-31", season=2024,
                              view="latest_truth")
    assert seen["AAA"] == (1, 2, 3)
    # player_seasons builds its denominator from that read, so a hidden schedule
    # must leave it with no rows at all rather than a slate from another view.
    assert av.player_seasons(db, as_of="2024-12-31", season=2024) == {}


def test_the_view_reaches_the_weekly_stats_read(db):
    """Only weekly_stats is gated: the stat-line appearance must disappear while
    everything around it stays visible."""
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2024,), weeks=weeks, retrieved="2024-01-01",
        retrieved_weekly="2026-07-25",
        appearances={("g", 2024): {1: ("AAA", "stat:RB"), 2: ("AAA", "stat:RB")}},
    )
    assert av.player_seasons(db, as_of="2024-12-31", season=2024) == {}
    seen = av.base.latest_truth(av.player_seasons)(db, as_of="2024-12-31", season=2024)
    assert seen["g"].played_weeks == (1, 2)


def test_the_view_reaches_the_snap_counts_read(db):
    """Only snap_counts is gated, and only snap-derived appearances are planted."""
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2024,), weeks=weeks, retrieved="2024-01-01",
        retrieved_snaps="2026-07-25",
        appearances={("g", 2024): {1: ("AAA", "snap:RB"), 2: ("AAA", "snap:RB")}},
    )
    assert av.player_seasons(db, as_of="2024-12-31", season=2024) == {}
    seen = av.base.latest_truth(av.player_seasons)(db, as_of="2024-12-31", season=2024)
    assert seen["g"].played_weeks == (1, 2)


def test_the_view_reaches_the_roster_evidence_read(db):
    """``injuries`` is read on two independent paths and neither may pick its own
    view. Path one: the roster-evidence read that decides whether a wholly-missed
    season becomes a row at all."""
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks, retrieved="2024-01-01",
        retrieved_injuries="2026-07-25",
        appearances={("g", 2023): _plan("AAA", "RB", weeks)},
        injuries=[("g", 2024, 1, "Out", "AAA", "RB")],
    )
    assert av._roster_evidence(db, as_of="2024-12-31", season=2024) == {}
    assert "g" not in av.player_seasons(db, as_of="2024-12-31", season=2024)
    revealed = av.base.latest_truth(av.player_seasons)(
        db, as_of="2024-12-31", season=2024
    )
    assert revealed["g"].roster_basis == av.SEASON_ROSTER_EVIDENCE


def test_the_view_reaches_the_injury_coverage_read_inside_measure_durability(db):
    """Path two: the injury-report cross-check. Its only test ran at an as-of
    where both views agree, so ``measure_durability``'s ``view`` was never
    exercised at all and hardcoding it left the file green."""
    weeks = (1, 2, 3)
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks, retrieved="2024-01-01",
        retrieved_injuries="2026-07-25",
        appearances={("g", 2023): _plan("AAA", "RB", weeks),
                     ("g", 2024): _plan("AAA", "RB", (1, 3))},
        injuries=[("g", 2024, 2, "Out", "AAA", "RB")],
    )
    panel = av.season_panel(db, as_of="2024-12-31", first_season=2023,
                            last_season=2024, view="latest_truth")
    assert panel[2024]["g"].missed == 1
    hidden = av.measure_durability(db, as_of="2024-12-31", first_season=2023,
                                   last_season=2024, panel=panel, min_role_weeks=1)
    shown = av.base.latest_truth(av.measure_durability)(
        db, as_of="2024-12-31", first_season=2023, last_season=2024, panel=panel,
        min_role_weeks=1,
    )
    assert hidden.injury_report_coverage["RB"] == 0.0
    assert shown.injury_report_coverage["RB"] == 1.0


def test_a_season_detected_by_the_injury_report_cannot_grade_the_injury_report(db):
    """Circularity guard on the coverage cross-check.

    A wholly-missed season found VIA an injury row was selected for carrying one;
    counting its 17 missed weeks in the coverage denominator would let the report
    mark its own homework in whichever direction the row happened to fall.
    """
    weeks = tuple(range(1, 18))
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks,
        appearances={("g", 2023): _plan("AAA", "RB", weeks)},
        injuries=[("g", 2024, w, "Out", "AAA", "RB") for w in weeks],
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2023, last_season=2024)
    assert panel[2024]["g"].roster_basis == av.SEASON_ROSTER_EVIDENCE
    m = av.measure_durability(db, as_of="2026-08-01", first_season=2023,
                              last_season=2024, panel=panel, min_role_weeks=1)
    # He IS in the cohort and his lost season DOES move the miss rate...
    assert m.cohort_n["RB"] == 1
    assert m.season_miss_rate["RB"] == 1.0
    # ...but he is not allowed to be evidence about the injury report's reach.
    assert m.injury_report_coverage["RB"] == 0.0
    assert m.presence_mean_missed["RB"] == 0.0


# ================================================= calibration-window invariants


def test_horizon_takes_the_first_n_club_games_not_the_last():
    """The whole basis of FANTASY_HORIZON_GAMES = 16.

    The game this drops is a club's LAST one — the rest-your-starters finale,
    measured onset 13.6% against ~7% every other week, and never a scoring week in
    this league. Taking the last n instead silently moves every position's rate
    (measured on the live panel: QB +6.6% relative, TE +9.5%) and every board row
    with it.
    """
    season = av.PlayerSeason(
        gsis_id="g", season=2024, position="RB", teams=("AAA",),
        game_weeks=tuple(range(1, 18)),
        played_weeks=tuple(range(3, 18)),      # missed his club's first two
        role_weeks=15,
    )
    assert season.horizon(16) == (2, 16)       # last-16 would report (1, 16)
    finale_only = av.PlayerSeason(
        gsis_id="g", season=2024, position="RB", teams=("AAA",),
        game_weeks=tuple(range(1, 18)),
        played_weeks=tuple(range(1, 17)),      # rested for the finale only
        role_weeks=16,
    )
    assert finale_only.horizon(16) == (0, 16), "the finale must be OUT of the window"
    assert finale_only.missed == 1, "...but it is still a missed game overall"


def test_the_measured_rate_excludes_the_rest_your_starters_finale(db):
    """The same invariant, through the whole measurement rather than the dataclass."""
    weeks = tuple(range(1, 18))
    plans = {}
    for i in range(3):
        plans[(f"te{i}", 2023)] = _plan(f"C{i}", "TE", weeks)
        plans[(f"te{i}", 2024)] = _plan(f"C{i}", "TE", weeks[:16])   # rested wk 17
    schedule = [(s, w, f"C{i}", "ZZZ") for s in (2023, 2024)
                for w in weeks for i in range(3)]
    _seed_world(db, seasons=(2023, 2024), weeks=weeks, schedule=schedule,
                appearances=plans)
    m = av.measure_durability(db, as_of="2026-08-01", first_season=2023,
                              last_season=2024)
    assert m.season_miss_rate["TE"] == 0.0, "the finale is not in the window"
    assert m.presence_mean_missed["TE"] == pytest.approx(1.0), "...but it happened"


def test_the_cohort_gate_excludes_a_prior_season_non_starter(db):
    """``min_role_weeks`` is the module's stated reason the measurement means
    anything ("measuring games missed over everyone who ever appeared is
    meaningless"), and deleting the threshold check used to pass the whole file.

    Measured on the live panel, dropping it takes the cohort from 1,017 to 2,047
    rows and blows the rates out: QB 0.261 -> 0.486, WR 0.197 -> 0.305.
    """
    weeks = tuple(range(1, 18))
    starter_weeks = weeks
    scrub_weeks = weeks[:4]                   # 4 role weeks, below the gate
    _seed_world(
        db, seasons=(2023, 2024), weeks=weeks,
        appearances={
            ("starter", 2023): _plan("AAA", "TE", starter_weeks),
            ("starter", 2024): _plan("AAA", "TE", starter_weeks),
            ("scrub", 2023): _plan("BBB", "TE", scrub_weeks),
            ("scrub", 2024): _plan("BBB", "TE", starter_weeks),
        },
    )
    panel = av.season_panel(db, as_of="2026-08-01", first_season=2023, last_season=2024)
    assert panel[2023]["starter"].role_weeks == 17
    assert panel[2023]["scrub"].role_weeks == 4
    ids = {ps.gsis_id for ps in av.cohort_seasons(panel)}
    assert ids == {"starter"}, "a prior-season non-starter is not in the cohort"
    # ...and the threshold is the thing doing it, not the presence of a Y-1 row.
    loosened = {ps.gsis_id for ps in av.cohort_seasons(panel, min_role_weeks=4)}
    assert loosened == {"starter", "scrub"}
