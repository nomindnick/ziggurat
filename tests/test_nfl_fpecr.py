"""Ingestion + leakage tests for the DynastyProcess db_fpecr ECR panel (011).

Fixtures are built inline — small, transparent, invented — so nothing here needs
the 38 MB archive or the network. Two things get more attention than the rest,
because they are the two ways this source silently corrupts a result:

* THE INFERRED NFL WEEK. The archive states no week. A Friday scrape sits AFTER
  that week's Thursday opener, so the natural rule ("the first week whose games
  have not started") is off by one for exactly the scrapes the archive is made
  of. ``test_friday_scrape_maps_to_the_week_it_ranks`` pins the shipped rule AND
  shows the naive rule disagreeing, so a future edit toward the naive rule fails
  loudly rather than shifting every weekly comparison by seven days.

* THE PAGE IN THE PRIMARY KEY. One ``ecr_type`` spans several ranking pages, and
  on one ``scrape_date`` two of them really do carry the same player.
  ``adp_rankings``' key cannot hold both.

  CORRECTED 2026-08-30, and the correction is the point of the test below.
  Migration 011's header — and an earlier version of this docstring — said the
  collision was the frozen PRESEASON cheatsheet against the live REST-OF-SEASON
  board, both ``ro``. Measured on the pinned mirror, ``ppr-cheatsheets`` and
  ``ros-ppr-overall`` share ZERO scrape dates, so that collision occurs exactly
  never and the leakage story built on it cannot fire. The REAL collision is
  DUAL ELIGIBILITY: FantasyPros publishes an RB/WR (or a defense) on two
  POSITIONAL pages of one series on one day — 215 such groups in the ro/rp/wp
  slice, 14 distinct player-name strings. The design decision is unchanged and
  the number of facts at stake is larger than the header claimed; only the
  stated mechanism was wrong.
"""

import pandas as pd
import pytest

from ziggurat.data.nfl import base, fpecr

# ---------------------------------------------------------------- fixtures


def _stub_players(db, mapping, *, retrieved="2026-08-01"):
    for fp, (gsis, espn) in mapping.items():
        db.execute(
            "INSERT INTO players (gsis_id, fantasypros_id, espn_id, retrieved_as_of, "
            "knowable_as_of) VALUES (?,?,?,?,?)",
            (gsis, fp, espn, retrieved, retrieved),
        )
    db.commit()


def _stub_schedule(db, season, weeks, *, retrieved="2026-08-01"):
    """``weeks`` maps week -> (first gameday, last gameday). One game each end."""
    for week, (first, last) in weeks.items():
        for i, day in enumerate({first, last}):
            db.execute(
                "INSERT INTO schedules (game_id, season, week, game_type, gameday, "
                "home_team, away_team, retrieved_as_of, knowable_as_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{season}_{week}_{i}", season, week, "REG", day, "BUF", "MIA",
                 retrieved, retrieved),
            )
    db.commit()


_CROSSWALK = {
    "17298": ("00-0034857", "3918298"),
    "10101": ("00-0038542", "4430807"),
    "10303": ("00-0036900", "4362628"),
    "26068": ("00-0037476", "4249087"),
}

_COLUMNS = ["id", "player", "pos", "team", "ecr", "sd", "best", "worst",
            "player_owned_avg", "player_owned_espn", "ecr_type", "fp_page", "scrape_date"]


def _row(id_, player, pos, team, ecr, *, sd=2.0, best=None, worst=None,
         ecr_type="ro", fp_page="/nfl/rankings/ppr-cheatsheets.php",
         scrape_date="2023-09-01", owned=90.0):
    return dict(
        id=str(id_), player=player, pos=pos, team=team, ecr=ecr, sd=sd,
        best=ecr - 2 if best is None else best, worst=ecr + 2 if worst is None else worst,
        player_owned_avg=owned, player_owned_espn=owned,
        ecr_type=ecr_type, fp_page=fp_page, scrape_date=scrape_date,
    )


def _frame(rows):
    return pd.DataFrame(rows, columns=_COLUMNS)


