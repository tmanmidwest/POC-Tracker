"""Generate slide-ready bullets for an executive readout deck.

Unlike ``summaries`` (which writes flowing prose for the on-screen summary), this
asks the configured provider for short, punchy bullets suited to slides: a few
executive-summary points and a few recommended next steps. Output is strict JSON
so the deck builder can drop it straight into ``ReadoutNarrative``. Any failure
(no provider, bad key, unparseable response) returns ``None`` — the deck then
falls back to its deterministic content, so a readout can always be produced.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import Project
from app.services.ai.base import GenerationError
from app.services.ai.registry import get_provider_spec
from app.services.ai.summaries import _build_context, default_provider
from app.services.report_pptx import ReadoutNarrative
from app.services.secret_box import decrypt_secret

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You prepare executive readout slides for proof-of-concept (POC) sales "
    "engagements. Audience: account executives and customer stakeholders. Be "
    "specific and grounded strictly in the data provided — never invent facts, "
    "names, numbers, or use cases. Write crisp, slide-ready bullets: no more than "
    "~14 words each, no trailing punctuation, no markdown. Respond with ONLY a "
    "JSON object of the form "
    '{"summary": ["..."], "next_steps": ["..."]}. '
    "Give 3-4 summary bullets (outcome and momentum first) and 2-4 next_steps "
    "bullets (concrete actions to move toward a decision). No other keys, no prose."
)

_MAX_BULLETS = 5


def generate_readout_narrative(db: Session, project: Project) -> ReadoutNarrative | None:
    """Return AI bullets for ``project``'s readout deck, or ``None`` on any failure."""
    provider = default_provider(db)
    if provider is None or not provider.has_key:
        return None
    spec = get_provider_spec(provider.provider)
    if spec is None or not spec.implemented or spec.generate is None:
        return None

    try:
        text = spec.generate(
            api_key=decrypt_secret(provider.api_key_encrypted),
            model=provider.model,
            system=_SYSTEM_PROMPT,
            prompt=_build_context(project),
            usage={},
        )
    except GenerationError:
        log.info("readout_narrative_generation_failed", extra={"project_id": project.id})
        return None

    parsed = _parse(text)
    if parsed is None:
        log.info("readout_narrative_unparseable", extra={"project_id": project.id})
        return None

    provider.last_used_at = datetime.now(UTC)
    db.flush()
    summary, next_steps = parsed
    narrative = ReadoutNarrative(
        summary=summary,
        next_steps=next_steps,
        model_label=f"{provider.provider}/{provider.model}",
    )
    return None if narrative.is_empty() else narrative


def _parse(text: str) -> tuple[list[str], list[str]] | None:
    """Pull the JSON object out of the model response and clean the bullet lists."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _clean(data.get("summary")), _clean(data.get("next_steps"))


def _clean(value) -> list[str]:
    """Coerce a value into a trimmed, capped list of non-empty bullet strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= _MAX_BULLETS:
            break
    return out
