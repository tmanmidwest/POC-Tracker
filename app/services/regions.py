"""Region membership helpers.

Thin layer over the ``user_regions`` join so the UI, bulk tools, and (later)
access enforcement share one implementation of "which regions is this user in?"
and "set this user's regions". Admins and external viewers ignore memberships,
but nothing here enforces that — callers decide when a user is region-scoped.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppUser, Project, Region, UserRegion

# Separators allowed *inside* a CSV region cell to list multiple regions, so the
# comma stays free as the CSV column delimiter (e.g. "jane@x.com,AMER;EMEA").
_REGION_CELL_SPLIT = re.compile(r"[;|]")
# First-column header names we skip if the CSV includes a header row.
_CSV_HEADER_KEYS = {"identifier", "user", "username", "email", "user_id", "login"}


def get_user_region_ids(db: Session, user_id: int) -> set[int]:
    """Return the set of region ids the user is a member of."""
    rows = db.scalars(
        select(UserRegion.region_id).where(UserRegion.user_id == user_id)
    ).all()
    return set(rows)


def set_user_regions(db: Session, user_id: int, region_ids: Iterable[int]) -> None:
    """Reconcile a user's region memberships to exactly ``region_ids``.

    Adds missing memberships and removes ones no longer selected. Silently drops
    ids that don't correspond to a real region so a stale form value can't create
    a dangling membership. Does not commit — the caller owns the transaction.
    """
    desired = set(region_ids)
    if desired:
        valid = set(
            db.scalars(select(Region.id).where(Region.id.in_(desired))).all()
        )
        desired &= valid

    current = get_user_region_ids(db, user_id)

    to_add = desired - current
    to_remove = current - desired

    for rid in to_add:
        db.add(UserRegion(user_id=user_id, region_id=rid))
    if to_remove:
        db.query(UserRegion).filter(
            UserRegion.user_id == user_id,
            UserRegion.region_id.in_(to_remove),
        ).delete(synchronize_session=False)


def unassigned_region_id(db: Session) -> int | None:
    """Return the id of the seeded system 'Unassigned' region, if present."""
    return db.scalar(
        select(Region.id).where(Region.is_system.is_(True)).order_by(Region.id).limit(1)
    )


def resolve_se_region_id(db: Session, sales_engineer_id: int | None) -> int | None:
    """The region a project inherits from its SE — only when unambiguous.

    Returns the SE's region id when that SE belongs to exactly one region;
    otherwise None (no SE, or a manager/SE spanning several — too ambiguous to
    pick automatically). Callers fall back to the Unassigned bucket.
    """
    if not sales_engineer_id:
        return None
    ids = get_user_region_ids(db, sales_engineer_id)
    return next(iter(ids)) if len(ids) == 1 else None


def backfill_project_regions(
    db: Session, *, fallback_to_unassigned: bool = True
) -> dict[str, int]:
    """Derive each project's region from its SE; orphans → Unassigned.

    For every project, set ``region_id`` to the SE's region when that resolves
    (see ``resolve_se_region_id``). Projects that don't resolve keep an existing
    region if they have one; otherwise, when ``fallback_to_unassigned`` is set,
    they're parked in the system 'Unassigned' region so enforcement never makes
    them invisible to everyone. Re-runnable — run again after assigning SE
    regions to pull projects into their real regions. Does not commit.

    Returns counts: ``total``, ``derived`` (set from SE), ``unassigned``
    (parked in the fallback), ``unchanged``.
    """
    fallback_id = unassigned_region_id(db) if fallback_to_unassigned else None
    projects = db.query(Project).all()
    derived = unassigned = unchanged = 0
    for project in projects:
        resolved = resolve_se_region_id(db, project.sales_engineer_id)
        if resolved is not None:
            if project.region_id != resolved:
                project.region_id = resolved
                derived += 1
            else:
                unchanged += 1
        elif project.region_id is None and fallback_id is not None:
            project.region_id = fallback_id
            unassigned += 1
        else:
            unchanged += 1
    return {
        "total": len(projects),
        "derived": derived,
        "unassigned": unassigned,
        "unchanged": unchanged,
    }


def parse_region_csv(text: str) -> list[tuple[str, list[str]]]:
    """Parse bulk-assignment CSV into ``(identifier, [region_name, ...])`` rows.

    Format: two columns, ``identifier,regions``. The identifier is a username or
    email; the regions cell lists one or more region names separated by ``;`` or
    ``|`` (so the comma stays the column delimiter). A leading header row is
    skipped if its first cell is a known header key. Blank rows are ignored.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return []
    if rows[0] and rows[0][0].strip().lower() in _CSV_HEADER_KEYS:
        rows = rows[1:]

    entries: list[tuple[str, list[str]]] = []
    for row in rows:
        identifier = row[0].strip() if row else ""
        if not identifier:
            continue
        cell = row[1] if len(row) > 1 else ""
        names = [p.strip() for p in _REGION_CELL_SPLIT.split(cell) if p.strip()]
        entries.append((identifier, names))
    return entries


def bulk_set_regions(
    db: Session, entries: Iterable[tuple[str, list[str]]]
) -> dict[str, list[str]]:
    """Apply ``(identifier, region_names)`` assignments to region-scoped users.

    Matches each identifier to a user by username or email (case-insensitive),
    resolves region names (case-insensitive), and reconciles that user's
    memberships to exactly the named set. Admins and external viewers are skipped
    (they ignore regions). Does not commit — the caller owns the transaction.

    Returns a summary with keys ``updated`` (usernames), ``unmatched``
    (identifiers with no user), ``skipped`` (matched an admin/external), and
    ``unknown_regions`` (region names that didn't resolve).
    """
    users = db.query(AppUser).all()
    by_username = {u.username.lower(): u for u in users}
    by_email = {u.email.lower(): u for u in users if u.email}
    by_region = {r.name.lower(): r for r in db.query(Region).all()}

    summary: dict[str, list[str]] = {
        "updated": [],
        "unmatched": [],
        "skipped": [],
        "unknown_regions": [],
    }
    seen_unknown: set[str] = set()

    for identifier, names in entries:
        key = identifier.strip().lower()
        user = by_username.get(key) or by_email.get(key)
        if user is None:
            summary["unmatched"].append(identifier)
            continue
        if user.is_admin or user.is_external:
            summary["skipped"].append(identifier)
            continue
        region_ids: list[int] = []
        for name in names:
            region = by_region.get(name.strip().lower())
            if region is None:
                if name.strip() and name.strip().lower() not in seen_unknown:
                    seen_unknown.add(name.strip().lower())
                    summary["unknown_regions"].append(name.strip())
            else:
                region_ids.append(region.id)
        set_user_regions(db, user.id, region_ids)
        summary["updated"].append(user.username)

    return summary
