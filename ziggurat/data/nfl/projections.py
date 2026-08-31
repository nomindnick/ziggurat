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

ONE MAPPED SOURCE KEY THE 2026 FEED DOES NOT SERVE, and what is done about it:
``fgm_50p``. See :func:`unbucketed_fg_makes` — it reads nothing on the current
feed, so every projected 50+ field goal scores ZERO while ``fg_missed`` goes on
charging -1 a miss. The makes are recoverable from the feed's own arithmetic;
the recovery ships as the opt-in ``derive_fg_50_plus`` flag (OFF by default,
because turning it on re-prices a board), a decomposition too broken to derive
from refuses the batch on that opt-in path only, and what the default leaves
unpriced is reported every run through ``base.note_incomplete`` rather than lost
in silence.
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
#
# ``fgm_50p`` READS NOTHING ON THE 2026 FEED and is kept deliberately — see
# :func:`unbucketed_fg_makes` for the whole story and the measurements. Short
# version: the 2026 feed serves no 50+ bucket under any name (0 of 2,754 kicker
# rows), so every projected 50+ make scored 0 while ``fg_missed`` went on
# charging -1 a miss. It is upstream DRIFT, not a mis-transcription: the 2021,
# 2022, 2023, 2024 and 2025 feeds all serve ``fgm_50p`` when read today. It
# therefore stays mapped rather than deleted, so a feed that does serve the
# bucket is priced from the SOURCE rather than from a derivation — and so the
# derivation can subtract it and never double-count.
_KICKER_DIRECT_MAP = {
    "fgm_40_49": "fg_made_40_49",
    "fgm_50p": "fg_made_50_59",
    "xpm": "pat_made",
}

#: Every DISJOINT made-FG count the feed can serve, low to high. The order is
#: the distance order and the set is exhaustive by the source's own arithmetic:
#: ``fgm`` is the TOTAL made count, so whatever these do not account for is a
#: make at a distance the feed does not bucket. Used only by
#: :func:`unbucketed_fg_makes` — the mapping above is what actually stores them.
_FG_MADE_BUCKET_KEYS = ("fgm_0_19", "fgm_20_29", "fgm_30_39", "fgm_40_49", "fgm_50p")

#: How far ``Σ buckets`` may exceed ``fgm`` before that row's decomposition is
#: declared broken.
#:
#: SIZED AS ROUNDING HEADROOM, and on nothing else. Sleeper publishes 2-decimal
#: values, so five summed buckets plus ``fgm`` can each be off by up to 0.005 —
#: a worst case of 0.03 with nothing wrong at all. 0.05 is that with margin.
#:
#: AN EARLIER VERSION OF THIS COMMENT CLAIMED MORE, and the claim was false. It
#: said "0.05 sits in the gap between the two populations". There is no gap.
#: Re-measured over the same 3,231 Sleeper kicker rows (2021-2026, all 18 weeks,
#: read 2026-08-30), the negative-residual histogram is CONTINUOUS through the
#: threshold::
#:
#:     -0.01 x438   -0.02 x35   -0.03 x7   -0.04 x2   -0.05 x21
#:     -0.06 x20    -0.07 x16   -0.08 x5   then a sparse tail to -0.37
#:
#: 44 rows lie strictly between -0.05 and -0.01, and the count RISES again
#: immediately below the threshold. The local minimum is at -0.03/-0.04 (7 and 2
#: rows), which is a trough two rows deep — noise, not a population boundary. So
#: the threshold is a rounding argument, not a clustering one, and rows between
#: about -0.03 and -0.05 are genuinely ambiguous and are called CONSISTENT by
#: choice. Moving it to 0.03 changes no season's verdict against
#: :data:`_MAX_BUCKET_MISMATCH_FRACTION` (measured: 2021 4.3%, 2022 6.8%, 2023
#: 0.8%, 2024 4.7%, 2025 0.0%, 2026 0.0% — same side of the 2% ceiling in all
#: six), which is why it is left where it is rather than tuned.
#:
#: WHAT THE AMBIGUITY CAN AND CANNOT REACH: nothing stored. A row whose residual
#: is negative is never derived from in either mode (``map_sleeper_projection``
#: stores the residual only when it is POSITIVE), so the boundary moves only the
#: batch gate's own count. And the season this repo drafts on does not come near
#: it: 2026's 575 kicker rows have a minimum residual of +0.05, i.e. not one
#: negative residual at any tolerance.
_FG_BUCKET_TOLERANCE = 0.05

