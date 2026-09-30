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

**AND THE ROWS OUTRANK ALL OF THEM (2026-09-30, item 3.14b).** Both of those
authorities describe the moment of the PULL; only the rows describe the FILE.
Three Tuesdays in four the page had flipped to week N+1 while upstream had not
rewritten Monday's week-N file, and the pull filed week N's board as N+1
(100% of its opponents were week N's). ``'opponents'`` —
:func:`infer_week_from_opponents` — now labels every page whose (team,
opponent) pairs decide a week, BEFORE the per-page floor runs, and the older
chain answers only where the content cannot.

RULE 2. ``r2p_pts`` and ``start_sit_grade`` are FantasyPros' OWN projected points
and their own start/sit letter grade, in THEIR scoring. They are stored because
they are what the market was saying — never as a points input. House points come
from ``ziggurat/core/scoring.py`` and nowhere else.

RULE 5 / ToS. FantasyPros' terms permit "a single copy made for personal use
only": the capture is local, and **nothing captured is ever committed**. The
committed fixture under ``tests/fixtures/nfl/`` is a trimmed copy of the public
NFL board — player names only, no league-private data.

THE COLLAPSE FENCE IS PER PAGE AND PER WEEK (item 3.14a, 2026-09-15), and the
scope was ARGUED FROM THE KEY rather than chosen for symmetry. Stated once, here,
because it is the sort of rule a later reader tightens "to be safe" and thereby
re-breaks:

* **Why per PAGE.** Measured live on the first in-season Tuesday: the week-2
  board at 07:30 PT carried ``dst`` 32 (of 32 NFL teams — COMPLETE), ``k`` 33,
  and skill pages at roughly half their eventual size (``ppr-wr`` 116, ``ppr-rb``
  88, ``qb`` 33). A whole-board floor read 356 against a stored 683 and refused
  the LOT — including a complete ``dst`` page, which item 3.14 had made the
  PRIMARY D/ST ranker for ``ziggurat stream`` that same morning. A complete page
  must never be thrown away because a sibling page is thin; the pages are
  published independently and there is no reason for them to fail together.
* **Why per WEEK, and why a cross-week thin page is NOT refused.** The stored
  primary key is ``(fantasypros_id, page, scrape_date, retrieved_as_of)`` and
  :func:`get_fp_weekly_ecr` resolves per ``(fantasypros_id, page, scrape_date)``.
  ``nfl_week`` is a deterministic function of ``scrape_date``
  (:func:`infer_weekly_board_week`, or the page authority read once per pull), so
  two captures carrying different weeks necessarily carry different
  ``scrape_date``s and therefore occupy **disjoint key spaces**. A week-2 row
  cannot shadow a week-1 row — not "usually", structurally. Refusing a thin
  week-2 page because last week's was fatter protects nothing that
  ``select_as_of`` could hide, and costs a perishable capture that exists
  nowhere else once upstream rewrites the file. It would also cry wolf every
  Tuesday of the season, since a new week's board is BUILT UP through the week
  (measured: week 1 grew ``ppr-wr`` 206 -> 257 over six days).
* **What the fence does still bite on: a truncated re-scrape of the SAME week.**
  That is the case where the incoming rows really can shadow stored ones — same
  page, same week, and (when it is the same ``scrape_date`` re-fetched a day
  later) the same keys. It is also the only case where "this page shrank" is
  evidence of a publishing fault rather than of a different population.
* **The narrowest defensible scope would be same ``scrape_date``**, since that is
  exactly the shadowing key. Same-WEEK is deliberately one notch wider: within a
  week the page ranks the same population, so a halving is a fault worth
  refusing even when it lands under a new ``scrape_date`` and shadows nothing.
  Recorded so the extra notch is a choice rather than an accident.
* **THE ONE HOLE THIS LEAVES, stated rather than discovered later.** "Different
  week implies a different ``scrape_date``" is exact in one direction only. The
  converse can fail: if upstream does NOT rewrite the file for a day and the
  OPT-IN page authority has meanwhile flipped weeks, two pulls of the SAME
  ``scrape_date`` can be labelled N and N+1 — same key, different week — and the
  later one would find no same-week baseline and so face no COUNT floor. The
  absolute arm still fires (it needs no baseline), so the emptied-values
  catastrophe is still caught; what slips is a half-sized re-fetch of a file that
  by construction has not changed. Judged not worth a second baseline path, which
  would be more machinery than the corner is worth — but it is a hole, not an
  absence of one.
  **CLOSED 2026-09-30 (item 3.14b), and it was not a corner.** It happened on
  three Tuesdays in four. The label also RELABELLED the stored rows, and the
  mislabelled baseline then refused the next genuine week's thin pages. The
  label now comes from the rows' own opponents whenever they decide it, so one
  file carries one week on every pull.

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
    "pos_rank", "start_sit_grade",
)

