"""Sleeper research ownership — the weekly market-attention panel (item 4.1).

WHAT THIS IS. Sleeper's ``/players/nfl/research/{season_type}/{season}/{week}``
endpoint serves one flat map per NFL week: ``sleeper_id -> {owned, started}``,
percent-rostered and percent-started across Sleeper's whole user base, plus one
entry per team abbreviation for the team defense. For a PAST season each week is
a frozen snapshot: a since-retired running back's 2021 week-6 number is a
declining past-season value, not his live ownership, and every one of the 90
frozen 2021-2025 weeks re-read live on 2026-09-01 is byte-identical to its file
(0 diffs; ``intel/research/market-archives.md``, item 4.1 audit). That makes it
the second, independent, weekly point-in-time market proxy beside
``fpecr_panel`` — what the room was DOING, where ECR is what the room was SAYING.

WHAT IS *NOT* VERIFIED: WHEN THE CURRENT WEEK SETTLES. Measured 2026-09-01: the
CURRENT-week bucket is an alias of the LIVE board — ``regular/2026/1``,
``regular/2026/0`` and ``pre/2026/N`` answer byte-identical bodies (744 keys),
``regular/2026/2`` is populated with different values before a single game, and
week 3+ is the 4-byte ``null``. So a week's number exists and moves BEFORE and
possibly AFTER its games; Sleeper does not document when it stops moving, and
the recon note's own "Residual unknowns" section says so (formal immutability
unproven, within-week timing unknown, ~1-week resolution). The module used to
claim immutability "by construction" and that a week "is never frozen
half-formed" because its last gameday had passed — the NFL calendar, not
Sleeper's week pointer — and both sentences stated a guess as a fact. The rule
now is a LABELLED SETTLEMENT HYPOTHESIS: ``SETTLE_DAYS`` (7) — week N is
fetched only once the following week has also finished, and
:func:`completed_weeks` enforces it. The measurement that settles it is
recorded in ``intel/research/backtest-harness-4.1-design.md`` (daily fetches of
``regular/2026/1`` and ``/2`` from 2026-09-15 to 09-22, diffed against the
first capture); until then ``--force`` is the only instrument: it re-fetches a
frozen week LIVE, diffs it against the file, and writes the divergence count
into the run log (never overwriting the file).

WHY THE TABLE IS READ UNDER ``latest_truth``. The whole grid is pulled in one
sitting and every row carries the pull day as ``retrieved_as_of``, so the
safe-default ``historical`` view returns NOTHING at any past ``as_of`` — exactly
the footgun ``base.latest_truth`` exists for. Backtest code reads through
``base.latest_truth(get_sleeper_ownership)``; fact-time protection
(``knowable_as_of <= as_of``) is unchanged either way, so a week-3 read still
cannot see week 9.

KNOWLEDGE TIME — A LABELLED HYPOTHESIS, NOT A FACT. Sleeper does not document
whether a week's value is the state at the start of the week, the end of it, or
a within-week average (``intel/research/market-archives.md``, "Residual
unknowns": timing resolution ~1 week). This module stamps ``knowable_as_of`` as
the week's LAST regular-season gameday from ``schedules`` (``fpecr.week_bounds``)
— the latest moment the number could still have been forming — because the
conservative direction for a leakage gate is LATER, not earlier: a value that
was in fact fixed on Tuesday is merely known a few days late, whereas a value
stamped early that was still moving would be a leak. The settlement delay does
NOT move this stamp: retrieval time and knowledge time are different columns,
and a week fetched a week late is still knowable when it finished.
``KNOWABLE_BASIS`` names the rule on every row's provenance; a consumer that
needs tighter timing must measure it, not assume it.

WHAT A MISSED PULL COSTS: NOTHING. Past weeks persist upstream, so this source
is replayable, unlike ``projections``/``adp_rankings``/``espn_ranks`` (item 3.1b).
The local freeze under ``data/backtest/sleeper-research/`` exists because the
endpoint is UNDOCUMENTED and could be removed, not because it revises — and a
frozen file is never overwritten: a re-pull loads from disk, and ``--force``
re-VERSIONS the rows and VERIFIES the file against a live copy but still never
rewrites it. The only way to re-freeze a week is to remove its file by hand.

WHAT THE NUMBERS MEAN, and three things they do not:

* CENSORED AT 1 PERCENT. Upstream omits any player below ~1% owned, so an
  ABSENT key is "at or below 1%", never zero and never "unknown". The delta
  helper :func:`ownership_deltas` imputes the absent side at
  ``CENSOR_FLOOR_PCT`` and marks the row ``censored=True`` — it never drops it,
  because "went from below the floor to 40% owned" is the exact breakout the
  panel is for.
* ~40 PERCENT OF KEYS ARE IDP (LB/CB/S/DE/DT — Sleeper hosts IDP leagues; the
  share grows from ~20% in 2021 to ~40% by 2023). Those are filtered BY DESIGN
  through ``fpecr.LEAGUE_POSITIONS`` after aliasing (``PK -> K``), reported on
  ``base.note_drops(by_design=True)`` so the drop ceiling does not fire on them.
* THE POPULATION IS SLEEPER'S REDRAFT+DYNASTY BASE, not a 10-team ESPN office
  league: LEVELS are not our league's levels (a dynasty stash reads 60% owned
  here and is a free agent in ours). Use week-over-week DELTAS as the market
  response variable; that is what the delta helper is for.

CROSSWALK. ``sleeper_id`` resolves to ``gsis_id`` (and to a position, which the
payload does not carry) through ``players``, on the NEWEST retrieved row per
Sleeper id — nflverse's ``ff_playerids`` re-keys a rookie's placeholder gsis
(``SAR527100``) to the real one (``00-0040876``) between pulls, and the newest
row is the corrected one. Coverage measured 2026-09-01 on the probes: 459/459
(2021 wk1), 754/756 (2023 wk6), 756/757 (2025 wk17). An UNRESOLVED id is KEPT
with a NULL ``gsis_id`` and position ``UNKNOWN_POSITION`` — it could be a
rookie the crosswalk has not caught up with, and its ownership curve is still
a fact; it is reported through ``base.note_incomplete``. Team keys become
position ``DST`` with a NULL ``gsis_id`` and the team folded through
``base.TEAM_ALIASES`` (``LAR -> LA``, the schedules spelling).

DEGRADED PULLS ARE FLOORED BEFORE THEY CAN SHADOW GOOD DATA (item 3.1b's
standing lesson). ``select_as_of`` resolves the newest ``retrieved_as_of`` per
key, so a half-served grid does not need to delete anything to hide the good
rows — merely arriving later is enough. The floor is compared LIKE FOR LIKE: per
``(season, season_type)`` over the weeks being loaded, in distinct post-filter
keys on both sides, and again per week; anything below ``_MIN_GRID_FRACTION``
of what is stored raises :class:`OwnershipCollapse` before any write. Zero rows
after filtering raises. A payload that is not a map of objects, an EMPTY map
(not a published week, not a fact — never frozen, so a blank day cannot brick
the season the way a frozen ``{}`` did: item 4.1 audit, SLEEP-2), one where
more than ``_MAX_UNUSABLE_FRACTION`` of the keys carry no numeric ``owned`` in
0..100, or one where ``started`` is absent on more than
``_MAX_MISSING_STARTED_FRACTION`` of the keys, raises
:class:`OwnershipSchemaDrift` before anything is frozen or written; a key or
two below the 2% fraction is an upstream ANOMALY, not drift — dropped with a
note that reaches the run log, never stored as a zero (measured: 2022 wk17
served one key with ``started`` and no ``owned`` at all). A week upstream has
not published answers HTTP 200 with body ``null`` (never 404) and is
:class:`UpstreamAbsent`: not frozen, not stored, re-requested on the next run.
Nothing here issues a ``DELETE``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta

from ziggurat import net
from ziggurat.data.nfl import base, fpecr

logger = logging.getLogger("ziggurat.data.nfl.sleeper_ownership")

SLEEPER_RESEARCH_URL = (
    "https://api.sleeper.com/players/nfl/research/{season_type}/{season}/{week}"
)
#: Upstream's own vocabulary for the path segment.
SEASON_TYPES = ("regular", "pre", "post")
#: The frozen-file name under the cache dir, per (season_type, season, week).
FROZEN_NAME = "{season_type}-{season}-wk{week:02d}.json"

#: The declared primary key of ``sleeper_ownership`` — ``base.upsert`` refuses a
#: ``key_cols`` that is not exactly this (item 3.2c, SEAM 2b).
_PK_COLS = ("season", "season_type", "week", "sleeper_id", "retrieved_as_of")

#: Collapse floor: an incoming grid must carry at least this fraction of the
#: distinct keys already stored for the same scope. Same number as
#: ``fpecr._MIN_PANEL_FRACTION`` — the two panels are read side by side.
_MIN_GRID_FRACTION = 0.75

#: SETTLEMENT LAG — A LABELLED HYPOTHESIS (item 4.1 audit, SLEEP-1 / OPS-1).
#: Week N is fetched only once ``last_gameday + SETTLE_DAYS < retrieved_as_of``,
#: i.e. once the FOLLOWING week has also finished. Source: measured 2026-09-01
#: that the current-week bucket aliases the live board (``regular/2026/1`` ==
#: ``regular/2026/0`` == ``pre/2026/N``, byte-identical), so "the last gameday
#: has passed" — the NFL calendar — says nothing about whether Sleeper has
#: rolled its own week pointer past N. Seven days matches the ~1-week timing
#: resolution the recon note already assigned; the value frozen the Tuesday
#: after MNF versus the value Sleeper later archives is UNMEASURED (the
#: measurement task is in the 4.1 design note), and a wrong freeze is
#: permanent by design. Every 2021-2025 backfill week clears this window by
#: years, so the backfill is unaffected; nothing in-season reads this table, so
#: the delay costs no decision anything. Drop to 1 only with evidence.
SETTLE_DAYS = 7

#: Upstream omits anything at or below ~1% owned. LABELLED HYPOTHESIS (source:
#: ``intel/research/market-archives.md``; scanned 2026-09-01 across all 90
#: frozen 2021-2025 weeks: minimum ``owned`` is exactly 1.0, no ``owned`` or
#: ``started`` outside 0..100, and the SMALLEST per-week maximum ``owned`` is
#: 97.9). The floor is also the unit guard: a 0..1 rescale of a percent sits
#: INSIDE 0..100, so the range check cannot see it — but a whole week at or
#: below the floor is impossible under upstream's own omission rule, and
#: ``validate_payload`` refuses it as drift.
CENSOR_FLOOR_PCT = 1.0

#: A key whose ``owned`` is missing / null / not a number / outside 0..100
#: percent (or whose ``started`` is present and not a number in 0..100) is
#: UNUSABLE: dropped with a WARNING-level note, never stored (``owned_pct`` is
#: NOT NULL and a missing value is not a zero; a 0..1 rescale or a negative is
#: numeric but not a percent, and clamping it would invent a fact). Measured
#: ONCE in the 90-week 2021-2025 grid: 2022 wk17, sleeper id 4166 (an IDP DT)
#: arrived as ``{"started": 0.1}`` with no ``owned`` at all, and a strict check
#: refused the whole season for it. DRIFT is when the SHAPE moved — a renamed
#: field or a rescaled unit takes every key with it — so more than this fraction
#: of a week's keys unusable raises :class:`OwnershipSchemaDrift` instead. 2% of
#: a 470-890-key week is 9-17 keys; the one anomaly measured is 0.16%.
_MAX_UNUSABLE_FRACTION = 0.02

#: ``started`` is OPTIONAL upstream on a trickle of keys — measured over the 90
#: frozen 2021-2025 weeks it is absent on at most 2.2% of a week's keys (2022
#: wk16: 14 of 638) — so the 2% anomaly allowance above cannot police it
#: without refusing a real week. A renamed or dropped field takes EVERY key
#: with it, so absence on more than this fraction of a non-empty payload is
#: drift, not optional data (item 4.1 audit, SLEEP-4). LABELLED HYPOTHESIS: the
#: measured maximum is 2.2%; one half is the widest margin that still cannot be
#: cleared by a wholesale rename.
_MAX_MISSING_STARTED_FRACTION = 0.5

#: Position stored for a Sleeper id the crosswalk cannot name. Kept, not
#: dropped: an absence in ``players`` is "not resolved", never "not a player".
UNKNOWN_POSITION = "UNK"

#: What ``knowable_as_of`` means on every row. See the module docstring.
KNOWABLE_BASIS = (
    "last REG gameday of the week, from schedules (hypothesis: ~1-week timing; "
    f"fetched only after SETTLE_DAYS={SETTLE_DAYS} — current-week settlement unmeasured)"
)

#: Polite spacing between two LIVE fetches, and the bounded retry ladder on a
#: transient failure (5xx / URLError / timeout). An ABSENCE is not retried —
#: and on this route the measured absence signal is HTTP 200 with the 4-byte
#: body ``null`` (2025 wk19, 2026 wk3+, 2018 wk1, all probed 2026-09-01), never
#: a 404: Sleeper 404s nothing here. A MALFORMED key (``regular/2026/abc``,
#: ``foo/2026/1``) answers 200 with the CURRENT-week grid — unreachable today
#: because the URL is built from ints, but a silent wrong-week freeze is the
#: failure to fear if that ever changes.
REQUEST_SPACING_S = 1.0
RETRY_BACKOFF_S = (2.0, 8.0)
_USER_AGENT = "ziggurat/4.1"

#: How a frozen file can be replaced. Appended to every message that tells the
#: operator to look at the freeze, because it is the only recovery path.
_HAND_REMOVE_NOTE = (
    "`--force` alone never re-fetches a frozen week (it re-versions the rows and "
    "reports a live-vs-frozen diff); to re-freeze, remove the file by hand."
)


class OwnershipCollapse(RuntimeError):
    """An incoming grid would shadow a materially larger stored one. Not written."""


class OwnershipSchemaDrift(ValueError):
    """The payload is not the ``{id: {"owned": float, ...}}`` map this module knows."""


class UpstreamAbsent(RuntimeError):
    """Sleeper has not published that (season_type, season, week).

    THE MEASURED SIGNAL IS HTTP 200 WITH BODY ``null`` — not a 404. Probed
    2026-09-01: ``regular/2025/19``, ``regular/2026/3`` .. ``/18`` and
    ``regular/2018/1`` all answer ``200 null``; no key on this route has ever
    answered 404. The 404 branch in :func:`_open_with_retries` is kept as a
    defensive fallback only. Carries ``code = 404`` so the refresh registry's
    ``_http_status`` reads it the way it reads a raw ``HTTPError`` and logs a
    run whose EVERY week was absent ``upstream_absent`` (a week the archive has
    not published) rather than ``failed`` (our defect). One absent week among
    stored ones is handled PER WEEK inside :func:`pull_sleeper_ownership` — left
    unfrozen and unstored, named in the run's note, re-requested tomorrow — so
    it never fails the run.
    """

    code = 404


# ------------------------------------------------------------------ freeze


def frozen_path(cache_dir, *, season_type: str, season: int, week: int) -> str:
    """Where one week's raw payload is frozen under ``cache_dir``."""
    return os.path.join(
        str(cache_dir),
        FROZEN_NAME.format(season_type=season_type, season=int(season), week=int(week)),
    )


