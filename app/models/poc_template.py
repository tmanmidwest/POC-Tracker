"""POC template — a reusable blueprint for spinning up a new POC.

A template bundles a set of use cases and kickoff tasks (plus a default project
status) so the New POC wizard can pre-fill a whole engagement instead of building
it from scratch. Templates are authored either by hand or by snapshotting an
existing project ("Save this POC as a template"). Applying a template copies its
contents — it does not create a live link, so later edits to the template never
touch POCs already created from it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.poc_template_task import PocTemplateTask
    from app.models.poc_template_use_case import PocTemplateUseCase


class PocTemplate(Base, TimestampMixin):
    """A named, reusable POC blueprint."""

    __tablename__ = "poc_templates"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Optional default project status applied to POCs created from this template.
    default_status_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_statuses.id", ondelete="SET NULL"), nullable=True
    )
    # Username of whoever authored the template (display/audit only).
    created_by: Mapped[str | None] = mapped_column(String(150), nullable=True)

    use_cases: Mapped[list[PocTemplateUseCase]] = relationship(
        "PocTemplateUseCase",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="PocTemplateUseCase.sort_order",
    )
    tasks: Mapped[list[PocTemplateTask]] = relationship(
        "PocTemplateTask",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="PocTemplateTask.sort_order",
    )

    def __repr__(self) -> str:
        return f"<PocTemplate id={self.id} name={self.name!r}>"
