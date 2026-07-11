"""External-user invitations: create, verify, accept, resend, revoke.

An invite provisions (or reuses) an external ``AppUser``, grants it a project,
and emails a one-time link. The token is stored only as a SHA-256 hash (the
plaintext is emailed once); accepting sets the user's password and activates the
account. See docs/INVITATIONS.md.

Only the service layer lives here. The admin entry points (project Share panel,
Users page) are wired in Phase 3; the public accept page is in app/ui/invite_routes.py.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppUser, Project, ProjectGrant, UserInvite
from app.models.project_grant import TIER_VIEWER
from app.models.user_invite import STATUS_ACCEPTED, STATUS_PENDING, STATUS_REVOKED
from app.services import email as email_service
from app.services.audit import record_event
from app.services.passwords import hash_password
from app.services.tokens import hash_token

INVITE_TTL_DAYS = 7
_TOKEN_BYTES = 32
_MIN_PASSWORD_LEN = 8


class InvitationError(Exception):
    """A problem creating or accepting an invitation."""


def _now() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; treat a naive value as UTC for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _resolve_base_url(explicit: str | None) -> str:
    """Externally-reachable base URL for building the accept link."""
    base = (explicit or get_settings().public_base_url or "").rstrip("/")
    if not base:
        raise InvitationError(
            "No public base URL is configured. Set POCT_PUBLIC_BASE_URL (or pass "
            "base_url) so invitation links point at this app."
        )
    return base


def accept_url(token: str, base_url: str) -> str:
    return f"{base_url}/invite/{token}"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_invite(
    db: Session,
    *,
    email: str,
    name: str | None = None,
    company: str | None = None,
    project: Project | None = None,
    invited_by: AppUser | None = None,
    base_url: str | None = None,
) -> tuple[UserInvite, str]:
    """Create (or refresh) an external user, grant the project, and email a link.

    Returns ``(invite, plaintext_token)``. The rows are committed before the email
    is sent, so a delivery failure raises but leaves a resend-able pending invite.
    """
    norm = _normalize_email(email)
    if "@" not in norm:
        raise InvitationError("A valid email address is required.")

    existing = db.query(AppUser).filter(AppUser.email == norm).one_or_none()
    if existing is None:
        # A username collision (usernames double as emails for invited users)
        # would break login; refuse rather than silently reuse an account.
        if db.query(AppUser).filter(AppUser.username == norm).one_or_none() is not None:
            raise InvitationError(f"A user named '{norm}' already exists.")
        user = AppUser(
            username=norm,
            email=norm,
            company=(company or None),
            display_name=(name or None),
            password_hash=None,  # set when they accept
            is_active=True,
            is_external=True,
        )
        db.add(user)
        db.flush()  # assign user.id
    else:
        # Never turn an internal account into an external one via invite.
        if not existing.is_external:
            raise InvitationError("That email belongs to an internal user.")
        user = existing
        if name:
            user.display_name = name
        if company:
            user.company = company

    if project is not None:
        already = (
            db.query(ProjectGrant)
            .filter(
                ProjectGrant.project_id == project.id,
                ProjectGrant.user_id == user.id,
            )
            .one_or_none()
        )
        if already is None:
            db.add(
                ProjectGrant(
                    project_id=project.id,
                    user_id=user.id,
                    tier=TIER_VIEWER,
                    granted_by_user_id=(invited_by.id if invited_by else None),
                )
            )

    token = _new_token()
    invite = UserInvite(
        user_id=user.id,
        project_id=(project.id if project else None),
        email=norm,
        company=(company or None),
        invited_name=(name or None),
        token_hash=hash_token(token),
        status=STATUS_PENDING,
        expires_at=_now() + timedelta(days=INVITE_TTL_DAYS),
        invited_by_user_id=(invited_by.id if invited_by else None),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)

    _send_invite_email(db, invite, token, base_url)
    record_event(
        category="invitation",
        event_type="invitation.sent",
        actor_type="user" if invited_by else "system",
        actor_label=(invited_by.username if invited_by else None),
        actor_id=(invited_by.id if invited_by else None),
        target_type="user_invite",
        target_id=invite.id,
        target_label=norm,
        message=f"Invited {norm} to view '{project.display_name}'"
        if project
        else f"Invited {norm}",
        detail={"user_id": user.id, "project_id": invite.project_id},
    )
    return invite, token


def _send_invite_email(
    db: Session, invite: UserInvite, token: str, base_url: str | None
) -> None:
    settings = get_settings()
    url = accept_url(token, _resolve_base_url(base_url))
    app_name = settings.app_name
    project_name = invite.project.display_name if invite.project else None

    subject = (
        f"You're invited to view {project_name} on {app_name}"
        if project_name
        else f"You're invited to {app_name}"
    )
    greeting = f"Hi {invite.invited_name}," if invite.invited_name else "Hi,"
    context = (
        f'You have been invited to view the project "{project_name}" on {app_name}.'
        if project_name
        else f"You have been invited to {app_name}."
    )
    text = (
        f"{greeting}\n\n{context}\n\n"
        f"Set your password and get access:\n{url}\n\n"
        f"This link expires in {INVITE_TTL_DAYS} days."
    )
    html = (
        f"<p>{greeting}</p><p>{context}</p>"
        f'<p><a href="{url}">Set your password and get access</a></p>'
        f"<p>This link expires in {INVITE_TTL_DAYS} days.</p>"
    )
    email_service.send_email(
        db, to=invite.email, subject=subject, text_body=text, html_body=html
    )


# ---------------------------------------------------------------------------
# Verify / accept / resend / revoke
# ---------------------------------------------------------------------------


def verify_token(db: Session, token: str) -> UserInvite | None:
    """Return the pending, unexpired invite for ``token``, or None."""
    if not token:
        return None
    invite = (
        db.query(UserInvite)
        .filter(UserInvite.token_hash == hash_token(token))
        .one_or_none()
    )
    if invite is None or invite.status != STATUS_PENDING:
        return None
    if _ensure_aware(invite.expires_at) < _now():
        return None
    return invite


def accept_invite(db: Session, invite: UserInvite, *, password: str) -> AppUser:
    """Set the user's password, activate the account, mark the invite accepted."""
    if len(password) < _MIN_PASSWORD_LEN:
        raise InvitationError(
            f"Password must be at least {_MIN_PASSWORD_LEN} characters."
        )
    user = invite.user
    user.password_hash = hash_password(password)
    user.is_active = True
    # Start the account's expiry term now that they've actually joined.
    from app.services import external_expiry

    external_expiry.set_initial_expiry(user)
    invite.status = STATUS_ACCEPTED
    invite.accepted_at = _now()
    db.commit()
    return user


def resend_invite(
    db: Session, invite: UserInvite, *, base_url: str | None = None
) -> str:
    """Issue a fresh token + expiry and re-send the email. Returns the new token."""
    if invite.status == STATUS_ACCEPTED:
        raise InvitationError("This invitation was already accepted.")
    token = _new_token()
    invite.token_hash = hash_token(token)
    invite.status = STATUS_PENDING
    invite.expires_at = _now() + timedelta(days=INVITE_TTL_DAYS)
    db.commit()
    _send_invite_email(db, invite, token, base_url)
    return token


def revoke_invite(db: Session, invite: UserInvite) -> None:
    """Mark an invitation revoked so its link no longer works."""
    invite.status = STATUS_REVOKED
    db.commit()
