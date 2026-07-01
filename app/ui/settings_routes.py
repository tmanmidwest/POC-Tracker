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
    OAuthClient,
    Project,
    ProjectGrant,
    UserIdentity,
)
from app.models.app_branding import BRANDING_ID
from app.models.audit_event import AuditEvent
from app.models.auth_provider import DEFAULT_SCOPES
from app.models.backup_run import BackupRun
from app.services import backups as backup_service
from app.services import branding as branding_service
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
from app.services.secret_box import encrypt_secret
from app.services.tokens import (
    generate_api_key,
    generate_oauth_client_id,
    generate_oauth_client_secret,
    hash_token,
)
from app.ui.dependencies import require_ui_user
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
    users = db.query(AppUser).order_by(AppUser.username).all()
    return render(
        request,
        "settings/admin_users.html",
        current_user=user,
        active_subsection="admin_users",
        users=users,
    )


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
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    username = username.strip()
    display = display_name.strip() or None
    if len(password) < 8:
        return render(
            request,
            "settings/admin_user_new.html",
            current_user=user,
            active_subsection="admin_users",
            form={"username": username, "role": role, "display_name": display},
            error="Password must be at least 8 characters.",
        )

    new_user = AppUser(
        username=username,
        display_name=display,
        password_hash=hash_password(password),
        is_active=True,
        is_seeded=False,
        is_admin=(role == "admin"),
        is_external=(role == "external"),
    )
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return render(
            request,
            "settings/admin_user_new.html",
            current_user=user,
            active_subsection="admin_users",
            form={"username": username, "role": role, "display_name": display},
            error=f"Username '{username}' is already taken.",
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
    )


@router.post("/admin-users/{user_id}/edit")
def update_user(
    user_id: int,
    request: Request,
    display_name: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    target = db.get(AppUser, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    target.display_name = display_name.strip() or None
    db.commit()
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.updated",
        target_type="app_user",
        target_id=target.id,
        target_label=target.username,
        message=f"Updated display name for '{target.username}'",
        detail={"display_name": target.display_name},
    )
    flash(request, f"Updated display name for '{target.username}'.", "success")
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
    make_admin = role == "admin"
    back = RedirectResponse(url="/ui/settings/admin-users", status_code=303)

    if target.id == user.id:
        flash(request, "You can't change your own role.", "error")
        return back
    if not make_admin and target.is_seeded:
        flash(request, "The seeded admin must remain an admin.", "error")
        return back
    if not make_admin and target.is_admin:
        admin_count = db.query(AppUser).filter(AppUser.is_admin.is_(True)).count()
        if admin_count <= 1:
            flash(request, "There must be at least one admin.", "error")
            return back
    if target.is_admin == make_admin:
        return back  # no change

    target.is_admin = make_admin
    if make_admin:
        # Admins are full internal users — never read-only external viewers.
        target.is_external = False
    db.commit()
    _settings_event(
        request, user,
        category="admin_user",
        event_type="admin_user.role_changed",
        target_type="app_user",
        target_id=target.id,
        target_label=target.username,
        message=f"Changed role of '{target.username}' to {'admin' if make_admin else 'standard'}",
        detail={"is_admin": make_admin},
    )
    flash(
        request,
        f"'{target.username}' is now {'an admin' if make_admin else 'a standard user'}.",
        "success",
    )
    return back


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
        },
    )


@router.post("/system")
def update_system(
    request: Request,
    audit_retention_days: int = Form(...),
    tasks_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    if audit_retention_days < 0 or audit_retention_days > _MAX_RETENTION_DAYS:
        return render(
            request,
            "settings/system.html",
            current_user=user,
            active_subsection="system",
            form={
                "audit_retention_days": audit_retention_days,
                "tasks_enabled": bool(tasks_enabled),
            },
            error=(
                f"Retention must be between 0 and {_MAX_RETENTION_DAYS} days "
                "(0 keeps events forever)."
            ),
        )

    config = system_config.get_config(db)
    previous = config.audit_retention_days
    tasks_was = config.tasks_enabled
    tasks_now = bool(tasks_enabled)
    system_config.set_retention_days(db, audit_retention_days)
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
    do_features = bool(reset_feature_types)
    do_ucstatuses = bool(reset_use_case_statuses)
    do_library = bool(reset_use_case_library)
    do_audit = bool(reset_audit_events)

    do_lookups = do_roles or do_pstatuses or do_features or do_ucstatuses

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
        flash(request, f"Restore rejected: {exc}", "error")
        return RedirectResponse(url="/ui/settings/backups", status_code=303)
    except Exception as exc:
        log.exception("ui_restore_stage_failed", extra={"by": user.username})
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