#: Source columns that MAY be absent and are then stored NULL. ``r2p_pts``
#: (FantasyPros' own projected points) vanished from ``fp_latest_weekly.csv`` on
#: 2026-09-15 — the first in-season Tuesday and the day item 3.14 made this
#: capture the primary D/ST ranker — and the whole perishable week-2 board
#: failed loudly on it (timer 07:22 PT: "source schema missing required columns
#: ['r2p_pts']"). Nothing in this repo may read that column (RULE 2 above), so
#: its absence costs no consumer anything; it stays in the table as NULL.
#: Anything else missing is still drift and still fails loudly.
_OPTIONAL = ("r2p_pts",)

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

#: THE CAPTURE FLOOR (see :class:`WeeklyEcrCollapse`). One fraction, applied PER
#: PAGE two ways: the page's key count, and the share of its rows carrying an
#: ``ecr``. A LABELLED HYPOTHESIS, and the number is set by BYE WEEKS rather than
#: by a revision measurement (there is none yet — recon UNKNOWN 5's neighbour).
#: The arithmetic: FantasyPros ranks the players who PLAY, and 2026 runs up to
#: six teams on bye in one week, so a healthy board legitimately shrinks ~19%
#: week to week and a one-per-team page (``dst`` 32 rows, ``k`` 34) shrinks to
#: ~26. A floor at 0.90 would fire on an ordinary Sunday. The shape this floor
#: actually exists to catch — a page missing entirely, or a half-written file —
#: is a far bigger move than 30%. The cost of the loose setting is stated rather
#: than hidden: a genuinely 30%-truncated scrape would pass.
#:
#: WITHIN ONE WEEK the observed move is smaller still and the direction is not
#: always up: the live week-1 board grew ``ppr-wr`` 206 -> 257 over six days and
#: then fell to 240, and ``qb`` fell 96 -> 82 on the Monday (−15%). So 0.70 has
#: real headroom against a same-week wobble, which is the only comparison this
#: fence now makes.
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

    **THE UNIT OF REFUSAL IS THE PAGE, NOT THE BOARD (item 3.14a, 2026-09-15).**
    See :func:`page_floor_verdicts` for the scope rule and the measurement that
    forced it. The shapes a page is refused for:

    * a page in which NOT ONE row carries an ``ecr`` — checked even on the first
      capture of a season, where there is nothing to compare against;
    * fewer than ``_MIN_BOARD_FRACTION`` of the same page's rows in the newest
      stored capture OF THE SAME WEEK — a truncated or half-published re-scrape;
    * a materially smaller share of that page's rows carrying an ``ecr`` — the
      emptied-values shape, which no row count can see.

    A page the stored week HAD and this capture does not is reported as a refusal
    (0 rows to refuse) rather than raising: there is nothing to write and nothing
    to shadow, but "upstream stopped publishing ``dst``" must not be silent.

    THE EXCEPTION IS STILL RAISED, and means exactly one thing now: **no page
    survived**, so there is nothing to write at all. "Wrote 0 rows" is never
    ``ok`` (item 3.1b), and a whole-capture failure is the honest status for it.
    A capture where SOME pages land and others are refused is a ``partial`` run
    with the refused pages named — see :func:`base.note_refused`.

    THE WHOLE-BOARD TOTAL ARM IS GONE, and nothing is lost by it: if every stored
    page clears ``f`` then the total clears ``f`` too (sum of per-page floors),
    so the total arm could only ever fire in cases the per-page arm already
    catches. What it COULD do, and did on 2026-09-15, is refuse a page that was
    complete because its siblings were thin.
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


# ------------------------------------------------------ the content authority

#: THE CONTENT AUTHORITY (2026-09-30). Every row names the opponent its player
#: faces, so a page can be matched against the schedule directly. That answers the
#: question the label exists for — WHICH WEEK DO THESE ROWS RANK — from the rows
#: themselves, instead of from the clock (``infer_weekly_board_week``) or from a
#: page read at pull time (``resolve_page_week``).
#:
#: WHY IT OUTRANKS BOTH (measured, three times in four in-season weeks). The file
#: and the page are two different moments. DynastyProcess rewrites the CSV on no
#: fixed clock (commits 09-27 07:32Z, 09-27 18:03Z, 09-28 08:09Z, 09-28 20:23Z,
#: then nothing until 09-30 07:53Z), while the FantasyPros page flips to the next
#: week on Monday night. So a Tuesday pull read "week 4" off the page and filed
#: the MONDAY scrape under it. On every page, 100% of that scrape's
#: opponents were week 3's and 0% were week 4's. Two consequences followed.
#: ``ziggurat stream`` printed a week-3 market rank beside a week-4 opponent. And
#: the per-page floor compared the genuine week-4 file (09-30) against that
#: mislabelled baseline, refusing its TE and WR pages. Because ``nfl_week`` was
#: decided per pull, a re-pull of the same ``scrape_date`` also RELABELLED rows
#: already stored. A label derived from the rows is the same on every pull of the
#: same file, so that hole closes as well.
#:
#: THE THRESHOLDS, labelled. A board ranks one week, so a genuine page matches it
#: almost completely. The misses are free agents FantasyPros still lists under a
#: team whose game they will not play in. A WRONG week matches only the rematches
#: it shares with the right one (at most a few of 16 games). Hence: at least
#: ``_CONTENT_MIN_ROWS`` rows with a recognisable (team, opponent) pair, at least
#: ``_CONTENT_MIN_SHARE`` of them matching the best week, and the runner-up week
#: matching no more than ``_CONTENT_MAX_RUNNER_UP``. Anything short of that is NOT
#: decisive, and the provisional label (page, then schedule) stands and says so.
_CONTENT_MIN_ROWS = 8
_CONTENT_MIN_SHARE = 0.80
_CONTENT_MAX_RUNNER_UP = 0.50

