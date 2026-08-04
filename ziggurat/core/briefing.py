"""The morning briefing composer (item 3.6): waiver + lineup + signals + alerts,
composed into a single two-minute read.

PURE COMPUTE + DETERMINISTIC RENDER. ``build_briefing`` calls the existing module
builders (it CALLS 3.4/3.5/3.3; it does not reimplement them — Rule 8's spirit)
and ``format_briefing`` renders the full markdown. NO LLM call and NO network and
NO file write live here: the ``push`` orchestration layer optionally hands this
rendered text to the router for a two-minute prose summary and writes it to
intel/weekly/, so this module is testable offline and a token/network failure can
never drop the deterministic briefing (R1's degrade-gracefully requirement).

PER-SECTION isolation (R2): every composed builder is wrapped so one section's
failure (candidates' NoCompletedWeek on a Tuesday, a pre-draft WeekResolutionError)
degrades THAT section to an explanatory note — a missing section must SAY why, not
silently vanish (Rule 6) — rather than killing the whole read. None of the four
builders share a common exception base, so each is caught on its own terms.
"""

from dataclasses import dataclass

from ziggurat.core import alerts as alerts_mod
from ziggurat.core import candidates as candidates_mod
from ziggurat.core import lineup_support, marginal, waiver
from ziggurat.data.asof import normalize_as_of
from ziggurat.data.nfl import news as news_mod
from ziggurat.data.nfl import refresh
from ziggurat.league import state as league_state

#: Snapshot age (days) past which the league-state banner escalates to a warning.
STALE_BANNER_DAYS = 2


@dataclass(frozen=True)
class BriefingSection:
    title: str
    body: str
    degraded: bool = False


@dataclass(frozen=True)
class Briefing:
    season: int
    as_of: str
    week: int | None
    team_id: int | None
    staleness: tuple[str, ...]
    sections: tuple[BriefingSection, ...]
    alert_events: tuple            # tuple[alerts.AlertEvent, ...] — feeds the teaser
    notes: tuple[str, ...]
    n_claims: int | None = None    # claims in the plan (None = plan unavailable)
    legal: bool | None = None      # roster legality (None = plan unavailable)

    @property
    def headline_summary(self) -> str:
        """A ONE-LINE, allowlist-safe teaser: counts + legality + week only, NO
        player or team names, so it is safe to push to a public ntfy topic even
        before the outbound scrub (which still runs as a backstop)."""
        wk = f"wk{self.week}" if self.week is not None else "wk?"
        parts = [wk]
        if self.legal is False:
            parts.append("ROSTER ILLEGAL — fix before claims")
        if self.n_claims is not None:
            parts.append(f"{self.n_claims} claim(s)")
        parts.append(f"{len(self.alert_events)} alert(s)")
        return ", ".join(parts) + " — full briefing on the box"


def _staleness_lines(conn, *, season, as_of, today) -> list[str]:
    """League-state snapshot recency + item-3.1b per-source contract + news-wire
    recency. A Wednesday briefing composed off a July projection or a dead news
    wire is Rule-1-invisible without this."""
    out: list[str] = []
    cutoff = normalize_as_of(as_of)

    days = league_state.snapshot_days(conn, season=season)
    knowable = [d for d in days if normalize_as_of(d) <= cutoff]
    if knowable:
        gap = (cutoff - normalize_as_of(knowable[-1])).days
        out.append(f"league state: last snapshot {knowable[-1]} ({gap}d before {as_of})")
        if gap > STALE_BANNER_DAYS:
            out.append(
                f"  WARNING: roster/FA pool are {gap} days stale — run `ziggurat league sync`."
            )
    else:
        out.append("league state: NO snapshot at this as-of — run `ziggurat league sync`.")

    if today is not None:
        watched = {"projections", "weekly_stats", "injuries"}
        for s in refresh.source_freshness(conn, season=season, today=today):
            if s["source"] in watched and s["verdict"] not in refresh.QUIET_VERDICTS:
                age = "never pulled" if s["age_days"] is None else f"{s['age_days']}d old"
                out.append(f"  ingest: {s['source']} {s['verdict']} ({age})")

    news_articles = news_mod.recent_news(conn, as_of=as_of, limit=1)
    if news_articles:
        out.append(
            f"news wire: newest stored note {news_articles[0]['published_at'][:10]} "
            f"(cadence health: `ziggurat alerts status`)"
        )
    else:
        out.append("news wire: no notes stored yet (`ziggurat alerts run` to pull).")
    return out