#: Decimal places the source publishes. The residual is a sum and difference of
#: 2-decimal values, so its true value is always a multiple of 0.01 and anything
#: finer is float error. Rounding to this before the comparison is what makes
#: the boundary DETERMINISTIC — see :func:`fg_buckets_are_consistent`.
_FG_SOURCE_DECIMALS = 2

#: What fraction of a batch's kicker rows may have a BROKEN decomposition before
#: :func:`ingest_projections` refuses to store a DERIVED 50+ count.
#:
#: The sibling of ``espn_projections._MAX_DRIFTED_FRACTION`` and sized the same
#: way — a high-water mark, not a median, because the failure is row-shaped.
#: MEASURED, per season, rows beyond the tolerance above (re-measured
#: 2026-08-30 THROUGH the rounded comparison the predicate actually makes; the
#: raw-float numbers this comment used to quote were 3.86 / 6.59 / 0.56 / 3.36 /
#: 0.00 / 0.00, and no season changes side of the ceiling either way):
#:
#:     2021 3.67%   2022 5.84%   2023 0.56%   2024 2.06%   2025 0.00%   2026 0.00%
#:
#: So the seasons this repo drafts on are CLEAN and the historical panel is not:
#: in 2021/2022/2024 a visible minority of rows have buckets that sum to roughly
#: ``fga`` rather than ``fgm``, and no residual taken from those is the 50+ count.
#: The threshold therefore lets 2025/2026 through and REFUSES a derived backfill
#: of 2021-2024, which is the correct answer in both directions.
_MAX_BUCKET_MISMATCH_FRACTION = 0.02


class KickerBucketMismatch(ValueError):
    """Too much of a batch's made-FG bucket decomposition does not hold.

    The ``require_columns`` idea applied to VALUES rather than column names.
    ``fgm`` is the total made count and the distance buckets are supposed to
    partition it; when they sum to MORE than the total, the residual between them
    is not "the makes at distances the feed does not bucket" — it is noise, and a
    50+ count derived from it is invented.

    Raised ONLY on the ``derive_fg_50_plus=True`` path, and that scoping is
    deliberate. Nothing else in this module reads the buckets against ``fgm``:
    ``fg_missed`` is ``fga - fgm``, which a bad bucket cannot touch. Raising on
    the DEFAULT path would therefore break ``ingest backfill`` (measured: it
    would abort 2021, 2022 and 2024) over rows that no stored value depends on —
    the cry-wolf failure ``base.note_drops(by_design=)`` exists to prevent. On
    that path the same rows are COUNTED and reported instead.
    """

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


