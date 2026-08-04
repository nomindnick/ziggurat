"""Event -> alert pipeline (item 3.6): turn live league/news events into
novice-legible, alert-worthy events for the push layer.

PURE COMPUTE. ``build_alerts`` READS (injury transitions, handcuff links, the FA
pool, news) and RETURNS candidate events. It writes nothing: the dedup ledger,
the per-tick rate cap, and the actual ntfy push live in the ``push`` orchestration
layer (``push/run.py``), so this module is testable without side effects and the
dependency stays ``core -> league/data`` (never ``core -> push``, never
``draft/`` — Rule 8).

TWO EVENT SOURCES, one shape:
  * ``state.injury_transitions()`` — the LIVE in-season injury signal, diffing
    consecutive daily ``league_player_state`` snapshots (4x/day). Only the
    ``ruled_out`` crossings become phone events; ``cleared`` is briefing context.
    (Smoke/synthetic-tested only until real games produce a transition — the 3.3
    caveat carried forward.)
  * ``news.recent_news()`` — the ESPN news wire (item 3.6 R3), for fresh notes
    about a player the operator owns or could roster. Low severity: a headline
    must never outrank a real OUT on the phone lane.

"starter down -> handcuff available" REUSES ``marginal.handcuff_links`` (the
QB/RB/TE gate + the labelled uplift hypothesis) — never a new score (Rule 2). A
grab is offered only when the backup is genuinely a FREE AGENT right now
(``state.who_held is None``) and the starter is not simply on bye this week (a
this-week vacancy, not a season-long one) — both code-enforced with tests (Rule 6).
"""

from dataclasses import dataclass

from ziggurat.core import candidates as candidates_mod
from ziggurat.core import marginal
from ziggurat.league import state as league_state

#: Positions worth an injury alert at all (a ruled-out K/DST is not a shock the
#: operator acts on the way a skill starter is). Reuses candidates' set.
FANTASY_INJURY_POSITIONS = candidates_mod.FANTASY_INJURY_POSITIONS

#: The handcuff-grab arm is QB/RB/TE only — the measured handcuff study's set;
#: WR is deliberately excluded (uplift ~0). Enforced by test.
HANDCUFF_POSITIONS = marginal._HANDCUFF_POSITIONS

#: Season-ending designation.
_SEASON_ENDING = "INJURY_RESERVE"

#: Plain-language for the raw ESPN status enums (Rule 6: a novice cannot read
#: 'INJURY_RESERVE').
_STATUS_PHRASE = {
    "OUT": "OUT (will not play this week)",
    "INJURY_RESERVE": "on INJURED RESERVE (likely out multiple weeks or the season)",
}


def _status_phrase(to_status: str) -> str:
    return _STATUS_PHRASE.get(to_status, to_status)

#: Base phone-lane severity by kind; own-team events get +0.5 so your own player
#: outranks a generic handcuff on a busy tick.
_SEV_INJURY_RESERVE = 3.0
_SEV_INJURY_OUT = 2.0
_SEV_NEWS = 1.0
_OWN_BUMP = 0.5


@dataclass(frozen=True)
class AlertEvent:
    kind: str            # "INJURY_OUT" | "NEWS"
    player: str
    position: str | None
    team: str | None     # NFL pro-team abbr
    gsis_id: str | None
    espn_id: str | None
    on_team_id: int | None      # league holder at the event (None = FA / news)
    is_own: bool                # affects the operator's own roster
    headline: str               # one-line, novice-legible, ALLOWLIST-safe (player names only)
    detail: tuple[str, ...]     # the WHY lines (Rule 6)
    source: str
    event_day: str              # became_knowable (injury) | publish date (news)
    severity: float
    dedup_key: str
    handcuff_name: str | None = None
    handcuff_espn_id: str | None = None


@dataclass(frozen=True)
class AlertBoard:
    events: tuple[AlertEvent, ...]   # all alert-worthy candidates, severity desc
    season: int
    as_of: str
    week: int | None
    own_team_id: int | None
    notes: tuple[str, ...]           # degradation / honesty disclosures (Rule 6)


