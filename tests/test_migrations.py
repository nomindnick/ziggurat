"""Migration-runner and schema-shape tests (item 3.2c).

WHY THIS FILE EXISTS. 3.2c's recon found a CRITICAL defect by running the real
migration runner rather than by reading the migrations: the item's two halves had
each written their own ``db/migrations/007_*.sql``, and ``store._migrations()``
raises on non-contiguous versions. ``apply_schema`` is called UNCONDITIONALLY by
``open_db`` — whose own docstring calls it "the safe default for any command" —
so two files numbered 007 take out every command that opens the database that
way, three weeks before the draft, with nothing in the suite failing.

MEASURED BLAST RADIUS, since the recon note over-stated it in one direction and
under-stated a different problem. With a second 007 staged in `db/migrations/`,
`ziggurat ingest status` exits 1 with the RuntimeError, and `marginal`,
`league *`, `ingest *` and `db init` go with it. `draft-board`, `draft-web`,
`valuation`, `divergence` and `mock-draft` DO NOT — they open the database with
bare ``store.connect`` (cli/main.py:114, 161, 300, 367), which never migrates.
Verified by launching `ziggurat draft-web` with the collision present: it printed
its banner and bound its port. So the cockpit is less exposed than the note
assumed, and the flip side is that the cockpit never applies a migration either.

So the load-bearing test here is dumb and cheap: **the SHIPPED migrations
directory must apply.** It is paired with a test that proves it has teeth (add a
colliding file, watch it raise) — a green assertion nobody has watched fail is
worth very little (the 3.1b frozen-fixture lesson).

WHAT THESE TESTS CAN AND CANNOT CATCH. They exercise the real
``db/migrations/*.sql`` against real SQLite, so they catch numbering collisions,
SQL syntax errors, a migration written against a shape ``db/schema.sql`` does not
produce, PK/index regressions, and row loss in a table rebuild. They cannot
catch anything about UPSTREAM data — whether a column nflverse serves still
exists, or whether an ingester writes the columns these tables declare. That is
the ingester tests' job, and per 3.1b a frozen fixture is weak evidence there.
"""

import hashlib
import re
import shutil

import pytest

from ziggurat.data.store import (
    BUSY_TIMEOUT_MS,
    apply_schema,
    connect,
    migration_alerts,
    open_db,
)
from ziggurat.paths import MIGRATIONS_DIR, SCHEMA_PATH

#: Bump with every migration. A literal, not a computed value: if this file
#: derived the number from the directory it would agree with any mistake.
LATEST_SCHEMA_VERSION = 19

#: sha256 of every shipped migration. Pinned as literals for the reason spelled
#: out in `test_an_applied_migration_is_never_edited` — this is the guard against
#: the trap the 3.2c audit round set for itself, and it is the only place in the
#: repo where content drift in an applied migration is visible at all.
MIGRATION_DIGESTS: dict[str, str] = {
    "002_temporal_indexes.sql":
        "e64529f45ca85485ec5c5765d83017fff1cf8592856d3bd38fae702980088bf5",
    "003_market_context.sql":
        "0c849eeb4fb3a794bb31052dc69bf5f0922256cee17bf094e5f2b611d7facb4b",
    "004_espn_ranks.sql":
        "184d2db2ab3ecd2074e21cad98e6e676bda90aed9920df969d50b0e5616345b4",
    "005_league_state.sql":
        "4db58e4900a2d83e7f6f62dc5eca7b26b35d91ca3132b18677aefdcec8b0538a",
    "006_nfl_ingest_runs.sql":
        "d50729ea3e8c25d72b890546bb2b15dc78b72403032e43e0cc0668505a2c8ab6",
    "007_backfill_and_depth_charts.sql":
        "c207a857f0e6304be3dcfce1e73248dacb0f735d53af1c0e1eb5dce614163448",
    "008_push_layer.sql":
        "5e6c32ebd7dbe4c9115dfed931a64afd0a7bfc69b0977d51364b092bc7934dd4",
    "009_espn_projections.sql":
        "cfcf9de6db318f91605f3573434b303cf7f2dd04993cfd3193c5d82c3ab0e7b5",
    "010_espn_projections_source_key.sql":
        "946e79431899c4a8fc944d5856b6f9ad287a5670ad68a9a0d0289db6c3deb882",
    "011_fpecr_panel.sql":
        "5a7fbe9b0c01c1d23a8ab0d9aa29f7644164c2dedf38441de89e54da87511287",
    "012_sleeper_ownership.sql":
        "58065eaafef5a1adce6baad14c4c9603a954d7008ed2693b4d5c9f9e37f78ab0",
    "013_weekly_stats_kicking.sql":
        "158fe1778ebf88e8b000c86cbfc29085364f7967a1ef8ff00fc3c32bbf1d9d7b",
    "014_league_ground_truth.sql":
        "3f7b424cbba214fd98e78296989f4661492b73e82fa42a8d159c5e7485ef6d0d",
    "015_ff_opportunity.sql":
        "312381828e81bbf7636e31ca841b0cdc09d489ea9cb55651654e9c723c3a32c4",
    "016_fp_weekly_ecr.sql":
        "b063e384f63bfbd39295dca1e8d81b94e9eca6b15f4851c6db9d09cb15e29fdf",
    "017_league_acquisition_time.sql":
        "fb5a5e851761a1c6494c5fc62fd4b5a1b2897735ad2a0a12e318aef1a2e1e565",
    "018_decision_freezes.sql":
        "4f48b58c589e9175dc90fc908299efbec969d359b70a25cf5f78471fadb91b1d",
    "019_gsis_placeholder_rekey.sql":
        "aa87ac6768eadcd336dbcd8241088836eaa27fa12f9e5e4a5983a59e81b8ea88",
}

#: The doctrine, printed by the test that enforces it. Long on purpose: the next
#: session to meet this failure will be mid-fix and will otherwise reach for the
#: obvious wrong move, which is to update the digest.
_APPLIED_MIGRATION_RULE = """
AN APPLIED MIGRATION IS NEVER RE-APPLIED — SHIP THE CORRECTION AS A NEW FILE.

`apply_schema` skips every migration whose number is <= the database's stored
`schema_version`, and it compares NUMBERS ONLY. It cannot see that a file's
CONTENT changed. So editing a migration that has already run:

  * changes what a FRESH database gets,
  * leaves every already-migrated database exactly as it was, permanently,
  * raises nothing, alerts nothing, and
  * makes 100% of this test suite agree with the file while 0% of it agrees with
    the operator's actual database.

Measured on this repo (3.2c audit C6): a column and a table appended to an
edited 007 -> fresh build has them, the live database (already at 7) does not,
`migration_alerts()` empty on both, no raise anywhere. The worst shape is not a
missing column (that at least raises inside a timer) but an edited PRIMARY KEY:
the live table keeps the old key and silently collapses rows while every test
proves it cannot.

WHAT TO DO INSTEAD: write `db/migrations/00N_<name>.sql` with the next number and
bump LATEST_SCHEMA_VERSION here. EXACTLY ONE FILE PER NUMBER — two files sharing
one make `store._migrations()` raise inside `open_db`, which takes down every
command that opens the database, including `ziggurat league status` (the alarm
for the one dataset whose lost day is unrecoverable).

If you are ADDING a migration, add its digest to MIGRATION_DIGESTS above.
If you are legitimately editing a migration that has NEVER run anywhere — a file
you added in this same uncommitted change, before any timer touched it — update
its digest, and be certain about "never ran anywhere": `db/ziggurat.sqlite` on
the operator's box is migrated by a systemd timer, not by hand.
"""