def unbucketed_fg_makes(stats) -> float | None:
    """Made field goals ``fgm`` reports that the distance buckets do not carry.

    ``None`` when the row has no ``fgm`` at all (not a kicker row, or a kicker
    the feed does not forecast) — "we cannot tell", never 0.0.

    THE DEFECT THIS FUNCTION EXISTS FOR, measured 2026-08-30 against the live
    Sleeper feed (season 2026, weeks 1-18, 2,754 kicker rows). ``fgm_50p`` is
    mapped by :data:`_KICKER_DIRECT_MAP` and **the 2026 feed does not serve it,
    under that or any other name**: the kicker keys actually served are ``fga``,
    ``fgm``, ``fgm_0_19``, ``fgm_20_29``, ``fgm_30_39``, ``fgm_40_49``,
    ``fgm_yds``, ``fgmiss_30_39``, ``fgmiss_40_49``, ``xpa``, ``xpm``,
    ``xpmiss`` — and ``fgm_50p`` appears in 0 of 2,754 rows. So the 50+ makes
    scored ZERO while ``fg_missed`` (``fga - fgm``) went on charging -1 for every
    miss. Confirmed on the stored table too: 0 of 22,840 kicker rows carry a
    non-zero ``fg_made_50_59`` or ``fg_made_60``, while 22,840 carry
    ``fg_missed``. That is the silently-absent-column failure in its purest form
    — the code believed it was reading a bucket that was not there, nothing
    raised, and the loss showed up only as kickers being uniformly cheap.

    IT IS UPSTREAM DRIFT, NOT A MIS-TRANSCRIPTION, and that matters for how it is
    handled: read today, the 2021, 2022, 2023, 2024 AND 2025 feeds all serve
    ``fgm_50p``. Only 2026 — the season being drafted — does not. So the mapping
    was right when item 1.5 wrote it, ``require_columns`` could never have caught
    this (it is a key inside a stat dict, not a DataFrame column), and the
    mapping is kept rather than deleted because the key may well come back.

    THE FEED CARRIES THE INFORMATION ANYWAY, in exactly the shape ``fg_missed``
    already uses: ``fgm`` is the total made count and the distance buckets are
    disjoint, so the makes the buckets do not account for ARE the long ones.
    Measured over the same 576 forecast kicker-weeks, that residual is positive
    in EVERY row (min +0.05, max +0.52 per week) and never negative, and over a
    season it runs 8.8% to 25.8% of a kicker's makes (median 19.9%) — the right
    order for real 50+ share, and worth 15 to 43 house points a season.

    Two independent cross-checks that it really is the 50+ bucket and not a
    bookkeeping artefact: ESPN's own projected 50-59 counts for the same 32
    kickers average 5.78 against this residual's 5.30; and pricing the residual
    at the house 50-59 rate reproduces ESPN's horizon-adjusted kicker totals to
    a mean of 1.9% (see ``core/kicker_board``).

    KNOWN AND DISCLOSED: the residual bundles 60+ with 50-59, so a projected 60+
    leg scores +5 rather than +6. That is the same lossiness ``fgm_50p`` itself
    documents, it is rare, and no source this repo reads projects a 60+ make
    (ESPN's 60+ count is 0.0 for all 32 kickers).
    """
    fgm = stats.get("fgm")
    if not _present(fgm):
        return None
    bucketed = sum(
        base._clean(stats.get(k)) for k in _FG_MADE_BUCKET_KEYS if _present(stats.get(k))
    )
    return base._clean(fgm) - bucketed


def fg_buckets_are_consistent(stats) -> bool:
    """False when this row's distance buckets sum to MORE makes than ``fgm``.

    ``True`` for a row with no ``fgm`` at all — there is no decomposition to be
    inconsistent with, and a QB row must not read as a broken kicker.

    WHY THIS IS A PREDICATE RATHER THAN A RAISE: over 3,231 kicker rows
    (Sleeper, 2021-2026) 567 over-run their own ``fgm``. The large majority are
    rounding — 438 of them by exactly -0.01 — but a real tail runs out to -0.37,
    and those rows have buckets summing to roughly ``fga`` instead of ``fgm``
    (a 2021 week-2 row: fgm 1.81, buckets 2.18, fga 2.16), so their residual
    measures nothing at all. They are 3.9% / 6.6% / 0.6% / 3.4% / 0.0% / 0.0% of
    seasons 2021-2026 — the drafting seasons are clean and the history is not.
    The two populations OVERLAP around the threshold; see
    :data:`_FG_BUCKET_TOLERANCE` for the histogram and for what the ambiguity
    can reach (nothing that gets stored).

    THE COMPARISON IS ROUNDED FIRST, and that is not cosmetic. The residual is a
    sum and difference of 2-decimal source values, so its exact value is always a
    multiple of 0.01 — but in binary it lands a hair either side. MEASURED: 21 of
    the 3,231 rows have a residual within 1e-6 of exactly -0.05, and comparing
    raw floats classified 9 of them CONSISTENT and 12 INCONSISTENT purely on
    which way the float error fell. A predicate whose answer depends on float
    noise is not a predicate. Rounding to :data:`_FG_SOURCE_DECIMALS` first makes
    every row on the boundary answer the same way (CONSISTENT — the permissive
    side, and the side that changes no stored value because a negative residual
    is never derived from).
    """
    residual = unbucketed_fg_makes(stats)
    if residual is None:
        return True
    return round(residual, _FG_SOURCE_DECIMALS) >= -_FG_BUCKET_TOLERANCE


