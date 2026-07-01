"""Shared httpx client for Google API calls.

Centralized so tests can inject a mock transport once and have it cover both the
OAuth token exchange (google_oauth) and the Tasks REST calls (google_tasks_sync)
— no real network in tests, the same approach the MCP server uses.
"""

from __future__ import annotations

import httpx

_client: httpx.Client | None = None


def client() -> httpx.Client:
    """Return the shared httpx client, creating it lazily."""
    global _client
    if _client is None:
        _client = httpx.Client(timeout=30.0)
    return _client


def set_client(new_client: httpx.Client | None) -> None:
    """Replace the shared client (tests inject an httpx.MockTransport client)."""
    global _client
    _client = new_client
