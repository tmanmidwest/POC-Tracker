"""Google OAuth for the Google Tasks integration.

Two concerns:
- The admin config singleton (client id + encrypted client secret + enable flag).
- The OAuth authorization-code + PKCE flow and token refresh, done directly over
  httpx (no heavy SDK) so it's self-contained and mockable in tests.

Scopes: ``openid email`` (to show which account is connected) plus
``.../auth/tasks`` (read/write Google Tasks). Access tokens are short-lived and
minted on demand from the stored refresh token.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any

from sqlalchemy.orm import Session

from app.models.google_tasks_config import GOOGLE_TASKS_CONFIG_ID, GoogleTasksConfig
from app.services.google_http import client
from app.services.secret_box import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

SCOPES = "openid email https://www.googleapis.com/auth/tasks"


class GoogleNotConfigured(Exception):
    """The admin hasn't configured/enabled the Google Tasks integration."""


class GoogleNeedsReauth(Exception):
    """The refresh token was rejected — the user must reconnect."""


# ---------------------------------------------------------------------------
# Admin config singleton
# ---------------------------------------------------------------------------


def get_config(db: Session) -> GoogleTasksConfig:
    """Return the singleton config row, creating an empty one if absent."""
    row = db.get(GoogleTasksConfig, GOOGLE_TASKS_CONFIG_ID)
    if row is None:
        row = GoogleTasksConfig(id=GOOGLE_TASKS_CONFIG_ID, is_enabled=False)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def set_config(
    db: Session, *, client_id: str, client_secret: str | None, is_enabled: bool
) -> GoogleTasksConfig:
    """Persist the admin OAuth client config. A blank secret keeps the stored one."""
    row = get_config(db)
    row.client_id = client_id.strip() or None
    if client_secret:
        row.client_secret_encrypted = encrypt_secret(client_secret.strip())
    row.is_enabled = is_enabled
    db.commit()
    db.refresh(row)
    return row


def is_ready(db: Session) -> bool:
    """Whether the integration is enabled and has client credentials."""
    row = db.get(GoogleTasksConfig, GOOGLE_TASKS_CONFIG_ID)
    return bool(row and row.is_enabled and row.is_configured)


def _client_secret(config: GoogleTasksConfig) -> str:
    if not config.client_secret_encrypted:
        raise GoogleNotConfigured("No Google client secret configured.")
    return decrypt_secret(config.client_secret_encrypted)


# ---------------------------------------------------------------------------
# PKCE + authorization URL
# ---------------------------------------------------------------------------


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for a PKCE S256 exchange."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def make_state() -> str:
    """Random anti-CSRF state token."""
    return secrets.token_urlsafe(24)


def build_authorize_url(
    config: GoogleTasksConfig, *, redirect_uri: str, state: str, code_challenge: str
) -> str:
    """Build the Google consent URL. ``access_type=offline`` + ``prompt=consent``
    guarantees a refresh token is issued."""
    if not config.client_id:
        raise GoogleNotConfigured("No Google client id configured.")
    from urllib.parse import urlencode

    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange / refresh
# ---------------------------------------------------------------------------


def exchange_code(
    config: GoogleTasksConfig, *, code: str, redirect_uri: str, code_verifier: str
) -> dict[str, Any]:
    """Exchange an authorization code for tokens. Returns the token response
    (access_token, refresh_token, scope, expires_in, …)."""
    resp = client().post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config.client_id,
            "client_secret": _client_secret(config),
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    if resp.status_code != 200:
        raise GoogleNotConfigured(f"Token exchange failed: {resp.status_code} {resp.text}")
    return resp.json()


def refresh_access_token(config: GoogleTasksConfig, refresh_token: str) -> str:
    """Mint a fresh access token from a stored refresh token.

    Raises GoogleNeedsReauth if Google rejects the refresh token (revoked/expired).
    """
    resp = client().post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.client_id,
            "client_secret": _client_secret(config),
        },
    )
    if resp.status_code == 400:
        # invalid_grant → the user revoked access or the token expired.
        raise GoogleNeedsReauth(resp.text)
    if resp.status_code != 200:
        raise GoogleNotConfigured(f"Token refresh failed: {resp.status_code} {resp.text}")
    return str(resp.json()["access_token"])


def fetch_email(access_token: str) -> str | None:
    """Best-effort: the connected account's email (for display). None on failure."""
    try:
        resp = client().get(
            USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
        )
        if resp.status_code == 200:
            return resp.json().get("email")
    except Exception:  # display-only; never block the connect flow
        pass
    return None


def revoke(token: str) -> None:
    """Best-effort revoke of a token at Google (on disconnect)."""
    try:
        client().post(REVOKE_ENDPOINT, data={"token": token})
    except Exception:
        pass
