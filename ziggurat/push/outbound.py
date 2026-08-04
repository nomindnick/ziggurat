"""The outbound push channel to ntfy — and the Rule-5 boundary that guards it.

THE NEW SURFACE (item 3.6 R4). ntfy.sh is a THIRD-PARTY server, and on the cheap
config a topic is PUBLIC BY OBSCURITY (the topic name IS the password). Curl'ing
text there is a brand-new egress surface the committed-file guard (``repo_guard``,
which matches file PATHS) does not cover. The hard red line is the SAME as Rule 5's
— real names of OTHER league members and their team names — but the domain is
message TEXT, so it needs its own guard.

TWO LAYERS, belt AND suspenders, because "a guard narrower than the thing it
guards" is the exact class of bug this project keeps finding:
  1. CONSTRUCT, don't truncate. The teaser is BUILT by the briefing/alert code
     from allowlisted structured fields (public NFL player names, counts, the
     operator's OWN team name). It is never a slice of the free-text briefing,
     which may carry standings/opponent context. So the leak lane is removed by
     construction — the full briefing with any opponent detail stays in the
     gitignored intel/weekly/ file only.
  2. ``assert_publishable`` is a HARD data-driven gate before any POST: it reads
     the ACTUAL league-private string set from THIS league's DB (every team's
     name/abbrev/owner EXCEPT the operator's own) and refuses to publish text that
     contains any of them. Data-driven beats a static pattern list — it stays
     correct as team names change, and it is the text-domain analogue of
     ``repo_guard.violations``. The guarantee holds even on a world-readable topic,
     which is what makes the cheap public-topic config acceptable.

SINGLE CHOKE POINT (Rule-4-shaped): all outbound text goes through ``publish``,
which calls ``assert_publishable`` first. No other module shells curl / hits ntfy
(a test greps the tree). Egress is bounded (``net.HTTP_TIMEOUT``) via stdlib
``urllib`` — NOT ``requests`` (an undeclared transitive dep, and the seam
``net.py`` bounds cleanly is exactly urllib).
"""

import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from ziggurat import net
from ziggurat.league import state as league_state

#: ntfy's per-message body cap. A larger body is silently converted to an
#: ATTACHMENT file (a worse phone UX), so we refuse rather than send one. The
#: teaser is a few short lines; this is a backstop.
NTFY_MAX_BYTES = 4096

#: Abbrevs shorter than this are NOT matched by the scrub: ESPN caps team abbrevs
#: at 4 chars, and 2-3 char strings are where NFL team codes (GB, KC, SF) and
#: common words (OUT, ADD) collide — matching them would block legitimate
#: player-news teasers wholesale. A 4-char league abbrev is distinctive enough
#: that the false-positive rate on a player-name teaser is low. The team NAME
#: (matched unconditionally below) is the real identity leak; the abbrev is a
#: backstop, so this trades a rare false-negative on a 2-3 char abbrev for not
#: breaking the channel. Documented, not silent.
_MIN_ABBREV_LEN = 4


class OutboundBoundaryError(RuntimeError):
    """Outbound text would carry league-private material off-box (Rule 5), or
    exceeds the ntfy size cap, or the channel is misconfigured. Raised BEFORE any
    network send. The caller logs it as a failed push and does NOT retry with the
    same text."""


def league_private_strings(conn, *, as_of, season, own_team_id) -> set[str]:
    """The set of strings that must NEVER leave the box: every league team's
    name/abbrev/owner EXCEPT the operator's own (the operator's own team name is
    SPEC-permitted). Read from THIS league's DB through the as-of accessor.

    ``own_team_id`` is REQUIRED — passing None would include the operator's own
    team in the denylist (blocking a legitimate own-team teaser) AND, worse,
    signals the caller never resolved whose roster this is. Raises then, rather
    than guessing (the Rule-6 discipline: a wrong answer the operator cannot smell
    is worse than a refusal).
    """
    if own_team_id is None:
        raise OutboundBoundaryError(
            "league_private_strings needs own_team_id (resolve_own_team first); "
            "refusing to build a denylist without knowing whose team is the operator's"
        )
    teams = league_state.get_team_state(conn, as_of=as_of, season=season)
    private: set[str] = set()
    for row in teams:
        if row["team_id"] == own_team_id:
            continue  # the operator's own team name is allowed to cross (SPEC)
        name = (row["name"] or "").strip()
        owner = (row["primary_owner"] or "").strip()
        abbrev = (row["abbrev"] or "").strip()
        if name:
            private.add(name)
        if owner:
            private.add(owner)
        if len(abbrev) >= _MIN_ABBREV_LEN:
            private.add(abbrev)
    return private


def _contains_private(text: str, private: set[str]) -> str | None:
    """Return the matched private string (for a redacted diagnostic), or None.

    WORD/TOKEN-BOUNDARY match (audit D2), case-insensitive: a colleague team name
    is a leak only when it appears as a standalone token, NOT as a substring inside
    a longer word — so a team named 'Rivals' does not block a headline containing
    'Arrivals', and a >=4-char abbrev matches only when it stands alone. We search
    ``private in text`` (not the reverse), so a team name that CONTAINS a public
    player name does not false-positive on that player. The residual ambiguity — a
    colleague whose team name IS exactly a player's name — fail-safes to blocking
    (a HARD red line: refuse rather than leak).
    """
    for s in private:
        if not s:
            continue
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(s) + r"(?![A-Za-z0-9])", text, re.IGNORECASE):
            return s
    return None


