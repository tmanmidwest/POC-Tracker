"""Screenshot file storage helpers.

Image bytes live on disk under <data_dir>/screenshots; the DB row holds the
metadata. Keeping blobs off the DB matches the app's named-volume model and
keeps backups/exports small.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from app.config import get_settings
from app.models import Screenshot

log = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


def screenshots_dir() -> Path:
    """Return (creating if needed) the directory where screenshots are stored."""
    d = get_settings().data_dir / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_bytes(content: bytes, content_type: str | None) -> str:
    """Write image bytes to disk under a random name. Returns the stored filename."""
    ext = ALLOWED_CONTENT_TYPES.get(content_type or "", "")
    stored = f"{secrets.token_urlsafe(16)}{ext}"
    (screenshots_dir() / stored).write_bytes(content)
    return stored


def path_for(shot: Screenshot) -> Path:
    """Absolute path to a screenshot's file on disk."""
    return screenshots_dir() / shot.stored_filename


def delete_file(shot: Screenshot) -> None:
    """Remove a screenshot's file from disk. Never raises."""
    try:
        path_for(shot).unlink(missing_ok=True)
    except OSError:
        log.warning("screenshot_file_delete_failed", extra={"id": shot.id})
