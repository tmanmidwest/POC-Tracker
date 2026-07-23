"""Region membership helpers.

Thin layer over the ``user_regions`` join so the UI, bulk tools, and (later)
access enforcement share one implementation of "which regions is this user in?"
and "set this user's regions". Admins and external viewers ignore memberships,
but nothing here enforces that — callers decide when a user is region-scoped.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Region, UserRegion


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
