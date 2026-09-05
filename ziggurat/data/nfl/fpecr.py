"""DynastyProcess ``db_fpecr`` — the weekly FantasyPros ECR panel (migration 011).

Spike 1.2 (``intel/research/market-archives.md``) closed on this file: a single
free ~38 MB parquet that is an APPEND-ONLY archive of weekly FantasyPros expert
consensus rankings, continuous across 2021-2025, carrying ``ecr``/``best``/
``worst``/``sd`` for every fantasy-relevant slice **including K and D/ST**. It is
the only point-in-time market series this project has for seasons it did not
live through, and therefore the only external yardstick a draft backtest can be
graded against.

HOW THIS DIFFERS FROM ``adp_rankings`` (003), which also stores FantasyPros ECR:

* ``adp_rankings`` is the LIVE, PERISHABLE feed; this panel is BULK IMMUTABLE
  HISTORY: one file, re-downloadable in full, empirically never revised in
  place. A missed pull here is staleness, never loss. (CORRECTED 2026-09-04,
  item 4.2b recon §0.4: this bullet used to add "FantasyPros serves today's
  scrape only, so a missed pull is a lost observation". Measured, the file
  ``adp_rankings`` reads is rewritten FRIDAYS ONLY and consecutive daily pulls
  are identical on 0 of 517 ``ro`` rows, so a missed DAY there loses nothing —
  only a missed FRIDAY costs a scrape, and even that scrape's CONTENT is in this
  archive. It keeps the flag because nothing re-populates THAT table. The
  source where "today only" is literally true is item 4.2b's ``fp_weekly_ecr``,
  rewritten twice a day.)
* the panel keys on ``fp_page``. One ``ecr_type`` spans several distinct ranking
  PAGES, and on 215 measured occasions two of them carry THE SAME PLAYER ON THE
  SAME DAY. Loaded onto ``adp_rankings``' key ``(fantasypros_id, ecr_type,
  scrape_date, retrieved_as_of)`` those two market facts collapse onto each
  other and ``INSERT OR REPLACE`` keeps whichever the loader handed SQLite last.

  READ THIS BEFORE YOU BELIEVE THE MIGRATION HEADER. Migration
  ``011_fpecr_panel.sql``'s header states the wrong mechanism, and it is a
  committed public file, so the correction has to live somewhere a reader
  reaches. It says the collision is ``ppr-cheatsheets`` (the frozen PRESEASON
  board) against ``ros-ppr-overall`` (the live REST-OF-SEASON board), "38 such
  key groups in ``ro`` alone", and that folding them would let a preseason board
  read return a mid-season ROS ranking. **That cannot happen.** Measured on the
  pinned 2026-08-30 mirror: ``ppr-cheatsheets`` has 259 scrape dates,
  ``ros-ppr-overall`` has 101, and the two sets share ZERO — no
  ``(id, ecr_type, scrape_date)`` group can contain both. The 38 groups that do
  exist in ``ro`` are 33 x {idp-cheatsheets, ppr-cheatsheets} and
  5 x {ros-idp, ros-ppr-overall}, all dual-eligibility rows.

  THE DECISION IS STILL RIGHT; ONLY THE STATED REASON WAS WRONG. Measured after
  this ingester's position filter, over ``ro``/``rp``/``wp``: **215 same-key page
  collisions** (``ro`` 38, ``rp`` 133, ``wp`` 44) across 14 distinct player-name
  strings, every one of them a DUAL-ELIGIBLE player or a defense that FantasyPros
  publishes on two positional pages of one series on one day —
  ``ppr-rb-cheatsheets``+``ppr-wr-cheatsheets`` 59, ``ppr-rb``+``ppr-wr`` 36,
  ``ros-ppr-rb``+``ros-ppr-wr`` 36, ``idp-cheatsheets``+``ppr-cheatsheets`` 33,
  ``db-cheatsheets``+``ppr-wr-cheatsheets`` 31, and a long tail down to
  ``ppr-te``+``qb`` 1. Those are 215 genuine, distinct market facts. Drop
  ``fp_page`` from the key and they silently fold to 107-ish.
  ``test_fp_page_must_stay_in_the_primary_key`` pins that, so the claim is
  enforced rather than merely asserted; the migration header cannot be edited
  (it has been applied — see CLAUDE.md's applied-migration rule) and is handed to
  the integrator as a correction to ship in a later numbered file.
* the panel carries an INFERRED ``nfl_week``. See below.

THE WEEK IS INFERRED, AND IT IS THE SHARPEST TRAP IN THIS SOURCE.
``market-archives.md`` records that the archive carries no NFL-week integer and
warns that a silent off-by-one "would corrupt every result". The inference here
is deliberately NOT the cadence-counting the note contemplated — it is a join to
the already-ingested ``schedules`` table:

    nfl_week(scrape) = the earliest REG week whose LAST gameday is >= scrape_date

Keying on the week's LAST gameday rather than its first is the entire fix. These
are FRIDAY scrapes, and a Friday sits AFTER that week's Thursday-night opener —
so the natural-looking rule "the first week whose games are still ahead" returns
week N+1 for exactly the scrapes this archive is made of. ``week_basis`` records
which branch decided each row, so ``nfl_week = 0`` ("a preseason board") is never
confused with ``nfl_week IS NULL`` ("we could not tell").

COLLAPSE FLOOR, AND WHY ONE IS NEEDED WITHOUT A DELETE. This ingester has no
delete-then-write path (``espn_ranks``' ``BoardCollapse`` case): every row is
keyed by ``retrieved_as_of``, so a re-pull adds a version and destroys nothing on
disk. The floor exists anyway because of item 3.1b's ``players.CrosswalkCollapse``
lesson: ``base.select_as_of`` resolves the NEWEST retrieved version per key, so a
degraded pull does not need to delete anything to make the good data unreadable —
it merely has to be newer. A truncated or half-published parquet would therefore
shadow a complete stored panel at read time while the run logged ``ok``.
:class:`PanelCollapse` is checked per season BEFORE anything is written.
"""

