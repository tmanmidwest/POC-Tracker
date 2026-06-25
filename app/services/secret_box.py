"""Reversible encryption for secrets that must be reused (not just compared).

API keys and OAuth *client* secrets we issue are stored as one-way SHA-256
hashes — we only ever need to verify them. An OIDC provider's client secret is
different: we have to send it back to the identity provider on every token
exchange, so it must be recoverable. We encrypt it at rest with Fernet
(AES-128-CBC + HMAC) using a key persisted alongside the other app secrets.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(get_settings().get_or_create_provider_secret_key())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage. Returns a URL-safe token string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """Decrypt a value produced by :func:`encrypt_secret`."""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
