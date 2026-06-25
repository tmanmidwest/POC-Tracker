"""Contact role lookup table model.

Roles a customer contact can hold on a POC, e.g. Champion, Sourcing,
Technical Stakeholder, Business Stakeholder. Admin-managed master list.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ContactRole(Base, TimestampMixin):
    """A role a customer contact can hold (pickable master list)."""

    __tablename__ = "contact_roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<ContactRole name={self.name!r}>"
