"""Routing-interface tests (item 0.2): one entry point, task tags, config-driven
backends, echo backend for tests. The pattern every LLM-consuming feature copies."""

import pytest

from ziggurat.llm import Router, RoutingError, TaskRoute
from ziggurat.llm.backends import ClaudeCLIBackend, EchoBackend
from ziggurat.paths import LLM_CONFIG_PATH


def test_complete_routes_task_to_backend():
    router = Router({"my_task": TaskRoute(backend="echo", tier="routine")})
    resp = router.complete("my_task", "hello ziggurat")
    assert resp.text == "hello ziggurat"  # echo: deterministic no-op
    assert (resp.task, resp.tier, resp.backend) == ("my_task", "routine", "echo")


def test_unregistered_task_tag_raises():
    router = Router({})
    with pytest.raises(RoutingError, match="unregistered task tag"):
        router.complete("never_declared", "hi")


def test_unknown_backend_rejected_at_construction():
    with pytest.raises(RoutingError, match="unknown backend"):
        Router({"t": TaskRoute(backend="gpt_hallucinated")})


def test_unknown_tier_rejected_at_construction():
    with pytest.raises(RoutingError, match="unknown tier"):
        Router({"t": TaskRoute(backend="echo", tier="yolo")})


def test_default_config_file_loads_and_routes():
    # config/llm.toml is committed; the smoke_test task must stay wired to echo.
    router = Router.from_toml(LLM_CONFIG_PATH)
    resp = router.complete("smoke_test", "ping")
    assert resp.text == "ping"
    assert resp.backend == "echo"


def test_config_typos_are_loud(tmp_path):
    bad = tmp_path / "llm.toml"
    bad.write_text('[tasks.t]\nbackend = "echo"\nmodle = "oops"\n')
    with pytest.raises(RoutingError, match="unknown config keys"):
        Router.from_toml(bad)
    missing = tmp_path / "llm2.toml"
    missing.write_text('[tasks.t]\ntier = "routine"\n')
    with pytest.raises(RoutingError, match="missing required key"):
        Router.from_toml(missing)


def test_unimplemented_backends_are_registered_but_refuse():
    # Registered so config is stable from day one; implementations land in 3.6/4.5.
    with pytest.raises(NotImplementedError):
        ClaudeCLIBackend().complete("hi")


def test_custom_backend_injection_for_tests():
    # Tests (and the 4.5 bake-off) can swap backends without touching config.
    class Canned:
        name = "canned"

        def complete(self, prompt, *, system=None, model=None):
            return "canned answer"

    router = Router(
        {"t": TaskRoute(backend="canned", tier="high_stakes")},
        backends={"canned": Canned(), "echo": EchoBackend()},
    )
    assert router.complete("t", "anything").text == "canned answer"
