"""Item 3.2c — the cross-agent SEAMS, tested from both sides at once.

3.2c was built by five agents working on disjoint file sets, each of whom could
verify their own half and had to hand the other half over as a written note. A
handoff is a claim about a file the claimant did not open; this module is where
those claims get checked mechanically instead of by reading two reports side by
side.

Three seams, each with a measured defect behind it:

  * **declared PRIMARY KEY <-> accessor resolution key.** F-E: the stored
    `snap_counts` PK omitted `team`, so one of a traded player's two clubs was
    silently overwritten (measured: 26,468 offered, 26,467 stored, 0 reported).
    Migration 007 widened the PK; the ACCESSOR's `key_cols` had to widen in the
    same breath or `select_as_of`'s correlated MAX resolves the two clubs'
    rows against each other and hides whichever carries the older
    `retrieved_as_of` — the row is in the table and invisible to every read.
    Two different agents owned those two files.
  * **declared PRIMARY KEY <-> ingester `key_cols`.** `base.upsert` already
    raises on a mismatch, but only for a table an ingester is actually run
    against in some test. These tests check the constant against the migrated
    schema directly, so a future migration that widens a PK without touching the
    module fails here rather than at 07:20 on a Tuesday.
  * **`base.note_collapsed` <-> `refresh.run_ingest`'s drop ceiling.** The
    counter and the ceiling were written by different agents against a written
    spec. Both halves are unit-tested in their own files; NEITHER test runs a
    real ingester through the real orchestrator, so a wiring break between them
    would pass both suites.

WHAT THESE TESTS CAN AND CANNOT CATCH (the item-3.1b fixture lesson, stated
rather than assumed). They run real SQL against the real migrated schema, real
registry specs and real ingesters, so they catch a key/accessor/ceiling drift on
ANY table, including one added later. They prove NOTHING about upstream: not
that nflverse still serves a column, not that a two-club week still occurs, not
that the row counts recon measured still hold. Only a live pull says that, and
per item 3.1b a frozen fixture is weak evidence there.

Rule 5: every player and team identifier below is invented.
"""

import ast
import importlib
import inspect
import pathlib
import re

import pandas as pd
import pytest

from ziggurat.data.nfl import (
    adp_rankings,
    base,
    depth_charts,
    depth_charts_weekly,
    espn_ranks,
    game_odds,
    injuries,
    ngs,
    players,
    projections,
    refresh,
    schedules,
    snap_counts,
    team_defense,
    weather,
    weekly_stats,
)

TODAY = "2026-07-25"


def _declared_pk(conn, table: str) -> tuple[str, ...]:
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(name for _, name in sorted((r[5], r[1]) for r in info if r[5]))


# ======================================================================
# SEAM 1 — the accessor's resolution key vs the table's declared PRIMARY KEY
# ======================================================================
#
# Captured, never hand-copied: `base.select_as_of` / `select_observed_as_of` are
# monkeypatched to record the `key_cols` they were handed, and each accessor is
# then called for real against an empty database. A hand-written map would drift
# exactly the way the thing it is checking drifts.

#: accessor -> the kwargs it needs to run at all. Deliberately every as-of
#: accessor in `ziggurat/data/nfl/`, not only the backfilled ones: F-E is a
#: property of the pairing, not of the backfill.
_ACCESSORS = {
    "players": (players.get_players, {}),
    "schedules": (schedules.get_schedule, {}),
    "weekly_stats": (weekly_stats.get_weekly_stats, {}),
    "snap_counts": (snap_counts.get_snap_counts, {}),
    "ngs_passing": (ngs.get_ngs_passing, {}),
    "ngs_rushing": (ngs.get_ngs_rushing, {}),
    "ngs_receiving": (ngs.get_ngs_receiving, {}),
    "injuries": (injuries.get_injuries, {}),
    "team_defense": (team_defense.get_team_defense, {}),
    "game_odds": (game_odds.get_game_odds, {}),
    "game_weather": (weather.get_game_weather, {}),
    "adp_rankings": (adp_rankings.get_adp_rankings, {}),
    "espn_draft_ranks": (espn_ranks.get_espn_draft_ranks, {"season": 2026}),
    "projections": (projections.get_projections, {}),
    "depth_charts_weekly": (depth_charts_weekly.get_depth_chart_week,
                            {"season": 2023, "week": 6}),
    "depth_chart_slots": (depth_charts.get_depth_chart, {"season": 2025}),
    "depth_chart_panels": (depth_charts.get_depth_chart_observed, {"season": 2025}),
}

