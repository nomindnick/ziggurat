"""Depth charts v2 — the dated daily PANEL regime (2025 onwards), item 3.2c.

Upstream replaced the weekly depth chart with a **daily snapshot panel** keyed on
a publish timestamp ``dt``: no season column, no week column, every position
(including IDP), one full 32-team chart per publish. That is why this source sat
``blocked`` from item 3.1b — the stored weekly table could not hold it and
``base.select_as_of`` could not query it.

**Two regimes, two tables, permanently.** 2021-2024 lives in
``depth_charts_weekly`` (this module's sibling). Routing it here was measured to
be a data-fabrication bug: four of the five key columns below do not exist in the
legacy frame, and the obvious rescue (``gsis_id`` -> ``espn_id`` through the
crosswalk) resolves ~82% of rows, so ~18% would land with ``espn_id`` NULL —
which in this table MEANS "this slot was vacated". See the migration-007 header.

WHAT IS STORED, AND WHY IT IS NOT THE PANEL
-------------------------------------------
A **change log plus tombstones**, not the verbatim panel:

* ``depth_chart_slots`` — one row per "at this instant, this slot's occupant
  became X". A slot with no row at a later ``dt`` is UNCHANGED. ``espn_id IS
  NULL`` is a **tombstone**: "this slot no longer exists". Same shape as item
  3.1's positive ``on_team_id IS NULL`` drop fact.
* ``depth_chart_panels`` — one row per observed snapshot, written even when
  nothing moved (8 of 2025's 221 panels and 20 of 2026's 127 carried zero
  changes). It is the reader's "as of when" (Rule 6) and the ingest watermark.

Measured warrant, re-measured by this module against the live 2025+2026 files
(923,162 source rows) on 2026-07-25: verbatim storage is 255.4 MB on a 43.4 MB
database; this encoding is **29,483 slot rows + 348 panel rows, 6.98 MiB
including both indexes** (the recon prototype's 6.50 MB is the same table with
one index instead of two) and takes 9.4 s of CPU to build. The row count was
31,085 before the collapse floor landed: 1,602 of those rows were the fabricated
"slot vacated" events of 10 partial scrapes and the re-additions that undid them
the next day. Losslessness is not assumed — it is the two assertions §7.3.3
separates:

* **338 of 348** published panels reconstruct row-for-row at ``observed_at``
  granularity, 0 mismatches. The other **10 are deliberately NOT reconstructed**:
  they are the panel-days on which upstream's scraper served a PARTIAL chart for
  at least one club, and this module refuses to read that absence as a vacancy —
  see ``PANEL_COLLAPSE_RATIO``. The pre-floor number was 348/348, and it was
  lossless about a *lie*. The deviation on those 10 days is one-directional and
  was measured column by column: 807 EXTRA rows (the degraded clubs' previous
  listings, carried forward), **0 missing rows and 0 differing payloads** — the
  read never loses or alters a published fact, it only declines to delete one.
* **344/344** are addressable through the day-granular accessor — 344, not 348,
  because four days carry 2-3 panels and ``as_of`` is a DAY by
  ``select_as_of``'s documented contract.

Storing every position rather than skill-only costs ~4 MB and keeps future D/ST
front-personnel work possible, so nothing is filtered.

**The tombstones are load-bearing, not tidiness.** Per-key resolution over a full
panel inflates a board 58% (a KC roster showing both a QB3 and a QB4 who are the
same player); per-key resolution WITHOUT tombstones resurrects ghosts (a phantom
rank-4 carried forward seven weeks). Change-only + tombstones + per-key
resolution ordered on ``observed_at`` — ``base.select_observed_as_of`` — satisfies
both, at every one of the 344 addressable as-of points above.

**...and precisely because they are load-bearing, they must never be guessed.**
A tombstone is an ASSERTION ("this slot no longer exists") derived from an
ABSENCE, so anything that can make a row absent for a reason other than a real
vacancy fabricates a fact. Upstream's per-club scraper failing is exactly such a
reason and it is not rare: **12 club-panels across the 348 published in 2025+2026
carry a partial chart** (most recently ARI 2026-07-24, 100 slots -> 42). Without
the floor below, the LAC 2025-12-18 collapse alone writes 91 tombstones, the run
log says ``ok``, and ``qb1_change_candidates`` then announces "Justin Herbert is
now listed QB1 for LAC (previous=None)" — a confident, well-formed, fabricated
fact. See ``PANEL_COLLAPSE_RATIO``.

THIS IS NOT AN INJURY OR AVAILABILITY SIGNAL
--------------------------------------------
Measured on 2025 and stated here because a docstring that merely omits it is how
item 3.3 would assume otherwise:

* A starter ruled ``Out`` is **not** demoted. Chuba Hubbard (out wk 5-6), Marvin
  Harrison Jr. (11-12) and Rhamondre Stevenson (9, 11) all held ``pos_rank = 1``
  every single day, and their listed backups never moved.
* Systematically: of 15 rank-1 skill players with >=3 consecutive ``Out`` weeks,
  **1 (7%)** was demoted within 14 days.
* It does not track who actually plays: on the 497 team-week-positions where the
  real snap leader changed, the pre-week chart already pointed at the new leader
  **35.0%** of the time. Pre-week rank-1 led the position in snaps QB 88.4%,
  RB 77.1%, TE 67.8%, **WR 55.0%** (n=2,161).

**Injuries = availability. Depth chart = role order.** Never conflated. The
absence of a demotion carries no evidence of availability. See ``§3.7`` of the
3.2c design note and ``qb1_change_candidates`` below for the one trigger this
module ships — as a labelled hypothesis, not a validated signal.

Anatomy follows the package convention: ``ingest_*`` (frame -> rows -> upsert),
``pull_*`` (wraps the one ``nfl.import_depth_charts`` seam tests patch), and
keyword-only ``as_of`` accessors. Ships leakage tests and a reconstruction oracle.
"""

import re

from ziggurat.data.asof import nfl_season_of, normalize_as_of
from ziggurat.data.nfl import base
from ziggurat.data.nfl import source as nfl

#: First season served in the panel regime. 2021-2024 are the weekly regime and
#: belong to ``depth_charts_weekly``; the 2025 file is the first dated panel.
PANEL_MIN_SEASON = 2025

#: Columns this module reads from the upstream frame. ``pos_name`` is deliberately
#: NOT required or stored: it is the long label for ``pos_id``, which is strictly
#: 1:1 with the stored ``pos_abb`` (0 violations in 554,215 rows).
_PANEL_COLUMNS = (
    "dt", "team", "player_name", "espn_id", "gsis_id",
    "pos_grp_id", "pos_grp", "pos_id", "pos_abb", "pos_slot", "pos_rank",
)

#: Columns that only the LEGACY weekly frame carries. Their presence means the
#: caller handed the wrong regime to the wrong ingester — verified live:
#: ``load_depth_charts([2020])`` returns 36,168 x 15 with these columns and none
#: of ``_PANEL_COLUMNS``' key members.
_LEGACY_MARKER_COLUMNS = ("week", "club_code", "depth_team", "formation")

