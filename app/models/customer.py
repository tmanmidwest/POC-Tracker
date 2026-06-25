"""Customer (prospect) model — the company a POC is run for."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.models.project import Project


class Customer(Base, TimestampMixin):
    """A prospect / customer company. Contacts and projects hang off this."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    contacts: Mapped[list[Contact]] = relationship(
        "Contact",
        back_populates="customer",
        cascade="all, delete-orphan",
    )
    projects: Mapped[list[Project]] = relationship(
        "Project",
        back_populates="customer",
    )

    def __repr__(self) -> str:
        return f"<Customer name={self.name!r}>"
