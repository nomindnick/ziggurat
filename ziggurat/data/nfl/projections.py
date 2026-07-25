"""Weekly stat-line projections (Sleeper) ingestion — item 1.5.

Single-provider Rotowire projections served by Sleeper's undocumented
``/projections/nfl/{season}/{week}`` endpoint, labeled ``sleeper_rotowire`` (NOT
a consensus). Each row is stored under scoring.py's canonical stat keys so a
persisted row scores DIRECTLY through ``ziggurat.core.scoring`` — the stored
``projected_points`` (source ``pts_ppr``) is a cross-check only and is NEVER a
scoring input (scoring.py ignores it as an unknown key).

Two knowledge-time regimes (design §2, §4.4):

* **forward / live** (default): pulled pre-game each week, stamped
  ``knowable_as_of = retrieved_as_of = pull day``. Leakage-safe by construction;
  this is the path that feeds valuation and the weekly loop.
* **bulk historical** (``bulk_historical=True``): a backfill of past weeks whose
  point-in-time integrity is UNVERIFIED (design §2 — ``last_modified`` is a
  post-week batch stamp, not a per-row published-at). It is stamped at the
  leakage-safe lower bound ``knowable_as_of = week_first_gameday_map(season,
  week)`` (schedules must be ingested first) and must be read ONLY through
  ``base.latest_truth(get_projections)`` — never presented as a reconstructed
  pre-game information set.

``source_player_id`` (Sleeper player_id, or the team abbr for a DEF) is the
durable PK spine, so an unresolved ``gsis_id`` (DEF, rookies) never drops a row.
"""

from ziggurat.core import scoring
from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

SOURCE = "sleeper_rotowire"

# Scoring positions we keep; the feed also returns FB/CB/P (and stray others)
# that carry no fantasy line in this league and are filtered out at map time.
_SCORING_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})

# ---------------------------------------------------------------- key mappings
# Sleeper stat key -> scoring.py canonical key. STRICT allow-list: only keys
# scoring.py actually reads are ever emitted (validated by
# ``validate_projection_keys``). Non-scoring Sleeper keys (bonus_rec_*, gp,
# adp_*, pts_std, ...) are dropped; ``pts_ppr`` is captured separately as the
# non-scoring ``projected_points`` cross-check, never as a scoring input.
_OFFENSE_MAP = {
    "pass_yd": "passing_yards",
    "pass_td": "passing_tds",
    "pass_int": "interceptions",
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_tds",
    "rec": "receptions",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_tds",
    "fum_lost": "fumbles_lost",  # pre-summed alias scoring.py accepts (supply ONE form)
    "pass_2pt": "passing_2pt_conversions",
    "rush_2pt": "rushing_2pt_conversions",
    "rec_2pt": "receiving_2pt_conversions",
}

_DST_MAP = {
    "sack": "sacks",
    "int": "def_interceptions",
    "fum_rec": "fumble_recoveries",
    "safe": "safeties",
    "blk_kick": "blocked_kicks",
    "pts_allow": "points_allowed",  # bracket input; absent => omitted, never 0
    "yds_allow": "yards_allowed",   # bracket input; absent => omitted, never 0
}

# Kicker made-FG count buckets (direct). ``fgm_50p`` is LOSSY: it bundles 60+, so
# a 60+ FG scores +5 not +6 (rare); ``fg_made_60`` cannot be filled from source.
_KICKER_DIRECT_MAP = {
    "fgm_40_49": "fg_made_40_49",
    "fgm_50p": "fg_made_50_59",
    "xpm": "pat_made",
}

# Columns of the projections table that carry a canonical scoring value (all
# other columns are metadata / provenance). Every stored row sets each of these
# so ``base.upsert`` sees a uniform key set; unmapped keys stay NULL.
_SCORING_COLUMNS = (
    "passing_yards", "passing_tds", "interceptions", "rushing_yards",
    "rushing_tds", "receptions", "receiving_yards", "receiving_tds",
    "fumbles_lost", "passing_2pt_conversions", "rushing_2pt_conversions",
    "receiving_2pt_conversions",
    "fg_made_0_39", "fg_made_40_49", "fg_made_50_59", "fg_made_60",
    "pat_made", "fg_missed",
    "sacks", "def_interceptions", "fumble_recoveries", "safeties",
    "blocked_kicks", "def_tds", "points_allowed", "yards_allowed",
)


def _position(raw) -> str | None:
    player = raw.get("player") or {}
    return player.get("position") or raw.get("position")


def _present(value) -> bool:
    return value is not None


