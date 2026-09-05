"""Same-week FantasyPros WEEKLY ECR board (migration 016) — item 4.2b, Unit F.

**Capture only.** Nothing reads this table in Week 1: no waiver column, no
candidate signal, no briefing line. ``tests/test_nfl_fp_weekly.py`` asserts that
no module under ``ziggurat/core/`` imports it. The reason to start capturing NOW
is that the observation is perishable and the season has begun.

WHAT IT IS, AND THE QUESTION IT ANSWERS. Item 4.2b's recon question was whether
the same-week ``wp`` (weekly positional) consensus can be captured on a live
Monday/Tuesday/Wednesday. It can, and without scraping anything: DynastyProcess
publishes ``files/fp_latest_weekly.csv`` **twice daily** in-season (measured
2026-09-04: 06:54Z and 17:22Z, the same rhythm every in-season day of 2025), and
it is the same ``wp`` series items 4.1 and 4.2 grade against. Before this table,
every market number this project held for a live Tuesday was either a
weekly-FRIDAY draft/rest-of-season board (``adp_rankings``) or a bulk archive
that stops at 2025 (``fpecr_panel``: season-2026 ``wp`` rows = 0, next scheduled
pull ~2026-09-30).

IT IS PERISHABLE, AND IT MOVES WITHIN THE DAY. Measured 2026-09-04: **81 of 159**
``ppr-rb`` ids changed integer rank in the 5.7 hours between the 17:22Z CSV and
the 23:06Z FantasyPros page. What this file said on a given Tuesday exists
nowhere once it is rewritten, so a missed pull is a LOST OBSERVATION, not
staleness — the ``perishable=True`` class, with ``retrieved_as_of`` in the
primary key so a second capture VERSIONS rather than replaces.

**NEVER MERGE THIS INTO ``fpecr_panel``.** That panel is a backtest input with a
verified immutability story (spike 1.2: zero in-place revisions across 1,261,623
shared rows). ``base.select_as_of`` resolves the newest ``retrieved_as_of`` per
key, so a live number sharing that key space would silently answer every
backtest read with today's board. Two tables, two immutability stories.

THE WEEK LABEL IS THIS SOURCE'S SHARPEST TRAP, AND IT IS NOT ``fpecr``'S RULE.
``fpecr.infer_nfl_week`` was written for FRIDAY archive scrapes and short-
circuits to week 0 ("a preseason board") for any scrape before week 1's opener.
Measured 2026-09-04: it answers **week 0** for that day's capture while the live
page ranks **week 1**. Filing a real weekly board under a week that does not
exist is worse than not labelling it, so the week here is derived independently
by :func:`infer_weekly_board_week` and ``week_basis`` records WHICH AUTHORITY
decided it — ``'schedules'``, ``'fantasypros_page'`` (opt-in, DEFAULT OFF), or
``'unknown'`` with a NULL week. See :func:`infer_weekly_board_week` for the rule
and its one known soft day.

RULE 2. ``r2p_pts`` and ``start_sit_grade`` are FantasyPros' OWN projected points
and their own start/sit letter grade, in THEIR scoring. They are stored because
they are what the market was saying — never as a points input. House points come
from ``ziggurat/core/scoring.py`` and nowhere else.

RULE 5 / ToS. FantasyPros' terms permit "a single copy made for personal use
only": the capture is local, and **nothing captured is ever committed**. The
committed fixture under ``tests/fixtures/nfl/`` is a trimmed copy of the public
NFL board — player names only, no league-private data.

WHAT IS NOT STORED, AND WHY THERE IS NO RAW MIRROR. ``ff_opportunity`` keeps a
lossless parquet mirror of every capture because it stores 25 of 159 columns and
a perishable vintage cannot be re-fetched. Here 20 of upstream's 28 columns are
stored and the eight that are not — ``page_pos`` (byte-equal to ``pos``),
``player_positions``, ``player_short_name``, ``player_eligibility``,
``player_page_url``, ``player_filename``, ``player_bye_week`` — are static player
metadata or derivable from ``schedules``, not point-in-time market facts. Every
number that MOVES (``rank``/``ecr``/``sd``/``best``/``worst``/
``player_owned_avg``/``player_ecr_delta``/``start_sit_grade``/``r2p_pts``) is
stored. A 387 KB daily mirror (~66 MB a season) would buy back only the static
half, so it is deliberately not kept — recorded here rather than left implicit,
because the choice is irreversible in the same way ``ff_opportunity``'s was.
"""

import io
import json
import logging
import os
import re
import time
import urllib.request

from ziggurat import net
from ziggurat.data.asof import nfl_season_of
from ziggurat.data.nfl import base

logger = logging.getLogger("ziggurat.data.nfl.fp_weekly")

