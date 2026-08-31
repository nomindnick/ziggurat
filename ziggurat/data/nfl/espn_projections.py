"""ESPN projected stat lines — the SECOND projection source (item 3.9).

WHY THIS MODULE EXISTS. Every valuation in this system — the VOR board, the
draft engine, the marginal/waiver board, streaming — prices through ONE feed:
Sleeper's ``sleeper_rotowire`` weekly projections. A single-source system cannot
tell "this player is good" from "this feed likes this player", and the 2026
slot-9 strategy note already names the symptom (a WR the engine takes at 73% in
round 8 on one flat-rate feed's opinion). This module lands an INDEPENDENT
second opinion so ``core/projection_ensemble.py`` can compute a disagreement
signal.

THE MEASURED FINDING THAT SHAPED IT (2026-08-30, live 2026 pool, 1,030 players).
ESPN publishes a full projected STAT LINE — not just a point total — in the SAME
``kona_player_info`` response ``espn_source.fetch_player_universe`` already
pulls for the draft board (item 2.1). Each player carries a ``stats`` array; an
entry is identified by

    statSourceId    0 = actual, 1 = PROJECTED
    statSplitTypeId 0 = whole season, 1 = one scoring period
    seasonId        the season the entry describes
    stats           {ESPN stat id (as a string) -> value}

so the projection is re-derivable through ``core/scoring.py`` rather than
trusted as a foreign point total. That matters for RULE 2 and it is the whole
reason this source is usable at K and D/ST:

  * ESPN's own ``appliedTotal`` on this endpoint is computed under THIS
    LEAGUE's scoring, not ESPN's default — the request goes through the league
    endpoint, so ESPN applies the league's settings. VERIFIED, not assumed: the
    stat line re-scored through ``scoring.py`` reproduces ``appliedTotal`` to
    1e-6 on **32/32 kickers and 32/32 D/ST** (the distance kicker with −1/miss
    AND both D/ST bracket systems, the exact places ESPN's default differs) and
    on **459/460 offensive players**. The one exception is a two-way player,
    explained below.
  * We still do NOT store ``appliedTotal`` as a scoring input. It is persisted
    as ``espn_applied_total``, a CROSS-CHECK ONLY — the same role and the same
    justification as ``projections.projected_points`` (see ``projections.py``).
    Every point this system prices comes from ``scoring.score``. The residual
    between the two is what ``_check_residual`` uses as a schema-drift alarm,
    which is a far stronger guard than a column-presence check: if ESPN
    renumbers a stat id, the re-derived total stops matching and the ingest
    refuses, instead of silently storing a board that is wrong by one stat.

THREE THINGS THE SOURCE CARRIES THAT THE SLEEPER FEED DOES NOT:

  1. **Per-week projections that are actually week-specific.** The Sleeper feed
     is a flat season rate (item 3.2: median week-to-week CV ~1% for every skill
     position). ESPN's ``statSplitTypeId == 1`` entry is opponent-aware. The
     endpoint serves only the CURRENT scoring period, so we store that one week
     alongside the season total rather than a full 17-week panel.
  2. **A projected GAMES count** (stat id 210). Live 2026: 514 of 524 projected
     players carry 17.0; ten carry less (a TE at 16, a RB at 11, a WR at 10, a
     QB at 4). That is ESPN pricing in a KNOWN absence, and the flat-rate feed
     has no such column. It is stored as ``projected_games`` and deliberately
     kept OUT of the points (``projection_ensemble`` surfaces it as a separate
     availability opinion — folding an availability discount into a "points
     blend" without saying so is the decomposition-wearing-a-mechanism's-label
     mistake the item-3.2 audit charged for).
  3. **D/ST bracket BAND COUNTS** — see the next section.

D/ST: WHY BAND COUNTS, AND WHY THEY ARE NOT A SCORING CONSTANT. ESPN does not
project a D/ST's season points-allowed as one number; it projects the EXPECTED
NUMBER OF GAMES in each bracket band (ids 89/90/91/92/121-125 for points
allowed, 128-136 for yards allowed). That is exactly the right shape for a
NON-LINEAR bracket — the expectation of a bracketed value, not the bracket of an
expected value — and it is why the D/ST re-derivation matches ESPN exactly. The
band → points conversion never hard-codes a number (rule 2): ``_band_points``
asks ``scoring.score_dst`` what a value at each END of the band scores and
REFUSES if the two disagree, i.e. if the ESPN band is not wholly contained in
one house bracket. So a re-banding on either side fails loud instead of
mispricing silently. This mirrors the pattern ``scoring._FG_COUNT_KEY_DISTANCES``
already uses for bucketed kicker counts.

The band counts are stored under names (``pa_games_*`` / ``ya_games_*``) that
are NOT scoring keys, and the scalar ``points_allowed`` / ``yards_allowed``
columns are deliberately ABSENT from this table. ESPN's D/ST season row does
carry a season-total points-allowed (~322), and a column of that name would let
a caller's naive ``scoring.score(pos, dict(row))`` price 322 through the "46+"
bracket once — catastrophically wrong and silent. With the scalars absent, a naive score
merely OMITS the brackets, which is loud rather than plausible: measured across
the 32 live 2026 defences the omitted bracket contribution runs from **-45.8 to
+27.9 points** (median -12.9) on totals of 45-131, so it does not merely scale
the board — it REORDERS it, and it is negative for a bad defence and positive
for a good one. **Price a stored row with :func:`house_points`, never with a
bare ``scoring.score``** — ``tests/test_projection_ensemble.py`` pins that
difference so the trap stays visible.

ONE STAT THE HOUSE SCORES AND THIS MODULE DOES NOT. The league's ESPN settings
(``tests/fixtures/espn/scoring_format.json``) contain ``FTD`` — "Fumble
Recovered for TD", stat id 63, 6.0 points — and ``core/scoring.py`` has no key
for it. It is left UNPRICED here rather than smuggled onto a neighbouring key,
and it is the ENTIRE residual against ESPN's own total on offensive players:
adding ``6 × stat 63`` closes 459 of 460 rows to 1e-6. Measured cost of leaving
it out: mean −0.11 pts on a ~300-pt QB, −0.03 RB, −0.02 WR, −0.01 TE (worst
single row −0.34). Leaving it out is also the more COMPARABLE choice, which is
what this module is for: the Sleeper mapper has no key for it either, so the
omission cancels on both sides of the ensemble instead of showing up as
disagreement. See ``UNPRICED_STAT_IDS``.

The 460th row is Travis Hunter, a two-way player whose OFFENSIVE projection
carries return/defensive TDs (ids 93/103/104). Those, with the kick- and
punt-return TDs (101/102), are mapped to ``special_teams_tds`` — the key
``scoring.py`` documents as riding ``points_per_def_td`` because it is "the same
value the league gives every return TD" (all five ids score 6.0 in the house
fixture). 98 of 460 offensive rows carry a non-zero one, so this is not a
one-player special case.

NETWORK. There is no new network code and no new User-Agent: this module reuses
``espn_source.fetch_player_universe`` verbatim — the one ESPN seam, already
bounded by ``net.bounded_espn()`` (item 3.1b) and already carrying the cookie/
client fingerprint the ESPN edge requires (item 3.6 learned that the hard way on
the news wire). Tests patch that seam; nothing here goes to the network offline.
A caller that has already fetched the universe (e.g. alongside
``espn_ranks.pull_espn_ranks``) should hand the raw list to
:func:`ingest_espn_projections` rather than pull it twice.
"""

