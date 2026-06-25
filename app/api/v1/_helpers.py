"""Shared helpers used by multiple API endpoints."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def count_references(
    db: Session,
    referencing_model: Any,
    fk_column: Any,
    target_id: int,
) -> int:
    """Count how many rows in `referencing_model` reference `target_id` via `fk_column`."""
    result = db.scalar(
        select(func.count()).select_from(referencing_model).where(fk_column == target_id)
    )
    return int(result or 0)


def raise_conflict_if_referenced(
    db: Session,
    target_label: str,
    references: Sequence[tuple[str, Any, Any, int]],
) -> None:
    """Raise 409 if any of the given references point to target_id.

    Each entry in `references` is (description, model, fk_column, target_id).
    The error message lists every referencer with a non-zero count.
    """
    blockers: list[str] = []
    for description, model, fk_column, target_id in references:
        count = count_references(db, model, fk_column, target_id)
        if count > 0:
            blockers.append(f"{count} {description}")
    if blockers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete {target_label}: still referenced by "
                + ", ".join(blockers)
                + ". Set is_active=false to hide from new dropdowns instead, "
                "or remove the references first."
            ),
        )


def raise_conflict_system_row(target_label: str) -> None:
    """Raise 409 for an attempt to delete or deactivate a system-protected row."""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Cannot modify or delete {target_label}: this is a system row.",
    )