def assert_publishable(text: str, *, conn, as_of, season, own_team_id, extra=(), strict=True) -> None:
    """Raise ``OutboundBoundaryError`` unless the outbound content is safe to send.

    Checks: (1) FAIL CLOSED — if ``strict`` and the league snapshot has no teams,
    the denylist cannot be built, so refuse rather than silently pass anything
    (audit D2); (2) no league-private team/owner string in the body OR the header
    strings (``extra`` = title/tags/click, all of which also leave the box); (3)
    the BODY is within the ntfy size cap (headers are not body-size-limited). The
    exception REDACTS the matched string (names the team_id, not the text) so a
    colleague identifier never lands in a log or run-log error column.
    """
    teams = league_state.get_team_state(conn, as_of=as_of, season=season)
    if strict and not teams:
        raise OutboundBoundaryError(
            "cannot verify outbound safety: no league snapshot at this as-of, so the "
            "league-private denylist is empty. Run `ziggurat league sync` before pushing."
        )
    private = league_private_strings(conn, as_of=as_of, season=season, own_team_id=own_team_id)
    combined = " ".join(s for s in (text, *extra) if s)  # everything that leaves the box
    hit = _contains_private(combined, private)
    if hit is not None:
        team_id = next(
            (r["team_id"] for r in teams
             if hit in ((r["name"] or "").strip(), (r["primary_owner"] or "").strip(),
                        (r["abbrev"] or "").strip())),
            "?",
        )
        raise OutboundBoundaryError(
            f"outbound text contains a league-private identifier for team {team_id} "
            f"(Rule 5); refusing to publish. The full detail belongs in the "
            f"intel/weekly/ file, not the phone teaser."
        )
    size = len(text.encode("utf-8"))
    if size > NTFY_MAX_BYTES:
        raise OutboundBoundaryError(
            f"outbound text is {size} bytes (> ntfy cap {NTFY_MAX_BYTES}); a larger "
            f"body becomes a silent attachment. Shorten the teaser."
        )


@dataclass(frozen=True)
class NtfyConfig:
    server: str
    topic: str
    token: str | None

    @property
    def url(self) -> str:
        return f"{self.server.rstrip('/')}/{self.topic}"


def load_ntfy_config(*, environ=None) -> NtfyConfig:
    """Read the ntfy channel config from the environment (loading the repo .env
    the same non-overriding way the ESPN credentials do).

    ``NTFY_TOPIC`` is REQUIRED and is a SECRET on a public topic (the topic name
    is the password) — treated like SWID: never printed/logged/committed.
    ``NTFY_SERVER`` defaults to https://ntfy.sh; ``NTFY_TOKEN`` is optional (only
    needed for a reserved/self-hosted deny-all topic — the privacy upgrade path).
    """
    if environ is None:
        try:  # optional: load the repo .env so the topic need not be exported
            from dotenv import load_dotenv

            from ziggurat.paths import REPO_ROOT

            load_dotenv(REPO_ROOT / ".env", override=False)
        except ImportError:  # pragma: no cover - dotenv is a declared dependency
            pass
        environ = os.environ
    topic = (environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        raise OutboundBoundaryError(
            "NTFY_TOPIC is not set; refusing to publish to an empty/wrong topic. "
            "Set NTFY_TOPIC (a high-entropy string — it is the topic's password) in .env."
        )
    server = (environ.get("NTFY_SERVER") or "https://ntfy.sh").strip()
    token = (environ.get("NTFY_TOKEN") or "").strip() or None
    return NtfyConfig(server=server, topic=topic, token=token)


@dataclass(frozen=True)
class PublishResult:
    ok: bool
    status: str        # http code as text | 'dry_run' | 'error: ...'
    bytes_sent: int


def _urllib_poster(url, body, headers, timeout):
    """The real ntfy POST seam (tests inject a fake). Bounded by net.HTTP_TIMEOUT
    — an unbounded urlopen under systemd Type=oneshot parks the alert cadence."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.status


def publish(
    text: str,
    *,
    conn,
    as_of,
    season,
    own_team_id,
    title: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    click: str | None = None,
    config: NtfyConfig | None = None,
    dry_run: bool = False,
    poster=_urllib_poster,
) -> PublishResult:
    """Publish ``text`` to the phone, AFTER the Rule-5 scrub. The one door.

    ``dry_run`` runs the scrub and builds the request but sends nothing (the
    default for a --no-push CLI run / tests). A network failure is a loud, caught
    error the caller logs as a failed push — never a silent swallow (the ingest
    run-log discipline).
    """
    # Scrub EVERYTHING that leaves the box — body AND the title/tags/click headers
    # (audit D2). A dry run does not fail-closed on an empty denylist (it is the
    # --no-push preview an operator runs before the league is synced / ntfy set up).
    assert_publishable(
        text, conn=conn, as_of=as_of, season=season, own_team_id=own_team_id,
        extra=(title, tags, click), strict=not dry_run,
    )
    body = text.encode("utf-8")
    if dry_run:
        return PublishResult(ok=True, status="dry_run", bytes_sent=len(body))
    cfg = config or load_ntfy_config()
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    # ntfy metadata headers must be latin-1-safe on the wire; a Title with a
    # non-latin-1 char would raise in urllib. Titles here are constructed from
    # ASCII labels + player names, but encode defensively.
    if title:
        headers["Title"] = title.encode("utf-8", "replace").decode("latin-1", "replace")
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"

    try:
        status = poster(cfg.url, body, headers, net.HTTP_TIMEOUT)
    except (urllib.error.URLError, OSError) as exc:
        # Do NOT include cfg.url in the message — the topic is a secret.
        return PublishResult(ok=False, status=f"error: {type(exc).__name__}", bytes_sent=len(body))
    ok = 200 <= int(status) < 300
    return PublishResult(ok=ok, status=str(status), bytes_sent=len(body))
