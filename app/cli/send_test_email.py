"""Send a test email using the current settings, and explain what went wrong.

    ./venv/bin/python -m app.cli.send_test_email you@example.com

SMTP failures are famously opaque — "Authentication failed" can mean six different
things depending on the provider. This turns the common ones into a specific next
step instead of a stack trace.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
import sys

from app.core.config import settings
from app.core.email import Email

DIVIDER = "─" * 68


def _describe_config() -> None:
    print(DIVIDER)
    print("Current email configuration")
    print(DIVIDER)
    print(f"  EMAIL_BACKEND : {settings.EMAIL_BACKEND}")
    print(f"  EMAIL_FROM    : {settings.EMAIL_FROM}")
    if settings.EMAIL_BACKEND == "smtp":
        print(f"  SMTP_HOST     : {settings.SMTP_HOST}")
        print(f"  SMTP_PORT     : {settings.SMTP_PORT}")
        print(f"  SMTP_USER     : {settings.SMTP_USER or '(empty)'}")
        print(f"  SMTP_PASSWORD : {'set, ' + str(len(settings.SMTP_PASSWORD)) + ' chars' if settings.SMTP_PASSWORD else '(empty)'}")
        print(f"  SMTP_USE_TLS  : {settings.SMTP_USE_TLS}")
        print(f"  SMTP_USE_SSL  : {settings.SMTP_USE_SSL}")
    elif settings.EMAIL_BACKEND == "file":
        print(f"  EMAIL_FILE_PATH: {settings.EMAIL_FILE_PATH}")
    print(f"  FRONTEND_URL  : {settings.FRONTEND_URL}")
    print(DIVIDER)


def _warn_if_not_really_sending() -> None:
    if settings.EMAIL_BACKEND == "console":
        print()
        print("  NOTE: EMAIL_BACKEND is 'console'. Nothing will leave this machine —")
        print("        the message is printed below instead. Set EMAIL_BACKEND=smtp")
        print("        in backend/.env to actually send.")
        print()
    elif settings.EMAIL_BACKEND == "file":
        print()
        print(f"  NOTE: EMAIL_BACKEND is 'file'. The message is written into")
        print(f"        {settings.EMAIL_FILE_PATH} rather than sent.")
        print()


def _diagnose(error: Exception) -> str:
    """Turn an SMTP exception into something actionable.

    Order matters and is not obvious: both smtplib.SMTPException and ssl.SSLError
    subclass OSError, so the specific cases have to be matched before any generic
    connection branch or they get the wrong explanation.
    """
    host, port = settings.SMTP_HOST, settings.SMTP_PORT

    # --- Provider policy: the reply reached us, it just said no ------------- #
    if isinstance(error, smtplib.SMTPAuthenticationError):
        detail = (error.smtp_error or b"").decode("utf-8", "replace").strip()
        return "\n".join(
            [
                f"The server rejected the credentials (code {error.smtp_code}).",
                f"  {detail}",
                "",
                "SMTP_USER and SMTP_PASSWORD must match the provider exactly:",
                "  Resend    — SMTP_USER is the literal word 'resend', SMTP_PASSWORD is the API key",
                "  Mailtrap  — the pair from Sandboxes → your sandbox → Integration",
                "  Gmail     — the full address, and a 16-char App Password (never the login password)",
            ]
        )

    if isinstance(error, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                          smtplib.SMTPDataError)):
        reply = _reply_text(error)

        if "verify a domain" in reply or "your own email address" in reply:
            return (
                "The provider accepted the connection but refused the recipient:\n"
                f"  {reply}\n\n"
                "This is the free-tier sandbox rule, not a bug. Until a sending domain\n"
                "is verified you can only email your own account address.\n\n"
                "To invite anyone else:\n"
                "  1. Add and verify a domain at https://resend.com/domains\n"
                "  2. Set EMAIL_FROM to an address on it, e.g.\n"
                '     EMAIL_FROM="Splitwise <invites@yourdomain.com>"'
            )

        if isinstance(error, smtplib.SMTPSenderRefused):
            return (
                f"The server refused the From address {settings.EMAIL_FROM!r}.\n"
                f"  {reply}\n\n"
                "Providers only accept a sender on a domain you have verified."
            )

        return f"The server refused the message.\n  {reply}"

    if isinstance(error, smtplib.SMTPServerDisconnected):
        return (
            f"{host}:{port} closed the connection unexpectedly.\n"
            "This usually means the TLS mode is wrong for the port — see the pairing below.\n"
            "  465 / 2465             → SMTP_USE_SSL=true,  SMTP_USE_TLS=false\n"
            "  587 / 2587 / 2525 / 25 → SMTP_USE_SSL=false, SMTP_USE_TLS=true"
        )

    # --- TLS (ssl.SSLError subclasses OSError, so it comes first) ----------- #
    if isinstance(error, ssl.SSLCertVerificationError):
        return (
            f"Could not verify the TLS certificate of {host}:{port}.\n"
            f"  {error}\n\n"
            "If this says 'unable to get local issuer certificate', this Python has no\n"
            "CA store. app.core.email falls back to certifi automatically, so the likely\n"
            "cause is that certifi is missing:\n"
            "    ./venv/bin/pip install certifi"
        )

    if isinstance(error, ssl.SSLError):
        return (
            f"TLS handshake failed against {host}:{port}.\n"
            f"  {error}\n\n"
            "The port and the TLS mode have to agree:\n"
            "  465 / 2465             → SMTP_USE_SSL=true,  SMTP_USE_TLS=false\n"
            "  587 / 2587 / 2525 / 25 → SMTP_USE_SSL=false, SMTP_USE_TLS=true"
        )

    # --- Network, last because everything above also subclasses OSError ----- #
    if isinstance(error, (socket.timeout, TimeoutError)):
        return (
            f"Timed out connecting to {host}:{port}.\n"
            "Either the host or port is wrong, or outbound SMTP is blocked on this\n"
            "network. Ports 587 and 2525 usually get through where 25 does not."
        )

    if isinstance(error, socket.gaierror):
        return (
            f"Could not resolve {host!r} ({error}).\n"
            "Check SMTP_HOST for a typo, and that DNS is reachable from here."
        )

    if isinstance(error, OSError):
        return (
            f"Could not reach {host}:{port} ({error}).\n"
            "If SMTP_HOST is still 'localhost' you have not pointed it at a provider yet."
        )

    return f"{type(error).__name__}: {error}"


def _reply_text(error: Exception) -> str:
    """Pull the server's human-readable reply out of an smtplib error."""
    for attribute in ("smtp_error", "recipients", "args"):
        value = getattr(error, attribute, None)
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace").strip()
        if isinstance(value, dict) and value:
            first = next(iter(value.values()))
            if isinstance(first, tuple) and len(first) > 1 and isinstance(first[1], bytes):
                return first[1].decode("utf-8", "replace").strip()
    return str(error)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    recipient = sys.argv[1]
    _describe_config()
    _warn_if_not_really_sending()

    message = Email(
        to=recipient,
        subject="Splitwise test email",
        text_body=(
            "This is a test message from your local Splitwise backend.\n\n"
            "If you are reading this in a real inbox, SMTP is configured correctly "
            "and friend invitations will be delivered.\n"
        ),
        html_body=(
            "<p>This is a test message from your local Splitwise backend.</p>"
            "<p>If you are reading this in a real inbox, SMTP is configured correctly "
            "and friend invitations will be delivered.</p>"
        ),
    )

    # Call the backend directly rather than send_email(), which swallows errors by
    # design. Here we want the exception so it can be explained.
    from app.core.email import _BACKENDS  # noqa: PLC0415

    backend = _BACKENDS.get(settings.EMAIL_BACKEND)
    if backend is None:
        print(f"Unknown EMAIL_BACKEND {settings.EMAIL_BACKEND!r}.")
        return 1

    try:
        backend(message)
    except Exception as error:  # noqa: BLE001 — the whole point is to explain it
        print()
        print("FAILED")
        print(DIVIDER)
        print(_diagnose(error))
        print(DIVIDER)
        return 1

    print()
    if settings.EMAIL_BACKEND == "smtp":
        print(f"SENT to {recipient}. Check that inbox (and its spam folder).")
    else:
        print(f"Handled by the '{settings.EMAIL_BACKEND}' backend — see above, not an inbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
