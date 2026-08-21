"""loan direction and counterparty rename

Revision ID: 899c0b04ff44
Revises: da15337b2ebf
Create Date: 2026-08-21 11:42:01.802859
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '899c0b04ff44'
down_revision: str | None = 'da15337b2ebf'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename lender/borrower to owner/counterparty, and add a direction.

    Renames rather than add-and-copy: every existing row is a loan the owner gave,
    so the data needs no transformation — only the columns were misnamed once a loan
    could also run the other way. `lender_id` holding a borrower would be a schema
    that lies about its own rows.
    """
    op.alter_column("loans", "lender_id", new_column_name="owner_id")
    op.alter_column("loans", "borrower_name", new_column_name="counterparty_name")
    op.alter_column("loans", "borrower_user_id", new_column_name="counterparty_user_id")

    # Every loan that already exists was given, which is also the column default.
    op.add_column(
        "loans",
        sa.Column(
            "direction",
            sa.String(length=16),
            nullable=False,
            server_default="given",
        ),
    )

    # The indexes and the check were named after the old columns.
    op.drop_index("ix_loans_lender_due", table_name="loans")
    op.drop_index("ix_loans_lender_id", table_name="loans")
    op.drop_index("ix_loans_borrower_user_id", table_name="loans")
    op.drop_constraint("no_loan_to_yourself", "loans", type_="check")

    op.create_index("ix_loans_owner_id", "loans", ["owner_id"])
    op.create_index("ix_loans_counterparty_user_id", "loans", ["counterparty_user_id"])
    op.create_index("ix_loans_owner_due", "loans", ["owner_id", "due_date"])
    op.create_index("ix_loans_owner_direction", "loans", ["owner_id", "direction"])
    op.create_check_constraint(
        "no_loan_to_yourself", "loans", "owner_id <> counterparty_user_id"
    )

    # The foreign keys keep their old names through a column rename, which would
    # leave the schema describing columns that no longer exist.
    op.execute(
        "ALTER TABLE loans RENAME CONSTRAINT fk_loans_lender_id_users "
        "TO fk_loans_owner_id_users"
    )
    op.execute(
        "ALTER TABLE loans RENAME CONSTRAINT fk_loans_borrower_user_id_users "
        "TO fk_loans_counterparty_user_id_users"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE loans RENAME CONSTRAINT fk_loans_owner_id_users "
        "TO fk_loans_lender_id_users"
    )
    op.execute(
        "ALTER TABLE loans RENAME CONSTRAINT fk_loans_counterparty_user_id_users "
        "TO fk_loans_borrower_user_id_users"
    )
    op.drop_constraint("no_loan_to_yourself", "loans", type_="check")
    op.drop_index("ix_loans_owner_direction", table_name="loans")
    op.drop_index("ix_loans_owner_due", table_name="loans")
    op.drop_index("ix_loans_counterparty_user_id", table_name="loans")
    op.drop_index("ix_loans_owner_id", table_name="loans")

    op.drop_column("loans", "direction")
    op.alter_column("loans", "counterparty_user_id", new_column_name="borrower_user_id")
    op.alter_column("loans", "counterparty_name", new_column_name="borrower_name")
    op.alter_column("loans", "owner_id", new_column_name="lender_id")

    op.create_index("ix_loans_lender_id", "loans", ["lender_id"])
    op.create_index("ix_loans_borrower_user_id", "loans", ["borrower_user_id"])
    op.create_index("ix_loans_lender_due", "loans", ["lender_id", "due_date"])
    op.create_check_constraint(
        "no_loan_to_yourself", "loans", "lender_id <> borrower_user_id"
    )
