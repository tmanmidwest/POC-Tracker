"""Feature / platform type lookup table model.

Global list of the feature/platform area a use case exercises, e.g. JML,
ISPM, Certifications, NHI, AI. Admin-managed master list.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class FeatureType(Base, TimestampMixin):
    """A feature/platform type a use case belongs to (pickable global list)."""

    __tablename__ = "feature_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<FeatureType name={self.name!r}>"
