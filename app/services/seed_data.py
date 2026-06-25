"""Seed data loader for POC Tracker.

``seed_database()`` is idempotent: it can be safely called on every startup. It
only inserts rows that are missing, never updates or deletes existing data.

Individual reset functions are exposed for the admin reset UI.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    AppUser,
    Contact,
    ContactRole,
    Customer,
    FeatureType,
    Project,
    ProjectStatus,
    ProjectUseCase,
    Screenshot,
    UseCaseLibrary,
    UseCaseStatus,
)
from app.models.project_use_case import SOURCE_LIBRARY
from app.services.passwords import hash_password

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default lookup values
# ---------------------------------------------------------------------------

# (name, is_system)
DEFAULT_CONTACT_ROLES: list[tuple[str, bool]] = [
    ("Champion", True),
    ("Economic Buyer", False),
    ("Sourcing", True),
    ("Technical Stakeholder", True),
    ("Business Stakeholder", True),
    ("Executive Sponsor", False),
    ("End User", False),
]

# (name, sort_order, is_terminal, is_system)
DEFAULT_PROJECT_STATUSES: list[tuple[str, int, bool, bool]] = [
    ("Pending Scheduling", 10, False, True),
    ("Pending Use Cases", 20, False, True),
    ("Scheduled", 30, False, False),
    ("In Progress", 40, False, True),
    ("On Hold", 50, False, False),
    ("Completed - Won", 90, True, False),
    ("Completed - Lost", 100, True, False),
]

# (name, description, is_system)
DEFAULT_FEATURE_TYPES: list[tuple[str, str | None, bool]] = [
    ("JML", "Joiner / Mover / Leaver lifecycle", True),
    ("ISPM", "Identity Security Posture Management", True),
    ("Certifications", "Access certifications / reviews", True),
    ("NHI", "Non-Human Identities", True),
    ("AI", "AI-driven capabilities", True),
    ("PAM", "Privileged Access Management", False),
    ("IGA", "Identity Governance & Administration", False),
]

# (name, sort_order, is_complete, is_system)
DEFAULT_USE_CASE_STATUSES: list[tuple[str, int, bool, bool]] = [
    ("Pending Testing", 10, False, True),
    ("Testing in Progress", 20, False, True),
    ("Completed", 30, True, True),
    ("Blocked", 40, False, False),
    ("Not Applicable", 50, False, False),
]

# Master use-case library: (category, ref, name, description, success, feature_type_name)
DEFAULT_USE_CASE_LIBRARY: list[tuple[str, str, str, str, str, str | None]] = [
    (
        "Joiner",
        "1.1",
        "New hire account provisioning",
        "Provision a new employee's accounts across target systems from the HR source of truth.",
        "Accounts exist in all in-scope targets within SLA with correct entitlements.",
        "JML",
    ),
    (
        "Joiner",
        "1.2",
        "Birthright role assignment",
        "Assign birthright roles based on department and job title at hire.",
        "Birthright roles are granted automatically with no manual intervention.",
        "JML",
    ),
    (
        "Mover",
        "2.1",
        "Department transfer access change",
        "Adjust access when an employee moves departments.",
        "Old access is revoked and new access granted reflecting the new role.",
        "JML",
    ),
    (
        "Leaver",
        "3.1",
        "Termination deprovisioning",
        "Disable and deprovision accounts when an employee is terminated.",
        "All accounts are disabled within SLA and access is fully revoked.",
        "JML",
    ),
    (
        "Certifications",
        "4.1",
        "Manager access review campaign",
        "Run a manager-based access certification campaign.",
        "Reviewers can approve/revoke and revocations are fulfilled automatically.",
        "Certifications",
    ),
    (
        "Posture",
        "5.1",
        "Detect orphaned accounts",
        "Identify accounts with no valid owner across connected systems.",
        "Orphaned accounts are surfaced in a report with remediation options.",
        "ISPM",
    ),
    (
        "Non-Human Identities",
        "6.1",
        "Service account discovery",
        "Discover and inventory non-human / service accounts.",
        "Service accounts are catalogued with ownership and last-use data.",
        "NHI",
    ),
    (
        "AI",
        "7.1",
        "AI access recommendations",
        "Use AI to recommend appropriate access during a request.",
        "Recommendations are relevant and reduce request handling time.",
        "AI",
    ),
]


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def seed_database(db: Session, settings: Settings | None = None) -> None:
    """Idempotently seed all default data. Safe to call on every startup."""
    settings = settings or get_settings()

    seed_contact_roles(db)
    seed_project_statuses(db)
    seed_feature_types(db)
    seed_use_case_statuses(db)
    seed_use_case_library(db)
    seed_admin_user(db, settings)
    seed_sample_data(db)

    db.commit()

    from app.models import ApiKey, OAuthClient

    log.info(
        "seed_database_complete",
        extra={
            "contact_roles": db.scalar(select(func.count()).select_from(ContactRole)) or 0,
            "project_statuses": db.scalar(
                select(func.count()).select_from(ProjectStatus)
            )
            or 0,
            "feature_types": db.scalar(select(func.count()).select_from(FeatureType)) or 0,
            "use_case_statuses": db.scalar(
                select(func.count()).select_from(UseCaseStatus)
            )
            or 0,
            "use_case_library": db.scalar(
                select(func.count()).select_from(UseCaseLibrary)
            )
            or 0,
            "customers": db.scalar(select(func.count()).select_from(Customer)) or 0,
            "projects": db.scalar(select(func.count()).select_from(Project)) or 0,
            "app_users": db.scalar(select(func.count()).select_from(AppUser)) or 0,
            "api_keys": db.scalar(select(func.count()).select_from(ApiKey)) or 0,
            "oauth_clients": db.scalar(select(func.count()).select_from(OAuthClient)) or 0,
        },
    )


# ---------------------------------------------------------------------------
# Individual seeders
# ---------------------------------------------------------------------------


def seed_contact_roles(db: Session) -> int:
    existing = {row[0] for row in db.execute(select(ContactRole.name)).all()}
    inserted = 0
    for name, is_system in DEFAULT_CONTACT_ROLES:
        if name not in existing:
            db.add(ContactRole(name=name, is_active=True, is_system=is_system))
            inserted += 1
    if inserted:
        db.flush()
        log.info("seeded_contact_roles", extra={"inserted": inserted})
    return inserted


def seed_project_statuses(db: Session) -> int:
    existing = {row[0] for row in db.execute(select(ProjectStatus.name)).all()}
    inserted = 0
    for name, sort_order, is_terminal, is_system in DEFAULT_PROJECT_STATUSES:
        if name not in existing:
            db.add(
                ProjectStatus(
                    name=name,
                    sort_order=sort_order,
                    is_terminal=is_terminal,
                    is_active=True,
                    is_system=is_system,
                )
            )
            inserted += 1
    if inserted:
        db.flush()
        log.info("seeded_project_statuses", extra={"inserted": inserted})
    return inserted


def seed_feature_types(db: Session) -> int:
    existing = {row[0] for row in db.execute(select(FeatureType.name)).all()}
    inserted = 0
    for name, description, is_system in DEFAULT_FEATURE_TYPES:
        if name not in existing:
            db.add(
                FeatureType(
                    name=name,
                    description=description,
                    is_active=True,
                    is_system=is_system,
                )
            )
            inserted += 1
    if inserted:
        db.flush()
        log.info("seeded_feature_types", extra={"inserted": inserted})
    return inserted


def seed_use_case_statuses(db: Session) -> int:
    existing = {row[0] for row in db.execute(select(UseCaseStatus.name)).all()}
    inserted = 0
    for name, sort_order, is_complete, is_system in DEFAULT_USE_CASE_STATUSES:
        if name not in existing:
            db.add(
                UseCaseStatus(
                    name=name,
                    sort_order=sort_order,
                    is_complete_status=is_complete,
                    is_active=True,
                    is_system=is_system,
                )
            )
            inserted += 1
    if inserted:
        db.flush()
        log.info("seeded_use_case_statuses", extra={"inserted": inserted})
    return inserted


def seed_use_case_library(db: Session) -> int:
    """Insert any missing library entries (matched by category + name)."""
    existing = {
        (row[0], row[1])
        for row in db.execute(
            select(UseCaseLibrary.category, UseCaseLibrary.name)
        ).all()
    }
    feature_ids = {f.name: f.id for f in db.scalars(select(FeatureType)).all()}
    inserted = 0
    for category, ref, name, description, success, ft_name in DEFAULT_USE_CASE_LIBRARY:
        if (category, name) in existing:
            continue
        db.add(
            UseCaseLibrary(
                category=category,
                default_reference_number=ref,
                name=name,
                description=description,
                success_validation=success,
                feature_type_id=feature_ids.get(ft_name) if ft_name else None,
                is_active=True,
            )
        )
        inserted += 1
    if inserted:
        db.flush()
        log.info("seeded_use_case_library", extra={"inserted": inserted})
    return inserted


def seed_admin_user(db: Session, settings: Settings) -> bool:
    """Create the seeded admin user if it doesn't exist."""
    username = settings.initial_admin_username
    existing = db.scalar(select(AppUser).where(AppUser.username == username))
    if existing is not None:
        return False

    user = AppUser(
        username=username,
        password_hash=hash_password(settings.initial_admin_password),
        is_seeded=True,
        is_active=True,
        is_admin=True,
    )
    db.add(user)
    db.flush()
    log.info("seeded_admin_user", extra={"username": username})
    write_initial_credentials_file(settings)
    return True