#: THE SLOT. The key is the slot; the occupant is the value — which is exactly
#: what makes "slot vacated" expressible instead of a row silently vanishing.
#: Measured on 554,215 rows: (dt, team, pos_grp_id, pos_id, pos_rank) -> 554,215
#: distinct, 0 duplicates. ``pos_grp_id`` is required, not decorative: nine
#: ``pos_abb`` values appear in BOTH base defenses and 9 teams switched scheme
#: during 2025, so (dt, team, pos_abb, pos_rank) is a latent silent collapse.
_SLOT_KEY_COLS = ("season", "team", "pos_grp_id", "pos_id", "pos_rank")

#: The stored PRIMARY KEY = the slot key + the observation instant + the revision
#: column. Passed to ``base.upsert`` so the returned count is distinct keys
#: written rather than rows offered.
_SLOT_PK_COLS = _SLOT_KEY_COLS + ("observed_at", "retrieved_as_of")
_PANEL_PK_COLS = ("season", "observed_at", "retrieved_as_of")

#: The occupant + payload carried by a slot. A change in ANY of these emits an
#: event: ``pos_slot`` and ``player_name`` are payload, but a payload-only change
#: is still a real restatement of the chart and storing it costs one row.
_PAYLOAD_COLUMNS = ("pos_abb", "pos_grp", "pos_slot", "espn_id", "gsis_id", "player_name")

#: The four position groups observed across 923,162 rows: '15' base 3-4 defense,
#: '16' base 4-3 defense, '18' special teams, '21' 3WR-1TE offense. A FIFTH is
#: reported and STORED, never fatal — the group is in the key, so the rows are
#: perfectly well-formed, and failing a whole daily run on an upstream taxonomy
#: addition is crying wolf.
KNOWN_POS_GRP_IDS = frozenset({"15", "16", "18", "21"})

#: A club's chart in this panel is DEGRADED — a partial scrape, not a roster
#: change — when it carries FEWER THAN THIS FRACTION of the slots that club
#: carried in the last panel this module trusted. Its slots are then treated
#: exactly like an unreadable row: **no tombstone is emitted**, the previous
#: state is carried forward, the panel row records one fewer authoritative club
#: (``n_teams``) and ``note_incomplete`` names the club.
#:
#: MEASURED, live, on the whole 2025 + 2026 files (348 panels, 923,162 rows,
#: 2026-07-25 — the numbers an auditor should re-run rather than trust):
#:
#: * 12 club-panels collapse. 2025: LAC 12-18 (72 -> 8). 2026: PHI 03-27 (75->18),
#:   SEA 03-27 (81->32), CAR 04-10 (78->2), CAR 05-01 (86->9), DEN 05-01 (86->16),
#:   SEA 05-24 (99->49), NO 06-11 (100->27), CHI 06-15 (99->5), TB 07-15 (98->31),
#:   IND 07-22 (99->28), ARI 07-24 (100->42). Each recovers in full the next day.
#: * **ALL 12 carry ZERO skill-position rows** (QB/RB/WR/TE) where the club's
#:   healthy panel carries 28-30, and every one of the 12 publishes exactly ONE
#:   ``pos_grp_id`` — the scraper emits one position group and drops the rest.
#: * **The separation is empirical, and it is what makes 0.50 a threshold rather
#:   than a taste**: the 12 defective ratios top out at **0.495**; the lowest
#:   LEGITIMATE ratio anywhere in either file is **0.563** (LV 2026-04-19,
#:   71 -> 40) and the annual roster-cutdown day bottoms out at **0.656**
#:   (JAX 2025-08-27, 96 -> 63). Nothing else falls between.
#: * **An ``n_teams`` floor catches NONE of them.** All 348 panels carry 32
#:   clubs; the collapse is inside a club, never a missing one. (A club that is
#:   missing ENTIRELY is caught here too — its ratio is 0.)
#: * The rows a partial panel DOES publish are trustworthy and are stored: all
#:   42 ARI / 28 IND slot keys exist in that club's previous full panel, with
#:   identical payloads on 40/42 and 28/28. Only the ABSENCE is untrustworthy.
#:
#: WHAT THIS DELIBERATELY DOES NOT DO: it does not raise. The whole file is
#: re-diffed on EVERY pull, so refusing a panel would re-refuse the same past
#: ``dt`` for ever and permanently brick the source — unlike ``espn_ranks`` and
#: the league sync, whose bad response is transient. Suppress, record, carry on.
#:
#: NOT YET EXERCISED IN SEASON: 11 of the 12 are offseason/preseason and the
#: twelfth is a December Thursday. Whether the rate rises when rosters churn is
#: unmeasured (3.2c design note §5).
PANEL_COLLAPSE_RATIO = 0.50

#: ``nflreadpy.get_current_season(roster=True)`` flips to the new year on
#: **March 15** (verified in the installed package, ``utils_date.py``), while
#: ``ziggurat.data.asof.nfl_season_of`` flips on **March 1**. Between those two
#: dates a request for the "current" season raises upstream while the live chart
#: is still publishing daily inside the PREVIOUS season's file — see
#: ``resolve_season``.
NFLREADPY_ROSTER_FLIP_DAY = 15

#: ``dt`` must be ISO-8601 Z at second granularity. Not decoration: TWO invariants
#: ride on the exact format. ``observed_at`` is ordered by STRING comparison (in
#: the key, in the accessor's MAX and in the watermark), which is only equivalent
#: to time order while every value is fixed-width UTC; and ``knowable_as_of`` is
#: ``dt[:10]``, which silently becomes garbage under any other shape. All 923,162
#: measured rows match. An upstream format change fails the run rather than
#: corrupting the knowledge gate — this is the class of drift item 3.1b's frozen
#: fixtures hid for a year.
_DT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class LegacyDepthChartFrame(ValueError):
    """A 2021-2024 weekly frame was handed to the panel ingester (or vice versa).

    Raised rather than coerced. The two regimes share neither key columns nor
    semantics, and the panel's occupant column doubles as its tombstone sentinel,
    so a "best effort" mapping fabricates vacancy facts (measured at 18% of legacy
    rows). Refuse rather than guess.
    """


class PanelTimestampFormat(ValueError):
    """Upstream's ``dt`` is no longer a fixed-width ISO-8601 Z instant.

    Raised rather than coerced, because both the ordering (string comparison) and
    the knowledge gate (``dt[:10]``) assume the shape. See ``_DT_PATTERN``.
    """


class PanelKeyCollision(ValueError):
    """Two rows in one published panel claim the same slot.

    Measured 0 times in 554,215 rows, and if it happens the diff is undefined:
    whichever row iterated last would silently become the slot's occupant. The
    run fails loudly and the day is retried tomorrow (the file carries its whole
    history, so nothing is lost by refusing).
    """


# ------------------------------------------------------------------ ingest