from collections.abc import Mapping

from ziggurat.core import scoring
from ziggurat.data.nfl import base, espn_ranks

#: Provenance label stored on every row; the ``projections.SOURCE`` analogue.
SOURCE = "espn"

#: ``week`` value for the WHOLE-SEASON projection. ESPN itself stamps the season
#: split with ``scoringPeriodId = 0``, so this is the source's own convention,
#: not an invented sentinel. A real scoring period is always >= 1.
SEASON_WEEK = 0

# ESPN ``stats`` entry discriminators (see module docstring).
_STAT_SOURCE_PROJECTED = 1
_SPLIT_SEASON = 0
_SPLIT_WEEK = 1

#: ESPN stat id for games played/projected. Not a scoring input.
GAMES_STAT_ID = "210"

# --------------------------------------------------------------- stat-id maps
#
# ESPN stat id (as the string key ESPN uses) -> the canonical ``scoring.py`` key.
# STRICT allow-list, exactly like ``projections._OFFENSE_MAP``: only keys
# ``scoring.py`` actually reads are ever emitted, and ``validate_scoring_keys``
# enforces that against the scoring tables themselves.
#
# The ids come from ``espn_api.football.constant.PLAYER_STATS_MAP`` and were each
# confirmed against the live payload by reproducing ESPN's own ``appliedTotal``.
# Note which ids are NOT here and why:
#   * 22 / 40 / 61 are the PER-GAME forms of passing / rushing / receiving yards
#     (verified: id 61 == id 42 / 17 for every 17-game row). Using them would
#     divide every skill projection by ~17.
#   * 41 is a second receptions id that the live payload does not populate; 53
#     is the one that carries the count and the one the league scores (REC, id
#     53, in the ESPN scoring fixture).
#   * 62 ("2PtConversions") is the SUM of 19 + 26 + 44; scoring the components
#     and the sum would double every two-point conversion.
#   * 73 ("turnovers") is the SUM of 20 + 72.
_OFFENSE_STAT_IDS: dict[str, str] = {
    "3": "passing_yards",
    "4": "passing_tds",
    "20": "interceptions",
    "19": "passing_2pt_conversions",
    "24": "rushing_yards",
    "25": "rushing_tds",
    "26": "rushing_2pt_conversions",
    "42": "receiving_yards",
    "43": "receiving_tds",
    "44": "receiving_2pt_conversions",
    "53": "receptions",
    "72": "fumbles_lost",
}

# Kicker made-FG buckets map 1:1 onto scoring.py's bucketed-count keys.
#   80 = FG made under 40, 77 = 40-49, 198 = 50-59, 201 = 60+.
# Id 74 ("madeFieldGoalsFrom50Plus") is deliberately NOT used: it is the 50-59
# and 60+ buckets ADDED TOGETHER (live 2026: id 74 == id 198 for all 32 kickers,
# because no kicker carries a 60+ projection yet), and the house pays 5 for 50-59
# but 6 for 60+. Using 74 would silently underpay a 60+ leg the moment ESPN
# starts projecting one — the exact loss the Sleeper mapper documents as its own
# ``fgm_50p`` defect and cannot fix.
_KICKER_STAT_IDS: dict[str, str] = {
    "80": "fg_made_0_39",
    "77": "fg_made_40_49",
    "198": "fg_made_50_59",
    "201": "fg_made_60",
    "86": "pat_made",
    "85": "fg_missed",
}

# D/ST per-event ids. 94 ("defensiveTouchdowns") and 105 ("defensivePlusSpecial
# TeamsTouchdowns") are AGGREGATES (verified on the live payload: 94 == 103 + 104
# and 105 == 93 + 94 + 101 + 102) and are excluded so no TD is counted twice.
_DST_EVENT_STAT_IDS: dict[str, str] = {
    "99": "sacks",
    "95": "def_interceptions",
    "96": "fumble_recoveries",
    "98": "safeties",
    "97": "blocked_kicks",
    "209": "one_point_safeties",
    "206": "two_point_returns",
}

#: The five non-scrimmage TD ids, each worth 6.0 in the house scoring fixture:
#: blocked punt/FG return (93), kickoff return (101), punt return (102),
#: interception return (103), fumble return (104). Summed onto ``def_tds`` for a
#: D/ST and onto ``special_teams_tds`` for an offensive player (see the module
#: docstring); both keys ride ``points_per_def_td``.
_RETURN_TD_STAT_IDS: tuple[str, ...] = ("93", "101", "102", "103", "104")

#: House scoring items ESPN projects that ``core/scoring.py`` has no key for, so
#: this module cannot price them. Kept as data (not a comment) so a future
#: ``scoring.py`` fix has a checklist and a reviewer can see the omission is
#: deliberate. Value is ``(the abbreviation in the league's ESPN scoring
#: settings, why it is left alone)``.
#:
#: NOTE the deliberate absence of the POINTS VALUE (rule 2 — house scoring
#: numbers live only in ``core/scoring.py``, and this module must not become a
#: second, stale copy of one). ``tests/test_projection_ensemble.py`` reads the
#: value from the committed league scoring fixture instead, which is the
#: authority ``scoring.py`` itself is checked against.
UNPRICED_STAT_IDS: dict[str, tuple[str, str]] = {
    "63": (
        "FTD",
        "Fumble Recovered for TD: present in the league's ESPN scoring settings "
        "but absent from core/scoring.py. Left unpriced rather than mapped onto "
        "a neighbouring key — the Sleeper mapper has no key for it either, so "
        "the omission cancels across the ensemble instead of reading as "
        "disagreement. Measured cost: mean -0.11 pts on a ~300-pt QB, -0.03 RB, "
        "-0.02 WR, -0.01 TE; worst single 2026 row -0.34.",
    ),
}

