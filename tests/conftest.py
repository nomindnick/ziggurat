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
def marginal_world(db):
    """Factory for a synthetic projection universe + league state (item 3.2).

    Entirely invented players and owners (Rule 5). The SHAPE is the live one, and
    the details that bite are reproduced exactly:

    * a skill/K bye is a projection row that IS PRESENT with a NULL opponent and
      NULL stats; a D/ST bye row is ABSENT entirely — a detector keyed on either
      shape alone mislabels the other class;
    * D/ST rows carry a negative ESPN id and a NULL ``gsis_id``, so they can only
      be joined on normalized team abbreviation;
    * roster rows land in ``league_player_state`` with the four post-draft fields
      (``on_team_id``, ``lineup_slot``, ``acquisition_type``, ``acquisition_date``),
      which are the ONLY fields that change when the real draft happens — so a
      test roster and the live roster are the same shape.

    Each spec is ``{"name", "pos", "team", "pts", "bye", "on_team", "slot",
    "owned", "injury", "weeks", "forecast", "proj_team"}``; ``pts`` is HOUSE points
    per playing week (offense via rushing yards at 0.1/yd, K via extra points at
    1.0, D/ST via sacks at 1.0 — no bracket keys, so no phantom shutout points),
    and ``weeks`` overrides individual weeks.

    ``forecast`` is the set of weeks the feed actually forecasts. Every other week
    gets a BYE-SHAPED row (team set, opponent NULL, all stats NULL) — which is what
    the live feed emits for a player it has no forecast for, byte-identical to a
    real bye. That indistinguishability is the trap: it made a 99.3%-owned WR with
    one forecast week read as the most droppable player on the roster.

    ``proj_team`` overrides the abbreviation stored in ``projections`` while the
    league row keeps ``team`` — the LAR/LA case, where joining raw loses the Rams.
    """
    def _stat_columns(pos, pts):
        if pos in ("QB", "RB", "WR", "TE"):
            return {"rushing_yards": pts * 10.0}
        if pos == "K":
            return {"pat_made": pts}
        return {"sacks": pts}          # D/ST: events only, brackets left absent

    def _make(specs, *, season=2026, weeks=None, retrieved="2026-09-15",
              scoring_period=0, source="sleeper_rotowire"):
        weeks = list(range(1, 18) if weeks is None else weeks)
        roster, pool = [], []
        for i, spec in enumerate(specs):
            pos = spec["pos"]
            is_dst = pos == "D/ST"
            espn_id = str(-16000 - i) if is_dst else str(2000 + i)
            gsis_id = None if is_dst else f"00-1{i:05d}"
            team = spec["team"]
            bye = spec.get("bye")
            sleeper_id = f"S{i}"

            if not is_dst:
                db.execute(
                    "INSERT INTO players (gsis_id, sleeper_id, espn_id, name, "
                    "retrieved_as_of, knowable_as_of) VALUES (?, ?, ?, ?, ?, ?)",
                    (gsis_id, sleeper_id, espn_id, spec["name"], retrieved, retrieved),
                )

            forecast = spec.get("forecast")
            for week in weeks:
                blank = week == bye or (forecast is not None and week not in forecast)
                if blank and is_dst:
                    continue                     # D/ST bye row is ABSENT
                pts = spec.get("weeks", {}).get(week, spec["pts"])
                cols = {
                    "source": source,
                    "source_player_id": sleeper_id if not is_dst else team,
                    "gsis_id": gsis_id,
                    "season": season,
                    "week": week,
                    "season_type": "regular",
                    "position": "DEF" if is_dst else pos,
                    "team": spec.get("proj_team", team),
                    "opponent": None if blank else "OPP",
                    "retrieved_as_of": spec.get("retrieved", retrieved),
                    "knowable_as_of": spec.get("retrieved", retrieved),
                }
                if not blank:
                    cols.update(_stat_columns(pos, pts))
                names = ", ".join(cols)
                db.execute(
                    f"INSERT INTO projections ({names}) VALUES "
                    f"({', '.join('?' * len(cols))})",
                    tuple(cols.values()),
                )

            row = {
                "season": season,
                "espn_player_id": espn_id,
                "gsis_id": gsis_id,
                "player": spec["name"],
                "position": pos,
                "pro_team": team,
                "on_team_id": spec.get("on_team"),
                "roster_status": spec.get("status",
                                          "ONTEAM" if spec.get("on_team") else "FREEAGENT"),
                "lineup_slot": spec.get("slot", "BE" if spec.get("on_team") else None),
                "acquisition_type": "DRAFT" if spec.get("on_team") else None,
                "acquisition_date": "2026-08-20" if spec.get("on_team") else None,
                "injury_status": spec.get("injury", "ACTIVE"),
                "percent_owned": spec.get("owned", 10.0 + i),
                "percent_started": 0.0,
                "percent_change": 0.0,
                "scoring_period": scoring_period,
                "retrieved_as_of": retrieved,
                "knowable_as_of": retrieved,
            }
            db.execute(
                f"INSERT INTO league_player_state ({', '.join(row)}) VALUES "
                f"({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
            (roster if spec.get("on_team") else pool).append(row)
        db.commit()
        return roster, pool

    return _make


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
