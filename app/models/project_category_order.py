"""Per-project ordering for use-case category sections.

Use-case categories are free text typed onto each use case (see
``ProjectUseCase.category``) — there is no category lookup table. By default the
project detail page lists the category sections alphabetically. This model lets
a user pin an explicit order to individual categories within one project: each
row assigns a ``sort_order`` number to a category name. Numbered categories sort
first (ascending by number); un-numbered categories fall back to alphabetical
after them. One row per (project, category).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProjectCategoryOrder(Base):
    """An explicit sort position for one use-case category within a project."""

    __tablename__ = "project_category_orders"
    __table_args__ = (
        UniqueConstraint("project_id", "category", name="uq_project_category_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(150), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<ProjectCategoryOrder project={self.project_id} "
            f"category={self.category!r} order={self.sort_order}>"
        )
