"""POC template CRUD and the mapping that pre-fills the wizard from a template.

Authoring is primarily "Save this POC as a template" — snapshotting a live
project's use cases and tasks. Applying a template does not create anything on its
own; it produces a *wizard context* (the same shape the wizard already uses to
re-render itself), so a selected template simply pre-fills the New POC wizard.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    PocTemplate,
    PocTemplateTask,
    PocTemplateUseCase,
    Project,
    UseCaseLibrary,
)
from app.models.poc_template_use_case import SOURCE_CUSTOM, SOURCE_LIBRARY


def list_templates(db: Session, *, include_inactive: bool = True) -> list[PocTemplate]:
    """All templates, active first, then by name."""
    query = select(PocTemplate)
    if not include_inactive:
        query = query.where(PocTemplate.is_active.is_(True))
    return list(
        db.scalars(
            query.order_by(PocTemplate.is_active.desc(), PocTemplate.name)
        ).all()
    )


def get_template(db: Session, template_id: int) -> PocTemplate | None:
    return db.get(PocTemplate, template_id)


def delete_template(db: Session, template: PocTemplate) -> None:
    """Delete a template and its use cases/tasks (cascade). Caller commits."""
    db.delete(template)


def _offset_days(start: date | None, target: date | None) -> int | None:
    """Whole-day offset of ``target`` from ``start`` (None if either is missing)."""
    if start is None or target is None:
        return None
    return (target - start).days


def create_template_from_project(
    db: Session,
    project: Project,
    *,
    name: str,
    description: str | None = None,
    created_by: str | None = None,
) -> PocTemplate:
    """Snapshot a project's use cases and tasks into a new template.

    Task dates are stored as offsets from the project's start date so they can be
    re-anchored when the template is later applied. Caller commits.
    """
    template = PocTemplate(
        name=name,
        description=description,
        created_by=created_by,
        default_status_id=project.status_id,
    )
    db.add(template)

    for i, uc in enumerate(project.use_cases):
        template.use_cases.append(
            PocTemplateUseCase(
                source=uc.source,
                library_id=uc.library_id,
                reference_number=uc.reference_number,
                category=uc.category,
                name=uc.name,
                description=uc.description,
                success_validation=uc.success_validation,
                feature_type_id=uc.feature_type_id,
                sort_order=i,
            )
        )

    start = project.start_date
    for i, task in enumerate(project.tasks):
        template.tasks.append(
            PocTemplateTask(
                title=task.title,
                details=task.details,
                start_offset_days=_offset_days(start, task.start_date),
                due_offset_days=_offset_days(start, task.due_date),
                sort_order=i,
            )
        )

    db.flush()
    return template


def _active_library_ids(db: Session) -> set[int]:
    return set(
        db.scalars(
            select(UseCaseLibrary.id).where(UseCaseLibrary.is_active.is_(True))
        ).all()
    )


def template_to_wizard_context(
    db: Session, template: PocTemplate, *, base_date: date
) -> dict:
    """Turn a template into the wizard's pre-fill context.

    Returns ``form`` defaults, ``selected_library_ids`` (to pre-check the picker),
    ``custom_rows`` and ``task_rows`` — the exact shape ``_render_wizard`` expects.
    Library-sourced use cases whose library entry is still active pre-check the
    picker; anything else falls back to a custom row from the stored snapshot, so a
    template keeps working even after its library entries change.
    """
    active_lib = _active_library_ids(db)
    selected_library_ids: set[int] = set()
    custom_rows: list[dict] = []
    for uc in template.use_cases:
        if uc.source == SOURCE_LIBRARY and uc.library_id in active_lib:
            selected_library_ids.add(uc.library_id)
        else:
            custom_rows.append(
                {
                    "category": uc.category,
                    "name": uc.name,
                    "description": uc.description,
                    "success_validation": uc.success_validation,
                }
            )

    task_rows: list[dict] = []
    for t in template.tasks:
        start = base_date + timedelta(days=t.start_offset_days) if t.start_offset_days is not None else None
        due = base_date + timedelta(days=t.due_offset_days) if t.due_offset_days is not None else None
        task_rows.append(
            {
                "title": t.title,
                "start_date": start.isoformat() if start else "",
                "due_date": due.isoformat() if due else "",
            }
        )

    return {
        "form": {"status_id": template.default_status_id},
        "selected_library_ids": selected_library_ids,
        "custom_rows": custom_rows,
        "task_rows": task_rows,
    }