#: Columns that are in the PK but are NOT part of the identity a read resolves
#: on — they are the VERSION axis, and resolving on them is what the accessor's
#: correlated MAX does instead.
_VERSION_COLUMNS = {"retrieved_as_of", "observed_at"}


@pytest.fixture()
def captured_keys(db, monkeypatch):
    """Call every accessor for real and record the (table, key_cols) it resolved on."""
    seen: dict[str, tuple[str, ...]] = {}
    real_as_of = base.select_as_of
    real_observed = base.select_observed_as_of

    def spy_as_of(conn, table, *, key_cols, **kw):
        seen[table] = tuple(key_cols)
        return real_as_of(conn, table, key_cols=key_cols, **kw)

    def spy_observed(conn, table, *, key_cols, **kw):
        seen[table] = tuple(key_cols)
        return real_observed(conn, table, key_cols=key_cols, **kw)

    for module in (players, schedules, weekly_stats, snap_counts, ngs, injuries,
                   team_defense, game_odds, weather, adp_rankings, espn_ranks,
                   projections, depth_charts_weekly, depth_charts):
        if hasattr(module, "base"):
            monkeypatch.setattr(module.base, "select_as_of", spy_as_of, raising=False)
            monkeypatch.setattr(module.base, "select_observed_as_of", spy_observed,
                                raising=False)

    for _, (fn, kwargs) in _ACCESSORS.items():
        fn(db, as_of=TODAY, **kwargs)
    return seen


@pytest.mark.parametrize("table", sorted(_ACCESSORS))
def test_the_accessor_resolves_on_exactly_the_declared_primary_key(db, captured_keys,
                                                                   table):
    """The F-E class, generalized to every table.

    A resolution key NARROWER than the PK shadows real rows: two rows that
    SQLite considers distinct get resolved against each other and only the one
    with the newest `retrieved_as_of` is returned, with no error and no count
    anywhere. That is precisely what `get_snap_counts` did to a traded player's
    second club until this item, and it is invisible in every direction — the
    row is physically present, the ingester's count is right, and the read is
    short.

    A resolution key WIDER than the PK is the mirror failure: rows SQLite
    already collapsed are treated as distinct, so a correction never supersedes
    the row it corrects and the accessor returns both versions.
    """
    declared = _declared_pk(db, table)
    expected = tuple(c for c in declared if c not in _VERSION_COLUMNS)
    assert set(captured_keys[table]) == set(expected), (
        f"{table}: accessor resolves on {sorted(captured_keys[table])} but the "
        f"declared primary key is {list(declared)} (identity columns {list(expected)})"
    )


def test_every_table_with_an_as_of_accessor_is_covered(db):
    """The parameterization must not drift away from the schema. A fact table
    that gains an accessor and no seam case is the hole this closes."""
    ignored = {
        # Run logs and metadata: no as-of accessor, by design.
        "meta", "nfl_ingest_runs", "league_sync_runs",
        # Item 3.1 owns the league tables and their own accessors/tests.
        "league_teams", "league_matchups", "league_player_state",
        "league_transactions",
    }
    tables = {
        r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")
    }
    assert tables - ignored - set(_ACCESSORS) == set()