# D/ST bracket BAND COUNTS: ESPN stat id -> (db column, band low, band high).
# The band bounds are the SOURCE's band definitions, not house scoring numbers;
# ``_band_points`` converts a band to points by asking scoring.py and refuses if
# the band straddles two house brackets. The open-ended top bands use a value
# comfortably inside the unbounded final bracket.
_PA_BANDS: dict[str, tuple[str, float, float]] = {
    "89": ("pa_games_0", 0.0, 0.0),
    "90": ("pa_games_1_6", 1.0, 6.0),
    "91": ("pa_games_7_13", 7.0, 13.0),
    "92": ("pa_games_14_17", 14.0, 17.0),
    "121": ("pa_games_18_21", 18.0, 21.0),
    "122": ("pa_games_22_27", 22.0, 27.0),
    "123": ("pa_games_28_34", 28.0, 34.0),
    "124": ("pa_games_35_45", 35.0, 45.0),
    "125": ("pa_games_46_plus", 46.0, 200.0),
}
_YA_BANDS: dict[str, tuple[str, float, float]] = {
    "128": ("ya_games_0_99", 0.0, 99.0),
    "129": ("ya_games_100_199", 100.0, 199.0),
    "130": ("ya_games_200_299", 200.0, 299.0),
    "131": ("ya_games_300_349", 300.0, 349.0),
    "132": ("ya_games_350_399", 350.0, 399.0),
    "133": ("ya_games_400_449", 400.0, 449.0),
    "134": ("ya_games_450_499", 450.0, 499.0),
    "135": ("ya_games_500_549", 500.0, 549.0),
    "136": ("ya_games_550_plus", 550.0, 5000.0),
}

#: Canonical ``scoring.py`` columns stored on every row (uniform key set for
#: ``base.upsert``). Mirrors ``projections._SCORING_COLUMNS`` name-for-name so a
#: reader of both tables sees one vocabulary — MINUS the scalar bracket inputs
#: ``points_allowed`` / ``yards_allowed``, which this source does not serve in a
#: bracketable form (see the module docstring) and which are absent by design.
_SCORING_COLUMNS: tuple[str, ...] = (
    "passing_yards", "passing_tds", "interceptions", "rushing_yards",
    "rushing_tds", "receptions", "receiving_yards", "receiving_tds",
    "fumbles_lost", "passing_2pt_conversions", "rushing_2pt_conversions",
    "receiving_2pt_conversions", "special_teams_tds",
    "fg_made_0_39", "fg_made_40_49", "fg_made_50_59", "fg_made_60",
    "pat_made", "fg_missed",
    "sacks", "def_interceptions", "fumble_recoveries", "safeties",
    "blocked_kicks", "def_tds", "one_point_safeties", "two_point_returns",
)

#: The D/ST band-count columns (NOT scoring keys — see the module docstring).
_BAND_COLUMNS: tuple[str, ...] = tuple(
    col for col, _, _ in
    [v for v in _PA_BANDS.values()] + [v for v in _YA_BANDS.values()]
)

#: Full stored key set, so ``base.upsert`` always sees uniform rows.
_ROW_COLUMNS: tuple[str, ...] = (
    "source", "espn_key", "espn_id", "gsis_id", "player", "position", "team",
    "season", "week", "projected_games", "espn_applied_total",
    *_SCORING_COLUMNS, *_BAND_COLUMNS,
)

#: The table's declared PRIMARY KEY, handed to ``base.upsert`` so its return
#: value is DISTINCT keys written rather than rows offered (item 3.2c, F-G).
#:
#: ``source`` LEADS the key, matching ``projections`` (migration 003). An earlier
#: draft omitted it while the migration comment advertised that a second opinion
#: "lands additively, without a migration" — measured false: two rows identical
#: in (season, week, espn_key, retrieved_as_of) but differing in ``source``
#: collapsed to ONE, the second silently REPLACING the first. ``base.upsert``
#: asserts this tuple equals the table's declared primary key, so the two can
#: never drift apart again.
_PK_COLS = ("source", "season", "week", "espn_key", "retrieved_as_of")


# ----------------------------------------------------------------- guard knobs

#: Minimum fraction of the stored snapshot an incoming pull must carry before it
#: is allowed to overwrite same-key rows. Same value and same reasoning as
#: ``espn_ranks._MIN_BOARD_FRACTION`` and ``league.state._MIN_SNAPSHOT_FRACTION``:
#: a refused pull is retried by the next run; a corrupted board is not.
_MIN_SNAPSHOT_FRACTION = 0.75

#: Schema-drift alarm, per row: how far ``house_points`` may sit from ESPN's own
#: league-applied total, relative. MEASURED on the live 2026 pool: K and D/ST are
#: exact (0.0) and the worst single offensive row is 0.0016, all of it the
#: known-unpriced FTD. 0.02 therefore leaves ~12x headroom over the worst
#: observed row.
_MAX_ROW_RESIDUAL = 0.02

#: ...and how many rows may exceed it before the pull is refused.
#:
#: WHY A FRACTION-OVER-TOLERANCE AND NOT A MEDIAN. The first version of this
#: guard compared the MEDIAN relative residual against a threshold, and a test
#: proved it toothless against the most likely real drift: a renumbered stat id
#: breaks only the rows that carry that stat. Receiving yards (id 42) appears in
#: 460 of the 1,041 live rows — 44%, so renaming it moved the median not at all
#: and the guard passed a board with every WR, TE and receiving RB silently
#: 45% low. A high-water mark is the right shape: essentially NO row should miss
#: ESPN's own arithmetic, so 2% of rows is already a loud alarm while still
#: tolerating a handful of oddities upstream may ship.
_MAX_DRIFTED_FRACTION = 0.02

