"""Sample dataset for local demo / testing instances — NOT for production.

``seed_demo_data()`` populates a realistic spread of customers, projects, use
cases and a couple of extra sales engineers so the dashboard insights (status
mix, engineer load, at-risk / stalled, feature areas) have something to show.

It is deliberately isolated from ``seed_data.py`` (which seeds only the lookups
and a single sample project on every startup). This module is opt-in and driven
by the ``poct-seed-demo`` CLI, which guards against running on the wrong DB.

Idempotent: customers are keyed by name, so re-running skips ones already there
(pass ``force=True`` to add them again). ``purge_demo_data()`` removes exactly
the rows this module creates, so a demo instance can be reset cleanly.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AppUser,
    Customer,
    FeatureType,
    Project,
    ProjectStatus,
    ProjectUseCase,
    UseCaseStatus,
)
from app.services.passwords import hash_password

log = logging.getLogger(__name__)

# Extra sales engineers so the "portfolio by engineer" chart is meaningful.
# (username, display name). They log in with DEMO_USER_PASSWORD.
DEMO_ENGINEERS = [
    ("amaya", "Amaya Okonkwo"),
    ("devlin", "Devlin Ross"),
]
DEMO_USER_PASSWORD = "demo-password-123"

# Each project: (customer, status_index, engineer_index, total_use_cases,
# completed_use_cases, end_date_offset_days). A negative offset that is not fully
# complete makes the project "at risk"; the two named below are also aged so they
# read as "stalled". status_index / engineer_index are clamped to what exists.
_DEMO_PROJECTS = [
    ("Northwind Trading", 0, 0, 6, 2, None),
    ("Contoso Manufacturing", 1, 1, 5, 5, None),
    ("Fabrikam Health", 0, 2, 8, 1, -9),
    ("Tailspin Toys", 2, 0, 4, 3, 20),
    ("Wingtip Insurance", 1, 1, 7, 4, None),
    ("Adventure Works", 0, 2, 5, 0, -3),
    ("Proseware Media", 3, 0, 3, 3, None),
]
_STALLED = {"Adventure Works", "Proseware Media"}

# Exact customer names this module owns — used to purge cleanly.
DEMO_CUSTOMER_NAMES = [name for (name, *_rest) in _DEMO_PROJECTS]


def _get_or_create_engineers(db: Session) -> list[AppUser]:
    """The seeded admin plus the two demo engineers (created if missing)."""
    admin = db.scalars(
        select(AppUser).where(AppUser.is_seeded.is_(True))
    ).first()
    # Fall back to any admin if the seeded flag isn't set for some reason.
    if admin is None:
        admin = db.scalars(
            select(AppUser).where(AppUser.is_admin.is_(True))
        ).first()

    engineers: list[AppUser] = [admin] if admin else []
    for username, display in DEMO_ENGINEERS:
        user = db.scalars(
            select(AppUser).where(AppUser.username == username)
        ).first()
        if user is None:
            user = AppUser(
                username=username,
                display_name=display,
                password_hash=hash_password(DEMO_USER_PASSWORD),
                is_admin=False,
                is_external=False,
                is_active=True,
            )
            db.add(user)
            db.flush()
        engineers.append(user)
    return engineers


def seed_demo_data(db: Session, *, force: bool = False) -> dict[str, int]:
    """Insert the demo dataset. Returns a summary of what was created.

    Idempotent by customer name unless ``force`` is set.
    """
    statuses = list(
        db.scalars(select(ProjectStatus).order_by(ProjectStatus.sort_order))
    )
    uc_statuses = list(
        db.scalars(select(UseCaseStatus).order_by(UseCaseStatus.sort_order))
    )
    features = list(db.scalars(select(FeatureType)))
    if not statuses or not uc_statuses:
        raise RuntimeError(
            "Lookup tables are empty — start the app once so the database is "
            "seeded before loading demo data."
        )
    done_status = next(s for s in uc_statuses if s.is_complete_status)
    open_statuses = [s for s in uc_statuses if not s.is_complete_status] or uc_statuses

    engineers = _get_or_create_engineers(db)
    if not engineers:
        raise RuntimeError("No admin/engineer user found to own demo projects.")

    # Deterministic status/feature spread across runs.
    rng = random.Random(7)
    summary = {"customers": 0, "projects": 0, "use_cases": 0, "skipped": 0}

    for name, sidx, eidx, total, ndone, end_off in _DEMO_PROJECTS:
        existing = db.scalars(
            select(Customer).where(Customer.name == name)
        ).first()
        if existing is not None and not force:
            summary["skipped"] += 1
            continue

        customer = Customer(name=name)
        db.add(customer)
        db.flush()
        summary["customers"] += 1

        status = statuses[min(sidx, len(statuses) - 1)]
        engineer = engineers[eidx % len(engineers)]
        end_date = (
            date.today() + timedelta(days=end_off) if end_off is not None else None
        )
        project = Project(
            customer_id=customer.id,
            name=f"{name} POC",
            status_id=status.id,
            sales_engineer_id=engineer.id,
            start_date=date.today() - timedelta(days=40),
            end_date=end_date,
        )
        db.add(project)
        db.flush()
        summary["projects"] += 1

        for j in range(total):
            uc_status = done_status if j < ndone else rng.choice(open_statuses)
            feature = features[j % len(features)] if features else None
            project.use_cases.append(
                ProjectUseCase(
                    category=(feature.name if feature else "General"),
                    name=f"{name} use case {j + 1}",
                    status_id=uc_status.id,
                    feature_type_id=(feature.id if feature else None),
                    source="custom",
                )
            )
            summary["use_cases"] += 1

        db.commit()

        # Age a couple so they surface as "stalled" on the dashboard.
        if name in _STALLED:
            aged = datetime.now(UTC) - timedelta(days=24)
            db.query(Project).filter(Project.id == project.id).update(
                {Project.updated_at: aged}
            )
            db.commit()

    log.info("demo_data_seeded", extra=summary)
    return summary


def purge_demo_data(db: Session) -> dict[str, int]:
    """Remove exactly the customers/projects/use cases this module creates, plus
    the demo engineers if they no longer own any projects."""
    removed = {"customers": 0, "projects": 0, "engineers": 0}

    customers = list(
        db.scalars(select(Customer).where(Customer.name.in_(DEMO_CUSTOMER_NAMES)))
    )
    for customer in customers:
        # customer.projects has no delete-cascade, so remove projects first;
        # each project cascades to its use cases.
        for project in list(customer.projects):
            db.delete(project)
            removed["projects"] += 1
        db.delete(customer)
        removed["customers"] += 1
    db.commit()

    for username, _display in DEMO_ENGINEERS:
        user = db.scalars(
            select(AppUser).where(AppUser.username == username)
        ).first()
        if user is None:
            continue
        still_owns = db.scalars(
            select(Project.id).where(Project.sales_engineer_id == user.id)
        ).first()
        if still_owns is None:
            db.delete(user)
            removed["engineers"] += 1
    db.commit()

    log.info("demo_data_purged", extra=removed)
    return removed