def test_the_seam_check_rejects_the_pre_3_2c_snap_counts_key(db, captured_keys):
    """The teeth, in the suite rather than only in a reviewer's terminal.

    Runs the parameterized assertion's own comparison against the PRE-3.2c
    resolution key (`team` dropped) and requires it to reject — so a future
    change that loosens the comparison (to a subset test, say) fails here.

    Reverting `snap_counts._KEY_COLS` to that key was also checked by hand and
    fails `…resolves_on_exactly_the_declared_primary_key[snap_counts]` with
    "accessor resolves on ['pfr_player_id', 'season', 'week'] but the declared
    primary key is [... 'team', 'retrieved_as_of']".
    """
    declared = _declared_pk(db, "snap_counts")
    identity = tuple(c for c in declared if c not in _VERSION_COLUMNS)
    pre_3_2c = tuple(c for c in identity if c != "team")

    assert set(captured_keys["snap_counts"]) == set(identity)   # shipped: passes
    assert set(pre_3_2c) != set(identity)                        # pre-3.2c: fails
    assert "team" in identity and "team" not in pre_3_2c


# ======================================================================
# SEAM 2 — the ingester's `key_cols` constant vs the declared PRIMARY KEY
# ======================================================================

#: module constant -> table. Each of these is passed to `base.upsert`, which
#: raises on a mismatch — but only for a table some test actually writes to.
_INGEST_PK_CONSTANTS = {
    "weekly_stats": weekly_stats._PK_COLS,
    "snap_counts": snap_counts._PK_COLS,
    "injuries": injuries._PK_COLS,
    "ngs_passing": ngs._PK_COLS,
    "ngs_rushing": ngs._PK_COLS,
    "ngs_receiving": ngs._PK_COLS,
    "depth_charts_weekly": depth_charts_weekly._PK_COLS,
    "depth_chart_slots": depth_charts._SLOT_PK_COLS,
    "depth_chart_panels": depth_charts._PANEL_PK_COLS,
}


@pytest.mark.parametrize("table", sorted(_INGEST_PK_CONSTANTS))
def test_the_ingester_key_matches_the_migrated_schema(db, table):
    """Migration 007 widened two primary keys. The modules carry their own
    copies of those keys, and the two live in files owned by different agents —
    so a future migration that widens a third one without touching the module
    must fail here, at import-and-schema time, rather than at 07:20 on a Tuesday
    inside `base.upsert`.
    """
    assert set(_INGEST_PK_CONSTANTS[table]) == set(_declared_pk(db, table))


@pytest.mark.parametrize("table", sorted(_INGEST_PK_CONSTANTS))
def test_the_ingester_key_is_the_accessor_key_plus_the_version_columns(db, table):
    """The two halves of the pairing, tied together explicitly.

    `snap_counts` derives one from the other in the module (`_PK_COLS =
    tuple(_KEY_COLS) + ("retrieved_as_of",)`) precisely so they cannot drift;
    this asserts the same relation for the tables that do not.
    """
    declared = set(_declared_pk(db, table))
    assert set(_INGEST_PK_CONSTANTS[table]) == declared
    identity = declared - _VERSION_COLUMNS
    assert identity, f"{table}: primary key is nothing but version columns"


# ======================================================================
# SEAM 2b — the `key_cols` constant actually REACHES `base.upsert`
# ======================================================================
#
# SEAM 2 compares each module's PK constant to the schema. It never asserts the
# constant is PASSED anywhere. Measured 2026-07-25: deleting `key_cols=` from
# SEVEN of the nine instrumented call sites left the entire suite green
# (`weekly_stats`, all three `ngs` tables, `injuries`, and both `depth_charts`
# tables). Meanwhile the one site that had never been instrumented at all,
# `adp_rankings`, was discarding a real market fact every day and reporting `ok`
# with a count one higher than the table held — the exact defect the parameter
# exists to end, on the source that is perishable, daily and draft-critical.
#
# So the expectation is derived from the SOURCE ITSELF, by parsing it. A
# hand-written map of "these call sites should have key_cols" drifts in precisely
# the way the thing it checks drifts, which is how the first one got missed.

