"""Provider-agnostic types for the AI layer."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field


class GenerationError(Exception):
    """Raised when a provider call fails (auth, network, bad response, refusal).

    The message is safe to surface to an admin user — it never includes the API
    key, and vendor error bodies are summarized rather than dumped verbatim.
    """


# A provider implementation: given a key, model, system prompt, and user prompt,
# return the generated text (or raise GenerationError).
GenerateFn = Callable[..., str]
# Streaming variant: yields text chunks as they arrive. Optional per provider.
StreamFn = Callable[..., Iterator[str]]


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of a provider for the registry and the config UI."""

    key: str  # stored on AIProvider.provider; selects the implementation
    label: str  # human label, e.g. "Anthropic (Claude)"
    default_model: str
    suggested_models: list[str] = field(default_factory=list)
    implemented: bool = False
    generate: GenerateFn | None = None
    # Optional streaming implementation; callers fall back to ``generate``.
    stream: StreamFn | None = None
    # Where to get a key, shown as a hint in the form.
    key_help: str = ""
