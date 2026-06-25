"""Token generation and hashing utilities for API keys and OAuth secrets.

We use SHA-256 hashes for storage (not bcrypt) because:
1. The tokens we generate already have full cryptographic randomness from
   `secrets.token_urlsafe()`, so we don't need bcrypt's salt+work-factor
   protection against weak passwords.
2. API key authentication runs on every request — bcrypt would be far too
   slow for that hot path.
3. SHA-256 of a high-entropy token is what most production API systems use
   (e.g., GitHub, Stripe, Slack).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

API_KEY_PREFIX = "poct_"
API_KEY_RANDOM_LEN = 32  # url-safe characters after the prefix

OAUTH_CLIENT_ID_PREFIX = "poct_client_"
OAUTH_CLIENT_ID_RANDOM_LEN = 16
OAUTH_CLIENT_SECRET_RANDOM_LEN = 32


def generate_api_key() -> tuple[str, str]:
    """Generate a new API key. Returns (full_key, key_prefix_for_display).

    Format: poct_<32 url-safe chars>
    The prefix returned is the first 14 characters (incl. `poct_`), suitable for
    showing the user "which key" without revealing the secret.
    """
    random_part = secrets.token_urlsafe(API_KEY_RANDOM_LEN)[:API_KEY_RANDOM_LEN]
    full_key = f"{API_KEY_PREFIX}{random_part}"
    prefix = full_key[:14]  # "poct_" + 8 chars
    return full_key, prefix


def generate_oauth_client_id() -> str:
    """Generate a new OAuth client_id."""
    random_part = secrets.token_urlsafe(OAUTH_CLIENT_ID_RANDOM_LEN)[
        :OAUTH_CLIENT_ID_RANDOM_LEN
    ]
    return f"{OAUTH_CLIENT_ID_PREFIX}{random_part}"


def generate_oauth_client_secret() -> str:
    """Generate a new OAuth client_secret."""
    return secrets.token_urlsafe(OAUTH_CLIENT_SECRET_RANDOM_LEN)[
        :OAUTH_CLIENT_SECRET_RANDOM_LEN
    ]


def hash_token(token: str) -> str:
    """Return the hex-encoded SHA-256 hash of a token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(plaintext: str, expected_hash: str) -> bool:
    """Constant-time comparison of a plaintext token against its stored hash."""
    return hmac.compare_digest(hash_token(plaintext), expected_hash)
