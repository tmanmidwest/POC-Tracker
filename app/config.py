"""Application configuration loaded from environment variables.

All settings are optional with sensible defaults. The POCT_ prefix is required
on every environment variable to avoid collisions.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="POCT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage ---
    data_dir: Path = Field(
        default=Path("/data"),
        description="Directory where the SQLite DB and signing keys live.",
    )

    # --- Bind ---
    bind_host: str = Field(default="0.0.0.0", description="Host to bind the HTTP server to.")
    bind_port: int = Field(default=8010, description="Port to bind the HTTP server to.")

    # --- Logging ---
    log_level: str = Field(
        default="INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR."
    )

    # --- Auth ---
    session_secret: str | None = Field(
        default=None,
        description="Cookie signing secret. Auto-generated and persisted if not set.",
    )
    session_max_age_seconds: int = Field(
        default=8 * 60 * 60,  # 8 hours
        description="Session cookie max age in seconds.",
    )
    initial_admin_username: str = Field(
        default="robbytheadmin", description="Username for the seeded admin account."
    )
    initial_admin_password: str = Field(
        default="N0nPr0dF0r$@viynt8",
        description="Default password for the seeded admin account.",
    )

    # --- OAuth / JWT ---
    oauth_default_token_lifetime_seconds: int = Field(
        default=3600,
        description="Default lifetime for OAuth access tokens.",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT signing algorithm.")

    # --- SSO / OIDC ---
    public_base_url: str | None = Field(
        default=None,
        description=(
            "Externally-reachable base URL of this app (e.g. https://hr.example.com). "
            "Used to build OIDC redirect URIs behind an HTTPS proxy. If unset, the "
            "redirect URI is derived from the incoming request."
        ),
    )

    # --- Audit / activity log ---
    audit_retention_days: int = Field(
        default=30,
        description=(
            "Delete audit/activity events older than this many days. Pruned at "
            "startup and once a day while running. Set to 0 to keep events forever."
        ),
    )

    # --- Backups ---
    backup_retention_count: int = Field(
        default=2,
        description=(
            "How many generated backup archives to keep on disk under "
            "<data_dir>/backups. Older ones are pruned after each new backup."
        ),
    )

    # --- App metadata ---
    app_name: str = Field(default="POC Tracker")
    app_version: str = Field(default="0.1.0")

    # --- Computed paths ---

    @property
    def database_path(self) -> Path:
        """Path to the SQLite database file."""
        return self.data_dir / "poct.db"

    @property
    def database_url(self) -> str:
        """SQLAlchemy connection string for the SQLite database."""
        return f"sqlite:///{self.database_path}"

    @property
    def jwt_signing_key_path(self) -> Path:
        """Path to the JWT signing key file."""
        return self.data_dir / "jwt_signing_key"

    @property
    def session_secret_path(self) -> Path:
        """Path to the persisted session secret file."""
        return self.data_dir / "session_secret"

    @property
    def provider_secret_key_path(self) -> Path:
        """Path to the key used to encrypt OIDC provider client secrets at rest."""
        return self.data_dir / "provider_secret_key"

    @property
    def initial_credentials_path(self) -> Path:
        """Path to the initial credentials marker file."""
        return self.data_dir / "INITIAL_CREDENTIALS.txt"

    @property
    def backups_dir(self) -> Path:
        """Directory holding generated backup archives (kept on the data volume)."""
        return self.data_dir / "backups"

    @property
    def restore_staging_dir(self) -> Path:
        """Directory where an uploaded restore archive is unpacked and held until
        the next startup applies it."""
        return self.data_dir / "restore_pending"

    @property
    def restore_marker_path(self) -> Path:
        """Marker file signalling a staged restore to apply on next startup."""
        return self.data_dir / "RESTORE_PENDING.json"

    @property
    def secret_key_paths(self) -> list[Path]:
        """The persisted secret-key files included in a backup so a restored
        instance can still decrypt provider secrets and validate sessions."""
        return [
            self.session_secret_path,
            self.jwt_signing_key_path,
            self.provider_secret_key_path,
        ]

    # --- Helpers ---

    def ensure_data_dir(self) -> None:
        """Create the data directory if it does not exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def get_or_create_session_secret(self) -> str:
        """Return the configured session secret, or generate and persist a new one."""
        if self.session_secret:
            return self.session_secret
        self.ensure_data_dir()
        if self.session_secret_path.exists():
            return self.session_secret_path.read_text().strip()
        new_secret = secrets.token_urlsafe(48)
        self.session_secret_path.write_text(new_secret)
        self.session_secret_path.chmod(0o600)
        return new_secret

    def get_or_create_jwt_signing_key(self) -> str:
        """Return the JWT signing key, generating and persisting it if missing."""
        self.ensure_data_dir()
        if self.jwt_signing_key_path.exists():
            return self.jwt_signing_key_path.read_text().strip()
        new_key = secrets.token_urlsafe(48)
        self.jwt_signing_key_path.write_text(new_key)
        self.jwt_signing_key_path.chmod(0o600)
        return new_key

    def get_or_create_provider_secret_key(self) -> bytes:
        """Return the Fernet key for encrypting OIDC provider client secrets.

        Generates and persists a new key on first use. Persisting it means
        encrypted provider secrets remain decryptable across restarts.
        """
        from cryptography.fernet import Fernet

        self.ensure_data_dir()
        if self.provider_secret_key_path.exists():
            return self.provider_secret_key_path.read_text().strip().encode("utf-8")
        new_key = Fernet.generate_key()
        self.provider_secret_key_path.write_bytes(new_key)
        self.provider_secret_key_path.chmod(0o600)
        return new_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