#: Rows whose ESPN total is below this are excluded from the residual median: a
#: deep-bench projection of 1.7 points divides a rounding difference into a huge
#: relative number and would make the alarm meaningless.
_RESIDUAL_MIN_TOTAL = 5.0

#: Minimum fraction of EACH POSITION's mapped rows that must carry ESPN's own
#: ``appliedTotal`` before the residual guard is allowed to pass.
#:
#: WHY THIS EXISTS, and why it is the guard's load-bearing half. ``_check_residual``
#: measures the re-derivation against ``appliedTotal``; a row without one is
#: skipped. So the ONE event class the guard exists to catch — ESPN changing the
#: shape of its payload — is also the event that can DELETE the guard's own
#: reference, and the first version failed OPEN: with ``appliedTotal`` stripped
#: it collected zero residuals, returned 0.0 and stored a board with every
#: receiver ~45% low, logged ``ok``. MEASURED (mutation on the live 1,030-player
#: universe): renaming stat id 42 alone RAISES correctly; renaming 42 *and*
#: dropping ``appliedTotal`` wrote 1,041 rows with no exception; dropping it for
#: the offensive positions only (K/D-ST keeping theirs) also wrote 1,041.
#:
#: PER POSITION, not pooled, for the same reason ``_MAX_DRIFTED_FRACTION`` is a
#: high-water mark rather than a median: DRIFT IS POSITION-SHAPED. A pooled floor
#: is cleared by K and D/ST while every offensive row goes unchecked.
#:
#: WHY 0.5: live 2026 coverage is **100% on all six positions** (1,041 of 1,041
#: mapped rows carry an ``appliedTotal``), so this leaves 2x headroom over the
#: observed value while still catching every mutation above. It is the sibling of
#: ``espn_ranks._MIN_EDITORIAL_COVERAGE``, which exists for this exact shape.
_MIN_RESIDUAL_COVERAGE = 0.5


class ProjectionCollapse(RuntimeError):
    """A degraded ESPN response would have overwritten a good stored snapshot.

    The sibling of ``espn_ranks.BoardCollapse`` and ``league.state.SnapshotCollapse``,
    and it exists for the same measured reason even though THIS ingester never
    DELETEs: the primary key ends in ``retrieved_as_of``, so a second pull on the
    same day ``INSERT OR REPLACE``s that day's rows in place. A 20-player
    degraded response cannot shrink the stored snapshot (the other rows survive),
    but it CAN replace the rows it does carry — and the shape item 3.1b measured
    on ``players`` is the dangerous one: not fewer rows, but the SAME keys with
    EMPTIED VALUES, which ``select_as_of`` then resolves to in preference to the
    good earlier pull.

    Two mechanisms close that together, and neither alone is enough:
      * a row with no priceable stat is never written at all (so an emptied
        response cannot shadow a good one — it writes nothing), and
      * this floor, checked BEFORE the write, refuses a snapshot materially
        smaller than the stored one (so a *partial* response cannot look healthy).
    """


class BandBracketMismatch(RuntimeError):
    """An ESPN D/ST band is not wholly inside one house scoring bracket.

    Raised by :func:`_band_points` when the low and high ends of an ESPN band
    score differently under ``scoring.score_dst``. That means the two bracket
    tables have drifted apart — ESPN re-banded, or the house re-bracketed — and
    every D/ST projection this module produces would be silently wrong. It is
    the D/ST analogue of ``base.require_columns``: a shape assumption checked in
    code rather than trusted.
    """


# --------------------------------------------------------------------- mapping


def _stat_entries(raw_player) -> list[dict]:
    entries = raw_player.get("stats")
    return list(entries) if entries else []


def _projection_entries(raw_player, *, season: int) -> list[tuple[int, dict]]:
    """The ``(week, entry)`` pairs this module stores for one raw player.

    Keeps ONLY ``statSourceId == 1`` (projected, never actual) and ONLY
    ``seasonId == season``. Both filters are load-bearing:

      * the same response carries ``statSourceId == 0`` ACTUALS for 2025 and a
        partial 2026 — storing those in a table named "projections" would let a
        backtest grade a projection against itself; and
      * it carries ESPN's PRIOR-SEASON projection (``seasonId == 2025``,
        ``statSourceId == 1``) served TODAY. That is a preseason-2025 opinion
        with today's retrieval stamp; admitting it would put a 2025 forecast in
        the 2026 board keyed only by week.
    """
    out: list[tuple[int, dict]] = []
    for entry in _stat_entries(raw_player):
        if entry.get("statSourceId") != _STAT_SOURCE_PROJECTED:
            continue
        if entry.get("seasonId") != season:
            continue
        split = entry.get("statSplitTypeId")
        if split == _SPLIT_SEASON:
            out.append((SEASON_WEEK, entry))
        elif split == _SPLIT_WEEK:
            period = entry.get("scoringPeriodId")
            if isinstance(period, int) and period >= 1:
                out.append((period, entry))
    return out


def _map_stats(position: str, stats: Mapping) -> dict:
    """ESPN's raw ``{stat id: value}`` -> canonical ``scoring.py`` keys (+ bands).

    Dispatches on the ESPN position label (``espn_ranks.DEFPOS`` values). Returns
    ONLY keys this module stores; an id ESPN did not send is simply absent, so a
    caller can tell "not projected" from "projected zero".
    """
    mapped: dict[str, float] = {}

    def add(key: str, value) -> None:
        if value is None:
            return
        mapped[key] = mapped.get(key, 0.0) + float(value)

    if position in ("QB", "RB", "WR", "TE"):
        for stat_id, key in _OFFENSE_STAT_IDS.items():
            if stat_id in stats:
                add(key, stats[stat_id])
        # Return / non-scrimmage TDs credited to an OFFENSIVE player. Emitted
        # only when at least one id is present, so a player with none keeps a
        # NULL rather than a manufactured 0.0.
        if any(sid in stats for sid in _RETURN_TD_STAT_IDS):
            for sid in _RETURN_TD_STAT_IDS:
                add("special_teams_tds", stats.get(sid) or 0.0)

    elif position == "K":
        for stat_id, key in _KICKER_STAT_IDS.items():
            if stat_id in stats:
                add(key, stats[stat_id])

    elif position in ("D/ST", "DST"):
        for stat_id, key in _DST_EVENT_STAT_IDS.items():
            if stat_id in stats:
                add(key, stats[stat_id])
        if any(sid in stats for sid in _RETURN_TD_STAT_IDS):
            for sid in _RETURN_TD_STAT_IDS:
                add("def_tds", stats.get(sid) or 0.0)
        for stat_id, (col, _lo, _hi) in {**_PA_BANDS, **_YA_BANDS}.items():
            if stat_id in stats:
                add(col, stats[stat_id])

    return mapped


