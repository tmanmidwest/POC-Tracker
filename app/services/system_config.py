"""System configuration backed by a single DB row (see app.models.app_config).

The audit retention window is read on every prune and on the Activity page, so
the resolved value is cached module-side and refreshed via ``invalidate()``
after a write — the same pattern as app.services.branding.

The initial value, when the row is first created, comes from the
POCT_AUDIT_RETENTION_DAYS env var (``settings.audit_retention_days``), so a
build can still set a starting default; the UI value then persists and overrides
it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings

log = logging.getLogger(__name__)

_cache: dict[str, Any] | None = None


def invalidate() -> None:
    """Drop the cached config so the next read reloads from the DB."""
    global _cache
    _cache = None


def _default_retention_days() -> int:
    """The starting value used when the config row is first created."""
    return get_settings().audit_retention_days


def get_config(db: Session) -> Any:
    """Return the singleton AppConfig row, creating it from env defaults if absent."""
    from app.models.app_config import APP_CONFIG_ID, AppConfig

    row = db.get(AppConfig, APP_CONFIG_ID)
    if row is None:
        row = AppConfig(
            id=APP_CONFIG_ID, audit_retention_days=_default_retention_days()
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _load() -> dict[str, Any]:
    """Read the singleton config row, falling back to env defaults if absent."""
    from app.db import get_session_factory
    from app.models.app_config import APP_CONFIG_ID, AppConfig

    retention = _default_retention_days()
    tasks_enabled = True
    db = get_session_factory()()
    try:
        row = db.get(AppConfig, APP_CONFIG_ID)
        if row is not None:
            retention = row.audit_retention_days
            tasks_enabled = row.tasks_enabled
    except Exception:
        # Config is non-critical — never let a DB hiccup break a page or prune.
        pass
    finally:
        db.close()
    return {"audit_retention_days": retention, "tasks_enabled": tasks_enabled}


def current_retention_days() -> int:
    """Return the cached audit retention window in days (0 = keep forever)."""
    global _cache
    if _cache is None:
        _cache = _load()
    return int(_cache["audit_retention_days"])


def tasks_enabled() -> bool:
    """Return whether the Task Manager module is enabled (cached)."""
    global _cache
    if _cache is None:
        _cache = _load()
    return bool(_cache["tasks_enabled"])


def set_retention_days(db: Session, days: int) -> None:
    """Persist a new audit retention window and refresh the cache."""
    row = get_config(db)
    row.audit_retention_days = days
    db.commit()
    invalidate()


def set_tasks_enabled(db: Session, enabled: bool) -> None:
    """Persist the Task Manager module toggle and refresh the cache."""
    row = get_config(db)
    row.tasks_enabled = enabled
    db.commit()
    invalidate()
