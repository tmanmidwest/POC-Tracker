"""The capability registry — the code-defined catalog of RBAC permissions.

Capabilities are **code-defined, not admin-defined**: a capability only means
something if a call site enforces it, so admins compose *roles* out of these
capabilities (via the role builder) but cannot invent new ones. This module is
the single source of truth for the capability set; Phase 2 reconciles it into a
``capabilities`` table at startup (for FK integrity + UI labels/grouping) and
Phase 3 adds ``AppUser.can("capability")`` on top. See
``docs/rbac-role-builder-plan.md``.

This module has **no database or enforcement behavior**. It only declares the
catalog and provides read helpers over it — nothing here imports SQLAlchemy or
touches a session, so it stays trivially importable and testable.

Each ``Capability`` key is a ``resource.action`` slug (e.g. ``project.edit``).
The ``area`` groups related capabilities for the admin matrix UI; ``AREAS`` fixes
the display order of those groups, and declaration order within ``CAPABILITIES``
fixes the order inside each group.

Baseline rights are intentionally **not** in this registry: ``access_ui`` (any
active user), ``edit_own_profile`` (any internal user), and submitting feedback
stay identity checks that every role has implicitly. Likewise the REST/MCP API
surfaces authenticate as principals and are out of scope for capability gating
(same boundary as region RBAC) — no capability here governs them.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Area groupings (display order for the role-builder matrix) --------------

AREA_PROJECTS = "Projects"
AREA_WORK = "Use Cases, Notes & Tasks"
AREA_CUSTOMERS = "Customers"
AREA_SHARING = "Grants & Sharing"
AREA_REPORTS = "Reports & Exports"
AREA_ADMIN = "Administration"
AREA_REGION = "Region Administration"
AREA_ROLES = "Roles & Access"

#: Areas in the order they should be shown in the admin UI.
AREAS: tuple[str, ...] = (
    AREA_PROJECTS,
    AREA_WORK,
    AREA_CUSTOMERS,
    AREA_SHARING,
    AREA_REPORTS,
    AREA_ADMIN,
    AREA_REGION,
    AREA_ROLES,
)


@dataclass(frozen=True)
class Capability:
    """One grantable permission.

    ``key``   — stable ``resource.action`` slug; the value stored in
                ``role_capabilities`` and checked by ``user.can(key)``.
    ``area``  — one of ``AREAS``; groups the capability in the admin matrix.
    ``label`` — short human label for the matrix row.
    ``description`` — one-line explanation of what granting it allows.
    """

    key: str
    area: str
    label: str
    description: str


# --- The catalog -------------------------------------------------------------
#
# Derived from the ~70 real enforcement sites (see the Phase 1 catalog in
# docs/rbac-role-builder-plan.md). Keep grouped by area, in display order; the
# tuple order is canonical (Phase 2 seeds sort_order from it).

CAPABILITIES: tuple[Capability, ...] = (
    # -- Projects -------------------------------------------------------------
    Capability(
        "project.view",
        AREA_PROJECTS,
        "View projects",
        "See POC projects and their detail pages (still subject to region scope "
        "and, for external viewers, share grants).",
    ),
    Capability(
        "project.create",
        AREA_PROJECTS,
        "Create projects",
        "Create new POC projects, including via the New POC wizard.",
    ),
    Capability(
        "project.edit",
        AREA_PROJECTS,
        "Edit projects",
        "Modify a project and its sub-resources (status, milestones, "
        "screenshots, AI fields). The per-project region check still applies.",
    ),
    Capability(
        "project.delete",
        AREA_PROJECTS,
        "Delete projects",
        "Permanently delete a POC project.",
    ),
    Capability(
        "project.assign_region",
        AREA_PROJECTS,
        "Assign project region",
        "Place or move a project into a region (the region axis limits which "
        "regions are reachable).",
    ),
    # -- Use Cases, Notes & Tasks --------------------------------------------
    Capability(
        "usecase.edit",
        AREA_WORK,
        "Edit use cases",
        "Add, edit, reorder, and set the status of a project's use cases.",
    ),
    Capability(
        "note.edit",
        AREA_WORK,
        "Edit notes",
        "Create, edit, and delete journal notes on a project.",
    ),
    Capability(
        "note.view_internal",
        AREA_WORK,
        "View internal-only notes",
        "See journal notes marked internal-only (hidden from external viewers).",
    ),
    Capability(
        "note.mark_internal",
        AREA_WORK,
        "Mark notes internal-only",
        "Flag a note as internal-only so external viewers never see it.",
    ),
    Capability(
        "task.view_own",
        AREA_WORK,
        "View own tasks",
        "See and work the tasks assigned to or owned by this user.",
    ),
    Capability(
        "task.view_all",
        AREA_WORK,
        "View all tasks",
        "See every user's tasks, not just your own (admin-level today).",
    ),
    Capability(
        "task.edit",
        AREA_WORK,
        "Edit tasks",
        "Create, update, and delete tasks.",
    ),
    Capability(
        "task.mark_internal",
        AREA_WORK,
        "Mark tasks internal-only",
        "Flag a task as internal-only so external viewers never see it.",
    ),
    # -- Customers ------------------------------------------------------------
    Capability(
        "customer.view",
        AREA_CUSTOMERS,
        "View customers",
        "See the customer list and customer detail pages.",
    ),
    Capability(
        "customer.create",
        AREA_CUSTOMERS,
        "Create customers",
        "Add new customer records.",
    ),
    Capability(
        "customer.edit",
        AREA_CUSTOMERS,
        "Edit customers",
        "Edit existing customer records (including logos).",
    ),
    Capability(
        "customer.delete",
        AREA_CUSTOMERS,
        "Delete customers",
        "Delete customer records.",
    ),
    # -- Grants & Sharing -----------------------------------------------------
    Capability(
        "grant.manage",
        AREA_SHARING,
        "Manage project grants",
        "Grant and revoke external viewers' access to a project (the per-project "
        "own-SE / in-region rules still apply).",
    ),
    Capability(
        "sharelink.manage",
        AREA_SHARING,
        "Manage share links",
        "Create and manage public portal share links for a project.",
    ),
    Capability(
        "external.extend_expiry",
        AREA_SHARING,
        "Extend external expiry",
        "Extend the expiry date of an external viewer's access.",
    ),
    # -- Reports & Exports ----------------------------------------------------
    Capability(
        "report.generate",
        AREA_REPORTS,
        "Generate reports",
        "Produce project reports and exports (PDF / PPTX / DOCX).",
    ),
    Capability(
        "report.choose_audience",
        AREA_REPORTS,
        "Choose report audience",
        "Select a report's audience, including internal reports that may contain "
        "internal-only content.",
    ),
    Capability(
        "report.export_internal",
        AREA_REPORTS,
        "Export internal reports",
        "Generate the internal report variant that includes internal-only notes.",
    ),
    # -- Administration -------------------------------------------------------
    Capability(
        "lookups.manage",
        AREA_ADMIN,
        "Manage lookups",
        "Create, edit, reorder, and delete lookup values (statuses, types, " "regions list, etc.).",
    ),
    Capability(
        "library.manage",
        AREA_ADMIN,
        "Manage use-case library",
        "Manage the shared use-case library sets and entries.",
    ),
    Capability(
        "user.view",
        AREA_ADMIN,
        "View users",
        "See the admin users list and user detail.",
    ),
    Capability(
        "user.create",
        AREA_ADMIN,
        "Create users",
        "Create new internal user accounts.",
    ),
    Capability(
        "user.edit",
        AREA_ADMIN,
        "Edit users",
        "Edit user accounts (display name, activation, etc.).",
    ),
    Capability(
        "user.delete",
        AREA_ADMIN,
        "Delete users",
        "Delete user accounts (the seeded admin and your own account stay " "protected).",
    ),
    Capability(
        "user.change_role",
        AREA_ADMIN,
        "Change user roles",
        "Assign roles to users (guards still block self-change, demoting the last "
        "admin, and un-adminning the seeded admin).",
    ),
    Capability(
        "user.set_password",
        AREA_ADMIN,
        "Set user passwords",
        "Set or reset another user's local password.",
    ),
    Capability(
        "user.unlock",
        AREA_ADMIN,
        "Unlock users",
        "Clear a lockout after too many failed sign-ins.",
    ),
    Capability(
        "external_user.manage",
        AREA_ADMIN,
        "Manage external users",
        "Invite, resend invites to, extend, and delete external viewer accounts.",
    ),
    Capability(
        "authprovider.manage",
        AREA_ADMIN,
        "Manage auth providers",
        "Configure OIDC / SSO identity providers.",
    ),
    Capability(
        "apikey.manage",
        AREA_ADMIN,
        "Manage API keys",
        "Create and revoke REST API keys.",
    ),
    Capability(
        "oauthclient.manage",
        AREA_ADMIN,
        "Manage OAuth clients",
        "Create and manage OAuth client credentials.",
    ),
    Capability(
        "mcptoken.manage",
        AREA_ADMIN,
        "Manage MCP tokens",
        "Create and manage MCP gateway tokens.",
    ),
    Capability(
        "settings.manage",
        AREA_ADMIN,
        "Manage system settings",
        "Change system settings, branding, SMTP, and demo-data controls.",
    ),
    Capability(
        "feedback.manage",
        AREA_ADMIN,
        "Manage feedback board",
        "View and act on the feedback board (submitting feedback stays open to " "all users).",
    ),
    Capability(
        "audit.view",
        AREA_ADMIN,
        "View audit log",
        "View the activity / audit log.",
    ),
    # -- Region Administration (region control plane) -------------------------
    Capability(
        "region.enforce_toggle",
        AREA_REGION,
        "Toggle region enforcement",
        "Flip the region-enforcement master switch on or off.",
    ),
    Capability(
        "region.backfill",
        AREA_REGION,
        "Backfill project regions",
        "Run the backfill that derives project regions from their SEs.",
    ),
    Capability(
        "region.assign_users",
        AREA_REGION,
        "Assign user regions",
        "Assign region memberships to users (bulk grid and CSV import).",
    ),
    # -- Roles & Access (the role builder itself) -----------------------------
    Capability(
        "role.manage",
        AREA_ROLES,
        "Manage roles",
        "Create, edit, and delete roles and assign them to users. "
        "Privilege-escalation sensitive — grant with care.",
    ),
)


# --- Derived lookups ---------------------------------------------------------

#: Fast membership set of every valid capability key.
CAPABILITY_KEYS: frozenset[str] = frozenset(c.key for c in CAPABILITIES)

_BY_KEY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}


def get_capability(key: str) -> Capability | None:
    """Return the ``Capability`` for ``key``, or ``None`` if it isn't registered."""
    return _BY_KEY.get(key)


