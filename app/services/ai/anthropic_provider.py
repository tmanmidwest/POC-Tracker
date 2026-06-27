"""Anthropic (Claude) text generation via the Messages API.

Implemented over ``httpx`` (already a project dependency) rather than the
``anthropic`` SDK so the provider layer stays uniform across vendors and adds no
new dependency. One blocking POST per generation — summaries are short, so this
is simpler than streaming for phase one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

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
    documents: list[dict] | None = None,
) -> str:
    """Generate text with a Claude model. Raises GenerationError on failure.

    ``documents`` is an optional list of ``{"media_type", "data"}`` (base64)
    attachments — PDFs and images are sent natively so the model reads tables and
    layout directly, rather than from pre-flattened text.
    """
    if not api_key:
        raise GenerationError("No API key is configured for this provider.")

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": _user_content(prompt, documents)}],
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


def _user_content(prompt: str, documents: list[dict] | None) -> object:
    """Build the user message content: native doc/image blocks, then the text."""
    if not documents:
        return prompt
    blocks: list[dict] = []
    for doc in documents:
        media = doc.get("media_type", "")
        source = {"type": "base64", "media_type": media, "data": doc["data"]}
        if media.startswith("image/"):
            blocks.append({"type": "image", "source": source})
        else:  # PDFs (application/pdf) and anything else → document block
            blocks.append({"type": "document", "source": source})
    blocks.append({"type": "text", "text": prompt})
    return blocks


def stream(
    *,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 1500,
) -> Iterator[str]:
    """Stream text chunks from a Claude model via SSE. Raises GenerationError."""
    if not api_key:
        raise GenerationError("No API key is configured for this provider.")

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    try:
        with httpx.stream(
            "POST", API_URL, json=payload, headers=headers, timeout=_TIMEOUT
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                raise GenerationError(_friendly_error(resp))
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                etype = event.get("type")
                if etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        chunk = delta.get("text", "")
                        if chunk:
                            yield chunk
                elif etype == "error":
                    msg = (event.get("error") or {}).get("message", "stream error")
                    raise GenerationError(f"Anthropic stream error: {msg}")
                elif etype == "message_stop":
                    break
    except httpx.HTTPError as exc:
        raise GenerationError(f"Could not reach Anthropic: {exc}") from exc


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
