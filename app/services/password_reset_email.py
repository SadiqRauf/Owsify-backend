"""The password reset email.

Kept beside the invitation mail rather than inside `services/auth.py`: composing
markup is a different job from deciding who may reset what, and mixing the two makes
the security-relevant module harder to read.
"""

from urllib.parse import quote

from app.core.config import settings
from app.core.email import Email
from app.models.user import User


def build(user: User, token: str) -> Email:
    # The token goes in the query string rather than the path so it survives any
    # trailing-slash normalising a host might do on the way through.
    url = f"{settings.FRONTEND_URL}/reset-password?token={quote(token)}"
    minutes = settings.PASSWORD_RESET_EXPIRE_MINUTES

    text = f"""Hi {user.full_name},

Someone asked to reset the password for your {settings.PROJECT_NAME} account.

Open this link to choose a new one. It expires in {minutes} minutes and can only
be used once:

{url}

If this was not you, you can ignore this email — your password has not changed,
and the link above is the only way to change it.
"""

    html = f"""<html><body style="font-family:system-ui,-apple-system,sans-serif;line-height:1.6">
  <h2 style="margin-bottom:8px">Reset your password</h2>
  <p style="color:#334155">
    Hi {user.full_name}, someone asked to reset the password for your account.
  </p>
  <p style="margin:24px 0">
    <a href="{url}" style="display:inline-block;background:#047857;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">
      Choose a new password
    </a>
  </p>
  <p style="color:#475569;font-size:14px">
    This link expires in {minutes} minutes and can only be used once.
  </p>
  <p style="color:#94a3b8;font-size:12px">
    If this was not you, you can ignore this email — your password has not changed,
    and the link above is the only way to change it.
  </p>
</body></html>"""

    return Email(
        to=user.email,
        subject=f"Reset your {settings.PROJECT_NAME.replace(' API', '')} password",
        text_body=text,
        html_body=html,
    )