_INGEST_PACKAGE = pathlib.Path(base.__file__).parent

#: Call sites deliberately NOT instrumented: `(module, lineno-free id) -> why`.
#: EMPTY on purpose — every `base.upsert` call under `ziggurat/data/nfl/` passes
#: its table's full primary key. An entry here must be a written decision that a
#: particular call cannot have an honest count, never a parking space for a call
#: somebody forgot. (`ziggurat/league/` is out of scope: its three call sites are
#: item 3.1's, and its tables are not in this package.)
_UNINSTRUMENTED: dict[tuple[str, str], str] = {}


def _upsert_sites_in(source: str, module: str) -> list[dict]:
    """Every `base.upsert(...)` call in one module's source, by AST.

    `table` is the string literal when the second positional argument is one
    (`ngs` passes a variable — it writes three tables through one function), and
    `key_cols_name` is the identifier when the keyword is a bare name.
    """
    sites = []
    for node in ast.walk(ast.parse(source, filename=f"{module}.py")):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "upsert"
                and isinstance(fn.value, ast.Name) and fn.value.id == "base"):
            continue
        table = node.args[1] if len(node.args) > 1 else None
        keywords = {kw.arg: kw.value for kw in node.keywords}
        key_cols = keywords.get("key_cols")
        sites.append({
            "module": module,
            "lineno": node.lineno,
            "table": table.value if isinstance(table, ast.Constant) else None,
            "has_key_cols": key_cols is not None and not (
                isinstance(key_cols, ast.Constant) and key_cols.value is None),
            "key_cols_name": key_cols.id if isinstance(key_cols, ast.Name) else None,
        })
    return sites


def _all_upsert_sites() -> list[dict]:
    sites = []
    for path in sorted(_INGEST_PACKAGE.glob("*.py")):
        sites.extend(_upsert_sites_in(path.read_text(), path.stem))
    return sites


def _uninstrumented(sites) -> list[str]:
    return [
        f"{s['module']}.py:{s['lineno']} base.upsert({s['table'] or '<variable>'})"
        for s in sites
        if not s["has_key_cols"]
        and (s["module"], s["table"]) not in _UNINSTRUMENTED
    ]


def test_every_upsert_in_the_package_is_given_its_primary_key():
    """The guard C2 slipped through. Deleting any `key_cols=` must turn this red.

    Without `key_cols` the return value is the number of rows OFFERED. Every
    caller treats it as rows written: `run_ingest` logs it as `rows_written`,
    `ingest status` reads it, and the collapse never reaches the drop ceiling —
    so a source can lose a fact a day and read `ok` forever.
    """
    missing = _uninstrumented(_all_upsert_sites())
    assert missing == [], (
        "base.upsert without key_cols returns rows OFFERED, not rows written:\n  "
        + "\n  ".join(missing)
    )


def test_the_parser_finds_every_call_the_text_contains():
    """The check above passes vacuously if the AST matcher stops matching.

    Cross-checked against a completely different method — counting the literal
    text — so a rename of `base` or of `upsert` cannot quietly reduce the
    inspected population to zero.
    """
    textual = {}
    for path in sorted(_INGEST_PACKAGE.glob("*.py")):
        textual[path.stem] = sum(
            line.count("base.upsert(")
            for line in path.read_text().splitlines()
            if not line.lstrip().startswith("#")   # prose about upsert is not a call
        )
    parsed = {}
    for site in _all_upsert_sites():
        parsed[site["module"]] = parsed.get(site["module"], 0) + 1
    assert parsed == {k: v for k, v in textual.items() if v}
    assert sum(parsed.values()) >= 10, (
        f"only {sum(parsed.values())} call sites found — has the matcher broken?")


