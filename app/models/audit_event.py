"""Append-only audit / activity event log.

Each row records one notable thing that happened in the app — a login, an
OAuth token grant, an OIDC SSO callback, a create/update/delete on an employee
or lookup (via the UI *or* the REST API that an IGA connector calls), a settings
change, a data reset. The UI's Activity section reads, filters, and exports
these rows.

Rows are written by :func:`app.services.audit.record_event` and are never
updated — treat the table as immutable. The free-form structured context for
each event lives in ``detail_json`` (a JSON string); it is what the UI's
"Export JSON" feature surfaces verbatim.

There is intentionally no foreign key on ``actor_id``: deleting an admin user
must never delete or break the history of what they did.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    """Return a timezone-aware UTC now. Used as a default factory."""
    return datetime.now(UTC)


class AuditEvent(Base):
    """One recorded audit / activity event."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)

    # When the event happened. Indexed because the table is almost always
    # sorted and filtered by time.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )

    # Coarse grouping for filtering: auth, oauth, oidc, employee, lookup,
    # api_key, oauth_client, auth_provider, branding, admin_user, system.
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Specific dotted type, e.g. "oauth.token.issued", "oidc.sso.success",
    # "employee.created", "auth.login.failure".
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # success | failure | error
    outcome: Mapped[str] = mapped_column(
        String(20), nullable=False, default="success", index=True
    )

    # Who performed/triggered it.
    # actor_type: user | oauth_client | api_key | idp | system | anonymous
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False, default="system")
    # Display label: username, client_id, key prefix, IdP subject, etc.
    actor_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # App-user id when the actor is a logged-in admin (plain int, no FK — see
    # module docstring).
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # What was acted upon (e.g. employee #42, country "US").
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Human-readable one-line summary shown in the table.
    message: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    # Request context (nullable — not every event originates from an HTTP request).
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Structured detail as a JSON string. This is the "raw JSON" the UI exports.
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def detail(self) -> dict[str, Any]:
        """Parse ``detail_json`` into a dict (empty dict if absent/invalid)."""
        if not self.detail_json:
            return {}
        try:
            parsed = json.loads(self.detail_json)
        except (ValueError, TypeError):
            return {"_unparseable": self.detail_json}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def to_dict(self) -> dict[str, Any]:
        """Full event as a plain dict — used for row detail and exports."""
        return {
            "id": self.id,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "category": self.category,
            "event_type": self.event_type,
            "outcome": self.outcome,
            "actor_type": self.actor_type,
            "actor_label": self.actor_label,
            "actor_id": self.actor_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_label": self.target_label,
            "message": self.message,
            "ip_address": self.ip_address,
            "request_id": self.request_id,
            "detail": self.detail,
        }

    def __repr__(self) -> str:
        return (
            f"<AuditEvent {self.event_type} {self.outcome} "
            f"at {self.occurred_at:%Y-%m-%d %H:%M:%S}>"
        )
