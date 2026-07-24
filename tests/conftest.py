"""Shared pytest fixtures for the NFL ingestion suite (item 1.4).

`db` is a fresh in-memory SQLite with the full schema applied; `nfl_fixture`
loads a captured source frame from tests/fixtures/nfl/*.parquet (2023 weeks 5-6
slices) so ingestion tests run offline — the cached-fixture pattern.
"""

from pathlib import Path

import pandas as pd
import pytest

from ziggurat.data.store import apply_schema, connect

NFL_FIXTURES = Path(__file__).parent / "fixtures" / "nfl"


def load_nfl_fixture(name: str) -> pd.DataFrame:
    return pd.read_parquet(NFL_FIXTURES / f"{name}.parquet")


@pytest.fixture()
def db():
    conn = connect(":memory:")
    apply_schema(conn)
    yield conn
    conn.close()


@pytest.fixture()
def nfl_fixture():
    """Return the loader so a test can pull any captured source frame by name."""
    return load_nfl_fixture


@pytest.fixture()
def league_world():
    """Factory for a synthetic 10-team league payload + matching player pool (item 3.1).

    Entirely synthetic — team names, owners and players are invented (rule 5: the
    real league's team names and managers are colleagues and never enter a
    committed file). The SHAPE mirrors the live ESPN payloads verified in the 3.1
    recon: entry-level ``onTeamId``/``status``, nested ``player.ownership``,
    roster entries with ``lineupSlotId``/``acquisitionType``/``acquisitionDate``,
    and a ``schedule`` of ``matchupPeriodId`` pairings.

    ``holdings`` maps espn_player_id -> league team id; every other player in the
    pool is a free agent, so a test can move one player between teams (or to the
    pool) across snapshots and assert the temporal reads.
    """
    POSITION_IDS = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "D/ST": 16}

    def _make(*, holdings=None, pool_size=40, scoring_period=3, season=2026,
              slots=None, acquisitions=None, drop_from_pool=()):
        holdings = dict(holdings or {})
        slots = dict(slots or {})
        acquisitions = dict(acquisitions or {})
        cycle = ["QB", "RB", "WR", "TE", "K", "D/ST"]

        pool = []
        for i in range(pool_size):
            pid = str(1000 + i)
            position = cycle[i % len(cycle)]
            if pid in drop_from_pool:
                continue
            pool.append({
                "id": int(pid),
                "onTeamId": holdings.get(pid, 0),
                "status": "ONTEAM" if pid in holdings else "FREEAGENT",
                "player": {
                    "id": int(pid),
                    "fullName": f"Synthetic Player {i:03d}",
                    "defaultPositionId": POSITION_IDS[position],
                    "proTeamId": 1 + (i % 32),
                    "injuryStatus": "ACTIVE" if i % 7 else "QUESTIONABLE",
                    "ownership": {
                        "percentOwned": max(0.0, 99.0 - i * 2.3),
                        "percentStarted": max(0.0, 90.0 - i * 2.5),
                        "percentChange": (i % 5) - 2.0,
                    },
                },
            })

        by_team: dict[int, list] = {}
        for pid, team_id in holdings.items():
            by_team.setdefault(team_id, []).append({
                "playerId": int(pid),
                "lineupSlotId": slots.get(pid, 20),  # 20 = BE
                "acquisitionType": acquisitions.get(pid, "DRAFT"),
                "acquisitionDate": 1788000000000,
            })

        teams = [{
            "id": tid,
            "abbrev": f"SYN{tid}",
            "name": f"Synthetic Team {tid}",
            "primaryOwner": f"{{OWNER-{tid}}}",
            "owners": [f"{{OWNER-{tid}}}"],
            "divisionId": 0,
            "waiverRank": tid,
            "playoffSeed": 0,
            "isTransactionLocked": False,
            "record": {"overall": {
                "wins": tid % 3, "losses": 2, "ties": 0,
                "pointsFor": 100.0 + tid, "pointsAgainst": 95.0 + tid,
                "streakLength": 1, "streakType": "WIN",
            }},
            "transactionCounter": {
                "acquisitions": tid, "drops": tid, "trades": 0,
                "moveToIR": 0, "moveToActive": 0,
                "acquisitionBudgetSpent": 0.0, "teamCharges": 0.0,
            },
            "roster": {"entries": by_team.get(tid, [])},
        } for tid in range(1, 11)]

        schedule = []
        for week in range(1, 15):
            for pair in range(5):
                home, away = 1 + pair * 2, 2 + pair * 2
                played = week < scoring_period
                schedule.append({
                    "matchupPeriodId": week,
                    "winner": "HOME" if played else "UNDECIDED",
                    "home": {"teamId": home, "totalPoints": 110.0 if played else 0.0,
                             "gamesPlayed": 1 if played else 0},
                    "away": {"teamId": away, "totalPoints": 99.0 if played else 0.0,
                             "gamesPlayed": 1 if played else 0},
                })

        payload = {
            "seasonId": season,
            "scoringPeriodId": scoring_period,
            "status": {"currentMatchupPeriod": scoring_period, "finalScoringPeriod": 17},
            "teams": teams,
            "schedule": schedule,
        }
        return payload, pool

    return _make


