"""Extract candidate use cases from a requirements document using the AI provider.

Provider-agnostic: asks the configured model to return a JSON array and parses it
robustly (tolerating code fences and surrounding prose), so it works the same way
no matter which provider is selected. The caller previews the candidates before
anything is written to the project.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.ai.base import GenerationError
from app.services.ai.registry import get_provider_spec
from app.services.ai.summaries import default_provider
from app.services.secret_box import decrypt_secret

log = logging.getLogger(__name__)

# Hard cap so a huge document can't create thousands of rows in one go.
MAX_CANDIDATES = 200
# Cap input size sent to the model (characters) — keeps token cost bounded.
MAX_INPUT_CHARS = 60_000

_SYSTEM_PROMPT = (
    "You extract proof-of-concept (POC) use cases / functional requirements from a "
    "document. Return ONLY a JSON array (no prose, no markdown fences). Each element is "
    'an object with these string keys: "reference_number" (the requirement number if '
    'present, else ""), "category" (a short grouping like "Access intelligence"), '
    '"name" (a concise title), "description" (1-3 sentences), and "success_validation" '
    '(how it would be proven, or ""). Preserve the document\'s own numbering and '
    "categories. Do not invent requirements that are not in the text."
)


@dataclass
class CandidateUseCase:
    reference_number: str
    category: str
    name: str
    description: str
    success_validation: str


def extract_use_cases(db: Session, text: str) -> list[CandidateUseCase]:
    """Extract candidate use cases from ``text``. Raises GenerationError on failure."""
    text = (text or "").strip()
    if not text:
        raise GenerationError("Paste or upload some requirements text first.")

    provider = default_provider(db)
    if provider is None:
        raise GenerationError(
            "No AI provider is configured. Add one in Settings → AI Assistant."
        )
    spec = get_provider_spec(provider.provider)
    if spec is None or not spec.implemented or spec.generate is None:
        raise GenerationError(
            f"The '{provider.provider}' provider is not available for extraction."
        )
    if not provider.has_key:
        raise GenerationError("The selected provider has no API key configured.")

    prompt = "Requirements document:\n\n" + text[:MAX_INPUT_CHARS]
    raw = spec.generate(
        api_key=decrypt_secret(provider.api_key_encrypted),
        model=provider.model,
        system=_SYSTEM_PROMPT,
        prompt=prompt,
        max_tokens=8000,
    )
    return _parse(raw)


def _parse(raw: str) -> list[CandidateUseCase]:
    """Parse the model's reply into candidates, tolerating fences/prose."""
    payload = _extract_json_array(raw)
    if payload is None:
        raise GenerationError("The model did not return a parseable list of use cases.")
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise GenerationError(f"Could not parse the extracted use cases: {exc}") from exc
    if not isinstance(data, list):
        raise GenerationError("The model returned an unexpected format.")

    candidates: list[CandidateUseCase] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue  # a use case must at least have a name
        candidates.append(
            CandidateUseCase(
                reference_number=str(item.get("reference_number") or "").strip(),
                category=str(item.get("category") or "Uncategorized").strip()
                or "Uncategorized",
                name=name[:255],
                description=str(item.get("description") or "").strip(),
                success_validation=str(item.get("success_validation") or "").strip(),
            )
        )
        if len(candidates) >= MAX_CANDIDATES:
            break
    if not candidates:
        raise GenerationError("No use cases were found in that document.")
    return candidates


def _extract_json_array(raw: str) -> str | None:
    """Pull the JSON array substring out of a model reply."""
    if not raw:
        return None
    text = raw.strip()
    # Strip a leading ```json / ``` fence if present.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: -3]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + 1]
