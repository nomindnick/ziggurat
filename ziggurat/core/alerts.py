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
    must never outrank a real OUT on the phone lane. Since item 3.16 the
    own-roster NEWS arm is additionally gated on ESPN's ``news_type`` (a LABELLED
    HYPOTHESIS with a review date — see ``NEWS_GATE_LABEL``); the ``INJURY_OUT``
    arm is deliberately untouched by that gate and always pushes.

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


# ===================== item 3.16: the NEWS arm's content gate =====================
# A LABELLED HYPOTHESIS WITH A REVIEW DATE, NOT A RULE (operator decision on record,
# 2026-09-13: "yes, with two conditions — build it after 2026-09-15, and review it
# after a month"). It narrows a lane the operator chose, on one post-hoc window, so
# it is dated and it says so on every surface it touches.
#
# What it gates: the OWN-ROSTER NEWS arm ONLY. `INJURY_OUT` is deliberately UNGATED
# and always pushes — a player ruled OUT names an action whatever the wire calls the
# article. Pinned by `test_injury_out_is_never_touched_by_the_news_gate`.

#: ESPN stamps every wire article with a `type`. Only these reach the phone.
#: ALLOWLIST, not denylist: an unrecognised or renamed type is CONTEXT until it
#: explicitly opts in — the same safe-by-default discipline `phone_worthy` already
#: applies to a new event KIND. The cost of that default is a lane that could go
#: quiet if ESPN renames the type, which is why every tick that withholds something
#: says so in `AlertBoard.notes` and in the on-box alert log, rather than dropping
#: it silently.
NEWS_PHONE_TYPES = frozenset({"HeadlineNews"})

#: One month from the ship date. The alert status page reads REVIEW DUE once the day
#: arrives — a review date nobody is reminded of is a review date that does not exist.
NEWS_GATE_ADOPTED = "2026-09-15"
NEWS_GATE_REVIEW_DATE = "2026-10-15"

#: The evidence, quoted wherever the gate is disclosed (Rule 6). Re-measured on the
#: live `alert_ledger` 2026-09-15; item 3.16 recorded 35 pushes / 14 Media from the
#: same window read partway through its final day, and both rows that landed after
#: that read were Media — so the KEPT count is 9 on either read.
NEWS_GATE_LABEL = (
    f"HYPOTHESIS, not a rule (item 3.16, adopted {NEWS_GATE_ADOPTED}, REVIEW "
    f"{NEWS_GATE_REVIEW_DATE}): the own-roster NEWS phone lane pushes only ESPN "
    f"news_type {sorted(NEWS_PHONE_TYPES)}. Evidence — over the 13-day Week-1 window "
    "(2026-09-01..09-13) the ledger holds 37 own-roster NEWS pushes: 9 HeadlineNews "
    "(9/9 carrying a beat byline, median body 174 chars), 16 Media (0/16 bylined, "
    "median 59) and 12 Story (12/12 bylined, median 110). Every decision-relevant "
    "push of that window was HeadlineNews; every journal-named piece of fluff was a "
    "bylineless Media row. It is n=37 on ONE window, chosen POST HOC, and it does "
    "drop Story/Media rows with watch value — which is why it carries a review date "
    "instead of being a rule. Nothing is lost: a withheld item still lands in the "
    "Wednesday briefing and in the on-box alert log."
)

#: Rule-6 disclosure WIRED TO NO GATE (item 3.16 deliberately did NOT adopt edge
#: probe E18's WATCH gate). It is printed because it is true, not because anything
#: filters on it: at that magnitude a "could this headline clear the flip margin?"
#: test passes essentially everything and would have suppressed ~0 of the 37 Week-1
#: pushes, so wiring it would buy a rule that does nothing while reading like a
#: safeguard.
WAIVER_FLIP_MARGIN_DISCLOSURE = (
    "Rule-6 disclosure, WIRED TO NO GATE: the measured waiver displacement margin "
    "over Week 1 is 0.018-0.053 house pts/week (edge probe E18) — that is how much a "
    "re-valuation would have to move a player to change the claim order. Nothing in "
    "this lane filters on it. (E18's related 'a perfect oracle could not have moved "
    "the waiver lane by more than +0.46 pts/wk' is false as written and is not used: "
    "every gain in that matrix is computed FROM the feed's own point estimates, and "
    "on 2026-09-09 one football fact moved one player's season projection by 227.50 "
    "points.)"
)