import contextlib
import glob
import logging
import os
import time
import urllib.request
from collections.abc import Iterable

from ziggurat import net
from ziggurat.data.asof import nfl_season_of, normalize_as_of
from ziggurat.data.nfl import base

#: The upstream mirror. Free, no auth, one file. Pin/mirror a local copy for
#: durable provenance — upstream's git history is truncated (~Dec 2024 back), so
#: the parquet is the only artifact and it is not versioned anywhere we control.
FPECR_URL = "https://github.com/dynastyprocess/data/raw/master/files/db_fpecr.parquet"

logger = logging.getLogger("ziggurat.data.nfl.fpecr")

#: WALL-CLOCK BUDGET for the whole ~38 MB download (item 4.1 audit, OPS-3).
#: ``net.HTTP_TIMEOUT`` bounds each socket READ, not the transfer: an upstream
#: that keeps trickling bytes just under the timeout holds the daily unit for
#: as long as it likes, and the three PERISHABLE sources behind this one in the
#: registry lose their observation for the day. Measured 2026-09-01: the full
#: download completes in ~11.6 s including the ingest; 600 s is ~50x that and
#: still well inside the unit's wall-clock cap. Exceeding it raises
#: ``TimeoutError`` naming the elapsed seconds and bytes, and the ``.part`` is
#: removed.
FETCH_BUDGET_S = 600.0

#: Positions this league starts. Everything else (DB/DL/LB/EDGE IDP) is dropped
#: at ingest, exactly as ``adp_rankings`` does and for the same reason: an IDP
#: can rank top-5 on an "overall" page, and a derived rank that counts him is a
#: rank nobody in this league can act on.
LEAGUE_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DST"})

#: Ranking series worth storing. The archive also ships dynasty (``do``/``dp``/
#: ``dsf``/``dr``/``drk``), best-ball (``bo``/``bp``), superflex (``rsf``/``wsf``)
#: and rookie boards; this league is a single-QB full-PPR REDRAFT league, so
#: those describe a different market and are dropped rather than stored as noise
#: a later reader could pick up by mistake.
#:   ro — redraft overall (the draft board, and the rest-of-season overall board)
#:   rp — redraft positional (per-position cheatsheets + rest-of-season)
#:   wp — weekly positional (the in-season series spike 1.2 scoped Phase 4 on)
DEFAULT_ECR_TYPES = ("ro", "rp", "wp")

#: The normalized page name of the PRESEASON full-PPR overall draft board. This
#: is the one page a draft backtest may read: ``ros-ppr-overall`` carries the
#: same ``ecr_type`` and is a REST-OF-SEASON ranking, which is a different fact.
PRESEASON_BOARD_PAGE = "ppr-cheatsheets"
PRESEASON_BOARD_ECR_TYPE = "ro"

#: Source columns required. A release that drops one fails loudly rather than
#: storing partial rows (the item-1.4 contract).
_REQUIRED = (
    "id", "player", "pos", "team", "ecr", "sd", "best", "worst",
    "player_owned_avg", "player_owned_espn", "ecr_type", "fp_page", "scrape_date",
)

#: db_column -> source_column for straight-through fields. Derived columns
#: (season/nfl_week/week_basis/gsis_id/espn_id/page_rank/pos_rank) are added
#: per-row after the frame is read.
_COLMAP = {
    "fantasypros_id": "id",
    "player": "player",
    "position": "pos",
    "team": "team",
    "ecr_type": "ecr_type",
    "fp_page": "fp_page",
    "ecr": "ecr",
    "sd": "sd",
    "best": "best",
    "worst": "worst",
    "player_owned_avg": "player_owned_avg",
    "player_owned_espn": "player_owned_espn",
    "scrape_date": "scrape_date",
}

