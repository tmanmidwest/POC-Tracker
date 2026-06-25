"""API key management endpoints (CRUD).

Access requires session auth (logged-in admin). Keys are created with
a randomly-generated value that's returned to the user once and stored
only as a SHA-256 hash thereafter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, AppUser
from app.schemas.auth import ApiKeyCreate, ApiKeyCreateResponse, ApiKeyOut
from app.services.auth import get_current_user
from app.services.tokens import generate_api_key, hash_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/api-keys", tags=["auth"])


@router.get("/", response_model=list[ApiKeyOut])
def list_api_keys(
    db: Session = Depends(get_db),
    _user: AppUser = Depends(get_current_user),
) -> list[ApiKey]:
    """List all API keys (does not include the secret value)."""
    return db.query(ApiKey).order_by(desc(ApiKey.created_at)).all()


@router.post(
    "/",
    response_model=ApiKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_api_key(
    body: ApiKeyCreate,
    db: Session = Depends(get_db),
    user: AppUser = Depends(get_current_user),
) -> ApiKeyCreateResponse:
    """Create a new API key.

    The full key value is returned in this response ONCE and never shown again.
    Only the SHA-256 hash and first 8 chars are stored.
    """
    full_key, prefix = generate_api_key()
    api_key = ApiKey(
        name=body.name,
        key_prefix=prefix,
        key_hash=hash_token(full_key),
        created_by_user_id=user.id,
        expires_at=body.expires_at,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    log.info(
        "api_key_created",
        extra={
            "api_key_id": api_key.id,
            "key_name": api_key.name,
            "prefix": prefix,
            "created_by": user.username,
        },
    )
    return ApiKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key=full_key,
        key_prefix=prefix,
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
    )


@router.get("/{api_key_id}", response_model=ApiKeyOut)
def get_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    _user: AppUser = Depends(get_current_user),
) -> ApiKey:
    """Get a single API key by ID."""
    api_key = db.get(ApiKey, api_key_id)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="API key not found."
        )
    return api_key


@router.post("/{api_key_id}/revoke", response_model=ApiKeyOut)
def revoke_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(get_current_user),
) -> ApiKey:
    """Revoke an API key. Revoked keys cannot authenticate."""
    api_key = db.get(ApiKey, api_key_id)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="API key not found."
        )
    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        db.commit()
        db.refresh(api_key)
        log.info(
            "api_key_revoked",
            extra={
                "api_key_id": api_key.id,
                "prefix": api_key.key_prefix,
                "revoked_by": user.username,
            },
        )
    return api_key


@router.delete("/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(get_current_user),
) -> None:
    """Permanently delete an API key. Prefer /revoke for audit trail."""
    api_key = db.get(ApiKey, api_key_id)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="API key not found."
        )
    db.delete(api_key)
    db.commit()
    log.info(
        "api_key_deleted",
        extra={"api_key_id": api_key_id, "deleted_by": user.username},
    )
