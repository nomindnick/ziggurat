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
grab is offered only when the backup is genuinely UNROSTERED right now (his own
CURRENT league-state row says so — a player ESPN does not list at all is not
offered), is not himself OUT / on IR / DOUBTFUL / suspended, and is labelled with
how he is actually acquired (FREE AGENT = click now; WAIVERS = a claim that clears
overnight). An injury vacancy is not a bye. All code-enforced with tests (Rule 6).

ITEM 3.16b (operator yes 2026-10-05) narrows the INJURY_OUT arm for SOMEONE ELSE'S
player. YOUR player ruled out still always pushes. Someone else's player reaches the
phone only when his backup prices at least ``HANDCUFF_PHONE_MIN_GAIN`` house points
for YOUR roster on the waiver tool's own board (``marginal.build_board`` — the same
pricing ``ziggurat waivers`` uses, never a new score). And every injury event is
checked against the player's CURRENT status: a crossing he has since come back from
is history, not news (the Penix 09-18 push reported an 11-day-old ruling that ESPN
had reversed three days before the push).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ziggurat.core import candidates as candidates_mod
from ziggurat.core import marginal
from ziggurat.core.lineup import active_players
from ziggurat.core.valuation import DEFAULT_ROSTER, weekly_lines
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


# ================ item 3.16b: the INJURY_OUT arm's handcuff gate ================
# Operator decision 2026-10-05 ("go ahead and implement the alert-narrowing
# feature"), after four weeks of journal evidence: 27 of the season's 32 injury
# pushes were about SOMEONE ELSE'S player, and the journals graded every one of them
# no-action. YOUR player is untouched by this gate — pinned by
# `test_own_player_out_is_never_touched_by_the_handcuff_gate`.

#: A backup in one of these designations cannot fill the hole this week, so a
#: "grab him" push is false on its face (Charbonnet OUT 10-02, Ferguson on IR 10-05,
#: Tua DOUBTFUL 09-18 — all three were pushed as grabs).
BACKUP_UNAVAILABLE_STATUSES = frozenset({"OUT", "INJURY_RESERVE", "DOUBTFUL", "SUSPENSION"})

#: The pricing floor, in house points over the board's own window (the rest of the
#: season). +1.0 is the candidate gate written in the week-2 journal, and it sits
#: just above the measured waiver displacement margin (0.018-0.053/wk, i.e. at most
#: ~0.7 over a 13-week window): a smaller number is inside the noise the claim order
#: itself cannot see.
HANDCUFF_PHONE_MIN_GAIN = 1.0

#: Someone else's player is phone NEWS only in the week his ruling lands. An older
#: crossing he is still out from is already in the ledger if it ever priced, and the
#: briefing's ALERTS block is "since the last check", not a season ledger.
NON_ROSTER_LOOKBACK_DAYS = 7

INJURY_GATE_ADOPTED = "2026-10-05"
INJURY_GATE_REVIEW_DATE = "2026-11-05"

INJURY_GATE_LABEL = (
    f"HYPOTHESIS, not a rule (item 3.16b, adopted {INJURY_GATE_ADOPTED}, REVIEW "
    f"{INJURY_GATE_REVIEW_DATE}): someone else's player ruled OUT reaches the phone "
    f"only when his backup is unrostered, is not himself out, and is worth at least "
    f"+{HANDCUFF_PHONE_MIN_GAIN:.1f} house pts (PROJECTED, rest of season) to YOUR "
    f"roster on the waiver tool's own board. Evidence — the ledger's 27 pushes about "
    f"someone else's player (2026-08-04..10-05), replayed at each push's own date: "
    f"16 held on price, 7 never formed (the player was already back, the ruling "
    f"was superseded by a later one, or the backup was himself out), 1 still pushed (Etienne OUT -> Kamara, +4.5, which became a "
    f"Tuesday claim target), and 3 cannot be replayed (a day's last sync overwrites "
    f"its intraday status). One window, chosen after the fact. The board OVER-values "
    f"a bench body (its own 'static roster' caveat), so a backup quarterback can "
    f"clear this floor on depth alone. Nothing is lost: a held item still lands in "
    f"the Wednesday briefing and in the on-box alert log, with the price that held it."
)


