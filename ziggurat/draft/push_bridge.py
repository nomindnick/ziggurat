"""Bridge from the DELETABLE draft cockpit to the permanent push egress.

``draft/`` -> ``push/`` is the legal import direction (Rule 8 forbids only the
reverse), and every draft-night phone push leaves through the same Rule-5
choke point as the in-season cadence: ``push.outbound.publish`` with the
league-private-name scrub.

The cockpit is deliberately DB-free, but the scrub needs a conn to build the
denylist — so the bridge resolves ``own_team_id`` ONCE at build time (loud at
18:00, not silent at 19:40) and opens a short-lived conn per push. Draft
pushes are ACTION-ONLY by construction (the operator attention contract: name
an action or stay silent) and carry no player or league-member names; the
scrub still runs as belt and braces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DraftPushResult:
    ok: bool
    status: str


def make_draft_pusher(
    *,
    db_path: Path,
    season: int,
    as_of: str,
    team: int | None = None,
) -> Any:
    """Build ``pusher(title, body) -> DraftPushResult`` for the cockpit.

    Raises (loudly, at cockpit LAUNCH) when the push channel cannot work:
    the ntfy topic unset in .env, or the own-team resolution fails — a draft-night
    misconfiguration must surface while the operator is still at the keyboard.
    The returned callable itself never raises: a failed send is a
    ``DraftPushResult(ok=False, ...)`` the cockpit records and retries.
    """
    from ziggurat.data.nfl.espn_source import load_espn_credentials
    from ziggurat.data.store import connect
    from ziggurat.league.state import resolve_own_team
    from ziggurat.push import outbound

    config = outbound.load_ntfy_config()  # raises if the ntfy topic is unset

    conn = connect(db_path)
    try:
        if team is not None:
            own_team_id = int(team)
        else:
            creds = load_espn_credentials()
            own_team_id = resolve_own_team(
                conn, as_of=as_of, season=season, swid=creds["swid"]
            )
    finally:
        conn.close()

    def pusher(title: str, body: str) -> DraftPushResult:
        try:
            c = connect(db_path)
            try:
                res = outbound.publish(
                    body,
                    conn=c,
                    as_of=as_of,
                    season=season,
                    own_team_id=own_team_id,
                    title=title,
                    priority="high",
                    tags="rotating_light",
                    config=config,
                )
            finally:
                c.close()
            return DraftPushResult(ok=res.ok, status=res.status)
        except Exception as exc:  # a push must never take the cockpit down
            return DraftPushResult(ok=False, status=f"error: {type(exc).__name__}")

    return pusher
