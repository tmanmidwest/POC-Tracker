"""Generate an executive summary for a POC project using the default provider."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.models import AIProvider, Project
from app.services.ai.base import GenerationError
from app.services.audit import record_event
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
    total_tokens: int = 0  # input + output tokens the provider reported


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
    usage: dict = {}
    text = spec.generate(
        api_key=decrypt_secret(provider.api_key_encrypted),
        model=provider.model,
        system=_SYSTEM_PROMPT,
        prompt=prompt,
        usage=usage,
    )

    provider.last_used_at = datetime.now(UTC)
    db.flush()
    return SummaryResult(
        text=text,
        html=_text_to_html(text),
        model_label=f"{provider.provider}/{provider.model}",
        total_tokens=int(usage.get("total_tokens") or 0),
    )


def stream_project_summary(
    project_id: int, *, actor_label: str | None = None
) -> Iterator[str]:
    """Stream an executive summary and persist it when the stream completes.

    Owns its own DB session so it stays valid for the life of the HTTP stream
    (the request's session is closed once the route returns the response). Yields
    text chunks; on completion, saves the accumulated summary to the project. A
    provider without streaming falls back to a single non-streamed chunk.

    ``actor_label`` (the requesting user) is only used to attribute a mid-stream
    failure event in the activity log.
    """
    db = get_session_factory()()
    try:
        project = db.get(Project, project_id)
        if project is None:
            return
        provider = default_provider(db)
        spec = get_provider_spec(provider.provider) if provider else None
        if provider is None or spec is None or not spec.implemented or not provider.has_key:
            yield "\n\n⚠️ No usable AI provider is configured."
            return

        key = decrypt_secret(provider.api_key_encrypted)
        prompt = _build_context(project)
        parts: list[str] = []
        usage: dict = {}
        try:
            if spec.stream is not None:
                for chunk in spec.stream(
                    api_key=key, model=provider.model,
                    system=_SYSTEM_PROMPT, prompt=prompt, usage=usage,
                ):
                    parts.append(chunk)
                    yield chunk
            elif spec.generate is not None:
                text = spec.generate(
                    api_key=key, model=provider.model,
                    system=_SYSTEM_PROMPT, prompt=prompt, usage=usage,
                )
                parts.append(text)
                yield text
        except GenerationError as exc:
            record_event(
                category="project", event_type="exec_summary.failed", outcome="failure",
                actor_type="user" if actor_label else "system",
                actor_label=actor_label or "ai-stream",
                target_type="project", target_id=project.id,
                target_label=project.display_name,
                message=f"Executive summary streaming failed for '{project.display_name}'",
                detail={"surface": "ui-stream", "error": str(exc)},
            )
            yield f"\n\n⚠️ {exc}"
            return

        full = "".join(parts).strip()
        if full:
            now = datetime.now(UTC)
            project.exec_summary = full
            project.exec_summary_html = _text_to_html(full)
            project.exec_summary_generated_at = now
            project.exec_summary_model = f"{provider.provider}/{provider.model}"
            project.exec_summary_tokens = int(usage.get("total_tokens") or 0) or None
            provider.last_used_at = now
            db.commit()
    finally:
        db.close()


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