def _require_panel_frame(df) -> None:
    """Fail fast, and fail with the right pointer, on the wrong regime."""
    columns = set(df.columns)
    legacy = [c for c in _LEGACY_MARKER_COLUMNS if c in columns]
    if legacy and "dt" not in columns:
        raise LegacyDepthChartFrame(
            f"this is the LEGACY weekly depth-chart frame (carries {legacy}) — it "
            "belongs to ziggurat.data.nfl.depth_charts_weekly and the "
            "depth_charts_weekly table. The panel table's key columns "
            "(pos_grp_id, pos_id, pos_rank, espn_id) do not exist in it, and "
            "espn_id IS NULL is this table's TOMBSTONE sentinel, so storing it "
            "here would fabricate ~18% 'slot vacated' facts."
        )
    base.require_columns(df, _PANEL_COLUMNS, source="depth_charts")


def _panels_by_dt(df, *, source: str = "depth_charts"):
    """Group the frame into ordered published panels.

    Returns ``(dts, panels)`` where ``dts`` is every distinct ``dt`` in ascending
    order and ``panels[dt]`` is ``(occupied, unreadable, n_rows, teams)``:

    * ``occupied``  — ``{slot_key: payload}`` for every readable row
    * ``unreadable`` — slot keys present in the panel but UNREADABLE (a NULL
      ``espn_id``). They are excluded from the vacancy computation on purpose: a
      row we cannot interpret means "unknown", and treating it as "vacant" would
      emit a tombstone for a slot the panel actually published. Refuse rather
      than guess, again. (``_change_log`` adds a second, much more populated
      class of "unknown" to this set — see ``PANEL_COLLAPSE_RATIO``.)
    * ``n_rows`` / ``teams`` — the panel AS PUBLISHED, before any drop.

    Raises ``PanelKeyCollision`` on a duplicate slot inside one panel.
    """
    dts: list[str] = []
    panels: dict[str, tuple[dict, set, int, set]] = {}
    dropped = 0
    unknown_groups: dict[str, int] = {}

    order: dict[str, None] = {}
    grouped: dict[str, list] = {}
    for row in df.itertuples(index=False):
        dt = base._clean(row.dt)
        if dt not in order:
            if not isinstance(dt, str) or not _DT_PATTERN.match(dt):
                raise PanelTimestampFormat(
                    f"depth_charts: dt {dt!r} is not a fixed-width ISO-8601 Z "
                    "instant (YYYY-MM-DDTHH:MM:SSZ). observed_at is ordered by "
                    "STRING comparison and knowable_as_of is dt[:10], so any "
                    "other shape corrupts both the ordering and the knowledge gate"
                )
            order[dt] = None
        grouped.setdefault(dt, []).append(row)

    for dt in sorted(order):
        occupied: dict[tuple, tuple] = {}
        unreadable: set = set()
        rows = grouped[dt]
        teams = set()
        for row in rows:
            team = base._clean(row.team)
            teams.add(team)
            pos_grp_id = base._clean(row.pos_grp_id)
            if pos_grp_id is not None and pos_grp_id not in KNOWN_POS_GRP_IDS:
                unknown_groups[pos_grp_id] = unknown_groups.get(pos_grp_id, 0) + 1
            key = (team, pos_grp_id, base._clean(row.pos_id), base._clean(row.pos_rank))
            espn_id = base._clean(row.espn_id)
            if espn_id is None or espn_id == "":
                # A NULL occupant would be READ BACK AS A TOMBSTONE — the one
                # value this encoding cannot store as data. Measured 0 nulls and
                # 0 empty strings in 923,162 rows, so this is a guard, not a
                # code path with a population.
                dropped += 1
                unreadable.add(key)
                continue
            if key in occupied:
                raise PanelKeyCollision(
                    f"depth_charts: panel {dt} publishes slot {key} twice — the "
                    "change log cannot say which row is the occupant "
                    "(measured 0 collisions in 554,215 rows, so this is new)"
                )
            occupied[key] = (
                base._clean(row.pos_abb), base._clean(row.pos_grp),
                base._clean(row.pos_slot), espn_id,
                base._clean(row.gsis_id), base._clean(row.player_name),
            )
        dts.append(dt)
        panels[dt] = (occupied, unreadable, len(rows), teams)

    base.note_drops(
        source, dropped, len(df),
        why="NULL espn_id — the occupant column is this table's tombstone "
            "sentinel, so the row cannot be stored without fabricating a vacancy",
    )
    for group, count in sorted(unknown_groups.items()):
        base.note_incomplete(
            source, count, len(df),
            why=f"unrecognised pos_grp_id {group!r} (known: "
                f"{sorted(KNOWN_POS_GRP_IDS)}) — stored anyway; the group is IN "
                "the key so the rows are well-formed",
        )
    return dts, panels


def _change_log(df, *, season: int, retrieved_as_of: str, source: str = "depth_charts"):
    """Diff the WHOLE frame into change events + one panel row per snapshot.

    ALWAYS the whole file, never just the tail — 4.2 s for the 554,215-row 2025
    file and 2.8 s for 2026's 368,947 (3.9 s re-measured with the collapse floor
    in place). Diffing everything and then filtering by the watermark is *provably
    identical* to the backfill's output, which is what makes the daily path and
    the backfill path ONE code path rather than two that must agree. Verified on
    the real file, not argued: replaying 2025 one ``dt`` at a time (216
    incremental days) reaches a byte-identical table to a single whole-file
    ingest — 22,836 slot rows, 221 panel rows — writing a median of 48 slot rows
    a day. **Re-verified after the collapse floor landed** on the live 2026 file,
    which is the one that matters here because 9 of its 127 panel-days are
    partial scrapes: 127 incremental days -> 6,647 slot + 127 panel rows,
    byte-identical to the whole-file ingest, every column. The floor cannot fork
    the two paths because suppression is a pure function of the whole file, and
    the watermark filter runs afterwards.

    Returns ``(events, panels, degraded)`` — the first two ready for
    ``base.upsert``, in ``dt`` order; ``degraded`` is one record per club-panel
    the collapse floor suppressed, for the caller to report (see
    ``ingest_depth_charts``, which reports only the ones it is about to STORE —
    re-warning about a panel absorbed weeks ago is how the reports that matter
    get ignored).
    """
    dts, panels = _panels_by_dt(df, source=source)
    retrieved = base.iso_date(retrieved_as_of)

    state: dict[tuple, tuple | None] = {}
    events: list[dict] = []
    panel_rows: list[dict] = []
    degraded_log: list[dict] = []
    #: club -> slots it carried in the last panel this loop TRUSTED. Deliberately
    #: NOT updated from a degraded panel: two bad days in a row would otherwise
    #: compare 42 against 42, call the second one healthy, and tombstone the club.
    trusted: dict[str, int] = {}

    for dt in dts:
        occupied, unreadable, n_rows, teams = panels[dt]
        knowable = dt[:10]
        changes = 0

        published: dict[str, int] = {}
        for key in occupied:
            published[key[0]] = published.get(key[0], 0) + 1

        # THE COLLAPSE FLOOR (PANEL_COLLAPSE_RATIO). A club whose chart shrank
        # past the floor is UNKNOWN in this panel, not emptied — so its slots
        # join the unreadable ones: no tombstone, previous state preserved.
        degraded = {
            team: (before, published.get(team, 0))
            for team, before in trusted.items()
            if published.get(team, 0) < before * PANEL_COLLAPSE_RATIO
        }
        blocked = set(unreadable)
        if degraded:
            blocked |= {key for key in state if key[0] in degraded}
            refused: dict[str, int] = {}
            for key, previous in state.items():
                if previous is not None and key[0] in degraded and key not in occupied:
                    refused[key[0]] = refused.get(key[0], 0) + 1
            for team, (before, now) in sorted(degraded.items()):
                degraded_log.append({
                    "observed_at": dt, "team": team, "before": before, "now": now,
                    "tombstones_refused": refused.get(team, 0),
                })

        for key, payload in occupied.items():
            if state.get(key) == payload:
                continue
            changes += 1
            events.append(_slot_row(season, key, payload, dt, retrieved, knowable))

        # A slot that was occupied and is absent from THIS panel has been
        # vacated. That positive fact is the tombstone; without it the previous
        # occupant is carried forward for ever by per-key resolution.
        for key in state.keys() - occupied.keys() - blocked:
            previous = state[key]
            if previous is None:
                continue  # already tombstoned; a vacancy is recorded once
            changes += 1
            events.append(_slot_row(
                season, key,
                # The tombstone keeps the label columns (so a reader can see WHICH
                # slot went away) and nulls the occupant + the lineup slot.
                (previous[0], previous[1], None, None, None, None),
                dt, retrieved, knowable,
            ))

        for key, payload in occupied.items():
            state[key] = payload
        for key in state.keys() - occupied.keys() - blocked:
            state[key] = None
        for team, count in published.items():
            if team not in degraded:
                trusted[team] = count

        panel_rows.append({
            "season": season,
            "observed_at": dt,
            # n_teams is the count of clubs this panel is AUTHORITATIVE for, i.e.
            # clubs whose absence from it we are willing to read as a vacancy —
            # NOT the count of clubs that appear in it. The two differ only on the
            # 10 measured partial-scrape days, and this is the reading migration
            # 007's own column comment already declares ("32 in all 348 observed
            # panels; < 32 is a partial scrape") and that nothing read until now.
            "n_teams": len(teams - set(degraded)),
            "n_slots": n_rows,
            "n_changes": changes,
            "retrieved_as_of": retrieved,
            "knowable_as_of": knowable,
        })

    return events, panel_rows, degraded_log


