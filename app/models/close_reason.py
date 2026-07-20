"""Close-reason lookup table model.

Reasons a POC engagement closed the way it did — e.g. "Chose competitor",
"Budget cut", "Technical fit", "Timeline". Attached to a project when it reaches
a terminal status so win/loss analytics can group by *why* deals were won or
lost. Admin-managed master list, mirroring ``contact_roles``.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class CloseReason(Base, TimestampMixin):
    """A reason a POC closed won or lost (pickable master list)."""

    __tablename__ = "close_reasons"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<CloseReason name={self.name!r}>"