#: The stored PRIMARY KEY (migration 011). Passed to ``base.upsert`` so its
#: return value is DISTINCT KEYS WRITTEN, and so ``base.upsert`` validates this
#: tuple against the key SQLite actually enforces on every ingest.
_PK_COLS = ("fantasypros_id", "ecr_type", "fp_page", "scrape_date", "retrieved_as_of")

#: A pull carrying fewer than this fraction of a season's already-stored keys is
#: refused. Generous on purpose: upstream occasionally prunes deep tails, and a
#: guard that cries wolf is how the report that matters gets ignored (the same
#: reasoning that kept the league sync's gap report out of ``refresh``).
_MIN_PANEL_FRACTION = 0.75

#: Upstream position spellings normalized to this project's vocabulary. ``PK``
#: appears on pre-2021 kicker pages; ``DEF``/``D/ST`` are defensive-unit
#: variants. (Measured: 2021+ ships none of these, but the archive reaches back
#: to 2019 and the ingester takes a season range.)
_POSITION_ALIASES = {"PK": "K", "DEF": "DST", "D/ST": "DST", "DEFENSE": "DST"}


class PanelCollapse(RuntimeError):
    """A degraded upstream file would have SHADOWED the stored panel.

    Not a delete guard — this table has no delete path. ``base.select_as_of``
    resolves the newest ``retrieved_as_of`` per key, so a truncated parquet that
    merely arrives later makes the complete stored panel unreadable while every
    row of it is still on disk. See the module docstring.
    """


class PanelRankDiscontinuity(ValueError):
    """A derived ``page_rank``/``pos_rank`` board has a hole in it.

    Fires only on a code defect: after ``_dedupe_on_key`` every ranked row is a
    distinct primary key and every distinct key is stored, so the property holds
    by construction. Raising is cheap here in a way it is not for
    ``adp_rankings`` (whose identical check trades against a perishable — though,
    corrected 2026-09-04, WEEKLY rather than daily — scrape): this source is a
    whole file, re-downloadable in full, so a refused run costs nothing but the
    download.
    """


# ------------------------------------------------------------------ helpers


def _normalize_page(value) -> str | None:
    """Upstream ships two spellings of the same page. Fold them.

    ``/nfl/rankings/ppr-cheatsheets.php`` and bare ``ppr-cheatsheets`` are the
    same ranking from two eras of the scraper, and both occur inside the target
    window. Unfolded they are two different values of a PRIMARY KEY column, which
    would split one series in half and let a "latest scrape" read silently pick
    the older spelling's date.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("/nfl/rankings/"):
        text = text[len("/nfl/rankings/"):]
    if text.endswith(".php"):
        text = text[: -len(".php")]
    return text


def _coerce_fp_id(value):
    """FantasyPros id -> the bare digit string, matching ``players.fantasypros_id``.

    Copied in behaviour (not imported) from ``adp_rankings._coerce_fp_id``: the
    archive stores the id as a STRING while the live ``import_ff_rankings`` frame
    stores it as an int64, so both float-ish and clean-string forms must land on
    the same key or the crosswalk join silently misses.
    """
    if value is None:
        return None
    if isinstance(value, float):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def week_bounds(conn, season: int) -> dict[int, tuple[str, str]]:
    """``week -> (first gameday, last gameday)`` for one season's REG schedule.

    Read straight off ``schedules`` with no as-of gate, and that is deliberate:
    an NFL calendar is not a decision input, it is the CLOCK the leakage gate is
    stated against. Gating it would mean a scrape could not be told which week it
    preceded until the games had been played, which is backwards.
    """
    rows = conn.execute(
        "SELECT week, MIN(gameday) AS first, MAX(gameday) AS last FROM schedules "
        "WHERE season = ? AND game_type = 'REG' AND gameday IS NOT NULL "
        "GROUP BY week ORDER BY week",
        (season,),
    ).fetchall()
    return {int(r["week"]): (r["first"], r["last"]) for r in rows}


def infer_nfl_week(scrape_date: str, bounds: dict[int, tuple[str, str]]) -> tuple[int | None, str]:
    """``(nfl_week, week_basis)`` for one scrape day. See the module docstring.

    * no schedule ingested for the season -> ``(None, "no_schedule")``
    * before week 1's FIRST gameday      -> ``(0, "schedules")``  (a preseason board)
    * otherwise the earliest week whose LAST gameday is >= the scrape
                                          -> ``(week, "schedules")``
    * later than the last REG gameday    -> ``(None, "after_season")``

    The last-gameday rule, restated because it is the off-by-one: these are
    Friday scrapes. Week N's first gameday is its Thursday opener, which a Friday
    scrape is already PAST, so "the first week that has not started" answers
    N + 1. Week N's last gameday is the Monday night game, which a Friday scrape
    is before, so "the first week that has not finished" answers N.
    """
    if not bounds:
        return None, "no_schedule"
    weeks = sorted(bounds)
    if scrape_date < bounds[weeks[0]][0]:
        return 0, "schedules"
    for week in weeks:
        if scrape_date <= bounds[week][1]:
            return week, "schedules"
    return None, "after_season"


def _survivor_rank(row) -> tuple:
    """Sort key deciding WHICH of several rows on one primary key survives.

    Same rule, and the same reasoning, as ``adp_rankings._survivor_rank``: keep
    the row reporting the WIDER expert dispersion, because ``sd``/``best``/
    ``worst`` describe how far apart the panel is and a partial aggregation
    UNDER-states that — and under-stated uncertainty is the confident-sounding
    number Rule 6 exists to prevent. The last element is a canonical rendering of
    the whole row, so the order is TOTAL and the winner never depends on the
    order upstream happened to ship the rows in.

    Measured on the shipped archive: for 2021+ there are ZERO collisions on this
    table's key (555,327 rows, 555,327 distinct keys) — every duplicate group in
    the file predates 2021 and every one of those is byte-identical. So this rule
    decides nothing today. It is here because the ingester takes a season range
    that reaches back to 2019, and because a rule that only appears once it is
    needed is a rule nobody reviewed.
    """
    sd = row.get("sd")
    best, worst = row.get("best"), row.get("worst")
    spread = (worst - best) if (best is not None and worst is not None) else None
    ecr = row.get("ecr")
    return (
        0 if sd is not None else 1,
        -(sd if sd is not None else 0.0),
        -(spread if spread is not None else 0.0),
        ecr if ecr is not None else float("inf"),
        repr(sorted((k, repr(v)) for k, v in row.items())),
    )


def _dedupe_on_key(rows: list[dict]) -> list[dict]:
    """Fold rows sharing this table's PRIMARY KEY down to one, BEFORE ranking.

    ``INSERT OR REPLACE`` would fold them anyway — silently, after the ranking
    had already numbered the doomed row, which is how ``adp_rankings`` shipped a
    published board with no rank 64. Folding first means the ranking numbers
    exactly the rows that will exist. Rows with a NULL in any key column pass
    through untouched (SQLite's PK index treats every NULL as distinct and stores
    them all). Relative order is preserved: a group's survivor is emitted where
    its first member was.
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(row[c] for c in _PK_COLS)
        if any(v is None for v in key):
            continue
        groups.setdefault(key, []).append(row)

    winners = {key: min(group, key=_survivor_rank) for key, group in groups.items()}
    collapsed = duplicated = 0
    for key, group in groups.items():
        for row in group:
            if row is winners[key]:
                continue
            if dict(row) == dict(winners[key]):
                duplicated += 1
            else:
                collapsed += 1
    base.note_collapsed("fpecr_panel", collapsed, duplicated, len(rows))

    kept: list[dict] = []
    emitted: set[tuple] = set()
    for row in rows:
        key = tuple(row[c] for c in _PK_COLS)
        if any(v is None for v in key):
            kept.append(row)
            continue
        if key in emitted:
            continue
        emitted.add(key)
        kept.append(winners[key])
    return kept


