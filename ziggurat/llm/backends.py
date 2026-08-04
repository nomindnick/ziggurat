"""LLM backends.

The `echo` backend is a deterministic no-op for tests and wiring checks. The
`claude_cli` backend (item 3.6, the push layer) is the first REAL backend: it
shells the headless `claude -p` CLI on the operator's Max subscription for
scheduled/batch summarization (the morning briefing, news summaries). The other
two are registered so config, routing, and task tags are stable from day one;
their implementations land with the features that first need them
(`anthropic_api` when a high-stakes task needs metered API, `ollama` with the
local-model bake-off, item 4.5).

WHY `claude_cli` IS SHAPED THE WAY IT IS (item 3.6 recon R1, validated with live
probes against `claude` v2.1.220):

* `--output-format json` — parse the single result object and branch on
  `is_error`/`subtype`; a bare `text` format makes an error indistinguishable
  from a valid completion without trusting the exit code alone.
* `--system-prompt` (full REPLACE, not `--append-system-prompt`) — collapses the
  ~6.3k-token default Claude Code agent system prompt to ~0 and yields a pure
  text-in/text-out summarizer. Measured: append keeps the heavy default; replace
  drops it.
* A NEUTRAL cwd (a throwaway empty temp dir, never the repo root) — `claude`
  auto-discovers and loads CLAUDE.md + SPEC when run from the repo (~12.8k tokens
  measured), which both wastes tokens AND pulls the private constitution into the
  LLM turn. `--system-prompt` replace does not stop that discovery; cwd is the
  control.
* `--tools ""` + `--permission-mode dontAsk` + `--strict-mcp-config` +
  `--no-session-persistence` — zero tools offered (cannot read files, cannot
  spawn a sub-agent, cannot hang on a permission prompt with no TTY), zero MCP
  servers spun up, no session state written. A scheduled cron run must never
  block waiting for input.
* Prompt over STDIN, system over argv — the briefing payload can be large and
  multiline; stdin sidesteps ARG_MAX. The system string is short and fixed.
* Auth is the Max-subscription OAuth token in ~/.claude (auto-refreshed); NO
  ANTHROPIC_API_KEY (an API key silently flips `claude` to metered billing — that
  is deliberately a DIFFERENT backend, `anthropic_api`). The subprocess env is
  scrubbed of ANTHROPIC_API_KEY and the inherited CLAUDE_CODE_*/CLAUDECODE vars
  so a dev run from inside a Claude Code session matches the clean systemd
  environment.
* `subprocess` timeout (net.py bounds in-process SOCKETS; this is a separate OS
  process, so net.py does not apply) with a process-GROUP kill on timeout so a
  hung `claude` cannot orphan a socket-holding child; systemd `TimeoutStartSec`
  on the 3.6 unit is the outer fence.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
from typing import Protocol

#: Default model when a task route pins none. `sonnet` is the standard-tier
#: summarizer; routine/high-volume tasks (news) pin `haiku` in config/llm.toml.
DEFAULT_MODEL = "sonnet"

#: Wall-clock cap (seconds) for one headless completion. Overridable per backend
#: instance; the CLI/systemd unit can pass a tighter value for the routine
#: news lane and a looser one for the briefing.
DEFAULT_TIMEOUT = 120

#: Terse fallback system prompt used only when the caller passes none. The 3.6
#: briefing/news code always passes its own; this keeps a bare call from ever
#: falling through to the heavy default Claude Code agent prompt.
DEFAULT_SUMMARY_SYSTEM = (
    "You are a concise summarizer for a fantasy-football decision tool. "
    "Return only the requested text, with no preamble, meta-commentary, or tool use."
)


class BackendError(RuntimeError):
    """A backend failed to produce a completion (timeout, non-zero exit, bad
    output, or a model-reported error). Distinct from RoutingError, which is a
    config/task-tag problem the router raises before any backend runs. The router
    does not catch this — it propagates to the caller (the 3.6 push builder),
    which degrades gracefully (writes the full briefing to disk without the LLM
    prose rather than dropping the run)."""


class Backend(Protocol):
    name: str

    def complete(self, prompt: str, *, system: str | None = None, model: str | None = None) -> str:
        """Return the model's text completion for `prompt`."""
        ...


class EchoBackend:
    """Deterministic no-op backend for tests and wiring checks: returns the prompt."""

    name = "echo"

    def complete(self, prompt: str, *, system: str | None = None, model: str | None = None) -> str:
        return prompt


def _resolve_binary() -> str:
    """Locate the `claude` CLI. Env override first (the systemd unit sets it,
    since a --user unit's PATH may not include ~/.local/bin), then PATH. No
    machine-specific absolute path is baked into this public-repo module."""
    override = os.environ.get("ZIGGURAT_CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    raise BackendError(
        "claude CLI not found; set ZIGGURAT_CLAUDE_BIN or add `claude` to PATH "
        "(the claude_cli backend shells the headless `claude -p`)"
    )