#: The ``week_basis`` value the content authority writes. No migration needed:
#: migration 016 declares the column ``TEXT NOT NULL`` with no CHECK.
CONTENT_BASIS = "opponents"


def _norm_team(value) -> str | None:
    if value is None:
        return None
    token = str(value).strip().upper()
    if not token or token in {"NAN", "NONE", "BYE"}:
        return None
    return base.TEAM_ALIASES.get(token, token)


def schedule_pairs(conn, season: int) -> dict[int, frozenset[tuple[str, str]]]:
    """``week -> {(team, opponent), ...}`` for one season's REG games, both directions.

    RULE 1, stated: no ``as_of``, for the reason ``week_bounds`` gives. The
    calendar is the CLOCK a board is labelled against, not a decision input.
    Gating it would mean a board could not be told which week it ranks until the
    games had been played.

    THE SEASON'S NEWEST SCHEDULE SNAPSHOT, not the newest row per ``game_id``.
    nflverse encodes the week IN the ``game_id`` (``2026_04_MIA_MIN``), so a game
    the league moves gets a NEW id, and a per-id newest-row read would keep a
    ghost of the old id in the old week. The whole-season file is re-pulled in
    full every time, so its newest snapshot is complete by construction.
    """
    rows = conn.execute(
        "SELECT week, home_team, away_team FROM schedules "
        "WHERE season = ? AND game_type = 'REG' "
        "AND retrieved_as_of = (SELECT MAX(retrieved_as_of) FROM schedules "
        "                       WHERE season = ? AND game_type = 'REG')",
        (int(season), int(season)),
    ).fetchall()
    out: dict[int, set[tuple[str, str]]] = {}
    for r in rows:
        home, away = _norm_team(r["home_team"]), _norm_team(r["away_team"])
        if home is None or away is None or r["week"] is None:
            continue
        pairs = out.setdefault(int(r["week"]), set())
        pairs.add((home, away))
        pairs.add((away, home))
    return {week: frozenset(p) for week, p in out.items()}


def infer_week_from_opponents(rows, pairs) -> dict:
    """Which week's schedule do these rows' (team, opponent) pairs belong to?

    PURE. ``rows`` carry ``team`` and ``player_opponent_id``; ``pairs`` is
    :func:`schedule_pairs`. Returns a dict with ``week`` (None unless DECISIVE,
    see the thresholds above), ``matched`` / ``considered`` (the best week's
    count over the rows with a recognisable pair), ``runner_up`` /
    ``runner_up_matched``, and ``best`` (the best week even when not decisive, so
    a note can say what the rows leaned toward).

    A bye or a blank opponent is not evidence either way and is left out of
    ``considered``. So is a team code the schedule has never heard of.
    """
    known = {team for week_pairs in pairs.values() for pair in week_pairs for team in pair}
    seen = []
    for row in rows:
        team = _norm_team(row.get("team"))
        opponent = _norm_team(row.get("player_opponent_id"))
        if team in known and opponent in known:
            seen.append((team, opponent))
    considered = len(seen)
    verdict = {"week": None, "best": None, "matched": 0, "considered": considered,
               "runner_up": None, "runner_up_matched": 0, "pairs": tuple(seen)}
    if not considered or not pairs:
        return verdict
    ranked = sorted(
        ((sum(1 for pair in seen if pair in week_pairs), week)
         for week, week_pairs in pairs.items()),
        key=lambda item: (-item[0], item[1]),
    )
    best_n, best_week = ranked[0]
    runner_n, runner_week = ranked[1] if len(ranked) > 1 else (0, None)
    verdict.update(best=best_week, matched=best_n, runner_up=runner_week,
                   runner_up_matched=runner_n)
    if (considered >= _CONTENT_MIN_ROWS
            and best_n >= _CONTENT_MIN_SHARE * considered
            and runner_n <= _CONTENT_MAX_RUNNER_UP * considered):
        verdict["week"] = best_week
    return verdict