def _assign_ranks(rows: list[dict]) -> None:
    """Derive ``page_rank`` and ``pos_rank`` in place, over LEAGUE positions only.

    ``page_rank`` numbers 1..n by ``ecr`` within (ecr_type, fp_page, scrape_date);
    on the preseason overall cheatsheet that IS the draft board rank the room
    would see. ``pos_rank`` does the same within each position on that page. A
    None ``ecr`` sorts last, so a ranked player always precedes an unranked one.
    Ties break on ``fantasypros_id`` so the numbering is a pure function of the
    data rather than of upstream's row order.
    """
    def order(group: list[dict]) -> None:
        group.sort(
            key=lambda r: (
                r["ecr"] is None,
                r["ecr"] if r["ecr"] is not None else 0.0,
                str(r["fantasypros_id"]),
            )
        )

    pages: dict[tuple, list[dict]] = {}
    for row in rows:
        pages.setdefault((row["ecr_type"], row["fp_page"], row["scrape_date"]), []).append(row)
    for group in pages.values():
        order(group)
        for i, row in enumerate(group, start=1):
            row["page_rank"] = i
        by_pos: dict[str, list[dict]] = {}
        for row in group:
            by_pos.setdefault(row["position"], []).append(row)
        for pos_group in by_pos.values():
            order(pos_group)
            for i, row in enumerate(pos_group, start=1):
                row["pos_rank"] = i


def _check_ranks_contiguous(rows) -> None:
    """Post-condition: every page is 1..n and every (page, position) is 1..m.

    A hole shifts every player below it by one, and a rank is the number a
    consumer differences against another board — so it reads perfectly normal
    while being wrong. See :class:`PanelRankDiscontinuity` for why this raises.
    """
    pages: dict[tuple, list[int]] = {}
    positions: dict[tuple, list[int]] = {}
    for row in rows:
        page_key = (row["ecr_type"], row["fp_page"], row["scrape_date"])
        pages.setdefault(page_key, []).append(row["page_rank"])
        positions.setdefault(page_key + (row["position"],), []).append(row["pos_rank"])
    for label, boards in (("page_rank", pages), ("pos_rank", positions)):
        for key, ranks in boards.items():
            expected = list(range(1, len(ranks) + 1))
            if sorted(ranks) != expected:
                missing = sorted(set(expected) - set(ranks))
                raise PanelRankDiscontinuity(
                    f"fpecr_panel: {label} board {key} holds {len(ranks)} rows but its "
                    f"ranks are not 1..{len(ranks)} (missing {missing}) — a board with a "
                    "hole in it shifts every player below the hole by one; refusing to "
                    "store it"
                )