def news_phone_gate(article) -> str | None:
    """Return ``None`` when this wire article may reach the phone, else the WHY.

    ``article`` is a ``news.recent_news()`` row (or any mapping carrying
    ``news_type`` / ``byline``). The gate is on TYPE ALONE; the byline is recorded
    in the reason as the review's evidence, never as a second condition — the
    one-month review has to be able to ask "did we ever withhold a BYLINED Media
    row?", and that question is only answerable if the withholding said so.
    """
    news_type = (article.get("news_type") or "").strip()
    if news_type in NEWS_PHONE_TYPES:
        return None
    shown = news_type or "(untyped)"
    byline = (article.get("byline") or "").strip()
    byline_clause = f", byline {byline!r}" if byline else ", no byline"
    return (
        f"held back from the phone: ESPN news_type={shown}{byline_clause} — context, "
        f"not an action (item 3.16 gate, HYPOTHESIS, review {NEWS_GATE_REVIEW_DATE}). "
        "It is in the Wednesday briefing and the on-box alert log."
    )


def _review_days_out(today) -> int:
    from datetime import date

    def _d(x):
        return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])

    return (_d(NEWS_GATE_REVIEW_DATE) - _d(today)).days


def format_phone_lane_policy(*, today=None) -> str:
    """The standing description of WHAT REACHES THE PHONE (items 3.6 + 3.16), for
    the alert status page.

    ``today`` is a DISPLAY clock (default: this box's today), never a data gate —
    it only decides whether the review date reads as upcoming or DUE. The text
    lives here, in the one module that owns the gate, so the page and the gate can
    never drift apart (the 3.8A lesson: two renderers of one rule disagreed, and
    the operator had no way to tell which was lying).
    """
    from datetime import date

    today = today or date.today()
    try:
        days = _review_days_out(today)
    except (TypeError, ValueError):  # pragma: no cover - a malformed display clock
        when, due = "(date unreadable)", "review"
    else:
        when = (f"in {days} day(s)" if days > 0
                else "TODAY" if days == 0
                else f"OVERDUE by {-days} day(s)")
        due = "review" if days > 0 else "REVIEW DUE"
    return "\n".join([
        "phone lane — what actually reaches the phone (items 3.6 + 3.16):",
        "  INJURY_OUT   ALWAYS pushes. UNGATED: a player ruled OUT names an action",
        "               (seat someone else / grab the handcuff). The item-3.16 news",
        "               gate does not touch this arm.",
        "  NEWS         own-roster only (operator decision 2026-08-05) AND ESPN",
        f"               news_type in {sorted(NEWS_PHONE_TYPES)} (item 3.16).",
        f"               {due} {NEWS_GATE_REVIEW_DATE} ({when}).",
        f"               {NEWS_GATE_LABEL}",
        "  everything else is computed for the Wednesday briefing and the on-box",
        "  alert log, and never pushed.",
        f"  {WAIVER_FLIP_MARGIN_DISCLOSURE}",
    ])


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
    #: The phone-push policy (operator decision 2026-08-05): a push must NAME AN
    #: ACTION. INJURY_OUT always does (seat someone else / grab the handcuff);
    #: NEWS qualifies only for a player on the operator's own roster — the news
    #: wire is the speed layer for "your starter just went down" (league sync is
    #: 4x/day, the news tick 20-min). Everything else is briefing/alert-log
    #: context, computed but never pushed. Safe-by-default: a new kind does not
    #: reach the phone until it explicitly opts in.
    #: SINCE ITEM 3.16 an own-roster NEWS event must ALSO clear the news_type gate
    #: (see ``news_phone_gate``); INJURY_OUT is untouched by it.
    phone_worthy: bool = False
    #: The wire article's ESPN ``type`` (NEWS only) — carried so the gate's decision
    #: is auditable from the alert log the one-month review will read.
    news_type: str | None = None
    #: Why this event did NOT reach the phone, when an OWN-roster NEWS item was
    #: withheld by the item-3.16 gate. ``None`` everywhere else — including on a
    #: not-owned news item, which is withheld by the older action-only rule and not
    #: by this gate. A withheld row is never silent: this string is rendered by
    #: ``format_alert_line`` and counted in ``AlertBoard.notes``.
    phone_gate: str | None = None


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
                phone_worthy=True,  # every surviving INJURY_OUT names an action
            )
        )

    # --- news wire (low severity: context, never outranks a real OUT) ---
    news_events = _news_events(
        conn, as_of=as_of, season=season, own_roster=own_roster,
        own_team_id=own_team_id, lookback_days=news_lookback_days, view=view,
    )
    events.extend(news_events)

    # The item-3.16 gate never drops an event silently. A tick that withheld
    # something says so HERE, with the count and the type breakdown, so the note
    # reaches the alert log and the Wednesday briefing; the standing policy text
    # itself lives on `ziggurat alerts status` rather than on every 20-minute tick.
    withheld = [e for e in news_events if e.phone_gate]
    if withheld:
        counts: dict[str, int] = {}
        for e in withheld:
            counts[(e.news_type or "(untyped)")] = counts.get(e.news_type or "(untyped)", 0) + 1
        breakdown = ", ".join(f"{n}x {t}" for t, n in sorted(counts.items()))
        notes.append(
            f"phone lane: {len(withheld)} own-roster news item(s) held back from the "
            f"phone by the item-3.16 news_type gate ({breakdown}). They are listed "
            f"below and in this briefing — withheld from the PUSH, not from you. "
            f"{NEWS_GATE_LABEL}"
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
    events.

    TWO gates stack here, and they are different rules with different ages:
      1. ACTION-ONLY (operator decision 2026-08-05): only OWN-roster news can be
         phone-worthy at all. FA/context news is computed for the briefing and the
         alert log but never pushed.
      2. THE news_type GATE (item 3.16, 2026-09-15, a labelled hypothesis with a
         review date): of the own-roster news, only ``NEWS_PHONE_TYPES`` reaches the
         phone. The rest keeps a ``phone_gate`` reason so the withholding is visible
         on every surface and countable by the one-month review.
    News precision remains otherwise UNTUNED (a feature article and an injury note
    still look alike beyond the type)."""
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
            # Gate 2 (item 3.16) applies to OWN-roster news only: a not-owned item is
            # already non-phone-worthy under the older action-only rule, and tagging it
            # with this gate's reason would blame the wrong rule for the silence.
            gate_reason = news_phone_gate(art) if is_own else None
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
                    phone_worthy=is_own and gate_reason is None,
                    news_type=art.get("news_type"),
                    phone_gate=gate_reason,
                )
            )
    return out


def format_alert_line(event: AlertEvent) -> str:
    """A single novice-legible line for the intel/weekly alert log (with WHY).

    A row the item-3.16 gate held back from the phone is rendered WITH its reason:
    the item's whole claim is that these rows are still delivered, just not as an
    interruption, and a row that arrived with no sign it had been routed would make
    that claim unfalsifiable.
    """
    head = f"[{event.kind}] {event.headline}"
    if event.detail:
        head += "\n    - " + "\n    - ".join(event.detail)
    if event.phone_gate:
        head += f"\n    - {event.phone_gate}"
    return head
