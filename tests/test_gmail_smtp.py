"""Gmail over SMTP.

The transport itself is the same code every provider uses, so these tests cover the
two things that are specific to Google and that fail silently when they are wrong:
the shape of an App Password, and the fact that Gmail will not send as anyone but
the authenticated account.
"""

import pytest

from app.core.config import Settings

GMAIL_BASE = {
    "EMAIL_BACKEND": "smtp",
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_USER": "sadiq@gmail.com",
    "SMTP_PASSWORD": "abcdefghijklmnop",
    "SECRET_KEY": "x" * 40,
}


def build(**overrides) -> Settings:
    """A Settings built from explicit values, ignoring the developer's own .env."""
    return Settings(_env_file=None, **(GMAIL_BASE | overrides))


class TestAppPasswords:
    def test_the_spaces_google_displays_are_stripped(self) -> None:
        """Google shows an App Password as 'abcd efgh ijkl mnop'.

        Pasted verbatim it fails with a bare "Username and Password not accepted"
        that never mentions spaces, which is a long afternoon for whoever hits it.
        """
        settings = build(SMTP_PASSWORD="abcd efgh ijkl mnop")
        assert settings.SMTP_PASSWORD == "abcdefghijklmnop"
        assert len(settings.SMTP_PASSWORD) == 16

    def test_a_password_without_spaces_is_untouched(self) -> None:
        assert build(SMTP_PASSWORD="abcdefghijklmnop").SMTP_PASSWORD == "abcdefghijklmnop"

    def test_other_providers_keep_their_password_exactly(self) -> None:
        """Only Google's passwords are known never to contain spaces, so only
        Google's are rewritten."""
        settings = build(
            SMTP_HOST="smtp.resend.com",
            SMTP_USER="resend",
            SMTP_PASSWORD="re_ab cd_secret",
        )
        assert settings.SMTP_PASSWORD == "re_ab cd_secret"


class TestTheFromAddress:
    def test_a_foreign_from_is_aligned_to_the_account(self) -> None:
        """Gmail refuses to send as anyone else, or quietly rewrites the header.

        Doing it here means the address the app thinks it used is the address that
        actually goes out.
        """
        settings = build(EMAIL_FROM="Owsify <no-reply@owsify.local>")
        assert settings.EMAIL_FROM == "Owsify <sadiq@gmail.com>"

    def test_the_display_name_survives(self) -> None:
        settings = build(EMAIL_FROM="Owsify Notifications <no-reply@owsify.local>")
        assert settings.EMAIL_FROM == "Owsify Notifications <sadiq@gmail.com>"

    def test_a_matching_from_is_left_alone(self) -> None:
        settings = build(EMAIL_FROM="Owsify <sadiq@gmail.com>")
        assert settings.EMAIL_FROM == "Owsify <sadiq@gmail.com>"

    def test_matching_is_case_insensitive(self) -> None:
        """Email addresses are not case sensitive, so this must not churn."""
        settings = build(EMAIL_FROM="Owsify <Sadiq@Gmail.com>")
        assert settings.EMAIL_FROM == "Owsify <Sadiq@Gmail.com>"

    def test_a_bare_address_gains_a_display_name(self) -> None:
        settings = build(EMAIL_FROM="no-reply@owsify.local")
        assert settings.EMAIL_FROM == "Owsify <sadiq@gmail.com>"

    def test_googlemail_is_gmail(self) -> None:
        settings = build(
            SMTP_HOST="smtp.googlemail.com", EMAIL_FROM="Owsify <no-reply@owsify.local>"
        )
        assert settings.EMAIL_FROM == "Owsify <sadiq@gmail.com>"


class TestOtherProvidersAreUntouched:
    def test_a_resend_from_is_left_alone(self) -> None:
        """Every other provider allows a From on any domain you have verified, so
        rewriting theirs would break a working setup."""
        settings = build(
            SMTP_HOST="smtp.resend.com",
            SMTP_USER="resend",
            EMAIL_FROM="Owsify <invites@owsify.com>",
        )
        assert settings.EMAIL_FROM == "Owsify <invites@owsify.com>"

    def test_nothing_happens_when_smtp_is_not_the_backend(self) -> None:
        """The console and file backends never authenticate, so there is nothing to
        align against."""
        settings = build(
            EMAIL_BACKEND="console",
            SMTP_PASSWORD="abcd efgh ijkl mnop",
            EMAIL_FROM="Owsify <no-reply@owsify.local>",
        )
        assert settings.EMAIL_FROM == "Owsify <no-reply@owsify.local>"
        assert settings.SMTP_PASSWORD == "abcd efgh ijkl mnop"


class TestPortPairing:
    @pytest.mark.parametrize(
        ("port", "use_tls", "use_ssl"),
        [(587, True, False), (465, False, True)],
    )
    def test_both_gmail_ports_are_accepted(self, port: int, use_tls: bool, use_ssl: bool) -> None:
        """587 with STARTTLS and 465 with implicit SSL are both valid for Gmail;
        the flags just have to match the port."""
        settings = build(SMTP_PORT=port, SMTP_USE_TLS=use_tls, SMTP_USE_SSL=use_ssl)
        assert settings.SMTP_PORT == port
        assert settings.SMTP_USE_TLS is use_tls
        assert settings.SMTP_USE_SSL is use_ssl
