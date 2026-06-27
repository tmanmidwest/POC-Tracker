"""Google Gemini text generation via the Generative Language REST API.

Same shape as the Anthropic provider — over ``httpx``, with a matching
``generate`` / ``stream`` pair — so it drops straight into the registry. The API
key is passed in the ``x-goog-api-key`` header (kept out of the URL/logs).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx

from app.services.ai.base import GenerationError

log = logging.getLogger(__name__)

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _payload(
    system: str, prompt: str, max_tokens: int, documents: list[dict] | None = None
) -> dict:
    parts: list[dict] = []
    for doc in documents or []:
        parts.append({"inlineData": {"mimeType": doc.get("media_type", ""), "data": doc["data"]}})
    parts.append({"text": prompt})
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }


def _headers(api_key: str) -> dict[str, str]:
    return {"x-goog-api-key": api_key, "content-type": "application/json"}


def generate(
    *,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 1500,
    documents: list[dict] | None = None,
) -> str:
    """Generate text with a Gemini model. Raises GenerationError on failure.

    ``documents`` is an optional list of ``{"media_type", "data"}`` (base64)
    attachments sent natively (PDFs/images) via ``inlineData``.
    """
    if not api_key:
        raise GenerationError("No API key is configured for this provider.")

    url = f"{_BASE}/{model}:generateContent"
    try:
        resp = httpx.post(
            url, json=_payload(system, prompt, max_tokens, documents),
            headers=_headers(api_key), timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise GenerationError(f"Could not reach Google Gemini: {exc}") from exc

    if resp.status_code != 200:
        raise GenerationError(_friendly_error(resp))

    data = resp.json()
    _raise_if_blocked(data)
    text = _candidate_text(data)
    if not text:
        raise GenerationError("Gemini returned an empty response.")
    return text


def stream(
    *,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 1500,
) -> Iterator[str]:
    """Stream text chunks from a Gemini model via SSE. Raises GenerationError."""
    if not api_key:
        raise GenerationError("No API key is configured for this provider.")

    url = f"{_BASE}/{model}:streamGenerateContent?alt=sse"
    try:
        with httpx.stream(
            "POST", url, json=_payload(system, prompt, max_tokens),
            headers=_headers(api_key), timeout=_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                raise GenerationError(_friendly_error(resp))
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                _raise_if_blocked(event)
                chunk = _candidate_text(event)
                if chunk:
                    yield chunk
    except httpx.HTTPError as exc:
        raise GenerationError(f"Could not reach Google Gemini: {exc}") from exc


def _candidate_text(data: dict) -> str:
    """Join the text parts of the first candidate."""
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


def _raise_if_blocked(data: dict) -> None:
    """Turn a safety/recitation block into a GenerationError."""
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GenerationError(
            f"Gemini blocked the request ({feedback['blockReason']})."
        )
    for cand in data.get("candidates") or []:
        reason = cand.get("finishReason")
        if reason and reason not in ("STOP", "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED"):
            raise GenerationError(f"Gemini stopped early ({reason}).")


def _friendly_error(resp: httpx.Response) -> str:
    """Turn a non-200 response into a short, key-free message."""
    detail = ""
    try:
        detail = (resp.json().get("error") or {}).get("message", "")
    except ValueError:
        detail = (resp.text or "")[:200]

    if resp.status_code in (400, 401, 403):
        # Gemini returns 400 for a bad key and 403 for a disabled key/API.
        if "API key" in detail or "API_KEY" in detail:
            return "Google rejected the API key (check the key and that the API is enabled)."
        return f"Gemini rejected the request: {detail or resp.status_code}"
    if resp.status_code == 404:
        return "Unknown Gemini model id. Check the model name."
    if resp.status_code == 429:
        return "Gemini rate limit/quota hit. Wait a moment and try again."
    if resp.status_code >= 500:
        return "Gemini had a server error. Try again shortly."
    return f"Gemini returned an error ({resp.status_code}): {detail}".strip()