#: The upstream mirror. Free, no auth, one small CSV, rewritten in place twice a
#: day in-season. Fetched BY URL rather than through ``nflreadpy.load_ff_rankings
#: (type="week")``, which maps to this same file: that client caches (default
#: ``CacheMode.MEMORY``, 86,400 s) and bounds only each socket read, while a
#: perishable capture wants a fresh transfer under a WHOLE-transfer budget. The
#: seam is the ``fpecr.fetch_fpecr`` one, pointed at a 387 KB file.
FP_WEEKLY_URL = (
    "https://github.com/dynastyprocess/data/raw/master/files/fp_latest_weekly.csv"
)

#: WALL-CLOCK BUDGET for the whole transfer (the ``fpecr`` precedent, item 4.1
#: audit OPS-3). ``net.HTTP_TIMEOUT`` bounds each socket READ, not the transfer:
#: an upstream that trickles bytes just under the timeout holds the daily unit
#: for as long as it likes, and the perishable sources behind this one in the
#: registry lose their observation for the day. The file is ~387 KB and lands in
#: well under a second.
FETCH_BUDGET_S = 120.0

#: Refuse a response that is not the shape of the small CSV this reads, rather
#: than holding it in memory to find out. 387 KB measured; 64 MB is ~170x that.
MAX_RESPONSE_BYTES = 64 << 20

#: Named after the project so a rate-limit conversation has something to point at.
_USER_AGENT = "ziggurat/4.2b"

#: Positions this league starts. The feed's IDP pages (db/dl/lb — 996 of the
#: 1,678 rows on the 2026-09-04 scrape) are dropped at ingest, exactly as
#: ``adp_rankings`` and ``fpecr`` do: this league scores none of them, and a
#: board a novice cannot act on is noise a later reader can pick up by mistake.
LEAGUE_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DST"})

#: The weekly pages that survive the position filter, for the collapse floor's
#: per-page arm and for the registry's scope line. Derived from the same
#: measurement as ``LEAGUE_POSITIONS`` (one page carries exactly one position on
#: all nine pages, measured), and asserted against the ingested rows rather than
#: trusted: a renamed page must widen the floor, not silently empty it.
LEAGUE_PAGES = frozenset({"qb", "ppr-rb", "ppr-wr", "ppr-te", "k", "dst"})

#: Upstream position spellings normalized to this project's vocabulary, copied
#: from ``fpecr`` (not imported: two ingesters that share a constant by import
#: are two ingesters one upstream change breaks together).
_POSITION_ALIASES = {"PK": "K", "DEF": "DST", "D/ST": "DST", "DEFENSE": "DST"}

#: Source columns required. A release that drops one fails loudly rather than
#: storing partial rows (the item-1.4 contract).
_REQUIRED = (
    "page", "scrape_date", "fantasypros_id", "player_name", "pos", "team",
    "rank", "ecr", "sd", "best", "worst", "player_owned_avg", "player_opponent",
    "player_opponent_id", "player_ecr_delta", "note", "tag", "recommendation",
    "pos_rank", "start_sit_grade", "r2p_pts",
)

#: db_column -> source_column for the straight-through fields. Derived columns
#: (season/nfl_week/week_basis/gsis_id/espn_id) are added per row afterwards.
_COLMAP = {
    "fantasypros_id": "fantasypros_id",
    "page": "page",
    "scrape_date": "scrape_date",
    "player": "player_name",
    "position": "pos",
    "team": "team",
    "rank": "rank",
    "ecr": "ecr",
    "sd": "sd",
    "best": "best",
    "worst": "worst",
    # Upstream's own label ('QB1'), NOT an integer — see the migration header for
    # why it is not called `pos_rank` like its two sibling tables' derived ints.
    "pos_rank_label": "pos_rank",
    "player_owned_avg": "player_owned_avg",
    "player_ecr_delta": "player_ecr_delta",
    "player_opponent": "player_opponent",
    "player_opponent_id": "player_opponent_id",
    "start_sit_grade": "start_sit_grade",
    "r2p_pts": "r2p_pts",
    "note": "note",
    "tag": "tag",
    "recommendation": "recommendation",
}

#: Columns stored as TEXT that upstream may serve as an all-NaN float column
#: (``note``/``tag``/``recommendation`` are empty on every scrape seen so far, so
#: pandas types them float64). Coerced explicitly so the stored value is always a
#: string or NULL and never a float that happens to live in a TEXT column.
_TEXT_COLUMNS = (
    "page", "scrape_date", "player", "position", "team", "pos_rank_label",
    "player_opponent", "player_opponent_id", "start_sit_grade",
    "note", "tag", "recommendation",
)

#: The stored PRIMARY KEY (migration 016). Passed to ``base.upsert`` so its
#: return value is DISTINCT KEYS WRITTEN, and so ``base.upsert`` validates this
#: tuple against the key SQLite actually enforces on every ingest.
_PK_COLS = ("fantasypros_id", "page", "scrape_date", "retrieved_as_of")

