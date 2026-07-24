"""Live ESPN league-state network seam (item 3.1).

The ONE network seam for league state, mirroring ``data/nfl/espn_source.py``:
thin functions that perform the raw HTTP pull and return parsed JSON, with the
third-party client imported lazily so the rest of the package (and the offline
test suite) never touches ``espn_api`` or the network. Tests patch these four
``fetch_*`` functions and feed captured fixture slices.

Why raw views instead of the ``espn_api`` wrapper objects: the wrapper drops
exactly the fields league state is about — entry-level ``onTeamId``/``status``,
``ownership`` percentages, ``acquisitionType``/``acquisitionDate``,
``waiverRank`` and ``transactionCounter``. Same reasoning as item 2.1's raw
``kona_player_info`` decision (design D9); we reuse that module's request layer
(``espn_source.league_client``) so ESPN auth and the ``lm-api-reads`` host are
configured in exactly one place.

RECON NOTE (2026-07-24, probed live): none of this is available historically —
``leagueHistory`` ignores ``scoringPeriodId`` on ``mRoster``, past-season box
scores carry no per-week roster, and past-season transactions/activity are empty
or 404. League history exists only because these pulls run on a cadence.
"""

import json
from importlib import import_module

# Kept in sync with espn_api's own activity filter (League.recent_activity):
# ADD/DROP/WAIVER/TRADE message type ids on the league communication feed.
_ACTIVITY_MSG_TYPES = [178, 180, 179, 239, 181, 244]

_TRANSACTION_TYPES = ["FREEAGENT", "WAIVER", "WAIVER_ERROR", "TRADE_ACCEPT", "ROSTER"]

# Views that make up one league-state snapshot. mSettings rides along so a
# mid-season settings change (roster/scoring/waiver) is visible in the raw pull.
STATE_VIEWS = ("mTeam", "mRoster", "mMatchupScore", "mStandings", "mSettings")


def _request(league, *, params, headers=None, extend=""):
    """Issue one authenticated league GET, converting ESPN's auth failures into a
    loud, actionable error.

    Cookie expiry is the expected operational failure of this whole item (spike
    1.1 flagged it): it must fail LEGIBLY rather than return an empty snapshot
    that a cron would happily write as "the league is empty today".
    """
    espn_requests = import_module("espn_api.requests.espn_requests")
    try:
        if extend:
            return league.espn_request.league_get(extend=extend, params=params, headers=headers)
        return league.espn_request.league_get(params=params, headers=headers)
    except (espn_requests.ESPNAccessDenied, espn_requests.ESPNInvalidLeague) as exc:
        raise RuntimeError(
            "ESPN rejected the league-state request (expired/invalid cookies?); "
            "refresh SWID/ESPN_S2 in .env"
        ) from exc


def _client(league_id, season, espn_s2, swid):
    from ziggurat.data.nfl import espn_source

    return espn_source.league_client(league_id, season, espn_s2, swid)


def fetch_league_state(*, league_id: int, season: int, espn_s2, swid) -> dict:
    """Pull one league-state snapshot: teams, rosters, matchups, standings, settings.

    Returns the raw league payload (``teams``, ``schedule``, ``status``,
    ``scoringPeriodId``, ``settings``, ...). One request, all views — ESPN serves
    them together and a single response keeps the snapshot internally consistent
    (rosters and standings from the same instant, not two pulls straddling a
    transaction).
    """
    league = _client(league_id, season, espn_s2, swid)
    data = _request(league, params={"view": list(STATE_VIEWS)})
    # The historical (leagueHistory) form returns a single-element array; the
    # current-season form returns the object. Accept both so a future backfill
    # attempt does not silently index a list.
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or "teams" not in data:
        raise RuntimeError(
            "ESPN league-state payload has no 'teams' key — the league id or the "
            "season is wrong, or ESPN changed the view contract"
        )
    return data


def fetch_player_pool(
    *,
    league_id: int,
    season: int,
    espn_s2,
    swid,
    scoring_period: int = 0,
    limit: int = 3000,
) -> list[dict]:
    """Pull the full player universe as raw ENTRY dicts (not ``p["player"]``).

    The entry wrapper is the point: ``onTeamId`` (0 = free agent) and ``status``
    (FREEAGENT/WAIVERS/ONTEAM) live there, and item 2.1's
    ``fetch_player_universe`` deliberately discards them. Each entry also carries
    the nested ``player`` with ``ownership`` percentages and ``injuryStatus``.

    Fails LOUD on truncation, exactly like the 2.1 pull: ESPN's ``limit`` filter
    hard-caps the response, so ``len >= limit`` means the pool overflowed the page
    and was silently cut. The observed 2026 universe is 1026 players; a cron that
    silently captured a truncated pool would write false free-agent history that
    cannot be recovered later.
    """
    league = _client(league_id, season, espn_s2, swid)
    filters = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
    }
    data = _request(
        league,
        params={"view": "kona_player_info", "scoringPeriodId": scoring_period},
        headers={"x-fantasy-filter": json.dumps(filters)},
    )
    entries = list(data.get("players") or [])
    if len(entries) >= limit:
        raise RuntimeError(
            f"ESPN player pool returned {len(entries)} rows at limit={limit}; the pool "
            "likely overflowed the page and was truncated — paginate the pull rather "
            "than silently capping (a truncated snapshot is unrecoverable history)."
        )
    return entries


def fetch_transactions(*, league_id: int, season: int, espn_s2, swid) -> list[dict]:
    """Pull the raw transaction feed. Returns ``[]`` when ESPN serves no
    ``transactions`` key — which is what it does today and did for all of 2025.

    Empty is NOT an error here (see the module docstring): snapshot diffing is the
    primary movement source and this feed only adds timestamp precision when it
    happens to be served.
    """
    league = _client(league_id, season, espn_s2, swid)
    filters = {"transactions": {"filterType": {"value": _TRANSACTION_TYPES}}}
    data = _request(
        league,
        params={"view": "mTransactions2"},
        headers={"x-fantasy-filter": json.dumps(filters)},
    )
    if isinstance(data, list):
        data = data[0] if data else {}
    return list((data or {}).get("transactions") or [])


def fetch_activity(*, league_id: int, season: int, espn_s2, swid, size: int = 100) -> list[dict]:
    """Pull the league communication (activity) feed — ADD/DROP/TRADE topics with
    per-message timestamps. Returns ``[]`` when unavailable.

    Second-best source for the same events as ``fetch_transactions``; ESPN
    populates one, the other, or neither depending on season state, so the sync
    asks for both and merges.
    """
    league = _client(league_id, season, espn_s2, swid)
    filters = {
        "topics": {
            "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
            "limit": size,
            "limitPerMessageSet": {"value": 25},
            "offset": 0,
            "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
            "sortFor": {"sortPriority": 2, "sortAsc": False},
            "filterIncludeMessageTypeIds": {"value": _ACTIVITY_MSG_TYPES},
        }
    }
    data = _request(
        league,
        params={"view": "kona_league_communication"},
        headers={"x-fantasy-filter": json.dumps(filters)},
        extend="/communication/",
    )
    if isinstance(data, list):
        data = data[0] if data else {}
    return list((data or {}).get("topics") or [])
