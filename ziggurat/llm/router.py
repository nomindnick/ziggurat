"""Task-tag router: the single entry point for all LLM calls.

Usage:
    router = Router.from_toml()                      # config/llm.toml
    resp = router.complete("smoke_test", "hello")    # -> LLMResponse

Task tags are registered in config/llm.toml with a stakes tier and a backend.
Unregistered tags raise — every LLM-consuming feature must declare its task
explicitly, which is what keeps backend swaps (e.g. `claude -p` -> Ollama after
a pricing change) a config edit instead of a code hunt.
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ziggurat.llm.backends import Backend, default_backends
from ziggurat.paths import LLM_CONFIG_PATH

TIERS = ("routine", "standard", "high_stakes")


class RoutingError(Exception):
    """Bad routing config or an unregistered task tag."""


@dataclass(frozen=True)
class TaskRoute:
    backend: str
    model: str | None = None
    tier: str = "routine"


@dataclass(frozen=True)
class LLMResponse:
    text: str
    task: str
    tier: str
    backend: str
    model: str | None


class Router:
    def __init__(
        self,
        routes: Mapping[str, TaskRoute],
        backends: Mapping[str, Backend] | None = None,
    ):
        self._backends = dict(backends) if backends is not None else default_backends()
        self._routes = dict(routes)
        for task, route in self._routes.items():
            if route.backend not in self._backends:
                raise RoutingError(
                    f"task {task!r} routes to unknown backend {route.backend!r} "
                    f"(known: {sorted(self._backends)})"
                )
            if route.tier not in TIERS:
                raise RoutingError(f"task {task!r} has unknown tier {route.tier!r} (known: {TIERS})")

    @classmethod
    def from_toml(cls, path: Path = LLM_CONFIG_PATH, backends: Mapping[str, Backend] | None = None) -> "Router":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        routes: dict[str, TaskRoute] = {}
        for task, entry in data.get("tasks", {}).items():
            extra = set(entry) - {"backend", "model", "tier"}
            if extra:
                raise RoutingError(f"task {task!r} has unknown config keys {sorted(extra)}")
            if "backend" not in entry:
                raise RoutingError(f"task {task!r} is missing required key 'backend'")
            routes[task] = TaskRoute(
                backend=entry["backend"],
                model=entry.get("model"),
                tier=entry.get("tier", "routine"),
            )
        return cls(routes, backends)

    def complete(self, task: str, prompt: str, *, system: str | None = None) -> LLMResponse:
        """Run `prompt` through the backend configured for `task`."""
        route = self._routes.get(task)
        if route is None:
            raise RoutingError(
                f"unregistered task tag {task!r} — add it to config/llm.toml "
                f"(known: {sorted(self._routes)})"
            )
        backend = self._backends[route.backend]
        text = backend.complete(prompt, system=system, model=route.model)
        return LLMResponse(
            text=text, task=task, tier=route.tier, backend=route.backend, model=route.model
        )