def _scoring_key_allowlist() -> set[str]:
    """The keys ``scoring.py`` actually reads, assembled by importing its tables.

    Copied in spirit from ``projections._scoring_key_allowlist`` (rule 2 — no
    re-hardcoded scoring value here). This table stores no scalar bracket input,
    so ``points_allowed`` / ``yards_allowed`` are NOT added.
    """
    return (
        set(scoring._OFFENSE_WEIGHTS)
        | set(scoring._DST_EVENT_WEIGHTS)
        | set(scoring._FG_COUNT_KEY_DISTANCES)
        | {"pat_made", "fg_missed"}
    )


def validate_scoring_keys(mapped: Mapping) -> None:
    """Raise if a mapped stat dict carries a key ``scoring.py`` would not read.

    Band columns are excluded from the check by name — they are deliberately not
    scoring keys. A typo in a canonical key would otherwise store a column that
    scores 0 forever, silently.
    """
    unknown = set(mapped) - _scoring_key_allowlist() - set(_BAND_COLUMNS)
    if unknown:
        raise ValueError(
            f"ESPN projection stat dict carries non-scoring keys {sorted(unknown)} "
            "(only core/scoring.py keys and the declared pa_/ya_ band columns are stored)"
        )


def map_espn_projection(raw_player, *, season: int) -> list[dict]:
    """Map ONE raw ``p["player"]`` dict to its stored projection rows.

    Returns a list (possibly empty) of partial row dicts — one for the whole
    season (``week == 0``) and one per projected scoring period ESPN served. A
    non-league position is skipped, and so is any entry with NO priceable stat:
    a row carrying only a games count would ``INSERT OR REPLACE`` over a good
    earlier pull's row with all-NULL points, which is the emptied-values shadow
    ``ProjectionCollapse`` documents. Identity follows ``espn_ranks`` exactly —
    ``DEFPOS`` for position, ``PRO_TEAM_MAP`` + ``TEAM_ALIASES`` for team, a NULL
    ``espn_id`` for D/ST (synthetic negative id) with the team abbr carrying the
    non-null ``espn_key``.
    """
    position = espn_ranks.DEFPOS.get(raw_player.get("defaultPositionId"))
    if position is None:
        return []  # IDP / FB / punter — not a league position

    is_dst = raw_player.get("defaultPositionId") == espn_ranks._DST_POSITION_ID
    team = espn_ranks._norm_team(
        espn_ranks._pro_team_map().get(raw_player.get("proTeamId"))
    )
    espn_id = None if is_dst else str(raw_player["id"])
    espn_key = espn_id or team
    if espn_key is None:
        return []  # a teamless D/ST has no durable key; nothing to store

    rows: list[dict] = []
    for week, entry in _projection_entries(raw_player, season=season):
        stats = entry.get("stats") or {}
        mapped = _map_stats(position, stats)
        if not mapped:
            continue  # no priceable stat — never written (see the docstring)
        validate_scoring_keys(mapped)
        row = {
            "source": SOURCE,
            "espn_key": espn_key,
            "espn_id": espn_id,
            "player": raw_player.get("fullName"),
            "position": position,
            "team": team,
            "season": season,
            "week": week,
            "projected_games": base._clean(stats.get(GAMES_STAT_ID)),
            "espn_applied_total": base._clean(entry.get("appliedTotal")),
        }
        row.update({col: None for col in _SCORING_COLUMNS})
        row.update({col: None for col in _BAND_COLUMNS})
        row.update(mapped)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- house points


def _band_points(kind: str, lo: float, hi: float,
                 rules: scoring.ScoringRules = scoring.HOUSE_RULES) -> float:
    """House points for ONE game landing anywhere in an ESPN bracket band.

    ``kind`` is ``points_allowed`` or ``yards_allowed``. The value is obtained by
    ASKING ``scoring.score_dst`` (rule 2 — this module holds no bracket number),
    at both ends of the band, and the two must agree: an ESPN band that straddles
    a house bracket boundary cannot be priced by a single number, and pretending
    otherwise is exactly the silent mispricing this check exists to prevent.
    """
    low = scoring.score_dst({kind: float(lo)}, rules)
    high = scoring.score_dst({kind: float(hi)}, rules)
    if low != high:
        raise BandBracketMismatch(
            f"ESPN {kind} band [{lo:g}, {hi:g}] straddles a house scoring bracket "
            f"({low:+g} at the low end, {high:+g} at the high end). The ESPN band "
            "table and core/scoring.py's brackets have drifted apart; every D/ST "
            "projection would be silently mispriced. Reconcile them before "
            "re-enabling this source."
        )
    return low


def house_points(row: Mapping, *, rules: scoring.ScoringRules = scoring.HOUSE_RULES) -> float:
    """House points for one stored (or freshly mapped) ESPN projection row.

    THE ONLY correct way to price a row from this table. A bare
    ``scoring.score(position, dict(row))`` gets QB/RB/WR/TE and K right but a
    D/ST WRONG — the bracket contribution lives in the ``pa_games_*`` /
    ``ya_games_*`` band columns, which are not scoring keys and which
    ``scoring.score`` therefore ignores in silence. That contribution is worth
    -45.8 to +27.9 points across the 32 live 2026 defences (median -12.9) on
    totals of 45-131: omitting it does not scale the D/ST board, it reorders it.

    Offense and K price straight through ``scoring.score``. D/ST prices its
    events through ``scoring.score_dst`` and then adds, for each band, the
    expected number of games in that band times what one game in that band
    scores. That is the expectation of a bracketed value — the right operation
    for a non-linear bracket, and the reason this re-derivation reproduces
    ESPN's own total exactly on all 32 defences.
    """
    position = row["position"]
    canon = str(position).strip().upper()
    stats = {k: row[k] for k in _SCORING_COLUMNS if _has(row, k)}

    if canon not in scoring.DST_POSITIONS:
        return scoring.score(canon, stats, rules)

    points = scoring.score_dst(stats, rules)
    for _stat_id, (col, lo, hi) in _PA_BANDS.items():
        games = row[col] if _has(row, col) else None
        if games:
            points += float(games) * _band_points("points_allowed", lo, hi, rules)
    for _stat_id, (col, lo, hi) in _YA_BANDS.items():
        games = row[col] if _has(row, col) else None
        if games:
            points += float(games) * _band_points("yards_allowed", lo, hi, rules)
    return points


