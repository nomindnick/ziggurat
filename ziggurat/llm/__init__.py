"""LLM routing interface (SPEC key decision 13).

Every programmatic model call in this repo goes through Router.complete().
No component may import a model SDK or shell out to a model directly —
backends and task routes are configuration (config/llm.toml), not code.
"""

from ziggurat.llm.router import LLMResponse, Router, RoutingError, TaskRoute

__all__ = ["LLMResponse", "Router", "RoutingError", "TaskRoute"]
