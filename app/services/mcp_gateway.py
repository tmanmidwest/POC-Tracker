"""Manage the MCP server's *inbound* host allow-list from the UI.

A gateway (Saviynt, another project, etc.) calls the MCP server with two pieces of
inbound config:

* a **bearer token** — now multiple named, revocable tokens managed in
  :mod:`app.services.mcp_gateway_tokens`, and
* an optional **allowed-hosts** list — Host headers permitted past a DNS-rebinding
  check (empty = allow any host; bearer auth is the primary control), handled here.

The allowed-hosts list is persisted on the data volume so it can be set/cleared
entirely from the app UI. The MCP server reads it **live on each request**, so
changes take effect immediately — the MCP container needs no secrets at deploy
time. ``POCT_MCP_ALLOWED_HOSTS`` still overrides the file for remote MCP hosts that
can't see the volume.

This is distinct from the *outbound* token in :mod:`app.services.mcp_token` (which
the MCP server uses to call the app's REST API).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

ALLOWED_HOSTS_FILENAME = "mcp_allowed_hosts"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def allowed_hosts_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / ALLOWED_HOSTS_FILENAME


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
    """Describe inbound host config for the settings page."""
    return {
        "allowed_hosts": _split(
            allowed_hosts_path(settings).read_text()
            if allowed_hosts_path(settings).exists()
            else ""
        ),
    }