def _content_labels(conn, kept) -> dict[tuple[int, str, str], dict]:
    """``(season, scrape_date, page) -> verdict`` for every page in a capture.

    A page decides for itself when it has enough rows. A page too THIN to decide
    (fewer than ``_CONTENT_MIN_ROWS`` recognisable pairs) borrows the verdict of
    its whole FILE (every page sharing its ``scrape_date``), because the pages of
    one file are one scrape — BUT ONLY IF ITS OWN ROWS DO NOT CONTRADICT IT: at
    least ``_CONTENT_MIN_SHARE`` of its recognisable pairs (if it has any) must be
    games of the file's week. A page with enough rows that still is not decisive
    does NOT borrow. Its own content is mixed, and papering over that with its
    siblings' answer would be the same over-claim this authority exists to end.
    The verdict dict gains ``scope``: ``'page'``, ``'file'``, or ``'undecided'``.
    """
    pairs_by_season: dict[int, dict] = {}
    pages: dict[tuple[int, str, str], list[dict]] = {}
    files: dict[tuple[int, str], list[dict]] = {}
    for row in kept:
        season = int(row["season"])
        pages.setdefault((season, str(row["scrape_date"]), str(row["page"])), []).append(row)
        files.setdefault((season, str(row["scrape_date"])), []).append(row)
    for season in {key[0] for key in pages}:
        pairs_by_season[season] = schedule_pairs(conn, season)
    file_verdicts = {
        key: infer_week_from_opponents(file_rows, pairs_by_season[key[0]])
        for key, file_rows in files.items()
    }
    out: dict[tuple[int, str, str], dict] = {}
    for key, page_rows in pages.items():
        season, scrape, _page = key
        verdict = infer_week_from_opponents(page_rows, pairs_by_season[season])
        if verdict["week"] is not None:
            verdict["scope"] = "page"
        elif verdict["considered"] < _CONTENT_MIN_ROWS:
            file_verdict = file_verdicts[(season, scrape)]
            file_week = file_verdict["week"]
            own = verdict["pairs"]
            agree = (sum(1 for pair in own if pair in pairs_by_season[season][file_week])
                     if file_week is not None else 0)
            if file_week is not None and agree >= _CONTENT_MIN_SHARE * len(own):
                verdict = dict(file_verdict, scope="file", page_matched=agree,
                               page_considered=len(own))
            else:
                verdict["scope"] = "undecided"
        else:
            verdict["scope"] = "undecided"
        out[key] = verdict
    return out


def _override_cause(basis: str, old, week: int) -> str:
    """One clause saying WHAT the content overrode and, only where it is known, WHY.

    Stated per basis and direction rather than as one stock sentence. The first
    draft of this note said "the file is older than the page it was labelled
    from" for every override, including a schedule-labelled Monday board that
    was NEWER than its calendar week and a pre-opener board no page was read for
    (item 3.14b review).
    """
    if basis == "fantasypros_page" and old is not None:
        direction = "OLDER" if week < int(old) else "NEWER"
        return (f"the FantasyPros page's week {old} — this file is {direction} than the "
                "page it was labelled from")
    if basis == "schedules" and old is not None:
        return (f"the schedule-clock week {old} — the rows rank a different week than "
                "the scrape date's calendar week")
    if basis == "unknown":
        return "an unlabelled week (the pull-time rules could not say)"
    return f"{basis}={old!r}"


def _content_phrase(page: str, verdict: dict) -> str:
    if verdict.get("scope") == "file":
        return (f"{page} (via the whole file: {verdict['matched']}/{verdict['considered']}; "
                f"its own rows {verdict.get('page_matched', 0)}/"
                f"{verdict.get('page_considered', 0)} agree but are too few to decide)")
    return f"{page} {verdict['matched']}/{verdict['considered']}"


def audit_stored_week_labels(conn, *, season: int) -> list[dict]:
    """Stored captures whose ``nfl_week`` disagrees with their own rows' opponents.

    READ-ONLY. One dict per ``(scrape_date, page, retrieved_as_of)`` capture whose
    content is DECISIVE for a week other than the stored one, with ``stored_week``
    / ``stored_basis`` / ``content_week`` / ``matched`` / ``considered`` /
    ``scope``. A capture whose content is not decisive is never reported: this
    names what the rows PROVE is wrong, not what they fail to confirm.

    Why this exists: before 2026-09-30 the label came from the pull-time page, so
    the 09-22 and 09-29 Tuesday pulls filed Monday scrapes under the NEXT week.
    ``select_as_of`` resolves the newest retrieval per key, so those relabels also
    rewrote what every later read saw for the earlier retrieval's key.
    """
    captures = conn.execute(
        "SELECT DISTINCT scrape_date, retrieved_as_of FROM fp_weekly_ecr "
        "WHERE season = ? ORDER BY scrape_date, retrieved_as_of",
        (int(season),),
    ).fetchall()
    out: list[dict] = []
    for capture in captures:
        rows = [
            dict(r) for r in conn.execute(
                "SELECT page, team, player_opponent_id, nfl_week, week_basis, season, "
                "scrape_date FROM fp_weekly_ecr WHERE season = ? AND scrape_date = ? "
                "AND retrieved_as_of = ?",
                (int(season), capture["scrape_date"], capture["retrieved_as_of"]),
            )
        ]
        verdicts = _content_labels(conn, rows)
        stored: dict[str, set] = {}
        for row in rows:
            stored.setdefault(str(row["page"]), set()).add(
                (row["nfl_week"], row["week_basis"]))
        for (_season, scrape, page), verdict in sorted(verdicts.items()):
            if verdict["week"] is None:
                continue
            labels = stored.get(page, set())
            if all(week == verdict["week"] for week, _basis in labels):
                continue
            out.append({
                "season": int(season),
                "scrape_date": scrape,
                "retrieved_as_of": capture["retrieved_as_of"],
                "page": page,
                "stored_week": sorted({w for w, _ in labels},
                                      key=lambda w: -1 if w is None else w),
                "stored_basis": sorted({b for _, b in labels}),
                "content_week": verdict["week"],
                "matched": verdict["matched"],
                "considered": verdict["considered"],
                "scope": verdict["scope"],
            })
    return out


