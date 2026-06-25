"""Use-case status lookup table model.

Per-use-case status within a project, e.g. Pending Testing, Testing in
Progress, Completed. The "completed" status gates screenshot uploads in the UI.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class UseCaseStatus(Base, TimestampMixin):
    """A status for a use case inside a project (pickable global list)."""

    __tablename__ = "use_case_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # True for the "Completed" status — used for progress metrics and to signal
    # that screenshots are expected.
    is_complete_status: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<UseCaseStatus name={self.name!r}>"
