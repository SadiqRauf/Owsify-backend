"""khata entry adjustments

Revision ID: 33cc63580cdb
Revises: f192e574e586
Create Date: 2026-08-20 14:54:17.942194
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '33cc63580cdb'
down_revision: str | None = 'f192e574e586'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow ADJUSTMENT entries, the one type that may carry a negative amount.

    Written by hand: Alembic does not autogenerate CHECK constraint changes, so an
    empty migration here would have silently shipped the old rule. The constraint
    names below are the bare ones — the metadata naming convention adds the
    `ck_<table>_` prefix, and passing the rendered name gets it prefixed twice.

    The old constraint said every amount is positive. The new pair says no amount
    is zero, and only an adjustment may be negative.
    """
    op.drop_constraint("amount_is_positive", "khata_entries", type_="check")

    op.create_check_constraint("amount_is_not_zero", "khata_entries", "amount <> 0")
    op.create_check_constraint(
        "only_adjustments_may_be_negative",
        "khata_entries",
        "entry_type = 'adjustment' OR amount > 0",
    )


def downgrade() -> None:
    """Restore the amount-always-positive rule.

    This fails if any negative adjustment exists, which is the right outcome:
    deleting ledger rows to make a schema fit would destroy financial history.
    Correct or remove those entries deliberately before going back.
    """
    op.drop_constraint("only_adjustments_may_be_negative", "khata_entries", type_="check")
    op.drop_constraint("amount_is_not_zero", "khata_entries", type_="check")

    op.create_check_constraint("amount_is_positive", "khata_entries", "amount > 0")