def _own_roster_espn(conn, *, as_of, season, own_team_id, view) -> set[str]:
    if own_team_id is None:
        return set()
    rows = league_state.get_player_state(
        conn, as_of=as_of, season=season, on_team_id=own_team_id, view=view
    )
    return {str(r["espn_player_id"]) for r in rows}


def _handcuff_by_starter(links) -> dict[str, marginal.HandcuffLink]:
    out: dict[str, marginal.HandcuffLink] = {}
    for link in links:
        if link.starter_espn_id:
            out[str(link.starter_espn_id)] = link
    return out


def build_alerts(
    conn,
    *,
    as_of,
    season: int,
    own_team_id: int | None,
    week: int | None = None,
    last_week: int = 17,
    source: str = "sleeper_rotowire",
    news_lookback_days: int = 2,
    view: league_state.base.AsOfView = "historical",
    today=None,
) -> AlertBoard:
    """Compute the alert-worthy events knowable at ``as_of``. Pure read."""
    notes: list[str] = []
    events: list[AlertEvent] = []
    own_roster = _own_roster_espn(conn, as_of=as_of, season=season, own_team_id=own_team_id, view=view)

    # --- handcuff links: price only if the week window resolves. ---
    handcuffs: dict[str, marginal.HandcuffLink] = {}
    resolved_week = week
    try:
        if week is not None:
            weeks = list(range(week, last_week + 1))
            resolved_week = week
        else:
            weeks = list(
                marginal.resolve_weeks(conn, as_of=as_of, season=season, last_week=last_week, view=view)
            )
            resolved_week = weeks[0]
        links = marginal.handcuff_links(
            conn, as_of=as_of, season=season, weeks=weeks,
            last_week=last_week, source=source, view=view,
        )
        handcuffs = _handcuff_by_starter(links)
    except marginal.WeekResolutionError:
        notes.append(
            "handcuff pricing unavailable (remaining-week window unresolved — "
            "preseason or no schedule yet): OWN-team player-down alerts still fire, "
            "but not-owned 'grab his handcuff' plays are suppressed until pricing "
            "resolves (a not-owned OUT with no priceable handcuff is not phone-worthy)."
        )
    # --- injury transitions (the live shock source) ---
    transitions = league_state.injury_transitions(conn, as_of=as_of, season=season, view=view)
    for tr in transitions:
        if tr["direction"] != "ruled_out":
            continue  # 'cleared' is briefing context, not a phone push
        position = (tr["position"] or "").strip().upper() or None
        espn_id = str(tr["espn_player_id"]) if tr["espn_player_id"] is not None else None
        is_own = own_team_id is not None and tr["on_team_id"] == own_team_id
        # Keep the fantasy SKILL positions; keep a None-position player only when he
        # is the operator's own (edge guard). An owned K/DST ruled OUT is NOT a
        # skill-starter shock and must not fire a high-severity injury alert that
        # would outrank a real handcuff opportunity (audit D6).
        keep = position in FANTASY_INJURY_POSITIONS or (position is None and is_own)
        if not keep:
            continue

        to_status = (tr["to_status"] or "").strip().upper()
        season_ending = to_status == _SEASON_ENDING
        base_sev = _SEV_INJURY_RESERVE if season_ending else _SEV_INJURY_OUT
        severity = base_sev + (_OWN_BUMP if is_own else 0.0)
        status_phrase = _status_phrase(to_status)

        # Handcuff enrichment: QB/RB/TE, backup a FA right now. An injury vacancy is
        # NOT a bye — a player ruled OUT is HURT, so a this-week bye of his NFL team
        # is irrelevant to whether his handcuff is worth grabbing for the rest of the
        # season. (An earlier bye-gate here wrongly dropped the grab for an
        # injured-on-bye starter — audit D5/D6.)
        handcuff_name = handcuff_espn = None
        detail: list[str] = []
        link = handcuffs.get(espn_id) if espn_id else None
        if (
            link is not None
            and position in HANDCUFF_POSITIONS
            and link.backup_espn_id
            and league_state.who_held(
                conn, as_of=as_of, season=season, espn_player_id=link.backup_espn_id, view=view
            ) is None
        ):
            handcuff_name = link.backup_name
            handcuff_espn = link.backup_espn_id
            detail.extend(link.reasons)

        # headline: own-player-down leads; a not-owned handcuff play leads with the grab.
        if is_own and handcuff_name:
            headline = (
                f"YOUR {tr['player']} ({position or '?'}) is {status_phrase} — "
                f"grab his handcuff {handcuff_name} (free agent)"
            )
        elif is_own:
            headline = f"YOUR {tr['player']} ({position or '?'}) is {status_phrase}"
            detail.append(
                "no free-agent handcuff identified — check the waiver claims in your "
                "briefing for a replacement."
            )
        elif handcuff_name:
            headline = (
                f"{tr['player']} ({tr['pro_team']} {position}) is {status_phrase} -> "
                f"handcuff {handcuff_name} is a FREE AGENT, grab him"
            )
        else:
            # not own, and no available handcuff -> not alert-worthy for the phone
            continue

        events.append(
            AlertEvent(
                kind="INJURY_OUT",
                player=tr["player"],
                position=position,
                team=tr["pro_team"],
                gsis_id=tr["gsis_id"],
                espn_id=espn_id,
                on_team_id=tr["on_team_id"],
                is_own=is_own,
                headline=headline,
                detail=tuple(detail),
                source="ESPN league state (live)",
                event_day=tr["became_knowable"],
                severity=severity,
                dedup_key=f"inj:{espn_id}:{tr['became_knowable']}:{tr['direction']}",
                handcuff_name=handcuff_name,
                handcuff_espn_id=handcuff_espn,
            )
        )

    # --- news wire (low severity: context, never outranks a real OUT) ---
    events.extend(
        _news_events(
            conn, as_of=as_of, season=season, own_roster=own_roster,
            own_team_id=own_team_id, lookback_days=news_lookback_days, view=view,
        )
    )

    events.sort(key=lambda e: (-e.severity, e.event_day or "", e.player or ""))
    return AlertBoard(
        events=tuple(events),
        season=season,
        as_of=str(as_of),
        week=resolved_week,
        own_team_id=own_team_id,
        notes=tuple(notes),
    )


