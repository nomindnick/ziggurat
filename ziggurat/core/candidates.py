"""Candidate generator & signals (item 3.3).

A high-recall weekly scan for breakout / opportunity candidates, with the
evidence attached. Two LIVE arms plus a labelled hypothesis (see the
IMPLEMENTATION_PLAN.md 3.3 amendment 2026-07-25, measured on the real 2025
season):

  (1) USAGE_BREAKOUT  — ``usage.usage_deltas`` over RB/WR/TE, the FULL metric set
      (targets/target_share/air_yards_share/wopr/carries/receptions/rush+rec
      yards/snap share). NOT snap-share alone: Michael Wilson's real 2025 breakout
      has FALLING snap share and is caught only by air-yards-share and targets.
      QB has no differenced usage metric and is not scanned by this arm.
  (2) INJURY_SHOCK    — DUAL SOURCE. ``injuries.get_injuries`` is the
      historical / 2025-validation source; ``league.state.injury_transitions``
      is the LIVE in-season source (the nflverse feed has ~no waiver-day lead time
      in 2025+ and is 100% gameday-stamped). Both are merged, de-duped by gsis.
      **IR / season-ending injuries are INVISIBLE to the nflverse feed** (measured:
      James Conner, Najee Harris = 0 rows) — those shocks are carried by the usage
      arm alone; the live league-state arm surfaces them as ``INJURY_RESERVE``.
  (3) QB1_CHANGE      — a LABELLED HYPOTHESIS only (``depth_charts.
      qb1_change_candidates``), QB-only, season >= 2025 (panel regime). Its own
      precision was never measured; the folded reasons say so. FORBIDDEN: any
      RB/WR/TE rank-change trigger; treating absence-of-demotion as availability.

  TD-regression is DEFERRED to Phase 4 (no in-season source; ``ff_opportunity`` is
  a post-season model output whose stamp leaks the outcome distribution). There is
  NO red-zone signal (the sources carry no such column).

Standing rules. Rule 1 — every read is keyword-only ``as_of`` with no default and
threads ``view`` straight into the underlying accessors; the live path is
``historical``, the 2025 validation path binds ``base.latest_truth`` by wrapping
the whole generator once. Rule 2 — NO scoring constant, NO points column lives
here: these are role / usage / availability signals. Rule 6 — every CandidateRow
ships plain reasons, priors are quoted with their source and the word
"hypothesis", and injury lead-time is disclosed on every shock. Rule 8 — permanent
module, never imports from ``ziggurat/draft/``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType

from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import base, injuries, refresh, schedules
from ziggurat.data.nfl.depth_charts import (
    PANEL_MIN_SEASON,
    NoBaselinePanel,
    qb1_change_candidates,
)
from ziggurat.data.nfl.snap_counts import get_snap_counts
from ziggurat.data.nfl.usage import _combine_clubs, usage_deltas
from ziggurat.data.nfl.weekly_stats import get_weekly_stats
from ziggurat.league import state as league_state

# --------------------------------------------------------------------- constants

SIGNAL_USAGE = "USAGE_BREAKOUT"
SIGNAL_INJURY = "INJURY_SHOCK"
SIGNAL_QB1 = "QB1_CHANGE"  # labelled hypothesis only

# The usage arm scans skill positions only — QB has NO differenced usage metric
# (USAGE_METRICS is targets/carries/receiving), and DST/K usage deltas are
# meaningless. This is a guard, not a tuning choice.
USAGE_POSITIONS = ("RB", "WR", "TE")

# The staleness banner shouts past this many days between the data's pull date and
# the decision date. Same constant marginal.py uses.
STALE_BANNER_DAYS = 7


@dataclass(frozen=True)
class BreakoutThresholds:
    """Per-metric floors for the usage arm — a LABELLED HYPOTHESIS, not a tuned
    knob. A usage row qualifies if ANY positive delta clears its floor (high
    recall by construction); the magnitude is the sum of (delta / floor) over the
    cleared metrics. Verified on the live 2025 backfill: the five §7.3 targets
    (Dowdle, Tucker, Monangai, Henderson, Wilson) each clear >= 1 floor and the
    negative control (Gibbs) clears none. Precision tuning is Phase 4 (item 4.2),
    which is why ``label``/``source`` travel into the reason text (Rule 6)."""

    floors: MappingProxyType
    label: str
    source: str

    def qualifies(self, row: dict) -> dict[str, float]:
        """The metrics whose positive delta cleared its floor, {metric: delta}.
        ``None`` deltas are UNKNOWN (no prior game / crosswalk miss) and never
        count as a breakout — they are not treated as a real 0.0."""
        hits: dict[str, float] = {}
        for metric, floor in self.floors.items():
            value = row.get(f"d_{metric}")
            if value is not None and value >= floor:
                hits[metric] = float(value)
        return hits

    def magnitude(self, hits: dict[str, float]) -> float:
        return sum(v / self.floors[m] for m, v in hits.items())


DEFAULT_BREAKOUT = BreakoutThresholds(
    floors=MappingProxyType({
        # volume
        "carries": 6.0,
        "targets": 4.0,
        "receptions": 3.0,
        # share / role (the metrics that catch a snap-share-blind breakout).
        # NOTE (item 3.3 F8/F10): 'wopr' is DELIBERATELY absent — WOPR is exactly
        # 1.5*target_share + 0.7*air_yards_share, a deterministic linear echo of
        # two metrics already summed here, so including it triple-counts a single
        # pass-game role change and biases WR/TE magnitude over rushing. It is also
        # bare jargon to a novice (Rule 6), so it is dropped from the reason text too.
        "target_share": 0.08,
        "air_yards_share": 0.10,
        "offense_pct": 0.20,  # snap share
        # yardage (noisier outcomes; loose floors, recall only)
        "rushing_yards": 25.0,
        "receiving_yards": 25.0,
    }),
    label="hypothesis: high-recall usage floors, NOT tuned to outcomes (Phase 4 grades)",
    source="item 3.3 recon, verified on the 2025 backfill (§7.3 five-target set)",
)

# Absolute-usage floors for the ROLE-EMERGENCE cohort (item 3.3 F1): a player
# whose target week is his first knowable game this season carries prior_week=None
# and every d_* = None (no prior game to difference — a debut / return / promotion,
# the exact role change this scan exists to surface). The differenced floors above
# cannot fire for him, so his raw target-week usage is qualified against these
# ABSOLUTE floors instead (any-of; magnitude = sum of raw/floor over cleared floors).
# LABELLED HYPOTHESIS: conservative, NOT tuned to outcomes — precision tuning is
# deferred to Phase 4/4.2 (same status as DEFAULT_BREAKOUT). Source: item 3.3 F1,
# floors chosen so the ~11-23 debut rows/week stay a trickle, not a flood.
EMERGENCE_FLOORS = MappingProxyType({
    "carries": 10.0,
    "targets": 5.0,
    "receptions": 4.0,
    "offense_pct": 0.55,  # snap share
})
EMERGENCE_LABEL = (
    "hypothesis: absolute-usage role-emergence floors, NOT tuned to outcomes "
    "(item 3.3, tuning deferred to Phase 4)")

# The mandatory lead-time disclosure on every injury shock (Rule 6). Measured
# through the real accessor at a real waiver-day as_of (IMPLEMENTATION_PLAN 3.3).
INJURY_LEAD_NOTE = (
    "lead-time reality: the nflverse injury feed lands ~2 days before kickoff "
    "(2025 rows are 100% gameday-stamped; only 3.6-9.1% are knowable >=3 days "
    "out), so a Tuesday/Wednesday waiver scan surfaces little injury signal that "
    "far ahead. Live status comes from the ESPN league sync (4x/day). "
    "IR / season-ending injuries are INVISIBLE to the nflverse feed — those "
    "shocks show only as usage deltas (or as INJURY_RESERVE in league state)."
)

# report_status values that count as a vacancy in the nflverse feed. Questionable
# is a weekly game designation, not a shock, so it is excluded (it resolves week
# to week — see marginal.AvailabilityModel).
INJURY_OUT_STATUSES = frozenset({"out", "doubtful"})

# The injury feed and the ESPN league state both carry the WHOLE NFL roster,
# including IDP / offensive-line players nobody in a 10-team offense-skill league
# can roster. Surfacing "Tony Jefferson (S) Out" as an opportunity shock is pure
# noise (measured: 53 of 81 wk9 shocks were IDP/OL). This module reasons only
# about the offensive skill universe its usage arm scans plus QB, so the injury
# arm is filtered to it. A None position is KEPT (never silently drop a shock we
# cannot classify — the feed always carries one, so this is an edge guard only).
# K/DST streaming vacancies are item 3.5's lane, deliberately not this module's.
FANTASY_INJURY_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})


# ------------------------------------------------------------------- output rows


@dataclass(frozen=True)
class CandidateRow:
    player_key: str            # gsis_id when known, else espn:<id> or name
    player: str
    position: str | None
    team: str | None
    gsis_id: str | None
    espn_id: str | None
    signal_kind: str           # one of SIGNAL_*
    magnitude: float           # ranking key WITHIN a signal_kind
    week: int
    prior_week: int | None
    hypothesis: bool           # True for QB1_CHANGE (never validated)
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CandidateBoard:
    rows: tuple[CandidateRow, ...]
    week: int
    freshness: tuple[str, ...]
    notes: tuple[str, ...]
    as_of: str
    season: int

    def by_kind(self, kind: str) -> tuple[CandidateRow, ...]:
        return tuple(r for r in self.rows if r.signal_kind == kind)


# ------------------------------------------------------------- week resolution


def _resolve_completed_week(conn, *, as_of, season, view: base.AsOfView = "historical",
                            last_week: int = 18) -> int | None:
    """The last REG week fully played AND knowable at ``as_of``.

    Mirrors ``marginal.resolve_weeks`` step 3 but points BACKWARD: the usage arm
    diffs a completed week, and a raw mid-week ``as_of`` yields a partial slice
    (measured: usage_deltas as_of 2025-10-30 wk9 -> 6 rows, the Thursday game).
    Returns None when no week is complete yet (pre-season) so the caller can say
    so rather than guess.
    """
    cutoff = normalize_as_of(as_of)
    last_gameday: dict[int, str] = {}
    for g in schedules.get_schedule(conn, as_of=as_of, season=season, view=view):
        if g["game_type"] != "REG" or g["gameday"] is None:
            continue
        wk = int(g["week"])
        day = str(g["gameday"])
        if wk not in last_gameday or day > last_gameday[wk]:
            last_gameday[wk] = day
    complete = [w for w, day in last_gameday.items()
                if w <= last_week and normalize_as_of(day) <= cutoff]
    return max(complete) if complete else None


def _schedule_hidden_by_view(conn, *, as_of, season) -> bool:
    """True when REG schedule rows for ``season`` exist but every one visible-by-
    knowability was retrieved AFTER ``as_of`` (so the default historical view hides
    them). This is the two-view trap (item 3.3 F3): the data is there, only the
    view is wrong — distinct from a genuine pre-season read where no rows exist."""
    cutoff = normalize_as_of(as_of).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(retrieved_as_of) AS earliest FROM schedules "
        "WHERE season = :season AND game_type = 'REG' AND gameday IS NOT NULL "
        "AND knowable_as_of <= :as_of",
        {"season": season, "as_of": cutoff},
    ).fetchone()
    if row is None or not row["n"]:
        return False
    return row["earliest"] is not None and row["earliest"] > cutoff


def _week_is_partial(conn, *, as_of, season, week, view: base.AsOfView) -> bool:
    """True when ``week``'s last REG gameday is after ``as_of`` (the scan would
    read a partial slice)."""
    cutoff = normalize_as_of(as_of)
    last_day = None
    for g in schedules.get_schedule(conn, as_of=as_of, season=season, week=week, view=view):
        if g["game_type"] != "REG" or g["gameday"] is None:
            continue
        day = str(g["gameday"])
        if last_day is None or day > last_day:
            last_day = day
    return last_day is not None and normalize_as_of(last_day) > cutoff


# ----------------------------------------------------------------- the arms


def _snap_pct_by_gsis(conn, *, as_of, season, week, view) -> dict[str, float | None]:
    """offense_pct per gsis for ONE week — combining a mid-week trade's two club
    lines exactly as ``usage_deltas`` does (so the emergence path reads the same
    snap share the differenced path would). None means UNKNOWN, never a real 0.0."""
    by_gsis: dict[str, list] = {}
    for s in get_snap_counts(conn, as_of=as_of, season=season, week=week, view=view):
        if s["gsis_id"] is not None:
            by_gsis.setdefault(s["gsis_id"], []).append(s)
    # _combine_clubs returns a sqlite3.Row (single club) or a dict (folded trade) —
    # both index by column, so read offense_pct with [] (never .get, F1 regression).
    return {g: _combine_clubs(v)["offense_pct"] for g, v in by_gsis.items()}


def _emergence_hits(raw, snap_pct: float | None) -> dict[str, float]:
    """The absolute-usage floors a prior_week=None player CLEARS on his target
    week (any-of). None raw values are UNKNOWN and never clear a floor (Rule 2:
    never treat a missing observation as a real 0.0). ``raw`` is a weekly_stats
    row (sqlite3.Row — index access, no .get)."""
    values = {
        "carries": raw["carries"],
        "targets": raw["targets"],
        "receptions": raw["receptions"],
        "offense_pct": snap_pct,
    }
    hits: dict[str, float] = {}
    for metric, floor in EMERGENCE_FLOORS.items():
        v = values.get(metric)
        if v is not None and float(v) >= floor:
            hits[metric] = float(v)
    return hits


def _usage_arm(conn, *, as_of, season, week, positions, view, thresholds, names):
    """USAGE_BREAKOUT rows. ``names`` maps gsis_id -> display name (Rule 6:
    usage_deltas / weekly_stats carry NO name column).

    Two paths merge into one block: the DIFFERENCED path (week-over-week deltas vs
    the prior knowable game) and, for the prior_week=None cohort whose deltas are
    all None (season debut / return / promotion — item 3.3 F1), an ABSOLUTE-usage
    ROLE-EMERGENCE path off the target week's raw usage. Emergence rows are added
    to the SAME list so the injury arm's beneficiary index sees a debut who
    inherited a vacated role."""
    espn_of = base.espn_by_gsis(conn)  # crosswalk-at-now, immutable identity
    # Raw target-week usage + snap share for the role-emergence path, read
    # as_of- and view-threaded exactly like the delta read below.
    raw_usage = {r["player_id"]: r for r in get_weekly_stats(
        conn, as_of=as_of, season=season, week=week, view=view)}
    snap_pct = _snap_pct_by_gsis(conn, as_of=as_of, season=season, week=week, view=view)
    rows: list[CandidateRow] = []
    for position in positions:
        for d in usage_deltas(conn, as_of=as_of, season=season, week=week,
                              position=position, view=view):
            gsis = d["player_id"]
            if d["prior_week"] is None:
                # ROLE EMERGENCE — no prior week to difference (F1).
                raw = raw_usage.get(gsis)
                if raw is None:
                    continue
                hits = _emergence_hits(raw, snap_pct.get(gsis))
                if not hits:
                    continue
                usage_bits = ", ".join(_fmt_delta_abs(m, v) for m, v in sorted(
                    hits.items(), key=lambda kv: -kv[1] / EMERGENCE_FLOORS[kv[0]]))
                reasons = [
                    f"role emergence — first knowable game this season (no prior week "
                    f"to difference): {usage_bits}.",
                    f"absolute-usage floor is a labelled hypothesis (item 3.3, tuning "
                    f"deferred to Phase 4). ({EMERGENCE_LABEL})",
                ]
                rows.append(CandidateRow(
                    player_key=gsis or f"?:{d['team']}",
                    player=names.get(gsis, gsis or "?"),
                    position=d["position"], team=d["team"],
                    gsis_id=gsis, espn_id=espn_of.get(gsis),
                    signal_kind=SIGNAL_USAGE,
                    magnitude=sum(v / EMERGENCE_FLOORS[m] for m, v in hits.items()),
                    week=week, prior_week=None,
                    hypothesis=False, reasons=tuple(reasons),
                ))
                continue
            hits = thresholds.qualifies(d)
            if not hits:
                continue
            reason_bits = ", ".join(_fmt_delta(m, v) for m, v in sorted(
                hits.items(), key=lambda kv: -kv[1] / thresholds.floors[kv[0]]))
            reasons = [
                f"usage jumped in week {week} vs week {d['prior_week']}: {reason_bits}.",
                f"({thresholds.label}; {thresholds.source})",
            ]
            snap = d.get("d_offense_pct")
            if snap is not None and snap < 0 and hits:
                reasons.append(
                    f"NOTE: snap share actually FELL ({snap:+.0%}); this breakout is "
                    f"in the passing-game usage, not playing time.")
            rows.append(CandidateRow(
                player_key=gsis or f"?:{d['team']}",
                player=names.get(gsis, gsis or "?"),
                position=d["position"], team=d["team"],
                gsis_id=gsis, espn_id=espn_of.get(gsis),
                signal_kind=SIGNAL_USAGE,
                magnitude=thresholds.magnitude(hits),
                week=week, prior_week=d["prior_week"],
                hypothesis=False, reasons=tuple(reasons),
            ))
    return rows


_METRIC_LABEL = {
    "carries": "carries", "targets": "targets", "receptions": "catches",
    "target_share": "target share", "air_yards_share": "air-yards share",
    "offense_pct": "snap share",  # 'wopr' intentionally omitted — see F8/F10 above
    "rushing_yards": "rush yds", "receiving_yards": "rec yds",
}

# Metrics rendered as percentages in reasons (shares/snap%); everything else counts.
_PCT_METRICS = ("target_share", "air_yards_share", "offense_pct")


def _fmt_delta(metric: str, value: float) -> str:
    label = _METRIC_LABEL.get(metric, metric)
    if metric in _PCT_METRICS:
        return f"{label} {value:+.0%}"
    return f"{label} {value:+.0f}"


def _fmt_delta_abs(metric: str, value: float) -> str:
    """Render an ABSOLUTE usage value (role-emergence path, F1) — no +/- sign,
    since these are raw counts/shares, not week-over-week deltas."""
    label = _METRIC_LABEL.get(metric, metric)
    if metric in _PCT_METRICS:
        return f"{label} {value:.0%}"
    return f"{label} {value:.0f}"


def _clean_position(pos) -> str | None:
    """Normalize a feed position: strip whitespace, and treat empty-after-strip as
    None (item 3.3 F9 — nflverse encodes some unknown positions as a whitespace
    string, which is non-None and would defeat the None-KEEP edge guard, silently
    dropping a shock the guard's stated intent is to keep)."""
    if pos is None:
        return None
    stripped = str(pos).strip()
    return stripped or None


def _injury_arm(conn, *, as_of, season, week, view, usage_rows):
    """INJURY_SHOCK rows from BOTH sources, merged/de-duped by a cross-source
    identity (match on EITHER gsis OR espn id, F16), so a player Out in both
    feeds with gsis in one and NULL in the other collapses to ONE row.

    Committee-safe: a vacancy is linked to ALL same-team/same-position usage
    upticks (never THE single beneficiary); the beneficiaries' usage rows get a
    hedged 'vacancy opened' reason appended in-place via ``usage_rows``.
    """
    # index usage upticks by (team, position) for committee-safe beneficiary hints
    by_slot: dict[tuple, list[CandidateRow]] = {}
    for u in usage_rows:
        by_slot.setdefault((u.team, u.position), []).append(u)

    # Crosswalk-at-now (immutable identity), same tool the display names use:
    # resolve a source's missing id so EITHER-id matching can collapse duplicates.
    espn_of_gsis = base.espn_by_gsis(conn)   # gsis -> espn
    gsis_of_espn = base.gsis_by_espn(conn)   # espn -> gsis
    # percent_owned proxy for rostered value (F17 secondary sort), keyed by both ids.
    pct_owned = _percent_owned_lookup(conn, as_of=as_of, season=season, view=view)

    merged: dict[str, dict] = {}
    gsis_index: dict[str, str] = {}   # gsis -> merged key
    espn_index: dict[str, str] = {}   # espn -> merged key

    def _entry(gsis, espn, name, position, team):
        # unify the key space: an entry already registered under EITHER id wins.
        key = (gsis and gsis_index.get(gsis)) or (espn and espn_index.get(espn))
        if key is None:
            key = gsis or (f"espn:{espn}" if espn else f"name:{name}")
            merged[key] = {
                "gsis_id": gsis, "espn_id": espn, "player": name,
                "position": position, "team": team, "sources": set(),
                "severity": 0.0, "reasons": [],
                "percent_owned": pct_owned.get(gsis) or pct_owned.get(espn),
            }
        entry = merged[key]
        entry["gsis_id"] = entry["gsis_id"] or gsis
        entry["espn_id"] = entry["espn_id"] or espn
        entry["player"] = entry["player"] or name
        entry["position"] = entry["position"] or position
        entry["team"] = entry["team"] or team
        if entry["percent_owned"] is None:
            entry["percent_owned"] = pct_owned.get(gsis) or pct_owned.get(espn)
        if gsis:
            gsis_index[gsis] = key
        if espn:
            espn_index[espn] = key
        return entry

    # (A) nflverse feed — historical / validation source
    for r in injuries.get_injuries(conn, as_of=as_of, season=season, week=week, view=view):
        status = str(r["report_status"] or "").strip().lower()
        if status not in INJURY_OUT_STATUSES:
            continue
        pos = _clean_position(r["position"])
        if pos is not None and pos not in FANTASY_INJURY_POSITIONS:
            continue  # IDP / OL — un-rosterable noise (Rule 6)
        gsis = r["gsis_id"]
        entry = _entry(gsis, espn_of_gsis.get(gsis), r["full_name"], pos, r["team"])
        entry["sources"].add("nflverse feed")
        entry["severity"] = max(entry["severity"], 2.0 if status == "out" else 1.0)
        injury = r["report_primary_injury"] or "injury"
        entry["reasons"].append(
            f"nflverse feed: listed {r['report_status']} for week {week} ({injury}).")

    # (B) ESPN league state — LIVE in-season source (also the ONLY source that
    # sees IR / season-enders). Transitions up to as_of; take the most recent
    # ruled_out per player.
    live_out: dict[str, dict] = {}
    for t in league_state.injury_transitions(conn, as_of=as_of, season=season, view=view):
        if t["direction"] != "ruled_out":
            continue
        pos = _clean_position(t["position"])
        if pos is not None and pos not in FANTASY_INJURY_POSITIONS:
            continue  # IDP / OL — un-rosterable noise (Rule 6)
        live_out[t["espn_player_id"]] = t  # last one wins (sorted ascending)
    for t in live_out.values():
        espn = t["espn_player_id"]
        gsis = t["gsis_id"] or gsis_of_espn.get(espn)  # resolve the crosswalk gap (F16)
        entry = _entry(gsis, espn, t["player"], _clean_position(t["position"]),
                       t["pro_team"])
        entry["sources"].add("ESPN league state (live)")
        entry["severity"] = max(entry["severity"],
                                3.0 if t["to_status"] == "INJURY_RESERVE" else 2.0)
        entry["reasons"].append(
            f"ESPN league state: {t['from_status'] or 'ACTIVE'} -> {t['to_status']} "
            f"(observed {t['became_knowable']}).")

    rows: list[CandidateRow] = []
    for key, e in merged.items():
        beneficiaries = by_slot.get((e["team"], e["position"]), [])
        reasons = list(e["reasons"])
        if beneficiaries:
            names = ", ".join(b.player for b in beneficiaries)
            reasons.append(f"possible beneficiaries (same team+position usage up): {names}. "
                           f"Committee — not naming one.")
            # Hedged to mirror the injury-side hedge (F7): a same-slot vacancy is
            # correlated with the usage, NOT proven to cause it (fires on entrenched
            # WR1/RB1 too, where the causation would be backwards).
            hint = (f"a same-team same-position vacancy opened this week "
                    f"({e['player'] or '?'} out, {'/'.join(sorted(e['sources']))}) "
                    f"— may or may not explain this usage.")
            # cross-annotate the beneficiaries' own usage rows in-place
            for i, b in enumerate(beneficiaries):
                beneficiaries[i] = _append_reason(b, hint)
        else:
            reasons.append("no same team+position usage uptick is visible yet "
                           "(first-week vacancy, committee unresolved, or he plays a "
                           "position the usage arm does not scan).")
        reasons.append(INJURY_LEAD_NOTE)
        rows.append(CandidateRow(
            player_key=key, player=e["player"] or "?",
            position=e["position"], team=e["team"],
            gsis_id=e["gsis_id"], espn_id=e["espn_id"],
            signal_kind=SIGNAL_INJURY, magnitude=e["severity"],
            week=week, prior_week=None, hypothesis=False,
            reasons=tuple(reasons),
        ))
    # Deterministic, value-aware order (F17): all Out rows tie at severity 2.0, so
    # rank by rostered value (percent_owned DESC) with a stable name/key tiebreak,
    # so --top keeps star shocks above the fold and is reproducible across a
    # backfill re-ingest. build_candidates' stable magnitude sort preserves this
    # within each severity tier.
    rows.sort(key=lambda r: (-(merged[r.player_key]["percent_owned"] or 0.0),
                             r.player or "", r.player_key))
    # write the cross-annotations back into the shared usage list
    _rewrite(usage_rows, by_slot)
    return rows


def _percent_owned_lookup(conn, *, as_of, season, view) -> dict[str, float]:
    """espn_id / gsis_id -> ESPN percent_owned as of a date (item 3.3 F17). Reads
    the league-state whole-universe snapshot; empty pre-season / off the DB, in
    which case the injury block falls back to its stable name/key tiebreak alone."""
    out: dict[str, float] = {}
    try:
        states = league_state.get_player_state(conn, as_of=as_of, season=season, view=view)
    except Exception:  # noqa: BLE001 — league_player_state may be absent in a fixture DB
        return out
    for s in states:
        pct = s["percent_owned"]
        if pct is None:
            continue
        if s["espn_player_id"] is not None:
            out[str(s["espn_player_id"])] = pct
        if s["gsis_id"] is not None:
            out[s["gsis_id"]] = pct
    return out


def _append_reason(row: CandidateRow, reason: str) -> CandidateRow:
    from dataclasses import replace
    if reason in row.reasons:
        return row
    return replace(row, reasons=row.reasons + (reason,))


def _rewrite(usage_rows: list[CandidateRow], by_slot: dict) -> None:
    """Push the cross-annotated beneficiary rows back into ``usage_rows`` (which
    the caller holds by reference), preserving order. ``by_slot`` lists hold the
    (already-replaced) annotated rows, keyed by gsis_id back onto the shared list."""
    remap = {b.gsis_id: b for slot in by_slot.values() for b in slot if b.gsis_id}
    for i, r in enumerate(usage_rows):
        if r.gsis_id in remap:
            usage_rows[i] = remap[r.gsis_id]


def _earliest_panel_day(conn, *, after, as_of, season, view) -> str | None:
    """The day (YYYY-MM-DD) of the earliest observed depth-chart panel at or after
    ``after`` and knowable at ``as_of`` — NEVER after ``as_of`` (the knowable gate
    forbids leakage). Used to snap a ``since`` that precedes the season's first
    panel forward onto a real baseline (item 3.3 F13)."""
    cutoff = normalize_as_of(as_of).isoformat()
    since_iso = normalize_as_of(after).isoformat()
    gate = "knowable_as_of <= :as_of"
    if view == "historical":
        gate += " AND retrieved_as_of <= :as_of"
    row = conn.execute(
        f"SELECT MIN(observed_at) AS first FROM depth_chart_panels "
        f"WHERE season = :season AND observed_at >= :since AND {gate}",
        {"season": season, "since": since_iso, "as_of": cutoff},
    ).fetchone()
    first = row["first"] if row else None
    return first[:10] if first else None


def _qb1_arm(conn, *, as_of, season, since, view):
    """QB1_CHANGE labelled-hypothesis rows plus an optional skip-note.

    Returns ``(rows, note)``. ``note`` is None when the arm answered (even if it
    found zero changes — that legitimately means "no QB1 changes"); it is a
    novice-legible sentence when the arm could NOT answer (item 3.3 F6), so an
    empty QB1 block is never ambiguous between "nothing happened" and "this view
    could not see the baseline". Season >= 2025 only (panel regime). A ``since``
    that precedes the season's first observed panel is snapped FORWARD onto that
    panel (F13), never crashed.
    """
    if since is None:
        return [], None  # arm not requested
    if int(season) < PANEL_MIN_SEASON:
        return [], (f"QB1 arm: season {season} is before the depth-chart panel "
                    f"regime (>= {PANEL_MIN_SEASON}); no QB1-change hypothesis is "
                    f"available for it.")
    try:
        candidates = qb1_change_candidates(conn, since=since, as_of=as_of,
                                           season=season, view=view)
    except NoBaselinePanel:
        # `since` precedes the first observed panel visible under this view. Snap
        # FORWARD to the earliest panel at-or-before as_of (F13) and diff from there.
        snapped = _earliest_panel_day(conn, after=since, as_of=as_of,
                                      season=season, view=view)
        skip_note = (
            f"QB1 arm produced nothing: no baseline depth-chart panel is visible "
            f"under the '{view}' view at/after {since} and knowable by {as_of}. For a "
            f"past-season read bind base.latest_truth (or pass --validate).")
        if snapped is None:
            return [], skip_note
        try:
            candidates = qb1_change_candidates(conn, since=snapped, as_of=as_of,
                                               season=season, view=view)
        except NoBaselinePanel:
            # Snapped day found by the outer as_of gate, but qb1_change_candidates
            # re-gates the baseline by as_of=snapped, under which a backfilled panel
            # (retrieved in the future) is still hidden — genuinely unanswerable here.
            return [], skip_note
    rows: list[CandidateRow] = []
    for c in candidates:
        rows.append(CandidateRow(
            player_key=c["gsis_id"] or f"espn:{c['espn_id']}",
            player=c["player_name"], position="QB", team=c["team"],
            gsis_id=c["gsis_id"], espn_id=c["espn_id"],
            signal_kind=SIGNAL_QB1, magnitude=1.0,  # a hypothesis list, not ranked precision
            week=0, prior_week=None, hypothesis=True,
            reasons=tuple(c["reasons"]),  # verbatim — carry the caveats (Rule 6)
        ))
    return rows, None


# ------------------------------------------------------------------- generator


class NoCompletedWeek(ValueError):
    """No REG week is fully played and knowable at ``as_of`` (pre-season), and the
    caller did not pass an explicit ``week``."""


def build_candidates(
    conn,
    *,
    as_of,
    season: int,
    week: int | None = None,
    positions: Sequence[str] | None = None,
    since=None,
    view: base.AsOfView = "historical",
    today=None,
) -> CandidateBoard:
    """The weekly candidate scan (item 3.3). Rule 1: ``as_of`` keyword-only, no
    default; ``view`` threaded into EVERY accessor.

    ``week`` is the last fully-played week to analyze. When omitted it is resolved
    to the last REG week fully played AND knowable at ``as_of``
    (``_resolve_completed_week``); a raw mid-week ``as_of`` otherwise reads a
    partial slice. ``positions`` restricts the usage arm (default RB/WR/TE).
    ``since`` enables the QB1 hypothesis arm (a baseline panel day; season >=
    2025). ``today`` drives the freshness banner only — never a substitute for
    ``as_of``.

    The 2025 validation path binds the WHOLE generator once:
    ``base.latest_truth(build_candidates)(conn, as_of=..., season=2025, week=W)``.
    """
    if week is None:
        week = _resolve_completed_week(conn, as_of=as_of, season=season, view=view)
        if week is None:
            # Distinguish a genuine pre-season read from the two-view trap (F3): if
            # backfilled schedule rows for this season exist but are hidden because
            # they were retrieved AFTER as_of, the fix is the view, not the week.
            if view == "historical" and _schedule_hidden_by_view(
                    conn, as_of=as_of, season=season):
                raise NoCompletedWeek(
                    f"no REG week resolves at as_of={as_of} for season {season} under "
                    f"the 'historical' view, but backfilled schedule rows for it exist "
                    f"that were retrieved after {as_of} (hidden under historical). For a "
                    f"past-season validation read bind base.latest_truth(build_candidates) "
                    f"(or pass --validate); otherwise pass week explicitly.")
            raise NoCompletedWeek(
                f"no REG week is fully played and knowable at as_of={as_of} for "
                f"season {season}; pass week explicitly (this is a pre-season as_of "
                f"with nothing to scan).")
    scan_positions = tuple(USAGE_POSITIONS if positions is None
                           else [p for p in positions if p in USAGE_POSITIONS])
    # Display names via the crosswalk-at-now (immutable identity), NOT the
    # as-of-gated id_crosswalk: under a past validation as_of the backfilled
    # players rows are hidden and every candidate would print as a raw gsis id.
    names = base.name_by_gsis(conn)

    usage_rows = _usage_arm(conn, as_of=as_of, season=season, week=week,
                            positions=scan_positions, view=view,
                            thresholds=DEFAULT_BREAKOUT, names=names)
    injury_rows = _injury_arm(conn, as_of=as_of, season=season, week=week,
                              view=view, usage_rows=usage_rows)
    qb1_rows, qb1_note = _qb1_arm(conn, as_of=as_of, season=season, since=since, view=view)

    # Rank WITHIN each kind by magnitude (descending). Python's sort is stable, so
    # the injury arm's percent_owned-then-name order (F17) survives this pass.
    ordered: list[CandidateRow] = []
    for group in (usage_rows, injury_rows, qb1_rows):
        ordered.extend(sorted(group, key=lambda r: -r.magnitude))

    notes: list[str] = []
    if _week_is_partial(conn, as_of=as_of, season=season, week=week, view=view):
        notes.append(
            f"PARTIAL WEEK: week {week}'s games are not all played/knowable at "
            f"{as_of} — the usage arm is reading an incomplete slice. Scan the last "
            f"fully-played week instead.")
    if qb1_note:  # the QB1 arm could not answer — say so (F6), never a silent empty block
        notes.append(qb1_note)
    if not usage_rows and not injury_rows and view == "historical":
        notes.append(
            "no candidates under the 'historical' view. If this is a past-season "
            "validation read, bind base.latest_truth(build_candidates) (or pass "
            "--validate on the CLI) — backfilled history is retrieved_as_of=today "
            "and reads EMPTY under historical.")

    freshness = tuple(_freshness_lines(conn, season=season, as_of=as_of, today=today))
    return CandidateBoard(
        rows=tuple(ordered), week=week, freshness=freshness,
        notes=tuple(notes), as_of=normalize_as_of(as_of).isoformat(), season=int(season),
    )


def _freshness_lines(conn, *, season, as_of, today) -> list[str]:
    """The staleness banner. Reads item 3.1b's per-source contract rather than
    inventing verdicts. A July usage snapshot pricing a November scan carries a
    valid ``knowable_as_of`` and is Rule-1-invisible — only this catches it.
    ``today`` is operational wall-clock, distinct from the ``as_of`` cutoff."""
    out: list[str] = []
    if today is None:
        return out
    # QUIET_VERDICTS is owned by refresh (item 3.2c F-D) so a new verdict cannot
    # reintroduce a false alarm here. Watched set = the sources THIS module reads.
    watched = {"weekly_stats", "snap_counts", "injuries", "depth_charts"}
    for s in refresh.source_freshness(conn, season=season, today=today):
        if s["source"] in watched and s["verdict"] not in refresh.QUIET_VERDICTS:
            age = "never pulled" if s["age_days"] is None else f"{s['age_days']}d old"
            out.append(
                f"  ingest says {s['source']}: {s['verdict']} ({age})"
                + ("  [this source cannot be re-pulled — a missed day is gone]"
                   if s["perishable"] else ""))
    return out


# --------------------------------------------------------------------- display

_KIND_TITLE = {
    SIGNAL_USAGE: "USAGE BREAKOUTS (role/volume change vs the prior week)",
    SIGNAL_INJURY: "INJURY SHOCKS (vacated roles — beneficiaries are committee-safe)",
    SIGNAL_QB1: "QB1-CHANGE HYPOTHESIS (labelled; precision never measured)",
}


def format_candidates(board: CandidateBoard, *, top: int | None = None,
                      reasons: bool = False) -> str:
    """Render the candidate board (display only — no logic, Rule 3).

    Each signal_kind prints in its own labelled block, ranked by magnitude.
    ``--reasons`` prints every candidate's evidence and caveats verbatim.
    """
    out: list[str] = [
        f"candidate scan — season {board.season}, week {board.week}, as of {board.as_of}",
    ]
    for note in board.notes:
        out.append(f"  ! {note}")
    for line in board.freshness:
        out.append(line)
    # Rule 6 legend: SIGNAL is a within-block ranking key, NOT fantasy points — the
    # blocks use different scales (usage sums delta/floor; injury is a fixed
    # severity), so it must never read as a points projection.
    out.append("  (SIGNAL = within-block ranking key, higher = stronger; NOT fantasy points)")
    out.append("")

    for kind in (SIGNAL_USAGE, SIGNAL_INJURY, SIGNAL_QB1):
        group = board.by_kind(kind)
        out.append(_KIND_TITLE[kind] + f"  ({len(group)})")
        if not group:
            out.append("  (none)")
            out.append("")
            continue
        shown = group if top is None else group[:top]
        out.append(f"  {'PLAYER':<24} {'POS':<4} {'TEAM':<5} {'SIGNAL':>7}")
        for r in shown:
            tag = "  [HYPOTHESIS]" if r.hypothesis else ""
            out.append(f"  {(r.player or '?')[:24]:<24} {(r.position or ''):<4} "
                       f"{(r.team or ''):<5} {r.magnitude:>7.2f}{tag}")
            if reasons:
                out.extend(f"      - {reason}" for reason in r.reasons)
        if top is not None and len(group) > top:
            out.append(f"  ... {len(group) - top} more")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
