"""Storage for the admin-supplied readout deck branding: a base template and a logo.

* **Template** — a `.pptx` (or `.potx`) whose slide master/theme/fonts sit
  underneath the generated readout slides (see report_pptx). A `.potx` is a
  PowerPoint *template* package; it's structurally a `.pptx` but its main part
  carries the template content-type, which python-pptx refuses to open — so on
  upload we normalize that content-type and store a plain `.pptx`.
* **Logo** — an image stamped in the corner of every generated slide. Uploads are
  validated and normalized to PNG.

Both are single optional files under ``<data_dir>`` — there's only ever one of
each, so no DB rows are needed. Uploads are validated by actually opening them.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

from pptx import Presentation

from app.config import get_settings

log = logging.getLogger(__name__)

_TEMPLATE_FILE = "readout_template.pptx"
_LOGO_FILE = "readout_logo.png"
MAX_TEMPLATE_BYTES = 25 * 1024 * 1024  # 25 MB — decks with imagery can be large
MAX_LOGO_BYTES = 5 * 1024 * 1024       # 5 MB

# The main-part content types for a presentation vs. a template package. A .potx
# differs from a .pptx essentially only by this override in [Content_Types].xml.
_PRESENTATION_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
_TEMPLATE_CT = "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"


class TemplateError(Exception):
    """Raised when an upload is missing, too large, or not a valid file."""


# ---------------------------------------------------------------------------
# Deck template (.pptx / .potx)
# ---------------------------------------------------------------------------


def template_path() -> Path:
    return get_settings().data_dir / _TEMPLATE_FILE


def has_template() -> bool:
    return template_path().exists()


def template_path_if_present() -> str | None:
    p = template_path()
    return str(p) if p.exists() else None


def _opens_as_pptx(data: bytes) -> bool:
    try:
        Presentation(io.BytesIO(data))
        return True
    except Exception:
        return False


def _normalize_potx(data: bytes) -> bytes:
    """Rewrite a template package's content-type to a presentation's.

    Returns the input unchanged if it isn't a zip we can read; the caller
    re-validates the result, so a no-op just surfaces as "invalid file".
    """
    try:
        src = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            body = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                body = body.replace(_TEMPLATE_CT.encode(), _PRESENTATION_CT.encode())
            dst.writestr(item, body)
    return out.getvalue()


def save_template(data: bytes) -> None:
    """Validate ``data`` as a .pptx/.potx and store it as a .pptx, replacing any existing."""
    if not data:
        raise TemplateError("The uploaded file is empty.")
    if len(data) > MAX_TEMPLATE_BYTES:
        raise TemplateError("The template is too large (max 25 MB).")

    stored = data
    if not _opens_as_pptx(stored):
        # Likely a .potx — normalize the content-type and re-check.
        stored = _normalize_potx(data)
        if not _opens_as_pptx(stored):
            raise TemplateError(
                "That doesn't look like a valid PowerPoint template (.pptx or .potx) file."
            )

    _atomic_write(template_path(), stored)
    log.info("readout_template_saved", extra={"bytes": len(stored)})


def delete_template() -> bool:
    """Remove the stored template. Returns True if one was present."""
    return _unlink(template_path(), "readout_template_deleted")


# ---------------------------------------------------------------------------
# Brand logo
# ---------------------------------------------------------------------------


def logo_path() -> Path:
    return get_settings().data_dir / _LOGO_FILE


def has_logo() -> bool:
    return logo_path().exists()


def logo_path_if_present() -> str | None:
    p = logo_path()
    return str(p) if p.exists() else None


def save_logo(data: bytes) -> None:
    """Validate ``data`` as an image and store it as PNG, replacing any existing."""
    if not data:
        raise TemplateError("The uploaded file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise TemplateError("The logo is too large (max 5 MB).")
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise TemplateError("That doesn't look like a valid image file.") from exc

    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG")  # normalize to PNG (keeps transparency)
    _atomic_write(logo_path(), buf.getvalue())
    log.info("readout_logo_saved", extra={"bytes": len(data)})


def delete_logo() -> bool:
    """Remove the stored logo. Returns True if one was present."""
    return _unlink(logo_path(), "readout_logo_deleted")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_write(dest: Path, data: bytes) -> None:
    get_settings().ensure_data_dir()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)  # atomic swap so a reader never sees a half-written file


def _unlink(path: Path, log_event: str) -> bool:
    if path.exists():
        path.unlink()
        log.info(log_event)
        return True
    return False
