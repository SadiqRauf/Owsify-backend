"""Email invitations for people who are not on Owsify yet.

The point of an invitation is what happens later: when the invitee registers with
the address they were invited at, every open invite for that address turns into an
accepted friendship, so they land on a populated app rather than an empty one.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email import Email, send_email
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.models.friendship import Friendship, FriendshipStatus
from app.models.invitation import Invitation, InvitationStatus
from app.models.user import User
from app.services import user as user_service

logger = logging.getLogger(__name__)

MAX_OPEN_INVITATIONS = 100


def _invite_url(invitation: Invitation) -> str:
    """Deep link to the register page, prefilled with the invited address."""
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/register?invite={invitation.token}&email={invitation.email}"


def build_invitation_email(invitation: Invitation, inviter: User) -> Email:
    url = _invite_url(invitation)
    note = f"\n\nThey added a note:\n\n  “{invitation.message}”\n" if invitation.message else ""

    text = (
        f"{inviter.full_name} wants to split expenses with you on Owsify."
        f"{note}\n"
        f"Create your account here:\n\n  {url}\n\n"
        f"You will be connected with {inviter.full_name} automatically once you sign up.\n"
        f"This invitation expires in {settings.INVITATION_EXPIRE_DAYS} days.\n\n"
        f"If you were not expecting this, you can ignore this email."
    )

    html = f"""\
<html><body style="font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:#0f172a">
  <p><strong>{inviter.full_name}</strong> wants to split expenses with you on Owsify.</p>
  {f'<blockquote style="border-left:3px solid #cbd5e1;margin:16px 0;padding:4px 0 4px 12px;color:#475569">{invitation.message}</blockquote>' if invitation.message else ''}
  <p>
    <a href="{url}" style="display:inline-block;background:#047857;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">
      Create your account
    </a>
  </p>
  <p style="color:#475569;font-size:14px">
    You will be connected with {inviter.full_name} automatically once you sign up.
    This invitation expires in {settings.INVITATION_EXPIRE_DAYS} days.
  </p>
  <p style="color:#94a3b8;font-size:12px">If you were not expecting this, you can ignore this email.</p>
</body></html>"""

    return Email(
        to=invitation.email,
        subject=f"{inviter.full_name} invited you to Owsify",
        text_body=text,
        html_body=html,
    )


def get_open_for_email(db: Session, email: str) -> list[Invitation]:
    return list(
        db.scalars(
            select(Invitation).where(
                func.lower(Invitation.email) == email.lower(),
                Invitation.status == InvitationStatus.PENDING,
                Invitation.expires_at > datetime.now(UTC),
            )
        )
    )


def list_sent(db: Session, user_id: uuid.UUID) -> list[Invitation]:
    """Invitations this user sent that are still worth showing: open or accepted."""
    return list(
        db.scalars(
            select(Invitation)
            .where(
                Invitation.invited_by_id == user_id,
                Invitation.status != InvitationStatus.CANCELLED,
            )
            .order_by(Invitation.created_at.desc())
        )
    )


def create(db: Session, inviter: User, email: str, message: str | None = None) -> Invitation:
    """Create and send an invitation, refusing the cases that should be something else."""
    address = email.strip().lower()

    if address == inviter.email.lower():
        raise BadRequestError("That is your own email address.")

    # If they already have an account, an invite is the wrong tool — the caller
    # should send a friend request instead, so say so in a way the UI can act on.
    existing_user = user_service.get_by_email(db, address)
    if existing_user is not None:
        raise ConflictError(
            f"{existing_user.full_name} is already on Owsify. Send them a friend request instead.",
            details=[
                {
                    "field": "email",
                    "message": "An account already exists for this address.",
                    "type": "account_exists",
                }
            ],
        )

    already_open = db.scalar(
        select(Invitation).where(
            Invitation.invited_by_id == inviter.id,
            func.lower(Invitation.email) == address,
            Invitation.status == InvitationStatus.PENDING,
            Invitation.expires_at > datetime.now(UTC),
        )
    )
    if already_open is not None:
        raise ConflictError("You have already invited that address.")

    open_count = db.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(
            Invitation.invited_by_id == inviter.id,
            Invitation.status == InvitationStatus.PENDING,
            Invitation.expires_at > datetime.now(UTC),
        )
    )
    if (open_count or 0) >= MAX_OPEN_INVITATIONS:
        raise BadRequestError(
            "You have too many invitations outstanding. Cancel some before sending more."
        )

    invitation = Invitation(
        email=address,
        invited_by_id=inviter.id,
        message=(message or "").strip() or None,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now(UTC) + timedelta(days=settings.INVITATION_EXPIRE_DAYS),
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)
    return invitation


def deliver(invitation: Invitation, inviter: User) -> bool:
    """Send the email. Safe to call from a background task."""
    return send_email(build_invitation_email(invitation, inviter))


def cancel(db: Session, invitation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    invitation = db.get(Invitation, invitation_id)
    if invitation is None or invitation.invited_by_id != user_id:
        raise NotFoundError("Invitation not found.")
    if invitation.status is InvitationStatus.ACCEPTED:
        raise ConflictError("That invitation has already been accepted.")

    invitation.status = InvitationStatus.CANCELLED
    db.commit()


def resend(db: Session, invitation_id: uuid.UUID, user_id: uuid.UUID) -> Invitation:
    """Push the expiry out and hand the invitation back so it can be sent again."""
    invitation = db.get(Invitation, invitation_id)
    if invitation is None or invitation.invited_by_id != user_id:
        raise NotFoundError("Invitation not found.")
    if invitation.status is InvitationStatus.ACCEPTED:
        raise ConflictError("That invitation has already been accepted.")

    invitation.status = InvitationStatus.PENDING
    invitation.expires_at = datetime.now(UTC) + timedelta(days=settings.INVITATION_EXPIRE_DAYS)
    db.commit()
    db.refresh(invitation)
    return invitation


def redeem_for_new_user(db: Session, new_user: User) -> int:
    """Turn every open invite for this address into an accepted friendship.

    Called during registration. Returns how many friendships were created. Errors
    here must never block the signup, so the caller wraps it defensively.
    """
    invitations = get_open_for_email(db, new_user.email)
    if not invitations:
        return 0

    created = 0
    now = datetime.now(UTC)

    for invitation in invitations:
        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = now
        invitation.accepted_by_id = new_user.id

        if invitation.invited_by_id == new_user.id:
            continue

        # A friendship may already exist if two people invited the same address and
        # one of them was also befriended another way.
        existing = db.scalar(
            select(Friendship).where(
                or_(
                    (Friendship.requester_id == invitation.invited_by_id)
                    & (Friendship.addressee_id == new_user.id),
                    (Friendship.requester_id == new_user.id)
                    & (Friendship.addressee_id == invitation.invited_by_id),
                )
            )
        )
        if existing is not None:
            if existing.status is not FriendshipStatus.ACCEPTED:
                existing.status = FriendshipStatus.ACCEPTED
                existing.responded_at = now
            continue

        db.add(
            Friendship(
                requester_id=invitation.invited_by_id,
                addressee_id=new_user.id,
                status=FriendshipStatus.ACCEPTED,
                responded_at=now,
            )
        )
        created += 1

    db.commit()
    logger.info("Redeemed %d invitation(s) for %s", len(invitations), new_user.email)
    return created