def _has(row: Mapping, key: str) -> bool:
    """True when ``row`` carries ``key`` at all.

    ``sqlite3.Row`` has no ``.get`` and raises IndexError on an unknown column,
    so this cannot be written as ``row.get(key) is not None`` — the same reason
    ``valuation.weekly_lines`` converts to ``dict`` before scoring.
    """
    try:
        return row[key] is not None
    except (IndexError, KeyError):
        return False


# --------------------------------------------------------------------- ingest


def _snapshot_size(conn, *, season: int, day: str, week: int | None = None) -> int:
    """Stored rows for one season+pull-day, optionally restricted to one ``week``
    partition. ``week=None`` counts the whole day (used only for reporting)."""
    sql = ("SELECT COUNT(*) FROM espn_projections "
           "WHERE source = ? AND season = ? AND retrieved_as_of = ?")
    params: list = [SOURCE, season, day]
    if week is not None:
        sql += " AND week = ?"
        params.append(week)
    return conn.execute(sql, params).fetchone()[0]


def _snapshot_sizes(conn, *, season: int, day: str) -> dict[int, int]:
    """``week -> stored row count`` for one season+pull-day."""
    return {
        int(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT week, COUNT(*) FROM espn_projections "
            "WHERE source = ? AND season = ? AND retrieved_as_of = ? GROUP BY week",
            (SOURCE, season, day),
        )
    }


def _stored_yardstick(conn, *, season: int, stamp: str) -> dict[int, int]:
    """``week -> the size an incoming snapshot is measured against``.

    PER WEEK PARTITION, and that is the whole point. The endpoint serves the
    whole-season split (``week == 0``) PLUS the CURRENT scoring period only, so a
    stored day holds two populations of comparable size (live: 524 season rows +
    517 week rows = 1,041). Pooling them makes the floor measure HOW MANY WEEKS
    ESPN SERVED rather than how much of the PLAYER POOL it served: a legitimate
    offseason/between-periods pull carrying only the season split arrived as 524
    against a 1,041 pooled yardstick and was refused as "a degraded pool" — and,
    because the yardstick takes a max over stored partitions, it stayed refused
    on every retry, sending the operator hunting for expired cookies. Comparing
    each week against its own kind is the honest measurement.

    Within a week, the yardstick is the LARGER of the partition this write would
    overwrite (``stamp``) and the most recent stored partition. Taking the max of
    the two is ``espn_ranks._board_size``'s fix for a measured hole — measuring
    only the newest partition lets a back-stamped write compare itself against
    the wrong (smaller) board and sail through.
    """
    latest = conn.execute(
        "SELECT MAX(retrieved_as_of) FROM espn_projections WHERE source = ? AND season = ?",
        (SOURCE, season),
    ).fetchone()[0]
    sizes = _snapshot_sizes(conn, season=season, day=stamp)
    if latest is not None and latest != stamp:
        for week, n in _snapshot_sizes(conn, season=season, day=latest).items():
            sizes[week] = max(sizes.get(week, 0), n)
    return sizes