def is_valid_capability(key: str) -> bool:
    """Whether ``key`` names a real, registered capability.

    Use before persisting a capability from any external input (a role-builder
    form post, a seed, an import) so an unknown or misspelled key can't land in
    ``role_capabilities`` where nothing would ever check it.
    """
    return key in CAPABILITY_KEYS


def capabilities_by_area() -> dict[str, list[Capability]]:
    """Capabilities grouped by area, both areas and members in display order.

    Returns an insertion-ordered dict keyed by the entries of ``AREAS``; each
    value is the capabilities in that area in declaration order. Empty areas are
    omitted. Intended to drive the grouped capability matrix in the admin UI.
    """
    grouped: dict[str, list[Capability]] = {area: [] for area in AREAS}
    for cap in CAPABILITIES:
        grouped[cap.area].append(cap)
    return {area: caps for area, caps in grouped.items() if caps}


# --- Import-time integrity checks --------------------------------------------
# Cheap invariants that turn a typo into an immediate, obvious failure rather
# than a capability that silently never matches.

if len(CAPABILITY_KEYS) != len(CAPABILITIES):  # pragma: no cover - guard
    raise RuntimeError("Duplicate capability key in CAPABILITIES registry")

_unknown_areas = {c.area for c in CAPABILITIES} - set(AREAS)
if _unknown_areas:  # pragma: no cover - guard
    raise RuntimeError(f"Capabilities reference unknown area(s): {_unknown_areas}")

for _cap in CAPABILITIES:  # pragma: no cover - guard
    if _cap.key.count(".") != 1 or _cap.key != _cap.key.lower():
        raise RuntimeError(
            f"Capability key {_cap.key!r} must be a lowercase 'resource.action' slug"
        )
