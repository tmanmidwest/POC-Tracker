"""Failed-login lockout for local (password) accounts.

Brute-force protection: after ``settings.max_login_attempts`` consecutive failed
sign-ins, a local account is locked. Lockout is *strict* — there is no
time-based auto-unlock; a locked account is restored only by an admin unlock
(Settings → Users) or by the user completing a password reset. The seeded admin
is not exempt, so the ``poct-reset-admin`` CLI also clears the lock as a
host-level break-glass.

Only accounts with a local password (``password_hash`` set) are tracked — OIDC
users authenticate through their provider and never hit this path.

Both login surfaces (the HTML form and the JSON API) funnel failures and
successes through here so they behave identically.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppUser
from app.services.audit import record_event

LOCKED_MESSAGE = (
    "This account is locked after too many failed sign-in attempts. "
    "Reset your password to unlock it."
)


def _now() -> datetime:
    return datetime.now(UTC)


def lockout_enabled() -> bool:
    return get_settings().max_login_attempts > 0


def is_locked(user: AppUser | None) -> bool:
    """Whether this account is currently locked out of password sign-in."""
    return user is not None and user.locked_at is not None


def clear_lockout(user: AppUser) -> None:
    """Reset the failure counter and unlock. Caller commits.

    Used on successful login and by every recovery path (admin unlock, password
    change, password reset, CLI reset).
    """
    user.failed_login_count = 0
    user.locked_at = None


def register_success(db: Session, user: AppUser) -> None:
    """Clear any accumulated failures after a successful sign-in."""
    if user.failed_login_count or user.locked_at is not None:
        clear_lockout(user)
        db.commit()


def register_failure(
    db: Session,
    user: AppUser,
    *,
    request: Request | None = None,
    surface: str,
) -> bool:
    """Record a failed password attempt for a local user; lock at the threshold.

    Returns True if this failure just locked the account. No-ops for users
    without a local password (OIDC) or when lockout is disabled.
    """
    if user.password_hash is None or not lockout_enabled():
        return False

    user.failed_login_count = (user.failed_login_count or 0) + 1
    threshold = get_settings().max_login_attempts
    just_locked = False
    if user.failed_login_count >= threshold and user.locked_at is None:
        user.locked_at = _now()
        just_locked = True
    db.commit()

    if just_locked:
        record_event(
            category="auth",
            event_type="auth.login.locked",
            outcome="failure",
            actor_type="user",
            actor_label=user.username,
            actor_id=user.id,
            target_type="app_user",
            target_id=user.id,
            target_label=user.username,
            message=(
                f"Locked '{user.username}' after "
                f"{user.failed_login_count} failed sign-in attempts"
            ),
            detail={"surface": surface, "threshold": threshold},
            request=request,
        )
    return just_locked


def record_blocked_attempt(
    db: Session,
    user: AppUser,
    *,
    request: Request | None = None,
    surface: str,
) -> None:
    """Audit a sign-in attempt against an already-locked account."""
    record_event(
        category="auth",
        event_type="auth.login.blocked",
        outcome="failure",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="app_user",
        target_id=user.id,
        target_label=user.username,
        message=f"Blocked sign-in for locked account '{user.username}'",
        detail={"surface": surface},
        request=request,
    )