def _board_frame(scrape_date="2023-09-01"):
    """A small preseason overall cheatsheet: 4 league positions + 1 IDP LB."""
    return _frame([
        _row(10303, "Ja'Marr Chase", "WR", "CIN", 2.1, scrape_date=scrape_date),
        _row(10101, "Bijan Robinson", "RB", "ATL", 3.5, scrape_date=scrape_date),
        _row(17298, "Josh Allen", "QB", "BUF", 25.2, scrape_date=scrape_date),
        _row(26068, "Brandon Aubrey", "K", "DAL", 187.1, scrape_date=scrape_date),
        _row(8130, "Los Angeles Rams", "DST", "LAR", 150.0, scrape_date=scrape_date),
        _row(19292, "Jordyn Brooks", "LB", "MIA", 1.7, scrape_date=scrape_date),  # IDP
    ])


@pytest.fixture()
def panel_db(db):
    _stub_players(db, _CROSSWALK)
    _stub_schedule(db, 2023, {
        1: ("2023-09-07", "2023-09-11"),
        2: ("2023-09-14", "2023-09-18"),
        3: ("2023-09-21", "2023-09-25"),
    })
    return db


# ---------------------------------------------------------------- the week


def test_friday_scrape_maps_to_the_week_it_ranks_not_the_next_one(panel_db):
    """THE off-by-one. A Friday scrape ranks THIS week's Sunday games.

    Week 2 runs Thu 09-14 through Mon 09-18. A scrape on Friday 09-15 is already
    past the Thursday opener, so the plausible-sounding "first week whose games
    have not started yet" answers week 3. The shipped rule keys on the week's
    LAST gameday and answers week 2. Both are computed here so an edit toward the
    wrong one cannot pass.
    """
    bounds = fpecr.week_bounds(panel_db, 2023)
    assert bounds[2] == ("2023-09-14", "2023-09-18")

    week, basis = fpecr.infer_nfl_week("2023-09-15", bounds)
    assert (week, basis) == (2, "schedules")

    naive = min(w for w, (first, _last) in sorted(bounds.items()) if first >= "2023-09-15")
    assert naive == 3, "the naive rule must genuinely disagree, or this pins nothing"


def test_week_inference_branches_are_each_a_positive_fact(panel_db):
    bounds = fpecr.week_bounds(panel_db, 2023)
    # Before week 1's first kickoff: a PRESEASON board, stated as week 0.
    assert fpecr.infer_nfl_week("2023-09-01", bounds) == (0, "schedules")
    # Tuesday after week 2's Monday night game -> week 3.
    assert fpecr.infer_nfl_week("2023-09-19", bounds) == (3, "schedules")
    # After the last REG gameday we know it is not a regular-season week...
    assert fpecr.infer_nfl_week("2024-01-10", bounds) == (None, "after_season")
    # ...and with no schedule at all we say we do not know, which is different.
    assert fpecr.infer_nfl_week("2023-09-15", {}) == (None, "no_schedule")


def test_scrape_date_season_follows_the_nfl_league_year(panel_db):
    """A January scrape belongs to the PREVIOUS season — which is when the
    fantasy playoffs are graded, so getting it wrong misfiles the sharpest week."""
    df = _frame([_row(10303, "Ja'Marr Chase", "WR", "CIN", 2.1,
                      ecr_type="wp", fp_page="ppr-wr", scrape_date="2024-01-05")])
    fpecr.ingest_fpecr(panel_db, df, retrieved_as_of="2026-08-30", ecr_types=None)
    row = panel_db.execute("SELECT season, nfl_week, week_basis FROM fpecr_panel").fetchone()
    assert row["season"] == 2023
    assert row["week_basis"] == "after_season" and row["nfl_week"] is None


# ---------------------------------------------------------------- the key


