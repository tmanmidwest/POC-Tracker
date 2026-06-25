"""FastAPI dependencies for authentication.

There are two distinct auth surfaces:

1. **Session auth** (web UI): a signed session cookie identifies an AppUser.
   Use `get_current_user` on UI routes.

2. **API auth** (REST): either an API key in Authorization: Bearer header,
   OR a JWT (issued via OAuth client credentials) in the same header.
   Use `get_authenticated_principal` on REST routes — it accepts both methods.

A "principal" is whoever is calling, identified by either:
- An ApiKey record (the "subject" is the user who created the key)
- An OAuthClient record (machine-to-machine, no user)

The shared `Principal` dataclass abstracts these so endpoints don't need to
care which method the caller used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, AppUser, OAuthClient
from app.services.jwt_tokens import JWTValidationError, validate_access_token
from app.services.tokens import API_KEY_PREFIX, hash_token

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Principal — represents whoever is calling the API
# ---------------------------------------------------------------------------


class PrincipalKind(StrEnum):
    """Which auth method was used."""

    API_KEY = "api_key"
    OAUTH = "oauth"


@dataclass
class Principal:
    """A successfully authenticated REST API caller."""

    kind: PrincipalKind
    api_key: ApiKey | None = None
    oauth_client: OAuthClient | None = None

    @property
    def identifier(self) -> str:
        """Human-readable identifier for logging."""
        if self.kind == PrincipalKind.API_KEY and self.api_key is not None:
            return f"api_key:{self.api_key.key_prefix}"
        if self.kind == PrincipalKind.OAUTH and self.oauth_client is not None:
            return f"oauth:{self.oauth_client.client_id}"
        return "unknown"


# ---------------------------------------------------------------------------
# Session auth — for the web UI
# ---------------------------------------------------------------------------


SESSION_USER_ID_KEY = "user_id"
SESSION_USERNAME_KEY = "username"


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> AppUser:
    """Return the AppUser identified by the session cookie, or raise 401."""
    user_id = request.session.get(SESSION_USER_ID_KEY)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please log in.",
        )
    user = db.get(AppUser, user_id)
    if user is None or not user.is_active:
        # The user was deleted or deactivated mid-session
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is no longer valid. Please log in again.",
        )
    return user


def get_optional_user(
    request: Request, db: Session = Depends(get_db)
) -> AppUser | None:
    """Like get_current_user but returns None instead of raising. Useful for
    pages that work for both logged-in and anonymous visitors (e.g., the login
    page itself shouldn't 401 you out of viewing it).
    """
    user_id = request.session.get(SESSION_USER_ID_KEY)
    if not user_id:
        return None
    user = db.get(AppUser, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


# ---------------------------------------------------------------------------
# REST API auth — API key OR JWT
# ---------------------------------------------------------------------------


def _extract_bearer_token(request: Request) -> str | None:
    """Return the bearer token from the Authorization header, or None."""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def _validate_api_key(token: str, db: Session, client_ip: str | None) -> ApiKey | None:
    """Look up and validate an API key. Returns the ApiKey row on success.

    Returns None if the token is not an API key (so the caller can try OAuth).
    Raises HTTPException(401) if the token IS an API key but invalid/revoked/expired.
    """
    if not token.startswith(API_KEY_PREFIX):
        return None

    token_hash = hash_token(token)
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == token_hash).one_or_none()

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    if api_key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has been revoked.",
        )

    now = datetime.now(UTC)
    if api_key.expires_at is not None and api_key.expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has expired.",
        )

    # Update usage tracking
    api_key.last_used_at = now
    api_key.last_used_ip = client_ip
    db.commit()

    return api_key


def _validate_jwt(token: str, db: Session, client_ip: str | None) -> OAuthClient | None:
    """Validate a JWT and return the associated OAuthClient.

    Returns None if the token isn't a JWT (e.g., wrong format entirely).
    Raises HTTPException(401) if it IS a JWT but invalid.
    """
    # JWTs have three base64url-encoded segments separated by dots.
    # API keys start with "poct_" so this is a clean discriminator.
    if token.count(".") != 2:
        return None

    try:
        claims = validate_access_token(token)
    except JWTValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid access token: {exc}",
        ) from exc

    client_id = claims.get("sub")
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token: missing subject.",
        )

    oauth_client = (
        db.query(OAuthClient).filter(OAuthClient.client_id == client_id).one_or_none()
    )
    if oauth_client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token references unknown client.",
        )
    if oauth_client.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OAuth client has been revoked.",
        )

    now = datetime.now(UTC)
    oauth_client.last_used_at = now
    oauth_client.last_used_ip = client_ip
    db.commit()

    return oauth_client


def get_authenticated_principal(
    request: Request, db: Session = Depends(get_db)
) -> Principal:
    """Authenticate a REST API request via API key OR JWT bearer token.

    Returns a Principal indicating which auth method succeeded and who's calling.
    Raises 401 on any failure.
    """
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization: Bearer <token> header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    client_ip = request.client.host if request.client else None

    # Try API key first (cheap prefix check)
    api_key = _validate_api_key(token, db, client_ip)
    if api_key is not None:
        log.info(
            "auth_success",
            extra={"method": "api_key", "principal": f"api_key:{api_key.key_prefix}"},
        )
        return Principal(kind=PrincipalKind.API_KEY, api_key=api_key)

    # Then try JWT
    oauth_client = _validate_jwt(token, db, client_ip)
    if oauth_client is not None:
        log.info(
            "auth_success",
            extra={"method": "oauth", "principal": f"oauth:{oauth_client.client_id}"},
        )
        return Principal(kind=PrincipalKind.OAUTH, oauth_client=oauth_client)

    # Token didn't match either pattern
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token format not recognized. Expected an API key or JWT.",
        headers={"WWW-Authenticate": "Bearer"},
    )