def _stored_key_counts(
    conn, seasons: Iterable[int], ecr_types: Iterable[str] | None = None
) -> dict[int, int]:
    """Distinct stored keys per season, across every ``retrieved_as_of``.

    SCOPED BY ``ecr_types``, and that scoping is the whole point of the argument.
    The public API lets a caller narrow a re-pull to one series
    (``pull_fpecr(..., ecr_types=("ro",))``), so an unscoped count compares one
    series against all three and declares a perfectly good file truncated —
    with a remedy ("re-download and retry") that can never clear, because the
    file is fine and the narrowing is the caller's. ``None`` means the caller
    asked for every series, so the count is unscoped too.
    """
    types = None if ecr_types is None else sorted({str(t) for t in ecr_types})
    out: dict[int, int] = {}
    for season in seasons:
        sql = ("SELECT COUNT(*) FROM (SELECT DISTINCT fantasypros_id, ecr_type, fp_page, "
               "scrape_date FROM fpecr_panel WHERE season = ?")
        params: list = [season]
        if types is not None:
            sql += " AND ecr_type IN (" + ",".join("?" * len(types)) + ")"
            params.extend(types)
        row = conn.execute(sql + ")", params).fetchone()
        out[int(season)] = int(row[0])
    return out


def _check_panel_size(conn, rows, *, seasons=None, ecr_types=None) -> None:
    """Refuse a pull that would shadow a materially larger stored panel.

    COMPARED LIKE FOR LIKE, and getting that wrong is a real failure mode rather
    than a theoretical one. Three scopings have to agree or the floor either
    cries wolf or sleeps:

    * PER SEASON, because the ingester takes a season range: a run scoped to 2021
      must not be measured against a database holding 2021-2026.
    * PER SERIES, because ``ecr_types`` is a public argument. Measured before this
      was scoped: feeding the stored season-2021 ``ro`` rows straight back in
      (28,315 of 71,757 keys — the rest being ``rp`` and ``wp``) raised
      ``PanelCollapse`` and told the operator to re-download a file that was
      never damaged. The narrowed re-pull the API offers could not succeed.
    * IN DISTINCT KEYS on both sides — the stored side is post-dedup, so a raw
      row count would compare two different quantities.

    A season the CALLER ASKED FOR that arrives with no rows at all is checked
    too: without that, a file that stopped shipping 2022 entirely would pass
    silently, because there would be no incoming bucket to compare. When
    ``seasons is None`` the caller asked for whatever the frame holds, so only
    the seasons present can be checked and that is stated rather than hidden.
    """
    incoming: dict[int, set[tuple]] = {}
    for row in rows:
        incoming.setdefault(int(row["season"]), set()).add(
            (row["fantasypros_id"], row["ecr_type"], row["fp_page"], row["scrape_date"])
        )
    requested = set() if seasons is None else {int(s) for s in seasons}
    checked = sorted(set(incoming) | (requested & set(_stored_seasons(conn))))
    stored = _stored_key_counts(conn, checked, ecr_types)
    scope = "every series" if ecr_types is None else "series " + "/".join(
        sorted({str(t) for t in ecr_types})
    )
    for season in checked:
        keys = incoming.get(season, set())
        previous = stored.get(season, 0)
        if previous == 0:
            continue
        floor = int(previous * _MIN_PANEL_FRACTION)
        if len(keys) < floor:
            raise PanelCollapse(
                f"fpecr_panel: the incoming panel carries {len(keys)} distinct keys for "
                f"season {season} ({scope}) but {previous} are already stored for the "
                f"same scope (floor {floor} = {_MIN_PANEL_FRACTION:.0%}). A truncated or "
                "half-published parquet does not have to delete anything to hide the "
                "good rows — select_as_of resolves the NEWEST retrieved version per key, "
                "so merely arriving later is enough. Refusing to write. Check the "
                "download first; if you deliberately narrowed this run, narrow "
                "`ecr_types`/`seasons` to match what you are re-pulling."
            )


def _stored_seasons(conn) -> list[int]:
    return [
        int(r[0]) for r in conn.execute("SELECT DISTINCT season FROM fpecr_panel")
    ]


# ------------------------------------------------------------------ ingest


