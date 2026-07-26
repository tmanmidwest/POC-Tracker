"""App-user (login account) endpoints.

Read + create login accounts over the REST API. This is what lets the MCP
server (and any API client) provision Sales Engineers, managers, admins, and
external viewers — e.g. to seed sample data or turn a name into a
``sales_engineer_id`` for project assignment.

Role maps to the underlying boolean flags via ``AppUser.role`` (admin |
manager | standard/SE | external). ``password`` is optional: omit it for
SSO-only or placeholder accounts (they simply have no local login).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.schemas.poc import AppUserCreate, AppUserOut
from app.services.audit import principal_actor, record_event
from app.services.auth import Principal, get_authenticated_principal
from app.services.passwords import hash_password

log = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/", response_model=list[AppUserOut])
def list_users(
    role: str | None = Query(default=None, description="Filter by resolved role."),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> list[AppUser]:
    q = db.query(AppUser)
    if not include_inactive:
        q = q.filter(AppUser.is_active.is_(True))
    users = q.order_by(AppUser.username).all()
    if role is not None:
        # ``role`` is a resolved property, so filter in Python.
        users = [u for u in users if u.role == role]
    return users


@router.post("/", response_model=AppUserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: AppUserCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> AppUser:
    if (
        body.email is not None
        and db.query(AppUser).filter(AppUser.email == body.email).first() is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The email '{body.email}' is already in use.",
        )

    new_user = AppUser(
        username=body.username.strip(),
        display_name=body.display_name.strip() if body.display_name else None,
        email=body.email,
        password_hash=hash_password(body.password) if body.password else None,
        is_active=body.is_active,
        is_seeded=False,
    )
    new_user.role = body.role  # maps to the underlying boolean flags
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{body.username}' or that email is already taken.",
        ) from None
    db.refresh(new_user)
    record_event(
        category="admin_user",
        event_type="admin_user.created",
        **principal_actor(principal),
        target_type="app_user",
        target_id=new_user.id,
        target_label=new_user.username,
        message=f"Created {new_user.role} user '{new_user.username}'",
        detail={"surface": "api", "role": new_user.role},
    )
    return new_user
