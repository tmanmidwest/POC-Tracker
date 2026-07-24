"""Capability reference table (mirror of the code-defined registry).

Rows here are **reconciled from code** at startup (see
``app.services.rbac.registry.reconcile_capabilities``) — the source of truth is
``app.services.rbac.capabilities.CAPABILITIES``, not this table. The table exists
so ``role_capabilities.capability_key`` has a real foreign key to point at, and
so labels/areas are available for SQL-side rendering.

Primary key is the ``key`` slug itself (e.g. ``project.edit``); there is no
surrogate id. ``area``/``label``/``description``/``sort_order`` are denormalized
copies of the registry entry, refreshed on every reconcile.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Capability(Base):
    """One grantable permission, mirrored from the code registry."""

    __tablename__ = "capabilities"

    # The resource.action slug is the natural primary key.
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    area: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    # Global display order, seeded from the registry's declaration order.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Capability key={self.key!r}>"
