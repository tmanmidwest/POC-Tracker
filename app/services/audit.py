"""Recording audit / activity events.

:func:`record_event` writes one row to ``audit_events``. It is deliberately
defensive in two ways:

* **It never raises into the caller.** Recording an event must not be able to
  break a login, a token grant, or a provisioning call. Any failure is swallowed
  and logged to stdout instead.
* **It writes in its own short-lived session and commits immediately.** This
  decouples the audit write from the request's transaction, so an event is
  persisted even when the surrounding request later rolls back — which is exactly
  what happens on a failed login or a rejected token request.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request

from app.db import get_session_factory
from app.models.audit_event import AuditEvent

log = logging.getLogger(__name__)


def prune_old_events(retention_days: int | None = None) -> int:
    """Delete audit events older than the retention window. Returns the number
    deleted. Never raises.

    ``retention_days`` defaults to ``settings.audit_retention_days``; a value of
    0 or less disables pruning (events are kept forever).
    """
    try:
        if retention_days is None:
            from app.services import system_config

            retention_days = system_config.current_retention_days()
        if retention_days <= 0:
            return 0

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        session = get_session_factory()()
        try:
            deleted = (
                session.query(AuditEvent)
                .filter(AuditEvent.occurred_at < cutoff)
                .delete(synchronize_session=False)
            )
            session.commit()
            if deleted:
                log.info(
                    "audit_pruned",
                    extra={"deleted": deleted, "retention_days": retention_days},
                )
            return int(deleted)
        finally:
            session.close()
    except Exception:
        log.exception("audit_prune_failed")
        return 0


def principal_actor(principal: Any) -> dict[str, str | None]:
    """Map a REST API ``Principal`` to ``actor_type``/``actor_label`` kwargs.

    Returns a dict ready to spread into :func:`record_event`. Distinguishes an
    API-key caller from an OAuth-client caller so the Activity view can filter
    on it. Accepts ``None`` defensively.
    """
    kind = getattr(principal, "kind", None)
    label = getattr(principal, "identifier", None)
    actor_type = "api_key" if str(kind) == "api_key" else "oauth_client"
    return {"actor_type": actor_type, "actor_label": label}


def _request_context(request: Request | None) -> dict[str, str | None]:
    """Pull client IP, request id, and user-agent from a request (if any)."""
    if request is None:
        return {"ip_address": None, "request_id": None, "user_agent": None}

    # Honor X-Forwarded-For (set by the ALB / proxy), then the socket peer.
    client_ip: str | None = None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    elif request.client is not None:
        client_ip = request.client.host

    return {
        "ip_address": client_ip,
        "request_id": request.headers.get("x-request-id"),
        "user_agent": request.headers.get("user-agent"),
    }


def record_event(
    *,
    category: str,
    event_type: str,
    outcome: str = "success",
    actor_type: str = "system",
    actor_label: str | None = None,
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: Any = None,
    target_label: str | None = None,
    message: str = "",
    detail: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Persist a single audit event. Never raises.

    ``category``/``event_type``/``outcome`` classify the event; the ``actor_*``
    fields say who did it; the ``target_*`` fields say what it acted on;
    ``detail`` is arbitrary structured context that is stored as JSON and shown
    verbatim in the UI's row-detail and exports.
    """
    try:
        ctx = _request_context(request)

        # Fold the user-agent into the detail blob so it's exportable without
        # needing its own column.
        full_detail: dict[str, Any] = dict(detail or {})
        if ctx["user_agent"] and "user_agent" not in full_detail:
            full_detail["user_agent"] = ctx["user_agent"]

        event = AuditEvent(
            occurred_at=datetime.now(UTC),
            category=category,
            event_type=event_type,
            outcome=outcome,
            actor_type=actor_type,
            actor_label=actor_label,
            actor_id=actor_id,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            target_label=target_label,
            message=message or event_type,
            ip_address=ctx["ip_address"],
            request_id=ctx["request_id"],
            detail_json=json.dumps(full_detail, default=str) if full_detail else None,
        )

        session = get_session_factory()()
        try:
            session.add(event)
            session.commit()
        finally:
            session.close()
    except Exception:
        log.exception("audit_record_failed", extra={"event_type": event_type})