def write_initial_credentials_file(settings: Settings) -> None:
    """Write the INITIAL_CREDENTIALS.txt file for operator reference."""
    settings.ensure_data_dir()
    content = (
        f"POC Tracker — Initial Credentials\n"
        f"{'=' * 50}\n"
        f"\n"
        f"Web UI: http://<your-host>:{settings.bind_port}\n"
        f"\n"
        f"Username: {settings.initial_admin_username}\n"
        f"Password: {settings.initial_admin_password}\n"
        f"\n"
        f"WARNING: This is a non-production POC application.\n"
        f"Change this password immediately via the UI for any non-trivial use.\n"
        f"Delete this file after initial setup.\n"
    )
    settings.initial_credentials_path.write_text(content)
    try:
        settings.initial_credentials_path.chmod(0o600)
    except OSError:
        pass


def seed_sample_data(db: Session) -> int:
    """Seed one sample customer + project + a couple use cases if empty.

    Returns the number of projects inserted (0 if customers already exist).
    """
    if db.scalar(select(Customer.id).limit(1)) is not None:
        return 0

    pending = db.scalar(
        select(ProjectStatus).where(ProjectStatus.name == "In Progress")
    )
    champion = db.scalar(select(ContactRole).where(ContactRole.name == "Champion"))
    uc_pending = db.scalar(
        select(UseCaseStatus).where(UseCaseStatus.name == "Pending Testing")
    )
    if not (pending and uc_pending):
        log.warning("sample_data_seed_prereqs_missing")
        return 0

    admin = db.scalar(select(AppUser).where(AppUser.is_seeded.is_(True)))

    customer = Customer(
        name="Acme Corporation",
        website="https://acme.example.com",
        notes="Sample customer seeded for demo purposes.",
    )
    db.add(customer)
    db.flush()

    db.add(
        Contact(
            customer_id=customer.id,
            name="Jordan Rivera",
            email="jordan.rivera@acme.example.com",
            phone="+1 555 0100",
            role_id=champion.id if champion else None,
        )
    )

    project = Project(
        customer_id=customer.id,
        name="Acme IGA Proof of Concept",
        status_id=pending.id,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 7, 15),
        sales_engineer_id=admin.id if admin else None,
        account_executive="Sam Carter",
        account_executive_email="sam.carter@example.com",
        notes="Sample project seeded for demo purposes.",
    )
    db.add(project)
    db.flush()

    # Pull two library entries into the project as snapshots.
    library = db.scalars(
        select(UseCaseLibrary).order_by(UseCaseLibrary.id).limit(2)
    ).all()
    for lib in library:
        db.add(
            ProjectUseCase(
                project_id=project.id,
                source=SOURCE_LIBRARY,
                library_id=lib.id,
                reference_number=lib.default_reference_number,
                category=lib.category,
                name=lib.name,
                description=lib.description,
                success_validation=lib.success_validation,
                feature_type_id=lib.feature_type_id,
                status_id=uc_pending.id,
            )
        )
    db.flush()
    log.info("seeded_sample_data", extra={"customer": customer.name})
    return 1


