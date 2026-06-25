"""Manage the MCP server's *inbound* access control from the UI.

Two pieces of inbound config let a gateway (Saviynt, etc.) call the MCP server:

* a **gateway token** — the bearer secret the gateway must present, and
* an optional **allowed-hosts** list — Host headers permitted past a DNS-rebinding
  check (empty = allow any host; bearer auth is the primary control).

Both are persisted on the data volume so they can be created/rotated entirely from
the app UI. The MCP server reads them **live on each request**, so configuring or
rotating in the UI takes effect immediately — the MCP container needs no secrets at
deploy time. Environment variables (``POCT_MCP_AUTH_TOKEN`` /
``POCT_MCP_ALLOWED_HOSTS``) still override the files for remote MCP hosts that can't
see the volume.

This is distinct from the *outbound* token in :mod:`app.services.mcp_token` (which
the MCP server uses to call the app's REST API).
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

GATEWAY_TOKEN_FILENAME = "mcp_gateway_token"
ALLOWED_HOSTS_FILENAME = "mcp_allowed_hosts"
GATEWAY_TOKEN_PREFIX = "poctgw_"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def gateway_token_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / GATEWAY_TOKEN_FILENAME


def allowed_hosts_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / ALLOWED_HOSTS_FILENAME


# ---------------------------------------------------------------------------
# Gateway token
# ---------------------------------------------------------------------------


def read_gateway_token(settings: Settings | None = None) -> str | None:
    """Resolve the inbound gateway token: env override, then the volume file."""
    env = os.environ.get("POCT_MCP_AUTH_TOKEN")
    if env:
        return env
    path = gateway_token_path(settings)
    if not path.exists():
        return None
    return path.read_text().strip() or None


def rotate_gateway_token(settings: Settings | None = None) -> str:
    """Generate, persist, and return a new gateway token (plaintext, shown once)."""
    settings = settings or get_settings()
    settings.ensure_data_dir()
    token = f"{GATEWAY_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    path = gateway_token_path(settings)
    path.write_text(token)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    log.info("mcp_gateway_token_rotated")
    return token


def clear_gateway_token(settings: Settings | None = None) -> bool:
    """Remove the gateway token file. Returns True if one existed."""
    path = gateway_token_path(settings)
    if not path.exists():
        return False
    path.unlink(missing_ok=True)
    log.info("mcp_gateway_token_cleared")
    return True


# ---------------------------------------------------------------------------
# Allowed hosts
# ---------------------------------------------------------------------------


def _split(value: str | None) -> list[str]:
    return [h.strip() for h in (value or "").split(",") if h.strip()]


def read_allowed_hosts(settings: Settings | None = None) -> list[str]:
    """Resolve allowed Host headers: env override, then the volume file, then []."""
    env = os.environ.get("POCT_MCP_ALLOWED_HOSTS")
    if env is not None and env.strip():
        return _split(env)
    path = allowed_hosts_path(settings)
    if path.exists():
        return _split(path.read_text())
    return []


def set_allowed_hosts(value: str, settings: Settings | None = None) -> list[str]:
    """Persist the allowed-hosts list (comma-separated). Empty clears it."""
    settings = settings or get_settings()
    hosts = _split(value)
    path = allowed_hosts_path(settings)
    if not hosts:
        path.unlink(missing_ok=True)
        return []
    settings.ensure_data_dir()
    path.write_text(",".join(hosts))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return hosts


def host_allowed(host: str, patterns: list[str]) -> bool:
    """Return True if `host` (a Host header, maybe with :port) matches a pattern.

    Empty `patterns` allows any host. Patterns support an exact match, a
    ``host:*`` port wildcard, a bare ``*`` (allow all), and a port-less host that
    matches the same host on any port.
    """
    if not patterns:
        return True
    h = host.lower()
    h_noport = h.rsplit(":", 1)[0] if (":" in h and not h.startswith("[")) else h
    for raw in patterns:
        p = raw.lower()
        if p == "*" or p == h:
            return True
        if p.endswith(":*"):
            base = p[:-2]
            if h == base or h.startswith(base + ":"):
                return True
        elif ":" not in p and p == h_noport:
            return True
    return False


# ---------------------------------------------------------------------------
# Status (for the UI)
# ---------------------------------------------------------------------------


def status(settings: Settings | None = None) -> dict[str, object]:
    """Describe inbound config for the settings page (never the secret itself)."""
    path = gateway_token_path(settings)
    token = path.read_text().strip() if path.exists() else ""
    return {
        "configured": bool(token),
        "prefix": (token[:14] if token else None),
        "token_path": str(path),
        "allowed_hosts": _split(
            allowed_hosts_path(settings).read_text()
            if allowed_hosts_path(settings).exists()
            else ""
        ),
    }
