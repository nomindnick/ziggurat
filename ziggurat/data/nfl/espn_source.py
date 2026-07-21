"""Live ESPN default-draft-board network seam (item 2.1).

The ONE network seam for the ESPN player universe. It mirrors the ``import_*``
pattern in ``source.py``: a single thin function that performs the raw HTTP pull
and returns parsed JSON, with the third-party client imported lazily so the rest
of the package (and the offline test suite) never touches ``espn_api`` or the
network. Tests patch ``fetch_player_universe`` and feed the captured
``tests/fixtures/espn/player_universe.json`` slice.

Why the RAW ``kona_player_info`` request instead of ``League.free_agents()``:
the wrapper is size-capped, drops on-team players, sorts by percent-owned, and
returns ``BoxPlayer`` objects that discard ``draftRanksByRankType`` /
``ownership.averageDraftPosition`` — the exact editorial-board + native-ADP
signals item 2.1 needs (design D9). We request the full pool sorted by the PPR
draft rank and read the raw player dicts.
"""

import json
import os
from importlib import import_module

# ESPN default-league scoringPeriodId for the pre-draft player pool snapshot.
_SCORING_PERIOD_PREDRAFT = 0


def load_espn_credentials(*, league_id: int | None = None) -> dict:
    """Resolve the ESPN pull credentials from the local (gitignored) ``.env``.

    Package-layer glue so the CLI stays thin (rule 3). ``SWID`` and ``ESPN_S2``
    are read from the environment (loading the repo ``.env`` if present, without
    overriding an already-set env var); ``league_id`` comes from the argument or
    the ``ESPN_LEAGUE_ID`` env var — the leagueId is private (rule 5) and never
    committed. Missing values fail LOUD with a refresh hint rather than pulling a
    wrong/empty board.
    """
    try:  # optional: load the repo .env so cookies need not be exported manually
        from dotenv import load_dotenv

        from ziggurat.paths import REPO_ROOT

        load_dotenv(REPO_ROOT / ".env", override=False)
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        pass

    swid = os.environ.get("SWID")
    espn_s2 = os.environ.get("ESPN_S2")
    resolved_league = league_id if league_id is not None else os.environ.get("ESPN_LEAGUE_ID")

    missing = [
        name
        for name, value in (("SWID", swid), ("ESPN_S2", espn_s2), ("league_id", resolved_league))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "missing ESPN pull credentials: "
            + ", ".join(missing)
            + " (set SWID/ESPN_S2 in .env and pass --league-id or set ESPN_LEAGUE_ID)"
        )
    return {"league_id": int(resolved_league), "espn_s2": espn_s2, "swid": swid}


def _league(league_id: int, season: int, espn_s2, swid):
    """Instantiate an ``espn_api`` League for the raw request, importing the
    third-party client lazily (mirrors ``source._client``). ``fetch_league=False``
    skips the standings/roster fetch we do not need pre-draft; ``espn_request`` is
    still constructed with the auth cookies."""
    try:
        football = import_module("espn_api.football")
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "espn_api is required for live ESPN pulls; install the project dependencies"
        ) from exc
    return football.League(
        league_id=league_id,
        year=season,
        espn_s2=espn_s2,
        swid=swid,
        fetch_league=False,
    )


def fetch_player_universe(
    *,
    league_id: int,
    season: int,
    espn_s2,
    swid,
    limit: int = 2000,
) -> list[dict]:
    """Pull the full ESPN player universe as raw ``p["player"]`` dicts (design §4).

    Returns the list of raw player dicts, each carrying ``id``, ``fullName``,
    ``defaultPositionId``, ``proTeamId``, ``draftRanksByRankType`` (editorial PPR
    board rank) and ``ownership.averageDraftPosition`` (native ADP). Sorted by the
    PPR draft rank server-side.

    Fails LOUD on truncation: ESPN's ``limit`` filter hard-caps the response, so a
    returned count of ``>= limit`` means the universe overflowed the page and was
    silently cut. We assert ``len < limit`` rather than cap-and-continue — a future
    larger pool must be paginated deliberately, never truncated silently. The
    default ``limit`` of 2000 sits well above the observed ~1025-player 2026 pool;
    the design's original ``limit=1000`` probe was itself truncating (the pool is
    1025), which this guard would (correctly) reject.

    ESPN auth failures (401 ``ESPNAccessDenied`` / 404 ``ESPNInvalidLeague`` from
    an expired cookie) propagate, re-wrapped with a refresh hint.
    """
    espn_requests = import_module("espn_api.requests.espn_requests")
    league = _league(league_id, season, espn_s2, swid)
    filters = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
            "limit": limit,
            "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": "PPR"},
        }
    }
    try:
        data = league.espn_request.league_get(
            params={"view": "kona_player_info", "scoringPeriodId": _SCORING_PERIOD_PREDRAFT},
            headers={"x-fantasy-filter": json.dumps(filters)},
        )
    except (espn_requests.ESPNAccessDenied, espn_requests.ESPNInvalidLeague) as exc:
        raise RuntimeError(
            "ESPN rejected the request (expired/invalid cookies?); "
            "refresh SWID/ESPN_S2 in .env"
        ) from exc

    players = [entry["player"] for entry in data.get("players", [])]
    if len(players) >= limit:
        raise RuntimeError(
            f"ESPN player universe returned {len(players)} rows at limit={limit}; "
            "the pool likely overflowed the page and was truncated — paginate the pull "
            "rather than silently capping."
        )
    return players