def map_sleeper_projection(raw_row) -> dict | None:
    """Map ONE raw Sleeper projection to a canonical scoring-key stat dict.

    Returns ``None`` for a non-scoring position (FB/CB/P/...) — the caller skips
    it. Otherwise returns a dict of ONLY scoring.py canonical keys (a subset of
    ``validate_projection_keys``'s allow-list), so the result both scores
    directly and passes the strict validator. ``pts_ppr`` is deliberately NOT in
    this dict (it is captured as the non-scoring ``projected_points`` by the
    ingester); a key is emitted only when its source value is present, so an
    absent bracket input stays absent (never a phantom 0-allowed shutout).
    """
    if _position(raw_row) not in _SCORING_POSITIONS:
        return None
    stats = raw_row.get("stats") or {}
    mapped: dict = {}

    for src_key, canon in _OFFENSE_MAP.items():
        if _present(stats.get(src_key)):
            mapped[canon] = base._clean(stats.get(src_key))

    for src_key, canon in _DST_MAP.items():
        if _present(stats.get(src_key)):
            mapped[canon] = base._clean(stats.get(src_key))

    for src_key, canon in _KICKER_DIRECT_MAP.items():
        if _present(stats.get(src_key)):
            mapped[canon] = base._clean(stats.get(src_key))

    # Kicker 0–39 bucket = 0–19 + 20–29 + 30–39 (all price at +3). The source
    # carries a distinct, populated fgm_0_19 that must NOT be dropped, or a
    # projected sub-20-yd make would silently score 0 instead of +3.
    fg_lo = [stats.get(k) for k in ("fgm_0_19", "fgm_20_29", "fgm_30_39") if _present(stats.get(k))]
    if fg_lo:
        mapped["fg_made_0_39"] = float(sum(base._clean(v) for v in fg_lo))

    # Missed FGs = attempts − makes (flat −1/miss in this league).
    fga, fgm = stats.get("fga"), stats.get("fgm")
    if _present(fga) and _present(fgm):
        mapped["fg_missed"] = float(base._clean(fga) - base._clean(fgm))

    # Every defensive + special-teams return TD, counted ONCE: def_td already
    # subsumes fumble/pick-six returns and st_td == the return-TD relabel, so
    # do NOT add def_pr_td / def_fum_td / pass_int_td / pr_td (design §4.4).
    def_td, st_td = stats.get("def_td"), stats.get("st_td")
    if _present(def_td) or _present(st_td):
        mapped["def_tds"] = float(base._clean(def_td) or 0.0) + float(base._clean(st_td) or 0.0)

    return mapped


def _scoring_key_allowlist() -> set[str]:
    """The set of keys scoring.py actually reads, assembled by importing the
    scoring tables (rule 2 — no re-hardcoded scoring value here). ``pat_made`` /
    ``fg_missed`` (read directly by ``score_kicker``) and ``points_allowed`` /
    ``yards_allowed`` (the ``score_dst`` bracket inputs) are not in the weight
    dicts, so they are added explicitly."""
    return (
        set(scoring._OFFENSE_WEIGHTS)
        | set(scoring._DST_EVENT_WEIGHTS)
        | set(scoring._FG_COUNT_KEY_DISTANCES)
        | {"pat_made", "fg_missed", "points_allowed", "yards_allowed"}
    )


def validate_projection_keys(mapped_stats) -> None:
    """Raise if a mapped stat dict carries any key scoring.py would not read.

    The strict unknown-key guard item 1.3 deferred here. Fed ONLY the MAPPED
    canonical dict (never a raw Sleeper row — those carry ~30 non-scoring keys
    and would raise on every extra). A misspelled canonical key surfaces loudly
    instead of silently scoring 0."""
    unknown = set(mapped_stats) - _scoring_key_allowlist()
    if unknown:
        raise ValueError(
            f"projection stat dict carries non-scoring keys {sorted(unknown)} "
            f"(feed the MAPPED canonical dict, not raw Sleeper stats)"
        )


def _sleeper_to_gsis(conn) -> dict[str, str | None]:
    """sleeper_id -> gsis_id from the latest players snapshot (mirrors
    ``base.gsis_by_pfr``). players.py normalizes ``sleeper_id`` to a bare digit
    string, matching Sleeper's ``player_id`` for skill players; DEF/rookies are
    absent and resolve to None (kept via the source_player_id spine)."""
    out: dict[str, str | None] = {}
    for r in conn.execute(
        """
        SELECT sleeper_id, gsis_id FROM players p
        WHERE sleeper_id IS NOT NULL AND retrieved_as_of = (
            SELECT MAX(retrieved_as_of) FROM players p2 WHERE p2.gsis_id = p.gsis_id
        )
        """
    ):
        out.setdefault(r["sleeper_id"], r["gsis_id"])
    return out


