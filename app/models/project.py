"""POC Project model — the central table."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser
from app.models.close_reason import CloseReason
from app.models.customer import Customer
from app.models.project_status import ProjectStatus
from app.models.project_type import ProjectType
from app.models.region import Region

if TYPE_CHECKING:
    from app.models.project_category_order import ProjectCategoryOrder
    from app.models.project_milestone import ProjectMilestone
    from app.models.project_note import ProjectNote
    from app.models.project_use_case import ProjectUseCase
    from app.models.task import Task


class Project(Base, TimestampMixin):
    """A proof-of-concept engagement run for a customer.

    The Sales Engineer is an app user (they log in and edit). The Account
    Executive is tracked as plain reference fields — in phase 1 AEs do not log
    in, so they are not modeled as users.
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    # Optional label so one customer can have multiple POCs (e.g. "Q3 ISPM POC").
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status_id: Mapped[int] = mapped_column(
        ForeignKey("project_statuses.id"), nullable=False, index=True
    )

    # Kind of engagement (Workshop, POC Playbook, POC Full Stack, …). Optional —
    # the dashboard groups by it and buckets untyped projects separately.
    type_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_types.id"), nullable=True, index=True
    )

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Sales Engineer assigned to run the POC — an app user.
    sales_engineer_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id"), nullable=True, index=True
    )

    # Region this POC belongs to — the axis for region-based access control. An
    # SE only sees POCs in their own region; a manager sees POCs across
    # their assigned regions. Nullable during rollout (backfilled from the SE's
    # region in Phase 4; orphans land in the "Unassigned" region). No DB-level FK
    # on the projects table (SQLite can't add one without recreating it, which
    # would drop the FTS si_project_* triggers) — the ORM relationship enforces it.
    region_id: Mapped[int | None] = mapped_column(
        ForeignKey("regions.id"), nullable=True, index=True
    )

    # Account Executive — tracked by reference only (no login in phase 1).
    account_executive: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_executive_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Full URL to the Salesforce opportunity; shown in the UI as a short
    # "Salesforce Opp" hyperlink rather than the raw URL.
    salesforce_opp_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Full URL to a notebook (e.g. a shared analysis/notebook); shown as a
    # "Notebook Link" hyperlink in the UI.
    notebook_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Full URL to the POC instance/environment; shown as a "POC Instance"
    # hyperlink in the UI.
    poc_instance_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Free-text project notes. ``notes`` holds a plain-text rendering (search/
    # export/fallback); ``notes_html`` holds sanitized rich-text HTML when present.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes_html: Mapped[str | None] = mapped_column(Text, nullable=True)

    # AI-generated executive summary. ``exec_summary`` is plain text; the HTML
    # variant holds the editable rich-text rendering shown in the UI/report.
    exec_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    exec_summary_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    exec_summary_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Which provider/model produced the current summary, e.g. "anthropic/claude-opus-4-8".
    exec_summary_model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # Total tokens the AI used to produce the current summary (input + output).
    exec_summary_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Win/loss close details, filled in when the project reaches a terminal
    # status. The won/lost outcome itself lives on the status (see
    # ``ProjectStatus.outcome``); these capture the surrounding context.
    #   - close_reason_id: why it closed (admin-managed lookup)
    #   - competitor: who we lost to / were up against (free text)
    #   - closed_date: when it closed — drives cycle-time analytics (vs
    #     ``start_date``); distinct from ``updated_at``, which any edit bumps.
    close_reason_id: Mapped[int | None] = mapped_column(
        ForeignKey("close_reasons.id"), nullable=True, index=True
    )
    competitor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    closed_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    customer: Mapped[Customer] = relationship(
        "Customer", back_populates="projects", lazy="joined"
    )
    status: Mapped[ProjectStatus] = relationship("ProjectStatus", lazy="joined")
    type: Mapped[ProjectType | None] = relationship("ProjectType", lazy="joined")
    close_reason: Mapped[CloseReason | None] = relationship(
        "CloseReason", lazy="joined"
    )
    sales_engineer: Mapped[AppUser | None] = relationship("AppUser", lazy="joined")
    region: Mapped[Region | None] = relationship("Region", lazy="joined")
    use_cases: Mapped[list[ProjectUseCase]] = relationship(
        "ProjectUseCase",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    # Dated journal entries, ordered newest-first for display.
    note_entries: Mapped[list[ProjectNote]] = relationship(
        "ProjectNote",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectNote.note_date.desc(), ProjectNote.id.desc()",
    )
    # User tasks assigned to this project. Tasks are user-owned and survive the
    # project's deletion (their project_id is set null), so this is not a cascade.
    tasks: Mapped[list[Task]] = relationship("Task", back_populates="project")
    # Lifecycle milestones — project-owned, ordered for the timeline. Deleted with
    # the project.
    milestones: Mapped[list["ProjectMilestone"]] = relationship(
        "ProjectMilestone",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectMilestone.sort_order, ProjectMilestone.target_date",
    )
    # Optional explicit sort numbers for use-case category sections (one row per
    # numbered category). Categories without a row fall back to alphabetical.
    category_orders: Mapped[list["ProjectCategoryOrder"]] = relationship(
        "ProjectCategoryOrder",
        cascade="all, delete-orphan",
    )

    @property
    def display_name(self) -> str:
        """Human label: the explicit name, or fall back to the customer name."""
        if self.name:
            return self.name
        return self.customer.name if self.customer else f"Project {self.id}"

    def __repr__(self) -> str:
        return f"<Project id={self.id} customer_id={self.customer_id}>"
