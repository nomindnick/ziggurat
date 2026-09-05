"""ffverse ``ff_opportunity`` expected-points weekly panel (migration 015).

Item 4.2b, amendment (a) — S10 capture. **Capture only.** Nothing reads this
table in Week 1: no waiver column, no candidate signal, no briefing line, and
``tests/test_nfl_ff_opportunity.py`` asserts that no module under
``ziggurat/core/`` imports this one. The instrument that would use it is 4.2c's
to define; the reason to start capturing NOW is that the observations are
perishable and the season has begun.

WHY IT IS PERISHABLE, WHICH IS THE WHOLE DESIGN.
``refresh.BACKFILL_EXCLUDED['ff_opportunity']`` carries the measurement: the
release tag is ``latest-data`` and each season's asset is REWRITTEN IN PLACE on
a game-window cron (measured 2026-09-04 — ``ep_weekly_2025.parquet`` was
rewritten 2026-09-01 and again 2026-09-04, two rewrites in three days). So this
is a MUTABLE CURRENT-VALUE source in the same class as ``espn_ranks``: what it
said on a given Tuesday exists nowhere once the file is rewritten. Three
consequences, all load-bearing:

* ``retrieved_as_of`` is IN THE PRIMARY KEY, so a rewrite VERSIONS rather than
  replaces. Diffing the versions is the point of capturing daily at all.
* ``BACKFILL_EXCLUDED`` STAYS. Once the name is in ``SOURCES`` the existing
  parametrised test covers it automatically, and ``decide()`` blocks any past
  season, not overridable by ``--force``. The table only accumulates forward.
* every capture is ALSO kept whole. :func:`pull_ff_opportunity` leaves the
  downloaded parquet in place, unmodified, as a dated lossless mirror under the
  gitignored ``data/ffopp/`` — 25 of 159 columns are stored in SQLite (963 B/row
  vs 196 B/row measured), and a column dropped from a vintage that cannot be
  re-fetched would be gone for good. 1.1 MB a day buys the column choice back.
* PAST SEASONS ARE MIRRORED, NEVER INGESTED. :func:`mirror_only` is D7's second
  half (item 4.2b audit, DC-9): it fetches one season's asset to a dated parquet
  and touches no database, so 4.2c gets ``ep_weekly_2021…2025`` as an input
  WITHOUT anyone relaxing the ``BACKFILL_EXCLUDED`` fence. It takes no
  connection, which is the fence — there is no argument to it that could write a
  row. Run it once, outside the cadence; a finished season's asset is frozen.

FETCHED BY URL, NOT THROUGH ``nflreadpy``, and that is a measurement too.
``nflreadpy.load_ff_opportunity`` validates ``season <= get_current_season()``
and raises BEFORE any download — measured 2026-09-04, ``seasons=2026`` gives
``ValueError: Season must be between 2006 and 2025``, and ``seasons=None``
silently pulls 2025 instead. It also returns no asset ``updated_at``, which the
amendment requires per pull. So the fetcher here is the ``fpecr`` one (a
``.part`` file, a per-read timeout AND a whole-transfer budget,
``sweep_stale_parts``) pointed at the GitHub release API.

RULE 2 — ``total_fantasy_points`` and ``total_fantasy_points_exp`` are
**ffverse's own full-PPR scoring**, not this league's. Measured: 1.0/reception
on 2,711 pure-receiving rows, 0.1/yd, 6/TD; no distance kicker, no dual D/ST
brackets, and neither K nor D/ST appears in this feed at all. They are stored
because the regression signal is the DIFFERENCE between them and both halves
must be in one currency. House points come from ``ziggurat/core/scoring.py``
and nowhere else.

WHAT THIS SOURCE CANNOT DO (measured on 2025 wk1, 313 joined rows): its raw
usage columns are byte-equal to the ones item 3.3 already reads —
``rec_attempt`` == ``weekly_stats.targets`` (max abs err 0.000000),
``rush_attempt`` == ``carries``, ``rec_air_yards/rec_air_yards_team`` ==
``air_yards_share``. It cannot improve the usage arm. It can only add an
expected-value arm.
"""

import contextlib
import glob
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ziggurat import net
from ziggurat.data.nfl import base

logger = logging.getLogger("ziggurat.data.nfl.ff_opportunity")

#: The release every asset hangs off. ffverse re-uploads into this ONE tag
#: forever (``ep_update.R``: ``gh_cli_release_upload(tag = "latest-data",
#: overwrite = TRUE)``), which is exactly why the source is perishable.
RELEASE_REPO = "ffverse/ffopportunity"
RELEASE_TAG = "latest-data"
RELEASE_API_URL = f"https://api.github.com/repos/{RELEASE_REPO}/releases/tags/{RELEASE_TAG}"

