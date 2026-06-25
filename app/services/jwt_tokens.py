"""JWT issuance and validation for OAuth 2.0 access tokens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt

from app.config import get_settings


class JWTValidationError(Exception):
    """Raised when a JWT fails validation (bad signature, expired, malformed)."""


def issue_access_token(client_id: str, lifetime_seconds: int) -> tuple[str, datetime]:
    """Issue a JWT access token for the given OAuth client.

    Returns (token, expires_at).
    """
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=lifetime_seconds)
    payload: dict[str, Any] = {
        "iss": settings.app_name,  # issuer
        "sub": client_id,  # subject — the OAuth client_id
        "aud": "poct-api",  # audience
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "token_type": "client_credentials",
    }
    signing_key = settings.get_or_create_jwt_signing_key()
    token = jwt.encode(payload, signing_key, algorithm=settings.jwt_algorithm)
    return token, expires_at


def validate_access_token(token: str) -> dict[str, Any]:
    """Validate a JWT access token and return its claims.

    Raises JWTValidationError on any failure (bad signature, expired, malformed,
    wrong audience, wrong issuer).
    """
    settings = get_settings()
    signing_key = settings.get_or_create_jwt_signing_key()
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=[settings.jwt_algorithm],
            audience="poct-api",
            issuer=settings.app_name,
        )
    except JWTError as e:
        raise JWTValidationError(str(e)) from e
    return claims
