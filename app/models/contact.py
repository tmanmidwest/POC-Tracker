"""Customer contact model — a person at the prospect/customer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.contact_role import ContactRole

if TYPE_CHECKING:
    from app.models.customer import Customer


class Contact(Base, TimestampMixin):
    """A contact at a customer. Role is picked from the ContactRole master list."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    role_id: Mapped[int | None] = mapped_column(
        ForeignKey("contact_roles.id"), nullable=True, index=True
    )

    customer: Mapped[Customer] = relationship("Customer", back_populates="contacts")
    role: Mapped[ContactRole | None] = relationship("ContactRole", lazy="joined")

    def __repr__(self) -> str:
        return f"<Contact name={self.name!r} customer_id={self.customer_id}>"
