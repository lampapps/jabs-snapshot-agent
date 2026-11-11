"""Immediate email alerts for backup events (errors, completions), standalone of any dashboard."""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from . import settings
from .config import EmailConfig

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 10
SMTP_SSL_PORT = 465


def is_configured(email: EmailConfig) -> bool:
    return bool(email.smtp_server and email.to_addrs)


def send(email: EmailConfig, subject: str, body: str) -> bool:
    """Send one plain-text message to email.to_addrs; True if the server accepted it. Never raises."""
    if not is_configured(email):
        logger.debug("email is not configured (smtp_server and to_addrs); not sending '%s'", subject)
        return False
    user = os.environ.get("JABS_SMTP_USER", "")
    password = os.environ.get("JABS_SMTP_PASS", "")
    sender = email.from_addr or user
    if not sender:
        logger.error("no sender address: set email.from_addr or JABS_SMTP_USER")
        return False
    if settings.env_mode() == "development":
        subject = f"[DEV] {subject}"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(email.to_addrs)
    message.set_content(body)
    try:
        context = ssl.create_default_context()
        implicit_tls = email.smtp_port == SMTP_SSL_PORT
        if implicit_tls:
            server: smtplib.SMTP = smtplib.SMTP_SSL(email.smtp_server, email.smtp_port,
                                                    timeout=SMTP_TIMEOUT_S, context=context)
        else:
            server = smtplib.SMTP(email.smtp_server, email.smtp_port, timeout=SMTP_TIMEOUT_S)
        with server:
            if email.use_tls and not implicit_tls:
                server.starttls(context=context)
            if user and password:
                server.login(user, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        logger.error("failed to send email '%s': %s", subject, exc)
        return False
    logger.info("email sent: '%s' to %s", subject, ", ".join(email.to_addrs))
    return True


def notify(email: EmailConfig, event_type: str, subject: str, body: str) -> bool:
    """Send the message if email.notify_on enables event_type ("error" or "backup_complete")."""
    event: Optional[object] = getattr(email.notify_on, event_type, None)
    if event is None or not event.enabled:
        logger.debug("notification for '%s' is disabled", event_type)
        return False
    return send(email, subject, body)