#: The release's two metadata assets, written by the same publisher run that
#: writes the parquet: the build clock and the model version.
TIMESTAMP_ASSET = "timestamp.txt"
VERSION_ASSET = "version.txt"

#: THE PIN. ``version.txt`` on the release, as measured 2026-09-04, and the
#: version every ``_exp`` column stored so far was produced by. A LABELLED
#: HYPOTHESIS about stability, not a fact about the future: ffverse's models are
#: a pinned 2006-2020 fit rather than a per-season refit, so this has not moved,
#: but a bump would silently redefine every expected column mid-season. The
#: ingester REFUSES to write when upstream disagrees with this literal — see
#: :class:`ModelVersionChanged` for what to do about it.
EXPECTED_MODEL_VERSION = "v1.0.0"

#: A GitHub API that answers with no User-Agent at all returns 403. Named after
#: the project so a rate-limit conversation has something to point at.
_USER_AGENT = "ziggurat/4.2b"

#: WALL-CLOCK BUDGET for one asset transfer (the ``fpecr`` precedent, item 4.1
#: audit OPS-3). ``net.HTTP_TIMEOUT`` bounds each socket READ, not the transfer:
#: an upstream that trickles bytes just under the timeout holds the daily unit
#: for as long as it likes. The parquet is ~1.1 MB and lands in well under a
#: second; 300 s is orders of magnitude more headroom than that and still inside
#: the unit's wall-clock cap. Exceeding it raises ``TimeoutError`` naming the
#: elapsed seconds and bytes, and the ``.part`` is removed.
FETCH_BUDGET_S = 300.0

#: THE CAPTURE FLOOR (see :class:`OpportunityCollapse`). One fraction, applied
#: three ways: total keys, per-week rows, and the share of rows carrying a
#: non-NULL expected-points value. A LABELLED HYPOTHESIS with an argument rather
#: than a measurement behind it — there is no revision-rate history yet (that is
#: recon UNKNOWN 8, to reassess after three live weeks). The argument: within one
#: season this file is CUMULATIVE — rows are added as games are played and a
#: played game's rows do not go away — so a healthy capture is never materially
#: smaller than the one before it. 0.90 leaves room for a model re-run to drop a
#: handful of unattributed rows and still refuses a half-published file.
_MIN_CAPTURE_FRACTION = 0.90

#: Source columns required. A release that drops one fails loudly rather than
#: storing partial rows (the item-1.4 contract). These are exactly the 25 stored
#: columns; the other 134 upstream columns are not required because nothing here
#: reads them — the lossless mirror is what preserves them.
_REQUIRED = (
    "season", "week", "game_id", "player_id", "posteam", "full_name", "position",
    "pass_attempt", "rec_attempt", "rush_attempt", "rec_air_yards",
    "receptions_exp", "rec_yards_gained_exp", "rush_yards_gained_exp",
    "rec_touchdown_exp", "rush_touchdown_exp", "pass_touchdown_exp",
    "total_touchdown_exp", "total_yards_gained_exp", "total_fantasy_points_exp",
    "total_fantasy_points", "total_fantasy_points_diff",
    "rec_attempt_team", "rush_attempt_team", "pass_attempt_team",
)

#: db_column -> source_column. Identity throughout: upstream's names are kept so
#: a reader can hold the stored row and the mirrored parquet in one head.
_COLMAP = {name: name for name in _REQUIRED}

#: The stored PRIMARY KEY (migration 015). Passed to ``base.upsert`` so its
#: return value is DISTINCT KEYS WRITTEN and so ``base.upsert`` validates this
#: tuple against the key SQLite actually enforces on every ingest.
_PK_COLS = ("player_id", "season", "week", "retrieved_as_of")

#: The column whose emptiness is the ``players.CrosswalkCollapse`` signature
#: here: the expected-points value is this source's entire reason to exist, and
#: a capture full of rows with nothing in it shadows a good capture without
#: deleting anything.
_VALUE_COLUMN = "total_fantasy_points_exp"