def format_label_audit(mismatches, *, season: int, repaired: int | None = None) -> str:
    """The ``ziggurat ingest fp-weekly-labels`` report (Rule 3: logic lives here)."""
    lines = [f"fp_weekly_ecr week labels — season {season}"]
    if not mismatches:
        lines.append("  every stored capture whose opponents decide a week carries that week.")
        return "\n".join(lines)
    lines.append(
        f"  {len(mismatches)} stored page capture(s) are labelled a week their own "
        "opponents contradict:")
    lines.append("  SCRAPE      RETRIEVED   PAGE     STORED        ROWS SAY   MATCHED")
    for m in mismatches:
        stored = ",".join("?" if w is None else str(w) for w in m["stored_week"])
        basis = ",".join(m["stored_basis"])
        lines.append(
            f"  {m['scrape_date']:<11} {m['retrieved_as_of']:<11} {m['page']:<8} "
            f"{stored + ' (' + basis + ')':<13} week {m['content_week']:<4} "
            f"{m['matched']}/{m['considered']}"
            + (" (whole file)" if m["scope"] == "file" else ""))
    if repaired is None:
        lines.append(
            "  Nothing changed. `--repair` rewrites ONLY nfl_week/week_basis on exactly "
            "these captures, in place, in one transaction. Back up db/ziggurat.sqlite first.")
    else:
        lines.append(f"  REPAIRED: {repaired} row(s) relabelled in place (week_basis = "
                     f"{CONTENT_BASIS!r}). No market value was touched. Frozen decision "
                     "archives keep the OLD label — that is by design, not corruption.")
    return "\n".join(lines)


def repair_stored_week_labels(conn, mismatches) -> int:
    """Rewrite ``nfl_week`` / ``week_basis`` on the captures ``mismatches`` names.

    Takes the output of :func:`audit_stored_week_labels` so the caller can print
    what will change before anything does. ONE transaction. Touches only the two
    label columns, and only on the exact ``(season, scrape_date, page,
    retrieved_as_of)`` captures listed. No market value is changed and no row is
    added or removed. Returns the number of rows rewritten.

    IN PLACE, deliberately, and not a new versioned capture. The wrong label was a
    DERIVATION bug, not an observation. A corrected row appended under a later
    ``retrieved_as_of`` would fix reads from today on, and would leave every
    historical as-of read, i.e. every backtest of the D/ST board, reading the bug.
    Take a database backup first. The CLI command that calls this says so.
    """
    changed = 0
    with conn:
        for m in mismatches:
            cursor = conn.execute(
                "UPDATE fp_weekly_ecr SET nfl_week = ?, week_basis = ? "
                "WHERE season = ? AND scrape_date = ? AND page = ? AND retrieved_as_of = ?",
                (int(m["content_week"]), CONTENT_BASIS, int(m["season"]),
                 m["scrape_date"], m["page"], m["retrieved_as_of"]),
            )
            changed += cursor.rowcount
    return changed


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


def stored_page_capture(
    conn, *, season: int, page: str, nfl_week: int | None
) -> tuple[str | None, int, int]:
    """The newest stored capture of ONE page in ONE week: ``(day, rows, valued)``.

    THE BASELINE IS SCOPED TO ``(season, page, nfl_week)`` — see the module
    docstring for why the week belongs in the scope and the whole board does not.
    ``nfl_week=None`` matches the stored NULL week (``IS``, not ``=``), which is
    the pre-opener / unlabellable cohort: those captures compare against each
    other and never against a labelled week.

    AND TO ONE ``scrape_date`` WITHIN THAT DAY, which is what makes "the stored
    capture" mean one upstream FILE. Two forced pulls on one day can land two
    different ``scrape_date``s under a single ``retrieved_as_of`` (the PK carries
    both, so neither replaces the other) — and a Tuesday morning pull plus an
    afternoon one to catch upstream's fill-in is exactly the habit this item's
    own finding invites. Counting their union would compare tomorrow's single
    file against two files added together and refuse a healthy board.
    """
    week = None if nfl_week is None else int(nfl_week)
    scope = (int(season), str(page), week)
    row = conn.execute(
        "SELECT MAX(retrieved_as_of) FROM fp_weekly_ecr "
        "WHERE season = ? AND page = ? AND nfl_week IS ?", scope,
    ).fetchone()
    day = row[0] if row else None
    if day is None:
        return None, 0, 0
    row = conn.execute(
        "SELECT MAX(scrape_date) FROM fp_weekly_ecr "
        "WHERE season = ? AND page = ? AND nfl_week IS ? AND retrieved_as_of = ?",
        (*scope, day),
    ).fetchone()
    scrape = row[0] if row else None
    counts = conn.execute(
        f"SELECT COUNT(*), COUNT({_VALUE_COLUMN}) FROM fp_weekly_ecr "
        "WHERE season = ? AND page = ? AND nfl_week IS ? AND retrieved_as_of = ? "
        "AND scrape_date = ?",
        (*scope, day, scrape),
    ).fetchone()
    return day, int(counts[0]), int(counts[1])