#: THE CAPTURE FLOOR (see :class:`WeeklyEcrCollapse`). One fraction, applied
#: three ways: total keys, per-page keys, and the share of rows carrying an
#: ``ecr``. A LABELLED HYPOTHESIS, and the number is set by BYE WEEKS rather than
#: by a revision measurement (there is none yet — recon UNKNOWN 5's neighbour).
#: The arithmetic: FantasyPros ranks the players who PLAY, and 2026 runs up to
#: six teams on bye in one week, so a healthy board legitimately shrinks ~19%
#: week to week and a one-per-team page (``dst`` 32 rows, ``k`` 34) shrinks to
#: ~26. A floor at 0.90 would fire on an ordinary Sunday. The shape this floor
#: actually exists to catch — a page missing entirely, or a half-written file —
#: is a far bigger move than 30%. The cost of the loose setting is stated rather
#: than hidden: a genuinely 30%-truncated scrape would pass.
_MIN_BOARD_FRACTION = 0.70

#: The column whose emptiness is the ``players.CrosswalkCollapse`` signature
#: here: a board full of rows with no consensus in them is not a board, and a row
#: COUNT cannot see it.
_VALUE_COLUMN = "ecr"

#: Environment variable behind operator decision D2(b) — the FantasyPros page as
#: the week-label AUTHORITY. **DEFAULT OFF**, and it stays off until the operator
#: ratifies: it is one extra outbound request per pull to a site whose terms
#: allow "a single copy made for personal use only", and the schedule-derived
#: label is already exact from the season opener on. Set it to 1/true/yes/on in
#: the repo ``.env`` to enable.
WEEK_PAGE_ENV = "ZIGGURAT_FP_WEEK_PAGE"

#: The page read when that opt-in is on. ``/nfl/rankings/`` is not disallowed by
#: FantasyPros' robots.txt (checked 2026-09-04). Any weekly page carries the same
#: ``ecrData.week``; the PPR RB board is the one this project measured against.
WEEK_PAGE_URL = "https://www.fantasypros.com/nfl/rankings/ppr-rb.php"

#: Server-rendered ``var ecrData = {...};`` — the object carrying an explicit
#: ``year``/``week``/``last_updated_ts``. Non-greedy up to the first ``};`` so a
#: later script block cannot extend the match.
_ECR_DATA_RE = re.compile(r"var\s+ecrData\s*=\s*(\{.*?\})\s*;", re.DOTALL)

#: Bounds for the one page read: it is ~500 KB of HTML.
_PAGE_BUDGET_S = 60.0
_MAX_PAGE_BYTES = 16 << 20


class WeeklyEcrCollapse(RuntimeError):
    """A degraded capture would have SHADOWED a good one. Checked BEFORE the write.

    APPEND-ONLY IS NOT A FLOOR (the ``players.CrosswalkCollapse`` lesson,
    measured live in item 3.1b): ``base.select_as_of`` resolves the newest
    ``retrieved_as_of`` PER KEY, so a capture that merely arrives later wins for
    every key it contains — even if its values are empty. Nothing has to be
    deleted for the good rows to become unreadable. This ingester has no delete
    path at all, and it still needs a floor.

    Four shapes are refused, all before anything is written:

    * a capture in which NOT ONE row carries an ``ecr`` (checked even on the
      first capture of a season, where there is nothing to compare against);
    * fewer than ``_MIN_BOARD_FRACTION`` of the newest stored capture's rows —
      a truncated or half-published file;
    * a PAGE the stored capture had that this one has lost or that shrank below
      the same fraction — the sharp case, because a scrape that published only
      ``qb`` is otherwise indistinguishable from a small week;
    * a materially smaller share of rows carrying an ``ecr`` — the emptied-values
      shape, which no row count can see.
    """


# ------------------------------------------------------------------ helpers


def _coerce_fp_id(value):
    """FantasyPros id -> the bare digit string, matching ``players.fantasypros_id``.

    Copied in behaviour (not imported) from ``adp_rankings``/``fpecr``: this feed
    serves the id as an int64 while the archive serves it as a string, and both
    forms must land on the same key or the crosswalk join silently misses.
    """
    if value is None:
        return None
    if isinstance(value, float):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def _text(value):
    """A cell as a plain string, or ``None``. See ``_TEXT_COLUMNS``."""
    if value is None or value != value:      # NaN is the only x != x
        return None
    text = str(value).strip()
    return text or None