def ingest_fpecr(
    conn,
    df,
    *,
    retrieved_as_of: str,
    seasons: Iterable[int] | None = None,
    ecr_types: Iterable[str] | None = DEFAULT_ECR_TYPES,
) -> int:
    """Persist the ECR panel, stamping ``knowable_as_of`` with the scrape date.

    ``seasons`` (``None`` = every season in the frame) and ``ecr_types``
    (``None`` = every series) narrow what is stored; both are applied BEFORE the
    per-row work, because the shipped file is 1.8M rows and this league cares
    about roughly a fifth of them.

    Drops IDP rows (not startable here) and non-redraft series; resolves
    gsis_id/espn_id through the FantasyPros crosswalk (DST and unresolved keep a
    NULL gsis_id and are KEPT); normalizes team through ``base.TEAM_ALIASES``;
    infers ``nfl_week`` from ``schedules``; derives ``page_rank``/``pos_rank``.

    The write is ONE transaction: a failure part-way rolls the whole run back
    rather than leaving a half-written season that a later read would resolve
    against (item 3.1b — "a failed source's partial rows must not ride the next
    source's commit").
    """
    base.require_columns(df, _REQUIRED, source="fpecr_panel")

    # Materialize both iterables ONCE. They are read in three places (the frame
    # filter, the per-row season filter, and the collapse floor's scoping), and a
    # caller passing a generator would find it exhausted after the first.
    ecr_types = None if ecr_types is None else tuple(str(t) for t in ecr_types)
    seasons = None if seasons is None else tuple(int(s) for s in seasons)

    frame = df
    if ecr_types is not None:
        wanted_types = set(ecr_types)
        frame = frame[frame["ecr_type"].isin(wanted_types)]
    # Narrow to the mapped columns before the row build: ``frame_to_rows``
    # iterates row-wise, and on a 1.8M x 24 frame the columns nobody stores cost
    # more than every other step in this function combined.
    frame = frame[list(dict.fromkeys(_COLMAP.values()))]

    rows = base.frame_to_rows(
        frame,
        _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=lambda src: base.iso_date(src.get("scrape_date")),
    )

    wanted_seasons = None if seasons is None else {int(s) for s in seasons}
    bounds_cache: dict[int, dict[int, tuple[str, str]]] = {}
    crosswalk = base.ids_by_fantasypros(conn)

    kept: list[dict] = []
    unstampable = 0
    for row in rows:
        scrape = row["scrape_date"]
        if scrape is None:
            # No knowledge time. Reported on the DROP channel, not the by-design
            # filter channel: a row we cannot stamp is a failure to handle the
            # data, and `refresh.run_ingest`'s drop ceiling excludes `filtered`.
            unstampable += 1
            continue
        row["scrape_date"] = scrape = base.iso_date(scrape)
        season = nfl_season_of(scrape)
        if wanted_seasons is not None and season not in wanted_seasons:
            continue
        position = str(row["position"] or "").strip().upper()
        position = _POSITION_ALIASES.get(position, position)
        if position not in LEAGUE_POSITIONS:
            continue
        page = _normalize_page(row["fp_page"])
        if page is None:
            continue
        row["position"] = position
        row["fp_page"] = page
        row["season"] = season
        if season not in bounds_cache:
            bounds_cache[season] = week_bounds(conn, season)
        week, basis = infer_nfl_week(scrape, bounds_cache[season])
        row["nfl_week"] = week
        row["week_basis"] = basis
        row["fantasypros_id"] = _coerce_fp_id(row["fantasypros_id"])
        if row["team"] is not None:
            team = str(row["team"]).strip().upper()
            row["team"] = base.TEAM_ALIASES.get(team, team)
        gsis, espn = crosswalk.get(row["fantasypros_id"], (None, None))
        row["gsis_id"] = gsis
        row["espn_id"] = espn
        kept.append(row)

    # ONE note_drops call, not two (item 3.2c finding F-H): ``base.collect_drops``
    # SUMS ``total`` across calls, so a second call reports a denominator larger
    # than the number of rows that ever existed. The genuinely-undroppable class
    # is handled by raising instead, immediately below.
    filtered = len(rows) - len(kept) - unstampable
    base.note_drops(
        "fpecr_panel", filtered, len(rows),
        why=("IDP or unknown position, off-series ecr_type, or out-of-range season "
             "(measured on the 2026-08-30 archive, IN THE ro/rp/wp SLICE THIS "
             "INGESTER SEES: exactly 1 row carries a NULL pos and 0 carry a blank "
             "one — a positionless row is not draftable in any league. The whole "
             "archive holds 34 NULL-pos rows, but 33 of them are dynasty series "
             "(`do` 28, `dsf` 5) that DEFAULT_ECR_TYPES drops before the position "
             "filter ever runs, so 34 is the wrong denominator for this message)"),
        by_design=True,
    )
    if unstampable:
        # NOT a by-design filter and not a silent drop. ``scrape_date`` is
        # upstream's own immutable snapshot key — it is what the whole archive is
        # partitioned on — so a row without one is a schema break, not a data
        # quirk, and a row with no knowledge time cannot be stored under Rule 1.
        # Refusing is cheap here for the same reason `PanelRankDiscontinuity`
        # refuses: this source is one file, re-downloadable in full, so a refused
        # run costs a download rather than a lost observation.
        raise PanelCollapse(
            f"fpecr_panel: {unstampable} of {len(rows)} rows carry no scrape_date, so "
            "they have no knowledge time to stamp. That column is the archive's own "
            "snapshot key; its absence is upstream schema drift. Refusing the run."
        )

    if not kept:
        # "wrote 0 rows" is never ok (item 3.1b). An empty result here means the
        # filters matched nothing, which for a 1.8M-row file is a caller error or
        # a schema change, not a legitimately empty upstream.
        raise PanelCollapse(
            "fpecr_panel: no rows survived the season/series/position filters. The "
            f"frame carried {len(rows)} rows; check `seasons`/`ecr_types` and that "
            "the archive still ships the pages this league reads."
        )

    kept = _dedupe_on_key(kept)

    unresolved = sum(
        1 for row in kept if row["gsis_id"] is None and row["position"] != "DST"
    )
    base.note_incomplete(
        "fpecr_panel", unresolved, len(kept),
        why="unresolved FantasyPros crosswalk id (kept, NULL gsis_id)",
    )
    no_week = sum(1 for row in kept if row["week_basis"] == "no_schedule")
    base.note_incomplete(
        "fpecr_panel", no_week, len(kept),
        why="season's REG schedule not ingested, so nfl_week is NULL (kept)",
    )

    _assign_ranks(kept)
    _check_ranks_contiguous(kept)
    _check_panel_size(conn, kept, seasons=wanted_seasons, ecr_types=ecr_types)

    with conn:
        return base.upsert(conn, "fpecr_panel", kept, key_cols=_PK_COLS, commit=False)