@dataclass(frozen=True)
class HandcuffPrice:
    """What one free backup is worth to the operator's roster (item 3.16b).

    ``gain`` is the best STANDALONE season-long move that adds him on the waiver
    tool's board: the best (add him, drop X) swap, or the pure add into an open
    active slot when there is one. ``None`` means the board found NO positive legal
    move for him (the swap matrix keeps positive rows only) or could not price him
    at all — ``why`` says which. Never a new score: it is ``SwapRow.gain`` /
    ``MarginalBoard.value_after`` verbatim (Rule 2)."""

    gain: float | None
    drop: str | None
    weeks: int
    why: str | None = None


#: The pricer seam: given the backups' current league-state rows, return their
#: prices keyed by ESPN id. Tests inject a stub; production builds ONE board.
HandcuffPricer = Callable[..., Mapping[str, HandcuffPrice]]


def price_handcuffs(
    conn,
    *,
    as_of,
    season: int,
    own_team_id: int,
    backup_rows: Sequence[Mapping],
    weeks: Sequence[int],
    lines=None,
    source: str = "sleeper_rotowire",
    view: league_state.base.AsOfView = "historical",
) -> dict[str, HandcuffPrice]:
    """Price every candidate backup against YOUR roster in ONE ``build_board`` call.

    The pool is exactly ``backup_rows`` (so the board's projection cut cannot drop
    one), the roster is your full current roster (IR included, which the board
    excludes from the lineup itself), and ``lines`` is the map the alert tick
    already read for ``handcuff_links`` — handing it over saves a second full
    projections read (item 3.16b; measured 6.8 s on the live DB)."""
    ids = {str(r["espn_player_id"]) for r in backup_rows if r.get("espn_player_id") is not None}
    roster = [dict(r) for r in league_state.get_player_state(
        conn, as_of=as_of, season=season, on_team_id=own_team_id, view=view)]
    n_weeks = len(list(weeks))
    if not roster:
        return {i: HandcuffPrice(None, None, n_weeks, "no roster rows to price against")
                for i in ids}
    board = marginal.build_board(
        conn, as_of=as_of, season=season, roster=roster,
        pool=[dict(r) for r in backup_rows], weeks=list(weeks),
        source=source, view=view, lines=lines,
    )
    # Every number here is SEASON-LONG on the board's own model. A row whose DROP is
    # a streamed position (D/ST, K) carries a one-week `gain` on `model_now` — e.g.
    # "drop your second D/ST", often the cheapest real drop — so it is re-priced
    # with `season_long_delta`, the board's own other-horizon number. Keyed on the
    # drop's POSITION, never on `horizon_weeks`: in the last week of the window every
    # row has horizon 1, and filtering on that silently priced every backup at None.
    best: dict[str, tuple[float, marginal.SwapRow]] = {}
    for row in board.swaps:
        aid = str(row.add_espn_id) if row.add_espn_id is not None else None
        if aid not in ids:
            continue
        gain = (board.season_long_delta(row)
                if row.drop_position in marginal.STREAMED_POSITIONS else row.gain)
        if aid not in best or gain > best[aid][0]:
            best[aid] = (gain, row)
    out: dict[str, HandcuffPrice] = {}
    open_slot = len(active_players(roster)) < DEFAULT_ROSTER.active_slots
    base_value = board.value_after() if open_slot and best else None
    counts = board.roster_position_counts
    for aid in ids:
        hit = best.get(aid)
        if hit is None or hit[0] <= 0.0:
            out[aid] = HandcuffPrice(
                None, None, n_weeks,
                "no positive add/drop move for him on your roster (or no projection "
                "to price him on)")
            continue
        gain, row = hit
        drop = row.drop
        # A pure add into an open slot RAISES his position's count, so it must clear
        # the same fence the claim planner applies (`waiver._select_claims` phase A,
        # `cap_ok(s, True)`): the board's effective cap, which already folds in the
        # league's own limits. Unchecked, a 4th TE in a 3-TE league priced +24.4
        # "into your open slot" — a move ESPN refuses.
        cap = board.position_caps.get(row.add_position)
        pure_allowed = cap is None or counts.get(row.add_position, 0) + 1 <= cap
        if open_slot and pure_allowed:
            # `pure_adds` reads only the ADD's key, so any row for him serves.
            pure = board.value_after(pure_adds=(row,)) - base_value
            if pure > gain:
                gain, drop = pure, None
        out[aid] = HandcuffPrice(gain, drop, n_weeks)
    return out