class OpportunityCollapse(RuntimeError):
    """A degraded capture would have SHADOWED a good one. Checked BEFORE the write.

    APPEND-ONLY IS NOT A FLOOR (the ``players.CrosswalkCollapse`` lesson,
    measured live in item 3.1b): ``base.select_as_of`` resolves the newest
    ``retrieved_as_of`` PER KEY, so a capture that merely arrives later wins for
    every key it contains — even if its values are empty. Nothing has to be
    deleted for the good rows to become unreadable.

    Three shapes are refused, all against the NEWEST STORED CAPTURE of the same
    season, all before anything is written:

    * fewer than ``_MIN_CAPTURE_FRACTION`` of its rows (a truncated or
      half-published file). Rows, not distinct keys: ``(player_id, season, week)``
      is unique in the shipped file (measured, 0 duplicates in 2025), and the
      incoming side is counted BEFORE ``base.upsert`` dedupes — so an upstream
      that started shipping duplicates would make this floor slightly LENIENT
      rather than slightly strict, and ``note_collapsed`` is what reports that;
    * a week it held that this capture has lost, or shrunk below the same
      fraction (the file is cumulative within a season, so a week never
      legitimately goes backwards);
    * a materially smaller share of rows carrying an expected-points value (the
      emptied-values shape, which no row count can see).

    On the FIRST capture of a season there is nothing to compare against, so only
    the absolute case is checked: a capture in which the expected-points column
    is entirely NULL is refused whatever else it holds.
    """


class ModelVersionChanged(RuntimeError):
    """Upstream's ``version.txt`` no longer matches what this ingester expects.

    Refusing is the point (the amendment's own words: "a future ffverse model
    bump cannot silently redefine a column mid-season"). Every ``_exp`` column in
    this table is a MODEL OUTPUT; a bumped model can change what those numbers
    mean without changing a single column name, and a mixed table with no marker
    is the kind of defect that is invisible for a season and then invalidates
    everything computed off it.

    THE COST IS REAL AND IS ACCEPTED DELIBERATELY: this source is perishable, so
    every day the refusal stands is a lost observation, and ``ingest status``
    will show a standing failure until a human acts. That is the correct trade
    only because the alternative is a silent redefinition. The remedy is a
    reviewed one-line code change — read the ffopportunity release notes, decide
    whether the stored history is still comparable, then update
    ``EXPECTED_MODEL_VERSION`` in this module (and record the bump in the plan,
    so a later reader knows which rows came from which model).
    """


# ------------------------------------------------------------------ helpers


def asset_name(season: int) -> str:
    """The weekly asset this ingester reads, for one season."""
    return f"ep_weekly_{int(season)}.parquet"


@dataclass(frozen=True)
class ReleaseAsset:
    """One asset on the ``latest-data`` release, as the GitHub API describes it."""

    name: str
    url: str
    updated_at: str | None
    size: int | None


def _absent(url: str, message: str) -> urllib.error.HTTPError:
    """A 404 shaped so ``refresh._is_upstream_absent`` recognises it.

    Not decoration: ``run_ingest`` maps a 404 to ``STATUS_ABSENT``, which is NOT
    an anchor, so the source retries tomorrow instead of standing as a failure.
    That is exactly the right behaviour for the pre-registered Week-1 shape —
    ``ep_weekly_2026.parquet`` did not exist on 2026-09-04 and is not expected
    before the first games are played. ``run_ingest`` additionally refuses to
    downgrade a 404 that arrives AFTER this source has already succeeded for the
    season, so a renamed or withdrawn release still fails loudly.
    """
    return urllib.error.HTTPError(url, 404, message, {}, None)  # type: ignore[arg-type]


def _open(url: str, *, accept: str | None = None):
    if not url:
        raise ValueError(
            "ff_opportunity: the release document names an asset with no download "
            "URL — upstream schema drift, not a missing file. Refusing rather than "
            "requesting an empty address."
        )
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if accept:
        request.add_header("Accept", accept)
    return urllib.request.urlopen(request, timeout=net.HTTP_TIMEOUT)  # noqa: S310


def _read_bounded(response, *, url: str, budget_s: float, max_bytes: int) -> bytes:
    """Read a whole small response under BOTH a time budget and a size cap.

    The same reasoning as ``fetch_asset``'s budget, applied to the two metadata
    reads: ``net.HTTP_TIMEOUT`` bounds each socket READ, not the transfer, so a
    trickling GitHub API would hold the daily unit for as long as it liked and the
    sources behind this one in the registry would never run. The size cap is the
    other half — a 330 KB document that answers with gigabytes is not a document
    this function should try to hold in memory to find that out.
    """
    started = time.monotonic()
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = response.read(1 << 16)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        received += len(chunk)
        if received > max_bytes:
            raise ValueError(
                f"ff_opportunity: {url} returned more than {max_bytes} bytes, which is "
                "not the shape of the small metadata document this reads. Refusing."
            )
        if time.monotonic() - started > budget_s:
            raise TimeoutError(
                f"ff_opportunity: reading {url} exceeded its {budget_s:.0f} s budget "
                f"({received} bytes received) — a trickling upstream; the sources behind "
                "this one in the registry still run"
            )


