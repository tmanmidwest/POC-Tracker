"""OIDC single sign-on endpoints for the web UI.

These sit alongside the local username/password login in auth_routes.py. The
flow is the standard OIDC Authorization Code grant (with PKCE):

    GET  /ui/auth/{slug}/login     -> redirect to the identity provider
    GET  /ui/auth/{slug}/callback  -> exchange code, provision user, set session
"""

from __future__ import annotations

import logging

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuthProvider
from app.services.audit import record_event
from app.services.auth import SESSION_USER_ID_KEY, SESSION_USERNAME_KEY
from app.services.oidc import build_client, callback_url, find_or_create_user
from app.ui.flash import flash


def _record_sso_failure(slug: str, reason: str, request: Request) -> None:
    """Record a failed/aborted OIDC sign-in attempt."""
    record_event(
        category="oidc",
        event_type="oidc.sso.failure",
        outcome="failure",
        actor_type="idp",
        actor_label=slug,
        target_type="auth_provider",
        target_label=slug,
        message=f"OIDC sign-in via '{slug}' failed ({reason})",
        detail={"provider": slug, "reason": reason},
        request=request,
    )

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/auth", tags=["ui"], include_in_schema=False)

_NEXT_SESSION_KEY = "_oidc_next"


def _enabled_provider(db: Session, slug: str) -> AuthProvider | None:
    return (
        db.query(AuthProvider)
        .filter(AuthProvider.slug == slug, AuthProvider.is_enabled.is_(True))
        .one_or_none()
    )


@router.get("/{slug}/login")
async def oidc_login(
    slug: str,
    request: Request,
    next: str = "/ui/dashboard",
    db: Session = Depends(get_db),
) -> Response:
    """Kick off the OIDC flow: redirect the browser to the identity provider."""
    provider = _enabled_provider(db, slug)
    if provider is None:
        flash(request, "That sign-in option is not available.", "error")
        return RedirectResponse(url="/ui/login", status_code=303)

    request.session[_NEXT_SESSION_KEY] = next
    client = build_client(provider)
    redirect_uri = callback_url(request, slug)
    try:
        return await client.authorize_redirect(request, redirect_uri)
    except OAuthError as exc:
        log.warning("oidc_authorize_redirect_failed", extra={"provider": slug, "error": str(exc)})
        _record_sso_failure(slug, "authorize_redirect_failed", request)
        flash(request, f"Could not start sign-in with {provider.display_name}.", "error")
        return RedirectResponse(url="/ui/login", status_code=303)
    except Exception:
        # Most often: the app can't reach the provider's discovery document
        # (wrong issuer URL, network, or an untrusted TLS cert on the IdP).
        log.exception("oidc_authorize_redirect_error", extra={"provider": slug})
        _record_sso_failure(slug, "authorize_redirect_error", request)
        flash(
            request,
            f"Could not start sign-in with {provider.display_name}. The app may be "
            f"unable to reach {provider.issuer_url}. See server logs for details.",
            "error",
        )
        return RedirectResponse(url="/ui/login", status_code=303)


@router.get("/{slug}/callback")
async def oidc_callback(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Handle the provider redirect: exchange the code and log the user in."""
    provider = _enabled_provider(db, slug)
    if provider is None:
        flash(request, "That sign-in option is not available.", "error")
        return RedirectResponse(url="/ui/login", status_code=303)

    client = build_client(provider)
    try:
        token = await client.authorize_access_token(request)
        claims = token.get("userinfo")
        if not claims:
            claims = await client.userinfo(token=token)
    except OAuthError as exc:
        log.warning("oidc_callback_failed", extra={"provider": slug, "error": str(exc)})
        _record_sso_failure(slug, "callback_failed", request)
        flash(request, f"Sign-in with {provider.display_name} failed or was cancelled.", "error")
        return RedirectResponse(url="/ui/login", status_code=303)
    except Exception:
        # Token endpoint / JWKS / userinfo call failed unexpectedly (network,
        # TLS, or a malformed response from the provider).
        log.exception("oidc_callback_error", extra={"provider": slug})
        _record_sso_failure(slug, "callback_error", request)
        flash(
            request,
            f"Sign-in with {provider.display_name} hit an unexpected error. "
            "See server logs for details.",
            "error",
        )
        return RedirectResponse(url="/ui/login", status_code=303)

    if not claims or not claims.get("sub"):
        log.warning("oidc_no_subject", extra={"provider": slug})
        _record_sso_failure(slug, "no_subject", request)
        flash(request, "The identity provider did not return a usable profile.", "error")
        return RedirectResponse(url="/ui/login", status_code=303)

    try:
        user = find_or_create_user(db, provider, dict(claims))
    except Exception:
        db.rollback()
        # Most often: the password_hash-nullable migration (0003) didn't run, or
        # a username/identity uniqueness conflict.
        log.exception("oidc_provision_error", extra={"provider": slug})
        _record_sso_failure(slug, "provision_error", request)
        flash(
            request,
            "Could not provision your account from the identity provider. "
            "See server logs for details.",
            "error",
        )
        return RedirectResponse(url="/ui/login", status_code=303)

    if not user.is_active:
        flash(request, "Your account is disabled. Contact an administrator.", "error")
        return RedirectResponse(url="/ui/login", status_code=303)

    request.session[SESSION_USER_ID_KEY] = user.id
    request.session[SESSION_USERNAME_KEY] = user.username
    next_url = request.session.pop(_NEXT_SESSION_KEY, "/ui/dashboard")

    log.info(
        "oidc_login_success",
        extra={"username": user.username, "user_id": user.id, "provider": slug},
    )
    record_event(
        category="oidc",
        event_type="oidc.sso.success",
        actor_type="idp",
        actor_label=user.username,
        actor_id=user.id,
        target_type="auth_provider",
        target_label=provider.slug,
        message=f"{user.username} signed in via {provider.display_name}",
        detail={"provider": slug, "provider_name": provider.display_name},
        request=request,
    )
    flash(request, f"Signed in as {user.username} via {provider.display_name}.", "success")
    return RedirectResponse(url=next_url, status_code=303)
