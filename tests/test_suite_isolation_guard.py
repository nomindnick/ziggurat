"""The suite never loads the operator's .env or touches the real ingest archives
(mid-season review 2026-11-05, P2(b) and P2(c)).

Pins conftest's autouse `_operator_dotenv_never_loads` and
`_ingest_archives_never_the_real_data_tree`, the way `test_push_mirror_guard.py`
pins the briefing-mirror guard. The .env half does not depend on this box
having a `.env`: `operator_env` writes a temporary one holding a canary value
for every loader's key and makes it THE operator file for the guard, so without
the guard every loader below reads those values and the tests fail.
"""

import os
from pathlib import Path

import dotenv
import pytest

import conftest
from ziggurat.data.nfl import espn_source, ff_opportunity, fp_weekly, refresh
from ziggurat.push import outbound

REAL_REPO = Path(__file__).resolve().parents[1]
REAL_DATA = (REAL_REPO / "data").resolve()


def _under_real_data(path) -> bool:
    path = Path(path).resolve()
    return path == REAL_DATA or REAL_DATA in path.parents


@pytest.fixture()
def operator_env(tmp_path, monkeypatch):
    import ziggurat.paths as paths

    repo = tmp_path / "repo"
    repo.mkdir()
    env = repo / ".env"
    env.write_text(
        "SWID={CANARY-SWID}\nESPN_S2=canary-s2\nESPN_LEAGUE_ID=1\n"
        "NTFY_TOPIC=zig-canary\nZIGGURAT_FP_WEEK_PAGE=1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "REPO_ROOT", repo)  # the loaders read it at call time
    monkeypatch.setattr(conftest, "OPERATOR_DOTENV", env.resolve())
    return env


def test_the_guard_protects_the_real_repo_dotenv():
    assert conftest.OPERATOR_DOTENV == (REAL_REPO / ".env").resolve()


def test_no_loader_reads_the_operator_dotenv(operator_env):
    with pytest.raises(outbound.OutboundBoundaryError, match="NTFY_TOPIC"):
        outbound.load_ntfy_config()
    with pytest.raises(RuntimeError, match="missing ESPN pull credentials"):
        espn_source.load_espn_credentials()
    assert fp_weekly.week_page_enabled() is False
    for key in conftest.OPERATOR_ENV_KEYS:
        assert key not in os.environ, key


def test_the_default_search_is_blocked_too():
    # dotenv's no-path search walks up from the caller and would find the real
    # repo .env (on a clone with no .env this is True either way).
    assert dotenv.load_dotenv() is False


def test_any_other_dotenv_still_loads_and_is_undone_at_teardown(tmp_path, monkeypatch):
    other = tmp_path / "other.env"
    other.write_text("ZIG_ISOLATION_CANARY=loaded\n", encoding="utf-8")
    assert "ZIG_ISOLATION_CANARY" not in os.environ
    assert dotenv.load_dotenv(other) is True
    assert os.environ["ZIG_ISOLATION_CANARY"] == "loaded"
    monkeypatch.undo()  # what teardown does
    assert "ZIG_ISOLATION_CANARY" not in os.environ


@pytest.fixture(scope="module")
def _a_key_loaded_before_the_guard_ran():
    """Module scope is set up BEFORE the function-scoped autouse guard, as a load
    at import or collection time would be."""
    prior = os.environ.get("NTFY_TOPIC")
    os.environ["NTFY_TOPIC"] = "zig-canary-preloaded"
    yield
    if prior is None:
        os.environ.pop("NTFY_TOPIC", None)
    else:
        os.environ["NTFY_TOPIC"] = prior


def test_a_key_loaded_before_the_guard_ran_is_removed(_a_key_loaded_before_the_guard_ran):
    assert "NTFY_TOPIC" not in os.environ


def test_the_ingest_archive_dirs_are_not_the_real_data_tree():
    for attr in ("FFOPP_ARCHIVE_DIR", "MARKET_ARCHIVE_DIR", "SLEEPER_RESEARCH_DIR"):
        assert not _under_real_data(getattr(refresh, attr)), attr
    assert not _under_real_data(refresh.ffopp_mirror_path(2026, "2026-09-15"))
    assert not _under_real_data(refresh.fpecr_mirror_path("2026-09-15"))


def test_the_ff_opportunity_pull_sweeps_a_temp_dir(db, monkeypatch):
    """The path the two orchestrator tests in test_nfl_ff_opportunity.py took:
    the pull sweeps `*.parquet.part` beside its mirror path BEFORE anything else.
    The sweep is replaced by a spy here, so nothing is deleted even unguarded."""
    swept = []
    monkeypatch.setattr(ff_opportunity, "sweep_stale_parts",
                        lambda path, **kw: swept.append(Path(path)) or [])

    def _stop():
        raise RuntimeError("canary stop")

    monkeypatch.setattr(ff_opportunity, "fetch_release", _stop)
    ctx = refresh.IngestContext(conn=db, season=2026, retrieved_as_of="2026-09-15",
                                today="2026-09-15")
    with pytest.raises(RuntimeError, match="canary stop"):
        refresh.SOURCES_BY_NAME["ff_opportunity"].pull(ctx)
    assert len(swept) == 1 and not _under_real_data(swept[0]), swept


@pytest.mark.canonical_archive_dirs
def test_the_opt_out_marker_sees_the_real_defaults():
    assert Path(refresh.FFOPP_ARCHIVE_DIR) == REAL_REPO / "data" / "ffopp"
    assert Path(refresh.MARKET_ARCHIVE_DIR) == REAL_REPO / "data" / "backtest"