def build_briefing(
    conn,
    *,
    as_of,
    season: int,
    own_team_id: int | None,
    week: int | None = None,
    last_week: int = 17,
    claim_budget: int = 3,
    source: str = "sleeper_rotowire",
    view: league_state.base.AsOfView = "historical",
    today=None,
) -> Briefing:
    today = today or (as_of if isinstance(as_of, str) else None)
    notes: list[str] = []
    sections: list[BriefingSection] = []
    n_claims: int | None = None
    legal: bool | None = None

    # Resolve the forward week window ONCE for the two forward-looking builders,
    # so the briefing cannot internally disagree about which week it describes.
    weeks = None
    resolved_week = week
    if week is not None:
        weeks = list(range(week, last_week + 1))
        resolved_week = week
    else:
        try:
            weeks = list(
                marginal.resolve_weeks(conn, as_of=as_of, season=season, last_week=last_week, view=view)
            )
            resolved_week = weeks[0]
        except marginal.WeekResolutionError:
            notes.append("remaining-week window unresolved (preseason) — roster sections degrade.")

    # --- 1. WAIVERS (legality + claims + FCFS + streaming + drops) ---
    if own_team_id is None:
        sections.append(BriefingSection(
            "WAIVERS", "own team unresolved (SWID did not match a synced team) — "
            "run `ziggurat league sync`, or pass --team.", degraded=True))
    else:
        try:
            plan = waiver.build_waiver_plan(
                conn, as_of=as_of, season=season, own_team_id=own_team_id,
                weeks=weeks, last_week=last_week, claim_budget=claim_budget,
                source=source, view=view, today=today,
            )
            n_claims = len(plan.claims)
            legal = not plan.blocked
            sections.append(BriefingSection(
                "WAIVERS", waiver.format_waiver_plan(plan, reasons=False)))
        except (league_state.OwnTeamUnresolved, marginal.WeekResolutionError) as exc:
            sections.append(BriefingSection(
                "WAIVERS", f"waiver plan unavailable: {exc}", degraded=True))
        except Exception as exc:  # any unexpected error degrades THIS section only
            sections.append(BriefingSection(
                "WAIVERS", f"waiver plan unavailable: {type(exc).__name__}: {exc}", degraded=True))

    # --- 2. LINEUP flags ---
    if own_team_id is None:
        sections.append(BriefingSection(
            "LINEUP", "own team unresolved — no lineup guidance.", degraded=True))
    else:
        try:
            rec = lineup_support.build_lineup(
                conn, as_of=as_of, season=season, own_team_id=own_team_id,
                week=resolved_week, last_week=last_week, source=source, view=view, today=today,
            )
            sections.append(BriefingSection(
                "LINEUP", lineup_support.format_lineup_recommendation(rec, reasons=False)))
        except (league_state.OwnTeamUnresolved, lineup_support.OwnTeamUnresolved,
                marginal.WeekResolutionError, lineup_support.StartabilityError) as exc:
            sections.append(BriefingSection(
                "LINEUP", f"lineup guidance unavailable: {exc}", degraded=True))
        except Exception as exc:
            sections.append(BriefingSection(
                "LINEUP", f"lineup guidance unavailable: {type(exc).__name__}: {exc}", degraded=True))

    # --- 3. SIGNALS (league-wide breakout/injury/QB1 candidates) ---
    try:
        board = candidates_mod.build_candidates(
            conn, as_of=as_of, season=season, view=view, today=today)
        sections.append(BriefingSection(
            "SIGNALS", candidates_mod.format_candidates(board, top=8)))
    except candidates_mod.NoCompletedWeek as exc:
        sections.append(BriefingSection(
            "SIGNALS", f"no signals yet: {exc}", degraded=True))
    except Exception as exc:
        sections.append(BriefingSection(
            "SIGNALS", f"signals unavailable: {type(exc).__name__}: {exc}", degraded=True))

    # --- 4. ALERTS (new injury/handcuff/news events) ---
    alert_events: tuple = ()
    try:
        alert_board = alerts_mod.build_alerts(
            conn, as_of=as_of, season=season, own_team_id=own_team_id,
            week=resolved_week, last_week=last_week, source=source, view=view, today=today)
        alert_events = alert_board.events
        if alert_events:
            body = "\n\n".join(alerts_mod.format_alert_line(e) for e in alert_events)
        else:
            body = "no new injury/handcuff/news events since the last check."
        for n in alert_board.notes:
            notes.append(n)
        sections.append(BriefingSection("ALERTS", body))
    except Exception as exc:  # alerts must never sink the whole briefing
        sections.append(BriefingSection(
            "ALERTS", f"alert feed unavailable: {type(exc).__name__}: {exc}", degraded=True))

    try:
        staleness = tuple(_staleness_lines(conn, season=season, as_of=as_of, today=today))
    except Exception as exc:  # a freshness read must never discard the briefing
        staleness = (f"freshness banner unavailable: {type(exc).__name__}: {exc}",)

    return Briefing(
        season=season,
        as_of=str(as_of),
        week=resolved_week,
        team_id=own_team_id,
        staleness=staleness,
        sections=tuple(sections),
        alert_events=alert_events,
        notes=tuple(notes),
        n_claims=n_claims,
        legal=legal,
    )


def format_briefing(briefing: Briefing) -> str:
    """The full deterministic markdown briefing (the intel/weekly/ artifact and
    the fallback if the LLM prose step fails)."""
    wk = f"week {briefing.week}" if briefing.week is not None else "week ?"
    lines = [
        f"# Ziggurat briefing — {briefing.as_of} ({wk}, season {briefing.season})",
        "",
        "## Data freshness",
    ]
    lines.extend(f"- {s}" for s in briefing.staleness)
    if briefing.notes:
        lines.append("")
        lines.append("## Notes")
        lines.extend(f"- {n}" for n in briefing.notes)
    for section in briefing.sections:
        lines.append("")
        flag = " (DEGRADED)" if section.degraded else ""
        lines.append(f"## {section.title}{flag}")
        lines.append(section.body)
    return "\n".join(lines)
