"""Extract plain text from an uploaded document for the requirements importer.

Supports PDF (via ``pypdf``) and Word ``.docx`` (via stdlib ``zipfile``/XML — no
extra dependency). Anything else is decoded as UTF-8 text. Raises
:class:`TextExtractError` with a user-facing message on failure.
"""

from __future__ import annotations

import io
import logging
import xml.etree.ElementTree as ET
import zipfile

log = logging.getLogger(__name__)

# WordprocessingML namespace for .docx body text.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class TextExtractError(Exception):
    """Raised when a document can't be read into text."""


def extract_text(filename: str | None, content: bytes, content_type: str | None) -> str:
    """Return the text of an uploaded file. Empty bytes -> empty string."""
    if not content:
        return ""
    name = (filename or "").lower()
    ctype = (content_type or "").lower()

    if name.endswith(".pdf") or ctype == "application/pdf":
        return _from_pdf(content)
    if name.endswith(".docx") or "wordprocessingml" in ctype:
        return _from_docx(content)
    # Plain text (or unknown) — best-effort decode.
    return content.decode("utf-8", errors="ignore")


def _from_pdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise TextExtractError("PDF support is not installed on the server.") from exc
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        log.warning("pdf_extract_failed", extra={"error": str(exc)})
        raise TextExtractError(
            "Could not read that PDF. If it's a scanned image, paste the text instead."
        ) from exc
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text.strip():
        raise TextExtractError(
            "No selectable text found in that PDF (it may be a scan). Paste the text instead."
        )
    return text


def _from_docx(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            xml = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise TextExtractError("Could not read that Word document.") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise TextExtractError("Could not parse that Word document.") from exc

    paragraphs: list[str] = []
    for para in root.iter(f"{_W}p"):
        runs = [t.text for t in para.iter(f"{_W}t") if t.text]
        line = "".join(runs).strip()
        if line:
            paragraphs.append(line)
    text = "\n".join(paragraphs)
    if not text.strip():
        raise TextExtractError("That Word document appears to have no text.")
    return text
