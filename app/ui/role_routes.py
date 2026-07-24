"""Admin role builder UI — create/edit/delete roles and assign them to users.

Whole router is gated by the ``role.manage`` capability (admins only while the
dynamic-RBAC switch is off). Thin routes over ``app.services.rbac.roles``; all
guard invariants live in that service and surface here as flashes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, Role
from app.services.audit import record_event
from app.services.rbac import capabilities as caps
from app.services.rbac.roles import (
    RoleError,
    assignable_roles,
    create_role,
    delete_role,
    roles_with_counts,
    set_user_roles,
    update_role,
    user_role_ids,
)
from app.services.system_config import rbac_dynamic_enabled
from app.ui.dependencies import require_capability
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/settings/roles", tags=["ui"], include_in_schema=False)

# Whole surface requires the role-management capability.
require_role_manage = require_capability("role.manage")


def _event(request: Request, user: AppUser, event: str, role: Role, verb: str) -> None:
    record_event(
        category="role",
        event_type=f"role.{event}",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="role",
        target_id=role.id,
        target_label=role.name,
        message=f"{verb} role '{role.name}'",
        detail={"surface": "ui", "role_key": role.key},
        request=request,
    )


@router.get("")
def list_roles(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_role_manage),
) -> Response:
    return render(
        request,
        "settings/roles.html",
        current_user=user,
        active_subsection="roles",
        rows=roles_with_counts(db),
        dynamic_enabled=rbac_dynamic_enabled(),
    )


@router.get("/new")
def new_role_form(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_role_manage),
) -> Response:
    return render(
        request,
        "settings/role_form.html",
        current_user=user,
        active_subsection="roles",
        role=None,
        by_area=caps.capabilities_by_area(),
        selected=set(),
        locked=False,
    )


@router.post("/new")
def create_role_submit(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    capabilities: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_role_manage),
) -> Response:
    try:
        role = create_role(db, name=name, description=description, capability_keys=capabilities)
        db.commit()
    except RoleError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url="/ui/settings/roles/new", status_code=303)
    _event(request, user, "created", role, "Created")
    flash(request, f"Role '{role.name}' created.", "success")
    return RedirectResponse(url="/ui/settings/roles", status_code=303)


def _get_role(db: Session, role_id: int) -> Role:
    role = db.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found.")
    return role


@router.get("/{role_id}/edit")
def edit_role_form(
    role_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_role_manage),
) -> Response:
    role = _get_role(db, role_id)
    selected = {rc.capability_key for rc in role.capabilities}
    return render(
        request,
        "settings/role_form.html",
        current_user=user,
        active_subsection="roles",
        role=role,
        by_area=caps.capabilities_by_area(),
        selected=selected,
        # A superuser role implicitly holds everything — matrix is read-only.
        locked=role.is_superuser,
    )


@router.post("/{role_id}/edit")
def edit_role_submit(
    role_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    capabilities: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_role_manage),
) -> Response:
    role = _get_role(db, role_id)
    try:
        update_role(db, role, name=name, description=description, capability_keys=capabilities)
        db.commit()
    except RoleError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url=f"/ui/settings/roles/{role_id}/edit", status_code=303)
    _event(request, user, "updated", role, "Updated")
    flash(request, f"Role '{role.name}' updated.", "success")
    return RedirectResponse(url="/ui/settings/roles", status_code=303)


@router.post("/{role_id}/delete")
def delete_role_submit(
    role_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_role_manage),
) -> Response:
    role = _get_role(db, role_id)
    label, key, rid = role.name, role.key, role.id
    try:
        delete_role(db, role)
        db.commit()
    except RoleError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url="/ui/settings/roles", status_code=303)
    record_event(
        category="role",
        event_type="role.deleted",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="role",
        target_id=rid,
        target_label=label,
        message=f"Deleted role '{label}'",
        detail={"surface": "ui", "role_key": key},
        request=request,
    )
    flash(request, f"Role '{label}' deleted.", "success")
    return RedirectResponse(url="/ui/settings/roles", status_code=303)


# --- per-user role assignment ------------------------------------------------


@router.get("/assign/{user_id}")
def assign_roles_form(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_role_manage),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return render(
        request,
        "settings/user_roles.html",
        current_user=user,
        active_subsection="admin_users",
        target_user=target,
        roles=assignable_roles(db),
        assigned=user_role_ids(db, target),
        is_self=(target.id == user.id),
        dynamic_enabled=rbac_dynamic_enabled(),
    )


@router.post("/assign/{user_id}")
def assign_roles_submit(
    user_id: int,
    request: Request,
    role_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_role_manage),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    try:
        set_user_roles(db, target, set(role_ids), actor=user)
        db.commit()
    except RoleError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url=f"/ui/settings/roles/assign/{user_id}", status_code=303)
    record_event(
        category="role",
        event_type="role.user_roles_changed",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="app_user",
        target_id=target.id,
        target_label=target.username,
        message=f"Changed roles for '{target.username}'",
        detail={"surface": "ui", "role_ids": sorted(role_ids)},
        request=request,
    )
    flash(request, f"Roles updated for '{target.username}'.", "success")
    return RedirectResponse(url="/ui/settings/admin-users", status_code=303)