# ---------------------------------------------------------------------------
# Reset operations (used by the admin reset UI)
# ---------------------------------------------------------------------------


def reset_sample_data(db: Session, reseed: bool = True) -> int:
    """Delete all customers/projects/use-cases/screenshots, optionally reseed.

    Returns the number of customers deleted.
    """
    db.query(Screenshot).delete()
    db.query(ProjectUseCase).delete()
    db.query(Project).delete()
    db.query(Contact).delete()
    deleted = db.query(Customer).delete()
    db.flush()
    log.info("reset_sample_data", extra={"customers_deleted": deleted})
    if reseed:
        seed_sample_data(db)
    db.commit()
    return deleted


def reset_contact_roles(db: Session) -> int:
    db.query(ContactRole).delete()
    db.flush()
    inserted = seed_contact_roles(db)
    db.commit()
    return inserted


def reset_project_statuses(db: Session) -> int:
    db.query(ProjectStatus).delete()
    db.flush()
    inserted = seed_project_statuses(db)
    db.commit()
    return inserted


def reset_feature_types(db: Session) -> int:
    db.query(FeatureType).delete()
    db.flush()
    inserted = seed_feature_types(db)
    db.commit()
    return inserted


def reset_use_case_statuses(db: Session) -> int:
    db.query(UseCaseStatus).delete()
    db.flush()
    inserted = seed_use_case_statuses(db)
    db.commit()
    return inserted


def reset_use_case_library(db: Session) -> int:
    """Delete the library and reseed. Project use cases are unaffected (snapshots)."""
    db.query(UseCaseLibrary).delete()
    db.flush()
    inserted = seed_use_case_library(db)
    db.commit()
    return inserted


def reset_admin_password(db: Session, settings: Settings | None = None) -> bool:
    """Reset the seeded admin user's password back to the configured default."""
    settings = settings or get_settings()
    username = settings.initial_admin_username
    user = db.scalar(select(AppUser).where(AppUser.username == username))
    if user is None or not user.is_seeded:
        return False
    user.password_hash = hash_password(settings.initial_admin_password)
    user.is_active = True
    user.is_admin = True
    user.updated_at = datetime.now(UTC)
    db.commit()
    log.info("reset_admin_password", extra={"username": username})
    return True
