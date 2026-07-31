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
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.services import system_config

log = logging.getLogger(__name__)

TIMEOUT = 8.0  # seconds per source; the whole chain stays well under a page timeout
_MIN_BYTES = 100  # anything smaller is a blank/1px placeholder — treat as a miss


@dataclass
class LogoFetchResult:
    """A fetched logo plus the full trail of what was tried to get it."""

    data: bytes
    source: str  # "brandfetch:logo" | "brandfetch:icon" | "favicon"
    attempts: list[str]  # ordered, human-readable outcome of every source tried


class LogoFetchError(Exception):
    """Raised when no source yields a usable logo image.

    Carries ``attempts`` — the same ordered trail as a successful result — so the
    caller can record exactly what was tried and why each source was rejected.
    """

    def __init__(self, message: str, attempts: list[str] | None = None) -> None:
        super().__init__(message)
        self.attempts: list[str] = attempts or []


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


def fetch(website: str | None, *, referer: str | None = None) -> LogoFetchResult:
    """Locate a logo for ``website`` and return it with the full attempt trail.

    Tries, in order: Brandfetch ``logo`` → Brandfetch ``icon`` (both only when a
    client ID is configured) → the public favicon. Every source appends a line to
    ``attempts`` recording its outcome — e.g. ``"brandfetch:logo → HTTP 403"`` or
    ``"favicon → ok (9700 B)"`` — so callers can log exactly what happened, not
    just which source won. Raises :class:`LogoFetchError` (carrying the same trail)
    when the website is unusable or no source returns an image.

    ``referer`` is sent as the ``Referer`` header on Brandfetch requests: the Logo
    API is built for browser ``<img>`` embedding and refuses server-side requests
    that carry no origin with HTTP 403. Pass this app's public base URL.
    """
    domain = domain_from_website(website)
    if not domain:
        raise LogoFetchError("Enter a valid website first (e.g. https://acme.com).")

    attempts: list[str] = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        client_id = system_config.brandfetch_client_id()
        if client_id:
            # Prefer the full wordmark ("logo"); fall back to the square mark
            # ("icon") when a brand has no standalone logo. fallback=404 makes
            # Brandfetch 404 on a genuine miss instead of returning a generic
            # auto-generated letter avatar that we'd wrongly accept.
            for kind in ("logo", "icon"):
                data = _try_brandfetch(client, domain, client_id, kind, referer, attempts)
                if data:
                    return _result(data, f"brandfetch:{kind}", attempts, domain)
        else:
            attempts.append("brandfetch → skipped (no client ID configured)")
        data = _try_google_favicon(client, domain, attempts)
        if data:
            return _result(data, "favicon", attempts, domain)

    log.info("customer_logo_fetch_miss", extra={"domain": domain, "attempts": attempts})
    trail = "; ".join(attempts) if attempts else "no logo found"
    raise LogoFetchError(f"Couldn't find a logo for {domain} ({trail}).", attempts)


def _result(
    data: bytes, source: str, attempts: list[str], domain: str
) -> LogoFetchResult:
    log.info(
        "customer_logo_fetch_hit",
        extra={"domain": domain, "source": source, "attempts": attempts},
    )
    return LogoFetchResult(data=data, source=source, attempts=attempts)


def _try_brandfetch(
    client: httpx.Client, domain: str, client_id: str, kind: str,
    referer: str | None, attempts: list[str],
) -> bytes | None:
    # Brandfetch Logo API: type ("logo"/"icon") and size are PATH segments, the
    # client ID is the ?c= query. Sized to 512 (save() caps there anyway); the
    # default WebP response decodes fine in Pillow. A Referer header is required —
    # without it Brandfetch returns 403 (the API is built for browser embedding).
    url = (
        f"https://cdn.brandfetch.io/{domain}/w/512/h/512/{kind}"
        f"?c={client_id}&fallback=404"
    )
    headers = {"Referer": referer} if referer else None
    return _get_image(client, url, f"brandfetch:{kind}", attempts, headers=headers)


def _try_google_favicon(
    client: httpx.Client, domain: str, attempts: list[str]
) -> bytes | None:
    # The icon shown on the site's browser tab — Google fetches and caches it.
    url = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    return _get_image(client, url, "favicon", attempts)


def _get_image(
    client: httpx.Client, url: str, source: str, attempts: list[str],
    headers: dict[str, str] | None = None,
) -> bytes | None:
    """GET ``url``; append the outcome to ``attempts`` and return bytes on success."""
    try:
        resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        attempts.append(f"{source} → error ({exc.__class__.__name__})")
        return None
    if resp.status_code != 200:
        attempts.append(f"{source} → HTTP {resp.status_code}")
        return None
    ctype = resp.headers.get("content-type", "")
    if "image" not in ctype:
        attempts.append(f"{source} → not an image ({ctype or 'unknown'})")
        return None
    data = resp.content
    if len(data) < _MIN_BYTES:
        attempts.append(f"{source} → empty image")
        return None
    attempts.append(f"{source} → ok ({len(data)} B)")
    return data
