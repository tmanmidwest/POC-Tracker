"""OIDC single sign-on helpers built on Authlib.

Providers are configured dynamically (stored in the DB, managed from the UI),
so rather than registering them once at startup we build a one-off Authlib
client per request from the stored config. The per-login flow state (OAuth
`state`, PKCE `code_verifier`, OIDC `nonce`) lives in the signed session
cookie, managed by Authlib's Starlette integration — the client object itself
is stateless across the redirect/callback round-trip.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppUser, AuthProvider, UserIdentity
from app.services.audit import record_event
from app.services.secret_box import decrypt_secret

log = logging.getLogger(__name__)


def discovery_url(issuer_url: str) -> str:
    """Return the OIDC discovery document URL for an issuer."""
    return issuer_url.rstrip("/") + "/.well-known/openid-configuration"


def build_client(provider: AuthProvider) -> Any:
    """Construct an Authlib OAuth client for a single provider."""
    oauth = OAuth()
    client_secret = (
        decrypt_secret(provider.client_secret_encrypted)
        if provider.client_secret_encrypted
        else None
    )
    client_kwargs = {"scope": provider.scopes or "openid email profile"}
    # Use PKCE (S256) — required for public clients, harmless for confidential.
    client_kwargs["code_challenge_method"] = "S256"
    oauth.register(
        name=provider.slug,
        client_id=provider.client_id,
        client_secret=client_secret,
        server_metadata_url=discovery_url(provider.issuer_url),
        client_kwargs=client_kwargs,
    )
    return oauth.create_client(provider.slug)


def callback_url(request: Request, slug: str) -> str:
    """Build the absolute redirect URI for a provider's callback.

    Prefers the configured public base URL (correct behind an HTTPS proxy),
    falling back to the incoming request's base URL.
    """
    settings = get_settings()
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    return f"{base}/ui/auth/{slug}/callback"


_USERNAME_SANITIZE = re.compile(r"[^a-z0-9._-]+")


def _candidate_username(claims: dict[str, Any], slug: str) -> str:
    """Derive a base username from OIDC claims."""
    raw = (
        claims.get("preferred_username")
        or claims.get("nickname")
        or (claims.get("email") or "").split("@")[0]
        or claims.get("name")
        or f"{slug}-user"
    )
    cleaned = _USERNAME_SANITIZE.sub("-", str(raw).strip().lower()).strip("-._")
    return cleaned or f"{slug}-user"


def _display_name_from_claims(claims: dict[str, Any]) -> str | None:
    """Best display name from OIDC claims: 'name', else given + family."""
    name = (claims.get("name") or "").strip()
    if name:
        return name[:200]
    parts = [str(claims.get("given_name") or "").strip(), str(claims.get("family_name") or "").strip()]
    combined = " ".join(p for p in parts if p)
    return combined[:200] or None


def _unique_username(db: Session, base: str) -> str:
    """Return `base`, or `base-2`, `base-3`, … if it's already taken."""
    candidate = base
    suffix = 2
    while db.query(AppUser).filter(AppUser.username == candidate).first() is not None:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def find_or_create_user(
    db: Session, provider: AuthProvider, claims: dict[str, Any]
) -> AppUser:
    """Resolve the local AppUser for an authenticated OIDC subject (JIT provision).

    Matching is done strictly on (provider, subject) via UserIdentity — never on
    a username/email collision with an existing local account, to avoid takeover
    of a local admin by an attacker who can set a matching name at the IdP.
    """
    subject = str(claims["sub"])
    email = claims.get("email")
    now = datetime.now(UTC)

    identity = (
        db.query(UserIdentity)
        .filter(
            UserIdentity.provider_id == provider.id,
            UserIdentity.subject == subject,
        )
        .one_or_none()
    )

    if identity is not None:
        user = identity.user
        identity.last_login_at = now
        identity.email = email
        user.last_login_at = now
        db.commit()
        return user

    username = _unique_username(db, _candidate_username(claims, provider.slug))
    user = AppUser(
        username=username,
        password_hash=None,
        is_active=True,
        is_seeded=False,
        last_login_at=now,
        display_name=_display_name_from_claims(claims),
    )
    db.add(user)
    db.flush()  # assign user.id

    db.add(
        UserIdentity(
            user_id=user.id,
            provider_id=provider.id,
            subject=subject,
            email=email,
            last_login_at=now,
        )
    )
    provider.last_used_at = now
    db.commit()
    log.info(
        "oidc_user_provisioned",
        extra={
            "username": username,
            "user_id": user.id,
            "provider": provider.slug,
            "subject": subject,
        },
    )
    record_event(
        category="oidc",
        event_type="oidc.jit.provisioned",
        actor_type="idp",
        actor_label=provider.slug,
        target_type="app_user",
        target_id=user.id,
        target_label=username,
        message=f"Provisioned new user '{username}' from {provider.display_name} (JIT)",
        detail={"provider": provider.slug, "subject": subject, "email": email},
    )
    return user