def test_fp_page_must_stay_in_the_primary_key(panel_db):
    """THE reason this is not rows in ``adp_rankings`` — measured, not assumed.

    A DUAL-ELIGIBLE player is ranked on two pages of one series on one day, with
    two different consensus ranks, because the market prices him differently in
    the two contexts. Those are two facts. ``adp_rankings``' key
    (fantasypros_id, ecr_type, scrape_date, retrieved_as_of) holds ONE of them;
    this table's key holds both. The pair used here is the real ``ro`` shape:
    a two-way player on both ``ppr-cheatsheets`` and ``idp-cheatsheets``, which
    is 33 of the 38 ``ro`` collisions on the shipped archive.

    MEASURED on the pinned 2026-08-30 mirror, after this ingester's position
    filter, over ro/rp/wp: 215 same-key page collisions (ro 38, rp 133, wp 44)
    across 14 distinct player-name strings — ppr-rb-cheatsheets +
    ppr-wr-cheatsheets 59, ppr-rb + ppr-wr 36, ros-ppr-rb + ros-ppr-wr 36,
    idp-cheatsheets + ppr-cheatsheets 33, db-cheatsheets + ppr-wr-cheatsheets 31,
    down to ppr-te + qb 1. Drop ``fp_page`` from the key and 215 real market
    facts silently fold to roughly half that.

    NOT the mechanism migration 011's header states. That header claims the
    collision is ``ppr-cheatsheets`` (preseason) against ``ros-ppr-overall``
    (rest-of-season) inside ``ro``, and that folding them could make a
    "preseason board" read return a mid-season ranking. Those two pages share
    ZERO scrape dates on the shipped archive (259 vs 101 dates, disjoint), so no
    key group can contain both and that failure cannot occur. The header is an
    applied migration and cannot be edited; this test is where the true reason
    lives.
    """
    df = _frame([
        _row(10303, "Two-Way Player", "WR", "CIN", 2.1, fp_page="ppr-cheatsheets"),
        _row(10303, "Two-Way Player", "WR", "CIN", 5.0, fp_page="idp-cheatsheets"),
    ])
    written = fpecr.ingest_fpecr(panel_db, df, retrieved_as_of="2026-08-30")
    assert written == 2
    stored = {
        r["fp_page"]: r["ecr"]
        for r in fpecr.get_fpecr(panel_db, as_of="2026-08-30", view="latest_truth")
    }
    assert stored == {"ppr-cheatsheets": 2.1, "idp-cheatsheets": 5.0}

    # And the counter-demonstration, run rather than asserted: the SAME two rows
    # loaded into adp_rankings collapse onto one key, keeping whichever page came
    # last, with nothing in the table recording that the other existed.
    from ziggurat.data.nfl import adp_rankings

    adp_rankings.ingest_adp_rankings(panel_db, df, retrieved_as_of="2026-08-30")
    adp_rows = base.latest_truth(adp_rankings.get_adp_rankings)(
        panel_db, as_of="2026-08-30", season=2023,
    )
    assert len(adp_rows) == 1, "adp_rankings would have held both — then 011 is pointless"


def test_both_upstream_spellings_of_a_page_fold_to_one(panel_db):
    """``/nfl/rankings/ppr-cheatsheets.php`` and ``ppr-cheatsheets`` are the same
    ranking from two eras of the scraper. Unfolded they split one series in half
    and a 'latest scrape' read can silently pick the older spelling's date."""
    df = _frame([
        _row(10303, "Ja'Marr Chase", "WR", "CIN", 2.1,
             fp_page="/nfl/rankings/ppr-cheatsheets.php", scrape_date="2023-08-25"),
        _row(10303, "Ja'Marr Chase", "WR", "CIN", 2.0,
             fp_page="ppr-cheatsheets", scrape_date="2023-09-01"),
    ])
    fpecr.ingest_fpecr(panel_db, df, retrieved_as_of="2026-08-30")
    pages = {r["fp_page"] for r in fpecr.get_fpecr(
        panel_db, as_of="2026-08-30", view="latest_truth")}
    assert pages == {"ppr-cheatsheets"}
    assert fpecr.latest_scrape_date(
        panel_db, as_of="2026-08-30", season=2023, view="latest_truth"
    ) == "2023-09-01"


# ---------------------------------------------------------------- filtering


def test_idp_is_dropped_and_positions_are_normalized(panel_db):
    df = pd.concat([
        _board_frame(),
        _frame([_row(30001, "Legacy Kicker", "PK", "SD", 200.0)]),
    ], ignore_index=True)
    written = fpecr.ingest_fpecr(panel_db, df, retrieved_as_of="2026-08-30")
    assert written == 6  # 7 offered, the IDP LB dropped

    rows = fpecr.get_fpecr(panel_db, as_of="2026-08-30", view="latest_truth")
    positions = {r["position"] for r in rows}
    assert "LB" not in positions
    assert "PK" not in positions and "K" in positions
    teams = {r["player"]: r["team"] for r in rows}
    assert teams["Los Angeles Rams"] == "LA"     # LAR -> LA
    assert teams["Legacy Kicker"] == "LAC"       # SD -> LAC


