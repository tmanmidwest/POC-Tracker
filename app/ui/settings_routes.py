"""UI routes for settings: admin users, API keys, OAuth clients, SSO, reset."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    AIProvider,
    ApiKey,
    AppBranding,
    AppUser,
    AuthProvider,
    Customer,
    OAuthClient,
    Project,
    ProjectGrant,
    Region,
    UserIdentity,
)
from app.models.app_branding import BRANDING_ID
from app.models.app_user import VALID_ROLES
from app.models.audit_event import AuditEvent
from app.models.auth_provider import DEFAULT_SCOPES
from app.models.backup_run import BackupRun
from app.services import backups as backup_service
from app.services import branding as branding_service
from app.services import demo_data
from app.services import login_security
from app.services import report_template
from app.services import (
    mcp_gateway,
    mcp_gateway_tokens,
    mcp_token,
    seed_data,
    system_config,
)
from app.services.ai import PROVIDERS, get_provider_spec
from app.services.audit import prune_old_events, record_event
from app.services.oidc import callback_url
from app.services.passwords import hash_password
from app.services.regions import (
    backfill_project_regions,
    bulk_set_regions,
    get_user_region_ids,
    parse_region_csv,
    set_user_regions,
)
from app.services.secret_box import encrypt_secret
from app.services.tokens import (
    generate_api_key,
    generate_oauth_client_id,
    generate_oauth_client_secret,
    hash_token,
)
from app.ui.dependencies import require_admin_ui, require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/settings", tags=["ui"], include_in_schema=False)


def _settings_event(
    request: Request,
    user: AppUser,
    *,
    category: str,
    event_type: str,
    target_type: str | None = None,
    target_id: object = None,
    target_label: str | None = None,
    message: str = "",
    detail: dict | None = None,
) -> None:
    """Record a settings-area audit event performed by a logged-in admin."""
    record_event(
        category=category,
        event_type=event_type,
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type=target_type,
        target_id=target_id,
        target_label=target_label,
        message=message,
        detail={"surface": "ui", **(detail or {})},
        request=request,
    )


# ===========================================================================
# Settings hub
# ===========================================================================


@router.get("")
def settings_hub(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Landing page linking out to each settings area."""
    return render(
        request,
        "settings/index.html",
        current_user=user,
        active_subsection="settings",
        demo_tools_enabled=get_settings().enable_demo_tools,
    )


# ===========================================================================
# Admin users
# ===========================================================================


