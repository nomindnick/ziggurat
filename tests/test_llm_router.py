"""Routing-interface tests (item 0.2): one entry point, task tags, config-driven
backends, echo backend for tests. The pattern every LLM-consuming feature copies."""

import json

import pytest

from ziggurat.llm import Router, RoutingError, TaskRoute
from ziggurat.llm.backends import BackendError, ClaudeCLIBackend, EchoBackend
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
    # anthropic_api and ollama are registered so config is stable from day one;
    # their implementations land in later phases (4.5 / when a high-stakes task
    # needs metered API). claude_cli is now IMPLEMENTED (item 3.6) — see below.
    from ziggurat.llm.backends import AnthropicAPIBackend, OllamaBackend

    with pytest.raises(NotImplementedError):
        AnthropicAPIBackend().complete("hi")
    with pytest.raises(NotImplementedError):
        OllamaBackend().complete("hi")


def _fake_runner(canned_stdout, *, returncode=0, stderr="", capture=None):
    """Build a ClaudeCLIBackend runner seam that returns canned CLI output
    without shelling out, and records the argv/prompt/env for assertions."""

    def runner(argv, prompt, timeout, cwd, env):
        if capture is not None:
            capture.update(argv=argv, prompt=prompt, timeout=timeout, cwd=cwd, env=env)
        return returncode, canned_stdout, stderr

    return runner


def test_claude_cli_backend_parses_json_result():
    capture = {}
    out = json.dumps({"is_error": False, "subtype": "success", "result": "PONG"})
    backend = ClaudeCLIBackend(binary="/x/claude", runner=_fake_runner(out, capture=capture))
    assert backend.complete("ping", system="be terse", model="haiku") == "PONG"
    # argv is the validated headless-safe invocation (item 3.6 R1).
    argv = capture["argv"]
    assert argv[:3] == ["/x/claude", "-p", "--output-format"]
    for flag in ("--tools", "--permission-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "haiku"
    assert "--system-prompt" in argv and argv[argv.index("--system-prompt") + 1] == "be terse"
    # prompt goes over stdin, not argv.
    assert capture["prompt"] == "ping"
    assert "ping" not in argv


def test_claude_cli_backend_scrubs_billing_and_session_env(monkeypatch):
    capture = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("HOME", "/home/whoever")
    out = json.dumps({"is_error": False, "subtype": "success", "result": "ok"})
    backend = ClaudeCLIBackend(binary="/x/claude", runner=_fake_runner(out, capture=capture))
    backend.complete("hi")
    env = capture["env"]
    assert "ANTHROPIC_API_KEY" not in env  # a key would flip claude to metered billing
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["HOME"] == "/home/whoever"  # auth lives in ~/.claude — HOME must survive


def test_claude_cli_backend_raises_on_model_error():
    out = json.dumps({"is_error": True, "subtype": "error_during_execution", "result": "boom"})
    backend = ClaudeCLIBackend(binary="/x/claude", runner=_fake_runner(out))
    with pytest.raises(BackendError, match="boom"):
        backend.complete("hi")


def test_claude_cli_backend_raises_on_nonzero_exit():
    backend = ClaudeCLIBackend(
        binary="/x/claude", runner=_fake_runner("", returncode=1, stderr="auth expired")
    )
    with pytest.raises(BackendError, match="auth expired"):
        backend.complete("hi")


def test_claude_cli_backend_raises_on_non_json():
    backend = ClaudeCLIBackend(binary="/x/claude", runner=_fake_runner("not json at all"))
    with pytest.raises(BackendError, match="non-JSON"):
        backend.complete("hi")


def test_claude_cli_backend_raises_on_non_object_json():
    # Valid JSON but a bare string/array would AttributeError on .get() (audit D3).
    backend = ClaudeCLIBackend(binary="/x/claude", runner=_fake_runner('"just a string"'))
    with pytest.raises(BackendError, match="non-object JSON"):
        backend.complete("hi")


def test_clean_env_strips_all_billing_and_endpoint_overrides(monkeypatch):
    # audit D3: an API key/auth token/base-url/config-dir override would reroute the
    # CLI off the Max subscription. All must be stripped; HOME must survive.
    from ziggurat.llm.backends import _clean_env

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CONFIG_DIR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("HOME", "/home/whoever")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _clean_env()
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CONFIG_DIR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        assert var not in env, var
    assert env["HOME"] == "/home/whoever" and env["PATH"] == "/usr/bin"


def test_subprocess_runner_kills_whole_group_on_timeout(tmp_path):
    # audit D3: the real runner must hard-kill the whole process GROUP on timeout,
    # so a hung claude cannot orphan a socket-holding child.
    import subprocess
    import sys
    import time

    from ziggurat.llm import backends

    sentinel = tmp_path / "grandchild.touched"
    # parent spawns a grandchild that waits 2s then touches the sentinel, then the
    # parent sleeps 30s. If the GROUP is killed on the 1s timeout, the grandchild
    # (same session/group) dies before it can touch the sentinel.
    child = (
        "import subprocess,sys,time,pathlib;"
        f"subprocess.Popen([sys.executable,'-c',\"import time,pathlib;time.sleep(2);"
        f"pathlib.Path(r'{sentinel}').touch()\"]);"
        "time.sleep(30)"
    )
    argv = [sys.executable, "-c", child]
    with pytest.raises(subprocess.TimeoutExpired):
        backends._subprocess_runner(argv, "", 1, str(tmp_path), {"PATH": "/usr/bin:/bin"})
    time.sleep(3)  # give a surviving grandchild time to touch
    assert not sentinel.exists(), "process group was NOT killed — grandchild survived"


def test_claude_cli_backend_maps_timeout_to_backend_error():
    import subprocess

    def timing_out_runner(argv, prompt, timeout, cwd, env):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    backend = ClaudeCLIBackend(binary="/x/claude", runner=timing_out_runner)
    with pytest.raises(BackendError, match="timed out"):
        backend.complete("hi")


def test_config_registers_claude_cli_tasks():
    # item 3.6: the two push-layer tasks load from the committed config and route
    # to the claude_cli backend at the right tiers.
    router = Router.from_toml(LLM_CONFIG_PATH)
    assert router._routes["morning_briefing"].backend == "claude_cli"
    assert router._routes["morning_briefing"].tier == "standard"
    assert router._routes["morning_briefing"].model == "sonnet"
    assert router._routes["news_summarization"].backend == "claude_cli"
    assert router._routes["news_summarization"].tier == "routine"
    assert router._routes["news_summarization"].model == "haiku"


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
