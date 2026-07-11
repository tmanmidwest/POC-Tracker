"""Generate a short Salesforce status update for a POC project (ephemeral).

Unlike the executive summary, this is a throwaway artifact scoped to a date
range: the SE generates it, copies it into the Salesforce opportunity, and it is
never persisted. It focuses on what actually happened during the window — notes
logged and use cases completed between the given dates — so it reads like a
periodic status update rather than a full project summary.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime

from app.db import get_session_factory
from app.models import Project
from app.services.ai.base import GenerationError
from app.services.ai.registry import get_provider_spec
from app.services.ai.summaries import default_provider
from app.services.audit import record_event
from app.services.secret_box import decrypt_secret

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You write brief, factual status updates for a Sales Engineer to paste into a "
    "Salesforce opportunity. Audience: the internal account team. Summarize concretely "
    "what happened during the reporting period — progress made, use cases completed, "
    "notable updates, and any blockers or clear next steps. Be specific and grounded "
    "only in the data provided; never invent facts, names, dates, or numbers. Write "
    "1-2 short paragraphs of plain prose. No markdown, no headers, no bullet lists, no "
    "preamble, no greeting, and no sign-off."
)


def stream_salesforce_update(
    project_id: int,
    start: date | None,
    end: date | None,
    *,
    actor_label: str | None = None,
) -> Iterator[str]:
    """Stream a Salesforce status update for the given date range. Not persisted.

    Owns its own DB session so it stays valid for the life of the HTTP stream (the
    request's session closes once the route returns the response). Yields text
    chunks; a provider without streaming falls back to a single non-streamed
    chunk. ``actor_label`` (the requesting user) only attributes a failure event.
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

        context, has_activity = _build_context(project, start, end)
        if not has_activity:
            yield (
                "No notes or completed use cases fall in the selected date range. "
                "Widen the range or add an update first, then try again."
            )
            return

        key = decrypt_secret(provider.api_key_encrypted)
        try:
            if spec.stream is not None:
                for chunk in spec.stream(
                    api_key=key, model=provider.model,
                    system=_SYSTEM_PROMPT, prompt=context,
                ):
                    yield chunk
            elif spec.generate is not None:
                yield spec.generate(
                    api_key=key, model=provider.model,
                    system=_SYSTEM_PROMPT, prompt=context,
                )
        except GenerationError as exc:
            record_event(
                category="project", event_type="salesforce_update.failed", outcome="failure",
                actor_type="user" if actor_label else "system",
                actor_label=actor_label or "ai-stream",
                target_type="project", target_id=project.id,
                target_label=project.display_name,
                message=f"Salesforce update streaming failed for '{project.display_name}'",
                detail={"surface": "ui-stream", "error": str(exc)},
            )
            yield f"\n\n⚠️ {exc}"
            return

        provider.last_used_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()


def _build_context(
    project: Project, start: date | None, end: date | None
) -> tuple[str, bool]:
    """Render range-scoped project activity as plain text.

    Returns ``(prompt, has_activity)`` where ``has_activity`` is False when nothing
    (no completed use cases, no notes) falls in the window — the caller uses that
    to avoid asking the model to write an update from nothing.
    """
    def in_range(d: date | None) -> bool:
        if d is None:
            return False
        if start and d < start:
            return False
        if end and d > end:
            return False
        return True

    lines: list[str] = []
    lines.append(f"Customer: {project.customer.name if project.customer else '—'}")
    lines.append(f"Project: {project.display_name}")
    lines.append(f"Status: {project.status.name if project.status else '—'}")
    if project.account_executive:
        lines.append(f"Account executive: {project.account_executive}")
    period = (
        f"{start.isoformat() if start else 'beginning'} "
        f"to {end.isoformat() if end else 'today'}"
    )
    lines.append(f"Reporting period: {period}")

    def effective_date(uc) -> date | None:
        """When a use case counts as completed: its ``completed_on`` if set, else
        the date its record last changed (a proxy for when it was marked done)."""
        if uc.completed_on:
            return uc.completed_on
        if uc.updated_at:
            return uc.updated_at.date()
        return None

    use_cases = list(project.use_cases)
    total = len(use_cases)
    done = sum(1 for uc in use_cases if uc.status and uc.status.is_complete_status)
    lines.append(f"Overall use-case progress: {done} of {total} complete")

    completed = [
        uc
        for uc in use_cases
        if uc.status and uc.status.is_complete_status and in_range(effective_date(uc))
    ]
    if completed:
        lines.append("")
        lines.append("Use cases completed during this period:")
        for uc in sorted(completed, key=lambda u: effective_date(u) or date.min):
            ref = f"[{uc.reference_number}] " if uc.reference_number else ""
            # Be honest about the date's source: an exact completion date vs. the
            # "last updated" proxy, so the model doesn't present a guess as fact.
            if uc.completed_on:
                when = f"completed {uc.completed_on.isoformat()}"
            else:
                when = f"marked complete around {effective_date(uc).isoformat()} (last updated)"
            lines.append(f"  - {ref}{uc.name} ({uc.category}) — {when}")
            if uc.comments:
                lines.append(f"      note: {uc.comments.strip()}")

    notes = [n for n in project.note_entries if in_range(n.note_date)]
    if notes:
        lines.append("")
        lines.append("Notes logged during this period:")
        for n in sorted(notes, key=lambda x: x.note_date):
            body = (n.body or "").strip()
            if body:
                lines.append(f"  - {n.note_date.isoformat()}: {body}")

    return "\n".join(lines), bool(completed or notes)
