"""Password hashing helpers using bcrypt via passlib."""

from __future__ import annotations

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash of the given plaintext password."""
    return _pwd_context.hash(plaintext)


def verify_password(plaintext: str, password_hash: str) -> bool:
    """Return True if the plaintext password matches the stored hash."""
    try:
        return _pwd_context.verify(plaintext, password_hash)
    except (ValueError, TypeError):
        return False