def week_bounds(conn, season: int) -> dict[int, tuple[str, str]]:
    """``week -> (first gameday, last gameday)`` for one season's REG schedule.

    Read straight off ``schedules`` with no as-of gate, and that is deliberate:
    an NFL calendar is not a decision input, it is the CLOCK the leakage gate is
    stated against. Gating it would mean a board could not be told which week it
    ranks until the games had been played, which is backwards.

    Byte-equivalent to ``fpecr.week_bounds`` and COPIED rather than imported. The
    two modules answer to two different tables with two different immutability
    stories, and the migration-016 header exists to keep a reader from conflating
    them; an import is the first step back toward that confusion, and ten lines
    of SQL is not a dependency worth taking on.
    """
    rows = conn.execute(
        "SELECT week, MIN(gameday) AS first, MAX(gameday) AS last FROM schedules "
        "WHERE season = ? AND game_type = 'REG' AND gameday IS NOT NULL "
        "GROUP BY week ORDER BY week",
        (season,),
    ).fetchall()
    return {int(r["week"]): (r["first"], r["last"]) for r in rows}


def infer_weekly_board_week(
    scrape_date: str, bounds: dict[int, tuple[str, str]]
) -> tuple[int | None, str, str]:
    """``(nfl_week, week_basis, why)`` for one scrape day, from the schedule alone.

    **NOT** ``fpecr.infer_nfl_week``, and the difference is the whole reason this
    function exists. That rule answers ``(0, 'schedules')`` — "a preseason board"
    — for any scrape before week 1's first kickoff, which is correct for the
    Friday DRAFT boards the archive is made of and WRONG for a weekly board:
    measured 2026-09-04 it returns week 0 while the live page ranks week 1. A
    weekly positional board has no week-0 edition to be.

    THE RULE, and the four answers it can give:

    * no REG schedule ingested for the season -> ``(None, 'unknown', ...)``
    * BEFORE the season's first REG gameday   -> ``(None, 'unknown', ...)``
    * otherwise the earliest REG week whose LAST gameday is on or after the
      scrape                                   -> ``(week, 'schedules', ...)``
    * after the last REG gameday               -> ``(None, 'unknown', ...)``

    WHY A PRE-OPENER CAPTURE IS ``'unknown'`` RATHER THAN WEEK 1. "The first week
    that has not finished" answers week 1 for every day back to the previous
    March, so before any REG game has been played the schedule is not deciding
    anything — it is agreeing with the only week left. The board really is week
    1's, but the SCHEDULE cannot say so, and a basis column that claims an
    authority it did not use is worse than a NULL. This is the branch operator
    decision D2(b) exists for: the FantasyPros page states ``year``/``week``
    explicitly, and when it is read the answer is ``'fantasypros_page'``.

    THE ONE KNOWN SOFT DAY, stated rather than discovered later: the MONDAY a
    week ends. Week N's last gameday is its Monday night game, so this rule still
    answers N that day — while FantasyPros may already have flipped its weekly
    page to N+1 (recon UNKNOWN 5; one in-season observation settles it). Every
    Tuesday capture — which is the one the archive is built around — is
    unambiguous, because week N is finished and week N+1 is the first unfinished
    one.
    """
    ask = (f" — set {WEEK_PAGE_ENV}=1 in .env to let the FantasyPros page state the "
           "week instead (operator decision D2(b), pending)")
    if not bounds:
        return None, "unknown", (
            "no REG schedule is ingested for this season, so nothing can label the "
            "board" + ask
        )
    weeks = sorted(bounds)
    if scrape_date < bounds[weeks[0]][0]:
        return None, "unknown", (
            f"the scrape predates the season opener ({bounds[weeks[0]][0]}), so the "
            "schedule cannot say which week this board ranks — every remaining week is "
            "still ahead. NOT week 0: a weekly board has no preseason edition" + ask
        )
    for week in weeks:
        if scrape_date <= bounds[week][1]:
            return week, "schedules", (
                f"the earliest REG week still unfinished on {scrape_date} "
                f"(week {week} runs {bounds[week][0]}..{bounds[week][1]})"
            )
    return None, "unknown", (
        f"the scrape is later than the season's last REG gameday ({bounds[weeks[-1]][1]})"
        + ask
    )


# --------------------------------------------------- the opt-in page authority


