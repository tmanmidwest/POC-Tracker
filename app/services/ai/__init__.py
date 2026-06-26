"""AI text-generation provider abstraction.

A small, pluggable layer so AI features (starting with executive summaries) can
call different vendors through one interface. Each provider is described by a
:class:`ProviderSpec` in the registry and implements a single ``generate``
function. Anthropic (Claude) is implemented today; Google (Gemini) and OpenAI
are registered as not-yet-implemented so they show up in the UI as coming soon
and can be filled in without touching callers.
"""

from app.services.ai.base import GenerateFn, GenerationError, ProviderSpec
from app.services.ai.registry import PROVIDERS, get_provider_spec, implemented_providers

__all__ = [
    "PROVIDERS",
    "GenerateFn",
    "GenerationError",
    "ProviderSpec",
    "get_provider_spec",
    "implemented_providers",
]
