"""Per-customer logo storage.

Mirrors :mod:`app.services.report_template`'s logo handling: image bytes live on
disk under ``<data_dir>/customer_logos/<id>.png``, normalized to a bounded PNG on
upload. The presence of that file is the single source of truth for "this customer
has a logo" — no DB column, and therefore no migration touching the (FTS-triggered)
``customers`` table.

Only raster images are accepted: Pillow can't decode SVG, so a vector upload is
rejected with a friendly error — which is exactly the safety posture we want
(no user-supplied SVG reaching any render surface).
"""

from __future__ import annotations

import base64
import io
import logging
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB before normalization
MAX_EDGE = 512  # px — logos are small marks/wordmarks; keeps the data URI tiny


class LogoError(ValueError):
    """Raised for an empty, oversized, or non-image upload."""


def logos_dir() -> Path:
    d = get_settings().data_dir / "customer_logos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_for(customer_id: int) -> Path:
    return logos_dir() / f"{customer_id}.png"


def has_logo(customer_id: int) -> bool:
    return path_for(customer_id).exists()


def save(customer_id: int, data: bytes) -> None:
    """Validate ``data`` as a raster image and store it as a bounded PNG.

    Normalizing (decode → RGBA → downscale → re-encode PNG) also neutralizes a
    malformed-image payload: anything Pillow can't fully decode is rejected.
    """
    if not data:
        raise LogoError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise LogoError("That image is too large (max 5 MB).")

    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise LogoError(
            "That doesn't look like a valid image. Use a PNG, JPG, or WebP file."
        ) from exc

    img = img.convert("RGBA")
    img.thumbnail((MAX_EDGE, MAX_EDGE))  # in place; preserves aspect ratio
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    _atomic_write(path_for(customer_id), buf.getvalue())
    log.info("customer_logo_saved", extra={"customer_id": customer_id, "bytes": len(data)})


def delete(customer_id: int) -> bool:
    """Remove a customer's logo. Returns True if one was present."""
    p = path_for(customer_id)
    if p.exists():
        p.unlink()
        log.info("customer_logo_deleted", extra={"customer_id": customer_id})
        return True
    return False


def data_uri(customer_id: int) -> str | None:
    """A ``data:image/png;base64,…`` URI for the logo, or None if unset.

    Inlined into every render surface (project page, portal, reports) so no
    public/authed file-serving route is needed. Cached by path + mtime so a
    customers list with many logos doesn't re-read files each render.
    """
    p = path_for(customer_id)
    if not p.exists():
        return None
    try:
        return _cached_data_uri(str(p), p.stat().st_mtime_ns)
    except OSError:
        return None


@lru_cache(maxsize=256)
def _cached_data_uri(path: str, mtime_ns: int) -> str:
    raw = Path(path).read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _atomic_write(dest: Path, data: bytes) -> None:
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)  # atomic swap so a reader never sees a half-written file
