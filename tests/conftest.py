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
