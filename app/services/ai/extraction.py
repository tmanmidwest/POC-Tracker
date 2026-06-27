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

from app.models import Project
from app.services.ai.base import GenerationError
from app.services.ai.registry import get_provider_spec
from app.services.ai.summaries import default_provider
from app.services.secret_box import decrypt_secret

log = logging.getLogger(__name__)

# Hard cap so a huge document can't create thousands of rows in one go.
MAX_CANDIDATES = 200
# Cap input size sent to the model (characters) — keeps token cost bounded.
MAX_INPUT_CHARS = 60_000
# Generous output ceiling — a long requirements doc can yield many use cases, and
# JSON is verbose. Too low truncates the array mid-object (extraction then yields
# nothing). Extraction streams the response so a large output won't time out.
MAX_OUTPUT_TOKENS = 16_000

_SYSTEM_PROMPT = (
    "You extract proof-of-concept (POC) use cases / functional requirements from the "
    "provided document(s) and/or text. Read everything fully, including any attached "
    "files and their tables. Return ONLY a JSON array (no prose, no markdown fences). "
    'Each element is an object with these string keys: "reference_number" (the '
    'requirement number if present, else ""), "category" (a short grouping like '
    '"Access intelligence"), "name" (a concise title), "description" (1-3 sentences), '
    'and "success_validation" (how it would be proven, or ""). Preserve the document\'s '
    "own numbering and categories. Do not invent requirements that are not present. If a "
    "list of existing use cases is provided, do NOT repeat them — only return ones not "
    "already covered."
)


@dataclass
class CandidateUseCase:
    reference_number: str
    category: str
    name: str
    description: str
    success_validation: str


@dataclass
class ExtractionResult:
    candidates: list[CandidateUseCase]
    provider: str        # registry key, e.g. "anthropic"
    provider_label: str  # human label, e.g. "Anthropic (Claude)"
    model: str           # e.g. "claude-opus-4-8"
    usage: dict          # {"input_tokens", "output_tokens", "total_tokens"} (may be empty)

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)


def extract_use_cases(
    db: Session,
    text: str,
    *,
    project: Project | None = None,
    documents: list[dict] | None = None,
) -> ExtractionResult:
    """Extract candidate use cases from pasted ``text`` and/or native ``documents``.

    ``documents`` is a list of ``{"media_type", "data"}`` (base64) attachments
    (PDFs/images) passed natively to the model. ``project``, if given, supplies the
    existing use cases so the model can avoid duplicates. Returns the candidates
    plus the provider/model used and token usage. Raises GenerationError.
    """
    text = (text or "").strip()
    if not text and not documents:
        raise GenerationError("Paste, upload, or attach some requirements first.")

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

    raw, usage = _collect(
        spec,
        api_key=decrypt_secret(provider.api_key_encrypted),
        model=provider.model,
        system=_SYSTEM_PROMPT,
        prompt=_build_prompt(text, project, has_documents=bool(documents)),
        max_tokens=MAX_OUTPUT_TOKENS,
        documents=documents or None,
    )
    return ExtractionResult(
        candidates=_parse(raw),
        provider=provider.provider,
        provider_label=spec.label,
        model=provider.model,
        usage=usage,
    )


def _collect(spec, **kwargs) -> tuple[str, dict]:
    """Get the model's full reply (and token usage), streaming when supported.

    Streaming keeps the connection fed during a long extraction, so a large
    output doesn't trip the HTTP idle-read timeout the way a single blocking
    response would. Falls back to one-shot generation otherwise.
    """
    usage: dict = {}
    if spec.stream is not None:
        text = "".join(spec.stream(usage=usage, **kwargs))
    else:
        text = spec.generate(usage=usage, **kwargs)
    return text, usage


def _build_prompt(text: str, project: Project | None, *, has_documents: bool) -> str:
    """Assemble the user prompt: existing-use-case context + the requirements."""
    sections: list[str] = []

    if project is not None and project.use_cases:
        lines = []
        for uc in list(project.use_cases)[:300]:
            ref = f"[{uc.reference_number}] " if uc.reference_number else ""
            lines.append(f"- {ref}{uc.name} ({uc.category})")
        sections.append(
            "This project ALREADY has the following use cases. Do not duplicate them — "
            "only return NEW requirements not already covered:\n" + "\n".join(lines)
        )

    if text:
        sections.append("Requirements text:\n\n" + text[:MAX_INPUT_CHARS])
    if has_documents:
        sections.append(
            "Additional requirements are in the attached document(s) — extract from them too."
        )
    if not text and not has_documents:  # defensive; caller already guards
        sections.append("Extract the use cases from the provided requirements.")

    return "\n\n".join(sections)


def _parse(raw: str) -> list[CandidateUseCase]:
    """Parse the model's reply into candidates, tolerating fences/prose/truncation."""
    data = _load_objects(raw)
    if not data:
        raise GenerationError(
            "The model didn't return any use cases. The document may be very large or in "
            "an unusual layout — try importing a section at a time, or paste the text."
        )

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


def _load_objects(raw: str) -> list:
    """Load the list of use-case objects from a model reply.

    First tries a clean parse of the JSON array (tolerating ```fences``` and
    surrounding prose). If that fails — most often because the array was
    truncated at the output-token limit — it recovers every *complete* object
    from the text, so a cut-off response still yields the items that fit instead
    of nothing.
    """
    if not raw:
        return []
    payload = _extract_json_array(raw)
    if payload:
        try:
            loaded = json.loads(payload)
            if isinstance(loaded, list):
                return loaded
        except ValueError:
            pass  # fall through to object recovery (likely truncated)
    recovered = _recover_objects(raw)
    if recovered:
        log.info("extraction_recovered_truncated", extra={"count": len(recovered)})
    return recovered


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


def _recover_objects(raw: str) -> list:
    """Recover every complete top-level JSON object from (possibly truncated) text.

    Walks the string with a JSON decoder, decoding one ``{...}`` object at a time
    and skipping past it; a trailing partial object (from truncation) simply stops
    the walk, keeping all the complete ones before it.
    """
    decoder = json.JSONDecoder()
    objects: list = []
    i = raw.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(raw, i)
        except ValueError:
            nxt = raw.find("{", i + 1)
            if nxt == -1:
                break
            i = nxt
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        i = raw.find("{", end)
    return objects
