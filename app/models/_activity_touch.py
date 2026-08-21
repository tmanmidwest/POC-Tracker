"""Keep ``Project.updated_at`` in step with activity on a project's children.

The dashboard's "stalled / no update in Nd" signal reads ``Project.updated_at``
(see ``app.services.insights.is_stalled``). Without this hook that timestamp only
moves when the *project record itself* is edited — so adding a note, changing a
use-case status, or updating a task would leave a busy POC looking stalled.

This ``before_flush`` listener bumps the parent project's ``updated_at`` whenever
one of its child records (notes, use cases, tasks, milestones) is inserted,
updated, or deleted in the same flush.

Guard rails:
- Projects that are themselves new/edited/deleted in the same flush are skipped,
  so their own ``updated_at`` (or their deletion) wins. This is what protects the
  demo seeder and any code that deliberately back-dates ``updated_at`` — those set
  it on the project directly, or via a Core ``UPDATE`` that never fires this event.
- A standalone task (``project_id is None``) is not project activity, so it is
  ignored.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_milestone import ProjectMilestone
from app.models.project_note import ProjectNote
from app.models.project_use_case import ProjectUseCase
from app.models.task import Task

_CHILD_TYPES = (ProjectNote, ProjectUseCase, ProjectMilestone, Task)


@event.listens_for(Session, "before_flush")
def _touch_project_on_child_activity(
    session: Session, flush_context: object, instances: object
) -> None:
    changed = (*session.new, *session.dirty, *session.deleted)

    affected: set[int] = set()
    for obj in changed:
        if isinstance(obj, _CHILD_TYPES):
            pid = getattr(obj, "project_id", None)
            if pid is not None:
                affected.add(pid)
    if not affected:
        return

    # Projects handled directly in this flush manage their own timestamp.
    skip = {
        obj.id
        for obj in changed
        if isinstance(obj, Project) and obj.id is not None
    }

    now = datetime.now(timezone.utc)
    for pid in affected - skip:
        project = session.get(Project, pid)
        if project is not None and project not in session.deleted:
            project.updated_at = now