def _slot_row(season, key, payload, observed_at, retrieved, knowable) -> dict:
    team, pos_grp_id, pos_id, pos_rank = key
    pos_abb, pos_grp, pos_slot, espn_id, gsis_id, player_name = payload
    return {
        "season": season, "team": team, "pos_grp_id": pos_grp_id, "pos_id": pos_id,
        "pos_rank": pos_rank, "observed_at": observed_at, "pos_abb": pos_abb,
        "pos_grp": pos_grp, "pos_slot": pos_slot, "espn_id": espn_id,
        "gsis_id": gsis_id, "player_name": player_name,
        "retrieved_as_of": retrieved, "knowable_as_of": knowable,
    }


def latest_observed_at(conn, *, season: int) -> str | None:
    """The ingest watermark: the newest ``observed_at`` stored for ``season``.

    Read from ``depth_chart_panels``, never from ``depth_chart_slots``: a quiet
    panel contributes no slot row at all, so the slot table's MAX would rewind to
    the last day something moved and the ingester would re-diff (and re-store)
    every panel since. This is operational state, not a fact about the world, so
    it deliberately does not go through an as-of view — same stance as the run
    log.

    ``season`` is coerced to ``int`` here and nowhere else in the module, because
    this is the one place a type mismatch would be both silent and expensive:
    SQLite does not match ``'2026'`` against an INTEGER column, so the watermark
    would read NULL and every morning's pull would rewrite the season's whole
    baseline under a fresh ``retrieved_as_of`` while still reporting success.
    """
    row = conn.execute(
        "SELECT MAX(observed_at) AS watermark FROM depth_chart_panels WHERE season = ?",
        (int(season),),
    ).fetchone()
    return row["watermark"] if row is not None else None


def _check_restatement(conn, *, season, events, since, source) -> None:
    """Did upstream rewrite a panel we already stored?

    The events at or below the watermark are computed anyway (we always diff the
    whole file), so comparing them to what is stored is free. Never observed —
    but "we would notice" is cheaper than "we assume", and every stored row
    carries its own ``retrieved_as_of``, so a real restatement lands as a new
    version rather than an overwrite.
    """
    replayed = {
        tuple(row[c] for c in _SLOT_KEY_COLS + ("observed_at",) + _PAYLOAD_COLUMNS)
        for row in events if row["observed_at"] <= since
    }
    columns = ", ".join(_SLOT_KEY_COLS + ("observed_at",) + _PAYLOAD_COLUMNS)
    stored = {
        tuple(row)
        for row in conn.execute(
            f"SELECT {columns} FROM depth_chart_slots "
            "WHERE season = ? AND observed_at <= ?", (season, since),
        ).fetchall()
    }
    if not stored:
        return
    # Fire only on "upstream now says something we do not hold". The other
    # direction (we hold an event the file no longer produces) is ALSO what a
    # restatement looks like once it has been absorbed by a --force re-pull, so
    # alarming on it would nag for ever afterwards — and a guard that cries wolf
    # is how the reports that matter get ignored. The count is still reported.
    missing = replayed - stored
    if not missing:
        return
    superseded = len(stored - replayed)
    base.note_incomplete(
        source, len(missing), len(replayed) or 1,
        why=f"upstream restated a past dt: replaying the file below the watermark "
            f"{since} produced {len(missing)} slot events that are NOT stored "
            f"({superseded} stored events the file no longer produces). Nothing was "
            "overwritten — every stored row carries its own retrieved_as_of — so "
            "re-pull this season with --force to land the restatement as a new version",
    )


def _note_degraded(degraded, *, source: str) -> None:
    """Report every club-panel the collapse floor suppressed, one line each.

    ``note_incomplete``, not ``note_drops``: nothing was dropped and nothing was
    lost — the club's previous listing is still readable at this ``as_of``, which
    is the whole point. What is missing is a fresh observation for that club.

    The caller passes only the records for panels it is about to STORE. A
    whole-file re-diff sees all 12 historical collapses on every single pull, and
    a warning that fires identically for ever is the wolf-cry this module's other
    guards are explicitly written to avoid (see ``_check_restatement``).
    """
    for row in degraded:
        base.note_incomplete(
            source, row["tombstones_refused"], row["before"],
            why=f"PARTIAL SCRAPE: {row['team']}'s chart in panel {row['observed_at']} "
                f"carries {row['now']} of the {row['before']} slots it carried in the "
                f"last trusted panel (< {PANEL_COLLAPSE_RATIO:.0%}), which upstream has "
                f"done 12 times in 348 panels. {row['tombstones_refused']} 'slot "
                f"vacated' facts were REFUSED rather than fabricated; that club's "
                "listings carry forward and this panel is not authoritative for it",
        )


