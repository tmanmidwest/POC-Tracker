"""Library set model — a named container for use-case library entries.

Each UseCaseLibrary entry belongs to exactly one library set. The seeded
"Standard" set holds everything that existed before named libraries; additional
sets let you carve out per-product or early-adoption use cases. Projects can
pull from any number of sets (the copy is still an independent snapshot).
"""

from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class LibrarySet(Base, TimestampMixin):
    """A named use-case library."""

    __tablename__ = "library_sets"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The pinned primary library: the landing/fallback when none is selected, and
    # protected from deletion. Exactly one library should have this set.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<LibrarySet id={self.id} name={self.name!r}>"