def _open_with_retries(url: str, *, sleep: Callable[[float], None]) -> bytes:
    """GET ``url`` with the bounded retry ladder. THE network seam.

    Bounded through ``net.HTTP_TIMEOUT`` (item 3.1b): an unbounded ``urlopen``
    under a systemd ``Type=oneshot`` unit parks the whole cadence on one
    black-holed connection. Retries only the transient class — a 5xx, a
    ``URLError`` (DNS, refused, reset) or a socket timeout — and only
    ``len(RETRY_BACKOFF_S)`` times. A 404 raises :class:`UpstreamAbsent` at once
    as a defensive fallback (upstream's real absence signal is ``200 null``,
    handled in :func:`fetch_research`; retrying an absence is how a 90-request
    grid becomes a 270-request one); any other 4xx raises as-is, because it is
    a request we got wrong.

    Tests patch ``urllib.request.urlopen`` on this module; nothing offline
    touches the network.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    attempts = len(RETRY_BACKOFF_S) + 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=net.HTTP_TIMEOUT) as response:  # noqa: S310
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpstreamAbsent(f"{url}: 404 — not published upstream") from exc
            if exc.code < 500 or attempt == attempts - 1:
                raise
            transient: Exception = exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            if attempt == attempts - 1:
                raise
            transient = exc
        backoff = RETRY_BACKOFF_S[attempt]
        logger.warning(
            "sleeper_ownership: transient failure on %s (%s: %s); retry %d/%d in %.0fs",
            url, type(transient).__name__, transient, attempt + 1, len(RETRY_BACKOFF_S), backoff,
        )
        sleep(backoff)
    raise AssertionError("unreachable: the retry ladder returns or raises")  # pragma: no cover


def research_url(season_type: str, season: int, week: int) -> str:
    """The one URL a (season_type, season, week) resolves to."""
    if season_type not in SEASON_TYPES:
        raise ValueError(f"season_type must be one of {SEASON_TYPES}, got {season_type!r}")
    return SLEEPER_RESEARCH_URL.format(season_type=season_type, season=int(season), week=int(week))


def fetch_research(
    season_type: str, season: int, week: int, *, sleep: Callable[[float], None] = time.sleep
) -> dict:
    """One live GET, decoded and validated. Raises :class:`UpstreamAbsent` on
    the measured absence signal (``200 null``) and, as a fallback, on 404."""
    url = research_url(season_type, season, week)
    payload = json.loads(_open_with_retries(url, sleep=sleep).decode("utf-8"))
    if payload is None:
        # Not a shape change: the 4-byte body `null` is how Sleeper says "no
        # such week yet". Refused BEFORE validation so it is never called drift,
        # and before the freeze so it is never frozen.
        raise UpstreamAbsent(
            f"{url}: 200/null — not yet published upstream; nothing frozen, "
            "re-requested on the next run"
        )
    # Validated HERE, before the freeze, so a drifted response is never frozen
    # as if it were a good week — a bad frozen file has to be removed by hand.
    validate_payload(payload, where=url)
    return payload


def freeze_week(
    cache_dir,
    *,
    season_type: str,
    season: int,
    week: int,
    fetch: Callable[..., dict] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict, bool]:
    """Return ``(payload, fetched)`` for one week, freezing it on first sight.

    A frozen file is NEVER overwritten: if it exists it is loaded and the
    network is not touched (``fetched=False``). The freeze is the archive —
    upstream is undocumented and may vanish, and the file is the proof of what
    it served on the day. Written to a ``.part`` and renamed, so a killed pull
    cannot leave a half file that a later run mistakes for a frozen week.

    NOTHING THAT IS NOT A PUBLISHED WEEK IS EVER FROZEN, whichever ``fetch``
    served it: a ``None`` (upstream's absence signal) raises
    :class:`UpstreamAbsent` and an empty or drifted map raises
    :class:`OwnershipSchemaDrift` HERE, before the ``.part`` write — not only in
    :func:`fetch_research` — so a stubbed fetch cannot freeze a blank either
    (item 4.1 audit, OPS-5). A frozen ``{}`` was the 3.2c brick shape: every
    later run loaded it from disk without a network call, raised the same
    zero-row refusal, and blocked every later week until the file was removed
    by hand.

    ``fetch=None`` resolves to :func:`fetch_research` AT CALL TIME (not as a
    default-argument binding), so a test can patch the module attribute and
    the registry wrapper — which passes no ``fetch`` — stays offline-testable.
    """
    path = frozen_path(cache_dir, season_type=season_type, season=season, week=week)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        validate_payload(payload, where=path)
        return payload, False
    payload = (fetch_research if fetch is None else fetch)(season_type, season, week, sleep=sleep)
    where = research_url(season_type, season, week)
    if payload is None:
        raise UpstreamAbsent(
            f"{where}: 200/null — not yet published upstream; nothing frozen, "
            "re-requested on the next run"
        )
    validate_payload(payload, where=where)
    os.makedirs(str(cache_dir), exist_ok=True)
    part = f"{path}.part"
    with open(part, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    os.replace(part, path)
    return payload, True


# ------------------------------------------------------------------ shape


def _is_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _is_percent(value) -> bool:
    """A number in 0..100 — the only unit this table stores. Never clamped."""
    return _is_number(value) and 0.0 <= value <= 100.0


def validate_payload(payload, *, where: str) -> frozenset[str]:
    """Raise :class:`OwnershipSchemaDrift` unless ``payload`` is the known map.

    Returns the UNUSABLE keys — those with no numeric ``owned`` in 0..100, or a
    ``started`` that is present and not a number in 0..100 — for the loader to
    drop with a note. More than ``_MAX_UNUSABLE_FRACTION`` of the keys unusable
    is drift (the shape or the unit moved), and so is any value that is not an
    object, an EMPTY map (not a published week — never a fact, never frozen),
    ``started`` absent on more than ``_MAX_MISSING_STARTED_FRACTION`` of the
    keys (a renamed field, not the trickle upstream legitimately omits), and a
    week whose every usable ``owned`` sits at or below ``CENSOR_FLOOR_PCT`` (a
    0..1 rescale is inside 0..100, so only the floor can catch it: upstream
    omits keys at or below ~1%, so a published week cannot consist of them).
    Nothing is ever clamped or rescaled.

    Runs on the LIVE response and on every frozen file (a hand-edited or
    truncated cache is not a hypothetical — see ``backtest/draft_backtest.py``'s
    kicking cache), so a pre-existing blank file raises naming its path.
    """
    if not isinstance(payload, Mapping):
        raise OwnershipSchemaDrift(
            f"{where}: expected a JSON object keyed by sleeper id / team, got "
            f"{type(payload).__name__}"
        )
    if not payload:
        raise OwnershipSchemaDrift(
            f"{where}: empty object — not a published week, not a fact; never frozen"
        )
    unusable: list[str] = []
    example = None
    no_started = 0
    max_owned = None
    for key, value in payload.items():
        if not isinstance(value, Mapping):
            raise OwnershipSchemaDrift(
                f"{where}: value for {key!r} is {type(value).__name__}, not an object"
            )
        owned = value.get("owned")
        started = value.get("started")
        if started is None:
            no_started += 1
        if _is_percent(owned) and (max_owned is None or owned > max_owned):
            max_owned = float(owned)
        if not _is_percent(owned) or (started is not None and not _is_percent(started)):
            unusable.append(str(key))
            if example is None:
                if _is_number(owned) and not _is_percent(owned):
                    example = f"{key!r} carries owned={owned!r} — outside 0..100 percent"
                elif _is_number(started) and not _is_percent(started):
                    example = f"{key!r} carries started={started!r} — outside 0..100 percent"
                else:
                    example = f"{key!r} carries owned={owned!r}, started={started!r}"
    if len(unusable) > _MAX_UNUSABLE_FRACTION * len(payload):
        raise OwnershipSchemaDrift(
            f"{where}: {len(unusable)} of {len(payload)} keys carry no numeric owned in "
            f"0..100 (or a started outside it) — above the {_MAX_UNUSABLE_FRACTION:.0%} "
            f"anomaly allowance, so the shape has moved, not one key. First: {example}"
        )
    if no_started > _MAX_MISSING_STARTED_FRACTION * len(payload):
        raise OwnershipSchemaDrift(
            f"{where}: `started` is absent on {no_started} of {len(payload)} keys "
            f"({no_started / len(payload):.0%}) — above the "
            f"{_MAX_MISSING_STARTED_FRACTION:.0%} absence allowance. Upstream omits it "
            "on at most 2.2% of a week's keys (measured over 90 frozen weeks, 2021-25), "
            "so a wholesale absence is a renamed field, not optional data"
        )
    if max_owned is not None and max_owned <= CENSOR_FLOOR_PCT:
        raise OwnershipSchemaDrift(
            f"{where}: every usable key carries owned <= {CENSOR_FLOOR_PCT} (largest "
            f"{max_owned!r}) — a 0..1 rescale, not a percent: upstream omits keys at or "
            "below the ~1% floor, so no published week can consist of them (measured: "
            "the smallest per-week maximum across 90 frozen weeks is 97.9). Never "
            "rescaled, never frozen"
        )
    return frozenset(unusable)


def _is_team_key(key: str) -> bool:
    """Team entries are the non-numeric keys (``"KC"``, ``"LAR"``)."""
    return not str(key).isdigit()


def payload_divergence(frozen: Mapping, live: Mapping) -> dict[str, int]:
    """How a live copy differs from the frozen one: ``changed / added / removed``.

    Pure. Keyed on the payload's own keys; ``changed`` counts keys present on
    both sides whose objects differ. This is the instrument behind ``--force``:
    the frozen file is the archive and is never rewritten, so a divergence is
    REPORTED, not repaired.
    """
    frozen_keys, live_keys = set(map(str, frozen)), set(map(str, live))
    changed = sum(
        1 for key in frozen_keys & live_keys
        if frozen[_key_in(frozen, key)] != live[_key_in(live, key)]
    )
    return {
        "changed": changed,
        "added": len(live_keys - frozen_keys),
        "removed": len(frozen_keys - live_keys),
    }


def _key_in(payload: Mapping, key: str):
    """The payload's own spelling of ``key`` (JSON keys are strings; a stub may not be)."""
    return key if key in payload else next(k for k in payload if str(k) == key)


# ------------------------------------------------------------------ crosswalk


def sleeper_crosswalk(conn) -> dict[str, tuple[str | None, str]]:
    """``sleeper_id -> (gsis_id, position)`` from the NEWEST ``players`` row per id.

    Read with no as-of gate, deliberately: this is an identity map, not a
    decision input, and the newest row is the corrected one (a rookie's
    placeholder gsis is re-keyed to the real one between nflverse pulls —
    measured 2026-09-01: 186 Sleeper ids carry two gsis spellings in
    ``players``, every one a placeholder followed by a real id). Ordering is a
    TOTAL order (newest ``retrieved_as_of``, then ``gsis_id``), so the map is
    the same in every process — a set-iteration tie-break here would make the
    loaded rows depend on ``PYTHONHASHSEED`` (item 3.11's ``bots.py`` lesson).
    """
    out: dict[str, tuple[str | None, str]] = {}
    for row in conn.execute(
        "SELECT sleeper_id, gsis_id, position FROM players "
        "WHERE sleeper_id IS NOT NULL "
        "ORDER BY sleeper_id, retrieved_as_of DESC, gsis_id"
    ):
        sid = str(row["sleeper_id"])
        if sid in out:
            continue
        position = str(row["position"] or "").strip().upper() or UNKNOWN_POSITION
        out[sid] = (row["gsis_id"], position)
    return out


# ------------------------------------------------------------------ rows


def completed_weeks(
    conn, season: int, *, retrieved_as_of: str, settle_days: int = SETTLE_DAYS
) -> dict[int, str]:
    """``week -> knowable_as_of`` for every REG week SETTLED before the pull day.

    "Settled" is the week's LAST gameday plus ``settle_days`` strictly before
    ``retrieved_as_of`` — with the default, week N is admitted only once week
    N+1 has also finished. That is the labelled settlement hypothesis
    (``SETTLE_DAYS``): the current-week bucket upstream aliases the live board,
    so "the last gameday has passed" is the NFL calendar's opinion, not
    Sleeper's, and a week frozen too early is frozen wrong forever. The VALUE
    is still the last gameday: knowledge time does not move with retrieval
    time. Empty when the season's schedule is not ingested — the registry's
    ``needs_schedules`` gate says so before this is reached.
    """
    stamp = base.iso_date(retrieved_as_of)
    lag = timedelta(days=int(settle_days))
    return {
        week: last
        for week, (_first, last) in fpecr.week_bounds(conn, int(season)).items()
        if (date.fromisoformat(base.iso_date(last)) + lag).isoformat() < stamp
    }


def rows_from_payload(
    payload: Mapping,
    *,
    season: int,
    season_type: str,
    week: int,
    crosswalk: Mapping[str, tuple[str | None, str]],
    knowable_as_of: str,
    retrieved_as_of: str,
    unusable: frozenset[str] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Map one week's payload to table rows. Returns ``(rows, tally)``.

    ``tally`` counts ``unusable`` (no numeric ``owned`` in 0..100 — an upstream
    anomaly, dropped, see ``_MAX_UNUSABLE_FRACTION``), ``filtered`` (IDP /
    non-league positions, by design), ``unresolved`` (kept with a NULL gsis and
    ``UNKNOWN_POSITION``), ``no_position`` (a gsis on file but no position —
    nflverse's ``XX``; kept under ``UNKNOWN_POSITION``), ``teams`` (DST rows)
    and ``no_started`` (counted on KEPT rows only, DST included, so the note's
    numerator is bounded by its denominator) so the caller can report them ONCE
    per pull rather than per week (``base.collect_drops`` sums ``total`` across
    calls — item 3.2c finding F-H). ``unusable`` is :func:`validate_payload`'s
    verdict; computed here when the caller has not already done so.
    """
    if unusable is None:
        unusable = validate_payload(payload, where=f"season {season} {season_type} week {week}")
    rows: list[dict] = []
    tally = _empty_tally()
    for key in sorted(payload, key=str):
        if str(key) in unusable:
            tally["unusable"] += 1
            continue
        value = payload[key]
        owned = float(value["owned"])
        started = value.get("started")
        started_pct = None if started is None else float(started)
        row = {
            "season": int(season),
            "season_type": season_type,
            "week": int(week),
            "sleeper_id": str(key),
            "gsis_id": None,
            "position": None,
            "team": None,
            "owned_pct": owned,
            "started_pct": started_pct,
            "retrieved_as_of": retrieved_as_of,
            "knowable_as_of": knowable_as_of,
        }
        if _is_team_key(key):
            team = str(key).strip().upper()
            row["team"] = base.TEAM_ALIASES.get(team, team)
            row["position"] = "DST"
            tally["teams"] += 1
            if started_pct is None:
                tally["no_started"] += 1
            rows.append(row)
            continue
        gsis, position = crosswalk.get(str(key), (None, UNKNOWN_POSITION))
        position = fpecr._POSITION_ALIASES.get(position, position)
        if position in ("", "XX", UNKNOWN_POSITION):
            # nflverse spells "no position on file" as XX. Not a league-position
            # verdict, so not a by-design filter: kept, disclosed as unresolved.
            position = UNKNOWN_POSITION
        if position not in fpecr.LEAGUE_POSITIONS and position != UNKNOWN_POSITION:
            tally["filtered"] += 1
            continue
        if gsis is None:
            tally["unresolved"] += 1
        elif position == UNKNOWN_POSITION:
            tally["no_position"] += 1
        if started_pct is None:
            # AFTER the position filter: a filtered IDP key without `started`
            # is not a kept row, and counting it printed "kept 10/9 rows".
            tally["no_started"] += 1
        row["gsis_id"] = gsis
        row["position"] = position
        rows.append(row)
    return rows, tally


def _empty_tally() -> dict[str, int]:
    return {"unusable": 0, "filtered": 0, "unresolved": 0, "no_position": 0,
            "teams": 0, "no_started": 0}


# ------------------------------------------------------------------ floor


def _stored_key_counts(
    conn, *, season: int, season_type: str, weeks: Iterable[int]
) -> dict[int, int]:
    """Distinct stored ``sleeper_id`` per week, across every ``retrieved_as_of``."""
    weeks = sorted({int(w) for w in weeks})
    if not weeks:
        return {}
    marks = ",".join("?" * len(weeks))
    rows = conn.execute(
        "SELECT week, COUNT(DISTINCT sleeper_id) AS n FROM sleeper_ownership "
        f"WHERE season = ? AND season_type = ? AND week IN ({marks}) GROUP BY week",
        [int(season), season_type, *weeks],
    ).fetchall()
    return {int(r["week"]): int(r["n"]) for r in rows}


def _check_grid_size(conn, rows: list[dict], *, season: int, season_type: str) -> None:
    """Refuse a grid that would shadow a materially larger stored one.

    LIKE FOR LIKE, twice. The scope is ``(season, season_type)`` restricted to
    THE WEEKS THIS RUN CARRIES (a run narrowed to week 9 is not measured
    against eighteen stored weeks), in distinct post-filter keys on both sides.
    The per-WEEK floor runs FIRST and names the week, because a single
    half-served week among eighteen is 5% of the season total and would clear a
    season-level floor while still replacing a good week with a bad one; the
    season-level floor follows as the guard against per-week rounding.
    """
    incoming: dict[int, set[str]] = {}
    for row in rows:
        incoming.setdefault(int(row["week"]), set()).add(row["sleeper_id"])
    stored = _stored_key_counts(conn, season=season, season_type=season_type, weeks=incoming)
    if not stored:
        return
    for week, previous in sorted(stored.items()):
        keys = len(incoming.get(week, ()))
        week_floor = int(previous * _MIN_GRID_FRACTION)
        if keys < week_floor:
            raise OwnershipCollapse(
                f"sleeper_ownership: week {week} of season {season} ({season_type}) "
                f"arrives with {keys} distinct keys but {previous} are stored (floor "
                f"{week_floor} = {_MIN_GRID_FRACTION:.0%}). Refusing to write the run: "
                "one shadowed week is one week of every ownership delta being wrong. "
                f"Check the frozen file for that week first. {_HAND_REMOVE_NOTE}"
            )
    total_in = sum(len(v) for v in incoming.values())
    total_stored = sum(stored.values())
    floor = int(total_stored * _MIN_GRID_FRACTION)
    if total_in < floor:
        raise OwnershipCollapse(
            f"sleeper_ownership: the incoming grid carries {total_in} distinct keys for "
            f"season {season} ({season_type}, weeks {sorted(incoming)}) but {total_stored} "
            f"are already stored for the same weeks (floor {floor} = "
            f"{_MIN_GRID_FRACTION:.0%}). A half-served grid does not have to delete "
            "anything to hide the good rows — select_as_of resolves the NEWEST "
            "retrieved version per key. Refusing to write. Check the frozen files "
            f"under the cache dir first. {_HAND_REMOVE_NOTE}"
        )


# ------------------------------------------------------------------ ingest


def ingest_sleeper_ownership(
    conn,
    payloads: Mapping[int, Mapping],
    *,
    season: int,
    season_type: str,
    retrieved_as_of: str,
    knowable_by_week: Mapping[int, str],
    paths: Mapping[int, str] | None = None,
) -> int:
    """Persist ``{week: payload}`` for one season. Returns distinct keys written.

    ``knowable_by_week`` is the week's ``knowable_as_of`` (see the module
    docstring); a week without one cannot be stamped and is refused rather than
    guessed. ``paths`` (optional, ``week -> frozen file``) lets every refusal
    name the file to inspect. Validation, the position filter, the crosswalk,
    the collapse floor and the zero-row refusal all run BEFORE the single
    transaction that writes, so a refused run leaves the table exactly as it
    found it.
    """
    if season_type not in SEASON_TYPES:
        raise ValueError(f"season_type must be one of {SEASON_TYPES}, got {season_type!r}")
    retrieved = base.iso_date(retrieved_as_of)
    crosswalk = sleeper_crosswalk(conn)
    kept: list[dict] = []
    seen = 0
    tally = _empty_tally()
    paths = paths or {}
    for week in sorted(int(w) for w in payloads):
        payload = payloads[week]
        where = paths.get(week) or f"season {season} {season_type} week {week}"
        unusable = validate_payload(payload, where=where)
        if week not in knowable_by_week:
            raise ValueError(
                f"sleeper_ownership: week {week} of season {season} has no knowable_as_of "
                "(no REG gameday in `schedules` for it) — a row with no knowledge time "
                "cannot be stored under Rule 1. Ingest the season's schedule first."
            )
        rows, week_tally = rows_from_payload(
            payload, season=season, season_type=season_type, week=week,
            crosswalk=crosswalk, knowable_as_of=base.iso_date(knowable_by_week[week]),
            retrieved_as_of=retrieved, unusable=unusable,
        )
        seen += len(payload)
        for name in tally:
            tally[name] += week_tally[name]
        if not rows:
            # "wrote 0 rows" is never ok (item 3.1b), and per WEEK: an empty
            # week among seventeen full ones is exactly the absence that is not
            # a fact — it would read as "nobody owned anyone", not "not served".
            raise OwnershipCollapse(
                f"sleeper_ownership: week {week} of season {season} ({season_type}) "
                f"carried {len(payload)} keys and none survived the position filter. "
                f"An empty week is never written; check {where} and upstream. "
                f"{_HAND_REMOVE_NOTE}"
            )
        kept.extend(rows)

    base.note_drops(
        "sleeper_ownership", tally["unusable"], seen,
        why=("no numeric `owned` in 0..100 (or a `started` outside it) — an upstream "
             "anomaly below the drift allowance; a missing value is not a zero and a "
             "wrong unit is not clamped, so the key is dropped, not stored (measured: "
             "2022 wk17 id 4166 served {started: 0.1} and no owned)"),
        by_design=False,
    )
    base.note_drops(
        "sleeper_ownership", tally["filtered"], seen,
        why=("IDP / non-league position after aliasing (LB, CB, S, DE, DT, DB, PN — "
             "Sleeper hosts IDP leagues; ~20% of keys in 2021 rising to ~40% by 2023, "
             "measured on the 2026-09-01 probes)"),
        by_design=True,
    )
    if not kept:
        raise OwnershipCollapse(
            f"sleeper_ownership: no rows for season {season} ({season_type}) — the "
            f"payloads carried {seen} keys in total and nothing survived. Refusing."
        )
    base.note_incomplete(
        "sleeper_ownership", tally["unresolved"], len(kept),
        why=("Sleeper id not in the players crosswalk (kept: NULL gsis_id, position "
             f"{UNKNOWN_POSITION})"),
    )
    base.note_incomplete(
        "sleeper_ownership", tally["no_position"], len(kept),
        why=(f"gsis on file but no position (nflverse XX; kept under {UNKNOWN_POSITION}, "
             "so a position-filtered read never sees it)"),
    )
    base.note_incomplete(
        "sleeper_ownership", tally["no_started"], len(kept),
        why="payload carries no `started` (kept, NULL started_pct)",
    )
    players_total = len(kept) - tally["teams"]
    resolved = players_total - tally["unresolved"]
    logger.info(
        "sleeper_ownership: season %s %s weeks %s — %d rows, crosswalk resolved %d/%d "
        "(%.1f%%), %d DST rows, %d IDP/non-league keys filtered by design",
        season, season_type, sorted(int(w) for w in payloads), len(kept), resolved,
        players_total, 100.0 * resolved / players_total if players_total else 0.0,
        tally["teams"], tally["filtered"],
    )

    _check_grid_size(conn, kept, season=season, season_type=season_type)
    with conn:
        return base.upsert(conn, "sleeper_ownership", kept, key_cols=list(_PK_COLS), commit=False)


def _verify_frozen_week(
    frozen: Mapping,
    *,
    season_type: str,
    season: int,
    week: int,
    path: str,
    fetch: Callable[..., dict] | None,
    sleep: Callable[[float], None],
) -> tuple[str, dict[str, int] | None]:
    """Fetch week ``week`` LIVE and diff it against the frozen payload.

    Returns ``(summary, divergence)``; ``divergence`` is None when no live copy
    could be compared (absent upstream, a network failure after the retry
    ladder, or a live shape that no longer validates — each of which is itself
    reported). The frozen file is NEVER touched. Best-effort by design: a
    verification that cannot complete must not fail a run whose rows all come
    from the freeze.
    """
    try:
        live = (fetch_research if fetch is None else fetch)(season_type, season, week, sleep=sleep)
        if live is None:
            raise UpstreamAbsent(f"{research_url(season_type, season, week)}: 200/null")
        validate_payload(live, where=f"live copy of {path}")
    except (UpstreamAbsent, OwnershipSchemaDrift, OSError) as exc:
        return (f"week {week}: no live copy to compare ({type(exc).__name__}: {exc}); "
                f"{path} stands unverified"), None
    diff = payload_divergence(frozen, live)
    if any(diff.values()):
        return (f"week {week}: {diff['changed']} of {len(frozen)} frozen keys CHANGED "
                f"upstream, {diff['added']} added, {diff['removed']} removed"), diff
    return f"week {week}: live copy identical ({len(frozen)} keys)", diff


def pull_sleeper_ownership(
    conn,
    season: int,
    *,
    retrieved_as_of: str,
    cache_dir,
    season_type: str = "regular",
    weeks: Iterable[int] | None = None,
    fetch: Callable[..., dict] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    verify_frozen: bool = False,
) -> int:
    """Freeze-and-load every settled week of one season. Returns keys written.

    ``cache_dir`` is REQUIRED and has no default: the frozen grid is third-party
    bulk data that belongs under the gitignored top-level ``data/`` tree (Rule
    5), and a module-level default path is how a file ends up written somewhere
    nobody expected. ``weeks`` narrows the run (``None`` = every week that has
    settled before ``retrieved_as_of`` — see :func:`completed_weeks` and
    ``SETTLE_DAYS``); a requested week that has not settled is refused before
    any fetch, never fetched early.

    A week upstream has not published (``200 null``) is handled PER WEEK: left
    unfrozen and unstored, named in the run's note through ``base.note_run``,
    and re-requested by the next run (``refresh.sleeper_new_weeks`` reads the
    table, so no extra plumbing is needed). Only when EVERY requested week is
    absent does the pull raise :class:`UpstreamAbsent` — a 0-row run is never
    ``ok``.

    ``verify_frozen`` (the registry passes ``--force``) gives the freeze its
    only instrument: for every week that is ALREADY frozen, fetch a live copy,
    diff it against the file, and put the divergence count into the run's note
    (loud when non-zero). The file is still never overwritten — a divergence is
    reported so the operator can decide, and the rows loaded are the frozen
    ones. This is how the ``SETTLE_DAYS`` hypothesis can ever be found wrong.
    On a fresh database with the archive already on disk ``ingest backfill``
    (which always runs with force) therefore re-fetches every frozen week live
    — ~90 spaced requests, a deliberate operator action, not the cadence.

    Live fetches are spaced ``REQUEST_SPACING_S`` apart — the sleep runs BEFORE
    every live request except the first, so a run served entirely from the
    freeze never sleeps at all. Only ``season_type="regular"`` is stamped for now:
    ``knowable_as_of`` derives from the REG schedule, and ``pre``/``post`` have
    no knowledge-time rule written down yet, so they are refused rather than
    stamped with a guess.
    """
    if season_type != "regular":
        raise ValueError(
            "sleeper_ownership: only season_type='regular' has a knowable_as_of rule "
            f"(the week's last REG gameday); {season_type!r} is not stamped yet"
        )
    finished = completed_weeks(conn, season, retrieved_as_of=retrieved_as_of)
    if not finished:
        raise ValueError(
            f"sleeper_ownership: season {season} has no REG week settled before "
            f"{retrieved_as_of} (last gameday + SETTLE_DAYS={SETTLE_DAYS} days; schedule "
            "ingested? season started?) — nothing to freeze."
        )
    if weeks is None:
        wanted = sorted(finished)
    else:
        wanted = sorted({int(w) for w in weeks})
        early = [w for w in wanted if w not in finished]
        if early:
            raise ValueError(
                f"sleeper_ownership: weeks {early} of season {season} have not settled "
                f"as of {retrieved_as_of} (a week is fetched only SETTLE_DAYS={SETTLE_DAYS} "
                "days after its last gameday — the current-week bucket upstream is the "
                "live board); a week still forming is never frozen."
            )

    payloads: dict[int, Mapping] = {}
    paths: dict[int, str] = {}
    fetched = 0
    requests = 0
    absent: list[int] = []
    verifications: list[str] = []
    diverged: list[int] = []
    for week in wanted:
        path = frozen_path(cache_dir, season_type=season_type, season=season, week=week)
        frozen = os.path.exists(path)
        if requests and (not frozen or verify_frozen):
            sleep(REQUEST_SPACING_S)
        if not frozen:
            requests += 1
        try:
            payload, live = freeze_week(
                cache_dir, season_type=season_type, season=season, week=week,
                fetch=fetch, sleep=sleep,
            )
        except UpstreamAbsent as exc:
            absent.append(week)
            logger.warning("sleeper_ownership: week %s of season %s not published upstream "
                           "(%s) — not frozen, not stored, retried next run", week, season, exc)
            continue
        fetched += int(live)
        if frozen and verify_frozen:
            requests += 1
            summary, diff = _verify_frozen_week(
                payload, season_type=season_type, season=season, week=week, path=path,
                fetch=fetch, sleep=sleep,
            )
            verifications.append(summary)
            if diff is not None and any(diff.values()):
                diverged.append(week)
        payloads[week] = payload
        paths[week] = path

    if absent and not payloads:
        raise UpstreamAbsent(
            f"sleeper_ownership: every requested REG week of season {season} "
            f"({absent}) is unpublished upstream (200/null) — nothing frozen, nothing "
            "written; re-requested on the next run"
        )
    if absent:
        base.note_run(
            "sleeper_ownership",
            f"weeks {absent} of season {season} not yet published upstream (200/null): "
            "not frozen, not stored, re-requested on the next run",
        )
    if verifications:
        head = (f"DIVERGENCE on week(s) {diverged}: the live copy no longer matches the "
                f"frozen file — the frozen rows were loaded and the file is untouched; "
                f"{_HAND_REMOVE_NOTE} "
                if diverged else "--force verified the frozen weeks against live copies — ")
        base.note_run("sleeper_ownership", head + "; ".join(verifications))
    logger.info(
        "sleeper_ownership: season %s %s — %d weeks (%d fetched live, %d from the freeze, "
        "%d absent upstream, %d verified)",
        season, season_type, len(wanted), fetched, len(payloads) - fetched, len(absent),
        len(verifications),
    )
    return ingest_sleeper_ownership(
        conn, payloads, season=season, season_type=season_type,
        retrieved_as_of=retrieved_as_of, knowable_by_week=finished, paths=paths,
    )


# ------------------------------------------------------------------ read


def get_sleeper_ownership(
    conn,
    *,
    as_of,
    season=None,
    week=None,
    gsis_id=None,
    season_type="regular",
    position=None,
    view: base.AsOfView = "historical",
):
    """Ownership rows knowable on or before ``as_of`` (keyword-only; no implicit now).

    Every filter is optional and ANDed; ``season_type`` defaults to the regular
    season because that is the only one stamped. ``position`` matches the
    STORED value — ``"K"`` not ``"PK"``, ``"DST"`` for team rows; unresolved ids
    sit under ``UNKNOWN_POSITION`` and are returned only by an unfiltered read.

    Bulk immutable history: read at a past ``as_of`` through
    ``base.latest_truth(get_sleeper_ownership)`` (module docstring).
    """
    clauses, params = [], {}
    for column, value in (
        ("season", season),
        ("week", week),
        ("gsis_id", gsis_id),
        ("season_type", season_type),
        ("position", position),
    ):
        if value is not None:
            clauses.append(f"t.{column} = :{column}")
            params[column] = value
    return base.select_as_of(
        conn, "sleeper_ownership", as_of=as_of,
        key_cols=["season", "season_type", "week", "sleeper_id"],
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


# ------------------------------------------------------------------ deltas


def ownership_deltas(
    prev_rows: Iterable[Mapping],
    cur_rows: Iterable[Mapping],
    *,
    floor_pct: float = CENSOR_FLOOR_PCT,
) -> list[dict]:
    """Week-over-week ``owned_pct`` change per key, censoring-aware. Pure.

    Keyed on ``sleeper_id`` (the panel's own identity — a NULL ``gsis_id`` must
    not fold every unresolved player into one row). A key present on only ONE
    side is IMPUTED at ``floor_pct`` on the absent side and flagged
    ``censored=True``: upstream omits players at or below ~1% owned, so absence
    means "below the floor", never zero and never unknown. Dropping those rows
    would delete precisely the breakouts (below the floor -> 40% in a week) the
    panel exists to surface; imputing 0.0 would overstate every one of them by
    a point. ``started_pct`` is carried on the same rule.

    Output rows: ``sleeper_id, gsis_id, position, team, prev_owned, cur_owned,
    delta, censored``, sorted by ``delta`` descending then ``sleeper_id``.
    """
    prev = {str(r["sleeper_id"]): r for r in prev_rows}
    cur = {str(r["sleeper_id"]): r for r in cur_rows}
    out: list[dict] = []
    for sid in prev.keys() | cur.keys():
        p, c = prev.get(sid), cur.get(sid)
        anchor = c if c is not None else p
        prev_owned = float(floor_pct) if p is None else float(p["owned_pct"])
        cur_owned = float(floor_pct) if c is None else float(c["owned_pct"])
        out.append({
            "sleeper_id": sid,
            "gsis_id": _field(anchor, "gsis_id"),
            "position": _field(anchor, "position"),
            "team": _field(anchor, "team"),
            "prev_owned": prev_owned,
            "cur_owned": cur_owned,
            "delta": cur_owned - prev_owned,
            "censored": p is None or c is None,
        })
    out.sort(key=lambda r: (-r["delta"], r["sleeper_id"]))
    return out


def _field(row, name: str):
    """``row[name]`` or None — for a ``sqlite3.Row`` (IndexError on a missing
    column, no ``.get``) and a plain mapping (KeyError) alike."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None