#: Budget and size cap for the two small metadata reads (the release document is
#: ~330 KB for 182 assets; version.txt is 7 bytes).
_METADATA_BUDGET_S = 60.0
_MAX_METADATA_BYTES = 8 << 20


def fetch_release(*, url: str = RELEASE_API_URL) -> dict:
    """The release document (its assets and their ``updated_at``). A network seam.

    Read on EVERY pull, including one that finds the parquet mirror already on
    disk: ``asset_updated_at`` is the rewrite clock, and recording it per capture
    is what the amendment asks for. ~330 KB for 182 assets, one bounded request.

    Tests patch THIS function; nothing offline touches the network.
    """
    with _open(url, accept="application/vnd.github+json") as response:
        raw = _read_bounded(response, url=url, budget_s=_METADATA_BUDGET_S,
                            max_bytes=_MAX_METADATA_BYTES)
    return json.loads(raw.decode("utf-8"))


def release_asset(release: dict, name: str) -> ReleaseAsset:
    """Find one asset by name, or raise a 404 that reads as "not published yet"."""
    for asset in release.get("assets") or ():
        if asset.get("name") == name:
            return ReleaseAsset(
                name=name,
                url=asset.get("browser_download_url"),
                updated_at=asset.get("updated_at"),
                size=asset.get("size"),
            )
    raise _absent(
        RELEASE_API_URL,
        f"ff_opportunity: the {RELEASE_TAG} release carries no asset named {name!r} "
        f"(it has {len(release.get('assets') or ())} assets). Upstream publishes a "
        "season's file once that season's first games have been played, so before "
        "week 1 this is the expected answer and the source retries tomorrow.",
    )


def fetch_text_asset(release: dict, name: str) -> str | None:
    """Read a small text asset (``timestamp.txt`` / ``version.txt``) as a string.

    ``None`` when the release does not carry it. Provenance must never be the
    reason a capture is lost: a missing metadata asset is recorded as unknown,
    while a CHANGED ``version.txt`` still refuses (see
    :func:`_check_model_version` — an unknown version is not a changed one).
    """
    try:
        asset = release_asset(release, name)
    except urllib.error.HTTPError:
        return None
    with _open(asset.url) as response:
        raw = _read_bounded(response, url=asset.url, budget_s=_METADATA_BUDGET_S,
                            max_bytes=_MAX_METADATA_BYTES)
    return raw.decode("utf-8").strip() or None


def fetch_asset(asset: ReleaseAsset, dest, *, budget_s: float = FETCH_BUDGET_S) -> str:
    """Download one release asset to ``dest`` and return the path. THE network seam.

    Bounded twice, exactly as ``fpecr.fetch_fpecr`` is: ``net.HTTP_TIMEOUT`` on
    every socket read, and ``budget_s`` on the WHOLE transfer measured with
    ``time.monotonic`` per chunk — a per-read timeout alone cannot bound a
    trickling upstream. Written to a ``.part`` file and renamed on success, so a
    truncated download can never be mistaken for a mirror.

    THE ``.part`` IS REMOVED ON ANY FAILURE the process can see. What that cannot
    cover is ``SIGKILL`` (or systemd's ``SIGTERM`` with no handler), under which
    no ``finally`` runs; :func:`pull_ff_opportunity` sweeps stale siblings at the
    start of the next run for exactly that case.
    """
    dest = str(dest)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = f"{dest}.part"
    started = time.monotonic()
    received = 0
    try:
        with _open(asset.url) as response:
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
                            f"ff_opportunity: download of {asset.url} exceeded its "
                            f"{budget_s:.0f} s budget ({elapsed:.0f} s elapsed, {received} "
                            "bytes received) — a trickling upstream; the partial file is "
                            "removed and the sources behind this one in the registry "
                            "still run"
                        )
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(part)
        raise
    os.replace(part, dest)
    return dest


