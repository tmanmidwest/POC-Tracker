"""POC Project model — the central table."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser
from app.models.customer import Customer
from app.models.project_status import ProjectStatus

if TYPE_CHECKING:
    from app.models.project_note import ProjectNote
    from app.models.project_use_case import ProjectUseCase


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

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Sales Engineer assigned to run the POC — an app user.
    sales_engineer_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id"), nullable=True, index=True
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

    # Free-text project notes. ``notes`` holds a plain-text rendering (search/
    # export/fallback); ``notes_html`` holds sanitized rich-text HTML when present.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes_html: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    sales_engineer: Mapped[AppUser | None] = relationship("AppUser", lazy="joined")
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

    @property
    def display_name(self) -> str:
        """Human label: the explicit name, or fall back to the customer name."""
        if self.name:
            return self.name
        return self.customer.name if self.customer else f"Project {self.id}"

    def __repr__(self) -> str:
        return f"<Project id={self.id} customer_id={self.customer_id}>"
