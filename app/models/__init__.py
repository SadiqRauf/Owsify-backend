"""SQLAlchemy models. Importing this package registers every table on Base.metadata."""

from app.db.base import Base
from app.models.expense import Expense, ExpenseCategory, ExpenseSplit, SplitType
from app.models.friendship import Friendship, FriendshipStatus
from app.models.group import Group, GroupMember, GroupRole
from app.models.invitation import Invitation, InvitationStatus
from app.models.khata import KhataAccount, KhataEntry, KhataEntryType
from app.models.loan import Loan, LoanDirection, LoanPayment, LoanStatus
from app.models.note import Note, NoteSubject, Reminder, ReminderStatus
from app.models.refresh_token import RefreshToken
from app.models.settlement import PaymentMethod, Settlement
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
    "KhataAccount",
    "KhataEntry",
    "KhataEntryType",
    "Loan",
    "LoanDirection",
    "LoanPayment",
    "LoanStatus",
    "Note",
    "NoteSubject",
    "PaymentMethod",
    "RefreshToken",
    "Reminder",
    "ReminderStatus",
    "Settlement",
    "SplitType",
    "User",
]