def _news_events(conn, *, as_of, season, own_roster, own_team_id, lookback_days, view):
    """News about a player the operator owns or could roster (a FA), as low-tier
    events. News precision is UNTUNED (a feature article and an injury note look
    alike here) — bounded by the run layer's rate cap; 4.2 tunes it."""
    from datetime import date, timedelta

    from ziggurat.data.asof import normalize_as_of
    from ziggurat.data.nfl import news as news_mod

    try:
        since = (normalize_as_of(as_of) - timedelta(days=lookback_days)).isoformat()
    except Exception:  # pragma: no cover - as_of already validated upstream
        since = None
    articles = news_mod.recent_news(conn, as_of=as_of, since=since, view=view)
    out: list[AlertEvent] = []
    for art in articles:
        for p in art["players"]:
            espn_id = str(p["espn_id"]) if p["espn_id"] is not None else None
            if espn_id is None:
                continue
            is_own = espn_id in own_roster
            if not is_own:
                held = league_state.who_held(
                    conn, as_of=as_of, season=season, espn_player_id=espn_id, view=view
                )
                if held is not None:  # rostered by someone else -> not actionable news
                    continue
            out.append(
                AlertEvent(
                    kind="NEWS",
                    player=p["player_name"] or "(player)",
                    position=None,
                    team=p["team"],
                    gsis_id=p["gsis_id"],
                    espn_id=espn_id,
                    on_team_id=own_team_id if is_own else None,
                    is_own=is_own,
                    headline=f"News: {art['headline']}",
                    detail=(art["body"],) if art["body"] else (),
                    source=f"news wire ({art['source']})",
                    event_day=(art["published_at"] or "")[:10],
                    severity=_SEV_NEWS + (_OWN_BUMP if is_own else 0.0),
                    dedup_key=f"news:{art['source']}:{art['news_id']}",
                )
            )
    return out


def format_alert_line(event: AlertEvent) -> str:
    """A single novice-legible line for the intel/weekly alert log (with WHY)."""
    head = f"[{event.kind}] {event.headline}"
    if event.detail:
        head += "\n    - " + "\n    - ".join(event.detail)
    return head
