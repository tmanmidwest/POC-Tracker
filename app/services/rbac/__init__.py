"""Dynamic RBAC (role builder) — capability registry and, later, enforcement.

Phase 1 of the role-builder plan (see ``docs/rbac-role-builder-plan.md``): this
package currently holds only the **capability registry** (``capabilities``) — the
single, code-defined source of truth for the set of permissions a role can grant.
No database tables (Phase 2) or ``user.can()`` enforcement (Phase 3) live here yet.

The public capability surface is re-exported here for convenience so callers can
``from app.services.rbac import CAPABILITIES, is_valid_capability``.
"""

from __future__ import annotations

from app.services.rbac.capabilities import (
    AREAS,
    CAPABILITIES,
    CAPABILITY_KEYS,
    Capability,
    capabilities_by_area,
    get_capability,
    is_valid_capability,
)

__all__ = [
    "AREAS",
    "CAPABILITIES",
    "CAPABILITY_KEYS",
    "Capability",
    "capabilities_by_area",
    "get_capability",
    "is_valid_capability",
]
