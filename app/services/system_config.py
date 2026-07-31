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
import time
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings

log = logging.getLogger(__name__)

_cache: dict[str, Any] | None = None
_cache_at: float = 0.0
# When more than one instance runs, a write in one instance can't invalidate()
# the others' caches, so bound cross-instance staleness with a short TTL.
_CACHE_TTL_SECONDS = 30


def invalidate() -> None:
    """Drop the cached config so the next read reloads from the DB."""
    global _cache
    _cache = None


def _get() -> dict[str, Any]:
    """Return the cached config, (re)loading on first use or after the TTL."""
    global _cache, _cache_at
    if _cache is None or time.monotonic() - _cache_at > _CACHE_TTL_SECONDS:
        _cache = _load()
        _cache_at = time.monotonic()
    return _cache


def _default_retention_days() -> int:
    """The starting value used when the config row is first created."""
    return get_settings().audit_retention_days


def _default_external_ttl_days() -> int:
    """The starting external-user lifetime used when the config row is created."""
    return get_settings().external_user_ttl_days


def _default_brandfetch_client_id() -> str | None:
    """The Brandfetch client ID seed from env, used when the DB value is unset."""
    return get_settings().brandfetch_client_id


def get_config(db: Session) -> Any:
    """Return the singleton AppConfig row, creating it from env defaults if absent."""
    from app.models.app_config import APP_CONFIG_ID, AppConfig

    row = db.get(AppConfig, APP_CONFIG_ID)
    if row is None:
        row = AppConfig(
            id=APP_CONFIG_ID,
            audit_retention_days=_default_retention_days(),
            external_user_ttl_days=_default_external_ttl_days(),
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
    external_ttl = _default_external_ttl_days()
    region_enforcement = False
    rbac_dynamic = False
    brandfetch_client_id = _default_brandfetch_client_id()
    db = get_session_factory()()
    try:
        row = db.get(AppConfig, APP_CONFIG_ID)
        if row is not None:
            retention = row.audit_retention_days
            tasks_enabled = row.tasks_enabled
            external_ttl = row.external_user_ttl_days
            region_enforcement = row.region_enforcement_enabled
            rbac_dynamic = row.rbac_dynamic_enabled
            # A non-empty DB value wins; NULL/empty falls back to the env seed so
            # existing rows (added before this column) still honor the env var.
            if (row.brandfetch_client_id or "").strip():
                brandfetch_client_id = row.brandfetch_client_id
    except Exception:
        # Config is non-critical — never let a DB hiccup break a page or prune.
        pass
    finally:
        db.close()
    return {
        "audit_retention_days": retention,
        "tasks_enabled": tasks_enabled,
        "external_user_ttl_days": external_ttl,
        "region_enforcement_enabled": region_enforcement,
        "rbac_dynamic_enabled": rbac_dynamic,
        "brandfetch_client_id": brandfetch_client_id,
    }


def current_retention_days() -> int:
    """Return the cached audit retention window in days (0 = keep forever)."""
    return int(_get()["audit_retention_days"])


def tasks_enabled() -> bool:
    """Return whether the Task Manager module is enabled (cached)."""
    return bool(_get()["tasks_enabled"])


def current_external_user_ttl_days() -> int:
    """Return the cached default external-user lifetime in days (0 = never)."""
    return int(_get()["external_user_ttl_days"])


def region_enforcement_enabled() -> bool:
    """Return whether region-based access control is enforced (cached).

    When False (default), region data is stored but internal users still see all
    projects — the safe pre-rollout state. Access/scope code calls this to decide
    whether to apply hard region boundaries.
    """
    return bool(_get()["region_enforcement_enabled"])


def set_retention_days(db: Session, days: int) -> None:
    """Persist a new audit retention window and refresh the cache."""
    row = get_config(db)
    row.audit_retention_days = days
    db.commit()
    invalidate()


def set_external_user_ttl_days(db: Session, days: int) -> None:
    """Persist the default external-user lifetime and refresh the cache."""
    row = get_config(db)
    row.external_user_ttl_days = max(0, days)
    db.commit()
    invalidate()


def set_tasks_enabled(db: Session, enabled: bool) -> None:
    """Persist the Task Manager module toggle and refresh the cache."""
    row = get_config(db)
    row.tasks_enabled = enabled
    db.commit()
    invalidate()


def set_region_enforcement_enabled(db: Session, enabled: bool) -> None:
    """Persist the region-enforcement master switch and refresh the cache."""
    row = get_config(db)
    row.region_enforcement_enabled = enabled
    db.commit()
    invalidate()


def rbac_dynamic_enabled() -> bool:
    """Return whether the dynamic-RBAC role builder is enforced (cached).

    When False (default), ``AppUser.can()`` resolves capabilities via the legacy
    gates so behavior matches the four hardcoded roles. When True, ``can()`` reads
    the union of the user's assigned roles' capabilities.
    """
    return bool(_get()["rbac_dynamic_enabled"])


def set_rbac_dynamic_enabled(db: Session, enabled: bool) -> None:
    """Persist the dynamic-RBAC master switch and refresh the cache."""
    row = get_config(db)
    row.rbac_dynamic_enabled = enabled
    db.commit()
    invalidate()


def brandfetch_client_id() -> str | None:
    """Return the resolved Brandfetch Logo API client ID, or None (cached).

    Resolution order: the UI-set DB value, else the POCT_BRANDFETCH_CLIENT_ID env
    seed, else None (in which case logo fetching uses the keyless favicon source).
    """
    value = _get()["brandfetch_client_id"]
    value = (value or "").strip()
    return value or None


def set_brandfetch_client_id(db: Session, value: str | None) -> None:
    """Persist the Brandfetch client ID (blank clears it) and refresh the cache."""
    row = get_config(db)
    row.brandfetch_client_id = (value or "").strip() or None
    db.commit()
    invalidate()
