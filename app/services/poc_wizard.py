"""Orchestration for the "New POC" wizard.

The wizard collects a whole POC — customer, project, use cases, and tasks — in a
single client-side flow and submits it once. This module turns that submission
into database rows inside a *single transaction*: the caller commits exactly
once, so a failure anywhere leaves nothing behind (no orphaned customer or empty
project).

Creation logic is not duplicated here — this reuses the same building blocks the
individual create routes use (``copy_library_entries_to_project``,
``default_*_status_id``), so wizard-created POCs are identical to hand-built ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Project,
    ProjectMilestone,
    ProjectUseCase,
    Task,
    TaskStatus,
)
from app.models.project_use_case import SOURCE_CUSTOM
from app.services.milestones import seed_project_milestones
from app.services.use_cases import (
    copy_library_entries_to_project,
    default_project_status_id,
    default_use_case_status_id,
)


class WizardError(ValueError):
    """A user-correctable problem with a wizard submission (bad/missing input)."""


@dataclass
class CustomUseCaseInput:
    category: str
    name: str
    description: str | None = None
    success_validation: str | None = None


@dataclass
class TaskInput:
    title: str
    start_date: date | None = None
    due_date: date | None = None


@dataclass
class MilestoneInput:
    name: str
    target_date: date | None = None


@dataclass
class WizardInput:
    # Customer: exactly one of these is used. An existing id wins if both are set.
    existing_customer_id: int | None = None
    new_customer: dict | None = None  # {"name", "website", "notes"}
    # Project field set, mirroring the fields of POST /ui/projects/new.
    project: dict = field(default_factory=dict)
    library_ids: list[int] = field(default_factory=list)
    custom_use_cases: list[CustomUseCaseInput] = field(default_factory=list)
    tasks: list[TaskInput] = field(default_factory=list)
    # Lifecycle milestones. Empty means "use the global default set" — an
    # explicitly cleared timeline is expressed by ``skip_milestones``.
    milestones: list[MilestoneInput] = field(default_factory=list)
    skip_milestones: bool = False


def _default_task_status_id(db: Session) -> int | None:
    """Lowest-sort active, non-terminal task status (an "open" starting state)."""
    row = db.scalar(
        select(TaskStatus)
        .where(TaskStatus.is_terminal.is_(False), TaskStatus.is_active.is_(True))
        .order_by(TaskStatus.sort_order)
        .limit(1)
    )
    return row.id if row else None


def _resolve_customer(db: Session, data: WizardInput) -> Customer:
    if data.existing_customer_id:
        customer = db.get(Customer, data.existing_customer_id)
        if customer is None:
            raise WizardError("The selected customer no longer exists.")
        return customer

    info = data.new_customer or {}
    name = (info.get("name") or "").strip()
    if not name:
        raise WizardError("A customer is required — pick an existing one or enter a new name.")
    # Customer.name is unique; check up front so a duplicate gives a clean error
    # instead of an IntegrityError that would poison the whole transaction.
    if db.scalar(select(Customer).where(Customer.name == name)) is not None:
        raise WizardError(f"A customer named '{name}' already exists — select it instead.")
    customer = Customer(
        name=name,
        website=(info.get("website") or "").strip() or None,
        notes=(info.get("notes") or "").strip() or None,
    )
    db.add(customer)
    db.flush()  # assign customer.id for the project FK
    return customer


def create_poc_from_wizard(db: Session, user, data: WizardInput) -> Project:
    """Create a customer (if new), project, use cases, and tasks in one unit.

    Does NOT commit — the caller owns the transaction so the whole POC lands
    atomically (or not at all). Returns the created project (flushed, so its id
    and relationships are populated). Raises :class:`WizardError` for
    user-correctable input problems.
    """
    customer = _resolve_customer(db, data)

    p = data.project
    project = Project(
        customer_id=customer.id,
        name=(p.get("name") or None),
        status_id=p.get("status_id") or default_project_status_id(db),
        type_id=p.get("type_id"),
        start_date=p.get("start_date"),
        end_date=p.get("end_date"),
        sales_engineer_id=p.get("sales_engineer_id"),
        account_executive=p.get("account_executive"),
        account_executive_email=p.get("account_executive_email"),
        salesforce_opp_url=p.get("salesforce_opp_url"),
        notebook_url=p.get("notebook_url"),
        poc_instance_url=p.get("poc_instance_url"),
        notes=p.get("notes"),
        notes_html=p.get("notes_html"),
    )
    db.add(project)
    db.flush()  # assign project.id for use-case/task FKs

    # Use cases from the library (snapshotted, de-duped by the shared helper).
    copy_library_entries_to_project(db, project, data.library_ids)

    # Custom use cases typed into the wizard.
    uc_status_id = default_use_case_status_id(db)
    for c in data.custom_use_cases:
        db.add(
            ProjectUseCase(
                project_id=project.id,
                source=SOURCE_CUSTOM,
                category=c.category,
                name=c.name,
                description=c.description,
                success_validation=c.success_validation,
                status_id=uc_status_id,
            )
        )

    # Lifecycle milestones: whatever the wizard submitted (a template's set, or
    # hand-edited rows), else the global standard set. Project-owned, so they're
    # visible to the whole team unlike the per-user tasks below.
    if not data.skip_milestones:
        if data.milestones:
            for i, ms in enumerate(data.milestones):
                db.add(
                    ProjectMilestone(
                        project_id=project.id,
                        name=ms.name,
                        target_date=ms.target_date,
                        sort_order=(i + 1) * 10,
                    )
                )
        else:
            seed_project_milestones(db, project)

    # Optional kickoff tasks, owned by the creating user.
    if data.tasks:
        task_status_id = _default_task_status_id(db)
        for t in data.tasks:
            db.add(
                Task(
                    owner_user_id=user.id,
                    title=t.title,
                    status_id=task_status_id,
                    project_id=project.id,
                    start_date=t.start_date,
                    due_date=t.due_date,
                )
            )

    db.flush()
    return project