def ingest_depth_charts(conn, df, *, season: int, retrieved_as_of: str,
                        since: str | None = None) -> int:
    """Diff a whole-season panel frame into change events and store them.

    ``season`` is **stamped from the file requested, never inferred from ``dt``**.
    The 2025 file spans ``2025-08-03 .. 2026-03-14`` and the 2026 file opens
    ``2026-03-22``; inferring the season from the timestamp would misfile every
    season's Jan-Mar tail into the next one. There are zero overlapping ``dt``, so
    the partition is clean — it is just not derivable from the data.

    ``since`` is the ingest watermark: events at or below it are already stored
    and are not written again (they are still computed, and compared — see
    ``_check_restatement``). ``since=None`` writes the whole history, which is the
    backfill and the first pull of a new season file.

    **A club whose chart collapses past ``PANEL_COLLAPSE_RATIO`` is suppressed,
    not tombstoned** — measured 12 times in 348 panels, and Sunday's first
    ``since=None`` pull would otherwise have baked all of them in at once. The
    suppression is a function of the WHOLE file, exactly like every other part of
    the diff, so an incremental pull and a backfill still reach byte-identical
    tables (asserted in the tests, on the fixture that carries two real
    collapses).

    ``knowable_as_of = observed_at[:10]``. Confirmed measurable rather than
    assumed: the 2026 asset's ``updated_at`` is 7 seconds after the file's
    ``max(dt)`` (the 2025 pair, 19 seconds), and a file cannot be public before it
    is uploaded, so ``dt`` <= public availability always — the conservative
    direction. All observed ``dt`` fall in 06:38Z-19:01Z, so the UTC date never
    precedes the US-Eastern date either.

    **Week is neither derived nor stored.** The feed has no week and the diff
    reads do not need one (a change is ``dt_n`` vs ``dt_{n-1}``). Where a week
    matters it is a query-time argument: pass ``as_of = the day before that team's
    gameday``. Storing a derived week would re-import the season-boundary problem
    for the Feb-Jul ``dt`` that belong to no week and reintroduce the schedules
    dependency that ``dt`` just eliminated.

    Returns rows written across BOTH tables (slots + panels), which is what the
    run log means by "rows written".
    """
    season = int(season)
    if season < PANEL_MIN_SEASON:
        raise LegacyDepthChartFrame(
            f"season {season} predates the dated-panel regime (first panel season "
            f"is {PANEL_MIN_SEASON}); 2021-2024 belong to "
            "ziggurat.data.nfl.depth_charts_weekly"
        )
    _require_panel_frame(df)

    events, panels, degraded = _change_log(
        df, season=season, retrieved_as_of=retrieved_as_of)
    if since is not None:
        _check_restatement(conn, season=season, events=events, since=since, source="depth_charts")
        events = [row for row in events if row["observed_at"] > since]
        panels = [row for row in panels if row["observed_at"] > since]
        degraded = [row for row in degraded if row["observed_at"] > since]
    _note_degraded(degraded, source="depth_charts")

    # ONE transaction for both tables, with the panel row written LAST. The panel
    # row is the watermark, so a crash between the two writes must not advance it
    # past slot rows that never landed.
    written = base.upsert(conn, "depth_chart_slots", events,
                          key_cols=_SLOT_PK_COLS, commit=False)
    written += base.upsert(conn, "depth_chart_panels", panels,
                           key_cols=_PANEL_PK_COLS, commit=False)
    conn.commit()
    return written


def pull_depth_charts(conn, season: int, *, retrieved_as_of: str) -> int:
    """Pull one season's panel file and store the events new since the watermark.

    ``nfl.import_depth_charts`` is the single seam cached-fixture tests patch.
    The whole file is downloaded every time — that is what makes the daily path
    and the backfill path the same code. Measured live on 2026-07-25: the whole
    2026 pull (download + diff + write of 8,248 rows) took 4.0 s, and the same
    call repeated the same day took 2.9 s and wrote 0 rows.
    """
    df = nfl.import_depth_charts([season])
    return ingest_depth_charts(
        conn, df, season=season, retrieved_as_of=retrieved_as_of,
        since=latest_observed_at(conn, season=season),
    )


# --------------------------------------------------- cadence seams (item 3.1b)


def resolve_season(*, season: int, today) -> int:
    """Which season's FILE to request today. The March handover, and only that.

    Two libraries disagree about when the NFL league year turns over:
    ``ziggurat.data.asof.nfl_season_of`` flips on **March 1**;
    ``nflreadpy.get_current_season(roster=True)``, which ``load_depth_charts``
    validates against, flips on **March 15**. So between March 1 and March 14 a
    request for the season ziggurat calls "current" raises upstream
    (``ValueError: Season must be between 2001 and <year>``) while the live chart
    is still being published daily inside the PREVIOUS season's file — measured:
    the 2025 file's last observation is ``2026-03-14T07:32:09Z``.

    This resolver must run BEFORE ``start_run`` writes the run-log row, or the
    log records a season that was never pulled — verbatim the failure
    ``refresh.last_run``'s own docstring records ("one backfill made the 2026
    board read `fresh 0d` against a table holding zero 2026 rows"), and it would
    leave the watermark NULL so the ingester rewrote the whole baseline every
    morning for two weeks.

    March 15-21 is deliberately NOT redirected. The old file has stopped (last
    ``dt`` 2026-03-14) and the new one has not opened (first ``dt``
    2026-03-22), so the honest answer is "this season is not published yet" —
    which ``run_ingest`` records as ``upstream_absent``: visible in
    ``ingest status`` as *awaiting*, exit code 0, no alarm. The exact cut date
    can only be BOUNDED to 2026-03-15..03-21 (no ``dt`` exists in the window), so
    nothing here infers one.

    Both failure directions are recoverable, which is the property that makes the
    bound safe: the season file carries its WHOLE history, so a panel this
    resolver was a few days early or late for is picked up in full by the next
    successful pull.

    An explicitly requested past season is never second-guessed — that is the
    backfill's path.
    """
    day = normalize_as_of(today)
    if season != nfl_season_of(day):
        return season
    if day.month == 3 and day.day < NFLREADPY_ROSTER_FLIP_DAY:
        return season - 1
    return season


def nothing_new_to_pull(conn, *, season: int, today) -> str | None:
    """``SourceSpec.applicable`` body: is there anything to fetch today?

    Returns a reason string (skip) or ``None`` (pull). Network-free by contract —
    ``refresh.decide()`` is pure of the network so that ``--dry-run`` reports
    exactly what a real run will do.

    Fires when the newest stored observation is already dated ``today``: upstream
    publishes one panel a day (all 348 observed ``dt`` fall in 06:38Z-19:01Z, and
    the ingest timer fires at 14:20 UTC), so a second run the same day has nothing
    to fetch. Two consequences, both deliberate:

    * ``--force`` is skipped too (``applicable`` is checked before the interval
      gate and takes no ``force``, exactly like ``game_weather``'s). Harmless: a
      re-pull would be a byte-identical no-op, and the panel row is written last
      inside one transaction, so "we hold today's panel" implies its slot rows
      landed.
    * Four measured days carry 2-3 panels (2025-08-09, 2025-08-11, 2026-03-22).
      The later one is picked up on the NEXT day's run — the whole file is
      re-diffed every time and ``knowable_as_of`` comes from ``dt``, so the stamp
      stays correct; only the retrieval is a day later.

    WHAT IT CANNOT SEE: the ~2% of days on which upstream publishes NO panel at
    all (5 of 224 days in 2025, 1 of 126 in 2026). Distinguishing those from a
    real failure needs the download, which ``decide()`` may not do — so such a day
    reaches ``run_ingest`` with 0 rows written. See this module's HANDOFF note in
    the 3.2c plan entry.
    """
    watermark = latest_observed_at(conn, season=season)
    if watermark is None:
        return None
    if watermark[:10] >= normalize_as_of(today).isoformat():
        return (f"already hold the panel published {watermark} — upstream publishes "
                "one depth-chart panel a day and today's is stored")
    return None