def test_crosswalk_resolves_skill_and_keeps_unresolved_rows(panel_db):
    fpecr.ingest_fpecr(panel_db, _board_frame(), retrieved_as_of="2026-08-30")
    rows = {r["player"]: r for r in fpecr.get_fpecr(
        panel_db, as_of="2026-08-30", view="latest_truth")}
    assert rows["Ja'Marr Chase"]["gsis_id"] == "00-0036900"
    # The DST's FantasyPros TEAM id is absent from the player crosswalk. The row
    # is KEPT with a NULL gsis and joined downstream by team.
    assert rows["Los Angeles Rams"]["gsis_id"] is None
    assert rows["Los Angeles Rams"]["team"] == "LA"


def test_ranks_are_contiguous_within_a_page_and_within_a_position(panel_db):
    fpecr.ingest_fpecr(panel_db, _board_frame(), retrieved_as_of="2026-08-30")
    rows = fpecr.get_fpecr(panel_db, as_of="2026-08-30", view="latest_truth")
    assert sorted(r["page_rank"] for r in rows) == [1, 2, 3, 4, 5]
    by_pos = {}
    for r in rows:
        by_pos.setdefault(r["position"], []).append(r["pos_rank"])
    assert all(sorted(v) == list(range(1, len(v) + 1)) for v in by_pos.values())
    # Ranked by ECR ascending, over LEAGUE positions only — the dropped IDP LB
    # had the best ECR of the frame and must not have consumed rank 1.
    ranked = sorted(rows, key=lambda r: r["page_rank"])
    assert ranked[0]["player"] == "Ja'Marr Chase"


def test_a_rank_hole_is_refused(panel_db, monkeypatch):
    """Mutation check: break the ranker and the post-condition must fire."""
    def _holed(rows):
        for i, row in enumerate(rows, start=1):
            row["page_rank"] = i + 1
            row["pos_rank"] = 1
    monkeypatch.setattr(fpecr, "_assign_ranks", _holed)
    with pytest.raises(fpecr.PanelRankDiscontinuity):
        fpecr.ingest_fpecr(panel_db, _board_frame(), retrieved_as_of="2026-08-30")


# ---------------------------------------------------------------- guards


def test_missing_upstream_column_fails_loudly(panel_db):
    df = _board_frame().drop(columns=["sd"])
    with pytest.raises(ValueError, match="missing required columns"):
        fpecr.ingest_fpecr(panel_db, df, retrieved_as_of="2026-08-30")


def test_a_shrunken_repull_is_refused_before_it_can_shadow_the_panel(panel_db):
    """No DELETE happens here, and the floor is still needed: ``select_as_of``
    resolves the NEWEST retrieved version per key, so a truncated file only has
    to arrive later to make the complete stored panel unreadable."""
    fpecr.ingest_fpecr(panel_db, _board_frame(), retrieved_as_of="2026-08-30")
    truncated = _frame([_row(10303, "Ja'Marr Chase", "WR", "CIN", 2.1)])
    with pytest.raises(fpecr.PanelCollapse, match="distinct keys"):
        fpecr.ingest_fpecr(panel_db, truncated, retrieved_as_of="2026-08-31")
    # The stored panel is untouched and still readable.
    assert len(fpecr.get_fpecr(panel_db, as_of="2026-08-31", view="latest_truth")) == 5