def page_floor_verdicts(conn, rows, *, season: int) -> tuple[set[str], list[str]]:
    """``(refused page keys, refusal sentences)`` for one season's incoming rows.

    PURE DECISION, NO WRITES. The caller drops the refused pages' rows, reports
    the sentences through :func:`base.note_refused` / :func:`base.note_run`, and
    writes what is left — so a complete page always lands, whatever its siblings
    did. The page key is ``(page, nfl_week)``: two weeks of one page in a single
    capture (which upstream does not currently do, but nothing stops it) are two
    independent decisions, not one.

    THE MEASUREMENT THIS EXISTS FOR (2026-09-15, 07:30 PT, the first in-season
    Tuesday): the live week-2 board carried ``dst`` 32/32, ``k`` 33, and skill
    pages at roughly half their eventual size. The pre-3.14a whole-board floor
    compared 356 incoming rows against a 683-row week-ONE capture and refused
    every page — including the complete ``dst`` page that item 3.14 had made the
    primary ``ziggurat stream`` D/ST ranker hours earlier. Two separate faults:
    the unit was the board, and the baseline crossed a week boundary whose key
    space is disjoint (module docstring).
    """
    by_page: dict[tuple[str, int | None], list[dict]] = {}
    for row in rows:
        week = row.get("nfl_week")
        by_page.setdefault(
            (str(row["page"]), None if week is None else int(week)), []
        ).append(row)

    refused: set[str] = set()
    sentences: list[str] = []

    for (page, week), page_rows in sorted(
        by_page.items(), key=lambda kv: (kv[0][0], -1 if kv[0][1] is None else kv[0][1])
    ):
        label = f"week {week}" if week is not None else "an unlabelled week"
        now_rows = len(page_rows)
        now_valued = sum(1 for r in page_rows if r[_VALUE_COLUMN] is not None)

        if now_valued == 0:
            # THE ABSOLUTE ARM, checked even with no baseline: rows present,
            # consensus empty. This is the shape `players.CrosswalkCollapse` was
            # written for, and it is the one a row count cannot see.
            refused.add(_page_key(page, week))
            sentences.append(
                f"page {page!r} ({label}): {now_rows} row(s) and NOT ONE carries an "
                f"{_VALUE_COLUMN} — the consensus is this source's entire reason to "
                "exist. Empty values do not have to delete anything to hide good ones: "
                "select_as_of resolves the NEWEST retrieved version per key, so merely "
                "arriving later is enough. Page refused."
            )
            continue

        day, was_rows, was_valued = stored_page_capture(
            conn, season=season, page=page, nfl_week=week
        )
        if day is None:
            continue                      # nothing of this page/week to shadow

        floor = int(was_rows * _MIN_BOARD_FRACTION)
        if now_rows < floor:
            refused.add(_page_key(page, week))
            sentences.append(
                f"page {page!r} ({label}): {now_rows} row(s) incoming against {was_rows} "
                f"in the same page+week captured on {day} (floor {floor} = "
                f"{_MIN_BOARD_FRACTION:.0%}, which already allows for a six-team bye "
                "week). A truncated re-scrape of a week already stored shadows it on "
                "every key it shares. Page refused; the other pages are unaffected."
            )
            continue

        was_share = was_valued / was_rows if was_rows else 0.0
        now_share = now_valued / now_rows
        if was_share and now_share < was_share * _MIN_BOARD_FRACTION:
            refused.add(_page_key(page, week))
            sentences.append(
                f"page {page!r} ({label}): {now_share:.1%} of its rows carry an "
                f"{_VALUE_COLUMN}, against {was_share:.1%} on {day} (floor "
                f"{_MIN_BOARD_FRACTION:.0%} of that). The row COUNT is fine, which is "
                "exactly why this arm exists separately: a page whose values were "
                "emptied upstream shadows a good one on every key it shares. Page "
                "refused."
            )

    sentences.extend(_vanished_page_sentences(conn, by_page, season=season))
    return refused, sentences


def _page_key(page: str, week: int | None) -> str:
    """The identity a refusal is recorded under — one page in one week."""
    return f"{page}@{'?' if week is None else week}"


