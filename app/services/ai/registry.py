"""Registry of supported AI providers.

To add a provider (e.g. Gemini): implement a ``generate`` function in a new
``<vendor>_provider.py``, then flip its entry here to ``implemented=True`` with
that function. Callers and the config UI read from this registry, so nothing
else needs to change.
"""

from __future__ import annotations

from app.services.ai import anthropic_provider
from app.services.ai.base import ProviderSpec

PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Anthropic (Claude)",
        default_model="claude-opus-4-8",
        suggested_models=[
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ],
        implemented=True,
        generate=anthropic_provider.generate,
        stream=anthropic_provider.stream,
        key_help="Create a key at console.anthropic.com → API Keys.",
    ),
    # Registered but not yet implemented — shown as "coming soon" in the UI.
    "google": ProviderSpec(
        key="google",
        label="Google (Gemini)",
        default_model="",
        suggested_models=[],
        implemented=False,
        key_help="Coming soon.",
    ),
    "openai": ProviderSpec(
        key="openai",
        label="OpenAI",
        default_model="",
        suggested_models=[],
        implemented=False,
        key_help="Coming soon.",
    ),
}


def get_provider_spec(key: str) -> ProviderSpec | None:
    return PROVIDERS.get(key)


def implemented_providers() -> list[ProviderSpec]:
    return [p for p in PROVIDERS.values() if p.implemented]