def test_a_narrowed_repull_of_one_series_is_not_mistaken_for_a_truncated_file(panel_db):
    """The floor must compare LIKE FOR LIKE, or a legal call can never succeed.

    ``pull_fpecr``/``ingest_fpecr`` both take ``ecr_types``, so re-pulling one
    series is a supported call. Before the floor was scoped it compared that one
    series' key count against EVERY stored series and refused, with a remedy
    ("re-download and retry") that could never clear because nothing was wrong
    with the file. Reproduced against the live panel at the time: feeding back
    the stored season-2021 ``ro`` rows (28,315 of 71,757 keys, the rest ``rp``
    and ``wp``) raised PanelCollapse.

    Mutation check: revert ``_stored_key_counts`` to an unscoped COUNT and the
    first ingest below raises.
    """
    board = _board_frame()
    positional = _frame([
        _row(10303, "Ja'Marr Chase", "WR", "CIN", 1.0,
             ecr_type="rp", fp_page="ppr-wr-cheatsheets"),
        _row(10101, "Bijan Robinson", "RB", "ATL", 1.0,
             ecr_type="rp", fp_page="ppr-rb-cheatsheets"),
        _row(17298, "Josh Allen", "QB", "BUF", 1.0,
             ecr_type="rp", fp_page="qb-cheatsheets"),
        _row(26068, "Brandon Aubrey", "K", "DAL", 1.0,
             ecr_type="rp", fp_page="k-cheatsheets"),
    ])
    fpecr.ingest_fpecr(panel_db, pd.concat([board, positional]),
                       retrieved_as_of="2026-08-30")

    # The narrowed re-pull the API offers: same rows, one series.
    written = fpecr.ingest_fpecr(
        panel_db, board, retrieved_as_of="2026-08-31", ecr_types=("ro",)
    )
    assert written == 5

    # ...and a genuinely truncated re-pull of that same narrow series is STILL
    # refused, so the fix widened nothing it should not have.
    with pytest.raises(fpecr.PanelCollapse, match="series ro"):
        fpecr.ingest_fpecr(
            panel_db,
            _frame([_row(10303, "Ja'Marr Chase", "WR", "CIN", 2.1)]),
            retrieved_as_of="2026-09-01", ecr_types=("ro",),
        )


def test_a_requested_season_that_stops_arriving_is_still_checked(panel_db):
    """A season the caller ASKED FOR that shows up with no rows is a collapse.

    Without this the floor sleeps on the worst case it exists for: a file that
    stopped publishing 2023 entirely creates no incoming bucket for 2023, so a
    per-incoming-bucket loop never looks at it and the stored season is shadowed
    by nothing at all. Mutation check: drop ``requested`` from ``_check_panel_size``
    and this passes silently.
    """
    _stub_schedule(panel_db, 2024, {1: ("2024-09-05", "2024-09-09")})
    y2023 = _board_frame(scrape_date="2023-09-01")
    y2024 = _board_frame(scrape_date="2024-09-01")
    fpecr.ingest_fpecr(panel_db, pd.concat([y2023, y2024]), retrieved_as_of="2026-08-30")
    assert {r["season"] for r in fpecr.get_fpecr(
        panel_db, as_of="2026-08-30", view="latest_truth")} == {2023, 2024}

    with pytest.raises(fpecr.PanelCollapse, match="season 2023"):
        fpecr.ingest_fpecr(
            panel_db, y2024, retrieved_as_of="2026-08-31", seasons=(2023, 2024)
        )


def test_a_full_repull_of_the_same_panel_is_allowed_and_versions(panel_db):
    fpecr.ingest_fpecr(panel_db, _board_frame(), retrieved_as_of="2026-08-30")
    fpecr.ingest_fpecr(panel_db, _board_frame(), retrieved_as_of="2026-08-31")
    stamps = {r[0] for r in panel_db.execute(
        "SELECT DISTINCT retrieved_as_of FROM fpecr_panel")}
    assert stamps == {"2026-08-30", "2026-08-31"}
    # ...and the read still resolves ONE row per key.
    assert len(fpecr.get_fpecr(panel_db, as_of="2026-08-31", view="latest_truth")) == 5


def test_writing_nothing_is_never_ok(panel_db):
    """A 1.8M-row file that filters down to nothing is a caller error or a schema
    change, not a legitimately empty upstream."""
    with pytest.raises(fpecr.PanelCollapse, match="no rows survived"):
        fpecr.ingest_fpecr(
            panel_db, _board_frame(), retrieved_as_of="2026-08-30", seasons=[1999]
        )


def test_a_failure_mid_write_rolls_the_whole_run_back(panel_db, monkeypatch):
    real = base.upsert

    def _explode(conn, table, rows, **kwargs):
        real(conn, table, rows, **kwargs)
        raise RuntimeError("upstream died after the insert")

    monkeypatch.setattr(fpecr.base, "upsert", _explode)
    with pytest.raises(RuntimeError):
        fpecr.ingest_fpecr(panel_db, _board_frame(), retrieved_as_of="2026-08-30")
    assert panel_db.execute("SELECT COUNT(*) FROM fpecr_panel").fetchone()[0] == 0