def map_sleeper_projection(raw_row, *, derive_fg_50_plus: bool = False) -> dict | None:
    """Map ONE raw Sleeper projection to a canonical scoring-key stat dict.

    Returns ``None`` for a non-scoring position (FB/CB/P/...) — the caller skips
    it. Otherwise returns a dict of ONLY scoring.py canonical keys (a subset of
    ``validate_projection_keys``'s allow-list), so the result both scores
    directly and passes the strict validator. ``pts_ppr`` is deliberately NOT in
    this dict (it is captured as the non-scoring ``projected_points`` by the
    ingester); a key is emitted only when its source value is present, so an
    absent bracket input stays absent (never a phantom 0-allowed shutout).

    ``derive_fg_50_plus`` folds :func:`unbucketed_fg_makes` into
    ``fg_made_50_59``. It is **OFF by default and that default is deliberate**:
    turning it on changes what a future ingest STORES, and therefore what
    ``build_valuation`` and every board downstream of it return — a change that
    belongs to an integrator making it on purpose, not to an ingester's default
    quietly re-pricing a board between two scheduled runs. The correction is
    also available without re-ingesting anything, from an independent source,
    via ``core/kicker_board``.

    A row whose buckets over-run its own ``fgm`` is NEVER derived from, in either
    mode — its residual is noise, not a count of long makes. Whether a BATCH of
    such rows is tolerated is :func:`ingest_projections`'s decision, not this
    one's: a mapper that raised would take the whole historical backfill with it
    over rows no stored value depends on.
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

    # The 50+ makes the feed does not bucket. Subtracting the mapped buckets
    # INCLUDING fgm_50p is what makes this safe against a feed that starts
    # serving one: the residual goes to zero rather than double-counting the
    # same makes.
    #
    # ``residual > 0`` is also the whole consistency check, and deliberately so
    # rather than a second call to ``fg_buckets_are_consistent``: a broken
    # decomposition is by definition one whose buckets OVER-run ``fgm``, so its
    # residual is negative and this same condition already excludes it. A second
    # guard here would be unreachable, and unreachable guards are how a reviewer
    # comes to believe something is checked that is not.
    # ``test_the_positivity_check_is_the_consistency_check`` pins the equivalence.
    residual = unbucketed_fg_makes(stats) if derive_fg_50_plus else None
    if residual is not None and residual > 0.0:
        mapped["fg_made_50_59"] = mapped.get("fg_made_50_59", 0.0) + residual

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


def ingest_projections(conn, rows, *, retrieved_as_of: str, bulk_historical: bool = False,
                       derive_fg_50_plus: bool = False) -> int:
    """Persist Sleeper projection rows, stamping the two knowledge-time regimes.

    ``bulk_historical=False`` (default, forward/live): ``knowable_as_of =
    retrieved_as_of``. ``bulk_historical=True``: ``knowable_as_of =
    week_first_gameday_map(season, week)`` (schedules must be ingested first) —
    read those rows ONLY via ``base.latest_truth(get_projections)``. Non-scoring
    positions are filtered (not counted as drops); a bulk row whose (season,
    week) has no gameday is dropped via ``base.note_drops`` rather than stored
    with a leaky NULL knowledge time. Returns rows written.

    ``derive_fg_50_plus`` is threaded to :func:`map_sleeper_projection` and is
    OFF by default (see that function for why). When it is off, the makes it
    would have stored are still COUNTED and reported through
    ``base.note_incomplete``, so the loss is visible in the run log instead of
    being invisible in the way it was for every ingest before this one.

    When it is ON, the batch is refused with :class:`KickerBucketMismatch` if
    more than :data:`_MAX_BUCKET_MISMATCH_FRACTION` of its kicker rows have a
    decomposition that does not hold — because a derived 50+ count taken from
    those rows is invented, and storing a board half-derived from noise is worse
    than the defect it was meant to fix. The refusal is scoped to this flag on
    purpose; see :class:`KickerBucketMismatch`.

    THE BATCH IS THE UNIT, and ``pull_projections`` calls this once PER WEEK, so
    a derived historical backfill can end up partly derived — a clean week
    stores, a broken week refuses, and the season is then a mixture. Measured:
    2026 is clean in all 18 weeks, so the season this repo drafts on is uniform;
    2022 is clean in week 2 and not in week 15. Derive a past season only if a
    mixed panel is acceptable to whatever reads it.
    """
    retrieved = base.iso_date(retrieved_as_of)
    crosswalk = _sleeper_to_gsis(conn)
    wfg = base.week_first_gameday_map(conn) if bulk_historical else None

    out: list[dict] = []
    dropped = 0
    considered = 0
    kicker_rows = 0
    unbucketed_rows = 0
    unbucketed_makes = 0.0
    mismatched_rows = 0
    for raw in rows:
        mapped = map_sleeper_projection(raw, derive_fg_50_plus=derive_fg_50_plus)
        if mapped is None:
            continue  # non-scoring position — expected filter, not a drop
        considered += 1
        validate_projection_keys(mapped)  # fail loud on a mis-mapped key

        # Long-make accounting, whether or not anything was stored. Counted here
        # rather than inside the mapper so one run reports one number.
        stats_in = raw.get("stats") or {}
        residual = unbucketed_fg_makes(stats_in)
        if residual is not None:
            kicker_rows += 1
            if not fg_buckets_are_consistent(stats_in):
                mismatched_rows += 1
            elif not derive_fg_50_plus and residual > 0.0:
                unbucketed_rows += 1
                unbucketed_makes += residual

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

    # THE BATCH GATE. Checked BEFORE the write, like every other collapse floor
    # in this package, so a refused batch stores nothing rather than half of it.
    if derive_fg_50_plus and kicker_rows and (
        mismatched_rows / kicker_rows > _MAX_BUCKET_MISMATCH_FRACTION
    ):
        raise KickerBucketMismatch(
            f"refusing to store DERIVED 50+ made-FG counts: {mismatched_rows} of "
            f"{kicker_rows} kicker rows in this batch have distance buckets that sum "
            f"to MORE makes than their own fgm (beyond the {_FG_BUCKET_TOLERANCE:g} "
            f"rounding tolerance) — {100 * mismatched_rows / kicker_rows:.1f}%, over "
            f"the {100 * _MAX_BUCKET_MISMATCH_FRACTION:.0f}% ceiling. On those rows "
            "the residual is not a count of long makes, so the derivation would "
            "invent one. Measured: seasons 2025 and 2026 are clean (0.0%); 2021, "
            "2022 and 2024 are not (3.7%, 5.8%, 2.1%). Ingest this batch WITHOUT "
            "derive_fg_50_plus, or price its kickers from core/kicker_board."
        )

    base.note_drops("projections", dropped, considered, why="no week gameday")
    if unbucketed_rows:
        base.note_incomplete(
            "projections", unbucketed_rows, kicker_rows,
            why=f"{unbucketed_makes:.1f} made FGs of 50+ yds stored as ZERO while "
                "fg_missed still charges every miss (the feed serves no 50+ bucket; "
                "pass derive_fg_50_plus=True, or read core/kicker_board)",
        )
    if mismatched_rows:
        base.note_incomplete(
            "projections", mismatched_rows, kicker_rows,
            why="distance buckets sum to more makes than fgm, so this row's 50+ "
                "count is UNKNOWABLE from the feed (never derived from; fg_missed "
                "= fga - fgm is unaffected)",
        )
    return base.upsert(conn, "projections", out, key_cols=_PK_COLS)


def pull_projections(conn, season, weeks, *, retrieved_as_of: str, bulk_historical: bool = False,
                     derive_fg_50_plus: bool = False) -> int:
    """Pull Sleeper projections for ``season`` across ``weeks`` and store them.
    ``nfl.import_sleeper_projections`` is the network seam tests patch."""
    total = 0
    for week in weeks:
        rows = nfl.import_sleeper_projections(season, week)
        total += ingest_projections(
            conn, rows, retrieved_as_of=retrieved_as_of, bulk_historical=bulk_historical,
            derive_fg_50_plus=derive_fg_50_plus,
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