def _at_version(conn, target: int) -> None:
    """Bootstrap `conn` to exactly schema_version `target` by hand, so a test can
    migrate FORWARD from a realistic older database."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for version in range(2, target + 1):
        path = next(MIGRATIONS_DIR.glob(f"{version:03d}_*.sql"))
        conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(target),)
    )
    conn.commit()


# =====================================================================
# The CRITICAL defect: the shipped migrations directory must apply.
# =====================================================================


def test_shipped_migrations_directory_applies(tmp_path):
    """THE test. Runs the real runner over the real `db/migrations/` on a real
    file, exactly as `open_db` does on every single CLI command."""
    conn = open_db(tmp_path / "fresh.sqlite")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == str(LATEST_SCHEMA_VERSION)
    conn.close()


def test_a_colliding_migration_number_is_rejected(tmp_path):
    """Proof the test above has teeth: recreate the exact defect (two files
    numbered 007) and watch the runner refuse. If this ever stops raising, the
    test above stops protecting anything."""
    staged = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, staged)
    collision = staged / "007_depth_chart_panel.sql"   # the second designer's filename
    collision.write_text("SELECT 1;\n", encoding="utf-8")

    conn = connect(":memory:")
    with pytest.raises(RuntimeError, match="contiguous"):
        apply_schema(conn, migrations_dir=staged)

    collision.unlink()
    apply_schema(conn, migrations_dir=staged)          # …and passes once it is gone
    assert conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"] == str(LATEST_SCHEMA_VERSION)


def test_an_applied_migration_is_never_edited():
    """THE C6 GUARD, and the one this audit round set for itself.

    The natural response to "007 has a defect and it isn't committed yet" is to
    edit 007. On the operator's box a systemd timer had ALREADY applied it —
    `db/ziggurat.sqlite` reads `schema_version 7` — so that edit would have
    diverged the live database from the file with nothing anywhere raising.

    The audit's own recommendation was explicitly NOT to make `apply_schema` raise
    on content drift: it migrates on every command, so a raise would brick the
    cadence the moment anyone reflowed a comment. A pinned digest in the suite is
    the version of that check which fails on a developer's machine instead of in a
    timer at 07:28.
    """
    shipped = {p.name for p in MIGRATIONS_DIR.glob("*.sql")}
    unpinned = shipped - set(MIGRATION_DIGESTS)
    assert not unpinned, (
        f"migration(s) with no pinned digest: {sorted(unpinned)}\n{_APPLIED_MIGRATION_RULE}"
    )
    missing = set(MIGRATION_DIGESTS) - shipped
    assert not missing, (
        f"pinned migration(s) missing from db/migrations/: {sorted(missing)} — a "
        "migration file was DELETED. Every database that already applied it keeps its "
        "effects and reports a schema_version the runner then calls 'newer than "
        "supported', which is `RuntimeError` on every command that opens the database."
    )
    for name, expected in sorted(MIGRATION_DIGESTS.items()):
        actual = hashlib.sha256((MIGRATIONS_DIR / name).read_bytes()).hexdigest()
        assert actual == expected, (
            f"db/migrations/{name} CHANGED (sha256 {actual[:16]}…, expected "
            f"{expected[:16]}…).\n{_APPLIED_MIGRATION_RULE}"
        )


def test_every_migration_filename_is_parseable_and_contiguous():
    """A misnamed file (`007-foo.sql`, `7_foo.sql`, `007_Foo.sql`) raises inside
    `_migrations` for a *different* reason than a collision; assert the shipped
    names satisfy the pattern so the collision test is the only way in."""
    from ziggurat.data.store import _MIGRATION_NAME, _migrations

    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert names, "no migrations found — MIGRATIONS_DIR is wrong"
    for name in names:
        assert _MIGRATION_NAME.fullmatch(name), name
    versions = [v for v, _ in _migrations(MIGRATIONS_DIR)]
    assert versions == list(range(2, LATEST_SCHEMA_VERSION + 1))


# =====================================================================
# Migration 007 — the shape it must produce
# =====================================================================


def _pk_columns(conn, table: str) -> list[str]:
    rows = [r for r in conn.execute(f"PRAGMA table_info({table})") if r["pk"]]
    return [r["name"] for r in sorted(rows, key=lambda r: r["pk"])]


@pytest.fixture()
def migrated(tmp_path):
    conn = open_db(tmp_path / "migrated.sqlite")
    yield conn
    conn.close()


def test_007_creates_the_depth_chart_v2_tables(migrated):
    tables = {r["name"] for r in migrated.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"depth_chart_panels", "depth_chart_slots"} <= tables

    # The slot key is the SLOT, never the occupant: that is what makes a
    # tombstone (espn_id IS NULL) expressible at all.
    assert _pk_columns(migrated, "depth_chart_slots") == [
        "season", "team", "pos_grp_id", "pos_id", "pos_rank", "observed_at", "retrieved_as_of",
    ]
    assert "espn_id" not in _pk_columns(migrated, "depth_chart_slots")
    assert "gsis_id" not in _pk_columns(migrated, "depth_chart_slots")
    assert _pk_columns(migrated, "depth_chart_panels") == [
        "season", "observed_at", "retrieved_as_of",
    ]

    indexes = {r["name"] for r in migrated.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_depth_chart_slots_lookup" in indexes
    assert "idx_depth_chart_slots_player" in indexes


def test_a_tombstone_row_is_storable(migrated):
    """`espn_id IS NULL` MEANS "this slot was vacated" — the same positive-fact
    shape as league_player_state's `on_team_id IS NULL`. If espn_id were ever
    made NOT NULL, or promoted into the key, vacancies become inexpressible and
    the accessor resurrects ghosts (measured: a phantom rank-4 carried forward
    seven weeks)."""
    migrated.execute(
        "INSERT INTO depth_chart_slots (season, team, pos_grp_id, pos_id, pos_rank, "
        "observed_at, pos_abb, espn_id, retrieved_as_of, knowable_as_of) "
        "VALUES (2025, 'CIN', '21', '8', 4, '2025-09-17T07:14:22Z', 'QB', NULL, "
        "'2026-07-25', '2025-09-17')"
    )
    row = migrated.execute("SELECT espn_id FROM depth_chart_slots").fetchone()
    assert row["espn_id"] is None


def test_two_scheme_fronts_at_one_instant_do_not_collide(migrated):
    """`pos_grp_id` is in the key because nine pos_abb values (SLB, LDE, RDE, NB,
    FS, SS, RCB, LCB, WLB) appear in BOTH base defenses. Today no team carries
    two fronts in one snapshot, but nine teams switched scheme during 2025 — the
    collapse is latent, not impossible."""
    common = ("2025", "'MIA'")
    for grp in ("15", "16"):     # 3-4 front and 4-3 front, same instant
        migrated.execute(
            "INSERT INTO depth_chart_slots (season, team, pos_grp_id, pos_id, pos_rank, "
            "observed_at, pos_abb, espn_id, retrieved_as_of, knowable_as_of) "
            f"VALUES ({common[0]}, {common[1]}, '{grp}', '30', 1, "
            "'2025-09-17T07:14:22Z', 'SLB', '999', '2026-07-25', '2025-09-17')"
        )
    assert migrated.execute("SELECT COUNT(*) AS n FROM depth_chart_slots").fetchone()["n"] == 2


def test_007_renames_depth_charts_to_weekly_and_fixes_its_key(migrated):
    tables = {r["name"] for r in migrated.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "depth_charts_weekly" in tables
    assert "depth_charts" not in tables, (
        "the legacy regime is renamed, not kept alongside — one name per regime"
    )
    # game_type and depth_team are the additions: without depth_team the old key
    # collapsed ~700 rows a season that differ ONLY in the depth ORDER.
    pk = _pk_columns(migrated, "depth_charts_weekly")
    assert pk == ["season", "week", "game_type", "club_code", "formation", "position",
                  "depth_position", "depth_team", "gsis_id", "retrieved_as_of"]

    indexes = {r["name"] for r in migrated.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_depth_charts_weekly_lookup" in indexes
    assert "idx_depth_charts_lookup" not in indexes, "stale index on a dropped table"


def test_depth_team_alone_no_longer_collapses_a_row(migrated):
    """The measured warrant for adding depth_team to the key: two rows differing
    only in the depth ORDER are two facts, and the old PK kept one of them while
    the ingester reported writing both."""
    def row(depth_team):
        return ("00-0031234", 2023, 5, "REG", "MIA", "WR", "WR1", depth_team,
                "3WR 1TE", "A Receiver", "2026-07-25", "2023-10-08")
    migrated.executemany(
        "INSERT OR REPLACE INTO depth_charts_weekly (gsis_id, season, week, game_type, "
        "club_code, position, depth_position, depth_team, formation, full_name, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [row("1"), row("2")],
    )
    assert migrated.execute(
        "SELECT COUNT(*) AS n FROM depth_charts_weekly").fetchone()["n"] == 2


# ------------------------------------------------- F-E: the snap_counts PK


def test_snap_counts_pk_holds_a_two_team_week(migrated):
    """Finding F-E, measured twice independently: pfr id `DaviJa06` (Jalen Davis)
    played 10 defensive snaps for MIA *and* 23 for CIN in 2021 week 12. Under the
    old PK (pfr_player_id, season, week, retrieved_as_of) those were the same key,
    so INSERT OR REPLACE kept ONE — the ingester returned 26,468 rows, the table
    received 26,467, and `note_drops` reported 0. Silent loss of a real fact.
    """
    assert _pk_columns(migrated, "snap_counts") == [
        "pfr_player_id", "season", "week", "team", "retrieved_as_of",
    ]
    migrated.executemany(
        "INSERT OR REPLACE INTO snap_counts (pfr_player_id, gsis_id, player, position, team, "
        "opponent, season, week, game_id, defense_snaps, retrieved_as_of, knowable_as_of) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("DaviJa06", "00-0034428", "Jalen Davis", "CB", "MIA", "CAR", 2021, 12,
             "2021_12_MIA_CAR", 10.0, "2026-07-25", "2021-11-28"),
            ("DaviJa06", "00-0034428", "Jalen Davis", "CB", "CIN", "PIT", 2021, 12,
             "2021_12_CIN_PIT", 23.0, "2026-07-25", "2021-11-28"),
        ],
    )
    stored = {r["team"]: r["defense_snaps"] for r in migrated.execute(
        "SELECT team, defense_snaps FROM snap_counts WHERE pfr_player_id = 'DaviJa06'")}
    assert stored == {"MIA": 10.0, "CIN": 23.0}

    indexes = {r["name"] for r in migrated.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_snap_counts_lookup" in indexes, "the rebuild must recreate migration 002's index"


def test_013_adds_nullable_kicking_columns_to_weekly_stats(migrated):
    """Item 4.1 §7.1. The eight nflverse kicking buckets exist on weekly_stats,
    every one NULLABLE — a row retrieved before the migration reads NULL there,
    and NULL means NOT CAPTURED (never zero) — and the primary key is untouched,
    so a re-pull versions the uncaptured row instead of editing it."""
    from ziggurat.data.nfl.weekly_stats import KICKING_COLUMNS

    info = {r["name"]: r for r in migrated.execute("PRAGMA table_info(weekly_stats)")}
    assert set(KICKING_COLUMNS) <= set(info), sorted(set(KICKING_COLUMNS) - set(info))
    for col in KICKING_COLUMNS:
        assert info[col]["type"] == "INTEGER"
        assert info[col]["notnull"] == 0, f"{col} must be nullable: NULL = not captured"
        assert info[col]["pk"] == 0
    assert _pk_columns(migrated, "weekly_stats") == [
        "player_id", "season", "week", "retrieved_as_of",
    ]

    # The pre-013 row shape still inserts (the 2021-2025 partitions stamped
    # 2026-07-25 have exactly this shape) and reads NULL in all eight.
    migrated.execute(
        "INSERT INTO weekly_stats (player_id, season, week, season_type, position, "
        "recent_team, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?)",
        ("00-0025565", 2023, 5, "REG", "K", "TEN", "2026-07-25", "2023-10-08"))
    row = migrated.execute(
        "SELECT * FROM weekly_stats WHERE player_id = '00-0025565'").fetchone()
    assert all(row[col] is None for col in KICKING_COLUMNS)


def test_014_adds_nullable_ground_truth_columns_and_the_settings_table(migrated):
    """Item 3.8a. Three NULLABLE columns on league_player_state — NULL means NOT
    CAPTURED, never False — plus the league_settings table, keyed one row per
    (season, snapshot day) like league_teams.

    CATCHES: a NOT NULL / DEFAULT 0 on `injured` or `droppable`, which would turn
    "we do not know" into "healthy" / "drop away" on all 41 pre-014 snapshot days;
    and a primary key that is not the day partition (the ingest deletes and
    rewrites `(season, retrieved_as_of)`).
    """
    info = {r["name"]: r for r in migrated.execute("PRAGMA table_info(league_player_state)")}
    for col, kind in (("injured", "INTEGER"), ("droppable", "INTEGER"),
                      ("entry_injury_status", "TEXT")):
        assert col in info, col
        assert info[col]["type"] == kind
        assert info[col]["notnull"] == 0, f"{col} must be nullable: NULL = not captured"
        assert info[col]["pk"] == 0
        assert info[col]["dflt_value"] is None, f"{col} must have no default"
    assert _pk_columns(migrated, "league_player_state") == [
        "season", "espn_player_id", "retrieved_as_of",
    ]

    # The pre-014 row shape still inserts and reads NULL in all three.
    migrated.execute(
        "INSERT INTO league_player_state (season, espn_player_id, player, position, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?)",
        (2026, "9999", "Pre-014 Player", "WR", "2026-08-01", "2026-08-01"))
    row = migrated.execute(
        "SELECT * FROM league_player_state WHERE espn_player_id = '9999'").fetchone()
    assert row["injured"] is None
    assert row["droppable"] is None
    assert row["entry_injury_status"] is None

    assert _pk_columns(migrated, "league_settings") == ["season", "retrieved_as_of"]
    settings_cols = {r["name"] for r in migrated.execute("PRAGMA table_info(league_settings)")}
    from ziggurat.league.state import _SETTINGS_COLUMNS
    assert set(_SETTINGS_COLUMNS) | {"retrieved_as_of", "knowable_as_of"} == settings_cols


def test_the_old_snap_counts_key_really_did_lose_the_row(tmp_path):
    """The other half of the F-E proof: at schema 6 the same two rows collapse to
    one. Without this, `test_snap_counts_pk_holds_a_two_team_week` is only
    asserting that a five-column key works, not that it fixed anything."""
    conn = connect(":memory:")
    _at_version(conn, 6)
    conn.executemany(
        "INSERT OR REPLACE INTO snap_counts (pfr_player_id, team, season, week, defense_snaps, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?)",
        [
            ("DaviJa06", "MIA", 2021, 12, 10.0, "2026-07-25", "2021-11-28"),
            ("DaviJa06", "CIN", 2021, 12, 23.0, "2026-07-25", "2021-11-28"),
        ],
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM snap_counts").fetchone()["n"] == 1


# ------------------------------------------- the rebuild on a POPULATED table


def test_007_rebuild_preserves_every_row_and_value(tmp_path):
    """SQLite cannot ALTER a PRIMARY KEY, so 007 rebuilds two tables. On this box
    both are 0 rows — which is exactly the condition under which a rebuild bug
    ships unnoticed. Populate a schema-6 database first, then migrate."""
    conn = connect(tmp_path / "populated.sqlite")
    _at_version(conn, 6)

    depth = [(f"00-{i:07d}", 2023, week, "REG", club, "WR", f"WR{rank}", str(rank),
              "3WR 1TE", f"Player {i}", "2026-07-25", "2023-09-10")
             for i in range(200) for week in (1, 2) for club in ("MIA", "CIN") for rank in (1, 2)]
    conn.executemany(
        "INSERT OR REPLACE INTO depth_charts (gsis_id, season, week, game_type, club_code, "
        "position, depth_position, depth_team, formation, full_name, retrieved_as_of, "
        "knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", depth)
    # One team per player, so the row count is unambiguous under BOTH keys (the
    # two-team collision is asserted on its own in the F-E tests above).
    snaps = [(f"Ply{i:05d}", f"00-{i:07d}", f"P{i}", "WR", ("MIA", "CIN")[i % 2], "OPP",
              2023, week, "gid", 12.0, 0.42, "2026-07-25", "2023-09-10")
             for i in range(200) for week in (1, 2)]
    conn.executemany(
        "INSERT OR REPLACE INTO snap_counts (pfr_player_id, gsis_id, player, position, team, "
        "opponent, season, week, game_id, offense_snaps, offense_pct, retrieved_as_of, "
        "knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", snaps)
    conn.commit()

    before_depth = conn.execute("SELECT COUNT(*) AS n FROM depth_charts").fetchone()["n"]
    before_snaps = conn.execute("SELECT COUNT(*) AS n FROM snap_counts").fetchone()["n"]
    sample = dict(conn.execute(
        "SELECT * FROM snap_counts WHERE pfr_player_id = 'Ply00042' AND team = 'MIA' "
        "AND week = 2").fetchone())

    apply_schema(conn)

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM depth_charts_weekly").fetchone()["n"] == before_depth
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM snap_counts").fetchone()["n"] == before_snaps
    assert dict(conn.execute(
        "SELECT * FROM snap_counts WHERE pfr_player_id = 'Ply00042' AND team = 'MIA' "
        "AND week = 2").fetchone()) == sample
    # Nothing collapsed, so the migration's alarm row must be absent.
    assert migration_alerts(conn) == {}


def test_migration_alerts_is_empty_on_a_healthy_database(tmp_path):
    """The alarm must read empty on a fresh database and on a database with no
    `meta` table at all — an alarm that cries wolf is an alarm the operator
    learns to skip (the reason 3.1b deliberately refused a gap report)."""
    assert migration_alerts(connect(":memory:")) == {}          # no schema at all
    assert migration_alerts(open_db(tmp_path / "clean.sqlite")) == {}


def test_a_rebuild_that_loses_rows_records_it_in_meta():
    """SILENCE IS NOT SUCCESS. The rebuild uses INSERT OR REPLACE so it can never
    raise inside `open_db` on an operator's database three weeks before the draft
    — which means it CAN collapse, and a collapse must leave a positive fact
    behind rather than a quieter table.

    The reachable path is narrow and worth naming. Both new keys are strict
    SUPERSETS of the old ones, so no pair of rows that were distinct before can
    collide on the added columns. What CAN collide is the rebuild's own
    `COALESCE(x, '')` on a nullable key member: SQLite treats PK NULLs as
    DISTINCT, so two `depth_charts` rows with `gsis_id IS NULL` are two rows at
    schema 6 and one row once both become ''. (The shipped ingester already
    coalesces at ingest, so this is a database written by some other path — which
    is exactly the case a migration cannot assume away.)"""
    conn = connect(":memory:")
    _at_version(conn, 6)
    conn.executemany(
        "INSERT OR REPLACE INTO depth_charts (gsis_id, season, week, game_type, club_code, "
        "position, depth_position, depth_team, formation, full_name, retrieved_as_of, "
        "knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (None, 2023, 5, "REG", "MIA", "WR", "WR1", "1", "3WR 1TE", "Someone",
             "2026-07-25", "2023-10-08"),
            (None, 2023, 5, "REG", "MIA", "WR", "WR1", "1", "3WR 1TE", "Someone Else",
             "2026-07-25", "2023-10-08"),
        ],
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM depth_charts").fetchone()["n"] == 2

    apply_schema(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM depth_charts_weekly").fetchone()["n"] == 1
    row = conn.execute(
        "SELECT value FROM meta WHERE key = '007_depth_charts_weekly_collapsed'").fetchone()
    assert row is not None, "a rebuild that lost a row must say so"
    assert row["value"] == "1"
    # …and it must be READABLE, or it is a note nobody opens.
    assert migration_alerts(conn) == {"007_depth_charts_weekly_collapsed": "1"}


def test_the_snap_counts_rebuild_cannot_collapse_at_all():
    """Companion to the above, recorded rather than assumed: the snap_counts key
    gains a column and coalesces `team`, but two rows that differ only in `team`
    are ALREADY one row at schema 6 (team is not in the old key). So there is no
    schema-6 state the rebuild can lose — the alarm row is unreachable there, and
    that is a property of the key, not of the data on this box."""
    conn = connect(":memory:")
    _at_version(conn, 6)
    conn.executemany(
        "INSERT OR REPLACE INTO snap_counts (pfr_player_id, team, season, week, defense_snaps, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?)",
        [
            ("Ply1", None, 2023, 5, 10.0, "2026-07-25", "2023-10-08"),
            ("Ply1", "",   2023, 5, 23.0, "2026-07-25", "2023-10-08"),
        ],
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM snap_counts").fetchone()["n"] == 1

    apply_schema(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM snap_counts").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM meta WHERE key = '007_snap_counts_collapsed'"
    ).fetchone()["n"] == 0


# =====================================================================
# The 2026 partition — the standing hazard
# =====================================================================


def test_007_touches_no_draft_critical_table(tmp_path):
    """The draft is ~3 weeks out and `espn_draft_ranks` / `projections` /
    `players` / `schedules` / `adp_rankings` are what the draft weapon runs on.
    Both 3.1 and 3.1b shipped a "a degraded pull destroys the day" defect; a
    migration that runs inside `open_db` on every command is the same class of
    blast radius. Fingerprint the CONTENT, not the row count — every ingester
    writes INSERT OR REPLACE, so an in-place overwrite leaves COUNT identical
    while replacing every value (the measured 3.1b headline finding)."""
    import hashlib

    conn = connect(tmp_path / "draft.sqlite")
    _at_version(conn, 6)
    conn.executemany(
        "INSERT OR REPLACE INTO espn_draft_ranks (board_key, espn_id, player, position, team, "
        "season, overall_rank, espn_pos_rank, adp, espn_adp_pos_rank, retrieved_as_of, "
        "knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(str(4000 + i), str(4000 + i), f"Player {i}", "WR", "MIA", 2026,
          i + 1, i + 1, float(i + 1), i + 1, "2026-07-24", "2026-07-24")
         for i in range(50)],
    )
    conn.commit()

    def fingerprint():
        rows = conn.execute(
            "SELECT * FROM espn_draft_ranks ORDER BY board_key, retrieved_as_of").fetchall()
        return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()

    before = fingerprint()
    apply_schema(conn)
    assert fingerprint() == before
    assert conn.execute("SELECT COUNT(*) AS n FROM espn_draft_ranks").fetchone()["n"] == 50


def _schema_shape(conn) -> list[tuple]:
    """Every declared object, normalized. `sqlite_autoindex_*` rows carry no SQL
    and are implied by the keys, so they are dropped rather than compared."""
    return sorted(
        (r["type"], r["name"], " ".join((r["sql"] or "").split()))
        for r in conn.execute("SELECT type, name, sql FROM sqlite_master")
        if not r["name"].startswith("sqlite_autoindex")
    )


@pytest.mark.parametrize("start_version", range(1, LATEST_SCHEMA_VERSION))
def test_a_fresh_database_and_an_upgraded_one_end_up_identical(tmp_path, start_version):
    """The upgrade path the OPERATOR takes must land on the schema a new clone
    gets. `db init` bootstraps `db/schema.sql` and then runs every migration; a
    database created months ago runs only the ones it has not seen.

    WHAT THIS DOES AND DOES NOT PROVE, stated because the first version of this
    test claimed more than it could deliver. It proves that every partial
    migration path completes and converges — a migration that raises when applied
    from an older start, or that leaves the version un-bumped, fails here. It
    canNOT prove that `db/schema.sql` and a later migration agree about a shared
    table, because BOTH sides of this comparison run today's schema.sql. That
    divergence is real and it is the next test's job.
    """
    fresh = open_db(tmp_path / f"fresh_{start_version}.sqlite")
    old = connect(tmp_path / f"old_{start_version}.sqlite")
    _at_version(old, start_version)
    apply_schema(old)

    assert old.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"] == str(LATEST_SCHEMA_VERSION)
    assert _schema_shape(old) == _schema_shape(fresh)
    fresh.close()
    old.close()


#: `CREATE TABLE [IF NOT EXISTS] <name>` — the declaration, wherever it is.
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def test_no_table_is_declared_by_two_different_schema_files():
    """The divergence the test above structurally cannot see, caught at the source.

    Every table in this repo is created with `IF NOT EXISTS`, which is right for a
    runner that must never raise inside `open_db`. The consequence is that the
    SECOND declaration of a table is a silent no-op on any database that already
    has it — so if `db/schema.sql` (the version-1 bootstrap) and a later migration
    both declare table T, then a FRESH database gets schema.sql's T and every
    EXISTING database keeps the migration's T, forever, with no error and nothing
    in `migration_alerts`. Same name, possibly different column ORDER, in a
    package that has positional `SELECT *` consumers.

    Today the answer is clean: zero tables are declared twice, and 007's table
    rebuilds go through a differently-named `_rebuild` table plus DROP/RENAME, so
    they do not trip this. That is the property being pinned — not a style rule.
    """
    declared: dict[str, list[str]] = {}
    for path in [SCHEMA_PATH, *sorted(MIGRATIONS_DIR.glob("*.sql"))]:
        for name in sorted(set(_CREATE_TABLE.findall(path.read_text(encoding="utf-8")))):
            declared.setdefault(name, []).append(path.name)
    twice = {t: files for t, files in declared.items() if len(files) > 1}
    assert not twice, (
        f"table(s) declared in more than one schema file: {twice}. A second "
        "`CREATE TABLE IF NOT EXISTS` is a no-op on every database that already has "
        "the table, so the fresh and the upgraded shapes silently diverge. If a "
        "table genuinely needs rebuilding, do it the way 007 does: build a "
        "differently-named table, copy the rows, DROP the old one, RENAME."
    )


def test_the_upgrade_preserves_every_draft_critical_partition(tmp_path):
    """The standing hazard, asserted across the WHOLE remaining migration path
    rather than for one migration: the draft is ~3 weeks out, `open_db` migrates
    on every command including the cockpit's, and items 3.1 and 3.1b each shipped
    a defect whose signature was a degraded write that reported success.

    CONTENT fingerprints, not row counts — every ingester writes INSERT OR
    REPLACE, so an in-place overwrite at the same (key, retrieved_as_of) leaves
    COUNT(*) and MAX(retrieved_as_of) identical while replacing every value (the
    measured 3.1b headline finding). League tables are in the set because league
    state is the one dataset whose lost day is UNRECOVERABLE.
    """
    conn = connect(tmp_path / "draft.sqlite")
    _at_version(conn, LATEST_SCHEMA_VERSION)
    conn.executemany(
        "INSERT OR REPLACE INTO espn_draft_ranks (board_key, espn_id, player, position, "
        "team, season, overall_rank, espn_pos_rank, adp, espn_adp_pos_rank, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(str(4000 + i), str(4000 + i), f"Player {i}", "WR", "MIA", 2026, i + 1, i + 1,
          float(i + 1), i + 1, "2026-07-25", "2026-07-25") for i in range(40)])
    conn.executemany(
        "INSERT OR REPLACE INTO projections (source, source_player_id, season, week, "
        "gsis_id, position, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?,?,?)",
        [("sleeper_rotowire", str(7000 + i), 2026, w, f"00-{i:07d}", "RB", "2026-07-25",
          "2026-07-25") for i in range(20) for w in (1, 2)])
    conn.executemany(
        "INSERT OR REPLACE INTO players (gsis_id, espn_id, name, position, "
        "retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?,?)",
        [(f"00-{i:07d}", str(4000 + i), f"Player {i}", "RB", "2026-07-25", "2026-07-25")
         for i in range(20)])
    conn.executemany(
        "INSERT OR REPLACE INTO adp_rankings (fantasypros_id, position, ecr_type, ecr, "
        "pos_rank, season, scrape_date, retrieved_as_of, knowable_as_of) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(str(9000 + i), "WR", "ro", float(i), i + 1, 2026, "2026-07-25", "2026-07-25",
          "2026-07-25") for i in range(20)])
    conn.executemany(
        "INSERT OR REPLACE INTO schedules (game_id, season, week, game_type, gameday, "
        "home_team, away_team, knowable_as_of, retrieved_as_of) VALUES (?,?,?,?,?,?,?,?,?)",
        [(f"2026_{w:02d}_AAA_BBB", 2026, w, "REG", "2026-09-10", "BBB", "AAA",
          "2026-08-01", "2026-07-25") for w in range(1, 19)])
    conn.executemany(
        "INSERT OR REPLACE INTO league_player_state (season, espn_player_id, "
        "on_team_id, retrieved_as_of, knowable_as_of) VALUES (?,?,?,?,?)",
        [(2026, str(4000 + i), (i % 10) + 1, "2026-07-25", "2026-07-25")
         for i in range(30)])
    conn.commit()

    tables = ["espn_draft_ranks", "projections", "players", "adp_rankings", "schedules",
              "league_player_state"]

    def fingerprint():
        out = {}
        for table in tables:
            digests = sorted(
                hashlib.sha256(repr(tuple(row)).encode()).hexdigest()
                for row in conn.execute(f"SELECT * FROM {table}")  # noqa: S608
            )
            out[table] = (len(digests), hashlib.sha256("".join(digests).encode()).hexdigest())
        return out

    before = fingerprint()
    assert all(n for n, _ in before.values()), "the fixture stored nothing to protect"

    apply_schema(conn)

    assert fingerprint() == before
    assert migration_alerts(conn) == {}
    conn.close()


# =====================================================================
# store.connect — the busy_timeout (item 3.2c §2.6h)
# =====================================================================


def test_connect_sets_a_busy_timeout():
    conn = connect(":memory:")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    assert BUSY_TIMEOUT_MS >= 30_000, (
        "the value protects the league sync, whose lost day is unrecoverable"
    )


#: How long the background thread holds an EXCLUSIVE lock in the two contention
#: tests. Long enough that a non-waiting writer certainly fails, short enough
#: that the pair costs ~1.2 s. The real bound being tested is 30 s.
_HELD_LOCK_SECONDS = 0.6


def _lock_holder(db_path):
    """Start a thread that grabs an EXCLUSIVE write lock on `db_path` and holds it
    for `_HELD_LOCK_SECONDS`. The connection is opened INSIDE the thread —
    sqlite3 objects are bound to their creating thread."""
    import threading
    import time

    holding = threading.Event()

    def hold():
        conn = connect(db_path)
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('holder', '1')")
        holding.set()
        time.sleep(_HELD_LOCK_SECONDS)
        conn.commit()
        conn.close()

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(timeout=10), "the lock holder never started"
    return thread


def test_a_writer_waits_out_another_process_lock(tmp_path):
    """The behaviour the pragma buys, and the reason it is set in `connect` rather
    than in the backfill: the process that must not lose is the LEAGUE SYNC (ESPN
    serves league state as a current snapshot only — a day it fails to capture is
    gone permanently), and it is the one that would be holding the short end of a
    collision with a multi-minute backfill."""
    import time

    db_path = tmp_path / "contended.sqlite"
    open_db(db_path).close()
    thread = _lock_holder(db_path)

    waiter = connect(db_path)             # 30 s busy_timeout, straight from `connect`
    started = time.perf_counter()
    waiter.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('waiter', '1')")
    waiter.commit()
    waited = time.perf_counter() - started

    thread.join()
    assert waited >= _HELD_LOCK_SECONDS / 2, (
        "the write should have BLOCKED on the holder, not raced past it — "
        "if this is ~0 the test is not testing contention at all"
    )
    assert waiter.execute(
        "SELECT value FROM meta WHERE key = 'waiter'").fetchone()["value"] == "1"
    waiter.close()


def test_without_the_busy_timeout_the_same_write_fails(tmp_path):
    """Teeth for the test above: set the timeout to 0 and the identical sequence
    raises `database is locked`. Without this, the test above would still pass on
    a build where the pragma did nothing."""
    import sqlite3

    db_path = tmp_path / "contended0.sqlite"
    open_db(db_path).close()
    thread = _lock_holder(db_path)

    impatient = connect(db_path)
    impatient.execute("PRAGMA busy_timeout = 0")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        impatient.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('waiter', '1')")
        impatient.commit()

    thread.join()
    impatient.close()


# =====================================================================
# Migration 019 — item 3.18's paired repair: re-derive the gsis join key
# =====================================================================
#
# The point of this migration is NARROWNESS. It rewrites a derived crosswalk
# column on rows already stored under an accidental resolution, and the failure
# mode that matters is not "it missed a row" — it is "it re-pointed a row it had
# no business touching", which would silently attribute one player's projections
# to another with nothing raising anywhere. So every test below is about the
# FENCE as much as the fix.

_M019 = "019_gsis_placeholder_rekey.sql"
_PH, _REAL = "LOV121782", "00-0041027"
_ESPN, _SLEEPER = "4870808", "12441"


def _seed_collision(conn):
    """The measured live shape: one player, two `players` rows, one espn id and
    one sleeper id — with every fact table stored the way the live database
    actually had it before the repair."""
    conn.executemany(
        "INSERT INTO players (gsis_id, espn_id, sleeper_id, retrieved_as_of, "
        "knowable_as_of) VALUES (?, ?, ?, '2026-09-15', '2026-09-15')",
        [(_PH, _ESPN, _SLEEPER), (_REAL, _ESPN, _SLEEPER)],
    )
    # projections: keyed on the PLACEHOLDER (what `setdefault` stamped)
    conn.executemany(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, retrieved_as_of, knowable_as_of) VALUES "
        "('sleeper_rotowire', ?, ?, 2026, ?, 'regular', 'RB', 'NO', "
        "'2026-09-15', '2026-09-15')",
        [(_SLEEPER, _PH, w) for w in (1, 2, 3)],
    )
    # league_player_state: the same placeholder, derived from gsis_by_espn
    conn.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, "
        "position, retrieved_as_of, knowable_as_of) VALUES "
        "(2026, ?, ?, 'A Rookie', 'RB', '2026-09-15', '2026-09-15')",
        (_ESPN, _PH),
    )
    # player_news_links: likewise
    conn.execute(
        "INSERT INTO player_news_links (source, news_id, espn_id, gsis_id, "
        "published_at, knowable_as_of, retrieved_as_of) VALUES "
        "('espn', 'n1', ?, ?, '2026-09-15T12:00:00Z', '2026-09-15', '2026-09-15')",
        (_ESPN, _PH),
    )
    conn.commit()


def _migrate_from_18(conn):
    _at_version(conn, 18)
    _seed_collision(conn)
    apply_schema(conn)
    return conn


@pytest.fixture()
def rekeyed():
    conn = connect(":memory:")
    _migrate_from_18(conn)
    yield conn
    conn.close()


def test_019_rekeys_the_three_derived_columns_onto_the_real_gsis(rekeyed):
    """The fix. Every table whose gsis this system DERIVED lands on the real id."""
    assert [r["gsis_id"] for r in rekeyed.execute(
        "SELECT gsis_id FROM projections ORDER BY week")] == [_REAL] * 3
    assert rekeyed.execute(
        "SELECT gsis_id FROM league_player_state").fetchone()["gsis_id"] == _REAL
    assert rekeyed.execute(
        "SELECT gsis_id FROM player_news_links").fetchone()["gsis_id"] == _REAL


def test_019_agrees_with_the_python_resolvers_it_is_repairing(rekeyed):
    """SQL ≡ Python. The migration expresses `_gsis_preference` as a SQL sort
    key; the ingesters express it in Python. Two encodings of one rule drift
    silently unless something compares them — and a drift here means the next
    pull re-splits the id space the migration just merged.
    """
    from ziggurat.data.nfl import base
    from ziggurat.data.nfl import projections as proj

    by_espn = base.gsis_by_espn(rekeyed)
    by_sleeper = proj._sleeper_to_gsis(rekeyed)

    for row in rekeyed.execute(
            "SELECT source_player_id, gsis_id FROM projections"):
        assert row["gsis_id"] == by_sleeper[row["source_player_id"]]
    for row in rekeyed.execute(
            "SELECT espn_player_id, gsis_id FROM league_player_state"):
        assert row["gsis_id"] == by_espn[row["espn_player_id"]]
    for row in rekeyed.execute("SELECT espn_id, gsis_id FROM player_news_links"):
        assert row["gsis_id"] == by_espn[row["espn_id"]]


def test_019_is_idempotent(rekeyed):
    """A second application is a no-op — the rewrite condition ("the preferred id
    differs from the stored one") is false for every row once it has run.

    `apply_schema` is version-gated and will never re-run it, so this is about
    the SQL being safe rather than about the runner: an operator re-running the
    file by hand, or a future migration reusing the same statement, must not be
    a second, different rewrite.
    """
    sql = (MIGRATIONS_DIR / _M019).read_text(encoding="utf-8")
    before = _fingerprint(rekeyed)
    rekeyed.executescript(sql)
    rekeyed.commit()
    assert _fingerprint(rekeyed) == before


def _fingerprint(conn):
    return (
        [tuple(r) for r in conn.execute(
            "SELECT source_player_id, gsis_id, week, retrieved_as_of, knowable_as_of "
            "FROM projections ORDER BY week")],
        [tuple(r) for r in conn.execute(
            "SELECT espn_player_id, gsis_id, retrieved_as_of, knowable_as_of "
            "FROM league_player_state")],
        [tuple(r) for r in conn.execute(
            "SELECT espn_id, gsis_id, retrieved_as_of, knowable_as_of "
            "FROM player_news_links")],
    )


def test_019_moves_no_row_and_no_timestamp(rekeyed):
    """RULE 1. The migration changes WHICH PLAYER a row is about, never WHEN it
    became knowable — and it must not add or drop a row either.

    A migration that silently re-stamped `knowable_as_of` would make a July pull
    readable in June, which is the one class of defect this repo's default view
    exists to make impossible.
    """
    assert rekeyed.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 3
    assert rekeyed.execute(
        "SELECT COUNT(*) FROM league_player_state").fetchone()[0] == 1
    assert rekeyed.execute(
        "SELECT COUNT(*) FROM player_news_links").fetchone()[0] == 1
    for table in ("projections", "league_player_state", "player_news_links"):
        stamps = {tuple(r) for r in rekeyed.execute(
            f"SELECT retrieved_as_of, knowable_as_of FROM {table}")}
        assert stamps == {("2026-09-15", "2026-09-15")}


def test_019_leaves_a_non_colliding_key_alone():
    """The fence, part 1: a sleeper/espn id with ONE gsis is never touched, even
    when that one gsis is a placeholder.

    A placeholder with no rival is nflverse's real answer for an unreconciled
    player, not an error to correct — rewriting it would invent an id.
    """
    conn = connect(":memory:")
    _at_version(conn, 18)
    conn.execute(
        "INSERT INTO players (gsis_id, espn_id, sleeper_id, retrieved_as_of, "
        "knowable_as_of) VALUES ('SOLO01', '999', '888', '2026-09-15', '2026-09-15')")
    conn.execute(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, retrieved_as_of, knowable_as_of) VALUES "
        "('sleeper_rotowire', '888', 'SOLO01', 2026, 1, 'regular', 'WR', 'NO', "
        "'2026-09-15', '2026-09-15')")
    conn.commit()
    apply_schema(conn)
    assert conn.execute("SELECT gsis_id FROM projections").fetchone()[0] == "SOLO01"
    conn.close()


def test_019_leaves_a_row_whose_stored_id_is_not_its_own_alternate_alone():
    """The fence, part 2 — the one that stops a re-key becoming a corruption.

    A projections row may carry a gsis that is NOT one of its own sleeper id's
    alternates (a stale crosswalk, a hand-loaded row, a source change). Rewriting
    that would move a stat line from one player to a DIFFERENT player, which is
    strictly worse than the join loss being repaired, and nothing downstream
    could detect it. So the migration requires the stored id to be one of THAT
    key's own ids before it will move it.
    """
    conn = connect(":memory:")
    _at_version(conn, 18)
    conn.executemany(
        "INSERT INTO players (gsis_id, espn_id, sleeper_id, retrieved_as_of, "
        "knowable_as_of) VALUES (?, ?, ?, '2026-09-15', '2026-09-15')",
        [(_PH, _ESPN, _SLEEPER), (_REAL, _ESPN, _SLEEPER)],
    )
    conn.execute(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, retrieved_as_of, knowable_as_of) VALUES "
        "('sleeper_rotowire', ?, '00-0099999', 2026, 1, 'regular', 'RB', 'NO', "
        "'2026-09-15', '2026-09-15')", (_SLEEPER,))
    conn.commit()
    apply_schema(conn)
    assert conn.execute("SELECT gsis_id FROM projections").fetchone()[0] == "00-0099999"
    conn.close()


def test_019_is_scoped_to_the_sleeper_keyed_projection_source():
    """The fence, part 3: `source_player_id` only means "Sleeper player_id" for
    `sleeper_rotowire`. Another source numbers its players its own way, and a
    collision on OUR sleeper map says nothing about ITS key space.
    """
    conn = connect(":memory:")
    _at_version(conn, 18)
    conn.executemany(
        "INSERT INTO players (gsis_id, espn_id, sleeper_id, retrieved_as_of, "
        "knowable_as_of) VALUES (?, ?, ?, '2026-09-15', '2026-09-15')",
        [(_PH, _ESPN, _SLEEPER), (_REAL, _ESPN, _SLEEPER)],
    )
    conn.execute(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, retrieved_as_of, knowable_as_of) VALUES "
        "('some_other_feed', ?, ?, 2026, 1, 'regular', 'RB', 'NO', "
        "'2026-09-15', '2026-09-15')", (_SLEEPER, _PH))
    conn.commit()
    apply_schema(conn)
    assert conn.execute("SELECT gsis_id FROM projections").fetchone()[0] == _PH
    conn.close()


def test_019_does_not_touch_the_upstream_depth_chart_gsis():
    """The fence, part 4, and the one deliberate exception.

    `depth_chart_slots.gsis_id` is a column of the UPSTREAM nflverse frame, not a
    value this crosswalk derived — 9 of its live ids are collision losers and it
    keeps them, because rewriting it would falsify what upstream published. A
    consumer that needs the spine joins through `espn_id`, which the same table
    carries.
    """
    conn = connect(":memory:")
    _at_version(conn, 18)
    conn.executemany(
        "INSERT INTO players (gsis_id, espn_id, sleeper_id, retrieved_as_of, "
        "knowable_as_of) VALUES (?, ?, ?, '2026-09-15', '2026-09-15')",
        [(_PH, _ESPN, _SLEEPER), (_REAL, _ESPN, _SLEEPER)],
    )
    conn.execute(
        "INSERT INTO depth_chart_slots (season, team, pos_grp_id, pos_id, pos_rank, "
        "pos_abb, observed_at, retrieved_as_of, knowable_as_of, espn_id, gsis_id) "
        "VALUES (2026, 'NO', 'RB', 'RB', 1, 'RB', '2026-09-15', '2026-09-15', "
        "'2026-09-15', ?, ?)",
        (_ESPN, _PH))
    conn.commit()
    apply_schema(conn)
    assert conn.execute("SELECT gsis_id FROM depth_chart_slots").fetchone()[0] == _PH
    conn.close()


def test_019_leaves_no_temp_table_behind(rekeyed):
    """`apply_schema` hands this connection back to the caller — every CLI
    command runs it — so a leftover TEMP table would outlive the migration for
    the life of the process."""
    assert [r["name"] for r in rekeyed.execute(
        "SELECT name FROM sqlite_temp_master WHERE type = 'table'")] == []


def test_019_re_keys_a_row_whose_key_column_never_saw_the_collision():
    """Pass 2 in SQL, on the measured live shape (Max Bredeson, espn 4878695).

    His REAL row has lost its `sleeper_id`, so the sleeper column offers ONE
    candidate and pass 1 has nothing to resolve — while `gsis_by_espn` sees both
    rows and moves his roster row to the real id. Without this hop his 954 stored
    projection rows stay on the placeholder and he goes from priced to
    UNPRICEABLE: a fresh instance of the defect, created by the fix.
    """
    conn = connect(":memory:")
    _at_version(conn, 18)
    conn.executemany(
        "INSERT INTO players (gsis_id, espn_id, sleeper_id, retrieved_as_of, "
        "knowable_as_of) VALUES (?, ?, ?, '2026-09-15', '2026-09-15')",
        [("00-0041081", "4878695", None), ("BRE060106", "4878695", "13516")],
    )
    conn.execute(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, retrieved_as_of, knowable_as_of) VALUES "
        "('sleeper_rotowire', '13516', 'BRE060106', 2026, 1, 'regular', 'TE', 'GB', "
        "'2026-09-15', '2026-09-15')")
    conn.execute(
        "INSERT INTO league_player_state (season, espn_player_id, gsis_id, player, "
        "position, retrieved_as_of, knowable_as_of) VALUES "
        "(2026, '4878695', 'BRE060106', 'Max Bredeson', 'TE', "
        "'2026-09-15', '2026-09-15')")
    conn.commit()
    apply_schema(conn)

    # both sides land on the SAME id — which is the whole point
    assert conn.execute("SELECT gsis_id FROM projections").fetchone()[0] == "00-0041081"
    assert conn.execute(
        "SELECT gsis_id FROM league_player_state").fetchone()[0] == "00-0041081"
    conn.close()


def test_019_does_not_hop_between_two_real_gsis_ids():
    """The pass-2 GATE, in SQL. Same fence as `base.preferred_gsis_by`'s.

    Upstream gives ONE espn id to TWO genuinely different retired players in four
    measured cases (2582138, 2574010, 2516049, 16094), both with real `00-` ids.
    An ungated hop would move one player's projection rows onto the other — which
    is strictly worse than the join loss being repaired and invisible downstream.
    """
    conn = connect(":memory:")
    _at_version(conn, 18)
    conn.executemany(
        "INSERT INTO players (gsis_id, espn_id, sleeper_id, retrieved_as_of, "
        "knowable_as_of) VALUES (?, '2582138', ?, '2026-09-15', '2026-09-15')",
        [("00-0032430", "3480"), ("00-0032606", "3481")],
    )
    conn.executemany(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, retrieved_as_of, knowable_as_of) VALUES "
        "('sleeper_rotowire', ?, ?, 2026, 1, 'regular', 'TE', 'NO', "
        "'2026-09-15', '2026-09-15')",
        [("3480", "00-0032430"), ("3481", "00-0032606")],
    )
    conn.commit()
    apply_schema(conn)

    got = dict(conn.execute("SELECT source_player_id, gsis_id FROM projections"))
    assert got == {"3480": "00-0032430", "3481": "00-0032606"}
    conn.close()


def test_019_pass_1_stands_on_its_own_when_no_espn_identity_exists():
    """Pass 1 pinned SEPARATELY from pass 2, because pass 2 hides it.

    Wherever both colliding rows carry an espn id, the identity hop rescues a
    placeholder that pass 1 wrongly chose — so an INVERTED preference produces
    the right answer anyway and every other test here stays green (measured: that
    exact mutant survived the whole file). This fixture removes the rescue: two
    gsis on one sleeper id, neither row carrying an espn id, so only pass 1 can
    decide. `players` rows with a null espn_id are common upstream, which is why
    this is a real case and not a contrivance.
    """
    conn = connect(":memory:")
    _at_version(conn, 18)
    conn.executemany(
        "INSERT INTO players (gsis_id, espn_id, sleeper_id, retrieved_as_of, "
        "knowable_as_of) VALUES (?, NULL, '4242', '2026-09-15', '2026-09-15')",
        [("PLC900001",), ("00-0049999",)],
    )
    conn.execute(
        "INSERT INTO projections (source, source_player_id, gsis_id, season, week, "
        "season_type, position, team, retrieved_as_of, knowable_as_of) VALUES "
        "('sleeper_rotowire', '4242', 'PLC900001', 2026, 1, 'regular', 'WR', 'NO', "
        "'2026-09-15', '2026-09-15')")
    conn.commit()
    apply_schema(conn)
    assert conn.execute("SELECT gsis_id FROM projections").fetchone()[0] == "00-0049999"
    conn.close()
