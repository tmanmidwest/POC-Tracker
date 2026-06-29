"""Server-side PDF rendering for the single-project report.

The report HTML is rendered from the same Jinja template used on screen, then
WeasyPrint turns it into a PDF. Screenshot ``<img>`` URLs are resolved to files
on disk via a custom url_fetcher, so WeasyPrint never has to authenticate
against the app's image routes. WeasyPrint is imported lazily so the app (and
the test suite) can run in environments where its system libraries aren't
installed — only the PDF endpoints require it.
"""

from __future__ import annotations

import logging
import struct
import zlib
from typing import Any
from urllib.parse import urlparse

from app.models import Project
from app.services import screenshots as screenshot_store
from app.ui.templating import templates

log = logging.getLogger(__name__)

# Base URL the report's root-relative image src attributes resolve against.
_BASE_URL = "https://report.local/"

def _make_blank_png() -> bytes:
    """A valid 1x1 white PNG, built byte-for-byte so it can never be a
    truncated/invalid stream (which would crash the whole PDF render)."""

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit, RGB
    idat = zlib.compress(b"\x00\xff\xff\xff")  # filter byte + one white pixel
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


# Substituted when a screenshot file is missing so one broken image never
# fails the entire render.
_BLANK_PNG = _make_blank_png()


def render_report_html(context: dict[str, Any]) -> str:
    """Render the project report template to an HTML string (no request)."""
    return templates.get_template("reports/project.html").render(context)


def render_library_html(context: dict[str, Any]) -> str:
    """Render the use-case library report template to an HTML string."""
    return templates.get_template("reports/library.html").render(context)


def library_pdf(html: str) -> bytes:
    """Render the library report HTML to PDF bytes (no images to embed)."""
    from weasyprint import HTML  # lazy: only needed when actually exporting

    return HTML(string=html, base_url=_BASE_URL).write_pdf()


def _screenshot_map(project: Project) -> dict[str, Any]:
    """Map each screenshot's serve-path to its on-disk file + content type."""
    out: dict[str, Any] = {}
    for uc in project.use_cases:
        for shot in uc.screenshots:
            out[f"/ui/projects/screenshots/{shot.id}"] = (
                screenshot_store.path_for(shot),
                shot.content_type or "image/png",
            )
    return out


def project_report_pdf(project: Project, html: str) -> bytes:
    """Render the report HTML to PDF bytes, embedding screenshots from disk."""
    from weasyprint import HTML  # lazy: only needed when actually exporting

    smap = _screenshot_map(project)

    def fetcher(url: str) -> dict[str, Any]:
        path = urlparse(url).path
        hit = smap.get(path)
        if hit:
            file_path, ctype = hit
            try:
                return {"string": file_path.read_bytes(), "mime_type": ctype}
            except OSError:
                log.warning("report_pdf_screenshot_missing", extra={"path": path})
        return {"string": _BLANK_PNG, "mime_type": "image/png"}

    return HTML(string=html, base_url=_BASE_URL, url_fetcher=fetcher).write_pdf()
