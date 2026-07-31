"""Fetch a customer logo from their website's domain.

Source chain: the Brandfetch Logo API (when ``POCT_BRANDFETCH_CLIENT_ID`` is
configured) → Google's public favicon service. Both are requests to *known*
third-party hosts that themselves fetch the customer's origin, so we never make a
server-side request to a user-supplied URL — there is no SSRF surface here, and no
private-network reachability to guard against.

The returned bytes are raster image data suitable for
:func:`app.services.customer_logo.save`, which does the real validation (Pillow
decode, downscale, re-encode to a bounded PNG). This module only *locates* an
image and hands the bytes over.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from app.services import system_config

log = logging.getLogger(__name__)

TIMEOUT = 8.0  # seconds per source; the whole chain stays well under a page timeout
_MIN_BYTES = 100  # anything smaller is a blank/1px placeholder — treat as a miss


class LogoFetchError(Exception):
    """Raised when no source yields a usable logo image."""


def domain_from_website(website: str | None) -> str | None:
    """Extract a bare registrable host (``acme.com``) from a website value.

    Accepts values with or without a scheme and a leading ``www.``. Returns None
    when the input can't plausibly be a domain.
    """
    if not website:
        return None
    raw = website.strip()
    if not raw:
        return None
    if "//" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or " " in host:
        return None
    return host or None


def fetch(website: str | None) -> bytes:
    """Return logo image bytes for ``website``, trying each source in turn.

    Raises :class:`LogoFetchError` with a user-facing message when the website is
    unusable or no source returns an image.
    """
    domain = domain_from_website(website)
    if not domain:
        raise LogoFetchError("Enter a valid website first (e.g. https://acme.com).")

    errors: list[str] = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        client_id = system_config.brandfetch_client_id()
        if client_id:
            data = _try_brandfetch(client, domain, client_id, errors)
            if data:
                return data
        data = _try_google_favicon(client, domain, errors)
        if data:
            return data

    detail = "; ".join(errors) if errors else "no logo found"
    log.info("customer_logo_fetch_miss", extra={"domain": domain, "detail": detail})
    raise LogoFetchError(f"Couldn't find a logo for {domain} ({detail}).")


def _try_brandfetch(
    client: httpx.Client, domain: str, client_id: str, errors: list[str]
) -> bytes | None:
    # Brandfetch returns the best available raster for the domain; sized down here
    # so we transfer a small image (save() caps it at 512px anyway).
    url = f"https://cdn.brandfetch.io/{domain}/w/512/h/512?c={client_id}"
    return _get_image(client, url, "brandfetch", errors)


def _try_google_favicon(
    client: httpx.Client, domain: str, errors: list[str]
) -> bytes | None:
    # The icon shown on the site's browser tab — Google fetches and caches it.
    url = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    return _get_image(client, url, "favicon", errors)


def _get_image(
    client: httpx.Client, url: str, source: str, errors: list[str]
) -> bytes | None:
    """GET ``url`` and return its bytes if it's a non-trivial image, else None."""
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        errors.append(f"{source}: {exc.__class__.__name__}")
        return None
    if resp.status_code != 200:
        errors.append(f"{source}: HTTP {resp.status_code}")
        return None
    if "image" not in resp.headers.get("content-type", ""):
        errors.append(f"{source}: not an image")
        return None
    data = resp.content
    if len(data) < _MIN_BYTES:
        errors.append(f"{source}: empty image")
        return None
    log.info("customer_logo_fetch_hit", extra={"source": source, "bytes": len(data)})
    return data