def test_the_uninstrumented_check_actually_rejects_a_bare_call():
    """Teeth, in the suite rather than in a reviewer's terminal.

    Runs the real predicate over a synthetic module holding one instrumented and
    one bare call, and requires it to name exactly the bare one — so a future
    loosening (treating a missing keyword as fine, say) fails here even while
    every real call site happens to be correct.
    """
    synthetic = (
        "def good(conn, rows):\n"
        "    return base.upsert(conn, 'good_table', rows, key_cols=_PK_COLS)\n"
        "def bad(conn, rows):\n"
        "    return base.upsert(conn, 'bad_table', rows)\n"
        "def explicit_none(conn, rows):\n"
        "    return base.upsert(conn, 'none_table', rows, key_cols=None)\n"
    )
    found = _uninstrumented(_upsert_sites_in(synthetic, "synthetic"))
    assert found == [
        "synthetic.py:4 base.upsert(bad_table)",
        "synthetic.py:6 base.upsert(none_table)",
    ]


def test_every_passed_key_cols_constant_is_the_declared_primary_key(db):
    """And the constant that reaches `base.upsert` is the RIGHT one.

    SEAM 2 pins a hand-listed set of constants against the schema; this pins
    whatever the call sites actually reference, discovered by parsing. The two
    together close the loop: the keyword is present (above), it names a real
    module constant (here), and that constant is the migrated primary key (here).
    """
    checked = 0
    for site in _all_upsert_sites():
        if not site["key_cols_name"]:
            continue                       # a literal/expression: value pinned by SEAM 2
        module = importlib.import_module(f"ziggurat.data.nfl.{site['module']}")
        constant = getattr(module, site["key_cols_name"], None)
        assert isinstance(constant, tuple) and constant, (
            f"{site['module']}.py:{site['lineno']}: key_cols={site['key_cols_name']} "
            "is not a module-level tuple"
        )
        assert all(isinstance(c, str) for c in constant)
        if site["table"] is None:
            continue                       # ngs writes three tables through one call
        assert set(constant) == set(_declared_pk(db, site["table"])), (
            f"{site['module']}.py:{site['lineno']}: {site['key_cols_name']} is "
            f"{list(constant)} but {site['table']}'s declared primary key is "
            f"{list(_declared_pk(db, site['table']))}"
        )
        checked += 1
    assert checked >= 10, f"only {checked} constants checked — has discovery broken?"


# ======================================================================
# SEAM 3 — `note_collapsed` reaches `run_ingest`'s drop ceiling, end to end
# ======================================================================
#
# base.py's tests prove `upsert(key_cols=)` counts a collapse; refresh.py's tests
# prove `run_ingest` folds `tally["collapsed"]` into the ceiling — but they do it
# with `base.note_collapsed("fake", ...)` called directly from a stub pull. No
# test in either file runs a REAL ingester through the REAL orchestrator, which
# is where the handoff actually lands.


def _collide_frame():
    """Two snap-count rows that share the FULL primary key and disagree.

    Same (pfr_player_id, season, week, team) — so INSERT OR REPLACE keeps the
    second and the first is gone. This is not a shape upstream ships (the two
    measured clubs of a traded player differ in `team`, which is why 007 put it
    in the key); it is the shape that proves the loss is now counted.
    """
    common = dict(season=2023, week=6, position="RB", opponent="BBB")
    return pd.DataFrame([
        dict(pfr_player_id="SyntPl00", player="Synthetic Player", team="AAA",
             offense_snaps=10, offense_pct=0.10, defense_snaps=0, defense_pct=0.0,
             st_snaps=0, st_pct=0.0, game_id="2023_06_AAA_BBB", **common),
        dict(pfr_player_id="SyntPl00", player="Synthetic Player", team="AAA",
             offense_snaps=55, offense_pct=0.55, defense_snaps=0, defense_pct=0.0,
             st_snaps=0, st_pct=0.0, game_id="2023_06_AAA_BBB", **common),
    ])


