"""Default system roles and the legacy→capability mapping.

This module is the single source of truth for two tightly-coupled things:

1. **``LEGACY_TIER``** — which of the three legacy authorization gates each
   capability corresponds to: ``open`` (any logged-in user), ``internal`` (any
   non-external user, i.e. the old ``require_internal_ui``), or ``admin`` (the
   old ``require_admin_ui`` / ``is_admin`` checks). Phase 3's ``legacy_can``
   uses this so that, with the dynamic-RBAC switch OFF, ``user.can()`` reproduces
   today's behavior exactly.

2. **The seeded system roles** — the four defaults (admin/manager/standard/
   external) whose capability sets are *derived from the same tiers*, so the
   seed and the legacy path can never drift: a manager/SE gets every open+internal
   capability; an external viewer gets the open ones; admin is a superuser.

Deriving both from one table is what guarantees "flipping the switch is a no-op"
(see docs/rbac-role-builder-plan.md §3/§4).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.app_user import (
    ROLE_ADMIN,
    ROLE_EXTERNAL,
    ROLE_MANAGER,
    ROLE_STANDARD,
)
from app.services.rbac.capabilities import CAPABILITY_KEYS

# Legacy authorization tier per capability. Every capability in the registry must
# appear here exactly once (asserted below).
#   open     -> any authenticated user (incl. external viewers)
#   internal -> non-external users (old require_internal_ui)
#   admin    -> admins only (old require_admin_ui / is_admin)
LEGACY_TIER: dict[str, str] = {
    # Projects
    "project.view": "open",
    "project.create": "internal",
    "project.edit": "internal",
    "project.delete": "internal",
    "project.assign_region": "internal",
    # Use cases / notes / tasks
    "usecase.edit": "internal",
    "note.edit": "internal",
    "note.view_internal": "internal",
    "note.mark_internal": "internal",
    "task.view_own": "internal",
    "task.view_all": "admin",  # legacy can_view_all_tasks == is_admin
    "task.edit": "internal",
    "task.mark_internal": "internal",
    # Customers
    "customer.view": "internal",
    "customer.create": "internal",
    "customer.edit": "internal",
    "customer.delete": "internal",
    # Grants / sharing
    "grant.manage": "internal",
    "sharelink.manage": "internal",
    "external.extend_expiry": "internal",
    # Reports (per-project exports)
    "report.generate": "open",
    "report.choose_audience": "internal",
    "report.export_internal": "internal",
    # Report builder (saved/shared/scheduled). All four are for any internal user
    # (managers + SEs) — scheduling recurring email included. Region scope still
    # bounds which rows a scheduled send can reach, and each send is audited. An
    # admin can narrow this per role via the role builder once dynamic RBAC is on.
    "report.view": "internal",
    "report.create": "internal",
    "report.publish": "internal",
    "report.schedule": "internal",
    # Administration
    "lookups.manage": "admin",
    "library.manage": "admin",
    "user.view": "admin",
    "user.create": "admin",
    "user.edit": "admin",
    "user.delete": "admin",
    "user.change_role": "admin",
    "user.set_password": "admin",
    "user.unlock": "admin",
    "external_user.manage": "admin",
    "authprovider.manage": "admin",
    "apikey.manage": "admin",
    "oauthclient.manage": "admin",
    "mcptoken.manage": "admin",
    "settings.manage": "admin",
    "feedback.manage": "admin",
    "audit.view": "internal",  # legacy audit router is internal_only, not admin
    # Region administration
    "region.enforce_toggle": "admin",
    "region.backfill": "admin",
    "region.assign_users": "admin",
    # Roles & access
    "role.manage": "admin",
}

# Derived capability sets (single source: LEGACY_TIER).
OPEN_CAPABILITIES: frozenset[str] = frozenset(
    k for k, tier in LEGACY_TIER.items() if tier == "open"
)
INTERNAL_CAPABILITIES: frozenset[str] = frozenset(
    k for k, tier in LEGACY_TIER.items() if tier in ("open", "internal")
)


@dataclass(frozen=True)
class SystemRoleDef:
    """A seeded default role and the capabilities it starts with."""

    key: str
    name: str
    description: str
    is_superuser: bool
    is_external: bool
    sort_order: int
    default_capabilities: frozenset[str]


# The four seeded roles. Admin is a superuser (implicitly holds everything, so no
# rows). Manager and SE share the internal set (they differ only on the region
# axis, not on capabilities). External gets the read-only open set.
SYSTEM_ROLES: tuple[SystemRoleDef, ...] = (
    SystemRoleDef(
        key=ROLE_ADMIN,
        name="Admin",
        description="Full access to everything (superuser). Protected.",
        is_superuser=True,
        is_external=False,
        sort_order=10,
        default_capabilities=frozenset(),
    ),
    SystemRoleDef(
        key=ROLE_MANAGER,
        name="Manager",
        description=(
            "Internal user who can view and edit POCs across their assigned "
            "regions. Same capabilities as an SE; region breadth differs."
        ),
        is_superuser=False,
        is_external=False,
        sort_order=20,
        default_capabilities=INTERNAL_CAPABILITIES,
    ),
    SystemRoleDef(
        key=ROLE_STANDARD,
        name="SE",
        description=(
            "Sales engineer. Creates and edits POCs; hard-scoped to their own "
            "region when region enforcement is on."
        ),
        is_superuser=False,
        is_external=False,
        sort_order=30,
        default_capabilities=INTERNAL_CAPABILITIES,
    ),
    SystemRoleDef(
        key=ROLE_EXTERNAL,
        name="External Viewer",
        description="Read-only viewer of projects explicitly shared with them.",
        is_superuser=False,
        is_external=True,
        sort_order=40,
        default_capabilities=OPEN_CAPABILITIES,
    ),
)

SYSTEM_ROLE_KEYS: frozenset[str] = frozenset(r.key for r in SYSTEM_ROLES)


def legacy_can(user: object, capability: str) -> bool:
    """Reproduce the pre-role-builder authorization decision for ``capability``.

    Used by ``AppUser.can()`` when the dynamic-RBAC master switch is OFF, so the
    app behaves exactly as it did before the role builder. ``user`` is duck-typed
    (needs ``is_admin`` / ``is_internal``) to avoid importing the model here.

    - admins pass everything (old god-mode);
    - ``open`` capabilities pass for any user;
    - ``internal`` capabilities pass for non-external users;
    - ``admin`` capabilities pass for admins only (already covered above).
    """
    if getattr(user, "is_admin", False):
        return True
    tier = LEGACY_TIER.get(capability)
    if tier == "open":
        return True
    if tier == "internal":
        return bool(getattr(user, "is_internal", False))
    return False


# Import-time integrity: every registered capability must have a legacy tier, and
# no tier may reference an unknown capability. Keeps the parity guarantee honest.
_missing = CAPABILITY_KEYS - set(LEGACY_TIER)
if _missing:  # pragma: no cover - guard
    raise RuntimeError(f"Capabilities missing a LEGACY_TIER entry: {sorted(_missing)}")
_extra = set(LEGACY_TIER) - CAPABILITY_KEYS
if _extra:  # pragma: no cover - guard
    raise RuntimeError(f"LEGACY_TIER references unknown capabilities: {sorted(_extra)}")
_bad_tiers = set(LEGACY_TIER.values()) - {"open", "internal", "admin"}
if _bad_tiers:  # pragma: no cover - guard
    raise RuntimeError(f"Unknown legacy tier(s): {sorted(_bad_tiers)}")
