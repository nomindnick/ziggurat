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
