"""Single-row app branding configuration (name, accent color, icon).

There is only ever one row, with a fixed primary key of 1. It customizes the
brand name, the accent color applied to the name + icon, and which preset icon
is shown in the sidebar, login page, and favicon. See app.services.branding for
the icon presets and the read-side cache.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin
from app.services.branding import DEFAULT_ICON, DEFAULT_NAME

# The singleton row always uses this primary key.
BRANDING_ID = 1


class AppBranding(Base, TimestampMixin):
    """Branding settings for the app shell and login page (one row only)."""

    __tablename__ = "app_branding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=BRANDING_ID)
    brand_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default=DEFAULT_NAME
    )
    # Hex like "#1e293b". Empty string means "use the theme default".
    brand_color: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    icon_key: Mapped[str] = mapped_column(
        String(50), nullable=False, default=DEFAULT_ICON
    )

    def __repr__(self) -> str:
        return f"<AppBranding name={self.brand_name!r} icon={self.icon_key!r}>"