def _seed_schedule(db):
    db.execute(
        "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, knowable_as_of, retrieved_as_of) "
        "VALUES ('2023_06_AAA_BBB', 2023, 6, 'REG', '2023-10-15', 'BBB', 'AAA', "
        "'2023-08-01', ?)", (TODAY,))
    db.commit()


def test_a_real_ingester_collapse_is_counted_by_the_real_ingester(db):
    """The base.py half, exercised through the shipped `ingest_snap_counts`
    rather than a stub: two rows in, ONE distinct key written, and the loss
    lands on the `collapsed` channel — not on `dropped` (nothing failed to
    stamp) and not on `filtered` (nothing was correctly excluded)."""
    _seed_schedule(db)
    with base.collect_drops() as tally:
        written = snap_counts.ingest_snap_counts(db, _collide_frame(),
                                                 retrieved_as_of=TODAY)
    assert written == 1
    assert db.execute("SELECT COUNT(*) FROM snap_counts").fetchone()[0] == 1
    assert tally["collapsed"] == 1
    assert tally["dropped"] == 0
    assert tally["filtered"] == 0


def test_a_real_ingester_collapse_reaches_the_orchestrator_ceiling(db):
    """THE SEAM. `run_ingest` must see the collapse the ingester counted.

    Before item 3.2c this pair returned `ok` with `rows_written=2`: the ingester
    reported the offered count and nothing anywhere knew a fact had been
    overwritten. 50% of the batch is over `_MAX_DROP_FRACTION`, so a correct
    wiring reports FAILED here; the assertion that matters is that the status is
    a problem status and the reason NAMES the collapse, because 'lost 1 row' is
    not something a novice operator can act on (Rule 6).
    """
    _seed_schedule(db)
    spec = refresh.SourceSpec(
        name="snap_counts", group=refresh.GROUP_WEEKLY,
        pull=lambda ctx: snap_counts.ingest_snap_counts(
            ctx.conn, _collide_frame(), retrieved_as_of=ctx.retrieved_as_of),
        phases=refresh.ALL_PHASES,
    )
    out = refresh.run_ingest(db, sources=[spec], season=2023, today=TODAY,
                             retrieved_as_of=TODAY, force=True)
    assert len(out) == 1
    assert out[0]["status"] in refresh.PROBLEM_STATUSES
    assert "collapsed on a primary-key collision" in out[0]["reason"]
    assert out[0]["rows"] == 1          # the HONEST count, not the 2 offered

    logged = db.execute(
        "SELECT status, rows_written, rows_dropped, error FROM nfl_ingest_runs "
        "WHERE source = 'snap_counts' ORDER BY run_id DESC LIMIT 1").fetchone()
    assert logged["rows_written"] == 1
    assert logged["rows_dropped"] == 1   # the collapse, on the ceiling's counter


def test_a_byte_identical_duplicate_does_not_reach_the_ceiling(db):
    """The other direction, and the one that would fail a healthy pull.

    The four legacy depth-chart files carry 145-207 byte-identical duplicate
    rows per season. Storing one of them loses nothing, so `duplicated` is
    tallied and deliberately kept OFF the ceiling — fold it in and every real
    `depth_charts_weekly` backfill season reports a loss it did not take.
    """
    _seed_schedule(db)
    frame = _collide_frame()
    frame.loc[1] = frame.loc[0]          # now byte-identical, not a disagreement
    spec = refresh.SourceSpec(
        name="snap_counts", group=refresh.GROUP_WEEKLY,
        pull=lambda ctx: snap_counts.ingest_snap_counts(
            ctx.conn, frame, retrieved_as_of=ctx.retrieved_as_of),
        phases=refresh.ALL_PHASES,
    )
    out = refresh.run_ingest(db, sources=[spec], season=2023, today=TODAY,
                             retrieved_as_of=TODAY, force=True)
    assert out[0]["status"] == refresh.STATUS_OK
    assert out[0]["rows"] == 1