def _acquire_phrase(roster_status) -> str:
    """How a free backup is actually acquired. ESPN's own token decides: the push
    used to call every unrostered player a FREE AGENT, including Kamara and Gordon on
    09-28, who were on WAIVERS and could not be clicked that night."""
    tok = str(roster_status or "").strip().upper()
    if tok == "WAIVERS":
        return "on WAIVERS (a claim — it clears in the overnight batch)"
    if tok == "FREEAGENT":
        return "a FREE AGENT (first come, first served — add him now)"
    return "unrostered (ESPN status unknown — check before acting)"


def _short_acquire(roster_status) -> str:
    tok = str(roster_status or "").strip().upper()
    return {"WAIVERS": "on WAIVERS", "FREEAGENT": "a FREE AGENT"}.get(tok, "unrostered")


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


def _d10(x):
    from datetime import date

    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def _review_days_out(today) -> int:
    return (_d10(NEWS_GATE_REVIEW_DATE) - _d10(today)).days


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
    try:
        inj_days = (_d10(INJURY_GATE_REVIEW_DATE) - _d10(today)).days
    except (TypeError, ValueError):  # pragma: no cover - a malformed display clock
        inj_when, inj_due = "(date unreadable)", "review"
    else:
        inj_when = (f"in {inj_days} day(s)" if inj_days > 0
                    else "TODAY" if inj_days == 0
                    else f"OVERDUE by {-inj_days} day(s)")
        inj_due = "review" if inj_days > 0 else "REVIEW DUE"
    return "\n".join([
        "phone lane — what actually reaches the phone (items 3.6 + 3.16 + 3.16b):",
        "  INJURY_OUT   YOUR player ruled out ALWAYS pushes. UNGATED: it names an",
        "               action (seat someone else / grab his handcuff). Neither",
        "               gate below touches it.",
        "               SOMEONE ELSE'S player pushes only when his backup is",
        "               unrostered, healthy enough to play, and worth at least",
        f"               +{HANDCUFF_PHONE_MIN_GAIN:.1f} house pts (PROJECTED, rest of season) to your",
        f"               roster, in the {NON_ROSTER_LOOKBACK_DAYS} days after the ruling (item 3.16b).",
        f"               {inj_due} {INJURY_GATE_REVIEW_DATE} ({inj_when}).",
        f"               {INJURY_GATE_LABEL}",
        "               Every injury push is checked against the player's CURRENT",
        "               status: one he has since come back from is never pushed.",
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
    #: Why this event did NOT reach the phone: an OWN-roster NEWS item withheld by
    #: the item-3.16 gate, or someone else's INJURY_OUT held by the item-3.16b
    #: handcuff gate. ``None`` everywhere else — including on a not-owned news item,
    #: which is withheld by the older action-only rule and not by either gate. A
    #: withheld row is never silent: this string is rendered by
    #: ``format_alert_line`` and counted in ``AlertBoard.notes``.
    phone_gate: str | None = None
    #: Item 3.16b: what the backup is worth to YOUR roster (the board's own number),
    #: when someone else's player was priced. ``None`` = not priced.
    handcuff_gain: float | None = None


@dataclass(frozen=True)
class AlertBoard:
    events: tuple[AlertEvent, ...]   # all alert-worthy candidates, severity desc
    season: int
    as_of: str
    week: int | None
    own_team_id: int | None
    notes: tuple[str, ...]           # degradation / honesty disclosures (Rule 6)
    #: Item 3.16b: why this tick could not price someone else's backups (the gate then
    #: held every one of them). Surfaced so the alert tick records a PARTIAL run —
    #: a persistent pricer bug must not read as a healthy "empty" tick.
    price_error: str | None = None


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
    handcuff_pricer: HandcuffPricer | None = None,
) -> AlertBoard:
    """Compute the alert-worthy events knowable at ``as_of``. Pure read.

    ``handcuff_pricer`` is the item-3.16b seam (tests inject a stub); the default
    prices every candidate backup in ONE ``price_handcuffs`` board, reusing the
    ``weekly_lines`` map this function already read for ``handcuff_links``."""
    notes: list[str] = []
    events: list[AlertEvent] = []
    own_roster = _own_roster_espn(conn, as_of=as_of, season=season, own_team_id=own_team_id, view=view)

    # --- handcuff links: price only if the week window resolves. ---
    handcuffs: dict[str, marginal.HandcuffLink] = {}
    resolved_week = week
    weeks: list[int] = []
    lines = None
    try:
        if week is not None:
            weeks = list(range(week, last_week + 1))
            resolved_week = week
        else:
            weeks = list(
                marginal.resolve_weeks(conn, as_of=as_of, season=season, last_week=last_week, view=view)
            )
            resolved_week = weeks[0]
        # ONE projections read serves both the depth chart and the 3.16b pricer.
        lines = weekly_lines(conn, as_of=as_of, season=season, weeks=weeks,
                             source=source, view=view)
        links = marginal.handcuff_links(
            conn, as_of=as_of, season=season, weeks=weeks,
            last_week=last_week, source=source, view=view, lines=lines,
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
    # `injury_transitions` replays EVERY crossing of the season on every tick. A
    # crossing is a fact about the past; whether it is NEWS is decided against the
    # player's CURRENT row (item 3.16b): back from it -> history, never pushed; owner
    # changed since -> the CURRENT owner decides YOUR vs someone else's.
    transitions = league_state.injury_transitions(conn, as_of=as_of, season=season, view=view)
    current: dict[str, Mapping | None] = {}

    def _now_row(pid):
        if pid is None:
            return None
        pid = str(pid)
        if pid not in current:
            rows = league_state.get_player_state(
                conn, as_of=as_of, season=season, espn_player_id=pid, view=view)
            current[pid] = dict(rows[0]) if rows else None
        return current[pid]

    try:
        today_d = _d10(as_of)
    except (TypeError, ValueError):  # pragma: no cover - as_of validated upstream
        today_d = None
    stale = 0
    acquired_after = 0
    backup_out: list[str] = []
    too_old = 0
    pending: list[dict] = []   # someone else's player, awaiting the 3.16b price
    # Only a player's LATEST ruling can be current: ESPN re-designates a long-term
    # injury every week, so one injured player carries a new crossing each time he
    # is cleared and ruled out again (Reed held three by 09-30). The earlier ones are
    # history, superseded — listing them made the briefing name him twice.
    latest: dict[str, dict] = {}
    for tr in transitions:  # ascending by became_knowable
        if tr["direction"] == "ruled_out" and tr["espn_player_id"] is not None:
            latest[str(tr["espn_player_id"])] = tr
    for tr in transitions:
        if tr["direction"] != "ruled_out":
            continue  # 'cleared' is briefing context, not a phone push
        if tr["espn_player_id"] is not None and latest.get(str(tr["espn_player_id"])) is not tr:
            continue  # superseded by a later ruling for the same player
        position = (tr["position"] or "").strip().upper() or None
        espn_id = str(tr["espn_player_id"]) if tr["espn_player_id"] is not None else None
        now = _now_row(espn_id)
        holder = now["on_team_id"] if now is not None else tr["on_team_id"]
        is_own = own_team_id is not None and holder == own_team_id
        # Ruled out while he was ALREADY yours? A player you acquired after his
        # ruling (an IR stash, a pre-draft OUT you then drafted) is not news to you:
        # pushing "YOUR X is on IR" the tick after you claim him would be the same
        # history-as-news defect this item fixes for everyone else.
        ours_at_ruling = own_team_id is not None and tr["on_team_id"] == own_team_id
        # Keep the fantasy SKILL positions; keep a None-position player only when he
        # is the operator's own (edge guard). An owned K/DST ruled OUT is NOT a
        # skill-starter shock and must not fire a high-severity injury alert that
        # would outrank a real handcuff opportunity (audit D6).
        keep = position in FANTASY_INJURY_POSITIONS or (position is None and is_own)
        if not keep:
            continue
        now_status = (now["injury_status"] or "").strip().upper() if now is not None else None
        if now is not None and now_status not in league_state.HARD_OUT_STATUSES:
            # He is back (or merely QUESTIONABLE now): the headline would be false.
            # Counted only when it could have been an alert at all — yours, or a
            # QB/RB/TE starter with a mapped backup — so the note never claims credit
            # for silencing crossings no rule would ever have pushed.
            link0 = handcuffs.get(espn_id) if espn_id else None
            if is_own or (link0 is not None and position in HANDCUFF_POSITIONS):
                stale += 1
            continue

        to_status = (now_status or tr["to_status"] or "").strip().upper()
        season_ending = to_status == _SEASON_ENDING
        base_sev = _SEV_INJURY_RESERVE if season_ending else _SEV_INJURY_OUT
        severity = base_sev + (_OWN_BUMP if is_own else 0.0)
        status_phrase = _status_phrase(to_status)

        # Handcuff enrichment: QB/RB/TE, backup UNROSTERED and able to play right now.
        # An injury vacancy is NOT a bye — a player ruled OUT is HURT, so a this-week
        # bye of his NFL team is irrelevant to whether his handcuff is worth grabbing
        # for the rest of the season (audit D5/D6).
        handcuff_name = handcuff_espn = None
        backup_row = None
        detail: list[str] = []
        link = handcuffs.get(espn_id) if espn_id else None
        if link is not None and position in HANDCUFF_POSITIONS and link.backup_espn_id:
            brow = _now_row(link.backup_espn_id)
            b_status = (brow["injury_status"] or "").strip().upper() if brow else ""
            if brow is None or brow["on_team_id"] is not None:
                pass  # rostered (or not in ESPN's pool at all): nothing to grab
            elif b_status in BACKUP_UNAVAILABLE_STATUSES:
                backup_out.append(f"{link.backup_name} ({_status_phrase(b_status)})")
                detail.append(
                    f"his backup {link.backup_name} is himself {_status_phrase(b_status)} "
                    f"— not a grab.")
            else:
                handcuff_name = link.backup_name
                handcuff_espn = str(link.backup_espn_id)
                backup_row = brow
                detail.extend(link.reasons)
                if b_status in {"QUESTIONABLE", "DAY_TO_DAY"}:
                    detail.append(f"his backup {link.backup_name} is {b_status} himself.")

        base = dict(
            kind="INJURY_OUT", player=tr["player"], position=position,
            team=tr["pro_team"], gsis_id=tr["gsis_id"], espn_id=espn_id,
            on_team_id=holder, is_own=is_own, source="ESPN league state (live)",
            event_day=tr["became_knowable"], severity=severity,
            dedup_key=f"inj:{espn_id}:{tr['became_knowable']}:{tr['direction']}",
            handcuff_name=handcuff_name, handcuff_espn_id=handcuff_espn,
        )

        if is_own and not ours_at_ruling:
            acquired_after += 1
            events.append(AlertEvent(
                headline=f"YOUR {tr['player']} ({position or '?'}) is {status_phrase}",
                detail=tuple(detail), phone_worthy=False,
                phone_gate=(f"held back from the phone: he was ruled out on "
                            f"{tr['became_knowable']}, before he joined your roster, so "
                            f"it is not news to you (item 3.16b truth check)."),
                **base))
            continue
        if is_own:
            # YOUR player, ruled out while yours: UNGATED (item 3.16b pins it). It
            # always names an action.
            if handcuff_name:
                headline = (
                    f"YOUR {tr['player']} ({position or '?'}) is {status_phrase} — "
                    f"his handcuff {handcuff_name} is "
                    f"{_short_acquire(backup_row.get('roster_status'))}"
                )
                detail.append(f"{handcuff_name} is {_acquire_phrase(backup_row.get('roster_status'))}.")
            else:
                headline = f"YOUR {tr['player']} ({position or '?'}) is {status_phrase}"
                detail.append(
                    "no unrostered handcuff identified — check the waiver claims in your "
                    "briefing for a replacement."
                )
            events.append(AlertEvent(headline=headline, detail=tuple(detail),
                                     phone_worthy=True, **base))
            continue

        if not handcuff_name:
            continue  # someone else's player with nothing to grab: not an alert at all
        age = None
        if today_d is not None and tr["became_knowable"]:
            try:
                age = (today_d - _d10(tr["became_knowable"])).days
            except (TypeError, ValueError):  # pragma: no cover - stamps are ISO dates
                age = None
        if age is not None and age > NON_ROSTER_LOOKBACK_DAYS:
            too_old += 1  # the week his ruling landed has passed; not news any more
            continue
        pending.append(dict(base=base, detail=detail, status_phrase=status_phrase,
                            tr=tr, position=position, backup_row=backup_row))

    # --- item 3.16b: price every pending backup against YOUR roster, once. ---
    # (No "window unresolved" branch: without a window `handcuff_links` returns
    # nothing, so no event ever reaches here — the preseason note above covers it.)
    prices: Mapping[str, HandcuffPrice] = {}
    price_error = None
    if pending:
        rows_by_id = {str(p["backup_row"]["espn_player_id"]): p["backup_row"] for p in pending}
        if own_team_id is None:
            price_error = "no own team resolved, so nothing can be priced for YOUR roster"
        else:
            try:
                if handcuff_pricer is None:
                    prices = price_handcuffs(
                        conn, as_of=as_of, season=season, own_team_id=own_team_id,
                        backup_rows=list(rows_by_id.values()), weeks=weeks, lines=lines,
                        source=source, view=view,
                    )
                else:
                    prices = handcuff_pricer(list(rows_by_id.values()), weeks=weeks, lines=lines)
            except Exception as exc:  # pricing is a gate, never a crash of the tick
                price_error = f"pricing failed: {type(exc).__name__}: {exc}"
    held = 0
    for p in pending:
        tr, base, detail = p["tr"], p["base"], list(p["detail"])
        bid = str(p["backup_row"]["espn_player_id"])
        status_tok = p["backup_row"].get("roster_status")
        price = prices.get(bid)
        gain = price.gain if price is not None else None
        lead = (f"{tr['player']} ({tr['pro_team']} {p['position']}) is {p['status_phrase']} -> "
                f"his backup {base['handcuff_name']} is {_short_acquire(status_tok)}")
        if gain is not None and gain >= HANDCUFF_PHONE_MIN_GAIN:
            # The how-to-acquire imperative ("add him now") rides ONLY a pushed row: on
            # a held row it would sit directly above "held back ... below the floor".
            detail.append(f"{base['handcuff_name']} is {_acquire_phrase(status_tok)}.")
            weeks_n = price.weeks
            pair = f"dropping {price.drop}" if price.drop else "into your open roster slot"
            headline = (f"{lead}: worth {gain:+.1f} house pts to your roster over "
                        f"{weeks_n} wks ({pair}), PROJECTED")
            detail.append(
                f"priced on the waiver tool's own board: the best move that adds him is "
                f"{gain:+.1f} house pts over {weeks_n} weeks ({gain / max(weeks_n, 1):+.2f}/wk), "
                f"{pair}. A bench body's number is an UPPER BOUND (static-roster caveat). "
                f"Run `ziggurat waivers --reasons` before acting.")
            events.append(AlertEvent(headline=headline, detail=tuple(detail),
                                     phone_worthy=True, handcuff_gain=gain, **base))
            continue
        held += 1
        if price_error is not None:
            why = price_error
        elif gain is None:
            why = (price.why if price is not None and price.why
                   else "the waiver board found no positive move that adds him")
        else:
            why = (f"he is worth {gain:+.1f} house pts to your roster over {price.weeks} "
                   f"wks, below the +{HANDCUFF_PHONE_MIN_GAIN:.1f} floor")
        events.append(AlertEvent(
            headline=lead, detail=tuple(detail), phone_worthy=False, handcuff_gain=gain,
            phone_gate=(f"held back from the phone: {why} (item 3.16b gate, HYPOTHESIS, "
                        f"review {INJURY_GATE_REVIEW_DATE}). It is in the Wednesday "
                        f"briefing and the on-box alert log."),
            **base))

    if stale:
        notes.append(
            f"{stale} earlier injury ruling(s) about your players or about starters with "
            f"a backup skipped: the player is no longer OUT or on IR on ESPN today, so "
            f"'is OUT' would be false (item 3.16b).")
    if acquired_after:
        notes.append(
            f"{acquired_after} of your players are listed but not pushed: each was ruled "
            f"out before he joined your roster (item 3.16b).")
    if too_old:
        notes.append(
            f"{too_old} older ruling(s) about other teams' players not listed: each landed "
            f"more than {NON_ROSTER_LOOKBACK_DAYS} days ago (item 3.16b).")
    if backup_out:
        notes.append(
            "no grab offered for these backups, who cannot play themselves: "
            + "; ".join(sorted(set(backup_out))) + ".")
    if held:
        notes.append(
            f"phone lane: {held} injury alert(s) about other teams' players held back by "
            f"the item-3.16b handcuff gate — listed below, withheld from the PUSH, not "
            f"from you. {INJURY_GATE_LABEL}"
            + (f" This tick could not price: {price_error}." if price_error else ""))

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
        price_error=price_error,
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