def week_page_enabled(environ=None) -> bool:
    """Is the FantasyPros page allowed to be the week-label authority? DEFAULT NO.

    Operator decision D2(b) is PENDING, so this is off unless
    ``ZIGGURAT_FP_WEEK_PAGE`` is set truthy in the repo ``.env`` (loaded the same
    non-overriding way the ESPN credentials and the ntfy topic are). Off means
    the capture still happens and ``week_basis`` records ``'schedules'`` or
    ``'unknown'`` — the authority is an upgrade to the LABEL, never a condition
    of the capture.

    ``environ`` is injectable so a test can exercise both settings without
    touching the process environment or the repo ``.env``.
    """
    if environ is None:
        try:  # optional: load the repo .env so the flag need not be exported
            from dotenv import load_dotenv

            from ziggurat.paths import REPO_ROOT

            load_dotenv(REPO_ROOT / ".env", override=False)
        except ImportError:  # pragma: no cover - dotenv is a declared dependency
            pass
        environ = os.environ
    return (environ.get(WEEK_PAGE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_bounded(response, *, url: str, budget_s: float, max_bytes: int) -> bytes:
    """Read a whole response under BOTH a time budget and a size cap.

    ``net.HTTP_TIMEOUT`` bounds each socket READ, not the transfer, so a
    trickling upstream would hold the daily unit for as long as it liked and the
    perishable sources behind this one in the registry would never run. The size
    cap is the other half: a document that answers with gigabytes is not one this
    function should try to hold in memory to find that out.
    """
    started = time.monotonic()
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = response.read(1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        received += len(chunk)
        if received > max_bytes:
            raise ValueError(
                f"fp_weekly: {url} returned more than {max_bytes} bytes, which is not "
                "the shape of the document this reads. Refusing."
            )
        if time.monotonic() - started > budget_s:
            raise TimeoutError(
                f"fp_weekly: reading {url} exceeded its {budget_s:.0f} s budget "
                f"({received} bytes received) — a trickling upstream; the partial read is "
                "discarded and the sources behind this one in the registry still run"
            )


def fetch_week_page(*, url: str = WEEK_PAGE_URL) -> str:
    """The FantasyPros rankings page as text. A NETWORK SEAM, never used offline.

    Only reached when :func:`week_page_enabled` is true, which is not the default
    and is never true in a test — tests patch this function or
    :func:`parse_page_week`. One bounded request; nothing from the page is stored
    beyond the three integers :func:`parse_page_week` extracts (Rule 5 / the site's
    "single copy for personal use" terms).
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=net.HTTP_TIMEOUT) as response:  # noqa: S310
        raw = _read_bounded(response, url=url, budget_s=_PAGE_BUDGET_S,
                            max_bytes=_MAX_PAGE_BYTES)
    return raw.decode("utf-8", errors="replace")


def parse_page_week(html: str) -> tuple[int, int, str | None] | None:
    """``(year, week, last_updated_ts)`` out of the page's ``var ecrData``, or None.

    ``None`` for anything unexpected — a missing block, unparseable JSON, a
    missing or non-integer ``week``. Returning None rather than raising is the
    point: this is an OPTIONAL label upgrade, and losing a perishable capture
    because a marketing team changed a script tag would be the wrong trade.
    """
    match = _ECR_DATA_RE.search(html or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except ValueError:
        return None
    try:
        year = int(payload["year"])
        week = int(payload["week"])
    except (KeyError, TypeError, ValueError):
        return None
    stamp = payload.get("last_updated_ts")
    return year, week, (str(stamp) if stamp is not None else None)


def resolve_page_week(*, season: int, fetcher=fetch_week_page) -> tuple[int, str | None] | None:
    """The page's own week for ``season``, or ``None`` if it cannot be trusted.

    Refuses a page whose ``year`` is not the season being captured — that is the
    one failure mode of an out-of-band authority (a cached page from January
    ranking week 18 of the previous season), and it is silent unless checked.
    Every failure here is LOGGED and swallowed: the capture must not be lost over
    an optional label.
    """
    try:
        parsed = parse_page_week(fetcher())
    except Exception as exc:                       # noqa: BLE001 — see docstring
        logger.warning(
            "fp_weekly: the FantasyPros week-label page could not be read (%s: %s); "
            "falling back to the schedule-derived week", type(exc).__name__, exc,
        )
        return None
    if parsed is None:
        logger.warning(
            "fp_weekly: the FantasyPros page carried no readable ecrData week; "
            "falling back to the schedule-derived week"
        )
        return None
    year, week, stamp = parsed
    if year != int(season):
        logger.warning(
            "fp_weekly: the FantasyPros page reports year %s while this capture is for "
            "season %s — refusing to label a %s board from a %s page", year, season,
            season, year,
        )
        return None
    return week, stamp


# ------------------------------------------------------------------ network


def fetch_fp_weekly(*, url: str = FP_WEEKLY_URL, budget_s: float = FETCH_BUDGET_S) -> bytes:
    """Download the weekly board CSV and return its bytes. THE network seam.

    Held in memory rather than mirrored to disk: the file is ~387 KB, every
    column that MOVES is stored, and the module docstring records why no raw
    mirror is kept. Bounded twice (``net.HTTP_TIMEOUT`` per socket read,
    ``budget_s`` over the whole transfer) for the reason ``fpecr.fetch_fpecr``
    is: a per-read timeout alone cannot bound a trickling upstream.

    Tests patch THIS function; nothing offline touches the network.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=net.HTTP_TIMEOUT) as response:  # noqa: S310
        return _read_bounded(response, url=url, budget_s=budget_s,
                             max_bytes=MAX_RESPONSE_BYTES)


def read_fp_weekly(source):
    """Parse the weekly board CSV (bytes, or a path) into a pandas frame.

    Kept as its own seam so a test can read the committed fixture through the
    SAME parse the live pull uses — a hand-built frame would not exercise the
    dtypes upstream actually serves (an int64 id, an all-NaN float column where a
    TEXT column is declared).
    """
    import pandas as pd

    if isinstance(source, bytes | bytearray):
        return pd.read_csv(io.BytesIO(bytes(source)))
    return pd.read_csv(str(source))


# ------------------------------------------------------------------ the floor


def _stored_capture(conn, *, season: int) -> tuple[str | None, dict[str, int], int, int]:
    """The newest stored capture for ``season``: (day, rows per page, rows, valued)."""
    row = conn.execute(
        "SELECT MAX(retrieved_as_of) FROM fp_weekly_ecr WHERE season = ?", (int(season),)
    ).fetchone()
    day = row[0] if row else None
    if day is None:
        return None, {}, 0, 0
    per_page = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT page, COUNT(*) FROM fp_weekly_ecr WHERE season = ? AND "
            "retrieved_as_of = ? GROUP BY page", (int(season), day),
        )
    }
    valued = int(conn.execute(
        f"SELECT COUNT(*) FROM fp_weekly_ecr WHERE season = ? AND retrieved_as_of = ? "
        f"AND {_VALUE_COLUMN} IS NOT NULL", (int(season), day),
    ).fetchone()[0])
    return day, per_page, sum(per_page.values()), valued