@router.get("/admin-users")
def list_admins(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    from app.models import AuthProvider, ProjectGrant, UserIdentity, UserInvite

    users = db.query(AppUser).order_by(AppUser.username).all()
    internal_users = [u for u in users if not u.is_external]

    # Identity source per internal user: their SSO provider(s) if linked, else a
    # local password account. One bulk query keyed by user id (no N+1).
    internal_ids = [u.id for u in internal_users]
    providers_by_user: dict[int, list[str]] = {}
    if internal_ids:
        rows = (
            db.query(UserIdentity.user_id, AuthProvider.display_name)
            .join(AuthProvider, UserIdentity.provider_id == AuthProvider.id)
            .filter(UserIdentity.user_id.in_(internal_ids))
            .order_by(AuthProvider.display_name)
            .all()
        )
        for uid, provider_name in rows:
            providers_by_user.setdefault(uid, []).append(provider_name)
    identity_sources: dict[int, str] = {}
    for u in internal_users:
        names = providers_by_user.get(u.id)
        if names:
            identity_sources[u.id] = ", ".join(names)
        elif u.password_hash is not None:
            identity_sources[u.id] = "Local"
        else:
            identity_sources[u.id] = "—"

    # External users shown in a distinct box with their company, sign-in status,
    # and which projects they can view.
    now = datetime.now(UTC)
    external_users = []
    for u in (x for x in users if x.is_external):
        grants = (
            db.query(ProjectGrant).filter(ProjectGrant.user_id == u.id).all()
        )
        latest_invite = (
            db.query(UserInvite)
            .filter(UserInvite.user_id == u.id)
            .order_by(UserInvite.id.desc())
            .first()
        )
        if u.password_hash is not None:
            # Accepted account: distinguish live vs. expired/deactivated.
            if not u.is_active:
                status = "account_expired" if u.is_expired else "disabled"
            else:
                status = "active"
        elif latest_invite is None:
            status = "none"
        elif latest_invite.status == "revoked":
            status = "revoked"
        else:
            exp = latest_invite.expires_at
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            status = "invite_expired" if (exp is not None and exp < now) else "pending"
        days_left = u.days_until_expiry
        external_users.append({
            "user": u,
            "projects": [g.project for g in grants if g.project],
            "accepted": u.password_hash is not None,
            "status": status,
            "expires_at": u.expires_at_aware,
            "days_left": days_left,
            "expiring_soon": days_left is not None and 0 <= days_left <= 7,
        })

    return render(
        request,
        "settings/admin_users.html",
        current_user=user,
        active_subsection="admin_users",
        internal_users=internal_users,
        identity_sources=identity_sources,
        external_users=external_users,
    )


def _normalize_optional_email(raw: str) -> str | None:
    """Normalize a submitted email to match the invitation flow (strip + lower).

    Returns ``None`` for a blank value so it stores as NULL — an empty string
    would collide on the unique constraint if two accounts left it blank.
    """
    norm = (raw or "").strip().lower()
    return norm or None


@router.get("/admin-users/new")
def show_new_admin(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request,
        "settings/admin_user_new.html",
        current_user=user,
        active_subsection="admin_users",
        form={},
    )


@router.post("/admin-users/new")
def create_admin(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("standard"),
    display_name: str = Form(""),
    email: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    username = username.strip()
    display = display_name.strip() or None
    email_norm = _normalize_optional_email(email)

    def _reject(message: str) -> Response:
        return render(
            request,
            "settings/admin_user_new.html",
            current_user=user,
            active_subsection="admin_users",
            form={
                "username": username,
                "role": role,
                "display_name": display,
                "email": email_norm,
            },
            error=message,
        )

    if len(password) < 8:
        return _reject("Password must be at least 8 characters.")
    if email_norm is not None and "@" not in email_norm:
        return _reject("Enter a valid email address, or leave it blank.")
    if (
        email_norm is not None
        and db.query(AppUser).filter(AppUser.email == email_norm).first() is not None
    ):
        return _reject(f"The email '{email_norm}' is already in use.")

    new_user = AppUser(
        username=username,
        display_name=display,
        email=email_norm,
        password_hash=hash_password(password),
        is_active=True,
        is_seeded=False,
    )
    # Map the selected role to the underlying flags. Unknown/blank falls back to
    # an SE. Region assignment happens after creation (see user regions).
    new_user.role = role if role in VALID_ROLES else "standard"
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _reject(
            f"Username '{username}' or that email is already taken."
        )
    log.info(
        "ui_admin_created",
        extra={"target_user_id": new_user.id, "target_username": username, "by": user.username},
    )
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.created",
        target_type="app_user",
        target_id=new_user.id,
        target_label=username,
        message=f"Created admin user '{username}'",
    )
    flash(request, f"Admin user '{username}' created.", "success")
    return RedirectResponse(url="/ui/settings/admin-users", status_code=303)


@router.get("/admin-users/{user_id}/password")
def show_password_form(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Admin user not found.")
    return render(
        request,
        "settings/admin_user_password.html",
        current_user=user,
        active_subsection="admin_users",
        target_user=target,
    )


@router.get("/admin-users/{user_id}/edit")
def show_edit_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return render(
        request,
        "settings/admin_user_edit.html",
        current_user=user,
        active_subsection="admin_users",
        target_user=target,
        regions=db.query(Region).filter(Region.is_active.is_(True))
        .order_by(Region.sort_order, Region.name)
        .all(),
        assigned_region_ids=get_user_region_ids(db, target.id),
    )


@router.post("/admin-users/{user_id}/edit")
def update_user(
    user_id: int,
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    region_ids: list[int] = Form(default=[]),
    region_form: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    email_norm = _normalize_optional_email(email)

    def _reject(message: str) -> Response:
        return render(
            request,
            "settings/admin_user_edit.html",
            current_user=user,
            active_subsection="admin_users",
            target_user=target,
            regions=db.query(Region).filter(Region.is_active.is_(True))
            .order_by(Region.sort_order, Region.name)
            .all(),
            assigned_region_ids=set(region_ids),
            error=message,
        )

    if email_norm is not None and "@" not in email_norm:
        return _reject("Enter a valid email address, or leave it blank.")
    if email_norm is not None:
        clash = (
            db.query(AppUser)
            .filter(AppUser.email == email_norm, AppUser.id != target.id)
            .first()
        )
        if clash is not None:
            return _reject(f"The email '{email_norm}' is already in use.")

    target.display_name = display_name.strip() or None
    target.email = email_norm
    # Reconcile region memberships only when the region selector was actually
    # part of the submitted form (region_form marker present). This is shown for
    # region-scoped roles (SE/manager); admins/externals ignore regions, so
    # their edits omit the marker and never touch memberships.
    if region_form:
        set_user_regions(db, target.id, region_ids)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _reject("That email is already in use.")
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.updated",
        target_type="app_user",
        target_id=target.id,
        target_label=target.username,
        message=f"Updated profile for '{target.username}'",
        detail={"display_name": target.display_name, "email": target.email},
    )
    flash(request, f"Updated profile for '{target.username}'.", "success")
    return RedirectResponse(url="/ui/settings/admin-users", status_code=303)


@router.post("/admin-users/{user_id}/password")
def change_password(
    user_id: int,
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Admin user not found.")

    if new_password != confirm_password:
        return render(
            request,
            "settings/admin_user_password.html",
            current_user=user,
            active_subsection="admin_users",
            target_user=target,
            error="Passwords do not match.",
        )
    if len(new_password) < 8:
        return render(
            request,
            "settings/admin_user_password.html",
            current_user=user,
            active_subsection="admin_users",
            target_user=target,
            error="Password must be at least 8 characters.",
        )

    target.password_hash = hash_password(new_password)
    # Setting a fresh password also lifts any failed-login lockout.
    login_security.clear_lockout(target)
    db.commit()
    log.info(
        "ui_password_changed",
        extra={"target_user_id": target.id, "target_username": target.username, "by": user.username},
    )
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.password_changed",
        target_type="app_user",
        target_id=target.id,
        target_label=target.username,
        message=f"Changed password for admin user '{target.username}'",
    )
    flash(request, f"Password updated for {target.username}.", "success")
    return RedirectResponse(url="/ui/settings/admin-users", status_code=303)


@router.post("/admin-users/{user_id}/unlock")
def unlock_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Lift a failed-login lockout so the user can sign in again."""
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if not target.is_locked:
        flash(request, f"{target.username} is not locked.", "info")
        return RedirectResponse(url="/ui/settings/admin-users", status_code=303)
    login_security.clear_lockout(target)
    db.commit()
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.unlocked",
        target_type="app_user",
        target_id=target.id,
        target_label=target.username,
        message=f"Unlocked '{target.username}' after a failed-login lockout",
    )
    flash(request, f"Unlocked {target.username}.", "success")
    return RedirectResponse(url="/ui/settings/admin-users", status_code=303)


@router.post("/admin-users/{user_id}/resend-invite")
def resend_user_invite(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Re-send the latest invitation for an external user who hasn't accepted."""
    from app.models import UserInvite
    from app.services import email as email_service
    from app.services import invitations

    back = RedirectResponse(url="/ui/settings/admin-users", status_code=303)
    target = db.get(AppUser, user_id)
    if target is None or not target.is_external:
        raise HTTPException(status_code=404, detail="External user not found.")

    invite = (
        db.query(UserInvite)
        .filter(UserInvite.user_id == user_id)
        .order_by(UserInvite.id.desc())
        .first()
    )
    if invite is None:
        flash(request, "There's no invitation to resend for this user.", "error")
        return back
    if invite.status == "accepted":
        flash(request, f"{target.email or target.username} already accepted.", "info")
        return back

    base_url = get_settings().public_base_url or str(request.base_url).rstrip("/")
    try:
        invitations.resend_invite(db, invite, base_url=base_url)
    except (invitations.InvitationError, email_service.EmailError) as exc:
        record_event(
            category="invitation",
            event_type="invitation.resend_failed",
            outcome="failure",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="user_invite", target_id=invite.id,
            target_label=target.email or target.username,
            message=f"Failed to resend invitation to {target.email or target.username}",
            detail={"surface": "ui", "error": str(exc), "error_type": type(exc).__name__},
            request=request,
        )
        flash(request, f"Couldn't resend the invitation: {exc}", "error")
        return back

    _settings_event(
        request, user,
        category="invitation",
        event_type="invitation.resent",
        target_type="user_invite",
        target_id=invite.id,
        target_label=target.email or target.username,
        message=f"Resent invitation to {target.email or target.username}",
    )
    flash(request, f"Invitation resent to {target.email or target.username}.", "success")
    return back


@router.post("/admin-users/{user_id}/extend")
def extend_external_user(
    user_id: int,
    request: Request,
    preset: str = Form(""),
    until: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Extend (and reactivate) an external user's account expiry."""
    from app.services import external_expiry

    back = RedirectResponse(url="/ui/settings/admin-users", status_code=303)
    target = db.get(AppUser, user_id)
    if target is None or not target.is_external:
        raise HTTPException(status_code=404, detail="External user not found.")
    try:
        new_expiry = external_expiry.resolve_extension(preset or None, until or None)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return back
    external_expiry.extend_user(db, target, until=new_expiry, actor=user, request=request)
    flash(
        request,
        f"{target.email or target.username} now expires {new_expiry.date().isoformat()}.",
        "success",
    )
    return back


@router.post("/admin-users/{user_id}/delete")
def delete_admin(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Admin user not found.")
    if target.is_seeded:
        flash(request, "Cannot delete the seeded admin user. Disable it instead.", "error")
        return RedirectResponse(url="/ui/settings/admin-users", status_code=303)
    if target.id == user.id:
        flash(request, "You cannot delete your own account.", "error")
        return RedirectResponse(url="/ui/settings/admin-users", status_code=303)

    # Detach references that would otherwise block the delete (these FKs don't
    # cascade). Provenance columns are nulled; the user's own API keys are
    # removed; OAuth clients they created are reassigned to the acting admin so
    # the integration keeps working. CASCADE handles grants-to-them, prefs, and
    # linked identities automatically. The username is preserved in audit history
    # (audit_events has no FK to app_users by design).
    username = target.username
    db.query(Project).filter(Project.sales_engineer_id == target.id).update(
        {Project.sales_engineer_id: None}
    )
    db.query(ProjectGrant).filter(ProjectGrant.granted_by_user_id == target.id).update(
        {ProjectGrant.granted_by_user_id: None}
    )
    db.query(AIProvider).filter(AIProvider.created_by_user_id == target.id).update(
        {AIProvider.created_by_user_id: None}
    )
    db.query(AuthProvider).filter(AuthProvider.created_by_user_id == target.id).update(
        {AuthProvider.created_by_user_id: None}
    )
    db.query(ApiKey).filter(ApiKey.created_by_user_id == target.id).delete()
    db.query(OAuthClient).filter(OAuthClient.created_by_user_id == target.id).update(
        {OAuthClient.created_by_user_id: user.id}
    )

    db.delete(target)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        log.warning("ui_admin_delete_blocked", extra={"target_user_id": user_id})
        flash(
            request,
            f"Couldn't delete '{username}' — they're still referenced by other "
            "records. Disable the account instead, or remove those references first.",
            "error",
        )
        return RedirectResponse(url="/ui/settings/admin-users", status_code=303)
    log.info(
        "ui_admin_deleted",
        extra={"target_user_id": user_id, "target_username": username, "by": user.username},
    )
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.deleted",
        target_type="app_user",
        target_id=user_id,
        target_label=username,
        message=f"Deleted admin user '{username}'",
    )
    flash(request, f"Deleted admin user '{username}'.", "success")
    return RedirectResponse(url="/ui/settings/admin-users", status_code=303)


@router.post("/admin-users/{user_id}/role")
def change_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    back = RedirectResponse(url="/ui/settings/admin-users", status_code=303)

    # This page manages internal accounts only; "external" is set elsewhere (the
    # invite / External users flow), so it isn't an accepted target here.
    labels = {"standard": "an SE", "manager": "a manager", "admin": "an admin"}
    if role not in labels:
        flash(request, "Invalid role.", "error")
        return back

    if target.id == user.id:
        flash(request, "You can't change your own role.", "error")
        return back
    if role != "admin" and target.is_seeded:
        flash(request, "The seeded admin must remain an admin.", "error")
        return back
    # Don't demote the last remaining admin (to manager or SE).
    if role != "admin" and target.is_admin:
        admin_count = db.query(AppUser).filter(AppUser.is_admin.is_(True)).count()
        if admin_count <= 1:
            flash(request, "There must be at least one admin.", "error")
            return back
    if target.role == role:
        return back  # no change

    # The setter maps the role name to the underlying flags (and clears
    # is_external, so promoting a user here always lands an internal account).
    target.role = role
    db.commit()
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.role_changed",
        target_type="app_user",
        target_id=target.id,
        target_label=target.username,
        message=f"Changed role of '{target.username}' to {role}",
        detail={"role": role},
    )
    flash(request, f"'{target.username}' is now {labels[role]}.", "success")
    return back


# ---------------------------------------------------------------------------
# Bulk region assignment (grid + CSV import)
# ---------------------------------------------------------------------------


def _region_scoped_users(db: Session) -> list[AppUser]:
    """Internal, non-admin users (SEs + managers) — the region-scoped set.

    Admins see every region and external viewers use share grants, so neither is
    listed here.
    """
    return (
        db.query(AppUser)
        .filter(AppUser.is_external.is_(False), AppUser.is_admin.is_(False))
        .order_by(AppUser.username)
        .all()
    )


def _render_bulk_regions(request: Request, user: AppUser, db: Session) -> Response:
    users = _region_scoped_users(db)
    regions = (
        db.query(Region)
        .filter(Region.is_active.is_(True))
        .order_by(Region.sort_order, Region.name)
        .all()
    )
    assigned = {u.id: get_user_region_ids(db, u.id) for u in users}
    return render(
        request,
        "settings/bulk_regions.html",
        current_user=user,
        active_subsection="admin_users",
        users=users,
        regions=regions,
        assigned=assigned,
    )


@router.get("/bulk-regions")
def show_bulk_regions(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return _render_bulk_regions(request, user, db)


@router.post("/bulk-regions")
async def save_bulk_regions(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    form = await request.form()
    # Hidden field lists every user row rendered, so a user whose boxes are all
    # unchecked is still reconciled (to no regions) rather than skipped.
    row_ids = {int(v) for v in form.getlist("user_ids") if str(v).isdigit()}
    scoped_ids = {u.id for u in _region_scoped_users(db)}
    updated = 0
    for uid in row_ids & scoped_ids:
        region_ids = [
            int(v) for v in form.getlist(f"regions_{uid}") if str(v).isdigit()
        ]
        set_user_regions(db, uid, region_ids)
        updated += 1
    db.commit()
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.regions_bulk_updated",
        target_type="app_user",
        target_id=None,
        target_label=f"{updated} users",
        message=f"Bulk-updated regions for {updated} users",
        detail={"count": updated, "via": "grid"},
    )
    flash(request, f"Saved region assignments for {updated} users.", "success")
    return RedirectResponse(url="/ui/settings/bulk-regions", status_code=303)


@router.post("/bulk-regions/import")
async def import_bulk_regions(
    request: Request,
    csv_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    raw = await csv_file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        flash(request, "Couldn't read that file — please upload a UTF-8 CSV.", "error")
        return RedirectResponse(url="/ui/settings/bulk-regions", status_code=303)

    entries = parse_region_csv(text)
    if not entries:
        flash(request, "No rows found in that CSV.", "error")
        return RedirectResponse(url="/ui/settings/bulk-regions", status_code=303)

    summary = bulk_set_regions(db, entries)
    db.commit()
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.regions_bulk_updated",
        target_type="app_user",
        target_id=None,
        target_label=f"{len(summary['updated'])} users",
        message=f"Imported region assignments for {len(summary['updated'])} users",
        detail={"count": len(summary["updated"]), "via": "csv"},
    )

    parts = [f"Updated {len(summary['updated'])} users."]
    if summary["unmatched"]:
        parts.append(f"No match for: {', '.join(summary['unmatched'][:10])}.")
    if summary["skipped"]:
        parts.append(
            f"Skipped (admin/external): {', '.join(summary['skipped'][:10])}."
        )
    if summary["unknown_regions"]:
        parts.append(f"Unknown regions: {', '.join(summary['unknown_regions'][:10])}.")
    level = "success" if not (summary["unmatched"] or summary["unknown_regions"]) else "error"
    flash(request, " ".join(parts), level)
    return RedirectResponse(url="/ui/settings/bulk-regions", status_code=303)


# ===========================================================================
# API Keys
# ===========================================================================


@router.get("/api-keys")
def list_api_keys(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    # Pull new_key out of session (one-shot reveal after creation)
    new_key = request.session.pop("_revealed_api_key", None)
    return render(
        request,
        "settings/api_keys.html",
        current_user=user,
        active_subsection="api_keys",
        keys=keys,
        new_key=new_key,
        mcp_key_id=mcp_token.current_key_id(db),
    )


@router.get("/api-keys/new")
def show_new_api_key(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request,
        "settings/api_key_new.html",
        current_user=user,
        active_subsection="api_keys",
    )


@router.post("/api-keys/new")
def create_api_key(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    full_key, prefix = generate_api_key()
    key = ApiKey(
        name=name.strip(),
        key_prefix=prefix,
        key_hash=hash_token(full_key),
        created_by_user_id=user.id,
    )
    db.add(key)
    db.commit()
    log.info(
        "ui_api_key_created",
        extra={"api_key_id": key.id, "key_name": key.name, "prefix": prefix, "by": user.username},
    )
    _settings_event(
        request, user,
        category="api_key",
        event_type="api_key.created",
        target_type="api_key",
        target_id=key.id,
        target_label=key.name,
        message=f"Created API key '{key.name}'",
        detail={"prefix": prefix},
    )
    # Stash the full key in session so the list page can reveal it once
    request.session["_revealed_api_key"] = full_key
    return RedirectResponse(url="/ui/settings/api-keys", status_code=303)


@router.post("/api-keys/{key_id}/revoke")
def revoke_api_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    if mcp_token.current_key_id(db) == key_id:
        flash(request, "That's the MCP server token — rotate or clear it on the MCP page.", "warning")
        return RedirectResponse(url="/ui/settings/mcp", status_code=303)
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        db.commit()
        log.info("ui_api_key_revoked", extra={"api_key_id": key_id, "by": user.username})
        _settings_event(
            request, user,
            category="api_key",
            event_type="api_key.revoked",
            target_type="api_key",
            target_id=key.id,
            target_label=key.name,
            message=f"Revoked API key '{key.name}'",
        )
        flash(request, f"Revoked API key '{key.name}'.", "success")
    return RedirectResponse(url="/ui/settings/api-keys", status_code=303)


@router.post("/api-keys/{key_id}/delete")
def delete_api_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    if mcp_token.current_key_id(db) == key_id:
        flash(request, "That's the MCP server token — rotate or clear it on the MCP page.", "warning")
        return RedirectResponse(url="/ui/settings/mcp", status_code=303)
    name = key.name
    db.delete(key)
    db.commit()
    log.info("ui_api_key_deleted", extra={"api_key_id": key_id, "by": user.username})
    _settings_event(
        request, user,
        category="api_key",
        event_type="api_key.deleted",
        target_type="api_key",
        target_id=key_id,
        target_label=name,
        message=f"Deleted API key '{name}'",
    )
    flash(request, f"Deleted API key '{name}'.", "success")
    return RedirectResponse(url="/ui/settings/api-keys", status_code=303)


# ===========================================================================
# MCP token — a rotatable key the MCP server reads live from the data volume
# ===========================================================================


@router.get("/mcp")
def show_mcp_token(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    new_token = request.session.pop("_revealed_mcp_token", None)
    new_gateway_token = request.session.pop("_revealed_gateway_token", None)
    return render(
        request,
        "settings/mcp.html",
        current_user=user,
        active_subsection="mcp",
        status=mcp_token.status(db),
        new_token=new_token,
        gateway=mcp_gateway.status(),
        gateway_tokens=mcp_gateway_tokens.list_tokens(db),
        new_gateway_token=new_gateway_token,
    )


@router.post("/mcp/rotate")
def rotate_mcp_token(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    full_key = mcp_token.rotate(db, actor_id=user.id)
    log.info("ui_mcp_token_rotated", extra={"by": user.username})
    _settings_event(
        request, user,
        category="api_key",
        event_type="api_key.mcp_rotated",
        target_type="api_key",
        target_label="MCP Server",
        message="Rotated the MCP server token",
    )
    # One-shot reveal on the next page load.
    request.session["_revealed_mcp_token"] = full_key
    flash(request, "MCP token rotated. The MCP server will use it on its next call.", "success")
    return RedirectResponse(url="/ui/settings/mcp", status_code=303)


@router.post("/mcp/clear")
def clear_mcp_token(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    existed = mcp_token.clear(db)
    if existed:
        log.info("ui_mcp_token_cleared", extra={"by": user.username})
        _settings_event(
            request, user,
            category="api_key",
            event_type="api_key.mcp_cleared",
            target_type="api_key",
            target_label="MCP Server",
            message="Cleared the MCP server token",
        )
        flash(request, "MCP token cleared and revoked.", "success")
    else:
        flash(request, "No MCP token was configured.", "warning")
    return RedirectResponse(url="/ui/settings/mcp", status_code=303)


@router.post("/mcp/gateway/tokens/new")
def create_gateway_token(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    label = name.strip()
    if not label:
        flash(request, "Give the token a name (e.g. the app or project it's for).", "warning")
        return RedirectResponse(url="/ui/settings/mcp", status_code=303)
    row, full = mcp_gateway_tokens.create(db, name=label, actor_id=user.id)
    log.info("ui_mcp_gateway_token_created", extra={"token_id": row.id, "by": user.username})
    _settings_event(
        request, user,
        category="api_key",
        event_type="api_key.mcp_gateway_created",
        target_type="mcp_gateway",
        target_id=row.id,
        target_label=row.name,
        message=f"Created MCP gateway token '{row.name}'",
        detail={"prefix": row.token_prefix},
    )
    request.session["_revealed_gateway_token"] = full
    flash(request, f"Gateway token '{row.name}' created. Copy it now — it won't be shown again.", "success")
    return RedirectResponse(url="/ui/settings/mcp", status_code=303)


@router.post("/mcp/gateway/tokens/{token_id}/revoke")
def revoke_gateway_token(
    token_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    row = mcp_gateway_tokens.revoke(db, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Gateway token not found.")
    log.info("ui_mcp_gateway_token_revoked", extra={"token_id": token_id, "by": user.username})
    _settings_event(
        request, user,
        category="api_key",
        event_type="api_key.mcp_gateway_revoked",
        target_type="mcp_gateway",
        target_id=row.id,
        target_label=row.name,
        message=f"Revoked MCP gateway token '{row.name}'",
    )
    flash(request, f"Gateway token '{row.name}' revoked — it stops working immediately.", "success")
    return RedirectResponse(url="/ui/settings/mcp", status_code=303)


@router.post("/mcp/gateway/tokens/{token_id}/delete")
def delete_gateway_token(
    token_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    name = mcp_gateway_tokens.delete(db, token_id)
    if name is None:
        raise HTTPException(status_code=404, detail="Gateway token not found.")
    log.info("ui_mcp_gateway_token_deleted", extra={"token_id": token_id, "by": user.username})
    _settings_event(
        request, user,
        category="api_key",
        event_type="api_key.mcp_gateway_deleted",
        target_type="mcp_gateway",
        target_id=token_id,
        target_label=name,
        message=f"Deleted MCP gateway token '{name}'",
    )
    flash(request, f"Gateway token '{name}' deleted.", "success")
    return RedirectResponse(url="/ui/settings/mcp", status_code=303)


@router.post("/mcp/allowed-hosts")
def save_allowed_hosts(
    request: Request,
    allowed_hosts: str = Form(""),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    hosts = mcp_gateway.set_allowed_hosts(allowed_hosts)
    log.info("ui_mcp_allowed_hosts_set", extra={"by": user.username, "count": len(hosts)})
    if hosts:
        flash(request, f"Allowed hosts saved ({len(hosts)}).", "success")
    else:
        flash(request, "Allowed hosts cleared — any host is accepted (bearer auth still required).", "success")
    return RedirectResponse(url="/ui/settings/mcp", status_code=303)


# ===========================================================================
# OAuth Clients
# ===========================================================================


@router.get("/oauth-clients")
def list_oauth_clients(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    clients = db.query(OAuthClient).order_by(OAuthClient.created_at.desc()).all()
    new_client = request.session.pop("_revealed_oauth_client", None)
    return render(
        request,
        "settings/oauth_clients.html",
        current_user=user,
        active_subsection="oauth_clients",
        clients=clients,
        new_client=new_client,
    )


@router.get("/oauth-clients/new")
def show_new_oauth_client(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request,
        "settings/oauth_client_new.html",
        current_user=user,
        active_subsection="oauth_clients",
    )


@router.post("/oauth-clients/new")
def create_oauth_client(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    client_id = generate_oauth_client_id()
    client_secret = generate_oauth_client_secret()
    client = OAuthClient(
        name=name.strip(),
        client_id=client_id,
        client_secret_hash=hash_token(client_secret),
        created_by_user_id=user.id,
    )
    db.add(client)
    db.commit()
    log.info(
        "ui_oauth_client_created",
        extra={"oauth_client_id": client.id, "client_id": client_id, "by": user.username},
    )
    _settings_event(
        request, user,
        category="oauth_client",
        event_type="oauth_client.created",
        target_type="oauth_client",
        target_id=client.id,
        target_label=client.name,
        message=f"Created OAuth client '{client.name}'",
        detail={"client_id": client_id},
    )
    request.session["_revealed_oauth_client"] = {
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return RedirectResponse(url="/ui/settings/oauth-clients", status_code=303)


@router.post("/oauth-clients/{client_pk}/revoke")
def revoke_oauth_client(
    client_pk: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    client = db.get(OAuthClient, client_pk)
    if client is None:
        raise HTTPException(status_code=404, detail="OAuth client not found.")
    if client.revoked_at is None:
        client.revoked_at = datetime.now(UTC)
        db.commit()
        log.info("ui_oauth_client_revoked", extra={"oauth_client_id": client_pk, "by": user.username})
        _settings_event(
            request, user,
            category="oauth_client",
            event_type="oauth_client.revoked",
            target_type="oauth_client",
            target_id=client.id,
            target_label=client.name,
            message=f"Revoked OAuth client '{client.name}'",
        )
        flash(request, f"Revoked OAuth client '{client.name}'.", "success")
    return RedirectResponse(url="/ui/settings/oauth-clients", status_code=303)


@router.post("/oauth-clients/{client_pk}/delete")
def delete_oauth_client(
    client_pk: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    client = db.get(OAuthClient, client_pk)
    if client is None:
        raise HTTPException(status_code=404, detail="OAuth client not found.")
    name = client.name
    db.delete(client)
    db.commit()
    log.info("ui_oauth_client_deleted", extra={"oauth_client_id": client_pk, "by": user.username})
    _settings_event(
        request, user,
        category="oauth_client",
        event_type="oauth_client.deleted",
        target_type="oauth_client",
        target_id=client_pk,
        target_label=name,
        message=f"Deleted OAuth client '{name}'",
    )
    flash(request, f"Deleted OAuth client '{name}'.", "success")
    return RedirectResponse(url="/ui/settings/oauth-clients", status_code=303)


# ===========================================================================
# Identity Providers (OIDC single sign-on)
# ===========================================================================


@router.get("/auth-providers")
def list_auth_providers(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    providers = db.query(AuthProvider).order_by(AuthProvider.created_at.desc()).all()
    # The redirect/callback URI each provider must have registered at the IdP.
    redirect_uris = {p.id: callback_url(request, p.slug) for p in providers}
    return render(
        request,
        "settings/auth_providers.html",
        current_user=user,
        active_subsection="auth_providers",
        providers=providers,
        redirect_uris=redirect_uris,
    )


@router.get("/auth-providers/new")
def show_new_auth_provider(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request,
        "settings/auth_provider_form.html",
        current_user=user,
        active_subsection="auth_providers",
        provider=None,
        form={"scopes": DEFAULT_SCOPES, "is_enabled": True, "default_user_tier": "standard"},
        # Show the callback pattern so the user can register it at the IdP.
        callback_base=callback_url(request, "SLUG").replace("/SLUG/", "/<slug>/"),
    )


def _validate_provider_form(
    slug: str, display_name: str, issuer_url: str, client_id: str
) -> str | None:
    """Return an error message if the form is invalid, else None."""
    if not _SLUG_RE.match(slug):
        return "Slug must contain only lowercase letters, numbers, and hyphens."
    if not display_name:
        return "Display name is required."
    if not issuer_url.startswith(("http://", "https://")):
        return "Issuer URL must start with http:// or https://."
    if not client_id:
        return "Client ID is required."
    return None


@router.post("/auth-providers/new")
def create_auth_provider(
    request: Request,
    display_name: str = Form(...),
    slug: str = Form(...),
    issuer_url: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(""),
    scopes: str = Form(DEFAULT_SCOPES),
    default_user_tier: str = Form("standard"),
    is_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    slug = slug.strip().lower()
    display_name = display_name.strip()
    issuer_url = issuer_url.strip()
    client_id = client_id.strip()
    scopes = scopes.strip() or DEFAULT_SCOPES
    default_user_tier = "external" if default_user_tier == "external" else "standard"

    form = {
        "display_name": display_name,
        "slug": slug,
        "issuer_url": issuer_url,
        "client_id": client_id,
        "scopes": scopes,
        "default_user_tier": default_user_tier,
        "is_enabled": bool(is_enabled),
    }

    error = _validate_provider_form(slug, display_name, issuer_url, client_id)
    if error:
        return render(
            request,
            "settings/auth_provider_form.html",
            current_user=user,
            active_subsection="auth_providers",
            provider=None,
            form=form,
            error=error,
            callback_base=callback_url(request, "SLUG").replace("/SLUG/", "/<slug>/"),
        )

    provider = AuthProvider(
        slug=slug,
        display_name=display_name,
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret_encrypted=encrypt_secret(client_secret) if client_secret else "",
        scopes=scopes,
        default_user_tier=default_user_tier,
        is_enabled=bool(is_enabled),
        created_by_user_id=user.id,
    )
    db.add(provider)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return render(
            request,
            "settings/auth_provider_form.html",
            current_user=user,
            active_subsection="auth_providers",
            provider=None,
            form=form,
            error=f"A provider with slug '{slug}' already exists.",
            callback_base=callback_url(request, "SLUG").replace("/SLUG/", "/<slug>/"),
        )
    log.info(
        "ui_auth_provider_created",
        extra={"provider_id": provider.id, "slug": slug, "by": user.username},
    )
    _settings_event(
        request, user,
        category="auth_provider",
        event_type="auth_provider.created",
        target_type="auth_provider",
        target_id=provider.id,
        target_label=display_name,
        message=f"Created identity provider '{display_name}'",
        detail={"slug": slug, "issuer_url": issuer_url},
    )
    flash(request, f"Identity provider '{display_name}' created.", "success")
    return RedirectResponse(url="/ui/settings/auth-providers", status_code=303)


@router.get("/auth-providers/{provider_id}/edit")
def show_edit_auth_provider(
    provider_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    provider = db.get(AuthProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Identity provider not found.")
    form = {
        "display_name": provider.display_name,
        "slug": provider.slug,
        "issuer_url": provider.issuer_url,
        "client_id": provider.client_id,
        "scopes": provider.scopes,
        "default_user_tier": provider.default_user_tier,
        "is_enabled": provider.is_enabled,
    }
    return render(
        request,
        "settings/auth_provider_form.html",
        current_user=user,
        active_subsection="auth_providers",
        provider=provider,
        form=form,
        redirect_uri=callback_url(request, provider.slug),
    )


@router.post("/auth-providers/{provider_id}/edit")
def update_auth_provider(
    provider_id: int,
    request: Request,
    display_name: str = Form(...),
    slug: str = Form(...),
    issuer_url: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(""),
    scopes: str = Form(DEFAULT_SCOPES),
    default_user_tier: str = Form("standard"),
    is_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    provider = db.get(AuthProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Identity provider not found.")

    slug = slug.strip().lower()
    display_name = display_name.strip()
    issuer_url = issuer_url.strip()
    client_id = client_id.strip()
    scopes = scopes.strip() or DEFAULT_SCOPES
    default_user_tier = "external" if default_user_tier == "external" else "standard"

    form = {
        "display_name": display_name,
        "slug": slug,
        "issuer_url": issuer_url,
        "client_id": client_id,
        "scopes": scopes,
        "default_user_tier": default_user_tier,
        "is_enabled": bool(is_enabled),
    }

    error = _validate_provider_form(slug, display_name, issuer_url, client_id)
    if error:
        return render(
            request,
            "settings/auth_provider_form.html",
            current_user=user,
            active_subsection="auth_providers",
            provider=provider,
            form=form,
            error=error,
            redirect_uri=callback_url(request, provider.slug),
        )

    provider.slug = slug
    provider.display_name = display_name
    provider.issuer_url = issuer_url
    provider.client_id = client_id
    provider.scopes = scopes
    provider.default_user_tier = default_user_tier
    provider.is_enabled = bool(is_enabled)
    # Only replace the stored secret when a new one is supplied.
    if client_secret:
        provider.client_secret_encrypted = encrypt_secret(client_secret)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return render(
            request,
            "settings/auth_provider_form.html",
            current_user=user,
            active_subsection="auth_providers",
            provider=provider,
            form=form,
            error=f"A provider with slug '{slug}' already exists.",
            redirect_uri=callback_url(request, provider.slug),
        )
    log.info(
        "ui_auth_provider_updated",
        extra={"provider_id": provider.id, "slug": slug, "by": user.username},
    )
    _settings_event(
        request, user,
        category="auth_provider",
        event_type="auth_provider.updated",
        target_type="auth_provider",
        target_id=provider.id,
        target_label=display_name,
        message=f"Updated identity provider '{display_name}'",
        detail={"slug": slug, "secret_rotated": bool(client_secret)},
    )
    flash(request, f"Identity provider '{display_name}' updated.", "success")
    return RedirectResponse(url="/ui/settings/auth-providers", status_code=303)


@router.post("/auth-providers/{provider_id}/toggle")
def toggle_auth_provider(
    provider_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    provider = db.get(AuthProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Identity provider not found.")
    provider.is_enabled = not provider.is_enabled
    db.commit()
    state = "enabled" if provider.is_enabled else "disabled"
    log.info(
        "ui_auth_provider_toggled",
        extra={"provider_id": provider_id, "state": state, "by": user.username},
    )
    _settings_event(
        request, user,
        category="auth_provider",
        event_type="auth_provider.toggled",
        target_type="auth_provider",
        target_id=provider.id,
        target_label=provider.display_name,
        message=f"{state.capitalize()} identity provider '{provider.display_name}'",
        detail={"state": state},
    )
    flash(request, f"Identity provider '{provider.display_name}' {state}.", "success")
    return RedirectResponse(url="/ui/settings/auth-providers", status_code=303)


@router.post("/auth-providers/{provider_id}/delete")
def delete_auth_provider(
    provider_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    provider = db.get(AuthProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Identity provider not found.")
    name = provider.display_name
    # Remove identity links explicitly — SQLite FK cascade isn't reliably on.
    db.query(UserIdentity).filter(UserIdentity.provider_id == provider.id).delete()
    db.delete(provider)
    db.commit()
    log.info(
        "ui_auth_provider_deleted",
        extra={"provider_id": provider_id, "by": user.username},
    )
    _settings_event(
        request, user,
        category="auth_provider",
        event_type="auth_provider.deleted",
        target_type="auth_provider",
        target_id=provider_id,
        target_label=name,
        message=f"Deleted identity provider '{name}'",
    )
    flash(request, f"Deleted identity provider '{name}'.", "success")
    return RedirectResponse(url="/ui/settings/auth-providers", status_code=303)


# ===========================================================================
# Branding
# ===========================================================================


def _get_or_create_branding(db: Session) -> AppBranding:
    """Return the singleton branding row, creating it from defaults if absent."""
    branding = db.get(AppBranding, BRANDING_ID)
    if branding is None:
        branding = AppBranding(
            id=BRANDING_ID,
            brand_name=branding_service.DEFAULT_NAME,
            brand_tagline=branding_service.DEFAULT_TAGLINE,
            brand_color="",
            icon_key=branding_service.DEFAULT_ICON,
        )
        db.add(branding)
        db.commit()
    return branding


@router.get("/branding")
def show_branding(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    branding = _get_or_create_branding(db)
    return render(
        request,
        "settings/branding.html",
        current_user=user,
        active_subsection="branding",
        form={
            "brand_name": branding.brand_name,
            "brand_tagline": branding.brand_tagline,
            "brand_color": branding.brand_color or branding_service.DEFAULT_COLOR,
            "icon_key": branding.icon_key,
        },
        icon_presets=branding_service.ICON_PRESETS,
        default_color=branding_service.DEFAULT_COLOR,
        has_deck_template=report_template.has_template(),
        has_deck_logo=report_template.has_logo(),
    )


@router.post("/branding")
def update_branding(
    request: Request,
    brand_name: str = Form(...),
    brand_tagline: str = Form(""),
    brand_color: str = Form(""),
    icon_key: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    brand_name = brand_name.strip()
    brand_tagline = brand_tagline.strip()
    brand_color = brand_color.strip()
    icon_key = icon_key.strip()

    form = {
        "brand_name": brand_name,
        "brand_tagline": brand_tagline,
        "brand_color": brand_color or branding_service.DEFAULT_COLOR,
        "icon_key": icon_key,
    }

    def _reject(message: str) -> Response:
        return render(
            request,
            "settings/branding.html",
            current_user=user,
            active_subsection="branding",
            form=form,
            icon_presets=branding_service.ICON_PRESETS,
            default_color=branding_service.DEFAULT_COLOR,
            has_deck_template=report_template.has_template(),
        has_deck_logo=report_template.has_logo(),
            error=message,
        )

    if not brand_name:
        return _reject("Brand name is required.")
    if len(brand_name) > 100:
        return _reject("Brand name must be 100 characters or fewer.")
    if len(brand_tagline) > 100:
        return _reject("Tagline must be 100 characters or fewer.")
    if brand_color and not _HEX_COLOR_RE.match(brand_color):
        return _reject("Color must be a hex value like #1e293b.")
    if icon_key not in branding_service.ICON_PRESETS:
        return _reject("Please choose one of the available icons.")

    # Store empty when the color matches the theme default so the app keeps
    # following the theme rather than pinning to a now-stale hex.
    if brand_color.lower() == branding_service.DEFAULT_COLOR.lower():
        brand_color = ""

    branding = _get_or_create_branding(db)
    branding.brand_name = brand_name
    branding.brand_tagline = brand_tagline
    branding.brand_color = brand_color
    branding.icon_key = icon_key
    db.commit()
    branding_service.invalidate()

    log.info(
        "ui_branding_updated",
        extra={"icon_key": icon_key, "has_color": bool(brand_color), "by": user.username},
    )
    _settings_event(
        request, user,
        category="branding",
        event_type="branding.updated",
        target_type="branding",
        target_label=brand_name,
        message=f"Updated branding to '{brand_name}'",
        detail={"icon_key": icon_key, "has_color": bool(brand_color)},
    )
    flash(request, "Branding updated.", "success")
    return RedirectResponse(url="/ui/settings/branding", status_code=303)


@router.post("/branding/deck-template")
async def upload_deck_template(
    request: Request,
    template_file: UploadFile = File(...),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Store an admin-supplied .pptx used as the readout deck's base template."""
    if not template_file or not template_file.filename:
        flash(request, "Choose a .pptx file to upload.", "error")
        return RedirectResponse(url="/ui/settings/branding", status_code=303)
    data = await template_file.read()
    try:
        report_template.save_template(data)
    except report_template.TemplateError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url="/ui/settings/branding", status_code=303)

    _settings_event(
        request, user,
        category="branding",
        event_type="branding.deck_template_uploaded",
        target_type="branding",
        target_label=template_file.filename,
        message=f"Uploaded readout deck template ({template_file.filename})",
        detail={"bytes": len(data)},
    )
    flash(request, "Deck template uploaded.", "success")
    return RedirectResponse(url="/ui/settings/branding", status_code=303)


@router.post("/branding/deck-template/delete")
def delete_deck_template(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Remove the admin-supplied readout deck template."""
    removed = report_template.delete_template()
    if removed:
        _settings_event(
            request, user,
            category="branding",
            event_type="branding.deck_template_removed",
            target_type="branding",
            message="Removed the readout deck template",
        )
        flash(request, "Deck template removed.", "success")
    else:
        flash(request, "No deck template to remove.", "info")
    return RedirectResponse(url="/ui/settings/branding", status_code=303)


@router.post("/branding/deck-logo")
async def upload_deck_logo(
    request: Request,
    logo_file: UploadFile = File(...),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Store an image stamped on every readout deck slide."""
    if not logo_file or not logo_file.filename:
        flash(request, "Choose an image to upload.", "error")
        return RedirectResponse(url="/ui/settings/branding", status_code=303)
    data = await logo_file.read()
    try:
        report_template.save_logo(data)
    except report_template.TemplateError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url="/ui/settings/branding", status_code=303)

    _settings_event(
        request, user,
        category="branding",
        event_type="branding.deck_logo_uploaded",
        target_type="branding",
        target_label=logo_file.filename,
        message=f"Uploaded readout deck logo ({logo_file.filename})",
        detail={"bytes": len(data)},
    )
    flash(request, "Deck logo uploaded.", "success")
    return RedirectResponse(url="/ui/settings/branding", status_code=303)


@router.post("/branding/deck-logo/delete")
def delete_deck_logo(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Remove the readout deck logo."""
    removed = report_template.delete_logo()
    if removed:
        _settings_event(
            request, user,
            category="branding",
            event_type="branding.deck_logo_removed",
            target_type="branding",
            message="Removed the readout deck logo",
        )
        flash(request, "Deck logo removed.", "success")
    else:
        flash(request, "No deck logo to remove.", "info")
    return RedirectResponse(url="/ui/settings/branding", status_code=303)


@router.get("/branding/deck-logo/preview")
def preview_deck_logo(
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Serve the stored deck logo for the Branding page preview."""
    path = report_template.logo_path_if_present()
    if path is None:
        raise HTTPException(status_code=404, detail="No logo uploaded.")
    return FileResponse(path, media_type="image/png")


# ===========================================================================
# System settings
# ===========================================================================

# Upper bound on the retention window (~10 years) — a sanity guard, not a policy.
_MAX_RETENTION_DAYS = 3650


@router.get("/system")
def show_system(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    config = system_config.get_config(db)
    return render(
        request,
        "settings/system.html",
        current_user=user,
        active_subsection="system",
        form={
            "audit_retention_days": config.audit_retention_days,
            "tasks_enabled": config.tasks_enabled,
            "external_user_ttl_days": config.external_user_ttl_days,
            "region_enforcement_enabled": config.region_enforcement_enabled,
        },
    )


@router.post("/system")
def update_system(
    request: Request,
    audit_retention_days: int = Form(...),
    external_user_ttl_days: int = Form(60),
    tasks_enabled: str | None = Form(None),
    region_enforcement_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    def _reject(message: str) -> Response:
        return render(
            request,
            "settings/system.html",
            current_user=user,
            active_subsection="system",
            form={
                "audit_retention_days": audit_retention_days,
                "tasks_enabled": bool(tasks_enabled),
                "external_user_ttl_days": external_user_ttl_days,
                "region_enforcement_enabled": bool(region_enforcement_enabled),
            },
            error=message,
        )

    if audit_retention_days < 0 or audit_retention_days > _MAX_RETENTION_DAYS:
        return _reject(
            f"Retention must be between 0 and {_MAX_RETENTION_DAYS} days "
            "(0 keeps events forever)."
        )
    if external_user_ttl_days < 0 or external_user_ttl_days > _MAX_RETENTION_DAYS:
        return _reject(
            f"External user lifetime must be between 0 and {_MAX_RETENTION_DAYS} days "
            "(0 means never expire)."
        )

    config = system_config.get_config(db)
    previous = config.audit_retention_days
    tasks_was = config.tasks_enabled
    tasks_now = bool(tasks_enabled)
    ttl_was = config.external_user_ttl_days
    system_config.set_retention_days(db, audit_retention_days)
    if external_user_ttl_days != ttl_was:
        system_config.set_external_user_ttl_days(db, external_user_ttl_days)
        _settings_event(
            request, user,
            category="system",
            event_type="system.settings.updated",
            target_type="app_config",
            message=f"Set external user lifetime to {external_user_ttl_days} day(s)",
            detail={"external_user_ttl_days": external_user_ttl_days},
        )
    if tasks_now != tasks_was:
        system_config.set_tasks_enabled(db, tasks_now)
        _settings_event(
            request, user,
            category="system",
            event_type="system.settings.updated",
            target_type="app_config",
            message=f"{'Enabled' if tasks_now else 'Disabled'} the Task Manager module",
            detail={"tasks_enabled": tasks_now},
        )

    region_was = config.region_enforcement_enabled
    region_now = bool(region_enforcement_enabled)
    if region_now != region_was:
        system_config.set_region_enforcement_enabled(db, region_now)
        _settings_event(
            request, user,
            category="system",
            event_type="system.settings.updated",
            target_type="app_config",
            message=f"{'Enabled' if region_now else 'Disabled'} region-based access enforcement",
            detail={"region_enforcement_enabled": region_now},
        )

    # Apply the new window immediately so lowering it takes effect now rather
    # than waiting for the next daily sweep.
    pruned = prune_old_events(audit_retention_days)

    _settings_event(
        request, user,
        category="system",
        event_type="system.settings.updated",
        target_type="app_config",
        message=f"Set audit retention to {audit_retention_days} day(s)",
        detail={
            "audit_retention_days": audit_retention_days,
            "previous": previous,
            "events_pruned": pruned,
        },
    )

    msg = f"Audit retention set to {audit_retention_days} day(s)."
    if audit_retention_days == 0:
        msg = "Audit retention disabled — events are now kept forever."
    if pruned:
        msg += f" Removed {pruned} event(s) older than the new window."
    flash(request, msg, "success")
    return RedirectResponse(url="/ui/settings/system", status_code=303)


@router.post("/system/backfill-regions")
def backfill_regions(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Derive every project's region from its SE; orphans → Unassigned.

    Safe to run repeatedly. Intended flow: define regions → assign SE regions
    (Users / Bulk Regions) → run this → verify → enable enforcement.
    """
    summary = backfill_project_regions(db)
    db.commit()
    _settings_event(
        request, user,
        category="system",
        event_type="system.regions.backfilled",
        target_type="project",
        target_label=f"{summary['total']} projects",
        message=(
            f"Backfilled project regions: {summary['derived']} from SE, "
            f"{summary['unassigned']} to Unassigned"
        ),
        detail=summary,
    )
    flash(
        request,
        f"Backfill complete: {summary['total']} projects — "
        f"{summary['derived']} set from their SE's region, "
        f"{summary['unassigned']} parked in Unassigned, "
        f"{summary['unchanged']} unchanged.",
        "success",
    )
    return RedirectResponse(url="/ui/settings/system", status_code=303)


# ===========================================================================
# Google Tasks integration (admin OAuth client config)
# ===========================================================================


@router.get("/google-tasks")
def show_google_tasks(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    from app.services import google_oauth
    from app.ui.google_routes import callback_uri

    config = google_oauth.get_config(db)
    return render(
        request,
        "settings/google_tasks.html",
        current_user=user,
        active_subsection="google_tasks",
        form={
            "client_id": config.client_id or "",
            "is_enabled": config.is_enabled,
            "has_secret": bool(config.client_secret_encrypted),
        },
        redirect_uri=callback_uri(request),
    )


@router.post("/google-tasks")
def update_google_tasks(
    request: Request,
    client_id: str = Form(""),
    client_secret: str = Form(""),
    is_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    from app.services import google_oauth

    client_id = client_id.strip()
    enabled = bool(is_enabled)
    config = google_oauth.get_config(db)

    # Can't enable without credentials present (existing secret counts).
    has_secret = bool(config.client_secret_encrypted) or bool(client_secret.strip())
    if enabled and not (client_id and has_secret):
        flash(request, "Add the client ID and secret before enabling sync.", "error")
        return RedirectResponse(url="/ui/settings/google-tasks", status_code=303)

    google_oauth.set_config(
        db,
        client_id=client_id,
        client_secret=client_secret.strip() or None,
        is_enabled=enabled,
    )
    _settings_event(
        request, user,
        category="system",
        event_type="google_tasks.settings.updated",
        target_type="google_tasks_config",
        message=f"{'Enabled' if enabled else 'Disabled'} Google Tasks sync",
        detail={"is_enabled": enabled, "secret_rotated": bool(client_secret.strip())},
    )
    flash(request, "Google Tasks settings saved.", "success")
    return RedirectResponse(url="/ui/settings/google-tasks", status_code=303)


# ===========================================================================
# Email (SMTP)
# ===========================================================================


@router.get("/email")
def show_email(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    from app.services import email as email_service

    config = email_service.get_config(db)
    return render(
        request,
        "settings/email.html",
        current_user=user,
        active_subsection="email",
        form={
            "host": config.host or "",
            "port": config.port,
            "security": config.security,
            "username": config.username or "",
            "from_email": config.from_email or "",
            "from_name": config.from_name or "",
            "is_enabled": config.is_enabled,
            "has_password": config.has_password,
        },
    )


@router.post("/email")
def update_email(
    request: Request,
    host: str = Form(""),
    port: str = Form("587"),
    security: str = Form("starttls"),
    username: str = Form(""),
    password: str = Form(""),
    from_email: str = Form(""),
    from_name: str = Form(""),
    is_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    from app.models.smtp_config import VALID_SECURITY
    from app.services import email as email_service

    host = host.strip()
    from_email = from_email.strip()
    enabled = bool(is_enabled)
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        port_num = 587
    if security not in VALID_SECURITY:
        security = "starttls"

    # Can't enable without the minimum needed to send.
    if enabled and not (host and from_email):
        flash(request, "Add the SMTP host and a from address before enabling email.", "error")
        return RedirectResponse(url="/ui/settings/email", status_code=303)

    email_service.set_config(
        db,
        host=host,
        port=port_num,
        security=security,
        username=username,
        password=password.strip() or None,
        from_email=from_email,
        from_name=from_name,
        is_enabled=enabled,
    )
    _settings_event(
        request, user,
        category="system",
        event_type="email.settings.updated",
        target_type="smtp_config",
        message=f"{'Enabled' if enabled else 'Disabled'} outbound email",
        detail={
            "is_enabled": enabled, "host": host, "security": security,
            "password_rotated": bool(password.strip()),
        },
    )
    flash(request, "Email settings saved.", "success")
    return RedirectResponse(url="/ui/settings/email", status_code=303)


@router.post("/email/test")
def send_test_email(
    request: Request,
    test_recipient: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    from app.services import email as email_service

    recipient = test_recipient.strip()
    if not recipient:
        flash(request, "Enter an address to send the test email to.", "error")
        return RedirectResponse(url="/ui/settings/email", status_code=303)

    app_name = get_settings().app_name
    try:
        email_service.send_test_email(db, to=recipient, app_name=app_name)
    except email_service.EmailError as exc:
        record_event(
            category="system", event_type="email.test", outcome="failure",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="smtp_config",
            message=f"SMTP test to {recipient} failed",
            detail={"surface": "ui", "recipient": recipient, "error": str(exc)},
            request=request,
        )
        flash(request, f"Test email failed: {exc}", "error")
        return RedirectResponse(url="/ui/settings/email", status_code=303)

    _settings_event(
        request, user,
        category="system",
        event_type="email.test",
        target_type="smtp_config",
        message=f"Sent SMTP test email to {recipient}",
        detail={"recipient": recipient},
    )
    flash(request, f"Test email sent to {recipient}.", "success")
    return RedirectResponse(url="/ui/settings/email", status_code=303)


# ===========================================================================
# Reset
# ===========================================================================


@router.get("/reset")
def show_reset(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request,
        "settings/reset.html",
        current_user=user,
        active_subsection="reset",
    )


@router.post("/reset")
def do_reset(
    request: Request,
    reset_sample_data: str | None = Form(None),
    reset_contact_roles: str | None = Form(None),
    reset_project_statuses: str | None = Form(None),
    reset_project_types: str | None = Form(None),
    reset_feature_types: str | None = Form(None),
    reset_use_case_statuses: str | None = Form(None),
    reset_use_case_library: str | None = Form(None),
    reset_audit_events: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    actions: list[str] = []

    do_sample = bool(reset_sample_data)
    do_roles = bool(reset_contact_roles)
    do_pstatuses = bool(reset_project_statuses)
    do_ptypes = bool(reset_project_types)
    do_features = bool(reset_feature_types)
    do_ucstatuses = bool(reset_use_case_statuses)
    do_library = bool(reset_use_case_library)
    do_audit = bool(reset_audit_events)

    do_lookups = do_roles or do_pstatuses or do_ptypes or do_features or do_ucstatuses

    # Lookups are referenced by customers/projects/use cases. Resetting a lookup
    # while that data still exists would violate a foreign key, so require the
    # sample-data wipe alongside any lookup reset.
    if do_lookups and not do_sample:
        flash(
            request,
            "Resetting lookups also requires resetting projects & customers "
            "(they reference these lookups). Select that box too.",
            "error",
        )
        return RedirectResponse(url="/ui/settings/reset", status_code=303)

    if not any(
        (do_sample, do_lookups, do_library, do_audit)
    ):
        flash(request, "Nothing was selected to reset.", "warning")
        return RedirectResponse(url="/ui/settings/reset", status_code=303)

    # Order matters: wipe projects/customers first, then the lookups they reference.
    try:
        if do_sample:
            n = seed_data.reset_sample_data(db, reseed=False)
            actions.append(f"customers & projects ({n})")

        if do_roles:
            n = seed_data.reset_contact_roles(db)
            actions.append(f"contact roles ({n})")

        if do_pstatuses:
            n = seed_data.reset_project_statuses(db)
            actions.append(f"project statuses ({n})")

        if do_ptypes:
            n = seed_data.reset_project_types(db)
            actions.append(f"project types ({n})")

        if do_features:
            n = seed_data.reset_feature_types(db)
            actions.append(f"feature types ({n})")

        if do_ucstatuses:
            n = seed_data.reset_use_case_statuses(db)
            actions.append(f"use-case statuses ({n})")

        if do_library:
            n = seed_data.reset_use_case_library(db)
            actions.append(f"use-case library ({n})")

        # Re-seed sample customers/projects last so their lookup FKs resolve.
        if do_sample:
            seed_data.seed_sample_data(db)
            db.commit()

        # Clearing the audit log is independent of the other tables (no FKs).
        if do_audit:
            n = db.query(AuditEvent).delete()
            db.commit()
            actions.append(f"audit events ({n})")
    except Exception as exc:
        db.rollback()
        log.exception("ui_reset_failed", extra={"by": user.username})
        flash(request, f"Reset failed: {exc}", "error")
        return RedirectResponse(url="/ui/settings/reset", status_code=303)

    log.warning("ui_reset_completed", extra={"by": user.username, "actions": actions})
    # Record the reset itself — written after the wipe so it survives an audit clear
    # and documents that the log was cleared.
    _settings_event(
        request, user,
        category="system",
        event_type="system.data_reset",
        message="Reset demo data: " + ", ".join(actions),
        detail={"actions": actions},
    )
    flash(request, "Reset complete: " + ", ".join(actions) + ".", "success")
    return RedirectResponse(url="/ui/settings/reset", status_code=303)


# ===========================================================================
# Demo data (local demo/testing instances only)
# ===========================================================================

# Typed-confirmation phrase for the destructive "remove" action, validated
# server-side in addition to the client-side button gate in app.js.
_DEMO_REMOVE_PHRASE = "REMOVE"


def _require_demo_tools() -> None:
    """404 the demo-data routes unless explicitly enabled. Keeps the feature
    absent on production, where POCT_ENABLE_DEMO_TOOLS is unset."""
    if not get_settings().enable_demo_tools:
        raise HTTPException(status_code=404)


def _demo_present_count(db: Session) -> int:
    """How many of the demo customers currently exist (so the page can show
    whether demo data is loaded)."""
    return (
        db.query(Customer)
        .filter(Customer.name.in_(demo_data.DEMO_CUSTOMER_NAMES))
        .count()
    )


@router.get("/demo-data")
def show_demo_data(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_admin_ui),
) -> Response:
    _require_demo_tools()
    return render(
        request,
        "settings/demo_data.html",
        current_user=user,
        active_subsection="demo_data",
        present_count=_demo_present_count(db),
        total_customers=len(demo_data.DEMO_CUSTOMER_NAMES),
        remove_phrase=_DEMO_REMOVE_PHRASE,
        demo_user_password=demo_data.DEMO_USER_PASSWORD,
    )


@router.post("/demo-data")
def do_demo_data(
    request: Request,
    action: str = Form(...),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_admin_ui),
) -> Response:
    _require_demo_tools()

    try:
        if action == "load":
            summary = demo_data.seed_demo_data(db)
            created = summary["customers"]
            if created:
                flash(
                    request,
                    f"Loaded demo data: {created} customer(s), "
                    f"{summary['projects']} project(s), "
                    f"{summary['use_cases']} use case(s).",
                    "success",
                )
            else:
                flash(
                    request,
                    "Demo data was already present — nothing to add.",
                    "info",
                )
            _settings_event(
                request, user,
                category="system",
                event_type="system.demo_data_loaded",
                message="Loaded demo data via UI",
                detail=dict(summary),
            )
        elif action == "remove":
            if confirm.strip() != _DEMO_REMOVE_PHRASE:
                flash(
                    request,
                    f"Type {_DEMO_REMOVE_PHRASE} to confirm removing demo data.",
                    "error",
                )
                return RedirectResponse(url="/ui/settings/demo-data", status_code=303)
            removed = demo_data.purge_demo_data(db)
            flash(
                request,
                f"Removed demo data: {removed['customers']} customer(s), "
                f"{removed['projects']} project(s), "
                f"{removed['engineers']} demo engineer account(s).",
                "success",
            )
            _settings_event(
                request, user,
                category="system",
                event_type="system.demo_data_removed",
                message="Removed demo data via UI",
                detail=dict(removed),
            )
        else:
            flash(request, "Unknown action.", "error")
    except Exception as exc:  # noqa: BLE001 — surface any failure to the admin
        db.rollback()
        log.exception("ui_demo_data_failed", extra={"by": user.username, "action": action})
        flash(request, f"Demo data action failed: {exc}", "error")

    return RedirectResponse(url="/ui/settings/demo-data", status_code=303)


# ===========================================================================
# Backups
# ===========================================================================

# Cap restore uploads defensively. Backups can be large (they bundle uploaded
# files), so this is generous; it only guards against absurd inputs.
_MAX_RESTORE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def _backup_row(run: BackupRun) -> dict:
    """Shape a BackupRun for the template (parse stored counts JSON)."""
    counts = {}
    if run.counts_json:
        try:
            counts = json.loads(run.counts_json)
        except (ValueError, TypeError):
            counts = {}
    return {"run": run, "counts": counts}


@router.get("/backups")
def backups_page(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    runs = [_backup_row(r) for r in backup_service.list_runs(db)]
    return render(
        request,
        "settings/backups.html",
        current_user=user,
        active_subsection="backups",
        backups=runs,
        retention=get_settings().backup_retention_count,
        pending_restore=backup_service.pending_restore_info(),
    )


@router.post("/backups/create")
def create_backup(
    request: Request,
    passphrase: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    phrase = passphrase.strip() or None
    try:
        run = backup_service.create_backup(db, created_by=user.username, passphrase=phrase)
    except Exception as exc:
        log.exception("ui_backup_failed", extra={"by": user.username})
        record_event(
            category="system", event_type="backup.failed", outcome="failure",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="backup",
            message="Backup creation failed",
            detail={"surface": "ui", "encrypted": bool(phrase), "error": str(exc)},
            request=request,
        )
        flash(request, f"Backup failed: {exc}", "error")
        return RedirectResponse(url="/ui/settings/backups", status_code=303)
    _settings_event(
        request, user,
        category="system",
        event_type="backup.created",
        target_type="backup", target_id=run.id, target_label=run.filename,
        message=f"Created backup '{run.filename}'",
        detail={"encrypted": run.encrypted, "size_bytes": run.size_bytes},
    )
    flash(
        request,
        "Backup created" + (" (encrypted)." if phrase else ".") + " Download it below.",
        "success",
    )
    return RedirectResponse(url="/ui/settings/backups", status_code=303)


@router.get("/backups/{run_id}/download")
def download_backup(
    run_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    run = db.get(BackupRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    path = backup_service.archive_path(run)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Backup file is missing.")
    _settings_event(
        request, user,
        category="system",
        event_type="backup.downloaded",
        target_type="backup", target_id=run.id, target_label=run.filename,
        message=f"Downloaded backup '{run.filename}'",
    )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=run.filename,
        content_disposition_type="attachment",
    )


@router.post("/backups/{run_id}/delete")
def delete_backup(
    run_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    run = db.get(BackupRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    label = run.filename
    backup_service.delete_run(db, run)
    _settings_event(
        request, user,
        category="system",
        event_type="backup.deleted",
        target_type="backup", target_id=run_id, target_label=label,
        message=f"Deleted backup '{label}'",
    )
    flash(request, "Backup deleted.", "success")
    return RedirectResponse(url="/ui/settings/backups", status_code=303)


@router.post("/backups/restore")
async def restore_backup(
    request: Request,
    backup_file: UploadFile = File(...),
    passphrase: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    if confirm.strip() != "RESTORE":
        flash(request, "Type RESTORE to confirm before restoring.", "error")
        return RedirectResponse(url="/ui/settings/backups", status_code=303)
    if not backup_file or not backup_file.filename:
        flash(request, "Choose a backup file to restore.", "error")
        return RedirectResponse(url="/ui/settings/backups", status_code=303)

    # Buffer the upload to a temp file so the validator can open it as a zip.
    settings = get_settings()
    settings.ensure_data_dir()
    tmp = settings.data_dir / f".restore-upload-{secrets_token()}.zip"
    try:
        size = 0
        with tmp.open("wb") as out:
            while chunk := await backup_file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_RESTORE_BYTES:
                    raise backup_service.BackupError("Upload is too large.")
                out.write(chunk)
        manifest = backup_service.stage_restore(tmp, passphrase.strip() or None)
    except backup_service.BackupError as exc:
        record_event(
            category="system", event_type="restore.failed", outcome="failure",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="backup", target_label=backup_file.filename,
            message=f"Restore rejected ({backup_file.filename})",
            detail={"surface": "ui", "reason": "validation", "error": str(exc)},
            request=request,
        )
        flash(request, f"Restore rejected: {exc}", "error")
        return RedirectResponse(url="/ui/settings/backups", status_code=303)
    except Exception as exc:
        log.exception("ui_restore_stage_failed", extra={"by": user.username})
        record_event(
            category="system", event_type="restore.failed", outcome="failure",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="backup", target_label=backup_file.filename,
            message=f"Restore failed ({backup_file.filename})",
            detail={"surface": "ui", "reason": "unexpected", "error": str(exc)},
            request=request,
        )
        flash(request, f"Restore failed: {exc}", "error")
        return RedirectResponse(url="/ui/settings/backups", status_code=303)
    finally:
        tmp.unlink(missing_ok=True)

    _settings_event(
        request, user,
        category="system",
        event_type="restore.staged",
        message=f"Staged a restore from a backup ({backup_file.filename})",
        detail={"manifest": {k: manifest.get(k) for k in ("app_version", "created_at")}},
    )
    flash(
        request,
        "Restore staged and verified. Restart the app to apply it — a safety "
        "backup of the current data will be taken automatically first.",
        "success",
    )
    return RedirectResponse(url="/ui/settings/backups", status_code=303)


@router.post("/backups/restore/cancel")
def cancel_restore(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    if backup_service.cancel_pending_restore():
        _settings_event(
            request, user,
            category="system",
            event_type="restore.cancelled",
            message="Cancelled a staged restore",
        )
        flash(request, "Staged restore cancelled.", "success")
    return RedirectResponse(url="/ui/settings/backups", status_code=303)


@router.post("/backups/restart")
def restart_app(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Trigger a process exit so a supervisor (Docker `restart: unless-stopped`,
    systemd, etc.) starts a fresh process — which applies any staged restore.

    No-op-safe if unsupervised: the process simply exits. We send the signal a
    beat after responding so this redirect still reaches the browser.
    """
    import os
    import signal
    import threading

    _settings_event(
        request, user,
        category="system",
        event_type="app.restart_requested",
        message="Requested an app restart from the UI",
    )
    threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    flash(
        request,
        "Restart requested. The app will be back shortly if it runs under a "
        "process supervisor; refresh in a few seconds.",
        "info",
    )
    return RedirectResponse(url="/ui/settings/backups", status_code=303)


def secrets_token() -> str:
    """Short random token for temp upload filenames."""
    import secrets

    return secrets.token_hex(8)


# ===========================================================================
# AI Assistant providers (Anthropic, etc.) — power AI features like summaries
# ===========================================================================


def _ai_form_context() -> dict:
    """Provider choices + suggested models for the add/edit form."""
    return {
        "provider_choices": [
            {"key": s.key, "label": s.label, "implemented": s.implemented}
            for s in PROVIDERS.values()
        ],
        # provider key -> {default_model, suggested_models, key_help}
        "provider_meta": json.dumps(
            {
                s.key: {
                    "default_model": s.default_model,
                    "suggested_models": s.suggested_models,
                    "key_help": s.key_help,
                }
                for s in PROVIDERS.values()
            }
        ),
    }


@router.get("/ai")
def list_ai_providers(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    providers = db.query(AIProvider).order_by(AIProvider.id).all()
    return render(
        request, "settings/ai_providers.html", current_user=user,
        active_subsection="ai", providers=providers,
    )


@router.get("/ai/new")
def show_new_ai_provider(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request, "settings/ai_provider_form.html", current_user=user,
        active_subsection="ai", provider=None,
        form={"provider": "anthropic", "model": "claude-opus-4-8", "is_enabled": True},
        **_ai_form_context(),
    )


def _clean_ai_form(provider: str, display_name: str, model: str) -> tuple[str, str | None]:
    """Validate provider+model. Returns (error|"", normalized_display_name)."""
    spec = get_provider_spec(provider)
    if spec is None or not spec.implemented:
        return "Choose a supported provider.", None
    if not model.strip():
        return "A model id is required.", None
    return "", (display_name.strip() or spec.label)


@router.post("/ai/new")
def create_ai_provider(
    request: Request,
    provider: str = Form(...),
    display_name: str = Form(""),
    model: str = Form(...),
    api_key: str = Form(""),
    is_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    provider = provider.strip()
    model = model.strip()
    error, name = _clean_ai_form(provider, display_name, model)
    form = {
        "provider": provider, "display_name": display_name,
        "model": model, "is_enabled": bool(is_enabled),
    }
    if error or not api_key.strip():
        error = error or "An API key is required."
        return render(
            request, "settings/ai_provider_form.html", current_user=user,
            active_subsection="ai", provider=None, form=form, error=error,
            **_ai_form_context(),
        )
    # First provider becomes the default automatically.
    is_first = db.query(AIProvider).count() == 0
    row = AIProvider(
        provider=provider,
        display_name=name,
        model=model,
        api_key_encrypted=encrypt_secret(api_key.strip()),
        is_enabled=bool(is_enabled),
        is_default=is_first,
        created_by_user_id=user.id,
    )
    db.add(row)
    db.commit()
    _settings_event(
        request, user, category="ai_provider", event_type="ai_provider.created",
        target_type="ai_provider", target_id=row.id, target_label=row.display_name,
        message=f"Added AI provider '{row.display_name}' ({provider}/{model})",
        detail={"provider": provider, "model": model},
    )
    flash(request, f"AI provider '{row.display_name}' added.", "success")
    return RedirectResponse(url="/ui/settings/ai", status_code=303)


@router.get("/ai/{provider_id}/edit")
def show_edit_ai_provider(
    provider_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    row = db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AI provider not found.")
    form = {
        "provider": row.provider, "display_name": row.display_name,
        "model": row.model, "is_enabled": row.is_enabled,
    }
    return render(
        request, "settings/ai_provider_form.html", current_user=user,
        active_subsection="ai", provider=row, form=form, **_ai_form_context(),
    )


@router.post("/ai/{provider_id}/edit")
def update_ai_provider(
    provider_id: int,
    request: Request,
    provider: str = Form(...),
    display_name: str = Form(""),
    model: str = Form(...),
    api_key: str = Form(""),
    is_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    row = db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AI provider not found.")
    provider = provider.strip()
    model = model.strip()
    error, name = _clean_ai_form(provider, display_name, model)
    if error:
        form = {
            "provider": provider, "display_name": display_name,
            "model": model, "is_enabled": bool(is_enabled),
        }
        return render(
            request, "settings/ai_provider_form.html", current_user=user,
            active_subsection="ai", provider=row, form=form, error=error,
            **_ai_form_context(),
        )
    row.provider = provider
    row.display_name = name
    row.model = model
    row.is_enabled = bool(is_enabled)
    # Only replace the key when a new one is supplied.
    if api_key.strip():
        row.api_key_encrypted = encrypt_secret(api_key.strip())
    db.commit()
    _settings_event(
        request, user, category="ai_provider", event_type="ai_provider.updated",
        target_type="ai_provider", target_id=row.id, target_label=row.display_name,
        message=f"Updated AI provider '{row.display_name}'",
        detail={"provider": provider, "model": model},
    )
    flash(request, "AI provider updated.", "success")
    return RedirectResponse(url="/ui/settings/ai", status_code=303)


@router.post("/ai/{provider_id}/default")
def set_default_ai_provider(
    provider_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    row = db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AI provider not found.")
    db.query(AIProvider).filter(AIProvider.id != row.id).update(
        {AIProvider.is_default: False}
    )
    row.is_default = True
    row.is_enabled = True  # the default must be usable
    db.commit()
    _settings_event(
        request, user, category="ai_provider", event_type="ai_provider.default_set",
        target_type="ai_provider", target_id=row.id, target_label=row.display_name,
        message=f"Set '{row.display_name}' as the default AI provider",
    )
    flash(request, f"'{row.display_name}' is now the default provider.", "success")
    return RedirectResponse(url="/ui/settings/ai", status_code=303)


@router.post("/ai/{provider_id}/toggle")
def toggle_ai_provider(
    provider_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    row = db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AI provider not found.")
    row.is_enabled = not row.is_enabled
    if not row.is_enabled:
        row.is_default = False  # a disabled provider can't be the default
    db.commit()
    flash(request, f"'{row.display_name}' {'enabled' if row.is_enabled else 'disabled'}.", "success")
    return RedirectResponse(url="/ui/settings/ai", status_code=303)


@router.post("/ai/{provider_id}/delete")
def delete_ai_provider(
    provider_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    row = db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AI provider not found.")
    label = row.display_name
    was_default = row.is_default
    db.delete(row)
    db.flush()
    # If we removed the default, promote the next enabled provider.
    if was_default:
        nxt = (
            db.query(AIProvider)
            .filter(AIProvider.is_enabled.is_(True))
            .order_by(AIProvider.id)
            .first()
        )
        if nxt is not None:
            nxt.is_default = True
    db.commit()
    _settings_event(
        request, user, category="ai_provider", event_type="ai_provider.deleted",
        target_type="ai_provider", target_id=provider_id, target_label=label,
        message=f"Deleted AI provider '{label}'",
    )
    flash(request, f"AI provider '{label}' deleted.", "success")
    return RedirectResponse(url="/ui/settings/ai", status_code=303)