def sweep_stale_parts(path, *, older_than_s: float = FETCH_BUDGET_S, now=None) -> list[str]:
    """Remove ``*.parquet.part`` leftovers beside ``path`` from a KILLED run.

    The ``fpecr`` helper, copied rather than imported: these are two independent
    sources with independent mirror directories, and importing one ingester into
    another to share ten lines is how a schema change in one breaks the other.
    Only a partial nobody has written to for longer than the whole budget is
    touched, so a download in progress in another process is never removed.
    Nothing ever reads a ``.part``; each removal is logged.
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
                "ff_opportunity: removed stale partial download %s (last written %.0f s "
                "ago — a previous run was killed mid-transfer; nothing reads a .part, so "
                "nothing is lost)", stale, age,
            )
    return removed


def read_ff_opportunity(path):
    """Read a mirrored parquet into a pandas frame (the shape ingesters take).

    ``polars`` is already a dependency (it is what ``nflreadpy`` returns) and the
    frame is converted at one seam, exactly as ``ziggurat/data/nfl/source.py``
    and ``fpecr.read_fpecr`` do.
    """
    import polars as pl

    return pl.read_parquet(str(path)).to_pandas()


def _game_key(value) -> str | None:
    """A source ``game_id`` cell as a plain string, or ``None``.

    Upstream's column is a parquet String, but a pandas frame serves a missing
    cell as ``NaN``/``None`` depending on the backend, and ``{nan: ...}.get(nan)``
    is not a lookup anybody should have to reason about at a stamping seam.
    """
    if value is None or value != value:          # NaN is the only x != x
        return None
    text = str(value).strip()
    return text or None


def gameday_by_game_id(conn) -> dict[str, str]:
    """``game_id`` -> gameday ISO string, from the NEWEST stored schedule row.

    ``schedules`` keys on ``(game_id, retrieved_as_of)``, so one game has as many
    rows as it has been pulled; flex scheduling really does move a Sunday kickoff
    up to ~12 days out, and the newest pull is the one that knows where it landed.

    NOT ``base.game_date_map``: that is keyed ``(season, week, team)``, and this
    source states the game directly. Going through the team key would mean
    re-deriving a join upstream already made — and for a player who changed teams
    mid-week, deriving it from the wrong side.

    UNGATED, like ``base.game_date_map`` and for the same reason: this is an
    INGEST-TIME stamping helper, not a decision read. Rule 1 governs what a
    decision may see; applying an as_of gate here would make the stamp depend on
    when the schedule happened to be pulled, which is how a row ends up with no
    knowledge time at all. The gate that matters is on :func:`get_ff_opportunity`.
    """
    out: dict[str, tuple[str, str]] = {}
    for row in conn.execute(
        "SELECT game_id, gameday, retrieved_as_of FROM schedules WHERE gameday IS NOT NULL"
    ):
        game_id, gameday, retrieved = row[0], row[1], row[2]
        stored = out.get(game_id)
        if stored is None or retrieved > stored[1]:
            out[game_id] = (gameday, retrieved)
    return {game_id: gameday for game_id, (gameday, _) in out.items()}


def _stored_capture(conn, *, season: int) -> tuple[str | None, dict[int, int], int, int]:
    """The newest stored capture for ``season``: (day, rows per week, rows, valued).

    ``valued`` counts the rows of that capture carrying a non-NULL expected-points
    value — the quantity a pure row count cannot see.
    """
    row = conn.execute(
        "SELECT MAX(retrieved_as_of) FROM ffopp_weekly WHERE season = ?", (int(season),)
    ).fetchone()
    day = row[0] if row else None
    if day is None:
        return None, {}, 0, 0
    per_week = {
        int(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT week, COUNT(*) FROM ffopp_weekly WHERE season = ? AND "
            "retrieved_as_of = ? GROUP BY week", (int(season), day),
        )
    }
    valued = int(conn.execute(
        f"SELECT COUNT(*) FROM ffopp_weekly WHERE season = ? AND retrieved_as_of = ? "
        f"AND {_VALUE_COLUMN} IS NOT NULL", (int(season), day),
    ).fetchone()[0])
    return day, per_week, sum(per_week.values()), valued


def _check_capture_floor(conn, rows, *, season: int) -> None:
    """Refuse a capture that would shadow a better one. See :class:`OpportunityCollapse`."""
    incoming_weeks: dict[int, int] = {}
    for row in rows:
        incoming_weeks[int(row["week"])] = incoming_weeks.get(int(row["week"]), 0) + 1
    incoming_total = len(rows)
    incoming_valued = sum(1 for row in rows if row[_VALUE_COLUMN] is not None)

    if incoming_valued == 0:
        # THE ABSOLUTE CASE, checked even on a first capture: rows present,
        # values empty. This is the shape `players.CrosswalkCollapse` was written
        # for, and it is the one a row count cannot see.
        raise OpportunityCollapse(
            f"ff_opportunity: the incoming capture carries {incoming_total} rows for "
            f"season {season} and NOT ONE of them has a {_VALUE_COLUMN} — the expected "
            "values are this source's entire reason to exist. Rows with empty values do "
            "not have to delete anything to hide good ones: select_as_of resolves the "
            "NEWEST retrieved version per key, so merely arriving later is enough. "
            "Refusing to write; check the download and re-run."
        )

    day, stored_weeks, stored_total, stored_valued = _stored_capture(conn, season=season)
    if day is None:
        return

    floor = int(stored_total * _MIN_CAPTURE_FRACTION)
    if incoming_total < floor:
        raise OpportunityCollapse(
            f"ff_opportunity: the incoming capture carries {incoming_total} rows for "
            f"season {season} but the capture stored on {day} holds {stored_total} "
            f"(floor {floor} = {_MIN_CAPTURE_FRACTION:.0%}). Within a season this file is "
            "cumulative — a played game's rows do not go away — so a smaller capture is a "
            "truncated or half-published file, and it would shadow the stored one for "
            "every key it does contain. Refusing to write."
        )

    for week in sorted(stored_weeks):
        was, now = stored_weeks[week], incoming_weeks.get(week, 0)
        if now < int(was * _MIN_CAPTURE_FRACTION):
            raise OpportunityCollapse(
                f"ff_opportunity: week {week} of season {season} holds {was} rows in the "
                f"capture stored on {day} but only {now} in the incoming one "
                f"(floor {_MIN_CAPTURE_FRACTION:.0%}). A completed week does not shrink; "
                "this is a partial file. Refusing to write the WHOLE capture — a partial "
                "one is how a week ends up half-versioned with nothing saying so."
            )

    was_share = stored_valued / stored_total if stored_total else 0.0
    now_share = incoming_valued / incoming_total if incoming_total else 0.0
    if was_share and now_share < was_share * _MIN_CAPTURE_FRACTION:
        raise OpportunityCollapse(
            f"ff_opportunity: {now_share:.1%} of the incoming rows for season {season} "
            f"carry a {_VALUE_COLUMN}, against {was_share:.1%} in the capture stored on "
            f"{day} (floor {_MIN_CAPTURE_FRACTION:.0%} of that). The row COUNT is fine, "
            "which is exactly why this check exists separately: a capture whose values "
            "were emptied upstream shadows a good one on every key it shares. Refusing "
            "to write."
        )


def _check_model_version(conn, model_version: str | None, *, season: int) -> None:
    """Refuse a capture produced by a model this ingester has not been told about.

    TWO comparisons, because either alone has a hole:

    * against ``EXPECTED_MODEL_VERSION`` — catches a bump on the very FIRST
      capture, i.e. on a fresh database, where there is no stored history to
      disagree with;
    * against the newest stored ``model_version`` for this season — catches a
      bump on a database whose pin someone edited without meaning to, and is the
      comparison that survives a code edit.

    An UNKNOWN version (upstream dropped ``version.txt``) is not a changed one and
    does not refuse: provenance going missing must never cost a perishable
    observation. It is recorded as NULL and noted on the run.
    """
    if model_version is None:
        base.note_run(
            "ff_opportunity",
            "version.txt is absent from the release, so model_version is stored NULL for "
            f"this capture — the pin ({EXPECTED_MODEL_VERSION}) could not be checked. Not "
            "a refusal: an unknown version is not a changed one, and a perishable capture "
            "is not worth losing over missing provenance.",
        )
        return
    stored = conn.execute(
        "SELECT model_version FROM ffopp_weekly WHERE season = ? AND model_version "
        "IS NOT NULL ORDER BY retrieved_as_of DESC LIMIT 1", (int(season),),
    ).fetchone()
    stored_version = stored[0] if stored else None
    for other, label in ((EXPECTED_MODEL_VERSION, "this ingester is pinned to"),
                         # "the newest stored", not "every stored": this guard is what
                         # keeps a season to ONE version, but the message must claim
                         # only what it actually read.
                         (stored_version,
                          f"the newest stored season-{season} row was built by")):
        if other is not None and model_version != other:
            raise ModelVersionChanged(
                f"ff_opportunity: upstream's version.txt says {model_version!r}, but "
                f"{label} {other!r}. Every `_exp` column in this table is a MODEL OUTPUT, "
                "and a bumped model can change what those numbers mean without changing a "
                "single column name — so nothing is written until a human decides. Read "
                "the ffopportunity release notes, decide whether the stored history is "
                "still comparable with what upstream now publishes, then update "
                "EXPECTED_MODEL_VERSION in ziggurat/data/nfl/ff_opportunity.py and record "
                "the bump in the plan. Until then this source records a FAILED run every "
                "day, and each of those days is a lost observation — that is the cost of "
                "not silently mixing two models in one table."
            )


# ------------------------------------------------------------------ ingest


def ingest_ff_opportunity(
    conn,
    df,
    *,
    retrieved_as_of: str,
    season: int,
    asset_updated_at: str | None = None,
    source_timestamp: str | None = None,
    model_version: str | None = None,
) -> int:
    """Persist one capture, stamping ``knowable_as_of`` with each row's own gameday.

    ``season`` is REQUIRED and is checked against the frame rather than inferred
    from it: the asset is per-season and the file name is the only thing that
    says which, so a frame carrying a different season is upstream drift (the
    wrong asset downloaded, or a release that re-pointed a name) and must fail
    rather than land under the season the caller believed it was pulling.

    The write is ONE transaction: a failure part-way rolls the whole capture back
    rather than leaving a half-written vintage that a later read would resolve
    against (item 3.1b — "a failed source's partial rows must not ride the next
    source's commit").
    """
    base.require_columns(df, _REQUIRED, source="ffopp_weekly")
    season = int(season)

    frame = df[list(dict.fromkeys(_COLMAP.values()))]
    gamedays = gameday_by_game_id(conn)
    rows = base.frame_to_rows(
        frame,
        _COLMAP,
        retrieved_as_of=retrieved_as_of,
        knowable_as_of=lambda src: gamedays.get(_game_key(src.get("game_id"))),
    )

    kept: list[dict] = []
    unattributed = 0
    unstampable = 0
    for row in rows:
        row_season = int(row["season"]) if row["season"] is not None else None
        if row_season != season:
            raise OpportunityCollapse(
                f"ff_opportunity: a row in the capture requested for season {season} "
                f"carries season {row_season!r}. The asset is per-season "
                f"({asset_name(season)}), so this means the wrong file was read or "
                "upstream re-pointed the name. Refusing the whole capture rather than "
                "filing one season's numbers under another."
            )
        if row["player_id"] is None:
            # Unattributed plays (423 of 6,054 in the 2025 file): a real upstream
            # class with NULL name and position too. They can never be joined to
            # anything, so dropping them is a BY-DESIGN filter, not a failure to
            # handle the data — `run_ingest`'s drop ceiling excludes `filtered`.
            unattributed += 1
            continue
        if row["knowable_as_of"] is None:
            # No knowledge time. Reported on the DROP channel: a row we cannot
            # stamp is a failure to handle the data, and Rule 1 has no room for a
            # guessed one. Measured 2026-09-04: 0 of 6,054 rows land here.
            unstampable += 1
            continue
        row["season"] = season
        row["week"] = int(row["week"])
        row["asset_updated_at"] = asset_updated_at
        row["source_timestamp"] = source_timestamp
        row["model_version"] = model_version
        kept.append(row)

    # ONE note_drops call per CHANNEL, and each one is given the population it
    # actually examined rather than a shared denominator. `base.collect_drops`
    # SUMS `total` across calls (item 3.2c, F-H), so passing len(rows) twice
    # would report more rows than ever existed — but passing 0 to keep the sum
    # honest makes the LOG LINE read "dropped 19/0", which is worse: that string
    # is the only place either number is ever shown. `refresh.run_ingest`'s drop
    # ceiling does not read `total` at all (it computes written + lost), so the
    # sum is not load-bearing and legibility wins.
    base.note_drops(
        "ffopp_weekly", unattributed, len(rows),
        why="NULL player_id — an unattributed play, joinable to nothing",
        by_design=True,
    )
    if unstampable:
        base.note_drops(
            "ffopp_weekly", unstampable, len(rows) - unattributed,
            why="game_id resolves to no gameday in `schedules` (no knowledge time to stamp)",
        )

    if not kept:
        # "wrote 0 rows" is never ok (item 3.1b). For a file that only exists at
        # all once games have been played, an empty result after filtering means
        # the schedule join failed wholesale or the frame is not what it claims.
        raise OpportunityCollapse(
            f"ff_opportunity: no rows survived for season {season}. The frame carried "
            f"{len(rows)} rows, of which {unattributed} were unattributed plays and "
            f"{unstampable} could not be stamped from `schedules`. A capture that stores "
            "nothing is not a capture; check that this season's schedules are ingested."
        )

    _check_model_version(conn, model_version, season=season)
    _check_capture_floor(conn, kept, season=season)

    with conn:
        return base.upsert(conn, "ffopp_weekly", kept, key_cols=_PK_COLS, commit=False)


def pull_ff_opportunity(
    conn,
    *,
    retrieved_as_of: str,
    season: int,
    path,
    refresh: bool = False,
) -> int:
    """Mirror this season's asset to ``path`` (unless already there) and ingest it.

    ``path`` is REQUIRED and has no default: the mirror is bulk data that belongs
    under the gitignored top-level ``data/`` tree (Rule 5), and a module-level
    default path is how a file ends up written somewhere nobody expected.
    ``refresh=True`` re-downloads over an existing mirror — which is what a
    deliberate ``--force`` means for a source that is rewritten upstream several
    times a week.

    The release document is read on EVERY call, mirror or no mirror: the asset's
    ``updated_at`` is the rewrite clock and is stored per capture. A dated mirror
    already on disk is therefore re-ingested with today's provenance, which is
    the honest description of what happened (this system read that file today).
    """
    season = int(season)
    sweep_stale_parts(path)
    release = fetch_release()
    asset = release_asset(release, asset_name(season))
    if refresh or not os.path.exists(str(path)):
        fetch_asset(asset, path)
    model_version = fetch_text_asset(release, VERSION_ASSET)
    source_timestamp = fetch_text_asset(release, TIMESTAMP_ASSET)
    base.note_run(
        "ff_opportunity",
        f"{asset.name} updated_at={asset.updated_at} (the rewrite clock), "
        f"timestamp.txt={source_timestamp!r}, version.txt={model_version!r}; "
        f"lossless mirror at {os.path.basename(str(path))}",
    )
    return ingest_ff_opportunity(
        conn, read_ff_opportunity(path),
        retrieved_as_of=retrieved_as_of, season=season,
        asset_updated_at=asset.updated_at, source_timestamp=source_timestamp,
        model_version=model_version,
    )


def mirror_only(season: int, path, *, refresh: bool = False) -> str:
    """Fetch ONE season's asset to ``path`` and ingest NOTHING. Returns the path.

    D7's second half, which had no code and no reminder anywhere (item 4.2b
    audit, DC-9): mirror ``ep_weekly_2021…2025.parquet`` ONCE, outside the
    cadence, so item 4.2c has a lossless input WITHOUT anyone relaxing the
    ``BACKFILL_EXCLUDED`` fence that keeps past-season rows out of the live
    capture table. ``ffopp_weekly`` is a FORWARD-ONLY capture: a past season's
    file has a different model version and a different rewrite clock, and
    ``select_as_of`` would resolve it per key beside the live rows.

    DEADLINE, because this is the whole reason it is a separate entry point:
    upstream rewrites the CURRENT season's asset several times a week and
    FREEZES it once the season ends — so 2025's file is already frozen and safe,
    while 2026's is not a thing to mirror yet.

    Touches no database and takes no connection, which is the fence: there is no
    argument to this function that could write a row.
    """
    sweep_stale_parts(path)
    asset = release_asset(fetch_release(), asset_name(int(season)))
    if refresh or not os.path.exists(str(path)):
        fetch_asset(asset, path)
    return str(path)


# ------------------------------------------------------------------ read


def get_ff_opportunity(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    player_id=None,
    view: base.AsOfView = "historical",
):
    """Capture rows knowable on or before ``as_of`` (keyword-only; no implicit now).

    Every filter is optional and ANDed.

    THE VIEW MATTERS HERE THE WAY IT DOES FOR ``fpecr``. Rows are stamped
    ``knowable_as_of`` = the game's own day but ``retrieved_as_of`` = the day this
    system captured the file, which is always later. Under the safe-default
    ``historical`` view a read at the gameday itself therefore returns NOTHING —
    correctly, because we did not have the file then. Any backtest or grading
    read goes through ``base.latest_truth(get_ff_opportunity)``, which binds the
    view so it cannot be forgotten; the fact-time gate is unchanged either way,
    so a read at week 3 still cannot see week 4's opportunity.

    NOTHING IN ``ziggurat/core/`` MAY CALL THIS IN WEEK 1 (item 4.2b: capture
    only, no integration), and a test enforces the import fence.
    """
    clauses, params = [], {}
    for column, value in (("season", season), ("week", week), ("player_id", player_id)):
        if value is not None:
            clauses.append(f"t.{column} = :{column}")
            params[column] = value
    return base.select_as_of(
        conn, "ffopp_weekly", as_of=as_of,
        key_cols=["player_id", "season", "week"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )
