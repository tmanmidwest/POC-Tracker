"""Outbound email via an admin-configured SMTP server.

The single ``smtp_config`` row (see :mod:`app.models.smtp_config`) holds the
server settings; the password is Fernet-encrypted at rest. This module renders a
message and delivers it over SMTP, honoring the configured security mode
(none / STARTTLS / SSL). It is intentionally small — the only consumer today is
external-user invitations.

Sends are synchronous (the UI routes that call this are sync); a send opens a
short-lived connection and closes it. Failures raise :class:`EmailError` so the
caller can surface a flash message and record an audit event.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.models.smtp_config import (
    SECURITY_SSL,
    SECURITY_STARTTLS,
    SMTP_CONFIG_ID,
    SmtpConfig,
)
from app.services.secret_box import decrypt_secret, encrypt_secret

_CONNECT_TIMEOUT = 15  # seconds


class EmailError(Exception):
    """Base class for email problems."""


class EmailNotConfigured(EmailError):
    """SMTP isn't enabled/configured, so nothing can be sent."""


class EmailSendError(EmailError):
    """The SMTP server rejected the message or the connection failed."""


# ---------------------------------------------------------------------------
# Admin config singleton
# ---------------------------------------------------------------------------


def get_config(db: Session) -> SmtpConfig:
    """Return the singleton SMTP config row, creating an empty one if absent."""
    row = db.get(SmtpConfig, SMTP_CONFIG_ID)
    if row is None:
        row = SmtpConfig(id=SMTP_CONFIG_ID, is_enabled=False)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def set_config(
    db: Session,
    *,
    host: str | None,
    port: int,
    security: str,
    username: str | None,
    password: str | None,
    from_email: str | None,
    from_name: str | None,
    is_enabled: bool,
) -> SmtpConfig:
    """Persist SMTP settings. A blank ``password`` keeps the stored one."""
    row = get_config(db)
    row.host = (host or "").strip() or None
    row.port = port
    row.security = security
    row.username = (username or "").strip() or None
    if password:
        row.password_encrypted = encrypt_secret(password.strip())
    row.from_email = (from_email or "").strip() or None
    row.from_name = (from_name or "").strip() or None
    row.is_enabled = is_enabled
    db.commit()
    db.refresh(row)
    return row


def is_ready(db: Session) -> bool:
    """Whether email is enabled and has the minimum config to send."""
    row = db.get(SmtpConfig, SMTP_CONFIG_ID)
    return bool(row and row.is_enabled and row.is_configured)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_email(
    db: Session,
    *,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    """Send one email using the stored SMTP config.

    Raises :class:`EmailNotConfigured` if email isn't enabled/configured, or
    :class:`EmailSendError` if delivery fails.
    """
    config = get_config(db)
    if not (config.is_enabled and config.is_configured):
        raise EmailNotConfigured(
            "Email is not enabled and configured (need a host and from address)."
        )
    message = _build_message(config, to, subject, text_body, html_body)
    _deliver(config, message, [to])


def send_test_email(db: Session, *, to: str, app_name: str = "Questlog") -> None:
    """Send a fixed 'it works' message to verify the SMTP settings."""
    send_email(
        db,
        to=to,
        subject=f"{app_name} SMTP test",
        text_body=(
            f"This is a test email from {app_name}.\n\n"
            "If you received it, your outbound SMTP settings are working."
        ),
        html_body=(
            f"<p>This is a test email from <strong>{app_name}</strong>.</p>"
            "<p>If you received it, your outbound SMTP settings are working.</p>"
        ),
    )


def _build_message(
    config: SmtpConfig,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None,
) -> EmailMessage:
    msg = EmailMessage()
    from_name = config.from_name or config.from_email
    msg["From"] = (
        f"{from_name} <{config.from_email}>" if config.from_name else config.from_email
    )
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def _deliver(config: SmtpConfig, msg: EmailMessage, recipients: list[str]) -> None:
    password = (
        decrypt_secret(config.password_encrypted)
        if config.password_encrypted
        else None
    )
    try:
        if config.security == SECURITY_SSL:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                config.host or "",
                config.port,
                timeout=_CONNECT_TIMEOUT,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(
                config.host or "", config.port, timeout=_CONNECT_TIMEOUT
            )
        with client:
            client.ehlo()
            if config.security == SECURITY_STARTTLS:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if config.username:
                client.login(config.username, password or "")
            client.send_message(msg, to_addrs=recipients)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailSendError(str(exc)) from exc