def _check_snapshot_size(conn, rows, *, season: int, stamp: str,
                         allow_shrink: bool) -> dict[int, int]:
    """Refuse to overwrite a stored snapshot with a materially smaller one.

    Runs BEFORE the write, never after: a refused pull leaves the stored rows
    untouched and the next run retries. Measures DISTINCT primary keys, not
    ``len(rows)`` — the stored side is post-dedup, so comparing a raw list length
    against it compares two different quantities (``espn_ranks`` pays for that
    lesson in a comment of its own).

    Compares WEEK PARTITION AGAINST WEEK PARTITION (see ``_stored_yardstick``):
    the season split and the current scoring period are two populations of
    similar size, and pooling them made the floor measure how many WEEKS ESPN
    served instead of how much of the PLAYER POOL it served. Returns the
    ``week -> yardstick`` map so the post-write re-count can reuse it.
    """
    if not rows:
        # Unconditional, and NOT covered by allow_shrink: ESPN never legitimately
        # projects zero players for a season it is serving a draft board for.
        raise ProjectionCollapse(
            f"refusing to write an EMPTY ESPN projection snapshot for season {season} "
            f"at {stamp}: the pull mapped 0 priceable rows (expired cookies, a payload "
            "shape change, or a degraded response). Nothing stored was touched; the "
            "next run will retry."
        )

    previous = _stored_yardstick(conn, season=season, stamp=stamp)
    if allow_shrink or not previous:
        return {}  # explicit override, or the first snapshot of the season

    incoming: dict[int, int] = {}
    for key in {(r["season"], r["week"], r["espn_key"]) for r in rows}:
        incoming[key[1]] = incoming.get(key[1], 0) + 1

    for week, stored in sorted(previous.items()):
        if week != SEASON_WEEK and week not in incoming:
            # A scoring-period split that is simply not being served right now.
            # ESPN publishes only the CURRENT period, so between periods (and in
            # the offseason) this partition legitimately vanishes; refusing here
            # is the false alarm that made the pooled floor unfixable by retry.
            # The season split is NOT excused — see below.
            continue
        got = incoming.get(week, 0)
        floor = int(stored * _MIN_SNAPSHOT_FRACTION)
        if got < floor:
            kind = ("the WHOLE-SEASON projection" if week == SEASON_WEEK
                    else f"the week-{week} projection")
            raise ProjectionCollapse(
                f"refusing to overwrite the stored {stamp} ESPN projections for season "
                f"{season}: {kind} carries {got} players vs {stored} stored (floor "
                f"{floor} = {_MIN_SNAPSHOT_FRACTION:.0%}). ESPN likely returned a "
                "degraded pool. Nothing stored was touched; the next run will retry. "
                "Pass allow_shrink only if the shrink is real. (This floor counts "
                "PLAYERS WITHIN ONE WEEK PARTITION: a pull that simply carries no "
                "current scoring period is not a shrink and is not refused here.)"
            )
    # Only the partitions actually policed above go to the post-write re-count.
    # A week the incoming pull does not carry was excused here and must be
    # excused there too, or the re-count re-raises what this loop just forgave.
    return {w: n for w, n in previous.items() if w == SEASON_WEEK or w in incoming}


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _check_residual(rows, *, rules: scoring.ScoringRules) -> float:
    """Refuse a snapshot whose re-derived points stop matching ESPN's own total.

    THE schema-drift guard, and a much sharper instrument than a column-presence
    check: ESPN computes ``appliedTotal`` under this league's settings, so the
    re-derivation through ``scoring.py`` must reproduce it. If ESPN renumbers a
    stat id, the mapping silently drops that stat and the residual jumps — this
    catches it at ingest instead of three weeks later on the draft board.

    Refuses on the FRACTION of rows exceeding ``_MAX_ROW_RESIDUAL``, not on a
    median: drift is position-shaped, so a stat that breaks 44% of the pool
    leaves a median untouched (see ``_MAX_DRIFTED_FRACTION``). Returns the median
    anyway, for logging.

    Rows below ``_RESIDUAL_MIN_TOTAL`` are excluded: a 1.7-point deep-bench
    projection turns a rounding difference into a huge ratio and would make the
    alarm meaningless.

    A row with NO ``espn_applied_total`` is likewise unmeasurable — but it is
    NOT thereby clean, and treating it as clean made this guard fail open on the
    one event it exists for. ``_MIN_RESIDUAL_COVERAGE`` is checked first, per
    position, and refuses the pull when the reference itself has gone missing.
    """
    # ---- FIRST: is the guard's own reference still there? -------------------
    # A row with no `espn_applied_total` is unCHECKABLE, not clean. Counting
    # coverage PER POSITION before measuring anything is what stops the guard
    # failing open when the payload change IS the disappearance of the reference
    # (see `_MIN_RESIDUAL_COVERAGE` for the three measured mutations).
    total_by_pos: dict[str, int] = {}
    have_by_pos: dict[str, int] = {}
    for row in rows:
        pos = row.get("position") or "?"
        total_by_pos[pos] = total_by_pos.get(pos, 0) + 1
        if row.get("espn_applied_total") is not None:
            have_by_pos[pos] = have_by_pos.get(pos, 0) + 1
    thin = sorted(
        (pos, have_by_pos.get(pos, 0), n)
        for pos, n in total_by_pos.items()
        if have_by_pos.get(pos, 0) < _MIN_RESIDUAL_COVERAGE * n
    )
    if thin:
        detail = "; ".join(f"{pos} {have}/{n}" for pos, have, n in thin)
        raise ValueError(
            "ESPN projection schema drift: the cross-check ESPN's own applied "
            "total provides has DISAPPEARED for "
            f"{'some positions' if len(thin) < len(total_by_pos) else 'every position'} "
            f"({detail}; min coverage {_MIN_RESIDUAL_COVERAGE:.0%} per position, "
            "observed 100% on all six live 2026 positions). Without it the "
            "re-derivation cannot be verified at all, so this pull is REFUSED "
            "rather than stored unchecked — an unverifiable board is the failure "
            "this guard exists to prevent, not a clean one. Nothing stored was "
            "touched. Reconcile the payload shape before storing this pull."
        )

    residuals = []
    for row in rows:
        applied = row.get("espn_applied_total")
        if applied is None or abs(applied) < _RESIDUAL_MIN_TOTAL:
            continue
        residuals.append(abs(house_points(row, rules=rules) - applied) / abs(applied))
    if not residuals:
        # Coverage passed, so totals ARE present — they are all below
        # `_RESIDUAL_MIN_TOTAL`. On the live pool 735 of 1,041 rows clear it, so
        # "not one row projects 5 points" is a broken pull, not a quiet season.
        raise ValueError(
            f"ESPN projection schema drift: not one of {len(rows)} mapped rows "
            f"carries an applied total of at least {_RESIDUAL_MIN_TOTAL:g} points, "
            "so the re-derivation could not be checked against anything (live "
            "2026: 735 of 1,041 rows clear that bar). Refusing the pull."
        )
    drifted = [r for r in residuals if r > _MAX_ROW_RESIDUAL]
    if len(drifted) > _MAX_DRIFTED_FRACTION * len(residuals):
        worst = max(drifted)
        raise ValueError(
            "ESPN projection schema drift: re-deriving the stat line through "
            f"core/scoring.py misses ESPN's own applied total by more than "
            f"{_MAX_ROW_RESIDUAL:.0%} on {len(drifted)} of {len(residuals)} rows "
            f"(worst {worst:.1%}; at most {_MAX_DRIFTED_FRACTION:.0%} of rows may). "
            "A stat id was almost certainly renumbered or dropped upstream — "
            "reconcile the _*_STAT_IDS maps before storing this pull."
        )
    return _median(residuals)