def fetch_fpecr(dest, *, budget_s: float = FETCH_BUDGET_S) -> str:
    """Download the archive to ``dest`` and return the path. THE network seam.

    Bounded twice (item 3.1b, item 4.1 audit OPS-3): ``net.HTTP_TIMEOUT`` on
    every socket read, and ``budget_s`` on the WHOLE transfer measured with
    ``time.monotonic`` per chunk — a per-read timeout alone cannot bound a
    trickling upstream. Written to a ``.part`` file and renamed on success, so
    a truncated download can never be mistaken for a mirror — the parquet
    reader would otherwise fail on a half file, or worse, succeed on one.

    THE ``.part`` IS REMOVED ON ANY FAILURE the process can see (a read error,
    the budget, a ``KeyboardInterrupt``): a leftover partial is not merely
    litter, it is a 38 MB file whose name says what it nearly was, and the
    next successful run would silently rename over it. What this cannot cover
    is ``SIGKILL`` (or ``SIGTERM`` with no handler — systemd's stop signal),
    which no ``finally`` runs under; :func:`pull_fpecr` sweeps stale
    ``*.parquet.part`` siblings of ``path`` at the start of the next run for
    exactly that case, logging each removal.

    Tests patch THIS function (or ``urllib.request.urlopen`` on this module);
    nothing offline touches the network.
    """
    dest = str(dest)
    part = f"{dest}.part"
    request = urllib.request.Request(FPECR_URL, headers={"User-Agent": "ziggurat/4.1"})
    started = time.monotonic()
    received = 0
    try:
        with urllib.request.urlopen(request, timeout=net.HTTP_TIMEOUT) as response:  # noqa: S310
            with open(part, "wb") as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    elapsed = time.monotonic() - started
                    if elapsed > budget_s:
                        raise TimeoutError(
                            f"fpecr: download of {FPECR_URL} exceeded its {budget_s:.0f} s "
                            f"budget ({elapsed:.0f} s elapsed, {received} bytes received) — "
                            "a trickling upstream; the partial file is removed and the "
                            "perishable sources behind this one in the registry still run"
                        )
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(part)
        raise
    os.replace(part, dest)
    return dest


def sweep_stale_parts(path, *, older_than_s: float = FETCH_BUDGET_S, now=None) -> list[str]:
    """Remove ``*.part`` leftovers beside ``path`` from a run that was KILLED.

    ``fetch_fpecr`` cleans up after every failure it can see; a ``SIGKILL`` or
    an unhandled ``SIGTERM`` (systemd's stop) runs no ``finally``, so the
    partial of a dated mirror survives under a name that says what it nearly
    was. Nothing ever reads a ``.part`` — the mirror is looked up by its final
    name — so removing it loses nothing; each removal is logged so a recurring
    kill is visible in the journal. Only a partial whose last write is older
    than ``older_than_s`` is touched: a download IN PROGRESS in another process
    (an attended backfill beside the timer) rewrites its file every chunk, and
    a partial nobody has written to for longer than the whole budget is not one
    anybody is still writing. Returns the paths removed.
    """
    directory = os.path.dirname(str(path)) or "."
    now = time.time() if now is None else now
    removed: list[str] = []
    for stale in sorted(glob.glob(os.path.join(directory, "*.parquet.part"))):
        try:
            age = now - os.stat(stale).st_mtime
        except FileNotFoundError:
            continue
        if age <= older_than_s:
            continue
        with contextlib.suppress(FileNotFoundError):
            os.unlink(stale)
            removed.append(stale)
            logger.warning(
                "fpecr: removed stale partial download %s (last written %.0f s ago — a "
                "previous run was killed mid-transfer; nothing reads a .part, so "
                "nothing is lost)", stale, age,
            )
    return removed