def _vanished_page_sentences(conn, by_page, *, season: int) -> list[str]:
    """Pages the stored week HAD that this capture does not carry at all.

    Not a refusal in the literal sense — there is nothing to write and nothing to
    shadow — but "upstream stopped publishing ``dst``" is exactly the fault the
    old per-page arm existed to surface, and it is invisible in every count this
    capture produces. Reported so the run goes ``partial`` and names it.

    Scoped to the weeks this capture actually carries: a capture of week 3 says
    nothing about whether week 2's pages still exist, and week 2's stored rows
    are not at risk from it either way.

    AND TO THE PREVIOUS CAPTURE, NOT TO EVERY CAPTURE OF THE WEEK — so this is a
    TRANSITION, not a standing state (the item-3.8A latching lesson). Comparing
    against the union of the week would re-print the same headline every day for
    the rest of the week once a page stopped being published, which is how a
    report earns being skimmed.
    """
    weeks = {week for _, week in by_page}
    present = set(by_page)
    out: list[str] = []
    for week in sorted(weeks, key=lambda w: -1 if w is None else w):
        bound = (int(season), None if week is None else int(week))
        day_row = conn.execute(
            "SELECT MAX(retrieved_as_of) FROM fp_weekly_ecr "
            "WHERE season = ? AND nfl_week IS ?", bound,
        ).fetchone()
        day = day_row[0] if day_row else None
        if day is None:
            continue
        scrape_row = conn.execute(
            "SELECT MAX(scrape_date) FROM fp_weekly_ecr "
            "WHERE season = ? AND nfl_week IS ? AND retrieved_as_of = ?", (*bound, day),
        ).fetchone()
        stored = {
            str(r[0]) for r in conn.execute(
                "SELECT DISTINCT page FROM fp_weekly_ecr WHERE season = ? AND "
                "nfl_week IS ? AND retrieved_as_of = ? AND scrape_date = ?",
                (*bound, day, scrape_row[0] if scrape_row else None),
            )
        }
        label = f"week {week}" if week is not None else "an unlabelled week"
        for page in sorted(stored - {p for p, w in present if w == week}):
            out.append(
                f"page {page!r} ({label}): present in the capture stored on {day} and "
                "ABSENT from this one — upstream published no rows for it at all. "
                "Nothing to write and nothing shadowed, but a page that stops being "
                "published is the fault this arm exists to surface."
            )
    return out


# ------------------------------------------------------------------ ingest


