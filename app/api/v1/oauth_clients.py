"""OAuth client management endpoints (CRUD).

Access requires session auth (logged-in admin). Client secrets are
returned once at creation and stored as SHA-256 hashes thereafter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, OAuthClient
from app.schemas.auth import (
    OAuthClientCreate,
    OAuthClientCreateResponse,
    OAuthClientOut,
)
from app.services.auth import get_current_user
from app.services.tokens import (
    generate_oauth_client_id,
    generate_oauth_client_secret,
    hash_token,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oauth-clients", tags=["auth"])


@router.get("/", response_model=list[OAuthClientOut])
def list_oauth_clients(
    db: Session = Depends(get_db),
    _user: AppUser = Depends(get_current_user),
) -> list[OAuthClient]:
    """List all OAuth clients (does not include the secret)."""
    return db.query(OAuthClient).order_by(desc(OAuthClient.created_at)).all()


@router.post(
    "/",
    response_model=OAuthClientCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_oauth_client(
    body: OAuthClientCreate,
    db: Session = Depends(get_db),
    user: AppUser = Depends(get_current_user),
) -> OAuthClientCreateResponse:
    """Create a new OAuth client. The client_secret is returned ONCE."""
    client_id = generate_oauth_client_id()
    client_secret = generate_oauth_client_secret()

    oauth_client = OAuthClient(
        name=body.name,
        client_id=client_id,
        client_secret_hash=hash_token(client_secret),
        token_lifetime_seconds=body.token_lifetime_seconds,
        created_by_user_id=user.id,
    )
    db.add(oauth_client)
    db.commit()
    db.refresh(oauth_client)

    log.info(
        "oauth_client_created",
        extra={
            "oauth_client_id": oauth_client.id,
            "client_name": oauth_client.name,
            "client_id": client_id,
            "created_by": user.username,
        },
    )
    return OAuthClientCreateResponse(
        id=oauth_client.id,
        name=oauth_client.name,
        client_id=client_id,
        client_secret=client_secret,
        token_lifetime_seconds=oauth_client.token_lifetime_seconds,
        created_at=oauth_client.created_at,
    )


@router.get("/{oauth_client_pk}", response_model=OAuthClientOut)
def get_oauth_client(
    oauth_client_pk: int,
    db: Session = Depends(get_db),
    _user: AppUser = Depends(get_current_user),
) -> OAuthClient:
    """Get a single OAuth client by primary key (NOT client_id)."""
    oauth_client = db.get(OAuthClient, oauth_client_pk)
    if oauth_client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OAuth client not found."
        )
    return oauth_client


@router.post("/{oauth_client_pk}/revoke", response_model=OAuthClientOut)
def revoke_oauth_client(
    oauth_client_pk: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(get_current_user),
) -> OAuthClient:
    """Revoke an OAuth client.

    Prevents new token issuance. Already-issued JWTs remain valid until they
    expire naturally — this matches standard OAuth semantics.
    """
    oauth_client = db.get(OAuthClient, oauth_client_pk)
    if oauth_client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OAuth client not found."
        )
    if oauth_client.revoked_at is None:
        oauth_client.revoked_at = datetime.now(UTC)
        db.commit()
        db.refresh(oauth_client)
        log.info(
            "oauth_client_revoked",
            extra={
                "oauth_client_id": oauth_client.id,
                "client_id": oauth_client.client_id,
                "revoked_by": user.username,
            },
        )
    return oauth_client


@router.delete("/{oauth_client_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_oauth_client(
    oauth_client_pk: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(get_current_user),
) -> None:
    """Permanently delete an OAuth client. Prefer /revoke for audit trail."""
    oauth_client = db.get(OAuthClient, oauth_client_pk)
    if oauth_client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OAuth client not found."
        )
    db.delete(oauth_client)
    db.commit()
    log.info(
        "oauth_client_deleted",
        extra={
            "oauth_client_id": oauth_client_pk,
            "deleted_by": user.username,
        },
    )
