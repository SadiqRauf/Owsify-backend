"""Outbound email.

Three backends, chosen with the ``EMAIL_BACKEND`` setting:

* ``console`` — logs the message. The default, so a fresh checkout sends invitations
  without anyone configuring SMTP, and the link is visible in the server log.
* ``file``    — writes each message as an ``.eml`` into ``EMAIL_FILE_PATH``.
* ``smtp``    — actually sends, using the ``SMTP_*`` settings.

Sending is deliberately best-effort: a failure is logged, never raised. An invite
row that exists with an email that did not go out is recoverable (resend it); a
500 on the request that created it is not.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that works on a Python with no system CA store.

    The python.org macOS builds ship without linking to the system trust store, so
    ssl.create_default_context() can come back with zero CA certificates and every
    handshake fails with "unable to get local issuer certificate". Fall back to
    certifi's bundle in that case rather than the usual advice of disabling
    verification, which would leave credentials open to interception.
    """
    context = ssl.create_default_context()

    if not context.get_ca_certs():
        try:
            import certifi
        except ImportError:
            logger.warning(
                "No system CA certificates and certifi is not installed; "
                "TLS verification will fail. Install certifi."
            )
        else:
            context.load_verify_locations(cafile=certifi.where())
            logger.debug("Loaded CA certificates from certifi at %s", certifi.where())

    return context


@dataclass(frozen=True, slots=True)
class Email:
    to: str
    subject: str
    text_body: str
    html_body: str | None = None

    def as_message(self) -> EmailMessage:
        message = EmailMessage()
        message["From"] = settings.EMAIL_FROM
        message["To"] = self.to
        message["Subject"] = self.subject
        message.set_content(self.text_body)
        if self.html_body:
            message.add_alternative(self.html_body, subtype="html")
        return message


def _send_console(email: Email) -> None:
    logger.info(
        "\n--- email (console backend) ---\nTo: %s\nSubject: %s\n\n%s\n--- end email ---",
        email.to,
        email.subject,
        email.text_body,
    )


def _send_file(email: Email) -> None:
    directory = Path(settings.EMAIL_FILE_PATH)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid.uuid4().hex}.eml"
    path.write_text(str(email.as_message()), encoding="utf-8")
    logger.info("Wrote email for %s to %s", email.to, path)


def _send_smtp(email: Email) -> None:
    message = email.as_message()

    if settings.SMTP_USE_SSL:
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.SMTP_HOST,
            settings.SMTP_PORT,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
            context=_ssl_context(),
        )
    else:
        client = smtplib.SMTP(
            settings.SMTP_HOST,
            settings.SMTP_PORT,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        )

    with client:
        if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
            client.starttls(context=_ssl_context())
        if settings.SMTP_USER:
            client.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        client.send_message(message)

    logger.info("Sent email to %s via SMTP", email.to)


_BACKENDS = {
    "console": _send_console,
    "file": _send_file,
    "smtp": _send_smtp,
}


def send_email(email: Email) -> bool:
    """Deliver a message. Returns whether it went out; never raises."""
    backend = _BACKENDS.get(settings.EMAIL_BACKEND, _send_console)
    try:
        backend(email)
        return True
    except Exception:
        logger.exception("Failed to send email to %s (backend=%s)", email.to, settings.EMAIL_BACKEND)
        return False