def _check_board_floor(conn, rows, *, season: int) -> None:
    """Refuse a capture that would shadow a better one. See :class:`WeeklyEcrCollapse`."""
    incoming_pages: dict[str, int] = {}
    for row in rows:
        page = str(row["page"])
        incoming_pages[page] = incoming_pages.get(page, 0) + 1
    incoming_total = len(rows)
    incoming_valued = sum(1 for row in rows if row[_VALUE_COLUMN] is not None)

    if incoming_valued == 0:
        # THE ABSOLUTE CASE, checked even on a first capture: rows present,
        # consensus empty. This is the shape `players.CrosswalkCollapse` was
        # written for, and it is the one a row count cannot see.
        raise WeeklyEcrCollapse(
            f"fp_weekly_ecr: the incoming board carries {incoming_total} rows for season "
            f"{season} and NOT ONE of them has an {_VALUE_COLUMN} — the consensus is this "
            "source's entire reason to exist. Rows with empty values do not have to "
            "delete anything to hide good ones: select_as_of resolves the NEWEST "
            "retrieved version per key, so merely arriving later is enough. Refusing to "
            "write; check the download and re-run."
        )

    day, stored_pages, stored_total, stored_valued = _stored_capture(conn, season=season)
    if day is None:
        return

    floor = int(stored_total * _MIN_BOARD_FRACTION)
    if incoming_total < floor:
        raise WeeklyEcrCollapse(
            f"fp_weekly_ecr: the incoming board carries {incoming_total} rows for season "
            f"{season} but the capture stored on {day} holds {stored_total} (floor "
            f"{floor} = {_MIN_BOARD_FRACTION:.0%}, which already allows for a six-team "
            "bye week). That is a truncated or half-published scrape, and it would "
            "shadow the stored one for every key it does contain. Refusing to write."
        )

    for page in sorted(stored_pages):
        was, now = stored_pages[page], incoming_pages.get(page, 0)
        if now < int(was * _MIN_BOARD_FRACTION):
            raise WeeklyEcrCollapse(
                f"fp_weekly_ecr: page {page!r} holds {was} rows in the season-{season} "
                f"capture stored on {day} but only {now} in the incoming one (floor "
                f"{_MIN_BOARD_FRACTION:.0%}). A page that vanishes or halves is a "
                "half-published scrape — and it is invisible in the TOTAL, which is why "
                "this arm exists separately. Refusing to write the WHOLE capture: a "
                "partial one is how a board ends up half-versioned with nothing saying so."
            )

    was_share = stored_valued / stored_total if stored_total else 0.0
    now_share = incoming_valued / incoming_total if incoming_total else 0.0
    if was_share and now_share < was_share * _MIN_BOARD_FRACTION:
        raise WeeklyEcrCollapse(
            f"fp_weekly_ecr: {now_share:.1%} of the incoming season-{season} rows carry an "
            f"{_VALUE_COLUMN}, against {was_share:.1%} in the capture stored on {day} "
            f"(floor {_MIN_BOARD_FRACTION:.0%} of that). The row COUNT is fine, which is "
            "exactly why this check exists separately: a board whose values were emptied "
            "upstream shadows a good one on every key it shares. Refusing to write."
        )


# ------------------------------------------------------------------ ingest


