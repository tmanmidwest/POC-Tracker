"""Self-service password reset via a one-time, emailed token.

A local user who is locked out (or just forgot their password) requests a reset
by username or email. We create a single-use, short-lived token, store only its
SHA-256 hash, and email the plaintext link (same model as invitations). Opening
the link and setting a new password marks the token used and clears any lockout.

To avoid account enumeration, the request flow never reveals whether an account
exists — the route always shows the same "check your email" confirmation. This
module records audit events so an operator can see what happened.

Only local accounts (``password_hash`` set, active) can reset; OIDC users manage
credentials at their identity provider.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppUser, PasswordResetToken
from app.models.password_reset_token import (
    STATUS_PENDING,
    STATUS_REVOKED,
    STATUS_USED,
)
from app.services import email as email_service
from app.services import login_security
from app.services.audit import record_event
from app.services.passwords import hash_password
from app.services.tokens import hash_token

RESET_TTL_HOURS = 2
_TOKEN_BYTES = 32
_MIN_PASSWORD_LEN = 8


class PasswordResetError(Exception):
    """A problem completing a password reset."""


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; treat a naive value as UTC for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def reset_url(token: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/reset/{token}"


def _find_local_user(db: Session, identifier: str) -> AppUser | None:
    """Find an active local (password) account by username or email."""
    ident = identifier.strip()
    if not ident:
        return None
    return (
        db.query(AppUser)
        .filter(
            AppUser.password_hash.isnot(None),
            AppUser.is_active.is_(True),
            or_(AppUser.username == ident, AppUser.email == ident.lower()),
        )
        .first()
    )


def request_reset(
    db: Session,
    *,
    identifier: str,
    base_url: str,
    request: Request | None = None,
) -> None:
    """Create and email a reset token for the matching local account.

    Non-enumerating: returns normally whether or not an account matched, whether
    or not it had an email, and whether or not SMTP delivery succeeded. Records an
    audit event in each case so operators retain visibility.
    """
    user = _find_local_user(db, identifier)
    if user is None:
        record_event(
            category="auth",
            event_type="password_reset.no_account",
            outcome="failure",
            actor_type="user",
            actor_label=identifier.strip() or "(blank)",
            message="Password reset requested for an unknown/ineligible account",
            detail={"surface": "ui"},
            request=request,
        )
        return

    if not user.email:
        record_event(
            category="auth",
            event_type="password_reset.no_email",
            outcome="failure",
            actor_type="user",
            actor_label=user.username,
            actor_id=user.id,
            target_type="app_user",
            target_id=user.id,
            target_label=user.username,
            message=(
                f"Password reset requested for '{user.username}', but the account "
                f"has no email address. Have an admin unlock/reset it instead."
            ),
            detail={"surface": "ui"},
            request=request,
        )
        return

    # Invalidate any earlier pending tokens so only the newest link works.
    (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.status == STATUS_PENDING,
        )
        .update({PasswordResetToken.status: STATUS_REVOKED}, synchronize_session=False)
    )

    token = _new_token()
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token(token),
        status=STATUS_PENDING,
        expires_at=_now() + timedelta(hours=RESET_TTL_HOURS),
    )
    db.add(reset)
    db.commit()

    try:
        _send_reset_email(db, user, token, base_url)
    except email_service.EmailError as exc:
        record_event(
            category="auth",
            event_type="password_reset.email_failed",
            outcome="failure",
            actor_type="user",
            actor_label=user.username,
            actor_id=user.id,
            target_type="app_user",
            target_id=user.id,
            target_label=user.email,
            message=f"Failed to email a password-reset link to '{user.username}'",
            detail={"surface": "ui", "error": str(exc)},
            request=request,
        )
        return

    record_event(
        category="auth",
        event_type="password_reset.requested",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="app_user",
        target_id=user.id,
        target_label=user.email,
        message=f"Emailed a password-reset link to '{user.username}'",
        detail={"surface": "ui"},
        request=request,
    )


def verify_token(db: Session, token: str) -> PasswordResetToken | None:
    """Return the pending, unexpired token for this plaintext, or None."""
    if not token:
        return None
    reset = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == hash_token(token))
        .one_or_none()
    )
    if reset is None or reset.status != STATUS_PENDING:
        return None
    if _ensure_aware(reset.expires_at) < _now():
        return None
    return reset


def consume(
    db: Session,
    reset: PasswordResetToken,
    *,
    new_password: str,
    request: Request | None = None,
) -> AppUser:
    """Set the account's new password, mark the token used, and clear any lockout."""
    if len(new_password) < _MIN_PASSWORD_LEN:
        raise PasswordResetError(
            f"Password must be at least {_MIN_PASSWORD_LEN} characters."
        )
    user = reset.user
    user.password_hash = hash_password(new_password)
    login_security.clear_lockout(user)
    reset.status = STATUS_USED
    reset.used_at = _now()
    # Any other outstanding tokens for this user are now moot.
    (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.status == STATUS_PENDING,
            PasswordResetToken.id != reset.id,
        )
        .update({PasswordResetToken.status: STATUS_REVOKED}, synchronize_session=False)
    )
    db.commit()

    record_event(
        category="auth",
        event_type="password_reset.completed",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="app_user",
        target_id=user.id,
        target_label=user.username,
        message=f"{user.username} reset their password",
        detail={"surface": "ui"},
        request=request,
    )
    return user


def _send_reset_email(
    db: Session, user: AppUser, token: str, base_url: str
) -> None:
    app_name = get_settings().app_name
    url = reset_url(token, base_url)
    subject = f"Reset your {app_name} password"
    greeting = f"Hi {user.display_name}," if user.display_name else "Hi,"
    text = (
        f"{greeting}\n\n"
        f"We received a request to reset the password for your {app_name} "
        f"account ({user.username}).\n\n"
        f"Open this link to choose a new password:\n{url}\n\n"
        f"This link expires in {RESET_TTL_HOURS} hours and can be used once. "
        f"If you didn't request this, you can ignore this email.\n"
    )
    html = (
        f"<p>{greeting}</p>"
        f"<p>We received a request to reset the password for your "
        f"<strong>{app_name}</strong> account ({user.username}).</p>"
        f'<p><a href="{url}">Choose a new password</a></p>'
        f"<p style=\"color:#666;font-size:13px;\">This link expires in "
        f"{RESET_TTL_HOURS} hours and can be used once. If you didn't request "
        f"this, you can ignore this email.</p>"
    )
    email_service.send_email(
        db, to=user.email, subject=subject, text_body=text, html_body=html
    )
