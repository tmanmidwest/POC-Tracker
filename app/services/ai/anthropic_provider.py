"""Anthropic (Claude) text generation via the Messages API.

Implemented over ``httpx`` (already a project dependency) rather than the
``anthropic`` SDK so the provider layer stays uniform across vendors and adds no
new dependency. One blocking POST per generation — summaries are short, so this
is simpler than streaming for phase one.
"""

from __future__ import annotations

import logging

import httpx

from app.services.ai.base import GenerationError

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def generate(
    *,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 1500,
) -> str:
    """Generate text with a Claude model. Raises GenerationError on failure."""
    if not api_key:
        raise GenerationError("No API key is configured for this provider.")

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    try:
        resp = httpx.post(API_URL, json=payload, headers=headers, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise GenerationError(f"Could not reach Anthropic: {exc}") from exc

    if resp.status_code != 200:
        raise GenerationError(_friendly_error(resp))

    data = resp.json()
    if data.get("stop_reason") == "refusal":
        raise GenerationError(
            "The model declined to generate this summary. Try rephrasing the project notes."
        )
    parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    text = "".join(parts).strip()
    if not text:
        raise GenerationError("The model returned an empty response.")
    return text


def _friendly_error(resp: httpx.Response) -> str:
    """Turn a non-200 response into a short, key-free message."""
    detail = ""
    try:
        body = resp.json()
        detail = (body.get("error") or {}).get("message", "")
    except ValueError:
        detail = (resp.text or "")[:200]

    if resp.status_code in (401, 403):
        return "Anthropic rejected the API key (check the key and its permissions)."
    if resp.status_code == 404:
        return "Unknown model id. Check the model name for this provider."
    if resp.status_code == 429:
        return "Anthropic rate limit hit. Wait a moment and try again."
    if resp.status_code >= 500:
        return "Anthropic had a server error. Try again shortly."
    return f"Anthropic returned an error ({resp.status_code}): {detail}".strip()
