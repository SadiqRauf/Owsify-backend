"""SQLAlchemy models. Importing this package registers every table on Base.metadata."""

from app.db.base import Base
from app.models.expense import Expense, ExpenseCategory, ExpenseSplit, SplitType
from app.models.friendship import Friendship, FriendshipStatus
from app.models.group import Group, GroupMember, GroupRole
from app.models.invitation import Invitation, InvitationStatus
from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = [
    "Base",
    "Expense",
    "ExpenseCategory",
    "ExpenseSplit",
    "Friendship",
    "FriendshipStatus",
    "Group",
    "GroupMember",
    "GroupRole",
    "Invitation",
    "InvitationStatus",
    "RefreshToken",
    "SplitType",
    "User",
]