def ingest_fp_weekly(conn, df, *, retrieved_as_of: str, page_week=None) -> int:
    """Persist one weekly board, stamping ``knowable_as_of`` with its scrape date.

    ``page_week`` is the optional ``(week, last_updated_ts)`` from the FantasyPros
    page authority (operator decision D2(b), DEFAULT OFF — see
    :func:`week_page_enabled`). When given it WINS over the schedule-derived week
    and ``week_basis`` records ``'fantasypros_page'``; when absent the schedule
    decides and may answer ``'unknown'``.

    The write is ONE transaction: a failure part-way rolls the whole capture back
    rather than leaving a half-written board that a later read would resolve
    against (item 3.1b — "a failed source's partial rows must not ride the next
    source's commit").
    """
    base.require_columns(df, _REQUIRED, source="fp_weekly_ecr")

    frame = df[list(dict.fromkeys(_COLMAP.values()))]
    rows = base.frame_to_rows(
        frame,
        _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=lambda src: base.iso_date(src.get("scrape_date")),
    )

    crosswalk = base.ids_by_fantasypros(conn)
    bounds_cache: dict[int, dict[int, tuple[str, str]]] = {}
    week_notes: dict[tuple[int, str], str] = {}

    kept: list[dict] = []
    unstampable = pageless = 0
    for row in rows:
        for column in _TEXT_COLUMNS:
            row[column] = _text(row[column])
        scrape = row["scrape_date"]
        if scrape is None:
            # No knowledge time; counted here and RAISED below (see there).
            unstampable += 1
            continue
        row["scrape_date"] = scrape = base.iso_date(scrape)
        position = (row["position"] or "").upper()
        position = _POSITION_ALIASES.get(position, position)
        if position not in LEAGUE_POSITIONS:
            continue                                # IDP — a by-design filter
        if row["page"] is None:
            # NOT a by-design filter and NOT fatal. `page` is a NOT NULL key
            # column, so a row without one is unstorable — that is a LOSS, and it
            # belongs on the drop channel where `run_ingest`'s 20% ceiling can see
            # it: one stray row is recorded, a systematic loss fails the run.
            # Distinct from the missing-scrape_date case, which raises: that
            # column is the board's own snapshot key AND its knowledge time, so
            # its absence is schema drift rather than one malformed row. Measured
            # 0 of 1,678 on the 2026-09-04 scrape.
            pageless += 1
            continue
        row["position"] = position
        row["page"] = row["page"].lower()
        season = nfl_season_of(scrape)
        row["season"] = season
        if season not in bounds_cache:
            bounds_cache[season] = week_bounds(conn, season)
        week, basis, why = infer_weekly_board_week(scrape, bounds_cache[season])
        if page_week is not None:
            # The AUTHORITY, when it was read at all: the page states the week it
            # ranks, the schedule only infers it. Recorded as such so a later
            # reader can tell the two apart rather than trusting one number.
            week, basis, why = (
                int(page_week[0]), "fantasypros_page",
                f"the FantasyPros rankings page states week {int(page_week[0])} "
                f"(last_updated_ts={page_week[1]!r}); the schedule would have said "
                f"{week!r} via {basis!r}",
            )
        row["nfl_week"] = week
        row["week_basis"] = basis
        week_notes[(season, basis)] = why
        row["fantasypros_id"] = _coerce_fp_id(row["fantasypros_id"])
        if row["team"] is not None:
            team = row["team"].upper()
            row["team"] = base.TEAM_ALIASES.get(team, team)
        if row["player_opponent_id"] is not None:
            opponent = row["player_opponent_id"].upper()
            row["player_opponent_id"] = base.TEAM_ALIASES.get(opponent, opponent)
        gsis, espn = crosswalk.get(row["fantasypros_id"], (None, None))
        row["gsis_id"] = gsis
        row["espn_id"] = espn
        kept.append(row)

    # ONE note_drops call PER CHANNEL, each given the population it actually
    # examined. `base.collect_drops` SUMS `total` across calls (item 3.2c, F-H),
    # so this sum over-states the denominator by `len(rows)` whenever the second
    # call fires — accepted for the same reason B4 accepted it on
    # `ff_opportunity`: `run_ingest`'s drop ceiling computes written + lost and
    # never reads `total`, so the sum is not load-bearing, while passing 0 to keep
    # it exact would make the LOG LINE read "dropped 1/0" — and that string is the
    # only place either number is ever shown.
    filtered = len(rows) - len(kept) - unstampable - pageless
    base.note_drops(
        "fp_weekly_ecr", filtered, len(rows),
        why=("IDP page (DB/DL/LB — not startable in this league; 996 of 1,678 rows on "
             "the 2026-09-04 scrape) or an unknown position"),
        by_design=True,
    )
    if pageless:
        base.note_drops(
            "fp_weekly_ecr", pageless, len(rows) - filtered - unstampable,
            why="no `page` — a NOT NULL key column, so the row cannot be stored",
        )
    if unstampable:
        # NOT a by-design filter. `scrape_date` is upstream's own snapshot key and
        # a row without one has no knowledge time to stamp under Rule 1. Refusing
        # costs a perishable day, which is why it is a hard error rather than a
        # drop: a board whose snapshot key is missing is upstream schema drift,
        # and storing part of it under a guessed date would be worse.
        raise WeeklyEcrCollapse(
            f"fp_weekly_ecr: {unstampable} of {len(rows)} rows carry no scrape_date, so "
            "they have no knowledge time to stamp. That column is the board's own "
            "snapshot key; its absence is upstream schema drift. Refusing the run."
        )

    if not kept:
        # "wrote 0 rows" is never ok (item 3.1b). An empty result here means the
        # position filter matched nothing, which for a board that is ~41% league
        # positions is a schema change, not a legitimately empty upstream.
        raise WeeklyEcrCollapse(
            f"fp_weekly_ecr: no rows survived the position filter ({filtered} filtered, "
            f"{pageless} without a page). The frame carried "
            f"{len(rows)} rows; check that upstream still publishes the "
            f"{sorted(LEAGUE_PAGES)} pages."
        )

    unresolved = sum(
        1 for row in kept if row["gsis_id"] is None and row["position"] != "DST"
    )
    base.note_incomplete(
        "fp_weekly_ecr", unresolved, len(kept),
        why="unresolved FantasyPros crosswalk id (kept, NULL gsis_id)",
    )
    for (season, basis), why in sorted(week_notes.items()):
        base.note_run(
            "fp_weekly_ecr",
            f"season {season} week label from {basis!r}: {why}",
        )

    for season in sorted({int(row["season"]) for row in kept}):
        _check_board_floor(conn, [r for r in kept if int(r["season"]) == season],
                           season=season)

    with conn:
        return base.upsert(conn, "fp_weekly_ecr", kept, key_cols=_PK_COLS, commit=False)