# --------------------------------------------------------------- accessors


def resolve_panel_season(conn, *, as_of, view: base.AsOfView = "historical") -> int | None:
    """The single season whose panel is current at ``as_of`` — never a union.

    Measured why this is not "no filter": at ``as_of=2026-07-25`` an unfiltered
    read returns **5,489 rows — 3,176 real 2026 slots plus 2,313 stale 2025
    slots**, because slots retired at the season boundary carry no cross-file
    tombstone (the 2025 file simply stops). Auto-resolution also removes the
    March trap from the caller: ``as_of=2026-03-18`` resolves to 2025, whose
    final chart is the true answer that day.

    Returns ``None`` when nothing is knowable at ``as_of``.
    """
    if view not in base.AS_OF_VIEWS:
        raise ValueError(f"unknown as-of view {view!r} (known: {base.AS_OF_VIEWS})")
    cutoff = normalize_as_of(as_of).isoformat()
    gate = "AND retrieved_as_of <= :as_of" if view == "historical" else ""
    row = conn.execute(
        f"""SELECT season FROM depth_chart_slots
             WHERE knowable_as_of <= :as_of {gate}
             GROUP BY season ORDER BY MAX(observed_at) DESC LIMIT 1""",
        {"as_of": cutoff},
    ).fetchone()
    return row["season"] if row is not None else None


def get_depth_chart(conn, *, as_of, season=None, team=None, positions=None,
                    view: base.AsOfView = "historical"):
    """The depth chart as it stood at ``as_of`` (keyword-only; no implicit now).

    Occupancy rows only — tombstones are resolved (they win the ``MAX`` so they
    can retire the slot they follow) and then filtered from the output. Filtering
    them earlier is precisely the ghost bug: a vacated slot has no newer row of
    its own, so its previous occupant would be carried forward for ever.

    ``season=None`` AUTO-RESOLVES through ``resolve_panel_season`` — see its
    docstring for the measured reason a union is wrong. ``positions`` filters
    ``pos_abb`` (``["QB", "RB", "WR", "TE"]`` is the fantasy set; the table stores
    every position, IDP included).

    Day-granular by ``select_as_of``'s documented contract: ``as_of=D`` sees every
    panel published on D. Where several panels share a day, ``observed_at``
    orders them and the last one wins.

    **``observed_at`` on a returned row is NOT the chart's publication date.**
    This is a change log: a slot keeps the instant its value was FIRST observed,
    so a correct read legitimately mixes many ``observed_at`` — a stable starter
    can carry an instant from August. Anything reporting "chart observed <date>"
    to the operator must take it from ``get_depth_chart_observed`` (the panel
    row), not from these rows, or a live chart will be presented as months stale.
    The row's own instant answers a different and also useful question: how long
    this listing has said so.
    """
    if season is None:
        season = resolve_panel_season(conn, as_of=as_of, view=view)
        if season is None:
            return []

    clauses = ["g.espn_id IS NOT NULL", "g.season = :season"]
    params = {"season": season}
    if team is not None:
        clauses.append("g.team = :team")
        params["team"] = team
    if positions is not None:
        names = {f"pos{i}": p for i, p in enumerate(positions)}
        if not names:
            return []
        clauses.append(f"g.pos_abb IN ({', '.join(':' + k for k in names)})")
        params.update(names)
    return base.select_observed_as_of(
        conn, "depth_chart_slots", as_of=as_of, key_cols=list(_SLOT_KEY_COLS),
        extra_where=" AND ".join(clauses), params=params, view=view,
    )


def get_depth_chart_observed(conn, *, as_of, season=None,
                             view: base.AsOfView = "historical"):
    """The panel-metadata row behind ``get_depth_chart(as_of=...)``, or ``None``.

    Rule 6: a recommendation must be able to say "depth chart observed
    2025-10-15 07:17Z" instead of implying the listing is live. The slot log alone
    cannot distinguish "no panel was published" from "a panel was published and
    nothing moved" — that is this table's whole job.

    ``n_teams`` on the returned row is the number of clubs this panel is
    AUTHORITATIVE for, not the number that appear in it: a club whose chart
    collapsed past ``PANEL_COLLAPSE_RATIO`` is excluded, because its listings at
    this ``as_of`` are carried forward from an earlier panel rather than
    confirmed by this one. ``panel_completeness_caveat`` turns that into the
    sentence a novice can act on — call it whenever this row is being reported.
    """
    if season is None:
        season = resolve_panel_season(conn, as_of=as_of, view=view)
        if season is None:
            return None
    rows = base.select_observed_as_of(
        conn, "depth_chart_panels", as_of=as_of, key_cols=["season"],
        extra_where="g.season = :season", params={"season": season}, view=view,
    )
    return rows[0] if rows else None


def panel_completeness_caveat(conn, *, as_of, season=None,
                              view: base.AsOfView = "historical") -> str | None:
    """One novice-legible sentence when the panel behind ``as_of`` is PARTIAL.

    Returns ``None`` on a complete panel (338 of 348 measured), so a caller can
    print it unconditionally.

    Rule 6, and it is the reason the collapse floor is not merely a silent
    correction: after suppression the chart reads perfectly normally — that is
    the repair — so nothing on the surface would otherwise tell the operator that
    some clubs' listings on this date were never confirmed. The comparison is
    data-derived (this season's largest authoritative panel at or before
    ``as_of``, which is 32 on the real files and 8 on the test fixture), never a
    hard-coded 32, so it cannot report nonsense on a partial-league slice.

    Gated exactly like every other read here: ``knowable_as_of`` always,
    ``retrieved_as_of`` too under ``historical``.
    """
    row = get_depth_chart_observed(conn, as_of=as_of, season=season, view=view)
    if row is None:
        return None
    gate = "AND retrieved_as_of <= :as_of" if view == "historical" else ""
    full = conn.execute(
        f"""SELECT MAX(n_teams) AS n FROM depth_chart_panels
             WHERE season = :season AND knowable_as_of <= :as_of {gate}""",
        {"season": row["season"], "as_of": normalize_as_of(as_of).isoformat()},
    ).fetchone()["n"]
    if full is None or row["n_teams"] >= full:
        return None
    return (
        f"CAVEAT: the depth chart published {row['observed_at']} is PARTIAL — "
        f"upstream served a usable chart for {row['n_teams']} of {full} clubs. The "
        "other clubs' listings shown here are CARRIED FORWARD from an earlier "
        "panel, not confirmed by this one, and nothing is reported as vacated for "
        "them (measured: upstream did this to 12 club-panels in the 348 published "
        "across 2025-2026; each recovered in full the next day)."
    )


