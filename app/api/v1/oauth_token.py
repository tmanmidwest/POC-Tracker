"""OAuth 2.0 token endpoint.

Implements the `client_credentials` grant flow per RFC 6749 section 4.4.
This endpoint is mounted at /oauth/token (not under /api/v1) to match
common OAuth conventions and what Saviynt and similar IGA tools expect.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import OAuthClient
from app.services.audit import record_event
from app.services.jwt_tokens import issue_access_token
from app.services.tokens import verify_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["auth"])


def _oauth_error(
    error: str, description: str, status_code: int = status.HTTP_400_BAD_REQUEST
) -> JSONResponse:
    """Return an RFC 6749-compliant error response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "error_description": description},
    )


@router.post("/token")
def token_endpoint(
    request: Request,
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """RFC 6749 token endpoint. Currently supports only `client_credentials`.

    Request:
        POST /oauth/token
        Content-Type: application/x-www-form-urlencoded

        grant_type=client_credentials
        &client_id=poct_client_xxxxxxxxxxxxxxxx
        &client_secret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

    Success response (200):
        {
          "access_token": "eyJ...",
          "token_type": "Bearer",
          "expires_in": 3600
        }
    """
    client_ip = request.client.host if request.client else None

    def _deny(reason: str) -> None:
        """Record a denied token request."""
        record_event(
            category="oauth",
            event_type="oauth.token.denied",
            outcome="failure",
            actor_type="oauth_client",
            actor_label=client_id,
            message=f"OAuth token request denied ({reason})",
            detail={"reason": reason, "grant_type": grant_type},
            request=request,
        )

    if grant_type != "client_credentials":
        log.info("oauth_token_unsupported_grant", extra={"grant_type": grant_type})
        _deny("unsupported_grant_type")
        return _oauth_error(
            "unsupported_grant_type",
            f"Grant type '{grant_type}' is not supported. Use 'client_credentials'.",
        )

    oauth_client = (
        db.query(OAuthClient).filter(OAuthClient.client_id == client_id).one_or_none()
    )

    # Use the same error for missing and wrong-secret to avoid client enumeration
    invalid_client = _oauth_error(
        "invalid_client",
        "Client authentication failed.",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )

    if oauth_client is None:
        log.info("oauth_token_unknown_client", extra={"client_id": client_id})
        _deny("unknown_client")
        return invalid_client

    if oauth_client.revoked_at is not None:
        log.info("oauth_token_revoked_client", extra={"client_id": client_id})
        _deny("revoked_client")
        return invalid_client

    if not verify_token(client_secret, oauth_client.client_secret_hash):
        log.info("oauth_token_bad_secret", extra={"client_id": client_id})
        _deny("invalid_secret")
        return invalid_client

    # Issue the token
    access_token, _expires_at = issue_access_token(
        client_id=oauth_client.client_id,
        lifetime_seconds=oauth_client.token_lifetime_seconds,
    )

    oauth_client.last_used_at = datetime.now(UTC)
    oauth_client.last_used_ip = client_ip
    db.commit()

    log.info(
        "oauth_token_issued",
        extra={
            "client_id": oauth_client.client_id,
            "lifetime_seconds": oauth_client.token_lifetime_seconds,
        },
    )
    record_event(
        category="oauth",
        event_type="oauth.token.issued",
        actor_type="oauth_client",
        actor_label=oauth_client.client_id,
        target_type="oauth_client",
        target_id=oauth_client.id,
        target_label=oauth_client.name,
        message=f"Access token issued to '{oauth_client.client_id}'",
        detail={
            "grant_type": grant_type,
            "lifetime_seconds": oauth_client.token_lifetime_seconds,
        },
        request=request,
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": oauth_client.token_lifetime_seconds,
        },
    )