def ingest_espn_projections(conn, raw_players, *, retrieved_as_of: str, season: int,
                            rules: scoring.ScoringRules = scoring.HOUSE_RULES,
                            allow_shrink: bool = False) -> int:
    """Persist an ESPN projection snapshot. Returns DISTINCT rows written.

    ESPN's projections are a LIVE MUTABLE signal that ESPN re-publishes and never
    serves a history of, so — exactly like ``espn_draft_ranks`` (item 2.1, design
    D8) — a pull is stamped ``knowable_as_of = retrieved_as_of = the pull day``.
    A backtest reads it through ``base.latest_truth(get_espn_projections)``.

    Two guards run BEFORE anything is written, in this order: the size floor
    (:class:`ProjectionCollapse`) and the re-derivation residual
    (:func:`_check_residual`, which prices every checkable row and therefore also
    surfaces :class:`BandBracketMismatch` on any D/ST band that has drifted out
    of its house bracket). A third, the post-write key-collapse re-count, runs
    inside the transaction. The whole write is ONE transaction, so every one of
    them leaves the store exactly as it was.
    """
    stamp = base.iso_date(retrieved_as_of)
    crosswalk = base.gsis_by_espn(conn)

    rows: list[dict] = []
    considered = 0
    unpriceable = 0
    for raw in raw_players:
        if espn_ranks.DEFPOS.get(raw.get("defaultPositionId")) is None:
            continue  # non-league position — an expected filter, not a drop
        considered += 1
        mapped = map_espn_projection(raw, season=season)
        if not mapped:
            unpriceable += 1
            continue
        for row in mapped:
            row["gsis_id"] = crosswalk.get(row["espn_id"]) if row["espn_id"] else None
            row["retrieved_as_of"] = stamp
            row["knowable_as_of"] = stamp
            for col in _ROW_COLUMNS:
                row.setdefault(col, None)
            rows.append(row)

    # An ESPN player with no projection at all is NORMAL: roughly half the live
    # 2026 pool (506 of 1,030) is deep-bench and unprojected. So this MUST stay
    # off `refresh.run_ingest`'s drop ceiling — a 49% "drop" every single run is
    # how the one alarm that matters gets ignored (the item-3.1b lesson).
    #
    # `by_design=True` rather than `note_incomplete`, deliberately: none of
    # base.py's three channels is an exact fit (nothing was dropped, nothing was
    # kept-with-a-missing-field, nothing collapsed), and of the three this one
    # logs the only accurate verb — "filtered", with the real reason attached —
    # while `note_incomplete` would log "kept 506/1030 rows with a missing field"
    # about rows that were never kept at all. Both keep it off the ceiling.
    base.note_drops(
        "espn_projections", unpriceable, considered, by_design=True,
        why="ESPN publishes no projected stat line for this player",
    )

    previous = _check_snapshot_size(conn, rows, season=season, stamp=stamp,
                                   allow_shrink=allow_shrink)
    _check_residual(rows, rules=rules)

    with conn:
        written = base.upsert(conn, "espn_projections", rows, commit=False,
                              key_cols=_PK_COLS)
        for week, yardstick in sorted(previous.items()):
            stored = _snapshot_size(conn, season=season, day=stamp, week=week)
            if stored < int(yardstick * _MIN_SNAPSHOT_FRACTION):
                # Rows collapsed onto fewer keys than they claimed. Raising inside
                # `with conn:` rolls the whole write back (the espn_ranks pattern).
                raise ProjectionCollapse(
                    f"refusing the {stamp} ESPN projections for season {season}: in "
                    f"week partition {week}, {len(rows)} offered rows collapsed onto "
                    f"{stored} stored keys vs {yardstick} previously (floor "
                    f"{int(yardstick * _MIN_SNAPSHOT_FRACTION)}). The stored snapshot "
                    "is restored by this rollback; the next run will retry."
                )
    return written


def pull_espn_projections(conn, *, league_id, season, espn_s2, swid,
                          retrieved_as_of: str, today,
                          rules: scoring.ScoringRules = scoring.HOUSE_RULES,
                          allow_shrink: bool = False,
                          allow_backfill: bool = False) -> int:
    """Live-pull ESPN projections and store them.

    ``espn_source.fetch_player_universe`` is the ONE network seam (bounded, with
    the working ESPN client fingerprint); tests patch it and no live call runs
    offline. A caller that already holds the universe should call
    :func:`ingest_espn_projections` directly rather than fetch it twice.

    ``today`` is required and BACK-stamping is REFUSED, for the reason
    ``espn_ranks.pull_espn_ranks`` spells out and which applies verbatim here:
    stamping a live pull with a PAST ``retrieved_as_of`` overwrites that past
    day's stored snapshot (perishable — ESPN serves no projection history) AND
    manufactures a retrieval-time leak, because ``get_espn_projections(as_of=<that
    past day>)`` would then serve today's numbers under the safe ``historical``
    view as if they had been knowable then. The guard lives HERE so every caller
    inherits it — item 3.1b found ``valuation --espn --as-of <past>`` bypassing
    the orchestrator's copy and destroying a stored board.

    A FUTURE stamp is allowed: it overwrites nothing (that partition cannot exist
    yet) and under-claims knowledge rather than over-claiming it.
    """
    from ziggurat.data.nfl import espn_source

    stamp = base.iso_date(retrieved_as_of)
    day = base.iso_date(today)
    if stamp < day and not allow_backfill:
        raise ValueError(
            f"refusing to store LIVE ESPN projections pulled {day} under "
            f"retrieved_as_of {stamp}: it would overwrite the stored {stamp} snapshot "
            "(which ESPN cannot re-serve) and would make today's projections readable "
            "at a past as_of under the historical view. Read the stored snapshot "
            "instead, or pass allow_backfill."
        )

    players = espn_source.fetch_player_universe(
        league_id=league_id, season=season, espn_s2=espn_s2, swid=swid
    )
    return ingest_espn_projections(conn, players, retrieved_as_of=stamp, season=season,
                                   rules=rules, allow_shrink=allow_shrink)


def get_espn_projections(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    position=None,
    espn_key=None,
    source: str | None = SOURCE,
    view: base.AsOfView = "historical",
):
    """ESPN projection rows knowable on or before ``as_of`` (keyword-only; no
    implicit now). Defaults to the safe ``historical`` view, which gates both
    ``knowable_as_of`` and ``retrieved_as_of``. Latest snapshot per
    ``(source, season, week, espn_key)``. Backtest reads go through
    ``base.latest_truth(get_espn_projections)``.

    ``week=0`` selects the WHOLE-SEASON projection (:data:`SEASON_WEEK`); a
    positive week selects that scoring period's own projection.

    ``source`` defaults to :data:`SOURCE` — the ONE feed stored today — and is
    part of the resolution key, so a second opinion added later cannot silently
    double every row of a caller that never asked for it. ``source=None`` reads
    across every stored source deliberately, and then a player can legitimately
    come back more than once (one row per source). This is NOT an ``as_of``-style
    implicit default: it narrows the read, it never widens a time gate.
    """
    clauses, params = [], {}
    if source is not None:
        clauses.append("t.source = :source")
        params["source"] = source
    if season is not None:
        clauses.append("t.season = :season")
        params["season"] = season
    if week is not None:
        clauses.append("t.week = :week")
        params["week"] = week
    if position is not None:
        clauses.append("t.position = :position")
        params["position"] = position
    if espn_key is not None:
        clauses.append("t.espn_key = :espn_key")
        params["espn_key"] = espn_key
    return base.select_as_of(
        conn, "espn_projections", as_of=as_of,
        key_cols=["source", "season", "week", "espn_key"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