#: What ``depth_chart_diff`` keys a player's listing on.
#:
#: NOT ``espn_id`` alone: that is not a function. Measured on the 2025 panel,
#: 554,215 rows -> 48,764 of 495,581 (dt, team, espn_id) triples carry MORE THAN
#: ONE row, all differing in ``pos_abb`` (e.g. Ihmir Smith-Marsette listed
#: Special-Teams PR rank 1, Special-Teams KR rank 4 AND 3WR-1TE WR rank 10 on the
#: same day). Keyed on the player alone, a dict keeps whichever row iterated last
#: and the winner can flip between the two reads — emitting a well-formed
#: ``{verdict: "demoted", rank_before: 1, rank_after: 10}`` for a player who did
#: not move. Filtered to skill positions that is 333 of 156,204 rows (0.21%) —
#: small, and exactly the fantasy-relevant RB/FB and WR/PK dual listings.
_LISTING_KEY = ("espn_id", "pos_grp_id", "pos_abb")

VERDICT_PROMOTED = "promoted"
VERDICT_DEMOTED = "demoted"
VERDICT_ADDED = "added"
VERDICT_REMOVED = "removed"


def _listings(rows) -> dict[tuple, dict]:
    return {tuple(row[c] for c in _LISTING_KEY): row for row in rows}


def _listing_dict(row, *, rank_before, rank_after, verdict) -> dict:
    return {
        "espn_id": row["espn_id"], "gsis_id": row["gsis_id"],
        "player_name": row["player_name"], "team": row["team"],
        "pos_grp_id": row["pos_grp_id"], "pos_abb": row["pos_abb"],
        "rank_before": rank_before, "rank_after": rank_after, "verdict": verdict,
    }


class NoBaselinePanel(LookupError):
    """No stored panel backs the ``since`` end of a requested comparison."""


def _require_baseline(conn, *, since, as_of, season, view, caller: str) -> None:
    """Refuse to diff against a day this archive never observed.

    An EMPTY before-state and an UNKNOWN before-state are not the same fact, and
    read back through ``get_depth_chart`` they are byte-identical: both are zero
    rows. Left undefended, every listing in the after-state reads as ``added``
    and every club reads as a QB1 change -- a confident, well-formed, fabricated
    answer with no error anywhere, which is the exact failure this repo has paid
    for twice (item 3.2's bye row vs its "no forecast" row, item 3.1's dropped
    player vs its stale holder). ``depth_chart_panels`` is what tells the two
    apart, so ask it before answering rather than after.

    Raises rather than returning empty: a caller that got ``[]`` would conclude
    "nothing changed", which is the same lie in the opposite direction. This is a
    READ path, so raising costs a caller an error it can handle -- unlike the
    ingest path, where refusing a partial panel would re-refuse on every pull and
    brick the source permanently (see ``PANEL_COLLAPSE_RATIO``).
    """
    if get_depth_chart_observed(conn, as_of=since, season=season, view=view) is not None:
        return
    raise NoBaselinePanel(
        f"{caller}: no depth-chart panel is stored at since={since} for season "
        f"{season}, so there is no baseline to compare against and a real change "
        f"cannot be told apart from a listing seen here for the first time. "
        f"get_depth_chart_observed(as_of=...) is the check for which days this "
        f"archive can answer for; pass a since= it returns a row for."
    )


def depth_chart_diff(conn, *, since, as_of, season=None, team=None, positions=None,
                     view: base.AsOfView = "historical") -> list[dict]:
    """Per-player NET change in listed role between two days.

    Both ends are ordinary ``get_depth_chart`` reads, so both are as-of gated and
    the same view applies to each. Returns
    ``{espn_id, gsis_id, player_name, team, pos_grp_id, pos_abb, rank_before,
    rank_after, verdict}`` with ``verdict`` in {promoted, demoted, added,
    removed}; an unchanged listing yields no row.

    NET change, not the event scan. The stored rows between two dates ARE the
    change set and reading them directly is ~free, but a slot that changed twice
    inside the window would be reported twice and the consumer (3.3) wants the
    net move. ~60 ms per call.

    A player who changed CLUBS inside the window is reported as ``removed`` from
    the old chart and ``added`` to the new one, never as a rank move: comparing
    "WR2 in Buffalo" with "WR4 in Miami" and printing "demoted" is exactly the
    kind of well-formed nonsense Rule 6 exists to prevent.
    """
    if normalize_as_of(since) > normalize_as_of(as_of):
        raise ValueError(f"since ({since}) must be on or before as_of ({as_of})")
    if season is None:
        season = resolve_panel_season(conn, as_of=as_of, view=view)
        if season is None:
            return []

    _require_baseline(conn, since=since, as_of=as_of, season=season, view=view,
                      caller="depth_chart_diff")

    read = dict(season=season, team=team, positions=positions, view=view)
    before = _listings(get_depth_chart(conn, as_of=since, **read))
    after = _listings(get_depth_chart(conn, as_of=as_of, **read))

    out: list[dict] = []
    for key in before.keys() - after.keys():
        row = before[key]
        out.append(_listing_dict(row, rank_before=row["pos_rank"], rank_after=None,
                                 verdict=VERDICT_REMOVED))
    for key in after.keys() - before.keys():
        row = after[key]
        out.append(_listing_dict(row, rank_before=None, rank_after=row["pos_rank"],
                                 verdict=VERDICT_ADDED))
    for key in before.keys() & after.keys():
        old, new = before[key], after[key]
        if old["team"] != new["team"]:
            out.append(_listing_dict(old, rank_before=old["pos_rank"], rank_after=None,
                                     verdict=VERDICT_REMOVED))
            out.append(_listing_dict(new, rank_before=None, rank_after=new["pos_rank"],
                                     verdict=VERDICT_ADDED))
        elif old["pos_rank"] > new["pos_rank"]:
            out.append(_listing_dict(new, rank_before=old["pos_rank"],
                                     rank_after=new["pos_rank"], verdict=VERDICT_PROMOTED))
        elif old["pos_rank"] < new["pos_rank"]:
            out.append(_listing_dict(new, rank_before=old["pos_rank"],
                                     rank_after=new["pos_rank"], verdict=VERDICT_DEMOTED))
    out.sort(key=lambda r: (r["team"] or "", r["pos_abb"] or "", r["rank_after"] or 99,
                            r["player_name"] or ""))
    return out