# The stored PRIMARY KEY, passed to ``base.upsert`` so its return value is the
# number of DISTINCT keys written rather than rows offered (item 3.2c, F-G).
# SWEPT 2026-07-25: the same instrumentation was applied to 6 of 14 call sites
# in 3.2c and skipped here, and the one skipped site that DID collide
# (adp_rankings) lost a real market fact a day for two days, silently, with an
# inflated count in the run log.
# Measured 0 same-batch collisions live (173,712 rows). PERISHABLE source: a
# collapse here is a lost observation that cannot be re-pulled.
_PK_COLS = ('source', 'source_player_id', 'season', 'week', 'retrieved_as_of')


def ingest_projections(conn, rows, *, retrieved_as_of: str, bulk_historical: bool = False) -> int:
    """Persist Sleeper projection rows, stamping the two knowledge-time regimes.

    ``bulk_historical=False`` (default, forward/live): ``knowable_as_of =
    retrieved_as_of``. ``bulk_historical=True``: ``knowable_as_of =
    week_first_gameday_map(season, week)`` (schedules must be ingested first) —
    read those rows ONLY via ``base.latest_truth(get_projections)``. Non-scoring
    positions are filtered (not counted as drops); a bulk row whose (season,
    week) has no gameday is dropped via ``base.note_drops`` rather than stored
    with a leaky NULL knowledge time. Returns rows written."""
    retrieved = base.iso_date(retrieved_as_of)
    crosswalk = _sleeper_to_gsis(conn)
    wfg = base.week_first_gameday_map(conn) if bulk_historical else None

    out: list[dict] = []
    dropped = 0
    considered = 0
    for raw in rows:
        mapped = map_sleeper_projection(raw)
        if mapped is None:
            continue  # non-scoring position — expected filter, not a drop
        considered += 1
        validate_projection_keys(mapped)  # fail loud on a mis-mapped key

        season = int(raw["season"])
        week = int(raw["week"])
        source_player_id = str(raw["player_id"])

        if bulk_historical:
            knowable = wfg.get((season, week))
            if knowable is None:
                dropped += 1
                continue
        else:
            knowable = retrieved

        stats = raw.get("stats") or {}
        row = {
            "source": SOURCE,
            "source_player_id": source_player_id,
            "gsis_id": crosswalk.get(source_player_id),
            "season": season,
            "week": week,
            "season_type": raw.get("season_type"),
            "position": _position(raw),
            "team": raw.get("team"),
            "opponent": raw.get("opponent"),
        }
        row.update({col: None for col in _SCORING_COLUMNS})
        row.update(mapped)
        row["projected_points"] = base._clean(stats.get("pts_ppr"))
        row["retrieved_as_of"] = retrieved
        row["knowable_as_of"] = knowable
        out.append(row)

    base.note_drops("projections", dropped, considered, why="no week gameday")
    return base.upsert(conn, "projections", out, key_cols=_PK_COLS)


def pull_projections(conn, season, weeks, *, retrieved_as_of: str, bulk_historical: bool = False) -> int:
    """Pull Sleeper projections for ``season`` across ``weeks`` and store them.
    ``nfl.import_sleeper_projections`` is the network seam tests patch."""
    total = 0
    for week in weeks:
        rows = nfl.import_sleeper_projections(season, week)
        total += ingest_projections(
            conn, rows, retrieved_as_of=retrieved_as_of, bulk_historical=bulk_historical
        )
    return total


def get_projections(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    gsis_id=None,
    source=None,
    position=None,
    view: base.AsOfView = "historical",
):
    """Projection rows knowable on or before ``as_of`` (keyword-only; no implicit
    now). Latest snapshot per (source, source_player_id, season, week). Backtest
    / bulk-history reads go through ``base.latest_truth(get_projections)``."""
    clauses, params = [], {}
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if gsis_id is not None:
        clauses.append("t.gsis_id = :gsis_id")
        params["gsis_id"] = gsis_id
    if source is not None:
        clauses.append("t.source = :source")
        params["source"] = source
    if position is not None:
        clauses.append("t.position = :position")
        params["position"] = position
    return base.select_as_of(
        conn, "projections", as_of=as_of,
        key_cols=["source", "source_player_id", "season", "week"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
