"""Helpers for the HTML UI layer.

`flash()` adds a one-shot toast message that the next request will render.
`get_flashes()` pulls them out of the session for rendering and clears them.
"""

from __future__ import annotations

from typing import Literal

from fastapi import Request

FlashLevel = Literal["success", "error", "info", "warning"]

_FLASH_KEY = "_flashes"


def flash(request: Request, message: str, level: FlashLevel = "info") -> None:
    """Queue a flash message to be shown on the next rendered page."""
    flashes = request.session.get(_FLASH_KEY, [])
    flashes.append({"message": message, "level": level})
    request.session[_FLASH_KEY] = flashes


def get_flashes(request: Request) -> list[dict[str, str]]:
    """Pop all queued flashes from the session."""
    flashes = request.session.get(_FLASH_KEY, [])
    request.session[_FLASH_KEY] = []
    return list(flashes)