def ingest_fp_weekly(conn, df, *, retrieved_as_of: str, page_week=None) -> int:
    """Persist one weekly board, stamping ``knowable_as_of`` with its scrape date.

    THE WEEK LABEL, in precedence order (2026-09-30):

    1. **The rows' own opponents** (:func:`infer_week_from_opponents`, per page,
       ``week_basis = 'opponents'``) whenever they are decisive. This beats both
       of the other authorities, because both describe the PULL and only this one
       describes the FILE (see ``_CONTENT_MIN_ROWS`` for the measurement).
    2. ``page_week``, the optional ``(week, last_updated_ts)`` from the
       FantasyPros page authority (operator decision D2(b), DEFAULT OFF — see
       :func:`week_page_enabled`), recorded as ``'fantasypros_page'``.
    3. The schedule-derived week, which may answer ``'unknown'``.

    When (1) overrides (2) or (3), the run log names the override.

    The write is ONE transaction: a failure part-way rolls the whole capture back
    rather than leaving a half-written board that a later read would resolve
    against (item 3.1b — "a failed source's partial rows must not ride the next
    source's commit").
    """
    base.require_columns(df, _REQUIRED, source="fp_weekly_ecr")

    # ``reindex`` (not ``df[...]``) so an OPTIONAL column the file lacks arrives
    # as NaN and is stored NULL instead of raising KeyError on selection.
    frame = df.reindex(columns=list(dict.fromkeys(_COLMAP.values())))
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

    # THE CONTENT AUTHORITY, BEFORE THE FENCE. The floor compares a page against
    # the stored capture OF THE SAME WEEK, so the week has to be right before
    # that comparison means anything. Measured 2026-09-30: the genuine week-4
    # TE and WR pages were refused against a week-3 file stored as week 4.
    content_notes: dict[tuple[int, str, int], list[str]] = {}
    override_notes: dict[tuple[int, str, int], set[tuple[str, int | None]]] = {}
    undecided: dict[tuple[int, str], list[str]] = {}
    blind: dict[tuple[int, str], list[str]] = {}
    for (season, scrape, page), verdict in sorted(_content_labels(conn, kept).items()):
        page_rows = [r for r in kept if int(r["season"]) == season
                     and r["scrape_date"] == scrape and r["page"] == page]
        if verdict["week"] is None:
            if verdict["considered"]:
                undecided.setdefault((season, scrape), []).append(
                    f"{page} (best week {verdict['best']}: "
                    f"{verdict['matched']}/{verdict['considered']}; next week "
                    f"{verdict['runner_up']}: "
                    f"{verdict['runner_up_matched']}/{verdict['considered']})")
            else:
                blind.setdefault((season, scrape), []).append(page)
            continue
        week = int(verdict["week"])
        for row in page_rows:
            if row["nfl_week"] != week:
                override_notes.setdefault((season, scrape, week), set()).add(
                    (str(row["week_basis"]), row["nfl_week"]))
            row["nfl_week"] = week
            row["week_basis"] = CONTENT_BASIS
        content_notes.setdefault((season, scrape, week), []).append(
            _content_phrase(page, verdict))

    # THE FENCE, PER PAGE, BEFORE THE WRITE. A refused page's rows are removed
    # from the batch and everything else is written — so a complete page is never
    # thrown away because a sibling page is thin (item 3.14a; see
    # :func:`page_floor_verdicts`). The refusals ride `base.note_refused`, which
    # is OFF `run_ingest`'s drop ceiling and makes the run `partial`, and are
    # named on a `note_run` line so `ingest status` can print WHICH pages.
    refused_keys: set[str] = set()
    refusal_notes: list[str] = []
    for season in sorted({int(row["season"]) for row in kept}):
        keys, sentences = page_floor_verdicts(
            conn, [r for r in kept if int(r["season"]) == season], season=season
        )
        refused_keys |= keys
        refusal_notes.extend(f"season {season}: {s}" for s in sentences)

    survivors = [
        row for row in kept
        if _page_key(str(row["page"]), row.get("nfl_week")) not in refused_keys
    ]
    if refusal_notes:
        # One sentence per refused page key, then one per page that is stored for
        # this week and absent from the capture entirely — so the two counts
        # partition `refusal_notes` and neither hides inside the other.
        absent = len(refusal_notes) - len(refused_keys)
        base.note_refused(
            "fp_weekly_ecr", len(kept) - len(survivors), len(kept),
            why="; ".join(refusal_notes),
        )
        # FIRST, deliberately: `ingest status` truncates the joined note at 220
        # chars and labels it off its opening word, so a long week-label note in
        # front of this one would hide the only line naming the refused pages.
        base.note_run(
            "fp_weekly_ecr",
            f"REFUSED {len(refused_keys)} page(s) at the capture floor"
            + (f" + {absent} absent from the capture" if absent else "")
            + f"; wrote {len(survivors)} of {len(kept)} row(s) — "
            + "; ".join(refusal_notes),
        )

    if not survivors:
        # Every page refused. "Wrote 0 rows" is never ok (item 3.1b), and there is
        # nothing to write, so the whole capture fails LOUDLY with each page's
        # own sentence rather than returning a healthy-looking zero.
        raise WeeklyEcrCollapse(
            f"fp_weekly_ecr: NO PAGE survived the capture floor — all {len(kept)} row(s) "
            "refused, so there is nothing to write. "
            + " | ".join(refusal_notes)
        )

    unresolved = sum(
        1 for row in survivors if row["gsis_id"] is None and row["position"] != "DST"
    )
    base.note_incomplete(
        "fp_weekly_ecr", unresolved, len(survivors),
        why="unresolved FantasyPros crosswalk id (kept, NULL gsis_id)",
    )
    for (season, scrape, week), phrases in sorted(content_notes.items()):
        overridden = sorted(override_notes.get((season, scrape, week), set()),
                            key=lambda item: (item[0], -1 if item[1] is None else item[1]))
        # The OVERRIDE leads the sentence: `ingest status` truncates a note at 220
        # characters, and the override is the part a reader must not lose.
        head = (f"season {season} week label from {CONTENT_BASIS!r} OVERRODE "
                + "; ".join(_override_cause(basis, old, week) for basis, old in overridden)
                if overridden else
                f"season {season} week label from {CONTENT_BASIS!r}")
        base.note_run(
            "fp_weekly_ecr",
            f"{head}: the {scrape} file ranks week {week}. Rows whose (team, opponent) "
            f"is a week-{week} game: " + ", ".join(phrases),
        )
    for (season, scrape), phrases in sorted(undecided.items()):
        base.note_run(
            "fp_weekly_ecr",
            f"season {season}: the {scrape} file's opponents did NOT decide the week for "
            + ", ".join(phrases)
            + f" (deciding needs {_CONTENT_MIN_ROWS}+ recognisable rows, "
            f"{_CONTENT_MIN_SHARE:.0%} of them games of one week, AND no other week "
            f"above {_CONTENT_MAX_RUNNER_UP:.0%}); the pull-time label stands for those pages",
        )
    for (season, scrape), pages_blind in sorted(blind.items()):
        # SILENCE IS NOT A VERDICT. With no recognisable (team, opponent) pair the
        # content authority cannot speak — no schedule ingested, or FantasyPros'
        # team codes drifted past `TEAM_ALIASES` — and the guarantee that one file
        # carries one week on every pull lapses for these pages. Said out loud.
        base.note_run(
            "fp_weekly_ecr",
            f"season {season}: no row of the {scrape} file's "
            + ", ".join(sorted(pages_blind))
            + " page(s) names a (team, opponent) game on the stored schedule (no schedule "
            "ingested, or the team codes changed), so the rows could not check the week "
            "label; the pull-time label stands and a re-pull CAN relabel these pages",
        )
    still_provisional = {(int(r["season"]), r["week_basis"]) for r in kept
                         if r["week_basis"] != CONTENT_BASIS}
    for (season, basis), why in sorted(week_notes.items()):
        if (season, basis) not in still_provisional:
            continue
        base.note_run(
            "fp_weekly_ecr",
            f"season {season} week label from {basis!r}: {why}",
        )

    with conn:
        return base.upsert(conn, "fp_weekly_ecr", survivors, key_cols=_PK_COLS, commit=False)


def pull_fp_weekly(conn, *, retrieved_as_of: str, season: int, environ=None) -> int:
    """Fetch today's weekly board and store it.

    ``season`` is used only to sanity-check the OPTIONAL page authority (a cached
    January page ranking last season's week 18 is the one silent failure mode of
    an out-of-band label); the board's own ``scrape_date`` decides every row's
    season, exactly as ``adp_rankings`` lets the scrape decide.
    """
    page_week = None
    if week_page_enabled(environ):
        page_week = resolve_page_week(season=int(season), fetcher=fetch_week_page)
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
