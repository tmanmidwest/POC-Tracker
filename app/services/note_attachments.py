"""Note attachment file storage helpers.

Files attached to project notes (PDFs, Office docs, images) live on disk under
<data_dir>/note_attachments; the DB row holds the metadata. Mirrors the
screenshot store — blobs stay off the DB to keep backups/exports small.

Validation is by file extension rather than the browser-supplied content type,
since browsers report Office formats (.docx/.xlsx) inconsistently.
"""

from __future__ import annotations

import logging
import mimetypes
import secrets
from pathlib import Path

from app.config import get_settings
from app.models import NoteAttachment

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
}
MAX_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB


def attachments_dir() -> Path:
    """Return (creating if needed) the directory where note files are stored."""
    d = get_settings().data_dir / "note_attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_allowed(original_filename: str | None) -> bool:
    """Whether a filename's extension is an accepted attachment type."""
    return Path(original_filename or "").suffix.lower() in ALLOWED_EXTENSIONS


def store_bytes(content: bytes, original_filename: str | None) -> str:
    """Write file bytes to disk under a random name, preserving the original
    extension. Returns the stored filename."""
    ext = Path(original_filename or "").suffix.lower()
    stored = f"{secrets.token_urlsafe(16)}{ext}"
    (attachments_dir() / stored).write_bytes(content)
    return stored


def content_type_for(att: NoteAttachment) -> str:
    """Best-effort MIME type for serving — stored value, else guessed from name."""
    if att.content_type:
        return att.content_type
    guessed, _ = mimetypes.guess_type(att.original_filename or att.stored_filename)
    return guessed or "application/octet-stream"


def path_for(att: NoteAttachment) -> Path:
    """Absolute path to an attachment's file on disk."""
    return attachments_dir() / att.stored_filename


def delete_file(att: NoteAttachment) -> None:
    """Remove an attachment's file from disk. Never raises."""
    try:
        path_for(att).unlink(missing_ok=True)
    except OSError:
        log.warning("note_attachment_file_delete_failed", extra={"id": att.id})