# ------------------------------------------------ the item-3.3 trigger contract
#
# THREE PERMITTED ROLES, each with its measured warrant. Written here because a
# module that merely omits the negative result is how 3.3 assumes otherwise.
#
#  1. `qb1_change_candidates` — a LABELLED HYPOTHESIS (below), never a validated
#     trigger, and never an availability claim.
#  2. Beneficiary identification, ON DEMAND. Given a candidate surfaced by
#     injury/usage, `get_depth_chart(team=T, positions=[P])` answers "who is
#     listed behind him". Report it as a LISTED ROLE with its observation date.
#  3. An EXPLANATION field on a candidate already ranked by usage/injury —
#     `explain_listing` below. Never a ranking input at RB/WR/TE.
#
# EXPLICITLY FORBIDDEN in 3.3: treating the ABSENCE of a demotion as evidence of
# availability (measured: it carries none), and any rank-1/rank-2 change at
# RB/WR/TE as a standalone trigger (panel rank-2 led the position after the
# starter's absence 75% at RB — identical to the usage baseline — 58% at TE and
# 49% at WR, where the usage baseline is BETTER at 52%).

#: The reasons attached to every QB1_CHANGE candidate. A list, verbatim, in the
#: shape item 3.2 established: every prior ships as a labelled hypothesis with its
#: source, and 3.2's own audit caught reasons quoting the WRONG study's n — so
#: each number below names the population it was measured on.
_QB1_CHANGE_REASONS = (
    "HYPOTHESIS, not a validated trigger: this is a change in LISTED ROLE ORDER, "
    "not an injury, an availability fact, or a start/sit recommendation.",
    "Population: 22 rank-1 QB changes across the 2025 regular season (~1.2 per "
    "week league-wide). THIS TRIGGER'S OWN PRECISION HAS NEVER BEEN MEASURED "
    "(3.2c design note §3.7 and §5) — measure it before promoting it to a rule.",
    "The supporting 92% is a DIFFERENT quantity: on the n=49 occasions a pre-game "
    "rank-1 QB was ABSENT from the box score, the pre-game rank-2 led the position "
    "92% of the time versus 73% for a prior-3-week usage baseline. That is "
    "conditioned on the starter's ABSENCE, not on this rank change, and n=49 is "
    "not the 22 events above.",
    "Known noise: 9% of rank-1 skill-position changes revert to the prior occupant "
    "within 7 days, and a 'persists for >=2 consecutive observations' filter was "
    "measured to suppress only 2 of 117 such changes — it does not work.",
    "Typical lag is ~3 days behind the roster move (Joe Burrow was injured in the "
    "2025-09-14 game and demoted at dt 2025-09-17T07:14:22Z), so this is a "
    "CONFIRMATION of news, not an early warning.",
)


def qb1_change_candidates(conn, *, since, as_of, season=None,
                          view: base.AsOfView = "historical") -> list[dict]:
    """Teams whose listed QB1 changed between ``since`` and ``as_of``.

    A LABELLED HYPOTHESIS. Every candidate carries ``reasons`` — the measured
    population, the fact that this trigger's own precision was never measured,
    which study the supporting 92% actually comes from and what it was
    conditioned on, and the known revert rate. Rule 6: the operator is a football
    novice and cannot smell an over-confident signal, so the caveats travel WITH
    the row rather than living in a design note nobody opens.

    Restricted to QB on purpose. Head-to-head against the usage-only baseline
    3.3 already has, the panel is additive at QB (92% vs 73%, n=49), a wash at RB
    (75% vs 75%), and WORSE THAN NOTHING at WR (49% vs 52%).

    Reports only teams that HAVE a listed QB1 at ``as_of``. A team whose QB1 slot
    vacated without a replacement is a chart oddity, not an opportunity signal;
    ``depth_chart_diff`` reports it as ``removed``.
    """
    if normalize_as_of(since) > normalize_as_of(as_of):
        raise ValueError(f"since ({since}) must be on or before as_of ({as_of})")
    if season is None:
        season = resolve_panel_season(conn, as_of=as_of, view=view)
        if season is None:
            return []
    _require_baseline(conn, since=since, as_of=as_of, season=season, view=view,
                      caller="qb1_change_candidates")
    observed = get_depth_chart_observed(conn, as_of=as_of, season=season, view=view)

    def qb1_by_team(day):
        rows = get_depth_chart(conn, as_of=day, season=season, positions=["QB"], view=view)
        return {row["team"]: row for row in rows if row["pos_rank"] == 1}

    before, after = qb1_by_team(since), qb1_by_team(as_of)
    out = []
    for team, row in sorted(after.items()):
        prior = before.get(team)
        if prior is not None and prior["espn_id"] == row["espn_id"]:
            continue
        out.append({
            "season": season, "team": team,
            "espn_id": row["espn_id"], "gsis_id": row["gsis_id"],
            "player_name": row["player_name"],
            "previous_espn_id": prior["espn_id"] if prior else None,
            "previous_player_name": prior["player_name"] if prior else None,
            "observed_at": row["observed_at"],
            "since": normalize_as_of(since).isoformat(),
            "as_of": normalize_as_of(as_of).isoformat(),
            "reasons": [
                f"{row['player_name']} is now listed QB1 for {team}"
                + (f", ahead of {prior['player_name']}" if prior else "")
                + f" (chart observed {row['observed_at']}"
                + (f"; panel published {observed['observed_at']}" if observed else "")
                + ").",
                *_QB1_CHANGE_REASONS,
            ],
        })
    return out


def explain_listing(conn, *, as_of, espn_id, season=None,
                    view: base.AsOfView = "historical") -> str | None:
    """One novice-legible sentence about a player's LISTED role, or ``None``.

    Role 3 of the trigger contract: an explanation field on a candidate that some
    other signal already ranked — never a ranking input itself.

    Two dates, deliberately, because conflating them is a Rule-6 hazard. **When
    the chart was published** comes from ``depth_chart_panels``; **since when this
    listing has said so** is the row's own ``observed_at``, which in a change log
    is the instant the value was FIRST observed and may be much older. Reporting
    the row's instant as the chart date would tell the operator a live chart is
    days stale; reporting only the chart date would hide that the listing has not
    moved in a month.
    """
    if season is None:
        season = resolve_panel_season(conn, as_of=as_of, view=view)
        if season is None:
            return None
    rows = base.select_observed_as_of(
        conn, "depth_chart_slots", as_of=as_of, key_cols=list(_SLOT_KEY_COLS),
        extra_where="g.espn_id IS NOT NULL AND g.season = :season AND g.espn_id = :espn_id",
        params={"season": season, "espn_id": str(espn_id)}, view=view,
    )
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (r["pos_abb"] or "", r["pos_rank"]))
    listings = ", ".join(f"{r['pos_abb']}{r['pos_rank']} ({r['pos_grp']})" for r in rows)
    unchanged_since = max(r["observed_at"] for r in rows)
    panel = get_depth_chart_observed(conn, as_of=as_of, season=season, view=view)
    published = panel["observed_at"] if panel is not None else unchanged_since
    name = rows[0]["player_name"] or f"espn_id {espn_id}"
    return (f"{name} is listed {listings} on {rows[0]['team']}'s depth chart "
            f"published {published} (this listing unchanged since {unchanged_since}). "
            "Listed ROLE ORDER only — a depth chart is not an availability signal "
            "(2025: starters ruled Out kept pos_rank 1 every single day).")