@pytest.fixture()
def crosswalked_db(db):
    """`db` with a players table populated so espn->gsis crosswalk coverage passes.

    Item 3.1's ingest refuses to write a snapshot whose crosswalk has collapsed
    (that would silently sever league state from the NFL spine), so every ingest
    test needs the crosswalk present.
    """
    rows = [
        (f"00-00{i:04d}", str(1000 + i), "2026-07-01", "2026-07-01")
        for i in range(200)
    ]
    db.executemany(
        "INSERT INTO players (gsis_id, espn_id, retrieved_as_of, knowable_as_of) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    db.commit()
    return db


@pytest.fixture()
def make_draft_board():
    """Factory building a synthetic, fully-draftable mock-sim board (item 2.2).

    Skill players (QB/RB/WR/TE) take the top ESPN ranks; K/DST are ranked deep
    (as the real ESPN editorial board ranks them). Counts default to a deep board;
    pass ``dst=10, k=10`` for the K/DST-scarce legality stress test. All synthetic
    — no real player/team identity (Rule 5). Imported lazily so this shared
    conftest still loads after the deletable draft package is removed (Rule 8).
    """
    from ziggurat.draft.bots import BoardEntry

    def _make(*, qb=32, rb=80, wr=90, te=32, dst=32, k=32):
        specs = {
            "QB": (320.0, 7.0, qb), "RB": (300.0, 3.0, rb), "WR": (300.0, 2.6, wr),
            "TE": (230.0, 5.0, te), "DST": (130.0, 3.0, dst), "K": (120.0, 2.0, k),
        }
        players: list[tuple[str, int, float]] = []
        by_pos_pts: dict[str, list[float]] = {}
        for pos, (top, decay, count) in specs.items():
            for i in range(count):
                pts = max(1.0, top - decay * i)
                players.append((pos, i, pts))
                by_pos_pts.setdefault(pos, []).append(pts)

        # replacement ~ points of the league-wide "first non-starter" at the pos.
        started = {"QB": 10, "RB": 30, "WR": 30, "TE": 12, "DST": 10, "K": 10}
        repl = {
            pos: lst[min(started[pos], len(lst) - 1)] for pos, lst in by_pos_pts.items()
        }

        skill = sorted((p for p in players if p[0] not in ("K", "DST")), key=lambda p: -p[2])
        kdst = sorted((p for p in players if p[0] in ("K", "DST")), key=lambda p: -p[2])
        board = []
        for rank, (pos, i, pts) in enumerate(skill + kdst, start=1):
            board.append(BoardEntry(f"{pos}-{i}", f"{pos}{i}", pos, rank, pts, pts - repl[pos]))
        return tuple(board)

    return _make
