"""Manage the MCP server's API token.

The MCP server authenticates to the REST API with an API key. To let an operator
rotate that key from the UI at any time *without restarting or reconfiguring the
MCP server*, we persist the current token to a file on the data volume
(``<data_dir>/mcp_api_key``). The MCP server reads this file live on each request,
so a rotation in the UI takes effect on the server's very next call.

Rotating creates a fresh ``ApiKey`` row (named "MCP Server") and revokes the
previous one, so an old token stops working immediately.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import ApiKey
from app.services.tokens import generate_api_key, hash_token

log = logging.getLogger(__name__)

TOKEN_FILENAME = "mcp_api_key"
MCP_KEY_NAME = "MCP Server"


def token_path(settings: Settings | None = None) -> Path:
    """Path to the file holding the current MCP token on the data volume."""
    settings = settings or get_settings()
    return settings.data_dir / TOKEN_FILENAME


def read_token(settings: Settings | None = None) -> str | None:
    """Return the current MCP token from disk, or None if not configured."""
    path = token_path(settings)
    if not path.exists():
        return None
    value = path.read_text().strip()
    return value or None


def _row_for(db: Session, full_key: str) -> ApiKey | None:
    return db.query(ApiKey).filter(ApiKey.key_hash == hash_token(full_key)).one_or_none()


def current_key_id(db: Session, settings: Settings | None = None) -> int | None:
    """Return the ApiKey id of the current MCP token, or None.

    Lets the API-keys list flag and protect the dedicated MCP key.
    """
    current = read_token(settings)
    if not current:
        return None
    row = _row_for(db, current)
    return row.id if row else None


def rotate(db: Session, *, actor_id: int, settings: Settings | None = None) -> str:
    """Revoke the previous MCP token (if any), mint a new one, persist it, and
    return the new plaintext key. The plaintext is only returned here."""
    settings = settings or get_settings()

    # Revoke the previous token's ApiKey row so it stops authenticating.
    previous = read_token(settings)
    if previous:
        row = _row_for(db, previous)
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)

    full_key, prefix = generate_api_key()
    db.add(
        ApiKey(
            name=MCP_KEY_NAME,
            key_prefix=prefix,
            key_hash=hash_token(full_key),
            created_by_user_id=actor_id,
        )
    )
    db.commit()

    settings.ensure_data_dir()
    path = token_path(settings)
    path.write_text(full_key)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # filesystem may not support chmod (e.g. some mounted volumes)
    log.info("mcp_token_rotated", extra={"prefix": prefix, "by_user_id": actor_id})
    return full_key


def clear(db: Session, *, settings: Settings | None = None) -> bool:
    """Revoke the current MCP token and remove the file. Returns True if one existed."""
    settings = settings or get_settings()
    current = read_token(settings)
    if current is None:
        return False
    row = _row_for(db, current)
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()
    token_path(settings).unlink(missing_ok=True)
    log.info("mcp_token_cleared")
    return True


def status(db: Session, settings: Settings | None = None) -> dict[str, Any]:
    """Describe the current MCP token for the settings page (never the secret)."""
    settings = settings or get_settings()
    current = read_token(settings)
    path = token_path(settings)
    if current is None:
        return {"configured": False, "path": str(path)}
    row = _row_for(db, current)
    return {
        "configured": True,
        "path": str(path),
        "prefix": current[:14],
        "created_at": row.created_at if row else None,
        "last_used_at": row.last_used_at if row else None,
        "revoked": bool(row and row.revoked_at is not None),
        "tracked": row is not None,
    }