def read_fpecr(path):
    """Read a mirrored parquet into a pandas frame (the shape ingesters take).

    ``polars`` is already a dependency (it is what ``nflreadpy`` returns), and it
    reads this file in ~1 s against pandas/pyarrow's several; the frame is
    converted at one seam exactly like ``ziggurat/data/nfl/source.py`` does.
    """
    import polars as pl

    return pl.read_parquet(str(path)).to_pandas()


def pull_fpecr(
    conn,
    *,
    retrieved_as_of: str,
    path,
    seasons: Iterable[int] | None = None,
    ecr_types: Iterable[str] | None = DEFAULT_ECR_TYPES,
    refresh: bool = False,
) -> int:
    """Mirror the archive to ``path`` (unless already there) and ingest it.

    ``path`` is REQUIRED and has no default: the mirror is league-private bulk
    data that belongs under the gitignored top-level ``data/`` tree (Rule 5), and
    a module-level default path is how a file ends up written somewhere nobody
    expected. ``refresh=True`` re-downloads over an existing mirror.
    """
    sweep_stale_parts(path)
    if refresh or not os.path.exists(str(path)):
        fetch_fpecr(path)
    return ingest_fpecr(
        conn, read_fpecr(path),
        retrieved_as_of=retrieved_as_of, seasons=seasons, ecr_types=ecr_types,
    )


# ------------------------------------------------------------------ read


def get_fpecr(
    conn,
    *,
    as_of,
    season=None,
    ecr_type=None,
    fp_page=None,
    position=None,
    nfl_week=None,
    scrape_date=None,
    view: base.AsOfView = "historical",
):
    """Panel rows knowable on or before ``as_of`` (keyword-only; no implicit now).

    Every filter is optional and ANDed. ``fp_page`` takes the NORMALIZED name
    (``"ppr-cheatsheets"``, not the ``.php`` URL) — the ingester folds upstream's
    two spellings, so the stored value is always the bare form.

    THE VIEW MATTERS MORE HERE THAN ALMOST ANYWHERE. This table is bulk immutable
    history: every row of the 2021-2025 panel carries a ``retrieved_as_of`` of
    whatever day the parquet was mirrored. Under the safe-default ``historical``
    view a read at any past ``as_of`` therefore returns NOTHING, silently — the
    footgun ``base.latest_truth`` exists for. Backtest and grading code reads
    through ``base.latest_truth(get_fpecr)``, which binds the view so it cannot be
    forgotten; fact-time protection (``knowable_as_of <= as_of``) is unchanged
    either way, so a preseason board read at a preseason ``as_of`` still cannot
    see a mid-season scrape.
    """
    clauses, params = [], {}
    for column, value in (
        ("season", season),
        ("ecr_type", ecr_type),
        ("fp_page", fp_page),
        ("position", position),
        ("nfl_week", nfl_week),
        ("scrape_date", scrape_date),
    ):
        if value is not None:
            clauses.append(f"t.{column} = :{column}")
            params[column] = value
    return base.select_as_of(
        conn, "fpecr_panel", as_of=as_of,
        key_cols=["fantasypros_id", "ecr_type", "fp_page", "scrape_date"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


def latest_scrape_date(
    conn,
    *,
    as_of,
    season,
    ecr_type=PRESEASON_BOARD_ECR_TYPE,
    fp_page=PRESEASON_BOARD_PAGE,
    before=None,
    view: base.AsOfView = "historical",
) -> str | None:
    """The most recent stored ``scrape_date`` for one series, or ``None``.

    ``before`` (exclusive, an ISO date) is the draft-board question: "the last
    consensus board published before week 1 kicked off". It is a SEPARATE
    parameter from ``as_of`` on purpose — ``as_of`` is the leakage gate on what
    this system knew, ``before`` is a fact about the football calendar. Folding
    them into one argument is how a caller ends up believing a knowledge gate is
    doing calendar work, or the reverse.

    Goes through :func:`get_fpecr`, not raw SQL, so the gate cannot be skipped
    here and then be missing from a leakage test.
    """
    cutoff = None if before is None else normalize_as_of(before).isoformat()
    dates = {
        row["scrape_date"]
        for row in get_fpecr(
            conn, as_of=as_of, season=season, ecr_type=ecr_type, fp_page=fp_page, view=view,
        )
        if row["scrape_date"] is not None and (cutoff is None or row["scrape_date"] < cutoff)
    }
    return max(dates) if dates else None
