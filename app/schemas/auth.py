"""Pydantic schemas for authentication endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Session login (UI)
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Request body for POST /api/v1/auth/session/login (JSON)."""

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=255)


class LoginResponse(BaseModel):
    """Response body for successful login."""

    username: str
    user_id: int
    message: str = "Logged in successfully."


class SessionInfoResponse(BaseModel):
    """Response body for GET /api/v1/auth/session/me."""

    authenticated: bool
    username: str | None = None
    user_id: int | None = None


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    """Request body for creating a new API key."""

    name: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None


class ApiKeyCreateResponse(BaseModel):
    """Response from creating an API key. The full `key` value is returned
    ONCE here at creation time and never shown again.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key: str  # The full plaintext key — shown only at creation time
    key_prefix: str
    created_at: datetime
    expires_at: datetime | None


class ApiKeyOut(BaseModel):
    """API key as returned by list/get endpoints (no secret value)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    last_used_ip: str | None
    revoked_at: datetime | None
    expires_at: datetime | None


# ---------------------------------------------------------------------------
# OAuth clients
# ---------------------------------------------------------------------------


class OAuthClientCreate(BaseModel):
    """Request body for creating a new OAuth client."""

    name: str = Field(min_length=1, max_length=200)
    token_lifetime_seconds: int = Field(default=3600, ge=60, le=86400)


class OAuthClientCreateResponse(BaseModel):
    """Response from creating an OAuth client. The full client_secret is
    returned ONCE here and never shown again.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    client_id: str
    client_secret: str  # plaintext — shown only at creation
    token_lifetime_seconds: int
    created_at: datetime


class OAuthClientOut(BaseModel):
    """OAuth client as returned by list/get endpoints (no secret)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    client_id: str
    token_lifetime_seconds: int
    created_at: datetime
    last_used_at: datetime | None
    last_used_ip: str | None
    revoked_at: datetime | None


# ---------------------------------------------------------------------------
# OAuth token endpoint
# ---------------------------------------------------------------------------


class TokenResponse(BaseModel):
    """RFC 6749 token response."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class TokenErrorResponse(BaseModel):
    """RFC 6749 error response."""

    error: str
    error_description: str | None = None