def pull_fp_weekly(conn, *, retrieved_as_of: str, season: int, environ=None) -> int:
    """Fetch today's weekly board and store it.

    ``season`` is used only to sanity-check the OPTIONAL page authority (a cached
    January page ranking last season's week 18 is the one silent failure mode of
    an out-of-band label); the board's own ``scrape_date`` decides every row's
    season, exactly as ``adp_rankings`` lets the scrape decide.
    """
    page_week = None
    if week_page_enabled(environ):
        page_week = resolve_page_week(season=int(season))
        if page_week is None:
            # Only noted when the opt-in was ON and failed. The OFF case is NOT
            # noted: it is the default, it fires every single day, and a standing
            # line about a setting nobody has turned on is exactly the wolf-cry
            # that teaches an operator to skim the run log. When the schedule
            # cannot label a board, `infer_weekly_board_week`'s own reason names
            # the setting — i.e. it is mentioned on the day it would have helped.
            base.note_run(
                "fp_weekly_ecr",
                f"{WEEK_PAGE_ENV} is set, but the FantasyPros page could not supply a "
                "usable week (see the warning above); the week label falls back to the "
                "schedule and week_basis says so.",
            )
    raw = fetch_fp_weekly()
    return ingest_fp_weekly(
        conn, read_fp_weekly(raw), retrieved_as_of=retrieved_as_of, page_week=page_week
    )


# ------------------------------------------------------------------ read


def get_fp_weekly_ecr(
    conn,
    *,
    as_of,
    season=None,
    nfl_week=None,
    page=None,
    position=None,
    scrape_date=None,
    view: base.AsOfView = "historical",
):
    """Weekly-board rows knowable on or before ``as_of`` (keyword-only; no implicit now).

    Every filter is optional and ANDed.

    THE VIEW. Unlike ``fpecr_panel`` this source is captured LIVE — the scrape
    day and the pull day are the same day — so a ``historical`` read at a past
    ``as_of`` returns what was genuinely knowable then, and that is the right
    default for any live decision. A backtest that BULK-LOADS past captures (or
    re-reads them after a later correction) goes through
    ``base.latest_truth(get_fp_weekly_ecr)``, which binds the view so it cannot
    be forgotten; the fact-time gate is unchanged either way, so a read at week 3
    still cannot see week 4's board.

    NOTHING IN ``ziggurat/core/`` MAY CALL THIS IN WEEK 1 (item 4.2b: capture
    only, no integration), and a test enforces the import fence.
    """
    clauses, params = [], {}
    for column, value in (
        ("season", season),
        ("nfl_week", nfl_week),
        ("page", page),
        ("position", position),
        ("scrape_date", scrape_date),
    ):
        if value is not None:
            clauses.append(f"t.{column} = :{column}")
            params[column] = value
    return base.select_as_of(
        conn, "fp_weekly_ecr", as_of=as_of,
        key_cols=["fantasypros_id", "page", "scrape_date"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