def test_duplicate_keys_fold_to_the_wider_dispersion(panel_db):
    """Upstream ships dual-eligibility players twice on one key. Fold BEFORE
    ranking (the ``adp_rankings`` lesson: a rank handed to a row INSERT OR
    REPLACE then discards is a hole in the published board), and keep the row
    that reports the WIDER expert disagreement."""
    df = _frame([
        _row(10303, "Two-Way Player", "WR", "CIN", 66.0, sd=1.0, best=65, worst=67),
        _row(10303, "Two-Way Player", "WR", "CIN", 66.1, sd=12.4, best=38, worst=112),
        _row(10101, "Bijan Robinson", "RB", "ATL", 3.5),
    ])
    written = fpecr.ingest_fpecr(panel_db, df, retrieved_as_of="2026-08-30")
    assert written == 2
    rows = {r["player"]: r for r in fpecr.get_fpecr(
        panel_db, as_of="2026-08-30", view="latest_truth")}
    assert rows["Two-Way Player"]["sd"] == 12.4
    assert sorted(r["page_rank"] for r in rows.values()) == [1, 2]


# ---------------------------------------------------------------- leakage


def test_a_later_scrape_is_invisible_at_an_earlier_as_of(panel_db):
    """The fact-time gate: ``knowable_as_of`` is the scrape day, so a mid-season
    re-ranking cannot reach a preseason read. This is what stops the backtest's
    'preseason board' from quietly containing week-3 information."""
    fpecr.ingest_fpecr(panel_db, _board_frame("2023-09-01"), retrieved_as_of="2026-08-30")
    fpecr.ingest_fpecr(panel_db, _board_frame("2023-09-22"), retrieved_as_of="2026-08-30")

    early = fpecr.get_fpecr(panel_db, as_of="2023-09-05", view="latest_truth")
    assert {r["scrape_date"] for r in early} == {"2023-09-01"}

    late = fpecr.get_fpecr(panel_db, as_of="2023-09-30", view="latest_truth")
    assert {r["scrape_date"] for r in late} == {"2023-09-01", "2023-09-22"}

    assert fpecr.latest_scrape_date(
        panel_db, as_of="2023-09-30", season=2023, before="2023-09-07",
        view="latest_truth",
    ) == "2023-09-01"


def test_the_default_historical_view_hides_bulk_loaded_history(panel_db):
    """The footgun ``base.latest_truth`` exists for, asserted rather than assumed.

    The panel is mirrored in 2026, so every 2021-2025 row carries a 2026
    ``retrieved_as_of``. Under the safe default a past read returns NOTHING —
    silently, reading exactly like 'there was no football that year'.
    """
    fpecr.ingest_fpecr(panel_db, _board_frame("2023-09-01"), retrieved_as_of="2026-08-30")
    assert fpecr.get_fpecr(panel_db, as_of="2023-09-05") == []
    assert len(fpecr.get_fpecr(panel_db, as_of="2023-09-05", view="latest_truth")) == 5
    assert fpecr.latest_scrape_date(panel_db, as_of="2023-09-05", season=2023) is None


def test_accessor_requires_an_explicit_as_of(panel_db):
    with pytest.raises(TypeError):
        fpecr.get_fpecr(panel_db)          # Rule 1: no implicit now
    with pytest.raises(TypeError):
        fpecr.get_fpecr(panel_db, as_of=None)


def test_latest_truth_refuses_a_conflicting_view(panel_db):
    with pytest.raises(ValueError, match="conflicting view"):
        base.latest_truth(fpecr.get_fpecr)(
            panel_db, as_of="2023-09-05", view="historical"
        )


def test_network_seam_is_bounded_and_atomic(monkeypatch, tmp_path):
    """``fetch_fpecr`` must pass a timeout (an unbounded urlopen under systemd
    ``Type=oneshot`` parks the cadence forever) and must rename only on success,
    so a truncated download is never mistaken for a mirror."""
    from ziggurat import net

    seen = {}

    class _Response:
        def __init__(self):
            self._chunks = [b"parquet-bytes", b""]

        def read(self, _n):
            return self._chunks.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        seen["timeout"] = timeout
        seen["url"] = request.full_url
        return _Response()

    monkeypatch.setattr(fpecr.urllib.request, "urlopen", _urlopen)
    dest = tmp_path / "db_fpecr.parquet"
    fpecr.fetch_fpecr(dest)

    assert seen["timeout"] == net.HTTP_TIMEOUT
    assert seen["url"] == fpecr.FPECR_URL
    assert dest.read_bytes() == b"parquet-bytes"
    assert not (tmp_path / "db_fpecr.parquet.part").exists()