#: Env vars that would REROUTE the CLI off the Max subscription: an API key or
#: auth token flips it to metered billing; a base-URL override points it at a
#: different (possibly metered/proxy) endpoint; a config-dir override points it at
#: different credentials. All are stripped so a scheduled/dev run uses only the
#: subscription OAuth in ~/.claude (audit D3).
_ANTHROPIC_OVERRIDE_VARS = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CONFIG_DIR", "CLAUDECODE",
})


def _clean_env() -> dict[str, str]:
    """The subprocess environment: strip the billing/auth/endpoint override vars
    and the inherited Claude Code session vars (so a dev run matches the clean
    systemd environment), keep everything else (notably HOME, where ~/.claude auth
    lives)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _ANTHROPIC_OVERRIDE_VARS
        and not k.startswith("ANTHROPIC_")
        and not k.startswith("CLAUDE_CODE")
    }
    env.setdefault("HOME", os.path.expanduser("~"))
    return env


class ClaudeCLIBackend:
    """Headless `claude -p` on the Max subscription (scheduled/batch work)."""

    name = "claude_cli"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        model_default: str = DEFAULT_MODEL,
        runner=None,
    ):
        # `binary` is resolved lazily on first use unless given, so importing the
        # module (and constructing default_backends()) never requires `claude` to
        # be installed — only actually CALLING complete() does.
        self._binary = binary
        self._timeout = timeout
        self._model_default = model_default
        # Seam for tests: a callable (argv, prompt, timeout, cwd, env) ->
        # (returncode, stdout, stderr). Defaults to the real subprocess runner.
        self._runner = runner or _subprocess_runner

    def _argv(self, system: str | None, model: str | None) -> list[str]:
        binary = self._binary or _resolve_binary()
        return [
            binary,
            "-p",
            "--output-format", "json",
            "--model", model or self._model_default,
            "--system-prompt", system or DEFAULT_SUMMARY_SYSTEM,
            "--tools", "",
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]

    def complete(self, prompt: str, *, system: str | None = None, model: str | None = None) -> str:
        argv = self._argv(system, model)
        env = _clean_env()
        with tempfile.TemporaryDirectory(prefix="ziggurat-claude-") as neutral_cwd:
            try:
                returncode, stdout, stderr = self._runner(
                    argv, prompt, self._timeout, neutral_cwd, env
                )
            except subprocess.TimeoutExpired as exc:
                raise BackendError(
                    f"claude_cli timed out after {self._timeout}s"
                ) from exc
        if returncode != 0:
            raise BackendError(
                f"claude_cli exited {returncode}: {(stderr or '').strip()[:500]}"
            )
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(
                f"claude_cli produced non-JSON output: {(stdout or '')[:500]!r}"
            ) from exc
        if not isinstance(data, dict):
            # Valid JSON but not the result object (e.g. a bare string/array) —
            # calling .get() on it would AttributeError instead of a clean error.
            raise BackendError(f"claude_cli produced non-object JSON: {(stdout or '')[:500]!r}")
        if data.get("is_error") or data.get("subtype") != "success":
            raise BackendError(
                f"claude_cli reported an error: {data.get('result') or data.get('subtype') or data}"
            )
        result = data.get("result")
        if not isinstance(result, str):
            raise BackendError(f"claude_cli returned no text result: {data!r}")
        return result


def _subprocess_runner(argv, prompt, timeout, cwd, env):
    """Run `claude` in its own process group and hard-kill the whole group on
    timeout, so a hung CLI cannot leave an orphaned child holding a socket. Any
    TimeoutExpired propagates to complete(), which maps it to a BackendError."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        start_new_session=True,  # proc.pid becomes the process-group leader
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()  # reap the dead child
        raise
    return proc.returncode, stdout, stderr


class AnthropicAPIBackend:
    """Metered Claude API — tolerated only for designated high-stakes tasks."""

    name = "anthropic_api"

    def complete(self, prompt: str, *, system: str | None = None, model: str | None = None) -> str:
        raise NotImplementedError("anthropic_api backend lands when a high-stakes task needs it")


class OllamaBackend:
    """Local models on the Strix Halo box (routine extraction/summarization)."""

    name = "ollama"

    def complete(self, prompt: str, *, system: str | None = None, model: str | None = None) -> str:
        raise NotImplementedError("ollama backend lands with the local-model bake-off (item 4.5)")


def default_backends() -> dict[str, Backend]:
    backends: list[Backend] = [
        EchoBackend(),
        ClaudeCLIBackend(),
        AnthropicAPIBackend(),
        OllamaBackend(),
    ]
    return {b.name: b for b in backends}
