"""Generate an executive summary for a POC project using the default provider."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from sqlalchemy.orm import Session

from app.models import AIProvider, Project
from app.services.ai.base import GenerationError
from app.services.ai.registry import get_provider_spec
from app.services.rich_text import sanitize_note_html
from app.services.secret_box import decrypt_secret

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You write concise, executive-ready summaries of proof-of-concept (POC) sales "
    "engagements. Audience: account executives and customer stakeholders. Lead with "
    "the outcome and momentum, then key wins and any open items. Be specific and "
    "grounded in the data provided — never invent facts, names, or numbers. Write 2-4 "
    "short paragraphs of plain prose. No markdown headers, no bullet lists, no preamble."
)


@dataclass
class SummaryResult:
    text: str           # plain-text summary
    html: str           # sanitized HTML rendering (editable in the UI)
    model_label: str    # "anthropic/claude-opus-4-8"


def default_provider(db: Session) -> AIProvider | None:
    """The enabled provider used for generation: the default, else any enabled one."""
    q = db.query(AIProvider).filter(AIProvider.is_enabled.is_(True))
    return (
        q.filter(AIProvider.is_default.is_(True)).first()
        or q.order_by(AIProvider.id).first()
    )


def generate_project_summary(db: Session, project: Project) -> SummaryResult:
    """Generate (but do not save) an executive summary for ``project``.

    Raises GenerationError if no provider is configured or the call fails.
    """
    provider = default_provider(db)
    if provider is None:
        raise GenerationError(
            "No AI provider is configured. Add one in Settings → AI Assistant."
        )
    spec = get_provider_spec(provider.provider)
    if spec is None or not spec.implemented or spec.generate is None:
        raise GenerationError(
            f"The '{provider.provider}' provider is not available for generation."
        )
    if not provider.has_key:
        raise GenerationError("The selected provider has no API key configured.")

    prompt = _build_context(project)
    text = spec.generate(
        api_key=decrypt_secret(provider.api_key_encrypted),
        model=provider.model,
        system=_SYSTEM_PROMPT,
        prompt=prompt,
    )

    provider.last_used_at = datetime.now(UTC)
    db.flush()
    return SummaryResult(
        text=text,
        html=_text_to_html(text),
        model_label=f"{provider.provider}/{provider.model}",
    )


def _build_context(project: Project) -> str:
    """Render the project's data as plain text for the model."""
    lines: list[str] = []
    lines.append(f"Customer: {project.customer.name if project.customer else '—'}")
    lines.append(f"Project: {project.display_name}")
    lines.append(f"Status: {project.status.name if project.status else '—'}")
    if project.start_date:
        lines.append(f"Start date: {project.start_date.isoformat()}")
    if project.end_date:
        lines.append(f"End date: {project.end_date.isoformat()}")
    if project.account_executive:
        lines.append(f"Account executive: {project.account_executive}")

    use_cases = list(project.use_cases)
    total = len(use_cases)
    done = sum(1 for uc in use_cases if uc.status and uc.status.is_complete_status)
    lines.append(f"Use-case progress: {done} of {total} complete")

    if project.notes:
        lines.append("")
        lines.append("Project notes:")
        lines.append(project.notes.strip())

    if use_cases:
        lines.append("")
        lines.append("Use cases (grouped by category):")
        by_cat: dict[str, list] = {}
        for uc in use_cases:
            by_cat.setdefault(uc.category, []).append(uc)
        for category in sorted(by_cat):
            lines.append(f"  {category}:")
            for uc in by_cat[category]:
                status = uc.status.name if uc.status else "—"
                ref = f"[{uc.reference_number}] " if uc.reference_number else ""
                lines.append(f"    - {ref}{uc.name} — {status}")
                if uc.comments:
                    lines.append(f"        note: {uc.comments.strip()}")

    return "\n".join(lines)


def _text_to_html(text: str) -> str:
    """Wrap plain-text paragraphs in <p>, escape, and sanitize for storage."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    html = "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
    return sanitize_note_html(html)