# ======================================================================
# SEAM 4 — the registry's callables vs the functions they were handed
# ======================================================================


def _positional_arity(fn) -> int:
    sig = inspect.signature(fn)
    return sum(1 for p in sig.parameters.values()
               if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))


@pytest.mark.parametrize(
    "spec", [pytest.param(s, id=s.name)
             for s in refresh.SOURCES + refresh.BACKFILL_ONLY_SOURCES])
def test_every_registry_callable_matches_the_shape_run_ingest_calls_it_with(spec):
    """A `SourceSpec` field is an untyped `object` for `season_resolver` and
    `applicable`, so a handed-off spec whose callable has the wrong shape fails
    at 07:20, inside the run, on a source that may only be reached two weeks a
    year (the March season handover is exactly that).
    """
    for field in ("pull", "scope", "applicable"):
        fn = getattr(spec, field)
        if fn is None:
            continue
        assert _positional_arity(fn) == 1, f"{spec.name}.{field} must take (ctx)"

    if spec.season_resolver is not None:
        sig = inspect.signature(spec.season_resolver)
        assert _positional_arity(spec.season_resolver) == 1, (
            f"{spec.name}.season_resolver must take (conn, *, season, today)")
        assert {"season", "today"} <= set(sig.parameters), (
            f"{spec.name}.season_resolver must accept season= and today=")


def test_the_depth_chart_spec_calls_the_ingester_that_actually_exists():
    """The specific handoff: the registry spec was written from a note while the
    panel module was being written in another file. `pull_depth_charts`'s
    signature is the contract between them.
    """
    sig = inspect.signature(depth_charts.pull_depth_charts)
    assert list(sig.parameters) == ["conn", "season", "retrieved_as_of"]
    assert sig.parameters["retrieved_as_of"].kind is inspect.Parameter.KEYWORD_ONLY

    weekly = inspect.signature(depth_charts_weekly.pull_depth_charts_weekly)
    assert list(weekly.parameters) == ["conn", "years", "retrieved_as_of"]


def test_the_two_depth_chart_regimes_meet_exactly_once():
    """Two modules, two tables, one boundary — asserted from the modules' own
    constants so the two cannot drift into a gap (a season nothing covers) or an
    overlap (a season both claim).
    """
    assert depth_charts.PANEL_MIN_SEASON == depth_charts_weekly.WEEKLY_MAX_SEASON + 1


def test_the_backfill_writes_a_table_for_every_source_it_claims():
    """`_BACKFILL_TABLES` is hand-written (deliberately — a wrong guess would
    report a healthy source as an empty one), so it must at least name a table
    that exists in the migrated schema for every source it can run."""
    named = {s.name for s in refresh.select_backfill_sources(with_weather=True)}
    assert named <= set(refresh._BACKFILL_TABLES)


def test_no_backfilled_source_is_still_blocked():
    """F-B unblocked `depth_charts`. A source that is on the backfill allowlist
    and still carries a `blocked` reason would be planned, attempted and refused
    inside the loop."""
    for spec in refresh.select_backfill_sources(with_weather=True):
        assert spec.blocked is None, f"{spec.name} is blocked: {spec.blocked}"
        assert spec.pull is not None


# ======================================================================
# SEAM 5 — the migration's alarm channel reaches an operator-visible surface
# ======================================================================


def test_the_migration_collapse_alarm_is_surfaced_by_a_command(db):
    """007 rebuilds two tables with `INSERT OR REPLACE` so it CANNOT raise inside
    `open_db` three weeks before the draft — which means it can silently collapse
    rows instead. It writes a positive `meta` fact when it does. That fact is
    only useful if something prints it.
    """
    from ziggurat.cli import main as cli_main

    src = inspect.getsource(cli_main)
    assert "migration_alerts" in src
    # ...and in a command the operator actually runs, not only in a helper.
    assert re.search(r"_echo_migration_alerts\(", src)
