"""LLM backends. Only `echo` is implemented in Phase 0.

The other three are registered now so config, routing, and task tags are
stable from day one; their implementations land with the features that first
need them (push layer 3.6, podcast extraction 4.3, local bake-off 4.5).
"""

from typing import Protocol


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


class ClaudeCLIBackend:
    """Headless `claude -p` on the Max subscription (scheduled/batch work)."""

    name = "claude_cli"

    def complete(self, prompt: str, *, system: str | None = None, model: str | None = None) -> str:
        raise NotImplementedError("claude_cli backend lands with the push layer (item 3.6)")


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
