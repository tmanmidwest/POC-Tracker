"""Configurable app branding: name, accent color, and icon.

Branding lives in a single-row table (see app.models.app_branding). Because the
brand name/icon are rendered on every page, the resolved values are cached in a
module-level dict and refreshed only when an admin saves changes (call
``invalidate()`` after a write).

Icons are picked from a fixed preset gallery rather than uploaded, so the SVG
markup is always trusted (no user-supplied SVG = no stored-XSS surface). Each
preset is the *inner* markup of a 24x24 ``viewBox`` drawn with
``stroke="currentColor"`` so the brand color flows through via CSS ``color``.
"""

from __future__ import annotations

from urllib.parse import quote

# Theme accent (matches --primary in app.css); used when no color is set.
DEFAULT_COLOR = "#EF9F27"  # quest gold
DEFAULT_NAME = "questlog"
DEFAULT_ICON = "questlog"
# Small sub-header under the brand name (sidebar + login). Empty hides it.
DEFAULT_TAGLINE = "Every POC is a quest — track it, win it."

# key -> {"label": human name, "svg": inner SVG markup for a 0 0 24 24 viewBox}
# Most presets are stroke glyphs (stroke="currentColor"); the Questlog mark is a
# fill glyph — its paths set fill="currentColor" so they show through the parent
# <svg fill="none">. The <g transform> normalizes the mark's native 96-unit
# geometry into the shared 24x24 viewBox.
ICON_PRESETS: dict[str, dict[str, str]] = {
    "questlog": {
        "label": "Questlog mark",
        "svg": (
            '<g transform="translate(12 12) scale(0.385) translate(-48 -48)">'
            '<path d="M40 22 L56 22 L53 54 L43 54 Z" fill="currentColor"/>'
            '<polygon points="48,60 55,67 48,74 41,67" fill="currentColor"/>'
            "</g>"
        ),
    },
    "suitcase": {
        "label": "Suitcase",
        "svg": (
            '<rect x="3" y="6" width="18" height="15" rx="2"/>'
            '<rect x="9" y="3" width="6" height="4" rx="1"/>'
            '<path d="M3 13h18"/><circle cx="12" cy="17" r="1.5"/>'
        ),
    },
    "building": {
        "label": "Building",
        "svg": (
            '<rect x="4" y="3" width="16" height="18" rx="1"/>'
            '<path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2"/>'
            '<path d="M10 21v-3h4v3"/>'
        ),
    },
    "users": {
        "label": "People",
        "svg": (
            '<circle cx="9" cy="8" r="3"/>'
            '<path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/>'
            '<path d="M16 5.3a3 3 0 0 1 0 5.4"/>'
            '<path d="M18 14.2c1.8.8 3 2.6 3 4.8"/>'
        ),
    },
    "id-badge": {
        "label": "ID badge",
        "svg": (
            '<rect x="4" y="3" width="16" height="18" rx="2"/>'
            '<path d="M9 3v2h6V3"/><circle cx="12" cy="10" r="2.2"/>'
            '<path d="M8.5 17c0-1.9 1.6-3.2 3.5-3.2s3.5 1.3 3.5 3.2"/>'
        ),
    },
    "shield": {
        "label": "Shield",
        "svg": '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/>',
    },
    "chart": {
        "label": "Bar chart",
        "svg": '<path d="M3 21h18"/><path d="M6 21v-7M11 21V6M16 21v-9"/>',
    },
    "globe": {
        "label": "Globe",
        "svg": (
            '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
            '<path d="M12 3c2.6 2.4 2.6 15.6 0 18M12 3c-2.6 2.4-2.6 15.6 0 18"/>'
        ),
    },
    "spark": {
        "label": "Spark",
        "svg": (
            '<path d="M12 3l2.2 6.3L20.5 12l-6.3 2.2L12 21l-2.2-6.8L3.5 12l6.3-2.7z"/>'
        ),
    },
}


def resolve_icon_key(key: str | None) -> str:
    """Return a valid preset key, falling back to the default."""
    return key if key in ICON_PRESETS else DEFAULT_ICON


def icon_svg(key: str | None) -> str:
    """Return the inner SVG markup for an icon preset key."""
    return ICON_PRESETS[resolve_icon_key(key)]["svg"]


# The Questlog app icon: the gold mark on its fixed midnight tile. Used as the
# favicon whenever the Questlog mark is selected — a recognizable brand tile that,
# per the brand rules, is NOT recolored per theme.
_QUESTLOG_FAVICON = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 96'>"
    "<rect width='96' height='96' rx='22' fill='#1c1b18'/>"
    "<path d='M40 22 L56 22 L53 54 L43 54 Z' fill='#EF9F27'/>"
    "<polygon points='48,60 55,67 48,74 41,67' fill='#EF9F27'/></svg>"
)


def favicon_data_uri(key: str | None, color: str) -> str:
    """Build a data: URI for the chosen icon in the brand color."""
    if resolve_icon_key(key) == "questlog":
        return "data:image/svg+xml," + quote(_QUESTLOG_FAVICON)
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' "
        f"fill='none' stroke='{color}' stroke-width='1.5' stroke-linecap='round' "
        f"stroke-linejoin='round'>{icon_svg(key)}</svg>"
    )
    return "data:image/svg+xml," + quote(svg)


_cache: dict[str, str] | None = None


def invalidate() -> None:
    """Drop the cached branding so the next render reloads from the DB."""
    global _cache
    _cache = None


def _load() -> dict[str, str]:
    """Read the singleton branding row (if any) and resolve display values."""
    from app.db import get_session_factory
    from app.models import AppBranding

    name, color, icon_key = DEFAULT_NAME, "", DEFAULT_ICON
    tagline = DEFAULT_TAGLINE
    db = get_session_factory()()
    try:
        row = db.get(AppBranding, 1)
        if row is not None:
            name = row.brand_name or DEFAULT_NAME
            color = row.brand_color or ""
            icon_key = resolve_icon_key(row.icon_key)
            # An explicit empty tagline hides it; only fall back when unset (None).
            tagline = row.brand_tagline if row.brand_tagline is not None else DEFAULT_TAGLINE
    except Exception:
        # Branding is non-critical chrome — never let a DB hiccup break a page.
        pass
    finally:
        db.close()

    effective_color = color or DEFAULT_COLOR
    return {
        "name": name,
        # Empty string means "no explicit override" so templates can keep the
        # theme default; effective_color is always a concrete hex for the icon.
        "color": color,
        "tagline": tagline,
        "icon_key": icon_key,
        "icon_svg": icon_svg(icon_key),
        "favicon": favicon_data_uri(icon_key, effective_color),
    }


def current_branding() -> dict[str, str]:
    """Return cached, render-ready branding values for template context."""
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache
