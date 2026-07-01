"""Manage the inbound MCP gateway tokens (bearer secrets external apps present).

This replaces the single legacy gateway token with multiple named, individually
revocable tokens — one per consuming project/app — managed from the UI like API
keys. The records live in the ``mcp_gateway_tokens`` table.

The catch: the MCP server runs as a **separate container with no database access**
(it only shares the data volume and reaches the app over HTTP). So the middleware
can't query these rows. Instead the app writes the *hashes* of the currently-active
tokens to a JSON file on the shared volume (``<data_dir>/mcp_gateway_tokens.json``),
and the middleware verifies presented tokens against that file, read live on each
request. Creating or revoking a token re-syncs the file, so changes take effect on
the MCP server's very next call with no restart.

``POCT_MCP_AUTH_TOKEN`` still works as a single static override for a remote MCP
host that can't see the volume.

The full token is returned only once, at creation; only its SHA-256 hash and a
short prefix are persisted.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import McpGatewayToken
from app.services.tokens import hash_token

log = logging.getLogger(__name__)

ACTIVE_TOKENS_FILENAME = "mcp_gateway_tokens.json"
LEGACY_TOKEN_FILENAME = "mcp_gateway_token"  # single-token file we retire on rollout
TOKEN_PREFIX = "poctgw_"
TOKEN_RANDOM_LEN = 32


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate_token() -> tuple[str, str]:
    """Generate a new gateway token. Returns (full_token, prefix_for_display).

    Format: ``poctgw_<32 url-safe chars>``. The prefix is the first 14 characters
    (incl. ``poctgw_``), enough to identify "which token" without revealing it.
    """
    random_part = secrets.token_urlsafe(TOKEN_RANDOM_LEN)[:TOKEN_RANDOM_LEN]
    full = f"{TOKEN_PREFIX}{random_part}"
    return full, full[:14]


# ---------------------------------------------------------------------------
# Synced active-token file (read by the DB-less MCP middleware)
# ---------------------------------------------------------------------------


def active_tokens_path(settings: Settings | None = None) -> Path:
    """Path to the file holding active token hashes on the data volume."""
    return (settings or get_settings()).data_dir / ACTIVE_TOKENS_FILENAME


def sync_active_tokens(db: Session, settings: Settings | None = None) -> int:
    """Rewrite the active-token file from the DB. Returns the active count.

    Also removes the retired single-token file so a stale secret doesn't linger.
    """
    settings = settings or get_settings()
    rows = (
        db.query(McpGatewayToken)
        .filter(McpGatewayToken.revoked_at.is_(None))
        .order_by(McpGatewayToken.created_at.asc())
        .all()
    )
    payload = [
        {"name": r.name, "prefix": r.token_prefix, "hash": r.token_hash} for r in rows
    ]

    settings.ensure_data_dir()
    path = active_tokens_path(settings)
    path.write_text(json.dumps(payload))
    try:
        path.chmod(0o600)
    except OSError:
        pass  # some mounted volumes don't support chmod

    # Retire the legacy single-token file if it's still around.
    (settings.data_dir / LEGACY_TOKEN_FILENAME).unlink(missing_ok=True)

    return len(payload)


def _active_hashes(settings: Settings | None = None) -> list[str]:
    """Active token hashes from the synced file (used by the MCP middleware)."""
    path = active_tokens_path(settings)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text() or "[]")
    except (json.JSONDecodeError, OSError):
        return []
    return [e["hash"] for e in data if isinstance(e, dict) and e.get("hash")]


# ---------------------------------------------------------------------------
# Verification (called by the MCP server's GatewayAuthMiddleware)
# ---------------------------------------------------------------------------


def is_configured(settings: Settings | None = None) -> bool:
    """True if any inbound token would authenticate (env override or a synced one).

    When False, the MCP endpoint has nothing to check against and should answer
    503 (operator hasn't generated a token yet) rather than 401.
    """
    if os.environ.get("POCT_MCP_AUTH_TOKEN"):
        return True
    return bool(_active_hashes(settings))


def verify(provided: str, settings: Settings | None = None) -> bool:
    """Constant-time check of a presented bearer against the active tokens."""
    if not provided:
        return False
    env = os.environ.get("POCT_MCP_AUTH_TOKEN")
    if env and hmac.compare_digest(provided, env):
        return True
    presented_hash = hash_token(provided)
    ok = False
    # Compare against every active hash without short-circuiting, to keep timing
    # independent of how many tokens exist or which one matched.
    for h in _active_hashes(settings):
        if hmac.compare_digest(presented_hash, h):
            ok = True
    return ok


# ---------------------------------------------------------------------------
# CRUD (called from the app UI; these touch the DB and re-sync the file)
# ---------------------------------------------------------------------------


def list_tokens(db: Session) -> list[McpGatewayToken]:
    """All gateway tokens, newest first, for the settings page."""
    return (
        db.query(McpGatewayToken)
        .order_by(McpGatewayToken.created_at.desc())
        .all()
    )


def create(
    db: Session, *, name: str, actor_id: int, settings: Settings | None = None
) -> tuple[McpGatewayToken, str]:
    """Mint a named token, persist its hash, sync the file, return (row, plaintext).

    The plaintext is only available here — afterward only the prefix is shown.
    """
    full, prefix = generate_token()
    row = McpGatewayToken(
        name=name.strip(),
        token_prefix=prefix,
        token_hash=hash_token(full),
        created_by_user_id=actor_id,
    )
    db.add(row)
    db.commit()
    sync_active_tokens(db, settings)
    log.info(
        "mcp_gateway_token_created",
        extra={
            "token_id": row.id,
            "token_name": row.name,
            "prefix": prefix,
            "by": actor_id,
        },
    )
    return row, full


def revoke(
    db: Session, token_id: int, settings: Settings | None = None
) -> McpGatewayToken | None:
    """Revoke a token so it stops authenticating. Returns the row, or None."""
    row = db.get(McpGatewayToken, token_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()
        sync_active_tokens(db, settings)
        log.info("mcp_gateway_token_revoked", extra={"token_id": token_id})
    return row


def delete(
    db: Session, token_id: int, settings: Settings | None = None
) -> str | None:
    """Delete a token record entirely. Returns its name, or None if not found."""
    row = db.get(McpGatewayToken, token_id)
    if row is None:
        return None
    name = row.name
    db.delete(row)
    db.commit()
    sync_active_tokens(db, settings)
    log.info(
        "mcp_gateway_token_deleted", extra={"token_id": token_id, "token_name": name}
    )
    return name
