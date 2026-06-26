"""Rich-text helpers for project notes.

Notes can now contain limited HTML (pasted from Word, etc.). We store the
sanitized HTML alongside a plain-text rendering: the HTML is rendered in the UI
and PDF reports, while the plain text keeps the existing ``body``/``notes``
columns meaningful for search and as a safe fallback.

Sanitization uses ``nh3`` (a Rust-backed HTML sanitizer) with a deliberately
small allow-list — only basic formatting and links survive. Everything else
(scripts, styles, classes, Word ``mso-`` cruft, inline event handlers, unsafe
URL schemes) is stripped, so the HTML is safe to render with ``| safe``.
"""

from __future__ import annotations

import re
from html import unescape

import nh3

# Basic formatting + lists + links. Headings/tables intentionally excluded.
ALLOWED_TAGS: set[str] = {
    "p",
    "br",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "a",
}
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {"a": {"href", "title"}}

# Tags whose close (or self-close) should become a line break in plain text.
_BLOCK_CLOSE_RE = re.compile(r"(?i)</\s*(p|li|ul|ol|div|h[1-6]|tr)\s*>")
_BR_RE = re.compile(r"(?i)<\s*br\s*/?>")
_LI_OPEN_RE = re.compile(r"(?i)<\s*li[^>]*>")
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def sanitize_note_html(raw: str | None) -> str | None:
    """Sanitize user-submitted HTML down to the allow-list.

    Returns the cleaned HTML, or ``None`` if it carries no visible text
    (e.g. an empty editor that submits ``<p><br></p>``).
    """
    if not raw or not raw.strip():
        return None
    cleaned = nh3.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer nofollow",
    )
    # Drop content that sanitizes to whitespace-only (no real text to show).
    if not html_to_text(cleaned):
        return None
    return cleaned


def html_to_text(html: str | None) -> str | None:
    """Render HTML to a readable plain-text approximation.

    Used for the stored ``body``/``notes`` fallback (search, exports, and old
    plain-text notes). List items become bullet lines and block boundaries
    become newlines; remaining tags are stripped and entities unescaped.
    """
    if not html:
        return None
    text = _BR_RE.sub("\n", html)
    text = _LI_OPEN_RE.sub("• ", text)  # bullet prefix for list items
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _MULTI_BLANK_RE.sub("\n\n", text).strip()
    return text or None
